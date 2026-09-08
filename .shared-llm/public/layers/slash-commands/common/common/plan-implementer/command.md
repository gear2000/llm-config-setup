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
2. When `$UPAGENT_IMPLEMENTER_START_RECEIPT` names a readable `implementer-start.json`, wait until `state` is `ready` or `ready-degraded`. Do not hire while it is `preparing` or `implementer-gated`. If the file is missing, append a degraded note to `implementer-status.md` and continue. Do not create a watchdog.
3. `just upagent-up` is optional. Determine this pane id from `$HERDR_PANE_ID` — that is `--cockpit-pane` on every hire. Do not infer it from UI focus. If `just upagent lists --type offerings --json` fails with `no checked-out main branch UpAgent source found` or `ambiguous checked-out main branch UpAgent sources`, `$UPAGENT_CANONICAL_REPO` should already be set from `start.sh`. Stop fail-loud; do not guess a checkout.
4. Read `--plan`. Do not invent a five-stage `route.yaml`. Do not start `/tui-control` or `/phase-leader`.

## Work — hire, don't hoard context

Slice the plan into Recruiter hires using the **Worker hire contract** below. Prefer workers so this pane stays small.

Hire independent reviewers the same way when a slice needs a check. Consult specialists with `just upagent-consult` when a listed specialist owns the area (`just upagent-specialists` is the phone book). Ordinary workers must not nest-hire; only this controller places work orders (consults excepted, through the Recruiter).

You may write code yourself only when a hire is the wrong tool. Record that choice in `implementer-status.md`.

## Human inbox

At every slice boundary, before placing the next hire and after reading its result, read `<run-root>/inbox/msg-<seq>.json` in numeric `seq` order. Also read it immediately on the fixed nudge `read your inbox`, and once more before writing the final result. The run root is the invocation's `--run-root`, never cwd. `control/inbox/` carries events for the HIL and is a separate directory.

Each envelope has `{seq, text, at_ns, acked}`. Act on every message with `acked: false` as human steering of the approved plan. Quote its entire `text` verbatim, with its `seq` and the action taken, in `<run-root>/implementer-status.md`. Resolve a blocker through the HIL's question/answer path below. After acting and recording the quote, atomically set `acked: true`: write the complete envelope to a unique temporary sibling, flush and fsync it, then rename it over the original. Preserve `seq`, `text`, and `at_ns`. Never delete envelopes or acknowledge one just because a nudge arrived.

Skip envelopes already acknowledged. A duplicate nudge is only a request to read the files again. If a restart finds a quoted but unacknowledged sequence, inspect the recorded action before repeating it, then finish its acknowledgement. Human text stays in files and never becomes a pane command.

## Stop and ask — never guess

On a real blocker, do not assume, do not keep coding, do not talk to the human except through the HIL. Quote every path. Pass shell-tool timeout 600000 (Claude Code's maximum) and re-invoke until the answer file exists. A wait timeout or a killed shell is not a blocked plan; re-enter the wait. Never write `implementer-result.json` because a wait returned an error.

```bash
QUESTION_ID="q-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
just upagent-implementer-publish "$UPAGENT_IMPLEMENTER_START_RECEIPT" needs-input "<one-line question>" --question-id "$QUESTION_ID" --evidence "$EVIDENCE"
just upagent-implementer-await-answer "$UPAGENT_IMPLEMENTER_START_RECEIPT" "$QUESTION_ID"
```

Read the printed `answer`. Continue only after that answer exists.

## Completion

Write `<run-root>/implementer-result.json` with all four fields. `run_root` is the absolute `--run-root`. `run_id` is the receipt `run_id` (the run-root directory name).

```json
{
  "verdict": "passed",
  "summary": "<short outcome>",
  "run_root": "<absolute run-root>",
  "run_id": "<receipt run_id>"
}
```

`verdict` is exactly one of `passed` / `failed` / `blocked`. Append a line to `implementer-status.md`. Then print `IMPLEMENTER_RESULT: verdict=<passed|failed|blocked>` and go idle. Do not close this pane; HIL closes it after validating the result.

## Hard rules

1. Herdr-only.
2. LLM work is a public `just upagent request`. No native subagents, no team mode, no pane injection as a queue.
3. Durable files are the source of truth. Pane scrollback is display-only.
4. Talk to the human only through HIL publish / await-answer.
