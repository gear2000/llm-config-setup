#!/usr/bin/env python3
"""Thin client for the one canonical machine-local UpAgent Hub.

The client performs repository/socket discovery and the protocol handshake only.  It contains no
Recruiter import, ledger access, reconciliation implementation, lifecycle dispatch, or worker
runner launch path.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "upagent_hub_transport", HERE / "hub_transport.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("could not load UpAgent Hub transport")
transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transport)

_public_spec = importlib.util.spec_from_file_location(
    "upagent_public_contract_client", HERE / "public_contract.py"
)
if _public_spec is None or _public_spec.loader is None:
    raise RuntimeError("could not load UpAgent public command contract")
public_contract = importlib.util.module_from_spec(_public_spec)
_public_spec.loader.exec_module(public_contract)

STARTUP_TIMEOUT_SECONDS = 8.0
MIGRATION_TIMEOUT_SECONDS = 8.0
HUB_LOG_SUFFIX = ".log"


class ClientError(RuntimeError):
    """A Hub discovery, startup, or protocol fault."""


def _round_trip(
    socket_path: Path,
    target: str,
    argv: list[str],
    *,
    cwd: Path,
    protocol_version: int = transport.PROTOCOL_VERSION,
    protocol_fingerprint: str = transport.PROTOCOL_FINGERPRINT,
    caller_context: dict[str, str] | None = None,
    request_stdin: str | None = None,
) -> dict[str, Any]:
    request_context = transport.validate_caller_context(
        transport.caller_context() if caller_context is None else caller_context
    )
    try:
        connection = transport.connect(socket_path)
    except OSError as error:
        raise ClientError(
            f"UpAgent Hub is unavailable at {socket_path}: {error}"
        ) from error
    with connection:
        connection.settimeout(None)
        stream = connection.makefile("rwb", buffering=0)
        transport.write_frame(
            stream,
            {
                "protocol_fingerprint": protocol_fingerprint,
                "protocol_version": protocol_version,
                "type": "hello",
            },
        )
        hello = transport.read_frame(stream)
        if hello.get("type") == "error":
            raise ClientError(str(hello.get("error", "Hub rejected the handshake")))
        if (
            hello.get("type") != "hello"
            or hello.get("protocol_version") != protocol_version
            or hello.get("protocol_fingerprint") != protocol_fingerprint
            or not isinstance(hello.get("identity"), dict)
        ):
            raise ClientError(f"invalid Hub handshake response: {hello}")
        transport.write_frame(
            stream,
            {
                "argv": argv,
                "caller_context": request_context,
                "cwd": str(cwd),
                "protocol_fingerprint": protocol_fingerprint,
                "protocol_version": protocol_version,
                "stdin": transport.validate_request_stdin(target, argv, request_stdin),
                "target": target,
                "type": "request",
            },
        )
        response = transport.read_frame(stream)
    if response.get("type") == "error":
        raise ClientError(str(response.get("error", "Hub rejected the request")))
    if response.get("type") != "response":
        raise ClientError(f"invalid Hub response: {response}")
    if not isinstance(response.get("exit_code"), int):
        raise ClientError("Hub response has no integer exit_code")
    for field in ("stdout", "stderr"):
        if not isinstance(response.get(field), str):
            raise ClientError(f"Hub response has no string {field}")
    return response


def _probe_protocol_ready(socket_path: Path, cwd: Path) -> dict[str, Any] | None:
    """Return a status response only after the socket completes the Hub protocol."""
    try:
        response = _round_trip(socket_path, "hub", ["status"], cwd=cwd)
    except (ClientError, OSError, transport.ProtocolError):
        return None
    identity = response.get("identity")
    if (
        response["exit_code"] != 0
        or not isinstance(identity, dict)
        or identity.get("protocol_version") != transport.PROTOCOL_VERSION
        or identity.get("protocol_fingerprint") != transport.PROTOCOL_FINGERPRINT
        or identity.get("socket_path") != str(socket_path.resolve())
    ):
        return None
    return response


def _legacy_status(
    socket_path: Path, protocol_version: int, cwd: Path
) -> dict[str, Any]:
    """Read an older Hub's status using the pre-fingerprint handshake only for migration."""

    try:
        connection = transport.connect(socket_path)
    except OSError as error:
        raise ClientError(
            f"resident UpAgent Hub is unavailable at {socket_path}: {error}"
        ) from error
    with connection:
        connection.settimeout(5)
        stream = connection.makefile("rwb", buffering=0)
        transport.write_frame(
            stream, {"protocol_version": protocol_version, "type": "hello"}
        )
        hello = transport.read_frame(stream)
        if (
            hello.get("type") != "hello"
            or hello.get("protocol_version") != protocol_version
            or not isinstance(hello.get("identity"), dict)
        ):
            raise ClientError(f"resident Hub rejected legacy status probe: {hello}")
        transport.write_frame(
            stream,
            {
                "argv": ["status"],
                "caller_context": {},
                "cwd": str(cwd),
                "protocol_version": protocol_version,
                "target": "hub",
                "type": "request",
            },
        )
        response = transport.read_frame(stream)
    if response.get("type") != "response" or response.get("exit_code") != 0:
        raise ClientError(f"resident Hub returned invalid legacy status: {response}")
    return response


def _resident_identity(socket_path: Path) -> dict[str, Any] | None:
    identity_path = socket_path.with_name(f"{socket_path.name}.identity.json")
    try:
        metadata = identity_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ClientError(f"cannot inspect resident Hub identity: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ClientError(
            f"resident Hub identity is not a same-user regular file: {identity_path}"
        )
    try:
        value = json.loads(identity_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ClientError(f"resident Hub identity is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise ClientError("resident Hub identity must be an object")
    return value


def _process_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as error:
        raise ClientError(
            f"cannot inspect resident Hub command line: {error}"
        ) from error
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _migration_guidance(socket_path: Path, reason: str) -> ClientError:
    return ClientError(
        f"resident UpAgent Hub at {socket_path} is incompatible and cannot be "
        f"restarted automatically: {reason}. Confirm that it has zero active workers, "
        "stop its exact published PID, remove only its stale socket/identity after that "
        "process exits, then rerun `just upagent-up`"
    )


def _migrate_incompatible_hub(socket_path: Path, cwd: Path) -> bool:
    """Stop an old canonical zero-worker Hub only after exact live-process verification."""

    identity = _resident_identity(socket_path)
    if identity is None:
        return False
    version = identity.get("protocol_version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise _migration_guidance(
            socket_path, "its identity has no valid protocol version"
        )
    if (
        version == transport.PROTOCOL_VERSION
        and identity.get("protocol_fingerprint") == transport.PROTOCOL_FINGERPRINT
    ):
        return False
    canonical_hub = transport.canonical_module_path("hub.py", cwd)
    canonical_engine = transport.canonical_module_path("recruiter.py", cwd)
    if identity.get("hub_path") != str(canonical_hub) or identity.get(
        "canonical_engine_path"
    ) != str(canonical_engine):
        raise _migration_guidance(
            socket_path,
            "its published canonical executable paths do not match this checkout",
        )
    pid = identity.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise _migration_guidance(socket_path, "its published PID is invalid")
    start_time = transport.process_start_time(pid)
    if start_time is None:
        raise _migration_guidance(socket_path, "its published process is not live")
    cmdline = _process_cmdline(pid)
    if len(cmdline) < 2 or Path(cmdline[1]).resolve() != canonical_hub:
        raise _migration_guidance(
            socket_path, "the published PID is not running the canonical Hub script"
        )
    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
    except OSError as error:
        raise _migration_guidance(
            socket_path, f"its interpreter executable cannot be verified: {error}"
        ) from error
    if executable != Path(sys.executable).resolve():
        raise _migration_guidance(
            socket_path, "the published PID uses a different Python executable"
        )
    response = _legacy_status(socket_path, version, cwd)
    live_identity = response.get("identity")
    identity_keys = (
        "canonical_engine_path",
        "hub_instance_id",
        "hub_path",
        "ledger_path",
        "pid",
        "protocol_version",
        "socket_path",
        "started_at_ns",
    )
    if not isinstance(live_identity, dict) or any(
        live_identity.get(key) != identity.get(key) for key in identity_keys
    ):
        raise _migration_guidance(
            socket_path,
            "its live handshake does not match its published PID/start identity",
        )
    ledger_path = identity.get("ledger_path")
    expected_ledger = socket_path.with_name(f"{socket_path.name}.ledger").resolve()
    if (
        not isinstance(ledger_path, str)
        or Path(ledger_path).resolve() != expected_ledger
    ):
        raise _migration_guidance(
            socket_path,
            "its active-worker ledger is not the canonical socket-local ledger",
        )
    active_root = expected_ledger / "active" / "requests"
    try:
        active_workers = [
            entry.name
            for entry in active_root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        ]
    except FileNotFoundError:
        active_workers = []
    except OSError as error:
        raise _migration_guidance(
            socket_path, f"its active-worker ledger cannot be inspected: {error}"
        ) from error
    if active_workers:
        raise _migration_guidance(
            socket_path,
            f"its ledger records {len(active_workers)} active worker(s)",
        )
    if transport.process_start_time(pid) != start_time:
        raise _migration_guidance(
            socket_path, "its PID/start identity changed during verification"
        )
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        raise _migration_guidance(
            socket_path, f"safe SIGTERM failed: {error}"
        ) from error
    identity_path = socket_path.with_name(f"{socket_path.name}.identity.json")
    deadline = time.monotonic() + MIGRATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if transport.process_start_time(pid) != start_time:
            return True
        # A parent-owned zombie retains /proc birth ticks but cannot execute or hold the socket.
        # Canonical Hub shutdown removes both owned artifacts in its finally block.
        if not socket_path.exists() and not identity_path.exists():
            return True
        time.sleep(0.05)
    raise _migration_guidance(socket_path, f"PID {pid} did not exit after safe SIGTERM")


@contextmanager
def _startup_lock(socket_path: Path) -> Iterator[None]:
    lock_path = socket_path.with_name(f"{socket_path.name}.client-start.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _hub_log_path(socket_path: Path) -> Path:
    return socket_path.with_name(f"{socket_path.name}{HUB_LOG_SUFFIX}")


def _open_hub_log(socket_path: Path) -> tuple[Any, Path, int]:
    """Open the Hub's durable process log without following a forged runtime symlink."""

    log_path = _hub_log_path(socket_path)
    flags = os.O_APPEND | os.O_CLOEXEC | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(log_path, flags, 0o600)
    except OSError as error:
        raise ClientError(
            f"could not open canonical UpAgent Hub log {log_path}: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ClientError(
                f"canonical UpAgent Hub log is not a same-user regular file: {log_path}"
            )
        os.fchmod(descriptor, 0o600)
        offset = os.lseek(descriptor, 0, os.SEEK_END)
        return os.fdopen(descriptor, "a", encoding="utf-8"), log_path, offset
    except (ClientError, OSError):
        os.close(descriptor)
        raise


def _startup_diagnostics(log_path: Path, offset: int) -> str:
    try:
        with log_path.open("rb") as stream:
            stream.seek(offset)
            diagnostic = stream.read().decode(errors="replace").strip()
    except OSError as error:
        return f"could not read startup diagnostics from {log_path}: {error}"
    return diagnostic or f"no startup diagnostics were written to {log_path}"


def _stop_failed_start(process: subprocess.Popen[Any]) -> None:
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _start_canonical_hub(socket_path: Path, cwd: Path) -> None:
    hub_path = transport.canonical_module_path("hub.py", cwd)
    engine_path = transport.canonical_module_path("recruiter.py", cwd)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in transport.CALLER_CONTEXT_KEYS
        and key != transport.RAW_OWNER_TOKEN_ENV
    }
    environment[transport.SOCKET_ENV] = str(socket_path)
    hub_log, log_path, log_offset = _open_hub_log(socket_path)
    try:
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(hub_path),
                    "serve",
                    "--socket",
                    str(socket_path),
                    "--engine",
                    str(engine_path),
                ],
                cwd=str(transport.canonical_repo_root(cwd)),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=hub_log,
                stderr=hub_log,
                start_new_session=True,
                text=True,
            )
        except OSError as error:
            raise ClientError(
                f"could not start canonical UpAgent Hub {hub_path}: {error}"
            ) from error
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _probe_protocol_ready(socket_path, cwd) is not None:
                return
            if process.poll() is not None:
                diagnostic = _startup_diagnostics(log_path, log_offset)
                raise ClientError(
                    f"canonical UpAgent Hub exited during startup ({process.returncode}); "
                    f"log {log_path}: {diagnostic}"
                )
            time.sleep(0.05)
        _stop_failed_start(process)
        diagnostic = _startup_diagnostics(log_path, log_offset)
        raise ClientError(
            f"canonical UpAgent Hub did not become ready at {socket_path} within "
            f"{STARTUP_TIMEOUT_SECONDS} seconds; log {log_path}: {diagnostic}"
        )
    finally:
        hub_log.close()


def _ensure_current_hub_for_up(socket_path: Path, cwd: Path) -> None:
    with _startup_lock(socket_path):
        if _probe_protocol_ready(socket_path, cwd) is not None:
            return
        _migrate_incompatible_hub(socket_path, cwd)
        if _probe_protocol_ready(socket_path, cwd) is None:
            _start_canonical_hub(socket_path, cwd)


def _read_requested_stdin(target: str, argv: list[str]) -> str | None:
    if not transport.request_accepts_stdin(target, argv):
        transport.validate_request_stdin(target, argv, None)
        return None
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    value = stream.read(transport.MAX_REQUEST_STDIN_BYTES + 1)
    if isinstance(value, bytes):
        if len(value) > transport.MAX_REQUEST_STDIN_BYTES:
            raise ClientError(
                f"request stdin exceeds {transport.MAX_REQUEST_STDIN_BYTES} bytes"
            )
        try:
            decoded = value.decode()
        except UnicodeDecodeError as error:
            raise ClientError("request stdin must be valid UTF-8") from error
    else:
        decoded = value
    return transport.validate_request_stdin(target, argv, decoded)


def invoke(target: str, argv: list[str], cwd: Path) -> int:
    socket_path = transport.socket_path(cwd)
    if target in ("public", "recruiter") and argv and argv[0] == "up":
        _ensure_current_hub_for_up(socket_path, cwd)
    request_stdin = _read_requested_stdin(target, argv)
    response = _round_trip(
        socket_path, target, argv, cwd=cwd, request_stdin=request_stdin
    )
    sys.stdout.write(response["stdout"])
    sys.stdout.flush()
    sys.stderr.write(response["stderr"])
    sys.stderr.flush()
    return int(response["exit_code"])


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    target = "public"
    if command[:1] == ["--target"]:
        if len(command) < 2:
            raise ClientError("--target requires a value")
        target = command[1]
        command = command[2:]
    allowed = {
        "public",
        "recruiter",
        "phase-controller",
        "phase-await",
        "direct-controller",
        "plan-controller",
        "run-lifecycle",
        "hub",
    }
    if target not in allowed:
        raise ClientError(f"unknown Hub target: {target}")
    if target == "hub" and not command:
        command = ["status"]
    if target == "public":
        if not command or command in (["--help"], ["help"]):
            sys.stdout.write(public_contract.help_text())
            return 0
        try:
            public_contract.parse_argv(command)
        except public_contract.PublicCommandError as error:
            raise ClientError(str(error)) from error
    if target == "recruiter" and command == ["status"]:
        target = "hub"
    if not command:
        raise ClientError("a Hub command is required")
    return invoke(target, command, Path.cwd().resolve())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ClientError,
        transport.ProtocolError,
        public_contract.PublicCommandError,
    ) as error:
        raise SystemExit(f"upagent-client: {error}") from error
