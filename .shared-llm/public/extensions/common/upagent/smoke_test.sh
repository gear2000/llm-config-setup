#!/usr/bin/env bash
# Cohesive local UpAgent integration smoke. One request crosses the real client CLI, a real
# detached recruiter.py run-job subprocess, fake local Herdr IPC, typed publication/receipt,
# oversight cleanup, runner-completed proof, and reconciliation. No model/network/AWS calls.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_CHECKOUT="$(cd "$HERE/../../../../.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/upagent-smoke.XXXXXX")"
BIN="$WORK/bin"
STATE="$WORK/fake-herdr"
mkdir -p "$BIN" "$STATE"

cat >"$BIN/herdr" <<'PY'
#!/usr/bin/env python3
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

root = Path(os.environ["UPAGENT_SMOKE_STATE"])
state_path = root / "state.json"
lock_path = root / "state.lock"
args = sys.argv[1:]
if args[:1] == ["--session"]:
    args = args[2:]


def locked_state():
    lock = lock_path.open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    state = json.loads(state_path.read_text()) if state_path.is_file() else {"panes": {}}
    return lock, state


def save(lock, state):
    state_path.write_text(json.dumps(state))
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()


def emit(value):
    print(json.dumps(value), flush=True)

if args == ["session", "list", "--json"]:
    emit({"sessions": [{"name": os.environ["HERDR_SESSION"], "running": True,
                         "socket_path": os.environ["HERDR_SOCKET_PATH"]}]})
    raise SystemExit(0)
if args[:2] == ["agent", "wait"]:
    signal.signal(signal.SIGTERM, lambda *_: raise_exit())
    while True:
        time.sleep(1)

def raise_exit():
    raise SystemExit(143)

lock, state = locked_state()
try:
    if args[:2] == ["pane", "get"]:
        pane_id = args[2]
        pane = state["panes"].get(pane_id)
        if pane is None and pane_id == "cockpit-pane":
            pane = {"pane_id": pane_id, "tab_id": "control-tab", "cwd": os.getcwd()}
        if pane is None:
            print("pane_not_found", file=sys.stderr)
            raise SystemExit(1)
        emit({"result": {"pane": pane}})
    elif args[:2] == ["pane", "process-info"]:
        pane_id = args[args.index("--pane") + 1]
        pane = state["panes"][pane_id]
        emit({"result": {"process_info": {"foreground_processes": [{
            "pid": pane["pid"], "name": "claude", "cmdline": "claude local-smoke"
        }]}}})
    elif args[:2] == ["pane", "split"]:
        # Herdr 0.7.5: the pane is made first (carrying cwd/env); the agent starts later.
        cwd = args[args.index("--cwd") + 1]
        pane_id = "pane-" + str(len(state["panes"]) + 1)
        state["panes"][pane_id] = {
            "agent": None, "agent_name": None, "agent_status": "unknown",
            "cwd": cwd, "foreground_cwd": cwd, "pane_id": pane_id, "pid": None,
            "tab_id": "worker-tab", "workspace_id": "w1"
        }
        emit({"result": {"pane": dict(state["panes"][pane_id])}})
    elif args[:2] == ["agent", "start"]:
        name = args[2]
        kind = args[args.index("--kind") + 1]
        pane_id = args[args.index("--pane") + 1]
        pane = state["panes"][pane_id]
        command = [kind, *args[args.index("--") + 1:]]
        process = subprocess.Popen(command, cwd=pane["cwd"], env=os.environ.copy(), start_new_session=True)
        pane.update({"agent": kind, "agent_name": name, "agent_status": "working", "pid": process.pid})
        emit({"result": {"agent": {"name": name, "pane_id": pane_id, "workspace_id": "w1"}}})
    elif args[:2] == ["pane", "list"]:
        emit({"result": {"panes": list(state["panes"].values())}})
    elif args[:2] == ["pane", "close"]:
        state["panes"].pop(args[2], None)
        emit({"result": {"closed": True}})
    elif args[:2] == ["pane", "report-agent"]:
        emit({"result": {"reported": True}})
    elif args[:2] == ["agent", "get"]:
        name = args[2]
        pane = next((item for item in state["panes"].values()
                     if item.get("agent_name") == name), None)
        if pane is None:
            print("agent_not_found", file=sys.stderr)
            raise SystemExit(1)
        emit({"result": {"agent": {"name": name, "pane_id": pane["pane_id"]}}})
    else:
        print("unsupported fake herdr command: " + " ".join(args), file=sys.stderr)
        raise SystemExit(2)
finally:
    save(lock, state)
PY
chmod +x "$BIN/herdr"

cat >"$WORK/fake_worker.py" <<'PY'
#!/usr/bin/env python3
import json
import re
import sys
import time
from pathlib import Path

text = Path(sys.argv[1]).read_text()
def value(label):
    match = re.search(rf"^- {label}: (.+)$", text, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"missing {label} path")
    return Path(match.group(1))
order = re.search(r'`order_id`: exactly "([^"]+)"', text).group(1)
time.sleep(0.2)
for path in (value("result.json"), value("compacted.md"), value("handoff.md")):
    path.parent.mkdir(parents=True, exist_ok=True)
value("result.json").write_text(json.dumps({
    "order_id": order, "verdict": "passed", "full_log": "local-smoke"
}) + "\n")
value("compacted.md").write_text("# Local smoke result\n")
value("handoff.md").write_text("Local smoke completed.\n")
PY
chmod +x "$WORK/fake_worker.py"

# Herdr 0.7.5 starts the kind's canonical executable itself, so the roster command must begin
# with `claude`; this shim on the fake herdr's PATH stands in for the real harness.
cat >"$BIN/claude" <<SH
#!/usr/bin/env bash
exec python3 "$WORK/fake_worker.py" "\$@"
SH
chmod +x "$BIN/claude"

cat >"$WORK/roster.yaml" <<EOF
management:
  mode: direct
  rescue_on_startup_failure: false
  startup_timeout_ms: 5000
  inactivity_check_ms: 60000
  requester_grace_ms: 1000
  account_manager:
    command: "unused {brief_path} {output_path}"
    expected_agent: claude
    expected_process: claude
    timeout_ms: 1000
  checker:
    command: "unused {brief_path} {output_path}"
    expected_agent: claude
    expected_process: claude
    timeout_ms: 1000
health:
  claude:
    expected_agent: claude
    expected_process: claude
harnesses:
  claude: "claude {instructions_path}"
EOF

cat >"$WORK/instructions.md" <<'EOF'
# Local smoke worker
Produce the typed artifacts required by the appended Recruiter delivery contract.
EOF
cat >"$WORK/order.json" <<EOF
{
  "order_id": "local-smoke.stage-1.try-1",
  "phase_id": "phase-0",
  "stage_id": "stage-1-implementation",
  "harness": "claude",
  "model": "local-fake",
  "agent": "backend",
  "cwd": "$WORK",
  "instructions_path": "$WORK/instructions.md",
  "result_path": "$WORK/result.json",
  "cockpit_pane": "cockpit-pane",
  "sentinel": false,
  "timeout_ms": 10000
}
EOF

export PATH="$BIN:$PATH"
export UPAGENT_SMOKE_STATE="$STATE"
export HERDR_SESSION="local-smoke"
export HERDR_SOCKET_PATH="$STATE/herdr.sock"
export UPAGENT_CANONICAL_REPO="$SOURCE_CHECKOUT"
export UPAGENT_RUNTIME_DIR="$WORK/runtime"
export UPAGENT_HUB_DIR="$WORK/ledger"
export UPAGENT_STATE="$WORK/services.json"

python3 "$HERE/client.py" --target recruiter --roster "$WORK/roster.yaml" \
	request "$WORK/order.json" >"$WORK/request.out"
python3 "$HERE/client.py" --target recruiter await "$WORK/order.json" \
	--notify-after-ms 0 >"$WORK/await.out"
python3 "$HERE/client.py" --target recruiter reconcile >"$WORK/reconcile.out"

python3 - "$WORK" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
requests = [path for path in (root / "ledger/requests").iterdir() if path.is_dir()]
assert len(requests) == 1, requests
request = requests[0]
runner = json.loads((request / "runner.json").read_text())
completed = json.loads((request / "runner-completed.json").read_text())
receipt = json.loads((request / "receipt.json").read_text())
result = json.loads((root / "result.json").read_text())
launches = [json.loads(path.read_text()) for path in (request / "launches").glob("*.json")]
assert result["verdict"] == "passed"
assert receipt["cleanup"]["verified_absent"] is True
assert receipt["cleanup"]["manager"]["verified_absent"] is True
assert receipt["cleanup"]["manager"]["status"] == "not-created"
assert completed["source"] == "supervisor"
assert runner["runner_pid"] > 1
assert runner["runner_session_id"] == runner["runner_pid"]
assert "recruiter.py" in " ".join(runner["runner_argv"])
assert runner["runner_argv"][-2] == "run-job"
assert runner["runner_argv"][-1] == request.name
assert launches and all(item["state"] == "closed" for item in launches)
assert all(item["cleanup"]["verified_absent"] is True for item in launches)
active = root / "ledger/active/requests"
assert not active.exists() or not any(active.iterdir())
state_path = root / "fake-herdr/state.json"
state = json.loads(state_path.read_text()) if state_path.is_file() else {"panes": {}}
assert state["panes"] == {}, state
print("SMOKE OK — real client -> detached run-job -> hire -> result/receipt -> oversight cleanup -> reconcile")
PY
