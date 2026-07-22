# Cancel an UpAgent request

Require exactly one canonical request id in `$ARGUMENTS`. Never substitute a lease token, timeout nonce, provider credential, or token from another request.

1. Look for the private mode-`0600` token created by `/upagent-run` at `${XDG_STATE_HOME:-$HOME/.local/state}/upagent/runs/<request-id>/control-token`. Refuse if the file is absent, not a regular file, is owned by another user, or is group/world accessible. Do not read its contents into chat or expose them in tool output.
2. Pass the absolute private path—not the token value—to the canonical façade, and redirect the JSON outcome to a private file:

   ```bash
   umask 077
   just upagent cancel --request "$request_id" \
     --control-token-file "$run_dir/control-token" --json \
     >"$run_dir/cancel.json"
   chmod 600 "$run_dir/cancel.json"
   ```

3. Report whether this invocation published the ordinary schema-valid blocked/cancelled terminal bundle or authenticated and returned an already-published terminal result.

A stale/wrong token or identity-cleanup refusal fails loud. Do not ask the human to paste the token into chat, retry with guessed tokens, kill processes/panes directly, steer the worker, or use cleanup as cancellation. Requests launched outside `/upagent-run` must be cancelled through a trusted caller that already holds a private token file; this skill must not scrape a token from Hub state or transcripts.
