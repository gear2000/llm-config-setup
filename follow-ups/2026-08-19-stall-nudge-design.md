# UpAgent suggestion: sentinel stall-check + cross-provider nudge (shipped design)

Date: 2026-08-19. Author: Gary (design) / Claude Code lead (write-up).
Status: **shipped as a deliberately narrow increment**, after two adversarial review
rounds and a real-Herdr live fire drill.
Scope: WORKERS ONLY.

This document preserves the original proposal's structure, but describes the code that
actually shipped. **SHIPPED** and **DEFERRED** labels are normative. The implementation
intentionally did not add a supervision subsystem or a new public command. The later
Section 4 follow-up added the narrow provider metadata and Sentinel-role configuration
needed to make cross-provider supervision the default.

## Problem

Provider-side capacity errors (Anthropic 529 `overloaded_error`; the analogous OpenAI
overload responses) can leave a running worker halted after the harness exhausts its own
retries. The process and conversation remain available, but the worker does not resume
until the request times out or a human sends `continue`.

Two codes, same halt: **529 `overloaded_error`** = provider capacity (usually transient);
**429 `rate_limit_error`** = the caller's own quota ceiling.

The shipped increment does not attempt to infer those provider errors mechanically. It
acts only after the existing Sentinel publishes a corroborated `STALLED` closeout for a
provably live worker.

## Boundary (deliberate, non-negotiable)

**SHIPPED.** Workers only. The lead/TUI remains human-watched: no Sentinel, watchdog, or
auto-resume watches the lead, and the Sentinel never inspects lead liveness or reads the
lead pane.

Retained / `completion_policy=requester_release` / keep-open workers are excluded too.
Their idle checkpoint is designed, so only authenticated requester
`review-continue`/`review-release` operations may resume them.

## Design principle (revised)

**SHIPPED.** The Sentinel never types the automatic nudge. It remains an advisory
observer that can publish a `STALLED` closeout. Python owns the literal payload,
eligibility checks, attempt/generation fence, durable intent, delivery, backoff, cap,
completion supersede, and escalation records.

```text
  HUMAN ──watch──► LEAD (TUI)
                     │ delegate / await / may message the worker directly
                     ▼
               UPAGENT WORKER ◄── literal "continue" delivered by HUB (Python)
                     ▲                              ▲
                     │ observe                      │ corroborated STALLED closeout
                  SENTINEL (advisory only) ─────────┘
```

## 1. Stall detection - existing Sentinel evidence plus Python corroboration

**SHIPPED, NARROWER THAN THE ORIGINAL STATE-MACHINE PROPOSAL.** The implementation reuses
the existing Sentinel lifecycle rather than creating a second deterministic stall
classifier. The Sentinel observes its attempt's worker pane, git/fs deltas, wake file,
and dialogue. It may publish `STALLED` only after repeated quiet observations and an
unanswered status nudge.

Python then applies the existing closeout contract and corroboration rules:

- request id and order id must match the attempt;
- absolute-path citations must exist inside the request's own territory;
- a `STALLED` closeout with no corroborated citation is rejected once when Python can
  positively re-probe the worker as live;
- a replacement attempt, unknown/gone pane, missing started-worker journal, or unreadable
  lifecycle state never becomes permission to nudge;
- malformed durable nudge state emits `worker-nudge-state-invalid` and falls through to
  the exact pre-ladder blocked path.

The broad structured classifier proposed originally—provider-error events, monotonic
output cursors, foreground child/TTY modeling, and repeated Python-owned quiet-window
classification—did **not** ship. This increment relies on the already-shipped Sentinel
closeout plus Python's mechanical corroboration and delivery gates.

## 2. Nudge - a hub-owned, cheaply fenced operation

**SHIPPED.** The hub-owned path accepts exactly one payload: the literal `continue`.
There is no public nudge command and no general message vocabulary.

For each accepted `STALLED` closeout, Python:

1. Loads the newest started worker journal and requires its `attempt` and `generation` to
   equal this `_SentinelWatch`'s attempt and generation. This is the deliberately cheap
   attempt/generation fence; a replacement worker preserves the old pre-ladder behavior
   instead of receiving an old watch's nudge.
2. Requires the watched worker pane to be positively present and reads
   `state/latest.json` fail-closed. Requester-facing, release, finalizing, cancelling,
   finished, and cleanup-failed states reject delivery.
3. Validates the completion bundle before spending a rung. If it already validates,
   completion wins: the closeout becomes `closeout.stalled-superseded.json` and the hub
   emits `worker-nudge-superseded` instead of resuming a finished worker.
4. Persists a nudge intent before delivery in `nudges.json`, keyed by the digest of
   `(generation, attempt, nudge_index)`, and emits `worker-nudge-intent`.
5. Rechecks lifecycle state at delivery, then sends only through the registered Herdr
   agent-address prompt path. Cursor uses its existing split paste/settle/Enter adapter;
   no path writes raw terminal input directly.
6. Marks the durable intent delivered and emits `worker-nudge-delivered`, or spends the
   rung and emits `worker-nudge-failed` when the idle/presence/state gate refuses it.
7. Archives the provisional closeout as `closeout.stalled-nudged-N.json` and tells the
   Sentinel the actual disposition.

The persisted ladder is **immediate / 5 minutes / 15 minutes**, represented by backoff
seconds `(0, 300, 900)`, with a hard cap of **3**. A closeout received inside backoff is
archived uniquely and emits `worker-nudge-held`. This replaces the proposal's unshipped
5 / 15 / 45-minute ladder.

`NUDGE_PROMPT_IDLE_TIMEOUT_MS` is **10 seconds**, not the ordinary Sentinel prompt's
120-second idle wait. A truly stalled worker should already be idle; a busy worker fails
the rung rather than widening the gate-to-send interval. There remains an accepted
seconds-scale race between the final state/completion check and actual pane send. A
mutation-lock reservation and nudge-only pre-send callback inside the shared adapter were
judged disproportionate for this bounded improvement; the README states this residual
race rather than claiming a perfect serialization fence.

## 3. Resume safety - completion-gated, with broader policy deferred

**SHIPPED:** completion supersede is the concrete safety gate. A bundle that validates
when the stall is consumed wins before intent or delivery, so the hub does not knowingly
resume an already-finished worker. Attempt/generation matching also prevents an old
watch from nudging a replacement worker.

**DEFERRED:** the original per-order resume-safety policy is not part of the shipped
request schema. Orders do not declare mutation receipts or idempotency guarantees, and
the hub does not model unresolved external mutations or structured tool-call outcomes.
Adding that policy would be a new contract/configuration surface and was explicitly out
of scope for this smallest useful increment.

Accordingly, resume remains at-least-once. Workers still must inspect durable state before
reissuing mutations; the nudge ladder reduces a common idle delay, not the general
exactly-once problem.

## 4. Cross-provider selection - default and fail-closed

**SHIPPED.** Public worker offerings carry a code-owned provider (`anthropic`, `openai`,
or deliberately `unknown`) in their immutable snapshots. The public management roster
also defines one approved Sentinel role for each supported provider. Before every
Sentinel launch — including a retry attempt — Python resolves the worker provider from
the pinned snapshot. Legacy orders without snapshot provider metadata alone fall back
to the known harness/model classifier.

The Recruiter then selects the opposite-provider role:

```text
anthropic worker → openai Sentinel
openai worker    → anthropic Sentinel
```

The hub validates `worker.provider != sentinel.provider` on every hire. Unknown worker
identity and a missing or unusable opposite-provider role use the existing
`sentinel-degraded` path with a typed `reason_type`; they never fall back to a
same-provider Sentinel. An explicit `management.sentinel` command override is still
honored as-is when its command identity proves a provider distinct from that worker. An
unknown or matching override provider degrades fail-closed with its own typed reason.
The durable `sentinel-hired` requester event records the resolved worker and Sentinel
providers.

The former `UPAGENT_REQUIRE_CROSS_PROVIDER_SENTINEL` opt-in and its environment gating
are retired. Cross-provider selection is now the invariant, not an operational mode.
Capacity-aware fallback among multiple roles within the same provider remains deferred;
the shipped contract needs exactly one approved role per supported provider.

## 5. Escalation - typed requester event, publish then flag

**SHIPPED.** Once all three intents are spent, Python emits the existing typed
`worker-stall-escalation` ledger event and publishes one requester-mailbox notification
with paths to `nudges.json` and the archived stalled closeouts. The Sentinel does not
watch the lead, confirm delivery, or acquire requester authority.

The implementation uses **publish then durable flag**:

1. if `state["escalated"]` is not true, publish the event and requester notification;
2. then save `escalated: true` in `nudges.json`;
3. later exhausted closeouts see the flag and do not republish.

This ordering deliberately prefers a rare duplicate over a lost escalation if the
process crashes between publication and flag persistence. It is not an atomic mailbox
transaction and does not claim to be one.

## 6. What this does NOT change

- The reconciler's fail-loud verdicts: a missing result artifact remains `blocked`.
- Request hard timeouts remain the final backstop.
- Worker packaging conventions remain: small scopes and early commits limit loss.
- The Sentinel remains advisory and never kills, cancels, or terminalizes on its own.
- Requester messages, cancellation, review continuation, and release keep their existing
  authenticated paths.
- No broad Python-owned stall detector was added.
- No public nudge command, arbitrary prompt capability, subsystem, or new config surface
  was added.
- Capacity-aware fallback among multiple Sentinel roles within one provider remains deferred.
- Per-order resume-safety policy remains deferred.

One Sentinel-contract change was necessary for the ladder itself: `STALLED` is now a
**provisional** closeout. After writing it, the Sentinel stays idle for the hub's
disposition. `SENTINEL_STALL_NUDGED` authorizes it to resume PULSE and later write a fresh
`STALLED` closeout. Only `COMPLETE`, `NEVER_STARTED`, and `FINALIZATION_FAILED` are
terminal closeouts that tell it to exit.

## Acceptance criteria (updated to the shipped scope)

The shipped tests and live drill pin the bounded behavior rather than the original broad
matrix:

1. Backoff is immediate / 5 / 15 minutes, cap 3; intent is durable before delivery and
   successful delivery is recorded separately.
2. The only worker payload is literal `continue`; failed or held attempts produce their
   own typed events and truthful Sentinel disposition prompts.
3. Attempt/generation mismatch, absent pane, missing journal, corrupt nudge state, and
   requester-facing or terminal lifecycle state do not deliver.
4. A valid completion bundle supersedes a simultaneous `STALLED` closeout without
   spending a rung.
5. Cursor receives the required paste settle behavior through the existing adapter.
6. Repeated held closeouts retain distinct archives.
7. Exhaustion publishes the requester escalation once in ordinary/recovery operation via
   the publish-then-flag scheme; the documented crash window may duplicate but cannot
   lose the first publication.
8. Every hire and retry selects the role opposite the worker offering's pinned provider;
   unknown workers, unavailable opposite roles, and unknown or matching command overrides
   degrade fail-closed with typed reasons. Resolved provider values appear in the existing
   hire event.
9. A real-machine fire drill submits an actual worker through `just upagent request`,
   injects one valid corroborated `STALLED` closeout, observes
   `worker-nudge-intent` then `worker-nudge-delivered`, sees literal `continue` arrive in
   the worker pane, and reaches a normal passed terminal result. Evidence is in
   `follow-ups/2026-08-19-stall-nudge-reviews/live-fire-drill.md`.

The accepted residual is explicit: the 10-second idle wait makes the final state-to-send
race seconds-scale but does not eliminate it. A full lock reservation, capacity-aware
same-provider role fallback, and per-order resume-safety contract are not acceptance
criteria for this shipped increment.

## Migration note (snapshot schema)

`validate_snapshot` now hard-requires the `provider` field, so a pre-§4 in-flight or queued
order whose persisted `offering_snapshot` lacks `provider` blocks as "invalid offering
snapshot" rather than merely degrading its sentinel. This retroactive tightening of the
otherwise-immutable snapshot is machine-local and accepted: drain or re-submit any in-flight
requests when deploying this change.
