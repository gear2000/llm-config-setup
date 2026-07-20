# Code Context

## Files Retrieved

1. `.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md` (lines 1-180) — plan-level TUI authority, phase loop, recovery, typed events, terminal authority, UI.
2. `.shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md` (lines 1-171) — phase-leader/stage contract, routing, UpAgent orders, review gates, durable results, retry ladder.
3. `.shared-llm/public/extensions/common/herdr/plan_controller.py` (lines 1-360) — deterministic Claude/Pi TUI launch and durable terminal marker.
4. `.shared-llm/public/extensions/common/upagent/phase_controller.py` (lines 1-9, 250-330, 390-505) — atomic, gated, ownership-checked phase-leader startup.
5. `.shared-llm/public/extensions/common/upagent/phase_await.py` (lines 1-32, 140-240, 270-368) — durable typed journal, reconciliation, redelivery, liveness and notification.
6. `.shared-llm/public/extensions/common/herdr/justfile` (full file) — Herdr/UpAgent bring-up, status table, plan start/finish CLI entry points.
7. `.shared-llm/public/llm/codex/common/hooks/herdr-status-fix.sh` (full file) — Codex sidebar/live-state repair; result-file polling remains correctness fallback.
8. `herdr-config.toml` (full file) — minimal local Herdr UI configuration.
9. `.shared-llm/public/extensions/common/herdr/plan_controller_test.py` (full file) — TUI health, unified/separate workspace, no-watchdog and terminal-marker tests.
10. `.shared-llm/public/extensions/common/upagent/phase_controller_test.py`, `.shared-llm/public/extensions/common/upagent/phase_await_test.py` — phase transaction and recovery tests.
11. `tools/test_herdr_phase_protocol.py` (full file) — static command/protocol invariants and an explicit unmechanized consult-gate caveat.
12. `tools/test_planner_herdr_handoff.py`, `tools/test_planner_herdr_regression.py` (full files) — canonical planner-to-Herdr handoff and generated-surface regression coverage.

## Key Code

### Execution/authority split

- **Plan-with-phases:** `/herdr-run` is the human-facing Claude Code TUI and owns only phase start, phase replay/advance, escalation and final lifecycle publication; hard judgments route to advisors (`herdr-run/command.md:1-3,108-130`).
- **Phase-with-stages:** one fresh phase leader owns stage ordering, stage retries and `phase-result.json`; it may not spawn native subagents/teams (`herdr-phase/command.md:1-3,162-171`).
- **Mechanical startup authority:** the TUI must invoke `just upagent-phase-start`; Python alone starts a gated leader, records identity, releases it, then health-checks it. Existing live ownership is refused (`phase_controller.py:407-430,439-505`).
- **Worker lifecycle authority:** only UpAgent Hub launches/cleans workers; the route—not Recruiter discretion—selects harness/model/effort/agent (`herdr-phase/command.md:59-75,164-171`). Requester control tokens authorize extension/cancellation.

### Durability and recovery

- Frozen `plan.md`; run-tree `route.yaml` is the single live route; origin is read-only (`herdr-run/command.md:26-28`; `herdr-phase/command.md:13-15`).
- Source of truth hierarchy: private worker result → validated/atomically published public `result.json` + receipt → `phase-result.json` → `run-status.md` → controller-written `control/run-terminal.json`. Pane output and `agent-status=done` are explicitly non-authoritative (`herdr-phase/command.md:48-59,157-160`; `herdr-run/command.md:104-106,151-164`).
- Phase events are schema-validated, sequence-numbered, atomically journaled, acked, deduplicated and redelivered until resolved (`phase_await.py:150-185,292-304`). Await reconciles leader liveness and durable results, emits `leader-missing`, `leader-stalled`, inactivity checkpoints and heartbeats, and escalates unacked urgent events through `herdr notification` (`phase_await.py:307-368`).
- Recovery is **forward-only**: stage failures revisit earliest named stage; phase failures revisit earliest named phase; fresh try/pass and leader are created. No automated revert of merged history (`herdr-phase/command.md:138-153`; `herdr-run/command.md:108-147`).
- Important limitation: there is no general owner→leader answer channel. Blocking questions must terminate the attempt as `blocked` and be baked into a fresh pass; only IaC has a durable approval-file channel (`herdr-phase/command.md:95-98,128-135`). **Severity: medium operational friction.**

### Routing and review gates

- Route validation requires every phase lead and five base stages, explicit profiles/agents, harness-native model IDs, merge timing, checks and auditor independence. `high` adds three-worker stage-0 alignment; `max` adds a second stage-2 auditor on a different model/harness (`herdr-phase/command.md:22-30,100-118`).
- Stage lifecycle: implementation → independent adversarial audit → integration seams → upstream verification → mandatory finalization/green checks/log inspection/cleanup (`herdr-phase/command.md:110-120`).
- Stage and phase budgets escalate to route-selected advisor decisions (`continue|loop|stop-ask-human`), otherwise human stop (`herdr-phase/command.md:138-153`; `herdr-run/command.md:115-130`).
- IaC approval is human-only, binds approval/apply receipt to the exact plan SHA, and applies from TUI rather than a normal stage worker (`herdr-run/command.md:132-143`).

### Claude Code UI/observability

- Claude TUI launches non-interactively with `--dangerously-skip-permissions` and always `--remote-control=<slug>`; default topology is one `herdr` workspace with role tabs (`control`, `workers`, `oversight`, persistent `services`), with separate workspaces opt-in (`plan_controller.py`; `herdr-run/command.md:30-45`).
- Herdr panes/sidebar are live views, not transport. Blocking filesystem awaits consume no LLM turns. Static HTML status snapshots are delegated on lifecycle changes/hourly (`herdr-run/command.md:86-106`).
- No standing LLM watchdog exists in coordination v2. Deterministic await plus one-shot anomaly checkers and human notifications replace it (`phase_controller.py:4-9`; `phase_await.py:307-368`).

## Architecture

`just herdr-up` starts the durable UpAgent Recruiter service. `just herdr-plan` calls `plan_controller.py`, which creates/joins the cockpit and health-checks a Claude Code TUI. The TUI executes `/herdr-run`, validates/finalizes the run tree, and calls the UpAgent phase controller once per phase. That controller atomically creates and proves one phase leader. The leader executes `/herdr-phase`, converting each stage into a strict UpAgent order. UpAgent owns leases, worker launch/health, durable mailbox/result publication, cleanup and specialist consult brokerage. The leader reduces stage evidence into `phase-result.json`; `phase_await.py` wakes the TUI with typed journal events. The TUI reduces phases into `run-status.md`, then only `herdr-plan-finish` may publish the terminal marker.

### Known complexity / risks

- **High:** behavior spans large prose protocols interpreted by LLMs plus Python controllers/ledger and Herdr pane state. Static substring tests prevent contract drift but do not prove an LLM follows the protocol end-to-end.
- **Medium:** `tools/test_herdr_phase_protocol.py` explicitly notes consult presence/owed-ness is not fully mechanical: Python verifies claimed receipts, but whether a consult was owed remains an auditor judgment, and omission is not contract-rejected.
- **Medium:** multiple Remote Control sessions use warning-only last-writer route markers, not locking; concurrent human edits can race.
- **Low/operational:** Codex live status needed a custom hook because native session reporting could leave waits/sidebar stale; durable result polling is the correctness fallback (`herdr-status-fix.sh`).
- No live Herdr/Claude end-to-end run was performed; tests are unit/static composition tests.

## Must-Not-Break Invariants

1. Only the Python phase-start transaction may create/release a phase leader; TUI ad-hoc pane/agent launch is a protocol violation.
2. Only UpAgent Hub may execute worker lifecycle; stage workers are terminal, non-delegating, and never Claude native subagents/team mode.
3. Route is authoritative for harness/model/effort/agent and audit independence; never let Recruiter infer routing.
4. Durable files/receipts/journals outrank pane scrollback, idle/done state, and display markers.
5. Startup and publication remain atomic and ownership-fenced; never close/adopt foreign or prior live panes.
6. Ack/redelivery semantics and stable globally scoped request IDs/control tokens must survive interruption.
7. Stage 2 remains independent; Stage 5 always runs; `max` requires two independent auditors.
8. Backtracking remains forward-only; true reset/revert and destructive/apply authority stay human-gated.
9. Success requires all matching phase results passed, a summary, and controller-authored terminal marker; quiet panes are not completion.
10. Unified-workspace close guidance must preserve the persistent services tab.

## Start Here

Open `.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md` first. It defines the top-level authority and phase-within-plan state machine; then follow its phase-start/await calls into `upagent/phase_controller.py` and `upagent/phase_await.py`, and finally read `herdr-phase/command.md` for stage-within-phase behavior.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Concrete authority, durability, recovery, routing, gates, UI, complexity findings cite exact repository files and line ranges; risks include severity."
    }
  ],
  "changedFiles": [
    ".lavish/research-herdr.md"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "pytest -q .shared-llm/public/extensions/common/herdr/plan_controller_test.py .shared-llm/public/extensions/common/upagent/phase_controller_test.py .shared-llm/public/extensions/common/upagent/phase_await_test.py tools/test_herdr_phase_protocol.py",
      "result": "passed",
      "summary": "48 passed in 0.40s"
    },
    {
      "command": "pytest -q tools/test_planner_herdr_handoff.py tools/test_planner_herdr_regression.py",
      "result": "passed",
      "summary": "11 passed in 0.64s"
    },
    {
      "command": "git diff --cached --quiet",
      "result": "passed",
      "summary": "Exit 0; no staged changes."
    },
    {
      "command": "git status --short",
      "result": "passed",
      "summary": "Pre-existing unstaged/untracked files observed; none staged."
    }
  ],
  "validationOutput": [
    "48 passed in 0.40s",
    "11 passed in 0.64s",
    "git diff --cached --quiet exit=0"
  ],
  "residualRisks": [
    "No live Herdr/Claude end-to-end execution was run.",
    "Some orchestration guarantees are prose/static-contract enforced rather than mechanically end-to-end enforced.",
    "Consult owed-ness and omitted consult lists are not fully mechanically gated."
  ],
  "noStagedFiles": true,
  "notes": "Only the requested research artifact was written. Existing unrelated working-tree changes were not modified."
}
```
