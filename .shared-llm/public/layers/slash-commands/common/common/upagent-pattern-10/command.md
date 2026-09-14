# /upagent-pattern-10

Run an approved plan with one named YAML workflow per phase, through Pattern 1: the human-facing HIL relays, a separate plan-implementer reads the workflows and hires UpAgent workers. Pattern 10 is Pattern 1 plus a `workflow.yaml`; it adds workflow selection, not a scheduler. For the same run without the agent in the middle, use `/upagent-pattern-20`.

```text
human <-> HIL (relay) <-> plan-implementer -> UpAgent workers
```

Pattern 10 is bounded by the plan-implementer's context window: nobody can compact that pane. Prefer Pattern 20 for a long plan.

## Invocation

```text
/upagent-pattern-10 --plan <plan.md> --workflow <workflow.yaml> --offering <implementer-id> --effort <effort> [--no-supervise]
```

Use Claude Code in a human-started Herdr pane, as `/hil` requires. `--offering` and `--effort` select the existing plan-implementer, not workers. All four values are explicit. Missing values stop preparation; ask the human rather than guessing.

## The workflow pool

The default pool is `workflows/` beside this installed `SKILL.md`. An explicit `pool` in the assignment replaces it; there is no overlay or fallback. A workflow is one `<name>.yaml` file in the pool; a name is a filename stem, not a path. New workflows in a pool need no registry or code change. `controller-requirements.md` in the pool binds every workflow.

A workflow is an ordered list of stages:

```yaml
workflow: phase-high-Luna
kind: phase              # phase | validation | finalize | controller
tier: high
retries: 5               # coder -> reviewers -> checks loops, then stop and ask the human

stages:
  - role: coder          # coder | reviewer | auditor | checks
    offering: pi-gpt-5-6-luna
    effort: max
  - role: reviewer
    offering: pi-gpt-5-6-sol
    effort: high
    adversarial: true
  - role: checks
    workflow: validation # the assignment's validation workflow

pass: every reviewer, checks and auditor stage passes on the same candidate
controller_requirements: controller-requirements.md
notes: |
  Prose the list cannot carry.
```

Run the stages top to bottom. Findings from any reviewer, auditor or checks stage go back to the same coder, and that is one loop. Stop after `retries` loops and ask the human; never add a silent extra hire. A stage with its own `retries` keeps its own counter. A second reviewer is one more list item. `offering` and `effort` must exist in the current UpAgent listings; validate before hiring and never substitute.

## The assignment

```yaml
phases:
  phase-1: phase-low-Luna
  phase-2: phase-medium-Luna
  phase-3: phase-high-Sol
validation: validate-Composer
finalize: finalize-adversarial-Fable        # or a list of two; both must pass, in list order
controller: delegated-implementer
# pool: ./workflows                         # optional human-owned pool
```

Phase identifiers must match the actual plan. For a single-scope plan, replace `phases` with `plan_flow: <name>`; supply exactly one of the two. Phased plans require `finalize`; single-scope plans need it only when the approved plan requires it. `validation` names the workflow every `checks` stage runs. `controller` names the controller workflow.

Before launching, verify:
- Every actual phase is assigned once, with no extra phase, duplicate YAML key, unknown assignment key, unreadable workflow or ambiguous phase identifier. Parse YAML safely and reject duplicate keys; do not evaluate tags or embedded commands.
- The plan is approved. The selected workflows fit its scope, dependencies and acceptance criteria. Workflows cannot waive higher-priority rules or human gates.
- Every offering and effort in every selected workflow exists in the current UpAgent listings. No substitutions. Settle any open choice with the human and record the answer: these are run decisions, not template variables.
- The write checkout or worktree, and any earlier merge required for testing, are explicitly authorized. Do not launch while any required choice is unresolved.

## Prepare one packet, then use Pattern 1

Create a fresh `<original-plan-dir>/pattern-10/<run-id>/plan.md`; never modify the approved original. This is an execution packet, not a new plan. Begin with the exact marker `<!-- upagent-pattern-10 -->` and include:
1. Absolute original plan path and original working directory. Preserve the original plan verbatim and resolve its relative references against the original location, not the packet directory.
2. The assignment verbatim, the resolved pool path and the recorded run decisions.
3. Complete copies of every selected workflow and of `controller-requirements.md`, each labelled with its name and source path, each copied once. Keep these frozen for the run.
4. This controller instruction: **Follow the assigned workflow for each phase, stage by stage. Delegate all production coding, fixes, validation and independent reviews through UpAgent. Inspect evidence yourself; never use the ordinary Pattern 1 exception to code locally. Only one intermediate controller is allowed.**

Keep private plans and packets in the destination, not this public kit. On resume, use the existing packet, receipts and counters; do not rebuild the packet or re-resolve a changed pool silently.

Read `/hil` and follow its existing preflight, start, relay, await and finish procedure with this packet as `--plan`, forwarding the human's literal `--offering`, `--effort` and optional `--no-supervise`. `/hil` and its Python launcher already freeze the plan and start `/plan-implementer`; add no launch flags or runtime code. Once launched, remain a relay. Do not start phase leaders, a `/tui-control` pane or native subagents.

Done means the implementer receipt reports the outcome, backed by every assigned workflow's stages and every assigned finalize workflow on the combined candidate. A merged candidate or a worker saying "done" is not final acceptance.
