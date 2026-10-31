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
from pathlib import Path

root = Path(os.environ["UPAGENT_SMOKE_STATE"])
state_path = root / "state.json"
args = sys.argv[1:]
if args[:1] == ["--session"]:
    assert args[1] == os.environ["HERDR_SESSION"]
    args = args[2:]

def emit(kind, **fields):
    print(json.dumps({"id": "cli:" + ":".join(args[:2]), "result": {"type": kind, **fields}}), flush=True)

def fail(code):
    print(json.dumps({"error": {"code": code, "message": code}}), file=sys.stderr)
    raise SystemExit(1)

if args == ["session", "list", "--json"]:
    print(json.dumps({"sessions": [{"name": os.environ["HERDR_SESSION"], "running": True,
                                   "socket_path": os.environ["HERDR_SOCKET_PATH"]}]}))
    raise SystemExit(0)
with (root / "state.lock").open("a+") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    state = json.loads(state_path.read_text()) if state_path.exists() else {"panes": {}, "counter": 1}
    panes = state["panes"]
    if args[:2] == ["pane", "get"]:
        pane = panes.get(args[2])
        if args[2] == "w1:p1":
            pane = {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "cwd": os.getcwd()}
        if pane is None: fail("pane_not_found")
        emit("pane_info", pane=pane)
    elif args[:2] == ["pane", "split"]:
        assert args[2] == "w1:p1"
        state["counter"] += 1
        pane_id = "w1:p" + str(state["counter"])
        cwd = args[args.index("--cwd") + 1]
        pane = {"pane_id": pane_id, "terminal_id": "term_smoke_" + str(state["counter"]),
                "workspace_id": "w1", "tab_id": "w1:t1", "cwd": cwd, "foreground_cwd": cwd,
                "agent_status": "unknown", "revision": 0}
        panes[pane_id] = pane
        emit("pane_info", pane=pane)
    elif args[:2] == ["agent", "start"]:
        assert "--cwd" not in args and "--split" not in args
        assert args[args.index("--kind") + 1] == "claude"
        command = args[args.index("--") + 1:]
        # A task on this argv would keep an interactive harness busy from its first turn.
        assert len(command) == 2 and command[0] == "--instructions", command
        pane = panes[args[args.index("--pane") + 1]]
        pane.update(agent="claude", name=args[2], agent_status="idle", interactive_ready=True,
                    instructions=command[1], pid=None)
        emit("agent_started", agent=pane, argv=["claude", *command])
    elif args[:2] in (["agent", "get"], ["agent", "wait"], ["agent", "prompt"]):
        pane = next((p for p in panes.values() if p.get("name") == args[2] or p["pane_id"] == args[2]), None)
        if pane is None or not pane.get("agent"): fail("agent_not_found")
        if args[1] == "prompt":
            if pane["agent_status"] == "blocked": fail("agent_blocked")
            assert pane["agent_status"] == "idle", "prompt must follow bare idle startup"
            assert args[3].startswith("Read ")
            process = subprocess.Popen([sys.executable, str(root.parent / "fake_worker.py"), pane["instructions"]],
                                       cwd=pane["cwd"], env=os.environ.copy(), start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pane.update(pid=process.pid, agent_status="working")
            emit("agent_prompted", agent=pane)
        else:
            assert "--status" not in args
            emit("agent_info", agent=pane)
    elif args[:2] == ["pane", "process-info"]:
        pane = panes[args[args.index("--pane") + 1]]
        emit("pane_process_info", process_info={"pane_id": pane["pane_id"], "foreground_processes": [
            {"pid": pane["pid"], "name": "claude", "argv": ["claude", "local-smoke"], "cmdline": "claude local-smoke"}
        ]})
    elif args[:2] == ["pane", "list"]:
        emit("pane_list", panes=list(panes.values()))
    elif args[:2] == ["pane", "close"]:
        pane = panes.pop(args[2], None)
        if pane and pane.get("pid"):
            try: os.killpg(pane["pid"], signal.SIGTERM)
            except ProcessLookupError: pass
        emit("ok")
    elif args[:2] == ["pane", "report-agent"]:
        emit("ok")
    elif args[:2] == ["tab", "list"]:
        emit("tab_list", tabs=[{"tab_id": "w1:t1", "workspace_id": "w1", "label": "workers"}])
    else:
        raise RuntimeError("unsupported fake Herdr command: " + repr(args))
    state_path.write_text(json.dumps(state))

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
    # The result label includes an explanatory REQUIRED annotation after two spaces.
    return Path(match.group(1).partition("  (REQUIRED")[0])
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
  claude: >-
    claude --instructions {instructions_path}
    "Read {instructions_path} and perform the assigned work."
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
  "cockpit_pane": "w1:p1",
  "timeout_ms": 10000,
  "sentinel": false
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
