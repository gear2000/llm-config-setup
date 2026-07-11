Route this request to the `codex:codex-rescue` subagent. The underlying
runtime is the same as `/codex:rescue`. Only the framing differs: this is
**routine delegation**, not a rescue.

The final user-visible response must be Codex's output verbatim.

Raw user request:
$ARGUMENTS

Framing rule:

- Treat the task as a normal piece of work being handed to a peer agent,
  not as a takeover after Claude failed. If you prepend any framing string
  to the prompt before forwarding (most invocations should NOT), use
  something neutral like "Routine delegation:" — never "Claude is stuck"
  or "Rescue request".

Execution mode:

- If the request includes `--background`, run the `codex:codex-rescue`
  subagent in the background.
- If the request includes `--wait`, run the `codex:codex-rescue` subagent
  in the foreground.
- If neither flag is present, default to foreground.
- `--background` and `--wait` are execution flags for Claude Code. Do not
  forward them to `task`, and do not treat them as part of the
  natural-language task text.
- `--model` and `--effort` are runtime-selection flags. Preserve them for
  the forwarded `task` call, but do not treat them as part of the
  natural-language task text.
- If the request includes `--resume`, do not ask whether to continue.
- If the request includes `--fresh`, do not ask whether to continue.
- Otherwise, since this is delegation (not continuation), default to a
  fresh Codex thread. Do not run the resume-candidate check unless the
  user clearly indicated they want to continue prior delegation
  ("keep going", "continue what Codex started", etc.). For ambiguous
  cases, prefer starting fresh — that matches the delegation framing.

Operating rules:

- The subagent is a thin forwarder only. It should use one `Bash` call to
  invoke `node "${CLAUDE_PLUGIN_ROOT}/scripts/codex-companion.mjs" task ...`
  inside the codex plugin's environment, and return that command's stdout
  as-is.
- Return the Codex companion stdout verbatim to the user. Do not
  paraphrase, summarize, rewrite, or add commentary before or after it.
- Do not ask the subagent to inspect files, monitor progress, poll
  `/codex:status`, fetch `/codex:result`, call `/codex:cancel`, summarize
  output, or do follow-up work of its own.
- Leave `--effort` unset unless the user explicitly asks for a specific
  reasoning effort.
- Leave the model unset unless the user explicitly asks for one. If they
  ask for `spark`, map it to `gpt-5.3-codex-spark`.
- Leave `--resume` and `--fresh` in the forwarded request — the subagent
  handles routing when it builds the `task` command.
- If the codex helper reports Codex is missing or unauthenticated, stop
  and tell the user to run `/codex:setup`.
- If the user did not supply a task, ask what Codex should handle.

Relationship to `/codex:rescue`:

- `/codex:rescue` is the right door when Claude is stuck, has tried and
  failed, or the situation has an emergency feel. Codex bails Claude out.
- `/codex-delegate` is the right door for routine handoff. Same runtime,
  no rescue framing. Use this when you want Codex to do a piece of work
  as a peer, not as a savior.
- Internally they share the codex-companion runtime and the
  `codex:codex-rescue` subagent. The split exists so the caller signals
  intent clearly and so the framing the model sees (when added) is
  consistent.
