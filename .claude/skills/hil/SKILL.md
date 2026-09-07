---
name: hil
description: Human-in-the-loop Claude Code proxy for Flow 1 under HERDR_ENV=1. Relays between the human (including Claude Code remote app) and a plan-implementer that hires UpAgent workers from an approved plan.md. Use when executing after `/cc-plan`, or when the user says `/hil`, HIL, or plan-implementer.
argument-hint: --plan <plan.md> --offering <id> --effort <effort>
---

# /hil

Relay between the human and a plan-implementer. This pane is the **HIL agent** — the only pane the human talks to. Stay small: copy human text to the implementer, and implementer questions back to the human. Do not design, code, pick models beyond the flags already given, or spawn panes yourself.

## Invocation

```text
/hil --plan <plan.md> --offering <id> --effort <effort>
```

`--plan`, `--offering`, and `--effort` are required. Fail loud rather than guessing. `--offering` / `--effort` select the **plan-implementer** from `just upagent lists --type offerings --json`, not a silent default.

## Pre-flight

1. Verify `HERDR_ENV=1`. If not, stop with: `ERROR: /hil must run inside a Herdr-managed pane. Start Claude Code in a Herdr pane yourself; automation must not create this pane.`
2. Require `$HERDR_PANE_ID` and confirm it is a live pane. Never infer the caller from UI focus:

```bash
[[ "${HERDR_ENV:-}" == "1" ]] || {
  echo "ERROR: /hil must run inside a Herdr-managed pane. Start Claude Code in a Herdr pane yourself; automation must not create this pane." >&2
  exit 1
}
[[ -n "${HERDR_PANE_ID:-}" ]] || {
  echo "ERROR: HERDR_ENV=1 but HERDR_PANE_ID is missing; refusing to infer the caller from UI focus." >&2
  exit 1
}
pane_list_json="$(herdr pane list)" || {
  echo "ERROR: could not list panes in the current Herdr session." >&2
  exit 1
}
if ! HERDR_EXPECTED_PANE="$HERDR_PANE_ID" python3 -c '
import json, os, sys
response = json.load(sys.stdin)
panes = response.get("result", {}).get("panes", [])
expected = os.environ["HERDR_EXPECTED_PANE"]
raise SystemExit(0 if any(isinstance(pane, dict) and pane.get("pane_id") == expected for pane in panes) else 1)
' <<<"$pane_list_json"; then
  echo "ERROR: HERDR_PANE_ID is not a live pane in the current Herdr session." >&2
  exit 1
fi
```

3. Require a readable `--plan` file.
4. Run `just upagent lists --type offerings --json`. The `--offering` id must exist and `--effort` must be in that offering's `efforts` list. Fail loud if not.
5. Resolve the run tree next to the plan (or under the plan directory `/cc-plan` already created):

   ```text
   <plan-dir>/implementer/<run-id>/
   ```

   Create that directory. Copy nothing yourself except what `just upagent-implementer-start` copies.

## Start + relay

Do not call `herdr pane split`, `herdr agent start`, `herdr pane run`, or type `/plan-implementer` into another pane. Python starts the implementer in this pane's `control` tab.

```text
just upagent-implementer-start <plan.md> <offering> <effort> <run-root>
```

Continue only after `IMPLEMENTER_STARTED` with `state: ready` and a live `implementer_pane`. The receipt is `<run-root>/control/implementer-start.json`.

Then loop:

```bash
just upagent-implementer-await <run-root>/control/implementer-start.json <after> [timeout-ms]
```

Handle the JSON `kind`, then `just upagent-implementer-ack <receipt> <event_id>`. Re-await with `after=<that event's sequence>` after every nonterminal event. Never derive a verdict from pane scrollback.

| `kind` | terminal | HIL action |
|---|---|---|
| `completed` | yes | Read `implementer-result.json`, tell the human, stop. |
| `failed` | yes | Show evidence, stop or restart only if the human says so. |
| `blocked` | yes | Show evidence. Do not guess a fix. |
| `needs-input` | no | Quote the question and evidence paths to the human **verbatim**. Write their answer to a file. Then `just upagent-implementer-respond <receipt> <question-id> <answer-file>`. The `question-id` is the event `dedupe_key`. Ack, re-await. |
| `leader-missing` / `leader-stalled` | no | Report to the human. Do not spawn a replacement implementer. |
| `inactivity-checkpoint` | no | Note it; ack; re-await. |
| `await-heartbeat` | no | Re-await silently. Never narrate heartbeats. |

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`. This pane is human-started, never `just run-start`.
2. Relay only. The plan-implementer hires UpAgent workers and owns the plan.
3. Durable files are the source of truth (`implementer-result.json`, the journal). Pane text is display-only.
4. Do not convert for Herdr, write `route.yaml`, or start `/tui-control`.
