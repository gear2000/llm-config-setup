# /meta-auto-run

The **single kickoff workflow** the meta-orchestrator brain sends to a freshly spawned Claude Code
TUI session over tmux. The brain spawns one session per phase and sends it exactly this one command.
It runs three steps **in order, as one driven sequence** — connect to the hub, do the phase work
(writing the result to a file), then ping the brain.

This is the design that removes the old "the worker forgot to report" fragility: because the phase
result is **written to disk as a file** in step 2, the brain can detect completion by watching that
file even if step 3 never runs. The file is the source of truth; the ping is only the accelerator.

## Invocation

```
/meta-auto-run --plan <plan.md> --phase <instructions.md> --output <results.md> --hub <loc> [--team true]
```

- `--plan <plan.md>` — the full plan file the brain wrote.
- `--phase <instructions.md>` — the brain's instructions file for THIS attempt (goal + done-check).
- `--output <results.md>` — the path you must end up WRITING the verdict to.
- `--hub <loc>` — the hub's discovery JSON path (default `$HOME/.meta-orch/hub.json`) or a direct url.
- `--team true` — optional. Passed straight through to `/run_phase` so the work is done by a
  TeamCreate team (named teammates in tmux windows) rather than subagents.

`--flag value` tokens in `$ARGUMENTS`, any order. **All of `--plan` / `--phase` / `--output` /
`--hub` are required → FAIL LOUD on any missing**, with the usage line.

## Execution — run these three steps IN ORDER

### Step 1 — Register on the hub

Run **`/hub-connect <hub>`** with the `--hub` value (its JSON path or url). This reads the hub's
discovery JSON, health-checks `/health`, and registers this session as an agent. If it fails (no
hub, stale JSON, `/register` non-2xx), `/hub-connect` STOPs loud — surface that and stop; there is
no point doing the phase work for a brain you cannot reach. Note the worker name it registered as
(you pass it to `/response` in step 3).

### Step 2 — Do the phase work and WRITE results.md (the mandatory disk write)

Run **`/run_phase --plan <plan.md> --phase <instructions.md> --output <results.md> [--team true]`**,
threading through the same `--plan`, `--phase`, `--output`, and `--team` you were given. `/run_phase`
reads the plan + the instructions, dispatches the team/subagents, runs the mandatory
`adversarial-evaluator` gate, and **WRITES `results.md`** with a leading `PHASE_RESULT:` line.

**This write is a MANDATORY final action.** The whole file-based design rests on `results.md`
existing on disk with an honest verdict — the brain judges the phase by reading that file. A run
that does the work but never writes `results.md` has not finished its job.

### Step 3 — Ping the brain

Run **`/response --hub <hub> --output <results.md> [--name <worker-name>] [--msg <msg_id>]`** — a
small done-ping carrying the verdict + the results.md path back to the brain.

**This ping is also a MANDATORY final action** — always send it when you can. BUT it is the
**accelerator, not the source of truth**: because step 2 already wrote `results.md` to disk, the
brain can detect completion by watching that file even if this ping is skipped or fails. So if the
ping fails, the phase result is NOT lost — it is on disk for the brain to read. Report the ping
outcome, but never treat a failed ping as a failed phase.

## What is mandatory, and why it can't be forgotten

- **The results.md write inside step 2 is mandatory** — it is the source of truth the brain judges
  by. Always write it, with an honest `PHASE_RESULT:` first line.
- **The step-3 ping is mandatory too** — always send it — **but** it is only the accelerator. The
  brain detects completion from the on-disk `results.md` regardless. So forgetting the ping no longer
  strands the phase the way the old hub-only design did: **the file is the safety net.**

Run the three steps in order, do not skip step 2's file write, send the step-3 ping, and report each
step's outcome.

## Hard rules

1. **All four paths/locs required → FAIL LOUD on any missing** (Invocation). Never default one.
2. **Run the three steps in order: `/hub-connect` → `/run_phase` → `/response`** (Steps 1-3). Do not
   reorder, do not skip step 1's connect or step 2's work+write.
3. **The results.md write in step 2 is the mandatory source of truth** — always write it with an
   honest leading `PHASE_RESULT:` line; never fabricate `passed`.
4. **The step-3 ping is mandatory but is only the accelerator** — always send it, but a failed ping
   is NOT a failed phase, because the result is already on disk. Report it; do not strand the phase
   over it.
5. **This command is a thin conductor** — it invokes the three existing commands and threads the args
   through. It does not re-implement their logic (the hub handshake lives in `/hub-connect`, the work
   + gate + file write in `/run_phase`, the ping in `/response`).
