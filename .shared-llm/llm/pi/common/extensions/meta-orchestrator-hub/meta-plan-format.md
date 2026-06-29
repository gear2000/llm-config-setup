# Meta plan format

The shape a plan must take for the meta-orchestrator brain to run it well, plus the
non-negotiables every plan carries. `meta-plan:check` verifies a plan against this;
`meta-plan:convert` rewrites a loose plan into it. This is the single source of truth for
both — keep them pointed here, do not redefine the shape elsewhere.

## Shape

- One `# Plan: <title>` heading.
- One `Goal:` line — the overall objective in a sentence or two.
- Phases as `## Phase <N> — <title>`, **numbered from 0, in order**. The brain runs Phase 0
  first, then upward (it may backtrack, but the plan is written in order).
- Each phase contains:
  - **the work** — what to do, the concrete steps;
  - **`Done:`** — the SUFFICIENT bar: a concrete, checkable condition that is *enough to move
    on*. The worker verifies this and reports `PHASE_RESULT` against it. Required.
  - **`Ideal:`** — OPTIONAL: the fuller goal, when it genuinely differs from sufficient.
- **No per-phase worker/agent/team directives.** The worker is chosen globally at launch
  (`--worker-type` / `--model` / `--mode`); per-phase `Agents:` / `team:` lines are obsolete —
  strip them.

## Sufficient vs. ideal — and the human's call

`Done:` is the minimum to proceed, NOT perfection. A phase that meets `Done:` but not `Ideal:`
is *sufficient*: the brain stops and asks the human whether to continue or hard-stop. The brain
never silently lowers the bar to fake a pass, and never lets scope grow chasing `Ideal:`.
Sufficient-or-stop is the human's decision, not the brain's.

When converting, give every phase a sufficient `Done:`. If a source phase has no checkable
condition and none can be honestly inferred from its text, write `Done: TODO — needs a checkable
condition` rather than INVENT one. A visible gap is correct; a fabricated check is not.

## Non-negotiables — every plan carries these; they are never compromised

The approach, design, and code standards of THIS repository are not negotiable. Whoever
executes the plan (and whoever converts it) MUST:

- **Follow this repository's own conventions and design docs.** Read its `CLAUDE.md` /
  `AGENTS.md` / design docs and adhere to them. The repository's contextual approach is the
  authority — over generic industry habit, and over what you think code "should" look like.
  Understand how THIS repo writes code and tests; do not project your own idea onto it. Design
  docs are not to be compromised.
- **Build the real thing or fail loud.** No shortcuts. No mocks, stubs, or graceful degradation
  to pass a test. No empty methods, no dead-end / unreachable / never-used code, no code written
  but never tested.
- **Never drop or silently cut a feature to make a test pass.** If something cannot be made to
  pass, FAIL LOUDLY and surface it. A loud failure now beats a silent gap that bites later.
- **Errors are welcome.** Surface them; never swallow or kick them down the road.

A `Done:` check is about whether the work is genuinely, honestly complete to its sufficient
bar — never about merely turning a test green.
