# /meta-cc-plan-and-grill

Prepare a reviewed, runnable Herdr input without starting execution.

## Invocation

```text
/meta-cc-plan-and-grill <cc-plan-and-grill arguments> --route <route.yaml>
```

## Workflow

1. Run `/cc-plan-and-grill` to research, draft, and grill the plan. Keep its Claude Code planning behavior unchanged.
2. Convert the accepted planning output into the canonical `plan.md` shape with `/meta-plan-convert` when needed. The runnable plan has exactly one `# Plan:` heading, one `Goal:` line, ordered `## Phase N — title` headings, and a non-empty `Done:` section per phase.
3. Write the route profile at the supplied `--route <route.yaml>` path. Ask for every value that conversion cannot honestly infer: profiles, harnesses, models, agents, merge-back stage, finalization checks, and log checks. Never invent those values.
4. Run `/meta-plan-check <plan.md> <route.yaml>`.
5. If the check fails, resolve its reported plan or route issue and re-run the check. Do not claim a TODO route is runnable.
6. After `PLAN_CHECK: PASS`, stop planning and print exactly:

   ```text
   /herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>
   ```

## Hard rules

- Planning produces and checks the frozen `plan.md` + `route.yaml` pair; it never starts workers or edits implementation code.
- A human deliberately starts `/herdr-run` after reviewing the checked files.
- `cc-*` remains the Claude Code planning front door. This helper only normalizes and checks the runner input.
