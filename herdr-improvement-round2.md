# Herdr meta-runner — improvement proposal, round 2

## Context for the implementing agent

This is a **separate** set of findings from `/tmp/herdr-improvement.md` (round 1, already
being implemented — do not merge into or edit that file). Same run, same context: the
"Diagram-findings gap fixes" plan run through `/herdr-run`. These two findings surfaced
after round 1 was already written, while phase 4 was still in flight.

Same source-file map as round 1:

- Runner prose: `.shared-llm/public/layers/slash-commands/common/common/{herdr-run,herdr-phase,meta-runner-phase-protocol.md,meta-runner-handoff-protocol.md}`
- Engines: `.shared-llm/public/extensions/common/{upagent,specialist,herdr}/`
- Schema substrate: `.shared-llm/public/llm/pi/common/meta-plan/`

Use the `update-shared-llm` workflow to apply: edit the source layer, run `just update` to
recompose, run the content review, then commit.

---

## 1. Workers read static docs but never actually ask the Specialist Hub Librarian — hit for 3 full tries on one bug

**Problem observed:** The protocol says a worker "may consult the Specialist Hub
Librarian for repo knowledge... that is a question, not delegation" — but "may" is
optional, and in practice workers default to reading static doc files instead of ever
messaging the live Librarian pane. Concrete evidence from this run: phase-4's
stage-1-implementation took **four tries** to find a real bug (Playwright's e2e suite was
silently testing an external, already-deployed URL instead of the fresh local build with
the actual code fix on it — so three tries in a row watched the identical test failures
persist no matter what code they changed, because the tests could never see their
changes). Try-1's own summary says exactly what happened: *"The scan and input-vars
specialist docs were consulted directly from `.claude/agents` and `.claude/skills`"* —
that's reading files, which can be stale or simply not cover the specific anomaly, not
asking a person. Across all four tries, the Librarian pane was never messaged even once.
The bug was eventually found on try 4 only by a worker adding its own ad-hoc debug-print
statement and manually inspecting raw output — the slow, expensive way.

**Why this matters beyond this one bug:** static docs go stale (this repo's own CLAUDE.md
says so explicitly elsewhere: "Design docs are a starting point... they drift"). A worker
that only ever reads files has no way to detect "this doc might not reflect current
reality" — it just silently trusts what it read and builds a wrong hypothesis on top of
it. The Librarian is the one thing in this architecture that could say "actually, that
doc's stale, here's what's really going on" or "I don't know either, but here's who
would" — and it's currently optional enough that nobody reaches for it under pressure.

**Proposed fix:** Make a Librarian consult **mandatory**, not optional, at a specific,
well-defined trigger point: **the moment a worker is about to re-attempt a diagnosis that
already failed once** (i.e., stage try N+1 is investigating the same unresolved failure
signature try N already looked at and didn't solve). Concretely, add this to the
worker-instructions template every retry after the first:

```
Before re-investigating: this is not the first attempt at this failure. Query the
Specialist Hub Librarian first (send it the failure signature + what the last try
already tried and ruled out) before forming a new hypothesis from docs alone. If the
Librarian doesn't know either, say so in your result — that's still useful signal — but
don't skip asking.
```

This doesn't require a new capability — the Librarian and the files-plus-signal query
pattern already exist (`.shared-llm/public/extensions/common/specialist/`). It's purely a
wording change: move it from "may" to "must, on retry."

**Where:** `meta-runner-phase-protocol.md`'s stage-retry / backtrack section (the part
that governs what a leader puts in try N+1's `instructions.md` when `revisit` points back
at the same stage) — add the mandatory-Librarian-on-retry requirement there.

---

## 2. The Recruiter is a single-threaded blocking process — undocumented, causes silent queueing

**Problem observed:** The Recruiter's `recruit` command (`recruiter.py:257-331`,
`cmd_recruit`) is not a dispatch-and-return function despite reading like one should be —
it calls `herdr wait agent-status <worker_pane> --status done --timeout <order's own
timeout>` **inline, synchronously**, before it can process anything else. There is no
threading, no background worker pool, no queue — one Recruiter pane handles exactly one
order at a time, start to finish, however long that takes (up to the order's own timeout,
which can be hours). This went unnoticed for this run's first several phases because a
single phase leader only ever places one stage order at a time anyway (stages are
inherently sequential), so nothing ever asked the Recruiter to multiplex. It only surfaced
when the TUI placed a genuinely separate, ad-hoc order directly (bypassing the phase-leader
layer, testing direct cross-LLM dispatch) while a phase leader's own order was already in
flight — the ad-hoc order sat as unexecuted, queued keystrokes in the Recruiter pane's
terminal buffer, invisible until someone thought to check.

**Why this matters:** nothing in the protocol documents this constraint, so nobody placing
an order has a way to know in advance "this will queue behind whatever's already running."
A human (or the TUI) can easily place what looks like an independent, parallel request and
have it silently wait — potentially for hours — with no visible error, no queue-position
indicator, nothing.

**Proposed fix:** Two complementary options, not mutually exclusive:
- **(a) Document the constraint plainly** in the Recruiter's own description and in
  `meta-runner-phase-protocol.md`'s order-placement section: one Recruiter = one order in
  flight at a time; anything placed while another order is active will queue silently
  behind it.
- **(b) Support a second, independent Recruiter instance** for cases that genuinely need
  concurrent order handling (e.g., a TUI placing an ad-hoc order while a phase leader has
  its own order in flight) — same roster, a separate pane, armed the same way via
  `cmd_up`. This isn't a rewrite of the Recruiter into a real dispatcher; it's just
  permitting more than one of the existing single-threaded ones to run side by side when
  a caller actually needs that.

**Where:** `recruiter.py`'s module docstring + `meta-runner-phase-protocol.md`'s
"Execution model" section (the order round-trip description) should state the
one-at-a-time constraint explicitly; `cmd_up`'s docstring should note it supports being
invoked more than once for a second independent Recruiter pane.

---

## Suggested priority order

1. **#1 (mandatory Librarian consult on retry)** — pure wording fix, no new moving parts,
   directly cost this run 3 full wasted tries on one bug.
2. **#2 (Recruiter one-at-a-time)** — at minimum document it (cheap); a second Recruiter
   instance (option b) is bigger scope and can be its own follow-up.
