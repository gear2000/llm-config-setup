Interview the user relentlessly until you reach a shared understanding. Map the plan or design as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait for the user's answers before the next round.

## Rendering a round

Default to a visual sheet when the environment supports it; fall back to chat text otherwise.

- **Visual (default):** render the round as a static annotatable HTML page following the shared Planish HTML Grill Contract at `.shared-llm/public/llm/common/common/planish-html-grill-contract.md` when that file exists, and serve it with the `lavish` skill (`npx -y lavish-axi <html-file>`) when available. One card per question: title, body, choices, and your recommended answer pre-marked. Annotation only — sticky notes and Copy Feedback; no in-page submit. The user pastes feedback back.
- **Chat fallback:** format the round like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

## Working the tree

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, etc.), dispatch a sub-agent to find it; don't ask the user for anything you could look up yourself. Don't block on it: a running exploration is an unsettled prerequisite, so only the questions downstream of it wait for the sub-agent to report; ask the rest of the frontier now. The _decisions_ are the user's: put each to them and wait.

## Docs write-back (`--docs`)

When invoked with `--docs` (or the user asks for docs as you go), record decisions as they settle, in the same round they settle:

- New or sharpened vocabulary goes into the repo's `CONTEXT.md` glossary (create it if missing). Use the repo's own words; never invent jargon.
- A settled decision with real alternatives and consequences becomes an ADR under `docs/adr/` (create the dir if missing), following the repo's existing ADR format if one exists.
- Cite the doc you updated in the next round's header so the user can veto.

The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. Do not act on it until the user confirms you have reached a shared understanding.
<!-- Grilling method vendored from matt-pocock-skills (MIT, (c) 2026 Matt Pocock), extended with visual sheets and docs write-back for the shared-llm kit. -->
