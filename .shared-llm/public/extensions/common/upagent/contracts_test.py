# pyright: reportMissingImports=false
"""Unit tests for the UpAgent order/result contracts. Pure stdlib — no Herdr needed.

Run: python3 -m pytest .shared-llm/extensions/common/upagent/contracts_test.py -q
"""

from __future__ import annotations

# Import the sibling module by path so the test runs from the repo root without packaging.
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_contracts", Path(__file__).with_name("contracts.py")
)
assert _spec and _spec.loader
contracts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contracts)
ContractError = contracts.ContractError


def _valid_order() -> dict:
    return {
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "phase_id": "phase-0",
        "stage_id": "stage-1-implementation",
        "harness": "claude",
        "model": "",
        "agent": "backend",
        "cwd": "/tmp/wt",
        "instructions_path": "/tmp/wt/instructions.md",
        "result_path": "/tmp/wt/result.json",
        "cockpit_pane": "1-1",
    }


def _valid_result() -> dict:
    return {
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "verdict": "passed",
        "full_log": "session://transcript/abc.jsonl",
    }


def test_valid_order_parses() -> None:
    order = contracts.parse_order(json.dumps(_valid_order()))
    assert order["stage_id"] == "stage-1-implementation"


def test_order_accepts_only_requester_release_completion_policy() -> None:
    order = _valid_order()
    order["completion_policy"] = "requester_release"
    assert (
        contracts.parse_order(json.dumps(order))["completion_policy"]
        == "requester_release"
    )

    order["completion_policy"] = "keep_forever"
    with pytest.raises(ContractError, match="completion_policy"):
        contracts.parse_order(json.dumps(order))

    order["completion_policy"] = "requester_release"
    order["timeout_ms"] = 7_200_001
    with pytest.raises(ContractError, match="120 minutes"):
        contracts.parse_order(json.dumps(order))


def test_order_management_override_accepts_both_modes() -> None:
    for mode in ("direct", "dedicated"):
        order = _valid_order()
        order["management"] = {"mode": mode}
        parsed = contracts.parse_order(json.dumps(order))
        assert parsed["management"]["mode"] == mode


def test_order_management_override_rejects_junk() -> None:
    order = _valid_order()
    order["management"] = "dedicated"
    with pytest.raises(ContractError, match="must be an object"):
        contracts.parse_order(json.dumps(order))

    order = _valid_order()
    order["management"] = {"mode": "autopilot"}
    with pytest.raises(ContractError, match="management.mode"):
        contracts.parse_order(json.dumps(order))

    order = _valid_order()
    order["management"] = {"mode": "direct", "rescue": True}
    with pytest.raises(ContractError, match="supports only"):
        contracts.parse_order(json.dumps(order))


def test_order_accepts_explicit_requester_and_request_identity() -> None:
    order = _valid_order()
    order["request_id"] = "req-run-a-stage-1"
    order["requester"] = {
        "id": "phase-leader-a",
        "kind": "herdr-agent",
        "address": "w1:p2",
    }
    parsed = contracts.parse_order(json.dumps(order))
    assert parsed["requester"]["id"] == "phase-leader-a"


def test_order_rejects_malformed_requester() -> None:
    order = _valid_order()
    order["requester"] = {"id": "phase-leader-a", "kind": "herdr-agent"}
    with pytest.raises(ContractError, match="requester.address"):
        contracts.parse_order(json.dumps(order))


def test_order_missing_key_fails() -> None:
    bad = _valid_order()
    del bad["cwd"]
    with pytest.raises(ContractError, match="cwd"):
        contracts.parse_order(json.dumps(bad))


def test_order_missing_cockpit_pane_fails() -> None:
    bad = _valid_order()
    del bad["cockpit_pane"]
    with pytest.raises(ContractError, match="cockpit_pane"):
        contracts.parse_order(json.dumps(bad))


def test_order_unknown_stage_fails() -> None:
    bad = _valid_order()
    bad["stage_id"] = "stage-9-nope"
    with pytest.raises(ContractError, match="unknown stage_id"):
        contracts.parse_order(json.dumps(bad))


def test_order_unknown_harness_fails() -> None:
    bad = _valid_order()
    bad["harness"] = "emacs"
    with pytest.raises(ContractError, match="unknown harness"):
        contracts.parse_order(json.dumps(bad))


def test_order_codex_harness_parses() -> None:
    order = _valid_order()
    order["harness"] = "codex"
    order["model"] = "gpt-5.6-sol"
    order["effort"] = "high"

    parsed = contracts.parse_order(json.dumps(order))

    assert parsed["harness"] == "codex"
    assert parsed["model"] == "gpt-5.6-sol"
    assert parsed["effort"] == "high"


def test_order_bad_env_fails() -> None:
    bad = _valid_order()
    bad["env"] = {"OK": 3}
    with pytest.raises(ContractError, match="env"):
        contracts.parse_order(json.dumps(bad))


def test_order_env_optional_absent_ok() -> None:
    contracts.parse_order(json.dumps(_valid_order()))  # no env key → fine


def test_order_effort_optional_absent_ok() -> None:
    contracts.parse_order(json.dumps(_valid_order()))  # no effort key → fine


def test_order_effort_string_ok() -> None:
    order = _valid_order()
    order["effort"] = "high"
    assert contracts.parse_order(json.dumps(order))["effort"] == "high"


def test_order_effort_non_string_fails() -> None:
    bad = _valid_order()
    bad["effort"] = 3
    with pytest.raises(ContractError, match="effort"):
        contracts.parse_order(json.dumps(bad))


@pytest.mark.parametrize("timeout_ms", [True, "1000", 0, -1])
def test_order_timeout_ms_must_be_a_positive_integer(timeout_ms: object) -> None:
    bad = _valid_order()
    bad["timeout_ms"] = timeout_ms
    with pytest.raises(ContractError, match="timeout_ms"):
        contracts.parse_order(json.dumps(bad))


def test_order_positive_timeout_ms_is_valid() -> None:
    order = _valid_order()
    order["timeout_ms"] = 1
    assert contracts.parse_order(json.dumps(order))["timeout_ms"] == 1


@pytest.mark.parametrize(
    ("agent", "kind"),
    [
        ("plan-lifecycle-watchdog", "plan"),
        ("phase-watchdog", "phase"),
    ],
)
def test_watchdog_order_requires_a_matching_durable_terminal_gate(
    agent: str, kind: str
) -> None:
    order = _valid_order()
    order["agent"] = agent

    with pytest.raises(ContractError, match="watchdog_terminal"):
        contracts.parse_order(json.dumps(order))

    order["watchdog_terminal"] = {
        "identity": "sample-run" if kind == "plan" else "phase-0",
        "kind": kind,
        "path": "/tmp/sample-run/control/run-terminal.json"
        if kind == "plan"
        else "/tmp/sample-run/phases/phase-0/phase-result.json",
    }
    if kind == "plan":
        order["mode"] = "direct"
        order["plan_id"] = "sample-run"
        order["step_id"] = "plan-watchdog"

    parsed = contracts.parse_order(json.dumps(order))

    assert parsed["watchdog_terminal"]["kind"] == kind


def test_watchdog_terminal_gate_rejects_wrong_kind_identity_and_relative_path() -> None:
    order = _valid_order()
    order["agent"] = "phase-watchdog"
    order["watchdog_terminal"] = {
        "identity": "another-phase",
        "kind": "plan",
        "path": "relative/phase-result.json",
    }

    with pytest.raises(ContractError, match="kind"):
        contracts.parse_order(json.dumps(order))


def test_not_json_fails() -> None:
    with pytest.raises(ContractError, match="not valid JSON"):
        contracts.parse_order("{not json")


def test_valid_result_parses() -> None:
    r = contracts.parse_result(json.dumps(_valid_result()))
    assert r["verdict"] == "passed"


def test_result_bad_verdict_fails() -> None:
    bad = _valid_result()
    bad["verdict"] = "great"
    with pytest.raises(ContractError, match="verdict"):
        contracts.parse_result(json.dumps(bad))


@pytest.mark.parametrize(
    "alias",
    [
        "VERIFICATION_PASSED",
        "VERIFIED",
        "PASS",
        "PASSED",
        "OK",
        "FAIL",
        "FAILED",
        "BLOCKED",
    ],
)
def test_result_cosmetic_verdict_aliases_are_rejected(alias: str) -> None:
    result = _valid_result()
    result["verdict"] = alias
    with pytest.raises(ContractError, match="verdict"):
        contracts.parse_result(json.dumps(result))


def test_result_singleton_full_log_list_is_rejected() -> None:
    result = _valid_result()
    result["full_log"] = [result["full_log"]]
    with pytest.raises(ContractError, match="full_log"):
        contracts.parse_result(json.dumps(result))


def test_result_order_id_mismatch_fails() -> None:
    with pytest.raises(ContractError, match="does not match"):
        contracts.parse_result(
            json.dumps(_valid_result()), expected_order_id="other-id"
        )


def test_failed_verdict_requires_revisit() -> None:
    bad = _valid_result()
    bad["verdict"] = "failed"
    with pytest.raises(ContractError, match="must name at least one stage"):
        contracts.parse_result(json.dumps(bad))


def test_failed_verdict_with_revisit_ok() -> None:
    r = _valid_result()
    r["verdict"] = "failed"
    r["revisit"] = ["stage-1-implementation"]
    parsed = contracts.parse_result(json.dumps(r))
    assert parsed["revisit"] == ["stage-1-implementation"]


def test_revisit_unknown_stage_fails() -> None:
    bad = _valid_result()
    bad["verdict"] = "failed"
    bad["revisit"] = ["stage-1-implementation", "stage-42"]
    with pytest.raises(ContractError, match="not a recognized stage"):
        contracts.parse_result(json.dumps(bad))


def test_revisit_recognized_string_coerced_to_list() -> None:
    # Observed in the field: a worker wrote the recognized stage id as a bare string on a
    # `passed` verdict. The reader repairs the shape and records it.
    r = _valid_result()
    r["revisit"] = "stage-1-implementation"
    parsed = contracts.parse_result(json.dumps(r))
    assert parsed["revisit"] == ["stage-1-implementation"]
    assert parsed["revisit_normalized"] == "stage-1-implementation"


def test_revisit_recognized_string_coerced_on_failed_verdict() -> None:
    r = _valid_result()
    r["verdict"] = "failed"
    r["revisit"] = "stage-1-implementation"
    parsed = contracts.parse_result(json.dumps(r))
    assert parsed["revisit"] == ["stage-1-implementation"]


def test_revisit_prose_string_dropped_on_non_failed_verdict() -> None:
    # Observed in the field: a prose paragraph as `revisit` on a `blocked` verdict.
    # The field is unused off the `failed` path, so noise is dropped.
    r = _valid_result()
    r["verdict"] = "blocked"
    r["revisit"] = "please revisit the implementation because the tests were flaky"
    parsed = contracts.parse_result(json.dumps(r))
    assert parsed["revisit"] == []
    assert "revisit_normalized" in parsed


def test_revisit_unrecognized_entries_dropped_on_non_failed_verdict() -> None:
    r = _valid_result()
    r["revisit"] = ["stage-1-implementation", "stage-42", 7]
    parsed = contracts.parse_result(json.dumps(r))
    assert parsed["revisit"] == ["stage-1-implementation"]


def test_revisit_prose_string_still_fails_on_failed_verdict() -> None:
    # A `failed` verdict's revisit list is load-bearing for backtracking: no repair.
    bad = _valid_result()
    bad["verdict"] = "failed"
    bad["revisit"] = "go back and redo the implementation"
    with pytest.raises(ContractError, match="revisit"):
        contracts.parse_result(json.dumps(bad))


_REVIEW_DOCUMENT = (
    "## Adversarial review\n\n"
    "Checked every claim in the result against the diff and the test output; the swallowed "
    "exception reported earlier is fixed and the suite genuinely passes.\n\n"
    "VERDICT: CLEARED"
)


def test_order_result_contract_accepts_only_review() -> None:
    order = _valid_order()
    order["result_contract"] = "review"
    assert contracts.parse_order(json.dumps(order))["result_contract"] == "review"
    order["result_contract"] = "audit"
    with pytest.raises(ContractError, match="result_contract"):
        contracts.parse_order(json.dumps(order))


def test_review_result_requires_verdict_document() -> None:
    result = _valid_result()
    with pytest.raises(ContractError, match="verdict_document"):
        contracts.parse_result(json.dumps(result), result_contract="review")


def test_review_result_with_valid_document_passes() -> None:
    result = _valid_result()
    result["verdict_document"] = _REVIEW_DOCUMENT
    parsed = contracts.parse_result(json.dumps(result), result_contract="review")
    assert parsed["verdict_document"].endswith("VERDICT: CLEARED")


def test_review_result_rejects_short_or_untailed_documents() -> None:
    result = _valid_result()
    result["verdict_document"] = "Looks fine. VERDICT: CLEARED"
    with pytest.raises(ContractError, match="verdict_document"):
        contracts.parse_result(json.dumps(result), result_contract="review")
    result["verdict_document"] = _REVIEW_DOCUMENT.replace(
        "VERDICT: CLEARED", "Everything held up."
    )
    with pytest.raises(ContractError, match="CLEARED"):
        contracts.parse_result(json.dumps(result), result_contract="review")


def test_review_verdict_must_match_the_document_tail() -> None:
    # passed/VEERED and failed/CLEARED are contradictions, not verdicts: the receipt would
    # say one thing while the derived compacted.md said the opposite.
    result = _valid_result()
    result["verdict_document"] = _REVIEW_DOCUMENT.replace(
        "VERDICT: CLEARED", "VERDICT: VEERED"
    )
    with pytest.raises(ContractError, match="contradicts"):
        contracts.parse_result(json.dumps(result), result_contract="review")

    failed = _valid_result()
    failed["verdict"] = "failed"
    failed["revisit"] = ["stage-1-implementation"]
    failed["verdict_document"] = _REVIEW_DOCUMENT
    with pytest.raises(ContractError, match="contradicts"):
        contracts.parse_result(json.dumps(failed), result_contract="review")

    failed["verdict_document"] = _REVIEW_DOCUMENT.replace(
        "VERDICT: CLEARED", "VERDICT: VEERED"
    )
    parsed = contracts.parse_result(json.dumps(failed), result_contract="review")
    assert parsed["verdict"] == "failed"


def test_review_contract_exempts_blocked_verdicts() -> None:
    # Python authors blocked terminals itself (repair exhausted, dead pane); a machine-written
    # outcome cannot carry a review it never performed.
    result = _valid_result()
    result["verdict"] = "blocked"
    assert contracts.parse_result(json.dumps(result), result_contract="review")


def test_result_loader_binds_the_order_contract(tmp_path) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(_valid_result()))
    plain = contracts.result_loader({"order_id": "x"})
    assert plain(path)["verdict"] == "passed"
    review = contracts.result_loader({"order_id": "x", "result_contract": "review"})
    with pytest.raises(ContractError, match="verdict_document"):
        review(path)


def test_advisor_decision_valid() -> None:
    r = _valid_result()
    r["decision"] = "stop-ask-human"
    assert contracts.parse_result(json.dumps(r))["decision"] == "stop-ask-human"


def test_advisor_decision_unknown_fails() -> None:
    r = _valid_result()
    r["decision"] = "shrug"
    with pytest.raises(ContractError, match="decision"):
        contracts.parse_result(json.dumps(r))


def test_result_without_decision_ok() -> None:
    # decision is optional — a normal (non-advisor) result omits it.
    assert "decision" not in contracts.parse_result(json.dumps(_valid_result()))


# --- coordination v2: typed events, owner commands, acknowledgements ---------


def _valid_event(**over) -> dict:
    base = {
        "schema_version": contracts.COORDINATION_SCHEMA_VERSION,
        "event_id": "evt-1",
        "sequence": 1,
        "occurred_at": "2026-07-16T00:00:00Z",
        "run_id": "sample-run",
        "phase_id": "phase-0",
        "kind": "needs-input",
        "terminal": False,
        "severity": "attention",
        "summary": "which db?",
        "ack_required": True,
    }
    base.update(over)
    return base


def test_valid_event_parses() -> None:
    event = contracts.parse_event(json.dumps(_valid_event()))
    assert event["kind"] == "needs-input"


def test_event_rejects_wrong_schema_version() -> None:
    with pytest.raises(ContractError, match="schema_version"):
        contracts.parse_event(json.dumps(_valid_event(schema_version=99)))


def test_event_rejects_unknown_kind() -> None:
    with pytest.raises(ContractError, match="unknown kind"):
        contracts.parse_event(json.dumps(_valid_event(kind="vibes")))


def test_blocked_event_is_terminal() -> None:
    # A blocked phase ends its attempt; the owner decides and replays as a fresh pass.
    assert contracts.EVENT_KINDS["blocked"] is True
    event = contracts.parse_event(
        json.dumps(_valid_event(kind="blocked", terminal=True))
    )
    assert event["terminal"] is True


def test_event_terminal_flag_must_match_kind() -> None:
    with pytest.raises(ContractError, match="terminal"):
        contracts.parse_event(
            json.dumps(_valid_event(kind="completed", terminal=False))
        )


def test_event_sequence_must_be_positive_integer() -> None:
    with pytest.raises(ContractError, match="sequence"):
        contracts.parse_event(json.dumps(_valid_event(sequence=0)))
    with pytest.raises(ContractError, match="sequence"):
        contracts.parse_event(json.dumps(_valid_event(sequence=True)))


def test_event_run_and_phase_identity_are_enforced() -> None:
    text = json.dumps(_valid_event())
    with pytest.raises(ContractError, match="run_id"):
        contracts.parse_event(text, expected_run_id="another-run")
    with pytest.raises(ContractError, match="phase_id"):
        contracts.parse_event(text, expected_phase_id="phase-9")


def test_event_order_requires_increasing_sequence() -> None:
    first = _valid_event(sequence=2)
    second = _valid_event(event_id="evt-2", sequence=2)
    with pytest.raises(ContractError, match="must exceed"):
        contracts.validate_event_order(first, second)


def test_event_order_terminal_blocks_same_generation_followers() -> None:
    done = _valid_event(kind="completed", terminal=True)
    late = _valid_event(event_id="evt-2", sequence=2)
    with pytest.raises(ContractError, match="terminal"):
        contracts.validate_event_order(done, late)
    fresh = _valid_event(event_id="evt-3", sequence=2, generation=2)
    assert contracts.validate_event_order(done, fresh) is fresh


def _valid_command(**over) -> dict:
    base = {
        "schema_version": contracts.COORDINATION_SCHEMA_VERSION,
        "command_id": "cmd-1",
        "run_id": "sample-run",
        "action": "continue",
        "issued_by": "tui:w1:p1",
    }
    base.update(over)
    return base


def test_valid_command_parses() -> None:
    # Forward contract: nothing consumes owner commands yet, but the shape is pinned.
    command = contracts.parse_phase_command(json.dumps(_valid_command()))
    assert command["action"] == "continue"


def test_command_rejects_unknown_action() -> None:
    with pytest.raises(ContractError, match="action"):
        contracts.parse_phase_command(json.dumps(_valid_command(action="reboot")))


def test_command_extend_requires_extension_ms() -> None:
    with pytest.raises(ContractError, match="extension_ms"):
        contracts.parse_phase_command(
            json.dumps(_valid_command(action="extend-soft-timeout"))
        )
    ok = contracts.parse_phase_command(
        json.dumps(_valid_command(action="extend-soft-timeout", extension_ms=60_000))
    )
    assert ok["extension_ms"] == 60_000


def test_command_run_identity_is_enforced() -> None:
    with pytest.raises(ContractError, match="run_id"):
        contracts.parse_phase_command(
            json.dumps(_valid_command()), expected_run_id="another-run"
        )


def test_valid_ack_parses_and_identity_is_enforced() -> None:
    ack = {
        "event_id": "evt-1",
        "state": "acknowledged",
        "actor": "owner",
        "occurred_at": "2026-07-16T00:00:00Z",
    }
    assert contracts.parse_ack(json.dumps(ack))["state"] == "acknowledged"
    with pytest.raises(ContractError, match="event_id"):
        contracts.parse_ack(json.dumps(ack), expected_event_id="evt-9")


def test_ack_rejects_unknown_state() -> None:
    ack = {
        "event_id": "evt-1",
        "state": "seen",
        "actor": "owner",
        "occurred_at": "2026-07-16T00:00:00Z",
    }
    with pytest.raises(ContractError, match="state"):
        contracts.parse_ack(json.dumps(ack))


def test_ack_transitions_move_forward_only() -> None:
    assert contracts.validate_ack_transition(None, "published") == "published"
    assert contracts.validate_ack_transition("published", "resolved") == "resolved"
    with pytest.raises(ContractError, match="regress"):
        contracts.validate_ack_transition("resolved", "acknowledged")


def _salvaged_result() -> str:
    return json.dumps(
        {
            "order_id": "order-1",
            "verdict": "salvaged-done",
            "reason": "recruiter: mechanical salvage",
            "full_log": "(none)",
        }
    )


def test_synthesized_verdicts_are_rejected_unless_the_caller_opts_in() -> None:
    """Only a Recruiter salvage writer may pass `allow_synthesized`; workers never can."""
    with pytest.raises(ContractError, match="must be one of passed, failed, blocked"):
        contracts.parse_result(_salvaged_result(), "order-1")

    parsed = contracts.parse_result(
        _salvaged_result(), "order-1", allow_synthesized=True
    )
    assert parsed["verdict"] == "salvaged-done"
    assert parsed["verdict"] not in contracts.VERDICTS
    assert parsed["verdict"] in contracts.SYNTHESIZED_VERDICTS


def test_result_loader_propagates_the_synthesis_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(_salvaged_result())
    order = {"order_id": "order-1"}

    with pytest.raises(ContractError, match="must be one of passed, failed, blocked"):
        contracts.result_loader(order)(path, "order-1")

    loaded = contracts.result_loader(order, allow_synthesized=True)(path, "order-1")
    assert loaded["verdict"] == "salvaged-done"


def test_receipt_synthesis_provenance_must_be_recognized() -> None:
    for synthesis_path in contracts.SYNTHESIS_PATHS:
        contracts.validate_receipt_synthesis(synthesis_path, "unconfirmed")
    contracts.validate_receipt_synthesis(contracts.DEFAULT_SYNTHESIS_PATH, "confirmed")

    with pytest.raises(ContractError, match="synthesis_path"):
        contracts.validate_receipt_synthesis("salvaged-somehow", "confirmed")
    with pytest.raises(ContractError, match="confirmation"):
        contracts.validate_receipt_synthesis("clean", "probably")
