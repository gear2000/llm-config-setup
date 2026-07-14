"""Static contracts for the planning-to-Herdr command surface."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / ".shared-llm/public"
RECIPES = PUBLIC / "compose/slash-commands"
LAYERS = PUBLIC / "layers/slash-commands"

RETAINED_PLANNERS = {
    "cc-plan-and-grill": LAYERS / "common/claude/cc-plan-and-grill/command.md",
    "cc-full": LAYERS / "common/claude/cc-full/command.md",
    "cc-planish": LAYERS / "common/claude/cc-planish/command.md",
    "do-plan-and-grill": LAYERS / "common/common/do-plan-and-grill/command.md",
    "do-full": LAYERS / "common/common/do-full/command.md",
    "meta-cc-plan-and-grill": LAYERS
    / "common/claude/meta-cc-plan-and-grill/command.md",
}

HANDOFF = "/herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>"
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
DIRECT_EXECUTORS = {"cc-implement", "cc-oneshot", "do-implement", "do-oneshot"}
RETIRED_RUNNER_NAMES = ("Meta-CC", "Meta-ORCH/Pi", "Meta-Herdr")
META_PLAN_FORMAT = PUBLIC / "llm/pi/common/meta-plan/meta-plan-format.md"
HERDR_SKILLS = (
    LAYERS / "common/common/herdr-run/command.md",
    LAYERS / "common/common/herdr-phase/command.md",
)
ROOT_JUSTFILE = ROOT / "justfile"
ADVERSARIAL_EVALUATOR_RECIPE = (
    PUBLIC / "compose/agents/adversarial-evaluator.yaml"
)
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


def test_retained_planners_finish_with_checked_herdr_input() -> None:
    for name, path in RETAINED_PLANNERS.items():
        text = path.read_text()
        assert "plan.md" in text, name
        assert "route.yaml" in text, name
        assert "/meta-plan-check" in text, name
        assert HANDOFF in text, name
        for forbidden in FORBIDDEN:
            assert forbidden not in text, f"{name} still references {forbidden}"


def _assert_no_retired_execution_instructions(path: Path, text: str) -> None:
    for instruction in RETIRED_EXECUTION_INSTRUCTIONS:
        assert instruction not in text, f"{path} still instructs {instruction}"


def test_active_planner_herdr_and_root_just_surfaces_have_no_old_runner_instructions() -> None:
    """The active planner, Herdr, and root command surfaces cannot resurrect a
    retired runner. Agent outputs get their own compose test below because the
    description is frontmatter generated from the public source layer."""
    for path in (*RETAINED_PLANNERS.values(), *HERDR_SKILLS, ROOT_JUSTFILE):
        _assert_no_retired_execution_instructions(path, path.read_text())


def test_recipe_inventory_keeps_only_planners_and_herdr_execution() -> None:
    recipe_paths = list(RECIPES.rglob("*.yaml"))
    recipes = {path.stem for path in recipe_paths}
    cc_commands = {path.stem for path in RECIPES.glob("common/claude/cc-*.yaml")}
    do_commands = {path.stem for path in RECIPES.glob("common/common/do-*.yaml")}

    assert cc_commands == {"cc-full", "cc-plan-and-grill", "cc-planish", "cc-research"}
    assert do_commands == {"do-full", "do-plan-and-grill", "do-research"}
    assert {"meta-cc-plan-and-grill", "meta-plan-check", "meta-plan-convert"} <= recipes
    assert {"herdr-run", "herdr-phase"} <= recipes
    assert not DIRECT_EXECUTORS & recipes
    assert not any(name.startswith("rphase-") for name in recipes)
    assert "cc-loop" not in recipes
    assert "do-loop" not in recipes


def test_pi_can_discover_portable_meta_plan_helpers() -> None:
    for name in ("meta-plan-check", "meta-plan-convert"):
        recipe = RECIPES / f"common/pi/{name}.yaml"
        assert recipe.exists(), name
        assert "/common/pi/" in str(recipe)


def test_meta_plan_format_and_composed_pi_helpers_name_only_herdr_runner(
    tmp_path: Path,
) -> None:
    source = META_PLAN_FORMAT.read_text()
    for retired_name in RETIRED_RUNNER_NAMES:
        assert retired_name not in source, retired_name
    assert "Herdr is the sole active runner." in source

    for name in ("meta-plan-check", "meta-plan-convert"):
        recipe = f".shared-llm/public/compose/slash-commands/common/pi/{name}.yaml"
        result = subprocess.run(
            [sys.executable, "tools/harness.py", "compose", recipe, "--target", str(tmp_path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        composed = (tmp_path / f".claude/skills/{name}/SKILL.md").read_text()
        for retired_name in RETIRED_RUNNER_NAMES:
            assert retired_name not in composed, f"{name}: {retired_name}"
        assert "Herdr is the sole active runner." in composed


def test_composed_adversarial_evaluator_has_no_old_runner_instructions(
    tmp_path: Path,
) -> None:
    """The public evaluator description is live Claude agent frontmatter, not
    historical documentation. Compose it before checking so the test catches
    changes to the description layer as users actually receive it."""
    result = subprocess.run(
        [
            sys.executable,
            "tools/harness.py",
            "compose",
            str(ADVERSARIAL_EVALUATOR_RECIPE.relative_to(ROOT)),
            "--target",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    artifact = tmp_path / ".claude/agents/adversarial-evaluator.md"
    _assert_no_retired_execution_instructions(artifact, artifact.read_text())
    assert "Herdr phase leader" in artifact.read_text()
    assert "UpAgent Recruiter" in artifact.read_text()


def test_active_agents_and_watchers_follow_the_herdr_dispatch_model(
    tmp_path: Path,
) -> None:
    """The live agent artifacts must preserve the sole execution path.

    This catches the stale-home failure mode: a generic agent can sound valid on
    its own while telling a live harness to bypass the TUI → phase leader →
    Recruiter → worker chain.
    """
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
        for removed in ("/run-phase", "/do-implement", "/do-oneshot"):
            assert removed not in artifact, f"{name} still directs {removed}"

    # The watcher is expressly mechanical, so it cannot become a second phase
    # evaluator or a covert worker while agents are idle.
    watcher = (tmp_path / ".claude/agents/team-pulse.md").read_text()
    for required in ("mechanical", "result_path", "Do not judge a result"):
        assert required in watcher


def test_shared_herdr_protocol_names_only_herdr_runners() -> None:
    protocol = (LAYERS / "common/common/meta-runner-phase-protocol.md").read_text()
    assert "transitional `/meta-*` runners" not in protocol


def test_public_route_guidance_uses_the_phase_leader_not_the_evaluator() -> None:
    for path in ROUTE_LEAD_EXAMPLES:
        text = path.read_text()
        assert "agent: herdr-phase-leader" in text, path

    evaluator = PHASE_EVALUATOR_SOURCE.read_text()
    assert "optional, independent **phase evaluator**" in evaluator
    assert "You do not move phase files, start another worker, or fix implementation." in evaluator
    assert "The phase leader alone makes the durable `phase-result.json` decision" in evaluator


def test_public_planning_layers_have_no_retired_execution_reference() -> None:
    for path in LAYERS.rglob("*.md"):
        text = path.read_text()
        for forbidden in FORBIDDEN:
            assert forbidden not in text, f"{path} still references {forbidden}"
