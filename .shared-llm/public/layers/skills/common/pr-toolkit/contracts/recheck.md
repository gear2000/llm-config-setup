## Recheck contract

Applies to prior findings and affected current review scope. Prior/current repository and PR/candidate identities and immutable comparisons are evidenced; cross-repository or cross-PR reports cannot establish continuity. Nonapplicable follow-up checks require an explicit rationale, not silent omission of inherited findings.

- Evidence covers both prior-head-to-current-head changes and the full current merge-base-to-head scope, with relevant complete files/callers. Rewritten history or changed base/merge base is explicit; affected integration/compatibility needs fresh review rather than automatic transfer of old passes.
- Every inherited finding ID remains present with a disposition: **FIXED** means the original defect is demonstrably corrected; **OPEN** means it remains demonstrated; **UNVERIFIED** means correction or continued applicability lacks proof; **SUPERSEDED** means a documented replacement or changed requirement makes it obsolete, linked to its successor/decision and reason, not a claim of correction.
- Deleted/moved lines, renamed tests, unrelated green jobs, or owner assertions alone cannot establish FIXED or SUPERSEDED. Evidence addresses the original trigger and behavioral contract; missing prior reproduction/contract evidence cannot justify FIXED.
- Finding dispositions remain separate from criterion PASS/FAIL/UNVERIFIED and the overall verdict. FIXED does not establish all related criteria. New defects receive new IDs; inherited IDs, dissent, and provenance survive deduplication.
- Historical reports remain historical evidence. Unavailable prior snapshots limit recheck conclusions; a fresh review needs explicit scope. Prior approvals/checks do not establish current hosted readiness.
