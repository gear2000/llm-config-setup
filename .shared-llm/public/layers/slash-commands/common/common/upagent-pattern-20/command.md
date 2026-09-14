# /upagent-pattern-20

Run an approved plan with one named YAML workflow per phase, from the pane you are already in. Pattern 20 is Pattern 10 without the agent in the middle: the HIL you are talking to reads the workflows, hires every stage through the UpAgent hub, loops on findings, moves phase by phase and runs finalize. It is built on Pattern 2, so the human can compact this pane whenever it fills up and the run continues from the recorded requests.

```text
Pattern 10:   human <-> HIL (relay) <-> plan-implementer -> UpAgent workers
Pattern 20:   human <-> HIL (runs the phases itself)     -> UpAgent workers
```

## Invocation

```text
/upagent-pattern-20 --plan <plan.md> --workflow <workflow.yaml>
```

No `--offering` or `--effort`: the controller is the agent the human already started. Run inside a human-started Herdr pane (`HERDR_ENV=1`) with a live `$HERDR_PANE_ID`; apply the caller-pane anchoring from `/upagent-run` to every hire. Both flags are explicit. A missing value stops preparation; ask, never guess.

## Pool, workflow and assignment

Identical to `/upagent-pattern-10`: read its installed `SKILL.md` for the workflow file shape (`stages`, `retries`, `pass`, `controller_requirements`), the assignment keys (`phases` or `plan_flow`, `validation`, `finalize`, optional `pool`) and the pre-launch checks. Its `workflows/` pool is this skill's pool too; the same files serve both patterns. The one difference: the assignment's `controller` key is ignored here, because this pane is the controller. `controller-requirements.md` binds this pane directly.

## Run

1. Preflight exactly as Pattern 10: approved plan, every phase assigned once, safe YAML parse, every offering and effort present in `just upagent lists --type offerings --json`, open choices settled with the human and recorded, checkout or worktree authorized.
2. Create `<plan-dir>/pattern-20/<run-id>/` and write `run-status.md` there: plan path, workflow path, resolved pool, run decisions, then one line per phase and per finalize workflow. Never modify the approved plan.
3. For each phase in plan order, read its assigned workflow and hire its stages top to bottom through `just upagent` with the anchored cockpit arguments. One writer at a time; every reviewer, auditor and checks stage is a fresh worker. After each receipt, record request id, candidate identity, findings and the loop counter in `run-status.md` before placing the next hire.
4. On findings, hire the same coder again with the findings. That is one loop. Stop after `retries` loops and ask the human, in this pane, plainly. A stage with its own `retries` keeps its own counter. Advance only when the current candidate passes every stage.
5. After the last phase, run each assigned finalize workflow on the combined candidate, in list order. All must pass.
6. Report the outcome in this pane: completed, failed or blocked, with the evidence path. A merged candidate or a worker saying "done" is not final acceptance; this run pushes and deploys nothing.

## Resume and compaction

The human may run `/compact` at any point. After compaction, or on any restart, read `run-status.md` and the UpAgent receipts first and continue from the recorded request; never re-hire from memory and never reset a counter. The hub refuses duplicate requests, which is the mechanical backstop.

## Rules

Delegate all production coding, fixes, validation and independent reviews through UpAgent; Pattern 2's local-coding exception does not apply here. Do not start `/hil`, `/plan-implementer`, `/tui-control`, phase leaders or native subagents. Ordinary workers do not hire workers. Preserve human gates and the plan's original acceptance criteria. Keep private plans and run trees in the destination, never in this public kit.
