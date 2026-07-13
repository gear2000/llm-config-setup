#!/usr/bin/env python3
"""UpAgent Recruiter — the always-up broker that hires a fresh worker per work order.

The Recruiter is a pane in the `shared-services` Herdr workspace. The phase leader places
an order by writing `order.json` and signaling the Recruiter's pane:

    herdr pane run <recruiter-pane> "just upagent recruit <path/to/order.json>"

The Recruiter validates and durably submits the order, starts a per-job Python runner, and
returns immediately. Only the job runner atomically claims that order, then:
  1. resolves the per-harness launch template from the roster (upagent.yaml);
  2. splits a fresh worker pane from the order's cockpit pane (into the cockpit), with the
     order's cwd (the phase worktree) and env (optional OTel vars);
  3. runs the worker, then blocks until it's done — `herdr wait agent-status <worker>
     --status done` for most harnesses, or polling for the result file directly for a harness
     in POLL_RESULT_FILE_HARNESSES (codex — its Herdr integration never reports a "done"
     transition, only a SessionStart registration). Leaders may also run an independent
     lightweight watchdog that watches the order's result_path directly; that watchdog is
     deliberately decoupled from the Recruiter's own wait path and is not managed here;
  4. reads + validates the worker's result.json (must echo the order_id);
  5. closes that worker pane, records terminal job state, and emits `ORDER <order_id> DONE`.

Independent orders no longer queue behind a long-running worker in the Recruiter pane. The
filesystem job ledger supplies exclusive per-order ownership; one job runner still owns one
worker lifecycle end to end.

The RESULT FILE is the source of truth; the `ORDER ... DONE` line is only the accelerator
the leader matches on. If anything goes wrong (herdr error, timeout, missing/bad result),
the Recruiter still writes a fail-loud `blocked` result.json and emits `ORDER ... DONE`, so
the leader is never stranded — it reads the blocked verdict and escalates per its budget.

route.yaml is authoritative for which harness/model/agent runs each stage; the Recruiter
only knows HOW to launch each harness. It never picks the agent.

Pure stdlib + PyYAML (as the sibling specialist hub uses). No Go hub, no tmux — Herdr only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import contracts  # noqa: E402  (sibling module, path-imported)

SHARED_SERVICES_WORKSPACE = "shared-services"
DEFAULT_TIMEOUT_MS = 1_800_000  # 30 min per worker unless the order overrides
STAGE_TIMEOUT_MS = {
    "stage-1-implementation": 10_800_000,
    "stage-2-adversarial-audit": 10_800_000,
}
RESULT_POLL_INTERVAL_S = 2.0
# Harnesses whose Herdr integration never reports an agent-status transition to "done" (only a
# SessionStart registration) — `herdr wait agent-status --status done` structurally never fires
# for these, verified live 2026-07-12 (a codex worker finished and wrote a valid result.json
# while the wait sat stuck for its full timeout). Completion is detected by polling for the
# result file instead, which is the contract's real source of truth regardless of how it's
# detected.
POLL_RESULT_FILE_HARNESSES = frozenset({"codex"})
# Where `up` records the resolved workspace + Recruiter pane so `status`/callers can find it.
STATE_FILE = Path(os.environ.get("UPAGENT_STATE", "/tmp/.upagent/recruiter.json")).expanduser()


def default_roster_path() -> str:
    """Resolve the launch-template roster. The filled roster is repo-owned, so prefer, in order:
      1. $UPAGENT_CONFIG (explicit override);
      2. the repo-owned `this_repo` roster, if the enclosing repo has one — walk up from cwd for
         a `.shared-llm/` dir and look under `.shared-llm/this_repo/extensions/common/upagent/`;
      3. `upagent.yaml` beside this engine (the kit's own adoption — editable in the kit source).
    `load_roster` fails loud if the resolved path does not exist, so a destination that has done
    neither (1) nor (2) gets a clear error rather than silently reading a kit-owned public file.
    Mirrors the Specialist Hub's SPECIALIST_HUB_CONFIG convention.
    """
    env = os.environ.get("UPAGENT_CONFIG")
    if env:
        return env
    for parent in [Path.cwd(), *Path.cwd().parents]:
        this_repo = parent / ".shared-llm/this_repo/extensions/common/upagent/upagent.yaml"
        if this_repo.is_file():
            return str(this_repo)
    return str(HERE / "upagent.yaml")
# Placeholders a launch template may use. The template author decides how each harness
# consumes them; the Recruiter only substitutes. A field absent from the order substitutes
# as "" — so a template flag like `--effort {effort}` needs the order to actually carry a
# value, or the harness CLI will eat the next token as the flag's value.
TEMPLATE_FIELDS = ("model", "agent", "cwd", "instructions_path", "result_path", "effort")


class RecruiterError(RuntimeError):
    """A fail-loud Recruiter fault (bad roster, missing herdr, herdr call failed)."""


class JobLedger:
    """Filesystem copy-on-write job state for concurrent Recruiter requests.

    Request state is immutable except for an atomically replaced ``state/latest.json``.
    ``active/requests/<key>`` is an atomic mkdir claim; its lease is authoritative while
    ``active/by-expiry`` is a disposable index for recovery/reaping.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get(
            "UPAGENT_HUB_DIR", "~/.local/state/herdr/upagent-hub"
        )).expanduser()
        self.requests = self.root / "requests"
        self.active = self.root / "active"

    @staticmethod
    def key(order_id: str) -> str:
        return hashlib.sha256(order_id.encode()).hexdigest()

    def request_dir(self, key: str) -> Path:
        return self.requests / key

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _event(self, key: str, event: str, **detail: object) -> None:
        payload = {"event": event, "at_ns": time.time_ns(), **detail}
        event_path = self.request_dir(key) / "events" / f"{payload['at_ns']}-{uuid.uuid4().hex}.json"
        self._write_json(event_path, payload)

    def _snapshot(self, key: str, state: str, **detail: object) -> None:
        payload = {"state": state, "at_ns": time.time_ns(), **detail}
        self._write_json(self.request_dir(key) / "state" / "latest.json", payload)

    def submit(self, order: dict) -> tuple[str, bool]:
        """Persist a request once. Duplicate identical order ids are idempotent."""
        key = self.key(order["order_id"])
        request = self.request_dir(key)
        try:
            request.mkdir(parents=True)
        except FileExistsError:
            try:
                stored = json.loads((request / "request.json").read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise RecruiterError(f"incomplete request record for {order['order_id']}: {e}") from e
            if stored != order:
                raise RecruiterError(f"order_id collision with different request: {order['order_id']}")
            return key, False
        self._write_json(request / "request.json", order)
        self._event(key, "submitted", order_id=order["order_id"])
        self._snapshot(key, "queued", order_id=order["order_id"])
        return key, True

    def order(self, key: str) -> dict:
        try:
            value = json.loads((self.request_dir(key) / "request.json").read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(f"job request {key} is unreadable: {e}") from e
        if not isinstance(value, dict):
            raise RecruiterError(f"job request {key} is not an object")
        return value

    def claim(self, key: str, order_id: str, timeout_ms: int) -> str | None:
        claim_dir = self.active / "requests" / key
        try:
            claim_dir.mkdir(parents=True)
        except FileExistsError:
            return None
        token = uuid.uuid4().hex
        expiry_epoch = int(time.time() + timeout_ms / 1000) + 60
        lease = {"order_id": order_id, "token": token, "expires_at": expiry_epoch}
        self._write_json(claim_dir / "lease.json", lease)
        self._write_json(self.active / "by-expiry" / str(expiry_epoch) / f"{key}-{token}.json", lease)
        self._event(key, "claimed", **lease)
        self._snapshot(key, "running", **lease)
        return token

    def finish(self, key: str, token: str, verdict: str, **detail: object) -> None:
        claim_dir = self.active / "requests" / key
        lease_path = claim_dir / "lease.json"
        try:
            lease = json.loads(lease_path.read_text())
        except (OSError, json.JSONDecodeError):
            lease = {}
        if lease.get("token") != token:
            return
        self._event(key, "finished", verdict=verdict, **detail)
        self._snapshot(key, "finished", verdict=verdict, **detail)
        shutil.rmtree(claim_dir, ignore_errors=True)


# --- pure, unit-testable core ------------------------------------------------


def load_roster(path: str | Path) -> dict:
    """Read + validate the launch-template roster (upagent.yaml). Fail-loud.

    Shape:
        harnesses:
          claude: "<launch template with {placeholders}>"
          codex:  "..."
    """
    p = Path(path)
    if not p.is_file():
        raise RecruiterError(f"roster not found: {p} (template: upagent.yaml.example)")
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        # Surface an unreadable file or invalid YAML as a RecruiterError so cmd_recruit's
        # fallback catches it (a blocked result + DONE) instead of it escaping past main().
        raise RecruiterError(f"roster {p} is unreadable or invalid YAML: {e}") from e
    harnesses = data.get("harnesses")
    if not isinstance(harnesses, dict) or not harnesses:
        raise RecruiterError(f"{p} must define a non-empty `harnesses:` map")
    for name, tmpl in harnesses.items():
        if not isinstance(tmpl, str) or not tmpl.strip():
            raise RecruiterError(f"{p} harness `{name}` must map to a non-empty template string")
    return data


def _default_timeout_ms(stage_id: str) -> int:
    """Return the stage-specific default without duplicating stage validation."""
    return STAGE_TIMEOUT_MS.get(stage_id, DEFAULT_TIMEOUT_MS)


def _load_normalized_result(path: str | Path, expected_order_id: str) -> dict:
    """Apply logged cosmetic repairs at the Recruiter's sole result-read boundary."""
    result_path = Path(path)
    if not result_path.is_file():
        raise contracts.ContractError(f"result.json not found: {result_path}")
    try:
        raw = json.loads(result_path.read_text())
    except json.JSONDecodeError as e:
        raise contracts.ContractError(f"result.json is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise contracts.ContractError("result.json must be a JSON object")
    normalized, corrections = contracts.normalize_cosmetic(raw)
    if corrections:
        sys.stderr.write(
            "recruiter: auto-corrected cosmetic result.json "
            f"({'; '.join(corrections)}) — not a real failure\n"
        )
    return contracts.parse_result(json.dumps(normalized), expected_order_id=expected_order_id)


def resolve_launch_command(order: dict, roster: dict) -> str:
    """Substitute an order's fields into its harness launch template. Pure. Fail-loud on an
    unknown harness or a template referencing an unknown placeholder."""
    harness = order["harness"]
    template = roster.get("harnesses", {}).get(harness)
    if template is None:
        raise RecruiterError(
            f"no launch template for harness {harness!r} in the roster; "
            f"add it under harnesses: (have: {', '.join(roster.get('harnesses', {}))})"
        )
    fields = {k: order.get(k, "") for k in TEMPLATE_FIELDS}
    try:
        return template.format(**fields)
    except KeyError as e:
        raise RecruiterError(
            f"harness {harness!r} template references unknown placeholder {e}; "
            f"allowed: {', '.join('{%s}' % f for f in TEMPLATE_FIELDS)}"
        ) from e


# --- herdr runtime helpers ---------------------------------------------------


def _herdr_available() -> None:
    if shutil.which("herdr") is None:
        raise RecruiterError("`herdr` not found in PATH — the Recruiter runs inside Herdr")


def _herdr_json(*args: str) -> dict:
    """Run a herdr subcommand expected to print JSON; return the parsed object. Fail-loud."""
    _herdr_available()
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecruiterError(f"herdr {' '.join(args)} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RecruiterError(f"herdr {' '.join(args)} did not print JSON: {proc.stdout[:200]}") from e


def _herdr(*args: str) -> None:
    """Run a herdr subcommand that prints nothing on success. Fail-loud on non-zero."""
    _herdr_available()
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecruiterError(f"herdr {' '.join(args)} failed: {proc.stderr.strip()}")


def _wait_for_result_file(result_path: Path, timeout_ms: str) -> None:
    """Poll for the worker's result file instead of waiting on herdr agent-status. Used for
    harnesses in POLL_RESULT_FILE_HARNESSES, whose integration hook never reports a "done"
    transition. Fail-loud on timeout, same as the agent-status wait it replaces."""
    deadline = time.monotonic() + int(timeout_ms) / 1000
    while time.monotonic() < deadline:
        if result_path.is_file():
            return
        time.sleep(RESULT_POLL_INTERVAL_S)
    raise RecruiterError(f"timed out waiting for {result_path} to appear")


def _report_state(pane: str | None, state: str, message: str) -> None:
    """Surface the Recruiter in Herdr's agents sidebar (`pane report-agent`). BEST-EFFORT:
    status display must never break a hire, so herdr faults are swallowed. `pane` may be
    None when the caller cannot know its own pane (then this is a no-op)."""
    if not pane:
        return
    try:
        _herdr(
            "pane", "report-agent", pane,
            "--source", "upagent-recruiter", "--agent", "recruiter",
            "--state", state, "--message", message,
        )
    except (RecruiterError, OSError):
        pass


def _write_blocked_result(order: dict, reason: str) -> dict | None:
    """Ensure a result.json exists so the leader is never stranded. Only writes a fallback
    `blocked` result when the worker did not leave a valid one of its own.

    Returns the worker's existing parsed result when one was already present and valid for this
    order; callers use that as the authoritative outcome instead of treating the order as a
    Recruiter fallback.

    BEST-EFFORT and never raises: it runs from cmd_recruit's except path, so a filesystem fault
    here must not escape (which would skip the `ORDER <id> DONE` emission). If it truly cannot
    write, the leader's bounded `wait output --timeout` falls back to treating the stage as
    blocked anyway."""
    try:
        result_path = Path(order["result_path"])
        if result_path.is_file():
            try:
                return contracts.parse_result(
                    result_path.read_text(), expected_order_id=order["order_id"]
                )
            except (contracts.ContractError, OSError):
                pass  # unreadable/invalid/stale → fall through and overwrite with a blocked result
        result_path.parent.mkdir(parents=True, exist_ok=True)
        # Only name a stage in `revisit` when it is a recognized one (a malformed order may carry
        # no valid stage_id); an unrecognized stage would fail result re-validation.
        stage = order.get("stage_id")
        revisit = [stage] if stage in contracts.RECOGNIZED_STAGE_IDS else []
        result_path.write_text(
            json.dumps(
                {
                    "order_id": order["order_id"],
                    "verdict": "blocked",
                    "revisit": revisit,
                    "reason": f"recruiter: {reason}",
                    "full_log": "(none — worker did not run to completion)",
                },
                indent=2,
            )
        )
    except OSError as e:
        sys.stderr.write(f"recruiter: could not write blocked result for {order.get('order_id')}: {e}\n")
    return None


# --- commands ----------------------------------------------------------------


def _recover_order_fields(order_path: str) -> tuple[str, str] | None:
    """Best-effort (order_id, result_path) from a malformed order.json, so the Recruiter can
    still leave a blocked result + emit DONE instead of stranding the leader. Returns None if
    the file is too broken to recover either field."""
    try:
        raw = json.loads(Path(order_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    order_id, result_path = raw.get("order_id"), raw.get("result_path")
    if isinstance(order_id, str) and order_id and isinstance(result_path, str) and result_path:
        return order_id, result_path
    return None


def _run_order(order_path: str, roster_path: str) -> int:
    """Hire one worker for one order. Emits `ORDER <id> DONE` whenever the order_id is known (a
    result.json always exists after that). Returns 0 on a clean hire, 1 on a blocked fallback,
    2 when the order is too malformed to even recover an id (leader falls back on its timeout)."""
    try:
        order = contracts.load_order(order_path)
    except contracts.ContractError as e:
        # Malformed order: try to still honor the DONE contract so the leader is not stranded.
        recovered = _recover_order_fields(order_path)
        if recovered is None:
            sys.stderr.write(f"recruiter: unrecoverable order {order_path}: {e}\n")
            return 2
        order_id, result_path = recovered
        _write_blocked_result(
            {"order_id": order_id, "result_path": result_path, "stage_id": "unknown"},
            f"malformed order.json: {e}",
        )
        print(f"ORDER {order_id} DONE", flush=True)
        return 1
    order_id = order["order_id"]
    fell_back = False
    worker_pane: str | None = None
    # The armed recruit() runs inside the Recruiter's own pane, so HERDR_PANE_ID names it;
    # flip the sidebar to working for the duration of the hire (best-effort, may be None
    # when recruit is invoked from outside a Herdr pane).
    my_pane = os.environ.get("HERDR_PANE_ID")
    _report_state(my_pane, "working", f"hiring for {order_id}")
    try:
        # Everything that can fail lives INSIDE the fallback block, now that order_id is known, so
        # a bad roster / launch / Herdr call still writes a blocked result and emits DONE rather
        # than raising past main() and stranding the leader.
        roster = load_roster(roster_path)
        # Remove any stale result.json from a prior try at this path BEFORE launching, so the only
        # result we can read is the one this worker writes (never a leftover from an earlier try).
        Path(order["result_path"]).unlink(missing_ok=True)
        launch = resolve_launch_command(order, roster)
        # `herdr pane split` splits an EXISTING pane; the order carries the cockpit pane to
        # split the worker from (there is no --workspace on split). This places the worker in
        # the cockpit beside the phase leader, per the topology.
        split_args = [
            "pane", "split", order["cockpit_pane"],
            "--direction", "right", "--no-focus",
            "--cwd", order["cwd"],
        ]
        for k, v in (order.get("env") or {}).items():
            split_args += ["--env", f"{k}={v}"]
        split = _herdr_json(*split_args)
        candidate_pane = split.get("result", {}).get("pane", {}).get("pane_id")
        if not isinstance(candidate_pane, str) or not candidate_pane:
            raise RecruiterError("herdr pane split response has no pane_id")
        worker_pane = candidate_pane

        _herdr("pane", "run", worker_pane, launch)
        timeout = str(order.get("timeout_ms") or _default_timeout_ms(order["stage_id"]))
        if order["harness"] in POLL_RESULT_FILE_HARNESSES:
            _wait_for_result_file(Path(order["result_path"]), timeout)
        else:
            _herdr("wait", "agent-status", worker_pane, "--status", "done", "--timeout", timeout)

        # The worker must have written a valid result.json echoing this order_id. Only this
        # Recruiter boundary normalizes explicitly listed cosmetic mistakes; contracts stay strict.
        _load_normalized_result(order["result_path"], expected_order_id=order_id)
    except (RecruiterError, contracts.ContractError, KeyError, TypeError, OSError) as e:
        # KeyError/TypeError guard Herdr JSON shape drift; OSError guards filesystem faults (e.g.
        # a result_path that is a dir, or a permission error on unlink) — all still write a blocked
        # result rather than a silent exit that strands the leader.
        existing_result = _write_blocked_result(order, str(e))
        if existing_result is None:
            fell_back = True
            sys.stderr.write(f"recruiter: order {order_id} fell back to blocked: {e}\n")
        else:
            sys.stderr.write(
                "recruiter: order "
                f"{order_id} kept existing worker result after Recruiter wait fault: {e}\n"
            )
    finally:
        if worker_pane is not None:
            try:
                _herdr("pane", "close", worker_pane)
            except (RecruiterError, OSError):
                pass  # closing a gone pane (or a fork/exec fault) must not skip the DONE emit
    # The accelerator signal the leader waits on. The RESULT FILE is the real verdict.
    final_label = "blocked" if fell_back else "done"
    _report_state(my_pane, "idle", f"last order: {order_id} ({final_label})")
    print(f"ORDER {order_id} DONE", flush=True)
    return 1 if fell_back else 0


def cmd_recruit(order_path: str, roster_path: str) -> int:
    """Submit an order and return immediately; its claimed job owns the blocking lifecycle.

    Leader-side result watchdogs are independent of this command: they watch the same
    result_path directly and can wake the leader even if this job's Herdr status wait sticks.
    """
    try:
        order = contracts.load_order(order_path)
    except contracts.ContractError:
        # Preserve the existing malformed-order fallback and terminal DONE signal.
        return _run_order(order_path, roster_path)
    ledger = JobLedger()
    key, _created = ledger.submit(order)
    # A duplicate submit starts another contender: the atomic claim admits only one while a
    # prior runner is live, and retries an earlier runner that died before claiming.
    command = [sys.executable, str(Path(__file__).resolve()), "--roster", roster_path, "run-job", key]
    try:
        # Inherit the Recruiter pane's output: the per-job owner emits its terminal marker there.
        subprocess.Popen(command, start_new_session=True)
    except OSError as e:
        _write_blocked_result(order, f"could not start job runner: {e}")
        ledger._event(key, "start-failed", reason=str(e))
        ledger._snapshot(key, "finished", verdict="blocked", reason=str(e))
        print(f"ORDER {order['order_id']} DONE", flush=True)
        return 1
    return 0


def cmd_run_job(key: str, roster_path: str) -> int:
    """Claim one persisted request, then run its existing exclusive worker lifecycle."""
    ledger = JobLedger()
    order = ledger.order(key)
    timeout_ms = int(order.get("timeout_ms") or _default_timeout_ms(order["stage_id"]))
    token = ledger.claim(key, order["order_id"], timeout_ms)
    if token is None:
        return 0
    result_code = 1
    verdict = "blocked"
    try:
        result_code = _run_order(str(ledger.request_dir(key) / "request.json"), roster_path)
        try:
            verdict = _load_normalized_result(order["result_path"], order["order_id"])["verdict"]
        except contracts.ContractError:
            verdict = "blocked"
        return result_code
    finally:
        ledger.finish(key, token, verdict, exit_code=result_code)


def _find_shared_services(workspaces_resp: dict) -> dict | None:
    """The shared-services WorkspaceInfo from a `workspace list` response, or None."""
    for w in workspaces_resp.get("result", {}).get("workspaces", []):
        if w.get("label") == SHARED_SERVICES_WORKSPACE:
            return w
    return None


RECRUITER_PANE_LABEL = "recruiter"


def _ensure_role_pane(role_label: str) -> tuple[str, str, bool]:
    """Resolve (workspace_id, pane_id, reused) for THIS engine's role pane in the single shared
    `shared-services` workspace, claiming ONLY a pane labeled `role_label` — never an arbitrary
    pane. This lets the Recruiter and the Librarian share one workspace without fighting over each
    other's panes, regardless of which engine started first:
      - create the shared-services workspace if it is absent, and label its root pane for my role;
      - if it exists, reuse my role-labeled pane if present, else split a fresh pane off an
        existing one and label it for my role.
    """
    existing = _find_shared_services(_herdr_json("workspace", "list"))
    if existing is None:
        created = _herdr_json(
            "workspace", "create", "--label", SHARED_SERVICES_WORKSPACE, "--no-focus"
        )["result"]
        workspace_id = created["workspace"]["workspace_id"]
        pane_id = created["root_pane"]["pane_id"]
        _herdr("pane", "rename", pane_id, role_label)
        return workspace_id, pane_id, False

    workspace_id = existing["workspace_id"]
    panes = _herdr_json("pane", "list", "--workspace", workspace_id).get("result", {}).get("panes", [])
    mine = next((p for p in panes if p.get("label") == role_label and p.get("pane_id")), None)
    if mine is not None:
        return workspace_id, mine["pane_id"], True
    anchor = next((p["pane_id"] for p in panes if p.get("pane_id")), None)
    if anchor is None:
        raise RecruiterError(f"shared-services workspace {workspace_id} has no pane to split from")
    new_pane = _herdr_json("pane", "split", anchor, "--direction", "down", "--no-focus")[
        "result"
    ]["pane"]["pane_id"]
    _herdr("pane", "rename", new_pane, role_label)
    return workspace_id, new_pane, True


def cmd_up(roster_path: str) -> int:
    """Ensure the shared-services workspace + an armed Recruiter pane. Idempotent.

    A single armed pane accepts concurrent orders because each accepted order runs in its own
    claimed job process; callers do not need to create a second Recruiter pane for parallel work.
    Arms a `recruit` shell function in the Recruiter pane so the phase leader can signal it with
    `herdr pane run <recruiter> "recruit <order.json>"`. The resolved roster path is baked into
    that function, so the Recruiter always hires against the intended (repo-owned) roster.
    Persists {workspace_id, recruiter_pane, roster} to STATE_FILE and prints it.
    """
    # Validate the roster up front so a missing/bad roster fails loudly at bring-up, not silently
    # at the first hire (the armed recruit() bakes this path in).
    load_roster(roster_path)
    workspace_id, recruiter_pane, reused = _ensure_role_pane(RECRUITER_PANE_LABEL)

    # Bake the resolved roster into the armed function so every hire uses the right roster.
    # shlex.quote every interpolated token so paths with spaces/metacharacters can't break arming.
    arm = (
        f"recruit() {{ {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"--roster {shlex.quote(roster_path)} recruit \"$1\"; }}"
    )
    _herdr("pane", "run", recruiter_pane, arm)

    state = {"workspace_id": workspace_id, "recruiter_pane": recruiter_pane, "roster": roster_path}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    # Surface the broker in Herdr's agents sidebar so "up" is visible, not just a shell.
    _report_state(recruiter_pane, "idle", "armed — waiting for work orders")
    print(json.dumps({**state, "reused": reused}))
    return 0


def cmd_status() -> int:
    up = _find_shared_services(_herdr_json("workspace", "list")) is not None
    print(f"shared-services: {'up' if up else 'down'}")
    if STATE_FILE.is_file():
        print(STATE_FILE.read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recruiter", description="UpAgent Recruiter")
    parser.add_argument(
        "--roster",
        default=default_roster_path(),
        help="launch-template roster (default: $UPAGENT_CONFIG, else upagent.yaml next to this file)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_recruit = sub.add_parser("recruit", help="submit a worker order without blocking the Recruiter pane")
    p_recruit.add_argument("order", help="path to order.json")
    p_run = sub.add_parser("run-job", help=argparse.SUPPRESS)
    p_run.add_argument("key", help=argparse.SUPPRESS)
    sub.add_parser("up", help="ensure the shared-services workspace")
    sub.add_parser("status", help="report shared-services state")

    args = parser.parse_args(argv)
    try:
        if args.command == "recruit":
            return cmd_recruit(args.order, args.roster)
        if args.command == "run-job":
            return cmd_run_job(args.key, args.roster)
        if args.command == "up":
            return cmd_up(args.roster)
        if args.command == "status":
            return cmd_status()
    except RecruiterError as e:
        sys.exit(f"recruiter: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
