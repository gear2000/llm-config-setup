# Planish HTML Grill Contract

This is the shared contract for customized planning flows. It applies to the Pi `/planish` extension and to the suite planners (`/do-plan-and-grill`, `/cc-plan-and-grill`). Standalone skill variants `/do-planish` and `/cc-planish` are intentionally removed to avoid overlapping meanings.

## Non-negotiable

Customized planning must not default to a plain chat list of questions. The default grill surface is a browser HTML page that explains the decision visually, collects answers, and supports annotation/feedback.

Terminal questions are allowed only for an explicit fallback/interactive mode where no browser/static server is available.

## Required page sections

Every grill round must include:

1. **Context header** — what is being planned, current draft/plan shape, and decisions already locked.
2. **Visual framing** — one or more diagrams that make the user's choice understandable.
3. **Question blocks** — each question has text, optional note, recommendation, and an answer textarea.
4. **Answer controls** — Copy Answers emits markdown for paste-back or tool return.
5. **Annotation controls** — sticky-note feedback and Copy Feedback / finalize behavior.

## Diagram ladder

Pick the lightest diagram that genuinely helps. Do not add diagrams for decoration, but do add them whenever a question would otherwise require the user to mentally simulate flow, ownership, dependency order, or data shape.

- **Simple** → Mermaid flow.
- **Medium** → ASCII/tree diagram in a `<pre>` block.
- **Complex** → full HTML flow using `.grill-fig`, `.flow`, `.flow-row`, `.flow-box`, `.flow-arrow`, and `.chip`.

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
- dropped Mermaid/ASCII/HTML visual fields,
- suite planners drifting away from this contract.
