# Review PR hosted readiness

Assess current hosted prerequisites through read-only evidence under the appended contracts. This is distinct from code quality or permission to merge.

Example: `/pr-readiness repo=. pr=42 scope="current target-branch checks and review gates"`

## Procedure

1. Establish the shared snapshot. Read authoritative repository/target-branch policy for expected workflows/check contexts, trusted producers, approvals, stale/dismissed-review rules, thread resolution, draft restrictions, conflicts, and queue requirements as applicable. Record policy source/time; inaccessible policy remains a gap, not an empty requirements list.
2. Map each expected check to its workflow identity/path, app/integration identity, run/job/check identifiers, target commit, observed conclusion, and time. Verify the producer rather than trusting a display name. Inspect existing results only; do not trigger workflows or jobs.
3. Bind each result to the pinned head or a policy-accepted synthetic merge. For the latter, trace provenance and constituent base/head commits and record why policy accepts the relationship. Distinguish PR-head, integration, and merge-queue checks; reject stale or unverifiable relationships as current proof.
4. Account for every expected check and preserve provider state separately from criterion status. Leave skipped/absent execution UNVERIFIED. Evaluate any documented snapshot-specific exemption as a separate gate-applicability criterion that may PASS; cite its policy/reason instead of calling a skipped test passed. Identify missing, pending, cancelled, failed, or unexplained outcomes.
5. Inspect current eligible approvals and changes requested, stale/dismissed decisions, unresolved threads, draft/conflict state, and queue state required by policy. Cite authoritative sources and snapshot relationships; distinguish unavailable/unknown state from passed gates. Do not substitute comments or historical review conclusions for current decisions.
6. Apply the shared remote base/head refresh before concluding current readiness. Record observation times and any staleness; for offline or prepublication candidates, report available pinned evidence with current hosted readiness UNVERIFIED.
7. Return the shared report and an expected-gate matrix: policy, trusted source identity, commit relationship, observed state, criterion status, and gap. Use `PR-READY-001`, etc. for new findings. Keep code-review conclusions separate and qualify the assessment to its observation time.
