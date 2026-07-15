# /herdr-run

Run a checked `plan.md + route.yaml` pair end to end through the Herdr-native meta runner. This is the kickoff command — the **TUI agent** — the one pane you talk to. It sets up the run cockpit, loops the plan's phases, creates one phase leader per phase, and applies phase-level backtracking and escalation. It stays small: it decides only whether a phase re-runs or the run continues, and delegates every hard evaluation to an advisor worker (when configured) rather than doing the work itself.

## Invocation

```text
/herdr-run --plan <plan.md> --route <route.yaml> [--run-tree <exact-dir> | --run-root <parent-dir>] [--slug <name>] [--start-phase <N>] [--max-phases <N>]
```

- `--plan <plan.md>` — canonical meta plan. Routing stays out of the plan body.
- `--route <route.yaml>` — route profile with `llm_profiles`, inline `agent` names, per-phase `accuracy`, and optional `advisor_profile`/budgets.
- `--run-tree <exact-dir>` — exact, already-created run directory containing the supplied `plan.md` and `route.yaml`. `just herdr-plan` always supplies this; use it directly and never create another dated/slug directory around it.
- `--run-root <dir>` — optional root under which the run tree is written. Defaults to the repo's configured work-log root, or a local `./.herdr-runs/` when none is configured.
- `--slug <name>` — optional run name. Defaults to a slug derived from the plan title.
- `--start-phase <N>` — optional phase to start from. Default `0`.
- `--max-phases <N>` — optional safety cap.

`--run-tree` and `--run-root` are mutually exclusive. All required flags must be present. Fail loud rather than guessing.

## Pre-flight

1. Verify `HERDR_ENV=1`. If not, stop with: `ERROR: /herdr-run must run inside a Herdr-managed pane.`
2. Run `herdr pane list` to identify the current pane — this pane is the **tui-agent** (the top, full-width pane of the cockpit; the one the human talks to). Do not control Herdr from outside Herdr.
3. Validate the installed Herdr command surface (`herdr workspace list/create`, `herdr pane list/split/run/read/close`, `herdr wait output`, `herdr wait agent-status`). Adapt only after validating any local syntax differences.
4. Read the plan and route profile and run the same gate as `/meta-plan-check <plan.md> <route.yaml>`. If it fails, stop **before** creating any workspace and tell the user to run `/meta-plan-convert` or fix the files. The check must confirm: canonical plan shape; `llm_profiles` defined; every phase to run has `lead.llm_profile`, `lead.agent`, `merge_back_at`, and its stage entries; `stage-0-alignment` present iff `accuracy: high`; worktree branch template, green checks, and log checks configured; all referenced profiles exist; each named agent resolves; Stage 2 (and stage-0's audit when high) independent from Stage 1.
5. Resolve the run tree without guessing. With `--run-tree`, require that its resolved path is the common parent of the supplied `plan.md` and `route.yaml`, use that directory exactly, and treat those files as already frozen. Without it, resolve `<slug>` and `<run-root>`, create `<run-root>/<date>/<slug>/`, and freeze the originals into it (`plan.md`, `route.yaml`, and `research.md` if present). Initialize `run-status.md`. The run-tree `route.yaml` is the **single live route copy** for this run; the origin passed by `--route` is historical/read-only after this point. All mid-run route changes apply only to the run-tree copy.
6. Record a Git baseline: branch/worktree identity, status, and phase-owned file manifest if the plan provides one.

## Cockpit + shared-services setup

The runtime topology is one cockpit workspace plus one always-up peripheral workspace:

```text
ws: <slug>                     ← the run cockpit, one screen
  ┌──────────────────────┬───────────────────────┐
  │ tui-agent            │ plan-lifecycle-watchdog │  you talk to the TUI
  ├──────────────────────┼───────────────────────┤
  │ phase-leader         │ stage/observer worker   │
  ├──────────────────────┼───────────────────────┤
  │ account manager      │ one-shot checker        │  role-balanced grid
  └──────────────────────┴───────────────────────┘

ws: shared-services            ← plan-agnostic · always up · peripheral
  ├── recruiter   (UpAgent Hub)    deterministic lifecycle and durable mailboxes
  └── librarian   (Specialist Hub) routes a question → transient specialist
```

1. The cockpit is the workspace holding this (tui-agent) pane. `just herdr-plan` has already created and health-checked this TUI, then asked the Recruiter to place one `plan-lifecycle-watchdog` and its Dedicated Account Manager beside it. Read `<run-tree>/control/plan-start.json` and acknowledge `ready` or `ready-degraded`; never wait forever for monitoring. The plan watchdog observes this TUI, discovers every phase leader, and advises both sides on state transitions. **The TUI has no authority to create, launch, prompt, adopt, or replace the plan watchdog, a phase leader, or a phase watchdog.** Its sole phase-start authority is the controller command in the phase loop below. A manually started `/herdr-run` may lack a plan watchdog; record that degraded condition and continue instead of attempting an ad-hoc repair.
2. Bring up the **UpAgent Recruiter** with `just upagent-up`. It ensures the visible `shared-services` Recruiter pane, validates the roster, persists its state, and starts a small deterministic Python supervisor for dead/expired leases. The pane is status/observability only; requesters use `just upagent-request` / `upagent-await`, never its shell. The roster still owns all pre-hardened harness launch templates.
3. Every order includes a `requester` (`id`, `kind`, `address`) and a caller-stable `request_id`; the Hub still assigns/scopes its durable identity. Each phase leader uses its own pane as requester and `cockpit_pane`. The Hub creates a Dedicated Account Manager, validates configuration, atomically starts the worker, and returns `worker-healthy` only after process/agent/cwd plus LLM startup assessment agree. Completion flows worker result → account manager explanation → Recruiter receipt → requester. The worker itself receives no controller addresses.
4. Multiple Remote Control TUI sessions can drive the same run; this is a warning-only last-writer check, not a lock. Before each route edit, read the run-tree `route.yaml` marker `# last-edited-by: <session-id> @ <iso-ts>`; before writing, warn if it changed since that session last read it. Update that marker on every edit. Never put this marker in the origin route.
5. Do not start an ad-hoc LLM result poller. The Recruiter owns deterministic checks and launches fresh one-shot LLM checkers only at configured inactivity/anomaly checkpoints. The plan watchdog owns run-level TUI↔leader observability; each phase watchdog owns one leader↔descendants view. Both are managed workers with Dedicated Account Managers, both are advisory, and neither may close or advance a pane. Phase startup is one Python transaction invoked through `just upagent-phase-start`; do not reproduce its pane operations manually. A leader startup failure is terminal and must be reported. A watchdog-only failure returns `ready-degraded`: record the warning and continue the phase.
6. Keep cockpit geometry deterministic and role-based. Workers split right; Account Managers,
   one-shot checkers, and phase leaders split down. After atomic startup, the Recruiter targets 28%
   of the local horizontal split for watchdogs and 20% of the local vertical split for managers and
   checkers. Resizing is bounded and presentation-only: report a warning if Herdr cannot resize,
   but never fail or alter a healthy worker lifecycle because of cockpit geometry.

## Phase loop

For each phase, in canonical order starting at `--start-phase` (respecting `--max-phases`):

1. **Start one complete phase transaction.** Run exactly `just upagent-phase-start <run-tree>/route.yaml <run-tree> <phase-id> <pass-number>` from the TUI pane. This is mandatory, not guidance. Do not call `herdr pane split`, `herdr agent start`, `herdr pane run`, launch an LLM, or `just upagent-request` yourself for phase startup. Do not send `/herdr-phase` to any pane yourself. Those actions create an unmanaged phase and are a protocol violation. Python validates the route and roster, starts the leader behind a filesystem gate, creates the watchdog order, records whether the watchdog is healthy or unavailable, releases and health-checks the leader, updates `active-leader-panes.json`, and atomically writes `<phase>/pass-<n>/control/phase-start.json`.
2. **Require a terminal startup response, never a perfect watchdog.** Continue after `PHASE_STARTED` with a live `leader_pane`. `state: ready` includes watchdog `manager_pane` + `worker_pane`; verify the manager and watchdog panes are in the same cockpit workspace as the leader and TUI using the receipt's workspace ids. Surface those pane addresses in the TUI status instead of hiding them. `state: ready-degraded` includes the explicit watchdog failure and is allowed to continue. Any command failure or missing leader means the phase never started; report the recorded cause and stop. The controller closes a gated leader on leader-start failure, but never destroys a previously owned live leader.
3. **The controller hands the phase to the leader.** The gated launch carries exactly one `/herdr-phase --phase <phase-id> --plan <run-tree>/plan.md --route <run-tree>/route.yaml --run-root <run-tree>` assignment. The leader owns stages, Recruiter orders, stage-level backtracking, and `phase-status.md`.
4. **Wait for authoritative completion, always bounded.** The phase watchdog alerts this TUI when the exact `phase-result.json` becomes terminal or when the leader is stranded. A `PHASE_RESULT` pane marker is an accelerator only. Never use `agent-status=done`: that marks a turn, not a phase. On the bounded deadline, read and validate `phases/<phase-id>/phase-result.json`; proceed only if valid, otherwise mark the phase `blocked` and stop for the human.
5. **Read `phase-result.json` for detail.** The durable file supplies the verdict and evidence.
6. **Destroy the phase leader unconditionally.** After result/evidence handling, close the recorded leader pane and remove its mapping. The watchdog finishes from the same terminal phase file and its Recruiter lifecycle cleans it up. A replay creates a fresh leader and watchdog.
7. Append a `run-status.md` line for the phase outcome (phase id, pass number, verdict, and any `revisit`) before acting on it. On every start/pass/fail/backtrack — or hourly if unchanged — delegate a minimal static HTML snapshot to a small disposable, non-stage helper. Give it only the status/result paths; it returns only the artifact path.

## Phase-level backtracking (forward-only)

The TUI backtracks phases; the leader backtracks stages. Both replay **forward, in order** — nothing is reverted.

- A passing `phase-result.json` advances to the next phase.
- A failing `phase-result.json` carries `revisit: [phase-ids]`. The TUI replays from the **earliest** pointed phase forward as a new pass, incrementing the pass count, first applying the same unconditional prior-leader cleanup and then creating a fresh leader for each replayed phase. Already-good later phases are only re-run if they are pointed to.

## Escalation ladder

Applied at the phase level, mirroring the leader's stage-level ladder:

```text
phase fails → phase-result.json.verdict=failed, revisit=[phase-ids]
TUI: replay from earliest revisit phase forward (new pass); increment pass
  phase_pass_budget hit (default 3) →
    advisor_profile set?  place an advisor order via the Recruiter (context = run-status.md);
                          advisor result.json: verdict=passed, decision = continue | loop | stop-ask-human
    no advisor_profile →  stop-ask-human
stop-ask-human → the TUI halts and surfaces status to the human
```

- The advisor is hired like any worker — placed as an order to the Recruiter on `advisor_profile`, reading `run-status.md`, writing no code and running no commands. A phase-level advisor order sets `phase_id` to the failing phase, but its `stage_id` must still be a **recognized** stage id (the order contract rejects anything outside the six) — a phase has a `phase_id`, not a `stage_id`, so there is nothing "phase" to reuse. Use the fixed convention `stage-5-finalization` (the whole-phase judgment stage) as the `stage_id`; never write a `phase_id` into `stage_id`, and there is no `stage_id: "advisor"`. The TUI knows it placed an advisor order and reads `result.json.decision`. The TUI stays small: it never performs the hard evaluation itself when an advisor is configured.
- The advisor worker writes a normal `result.json` with `verdict: passed` **plus** the optional `decision` field, one of the exact tokens `continue`, `loop`, or `stop-ask-human` (the contract's `ADVISOR_DECISIONS`). The TUI reads `result.json.decision`, not a special verdict: `continue` accepts the phase as good enough and advances; `loop` grants another pass (reset/extend the budget); `stop-ask-human` halts and surfaces to the human.

## Forward-only — no revert automation

This is a Ralph-style forward loop. Nothing already produced is discarded to go back, and no merged history is rewound. A **true revert** (throwing away merged work) is a major decision — the TUI **STOPs and asks the human**; it is never automated.

## Completion

When every in-scope phase has a passing `phase-result.json` (or the human has resolved a STOP), write a final `run-status.md` summary: the phase order actually run, passes and backtracks per phase with reasons, the run tree root, and the overall verdict. The run tree under `<run-root>/<date>/<slug>/` is the durable record.

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`.
2. Canonical plan body stays clean; the route profile owns profiles, agents, accuracy, and budgets.
3. Do not auto-convert at execution time. `/herdr-run` only runs an already-runnable `plan.md + route.yaml`.
4. Stage work is done by workers hired through the Recruiter — never by native subagents or Claude team mode. The only exceptions are small, disposable non-stage helpers for watchdog monitoring or static status rendering; they perform no stage work, do not delegate, and return only their alert or artifact path.
5. The run tree files (`phase-result.json`, `result.json`, the `*-status.md` logs) are the source of truth; pane scrollback is live-view only.
6. Stay small: decide re-run/continue and delegate hard calls to the advisor when configured.
7. Do not push, deploy, reset, or revert unless the plan and rollback policy explicitly allow it; a true revert stops for the human.
