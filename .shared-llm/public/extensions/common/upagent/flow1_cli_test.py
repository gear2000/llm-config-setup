"""Exercise the real just/client/controller paths using local fake Herdr processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parents[4]

HERDR = r"""
import fcntl, json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['FLOW1_FAKE'])
args = sys.argv[1:]
if args[:1] == ['--session']: args = args[2:]
if args == ['session', 'list', '--json']:
    print(json.dumps({'sessions': [{'name': 'test-flow1', 'running': True, 'socket_path': os.environ['HERDR_SOCKET_PATH']}]}))
    sys.exit(0)
with (root/'lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = json.loads((root/'panes.json').read_text())
    response = {}
    if args[:2] == ['pane','get']:
        if args[2] not in state:
            print('pane_not_found', file=sys.stderr); sys.exit(1)
        response = {'pane': state[args[2]]}
    elif args[:2] == ['pane','list']:
        response = {'panes': list(state.values())}
    elif args[:2] == ['agent','get']:
        p = next((p for p in state.values() if p['agent_name'] == args[2]), None)
        if p is None:
            print('agent_not_found', file=sys.stderr); sys.exit(1)
        response = {'agent': {'name': p['agent_name'], 'workspace_id': 'workspace', 'pane_id': p['pane_id']}}
    elif args[:2] == ['pane','process-info']:
        p = state[args[-1]]
        try:
            command = Path('/proc/'+str(p['pid'])+'/cmdline').read_bytes().replace(b'\0', b' ').decode()
        except FileNotFoundError:
            command = ''
        response = {'process_info': {'foreground_processes': [{'pid': p['pid'], 'name': p['agent'], 'cmdline': command}] if command else []}}
    elif args[:2] == ['agent','start']:
        name = args[2]
        cwd = args[args.index('--cwd')+1]
        command = args[args.index('--')+1:]
        with (root/'children.log').open('a') as log:
            p = subprocess.Popen(command, cwd=cwd, env=os.environ.copy(), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        with (root/'pids').open('a') as log: log.write(str(p.pid)+'\n')
        time.sleep(.08)
        pane = 'pane-'+name
        agent = 'claude' if name.startswith('plan-implementer') else 'codex'
        state[pane] = {'pane_id': pane, 'agent_name': name, 'workspace_id': 'workspace', 'tab_id': 'control', 'cwd': cwd, 'foreground_cwd': cwd, 'agent': agent, 'agent_status': 'working', 'pid': p.pid}
        response = {'agent': {'name': name, 'pane_id': pane, 'workspace_id': 'workspace'}}
    elif args[:2] == ['pane','close']:
        p = state.pop(args[2], None)
        if p:
            try: os.killpg(p['pid'], signal.SIGTERM)
            except ProcessLookupError: pass
        response = {'closed': True}
    elif args[:2] == ['tab','list']:
        response = {'tabs': [{'tab_id': 'control', 'label': 'control'}]}
    elif args[:2] == ['pane','run']:
        with (root/'prompts').open('a') as stream: stream.write(json.dumps(args)+'\n')
    else:
        raise RuntimeError('unsupported fake Herdr: '+repr(args))
    (root/'panes.json').write_text(json.dumps(state))
    print(json.dumps({'result': response}))
"""

MODEL = r"""
import json, os, sys, time
from pathlib import Path
if '--output' in sys.argv:
    path = Path(sys.argv[sys.argv.index('--output')+1])
    if path.name == 'decision.json':
        mode = os.environ.get('FLOW1_MANAGER', 'approved')
        if mode != 'timeout':
            path.write_text(json.dumps({'request_id': 'run', 'generation': 1, 'decision': mode, 'message': 'CLI literal advisory', 'requested_changes': []}))
while True: time.sleep(.1)
"""


@pytest.fixture
def cli(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, body in [("herdr", HERDR), ("claude", MODEL), ("codex", MODEL)]:
        path = binary / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)
    (tmp_path / "panes.json").write_text(
        json.dumps(
            {
                "hil": {
                    "pane_id": "hil",
                    "agent_name": "hil",
                    "workspace_id": "workspace",
                    "tab_id": "control",
                    "cwd": str(tmp_path),
                }
            }
        )
    )
    roster = tmp_path / "roster.yaml"
    role = {
        "command": str(binary / "codex") + " --output {output_path}",
        "expected_agent": "codex",
        "expected_process": "codex",
        "timeout_ms": 2000,
    }
    roster.write_text(
        yaml.safe_dump(
            {
                "harnesses": {"claude": "claude {model}"},
                "plan_implementers": {
                    "claude": str(binary / "claude")
                    + " --model {model} --effort {effort}"
                },
                "management": {"account_manager": role, "sentinel": role},
                "run_watch": {"enabled": False},
            }
        )
    )
    (tmp_path / "plan.md").write_text("# Approved local test\n")
    env = {
        **os.environ,
        "PATH": str(binary) + ":" + os.environ["PATH"],
        "UPAGENT_CONFIG": str(roster),
        "UPAGENT_CANONICAL_REPO": str(ROOT),
        "UPAGENT_HUB_DIR": str(tmp_path / "ledger"),
        "UPAGENT_STATE": str(tmp_path / "service.json"),
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "hil",
        "HERDR_SESSION": "test-flow1",
        "HERDR_SOCKET_PATH": str(tmp_path / "socket"),
        "FLOW1_FAKE": str(tmp_path),
    }

    def run(recipe, *args):
        return subprocess.run(
            ["just", recipe, *map(str, args)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=25,
        )

    yield tmp_path, env, run
    if (tmp_path / "pids").exists():
        for value in (tmp_path / "pids").read_text().splitlines():
            try:
                os.killpg(int(value), signal.SIGTERM)
            except ProcessLookupError:
                continue


def start(cli, *flags):
    root, env, run = cli
    # Use the validated public roster/listing, including the actual effort.
    listing = run("upagent", "lists", "--type", "plan-implementers", "--json")
    assert listing.returncode == 0, listing.stderr
    listing = json.loads(listing.stdout)
    rows = (
        listing
        if isinstance(listing, list)
        else listing.get("offerings", listing.get("plan_implementers", []))
    )
    if not rows:
        raise AssertionError(listing)
    row = next(row for row in rows if row["harness"] == "claude")
    selected = row.get("id", row.get("offering"))
    effort = row["efforts"][0]
    response = run(
        "upagent-implementer-start",
        root / "plan.md",
        selected,
        effort,
        root / "run",
        *flags,
    )
    assert response.returncode == 0, response.stderr
    receipt = json.loads((root / "run/control/implementer-start.json").read_text())
    return receipt


@pytest.mark.parametrize(
    "mode", ["no-supervise", "approved", "needs-requester", "timeout", "watch-enabled"]
)
def test_real_start_recipe_and_finish(cli, mode):
    root, env, run = cli
    env["FLOW1_MANAGER"] = "approved" if mode == "watch-enabled" else mode
    if mode == "watch-enabled":
        roster = Path(env["UPAGENT_CONFIG"])
        roster.write_text(roster.read_text().replace("enabled: false", "enabled: true"))
    receipt = start(cli, *(["--no-supervise"] if mode == "no-supervise" else []))
    assert receipt["supervise"] == (mode != "no-supervise")
    assert receipt["state"] == (
        "ready-degraded" if mode in ("needs-requester", "timeout") else "ready"
    )
    if mode == "no-supervise":
        assert not (root / "run/control/sentinel").exists()
        assert not (root / "run/control/manager").exists()
        assert not (root / "run/control/run-watch.json").exists()
    elif mode == "needs-requester":
        assert receipt["startup_advisory"] == "CLI literal advisory"
        event = run(
            "upagent-implementer-await",
            root / "run/control/implementer-start.json",
            "0",
            "1",
        )
        assert event.returncode == 0, event.stderr
        assert json.loads(event.stdout)["summary"] == "CLI literal advisory"
    finished = run(
        "upagent-implementer-finish",
        root / "run/control/implementer-start.json",
        "--force",
    )
    assert finished.returncode == 0, finished.stderr
    finish = json.loads((root / "run/control/implementer-finish.json").read_text())
    assert finish["verdict"] == "cancelled"
    if mode == "watch-enabled":
        assert (
            json.loads((root / "run/control/run-watch.json").read_text())["state"]
            == "finished"
        )
    assert all(item["status"] != "cleanup-failed" for item in finish["cleanup"]), finish
    assert list(json.loads((root / "panes.json").read_text())) == ["hil"]


def test_real_finish_recipe_displays_cleanup_failure_evidence(cli):
    root, env, run = cli
    receipt = start(cli, "--no-supervise")
    state_path = root / "panes.json"
    state = json.loads(state_path.read_text())
    state[receipt["implementer_pane"]]["workspace_id"] = "replacement"
    state_path.write_text(json.dumps(state))
    returned = run(
        "upagent-implementer-finish",
        root / "run/control/implementer-start.json",
        "--force",
    )
    assert returned.returncode == 0, returned.stderr
    assert (
        "cleanup-failed" in returned.stdout
        and "pane identity mismatch" in returned.stdout
    )
    journal = [
        json.loads(path.read_text())
        for path in sorted((root / "run/control/events").glob("*.json"))
    ]
    assert journal[0]["terminal"]
    assert journal[-1]["dedupe_key"] == "flow1:cleanup-failed:implementer"
    assert "pane identity mismatch" in journal[-1]["summary"]
    assert receipt["implementer_pane"] in json.loads(state_path.read_text())


def test_real_start_recipe_budget_failure_opens_nothing(cli):
    root, env, run = cli
    roster_path = Path(env["UPAGENT_CONFIG"])
    roster = yaml.safe_load(roster_path.read_text())
    roster["run_watch"]["drain_minutes"] = 20
    roster_path.write_text(yaml.safe_dump(roster))
    response = run(
        "upagent-implementer-start", root / "plan.md", "unused", "medium", root / "run"
    )
    assert response.returncode != 0 and "480" in response.stderr
    assert not (root / "pids").exists()
    assert not (root / "run").exists()


def test_real_inject_recipe_working_idle_batch_duplicate_and_finish(cli):
    root, env, run = cli
    receipt = start(cli, "--no-supervise")
    receipt_path = root / "run/control/implementer-start.json"

    def inject(expected):
        response = run("upagent-implementer-inject", receipt_path)
        assert response.returncode == 0, response.stderr
        data = json.loads(response.stdout)
        assert data["outcome"] == expected, data
        return data

    assert inject("nothing-pending")["envelope_seqs"] == []
    inbox = root / "run/inbox"
    inbox.mkdir()
    for seq in (3, 1, 2):
        (inbox / f"msg-{seq}.json").write_text(json.dumps({
            "seq": seq, "text": "Literal human text: $(false)\n`false` 'quotes'",
            "at_ns": seq, "acked": False,
        }))
    before = {path.name: path.read_bytes() for path in inbox.iterdir()}
    assert inject("not-idle")["envelope_seqs"] == [1, 2, 3]
    assert not (root / "prompts").exists()
    state_path = root / "panes.json"
    state = json.loads(state_path.read_text())
    state[receipt["implementer_pane"]]["agent_status"] = "idle"
    state_path.write_text(json.dumps(state))
    assert inject("delivered")["envelope_seqs"] == [1, 2, 3]
    assert inject("nothing-pending")["envelope_seqs"] == [1, 2, 3]
    assert {path.name: path.read_bytes() for path in inbox.iterdir()} == before
    prompts = (root / "prompts").read_text().splitlines()
    assert [json.loads(line) for line in prompts] == [
        ["pane", "run", receipt["implementer_pane"], "read your inbox"]
    ]
    authority = json.loads(next((root / "ledger/nudge").glob("*.json")).read_text())
    assert authority["continue"]["nudges"] == []
    assert all(item["state"] == "delivered" and item["attempts"] == 1
               for item in authority["inbox"].values())
    for path in inbox.iterdir():
        data = json.loads(path.read_text())
        data["acked"] = True
        path.write_text(json.dumps(data))
    assert inject("nothing-pending")["envelope_seqs"] == []
    finished = run("upagent-implementer-finish", receipt_path, "--force")
    assert finished.returncode == 0, finished.stderr
    (inbox / "msg-4.json").write_text(json.dumps({
        "seq": 4, "text": "Message after finish", "at_ns": 4, "acked": False,
    }))
    # Forced finish without a result is fenced even with supervision disabled.
    inject("not-idle")
    assert (root / "prompts").read_text().splitlines() == prompts


def test_real_inject_recipe_reports_invalid_envelope(cli):
    root, env, run = cli
    start(cli, "--no-supervise")
    inbox = root / "run/inbox"
    inbox.mkdir()
    (inbox / "msg-1.json").write_text('{"seq": 1, "acked": "false"}')
    response = run("upagent-implementer-inject", root / "run/control/implementer-start.json")
    assert response.returncode != 0
    assert "upagent-implementer-inject: invalid human inbox envelope" in response.stderr
    assert not (root / "prompts").exists()
