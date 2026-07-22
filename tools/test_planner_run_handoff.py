"""Static contracts for the planning/conversion/Herdr command surface."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / ".shared-llm/public"
RECIPES = PUBLIC / "compose/slash-commands"
LAYERS = PUBLIC / "layers/slash-commands"

PLAN_COMMANDS = {
    "cc-plan": LAYERS / "common/claude/cc-plan/command.md",
    "do-plan": LAYERS / "common/common/do-plan/command.md",
}
CONVERT_COMMANDS = {
    "cc-convert": LAYERS / "common/claude/cc-convert/command.md",
    "do-convert": LAYERS / "common/common/do-convert/command.md",
}
IMPLEMENT_COMMANDS = {
    "cc-implement": LAYERS / "common/claude/cc-implement/command.md",
    "do-implement": LAYERS / "common/common/do-implement/command.md",
}
FULL_COMMANDS = {
    "cc-full": LAYERS / "common/claude/cc-full/command.md",
    "do-full": LAYERS / "common/common/do-full/command.md",
}
ALIASES = {
    "cc-plan-and-grill": LAYERS / "common/claude/cc-plan-and-grill/command.md",
    "cc-planish": LAYERS / "common/claude/cc-planish/command.md",
    "do-plan-and-grill": LAYERS / "common/common/do-plan-and-grill/command.md",
    "meta-plan-check": LAYERS / "common/common/meta-plan-check/command.md",
    "meta-plan-convert": LAYERS / "common/common/meta-plan-convert/command.md",
}

FORBIDDEN = ("rphase-create", "rphase-run", "rphase-unblock", "cc-loop", "do-loop")
RETIRED_EXECUTION_INSTRUCTIONS = (
    "/run-phase",
    "/meta-autorun",
    "/meta-run",
    "/meta-herdr",
    "rphase-",
    "old Meta-orchestrator",
    "ask_brain",
)
RETIRED_RUNNER_NAMES = ("Meta-CC", "Meta-ORCH/Pi", "Meta-Herdr")
META_PLAN_FORMAT = PUBLIC / "llm/pi/common/meta-plan/meta-plan-format.md"
HERDR_SKILLS = (
    LAYERS / "common/common/tui-control/command.md",
    LAYERS / "common/common/phase-leader/command.md",
)
ROOT_JUSTFILE = ROOT / "justfile"
ADVERSARIAL_EVALUATOR_RECIPE = PUBLIC / "compose/agents/adversarial-evaluator.yaml"
PLAN_ADVERSARY_RECIPE = PUBLIC / "compose/agents/plan-adversary.yaml"
ACTIVE_AGENT_RECIPES = {
    "adversarial-evaluator": PUBLIC / "compose/agents/adversarial-evaluator.yaml",
    "phase-evaluator": PUBLIC / "compose/agents/phase-evaluator.yaml",
}
WATCHER_RECIPES = {
    "plan-watchdog": PUBLIC / "compose/agents/plan-watchdog.yaml",
    "team-pulse": PUBLIC / "compose/agents/team-pulse.yaml",
}
ROUTE_LEAD_EXAMPLES = (
    PUBLIC / "extensions/common/upagent/examples/route.yaml",
    PUBLIC / "llm/pi/common/meta-plan/fixtures/route.yaml",
    PUBLIC / "llm/pi/common/meta-plan/meta-plan-format.md",
    LAYERS / "common/common/meta-runner-phase-protocol.md",
)
PHASE_EVALUATOR_SOURCE = PUBLIC / "layers/agents/common/phase-evaluator.md"


def test_plan_commands_own_grill_design_and_two_round_plan_adversary() -> None:
    for name, path in PLAN_COMMANDS.items():
        text = path.read_text() + "\n" + (LAYERS / "common/common/plan-core.md").read_text()
        assert "Planish" in text, name
        assert "conditionally" in text or "conditional design" in text, name
        assert "--adversarial-iterations N" in text, name
        assert "defaults to `2`" in text, name
        assert "N=0" in text, name
        assert "Cap unattended review at `4`" in text, name
        assert "plan-adversary" in text, name
        assert "human-decision-required" in text, name
        assert "plan-candidate-vN.md" in text, name
        assert "final human approval" in text, name
        assert "Do not create `route.yaml`" in text, name


def test_convert_commands_share_herdr_core_and_design_required_contract() -> None:
    core = (LAYERS / "common/common/plan-conversion-contract.md").read_text()
    for name, path in CONVERT_COMMANDS.items():
        recipe = (RECIPES / f"common/{'claude' if name.startswith('cc-') else 'common'}/{name}.yaml").read_text()
        text = path.read_text() + "\n" + core
        assert "plan-conversion-contract.md" in recipe, name
        assert "--herdr" in text, name
        assert "idempotent" in text, name
        assert "DESIGN_REQUIRED" in text, name
        assert "conversion-review.html" in text, name
        assert "validate" in text.lower(), name
        assert "exact-SHA shared environment check" in text, name
        assert "not-configured" in text, name
        assert "Do not invent private infrastructure" in text, name


def test_direct_implement_commands_do_not_decompose_for_herdr() -> None:
    core = (LAYERS / "common/common/implement-core.md").read_text()
    for name, path in IMPLEMENT_COMMANDS.items():
        text = path.read_text() + "\n" + core
        assert "approved" in text, name
        assert "Do not decompose it for Herdr" in text, name
        assert "Do not create `route.yaml`" in text, name
        assert "Do not call `/cc-convert`" in text, name


def test_full_commands_call_each_primitive_exactly_once() -> None:
    expectations = {
        "cc-full": ("/cc-plan", "/cc-implement", "/cc-convert --herdr"),
        "do-full": ("/do-plan", "/do-implement", "/do-convert --herdr"),
    }
    for name, path in FULL_COMMANDS.items():
        text = path.read_text()
        for token in expectations[name]:
            assert text.count(token) == 1, f"{name} should mention {token} exactly once"
        assert text.count("just run-start") == 1, name
        assert "With neither flag, prompt the human once" in text, name
        assert "Do not run a standalone check command" in text, name
        assert "DESIGN_REQUIRED" in text, name


def test_aliases_are_warning_delegate_only() -> None:
    for name, path in ALIASES.items():
        text = path.read_text()
        assert "WARNING:" in text, name
        assert "alias" in text.lower() or "Deprecated" in text or "deprecated" in text, name
        assert "Do not" in text, name
        assert "route.todo.yaml" not in text, name
    assert "/cc-plan <same arguments>" in ALIASES["cc-plan-and-grill"].read_text()
    assert "/do-plan <same arguments>" in ALIASES["do-plan-and-grill"].read_text()
    assert "/cc-convert --herdr" in ALIASES["meta-plan-convert"].read_text()
    assert "/do-convert --herdr" in ALIASES["meta-plan-convert"].read_text()


def _assert_no_retired_execution_instructions(path: Path, text: str) -> None:
    for instruction in RETIRED_EXECUTION_INSTRUCTIONS:
        assert instruction not in text, f"{path} still instructs {instruction}"


def test_active_surfaces_have_no_old_runner_instructions() -> None:
    for path in (
        *PLAN_COMMANDS.values(),
        *CONVERT_COMMANDS.values(),
        *IMPLEMENT_COMMANDS.values(),
        *FULL_COMMANDS.values(),
        *HERDR_SKILLS,
        ROOT_JUSTFILE,
    ):
        _assert_no_retired_execution_instructions(path, path.read_text())


def test_recipe_inventory_has_new_surface_and_retired_meta_wrapper_removed() -> None:
    recipe_paths = list(RECIPES.rglob("*.yaml"))
    recipes = {path.stem for path in recipe_paths}
    cc_commands = {path.stem for path in RECIPES.glob("common/claude/cc-*.yaml")}
    do_commands = {path.stem for path in RECIPES.glob("common/common/do-*.yaml")}

    assert cc_commands == {
        "cc-convert",
        "cc-full",
        "cc-implement",
        "cc-plan",
        "cc-plan-and-grill",
        "cc-planish",
        "cc-research",
    }
    assert do_commands == {
        "do-convert",
        "do-full",
        "do-implement",
        "do-plan",
        "do-plan-and-grill",
        "do-research",
    }
    assert {"meta-plan-check", "meta-plan-convert"} <= recipes
    assert {"tui-control", "phase-leader"} <= recipes
    assert "herdr-run" not in recipes
    assert "meta-cc-plan-and-grill" not in recipes
    assert not any(name.startswith("rphase-") for name in recipes)


def test_meta_plan_format_names_new_converter_and_controller() -> None:
    source = META_PLAN_FORMAT.read_text()
    for retired_name in RETIRED_RUNNER_NAMES:
        assert retired_name not in source, retired_name
    assert "The TUI controller is the sole active runner; Herdr supplies its pane transport." in source
    assert "cc/do-convert --herdr" in source
    assert "/tui-control" in source
    assert "just run-start" in source
    assert "/meta-plan-check" not in source
    assert "/meta-plan-convert" not in source
    assert "/herdr-run" not in source


def test_composed_plan_adversary_is_separate_from_code_adversary(tmp_path: Path) -> None:
    for recipe, name in (
        (PLAN_ADVERSARY_RECIPE, "plan-adversary"),
        (ADVERSARIAL_EVALUATOR_RECIPE, "adversarial-evaluator"),
    ):
        result = subprocess.run(
            [
                sys.executable,
                "tools/harness.py",
                "compose",
                str(recipe.relative_to(ROOT)),
                "--target",
                str(tmp_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"
    plan = (tmp_path / ".claude/agents/plan-adversary.md").read_text()
    code = (tmp_path / ".claude/agents/adversarial-evaluator.md").read_text()
    assert "candidate implementation plans" in plan
    assert "Do not suggest implementation patches" in plan
    assert "Do not reuse the code-focused adversarial-evaluator framing" in plan
    assert "phase leader" in code
    assert "candidate implementation plans" not in code


def test_active_agents_and_watchers_follow_the_herdr_dispatch_model(
    tmp_path: Path,
) -> None:
    for name, recipe in {**ACTIVE_AGENT_RECIPES, **WATCHER_RECIPES}.items():
        result = subprocess.run(
            [
                sys.executable,
                "tools/harness.py",
                "compose",
                str(recipe.relative_to(ROOT)),
                "--target",
                str(tmp_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"
        artifact = (tmp_path / f".claude/agents/{name}.md").read_text()
        for required in ("TUI agent", "phase leader", "UpAgent Recruiter"):
            assert required in artifact, f"{name} missing {required!r}"
        for removed in ("/run-phase", "/do-oneshot"):
            assert removed not in artifact, f"{name} still directs {removed}"

    watcher = (tmp_path / ".claude/agents/team-pulse.md").read_text()
    for required in ("mechanical", "result_path", "Do not judge a result"):
        assert required in watcher


def test_shared_herdr_protocol_names_new_controller() -> None:
    protocol = (LAYERS / "common/common/meta-runner-phase-protocol.md").read_text()
    assert "transitional `/meta-*` runners" not in protocol
    assert "/tui-control" in protocol


def test_public_route_guidance_uses_the_phase_leader_not_the_evaluator() -> None:
    for path in ROUTE_LEAD_EXAMPLES:
        text = path.read_text()
        assert "agent: phase-leader" in text, path

    evaluator = PHASE_EVALUATOR_SOURCE.read_text()
    assert "optional, independent **phase evaluator**" in evaluator
    assert "You do not move phase files, start another worker, or fix implementation." in evaluator
    assert "The phase leader alone makes the durable `phase-result.json` decision" in evaluator


def test_public_planning_layers_have_no_retired_execution_reference() -> None:
    for path in LAYERS.rglob("*.md"):
        text = path.read_text()
        for forbidden in FORBIDDEN:
            assert forbidden not in text, f"{path} still references {forbidden}"
