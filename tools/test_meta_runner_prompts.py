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
PI_HUB = REPO_ROOT / ".shared-llm/public/llm/pi/common/meta-plan"

PHASE_PROTOCOL = LAYERS / "slash-commands/common/common/meta-runner-phase-protocol.md"
HANDOFF_PROTOCOL = LAYERS / "slash-commands/common/common/meta-runner-handoff-protocol.md"
COMPOSE_SLASH_COMMANDS = REPO_ROOT / ".shared-llm/public/compose/slash-commands"
HERDR_PHASE = LAYERS / "slash-commands/common/common/herdr-phase/command.md"
ADVERSARIAL_EVALUATOR_SRC = LAYERS / "agents/common/adversarial-evaluator.md"
ADVERSARIAL_EVALUATOR_ARTIFACT = REPO_ROOT / ".claude/agents/adversarial-evaluator.md"
META_PLAN_FORMAT = PI_HUB / "meta-plan-format.md"

# The canonical phrase every Stage 2 reference must use verbatim.
GATE_PHRASE = "unused intake / accepted-but-ignored inputs"

# Every document that describes or references the Stage 2 audit must name the gate.
STAGE2_REFERENCING_DOCS = [
    PHASE_PROTOCOL,
    HERDR_PHASE,
    ADVERSARIAL_EVALUATOR_SRC,
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


def test_ordinary_stage3_is_deterministic_local_seam_verification() -> None:
    text = PHASE_PROTOCOL.read_text()

    assert "Stage 3 is deterministic changed-scope local seam/contract verification" in text
    assert "not a second semantic reviewer" in text
    assert "not a shared-environment acceptance stage" in text
    assert "A fresh LLM verifier is hired through the Recruiter only when" in text
    assert "residual cross-slice production work belongs in an explicit integration-construction phase" in text


def test_execution_model_distinguishes_worker_orders_from_controller_stages() -> None:
    text = PHASE_PROTOCOL.read_text()

    assert "## Execution model — worker orders and deterministic controller stages" in text
    assert "runs all LLM work by placing a **work order**" in text
    assert "Ordinary Stage 3 seam checks, ordinary Stage 4 deferral/merge records, and Stage 5 finalization are deterministic controller actions" in text
    assert "## Execution model — a stage is a work order, not a subagent" not in text
    assert "The phase leader runs each stage by placing a **work order**" not in text


def test_deterministic_stage_evidence_does_not_invent_worker_fields() -> None:
    text = PHASE_PROTOCOL.read_text()

    assert "A deterministic controller stage writes `controller-result.json` with this shape instead" in text
    assert "stage id, `runner: controller`" in text
    assert "commands run, exit codes, log/evidence paths or bounded excerpts" in text
    assert "Do not invent a synthetic `order_id`" in text
    assert "worker `result.json`" in text
    assert "worker `full_log`" in text
    assert "deterministic-stage evidence from `controller-result.json`: stage id" in text
    assert "worker-stage evidence: stage id, `llm_profile`, `agent`, `order_id`" in text
    assert "each stage id with `llm_profile` and `agent` used, plus its `order_id`" not in text
    assert "commands run and evidence paths/log excerpts (`full_log` pointers)" not in text


def test_ordinary_stage4_is_not_per_phase_deployment_or_acceptance() -> None:
    text = PHASE_PROTOCOL.read_text()

    assert "Stage 4 must not become a per-phase shared-environment, deployment, CI, upstream-DAG, or global acceptance stage" in text
    assert "Broad shared acceptance belongs once at candidate level" in text
    assert "route-owned candidate-level finalization/gate" in text
    assert "TUI-owned apply receipt" in text
    assert "/herdr-phase`'s \"IaC phases (kind: iac)\" section" in text
    assert "meta-plan format's \"IaC phases (`kind: iac`)\" section" in text
    assert "described below" not in text
    assert "Stage 4 — upstream DAG dependent build/deploy/test verification" not in text
    assert "Trace every upstream dependent" not in text
    assert "deployment or deployment dry-run" not in text
    assert "acceptance/live checks" not in text


def test_stage5_runs_exactly_route_owned_green_checks() -> None:
    for doc in (PHASE_PROTOCOL, META_PLAN_FORMAT):
        text = doc.read_text()
        assert "always runs exactly the effective route-owned `green_checks`" in text
        assert "does not infer" in text
        assert "At plan/conversion time" in text
        assert "omit those generic commands from per-phase `green_checks`" in text
        assert (
            "retains the repository's normal green checks" in text
            or "retain the repository's normal green-check command" in text
        )
        assert "candidate gate is configured" not in text
        assert "no candidate gate is configured" not in text


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

    assert "phases/<phase-id>/handoffs/<role>-vN.md" in text
    assert "writes a short handoff before its pane closes" in text
    assert "phase leader reads the relevant handoffs" in text


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
