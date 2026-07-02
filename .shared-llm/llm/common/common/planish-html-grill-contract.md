# Planish HTML Grill Contract

This is the shared contract for customized planning flows. It applies to the Pi `/planish` extension and to the suite planners (`/do-plan-and-grill`, `/cc-plan-and-grill`). Standalone skill variants `/do-planish` and `/cc-planish` are intentionally removed to avoid overlapping meanings.

## Non-negotiable

Customized planning must not default to a plain chat list of questions. The default grill surface is a browser HTML page that explains the decision visually, collects answers, and supports annotation/feedback.

Terminal questions are allowed only for an explicit fallback/interactive mode where no browser/static server is available.

## Answer transports — never mix

There are exactly two ways answers travel back, and a session uses ONE:

1. **Tool-return (Pi `planish_grill` / `planish_submit_plan`).** The tool call blocks; the page it serves (localhost:4390) MUST carry a **Submit** button that POSTs back and unblocks the call. While a planish tool call is open, the TUI is blocked — paste-back is impossible, so never direct the user to a Copy-Answers-only page in this mode.
2. **Paste-back (static file, e.g. Claude Code).** A subagent writes the grill HTML to disk, the assistant turn ENDS after giving the URL, and the user pastes the Copy Answers markdown back into chat. No Submit button needed — nothing is blocked.

A blocking tool call pointed at a page without a Submit button deadlocks the session. Any grill implementation must make the abort path work: cancelling the tool call from the TUI must unblock it cleanly.

## Write for the user

The grill is an interview about the design, not a changelog. Hard rules:

1. **Open in plain English.** The context header explains what the plan is trying to do and what was found so far, in complete sentences a teammate could follow without reading the code.
2. **Questions are about the mechanism or design choice** — which approach, what trade-off, what behavior. Never "these files changed" or a walk through methods; that is noise, not a question.
3. **Technical terms are fine; acronyms must be defined at first use.** Never assume the reader knows an acronym.
4. **File paths, method names, and change lists go in an `Appendix` section at the BOTTOM of the page** — never at the top, never inside a question block.

## Fresh annotations every round

Rounds often reuse one URL/path (`grill_current.html`, a localhost server), and note storage is keyed per page — so every generated round page must carry a unique key: `<meta name="desdoc-key" content="<date>-<slug>-r<round>">` in `<head>` (the annotation toolkit uses it and clears the previous round's notes). Server-rendered grills (Pi `planish_grill`) do this automatically. A round that shows the previous round's sticky notes is a bug.

## Required page sections

Every grill round must include:

1. **Context header** — what is being planned, current draft/plan shape, and decisions already locked.
2. **Visual framing** — one or more diagrams that make the user's choice understandable.
3. **Question blocks** — each question has text, optional note, recommendation, and an answer textarea.
4. **Answer controls** — Copy Answers emits markdown for paste-back or tool return.
5. **Annotation controls** — sticky-note feedback and Copy Feedback / finalize behavior.

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
  <textarea class="grill-a" placeholder="Your answer…"></textarea>
</div>
```

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

## Regression rule

A customized planning change is incomplete if tests or review show any of these:

- plain Q&A-only grill as the default path,
- missing Copy Answers,
- missing annotation/feedback controls,
- dropped ASCII/HTML visual fields, or any Mermaid reintroduced,
- questions leading with file lists/undefined acronyms instead of plain-English mechanism questions,
- a round page missing its unique `desdoc-key` (stale annotations),
- suite planners drifting away from this contract.
