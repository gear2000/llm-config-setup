# Evidence-based reviews and checks

These procedures strengthen the existing agents and skills. They do not add a review orchestrator, change approval ownership, or grant permission to publish, merge, or repair application code during review.

For dedicated PR skills and reviewers, see the [PR-only toolkit](PR-TOOLKIT.md). It adds opt-in preparation, report consolidation, finding rechecks, and focused specialists under a shared input, authority, and report contract. The existing roles below keep their current contracts.

## Choose the existing role

| Task | Agent or skill | Boundary |
| --- | --- | --- |
| Check a proposed design | `plan-adversary` | Read-only plan walkthrough, not runtime verification |
| Review a PR's structure and contracts | `architecture` in review mode | No scaffolding or implementation |
| Review a PR's trust and access boundaries | `security` in review mode | Local authorized probes only; no repairs |
| Review correctness and regression coverage | `code-review` | Read-only; use existing automated verdicts when dispatched by a loop |
| Review browser behavior | `playwright-cli` in review mode | Browser-driven checks; no application or test edits in review mode |
| Combine supplied reviews | `reviewer` | No worker spawning; preserve each report's scope, evidence, and disagreements |
| Review repository state in Pi | `pr-reviewer` | Pinned revision; same existing verdict format |
| Validate an implementation | `qa` agent | Test-only edits; application and environment faults go back to the owner |
| Refactor without changing behavior | `/refactor-safe` | Characterization before edits; no automatic commit |
| Find and confirm bugs | `/audit-bugs` | Report-only; issue publication needs separate authority |
| Report an interactive QA issue | `/qa` skill | Check duplicates and publication authority before filing |
| Compare instruction variants | `/writing-for-agents`, evaluation reference | Controlled experiment; no invented performance claims |

For PR reviews, the caller selects the reviewers and passes the same resolved base and head commit IDs to each. Reviewers inspect files at those revisions, not whichever working-tree version happens to be present. Generic reviewers also accept explicitly requested working-tree or supplied-file reviews; they record the base revision and a diff/file snapshot, including relevant untracked files. Do not combine those results with reports for a different snapshot. The caller still owns dispatch and any approval decision. A combined report must not turn a majority vote or a missing specialist result into approval.

## Evidence is separate from the verdict

For each in-scope criterion, record its ID, revision, check, observation, and evidence status:

- **PASS**: the check ran or the named inspection was completed, and the evidence supports that criterion.
- **FAIL**: the evidence demonstrates a violated requirement or contract.
- **UNVERIFIED**: the check could not complete, was not run, or lacks sufficient evidence. State why and what would resolve it.

A source inspection is not an executed test. A design walkthrough is not an implemented feature. A reported result that the reviewer has not rerun must be labelled as supplied evidence. An important check required at the current stage and left UNVERIFIED prevents unqualified approval; it does not become a confirmed code defect. Future implementation tests do not block plan approval merely because implementation has not started.

Keep strict automated report schemas unchanged. Put this information inside the caller's permitted evidence, issue, summary, or notes fields. Do not add a new outer verdict or a second report block.

## Tests and probes

For a regression fix, compare the principal test against the unfixed and fixed revisions using the same relevant test and inputs. The unfixed run must fail for the intended behavior, not a setup or import error. The fixed run must pass. If a reviewer cannot make that comparison safely within its permissions, it reports UNVERIFIED.

Use explicitly authorized disposable workspaces, storage, and accounts. Verify targets before running commands. Tests can write data even when the agent is reviewing read-only. Never use the user's working tree or index to assemble a negative control. Never allow a missing test configuration to select a real home database or live service.

Browser checks include failure states and recovery: invalid input, repeated submission, back/refresh, retained data, visible errors, keyboard use, and narrow layouts. Preserve screenshots or traces only after removing secrets and private data.

## Planning and publication

Planning keeps stable requirement, invariant, and acceptance IDs. Link to their definitions instead of copying them into several documents. Specify future verification in the plan; mark implementation evidence UNVERIFIED until checked. Existing plan directories, approval gates, and typed receipts stay unchanged.

The GitHub agent checks descriptions against the final diff and re-reads the published title and body after an authorized update. CI verification first establishes which checks are expected, then verifies their results against the pushed revision. An empty check list is not success. Publication, verification, and merge authorization remain separate facts.

## Upstream adaptations and licenses

Only selected checks and procedures were adapted. The upstream factory workflows, JSON contracts, hardcoded paths, automatic commits, and merge policies were not imported.

| Upstream source | Adapted into |
| --- | --- |
| Blueprint `skills/architecture-review` | Plan walkthrough and focused architecture review |
| Blueprint `skills/test` and Software Factory `src/roles/reviewer.md` | Regression comparison and malformed-input verification |
| Blueprint `skills/improve` | `refactor-safe` |
| Blueprint `skills/requirements`, `skills/spec`, `skills/test` | Stable acceptance IDs and evidence tracking |
| Machinist `examples/prompts/audit.md` | `audit-bugs` |
| Machinist PR-quality and task-to-PR prompts | GitHub description and expected-CI checks |
| Software Factory `src/roles/security-reviewer.md`, `ux-reviewer.md`, `triage.md` | Security triggers, browser failure cases, duplicate checks |
| Software Factory `evals/README.md` and tasker/CRM results | Controlled prompt evaluation reference |

Reviewed upstream revisions:

- [Blueprint](https://github.com/owainlewis/blueprint/tree/fb5a850965039d434343fa1b74f8c7f47eb2bd96)
- [Machinist](https://github.com/owainlewis/machinist/tree/4d7d77664b5dcf93b6d0b034f7dfac02e42e11c8)
- [Software Factory](https://github.com/owainlewis/software-factory/tree/f205cd7b4576260d49f140059d8d3540282666c9)

All three carry the MIT license, Copyright (c) 2026 Owain Lewis. The full notice is in [the shared license layer](../.shared-llm/public/layers/agents/common/_owainlewis-license.md). Recipes include it in adapted composed outputs. The standalone Pi reviewer and bundled evaluation reference also retain the full notice.

## Local verification

`tools/test_review_prompt_composition.py` composes actual recipes into temporary directories. It checks evidence-layer inclusion, license retention, reference packaging, stable output, the existing PR verdict format, and the QA application-edit prohibition. Run it with the existing composer and description tests:

```sh
python3 -m pytest tools/test_review_prompt_composition.py tools/test_compose_layers.py tools/test_descriptions.py -q
```

These checks prove packaging and instruction contracts, not improved model behavior. Use the evaluation reference to measure behavior on frozen tasks before making comparative quality claims. Local composition is not a deployment to configured repositories or home agents.
