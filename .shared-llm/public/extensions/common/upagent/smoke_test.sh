#!/usr/bin/env bash
# UpAgent Hub live smoke test — exercises the REAL lifecycle end to end:
#
#   1. hub up (canonical socket, identity handshake)
#   2. specialist roster listing over the socket
#   3. one real consultation  (specialist worker in a Herdr pane; answer must be `cited`)
#   4. one real worker request (public offering + Account Manager; verdict must be `passed`)
#
# This costs real model calls and needs a running Herdr server. Run from the repo
# that owns the hub checkout:
#
#   HERDR_SESSION=default bash .shared-llm/public/extensions/common/upagent/smoke_test.sh
#
# Exit 0 only when every step passes. Artifacts land in a fresh temp dir printed at start.
set -euo pipefail

HERDR_SESSION="${HERDR_SESSION:-default}"
export HERDR_SESSION
REPO_ROOT="$(git rev-parse --show-toplevel)"
SPECIALIST="${UPAGENT_SMOKE_SPECIALIST:-backend}"
OFFERING="${UPAGENT_SMOKE_OFFERING:-claude-sonnet-5}"
RUN_ID="$(date +%s)"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/upagent-smoke.XXXXXX")"
FAILURES=0

say() { printf '\n=== %s\n' "$*"; }
pass() { printf 'PASS  %s\n' "$*"; }
fail() {
	printf 'FAIL  %s\n' "$*"
	FAILURES=$((FAILURES + 1))
}

say "UpAgent smoke test — repo: $REPO_ROOT, session: $HERDR_SESSION, artifacts: $WORK_DIR"
cd "$REPO_ROOT"

# 1. Hub up + identity ------------------------------------------------------
say "1/4 hub up"
just upagent up >/dev/null
STATUS="$(just upagent status 2>/dev/null)"
if grep -q '^services_ready: True' <<<"$STATUS" && grep -q '^process_start_time: [0-9]' <<<"$STATUS"; then
	pass "hub is up with live process identity"
else
	fail "hub status missing services_ready/process_start_time"
	printf '%s\n' "$STATUS"
fi

# 2. Specialist roster ------------------------------------------------------
say "2/4 specialist roster"
ROSTER="$(just upagent lists --type specialists 2>/dev/null | grep -c . || true)"
if [ "$ROSTER" -ge 1 ] && just upagent lists --type specialists 2>/dev/null | grep -q "^${SPECIALIST} "; then
	pass "roster lists $ROSTER specialists (includes '$SPECIALIST')"
else
	fail "roster missing or lacks '$SPECIALIST'"
fi

# 3. Consultation -----------------------------------------------------------
say "3/4 consultation (specialist: $SPECIALIST — launches a real worker pane)"
cat >"$WORK_DIR/consult.json" <<EOF
{
  "consult_id": "smoke-consult-$RUN_ID",
  "specialist": "$SPECIALIST",
  "question": "In this repository, which YAML file defines the UpAgent specialist roster, and what is the name of its first specialist entry? Cite exact file:line locations.",
  "answer_path": "$WORK_DIR/answer.json",
  "cwd": "$REPO_ROOT"
}
EOF
if just upagent-consult "$WORK_DIR/consult.json" >"$WORK_DIR/consult.out" 2>&1 &&
	grep -q '"answer_verdict": "cited"' "$WORK_DIR/consult.out"; then
	pass "consult answered with cited evidence ($WORK_DIR/answer.json)"
else
	fail "consult did not produce a cited answer — see $WORK_DIR/consult.out"
fi

# 4. Worker request ---------------------------------------------------------
say "4/4 worker request (offering: $OFFERING — launches manager + worker panes)"
cat >"$WORK_DIR/worker-brief.md" <<EOF
# Task
In the repository at $REPO_ROOT, count how many specialists are defined in
.shared-llm/public/extensions/common/upagent/specialists.yaml and list their names in compacted.md.
Write a one-line handoff.md. This is a read-only task: change no files.
Report verdict "passed" if you could read the roster, otherwise "blocked".
EOF
if just upagent request --type worker --offering "$OFFERING" --effort low --agent backend \
	--prompt-file "$WORK_DIR/worker-brief.md" --cwd "$REPO_ROOT" --wait --json \
	>"$WORK_DIR/worker-out.json" 2>"$WORK_DIR/worker-err.txt" &&
	grep -q '"verdict": "passed"' "$WORK_DIR/worker-out.json"; then
	pass "worker ran to a passed terminal verdict"
else
	fail "worker request did not pass — see $WORK_DIR/worker-out.json / worker-err.txt"
fi

# Summary --------------------------------------------------------------------
say "summary"
if [ "$FAILURES" -eq 0 ]; then
	printf 'SMOKE OK — all 4 stages passed. Artifacts: %s\n' "$WORK_DIR"
else
	printf 'SMOKE FAILED — %d stage(s) failed. Artifacts: %s\n' "$FAILURES" "$WORK_DIR"
	exit 1
fi
