# /cc-full — Claude Code planning to a Herdr handoff

`/cc-full` is a Claude Code planning conductor. It does not execute implementation.

## Workflow

1. Invoke `/cc-plan-and-grill <title>` and let it complete the research and grill.
2. Use `/meta-plan-convert` to produce the canonical `plan.md` and `route.yaml` in the work-log directory. Ask the user for any route values that cannot be inferred honestly; do not invent profiles, models, agents, or checks.
3. Run `/meta-plan-check <plan.md> <route.yaml>`. Resolve every reported issue and re-run the check until it prints `PLAN_CHECK: PASS`.
4. Stop and show the human the checked files and this exact next command:

   ```text
   /herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>
   ```

The human decides whether to start Herdr. Do not start workers, modify implementation code, or continue into an execution loop.
