"""Tests for tools/harness.py compose — the layer composer.

Run: python3.14 -m pytest tools/test_compose_layers.py -q

Covers the slash-command path that the cross-harness layout depends on:
inputs are concatenated in order, the optional `frontmatter:` passthrough is
emitted (so a command keeps its `argument-hint` etc.), and the recipe's
`output:` lands where it points (the harness/scope routing is the recipe path).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

TOOLS_DIR = Path(__file__).resolve().parent
COMPOSER = TOOLS_DIR / "harness.py"
REPO_ROOT = TOOLS_DIR.parent


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _compose(shared_llm: Path, target: Path, recipe_rel: str) -> None:
    result = subprocess.run(
        [sys.executable, str(COMPOSER), "compose", recipe_rel,
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


def _compose_expect_fail(shared_llm: Path, target: Path, recipe_rel: str) -> str:
    """Run compose expecting a non-zero exit; return stderr."""
    result = subprocess.run(
        [sys.executable, str(COMPOSER), "compose", recipe_rel,
         "--shared-llm", str(shared_llm), "--target", str(target)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "compose unexpectedly succeeded"
    return result.stderr


def test_copy_verbatim_preserves_executable_bit(tmp_path: Path) -> None:
    shared = tmp_path / ".shared-llm"
    src = shared / "llm/claude/common/hooks/hook.sh"
    _write(src, "#!/usr/bin/env bash\necho hi\n")
    src.chmod(0o755)
    recipe_rel = ".shared-llm/compose/hooks/common/hook.yaml"
    _write(shared.parent / recipe_rel, yaml.safe_dump({
        "type": "copy",
        "inputs": [".shared-llm/llm/claude/common/hooks/hook.sh"],
        "output": ".claude/hooks/hook.sh",
    }, sort_keys=False))
    _compose(shared, tmp_path, recipe_rel)
    out = tmp_path / ".claude/hooks/hook.sh"
    assert out.read_text() == "#!/usr/bin/env bash\necho hi\n"
    assert out.stat().st_mode & 0o111, "executable bit not preserved"


def test_copy_rejects_multiple_inputs(tmp_path: Path) -> None:
    shared = tmp_path / ".shared-llm"
    _write(shared / "llm/claude/common/hooks/a.sh", "a")
    _write(shared / "llm/claude/common/hooks/b.sh", "b")
    recipe_rel = ".shared-llm/compose/hooks/common/two.yaml"
    _write(shared.parent / recipe_rel, yaml.safe_dump({
        "type": "copy",
        "inputs": [".shared-llm/llm/claude/common/hooks/a.sh",
                   ".shared-llm/llm/claude/common/hooks/b.sh"],
        "output": ".claude/hooks/x.sh",
    }, sort_keys=False))
    stderr = _compose_expect_fail(shared, tmp_path, recipe_rel)
    assert "exactly one input" in stderr


def _load_harness_module():
    spec = importlib.util.spec_from_file_location("harness_under_test", COMPOSER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_install_global_routes_slash_skills_by_harness(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = os.environ.copy()
    env["HOME"] = str(home)
    # The scripts default to python3.14 in production. The test pins the current
    # interpreter so CI can run as long as it is new enough for harness.py.
    env["PYTHON_BIN"] = sys.executable
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "tools/install-global.sh")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"install-global failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    claude = home / ".claude/skills"
    pi = home / ".pi/agent/skills"
    assert (claude / "cc-plan-and-grill/SKILL.md").exists(), "Claude should receive workflow cc-* planning skills"
    assert (claude / "qa/SKILL.md").exists(), "Claude should receive portable non-do-* skills"
    assert not (claude / "cc-planish").exists(), "Removed standalone cc-planish must not install"
    assert not (claude / "do-planish").exists(), "Removed standalone do-planish must not install"

    for name in ("do-full", "do-implement", "do-loop", "do-oneshot", "do-plan-and-grill", "do-research"):
        assert (pi / name / "SKILL.md").exists(), f"Pi should receive portable {name}"
    assert (pi / "qa/SKILL.md").exists(), "Pi should receive portable common skills"
    assert not (pi / "do-planish").exists(), "Removed standalone do-planish must not install"
    assert not (pi / "cc-planish").exists(), "Pi must not receive Claude-only/removed cc-planish"


def test_plan_pi_routes_common_skills_and_planish_extension(tmp_path: Path) -> None:
    harness = _load_harness_module()
    root = tmp_path / "repo"
    home = tmp_path / "home"
    harness.HOME = home

    for skill in ("do-plan-and-grill", "qa", "cc-plan-and-grill"):
        _write(root / ".claude/skills" / skill / "SKILL.md", f"---\nname: {skill}\n---\n")
    _write(root / ".shared-llm/compose/slash-commands/common/common/do-plan-and-grill.yaml", "name: do-plan-and-grill\n")
    _write(root / ".shared-llm/compose/slash-commands/common/common/qa.yaml", "name: qa\n")
    _write(root / ".shared-llm/compose/slash-commands/common/claude/cc-plan-and-grill.yaml", "name: cc-plan-and-grill\n")
    _write(root / ".shared-llm/llm/pi/common/extensions/planish.ts", "export default function() {}\n")

    plan = harness.plan_pi(root)
    assert plan.desired[home / ".pi/agent/skills/do-plan-and-grill"] == root / ".claude/skills/do-plan-and-grill"
    assert plan.desired[home / ".pi/agent/skills/qa"] == root / ".claude/skills/qa"
    assert home / ".pi/agent/skills/cc-plan-and-grill" not in plan.desired
    assert plan.desired[home / ".pi/agent/extensions/planish.ts"] == root / ".shared-llm/llm/pi/common/extensions/planish.ts"


def test_planish_visual_contract_is_referenced_and_runtime_exposes_visual_fields() -> None:
    contract = REPO_ROOT / ".shared-llm/llm/common/common/planish-html-grill-contract.md"
    assert contract.exists()
    for rel in (
        ".shared-llm/layers/slash-commands/common/common/do-plan-and-grill/command.md",
        ".shared-llm/layers/slash-commands/common/claude/cc-plan-and-grill/command.md",
    ):
        text = (REPO_ROOT / rel).read_text()
        assert "planish-html-grill-contract.md" in text
        assert "plain chat list of questions" in text

    planish_ts = (REPO_ROOT / ".shared-llm/llm/pi/common/extensions/planish.ts").read_text()
    for token in ("contextHtml", "mermaid", "ascii", "visualHtml", "Copy Answers", "+ Note", "Copy Feedback"):
        assert token in planish_ts

    assert not (REPO_ROOT / ".shared-llm/compose/slash-commands/common/common/do-planish.yaml").exists()
    assert not (REPO_ROOT / ".shared-llm/compose/slash-commands/common/claude/cc-planish.yaml").exists()


def test_settings_deep_merge(tmp_path: Path) -> None:
    shared = tmp_path / ".shared-llm"
    base = {
        "permissions": {"defaultMode": "acceptEdits"},
        "hooks": {"PostToolUse": [{"matcher": "Edit"}]},
        "effortLevel": "low",
    }
    overlay = {
        "hooks": {"PreToolUse": [{"matcher": "Bash"}], "PostToolUse": [{"matcher": "Write"}]},
        "effortLevel": "max",
    }
    _write(shared / "llm/claude/common/settings.json", json.dumps(base))
    _write(shared / "llm/claude/this_repo/settings.json", json.dumps(overlay))
    recipe_rel = ".shared-llm/compose/settings/settings.yaml"
    _write(shared.parent / recipe_rel, yaml.safe_dump({
        "type": "settings",
        "inputs": [".shared-llm/llm/claude/common/settings.json",
                   ".shared-llm/llm/claude/this_repo/settings.json"],
        "output": ".claude/settings.json",
    }, sort_keys=False))
    _compose(shared, tmp_path, recipe_rel)
    merged = json.loads((tmp_path / ".claude/settings.json").read_text())
    # dict recurses (permissions survives from base, hooks gains PreToolUse)
    assert merged["permissions"]["defaultMode"] == "acceptEdits"
    assert set(merged["hooks"].keys()) == {"PostToolUse", "PreToolUse"}
    # list concatenates (both PostToolUse entries kept, base first)
    assert merged["hooks"]["PostToolUse"] == [{"matcher": "Edit"}, {"matcher": "Write"}]
    # scalar overlay wins
    assert merged["effortLevel"] == "max"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
