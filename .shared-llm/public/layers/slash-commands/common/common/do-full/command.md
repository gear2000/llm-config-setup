# /do-full — Pi planning to a Herdr handoff

`/do-full` conducts Pi planning only. It does not execute implementation.

## Workflow

1. Invoke `/do-plan-and-grill <title>` and preserve its Pi Planish grill behavior.
2. Use `/meta-plan-convert` to produce canonical `plan.md` and `route.yaml` in the work-log directory. Gather missing route values from the user; never invent profiles, models, agents, or checks.
3. Run `/meta-plan-check <plan.md> <route.yaml>`. Resolve all reported errors and repeat until `PLAN_CHECK: PASS`.
4. Stop and show the human the checked files and this exact next command:

   ```text
   /herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>
   ```

The human starts Herdr deliberately after review. Do not create phase JSON, start workers, or continue into an execution loop.
