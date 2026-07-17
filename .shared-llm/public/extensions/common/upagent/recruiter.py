#!/usr/bin/env python3
"""UpAgent Recruiter — the always-up broker that hires a fresh worker per work order.

The Recruiter has a status pane in the services workspace — the unified `herdr` workspace by
default (services and every run's tabs share it), or a dedicated `shared-services` workspace
with `up --separate-workspaces`. Any requester places an order directly through the durable
CLI lifecycle:

    just upagent-request <path/to/order.json>
    just upagent-await <path/to/order.json>

These commands submit directly to the durable ledger instead of injecting command text into the
Recruiter's shell pane. The compatibility ``dispatch`` command remains for non-interactive
callers. Only the job runner atomically claims an order, then:
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

Pure stdlib + PyYAML (as the sibling specialist hub uses). No Go hub, no tmux — Herdr only.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
import errno
import fcntl
import hashlib
import hmac
import importlib.util
import json
import math
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

# Single-workspace default: services (and every run's tabs) share one `herdr` workspace.
# `up --separate-workspaces` restores the dedicated `shared-services` workspace.
UNIFIED_WORKSPACE_LABEL = "herdr"
SHARED_SERVICES_WORKSPACE = "shared-services"
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

    def completed_result(self, key: str, order: dict) -> dict | None:
        """Return a strictly valid terminal result, if this order has already finished."""
        if not self._is_finished(key):
            return None
        result = load_result(order["result_path"], expected_order_id=order["order_id"])
        receipt = self.completed_receipt(key, order)
        if receipt["verdict"] != result["verdict"]:
            raise RecruiterError(
                f"completion receipt for {order['order_id']} disagrees with result.json"
            )
        return result

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
            lease = {
                "order_id": order_id,
                "token": token,
                "requester_control_token": uuid.uuid4().hex,
                "expires_at": expiry_epoch,
                **(owner or {}),
            }
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
            self._event(key, "claimed", **lease)
            self._snapshot(key, "claimed", **lease)
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

    def mark_manager_ready(self, key: str, token: str, decision: object) -> bool:
        claim_dir = self.active / "requests" / key
        with self._claim_lock(key):
            if not claim_dir.is_dir():
                return False
            lease = self._lease(claim_dir / "lease.json")
            if lease["token"] != token:
                return False
            detail = {
                "decision": getattr(decision, "decision"),
                "generation": getattr(decision, "generation"),
                "message": getattr(decision, "message"),
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
        self, key: str, token: str, nonce: str, decision: object
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
                "request_id": getattr(decision, "request_id"),
                "generation": getattr(decision, "generation"),
                "action": getattr(decision, "action"),
                "extension_ms": getattr(decision, "extension_ms"),
                "message": getattr(decision, "message"),
            }
            self._write_json(path, value)
            self._event(
                key,
                "requester-decision",
                decision_path=str(path),
                action=value["action"],
            )
            return path

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
            self._write_json(Path(order["result_path"]), parsed)
            load_result(order["result_path"], expected_order_id=order["order_id"])
            terminal_state = "finished" if verified_absent else "cleanup-failed"
            if not verified_absent and parsed["verdict"] != "blocked":
                raise RecruiterError(
                    f"cleanup-failed order {order['order_id']} must publish a blocked result"
                )
            receipt = {
                "cleanup": cleanup,
                "generation": lease.get("generation", 1),
                "order_id": order["order_id"],
                "request_id": lease.get(
                    "request_id", lifecycle.request_identity(order)
                ),
                "result_path": order["result_path"],
                "state": terminal_state,
                "verdict": parsed["verdict"],
            }
            self._event(
                key,
                terminal_state,
                verdict=parsed["verdict"],
                cleanup=cleanup,
                **detail,
            )
            self._write_json(self.request_dir(key) / "receipt.json", receipt)
            self._snapshot(
                key,
                terminal_state,
                verdict=parsed["verdict"],
                cleanup=cleanup,
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
        errors.append("Pi model must use provider/id[:thinking] form")
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


def _herdr_json(*args: str, timeout_seconds: float | None = None) -> dict:
    """Run a herdr subcommand expected to print JSON; return the parsed object. Fail-loud."""
    _herdr_available()
    try:
        proc = subprocess.run(
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
        raise RecruiterError(f"herdr {' '.join(args)} could not run: {error}") from error
    if proc.returncode != 0:
        raise RecruiterError(f"herdr {' '.join(args)} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RecruiterError(
            f"herdr {' '.join(args)} did not print JSON: {proc.stdout[:200]}"
        ) from e


def _herdr(*args: str) -> None:
    """Run a herdr subcommand that prints nothing on success. Fail-loud on non-zero."""
    _herdr_available()
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecruiterError(f"herdr {' '.join(args)} failed: {proc.stderr.strip()}")


def _pane_recent_output(pane: str, lines: int = 80) -> str:
    _herdr_available()
    process = subprocess.run(
        [
            "herdr",
            "pane",
            "read",
            pane,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(lines),
        ],
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RecruiterError(f"herdr pane read {pane} failed: {process.stderr.strip()}")
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
    cockpit = (
        _herdr_json("pane", "get", order["cockpit_pane"])
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
    response = _herdr_json(*args)
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
                )
            except RecruiterError as error:
                _layout_warning(tab_role, pane_id, str(error))
    return pane_id, workspace_id, address


def _layout_warning(role: str, pane_id: str, reason: str) -> None:
    sys.stderr.write(
        f"recruiter: {role} pane {pane_id} layout adjustment failed: {reason}; "
        "worker lifecycle continues\n"
    )


def _resize_started_pane(
    pane_id: str,
    *,
    split_direction: str,
    target_fraction: float,
    role: str,
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
) -> dict[str, object]:
    """Prove an expected harness actually started; pane creation alone is insufficient."""
    resolved_cwd = os.path.realpath(expected_cwd)
    deadline = time.monotonic() + timeout_ms / 1000
    started = time.monotonic()
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        pane = _herdr_json("pane", "get", pane_id).get("result", {}).get("pane", {})
        process_info = (
            _herdr_json("pane", "process-info", "--pane", pane_id)
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
            output = _pane_recent_output(pane_id)
            raise RecruiterError(
                f"agent pane {pane_id} did not start expected {expected_process} process; recent output: {output[-1000:]}"
            )
        time.sleep(HEALTH_PROBE_SECONDS)
    raise RecruiterError(
        f"agent pane {pane_id} did not become healthy within {timeout_ms} ms; evidence={json.dumps(latest)}"
    )


def _wait_for_worker_health(
    worker_pane: str, order: dict, timeout_ms: int, roster: dict | None = None
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
    )


def _live_pane_ids() -> set[str]:
    response = _herdr_json("pane", "list")
    panes = response.get("result", {}).get("panes", [])
    return {
        pane["pane_id"]
        for pane in panes
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
    }


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


def _write_worker_instructions(
    order: dict, worker_result_path: Path, destination: Path
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
        raise RecruiterError(
            f"could not write lease-specific worker instructions {destination}: {e}"
        ) from e


def _wait_for_agent_status(
    worker_pane: str, timeout_ms: int, monitor_finalized: threading.Event | None
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


def _watchdog_terminal_reason(order: dict, result: dict) -> str | None:
    """Return why a watchdog result is premature, or None when its durable gate is terminal."""
    if order.get("agent") not in WATCHDOG_AGENTS:
        return None
    terminal = order["watchdog_terminal"]
    path = Path(terminal["path"])
    if not path.is_file():
        return f"authoritative {terminal['kind']} terminal record does not exist: {path}"
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


def _start_completion_monitor(
    order: dict,
    worker_result_path: Path,
    timeout_ms: int,
    *,
    inactivity_check_ms: int | None = None,
    on_inactivity: Callable[[int], None] | None = None,
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
        invalid_signature: tuple[int, int] | None = None
        invalid_since = 0.0
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
                    while finalized.is_set() and not stop.wait(
                        COMPLETION_MONITOR_POLL_SECONDS
                    ):
                        pass
                    invalid_signature = None
                    continue
                stop.wait(COMPLETION_MONITOR_POLL_SECONDS)
                continue
            finalized.set()
            while finalized.is_set() and not stop.wait(
                COMPLETION_MONITOR_POLL_SECONDS
            ):
                pass
            invalid_signature = None

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


def _wait_typed_file(
    path: Path, timeout_ms: int, parser: Callable[[str], object]
) -> object:
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


def _submit_agent_prompt(target: str, message: str, idle_timeout_ms: int) -> None:
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
        )
        agent = _herdr_json("agent", "get", target).get("result", {}).get("agent", {})
        pane_id = agent.get("pane_id") if isinstance(agent, dict) else None
        if not isinstance(pane_id, str) or not pane_id:
            raise RecruiterError(f"Herdr agent target {target!r} has no current pane")
        _herdr("pane", "run", pane_id, message)


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

def _manager_anchor_pane(order: dict) -> str:
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
        _herdr_json("workspace", "list").get("result", {}).get("workspaces", [])
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
        pane = _herdr_json("pane", "get", anchor).get("result", {}).get("pane", {})
        if not isinstance(pane, dict) or pane.get("workspace_id") != workspace_id:
            raise RecruiterError(
                f"manager anchor pane {anchor} is not in requested workspace {workspace_id}"
            )
        return anchor
    panes = (
        _herdr_json("pane", "list", "--workspace", workspace_id)
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


def _direct_manager(config: object, order: dict, generation: int = 1) -> dict[str, object]:
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
) -> dict[str, object]:
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
    manager_order = {**order, "cockpit_pane": _manager_anchor_pane(order)}
    manager_pane, workspace_id, manager_address = _start_herdr_agent(
        name,
        manager_order,
        command,
        split_direction="down",
        tab_role="oversight",
    )
    if not ledger.record_manager(
        key, token, manager_pane, manager_address, workspace_id, generation
    ):
        _close_worker_pane(manager_pane)
        raise RecruiterError(
            f"lease ownership changed before manager {manager_address} was recorded"
        )
    _resize_started_pane(
        manager_pane,
        split_direction="down",
        target_fraction=SUPPORT_PANE_FRACTION,
        role="account manager",
    )
    try:
        health = _wait_for_agent_health(
            manager_pane,
            expected_agent=config.account_manager.expected_agent,
            expected_process=config.account_manager.expected_process,
            expected_cwd=order["cwd"],
            timeout_ms=config.startup_timeout_ms,
        )
        decision = _wait_typed_file(
            decision_path,
            config.account_manager.timeout_ms,
            lambda text_value: lifecycle.parse_manager_decision(
                text_value, request_id, generation
            ),
        )
    except (RecruiterError, OSError):
        _close_worker_pane(manager_pane)
        raise
    if not ledger.mark_manager_ready(key, token, decision):
        _close_worker_pane(manager_pane)
        raise RecruiterError(
            f"lease ownership changed before manager {manager_address} became ready"
        )
    _notify_requester(
        ledger,
        key,
        order,
        generation,
        "account-manager-ready",
        getattr(decision, "message"),
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
        "pane": manager_pane,
        "workspace_id": workspace_id,
    }


def _ask_manager_about_startup(
    ledger: JobLedger,
    key: str,
    order: dict,
    manager: dict[str, object],
    worker_evidence: dict[str, object],
) -> object:
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
        assessment=getattr(assessment, "assessment"),
        confidence=getattr(assessment, "confidence"),
        recommended_action=getattr(assessment, "recommended_action"),
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
    directory = ledger.request_dir(key) / "checks" / f"{check_number:06d}"
    evidence_path = directory / "evidence.json"
    output_path = directory / "assessment.json"
    output_path.unlink(missing_ok=True)
    pane = _herdr_json("pane", "get", worker_pane).get("result", {}).get("pane", {})
    process_info = (
        _herdr_json("pane", "process-info", "--pane", worker_pane)
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
        "recent_output": _pane_recent_output(worker_pane, lines=120)[-8000:],
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
    checker_anchor = manager["pane"] if manager["pane"] is not None else order["cockpit_pane"]
    checker_order = {**order, "cockpit_pane": cast(str, checker_anchor)}
    checker_pane, _, _ = _start_herdr_agent(
        name,
        checker_order,
        command,
        split_direction="down",
        tab_role="oversight",
    )
    try:
        _resize_started_pane(
            checker_pane,
            split_direction="down",
            target_fraction=SUPPORT_PANE_FRACTION,
            role="one-shot checker",
        )
        _wait_for_agent_health(
            checker_pane,
            expected_agent=config.checker.expected_agent,
            expected_process=config.checker.expected_process,
            expected_cwd=order["cwd"],
            timeout_ms=config.startup_timeout_ms,
        )
        assessment = _wait_typed_file(
            output_path,
            config.checker.timeout_ms,
            lambda text_value: lifecycle.parse_check_assessment(
                text_value, request_id, generation
            ),
        )
    finally:
        _close_worker_pane(checker_pane)
    ledger._event(
        key,
        "worker-checked",
        assessment=getattr(assessment, "assessment"),
        check_number=check_number,
        confidence=getattr(assessment, "confidence"),
        recommended_action=getattr(assessment, "recommended_action"),
    )
    if manager["address"] is not None:
        with suppress(RecruiterError):
            _submit_agent_prompt(
                cast(str, manager["address"]),
                f"Check {check_number} assessed worker {worker_pane} as "
                f"{getattr(assessment, 'assessment')}: {getattr(assessment, 'message')}",
                idle_timeout_ms=config.account_manager.timeout_ms,
            )
    if getattr(assessment, "assessment") not in ("healthy", "completed"):
        _notify_requester(
            ledger,
            key,
            order,
            generation,
            "worker-check-alert",
            getattr(assessment, "message"),
            {
                "assessment": getattr(assessment, "assessment"),
                "check_number": check_number,
                "confidence": getattr(assessment, "confidence"),
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
        rescue_pane, _, _ = _start_herdr_agent(
            name, rescue_order, command, split_direction="down", tab_role="oversight"
        )
        try:
            _wait_for_agent_health(
                rescue_pane,
                expected_agent=config.checker.expected_agent,
                expected_process=config.checker.expected_process,
                expected_cwd=order["cwd"],
                timeout_ms=config.startup_timeout_ms,
            )
            assessment = _wait_typed_file(
                output_path,
                config.checker.timeout_ms,
                lambda text_value: lifecycle.parse_check_assessment(
                    text_value, request_id, generation
                ),
            )
        finally:
            _close_worker_pane(rescue_pane)
    except (RecruiterError, LifecycleError, ContractError, OSError) as error:
        # The event is telemetry; the advice is the contract. A broken ledger write must
        # not turn "broker unavailable" into a raised exception.
        with suppress(OSError):
            ledger._event(key, "startup-rescue-advice-unavailable", reason=str(error))
        return "retry-startup"
    action = cast(str, getattr(assessment, "recommended_action"))
    with suppress(OSError):
        ledger._event(
            key,
            "startup-rescue-assessed",
            assessment=getattr(assessment, "assessment"),
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
    before_worker_cleanup: Callable[[], None] | None = None,
    attempt: int = 1,
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
    cleanup: dict[str, object] = {
        "status": "not-created",
        "worker_pane": None,
        "verified_absent": True,
    }
    monitor_finalized: threading.Event | None = None
    startup_validated = False
    startup_rejected = False
    # Direct dispatch runs in the phase leader's environment; never report Recruiter state onto
    # that pane. Resolve the broker's explicit persisted address instead.
    my_pane = _recruiter_pane_from_state()
    _report_state(my_pane, "working", f"hiring for {order_id}")
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
        _write_worker_instructions(order, worker_result_path, effective_instructions)
        execution_order["instructions_path"] = str(effective_instructions)
        launch = resolve_launch_command(execution_order, roster)
        management_config = llm_management.load_management_config(roster)
        request_id = lifecycle.request_identity(order)
        # The attempt number keeps a rescue relaunch's agent name distinct from attempt 1's.
        worker_name = _safe_agent_name("upagent", request_id, attempt)
        worker_tab = _worker_tab_role(execution_order)
        worker_pane, workspace_id, worker_address = _start_herdr_agent(
            worker_name, execution_order, launch, tab_role=worker_tab
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
            )
        # The pane address is durably owned before health validation. A failed launch can
        # therefore be cleaned without guessing, while nobody is told "running" prematurely.
        health = _wait_for_worker_health(
            worker_pane, execution_order, management_config.startup_timeout_ms, roster
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
                _wait_for_agent_status(worker_pane, remaining_ms, monitor_finalized)
            except AgentWaitTimeout:
                timeout_number += 1
                if on_timeout is None:
                    raise
                extension_ms = on_timeout(timeout_number, monitor_finalized)
                if extension_ms is None:
                    raise AgentWaitTimeout(
                        f"worker {worker_pane} exceeded its cap and no extension was authorized"
                    )
                wait_deadline = time.monotonic() + extension_ms / 1000
                continue

            # Invalid aliases are a worker contract failure, not a repair.
            result = load_result(worker_result_path, expected_order_id=order_id)
            premature_reason = _watchdog_terminal_reason(order, result)
            if premature_reason is None:
                break
            premature_number += 1
            archived = _archive_premature_watchdog_result(
                worker_result_path, premature_number
            )
            if monitor_finalized is not None:
                monitor_finalized.clear()
            sys.stderr.write(
                f"recruiter: watchdog {order_id} produced premature result; "
                f"archived {archived}: {premature_reason}\n"
            )
            _submit_agent_prompt(
                worker_address,
                "WATCHDOG_CONTINUE: Your result was not accepted because "
                f"{premature_reason}. Resume monitoring now. Do not write another result "
                "until the authoritative terminal record exists and matches this assignment.",
                idle_timeout_ms=WATCHDOG_CONTINUATION_TIMEOUT_MS,
            )
    except (RecruiterError, ContractError, KeyError, TypeError, OSError) as e:
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
        result = _write_blocked_result(
            order, str(e), worker_result_path, preserve_valid=preserve_existing
        )
        fell_back = not preserve_existing
        if fell_back:
            sys.stderr.write(f"recruiter: order {order_id} fell back to blocked: {e}\n")
        else:
            sys.stderr.write(
                f"recruiter: order {order_id} kept existing worker result after Recruiter wait fault: {e}\n"
            )
    finally:
        if before_worker_cleanup is not None:
            before_worker_cleanup()
        if worker_pane is not None:
            try:
                cleanup = _close_worker_pane(worker_pane)
            except RecruiterError as e:
                result = _write_blocked_result(
                    order,
                    f"worker cleanup failed: {e}",
                    worker_result_path,
                    preserve_valid=False,
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
    final_label = "blocked" if fell_back else "done"
    _report_state(my_pane, "idle", f"last order: {order_id} ({final_label})")
    return (1 if fell_back else 0), result, cleanup


def _issued_consult_token() -> str | None:
    """The consult token this Recruiter issued at `up` (from its own STATE_FILE), or None when
    none was issued (state absent, corrupt, or written by a pre-token `up`)."""
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = state.get("consult_token") if isinstance(state, dict) else None
    return token if isinstance(token, str) and token else None


def _reject_unbrokered_consult(order: dict) -> None:
    """Specialist consults must arrive brokered by the Librarian, which stamps the
    Recruiter-issued consult token on every order it authors. A consult-shaped order without
    the current token is a hand-written imitation — a worker that read the hub's files and
    forged the Librarian's identity — and is refused with the correct door named. No token
    issued (a pre-token `up` state) means there is nothing to verify against, so enforcement
    waits for the next `up`."""
    requester = order.get("requester")
    requester_id = requester.get("id") if isinstance(requester, dict) else None
    consult_shaped = (
        order.get("phase_id") == "specialist-consult"
        or str(order.get("order_id", "")).startswith("specialist-consult-")
        or requester_id == "specialist-librarian"
    )
    if not consult_shaped:
        return
    issued = _issued_consult_token()
    if issued is None:
        return
    if order.get("consult_token") != issued:
        raise RecruiterError(
            f"order {order.get('order_id')!r} is consult-shaped but was not brokered by the "
            "Librarian (missing or stale consult token). Never hand-write specialist-consult "
            "orders or inject them into panes — write a consult.json and run "
            "`just specialist-hub consult <consult.json>`."
        )


def cmd_recruit(order_path: str, roster_path: str) -> int:
    """Submit an order and return immediately; its claimed job owns the blocking lifecycle.

    Compatibility/manual surface only. Phase leaders use ``dispatch`` so their shell call blocks
    for the durable receipt without depending on this command's pane output.
    """
    try:
        order = load_order(order_path)
    except ContractError as e:
        _reject_legacy_order(order_path, f"invalid order {order_path}: {e}")
        return 1
    _reject_unbrokered_consult(order)
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
    # A duplicate of a queued or live order starts another contender: the atomic claim admits
    # only one while a prior runner is live, and retries an earlier runner that died before claim.
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--roster",
        roster_path,
        "run-job",
        key,
    ]
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
            order,
            f"could not start job runner: {e}",
            ledger.result_staging_path(key, token),
        )
        cleanup = {
            "status": "not-created",
            "worker_pane": None,
            "verified_absent": True,
        }
        if not ledger.finalize(
            key, token, order, result, cleanup=cleanup, reason=str(e), exit_code=1
        ):
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
    return (
        "run-job" in command
        and key in command
        and str(Path(__file__).resolve()) in command
    )


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


def _cleanup_lease_panes(lease: dict) -> dict[str, object]:
    """Close every pane explicitly recorded by one lease generation, and no others."""
    outcomes: dict[str, object] = {}
    failures = []
    for role, field in (("worker", "worker_pane"), ("manager", "manager_pane")):
        pane = lease.get(field)
        if not isinstance(pane, str) or not pane:
            outcomes[role] = {
                "status": "not-created",
                "worker_pane": None,
                "verified_absent": True,
            }
            continue
        try:
            outcomes[role] = _close_worker_pane(pane)
        except RecruiterError as error:
            outcomes[role] = {
                "status": "cleanup-failed",
                "worker_pane": pane,
                "verified_absent": False,
                "reason": str(error),
            }
            failures.append(str(error))
    return {
        **outcomes,
        "status": "closed" if not failures else "cleanup-failed",
        "verified_absent": not failures,
        "worker_pane": lease.get("worker_pane"),
        **({"reason": "; ".join(failures)} if failures else {}),
    }


def _reconcile_claim(ledger: JobLedger, key: str, lease: dict, *, force: bool) -> bool:
    """Close and terminalize one dead/expired owned job. Never touches an unrecorded pane."""
    expired = lease["expires_at"] <= int(time.time())
    runner_alive = _runner_alive(lease.get("runner_pid"), key)
    if not force and not expired and runner_alive:
        return False
    if runner_alive:
        _terminate_owned_runner(lease.get("runner_pid"), key)

    worker_pane = lease.get("worker_pane")
    cleanup = _cleanup_lease_panes(lease)

    order = ledger.order(key)
    staging = ledger.result_staging_path(key, lease["token"])
    if cleanup["verified_absent"]:
        try:
            result = load_result(staging, expected_order_id=order["order_id"])
        except ContractError as e:
            result = _write_blocked_result(
                order, f"runner reconciliation: {e}", staging
            )
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
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--roster",
        roster_path,
        "run-job",
        key,
    ]
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


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
    _reject_unbrokered_consult(order)
    ledger = JobLedger()
    key, _created = ledger.submit(order)
    warning, announce = _record_phase_receipt_warning(ledger, key, order)
    if warning is not None and announce:
        print(
            f"REQUEST_DEGRADED {json.dumps({'request_id': lifecycle.request_identity(order), 'warning': warning}, sort_keys=True)}",
            flush=True,
        )
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
                raise RecruiterError(
                    f"could not start a contender for live order {order['order_id']}: {e}"
                ) from e
            result = _write_blocked_result(
                order,
                f"could not start job runner: {e}",
                ledger.result_staging_path(key, token),
            )
            cleanup = {
                "status": "not-created",
                "worker_pane": None,
                "verified_absent": True,
            }
            ledger.finalize(
                key, token, order, result, cleanup=cleanup, reason=str(e), exit_code=1
            )
            process = None
        timeout_ms = order.get("timeout_ms", _default_timeout_ms(order["stage_id"]))
        deadline = time.monotonic() + timeout_ms / 1000 + LEASE_GRACE_SECONDS
        if process is not None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
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
                    )
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
    print(f"ORDER_RECEIPT {json.dumps(receipt, sort_keys=True)}", flush=True)
    return 0


def cmd_request(order_path: str, roster_path: str) -> int:
    """Submit directly and return only after the worker is healthy or terminally rejected."""
    try:
        order = load_order(order_path)
    except ContractError as error:
        raise RecruiterError(f"invalid order {order_path}: {error}") from error
    _reject_unbrokered_consult(order)
    roster = load_roster(roster_path)
    config = llm_management.load_management_config(roster)
    ledger = JobLedger()
    key, _ = ledger.submit(order)
    warning, _announce = _record_phase_receipt_warning(ledger, key, order)
    existing = ledger.completed_result(key, order)
    if existing is not None:
        receipt = ledger.completed_receipt(key, order)
        print(f"REQUEST_TERMINAL {json.dumps(receipt, sort_keys=True)}")
        return 0 if existing["verdict"] == "passed" else 1
    try:
        process = _spawn_job(key, roster_path)
    except OSError as error:
        raise RecruiterError(
            f"could not start request owner for {order['order_id']}: {error}"
        ) from error
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
            print(
                f"REQUEST_ACCEPTED {json.dumps(response, sort_keys=True)}", flush=True
            )
            return 0
        if state.get("state") in ("finished", "cleanup-failed"):
            receipt = ledger.completed_receipt(key, order)
            print(f"REQUEST_TERMINAL {json.dumps(receipt, sort_keys=True)}", flush=True)
            return 0 if receipt["verdict"] == "passed" else 1
        if process.poll() is not None and state.get("state") not in (
            "running",
            "finished",
            "cleanup-failed",
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
    try:
        subprocess.run(command, check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


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
    }
    verify_order_path = base.with_name("verify-order.json")
    JobLedger._write_json(verify_order_path, verify_order)
    return cmd_dispatch(str(verify_order_path), roster_path)


_AWAIT_ANY_VERDICT_KINDS = {"passed": "completed", "failed": "failed", "blocked": "blocked"}


def _mailbox_messages(ledger: JobLedger, key: str) -> list[tuple[int, dict]]:
    messages_dir = ledger.requester_mailbox(key).messages
    messages: list[tuple[int, dict]] = []
    paths = sorted(messages_dir.glob("[0-9]*-*.json")) if messages_dir.is_dir() else []
    for path in paths:
        try:
            sequence = int(path.name.split("-", 1)[0])
            payload = json.loads(path.read_text())
        except (ValueError, OSError, json.JSONDecodeError) as error:
            raise RecruiterError(f"mailbox message {path} is unreadable: {error}") from error
        if isinstance(payload, dict):
            messages.append((sequence, payload))
    return messages


def _emit_await_event(kind: str, request_id: str | None, summary: str, cursor: dict, **detail: object) -> int:
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


def cmd_await_any(order_paths: list[str], timeout_ms: int, cursor_json: str = "{}", poll_seconds: float = HEALTH_PROBE_SECONDS) -> int:
    if not order_paths:
        raise RecruiterError("await-any requires at least one order path")
    try:
        cursor_value = json.loads(cursor_json) if cursor_json else {}
    except json.JSONDecodeError as error:
        raise RecruiterError(f"await-any cursor is not valid JSON: {error}") from error
    if not isinstance(cursor_value, dict) or not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) for k, v in cursor_value.items()):
        raise RecruiterError("await-any cursor must be a JSON object of request_id -> integer")
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
            raise RecruiterError(f"request {lifecycle.request_identity(order)} has not been submitted")
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
                return _emit_await_event("decision-required", request_id, f"Request {request_id} reached a work cap; the owner must extend or cancel.", cursor, decision_nonce=state.get("decision_nonce"), order_path=path, terminal=False, timeout_number=state.get("timeout_number"))
            if state.get("state") in ("finished", "cleanup-failed"):
                receipt = ledger.completed_receipt(key, order)
                verdict = str(receipt.get("verdict"))
                kind = _AWAIT_ANY_VERDICT_KINDS.get(verdict, "failed")
                return _emit_await_event(kind, request_id, f"Request {request_id} is terminal: verdict={verdict}.", cursor, order_path=path, receipt=receipt, terminal=True)
            for sequence, payload in _mailbox_messages(ledger, key):
                if sequence <= cursor.get(request_id, 0):
                    continue
                cursor[request_id] = sequence
                message_type = str(payload.get("type", ""))
                kind = "worker-warning" if ("warning" in message_type or "failed" in message_type) else "advisory"
                return _emit_await_event(kind, request_id, str(payload.get("message", message_type)), cursor, detail=payload.get("detail", {}), message_type=message_type, order_path=path, sequence=sequence, terminal=False)
        if time.monotonic() >= deadline:
            return _emit_await_event("await-heartbeat", None, "Quiet and healthy: no watched request moved within the bounded wait; re-await.", cursor, states=states, terminal=False)
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
        "generation": generation,
        "request_id": request_id,
        "runner_pid": os.getpid(),
        "phase_leader_pane": order["cockpit_pane"],
        "recruiter_pane": _recruiter_pane_from_state(),
    }
    lease_window_ms = (
        timeout_ms
        + management_config.account_manager.timeout_ms
        + management_config.startup_timeout_ms * 2
        + management_config.requester_grace_ms
    )
    token = ledger.claim(key, order["order_id"], lease_window_ms, owner=owner)
    if token is None:
        return 0
    worker_result_path = ledger.result_staging_path(key, token)
    monitor: tuple[threading.Event, threading.Event, threading.Thread] | None = None
    manager: dict[str, object] | None = None
    mechanical_validation = inspect_worker_configuration(order, roster)

    # An order may pin its own lifecycle ownership (the Specialist Hub pins `dedicated`
    # for every consult so consults always get a broker); the roster sets the default.
    requested_management = order.get("management") or {}
    effective_management_mode = requested_management.get("mode") or management_config.mode
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
            )
        else:
            manager = _direct_manager(management_config, order, generation)
    except (RecruiterError, ManagementConfigError, LifecycleError, OSError) as error:
        _notify_requester(
            ledger,
            key,
            order,
            generation,
            "account-manager-failed",
            f"The Hub could not establish lifecycle ownership: {error}",
        )
        result = _write_blocked_result(
            order, f"account manager failed: {error}", worker_result_path
        )
        cleanup = {
            "status": "not-created",
            "worker_pane": None,
            "verified_absent": True,
        }
        ledger.finalize(
            key,
            token,
            order,
            result,
            cleanup=cleanup,
            exit_code=1,
            completion_source="account-manager-startup",
        )
        return 1

    decision = manager["decision"]
    configuration_errors = cast(list[str], mechanical_validation["errors"])
    if getattr(decision, "decision") != "approved" or configuration_errors:
        message = getattr(decision, "message")
        message_type = getattr(decision, "decision")
        if configuration_errors:
            message_type = "needs-requester"
            message = f"Worker configuration is invalid: {'; '.join(configuration_errors)}. {message}"
        _notify_requester(ledger, key, order, generation, message_type, message)
        result = _write_blocked_result(
            order, f"account manager: {message}", worker_result_path
        )
        manager_cleanup = (
            _close_worker_pane(cast(str, manager["pane"]))
            if manager["pane"] is not None
            else {"status": "not-created", "worker_pane": None, "verified_absent": True}
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
            completion_source="account-manager-decision",
        )
        return 1

    inactivity_lock = threading.Lock()
    owned_worker_pane: str | None = None

    def start_monitor(
        worker_pane: str, workspace_id: str | None, worker_address: str
    ) -> threading.Event:
        nonlocal owned_worker_pane
        nonlocal monitor
        owned_worker_pane = worker_pane
        if not ledger.record_worker(
            key, token, worker_pane, workspace_id, worker_address
        ):
            raise RecruiterError(
                f"lease ownership changed before worker {worker_pane} was recorded"
            )

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

    def finish_monitor_before_cleanup() -> None:
        if monitor is None:
            return
        monitor[0].set()
        # A checker is bounded by its own startup/response timeouts. Taking this lock prevents
        # worker cleanup from racing an in-flight evidence read.
        with inactivity_lock:
            pass
        monitor[2].join()

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
        if getattr(assessment, "assessment") not in ("healthy", "completed"):
            message = getattr(assessment, "message")
            _notify_requester(
                ledger,
                key,
                order,
                generation,
                "startup-needs-requester",
                message,
                {"assessment": getattr(assessment, "assessment")},
            )
            raise StartupRejectedByManager(
                f"account manager did not validate worker startup: {message}"
            )
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
            getattr(assessment, "message"),
            evidence,
        )

    def run_order_once(attempt: int) -> tuple[int, dict, dict[str, object]]:
        return _run_order(
            str(ledger.request_dir(key) / "request.json"),
            roster_path,
            worker_result_path,
            start_monitor,
            ledger.worker_instructions_path(key, token),
            worker_healthy,
            handle_timeout,
            finish_monitor_before_cleanup,
            attempt=attempt,
        )

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
            _close_worker_pane(cast(str, manager["pane"]))
            if manager["pane"] is not None
            else {"status": "not-created", "worker_pane": None, "verified_absent": True}
        )
    except RecruiterError as error:
        result = _write_blocked_result(
            order,
            f"account manager cleanup failed: {error}",
            worker_result_path,
            preserve_valid=False,
        )
        result_code = 1
        manager_cleanup = {
            "status": "cleanup-failed",
            "worker_pane": manager["pane"],
            "verified_absent": False,
            "reason": str(error),
        }
    cleanup["manager"] = manager_cleanup
    cleanup["verified_absent"] = (
        cleanup["verified_absent"] is True
        and manager_cleanup["verified_absent"] is True
    )
    _notify_requester(
        ledger,
        key,
        order,
        generation,
        "result-ready",
        f"Worker finished with verdict {result['verdict']}; lifecycle cleanup is being finalized.",
        {"verdict": result["verdict"]},
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
    marker = "DONE" if cleanup["verified_absent"] else "CLEANUP_FAILED"
    print(f"ORDER {order['order_id']} {marker}", flush=True)
    return result_code


def _find_workspace(workspaces_resp: dict, label: str) -> dict | None:
    """The WorkspaceInfo labeled `label` from a `workspace list` response, or None."""
    for w in workspaces_resp.get("result", {}).get("workspaces", []):
        if w.get("label") == label:
            return w
    return None


def _find_services_workspace(workspaces_resp: dict) -> dict | None:
    """The live services workspace under either mode's label, preferring the unified default."""
    for label in (UNIFIED_WORKSPACE_LABEL, SHARED_SERVICES_WORKSPACE):
        found = _find_workspace(workspaces_resp, label)
        if found is not None:
            return found
    return None


RECRUITER_PANE_LABEL = "recruiter"
LIBRARIAN_PANE_LABEL = "librarian"

# The Recruiter pane's shell used to arm a working `recruit()` function — a side door that let
# any agent hire by injecting text into the pane. It is sealed: the stub refuses loudly and
# names the real doors. Sealing takes effect per pane at arm time, so a pane armed before this
# change keeps its old function until the next `up`.
SEALED_RECRUIT_DOOR_STUB = (
    "recruit() { echo 'recruit door sealed: pane text is not a message queue. Submit orders "
    "with `just upagent-request <order.json>` (verified startup) or `just upagent-recruit "
    "<order.json>` (blocking dispatch); specialist consults go through `just specialist-hub "
    "consult <consult.json>`.' >&2; return 2; }"
)


def _ensure_role_pane(role_label: str, workspace_label: str) -> tuple[str, str, bool]:
    """Resolve (workspace_id, pane_id, reused) for THIS engine's role pane in the single services
    workspace (`workspace_label`), claiming ONLY a pane labeled `role_label` — never an arbitrary
    pane. This lets the Recruiter and the Librarian share one workspace without fighting over each
    other's panes, regardless of which engine started first:
      - if services are already up under the OTHER mode's label, fail loud (run `just herdr-down`
        first) rather than splitting the services across two workspaces;
      - create the services workspace if it is absent, and label its root pane for my role;
      - if it exists, reuse my role-labeled pane if present, else split a fresh pane off an
        existing one and label it for my role.
    """
    other_label = (
        SHARED_SERVICES_WORKSPACE
        if workspace_label == UNIFIED_WORKSPACE_LABEL
        else UNIFIED_WORKSPACE_LABEL
    )
    other = _find_workspace(_herdr_json("workspace", "list"), other_label)
    if other is not None and isinstance(other.get("workspace_id"), str):
        other_panes = (
            _herdr_json("pane", "list", "--workspace", other["workspace_id"])
            .get("result", {})
            .get("panes", [])
        )
        if any(
            p.get("label") in (RECRUITER_PANE_LABEL, LIBRARIAN_PANE_LABEL)
            for p in other_panes
            if isinstance(p, dict)
        ):
            raise RecruiterError(
                f"services are already up in workspace {other_label!r}; "
                "run `just herdr-down` first to switch workspace modes"
            )
    existing = _find_workspace(_herdr_json("workspace", "list"), workspace_label)
    if existing is None:
        created = _herdr_json(
            "workspace", "create", "--label", workspace_label, "--no-focus"
        )["result"]
        workspace_id = created["workspace"]["workspace_id"]
        pane_id = created["root_pane"]["pane_id"]
        _herdr("pane", "rename", pane_id, role_label)
        return workspace_id, pane_id, False

    workspace_id = existing["workspace_id"]
    panes = (
        _herdr_json("pane", "list", "--workspace", workspace_id)
        .get("result", {})
        .get("panes", [])
    )
    mine = next(
        (p for p in panes if p.get("label") == role_label and p.get("pane_id")), None
    )
    if mine is not None:
        return workspace_id, mine["pane_id"], True
    # Prefer splitting beside the sibling service pane so services stay together in one tab
    # even when the unified workspace already holds run panes.
    anchor = next(
        (
            p["pane_id"]
            for p in panes
            if p.get("label") == LIBRARIAN_PANE_LABEL and p.get("pane_id")
        ),
        None,
    ) or next((p["pane_id"] for p in panes if p.get("pane_id")), None)
    if anchor is None:
        raise RecruiterError(
            f"services workspace {workspace_id} has no pane to split from"
        )
    new_pane = _herdr_json(
        "pane", "split", anchor, "--direction", "down", "--no-focus"
    )["result"]["pane"]["pane_id"]
    _herdr("pane", "rename", new_pane, role_label)
    return workspace_id, new_pane, True


def cmd_up(roster_path: str, *, separate_workspaces: bool = False) -> int:
    """Ensure the services workspace + an armed Recruiter pane. Idempotent.

    Default is the unified `herdr` workspace (services and every run's tabs share it);
    `--separate-workspaces` restores the dedicated `shared-services` workspace. The pane is a
    visible status surface ONLY; requesters submit through the CLI and durable ledger. The
    pane's `recruit()` function is armed as a sealed stub that refuses and names the real
    doors — pane text is not a message queue. `up` also issues a fresh `consult_token` that
    the Librarian stamps on the orders it brokers; consult-shaped orders without it are
    refused at submission. Persists workspace, mode, pane, roster, token, and supervisor
    ownership to STATE_FILE.
    """
    # Validate the roster up front so a missing/bad roster fails loudly at bring-up, not
    # silently at the first hire.
    load_roster(roster_path)
    workspace_label = (
        SHARED_SERVICES_WORKSPACE if separate_workspaces else UNIFIED_WORKSPACE_LABEL
    )
    workspace_id, recruiter_pane, reused = _ensure_role_pane(
        RECRUITER_PANE_LABEL, workspace_label
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
            )
        except RecruiterError as error:
            _layout_warning("services", recruiter_pane, str(error))

    # Arm the sealed stub (replacing any previously armed working function in this shell) so
    # pane-injected `recruit <path>` text can never hire again.
    _herdr("pane", "run", recruiter_pane, SEALED_RECRUIT_DOOR_STUB)

    supervisor_token = uuid.uuid4().hex
    state = {
        "workspace_id": workspace_id,
        "workspace_label": workspace_label,
        "separate_workspaces": separate_workspaces,
        "recruiter_pane": recruiter_pane,
        "roster": roster_path,
        "supervisor_token": supervisor_token,
        # Issued fresh per `up`; the Librarian reads it from this state file and stamps it on
        # every consult order it brokers. See _reject_unbrokered_consult.
        "consult_token": uuid.uuid4().hex,
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    JobLedger._write_json(STATE_FILE, state)
    supervisor = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--roster",
            roster_path,
            "supervise",
            supervisor_token,
        ],
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
    services = _find_services_workspace(_herdr_json("workspace", "list"))
    label = services.get("label") if services else None
    print(f"services: {'up (' + str(label) + ')' if services else 'down'}")
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
    p_recruit = sub.add_parser(
        "recruit", help="submit a worker order without blocking the Recruiter pane"
    )
    p_recruit.add_argument("order", help="path to order.json")
    p_dispatch = sub.add_parser(
        "dispatch", help="submit an order and block for its durable completion receipt"
    )
    p_dispatch.add_argument("order", help="path to order.json")
    p_request = sub.add_parser(
        "request", help="submit an order and return after verified worker startup"
    )
    p_request.add_argument("order", help="path to order.json")
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
        "await-any", help="block until any watched request moves; print one tagged AWAIT_EVENT"
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
        help="keep services in their own `shared-services` workspace instead of the unified `herdr` one",
    )
    sub.add_parser("down", help="stop the Recruiter and reconcile every owned worker")
    p_reconcile = sub.add_parser(
        "reconcile", help="reconcile dead or expired owned workers"
    )
    p_reconcile.add_argument(
        "--all", action="store_true", help="reconcile every active worker"
    )
    sub.add_parser("status", help="report shared-services state")

    args = parser.parse_args(argv)
    try:
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
    except RecruiterError as e:
        sys.exit(f"recruiter: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
