# /run_phase

Run **one phase** of a canonical meta plan from files. This is the file-based phase Lead Agent playbook launched by Meta-ORCH / Meta-CC. The hub is not involved here; this command reads files and writes a result file.

## Invocation

```text
/run_phase --plan <plan.md> --phase <instructions.md> --route <route.yaml> --output <results.md>
```

Required:

- `--plan <plan.md>` — canonical meta plan.
- `--phase <instructions.md>` — phase instructions for this attempt.
- `--route <route.yaml>` — resolved route profile with `llm_profiles` and inline `agent` names.
- `--output <results.md>` — result file to write.

Fail loud on any missing or unreadable required input. Do not guess defaults.

## File contract

The result file at `--output` is the deliverable and source of truth. Its first line must be exactly:

```text
PHASE_RESULT: passed|partial|blocked|failed
```

Then include evidence for the phase lead, stages, advisor status, dependency graph source, commands/logs, and cleanup or rollback actions.

## Execution

### Step 1 — Parse and validate inputs

Read the whole plan, the phase instructions file, and the route profile. Run the same gate as `/meta-plan-check <plan.md> <route.yaml>` first. Validate:

- canonical plan shape;
- no runner/stage routing in the plan body;
- the selected phase exists;
- the phase has `lead.llm_profile` and `lead.agent` in the route profile;
- all five stages have `llm_profile` and `agent`;
- the phase has `merge_back_at` set to Stage 3, Stage 4, or Stage 5;
- worktree branch template, green checks, and log checks are configured;
- referenced profiles exist;
- named agents resolve in the appropriate Claude/project or harness context;
- Stage 2 is independent from Stage 1.

### Step 2 — Act as phase Lead Agent

You are the phase Lead Agent. You orchestrate one phase; you do not do domain work directly. Create exactly one non-delegating stage agent at a time according to the route profile.

Stage agents and advisors must not create agents, teams, panes, nested harness sessions, or advisors. If they need help, they return `BLOCKED` and you decide or escalate.

### Step 3 — Run the shared five-stage worktree protocol

1. **Pre-flight** — dependency/import graph safety. If a circular dependency involving the target and parent/dependent layers is confirmed, write `blocked` and include the required critical-fail message.
2. **Stage 1** — unit tests + implementation in a TDD loop on the temporary worktree branch.
3. **Stage 2** — adversarial audit of Stage 1 code on the same temporary worktree branch, including unused intake / accepted-but-ignored inputs. Blocking findings loop back to Stage 1; `VERIFICATION_PASSED` advances.
4. **Stage 3** — integration/acceptance seam testing. Merge here only if `merge_back_at` selects Stage 3; otherwise continue on the temp worktree.
5. **Stage 4** — upstream DAG dependent build/deploy/test verification. Merge here only if `merge_back_at` selects Stage 4; otherwise use main if already merged or continue on the temp worktree.
6. **Stage 5** — finalization. Merge if needed, verify main, destroy the temp worktree/branch, run green checks, inspect logs, and record evidence.

### Step 4 — Write `results.md`

Write `--output` with the `PHASE_RESULT:` first line and a report containing:

- phase id;
- phase lead `llm_profile` and `agent`;
- `merge_back_at` and actual merge stage;
- temp worktree branch/path and cleanup result;
- every stage id, `llm_profile`, and `agent`;
- advisor status;
- dependency graph source;
- stage evidence;
- upstream verification;
- Stage 5 green-check and log-review evidence;
- rollback/cleanup actions.

Write `passed` only when all five stages completed and Stage 5 cleanup, green checks, and log review passed. Otherwise write `partial`, `blocked`, or `failed` honestly.

## Hard rules

1. Canonical plan body stays clean; route profile owns execution routing.
2. Do not auto-convert at phase execution time; invalid inputs fail before semi-AFK work.
3. Every phase lead and stage requires explicit `llm_profile` and `agent`.
4. No Claude team mode for synchronized meta execution.
5. Stage agents and advisors are non-delegating.
6. The result file is the source of truth.
7. This command is hub-agnostic.
