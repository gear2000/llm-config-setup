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
