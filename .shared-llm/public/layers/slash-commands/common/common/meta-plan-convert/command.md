# /meta-plan-convert

Convert a loose Markdown plan into the two-file Herdr input shape. This portable helper is available to every planning harness.

## Invocation

```text
/meta-plan-convert <source.md> <plan-output.md> [route-output.yaml]
```

If `route-output.yaml` is omitted, write `route.todo.yaml` next to `plan-output.md`.

HTML input is not supported. If the user passes `plan.html`, ask for the paired Markdown file.

## Output shape

```text
converted-meta-job/
├── plan.md
└── route.yaml or route.todo.yaml
```

## Conversion rules

- Preserve the source plan's intent and scope.
- Convert the work plan into:

  ```text
  # Plan: <title>

  Goal: <one clear goal>

  ## Phase 0 — <phase title>

  <work to do>

  Done:
  - <checkable completion criteria>

  Ideal:
  - <optional stretch goal>
  ```

- Number phases from 0 in order.
- Keep `Done:` criteria when present.
- If a phase has no honest completion condition, write:

  ```text
  Done:
  - TODO — needs a checkable condition
  ```

- Move model, harness, agent, worker, team, stage-routing notes, merge timing, worktree details, and CI/CD checks out of `plan.md`.
- Keep supporting/reference material you preserve from the source (context, non-negotiables,
  acceptance gates, tables, handoff notes) as `###` (H3) sections placed BEFORE `## Phase 0`.
  Only `## Phase <N> — <title>` may be an H2, and nothing may trail after the last phase — the
  runtime validator scans trailing content as part of that phase's `Done:` block.
- Scrub `todo`, lowercase `<angle-placeholder>` tokens (e.g. `<id>`, `<sha>`), and `{{...}}` out
  of the `Goal:` line and every `Done:` block — write real values or plain prose. (The deliberate
  `Done: - TODO — needs a checkable condition` marker is the one exception: it is SUPPOSED to
  fail the check until a human resolves it.)
- Never mention the route file by name, LLM profiles, worktrees, or merge timing in the plan
  body — not even a pointer sentence; the check fails on the words alone.
- If this is an interactive conversion, ask the user for each phase: merge back at Stage 3, Stage 4, or Stage 5. If the conversion is non-interactive, set `merge_back_at: stage-3-integration-acceptance-seams` for safety.
- Write a route profile using this shape:

  ```yaml
  llm_profiles:
    TODO-profile:
      harness: TODO
      model: TODO

  worktree:
    branch_template: tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}

  finalization_defaults:
    green_checks:
      - command: TODO
    log_checks:
      - source: TODO

  phases:
    phase-0:
      merge_back_at: stage-3-integration-acceptance-seams
      lead:
        llm_profile: TODO-profile
        agent: TODO
      stages:
        stage-1-implementation:
          llm_profile: TODO-profile
          agent: TODO
        stage-2-adversarial-audit:
          llm_profile: TODO-profile
          agent: TODO
        stage-3-integration-acceptance-seams:
          llm_profile: TODO-profile
          agent: TODO
        stage-4-upstream-dag-verification:
          llm_profile: TODO-profile
          agent: TODO
        stage-5-finalization:
          llm_profile: TODO-profile
          agent: TODO
  ```

- Do not invent profiles, models, harnesses, agents, green-check commands, or log sources.
- If real routing cannot be inferred from explicit source text, leave TODO values. A TODO route is expected to fail `/meta-plan-check` until a human completes it.

## After writing

Run or instruct the user to run:

```text
/meta-plan-check <plan-output.md> <route-output.yaml>
```

Report whether the output is runnable now or still needs human route completion. After `/meta-plan-check` passes, planning stops and the human may run `/herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>`.
