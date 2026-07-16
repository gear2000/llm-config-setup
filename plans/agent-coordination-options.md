# Agent Coordination Improvements — Options & Open Questions

> Status: **design settled in terminal dialogue** — this is now the implementation plan.

## Context

Every agent-to-agent notification today ended in "wait for the target to be idle, then paste into its terminal pane." A TUI busy waiting on a phase is never idle, so watchdogs cannot deliver and the system can deadlock. Coordination v2 changes the rule: LLMs wait inside blocking code whose return value is the wake-up. Files are authoritative; pane text is observational.

## Settled architecture

```text
HUMAN
└── TUI (owns the plan)
    ├── upagent-phase-start → launches PHASE LEADER
    └── upagent-phase-await ← blocks in plain code until one typed phase event
        └── PHASE LEADER
            ├── upagent-request → RECRUITER validates, leases, launches worker
            └── upagent-await / upagent-await-any ← blocks until worker movement
```

Waiting rule at both boundaries: block → typed event → act → acknowledge → re-await. Heartbeat is a normal return; it is not completion.

## Decisions locked

- No native sub-agents in the meta-runner path.
- No hooks and no MCP in round one; CLI plus durable files only.
- No new hub or socket; Herdr remains observe/present, while transport is files plus blocking CLI.
- No standing plan or phase watchdogs. Deterministic awaits reconcile durable state against live Herdr state.
- Account Manager is no longer default. Direct Python lifecycle owns validation, lease, monitor, timeout mechanics, and requester mailbox events. `management.mode: dedicated` keeps the historical path as opt-in.
- Ambiguous inactivity is handled by disposable one-shot checkers; urgent unacknowledged events escalate via `herdr notification`.

## Implementation slices

- [x] Event, command, and acknowledgement contracts in `contracts.py`.
- [x] `phase_await.py` for phase journal/result/live-state rendezvous.
- [x] `recruiter.py await-any` for multiplexed worker waits.
- [x] Direct lifecycle default with dedicated managers retained as opt-in.
- [x] Plan and phase startup no longer hire watchdogs; receipts retain `watchdog: not-configured` for compatibility.
- [x] `/herdr-run`, `/herdr-phase`, and protocol docs rewritten around typed awaits.

## Decisions from the 2026-07-16 review

- A `blocked` phase ends its attempt. The owner reads the evidence, decides, and replays the phase as a fresh pass. The blocked event is terminal in the journal, which also kills the bug where a dismissed blocked alert re-fired forever.
- The owner→leader command contract (`parse_phase_command`) stays in `contracts.py` as groundwork. Nothing consumes it yet; the docs say so. A leader that needs an answer before it can continue publishes `blocked`, never `needs-input`.
- Accepted round-one limit: if the TUI itself dies, nothing watches it. All reconciliation and human escalation runs inside the TUI's own blocking await. Known and accepted; revisit in a later round.
- A failed `herdr notification` to the human is retried on the next sweep instead of being marked as delivered.
- Tests now cover the direct lifecycle default, `phase_await.py`, `await-any`, the coordination contracts, and the leader-start failure cleanup.

## Acceptance

A healthy run can complete with no watchdog and no manager. Every actionable condition known to the controller is represented as a typed event or durable receipt, and no owner relies on another agent typing into its pane for delivery.
