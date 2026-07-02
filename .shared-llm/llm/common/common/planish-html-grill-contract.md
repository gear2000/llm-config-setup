# Planish HTML Grill Contract

This is the shared contract for customized planning flows. It applies to the Pi `/planish` extension and to the suite planners (`/do-plan-and-grill`, `/cc-plan-and-grill`). Standalone skill variants `/do-planish` and `/cc-planish` are intentionally removed to avoid overlapping meanings.

## Non-negotiable

Customized planning must not default to a plain chat list of questions. The default grill surface is a browser HTML page that explains the decision visually and collects feedback through sticky-note annotation.

Terminal questions are allowed only for an explicit fallback/interactive mode where no browser/static server is available.

## One feedback transport — annotate, copy, paste back

There is exactly ONE way feedback travels back, in every environment (Pi, Claude Code, anywhere else):

1. The page carries the sticky-note annotation controls (`+ Note`, `Notes`, `Copy Feedback`, `Finalize ✓`) — and nothing else interactive.
2. The user drops a note on anything they want to answer, change, or challenge, and types into the note. Then adds the next note. And so on.
3. The user clicks **Copy Feedback** and pastes the `## Feedback` block into the chat/TUI themselves.

Hard bans, everywhere:

- **No answer boxes.** No fill-in fields on the page outside the sticky notes themselves. Copy Feedback carries only the notes, so anything typed into a page-level box is silently lost — that is why boxes are banned.
- **No direct browser→assistant feedback.** No Submit / Approve / Request-Changes buttons, no POST back to the assistant, and no tool call that blocks waiting on the browser. A planish tool serves its page and returns immediately; the assistant gives the user the URL and ENDS ITS TURN. Feedback arrives as the user's next pasted message.

The convention that makes this fast: **a question with no note means "go with the recommendation."** Every question block carries a concrete recommendation, so the user only annotates what they want to change or answer differently. **Finalize ✓** copies a `## FINALIZED` block (with any final notes); pasting it back signals approval.

## Deliver the page two ways — URL and downloadable file

1. **URL** — always. The host is NOT hardcoded `localhost`: use the `host:` field of the nearest `.planish.yaml` when set (the machine name the user's browser reaches — e.g. a Tailscale name for remote sessions), else `localhost`. The Pi planish server reads the same field and binds `0.0.0.0` for a non-localhost host so remote connections actually work.
2. **Downloadable file** — whenever the harness provides a file-send tool (e.g. `SendUserFile` in the Claude Code app), ALSO send the HTML file itself. Remote and app sessions often cannot reach the URL at all; a downloaded copy always opens.

This only works because every page is **fully self-contained**: inline styles and scripts, no CDN, no external fetches — a grill or plan page must render and annotate correctly opened straight from a download (`file://`).

## Versioned history — plans never update in place

The evolution of the planning session must stay visible. Two parallel histories, same pattern:

- **Grill rounds** — each round writes a frozen `grill-v<round>.html` (kept forever) and overwrites `grill_current.html` (the tab the user keeps open).
- **Plan revisions** — every time the plan is written or rewritten (draft, each grill-driven revision, finalization), freeze the same content as `plan-v<k>.md` (+ `plan-v<k>.html` whenever an HTML twin is produced): `v1` for the first draft, incrementing every revision. `plan.md` / `plan.html` are overwritten with the latest and remain the ONLY files downstream tooling reads.

Never edit an existing `grill-v*` or `plan-v*` file — they are frozen history. A plan that mutates in place with no new `plan-v<k>` snapshot is a bug.

## Write for the user

The grill is an interview about the design, not a changelog. Hard rules:

1. **Open in plain English.** The context header explains what the plan is trying to do and what was found so far, in complete sentences a teammate could follow without reading the code.
2. **Questions are about the mechanism or design choice** — which approach, what trade-off, what behavior. Never "these files changed" or a walk through methods; that is noise, not a question.
3. **Technical terms are fine; acronyms must be defined at first use.** Never assume the reader knows an acronym.
4. **File paths, method names, and change lists go in an `Appendix` section at the BOTTOM of the page** — never at the top, never inside a question block.

## Fresh annotations every round

Rounds often reuse one URL/path (`grill_current.html`, a localhost server), and note storage is keyed per page — so every generated round page must carry a unique key: `<meta name="desdoc-key" content="<date>-<slug>-r<round>">` in `<head>` (the annotation toolkit uses it and clears the previous round's notes). Server-rendered grills (Pi `planish_grill`) do this automatically. Notes are the ONLY answer channel, so a round that shows the previous round's sticky notes is a bug.

## Required page sections

Every grill round must include:

1. **Context header** — what is being planned, current draft/plan shape, and decisions already locked — ending with the one-line how-to: _"Answer by annotation: + Note on a question → type → Copy Feedback → paste it back into the chat. No note on a question = the recommendation stands."_
2. **Visual framing** — one or more diagrams that make the user's choice understandable.
3. **Question blocks** — each question has text, optional note, and a concrete recommendation. No input fields.
4. **Annotation controls** — the sticky-note bar (`+ Note` / `Notes` / `Copy Feedback` / `Finalize ✓`) pasted before `</body>`. This is the page's only interactive surface.

## Diagram modes — two only, never Mermaid

Pick the lightest diagram that genuinely helps. Do not add diagrams for decoration, but do add them whenever a question would otherwise require the user to mentally simulate flow, ownership, dependency order, or data shape.

- **Default** → ASCII/tree diagram in a `<pre>` block.
- **When ASCII can't carry it** → full HTML drawn row by row using `.grill-fig`, `.flow`, `.flow-row`, `.flow-box`, `.flow-arrow`, and `.chip`.

**Mermaid is forbidden.** It renders from a CDN at view time, so any syntax slip produces a silently broken diagram — it has caused more problems than it solved.

## Question block shape

```html
<div class="grill-q">
  <div class="grill-q-text">The question?</div>
  <div class="grill-q-note">Why this matters / context.</div>
  <div class="grill-q-rec">Recommended: concrete recommended answer + why.</div>
</div>
```

No answer field. The user answers by dropping a sticky note on the block; Copy Feedback tags each note with the nearest question/heading so the pasted block reads unambiguously.

## Complex visual vocabulary

```html
<div class="grill-fig">
  <div class="grill-fig-cap">flow</div>
  <div class="flow">
    <div class="flow-row">
      <span class="flow-box in">input<small>source</small></span>
      <span class="flow-arrow">→</span>
      <span class="flow-box sut">worker<small>decision point</small></span>
      <span class="flow-arrow">→</span>
      <span class="flow-box out">result<small>output</small></span>
    </div>
  </div>
</div>
```

## Final plan output

When the workflow produces a plan artifact, it must produce both:

- `plan.md` — canonical, token-lean agent/tooling surface.
- `plan.html` — same plan rendered in the dark visual style with annotation controls before `</body>`.

…and freeze the finalized pair as the next `plan-v<k>.md` + `plan-v<k>.html` (see "Versioned history"). Where a file-send tool exists, send `plan.html` as a downloadable file as well.

## Regression rule

A customized planning change is incomplete if tests or review show any of these:

- plain Q&A-only grill as the default path,
- any page-level answer box or fill-in field reintroduced,
- any Submit/Approve-style button, browser→assistant POST-back, or tool call that blocks waiting on the browser,
- missing annotation/feedback controls,
- a plan revised in place with no new `plan-v<k>` snapshot, or any `grill-v*`/`plan-v*` history file edited after the fact,
- a hardcoded `localhost` URL where `.planish.yaml` sets `host:`, or no downloadable copy offered where a file-send tool exists,
- dropped ASCII/HTML visual fields, or any Mermaid reintroduced,
- questions leading with file lists/undefined acronyms instead of plain-English mechanism questions,
- a round page missing its unique `desdoc-key` (stale annotations),
- suite planners drifting away from this contract.
