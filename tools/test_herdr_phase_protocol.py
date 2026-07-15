"""Static contract test for generated Herdr phase-worker instructions."""

from pathlib import Path


PROTOCOL = (
    Path(__file__).resolve().parent.parent
    / ".shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md"
)
RUNNER = PROTOCOL.parent.parent / "herdr-run/command.md"


def test_result_template_requires_literal_order_identity_and_destination() -> None:
    text = PROTOCOL.read_text()

    assert "The Recruiter-generated final worker brief includes a copy-pasteable result template and its destination." in text
    assert "literal `order_id` and literal absolute `result_path`" in text
    assert "never text that may appear in a generated instruction" in text
    assert "Write result.json exactly to: <literal result_path from this order.json>" in text
    assert '"order_id": "<literal order_id from this order.json>"' in text
    assert "MUST NOT invent, generate, replace, or otherwise alter the order id." in text


def test_phase_dispatch_verifies_startup_then_waits_without_shell_injection() -> None:
    text = PROTOCOL.read_text()

    assert "just upagent-request <order.json path>" in text
    assert "just upagent-await <order.json path>" in text
    assert "REQUEST_ACCEPTED" in text
    assert "REQUESTER_DECISION_REQUIRED" in text
    assert "control_token" in text
    assert "ORDER_RECEIPT" in text
    assert 'herdr pane run <recruiter_pane> "recruit <order.json path>"' not in text
    assert "per-order result watchdog" not in text


def test_tui_requests_one_managed_phase_watchdog() -> None:
    text = RUNNER.read_text()

    assert "just upagent-request <order>" in text
    assert "agent: phase-watchdog" in text
    assert "finalization_defaults.watchdog_profile" in text
    assert "phase-result.json" in text
    assert "Never use `agent-status=done`" in text
