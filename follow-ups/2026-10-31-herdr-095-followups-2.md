# Herdr 0.9.5 go-live follow-ups, batch 2

Date: 2026-10-31. Found while live-checking batch 1 (PR #24) on `main` 151048f.
Same rule as batch 1: a repro test that fails on today's `main` first, then the fix; stop and
say so if an item cannot be reproduced.

## 6c. Stale expiry-index entries are still never pruned in production

- **Where:** batch 1 item 6 taught `JobLedger.reap_expired` to unlink stale
  `active/by-expiry/<epoch>/<key>-<token>.json` entries. But nothing outside the tests calls
  `reap_expired`, so on the live machine the index still only grows (266 entries, 243 epoch
  dirs, no matching active claim for almost all of them).
- **Reproduced?** Yes, live: count unchanged after PR #24 and a full set of hub commands.
- **Repro test (must fail today):** claim, finish (claim dir removed), then run
  `cmd_reconcile(force=False, emit=False)` (the opportunistic reconcile every client command
  runs); the index entry is still on disk.
- **Fix:** prune index entries whose claim is gone or whose lease token/expiry no longer match,
  on a path that runs in production (the claim-release path, or `cmd_reconcile`), under the
  claim lock. Pruning must not reclaim or requeue anything; it only deletes stale index files
  and empty epoch dirs. Keep entries for live claims.
- **Done when:** the test passes; live, one hub command on `main` brings the index down to the
  entries of active claims only.

## 10. A launch that fails before any pane split stays cleanup-pending forever

- **Where:** `recruiter.py` `_launch_managed_pane` (~4740) journals `split-then-start`, then
  `_start_herdr_agent` fails before splitting (here its anchor `pane get w2M:p1` returned
  `pane_not_found`). `_reconcile_exact_launch` will not accept "agent name not found" as
  proof of absence for `split-then-start` (~4657), because a split pane may exist before the
  agent is named. So the journal stays `cleanup-pending`, the claim stays active, and every
  hub command retries it.
- **Reproduced?** Yes, live: consult `consult-c7c84183272d0a4e37a6ae5a` (submitted 02:13 on
  2026-10-31) is still `claimed`, journal `worker-c916b9cdf6384c00852ee4a097d583dd`
  `cleanup-pending` with `worker_pane: None`, owner pid dead.
- **Repro test (must fail today):** make the anchor pane probe fail before the split; after
  `_launch_managed_pane` raises, the launch journal is `cleanup-pending` and the claim can never
  terminalize.
- **Fix:** record when the split is actually attempted (e.g. a callback or journal mark right
  before `pane split` is sent). A failure before that mark proves no pane was created: close the
  journal as verified absent with that reason. Keep today's caution for failures after the split
  was sent.
- **Existing stuck record:** its journal predates the mark. Decide whether reconcile can clear
  it from evidence it already holds, without guessing; if not, say how it should be cleared.
- **Done when:** the test passes, and the live stuck consult is terminal.

## Results (2026-10-31)

Every repro test was re-run by the lead against unmodified `main` 151048f and failed there;
all pass on the branch. `just test` on the branch: 2174 passed, all Node checks pass.

| Item | Commit | Repro test (red on main) | Live |
| --- | --- | --- | --- |
| 6c | 1669b5b | `recruiter_test::test_opportunistic_reconcile_prunes_stale_expiry_index_entries`, `::test_a_malformed_expiry_index_entry_never_breaks_the_reconcile` | checked on the main ledger after merge |
| 10 | 2da31c2 | `recruiter_test::test_a_launch_that_fails_before_the_split_closes_as_verified_absent`, `::test_reconcile_trusts_only_a_recorded_never_split_mark[False]` (`[True]`/`[None]` guard the kept caution) | the stuck consult predates the mark, so it was cleared by the operator procedure below |
| 10b | 0b7930b | `recruiter_test::test_an_intake_clerk_launch_that_fails_before_the_split_closes_as_absent`, `specialist_lifecycle_test::test_a_start_that_failed_before_the_split_never_blocks_the_next_up` (same bug in the intake clerk and the resident launch) | test only |

**Stuck consult cleared by an operator, not by code.** Its journal had no pane, no terminal id,
and no split mark, which is indistinguishable from a split whose reply was lost, so reconcile
must not guess. The operator confirmed: the request's `supervisor.log` shows the anchor
`pane get` failed with `pane_not_found` before any split; the owner pid was dead; no agent by
the launch's name existed; the session held only panes that pre-dated or post-dated the
launch. The journal was then closed with `JobLedger.mark_launch_closed(..., {"status":
"operator-verified-absent", "verified_absent": true, "reason": <the checks>})`, and the next
hub command terminalized the claim. Never hand-write `split_attempted: false` into an old
journal: that forges the evidence the new code trusts.

Left alone (low payoff): the startup-budget and PATH checks run after the split mark but before
the split is sent; the prune pass stops at the first malformed entry.
