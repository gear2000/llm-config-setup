"""Tests for tools/compose-layers.py — the layer composer.

Run: python3 -m pytest tools/test_compose_layers.py -q

Covers the slash-command path that the cross-harness layout depends on:
inputs are concatenated in order, the optional `frontmatter:` passthrough is
emitted (so a command keeps its `argument-hint` etc.), and the recipe's
`output:` lands where it points (the harness/scope routing is the recipe path).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

COMPOSER = Path(__file__).resolve().parent / "compose-layers.py"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _compose(shared_llm: Path, target: Path, recipe_rel: str) -> None:
    result = subprocess.run(
        [sys.executable, str(COMPOSER), recipe_rel,
         "--shared-llm", str(shared_llm), "--target", str(target)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"compose failed: {result.stderr}"


def _split(text: str):
    assert text.startswith("---\n"), "missing frontmatter"
    _, fm_text, body = text.split("---", 2)
    return yaml.safe_load(fm_text), body.lstrip("\n")


def _fixture(tmp_path: Path, bucket: str, name: str, *, inputs: list[str],
             extra_frontmatter: dict | None = None) -> tuple[Path, Path, str]:
    """Build a .shared-llm fixture with one slash-command recipe; return roots + recipe path."""
    shared = tmp_path / ".shared-llm"
    layer_dir = shared / "layers/slash-commands" / bucket / name
    for i, body in enumerate(inputs):
        _write(layer_dir / f"part{i}.md", body)
    _write(layer_dir / "description.md", "A test command.\n")

    recipe = {
        "type": "skill",
        "name": name,
        "description": f".shared-llm/layers/slash-commands/{bucket}/{name}/description.md",
    }
    if extra_frontmatter:
        recipe["frontmatter"] = extra_frontmatter
    recipe["inputs"] = [
        f".shared-llm/layers/slash-commands/{bucket}/{name}/part{i}.md" for i in range(len(inputs))
    ]
    recipe["output"] = f".claude/skills/{name}/SKILL.md"

    recipe_rel = f".shared-llm/compose/slash-commands/{bucket}/{name}.yaml"
    _write(shared.parent / recipe_rel, yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True))
    return shared, tmp_path, recipe_rel


def test_inputs_concatenated_in_order(tmp_path: Path) -> None:
    shared, target, recipe = _fixture(
        tmp_path, "common/common", "ordercmd", inputs=["FIRST layer.", "SECOND layer."]
    )
    _compose(shared, target, recipe)
    fm, body = _split((target / ".claude/skills/ordercmd/SKILL.md").read_text())
    assert fm["name"] == "ordercmd"
    assert fm["description"] == "A test command."
    assert body.index("FIRST layer.") < body.index("SECOND layer.")


def test_frontmatter_passthrough(tmp_path: Path) -> None:
    shared, target, recipe = _fixture(
        tmp_path, "this_repo/claude", "hintcmd", inputs=["body"],
        extra_frontmatter={"argument-hint": "[filter]", "allowed-tools": "Bash(task:*)"},
    )
    _compose(shared, target, recipe)
    fm, _ = _split((target / ".claude/skills/hintcmd/SKILL.md").read_text())
    # name + description first, then the passthrough fields preserved verbatim
    assert fm["argument-hint"] == "[filter]"
    assert fm["allowed-tools"] == "Bash(task:*)"
    assert list(fm.keys())[:2] == ["name", "description"]


def test_output_routing_follows_recipe(tmp_path: Path) -> None:
    shared, target, recipe = _fixture(tmp_path, "common/codex", "codexcmd", inputs=["x"])
    _compose(shared, target, recipe)
    assert (target / ".claude/skills/codexcmd/SKILL.md").exists()


def test_resources_copied_into_output(tmp_path: Path) -> None:
    shared, target, recipe_rel = _fixture(tmp_path, "common/common", "rescmd", inputs=["body"])
    # add a resources tree to the layer source + point the recipe at it
    res = shared / "layers/slash-commands/common/common/rescmd/resources"
    _write(res / "references/guide.md", "REFERENCE GUIDE")
    recipe_path = shared.parent / recipe_rel
    data = yaml.safe_load(recipe_path.read_text())
    data["resources"] = ".shared-llm/layers/slash-commands/common/common/rescmd/resources"
    recipe_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    _compose(shared, target, recipe_rel)
    copied = target / ".claude/skills/rescmd/references/guide.md"
    assert copied.exists() and copied.read_text() == "REFERENCE GUIDE"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
