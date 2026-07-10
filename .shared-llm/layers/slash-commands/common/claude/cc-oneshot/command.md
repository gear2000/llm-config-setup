# cc-oneshot — Research + Plan + Implement, in one shot

The lightweight shortcut through the build pipeline. For tasks that need some research and a sketch of a plan but don't warrant the full `/cc-plan-and-grill` ceremony with a persisted plan.md.

## Cardinal rule: delegate, don't do

Your job as the team leader is to **orchestrate**. Every concrete unit of work — reading code, exploring the repo, writing files, running commands, drafting tests, deploying — must be delegated to a fresh subagent (or a named team member, with `--team`).

If you are about to do work yourself — read a file, write code, run a command, push to your Git remote — **stop, dispatch a fresh subagent for the work, return to orchestrating.**

Why: leader context fills up fast when it does work. Subagents have fresh context windows. Delegate, delegate, delegate.

## Invocation

```
/cc-oneshot [--team] <title>
```

- **`<title>`** — Required. Slugified for the work-log directory.
- **`--team`** — Optional. Wraps agents in TeamCreate (TMUX windows, named members, peer SendMessage, team-pulse). Default is subagents.

## When to use this vs other cc- skills

- **`/cc-research`** — research only, no implementation
- **`/cc-plan`** — research + persisted plan.md, no implementation
- **`/cc-plan-and-grill`** — research + persisted plan + iterative grill, no implementation
- **`/cc-oneshot`** — research + sketch + implement, all in one shot, no persisted plan ← **you are here**
- **`/cc-implement`** — execute an existing persisted plan.md

If you find yourself wanting to revisit decisions or re-run with different scope, you've outgrown `/cc-oneshot` — bail out and use `/cc-plan-and-grill` instead.

## Workflow

### Step 1: Brief Research (delegate to Explore)

Slugify the title, determine version by globbing `~/project/repos/{{OPS_REPO}}/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/oneshot/v*/`.
<!-- <YYYY-MM-DD> is the ISO date the work was started (NOT modified). Use the date at the time of the first invocation of this skill in this work-log. -->

Dispatch **1 Explore subagent** (or named team member with `--team`) with a focused question covering: what the change touches, current behavior, constraints, prior incidents in git history if relevant. Tell it to return a brief summary — no need to write a full research.md unless the work later turns out to warrant one.

Output paths use the form `work-log/<YYYY-MM-DD>/<slug>/oneshot/v<N>/...`.

If the scope is unclear, dispatch a second Explore agent for a different angle.

### Step 2: Grill + Sketch (HTML)

Even a one-shot resolves its open questions before it runs — but lightly: usually ONE quick HTML grill round, not the full plan-and-grill grind. Skip the grill only if the Explore returns left genuinely nothing to decide.

1. From the Explore returns, work out the handful of questions worth asking — scope edges, the real choices, anything the change hinges on.
2. Ensure the work-log static server is up (idempotent — no-op if already running): `bash ~/project/repos/{{OPS_REPO}}/tools/serve-worklog.sh up`
3. **Dispatch a subagent** to write a fresh `grill-v<round>.html` (kept for history) plus `grill_current.html` (same content — the tab you keep open) into `~/project/repos/{{OPS_REPO}}/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/oneshot/v<N>/`:
   - A short context header (what's being changed; the shape so far from the Explore summary).
   - Keep every question tight — bullets, never a sentence over two lines, plain English. When a question is complex, SHOW it with a diagram — two modes only, NEVER Mermaid (CDN-rendered, breaks silently on any syntax slip): default → an ASCII tree in a `<pre>`; when ASCII can't carry it → the row-by-row HTML flow (`.grill-fig` / `.flow` / `.flow-box`, styled by the grill style toolkit). A diagram only when it genuinely helps — never for its own sake.
   - `<meta name="desdoc-key" content="<date>-<slug>-oneshot-r<round>">` in `<head>` — unique per round, so the annotation toolkit starts each round with a clean slate (`grill_current.html` reuses one path across rounds).
   - One `<div class="grill-q">` block per question — `.grill-q-text`, an optional `.grill-q-note` (why it matters), and your `.grill-q-rec` recommendation. **No answer boxes** — the user answers by dropping a sticky note on the block.
   - Question/diagram styles from `.shared-llm/llm/common/common/toolkits/form-toolkit.html` (style-only) and the sticky-note annotation controls from `.shared-llm/llm/common/common/toolkits/annotation-toolkit.html` pasted verbatim before `</body>` — the annotation bar is the page's ONLY interactive control. `<title>` = `<Title> — grill v<N>`.
4. Tell the user: _"Grill is up — http://<host>:8089/work-log/<YYYY-MM-DD>/<slug>/oneshot/v<N>/grill_current.html — drop a note on anything to answer or change (+ Note), click Copy Feedback, paste back here. No note on a question = the recommendation stands."_ `<host>` = the `host:` field of the nearest `.planish.yaml` when set (the machine name remote browsers use, e.g. a Tailscale name), else `localhost`. If this session has a file-send tool (e.g. `SendUserFile` in the Claude Code app), also send `grill_current.html` as a downloadable file — remote/app sessions often can't reach the URL, and the page is self-contained so it works opened straight from a download.
5. The user pastes the `## Feedback —` block (each note is tagged with the nearest question/heading; un-noted questions mean the recommendation is accepted). Fold the feedback into the sketch. One round is enough for a one-shot; re-grill only if a note opened a real fork.

Then present the resolved sketch and get the go-ahead:
- What you'll do (1–3 bullet points)
- Which agent(s) will do the work
- What "done" looks like (verification criteria)
- Whether this needs `deployer` for live verification

Once confirmed, **dispatch a writer subagent** to persist the sketch as a dual `plan.md` + `plan.html` in `~/project/repos/{{OPS_REPO}}/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/oneshot/v<N>/`:

- `plan.md` — the canonical sketch, written lightweight: the 1–3 bullets of what you'll do, the agent(s), the "done" criteria, and whether `deployer` is needed. No plan-and-grill ceremony — no required Executive Summary, no Team section, no `**Size**:` tags.
- `plan.html` — the same sketch in the project dark style (the `<style>` block from `~/project/repos/{{OPS_REPO}}/mkdocs/docs/diagrams/architecture/v3.html` if it exists, else the default style block from the `/design-doc` skill), with `.shared-llm/llm/common/common/toolkits/annotation-toolkit.html` pasted verbatim immediately before `</body>`. `<title>` = `<Title> — plan v<N>`.

The persisted plan is a record of intent, not a gate — write it, then move straight to implementing. Do not re-grill.

### Step 3: Dispatch watchdog + implement

**Dispatch plan-watchdog (mandatory).** Even ad-hoc gets a watchdog — it's the cheapest insurance against scope creep:
```
Agent(
  subagent_type="plan-watchdog",
  name="watchdog",
  model="sonnet",
  run_in_background=true,
  prompt="Watch /cc-oneshot run titled <title>. Poll every 60–90s.
          Flag scope drift (worker touching files outside the sketch's
          file list), worker FAILED repeatedly, missing decisions/results
          writes. FLAG → BLOCK → HALT. Do NOT take corrective action."
)
```

Dispatch **1 worker** chosen from the change scope:
- `.py` → `backend`
- `.tsx`, `.ts`, `.jsx`, `app/` → `frontend`
- `.tf`, `.hcl`, `Dockerfile`, `terraform/` → `devops`
- schema SQL, migrations → `database`

**Worker prompt MUST include:**
```
Implement the change described above. Scope is strictly limited to <files>.
After your code and tests pass:

1. Write captured output (test results, files changed, verification command
   outputs) to <evidence-dir>/<gate>.log files.
2. Return the Output Schema block.

## Output Schema (return EXACTLY this block at end of your message)

### Result
- status: PASSED | FAILED | BLOCKED
- evidence:
  - review: <path or "skipped: <reason>">
  - deploy: <path or "skipped: <reason>">
  - live:   <path or "skipped: <reason>">
- decisions_made:
  - <one-line each — will be appended to decisions.md by the leader>
- scope_drift:
  - <tech debt found mid-task that this task did NOT address>
- handoff_notes: |
  <≤200 words for any follow-up worker — pitfalls, state.>

Do NOT say "done" — say "ready for review and deploy." The leader judges
the verdict, not you.
```

**With `--team`:** also spawn `team-pulse` for the 90s liveness heartbeat.

### Step 4: Code Review (delegate)

When the worker reports ready, **dispatch `code-review`** to read the git diff. Look for silent errors, broad `try/except`, swallowed exceptions, retry loops that hide failures, hardcoded secrets. Do NOT read the diff yourself — always dispatch a subagent for this.

If issues found: dispatch the worker again with fixes.

### Step 5: Deploy + Verify (delegate)

If the change touches anything live, dispatch **`deployer`** with the standard prompt:

```
Push to your Git remote. Trigger your CI/deploy
pipeline. Read pipeline logs in full — not just exit codes. Hit the live
endpoints. Return URLs, status codes, log excerpts. No evidence = not deployed.
```

Logs must be clean. If not, dispatch the worker to fix and re-run the deployer.

### Step 6: Write Results + Decisions + Clean Up

1. **Dispatch a writer subagent** to compose **two outputs** in `~/project/repos/{{OPS_REPO}}/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/oneshot/v<N>/` — `results.md` (canonical) and `results.html` (the visual, annotatable surface you read in the browser). Same content; markdown is the agent/tooling surface, HTML is the human surface. `results.md` has the required frontmatter:

   ```markdown
   ---
   status: Done
   tldr: One sentence. What this oneshot accomplished.
   decision: One sentence if a decision was made; omit otherwise.
   links:
     - decisions: ../../decisions.md
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

   Use "None needed" when the work is complete and nothing remains. Use a link list when one or more follow-up items exist — each link points to a separate file in `~/project/repos/{{OPS_REPO}}/mkdocs/docs/followup/<YYYY-MM-DD>/<slug>/<name>.md` that the writer should ALSO create with its content.

   When creating a followup/<date>/<slug>/<name>.md file, use frontmatter:
   ```yaml
   ---
   status: Open
   parent: <link back to results.md>
   tldr: <one-sentence summary>
   ---
   ```

   **`results.html`** — the same content as `results.md`, rendered in the project dark style (the `<style>` block from `~/project/repos/{{OPS_REPO}}/mkdocs/docs/diagrams/architecture/v3.html` if it exists, else the default style block from the `/design-doc` skill), with `.shared-llm/llm/common/common/toolkits/annotation-toolkit.html` pasted verbatim immediately before `</body>`. `<title>` = `<Title> — results v<N>`. The frontmatter and `## Follow-up` discipline above govern `results.md`; the HTML twin just mirrors the rendered findings for reading and annotation.

   Always dispatch a writer subagent for this — do not write the files yourself.

2. **Append decisions** from the worker's `decisions_made` to `<work-log-root>/decisions.md` (path: `~/project/repos/{{OPS_REPO}}/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/decisions.md`). Create the file if it doesn't exist. Format:

   ```markdown
   ## <ISO-8601 timestamp> [SCOPE|ARCH|CODE|...] <decision title>
   - <body>

   Refs: oneshot/v<N>/results.md
   ```

3. **Dispatch nav-sync** to update `~/project/repos/{{OPS_REPO}}/mkdocs/mkdocs.yml`.
4. **Send `STOP_WATCHDOG`** to plan-watchdog (from Step 3).
5. **If `--team`:** SendMessage `STOP_PULSE` to team-pulse.
6. Report the summary to the user.

Results must include: what was done, code review status, deployment evidence (URLs, status codes), **log verification status**.

### Nav Structure

```yaml
- Logs:
    - <YYYY-MM-DD>:
        - <title>:
            - Oneshot:
                - v<N>: work-log/<YYYY-MM-DD>/<title>/oneshot/v<N>/results.html
```
