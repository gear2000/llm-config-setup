"""Regression coverage for the generated planner-to-Herdr handoff surface.

This deliberately exercises the configured-destination policy instead of testing
only source layers: public content is copied through the hub, a repository
overlay is composed last, Pi-only ``do-*`` skills are routed, and the global Pi
links are reconciled into an isolated HOME.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
HARNESS = TOOLS / "harness.py"
HANDOFF = "/herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>"

PLANNERS = {
    "cc-full": ".claude/skills/cc-full/SKILL.md",
    "cc-plan-and-grill": ".claude/skills/cc-plan-and-grill/SKILL.md",
    "cc-planish": ".claude/skills/cc-planish/SKILL.md",
    "meta-cc-plan-and-grill": ".claude/skills/meta-cc-plan-and-grill/SKILL.md",
    "do-full": ".pi-skills/do-full/SKILL.md",
    "do-plan-and-grill": ".pi-skills/do-plan-and-grill/SKILL.md",
}
PI_REQUIRED_SKILLS = {
    "do-full": ".pi-skills/do-full",
    "do-plan-and-grill": ".pi-skills/do-plan-and-grill",
    "herdr-phase": ".claude/skills/herdr-phase",
    "herdr-run": ".claude/skills/herdr-run",
    "meta-plan-check": ".claude/skills/meta-plan-check",
    "meta-plan-convert": ".claude/skills/meta-plan-convert",
}
FORBIDDEN_SKILL_NAMES = {
    "cc-implement",
    "cc-loop",
    "cc-oneshot",
    "do-implement",
    "do-loop",
    "do-oneshot",
    "meta-auto-run",
    "meta-autorun",
    "meta-cc",
    "meta-connect",
    "meta-herdr",
    "meta-herdr-phase",
    "meta-response",
    "meta-run",
    "rphase-create",
    "rphase-run",
    "rphase-unblock",
    "run-phase",
    "run_phase",
    "team",
}
FORBIDDEN_TEXT = (
    "/cc-implement",
    "/cc-oneshot",
    "/do-implement",
    "/do-oneshot",
    "/meta-auto-run",
    "/meta-autorun",
    "/meta-connect",
    "/meta-herdr",
    "/meta-response",
    "/meta-run",
    "/run-phase",
    "Meta-CC",
    "Meta-Herdr",
    "Meta-ORCH/Pi",
    "rphase",
    "worker-up",
)


def _load_harness():
    spec = importlib.util.spec_from_file_location("planner_herdr_harness", HARNESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _patch_home(harness, home: Path) -> None:
    home.mkdir(parents=True)
    harness.HOME = home
    harness.CONFIG_PATH = home / ".shared-llm.yaml"
    harness.DEFAULT_SOURCE = home / ".shared-llm"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _add_repository_overlay(destination: Path) -> None:
    """Compose one repository-owned agent after the public recipe set.

    The normal policy composes public recipes first and a destination's
    ``this_repo`` recipes second. Keeping this overlay in the fixture proves the
    generated surfaces below use that policy without relying on a real project.
    """
    overlay = destination / ".shared-llm/this_repo"
    _write(
        overlay / "layers/agents/this_repo/plan-watchdog.description.md",
        "Repository overlay watchdog.\n",
    )
    _write(
        overlay / "layers/agents/this_repo/plan-watchdog.md",
        "Repository-specific reporting stays with the Herdr TUI agent.\n",
    )
    _write(
        overlay / "compose/agents/plan-watchdog.yaml",
        "\n".join(
            (
                "type: agent",
                "name: plan-watchdog",
                "description: .shared-llm/this_repo/layers/agents/this_repo/plan-watchdog.description.md",
                "inputs:",
                "  - .shared-llm/public/layers/agents/common/plan-watchdog.md",
                "  - .shared-llm/this_repo/layers/agents/this_repo/plan-watchdog.md",
                "output: .claude/agents/plan-watchdog.md",
                "",
            )
        ),
    )


def _active_artifacts(destination: Path) -> list[Path]:
    paths = [
        *sorted((destination / ".claude/skills").glob("*/SKILL.md")),
        *sorted((destination / ".pi-skills").glob("*/SKILL.md")),
        *sorted((destination / ".claude/agents").glob("*.md")),
    ]
    assert paths, "compose produced no active skills or agents"
    return paths


def test_generated_planner_handoff_and_pi_link_policy(tmp_path: Path) -> None:
    harness = _load_harness()
    home = tmp_path / "home"
    destination = tmp_path / "destination"
    _patch_home(harness, home)
    _add_repository_overlay(destination)
    config = {
        "source": str(harness.DEFAULT_SOURCE),
        "global": [],
        "destinations": [
            {
                "path": str(destination),
                "harnesses": ["cc", "pi"],
                "placeholders": {"OPS_REPO": "fixture-ops", "PRIVATE_REPO": "fixture"},
            }
        ],
    }
    log = harness.RunLog(verbose=False)

    # This is the normal consumer path, not a direct single-recipe compose.
    harness.do_copy(config, log)
    harness.do_compose(config, log)
    harness.do_link(config, log)

    for name, relative in PLANNERS.items():
        text = (destination / relative).read_text()
        for required in ("plan.md", "route.yaml", "/meta-plan-check", HANDOFF):
            assert required in text, f"{name} missing {required!r}"

    active = _active_artifacts(destination)
    active_names = {path.parent.name for path in active if path.name == "SKILL.md"}
    assert not FORBIDDEN_SKILL_NAMES & active_names
    for artifact in active:
        text = artifact.read_text()
        for forbidden in FORBIDDEN_TEXT:
            assert forbidden not in text, f"{artifact} still contains {forbidden!r}"
    assert "Repository-specific reporting" in (
        destination / ".claude/agents/plan-watchdog.md"
    ).read_text()

    for name, relative in PI_REQUIRED_SKILLS.items():
        link = home / ".pi/agent/skills" / name
        assert link.is_symlink(), f"Pi does not expose {name}"
        assert link.resolve() == (destination / relative).resolve()

    root_justfile = (ROOT / "justfile").read_text()
    assert "worker-up:" not in root_justfile
    assert "worker-up " not in root_justfile
