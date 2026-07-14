#!/usr/bin/env python3
"""UpAgent Recruiter — the always-up broker that hires a fresh worker per work order.

The Recruiter has a status pane in the `shared-services` Herdr workspace. The phase leader
places an order by writing `order.json` and using the blocking CLI:

    just upagent-recruit <path/to/order.json>

The normal phase-leader entry point is the blocking ``dispatch`` command. It submits directly
to the durable ledger instead of injecting command text into the Recruiter's shell pane. Only
the job runner atomically claims that order, then:
  1. resolves the per-harness launch template from the roster (upagent.yaml);
  2. splits a fresh worker pane from the order's cockpit pane (into the cockpit), with the
     order's cwd (the phase worktree) and env (optional OTel vars);
  3. writes a lease-specific worker brief with one literal result path and order id, runs the
     worker, and races Herdr's event-driven agent-status wait against that private result;
  4. reads + validates the worker's result.json (must echo the order_id);
  5. closes and verifies that worker pane is absent, then atomically publishes the public
     result and a durable completion receipt.

Independent orders no longer queue behind a long-running worker in the Recruiter pane. The
filesystem job ledger supplies exclusive per-order ownership; one job runner still owns one
worker lifecycle end to end.

The RESULT FILE is the source of truth and ``receipt.json`` is the durable wake-up record.
Pane output is display-only. If anything goes wrong (Herdr error, timeout, missing/bad result,
or cleanup failure), the Recruiter publishes a fail-loud ``blocked`` result and a receipt, so
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
import signal
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
INVALID_RESULT_SETTLE_SECONDS = 0.5
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
TEMPLATE_FIELDS = ("order_id", "model", "agent", "cwd", "instructions_path", "result_path", "effort")


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
        return snapshot.get("state") in ("finished", "cleanup-failed")

    def completed_result(self, key: str, order: dict) -> dict | None:
        """Return a strictly valid terminal result, if this order has already finished."""
        if not self._is_finished(key):
            return None
        result = load_result(order["result_path"], expected_order_id=order["order_id"])
        receipt = self.completed_receipt(key, order)
        if receipt["verdict"] != result["verdict"]:
            raise RecruiterError(f"completion receipt for {order['order_id']} disagrees with result.json")
        return result

    def completed_receipt(self, key: str, order: dict) -> dict:
        """Return the durable receipt for a finished order, validating its identity."""
        path = self.request_dir(key) / "receipt.json"
        try:
            receipt = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(f"completion receipt for {order['order_id']} is unreadable: {e}") from e
        if not isinstance(receipt, dict) or receipt.get("order_id") != order["order_id"]:
            raise RecruiterError(f"completion receipt for {order['order_id']} has the wrong identity")
        if receipt.get("state") not in ("finished", "cleanup-failed") or receipt.get("result_path") != order["result_path"]:
            raise RecruiterError(f"completion receipt for {order['order_id']} is not terminal")
        cleanup = receipt.get("cleanup")
        if not isinstance(cleanup, dict) or not isinstance(cleanup.get("verified_absent"), bool):
            raise RecruiterError(f"completion receipt for {order['order_id']} has invalid cleanup state")
        return receipt

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
        if lease.get("worker_pane"):
            # Only the runtime reconciler can reclaim a lease after proving its recorded pane
            # absent. Blind filesystem reaping would orphan a live worker.
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

    def active_claims(self) -> list[tuple[str, dict]]:
        """Return validated active leases. The lease token is the ownership proof."""
        root = self.active / "requests"
        if not root.is_dir():
            return []
        claims = []
        for claim_dir in root.iterdir():
            if claim_dir.is_dir() and not claim_dir.name.startswith("."):
                claims.append((claim_dir.name, self._lease(claim_dir / "lease.json")))
        return claims

    def claim(
        self, key: str, order_id: str, timeout_ms: int, *, owner: dict[str, object] | None = None
    ) -> str | None:
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            self._reclaim_expired_locked(key, int(time.time()))
            if self._is_finished(key) or claim_dir.exists():
                return None
            token = uuid.uuid4().hex
            expiry_epoch = int(time.time() + timeout_ms / 1000) + 60
            lease = {
                "order_id": order_id,
                "token": token,
                "expires_at": expiry_epoch,
                **(owner or {}),
            }
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

    def worker_instructions_path(self, key: str, token: str) -> Path:
        return self.request_dir(key) / "workers" / f"{token}-instructions.md"

    def record_worker(self, key: str, token: str, worker_pane: str, workspace_id: str | None) -> bool:
        """Durably add the spawned worker address to the active lease iff token still owns it."""
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                return False
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                return False
            lease["worker_pane"] = worker_pane
            if workspace_id:
                lease["workspace_id"] = workspace_id
            self._write_json(claim_dir / "lease.json", lease)
            self._event(key, "worker-launched", worker_pane=worker_pane, workspace_id=workspace_id)
            self._snapshot(key, "running", **lease)
            return True

    def finalize(
        self,
        key: str,
        token: str,
        order: dict,
        result: dict,
        *,
        cleanup: dict[str, object],
        allow_expired: bool = False,
        **detail: object,
    ) -> bool:
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
            if not allow_expired and lease["expires_at"] <= int(time.time()):
                return False
            verified_absent = cleanup.get("verified_absent") is True
            # Revalidate immediately before the durable public write. A terminal ledger state
            # and receipt are never published without the result file that makes them meaningful.
            parsed = parse_result(json.dumps(result), expected_order_id=order["order_id"])
            self._write_json(Path(order["result_path"]), parsed)
            load_result(order["result_path"], expected_order_id=order["order_id"])
            terminal_state = "finished" if verified_absent else "cleanup-failed"
            if not verified_absent and parsed["verdict"] != "blocked":
                raise RecruiterError(f"cleanup-failed order {order['order_id']} must publish a blocked result")
            receipt = {
                "cleanup": cleanup,
                "order_id": order["order_id"],
                "result_path": order["result_path"],
                "state": terminal_state,
                "verdict": parsed["verdict"],
            }
            self._event(key, terminal_state, verdict=parsed["verdict"], cleanup=cleanup, **detail)
            self._write_json(self.request_dir(key) / "receipt.json", receipt)
            self._snapshot(key, terminal_state, verdict=parsed["verdict"], cleanup=cleanup, **detail)
            if verified_absent:
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


def _live_pane_ids() -> set[str]:
    response = _herdr_json("pane", "list")
    panes = response.get("result", {}).get("panes", [])
    return {pane["pane_id"] for pane in panes if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)}


def _close_worker_pane(worker_pane: str) -> dict[str, object]:
    """Close one known-owned worker and prove its pane id is no longer live.

    A failed close is recoverable only when a fresh pane listing proves the target was already
    absent. Other close/list faults propagate; terminal publication must not hide leaked panes.
    """
    close_status = "closed"
    try:
        _herdr("pane", "close", worker_pane)
    except RecruiterError:
        if worker_pane in _live_pane_ids():
            raise
        close_status = "already-absent"
    if worker_pane in _live_pane_ids():
        raise RecruiterError(f"worker pane {worker_pane} is still live after close")
    return {"status": close_status, "worker_pane": worker_pane, "verified_absent": True}


def _write_worker_instructions(order: dict, worker_result_path: Path, destination: Path) -> None:
    """Append one final, literal delivery contract to the stage brief.

    The order's public result_path belongs to the Recruiter. The worker writes only this lease's
    private staging path, which prevents a stale/recovered worker from publishing over its owner.
    """
    source = Path(order["instructions_path"])
    try:
        original = source.read_text()
    except OSError as e:
        raise RecruiterError(f"worker instructions {source} are unreadable: {e}") from e
    suffix = (
        "\n\n# Recruiter delivery contract (final and authoritative)\n\n"
        "The Recruiter, not this worker, publishes the order's public result. "
        "Ignore any earlier result destination in this brief.\n"
        f"Write exactly one result JSON file to: {worker_result_path}\n"
        f'Its `order_id` must be exactly: "{order["order_id"]}"\n'
        "Do not write a result to any other path.\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(original.rstrip() + suffix)
        os.replace(temporary, destination)
    except OSError as e:
        temporary.unlink(missing_ok=True)
        raise RecruiterError(f"could not write lease-specific worker instructions {destination}: {e}") from e


def _wait_for_agent_status(worker_pane: str, timeout_ms: int, monitor_finalized: threading.Event | None) -> bool:
    """Race one event-driven Herdr wait against the private-result monitor.

    This opens one Herdr subscription for the whole worker lifetime. When the durable file wins,
    terminate that waiter promptly; do not create a new socket subscription every second.
    """
    _herdr_available()
    deadline = time.monotonic() + timeout_ms / 1000
    args = (
        "wait",
        "agent-status",
        worker_pane,
        "--status",
        "done",
        "--timeout",
        str(timeout_ms),
    )
    process = subprocess.Popen(
        ["herdr", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise RecruiterError(f"herdr wait agent-status {worker_pane} timed out after {timeout_ms} ms")
        wait_seconds = min(COMPLETION_MONITOR_POLL_SECONDS, remaining)
        if monitor_finalized is not None and monitor_finalized.wait(wait_seconds):
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            return False
        if monitor_finalized is None:
            time.sleep(wait_seconds)
    stdout, stderr = process.communicate()
    if process.returncode == 0:
        return True
    if monitor_finalized is not None and monitor_finalized.is_set():
        return False
    raise RecruiterError(f"herdr {' '.join(args)} failed: {(stderr or stdout).strip()}")


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


def _write_blocked_result(
    order: dict,
    reason: str,
    result_path: str | Path | None = None,
    *,
    preserve_valid: bool = True,
) -> dict:
    """Return a valid fallback result, writing it when no valid worker result exists.

    This deliberately does not suppress filesystem failures.  Callers may emit DONE or publish
    terminal ledger state only after this result has been durably promoted by the lease owner.
    """
    path = Path(result_path or order["result_path"])
    if preserve_valid and path.is_file():
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
    order: dict,
    worker_result_path: Path,
    timeout_ms: int,
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Watch one lease's staging result and wake its job runner when it validates.

    Herdr's agent-status signal is only an accelerator. The monitor never publishes, closes a
    pane, or emits terminal output; the single job runner retains lifecycle ownership.
    """
    stop = threading.Event()
    finalized = threading.Event()
    deadline = time.monotonic() + timeout_ms / 1000 + LEASE_GRACE_SECONDS

    def monitor() -> None:
        invalid_signature: tuple[int, int] | None = None
        invalid_since = 0.0
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                load_result(worker_result_path, expected_order_id=order["order_id"])
            except ContractError:
                try:
                    stat = worker_result_path.stat()
                except FileNotFoundError:
                    invalid_signature = None
                    stop.wait(COMPLETION_MONITOR_POLL_SECONDS)
                    continue
                signature = (stat.st_mtime_ns, stat.st_size)
                if signature != invalid_signature:
                    invalid_signature = signature
                    invalid_since = time.monotonic()
                elif time.monotonic() - invalid_since >= INVALID_RESULT_SETTLE_SECONDS:
                    # A stable malformed file is terminal worker output, not an absence to wait
                    # on for hours. Wake the owner; its strict load produces the blocked reason.
                    finalized.set()
                    return
                stop.wait(COMPLETION_MONITOR_POLL_SECONDS)
                continue
            finalized.set()
            return

    thread = threading.Thread(target=monitor, name=f"upagent-monitor-{order['order_id'][:24]}", daemon=True)
    thread.start()
    return stop, finalized, thread


def _run_order(
    order_path: str,
    roster_path: str,
    worker_result_path: Path,
    on_worker_launched: Callable[[str, str | None], threading.Event] | None = None,
    worker_instructions_path: Path | None = None,
) -> tuple[int, dict, dict[str, object]]:
    """Run a worker and return its valid private result without publishing terminal state.

    ``worker_result_path`` is unique to the lease.  Only ``JobLedger.finalize`` may promote it
    to the public result path and emit the terminal state/DONE contract.
    """
    order = load_order(order_path)
    order_id = order["order_id"]
    fell_back = False
    execution_order = {**order, "result_path": str(worker_result_path)}
    worker_pane: str | None = None
    cleanup: dict[str, object] = {"status": "not-created", "worker_pane": None, "verified_absent": True}
    monitor_finalized: threading.Event | None = None
    # Direct dispatch runs in the phase leader's environment; never report Recruiter state onto
    # that pane. Resolve the broker's explicit persisted address instead.
    my_pane = _recruiter_pane_from_state()
    _report_state(my_pane, "working", f"hiring for {order_id}")
    try:
        # Everything that can fail lives INSIDE the fallback block, now that order_id is known, so
        # a bad roster / launch / Herdr call still writes a blocked result and emits DONE rather
        # than raising past main() and stranding the leader.
        roster = load_roster(roster_path)
        # Each lease writes a private result, so stale recovered workers cannot touch the public
        # result path or a newer lease's staging file.
        worker_result_path.parent.mkdir(parents=True, exist_ok=True)
        worker_result_path.unlink(missing_ok=True)
        effective_instructions = worker_instructions_path or worker_result_path.with_name("worker-instructions.md")
        _write_worker_instructions(order, worker_result_path, effective_instructions)
        execution_order["instructions_path"] = str(effective_instructions)
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

        pane_info = split.get("result", {}).get("pane", {})
        workspace_id = pane_info.get("workspace_id") if isinstance(pane_info, dict) else None
        if not isinstance(workspace_id, str):
            workspace_id = None

        if on_worker_launched is not None:
            monitor_finalized = on_worker_launched(worker_pane, workspace_id)
        # Ownership is durable before launch. A crash after pane creation can therefore be
        # reconciled without guessing which pane belongs to this lease.
        _herdr("pane", "run", worker_pane, launch)
        timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
        _wait_for_agent_status(worker_pane, timeout_ms, monitor_finalized)

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
            try:
                cleanup = _close_worker_pane(worker_pane)
            except RecruiterError as e:
                result = _write_blocked_result(
                    order, f"worker cleanup failed: {e}", worker_result_path, preserve_valid=False
                )
                fell_back = True
                # The failed pane address remains in the active lease for the supervisor to retry.
                cleanup = {
                    "status": "cleanup-failed",
                    "worker_pane": worker_pane,
                    "verified_absent": False,
                    "reason": str(e),
                }
    final_label = "blocked" if fell_back else "done"
    _report_state(my_pane, "idle", f"last order: {order_id} ({final_label})")
    return (1 if fell_back else 0), result, cleanup


def cmd_recruit(order_path: str, roster_path: str) -> int:
    """Submit an order and return immediately; its claimed job owns the blocking lifecycle.

    Compatibility/manual surface only. Phase leaders use ``dispatch`` so their shell call blocks
    for the durable receipt without depending on this command's pane output.
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
        cleanup = {"status": "not-created", "worker_pane": None, "verified_absent": True}
        if not ledger.finalize(key, token, order, result, cleanup=cleanup, reason=str(e), exit_code=1):
            return 1
        print(f"ORDER {order['order_id']} DONE", flush=True)
        return 1
    return 0


def _recruiter_pane_from_state() -> str | None:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pane = state.get("recruiter_pane") if isinstance(state, dict) else None
    return pane if isinstance(pane, str) and pane else None


def _process_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _runner_alive(pid: object, key: str) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    command = _process_cmdline(pid)
    return "run-job" in command and key in command and str(Path(__file__).resolve()) in command


def _terminate_owned_runner(pid: object, key: str) -> None:
    if not _runner_alive(pid, key):
        return
    assert isinstance(pid, int)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while _runner_alive(pid, key) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _runner_alive(pid, key):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _reconcile_claim(ledger: JobLedger, key: str, lease: dict, *, force: bool) -> bool:
    """Close and terminalize one dead/expired owned job. Never touches an unrecorded pane."""
    expired = lease["expires_at"] <= int(time.time())
    runner_alive = _runner_alive(lease.get("runner_pid"), key)
    if not force and not expired and runner_alive:
        return False
    if runner_alive:
        _terminate_owned_runner(lease.get("runner_pid"), key)

    worker_pane = lease.get("worker_pane")
    if isinstance(worker_pane, str) and worker_pane:
        try:
            cleanup = _close_worker_pane(worker_pane)
        except RecruiterError as e:
            cleanup = {
                "status": "cleanup-failed",
                "worker_pane": worker_pane,
                "verified_absent": False,
                "reason": str(e),
            }
    else:
        cleanup = {"status": "not-created", "worker_pane": None, "verified_absent": True}

    order = ledger.order(key)
    staging = ledger.result_staging_path(key, lease["token"])
    if cleanup["verified_absent"]:
        try:
            result = load_result(staging, expected_order_id=order["order_id"])
        except ContractError as e:
            result = _write_blocked_result(order, f"runner reconciliation: {e}", staging)
    else:
        result = _write_blocked_result(
            order,
            f"runner reconciliation could not close worker pane {worker_pane}",
            staging,
            preserve_valid=False,
        )
    finalized = ledger.finalize(
        key,
        lease["token"],
        order,
        result,
        cleanup=cleanup,
        allow_expired=True,
        exit_code=1,
        completion_source="reconciler",
    )
    if finalized:
        marker = "DONE" if cleanup["verified_absent"] else "CLEANUP_FAILED"
        print(f"ORDER {order['order_id']} {marker}", flush=True)
    return finalized


def cmd_reconcile(*, force: bool = False) -> int:
    ledger = JobLedger()
    reconciled = 0
    for key, lease in ledger.active_claims():
        if _reconcile_claim(ledger, key, lease, force=force):
            reconciled += 1
    print(json.dumps({"reconciled": reconciled, "force": force}, sort_keys=True))
    return 0


def cmd_supervise(token: str) -> int:
    """Reconcile dead/expired runners while this exact Recruiter generation remains active."""
    while True:
        try:
            state = json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(state, dict) or state.get("supervisor_token") != token:
            return 0
        cmd_reconcile(force=False)
        time.sleep(2)


def _spawn_job(key: str, roster_path: str) -> subprocess.Popen:
    command = [sys.executable, str(Path(__file__).resolve()), "--roster", roster_path, "run-job", key]
    return subprocess.Popen(command, start_new_session=True)


def cmd_dispatch(order_path: str, roster_path: str) -> int:
    """Submit one order and block until its durable terminal receipt exists.

    This is the phase leader's normal transport. It never writes command text into the shared
    Recruiter shell, so adjacent orders cannot interleave. The child process is the zero-poll
    wake-up path; an idempotent duplicate falls back to a bounded ledger wait.
    """
    try:
        order = load_order(order_path)
    except ContractError as e:
        raise RecruiterError(f"invalid order {order_path}: {e}") from e
    ledger = JobLedger()
    key, _created = ledger.submit(order)
    if ledger.completed_result(key, order) is None:
        try:
            process = _spawn_job(key, roster_path)
        except OSError as e:
            timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
            token = ledger.claim(
                key,
                order["order_id"],
                timeout_ms,
                owner={
                    "runner_pid": os.getpid(),
                    "phase_leader_pane": order["cockpit_pane"],
                    "recruiter_pane": _recruiter_pane_from_state(),
                },
            )
            if token is None:
                raise RecruiterError(f"could not start a contender for live order {order['order_id']}: {e}") from e
            result = _write_blocked_result(
                order, f"could not start job runner: {e}", ledger.result_staging_path(key, token)
            )
            cleanup = {"status": "not-created", "worker_pane": None, "verified_absent": True}
            ledger.finalize(key, token, order, result, cleanup=cleanup, reason=str(e), exit_code=1)
            process = None
        timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
        deadline = time.monotonic() + timeout_ms / 1000 + LEASE_GRACE_SECONDS
        if process is not None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                matching = next((lease for candidate, lease in ledger.active_claims() if candidate == key), None)
                if matching is None or not _reconcile_claim(ledger, key, matching, force=True):
                    raise RecruiterError(f"job runner for {order['order_id']} exceeded its lease window")
        # Normal jobs publish before exiting. A killed/crashed runner may need the standing
        # reconciler to publish a blocked receipt; this short bounded wait is anomaly-only.
        while ledger.completed_result(key, order) is None and time.monotonic() < deadline:
            time.sleep(0.1)
    result = ledger.completed_result(key, order)
    if result is None:
        raise RecruiterError(f"job runner for {order['order_id']} exited without a completion receipt")
    receipt = ledger.completed_receipt(key, order)
    print(f"ORDER_RECEIPT {json.dumps(receipt, sort_keys=True)}", flush=True)
    return 0


def cmd_run_job(key: str, roster_path: str) -> int:
    """Claim one persisted request, then run its existing exclusive worker lifecycle."""
    ledger = JobLedger()
    order = ledger.order(key)
    timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
    owner = {
        "runner_pid": os.getpid(),
        "phase_leader_pane": order["cockpit_pane"],
        "recruiter_pane": _recruiter_pane_from_state(),
    }
    token = ledger.claim(key, order["order_id"], timeout_ms, owner=owner)
    if token is None:
        return 0
    worker_result_path = ledger.result_staging_path(key, token)
    monitor: tuple[threading.Event, threading.Event, threading.Thread] | None = None

    def start_monitor(worker_pane: str, workspace_id: str | None) -> threading.Event:
        nonlocal monitor
        if not ledger.record_worker(key, token, worker_pane, workspace_id):
            raise RecruiterError(f"lease ownership changed before worker {worker_pane} was recorded")
        monitor = _start_completion_monitor(order, worker_result_path, timeout_ms)
        return monitor[1]

    result_code, result, cleanup = _run_order(
        str(ledger.request_dir(key) / "request.json"),
        roster_path,
        worker_result_path,
        start_monitor,
        ledger.worker_instructions_path(key, token),
    )
    if monitor is not None:
        monitor[0].set()
        monitor[2].join(timeout=COMPLETION_MONITOR_POLL_SECONDS * 2)
    finalized = ledger.finalize(
        key,
        token,
        order,
        result,
        cleanup=cleanup,
        exit_code=result_code,
        completion_source="result-or-agent-status",
    )
    if not finalized:
        return 1
    marker = "DONE" if cleanup["verified_absent"] else "CLEANUP_FAILED"
    print(f"ORDER {order['order_id']} {marker}", flush=True)
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

    The pane remains a visible status/compatibility surface; normal phase leaders dispatch
    directly through the CLI and durable ledger. A legacy ``recruit`` shell function remains for
    manual use, but its output is never a completion contract. The resolved roster is baked into
    that function. Persists workspace, pane, roster, and supervisor ownership to STATE_FILE.
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

    supervisor_token = uuid.uuid4().hex
    state = {
        "workspace_id": workspace_id,
        "recruiter_pane": recruiter_pane,
        "roster": roster_path,
        "supervisor_token": supervisor_token,
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    JobLedger._write_json(STATE_FILE, state)
    supervisor = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--roster", roster_path, "supervise", supervisor_token],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    state["supervisor_pid"] = supervisor.pid
    JobLedger._write_json(STATE_FILE, state)
    # Surface the broker in Herdr's agents sidebar so "up" is visible, not just a shell.
    _report_state(recruiter_pane, "idle", "armed — waiting for work orders")
    print(json.dumps({**state, "reused": reused}))
    return 0


def cmd_down() -> int:
    """Terminalize all owned jobs, close the Recruiter pane, and retire its supervisor."""
    cmd_reconcile(force=True)
    recruiter_pane = _recruiter_pane_from_state()
    if recruiter_pane:
        try:
            _herdr("pane", "close", recruiter_pane)
        except RecruiterError:
            if recruiter_pane in _live_pane_ids():
                raise
    STATE_FILE.unlink(missing_ok=True)
    print(json.dumps({"down": True, "recruiter_pane": recruiter_pane}, sort_keys=True))
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
    p_dispatch = sub.add_parser("dispatch", help="submit an order and block for its durable completion receipt")
    p_dispatch.add_argument("order", help="path to order.json")
    p_run = sub.add_parser("run-job", help=argparse.SUPPRESS)
    p_run.add_argument("key", help=argparse.SUPPRESS)
    p_supervise = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p_supervise.add_argument("token", help=argparse.SUPPRESS)
    sub.add_parser("up", help="ensure the shared-services workspace")
    sub.add_parser("down", help="stop the Recruiter and reconcile every owned worker")
    p_reconcile = sub.add_parser("reconcile", help="reconcile dead or expired owned workers")
    p_reconcile.add_argument("--all", action="store_true", help="reconcile every active worker")
    sub.add_parser("status", help="report shared-services state")

    args = parser.parse_args(argv)
    try:
        if args.command == "recruit":
            return cmd_recruit(args.order, args.roster)
        if args.command == "dispatch":
            return cmd_dispatch(args.order, args.roster)
        if args.command == "run-job":
            return cmd_run_job(args.key, args.roster)
        if args.command == "supervise":
            return cmd_supervise(args.token)
        if args.command == "up":
            return cmd_up(args.roster)
        if args.command == "down":
            return cmd_down()
        if args.command == "reconcile":
            return cmd_reconcile(force=args.all)
        if args.command == "status":
            return cmd_status()
    except RecruiterError as e:
        sys.exit(f"recruiter: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
