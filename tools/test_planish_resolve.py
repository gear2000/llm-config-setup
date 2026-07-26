"""Executable contracts for deterministic work-log output placement."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".shared-llm/public/llm/common/common/planish_resolve.py"
PI_EXTENSION = ROOT / ".shared-llm/public/llm/pi/common/extensions/do-planish.ts"
SPEC = importlib.util.spec_from_file_location("planish_resolve_tested", SCRIPT)
assert SPEC and SPEC.loader
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)

ENV_VARS = ("WORK_LOG_DIR", "PLANISH_DIR", "WORK_LOG_HOST", "PLANISH_HOST")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case states its own environment — inherit nothing."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ─── directory precedence ────────────────────────────────────────────────────


def test_explicit_dir_wins_and_expands_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORK_LOG_DIR", "ignored")
    monkeypatch.setenv("PLANISH_DIR", "ignored")
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: ignored\n")
    result = resolver.resolve(tmp_path, "New API", "plans/{date}/{slug}/{type}/v{n}")
    assert (
        result
        == tmp_path / "plans" / date.today().isoformat() / "new-api" / "plan" / "v1"
    )
    assert result.is_dir()


def test_work_log_dir_env_beats_legacy_env_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORK_LOG_DIR", "from-env/{slug}")
    monkeypatch.setenv("PLANISH_DIR", "legacy-env/{slug}")
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: from-config/{slug}\n")
    assert resolver.resolve(tmp_path, "Topic") == tmp_path / "from-env/topic"


def test_legacy_env_still_works_but_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLANISH_DIR", "legacy-env/{slug}")
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: from-config/{slug}\n")
    assert resolver.resolve(tmp_path, "Topic") == tmp_path / "legacy-env/topic"
    captured = capsys.readouterr()
    assert "$PLANISH_DIR is deprecated" in captured.err
    assert captured.out == ""


def test_config_without_work_log_key_is_skipped_and_walk_continues(
    tmp_path: Path,
) -> None:
    """A destination roster (~/.shared-llm.yaml) has no work_log: — it must not
    shadow the repo config nor stop the walk."""
    roster = "destinations:\n  - path: /somewhere/repo\n    harnesses: [cc, pi]\n"
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: docs/plans/{slug}\n")
    _write(tmp_path / "repo/.shared-llm.yaml", roster)
    nested = tmp_path / "repo/src/deep"
    nested.mkdir(parents=True)
    _write(nested / ".shared-llm.yaml", roster)
    assert resolver.resolve(nested, "Topic") == tmp_path / "docs/plans/topic"


def test_nearest_work_log_config_wins_over_an_outer_one(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: outer/{slug}\n")
    inner = tmp_path / "repo"
    _write(inner / ".shared-llm.yaml", "work_log:\n  dir: inner/{slug}\n")
    assert resolver.resolve(inner, "Topic") == inner / "inner/topic"


def test_config_is_relative_to_config_and_version_increments(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src/deep"
    nested.mkdir(parents=True)
    _write(repo / ".shared-llm.yaml", "work_log:\n  dir: docs/plans/{slug}/v{n}\n")
    (repo / "docs/plans/topic/v1").mkdir(parents=True)
    assert resolver.resolve(nested, "Topic") == repo / "docs/plans/topic/v2"


def test_absolute_configured_dir_is_used_as_is(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    _write(
        tmp_path / "repo/.shared-llm.yaml",
        f"work_log:\n  dir: {target}/{{slug}}\n",
    )
    assert resolver.resolve(tmp_path / "repo", "Topic") == target / "topic"


def test_malformed_work_log_dir_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: []\n")
    with pytest.raises(ValueError, match="work_log.dir.*non-empty string"):
        resolver.resolve(tmp_path, "Topic")


def test_empty_work_log_dir_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", 'work_log:\n  dir: "   "\n')
    with pytest.raises(ValueError, match="work_log.dir.*non-empty string"):
        resolver.resolve(tmp_path, "Topic")


def test_work_log_that_is_not_a_mapping_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log: /var/tmp/plans\n")
    with pytest.raises(ValueError, match="work_log.*must be a mapping"):
        resolver.resolve(tmp_path, "Topic")


@pytest.mark.parametrize("body", ["work_log:\n", "work_log: {}\n"])
def test_empty_work_log_fails_loud(tmp_path: Path, body: str) -> None:
    """An empty block carries no dir and no host — taking the default silently
    is the fallback this contract exists to prevent."""
    _write(tmp_path / ".shared-llm.yaml", body)
    with pytest.raises(ValueError, match="work_log"):
        resolver.resolve(tmp_path, "Topic")


def test_work_log_flow_mapping_is_honored(tmp_path: Path) -> None:
    """Flow style is valid YAML — it must behave like the block form."""
    _write(tmp_path / ".shared-llm.yaml", 'work_log: {dir: "plans/{slug}"}\n')
    result = resolver.resolve(tmp_path, "Redesign Auth")
    assert result == tmp_path / "plans/redesign-auth"


def test_work_log_without_dir_takes_the_default(tmp_path: Path) -> None:
    """host-only config is legitimate — the dir falls back to the default."""
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  host: example-host\n")
    result = resolver.resolve(tmp_path, "Topic")
    default_root = str(Path("/var/tmp/work-log").resolve())
    assert str(result).startswith(default_root + "/")


# ─── legacy .planish.yaml fallback ───────────────────────────────────────────


def test_legacy_config_still_resolves_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src"
    nested.mkdir(parents=True)
    _write(repo / ".planish.yaml", "dir: docs/plans/{slug}\n")
    assert resolver.resolve(nested, "Topic") == repo / "docs/plans/topic"
    captured = capsys.readouterr()
    assert ".planish.yaml is deprecated" in captured.err
    assert "work_log.dir" in captured.err
    assert captured.out == ""


def test_work_log_config_beats_a_nearer_legacy_config(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: modern/{slug}\n")
    nested = tmp_path / "repo"
    _write(nested / ".planish.yaml", "dir: legacy/{slug}\n")
    assert resolver.resolve(nested, "Topic") == tmp_path / "modern/topic"


def test_malformed_legacy_dir_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".planish.yaml", "dir: []\n")
    with pytest.raises(ValueError, match='"dir" must be a non-empty string'):
        resolver.resolve(tmp_path, "Topic")


def test_default_is_outside_repo(tmp_path: Path) -> None:
    result = resolver.resolve(tmp_path, "Topic")
    # /var/tmp may itself be a symlink, so compare against the resolved root
    # rather than the literal string.
    default_root = str(Path("/var/tmp/work-log").resolve())
    assert str(result).startswith(default_root + "/")
    assert result.name == "topic"


def test_empty_topic_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="topic must be non-empty"):
        resolver.resolve(tmp_path, "   ")


# ─── review host ─────────────────────────────────────────────────────────────


def test_host_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n  dir: plans/{slug}\n  host: config-host\n",
    )
    assert resolver.resolve_host(tmp_path) == "config-host"

    monkeypatch.setenv("PLANISH_HOST", "legacy-host")
    assert resolver.resolve_host(tmp_path) == "legacy-host"
    assert "$PLANISH_HOST is deprecated" in capsys.readouterr().err

    monkeypatch.setenv("WORK_LOG_HOST", "env-host")
    assert resolver.resolve_host(tmp_path) == "env-host"


def test_host_falls_back_to_legacy_config_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / ".planish.yaml", "dir: plans/{slug}\nhost: legacy-host\n")
    assert resolver.resolve_host(tmp_path) == "legacy-host"
    captured = capsys.readouterr()
    assert "work_log.host" in captured.err


def test_host_is_none_when_unconfigured(tmp_path: Path) -> None:
    assert resolver.resolve_host(tmp_path) is None


def test_work_log_config_without_host_does_not_reach_legacy(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: plans/{slug}\n")
    _write(tmp_path / "repo/.planish.yaml", "host: legacy-host\n")
    assert resolver.resolve_host(tmp_path / "repo") is None


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_stdout_stays_pure_json_while_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLANISH_DIR", str(tmp_path / "plans/{slug}"))
    monkeypatch.setenv("WORK_LOG_HOST", "env-host")
    assert resolver.main(["--topic", "Topic", "--cwd", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {"host": "env-host", "plan_dir": str(tmp_path / "plans/topic")}
    assert "deprecated" in captured.err


def test_cli_fails_loudly_on_a_malformed_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: []\n")
    with pytest.raises(SystemExit) as exit_info:
        resolver.main(["--topic", "Topic", "--cwd", str(tmp_path)])
    assert "work_log.dir" in str(exit_info.value)
    assert capsys.readouterr().out == ""


def test_pi_submit_requires_the_canonical_absolute_path() -> None:
    source = PI_EXTENSION.read_text()
    assert 'name: "planish_resolve_dir"' not in source
    assert "filePath must be the absolute path returned by planish_resolve.py" in source
    assert "const base = path.dirname(filePath)" in source
    assert "ctx?.cwd ?? process.cwd()" not in source
