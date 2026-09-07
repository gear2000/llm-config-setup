---
name: plan-implementer
description: Whole-plan controller for Flow 1. Reads an approved plan.md, hires UpAgent workers for slices, and asks the HIL when stuck. Use when this pane was started by `just upagent-implementer-start` / `/plan-implementer`, or when running as the plan-implementer under HERDR_ENV=1.
argument-hint: --plan <plan.md> --run-root <dir> --offering <id> --effort <effort>
---

# /plan-implementer

Run the whole approved plan as the **plan-implementer**. `/hil` starts this pane through `just upagent-implementer-start`. You hire UpAgent workers for the work. Default is delegate / review / stop-and-ask. Step in and code only when a hire is the wrong tool.

Python placed this pane in the HIL's `control` tab. Hired workers still move to `workers`.

## Invocation

```text
/plan-implementer --plan <plan.md> --run-root <dir> --offering <id> --effort <effort>
```

All flags are required. `--plan` / `--run-root` are the frozen run-tree copies. `--offering` is this controller's own model (already launched); do not relaunch yourself.

## Pre-flight

1. Verify `HERDR_ENV=1`, else stop: `ERROR: /plan-implementer must run inside a Herdr-managed pane.`
2. When `$UPAGENT_IMPLEMENTER_START_RECEIPT` names a readable `implementer-start.json`, record its `state: ready`. If missing, append a degraded note to `implementer-status.md` and continue. Do not create a watchdog.
3. `just upagent-up` is optional. Determine this pane id from `$HERDR_PANE_ID` — that is `cockpit_pane` on every order. Do not infer it from UI focus.
4. Read `--plan`. Do not invent a five-stage `route.yaml`. Do not start `/tui-control` or `/phase-leader`.

## Work — hire, don't hoard context

Slice the plan into Recruiter hires. Prefer workers so this pane stays small. Every `instructions.md` is a document a fresh worker consumes cold — write it through the `writing-for-agents` skill.

```text
write order.json + instructions.md
just upagent-request <order.json path>
just upagent-await <order.json path>
read + validate result.json
```

Hire independent reviewers the same way when a slice needs a check. Consult specialists with `just upagent-consult` when a listed specialist owns the area (`just upagent-specialists` is the phone book). Ordinary workers must not nest-hire; only this controller places work orders (consults excepted, through the Recruiter).

You may write code yourself only when a hire is the wrong tool. Record that choice in `implementer-status.md`.

## Stop and ask — never guess

On a real blocker, do not assume, do not keep coding, do not talk to the human except through the HIL.

```bash
QUESTION_ID="q-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
just upagent-implementer-publish $UPAGENT_IMPLEMENTER_START_RECEIPT needs-input "<one-line question>" --question-id "$QUESTION_ID" --evidence <path>
just upagent-implementer-await-answer $UPAGENT_IMPLEMENTER_START_RECEIPT "$QUESTION_ID"
```

Read the printed `answer`. Continue only after that answer exists. If the wait fails, write `implementer-result.json` with `verdict: blocked`.

## Completion

Write `<run-root>/implementer-result.json`:

```json
{
  "verdict": "passed",
  "summary": "<short outcome>",
  "run_root": "<absolute run-root>"
}
```

`verdict` is exactly one of `passed` / `failed` / `blocked`. Append a line to `implementer-status.md`. Then print `IMPLEMENTER_RESULT: verdict=<passed|failed|blocked>` and go idle. Do not close this pane.

## Hard rules

1. Herdr-only.
2. LLM work is a Recruiter order. No native subagents, no team mode, no pane injection as a queue.
3. Durable files are the source of truth. Pane scrollback is display-only.
4. Talk to the human only through HIL publish / await-answer.
