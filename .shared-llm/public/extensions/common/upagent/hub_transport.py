#!/usr/bin/env python3
"""Shared wire/discovery primitives for the machine-local UpAgent Hub.

This module deliberately knows nothing about the Recruiter, its ledger, Herdr dispatch, or
worker processes.  Both the thin client and the canonical Hub use it to agree on one socket and
one small versioned JSON-lines protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 5
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_REQUEST_STDIN_BYTES = 64 * 1024
SOCKET_ENV = "UPAGENT_SOCKET"
OWNER_TOKEN_FILE_ENV = "RUNNER_OWNER_TOKEN_FILE"
RAW_OWNER_TOKEN_ENV = "RUNNER_OWNER_TOKEN"
CALLER_CONTEXT_KEYS = frozenset(
    (
        "HERDR_ENV",
        "HERDR_PANE_ID",
        "HERDR_SESSION",
        "HERDR_SOCKET_PATH",
        OWNER_TOKEN_FILE_ENV,
    )
)
_SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_SAFE_SESSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_PROTOCOL_SCHEMA = {
    "hello": ("protocol_fingerprint", "protocol_version", "type"),
    "hello_response": (
        "identity",
        "protocol_fingerprint",
        "protocol_version",
        "type",
    ),
    "request": (
        "argv",
        "caller_context",
        "cwd",
        "protocol_fingerprint",
        "protocol_version",
        "stdin",
        "target",
        "type",
    ),
    "response": ("exit_code", "identity", "stderr", "stdout", "type"),
    "request_stdin": {
        "target": "run-lifecycle",
        "operations": ("cleanup", "guard"),
        "flag": "--token-stdin",
        "max_bytes": MAX_REQUEST_STDIN_BYTES,
    },
}


_CACHED_HERDR_RUNTIME = (
    "controller_transport.py",
    "plan_controller.py",
    "run_lifecycle.py",
)


def _runtime_source_fingerprint(upagent_dir: Path | None = None) -> str:
    """Fingerprint every Python source cached by one resident Hub process."""
    directory = (upagent_dir or Path(__file__).resolve().parent).resolve()
    herdr_dir = directory.parent / "herdr"
    sources = sorted(
        path for path in directory.glob("*.py") if not path.name.endswith("_test.py")
    ) + [herdr_dir / name for name in _CACHED_HERDR_RUNTIME]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise RuntimeError(
            "UpAgent runtime fingerprint sources are missing: " + ", ".join(missing)
        )
    digest = hashlib.sha256()
    for path in sources:
        digest.update(str(path.relative_to(directory.parent)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _protocol_fingerprint(upagent_dir: Path) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "runtime_source_fingerprint": _runtime_source_fingerprint(upagent_dir),
                "schema": _PROTOCOL_SCHEMA,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


PROTOCOL_FINGERPRINT = _protocol_fingerprint(Path(__file__).resolve().parent)


class ProtocolError(RuntimeError):
    """A malformed or incompatible Hub protocol frame."""


def validate_caller_context(value: object) -> dict[str, str]:
    """Validate the complete, deliberately tiny caller environment whitelist."""

    if not isinstance(value, dict):
        raise ProtocolError("request caller_context must be an object")
    unknown = sorted(set(value) - CALLER_CONTEXT_KEYS)
    if unknown:
        raise ProtocolError(
            f"request caller_context has unknown keys {', '.join(unknown)}"
        )
    if not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ProtocolError("request caller_context keys and values must be strings")
    context = dict(value)
    herdr_env = context.get("HERDR_ENV")
    if herdr_env is not None and herdr_env != "1":
        raise ProtocolError("request caller_context HERDR_ENV must be '1'")
    pane_id = context.get("HERDR_PANE_ID")
    if pane_id is not None and _SAFE_ID_RE.fullmatch(pane_id) is None:
        raise ProtocolError("request caller_context HERDR_PANE_ID is invalid")
    session = context.get("HERDR_SESSION")
    if session is not None and _SAFE_SESSION_RE.fullmatch(session) is None:
        raise ProtocolError("request caller_context HERDR_SESSION is invalid")
    socket_value = context.get("HERDR_SOCKET_PATH")
    if socket_value is not None and (
        not socket_value
        or len(socket_value) > 4096
        or "\x00" in socket_value
        or "\n" in socket_value
        or not Path(socket_value).is_absolute()
    ):
        raise ProtocolError(
            "request caller_context HERDR_SOCKET_PATH must be a valid absolute path"
        )
    token_file_value = context.get(OWNER_TOKEN_FILE_ENV)
    if token_file_value is not None:
        token_file = Path(token_file_value)
        if (
            not token_file_value
            or len(token_file_value) > 4096
            or "\x00" in token_file_value
            or "\n" in token_file_value
            or not token_file.is_absolute()
        ):
            raise ProtocolError(
                f"request caller_context {OWNER_TOKEN_FILE_ENV} must be a valid absolute path"
            )
        try:
            metadata = token_file.lstat()
        except OSError as error:
            raise ProtocolError(
                f"request caller_context {OWNER_TOKEN_FILE_ENV} is not readable: {token_file}: {error}"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or not os.access(token_file, os.R_OK)
        ):
            raise ProtocolError(
                f"request caller_context {OWNER_TOKEN_FILE_ENV} must be a readable same-user private regular file"
            )
        context[OWNER_TOKEN_FILE_ENV] = str(token_file.resolve())
    return context


def caller_context(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy only approved Herdr identity fields from a client environment."""

    source = os.environ if environ is None else environ
    return validate_caller_context(
        {key: source[key] for key in CALLER_CONTEXT_KEYS if key in source}
    )


def _lifecycle_operation(argv: list[str]) -> str | None:
    """Return the run-lifecycle subcommand after its one global option, if well formed."""

    index = 0
    if argv[:1] == ["--repo"]:
        if len(argv) < 3:
            return None
        index = 2
    return argv[index] if index < len(argv) else None


def request_accepts_stdin(target: str, argv: list[str]) -> bool:
    """True only for the two lifecycle operations that explicitly request token stdin."""

    return (
        target == "run-lifecycle"
        and _lifecycle_operation(argv) in ("guard", "cleanup")
        and argv.count("--token-stdin") == 1
    )


def validate_request_stdin(target: object, argv: object, value: object) -> str | None:
    """Validate request-local stdin without ever placing it in identity or environment."""

    if not isinstance(target, str):
        raise ProtocolError("request target must be a string")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ProtocolError("request argv must be a list of strings")
    requested = "--token-stdin" in argv
    allowed = request_accepts_stdin(target, argv)
    if requested and not allowed:
        raise ProtocolError(
            "--token-stdin is allowed only once on run-lifecycle guard or cleanup"
        )
    if value is None:
        if allowed:
            raise ProtocolError("--token-stdin requires request-local stdin")
        return None
    if not allowed:
        raise ProtocolError("request stdin is forbidden for this target and operation")
    if not isinstance(value, str):
        raise ProtocolError("request stdin must be a string or null")
    if len(value.encode()) > MAX_REQUEST_STDIN_BYTES:
        raise ProtocolError(f"request stdin exceeds {MAX_REQUEST_STDIN_BYTES} bytes")
    return value


def process_start_time(pid: object) -> str | None:
    """Return Linux process birth ticks so PID reuse cannot authorize migration."""

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
    return fields[19] if len(fields) > 19 else None


def git_common_dir(cwd: str | Path | None = None) -> Path:
    """Return the absolute common git directory shared by a checkout and all its worktrees."""
    directory = Path(cwd or Path.cwd()).resolve()
    try:
        process = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProtocolError(
            f"cannot discover the repository common directory from {directory}: {error}"
        ) from error
    value = process.stdout.strip()
    if not value:
        raise ProtocolError(f"git returned an empty common directory for {directory}")
    return Path(value).resolve()


def canonical_repo_root(cwd: str | Path | None = None) -> Path:
    """The main checkout root, even when called from a linked git worktree."""
    common = git_common_dir(cwd)
    if common.name != ".git":
        raise ProtocolError(
            f"unsupported git common directory (expected .git): {common}"
        )
    return common.parent


def canonical_protocol_fingerprint(cwd: str | Path | None = None) -> str:
    """Fingerprint the main checkout runtime even when the caller is in a linked worktree."""
    upagent_dir = (
        canonical_repo_root(cwd) / ".shared-llm/public/extensions/common/upagent"
    )
    return _protocol_fingerprint(upagent_dir)


def repository_id(cwd: str | Path | None = None) -> str:
    common = git_common_dir(cwd)
    return hashlib.sha256(str(common).encode()).hexdigest()[:20]


def socket_path(cwd: str | Path | None = None) -> Path:
    override = os.environ.get(SOCKET_ENV)
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ProtocolError(f"{SOCKET_ENV} must be an absolute path: {path}")
        return path
    return Path("/tmp/.upagent/hubs") / repository_id(cwd) / "hub.sock"


def canonical_module_path(filename: str, cwd: str | Path | None = None) -> Path:
    path = (
        canonical_repo_root(cwd)
        / ".shared-llm/public/extensions/common/upagent"
        / filename
    )
    if not path.is_file():
        raise ProtocolError(f"canonical UpAgent module does not exist: {path}")
    return path.resolve()


def write_frame(stream: Any, payload: dict[str, object]) -> None:
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError(f"protocol frame exceeds {MAX_FRAME_BYTES} bytes")
    stream.write(raw)
    stream.flush()


def read_frame(stream: Any) -> dict[str, Any]:
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    if not raw:
        raise ProtocolError("Hub connection closed before a complete frame arrived")
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ProtocolError(
            f"protocol frame is unterminated or exceeds {MAX_FRAME_BYTES} bytes"
        )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"protocol frame is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ProtocolError("protocol frame must be one JSON object")
    return value


def connect(path: Path, timeout_seconds: float = 5.0) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout_seconds)
    try:
        connection.connect(str(path))
    except OSError:
        connection.close()
        raise
    return connection
