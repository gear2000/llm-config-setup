# Herdr meta-runner improvements (9 items) — implementation plan

> **Status:** planned, NOT implemented. This document is the executable plan for a follow-up
> agent/engineer. Only the pyramid-cockpit / gpt-5.6-routing / codex-status-hook work (commit
> `84e50a6` on branch `cockpit-and-routing`) is already done; the 9 items below are not.

## Context

After the first full 6-phase `/herdr-run`, an improvement review recorded 9 real frictions
(each with an "observed" note from that run): a false-completion signal that forced
context-eating file polling, no stuck-detection, no glanceable status view, route-copy drift,
stale leader panes, unguarded multi-session operation, a 30-min timeout that killed 1.5–4h
stages three times, cosmetic `result.json` violations burning retry budget, and a stale primary
working tree after ref-only merges. This plan fixes all 9 in the **public kit source layers
only** — the prose the TUI/leader follow plus the UpAgent engine. It changes no runtime
behaviour until a later, deliberate `just update` composes it into destinations.

## Hard constraints

- Work in a **git worktree/branch**, never directly on `main` (other agents run there).
- **Do NOT run `just update`** while implementing — it writes into the configured destination
  repos and into `~/.claude`/`~/.pi`, and would pollute another agent's in-flight work.
  Validate with `just test` only (kit-local: pytest + node schema). Composition/propagation is
  a separate, explicitly-approved later step.
- Edit **source layers**, never generated `SKILL.md`. Public repo → generic prose, no
  proprietary strings; an independent proprietary vet gates any push (see `CLAUDE.md`).

## Clarification (#4)

There is **no `route.yaml` file to edit in this kit** — the live route is a per-run file in the
destination's work-log tree. #4 is purely a **prose change** to the runner protocol. There is
also no literal "kept in sync manually" string to delete (the review paraphrased operational
reality); the fix is an **additive** declaration that the run-tree copy is the single live copy.

## The 9 changes

File paths are kit-relative. Line numbers are approximate anchors as of this writing.

### A. Prose-only (protocol + runner command layers) — #1, #4, #5, #6, #9, #2, #3

Files:
- `.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md`
- `.shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md`
- `.shared-llm/public/layers/slash-commands/common/common/meta-runner-phase-protocol.md`

**#1 — Event-driven phase completion (replace polling).**
- Protocol "Phase leader responsibilities" (writes `phase-result.json`) and
  `herdr-phase/command.md`: require that, immediately after writing `phase-result.json`, the
  leader's **literal last action before going idle** is to print one namespaced line to its own
  pane: `PHASE_RESULT: phase-<id> verdict=<passed|failed|blocked> pass=<n>`.
- `herdr-run/command.md` phase-loop step 3: change the wait from
  `herdr wait agent-status <leader-pane> --status done` to
  `herdr wait output <leader-pane> --match "PHASE_RESULT: phase-<id>" --timeout <ms>` — the same
  event-driven primitive already used for the Recruiter's `ORDER <id> DONE`. The TUI reads the
  verdict from the matched line; it opens `phase-result.json` only for detail / on failure. Keep
  the existing on-timeout fallback (read+validate `phase-result.json`, else `blocked`).
- Rationale to encode: a leader's `agent_status` flips to idle between its *own* turns while a
  Recruiter order is still in flight, so `--status done` returns prematurely.

**#4 — Route-copy single-source-of-truth (freeze means frozen).**
- `herdr-run/command.md` pre-flight step 5 and protocol "The run tree" section: declare the
  run-tree frozen copy (`<run-root>/<date>/<slug>/route.yaml`) the **single live copy** for the
  run. The origin passed via `--route` becomes read-only/historical once the run starts; every
  `/herdr-phase` invocation is handed the run-tree copies; all mid-run routing edits happen
  **only** to the run-tree copy.
- `herdr-phase/command.md`: leaders read (and, if a human adds a profile mid-run, edit) the
  run-tree route copy — never the origin.

**#5 — Unconditional leader-pane cleanup (incl. backtrack).**
- `herdr-run/command.md` step 5 + "Phase-level backtracking": make destroying the prior leader
  pane an explicit, unconditional step on **every** leader transition — forward advance AND
  backtrack replay. Before creating a fresh leader for a phase, close any existing leader pane
  for it.
- Add an optional tiny state file `active-leader-panes.json` (phase-id → current leader pane-id)
  in the run tree as the one canonical liveness check across a long run.

**#6 — Concurrent `/herdr-run` session awareness.**
- New subsection in `herdr-run/command.md` near "Cockpit + shared-services setup": document that
  multiple TUI sessions can drive one run (easy to reach via Remote Control) and is currently
  unguarded. Add a **last-writer marker** — a comment line (`# last-edited-by: <session-id> @
  <iso-ts>`) written to the run-tree `route.yaml` on every edit, so a second session can detect
  "someone touched this since I last read it" before overwriting. Warning, not a lock.

**#9 — Reconcile primary working tree after ref-only merge.**
- Protocol Stage 3/4/5 merge steps + "Rollback safety": bake the fix into the merge action
  itself — immediately after any ref-only merge (`git update-ref` / fast-forward chosen to avoid
  touching a dirty primary checkout), run a pathspec-scoped `git checkout HEAD -- <the
  phase-touched files>` in the primary checkout to bring its working tree/index current,
  **before** the merging stage proceeds. Stated once, structurally — not left for each Stage 3
  to rediscover.

**#2 — Optional lightweight watchdog (new capability, prose).**
- New subsection in the protocol (after "Phase leader responsibilities"). Frame it explicitly as
  an **allowed non-stage monitoring helper** — NOT stage work, NOT a Recruiter order, NOT a
  delegating worker — the one sanctioned exception to the "no native subagents" rule, precisely
  because it does no stage work. Spec: plain subagent on a mid-tier model (not the cheapest
  tier); wakes every 5–10 min (configurable); reads `agent_status` + a short output tail for the
  panes it watches; diffs vs its last sample; stays silent unless **stuck** (working, no output
  change across N checks), **failed** (error/traceback/blocked in fresh output), or **done**
  (`phase-result.json` / the #1 marker appears); reports back a single message, not running
  commentary. TUI or leader may spawn one alongside long stages.

**#3 — Glanceable HTML run-status snapshot (new capability, prose).**
- New step in `herdr-run/command.md` phase loop after the run-status append: on each
  phase-level status change (start/pass/fail/backtrack), or hourly if none, generate a minimal
  static HTML snapshot of run state (per-phase done/in-progress/blocked, current stage, one line
  each — mirroring `run-status.md`). **Delegate generation to a small disposable subagent** (same
  sanctioned non-stage-helper framing as #2) given only the `run-status.md`/`phase-status.md`/
  `phase-result.json` paths; it writes the file and returns only the path — keeping HTML out of
  the TUI's token budget. Resurface via a file-send/`Artifact` mechanism, never inline. Reuse the
  existing HTML CSS baseline (`.shared-llm/public/llm/common/common/planish-html-grill-contract.md`
  + `toolkits/`) for visual consistency rather than inventing styling.

### B. Engine (UpAgent) — #7, #8

Files:
- `.shared-llm/public/extensions/common/upagent/recruiter.py`
- `.shared-llm/public/extensions/common/upagent/contracts.py`
- `.shared-llm/public/extensions/common/upagent/recruiter_test.py`
- `.shared-llm/public/extensions/common/upagent/contracts_test.py`
- `.shared-llm/public/llm/pi/common/meta-plan/meta-plan-format.md` (doc note only)

**#7 — Per-stage-type default timeout.**
- `recruiter.py`: add a `STAGE_TIMEOUT_MS` map — `stage-1-implementation` and
  `stage-2-adversarial-audit` → a realistic multi-hour default (**3h = 10_800_000**); every
  other stage keeps the current `DEFAULT_TIMEOUT_MS` (30 min). Add a helper
  `_default_timeout_ms(stage_id)`. Where the timeout is resolved
  (`timeout = str(order.get("timeout_ms", DEFAULT_TIMEOUT_MS))`), change to
  `order.get("timeout_ms") or _default_timeout_ms(order["stage_id"])` so an **explicit order
  `timeout_ms` still overrides**. Reuse `contracts.RECOGNIZED_STAGE_IDS` for key validity.
- `recruiter_test.py`: cover stage-1/2 get the long default, other stages get the short default,
  explicit `timeout_ms` overrides both.
- `meta-plan-format.md`: one note that stage-1/2 auto-get a multi-hour timeout and an order may
  override via `timeout_ms` (no schema field — the `estimated_duration` option was declined).

**#8 — Cosmetic `result.json` self-heal + copy-pasteable template.**
- `contracts.py`: add an explicit, **small** `normalize_cosmetic(raw: dict) -> (dict, list[str])`
  — verdict aliases (`VERIFICATION_PASSED`, `VERIFIED`, `PASS`, `PASSED`, `OK` → `passed`;
  `FAIL`/`FAILED` → `failed`; `BLOCKED` → `blocked`), and `full_log` as a single-element list →
  its string element. Returns the normalized dict plus the list of corrections applied. The
  strict `parse_result` stays strict and unchanged (other callers/tests unaffected).
- `recruiter.py`: at the result-read boundary (before `contracts.load_result`), run
  `normalize_cosmetic` on the raw JSON; if corrections were applied, log
  `recruiter: auto-corrected cosmetic result.json (<corrections>) — not a real failure` to
  stderr and validate the normalized dict. Leniency lives only at this boundary, explicit and
  logged — never silent, never a general loosening.
- Protocol: add a copy-pasteable `result.json` template block to the `instructions.md` injection
  list (Phase leader responsibilities). Clarify the Stage-2 `VERIFICATION_PASSED` line so it
  reads as the audit-outcome concept, NOT the `result.json` verdict enum (which is `passed`) —
  this exact confusion produced a `verdict: "VERIFICATION_PASSED"` in the observed run.
- `contracts_test.py`: cover each alias, the array→string `full_log` coercion, and that a
  genuinely-invalid result still fails.

## Files touched (summary)

Prose: `herdr-run/command.md`, `herdr-phase/command.md`, `meta-runner-phase-protocol.md`.
Engine: `upagent/recruiter.py`, `upagent/contracts.py` (+ their `_test.py`).
Doc: `meta-plan/meta-plan-format.md`.
Schema (`meta-plan-schema.ts`) is **not** touched — the 37 node checks stay stable.

## Decisions baked in (raised, settled)

- All 9 items in scope.
- #4 resolved by "freeze means frozen" (prose only; no symlink).
- #7 resolved by per-stage-type recruiter default (no `estimated_duration` schema field);
  long default = **3h** for stage-1/2 (tune the number if desired).
- #2 watchdog model = "mid-tier, not the cheapest" stated generically (kit prose is
  provider-neutral).
- #8 alias set kept deliberately small (the list above); not a general validator loosening.

## Verification

- `just test` from the kit root must stay green: **111 pytest + 37 node schema**, plus the new
  #7/#8 cases. Kit-local; touches no destination.
- **Not** run during implementation: `just update` (deferred), any `/herdr-run` (needs
  composition + a live Herdr), any push. The prose changes only take effect after a later,
  approved `just update` and a real run.
