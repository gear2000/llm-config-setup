# Follow-up: gate for artifactless `passed` review results (observed 2026-08-14)

**What happened.** During the Sentinel feature's own adversarial-review loop, a review
worker published `verdict: passed` with empty findings, no reason, no verdict document,
and **zero artifact files** — while its own full_log admitted a check was still
"Pending". The
identical false-verdict failure the feature exists to prevent, reproduced live by the
review layer. A retry with a hardened output contract ("a verdict without a written
account is invalid; incomplete review returns VEERED, never a bare passed") produced a
genuine evidence-backed review.

**The gap.** The new verdict/artifact consistency gate keys on non-empty artifacts
CONTRADICTING the verdict. A worker that writes no artifacts at all slips past it: no
artifacts, nothing to contradict. For review-shaped orders, `passed` + empty findings +
no artifact account is itself the suspicious shape.

**Proposed gate.** In the completion validator: when an order is review/evaluation-shaped
(or whenever a persona contract requires a verdict document), a `passed` result with
empty findings and no non-empty artifact account is invalid — force the same
re-evaluation path as the existing consistency gate. Interim mitigation already in use:
every review prompt carries the hardened output contract quoted above.

**Also observed, same day.** All three native (in-process) subagents finished their work
but skipped the final report until nudged — the finalization pattern the Sentinel's
LANDING dialogue addresses. The one brief that explicitly ordered "send your report
unprompted via SendMessage" got its report unprompted; hub workers under the pi harness
finalized correctly on their own. Keep the explicit report-back order in every native
subagent brief until the Sentinel machinery is deployed and covering them.
