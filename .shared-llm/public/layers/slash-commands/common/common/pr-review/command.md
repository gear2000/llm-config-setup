# Review a PR

Review the requested pinned PR scope and synthesize supplied same-snapshot evidence using the appended PR contract. This is a bounded report, not a new orchestration or risk-scoring workflow.

## Invocation examples

- `/pr-review repo=. pr=42 base=<base-sha> head=<head-sha> scope="authorization and API compatibility" reports=<security-report>,<compatibility-report>`
- `/pr-review repo=. base=origin/main head=feature/cache target=main scope="tests for committed candidate"` — resolve once and label prepublication; hosted readiness remains UNVERIFIED.

## Procedure

1. Pin repository, PR or candidate identity, base, head, and merge base. Establish accepted criteria and scope before inspecting the current full diff and relevant files. Record excluded scope and unavailable evidence. For a selection-only request, return bounded reviewer recommendations rather than performing a full review; label unperformed code and readiness checks UNVERIFIED. Any verdict in that report concerns selection only, not approval of the PR.
2. Suggest only relevant specialists for the caller to dispatch: `pr-security` for trust boundaries and sensitive sinks; `pr-tests` for behavior/assertions and regression evidence; `pr-compatibility` for consumer contracts and rollout; `pr-readiness` for current hosted gates. Give a concrete reason and bounded question for each suggestion. Do not spawn workers, prescribe a mandatory pipeline, or duplicate the caller's `pr-risk` analysis or score.
3. Consume supplied reports only after verifying their repository, PR/candidate identity, base, head, and merge-base match this SAME-SNAPSHOT review. Quarantine mismatched, ambiguous, or stale evidence in Limits; request a refreshed report instead of treating it as current proof. Record report source, role, author/reviewer identity, observation time, criterion IDs, and finding IDs. A supplied result is not a result you reran.
4. Assess the scoped code and evidence. Deduplicate findings by root cause, retaining every source ID, affected location, and provenance. Preserve unresolved dissent and missing checks instead of averaging verdicts or dropping an inconvenient report. If evidence conflicts, explain what would resolve it and mark the affected criterion UNVERIFIED until resolved.
5. Count independence honestly: your own repeated passes or role labels are not multiple independent reviews. Report distinct reviewer provenance where supplied; unavailable independence evidence remains UNVERIFIED. Specialist agreement does not fill an unreviewed area or satisfy unrelated required checks.
6. Use the shared report to state a single scoped verdict, criterion evidence, open findings, and limits. Use `PR-REVIEW-001`, etc. for new findings and retain inherited IDs. Explicitly separate **scoped code review** from **current hosted readiness**: only fresh, policy-grounded readiness evidence plus the shared remote snapshot refresh can support the latter. Otherwise leave readiness UNVERIFIED, especially for offline or branch-only candidates.

Return recommendations and missing evidence for the owner; do not publish reviews, grant remote approval, or merge.
