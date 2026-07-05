# /meta-herdr-phase

Run one phase as the Meta-Herdr phase Lead Agent. This command is sent to a phase lead pane by `/meta-herdr`.

## Invocation

```text
/meta-herdr-phase --plan <plan.md> --route <route.yaml> --phase <phase-id> --output <results.md>
```

All four flags are required. Fail loud on any missing or unreadable path.

## Input contract

- `--plan` points to a canonical meta plan.
- `--route` points to a route profile with `llm_profiles` and inline `agent` names.
- `--phase` identifies one canonical plan phase, for example `phase-0` or `0`.
- `--output` is the phase result file to write.

## Phase lead responsibilities

1. Validate this phase's route entry:
   - `lead.llm_profile` exists;
   - `lead.agent` is present;
   - all five stage entries exist;
   - `merge_back_at` is Stage 3, Stage 4, or Stage 5;
   - worktree branch template, green checks, and log checks are configured;
   - every stage has `llm_profile` and `agent`;
   - each referenced profile exists;
   - each named agent resolves in the selected harness/project context;
   - Stage 2 is independent from Stage 1.
2. Run the pre-flight dependency/import safety check before Stage 1.
3. Create exactly one stage agent at a time, using the stage's `llm_profile` and `agent`.
4. Collect evidence from every stage.
5. Write the result file. The result file is the source of truth.

## Stage execution

### Pre-flight — dependency/import safety

Find the target module/layer from the phase and route context. Discover dependency graph files by known names (`dependencies.yaml`, `build-dependencies.yaml`, and repo-documented equivalents) or derive a graph from import/build metadata. If a circular dependency involving the target and parent/dependent layers is confirmed, write a blocked result and include:

```text
CRITICAL FAIL: Circular dependency detected. Human intervention required before entering Stage 1.
```

### Stage 1 — unit tests + implementation on the temp worktree

Create/use the temporary worktree branch named by the route template. Launch the configured Stage 1 agent there. It writes/updates unit tests and real implementation in one TDD loop. It must not hardcode, bypass validation, add empty stubs, or cheat the goal. It is non-delegating.

### Stage 2 — adversarial audit on the same temp worktree

Launch the configured Stage 2 auditor on the same temporary worktree branch. It reviews Stage 1 changes for signature mismatches, unused/dead code, goal cheating, and **unused intake / accepted-but-ignored inputs**. Blocking findings go back to a new Stage 1 attempt with the raw audit report. `VERIFICATION_PASSED` advances to Stage 3. Non-blocking notes are recorded but do not fail the phase.

For the unused-intake check, the auditor must enumerate newly accepted inputs from the phase diff and reject any function parameter, destructured field, request/schema field, config/env value, command-line option, validation parameter, or fixture value that does not affect validation, control flow, transformation, persistence, or downstream calls. It should use AST-aware inspection where available, then cross-check lint/type/static-analysis signals, directly affected call-sites, and semantic test behavior. Do not accept hardcoding, bypasses, stubs, fake intake, or "intentional unused" markers that hide goal cheating. Each failure report must name the ignored input, where it is accepted, expected behavioral role, evidence, affected public surface/call-site when applicable, and whether to remove the intake or wire it into real behavior.

### Stage 3 — integration/acceptance seam testing

If `merge_back_at` is Stage 3, merge the temporary worktree branch back to main here and run Stage 3 from main. Otherwise launch the configured Stage 3 agent on the temporary worktree. It inspects deep module surfaces and seams affected by Stage 1. It creates, updates, and runs integration/acceptance/seam tests only when needed. If no seam changed, record why no higher-level test update was needed.

### Stage 4 — upstream DAG dependent verification

If `merge_back_at` is Stage 4, merge the temporary worktree branch back to main here and run Stage 4 from main. If already merged at Stage 3, run from main. Otherwise launch the configured Stage 4 agent on the temporary worktree. It traces every upstream dependent of the changed layer and runs the repo-declared equivalent of build, unit tests, integration/seam tests, deployment dry-run or deploy checks where required, and acceptance/live checks where safe.

If any upstream check fails, save evidence, stop the phase, and apply rollback safety rules. Do not repair upstream dependents from this phase unless the plan explicitly authorizes it.

### Stage 5 — finalization

Stage 5 always runs. If `merge_back_at` is Stage 5, merge the temporary worktree branch back to main now. If already merged at Stage 3 or Stage 4, verify main contains the change. Then run the effective green checks, inspect configured logs for hidden fatal/error/traceback/uncaught failures, destroy the temporary worktree and temporary branch, and write final evidence. If merge, checks, log review, or cleanup fails, preserve evidence and report `failed` or `blocked`.

## Result file

The result file must start with:

```text
PHASE_RESULT: passed|partial|blocked|failed
```

Include:

- phase id;
- phase lead `llm_profile` and `agent`;
- `merge_back_at` and actual merge stage;
- temporary worktree branch/path and cleanup result;
- every stage id, `llm_profile`, and `agent`;
- advisor status where applicable;
- dependency graph source;
- commands/evidence/log paths;
- Stage 2 audit result;
- Stage 3 seam-test decision;
- Stage 4 upstream DAG result;
- Stage 5 green-check and log-review result;
- rollback/cleanup actions.

Only write `passed` when all five stages completed and Stage 5 cleanup, green checks, and log review passed.

## Hard rules

- Stage agents and advisors are non-delegating: no agents, teams, panes, nested harness sessions, or advisors.
- The canonical plan body stays free of runner/stage routing.
- Do not use Claude team mode for meta execution.
- Do not close panes until evidence is persisted.
- Do not reset shared/main branches without checking for unrelated changes and asking the human.
