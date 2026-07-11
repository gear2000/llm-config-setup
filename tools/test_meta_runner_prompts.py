"""Prompt contract tests for the meta-runner phase protocol.

These are doc-contract tripwires, not behavioral tests: the Stage 2 "unused
intake" gate is prose executed by an LLM auditor, so there is no runtime path to
exercise. What these tests DO enforce is that the rule is present in every stage
document that references Stage 2, that the wording stays consistent across those
copies, and — critically — that the generated agent artifact stays in sync with
its source layer (the failure mode of hand-editing a compose output).
"""

from __future__ import annotations
from pathlib import Path


# tools/ -> repo root. This file lives directly under tools/, so one parent up
# from the file's dir reaches the repo root that owns .shared-llm/.
REPO_ROOT = Path(__file__).resolve().parent.parent

LAYERS = REPO_ROOT / ".shared-llm/public/layers"
PI_HUB = REPO_ROOT / ".shared-llm/public/llm/pi/common/extensions/meta-orchestrator-hub"

PHASE_PROTOCOL = LAYERS / "slash-commands/common/common/meta-runner-phase-protocol.md"
HANDOFF_PROTOCOL = LAYERS / "slash-commands/common/common/meta-runner-handoff-protocol.md"
COMPOSE_SLASH_COMMANDS = REPO_ROOT / ".shared-llm/public/compose/slash-commands"
META_HERDR_PHASE = LAYERS / "slash-commands/common/common/meta-herdr-phase/command.md"
META_HERDR = LAYERS / "slash-commands/common/common/meta-herdr/command.md"
RUN_PHASE = LAYERS / "slash-commands/common/claude/run-phase/command.md"
RUN_PHASE_FILE = LAYERS / "slash-commands/common/claude/run-phase-file/command.md"
ADVERSARIAL_EVALUATOR_SRC = LAYERS / "agents/common/adversarial-evaluator.md"
ADVERSARIAL_EVALUATOR_ARTIFACT = REPO_ROOT / ".claude/agents/adversarial-evaluator.md"
BRAIN_EXECUTE_PLAN = PI_HUB / "brain-execute-plan-prompt.md"
META_PLAN_FORMAT = PI_HUB / "meta-plan-format.md"

# The canonical phrase every Stage 2 reference must use verbatim.
GATE_PHRASE = "unused intake / accepted-but-ignored inputs"

# Every document that describes or references the Stage 2 audit must name the gate.
STAGE2_REFERENCING_DOCS = [
    PHASE_PROTOCOL,
    META_HERDR_PHASE,
    META_HERDR,
    RUN_PHASE,
    RUN_PHASE_FILE,
    ADVERSARIAL_EVALUATOR_SRC,
    BRAIN_EXECUTE_PLAN,
    META_PLAN_FORMAT,
]


def test_gate_phrase_present_in_every_stage2_reference() -> None:
    # Case-insensitive: the evaluator persona writes it as a bold bullet heading
    # ("Unused intake ..."); the stage docs use it mid-sentence in lower case.
    for doc in STAGE2_REFERENCING_DOCS:
        assert GATE_PHRASE in doc.read_text().lower(), f"missing gate phrase in {doc}"


def test_stage2_audit_has_unused_intake_hard_gate() -> None:
    text = PHASE_PROTOCOL.read_text()

    assert GATE_PHRASE in text
    assert "Every newly accepted input must affect validation" in text
    assert "Do not allow hardcoding, bypassing, stubbing, or fake intake" in text
    assert "`VERIFICATION_PASSED` advances to Stage 3" in text


def test_stage2_audit_requires_multi_angle_detection() -> None:
    text = PHASE_PROTOCOL.read_text()

    assert "Use a multi-angle audit" in text
    assert "AST-aware inspection" in text
    assert "lint, type, and static-analysis signals" in text
    assert "public interfaces and call-sites" in text
    assert "semantically inspect tests and implementation" in text


def test_meta_herdr_stage2_report_requires_evidence_for_ignored_inputs() -> None:
    text = META_HERDR_PHASE.read_text()

    assert GATE_PHRASE in text
    assert "name the ignored input" in text
    assert "where it" in text and "accepted" in text
    assert "expected behavioral role" in text
    assert "affected public surface/call-site" in text


def test_adversarial_evaluator_source_hunts_accepted_but_ignored_inputs() -> None:
    text = ADVERSARIAL_EVALUATOR_SRC.read_text()

    assert "Unused intake / accepted-but-ignored inputs" in text
    assert "Use AST-aware inspection" in text
    assert "remove the intake or wire it into real behavior" in text


def test_adversarial_evaluator_artifact_in_sync_with_source() -> None:
    """The .claude/agents artifact is a compose output of the source layer.

    It must never be hand-edited: the source layer's unused-intake block has to
    appear verbatim in the generated artifact. If this fails, re-run
    `python3 tools/harness.py compose .shared-llm/public/compose/agents/adversarial-evaluator.yaml`
    instead of editing the artifact by hand.
    """
    src = ADVERSARIAL_EVALUATOR_SRC.read_text()
    artifact = ADVERSARIAL_EVALUATOR_ARTIFACT.read_text()

    # Pull the unused-intake bullet out of the source (from its bold lead-in to
    # the blank line that ends the bullet) and require it verbatim in the output.
    marker = "- **Unused intake / accepted-but-ignored inputs.**"
    assert marker in src
    block = src[src.index(marker) :].split("\n\n", 1)[0]
    assert block in artifact, "generated artifact is out of sync with its source layer"


def test_handoff_protocol_states_the_contract() -> None:
    text = HANDOFF_PROTOCOL.read_text()

    assert ".meta/handoffs/<phase-id>/<role>-vN.md" in text
    assert "writes a short handoff before it returns" in text
    assert "Lead Agent reads the relevant handoffs" in text


def test_handoff_protocol_wired_wherever_phase_protocol_is() -> None:
    """The handoff protocol is a sibling contract to the phase protocol: every
    slash-command recipe that composes the phase protocol must compose the
    handoff protocol too, and at least one recipe must consume it at all (an
    orphaned layer file silently drops out of every composed skill)."""
    recipes = sorted(COMPOSE_SLASH_COMMANDS.rglob("*.yaml"))
    assert recipes, f"no recipes found under {COMPOSE_SLASH_COMMANDS}"

    consumers = []
    for recipe in recipes:
        text = recipe.read_text()
        if "meta-runner-handoff-protocol.md" in text:
            consumers.append(recipe)
        if "meta-runner-phase-protocol.md" in text:
            assert "meta-runner-handoff-protocol.md" in text, (
                f"{recipe} includes the phase protocol but not the handoff protocol"
            )
    assert consumers, "meta-runner-handoff-protocol.md is orphaned — no recipe consumes it"


def test_intentional_unused_uses_straight_quotes_everywhere() -> None:
    """No curly quotes around the 'intentional unused' marker — they drift and
    are harder for downstream parsers to match than plain ASCII quotes."""
    for doc in STAGE2_REFERENCING_DOCS:
        text = doc.read_text()
        if "intentional unused" in text:
            assert "“intentional unused”" not in text, f"curly quotes in {doc}"
