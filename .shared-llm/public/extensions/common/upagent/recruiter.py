#!/usr/bin/env python3
"""UpAgent Recruiter — the always-up broker that hires a fresh worker per work order.

The Recruiter has an `upagent` status pane in the unified `upagent` workspace by default
(services and every run's tabs share it), or in a dedicated `shared-services` workspace
with `up --separate-workspaces`. Any requester places an order directly through the durable
CLI lifecycle:

    just upagent-request <path/to/order.json>
    just upagent-await <path/to/order.json>

These commands cross the canonical machine-local Hub socket instead of importing a nearby
worktree copy or injecting command text into the Recruiter's shell pane. The compatibility
``dispatch`` command remains behind that transport for non-interactive callers. Only a
Hub-authorized canonical job runner atomically claims an order, then:
  1. resolves the per-harness launch template from the roster (upagent.yaml);
  2. creates and verifies a Dedicated Account Manager;
  3. atomically starts a worker beside the order's cockpit pane with its cwd and env;
  4. writes a lease-specific worker brief with one literal result path and order id, runs the
     worker, and races Herdr's event-driven agent-status wait against that private result;
  5. reads + validates the worker's result.json (must echo the order_id);
  6. closes and verifies owned panes are absent, then atomically publishes the public
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

Pure stdlib + PyYAML. No Go hub, no tmux — Herdr only.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast

import yaml

HERE = Path(__file__).resolve().parent
_contracts_spec = importlib.util.spec_from_file_location(
    "upagent_contracts", HERE / "contracts.py"
)
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

# The consult/answer contracts: a SEPARATE boundary from contracts.py. That one gates the
# leader<->worker lifecycle (`result.json`); this one gates the caller<->specialist exchange
# (`consult.json` / `answer.json`) and owns the only mechanical citation check in the repo.
_consult_spec = importlib.util.spec_from_file_location(
    "upagent_contracts_consult", HERE / "contracts_consult.py"
)
if _consult_spec is None or _consult_spec.loader is None:
    raise RuntimeError("could not load UpAgent consult contracts")
contracts_consult = cast(Any, importlib.util.module_from_spec(_consult_spec))
sys.modules[_consult_spec.name] = contracts_consult
_consult_spec.loader.exec_module(contracts_consult)
ConsultError = contracts_consult.ConsultError

_lifecycle_spec = importlib.util.spec_from_file_location(
    "upagent_lifecycle", HERE / "lifecycle.py"
)
if _lifecycle_spec is None or _lifecycle_spec.loader is None:
    raise RuntimeError("could not load UpAgent lifecycle contracts")
lifecycle = cast(Any, importlib.util.module_from_spec(_lifecycle_spec))
sys.modules[_lifecycle_spec.name] = lifecycle
_lifecycle_spec.loader.exec_module(lifecycle)
LifecycleError = lifecycle.LifecycleError

_management_spec = importlib.util.spec_from_file_location(
    "upagent_llm_management", HERE / "llm_management.py"
)
if _management_spec is None or _management_spec.loader is None:
    raise RuntimeError("could not load UpAgent LLM management contracts")
llm_management = cast(Any, importlib.util.module_from_spec(_management_spec))
sys.modules[_management_spec.name] = llm_management
_management_spec.loader.exec_module(llm_management)
ManagementConfigError = llm_management.ManagementConfigError

_offerings_spec = importlib.util.spec_from_file_location(
    "upagent_offerings", HERE / "offerings.py"
)
if _offerings_spec is None or _offerings_spec.loader is None:
    raise RuntimeError("could not load UpAgent offerings")
offering_catalog = cast(Any, importlib.util.module_from_spec(_offerings_spec))
sys.modules[_offerings_spec.name] = offering_catalog
_offerings_spec.loader.exec_module(offering_catalog)
OfferingError = offering_catalog.OfferingError

_completion_spec = importlib.util.spec_from_file_location(
    "upagent_completion", HERE / "completion.py"
)
if _completion_spec is None or _completion_spec.loader is None:
    raise RuntimeError("could not load UpAgent completion contracts")
completion = cast(Any, importlib.util.module_from_spec(_completion_spec))
sys.modules[_completion_spec.name] = completion
_completion_spec.loader.exec_module(completion)
CompletionError = completion.CompletionError

_runtime_name = "upagent_command_runtime"
if _runtime_name in sys.modules:
    command_runtime = sys.modules[_runtime_name]
else:
    _runtime_spec = importlib.util.spec_from_file_location(
        _runtime_name, HERE / "command_runtime.py"
    )
    if _runtime_spec is None or _runtime_spec.loader is None:
        raise RuntimeError("could not load UpAgent command runtime")
    command_runtime = importlib.util.module_from_spec(_runtime_spec)
    sys.modules[_runtime_name] = command_runtime
    _runtime_spec.loader.exec_module(command_runtime)

# Single-workspace default: services (and every run's tabs) share one `upagent` workspace.
# `up --separate-workspaces` restores the dedicated `shared-services` workspace. Bring-up
# migrates the two retired presentation labels in place; they are never used for new state.
UNIFIED_WORKSPACE_LABEL = "upagent"
LEGACY_UNIFIED_WORKSPACE_LABEL = "herdr"
SHARED_SERVICES_WORKSPACE = "shared-services"
UPAGENT_PANE_LABEL = "upagent"
LEGACY_RECRUITER_PANE_LABEL = "recruiter"
SERVICES_TAB_LABEL = "services"
DEFAULT_TIMEOUT_MS = 1_800_000  # 30 min per worker unless the order overrides
LEASE_GRACE_SECONDS = 60
COMPLETION_MONITOR_POLL_SECONDS = 0.05
INVALID_RESULT_SETTLE_SECONDS = 0.5
STARTUP_FAILURE_SETTLE_SECONDS = 2.0
HEALTH_PROBE_SECONDS = 0.1
EXPECTED_HARNESS_AGENT = {
    "claude": "claude",
    "codex": "codex",
    "pi": "pi",
    "cursor": "cursor",
}
EXPECTED_HARNESS_PROCESS = {
    "claude": "claude",
    "codex": "codex",
    "pi": "pi",
    "cursor": "cursor-agent",
}
STAGE_TIMEOUT_MS = {
    "stage-1-implementation": 10_800_000,
    "stage-2-adversarial-audit": 10_800_000,
}
# How long a consult's specialist worker gets. A consult is read-and-cite work, not a stage.
CONSULT_TIMEOUT_MS = 600_000
# The phase id every consult order carries. Deliberately NOT the caller's phase: the phase-tree
# receipt check walks a brief's path for `phases/<phase_id>/pass-N/`, and a consult whose brief
# sits inside a phase tree would otherwise emit a spurious `phase-receipt-degraded` per question.
CONSULT_PHASE_ID = "consult"
# Where `up` records the resolved workspace + Recruiter pane so `status`/callers can find it.
STATE_FILE = Path(
    os.environ.get("UPAGENT_STATE", "/tmp/.upagent/recruiter.json")
).expanduser()
PHASE_START_RECEIPT_ENV = "UPAGENT_PHASE_START_RECEIPT"
PHASE_START_READY_STATES = ("watchdog-ready", "ready", "ready-degraded")
WATCHDOG_AGENTS = frozenset(("phase-watchdog", "plan-lifecycle-watchdog"))
WATCHDOG_PANE_FRACTION = 0.28
SUPPORT_PANE_FRACTION = 0.20
LAYOUT_COMMAND_TIMEOUT_SECONDS = 2.0
WATCHDOG_CONTINUATION_TIMEOUT_MS = 120_000
COCKPIT_TAB_ROLES = frozenset(("workers", "oversight", "services", "control"))
COCKPIT_LAYOUT_LOCK_TIMEOUT_SECONDS = 10.0
HERDR_SOCKET_ENV = "HERDR_SOCKET_PATH"
HERDR_SESSION_ENV = "HERDR_SESSION"
HERDR_SESSION_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
HUB_INSTANCE_ENV = "UPAGENT_HUB_INSTANCE_ID"
HUB_ENGINE_ENV = "UPAGENT_HUB_ENGINE_PATH"
HUB_LOCK_FD_ENV = "UPAGENT_HUB_LOCK_FD"
HUB_PATH_ENV = "UPAGENT_HUB_PATH"
HUB_PID_ENV = "UPAGENT_HUB_PID"
HUB_SOCKET_ENV = "UPAGENT_SOCKET"
_BOUND_HUB_LEDGER_ROOT: Path | None = None
_BOUND_HUB_AUTHORITY: dict[str, object] | None = None
_BOUND_HUB_LOCK_FD: int | None = None
_REQUEST_RUNTIME_LOCKS: dict[str, threading.Lock] = {}
_REQUEST_RUNTIME_LOCKS_GUARD = threading.Lock()


@contextmanager
def _request_runtime_lock(key: str) -> Iterator[None]:
    """Serialize one request's pane creation against anytime cancellation."""
    with _REQUEST_RUNTIME_LOCKS_GUARD:
        lock = _REQUEST_RUNTIME_LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


def _bind_hub_runtime(
    ledger_root: str | Path,
    state_file: str | Path | None = None,
    identity: dict[str, object] | None = None,
    lock_fd: int | None = None,
) -> None:
    """Bind the canonical Hub runtime without process-global cwd or environment mutation."""
    global _BOUND_HUB_AUTHORITY, _BOUND_HUB_LEDGER_ROOT, _BOUND_HUB_LOCK_FD, STATE_FILE
    _BOUND_HUB_LEDGER_ROOT = Path(ledger_root).expanduser().resolve()
    if state_file is not None:
        STATE_FILE = Path(state_file).expanduser().resolve()
    _BOUND_HUB_AUTHORITY = dict(identity) if identity is not None else None
    _BOUND_HUB_LOCK_FD = lock_fd


def _canonical_engine_path() -> Path:
    """Return the Hub-published engine, rejecting a mixed-version process tree."""
    configured = os.environ.get(HUB_ENGINE_ENV)
    engine = (
        Path(configured).expanduser().resolve()
        if configured
        else (HERE / "recruiter.py").resolve()
    )
    if engine != (HERE / "recruiter.py").resolve():
        raise RecruiterError(
            f"canonical engine mismatch: Hub published {engine}, running copy is {HERE / 'recruiter.py'}"
        )
    return engine


def _require_hub_authority() -> None:
    """Prove this module is bound to the live Hub and its lifetime lock descriptor."""
    authority = _BOUND_HUB_AUTHORITY
    descriptor = _BOUND_HUB_LOCK_FD
    if authority is None or descriptor is None:
        raise RecruiterError(
            "Hub authority is unbound; direct Recruiter execution is forbidden; use the UpAgent thin client / imported just recipes"
        )
    try:
        hub_pid = int(cast(int, authority["pid"]))
        instance_id = cast(str, authority["hub_instance_id"])
        socket_value = cast(str, authority["socket_path"])
        descriptor_stat = os.fstat(descriptor)
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise RecruiterError(
            "Hub authority has an invalid process or lock descriptor"
        ) from error
    if hub_pid != os.getpid():
        raise RecruiterError(
            "Hub authority belongs to a different process; mutating subprocesses are forbidden"
        )
    socket_path = Path(socket_value).expanduser().resolve()
    lock_path = socket_path.with_name(f"{socket_path.name}.lock")
    identity_path = socket_path.with_name(f"{socket_path.name}.identity.json")
    try:
        lock_stat = lock_path.stat()
        identity = json.loads(identity_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RecruiterError(
            "Hub authority has no live published lock identity"
        ) from error
    if not isinstance(identity, dict):
        raise RecruiterError("Hub authority identity must be an object")
    if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
        lock_stat.st_dev,
        lock_stat.st_ino,
    ):
        raise RecruiterError(
            "Hub authority lock descriptor does not name the published lock"
        )
    expected = {
        "hub_instance_id": instance_id,
        "pid": hub_pid,
        "canonical_engine_path": str(_canonical_engine_path()),
        "hub_path": authority.get("hub_path"),
        "socket_path": str(socket_path),
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise RecruiterError(
            "Hub authority does not match the live published Hub identity"
        )
    try:
        # Re-locking the Hub's own open file description succeeds and leaves the lifetime lock
        # held. A forged descriptor opened by another process cannot acquire while the Hub lives.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as error:
        raise RecruiterError(
            "Hub authority does not hold the published lifetime lock"
        ) from error


def default_roster_path() -> str:
    """Resolve the launch-template roster. The filled roster is repo-owned, so prefer, in order:
      1. $UPAGENT_CONFIG (explicit override);
      2. the repo-owned `this_repo` roster, if the enclosing repo has one — walk up from cwd for
         a `.shared-llm/` dir and look under `.shared-llm/this_repo/extensions/common/upagent/`;
      3. `upagent.yaml` beside this engine (the kit's own adoption — editable in the kit source).
    `load_roster` fails loud if the resolved path does not exist, so a destination that has done
    neither (1) nor (2) gets a clear error rather than silently reading a kit-owned public file.
    """
    env = command_runtime.getenv("UPAGENT_CONFIG")
    if env:
        return env
    cwd = command_runtime.current_cwd()
    for parent in [cwd, *cwd.parents]:
        this_repo = (
            parent / ".shared-llm/this_repo/extensions/common/upagent/upagent.yaml"
        )
        if this_repo.is_file():
            return str(this_repo)
    return str(HERE / "upagent.yaml")


# Placeholders a launch template may use. The template author decides how each harness
# consumes them; the Recruiter only substitutes. A field absent from the order substitutes
# as "" — so a template flag like `--effort {effort}` needs the order to actually carry a
# value, or the harness CLI will eat the next token as the flag's value.
TEMPLATE_FIELDS = (
    "order_id",
    "model",
    "agent",
    "cwd",
    "instructions_path",
    "result_path",
    "effort",
)


class RecruiterError(RuntimeError):
    """A fail-loud Recruiter fault (bad roster, missing herdr, herdr call failed)."""


class FencedLaunchError(RecruiterError):
    """A failed launch carrying the exact journal's current cleanup evidence."""

    def __init__(self, message: str, cleanup: dict[str, object]):
        super().__init__(message)
        self.cleanup = cleanup


class AgentWaitTimeout(RecruiterError):
    """The worker reached its declared work cap without terminal evidence."""


class StartupRejectedByManager(RecruiterError):
    """A dedicated manager explicitly refused the startup; a ruling, not a launch flake."""


class JobLedger:
    """Filesystem copy-on-write job state for concurrent Recruiter requests.

    A complete request directory is atomically published, so a concurrent duplicate never reads
    a half-written request.json. Active claims are guarded by a per-key advisory file lock; the
    lease token is checked while holding that lock before either recovery or terminal cleanup.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(
            root
            or _BOUND_HUB_LEDGER_ROOT
            or os.environ.get("UPAGENT_HUB_DIR", "~/.local/state/herdr/upagent-hub")
        ).expanduser()
        self.requests = self.root / "requests"
        self.active = self.root / "active"

    @staticmethod
    def key(identity: str) -> str:
        return hashlib.sha256(identity.encode()).hexdigest()

    @classmethod
    def key_for_order(cls, order: dict) -> str:
        return cls.key(lifecycle.request_identity(order))

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
        if (
            not isinstance(token, str)
            or not token
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise RecruiterError(f"lease {path} has an invalid token or expiry")
        return lease

    def _event(self, key: str, event: str, **detail: object) -> None:
        payload = {"event": event, "at_ns": time.time_ns(), **detail}
        event_path = (
            self.request_dir(key)
            / "events"
            / f"{payload['at_ns']}-{uuid.uuid4().hex}.json"
        )
        self._write_json(event_path, payload)

    def _snapshot(self, key: str, state: str, **detail: object) -> None:
        payload = {"state": state, "at_ns": time.time_ns(), **detail}
        self._write_json(self.request_dir(key) / "state" / "latest.json", payload)

    def _existing_request(
        self, request: Path, order: dict, key: str
    ) -> tuple[str, bool]:
        try:
            stored = json.loads((request / "request.json").read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(
                f"incomplete request record for {order['order_id']}: {e}"
            ) from e
        if stored != order:
            raise RecruiterError(
                f"order_id collision with different request: {order['order_id']}"
            )
        return key, False

    def submit(self, order: dict) -> tuple[str, bool]:
        """Atomically persist one request. Duplicate identical order ids are idempotent."""
        try:
            completion.ensure_publication_contract(order)
            contracts.parse_order(json.dumps(order))
        except (CompletionError, ContractError) as error:
            raise RecruiterError(
                f"request {order.get('order_id', '<unknown>')} has no valid typed artifact contract: {error}"
            ) from error
        key = self.key_for_order(order)
        request = self.request_dir(key)
        self.requests.mkdir(parents=True, exist_ok=True)
        if request.exists():
            return self._existing_request(request, order, key)

        # Compatibility with ledgers created before request identity was scoped by result_path.
        # Reuse an identical legacy record; a different record with the same human order_id is a
        # separate request rather than a global collision.
        legacy_key = self.key(order["order_id"])
        legacy_request = self.request_dir(legacy_key)
        if legacy_request.exists():
            try:
                legacy_order = json.loads((legacy_request / "request.json").read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise RecruiterError(
                    f"legacy request record for {order['order_id']} is unreadable: {error}"
                ) from error
            if legacy_order == order:
                return legacy_key, False

        temporary = self.requests / f".{key}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir()
        self._write_json(temporary / "request.json", order)
        submitted_at = time.time_ns()
        request_id = lifecycle.request_identity(order)
        self._write_json(
            temporary / "events" / f"{submitted_at}-{uuid.uuid4().hex}.json",
            {
                "event": "submitted",
                "at_ns": submitted_at,
                "order_id": order["order_id"],
                "request_id": request_id,
            },
        )
        self._write_json(
            temporary / "state" / "latest.json",
            {
                "state": "requested",
                "at_ns": time.time_ns(),
                "generation": 1,
                "order_id": order["order_id"],
                "request_id": request_id,
            },
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

    def state(self, key: str) -> dict:
        path = self.request_dir(key) / "state" / "latest.json"
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RecruiterError(f"job state {key} is unreadable: {error}") from error
        if not isinstance(value, dict):
            raise RecruiterError(f"job state {key} is not an object")
        return value

    def _is_finished(self, key: str) -> bool:
        try:
            snapshot = json.loads(
                (self.request_dir(key) / "state" / "latest.json").read_text()
            )
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(f"job state {key} is unreadable: {e}") from e
        if not isinstance(snapshot, dict):
            raise RecruiterError(f"job state {key} is not an object")
        return snapshot.get("state") in ("finished", "cleanup-failed")

    def control_path(self, key: str) -> Path:
        return self.request_dir(key) / "control.json"

    def _write_control(self, key: str, token: str, generation: int) -> None:
        self._write_json(
            self.control_path(key),
            {
                "generation": generation,
                "requester_control_token_sha256": hashlib.sha256(
                    token.encode()
                ).hexdigest(),
                "schema_version": 1,
            },
        )

    def control_record(self, key: str) -> dict[str, object]:
        path = self.control_path(key)
        tombstone_path = self.request_dir(key) / "tombstone.json"
        try:
            value = json.loads((path if path.is_file() else tombstone_path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RecruiterError(
                f"request control record {key} is unreadable: {error}"
            ) from error
        if not isinstance(value, dict):
            raise RecruiterError(f"request control record {key} is not an object")
        control = (
            value.get("control")
            if tombstone_path.is_file() and not path.is_file()
            else value
        )
        if not isinstance(control, dict):
            raise RecruiterError(f"request control record {key} is invalid")
        digest = control.get("requester_control_token_sha256")
        generation = control.get("generation")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise RecruiterError(f"request control record {key} has invalid fields")
        return cast(dict[str, object], control)

    def verify_control_token(self, key: str, token: str) -> dict[str, object]:
        control = self.control_record(key)
        supplied = hashlib.sha256(token.encode()).hexdigest()
        if not hmac.compare_digest(
            cast(str, control["requester_control_token_sha256"]), supplied
        ):
            raise RecruiterError(
                "requester control token does not match the current request generation"
            )
        return control

    def published_result_path(self, key: str) -> Path:
        """Return the hub's own durable copy of the result it published for one order.

        The order's `result_path` belongs to the caller's run tree, which may be pruned long
        before this ledger record is retired.  This copy is what keeps a terminal record
        answerable once that happens.
        """
        return self.request_dir(key) / "published-result.json"

    def completed_result(self, key: str, order: dict) -> dict | None:
        """Return a strictly valid terminal result, if this order has already finished."""
        if not self._is_finished(key):
            return None
        receipt = self.completed_receipt(key, order)
        try:
            result = load_result(
                order["result_path"], expected_order_id=order["order_id"]
            )
        except (ContractError, OSError) as unusable:
            # Both a malformed contract and a filesystem read error (the public result vanished
            # between is_file() and read_text(), or is unreadable) are unusable-result evidence,
            # routed through the same reconcile-or-refuse path rather than crashing the loader.
            result = self._reconcile_terminal_result(key, order, receipt, unusable)
        if receipt["verdict"] != result["verdict"]:
            raise RecruiterError(
                f"completion receipt for {order['order_id']} disagrees with result.json"
            )
        return result

    def _reconcile_terminal_result(
        self, key: str, order: dict, receipt: dict, unusable: ContractError | OSError
    ) -> dict:
        """Restore a finished order's public result from the hub's own durable copy.

        A terminal record always answers with exactly one structured outcome: the recovered
        result, or a refusal naming the evidence.  `unusable` is whatever made the public result
        unreadable — a malformed contract or a filesystem read error alike.  It never crashes in
        the result loader — not on the read that detected the problem, not on the best-effort
        republication — and it never quietly reopens work the ledger has already finished.
        """
        durable = self._durable_terminal_result(key, order, receipt)
        if durable is None:
            raise RecruiterError(
                f"terminal order {order['order_id']} cannot be answered: {unusable}; "
                f"receipt {self.request_dir(key) / 'receipt.json'} records verdict "
                f"{receipt['verdict']!r} but no durable copy of that result survives"
            )
        try:
            self._write_json(Path(order["result_path"]), durable)
        except OSError as republish_error:
            # Republication is best-effort: it must not lose an already-recovered result or crash.
            # The next dispatch reconciles again idempotently from the same durable copy.
            command_runtime.write_stderr(
                f"upagent: could not republish result for {order['order_id']} to "
                f"{order['result_path']}: {republish_error}\n"
            )
            self._event(
                key,
                "result-republish-failed",
                verdict=durable["verdict"],
                result_path=order["result_path"],
                reason=str(republish_error),
            )
            return durable
        self._event(
            key,
            "result-republished",
            verdict=durable["verdict"],
            result_path=order["result_path"],
            reason=str(unusable),
        )
        # `durable` is the strictly-parsed result object just written (validated in
        # `_durable_terminal_result`); re-reading the file only reintroduces the loader crash
        # this method exists to prevent.
        return durable

    def _durable_terminal_result(
        self, key: str, order: dict, receipt: dict
    ) -> dict | None:
        """Return the hub-held result for a finished order, or None when it cannot be trusted.

        A hub copy that is malformed OR unreadable (OSError) is treated the same: untrustworthy,
        so the terminal record refuses with evidence rather than crashing in the loader.
        """
        recorded = receipt.get("published_result_path")
        if isinstance(recorded, str) and recorded:
            try:
                return load_result(recorded, expected_order_id=order["order_id"])
            except (ContractError, OSError):
                return None
        # Records finalized before the receipt named its copy: the lease-private results still
        # sit under the request directory.  Accept one only when order identity and the
        # receipt's verdict leave exactly one candidate — a retried order has several.
        surviving: list[dict] = []
        legacy_paths = sorted((self.request_dir(key) / "results").glob("*.json"))
        typed_paths = sorted(
            (self.request_dir(key) / "artifacts").glob("*/result.json")
        )
        for path in [*legacy_paths, *typed_paths]:
            try:
                candidate = load_result(path, expected_order_id=order["order_id"])
            except (ContractError, OSError):
                continue
            if (
                candidate["verdict"] == receipt["verdict"]
                and candidate not in surviving
            ):
                surviving.append(candidate)
        return surviving[0] if len(surviving) == 1 else None

    def completed_receipt(self, key: str, order: dict) -> dict:
        """Return the durable receipt for a finished order, validating its identity."""
        path = self.request_dir(key) / "receipt.json"
        try:
            receipt = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RecruiterError(
                f"completion receipt for {order['order_id']} is unreadable: {e}"
            ) from e
        if (
            not isinstance(receipt, dict)
            or receipt.get("order_id") != order["order_id"]
        ):
            raise RecruiterError(
                f"completion receipt for {order['order_id']} has the wrong identity"
            )
        if (
            receipt.get("state") not in ("finished", "cleanup-failed")
            or receipt.get("result_path") != order["result_path"]
        ):
            raise RecruiterError(
                f"completion receipt for {order['order_id']} is not terminal"
            )
        cleanup = receipt.get("cleanup")
        if not isinstance(cleanup, dict) or not isinstance(
            cleanup.get("verified_absent"), bool
        ):
            raise RecruiterError(
                f"completion receipt for {order['order_id']} has invalid cleanup state"
            )
        return receipt

    def _reclaim_expired_locked(
        self,
        key: str,
        now: int,
        expected_token: str | None = None,
        expected_expiry: int | None = None,
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
                raise RecruiterError(
                    f"invalid lease expiry index directory: {expiry_dir}"
                ) from e
            if expiry > current_time:
                continue
            for index_path in expiry_dir.glob("*.json"):
                lease = self._lease(index_path)
                suffix = f"-{lease['token']}.json"
                if not index_path.name.endswith(suffix):
                    raise RecruiterError(
                        f"lease index {index_path} does not match its token"
                    )
                key = index_path.name.removesuffix(suffix)
                with self._claim_lock(key):
                    if self._reclaim_expired_locked(
                        key, current_time, lease["token"], expiry
                    ):
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
        self,
        key: str,
        order_id: str,
        timeout_ms: int,
        *,
        owner: dict[str, object] | None = None,
    ) -> str | None:
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            self._reclaim_expired_locked(key, int(time.time()))
            if self._is_finished(key) or claim_dir.exists():
                return None
            token = uuid.uuid4().hex
            expiry_epoch = int(time.time() + timeout_ms / 1000) + 60
            requester_control_token = uuid.uuid4().hex
            lease = {
                "order_id": order_id,
                "token": token,
                "requester_control_token": requester_control_token,
                "expires_at": expiry_epoch,
                **(owner or {}),
            }
            generation = lease.get("generation", 1)
            if isinstance(generation, bool) or not isinstance(generation, int):
                raise RecruiterError("request lease generation must be an integer")
            self._write_control(key, requester_control_token, generation)
            temporary = claim_dir.with_name(f".{key}.{token}.tmp")
            temporary.mkdir(parents=True)
            self._write_json(temporary / "lease.json", lease)
            self._write_json(
                self.active / "by-expiry" / str(expiry_epoch) / f"{key}-{token}.json",
                lease,
            )
            try:
                os.replace(temporary, claim_dir)
            except OSError as e:
                if e.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                shutil.rmtree(temporary)
                return None
            manifest = completion.build_manifest(
                self.order(key),
                self.request_dir(key),
                token,
                lifecycle.request_identity(self.order(key)),
            )
            completion.write_manifest(
                self.request_dir(key) / "artifact-manifest.json", manifest
            )
            self._event(key, "claimed", **lease)
            self._snapshot(key, "claimed", **lease)
            return token

    def result_staging_path(self, key: str, token: str) -> Path:
        """Return the private worker result path for one lease token.

        A worker never writes the order's public result path directly.  Its job runner promotes
        this token-scoped file only while it still owns the lease, fencing a recovered runner
        from replacing a newer runner's result.
        """
        order = self.order(key)
        manifest = completion.build_manifest(
            order, self.request_dir(key), token, lifecycle.request_identity(order)
        )
        return manifest.artifact("result").staging_path

    def worker_instructions_path(self, key: str, token: str) -> Path:
        return self.request_dir(key) / "workers" / f"{token}-instructions.md"

    def manager_dir(self, key: str, generation: int = 1) -> Path:
        return self.request_dir(key) / "manager" / f"generation-{generation}"

    def requester_mailbox(self, key: str) -> Any:
        return lifecycle.RequestMailbox(self.request_dir(key) / "outbox")

    def publish_requester(
        self,
        key: str,
        request_id: str,
        generation: int,
        message_type: str,
        message: str,
        detail: dict | None = None,
    ) -> Path:
        path = self.requester_mailbox(key).publish(
            request_id, generation, message_type, message, detail
        )
        self._event(
            key, "requester-message", message_type=message_type, message_path=str(path)
        )
        return path

    def record_worker(
        self,
        key: str,
        token: str,
        worker_pane: str,
        workspace_id: str | None,
        worker_address: str | None = None,
    ) -> bool:
        """Durably add the spawned worker address to the active lease iff token still owns it."""
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                return False
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                return False
            lease["worker_pane"] = worker_pane
            if worker_address:
                lease["worker_address"] = worker_address
            if workspace_id:
                lease["workspace_id"] = workspace_id
            self._write_json(claim_dir / "lease.json", lease)
            self._event(
                key,
                "worker-launched",
                worker_address=worker_address,
                worker_pane=worker_pane,
                workspace_id=workspace_id,
            )
            self._snapshot(key, "startup-check", **lease)
            return True

    def mark_worker_healthy(
        self, key: str, token: str, evidence: dict[str, object]
    ) -> bool:
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                return False
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                return False
            self._write_json(self.request_dir(key) / "worker-health.json", evidence)
            self._event(key, "worker-healthy", evidence=evidence)
            self._snapshot(
                key,
                "running",
                **lease,
                worker_health_path=str(self.request_dir(key) / "worker-health.json"),
            )
            return True

    def record_manager(
        self,
        key: str,
        token: str,
        manager_pane: str,
        manager_address: str,
        workspace_id: str | None,
        generation: int,
    ) -> bool:
        """Fence the LLM manager address into the owning lease before trusting its output."""
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                return False
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                return False
            lease.update(
                {
                    "generation": generation,
                    "manager_address": manager_address,
                    "manager_pane": manager_pane,
                    "manager_workspace_id": workspace_id,
                }
            )
            self._write_json(claim_dir / "lease.json", lease)
            self._event(
                key,
                "manager-started",
                generation=generation,
                manager_address=manager_address,
                manager_pane=manager_pane,
                workspace_id=workspace_id,
            )
            self._snapshot(key, "manager-starting", **lease)
            return True

    def launch_journal_path(self, key: str, launch_id: str) -> Path:
        return self.request_dir(key) / "launches" / f"{launch_id}.json"

    def begin_launch(
        self,
        key: str,
        token: str,
        role: str,
        agent_name: str,
        herdr_session: str,
        expected_cwd: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> str:
        """Persist fenced launch intent before Herdr can create a pane."""
        launch_id = f"{role}-{uuid.uuid4().hex}"
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                raise RecruiterError("request lease disappeared before pane launch")
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                raise RecruiterError("request lease changed before pane launch")
            journal = {
                "agent_name": agent_name,
                "expected_cwd": expected_cwd,
                "expires_at": lease["expires_at"],
                "herdr_session": herdr_session,
                "key": key,
                "launch_id": launch_id,
                "lease_token": token,
                "owner_pid": os.getpid(),
                "owner_start_time": _process_start_time(os.getpid()),
                "role": role,
                "schema_version": 1,
                "state": "launching",
                **(metadata or {}),
            }
            self._write_json(self.launch_journal_path(key, launch_id), journal)
            self._event(
                key,
                "pane-launching",
                launch_id=launch_id,
                role=role,
                agent_name=agent_name,
            )
        return launch_id

    def record_launch_created(
        self,
        key: str,
        token: str,
        launch_id: str,
        pane: str,
        workspace_id: str | None,
        address: str,
    ) -> None:
        """Persist Herdr's exact returned identity before attempting the lease commit."""

        path = self.launch_journal_path(key, launch_id)
        with self._claim_lock(key):
            try:
                journal = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise RecruiterError(
                    f"launch journal {path} is unreadable: {error}"
                ) from error
            if (
                not isinstance(journal, dict)
                or journal.get("state") != "launching"
                or journal.get("lease_token") != token
            ):
                raise RecruiterError(
                    f"launch journal {launch_id} changed before create was recorded"
                )
            journal.update(
                {
                    "address": address,
                    "created_at_ns": time.time_ns(),
                    "pane": pane,
                    "state": "created",
                    "workspace_id": workspace_id,
                }
            )
            self._write_json(path, journal)
            self._event(
                key,
                "pane-created",
                launch_id=launch_id,
                pane=pane,
                agent_name=journal.get("agent_name"),
            )

    def mark_launch_started(
        self,
        key: str,
        token: str,
        launch_id: str,
        pane: str,
        workspace_id: str | None,
        address: str,
    ) -> bool:
        """CAS created -> started and publish the exact pane into the owning lease."""
        claim_dir = self.active / "requests" / key
        path = self.launch_journal_path(key, launch_id)
        with self._claim_lock(key):
            if not claim_dir.is_dir() or not path.is_file():
                return False
            lease = self._lease(claim_dir / "lease.json")
            try:
                journal = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise RecruiterError(
                    f"launch journal {path} is unreadable: {error}"
                ) from error
            if (
                not isinstance(journal, dict)
                or journal.get("state") not in ("launching", "created")
                or journal.get("lease_token") != token
                or lease["token"] != token
            ):
                return False
            journal.update(
                {
                    "address": address,
                    "pane": pane,
                    "started_at_ns": time.time_ns(),
                    "state": "started",
                    "workspace_id": workspace_id,
                }
            )
            role = journal.get("role")
            if role == "worker":
                lease.update(
                    {
                        "worker_address": address,
                        "worker_pane": pane,
                        **({"workspace_id": workspace_id} if workspace_id else {}),
                    }
                )
            elif role == "manager":
                lease.update(
                    {
                        "generation": journal.get("generation", 1),
                        "manager_address": address,
                        "manager_pane": pane,
                        "manager_workspace_id": workspace_id,
                    }
                )
            self._write_json(path, journal)
            self._write_json(claim_dir / "lease.json", lease)
            self._event(key, "pane-started", launch_id=launch_id, role=role, pane=pane)
            self._snapshot(key, "startup-check", **lease)
            return True

    def mark_launch_closed(
        self,
        key: str,
        launch_id: str,
        pane: str | None,
        cleanup: dict[str, object],
        *,
        expected_lease_token: str | None = None,
    ) -> bool:
        """Close one journal only when its exact pane and optional owner fence match."""
        path = self.launch_journal_path(key, launch_id)
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            try:
                journal = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise RecruiterError(
                    f"launch journal {path} is unreadable: {error}"
                ) from error
            if not isinstance(journal, dict):
                raise RecruiterError(f"launch journal {path} is not an object")
            if expected_lease_token is not None:
                if journal.get("lease_token") != expected_lease_token:
                    return False
                if not claim_dir.is_dir():
                    return False
                lease = self._lease(claim_dir / "lease.json")
                if lease["token"] != expected_lease_token:
                    return False
            existing_cleanup = journal.get("cleanup")
            if (
                journal.get("state") == "closed"
                and isinstance(existing_cleanup, dict)
                and existing_cleanup.get("verified_absent") is True
            ):
                return True
            recorded = journal.get("pane")
            if recorded is not None and pane is not None and recorded != pane:
                raise RecruiterError(
                    f"launch journal {launch_id} records pane {recorded}, not {pane}"
                )
            journal.update(
                {
                    "cleanup": cleanup,
                    **({"pane": pane} if pane is not None else {}),
                    "cleanup_checked_at_ns": time.time_ns(),
                    "state": "closed"
                    if cleanup.get("verified_absent") is True
                    else "cleanup-pending",
                    **(
                        {"closed_at_ns": time.time_ns()}
                        if cleanup.get("verified_absent") is True
                        else {}
                    ),
                }
            )
            self._write_json(path, journal)
            self._event(
                key,
                "pane-launch-closed"
                if cleanup.get("verified_absent") is True
                else "pane-launch-cleanup-pending",
                launch_id=launch_id,
                cleanup=cleanup,
            )
            return True

    def launch_journals(self) -> list[tuple[str, dict]]:
        journals: list[tuple[str, dict]] = []
        if not self.requests.is_dir():
            return journals
        for request in self.requests.iterdir():
            launches = request / "launches"
            if not launches.is_dir():
                continue
            for path in launches.glob("*.json"):
                try:
                    value = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError) as error:
                    raise RecruiterError(
                        f"launch journal {path} is unreadable: {error}"
                    ) from error
                if not isinstance(value, dict) or value.get("launch_id") != path.stem:
                    raise RecruiterError(f"launch journal {path} has invalid identity")
                journals.append((request.name, value))
        return journals

    def mark_manager_ready(self, key: str, token: str, decision: Any) -> bool:
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                return False
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                return False
            detail = {
                "decision": decision.decision,
                "generation": decision.generation,
                "message": decision.message,
            }
            self._event(key, "manager-ready", **detail)
            self._snapshot(key, "manager-ready", **{**lease, **detail})
            return True

    def mark_awaiting_requester(
        self, key: str, token: str, nonce: str, timeout_number: int
    ) -> dict:
        """Publish a timeout decision point while preserving the lease ownership fence."""
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                raise RecruiterError("request lease disappeared before timeout warning")
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                raise RecruiterError("request lease changed before timeout warning")
            detail = {"decision_nonce": nonce, "timeout_number": timeout_number}
            self._event(key, "timeout-warning", **detail)
            self._snapshot(key, "awaiting-requester", **lease, **detail)
            return lease

    def extend_lease(self, key: str, token: str, extension_ms: int) -> int:
        """Extend only the current generation and add a new expiry index entry."""
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                raise RecruiterError("request lease disappeared before extension")
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                raise RecruiterError("request lease changed before extension")
            expires_at = (
                max(lease["expires_at"], int(time.time()))
                + int(extension_ms / 1000)
                + 1
            )
            lease["expires_at"] = expires_at
            self._write_json(claim_dir / "lease.json", lease)
            self._write_json(
                self.active / "by-expiry" / str(expires_at) / f"{key}-{token}.json",
                lease,
            )
            self._event(
                key, "lease-extended", extension_ms=extension_ms, expires_at=expires_at
            )
            self._snapshot(key, "running", **lease)
            return expires_at

    def record_requester_decision(
        self, key: str, token: str, nonce: str, decision: Any
    ) -> Path:
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                raise RecruiterError("request is no longer active")
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                raise RecruiterError(
                    "request generation changed before the requester decision"
                )
            path = self.request_dir(key) / "responses" / f"{nonce}.json"
            if path.exists():
                raise RecruiterError("this timeout decision has already been answered")
            value = {
                "request_id": decision.request_id,
                "generation": decision.generation,
                "action": decision.action,
                "extension_ms": decision.extension_ms,
                "message": decision.message,
            }
            self._write_json(path, value)
            self._event(
                key,
                "requester-decision",
                decision_path=str(path),
                action=value["action"],
            )
            return path

    def begin_cancel(self, key: str, control_token: str) -> dict[str, object]:
        """Fence the current runner or return terminal evidence when publication won."""
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            tombstone_path = self.request_dir(key) / "tombstone.json"
            if tombstone_path.is_file():
                self.verify_control_token(key, control_token)
                tombstone = json.loads(tombstone_path.read_text())
                if not isinstance(tombstone, dict):
                    raise RecruiterError(f"request tombstone {key} is not an object")
                return {"terminal": True, "tombstone": tombstone}
            if self._is_finished(key):
                self.verify_control_token(key, control_token)
                order = self.order(key)
                return {
                    "terminal": True,
                    "receipt": self.completed_receipt(key, order),
                    "result": self.completed_result(key, order),
                }
            if not claim_dir.is_dir():
                raise RecruiterError(
                    f"request {lifecycle.request_identity(self.order(key))} is not active"
                )
            lease = self._lease(claim_dir / "lease.json")
            expected_control = lease.get("requester_control_token")
            if not isinstance(expected_control, str) or not hmac.compare_digest(
                expected_control, control_token
            ):
                raise RecruiterError(
                    "requester control token does not match the current request generation"
                )
            existing_cancellation = lease.get("cancel_requested_at_ns")
            if isinstance(existing_cancellation, int) and not isinstance(
                existing_cancellation, bool
            ):
                return {
                    "terminal": False,
                    "lease": lease,
                    "token": cast(str, lease["token"]),
                }
            cancellation_token = uuid.uuid4().hex
            lease.update(
                {
                    "cancel_requested_at_ns": time.time_ns(),
                    "expires_at": max(lease["expires_at"], int(time.time()) + 300),
                    "token": cancellation_token,
                }
            )
            self._write_json(claim_dir / "lease.json", lease)
            self._write_json(
                self.active
                / "by-expiry"
                / str(lease["expires_at"])
                / f"{key}-{cancellation_token}.json",
                lease,
            )
            self._event(key, "cancel-requested", generation=lease.get("generation", 1))
            self._snapshot(key, "cancelling", **lease)
            return {"terminal": False, "lease": lease, "token": cancellation_token}

    def _terminal_cleanup_evidence_locked(
        self, key: str, request_id: str, payload_sha256: str
    ) -> dict[str, object]:
        request = self.request_dir(key)
        tombstone_path = request / "tombstone.json"
        if tombstone_path.is_file():
            try:
                tombstone = json.loads(tombstone_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise RecruiterError(
                    f"request tombstone {tombstone_path} is unreadable: {error}"
                ) from error
            if (
                not isinstance(tombstone, dict)
                or tombstone.get("request_id") != request_id
                or tombstone.get("payload_sha256") != payload_sha256
            ):
                raise RecruiterError(
                    f"request tombstone {tombstone_path} has conflicting identity"
                )
            return {**tombstone, "already_pruned": True}
        if not request.is_dir():
            raise RecruiterError(f"request {request_id} has no Recruiter ledger record")
        if (self.active / "requests" / key).exists():
            state = self.state(key).get("state")
            raise RecruiterError(f"request {request_id} is active ({state})")
        order = self.order(key)
        if lifecycle.request_identity(order) != request_id:
            raise RecruiterError(
                f"request {request_id} has conflicting ledger identity"
            )
        public_request = order.get("public_request")
        if (
            not isinstance(public_request, dict)
            or public_request.get("payload_sha256") != payload_sha256
        ):
            raise RecruiterError(f"request {request_id} has conflicting payload hash")
        state = self.state(key)
        state_name = state.get("state")
        if state_name == "cleanup-failed":
            raise RecruiterError(f"request {request_id} is cleanup-failed")
        if state_name != "finished":
            raise RecruiterError(
                f"request {request_id} is not successfully terminal ({state_name})"
            )
        receipt = self.completed_receipt(key, order)
        cleanup = receipt.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("verified_absent") is not True:
            raise RecruiterError(
                f"request {request_id} has no verified-absent terminal cleanup"
            )
        terminal_at_ns = receipt.get(
            "terminal_at_ns", state.get("terminal_at_ns", state.get("at_ns"))
        )
        if (
            isinstance(terminal_at_ns, bool)
            or not isinstance(terminal_at_ns, int)
            or terminal_at_ns <= 0
        ):
            raise RecruiterError(
                f"request {request_id} has no valid terminal timestamp"
            )
        launches: list[dict[str, object]] = []
        launches_dir = request / "launches"
        for path in (
            sorted(launches_dir.glob("*.json")) if launches_dir.is_dir() else []
        ):
            try:
                journal = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise RecruiterError(
                    f"launch journal {path} is unreadable: {error}"
                ) from error
            journal_cleanup = (
                journal.get("cleanup") if isinstance(journal, dict) else None
            )
            if (
                not isinstance(journal, dict)
                or journal.get("launch_id") != path.stem
                or journal.get("state") != "closed"
                or not isinstance(journal_cleanup, dict)
                or journal_cleanup.get("verified_absent") is not True
            ):
                raise RecruiterError(
                    f"request {request_id} retains unresolved launch {path.name}"
                )
            launches.append(cast(dict[str, object], journal))
        result_path = self.published_result_path(key)
        try:
            result = load_result(result_path, expected_order_id=order["order_id"])
        except (ContractError, OSError) as error:
            raise RecruiterError(
                f"request {request_id} has no valid retained terminal result: {error}"
            ) from error
        return {
            "already_pruned": False,
            "control": self.control_record(key),
            "launches": launches,
            "order": order,
            "payload_sha256": payload_sha256,
            "receipt": receipt,
            "request_id": request_id,
            "result": result,
            "terminal_at_ns": terminal_at_ns,
            "terminal_state": "finished",
            "terminal_verdict": result["verdict"],
        }

    def terminal_cleanup_evidence(
        self, key: str, request_id: str, payload_sha256: str
    ) -> dict[str, object]:
        with self._claim_lock(key):
            return self._terminal_cleanup_evidence_locked(
                key, request_id, payload_sha256
            )

    def prune_terminal(
        self,
        key: str,
        request_id: str,
        payload_sha256: str,
        tombstone: dict[str, object],
        *,
        verify_absence: Callable[[dict[str, object]], None],
    ) -> dict[str, object]:
        """Commit one atomic tombstone, then prune only its Hub-owned siblings."""
        with self._claim_lock(key):
            evidence = self._terminal_cleanup_evidence_locked(
                key, request_id, payload_sha256
            )
            request = self.request_dir(key)
            already_pruned = evidence.get("already_pruned") is True
            if not already_pruned:
                verify_absence(evidence)
                if (
                    tombstone.get("request_id") != request_id
                    or tombstone.get("payload_sha256") != payload_sha256
                    or tombstone.get("terminal_verdict") != evidence["terminal_verdict"]
                ):
                    raise RecruiterError(
                        "cleanup tombstone does not match terminal evidence"
                    )
                self._write_json(request / "tombstone.json", tombstone)
            for child in request.iterdir():
                if child.name == "tombstone.json":
                    continue
                if child.is_symlink() or child.is_file():
                    child.unlink()
                else:
                    shutil.rmtree(child)
            directory_fd = os.open(request, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            garbage = self.requests / f".{key}.cleanup-old"
            if garbage.exists():
                shutil.rmtree(garbage)
            return {
                **(evidence if already_pruned else tombstone),
                "already_pruned": already_pruned,
            }

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
            parsed = parse_result(
                json.dumps(result), expected_order_id=order["order_id"]
            )
            manifest = completion.build_manifest(
                order,
                self.request_dir(key),
                token,
                lifecycle.request_identity(order),
            )
            manifest_path = self.request_dir(key) / "artifact-manifest.json"
            if not manifest_path.is_file():
                raise CompletionError(
                    f"typed completion manifest is missing: {manifest_path}"
                )
            completion.parse_manifest(manifest_path.read_text(), manifest)
            mandatory_errors = completion.mandatory_consult_errors(
                manifest, parsed, resolve_consult_claims(order, parsed)
            )
            if parsed.get("verdict") == "passed" and mandatory_errors:
                parsed = completion.write_blocked_bundle(
                    manifest,
                    "mandatory consultation gate: " + "; ".join(mandatory_errors),
                    write_result=lambda path, why: _write_blocked_result(
                        order, why, path, preserve_valid=False
                    ),
                    failure_answer=contracts_consult.failure_answer,
                )
            projected = completion.project_bundle(
                manifest,
                load_result=load_result,
                load_answer=contracts_consult.load_answer,
            )
            if projected != parsed:
                raise CompletionError(
                    "finalization result differs from the validated artifact bundle"
                )
            # The hub keeps its own copy of what it published, so a pruned run tree cannot
            # strand this record without the result its receipt vouches for.
            published = self.published_result_path(key)
            self._write_json(published, parsed)
            terminal_state = "finished" if verified_absent else "cleanup-failed"
            if not verified_absent and parsed["verdict"] != "blocked":
                raise RecruiterError(
                    f"cleanup-failed order {order['order_id']} must publish a blocked result"
                )
            terminal_at_ns = time.time_ns()
            receipt = {
                "cleanup": cleanup,
                "generation": lease.get("generation", 1),
                "order_id": order["order_id"],
                "published_result_path": str(published),
                "request_id": lease.get(
                    "request_id", lifecycle.request_identity(order)
                ),
                "result_path": order["result_path"],
                "state": terminal_state,
                "terminal_at_ns": terminal_at_ns,
                "verdict": parsed["verdict"],
                # A worker's `consults` list is its own account of its own diligence. Resolve it
                # against the Hub's record of what it actually brokered, so the Stage 2 auditor
                # reads a Python-checked fact instead of the worker's prose. Present only when
                # the worker made claims; absent means it recorded no `consults` key at all.
                **resolve_consult_claims(order, parsed),
                "artifact_manifest_path": str(manifest_path),
                "artifacts": [item.as_dict() for item in manifest.artifacts],
                **(
                    {
                        "cancelled": True,
                        "cancellation_reason": detail.get("cancellation_reason"),
                    }
                    if detail.get("cancelled") is True
                    else {}
                ),
            }
            # receipt.json is the commit marker. Durable terminal evidence is forbidden before
            # this write, so await can wake only after all public artifacts validate.
            self._write_json(self.request_dir(key) / "receipt.json", receipt)
            self._event(
                key,
                terminal_state,
                verdict=parsed["verdict"],
                cleanup=cleanup,
                terminal_at_ns=terminal_at_ns,
                **detail,
            )
            self._snapshot(
                key,
                terminal_state,
                verdict=parsed["verdict"],
                cleanup=cleanup,
                terminal_at_ns=terminal_at_ns,
                **detail,
            )
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
    if "offerings" in data:
        try:
            public_roster = offering_catalog.load_roster(p)
        except OfferingError as error:
            raise RecruiterError(
                f"{p} has invalid public offerings: {error}"
            ) from error
        # Public launch commands are always rendered by offerings.py. These sentinels keep the
        # legacy roster interface available to the lifecycle without making YAML executable.
        data["harnesses"] = dict.fromkeys(
            sorted({item.harness for item in public_roster.offerings.values()}),
            "__code_owned_offering_renderer__",
        )
        data["management"] = offering_catalog.materialize_management(public_roster)
    harnesses = data.get("harnesses")
    if not isinstance(harnesses, dict) or not harnesses:
        raise RecruiterError(f"{p} must define a non-empty `harnesses:` map")
    for name, tmpl in harnesses.items():
        if name not in KNOWN_HARNESSES:
            raise RecruiterError(
                f"{p} harness `{name}` is unsupported; expected one of {', '.join(KNOWN_HARNESSES)}"
            )
        if not isinstance(tmpl, str) or not tmpl.strip():
            raise RecruiterError(
                f"{p} harness `{name}` must map to a non-empty template string"
            )
    health = data.get("health", {})
    if not isinstance(health, dict):
        raise RecruiterError(f"{p} `health:` must be an object when present")
    for name, value in health.items():
        if name not in harnesses or not isinstance(value, dict):
            raise RecruiterError(
                f"{p} health `{name}` must match a configured harness and be an object"
            )
        for field in ("expected_agent", "expected_process"):
            if not isinstance(value.get(field), str) or not value[field]:
                raise RecruiterError(f"{p} health `{name}` needs non-empty `{field}`")
    try:
        llm_management.load_management_config(data)
    except ManagementConfigError as error:
        raise RecruiterError(
            f"{p} has invalid LLM management configuration: {error}"
        ) from error
    return data


# --- the specialist roster: WHO can be asked ----------------------------------
#
# A SECOND, deliberate load path — not an extension of `load_roster`. The two rosters answer
# different questions and therefore need opposite merge rules:
#
#   load_roster(path)          how do I LAUNCH harness X   upagent.yaml      replace
#   load_specialist_roster()   who can be ASKED about Y    specialists.yaml  merge
#
# Launch templates must replace: you never want half a launch command assembled from two files.
# Specialists must merge: a destination that overrides one specialist has to keep the eleven the
# kit ships. Forcing either rule onto the other file breaks something, and the specialist
# direction breaks silently — the phone book just gets shorter, which reads to a worker as "no
# specialist owns this area" rather than as an error.

SPECIALIST_ROSTER_FILE = "specialists.yaml"
SPECIALIST_OVERLAY_REL = (
    ".shared-llm/this_repo/extensions/common/upagent/specialists.yaml"
)


def specialist_roster_paths() -> tuple[Path | None, Path, str]:
    """Resolve (base, primary, primary_origin) specialist roster paths.

    The effective roster is base merged UNDER primary — primary entries clobber same-named base
    entries, everything else is the union:
      1. a repo-owned `this_repo` overlay — walk up from cwd for
         `.shared-llm/this_repo/extensions/common/upagent/specialists.yaml` — merged ON TOP of
         the kit base `specialists.yaml` beside this engine (kit-synced into every destination);
      2. no overlay — the kit base alone.

    There is deliberately NO single-file environment override. `$UPAGENT_CONFIG` names an
    `upagent.yaml` and is irrelevant here, and a variable that could drop the base entirely is
    the silent-loss trap above reachable by an environment.
    """
    base = HERE / SPECIALIST_ROSTER_FILE
    cwd = command_runtime.current_cwd()
    for parent in [cwd, *cwd.parents]:
        this_repo = parent / SPECIALIST_OVERLAY_REL
        if this_repo.is_file():
            return (base if base.is_file() else None), this_repo, "this-repo"
    return None, base, "kit-base"


def _resolve_specialist_path(value: object, base: Path, field: str) -> Path:
    """Resolve a configured path against `base`, rejecting non-string path values."""
    if not isinstance(value, str) or not value:
        raise RecruiterError(f"{field} must be a non-empty string (got {value!r})")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _repo_root_from_roster(path: Path) -> Path:
    """The repository containing a discovered roster, found without consulting the current
    directory. Re-derived on every load rather than frozen anywhere: a consult that inherits a
    stale root starts its specialist in a directory that may no longer exist (`fe96fba`)."""
    for candidate in (path.parent, *path.parent.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RecruiterError(
        f"could not find a repository root above roster {path}: no .git marker"
    )


def _read_specialist_file(path: Path) -> dict:
    """One roster document: a YAML object with a non-empty `specialists:` list. Fail-loud per
    file, so a broken overlay names itself rather than shrinking the merge."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise RecruiterError(f"{path} is unreadable or invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise RecruiterError(f"{path} must be a YAML object with a `specialists:` list")
    specialists = data.get("specialists")
    if not isinstance(specialists, list) or not specialists:
        raise RecruiterError(f"{path} must have a non-empty `specialists:` list")
    return data


def _named_specialists(entries: list, origin: str, path: Path) -> dict[str, dict]:
    """Ordered name -> entry copies tagged with their roster of origin. Merging is BY NAME, so
    an unnamed entry is unmergeable and fails loud here with its source file.

    A name defined twice WITHIN one file is ambiguous and fails loud too: base-under-overlay
    replacement happens BETWEEN files, so silently keeping the last same-named entry within a
    single file would hide a malformed roster rather than surface it."""
    named: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RecruiterError(
                f"every specialist entry must be an object: {entry!r} (in {path})"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise RecruiterError(
                f"every specialist needs a non-empty `name`: {entry} (in {path})"
            )
        if name in named:
            raise RecruiterError(
                f"specialist {name!r} is defined more than once in {path}; base-under-overlay "
                "replacement happens between files, never within one"
            )
        named[name] = {**entry, "origin": origin}
    return named


def _validate_specialist(entry: dict) -> None:
    """One merged entry must resolve one approved immutable offering."""
    for key in ("name", "offering", "effort", "agent"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise RecruiterError(f"every specialist needs a non-empty `{key}`: {entry}")
    try:
        offering_catalog.load_roster().resolve(entry["offering"], entry["effort"])
    except OfferingError as error:
        raise RecruiterError(
            f"specialist {entry['name']!r} has invalid offering selection: {error}"
        ) from error


def load_specialist_roster() -> dict:
    """Read + validate + MERGE the specialist rosters (kit base under the repo overlay).

    Returns the merged list plus the merge's own audit trail: which base names the overlay
    replaced, and the repository the roster describes. Fail-loud (`RecruiterError`) on any
    problem — the consult door catches it inside its recoverable block so a bad roster still
    leaves the caller a legible failure answer.
    """
    base_path, primary_path, primary_origin = specialist_roster_paths()
    if not primary_path.is_file():
        raise RecruiterError(
            f"specialist roster not found: {primary_path} "
            "(template: specialists.yml.sample, beside this engine)"
        )
    primary = _read_specialist_file(primary_path)
    base = _read_specialist_file(base_path) if base_path is not None else None

    named = _named_specialists(primary["specialists"], primary_origin, primary_path)
    overridden: list[str] = []
    if base is not None and base_path is not None:
        base_named = _named_specialists(base["specialists"], "kit-base", base_path)
        overridden = sorted(set(base_named) & set(named))
        # The overlay clobbers same-named base entries wholesale and appends its new ones; base
        # order is preserved, so an override keeps the position a worker already knows it by.
        base_named.update(named)
        named = base_named
    specialists = list(named.values())
    for entry in specialists:
        _validate_specialist(entry)

    config_path = primary_path.resolve()
    return {
        "specialists": specialists,
        "overridden": overridden,
        # Anchored on the roster this call actually resolved, never on a recorded path: a
        # consult runs in the repository its roster describes.
        "repo_root": _repo_root_from_roster(config_path),
        "config_path": config_path,
        "base_config_path": base_path.resolve() if base_path is not None else None,
    }


def _specialist_description(roster: dict, entry: dict) -> str:
    """One line: the entry's own `description`, else the persona file's frontmatter
    description, else empty. The fallback is what keeps a roster that only carries
    `location:` from rendering a phone book of bare names nobody can choose from."""
    if entry.get("description"):
        return str(entry["description"]).strip()
    location = entry.get("location")
    if not location:
        return ""
    path = _resolve_specialist_path(
        location, roster["repo_root"], "specialist location"
    )
    if not path.is_file():
        return ""
    matched = re.match(r"\A---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
    if not matched:
        return ""
    try:
        frontmatter = yaml.safe_load(matched.group(1))
    except yaml.YAMLError:
        return ""
    description = (
        (frontmatter or {}).get("description", "")
        if isinstance(frontmatter, dict)
        else ""
    )
    return str(description).strip().split("\n")[0]


def _specialist_index(roster: dict) -> dict[str, dict]:
    """The merged roster keyed by name, with descriptions resolved. In memory only — it is
    cheap to compute, and a persisted copy is a cache with a staleness problem and no owner."""
    offering_roster = offering_catalog.load_roster()
    return {
        entry["name"]: {
            "name": entry["name"],
            "description": _specialist_description(roster, entry),
            "location": entry.get("location", ""),
            "agent": entry["agent"],
            "effort": entry["effort"],
            "offering": entry["offering"],
            "offering_snapshot": offering_roster.resolve(
                entry["offering"], entry["effort"]
            ),
            "origin": entry.get("origin", ""),
        }
        for entry in roster["specialists"]
    }


def _default_timeout_ms(stage_id: str) -> int:
    """Return the stage-specific default without duplicating stage validation."""
    return STAGE_TIMEOUT_MS.get(stage_id, DEFAULT_TIMEOUT_MS)


def _phase_start_receipt(order: dict) -> Path | None:
    """Return the required startup receipt for a conventional phase-tree order, if any."""
    instructions = Path(order["instructions_path"]).expanduser().resolve()
    for parent in (instructions.parent, *instructions.parents):
        if not parent.name.startswith("pass-"):
            continue
        phase_dir = parent.parent
        if phase_dir.parent.name != "phases" or phase_dir.name != order["phase_id"]:
            continue
        return parent / "control" / "phase-start.json"
    return None


def phase_receipt_warning(order: dict) -> str | None:
    """Describe degraded phase coordination without blocking useful stage work.

    The check is advisory. Non-phase orders (and the legacy watchdog bootstrap order) need no
    receipt. Conventional phase orders are still inspected so the warning can be persisted and
    returned to their requester, but a missing or stale receipt never becomes a work-stopping
    gate: the order runs either way.
    """
    if order.get("agent") == "phase-watchdog":
        return None
    receipt_path = _phase_start_receipt(order)
    if receipt_path is None:
        return None
    if not receipt_path.is_file():
        return (
            f"phase order {order['order_id']} has no phase-start receipt; missing "
            f"{receipt_path}. The phase kickoff (just upagent-phase-start) never ran for "
            "this pass, so phase coordination events are not active for this order. "
            "Work continues"
        )
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return f"phase-start receipt {receipt_path} is unreadable ({error}); continuing degraded"
    if not isinstance(receipt, dict):
        return (
            f"phase-start receipt {receipt_path} is not an object; continuing degraded"
        )
    if receipt.get("state") not in PHASE_START_READY_STATES:
        return f"phase-start receipt {receipt_path} is not ready; continuing degraded"
    if receipt.get("phase_id") != order["phase_id"]:
        return f"phase-start receipt {receipt_path} belongs to another phase; continuing degraded"
    if receipt.get("leader_pane") != order["cockpit_pane"]:
        return (
            f"phase-start receipt {receipt_path} belongs to leader {receipt.get('leader_pane')}; "
            f"order requester is {order['cockpit_pane']}. Continuing degraded"
        )
    watchdog = receipt.get("watchdog")
    if isinstance(watchdog, dict) and watchdog.get("state") == "not-configured":
        return None
    if not isinstance(watchdog, dict) or not isinstance(
        watchdog.get("worker_pane"), str
    ):
        reason = watchdog.get("reason") if isinstance(watchdog, dict) else None
        detail = f": {reason}" if isinstance(reason, str) and reason else ""
        return f"phase watchdog is unavailable{detail}; continuing degraded"
    return None


def _record_phase_receipt_warning(
    ledger: JobLedger, key: str, order: dict
) -> tuple[str | None, bool]:
    """Persist any degraded-coordination warning and decide whether to announce it.

    The warning lands in every affected order's ledger so receipts stay complete, but a missing
    phase-start receipt is announced once per phase pass: the first announcer atomically claims
    a marker beside where the receipt belongs, and later orders in the same pass stay quiet.
    Other degraded reasons are rare per-order conditions and always announce.
    """
    warning = phase_receipt_warning(order)
    if warning is None:
        return None, False
    ledger._event(key, "phase-receipt-degraded", warning=warning)
    announce = True
    receipt_path = _phase_start_receipt(order)
    if receipt_path is not None and not receipt_path.is_file():
        announce = _claim_pass_warning_marker(receipt_path.parent, order["order_id"])
    return warning, announce


def _claim_pass_warning_marker(control_dir: Path, order_id: str) -> bool:
    """Atomically claim the one missing-receipt announcement allowed per phase pass.

    Returns True for the first claimant. When the marker cannot be persisted at all, warn
    anyway; a repeated warning beats a silently lost one.
    """
    marker = control_dir / "phase-start-missing.warned.json"
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Includes a plain file squatting on the control-dir path: no marker home, warn anyway.
        return True
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {"first_order_id": order_id, "announced_at_ns": time.time_ns()}, handle
            )
    except OSError:
        pass
    return True


def _reject_legacy_order(order_path: str, reason: str) -> None:
    """Best-effort terminal response for the deprecated pane-driven recruit command.

    A well-formed JSON object can still carry its correlation id and result destination even when
    the complete order contract is invalid. Persist BLOCKED before emitting DONE so old waiters
    wake up and propagate the infrastructure failure instead of waiting forever.
    """
    path = Path(order_path)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        print(
            f"ORDER_REJECTED {json.dumps({'order_path': order_path, 'reason': reason}, sort_keys=True)}",
            flush=True,
        )
        return
    if not isinstance(value, dict):
        print(
            f"ORDER_REJECTED {json.dumps({'order_path': order_path, 'reason': reason}, sort_keys=True)}",
            flush=True,
        )
        return
    order_id = value.get("order_id")
    result_path = value.get("result_path")
    if (
        not isinstance(order_id, str)
        or not order_id
        or not isinstance(result_path, str)
        or not result_path
    ):
        print(
            f"ORDER_REJECTED {json.dumps({'order_path': order_path, 'reason': reason}, sort_keys=True)}",
            flush=True,
        )
        return
    _write_blocked_result(value, reason, result_path, preserve_valid=False)
    print(f"ORDER {order_id} DONE", flush=True)


def resolve_launch_command(order: dict, roster: dict) -> str:
    """Resolve a code-owned public offering or a legacy controller launch template."""
    snapshot = order.get("offering_snapshot")
    if snapshot is not None:
        try:
            selected = offering_catalog.validate_snapshot(snapshot)
            if (
                order.get("harness") != selected["harness"]
                or order.get("model") != selected["model"]
                or order.get("effort") != selected["selected_effort"]
            ):
                raise RecruiterError(
                    "order harness/model/effort does not match its immutable offering snapshot"
                )
            return offering_catalog.render_shell(
                selected, order["agent"], order["instructions_path"]
            )
        except OfferingError as error:
            raise RecruiterError(f"invalid offering snapshot: {error}") from error
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


def inspect_worker_configuration(order: dict, roster: dict) -> dict[str, object]:
    """Return deterministic pre-launch facts for the manager; never guess a repair."""
    errors: list[str] = []
    cwd = Path(order["cwd"]).expanduser()
    instructions = Path(order["instructions_path"]).expanduser()
    result = Path(order["result_path"]).expanduser()
    if not cwd.is_absolute() or not cwd.is_dir():
        errors.append(f"cwd is not an existing absolute directory: {cwd}")
    if not instructions.is_absolute() or not instructions.is_file():
        errors.append(
            f"instructions_path is not an existing absolute file: {instructions}"
        )
    if not result.is_absolute():
        errors.append(f"result_path is not absolute: {result}")
    template = roster.get("harnesses", {}).get(order["harness"], "")
    if "{model}" in template and not order.get("model"):
        errors.append("launch template requires a non-empty model")
    if "{effort}" in template and not order.get("effort"):
        errors.append("launch template requires a non-empty effort")
    model = order.get("model", "")
    if order["harness"] == "pi" and "/" not in model:
        errors.append(
            "Pi model must use provider/id form; effort is a separate --thinking token"
        )
    if order["harness"] in ("claude", "codex") and "/" in model:
        errors.append(
            f"{order['harness']} model must use a harness-native id without provider/"
        )
    launch: str | None = None
    try:
        launch = resolve_launch_command(order, roster)
        words = shlex.split(launch)
    except (RecruiterError, ValueError) as error:
        errors.append(f"launch command is invalid: {error}")
        words = []
    binary = words[0] if words else None
    if binary and shutil.which(binary) is None:
        errors.append(f"launch executable is not on PATH: {binary}")
    agent_candidates: list[str] = []
    if order["harness"] == "claude" and "--agent {agent}" in template:
        agent_file = f"{order['agent']}.md"
        roots = [cwd / ".claude/agents", Path.home() / ".claude/agents"]
        agent_candidates = [str(root / agent_file) for root in roots]
        if not any(Path(candidate).is_file() for candidate in agent_candidates):
            errors.append(
                f"Claude agent {order['agent']!r} was not found at: {', '.join(agent_candidates)}"
            )
    return {
        "agent_candidates": agent_candidates,
        "binary": binary,
        "errors": errors,
        "launch_resolved": launch is not None,
        "valid": not errors,
    }


# --- herdr runtime helpers ---------------------------------------------------


def _herdr_available() -> None:
    if shutil.which("herdr") is None:
        raise RecruiterError(
            "`herdr` not found in PATH — the Recruiter runs inside Herdr"
        )


def _validate_herdr_session_name(value: object, field: str = "herdr session") -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecruiterError(f"{field} must be a non-empty string")
    if value.strip() != value or HERDR_SESSION_NAME_RE.fullmatch(value) is None:
        raise RecruiterError(f"{field} contains unsupported characters: {value!r}")
    return value


def _herdr_command_display(session: str, args: Sequence[str]) -> str:
    return " ".join(["herdr", "--session", session, *args])


def _run_raw_herdr(
    args: Sequence[str], *, timeout_seconds: float | None = None
) -> subprocess.CompletedProcess[str]:
    _herdr_available()
    try:
        return subprocess.run(
            ["herdr", *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise RecruiterError(
            f"herdr {' '.join(args)} timed out after {timeout_seconds} seconds"
        ) from error
    except OSError as error:
        raise RecruiterError(
            f"herdr {' '.join(args)} could not run: {error}"
        ) from error


def _herdr_session_list() -> dict:
    process = _run_raw_herdr(("session", "list", "--json"), timeout_seconds=15)
    if process.returncode != 0:
        raise RecruiterError(
            f"herdr session list --json failed: {process.stderr.strip()}"
        )
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RecruiterError(
            f"herdr session list --json did not print JSON: {process.stdout[:200]}"
        ) from error
    if not isinstance(value, dict):
        raise RecruiterError("herdr session list --json must return an object")
    return value


def _active_herdr_socket_from_status(session_hint: str | None) -> str:
    args: list[str] = []
    if session_hint is not None:
        args.extend(
            ("--session", _validate_herdr_session_name(session_hint, HERDR_SESSION_ENV))
        )
    args.extend(("status", "--json"))
    process = _run_raw_herdr(args, timeout_seconds=15)
    if process.returncode != 0:
        raise RecruiterError(f"herdr {' '.join(args)} failed: {process.stderr.strip()}")
    try:
        status = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RecruiterError(
            f"herdr {' '.join(args)} did not print JSON: {process.stdout[:200]}"
        ) from error
    server = status.get("server") if isinstance(status, dict) else None
    if not isinstance(server, dict) or server.get("running") is not True:
        raise RecruiterError("could not resolve Herdr session: server is not running")
    socket = server.get("socket") or server.get("socket_path")
    if not isinstance(socket, str) or not socket:
        raise RecruiterError(
            "could not resolve Herdr session: status returned no socket"
        )
    return socket


def _herdr_session_name_for_socket(socket_path: str, session_hint: str | None) -> str:
    if not socket_path:
        raise RecruiterError("could not resolve Herdr session: socket path is empty")
    sessions = _herdr_session_list().get("sessions")
    if not isinstance(sessions, list):
        raise RecruiterError("herdr session list --json returned no sessions list")
    matches = [
        session
        for session in sessions
        if isinstance(session, dict)
        and session.get("running") is True
        and session.get("socket_path") == socket_path
    ]
    if len(matches) != 1:
        raise RecruiterError(
            f"could not resolve Herdr session for socket {socket_path!r}: "
            f"expected exactly one running match, found {len(matches)}"
        )
    name = _validate_herdr_session_name(matches[0].get("name"))
    if session_hint is not None and name != session_hint:
        raise RecruiterError(
            f"resolved Herdr socket belongs to session {name!r}, not {session_hint!r}"
        )
    return name


def _resolve_current_herdr_session_name() -> str:
    """Resolve the active Herdr session by socket identity, never by default fallback."""
    raw_hint = command_runtime.getenv(HERDR_SESSION_ENV)
    session_hint = (
        _validate_herdr_session_name(raw_hint, HERDR_SESSION_ENV)
        if isinstance(raw_hint, str) and raw_hint
        else None
    )
    socket_path = command_runtime.getenv(HERDR_SOCKET_ENV)
    if not isinstance(socket_path, str) or not socket_path:
        socket_path = _active_herdr_socket_from_status(session_hint)
    return _herdr_session_name_for_socket(socket_path, session_hint)


def _herdr_owner_record() -> dict[str, object]:
    return {"herdr_session": _resolve_current_herdr_session_name()}


def _recorded_herdr_session(value: object, operation: str) -> str:
    try:
        return _validate_herdr_session_name(value, "recorded Herdr session")
    except RecruiterError as error:
        raise RecruiterError(
            f"{operation} requires an explicit recorded Herdr session"
        ) from error


def _herdr_argv(
    args: Sequence[str], herdr_session: str | None = None
) -> tuple[str, list[str]]:
    session = (
        _resolve_current_herdr_session_name()
        if herdr_session is None
        else _validate_herdr_session_name(herdr_session, "Herdr session")
    )
    return session, ["herdr", "--session", session, *args]


def _herdr_json(
    *args: str,
    timeout_seconds: float | None = None,
    herdr_session: str | None = None,
) -> dict:
    """Run a herdr subcommand expected to print JSON; return the parsed object. Fail-loud."""
    _herdr_available()
    session, argv = _herdr_argv(args, herdr_session)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise RecruiterError(
            f"{_herdr_command_display(session, args)} timed out after {timeout_seconds} seconds"
        ) from error
    except OSError as error:
        raise RecruiterError(
            f"{_herdr_command_display(session, args)} could not run: {error}"
        ) from error
    if proc.returncode != 0:
        raise RecruiterError(
            f"{_herdr_command_display(session, args)} failed: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RecruiterError(
            f"{_herdr_command_display(session, args)} did not print JSON: {proc.stdout[:200]}"
        ) from e


def _herdr(*args: str, herdr_session: str | None = None) -> None:
    """Run a herdr subcommand that prints nothing on success. Fail-loud on non-zero."""
    _herdr_available()
    session, argv = _herdr_argv(args, herdr_session)
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecruiterError(
            f"{_herdr_command_display(session, args)} failed: {proc.stderr.strip()}"
        )


def _pane_recent_output(
    pane: str, lines: int = 80, *, herdr_session: str | None = None
) -> str:
    _herdr_available()
    session, argv = _herdr_argv(
        (
            "pane",
            "read",
            pane,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(lines),
        ),
        herdr_session,
    )
    process = subprocess.run(
        argv,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RecruiterError(
            f"{_herdr_command_display(session, ('pane', 'read', pane))} failed: {process.stderr.strip()}"
        )
    return process.stdout


def _safe_agent_name(prefix: str, request_id: str, generation: int) -> str:
    compact = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in request_id
    )
    return f"{prefix}-{compact[:40]}-g{generation}"


@contextmanager
def _exclusive_workspace_layout(workspace_id: str) -> Iterator[None]:
    key = hashlib.sha256(workspace_id.encode()).hexdigest()
    lock_path = JobLedger().root / "layout-locks" / f"{key}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+", encoding="utf-8")
    except OSError as error:
        raise RecruiterError(
            f"could not open cockpit layout lock for workspace {workspace_id}: {error}"
        ) from error
    with stream:
        deadline = time.monotonic() + COCKPIT_LAYOUT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise RecruiterError(
                        f"cockpit layout lock for workspace {workspace_id} timed out"
                    ) from error
                time.sleep(HEALTH_PROBE_SECONDS)
        try:
            yield
        finally:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError as error:
                raise RecruiterError(
                    f"could not release cockpit layout lock for workspace {workspace_id}: {error}"
                ) from error


def _place_started_agent_in_role_tab(
    pane_id: str,
    workspace_id: str,
    tab_role: str,
    *,
    split_direction: str,
    herdr_session: str | None = None,
) -> str:
    """Move a live agent into one uniquely labeled role tab before publishing its address."""
    if tab_role not in COCKPIT_TAB_ROLES:
        raise RecruiterError(
            f"cockpit tab role must be one of {', '.join(sorted(COCKPIT_TAB_ROLES))}"
        )
    with _exclusive_workspace_layout(workspace_id):
        tabs = (
            _herdr_json(
                "tab",
                "list",
                "--workspace",
                workspace_id,
                timeout_seconds=LAYOUT_COMMAND_TIMEOUT_SECONDS,
                herdr_session=herdr_session,
            )
            .get("result", {})
            .get("tabs", [])
        )
        matches = [
            tab
            for tab in tabs
            if isinstance(tab, dict) and tab.get("label") == tab_role
        ]
        if len(matches) > 1:
            raise RecruiterError(
                f"workspace {workspace_id} has multiple tabs labeled {tab_role!r}"
            )
        if matches:
            tab_id = matches[0].get("tab_id")
            if not isinstance(tab_id, str) or not tab_id:
                raise RecruiterError(
                    f"workspace {workspace_id} {tab_role!r} tab has no tab_id"
                )
            panes = (
                _herdr_json(
                    "pane",
                    "list",
                    "--workspace",
                    workspace_id,
                    timeout_seconds=LAYOUT_COMMAND_TIMEOUT_SECONDS,
                    herdr_session=herdr_session,
                )
                .get("result", {})
                .get("panes", [])
            )
            current = next(
                (
                    pane
                    for pane in panes
                    if isinstance(pane, dict) and pane.get("pane_id") == pane_id
                ),
                None,
            )
            if isinstance(current, dict) and current.get("tab_id") == tab_id:
                return pane_id
            target = next(
                (
                    pane.get("pane_id")
                    for pane in panes
                    if isinstance(pane, dict)
                    and pane.get("tab_id") == tab_id
                    and isinstance(pane.get("pane_id"), str)
                ),
                None,
            )
            if not isinstance(target, str) or not target:
                raise RecruiterError(
                    f"workspace {workspace_id} {tab_role!r} tab has no target pane"
                )
            response = _herdr_json(
                "pane",
                "move",
                pane_id,
                "--tab",
                tab_id,
                "--split",
                split_direction,
                "--target-pane",
                target,
                "--no-focus",
                timeout_seconds=LAYOUT_COMMAND_TIMEOUT_SECONDS,
                herdr_session=herdr_session,
            )
        else:
            response = _herdr_json(
                "pane",
                "move",
                pane_id,
                "--new-tab",
                "--workspace",
                workspace_id,
                "--label",
                tab_role,
                "--no-focus",
                timeout_seconds=LAYOUT_COMMAND_TIMEOUT_SECONDS,
                herdr_session=herdr_session,
            )
        move = response.get("result", {}).get("move_result", {})
        moved_pane = move.get("pane", {}) if isinstance(move, dict) else {}
        moved_id = moved_pane.get("pane_id") if isinstance(moved_pane, dict) else None
        moved_workspace = (
            moved_pane.get("workspace_id") if isinstance(moved_pane, dict) else None
        )
        if (
            not isinstance(move, dict)
            or move.get("changed") is not True
            or not isinstance(moved_id, str)
            or not moved_id
            or moved_workspace != workspace_id
        ):
            raise RecruiterError(
                f"Herdr did not place agent pane {pane_id} in {tab_role!r} tab"
            )
        return moved_id


def _start_herdr_agent(
    name: str,
    order: dict,
    launch: str,
    *,
    split_direction: str = "right",
    tab_role: str | None = None,
    herdr_session: str | None = None,
) -> tuple[str, str | None, str]:
    """Atomically create a named Herdr pane and start its process.

    ``herdr agent start`` carries argv through the socket in one request; unlike ``pane run`` it
    cannot interleave launch keystrokes with another command in a shared shell. Role-specific
    directions form a balanced grid: workers default right; managers and checkers request down.
    """
    if split_direction not in ("right", "down"):
        raise RecruiterError(
            f"agent split direction must be right or down, got {split_direction!r}"
        )
    session = _recorded_herdr_session(herdr_session, "agent startup")
    cockpit = (
        _herdr_json("pane", "get", order["cockpit_pane"], herdr_session=session)
        .get("result", {})
        .get("pane", {})
    )
    tab_id = cockpit.get("tab_id") if isinstance(cockpit, dict) else None
    if not isinstance(tab_id, str) or not tab_id:
        raise RecruiterError(f"cockpit pane {order['cockpit_pane']} has no tab_id")
    args = [
        "agent",
        "start",
        name,
        "--cwd",
        order["cwd"],
        "--tab",
        tab_id,
        "--split",
        split_direction,
        "--no-focus",
    ]
    for key, value in (order.get("env") or {}).items():
        args.extend(("--env", f"{key}={value}"))
    args.extend(("--", "bash", "-lc", launch))
    response = _herdr_json(*args, herdr_session=session)
    agent = response.get("result", {}).get("agent", {})
    pane_id = agent.get("pane_id") if isinstance(agent, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        raise RecruiterError("herdr agent start response has no pane_id")
    workspace_id = agent.get("workspace_id") if isinstance(agent, dict) else None
    if not isinstance(workspace_id, str):
        workspace_id = None
    address = agent.get("name") if isinstance(agent, dict) else None
    if not isinstance(address, str) or not address:
        address = name
    if tab_role is not None:
        if workspace_id is None:
            _layout_warning(
                tab_role,
                pane_id,
                f"agent {name!r} has no workspace_id for tab placement",
            )
        else:
            try:
                pane_id = _place_started_agent_in_role_tab(
                    pane_id,
                    workspace_id,
                    tab_role,
                    split_direction=split_direction,
                    herdr_session=session,
                )
            except RecruiterError as error:
                _layout_warning(tab_role, pane_id, str(error))
    return pane_id, workspace_id, address


def _reconcile_exact_launch(
    ledger: JobLedger,
    key: str,
    launch_id: str,
    *,
    known_pane: str | None,
    herdr_session: str,
    allow_not_found_absent: bool,
    attempts: int = 3,
    trusted_created_identity: bool = False,
) -> dict[str, object]:
    """Boundedly resolve one journaled agent and prove its exact pane absent."""

    path = ledger.launch_journal_path(key, launch_id)
    errors: list[str] = []
    pane = known_pane
    agent_name: str | None = None
    for attempt in range(1, attempts + 1):
        agent_verified = trusted_created_identity and pane is not None
        try:
            journal = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RecruiterError(
                f"launch journal {path} is unreadable: {error}"
            ) from error
        if not isinstance(journal, dict) or journal.get("launch_id") != launch_id:
            raise RecruiterError(f"launch journal {path} has invalid identity")
        journal_agent = journal.get("agent_name")
        if not isinstance(journal_agent, str) or not journal_agent:
            raise RecruiterError(f"launch journal {launch_id} has no exact agent name")
        agent_name = journal_agent
        recorded_pane = journal.get("pane")
        if isinstance(recorded_pane, str) and recorded_pane:
            if pane is not None and pane != recorded_pane:
                raise RecruiterError(
                    f"launch {launch_id} returned pane {pane}, journal records {recorded_pane}"
                )
            pane = recorded_pane
        not_found = False
        try:
            agent = (
                _herdr_json("agent", "get", agent_name, herdr_session=herdr_session)
                .get("result", {})
                .get("agent")
            )
        except RecruiterError as error:
            not_found = "agent_not_found" in str(error)
            if not not_found:
                errors.append(str(error))
            agent = None
        if isinstance(agent, dict) and agent:
            if agent.get("name") != agent_name:
                raise RecruiterError(
                    f"launch {launch_id} resolved an agent with the wrong identity"
                )
            resolved_pane = agent.get("pane_id")
            if not isinstance(resolved_pane, str) or not resolved_pane:
                raise RecruiterError(f"launch {launch_id} resolved no exact pane")
            if pane is not None and pane != resolved_pane:
                raise RecruiterError(
                    f"launch {launch_id} records pane {pane}, resolved {resolved_pane}"
                )
            pane = resolved_pane
            agent_verified = True
        if pane is not None:
            if not agent_verified:
                live_panes = _live_pane_ids(herdr_session=herdr_session)
                if pane not in live_panes:
                    evidence = {
                        "agent_name": agent_name,
                        "launch_id": launch_id,
                        "reconciliation_attempt": attempt,
                        "status": "already-absent",
                        "verified_absent": True,
                        "worker_pane": pane,
                    }
                    ledger.mark_launch_closed(key, launch_id, pane, evidence)
                    return evidence
                errors.append(
                    f"pane {pane} is live but agent {agent_name!r} could not be identity-verified"
                )
            else:
                try:
                    cleanup = _close_worker_pane(pane, herdr_session=herdr_session)
                except RecruiterError as error:
                    errors.append(str(error))
                else:
                    evidence = {
                        **cleanup,
                        "agent_name": agent_name,
                        "launch_id": launch_id,
                        "reconciliation_attempt": attempt,
                        "worker_pane": pane,
                    }
                    ledger.mark_launch_closed(key, launch_id, pane, evidence)
                    if evidence.get("verified_absent") is True:
                        return evidence
                    errors.append(
                        str(evidence.get("reason", "cleanup was not verified"))
                    )
        elif allow_not_found_absent and not_found and attempt == attempts:
            evidence = {
                "agent_name": agent_name,
                "launch_id": launch_id,
                "reconciliation_attempt": attempt,
                "status": "already-absent",
                "verified_absent": True,
                "worker_pane": None,
            }
            ledger.mark_launch_closed(key, launch_id, None, evidence)
            return evidence
        if attempt < attempts:
            time.sleep(0.05)
    pending = {
        "agent_name": agent_name,
        "launch_id": launch_id,
        "reason": "; ".join(errors)
        if errors
        else "exact agent launch remains uncertain",
        "status": "cleanup-pending",
        "verified_absent": False,
        "worker_pane": pane,
    }
    ledger.mark_launch_closed(key, launch_id, pane, pending)
    return pending


def _start_fenced_ledger_agent(
    ledger: JobLedger,
    key: str,
    token: str,
    role: str,
    name: str,
    order: dict,
    launch: str,
    *,
    split_direction: str = "right",
    tab_role: str | None = None,
    herdr_session: str,
    metadata: dict[str, object] | None = None,
) -> tuple[str, str | None, str, str]:
    """Journal before create; failed commit returns only truthful cleanup evidence."""

    with _request_runtime_lock(key):
        launch_id = ledger.begin_launch(
            key,
            token,
            role,
            name,
            herdr_session,
            order["cwd"],
            metadata=metadata,
        )
        pane: str | None = None
        try:
            pane, workspace_id, address = _start_herdr_agent(
                name,
                order,
                launch,
                split_direction=split_direction,
                tab_role=tab_role,
                herdr_session=herdr_session,
            )
            ledger.record_launch_created(
                key, token, launch_id, pane, workspace_id, address
            )
            if ledger.mark_launch_started(
                key, token, launch_id, pane, workspace_id, address
            ):
                return pane, workspace_id, address, launch_id
            failure = RecruiterError(
                f"lease ownership changed before {role} pane {pane} was committed"
            )
        except (RecruiterError, OSError, RuntimeError, KeyError, TypeError) as error:
            failure = error
        cleanup = _reconcile_exact_launch(
            ledger,
            key,
            launch_id,
            known_pane=pane,
            herdr_session=herdr_session,
            allow_not_found_absent=pane is None,
            trusted_created_identity=pane is not None,
        )
        raise FencedLaunchError(
            f"{role} fenced launch {launch_id} failed: {failure}", cleanup
        ) from failure


def _layout_warning(role: str, pane_id: str, reason: str) -> None:
    command_runtime.write_stderr(
        f"recruiter: {role} pane {pane_id} layout adjustment failed: {reason}; "
        "worker lifecycle continues\n"
    )


def _resize_started_pane(
    pane_id: str,
    *,
    split_direction: str,
    target_fraction: float,
    role: str,
    herdr_session: str | None = None,
) -> None:
    """Shrink a new 50/50 split without making presentation part of worker correctness.

    Agent startup remains one atomic ``herdr agent start`` operation. Resizing happens afterward
    by expanding the adjacent older pane toward the new pane. Herdr versions without layout
    controls produce a visible warning; an optional presentation failure never strands a healthy
    worker or changes lifecycle ownership.
    """
    if split_direction not in ("right", "down"):
        raise RecruiterError(
            f"pane resize direction must be right or down, got {split_direction!r}"
        )
    if not 0 < target_fraction < 0.5:
        raise RecruiterError(
            f"pane target fraction must be between 0 and 0.5, got {target_fraction!r}"
        )
    neighbor_direction = "left" if split_direction == "right" else "up"
    try:
        neighbor_response = _herdr_json(
            "pane",
            "neighbor",
            "--direction",
            neighbor_direction,
            "--pane",
            pane_id,
            timeout_seconds=LAYOUT_COMMAND_TIMEOUT_SECONDS,
            herdr_session=herdr_session,
        )
    except RecruiterError as error:
        _layout_warning(role, pane_id, str(error))
        return
    neighbor = neighbor_response.get("result", {}).get("neighbor", {})
    neighbor_pane = (
        neighbor.get("neighbor_pane_id") if isinstance(neighbor, dict) else None
    )
    if not isinstance(neighbor_pane, str) or not neighbor_pane:
        layout = neighbor.get("layout", {}) if isinstance(neighbor, dict) else {}
        panes = layout.get("panes", []) if isinstance(layout, dict) else []
        if isinstance(panes, list) and len(panes) == 1:
            return
        _layout_warning(role, pane_id, "Herdr returned no adjacent pane")
        return
    amount = format(0.5 - target_fraction, ".2f").rstrip("0").rstrip(".")
    try:
        resize_response = _herdr_json(
            "pane",
            "resize",
            "--pane",
            neighbor_pane,
            "--direction",
            split_direction,
            "--amount",
            amount,
            timeout_seconds=LAYOUT_COMMAND_TIMEOUT_SECONDS,
            herdr_session=herdr_session,
        )
    except RecruiterError as error:
        _layout_warning(role, pane_id, str(error))
        return
    resize = resize_response.get("result", {}).get("resize", {})
    if not isinstance(resize, dict) or resize.get("changed") is not True:
        _layout_warning(role, pane_id, "Herdr did not change the split ratio")


def _worker_pane_fraction(order: dict) -> float | None:
    return WATCHDOG_PANE_FRACTION if order.get("agent") in WATCHDOG_AGENTS else None


def _worker_tab_role(order: dict) -> str:
    return "oversight" if order.get("agent") in WATCHDOG_AGENTS else "workers"


def _wait_for_agent_health(
    pane_id: str,
    *,
    expected_agent: str,
    expected_process: str,
    expected_cwd: str,
    timeout_ms: int,
    completion_order: dict | None = None,
    herdr_session: str | None = None,
) -> dict[str, object]:
    """Prove an expected harness actually started; pane creation alone is insufficient."""
    resolved_cwd = os.path.realpath(expected_cwd)
    deadline = time.monotonic() + timeout_ms / 1000
    started = time.monotonic()
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        pane = (
            _herdr_json("pane", "get", pane_id, herdr_session=herdr_session)
            .get("result", {})
            .get("pane", {})
        )
        process_info = (
            _herdr_json(
                "pane",
                "process-info",
                "--pane",
                pane_id,
                herdr_session=herdr_session,
            )
            .get("result", {})
            .get("process_info", {})
        )
        processes = (
            process_info.get("foreground_processes", [])
            if isinstance(process_info, dict)
            else []
        )
        matching_process = next(
            (
                process
                for process in processes
                if isinstance(process, dict)
                and (
                    process.get("name") == expected_process
                    or expected_process in str(process.get("cmdline", ""))
                    or expected_process
                    in " ".join(str(item) for item in process.get("argv", []))
                )
            ),
            None,
        )
        detected_agent = pane.get("agent") if isinstance(pane, dict) else None
        status = pane.get("agent_status") if isinstance(pane, dict) else None
        cwd = (
            pane.get("foreground_cwd", pane.get("cwd"))
            if isinstance(pane, dict)
            else None
        )
        cwd_matches = isinstance(cwd, str) and os.path.realpath(cwd) == resolved_cwd
        latest = {
            "agent_status": status,
            "cwd": cwd,
            "cwd_matches": cwd_matches,
            "detected_agent": detected_agent,
            "expected_agent": expected_agent,
            "expected_process": expected_process,
            "healthy": False,
            "pane_id": pane_id,
            "process_pid": matching_process.get("pid")
            if isinstance(matching_process, dict)
            else None,
        }
        if (
            matching_process is not None
            and detected_agent == expected_agent
            and status in ("working", "idle", "done")
        ):
            if not cwd_matches:
                raise RecruiterError(
                    f"agent {pane_id} started in {cwd!r}, expected {expected_cwd!r}"
                )
            latest["healthy"] = True
            return latest
        completed_during_startup = False
        if completion_order is not None:
            try:
                load_result(
                    completion_order["result_path"],
                    expected_order_id=completion_order["order_id"],
                )
            except ContractError:
                completed_during_startup = False
            else:
                completed_during_startup = True
        if completed_during_startup:
            latest["completed_during_startup"] = True
            latest["healthy"] = True
            return latest
        if (
            matching_process is None
            and time.monotonic() - started >= STARTUP_FAILURE_SETTLE_SECONDS
        ):
            output = _pane_recent_output(pane_id, herdr_session=herdr_session)
            raise RecruiterError(
                f"agent pane {pane_id} did not start expected {expected_process} process; recent output: {output[-1000:]}"
            )
        time.sleep(HEALTH_PROBE_SECONDS)
    raise RecruiterError(
        f"agent pane {pane_id} did not become healthy within {timeout_ms} ms; evidence={json.dumps(latest)}"
    )


def _wait_for_worker_health(
    worker_pane: str,
    order: dict,
    timeout_ms: int,
    roster: dict | None = None,
    *,
    herdr_session: str | None = None,
) -> dict[str, object]:
    override = (roster or {}).get("health", {}).get(order["harness"], {})
    return _wait_for_agent_health(
        worker_pane,
        expected_agent=override.get(
            "expected_agent", EXPECTED_HARNESS_AGENT[order["harness"]]
        ),
        expected_process=override.get(
            "expected_process", EXPECTED_HARNESS_PROCESS[order["harness"]]
        ),
        expected_cwd=order["cwd"],
        timeout_ms=timeout_ms,
        completion_order=order,
        herdr_session=herdr_session,
    )


def _live_pane_ids(*, herdr_session: str | None = None) -> set[str]:
    response = _herdr_json("pane", "list", herdr_session=herdr_session)
    panes = response.get("result", {}).get("panes", [])
    return {
        pane["pane_id"]
        for pane in panes
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
    }


def _close_worker_pane(
    worker_pane: str, *, herdr_session: str | None = None
) -> dict[str, object]:
    """Close one known-owned worker and prove its pane id is no longer live.

    A failed close is recoverable only when a fresh pane listing proves the target was already
    absent. Other close/list faults propagate; terminal publication must not hide leaked panes.
    """
    session = _recorded_herdr_session(herdr_session, "pane cleanup")
    close_status = "closed"
    try:
        _herdr("pane", "close", worker_pane, herdr_session=session)
    except RecruiterError:
        if worker_pane in _live_pane_ids(herdr_session=session):
            raise
        close_status = "already-absent"
    if worker_pane in _live_pane_ids(herdr_session=session):
        raise RecruiterError(f"worker pane {worker_pane} is still live after close")
    return {
        "herdr_session": session,
        "status": close_status,
        "worker_pane": worker_pane,
        "verified_absent": True,
    }


def _write_worker_instructions(
    order: dict,
    worker_result_path: Path,
    destination: Path,
    artifact_manifest: Any,
) -> None:
    """Append one final, literal delivery contract to the stage brief.

    The order's public result_path belongs to the Recruiter. The worker writes only this lease's
    private staging path, which prevents a stale/recovered worker from publishing over its owner.
    """
    source = Path(order["instructions_path"])
    try:
        original = source.read_text()
    except OSError as e:
        raise RecruiterError(f"worker instructions {source} are unreadable: {e}") from e
    paths = {item.kind: item.staging_path for item in artifact_manifest.artifacts}
    destinations = (
        "Write ALL required artifacts to these literal lease-private paths:\n"
        f"- result.json: {paths['result']}\n"
        f"- compacted.md: {paths['compacted']}\n"
        f"- handoff.md: {paths['handoff']}\n"
        + (f"- answer.json: {paths['answer']}\n" if "answer" in paths else "")
        + "Write result.json as a JSON object with these required fields:\n"
        + f'- `order_id`: exactly "{order["order_id"]}"\n'
        + '- `verdict`: exactly one of "passed", "failed", or "blocked"\n'
        + "- `full_log`: a non-empty transcript path, session id, or worker-session description\n"
        + "Do not invent workflow stage names; omit `revisit` unless the assignment names one.\n"
        + (
            "Write answer.json as either a cited success object "
            f'`{{"consult_id":"{artifact_manifest.consult_id}","answer":"...",'
            '"citations":["file:line"]}}` or a failure object '
            f'`{{"consult_id":"{artifact_manifest.consult_id}","error":"..."}}`.\n'
            if artifact_manifest.consult_id is not None
            else ""
        )
        + "Markdown artifacts must contain non-whitespace text. Do not write any public path.\n"
    )
    suffix = (
        "\n\n# Recruiter delivery contract (final and authoritative)\n\n"
        "The Recruiter, not this worker, publishes completion artifacts. "
        "Ignore every earlier artifact destination in this brief.\n" + destinations
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(original.rstrip() + suffix)
        os.replace(temporary, destination)
    except OSError as e:
        temporary.unlink(missing_ok=True)
        raise RecruiterError(
            f"could not write lease-specific worker instructions {destination}: {e}"
        ) from e


def _wait_for_agent_status(
    worker_pane: str,
    timeout_ms: int,
    monitor_finalized: threading.Event | None,
    *,
    herdr_session: str | None = None,
) -> bool:
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
    _session, argv = _herdr_argv(args, herdr_session)
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
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
            raise AgentWaitTimeout(
                f"herdr wait agent-status {worker_pane} timed out after {timeout_ms} ms"
            )
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
    raise RecruiterError(f"{' '.join(argv)} failed: {(stderr or stdout).strip()}")


def _report_state(
    pane: str | None,
    state: str,
    message: str,
    *,
    herdr_session: str | None = None,
) -> None:
    """Surface UpAgent in Herdr's agents sidebar (`pane report-agent`). BEST-EFFORT:
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
            "upagent",
            "--agent",
            "upagent",
            "--state",
            state,
            "--message",
            message,
            herdr_session=herdr_session,
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


def _watchdog_terminal_reason(order: dict, result: dict) -> str | None:
    """Return why a watchdog result is premature, or None when its durable gate is terminal."""
    if order.get("agent") not in WATCHDOG_AGENTS:
        return None
    terminal = order["watchdog_terminal"]
    path = Path(terminal["path"])
    if not path.is_file():
        return (
            f"authoritative {terminal['kind']} terminal record does not exist: {path}"
        )
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(
            f"authoritative {terminal['kind']} terminal record {path} is invalid: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ContractError(
            f"authoritative {terminal['kind']} terminal record {path} must be an object"
        )
    identity = terminal["identity"]
    if terminal["kind"] == "plan":
        if value.get("plan_id") != identity:
            raise ContractError(
                f"plan terminal record {path} does not match plan {identity!r}"
            )
        if value.get("state") not in ("succeeded", "stopped"):
            return f"plan terminal record {path} is not terminal"
        summary_path = value.get("summary_path")
        if (
            not isinstance(summary_path, str)
            or not Path(summary_path).is_absolute()
            or not Path(summary_path).is_file()
        ):
            raise ContractError(
                f"plan terminal record {path} has no existing absolute summary_path"
            )
        return None
    if value.get("phase_id") != identity:
        raise ContractError(
            f"phase terminal record {path} does not match phase {identity!r}"
        )
    if value.get("verdict") not in ("passed", "partial", "blocked", "failed"):
        return f"phase terminal record {path} is not terminal"
    return None


def _archive_premature_watchdog_result(path: Path, number: int) -> Path:
    archive_dir = path.parent / "premature-results"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / f"{number:04d}-{time.time_ns()}.json"
    os.replace(path, archived)
    for directory in {path.parent, archive_dir}:
        directory_fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return archived


def _may_preserve_worker_result(
    order: dict, result: dict, *, startup_validated: bool
) -> bool:
    """A wait fault may preserve only a semantically terminal worker result."""
    if not startup_validated:
        return False
    try:
        return _watchdog_terminal_reason(order, result) is None
    except ContractError:
        return False


# --- commands ----------------------------------------------------------------


def _normalize_public_result_revisit(order: dict, manifest: Any) -> None:
    """Keep route-stage mechanics out of ad-hoc public worker output.

    Public requests are assigned an internal compatibility stage, but their workers do not know
    the route vocabulary and must not be asked to manufacture it. Python owns this envelope field.
    """
    if not isinstance(order.get("public_request"), dict):
        return
    path = manifest.artifact("result").staging_path
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(value, dict):
        return
    verdict = value.get("verdict")
    if verdict in ("passed", "blocked"):
        revisit: list[str] = []
    elif verdict == "failed":
        revisit = [order["stage_id"]]
    else:
        return
    if value.get("revisit") == revisit:
        return
    JobLedger._write_json(path, {**value, "revisit": revisit})


def _complete_typed_bundle(
    ledger: JobLedger,
    key: str,
    order: dict,
    manifest: Any,
    worker_address: str | None,
    *,
    herdr_session: str,
) -> bool:
    """Validate once, request one same-worker repair, then deterministically block if needed.

    Return whether Python had to author a blocked bundle.
    """
    python_blocked = False

    def validate() -> dict:
        _normalize_public_result_revisit(order, manifest)
        return cast(
            dict,
            completion.validate_bundle(
                manifest,
                load_result=load_result,
                load_answer=contracts_consult.load_answer,
            ),
        )

    result: dict | None = None
    try:
        result = validate()
    except CompletionError as first_error:
        ledger._event(
            key,
            "completion-artifact-invalid",
            repair_number=1,
            reason=str(first_error),
        )
        repair_error: Exception | None = None
        if worker_address is None:
            repair_error = RecruiterError(
                "the original worker address is unavailable for the one allowed repair"
            )
        else:
            paths = "\n".join(
                f"- {item.kind}: {item.staging_path}" for item in manifest.artifacts
            )
            try:
                _submit_agent_prompt(
                    worker_address,
                    "COMPLETION_REPAIR 1/1: Python rejected the required staged artifact "
                    f"bundle: {first_error}\nRepair only the artifacts at these exact paths, "
                    "then stop. Do not launch or delegate to another worker.\n"
                    f"{paths}",
                    idle_timeout_ms=WATCHDOG_CONTINUATION_TIMEOUT_MS,
                    herdr_session=herdr_session,
                )
                result = validate()
            except (RecruiterError, CompletionError, OSError) as error:
                repair_error = error
        if repair_error is not None:
            reason = (
                f"completion artifacts remained invalid after exactly one same-worker repair: "
                f"{repair_error}"
            )
            result = completion.write_blocked_bundle(
                manifest,
                reason,
                write_result=lambda path, why: _write_blocked_result(
                    order, why, path, preserve_valid=False
                ),
                failure_answer=contracts_consult.failure_answer,
            )
            completion.validate_bundle(
                manifest,
                load_result=load_result,
                load_answer=contracts_consult.load_answer,
            )
            ledger._event(key, "completion-repair-exhausted", reason=reason)
            python_blocked = True

    if result is None:
        raise CompletionError("completion reactor produced no terminal artifact bundle")
    resolved = resolve_consult_claims(order, result)
    consult_errors = completion.mandatory_consult_errors(manifest, result, resolved)
    if result.get("verdict") == "passed" and consult_errors:
        reason = "mandatory consultation gate: " + "; ".join(consult_errors)
        result = completion.write_blocked_bundle(
            manifest,
            reason,
            write_result=lambda path, why: _write_blocked_result(
                order, why, path, preserve_valid=False
            ),
            failure_answer=contracts_consult.failure_answer,
        )
        completion.validate_bundle(
            manifest,
            load_result=load_result,
            load_answer=contracts_consult.load_answer,
        )
        ledger._event(key, "mandatory-consult-blocked", reasons=consult_errors)
        python_blocked = True
    return python_blocked


def _start_completion_monitor(
    order: dict,
    worker_result_path: Path,
    timeout_ms: int,
    *,
    inactivity_check_ms: int | None = None,
    on_inactivity: Callable[[int], None] | None = None,
    artifact_manifest: Any,
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Watch one lease's staging result and wake its job runner when it validates.

    Herdr's agent-status signal is only an accelerator. The monitor never publishes, closes a
    pane, or emits terminal output; the single job runner retains lifecycle ownership.
    """
    stop = threading.Event()
    finalized = threading.Event()
    next_check = (
        time.monotonic() + inactivity_check_ms / 1000
        if inactivity_check_ms is not None and on_inactivity is not None
        else None
    )

    def monitor() -> None:
        nonlocal next_check
        check_number = 0
        while not stop.is_set():
            if (
                next_check is not None
                and on_inactivity is not None
                and time.monotonic() >= next_check
            ):
                check_number += 1
                on_inactivity(check_number)
                next_check = time.monotonic() + cast(int, inactivity_check_ms) / 1000
            try:
                completion.validate_bundle(
                    artifact_manifest,
                    load_result=load_result,
                    load_answer=contracts_consult.load_answer,
                )
            except CompletionError:
                stop.wait(COMPLETION_MONITOR_POLL_SECONDS)
                continue
            finalized.set()
            while finalized.is_set() and not stop.wait(COMPLETION_MONITOR_POLL_SECONDS):
                pass

    thread = threading.Thread(
        target=monitor, name=f"upagent-monitor-{order['order_id'][:24]}", daemon=True
    )
    thread.start()
    return stop, finalized, thread


def _write_text_atomic(path: Path, text_value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(text_value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _wait_typed_file(path: Path, timeout_ms: int, parser: Callable[[str], Any]) -> Any:
    """Wait for one atomically replaceable LLM response and reject stable malformed output."""
    deadline = time.monotonic() + timeout_ms / 1000
    invalid_signature: tuple[int, int] | None = None
    invalid_since = 0.0
    latest_error: LifecycleError | None = None
    while time.monotonic() < deadline:
        try:
            text_value = path.read_text()
        except FileNotFoundError:
            time.sleep(HEALTH_PROBE_SECONDS)
            continue
        try:
            return parser(text_value)
        except LifecycleError as error:
            latest_error = error
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != invalid_signature:
            invalid_signature = signature
            invalid_since = time.monotonic()
        elif time.monotonic() - invalid_since >= INVALID_RESULT_SETTLE_SECONDS:
            raise RecruiterError(
                f"LLM response {path} is invalid: {latest_error}"
            ) from latest_error
        time.sleep(HEALTH_PROBE_SECONDS)
    raise RecruiterError(f"LLM response {path} did not arrive within {timeout_ms} ms")


_PROMPT_SUBMISSION_LOCK = threading.Lock()


def _submit_agent_prompt(
    target: str,
    message: str,
    idle_timeout_ms: int,
    *,
    herdr_session: str | None = None,
) -> None:
    """Submit one prompt only after Herdr proves the dedicated target is idle.

    ``herdr agent send`` intentionally pastes without Enter. The Hub resolves the target to its
    current pane and uses one serialized ``pane run`` socket action, which includes Enter. This is
    safe for a dedicated manager; requester delivery remains best-effort and is skipped while busy.
    """
    with _PROMPT_SUBMISSION_LOCK:
        _herdr(
            "agent",
            "wait",
            target,
            "--status",
            "idle",
            "--timeout",
            str(idle_timeout_ms),
            herdr_session=herdr_session,
        )
        agent = (
            _herdr_json("agent", "get", target, herdr_session=herdr_session)
            .get("result", {})
            .get("agent", {})
        )
        pane_id = agent.get("pane_id") if isinstance(agent, dict) else None
        if not isinstance(pane_id, str) or not pane_id:
            raise RecruiterError(f"Herdr agent target {target!r} has no current pane")
        _herdr("pane", "run", pane_id, message, herdr_session=herdr_session)


def _notify_requester(
    ledger: JobLedger,
    key: str,
    order: dict,
    generation: int,
    message_type: str,
    message: str,
    detail: dict | None = None,
) -> None:
    """Publish one durable requester event. Delivery is the requester's await."""
    request_id = lifecycle.request_identity(order)
    address = lifecycle.requester_address(order)
    ledger.publish_requester(key, request_id, generation, message_type, message, detail)
    if address.kind == "file-mailbox":
        lifecycle.RequestMailbox(address.address).publish(
            request_id, generation, message_type, message, detail
        )


def _manager_anchor_pane(order: dict, *, herdr_session: str | None = None) -> str:
    """Resolve where the dedicated manager belongs.

    Managers default to their requester's cockpit so the lifecycle owner is visible beside the
    TUI/leader and worker it serves. `shared` is an explicit opt-in for peripheral work. A named
    workspace is resolved to a real pane before launch; `_start_herdr_agent` then atomically starts
    the manager in that pane's tab.
    """
    placement = order.get("manager_placement") or {"mode": "requester"}
    mode = placement.get("mode")
    if mode == "requester":
        return placement.get("anchor_pane") or order["cockpit_pane"]
    if mode == "shared":
        recruiter_pane = _recruiter_pane_from_state()
        if recruiter_pane is None:
            raise RecruiterError(
                "shared manager placement requires a live Recruiter pane"
            )
        return recruiter_pane
    if mode != "workspace":
        raise RecruiterError(f"unsupported manager placement mode: {mode}")

    workspaces = (
        _herdr_json("workspace", "list", herdr_session=herdr_session)
        .get("result", {})
        .get("workspaces", [])
    )
    workspace_id = placement.get("workspace_id")
    if workspace_id is None:
        workspace_label = placement.get("workspace_label")
        match = next(
            (
                workspace
                for workspace in workspaces
                if isinstance(workspace, dict)
                and workspace.get("label") == workspace_label
            ),
            None,
        )
        workspace_id = match.get("workspace_id") if isinstance(match, dict) else None
    if not isinstance(workspace_id, str) or not workspace_id:
        raise RecruiterError("manager placement workspace does not exist")

    anchor = placement.get("anchor_pane")
    if isinstance(anchor, str) and anchor:
        pane = (
            _herdr_json("pane", "get", anchor, herdr_session=herdr_session)
            .get("result", {})
            .get("pane", {})
        )
        if not isinstance(pane, dict) or pane.get("workspace_id") != workspace_id:
            raise RecruiterError(
                f"manager anchor pane {anchor} is not in requested workspace {workspace_id}"
            )
        return anchor
    panes = (
        _herdr_json(
            "pane", "list", "--workspace", workspace_id, herdr_session=herdr_session
        )
        .get("result", {})
        .get("panes", [])
    )
    anchor = next(
        (
            pane.get("pane_id")
            for pane in panes
            if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
        ),
        None,
    )
    if not isinstance(anchor, str):
        raise RecruiterError(f"manager placement workspace {workspace_id} has no pane")
    return anchor


def _direct_manager(
    config: object,
    order: dict,
    generation: int = 1,
    *,
    herdr_session: str | None = None,
) -> dict[str, object]:
    """Default direct lifecycle owner: no standing manager LLM pane."""
    request_id = lifecycle.request_identity(order)
    return {
        "address": None,
        "config": config,
        "decision": lifecycle.ManagerDecision(
            request_id=request_id,
            generation=generation,
            decision="approved",
            message="Direct lifecycle: Python mechanical validation gates launch; no dedicated Account Manager was created.",
        ),
        "generation": generation,
        "health": None,
        "herdr_session": herdr_session,
        "pane": None,
        "workspace_id": None,
    }


def _start_account_manager(
    ledger: JobLedger,
    key: str,
    token: str,
    order: dict,
    roster: dict,
    generation: int = 1,
    mechanical_validation: dict[str, object] | None = None,
    herdr_session: str | None = None,
) -> dict[str, object]:
    session = _recorded_herdr_session(herdr_session, "account manager lifecycle")
    config = llm_management.load_management_config(roster)
    request_id = lifecycle.request_identity(order)
    directory = ledger.manager_dir(key, generation)
    decision_path = directory / "decision.json"
    decision_path.unlink(missing_ok=True)
    brief_path = directory / "brief.md"
    _write_text_atomic(
        brief_path,
        llm_management.account_manager_brief(
            request_id, generation, order, decision_path, mechanical_validation
        ),
    )
    command = llm_management.render_role_command(
        config.account_manager, brief_path, order["cwd"], decision_path
    )
    name = _safe_agent_name("upagent-manager", request_id, generation)
    manager_order = {
        **order,
        "cockpit_pane": _manager_anchor_pane(order, herdr_session=session),
    }
    manager_pane, workspace_id, manager_address, launch_id = _start_fenced_ledger_agent(
        ledger,
        key,
        token,
        "manager",
        name,
        manager_order,
        command,
        split_direction="down",
        tab_role="oversight",
        herdr_session=session,
        metadata={"generation": generation},
    )
    _resize_started_pane(
        manager_pane,
        split_direction="down",
        target_fraction=SUPPORT_PANE_FRACTION,
        role="account manager",
        herdr_session=session,
    )
    try:
        health = _wait_for_agent_health(
            manager_pane,
            expected_agent=config.account_manager.expected_agent,
            expected_process=config.account_manager.expected_process,
            expected_cwd=order["cwd"],
            timeout_ms=config.startup_timeout_ms,
            herdr_session=session,
        )
        decision = _wait_typed_file(
            decision_path,
            config.account_manager.timeout_ms,
            lambda text_value: lifecycle.parse_manager_decision(
                text_value, request_id, generation
            ),
        )
    except (RecruiterError, OSError):
        cleanup = _close_worker_pane(manager_pane, herdr_session=session)
        ledger.mark_launch_closed(
            key,
            launch_id,
            manager_pane,
            cleanup,
            expected_lease_token=token,
        )
        raise
    if not ledger.mark_manager_ready(key, token, decision):
        cleanup = _close_worker_pane(manager_pane, herdr_session=session)
        ledger.mark_launch_closed(
            key,
            launch_id,
            manager_pane,
            cleanup,
            expected_lease_token=token,
        )
        raise RecruiterError(
            f"lease ownership changed before manager {manager_address} became ready"
        )
    _notify_requester(
        ledger,
        key,
        order,
        generation,
        "account-manager-ready",
        decision.message,
        {
            "manager_address": manager_address,
            "manager_pane": manager_pane,
            "manager_workspace_id": workspace_id,
        },
    )
    return {
        "address": manager_address,
        "config": config,
        "decision": decision,
        "generation": generation,
        "health": health,
        "herdr_session": session,
        "launch_id": launch_id,
        "lease_token": token,
        "pane": manager_pane,
        "workspace_id": workspace_id,
    }


def _ask_manager_about_startup(
    ledger: JobLedger,
    key: str,
    order: dict,
    manager: dict[str, object],
    worker_evidence: dict[str, object],
) -> Any:
    generation = cast(int, manager["generation"])
    request_id = lifecycle.request_identity(order)
    directory = ledger.manager_dir(key, generation)
    evidence_path = directory / "worker-startup-evidence.json"
    output_path = directory / "worker-startup-assessment.json"
    output_path.unlink(missing_ok=True)
    JobLedger._write_json(evidence_path, worker_evidence)
    config = cast(Any, manager["config"])
    message = llm_management.checker_brief(
        request_id, generation, evidence_path, output_path
    )
    _submit_agent_prompt(
        cast(str, manager["address"]),
        message,
        idle_timeout_ms=config.account_manager.timeout_ms,
        herdr_session=cast(str | None, manager.get("herdr_session")),
    )
    assessment = _wait_typed_file(
        output_path,
        config.account_manager.timeout_ms,
        lambda text_value: lifecycle.parse_check_assessment(
            text_value, request_id, generation
        ),
    )
    ledger._event(
        key,
        "worker-startup-assessed",
        assessment=assessment.assessment,
        confidence=assessment.confidence,
        recommended_action=assessment.recommended_action,
    )
    return assessment


def _run_one_shot_checker(
    ledger: JobLedger,
    key: str,
    order: dict,
    manager: dict[str, object],
    worker_pane: str,
    worker_result_path: Path,
    check_number: int,
) -> object:
    """Launch one bounded LLM assessment from a fresh evidence snapshot, then destroy it."""
    generation = cast(int, manager["generation"])
    request_id = lifecycle.request_identity(order)
    config = cast(Any, manager["config"])
    herdr_session = _recorded_herdr_session(
        manager.get("herdr_session"), "checker cleanup"
    )
    directory = ledger.request_dir(key) / "checks" / f"{check_number:06d}"
    evidence_path = directory / "evidence.json"
    output_path = directory / "assessment.json"
    output_path.unlink(missing_ok=True)
    pane = (
        _herdr_json("pane", "get", worker_pane, herdr_session=herdr_session)
        .get("result", {})
        .get("pane", {})
    )
    process_info = (
        _herdr_json(
            "pane", "process-info", "--pane", worker_pane, herdr_session=herdr_session
        )
        .get("result", {})
        .get("process_info", {})
    )
    try:
        load_result(worker_result_path, expected_order_id=order["order_id"])
    except ContractError:
        valid_result = False
    else:
        valid_result = True
    evidence = {
        "check_number": check_number,
        "pane": pane,
        "process_info": process_info,
        "recent_output": _pane_recent_output(
            worker_pane, lines=120, herdr_session=herdr_session
        )[-8000:],
        "request_id": request_id,
        "result_valid": valid_result,
        "worker_pane": worker_pane,
    }
    JobLedger._write_json(evidence_path, evidence)
    brief_path = directory / "brief.md"
    _write_text_atomic(
        brief_path,
        llm_management.checker_brief(
            request_id, generation, evidence_path, output_path
        ),
    )
    command = llm_management.render_role_command(
        config.checker, brief_path, order["cwd"], output_path
    )
    name = _safe_agent_name(f"upagent-check-{check_number}", request_id, generation)
    checker_anchor = (
        manager["pane"] if manager["pane"] is not None else order["cockpit_pane"]
    )
    checker_order = {**order, "cockpit_pane": cast(str, checker_anchor)}
    lease_token = manager.get("lease_token")
    if not isinstance(lease_token, str) or not lease_token:
        raise RecruiterError("one-shot checker requires the owning lease token")
    checker_pane, _, _, launch_id = _start_fenced_ledger_agent(
        ledger,
        key,
        lease_token,
        "checker",
        name,
        checker_order,
        command,
        split_direction="down",
        tab_role="oversight",
        herdr_session=herdr_session,
        metadata={"check_number": check_number, "generation": generation},
    )
    try:
        _resize_started_pane(
            checker_pane,
            split_direction="down",
            target_fraction=SUPPORT_PANE_FRACTION,
            role="one-shot checker",
            herdr_session=herdr_session,
        )
        _wait_for_agent_health(
            checker_pane,
            expected_agent=config.checker.expected_agent,
            expected_process=config.checker.expected_process,
            expected_cwd=order["cwd"],
            timeout_ms=config.startup_timeout_ms,
            herdr_session=herdr_session,
        )
        assessment = _wait_typed_file(
            output_path,
            config.checker.timeout_ms,
            lambda text_value: lifecycle.parse_check_assessment(
                text_value, request_id, generation
            ),
        )
    finally:
        checker_cleanup = _close_worker_pane(checker_pane, herdr_session=herdr_session)
        ledger.mark_launch_closed(
            key,
            launch_id,
            checker_pane,
            checker_cleanup,
            expected_lease_token=cast(str, manager["lease_token"]),
        )
    ledger._event(
        key,
        "worker-checked",
        assessment=assessment.assessment,
        check_number=check_number,
        confidence=assessment.confidence,
        recommended_action=assessment.recommended_action,
    )
    if manager["address"] is not None:
        with suppress(RecruiterError):
            _submit_agent_prompt(
                cast(str, manager["address"]),
                f"Check {check_number} assessed worker {worker_pane} as "
                f"{assessment.assessment}: {assessment.message}",
                idle_timeout_ms=config.account_manager.timeout_ms,
                herdr_session=herdr_session,
            )
    if assessment.assessment not in ("healthy", "completed"):
        _notify_requester(
            ledger,
            key,
            order,
            generation,
            "worker-check-alert",
            assessment.message,
            {
                "assessment": assessment.assessment,
                "check_number": check_number,
                "confidence": assessment.confidence,
                "worker_pane": worker_pane,
            },
        )
    return assessment


def _await_requester_timeout_decision(
    ledger: JobLedger,
    key: str,
    token: str,
    order: dict,
    manager: dict[str, object],
    worker_pane: str,
    timeout_number: int,
    monitor_finalized: threading.Event | None,
) -> int | None:
    """Ask the recorded owner before a hard stop; return an authorized extension in ms."""
    generation = cast(int, manager["generation"])
    request_id = lifecycle.request_identity(order)
    nonce = uuid.uuid4().hex
    lease = ledger.mark_awaiting_requester(key, token, nonce, timeout_number)
    control_token = cast(str, lease["requester_control_token"])
    config = cast(Any, manager["config"])
    response_path = ledger.request_dir(key) / "responses" / f"{nonce}.json"
    command = (
        f"just upagent-respond {shlex.quote(str(ledger.request_dir(key) / 'request.json'))} "
        f"{shlex.quote(control_token)} {shlex.quote(nonce)} "
        "<extend|cancel> <extension-ms-or-0>"
    )
    message = (
        f"Worker {worker_pane} reached work cap {timeout_number}. Inspect it if useful, then "
        f"authorize an extension or cancellation within {config.requester_grace_ms} ms. "
        f"Response command: {command}"
    )
    if manager["address"] is not None:
        with suppress(RecruiterError):
            _submit_agent_prompt(
                cast(str, manager["address"]),
                message,
                idle_timeout_ms=config.account_manager.timeout_ms,
                herdr_session=cast(str | None, manager.get("herdr_session")),
            )
    _notify_requester(
        ledger,
        key,
        order,
        generation,
        "timeout-warning",
        message,
        {
            "decision_nonce": nonce,
            "response_command": command,
            "timeout_number": timeout_number,
            "worker_pane": worker_pane,
        },
    )
    deadline = time.monotonic() + config.requester_grace_ms / 1000
    while time.monotonic() < deadline:
        if monitor_finalized is not None and monitor_finalized.is_set():
            ledger._event(
                key,
                "result-arrived-during-timeout-grace",
                timeout_number=timeout_number,
            )
            return 1
        if response_path.is_file():
            try:
                decision = lifecycle.parse_requester_decision(
                    response_path.read_text(), request_id, generation
                )
            except (OSError, LifecycleError) as error:
                raise RecruiterError(
                    f"requester decision is invalid: {error}"
                ) from error
            if decision.action == "extend":
                assert decision.extension_ms is not None
                ledger.extend_lease(key, token, decision.extension_ms)
                _notify_requester(
                    ledger,
                    key,
                    order,
                    generation,
                    "worker-extended",
                    decision.message,
                    {
                        "extension_ms": decision.extension_ms,
                        "timeout_number": timeout_number,
                    },
                )
                return decision.extension_ms
            _notify_requester(
                ledger,
                key,
                order,
                generation,
                "worker-cancelling",
                decision.message,
                {"timeout_number": timeout_number},
            )
            return None
        time.sleep(HEALTH_PROBE_SECONDS)
    _notify_requester(
        ledger,
        key,
        order,
        generation,
        "hard-timeout",
        "Requester grace expired without a decision; the Hub will close the owned worker.",
        {"timeout_number": timeout_number, "worker_pane": worker_pane},
    )
    return None


def _startup_rescue_advice(
    ledger: JobLedger,
    key: str,
    order: dict,
    manager: dict[str, object],
    failure_reason: str,
) -> str:
    """One bounded broker assessment of a failed launch: retry it, or stop and say why.

    This is the rescue half of the broker arrangement. The fast Python launch stays the
    normal path, and a small LLM is hired exactly at the failure point to judge whether a
    relaunch can succeed or the requester must hear about it first. When the broker itself
    cannot run, default to one retry — ensuring the worker gets created beats waiting on
    unavailable advice.
    """
    generation = cast(int, manager["generation"])
    request_id = lifecycle.request_identity(order)
    config = cast(Any, manager["config"])
    directory = ledger.request_dir(key) / "rescue"
    evidence_path = directory / "evidence.json"
    output_path = directory / "assessment.json"
    evidence = {
        "kind": "startup-rescue",
        "startup_failure": failure_reason,
        "request_id": request_id,
        "worker_configuration": {
            field: order.get(field)
            for field in ("order_id", "harness", "model", "agent", "effort", "cwd")
        },
    }
    name = _safe_agent_name("upagent-rescue", request_id, generation)
    anchor = manager["pane"] if manager["pane"] is not None else order["cockpit_pane"]
    rescue_order = {**order, "cockpit_pane": cast(str, anchor)}
    herdr_session = _recorded_herdr_session(
        manager.get("herdr_session"), "startup rescue cleanup"
    )
    try:
        # Every filesystem step lives inside this guard: this function must never raise —
        # its caller runs in a detached job runner whose stderr goes nowhere.
        JobLedger._write_json(evidence_path, evidence)
        brief_path = directory / "brief.md"
        _write_text_atomic(
            brief_path,
            llm_management.checker_brief(
                request_id, generation, evidence_path, output_path
            ),
        )
        command = llm_management.render_role_command(
            config.checker, brief_path, order["cwd"], output_path
        )
        output_path.unlink(missing_ok=True)
        lease_token = manager.get("lease_token")
        if not isinstance(lease_token, str) or not lease_token:
            raise RecruiterError("startup rescue requires the owning lease token")
        rescue_pane, _, _, launch_id = _start_fenced_ledger_agent(
            ledger,
            key,
            lease_token,
            "rescue",
            name,
            rescue_order,
            command,
            split_direction="down",
            tab_role="oversight",
            herdr_session=herdr_session,
            metadata={"generation": generation},
        )
        try:
            _wait_for_agent_health(
                rescue_pane,
                expected_agent=config.checker.expected_agent,
                expected_process=config.checker.expected_process,
                expected_cwd=order["cwd"],
                timeout_ms=config.startup_timeout_ms,
                herdr_session=herdr_session,
            )
            assessment = _wait_typed_file(
                output_path,
                config.checker.timeout_ms,
                lambda text_value: lifecycle.parse_check_assessment(
                    text_value, request_id, generation
                ),
            )
        finally:
            rescue_cleanup = _close_worker_pane(
                rescue_pane, herdr_session=herdr_session
            )
            ledger.mark_launch_closed(
                key,
                launch_id,
                rescue_pane,
                rescue_cleanup,
                expected_lease_token=cast(str, manager["lease_token"]),
            )
    except (RecruiterError, LifecycleError, ContractError, OSError) as error:
        # The event is telemetry; the advice is the contract. A broken ledger write must
        # not turn "broker unavailable" into a raised exception.
        with suppress(OSError):
            ledger._event(key, "startup-rescue-advice-unavailable", reason=str(error))
        return "retry-startup"
    action = cast(str, assessment.recommended_action)
    with suppress(OSError):
        ledger._event(
            key,
            "startup-rescue-assessed",
            assessment=assessment.assessment,
            recommended_action=action,
        )
    return action


def _run_order(
    order_path: str,
    roster_path: str,
    worker_result_path: Path,
    on_worker_launched: Callable[[str, str | None, str], threading.Event] | None = None,
    worker_instructions_path: Path | None = None,
    on_worker_healthy: Callable[[dict[str, object]], None] | None = None,
    on_timeout: Callable[[int, threading.Event | None], int | None] | None = None,
    before_worker_cleanup: Callable[[], bool | None] | None = None,
    attempt: int = 1,
    herdr_session: str | None = None,
    start_worker_agent: Callable[..., tuple[str, str | None, str]] | None = None,
    artifact_manifest: Any | None = None,
) -> tuple[int, dict, dict[str, object]]:
    """Run a worker and return its valid private result without publishing terminal state.

    ``worker_result_path`` is unique to the lease.  Only ``JobLedger.finalize`` may promote it
    to the public result path and emit the terminal state/DONE contract.
    """
    order = load_order(order_path)
    if artifact_manifest is None:
        raise CompletionError("worker lifecycle requires a typed artifact manifest")
    session = _recorded_herdr_session(herdr_session, "worker lifecycle")
    order_id = order["order_id"]
    fell_back = False
    execution_order = {**order, "result_path": str(worker_result_path)}
    worker_pane: str | None = None
    cleanup: dict[str, object] = {
        "status": "not-created",
        "worker_pane": None,
        "verified_absent": True,
    }
    monitor_finalized: threading.Event | None = None
    startup_validated = False
    startup_rejected = False
    launch_recovery_pending = False
    # Direct dispatch runs in the phase leader's environment; never report Recruiter state onto
    # that pane. Resolve the broker's explicit persisted address instead.
    my_pane = _recruiter_pane_from_state()
    _report_state(my_pane, "working", f"hiring for {order_id}", herdr_session=session)
    try:
        # Everything that can fail lives INSIDE the fallback block, now that order_id is known, so
        # a bad roster / launch / Herdr call still writes a blocked result and durable receipt rather
        # than raising past main() and stranding the leader.
        roster = load_roster(roster_path)
        # Each lease writes a private result, so stale recovered workers cannot touch the public
        # result path or a newer lease's staging file.
        worker_result_path.parent.mkdir(parents=True, exist_ok=True)
        worker_result_path.unlink(missing_ok=True)
        effective_instructions = (
            worker_instructions_path
            or worker_result_path.with_name("worker-instructions.md")
        )
        _write_worker_instructions(
            order, worker_result_path, effective_instructions, artifact_manifest
        )
        execution_order["instructions_path"] = str(effective_instructions)
        launch = resolve_launch_command(execution_order, roster)
        management_config = llm_management.load_management_config(roster)
        request_id = lifecycle.request_identity(order)
        # The attempt number keeps a rescue relaunch's agent name distinct from attempt 1's.
        worker_name = _safe_agent_name("upagent", request_id, attempt)
        worker_tab = _worker_tab_role(execution_order)
        start_agent = start_worker_agent or _start_herdr_agent
        worker_pane, workspace_id, worker_address = start_agent(
            worker_name,
            execution_order,
            launch,
            tab_role=worker_tab,
            herdr_session=session,
        )
        if on_worker_launched is not None:
            monitor_finalized = on_worker_launched(
                worker_pane, workspace_id, worker_address
            )
        worker_fraction = _worker_pane_fraction(execution_order)
        if worker_fraction is not None:
            _resize_started_pane(
                worker_pane,
                split_direction="right",
                target_fraction=worker_fraction,
                role="watchdog",
                herdr_session=session,
            )
        # The pane address is durably owned before health validation. A failed launch can
        # therefore be cleaned without guessing, while nobody is told "running" prematurely.
        health = _wait_for_worker_health(
            worker_pane,
            execution_order,
            management_config.startup_timeout_ms,
            roster,
            herdr_session=session,
        )
        if on_worker_healthy is not None:
            on_worker_healthy(health)
        startup_validated = True
        wait_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
        wait_deadline = time.monotonic() + wait_ms / 1000
        timeout_number = 0
        premature_number = 0
        while True:
            try:
                remaining_ms = max(
                    1, math.ceil((wait_deadline - time.monotonic()) * 1000)
                )
                agent_finished = _wait_for_agent_status(
                    worker_pane,
                    remaining_ms,
                    monitor_finalized,
                    herdr_session=session,
                )
            except AgentWaitTimeout as error:
                timeout_number += 1
                if on_timeout is None:
                    raise
                extension_ms = on_timeout(timeout_number, monitor_finalized)
                if extension_ms is None:
                    raise AgentWaitTimeout(
                        f"worker {worker_pane} exceeded its cap and no extension was authorized"
                    ) from error
                wait_deadline = time.monotonic() + extension_ms / 1000
                continue

            # Ordinary workers proceed directly to the completion reactor, which validates the
            # whole bundle and can repair it while the original worker is still addressable.
            # Watchdogs alone need an early semantic check because a syntactically valid result
            # may still be premature relative to their authoritative terminal record.
            if order.get("agent") not in WATCHDOG_AGENTS:
                break
            try:
                result = load_result(worker_result_path, expected_order_id=order_id)
            except ContractError:
                if agent_finished:
                    break
                raise
            premature_reason = _watchdog_terminal_reason(order, result)
            if premature_reason is None:
                break
            premature_number += 1
            archived = _archive_premature_watchdog_result(
                worker_result_path, premature_number
            )
            if monitor_finalized is not None:
                monitor_finalized.clear()
            command_runtime.write_stderr(
                f"recruiter: watchdog {order_id} produced premature result; "
                f"archived {archived}: {premature_reason}\n"
            )
            _submit_agent_prompt(
                worker_address,
                "WATCHDOG_CONTINUE: Your result was not accepted because "
                f"{premature_reason}. Resume monitoring now. Do not write another result "
                "until the authoritative terminal record exists and matches this assignment.",
                idle_timeout_ms=WATCHDOG_CONTINUATION_TIMEOUT_MS,
                herdr_session=session,
            )
    except (RecruiterError, ContractError, KeyError, TypeError, OSError) as e:
        if isinstance(e, FencedLaunchError):
            cleanup = dict(e.cleanup)
            if cleanup.get("verified_absent") is not True:
                # No result or receipt may claim absence. The standing supervisor keeps the
                # active lease and exact launch journal until reconciliation proves cleanup.
                launch_recovery_pending = True
                raise
        # A dedicated manager's explicit refusal is a ruling, not a launch flake — callers
        # must not auto-relaunch against it.
        startup_rejected = isinstance(e, StartupRejectedByManager)
        # A filesystem failure writing the fallback propagates.  Without a valid result, the
        # caller must not publish terminal state or DONE.
        try:
            existing_result = load_result(
                worker_result_path, expected_order_id=order_id
            )
        except ContractError:
            existing_result = None
        preserve_existing = existing_result is not None and _may_preserve_worker_result(
            order, existing_result, startup_validated=startup_validated
        )
        if preserve_existing:
            result = cast(dict, existing_result)
        else:
            if artifact_manifest is None:
                raise CompletionError(
                    "worker fallback has no required typed artifact manifest"
                ) from e
            result = _write_required_blocked_bundle(order, artifact_manifest, str(e))
        fell_back = not preserve_existing
        if fell_back:
            command_runtime.write_stderr(
                f"recruiter: order {order_id} fell back to blocked: {e}\n"
            )
        else:
            command_runtime.write_stderr(
                f"recruiter: order {order_id} kept existing worker result after Recruiter wait fault: {e}\n"
            )
    finally:
        if not launch_recovery_pending and before_worker_cleanup is not None:
            completion_blocked = before_worker_cleanup()
            if completion_blocked is not None:
                fell_back = fell_back or completion_blocked
        if worker_pane is not None:
            try:
                cleanup = _close_worker_pane(worker_pane, herdr_session=session)
            except RecruiterError as e:
                if artifact_manifest is None:
                    raise CompletionError(
                        "worker cleanup failed without the required typed artifact manifest"
                    ) from e
                result = _write_required_blocked_bundle(
                    order, artifact_manifest, f"worker cleanup failed: {e}"
                )
                fell_back = True
                # The failed pane address remains in the active lease for the supervisor to retry.
                cleanup = {
                    "status": "cleanup-failed",
                    "worker_pane": worker_pane,
                    "verified_absent": False,
                    "reason": str(e),
                }
    # Callers use these to tell "launch never became healthy" from "ran and failed",
    # and a manager's explicit refusal from a mechanical launch failure.
    cleanup["startup_validated"] = startup_validated
    cleanup["startup_rejected"] = startup_rejected
    # The completion reactor may have repaired or deterministically replaced the staged result
    # while the original worker was still addressable. Return that authoritative staged value.
    result = load_result(worker_result_path, expected_order_id=order_id)
    final_label = "blocked" if fell_back else "done"
    _report_state(
        my_pane,
        "idle",
        f"last order: {order_id} ({final_label})",
        herdr_session=session,
    )
    return (1 if fell_back else 0), result, cleanup


# Accepted spellings for order fields, first entry canonical. Aliases describe form only;
# values are never changed by this map. Keep the canonical keys in lockstep with parse_order.
ORDER_INTAKE_ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "id"),
    "request_id": ("request_id", "req_id"),
    "phase_id": ("phase_id", "phase"),
    "stage_id": ("stage_id", "stage"),
    "harness": ("harness",),
    "model": ("model",),
    "agent": ("agent", "persona"),
    "effort": ("effort",),
    "cwd": ("cwd", "workdir", "working_directory", "dir"),
    "instructions_path": (
        "instructions_path",
        "instructions",
        "brief",
        "brief_path",
        "prompt_path",
        "prompt_file",
    ),
    "result_path": ("result_path", "result", "result_file", "output_path", "output"),
    "cockpit_pane": ("cockpit_pane", "pane", "cockpit"),
    "timeout_ms": ("timeout_ms", "timeout"),
    "env": ("env",),
    "requester": ("requester",),
    "management": ("management",),
    "artifact_publication": ("artifact_publication",),
    "mode": ("mode",),
    "plan_id": ("plan_id",),
    "step_id": ("step_id",),
    "operation": ("operation",),
    "requires_apply": ("requires_apply",),
    "manager_placement": ("manager_placement",),
    "approval": ("approval",),
    "plan_artifact": ("plan_artifact",),
    "watchdog_terminal": ("watchdog_terminal",),
}
_SAFE_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ORDER_ENVELOPE_KEYS = frozenset(
    ("payload", "order", "request", "work_order", "data", "body")
)
ORDER_INTAKE_NEVER_INVENTED = (
    "harness",
    "agent",
    "cwd",
    "instructions_path",
    "cockpit_pane",
)
# Any explicit value in these fields is execution intent. A clerk may move it to the canonical
# key, but Python proves the exact value came from the submitted bytes and was not omitted.
ORDER_INTAKE_PROTECTED = (
    "order_id",
    "request_id",
    "phase_id",
    "stage_id",
    "harness",
    "model",
    "agent",
    "effort",
    "cwd",
    "instructions_path",
    "result_path",
    "cockpit_pane",
    "timeout_ms",
    "env",
    "requester",
    "management",
    "artifact_publication",
    "mode",
    "plan_id",
    "step_id",
    "operation",
    "requires_apply",
    "manager_placement",
    "approval",
    "plan_artifact",
    "watchdog_terminal",
)
_ORDER_KNOWN_ALIASES = frozenset(
    alias for aliases in ORDER_INTAKE_ALIASES.values() for alias in aliases
)

# Every distinct submission starts one fresh intake clerk (a byte-identical resubmission reuses a
# clean prior attempt instead). When that clerk's machine-readable answer fails Python's provenance
# or contract checks, the same role gets those findings back this many more times before the
# request ends as an intake-clerk failure.
INTAKE_CORRECTION_LIMIT = 2
INTAKE_ATTEMPT_LIMIT = INTAKE_CORRECTION_LIMIT + 1

# The four visible outcomes of a submission: acceptance (exit 0, printed by each door), then one
# standardized non-accepted outcome per marker. A caller can branch on the exit code alone.
INTAKE_OUTCOMES: dict[str, tuple[str, int]] = {
    "blocked": ("REQUEST_BLOCKED", 3),
    "intake-clerk-failure": ("REQUEST_INTAKE_FAILED", 4),
    "infrastructure-failure": ("REQUEST_INFRASTRUCTURE_FAILED", 5),
}


class IntakeOutcomeError(RecruiterError):
    """One non-accepted request outcome, its durable evidence, and its exit code.

    Only three things may end a received request: the intake clerk authored a block, the intake
    clerk could not answer, or the Hub's own machinery failed. A request's schema, wording, order
    id, agent name, or intent never ends it here.
    """

    def __init__(
        self,
        outcome: str,
        order_path: str,
        reason: str,
        *,
        evidence: dict[str, str],
        missing: Sequence[str] = (),
        understood: Sequence[str] = (),
        errors: Sequence[str] = (),
        attempts: int = 0,
    ) -> None:
        marker, exit_code = INTAKE_OUTCOMES[outcome]
        super().__init__(
            f"request {order_path} could not be taken in: {reason}"
            if outcome == "infrastructure-failure"
            else _intake_refusal_message(order_path, reason, list(missing))
        )
        self.outcome = outcome
        self.marker = marker
        self.exit_code = exit_code
        self.order_path = order_path
        self.reason = reason
        self.evidence = dict(evidence)
        self.missing = list(missing)
        self.understood = list(understood)
        self.errors = list(errors) or [reason]
        self.attempts = attempts

    def payload(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "authored_by": _intake_author(self.outcome),
            "errors": self.errors,
            "evidence": self.evidence,
            "missing": self.missing,
            "order_path": self.order_path,
            "outcome": self.outcome,
            "reason": self.reason,
            "understood": self.understood,
        }


def _intake_author(outcome: str) -> str:
    """Who ended the request. Only a block is the clerk's own words."""
    return "intake-clerk" if outcome == "blocked" else "recruiter"


def _print_intake_outcome(outcome: IntakeOutcomeError) -> None:
    """One machine-readable line naming the outcome and every durable evidence path."""
    print(
        f"{outcome.marker} {json.dumps(outcome.payload(), sort_keys=True)}", flush=True
    )


def _intake_artifact_paths(order_path: Path) -> dict[str, Path]:
    return {
        "raw": order_path.with_name(order_path.name + ".raw-submitted"),
        "interpreted": order_path.with_name(order_path.name + ".interpreted.json"),
        "intake": order_path.with_name(order_path.name + ".intake.json"),
        "validation": order_path.with_name(order_path.name + ".validation.json"),
        "refusal": order_path.with_name(order_path.name + ".refusal.json"),
    }


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


_INTAKE_DIRECTORY_MODE = 0o700
_INTAKE_FILE_MODE = 0o600
_INTAKE_ATTEMPT_NAME_RE = re.compile(r"^attempt-[A-Za-z0-9_-]{8,}$")


def _open_private_directory(
    path: Path, *, create: bool = False, repair_mode: bool = False
) -> int:
    """Open one broker-owned directory without following a symlink."""
    if create:
        try:
            os.mkdir(path, _INTAKE_DIRECTORY_MODE)
        except FileExistsError:
            pass
        except OSError as error:
            raise RecruiterError(
                f"could not create private intake directory {path}: {error}"
            ) from error
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise RecruiterError(
            f"intake path {path} must be a real broker-owned directory: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise RecruiterError(
                f"intake path {path} is not a directory owned by the current user"
            )
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != _INTAKE_DIRECTORY_MODE:
            if not repair_mode:
                raise RecruiterError(
                    f"intake directory {path} has mode {mode:o}; expected 700"
                )
            os.fchmod(descriptor, _INTAKE_DIRECTORY_MODE)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_intake_layout() -> dict[str, Path]:
    """Create and verify deterministic broker roots; attempt directories remain random."""
    parent = Path(os.path.abspath(STATE_FILE.parent))
    if not parent.is_dir():
        raise RecruiterError(f"Recruiter state directory does not exist: {parent}")
    root = parent / "intake"
    paths = {
        "root": root,
        "attempts": root / "attempts",
        "index": root / "index",
        "locks": root / "locks",
    }
    for path in paths.values():
        descriptor = _open_private_directory(path, create=True, repair_mode=True)
        os.close(descriptor)
    return paths


def _secure_file_bytes(path: Path) -> bytes:
    parent_descriptor = _open_private_directory(path.parent)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            raise RecruiterError(
                f"secure intake file {path} is unreadable: {error}"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RecruiterError(
                    f"secure intake file {path} is not a regular owned file"
                )
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RecruiterError(
                    f"secure intake file {path} is accessible outside its owner"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _secure_json(path: Path) -> dict:
    try:
        value = json.loads(_secure_file_bytes(path))
    except json.JSONDecodeError as error:
        raise RecruiterError(
            f"secure intake JSON {path} is invalid: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RecruiterError(f"secure intake JSON {path} must be an object")
    return value


def _secure_file_exists(path: Path) -> bool:
    parent_descriptor = _open_private_directory(path.parent)
    try:
        try:
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise RecruiterError(
                f"could not inspect secure intake file {path}: {error}"
            ) from error
        return True
    finally:
        os.close(parent_descriptor)


def _secure_write_bytes(path: Path, value: bytes) -> None:
    parent_descriptor = _open_private_directory(path.parent)
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            _INTAKE_FILE_MODE,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except OSError as error:
        raise RecruiterError(
            f"could not write secure intake file {path}: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.close(parent_descriptor)


def _secure_write_json(path: Path, value: dict) -> None:
    _secure_write_bytes(
        path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    )


def _secure_write_text(path: Path, value: str) -> None:
    _secure_write_bytes(path, value.encode())


@contextmanager
def _intake_attempt_lock(key: str) -> Iterator[None]:
    layout = _prepare_intake_layout()
    lock_directory = _open_private_directory(layout["locks"])
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                f"{key}.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                _INTAKE_FILE_MODE,
                dir_fd=lock_directory,
            )
        except OSError as error:
            raise RecruiterError(
                f"could not open secure intake lock for {key}: {error}"
            ) from error
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise RecruiterError(f"intake lock for {key} is not a regular owned file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(lock_directory)


def _raw_unknown_fields(raw_text: str) -> list[str]:
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError:
        return []
    known = {alias for aliases in ORDER_INTAKE_ALIASES.values() for alias in aliases}
    unknown: list[str] = []

    def walk(value: object, prefix: str = "") -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, prefix)
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if key in known:
                continue  # nested members belong to this recognized typed field
            if key in _ORDER_ENVELOPE_KEYS:
                walk(child, path)
                continue
            unknown.append(path)
            if isinstance(child, (dict, list)):
                walk(child, path)

    walk(document)
    return sorted(set(unknown))


def _complete_order_form(
    candidate: dict, raw_text: str, order_path: Path
) -> tuple[dict, list[str]]:
    """Apply only Python-owned bookkeeping/path defaults, then run the strict contract."""
    unknown = set(candidate) - set(ORDER_INTAKE_ALIASES)
    if unknown:
        raise ContractError(
            "order.json: interpreted order has unknown fields: "
            + ", ".join(sorted(unknown))
        )
    order = dict(candidate)
    changes: list[str] = []
    order_dir = order_path.resolve().parent

    order_id = order.get("order_id")
    if (
        isinstance(order_id, str)
        and order_id
        and not _SAFE_ORDER_ID_RE.fullmatch(order_id)
    ):
        if order_id.startswith("consult-"):
            # NOT specialist vocabulary — a laundering guard. An unsafe order id claiming the
            # consult door's minted identity is REFUSED, never silently renamed into a valid
            # one. `_SAFE_ORDER_ID_RE` already excludes `/`; this guard's contribution is
            # refusing rather than repairing. Pinned by recruiter_test.py.
            raise ContractError(
                "order.json: unsafe consult-shaped order_id cannot be regenerated"
            )
        order.pop("order_id")
        changes.append(f"regenerated unsafe order_id {order_id!r}")
    if not isinstance(order.get("order_id"), str) or not order["order_id"]:
        order["order_id"] = (
            f"intake-{hashlib.sha256(raw_text.encode()).hexdigest()[:12]}"
        )
        changes.append(f"generated order_id {order['order_id']}")
    if not isinstance(order.get("phase_id"), str) or not order["phase_id"]:
        order["phase_id"] = "intake-adhoc"
        changes.append("defaulted missing phase_id to intake-adhoc")
    if "stage_id" not in order:
        order["stage_id"] = "stage-5-finalization"
        changes.append("defaulted missing stage_id to stage-5-finalization")
    if "model" not in order:
        order["model"] = ""
        changes.append(
            "defaulted unspecified model to the harness-resolved empty value"
        )

    for field in ("cwd", "instructions_path"):
        value = order.get(field)
        if isinstance(value, str) and value and not Path(value).is_absolute():
            order[field] = str(order_dir / value)
            changes.append(f"anchored relative {field} at {order[field]}")
    result = order.get("result_path")
    if not isinstance(result, str) or not result:
        order["result_path"] = str(order_dir / f"{order['order_id']}-result.json")
        changes.append(f"defaulted result_path to {order['result_path']}")
    elif not Path(result).is_absolute():
        order["result_path"] = str(order_dir / result)
        changes.append(f"anchored relative result_path at {order['result_path']}")
    timeout = order.get("timeout_ms")
    if isinstance(timeout, str) and timeout.isdigit():
        order["timeout_ms"] = int(timeout)
        changes.append("coerced decimal timeout_ms to an integer")

    contracts.parse_order(json.dumps(order))
    return order, changes


def _nested_field_values(value: object, aliases: tuple[str, ...]) -> list[object]:
    """Collect every value keyed by these aliases. A recognized field owns its own subtree — the
    `mode` inside `manager_placement` and the `id` inside `requester` are that object's members,
    never a second top-level declaration — matching how _raw_unknown_fields walks the same JSON."""
    found: list[object] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_nested_field_values(item, aliases))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in aliases:
                found.append(child)
            elif key not in _ORDER_KNOWN_ALIASES:
                found.extend(_nested_field_values(child, aliases))
    return found


def _raw_field_values(raw_text: str, canonical: str) -> list[object]:
    """Return structurally keyed JSON values only; prose is never execution authority."""
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError:
        return []
    if not isinstance(document, dict):
        return []
    return _nested_field_values(document, ORDER_INTAKE_ALIASES[canonical])


def _intake_field_summary(raw_text: str) -> tuple[list[str], list[str]]:
    """Name safely understood required intent and the values still absent or ambiguous."""
    understood: list[str] = []
    missing: list[str] = []
    for field in ORDER_INTAKE_NEVER_INVENTED:
        values = _raw_field_values(raw_text, field)
        distinct: list[object] = []
        for value in values:
            if value not in distinct:
                distinct.append(value)
        if len(distinct) == 1 and isinstance(distinct[0], str) and distinct[0]:
            understood.append(f"{field}={distinct[0]}")
        else:
            missing.append(field)
    return understood, missing


def _clerk_provenance_errors(raw_text: str, candidate: dict) -> list[str]:
    errors: list[str] = []
    unknown_candidate = sorted(set(candidate) - set(ORDER_INTAKE_ALIASES))
    if unknown_candidate:
        errors.append(
            "interpreted order contains unknown fields: " + ", ".join(unknown_candidate)
        )
    raw_unknown = _raw_unknown_fields(raw_text)
    if raw_unknown:
        errors.append(
            "submission contains unclassified fields: " + ", ".join(raw_unknown)
        )

    for field in ORDER_INTAKE_PROTECTED:
        supplied = _raw_field_values(raw_text, field)
        if supplied and field not in candidate:
            errors.append(f"clerk dropped explicit {field}")
            continue
        if field not in candidate:
            continue
        value = candidate[field]
        if field == "model" and value == "" and not supplied:
            continue  # Python's only execution-profile default: no model selection was made.
        if not supplied:
            errors.append(f"clerk invented {field}")
            continue
        distinct: list[object] = []
        for item in supplied:
            if item not in distinct:
                distinct.append(item)
        if len(distinct) > 1:
            errors.append(f"submission has conflicting values for {field}")
        elif (
            field == "timeout_ms"
            and isinstance(distinct[0], str)
            and distinct[0].isdigit()
            and isinstance(value, int)
            and not isinstance(value, bool)
            and int(distinct[0]) == value
        ):
            continue  # lossless form coercion, not a deadline change
        elif value != distinct[0]:
            errors.append(f"clerk changed {field}")
    return errors


def _intake_record(
    *,
    mode: str,
    raw_path: Path,
    interpreted_path: Path,
    changes: list[str],
    unknown_fields: list[str],
    attempts: int,
    clerk: dict | None = None,
) -> dict:
    return {
        "at_ns": time.time_ns(),
        "mode": mode,
        "attempts": attempts,
        "raw_path": str(raw_path),
        "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "interpreted_path": str(interpreted_path),
        "interpreted_sha256": hashlib.sha256(interpreted_path.read_bytes()).hexdigest(),
        "changes": changes,
        "unknown_fields": unknown_fields,
        **({"clerk": clerk} if clerk is not None else {}),
    }


def _intake_evidence(
    paths: dict[str, Path], clerk: dict | None = None
) -> dict[str, str]:
    """Every durable path a human or agent can open after a non-accepted outcome."""
    evidence = {name: str(path) for name, path in paths.items()}
    for field in ("brief_path", "output_path", "ownership_path"):
        value = (clerk or {}).get(field)
        if isinstance(value, str):
            evidence[f"clerk_{field.removesuffix('_path')}"] = value
    return evidence


def _persist_intake_success(
    order_path: Path,
    paths: dict[str, Path],
    order: dict,
    *,
    changes: list[str],
    unknown_fields: list[str],
    clerk: dict | None,
    attempts: int,
) -> None:
    """Commit the full paper trail before atomically replacing the caller's order."""
    paths["refusal"].unlink(missing_ok=True)
    JobLedger._write_json(paths["interpreted"], order)
    JobLedger._write_json(
        paths["intake"],
        _intake_record(
            mode="intake-clerk",
            raw_path=paths["raw"],
            interpreted_path=paths["interpreted"],
            changes=changes,
            unknown_fields=unknown_fields,
            attempts=attempts,
            clerk=clerk,
        ),
    )
    JobLedger._write_json(
        paths["validation"],
        {
            "at_ns": time.time_ns(),
            "attempts": attempts,
            "authored_by": "intake-clerk",
            "valid": True,
            "errors": [],
        },
    )
    JobLedger._write_json(order_path, order)


def _persist_intake_refusal(
    paths: dict[str, Path],
    *,
    outcome: str,
    reason: str,
    missing: list[str],
    understood: list[str],
    candidate: dict | None,
    unknown_fields: list[str],
    clerk: dict | None,
    attempts: int,
    errors: list[str] | None = None,
) -> None:
    authored_by = _intake_author(outcome)
    JobLedger._write_json(
        paths["interpreted"], candidate if candidate is not None else {"order": None}
    )
    JobLedger._write_json(
        paths["intake"],
        _intake_record(
            mode="intake-clerk-refusal"
            if outcome == "blocked"
            else "intake-clerk-failure",
            raw_path=paths["raw"],
            interpreted_path=paths["interpreted"],
            changes=[],
            unknown_fields=unknown_fields,
            attempts=attempts,
            clerk=clerk,
        ),
    )
    validation_errors = errors or [reason]
    JobLedger._write_json(
        paths["validation"],
        {
            "at_ns": time.time_ns(),
            "attempts": attempts,
            "authored_by": authored_by,
            "valid": False,
            "errors": validation_errors,
        },
    )
    JobLedger._write_json(
        paths["refusal"],
        {
            "at_ns": time.time_ns(),
            "attempts": attempts,
            "authored_by": authored_by,
            "error": reason,
            "missing": missing,
            "understood": understood,
        },
    )


def _new_intake_attempt(layout: dict[str, Path]) -> Path:
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=layout["attempts"]))
    os.chmod(attempt, _INTAKE_DIRECTORY_MODE)
    descriptor = _open_private_directory(attempt)
    os.close(descriptor)
    return attempt


def _validated_attempt_directory(attempts_root: Path, attempt_name: object) -> Path:
    if (
        not isinstance(attempt_name, str)
        or not _INTAKE_ATTEMPT_NAME_RE.fullmatch(attempt_name)
        or Path(attempt_name).name != attempt_name
    ):
        raise RecruiterError("intake index contains an invalid attempt directory name")
    attempt = attempts_root / attempt_name
    if attempt.parent != attempts_root:
        raise RecruiterError("intake attempt escaped the trusted attempts root")
    descriptor = _open_private_directory(attempt)
    os.close(descriptor)
    return attempt


def _process_start_time(pid: object) -> str | None:
    """Linux process birth identity. A PID without the same start tick is a different owner."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2 :].split()
    # The suffix begins at field 3 (state); starttime is field 22.
    return fields[19] if len(fields) > 19 else None


def _same_owner_process(ownership: dict) -> bool:
    pid = ownership.get("owner_pid")
    recorded_start = ownership.get("owner_start_time")
    return (
        isinstance(recorded_start, str)
        and bool(recorded_start)
        and _process_start_time(pid) == recorded_start
    )


def _intake_clerk_agent_name(intake_key: str, lease_token: str) -> str:
    return _safe_agent_name(
        "upagent-intake", f"{intake_key[:16]}-{lease_token[:16]}", 1
    )


def _load_reusable_intake_clerk_response(
    layout: dict[str, Path], intake_key: str
) -> tuple[object, dict] | None:
    index_path = layout["index"] / f"{intake_key}.json"
    if not _secure_file_exists(index_path):
        return None
    index = _secure_json(index_path)
    if index.get("schema_version") != 1 or index.get("intake_key") != intake_key:
        raise RecruiterError(
            "intake reuse index has the wrong schema or request identity"
        )
    attempt = _validated_attempt_directory(
        layout["attempts"], index.get("attempt_name")
    )
    ownership_path = attempt / "ownership.json"
    ownership = _secure_json(ownership_path)
    lease_token = index.get("lease_token")
    if (
        not isinstance(lease_token, str)
        or not lease_token
        or ownership.get("lease_token") != lease_token
        or ownership.get("intake_key") != intake_key
        or ownership.get("attempt_name") != attempt.name
        or ownership.get("agent_name")
        != _intake_clerk_agent_name(intake_key, lease_token)
    ):
        raise RecruiterError("intake reuse index does not match its ownership journal")
    cleanup = ownership.get("cleanup")
    if (
        ownership.get("state") != "closed"
        or not isinstance(cleanup, dict)
        or cleanup.get("verified_absent") is not True
    ):
        raise RecruiterError("intake reuse index points at an unclean clerk attempt")
    response_path = attempt / "response.json"
    response_bytes = _secure_file_bytes(response_path)
    response_sha256 = hashlib.sha256(response_bytes).hexdigest()
    if (
        index.get("response_sha256") != response_sha256
        or ownership.get("response_sha256") != response_sha256
    ):
        raise RecruiterError("intake reuse response hash does not match its journal")
    try:
        response = lifecycle.parse_intake_clerk_response(response_bytes.decode())
    except (UnicodeDecodeError, LifecycleError) as error:
        raise RecruiterError(
            f"secure intake response {response_path} is invalid: {error}"
        ) from error
    return response, {
        "attempt": 1,
        "reused": True,
        "attempt_name": attempt.name,
        "brief_path": str(attempt / "brief.md"),
        "output_path": str(response_path),
        "ownership_path": str(ownership_path),
        "cleanup": cleanup,
    }


def _matching_intake_process(
    process_info: object, expected_process: str
) -> dict | None:
    processes = (
        process_info.get("foreground_processes", [])
        if isinstance(process_info, dict)
        else []
    )
    return next(
        (
            process
            for process in processes
            if isinstance(process, dict)
            and (
                process.get("name") == expected_process
                or expected_process in str(process.get("cmdline", ""))
                or expected_process
                in " ".join(str(item) for item in process.get("argv", []))
            )
        ),
        None,
    )


def _resolve_intake_clerk_identity(ownership: dict) -> dict[str, object]:
    """Resolve a clerk by its unguessable agent name; a recorded pane id is never authority."""
    intake_key = ownership.get("intake_key")
    lease_token = ownership.get("lease_token")
    agent_name = ownership.get("agent_name")
    if (
        not isinstance(intake_key, str)
        or not isinstance(lease_token, str)
        or not isinstance(agent_name, str)
        or agent_name != _intake_clerk_agent_name(intake_key, lease_token)
    ):
        return {"status": "blocked", "reason": "invalid intake lease identity"}
    try:
        herdr_session = _recorded_herdr_session(
            ownership.get("herdr_session"), "intake cleanup"
        )
    except RecruiterError as error:
        return {"status": "blocked", "reason": str(error)}
    try:
        agent = (
            _herdr_json("agent", "get", agent_name, herdr_session=herdr_session)
            .get("result", {})
            .get("agent")
        )
    except RecruiterError as error:
        if "agent_not_found" in str(error):
            return {"status": "absent", "agent_name": agent_name}
        return {"status": "blocked", "reason": f"agent lookup failed: {error}"}
    if not isinstance(agent, dict) or not agent:
        return {"status": "absent", "agent_name": agent_name}
    if agent.get("name") != agent_name:
        return {
            "status": "blocked",
            "reason": "Herdr agent name does not match the lease",
        }
    pane_id = agent.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        return {"status": "absent", "agent_name": agent_name}
    recorded_pane = ownership.get("pane")
    if isinstance(recorded_pane, str) and recorded_pane and recorded_pane != pane_id:
        return {
            "status": "blocked",
            "reason": "recorded pane no longer belongs to the intake agent name",
            "pane": pane_id,
        }
    try:
        pane = (
            _herdr_json("pane", "get", pane_id, herdr_session=herdr_session)
            .get("result", {})
            .get("pane", {})
        )
        process_info = (
            _herdr_json(
                "pane", "process-info", "--pane", pane_id, herdr_session=herdr_session
            )
            .get("result", {})
            .get("process_info", {})
        )
    except RecruiterError as error:
        return {"status": "blocked", "reason": f"pane identity lookup failed: {error}"}
    if not isinstance(pane, dict) or pane.get("pane_id", pane_id) != pane_id:
        return {
            "status": "blocked",
            "reason": "Herdr pane identity changed during cleanup",
        }
    expected_agent = ownership.get("expected_agent")
    expected_process = ownership.get("expected_process")
    expected_cwd = ownership.get("expected_cwd")
    detected_agent = pane.get("agent")
    cwd = pane.get("foreground_cwd", pane.get("cwd"))
    if (
        not isinstance(expected_agent, str)
        or detected_agent != expected_agent
        or not isinstance(expected_cwd, str)
        or not isinstance(cwd, str)
        or os.path.realpath(cwd) != os.path.realpath(expected_cwd)
    ):
        return {
            "status": "blocked",
            "reason": "pane agent or cwd does not match the intake ownership journal",
            "pane": pane_id,
        }
    current_process = (
        _matching_intake_process(process_info, expected_process)
        if isinstance(expected_process, str)
        else None
    )
    health = ownership.get("health")
    healthy_record: dict | None = None
    if isinstance(health, dict) and (
        health.get("healthy") is True
        and health.get("pane_id") == pane_id
        and health.get("detected_agent") == expected_agent
        and health.get("cwd_matches") is True
    ):
        healthy_record = health
    previously_healthy = healthy_record is not None
    if current_process is not None and healthy_record is not None:
        recorded_process_pid = healthy_record.get("process_pid")
        recorded_process_start = healthy_record.get("process_start_time")
        current_process_pid = current_process.get("pid")
        if (
            isinstance(recorded_process_pid, int)
            and isinstance(recorded_process_start, str)
            and (
                current_process_pid != recorded_process_pid
                or _process_start_time(current_process_pid) != recorded_process_start
            )
        ):
            return {
                "status": "blocked",
                "reason": "intake foreground process identity no longer matches its lease",
                "pane": pane_id,
            }
    if current_process is None and not previously_healthy:
        return {
            "status": "blocked",
            "reason": "expected intake process was never verified in the owned pane",
            "pane": pane_id,
        }
    return {
        "status": "owned",
        "agent_name": agent_name,
        "pane": pane_id,
        "current_process": current_process,
        "previously_healthy": previously_healthy,
    }


def _cleanup_intake_clerk(ownership: dict) -> dict[str, object]:
    identity = _resolve_intake_clerk_identity(ownership)
    if identity.get("status") == "absent":
        recorded_pane = ownership.get("pane")
        if ownership.get("state") != "closed" and not (
            isinstance(recorded_pane, str) and recorded_pane
        ):
            # `herdr agent start` is a separate socket transaction. The caller can die after
            # sending it but before Herdr publishes the named agent. One not-found lookup cannot
            # prove that no pane will appear later, so this tiny journal stays open and is
            # rechecked by every reconciliation sweep.
            return {
                "status": "launch-uncertain",
                "worker_pane": None,
                "verified_absent": False,
                "agent_name": ownership.get("agent_name"),
                "reason": "named intake launch may still complete after owner loss",
            }
        return {
            "status": "already-absent",
            "worker_pane": recorded_pane,
            "verified_absent": True,
            "agent_name": ownership.get("agent_name"),
        }
    if identity.get("status") != "owned":
        return {
            "status": "cleanup-blocked",
            "worker_pane": ownership.get("pane"),
            "verified_absent": False,
            "agent_name": ownership.get("agent_name"),
            "reason": identity.get("reason", "intake identity could not be verified"),
        }
    # Resolve twice immediately before close. A stale pane id from the journal is never used.
    confirmed = _resolve_intake_clerk_identity(ownership)
    if confirmed.get("status") != "owned" or confirmed.get("pane") != identity.get(
        "pane"
    ):
        return {
            "status": "cleanup-blocked",
            "worker_pane": identity.get("pane"),
            "verified_absent": False,
            "agent_name": ownership.get("agent_name"),
            "reason": "intake identity changed immediately before cleanup",
        }
    pane = cast(str, confirmed["pane"])
    herdr_session = _recorded_herdr_session(
        ownership.get("herdr_session"), "intake cleanup"
    )
    try:
        cleanup = _close_worker_pane(pane, herdr_session=herdr_session)
    except RecruiterError as error:
        return {
            "status": "cleanup-failed",
            "worker_pane": pane,
            "verified_absent": False,
            "agent_name": ownership.get("agent_name"),
            "reason": str(error),
        }
    after = _resolve_intake_clerk_identity(ownership)
    if after.get("status") != "absent":
        return {
            "status": "cleanup-blocked",
            "worker_pane": pane,
            "verified_absent": False,
            "agent_name": ownership.get("agent_name"),
            "reason": "intake agent name still resolves after pane close",
        }
    return {**cleanup, "agent_name": ownership.get("agent_name")}


def _record_started_intake_clerk(
    ownership_path: Path,
    ownership: dict,
    pane: str,
    workspace_id: str | None,
    address: str,
) -> None:
    """CAS the pre-launch intake journal to started before any pane is trusted."""
    current = _secure_json(ownership_path)
    if (
        current.get("state") != "launching"
        or current.get("lease_token") != ownership.get("lease_token")
        or current.get("agent_name") != ownership.get("agent_name")
    ):
        raise RecruiterError("intake launch journal changed before pane commit")
    ownership.update(
        {
            "address": address,
            "pane": pane,
            "started_at_ns": time.time_ns(),
            "state": "started",
            "workspace_id": workspace_id,
        }
    )
    _secure_write_json(ownership_path, ownership)


def _run_order_intake_clerk(
    raw_text: str,
    raw_path: Path,
    roster_path: str,
    intake_key: str,
    *,
    attempt_number: int = 1,
    unknown_fields: Sequence[str] = (),
    correction: dict | None = None,
) -> tuple[Any, dict]:
    """Launch one journaled support clerk, or reuse a clean prior attempt's validated response for a
    byte-identical resubmission (same `intake_key`); malformed caller data never controls its launch."""
    recruiter_pane = _recruiter_pane_from_state()
    if recruiter_pane is None:
        raise RecruiterError(
            f"no live Recruiter state at {STATE_FILE}; run the Hub's up command first"
        )
    roster = load_roster(roster_path)
    config = llm_management.load_management_config(roster)
    layout = _prepare_intake_layout()
    reused = _load_reusable_intake_clerk_response(layout, intake_key)
    if reused is not None:
        return reused[0], {**reused[1], "attempt": attempt_number}

    attempt = _new_intake_attempt(layout)
    output_path = attempt / "response.json"
    brief_path = attempt / "brief.md"
    ownership_path = attempt / "ownership.json"
    cwd = str(attempt)
    _secure_write_text(
        brief_path,
        llm_management.intake_clerk_brief(
            raw_text,
            raw_path,
            output_path,
            attempt=attempt_number,
            attempt_limit=INTAKE_ATTEMPT_LIMIT,
            unknown_fields=unknown_fields,
            correction=correction,
        ),
    )
    command = llm_management.render_intake_clerk_command(
        config.intake_clerk, brief_path, cwd, output_path
    )
    lease_token = uuid.uuid4().hex
    agent_name = _intake_clerk_agent_name(intake_key, lease_token)
    herdr_session = _resolve_current_herdr_session_name()
    owner_start_time = _process_start_time(os.getpid())
    if owner_start_time is None:
        raise RecruiterError(
            "could not record the intake owner's process start identity"
        )
    ownership: dict[str, object] = {
        "schema_version": 1,
        "intake_key": intake_key,
        "attempt_name": attempt.name,
        "lease_token": lease_token,
        "agent_name": agent_name,
        "owner_pid": os.getpid(),
        "owner_start_time": owner_start_time,
        "expected_agent": config.intake_clerk.expected_agent,
        "expected_process": config.intake_clerk.expected_process,
        "expected_cwd": cwd,
        "herdr_session": herdr_session,
        "expires_at": int(time.time())
        + max(
            1,
            (config.startup_timeout_ms + config.intake_clerk.timeout_ms) // 1000 + 30,
        ),
        "pane": None,
        "state": "launching",
    }
    # This journal exists before Herdr is asked to create anything. A crash at any later point
    # can resolve the unguessable agent name even when no pane id was written.
    _secure_write_json(ownership_path, ownership)

    clerk_order = {"cockpit_pane": recruiter_pane, "cwd": cwd}
    pane: str | None = None
    cleanup: dict[str, object] = {
        "status": "launch-uncertain",
        "worker_pane": None,
        "verified_absent": False,
        "agent_name": agent_name,
    }
    response: object | None = None
    failure: BaseException | None = None
    launch_attempted = False
    try:
        launch_attempted = True
        pane, workspace_id, address = _start_herdr_agent(
            agent_name,
            clerk_order,
            command,
            split_direction="down",
            tab_role="oversight",
            herdr_session=herdr_session,
        )
        ownership["pane"] = (
            pane  # local cleanup remains possible if journal update fails
        )
        try:
            _record_started_intake_clerk(
                ownership_path, ownership, pane, workspace_id, address
            )
        except (RecruiterError, OSError):
            compensation = _close_worker_pane(pane, herdr_session=herdr_session)
            ownership.update({"cleanup": compensation, "state": "closed"})
            _secure_write_json(ownership_path, ownership)
            pane = None
            raise
        _resize_started_pane(
            pane,
            split_direction="down",
            target_fraction=SUPPORT_PANE_FRACTION,
            role="intake clerk",
            herdr_session=herdr_session,
        )
        health = _wait_for_agent_health(
            pane,
            expected_agent=config.intake_clerk.expected_agent,
            expected_process=config.intake_clerk.expected_process,
            expected_cwd=cwd,
            timeout_ms=config.startup_timeout_ms,
            herdr_session=herdr_session,
        )
        process_pid = health.get("process_pid")
        health["process_start_time"] = _process_start_time(process_pid)
        ownership.update({"health": health, "state": "active"})
        _secure_write_json(ownership_path, ownership)
        response = _wait_typed_file(
            output_path,
            config.intake_clerk.timeout_ms,
            lifecycle.parse_intake_clerk_response,
        )
    except (
        RecruiterError,
        LifecycleError,
        ManagementConfigError,
        OSError,
        UnicodeDecodeError,
    ) as error:
        failure = error
    finally:
        if pane is not None:
            if not isinstance(ownership.get("health"), dict):
                try:
                    cleanup_health = _wait_for_agent_health(
                        pane,
                        expected_agent=config.intake_clerk.expected_agent,
                        expected_process=config.intake_clerk.expected_process,
                        expected_cwd=cwd,
                        timeout_ms=config.startup_timeout_ms,
                        herdr_session=herdr_session,
                    )
                except RecruiterError as error:
                    if failure is None:
                        failure = error
                else:
                    cleanup_health["process_start_time"] = _process_start_time(
                        cleanup_health.get("process_pid")
                    )
                    ownership["health"] = cleanup_health
            cleanup = _cleanup_intake_clerk(ownership)
            if cleanup.get("verified_absent") is not True and failure is None:
                failure = RecruiterError(
                    str(cleanup.get("reason", "clerk cleanup failed"))
                )
        elif launch_attempted:
            # Herdr may have created the named agent before a transport/parsing error prevented
            # _start_herdr_agent from returning its pane. Resolve the pre-journaled name now;
            # reconciliation uses the same path if this process dies first.
            cleanup = _cleanup_intake_clerk(ownership)
            if cleanup.get("verified_absent") is not True and failure is None:
                failure = RecruiterError(
                    str(cleanup.get("reason", "uncertain clerk launch cleanup failed"))
                )
        else:
            cleanup = {
                "status": "not-created",
                "worker_pane": None,
                "verified_absent": True,
                "agent_name": agent_name,
            }
        ownership.update(
            {
                "cleanup": cleanup,
                "state": (
                    "closed"
                    if cleanup.get("verified_absent")
                    else "launch-uncertain"
                    if cleanup.get("status") == "launch-uncertain"
                    else "cleanup-failed"
                ),
            }
        )
        try:
            _secure_write_json(ownership_path, ownership)
        except RecruiterError as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise RecruiterError(f"intake clerk failed: {failure}") from failure
    assert response is not None

    response_bytes = _secure_file_bytes(output_path)
    response_sha256 = hashlib.sha256(response_bytes).hexdigest()
    # Parse the securely opened bytes again; the wait path is only an arrival accelerator.
    try:
        response = lifecycle.parse_intake_clerk_response(response_bytes.decode())
    except (UnicodeDecodeError, LifecycleError) as error:
        raise RecruiterError(
            f"secure intake response {output_path} is invalid: {error}"
        ) from error
    ownership["response_sha256"] = response_sha256
    _secure_write_json(ownership_path, ownership)
    _secure_write_json(
        layout["index"] / f"{intake_key}.json",
        {
            "schema_version": 1,
            "intake_key": intake_key,
            "attempt_name": attempt.name,
            "lease_token": lease_token,
            "response_sha256": response_sha256,
        },
    )
    return response, {
        "attempt": attempt_number,
        "attempt_name": attempt.name,
        "brief_path": str(brief_path),
        "output_path": str(output_path),
        "ownership_path": str(ownership_path),
        "cleanup": cleanup,
    }


def _intake_refusal_message(
    order_path: str, reason: str, missing: list[str] | None = None
) -> str:
    suffix = f" Missing: {', '.join(missing)}." if missing else ""
    detail = reason.rstrip().removesuffix(
        "."
    )  # the clerk writes sentences; keep one full stop
    return (
        f"invalid order {order_path}: {detail}.{suffix} One fresh intake clerk read the exact "
        "submitted bytes and could not produce an executable order. It will not invent or change "
        + ", ".join(ORDER_INTAKE_NEVER_INVENTED)
        + ", target model/effort, requester identity, or operation/apply authority. "
        "Add the named missing values and resubmit."
    )


def _intake_attempt_key(
    submission_key: str, attempt: int, correction: dict | None
) -> str:
    """Give every bounded attempt its own reuse identity so a correction round can never be
    answered by the very response it was sent to correct."""
    if attempt == 1:
        return submission_key
    digest = hashlib.sha256(
        json.dumps(correction, sort_keys=True, default=str).encode()
    ).hexdigest()
    return hashlib.sha256(f"{submission_key}\0{attempt}\0{digest}".encode()).hexdigest()


def _refuse_intake(
    paths: dict[str, Path],
    order_path: str,
    outcome: str,
    *,
    reason: str,
    raw_text: str,
    unknown_fields: list[str],
    clerk: dict | None,
    attempts: int,
    missing: list[str] | None = None,
    understood: list[str] | None = None,
    candidate: dict | None = None,
    errors: list[str] | None = None,
) -> IntakeOutcomeError:
    """Commit the durable refusal, then hand the door one standardized outcome to raise."""
    if missing is None or understood is None:
        summary_understood, summary_missing = _intake_field_summary(raw_text)
        missing = summary_missing if missing is None else missing
        understood = summary_understood if understood is None else understood
    try:
        _persist_intake_refusal(
            paths,
            outcome=outcome,
            reason=reason,
            missing=missing,
            understood=understood,
            candidate=candidate,
            unknown_fields=unknown_fields,
            clerk=clerk,
            attempts=attempts,
            errors=errors,
        )
    except OSError as error:
        return IntakeOutcomeError(
            "infrastructure-failure",
            order_path,
            f"{reason}; the refusal paper trail also failed: {error}",
            evidence=_intake_evidence(paths, clerk),
            attempts=attempts,
        )
    return IntakeOutcomeError(
        outcome,
        order_path,
        reason,
        evidence=_intake_evidence(paths, clerk),
        missing=missing,
        understood=understood,
        errors=errors or [reason],
        attempts=attempts,
    )


def _intake_order(order_path: str, roster_path: str) -> dict:
    """Every distinct submission starts one fresh intake clerk; Python never interprets a submission.

    Python's whole job here is to preserve the exact submitted bytes, launch the clerk, record its
    evidence, and consume its canonical output. Canonical JSON, malformed JSON, prose, an
    incomplete object, unknown fields, and specialist-worded requests all take this one path, and
    nothing about a request's schema, wording, order id, agent name, or intent ends it in Python.
    A byte-identical resubmission of the same file with a clean prior attempt is idempotent: it
    reuses that attempt's already-validated response instead of launching again (the reuse seam
    lives in `_run_order_intake_clerk`). Only three things end a request: a clerk-authored block, a
    clerk that could not answer within its bounded correction rounds, or a failure of the Hub's own
    machinery.
    """
    path = Path(order_path)
    paths = _intake_artifact_paths(path)
    path_key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    try:
        with _intake_attempt_lock(path_key):
            return _interpret_submission(order_path, path, paths, roster_path)
    except IntakeOutcomeError:
        raise
    except RecruiterError as error:  # the Hub's own workspace, never the submission
        raise IntakeOutcomeError(
            "infrastructure-failure",
            order_path,
            f"the Hub's intake workspace is unusable: {error}",
            evidence=_intake_evidence(paths),
        ) from error


def _interpret_submission(
    order_path: str, path: Path, paths: dict[str, Path], roster_path: str
) -> dict:
    """Run the bounded intake conversation for one submission under its held path lock."""
    try:
        raw_bytes = path.read_bytes()
        raw_text = raw_bytes.decode()
    except (OSError, UnicodeDecodeError) as error:
        raise IntakeOutcomeError(
            "infrastructure-failure",
            order_path,
            f"could not preserve the submitted bytes: {error}",
            evidence=_intake_evidence(paths),
        ) from error
    try:
        _write_bytes_atomic(paths["raw"], raw_bytes)
    except OSError as error:
        raise IntakeOutcomeError(
            "infrastructure-failure",
            order_path,
            "intake could not persist the exact raw submission; no paper trail means no "
            f"execution ({error})",
            evidence=_intake_evidence(paths),
        ) from error

    unknown_fields = _raw_unknown_fields(raw_text)
    submission_key = hashlib.sha256(
        str(path.resolve()).encode() + b"\0" + raw_bytes
    ).hexdigest()
    clerk_record: dict | None = None
    correction: dict[str, object] | None = None
    candidate: dict | None = None
    errors: list[str] = []
    reason = "the intake clerk returned no interpretation"

    for attempt in range(1, INTAKE_ATTEMPT_LIMIT + 1):
        try:
            response, clerk_record = _run_order_intake_clerk(
                raw_text,
                paths["raw"],
                roster_path,
                _intake_attempt_key(submission_key, attempt, correction),
                attempt_number=attempt,
                unknown_fields=unknown_fields,
                correction=correction,
            )
        except (
            RecruiterError,
            LifecycleError,
            ManagementConfigError,
            OSError,
        ) as error:
            raise _refuse_intake(
                paths,
                order_path,
                "intake-clerk-failure",
                reason=f"intake clerk unavailable: {error}",
                raw_text=raw_text,
                unknown_fields=unknown_fields,
                clerk=clerk_record,
                attempts=attempt,
            ) from error

        refusal = response.refusal
        if refusal is not None:
            raise _refuse_intake(
                paths,
                order_path,
                "blocked",
                reason=refusal,
                raw_text=raw_text,
                unknown_fields=unknown_fields,
                clerk=clerk_record,
                attempts=attempt,
                missing=list(response.missing),
                understood=list(response.understood),
            )

        candidate = cast(dict, response.order)
        errors = _clerk_provenance_errors(raw_text, candidate)
        if errors:
            reason = (
                "intake clerk interpretation invented or changed execution intent: "
                + "; ".join(errors)
            )
        else:
            try:
                order, form_changes = _complete_order_form(candidate, raw_text, path)
            except ContractError as error:
                errors = [str(error)]
                reason = (
                    f"intake clerk interpretation failed strict validation: {error}"
                )
            else:
                try:
                    _persist_intake_success(
                        path,
                        paths,
                        order,
                        changes=list(response.notes) + form_changes,
                        unknown_fields=unknown_fields,
                        clerk=clerk_record,
                        attempts=attempt,
                    )
                except OSError as error:
                    raise IntakeOutcomeError(
                        "infrastructure-failure",
                        order_path,
                        "intake could not persist its paper trail; no execution occurred "
                        f"({error})",
                        evidence=_intake_evidence(paths, clerk_record),
                        attempts=attempt,
                    ) from error
                return order
        # Bounded correction: the same intake role sees Python's authoritative findings and
        # either fixes its own answer or refuses. Python never edits an interpretation itself.
        correction = {"order": candidate, "errors": errors}

    raise _refuse_intake(
        paths,
        order_path,
        "intake-clerk-failure",
        reason=(
            f"the intake clerk did not produce a valid order in {INTAKE_ATTEMPT_LIMIT} "
            f"bounded attempts: {reason}"
        ),
        raw_text=raw_text,
        unknown_fields=unknown_fields,
        clerk=clerk_record,
        attempts=INTAKE_ATTEMPT_LIMIT,
        candidate=candidate,
        errors=errors,
    )


def cmd_recruit(order_path: str, roster_path: str) -> int:
    """Submit an order and return immediately; its claimed job owns the blocking lifecycle.

    Compatibility/manual surface only. Phase leaders use ``dispatch`` so their shell call blocks
    for the durable receipt without depending on this command's pane output.
    """
    order = _strict_order(order_path)
    ledger = JobLedger()
    key, _created = ledger.submit(order)
    warning, announce = _record_phase_receipt_warning(ledger, key, order)
    if warning is not None and announce:
        print(
            f"ORDER {order['order_id']} DEGRADED {json.dumps({'warning': warning})}",
            flush=True,
        )
    if ledger.completed_result(key, order) is not None:
        # A completed order is terminal and idempotent: its strict result already exists, so do
        # not open another job runner or worker pane.
        print(f"ORDER {order['order_id']} DONE", flush=True)
        return 0
    # Duplicate submissions atomically attach to the one registered live runner. A missing
    # handle for this Hub's durable claim is reconciled instead of launching a contender.
    owner: dict[str, object] = {"runner_pid": os.getpid()}
    try:
        # Resolve session ownership before launch so a correlated launch/session failure still
        # reaches the durable blocked-result path below.
        owner.update(_herdr_owner_record())
        _spawn_job(key, roster_path)
    except (OSError, RuntimeError, RecruiterError) as error:
        if not _terminalize_start_failure(ledger, key, order, owner, error):
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
    if pid != os.getpid():
        return False
    with _JOB_THREADS_LOCK:
        handle = _JOB_THREADS.get(key)
    return handle is not None and handle.thread.is_alive()


def _terminate_owned_runner(pid: object, key: str) -> None:
    """Never signal the locked Hub process in an attempt to stop one daemon job thread.

    Reconciliation fences the lease token before publication. The process itself terminates all
    daemon jobs when the Hub exits, so no runner can retain mutation authority afterward.
    """
    if pid != os.getpid() or not _runner_alive(pid, key):
        return


def _cleanup_lease_panes(lease: dict) -> dict[str, object]:
    """Close every pane explicitly recorded by one lease generation, and no others."""
    outcomes: dict[str, object] = {}
    failures = []
    try:
        herdr_session = _recorded_herdr_session(
            lease.get("herdr_session"), "lease pane cleanup"
        )
    except RecruiterError as error:
        herdr_session = None
        session_error = str(error)
    else:
        session_error = None
    for role, field in (("worker", "worker_pane"), ("manager", "manager_pane")):
        pane = lease.get(field)
        if not isinstance(pane, str) or not pane:
            outcomes[role] = {
                "status": "not-created",
                "worker_pane": None,
                "verified_absent": True,
            }
            continue
        if herdr_session is None:
            outcomes[role] = {
                "status": "cleanup-blocked",
                "worker_pane": pane,
                "verified_absent": False,
                "reason": session_error,
            }
            failures.append(session_error or "missing Herdr session")
            continue
        try:
            outcomes[role] = _close_worker_pane(pane, herdr_session=herdr_session)
        except RecruiterError as error:
            outcomes[role] = {
                "status": "cleanup-failed",
                "herdr_session": herdr_session,
                "worker_pane": pane,
                "verified_absent": False,
                "reason": str(error),
            }
            failures.append(str(error))
    return {
        **outcomes,
        "status": "closed" if not failures else "cleanup-failed",
        "verified_absent": not failures,
        **({"herdr_session": herdr_session} if herdr_session is not None else {}),
        "worker_pane": lease.get("worker_pane"),
        **({"reason": "; ".join(failures)} if failures else {}),
    }


def _verify_terminal_cleanup_absence(
    evidence: dict[str, object], *, require_runner_absent: bool = True
) -> None:
    """Read-only proof that terminal request panes remain absent before pruning."""
    order = evidence.get("order")
    if not isinstance(order, dict):
        raise RecruiterError("terminal cleanup evidence has no order")
    key = JobLedger.key_for_order(order)
    if require_runner_absent and _runner_alive(os.getpid(), key):
        raise RecruiterError(
            f"request {lifecycle.request_identity(order)} still has a live job runner"
        )
    launches = evidence.get("launches")
    if not isinstance(launches, list):
        raise RecruiterError("terminal cleanup evidence has invalid launches")
    by_session: dict[str, set[str]] = {}
    for journal in launches:
        if not isinstance(journal, dict):
            raise RecruiterError("terminal cleanup evidence has a malformed launch")
        pane = journal.get("pane")
        session = journal.get("herdr_session")
        if pane is None:
            continue
        if (
            not isinstance(pane, str)
            or not pane
            or not isinstance(session, str)
            or not session
        ):
            raise RecruiterError("terminal cleanup launch has invalid pane ownership")
        by_session.setdefault(session, set()).add(pane)
    for session, panes in by_session.items():
        remaining = panes & _live_pane_ids(herdr_session=session)
        if remaining:
            raise RecruiterError(
                "terminal cleanup refuses live owned pane(s): "
                + ", ".join(sorted(remaining))
            )


def _cancel_owned_request(
    ledger: JobLedger, key: str, lease: dict[str, object]
) -> dict[str, object]:
    """Close only journal-verified panes after the old lease token is fenced."""
    herdr_session = _recorded_herdr_session(
        lease.get("herdr_session"), "request cancellation"
    )
    journals = [
        journal
        for candidate_key, journal in ledger.launch_journals()
        if candidate_key == key
    ]
    journal_panes = {
        journal.get("pane")
        for journal in journals
        if isinstance(journal.get("pane"), str) and journal.get("pane")
    }
    for field in ("worker_pane", "manager_pane"):
        pane = lease.get(field)
        if isinstance(pane, str) and pane and pane not in journal_panes:
            raise RecruiterError(
                f"cancellation refuses unjournaled owned pane {pane} from {field}"
            )
    for journal in journals:
        cleanup = journal.get("cleanup")
        if (
            journal.get("state") == "closed"
            and isinstance(cleanup, dict)
            and cleanup.get("verified_absent") is True
        ):
            continue
        launch_id = journal.get("launch_id")
        if not isinstance(launch_id, str) or not launch_id:
            raise RecruiterError("cancellation found a launch without identity")
        current_session = _recorded_herdr_session(
            journal.get("herdr_session", herdr_session),
            "request cancellation launch",
        )
        outcome = _reconcile_exact_launch(
            ledger,
            key,
            launch_id,
            known_pane=cast(str | None, journal.get("pane")),
            herdr_session=current_session,
            allow_not_found_absent=True,
        )
        if outcome.get("verified_absent") is not True:
            reason = outcome.get("reason", "cleanup was not verified")
            raise RecruiterError(
                f"cancellation could not verify launch {launch_id} absent: {reason}"
            )
    refreshed = [
        journal
        for candidate_key, journal in ledger.launch_journals()
        if candidate_key == key
    ]
    if any(
        not isinstance(journal.get("cleanup"), dict)
        or cast(dict[str, object], journal["cleanup"]).get("verified_absent")
        is not True
        for journal in refreshed
    ):
        raise RecruiterError("cancellation did not close every owned launch")
    _verify_terminal_cleanup_absence(
        {"order": ledger.order(key), "launches": refreshed},
        require_runner_absent=False,
    )
    return {
        "status": "closed",
        "verified_absent": True,
        "herdr_session": herdr_session,
        "launches": _launch_receipt_evidence(ledger, key),
        "worker_pane": lease.get("worker_pane"),
    }


def _write_required_blocked_bundle(order: dict, manifest: Any, reason: str) -> dict:
    """Regenerate every required staged artifact with one consistent blocked reason."""
    return cast(
        dict,
        completion.write_blocked_bundle(
            manifest,
            reason,
            write_result=lambda path, why: _write_blocked_result(
                order, why, path, preserve_valid=False
            ),
            failure_answer=contracts_consult.failure_answer,
        ),
    )


def _terminalize_start_failure(
    ledger: JobLedger,
    key: str,
    order: dict,
    owner: dict[str, object],
    error: OSError | RuntimeError | RecruiterError,
) -> bool:
    """Publish the Python-authored blocked bundle for a runner launch failure."""
    timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
    token = ledger.claim(key, order["order_id"], timeout_ms, owner=owner)
    if token is None:
        ledger._event(key, "start-failed-unowned", reason=str(error))
        return False
    manifest = completion.build_manifest(
        order, ledger.request_dir(key), token, lifecycle.request_identity(order)
    )
    reason = f"could not start job runner: {error}"
    result = _write_required_blocked_bundle(order, manifest, reason)
    cleanup = {
        "status": "not-created",
        "worker_pane": None,
        "verified_absent": True,
    }
    return ledger.finalize(
        key,
        token,
        order,
        result,
        cleanup=cleanup,
        reason=str(error),
        exit_code=1,
        completion_source="runner-start-failure",
    )


def _launch_receipt_evidence(ledger: JobLedger, key: str) -> list[dict[str, object]]:
    """Return exact closed-launch evidence, refusing any unresolved journal."""

    journals = [
        journal
        for candidate_key, journal in ledger.launch_journals()
        if candidate_key == key
    ]
    unresolved = [
        journal
        for journal in journals
        if journal.get("state") != "closed"
        or not isinstance(journal.get("cleanup"), dict)
        or journal["cleanup"].get("verified_absent") is not True
    ]
    if unresolved:
        raise FencedLaunchError(
            "launch cleanup remains unresolved for "
            + ", ".join(str(journal.get("launch_id")) for journal in unresolved),
            {
                "launch_ids": [journal.get("launch_id") for journal in unresolved],
                "status": "cleanup-pending",
                "verified_absent": False,
            },
        )
    return [
        {
            "agent_name": journal.get("agent_name"),
            "cleanup": cast(dict[str, object], journal["cleanup"]),
            "launch_id": journal.get("launch_id"),
            "pane": journal.get("pane"),
            "role": journal.get("role"),
            "state": journal.get("state"),
        }
        for journal in sorted(journals, key=lambda item: str(item.get("launch_id")))
    ]


def _reconcile_claim(ledger: JobLedger, key: str, lease: dict, *, force: bool) -> bool:
    """Close and terminalize one dead/expired owned job. Never touches an unrecorded pane."""
    expired = lease["expires_at"] <= int(time.time())
    runner_alive = _runner_alive(lease.get("runner_pid"), key)
    if not force and not expired and runner_alive:
        return False
    if runner_alive:
        _terminate_owned_runner(lease.get("runner_pid"), key)

    worker_pane = lease.get("worker_pane")
    try:
        launch_evidence = _launch_receipt_evidence(ledger, key)
    except FencedLaunchError as error:
        ledger._event(
            key,
            "terminalization-deferred-for-launch-cleanup",
            launch_ids=error.cleanup.get("launch_ids", []),
        )
        return False
    cleanup = _cleanup_lease_panes(lease)
    if launch_evidence:
        cleanup["launches"] = launch_evidence

    order = ledger.order(key)
    manifest = completion.build_manifest(
        order,
        ledger.request_dir(key),
        lease["token"],
        lifecycle.request_identity(order),
    )
    manifest_path = ledger.request_dir(key) / "artifact-manifest.json"
    try:
        completion.parse_manifest(manifest_path.read_text(), manifest)
    except (OSError, CompletionError):
        # The manifest is Python-owned lease metadata. Recreate it deterministically before
        # authoring a recovery bundle when the runner crashed before (or during) staging.
        completion.write_manifest(manifest_path, manifest)
    if cleanup["verified_absent"]:
        try:
            result = completion.validate_bundle(
                manifest,
                load_result=load_result,
                load_answer=contracts_consult.load_answer,
            )
        except CompletionError as error:
            result = _write_required_blocked_bundle(
                order, manifest, f"runner reconciliation: {error}"
            )
    else:
        result = _write_required_blocked_bundle(
            order,
            manifest,
            f"runner reconciliation could not close worker pane {worker_pane}",
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


def _reconcile_launch_journals(ledger: JobLedger, *, force: bool) -> int:
    """Boundedly compensate every nonterminal exact-agent launch journal."""

    reconciled = 0
    active_claims = dict(ledger.active_claims())
    for key, journal in ledger.launch_journals():
        if journal.get("state") == "closed":
            continue
        expires_at = journal.get("expires_at")
        expired = isinstance(expires_at, int) and expires_at <= int(time.time())
        if not force and not expired and _same_owner_process(journal):
            lease = active_claims.get(key)
            if lease is not None and _runner_alive(lease.get("runner_pid"), key):
                continue
        launch_id = journal.get("launch_id")
        if not isinstance(launch_id, str):
            raise RecruiterError("launch journal is missing launch_id")
        herdr_session = _recorded_herdr_session(
            journal.get("herdr_session"), "launch reconciliation"
        )
        cleanup = _reconcile_exact_launch(
            ledger,
            key,
            launch_id,
            known_pane=cast(str | None, journal.get("pane")),
            herdr_session=herdr_session,
            allow_not_found_absent=True,
        )
        if cleanup.get("verified_absent") is True:
            reconciled += 1
    return reconciled


def _reconcile_intake_clerks(*, force: bool) -> int:
    """Resolve orphan clerks by journaled agent name; never close a recorded pane id alone."""
    try:
        layout = _prepare_intake_layout()
    except RecruiterError as error:
        command_runtime.write_stderr(
            f"recruiter: intake reconciliation unavailable: {error}\n"
        )
        return 0
    reconciled = 0
    try:
        entries = list(os.scandir(layout["attempts"]))
    except OSError as error:
        command_runtime.write_stderr(
            f"recruiter: could not scan intake attempts: {error}\n"
        )
        return 0
    for entry in entries:
        if not entry.name.startswith("attempt-"):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                raise RecruiterError(
                    f"intake attempt {entry.name} is not a real directory"
                )
            attempt = _validated_attempt_directory(layout["attempts"], entry.name)
            ownership_path = attempt / "ownership.json"
            if not _secure_file_exists(ownership_path):
                continue
            ownership = _secure_json(ownership_path)
            journal_key = ownership.get("intake_key")
            journal_token = ownership.get("lease_token")
            if (
                ownership.get("schema_version") != 1
                or ownership.get("attempt_name") != attempt.name
                or not isinstance(journal_key, str)
                or not isinstance(journal_token, str)
                or ownership.get("agent_name")
                != _intake_clerk_agent_name(journal_key, journal_token)
            ):
                raise RecruiterError(
                    "intake ownership journal does not match its attempt directory or lease"
                )
            if ownership.get("state") == "closed":
                continue
            expires_at = ownership.get("expires_at")
            expired = isinstance(expires_at, int) and expires_at <= int(time.time())
            if not force and not expired and _same_owner_process(ownership):
                continue
            cleanup = _cleanup_intake_clerk(ownership)
            ownership.update(
                {
                    "cleanup": cleanup,
                    "reconciled_at_ns": time.time_ns(),
                    "state": (
                        "closed"
                        if cleanup.get("verified_absent")
                        else "launch-uncertain"
                        if cleanup.get("status") == "launch-uncertain"
                        else "cleanup-failed"
                    ),
                }
            )
            _secure_write_json(ownership_path, ownership)
            if cleanup.get("verified_absent"):
                reconciled += 1
        except RecruiterError as error:
            command_runtime.write_stderr(
                f"recruiter: unsafe or unreadable intake attempt {entry.name}: {error}\n"
            )
    return reconciled


def cmd_reconcile(*, force: bool = False, emit: bool = True) -> int:
    ledger = JobLedger()
    launches_reconciled = _reconcile_launch_journals(ledger, force=force)
    reconciled = 0
    for key, lease in ledger.active_claims():
        if _reconcile_claim(ledger, key, lease, force=force):
            reconciled += 1
    intake_reconciled = _reconcile_intake_clerks(force=force)
    if emit:
        print(
            json.dumps(
                {
                    "reconciled": reconciled,
                    "intake_clerks_reconciled": intake_reconciled,
                    "launches_reconciled": launches_reconciled,
                    "force": force,
                },
                sort_keys=True,
            )
        )
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
        try:
            cmd_reconcile(force=False, emit=False)
        except RecruiterError as error:
            command_runtime.write_stderr(
                f"recruiter: supervisor reconciliation remains pending: {error}\n"
            )
        time.sleep(2)


class _JobThread:
    """Popen-shaped handle for one Hub-owned daemon runner thread."""

    def __init__(self, key: str, roster_path: str):
        self.key = key
        self.cwd = command_runtime.current_cwd()
        self.environ = command_runtime.current_environ()
        self.roster_path = roster_path
        self.thread = threading.Thread(
            target=self._run,
            name=f"upagent-job-{key[:12]}",
            daemon=True,
        )

    def _run(self) -> None:
        # Only cwd/environment follow the submission. Output deliberately has no request sink,
        # so a background runner can never write into the response that happened to launch it.
        with command_runtime.activate(self.cwd, self.environ):
            cmd_run_job(self.key, self.roster_path)

    def start(self) -> None:
        self.thread.start()

    def poll(self) -> int | None:
        return None if self.thread.is_alive() else 0

    def wait(self, timeout: float | None = None) -> int:
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired(
                f"Hub-owned runner {self.key}", timeout if timeout is not None else 0
            )
        return 0


_JOB_THREADS: dict[str, _JobThread] = {}
_JOB_THREADS_LOCK = threading.Lock()


def migration_activity() -> dict[str, list[str]]:
    """Return every request that makes runtime replacement unsafe.

    A newly spawned runner is visible in the thread registry before it can claim the ledger, so
    the Hub's atomic stop transaction cannot miss the submit-to-claim window.
    """
    _require_hub_authority()
    # Snapshot runners before claims. A registered live runner may transition into a durable
    # claim after this first read; seeing it live is already sufficient to refuse replacement.
    # A runner that is no longer alive cannot create a future claim, so the following ledger read
    # closes the opposite side of the transition without a missed-empty window.
    with _JOB_THREADS_LOCK:
        live_runners = sorted(
            key for key, handle in _JOB_THREADS.items() if handle.thread.is_alive()
        )
    active_requests = sorted(key for key, _lease in JobLedger().active_claims())
    return {
        "active_requests": active_requests,
        "live_runners": live_runners,
    }


def _spawn_job(key: str, roster_path: str) -> _JobThread | None:
    """Atomically get-or-attach one runner, reconciling an orphaned owned claim."""
    _require_hub_authority()
    orphaned_claim: dict | None = None
    with _JOB_THREADS_LOCK:
        existing = _JOB_THREADS.get(key)
        if existing is not None and existing.thread.is_alive():
            return existing
        if existing is not None:
            del _JOB_THREADS[key]
        orphaned_claim = next(
            (
                lease
                for candidate, lease in JobLedger().active_claims()
                if candidate == key and lease.get("runner_pid") == os.getpid()
            ),
            None,
        )
        if orphaned_claim is None:
            handle = _JobThread(key, roster_path)
            _JOB_THREADS[key] = handle
            try:
                handle.start()
            except (OSError, RuntimeError):
                if _JOB_THREADS.get(key) is handle:
                    del _JOB_THREADS[key]
                raise
            return handle
    # Never hold the registry lock while reconciliation calls back through _runner_alive().
    assert orphaned_claim is not None
    _reconcile_claim(JobLedger(), key, orphaned_claim, force=True)
    return None


def _strict_order(order_path: str) -> dict:
    try:
        order = load_order(order_path)
        completion.ensure_publication_contract(order)
        contracts.parse_order(json.dumps(order))
    except (ContractError, CompletionError) as error:
        raise RecruiterError(f"invalid order {order_path}: {error}") from error
    # Persist compatibility metadata before the first ledger mutation. Crash recovery therefore
    # reads the exact required manifest contract used by the launching path.
    JobLedger._write_json(Path(order_path), order)
    return order


_CURRENT_LOG_ORDER_PATH: str | None = None


@contextmanager
def _request_log_order_path(order_path: str) -> Iterator[None]:
    global _CURRENT_LOG_ORDER_PATH
    previous = _CURRENT_LOG_ORDER_PATH
    _CURRENT_LOG_ORDER_PATH = order_path
    try:
        yield
    finally:
        _CURRENT_LOG_ORDER_PATH = previous


def cmd_dispatch(order_path: str, roster_path: str) -> int:
    """Strict legacy/controller socket shim; no intake LLM or form repair."""
    with _request_log_order_path(order_path):
        return _dispatch_order(_strict_order(order_path), roster_path)


def cmd_dispatch_strict(order_path: str, roster_path: str) -> int:
    """Strict public-path dispatch entry used only after closed-schema validation."""
    with _request_log_order_path(order_path):
        return _dispatch_order(_strict_order(order_path), roster_path)


def _log_request_event(event: str, payload: dict[str, object]) -> None:
    """Emit human-visible lifecycle progress without changing stdout receipt grammar."""
    command_runtime.write_stderr(
        f"UPAGENT_{event} {json.dumps(payload, sort_keys=True)}\n"
    )


def _order_log_payload(
    order: dict, *, order_path: str | None = None
) -> dict[str, object]:
    order_path = order_path or _CURRENT_LOG_ORDER_PATH
    payload: dict[str, object] = {
        "agent": order.get("agent"),
        "harness": order.get("harness"),
        "order_id": order.get("order_id"),
        "request_id": lifecycle.request_identity(order),
        "stage_id": order.get("stage_id"),
        "timeout_ms": order.get("timeout_ms", _default_timeout_ms(order["stage_id"])),
    }
    if order_path is not None:
        payload["order_path"] = order_path
    result_path = order.get("result_path")
    if result_path:
        payload["result_path"] = result_path
    return {key: value for key, value in payload.items() if value is not None}


def _dispatch_order(order: dict, roster_path: str) -> int:
    """Submit one already-valid order and block for its durable terminal receipt."""
    _log_request_event("DISPATCH_START", _order_log_payload(order))
    ledger = JobLedger()
    key, _created = ledger.submit(order)
    warning, announce = _record_phase_receipt_warning(ledger, key, order)
    if warning is not None and announce:
        print(
            f"REQUEST_DEGRADED {json.dumps({'request_id': lifecycle.request_identity(order), 'warning': warning}, sort_keys=True)}",
            flush=True,
        )
    if ledger.completed_result(key, order) is None:
        owner: dict[str, object] = {"runner_pid": os.getpid()}
        try:
            owner.update(_herdr_owner_record())
            _log_request_event(
                "DISPATCH_SPAWN",
                {**_order_log_payload(order), "ledger_key": key},
            )
            process = _spawn_job(key, roster_path)
        except (OSError, RuntimeError, RecruiterError) as error:
            if not _terminalize_start_failure(ledger, key, order, owner, error):
                raise RecruiterError(
                    f"could not start a contender for live order {order['order_id']}: {error}"
                ) from error
            process = None
        timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
        deadline = time.monotonic() + timeout_ms / 1000 + LEASE_GRACE_SECONDS
        if process is not None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                matching = next(
                    (
                        lease
                        for candidate, lease in ledger.active_claims()
                        if candidate == key
                    ),
                    None,
                )
                if matching is None or not _reconcile_claim(
                    ledger, key, matching, force=True
                ):
                    raise RecruiterError(
                        f"job runner for {order['order_id']} exceeded its lease window"
                    ) from error
        # Normal jobs publish before exiting. A killed/crashed runner may need the standing
        # reconciler to publish a blocked receipt; this short bounded wait is anomaly-only.
        while (
            ledger.completed_result(key, order) is None and time.monotonic() < deadline
        ):
            time.sleep(0.1)
    result = ledger.completed_result(key, order)
    if result is None:
        raise RecruiterError(
            f"job runner for {order['order_id']} exited without a completion receipt"
        )
    receipt = ledger.completed_receipt(key, order)
    _log_request_event(
        "DISPATCH_DONE",
        {
            **_order_log_payload(order),
            "receipt_state": receipt.get("state"),
            "verdict": result.get("verdict"),
        },
    )
    print(f"ORDER_RECEIPT {json.dumps(receipt, sort_keys=True)}", flush=True)
    return 0 if result["verdict"] == "passed" else 1


def cmd_request(order_path: str, roster_path: str) -> int:
    """Strict legacy socket shim; arbitrary or malformed intake is rejected in Python."""
    with _request_log_order_path(order_path):
        return _request_order(_strict_order(order_path), roster_path)


def cmd_request_strict(order_path: str, roster_path: str) -> int:
    """Strict public-path request entry used only after closed-schema validation."""
    with _request_log_order_path(order_path):
        return _request_order(_strict_order(order_path), roster_path)


def _request_order(order: dict, roster_path: str) -> int:
    """Submit one already-valid order and return after worker health or rejection."""
    _log_request_event("REQUEST_START", _order_log_payload(order))
    roster = load_roster(roster_path)
    config = llm_management.load_management_config(roster)
    ledger = JobLedger()
    key, _ = ledger.submit(order)
    warning, _announce = _record_phase_receipt_warning(ledger, key, order)
    existing = ledger.completed_result(key, order)
    if existing is not None:
        receipt = ledger.completed_receipt(key, order)
        _log_request_event(
            "REQUEST_ALREADY_TERMINAL",
            {
                **_order_log_payload(order),
                "receipt_state": receipt.get("state"),
                "verdict": existing.get("verdict"),
            },
        )
        print(f"REQUEST_TERMINAL {json.dumps(receipt, sort_keys=True)}")
        return 0 if existing["verdict"] == "passed" else 1
    owner: dict[str, object] = {"runner_pid": os.getpid()}
    try:
        owner.update(_herdr_owner_record())
        _log_request_event(
            "REQUEST_SPAWN",
            {**_order_log_payload(order), "ledger_key": key},
        )
        process = _spawn_job(key, roster_path)
    except (OSError, RuntimeError, RecruiterError) as error:
        if not _terminalize_start_failure(ledger, key, order, owner, error):
            raise RecruiterError(
                f"could not start request owner for {order['order_id']}: {error}"
            ) from error
        process = None
    deadline = (
        time.monotonic()
        + (config.account_manager.timeout_ms + config.startup_timeout_ms * 2) / 1000
    )
    while time.monotonic() < deadline:
        state = ledger.state(key)
        if state.get("state") == "running":
            response = {
                "control_token": state.get("requester_control_token"),
                "manager_address": state.get("manager_address"),
                "manager_pane": state.get("manager_pane"),
                "manager_workspace_id": state.get("manager_workspace_id"),
                "request_id": lifecycle.request_identity(order),
                "state": "running",
                "worker_address": state.get("worker_address"),
                "worker_pane": state.get("worker_pane"),
                "worker_workspace_id": state.get("workspace_id"),
            }
            if warning is not None:
                response["degraded"] = True
                response["warning"] = warning
            _log_request_event(
                "REQUEST_ACCEPTED",
                {
                    **_order_log_payload(order),
                    "manager_pane": response.get("manager_pane"),
                    "state": "running",
                    "worker_pane": response.get("worker_pane"),
                },
            )
            print(
                f"REQUEST_ACCEPTED {json.dumps(response, sort_keys=True)}", flush=True
            )
            return 0
        if state.get("state") in ("finished", "cleanup-failed"):
            receipt = ledger.completed_receipt(key, order)
            _log_request_event(
                "REQUEST_TERMINAL",
                {
                    **_order_log_payload(order),
                    "receipt_state": receipt.get("state"),
                    "verdict": receipt.get("verdict"),
                },
            )
            print(f"REQUEST_TERMINAL {json.dumps(receipt, sort_keys=True)}", flush=True)
            return 0 if receipt["verdict"] == "passed" else 1
        if (
            process is not None
            and cast(Any, process).poll() is not None
            and state.get("state")
            not in (
                "running",
                "finished",
                "cleanup-failed",
            )
        ):
            raise RecruiterError(
                f"request owner for {order['order_id']} exited before worker health: state={state.get('state')}"
            )
        time.sleep(HEALTH_PROBE_SECONDS)
    raise RecruiterError(
        f"request {order['order_id']} did not reach worker-healthy before its startup deadline"
    )


def _notify_order_completion(order_id: str, verdict: str) -> None:
    """Best-effort human ping via herdr notification; never raises."""
    command = [
        "herdr",
        "notification",
        "show",
        "Herdr order finished",
        "--body",
        f"{order_id} finished with verdict {verdict}",
        "--sound",
        "request",
    ]
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(command, check=False, capture_output=True, timeout=10)


def _maybe_notify_completion(
    waited_ms: float,
    threshold_ms: int,
    order_id: str,
    verdict: str,
    notify: Callable[[str, str], None] = _notify_order_completion,
) -> bool:
    """Ping the human when a long one-shot finishes. threshold_ms <= 0 disables."""
    if threshold_ms <= 0 or waited_ms < threshold_ms:
        return False
    notify(order_id, verdict)
    return True


def cmd_await(order_path: str, notify_after_ms: int = 600_000) -> int:
    """Block in Python until a request is terminal or needs an owner decision.

    When the wait outlives ``notify_after_ms`` (default ten minutes; 0 disables), the
    terminal receipt also pings the human via ``herdr notification`` so nobody babysits
    a pane for a long one-shot.
    """
    try:
        order = load_order(order_path)
    except ContractError as error:
        raise RecruiterError(f"invalid order {order_path}: {error}") from error
    ledger = JobLedger()
    key = ledger.key_for_order(order)
    if not ledger.request_dir(key).is_dir():
        raise RecruiterError(
            f"request {lifecycle.request_identity(order)} has not been submitted"
        )
    started = time.monotonic()
    deadline = started + (
        order.get("timeout_ms", _default_timeout_ms(order["stage_id"])) / 1000
        + LEASE_GRACE_SECONDS
    )
    while time.monotonic() < deadline:
        state = ledger.state(key)
        if state.get("state") == "awaiting-requester":
            response = {
                "decision_nonce": state.get("decision_nonce"),
                "request_id": lifecycle.request_identity(order),
                "state": "awaiting-requester",
                "timeout_number": state.get("timeout_number"),
            }
            print(
                f"REQUESTER_DECISION_REQUIRED {json.dumps(response, sort_keys=True)}",
                flush=True,
            )
            return 2
        if state.get("state") in ("finished", "cleanup-failed"):
            receipt = ledger.completed_receipt(key, order)
            print(f"ORDER_RECEIPT {json.dumps(receipt, sort_keys=True)}", flush=True)
            _maybe_notify_completion(
                (time.monotonic() - started) * 1000,
                notify_after_ms,
                order["order_id"],
                str(receipt["verdict"]),
            )
            return 0 if receipt["verdict"] == "passed" else 1
        time.sleep(HEALTH_PROBE_SECONDS)
    raise RecruiterError(
        f"request {lifecycle.request_identity(order)} exceeded its await window"
    )


def cmd_verify(
    order_path: str,
    roster_path: str,
    harness: str = "claude",
    model: str = "",
    agent: str = "reviewer",
    effort: str = "low",
    offering_snapshot: dict[str, object] | None = None,
    wait: bool = True,
) -> int:
    """Hire one independent reviewer to poke holes in a finished order's result.

    Nothing calls this automatically — it is the optional second opinion for the
    one-shot flow. The reviewer gets the original brief, the finished result, and the
    work directory, and writes an ordinary result.json: ``passed`` when the claims
    hold, ``failed`` with concrete findings when they do not.
    """
    original = load_order(order_path)
    result = load_result(
        original["result_path"], expected_order_id=original["order_id"]
    )
    base = Path(original["result_path"]).expanduser()
    verify_id = f"{original['order_id']}.verify"
    instructions_path = base.with_name("verify-instructions.md")
    verify_result_path = base.with_name("verify-result.json")
    brief = f"""# Independent verification of a finished order

You are an independent reviewer. Another worker claims it finished this job:

- Original brief: {original["instructions_path"]}
- Claimed result (verdict `{result["verdict"]}`): {original["result_path"]}
- Work directory: {original["cwd"]}

Adversarially check the claims against the actual work. Read the code and evidence;
do not take the result's word for anything. Look for half-done work, silent failures,
scope creep, and claims without evidence.

Write exactly one JSON object to `{verify_result_path}`:

```json
{{
  "order_id": "{verify_id}",
  "verdict": "passed",
  "full_log": "<your transcript path or session id>",
  "reason": "one-line summary; on failed, the concrete findings worst-first"
}}
```

Use `passed` when the original claims hold, `failed` when they do not (include
`"revisit": ["{original["stage_id"]}"]`). Then exit the session.
"""
    _write_text_atomic(instructions_path, brief)
    verify_order = {
        "order_id": verify_id,
        "request_id": f"{lifecycle.request_identity(original)}.verify",
        "phase_id": original["phase_id"],
        "stage_id": "stage-2-adversarial-audit",
        "harness": harness,
        "model": model,
        "agent": agent,
        "effort": effort,
        "cwd": original["cwd"],
        "instructions_path": str(instructions_path),
        "result_path": str(verify_result_path),
        "cockpit_pane": original["cockpit_pane"],
        **(
            {"offering_snapshot": offering_snapshot}
            if offering_snapshot is not None
            else {}
        ),
    }
    verify_order_path = base.with_name("verify-order.json")
    JobLedger._write_json(verify_order_path, verify_order)
    if wait:
        return cmd_dispatch(str(verify_order_path), roster_path)
    return cmd_request(str(verify_order_path), roster_path)


_AWAIT_ANY_VERDICT_KINDS = {
    "passed": "completed",
    "failed": "failed",
    "blocked": "blocked",
}


def _mailbox_messages(ledger: JobLedger, key: str) -> list[tuple[int, dict]]:
    messages_dir = ledger.requester_mailbox(key).messages
    messages: list[tuple[int, dict]] = []
    paths = sorted(messages_dir.glob("[0-9]*-*.json")) if messages_dir.is_dir() else []
    for path in paths:
        try:
            sequence = int(path.name.split("-", 1)[0])
            payload = json.loads(path.read_text())
        except (ValueError, OSError, json.JSONDecodeError) as error:
            raise RecruiterError(
                f"mailbox message {path} is unreadable: {error}"
            ) from error
        if isinstance(payload, dict):
            messages.append((sequence, payload))
    return messages


def _emit_await_event(
    kind: str, request_id: str | None, summary: str, cursor: dict, **detail: object
) -> int:
    event: dict[str, object] = {
        "at_ns": time.time_ns(),
        "cursor": cursor,
        "kind": kind,
        "request_id": request_id,
        "summary": summary,
    }
    event.update(detail)
    print(f"AWAIT_EVENT {json.dumps(event, sort_keys=True)}", flush=True)
    return 0


def cmd_await_any(
    order_paths: list[str],
    timeout_ms: int,
    cursor_json: str = "{}",
    poll_seconds: float = HEALTH_PROBE_SECONDS,
) -> int:
    if not order_paths:
        raise RecruiterError("await-any requires at least one order path")
    try:
        cursor_value = json.loads(cursor_json) if cursor_json else {}
    except json.JSONDecodeError as error:
        raise RecruiterError(f"await-any cursor is not valid JSON: {error}") from error
    if not isinstance(cursor_value, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in cursor_value.items()
    ):
        raise RecruiterError(
            "await-any cursor must be a JSON object of request_id -> integer"
        )
    cursor: dict[str, int] = dict(cursor_value)
    ledger = JobLedger()
    watched: list[tuple[str, dict, str, str]] = []
    seen: set[str] = set()
    for path in order_paths:
        try:
            order = load_order(path)
        except ContractError as error:
            raise RecruiterError(f"invalid order {path}: {error}") from error
        key = ledger.key_for_order(order)
        if not ledger.request_dir(key).is_dir():
            raise RecruiterError(
                f"request {lifecycle.request_identity(order)} has not been submitted"
            )
        request_id = lifecycle.request_identity(order)
        if request_id in seen:
            raise RecruiterError(f"await-any lists request {request_id} more than once")
        seen.add(request_id)
        watched.append((path, order, key, request_id))
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        states: dict[str, str] = {}
        for path, order, key, request_id in watched:
            state = ledger.state(key)
            states[request_id] = str(state.get("state"))
            if state.get("state") == "awaiting-requester":
                return _emit_await_event(
                    "decision-required",
                    request_id,
                    f"Request {request_id} reached a work cap; the owner must extend or cancel.",
                    cursor,
                    decision_nonce=state.get("decision_nonce"),
                    order_path=path,
                    terminal=False,
                    timeout_number=state.get("timeout_number"),
                )
            if state.get("state") in ("finished", "cleanup-failed"):
                receipt = ledger.completed_receipt(key, order)
                verdict = str(receipt.get("verdict"))
                kind = _AWAIT_ANY_VERDICT_KINDS.get(verdict, "failed")
                return _emit_await_event(
                    kind,
                    request_id,
                    f"Request {request_id} is terminal: verdict={verdict}.",
                    cursor,
                    order_path=path,
                    receipt=receipt,
                    terminal=True,
                )
            for sequence, payload in _mailbox_messages(ledger, key):
                if sequence <= cursor.get(request_id, 0):
                    continue
                cursor[request_id] = sequence
                message_type = str(payload.get("type", ""))
                kind = (
                    "worker-warning"
                    if ("warning" in message_type or "failed" in message_type)
                    else "advisory"
                )
                return _emit_await_event(
                    kind,
                    request_id,
                    str(payload.get("message", message_type)),
                    cursor,
                    detail=payload.get("detail", {}),
                    message_type=message_type,
                    order_path=path,
                    sequence=sequence,
                    terminal=False,
                )
        if time.monotonic() >= deadline:
            return _emit_await_event(
                "await-heartbeat",
                None,
                "Quiet and healthy: no watched request moved within the bounded wait; re-await.",
                cursor,
                states=states,
                terminal=False,
            )
        time.sleep(poll_seconds)


def cmd_respond(
    order_path: str,
    control_token: str,
    nonce: str,
    action: str,
    extension_ms: int | None,
    message: str,
) -> int:
    """Record one authenticated owner decision for the current timeout warning."""
    try:
        order = load_order(order_path)
    except ContractError as error:
        raise RecruiterError(f"invalid order {order_path}: {error}") from error
    ledger = JobLedger()
    key = ledger.key_for_order(order)
    claim_dir = ledger.active / "requests" / key
    if not claim_dir.is_dir():
        raise RecruiterError(
            f"request {lifecycle.request_identity(order)} is not active"
        )
    state = ledger.state(key)
    if (
        state.get("state") != "awaiting-requester"
        or state.get("decision_nonce") != nonce
    ):
        raise RecruiterError(
            "response does not match the current requester decision point"
        )
    lease = ledger._lease(claim_dir / "lease.json")
    expected_control = lease.get("requester_control_token")
    if not isinstance(expected_control, str) or not hmac.compare_digest(
        expected_control, control_token
    ):
        raise RecruiterError(
            "requester control token does not match the current request generation"
        )
    generation = lease.get("generation", 1)
    payload = {
        "request_id": lifecycle.request_identity(order),
        "generation": generation,
        "action": action,
        "extension_ms": extension_ms,
        "message": message,
    }
    decision = lifecycle.parse_requester_decision(
        json.dumps(payload), lifecycle.request_identity(order), generation
    )
    path = ledger.record_requester_decision(key, lease["token"], nonce, decision)
    print(
        json.dumps(
            {"accepted": True, "decision_path": str(path), "action": action},
            sort_keys=True,
        )
    )
    return 0


def cmd_cancel(order_path: str, control_token: str) -> dict[str, object]:
    """Authenticate, fence, clean, and terminalize one requester cancellation."""
    try:
        order = load_order(order_path)
    except ContractError as error:
        raise RecruiterError(f"invalid order {order_path}: {error}") from error
    ledger = JobLedger()
    key = ledger.key_for_order(order)
    outcome = ledger.begin_cancel(key, control_token)
    if outcome.get("terminal") is True:
        tombstone = outcome.get("tombstone")
        if isinstance(tombstone, dict):
            return {
                "cancelled": tombstone.get("cancelled", False),
                "receipt": tombstone.get("receipt"),
                "result": tombstone.get("result"),
                "terminal": True,
            }
        receipt = outcome.get("receipt")
        return {
            "cancelled": receipt.get("cancelled", False)
            if isinstance(receipt, dict)
            else False,
            "receipt": receipt,
            "result": outcome.get("result"),
            "terminal": True,
        }
    lease = outcome.get("lease")
    token = outcome.get("token")
    if not isinstance(lease, dict) or not isinstance(token, str):
        raise RecruiterError("cancellation fence returned invalid ownership")
    with _request_runtime_lock(key):
        cleanup = _cancel_owned_request(ledger, key, lease)
        manifest = completion.build_manifest(
            order,
            ledger.request_dir(key),
            token,
            lifecycle.request_identity(order),
        )
        completion.write_manifest(
            ledger.request_dir(key) / "artifact-manifest.json", manifest
        )
        reason = "requester cancelled this request using its control token"
        result = _write_required_blocked_bundle(order, manifest, reason)
        finalized = ledger.finalize(
            key,
            token,
            order,
            result,
            cleanup=cleanup,
            cancelled=True,
            cancellation_reason=reason,
            completion_source="requester-cancel",
        )
    receipt = ledger.completed_receipt(key, order)
    result = ledger.completed_result(key, order)
    return {
        "cancelled": True if finalized else receipt.get("cancelled", False),
        "receipt": receipt,
        "result": result,
        "terminal": True,
    }


def cmd_run_job(key: str, roster_path: str) -> int:
    """Claim one persisted request, then run its existing exclusive worker lifecycle."""
    ledger = JobLedger()
    order = ledger.order(key)
    roster = load_roster(roster_path)
    management_config = llm_management.load_management_config(roster)
    timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
    request_id = lifecycle.request_identity(order)
    generation = 1
    owner = {
        **_herdr_owner_record(),
        "generation": generation,
        "request_id": request_id,
        "runner_pid": os.getpid(),
        "phase_leader_pane": order["cockpit_pane"],
        "recruiter_pane": _recruiter_pane_from_state(),
    }
    herdr_session = cast(str, owner["herdr_session"])
    lease_window_ms = (
        timeout_ms
        + management_config.account_manager.timeout_ms
        + management_config.startup_timeout_ms * 2
        + management_config.requester_grace_ms
    )
    token = ledger.claim(key, order["order_id"], lease_window_ms, owner=owner)
    if token is None:
        return 0
    artifact_manifest = completion.build_manifest(
        order, ledger.request_dir(key), token, request_id
    )
    completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", artifact_manifest
    )
    worker_result_path = artifact_manifest.artifact("result").staging_path
    monitor: tuple[threading.Event, threading.Event, threading.Thread] | None = None
    manager: dict[str, object] | None = None
    mechanical_validation = inspect_worker_configuration(order, roster)

    # An order may pin its own lifecycle ownership; the roster sets the default. Consults do
    # not pin one: they follow the roster like any other order.
    requested_management = order.get("management") or {}
    effective_management_mode = (
        requested_management.get("mode") or management_config.mode
    )
    try:
        if effective_management_mode == "dedicated":
            manager = _start_account_manager(
                ledger,
                key,
                token,
                order,
                roster,
                generation,
                mechanical_validation,
                herdr_session=herdr_session,
            )
        else:
            manager = _direct_manager(
                management_config, order, generation, herdr_session=herdr_session
            )
        manager["lease_token"] = token
    except (RecruiterError, ManagementConfigError, LifecycleError, OSError) as error:
        # Account Manager supervision is advisory. Losing it degrades observation only; Python
        # still owns the lease, runs mechanically valid work, and guarantees a terminal bundle.
        ledger._event(key, "account-manager-degraded", reason=str(error))
        _notify_requester(
            ledger,
            key,
            order,
            generation,
            "account-manager-degraded",
            f"Advisory Account Manager supervision is unavailable: {error}",
        )
        manager = _direct_manager(
            management_config, order, generation, herdr_session=herdr_session
        )
        manager["lease_token"] = token

    configuration_errors = cast(list[str], mechanical_validation["errors"])
    if configuration_errors:
        message_type = "needs-requester"
        message = f"Worker configuration is invalid: {'; '.join(configuration_errors)}."
        _notify_requester(ledger, key, order, generation, message_type, message)
        result = _write_required_blocked_bundle(
            order, artifact_manifest, f"mechanical validation: {message}"
        )
        try:
            manager_cleanup = (
                _close_worker_pane(
                    cast(str, manager["pane"]), herdr_session=herdr_session
                )
                if manager["pane"] is not None
                else {
                    "status": "not-created",
                    "worker_pane": None,
                    "verified_absent": True,
                }
            )
        except RecruiterError as error:
            result = _write_required_blocked_bundle(
                order,
                artifact_manifest,
                f"account manager cleanup failed: {error}",
            )
            manager_cleanup = {
                "status": "cleanup-failed",
                "worker_pane": manager["pane"],
                "verified_absent": False,
                "reason": str(error),
            }
        manager_launch_id = manager.get("launch_id")
        if isinstance(manager_launch_id, str):
            ledger.mark_launch_closed(
                key,
                manager_launch_id,
                cast(str | None, manager["pane"]),
                manager_cleanup,
                expected_lease_token=token,
            )
        cleanup = {
            "manager": manager_cleanup,
            "status": "not-created",
            "worker_pane": None,
            "verified_absent": manager_cleanup["verified_absent"],
        }
        ledger.finalize(
            key,
            token,
            order,
            result,
            cleanup=cleanup,
            exit_code=1,
            completion_source="mechanical-validation",
        )
        return 1

    inactivity_lock = threading.Lock()
    owned_worker_pane: str | None = None
    owned_worker_address: str | None = None
    worker_launches: list[tuple[str, str]] = []

    def start_worker(
        name: str,
        execution_order: dict,
        launch: str,
        **kwargs: object,
    ) -> tuple[str, str | None, str]:
        pane, workspace_id, address, launch_id = _start_fenced_ledger_agent(
            ledger,
            key,
            token,
            "worker",
            name,
            execution_order,
            launch,
            tab_role=cast(str | None, kwargs.get("tab_role")),
            herdr_session=herdr_session,
            metadata={"attempt": len(worker_launches) + 1, "generation": generation},
        )
        worker_launches.append((launch_id, pane))
        return pane, workspace_id, address

    def start_monitor(
        worker_pane: str, workspace_id: str | None, worker_address: str
    ) -> threading.Event:
        nonlocal owned_worker_address
        nonlocal owned_worker_pane
        nonlocal monitor
        owned_worker_address = worker_address
        owned_worker_pane = worker_pane

        def check_inactivity(check_number: int) -> None:
            assert manager is not None
            with inactivity_lock:
                try:
                    _run_one_shot_checker(
                        ledger,
                        key,
                        order,
                        manager,
                        worker_pane,
                        worker_result_path,
                        check_number,
                    )
                except (RecruiterError, LifecycleError, OSError) as error:
                    ledger._event(
                        key,
                        "worker-check-failed",
                        check_number=check_number,
                        reason=str(error),
                    )
                    _notify_requester(
                        ledger,
                        key,
                        order,
                        generation,
                        "worker-check-failed",
                        f"Lifecycle check {check_number} failed: {error}",
                        {"check_number": check_number, "worker_pane": worker_pane},
                    )

        monitor = _start_completion_monitor(
            order,
            worker_result_path,
            timeout_ms,
            inactivity_check_ms=management_config.inactivity_check_ms,
            on_inactivity=check_inactivity,
            artifact_manifest=artifact_manifest,
        )
        return monitor[1]

    def handle_timeout(
        timeout_number: int, finalized: threading.Event | None
    ) -> int | None:
        assert manager is not None
        assert owned_worker_pane is not None
        return _await_requester_timeout_decision(
            ledger,
            key,
            token,
            order,
            manager,
            owned_worker_pane,
            timeout_number,
            finalized,
        )

    def finish_monitor_before_cleanup() -> bool | None:
        if monitor is not None:
            monitor[0].set()
            # A checker is bounded by its own startup/response timeouts. Taking this lock prevents
            # worker cleanup from racing an in-flight evidence read.
            with inactivity_lock:
                pass
            monitor[2].join()
        return _complete_typed_bundle(
            ledger,
            key,
            order,
            artifact_manifest,
            owned_worker_address,
            herdr_session=herdr_session,
        )

    def worker_healthy(evidence: dict[str, object]) -> None:
        assert manager is not None
        if manager["pane"] is None:
            if not ledger.mark_worker_healthy(key, token, evidence):
                raise RecruiterError(
                    "lease ownership changed before worker health could be published"
                )
            _notify_requester(
                ledger,
                key,
                order,
                generation,
                "worker-healthy",
                "Python verified the expected worker process, harness, and working directory (direct lifecycle; no manager assessment).",
                evidence,
            )
            return
        try:
            assessment = _ask_manager_about_startup(
                ledger, key, order, manager, evidence
            )
        except (RecruiterError, LifecycleError, OSError) as error:
            # Python has already proved pane/process/agent/cwd health. The LLM assessment adds
            # semantic context, but a missing or malformed advisory response must not tear down a
            # mechanically healthy worker. Persist and surface the degraded observation instead.
            ledger._event(
                key,
                "worker-startup-assessment-degraded",
                reason=str(error),
                worker_pane=owned_worker_pane,
            )
            if not ledger.mark_worker_healthy(key, token, evidence):
                raise RecruiterError(
                    "lease ownership changed before degraded worker health could be published"
                ) from error
            _notify_requester(
                ledger,
                key,
                order,
                generation,
                "worker-healthy-degraded",
                "Python verified the expected worker process, harness, and working directory, "
                f"but the Account Manager startup assessment was unavailable: {error}. "
                "The worker is continuing; do not retry solely for this advisory failure.",
                {"assessment_error": str(error), **evidence},
            )
            return
        if assessment.assessment not in ("healthy", "completed"):
            # The assessment is evidence for the requester, never authority over a Python-valid
            # startup. Keep the worker running and mark supervision degraded.
            ledger._event(
                key,
                "worker-startup-advisory",
                assessment=assessment.assessment,
                message=assessment.message,
            )
            if not ledger.mark_worker_healthy(key, token, evidence):
                raise RecruiterError(
                    "lease ownership changed before advisory worker health could be published"
                )
            _notify_requester(
                ledger,
                key,
                order,
                generation,
                "worker-healthy-advisory",
                assessment.message,
                {"assessment": assessment.assessment, **evidence},
            )
            return
        if not ledger.mark_worker_healthy(key, token, evidence):
            raise RecruiterError(
                "lease ownership changed before worker health could be published"
            )
        _notify_requester(
            ledger,
            key,
            order,
            generation,
            "worker-healthy",
            assessment.message,
            evidence,
        )

    def run_order_once(attempt: int) -> tuple[int, dict, dict[str, object]]:
        before = len(worker_launches)
        outcome = _run_order(
            str(ledger.request_dir(key) / "request.json"),
            roster_path,
            worker_result_path,
            start_monitor,
            ledger.worker_instructions_path(key, token),
            worker_healthy,
            handle_timeout,
            finish_monitor_before_cleanup,
            attempt=attempt,
            herdr_session=herdr_session,
            start_worker_agent=start_worker,
            artifact_manifest=artifact_manifest,
        )
        if len(worker_launches) > before:
            launch_id, pane = worker_launches[-1]
            ledger.mark_launch_closed(
                key,
                launch_id,
                pane,
                outcome[2],
                expected_lease_token=token,
            )
        return outcome

    result_code, result, cleanup = run_order_once(1)
    if (
        result_code != 0
        and cleanup.get("startup_validated") is False
        and not cleanup.get("startup_rejected")
        and management_config.rescue_on_startup_failure
    ):
        failure_reason = str(
            result.get("reason", "worker launch failed before health verification")
        )
        advice = _startup_rescue_advice(ledger, key, order, manager, failure_reason)
        if advice == "retry-startup":
            ledger._event(key, "startup-rescue-relaunch", failure=failure_reason)
            _notify_requester(
                ledger,
                key,
                order,
                generation,
                "startup-rescue",
                "The worker launch failed before health verification. The rescue broker "
                "advised one retry; relaunching now.",
                {"failure": failure_reason},
            )
            result_code, result, cleanup = run_order_once(2)
        else:
            _notify_requester(
                ledger,
                key,
                order,
                generation,
                "startup-rescue-declined",
                "The worker launch failed before health verification and the rescue "
                f"broker advised '{advice}' instead of a retry.",
                {"advice": advice, "failure": failure_reason},
            )
    try:
        manager_cleanup = (
            _close_worker_pane(cast(str, manager["pane"]), herdr_session=herdr_session)
            if manager["pane"] is not None
            else {"status": "not-created", "worker_pane": None, "verified_absent": True}
        )
    except RecruiterError as error:
        result = _write_required_blocked_bundle(
            order,
            artifact_manifest,
            f"account manager cleanup failed: {error}",
        )
        result_code = 1
        manager_cleanup = {
            "status": "cleanup-failed",
            "worker_pane": manager["pane"],
            "verified_absent": False,
            "reason": str(error),
        }
    manager_launch_id = manager.get("launch_id")
    if isinstance(manager_launch_id, str):
        ledger.mark_launch_closed(
            key,
            manager_launch_id,
            cast(str | None, manager["pane"]),
            manager_cleanup,
            expected_lease_token=token,
        )
    cleanup["manager"] = manager_cleanup
    cleanup["verified_absent"] = (
        cleanup["verified_absent"] is True
        and manager_cleanup["verified_absent"] is True
    )
    launch_evidence = _launch_receipt_evidence(ledger, key)
    if launch_evidence:
        cleanup["launches"] = launch_evidence
    if result.get("verdict") == "blocked":
        result = _write_required_blocked_bundle(
            order,
            artifact_manifest,
            str(result.get("reason", "worker lifecycle blocked")),
        )
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
    # Requester notification is post-receipt evidence. Await must never wake on an artifact that
    # has not survived public projection and revalidation.
    _notify_requester(
        ledger,
        key,
        order,
        generation,
        "terminal",
        f"Request finished with verdict {result['verdict']} after artifact publication.",
        {"verdict": result["verdict"]},
    )
    marker = "DONE" if cleanup["verified_absent"] else "CLEANUP_FAILED"
    print(f"ORDER {order['order_id']} {marker}", flush=True)
    return result_code


def _find_workspace(workspaces_resp: dict, label: str) -> dict | None:
    """The WorkspaceInfo labeled `label` from a `workspace list` response, or None."""
    for w in workspaces_resp.get("result", {}).get("workspaces", []):
        if w.get("label") == label:
            return w
    return None


def _find_services_workspace(
    workspaces_resp: dict, *, legacy_workspace_id: str | None = None
) -> dict | None:
    """Find current service labels, or an exact legacy workspace recorded by UpAgent."""
    for label in (UNIFIED_WORKSPACE_LABEL, SHARED_SERVICES_WORKSPACE):
        found = _find_workspace(workspaces_resp, label)
        if found is not None:
            return found
    if legacy_workspace_id is None:
        return None
    legacy = _find_workspace(workspaces_resp, LEGACY_UNIFIED_WORKSPACE_LABEL)
    if legacy is not None and legacy.get("workspace_id") == legacy_workspace_id:
        return legacy
    return None


def _workspace_panes(workspace_id: str, herdr_session: str) -> list[dict]:
    panes = (
        _herdr_json(
            "pane", "list", "--workspace", workspace_id, herdr_session=herdr_session
        )
        .get("result", {})
        .get("panes", [])
    )
    return [pane for pane in panes if isinstance(pane, dict)]


def _service_role_pane(panes: Sequence[dict], role_label: str) -> dict | None:
    accepted = {role_label}
    if role_label == UPAGENT_PANE_LABEL:
        accepted.add(LEGACY_RECRUITER_PANE_LABEL)
    matches = [
        pane for pane in panes if pane.get("label") in accepted and pane.get("pane_id")
    ]
    if len(matches) > 1:
        raise RecruiterError(
            "workspace has multiple UpAgent service panes; remove the stale pane before bring-up"
        )
    return matches[0] if matches else None


def _recruit_door_command(roster_path: str) -> str:
    """A strict compatibility door for exactly one order file over the Hub socket."""
    python = shlex.quote(sys.executable)
    script = shlex.quote(str(_canonical_engine_path().with_name("client.py")))
    roster = shlex.quote(str(Path(roster_path).expanduser().resolve()))
    socket_override = command_runtime.getenv("UPAGENT_SOCKET")
    socket_env = (
        f"UPAGENT_SOCKET={shlex.quote(socket_override)} " if socket_override else ""
    )
    return (
        'recruit() { if [ "$#" -ne 1 ]; then '
        "echo 'recruit expects exactly one strict order.json path' >&2; return 2; fi; "
        f'{socket_env}{python} {script} --target recruiter --roster {roster} request -- "$1"; '
        "} # strict socket request door"
    )


def _ensure_role_pane(
    role_label: str, workspace_label: str, herdr_session: str
) -> tuple[str, str, bool, bool]:
    """Resolve ``(workspace_id, pane_id, workspace_created, pane_created)`` for this engine's
    role pane in the services workspace, claiming ONLY a pane labeled `role_label` — never an arbitrary
    pane. Claiming by role label keeps bring-up idempotent and lets the service coexist with a
    run's own panes in the unified workspace without fighting over them:
      - if services are already up under the OTHER mode's label, fail loud (run `just upagent-down`
        first) rather than splitting the services across two workspaces;
      - create the services workspace if it is absent, and label its root pane for my role;
      - if it exists, reuse my role-labeled pane if present, else split a fresh pane off an
        existing one and label it for my role.
    """
    workspaces = _herdr_json("workspace", "list", herdr_session=herdr_session)
    existing = _find_workspace(workspaces, workspace_label)

    # One-time visible-label migration. Only claim the retired generic `herdr` workspace when
    # it contains this service's known pane; an unrelated human workspace with that label is
    # left untouched. Rename in place so run tabs and pane identities survive the upgrade.
    if workspace_label == UNIFIED_WORKSPACE_LABEL:
        legacy = _find_workspace(workspaces, LEGACY_UNIFIED_WORKSPACE_LABEL)
        if legacy is not None and isinstance(legacy.get("workspace_id"), str):
            legacy_panes = _workspace_panes(legacy["workspace_id"], herdr_session)
            if _service_role_pane(legacy_panes, role_label) is not None:
                if existing is not None:
                    raise RecruiterError(
                        "services exist in both the current and retired unified workspaces; "
                        "remove the stale service pane before bring-up"
                    )
                _herdr(
                    "workspace",
                    "rename",
                    legacy["workspace_id"],
                    UNIFIED_WORKSPACE_LABEL,
                    herdr_session=herdr_session,
                )
                existing = {**legacy, "label": UNIFIED_WORKSPACE_LABEL}

    other_labels = (
        (SHARED_SERVICES_WORKSPACE,)
        if workspace_label == UNIFIED_WORKSPACE_LABEL
        else (UNIFIED_WORKSPACE_LABEL, LEGACY_UNIFIED_WORKSPACE_LABEL)
    )
    for other_label in other_labels:
        other = _find_workspace(workspaces, other_label)
        if other is None or not isinstance(other.get("workspace_id"), str):
            continue
        other_panes = _workspace_panes(other["workspace_id"], herdr_session)
        if _service_role_pane(other_panes, role_label) is not None:
            raise RecruiterError(
                f"services are already up in workspace {other_label!r}; "
                "run `just upagent-down` first to switch workspace modes"
            )

    if existing is None:
        created = _herdr_json(
            "workspace",
            "create",
            "--label",
            workspace_label,
            "--no-focus",
            herdr_session=herdr_session,
        )["result"]
        workspace_id = created["workspace"]["workspace_id"]
        pane_id = created["root_pane"]["pane_id"]
        _herdr("pane", "rename", pane_id, role_label, herdr_session=herdr_session)
        return workspace_id, pane_id, True, True

    workspace_id = existing["workspace_id"]
    panes = _workspace_panes(workspace_id, herdr_session)
    mine = _service_role_pane(panes, role_label)
    if mine is not None:
        pane_id = mine["pane_id"]
        if mine.get("label") != role_label:
            _herdr("pane", "rename", pane_id, role_label, herdr_session=herdr_session)
        return workspace_id, pane_id, False, False
    # Split my role pane off an existing pane so the service stays in this tab even when the
    # unified workspace already holds a run's own panes.
    anchor = next((p["pane_id"] for p in panes if p.get("pane_id")), None)
    if anchor is None:
        raise RecruiterError(
            f"services workspace {workspace_id} has no pane to split from"
        )
    new_pane = _herdr_json(
        "pane",
        "split",
        anchor,
        "--direction",
        "down",
        "--no-focus",
        herdr_session=herdr_session,
    )["result"]["pane"]["pane_id"]
    _herdr("pane", "rename", new_pane, role_label, herdr_session=herdr_session)
    return workspace_id, new_pane, False, True


def cmd_up(roster_path: str, *, separate_workspaces: bool = False) -> int:
    """Ensure the services workspace + an armed Recruiter pane. Idempotent.

    Default is the unified `upagent` workspace (services and every run's tabs share it);
    `--separate-workspaces` restores the dedicated `shared-services` workspace. The pane is a
    visible status surface ONLY; requesters submit through the CLI and durable ledger. The
    pane's `recruit()` function is armed as a sealed stub that refuses and names the real
    doors — pane text is not a message queue. Persists workspace, mode, pane, roster, and
    supervisor ownership to STATE_FILE.
    """
    # Validate the roster up front so a missing/bad roster fails loudly at bring-up, not
    # silently at the first hire.
    load_roster(roster_path)
    workspace_label = (
        SHARED_SERVICES_WORKSPACE if separate_workspaces else UNIFIED_WORKSPACE_LABEL
    )
    herdr_session = _resolve_current_herdr_session_name()
    workspace_id, recruiter_pane, workspace_created, pane_created = _ensure_role_pane(
        UPAGENT_PANE_LABEL, workspace_label, herdr_session
    )
    if not separate_workspaces:
        # Presentation-only: keep the Recruiter in the `services` role tab (joined when present,
        # created otherwise) so the unified-workspace sidebar reads services / control / workers
        # / oversight. A placement failure warns and never fails bring-up.
        try:
            recruiter_pane = _place_started_agent_in_role_tab(
                recruiter_pane,
                workspace_id,
                SERVICES_TAB_LABEL,
                split_direction="down",
                herdr_session=herdr_session,
            )
        except RecruiterError as error:
            _layout_warning("services", recruiter_pane, str(error))

    # Arm the narrow compatibility function. It accepts one opaque path and calls the verified
    # request door; arbitrary pane text and extra arguments still cannot become a hire.
    _herdr(
        "pane",
        "run",
        recruiter_pane,
        _recruit_door_command(roster_path),
        herdr_session=herdr_session,
    )

    supervisor_token = uuid.uuid4().hex
    state = {
        "workspace_id": workspace_id,
        "workspace_label": workspace_label,
        "herdr_session": herdr_session,
        "ownership": {
            "pane": {
                "pane_id": recruiter_pane,
                "state": "created" if pane_created else "adopted",
            },
        },
        "separate_workspaces": separate_workspaces,
        "recruiter_pane": recruiter_pane,
        "roster": roster_path,
        "supervisor_token": supervisor_token,
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    JobLedger._write_json(STATE_FILE, state)
    supervisor = threading.Thread(
        target=command_runtime.run_detached,
        args=(cmd_supervise, supervisor_token),
        name="upagent-supervisor",
        daemon=True,
    )
    supervisor.start()
    # A process id, not a transferable child authority: the supervisor dies with the Hub.
    state["supervisor_pid"] = os.getpid()
    JobLedger._write_json(STATE_FILE, state)
    # Surface the broker in Herdr's agents sidebar so "up" is visible, not just a shell.
    _report_state(
        recruiter_pane,
        "idle",
        "armed — waiting for work orders",
        herdr_session=herdr_session,
    )
    # Preserve the public meaning of `reused`: the services workspace pre-existed.
    print(json.dumps({**state, "reused": not workspace_created}))
    return 0


def _recruiter_pane_cleanup_decision(
    state: dict, recruiter_pane: object
) -> dict[str, object]:
    if not isinstance(recruiter_pane, str) or not recruiter_pane:
        return {
            "status": "not-created",
            "worker_pane": None,
            "verified_absent": True,
        }
    ownership = state.get("ownership")
    pane = ownership.get("pane") if isinstance(ownership, dict) else None
    if not isinstance(pane, dict):
        raise RecruiterError(
            "Recruiter teardown requires structural pane ownership; inspect the state file "
            "and repair or remove it only after confirming the recorded pane is safe"
        )
    pane_id = pane.get("pane_id")
    pane_state = pane.get("state")
    if pane_id != recruiter_pane:
        raise RecruiterError("Recruiter teardown pane ownership does not match state")
    if pane_state == "adopted":
        return {
            "status": "skipped-adopted",
            "worker_pane": recruiter_pane,
            "verified_absent": False,
        }
    if pane_state != "created":
        raise RecruiterError("Recruiter teardown pane ownership is not created/adopted")
    return {
        "status": "close-created",
        "worker_pane": recruiter_pane,
        "verified_absent": False,
    }


def cmd_down() -> int:
    """Terminalize all owned jobs, close the Recruiter pane, and retire its supervisor."""
    cmd_reconcile(force=True)
    try:
        state = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        state = {}
    except (OSError, json.JSONDecodeError) as error:
        raise RecruiterError(f"Recruiter state is invalid: {error}") from error
    if not isinstance(state, dict):
        raise RecruiterError("Recruiter state must be an object")
    recruiter_pane = state.get("recruiter_pane") if isinstance(state, dict) else None
    cleanup = _recruiter_pane_cleanup_decision(state, recruiter_pane)
    if cleanup["status"] == "close-created":
        herdr_session = _recorded_herdr_session(
            state.get("herdr_session"), "Recruiter teardown"
        )
        try:
            _herdr(
                "pane",
                "close",
                cast(str, recruiter_pane),
                herdr_session=herdr_session,
            )
        except RecruiterError:
            if cast(str, recruiter_pane) in _live_pane_ids(herdr_session=herdr_session):
                raise
        cleanup = {
            "herdr_session": herdr_session,
            "status": "closed",
            "worker_pane": recruiter_pane,
            "verified_absent": True,
        }
    STATE_FILE.unlink(missing_ok=True)
    print(
        json.dumps(
            {"cleanup": cleanup, "down": True, "recruiter_pane": recruiter_pane},
            sort_keys=True,
        )
    )
    return 0


def cmd_status() -> int:
    if STATE_FILE.is_file():
        try:
            state = json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RecruiterError(f"Recruiter state is invalid: {error}") from error
        if not isinstance(state, dict):
            raise RecruiterError("Recruiter state must be an object")
        herdr_session = _recorded_herdr_session(
            state.get("herdr_session"), "Recruiter status"
        )
    else:
        state = None
        herdr_session = None
    legacy_workspace_id = None
    if (
        state is not None
        and state.get("workspace_label") == LEGACY_UNIFIED_WORKSPACE_LABEL
    ):
        recorded_workspace_id = state.get("workspace_id")
        if isinstance(recorded_workspace_id, str):
            legacy_workspace_id = recorded_workspace_id
    services = _find_services_workspace(
        _herdr_json("workspace", "list", herdr_session=herdr_session),
        legacy_workspace_id=legacy_workspace_id,
    )
    label = services.get("label") if services else None
    print(f"services: {'up (' + str(label) + ')' if services else 'down'}")
    if state is not None:
        print(STATE_FILE.read_text())
    return 0


# --- the phone book -----------------------------------------------------------


PHONE_BOOK_DESCRIPTION_CAP = 220
# The whole rendered line, name and markers included — the budget that actually rides in every
# stage brief. A description cap alone is not the same bound: a 29-character specialist name
# with a `(this repo)` marker adds 48 characters of prefix in front of it.
PHONE_BOOK_LINE_CAP = 260


def cmd_specialists(as_json: bool = False) -> int:
    """Print the merged specialist roster.

    Default: the paste-ready stage-brief block (the phone book) that a phase leader embeds
    VERBATIM in every worker's instructions.md, so a worker never has to discover consulting on
    its own. `--json`: the raw merged index.
    """
    roster = load_specialist_roster()
    index = _specialist_index(roster)
    if as_json:
        print(json.dumps(index, indent=2))
        return 0
    lines = [
        "## Repo specialists — consult before deciding (MANDATORY where a specialist owns the area)",
        "",
        "Conventions are asked, never guessed. Before deciding anything in an area listed below,",
        "consult its specialist through UpAgent. Record every consult in your result.json",
        "`consults` list (empty list when none applied); a skipped mandated consult is a blocking",
        "audit finding.",
        "",
    ]
    for name, entry in index.items():
        origin = " (this repo)" if entry.get("origin") == "this-repo" else ""
        # This block rides inside EVERY stage brief of every phase, so an unbounded roster
        # description is context spent on every worker the run ever hires. One line each, hard
        # capped, whatever the roster says.
        #
        # The LINE is what is capped, not just the description: a long specialist name plus the
        # `(this repo)` marker is 40-odd characters of prefix, so capping the description alone
        # lets the rendered line run well past the budget on a real roster.
        prefix = f"- **{name}**{origin} — "
        budget = min(PHONE_BOOK_DESCRIPTION_CAP, PHONE_BOOK_LINE_CAP - 1 - len(prefix))
        description = (
            str(entry.get("description") or "(no description)").strip().split("\n")[0]
        )
        if len(description) > budget:
            description = description[: max(0, budget - 3)].rstrip() + "..."
        lines.append(prefix + description)
    lines += [
        "",
        "How to consult (files + one blocking command; NEVER paste a question into any pane):",
        "1. Write <your-cwd>/consults/<consult-id>.json with:",
        '   {"consult_id": "<unique-id>", "specialist": "<name above>",',
        '    "question": "<one specific question>", "answer_path": "<absolute path for answer.json>",',
        '    "requested_by": "<YOUR OWN order_id>"}',
        "   `requested_by` must be your order_id exactly. It is how the Hub files the consult",
        "   under you; omit it and your consult still runs, but it can never be credited to you.",
        "2. Run `just upagent-consult <that consult.json>`. It BLOCKS until the consult is",
        "   terminal and prints one CONSULT_RECEIPT line — the command returning IS the signal,",
        "   so there is no sentinel to wait for. An answer is always written, failures included.",
        "3. Read answer_path: a success answer carries file:line citations; a failure carries `error`.",
        "4. Record {consult_id, specialist, request_id, answer_path} under `consults` in your",
        "   result.json. `request_id` is the ordinary UpAgent request id the receipt names, and",
        "   the Recruiter resolves every entry against its own record of what it brokered: a",
        "   consult you did not actually make is published as `consults_unverified`.",
        "",
        "`just upagent-specialists` reprints this block.",
    ]
    print("\n".join(lines))
    return 0


# --- the consult door ---------------------------------------------------------
#
# One question in, one cited answer out. Everything between is an ORDINARY UpAgent order: the
# door builds it, hands it to `cmd_dispatch` in-process, and the unchanged generic lifecycle
# runs it. The order carries no consult-specific field — `_complete_order_form` rejects any key
# outside `ORDER_INTAKE_ALIASES`, so a `consult` block in the order would be a latent failure
# with a long fuse. The consult<->order link lives in the sidecar receipt, keyed by order_id.


def _resolve_specialist_name(
    value: str, roster_names: list[str]
) -> tuple[str | None, str | None]:
    """(resolved_name, note) when `value` matches exactly one roster name under deterministic
    normalizations; (None, None) when ambiguous or unmatched — which is a refusal listing the
    roster, never a guess. This fills in the envelope, never the intent: a capitalisation must
    not cost an LLM hire, and an unrecognizable name must not be quietly resolved to something
    the caller did not ask for."""
    if value in roster_names:
        return value, None
    lowered = value.lower()
    case_matches = [n for n in roster_names if n.lower() == lowered]
    if len(case_matches) == 1:
        return case_matches[
            0
        ], f"specialist {value!r} matched {case_matches[0]!r} (case)"
    stripped = lowered.removesuffix("-agent")
    suffix_matches = [
        n for n in roster_names if n.lower() in (stripped, f"{stripped}-agent")
    ]
    if len(set(suffix_matches)) == 1:
        resolved = suffix_matches[0]
        return resolved, f"specialist {value!r} matched {resolved!r} (-agent suffix)"
    return None, None


def _resolve_consult_cwd(roster: dict, consult: dict, consult_id: str) -> str:
    """Pick a directory the specialist can actually be started in.

    The rule is deliberately dull: run in the repository the roster came from, unless the caller
    named a directory that exists. `roster["repo_root"]` is re-derived on every load from the
    roster file this call resolved, so it is live by construction.

    What this replaces: the hub used to inherit a repo_root frozen into services state at `up`
    time. Bring services up inside a throwaway worktree, delete the worktree, and every consult
    afterwards died starting a process in a directory that no longer existed (`fe96fba`).
    Consults are read-and-cite work; there is no reason to pin them to a transient checkout.
    """
    requested = consult.get("cwd")
    if requested and Path(requested).is_dir():
        return str(requested)
    root = str(roster["repo_root"])
    if requested:
        command_runtime.write_stderr(
            f"upagent-consult: consult {consult_id}: requested cwd {requested} does not "
            f"exist; running in {root}\n"
        )
    return root


def build_consult_brief(consult: dict, location: str, cwd: str) -> str:
    """The briefing a transient specialist reads: the question, where its own definition and the
    repo live, and the exact answer.json contract it must satisfy (answer + file:line citations).

    Two contracts stack here and both are load-bearing. This one produces the consult's PRODUCT.
    `_write_worker_instructions` then appends the Recruiter delivery contract, which produces the
    lifecycle RECEIPT. The closing sentence hands the worker from one to the other.
    """
    return (
        f"You are the '{consult['specialist']}' specialist answering ONE consult. "
        f"Load only your own definition at {location} and inspect the repo at {cwd}. "
        f"Question: {consult['question']} "
        "Answer concisely from your domain, then write STRICT JSON to the lease-private "
        "answer path in the final Recruiter delivery contract appended below "
        f'with keys: "consult_id": "{consult["consult_id"]}", "answer": "<your answer>", '
        f'"citations": ["path/to/file:line", ...]. Every claim MUST carry a real file:line '
        f"citation into the repo. Write nothing outside that JSON file.\n"
        "After the cited answer is durable, satisfy the Recruiter delivery contract appended "
        "to this brief and exit.\n"
    )


def consult_request_id(consult_id: str) -> str:
    """The ordinary UpAgent request identity for one consult.

    A digest, not the caller's string: `_SAFE_ORDER_ID_RE` admits only `[A-Za-z0-9._-]`, and a
    digest is safe by construction whatever the caller put in `consult_id`. This is the id a
    `consults` receipt names, and the id the Recruiter's own ledger can be asked about.
    """
    return f"consult-{hashlib.sha256(consult_id.encode()).hexdigest()[:24]}"


def consult_artifact_paths(consult_path: str | Path) -> dict[str, Path]:
    """Every artifact a consult produces, beside the consult.json that asked for it."""
    path = Path(consult_path)
    return {
        "brief": path.with_name(path.name + ".brief.md"),
        "order": path.with_name(path.name + ".order.json"),
        "result": path.with_name(path.name + ".upagent-result.json"),
        "compacted": path.with_name(path.name + ".compacted.md"),
        "handoff": path.with_name(path.name + ".handoff.md"),
        "receipt": path.with_name(path.name + ".receipt.json"),
    }


def build_consult_order(
    consult: dict,
    entry: dict,
    artifacts: dict[str, Path],
    *,
    cwd: str,
    cockpit_pane: str,
) -> dict:
    """One ordinary order. Every key below is an existing `contracts.parse_order` key.

    `stage_id` is inert: a consult is not a phase stage, but `parse_order` requires a member of
    RECOGNIZED_STAGE_IDS, and adding `stage-consult` to that enum would legitimise
    `revisit: ["stage-consult"]` — meaningless — and ripple into the route schema and the
    five-stage protocol. `timeout_ms` is set explicitly, so the stage never decides anything.

    No `management` key: consults follow the roster default. The historical per-consult
    dedicated broker duplicated Python's own checks with an idle LLM pane per question.
    """
    request_id = consult_request_id(consult["consult_id"])
    return {
        "order_id": request_id,
        "request_id": request_id,
        "requester": {
            "id": "upagent-consult",
            "kind": "file-mailbox",
            "address": str(artifacts["receipt"]),
        },
        "phase_id": CONSULT_PHASE_ID,
        "stage_id": "stage-5-finalization",
        "harness": entry["offering_snapshot"]["harness"],
        "model": entry["offering_snapshot"]["model"],
        "agent": entry["agent"],
        "effort": entry["effort"],
        "offering_snapshot": entry["offering_snapshot"],
        "cwd": cwd,
        "instructions_path": str(artifacts["brief"]),
        "result_path": str(artifacts["result"]),
        "cockpit_pane": cockpit_pane,
        "timeout_ms": CONSULT_TIMEOUT_MS,
        "artifact_publication": {
            "schema_version": 1,
            "compacted_path": str(artifacts["compacted"]),
            "handoff_path": str(artifacts["handoff"]),
            "answer_path": consult["answer_path"],
            "consult_id": consult["consult_id"],
            "consult_payload_sha256": hashlib.sha256(
                json.dumps(consult, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "mandatory_consults": [],
        },
    }


def _recover_consult_fields(consult_path: str) -> tuple[str, str] | None:
    """Best-effort (consult_id, answer_path) from a malformed consult.json, so the door can
    still leave a failure answer instead of stranding the caller. None when the file is too
    broken to recover either field. Mirrors the Recruiter's own `_recover_order_fields`."""
    try:
        raw = json.loads(Path(consult_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    consult_id, answer_path = raw.get("consult_id"), raw.get("answer_path")
    if (
        isinstance(consult_id, str)
        and consult_id
        and isinstance(answer_path, str)
        and answer_path
    ):
        return consult_id, answer_path
    return None


def _write_failure_answer(answer_path: str, consult_id: str, reason: str) -> None:
    """Atomically project and revalidate a legible FAILURE answer before any receipt."""
    path = Path(answer_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(
        path,
        (
            json.dumps(
                contracts_consult.failure_answer(
                    consult_id, f"upagent-consult: {reason}"
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    contracts_consult.load_answer(path, expected_consult_id=consult_id)


def consult_index_dir() -> Path:
    """The Hub's own record of the consults it brokered. Resolved from `STATE_FILE` at call
    time, never frozen at import, so it follows the Recruiter's state root."""
    return STATE_FILE.parent / "consults"


def consult_index_entry_path(requested_by: str, consult_id: str) -> Path:
    """Where the Hub records that IT brokered this consult for this requester.

    Under the Recruiter's own state root, not the caller's tree: the point of the index is that
    the worker's `result.json` is not the only account of what happened. Both path segments are
    digests — `consult_id` is caller-controlled and would otherwise be a traversal into the
    Hub's state directory, and the request-id digest is the identity being verified anyway.
    """
    requester_key = hashlib.sha256(requested_by.encode()).hexdigest()[:16]
    return (
        consult_index_dir() / requester_key / f"{consult_request_id(consult_id)}.json"
    )


def _recorded_consult(requested_by: object, claim: dict) -> dict | None:
    """The Hub's own record of one claimed consult, or None when nothing backs the claim."""
    consult_id = claim.get("consult_id")
    if not isinstance(requested_by, str) or not requested_by:
        return None
    if not isinstance(consult_id, str) or not consult_id:
        return None
    try:
        recorded = json.loads(
            consult_index_entry_path(requested_by, consult_id).read_text()
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(recorded, dict):
        return None
    # Only a consult whose order actually finished is verifiable. The write side already keeps
    # unfinished consults out of the index; refusing one here too means the forgery guarantee
    # holds even against an index entry some other path may have written.
    if recorded.get("order_receipt_state") != "finished":
        return None
    # The digest in the filename already binds requester and consult_id, so these two compare
    # the CONTENT of the claim against the content of the record: a worker naming a consult that
    # really happened but attaching someone else's request id does not get a pass.
    if recorded.get("consult_id") != consult_id:
        return None
    if recorded.get("request_id") != claim.get("request_id"):
        return None
    return recorded


def resolve_consult_claims(order: dict, result: dict) -> dict[str, list]:
    """Split a worker's claimed consults into the ones the Hub can confirm and the ones it cannot.

    WHAT THIS CLOSES: forgery. A `consults` entry used to be four keys of prose that nothing
    ever read, so a worker could bank a receipt for a consultation that never happened. Now each
    claim has to resolve to an entry the CONSULT DOOR wrote, under the Recruiter's own state
    root, keyed by the requester — and only a consult whose order actually RAN TO COMPLETION
    (`order_receipt_state == "finished"`) creates one. A consult rejected before a specialist
    ran — unknown specialist, Recruiter down, dispatch failure — leaves a rejected receipt but no
    index entry, so it can never be verified. A consult whose worker ran stays verifiable whatever
    verdict its answer earned — `cited`, a specialist-signaled `failed`, or a citation-gate
    `rejected` — because its order finished.

    WHAT THIS DOES NOT CLOSE, and no mechanism here could: whether a consult SHOULD have
    happened. Judging that means judging whether the work touched an area a listed specialist
    owns, which stays the Stage 2 auditor's call. A worker can also ask something trivial and
    bank a valid receipt; the cost of a fake rises from one line of JSON to actually hiring a
    specialist, which is a real raise and not a proof of diligence.

    Every field of a verified entry is read from the Hub's record, never echoed from the claim.
    Advisory by construction: this reports, and never raises into publication — a bookkeeping
    fault must not destroy a result that represents real work.
    """
    claims = result.get("consults")
    if not isinstance(claims, list):
        return {}
    requested_by = order.get("order_id")
    verified: list[dict] = []
    unverified: list[dict] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        recorded = _recorded_consult(requested_by, claim)
        if recorded is None:
            unverified.append(
                {
                    "consult_id": claim.get("consult_id"),
                    "request_id": claim.get("request_id"),
                }
            )
            continue
        verified.append(
            {
                "answer_verdict": recorded.get("answer_verdict"),
                "consult_id": recorded.get("consult_id"),
                "request_id": recorded.get("request_id"),
                "specialist": recorded.get("resolved_specialist")
                or recorded.get("specialist"),
            }
        )
    return {"consults_verified": verified, "consults_unverified": unverified}


def _record_consult_in_index(receipt: dict) -> Path | None:
    """Record a brokered consult under its requester, or explain why it could not be.

    Only a consult that actually RAN TO COMPLETION is indexed. `order_receipt_state` becomes
    "finished" at exactly one point — after `cmd_dispatch` returns a durable ORDER_RECEIPT — so
    this admits every consult whose worker ran, whatever its answer earned: `cited`, a
    specialist-signaled failure (`answer_verdict == "failed"`), or an answer the citation gate
    rejected (`answer_verdict == "rejected"`). It excludes only a consult rejected BEFORE a
    specialist ran: an unknown specialist, a Recruiter that is down, or a dispatch failure.
    Indexing one of those would let a worker bank a `consults` receipt for a consultation that
    never happened.

    A consult with no `requested_by` is likewise not indexed and therefore not later verifiable.
    Both omissions fail in the SAFE direction: a worker can only ever understate its own
    consulting, never overstate it.
    """
    if receipt.get("order_receipt_state") != "finished":
        return None
    requested_by = receipt.get("requested_by")
    if not isinstance(requested_by, str) or not requested_by:
        return None
    path = consult_index_entry_path(requested_by, str(receipt["consult_id"]))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_atomic(
            path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
        )
    except OSError as e:
        # Loud, because the consequence lands on someone else: an unrecorded consult makes an
        # honest worker's claim read as unverified.
        command_runtime.write_stderr(
            f"upagent-consult: could not index consult at {path}: {e}\n"
        )
        return None
    return path


def _publish_consult_receipt(receipt: dict, receipt_path: Path) -> None:
    """Publish the consult's own terminal record — beside the consult.json for the caller, and
    into the Hub's requester-keyed index for the audit.

    Every field derives from an ordinary UpAgent request id or result path plus the durable
    ORDER_RECEIPT the generic lifecycle already produced — nothing here is invented.
    """
    indexed = _record_consult_in_index(receipt)
    if indexed is not None:
        receipt = {**receipt, "index_path": str(indexed)}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(
        receipt_path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    )
    print(f"CONSULT_RECEIPT {json.dumps(receipt, sort_keys=True)}", flush=True)


def _roster_names_or_reason() -> str:
    """The phone book as one line for an error message, or why it could not be read."""
    try:
        return ", ".join(_specialist_index(load_specialist_roster())) or "(empty)"
    except RecruiterError as e:
        return f"(roster unavailable: {e})"


def cmd_consult(consult_path: str, roster_path: str) -> int:
    """Route one question into an ordinary UpAgent order and validate the answer it produces.

    Guarantee: once the consult_id is known, every recoverable path atomically leaves an
    answer.json (real or failure), revalidates it, and only then publishes a receipt. A filesystem
    fault propagates before receipt rather than advertising an answer that was not published. The
    command BLOCKS, and its returning is the completion signal: there is no
    sentinel, because `cmd_dispatch` already blocks for the durable ORDER_RECEIPT.

    Everything that can fail lives inside the recoverable block once consult_id is known —
    including the roster load — so a bad roster, an unknown specialist or a Herdr fault still
    resolves the caller instead of raising past it.
    """
    artifacts = consult_artifact_paths(consult_path)
    try:
        consult = contracts_consult.load_consult(consult_path)
    except ConsultError as strict_error:
        recovered = _recover_consult_fields(consult_path)
        if recovered is None:
            # Nothing to answer INTO. This is the one path with no durable artifact to leave,
            # so it is the one path that refuses in Python — naming what was missing.
            raise RecruiterError(
                f"{strict_error}. A consult needs at least `consult_id` and `answer_path` "
                f"before anything can be answered into it. Roster: {_roster_names_or_reason()}"
            ) from strict_error
        consult_id, answer_path = recovered
        reason = f"{strict_error}. Roster: {_roster_names_or_reason()}"
        _write_failure_answer(answer_path, consult_id, reason)
        _publish_consult_receipt(
            {
                "answer_path": answer_path,
                "answer_verdict": "rejected",
                "consult_id": consult_id,
                "reason": reason,
            },
            artifacts["receipt"],
        )
        return 0

    consult_id = consult["consult_id"]
    answer_path = consult["answer_path"]
    request_id = consult_request_id(consult_id)
    _log_request_event(
        "CONSULT_START",
        {
            "answer_path": answer_path,
            "consult_id": consult_id,
            "consult_path": consult_path,
            "requested_by": consult.get("requested_by"),
            "request_id": request_id,
            "specialist": consult["specialist"],
        },
    )
    receipt: dict[str, object] = {
        "answer_path": answer_path,
        "consult_id": consult_id,
        "order_id": request_id,
        "request_id": request_id,
        "requested_by": consult.get("requested_by"),
        "result_path": str(artifacts["result"]),
        "specialist": consult["specialist"],
    }
    try:
        roster = load_specialist_roster()
        index = _specialist_index(roster)
        resolved, note = _resolve_specialist_name(consult["specialist"], list(index))
        if resolved is None:
            raise RecruiterError(
                f"unknown specialist {consult['specialist']!r}; roster: "
                f"{', '.join(index) or '(empty)'}"
            )
        receipt["resolved_specialist"] = resolved
        if note is not None:
            # A silent rename is never invisible: the interpretation is always recorded.
            receipt["resolution_note"] = note
        entry = index[resolved]

        cockpit_pane = _recruiter_pane_from_state()
        if not cockpit_pane:
            raise RecruiterError(
                f"the Recruiter is not up (no recruiter pane in {STATE_FILE}); "
                "run `just upagent-up` first"
            )
        cwd = _resolve_consult_cwd(roster, consult, consult_id)
        receipt["cwd"] = cwd
        location = "(no definition file)"
        if entry.get("location"):
            location = str(
                _resolve_specialist_path(
                    entry["location"], roster["repo_root"], "specialist location"
                )
            )

        artifacts["brief"].parent.mkdir(parents=True, exist_ok=True)
        artifacts["brief"].write_text(
            build_consult_brief({**consult, "specialist": resolved}, location, cwd)
        )
        # Any answer.json left at this path by a PRIOR consult goes before launch, so the only
        # answer readable afterwards is the one this specialist wrote.
        Path(answer_path).unlink(missing_ok=True)
        order = build_consult_order(
            consult, entry, artifacts, cwd=cwd, cockpit_pane=cockpit_pane
        )
        artifacts["result"].unlink(missing_ok=True)
        _write_bytes_atomic(
            artifacts["order"],
            (json.dumps(order, indent=2, sort_keys=True) + "\n").encode(),
        )
        _log_request_event(
            "CONSULT_ORDER_WRITTEN",
            {
                "answer_path": answer_path,
                "consult_id": consult_id,
                "order_id": order["order_id"],
                "order_path": str(artifacts["order"]),
                "requested_by": consult.get("requested_by"),
                "result_path": str(artifacts["result"]),
                "specialist": resolved,
            },
        )

        # In-process, no subprocess hop: the door is a caller of the ordinary lifecycle, not a
        # second one. This blocks until the durable ORDER_RECEIPT exists.
        cmd_dispatch(str(artifacts["order"]), roster_path)
        receipt["order_receipt_state"] = "finished"
        _log_request_event(
            "CONSULT_WORKER_DONE",
            {
                "answer_path": answer_path,
                "consult_id": consult_id,
                "order_id": order["order_id"],
                "requested_by": consult.get("requested_by"),
                "specialist": resolved,
            },
        )

        # THE CITATION GATE. `parse_result` already said the worker ran and delivered; this says
        # the answer is backed by evidence. Two questions, two artifacts, both required.
        answer = contracts_consult.load_answer(
            answer_path, expected_consult_id=consult_id
        )
        receipt["answer_verdict"] = "failed" if answer.get("error") else "cited"
    except (
        RecruiterError,
        ContractError,
        ConsultError,
        LifecycleError,
        ManagementConfigError,
        OSError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
    ) as e:
        receipt["answer_verdict"] = "rejected"
        receipt["reason"] = str(e)
        _write_failure_answer(answer_path, consult_id, str(e))
        command_runtime.write_stderr(
            f"upagent-consult: consult {consult_id} failed: {e}\n"
        )
    # Tier 2 (`consults_verified`) plugs in HERE and nowhere else: a second write of this same
    # receipt into a requester-keyed index under the Recruiter's state root, which order
    # finalization then resolves a worker's claimed `consults` against. Additive — no field
    # below changes, and the door already carries `requested_by` and `request_id`.
    _publish_consult_receipt(receipt, artifacts["receipt"])
    _log_request_event(
        "CONSULT_RECEIPT",
        {
            "answer_path": answer_path,
            "answer_verdict": receipt.get("answer_verdict"),
            "consult_id": consult_id,
            "receipt_path": str(artifacts["receipt"]),
            "requested_by": consult.get("requested_by"),
            "specialist": receipt.get("resolved_specialist", consult.get("specialist")),
        },
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = command_runtime.ArgumentParser(
        prog="recruiter",
        description="UpAgent Recruiter",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--roster",
        default=default_roster_path(),
        help="launch-template roster (default: $UPAGENT_CONFIG, else upagent.yaml next to this file)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_recruit = sub.add_parser(
        "recruit",
        help="submit one strict order file without blocking the Recruiter pane",
    )
    p_recruit.add_argument("order", help="path to one strict order.json")
    p_dispatch = sub.add_parser(
        "dispatch", help="submit an order and block for its durable completion receipt"
    )
    p_dispatch.add_argument("order", help="path to order.json")
    p_request = sub.add_parser(
        "request",
        help="submit one strict order and return after verified worker startup",
    )
    p_request.add_argument("order", help="path to one strict order.json")
    p_await = sub.add_parser(
        "await", help="wait for completion or an owner decision point"
    )
    p_await.add_argument("order", help="path to order.json")
    p_await.add_argument(
        "--notify-after-ms",
        type=int,
        default=600_000,
        help="ping the human when the wait outlives this (default 10 minutes; 0 disables)",
    )
    p_verify = sub.add_parser(
        "verify", help="hire one independent reviewer against a finished order's result"
    )
    p_verify.add_argument("order", help="path to the FINISHED order.json")
    p_verify.add_argument("--harness", default="claude")
    p_verify.add_argument("--model", default="")
    p_verify.add_argument("--agent", default="reviewer")
    p_verify.add_argument("--effort", default="low")
    p_await_any = sub.add_parser(
        "await-any",
        help="block until any watched request moves; print one tagged AWAIT_EVENT",
    )
    p_await_any.add_argument("orders", nargs="+", help="paths to order.json files")
    p_await_any.add_argument("--timeout-ms", type=int, default=600_000)
    p_await_any.add_argument("--cursor", default="{}")
    p_respond = sub.add_parser(
        "respond", help="authorize extension or cancellation as the requester"
    )
    p_respond.add_argument("order", help="path to order.json")
    p_respond.add_argument("control_token", help="control token returned by request")
    p_respond.add_argument("nonce", help="nonce returned by await/timeout warning")
    p_respond.add_argument("action", choices=lifecycle.REQUESTER_ACTIONS)
    p_respond.add_argument(
        "extension_ms", type=int, help="positive for extend; 0 for cancel"
    )
    p_run = sub.add_parser("run-job", help=argparse.SUPPRESS)
    p_run.add_argument("key", help=argparse.SUPPRESS)
    p_supervise = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p_supervise.add_argument("token", help=argparse.SUPPRESS)
    p_up = sub.add_parser("up", help="ensure the services workspace")
    p_up.add_argument(
        "--separate-workspaces",
        action="store_true",
        help="keep services in their own `shared-services` workspace instead of the unified `upagent` one",
    )
    sub.add_parser("down", help="stop the Recruiter and reconcile every owned worker")
    p_reconcile = sub.add_parser(
        "reconcile", help="reconcile dead or expired owned workers"
    )
    p_reconcile.add_argument(
        "--all", action="store_true", help="reconcile every active worker"
    )
    sub.add_parser("status", help="report shared-services state")
    p_specialists = sub.add_parser(
        "specialists",
        help="print the merged specialist roster as a paste-ready brief block",
    )
    p_specialists.add_argument(
        "--json", action="store_true", help="print the raw merged index as JSON"
    )
    p_consult = sub.add_parser(
        "consult", help="ask one specialist one question; blocks for the cited answer"
    )
    p_consult.add_argument("consult", help="path to the consult.json to answer")

    args = parser.parse_args(argv)
    try:
        _require_hub_authority()
        if args.command == "recruit":
            return cmd_recruit(args.order, args.roster)
        if args.command == "dispatch":
            return cmd_dispatch(args.order, args.roster)
        if args.command == "request":
            return cmd_request(args.order, args.roster)
        if args.command == "await":
            return cmd_await(args.order, args.notify_after_ms)
        if args.command == "verify":
            return cmd_verify(
                args.order,
                args.roster,
                harness=args.harness,
                model=args.model,
                agent=args.agent,
                effort=args.effort,
            )
        if args.command == "await-any":
            return cmd_await_any(args.orders, args.timeout_ms, args.cursor)
        if args.command == "respond":
            extension_ms = args.extension_ms if args.action == "extend" else None
            if args.action == "cancel" and args.extension_ms != 0:
                raise RecruiterError("cancel requires extension_ms=0")
            return cmd_respond(
                args.order,
                args.control_token,
                args.nonce,
                args.action,
                extension_ms,
                f"Requester authorized {args.action}.",
            )
        if args.command == "run-job":
            return cmd_run_job(args.key, args.roster)
        if args.command == "supervise":
            return cmd_supervise(args.token)
        if args.command == "up":
            return cmd_up(args.roster, separate_workspaces=args.separate_workspaces)
        if args.command == "down":
            return cmd_down()
        if args.command == "reconcile":
            return cmd_reconcile(force=args.all)
        if args.command == "status":
            return cmd_status()
        if args.command == "specialists":
            return cmd_specialists(as_json=args.json)
        if args.command == "consult":
            return cmd_consult(args.consult, args.roster)
    except IntakeOutcomeError as e:
        # One standardized non-accepted outcome: the machine line, the human reason, and an exit
        # code the caller can branch on without parsing anything.
        _print_intake_outcome(e)
        command_runtime.write_stderr(f"recruiter: {e}\n")
        return e.exit_code
    except RecruiterError as e:
        sys.exit(f"recruiter: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
