---
name: cc-full
description: 'Phone-friendly Claude Code composer: run `/cc-plan` exactly once, then either `/cc-implement` once for direct work or `/cc-convert --herdr` once plus `just run-start` once. Prompts for mode when no execution flag is supplied.'
---

# /cc-full

Phone-friendly Claude Code composer for the approved plan workflow.

## Invocation

```text
/cc-full "<ask>" [--execute-direct | --execute-with-herdr] [--adversarial-iterations N] [--adversary-profile <profile>]
```

## Workflow

1. Resume from durable receipts if this command already has a plan directory. Do not redo completed research, grill, adversarial review, conversion, or execution-start steps.
2. Run `/cc-plan <same planning arguments>` exactly once. Wait for final human plan approval. The planning command owns Planish grill, conditional design, and the default two-round plan-adversary loop.
3. After the final approved `plan.md` exists, choose the path:
   - `--execute-direct` preselects direct implementation.
   - `--execute-with-herdr` preselects Herdr conversion/execution.
   - With neither flag, prompt the human once: direct implementation, Herdr execution, or stop after planning.
4. Direct path: run `/cc-implement <approved-plan.md>` exactly once. Do not convert for Herdr.
5. Herdr path: run `/cc-convert --herdr <approved-plan.md>` exactly once. If it returns `DESIGN_REQUIRED`, stop and send the work back to the planner with the evidence; do not start the checked run. If conversion passes, run exactly one shell launcher:

   ```text
   just run-start <converted-run-dir>
   ```

6. Report the resulting direct implementation session or TUI controller name.

## Hard rules

- Do not implement a second planning review loop here; the planning command owns it.
- Do not run a standalone check command; conversion validates internally and the run launcher rechecks at startup.
- Do not call both execution paths.
- Execution flags never bypass final human approval of the plan.
