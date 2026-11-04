# PRD to Plan

Break a PRD into a phased implementation plan using vertical slices (tracer bullets). Output is a Markdown file under `work-log/<YYYY-MM-DD>/<slug>/plan/`.

## Process

### 1. Confirm the PRD is in context

The PRD should already be in the conversation. If it isn't, ask the user to paste it or point you to the file.

### 2. Explore the codebase

If you have not already explored the codebase, do so to understand the current architecture, existing patterns, and integration layers.

### 3. Identify durable architectural decisions

Before slicing, identify high-level decisions that are unlikely to change throughout implementation:

- Route structures / URL patterns
- Database schema shape
- Key data models
- Authentication / authorization approach
- Third-party service boundaries

These go in the plan header so every phase can reference them. Link to authoritative requirements and design decisions instead of copying their documents. Preserve all existing requirement, invariant, and acceptance IDs exactly; never renumber or reuse them. Assign `REQ-n`, `INV-n`, or `AC-n` only to items missing an ID, avoiding collisions. An accepted PRD or design is approved intent, not proof of implementation.

### 4. Draft vertical slices

Break the PRD into **tracer bullet** phases. Each phase is a thin vertical slice that cuts through ALL integration layers end-to-end, NOT a horizontal slice of one layer.

<vertical-slice-rules>
- Each slice delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed slice is demoable or verifiable on its own
- Prefer many thin slices over few thick ones
- Do NOT include specific file names, function names, or implementation details that are likely to change as later phases are built
- DO include durable decisions: route paths, schema shapes, data model names
</vertical-slice-rules>

### 5. Quiz the user

Present the proposed breakdown as a numbered list. For each phase show:

- **Title**: short descriptive name
- **User stories covered**: which user stories from the PRD this addresses, linked by their existing IDs
- **Verification coverage**: linked requirement/invariant/acceptance IDs and the future checks each phase will support

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Should any phases be merged or split further?

Iterate until the user approves the breakdown.

### 6. Write the plan file

Write the plan to `work-log/<YYYY-MM-DD>/<slug>/plan/plan.md` — a work-log-relative path (no leading `./`, no host or server assumptions), where `<slug>` is the slugified feature name and `<YYYY-MM-DD>` is today's date. Create the directory if it doesn't exist. Use the template below.

For each criterion, specify a future verification command or observable scenario and expected result, including relevant success, invalid-input, denial, and failure cases through public entry points and browser checks for visible behavior. Planning is not execution: unrun checks are `UNVERIFIED`; never claim runtime `PASS` based on an approved plan or design. Define the evidence to collect later per criterion: `PASS` / `FAIL` / `UNVERIFIED`, tested revision (including relevant uncommitted changes), executed command or browser observation, actual outcome, and artifact reference or blocker. Keep that evidence distinct from the overall approval verdict and preserve any caller-required schema.

<plan-template>
# Plan: <Feature Name>

> Source PRD: <brief identifier or link>

## Architectural decisions

Durable decisions that apply across all phases:

- **Routes**: ...
- **Schema**: ...
- **Key models**: ...
- (add/remove sections as appropriate)

---

## Phase 1: <Title>

**User stories**: <list from PRD>

### What to build

A concise description of this vertical slice. Describe the end-to-end behavior, not layer-by-layer implementation.

### Acceptance criteria

- [ ] <Existing criterion ID and source link>: <observable outcome>; planned check: <command or scenario and expected result>; evidence: UNVERIFIED (not executed)
- [ ] <Next criterion ID and source link>: ...

---

## Phase 2: <Title>

**User stories**: <list from PRD>

### What to build

...

### Acceptance criteria

- [ ] <Existing criterion ID and source link>: <observable outcome>; planned check: <command or scenario and expected result>; evidence: UNVERIFIED (not executed)

<!-- Repeat for each phase; assign AC-n only when a criterion has no existing ID. -->
</plan-template>
