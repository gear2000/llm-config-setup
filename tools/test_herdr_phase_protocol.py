"""Static contract test for generated Herdr phase-worker instructions."""

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = (
    ROOT / ".shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md"
)
RUNNER = PROTOCOL.parent.parent / "herdr-run/command.md"
SHARED_PROTOCOL = PROTOCOL.parent.parent / "meta-runner-phase-protocol.md"
EXTENSIONS = ROOT / ".shared-llm/public/extensions"
HERDR_JUSTFILE = EXTENSIONS / "common/herdr/justfile"
PLAN_CONTROLLER = HERDR_JUSTFILE.with_name("plan_controller.py")

# A runnable command in these documents is always in code formatting, so the extraction reads
# inline code spans and fenced blocks only — never bare prose, where "just to pass tests" is
# English and not a recipe.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_CODE_FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)
_JUST_COMMAND = re.compile(r"^just\s+([a-zA-Z0-9][a-zA-Z0-9_-]*)")
# `<recipe> <args>:` at the start of a line is a justfile recipe definition.
_RECIPE_DEF = re.compile(r"^([a-z][a-z0-9-]*)(?:\s+[^\n]*?)?:", re.M)
_JUST_VARIABLE = re.compile(r"^(_[A-Z_]+)\s*:=\s*(.+)$", re.M)
_ENGINE_MODULE = re.compile(r"extensions/common/([a-z0-9_-]+)/[a-z0-9_]+\.py")


def _documented_just_commands(*docs: Path) -> set[str]:
    """Every `just <recipe>` a document tells an agent to run."""
    commands: set[str] = set()
    for doc in docs:
        text = doc.read_text()
        snippets = _CODE_SPAN.findall(text)
        for block in _CODE_FENCE.findall(text):
            snippets.extend(block.splitlines())
        for snippet in snippets:
            match = _JUST_COMMAND.match(snippet.strip().lstrip("$ ").strip())
            if match:
                commands.add(match.group(1))
    return commands


def _defining_module(recipe: str) -> str:
    """The extension module whose justfile defines `recipe`. Fail-loud when none does."""
    for justfile in sorted(EXTENSIONS.glob("*/*/justfile")):
        if recipe in _RECIPE_DEF.findall(justfile.read_text()):
            return justfile.parent.name
    raise AssertionError(f"no extension module defines the `just {recipe}` recipe")


def _modules_started_by(recipe: str) -> set[str]:
    """The extension modules whose engine `recipe` launches, read from the recipe body."""
    text = HERDR_JUSTFILE.read_text()
    variables = dict(_JUST_VARIABLE.findall(text))
    body = re.search(rf"^{recipe}[^\n]*:\n((?:[ \t]+[^\n]*\n)+)", text, re.M)
    assert body is not None, f"{HERDR_JUSTFILE} defines no `{recipe}` recipe"
    expanded = re.sub(
        r"\{\{(_[A-Z_]+)\}\}", lambda m: variables.get(m.group(1), ""), body.group(1)
    )
    return set(_ENGINE_MODULE.findall(expanded))


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

    assert "SUCCESS — Everything succeeded. Safe to close this run's panes." in text
    assert "SUCCESS — Everything succeeded. Cleanup is still finishing" in text
    assert "STOPPED — This run did not succeed." in text
    assert "Do not print a stage-by-stage recap" in text
    assert "Those details belong only in `run-status.md`" in text
    # Mode-aware close guidance: never tell the human to close a workspace that still
    # hosts the services (the single-workspace default shares `herdr` across runs).
    assert "Never tell the human" in text
    assert "close a workspace that still hosts the services" in text


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


def test_ordinary_stage3_and_stage5_are_deterministic_controller_stages() -> None:
    text = PROTOCOL.read_text()

    assert "leader runs LLM implementation, audit, verifier, advisor, and consult work by placing work orders" in text
    assert "runs ordinary Stage 3 seam checks and Stage 5 finalization as deterministic controller actions with typed evidence" in text
    assert "worker orders plus deterministic controller stages" in text
    assert "Ordinary Stage 3 seam checks and Stage 5 finalization are deterministic controller stages" in text
    assert "Stage 3 — ordinary deterministic local seam/contract checks" in text
    assert "Hire a fresh verifier through the Recruiter only when a command fails" in text
    assert "places a work ORDER per stage" not in text
    assert "The leader runs the shared five-stage worktree lifecycle, ordering one worker per stage" not in text
    assert "A stage is a work order to the Recruiter" not in text


def test_phase_result_distinguishes_worker_and_deterministic_stage_evidence() -> None:
    text = PROTOCOL.read_text()

    assert "worker-stage evidence (`stage_id`, `llm_profile`, `agent`, `order_id`, tries, final verdict, and `full_log` pointer)" in text
    assert "deterministic-stage evidence from `controller-result.json` (`stage_id`, `runner: controller` or equivalent marker, commands, exit codes, log/evidence paths or bounded excerpts, tries, and final verdict)" in text
    assert "write `controller-result.json`" in text
    assert "do not invent a synthetic `order_id`, worker `result.json`, or worker `full_log`" in text
    assert "record that verifier as separate worker-stage evidence" in text
    assert "each stage id with `llm_profile`/`agent`/`order_id`/tries/final verdict" not in text
    assert "commands/evidence/`full_log` pointers" not in text


def test_ordinary_stage4_is_not_a_shared_environment_deployment_stage() -> None:
    text = PROTOCOL.read_text()

    assert "Stage 4 — no ordinary shared acceptance/deployment stage" in text
    assert "Do not run per-phase shared-environment, deployment, CI, upstream-DAG, or global acceptance checks" in text
    assert "broad shared acceptance is deferred to the route-owned candidate-level finalization/gate" in text
    assert "Non-ordinary variants keep their explicit contracts, including IaC" in text
    assert "Stage 4 — upstream DAG verification" not in text


def test_stage5_runs_exactly_route_owned_green_checks() -> None:
    text = PROTOCOL.read_text()

    assert "run exactly the effective route-owned `green_checks`" in text
    assert "The leader does not infer or branch on later candidate-level ownership" in text
    assert "Route authors decide the command set before execution" in text
    assert "omit those generic commands from per-phase `green_checks`" in text
    assert "otherwise retain the repository's normal green checks" in text
    assert "candidate gate follows" not in text
    assert "no candidate gate is configured" not in text



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


def test_every_command_the_protocol_names_resolves_to_a_real_recipe() -> None:
    """A command an agent is told to run must exist. Every `just <recipe>` in the three
    protocol documents is resolved against the real recipe set, so dropping a module's
    justfile import while its command is still documented fails here rather than sending a
    worker to a command that does not exist."""
    documented = _documented_just_commands(PROTOCOL, RUNNER, SHARED_PROTOCOL)
    assert documented, "the protocol documents no runnable command"

    summary = subprocess.run(
        ["just", "--justfile", str(ROOT / "justfile"), "--summary"],
        check=True,
        capture_output=True,
        text=True,
    )
    defined = set(summary.stdout.split())

    assert documented <= defined, f"documented but undefined: {sorted(documented - defined)}"


def test_every_service_the_runner_health_checks_is_started_by_herdr_up() -> None:
    """The cockpit's health checks are matched against the bring-up that has to satisfy
    them. Removing a service from `herdr-up` while the runner still tells the TUI to verify
    it fails here — the runner would otherwise send the TUI to check a service nothing
    starts, which reads as a broken environment rather than as the removal it is."""
    # Both spellings a health check can take: a module command with a `status` subcommand
    # (`just <module> status`) and a flat per-module recipe (`just upagent-status`).
    # Matching only the first would silently find nothing the day a module switches spelling,
    # and "no service is health-checked" would then read as "every service is accounted for".
    text = RUNNER.read_text()
    health_checked = {
        _defining_module(recipe)
        for recipe in re.findall(r"`just ([a-z0-9-]+) status`", text)
        + re.findall(r"`just ([a-z0-9-]+-status)`", text)
    }
    assert health_checked, "the runner health-checks no service at cockpit setup"

    assert health_checked <= _modules_started_by("herdr-up")


# NOT MECHANICALLY VERIFIABLE — deliberately not asserted here.
# The protocol also requires workers to record consult receipts in `result.json` under
# `consults`, and Stage 2 to audit them. Nothing enforces either half: `contracts.parse_result`
# does not read `consults` (RESULT_REQUIRED is order_id/verdict/full_log), so a worker that
# omits the key entirely still writes a valid result, and the audit is one LLM checking
# another LLM's self-report. The previous tests here asserted that this prose existed and
# called that "mechanical"; a substring in a markdown file is not a gate. The consult
# ANSWER contract is enforced and is pinned in
# .shared-llm/public/extensions/common/upagent/consult_answer_contract_test.py; the receipt
# regime becomes testable only once parse_result validates `consults` itself.


def test_workspace_mode_defaults_to_single_and_flag_restores_separate() -> None:
    runner = RUNNER.read_text()
    launcher = HERDR_JUSTFILE.read_text()
    controller = PLAN_CONTROLLER.read_text()

    assert "ws: herdr" in runner
    assert "--separate-workspaces" in runner
    assert "--separate-workspaces" in launcher
    assert "--separate-workspaces" in controller
    assert "UNIFIED_WORKSPACE_LABEL" in controller


def test_claude_tui_always_launches_with_remote_control() -> None:
    controller = PLAN_CONTROLLER.read_text()
    launcher = HERDR_JUSTFILE.read_text()

    assert "--remote-control=" in controller
    assert "--remote-control=<slug>" in launcher
