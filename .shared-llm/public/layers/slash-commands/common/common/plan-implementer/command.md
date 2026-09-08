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
