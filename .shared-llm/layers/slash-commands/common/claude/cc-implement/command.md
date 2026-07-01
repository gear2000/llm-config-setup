# cc-implement — Execute Plan

## Cardinal rule: delegate, don't do

Your job as the team leader is to **orchestrate**. Every concrete unit of work — reading code, exploring the repo, writing files, running commands, analysing diffs, drafting tests — must be delegated to a fresh subagent (or to a named team member, with `--team`).

If you are about to do work yourself — anything beyond dispatching agents, reading their returns, and deciding the next step — **stop, dispatch a fresh subagent for the work, return to orchestrating.**

Why: your context window fills as you work. Once it's full, your decision quality degrades and downstream work accumulates tech debt. Subagents have fresh context windows; using them keeps the leader sharp and the work clean. Delegate, delegate, delegate.

The leader's tools are: dispatch agents, read their returns, decide next step, repeat. That's it.

## THE GATE — Read This First, Obey It Last

**You CANNOT report success, write `results.md`, or tell the user "done" until ALL of these are true:**

1. The plan's code changes have been **pushed to your Git remote and your CI/deploy pipeline triggered** — by a `deployer` agent you dispatched.
2. Pipeline logs have been **read in full and confirmed clean** (not just exit code — actual log content) by an agent you dispatched.
3. Live endpoints have been **hit and confirmed correct** by an agent.
4. You have URLs, status codes, and log excerpts in this conversation as evidence.

This gate is non-negotiable. No exceptions.

When you create `.state/claude/cc-implement-active`, you are committing to this gate. It stays until you can honestly delete it after full verification.

---

## Invocation

```
/cc-implement [--team] <path-to-plan.md>
```

- **`<path-to-plan.md>`** — Required. If missing, ask for it. Plans produced by `/cc-plan-and-grill` live under `work-log/<YYYY-MM-DD>/<slug>/plan/v<N>/plan.md` where `<YYYY-MM-DD>` is the ISO date the work was started.
  <!-- <YYYY-MM-DD> is the ISO date the work was started (NOT modified). Use the date at the time of the first invocation of the planning skill in this work-log. -->
- **`--team`** — Optional. Wraps agents in `TeamCreate` so members are named, addressable, can `SendMessage` peers, and run in their own TMUX windows. Adds `team-pulse` heartbeat. Default is subagents (no `TeamCreate`) for fresh-per-call context windows.

## Two transports, same delegation model

### Default: subagents

Each unit of work is a fresh `Agent(...)` call without `team_name`. The agent runs, returns its result, and disappears. You read the return, decide the next step, dispatch the next subagent. No persistent team, no `team-pulse`, no `SendMessage` between agents — they don't see each other.

Benefits: tight per-call context windows, lower running cost, simpler mental model. Use this most of the time.

### `--team`: TeamCreate

Wrap the work in a named team. Each agent is spawned with `team_name=<slug>-implement` and `name=<role>`. They can `SendMessage` each other. You also spawn `team-pulse` as a 90s liveness heartbeat. Each member gets a TMUX window for live observability.

Benefits: peer-to-peer messaging (rare but real), TMUX windows for monitoring long runs. Use when work is long-running and you want to watch it, or when peer agents genuinely need to talk to each other.

## The plan owns the roster

The plan's `## Team` section names exactly which agents to spawn. Read the plan, honour what it says — it's the contract.

**If the plan has a Team section** — dispatch those agents (as subagents by default; as named team members with `--team`).

**If the plan has no Team section** — default roster is **1 worker** chosen by the plan's `files_touched`:
- `.py` → `backend`
- `.tsx`, `.ts`, `.jsx`, `app/` → `frontend`
- `.tf`, `.hcl`, `Dockerfile`, `terraform/` → `devops`
- schema SQL, migrations → `database`

You handle watchdog, code review, and deploy decision — but those roles also delegate. "Reviewing the diff" means dispatching a `code-review` subagent. "Deploying" means dispatching a `deployer` subagent. "Watching for plan drift" means dispatching a `plan-watchdog` (when the plan asks for one).

**Summon specialists ad-hoc.** Even mid-execution, if you need a specialist the plan didn't include (`deployer` for an unexpectedly hairy deploy, `database` for a migration that surfaced, `security` for an auth flow that turned out trickier), spawn them. The plan's roster is a floor, not a ceiling.

## Workflow

### 1. Read Plan + Mark Active + Dispatch Watchdog

1. **Dispatch a reader subagent** to read `plan.md` (and `research.md` if present) and return a structured summary: frontmatter (status/tldr/decision/execution), phases, agent assignments from the Team section, verification criteria, files touched.
2. Verify the plan has the required frontmatter AND phases AND agent assignments AND verification criteria. If frontmatter is missing or malformed, refuse to proceed and tell the user to regenerate the plan via `/cc-plan-and-grill`. If phases/Team/verification missing, ask the user to run `/cc-plan-and-grill` first.
3. Note the `execution:` field from frontmatter (default `sequential`). This drives handoff injection in Step 2.
4. Create marker: `echo "$(date -Iseconds) $PLAN_PATH" > .state/claude/cc-implement-active`.
5. **Dispatch plan-watchdog (mandatory).** Watchdog is default-on for `/cc-implement`. Spawn it before the first phase:
   ```
   Agent(
     subagent_type="plan-watchdog",
     name="watchdog",
     model="sonnet",
     run_in_background=true,
     prompt="Watch /cc-implement run for plan <path>. Poll every 60–90s.
             Flag scope drift (worker touching files outside the plan's
             files_touched), missing results.md / decisions.md writes on
             completed phases, repeated worker FAILED. Escalation ladder:
             FLAG → BLOCK → HALT. Do NOT take corrective action."
   )
   ```
6. Present summary to user, confirm proceed.

Always dispatch a reader subagent for this — do not read plan.md yourself regardless of plan size.

### 2. Dispatch Phase Work

For each phase in the plan, in order (if `execution: sequential`) or as the dependency graph allows (if `execution: parallel`):

**Handoff injection (sequential mode):** before dispatching phase N's worker, collect handoff sections from `<plan-dir>/phases/done/phase-*/results.md` (if the per-phase results.md pattern is in use) OR the prior phases' summaries you've collected in this session. Inject as a `## Prior phase context` block in the worker prompt.

**Default (no `--team`):**
- Spawn the phase's worker as a subagent with the phase's task list as its prompt. Tell it to write its captured output to `<plan-dir>/phases/done/<phase-id>/evidence/<gate>.log` files, then return the Output Schema block.

**With `--team`:**
- `TeamCreate(team_name="<slug>-implement", description="Implement: <title>")` once at start.
- Spawn each phase's worker as a named team member.
- Spawn `team-pulse` once with the standard 90s heartbeat prompt.

**Worker prompt MUST include:**
```
You are implementing ONE phase of a plan. Your scope is strictly limited to
the phase's tasks. Stay within files_touched.

## Prior phase context

<inject collected handoffs here. Omit block if empty.>

## Implementation rules

- Implement only what files_touched + tasks describe. Tech debt outside that
  scope goes in scope_drift (Output Schema below) — do NOT fix it now.
- Decisions the plan didn't anticipate (naming, library choice, API shape)
  go in decisions_made.
- After implementing, run all verification commands and capture stdout,
  stderr, exit codes to <plan-dir>/phases/done/<phase-id>/evidence/<gate>.log
  files.

## Output Schema (return EXACTLY this block at end of your message)

### Result
- status: PASSED | FAILED | BLOCKED
- evidence:
  - review: <path under phases/done/.../evidence/ OR "skipped: <reason>">
  - deploy: <path OR "skipped: <reason>">
  - live:   <path OR "skipped: <reason>">
- decisions_made:
  - <one-line each — will be appended to decisions.md by the leader>
- scope_drift:
  - <tech debt found mid-phase that this phase did NOT address>
- handoff_notes: |
  <≤200 words for the next phase's worker — key decisions, pitfalls, state
   the next agent needs.>

Do NOT say "done" — say "ready for review and deploy." The leader judges
the verdict, not you.
```

### 3. Monitor + Review (orchestrating, not working)

- **When a worker returns:** dispatch `code-review` (subagent or team member) to review the git diff. Do not read the diff yourself — always dispatch a subagent for this. The reviewer looks for silent errors, broad `try/except`, swallowed exceptions, retry loops that hide failures, hardcoded secrets.
- **When code-review reports issues:** dispatch the worker again with the fixes needed. Iterate until clean.
- **If with `--team`:** when `team-pulse` pings you, reply briefly, then `SendMessage` each teammate to confirm progress. Push if stalled.
- **If a phase fails:** stop dependent phases, report to the user, wait.

### 4. Deploy — Delegate the gate

Dispatch `deployer` (subagent by default; named member with `--team`) with the prompt:

```
Push to your Git remote. Trigger your CI/deploy pipeline.
Read pipeline logs in full — not just exit codes. Hit the live endpoints listed
in the plan's verification section. Return URLs, status codes, log excerpts.
No evidence = not deployed.
```

When it returns, verify the evidence is real — do NOT replay the deploy yourself, always dispatch a subagent for that. If the deployer comes back without evidence, or with evidence that the logs aren't clean, dispatch the worker again to fix and re-run the deployer.

### 5. Per-Phase Results + Decisions

For EACH phase as it completes (not at the end — per-phase):

1. **Dispatch a writer subagent** to compose the canonical `<plan-dir>/phases/done/<phase-id>/results.md` from the worker's Output Schema block + reviewer outputs + deploy/live evidence. Format per `mkdocs/docs/work-log/README.md`:
   ```markdown
   ---
   status: Done
   tldr: ...
   decision: (if any)
   links: [...]
   ---

   ## What changed
   - <≤5 bullets from worker output>

   ## Verification evidence
   - review / deploy / live: paths or skipped reasons

   ## Scope drift
   - <from worker's scope_drift>

   ## Handoff
   <verbatim worker's handoff_notes, ≤200 words>

   ## Follow-up
   None needed
   ```

   The results.md MUST end with a `## Follow-up` section containing EXACTLY one of:
   - `None needed` (single line, no other text)
   - One or more `[label](../../../followup/<YYYY-MM-DD>/<slug>/<name>.md)` links

   Use "None needed" when the work is complete and nothing remains. Use a link list when one or more follow-up items exist — each link points to a separate file in `~/project/repos/your-repo-ops/mkdocs/docs/followup/<YYYY-MM-DD>/<slug>/<name>.md` that the writer should ALSO create with its content.

   When creating a followup/<date>/<slug>/<name>.md file, use frontmatter:
   ```yaml
   ---
   status: Open
   parent: <link back to results.md>
   tldr: <one-sentence summary>
   ---
   ```
2. **Append decisions** from the worker's `decisions_made` list to `<work-log-root>/decisions.md`. Compute work-log root from plan path. Format:
   ```markdown
   ## <ISO-8601 timestamp> [SCOPE|ARCH|CODE|...] <decision title>
   - <body>

   Refs: phases/done/<phase-id>/results.md
   ```

### 6. Final Write Results + Clean Up

Once Step 4 (deploy gate) is satisfied AND every phase has its own results.md:

1. **Dispatch a writer subagent** to compose a top-level `<plan-dir>/results.md` summary that links to each phase's results.md. Path: same directory as plan.md. Always dispatch a writer subagent for this — do not write the file yourself.
2. **Dispatch a general-purpose subagent** to update `~/project/repos/your-repo-ops/mkdocs/mkdocs.yml` nav.
3. **Send `STOP_WATCHDOG`** to plan-watchdog (from Step 1).
4. **If `--team`:** SendMessage `STOP_PULSE` to team-pulse.
5. Remove marker: `rm -f .state/claude/cc-implement-active`.

### Nav Structure

```yaml
- Logs:
    - <YYYY-MM-DD>:
        - <title>:
            - Plan:
                - v<N>:
                    - Research: work-log/<YYYY-MM-DD>/<title>/plan/v<N>/research.md
                    - Plan: work-log/<YYYY-MM-DD>/<title>/plan/v<N>/plan.md
                    - Results: work-log/<YYYY-MM-DD>/<title>/plan/v<N>/results.md
```
