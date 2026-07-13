# /herdr-phase

Run one phase as the Herdr-native **phase leader**. This command is sent to a bottom-left cockpit pane by `/herdr-run` (below the full-width tui-agent row), once per phase; the Recruiter's worker pane splits off to its right (bottom-right). The leader runs the phase's stages by placing a work ORDER per stage to the UpAgent Recruiter — never by spawning a native or nested subagent — applies stage-level backtracking and the stage-try budget ladder, maintains `phase-status.md`, and writes `phase-result.json` (the source of truth the TUI reads).

## Invocation

```text
/herdr-phase --phase <phase-id> --plan <plan.md> --route <route.yaml> --run-root <dir>
```

All four flags are required. Fail loud on any missing or unreadable path.

- `--phase` identifies one canonical plan phase, for example `phase-0` or `0`.
- `--plan` / `--route` are the run-tree frozen copies. During a run, the run-tree `route.yaml` is the only live route: a human profile addition edits that copy only, never the origin.
- `--run-root` is the run tree root `<run-root>/<date>/<slug>/` that `/herdr-run` created; this leader writes under `phases/<phase-id>/`.

## Pre-flight

1. Verify `HERDR_ENV=1`, else stop: `ERROR: /herdr-phase must run inside a Herdr-managed pane.`
2. Read the Recruiter pane id (`recruiter_pane`) from the UpAgent state file that `/herdr-run`'s `just upagent-up` persisted — `/tmp/.upagent/recruiter.json` by default, or the path in `UPAGENT_STATE`. This is the pane the leader signals with `herdr pane run <recruiter_pane> "recruit <order.json>"`. Confirm it is alive. Also determine this leader's OWN pane id — the `cockpit_pane` it stamps on every order — from its `$HERDR_PANE_ID` env var (or `herdr pane current`, which resolves the caller's own pane). Do **not** infer it from `herdr pane list` UI focus: the leader is not necessarily the focused pane, so focus-based discovery can name the wrong pane.
3. Validate this phase's route entry:
   - `lead.llm_profile` and `lead.agent` present;
   - `accuracy` is `medium` (default) or `high`; if `high`, `stage-0-alignment` is present, else it is absent;
   - all five base stage entries exist; each stage has `llm_profile` and `agent`;
   - `merge_back_at` is Stage 3, Stage 4, or Stage 5;
   - worktree branch template, green checks, and log checks configured;
   - each referenced profile exists and each named agent resolves in its harness/project context;
   - each profile's `model` is the harness-native id shape — claude: alias or full name (paired with `effort`); codex: bare id (paired with `effort`); pi: `provider/id[:thinking]` (the `:thinking` suffix is pi's effort). A `provider/…` model on a claude/codex profile, or a bare id on a pi profile, is a route error — fail loud, do not guess a translation;
   - Stage 2 (and, when high, stage-0's audit) is independent from Stage 1.
4. Determine the current **pass** number: count existing `pass-<p>/` dirs under `phases/<phase-id>/` and use the next one (start at `pass-1`). Read `phase-status.md` if it exists to see where a prior pass left off.
5. Run the pre-flight dependency/import safety check before any code stage (see the shared phase protocol). On a confirmed circular dependency, write a `blocked` `phase-result.json` and stop.
6. Create or select the temporary worktree branch from the route template and record its path, branch, and base commit. This path is the `cwd` on every stage order.

## Stage execution — one work order per stage

The leader runs the phase's stages in order — `stage-1` … `stage-5` for `accuracy: medium`, and `stage-0-alignment` first for `accuracy: high`. For each stage it places exactly one work order to the Recruiter and reads back the worker's `result.json`:

```text
leader:    write pass-<p>/stages/<stage-id>/try-<m>/order.json + instructions.md   (order.cockpit_pane = this leader's pane)
leader:    herdr pane run <recruiter_pane> "recruit <order.json path>"
leader:    herdr wait output <recruiter_pane> --match "ORDER <order_id> DONE" --timeout <ms>
           # ALWAYS bound this wait (>= the order's timeout_ms + margin). Herdr's `wait output`
           # blocks FOREVER when --timeout is omitted, so an un-emitted DONE (e.g. an
           # unrecoverable malformed order) would hang the leader. On timeout, the leader treats
           # the stage as blocked (it always knows order_id — it wrote the order).
leader:    read + validate pass-<p>/stages/<stage-id>/try-<m>/result.json
```

`order.json` carries the contract fields (`order_id`, `phase_id`, `stage_id`, `harness`, `model`, `agent`, `effort`, `cwd`, `instructions_path`, `result_path`, `cockpit_pane`, optional `env`), with `harness`/`model`/`agent`/`effort` resolved deterministically from the route stage entry (`effort` is the profile's `effort`, or `medium` when the profile omits it — never left empty, because a roster template may substitute it into a CLI flag) and `cockpit_pane` set to a live cockpit pane (this leader's own) for the Recruiter to split the worker from — `herdr pane split` takes a source pane, not a workspace. `instructions.md` is the stage brief: the phase goal, this stage's job, the worktree branch, the deterministic merge timing, the non-delegation rule, the repo's available specialists (from the Specialist Hub roster) with the mandatory-consult rule — any area a listed specialist owns is asked, never guessed — and pointers to the plan and prior handoffs. When the stage's profile routes to any gpt-5.6 model (terra especially), the brief MUST also carry the overproduction guardrail verbatim: implement only what this stage requires; no speculative abstractions or extra features; tests proportionate to the change — cover the contract, not every permutation; prefer extending existing files over creating new ones. The Recruiter splits one fresh worker pane from `cockpit_pane` into the cockpit (`herdr pane split <cockpit_pane> --direction right --no-focus --cwd <worktree> [--env ...]`), runs the harness launch template, waits for the worker to finish, validates its `result.json`, closes the worker pane, and emits `ORDER <order_id> DONE`. The worker writes its own transcript path into `result.json`'s `full_log` field — that pointer is the durable audit trail.

Every generated `instructions.md` includes this copy-pasteable result template, and says that the enum is deliberately strict:

```json
{
  "order_id": "<exact order_id>",
  "verdict": "passed",
  "full_log": "<worker transcript path or session id>"
}
```

Use only `passed`, `failed`, or `blocked` for `result.json.verdict`; a failed result also includes a non-empty `revisit` list of recognized stage ids. `VERIFICATION_PASSED` is the Stage-2 audit-outcome concept, not a result-file verdict.

A missing or malformed `result.json`, or a Recruiter error, is treated as a `blocked` stage — never silently retried as if passed.

Before ordering a stage that has a prior same-role handoff, the leader points the worker at the latest `phases/<phase-id>/handoffs/<role>-vN.md`. On a retry that re-investigates the same unresolved failure signature, the leader additionally requires a live Specialist Hub Librarian consult before the worker forms a new hypothesis: provide the failure signature and prior ruled-out work, and require the worker to record the consult id and answer/error path in `result.json`. Reading static specialist docs is not a substitute. Every worker writes its handoff, `compacted.md`, and `result.json` before its pane closes.

## Stage-0-alignment (accuracy: high only)

When `accuracy: high`, before Stage 1 the leader orchestrates a sequence of **three separate non-delegating workers** — it does not hand this to one delegating agent:

1. **mini-research** order — research this phase against the original `research.md`.
2. **mini-plan** order — draft a mini-plan for this phase against the original `plan.md`.
3. **independent audit** order — a worker independent from `stage-1-implementation` audits the mini-plan against the big plan.

Misaligned ⇒ loop stage-0 (redo the mini-plan) within the stage-try budget. Unreconcilable ⇒ `blocked` (escalates). Stage-0 outputs are versioned, never overwritten.

## Five-stage lifecycle and merge timing

The leader runs the shared five-stage worktree lifecycle, ordering one worker per stage:

1. **Stage 1 — implementation** on the temporary worktree branch (unit tests + code, TDD loop, no goal cheating).
2. **Stage 2 — adversarial audit** of Stage 1 on the same branch, including the hard gate for **unused intake / accepted-but-ignored inputs**. A verification-passed audit outcome advances; its worker `result.json` still uses the enum `verdict=passed`. Blocking findings return `verdict=failed`, `revisit=[stage-1-implementation]` with the raw findings; non-blocking notes are recorded. Stage 2 must be run by a worker independent from Stage 1.
3. **Stage 3 — integration/acceptance seams**; merge the worktree back to main here iff `merge_back_at` selects Stage 3.
4. **Stage 4 — upstream DAG verification**; merge here iff `merge_back_at` selects Stage 4 (or run from main if already merged at Stage 3).
5. **Stage 5 — finalization**: merge if not already merged, verify main, run green checks, inspect logs for hidden failures, destroy the temporary worktree/branch, and write final evidence. Stage 5 always runs.

The full stage rules, the Stage 2 multi-angle audit detail, and rollback safety live in the shared phase protocol; follow them exactly.

## Stage-level backtracking and escalation

Backtracking replays **forward, in order** — nothing is reverted.

```text
stage result.json.verdict=failed, revisit=[stage-ids] → record the reason in phase-status.md
leader: replay from the earliest revisit stage-id forward; increment try (try-<m+1>)
  stage_try_budget hit (default 3) →
    advisor_profile set?  place an advisor order via the Recruiter (context = phase-status.md);
                          advisor result.json: verdict=passed, decision = continue | loop | stop-ask-human
    no advisor_profile →  give up: write phase-result.json.verdict=failed, revisit=[phase-ids]
```

The advisor is hired like any worker. Its order **reuses the failing stage's `stage_id`** (an ordinary recognized id) so it passes the order contract — there is no `stage_id: "advisor"`, which the contract would reject; the leader knows it placed an advisor order and reads `result.json.decision`. The advisor worker writes a normal `result.json` with `verdict: passed` **plus** the optional `decision` field — one of the exact tokens `continue`, `loop`, or `stop-ask-human` (the contract's `ADVISOR_DECISIONS`). The leader reads `result.json.decision`, not a special verdict: `continue` = accept the stage and advance; `loop` = another try (reset/extend the try budget); `stop-ask-human` = give up the phase to the TUI (write `phase-result.json.verdict=failed`, `revisit=[phase-ids]`).

When the leader gives up on the phase, it writes `phase-result.json` with `verdict=failed` and `revisit:[phase-ids]` so the TUI can replay from the earliest pointed phase. The leader never reverts merged history; a true revert is escalated to the TUI, which asks the human.

## phase-status.md and phase-result.json

- `phase-status.md` — one line per stage per pass, appended as work happens: `pass<p> <stage-id> try<m> <verdict> — <reason>, revisit <ids>`. This rolling log is how a later pass or try knows where it failed.
- `phase-result.json` — the latest phase verdict the TUI reads. Write and validate it, then as the literal final action before going idle print to this leader pane: `PHASE_RESULT: <phase-id> verdict=<passed|failed|blocked> pass=<n>`. Map `partial` to `blocked` for this completion marker while retaining the detailed file verdict. Include: phase id; pass number; `verdict` (`passed`/`partial`/`blocked`/`failed`); `revisit:[phase-ids]` on any non-passing verdict; lead `llm_profile`/`agent`; `accuracy` and the stage-0 outcome when high; `merge_back_at` and actual merge stage; temporary worktree branch/path and cleanup result; each stage id with `llm_profile`/`agent`/`order_id`/tries/final verdict; advisor status; dependency graph source; commands/evidence/`full_log` pointers; Stage 3 seam decision; Stage 4 upstream result; Stage 5 green-check and log-review result; rollback/cleanup actions.

Only write `passed` when every required stage passed and Stage 5 cleanup, green checks, and log review passed.

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`.
2. A stage is a work order to the Recruiter — never a native subagent, team, pane, or nested harness session created by the leader, and no Claude team mode.
3. Workers are terminal and non-delegating; a worker that needs more help returns `blocked`.
4. The route is authoritative and deterministic per stage; the leader resolves `harness`/`model`/`agent` from it and never lets the Recruiter pick.
5. `result.json` / `phase-result.json` are the source of truth; pane scrollback is evidence only.
6. Forward-only: replay forward on `revisit`; never revert merged history from this phase — escalate a true revert to the TUI.
7. Do not close the worker pane until its evidence is persisted; do not reset shared/main branches without checking for unrelated changes and asking the human.
