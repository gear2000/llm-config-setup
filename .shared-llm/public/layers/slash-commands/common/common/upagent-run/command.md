# Run an ad-hoc UpAgent request

Use only the repository's canonical `just upagent` façade. Do not start another service, launch an exploration session, steer a live worker, or route silently by account/quota.

## Mandatory caller-pane anchoring inside Herdr

Before routing or submission, build the invocation-only cockpit arguments exactly once:

```bash
cockpit_args=()
if [[ "${HERDR_ENV:-}" == "1" ]]; then
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
  cockpit_args=(--cockpit-pane "$HERDR_PANE_ID")
fi
```

This is a hard routing boundary. `HERDR_PANE_ID` is the caller's environment-provided pane identity;
UI focus can move, and the UpAgent services pointer can name a different or stale pane. Never use
`herdr pane current`, focus inspection, or the services pointer to infer the caller. The public API
repeats the live-pane check before registration. Outside Herdr, leave `cockpit_args` empty so the
existing service-pane resolution/creation behavior remains unchanged.

1. Parse optional leading controls from `$ARGUMENTS`: `--duration-minutes N` (integer 1–120; omitted keeps the 60-minute default) and `--keep-open`. Treat the remaining text as the complete bounded task. If no task remains, ask for one before launching. `--keep-open` is for a managed TUI controller or phase leader that will inspect checkpoints and explicitly continue or release the same worker; do not use it for an unattended ad-hoc request.
2. Run `just upagent lists --type offerings --json`. Select one existing offering id and one effort that its `efforts` list permits. Honor an explicit user choice; otherwise make a task-based choice and state it. Do not invent an id or probe provider accounts.
3. Select one existing persona appropriate to the task from the repository/home agent definitions. Honor an explicit persona; fail loud if it does not exist. Do not create a persona as part of this command.
4. Resolve the current repository directory with `pwd -P`. For read-only commands, tests, audits, and investigations, use that absolute path as `--cwd` unless the user supplied another existing absolute repository directory. If the task may edit files, do not hand a worker an unisolated primary checkout by default: require an explicitly approved existing worktree or ask the user whether to create/use one before dispatch.
5. Generate a canonical UUID with Python. Resolve `${XDG_STATE_HOME:-$HOME/.local/state}` to an absolute path, set `umask 077`, create `<state-root>/upagent/runs/<request-id>` with mode `0700`, and write the full task to `<run-dir>/prompt.md` as UTF-8 with mode `0600`. Author `prompt.md` through the `writing-for-agents` skill: the worker reads it cold, so front-load the goal, end each step on a checkable completion criterion, and carry enough context that the worker understands the overall agreed work, not just the isolated task. Keep this caller-owned directory; never put it in the Recruiter ledger.
6. Submit asynchronously first so the originating response returns while the request is active. Redirect that response straight to a mode-`0600` file—never print it through `tee`, paste it into chat, or expose it in tool output:

   ```bash
   umask 077
   response="$run_dir/request.json"
   extra_args=()
   [[ -n "${duration_minutes:-}" ]] && extra_args+=(--duration-minutes "$duration_minutes")
   [[ -n "${keep_open:-}" ]] && extra_args+=(--keep-open)
   if just upagent request --type worker --request-id "$request_id" \
     --offering "$offering" --effort "$effort" --agent "$persona" \
     --prompt-file "$run_dir/prompt.md" --cwd "$cwd" \
     "${cockpit_args[@]}" "${extra_args[@]}" --json >"$response"; then
     request_rc=0
   else
     request_rc=$?
   fi
   chmod 600 "$response"
   ```

   If this pre-acceptance call fails with `cockpit_pane_not_found`, do not mint a new request ID
   or relaunch work. Run `just upagent up` when using service-pane fallback, rebuild
   `cockpit_args` from the current environment/live pane check above, and retry the exact same
   request command with the same ID. UpAgent refreshes only unaccepted placement routing; an
   already-accepted or terminal attachment ignores a new pane and preserves its exact order.

7. Before any await, use a local Python helper to atomically extract `.state.requester_control_token` into `<run-dir>/control-token` with mode `0600`, remove that field from `request.json`, and atomically replace the response with the redacted value. Refuse to print or load the unredacted response into the conversation. If the request terminalized before reaching healthy `running`, the token may be absent; record that fact and do not invent one.
8. For ordinary one-shot work, block without burning LLM turns with `just upagent await --request "$request_id" --json >"$run_dir/terminal.json"`, preserving its exit code and setting mode `0600` as before.
9. For `--keep-open`, do **not** call terminal `await` yet. Block on `just upagent review-await --request "$request_id" --after "$last_sequence" --json`. Record both `review.latest_sequence` and `review.checkpoint_sha256`, then inspect that exact checkpoint and the actual worktree diff/tests. Then make exactly one requester-owned decision:
   - Continue the same live worker: write concrete feedback to a mode-`0600` file and run `just upagent review-continue --request "$request_id" --checkpoint "$sequence" --checkpoint-sha256 "$checkpoint_sha256" --prompt-file "$feedback" --control-token-file "$run_dir/control-token" --json`; await the next checkpoint.
   - Accept and finalize: run `just upagent review-release --request "$request_id" --checkpoint "$sequence" --checkpoint-sha256 "$checkpoint_sha256" --control-token-file "$run_dir/control-token" --json`, then call ordinary `await` for the terminal receipt.
   - Cancel: use `/upagent-cancel` against the same request.
   Only release authorizes terminal worker artifacts and pane cleanup. The original duration covers both coding and review; a timeout still requires the existing authenticated extend/cancel decision.
10. Read the redacted response and terminal JSON, then read any existing `result`, `compacted`, and `handoff` artifact paths. Summarize the verdict, reason/revisit, compacted outcome, handoff, full-log pointer, request id, chosen offering/effort/persona, requested duration, run directory, and nonzero request/await codes when applicable. A blocked or failed verdict is a real outcome, not permission to relaunch silently.
11. Retain the private token file so `/upagent-cancel` and retained review actions can authenticate without asking the human to paste a token. For later lifecycle actions use `/upagent-get`, `/upagent-cancel`, or `/upagent-cleanup` against the same request. Do not delete the caller-owned run directory.
