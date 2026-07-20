#!/usr/bin/env python3
"""Guarded non-default Herdr lab sessions for smoke tests.

This helper is intentionally stricter than production startup. It creates a generated
non-default session, records a before snapshot of `herdr session list --json`, and refuses
teardown unless it owns the named session and the after snapshot matches exactly once that
session is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

LAB_PREFIX = "llm-lab-"
LAB_NAME_RE = re.compile(r"\Allm-lab-[A-Za-z0-9][A-Za-z0-9_-]*\Z")
STATE_DIR = Path(
    os.environ.get("LLM_HERDR_LAB_STATE_DIR", f"/tmp/llm-herdr-lab-{os.getuid()}")
)


class LabError(RuntimeError):
    """A fail-closed lab safety violation."""


def _validate_lab_session(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise LabError("refusing an empty session name")
    if name == "default":
        raise LabError("refusing session name 'default'")
    if LAB_NAME_RE.fullmatch(name) is None:
        raise LabError(
            f"session name must be generated with prefix {LAB_PREFIX!r}: {name!r}"
        )
    return name


def _run_herdr(
    args: list[str], *, session: str | None = None, timeout: float = 15
) -> subprocess.CompletedProcess[str]:
    argv = ["herdr"]
    if session is not None:
        argv.extend(("--session", _validate_lab_session(session)))
    argv.extend(args)
    try:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LabError(f"{' '.join(argv)} could not run: {error}") from error
    if process.returncode != 0:
        raise LabError(f"{' '.join(argv)} failed: {process.stderr.strip()}")
    return process


def _session_list() -> dict:
    process = _run_herdr(["session", "list", "--json"])
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise LabError(
            f"herdr session list --json returned invalid JSON: {error}"
        ) from error
    if not isinstance(value, dict) or not isinstance(value.get("sessions"), list):
        raise LabError("herdr session list --json returned no sessions list")
    return value


def _fleet_snapshot(value: dict) -> list[dict[str, object]]:
    snapshot = []
    for session in value["sessions"]:
        if not isinstance(session, dict):
            raise LabError("herdr session list contains a non-object session")
        name = session.get("name")
        if not isinstance(name, str) or not name:
            raise LabError("herdr session list contains a session with no name")
        snapshot.append(
            {
                "default": session.get("default") is True,
                "name": name,
                "running": session.get("running") is True,
                "socket_path": session.get("socket_path"),
            }
        )
    return sorted(snapshot, key=lambda item: str(item["name"]))


def _find_session(value: dict, name: str) -> dict | None:
    matches = [
        item
        for item in value["sessions"]
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) > 1:
        raise LabError(f"session {name!r} is ambiguous in herdr session list")
    return matches[0] if matches else None


def _snapshot_without_session(
    snapshot: list[dict[str, object]], name: str
) -> list[dict[str, object]]:
    return [item for item in snapshot if item.get("name") != name]


def _ensure_state_dir() -> None:
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = STATE_DIR.lstat()
    except OSError as error:
        raise LabError(
            f"lab state directory {STATE_DIR} is unavailable: {error}"
        ) from error
    if not stat.S_ISDIR(info.st_mode) or STATE_DIR.is_symlink():
        raise LabError(f"lab state directory {STATE_DIR} must be a real directory")
    if info.st_uid != os.getuid():
        raise LabError(f"lab state directory {STATE_DIR} is not owned by this user")
    try:
        STATE_DIR.chmod(0o700)
    except OSError as error:
        raise LabError(
            f"lab state directory {STATE_DIR} cannot be secured: {error}"
        ) from error


def _ownership_path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def _read_ownership(name: str) -> dict:
    _ensure_state_dir()
    path = _ownership_path(name)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise LabError(f"missing lab ownership record for {name!r}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise LabError(
            f"lab ownership record for {name!r} is invalid: {error}"
        ) from error
    if not isinstance(value, dict) or value.get("session") != name:
        raise LabError(f"lab ownership record for {name!r} has the wrong identity")
    if value.get("created") is not True:
        raise LabError(
            f"lab ownership record for {name!r} does not own a created session"
        )
    if not isinstance(value.get("before"), list):
        raise LabError(f"lab ownership record for {name!r} has no before snapshot")
    return value


def _write_ownership(name: str, value: dict[str, object]) -> None:
    _ensure_state_dir()
    path = _ownership_path(name)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temp, path)


def generated_name(label: str) -> str:
    compact = "".join(ch for ch in label if ch.isalnum() or ch in "-_").strip("-_")
    if not compact:
        compact = "smoke"
    compact = compact[:16].strip("-_") or "smoke"
    return f"{LAB_PREFIX}{compact}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def prepare(name: str) -> dict[str, object]:
    name = _validate_lab_session(name)
    _ensure_state_dir()
    sessions = _session_list()
    if _find_session(sessions, name) is not None:
        raise LabError(f"session {name!r} already exists; refusing to adopt it")
    if _ownership_path(name).exists():
        raise LabError(f"ownership record already exists for {name!r}")
    owner = {
        "before": _fleet_snapshot(sessions),
        "created": False,
        "resources": {"session": {"name": name, "state": "planned"}},
        "session": name,
    }
    _write_ownership(name, owner)
    return owner


def provision(name: str) -> dict[str, object]:
    name = _validate_lab_session(name)
    _ensure_state_dir()
    owner_path = _ownership_path(name)
    if owner_path.exists():
        try:
            owner = json.loads(owner_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise LabError(
                f"lab ownership record for {name!r} is invalid: {error}"
            ) from error
        if not isinstance(owner, dict) or owner.get("session") != name:
            raise LabError(f"lab ownership record for {name!r} has the wrong identity")
        if not isinstance(owner.get("before"), list):
            raise LabError(f"lab ownership record for {name!r} has no before snapshot")
    else:
        owner = prepare(name)
    if owner.get("created") is True:
        raise LabError(f"session {name!r} is already owned by this lab helper")
    if _find_session(_session_list(), name) is not None:
        raise LabError(
            f"session {name!r} appeared before provision; refusing to adopt it"
        )
    process = subprocess.Popen(
        ["herdr", "--session", name, "server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            status = _run_herdr(["status", "--json"], session=name).stdout
            running = json.loads(status).get("server", {}).get("running") is True
        except (LabError, json.JSONDecodeError, AttributeError):
            running = False
        if running:
            sessions = _session_list()
            session = _find_session(sessions, name)
            if not isinstance(session, dict) or session.get("default") is True:
                raise LabError(f"created lab session {name!r} is absent or default")
            owner.update(
                {
                    "created": True,
                    "resources": {"session": {"name": name, "state": "created"}},
                    "server_pid": process.pid,
                }
            )
            _write_ownership(name, owner)
            return owner
        time.sleep(0.2)
    process.terminate()
    raise LabError(f"lab session {name!r} did not report running within 60s")


def run(name: str, args: list[str]) -> int:
    name = _validate_lab_session(name)
    _read_ownership(name)
    _refuse_default(name)
    if not args:
        raise LabError("run requires Herdr arguments")
    if args[0].startswith("-"):
        raise LabError("run forbids leading Herdr options before the subcommand")
    if any(arg == "--session" or arg.startswith("--session=") for arg in args):
        raise LabError("run forbids caller-supplied --session")
    if args[0] == "server":
        raise LabError("run forbids server operations")
    if args[0] == "session" and (len(args) == 1 or args[1] != "list"):
        raise LabError("run forbids session lifecycle operations")
    process = _run_herdr(args, session=name, timeout=60)
    sys.stdout.write(process.stdout)
    sys.stderr.write(process.stderr)
    return process.returncode


def _refuse_default(name: str) -> dict:
    sessions = _session_list()
    session = _find_session(sessions, name)
    if not isinstance(session, dict) or session.get("default") is True:
        raise LabError(f"refusing destructive call for absent/default session {name!r}")
    return sessions


def teardown(name: str) -> dict[str, object]:
    name = _validate_lab_session(name)
    owner = _read_ownership(name)
    sessions = _session_list()
    before_destructive = _snapshot_without_session(_fleet_snapshot(sessions), name)
    if owner["before"] != before_destructive:
        raise LabError("FLEET-STATE TRIPWIRE FAILED: non-lab Herdr sessions changed")
    session = _find_session(sessions, name)
    if isinstance(session, dict):
        if session.get("default") is True:
            raise LabError(f"refusing destructive call for default session {name!r}")
        _run_herdr(["session", "stop", name, "--json"], session=name)
        time.sleep(0.2)
        _refuse_default(name)
        _run_herdr(["session", "delete", name, "--json"], session=name)
    after_sessions = _session_list()
    if _find_session(after_sessions, name) is not None:
        raise LabError(f"lab session {name!r} still exists after teardown")
    after = _fleet_snapshot(after_sessions)
    if owner["before"] != after:
        raise LabError("FLEET-STATE TRIPWIRE FAILED: non-lab Herdr sessions changed")
    _ownership_path(name).unlink()
    return {"deleted": True, "session": name}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-lab-session")
    sub = parser.add_subparsers(dest="command", required=True)
    p_name = sub.add_parser("name")
    p_name.add_argument("label")
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("session")
    p_provision = sub.add_parser("provision")
    p_provision.add_argument("session")
    p_run = sub.add_parser("run")
    p_run.add_argument("session")
    p_run.add_argument("args", nargs=argparse.REMAINDER)
    p_teardown = sub.add_parser("teardown")
    p_teardown.add_argument("session")
    args = parser.parse_args(argv)
    try:
        if args.command == "name":
            print(generated_name(args.label))
        elif args.command == "prepare":
            print(json.dumps(prepare(args.session), sort_keys=True))
        elif args.command == "provision":
            print(json.dumps(provision(args.session), sort_keys=True))
        elif args.command == "run":
            return run(args.session, args.args)
        elif args.command == "teardown":
            print(json.dumps(teardown(args.session), sort_keys=True))
    except (LabError, OSError, subprocess.SubprocessError) as error:
        sys.exit(f"herdr-lab-session: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
