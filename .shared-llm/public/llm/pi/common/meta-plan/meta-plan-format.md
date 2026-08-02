# Meta plan runnable input format

This format defines the runnable `plan.md` + `route.yaml` input that planners prepare for Herdr. The TUI controller is the sole active runner; Herdr supplies its pane transport. Do not invent a runner-specific plan shape.

A runnable meta job is **two files**:

```text
runnable-meta-job/
├── plan.md       # the work
└── route.yaml    # who runs it, when it merges, and how finalization proves green
```

The runner only starts when both files pass validation. The run controller automatically assigns a 3-hour worker timeout to `stage-1-implementation` and `stage-2-adversarial-audit`; an order may explicitly override it with `timeout_ms`. There is only ever one live route file for a run: `route.yaml`. Conversion may preserve non-runnable drafts for evidence, but `just run-start` consumes exactly `plan.md` + `route.yaml`.

```text
approved big plan
   │
   ▼
cc/do-convert --herdr
   │
   ├── plan.md
   └── route.yaml
          │
          ▼
internal runnable validation
   │
   ├── PASS → just run-start may start
   └── DESIGN_REQUIRED / FAIL → human fixes design, plan, or route first
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
`/cc-convert --herdr`, `/do-convert --herdr`, and `/tui-control` run before launching any worker) is the authority, and it is stricter
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
# Recommended default profiles: copy the job slots from route-defaults.yaml (same
# directory) into llm_profiles. The slots map jobs — implementer, auditor, second_auditor,
# judge, advisor — to editable harness/model/effort values. Nothing is hardwired in code;
# swap the values in that one file as models move.
#
# scope_leash (OPTIONAL, per profile): true means every stage brief routed to this profile
# MUST carry the scope-discipline block from scope-leash.md (same directory) verbatim —
# it reins in models that write far more code and tests than a stage needs. Route authors
# set the flag on the profile; phase leaders copy the block into the brief (see
# /phase-leader). The flag decides who gets the leash — the rule itself names no model.

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
                                # max = high + a second stage-2 auditor on a different
                                # harness or model (second_llm_profile, see below).
    # kind: iac                 # OPTIONAL. Marks a terraform layer phase: the same
                                # ladder with IaC meanings (see "IaC phases" below).
    # parallel_group: hotfix    # OPTIONAL escape hatch. Phases sharing a token may be
                                # started together; absent = strictly sequential.
    merge_back_at: stage-3-integration-acceptance-seams
    lead:
      llm_profile: claude-low
      agent: phase-leader  # the phase-leader controller that runs /phase-leader
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
        # second_llm_profile goes here ONLY when accuracy: max — a second independent
        # auditor on a different harness or model; both must clear the work:
        #   second_llm_profile: claude-low
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
        ├── accuracy: medium | high | max      (optional; absent ⇒ medium)
        ├── kind: iac                          (optional; marks a terraform layer phase)
        ├── parallel_group: <token>            (optional; phases sharing it may start together)
        ├── merge_back_at: stage-3-integration-acceptance-seams | stage-4-upstream-dag-verification | stage-5-finalization
        ├── lead
        │   ├── llm_profile
        │   └── agent
        └── stages
            ├── stage-0-alignment              (required iff accuracy: high or max; forbidden otherwise)
            ├── stage-1-implementation
            ├── stage-2-adversarial-audit      (+ second_llm_profile iff accuracy: max)
            ├── stage-3-integration-acceptance-seams
            ├── stage-4-upstream-dag-verification
            └── stage-5-finalization
```

Each phase lead and each stage must name both:

```text
llm_profile: <profile from llm_profiles>
agent: <real configured agent/persona name>
```

In a meta run, every `lead.agent` is `phase-leader`. That dedicated Claude agent runs the `/phase-leader` controller protocol. `phase-evaluator` remains an optional evidence-only worker that can recommend a verdict to the leader; never use `phase-evaluator` as a lead.

`runner_adapters` are optional launch hints. They are not part of the required MVP runnable schema.

## Stages: five (medium) or six (high) per phase

Every route phase has the same five base stage ids:

1. `stage-1-implementation` — LLM implementation plus focused local tests on the temporary worktree branch.
2. `stage-2-adversarial-audit` — independent semantic/adversarial audit of Stage 1 on the same temporary worktree branch, including a hard gate for unused intake / accepted-but-ignored inputs.
3. `stage-3-integration-acceptance-seams` — deterministic changed-scope local seam/contract checks; a verifier is hired only on failure or ambiguity; merge here only when `merge_back_at` is this stage.
4. `stage-4-upstream-dag-verification` — compatibility slot and merge point only for ordinary phases; it is not a per-phase shared-environment, deployment, CI, upstream-DAG, or global acceptance stage. Broad shared acceptance belongs at candidate level; real residual cross-slice production wiring is an explicit integration-construction phase.
5. `stage-5-finalization` — deterministic merge if not already merged, verify main, run exactly the effective route-owned `green_checks`, inspect logs for hidden failures, destroy the temporary worktree/branch, and record evidence. At plan/conversion time, when an explicit later candidate-level finalization/gate owns repository-wide test/lint/static-analysis, omit those generic commands from per-phase `green_checks`; otherwise retain the repository's normal green checks.

### Accuracy: `medium` (default) vs `high` vs `max`

Each phase optionally sets `accuracy:`. Absent or `medium` runs the five base stages above — unchanged behavior. `high` additionally requires a pre-code alignment stage that runs **before** stage-1:

1. `stage-0-alignment` — a fresh worker does mini-research for this phase against the original research, drafts a mini-plan against the original plan, then an **independent** audit checks the mini-plan against the big plan. Misaligned ⇒ redo the mini-plan; unreconcilable ⇒ `BLOCKED` (escalates). It is versioned, never overwritten.

`stage-0-alignment` is **required iff** `accuracy: high` or `max` and **forbidden** otherwise. Its audit reviewer must be independent from `stage-1-implementation` (same rule as Stage 2 — by profile, agent, harness, model family, or persona).

`max` keeps the full `high` ladder and doubles the stage-2 audit. The stage-2 route entry additionally names `second_llm_profile` — a second independent reviewer that must use a **different harness or model** than the primary auditor. Both reviewers must clear the work. When they disagree, the leader consults `finalization_defaults.advisor_profile` as the judge when set; with no advisor the phase goes to the human as `blocked`. `second_llm_profile` is required iff `accuracy: max` and forbidden otherwise.

Stage 2 must be independent from Stage 1 by profile, agent, harness, model family, or persona.

### IaC phases (`kind: iac`)

One terraform layer runs as one ordinary phase; the ladder is reused with IaC meanings. Stage-1 writes the terraform (stage workers stay plan-only: `fmt`, `validate`, `init`, `plan`, `show` — never apply; apply is gated on human approval and happens later, in stage-4, not here). Stage-2 is the adversarial review of the terraform, best on a different model family. Stage-3 runs init and plan, captures the plan output as evidence, and builds the approval table (`just iac-plan-table`; replace is broken out because it destroys and recreates). The leader then publishes a `decision-required` event tagged `iac-approval` and waits on durable answer files. The TUI shows the human the table verbatim plus `cd <absolute pass-dir>` and the exact command, collects the typed destroy total when it is above zero, writes the approval file recording the digest of what the human reviewed, then applies with a FRESH `tofu apply`/`terraform apply` run directly in that directory — never a saved plan file passed to apply — and writes the apply receipt (see /tui-control). Stage-4 is that approved apply, recorded from the receipt. Stage-5 finalizes as usual.

IaC layers run strictly in order because a later layer's plan is only truthful after the earlier layer's apply. `parallel_group` is the explicit per-run escape hatch for genuinely independent stacks (urgent fixes) — the human owns that risk.

**Choosing a gear is always the plan author's call — nothing escalates automatically.** As a rule of thumb only: `medium` is the everyday default; `high` earns its extra alignment stage on unfamiliar or intricate work; `max` is worth considering for auth, destructive infrastructure, migrations, cross-service contracts, or a phase already on its second failed pass.

### Escalation budgets (optional)

`finalization_defaults.phase_pass_budget` / `stage_try_budget` bound how many times a phase re-runs (a **pass**, controller = the run loop) or a stage retries (a **try**, controller = the phase lead) before escalating. On budget exhaustion the controller consults `advisor_profile` when set; the advisor rules continue / keep-looping / stop-and-ask-human. With no `advisor_profile`, exhaustion escalates straight to the human. Absent budgets default to 3.

`merge_back_at` is required for every phase and must be one of:

- `stage-3-integration-acceptance-seams`
- `stage-4-upstream-dag-verification`
- `stage-5-finalization`

Runtime leads do not decide merge timing. If the plan is created interactively, the planner must ask the user when each phase merges. Non-interactive conversion defaults to `stage-3-integration-acceptance-seams` because local seam/contract evidence is the earliest ordinary deterministic merge point.

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
├── run configured green checks
├── inspect build/deploy/runner logs for hidden errors
└── write final evidence
```

The leader always runs exactly the effective route-owned `green_checks`; it does not infer or branch on later candidate-level ownership. At plan/conversion time, when an explicit later candidate-level finalization/gate owns repository-wide test/lint/static-analysis, omit those generic commands from per-phase `green_checks`; otherwise retain the repository's normal green-check command so configured validation is not dropped.

If merge, checks, log review, or cleanup fails, the runner preserves evidence, keeps the temporary branch when needed, and reports `failed` or `blocked`. It must not silently clean up and claim success.

## Check/convert behavior

- `/cc-convert --herdr <approved-plan.md>` and `/do-convert --herdr <approved-plan.md>` convert an approved big plan into a runnable Herdr directory and validate it internally.
- `just run-start <converted-run-dir>` starts the checked run and revalidates before launching `/tui-control`.

Conversion preserves the source plan's intent. It must not invent model, harness, profile, agent choices, finalization commands, environment names, account details, URLs, credentials, or deployment gates. If route information is missing, conversion asks the human or returns a non-runnable result; it does not declare a TODO route runnable. Non-interactive conversion still fills `merge_back_at: stage-3-integration-acceptance-seams` as the safe default local seam/contract merge point.

If the approved plan calls for an external candidate gate such as an exact-SHA shared environment check and the public route inputs do not configure that gate, conversion records it as deferred `not-configured` evidence in the conversion receipt/review and stops short of inventing private infrastructure.

## Run controller gate

Before execution, the run controller validates both files:

```text
The run controller checks plan.md + route.yaml first
```

Invalid input stops before execution and points the user to check/convert. The run controller does not silently auto-convert at runtime.
