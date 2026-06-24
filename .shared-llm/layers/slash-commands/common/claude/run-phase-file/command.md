# /run_phase

Run **ONE phase** of a plan **from files** — the file-based worker playbook the meta-orchestrator
**brain** launches per phase. A Pi brain spawns a fresh Claude Code TUI session and sends it ONE
command. The phase's data does NOT travel over the hub — it travels as **files on disk**. This
command reads the plan file and the phase's instructions file, does the work by dispatching an
agent **team** (or subagents), runs the mandatory adversarial gate, and **writes the phase result
to a file**. That results file is the source of truth the brain reads to judge the phase.

This is the file-based sibling of the older `/run-phase` (which took `plan=…/phase=N/agents=…` and
got its work over the hub). Here the work, the instructions, and the verdict all live in files; the
hub is only used elsewhere for the join + a done-ping. **This command itself is HUB-AGNOSTIC — it
touches no hub, only files.**

## Invocation

```
/run_phase --plan <plan.md> --phase <instructions.md> --output <results.md> [--team true]
```

- `--plan <plan.md>` — absolute path to the full plan markdown the brain already wrote.
- `--phase <instructions.md>` — absolute path to the brain's instructions for THIS attempt
  (`phases/<phase>/iteration/<n>/instructions.md`). This file states the phase's goal + done-check.
- `--output <results.md>` — absolute path you WRITE the result to
  (`phases/<phase>/iteration/<n>/results.md`). The brain reads this file to judge the phase.
- `--team true` — optional. When present, do the work with a **TeamCreate** team (named teammates
  in their own tmux windows — the interactive-TUI team feature). When absent, dispatch **subagents**
  via the Task/Agent tool.

These are `--flag value` tokens in `$ARGUMENTS`; they may appear in any order.

## The file contract (read this — it is the whole job)

Everything reads and writes files under the session dir `/tmp/meta-orch/<session_name>/`:

- `plan.md` — the full plan (the brain already wrote it). You READ it.
- `phases/<phase>/iteration/<n>/instructions.md` — the brain's instructions for this attempt (the
  brain already wrote it). You READ it: it carries the phase's goal and its done-check.
- `phases/<phase>/iteration/<n>/results.md` — **you WRITE this.** It MUST begin with a single line:

  ```
  PHASE_RESULT: passed|partial|blocked|failed
  ```

  then a `----- REPORT -----` section below it with what was done + the evaluator outcome. **The
  brain reads this file to judge the phase — it is the source of truth, so writing it correctly is
  the whole job.** A run that does the work but never writes `results.md` (or writes it without the
  leading `PHASE_RESULT:` line) is a failed run — the brain cannot certify it.

## Execution

### Step 1 — Parse `$ARGUMENTS` (fail loud on any missing or unreadable)

Parse `--plan`, `--phase`, `--output` (and the optional `--team`). **Do not guess a default for any
of the three required paths.**

- **Any of `--plan` / `--phase` / `--output` missing** → print and STOP:

  ```
  Usage: /run_phase --plan <plan.md> --phase <instructions.md> --output <results.md> [--team true]
  All three paths are required: the plan file, the phase instructions file, and the output results file.
  ```

- **`--plan` unreadable** → STOP: `ERROR: plan file not found / unreadable: <plan>`.
- **`--phase` unreadable** → STOP: `ERROR: phase instructions file not found / unreadable: <phase>`.
- **`--output` directory does not exist / is not writable** → STOP:
  `ERROR: results output path not writable: <output>`. (The brain creates the iteration dir; if it
  is missing, the launch is broken — fail loud, do not silently `mkdir` somewhere else.)

### Step 2 — Read the plan AND the phase instructions; understand the goal + done-check

1. **Read the WHOLE plan file** at `--plan` for context — the plan's goal, the phases, their order.
2. **Read the phase instructions file** at `--phase`. THIS is the work for this attempt: its goal,
   its steps, and its **done-check** (how you know the phase is finished). The instructions file is
   the contract; the plan is the surrounding context. Execute the instructions' work, judged against
   the instructions' done-check.

If the instructions file has no discernible goal or done-check, that is a broken launch — STOP and
write a `blocked` results.md (Step 5) naming the gap. Do not invent a goal.

### Step 3 — Do the work by DISPATCHING A TEAM (or subagents)

You are the orchestrating lead. You do not do the domain work yourself — you dispatch, read each
return, check it against the instructions' goal + done-check, and iterate.

- **`--team true`** → use **TeamCreate**: stand up named teammates in their own tmux windows
  (and a watchdog if the phase needs one). This is the interactive-TUI team feature — the spawned
  Claude session is a real TUI, so a team with lateral SendMessage + tmux visibility is available.
  Scope each teammate to one slice of the phase's work.
- **no `--team`** → dispatch **subagents** via the Task/Agent tool — a fresh context window per unit
  of work, no `team_name`. Right when the phase is a tidy set of independent units that report back.

Either way: read each return, check it against the instructions' done-check, re-dispatch to correct
drift or finish a half-done piece, and iterate **dynamically toward the phase goal** — not a rigid
fixed-step loop.

### Step 4 — Mandatory end-of-phase adversarial gate (always, exactly like /run-phase)

After the work agents finish and your first-line checking says the phase goal looks met, you
**ALWAYS** dispatch ONE more agent before the phase can pass: the **`adversarial-evaluator`** (Opus
4.8, max effort). This is a **built-in mandatory gate**, not part of the team roster — it runs on
**every** phase, you never skip it, and you never substitute a work agent for it.

Dispatch it (`Agent(subagent_type="adversarial-evaluator", …)`) with: the phase's goal + work (from
the instructions file — the contract it judges against) and everything that was done (the agents'
returns, files touched, commands run + their output, the diff, any evidence you collected). It
returns a verdict:

- **CLEARED** → the phase work is cleared. Proceed to Step 5 and write `passed` (only if the work is
  also fully done against the done-check).
- **VEERED** → the phase does **NOT** pass. Read its findings (each cites a file:line or the exact
  claim that veers), re-work the flagged piece (re-dispatch the relevant agent), and **re-run a
  fresh gate**. Loop until CLEARED. A VEERED you cannot clear is a `blocked`/`partial` phase — write
  that honestly in Step 5; **never write `passed` over a standing VEERED.**

(If `adversarial-evaluator` does not resolve to a known agent, that is a broken setup — FAIL LOUD,
do not skip the gate.)

### Step 5 — WRITE `results.md` (the `--output` path) — this is the deliverable

Write the file at `--output`. Its **FIRST line** is **exactly** the verdict line, nothing before it:

```
PHASE_RESULT: <passed|partial|blocked|failed>
```

then a report section:

```
----- REPORT -----
<what the team/subagents did, file:line evidence, commands run + outcomes>
<the adversarial-evaluator outcome: CLEARED, or VEERED with the findings>
```

Choose the verdict **honestly from what actually happened**, never to make the process look clean:

| Verdict   | Write it when…                                                                                   |
|-----------|--------------------------------------------------------------------------------------------------|
| `passed`  | the phase's work is **fully done** against the instructions' done-check **AND** the adversarial-evaluator returned **CLEARED**. Both. |
| `partial` | real work landed but the done-check is not fully met. |
| `blocked` | you could not proceed — a VEERED you could not clear, a hard blocker, or a broken launch. |
| `failed`  | the phase hit an error it could not get past. |

**Never write `PHASE_RESULT: passed` when the evaluator VEERED, when any done-check is unmet, or
when the work is otherwise incomplete.** Fabricating `passed` corrupts the brain's judgment of the
whole plan — the results file is the source of truth.

After writing the file, also print the same `PHASE_RESULT:` line to your output as the last line, so
a human watching the TUI sees the verdict too.

## Hard rules

1. **All three paths required → FAIL LOUD on any missing or unreadable** (Step 1). Never default a
   path, never silently relocate the output.
2. **Read BOTH files** (Step 2): the plan for context, the instructions for the work + done-check.
3. **Dispatch a team (`--team true`) or subagents (default); you orchestrate, you do not do the
   domain work yourself** (Step 3).
4. **The end-of-phase `adversarial-evaluator` gate is MANDATORY on every phase** (Step 4). Pass only
   on CLEARED; never over a standing VEERED.
5. **WRITE `results.md` with the leading `PHASE_RESULT:` line, honestly** (Step 5). This file is the
   source of truth the brain judges the phase by. A run that does the work but never writes a correct
   results.md is a failed run.
6. **HUB-AGNOSTIC.** This command touches no hub — only files. Registering on the hub and pinging the
   brain are other commands' jobs (`/hub-connect`, `/response`), chained by `/meta-auto-run`.
