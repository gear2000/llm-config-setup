"""Herdr transport primitives with no Recruiter or ledger dependency."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

UNIFIED_WORKSPACE_LABEL = "herdr"
PHASE_START_RECEIPT_ENV = "UPAGENT_PHASE_START_RECEIPT"
HERDR_SOCKET_ENV = "HERDR_SOCKET_PATH"
HERDR_SESSION_ENV = "HERDR_SESSION"
SESSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
HEALTH_PROBE_SECONDS = 0.1
STARTUP_FAILURE_SETTLE_SECONDS = 2.0
LAYOUT_COMMAND_TIMEOUT_SECONDS = 2.0
LAYOUT_LOCK_TIMEOUT_SECONDS = 10.0
TAB_ROLES = frozenset(("workers", "oversight", "services", "control"))
# Transitional test seam only; this transport never reads Recruiter state or a job ledger.
STATE_FILE = Path(os.environ.get("UPAGENT_STATE", "/tmp/.upagent/recruiter.json"))


class HerdrTransportError(RuntimeError):
    """A canonical Herdr transport or identity failure."""


def _getenv(name: str, default: str | None = None) -> str | None:
    runtime = sys.modules.get("upagent_command_runtime")
    return (
        runtime.getenv(name, default)
        if runtime is not None
        else os.environ.get(name, default)
    )


def default_roster_path() -> str:
    configured = _getenv("UPAGENT_CONFIG")
    if configured:
        return configured
    engine = _getenv("UPAGENT_HUB_ENGINE_PATH")
    if engine:
        return str(Path(engine).resolve().with_name("upagent.yaml"))
    return str(Path(__file__).resolve().parent.parent / "upagent" / "upagent.yaml")


def _validate_session(value: object, field: str = "Herdr session") -> str:
    if not isinstance(value, str) or not SESSION_RE.fullmatch(value):
        raise HerdrTransportError(f"{field} has an invalid value: {value!r}")
    return value


def _raw(
    args: Sequence[str], *, timeout_seconds: float | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["herdr", *args], capture_output=True, text=True, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as error:
        raise HerdrTransportError(
            f"herdr {' '.join(args)} timed out after {timeout_seconds} seconds"
        ) from error
    except OSError as error:
        raise HerdrTransportError(
            f"herdr {' '.join(args)} could not run: {error}"
        ) from error


def _json_process(process: subprocess.CompletedProcess[str], display: str) -> dict:
    if process.returncode != 0:
        raise HerdrTransportError(f"{display} failed: {process.stderr.strip()}")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise HerdrTransportError(
            f"{display} did not print JSON: {process.stdout[:200]}"
        ) from error
    if not isinstance(value, dict):
        raise HerdrTransportError(f"{display} must return a JSON object")
    return value


def resolve_current_herdr_session_name() -> str:
    hint_value = _getenv(HERDR_SESSION_ENV)
    hint = _validate_session(hint_value, HERDR_SESSION_ENV) if hint_value else None
    socket_path = _getenv(HERDR_SOCKET_ENV)
    if not socket_path:
        args = [*(["--session", hint] if hint else []), "status", "--json"]
        status = _json_process(
            _raw(args, timeout_seconds=15), f"herdr {' '.join(args)}"
        )
        server = status.get("server")
        if not isinstance(server, dict) or server.get("running") is not True:
            raise HerdrTransportError(
                "could not resolve Herdr session: server is not running"
            )
        socket_path = server.get("socket") or server.get("socket_path")
    if not isinstance(socket_path, str) or not socket_path:
        raise HerdrTransportError("could not resolve Herdr session socket")
    listing = _json_process(
        _raw(("session", "list", "--json"), timeout_seconds=15),
        "herdr session list --json",
    )
    sessions = listing.get("sessions")
    if not isinstance(sessions, list):
        raise HerdrTransportError("herdr session list returned no sessions list")
    matches = [
        item
        for item in sessions
        if isinstance(item, dict)
        and item.get("running") is True
        and item.get("socket_path") == socket_path
    ]
    if len(matches) != 1:
        raise HerdrTransportError(
            f"expected one running Herdr session for {socket_path!r}, found {len(matches)}"
        )
    name = _validate_session(matches[0].get("name"))
    if hint is not None and name != hint:
        raise HerdrTransportError(
            f"resolved Herdr socket belongs to {name!r}, not {hint!r}"
        )
    return name


def herdr_json(
    *args: str,
    timeout_seconds: float | None = None,
    herdr_session: str | None = None,
) -> dict:
    session = (
        resolve_current_herdr_session_name()
        if herdr_session is None
        else _validate_session(herdr_session)
    )
    argv = ["--session", session, *args]
    return _json_process(
        _raw(argv, timeout_seconds=timeout_seconds), f"herdr {' '.join(argv)}"
    )


def herdr(*args: str, herdr_session: str | None = None) -> None:
    session = (
        resolve_current_herdr_session_name()
        if herdr_session is None
        else _validate_session(herdr_session)
    )
    argv = ["--session", session, *args]
    process = _raw(argv)
    if process.returncode != 0:
        raise HerdrTransportError(
            f"herdr {' '.join(argv)} failed: {process.stderr.strip()}"
        )


def process_start_time(pid: object) -> str | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None


def _pane_recent_output(pane: str, herdr_session: str) -> str:
    session = _validate_session(herdr_session)
    process = _raw(
        (
            "--session",
            session,
            "pane",
            "read",
            pane,
            "--source",
            "recent-unwrapped",
            "--lines",
            "80",
        )
    )
    if process.returncode != 0:
        raise HerdrTransportError(
            f"could not read pane {pane}: {process.stderr.strip()}"
        )
    return process.stdout


def wait_for_agent_health(
    pane_id: str,
    *,
    expected_agent: str,
    expected_process: str,
    expected_cwd: str,
    timeout_ms: int,
    herdr_session: str,
) -> dict[str, object]:
    resolved_cwd = os.path.realpath(expected_cwd)
    deadline = time.monotonic() + timeout_ms / 1000
    started = time.monotonic()
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        pane = (
            herdr_json("pane", "get", pane_id, herdr_session=herdr_session)
            .get("result", {})
            .get("pane", {})
        )
        info = (
            herdr_json(
                "pane", "process-info", "--pane", pane_id, herdr_session=herdr_session
            )
            .get("result", {})
            .get("process_info", {})
        )
        processes = (
            info.get("foreground_processes", []) if isinstance(info, dict) else []
        )
        match = next(
            (
                item
                for item in processes
                if isinstance(item, dict)
                and (
                    item.get("name") == expected_process
                    or expected_process in str(item.get("cmdline", ""))
                    or expected_process
                    in " ".join(str(value) for value in item.get("argv", []))
                )
            ),
            None,
        )
        cwd = (
            pane.get("foreground_cwd", pane.get("cwd"))
            if isinstance(pane, dict)
            else None
        )
        latest = {
            "agent_status": pane.get("agent_status")
            if isinstance(pane, dict)
            else None,
            "cwd": cwd,
            "cwd_matches": isinstance(cwd, str)
            and os.path.realpath(cwd) == resolved_cwd,
            "detected_agent": pane.get("agent") if isinstance(pane, dict) else None,
            "expected_agent": expected_agent,
            "expected_process": expected_process,
            "healthy": False,
            "pane_id": pane_id,
            "process_pid": match.get("pid") if isinstance(match, dict) else None,
        }
        if (
            match is not None
            and latest["detected_agent"] == expected_agent
            and latest["agent_status"] in ("working", "idle", "done")
        ):
            if latest["cwd_matches"] is not True:
                raise HerdrTransportError(
                    f"agent {pane_id} started in {cwd!r}, expected {expected_cwd!r}"
                )
            latest["healthy"] = True
            return latest
        if (
            match is None
            and time.monotonic() - started >= STARTUP_FAILURE_SETTLE_SECONDS
        ):
            output = _pane_recent_output(pane_id, herdr_session)
            raise HerdrTransportError(
                f"agent pane {pane_id} did not start {expected_process}; recent output: {output[-1000:]}"
            )
        time.sleep(HEALTH_PROBE_SECONDS)
    raise HerdrTransportError(
        f"agent pane {pane_id} did not become healthy within {timeout_ms} ms; evidence={json.dumps(latest)}"
    )


@contextmanager
def _layout_lock(workspace_id: str) -> Iterator[None]:
    key = hashlib.sha256(workspace_id.encode()).hexdigest()
    path = Path("/tmp/.upagent/tui-controller-layout") / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        deadline = time.monotonic() + LAYOUT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise HerdrTransportError(
                        f"layout lock for workspace {workspace_id} timed out"
                    ) from error
                time.sleep(HEALTH_PROBE_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def place_started_agent_in_role_tab(
    pane_id: str,
    workspace_id: str,
    tab_role: str,
    *,
    split_direction: str,
    herdr_session: str,
) -> str:
    if tab_role not in TAB_ROLES:
        raise HerdrTransportError(f"unknown controller tab role {tab_role!r}")
    with _layout_lock(workspace_id):
        tabs = (
            herdr_json(
                "tab", "list", "--workspace", workspace_id, herdr_session=herdr_session
            )
            .get("result", {})
            .get("tabs", [])
        )
        matches = [
            item
            for item in tabs
            if isinstance(item, dict) and item.get("label") == tab_role
        ]
        if len(matches) > 1:
            raise HerdrTransportError(f"workspace has multiple {tab_role!r} tabs")
        if matches:
            tab_id = matches[0].get("tab_id")
            panes = (
                herdr_json(
                    "pane",
                    "list",
                    "--workspace",
                    workspace_id,
                    herdr_session=herdr_session,
                )
                .get("result", {})
                .get("panes", [])
            )
            current = next(
                (
                    item
                    for item in panes
                    if isinstance(item, dict) and item.get("pane_id") == pane_id
                ),
                None,
            )
            if isinstance(current, dict) and current.get("tab_id") == tab_id:
                return pane_id
            target = next(
                (
                    item.get("pane_id")
                    for item in panes
                    if isinstance(item, dict)
                    and item.get("tab_id") == tab_id
                    and isinstance(item.get("pane_id"), str)
                ),
                None,
            )
            if not isinstance(tab_id, str) or not isinstance(target, str):
                raise HerdrTransportError(f"{tab_role!r} tab has no target pane")
            response = herdr_json(
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
                herdr_session=herdr_session,
            )
        else:
            response = herdr_json(
                "pane",
                "move",
                pane_id,
                "--new-tab",
                "--workspace",
                workspace_id,
                "--label",
                tab_role,
                "--no-focus",
                herdr_session=herdr_session,
            )
        moved = response.get("result", {}).get("move_result", {})
        pane = moved.get("pane", {}) if isinstance(moved, dict) else {}
        moved_id = pane.get("pane_id") if isinstance(pane, dict) else None
        if moved.get("changed") is not True or not isinstance(moved_id, str):
            raise HerdrTransportError(
                f"Herdr did not place pane {pane_id} in {tab_role!r} tab"
            )
        return moved_id


# Transitional private-name aliases for controller tests and one-release callers.
_resolve_current_herdr_session_name = resolve_current_herdr_session_name
_herdr_json = herdr_json
_herdr = herdr
_process_start_time = process_start_time
_wait_for_agent_health = wait_for_agent_health
_place_started_agent_in_role_tab = place_started_agent_in_role_tab
_submit_agent_prompt = None
