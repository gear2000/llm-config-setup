---
name: do-plan-and-grill
description: 'Research a problem, draft a plan, then grill the user iteratively until the plan is rock solid. Flow: research (Explore subagents) → synthesizer → plan-writer → grill the user → finalize plan → STOP. Default transport is subagents (fresh context per call); pass --team to run inside TeamCreate. The leader orchestrates and runs the grill conversation; the leader does not read code or write files itself — it always dispatches a subagent for that work. The grill phase is the core value — iterate until every decision is resolved. Pass --route-phases to add mandatory per-phase size tagging (required for /do:loop --afk execution). Invocation: /do-plan-and-grill [--team] [--route-phases] <title>.'
---

# do-plan-and-grill — Research → Draft → Grill → Iterate → Finalize

Like `/do-plan` but with an iterative grilling phase after the initial draft. The plan gets refined through back-and-forth with the user until every decision is resolved.

## Cardinal rule: delegate, don't do

Your job is to **orchestrate** and to **run the grill conversation**.

- Reading code, writing research.md, drafting plan.md, applying plan revisions: dispatch fresh subagents.
- The grill itself (asking the user one question at a time, processing answers): you do this — that's the orchestration loop, not "work."

If you find yourself reading files, writing markdown, or running commands directly, **stop, dispatch a fresh subagent for the work, return to orchestrating.**

## Invocation

```
/do-plan-and-grill [--team] [--route-phases] [--interactive] [--docs] <title>
```

- **`<title>`** — Required. Slugified for directory name.
- **`--team`** — Optional. Run inside TeamCreate during the research phase (TMUX windows, named members). Default is subagents.
- **`--route-phases`** — Optional. Enables mandatory per-phase size tagging. See [## --route-phases flag](#--route-phases-flag) below.
- **`--interactive`** — Optional. Grill one question at a time in the terminal (the classic flow). **Default is HTML-batch** — see [## Grill modes](#grill-modes). Use `--interactive` when you can't open the HTML grill in a browser (headless / no display), or when you simply prefer the terminal.
- **`--docs`** — Optional. Capture domain terms + hard decisions to `auto-docs/` as the grill resolves them, **without** asking first. Without this flag, Step 1 asks you once whether to capture. See [## Domain capture](#domain-capture).

## --route-phases flag

Use this flag when the plan will be executed AFK via `/do-loop --afk`. Without size tags, `/do-loop --afk` refuses to start.

**Content contract:**

- **WITH `--route-phases`**: every phase MUST have a `**Size**: small|big` line directly under its phase header. A plan missing any size tag is incomplete — re-grill until all are set.
- **WITHOUT `--route-phases`**: phases must NOT have `**Size**:` lines. The plain grill produces a clean plan that `/do-loop --interactive` and `/do-implement` consume.

**Downstream effect:** `/do-loop --afk` refuses plans without size tags. `/do-loop --interactive` accepts both.

**What changes with `--route-phases`:**

1. **Draft step** — the plan-writer MUST insert `**Size**: small|big` under each phase header. Initial sizes are the writer's best guess (see sizing heuristic below).
2. **Grill step** — termination requires every phase to have all five checks resolved AND every `**Size**:` confirmed. "Maybe big" is not a valid answer — push for a decision.
3. **Final plan** — every phase in the output `plan.md` starts with a `**Size**:` line.
4. **Finalization step** — the plan-writer receives an explicit instruction: every phase MUST start with `**Size**: small` or `**Size**: big` directly under its header. A return that omits any size tag is rejected — re-dispatch.
5. **Last-phase live-deploy gate** — before finalizing, scan the last phase's verification list. At least one entry must match a live-deploy pattern (`sync-to-git.sh`, `curl https://`, `aws lambda invoke`, `aws logs tail`, `terraform apply`, Jenkins triggers, playwright against live URL). If none found, tell the user and re-enter the grill until the gate passes.
6. **decisions.md entry** — uses route-phases format (see Step 6 below).
7. **Stop message** — tells the user phase counts and suggests `/rphase-create <path>` as the next step.

**Sizing heuristic for the plan-writer (--route-phases only):**

```
small if ALL of:
  - ≤4 tasks
  - ≤5 files in files_touched
  - no live-deploy verification commands (sync-to-git.sh, curl https://,
    aws lambda invoke, terraform apply, Jenkins triggers, playwright
    against live URL)
  - single role (only backend OR only frontend OR only devops OR only
    database — not a mix)

big if ANY of:
  - >4 tasks OR >5 files
  - any live-deploy verification command
  - cross-package or cross-role coordination (e.g., backend + frontend)
  - terraform apply, schema migration, IAM/security change, auth flow change

If unsure, default to big — over-provisioning a small phase costs less
than under-provisioning a big one.
```

**Plan.md output format (applies to both `--route-phases` and plain plans):**

Every plan.md MUST begin with an `## Executive Summary` section immediately after the frontmatter. This is what the user reads to understand the plan at a glance before diving into phase details.

**Executive Summary rules:**
- Written for a human scanning on a small screen — bullets over prose.
- Any sentence longer than two lines MUST be rewritten as a bullet list.
- No phase-level details here — those live in the phase blocks below.
- The user should be able to understand the full plan shape from the summary alone.

**Plan.md output format with `--route-phases`:**

```markdown
---
status: Accepted
tldr: One sentence.
decision: One sentence.
execution: sequential   # or: parallel
links:
  - research: ./research.md
  - decisions: ../../decisions.md
---

## Executive Summary

**Goal**: [What this plan accomplishes and why — 1-2 lines max. If longer, make it bullets.]

**Phases at a glance**:
- **Phase A — Title**: [One-liner: what it delivers]
- **Phase B — Title**: [One-liner]
- ...

**How it fits together**: [1-2 bullets — key sequencing rationale, dependencies, or why this order]

**Key risks**: [2-3 bullets max — only genuinely risky items. Omit if none.]

---

<!-- Phase details follow — executor reads from here. -->

### Phase A — Title

**Size**: small

**Goal**: ...

**Files touched**:
- ...

**Tasks**:
1. ...

**Verification**:
- review: required, reviewer: auto | adversarial | both
- deploy: required: true | false
- live:   required: true | false
- (mechanically-checkable commands listed below if any)

**Rollback**: ...

---
```

## Workflow

### Step 1: Setup

1. Call `EnterPlanMode`.
2. Slugify title, determine version by globbing `~/project/repos/your-repo-ops/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/plan/v*/`.
   <!-- <YYYY-MM-DD> is the ISO date the work was started (NOT modified). Use the date at the time of the first invocation of this skill in this work-log. -->
3. **Domain-capture decision (ask first, unless `--docs` or `--interactive`-AFK).**
   - If `--docs` was passed → capture is ON, do not ask.
   - Otherwise, ask exactly one yes/no question before anything else: _"Capture domain terms + hard decisions to `auto-docs/` as we grill? (y/n)"_ — then remember the answer for the whole run.
   - In a non-interactive / AFK run there is no one to answer: capture is ON only if `--docs`, otherwise OFF. Never block on the prompt.
   - See [## Domain capture](#domain-capture) for what gets written.
4. Brief scope confirmation with the user (1–2 questions max — save the real questions for the grill).

### Step 2: Research (delegate)

Dispatch Explore subagents (or named team members with `--team`) covering the relevant aspects, each writing to a section file. Then dispatch a synthesizer subagent to consolidate into `~/project/repos/your-repo-ops/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/plan/v<N>/research.md`. Same pattern as `/do-research` — see that skill for details.

### Step 3: Draft Plan (delegate)

**Dispatch a plan-writer subagent** with research.md as input and the user's stated goal. Same prompt template as `/do-plan` (see that skill's Step 3 for the full prompt + required frontmatter + required sections, including the `## Team` section spec).

The plan-writer MUST emit:
1. The required frontmatter at the top of `plan.md` (`status`, `tldr`, `decision`, `execution: sequential|parallel`, `links`).
2. Immediately after the frontmatter: the `## Executive Summary` section (see format block below). This is non-negotiable — a plan.md without an executive summary is incomplete.
3. Then the per-phase detail blocks.

The `execution:` field defaults to `sequential` in the draft and is confirmed in the grill.

**If `--route-phases`**: extend the plan-writer's prompt with:

```
Each phase section MUST begin with a `**Size**:` line directly under the
phase header. Acceptable values: `small` or `big`. Use the sizing heuristic
in the --route-phases flag section as your first pass — the user will confirm
or override during the grill. Initial sizes are a best guess; the grill
will challenge them.
```

The plan-writer's output is a *draft* — the grill will refine it.

Without `--route-phases`: Present the draft summary to the user with: _"Here's the draft based on the research. Now I'll walk through every decision point with you to make sure this is right."_

With `--route-phases`: Present the draft summary with: _"Here's the draft based on the research. Now I'll walk every phase with you to lock the size tag and the verification."_

### Step 4: Grill (the core step)

> **This is the heart of this skill. Do NOT rush. Do NOT skip. Every decision branch must be resolved before you finalize.**

The *what* of the grill is identical in both modes — same topics, same rigor, same "don't stop early." Only the *how* differs: how questions reach the user and how answers come back. **Default is HTML-batch; `--interactive` is one-at-a-time in the terminal.**

You run the grill directly — it's orchestration, not "work." When an answer requires a plan change, dispatch the plan-writer subagent with the revision (always, even for tiny changes), then chase the downstream implications.

#### Grill modes

**Default — HTML-batch (fast).** The terminal one-at-a-time loop is slow because each question is its own LLM round-trip; ten simple questions can burn 40 minutes of waiting. Batching collapses that: ask everything independent in a single round.

Ensure the work-log static server is up (idempotent — no-op if already running): `bash ~/project/repos/your-repo-ops/tools/serve-worklog.sh up`

Each round:
1. Work out every question you can ask **right now** — split them into independent (answerable in any order) and dependent (wording hinges on an independent answer). Ask the independent ones this round.
2. **Dispatch a subagent** to write `grill_current.html` into the plan work-log dir (`.../plan/v<N>/grill_current.html`) — write a fresh `grill-v<round>.html` each round (kept for history) and overwrite `grill_current.html` with the same content, then refresh the tab (kept on `grill_current.html`) to load the new round (the static server serves the raw file — no live-reload). The file contains:
   - A context header: what's being planned, the draft shape, and enough background that each question is understandable on its own (not a wall of text — use headings/tables).
   - Keep every question tight — bullets, never a sentence over two lines, plain English. When a question is complex, SHOW it with a diagram chosen by complexity: simple → a Mermaid block (add its `<script>` to the page `<head>`); a bit more complex → an ASCII tree in a `<pre>`; quite complex → the row-by-row HTML flow (`.grill-fig` / `.flow` / `.flow-box`, styled by the form toolkit). A diagram only when it genuinely helps — never for its own sake.
   - One `<div class="grill-q">` block per question, each with `.grill-q-text` (the question), an optional `.grill-q-note` (why it matters) and `.grill-q-rec` (your recommendation), and a `<textarea class="grill-a">`.
   - The form toolkit pasted verbatim before `</body>` from `.shared-llm/llm/claude/common/toolkits/form-toolkit.html`.
   - `<title>` = `<Title> — grill v<N>`.
3. Tell the user the round is ready: _"Round <k> is up — http://localhost:8089/work-log/<YYYY-MM-DD>/<slug>/plan/v<N>/grill_current.html — fill it in, click Copy Answers, paste back here."_
4. The user pastes the `## Answers —` block. Process every answer: revise the plan (dispatch plan-writer), and from what you learned, compute the next round's questions (the dependent ones now have concrete wording, plus anything the answers newly surfaced).
5. Repeat. The LLM decides each round's batch size — converge as questions run out. A round with no open questions ends the grill.

**`--interactive` — one at a time (portable).** No web server needed. Present one decision, give your recommendation, ask, wait, process, move on. Never dump a list. This is the classic flow; use it anywhere you can't open the HTML grill in a browser.

**Both modes — the rules that don't change:**
- Resolve every branch. If an answer changes other decisions, revisit those before finalizing.
- Always recommend. Every question carries your best answer + reasoning, never a bare ask.
- Challenge when the user's answer conflicts with the research — say so.
- Revise on every change. Even a tiny plan tweak goes through the plan-writer subagent.
- Don't assume silence is approval.

**Topics to always cover:**
- Scope boundaries (what's in, what's out)
- Architectural approach (why this way, not that way)
- Phase ordering and dependencies
- What "done" looks like for each phase
- Risk tolerance (what if X fails?)
- Testing strategy — what tests prove success? What does a passing test actually verify? How do we confirm the test exercised the right code path (read logs, not just exit codes)?
- Deployment strategy — how code reaches production
- Post-deployment verification — how do we confirm it's running correctly (logs, not just status codes)
- Error handling strategy — where exceptions propagate vs are caught
- **Team roster** — does this need 1 or 2 workers? Is the deploy nontrivial enough to dispatch a `deployer`? Is the work risky enough to want a `plan-watchdog`? Does any phase need a specialist (`database`, `security`, `devops`)? Resolve this in the grill — it goes in the plan's `## Team` section that drives `/do-implement`.
- **Execution mode** — `sequential` (each phase consumes prior phase's handoff section; default) or `parallel` (phases independent, no handoff injection). Mixed plans set `execution: parallel` and tag individual phases with `depends_on: [phase-id, ...]`. Defaults to `sequential` if not raised — confirm explicitly when the work is parallel-eligible (e.g., independent package edits, parallel team-of-workers).
- **Review style per risky phase** — for phases touching auth/security/infra/migrations, ask whether the review gate should be `auto` (default automated review), `adversarial` (a fresh adversarial reviewer — Codex via `codex:rescue` or fresh Claude agent — hunts bugs, security issues, scope creep), or `both`. Capture per-phase in plan.md so `rphase-create` emits the right `reviewer` field.
- **Deploy/live gate applicability** — for each phase, is `deploy` required? Is `live` required? Greenfield phases that produce libraries (no service to deploy to yet) keep these optional. Capture decisions so `rphase-create` emits correct `required:` flags on the verification array.

**With `--route-phases` — additional per-phase checks (walk phases in order):**

For each phase, resolve all five before moving on:

1. **Goal** — does the goal match the user's mental model?
2. **Files touched** — exhaustive? Any file the phase will modify must be listed.
3. **Tasks** — concrete enough that a worker can execute without back-and-forth.
4. **Verification** — every entry must be a mechanically-checkable command. Lock `review.reviewer` (`auto`|`adversarial`|`both`), `deploy.required`, `live.required`.
5. **Size tag** — confirm or change. Ask: _"Phase NN is currently tagged `<size>`. That means it'll run as <single agent | TeamCreate-of-one with TMUX visibility>. Reasoning: <rationale>. Keep `<size>` or change?"_ Don't allow "maybe big" — push for a decision.

After walking all phases, resolve plan-level execution mode: sequential (default) or parallel. If parallel, tag phases with `depends_on`.

Common size-upgrade triggers (`small` → `big`): "I want to watch in TMUX", "live deploy at the end", "touches auth/security/IAM", "cross-package."
Common size-downgrade triggers (`big` → `small`): "just a config tweak", "single file edit, no deploy."

**When to stop grilling:** when you can finalize the plan with zero ambiguity remaining. If `--route-phases`, ALSO require that every phase has all five checks resolved AND `**Size**: small|big` confirmed before terminating. Do not assume silence equals approval.

### Step 5: Finalize Plan (delegate)

**Dispatch the plan-writer subagent one more time** with all the grill decisions, asking it to produce the final plan as **two outputs** — `plan.md` (canonical, what `/do-implement` and `/rphase-create` consume) and `plan.html` (the visual, annotatable surface you read and can still mark up). Same content; markdown is the agent/tooling surface, HTML is the human surface. Make sure:

- Frontmatter at top reflects grill outcomes (`status: Accepted`, `decision:` sentence, `execution: sequential|parallel`).
- `## Executive Summary` section immediately after the frontmatter — updated to reflect any grill decisions that changed the plan. Bullet-point format: any point longer than two lines must be rewritten as bullets. A final plan.md missing this section is rejected — re-dispatch.
- `## Team` section matches the agreed roster.
- Per-phase `reviewer:` annotation present where adversarial review was decided.
- Per-phase `deploy` / `live` required-flag intent present so `rphase-create` can emit correct gates.
- **If `--route-phases`**: instruct the writer explicitly: "Every phase MUST start with `**Size**: small` or `**Size**: big` directly under its header. A return that omits any size tag is rejected — re-dispatch." A plan missing any size tag is incomplete.
<!-- # dup 2 (plan-html-style) — canonical in common/common/do-planish/command.md -->
- **`plan.html`**: render the executive summary + phases in the project dark style (the v3.html `<style>` block if present, else the `/design-doc` default), use the flow/box visual vocabulary for phase sequencing where it helps, and paste `.shared-llm/llm/claude/common/toolkits/annotation-toolkit.html` verbatim before `</body>`. `<title>` = `<Title> — plan v<N>`. Downstream tooling reads `plan.md` only — `plan.html` is never parsed by `rphase-create` or `do-implement`.

Only `plan.md` is the source of truth for execution. If the user later annotates `plan.html` and pastes feedback, treat it as a new grill round → bump to `v<N+1>` and re-finalize both outputs.

### Step 6: Append decisions.md entry

**Append a decision entry** to `~/project/repos/your-repo-ops/mkdocs/docs/work-log/<YYYY-MM-DD>/<slug>/decisions.md` (create if missing).

Without `--route-phases`:

```markdown
## <ISO-8601 timestamp> [SCOPE] Plan v<N> finalized (grilled)
- Approach: <one-line summary of frontmatter `decision:`>
- Execution: <sequential | parallel>
- Team: <agents in Team section>
- Key grill outcomes: <one-line summary of the 2–3 most important grill decisions, especially anything that changed from the draft>

Refs: plan/v<N>/plan.md, plan/v<N>/research.md
```

With `--route-phases`:

```markdown
## <ISO-8601 timestamp> [SCOPE] Plan v<N> finalized (route-phases grill)
- Approach: <one-line summary of frontmatter `decision:`>
- Execution: <sequential | parallel>
- Size tags: <count_small> small, <count_big> big
- Adversarial reviews on: <comma-list of phase IDs, or "none">
- Phases with live-deploy gate: <comma-list, or "final phase only">

Refs: plan/v<N>/plan.md, plan/v<N>/research.md
```

Append; do NOT overwrite. See `mkdocs/docs/work-log/README.md` for the full format.

### Step 7: Persist and Stop

1. **Dispatch a nav-sync subagent** to update `~/project/repos/your-repo-ops/mkdocs/mkdocs.yml` under `- Logs:`.
2. If `--team`: SendMessage `STOP_PULSE` to team-pulse if it was running.
3. Call `ExitPlanMode`.
4. Without `--route-phases`: Tell the user: _"Plan finalized at `<path>`. Run `/do-implement <path>/plan.md` when ready."_
   With `--route-phases`: Tell the user:
   ```
   Plan finalized at <path>.
   N phases tagged: <count_small> small, <count_big> big.
   Execution: <sequential | parallel>.
   Run `/rphase-create <path>` next to convert to phase JSON files.
   Then `/do-loop --afk <phases_root>` to execute AFK.
   ```
5. **STOP.** Do not call `/rphase-create`. Do not implement. The user runs the next step deliberately.

### Nav Structure

```yaml
- Logs:
    - <YYYY-MM-DD>:
        - <title>:
            - Plan:
                - v<N>:
                    - Research: work-log/<YYYY-MM-DD>/<title>/plan/v<N>/research.md
                    - Plan: work-log/<YYYY-MM-DD>/<title>/plan/v<N>/plan.md
```

## Domain capture

When capture is ON (the user answered yes in Step 1, or passed `--docs`), the grill does double duty: as terms get pinned and hard decisions get locked, write them down. This builds a glossary and a decision log as a **side effect** of grilling — no separate session.

**Where it lives:** `auto-docs/` at the **code repo root** (not the ops repo). This is a deliberate exception to the repo's "source code only" rule — the glossary is about the *language and domain of this code*, so it sits next to the code. Tracked (committed), so it persists and you can sanitize it later.

```
auto-docs/
├── CONTEXT.md      — living glossary: one entry per domain term + its definition
└── decisions/      — one markdown file per hard decision (ADR-style)
```

**What gets written, and when:**

- **A term** → `CONTEXT.md`, the moment a term is resolved during the grill. Format: the term, its precise definition, and what it is *not* (the confusion it resolves). Dispatch a subagent to append — do not write it yourself.
- **A decision** → a new file in `decisions/` **only when all three hold**: hard to reverse, surprising without context, and the result of a real trade-off. Most grill answers are none of these — stay quiet. When it qualifies: title, the decision, the alternatives considered, why this one.

**Hard rules:**
- **Never auto-injected.** Nothing reads `auto-docs/` unless the user explicitly references it (`@auto-docs/CONTEXT.md`). Do not add it to `CLAUDE.md` or any session hook. It is pull-only, never push — that is what keeps a stale entry from silently misleading a future session.
- **Propose, then write.** During the grill, when you record a term or decision, state it in one line so the user sees what landed. No silent writes.
- **Create lazily.** Only create `auto-docs/`, `CONTEXT.md`, or `decisions/` when there is a first real entry to write — never empty scaffolding.
- **Append, don't rewrite.** Add to the glossary; never silently overwrite an existing definition. If a term's meaning changes, show the old and new and let the user confirm (same discipline as the rest of the grill).
