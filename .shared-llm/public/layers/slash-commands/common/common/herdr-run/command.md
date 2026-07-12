# /herdr-run

Run a checked `plan.md + route.yaml` pair end to end through the Herdr-native meta runner. This is the kickoff command — the **TUI agent** — the one pane you talk to. It sets up the run cockpit, loops the plan's phases, creates one phase leader per phase, and applies phase-level backtracking and escalation. It stays small: it decides only whether a phase re-runs or the run continues, and delegates every hard evaluation to an advisor worker (when configured) rather than doing the work itself.

## Invocation

```text
/herdr-run --plan <plan.md> --route <route.yaml> [--run-root <dir>] [--slug <name>] [--start-phase <N>] [--max-phases <N>]
```

- `--plan <plan.md>` — canonical meta plan. Routing stays out of the plan body.
- `--route <route.yaml>` — route profile with `llm_profiles`, inline `agent` names, per-phase `accuracy`, and optional `advisor_profile`/budgets.
- `--run-root <dir>` — optional root under which the run tree is written. Defaults to the repo's configured work-log root, or a local `./.herdr-runs/` when none is configured.
- `--slug <name>` — optional run name. Defaults to a slug derived from the plan title.
- `--start-phase <N>` — optional phase to start from. Default `0`.
- `--max-phases <N>` — optional safety cap.

All required flags must be present. Fail loud rather than guessing.

## Pre-flight

1. Verify `HERDR_ENV=1`. If not, stop with: `ERROR: /herdr-run must run inside a Herdr-managed pane.`
2. Run `herdr pane list` to identify the current pane — this pane is the **tui-agent** (the bottom, full-width pane of the cockpit; the one the human talks to). Do not control Herdr from outside Herdr.
3. Validate the installed Herdr command surface (`herdr workspace list/create`, `herdr pane list/split/run/read/close`, `herdr wait output`, `herdr wait agent-status`). Adapt only after validating any local syntax differences.
4. Read the plan and route profile and run the same gate as `/meta-plan-check <plan.md> <route.yaml>`. If it fails, stop **before** creating any workspace and tell the user to run `/meta-plan-convert` or fix the files. The check must confirm: canonical plan shape; `llm_profiles` defined; every phase to run has `lead.llm_profile`, `lead.agent`, `merge_back_at`, and its stage entries; `stage-0-alignment` present iff `accuracy: high`; worktree branch template, green checks, and log checks configured; all referenced profiles exist; each named agent resolves; Stage 2 (and stage-0's audit when high) independent from Stage 1.
5. Resolve `<slug>` and `<run-root>`, then create the run tree root `<run-root>/<date>/<slug>/`. Freeze the originals into it (`plan.md`, `route.yaml`, and `research.md` if present) as versioned copies, and initialize `run-status.md`.
6. Record a Git baseline: branch/worktree identity, status, and phase-owned file manifest if the plan provides one.

## Cockpit + shared-services setup

The runtime topology is one cockpit workspace plus one always-up peripheral workspace:

```text
ws: <slug>                     ← the run cockpit, one screen
  ┌──────────────────────┬──────────────────────┐
  │  phase-leader        │  worker              │  top-left: leader (replaced per phase)
  │  (top-left)          │  (top-right)         │  top-right: ONE worker at a time
  ├──────────────────────┴──────────────────────┤
  │  tui-agent  (bottom, full width)            │  bottom: you talk HERE
  └─────────────────────────────────────────────┘

ws: shared-services            ← plan-agnostic · always up · peripheral
  ├── recruiter   (UpAgent Hub)    takes a leader's order → spawns the worker INTO <slug> top-right
  └── librarian   (Specialist Hub) routes a question → transient specialist
```

1. The cockpit is the workspace holding this (tui-agent) pane. Confirm or create it as `<slug>`. The phase-leader pane (top-left) is created and destroyed per phase; the worker pane (top-right) is created by the Recruiter, one worker at a time — so the cockpit never exceeds three panes.
2. Bring up the **UpAgent Recruiter** by running `just upagent-up` at startup. It ensures the always-up `shared-services` workspace, arms a `recruit` shell function in the Recruiter pane against the resolved roster, and prints/persists `{workspace_id, recruiter_pane, roster}` (to `/tmp/.upagent/recruiter.json` by default, overridable via `UPAGENT_STATE`). Capture the printed `recruiter_pane` id — this is the address every phase leader signals with `herdr pane run <recruiter_pane> "recruit <order.json>"`. `just upagent-up` is idempotent: it reuses an existing `shared-services` and re-arms the pane. The roster it arms (the repo-owned `upagent.yaml`) holds pre-hardened per-harness launch templates — non-interactive bypass flags, harness-native model/effort flags, and an insulated pi launch (`--no-extensions` plus an explicit `-e` for Herdr's pi integration so agent-status reporting stays alive) — so neither the TUI nor a phase leader ever hand-crafts or "improves" a worker launch command: the route picks the profile, the roster does the launching. Bring up the **Specialist Hub Librarian** in the same workspace the same way when repo consults are configured.
3. Each phase leader discovers `recruiter_pane` by reading the persisted UpAgent state file (`/tmp/.upagent/recruiter.json`, or `UPAGENT_STATE`) that `just upagent-up` wrote — so the leader signals the right pane without it being passed as a flag. The leader stamps its own live cockpit pane id into every order as `cockpit_pane` (since `herdr pane split` splits an existing source pane and has no `--workspace` flag), and the Recruiter spawns each worker by splitting from that `cockpit_pane` into the cockpit, beside the leader that ordered it.

## Phase loop

For each phase, in canonical order starting at `--start-phase` (respecting `--max-phases`):

1. **Create one phase leader.** Split a top-left pane in the cockpit and launch the phase's `lead.llm_profile` + `lead.agent` there. Assemble the launch command from the validated route profile plus local harness capability; do not hard-code unsupported model names.
2. **Hand the phase to the leader.** Send exactly one `/herdr-phase --phase <phase-id> --plan <plan.md> --route <route.yaml> --run-root <run-root>` invocation to the phase-leader pane. The leader owns the phase's stages, the Recruiter orders, stage-level backtracking, and `phase-status.md`.
3. **Wait for the leader to finish, always bounded.** Use `herdr wait agent-status <leader-pane> --status done --timeout <ms>`. Every `herdr wait` MUST pass `--timeout` — without it Herdr's wait blocks forever. On timeout, read and validate `phases/<phase-id>/phase-result.json`: if it is present and valid, proceed with it; if it is missing or malformed, mark the phase `blocked` and stop for the human rather than looping on a wait that will never return.
4. **Read `phase-result.json`.** It is the source of truth; pane output is evidence only.
5. **Destroy the phase leader.** Close the top-left pane only after the result and evidence are persisted. One leader per phase — a re-run creates a fresh leader.
6. Append a `run-status.md` line for the phase outcome (phase id, pass number, verdict, and any `revisit`).

## Phase-level backtracking (forward-only)

The TUI backtracks phases; the leader backtracks stages. Both replay **forward, in order** — nothing is reverted.

- A passing `phase-result.json` advances to the next phase.
- A failing `phase-result.json` carries `revisit: [phase-ids]`. The TUI replays from the **earliest** pointed phase forward as a new pass, incrementing the pass count, re-creating a fresh leader for each replayed phase. Already-good later phases are only re-run if they are pointed to.

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
4. Stage work is done by workers hired through the Recruiter — never by native subagents or Claude team mode.
5. The run tree files (`phase-result.json`, `result.json`, the `*-status.md` logs) are the source of truth; pane scrollback is live-view only.
6. Stay small: decide re-run/continue and delegate hard calls to the advisor when configured.
7. Do not push, deploy, reset, or revert unless the plan and rollback policy explicitly allow it; a true revert stops for the human.
