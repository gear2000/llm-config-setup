# PR-only skills and reviewers

Use this toolkit for a pull request or an explicitly requested, committed PR candidate. It adds focused prompts, not a review service, mandatory pipeline, or automatic merge step. Existing general reviewers, the Pi `pr-reviewer`, and your `pr-risk` workflow keep their current contracts.

## Choose a task

| Task | Skill or agent | Result |
| --- | --- | --- |
| Draft a PR from the final committed diff | `/pr-prepare` | Proposed title and body, with checked claims and explicit gaps |
| Choose focused reviews or combine supplied reports | `/pr-review` | Recommended reviewers or a same-snapshot combined report |
| Check whether earlier findings were fixed | `/pr-recheck` | Stable finding IDs with new evidence and dispositions |
| Inspect security consequences | `pr-security` | Trust-boundary traces, exploit preconditions, and evidence |
| Inspect regression proof | `pr-tests` | Behavior-to-assertion map and fixed/unfixed comparison when authorized |
| Inspect compatibility | `pr-compatibility` | Supported old/new combinations, migration, and rollback checks |
| Inspect current hosted prerequisites | `pr-readiness` | Expected checks and approvals tied to the current PR snapshot |

The skills do not spawn these agents. The caller selects and dispatches specialists through its existing tools. If dispatch is unavailable, `/pr-review` returns the proposed selection and missing reports. It must not pretend that several sections written by one agent are independent reviews.

## Quick start

These are natural-language skill invocations, not commands for a new CLI parser. Replace the example PR number and supply repository context.

```text
/pr-prepare PR #42. Draft only. Verify title/body claims against its pinned diff.

/pr-review PR #42. Recommend focused reviewers. Do not dispatch them.

/pr-review PR #42. Combine the supplied security and test reports.
Check their repository, base, head, and merge-base identities first.

/pr-recheck PR #42 against the supplied prior report.
Preserve finding IDs and check the new head. Do not implement fixes.
```

A specialist handoff needs the repository identity and path, PR identity or explicit prepublication candidate, immutable base/head/merge-base commits, scope and criteria, evidence sources, probe permissions, and report destination. The prompt can resolve refs to commits, but must stop on missing or ambiguous revisions. Tests are not authorized merely because a handoff requests review.

Uncommitted work belongs with `code-review` or `reviewer` using a captured working-tree snapshot. This toolkit must not silently review committed HEAD instead.

## Three shared contracts

The source is [`_pr-review-contract.md`](../.shared-llm/public/layers/agents/common/_pr-review-contract.md). Each of the seven recipes includes it in the generated prompt, followed by the shared evidence rules and MIT notice. There is no source-path dependency at runtime.

1. **Input contract.** Record repository and PR identity, immutable base/head/merge-base commits, requested scope, and evidence permissions. Read revision-qualified content. Keep remote observations timestamped. Do not combine reports from different snapshots.
2. **Authority contract.** Review and report only. No repository edits, worker spawning, commits, publication, remote approvals, or merges. Tests and probes require explicit authorization and disposable targets. The narrow negative-control exception permits the same test/harness files in isolated fixed and unfixed copies, not changes to application code.
3. **Report contract.** The default `PR report v1` has Snapshot, Verdict, Checks, Findings, and Limits sections. Criteria use PASS, FAIL, or UNVERIFIED. Overall verdicts retain APPROVED, APPROVED WITH NITS, and CHANGES REQUESTED. Important missing verification blocks an unqualified approval without becoming a fabricated defect.

These are instruction contracts, not executable access controls or a new JSON API. A stricter caller schema retains its existing shape. These prompts do not change the standalone Pi review hub or its verdict parser. Their default report is not a drop-in replacement for a typed worker receipt.

## Evidence and follow-up

A source inspection, an executed test, and a supplied result are different evidence types. Reports state which one supports each criterion. A green subset or an empty findings list cannot establish full coverage.

Rechecks preserve the original finding IDs. FIXED, OPEN, UNVERIFIED, and SUPERSEDED describe finding disposition, not test status. A removed line is not enough to prove a fix. Changed base commits or unrelated rewritten history can require a fresh review. New defects receive new IDs rather than replacing old findings.

Readiness is separate from code quality. It requires authoritative expected-check policy, trusted check producers, accepted commit relationships, and current hosted review state. Offline or prepublication review leaves current hosted readiness UNVERIFIED. A remote head or base change makes an earlier readiness assessment stale. Even a current APPROVED assessment grants no permission to merge.

## Packaging and verification

Sources live in `.shared-llm/public/layers/agents/common/` and `.shared-llm/public/layers/slash-commands/common/common/`. Recipes live under the matching `compose/agents/` and `compose/slash-commands/common/common/` trees. Use the kit's normal update process when you choose to deploy them; local composition alone does not install them globally.

The tests in `tools/test_review_prompt_composition.py` compose the real recipes, verify shared-contract and license inclusion, check description limits, and exercise public copy plus destination composition twice in a temporary environment. Existing standalone Pi verdict checks remain in place.

```sh
python3 -m pytest tools/test_review_prompt_composition.py tools/test_compose_layers.py tools/test_descriptions.py -q
```

These tests catch packaging errors and instruction changes. They do not prove that models follow the prompts or improve review quality. Use the [controlled evaluation procedure](../.shared-llm/public/layers/slash-commands/common/common/writing-for-agents/resources/references/evaluation.md) for behavioral comparisons.

The focused procedures build on the adaptations documented in [Review procedures](REVIEW-PROCEDURES.md). Their composed outputs retain the upstream MIT notice.
