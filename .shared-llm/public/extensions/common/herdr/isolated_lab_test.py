"""Isolation boundaries must hold before the first Herdr subprocess is started."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location(
    "isolated_lab_tested", HERE / "isolated_lab.py"
)
assert spec and spec.loader
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def test_initialize_separates_binary_auth_integrations_and_ledger(
    tmp_path, monkeypatch
):
    auth = tmp_path / "operator"
    files = (
        ".claude/.credentials.json",
        ".codex/auth.json",
        ".pi/agent/auth.json",
        ".config/cursor/auth.json",
        ".claude.json",
    )
    for name in files:
        path = auth / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"hasCompletedOnboarding": true}')
    binary = tmp_path / "release"
    binary.write_bytes(b"test binary")
    monkeypatch.setattr(
        lab, "RELEASE_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/production/socket")
    monkeypatch.setenv("HERDR_PANE_ID", "production:pane")
    monkeypatch.setenv("UPAGENT_STATE", "/production/state.json")
    calls = []
    monkeypatch.setattr(
        lab.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs))
    )
    # A short root is needed for AF_UNIX's 108-byte path limit.
    monkeypatch.setattr(lab.os, "fsencode", lambda value: b"short-test-socket")
    root = tmp_path / "lab"
    lab.initialize(root, binary, "llm-lab-test", auth)
    assert len(calls) == 5
    assert [args[-1] for args, _ in calls[:4]] == ["pi", "claude", "codex", "cursor"]
    assert calls[-1][0][-2:] == ["provision", "llm-lab-test"]
    for _, kwargs in calls:
        env = kwargs["env"]
        assert kwargs["check"] is True
        assert "HERDR_PANE_ID" not in env
        assert env["HERDR_SESSION"] == "llm-lab-test"
        for key in (
            "HOME",
            "HERDR_SOCKET_PATH",
            "UPAGENT_STATE",
            "UPAGENT_HUB_DIR",
            "UPAGENT_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
            "CODEX_HOME",
            "CURSOR_CONFIG_DIR",
            "PI_CODING_AGENT_DIR",
            "LLM_HERDR_LAB_STATE_DIR",
        ):
            assert Path(env[key]).is_relative_to(root), key
        assert env["PATH"].split(":")[0] == str(root / "bin")
        assert env["UPAGENT_CANONICAL_REPO"] == str(lab.CHECKOUT)
    for name in files[:-1]:
        copy = root / "home" / name
        assert not copy.is_symlink()
        assert copy.stat().st_ino != (auth / name).stat().st_ino
        assert copy.stat().st_mode & 0o777 == 0o400
        assert (auth / name).read_text() == '{"hasCompletedOnboarding": true}'
    assert (root / "bin/herdr").read_bytes() == binary.read_bytes()
    assert json.loads((root / lab.MANIFEST).read_text())["session"] == "llm-lab-test"


def test_unverified_binary_cannot_create_lab(tmp_path):
    binary = tmp_path / "release"
    binary.write_bytes(b"wrong binary")
    root = tmp_path / "lab"
    with pytest.raises(lab.lab_session.LabError, match="release digest"):
        lab.initialize(root, binary, "llm-lab-test", tmp_path)
    assert not root.exists()


def test_existing_lab_is_not_adopted(tmp_path, monkeypatch):
    binary = tmp_path / "release"
    binary.write_bytes(b"test binary")
    monkeypatch.setattr(
        lab, "RELEASE_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    root = tmp_path / "lab"
    root.mkdir()
    marker = root / "handwritten"
    marker.write_text("preserve")
    with pytest.raises(FileExistsError):
        lab.initialize(root, binary, "llm-lab-test", tmp_path)
    assert marker.read_text() == "preserve"


def test_environment_refuses_home_symlink(tmp_path):
    (tmp_path / "home").symlink_to(Path.home(), target_is_directory=True)
    with pytest.raises(lab.lab_session.LabError, match="separate"):
        lab.environment(tmp_path, {"session": "llm-lab-test"})


def test_environment_refuses_default_session(tmp_path):
    (tmp_path / "home").mkdir()
    with pytest.raises(lab.lab_session.LabError, match="default"):
        lab.environment(tmp_path, {"session": "default"})


def test_environment_refuses_socket_path_that_cannot_bind(tmp_path):
    (tmp_path / "home").mkdir()
    with pytest.raises(lab.lab_session.LabError, match="too long"):
        lab.environment(tmp_path, {"session": "llm-lab-" + "x" * 100})
