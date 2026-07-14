"""Static contract test for generated Herdr phase-worker instructions."""

from pathlib import Path


PROTOCOL = (
    Path(__file__).resolve().parent.parent
    / ".shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md"
)


def test_result_template_requires_literal_order_identity_and_destination() -> None:
    text = PROTOCOL.read_text()

    assert "Every generated `instructions.md` includes a copy-pasteable result template and its destination." in text
    assert "literal `order_id` and literal absolute `result_path`" in text
    assert "never text that may appear in a generated instruction" in text
    assert "Write result.json exactly to: <literal result_path from this order.json>" in text
    assert '"order_id": "<literal order_id from this order.json>"' in text
    assert "MUST NOT invent, generate, replace, or otherwise alter the order id." in text
