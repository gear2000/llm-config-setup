# do-research — Research → Stop

Pure research and exploration. The leader does not investigate the codebase or write the file directly — it dispatches Explore agents and a synthesizer.

## Cardinal rule: delegate, don't do

Your job is to **orchestrate**. Reading code, exploring directories, running greps, drafting research.md — every concrete unit of work is a fresh subagent (or a named team member, with `--team`).

If you are about to do work yourself — read a file, search the repo, write research.md — **stop, dispatch a fresh subagent for the work, return to orchestrating.**

Why: leader context fills up fast when it does work. Subagents have fresh context; using them keeps the leader sharp and the research clean. Delegate, delegate, delegate.

## Invocation

```
/do-research [--team] <title>
```

- **`<title>`** — Required. Slugified for directory name. Example: `worker-pipeline-v2`
- **`--team`** — Optional. Wraps agents in TeamCreate (TMUX windows, named members, peer SendMessage). Default is subagents.

## Workflow

### Step 1: Setup

1. Slugify the title. Determine version by globbing `~/project/repos/your-repo-ops/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/research/v*/`.
   <!-- <YYYY-MM-DD> is the ISO date the work was started (NOT modified). Use the date at the time of the first invocation of this skill in this work-log. -->
2. Ask brief clarifying questions about scope (1–2 max — the Explore agents will surface details).
3. Decide the research aspects to cover. For a small task, one Explore agent is enough. For a broader question, dispatch multiple Explore agents in parallel covering different angles (data flow, callers, tests, prior incidents in git history, etc.).

### Step 2: Dispatch Exploration

**Default (no `--team`):**
- For each aspect, spawn an `Explore` subagent with a focused question, instructed to write its findings to `~/project/repos/your-repo-ops/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/research/v<N>/sections/<aspect>.md` and return a one-paragraph summary.
- If you can run them in parallel (independent aspects), dispatch multiple `Agent` calls in a single message.

**With `--team`:**
- `TeamCreate(team_name="<slug>-research", description="Research: <title>")` once.
- Spawn each Explore agent as a named team member.
- Optional: spawn `team-pulse` if research is expected to take long enough to risk stalling.

**Explore agent prompt MUST include:**
```
Focus: <specific question or area>.
Write your findings to <path>. Cover: affected files, current behavior,
constraints, patterns to respect, risks, edge cases, prior incidents in
git history if relevant. Return a one-paragraph summary to the leader.
```

### Step 3: Synthesize (dual output — Markdown + HTML)

When all Explore agents have returned, **dispatch a synthesizer subagent** with the section-file paths. The synthesizer reads each section file and writes **two consolidated outputs** into `~/project/repos/your-repo-ops/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/research/v<N>/`:

1. **`research.md`** — the canonical, token-lean record. This is what an agent reads back as context when producing the next version, and what gets archived. Markdown.
2. **`research.html`** — the visual, annotatable surface **you** read in the browser. Same content as the markdown, rendered in the project's dark style with the sticky-note annotation toolkit baked in.

Both hold the same findings — the markdown is the agent surface, the HTML is the human surface.

The synthesizer's prompt MUST require frontmatter at the top of `research.md`:

```yaml
---
status: Proposed | Accepted | In-Progress | Done | Superseded
tldr: One sentence. What this research is and its current state.
decision: (research docs frame the decision-question, not a committed decision — one sentence stating what the plan that follows will decide)
links:
  - sections: ./sections/
  - plan: ../../plan/   # if a plan exists or is expected
  - decisions: ../../../decisions.md
---
```

**`research.html` requirements (give these to the synthesizer verbatim):**
- Use the dark style from `~/project/repos/your-repo-ops/mkdocs/docs/diagrams/architecture/v3.html` (`<style>` block) if it exists; otherwise the default style block from the `/design-doc` skill.
- Render the same findings as the markdown — structured with headings, tables, and the flow/box visual vocabulary where a diagram helps. Not a wall of text.
- Paste the contents of `.shared-llm/llm/claude/common/toolkits/annotation-toolkit.html` verbatim immediately before `</body>`.
- `<title>` = `<Title> — research v<N>`.

The synthesizer must also keep verbose dumps (raw transcripts, full scan output, full curl bodies) in an `evidence/` sibling directory — NOT inlined in either output. See `mkdocs/docs/work-log/README.md` for the full contract.

Do NOT read the section files yourself or write the outputs yourself — always dispatch a synthesizer subagent for this.

### Step 4: Persist and Stop

1. **Dispatch a nav-sync subagent** to update `~/project/repos/your-repo-ops/mkdocs/mkdocs.yml` under `- Logs:`.
2. If `--team`: SendMessage `STOP_PULSE` to team-pulse if it was running.
3. Ensure the work-log static server is up (idempotent — no-op if already running): `bash ~/project/repos/your-repo-ops/tools/serve-worklog.sh up`
4. Tell the user:
   ```
   Research saved.
   Read:     http://localhost:8089/work-log/<YYYY-MM-DD>/<slug>/research/v<N>/research.html
   Markdown: <path>/research.md

   Annotate in the browser, click "Copy Feedback", paste it back here to get v<N+1>.
   ```
5. **STOP.** Do not plan, do not implement.

## Iteration — handling pasted feedback

When the user pastes output that starts with `## Feedback —` or `## FINALIZED —` and the `File:` line points at a `research/v<N>/research.html`:

1. Compute the next version `v<N+1>`.
2. **Dispatch a synthesizer subagent** with: the pasted notes, the path to the current `research.md` (it reads this as the lean base — NOT the HTML), and any new exploration needed to address the notes.
3. The synthesizer writes `research/v<N+1>/research.md` + `research.html` (same dual-output contract as Step 3), addressing every note.
4. Dispatch nav-sync, then report the new paths + URL.

The agent always reads the **markdown** of the prior version as its base, never the HTML — the HTML carries the annotation toolkit and styling that would just burn tokens.

### Nav Structure

```yaml
- Logs:
    - <YYYY-MM-DD>:
        - <title>:
            - Research:
                - v<N>: work-log/<YYYY-MM-DD>/<title>/research/v<N>/research.html
```
