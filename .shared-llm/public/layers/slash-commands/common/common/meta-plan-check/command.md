# /meta-plan-check

Check whether a Herdr input pair is runnable. This portable helper is available to every planning harness.

## Invocation

```text
/meta-plan-check <plan.md> [route.yaml]
```

## Required schema

A runnable meta job is two files:

```text
runnable-meta-job/
├── plan.md       # the work
└── route.yaml    # who runs it, when it merges, and how finalization proves green
```

`plan.md`:

```text
# Plan: <title>

Goal: <one clear goal>

## Phase 0 — <phase title>

<work to do>

Done:
- <checkable completion criteria>

Ideal:
- <optional stretch goal>
```

`route.yaml`:

```text
route.yaml
├── llm_profiles
├── worktree
│   └── branch_template: tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}
├── finalization_defaults
│   ├── green_checks
│   └── log_checks
└── phases
    └── phase-N
        ├── merge_back_at: stage-3-integration-acceptance-seams | stage-4-upstream-dag-verification | stage-5-finalization
        ├── lead
        │   ├── llm_profile
        │   └── agent
        └── stages
            ├── stage-1-implementation
            ├── stage-2-adversarial-audit
            ├── stage-3-integration-acceptance-seams
            ├── stage-4-upstream-dag-verification
            └── stage-5-finalization
```

## Check rules

Report exactly one of:

```text
PLAN_CHECK: PASS
PLAN_CHECK: FAIL
```

Fail if:

- `plan.md` does not have one `# Plan:` heading;
- `plan.md` does not have one `Goal:` line;
- phases are not `## Phase <N> — <title>`, starting at 0 with no gaps (em dash ` — ` or hyphen
  ` - ` as separator — an en dash `–` fails);
- any phase is missing `Done:`;
- any non-phase `##` heading is present — supporting/reference sections must be `###` (H3) and
  sit BEFORE `## Phase 0`, never after the last phase (trailing content is scanned as part of
  that phase's `Done:` block);
- the `Goal:` line or any `Done:` block contains `todo`, a lowercase `<angle-placeholder>`
  (e.g. `<id>`, `<sha>`), or `{{...}}`;
- `plan.md` contains model, harness, agent, team, worker, or stage routing;
- `plan.md` mentions the route file by name, LLM profiles, worktrees, or merge-back timing
  anywhere in its body (even a pointer sentence fails);
- `route.yaml` is missing when checking runnable input;
- `route.yaml` lacks `llm_profiles:`, `worktree:`, `finalization_defaults:`, or `phases:`;
- `worktree.branch_template` is missing, TODO, or lacks `{date}`, `{repo}`, `{phase}`, and `{run_id}`;
- any plan phase lacks a matching `phase-N` route entry;
- any phase lacks `merge_back_at` or uses anything other than Stage 3, Stage 4, or Stage 5;
- any phase lead or stage lacks `llm_profile` or `agent`;
- any phase lacks any of the five fixed stage ids (`stage-1-implementation`, `stage-2-adversarial-audit`, `stage-3-integration-acceptance-seams`, `stage-4-upstream-dag-verification`, `stage-5-finalization`);
- a referenced `llm_profile` is not defined;
- Stage 2 is not independent from Stage 1;
- Stage 5 has no effective green check or log check;
- any required value is still `TODO`.

## Hard rules

- Do not modify files.
- Do not auto-convert during check.
- If the check fails, tell the user to run `/meta-plan-convert` or edit `plan.md` / `route.yaml`.
- A plan-only pass is not runnable; semi-AFK runners require both `plan.md` and `route.yaml`.
