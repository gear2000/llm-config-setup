#!/usr/bin/env python3
"""Durable Herdr run lifecycle ownership, snapshot, reconciliation, and cleanup.

This is a local, file-backed control plane for one run tree. It is deliberately
conservative: typed receipts beat panes, panes beat prose, and cleanup closes only
resources that were structurally recorded as created and still validate by identity.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
_runtime_name = "upagent_command_runtime"
if _runtime_name in sys.modules:
    command_runtime = sys.modules[_runtime_name]
else:
    _runtime_spec = importlib.util.spec_from_file_location(
        _runtime_name, HERE.parent / "upagent" / "command_runtime.py"
    )
    if _runtime_spec is None or _runtime_spec.loader is None:
        raise RuntimeError("could not load UpAgent command runtime")
    command_runtime = importlib.util.module_from_spec(_runtime_spec)
    sys.modules[_runtime_name] = command_runtime
    _runtime_spec.loader.exec_module(command_runtime)

_control_spec = importlib.util.spec_from_file_location(
    "herdr_run_controller_transport", HERE / "controller_transport.py"
)
if _control_spec is None or _control_spec.loader is None:
    raise RuntimeError("could not load canonical Herdr controller transport")
control = cast(Any, importlib.util.module_from_spec(_control_spec))
_control_spec.loader.exec_module(control)

SCHEMA = "herdr-run-lifecycle.v1"
SNAPSHOT_SCHEMA = "herdr-run-snapshot.v1"
RECONCILE_SCHEMA = "herdr-run-reconciliation.v1"
CLEANUP_SCHEMA = "herdr-run-cleanup.v1"
DEFAULT_HEARTBEAT_TTL_SECONDS = 300
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60
HERDR_RUN_OWNER_TOKEN_ENV = "HERDR_RUN_OWNER_TOKEN"
HERDR_RUN_OWNER_TOKEN_FILE_ENV = "HERDR_RUN_OWNER_TOKEN_FILE"
HERDR_RUN_TOKEN_DIR_ENV = "HERDR_RUN_TOKEN_DIR"
HERDR_RUN_DIR_ENV = "HERDR_RUN_DIR"
SAFE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MUTATING_ACTIONS = frozenset(("mutation", "session-start", "stop", "cleanup"))
READ_ACTIONS = frozenset(("snapshot", "reconcile", "recovery-read"))
TERMINAL_VERDICTS = frozenset(("passed", "failed", "blocked"))


class LifecycleError(RuntimeError):
    """A fail-closed lifecycle ownership, reconciliation, or cleanup fault."""


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    _ensure_control_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _refuse_symlink(path, label)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise LifecycleError(f"{label} not found: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise LifecycleError(f"{label} must be a JSON object")
    return value


def _maybe_read_json(path: Path, label: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _read_json(path, label)


def _refuse_symlink(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise LifecycleError(f"{label} must not be a symlink: {path}")


def _ensure_private_dir(path: Path, label: str) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise LifecycleError(
            f"{label} must be a same-user 0700 directory: {path}"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise LifecycleError(f"{label} must be a same-user 0700 directory: {path}")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return path


def _ensure_control_dir(path: Path) -> None:
    _ensure_private_dir(path, "control path")


def _control_dir(run_dir: Path) -> Path:
    if not run_dir.is_absolute() or not run_dir.is_dir():
        raise LifecycleError(
            f"run dir must be an existing absolute directory: {run_dir}"
        )
    control = run_dir / "control"
    _ensure_control_dir(control)
    return control


def _lease_path(run_dir: Path) -> Path:
    return _control_dir(run_dir) / "run-owner.json"


def _lock_path(run_dir: Path) -> Path:
    return _control_dir(run_dir) / ".run-owner.lock"


def _now_ns() -> int:
    return time.time_ns()


def _validate_token(value: object, field: str = "owner token") -> str:
    if not isinstance(value, str) or SAFE_NAME_RE.fullmatch(value) is None:
        raise LifecycleError(f"{field} must be a non-empty safe token")
    return value


def _token_hash(token: object) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def _owner_token_dir() -> Path:
    configured = os.environ.get(HERDR_RUN_TOKEN_DIR_ENV)
    directory = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / f"herdr-run-tokens-{os.getuid()}"
    )
    return _ensure_private_dir(directory, "owner token directory")


def owner_token_path(run_dir: Path) -> Path:
    identity = hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()
    return _owner_token_dir() / f"{identity}.token"


def write_owner_token_file(run_dir: Path, token: str) -> Path:
    token = _validate_token(token)
    directory = _owner_token_dir()
    path = owner_token_path(run_dir)
    _refuse_symlink(path, "owner token file")
    temporary = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
    except Exception:
        try:
            temporary.unlink()
        finally:
            raise
    os.replace(temporary, path)
    directory_fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def read_owner_token_file(path: Path) -> str:
    path = path.expanduser()
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise LifecycleError(f"owner token file not found: {path}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise LifecycleError(
            f"owner token file must be a same-user 0600 regular file: {path}"
        )
    return _validate_token(path.read_text().strip(), "owner token file")


def remove_owner_token_file(run_dir: Path) -> bool:
    path = owner_token_path(run_dir)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise LifecycleError(
            f"owner token file is not a same-user regular file: {path}"
        )
    path.unlink()
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def token_from_env_or_file() -> str | None:
    token_file = command_runtime.getenv(HERDR_RUN_OWNER_TOKEN_FILE_ENV)
    if token_file:
        return read_owner_token_file(Path(token_file))
    token = command_runtime.getenv(HERDR_RUN_OWNER_TOKEN_ENV)
    return _validate_token(token) if token else None


def resolve_token_sources(
    direct: str | None,
    token_file: Path | None,
    stdin_text: str | None,
) -> str | None:
    explicit_count = sum(
        source is not None for source in (direct, token_file, stdin_text)
    )
    if explicit_count > 1:
        raise LifecycleError(
            "owner token sources conflict; choose only direct, file, or stdin"
        )
    if token_file is not None:
        return read_owner_token_file(token_file)
    if stdin_text is not None:
        value = stdin_text.strip()
        return _validate_token(value) if value else token_from_env_or_file()
    if direct:
        return _validate_token(direct)
    return token_from_env_or_file()


def _generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LifecycleError("owner generation must be a positive integer")
    return value


def _owner_identity_from_health(
    *,
    kind: str,
    herdr_session: str,
    pane_id: str,
    expected_agent: str,
    expected_process: str,
    expected_cwd: str,
    health: dict[str, object],
    workspace_id: str | None = None,
) -> dict[str, object]:
    process_pid = health.get("process_pid")
    process_start = health.get("process_start_time")
    if (
        isinstance(process_pid, bool)
        or not isinstance(process_pid, int)
        or process_pid <= 0
    ):
        raise LifecycleError("owner health must include a positive process_pid")
    if not isinstance(process_start, str) or not process_start:
        raise LifecycleError("owner health must include process_start_time")
    return {
        "expected_agent": expected_agent,
        "expected_cwd": os.path.realpath(expected_cwd),
        "expected_process": expected_process,
        "herdr_session": herdr_session,
        "kind": kind,
        "pane_id": pane_id,
        "process_pid": process_pid,
        "process_start_time": process_start,
        **({"workspace_id": workspace_id} if workspace_id else {}),
    }


def current_process_identity(kind: str = "operator") -> dict[str, object]:
    return {
        "kind": kind,
        "process_pid": os.getpid(),
        "process_start_time": control._process_start_time(os.getpid()),
    }


def tui_owner_identity(
    tui: dict[str, object], *, repo: Path, harness: str
) -> dict[str, object]:
    health = tui.get("health")
    if not isinstance(health, dict):
        raise LifecycleError("TUI receipt has no health block")
    process_pid = health.get("process_pid")
    if isinstance(process_pid, int) and not isinstance(
        health.get("process_start_time"), str
    ):
        health = {
            **health,
            "process_start_time": control._process_start_time(process_pid),
        }
    pane_id = tui.get("pane_id")
    herdr_session = tui.get("herdr_session")
    if not isinstance(pane_id, str) or not isinstance(herdr_session, str):
        raise LifecycleError("TUI receipt has no pane/session identity")
    launch = {
        "claude": ("claude", "claude"),
        "pi": ("pi", "pi"),
    }
    expected_agent, expected_process = launch.get(harness, (harness, harness))
    raw_workspace_id = tui.get("workspace_id")
    workspace_id = raw_workspace_id if isinstance(raw_workspace_id, str) else None
    return _owner_identity_from_health(
        kind="tui",
        herdr_session=herdr_session,
        pane_id=pane_id,
        expected_agent=expected_agent,
        expected_process=expected_process,
        expected_cwd=str(repo),
        health=cast(dict[str, object], health),
        workspace_id=workspace_id,
    )


def _fresh(lease: dict[str, Any], now_ns: int | None = None) -> bool:
    now = now_ns or _now_ns()
    ttl = lease.get("heartbeat_ttl_seconds", DEFAULT_HEARTBEAT_TTL_SECONDS)
    heartbeat = lease.get("heartbeat_at_ns")
    return (
        isinstance(ttl, int)
        and ttl > 0
        and isinstance(heartbeat, int)
        and heartbeat + ttl * 1_000_000_000 >= now
    )


def _same_process_identity(left: dict[str, Any], right: dict[str, object]) -> bool:
    return (
        left.get("process_pid") == right.get("process_pid")
        and isinstance(left.get("process_start_time"), str)
        and left.get("process_start_time") == right.get("process_start_time")
    )


def _process_identity_live(identity: dict[str, Any]) -> bool:
    pid = identity.get("process_pid")
    recorded = identity.get("process_start_time")
    return (
        isinstance(recorded, str)
        and bool(recorded)
        and control._process_start_time(pid) == recorded
    )


def _pane_identity_verified(identity: dict[str, Any]) -> tuple[bool, str]:
    pane_id = identity.get("pane_id")
    herdr_session = identity.get("herdr_session")
    if not isinstance(pane_id, str) or not isinstance(herdr_session, str):
        return False, "owner has no recorded pane/session"
    try:
        pane = (
            control._herdr_json("pane", "get", pane_id, herdr_session=herdr_session)
            .get("result", {})
            .get("pane", {})
        )
        process_info = (
            control._herdr_json(
                "pane", "process-info", "--pane", pane_id, herdr_session=herdr_session
            )
            .get("result", {})
            .get("process_info", {})
        )
    except control.ControllerTransportError as error:
        return False, f"pane lookup failed: {error}"
    if not isinstance(pane, dict) or pane.get("pane_id", pane_id) != pane_id:
        return False, "pane identity changed"
    expected_agent = identity.get("expected_agent")
    expected_process = identity.get("expected_process")
    expected_cwd = identity.get("expected_cwd")
    cwd = pane.get("foreground_cwd", pane.get("cwd"))
    if isinstance(expected_agent, str) and pane.get("agent") != expected_agent:
        return False, "pane agent does not match owner identity"
    if isinstance(expected_cwd, str) and (
        not isinstance(cwd, str)
        or os.path.realpath(cwd) != os.path.realpath(expected_cwd)
    ):
        return False, "pane cwd does not match owner identity"
    processes = (
        process_info.get("foreground_processes", [])
        if isinstance(process_info, dict)
        else []
    )
    match = next(
        (
            process
            for process in processes
            if isinstance(process, dict)
            and isinstance(expected_process, str)
            and (
                process.get("name") == expected_process
                or expected_process in str(process.get("cmdline", ""))
                or expected_process
                in " ".join(str(item) for item in process.get("argv", []))
            )
        ),
        None,
    )
    if not isinstance(match, dict):
        return False, "expected foreground process is absent"
    if match.get("pid") != identity.get("process_pid") or control._process_start_time(
        match.get("pid")
    ) != identity.get("process_start_time"):
        return False, "foreground process identity no longer matches owner"
    return True, "identity-verified live pane"


def _owner_live_status(lease: dict[str, Any]) -> dict[str, object]:
    identity = lease.get("owner")
    if not isinstance(identity, dict):
        return {"fresh": False, "live": False, "reason": "lease has no owner identity"}
    if identity.get("kind") == "tui":
        live, reason = _pane_identity_verified(identity)
    else:
        live = _process_identity_live(identity)
        reason = (
            "identity-verified live process"
            if live
            else "owner process is absent or reused"
        )
    fresh = _fresh(lease)
    return {
        "fresh": fresh,
        "live": live,
        "reason": reason,
        "state": "healthy" if live and fresh else "stale",
    }


def read_owner_lease(run_dir: Path) -> dict[str, Any] | None:
    path = _lease_path(run_dir)
    if not path.exists():
        return None
    lease = _read_json(path, "run owner lease")
    if lease.get("schema") != SCHEMA:
        raise LifecycleError("run owner lease has the wrong schema")
    _generation(lease.get("generation"))
    _validate_token(lease.get("token"))
    owner = lease.get("owner")
    if not isinstance(owner, dict):
        raise LifecycleError("run owner lease has no owner identity")
    return lease


def _lease_receipt(
    lease: dict[str, Any], role: str, reason: str | None = None
) -> dict[str, object]:
    status = _owner_live_status(lease)
    return {
        "generation": lease["generation"],
        "heartbeat_at_ns": lease.get("heartbeat_at_ns"),
        "heartbeat_ttl_seconds": lease.get("heartbeat_ttl_seconds"),
        "owner": lease["owner"],
        "owner_status": status,
        "reason": reason,
        "role": role,
        "schema": SCHEMA,
        "token": lease["token"] if role == "owner" else None,
        "token_sha256": _token_hash(lease.get("token")),
    }


def public_owner_receipt(receipt: dict[str, object] | None) -> dict[str, object] | None:
    if receipt is None:
        return None
    return {key: value for key, value in receipt.items() if key != "token"}


def acquire_owner(
    run_dir: Path,
    *,
    owner: dict[str, object],
    takeover_stale: bool = False,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> dict[str, object]:
    if ttl_seconds <= 0:
        raise LifecycleError("heartbeat TTL must be positive")
    with _exclusive(_lock_path(run_dir)):
        existing = read_owner_lease(run_dir)
        if existing is not None:
            status = _owner_live_status(existing)
            if _same_process_identity(cast(dict[str, Any], existing["owner"]), owner):
                existing["heartbeat_at_ns"] = _now_ns()
                existing["heartbeat_ttl_seconds"] = ttl_seconds
                _write_json_atomic(_lease_path(run_dir), existing)
                return _lease_receipt(existing, "owner", "same owner refreshed")
            if status["live"] is True and status["fresh"] is True:
                return _lease_receipt(
                    existing, "observer", "another live owner holds the run"
                )
            if not takeover_stale:
                return _lease_receipt(
                    existing,
                    "observer",
                    "owner is stale; reconcile before explicit takeover",
                )
            _require_stale_reconciliation(run_dir, existing)
            generation = _generation(existing["generation"]) + 1
        else:
            generation = 1
        token = uuid.uuid4().hex
        lease = {
            "acquired_at_ns": _now_ns(),
            "generation": generation,
            "heartbeat_at_ns": _now_ns(),
            "heartbeat_ttl_seconds": ttl_seconds,
            "owner": owner,
            "schema": SCHEMA,
            "token": token,
        }
        _write_json_atomic(_lease_path(run_dir), lease)
        return _lease_receipt(lease, "owner")


def refresh_owner(
    run_dir: Path,
    *,
    token: str,
    owner: dict[str, object] | None = None,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> dict[str, object]:
    token = _validate_token(token)
    with _exclusive(_lock_path(run_dir)):
        lease = read_owner_lease(run_dir)
        if lease is None:
            raise LifecycleError("cannot refresh: run has no owner lease")
        if lease.get("token") != token:
            raise LifecycleError("owner token does not match the current lease")
        if owner is not None:
            lease["owner"] = owner
        lease["heartbeat_at_ns"] = _now_ns()
        lease["heartbeat_ttl_seconds"] = ttl_seconds
        _write_json_atomic(_lease_path(run_dir), lease)
        return _lease_receipt(lease, "owner")


def heartbeat_once(
    run_dir: Path,
    *,
    token: str,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> dict[str, object]:
    lease = read_owner_lease(run_dir)
    if lease is None:
        raise LifecycleError("cannot heartbeat: run has no owner lease")
    if lease.get("token") != _validate_token(token):
        raise LifecycleError("heartbeat token does not match current lease")
    status = _owner_live_status(lease)
    if status.get("live") is not True:
        raise LifecycleError("heartbeat owner identity is no longer live")
    return refresh_owner(run_dir, token=token, ttl_seconds=ttl_seconds)


def heartbeat_loop(
    run_dir: Path,
    *,
    token: str,
    interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    sleep: Any = time.sleep,
) -> dict[str, object]:
    if interval_seconds <= 0 or interval_seconds >= ttl_seconds:
        raise LifecycleError("heartbeat interval must be positive and less than TTL")
    while True:
        try:
            heartbeat_once(run_dir, token=token, ttl_seconds=ttl_seconds)
        except LifecycleError as error:
            return {
                "reason": str(error),
                "schema": SCHEMA,
                "state": "exited",
                "token_sha256": _token_hash(token),
            }
        sleep(interval_seconds)


def start_background_heartbeat(
    run_dir: Path,
    *,
    token: str,
    interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> dict[str, object]:
    env = {
        **os.environ,
        HERDR_RUN_DIR_ENV: str(run_dir),
        HERDR_RUN_OWNER_TOKEN_FILE_ENV: str(write_owner_token_file(run_dir, token)),
    }
    env.pop(HERDR_RUN_OWNER_TOKEN_ENV, None)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "heartbeat-loop",
            str(run_dir),
            "--interval-seconds",
            str(interval_seconds),
            "--ttl-seconds",
            str(ttl_seconds),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    receipt = {
        "interval_seconds": interval_seconds,
        "pid": process.pid,
        "schema": SCHEMA,
        "state": "started",
        "token_sha256": _token_hash(token),
        "ttl_seconds": ttl_seconds,
    }
    _write_json_atomic(_control_dir(run_dir) / "heartbeat.json", receipt)
    return receipt


def _require_stale_reconciliation(run_dir: Path, stale_lease: dict[str, Any]) -> None:
    try:
        receipt = _read_json(
            _control_dir(run_dir) / "reconciliation.json", "reconciliation receipt"
        )
    except LifecycleError as error:
        raise LifecycleError(
            f"stale takeover requires a v1 reconciliation receipt: {error}"
        ) from error
    if receipt.get("schema") != RECONCILE_SCHEMA:
        raise LifecycleError("stale takeover requires a v1 reconciliation receipt")
    owner = receipt.get("owner")
    if not isinstance(owner, dict):
        raise LifecycleError("reconciliation receipt has no owner block")
    if owner.get("token_sha256") != _token_hash(stale_lease.get("token")) or owner.get(
        "generation"
    ) != stale_lease.get("generation"):
        raise LifecycleError(
            "reconciliation receipt does not cover the current stale lease"
        )
    status = owner.get("status")
    if not isinstance(status, dict) or status.get("state") != "stale":
        raise LifecycleError(
            "reconciliation receipt did not classify the owner as stale"
        )


def guard(
    run_dir: Path,
    *,
    action: str,
    token: str | None = None,
) -> dict[str, object]:
    if action not in MUTATING_ACTIONS | READ_ACTIONS:
        raise LifecycleError("guard action is not recognized")
    lease = read_owner_lease(run_dir)
    if lease is None:
        if action in READ_ACTIONS:
            return {
                "allowed": True,
                "reason": "read allowed without owner",
                "schema": SCHEMA,
            }
        raise LifecycleError("mutation refused: run has no owner lease")
    status = _owner_live_status(lease)
    if action in READ_ACTIONS:
        return {"allowed": True, "owner_status": status, "schema": SCHEMA}
    if token is None:
        raise LifecycleError("mutation refused: owner token is required")
    token = _validate_token(token)
    if token != lease.get("token"):
        raise LifecycleError(
            "mutation refused: owner token does not match current lease"
        )
    if status["live"] is not True or status["fresh"] is not True:
        raise LifecycleError(
            "mutation refused: owner supervision is stale or not identity-verified"
        )
    return {"allowed": True, "owner_status": status, "schema": SCHEMA}


def _typed_terminal(run_dir: Path) -> dict[str, object] | None:
    terminal = _maybe_read_json(
        _control_dir(run_dir) / "run-terminal.json", "run terminal receipt"
    )
    if terminal is None:
        return None
    state = terminal.get("state")
    if state not in ("succeeded", "stopped"):
        raise LifecycleError("run terminal receipt has an unsupported state")
    return {"detail": terminal, "source": "typed-terminal", "state": state}


def _safe_json_for_snapshot(
    path: Path, label: str, errors: list[dict[str, object]]
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return _read_json(path, label)
    except LifecycleError as error:
        errors.append({"path": str(path), "reason": str(error), "source": label})
        return None


def _phase_ids(run_dir: Path) -> list[str]:
    route_path = run_dir / "route.yaml"
    if not route_path.is_file():
        return []
    try:
        import yaml

        route = yaml.safe_load(route_path.read_text())
    except (OSError, ImportError, AttributeError) as error:
        raise LifecycleError(
            f"route could not be read for snapshot: {error}"
        ) from error
    if not isinstance(route, dict):
        return []
    phases = route.get("phases")
    if not isinstance(phases, dict):
        return []
    return [phase for phase in phases if isinstance(phase, str)]


def _phase_receipts(
    run_dir: Path, errors: list[dict[str, object]]
) -> dict[str, object]:
    values: dict[str, object] = {}
    for phase_id in _phase_ids(run_dir):
        phase_dir = run_dir / "phases" / phase_id
        result = _safe_json_for_snapshot(
            phase_dir / "phase-result.json", f"{phase_id} result", errors
        )
        starts = []
        for path in sorted(phase_dir.glob("pass-*/control/phase-start.json")):
            value = _safe_json_for_snapshot(path, f"{phase_id} phase start", errors)
            if value is not None:
                starts.append(value)
        values[phase_id] = {
            "phase_result": result,
            "phase_starts": starts,
        }
    return values


def _active_leaders(
    run_dir: Path, errors: list[dict[str, object]] | None = None
) -> dict[str, object]:
    path = run_dir / "active-leader-panes.json"
    value = (
        _safe_json_for_snapshot(path, "active leader panes", errors)
        if errors is not None
        else _maybe_read_json(path, "active leader panes")
    )
    return value or {}


def _cleanup_reports(
    run_dir: Path, errors: list[dict[str, object]]
) -> list[dict[str, object]]:
    reports = []
    for path in sorted((run_dir / "control").glob("cleanup*.json")):
        value = _safe_json_for_snapshot(path, "cleanup report", errors)
        if value is not None:
            reports.append({"path": str(path), "value": value})
    return reports


def _upagent_claims() -> list[dict[str, object]]:
    """Never inspect a checkout-local ledger; the canonical Hub owns claim projection."""
    return [{"projection": "canonical-hub-only"}]


def _derive_run_state(
    run_dir: Path,
    owner_status: dict[str, object],
    phases: dict[str, object],
    errors: list[dict[str, object]],
) -> dict[str, object]:
    try:
        terminal = _typed_terminal(run_dir)
    except LifecycleError as error:
        errors.append(
            {
                "path": str(_control_dir(run_dir) / "run-terminal.json"),
                "reason": str(error),
                "source": "run terminal receipt",
            }
        )
        terminal = None
    if terminal is not None:
        return terminal
    phase_values = [
        cast(dict[str, object], value).get("phase_result") for value in phases.values()
    ]
    if phase_values and all(
        isinstance(value, dict) and value.get("verdict") in TERMINAL_VERDICTS
        for value in phase_values
    ):
        verdicts = [
            cast(dict[str, object], value).get("verdict") for value in phase_values
        ]
        return {
            "detail": {"phase_verdicts": verdicts},
            "source": "typed-phase-results",
            "state": "succeeded"
            if all(verdict == "passed" for verdict in verdicts)
            else "blocked",
        }
    if owner_status.get("live") is True and owner_status.get("fresh") is True:
        return {
            "detail": owner_status,
            "source": "identity-verified-live-pane",
            "state": "running",
        }
    return {
        "detail": "no typed terminal result and no identity-verified fresh owner",
        "source": "prose-untrusted",
        "state": "unknown",
    }


def snapshot(run_dir: Path) -> dict[str, object]:
    errors: list[dict[str, object]] = []
    lease = read_owner_lease(run_dir)
    owner_status = (
        _owner_live_status(lease)
        if lease is not None
        else {"state": "absent", "live": False, "fresh": False}
    )
    phases = _phase_receipts(run_dir, errors)
    return {
        "active_leaders": _active_leaders(run_dir, errors),
        "cleanup": _cleanup_reports(run_dir, errors),
        "generated_at_ns": _now_ns(),
        "owner": {
            "generation": lease.get("generation") if lease else None,
            "identity": lease.get("owner") if lease else None,
            "status": owner_status,
            "token_sha256": _token_hash(lease.get("token") if lease else None),
        },
        "phases": phases,
        "plan": {
            "plan_start": _safe_json_for_snapshot(
                _control_dir(run_dir) / "plan-start.json", "plan start receipt", errors
            ),
            "run_terminal": _safe_json_for_snapshot(
                _control_dir(run_dir) / "run-terminal.json",
                "run terminal receipt",
                errors,
            ),
        },
        "run_dir": str(run_dir),
        "run_state": _derive_run_state(run_dir, owner_status, phases, errors),
        "schema": SNAPSHOT_SCHEMA,
        "shared_environment": {"state": "not-configured"},
        "source_errors": errors,
        "upagent_claims": _upagent_claims(),
    }


def reconcile(run_dir: Path) -> dict[str, object]:
    snap = snapshot(run_dir)
    owner = snap["owner"] if isinstance(snap["owner"], dict) else {}
    receipt = {
        "at_ns": _now_ns(),
        "owner": {
            "generation": owner.get("generation"),
            "status": owner.get("status"),
            "token_sha256": owner.get("token_sha256"),
        },
        "precedence": [
            "typed terminal/result",
            "identity-verified live pane",
            "prose",
        ],
        "run_state": snap["run_state"],
        "schema": RECONCILE_SCHEMA,
        "snapshot": snap,
    }
    _write_json_atomic(_control_dir(run_dir) / "reconciliation.json", receipt)
    return receipt


def _repo_git_state(repo: Path) -> dict[str, object]:
    if not repo.is_absolute() or not repo.is_dir():
        return {
            "clean": False,
            "landed": False,
            "reason": f"repo must be an existing absolute directory: {repo}",
        }
    git = shutil.which("git")
    if git is None:
        return {
            "clean": False,
            "landed": False,
            "reason": "git not found; worktree uninspectable",
        }
    try:
        status = subprocess.run(
            [git, "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        head = subprocess.run(
            [git, "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        contains = subprocess.run(
            [git, "-C", str(repo), "branch", "-r", "--contains", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "clean": False,
            "landed": False,
            "reason": f"git inspection failed: {error}",
        }
    if status.returncode != 0:
        return {
            "clean": False,
            "landed": False,
            "reason": f"git status failed: {status.stderr.strip()}",
        }
    if head.returncode != 0:
        return {
            "clean": False,
            "landed": False,
            "reason": f"git HEAD inspection failed: {head.stderr.strip()}",
        }
    if contains.returncode != 0:
        return {
            "clean": False,
            "landed": False,
            "reason": f"git remote reachability failed: {contains.stderr.strip()}",
        }
    dirty = bool(status.stdout.strip())
    remote_branches = [
        line.strip() for line in contains.stdout.splitlines() if line.strip()
    ]
    landed = bool(remote_branches)
    reason = None
    if dirty:
        reason = "repo has uncommitted changes"
    elif not landed:
        reason = "repo HEAD is not reachable from any remote branch"
    return {
        "clean": not dirty,
        "dirty": dirty,
        "head": head.stdout.strip(),
        "landed": landed,
        "porcelain": status.stdout,
        "reason": reason,
        "remote_branches": remote_branches,
    }


def _cleanup_decisions(
    run_dir: Path, source_errors: list[dict[str, object]]
) -> list[dict[str, object]]:
    try:
        plan = _maybe_read_json(
            _control_dir(run_dir) / "plan-start.json", "plan start receipt"
        )
    except LifecycleError as error:
        source_errors.append({"source": "plan start receipt", "reason": str(error)})
        return [
            {
                "action": "preserve",
                "reason": "plan-start receipt is malformed",
                "resource": "plan-start",
            }
        ]
    decisions: list[dict[str, object]] = []
    if plan is None:
        return [
            {
                "resource": "plan-start",
                "action": "preserve",
                "reason": "no plan-start receipt",
            }
        ]
    tui = plan.get("tui")
    if not isinstance(tui, dict):
        return [
            {
                "resource": "tui",
                "action": "preserve",
                "reason": "malformed or missing TUI receipt",
            }
        ]
    decisions.extend(_resource_cleanup_decisions("tui-pane", tui))
    try:
        leaders = _active_leaders(run_dir)
    except LifecycleError as error:
        source_errors.append(
            {
                "path": str(run_dir / "active-leader-panes.json"),
                "reason": str(error),
                "source": "active leader panes",
            }
        )
        decisions.append(
            {
                "action": "preserve",
                "reason": "active leader map is malformed",
                "resource": "phase-leaders",
            }
        )
        return decisions
    if leaders:
        for phase_id, leader in leaders.items():
            if not isinstance(leader, dict):
                decisions.append(
                    {
                        "action": "preserve",
                        "reason": "active leader record is malformed",
                        "resource": f"phase-leader:{phase_id}",
                    }
                )
                continue
            decisions.extend(
                _resource_cleanup_decisions(f"phase-leader:{phase_id}", leader)
            )
    return decisions


def _resource_cleanup_decisions(
    resource: str, receipt: dict[str, Any]
) -> list[dict[str, object]]:
    ownership = receipt.get("ownership")
    pane = ownership.get("pane") if isinstance(ownership, dict) else None
    if not isinstance(pane, dict):
        return [
            {
                "resource": resource,
                "action": "preserve",
                "reason": "missing structural pane ownership",
            }
        ]
    pane_id = pane.get("pane_id")
    state = pane.get("state")
    session = receipt.get("herdr_session")
    health = receipt.get("health")
    if state == "adopted":
        return [
            {
                "resource": resource,
                "pane_id": pane_id,
                "action": "preserve",
                "reason": "adopted pane",
            }
        ]
    if state != "created":
        return [
            {
                "resource": resource,
                "pane_id": pane_id,
                "action": "preserve",
                "reason": "pane ownership is not created/adopted",
            }
        ]
    if (
        not isinstance(pane_id, str)
        or not isinstance(session, str)
        or session == "default"
    ):
        return [
            {
                "resource": resource,
                "pane_id": pane_id,
                "action": "preserve",
                "reason": "missing recorded non-default Herdr session",
            }
        ]
    if not isinstance(health, dict):
        return [
            {
                "resource": resource,
                "pane_id": pane_id,
                "action": "preserve",
                "reason": "missing startup health identity",
            }
        ]
    try:
        if pane_id not in _live_pane_ids(session):
            return [
                {
                    "action": "already-absent",
                    "herdr_session": session,
                    "pane_id": pane_id,
                    "resource": resource,
                    "verified_absent": True,
                }
            ]
    except LifecycleError as error:
        return [
            {
                "action": "preserve",
                "pane_id": pane_id,
                "reason": f"could not verify pane absence: {error}",
                "resource": resource,
            }
        ]
    identity = {
        "expected_agent": health.get("expected_agent"),
        "expected_cwd": health.get("cwd"),
        "expected_process": health.get("expected_process"),
        "herdr_session": session,
        "kind": "tui",
        "pane_id": pane_id,
        "process_pid": health.get("process_pid"),
        "process_start_time": health.get("process_start_time"),
    }
    verified, reason = _pane_identity_verified(cast(dict[str, Any], identity))
    if not verified:
        return [
            {
                "resource": resource,
                "pane_id": pane_id,
                "action": "preserve",
                "reason": reason,
            }
        ]
    return [
        {
            "resource": resource,
            "pane_id": pane_id,
            "herdr_session": session,
            "action": "close",
        }
    ]


def _live_pane_ids(herdr_session: str) -> set[str]:
    panes = (
        control._herdr_json("pane", "list", herdr_session=herdr_session)
        .get("result", {})
        .get("panes", [])
    )
    if not isinstance(panes, list):
        raise LifecycleError("Herdr pane list returned no panes array")
    return {
        pane["pane_id"]
        for pane in panes
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
    }


def cleanup(
    run_dir: Path, *, repo: Path, token: str | None = None
) -> dict[str, object]:
    refusal_errors: list[str] = []
    source_errors: list[dict[str, object]] = []
    if token is None:
        raise LifecycleError("cleanup requires an owner token")
    guard(run_dir, action="cleanup", token=token)
    repo_state = _repo_git_state(repo)
    if repo_state.get("clean") is not True or repo_state.get("landed") is not True:
        refusal_errors.append(
            str(repo_state.get("reason") or "repo state is not safely landed")
        )
    decisions = _cleanup_decisions(run_dir, source_errors)
    for decision in decisions:
        if decision.get("action") not in ("close", "already-absent"):
            refusal_errors.append(str(decision.get("reason", "preserved resource")))
    report: dict[str, object] = {
        "at_ns": _now_ns(),
        "decisions": decisions,
        "errors": refusal_errors,
        "preflight": {"repo": repo_state},
        "schema": CLEANUP_SCHEMA,
        "source_errors": source_errors,
        "state": "refused" if refusal_errors else "preflight-passed",
    }
    if refusal_errors:
        _write_json_atomic(_control_dir(run_dir) / "cleanup-report.json", report)
        raise LifecycleError("cleanup refused: " + "; ".join(refusal_errors))
    closed: list[dict[str, object]] = []
    for decision in decisions:
        if decision.get("action") == "close":
            pane_id = cast(str, decision["pane_id"])
            session = cast(str, decision["herdr_session"])
            control._herdr("pane", "close", pane_id, herdr_session=session)
            verified_absent = pane_id not in _live_pane_ids(session)
            closed.append(
                {
                    "herdr_session": session,
                    "pane_id": pane_id,
                    "verified_absent": verified_absent,
                }
            )
            if not verified_absent:
                report = {
                    **report,
                    "closed": closed,
                    "errors": [
                        *refusal_errors,
                        f"pane {pane_id} remained live after close",
                    ],
                    "state": "cleanup-failed",
                }
                _write_json_atomic(
                    _control_dir(run_dir) / "cleanup-report.json", report
                )
                raise LifecycleError(
                    f"cleanup failed: pane {pane_id} remained live after close"
                )
    token_removed = remove_owner_token_file(run_dir)
    report = {
        **report,
        "closed": closed,
        "owner_token_removed": token_removed,
        "state": "closed",
    }
    _write_json_atomic(_control_dir(run_dir) / "cleanup-report.json", report)
    return report


def _resolve_run_dir(value: Path, repo: Path) -> Path:
    path = value.expanduser()
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _request_cwd() -> Path:
    return command_runtime.current_cwd()


def main(argv: list[str] | None = None) -> int:
    parser = command_runtime.ArgumentParser(prog="herdr-run-lifecycle")
    parser.add_argument("--repo", type=Path, default=_request_cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "session-start",
        "heartbeat",
        "heartbeat-loop",
        "snapshot",
        "reconcile",
        "cleanup",
    ):
        child = sub.add_parser(name)
        child.add_argument("run_dir", type=Path)
    sub.choices["session-start"].add_argument("--takeover-stale", action="store_true")
    sub.choices["session-start"].add_argument(
        "--ttl-seconds", type=int, default=DEFAULT_HEARTBEAT_TTL_SECONDS
    )
    sub.choices["heartbeat"].add_argument(
        "--ttl-seconds", type=int, default=DEFAULT_HEARTBEAT_TTL_SECONDS
    )
    sub.choices["heartbeat-loop"].add_argument(
        "--interval-seconds", type=int, default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    )
    sub.choices["heartbeat-loop"].add_argument(
        "--ttl-seconds", type=int, default=DEFAULT_HEARTBEAT_TTL_SECONDS
    )
    sub.choices["cleanup"].add_argument("--token")
    sub.choices["cleanup"].add_argument("--token-file", type=Path)
    sub.choices["cleanup"].add_argument("--token-stdin", action="store_true")
    guard_parser = sub.add_parser("guard")
    guard_parser.add_argument("run_dir", type=Path)
    guard_parser.add_argument("--action", required=True)
    guard_parser.add_argument("--token")
    guard_parser.add_argument("--token-file", type=Path)
    guard_parser.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    run_dir = _resolve_run_dir(args.run_dir, repo)
    try:
        if args.command == "session-start":
            result = acquire_owner(
                run_dir,
                owner=current_process_identity("session-start"),
                takeover_stale=args.takeover_stale,
                ttl_seconds=args.ttl_seconds,
            )
            result = public_owner_receipt(cast(dict[str, object], result))
        elif args.command == "heartbeat":
            token = token_from_env_or_file()
            if not token:
                raise LifecycleError(
                    f"{HERDR_RUN_OWNER_TOKEN_FILE_ENV} or {HERDR_RUN_OWNER_TOKEN_ENV} is required"
                )
            result = public_owner_receipt(
                heartbeat_once(run_dir, token=token, ttl_seconds=args.ttl_seconds)
            )
        elif args.command == "heartbeat-loop":
            token = token_from_env_or_file()
            if not token:
                raise LifecycleError(
                    f"{HERDR_RUN_OWNER_TOKEN_FILE_ENV} or {HERDR_RUN_OWNER_TOKEN_ENV} is required"
                )
            result = heartbeat_loop(
                run_dir,
                token=token,
                interval_seconds=args.interval_seconds,
                ttl_seconds=args.ttl_seconds,
            )
        elif args.command == "snapshot":
            result = snapshot(run_dir)
        elif args.command == "reconcile":
            result = reconcile(run_dir)
        elif args.command == "guard":
            token = resolve_token_sources(
                args.token,
                args.token_file,
                command_runtime.stdin_stream().read() if args.token_stdin else None,
            )
            result = guard(run_dir, action=args.action, token=token)
        elif args.command == "cleanup":
            token = resolve_token_sources(
                args.token,
                args.token_file,
                command_runtime.stdin_stream().read() if args.token_stdin else None,
            )
            result = cleanup(run_dir, repo=repo, token=token)
        else:
            raise AssertionError(args.command)
    except (LifecycleError, OSError, control.ControllerTransportError) as error:
        command_runtime.write_stderr(f"herdr-run-lifecycle: {error}\n")
        return 1
    command_runtime.command_print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
