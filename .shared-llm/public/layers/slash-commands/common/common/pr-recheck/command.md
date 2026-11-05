# Recheck a PR

Reassess prior findings against a new pinned snapshot using the appended contracts. A follow-up diff is evidence, not a substitute for the full current scope.

Example: `/pr-recheck repo=. pr=42 previous=prior-report.md base=<current-base-sha> head=<new-head-sha> scope="all prior findings"`

## Procedure

1. Recover the prior repository, PR/candidate identity, base, head, merge base, scope, criterion/finding IDs, and evidence. Verify old commits and comparison relationships without filling gaps from current refs. Reject cross-repository/PR matches; if old identity or evidence is unavailable, explain what cannot be rechecked. A fresh current review needs explicit scope.
2. Establish the new shared snapshot and compare identities. Inspect both prior-head-to-new-head and full current merge-base-to-head diffs, relevant complete files, and callers. Document rewritten history and use verified comparisons rather than assuming an incremental commit range proves coverage.
3. If base or merge base changed, identify affected integration/compatibility requiring fresh review. Perform that review only within requested scope; otherwise record it UNVERIFIED. Keep the previous report as historical evidence, not a transferable current pass.
4. For every inherited finding, follow the original trigger and claimed correction through the current path and seek counterevidence. Compare the original behavioral criterion, not just deleted/moved lines, renamed tests, or unrelated green jobs. Use a focused same-test regression/control only under separate authorization and the shared isolation rules.
5. Assign each retained finding ID a contract-defined FIXED, OPEN, UNVERIFIED, or SUPERSEDED disposition with current evidence and any linked successor/decision. Do not omit inconvenient findings or call missing original evidence a fix. Assess criterion statuses independently; dispositions do not determine all related checks.
6. Add new defects with new stable `PR-RECHECK-001`, etc. IDs, preserving inherited IDs, source provenance, and dissent during deduplication. Identify newly affected scope that needs review instead of implying prior findings cover the whole PR.
7. Return the shared report and prior-finding disposition table within the allowed schema. Include old/new identities, both comparisons, base changes, fresh-review coverage, new findings, and gaps. Re-establish current hosted readiness independently only if requested; historical checks and approvals do not transfer.
