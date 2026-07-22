# Run an ad-hoc UpAgent request

Use only the repository's canonical `just upagent` façade. Do not start another service, launch an exploration session, steer a live worker, or route silently by account/quota.

1. Treat `$ARGUMENTS` as the complete bounded task. If it does not state a task, ask for one before launching.
2. Run `just upagent lists --type offerings --json`. Select one existing offering id and one effort that its `efforts` list permits. Honor an explicit user choice; otherwise make a task-based choice and state it. Do not invent an id or probe provider accounts.
3. Select one existing persona appropriate to the task from the repository/home agent definitions. Honor an explicit persona; fail loud if it does not exist. Do not create a persona as part of this command.
4. Resolve the current repository directory with `pwd -P`. For read-only commands, tests, audits, and investigations, use that absolute path as `--cwd` unless the user supplied another existing absolute repository directory. If the task may edit files, do not hand a worker an unisolated primary checkout by default: require an explicitly approved existing worktree or ask the user whether to create/use one before dispatch.
5. Generate a canonical UUID with Python. Resolve `${XDG_STATE_HOME:-$HOME/.local/state}` to an absolute path, set `umask 077`, create `<state-root>/upagent/runs/<request-id>` with mode `0700`, and write the full task to `<run-dir>/prompt.md` as UTF-8 with mode `0600`. Keep this caller-owned directory; never put it in the Hub ledger.
6. Submit asynchronously first so the originating response returns while the request is active. Redirect that response straight to a mode-`0600` file—never print it through `tee`, paste it into chat, or expose it in tool output:

   ```bash
   umask 077
   response="$run_dir/request.json"
   if just upagent request --type worker --request-id "$request_id" \
     --offering "$offering" --effort "$effort" --agent "$persona" \
     --prompt-file "$run_dir/prompt.md" --cwd "$cwd" --json \
     >"$response"; then
     request_rc=0
   else
     request_rc=$?
   fi
   chmod 600 "$response"
   ```

7. Before any await, use a local Python helper to atomically extract `.state.requester_control_token` into `<run-dir>/control-token` with mode `0600`, remove that field from `request.json`, and atomically replace the response with the redacted value. Refuse to print or load the unredacted response into the conversation. If the request terminalized before reaching healthy `running`, the token may be absent; record that fact and do not invent one.
8. When the redacted response says the request is active, block without burning LLM turns and preserve the terminal exit code:

   ```bash
   if just upagent await --request "$request_id" --json \
     >"$run_dir/terminal.json"; then
     await_rc=0
   else
     await_rc=$?
   fi
   chmod 600 "$run_dir/terminal.json"
   ```

9. Read the redacted response and terminal JSON, then read any existing `result`, `compacted`, and `handoff` artifact paths. Summarize the verdict, reason/revisit, compacted outcome, handoff, full-log pointer, request id, chosen offering/effort/persona, run directory, and nonzero `request_rc`/`await_rc` when applicable. A blocked or failed verdict is a real outcome, not permission to relaunch silently.
10. Retain the private token file so `/upagent-cancel` can authenticate without asking the human to paste a token. For later lifecycle actions use `/upagent-get`, `/upagent-cancel`, or `/upagent-cleanup` against the same request. Do not delete the caller-owned run directory.
