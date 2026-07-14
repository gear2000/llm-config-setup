"""Unit tests for Specialist Hub configuration resolution. No Herdr runtime is launched."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "specialist_hub", Path(__file__).with_name("hub.py")
)
assert _spec is not None
assert _spec.loader is not None
hub = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = hub
_spec.loader.exec_module(hub)


def _write_roster(path: Path, runtime_dir: Path, repo_root: Path | None = None) -> None:
    repo_root_line = f"repo_root: {repo_root}\n" if repo_root is not None else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "runtime_dir: " + str(runtime_dir) + "\n"
        + repo_root_line
        + "agents:\n"
        + "  - name: docs\n"
        + "    location: .claude/agents/docs.md\n"
        + "    cmd: 'claude -p {prompt} --agent docs'\n"
    )


def test_missing_repo_root_uses_roster_ancestor_from_nested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    invocation_dir = repo_root / "nested/invocation"
    invocation_dir.mkdir(parents=True)
    _write_roster(roster, tmp_path / "runtime")
    monkeypatch.delenv("SPECIALIST_HUB_CONFIG", raising=False)
    monkeypatch.chdir(invocation_dir)

    cfg = hub.load_config()

    assert cfg["repo_root"] == repo_root


def test_missing_repo_root_uses_roster_ancestor_from_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    _write_roster(roster, tmp_path / "runtime")
    outside_repository = tmp_path / "outside"
    outside_repository.mkdir()
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))
    monkeypatch.chdir(outside_repository)

    cfg = hub.load_config()

    assert cfg["repo_root"] == repo_root


def test_missing_repo_root_fails_when_roster_is_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = tmp_path / "not-a-repository/config/agents.yaml"
    _write_roster(roster, tmp_path / "runtime")
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))

    with pytest.raises(hub.ConfigError, match="could not find a repository root"):
        hub.load_config()


def test_relative_specialist_location_uses_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    definition = repo_root / ".claude/agents/docs.md"
    definition.parent.mkdir(parents=True)
    definition.write_text('---\ndescription: "Repository docs specialist."\n---\n')
    roster = tmp_path / "config/agents.yaml"
    _write_roster(roster, tmp_path / "runtime", repo_root)
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))

    cfg = hub.load_config()

    assert hub.resolve_specialist_location(cfg, ".claude/agents/docs.md") == definition
    assert hub._description(cfg, cfg["agents"][0]) == "Repository docs specialist."


def test_runtime_dir_command_uses_roster_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_dir = tmp_path / "configured-runtime"
    roster = tmp_path / "config/agents.yaml"
    _write_roster(roster, runtime_dir, tmp_path / "repo")
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))
    monkeypatch.setattr(sys, "argv", ["hub.py", "runtime-dir"])

    hub.main()

    assert capsys.readouterr().out == f"{runtime_dir}\n"
