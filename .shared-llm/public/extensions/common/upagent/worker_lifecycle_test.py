"""Worker lifecycles across the Herdr 0.9.5 boundary, with real ledger publication.

Response envelopes and occupant loss follow captured headless 0.9.5 replies. No
Herdr server, home deployment, model provider, or production ledger is contacted.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "worker_lifecycle_recruiter", HERE / "recruiter.py"
)
assert spec and spec.loader
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class Wait:
    def __init__(self, fake, mode="done"):
        self.fake, self.mode = fake, mode
        self.returncode = None
        self.terminated = False

    def poll(self):
        if self.mode == "lost":
            self.returncode = 1
        elif self.mode == "gone":
            self.fake.panes.pop("w1:p2", None)
            self.returncode = 1
        elif self.mode == "done" and self.fake.staged.is_set():
            self.returncode = 0
        return self.returncode

    def communicate(self, timeout=None):
        error = {"lost": "events_lost", "gone": "agent_not_running"}.get(self.mode)
        return "", json.dumps({"error": {"code": error}}) if error else ""

    def terminate(self):
        self.returncode, self.terminated = -15, True

    kill = terminate


class Herdr095:
    def __init__(self, root):
        self.root = root
        self.panes = {"w1:p1": self.pane("w1:p1")}
        self.staged = threading.Event()
        self.prompt_count = 0
        self.waits = []
        self.sequence = []
        self.dialog = False
        self.timer = None
        self.start_commands = []
        self.shell_commands = []
        self.start_timeouts = []
        self.start_process_timeouts = []
        self.prompt_timeouts = []
        self.stall_mode = None
        self.uncertain_prompt = None
        self.delivery_observations = 0
        self.recent_text = ""
        self.uncertain_at = None
        self.delivery_delay = 0

    def pane(self, pane):
        return {
            "pane_id": pane,
            "terminal_id": "term_" + pane,
            "workspace_id": "w1",
            "tab_id": "w1:t1",
            "cwd": str(self.root),
            "foreground_cwd": str(self.root),
        }

    @staticmethod
    def reply(kind, **fields):
        return {"result": {"type": kind, **fields}}

    def stage(self, instructions):
        text = Path(instructions).read_text()
        for label in ("result.json", "compacted.md", "handoff.md"):
            found = re.search(
                r"^- " + re.escape(label) + r": (.+)$", text, re.MULTILINE
            )
            assert found, label
            path = Path(found.group(1).partition("  (REQUIRED")[0])
            path.parent.mkdir(parents=True, exist_ok=True)
            if label == "result.json":
                order_id = re.search(r'`order_id`: exactly "([^"]+)"', text).group(1)
                path.write_text(
                    json.dumps(
                        {
                            "order_id": order_id,
                            "verdict": "passed",
                            "full_log": "fake-worker-session",
                        }
                    )
                )
            else:
                path.write_text("Worker completed the verified task.\n")
        self.sequence.append("bundle-staged")
        self.staged.set()

    def begin_work(self, pane):
        pane["agent_status"] = "working"
        self.sequence.append("working")
        self.timer = threading.Timer(0.04, self.stage, (pane["instructions"],))
        self.timer.start()

    def rpc(self, *args, **kwargs):
        head = args[:2]
        if head == ("pane", "split"):
            pane = self.pane("w1:p2")
            self.panes["w1:p2"] = pane
            self.sequence.append("pane-created")
            return self.reply("pane_info", pane=pane.copy())
        if head == ("pane", "get"):
            if args[2] not in self.panes:
                raise r.RecruiterError("pane_not_found")
            return self.reply("pane_info", pane=self.panes[args[2]].copy())
        if head == ("pane", "list"):
            return self.reply("pane_list", panes=list(self.panes.values()))
        if head == ("pane", "run"):
            self.shell_commands.append(args[3])
            pane = self.panes[args[2]]
            pane.update(agent="claude", agent_status="working")
            return self.reply("ok")
        if head == ("agent", "rename"):
            self.panes[args[2]].update(name=args[3], agent_name=args[3])
            return self.reply("ok")
        if head == ("pane", "process-info"):
            pane = self.panes[args[-1]]
            kind = pane.get("agent", "bash")
            return self.reply(
                "pane_process_info",
                process_info={
                    "foreground_processes": [
                        {
                            "pid": os.getpid(),
                            "name": kind,
                            "argv": [kind],
                            "cmdline": kind,
                        }
                    ]
                },
            )
        if head == ("pane", "read"):
            return self.reply(
                "pane_output", text="Tool: sleep 100; write verified result"
            )
        if head == ("pane", "close"):
            self.panes.pop(args[2], None)
            self.sequence.append("pane-closed")
            return self.reply("ok")
        if head == ("tab", "list"):
            return self.reply(
                "tab_list",
                tabs=[{"tab_id": "w1:t1", "workspace_id": "w1", "label": "workers"}],
            )
        if head == ("agent", "start"):
            assert "--tab" not in args and "--split" not in args
            kind = args[args.index("--kind") + 1]
            argv = args[args.index("--") + 1 :]
            self.start_commands.append((kind, argv))
            self.start_timeouts.append(int(args[args.index("--timeout") + 1]))
            self.start_process_timeouts.append(kwargs["timeout_seconds"])
            if kind != "codex" and any(word.startswith("Read ") for word in argv):
                raise r.RecruiterError(
                    "timeout: interactive worker busy from its first turn"
                )
            pane = self.panes[args[args.index("--pane") + 1]]
            pane.update(
                agent=kind,
                name=args[2],
                agent_name=args[2],
                agent_status="idle",
                completion_seq=7,
                interactive_ready=True,
                instructions=argv[argv.index("--instructions") + 1],
            )
            self.sequence.append("bare-start" if kind != "codex" else "exec-start")
            if kind == "codex":
                assert argv[-1].startswith("Read ")
                self.begin_work(pane)
            return self.reply("agent_started", agent=pane.copy(), argv=[kind, *argv])
        if head in (("agent", "get"), ("agent", "wait"), ("agent", "prompt")):
            pane = next(
                (
                    p
                    for p in self.panes.values()
                    if p.get("name") == args[2]
                    or (p["pane_id"] == args[2] and p.get("agent"))
                ),
                None,
            )
            if pane is None:
                raise r.RecruiterError("agent_not_found")
            if head == ("agent", "get") and self.uncertain_prompt:
                self.delivery_observations += 1
                if self.stall_mode in ("echo", "old-done"):
                    # Enter was lost: text remains in the composer, no task ran.
                    self.recent_text = self.uncertain_prompt
                    if self.stall_mode == "old-done":
                        pane["agent_status"] = "done"
                elif (self.delivery_observations >= 2 and self.stall_mode != "never"
                      and time.monotonic() >= self.uncertain_at + self.delivery_delay):
                    self.uncertain_prompt = None
                    self.begin_work(pane)
                    if self.stall_mode == "new-done":
                        pane["agent_status"] = "done"
                        pane["completion_seq"] = 8
            if head == ("agent", "wait"):
                assert "--until" in args and "--status" not in args
                self.sequence.append("idle-gate")
            if head == ("agent", "prompt"):
                self.prompt_timeouts.append(int(args[args.index("--timeout") + 1]))
                if self.stall_mode:
                    self.prompt_count += 1
                    self.uncertain_prompt = args[3]
                    self.uncertain_at = time.monotonic()
                    raise r.RecruiterError("agent_prompt_stalled: no observed activity")
                if self.dialog == "accepted":
                    self.prompt_count += 1
                    pane["agent_status"] = "blocked"
                    # Captured 0.9.5 successful delivery that settles on permission UI.
                    return {
                        "id": "cli:agent:prompt",
                        **self.reply("agent_prompted", agent=pane.copy()),
                    }
                if self.dialog:
                    raise r.RecruiterError("agent_blocked")
                assert pane["agent_status"] == "idle"
                self.prompt_count += 1
                self.begin_work(pane)
            return self.reply("agent_info", agent=pane.copy())
        raise AssertionError(args)

    def popen(self, argv, **kwargs):
        assert argv[:3] == ["herdr", "agent", "wait"]
        assert argv[3] == "w1:p2" and argv[argv.index("--until") + 1] == "done"
        self.waits.append(argv)
        return Wait(self)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    fake = Herdr095(tmp_path)
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv(r.RUNNER_REPARENT_ENV, raising=False)
    monkeypatch.setattr(r, "STATE_FILE", tmp_path / "services.json")
    monkeypatch.setattr(
        r, "_herdr_owner_record", lambda: {"herdr_session": "llm-lab-test"}
    )
    monkeypatch.setattr(r, "_recruiter_pane_from_state", lambda: None)
    monkeypatch.setattr(r, "_require_hub_authority", lambda: None)
    monkeypatch.setattr(r, "_herdr_available", lambda: None)
    monkeypatch.setattr(
        r, "_herdr_argv", lambda args, session: (session, ["herdr", *args])
    )
    monkeypatch.setattr(r, "_herdr_json", fake.rpc)
    monkeypatch.setattr(r, "_herdr", fake.rpc)
    monkeypatch.setattr(r.subprocess, "Popen", fake.popen)
    monkeypatch.setattr(
        r, "inspect_worker_configuration", lambda *a, **kw: {"errors": []}
    )
    monkeypatch.setattr(r, "_notify_requester", lambda *a, **kw: None)
    monkeypatch.setattr(r, "COMPLETION_MONITOR_POLL_SECONDS", 0.005)
    yield fake
    if fake.timer:
        fake.timer.join(timeout=1)
        assert not fake.timer.is_alive()


def request(root, kind):
    (root / "instructions.md").write_text(
        "First sleep 100, then produce the required bundle.\n"
    )
    order = {
        "order_id": "lifecycle-" + kind,
        "phase_id": "phase-0",
        "stage_id": "stage-1-implementation",
        "harness": kind,
        "model": "local-fake",
        "agent": "backend",
        "cwd": str(root),
        "instructions_path": str(root / "instructions.md"),
        "result_path": str(root / "public/result.json"),
        "cockpit_pane": "w1:p1",
        "timeout_ms": 2000,
        "sentinel": False,
    }
    roster = root / "roster.yaml"
    roster.write_text(
        "management:\n  mode: direct\n  rescue_on_startup_failure: false\n  startup_timeout_ms: 1000\n"
        "  requester_grace_ms: 1\nharnesses:\n  "
        + kind
        + ": >-\n    "
        + kind
        + ' --instructions {instructions_path} "Read {instructions_path} and perform the assigned work."\n'
    )
    return order, roster


@pytest.mark.parametrize("kind", ["claude", "pi", "codex"])
def test_worker_accepted_through_validated_receipt_and_pane_cleanup(runtime, kind):
    order, roster = request(runtime.root, kind)
    ledger = r.JobLedger()
    key, created = ledger.submit(order)
    assert created and ledger.state(key)["state"] == "requested"
    assert r.cmd_run_job(key, str(roster)) == 0
    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "passed" and receipt["cleanup"]["verified_absent"]
    assert (
        json.loads(Path(order["result_path"]).read_text())["full_log"]
        == "fake-worker-session"
    )
    assert all(item["present"] for item in receipt["artifacts"])
    assert list(runtime.panes) == ["w1:p1"]
    assert runtime.sequence.index("bundle-staged") < runtime.sequence.index(
        "pane-closed"
    )
    if kind == "codex":
        assert runtime.waits and runtime.prompt_count == 0
    else:
        assert runtime.prompt_count == 1 and not runtime.waits
        assert runtime.sequence[:4] == [
            "pane-created",
            "bare-start",
            "idle-gate",
            "working",
        ]
    assert not list(ledger.active_claims())


def test_busy_first_turn_is_not_passed_to_interactive_start(runtime, monkeypatch):
    # Mutation proof: restoring the old inline-prompt launch must fail the lifecycle.
    split = r._agent_launch_argv
    monkeypatch.setattr(
        r,
        "_agent_launch_argv",
        lambda command: (lambda a, k, p: (a + [p], k, None))(*split(command)),
    )
    order, roster = request(runtime.root, "claude")
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    assert r.cmd_run_job(key, str(roster)) != 0
    assert ledger.completed_receipt(key, order)["verdict"] != "passed"
    assert not runtime.staged.is_set() and runtime.prompt_count == 0
    assert list(runtime.panes) == ["w1:p1"]


def test_blocked_dialog_never_receives_a_prompt_or_leaks_its_pane(runtime):
    runtime.dialog = True
    order, roster = request(runtime.root, "pi")
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    assert r.cmd_run_job(key, str(roster)) != 0
    assert ledger.completed_receipt(key, order)["verdict"] == "blocked"
    assert runtime.prompt_count == 0 and not runtime.staged.is_set()
    assert list(runtime.panes) == ["w1:p1"]


def test_pane_vanishes_mid_server_wait_and_returns_to_salvage(runtime, monkeypatch):
    runtime.panes["w1:p2"] = runtime.pane("w1:p2")
    monkeypatch.setattr(r.subprocess, "Popen", lambda *a, **kw: Wait(runtime, "gone"))
    started = time.monotonic()
    assert (
        r._wait_for_agent_status(
            "w1:p2",
            60_000,
            None,
            expected_process="codex",
            herdr_session="llm-lab-test",
        )
        is True
    )
    assert time.monotonic() - started < 2
    assert "w1:p2" not in runtime.panes


def test_lost_events_reconciles_live_state_then_reopens_wait(runtime, monkeypatch):
    runtime.panes["w1:p2"] = runtime.pane("w1:p2")
    timeouts = []

    def wait(argv, **kwargs):
        timeouts.append(int(argv[argv.index("--timeout") + 1]))
        if len(timeouts) == 1:
            time.sleep(0.03)
            return Wait(runtime, "lost")
        runtime.staged.set()
        return Wait(runtime)

    monkeypatch.setattr(r.subprocess, "Popen", wait)
    assert (
        r._wait_for_agent_status("w1:p2", 1000, None, herdr_session="llm-lab-test")
        is True
    )
    assert len(timeouts) == 2 and timeouts[1] < timeouts[0]
    assert "w1:p2" in runtime.panes


def supervised(runtime, kind, *, valid):
    order, _ = request(runtime.root, kind)
    order["sentinel"] = True
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1000)
    manifest = r.completion.build_manifest(
        order, ledger.request_dir(key), token, r.lifecycle.request_identity(order)
    )
    r.completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    finalized = threading.Event()
    if valid:
        for artifact in manifest.artifacts:
            artifact.staging_path.parent.mkdir(parents=True, exist_ok=True)
            artifact.staging_path.write_text(
                json.dumps(
                    {
                        "order_id": order["order_id"],
                        "verdict": "passed",
                        "full_log": "worker-session",
                    }
                )
                if artifact.kind == "result"
                else "Verified work.\n"
            )
        r.completion.validate_bundle(
            manifest,
            load_result=r.load_result,
            load_answer=r.contracts_consult.load_answer,
        )
        finalized.set()
    runtime.panes["w1:p2"] = {
        **runtime.pane("w1:p2"),
        "agent": kind,
        "agent_status": "working",
    }
    runtime.panes["w1:p3"] = {
        **runtime.pane("w1:p3"),
        "agent": "claude",
        "agent_status": "working",
    }
    path = r._sentinel_closeout_path(ledger, key, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    watch = r._SentinelWatch(
        ledger,
        key,
        order,
        manifest,
        path,
        {"address": "sentinel", "pane": "w1:p3"},
        herdr_session="llm-lab-test",
    )
    return ledger, key, watch, finalized


@pytest.mark.parametrize("kind", ["claude", "pi", "codex"])
def test_hard_deadline_preserves_valid_bundle_and_sentinel_landing_window(
    runtime, monkeypatch, kind
):
    ledger, key, watch, finalized = supervised(runtime, kind, valid=True)
    monkeypatch.setattr(r, "SENTINEL_LANDING_WINDOW_SECONDS", 60)
    monkeypatch.setattr(r.subprocess, "Popen", lambda *a, **kw: Wait(runtime, "hold"))
    started = time.monotonic()
    assert (
        r._wait_for_agent_status(
            "w1:p2",
            70,
            finalized,
            completion_style="exec" if kind == "codex" else "interactive",
            expected_process=kind,
            sentinel_watch=watch,
            herdr_session="llm-lab-test",
        )
        is False
    )
    assert 0.05 <= time.monotonic() - started < 1
    assert watch.wake_path.read_text().strip() == "valid-bundle"
    events = [
        json.loads(p.read_text())
        for p in (ledger.request_dir(key) / "events").glob("*.json")
    ]
    assert any(
        e["event"] == "sentinel-window-lapsed" and e["window"] == "landing"
        for e in events
    )


def test_pane_loss_gives_sentinel_its_bounded_closeout_before_salvage(
    runtime, monkeypatch
):
    ledger, key, watch, finalized = supervised(runtime, "codex", valid=False)
    monkeypatch.setattr(r, "SENTINEL_CLOSEOUT_GRACE_SECONDS", 0.04)
    monkeypatch.setattr(r.subprocess, "Popen", lambda *a, **kw: Wait(runtime, "gone"))
    started = time.monotonic()
    assert (
        r._wait_for_agent_status(
            "w1:p2",
            3000,
            finalized,
            expected_process="codex",
            sentinel_watch=watch,
            herdr_session="llm-lab-test",
        )
        is True
    )
    assert time.monotonic() - started >= 0.04
    assert watch.wake_path.read_text().strip() == "worker-gone"
    events = [
        json.loads(p.read_text())
        for p in (ledger.request_dir(key) / "events").glob("*.json")
    ]
    assert any(
        e["event"] == "sentinel-window-lapsed" and e["window"] == "closeout"
        for e in events
    )


@pytest.mark.skipif(
    sys.platform != "linux", reason="process-tree proof uses Linux /proc"
)
def test_reparented_runner_survives_callers_descendant_tree_kill(tmp_path):
    # A real subprocess boundary: setsid alone leaves the runner in the caller's tree.
    # The live lab repeats this with an actual agent shell-tool caller and 120 seconds.
    script = tmp_path / "caller.py"
    marker, release, result = [
        tmp_path / name for name in ("runner.json", "release", "result")
    ]
    script.write_text("""import importlib.util, json, os, subprocess, sys, time
from pathlib import Path
source, root = Path(sys.argv[1]), Path(sys.argv[2])
marker = root / "runner.json"
if len(sys.argv) == 4:
    spec = importlib.util.spec_from_file_location("reparent_worker", source)
    r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
    r.JobLedger = lambda: None
    r._active_claim_for = lambda *a: json.loads(marker.read_text()) if marker.exists() else None
    r._reparent_runner("test")
    marker.write_text(json.dumps({"runner_pid": os.getpid()}))
    while not (root / "release").exists(): time.sleep(.01)
    (root / "result").write_text("survived")
else:
    subprocess.Popen([sys.executable, __file__, str(source), str(root), "runner"], start_new_session=True)
    while True: time.sleep(.1)
""")
    caller = subprocess.Popen(
        [sys.executable, str(script), str(HERE / "recruiter.py"), str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runner_pid = None

    def parent(pid):
        try:
            return int(
                Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[1]
            )
        except FileNotFoundError:
            return None

    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists():
                runner_pid = json.loads(marker.read_text())["runner_pid"]
                if parent(runner_pid) == 1:
                    break
            time.sleep(0.01)
        assert runner_pid and parent(runner_pid) == 1
        parents = {
            int(p.name): parent(int(p.name))
            for p in Path("/proc").iterdir()
            if p.name.isdigit()
        }
        descendants = {caller.pid}
        while True:
            found = {pid for pid, ppid in parents.items() if ppid in descendants}
            if found <= descendants:
                break
            descendants |= found
        assert runner_pid not in descendants
        for pid in sorted(descendants, reverse=True):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        caller.wait(timeout=2)
        release.touch()
        deadline = time.monotonic() + 2
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert result.read_text() == "survived"
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait()
        if runner_pid and r._process_start_time(runner_pid):
            os.kill(runner_pid, 9)


def test_cursor_first_prompt_waits_for_its_official_session_registration(
    runtime, monkeypatch
):
    observations = []

    def get(*args, **kwargs):
        observations.append(args)
        return {
            "result": {
                "type": "agent_info",
                "agent": {
                    "name": "cursor-worker",
                    "pane_id": "w1:p2",
                    "agent_session": {
                        "source": "herdr:cursor",
                        "value": "registered-session",
                    }
                    if len(observations) == 2
                    else None,
                },
            }
        }

    monkeypatch.setattr(r, "_herdr_json", get)
    r._wait_for_cursor_session(
        "cursor-worker", "w1:p2", time.monotonic() + 1, "llm-lab-test"
    )
    assert len(observations) == 2
    with pytest.raises(r.RecruiterError, match="identity changed"):
        r._wait_for_cursor_session(
            "cursor-worker", "w1:p9", time.monotonic() + 1, "llm-lab-test"
        )


def test_cleared_name_never_authorizes_a_different_terminal(runtime, monkeypatch):
    # A launch reply can be lost while the process exits and clears its name.
    order, _ = request(runtime.root, "claude")
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1000)
    launch = ledger.begin_launch(
        key, token, "worker", "worker-name", "llm-lab-test", str(runtime.root)
    )
    ledger.record_launch_created(
        key,
        token,
        launch,
        "w1:p2",
        "w1",
        "worker-name",
        terminal_id="original-terminal",
    )
    runtime.panes["w1:p2"] = runtime.pane("w1:p2")
    cleanup = r._reconcile_exact_launch(
        ledger,
        key,
        launch,
        known_pane="w1:p2",
        herdr_session="llm-lab-test",
        allow_not_found_absent=True,
        attempts=1,
    )
    assert not cleanup["verified_absent"] and "w1:p2" in runtime.panes


def test_intake_lost_split_reply_cannot_claim_an_anonymous_pane_absent(runtime):
    intake_key, token = "intake-key", "lease-token"
    ownership = {
        "intake_key": intake_key,
        "lease_token": token,
        "agent_name": r._intake_clerk_agent_name(intake_key, token),
        "herdr_session": "llm-lab-test",
        "herdr_launch_style": "split-then-start",
        "pane": None,
    }
    identity = r._resolve_intake_clerk_identity(ownership)
    assert identity["status"] == "blocked" and "uncertain" in identity["reason"]


def test_real_intake_clerk_wrapper_uses_shell_launch_and_health(runtime):
    config = r.llm_management.load_management_config({})
    brief = runtime.root / "clerk brief.md"
    brief.write_text("Classify this request.\n")
    wrapper = r.llm_management.render_intake_clerk_command(
        config.intake_clerk, brief, str(runtime.root), runtime.root / "response.json"
    )
    pane, workspace, address = r._start_herdr_agent(
        "intake-clerk-test",
        {"cwd": str(runtime.root), "cockpit_pane": "w1:p1"},
        wrapper,
        expected_agent=config.intake_clerk.expected_agent,
        expected_process=config.intake_clerk.expected_process,
        herdr_session="llm-lab-test",
    )
    assert (pane, workspace, address) == ("w1:p2", "w1", "intake-clerk-test")
    assert runtime.shell_commands == [wrapper]
    assert not runtime.start_commands and runtime.prompt_count == 0
    assert runtime.panes[pane]["agent_status"] == "working"


@pytest.mark.parametrize("delivery", ["working", "new-done"])
def test_stalled_prompt_reconciles_late_delivery_without_resending(
    runtime, monkeypatch, delivery
):
    runtime.stall_mode = delivery
    monkeypatch.setattr(r, "PROMPT_RECONCILE_POLL_SECONDS", 0.005)
    monkeypatch.setattr(r, "_pane_recent_output", lambda *a, **kw: runtime.recent_text)
    order, roster = request(runtime.root, "pi")
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    assert r.cmd_run_job(key, str(roster)) == 0
    assert ledger.completed_receipt(key, order)["verdict"] == "passed"
    assert runtime.prompt_count == 1
    assert runtime.sequence.index("bundle-staged") < runtime.sequence.index(
        "pane-closed"
    )


@pytest.mark.parametrize("delivery", ["never", "echo", "old-done"])
def test_stalled_prompt_never_delivered_fails_and_closes_owned_pane(
    runtime, monkeypatch, delivery
):
    runtime.stall_mode = delivery
    clock = [100.0]
    monkeypatch.setattr(r.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(r.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(r, "PROMPT_RECONCILE_POLL_SECONDS", 0.005)
    monkeypatch.setattr(
        r, "_pane_recent_output", lambda *a, **kw: runtime.recent_text
    )
    order, roster = request(runtime.root, "pi")
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    assert r.cmd_run_job(key, str(roster)) != 0
    assert ledger.completed_receipt(key, order)["verdict"] == "blocked"
    assert runtime.prompt_count == 1 and not runtime.staged.is_set()
    if delivery in ("echo", "old-done"):
        assert runtime.recent_text == runtime.uncertain_prompt
    assert list(runtime.panes) == ["w1:p1"]


def test_sentinel_brief_echo_without_new_turn_fails_hire_and_closes_pane(runtime, monkeypatch):
    runtime.stall_mode = "echo"
    # Layout is independent of this launch failure. Let a wrongly accepted prompt
    # finish the hire so the test fails on delivery, not on the fake's layout API.
    monkeypatch.setattr(r, "_place_started_agent_in_role_tab", lambda pane, *a, **kw: pane)
    monkeypatch.setattr(r, "_resize_started_pane", lambda *a, **kw: None)
    clock = [100.0]
    monkeypatch.setattr(r.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(r.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(r, "_pane_recent_output", lambda *a, **kw: runtime.recent_text)
    order, _ = request(runtime.root, "pi")
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 600_000)
    config = r.llm_management.load_management_config({"management": {"startup_timeout_ms": 1000}})
    role = r.llm_management.ManagementRole(
        command='pi --instructions {brief_path} "Read {brief_path} and supervise the worker."',
        expected_agent="pi", expected_process="pi", timeout_ms=1000,
    )
    closeout = r._sentinel_closeout_path(ledger, key, 1)
    closeout.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(r.FencedLaunchError, match="delivery unconfirmed") as failure:
        r._start_sentinel(
            ledger, key, token, order, config, role, "w1:p1", closeout, 1,
            herdr_session="llm-lab-test", liftoff_deadline_ms=60_000,
        )
    assert failure.value.cleanup["verified_absent"]
    assert runtime.prompt_count == 1 and runtime.recent_text == runtime.uncertain_prompt
    assert not runtime.staged.is_set() and not closeout.exists()
    assert list(runtime.panes) == ["w1:p1"]
    assert "sentinel-hired" not in [event["event"] for event in ledger.events(key)]


@pytest.mark.parametrize(
    "configured,budget", [(1000, 180000), (240000, 240000), (600000, 299000)]
)
def test_roster_start_budget_has_floor_cap_and_independent_prompt_budget(
    runtime, configured, budget
):
    order, roster = request(runtime.root, "pi")
    roster.write_text(
        roster.read_text().replace(
            "startup_timeout_ms: 1000", f"startup_timeout_ms: {configured}"
        )
    )
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    assert r.cmd_run_job(key, str(roster)) == 0
    assert budget - 2000 < runtime.start_timeouts[0] <= budget
    assert runtime.start_process_timeouts[0] == runtime.start_timeouts[0] / 1000 + 5
    assert runtime.prompt_timeouts == [budget]


def test_accepted_prompt_that_reaches_dialog_is_rejected_and_cleaned_up(runtime):
    runtime.dialog = "accepted"
    order, _ = request(runtime.root, "claude")
    launch = f"claude --instructions {order['instructions_path']} 'Read the assigned instructions and work.'"
    with pytest.raises(r.RecruiterError, match="agent_blocked.*after prompt delivery"):
        r._start_herdr_agent(
            "dialog-worker", order, launch, herdr_session="llm-lab-test"
        )
    assert runtime.prompt_count == 1
    assert not runtime.staged.is_set() and list(runtime.panes) == ["w1:p1"]


def test_codex_task_variables_are_literal_while_options_expand(monkeypatch):
    import shlex

    monkeypatch.setenv("TEST_LAUNCH_PATH", "/lab/option")
    prompt = "Explain $TEST_LAUNCH_PATH and ${TEST_LAUNCH_PATH} literally."
    argv, kind, delayed = r._agent_launch_argv(
        shlex.join(["codex", "exec", "--config", "$TEST_LAUNCH_PATH", prompt])
    )
    assert kind == "codex" and delayed is None
    assert argv[-1] == prompt and argv[-2] == "/lab/option"


def test_lost_wait_probe_cannot_overrun_original_deadline(runtime, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(r.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        r.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    waits, probes = [], []

    def lost(*args, **kwargs):
        waits.append(args)
        raise r.HerdrWaitLost("events_lost")

    def wedged(*args, **kwargs):
        assert args[:2] == ("pane", "get")
        probes.append(kwargs["timeout_seconds"])
        clock[0] += kwargs["timeout_seconds"]
        raise r.RecruiterError("socket probe timed out")

    monkeypatch.setattr(r, "_wait_for_exec_status", lost)
    monkeypatch.setattr(r, "_herdr_json", wedged)
    with pytest.raises(r.AgentWaitTimeout):
        r._wait_for_agent_status("w1:p2", 250, None, herdr_session="llm-lab-test")
    assert len(waits) == 1 and probes == [0.25] and clock[0] == 100.25


def test_repeated_lost_waits_back_off_before_reopening(runtime, monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(r.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(r.time, "sleep", sleep)
    runtime.panes["w1:p2"] = runtime.pane("w1:p2")
    waits = []

    def lost_then_done(*args, **kwargs):
        waits.append(args[1])
        if len(waits) < 4:
            raise r.HerdrWaitLost("events_lost")
        return True

    monkeypatch.setattr(r, "_wait_for_exec_status", lost_then_done)
    assert r._wait_for_agent_status("w1:p2", 1000, None, herdr_session="llm-lab-test")
    assert len(sleeps) == 3 and all(delay > 0 for delay in sleeps)
    assert all(a > b for a, b in zip(waits, waits[1:]))


@pytest.mark.parametrize("prompt_budget,delay,confirmed", [(1000, 45, True), (1000, 61, False), (180000, 90, True)])
def test_stalled_prompt_observation_uses_full_prompt_budget(runtime, monkeypatch, prompt_budget, delay, confirmed):
    clock = [100.0]
    monkeypatch.setattr(r.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(r.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    order, _ = request(runtime.root, "pi")
    runtime.panes["w1:p2"] = {**runtime.pane("w1:p2"), "name": "slow-worker",
        "agent": "pi", "agent_status": "idle", "completion_seq": 7,
        "instructions": order["instructions_path"]}
    # This direct prompt test proves state delivery; no result timer is needed.
    monkeypatch.setattr(runtime, "begin_work", lambda pane: pane.update(agent_status="working"))
    runtime.stall_mode = "working"
    runtime.delivery_delay = delay
    if confirmed:
        r._submit_agent_prompt("slow-worker", "Do the task.", prompt_budget, herdr_session="llm-lab-test")
        assert clock[0] >= 100 + delay
    else:
        with pytest.raises(r.PromptDeliveryUnconfirmed):
            r._submit_agent_prompt("slow-worker", "Do the task.", prompt_budget, herdr_session="llm-lab-test")
        assert clock[0] == 160
    assert runtime.prompt_count == 1
    assert runtime.prompt_timeouts == [max(60000, prompt_budget)]
