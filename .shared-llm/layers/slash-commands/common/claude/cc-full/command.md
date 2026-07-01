# cc-full — Research → Grill → Freeze → Phases → Interactive Implement, one command

One command that drives the whole interactive build pipeline end to end, so you stop re-launching each stage by hand. It is a **conductor**: it invokes three skills you already have, in order, and carries each stage's on-disk artifact into the next.

This is the interactive path. It is NOT for AFK runs — the grill needs you, and the per-phase verdicts need you. AFK is the path that produces slop; this skill is the opposite.

## What it chains

| Stage | Skill invoked | Heavy work | You |
|---|---|---|---|
| 1. Research + plan + grill | `cc-plan-and-grill --route-phases <title>` | research + plan-writing = subagents | answer the grill |
| 2. Freeze → phase JSON | `rphase-create <plan.md>` | none (solo skill) | confirm any fuzzy verification |
| 3. Interactive implement | `cc-loop --interactive <phases_root>` | each phase = subagent | mediate blocked phases |

The three stages share ONE work-log directory, because Stage 1 creates it and Stages 2–3 operate on its artifacts.

## Cardinal rule: conduct, don't do — and don't re-implement

Two rules, both load-bearing:

1. **Delegate, don't do.** Same as every `do:` skill — you orchestrate, subagents do the work. The sub-skills already enforce this internally; you inherit it. Do not read code or write files yourself.
2. **Conduct, don't re-implement.** You do NOT re-specify how research, grilling, phase-creation, or implementation work. You invoke the sub-skill and let it run its own current workflow. Your only added value is the control flow *between* stages: capture each stage's output path, override its terminal STOP, move to the next stage. When `cc-loop` improves later, `cc-full` inherits it for free — keep it that way.

## Invocation

```
/cc-full <title>
```

- **`<title>`** — Required. Passed straight to `cc-plan-and-grill`, which slugifies it for the work-log directory. All three stages then share that one directory.

No `--team`, no `--afk`. This skill is deliberately interactive-subagent only. If you want TMUX visibility or unattended execution, run the stages by hand instead.

## The STOP-override contract (the one non-obvious thing)

Each sub-skill ends by STOPPING and telling a human to run the next command. `cc-plan-and-grill` Step 7 literally says *"STOP. Do not call /rphase-create."* and, with `--route-phases`, suggests `/cc-loop --afk`. **In `/cc-full`, YOU are the human those messages are addressed to.**

So, at every stage boundary:

- Treat the sub-skill's terminal STOP and its "run X next" suggestion as **addressed to you**.
- Do **not** end your turn. Do **not** wait for the user to re-prompt.
- Mark the stage's todo done, capture the path the sub-skill reported, and immediately invoke the next stage.

Two overrides specifically:
- `cc-plan-and-grill --route-phases` will suggest `/cc-loop --afk`. **Ignore the `--afk`** — `/cc-full` always goes `--interactive`.
- `rphase-create` freezes the plan and stops. Proceed straight to `cc-loop --interactive`.

The TodoWrite list (Step 0) is your anchor: it — not the sub-skills' STOP lines — governs what happens next.

## Workflow

### Step 0: Set up the anchor

Create a TodoWrite list with exactly three items, so the sub-skills' STOPs can't derail you:

1. Stage 1 — plan-and-grill (`--route-phases`)
2. Stage 2 — rphase-create
3. Stage 3 — cc-loop --interactive

Mark Stage 1 `in_progress`.

### Step 1: Plan + grill (invoke cc-plan-and-grill)

Invoke the **`cc-plan-and-grill`** skill via the Skill tool with args: `--route-phases <title>`.

Why `--route-phases` even though we implement interactively: `rphase-create` (Stage 2) **refuses any plan whose phases lack a `**Size**` tag.** `--route-phases` is what puts those tags there. `--interactive` won't use the tags, but `rphase-create` still demands them — so the flag is mandatory for the chain, not optional. (If the user asks why: that's the answer — the converter validates the tags; the loop just ignores them in interactive mode.)

Let plan-and-grill run its full workflow: research (Explore subagents → synthesizer), draft (plan-writer subagent), and the grill (you, in the main thread, one question at a time — this is where the user spends their time). It finalizes a frozen `plan.md` and STOPS.

**Capture:** the finalized plan directory it reports — `…/work-log/<date>/<slug>/plan/v<N>/`. Call it `<plan-dir>`. The plan file is `<plan-dir>/plan.md`.

**Then:** per the STOP-override contract, do not stop. Mark Stage 1 done, Stage 2 `in_progress`, proceed.

**Seam guard:** if the grill was abandoned and no `plan.md` was finalized (frontmatter `status` is not `Accepted`), STOP here — do not create phases from a plan that was never agreed. Tell the user the plan wasn't finalized and that they can re-run `/cc-full` or pick up the draft.

### Step 2: Convert to phases (invoke rphase-create)

Invoke the **`rphase-create`** skill via the Skill tool with the plan file path: `<plan-dir>/plan.md`.

It reads the frozen plan, may grill briefly on any ambiguous verification block, writes per-phase JSON to `<plan-dir>/phases/current/available/phase-NN.json`, scaffolds the loop's state folders (`done/`, `blocked/`, `iterations/`), and chmod-freezes `plan.md`.

**Capture:** the phases root — `<plan-dir>/phases`.

**Then:** mark Stage 2 done, Stage 3 `in_progress`, proceed.

**Seam guard:** `rphase-create` is idempotent — it refuses if `phases/` already exists. If it refuses for that reason, do NOT try to recreate; the phases are already there. Tell the user you're resuming (not regenerating), and skip straight to Stage 3 against the existing `<plan-dir>/phases`.

### Step 3: Interactive implementation (invoke cc-loop)

Invoke the **`cc-loop`** skill via the Skill tool with args: `--interactive <plan-dir>/phases`.

The orchestrator (you, main thread) stays alive across phases. Each phase's real work runs in a fresh subagent, so code never piles up in the main thread. You judge PASSED / FAILED / BLOCKED per phase; BLOCKED phases pause the loop for the user (the loop pre-researches fix options before asking via AskUserQuestion). The loop runs until all phases are done or a stop condition fires.

Let `cc-loop` own everything from here — phase routing, verdicts, the live-deploy gate on the final phase, results.md, decisions.md. Do not duplicate any of it.

### Step 4: Close out

When the loop finishes, mark Stage 3 done and report to the user in the project's response format (Summary / Issues / Next):
- phases done / total
- where results live (`<plan-dir>/phases/done/<phase-id>/results.md`)
- anything left blocked, with the blocker reason

## Clean-resume escape hatch (tell the user once)

Because `plan.md` and the phase JSON are frozen on disk, the planning conversation is not load-bearing for implementation. At the Stage 2→3 boundary, remind the user once:

> *"Plan frozen and phases created at `<plan-dir>/phases`. Continuing into implementation now. If you ever want a fresh context, you can `/clear` and run `/cc-loop --interactive <plan-dir>/phases` to pick up exactly here."*

That manual `/clear` against the on-disk phases is the only "rewind" available. There is no automatic mid-command context reset — a skill cannot clear its own window. You don't need one here: the heavy work is already in subagents, so the main thread only ever holds the grill Q&A and short per-phase verdicts.

## When to use this vs other cc- skills

- **`/cc-research`** — research only.
- **`/cc-plan-and-grill`** — research + grilled plan, then STOP (you implement separately).
- **`/cc-oneshot`** — lightweight research + sketch + implement, no persisted plan.
- **`/cc-full`** — the whole heavyweight interactive pipeline, one command ← **you are here**.
- **`/cc-loop --afk`** — unattended phased execution (the AFK path `/cc-full` deliberately avoids).
