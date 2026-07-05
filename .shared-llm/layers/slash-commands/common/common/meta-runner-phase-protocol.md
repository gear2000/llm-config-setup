# Shared meta-runner phase protocol

This is the shared execution contract for Meta-CC, Meta-ORCH, and Meta-Herdr. It is deliberately generic and public: examples use placeholder model and agent names only.

## Runnable input is two files

Every semi-AFK meta run consumes both files below. This is the schema from the 2026-07-04 meta runner synchronization plan-v9 plus the five-stage worktree lifecycle update.

```text
runnable-meta-job/
├── plan.md       # the work
└── route.yaml    # who runs it, when it merges, and how finalization proves green
```

The canonical plan body stays clean:

- one `# Plan: <title>` heading;
- one `Goal:` line;
- ordered `## Phase <N> — <title>` sections starting at 0;
- phase work plus required `Done:` and optional `Ideal:`.

Do **not** put runner, model, harness, agent, team, worker, stage routing, merge timing, worktree branch names, or CI/CD checks in the plan body. If a draft plan contains that information, move it to the route profile or fail loud for human correction.

The route profile centralizes `llm_profiles`, worktree lifecycle, finalization checks, deterministic merge timing, and explicit phase/stage agent choices.

Required route shape:

```yaml
llm_profiles:
  claude-low:
    harness: claude
    model: configured-claude-model
    effort: low
    advisor:
      enabled: false

  claude-low-with-advisor:
    harness: claude
    model: configured-claude-model
    effort: low
    advisor:
      enabled: true
      model: configured-frontier-advisor
      effort: medium
      required: false

  codex-auditor:
    harness: codex
    model: configured-codex-model
    effort: medium

  pi-default:
    harness: pi
    model: configured-default

worktree:
  branch_template: tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}

finalization_defaults:
  green_checks:
    - command: task ci
  log_checks:
    - source: build, deploy, and runner logs
      fail_patterns: ERROR,FATAL,Traceback,uncaught

phases:
  phase-0:
    merge_back_at: stage-3-integration-acceptance-seams
    lead:
      llm_profile: claude-low-with-advisor
      agent: phase-evaluator
    stages:
      stage-1-implementation:
        purpose: "unit tests + implementation in a TDD coding loop on the temp worktree branch"
        llm_profile: claude-low-with-advisor
        agent: backend
      stage-2-adversarial-audit:
        purpose: "hostile audit of Stage 1 code on the same temp worktree branch"
        llm_profile: codex-auditor
        agent: adversarial-evaluator
        must_differ_from: stage-1-implementation
      stage-3-integration-acceptance-seams:
        purpose: "determine/create/update/run higher-level tests at module seams; merge here iff merge_back_at selects this stage"
        llm_profile: claude-low
        agent: qa
      stage-4-upstream-dag-verification:
        purpose: "build/deploy/test upstream dependents; merge here iff merge_back_at selects this stage"
        llm_profile: pi-default
        agent: monorepo-pkgs
      stage-5-finalization:
        purpose: "merge if needed, verify main, destroy temp worktree/branch, run green checks, inspect logs"
        llm_profile: claude-low
        agent: qa
```

Rules:

- Every phase has `merge_back_at`, with exactly one of Stage 3, Stage 4, or Stage 5.
- Every phase lead has explicit `llm_profile` and `agent` fields.
- Every stage has explicit `llm_profile` and `agent` fields.
- Every phase has all five fixed stage ids.
- The `agent` value is the configured project or harness agent/persona name, such as `general`, `backend`, `golang`, `python`, `frontend`, `qa`, `onboarding`, or a project-specific specialist.
- The runner resolves `agent` from the appropriate harness/project agent directories and fails loud if a required agent cannot be found.
- Prefer domain or feature-specific agents when available. Use `agent: general` only when generic behavior is intentional.
- Repeated templates are allowed only if resolved to explicit phase/stage entries before execution begins.
- `runner_adapters` are optional launch hints, not part of the required runnable-input schema.
- Stage 2 must be independent from Stage 1 by profile, agent, harness, model family, or persona.
- Stage 5 must have effective green checks and log checks from `finalization_defaults` or phase-level `finalization` overrides/additions.
- Advisor settings may be used by a phase Lead Agent or by a stage agent when the selected harness supports advisors. Advisors are advisory only.

## Phase Lead Agent

A meta runner creates one phase Lead Agent per phase. The phase Lead Agent:

- validates the route profile entries for its phase;
- performs the pre-flight boundary check before any stage writes code;
- creates exactly one stage agent at a time;
- injects the stage instructions, route details, worktree branch, deterministic merge timing, and non-delegation rule;
- records evidence and stage outcomes;
- enforces loops between Stage 2 and Stage 1 when blocking audit findings appear;
- enforces the Stage 3/4/5 merge point from `merge_back_at`;
- enforces Stage 5 finalization, cleanup, green checks, and log review;
- writes the final phase result.

A phase Lead Agent may use an advisor when configured. The advisor does not write files, run commands, or create agents.

## Non-delegating stage agents and advisors

Stage agents and advisors are terminal workers. They must not create additional agents, teams, panes, nested harness sessions, or advisors. If a stage agent needs more help, it returns `BLOCKED` with the decision needed; the phase Lead Agent decides the next move.

The synchronized meta path does not use Claude team mode. Keep execution to: meta runner → phase Lead Agent → one stage agent at a time.

## Five-stage phase protocol

### Pre-flight — dependency/import safety

Before Stage 1, inspect the target module/layer and dependency graph. Look for `dependencies.yaml`, `build-dependencies.yaml`, repo-documented equivalents, or derive a graph from local import/build metadata. If a circular dependency involving the target module and parent/dependent layers is confirmed, stop with:

```text
CRITICAL FAIL: Circular dependency detected. Human intervention required before entering Stage 1.
```

Missing canonical graph files are not by themselves fatal. The runner must attempt dynamic graph derivation before blocking.

Before launching Stage 1, create or select the temporary worktree branch using the route template. The default template is:

```text
tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}
```

Refuse to reuse an existing dirty temp worktree. Record the temp worktree path, branch, base commit, and current main branch identity in evidence.

### Stage 1 — unit tests + implementation on the temp worktree

This is the implementation stage. The selected stage agent writes/updates unit tests and production code in one TDD loop on the temporary worktree branch:

1. write or update the relevant unit test;
2. verify expected failure when practical;
3. write the minimal real implementation;
4. run the unit tests to pass;
5. refactor without widening scope.

No hardcoding, bypassing validation, empty stubs, or goal cheating just to pass tests.

### Stage 2 — adversarial audit of Stage 1 code on the same temp worktree

Run an independent hostile reviewer against the files modified in Stage 1 on the same temporary worktree branch. It checks signature mismatches, unused/dead code, and goal cheating.

- `VERIFICATION_PASSED` advances to Stage 3.
- Blocking findings loop back to a new Stage 1 attempt with the raw findings.
- Non-blocking notes are reported but do not fail the phase.

### Stage 3 — integration/acceptance seam testing

If `merge_back_at` is `stage-3-integration-acceptance-seams`, merge the temporary worktree branch back to main at this stage and run Stage 3 from main. Otherwise, continue on the temporary worktree branch.

Review deep module surfaces and seams affected by the Stage 1 change. Determine whether higher-level tests need to be created or updated:

- integration tests;
- acceptance tests;
- seam tests where modules/packages/services interact.

Do not write tests for their own sake. If no public/deep-module seam changed, record the reason and pass the stage. These are not unit tests; they verify behavior where modules interact.

### Stage 4 — upstream DAG dependent build/deploy/test verification

If `merge_back_at` is `stage-4-upstream-dag-verification`, merge the temporary worktree branch back to main at this stage and run Stage 4 from main. If the branch was already merged in Stage 3, run Stage 4 from main. Otherwise, continue on the temporary worktree branch.

Locate the modified package/layer/module in the dependency DAG. Trace every upstream dependent that imports, builds on, deploys with, or otherwise depends on the changed layer. For each upstream node, sequentially run the repo-declared equivalent of:

- build;
- unit tests;
- integration/seam tests;
- deployment or deployment dry-run where required;
- acceptance/live checks where applicable and safe.

If an upstream build/deploy/test fails, save logs outside the repo when possible, stop the pipeline, and trigger rollback policy. Do not blindly fix upstream from the current phase context.

### Stage 5 — finalization, green checks, log review, and cleanup

Stage 5 always runs.

- If `merge_back_at` is `stage-5-finalization`, merge the temporary worktree branch back to main now.
- If the branch was already merged in Stage 3 or Stage 4, verify main contains the expected change.
- Run the effective `green_checks` from `finalization_defaults` plus any phase-level additions/overrides.
- Inspect the effective `log_checks` sources for hidden failures. Treat obvious fatal/error/traceback/uncaught/deploy-failure patterns as hard failures unless an explicit allowlist explains them.
- Destroy/prune the temporary worktree and temporary branch only after merge, green checks, and log review succeed.
- Write final evidence: merge point, main commit, cleanup actions, green-check output, and log-review summary.

If merge, green checks, log review, or cleanup fails, preserve evidence, keep the temporary branch when needed, and return `failed` or `blocked`. Never silently clean up and claim success.

## Rollback safety

At phase start, record a Git baseline: branch/worktree identity, status, and phase-owned file manifest.

- Temporary worktree branch: Stage 4 regression may use a hard reset after logs are saved.
- Main branch: inspect whether uncommitted changes include files outside the phase-owned manifest. Ask the human before destructive rollback; prefer restoring only phase-owned paths when safe.
- Stage 5 cleanup is not allowed until merge/final checks/log review have succeeded.

Never reset unrelated human or agent work without an explicit safety check and human gate.

## Result evidence

Each phase result should report:

- runner name;
- phase id;
- phase lead `llm_profile` and `agent`;
- `merge_back_at` value and actual merge stage;
- temporary worktree branch/path and cleanup result;
- each stage id with `llm_profile` and `agent` used;
- advisor status when applicable;
- dependency graph source;
- commands run and evidence paths/log excerpts;
- upstream verification result;
- Stage 5 green-check and log-review result;
- rollback or cleanup actions.
