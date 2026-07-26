# /upagent-pipeline

Run one issue-sized change end to end through a named pipeline. **This session is the durable TUI
agent for the run**: it places every worker order, keeps the run log, holds the human review gate,
and does the implementation itself.

Route by size. `/cc-plan` is the front door for a big plan that deserves research, design
resolution, and adversarial grilling. `/upagent-pipeline rpi` is the small loop for one issue —
research, plan, approve, implement, done.

## Invocation

```text
/upagent-pipeline <pipeline> [--skip-research] [--skip-plan] <issue-location>
```

- `<pipeline>` — a pipeline id from the registry, never invented here.
- `--skip-research` / `--skip-plan` — drop one optional stage.
- `<issue-location>` — where the issue text lives. Always a location, never the issue text itself.

## Pre-flight

1. Run `just upagent-list-pipelines --json`. If `<pipeline>` is not one of the listed ids, stop and
   print the valid ids. Never fall back to a default pipeline or to the closest-looking id. The
   registry is read from the canonical UpAgent source, so a `no checked-out main branch UpAgent
   source found` failure here means `UPAGENT_CANONICAL_REPO` must be set to an absolute checkout
   root — report that fix rather than the raw traceback.
2. Read the selected pipeline's `optional_stages` and `max_phases` from that same output. A
   `--skip-*` flag naming a stage this pipeline does not list as optional is a stop, not a warning.
3. Resolve `<issue-location>` to issue text before anything else:
   - An existing file path is used as-is; copy it verbatim into the run tree as `issue.md`.
   - A tracker reference or URL (GitHub, Forgejo, Linear) is fetched verbatim — `gh issue view`, the
     tracker's API — and written to `issue.md` unedited.
   - Anything else is a loud stop: a bare issue number with no tracker, an unreachable ref, a title,
     or a description typed into the chat. Report exactly what you were given and ask for a path or
     a resolvable ref. Never guess which issue a number means, and never write `issue.md` from the
     conversation.
4. Resolve the work-log directory by running
   `.shared-llm/public/llm/common/common/planish_resolve.py --topic <issue-slug>` and reading
   `plan_dir` and `host` from its JSON result. Do not reproduce its precedence in prose. Create the
   run tree under `plan_dir`:

   ```text
   <run-dir>/
     pipeline-log.md        append-only run log
     issue.md               the fetched issue, verbatim
     research/v<N>/research.md
     plan/v<N>/plan.md
   ```

5. `pipeline-log.md` is append-only and written at **every** stage transition — stage start and
   finish, each worker request id, each receipt path, each decision you make, each human approval,
   the worktree path, and the cleanup. Append before you act, not after. A stage that is not in the
   log did not happen.
6. Create a git worktree for this run and record its absolute path in `pipeline-log.md`. Every
   worker gets it as `--cwd` and you implement in it; never hand a worker the primary checkout.
   Removing it is an explicit final step (below), never an implicit side effect.

## Stage orders

Every stage worker is one bounded one-shot UpAgent order placed through the canonical `just upagent`
façade, following the same rules as `/upagent-run`: select an existing offering id and an effort its
`efforts` list permits from `just upagent lists --type offerings --json`, make that choice
task-based and state it, submit with `--cwd <worktree>` and the stage's persona, then block in
`just upagent await`. One order per stage. The worker is destroyed at its receipt — a follow-up
question is a new order, never a message into a live worker.

If the stage's output file is absent when the request terminalizes, the stage failed. Record the
failure and stop or re-hire; never write the missing artifact yourself.

## Pipeline: `rpi`

1. **Research** — skipped by `--skip-research`. Persona `researcher`. Brief: read
   `<run-dir>/issue.md`, investigate inside the worktree, write findings to exactly
   `<run-dir>/research/v<N>/research.md`.
2. **Plan** — skipped by `--skip-plan`. Persona `planner`. Brief: the absolute `issue.md` path and
   the absolute research path (say explicitly that research was skipped when it was), the phase
   budget from the registry's `max_phases`, and exactly one output path,
   `<run-dir>/plan/v<N>/plan.md`. Plans are small: one phase is the target, `max_phases` is the
   ceiling.
3. **Phase-count gate — mechanical, before anything else reads the plan.** Count the phases in
   `plan.md` yourself. Over `max_phases`, refuse to continue: append the count and the refusal to
   `pipeline-log.md` and tell the human the plan is too big for this pipeline, with the two ways
   forward — re-hire the planner on a narrower issue, or take the work to `/cc-plan`. Do not trim
   the plan to make it fit. A prose phase limit gets bypassed; this count is the enforcement.
4. **Human review gate.** Render `plan.md` as a work-log HTML page following the shared Planish HTML
   Grill Contract at `.shared-llm/public/llm/common/common/planish-html-grill-contract.md` —
   annotatable static page, sticky notes, Copy Feedback, no in-page submit. Build the URL with the
   resolver's `host` value, give it to the human, and end the turn. Apply pasted feedback either by
   editing `plan.md` yourself for small corrections or by placing a fresh `planner` order for a
   rewrite, freezing each revision as the next `plan/v<N>/`. Loop until the human approves. Record
   every round and the final approval in `pipeline-log.md`. No approval, no implementation.
5. **Implement.** Implement the approved phases **directly in the worktree**, the way you would any
   small change: read the code first, make the change, run the checks the plan names, preserve
   unrelated work. No phase leaders, no stage workers, no `route.yaml`, no `/tui-control`. If the
   plan turns out to be wrong, go back to the review gate with what you found; do not redesign
   mid-implementation.

## Pipeline: `no-mistakes`

Task-first: do the work, then drive the real gate. This pipeline has no research and no plan stage —
it implements and validates. A `--skip-research` or `--skip-plan` flag on it is a loud stop, not a
no-op.

1. Implement the issue in the worktree on a feature branch — never the repository's default branch.
   Inspect `git status` first, preserve unrelated uncommitted work, and commit only this task's
   changes. The gate validates committed history on a non-default branch.
2. Confirm `no-mistakes` is installed and the repository is initialized. If not, stop and report the
   exact fix command (`no-mistakes init`, or what `no-mistakes doctor` prints). Never simulate the
   gate by hand-running review, tests, or lint in its place.
3. Drive the real daemon: `no-mistakes axi run --intent "<intent>"`. Build the intent from
   `issue.md` plus the decisions you actually made — the goal in the issue's own terms, the
   tradeoffs, the constraints, and the approaches you ruled out. A thin one-line intent makes the
   review step flag deliberate choices as mistakes.
4. `axi run` and every `axi respond` block for minutes at a time. That is normal: do not cancel,
   re-issue, abort, or fix findings in the worktree yourself while a run is active. Read every
   return, respond at each `gate:`, and loop until an `outcome:`.
5. The gate's pipeline agent is configured per repository — `claude`, `codex`, or `pi` natively, or
   `cursor` over ACP with ordered fallback. Never assume which one it is, drive it directly, or act
   as its backend. You are the AXI driver.
6. Relay `ask-user` findings and parked gates to the human **verbatim** — id, severity, file, and
   description exactly as printed. Do not resolve them yourself and do not approve to keep the run
   moving.
7. Append the terminal outcome and the PR link to `pipeline-log.md`.

## Cleanup

Removing the worktree is an explicit final step, never automatic. Report the outcome, the run
directory, and the worktree path; remove the worktree only once the human confirms the work has
landed or asks you to, and append the removal to `pipeline-log.md`. Never remove it on the way out
of a failed stage — that is where the evidence is.

## Hard rules

1. Unknown pipeline id, a skip flag for a non-optional stage, an unresolvable issue location, and an
   over-budget plan are all loud stops. None of them has a silent fallback.
2. The issue is always a location. `issue.md` is fetched verbatim, never authored from the chat.
3. Every stage worker is a fresh one-shot `just upagent` order with an explicit offering, effort,
   persona, and `--cwd <worktree>`. No native subagents and no team mode for stage work.
4. Append to `pipeline-log.md` at every transition, before acting on it.
5. This command never converts a plan for Herdr, writes a `route.yaml`, or starts `/tui-control`.
   Work that needs those is too big for this pipeline — take it to `/cc-plan`.
