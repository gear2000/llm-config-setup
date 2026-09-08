"""Deterministic Phase 3 proofs. No real worker or model is hired."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "run_watch_tests_runtime", HERE / "run_watch.py"
)
assert spec and spec.loader
watcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watcher
spec.loader.exec_module(watcher)
auth = watcher.authority


def response(request="worker-a", generation=1):
    return {
        "request_id": request,
        "payload_sha256": "a" * 64,
        "state": {
            "state": "requested",
            "order_id": "public-" + request,
            "generation": generation,
        },
        "receipt": None,
        "result": None,
        "submission": {"state": "submitted"},
    }


def receipt(root, **over):
    value = {
        "state": "ready",
        "run_id": root.name,
        "phase_id": "plan",
        "harness": "claude",
        "model": "claude-sonnet-5",
        "agent_name": "implementer",
        "workspace_id": "workspace",
        "implementer_pane": "implementer-pane",
        "herdr_session": "test-session",
        **over,
    }
    path = root / "control" / "implementer-start.json"
    watcher.write_json(path, value)
    return watcher.awaiter.ImplementerContext(path)


class FakeRuntime:
    def __init__(self, root):
        self.ctx = receipt(root)
        self.nudges = auth.NudgeAuthority(root / "ledger")
        self.targets = {}
        self.probes = {}
        self.sent = []
        self.events = []
        self.checks = []
        self.closed = []
        self.check_done = False
        self.check_message = None
        self.add("implementer", status="working", worker=False)

    def add(
        self,
        name,
        status="idle",
        style="claude",
        terminal=False,
        presence=None,
        worker=True,
    ):
        record = (
            watcher.register_worker(self.ctx.run_root, response(name))
            if worker
            else None
        )
        identity = auth.TargetIdentity(
            "test-session",
            "workspace",
            name + "-pane",
            name,
            "request" if worker else "flow1-run",
            name if worker else self.ctx.run_id,
        )
        target = watcher.Target(
            identity,
            style,
            1,
            {
                "harness": style,
                "model": "claude-sonnet-5",
                "cwd": str(self.ctx.run_root),
            },
            record,
            terminal,
        )
        self.targets[name] = target
        self.probes[name] = auth.Probe(
            identity, status, terminal, presence or auth.PaneState.PRESENT, style
        )
        return target

    def resolve(self, record):
        return self.targets[record["request_id"]]

    def implementer(self):
        return self.targets["implementer"]

    def probe(self, target):
        return self.probes[target.identity.agent_name]

    def deliver(self, target, text):
        self.sent.append((target.identity.agent_name, text))

    def publish(self, key, message):
        self.events.append((key, message))

    def checker_start(self, target, directory, now):
        launch = {
            "state": "running",
            "provider": "other-provider",
            "request_id": target.identity.owner_id,
            "generation": 1,
            "deadline": now + 285,
            "output": str(directory / "assessment.json"),
        }
        self.checks.append(launch)
        return launch

    def checker_poll(self, launch, now):
        return self.check_done or now >= launch["deadline"], self.check_message

    def checker_close(self, launch):
        self.closed.append(launch.copy())


def test_registry_layer_and_real_response(tmp_path):
    layer = (
        HERE.parents[2] / "layers/slash-commands/common/common/plan-implementer/hire.md"
    )
    text = layer.read_text()
    assert 'just upagent-register-worker "$run_root" "$response"' in text
    assert text.index("just upagent-register-worker") < text.index("just upagent await")
    first = watcher.register_worker(tmp_path, response())
    attached = watcher.register_worker(tmp_path, response())
    assert attached == first
    assert set(first) == {
        "request_id",
        "payload_sha256",
        "order_id",
        "generation",
        "placed_at_ns",
    }
    assert (tmp_path / "control/workers/worker-a.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(watcher.RunWatchError, match="generation mismatch"):
        watcher.register_worker(tmp_path, response(generation=2))
    token = response()
    token["state"]["requester_control_token"] = "fixture-secret"
    with pytest.raises(watcher.RunWatchError, match="redact"):
        watcher.register_worker(tmp_path, token)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "../escape"),
        ("payload_sha256", "wrong"),
        ("generation", True),
        ("order_id", None),
    ],
)
def test_registry_rejects_invalid_response(tmp_path, field, value):
    data = response()
    (data["state"] if field in ("generation", "order_id") else data)[field] = value
    with pytest.raises(watcher.RunWatchError):
        watcher.register_worker(tmp_path, data)
    assert not (tmp_path / "control/workers").exists()


def test_two_runs_same_cwd_sweep_only_their_registry(tmp_path):
    first, second = FakeRuntime(tmp_path / "one"), FakeRuntime(tmp_path / "two")
    first.add("a")
    second.add("b")
    watcher.Watch(first.ctx.run_root, {}, first).sweep()
    watcher.Watch(second.ctx.run_root, {}, second).sweep()
    assert first.sent == [("a", "continue")]
    assert second.sent == [("b", "continue")]


def test_sweep_matrix(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.add("working", "working")
    runtime.add("idle")
    runtime.add("done", "done")
    runtime.add("finished", terminal=True, presence=auth.PaneState.GONE)
    runtime.add("gone", presence=auth.PaneState.GONE)
    runtime.add("blocked", "blocked")
    runtime.add("exec", style="codex")
    runtime.add("unknown", presence=auth.PaneState.UNKNOWN)
    now = [1000.0]
    watch = watcher.Watch(tmp_path, {}, runtime, clock=lambda: now[0])
    watch.sweep()
    watch.sweep()
    assert runtime.sent == [("done", "continue"), ("idle", "continue")]
    keys = [k for k, _ in runtime.events]
    assert keys.count("run-watch:gone:gone-pane") == 1
    assert keys.count("run-watch:blocked:blocked-pane") == 1
    assert "run-watch:gone:finished-pane" not in keys
    assert "finished" not in watch.data["remaining_request_ids"]
    now[0] += 3600
    watch.sweep()
    assert len([k for k, _ in runtime.events if k == "run-watch:gone:gone-pane"]) == 2
    assert all(text == "continue" for _, text in runtime.sent)


def test_episode_cap_checker_one_shot_and_provider_dedupe(tmp_path, monkeypatch):
    runtime = FakeRuntime(tmp_path)
    runtime.add("idle")
    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    watch = watcher.Watch(tmp_path, {}, runtime, clock=lambda: now[0])
    for step in (0, 300, 1200, 1201, 1202):
        now[0] = 1000 + step
        watch.sweep()
    assert len(runtime.sent) == 3
    assert len(runtime.checks) == 1
    runtime.check_done = True
    watch.poll_checks()
    watch.sweep()
    assert len(runtime.closed) == 1 and len(runtime.checks) == 1
    assert any(message == "checker produced nothing" for _, message in runtime.events)
    # A new working->idle episode can escalate, without resetting provider history.
    target = runtime.targets["idle"]
    runtime.probes["idle"] = auth.Probe(
        target.identity, "working", False, auth.PaneState.PRESENT, "claude"
    )
    watch.sweep()
    runtime.probes["idle"] = auth.Probe(
        target.identity, "idle", False, auth.PaneState.PRESENT, "claude"
    )
    for step in (0, 300, 1200, 1201):
        now[0] = 3000 + step
        watch.sweep()
    watch.poll_checks()
    assert len(runtime.closed) == 2
    assert (
        len([k for k, _ in runtime.events if k == "run-watch:provider:other-provider"])
        == 1
    )
    watch.advisory("run-watch:provider:other-provider", "again")
    assert (
        len([k for k, _ in runtime.events if k == "run-watch:provider:other-provider"])
        == 1
    )


@pytest.mark.parametrize("finish", ["finish-record", "sigterm"])
def test_drain_to_empty_and_timeout(tmp_path, finish):
    runtime = FakeRuntime(tmp_path)
    runtime.add("worker", "working")
    now = [1000.0]
    watch = watcher.Watch(tmp_path, {"drain_minutes": 1}, runtime, clock=lambda: now[0])
    if finish == "finish-record":
        watcher.write_json(
            tmp_path / "control/implementer-finish.json", {"verdict": "passed"}
        )
    else:
        watch.stop = True
    assert watch.tick()
    assert watch.data["state"] == "draining"
    now[0] += 60
    assert not watch.tick()
    assert watch.data["state"] == "drain-timeout"
    assert watch.data["remaining_request_ids"] == ["worker"]
    runtime.targets["worker"].terminal = True
    assert not watch.tick()
    assert watch.data["state"] == "finished"


@pytest.mark.parametrize("mode", ["missing", "failed"])
def test_orphaned(tmp_path, mode):
    runtime = FakeRuntime(tmp_path)
    watch = watcher.Watch(tmp_path, {}, runtime)
    path = tmp_path / "control/implementer-start.json"
    if mode == "missing":
        path.unlink()
    else:
        watcher.write_json(path, {"state": "failed"})
    assert not watch.tick()
    assert watch.data["state"] == "orphaned"


def cli_env(tmp_path):
    return {
        **os.environ,
        "UPAGENT_CANONICAL_REPO": str(HERE.parents[4]),
        "UPAGENT_HUB_DIR": str(tmp_path / "ledger"),
        "UPAGENT_STATE": str(tmp_path / "service.json"),
    }


@pytest.mark.parametrize("kind", ["public", "legacy"])
@pytest.mark.parametrize(
    "policy,valid,enabled",
    [
        ({}, True, True),
        ({"enabled": True}, True, True),
        ({"enabled": False}, True, False),
        ({"enabled": "false"}, False, None),
        ({"interval_minutes": 0}, False, None),
        ({"drain_minutes": True}, False, None),
        ({"unknown": True}, False, None),
        (None, False, None),
    ],
)
def test_real_cli_switch_validation(tmp_path, kind, policy, valid, enabled):
    roster = tmp_path / "roster.yaml"
    raw = watcher.recruiter.yaml.safe_load(
        watcher.offerings.render_roster(["standard"])
        if kind == "public"
        else (HERE / "upagent.yaml").read_text()
    )
    raw.pop("run_watch", None)
    if policy != {}:
        raw["run_watch"] = policy
    roster.write_text(watcher.recruiter.yaml.safe_dump(raw))
    result = subprocess.run(
        [
            "just",
            "upagent-run-watch",
            str(tmp_path / "run"),
            "--roster",
            str(roster),
            "--once",
        ],
        cwd=HERE.parents[4],
        env=cli_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode == 0) == valid, result.stderr
    if valid:
        state = watcher.read_json(tmp_path / "run/control/run-watch.json")
        assert state["run_watch"] == {
            "enabled": enabled,
            "interval_minutes": 5,
            "drain_minutes": 5,
        }
        assert state["state"] == "orphaned"
        assert state["ownership"]["process_start_time"]
    else:
        assert "run_watch" in result.stderr
        assert not (tmp_path / "run/control/run-watch.json").exists()


@pytest.fixture
def canonical_worker(tmp_path, monkeypatch):
    helpers = watcher._load("public_api_test")
    roster_path = tmp_path / watcher.offerings.ROSTER_RELATIVE_PATH
    roster_path.parent.mkdir(parents=True)
    roster_path.write_text(watcher.offerings.render_roster(["standard"]))
    monkeypatch.setattr(helpers.recruiter, "verify_cockpit_pane", lambda _: None)
    store, registered, ledger, token, _ = helpers._registered_request(
        tmp_path, monkeypatch
    )
    order = helpers.recruiter.load_order(registered.order_path)
    key = ledger.key_for_order(order)
    # The normal launcher owns these records. This fixture only creates local
    # ledger evidence; it never invokes a request command or opens a worker.
    launch = ledger.begin_launch(
        key,
        token,
        "worker",
        "worker-name",
        "test-session",
        str(tmp_path),
        metadata={"attempt": 1, "generation": 1},
    )
    journal_path = ledger.launch_journal_path(key, launch)
    journal = watcher.read_json(journal_path)
    journal.update(
        state="started",
        pane="worker-pane",
        workspace_id="workspace",
        started_at_ns=time.time_ns(),
    )
    watcher.write_json(journal_path, journal)
    state = ledger.state(key)
    state.update(
        state="running",
        order_id=order["order_id"],
        request_id=registered.request_id,
        generation=1,
    )
    watcher.write_json(ledger.request_dir(key) / "state/latest.json", state)
    data = helpers.public_api._public_status(store, registered)
    root = tmp_path / "flow"
    ctx = receipt(root)
    record = watcher.register_worker(root, data)
    runtime = watcher.Runtime(ctx, watcher.recruiter.load_roster(roster_path))
    return runtime, record, ledger, key, journal_path


def test_resolves_real_public_response_and_rejects_retry(canonical_worker):
    runtime, record, ledger, key, journal_path = canonical_worker
    target = runtime.resolve(record)
    assert target.identity.pane_id == "worker-pane"
    assert target.identity.owner_id == record["request_id"]
    assert runtime.nudges.state_path(target.identity) == auth.NudgeAuthority(
        ledger.root
    ).state_path(target.identity)
    state = ledger.state(key)
    state["generation"] = 2
    watcher.write_json(ledger.request_dir(key) / "state/latest.json", state)
    with pytest.raises(watcher.RunWatchError, match="generation mismatch"):
        runtime.resolve(record)
    state["generation"] = 1
    watcher.write_json(ledger.request_dir(key) / "state/latest.json", state)
    journal = watcher.read_json(journal_path)
    journal["generation"] = 2
    watcher.write_json(journal_path, journal)
    with pytest.raises(watcher.RunWatchError, match="launch generation mismatch"):
        runtime.resolve(record)


def test_terminal_ledger_wins_over_deleted_pane(canonical_worker, monkeypatch):
    runtime, record, ledger, key, journal_path = canonical_worker
    target = runtime.resolve(record)
    state = ledger.state(key)
    state["state"] = "finished"
    watcher.write_json(ledger.request_dir(key) / "state/latest.json", state)
    journal_path.unlink()
    monkeypatch.setattr(
        runtime, "herdr", lambda *_: pytest.fail("terminal evidence must avoid Herdr")
    )
    assert runtime.resolve(record).terminal
    assert runtime.probe(target).classify() == auth.Verdict.FINISHED


def test_identity_recheck_before_send(canonical_worker, monkeypatch):
    runtime, record, _, _, journal_path = canonical_worker
    target = runtime.resolve(record)
    calls = []

    def presence(identity, harness):
        calls.append(identity)
        if len(calls) == 1:
            journal = watcher.read_json(journal_path)
            journal["pane"] = "replacement-pane"
            watcher.write_json(journal_path, journal)
        return auth.Probe(identity, "idle", False, auth.PaneState.PRESENT, harness)

    monkeypatch.setattr(runtime, "presence", presence)
    with pytest.raises(watcher.RunWatchError, match="identity changed"):
        runtime.nudges.request_nudge(
            target.identity,
            "continue",
            "run-watch",
            lambda: runtime.probe(target),
            lambda _: pytest.fail("must never send to a replaced worker"),
        )


class FakeHerdr:
    """Model the actual Herdr JSON boundaries for one-shot Checker tests."""

    def __init__(self):
        self.panes = {
            "worker-pane": {
                "pane_id": "worker-pane",
                "workspace_id": "workspace",
                "tab_id": "tab",
                "agent_status": "idle",
            }
        }
        self.agents = {
            "worker-name": {
                "name": "worker-name",
                "pane_id": "worker-pane",
                "workspace_id": "workspace",
            }
        }
        self.commands = []
        self.process_exit = False
        self.keep_on_close = False

    def __call__(self, session, *args):
        self.commands.append(args)
        if args[:2] == ("pane", "get"):
            if args[2] not in self.panes:
                raise watcher.recruiter.RecruiterError("pane_not_found")
            return {"result": {"pane": self.panes[args[2]]}}
        if args[:2] == ("agent", "get"):
            return {"result": {"agent": self.agents[args[2]]}}
        if args[:2] == ("agent", "start"):
            name = args[2]
            pane = "checker-pane"
            self.panes[pane] = {
                "pane_id": pane,
                "workspace_id": "workspace",
                "agent_status": "working",
            }
            self.agents[name] = {
                "name": name,
                "pane_id": pane,
                "workspace_id": "workspace",
            }
            return {"result": {"agent": self.agents[name]}}
        if args[:2] == ("pane", "close"):
            if not self.keep_on_close:
                self.panes.pop(args[2])
            return {"result": {}}
        if args[:2] == ("pane", "process-info"):
            return {
                "result": {
                    "process_info": {
                        "foreground_processes": []
                        if self.process_exit
                        else [{"pid": 123}]
                    }
                }
            }
        raise AssertionError(args)


@pytest.mark.parametrize("outcome", ["valid", "malformed", "none", "exit", "timeout"])
def test_checker_real_selection_render_parse_and_cleanup(
    canonical_worker, monkeypatch, outcome
):
    runtime, record, *_ = canonical_worker
    target = runtime.resolve(record)
    herdr = FakeHerdr()
    monkeypatch.setattr(runtime, "herdr", herdr)
    monkeypatch.setattr(runtime, "recent_output", lambda _: "one bounded snapshot")
    directory = runtime.ctx.control_dir / "checks/check-one"
    launch = runtime.checker_start(target, directory, 1000)
    assert launch["provider"] != watcher.recruiter._worker_provider(target.order)
    assert len([c for c in herdr.commands if c[:2] == ("agent", "start")]) == 1
    assert (
        "then exit" in [c[-1] for c in herdr.commands if c[:2] == ("agent", "start")][0]
    )
    assert "one evidence snapshot" in (directory / "brief.md").read_text()
    now = 1001
    if outcome == "valid":
        watcher.write_json(
            Path(launch["output"]),
            {
                "request_id": record["request_id"],
                "generation": 1,
                "assessment": "suspected-stall",
                "confidence": 0.8,
                "evidence": ["unchanged"],
                "recommended_action": "inspect",
                "message": "needs inspection",
            },
        )
    elif outcome == "malformed":
        Path(launch["output"]).write_text('{"request_id": "wrong"}')
    elif outcome == "exit":
        herdr.process_exit = True
    elif outcome == "timeout":
        now = 1285
    elif outcome == "none":
        herdr.panes.pop("checker-pane")
    done, message = runtime.checker_poll(launch, now)
    assert done
    runtime.checker_close(launch)
    assert "checker-pane" not in herdr.panes
    assert now - 1000 <= 300
    if outcome == "valid":
        assert message == "needs inspection"
    elif outcome == "malformed":
        assert "malformed" in message
    else:
        assert message is None


def test_checker_cleanup_refuses_reused_pane_and_proves_absence(
    canonical_worker, monkeypatch
):
    runtime, record, *_ = canonical_worker
    herdr = FakeHerdr()
    monkeypatch.setattr(runtime, "herdr", herdr)
    monkeypatch.setattr(runtime, "recent_output", lambda _: "snapshot")
    launch = runtime.checker_start(
        runtime.resolve(record), runtime.ctx.control_dir / "checks/check", 1000
    )
    herdr.agents[launch["name"]]["pane_id"] = "replacement"
    with pytest.raises(watcher.RunWatchError, match="identity unverified"):
        runtime.checker_close(launch)
    assert not any(c[:2] == ("pane", "close") for c in herdr.commands)
    herdr.agents[launch["name"]]["pane_id"] = "checker-pane"
    herdr.keep_on_close = True
    with pytest.raises(watcher.RunWatchError, match="still present"):
        runtime.checker_close(launch)


def test_checker_deadline_does_not_pause_sweeps_or_finish(tmp_path):
    runtime = FakeRuntime(tmp_path)
    target = runtime.add("worker", "working")
    now = [1000.0]
    watch = watcher.Watch(
        tmp_path, {"interval_minutes": 1}, runtime, clock=lambda: now[0]
    )
    # Reserve an actual authority episode before escalating.
    runtime.probes["worker"] = auth.Probe(
        target.identity, "idle", False, auth.PaneState.PRESENT, "claude"
    )
    runtime.nudges.request_nudge(
        target.identity,
        "continue",
        "run-watch",
        lambda: runtime.probe(target),
        lambda _: None,
    )
    watch.escalate(target, 1)
    assert watch.tick()
    now[0] += 60
    assert watch.tick()
    assert watch.data["last_sweep"] == now[0]
    watch.stop = True
    assert watch.tick()
    assert len(runtime.closed) == 1
    assert watch.data["state"] == "draining"


def test_validated_implementer_predicate_and_exec_hold(tmp_path, monkeypatch):
    ctx = receipt(tmp_path, harness="codex")
    runtime = watcher.Runtime(ctx, watcher.recruiter.load_roster(HERE / "upagent.yaml"))
    target = runtime.implementer()
    monkeypatch.setattr(
        runtime,
        "presence",
        lambda identity, harness: auth.Probe(
            identity, "idle", False, auth.PaneState.PRESENT, harness
        ),
    )
    assert runtime.probe(target).classify() == auth.Verdict.HOLD
    watcher.write_json(
        ctx.result_path,
        {
            "verdict": "passed",
            "summary": "done",
            "run_root": str(tmp_path),
            "run_id": "wrong",
        },
    )
    assert not watcher.awaiter.has_terminal_result(ctx)
    watcher.write_json(
        ctx.result_path,
        {
            "verdict": "passed",
            "summary": "done",
            "run_root": str(tmp_path),
            "run_id": ctx.run_id,
        },
    )
    monkeypatch.setattr(
        runtime, "presence", lambda *_: pytest.fail("valid result precedes probe")
    )
    assert runtime.probe(target).classify() == auth.Verdict.FINISHED


def test_real_cli_sigterm_and_single_owner(tmp_path):
    root = tmp_path / "run"
    ctx = receipt(root, harness="codex")
    # Valid result avoids all Herdr operations but keeps the watch alive until finish.
    watcher.write_json(
        ctx.result_path,
        {
            "verdict": "passed",
            "summary": "done",
            "run_root": str(root),
            "run_id": root.name,
        },
    )
    command = [
        sys.executable,
        str(HERE / "client.py"),
        "--target",
        "run-watch",
        str(root),
        "--roster",
        str(HERE / "upagent.yaml"),
    ]
    process = subprocess.Popen(
        command,
        env=cli_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        path = root / "control/run-watch.json"
        while not path.exists() or watcher.read_json(path).get("last_sweep") is None:
            assert time.monotonic() < deadline
            assert process.poll() is None
            time.sleep(0.05)
        duplicate = subprocess.run(
            command + ["--once"],
            env=cli_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert duplicate.returncode != 0 and "already owns" in duplicate.stderr
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["state"] == "finished"
        assert watcher.read_json(path)["remaining_request_ids"] == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_real_cli_disabled_touches_no_panes(tmp_path):
    root = tmp_path / "run"
    receipt(root)
    roster = tmp_path / "roster.yaml"
    raw = watcher.recruiter.yaml.safe_load((HERE / "upagent.yaml").read_text())
    raw["run_watch"] = {"enabled": False}
    roster.write_text(watcher.recruiter.yaml.safe_dump(raw))
    result = subprocess.run(
        ["just", "upagent-run-watch", str(root), "--roster", str(roster)],
        cwd=HERE.parents[4],
        env=cli_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "disabled"
    assert not (tmp_path / "ledger").exists()


def test_real_cli_registers_redacted_response_without_recruiting(tmp_path):
    source = tmp_path / "response.json"
    watcher.write_json(source, response())
    command = ["just", "upagent-register-worker", str(tmp_path / "run"), str(source)]
    first = subprocess.run(
        command,
        cwd=HERE.parents[4],
        env=cli_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert first.returncode == 0, first.stderr
    second = subprocess.run(
        command,
        cwd=HERE.parents[4],
        env=cli_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert not (tmp_path / "ledger").exists()
    watcher.write_json(source, response(generation=2))
    retry = subprocess.run(
        command,
        cwd=HERE.parents[4],
        env=cli_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert retry.returncode != 0 and "generation mismatch" in retry.stderr


def test_drain_gone_worker_has_advisory_but_no_live_remainder(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.add("gone", presence=auth.PaneState.GONE)
    watch = watcher.Watch(tmp_path, {}, runtime)
    watch.stop = True
    assert not watch.tick()
    assert watch.data["state"] == "finished"
    assert watch.data["remaining_request_ids"] == []
    assert runtime.events[0][0] == "run-watch:gone:gone-pane"


def test_hourly_dedupe_survives_restart_and_republishes_in_real_journal(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.add("gone", presence=auth.PaneState.GONE)
    real = watcher.Runtime(
        runtime.ctx, watcher.recruiter.load_roster(HERE / "upagent.yaml")
    )
    runtime.publish = real.publish
    now = [1000.0]
    watch = watcher.Watch(tmp_path, {}, runtime, clock=lambda: now[0])
    watch.sweep()
    restarted = watcher.Watch(tmp_path, {}, runtime, clock=lambda: now[0])
    restarted.sweep()
    assert len(watcher.awaiter.read_journal(runtime.ctx)) == 1
    now[0] += 3600
    restarted.sweep()
    assert len(watcher.awaiter.read_journal(runtime.ctx)) == 2


def test_poll_interval_and_sweep_interval_are_separate(tmp_path):
    runtime = FakeRuntime(tmp_path)
    now = [1000.0]
    watch = watcher.Watch(
        tmp_path, {"interval_minutes": 2}, runtime, clock=lambda: now[0]
    )
    assert watcher.POLL_SECONDS == 5
    watch.tick()
    now[0] += 5
    watch.tick()
    assert watch.data["last_sweep"] == 1000
    now[0] += 115
    watch.tick()
    assert watch.data["last_sweep"] == 1120


def test_recover_checker_created_between_reservation_and_status_save(tmp_path):
    runtime = FakeRuntime(tmp_path)
    now = [1000.0]
    watch = watcher.Watch(tmp_path, {}, runtime, clock=lambda: now[0])
    watch.data["checks"]["check"] = {"state": "reserved", "target_pane": "worker-pane"}
    watch.save()
    launch = {
        "state": "running",
        "provider": "other",
        "request_id": "worker",
        "generation": 1,
        "target_pane": "worker-pane",
        "deadline": 999,
    }
    watcher.write_json(tmp_path / "control/checks/check/launch.json", launch)
    restarted = watcher.Watch(tmp_path, {}, runtime, clock=lambda: now[0])
    restarted.poll_checks()
    assert len(runtime.closed) == 1
    assert restarted.data["checks"]["check"]["verified_absent"]
    assert len(runtime.checks) == 0


def test_interrupted_checker_reservation_is_reported_without_rehire(tmp_path):
    runtime = FakeRuntime(tmp_path)
    watch = watcher.Watch(tmp_path, {}, runtime)
    watch.data["checks"]["check"] = {"state": "reserved", "target_pane": "worker-pane"}
    watch.save()
    restarted = watcher.Watch(tmp_path, {}, runtime)
    restarted.poll_checks()
    assert restarted.data["checks"]["check"]["state"] == "failed"
    assert "interrupted before launch" in runtime.events[0][1]
    assert not runtime.checks


def test_receipt_metadata_update_keeps_identity_but_pane_change_rejects(
    tmp_path, monkeypatch
):
    ctx = receipt(tmp_path)
    runtime = watcher.Runtime(ctx, watcher.recruiter.load_roster(HERE / "upagent.yaml"))
    target = runtime.implementer()
    monkeypatch.setattr(
        runtime,
        "presence",
        lambda identity, harness: auth.Probe(
            identity, "working", False, auth.PaneState.PRESENT, harness
        ),
    )
    updated = {**ctx.receipt, "ownership": {"run_watch": {"pid": os.getpid()}}}
    watcher.write_json(ctx.control_dir / "implementer-start.json", updated)
    assert runtime.probe(target).classify() == auth.Verdict.WORKING
    updated["implementer_pane"] = "reused"
    watcher.write_json(ctx.control_dir / "implementer-start.json", updated)
    with pytest.raises(watcher.RunWatchError, match="identity changed"):
        runtime.probe(target)


def test_registry_mismatch_is_typed_advisory_and_remains_in_drain(canonical_worker):
    runtime, record, ledger, key, _ = canonical_worker
    state = ledger.state(key)
    state["generation"] = 2
    watcher.write_json(ledger.request_dir(key) / "state/latest.json", state)
    watch = watcher.Watch(runtime.ctx.run_root, {}, runtime)
    watch.stop = True
    assert watch.tick()
    assert watch.data["remaining_request_ids"] == [record["request_id"]]
    events = watcher.awaiter.read_journal(runtime.ctx)
    assert (
        events[0]["kind"] == "advisory" and "registry-rejected" in events[0]["summary"]
    )


def test_bound_checker_has_only_one_evidence_read(canonical_worker, monkeypatch):
    runtime, record, *_ = canonical_worker
    herdr = FakeHerdr()
    reads = []
    monkeypatch.setattr(runtime, "herdr", herdr)
    monkeypatch.setattr(
        runtime, "recent_output", lambda target: reads.append(target) or "snapshot"
    )
    target = runtime.resolve(record)
    launch = runtime.checker_start(target, runtime.ctx.control_dir / "checks/one", 1000)
    for now in (1005, 1010, 1100):
        assert runtime.checker_poll(launch, now) == (False, None)
    assert runtime.checker_poll(launch, launch["deadline"]) == (True, None)
    runtime.checker_close(launch)
    assert len(reads) == 1
    assert launch["deadline"] - 1000 < 300


def test_busy_shared_authority_cannot_block_checker_deadline(tmp_path):
    import fcntl

    runtime = FakeRuntime(tmp_path)
    target = runtime.add("worker")
    now = [1000.0]
    watch = watcher.Watch(tmp_path, {}, runtime, clock=lambda: now[0])
    path = runtime.nudges.state_path(target.identity).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as competing_owner:
        fcntl.flock(competing_owner, fcntl.LOCK_EX)
        watch.sweep()
    assert runtime.sent == []
    assert not runtime.nudges.state_path(target.identity).exists()
    watch.sweep()
    assert runtime.sent == [("worker", "continue")]
