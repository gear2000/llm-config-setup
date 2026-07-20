"""Hermetic tests for the guarded Herdr lab/session helper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "herdr_lab_session_tested", Path(__file__).with_name("lab_session.py")
)
assert _spec and _spec.loader
lab_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab_session)
LabError = lab_session.LabError


def _fake_herdr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    state = tmp_path / "state.json"
    log = tmp_path / "herdr.log"
    state.write_text(
        json.dumps(
            {
                "default_socket": "/tmp/default.sock",
                "sessions": {},
            }
        )
    )
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    script = fakebin / "herdr"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_HERDR_STATE"])
log_path = Path(os.environ["FAKE_HERDR_LOG"])
log_path.write_text(log_path.read_text() + "\\x1f".join(sys.argv[1:]) + "\\n")
state = json.loads(state_path.read_text())
args = sys.argv[1:]
session = None
if len(args) >= 2 and args[0] == "--session":
    session = args[1]
    args = args[2:]

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

def sessions_payload():
    sessions = [
        {
            "default": True,
            "name": "default",
            "running": True,
            "socket_path": state["default_socket"],
        }
    ]
    for name, item in sorted(state["sessions"].items()):
        if item.get("deleted"):
            continue
        sessions.append(
            {
                "default": item.get("default", False),
                "name": name,
                "running": item.get("running", False),
                "socket_path": f"/tmp/{name}.sock",
            }
        )
    print(json.dumps({"sessions": sessions}))

if args == ["session", "list", "--json"]:
    sessions_payload()
elif args == ["server"] and session:
    state["sessions"][session] = {"running": True, "default": False}
    save()
elif args == ["status", "--json"] and session:
    running = state["sessions"].get(session, {}).get("running", False)
    print(json.dumps({"server": {"running": running, "socket": f"/tmp/{session}.sock"}}))
elif len(args) >= 4 and args[:2] == ["session", "stop"] and session:
    if args[2] != session:
        print("wrong stop target", file=sys.stderr)
        sys.exit(90)
    state["sessions"].setdefault(session, {})["running"] = False
    save()
    print(json.dumps({"stopped": session}))
elif len(args) >= 4 and args[:2] == ["session", "delete"] and session:
    if args[2] != session:
        print("wrong delete target", file=sys.stderr)
        sys.exit(91)
    state["sessions"].setdefault(session, {})["deleted"] = True
    save()
    print(json.dumps({"deleted": session}))
else:
    print(json.dumps({"ok": True}))
"""
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}:{Path('/usr/bin')}")
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.setattr(lab_session, "STATE_DIR", tmp_path / "ownership")
    log.write_text("")
    return state, log


def test_lab_helper_refuses_unsafe_or_ambiguous_names() -> None:
    with pytest.raises(LabError, match="empty"):
        lab_session._validate_lab_session("")
    with pytest.raises(LabError, match="default"):
        lab_session._validate_lab_session("default")
    with pytest.raises(LabError, match="prefix"):
        lab_session._validate_lab_session("manual")

    name = lab_session.generated_name("smoke")
    assert name.startswith("llm-lab-smoke-")
    assert lab_session._validate_lab_session(name) == name


def test_provision_run_and_teardown_are_scoped_to_created_lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, log = _fake_herdr(tmp_path, monkeypatch)
    name = "llm-lab-behavior-1"

    owner = lab_session.provision(name)
    assert owner["created"] is True
    assert (tmp_path / "ownership" / f"{name}.json").is_file()

    assert lab_session.run(name, ["workspace", "list"]) == 0
    with pytest.raises(LabError, match="server operations"):
        lab_session.run(name, ["server", "stop"])
    with pytest.raises(LabError, match="session lifecycle"):
        lab_session.run(name, ["session", "delete", name])
    with pytest.raises(LabError, match="caller-supplied"):
        lab_session.run(name, ["workspace", "list", "--session", "default"])

    result = lab_session.teardown(name)
    assert result == {"deleted": True, "session": name}
    assert not (tmp_path / "ownership" / f"{name}.json").exists()

    lines = log.read_text().splitlines()
    assert f"--session\x1f{name}\x1fsession\x1fstop\x1f{name}\x1f--json" in lines
    assert f"--session\x1f{name}\x1fsession\x1fdelete\x1f{name}\x1f--json" in lines
    assert all("server\x1fstop" not in line for line in lines)


def test_provision_refuses_session_that_appeared_after_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, log = _fake_herdr(tmp_path, monkeypatch)
    name = "llm-lab-race-1"
    lab_session.prepare(name)
    data = json.loads(state.read_text())
    data["sessions"][name] = {"running": True, "default": False}
    state.write_text(json.dumps(data))

    with pytest.raises(LabError, match="appeared before provision"):
        lab_session.provision(name)

    assert all(
        f"--session\x1f{name}\x1fserver" not in line
        for line in log.read_text().splitlines()
    )


def test_lab_state_directory_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_herdr(tmp_path, monkeypatch)
    state_dir = tmp_path / "ownership"
    state_dir.mkdir(mode=0o777)
    state_dir.chmod(0o777)

    lab_session.prepare("llm-lab-private-1")

    assert state_dir.stat().st_mode & 0o777 == 0o700


def test_run_refuses_unowned_generated_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _log = _fake_herdr(tmp_path, monkeypatch)
    name = "llm-lab-unowned-1"
    data = json.loads(state.read_text())
    data["sessions"][name] = {"running": True, "default": False}
    state.write_text(json.dumps(data))

    with pytest.raises(LabError, match="missing lab ownership"):
        lab_session.run(name, ["workspace", "list"])


def test_tripwire_change_blocks_teardown_and_keeps_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, log = _fake_herdr(tmp_path, monkeypatch)
    name = "llm-lab-tripwire-1"
    lab_session.provision(name)
    data = json.loads(state.read_text())
    data["default_socket"] = "/tmp/changed-default.sock"
    state.write_text(json.dumps(data))

    with pytest.raises(LabError, match="TRIPWIRE"):
        lab_session.teardown(name)

    assert (tmp_path / "ownership" / f"{name}.json").is_file()
    lines = log.read_text().splitlines()
    assert all(f"session\x1fstop\x1f{name}" not in line for line in lines)
    assert all(f"session\x1fdelete\x1f{name}" not in line for line in lines)


def test_prepare_refuses_to_adopt_existing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _log = _fake_herdr(tmp_path, monkeypatch)
    name = "llm-lab-existing-1"
    data = json.loads(state.read_text())
    data["sessions"][name] = {"running": True, "default": False}
    state.write_text(json.dumps(data))

    with pytest.raises(LabError, match="refusing to adopt"):
        lab_session.prepare(name)
