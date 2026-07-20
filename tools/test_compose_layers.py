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
import subprocess
import sys
from pathlib import Path

import yaml

TOOLS_DIR = Path(__file__).resolve().parent
COMPOSER = TOOLS_DIR / "harness.py"
REPO_ROOT = TOOLS_DIR.parent


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _compose(shared_llm: Path, target: Path, recipe_rel: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(COMPOSER),
            "compose",
            recipe_rel,
            "--shared-llm",
            str(shared_llm),
            "--target",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"compose failed: {result.stderr}"


def _split(text: str):
    assert text.startswith("---\n"), "missing frontmatter"
    _, fm_text, body = text.split("---", 2)
    return yaml.safe_load(fm_text), body.lstrip("\n")


def _fixture(
    tmp_path: Path,
    bucket: str,
    name: str,
    *,
    inputs: list[str],
    extra_frontmatter: dict | None = None,
) -> tuple[Path, Path, str]:
    """Build a .shared-llm fixture with one slash-command recipe; return roots + recipe path."""
    shared = tmp_path / ".shared-llm"
    layer_dir = shared / "public/layers/slash-commands" / bucket / name
    for i, body in enumerate(inputs):
        _write(layer_dir / f"part{i}.md", body)
    _write(layer_dir / "description.md", "A test command.\n")

    recipe: dict[str, object] = {
        "type": "skill",
        "name": name,
        "description": f".shared-llm/public/layers/slash-commands/{bucket}/{name}/description.md",
    }
    if extra_frontmatter:
        recipe["frontmatter"] = extra_frontmatter
    recipe["inputs"] = [
        f".shared-llm/public/layers/slash-commands/{bucket}/{name}/part{i}.md"
        for i in range(len(inputs))
    ]
    recipe["output"] = f".claude/skills/{name}/SKILL.md"

    recipe_rel = f".shared-llm/public/compose/slash-commands/{bucket}/{name}.yaml"
    _write(
        shared.parent / recipe_rel,
        yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True),
    )
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
        tmp_path,
        "this_repo/claude",
        "hintcmd",
        inputs=["body"],
        extra_frontmatter={
            "argument-hint": "[filter]",
            "allowed-tools": "Bash(task:*)",
        },
    )
    _compose(shared, target, recipe)
    fm, _ = _split((target / ".claude/skills/hintcmd/SKILL.md").read_text())
    # name + description first, then the passthrough fields preserved verbatim
    assert fm["argument-hint"] == "[filter]"
    assert fm["allowed-tools"] == "Bash(task:*)"
    assert list(fm.keys())[:2] == ["name", "description"]


def test_output_routing_follows_recipe(tmp_path: Path) -> None:
    shared, target, recipe = _fixture(
        tmp_path, "common/codex", "codexcmd", inputs=["x"]
    )
    _compose(shared, target, recipe)
    assert (target / ".claude/skills/codexcmd/SKILL.md").exists()


def test_resources_copied_into_output(tmp_path: Path) -> None:
    shared, target, recipe_rel = _fixture(
        tmp_path, "common/common", "rescmd", inputs=["body"]
    )
    # add a resources tree to the layer source + point the recipe at it
    res = shared / "public/layers/slash-commands/common/common/rescmd/resources"
    _write(res / "references/guide.md", "REFERENCE GUIDE")
    recipe_path = shared.parent / recipe_rel
    data = yaml.safe_load(recipe_path.read_text())
    data["resources"] = (
        ".shared-llm/public/layers/slash-commands/common/common/rescmd/resources"
    )
    recipe_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    _compose(shared, target, recipe_rel)
    copied = target / ".claude/skills/rescmd/references/guide.md"
    assert copied.exists() and copied.read_text() == "REFERENCE GUIDE"


def _compose_expect_fail(shared_llm: Path, target: Path, recipe_rel: str) -> str:
    """Run compose expecting a non-zero exit; return stderr."""
    result = subprocess.run(
        [
            sys.executable,
            str(COMPOSER),
            "compose",
            recipe_rel,
            "--shared-llm",
            str(shared_llm),
            "--target",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "compose unexpectedly succeeded"
    return result.stderr


def test_input_outside_shared_llm_resolves_against_repo_root(tmp_path: Path) -> None:
    """A recipe input that does NOT start with `.shared-llm/` (repo content that
    lives outside the layer tree) resolves against the repo root, identically to
    a `.shared-llm/...` path. This is the single-rule resolver (review item 1):
    no prefix-stripping, no second base — every input path is `repo_root / path`.
    """
    shared = tmp_path / ".shared-llm"
    _write(shared / "public/layers/agents/this_repo/desc.md", "A test agent.\n")
    _write(
        shared / "public/layers/agents/this_repo/body.md",
        "LAYER BODY from .shared-llm tree.\n",
    )
    # A file OUTSIDE .shared-llm/, elsewhere in the repo (mirrors the private
    # repo's `ops/mkdocs/...` doc pulled into a skill). It must resolve to
    # repo_root / "external/docs/platform.md", NOT shared / "external/...".
    _write(
        tmp_path / "external/docs/platform.md", "EXTERNAL DOC outside .shared-llm.\n"
    )
    recipe_rel = ".shared-llm/public/compose/agents/demo.yaml"
    _write(
        shared.parent / recipe_rel,
        yaml.safe_dump(
            {
                "type": "agent",
                "name": "demo-agent",
                "model": "opus",
                "description": ".shared-llm/public/layers/agents/this_repo/desc.md",
                "inputs": [
                    ".shared-llm/public/layers/agents/this_repo/body.md",
                    "external/docs/platform.md",
                ],
                "output": ".claude/agents/demo-agent.md",
            },
            sort_keys=False,
        ),
    )
    _compose(shared, tmp_path, recipe_rel)
    out = (tmp_path / ".claude/agents/demo-agent.md").read_text()
    assert "LAYER BODY from .shared-llm tree." in out
    assert "EXTERNAL DOC outside .shared-llm." in out


def test_copy_verbatim_preserves_executable_bit(tmp_path: Path) -> None:
    shared = tmp_path / ".shared-llm"
    src = shared / "public/llm/claude/common/hooks/hook.sh"
    _write(src, "#!/usr/bin/env bash\necho hi\n")
    src.chmod(0o755)
    recipe_rel = ".shared-llm/public/compose/hooks/common/hook.yaml"
    _write(
        shared.parent / recipe_rel,
        yaml.safe_dump(
            {
                "type": "copy",
                "inputs": [".shared-llm/public/llm/claude/common/hooks/hook.sh"],
                "output": ".claude/hooks/hook.sh",
            },
            sort_keys=False,
        ),
    )
    _compose(shared, tmp_path, recipe_rel)
    out = tmp_path / ".claude/hooks/hook.sh"
    assert out.read_text() == "#!/usr/bin/env bash\necho hi\n"
    assert out.stat().st_mode & 0o111, "executable bit not preserved"


def test_copy_rejects_multiple_inputs(tmp_path: Path) -> None:
    shared = tmp_path / ".shared-llm"
    _write(shared / "public/llm/claude/common/hooks/a.sh", "a")
    _write(shared / "public/llm/claude/common/hooks/b.sh", "b")
    recipe_rel = ".shared-llm/public/compose/hooks/common/two.yaml"
    _write(
        shared.parent / recipe_rel,
        yaml.safe_dump(
            {
                "type": "copy",
                "inputs": [
                    ".shared-llm/public/llm/claude/common/hooks/a.sh",
                    ".shared-llm/public/llm/claude/common/hooks/b.sh",
                ],
                "output": ".claude/hooks/x.sh",
            },
            sort_keys=False,
        ),
    )
    stderr = _compose_expect_fail(shared, tmp_path, recipe_rel)
    assert "exactly one input" in stderr


def _load_harness_module():
    spec = importlib.util.spec_from_file_location("harness_under_test", COMPOSER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_common_pi_skills_takes_do_star_from_pi_dir_and_common_not_cc(
    tmp_path: Path,
) -> None:
    """Pi links every do-* (from the routed .pi-skills dir) plus common skills from
    .claude/skills, but never cc-* (Claude-only)."""
    harness = _load_harness_module()
    root = tmp_path / "repo"
    # do-* live in the Pi-only routed dir after compose
    _write(
        root / ".pi-skills/do-plan-and-grill/SKILL.md",
        "---\nname: do-plan-and-grill\n---\n",
    )
    # common + cc-* stay in .claude/skills
    for skill in ("qa", "cc-plan-and-grill"):
        _write(
            root / ".claude/skills" / skill / "SKILL.md", f"---\nname: {skill}\n---\n"
        )
    _write(
        root / ".shared-llm/public/compose/slash-commands/common/common/qa.yaml",
        "name: qa\n",
    )
    _write(
        root
        / ".shared-llm/public/compose/slash-commands/common/claude/cc-plan-and-grill.yaml",
        "name: cc-plan-and-grill\n",
    )

    got = harness._common_pi_skills(root)
    assert (
        got["do-plan-and-grill"] == root / ".pi-skills/do-plan-and-grill"
    )  # Pi-only source
    assert got["qa"] == root / ".claude/skills/qa"  # common
    assert "cc-plan-and-grill" not in got  # Claude-only


def test_common_codex_skills_includes_common_and_codex(tmp_path: Path) -> None:
    harness = _load_harness_module()
    root = tmp_path / "repo"
    for skill in ("qa", "cc-plan-and-grill"):
        _write(
            root / ".claude/skills" / skill / "SKILL.md", f"---\nname: {skill}\n---\n"
        )
    _write(
        root / ".shared-llm/public/compose/slash-commands/common/common/qa.yaml",
        "name: qa\n",
    )
    _write(
        root
        / ".shared-llm/public/compose/slash-commands/common/claude/cc-plan-and-grill.yaml",
        "name: cc-plan-and-grill\n",
    )

    got = harness._common_codex_skills(root)
    assert got["qa"] == root / ".claude/skills/qa"
    assert "cc-plan-and-grill" not in got


def test_planish_visual_contract_is_referenced_and_runtime_exposes_visual_fields() -> (
    None
):
    contract = (
        REPO_ROOT
        / ".shared-llm/public/llm/common/common/planish-html-grill-contract.md"
    )
    assert contract.exists()
    for rel in (
        ".shared-llm/public/layers/slash-commands/common/common/plan-core.md",
        ".shared-llm/public/layers/slash-commands/common/claude/cc-plan/command.md",
        ".shared-llm/public/layers/slash-commands/common/common/do-plan/command.md",
    ):
        text = (REPO_ROOT / rel).read_text()
        if rel.endswith("plan-core.md"):
            assert "planish-html-grill-contract.md" in text
            assert "plain chat questionnaire" in text

    planish_ts = (
        REPO_ROOT / ".shared-llm/public/llm/pi/common/extensions/do-planish.ts"
    ).read_text()
    for token in (
        "contextHtml",
        "mermaid",
        "ascii",
        "visualHtml",
        "+ Note",
        "Copy Feedback",
    ):
        assert token in planish_ts
    assert "sendUserMessage(`/do-plan ${trimmed}`.trim())" in planish_ts
    assert "prompt: `/do-plan" not in planish_ts

    # do-planish stays an extension-only alias (no compose recipe). cc-planish is
    # a one-release Claude Code alias skill.
    assert not (
        REPO_ROOT
        / ".shared-llm/public/compose/slash-commands/common/common/do-planish.yaml"
    ).exists()
    assert (
        REPO_ROOT
        / ".shared-llm/public/compose/slash-commands/common/claude/cc-planish.yaml"
    ).exists()
    assert (
        REPO_ROOT
        / ".shared-llm/public/layers/slash-commands/common/claude/cc-planish/command.md"
    ).exists()
    assert (
        "Deprecated one-release alias"
        in (
            REPO_ROOT
            / ".shared-llm/public/layers/slash-commands/common/claude/cc-planish/command.md"
        ).read_text()
    )


def test_do_planish_alias_sends_a_real_pi_user_message() -> None:
    extension = REPO_ROOT / ".shared-llm/public/llm/pi/common/extensions/do-planish.ts"
    script = f"""
import extension from {json.dumps(extension.as_uri())};
let alias;
const sent = [];
const api = {{
  on() {{}}, registerTool() {{}},
  registerCommand(name, options) {{ if (name === 'do-planish') alias = options; }},
  sendUserMessage(message) {{ sent.push(message); }},
}};
extension(api);
await alias.handler('topic', {{ ui: {{ notify() {{}} }} }});
if (sent.length !== 1 || sent[0] !== '/do-plan topic') throw new Error(JSON.stringify(sent));
"""
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_grill_feedback_is_annotation_only() -> None:
    """One feedback transport everywhere: sticky notes -> Copy Feedback -> paste back.
    No page-level answer boxes, no Submit/Approve buttons, no browser->agent POST."""
    # Pi runtime serves its pages and returns immediately — no blocking POST plumbing.
    planish_ts = (
        REPO_ROOT / ".shared-llm/public/llm/pi/common/extensions/do-planish.ts"
    ).read_text()
    for banned in (
        "Copy Answers",
        "Submit Answers",
        "grill-respond",
        "/respond",
        "Request Changes",
        "pendingResolve",
        "grill-a",
    ):
        assert banned not in planish_ts, f"do-planish.ts must not contain {banned!r}"

    # Grill-authoring commands: notes are the only answer channel.
    for rel in (".shared-llm/public/layers/slash-commands/common/common/plan-core.md",):
        text = (REPO_ROOT / rel).read_text()
        assert "Copy Feedback" in text, rel
        for banned in ("Copy Answers", "Submit Answers", "textarea", "grill-a"):
            assert banned not in text, f"{rel} must not contain {banned!r}"

    # Toolkits: the form toolkit is style-only; the annotation toolkit is the
    # single interactive surface, with anchor-tagged Copy Feedback.
    form = (
        REPO_ROOT / ".shared-llm/public/llm/common/common/toolkits/form-toolkit.html"
    ).read_text()
    for banned in ("<script", "<button", "textarea", "Copy Answers"):
        assert banned not in form, f"form-toolkit.html must not contain {banned!r}"
    ann = (
        REPO_ROOT
        / ".shared-llm/public/llm/common/common/toolkits/annotation-toolkit.html"
    ).read_text()
    for token in ("+ Note", "Copy Feedback", "desdoc-key", "ddAnchor"):
        assert token in ann, f"annotation-toolkit.html must contain {token!r}"

    # The shared contract spells out the single transport.
    contract = (
        REPO_ROOT
        / ".shared-llm/public/llm/common/common/planish-html-grill-contract.md"
    ).read_text()
    for token in ("One feedback transport", "Copy Feedback", "No answer boxes"):
        assert token in contract, f"contract must contain {token!r}"


def test_plan_versioning_downloadable_and_host() -> None:
    """Plans freeze plan-v<k> history (never revised in place), pages are also
    offered as downloadable files where the harness can send them, and URLs
    honor the .planish.yaml host: field for remote (Tailscale) sessions."""
    planish_ts = (
        REPO_ROOT / ".shared-llm/public/llm/pi/common/extensions/do-planish.ts"
    ).read_text()
    for token in ("plan-v<k>", "resolveHost", "PLANISH_HOST", "0.0.0.0"):
        assert token in planish_ts, f"do-planish.ts must contain {token!r}"
    assert 'listen(PORT, "127.0.0.1"' not in planish_ts  # bind follows the host

    # plan-v<k> freezing is tool-enforced (auto-freeze in planish_submit_plan), not
    # left to prompt prose: the submit tool snapshots the next frozen pair itself.
    for token in ("autoFreezePlan", "newestFrozenVersion", "planish_submit_plan"):
        assert token in planish_ts, f"do-planish.ts must wire auto-freeze via {token!r}"

    # tf-implement's duplicated resolver must stay in sync: same config file.
    tf_ts = (
        REPO_ROOT / ".shared-llm/public/llm/pi/common/extensions/tf-implement.ts"
    ).read_text()
    assert ".planish.yaml" in tf_ts, "tf-implement resolver must read .planish.yaml"
    assert ".planish.json" not in tf_ts, "drifted .planish.json reference"
    assert "plan-v<k>" in tf_ts

    example = (REPO_ROOT / ".planish.yaml.example").read_text()
    assert "host:" in example, ".planish.yaml.example must document host:"

    text = (
        REPO_ROOT
        / ".shared-llm/public/layers/slash-commands/common/common/plan-core.md"
    ).read_text()
    for token in (
        "plan-candidate-vN.md",
        "plan-vN.md",
        "never rewrite",
        "Planish",
        "final human approval",
        "--dir <path>",
        "$PLANISH_DIR",
        ".planish.yaml",
        "/tmp/planish/{date}/{slug}",
        "`host:`",
        "downloadable",
    ):
        assert token in text, f"plan-core.md must contain {token!r}"

    contract = (
        REPO_ROOT
        / ".shared-llm/public/llm/common/common/planish-html-grill-contract.md"
    ).read_text()
    for token in ("Versioned history", "downloadable", "host:"):
        assert token in contract, f"contract must contain {token!r}"


def _agent_fixture(
    tmp_path: Path, name: str, *, model: str | None
) -> tuple[Path, Path, str]:
    """Build a .shared-llm fixture with one agent recipe; include `model:` only when given."""
    shared = tmp_path / ".shared-llm"
    layer_dir = shared / "public/layers/agents/common" / name
    _write(layer_dir / "body.md", f"# {name}\n\nAgent body.\n")
    _write(layer_dir / "description.md", "A test agent.\n")

    recipe: dict = {
        "type": "agent",
        "name": name,
        "color": "red",
        "description": f".shared-llm/public/layers/agents/common/{name}/description.md",
    }
    if model is not None:
        recipe["model"] = model
    recipe["inputs"] = [f".shared-llm/public/layers/agents/common/{name}/body.md"]
    recipe["output"] = f".claude/agents/{name}.md"

    recipe_rel = f".shared-llm/public/compose/agents/{name}.yaml"
    _write(
        shared.parent / recipe_rel,
        yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True),
    )
    return shared, tmp_path, recipe_rel


def test_agent_recipe_without_model_omits_model_frontmatter(tmp_path: Path) -> None:
    """An agent recipe with no `model:` composes cleanly and emits no `model:` line —
    so the agent's LLM is chosen per run (e.g. by a route profile), never hardwired.
    This guards the ClaudeAgent optional-`model:` branch (adversarial-evaluator)."""
    shared, target, recipe = _agent_fixture(tmp_path, "no-model-agent", model=None)
    _compose(shared, target, recipe)
    text = (target / ".claude/agents/no-model-agent.md").read_text()
    fm, _ = _split(text)
    assert "model" not in fm, f"unexpected model in frontmatter: {fm}"
    assert not any(line.startswith("model:") for line in text.splitlines())
    # color still passes through when the recipe declares it
    assert fm["color"] == "red"


def test_agent_recipe_with_model_emits_model_frontmatter(tmp_path: Path) -> None:
    """The other direction: a recipe that declares `model:` still emits it verbatim."""
    shared, target, recipe = _agent_fixture(tmp_path, "pinned-agent", model="opus")
    _compose(shared, target, recipe)
    fm, _ = _split((target / ".claude/agents/pinned-agent.md").read_text())
    assert fm["model"] == "opus"
    assert fm["color"] == "red"


def test_settings_deep_merge(tmp_path: Path) -> None:
    shared = tmp_path / ".shared-llm"
    base = {
        "permissions": {"defaultMode": "acceptEdits"},
        "hooks": {"PostToolUse": [{"matcher": "Edit"}]},
        "effortLevel": "low",
    }
    overlay = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash"}],
            "PostToolUse": [{"matcher": "Write"}],
        },
        "effortLevel": "max",
    }
    _write(shared / "public/llm/claude/common/settings.json", json.dumps(base))
    _write(shared / "public/llm/claude/this_repo/settings.json", json.dumps(overlay))
    recipe_rel = ".shared-llm/public/compose/settings/settings.yaml"
    _write(
        shared.parent / recipe_rel,
        yaml.safe_dump(
            {
                "type": "settings",
                "inputs": [
                    ".shared-llm/public/llm/claude/common/settings.json",
                    ".shared-llm/public/llm/claude/this_repo/settings.json",
                ],
                "output": ".claude/settings.json",
            },
            sort_keys=False,
        ),
    )
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
