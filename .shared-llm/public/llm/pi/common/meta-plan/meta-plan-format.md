# Meta plan runnable input format

This format defines the runnable `plan.md` + `route.yaml` input that planners prepare for Herdr. Herdr is the sole active runner. Do not invent a runner-specific plan shape.

A runnable meta job is **two files**:

```text
runnable-meta-job/
├── plan.md       # the work
└── route.yaml    # who runs it, when it merges, and how finalization proves green
```

The runner only starts when both files pass validation. Herdr automatically assigns a 3-hour worker timeout to `stage-1-implementation` and `stage-2-adversarial-audit`; an order may explicitly override it with `timeout_ms`. There is only ever ONE route file:
`route.todo.yaml` is not a second file — it is the same route file, named `.todo` while required
values are still TODO placeholders so the runnable check fails loudly. Fill the TODOs and rename
it to `route.yaml`; the runner consumes exactly `plan.md` + `route.yaml`.

```text
plain Markdown plan
   │
   ▼
meta-plan convert
   │
   ├── plan.md
   └── route.yaml or route.todo.yaml
          │
          ▼
meta-plan check
   │
   ├── PASS → runner may start
   └── FAIL → human fixes plan or route first
```

## `plan.md` schema

```text
# Plan: <title>

Goal: <one clear goal>

## Phase 0 — <phase title>

<work to do>

Done:
- <checkable completion criteria>

Ideal:
- <optional stretch goal>

## Phase 1 — <phase title>
...
```

Rules:

```text
plan.md
├── one # Plan title
├── one Goal line
├── phases numbered from 0, in order
├── every phase has Done:
├── Ideal: is optional
└── no routing, merge timing, worktree, or CI/CD config in the plan body
```

No model names, harness names, agents, teams, worker rosters, stage routing, branch names, merge timing, or CI/CD checks go in `plan.md`.

### Strict body rules (enforced by the runtime validator)

The runtime validator (`meta-plan-schema.ts` — the same `validateRunnable` check
`/meta-plan-check` and `/herdr-run` run before launching any worker) is the authority, and it is stricter
than the shape sketch above:

- `##` (H2) headings are for phases ONLY — `## Phase <N> — <title>`, separated by an em dash
  (`—`) or a hyphen (`-`), never an en dash (`–`). Any other `##` heading fails the check.
- Supporting/reference material (context, non-negotiables, acceptance gates, tables, handoff
  notes) goes in `###` (H3) sections placed BEFORE `## Phase 0` — never after the last phase: the
  last phase's block runs to end-of-file, so trailing content gets scanned as part of that
  phase's `Done:` block.
- Exactly one `Goal:` line. No body line may start with a routing key such as `model:`,
  `agent:`/`agents:`, `team:`, `worker:`, `harness:`, `ci:`, `lead:`, `stages:`, `llm_profiles:`,
  `merge_back_at:`, `worktree:`, `branch_template:`, `green_checks:`, `log_checks:`,
  `finalization:`, `phases:`, or `runner_adapters:`.
- The body must not mention the route file by name, LLM profiles, worktrees, or merge-back timing
  at all — even a pointer sentence fails the heuristic; route information lives only in the route
  file.
- The `Goal:` line and every `Done:` block must be free of unresolved placeholders: the word
  "todo", a lowercase `<angle-placeholder>` (e.g. `<id>`, `<sha>`), or `{{...}}` all fail. Write
  real values or plain prose (`resource remove … destroy=True`, not `id=<id>`). Placeholders in
  other body prose are tolerated by the validator, but avoid them. (A deliberate
  `Done: - TODO — needs a checkable condition` from conversion is SUPPOSED to fail the check
  until a human resolves it.)

## `route.yaml` schema

```yaml
llm_profiles:
  claude-low:
    harness: claude
    model: configured-claude-model
    effort: low

  pi-default:
    harness: pi
    model: configured-default

# Profile rules (route authors get these wrong most often):
#   - `model` must be the HARNESS-NATIVE id shape:
#       claude → alias or full name (e.g. a configured claude alias); pairs with `effort`
#       codex  → bare model id; pairs with `effort` (mapped to the CLI's reasoning-effort)
#       pi     → `provider/id[:thinking]` — the optional `:thinking` suffix IS pi's effort,
#                so a pi profile normally omits `effort`
#     A `provider/…` model on a claude or codex profile (or a bare id on a pi profile)
#     is a routing error — the worker CLI will reject or misread it.
#   - `effort` is OPTIONAL. At order time the phase leader resolves it to `medium` when the
#     profile omits it, so launch templates can always substitute `{effort}`.
#
# Recommended default profiles (as of 2026-07 — revisit as models move):
#   implementation (stage-1):       codex  gpt-5.6-terra       effort medium
#   adversarial audit (stage-2):    claude opus (Opus 4.8)     effort high (max for critical phases)
#   advisor / lead / high-judgment: codex gpt-5.6-sol effort high,
#                                   or claude fable (Fable 5) effort medium,
#                                   or claude opus effort max
#   planning (outside runs):        Claude Code or Codex directly; through pi use
#                                   openai-codex/gpt-5.6-sol:medium|high, anthropic opus at
#                                   max, or fable at low|medium
#
# GPT-5.6 OVERPRODUCTION GUARDRAIL. The 5.6 family (terra especially) tends to write far
# more code and far more tests than a stage needs. Every stage brief for a worker routed
# to ANY gpt-5.6 model MUST carry an explicit scope-discipline instruction: implement only
# what the stage requires, no speculative abstractions or extra features, tests
# proportionate to the change (cover the contract, not every permutation), prefer
# extending existing files over spawning new ones. Route authors: flag it on the profile;
# phase leaders: put it in the brief (see /herdr-phase).

worktree:
  branch_template: tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}

finalization_defaults:
  green_checks:
    - command: just test-meta-plan
  log_checks:
    - source: build, deploy, and runner logs
      fail_patterns: ERROR,FATAL,Traceback,uncaught
  # OPTIONAL lifecycle/escalation config (all four may be omitted):
  watchdog_profile: claude-low  # cheap profile for one phase watchdog per live leader.
                                # Absent ⇒ use that phase's lead profile.
  advisor_profile: claude-low   # a stronger profile the controller consults when a
                                # budget is exhausted. Absent ⇒ escalation goes straight
                                # to the human.
  phase_pass_budget: 3          # phase re-runs (passes) before escalating. Absent ⇒ 3.
  stage_try_budget: 3           # stage retries (tries) before escalating. Absent ⇒ 3.

phases:
  phase-0:
    accuracy: medium            # OPTIONAL. medium (default) = stages 1–5.
                                # high = add stage-0-alignment before stage-1 (see below).
    merge_back_at: stage-3-integration-acceptance-seams
    lead:
      llm_profile: claude-low
      agent: herdr-phase-leader  # Jiffy controller that runs /herdr-phase
    stages:
      # stage-0-alignment goes here ONLY when accuracy: high — e.g.:
      #   stage-0-alignment:
      #     llm_profile: pi-default        # must be independent from stage-1
      #     agent: aligner
      stage-1-implementation:
        llm_profile: claude-low
        agent: backend
      stage-2-adversarial-audit:
        llm_profile: pi-default
        agent: adversarial-evaluator
      stage-3-integration-acceptance-seams:
        llm_profile: claude-low
        agent: qa
      stage-4-upstream-dag-verification:
        llm_profile: pi-default
        agent: monorepo-pkgs
      stage-5-finalization:
        llm_profile: claude-low
        agent: qa
```

Tree view:

```text
route.yaml
├── llm_profiles
│   └── <profile>
│       ├── harness
│       ├── model
│       └── effort / advisor / permissions as needed
├── worktree
│   └── branch_template: tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}
├── finalization_defaults
│   ├── green_checks
│   ├── log_checks
│   ├── watchdog_profile       (optional; absent ⇒ phase lead profile)
│   ├── advisor_profile        (optional; absent ⇒ escalation → human)
│   ├── phase_pass_budget      (optional; absent ⇒ 3)
│   └── stage_try_budget       (optional; absent ⇒ 3)
└── phases
    └── phase-N
        ├── accuracy: medium | high            (optional; absent ⇒ medium)
        ├── merge_back_at: stage-3-integration-acceptance-seams | stage-4-upstream-dag-verification | stage-5-finalization
        ├── lead
        │   ├── llm_profile
        │   └── agent
        └── stages
            ├── stage-0-alignment              (required iff accuracy: high; forbidden otherwise)
            ├── stage-1-implementation
            ├── stage-2-adversarial-audit
            ├── stage-3-integration-acceptance-seams
            ├── stage-4-upstream-dag-verification
            └── stage-5-finalization
```

Each phase lead and each stage must name both:

```text
llm_profile: <profile from llm_profiles>
agent: <real configured agent/persona name>
```

For Jiffy, every `lead.agent` is `herdr-phase-leader`. That dedicated Claude agent runs the `/herdr-phase` controller protocol. `phase-evaluator` remains an optional evidence-only worker that can recommend a verdict to the leader; never use `phase-evaluator` as a lead.

`runner_adapters` are optional launch hints. They are not part of the required MVP runnable schema.

## Stages: five (medium) or six (high) per phase

Every route phase has the same five base stage ids:

1. `stage-1-implementation` — create/use the temporary worktree branch, write unit tests, and write the code.
2. `stage-2-adversarial-audit` — independent hostile audit of Stage 1 on the same temporary worktree branch, including a hard gate for unused intake / accepted-but-ignored inputs.
3. `stage-3-integration-acceptance-seams` — integration/acceptance/seam checks; merge here only when `merge_back_at` is this stage.
4. `stage-4-upstream-dag-verification` — dependent build/deploy/test verification; merge here only when `merge_back_at` is this stage.
5. `stage-5-finalization` — merge if not already merged, verify main, destroy the temporary worktree/branch, run green checks, inspect logs for hidden failures, and record evidence.

### Accuracy: `medium` (default) vs `high`

Each phase optionally sets `accuracy:`. Absent or `medium` runs the five base stages above — unchanged behavior. `high` additionally requires a pre-code alignment stage that runs **before** stage-1:

1. `stage-0-alignment` — a fresh worker does mini-research for this phase against the original research, drafts a mini-plan against the original plan, then an **independent** audit checks the mini-plan against the big plan. Misaligned ⇒ redo the mini-plan; unreconcilable ⇒ `BLOCKED` (escalates). It is versioned, never overwritten.

`stage-0-alignment` is **required iff** `accuracy: high` and **forbidden** otherwise. Its audit reviewer must be independent from `stage-1-implementation` (same rule as Stage 2 — by profile, agent, harness, model family, or persona).

Stage 2 must be independent from Stage 1 by profile, agent, harness, model family, or persona.

### Escalation budgets (optional)

`finalization_defaults.phase_pass_budget` / `stage_try_budget` bound how many times a phase re-runs (a **pass**, controller = the run loop) or a stage retries (a **try**, controller = the phase lead) before escalating. On budget exhaustion the controller consults `advisor_profile` when set; the advisor rules continue / keep-looping / stop-and-ask-human. With no `advisor_profile`, exhaustion escalates straight to the human. Absent budgets default to 3.

`merge_back_at` is required for every phase and must be one of:

- `stage-3-integration-acceptance-seams`
- `stage-4-upstream-dag-verification`
- `stage-5-finalization`

Runtime leads do not decide merge timing. If the plan is created interactively, the planner must ask the user when each phase merges. Non-interactive conversion defaults to `stage-3-integration-acceptance-seams` because integration/acceptance work often needs main/staging infrastructure.

## Worktree lifecycle

Temporary branch names use this convention unless a compatible template is explicitly provided:

```text
tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}
```

A compatible template must include `{date}`, `{repo}`, `{phase}`, and `{run_id}`. Stage 1 and Stage 2 always use the same temporary worktree branch.

Stage 5 always runs, even if the phase already merged at Stage 3 or Stage 4:

```text
stage-5-finalization
├── if not merged: merge now
├── if already merged: verify main contains the change
├── destroy temporary worktree and temporary branch
├── run green checks
├── inspect build/deploy/runner logs for hidden errors
└── write final evidence
```

If merge, checks, log review, or cleanup fails, the runner preserves evidence, keeps the temporary branch when needed, and reports `failed` or `blocked`. It must not silently clean up and claim success.

## Check/convert behavior

- `meta-plan:check <plan.md> [route.yaml]` reports whether the pair is runnable.
- `/meta-plan-check <plan.md> [route.yaml]` is the Claude Code equivalent.
- `meta-plan:convert <source.md> <plan-output.md> [route-output.yaml]` converts a loose plan into canonical starter files.
- `/meta-plan-convert <source.md> <plan-output.md> [route-output.yaml]` is the Claude Code equivalent.

Conversion preserves the source plan's intent. It must not invent model, harness, profile, agent choices, or finalization commands. If route information is missing, conversion writes explicit TODO values and the runnable check fails until a human fills them in. Non-interactive conversion still fills `merge_back_at: stage-3-integration-acceptance-seams` as the safe default.

## Herdr runner gate

Before execution, Herdr validates both files:

```text
Herdr checks plan.md + route.yaml first
```

Invalid input stops before execution and points the user to check/convert. Herdr does not silently auto-convert at runtime.
