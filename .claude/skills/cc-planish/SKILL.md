---
name: cc-planish
description: 'Standalone lightweight planner: grill the user on an annotatable HTML page, then build and iterate a versioned plan.md + plan.html until approved — no research subagents, no Team, no phase routing. The Claude Code port of the Pi /do-planish extension: the same .planish.yaml contract (walk-up discovery, dir:/host: fields, {date}/{slug}/{type}/{n} tokens, PLANISH_DIR/PLANISH_HOST env overrides), the same annotation-only feedback (sticky notes → Copy Feedback → paste the block back; a question with no note accepts the recommendation), and the same frozen plan-v<k> versioning (plans never mutate in place). Use when you want a quick standalone plan → grill → finalize without the full /cc-plan-and-grill research ceremony; reach for /cc-plan-and-grill instead when the work needs research, a roster, or phased execution. Invocation: /cc-planish <topic>, /cc-planish --review <path>, optional --dir <path>.'
argument-hint: <topic> | --review <path> [--dir <path>]
---

# cc-planish — Standalone plan → grill → finalize (Claude Code)

The lightweight standalone planner: grill the user on an annotatable HTML page, build a versioned `plan.md` + `plan.html`, iterate until approved. This is the Claude Code port of the Pi `/do-planish` extension — the same flow, the same `.planish.yaml` contract, the same annotation-only feedback — expressed as a file-based skill instead of an in-memory HTTP server.

It is deliberately smaller than `/cc-plan-and-grill`: **no research subagents, no `## Team` section, no phase routing, no size tags.** Just grill → build → review. If you find you need research, a roster, or phased execution, you have outgrown `/cc-planish` — use `/cc-plan-and-grill` instead.

This command follows the canonical **Planish HTML Grill Contract** at `.shared-llm/llm/common/common/planish-html-grill-contract.md`. The default grill surface is a visual, annotatable HTML page — never a plain chat list of questions. Read that contract; the rules below defer to it.

## When to use this vs the other planners

- **`/do-planish`** (Pi) — the same standalone flow, served from Pi's in-memory HTTP server.
- **`/cc-planish`** (here) — the same standalone flow on Claude Code, file-based. ← **you are here**
- **`/cc-plan-and-grill`** — research + a full plan + iterative grill + a roster. Reach for it when the work needs exploration or phased execution.

## Invocation

```
/cc-planish <topic>
/cc-planish --review <path>
/cc-planish --dir <path> <topic>
```

- **`<topic>`** — What you want to plan. Slugified for the plan directory.
- **`--review <path>`** — Skip the grill/build; re-open an existing `plan.html` (or `plan.md`) for another review round.
- **`--dir <path>`** — Optional. Override where `plan.md` + `plan.html` are written for this run (highest precedence — see below).

## Shared config — the SAME `.planish.yaml` as `/do-planish` (hard requirement)

`/cc-planish` and `/do-planish` **must** read the identical config. There is NO cc-specific config file — the filename stays `.planish.yaml` and the env names stay `PLANISH_DIR` / `PLANISH_HOST`. One config drives both variants. Resolve exactly as the Pi extension does (`do-planish.ts` `resolvePlanDir` / `resolveHost`):

**Plan directory** — first match wins:

1. **`--dir <path>`** flag, if given. A relative path resolves against the current working directory.
2. **`$PLANISH_DIR`**, if set and non-empty. A relative path resolves against the current working directory.
3. **The nearest `.planish.yaml`'s `dir:` template.** Walk UP from the current working directory to the first ancestor holding a `.planish.yaml`. If that file has a `dir:` key:
   - A **relative** `dir:` template resolves against the **directory that holds the `.planish.yaml`** (not cwd).
   - If `dir:` is present but is **not a non-empty string**, that is a config typo — **fail loud and stop**. Never silently fall back to the default.
4. **Default** `/tmp/planish/{date}/{slug}` — only when there is no `.planish.yaml`, or it sets `host:` but no `dir:`.

**Token expansion** in the chosen template:

- `{date}` → today's date, `YYYY-MM-DD`.
- `{slug}` → the topic, lowercased with non-alphanumeric runs collapsed to `-` (empty → `plan`).
- `{type}` → the literal `plan`.
- `{n}` → the next version integer for that path segment: scan the parent directory for existing siblings matching the segment's prefix/suffix and use `max + 1` (1 if none).

Then create the directory (`mkdir -p`). This is the plan dir for the whole session — grill pages and plan files all land here.

**URL host** — first match wins: `$PLANISH_HOST` → the nearest `.planish.yaml`'s `host:` → `localhost`. Use it for every URL you hand the user (a `host:` names the machine a remote browser reaches, e.g. a Tailscale name); never hardcode `localhost` when `host:` is set.

**Serving.** The skill stays generic: any static file server rooted at (or above) the plan dir works — e.g. `python3 -m http.server 8089` run from the plan dir — and so does a plain `file://` path, because every page is fully self-contained (inline styles/scripts, no CDN). Build the URL from the resolved host. Whenever this session has a file-send tool (e.g. `SendUserFile` in the Claude Code app), ALSO send the HTML file itself — remote/app sessions often cannot reach the URL, and a downloaded copy always opens.

`.planish.yaml` example (identical for both variants):

```yaml
# where plans land; resolved relative to this file's directory
dir: /var/tmp/mkdocs/plans/{date}/{slug}
# optional: the machine name a remote browser reaches (else localhost)
host: dev-box
```

## Workflow

You run this flow yourself — it is small enough that you write the grill and plan files directly. Do NOT build or run the planned change; produce a plan, and stop when it is approved.

### STEP 1 — GRILL

Resolve the plan dir (above). Then interview the user on an annotatable HTML page, one round at a time, until every decision is resolved.

Each round, write a fresh `grill-v<round>.html` (kept forever as history) and overwrite `grill_current.html` (the tab the user keeps open) with the same content, both in the plan dir:

- **What the round shows — unanswered-first.** Follow the contract's **"Unanswered-first rounds — carry only open questions forward"** section verbatim; do not restate the rule here. In short: round 1 carries the full context header and every question; round > 1 shows ONLY the still-open questions (open-longest-standing first), collapses resolved ones to a single `<N> resolved — recommendations accepted` line, and opens with a slim header (round number, one-line what-changed, open/resolved counts).
- **Context header (round 1)** — plain English: what the plan is trying to do and what you found. Define every acronym at first use. File paths / method names / change lists go ONLY in an `Appendix` at the bottom — never at the top, never inside a question.
- **Question blocks** — one `<div class="grill-q">` per question, each with a `.grill-q-text` (the question), an optional `.grill-q-note` (why it matters), and a `.grill-q-rec` (your concrete recommendation). **No answer boxes** — the user answers by dropping a sticky note on the block. Keep each question tight; when a choice is complex, SHOW it with a diagram — two modes only, NEVER Mermaid: default is an ASCII tree in a `<pre>`; when ASCII can't carry it, the row-by-row HTML flow (`.grill-fig` / `.flow` / `.flow-box`). A diagram only when it genuinely helps.
- **`<meta name="desdoc-key" content="<date>-<slug>-r<round>">`** in `<head>` — a unique value per round so the annotation toolkit starts each round with a clean slate (`grill_current.html` reuses one path across rounds).
- **Toolkits** — question/diagram styles from `.shared-llm/llm/common/common/toolkits/form-toolkit.html` (style-only), and the sticky-note annotation controls from `.shared-llm/llm/common/common/toolkits/annotation-toolkit.html` pasted verbatim immediately before `</body>`. The annotation bar is the page's ONLY interactive control. `<title>` = `<Topic> — grill v<round>`.

Then tell the user, and end your turn: _"Round `<k>` is up — `http://<host>:8089/.../grill_current.html` — drop a note on anything to answer or change (+ Note), click Copy Feedback, paste the block back here. No note on a question = the recommendation stands."_ Also send `grill_current.html` as a downloadable file when a file-send tool exists. Nothing blocks — feedback arrives as the user's next pasted message.

When the user pastes their `## Feedback —` block: process every note, treat un-noted questions as accepted recommendations, and compute the next round's still-open questions (the dependent ones now have concrete wording, plus anything the feedback newly surfaced). Repeat. **A round with no open questions ends the grill** — move to BUILD.

### STEP 2 — BUILD

Write the plan to TWO files in the plan dir:

- **`plan.md`** — the canonical, token-lean plan: title, the phases/steps, key decisions, and verification. This is the file downstream tooling reads.
- **`plan.html`** — the same plan in the dark visual style (the `<style>` block from `~/project/repos/your-repo-ops/mkdocs/docs/diagrams/architecture/v3.html` if it exists, else the `/design-doc` default style), with `.shared-llm/llm/common/common/toolkits/annotation-toolkit.html` pasted verbatim before `</body>` and a unique `<meta name="desdoc-key" content="<slug>-plan-v<k>">` in `<head>` so each plan version starts with a clean note slate. `<title>` = `<Topic> — plan v<k>`. The annotation bar is the page's ONLY interactive control — no answer boxes, no submit buttons.

**HARD RULE — freeze `plan-v<k>` before every write (this is the same discipline `/do-planish` enforces in its tool):**

- On the FIRST build, freeze the content as `plan-v1.md` + `plan-v1.html`, then write `plan.md` + `plan.html`.
- On EVERY later revision, FIRST freeze the next `plan-v<k>.md` + `plan-v<k>.html` pair (v2, v3, …), THEN overwrite `plan.md` + `plan.html` with the new content.
- `plan.md` / `plan.html` are the latest convenience copy and MAY be clobbered. A frozen `plan-v<k>` file is history — **never edit or overwrite it once written.**
- **Before ending a build turn, verify on disk that the frozen `plan-v<k>` pair for the current revision exists.** A plan that mutates in place with no matching new `plan-v<k>` snapshot is a bug.

### STEP 3 — REVIEW

Serve `plan.html` and hand the user the URL (host resolved as above); also send the file when a file-send tool exists. Tell them the page is ready and END YOUR TURN — nothing blocks. The user annotates and pastes back:

- A **`## FINALIZED`** block (or an explicit approval message) means the plan is APPROVED — the deliverable is done.
- Notes requesting changes mean: revise BOTH files, freeze the revision as the next `plan-v<k>` pair FIRST (STEP 2's hard rule), then serve `plan.html` again. Loop until approved.

## `--review <path>` mode

`/cc-planish --review <path>` skips STEP 1/2: ensure the file at `<path>` carries the annotation controls (append the canonical `annotation-toolkit.html` before `</body>` if it does not already have them), serve it, hand the user the URL (and the file where a file-send tool exists), and run STEP 3's review loop. Any change still freezes the next `plan-v<k>` pair before overwriting `plan.md` / `plan.html`.
