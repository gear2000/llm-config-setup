# /hil

Relay between the human and a plan-implementer. This pane is the **HIL agent** — the only pane the human talks to. Stay small: copy human text to the implementer, and implementer questions back to the human. Do not design, code, pick models beyond the flags already given, or spawn panes yourself.

## Invocation

```text
/hil --plan <plan.md> --offering <id> --effort <effort> [--no-supervise]
```

`--plan`, `--offering`, and `--effort` are required. Fail loud rather than guessing. `--offering` / `--effort` select the **plan-implementer** from `just upagent lists --type plan-implementers --json`, not a silent default and not the general worker list.

## Pre-flight

1. Verify `HERDR_ENV=1`. If not, stop with: `ERROR: /hil must run inside a Herdr-managed pane. Start Claude Code in a Herdr pane yourself; automation must not create this pane.`
2. Require `$HERDR_PANE_ID` and confirm it is a live pane. Never infer the caller from UI focus:

```bash
[[ "${HERDR_ENV:-}" == "1" ]] || {
  echo "ERROR: /hil must run inside a Herdr pane. Start Claude Code in a Herdr pane yourself; automation must not create this pane." >&2
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

3. Refuse before listing or starting unless `--offering` is literally present in the human's invocation. Stop with `ERROR: /hil requires an explicit --offering; supply an id from the plan-implementers list.` Never infer it from context, a previous run, or a default. Require a readable `--plan` file and an explicit `--effort`.
4. Run `just upagent lists --type plan-implementers --json`. The `--offering` id must exist in that list and `--effort` must be in that offering's `efforts` list. Fail loud if not. Do not use a ClaudeX or other worker-only offering as the controller. If the command fails with `no checked-out main branch UpAgent source found` or `ambiguous checked-out main branch UpAgent sources`, set `CHECKOUT` to this checkout's absolute root. Prefix **every** later `just upagent*` invocation in this pane with `UPAGENT_CANONICAL_REPO="$CHECKOUT"` — Claude Code's Bash tool does not persist `export` across calls. Report that fix rather than the raw traceback. Do not skip the list.
5. Echo `offering=<id> harness=<harness> effort=<effort> supervise=<bool>` using the literal invocation and validated listing. `supervise=false` only when `--no-supervise` was supplied.
6. Resolve the run tree next to the plan (or under the plan directory `/cc-plan` already created):

   ```text
   <plan-dir>/implementer/<run-id>/
   ```

   Create that directory. Copy nothing yourself except what `just upagent-implementer-start` copies. A failed prior start at the same run-root cannot be retried; mint a new run-id.

## Start + relay

Do not call `herdr pane split`, `herdr agent start`, `herdr pane run`, or type `/plan-implementer` into another pane. Python starts the implementer in this workspace's `control` tab.

```bash
UPAGENT_CANONICAL_REPO="$CHECKOUT" just upagent-implementer-start "$PLAN" "$OFFERING" "$EFFORT" "$RUN_ROOT"
```

Omit the `UPAGENT_CANONICAL_REPO="$CHECKOUT"` prefix when the list succeeded without it. When the prefix is required, use it on every later `just upagent-implementer-await`, `inject`, `ack`, `respond`, and `finish` as well. Python copies that env into the implementer's `start.sh` so hired work can run `just upagent` on a feature-branch checkout.

Pass `--no-supervise` through to the start recipe when supplied. Call both start and finish with shell `timeout: 600000`; Python validates their 480-second budgets.

Continue only after `IMPLEMENTER_STARTED` with `state: ready` or `state: ready-degraded` and a live `implementer_pane`. For `ready-degraded`, print the receipt's `startup_advisory` verbatim, then enter the await loop. The receipt is `"$RUN_ROOT"/control/implementer-start.json`. Quote every path.

Then loop with 60-second awaits. Pass `timeout: 600000` on the shell tool and `timeout_ms` 60000 so queued human input can be relayed after each return:

```bash
just upagent-implementer-await "$RECEIPT" "$AFTER" 60000
```

After every await return, including a heartbeat or interrupted await, first persist each new unsolicited human message exactly once. Use a file-writing tool to put its verbatim text in a temporary `$HUMAN_MESSAGE_FILE`. Human text must stay in files; never interpolate it into shell commands or pane commands. An answer to a pending `needs-input` question still uses `respond` below.

For each new message, run this file-only writer. `$RUN_ROOT` comes from the start receipt. The HIL is the sole sequence allocator. Keep acknowledged envelopes so sequence numbers never repeat after a restart. The human inbox is `<run-root>/inbox/`; `control/inbox/` is reserved for implementer events.

```bash
python3 - "$RUN_ROOT" "$HUMAN_MESSAGE_FILE" <<'PY'
import json, os, sys, time, uuid
from pathlib import Path

inbox = Path(sys.argv[1]) / "inbox"
inbox.mkdir(parents=True, exist_ok=True)
seq = 1 + max((int(p.stem.removeprefix("msg-")) for p in inbox.glob("msg-*.json")), default=0)
envelope = {"seq": seq, "text": Path(sys.argv[2]).read_text(), "at_ns": time.time_ns(), "acked": False}
path = inbox / f"msg-{seq}.json"
temporary = inbox / f".{path.name}.{uuid.uuid4().hex}.tmp"
with temporary.open("x", encoding="utf-8") as stream:
    os.chmod(temporary, 0o600)
    json.dump(envelope, stream, ensure_ascii=False)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
fd = os.open(inbox, os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
print(path)
PY
```

Then invoke injection whenever any envelope remains unacknowledged, even if this return brought no new human text:

```bash
just upagent-implementer-inject "$RECEIPT"
```

The response contains `outcome`, `reason`, `episode`, and `envelope_seqs` listing all unacknowledged messages. `delivered` means the fixed `read your inbox` prompt was sent, not that the messages were acknowledged. Leave the files untouched and retry while any envelope is unacknowledged. `not-idle`, `backoff`, `aborted`, and `nothing-pending` do not acknowledge messages. A `finished` outcome sends nothing; report any remaining unacknowledged messages to the human. An inject error must be shown, never silently dropped. Never send human text with `herdr pane run` or any other pane command.

Handle the JSON `kind`, then `just upagent-implementer-ack "$RECEIPT" "$EVENT_ID"`. Re-await with `after=<that event's sequence>` after every nonterminal event. Never derive a verdict from pane scrollback. An unrecognized `kind` is reported in full to the human, then stop fail-loud; do not guess. If the shell tool is killed, times out, or the await exits without event JSON, re-enter the same await with the same `after`. That is not a terminal verdict and not an unrecognized kind.

| `kind` | terminal | HIL action |
|---|---|---|
| `completed` | yes | Read `implementer-result.json`. `just upagent-implementer-finish "$RECEIPT"` closes only the recorded implementer pane. Tell the human, stop. |
| `failed` | yes | Show evidence. Finish/close the recorded implementer pane. Stop or restart only if the human says so (new run-id). |
| `blocked` | yes | Show evidence. Finish/close the recorded implementer pane. Do not guess a fix. |
| `cancelled` | yes | Show evidence. `just upagent-implementer-finish "$RECEIPT" --force` closes the recorded implementer pane even when no result file exists. Stop. |
| `hard-timeout` | yes | Show evidence. Finish with `--force`. Stop. Do not spawn a replacement. |
| `needs-input` | no | Quote the question and evidence paths to the human **verbatim**. Write their answer to a file. Then `just upagent-implementer-respond "$RECEIPT" "$QUESTION_ID" "$ANSWER_FILE"`. The `question-id` is the event `dedupe_key`. Ack, re-await. |
| `invalid-result` | no | Quote the event. The result file is not success. Ask the human; do not invent a verdict. Ack only after they decide. |
| `leader-missing` / `leader-stalled` | no | Report to the human. Do not spawn a replacement implementer. |
| `inactivity-checkpoint` | no | Note it; ack; re-await. |
| `await-heartbeat` | no | Re-await silently. Never narrate heartbeats. |
| `startup-ready` / `startup-degraded` | no | Record; ack; re-await. |
| `progress` / `advisory` / `worker-warning` / `worker-missing` | no | Show the full event to the human; ack; re-await. |
| `decision-required` / `soft-timeout` | no | Quote the event to the human; wait for their instruction; ack; re-await. |

After every finish recipe return, read `"$RUN_ROOT"/control/implementer-finish.json`. Print every `cleanup` entry verbatim. Read the event journal and print every post-terminal advisory with dedupe prefix `flow1:cleanup-failed:` verbatim before stopping.

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`. This pane is human-started, never `just run-start`.
2. Relay only. The plan-implementer hires UpAgent workers and owns the plan.
3. Durable files are the source of truth (`implementer-result.json`, the journal). Pane text is display-only.
4. Do not convert for Herdr, write `route.yaml`, or start `/tui-control`.
