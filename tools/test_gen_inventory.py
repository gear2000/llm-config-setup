"""Tests for tools/gen_inventory.py — the README inventory generator.

Run: python3.14 -m pytest tools/test_gen_inventory.py -q

Covers the branching logic code review flagged as untested (gen_inventory.py had
zero coverage while the rest of tools/ has a full suite): marker handling in
rewrite_readme() (missing END, reversed order, the exactly-one-pair happy path),
summarize()'s 100-char word-boundary truncation, fail-loud behavior when a recipe
is missing `name` / `description` / points at a missing description file, and
idempotence of a full generate run.

gen_inventory.py computes REPO_ROOT / SHARED / README from its own `__file__` at
import time, so — mirroring test_config_flow.py's `_patch_home` — every test here
loads a fresh module instance and repoints those globals at a tmp_path fixture
before calling into it. CATEGORIES is bound from the ORIGINAL SHARED at import
time (a list comprehension evaluated once at module load), so it needs its own
explicit override per test rather than following a patched SHARED automatically.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import yaml

TOOLS_DIR = Path(__file__).resolve().parent
GEN_INVENTORY = TOOLS_DIR / "gen_inventory.py"

# gen_inventory.py does a bare `import harness`, which only resolves without a
# package context if tools/ is on sys.path (true when it's run as `python3
# tools/gen_inventory.py` — Python adds the script's own dir to sys.path[0]).
# Loading it here via importlib doesn't guarantee that, so make it explicit
# instead of relying on pytest's import-mode default.
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def _load():
    spec = importlib.util.spec_from_file_location("gen_inventory_under_test", GEN_INVENTORY)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _patch_root(m, root: Path) -> None:
    """Point the module's fixed repo-root globals at a tmp fixture."""
    m.REPO_ROOT = root
    m.SHARED = root / ".shared-llm"
    m.README = root / "README.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- summarize() / description truncation -----------------------------------

def test_summarize_short_string_returned_unchanged() -> None:
    m = _load()
    text = "A short description well under the limit."
    assert len(text) < m.MAX_DESC_LEN
    assert m.summarize(text) == text


def test_summarize_exactly_max_len_returned_unchanged() -> None:
    m = _load()
    text = "x" * m.MAX_DESC_LEN
    assert m.summarize(text) == text  # len == max_len: the <= boundary, not truncated


def test_summarize_long_string_truncates_at_word_boundary_not_mid_word() -> None:
    m = _load()
    # 95 'x's, a space, then 20 'y's: a naive text[:100] slice would cut the 'y'
    # run down to 4 chars ("...x yyyy") — a broken word fragment. Word-boundary
    # truncation must back up past the whole partial word instead of keeping it.
    text = ("x" * 95) + " " + ("y" * 20)
    assert len(text) > m.MAX_DESC_LEN

    result = m.summarize(text)

    assert result == ("x" * 95) + "…"
    assert "y" not in result  # the partial word is dropped whole, not cut mid-word
    assert len(result) < m.MAX_DESC_LEN


# --- marker handling in rewrite_readme() -------------------------------------

def test_begin_without_end_fails_loud_and_leaves_file_untouched(tmp_path: Path, capsys) -> None:
    m = _load()
    _patch_root(m, tmp_path)
    m.CATEGORIES = []  # isolate marker logic from recipe discovery
    original = (
        f"# Title\n\nintro\n\n{m.BEGIN_MARKER}\nstale body\n\n"
        "more text, no end marker anywhere.\n"
    )
    _write(m.README, original)

    try:
        m.rewrite_readme()
        assert False, "expected SystemExit when END marker is missing"
    except SystemExit as e:
        assert e.code not in (0, None)
    err = capsys.readouterr().err
    assert m.END_MARKER in err
    # Fail-loud must not eat the rest of the file: content is untouched.
    assert m.README.read_text() == original


def test_reversed_marker_order_fails_loud_and_leaves_file_untouched(tmp_path: Path, capsys) -> None:
    m = _load()
    _patch_root(m, tmp_path)
    m.CATEGORIES = []
    # Both markers present exactly once, but END appears before BEGIN.
    original = f"# Title\n\n{m.END_MARKER}\nmiddle\n{m.BEGIN_MARKER}\n"
    _write(m.README, original)

    try:
        m.rewrite_readme()
        assert False, "expected SystemExit when markers are reversed"
    except SystemExit as e:
        assert e.code not in (0, None)
    err = capsys.readouterr().err
    assert "before" in err
    assert m.README.read_text() == original


def test_exactly_one_marker_pair_rewrites_in_place(tmp_path: Path) -> None:
    m = _load()
    _patch_root(m, tmp_path)
    m.CATEGORIES = []
    original = (
        "# Title\n\nintro text stays.\n\n"
        f"{m.BEGIN_MARKER}\nSTALE OLD BODY\n{m.END_MARKER}\n\n"
        "trailing text stays.\n"
    )
    _write(m.README, original)

    changed = m.rewrite_readme()

    assert changed is True
    text = m.README.read_text()
    assert "intro text stays." in text
    assert "trailing text stays." in text
    assert "STALE OLD BODY" not in text
    assert m.GENERATED_NOTICE in text
    assert text.count(m.BEGIN_MARKER) == 1
    assert text.count(m.END_MARKER) == 1


# --- fail-loud on a malformed recipe -----------------------------------------

def test_missing_name_field_fails_loud(tmp_path: Path, capsys) -> None:
    m = _load()
    _patch_root(m, tmp_path)
    recipe_dir = tmp_path / ".shared-llm/compose/skills"
    # type: prompt bypasses harness.load_compose_yaml's OWN name/description
    # requirement (it only applies to skill/agent-shaped recipes) — this recipe
    # is deliberately shaped so it's gen_inventory.py's own check that fires,
    # not harness's pre-existing one. The description file DOES exist and
    # resolves cleanly, so `name` is the only thing wrong with this recipe —
    # isolates the name check from the (separately tested) description checks.
    _write(recipe_dir / "noname.yaml", yaml.safe_dump({
        "type": "prompt",
        "inputs": [],
        "output": "x.md",
        "description": ".shared-llm/layers/skills/noname/description.md",
    }, sort_keys=False))
    _write(tmp_path / ".shared-llm/layers/skills/noname/description.md", "A description.\n")

    try:
        m.load_entries(recipe_dir)
        assert False, "expected SystemExit on missing name"
    except SystemExit as e:
        assert e.code not in (0, None)
    err = capsys.readouterr().err
    assert "noname.yaml" in err
    assert "name" in err


def test_missing_description_field_fails_loud(tmp_path: Path, capsys) -> None:
    m = _load()
    _patch_root(m, tmp_path)
    recipe_dir = tmp_path / ".shared-llm/compose/skills"
    _write(recipe_dir / "nodesc.yaml", yaml.safe_dump({
        "type": "prompt",  # bypasses harness's own description requirement too
        "name": "nodesc",
        "inputs": [],
        "output": "x.md",
    }, sort_keys=False))

    try:
        m.load_entries(recipe_dir)
        assert False, "expected SystemExit on missing description"
    except SystemExit as e:
        assert e.code not in (0, None)
    err = capsys.readouterr().err
    assert "nodesc.yaml" in err
    assert "description" in err


def test_missing_description_file_fails_loud(tmp_path: Path, capsys) -> None:
    m = _load()
    _patch_root(m, tmp_path)
    recipe_dir = tmp_path / ".shared-llm/compose/skills"
    _write(recipe_dir / "ghost.yaml", yaml.safe_dump({
        "type": "skill",
        "name": "ghost",
        "inputs": ["x"],
        "output": "x.md",
        "description": ".shared-llm/layers/skills/ghost/description.md",  # never written
    }, sort_keys=False))

    try:
        m.load_entries(recipe_dir)
        assert False, "expected SystemExit on missing description file"
    except SystemExit as e:
        assert e.code not in (0, None)
    err = capsys.readouterr().err
    assert "description.md" in err


# --- idempotence --------------------------------------------------------------

def test_generate_twice_is_idempotent(tmp_path: Path, capsys) -> None:
    m = _load()
    _patch_root(m, tmp_path)
    skills_dir = m.SHARED / "compose" / "skills"
    _write(tmp_path / ".shared-llm/layers/skills/alpha/description.md", "Alpha does one thing well.\n")
    _write(skills_dir / "alpha.yaml", yaml.safe_dump({
        "type": "skill",
        "name": "alpha",
        "description": ".shared-llm/layers/skills/alpha/description.md",
        "inputs": ["x"],
        "output": ".claude/skills/alpha/SKILL.md",
    }, sort_keys=False))
    m.CATEGORIES = [("Skills", skills_dir, "Test skills.")]
    _write(m.README, f"# Title\n\n{m.BEGIN_MARKER}\nplaceholder\n{m.END_MARKER}\n")

    m.main()
    first_text = m.README.read_text()
    first_out = capsys.readouterr().out
    assert "alpha" in first_text
    assert "updated" in first_out

    m.main()
    second_text = m.README.read_text()
    second_out = capsys.readouterr().out

    assert second_text == first_text
    assert "already up to date" in second_out


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
