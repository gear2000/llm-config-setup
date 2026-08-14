# UpAgent follow-ups

Deferred work that has a decided shape but is intentionally not being built yet. Each
entry says what it is, why it was deferred, and what evidence would reopen it.

## Live HOLD — Sentinel-brokered parked state (deferred 2026-08-14)

**What.** After the Sentinel's landing dialogue, a worker that reports done-but-blocked
(or done, with the requester likely to have follow-ups) could be parked instead of torn
down: the Sentinel writes a typed `HOLDING` state to the ledger with a TTL, tells the
requester the worker is up and what it is blocked on, and the requester talks to the
worker directly. Follow-up work is appended to the ledger as an addendum to the order so
the final bundle still describes everything the worker did. `closeout.json` remains the
only teardown trigger; the TTL guarantees a hold can never become an immortal pane.
Scope rule: follow-ups that clarify, unblock, or finish the original request are
addenda; a genuinely new task goes through the front door as a new request.

**Why deferred.** Fresh-workers-always is the stronger default: keeping warm panes alive
is an optimization, not a reliability fix, and it cuts against the one-order-one-result
contract the audit trail depends on. The cheap 80% shipped instead: the closeout schema
carries a `blocking_question` field, so a done-but-blocked worker's question survives
into the retry worker's brief (a cold handoff).

**Reopen when.** Ledger evidence shows blocked retries that still failed even though the
retry brief carried the `blocking_question` answer forward — i.e. cold handoffs are
demonstrably losing context that only a live worker held.
