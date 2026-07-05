# /meta-herdr

Run a canonical meta plan through Herdr-visible phase panes. This command is the user-facing Herdr meta runner.

## Invocation

```text
/meta-herdr --plan <plan.md> --route <route.yaml> --output-dir <dir> [--start-phase <N>] [--max-phases <N>]
```

- `--plan <plan.md>` — canonical meta plan. It must keep routing out of the plan body.
- `--route <route.yaml>` — route profile with `llm_profiles` and inline `agent` names.
- `--output-dir <dir>` — directory where phase results and evidence are written.
- `--start-phase <N>` — optional phase number to start from. Default is `0`.
- `--max-phases <N>` — optional safety cap.

All required flags must be present. Fail loud rather than guessing defaults.

## Pre-flight

1. Verify `HERDR_ENV=1`. If not, stop with: `ERROR: /meta-herdr must run inside a Herdr-managed pane.`
2. Run `herdr pane list` to identify the current pane. Do not control Herdr from outside Herdr.
3. Validate the installed Herdr command surface before launch. The documented baseline is pane-oriented commands: `herdr pane split`, `herdr pane run`, `herdr wait agent-status`, `herdr pane read`, and `herdr pane close`. If the local Herdr version exposes different syntax, adapt only after validating it.
4. Read the plan and route profile. Run the same gate as `/meta-plan-check <plan.md> <route.yaml>`. If it fails, stop before creating panes and tell the user to run `/meta-plan-convert` or fix the files. Validate:
   - the plan has canonical meta-plan shape;
   - the route profile defines `llm_profiles`;
   - every phase to run has `lead.llm_profile`, `lead.agent`, `merge_back_at`, and all five stage entries;
   - worktree branch template, green checks, and log checks are configured;
   - every stage has `llm_profile` and `agent`;
   - all referenced profiles exist;
   - each named agent resolves in the appropriate harness/project agent directory;
   - Stage 2 is independent from Stage 1 by profile, agent, harness, model family, or persona.
5. Record a Git baseline: branch/worktree identity, status, and phase-owned file manifest if the plan provides one.

## Execution model

For each phase, in canonical phase order:

1. Create one phase Lead Agent pane with Herdr. Prefer a right split from the current pane and keep focus on the orchestrator pane:

   ```bash
   PHASE_PANE=$(herdr pane split "$CURRENT_PANE" --direction right --no-focus \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
   ```

2. Launch the phase lead using the phase's `lead.llm_profile` and `lead.agent`. The concrete launch command is runner-adapter-specific and must be assembled from the validated route profile plus local harness capabilities. Do not hard-code unsupported model names.
3. Send exactly one `/meta-herdr-phase` invocation to the phase lead pane with the plan path, route path, phase id, and phase output path.
4. Wait for the phase Lead Agent to finish using `herdr wait agent-status <pane> --status done` when available. If status detection is unavailable for that harness, wait for the expected result-file output and then read the pane.
5. Read the phase result file. The result file is the source of truth; pane output is evidence only.
6. Close temporary phase/stage panes only after the result and evidence are persisted.

## Phase Lead Agent contract

The phase Lead Agent runs `/meta-herdr-phase`. It creates exactly one stage agent/pane at a time and runs the shared five-stage worktree protocol:

1. pre-flight dependency/import safety;
2. Stage 1 — unit tests + implementation on the temporary worktree branch;
3. Stage 2 — adversarial audit of Stage 1 code on the same temporary worktree branch, including unused intake / accepted-but-ignored inputs;
4. Stage 3 — integration/acceptance seam testing, merging only if `merge_back_at` selects Stage 3;
5. Stage 4 — upstream DAG dependent build/deploy/test verification, merging only if `merge_back_at` selects Stage 4;
6. Stage 5 — finalization: merge if needed, verify main, destroy temp worktree/branch, run green checks, and inspect logs.

Stage agents and advisors are non-delegating. They must not create agents, teams, Herdr panes, nested harness sessions, or advisors. If they need help, they return `BLOCKED` for the phase Lead Agent to handle.

## Rollback safety

If Stage 4 finds an upstream regression or Stage 5 finds failed green checks/log errors:

- save upstream logs outside the repo when possible;
- if this is a temporary worktree branch, preserve it until evidence and recovery notes are saved;
- if this is a shared/main branch, inspect for changes outside the phase-owned manifest and ask the human before destructive rollback;
- do not fix upstream services from the current phase context unless the plan explicitly says to.

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`.
2. Canonical plan body stays clean; route profile owns profiles and agent names.
3. Do not auto-convert at execution time. `/meta-herdr` only runs already-runnable `plan.md + route.yaml` inputs.
4. Every phase lead and stage must have explicit `llm_profile` and `agent`; every phase must have deterministic `merge_back_at`.
5. The result file is the source of truth.
6. No Claude team mode and no nested delegation.
7. Do not push, deploy, or reset unless the plan and rollback policy explicitly allow it.
