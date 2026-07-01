# cc-loop — In-Session Sequential Phase Loop

Run every available phase of a plan, end-to-end, inside one Claude Code session. Each phase gets fresh context (via subagent or per-phase TeamCreate-of-one), but the *orchestrator* — you, the main thread — persists across phases. That persistence is the core value: when a phase BLOCKS, you don't have to relaunch a separate session. You research, draft options, ask the user (interactive) or escalate via code review (afk), apply the fix, and resume — all in one conversation.

Filesystem protocol is identical to `/rphase-run` and the bash `tools/ralph-phase-loop.sh`, so phase trees produced by `/rphase-create` work with all three. You can mix and match drivers between phases of the same plan without coordination.

## Cardinal rule: delegate, don't do

Your job as the orchestrator is to **dispatch and bookkeep**. You move phase files, write iteration records, parse structured returns, and mediate blocks. You do not read source files for a phase, run verification commands, push to your Git remote, edit code, or `git commit` yourself.

**There are no exceptions and no "ask for approval" escape hatches.** If you are about to do work directly, stop and dispatch a fresh subagent for the work instead. Your tools are: read the phase JSON, dispatch a worker, dispatch a deployer (when needed), dispatch a blocker-researcher (when needed), write the iteration directory, `mv` the phase file, `AskUserQuestion`. That's the complete list.

Why: your context window fills as you work. Subagents have fresh context windows; using them keeps the leader sharp and the work clean. Delegate unconditionally.

The single exception is *blocker research* (Step 7): when you need to prepare fix options for a BLOCKED phase and the worker supplied none, dispatch the Opus `blocker-researcher` subagent. Even there, the *researcher* does the reading and reasoning — you just orchestrate.

## Invocation

```
/cc-loop [--interactive|--afk] <phases-path-or-work-log-name> [-i N] [-t SECS] [--pulse] [--subagent]
```

- **`--interactive`** — Default mode. Per-phase Sonnet worker dispatched via TeamCreate-of-one (or `--subagent` for headless). User mediates all BLOCKED phases in chat.
- **`--afk`** — AFK mode. Per-phase routing by `size` tag. Mandatory code review after every PASSED worker. Single auto-fix pass on NEEDS_CHANGES before blocking. Requires a plan produced by `/cc-plan-and-grill --route-phases`. **Refuses to run if any phase is missing its `size` field.**
- **`<phases-path-or-work-log-name>`** — Required. Either:
  - A direct path to a phases-tree root (the directory containing `current/available/`, `current/done/`, `current/blocked/`, `iterations/`, `unblocked/`). Example: `mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/plan/v1/`.
    <!-- <YYYY-MM-DD> is the ISO date the work was started (NOT modified). Use the date at the time of the first invocation of the planning skill in this work-log. -->
  - A bare work-log name. The skill auto-prepends `mkdocs/docs/work-log/` and resolves the latest `<YYYY-MM-DD>/<slug>/plan/v<N>/` underneath.
- **`-i N`** — Maximum loop iterations. Default 20. One iteration = one worker dispatch (PASSED, FAILED, or BLOCKED). The same phase failing twice consumes two iterations.
- **`-t SECS`** — Per-subagent-dispatch wall-clock timeout. Default 3600 (1 hour). Hard-enforced via background dispatch + TaskStop.
- **`--pulse`** — Add `team-pulse` to the team-of-one. Only meaningful in default (TeamCreate-of-one) transport; ignored when `--subagent` is set. Off by default.
- **`--subagent`** — Interactive mode only. Switch transport to plain `Agent()` (no TeamCreate, no TMUX window). Use for headless/CI/long-plan runs.

## Mode selection

### --interactive (default)

Use for plans you want to drive yourself. You see every verdict in chat, and BLOCKED phases pause the loop immediately for your input. You don't need size tags on phases.

Transport per phase: TeamCreate-of-one (default) or plain Agent (`--subagent`). Worker is always Sonnet. No code review pass; you are the review. Livelock auto-escalates to BLOCKED after 2 identical consecutive FAILED attempts.

When a phase BLOCKS, the orchestrator pre-researches fix options (Step 7 below) before presenting them to you via `AskUserQuestion`. The loop resumes only after you approve a fix.

### --afk

Use for unattended runs after `/cc-plan-and-grill --route-phases` has tagged every phase with `small` or `big`.

**Content-contract refusal:**

BEFORE running `--afk`, the orchestrator MUST scan every phase JSON in `current/available/` for a `"size"` field. If ANY phase is missing it, REFUSE to run with this message:

```
Cannot run --afk: phase <id> has no Size tag. Run /cc-plan-and-grill
--route-phases to produce a routable plan, or invoke /cc-loop
--interactive to run this plan in-session.
```

Transport per phase: `small` → plain `Agent()` subagent; `big` → TeamCreate-of-one (+ `team-pulse` if `--pulse`). After every PASSED worker return, a fresh `code-review` subagent reviews the diff and evidence. NEEDS_CHANGES triggers one auto-fix worker pass; if the second review still fails, the phase blocks. Every done phase gets committed exactly once. Watchdog is mandatory.

There is **no `--subagent` flag** in `--afk` mode — transport is decided per phase from the JSON `size` field.

## Pre-flight

Before the first iteration:

1. **Resolve `<phases_root>`** from the argument. Verify these directories exist (create empty ones if missing):
   - `<phases_root>/current/available/`
   - `<phases_root>/current/done/`
   - `<phases_root>/current/blocked/`
   - `<phases_root>/iterations/`
   - `<phases_root>/unblocked/`
   - `<phases_root>/done/` (--afk only: canonical per-phase results.md home)

2. **Content-contract check (--afk only).** Iterate `current/available/*.json`. For each, parse JSON and read `.size` AND `.verification` AND `.execution_mode`. If any phase is missing the size tag, missing the verification array, or has malformed verification gates: refuse (see message above) and exit.

3. **Determine plan-level execution mode (--afk only).** Read `.execution_mode` from any phase JSON (they're all identical). Default `sequential` if absent. Drives handoff injection in the per-iteration loop.

4. **Drain blocked.** If `current/blocked/` is non-empty, drop into Step 7 immediately — present each blocked phase's options before any new phase work begins. Resume only when `current/blocked/` is empty.

5. **Recommend Sonnet.** If the session is running on Opus, print a one-line nudge: `note: orchestrator is on Opus; consider /model sonnet to keep this session cheap (workers are Sonnet anyway).` Do NOT switch the model — that's the user's choice. Fires once per invocation.

6. **Dispatch plan-watchdog (--afk mandatory, --interactive optional).** In `--afk` mode watchdog is always on. Spawn before the first iteration:
   ```
   Agent(
     subagent_type="plan-watchdog",
     name="watchdog",
     model="sonnet",
     run_in_background=true,
     prompt="<watchdog prompt — see below>"
   )
   ```
   Watchdog prompt:
   ```
   You are plan-watchdog for a /cc-loop run.
   phases_root: <abs path>
   Watch every iteration. Poll iterations/ and current/ every 60–90s.
   Flag scope drift (worker touching files outside files_touched), repeated
   FAILED on the same phase (>2 consecutive), missing decisions.md entries
   on PASSED phases, evidence files that look empty/synthesized.
   Escalation ladder: FLAG (post a note to the orchestrator) → BLOCK (next
   phase pickup waits for orchestrator review) → HALT (whole loop pauses
   for user). Do NOT take corrective action yourself — your role is to
   surface problems, not fix them.
   ```
   Save the watchdog's agent ID. Send it `STOP_WATCHDOG` after the loop's stop conditions fire.

7. **Print run header:**
   ```
   /cc-loop starting
     mode:          interactive  (or: afk)
     phases_root:   <abs path>
     transport:     teamcreate-of-one  (or "subagent" / "per-size")
     pulse:         off               (or "on")
     watchdog:      on                (or "off")
     timeout:       3600s
     max iters:     20
     available:     N
     blocked:       0
   ```

8. Initialize `iter_count = 0` and `last_failed = {phase_id: None, failed_checks_set: None}`.

## Per-iteration loop

Repeat until a stop condition fires.

### Step 1 — Pre-iteration check

- If `current/blocked/` is non-empty → drop into Step 7.
- If `current/available/` empty AND `current/blocked/` empty → loop COMPLETE. Print summary, exit.
- If `iter_count >= -i N` → loop STOP. Print "iteration cap reached, M phases remain available, K blocked." Exit.

### Step 2 — Pick the next phase

First `.json` file in `current/available/` by alphabetical sort. Read it into memory. Note: `id`, `title`, `goal`, `tasks`, `files_touched`, `verification`, `raw_markdown`, and (in `--afk`) `size`.

### Step 3 — Compute iteration directory

Walk `<phases_root>/iterations/<phase_id>/`:

1. **No directory yet** → use `v1/attempt-1/`.
2. **Directory exists** → find highest `v<N>/`, then most recent `attempt-<M>/results.md`. Read VERDICT line:
   - **BLOCKED** → user must have just unblocked → `v<N+1>/attempt-1/`.
   - **FAILED** → retry within same version → `v<N>/attempt-<M+1>/`.
   - **PASSED** → corrupt state. Treat phase as immediately BLOCKED with reason `State inconsistency: highest iteration entry was PASSED but phase is still in available/`. Skip Steps 4–5, fall through to Step 6 with VERDICT=BLOCKED.

`mkdir -p <iteration_dir>`. Hold its absolute path in `iteration_dir`.

### Step 4 — Pre-flag live-deploy + collect prior handoffs (--afk)

**Live-deploy:** Inspect the phase's verification array. Set `needs_deploy = verification[deploy].required` and `needs_live = verification[live].required`. Don't pre-spawn deployer; dispatch at moment of need in Step 5.

**Handoff collection (--afk, sequential mode):** If `execution_mode` is `sequential` (or absent):
- Glob `<phases_root>/done/phase-*/results.md`.
- For each, extract its `## Handoff` section content.
- Concatenate into a `prior_handoffs` string, ordered by phase ID.

**Handoff collection (--afk, parallel mode):** If `execution_mode` is `parallel`:
- If the phase's `depends_on` is empty, set `prior_handoffs = ""` (skip injection).
- Otherwise, for each phase ID in `depends_on`, read its `<phases_root>/done/<phase-id>/results.md` and extract the handoff. If a depended-on phase isn't in `done/` yet, BLOCK the phase with `blocker_reason = "depends_on phase <id> not yet done"`.

### Step 5 — Dispatch the worker

Pick `subagent_type` from `files_touched` extensions:

- `.py` → `backend`
- `.tf`, `.hcl`, `Dockerfile`, `terraform/` paths → `devops`
- `.tsx`, `.ts`, `.jsx`, `app/` paths → `frontend`
- schema SQL, migrations → `database`

Build the worker prompt (see "Worker contract" below), substituting the phase JSON path, `iteration_dir`, per-dispatch timeout, and (in `--afk`) the `prior_handoffs` block.

#### --interactive: TeamCreate-of-one (default)

```
TeamCreate(
  team_name="<phase_id>-loop-v<N>-attempt-<M>",
  description="cc-loop --interactive attempt <M> of v<N> for <phase_id>: <title>"
)
```

Spawn the worker:

```
Agent(
  team_name="<phase_id>-loop-v<N>-attempt-<M>",
  name="worker",
  subagent_type="<chosen>",
  model="sonnet",
  prompt="<worker prompt>",
  run_in_background=true
)
```

If `--pulse` was passed, also spawn `team-pulse` (Haiku). Otherwise skip.

#### --interactive: `--subagent` mode

```
Agent(
  subagent_type="<chosen>",
  model="sonnet",
  prompt="<worker prompt>",
  run_in_background=true
)
```

No TeamCreate. No pulse.

#### --afk: small phase

```
Agent(
  subagent_type="<chosen>",
  model="sonnet",
  prompt="<worker prompt>",
  run_in_background=true
)
```

No TeamCreate. No TMUX window.

#### --afk: big phase

```
TeamCreate(
  team_name="<phase_id>-loop-afk-v<N>-attempt-<M>",
  description="cc-loop --afk attempt <M> of v<N> for <phase_id>: <title>"
)
```

Spawn worker and optionally `team-pulse` (if `--pulse`):

```
Agent(
  team_name="<phase_id>-loop-afk-v<N>-attempt-<M>",
  name="worker",
  subagent_type="<chosen>",
  model="sonnet",
  prompt="<worker prompt>",
  run_in_background=true
)
```

#### Timeout monitor (all modes)

The dispatch returns a task identifier. Loop:

1. Wait 30s.
2. `TaskGet` the worker's task.
3. If status `completed` → break, read its return.
4. If wall-clock since dispatch ≥ `-t SECS` (default 3600) → call `TaskStop`, synthesize a FAILED record (TIMEOUT_HARD_KILL branch). Skip rest of Step 5.

When the worker completes, parse its return. Required block:

```
VERDICT: <PASSED|FAILED|BLOCKED>
SUMMARY: <one paragraph, ≤120 words>
FAILED_CHECKS: <comma list, or NONE>
BLOCKER_REASON: <one paragraph, only if BLOCKED, else NONE>
UNBLOCK_OPTIONS: <only if BLOCKED. NONE, or 1–3 options separated by " | ". Each option is "<short label>: <specific change>">
EVIDENCE_PATH: <iteration_dir>/worker-evidence.md
GIT_DIFF_SUMMARY: <≤8 lines>
```

In `--afk` mode, the Output Schema block (status / evidence / decisions_made / scope_drift / handoff_notes) is also required. The orchestrator parses the Output Schema first, falling back to the legacy VERDICT block only if the schema is missing.

Parse failure → treat as VERDICT=FAILED with `FAILED_CHECKS: WORKER_RETURN_MALFORMED` and proceed to Step 6.

#### Conditional second dispatches (--interactive, capped at 1 extra)

Only one of these may fire per iteration. Skip if you hit the timeout branch.

- **Live deploy still needed?** If `needs_live` is true and the worker didn't perform the live commands itself, dispatch the `deployer` subagent (Sonnet). Same timeout via background + TaskStop. Append findings to `<iteration_dir>/worker-evidence.md` under a "Deployer evidence" section.
- **Borderline verdict?** If VERDICT=PASSED with stale/ambiguous evidence, OR VERDICT=FAILED with output that doesn't clearly tie to a verification command, dispatch the `phase-evaluator` subagent for a second opinion. Replace the worker's verdict with the evaluator's only if the evaluator returns FAILED or BLOCKED — never silently upgrade FAILED→PASSED.
- **BLOCKED with no options?** Defer the researcher to Step 7 so the unblock conversation is contiguous.

#### Branch on worker verdict (--afk)

- **Worker BLOCKED** → write iteration record, move phase to `current/blocked/`, drop into unblock conversation (Step 7). Skip Stages C/D for this iteration. `iter_count += 1`.
- **Worker FAILED** → write iteration record, livelock check; if livelock, escalate to BLOCKED. Otherwise leave phase in `current/available/` for retry. Skip Stages C/D. `iter_count += 1`.
- **Worker PASSED** → continue to Stage C (code review).

### Step 6 — Write iteration record and move phase file

Always write `<iteration_dir>/phase.json` (snapshot of the phase JSON as it was when run) and `<iteration_dir>/results.md`.

#### PASSED (--interactive)

```markdown
# results.md for <phase-id> v<N> attempt-<M>

**VERDICT**: PASSED
**Team**: <team_name or "subagent">
**Duration**: <elapsed>

## Evidence

<merge worker's EVIDENCE_PATH content + key excerpts>

## Deployer evidence (if applicable)

<deployer findings, URLs, status codes, log excerpts>

## Follow-up
None needed
```

The results.md MUST end with a `## Follow-up` section containing EXACTLY one of:
- `None needed` (single line, no other text)
- One or more `[label](../../../followup/<YYYY-MM-DD>/<slug>/<name>.md)` links

Use "None needed" when the work is complete. Use a link list when follow-up items exist — each link points to a file at `~/project/repos/your-repo-ops/mkdocs/docs/followup/<YYYY-MM-DD>/<slug>/<name>.md` that the writer ALSO creates.

When creating a followup file, use frontmatter:
```yaml
---
status: Open
parent: <link back to results.md>
tldr: <one-sentence summary>
---
```

Then:

1. If TeamCreate mode: `TeamDelete <team_name>`.
2. `mv <phases_root>/current/available/<phase_id>.json <phases_root>/current/done/<phase_id>.json`
3. Print: `phase-<phase_id> PASSED after attempt <M> of version <N>`.
4. `iter_count += 1`. Continue loop.

#### FAILED (both modes)

```markdown
# results.md for <phase-id> v<N> attempt-<M>

**VERDICT**: FAILED
**Team**: <team_name or "subagent">
**Duration**: <elapsed>

## Failed checks

<list of which verification commands failed, with FAILED_CHECKS verbatim>

## Evidence

<full captured output, log excerpts>
```

Then:

1. If TeamCreate mode: `TeamDelete <team_name>`.
2. **Do not move the phase file.** It stays in `current/available/`.
3. **Livelock check.** If `phase_id == last_failed.phase_id` AND `failed_checks_set == last_failed.failed_checks_set` (identical FAILED_CHECKS in two consecutive attempts) → escalate to BLOCKED with `blocker_reason="Auto-escalated after 2 identical FAILED attempts"`. Re-enter Step 6 BLOCKED branch. Do NOT bump `iter_count` for the escalation itself.
4. Otherwise: update `last_failed`. Print: `phase-<phase_id> FAILED on attempt <M> of version <N>, will retry`.
5. `iter_count += 1`. Continue loop.

**TIMEOUT_HARD_KILL sub-branch:** Same as FAILED, but `FAILED_CHECKS: TIMEOUT_HARD_KILL` and the Evidence section notes "worker hard-killed at <SECS>s via TaskStop". Skip the livelock check for this attempt.

#### BLOCKED (both modes)

```markdown
# results.md for <phase-id> v<N> attempt-<M>

**VERDICT**: BLOCKED
**Team**: <team_name or "subagent">
**Duration**: <elapsed>

## Blocker reason

<BLOCKER_REASON verbatim>

## Worker-supplied unblock options

<UNBLOCK_OPTIONS verbatim, or "NONE — researcher dispatched in Step 7">

## Evidence

<what pointed at a blocker rather than a failure>
```

Then:

1. If TeamCreate mode: `TeamDelete <team_name>`.
2. Read the phase JSON, add a top-level field `"blocker_reason": "<reason>"`, write to `<phases_root>/current/blocked/<phase_id>.json`.
3. Remove `<phases_root>/current/available/<phase_id>.json`.
4. Ensure `<phases_root>/unblocked/<phase_id>/` exists; `cp current/blocked/<phase_id>.json unblocked/<phase_id>/v<N>.json`.
5. Print: `phase-<phase_id> BLOCKED on version <N>, entering unblock conversation`.
6. `iter_count += 1`.
7. **Drop into Step 7 immediately.**

#### PASSED (--afk) — code review + finalization

After the worker reports PASSED in `--afk` mode, continue to Stage C (code review).

**Stage C — Dispatch reviewer**

The reviewer type depends on the phase JSON's `verification[review].reviewer` field:

- `auto` (default): dispatch one `code-review` subagent (Sonnet).
- `adversarial`: dispatch one adversarial reviewer (Sonnet, adversarial prompt below), OR `codex:rescue` if available.
- `both`: dispatch BOTH in parallel. Final verdict is the WORSE of the two (REJECT > NEEDS_CHANGES > APPROVED).

Adversarial reviewer prompt:
```
You are an adversarial code reviewer. Hunt for: silent failures, broad
try/except blocks that swallow exceptions, scope creep beyond
<files_touched>, half-finished implementations, security holes,
off-by-one bugs, hardcoded secrets, missing input validation, dishonest
test patterns (mocks pretending), test output that exit-0s but missed
the failure path.

Phase JSON: <ABS_PATH_TO_PHASE_JSON>
Worker evidence: <ABS_PATH_TO_EVIDENCE>
Read the git diff via `git diff` against the branch tip.

Return EXACTLY the same structured block (REVIEW_VERDICT / REVIEW_NOTES
/ COMMIT_SHA / VERIFICATION_RESULTS / SCOPE). NEEDS_CHANGES if any
concrete fix could close a gap. REJECT only if structurally wrong.
```

Reviewer dispatch:
```
Agent(
  subagent_type="code-review",
  model="sonnet",
  prompt="<reviewer prompt>",
  run_in_background=true
)
```

Reviewer prompt preamble (both small and big):
```
You are reviewing one phase of an AFK cc-loop run. The worker has just
reported PASSED. Your job is to:

  1. Read the phase JSON at <ABS_PATH_TO_PHASE_JSON>.
  2. Read the worker's evidence at <ABS_PATH_TO_EVIDENCE>.
  3. Read the git diff via `git diff --stat` and `git diff` against the
     worktree's branch tip.
  4. Apply the Code Review Agent checklist (your normal checklist).
  5. Cross-check the worker's claims against the diff and evidence — did
     they touch only files in `files_touched`? Did each `verification`
     command actually run, with exit 0 and clean output?
```

For **small phases**, append:
```
Because this phase is `size: small`, you are responsible for the verify +
commit step in addition to review. After review:

  - Re-run every command in `verification`. Capture exit codes and output.
  - If review is APPROVED AND every command exits 0: stage all changes in
    `files_touched`, commit with message "phase-<id>: <title>" (one commit,
    no body), capture the resulting commit SHA.

Return EXACTLY this block (no prose before or after):

REVIEW_VERDICT: <APPROVED|NEEDS_CHANGES|REJECT>
REVIEW_NOTES: <≤200 words>
COMMIT_SHA: <hash if APPROVED, else NONE>
VERIFICATION_RESULTS: <one line per verification command:
"<command> -> exit <code> [pass|fail]">
SCOPE: small

Cardinal rule: NEEDS_CHANGES if any concrete fix could close the gap.
REJECT only if the worker's approach is structurally wrong.
```

For **big phases**, append:
```
Because this phase is `size: big`, you only review. Do NOT commit. A
separate verify+commit subagent handles that step after you approve.

Return EXACTLY this block (no prose before or after):

REVIEW_VERDICT: <APPROVED|NEEDS_CHANGES|REJECT>
REVIEW_NOTES: <≤200 words>
COMMIT_SHA: NONE
VERIFICATION_RESULTS: NONE
SCOPE: big
```

Always write `<iteration_dir>/review.md` with the verbatim reviewer return + header `# Review for phase-<id> v<N> attempt-<M>`.

Parse failure → treat as `REVIEW_VERDICT: NEEDS_CHANGES` with `REVIEW_NOTES: "REVIEWER_RETURN_MALFORMED"`.

**Branch on reviewer verdict:**

- **APPROVED** → continue to Stage D (big) or finalize done (small).
- **NEEDS_CHANGES** AND first review → fire Stage B-2 (auto-fix), then re-enter Stage C once.
- **NEEDS_CHANGES** AND already came from Stage B-2 → BLOCKED. `blocker_reason = "Code review NEEDS_CHANGES after auto-fix attempt: <truncated REVIEW_NOTES>"`.
- **REJECT** (any time) → BLOCKED. `blocker_reason = "Code review REJECT: <truncated REVIEW_NOTES>"`. No Stage B-2.

**Stage B-2 — auto-fix (first NEEDS_CHANGES only)**

Re-dispatch the worker with the same `subagent_type` and same transport as Stage A (small → subagent, big → re-spawn into the existing team — keep team alive between reviews; only `TeamDelete` after the iteration is fully resolved).

Fix worker prompt:
```
You are fixing review feedback on a phase you (or a peer) just implemented.

Phase JSON: <ABS_PATH_TO_PHASE_JSON>
Original worker evidence: <ABS_PATH_TO_EVIDENCE>
Reviewer feedback (REVIEW_NOTES verbatim):

<REVIEWER_NOTES_BLOCK>

Apply the reviewer's fixes. Stay strictly within `files_touched`. After
fixing, re-run every command in `verification` and append the new captures
to <ABS_PATH_TO_EVIDENCE> under a "## Fix attempt - $(date)" header.

Return the same VERDICT block as before. Cardinal rule: if you cannot fix
the reviewer's feedback, return VERDICT: BLOCKED with a clear BLOCKER_REASON.
Do NOT silently skip a fix.
```

Write `<iteration_dir>/fix-attempt.md`:
```markdown
# Fix attempt for phase-<id> v<N> attempt-<M>

## Reviewer feedback that triggered the fix

<REVIEW_NOTES verbatim>

## Fix worker return

<full worker return block>
```

After the fix worker completes:
- **Fix worker BLOCKED** → write iteration record, move phase to blocked.
- **Fix worker FAILED** → write iteration record. Phase stays in available. Skip Stage C re-run. `iter_count += 1`.
- **Fix worker PASSED** → re-enter Stage C **once**. Second NEEDS_CHANGES → BLOCKED.

**Stage D — verify+commit (big phases only, after APPROVED review)**

Dispatch a fresh `code-review` subagent (plain `Agent()`, not in the team):

```
Agent(
  subagent_type="code-review",
  model="sonnet",
  prompt="<verify-commit prompt>",
  run_in_background=true
)
```

Verify-commit prompt:
```
You are the verify+commit step for a big-size phase that just passed code
review. Your job is mechanical — re-run verification, commit on all-green,
report a structured failure on any gap.

Phase JSON: <ABS_PATH_TO_PHASE_JSON>
Reviewer notes (already APPROVED): <ABS_PATH_TO_REVIEW>
Worker evidence: <ABS_PATH_TO_EVIDENCE>
Iteration directory: <ABS_PATH_TO_ITERATION_DIR>

Steps:
  1. Re-run every command in `verification`. Capture exit code, stdout,
     stderr. Append a "## Verify+commit re-run" section to the evidence file.
  2. If every command exits 0: stage all changes in `files_touched`, commit
     with message "phase-<id>: <title>" (one commit, no body), capture SHA.
  3. If ANY command exits non-zero, do NOT commit. Capture which failed.

Return EXACTLY this block (no prose before or after):

VC_VERDICT: <COMMITTED|VERIFICATION_GAP>
COMMIT_SHA: <hash if COMMITTED, else NONE>
VERIFICATION_RESULTS: <one line per verification command: "<command> -> exit <code> [pass|fail]">
GAP_REASON: <if VERIFICATION_GAP, name which checks failed and their output. If COMMITTED, NONE.>

Cardinal rule: do not auto-fix. VERIFICATION_GAP → report it, let the
orchestrator decide.
```

- **VC_VERDICT = COMMITTED** → continue to finalize done.
- **VC_VERDICT = VERIFICATION_GAP** → BLOCKED. `blocker_reason = "Verify+commit verification gap: <GAP_REASON>"`. No Stage B-2.
- Parse failure → treat as `VC_VERDICT = VERIFICATION_GAP` with `GAP_REASON = "VC_RETURN_MALFORMED"`.

**Finalize done (--afk)**

Runs when: small phase + APPROVED + COMMIT_SHA non-NONE, OR big phase + VC COMMITTED.

1. Update phase JSON: fill in `verification[].evidence` for each gate. Write updated JSON to `<iteration_dir>/phase.json`.

2. Write `<iteration_dir>/results.md` (verbose iteration audit):
   ```markdown
   # results.md for <phase-id> v<N> attempt-<M> (iteration audit)

   **VERDICT**: PASSED
   **Size**: <small|big>
   **Team**: <team_name or "subagent">
   **Duration**: <elapsed>

   ## Evidence by gate
   - review: <path or "skipped: ..."> (reviewer: <auto|adversarial|both>)
   - deploy: <path or "skipped: ...">
   - live:   <path or "skipped: ...">

   ## Worker Output Schema return

   <verbatim copy of worker's Output Schema block>

   ## Reviewer findings

   <auto reviewer return, and if "both", adversarial reviewer return below>

   ## Commit

   <commit_sha>
   ```

3. Write the **canonical per-phase `results.md`** at `<phases_root>/done/<phase-id>/results.md` (≤300 words, with frontmatter). This is what downstream phases read for handoff injection:
   ```markdown
   ---
   status: Done
   tldr: One sentence. What this phase accomplished.
   decision: One sentence if the phase made a decision; omit otherwise.
   links:
     - phase_json: <relative path to phase JSON in current/done/>
     - iteration_audit: <relative path to iterations/.../results.md>
     - decisions: ../../../decisions.md
   ---

   ## What changed
   - <≤5 bullets, file paths>

   ## Verification evidence
   - review: <evidence path or skipped:reason>  (reviewer: <auto|adversarial|both>)
   - deploy: <evidence path or skipped:reason>
   - live:   <evidence path or skipped:reason>

   ## Scope drift
   - <copied from worker's scope_drift list>

   ## Handoff
   <copied verbatim from worker's handoff_notes (≤200 words)>

   ## Follow-up
   None needed
   ```

4. **Append decisions to `<work-log-root>/decisions.md`** for each entry in the worker's `decisions_made`. Compute the work-log root from `<phases_root>` (typically 3 directories up). Format:
   ```markdown
   ## <ISO-8601 timestamp> [SCOPE|ARCH|CODE|...] <decision title>
   - <body — what / why / how it changes the plan>

   Refs: phases/done/<phase-id>/results.md
   ```

5. Write `<iteration_dir>/commit.md`:
   ```json
   {
     "phase_id": "<id>",
     "size": "<small|big>",
     "branch": "<git rev-parse --abbrev-ref HEAD>",
     "commit_sha": "<sha>"
   }
   ```

6. If big phase: `TeamDelete <team_name>`.
7. `mv <phases_root>/current/available/<phase_id>.json <phases_root>/current/done/<phase_id>.json`. Ensure `<phases_root>/done/<phase-id>/` exists.
8. Print: `phase-<phase_id> PASSED + COMMITTED <commit_sha> after attempt <M> of version <N>. Canonical results at <phases_root>/done/<phase-id>/results.md.`
9. `iter_count += 1`. Continue loop.

## Blocked-phase intervention

For each phase currently in `current/blocked/`:

1. **Source the options.**
   - **Path A — worker supplied them.** If the most recent `iterations/<phase_id>/v<N>/attempt-<M>/results.md` shows non-NONE `UNBLOCK_OPTIONS`, parse and use those. No researcher dispatch.
   - **Path B — worker said NONE, or escalated livelock.** Dispatch a fresh researcher:
     ```
     Agent(
       subagent_type="general-purpose",
       name="blocker-researcher",
       model="opus",
       prompt="<researcher prompt>",
       run_in_background=true
     )
     ```
     Same `-t SECS` timeout. Researcher prompt:
     > Phase `<phase_id>` BLOCKED. Phase JSON: `<abs path>`. Latest results: `<abs path>`. Worker's BLOCKER_REASON: `<text>`. Read the relevant files (start with `files_touched` and any paths named in BLOCKER_REASON; consult `.original/` only if the issue is architectural). Return 1–3 concrete fix options, each as: `<short label>: <specific change to the phase JSON>` with a one-line rationale. Each option must specify which fields (tasks, verification, files_touched, raw_markdown) change and exactly how. If you find no fix possible without external input, return that as the only option labelled `external-only: <what the human must do outside the loop>`.

2. **Present via AskUserQuestion.**
   - Question: `Phase <phase_id> blocked: <one-line blocker_reason>. How should we unblock?`
   - One option per fix candidate (label them clearly). Always include "None of these — I'll explain" as an option.
   - Cap at 4 options total. If more, keep the top 3 and add "Show all options".

3. **Apply the chosen fix.** If the user (or in `--afk`, the orchestrator based on research) picked a structured option:
   - Apply the JSON delta to the phase JSON.
   - Remove the `blocker_reason` top-level field.
   - Write the revised JSON to `<phases_root>/current/available/<phase_id>.json`.
   - `rm <phases_root>/current/blocked/<phase_id>.json`.
   - Print: `phase-<phase_id> unblocked, returned to available/, resuming loop`.

4. **Free-text fallback.** If the user selected "None of these — I'll explain" or typed their own answer:
   - Treat it as a conversational unblock. Propose a concrete JSON delta, get explicit confirmation, apply per step 3. If the conversation balloons (more than 2 round-trips), invoke `/rphase-unblock <phase_id>` explicitly — its conversational flow is purpose-built.

5. After all blocked phases are resolved → return to Step 1. Do NOT increment `iter_count` for unblock work.

## --pulse flag

Optional. When passed, adds a `team-pulse` member (Haiku, 90s heartbeat) to the TeamCreate-of-one. Only meaningful when transport is TeamCreate-of-one; ignored otherwise. Off by default — single-worker teams don't stall against themselves and pulse clutters the TMUX window list.

In `--afk` mode, `--pulse` applies only to big-phase teams (small phases never get a team).

## --subagent flag

Interactive mode only (`--interactive`). Switches transport from TeamCreate-of-one to plain `Agent()`. Use for headless/CI/long-plan runs where you don't need TMUX window visibility. No pulse possible with this flag.

Not available in `--afk` mode — `--afk` transport is decided per-phase from the JSON `size` field.

## Stop conditions (priority order)

1. `current/available/` empty AND `current/blocked/` empty → COMPLETE. Print one-line per-phase summary (verdict counts) and exit. Send `STOP_WATCHDOG` if watchdog is running.
2. `iter_count >= -i N` → STOP. Print remaining counts. Send `STOP_WATCHDOG` if running.
3. User interrupts (Ctrl+C / message) → loop exits naturally; in-flight worker is `TaskStop`'d, no half-written iteration record.
4. 3 consecutive WORKER_RETURN_MALFORMED → print error and exit; phase stays in `current/available/`. Send `STOP_WATCHDOG` if running.

## Worker contract (the prompt template)

```
You are working on ONE phase of a larger plan. Your scope is strictly limited
to the files listed in `files_touched` and the tasks listed in `tasks`. Do not
touch anything else.

The phase JSON is at: <ABS_PATH_TO_PHASE_JSON>
Read it. The `raw_markdown` field has the original plan wording in case you
need more context.

The orchestrator has already created your iteration directory at:
<ABS_PATH_TO_ITERATION_DIR>

[--afk only, if prior_handoffs non-empty:
## Prior phase context
<prior_handoffs block>
Use this context to avoid re-discovering facts known to prior phases.
]

Implement every task in `tasks`. Then run every command in `verification`,
capturing stdout, stderr, and exit code for each. Write the per-command
captured output to <ABS_PATH_TO_ITERATION_DIR>/worker-evidence.md as you go,
formatted as:

  ## $ <verification command>
  exit: <code>
  stdout:
  <stdout>
  stderr:
  <stderr>

If wall-clock time inside this dispatch exceeds 55 minutes, STOP — do not
start new work. Write whatever evidence you've captured so far to
worker-evidence.md. Return VERDICT: FAILED with FAILED_CHECKS:
TIMEOUT_SELF_BAIL. Do not advocate for PASSED.

[--afk only: also return the Output Schema block (status / evidence /
decisions_made / scope_drift / handoff_notes) BEFORE the VERDICT block.
The orchestrator parses the Output Schema first.]

When done, return to the orchestrator a SINGLE response in this EXACT format
(do not add prose before or after; the orchestrator parses this verbatim):

VERDICT: <PASSED|FAILED|BLOCKED>
SUMMARY: <one paragraph, ≤120 words, plain prose>
FAILED_CHECKS: <comma-separated list of which verification commands failed, or NONE>
BLOCKER_REASON: <one paragraph, only if VERDICT=BLOCKED; otherwise the literal NONE>
UNBLOCK_OPTIONS: <only meaningful if VERDICT=BLOCKED. Either NONE if you have no
clear fix in mind, or 1–3 fix options separated by " | ". Each option is
"<short label>: <specific change to phase JSON or external action>". Offer
options ONLY when your in-task context already makes a fix obvious — do not
speculate. If unsure, return NONE.>
EVIDENCE_PATH: <ABS_PATH_TO_ITERATION_DIR>/worker-evidence.md
GIT_DIFF_SUMMARY: <≤8 lines, files + line counts; output of `git diff --stat`
truncated to the relevant files>

Cardinal rule: unsure → FAILED, never PASSED. If verification ran but exit
codes / log content contradict success, that is FAILED. If a precondition
the phase assumed is wrong, that is BLOCKED — list fixes as UNBLOCK_OPTIONS
only if your in-task context makes the fix obvious.

Watch for try/except masking. If you wrapped a verification step in try/except
to make exit code 0 happen, or swallowed errors, that is FAILED.
```

## Models

| Role | Model | Why |
|---|---|---|
| Orchestrator (this session) | inherits user choice; nudge `/model sonnet` | Orchestration only. |
| Worker | **sonnet** | Coding + verification. |
| `deployer` | **sonnet** | Mechanical script + log read. |
| `phase-evaluator` | **sonnet** | Reads evidence, applies rules. |
| Reviewer (`code-review`, --afk) | **sonnet** | Reads diff + evidence + checklist. |
| Verify-committer (--afk big) | **sonnet** | Mechanical re-run + commit. |
| Fix worker (Stage B-2, --afk) | **sonnet** | Same as worker. |
| `blocker-researcher` | **opus** | Hard call — root cause + fix design. |

Pass `model:` explicitly on every dispatch. Don't rely on agent-definition defaults.

## Iteration directory layout

For every iteration, after Step 6 finishes:

```
iterations/<phase_id>/v<N>/attempt-<M>/
├── phase.json              # snapshot of phase JSON at run time
├── results.md              # final verdict + sections
├── worker-evidence.md      # worker's captured verification output
├── review.md               # reviewer's structured return (--afk, always if Stage C ran)
├── fix-attempt.md          # only present if Stage B-2 ran (--afk)
└── commit.md               # only present on done/ outcome (--afk)
```

## Verdict mapping summary (--afk)

| Path | Final state |
|---|---|
| Worker FAILED | available/, retry next iteration |
| Worker BLOCKED | blocked/, unblock conversation |
| Worker PASSED → Reviewer APPROVED → small commit OK | done/ with commit hash |
| Worker PASSED → Reviewer APPROVED → big VC COMMITTED | done/ with commit hash |
| Worker PASSED → NEEDS_CHANGES → Fix PASSED → APPROVED → commit/VC OK | done/ with commit hash |
| Worker PASSED → NEEDS_CHANGES → Fix PASSED → NEEDS_CHANGES (2nd) | blocked/ |
| Worker PASSED → NEEDS_CHANGES → Fix FAILED | available/, retry |
| Worker PASSED → NEEDS_CHANGES → Fix BLOCKED | blocked/ |
| Worker PASSED → REJECT (any pass) | blocked/ |
| Worker PASSED → Reviewer APPROVED (big) → VC VERIFICATION_GAP | blocked/ |

## Rules

- **Filesystem protocol is shared with `/rphase-run` and the bash loop.** Phase file moves, `iterations/<phase>/v<N>/attempt-<M>/{phase.json,results.md}`, `unblocked/<phase>/v<N>.json` — identical layout. You can mix and match runs freely.
- **Every iteration must write its iteration record before moving the phase file.** The audit trail is append-only and non-negotiable.
- **Never `mv` the phase file before VERDICT is fully decided.** Move only as the very last action of Step 6.
- **Always `TeamDelete` between phases in TeamCreate mode.** Each phase gets a fresh team. TMUX window history persists in terminal scrollback.
- **Background dispatch + TaskStop is the timeout floor.** Self-bail at 55min is best-effort; orchestrator's wall-clock + TaskStop is the guarantee.
- **Don't dispatch more than 2 subagents per iteration (--interactive).** Worker, plus optionally one of {deployer, phase-evaluator, blocker-researcher}.
- **At most ONE Stage B-2 fire per iteration (--afk).** A second consecutive NEEDS_CHANGES blocks.
- **Stage D never auto-fixes (--afk).** Verification gaps after APPROVED review block — by design.
- **Watchdog is mandatory in --afk.** Send `STOP_WATCHDOG` on loop exit.
- **Handoff injection is conditional on `execution_mode` (--afk).** Sequential → glob all prior `done/phase-*/results.md` handoffs. Parallel → respect `depends_on` only.
- **Decisions get appended to `decisions.md` for every non-empty `decisions_made` list (--afk).** Never overwrite — always append.
- **Canonical per-phase `results.md` is mandatory on PASSED (--afk).** Lives at `<phases_root>/done/<phase-id>/results.md` with frontmatter.

## Team member lifecycle

Subagents (`Agent()` dispatch) auto-close on return. No accumulation. Safe.

Team members (TeamCreate roster) stay addressable for the whole session. Accumulating idle team members eats the leader's context window.

Lifecycle rule for the leader, applied at every transition (worker→reviewer, reviewer→fix-worker, fix-worker→reviewer, blocker→unblocker):

- **Leader context window <40% full** — reuse an existing team member via SendMessage is OK.
- **40–60% full** — judgment call. Reuse only if the next task is very similar (same files, same scope). Otherwise destroy and respawn.
- **>60% full** — always `TeamDelete` the old member and spawn a fresh one. Fresh context is priority.

Never accumulate idle team members. One step done → reuse, replace, or delete. No idle survivors.

## When to use which mode

- **`/cc-loop --interactive`**: plans you want to drive yourself, plans without size tags, plans where blocks are expected and you want conversational resolution.
- **`/cc-loop --afk`**: AFK runs after `/cc-plan-and-grill --route-phases`. Zero touch — kick off, walk away, come back to commits and a shortlist of blocked phases.
- **bash `tools/ralph-phase-loop.sh`**: unattended/CI runs that need maximum process isolation (fresh `claude -p` per phase). Lower review automation but maximum isolation. Blocks halt it; re-launch after manual unblock.

All three share the phase tree filesystem. You can switch between them mid-plan as long as size tags are present (or you're not using `--afk`).
