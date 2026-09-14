# /upagent-pattern-0

Run an approved plan using separate workflow documents. Reuse Flow 1: the human-facing HIL relays; the plan-implementer reads the documents and hires UpAgent workers. Existing flows keep their behavior. This skill adds instructions, not a deterministic scheduler.

## Invocation

```text
/upagent-pattern-0 --plan <plan.md> --workflow <workflow.yaml> --offering <controller-id> --effort <effort> [--no-supervise]
```

Use Claude Code in a human-started Herdr pane, as `/hil` requires. `--offering` and `--effort` select the existing plan-implementer, not workers. All four values are explicit. Missing values stop preparation; ask the human rather than guessing.

## Select documents

Read the approved plan and assignment file. Resolve assignment-relative paths against the assignment file's directory. The default pool is `workflows/` beside this installed `SKILL.md`. An explicit `pool` directory replaces it; there is no overlay or fallback. List that directory and read each selected `<name>.md` completely. A name is one filename stem, not a path. New documents in the pool need no registry, recipe per document, or code change.

Example assignment (phase identifiers must match the actual plan):

```yaml
phases:
  phase-1: phase-low
  phase-2: phase-medium-sonnet
  phase-3: phase-high-Astra
validation: validate
finalize: finalize-adversarial
controller: delegated-implementer
```

For a single-scope plan, replace `phases` with `plan_flow: phase-low`. Supply exactly one of `phases` or `plan_flow`. Phased plans require `finalize`; single-scope plans need it only when the approved plan requires it. `validation` and `controller` explicitly name their documents. Optional `pool: ./workflows` selects a human-owned pool.

No arguments on document names. The five phase documents repeat their own procedure and requirements on purpose. Selection is by name, not a hardcoded list in this skill.

Before launching, verify:
- Every actual phase is assigned once, with no extra phase, duplicate YAML key, unknown assignment key, unreadable document or ambiguous phase identifier. Parse YAML safely and reject duplicate keys; do not evaluate tags or embedded commands.
- The plan is approved. The selected documents fit its scope, dependencies and acceptance criteria. Documents cannot waive higher-priority rules or human gates.
- Every required offering/effort/persona exists in the current UpAgent listings/agent definitions. No substitutions. Settle the documents' open choices with the human: reviewer effort, validation runner, retry bounds, final reviewer(s) and their ordering. Record answers explicitly; these are run decisions, not arguments or template variables.
- The write checkout/worktree and any earlier merge required for testing are explicitly authorized. Do not launch work while any required choice is unresolved.

## Prepare one packet, then use Flow 1

Create a fresh `<original-plan-dir>/pattern-0/<run-id>/plan.md`; never modify the approved original. This is an execution packet, not a newly designed plan. Begin with the exact marker `<!-- upagent-pattern-0 -->` and include:
1. Absolute original plan path and original working directory. Preserve the original plan verbatim and resolve its relative references against that original location, not the packet directory.
2. The assignment file verbatim, resolved pool path and the explicit run decisions above.
3. Complete copies of all selected documents, labelled with their names and source paths. Keep these frozen for the run. Repeat a selected document only once; every phase points to its name.
4. This controller instruction: **Follow the selected workflow for each assigned phase. Delegate all production coding, fixes, validation and independent reviews through UpAgent. Inspect evidence yourself; never use the ordinary Flow 1 exception to code locally. Only one intermediate controller is allowed.**

Keep private plans and execution packets in the destination, not this public kit. On resume, use the existing packet, receipt and counters; do not build a replacement or re-resolve a changed pool silently.

Read `/hil` and follow its existing preflight, start, relay, await and finish procedure with this packet as `--plan`, forwarding the human's literal `--offering`, `--effort` and optional `--no-supervise`. `/hil` and its Python launcher already freeze the plan and start `/plan-implementer`; add no new launch flags or runtime code. Once launched, remain a relay. Do not start phase leaders, a TUI controller or native subagents.

Done means the existing implementer receipt/result reports the outcome, backed by each assigned document's checks and required final audit(s). A merged candidate or a worker saying “done” is not final acceptance.
