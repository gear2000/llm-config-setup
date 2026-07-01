# Planish Consistent Planning UX

## Final direction

Remove the confusing standalone skill variants:

- Remove `/do-planish`.
- Remove `/cc-planish`.

Keep the command model parallel and clear:

```text
Standalone Pi planner
└─ /planish
   └─ Pi extension, browser-first, annotatable planning

Workflow-suite planners
├─ /do-plan-and-grill
│  └─ Pi / portable workflow suite
└─ /cc-plan-and-grill
   └─ Claude Code workflow suite
```

This avoids three overlapping meanings for “planish.”

## Why remove `/do-planish` and `/cc-planish`

They create confusion because they sound like suite commands, but they overlap with standalone Planish behavior.

- `/planish` already covers standalone planning in Pi.
- `/do-plan-and-grill` covers the richer `do-*` workflow-suite planning path.
- `/cc-plan-and-grill` covers the same richer suite path in Claude Code.
- A separate `/cc-planish` would imply Claude Code has a standalone Planish equivalent, but that is not needed and makes the command model less parallel.

So the clean model is:

- Standalone planning: `/planish` only.
- Suite planning: `/do-plan-and-grill` and `/cc-plan-and-grill`.

## Required consistency

Even after removing `/do-planish` and `/cc-planish`, the visual planning behavior must be consistent wherever customized planning happens.

The following must use the same visual HTML planning contract:

- `/planish`
- `/do-plan-and-grill`
- `/cc-plan-and-grill`

Shared requirements:

1. No plain Q&A-only planning flow by default.
2. Browser-first grill when possible.
3. Annotatable HTML.
4. Copy Answers / Copy Feedback controls.
5. Diagram ladder:
   - Simple: Mermaid.
   - Medium: ASCII/tree.
   - Complex: full HTML flow boxes.
6. Final output includes `plan.md` and annotatable `plan.html` where the workflow produces a plan artifact.

## Command taxonomy

```text
Planning commands
├─ /planish
│  ├─ Standalone Pi planner
│  ├─ Extension-backed
│  ├─ Browser-first
│  └─ Not part of do-* / cc-* suite
│
├─ /do-plan-and-grill
│  ├─ Pi / portable workflow-suite planner
│  ├─ Research → draft → grill → finalize
│  ├─ Can feed implementation / phase conversion / meta orchestration
│  └─ Uses the same visual HTML grill contract
│
└─ /cc-plan-and-grill
   ├─ Claude Code workflow-suite planner
   ├─ Parallel to /do-plan-and-grill
   ├─ Claude-compatible orchestration
   └─ Uses the same visual HTML grill contract
```

## Simple flow

```text
User wants standalone Pi planning
  ↓
Use /planish
  ↓
Visual browser grill
  ↓
Annotatable plan

User wants workflow-suite planning in Pi
  ↓
Use /do-plan-and-grill
  ↓
Research → draft → visual grill → finalized plan
  ↓
Implementation / phases / meta orchestration as needed

User wants workflow-suite planning in Claude Code
  ↓
Use /cc-plan-and-grill
  ↓
Same suite concept, Claude-compatible mechanics
```

## Implementation phases

### Phase 1 — Remove standalone skill variants

Remove or stop installing:

- `/do-planish`
- `/cc-planish`

Clean up:

- recipe files if they should no longer generate,
- installed skill routing,
- docs references,
- README / onboarding references,
- stale generated `.claude/skills/*` examples if appropriate.

### Phase 2 — Define the canonical visual planning contract

Create one source of truth for visual planning/grill pages.

It should define:

- Required context header.
- Required decision framing.
- Required diagram ladder.
- Required answer capture.
- Required annotation controls.
- Required final plan behavior.

### Phase 3 — Fix `/planish`

Update the Pi extension so `planish_grill` cannot degrade into boring plain Q&A.

It should support structured visual fields:

- `contextHtml`
- `mermaid`
- `ascii`
- `visualHtml`
- `question`
- `note`
- `recommendation`

It should render a browser page that supports:

- visual context,
- answer capture,
- sticky-note annotation,
- Copy Answers,
- Copy Feedback / finalize behavior.

### Phase 4 — Fix `/do-plan-and-grill` and `/cc-plan-and-grill`

Update both suite planners to reference the same visual planning contract.

They should not carry separate, drifting instructions for how to render grill pages.

They should both require:

- visual HTML grill pages,
- diagram ladder,
- annotations,
- copyable answers,
- final annotatable `plan.html`.

### Phase 5 — Update installer routing

After removing `/do-planish` and `/cc-planish`:

- Pi should not get `/do-planish`.
- Claude Code should not get `/cc-planish`.
- Pi should keep `/do-plan-and-grill`.
- Claude Code should keep `/cc-plan-and-grill`.
- Pi should keep `/planish` through the extension.

### Phase 6 — Add regression tests

Tests should fail if:

- `/do-planish` is still installed.
- `/cc-planish` is still installed.
- `/planish` grill output lacks annotation controls.
- `/planish` grill output lacks Copy Answers.
- `/do-plan-and-grill` or `/cc-plan-and-grill` stop referencing the canonical visual contract.
- Mermaid / ASCII / HTML visual fields are dropped.

### Phase 7 — Sync and verify live Pi

The live Pi extension is loaded from the private repo path, so changes must be synced there.

Verification:

1. Sync this repo’s changes into the private repo.
2. Reinstall/resync skills and extensions.
3. Restart Pi.
4. Confirm `/planish` exists.
5. Confirm `/do-planish` does not exist.
6. Confirm `/cc-planish` does not exist in Claude Code.
7. Confirm `/do-plan-and-grill` and `/cc-plan-and-grill` use the visual HTML contract.

## Plain-language recommendation

Use this command model:

- `/planish` = standalone Pi planning.
- `/do-plan-and-grill` = Pi workflow-suite planning.
- `/cc-plan-and-grill` = Claude Code workflow-suite planning.

Remove:

- `/do-planish`
- `/cc-planish`

This keeps the system smaller, less confusing, and more parallel between `do-*` and `cc-*`.
