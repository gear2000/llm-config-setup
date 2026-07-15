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


def test_tui_requests_one_managed_phase_watchdog() -> None:
    text = RUNNER.read_text()

    assert "just upagent-phase-start <run-tree>/route.yaml <run-tree> <phase-id> <pass-number>" in text
    assert "PHASE_STARTED" in text
    assert "state: ready-degraded" in text
    assert "manager and watchdog panes are in the same cockpit workspace" in text
    assert (
        "The TUI has no authority to create, launch, prompt, adopt, or replace the plan watchdog, "
        "a phase leader, or a phase watchdog." in text
    )
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


def test_plan_launcher_owns_tui_and_plan_watchdog_startup() -> None:
    runner = RUNNER.read_text()
    launcher = HERDR_JUSTFILE.read_text()
    controller = PLAN_CONTROLLER.read_text()

    assert "plan-lifecycle-watchdog" in runner
    assert (
        "The TUI has no authority to create, launch, prompt, adopt, or replace the plan watchdog"
        in runner
    )
    assert "record that degraded condition and continue" in runner
    assert "plan_controller.py" in launcher
    assert 'herdr pane run "$pane"' not in launcher
    assert "--run-tree" in controller
    assert 'recruiter._submit_agent_prompt(pane_id, message, idle_timeout_ms=5_000)' in controller
    assert 'recruiter._herdr("agent", "send"' not in controller


def test_phase_leader_continues_degraded_without_a_controller_watchdog_receipt() -> None:
    text = PROTOCOL.read_text()

    assert "Inspect controller ownership without making monitoring a work gate." in text
    assert "$UPAGENT_PHASE_START_RECEIPT" in text
    assert "Monitoring failure must never become an infinite wait or prevent plan work." in text
