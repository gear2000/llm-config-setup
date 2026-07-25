"""Executable contracts for deterministic Planish output placement."""

from __future__ import annotations

import importlib.util
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


def test_explicit_dir_wins_and_expands_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANISH_DIR", "ignored")
    result = resolver.resolve(tmp_path, "New API", "plans/{date}/{slug}/{type}/v{n}")
    assert (
        result
        == tmp_path / "plans" / date.today().isoformat() / "new-api" / "plan" / "v1"
    )
    assert result.is_dir()


def test_config_is_relative_to_config_and_version_increments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PLANISH_DIR", raising=False)
    repo = tmp_path / "repo"
    nested = repo / "src/deep"
    nested.mkdir(parents=True)
    (repo / ".planish.yaml").write_text("dir: docs/plans/{slug}/v{n}\n")
    (repo / "docs/plans/topic/v1").mkdir(parents=True)
    assert resolver.resolve(nested, "Topic") == repo / "docs/plans/topic/v2"


def test_malformed_config_dir_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PLANISH_DIR", raising=False)
    (tmp_path / ".planish.yaml").write_text("dir: []\n")
    with pytest.raises(ValueError, match="dir.*non-empty string"):
        resolver.resolve(tmp_path, "Topic")


def test_default_is_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PLANISH_DIR", raising=False)
    result = resolver.resolve(tmp_path, "Topic")
    # /tmp may itself be a symlink (macOS: /tmp -> /private/tmp), so compare
    # against the resolved default root rather than the literal string.
    default_root = str(Path("/tmp/planish").resolve())
    assert str(result).startswith(default_root + "/")


def test_pi_submit_requires_the_canonical_absolute_path() -> None:
    source = PI_EXTENSION.read_text()
    assert 'name: "planish_resolve_dir"' not in source
    assert "filePath must be the absolute path returned by planish_resolve.py" in source
    assert "const base = path.dirname(filePath)" in source
    assert "ctx?.cwd ?? process.cwd()" not in source
