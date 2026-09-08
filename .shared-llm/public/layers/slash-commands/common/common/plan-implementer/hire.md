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

Redact `.state.requester_control_token` into `$run_dir/control-token` mode `0600`, then remove that field from `request.json` before any further use.

After every accepted response, including attachment to an existing request, register it
before awaiting. `$run_root` is the absolute `--run-root` supplied to this implementer,
not `$run_dir` and never a directory inferred from cwd. Two runs can share one cwd.

```bash
just upagent-register-worker "$run_root" "$response"
```

This atomically writes `<run-root>/control/workers/<request-id>.json` with only
`request_id` from `.request_id`, `payload_sha256` from `.payload_sha256`, `order_id`
from `.state.order_id`, `generation` from `.state.generation`, and `placed_at_ns`.
It stores no `cockpit_pane` or control token. An identical attachment preserves the
record and its placement time. Identity or retry-generation mismatch fails loud;
do not overwrite the previous generation or silently await it. Resolve that mismatch
with the HIL. A registration failure stops this hire's await path.

Then:

```bash
just upagent await --request "$request_id" --json >"$run_dir/terminal.json"
chmod 600 "$run_dir/terminal.json"
```

Read the redacted response, terminal JSON, and any `result` / `compacted` / `handoff` pointers. A blocked or failed verdict is a real outcome, not permission to relaunch silently.

## Consults

`just upagent-consult` and `just upagent-specialists` stay on this controller. Ordinary workers do not nest-hire.
