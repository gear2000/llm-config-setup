# /do-convert-pattern-0

Prepare a plan for Pattern 0 by separating the work from its workflow assignments. This is a Pi planning command, not an execution command. `/do-convert --herdr` remains the separate phase-leader converter.

## Invocation

```text
/do-convert-pattern-0 --plan <source-plan.md> [--out <conversion-dir>] [--pool <workflow-dir>] [--non-interactive]
```

Accept a Markdown plan. If the conversation identifies exactly one source plan, use it and state its path; otherwise ask. Default output is `pattern-0-conversion/` beside the source. Keep private plans in their destination repository, never in the public kit.

Read `../upagent-pattern-0/SKILL.md` relative to this installed skill directory before preparing assignments. Its assignment and launch contracts are authoritative. Use its installed `workflows/` directory unless `--pool` explicitly replaces it. List the pool and read each selected document completely. Missing documents block conversion; never substitute a guessed workflow. Do not copy the phase-leader plan schema or create `route.yaml`.

## Separate and grade the work

1. Read the complete source plan, referenced decisions, acceptance criteria and repository rules. Record the source path, content digest and approval evidence. Preserve the source unchanged. A draft stays a draft. Unknown approval is not approval.
2. Keep useful existing phase boundaries. Split oversized phases only where each result is independently reviewable and has checkable completion criteria. Preserve dependencies and serial/parallel restrictions. Do not split by package alone, separate a necessary trust check from the feature it protects, or manufacture easy phases. Record every original requirement's destination in a coverage map.
3. Give each phase a stable identifier, deliverable, scope, dependencies and `Done:` criteria. Keep product settings, API contracts and required verification with the work. Move execution-model choices, reviewer/retry choices and delivery procedure into the conversion review. Preserve every required build, merge, deployment and acceptance gate there verbatim or by an exact source reference. A work-only plan is not permission to omit those gates.
4. Grade each phase with a reason: **easy** for a bounded change following an established contract; **medium** for several understood changes with contained integration; **hard** for trust boundaries, durable state/concurrency, cross-service failure recovery or substantial unresolved design. Difficulty is not the old route accuracy setting. An unresolved design remains blocking regardless of its grade.
5. Recommend documents from the actual pool. Bundled choices are `phase-low` for easy, `phase-medium-sonnet` or `phase-medium-chatgpt-5.5` for medium, and `phase-high-Astra` or `phase-high-Fable` for hard. These are recommendations, not a closed registry or automatic model selection. Custom pools may use different names. Obtain human agreement on exact documents, especially medium/hard variants. Read their requirements rather than duplicating model rosters here.
6. Preserve separate validation, final combined-plan review and controller selections using the Pattern 0 contract. Capture each selected document's required run decisions, including reviewer effort, validation runner, retry bounds, final reviewer(s)/ordering and controller offering/effort. Verify required personas and offerings through existing listings only. Do not hire workers to test availability or invent defaults. Higher-priority repository rules still apply; report incompatible workflow requirements rather than overriding either silently.

## Outputs and readiness

```text
conversion directory
├── plan.md                 Work, dependencies, Done criteria, approval status
├── workflow.draft.yaml     Proposed assignments while any gate remains open
├── conversion-review.md    Grades, coverage map, delivery gates and open choices
└── conversion-receipt.json Source identity, status and validation evidence
```

The receipt records source path/digest, source approval evidence, converted-plan approval evidence, resolved pool path, selected document paths/digests, status, blockers and checks actually performed. Do not record a successful check that was not run. Keep grades and readiness metadata in the review/receipt, not extra keys in the assignment file.

The assignment file uses only the existing Pattern 0 keys: `phases` or `plan_flow`, `validation`, `finalize`, `controller`, optional `pool`. Its pool path must remain resolvable from the output directory. For a draft, use null only for genuinely unselected assignments and explain each in the review. Null is not runnable. For selected assignments, use one document filename stem, never inline arguments.

Use `workflow.yaml` only after all readiness gates pass. These gates include approved source scope, human approval of the converted phase split and assignments, no unresolved design, every required run decision settled, and verified document/persona/offering availability. Receipt status is `ready-for-handoff`, not executed or accepted implementation. A source approval alone does not approve a changed phase split.

Before that, keep `Status: DRAFT. Not approved for implementation.` at the top of the converted plan. Return `DESIGN_REQUIRED` for unresolved product/architecture decisions; use `draft` for missing approval or execution choices only. List blockers plainly. In `--non-interactive` mode, write the non-runnable draft and stop rather than guessing or asking questions.

## Review and validation

Validate the generated assignment with a safe YAML parser that rejects duplicate keys and unsafe tags. Check every actual phase is assigned exactly once, no extra phases, exactly one of `phases`/`plan_flow`, known top-level keys and readable pool documents. For a draft, report null assignments as unresolved, not a passing runnable validation. Check every original acceptance criterion, dependency and delivery gate against the coverage map. Require explicit human approval of any material change.

Show the phase split, difficulty reasons, proposed document assignments and remaining choices. Review one decision at a time in the user's preferred format; create an annotatable page if requested. Do not promote draft output until approval and all other gates are recorded.

Conversion is idempotent: identical source, pool contents and human decisions produce identical plan, assignment and receipt content. Preserve prior revisions when inputs change; never overwrite approved or frozen artifacts. If an output directory already contains a ready assignment and inputs change, use a new revision directory rather than leaving a stale `workflow.yaml` next to a changed draft.

## Handoff boundary

Report output paths, readiness and the next unresolved decision. When ready, tell the human that `/upagent-pattern-0` is the separate execution entry point in a human-started Claude Code HIL session. That command must recheck its own preflight and freeze its execution packet.

Do not invoke `/upagent-pattern-0`, `/hil`, `/plan-implementer`, `/tui-control`, `just run-start`, phase leaders or execution workers. Do not implement, build, deploy or start the converted plan. Conversion never grants execution authority.
