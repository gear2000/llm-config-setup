"""Static contract test for generated Herdr phase-worker instructions."""

from pathlib import Path


PROTOCOL = (
    Path(__file__).resolve().parent.parent
    / ".shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md"
)


def test_result_template_requires_literal_order_identity_and_destination() -> None:
    text = PROTOCOL.read_text()

    assert "The Recruiter-generated final worker brief includes a copy-pasteable result template and its destination." in text
    assert "literal `order_id` and literal absolute `result_path`" in text
    assert "never text that may appear in a generated instruction" in text
    assert "Write result.json exactly to: <literal result_path from this order.json>" in text
    assert '"order_id": "<literal order_id from this order.json>"' in text
    assert "MUST NOT invent, generate, replace, or otherwise alter the order id." in text


def test_phase_dispatch_is_blocking_and_does_not_inject_commands_into_recruiter_pane() -> None:
    text = PROTOCOL.read_text()

    assert "just upagent-recruit <order.json path>" in text
    assert "blocking dispatch" in text
    assert "ORDER_RECEIPT" in text
    assert 'herdr pane run <recruiter_pane> "recruit <order.json path>"' not in text
    assert "per-order result watchdog" not in text
