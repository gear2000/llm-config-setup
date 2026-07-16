"""Static contract test for generated Herdr phase-worker instructions."""

from pathlib import Path


PROTOCOL = (
    Path(__file__).resolve().parent.parent
    / ".shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md"
)
RUNNER = PROTOCOL.parent.parent / "herdr-run/command.md"
HERDR_JUSTFILE = (
    Path(__file__).resolve().parent.parent
    / ".shared-llm/public/extensions/common/herdr/justfile"
)
PLAN_CONTROLLER = HERDR_JUSTFILE.with_name("plan_controller.py")


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


def test_tui_runs_one_managed_phase_start_without_a_watchdog() -> None:
    text = RUNNER.read_text()

    assert "just upagent-phase-start <run-tree>/route.yaml <run-tree> <phase-id> <pass-number>" in text
    assert "PHASE_STARTED" in text
    assert "`state: ready-degraded` receipt is equally continuable" in text
    assert "`not-configured` by design" in text
    assert (
        "The TUI has no authority to create, launch, prompt, adopt, or replace a "
        "watchdog agent or a phase leader." in text
    )
    assert "never create a standing watchdog agent" in text
    assert "do not reproduce its pane operations manually" in text
    assert "phase-result.json" in text
    assert "Never use `agent-status=done`" in text
    assert "Do not call `herdr pane split`, `herdr agent start`, `herdr pane run`" in text
    assert "This is mandatory, not guidance." in text
    assert "Do not send `/herdr-phase` to any pane yourself." in text


def test_tui_final_message_is_short_and_unambiguous() -> None:
    text = RUNNER.read_text()

    assert "SUCCESS — Everything succeeded. Safe to close this workspace." in text
    assert "SUCCESS — Everything succeeded. Cleanup is still finishing" in text
    assert "STOPPED — This run did not succeed." in text
    assert "Do not print a stage-by-stage recap" in text
    assert "Those details belong only in `run-status.md`" in text


def test_plan_launcher_owns_tui_startup_and_never_hires_a_watchdog() -> None:
    runner = RUNNER.read_text()
    launcher = HERDR_JUSTFILE.read_text()
    controller = PLAN_CONTROLLER.read_text()

    assert "There is no standing plan watchdog" in launcher
    assert "herdr notification" in launcher
    assert "there is no standing plan-lifecycle-watchdog" in runner
    assert "escalate to the human through `herdr notification`" in runner
    assert (
        "The TUI has no authority to create, launch, prompt, adopt, or replace a "
        "watchdog agent or a phase leader." in runner
    )
    assert "plan_controller.py" in launcher
    assert 'herdr pane run "$pane"' not in launcher
    assert "--run-tree" in controller
    assert "_submit_agent_prompt" not in controller
    assert "plan-lifecycle-watchdog" not in controller
    assert 'recruiter._herdr("agent", "send"' not in controller


def test_phase_leader_continues_degraded_without_a_controller_watchdog_receipt() -> None:
    text = PROTOCOL.read_text()

    assert "Inspect controller ownership without making monitoring a work gate." in text
    assert "$UPAGENT_PHASE_START_RECEIPT" in text
    assert "Monitoring failure must never become an infinite wait or prevent plan work." in text



def test_tui_waits_inside_phase_await_not_pane_watching() -> None:
    text = RUNNER.read_text()

    assert "just upagent-phase-await" in text
    assert "just upagent-phase-ack" in text
    assert "re-await with `after=<that event's sequence>`" in text
    assert "`await-heartbeat` | no | Quiet and healthy: re-await immediately and silently" in text
    assert "`leader-missing`" in text
    assert "`leader-stalled`" in text
    assert "`inactivity-checkpoint`" in text
    assert "An unacknowledged actionable event is redelivered by the next await" in text
    assert "a `PHASE_RESULT` pane marker is display-only" in text


def test_leader_publishes_typed_events_and_multiplexes_awaits() -> None:
    text = PROTOCOL.read_text()

    assert "just upagent-await-any" in text
    assert "just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT needs-input" in text
    assert "just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT blocked" in text
    assert "AWAIT_EVENT" in text
    assert "Echo the returned `cursor` back on the next call" in text
    assert "nothing is ever pasted into this leader's pane" in text
