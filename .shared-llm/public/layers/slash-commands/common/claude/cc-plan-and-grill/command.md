# /cc-plan-and-grill — Claude Code planning to a Herdr handoff

Research the requested work, draft a plan, and grill the user until the scope and completion criteria are agreed. This command plans only.

## Workflow

1. Use Claude Code planning primitives and fresh research/plan-writing subagents. The leader runs the grill and does not implement the work.
2. Use the HTML Planish grill by default. Use `--interactive` only when the user explicitly requests the terminal fallback. Follow `.shared-llm/public/llm/common/common/planish-html-grill-contract.md`: the user annotates the page, selects **Copy Feedback**, and pastes the feedback into the session. Do not use a plain chat list of questions or add answer boxes, approval buttons, or a browser-to-agent request.
3. Preserve the human-readable grill history and visual `plan.html` in the work-log. Freeze each accepted revision as `plan-v1.md`, then increment it; write history **never in place**. Where `SendUserFile` is available, send the HTML file too. Use the nearest `.planish.yaml` `host:` value when giving the user the grill URL.
4. Create the runnable pair in the work-log:
   - `plan.md` must use the canonical shape: one `# Plan:` heading, one `Goal:` line, ordered `## Phase 0 — ...` headings, and a non-empty `Done:` section in every phase.
   - `route.yaml` must contain profiles, worktree template, finalization checks, and every phase lead/stage route. Ask the user for routing values that research does not establish. Do not invent them.
   - When an existing human-readable plan is not canonical, run `/meta-plan-convert` and retain the source as plan history.
5. Run `/meta-plan-check <plan.md> <route.yaml>`. Fix every reported issue and repeat until it prints `PLAN_CHECK: PASS`.
6. Exit planning and stop. Show the human the checked files and exactly:

   ```text
   /herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>
   ```

## Rules

- Do not create phase JSON, start workers, modify code, or continue into an execution loop.
- The human reviews the checked pair and deliberately starts `/herdr-run`.
