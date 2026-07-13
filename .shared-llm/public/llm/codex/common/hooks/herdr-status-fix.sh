#!/bin/sh
# Custom, NOT managed by herdr — kept beside herdr-agent-state.sh per that file's own
# instruction ("add custom hooks beside this file instead of editing it").
#
# Why this exists: herdr's own codex integration (herdr-agent-state.sh) only reports
# pane.report_agent_session on SessionStart, which registers the pane's session id but never
# calls pane.report_agent with an explicit idle/working state. `herdr wait agent-status
# --status done` needs a prior explicit state report to have something to transition FROM when
# the codex process exits (herdr's exit handler only publishes a terminal status for panes it
# already knows have an agent in a live state) — session registration alone isn't enough.
# Root-caused live 2026-07-12: a Codex worker order finished and wrote a valid result.json, but
# the Recruiter's blocking wait on agent-status never fired and sat stuck for the full
# timeout. (The Recruiter also polls the result file for codex as a correctness fallback —
# this hook restores live status so waits and the sidebar work; neither depends on the other.)
#
# Usage: herdr-status-fix.sh <working|idle>
#
# INSTALL (per machine):
#   cp herdr-status-fix.sh ~/.codex/hooks/ && chmod +x ~/.codex/hooks/herdr-status-fix.sh
# then add SECOND hook entries to ~/.codex/hooks.json, alongside the existing ones — never
# replacing them (herdr reinstalls overwrite only its managed herdr-agent-state.sh):
#   SessionStart: {"type":"command","command":"bash '$HOME/.codex/hooks/herdr-status-fix.sh' working","timeout":10}
#   Stop:         {"type":"command","command":"bash '$HOME/.codex/hooks/herdr-status-fix.sh' idle","timeout":10}

set -eu

state="${1:-}"
hook_input_file="$(mktemp "${TMPDIR:-/tmp}/herdr-codex-status-fix.XXXXXX")" || exit 0
trap 'rm -f "$hook_input_file"' EXIT HUP INT TERM
cat >"$hook_input_file" 2>/dev/null || true

case "$state" in
  working|idle) ;;
  *) exit 0 ;;
esac

[ "${HERDR_ENV:-}" = "1" ] || exit 0
[ -n "${HERDR_SOCKET_PATH:-}" ] || exit 0
[ -n "${HERDR_PANE_ID:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

HERDR_STATE="$state" HERDR_HOOK_INPUT_FILE="$hook_input_file" python3 - <<'PY'
import json
import os
import random
import socket
import time

source = "herdr:codex-status-fix"
state = os.environ.get("HERDR_STATE", "")
pane_id = os.environ.get("HERDR_PANE_ID")
socket_path = os.environ.get("HERDR_SOCKET_PATH")
hook_input_file = os.environ.get("HERDR_HOOK_INPUT_FILE")

if not pane_id or not socket_path or state not in ("working", "idle"):
    raise SystemExit(0)

hook_input = {}
if hook_input_file:
    try:
        with open(hook_input_file, encoding="utf-8") as handle:
            content = handle.read()
        if content.strip():
            hook_input = json.loads(content)
    except Exception:
        hook_input = {}

session_id = hook_input.get("session_id")
agent_session_id = session_id if isinstance(session_id, str) and session_id else None

request_id = f"{source}:{int(time.time() * 1000)}:{random.randrange(1_000_000):06d}"
report_seq = time.time_ns()
params = {
    "pane_id": pane_id,
    "source": source,
    "agent": "codex",
    "state": state,
    "seq": report_seq,
}
if agent_session_id:
    params["agent_session_id"] = agent_session_id
request = {
    "id": request_id,
    "method": "pane.report_agent",
    "params": params,
}

try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.5)
    client.connect(socket_path)
    client.sendall((json.dumps(request) + "\n").encode())
    try:
        client.recv(4096)
    except Exception:
        pass
    client.close()
except Exception:
    pass
PY
