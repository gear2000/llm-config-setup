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
  3. runs the worker, starts a bounded monitor for that lease's private staging result, then
     blocks until Herdr reports `agent-status <worker> --status done`; the monitor atomically
     promotes a valid staging result if the Herdr status signal remains stuck;
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
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, cast
import uuid

import yaml

HERE = Path(__file__).resolve().parent
_contracts_spec = importlib.util.spec_from_file_location("upagent_contracts", HERE / "contracts.py")
if _contracts_spec is None or _contracts_spec.loader is None:
    raise RuntimeError("could not load UpAgent contracts")
contracts = cast(Any, importlib.util.module_from_spec(_contracts_spec))
_contracts_spec.loader.exec_module(contracts)
ContractError = contracts.ContractError
KNOWN_HARNESSES = contracts.KNOWN_HARNESSES
RECOGNIZED_STAGE_IDS = contracts.RECOGNIZED_STAGE_IDS
load_order = contracts.load_order
load_result = contracts.load_result
parse_result = contracts.parse_result

SHARED_SERVICES_WORKSPACE = "shared-services"
DEFAULT_TIMEOUT_MS = 1_800_000  # 30 min per worker unless the order overrides
LEASE_GRACE_SECONDS = 60
COMPLETION_MONITOR_POLL_SECONDS = 0.05
STAGE_TIMEOUT_MS = {
    "stage-1-implementation": 10_800_000,
    "stage-2-adversarial-audit": 10_800_000,
}
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

    A complete request directory is atomically published, so a concurrent duplicate never reads
    a half-written request.json. Active claims are guarded by a per-key advisory file lock; the
    lease token is checked while holding that lock before either recovery or terminal cleanup.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("UPAGENT_HUB_DIR", "~/.local/state/herdr/upagent-hub")).expanduser()
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
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @contextmanager
    def _claim_lock(self, key: str) -> Iterator[None]:
        lock_path = self.active / "locks" / f"{key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _lease(path: Path) -> dict:
        try:
            lease = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(f"lease {path} is unreadable: {e}") from e
        token = lease.get("token") if isinstance(lease, dict) else None
        expires_at = lease.get("expires_at") if isinstance(lease, dict) else None
        if not isinstance(token, str) or not token or isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise RecruiterError(f"lease {path} has an invalid token or expiry")
        return lease

    def _event(self, key: str, event: str, **detail: object) -> None:
        payload = {"event": event, "at_ns": time.time_ns(), **detail}
        event_path = self.request_dir(key) / "events" / f"{payload['at_ns']}-{uuid.uuid4().hex}.json"
        self._write_json(event_path, payload)

    def _snapshot(self, key: str, state: str, **detail: object) -> None:
        payload = {"state": state, "at_ns": time.time_ns(), **detail}
        self._write_json(self.request_dir(key) / "state" / "latest.json", payload)

    def _existing_request(self, request: Path, order: dict, key: str) -> tuple[str, bool]:
        try:
            stored = json.loads((request / "request.json").read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(f"incomplete request record for {order['order_id']}: {e}") from e
        if stored != order:
            raise RecruiterError(f"order_id collision with different request: {order['order_id']}")
        return key, False

    def submit(self, order: dict) -> tuple[str, bool]:
        """Atomically persist one request. Duplicate identical order ids are idempotent."""
        key = self.key(order["order_id"])
        request = self.request_dir(key)
        self.requests.mkdir(parents=True, exist_ok=True)
        if request.exists():
            return self._existing_request(request, order, key)

        temporary = self.requests / f".{key}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir()
        self._write_json(temporary / "request.json", order)
        submitted_at = time.time_ns()
        self._write_json(
            temporary / "events" / f"{submitted_at}-{uuid.uuid4().hex}.json",
            {"event": "submitted", "at_ns": submitted_at, "order_id": order["order_id"]},
        )
        self._write_json(
            temporary / "state" / "latest.json",
            {"state": "queued", "at_ns": time.time_ns(), "order_id": order["order_id"]},
        )
        try:
            os.replace(temporary, request)
        except OSError as e:
            if e.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            shutil.rmtree(temporary)
            return self._existing_request(request, order, key)
        return key, True

    def order(self, key: str) -> dict:
        try:
            value = json.loads((self.request_dir(key) / "request.json").read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(f"job request {key} is unreadable: {e}") from e
        if not isinstance(value, dict):
            raise RecruiterError(f"job request {key} is not an object")
        return value

    def _is_finished(self, key: str) -> bool:
        try:
            snapshot = json.loads((self.request_dir(key) / "state" / "latest.json").read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(f"job state {key} is unreadable: {e}") from e
        if not isinstance(snapshot, dict):
            raise RecruiterError(f"job state {key} is not an object")
        return snapshot.get("state") == "finished"

    def completed_result(self, key: str, order: dict) -> dict | None:
        """Return a strictly valid terminal result, if this order has already finished."""
        if not self._is_finished(key):
            return None
        return load_result(order["result_path"], expected_order_id=order["order_id"])

    def _reclaim_expired_locked(
        self, key: str, now: int, expected_token: str | None = None, expected_expiry: int | None = None
    ) -> bool:
        claim_dir = self.active / "requests" / key
        if not claim_dir.is_dir():
            return False
        lease = self._lease(claim_dir / "lease.json")
        if expected_token is not None and lease["token"] != expected_token:
            return False
        if expected_expiry is not None and lease["expires_at"] != expected_expiry:
            return False
        if lease["expires_at"] > now:
            return False
        shutil.rmtree(claim_dir)
        self._event(key, "lease-expired", **lease)
        self._snapshot(key, "queued", order_id=lease["order_id"])
        return True

    def reap_expired(self, now: int | None = None) -> int:
        """Reclaim expired indexed leases only when the indexed token remains active."""
        current_time = int(time.time()) if now is None else now
        expiry_root = self.active / "by-expiry"
        if not expiry_root.is_dir():
            return 0
        reclaimed = 0
        for expiry_dir in expiry_root.iterdir():
            try:
                expiry = int(expiry_dir.name)
            except ValueError as e:
                raise RecruiterError(f"invalid lease expiry index directory: {expiry_dir}") from e
            if expiry > current_time:
                continue
            for index_path in expiry_dir.glob("*.json"):
                lease = self._lease(index_path)
                suffix = f"-{lease['token']}.json"
                if not index_path.name.endswith(suffix):
                    raise RecruiterError(f"lease index {index_path} does not match its token")
                key = index_path.name.removesuffix(suffix)
                with self._claim_lock(key):
                    if self._reclaim_expired_locked(key, current_time, lease["token"], expiry):
                        reclaimed += 1
        return reclaimed

    def claim(self, key: str, order_id: str, timeout_ms: int) -> str | None:
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            self._reclaim_expired_locked(key, int(time.time()))
            if self._is_finished(key) or claim_dir.exists():
                return None
            token = uuid.uuid4().hex
            expiry_epoch = int(time.time() + timeout_ms / 1000) + 60
            lease = {"order_id": order_id, "token": token, "expires_at": expiry_epoch}
            temporary = claim_dir.with_name(f".{key}.{token}.tmp")
            temporary.mkdir(parents=True)
            self._write_json(temporary / "lease.json", lease)
            self._write_json(self.active / "by-expiry" / str(expiry_epoch) / f"{key}-{token}.json", lease)
            try:
                os.replace(temporary, claim_dir)
            except OSError as e:
                if e.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                shutil.rmtree(temporary)
                return None
            self._event(key, "claimed", **lease)
            self._snapshot(key, "running", **lease)
            return token

    def result_staging_path(self, key: str, token: str) -> Path:
        """Return the private worker result path for one lease token.

        A worker never writes the order's public result path directly.  Its job runner promotes
        this token-scoped file only while it still owns the lease, fencing a recovered runner
        from replacing a newer runner's result.
        """
        return self.request_dir(key) / "results" / f"{token}.json"

    def finalize(self, key: str, token: str, order: dict, result: dict, **detail: object) -> bool:
        """Atomically publish a valid result and terminal state iff ``token`` still owns ``key``.

        Returns ``False`` when lease recovery has fenced this runner.  Filesystem and contract
        failures deliberately propagate: without a valid public result there is no terminal
        snapshot and no DONE signal.
        """
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                return False
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                return False
            # A lease is a fence as well as an ownership token. An expired owner cannot publish
            # a result during the gap before a replacement runner claims this order.
            if lease["expires_at"] <= int(time.time()):
                return False
            # Revalidate immediately before the durable public write.  A terminal ledger state
            # is never published without the result file that makes that state meaningful.
            parsed = parse_result(json.dumps(result), expected_order_id=order["order_id"])
            self._write_json(Path(order["result_path"]), parsed)
            load_result(order["result_path"], expected_order_id=order["order_id"])
            self._event(key, "finished", verdict=parsed["verdict"], **detail)
            self._snapshot(key, "finished", verdict=parsed["verdict"], **detail)
            shutil.rmtree(claim_dir)
            return True


# --- pure, unit-testable core ------------------------------------------------


def load_roster(path: str | Path) -> dict:
    """Read + validate the launch-template roster (upagent.yaml). Fail-loud.

    Shape:
        harnesses:
          claude: "<launch template with {placeholders}>"
          pi:     "..."
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
        if name not in KNOWN_HARNESSES:
            raise RecruiterError(f"{p} harness `{name}` is unsupported; expected one of {', '.join(KNOWN_HARNESSES)}")
        if not isinstance(tmpl, str) or not tmpl.strip():
            raise RecruiterError(f"{p} harness `{name}` must map to a non-empty template string")
    return data


def _default_timeout_ms(stage_id: str) -> int:
    """Return the stage-specific default without duplicating stage validation."""
    return STAGE_TIMEOUT_MS.get(stage_id, DEFAULT_TIMEOUT_MS)


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
            f"allowed: {', '.join(f'{{{field}}}' for field in TEMPLATE_FIELDS)}"
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


def _report_state(pane: str | None, state: str, message: str) -> None:
    """Surface the Recruiter in Herdr's agents sidebar (`pane report-agent`). BEST-EFFORT:
    status display must never break a hire, so herdr faults are swallowed. `pane` may be
    None when the caller cannot know its own pane (then this is a no-op)."""
    if not pane:
        return
    with suppress(RecruiterError, OSError):
        _herdr(
            "pane",
            "report-agent",
            pane,
            "--source",
            "upagent-recruiter",
            "--agent",
            "recruiter",
            "--state",
            state,
            "--message",
            message,
        )


def _write_blocked_result(order: dict, reason: str, result_path: str | Path | None = None) -> dict:
    """Return a valid fallback result, writing it when no valid worker result exists.

    This deliberately does not suppress filesystem failures.  Callers may emit DONE or publish
    terminal ledger state only after this result has been durably promoted by the lease owner.
    """
    path = Path(result_path or order["result_path"])
    if path.is_file():
        try:
            return load_result(path, expected_order_id=order["order_id"])
        except ContractError:
            pass
    # Only name a stage in `revisit` when it is recognized (a malformed order may not have one).
    stage = order.get("stage_id")
    result = {
        "order_id": order["order_id"],
        "verdict": "blocked",
        "revisit": [stage] if stage in RECOGNIZED_STAGE_IDS else [],
        "reason": f"recruiter: {reason}",
        "full_log": "(none — worker did not run to completion)",
    }
    JobLedger._write_json(path, result)
    return load_result(path, expected_order_id=order["order_id"])


# --- commands ----------------------------------------------------------------


def _start_completion_monitor(
    ledger: JobLedger,
    key: str,
    token: str,
    order: dict,
    worker_result_path: Path,
    worker_pane: str,
    timeout_ms: int,
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Poll one lease's staging result until it is finalized or the lease window closes.

    Herdr's agent-status signal is only an accelerator. This bounded monitor lets a valid worker
    result reach the public path even if that signal remains stuck. ``finalize`` is the sole
    terminal publisher, so its claim-lock and lease checks choose exactly one winner.
    """
    stop = threading.Event()
    finalized = threading.Event()
    deadline = time.monotonic() + timeout_ms / 1000 + LEASE_GRACE_SECONDS

    def monitor() -> None:
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                result = load_result(worker_result_path, expected_order_id=order["order_id"])
            except ContractError:
                stop.wait(COMPLETION_MONITOR_POLL_SECONDS)
                continue
            if ledger.finalize(key, token, order, result, exit_code=0, completion_source="staging-completion-monitor"):
                finalized.set()
                with suppress(RecruiterError, OSError):
                    _herdr("pane", "close", worker_pane)
                print(f"ORDER {order['order_id']} DONE", flush=True)
            return

    thread = threading.Thread(target=monitor, name=f"upagent-monitor-{key[:12]}", daemon=True)
    thread.start()
    return stop, finalized, thread


def _run_order(
    order_path: str,
    roster_path: str,
    worker_result_path: Path,
    on_worker_launched: Callable[[str], None] | None = None,
) -> tuple[int, dict]:
    """Run a worker and return its valid private result without publishing terminal state.

    ``worker_result_path`` is unique to the lease.  Only ``JobLedger.finalize`` may promote it
    to the public result path and emit the terminal state/DONE contract.
    """
    order = load_order(order_path)
    order_id = order["order_id"]
    fell_back = False
    execution_order = {**order, "result_path": str(worker_result_path)}
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
        # Each lease writes a private result, so stale recovered workers cannot touch the public
        # result path or a newer lease's staging file.
        worker_result_path.unlink(missing_ok=True)
        launch = resolve_launch_command(execution_order, roster)
        # `herdr pane split` splits an EXISTING pane; the order carries the cockpit pane to
        # split the worker from (there is no --workspace on split). This places the worker in
        # the cockpit beside the phase leader, per the topology.
        split_args = [
            "pane",
            "split",
            order["cockpit_pane"],
            "--direction",
            "right",
            "--no-focus",
            "--cwd",
            order["cwd"],
        ]
        for k, v in (order.get("env") or {}).items():
            split_args += ["--env", f"{k}={v}"]
        split = _herdr_json(*split_args)
        candidate_pane = split.get("result", {}).get("pane", {}).get("pane_id")
        if not isinstance(candidate_pane, str) or not candidate_pane:
            raise RecruiterError("herdr pane split response has no pane_id")
        worker_pane = candidate_pane

        _herdr("pane", "run", worker_pane, launch)
        if on_worker_launched is not None:
            on_worker_launched(worker_pane)
        timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
        _herdr("wait", "agent-status", worker_pane, "--status", "done", "--timeout", str(timeout_ms))

        # Invalid aliases are a worker contract failure, not a repair.
        result = load_result(worker_result_path, expected_order_id=order_id)
    except (RecruiterError, ContractError, KeyError, TypeError, OSError) as e:
        # A filesystem failure writing the fallback propagates.  Without a valid result, the
        # caller must not publish terminal state or DONE.
        try:
            load_result(worker_result_path, expected_order_id=order_id)
            existing_result = True
        except ContractError:
            existing_result = False
        result = _write_blocked_result(order, str(e), worker_result_path)
        fell_back = not existing_result
        if fell_back:
            sys.stderr.write(f"recruiter: order {order_id} fell back to blocked: {e}\n")
        else:
            sys.stderr.write(
                f"recruiter: order {order_id} kept existing worker result after Recruiter wait fault: {e}\n"
            )
    finally:
        if worker_pane is not None:
            with suppress(RecruiterError, OSError):
                _herdr("pane", "close", worker_pane)
    final_label = "blocked" if fell_back else "done"
    _report_state(my_pane, "idle", f"last order: {order_id} ({final_label})")
    return (1 if fell_back else 0), result


def cmd_recruit(order_path: str, roster_path: str) -> int:
    """Submit an order and return immediately; its claimed job owns the blocking lifecycle.

    Leader-side result watchdogs are independent of this command: they watch the same
    result_path directly and can wake the leader even if this job's Herdr status wait sticks.
    """
    try:
        order = load_order(order_path)
    except ContractError as e:
        raise RecruiterError(f"invalid order {order_path}: {e}") from e
    ledger = JobLedger()
    key, _created = ledger.submit(order)
    if ledger.completed_result(key, order) is not None:
        # A completed order is terminal and idempotent: its strict result already exists, so do
        # not open another job runner or worker pane.
        print(f"ORDER {order['order_id']} DONE", flush=True)
        return 0
    # A duplicate of a queued or live order starts another contender: the atomic claim admits
    # only one while a prior runner is live, and retries an earlier runner that died before claim.
    command = [sys.executable, str(Path(__file__).resolve()), "--roster", roster_path, "run-job", key]
    try:
        # Inherit the Recruiter pane's output: the per-job owner emits its terminal marker there.
        subprocess.Popen(command, start_new_session=True)
    except OSError as e:
        # A duplicate Popen failure is not an owner.  Claim first; only the successful claimant
        # may publish a fallback result and terminal state for this request.
        timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
        token = ledger.claim(key, order["order_id"], timeout_ms)
        if token is None:
            ledger._event(key, "start-failed-unowned", reason=str(e))
            return 1
        result = _write_blocked_result(
            order, f"could not start job runner: {e}", ledger.result_staging_path(key, token)
        )
        if not ledger.finalize(key, token, order, result, reason=str(e), exit_code=1):
            return 1
        print(f"ORDER {order['order_id']} DONE", flush=True)
        return 1
    return 0


def cmd_run_job(key: str, roster_path: str) -> int:
    """Claim one persisted request, then run its existing exclusive worker lifecycle."""
    ledger = JobLedger()
    order = ledger.order(key)
    timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
    token = ledger.claim(key, order["order_id"], timeout_ms)
    if token is None:
        return 0
    worker_result_path = ledger.result_staging_path(key, token)
    monitor: tuple[threading.Event, threading.Event, threading.Thread] | None = None

    def start_monitor(worker_pane: str) -> None:
        nonlocal monitor
        monitor = _start_completion_monitor(ledger, key, token, order, worker_result_path, worker_pane, timeout_ms)

    result_code, result = _run_order(
        str(ledger.request_dir(key) / "request.json"), roster_path, worker_result_path, start_monitor
    )
    # Recovery or the completion monitor may have finalized first. The claim lock makes a False
    # result authoritative: this runner must not emit a second DONE line.
    finalized = ledger.finalize(key, token, order, result, exit_code=result_code, completion_source="agent-status")
    if monitor is not None:
        monitor[0].set()
        if not finalized and monitor[1].is_set():
            # The monitor owns the terminal marker after winning the lease. Wait for its
            # close-before-DONE sequence so this runner cannot exit and stop it mid-cleanup.
            monitor[2].join()
        else:
            monitor[2].join(timeout=COMPLETION_MONITOR_POLL_SECONDS * 2)
    if not finalized:
        return 0 if monitor is not None and monitor[1].is_set() else 1
    print(f"ORDER {order['order_id']} DONE", flush=True)
    return result_code


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
        created = _herdr_json("workspace", "create", "--label", SHARED_SERVICES_WORKSPACE, "--no-focus")["result"]
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
    new_pane = _herdr_json("pane", "split", anchor, "--direction", "down", "--no-focus")["result"]["pane"]["pane_id"]
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
        f'--roster {shlex.quote(roster_path)} recruit "$1"; }}'
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
