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
# Worker hire contract

Use the public façade. Place work only with `just upagent request`.

`$HERDR_PANE_ID` is `cockpit_pane` on every hire. Do not infer the caller from UI focus.

## Select offering, effort, persona

1. Run `just upagent lists --type offerings --json`. Pick one existing id and one effort that offering permits. Do not invent an id.
2. Pick one existing persona from the repository/home agent definitions. Fail loud if it does not exist. Typical: a domain agent for implementation, `reviewer` or `adversarial-evaluator` for independent review. Do not create a persona.
3. `--offering` on `/plan-implementer` is this controller's model, not the worker's. Choose worker offerings independently.

## Isolation

- Read-only work (review, audit, investigate, test without writes): `--cwd` is `pwd -P` or another existing absolute repository directory the human named.
- Work that may edit files: do not hand the primary checkout to the worker. Use an explicitly approved existing worktree, or ask the HIL whether to create/use one, then pass that absolute path as `--cwd`.
- Only one mutating worker at a time against a given checkout. Further hires against that tree wait until the writer finishes.

## Brief

Generate a UUID. Create `${XDG_STATE_HOME:-$HOME/.local/state}/upagent/runs/<request-id>/` mode `0700`. Write `prompt.md` mode `0600` through the `writing-for-agents` skill. Every brief includes:

```text
You are an ordinary UpAgent worker. You may not place UpAgent work orders, start panes, or hire anyone. Only this plan-implementer controller places work. Consult a specialist only when this brief names the specialist and the consult command.
```

Keep this caller-owned directory. Never put it in the Recruiter ledger.

## Submit and await

Build cockpit args once from `$HERDR_PANE_ID` (live-pane check, same as `/upagent-run`). Then:

```bash
umask 077
response="$run_dir/request.json"
if just upagent request --type worker --request-id "$request_id" \
  --offering "$offering" --effort "$effort" --agent "$persona" \
  --prompt-file "$run_dir/prompt.md" --cwd "$cwd" \
  --cockpit-pane "$HERDR_PANE_ID" --json >"$response"; then
  request_rc=0
else
  request_rc=$?
fi
chmod 600 "$response"
if [[ "$request_rc" -ne 0 ]]; then
  echo "ERROR: just upagent request failed (exit $request_rc). See $response. Do not await an unregistered request." >&2
  printf '%s\n' "hire failed: request exit $request_rc" >> implementer-status.md
  exit "$request_rc"
fi
```

Redact `.state.requester_control_token` into `$run_dir/control-token` mode `0600` before any further use of `request.json`. Then:

```bash
just upagent await --request "$request_id" --json >"$run_dir/terminal.json"
chmod 600 "$run_dir/terminal.json"
```

Read the redacted response, terminal JSON, and any `result` / `compacted` / `handoff` pointers. A blocked or failed verdict is a real outcome, not permission to relaunch silently.

## Consults

`just upagent-consult` and `just upagent-specialists` stay on this controller. Ordinary workers do not nest-hire.
