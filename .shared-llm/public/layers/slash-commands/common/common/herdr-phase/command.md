# /herdr-phase

Run one phase as the Herdr-native **phase leader**. This command is sent to a cockpit pane by `/herdr-run`, once per phase. The leader runs the phase's stages by placing a work ORDER per stage with the UpAgent Recruiter — never by spawning a native or nested subagent — applies stage-level backtracking and the stage-try budget ladder, maintains `phase-status.md`, and writes `phase-result.json` (the source of truth the TUI reads).

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
2. **Inspect controller ownership without making monitoring a work gate.** When `$UPAGENT_PHASE_START_RECEIPT` names a readable `phase-start.json`, record its state (`ready`; the `watchdog` block reads `not-configured` by design — the TUI's blocking `upagent-phase-await` on that same receipt owns liveness, and this leader publishes to its journal via `upagent-phase-publish`). If the variable or receipt is missing or mismatched, append `phase-monitoring: degraded — <reason>` to `phase-status.md` and continue the phase. Do not create, adopt, or repair a watchdog yourself — none exists to repair. Monitoring failure must never become an infinite wait or prevent plan work.
3. Confirm `just upagent-up` has persisted a live Recruiter in `/tmp/.upagent/recruiter.json` (or `UPAGENT_STATE`). The pane is a visible status surface, not a command mailbox. Determine this leader's OWN pane id — the `cockpit_pane` stamped on every order — from `$HERDR_PANE_ID` (or `herdr pane current`). Do **not** infer it from UI focus.
4. Validate this phase's route entry:
   - `lead.llm_profile` and `lead.agent` present;
   - `accuracy` is `medium` (default), `high`, or `max`; if `high` or `max`, `stage-0-alignment` is present, else it is absent; if `max`, stage-2 also names `second_llm_profile` referencing a profile on a different harness or model than the primary auditor;
   - all five base stage entries exist; each stage has `llm_profile` and `agent`;
   - `merge_back_at` is Stage 3, Stage 4, or Stage 5;
   - worktree branch template, green checks, and log checks configured;
   - each referenced profile exists and each named agent resolves in its harness/project context;
   - each profile's `model` is the harness-native id shape — claude: alias or full name (paired with `effort`); codex: bare model id such as `gpt-5.6-sol` (paired with `effort`, passed as `model_reasoning_effort`); pi: `provider/id[:thinking]` (the `:thinking` suffix is pi's effort). A `provider/…` model on a claude/codex profile, or a bare id on a pi profile, is a route error — fail loud, do not guess a translation;
   - Stage 2 (and, when high or max, stage-0's audit) is independent from Stage 1.
5. Determine the current **pass** number: count existing `pass-<p>/` dirs under `phases/<phase-id>/` and use the next one (start at `pass-1`). Read `phase-status.md` if it exists to see where a prior pass left off.
6. Run the pre-flight dependency/import safety check before any code stage (see the shared phase protocol). On a confirmed circular dependency, write a `blocked` `phase-result.json` and stop.
7. Create or select the temporary worktree branch from the route template and record its path, branch, and base commit. This path is the `cwd` on every stage order.

## Stage execution — one work order per stage

The leader runs the phase's stages in order — `stage-1` … `stage-5` for `accuracy: medium`, with `stage-0-alignment` first for `accuracy: high` and `max` (and, under `max`, a doubled stage-2 audit — see the five-stage lifecycle below). For each stage it places exactly one work order to the Recruiter and reads back the worker's `result.json`:

```text
leader:    write pass-<p>/stages/<stage-id>/try-<m>/order.json + instructions.md   (order.cockpit_pane = this leader's pane)
leader:    just upagent-request <order.json path>       # returns only after verified startup
Recruiter: return REQUEST_ACCEPTED <json> with manager/worker addresses + control token
leader:    just upagent-await <order.json path>         # deterministic wait; no LLM polling
Recruiter: return ORDER_RECEIPT <json>, or REQUESTER_DECISION_REQUIRED at a work cap
leader:    read + validate pass-<p>/stages/<stage-id>/try-<m>/result.json
```

The public result file is the source of truth; the durable receipt is its wake-up record. `upagent-request` and `upagent-await` call the filesystem ledger directly, so shell keystrokes cannot interleave and no LLM file-polling watchdog is needed. Save the `control_token` from `REQUEST_ACCEPTED`. If `upagent-await` returns `REQUESTER_DECISION_REQUIRED`, inspect the evidence and use `just upagent-respond <order> <control-token> <nonce> extend <milliseconds>` or `... cancel 0`; then call `upagent-await` again after an extension. Only this recorded requester may make that decision. `ORDER ... DONE` pane text is display-only and MUST NOT drive a verdict.


With **more than one order in flight** (stage-0 sequences, an advisor beside a worker), do not stack single awaits — block once over the whole set:

```text
just upagent-await-any <timeout-ms> <cursor-json> <order.json> [<order.json> ...]
```

It returns one `AWAIT_EVENT <json>` tagged with the `request_id` that moved: a terminal receipt (`completed`/`failed`/`blocked`), a `decision-required` work cap, a `worker-warning`/`advisory` mailbox message, or `await-heartbeat` on quiet expiry. Echo the returned `cursor` back on the next call so handled messages never replay, and drop terminal orders from the set before re-awaiting. Requester notifications are durable mailbox events consumed by these awaits — nothing is ever pasted into this leader's pane.

`order.json` carries the contract fields (`order_id`, globally scoped `request_id`, `requester: {id, kind, address}`, `phase_id`, `stage_id`, `harness`, `model`, `agent`, `effort`, `cwd`, `instructions_path`, public `result_path`, `cockpit_pane`, optional `env`). Build `request_id` from the run slug, phase, pass, stage, and try; never use a phase-local name. Set the requester address to this leader's exact Herdr agent name or pane. The Recruiter creates a Dedicated Account Manager, validates its startup, creates a lease-private result path, atomically launches the worker, and proves its process/agent/cwd before reporting healthy. The worker writes only the private result. The Recruiter validates it, closes and verifies the owned panes, then publishes the public `result_path` and `receipt.json` atomically under the lease fence. Its lease records requester, manager, runner, worker, workspace, generation, control token, and expiry so the standing Python supervisor can reconcile an orphan without guessing ownership. The worker's `full_log` points to its harness transcript; it is not the result transport.

The Recruiter-generated final worker brief includes a copy-pasteable result template and its destination. The Recruiter MUST replace the two values below with the literal `order_id` and literal absolute `result_path` (the lease-private path); angle-bracket text is a protocol-writing marker, never text that may appear in a generated instruction. The worker MUST write exactly one result at that path and MUST copy the stated `order_id` exactly. It MUST NOT invent, generate, replace, or otherwise alter the order id.

```text
Write result.json exactly to: <literal result_path from this order.json>
```

```json
{
  "order_id": "<literal order_id from this order.json>",
  "verdict": "passed",
  "full_log": "<worker transcript path or session id>"
}
```

Use only `passed`, `failed`, or `blocked` for `result.json.verdict`; a failed result also includes a non-empty `revisit` list of recognized stage ids. `VERIFICATION_PASSED` is the Stage-2 audit-outcome concept, not a result-file verdict.

Every generated `instructions.md` also includes this terminal instruction: after `result.json`, `compacted.md`, and the handoff are durably written, exit the session. Do not stop at an idle prompt and wait for another instruction.

When a stage's route profile sets `scope_leash: true`, the leader copies the scope-discipline block from `.shared-llm/public/llm/pi/common/meta-plan/scope-leash.md` into that stage's `instructions.md` verbatim, before the result template. The flag on the profile decides; the leader never applies or skips the leash by model name.

A missing or malformed `result.json`, or a Recruiter error, is treated as a `blocked` stage — never silently retried as if passed.

Before ordering a stage that has a prior same-role handoff, the leader points the worker at the latest `phases/<phase-id>/handoffs/<role>-vN.md`. On a retry that re-investigates the same unresolved failure signature, the leader additionally requires a specialist consult before the worker forms a new hypothesis: provide the failure signature and prior ruled-out work, and require the worker to record the consult id, its UpAgent request id, and the answer/error path in `result.json`. Reading static specialist docs is not a substitute. Every worker writes its handoff, `compacted.md`, and `result.json` before its pane closes, then exits its session.

**Every stage brief carries the specialist phone book.** Once per phase (and again if the roster changes mid-phase), the leader runs `just upagent-specialists` and pastes its output VERBATIM into every stage's `instructions.md`. That block is the merged roster — the kit's base specialists plus this repo's own — with the mandatory-consult rule and the exact consult mechanics, so a worker never has to discover consulting on its own. Workers record every consult they make in `result.json` under `consults` (`{consult_id, specialist, request_id, answer_path}`; empty list when none applied), and set `requested_by` on each `consult.json` to their own `order_id` — that is what files the consult under the worker so the Recruiter can credit it back.

Consults travel through the UpAgent CLI, never through pane text. A consult is an ordinary UpAgent order: the same Recruiter, the same ledger, the same worker lifecycle as any stage worker. The pasted phone book already spells out the mechanics: write the question to a `consult.json` file, run `just upagent-consult <that file>`, and read the referenced `answer.json` when the command returns — it BLOCKS until the consult is terminal and prints one `CONSULT_RECEIPT` line, and an answer (real or failure) is always written. A brief MUST NOT tell a worker to deliver a consult with `herdr pane run`, `herdr pane send-text`, or any other pane injection: the Recruiter pane is a status surface holding a plain shell, so pasted prose lands in bash and is silently lost with no error back to the sender.

## Talking to the TUI — typed events, never pane text

The TUI waits inside `upagent-phase-await` on this phase's `phase-start.json` receipt. When this leader needs the owner before it can advance, it publishes one typed event into the phase journal instead of printing a plea into scrollback:

```text
just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT needs-input "<one-line question + evidence path>"
just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT blocked "<what cannot advance and why>"
```

The TUI's blocking await returns that event as its next turn — no idle-wait, no pane injection, no watchdog relay. Terminal outcomes still travel through `phase-result.json`; do not double-publish them. `blocked` is terminal: publishing it ends this attempt, so write `phase-result.json` with verdict `blocked` immediately after — the owner decides and replays as a fresh pass. There is no owner→leader answer channel yet (the command contract exists but nothing consumes it), so a question that must be answered before work can continue is a `blocked`, never a `needs-input`.

## Stage-0-alignment (accuracy: high or max)

When `accuracy` is `high` or `max`, before Stage 1 the leader orchestrates a sequence of **three separate non-delegating workers** — it does not hand this to one delegating agent:

1. **mini-research** order — research this phase against the original `research.md`.
2. **mini-plan** order — draft a mini-plan for this phase against the original `plan.md`.
3. **independent audit** order — a worker independent from `stage-1-implementation` audits the mini-plan against the big plan.

Misaligned ⇒ loop stage-0 (redo the mini-plan) within the stage-try budget. Unreconcilable ⇒ `blocked` (escalates). Stage-0 outputs are versioned, never overwritten.

## Five-stage lifecycle and merge timing

The leader runs the shared five-stage worktree lifecycle, ordering one worker per stage:

1. **Stage 1 — implementation** on the temporary worktree branch (unit tests + code, TDD loop, no goal cheating).
2. **Stage 2 — adversarial audit** of Stage 1 on the same branch, including the hard gate for **unused intake / accepted-but-ignored inputs** and the **consult-receipt check**: compare the brief's specialist phone book against the Stage 1 `result.json` `consults` list — deciding in an area a listed specialist owns with no matching consult receipt is a blocking finding. Read `consults_verified` on the phase receipt, not the worker's own list: the Recruiter resolves every claimed entry against its own record of what it brokered, so a verified entry is a Python-checked fact and anything in `consults_unverified` is a claim with nothing behind it. What the stamp cannot decide is whether a consult was OWED — that is this audit's judgement, and a verified consult proves a question was asked, not that it was the right one. A verification-passed audit outcome advances; its worker `result.json` still uses the enum `verdict=passed`. Blocking findings return `verdict=failed`, `revisit=[stage-1-implementation]` with the raw findings; non-blocking notes are recorded. Stage 2 must be run by a worker independent from Stage 1. Under `accuracy: max` the leader places TWO independent stage-2 orders — the primary `llm_profile` and the route's `second_llm_profile` (a different harness or model) — and blocks over both with `upagent-await-any`. Both must return `passed` to advance. On disagreement, consult `finalization_defaults.advisor_profile` as the judge when set (its `result.json.decision`: `continue` accepts the work, `loop` replays stage-1 with both sets of findings, `stop-ask-human` gives the phase up); with no advisor, write `phase-result.json` verdict `blocked` for the human. Each auditor's order reuses the stage-2 `stage_id` with its own try and order id, and both results are recorded in `phase-status.md`.
3. **Stage 3 — integration/acceptance seams**; merge the worktree back to main here iff `merge_back_at` selects Stage 3.
4. **Stage 4 — upstream DAG verification**; merge here iff `merge_back_at` selects Stage 4 (or run from main if already merged at Stage 3).
5. **Stage 5 — finalization**: merge if not already merged, verify main, run green checks, inspect logs for hidden failures, destroy the temporary worktree/branch, and write final evidence. Stage 5 always runs.

The full stage rules, the Stage 2 multi-angle audit detail, and rollback safety live in the shared phase protocol; follow them exactly.

## IaC phases (kind: iac)

When the route marks this phase `kind: iac`, the same ladder runs with terraform meanings:

- Stage-1 writes the terraform; stage-2 audits it adversarially (a different model family is recommended). Every IaC stage brief restricts the worker to `fmt`, `validate`, `init`, `plan`, and `show` — a stage worker never applies, and the terraform persona refuses apply without explicit approval evidence anyway.
- Stage-3 runs `init` and `plan -out <pass-dir>/iac/plan.bin`, saves `terraform show -json` output as `<pass-dir>/iac/plan.json`, builds the human table with `just iac-plan-table <pass-dir>/iac/plan.json > <pass-dir>/iac/plan-table.txt`, and records the artifact's SHA-256 in `phase-status.md`.
- The leader then asks the owner for the apply decision. The durable approval file is the owner→leader answer channel here, so this is a wait, not a `blocked`:

```text
just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT decision-required "IaC layer <phase-id>: apply approval required (destroy total <n>)" --requested-action iac-approval --evidence <pass-dir>/iac/plan-table.txt --evidence <pass-dir>/iac/plan.bin
```

  Then wait (bounded by the phase timeout) for `<pass-dir>/iac/approval.json` and, when it says approved, `<pass-dir>/iac/apply-receipt.json`. Validate both: `plan_sha256` in each must equal the recorded artifact digest, and the receipt's `exit_code` must be 0. `approved: false` ⇒ `phase-result.json` verdict `blocked` (the human declined). Timeout ⇒ `blocked` with the evidence paths. A sha mismatch means the plan went stale — re-run stage-3 as a new try and re-ask.
- Stage-4 is the TUI-performed apply, recorded from the receipt (runner `tui-apply`, receipt path as evidence) — the leader hires no stage-4 worker in an iac phase. The variant for very long applies is a worker order with `operation: apply`, `requires_apply: true`, and the `plan_artifact`/`approval` blocks (the same SHA-bound contract the direct controller enforces).
- Stage-5 finalizes as usual: verify outputs/state summary, record evidence, clean the worktree.

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
3. Workers are terminal and non-delegating; a worker that needs more help returns `blocked`, and a worker that has written its required files exits its session.
4. The route is authoritative and deterministic per stage; the leader resolves `harness`/`model`/`agent` from it and never lets the Recruiter pick.
5. `result.json` / `phase-result.json` are the source of truth; pane scrollback is evidence only.
6. Forward-only: replay forward on `revisit`; never revert merged history from this phase — escalate a true revert to the TUI.
7. Do not close the worker pane until its evidence is persisted; do not reset shared/main branches without checking for unrelated changes and asking the human.
8. Only the Hub executes lifecycle actions, and only this requester authorizes extension/cancellation before the hard-deadline grace expires. In the default direct lifecycle there is no Dedicated Account Manager; a roster may opt into `management.mode: dedicated`.
