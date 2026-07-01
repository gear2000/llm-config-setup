---
name: do-planish
description: 'Your everyday HTML-first planner — one focused research pass, an HTML-batch grill, and a dual plan.md + plan.html you can read and annotate in the browser. Lighter than /do-plan-and-grill (no --route-phases, no required Team section, no decisions log; reach for plan-and-grill on heavy or risky work that wants multi-aspect research and per-phase size tags). Heavier than /do:oneshot (which sketches and implements in one shot). Flow: research (one Explore subagent) → HTML-batch grill → finalize dual plan → STOP. Default transport is subagents (fresh context per call); pass --team to run inside TeamCreate. The leader orchestrates and runs the grill conversation; it does not read code or write files itself — it always dispatches a subagent for that work. Invocation: /do-planish [--team] <title>.'
---

# do-planish — Research → Grill → Plan (HTML-first express lane)

The everyday HTML-first planner. One focused research pass, an HTML-batch grill, and a dual `plan.md` + `plan.html` you can read and annotate. Lighter than `/do-plan-and-grill` (no `--route-phases`, no required Team section, no decisions log); heavier than `/do-oneshot` (which sketches and implements in one shot, no persisted plan). Use it when you want a real plan you can read and mark up — without the full grill ceremony.

## Cardinal rule: delegate, don't do

Your job is to **orchestrate** and to **run the grill conversation**.

- Reading code, writing research.md, drafting plan.md / plan.html, applying plan revisions: dispatch fresh subagents.
- The grill itself (deciding each round's questions, processing the pasted answers): you do this — that's the orchestration loop, not "work."

If you find yourself reading files, writing markdown, or running commands directly, **stop, dispatch a fresh subagent for the work, return to orchestrating.**

## Invocation

```
/do-planish [--team] <title>
```

- **`<title>`** — Required. Slugified for the work-log directory.
- **`--team`** — Optional. Wraps agents in TeamCreate (TMUX windows, named members). Default is subagents.

## When to use this vs other do: skills

- **`/do-research`** — research only, no plan
- **`/do-oneshot`** — research + sketch + implement in one shot
- **`/do-planish`** — research + HTML-batch grill + persisted dual plan, no implementation ← **you are here**
- **`/do-plan-and-grill`** — the heavy planner: multi-aspect research fan-out, full grill, Team section, decisions log, optional `--route-phases` size tags
- **`/do-implement`** — execute an existing persisted plan.md

If the work is risky or large enough that you want multi-aspect research, a Team roster, per-phase size tags, or a decisions log, reach for `/do-plan-and-grill` instead.

## Workflow

### Step 1: Setup + Research (delegate to ONE Explore)

1. Slugify the title. Determine version by globbing `~/project/repos/your-repo-ops/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/plan/v*/`.
   <!-- <YYYY-MM-DD> is the ISO date the work was started (NOT modified). Use the date at the time of the first invocation of this skill in this work-log. -->
2. Ask 1–2 brief scope questions — save the real questions for the grill.
3. **Dispatch EXACTLY ONE Explore subagent** (or one named team member with `--team`) with a single focused question covering: what the change touches, current behavior, constraints, and prior incidents in git history if relevant. This is the light path — NOT the multi-aspect fan-out `/do-plan-and-grill` uses.
   - It returns a brief summary. It MAY write a short `research.md` into `.../plan/v<N>/research.md` if the work warrants one — but with no required frontmatter and no section ceremony. A paragraph or two is fine; skip it entirely for small work.

Output paths use the form `work-log/<YYYY-MM-DD>/<slug>/plan/v<N>/...`.

### Step 2: Grill (HTML-batch)

> This is the core step. Don't rush it. Every open decision gets resolved before you finalize.

<!-- NOTE: this grill->build->review flow mirrors the planish Pi extension (planish.ts) and tf-implement.ts. The ~10-line prompt is intentionally duplicated, NOT shared. Keep in sync. -->

Same mechanism as `/do-plan-and-grill` Step 4's HTML-batch grill. You run the grill directly — it's orchestration, not "work." There is no separate draft step in this light path: you grill off the research summary, holding the plan shape in your head as it firms up, and the plan gets written once at finalize.

Ensure the work-log static server is up (idempotent — no-op if already running): `bash ~/project/repos/your-repo-ops/tools/serve-worklog.sh up`

Each round:
1. Work out every question you can ask **right now** — split independent (answerable in any order) from dependent (wording hinges on an independent answer). Ask the independent ones this round.
2. **Dispatch a subagent** to write `grill_current.html` into `.../plan/v<N>/grill_current.html` — write a fresh `grill-v<round>.html` each round (kept for history) and overwrite `grill_current.html` with the same content, then refresh the tab (kept on `grill_current.html`) to load the new round (the static server serves the raw file — no live-reload). Give the subagent the research summary, the decisions locked so far, and this round's questions. The file contains:
   - A context header: what's being planned, the plan shape so far (from the research + answers so far), and enough background that each question stands on its own — headings/tables, not a wall of text.
   - Keep every question tight — bullets, never a sentence over two lines, plain English. When a question is complex, SHOW it with a diagram chosen by complexity: simple → a Mermaid block (add its `<script>` to the page `<head>`); a bit more complex → an ASCII tree in a `<pre>`; quite complex → the row-by-row HTML flow (`.grill-fig` / `.flow` / `.flow-box`, styled by the form toolkit). A diagram only when it genuinely helps — never for its own sake.
   - One `<div class="grill-q">` block per question, each with `.grill-q-text` (the question), an optional `.grill-q-note` (why it matters) and `.grill-q-rec` (your recommendation), and a `<textarea class="grill-a">`.
   - The form toolkit pasted verbatim before `</body>` from `.shared-llm/llm/claude/common/toolkits/form-toolkit.html`.
   - `<title>` = `<Title> — grill v<N>`.
3. Tell the user the round is ready: _"Round <k> is up — http://localhost:8089/work-log/<YYYY-MM-DD>/<slug>/plan/v<N>/grill_current.html — fill it in, click Copy Answers, paste back here."_
4. The user pastes the `## Answers —` block. Process every answer: update your running understanding of the plan, then compute the next round's questions (the dependent ones now have concrete wording, plus anything the answers newly surfaced).
5. Repeat. The LLM decides each round's batch size — converge as questions run out. A round with no open questions ends the grill.

**Rules that don't change:**
- Always recommend. Every question carries your best answer + reasoning, never a bare ask.
- Challenge when an answer conflicts with the research — say so.
- Resolve every branch. If an answer changes an earlier decision, revisit it before finalizing.
- Don't assume silence is approval.

If the work-log server isn't reachable at http://localhost:8089 (start it with `bash ~/project/repos/your-repo-ops/tools/serve-worklog.sh up`), switch to `/do-plan-and-grill --interactive` — planish is HTML-first by design.

### Step 3: Finalize Plan (delegate — dual plan.md + plan.html)

**Dispatch a plan-writer subagent** with all the grill decisions to produce the final plan as **two outputs** in `.../plan/v<N>/` — `plan.md` (canonical, what `/do-implement` consumes) and `plan.html` (the visual, annotatable surface you read and mark up). Same content; markdown is the agent/tooling surface, HTML is the human surface.

`plan.md` requirements:
- Frontmatter at the top: `status: Accepted`, `tldr:` (one sentence), `decision:` (one sentence), `links:` pointing at `./research.md` if one was written.
- An `## Executive Summary` immediately after the frontmatter — bullet-point format; any point longer than two lines must be rewritten as bullets. This stays — it's what the user reads at a glance.
- Per-phase detail blocks below the summary.
- **Dropped vs plan-and-grill** (this is the light path): NO `--route-phases` `**Size**:` tags, NO required `## Team` section, NO mandatory `decisions.md` entry. Include a `## Team` section only if the grill settled a specific roster; otherwise omit it and let `/do-implement` infer a worker from the change scope (as `/do-oneshot` does).

<!-- # ref 2 (plan-html-style) — duplicated in: do-plan-and-grill/command.md, do-oneshot/command.md, this_repo/claude/do-planish/command.md -->
`plan.html` requirements (give these to the plan-writer verbatim):
- Render the executive summary + phases in the project dark style — the `<style>` block from `~/project/repos/your-repo-ops/mkdocs/docs/diagrams/architecture/v3.html` if it exists, otherwise the default style block from the `/design-doc` skill. Use the flow/box visual vocabulary for phase sequencing where it helps.
- Paste `.shared-llm/llm/claude/common/toolkits/annotation-toolkit.html` verbatim immediately before `</body>`.
- `<title>` = `<Title> — plan v<N>`.

Only `plan.md` is the source of truth for execution — `plan.html` is never parsed by `/do-implement`. If the user annotates `plan.html` and pastes feedback later, treat it as a new grill round → bump to `v<N+1>` and re-finalize both outputs.

### Step 4: Persist and Stop

1. **Dispatch a nav-sync subagent** to update `~/project/repos/your-repo-ops/mkdocs/mkdocs.yml` under `- Logs:`.
2. If `--team`: SendMessage `STOP_PULSE` to team-pulse if it was running.
3. Tell the user:
   ```
   Plan finalized.
   Read:     http://localhost:8089/work-log/<YYYY-MM-DD>/<slug>/plan/v<N>/plan.html
   Markdown: <path>/plan.md

   Annotate in the browser, click "Copy Feedback", paste it back here to get v<N+1>.
   Run `/do-implement <path>/plan.md` when ready.
   ```
4. **STOP.** Do not implement. The user runs the next step deliberately.

### Nav Structure

```yaml
- Logs:
    - <YYYY-MM-DD>:
        - <title>:
            - Plan:
                - v<N>: work-log/<YYYY-MM-DD>/<title>/plan/v<N>/plan.html
```
