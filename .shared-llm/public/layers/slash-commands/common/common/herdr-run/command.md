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
ws: <slug>                     ← one run cockpit with role tabs
  tab: control                 ← primary view
    ├── tui-agent              you talk to the TUI
    └── phase-leader           current phase owner
  tab: workers                 ← active work
    └── stage UpAgent workers
  tab: oversight               ← inspect when needed
    ├── account managers
    ├── plan/phase watchdogs
    └── one-shot checkers

ws: shared-services            ← plan-agnostic · always up · peripheral
  ├── recruiter   (UpAgent Hub)    deterministic lifecycle and durable mailboxes
  └── librarian   (Specialist Hub) routes a question → transient specialist
```

1. The cockpit is the workspace holding this (tui-agent) pane. `just herdr-plan` has already created and health-checked this TUI. Read `<run-tree>/control/plan-start.json` and acknowledge `ready` (its `watchdog` block says `not-configured` by design — there is no standing plan-lifecycle-watchdog in coordination v2; a legacy run may still show `ready-degraded`, which is equally continuable). Liveness does not come from an observer agent: this TUI hears every phase condition — completion, blocked, crash, stall, quiet — as the typed return value of its own blocking `upagent-phase-await` call, and urgent unacknowledged events additionally escalate to the human through `herdr notification`. **The TUI has no authority to create, launch, prompt, adopt, or replace a watchdog agent or a phase leader.** Its sole phase-start authority is the controller command in the phase loop below; never attempt an ad-hoc monitoring repair.
2. Bring up the **UpAgent Recruiter** with `just upagent-up`. It ensures the visible `shared-services` Recruiter pane, validates the roster, persists its state, and starts a small deterministic Python supervisor for dead/expired leases. The pane is status/observability only; requesters use `just upagent-request` / `upagent-await`, never its shell. The roster still owns all pre-hardened harness launch templates.
3. Every order includes a `requester` (`id`, `kind`, `address`) and a caller-stable `request_id`; the Hub still assigns/scopes its durable identity. Each phase leader uses its own pane as requester and `cockpit_pane`. The Hub defaults to direct lifecycle: Python validates configuration, atomically starts the worker, returns `worker-healthy` after process/agent/cwd proof, and publishes durable requester mailbox events consumed by `upagent-await` / `upagent-await-any`. A roster may opt into `management.mode: dedicated` for the historical Account Manager pane. The worker itself receives no controller addresses.
4. Multiple Remote Control TUI sessions can drive the same run; this is a warning-only last-writer check, not a lock. Before each route edit, read the run-tree `route.yaml` marker `# last-edited-by: <session-id> @ <iso-ts>`; before writing, warn if it changed since that session last read it. Update that marker on every edit. Never put this marker in the origin route.
5. Do not start an ad-hoc LLM result poller, and never create a standing watchdog agent. Observability is layered deterministically: the blocking awaits (`upagent-phase-await` here, `upagent-await`/`upagent-await-any` in the leader) reconcile durable state against live Herdr state every sweep and return `leader-missing`/`leader-stalled`/`inactivity-checkpoint` events; the Recruiter launches fresh **one-shot** LLM checkers only at configured inactivity/anomaly checkpoints; urgent unacknowledged events escalate to the human via `herdr notification`. Phase startup is one Python transaction invoked through `just upagent-phase-start`; do not reproduce its pane operations manually. A leader startup failure is terminal and must be reported.
6. Keep cockpit geometry deterministic and role-based. The launcher names the TUI tab `control`;
   phase leaders stay there. The Recruiter moves active stage workers to `workers` and Account
   Managers (opt-in) and one-shot checkers to `oversight` before publishing their addresses.
   Role tabs are created lazily from the first live pane, not with empty placeholder shells.
   Workers split right; support roles split down. Resizing is bounded and presentation-only:
   report a warning and leave the agent in its source tab if Herdr cannot move or resize it, but
   never fail or alter a healthy worker lifecycle because of cockpit geometry.

## Phase loop

For each phase, in canonical order starting at `--start-phase` (respecting `--max-phases`):

1. **Start one complete phase transaction.** Run exactly `just upagent-phase-start <run-tree>/route.yaml <run-tree> <phase-id> <pass-number>` from the TUI pane. This is mandatory, not guidance. Do not call `herdr pane split`, `herdr agent start`, `herdr pane run`, launch an LLM, or `just upagent-request` yourself for phase startup. Do not send `/herdr-phase` to any pane yourself. Those actions create an unmanaged phase and are a protocol violation. Python validates the route and roster, starts the leader behind a filesystem gate, releases and health-checks the leader, updates `active-leader-panes.json`, and atomically writes `<phase>/pass-<n>/control/phase-start.json`.
2. **Require a terminal startup response.** Continue after `PHASE_STARTED` with a live `leader_pane` and `state: ready` (the receipt's `watchdog` block is `not-configured` by design; a legacy `state: ready-degraded` receipt is equally continuable). Any command failure or missing leader means the phase never started; report the recorded cause and stop. The controller closes a gated leader on leader-start failure, but never destroys a previously owned live leader.
3. **The controller hands the phase to the leader.** The gated launch carries exactly one `/herdr-phase --phase <phase-id> --plan <run-tree>/plan.md --route <run-tree>/route.yaml --run-root <run-tree>` assignment. The leader owns stages, Recruiter orders, stage-level backtracking, and `phase-status.md`.
4. **Wait inside the deterministic await — never by watching panes.** After `PHASE_STARTED`, block in exactly one repeated command:

   ```bash
   just upagent-phase-await <run-tree>/phases/<phase-id>/pass-<n>/control/phase-start.json <after> [timeout-ms]
   ```

   This is plain Python — no LLM turns are burned while blocked. It multiplexes the phase event journal, the leader's typed publications, the authoritative `phase-result.json`, and live Herdr state, then prints exactly one typed JSON event. Handle that event by `kind`, acknowledge it only after parsing (`just upagent-phase-ack <receipt> <event_id>`), and re-await with `after=<that event's sequence>` after every nonterminal event. An unacknowledged actionable event is redelivered by the next await, so a lost turn replays instead of disappearing. Never use `agent-status=done`: that marks a turn, not a phase. Never derive a verdict from pane scrollback; a `PHASE_RESULT` pane marker is display-only.

   | `kind` | terminal | TUI action |
   |---|---|---|
   | `completed` | yes | Validate `phases/<phase-id>/phase-result.json`, ack, record in `run-status.md`, advance. |
   | `failed` | yes | Ack; apply phase-level backtracking from the event/result `revisit` list. |
   | `blocked` | yes | The attempt is over. Read the evidence paths and `phase-result.json`, ack, destroy the leader, then decide: replay the phase as a fresh pass with the answer baked into its inputs, or stop for the human. |
   | `needs-input` | no | Advisory only until the owner-command channel lands: note the question in `run-status.md`; ack; re-await. A leader that cannot continue without the answer publishes `blocked` instead. |
   | `decision-required` | no | A descendant hit a work cap: `just upagent-respond … extend/cancel`; ack; re-await. |
   | `worker-warning` | no | Note in `run-status.md`; act only if it changes phase risk; ack; re-await. |
   | `leader-missing` | no | Verify the recorded evidence; clean up the dead leader mapping and replay the phase as a new pass, or stop for the human. |
   | `leader-stalled` | no | Durable state contradicts live status: inspect once; if truly stranded treat like `leader-missing`, else ack and re-await. |
   | `inactivity-checkpoint` | no | Quiet too long: request one bounded checker/inspection; ack; re-await. |
   | `advisory` | no | Read the observer evidence; act only when it changes risk; ack; re-await. |
   | `startup-ready` / `startup-degraded` | no | Record observability state; re-await. |
   | `soft-timeout` | no | Extend or cancel within the decision window; ack; re-await. |
   | `hard-timeout` | yes | Enforced stop: record the enforcement evidence; treat the phase as failed. |
   | `cancelled` | yes | Record who cancelled and why; stop or replay per authority. |
   | `await-heartbeat` | no | Quiet and healthy: re-await immediately and silently — never narrate heartbeats to the human. |

5. **Read `phase-result.json` for detail.** The durable file supplies the verdict and evidence; the event is the wake-up, not the record.
6. **Destroy the phase leader unconditionally.** After result/evidence handling, close the recorded leader pane and remove its mapping. A replay creates a fresh leader and a fresh `phase-start.json` receipt for the new pass's await.
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

After that summary exists, you **MUST** publish the terminal lifecycle fact through the controller:

```bash
just herdr-plan-finish <exact-run-tree> succeeded
```

Use `stopped` instead of `succeeded` for any non-successful terminal outcome. This command is
mandatory and must run before you wait for support panes to close or print the final message. Do
not write `control/run-terminal.json` yourself. If the command fails, report that exact lifecycle
failure and do not claim the workspace is safe to close. The marker is the run's only terminal
authority (any in-flight legacy watchdog also retires from it); quiet panes and completed turns
are never completion authority.

Keep the final TUI message deliberately short. After writing the durable summary, wait a bounded
interval for every managed run pane except this TUI to close. Then use exactly one of these forms:

```text
SUCCESS — Everything succeeded. Safe to close this workspace.
Details: <absolute run-status.md path>
```

```text
SUCCESS — Everything succeeded. Cleanup is still finishing; leave this workspace open.
Details: <absolute run-status.md path>
```

```text
STOPPED — This run did not succeed. See: <absolute run-status.md path>
```

Do not print a stage-by-stage recap, model list, commit narrative, verification transcript, or
implementation caveats in the final TUI message. Those details belong only in `run-status.md` and
the run tree. The terminal message exists solely to make outcome and close-safety unmistakable.

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`.
2. Canonical plan body stays clean; the route profile owns profiles, agents, accuracy, and budgets.
3. Do not auto-convert at execution time. `/herdr-run` only runs an already-runnable `plan.md + route.yaml`.
4. Stage work is done by workers hired through the Recruiter — never by native subagents or Claude team mode. The only exceptions are small, disposable non-stage helpers for watchdog monitoring or static status rendering; they perform no stage work, do not delegate, and return only their alert or artifact path.
5. The run tree files (`phase-result.json`, `result.json`, the `*-status.md` logs) are the source of truth; pane scrollback is live-view only.
6. Stay small: decide re-run/continue and delegate hard calls to the advisor when configured.
7. Do not push, deploy, reset, or revert unless the plan and rollback policy explicitly allow it; a true revert stops for the human.
