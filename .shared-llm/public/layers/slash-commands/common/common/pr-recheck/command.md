# Recheck a PR

Reassess previous findings against a newly pinned PR snapshot using the appended PR contract. A follow-up diff is useful evidence, not a substitute for the full current review scope.

## Invocation examples

- `/pr-recheck repo=. pr=42 previous=<report-path> base=<current-base-sha> head=<new-head-sha> scope="recheck all prior findings"`
- `/pr-recheck repo=. target=main previous=<candidate-report> base=<current-base-sha> head=<new-head-sha> scope="candidate regression and compatibility"` — keep the candidate label and current hosted readiness UNVERIFIED.

## Procedure

1. Recover the previous repository, PR or candidate identity, base, head, merge base, scope, criterion IDs, finding IDs, and supporting evidence. Verify those commit IDs and comparison relationships; do not silently fill missing old identity from current refs. Reject cross-repository or cross-PR matches. If the prior snapshot or evidence is unavailable, identify the gap and do not claim its findings fixed; a fresh current-snapshot review requires explicit scope.
2. Pin the new base, head, and merge base under the shared contract. Explicitly compare old and new identities. Inspect both **prior head to new head** and the **full current merge-base-to-head diff**, including relevant complete files and callers. If history was rewritten, document it and use verified commit comparisons rather than assuming an incremental commit range proves coverage.
3. If base or merge base changed, require fresh review of affected integration and compatibility scope; old findings and passing checks do not automatically transfer. Identify what must be re-reviewed and perform it only within the requested scope, otherwise mark it UNVERIFIED. Preserve the earlier report as historical evidence, not current proof.
4. For each prior finding, inspect the claimed correction and original trigger through the current path. A deleted or moved line, renamed test, green unrelated job, or owner assertion is not proof of correction. Seek the original behavioral criterion and counterevidence; run the same focused regression/control only if separately authorized under the shared isolation contract. Missing old reproduction or contract evidence cannot justify FIXED.
5. Retain every inherited finding ID and assign a disposition with evidence: **FIXED** — the original defect is demonstrably corrected for this snapshot; **OPEN** — the defect remains demonstrated; **UNVERIFIED** — correction or continued applicability lacks proof; **SUPERSEDED** — a documented replacement or changed requirement makes this finding obsolete, with a linked successor/decision and reason, not an assertion of a fix. Do not use SUPERSEDED merely because a line disappeared.
6. Keep these dispositions separate from each criterion's PASS/FAIL/UNVERIFIED and the overall verdict. A FIXED finding does not prove all related criteria passed. Append new findings with new stable `PR-RECHECK-001`, etc. IDs; never recycle or renumber previous IDs. Preserve dissent and all source IDs when deduplicating.
7. Return the shared report and a prior-finding disposition table (inside allowed fields for a strict schema). Include old/new snapshot identities, both diff comparisons, changes in base, fresh-review coverage, new findings, and remaining gaps. Re-establish current hosted readiness independently if requested; past approvals or checks do not transfer automatically.
