---
name: do-plan-and-grill
description: 'Pi planning front door: use the Planish grill, produce and check `plan.md` + `route.yaml`, then stop with the explicit `herdr-run` handoff. The command does not execute implementation.'
---

# /do-plan-and-grill — Pi planning to a Herdr handoff

Research the request, draft a plan, and grill the user until scope and completion criteria are agreed. This command plans only.

## Workflow

1. Use Pi planning tools and fresh research/plan-writing subagents. The leader runs the grill and does not implement the work.
2. Preserve Pi's Planish behavior: use `planish_grill` when available to serve the annotatable HTML grill, give the user its URL, and wait for the pasted feedback. Follow `.shared-llm/public/llm/common/common/planish-html-grill-contract.md`: the user annotates the page, selects **Copy Feedback**, and pastes the feedback into the session. Do not use a plain chat list of questions or add answer boxes, approval buttons, or a browser-to-agent request. Use the explicit `--interactive` fallback only where that surface cannot be used.
3. Preserve the human-readable grill history and visual `plan.html` in the work-log. Freeze each accepted revision as `plan-v1.md`, then increment it; write history **never in place**. Where `SendUserFile` is available, send the HTML file too. Use the nearest `.planish.yaml` `host:` value when giving the user the grill URL.
4. Create the runnable pair in the work-log:
   - `plan.md` uses the canonical shape: one `# Plan:` heading, one `Goal:` line, ordered `## Phase 0 — ...` headings, and a non-empty `Done:` section in every phase.
   - `route.yaml` declares profiles, worktree template, finalization checks, and every phase lead/stage route. Ask for values research does not establish; do not invent them.
   - If the grilled Markdown is not canonical, run `/meta-plan-convert` and retain the original as plan history.
5. Run `/meta-plan-check <plan.md> <route.yaml>`. Resolve every reported error and re-run the check until it prints `PLAN_CHECK: PASS`.
6. Stop. Show the human the checked files and exactly:

   ```text
   /herdr-run --plan <plan.md> --route <route.yaml> --run-root <work-log-dir>
   ```

## Rules

- Do not create phase JSON, start workers, modify code, or continue into an execution loop.
- The human reviews the checked pair and deliberately starts `/herdr-run`.
