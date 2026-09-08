"""Local Flow 1 lifecycle tests. Herdr and management are deterministic fakes."""

from __future__ import annotations

import importlib.util
import multiprocessing
import shlex
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "flow1_controller_test", HERE / "implementer_controller.py"
)
assert spec and spec.loader
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)
f = controller.supervision
recruiter = f._load("recruiter")
controller._bind_recruiter_runtime(recruiter)
awaiter = f._load("implementer_await")


class LocalHerdr:
    def __init__(self, root):
        self.root = root
        self.panes = {}
        self.closed = []
        self.sent = []
        self.births = {}
        self.argv = {}
        self.manager = "approved"
        self.health_failure = False
        self.mismatch = None
        self.add("implementer", "plan-implementer-run")

    def add(self, pane, name, agent="claude"):
        pid = len(self.births) + 100
        self.births[pid] = str(pid)
        self.argv[pid] = [agent, "--model", "sonnet", "--effort", "medium"]
        self.panes[pane] = {
            "pane_id": pane,
            "workspace_id": "workspace",
            "tab_id": "control",
            "agent_name": name,
            "agent": agent,
            "agent_status": "working",
            "cwd": str(self.root),
            "foreground_cwd": str(self.root),
            "pid": pid,
        }
        return pane

    def rpc(self, *args, **kwargs):
        if args[:2] == ("pane", "get"):
            if args[2] not in self.panes:
                raise recruiter.RecruiterError("pane_not_found")
            return {"result": {"pane": dict(self.panes[args[2]])}}
        if args[:2] == ("agent", "get"):
            p = next(
                (p for p in self.panes.values() if p["agent_name"] == args[2]), None
            )
            if not p:
                raise recruiter.RecruiterError("agent_not_found")
            return {
                "result": {
                    "agent": {
                        "name": p["agent_name"],
                        "pane_id": p["pane_id"],
                        "workspace_id": p["workspace_id"],
                    }
                }
            }
        if args[:2] == ("pane", "process-info"):
            p = self.panes[args[-1]]
            processes = (
                [
                    {
                        "pid": p["pid"],
                        "name": p["agent"],
                        "cmdline": " ".join(self.argv[p["pid"]]),
                    }
                ]
                if p["pid"] in self.births
                else []
            )
            return {"result": {"process_info": {"foreground_processes": processes}}}
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": list(self.panes.values())}}
        if args[:2] == ("pane", "close"):
            self.closed.append(args[2])
            del self.panes[args[2]]
            return {"result": {}}
        if args[:2] == ("pane", "run"):
            self.sent.append((args[2], args[3]))
            return {"result": {}}
        if args[:2] == ("agent", "start"):
            name = args[2]
            words = shlex.split(args[-1])
            pane = self.add(name, name, "codex")
            if "--output" in words:
                output = Path(words[words.index("--output") + 1])
                if output.name == "decision.json" and self.manager != "timeout":
                    f.write(
                        output,
                        {
                            "request_id": "run",
                            "generation": 1,
                            "decision": self.manager,
                            "message": "Literal manager advisory",
                            "requested_changes": [],
                        },
                    )
            return {
                "result": {
                    "agent": {
                        "name": name,
                        "pane_id": pane,
                        "workspace_id": "workspace",
                    }
                }
            }
        raise AssertionError(args)

    def health(self, pane, **kwargs):
        if self.health_failure:
            raise recruiter.RecruiterError("health failed")
        return {
            "healthy": True,
            "expected_agent": kwargs["expected_agent"],
            "expected_process": kwargs["expected_process"],
            "cwd": kwargs["expected_cwd"],
            "process_pid": self.panes[pane]["pid"],
        }


@pytest.fixture
def local(tmp_path, monkeypatch):
    fake = LocalHerdr(tmp_path)
    monkeypatch.setattr(recruiter, "_herdr_json", fake.rpc)
    monkeypatch.setattr(recruiter, "_wait_for_agent_health", fake.health)
    monkeypatch.setattr(
        f.process, "process_start_time", lambda pid: fake.births.get(pid)
    )
    monkeypatch.setattr(
        f.process, "process_cmdline", lambda pid: fake.argv.get(pid, [])
    )
    monkeypatch.setattr(
        controller, "_start_gated", lambda *args: ("implementer", "workspace")
    )
    monkeypatch.setattr(controller, "_verify_gated", lambda *args: None)
    monkeypatch.setattr(controller, "_place_in_control_tab", lambda pane, *args: pane)
    monkeypatch.setattr(controller, "_release_gate", lambda path, token: path.unlink())
    monkeypatch.setattr(
        controller,
        "_resolve_offering",
        lambda *args: {
            "id": "test-offering",
            "harness": "claude",
            "model": "sonnet",
            "effort": "medium",
        },
    )
    monkeypatch.setattr(
        controller,
        "_health",
        lambda pane, cwd, *args: fake.health(
            pane,
            expected_agent="claude",
            expected_process="claude",
            expected_cwd=str(cwd),
        ),
    )
    monkeypatch.setattr(controller.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        recruiter, "_resolve_current_herdr_session_name", lambda: "test"
    )
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "hil")
    monkeypatch.delenv("UPAGENT_CANONICAL_REPO", raising=False)
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("UPAGENT_STATE", str(tmp_path / "service.json"))
    roster = tmp_path / "roster.yaml"
    roster.write_text("""harnesses:
  claude: 'claude {model}'
plan_implementers:
  claude: 'claude --model {model} --effort {effort}'
management:
  account_manager:
    command: 'codex --output {output_path}'
    expected_agent: codex
    expected_process: codex
    timeout_ms: 10
  sentinel:
    command: 'codex --output {output_path}'
    expected_agent: codex
    expected_process: codex
    timeout_ms: 10
run_watch:
  enabled: false
""")
    plan = tmp_path / "plan.md"
    plan.write_text("# Approved plan\n")
    fake.inputs = dict(
        plan_path=plan,
        offering_id="test-offering",
        effort="medium",
        run_root=tmp_path / "run",
        hil_pane="hil",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    fake.path = tmp_path / "run/control/implementer-start.json"
    return fake


def result(path, **over):
    f.write(
        path.parent.parent / "implementer-result.json",
        {
            "run_id": "run",
            "run_root": str(path.parent.parent),
            "summary": "Done",
            "verdict": "passed",
            **over,
        },
    )


def events(path):
    return awaiter.read_journal(awaiter.ImplementerContext(path, allow_incomplete=True))


@pytest.mark.parametrize(
    "decision", ["approved", "needs-requester", "blocked", "timeout"]
)
def test_manager_advisory_and_timeout_close(local, decision):
    local.manager = decision
    receipt = controller.start_implementer(**local.inputs)
    assert receipt["state"] == ("ready" if decision == "approved" else "ready-degraded")
    assert receipt["manager"]["decision"] == (
        "degraded" if decision == "timeout" else decision
    )
    assert receipt["manager"]["pane_id"] in local.closed
    assert receipt["sentinel"]["state"] == "started"
    if decision != "approved":
        assert events(local.path)[-1]["kind"] == "startup-degraded"
        assert events(local.path)[-1]["summary"] == receipt["startup_advisory"]


@pytest.mark.parametrize("field", ["cwd", "agent", "effort", "script"])
def test_mechanical_mismatch_records_failed_and_closes(local, monkeypatch, field):
    original = controller._health

    def health(*args):
        value = original(*args)
        if field == "cwd":
            local.panes["implementer"]["foreground_cwd"] = "/different"
        elif field == "agent":
            local.panes["implementer"]["agent"] = "pi"
        elif field == "effort":
            local.inputs["effort"] = "low"
            # Break the rendered command itself, after resolution.
            script = local.path.parent / "start.sh"
            script.write_text(
                script.read_text().replace("--effort medium", "--effort low")
            )
        else:
            (local.path.parent / "start.sh").write_text("tampered")
        return value

    monkeypatch.setattr(controller, "_health", health)
    with pytest.raises(f.SupervisionError, match="mismatch"):
        controller.start_implementer(**local.inputs)
    assert f.read(local.path)["state"] == "failed"
    assert local.closed == ["implementer"]


def test_budget_rejected_before_start(local):
    path = Path(local.inputs["roster_path"])
    path.write_text(path.read_text().replace("timeout_ms: 10", "timeout_ms: 300000"))
    with pytest.raises(controller.ImplementerStartError, match="480"):
        controller.start_implementer(**local.inputs)
    assert not local.path.exists()
    assert not local.closed


@pytest.mark.parametrize("policy", [{"drain_minutes": 7}, {"drain_minutes": 100}])
def test_finish_budget_before_creation(local, policy):
    raw = recruiter.load_roster(local.inputs["roster_path"])
    raw["run_watch"] = policy
    Path(local.inputs["roster_path"]).write_text(recruiter.yaml.safe_dump(raw))
    with pytest.raises(controller.ImplementerStartError, match="480"):
        controller.start_implementer(**local.inputs)
    assert not local.path.exists()


def test_finish_idempotent_forced_cancel_and_post_terminal_cleanup(local):
    controller.start_implementer(**local.inputs)
    local.panes["implementer"]["workspace_id"] = "reused"
    finish = controller.finish_implementer(local.path, force=True)
    assert finish["verdict"] == "cancelled" and finish["forced"]
    assert finish["cleanup"][-1]["status"] == "cleanup-failed"
    assert "implementer" not in local.closed
    assert events(local.path)[0]["kind"] == "cancelled"
    assert events(local.path)[-1]["dedupe_key"] == "flow1:cleanup-failed:implementer"
    before = local.path.with_name("implementer-finish.json").read_bytes()
    assert controller.finish_implementer(local.path) == finish
    assert local.path.with_name("implementer-finish.json").read_bytes() == before


@pytest.mark.parametrize("change", ["pid", "argv", "pane", "gone", "exited"])
def test_finish_fences_reused_process_and_pane(local, change):
    controller.start_implementer(**local.inputs)
    result(local.path)
    pid = local.panes["implementer"]["pid"]
    if change == "pid":
        local.births[pid] = "reused"
    elif change == "argv":
        local.argv[pid] = ["different"]
    elif change == "pane":
        local.panes["implementer"]["agent_name"] = "different"
    elif change == "gone":
        del local.panes["implementer"]
    elif change == "exited":
        del local.births[pid]
    finish = controller.finish_implementer(local.path)
    entry = finish["cleanup"][-1]
    assert entry["status"] == {"gone": "already-gone", "exited": "closed"}.get(
        change, "cleanup-failed"
    )
    assert finish["verdict"] == "passed"


def test_finish_interrupted_after_step_three_resumes(local, monkeypatch):
    controller.start_implementer(**local.inputs)
    result(local.path)
    original = f.write

    def interrupt(path, value):
        original(path, value)
        if path.name == "finishing.json" and value["step"] == 3:
            raise KeyboardInterrupt()

    monkeypatch.setattr(f, "write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        controller.finish_implementer(local.path)
    closed = list(local.closed)
    monkeypatch.setattr(f, "write", original)
    finish = controller.finish_implementer(local.path)
    assert local.closed == closed + ["implementer"]
    assert f.read(local.path.with_name("finishing.json"))["step"] == 7
    assert finish["verdict"] == "passed"


def test_partial_start_force_finish(local, monkeypatch):
    monkeypatch.setattr(
        controller,
        "_health",
        lambda *args: (_ for _ in ()).throw(
            controller.ImplementerStartError("health failed")
        ),
    )
    with pytest.raises(controller.ImplementerStartError):
        controller.start_implementer(**local.inputs)
    finish = controller.finish_implementer(local.path, force=True)
    assert finish["verdict"] == "cancelled"
    assert local.closed == ["implementer"]


@pytest.mark.parametrize("kind", list(awaiter.contracts.EVENT_KINDS))
def test_only_cleanup_advisories_after_terminal(local, kind):
    controller.start_implementer(**local.inputs, supervise=False)
    ctx = awaiter.ImplementerContext(local.path)
    awaiter.publish_event(ctx, "completed", "done")
    assert awaiter.publish_event(
        ctx, "advisory", "cleanup failed", dedupe_key="flow1:cleanup-failed:one"
    )
    assert awaiter.publish_event(ctx, kind, "other", dedupe_key="other") is None
    allowed = awaiter.publish_event(
        ctx, kind, "cleanup again", dedupe_key="flow1:cleanup-failed:two"
    )
    assert bool(allowed) == (kind == "advisory")
    events(local.path)


def test_journal_rejects_forged_event_after_cleanup_advisory(local):
    controller.start_implementer(**local.inputs, supervise=False)
    ctx = awaiter.ImplementerContext(local.path)
    terminal = awaiter.publish_event(ctx, "completed", "done")
    awaiter.publish_event(
        ctx, "advisory", "cleanup", dedupe_key="flow1:cleanup-failed:one"
    )
    f.write(
        ctx.events_dir / "00000003.json",
        {
            **terminal,
            "sequence": 3,
            "event_id": "evt-forged",
            "kind": "progress",
            "terminal": False,
        },
    )
    with pytest.raises(awaiter.contracts.ContractError, match="terminal"):
        awaiter.read_journal(ctx)


def reconciler(local, monkeypatch):
    controller.start_implementer(**local.inputs)
    ctx = awaiter.ImplementerContext(local.path)
    rw = f._load("run_watch")
    monkeypatch.setattr(rw, "recruiter", recruiter)
    runtime = rw.Runtime(ctx, {})
    runtime.nudges = rw.authority.NudgeAuthority(local.root / "ledger")
    return f.Reconciler(ctx, recruiter, runtime)


def test_0342_replay_and_restart_nudges_second_probe(local, monkeypatch):
    r = reconciler(local, monkeypatch)
    local.panes["implementer"]["agent_status"] = "done"
    r.tick()
    assert local.sent == []
    r = f.Reconciler(r.ctx, recruiter, r.runtime)
    r.tick()
    assert local.sent[0] == ("implementer", "continue")
    assert local.sent[1][1].startswith("SENTINEL_STALL_NUDGED")
    assert not events(local.path)
    assert (local.path.parent / "sentinel/wake").exists()


def test_cap_exhaustion_once_and_working_closes_episode(local, monkeypatch):
    r = reconciler(local, monkeypatch)
    local.panes["implementer"]["agent_status"] = "idle"
    now = [1000.0]
    monkeypatch.setattr(r.authority.time, "time", lambda: now[0])
    r.tick()
    for delay in (0, 301, 901, 901, 901):
        now[0] += delay
        r.tick()
    assert sum(e["kind"] == "leader-stalled" for e in events(local.path)) == 1
    assert sum(p == "implementer" for p, _ in local.sent) == 3
    local.panes["implementer"]["agent_status"] = "working"
    r.tick()
    local.panes["implementer"]["agent_status"] = "idle"
    r.tick()
    r.tick()
    assert sum(p == "implementer" for p, _ in local.sent) == 4


@pytest.mark.parametrize(
    "harness,status,gone,kind",
    [
        ("codex", "idle", False, None),
        ("codex", "idle", True, "leader-missing"),
        ("claude", "blocked", False, "decision-required"),
    ],
)
def test_exec_and_blocked_mappings(local, monkeypatch, harness, status, gone, kind):
    r = reconciler(local, monkeypatch)
    r.ctx.receipt["harness"] = harness
    f.write(local.path, r.ctx.receipt)
    r.target = r.runtime.implementer()
    local.panes["implementer"]["agent_status"] = status
    if gone:
        del local.panes["implementer"]
    r.tick()
    r.tick()
    assert not local.sent
    assert [e["kind"] for e in events(local.path)] == ([kind] if kind else [])


@pytest.mark.parametrize(
    "outcome",
    ["COMPLETE", "NEVER_STARTED", "STALLED", "FINALIZATION_FAILED", "malformed"],
)
def test_sentinel_outcomes_and_restart(local, monkeypatch, outcome):
    r = reconciler(local, monkeypatch)
    local.panes["implementer"]["agent_status"] = "idle"
    closeout = {
        "request_id": "run",
        "order_id": "flow1-run",
        "outcome": outcome,
        "interpretation": "Literal Sentinel report",
        "citations": [str(local.inputs["plan_path"]), "/not-a-real-citation"],
        "exchanges": [],
        "bundle": str(local.path.parent.parent),
        "progress_so_far": "work",
        "last_alive": "03:42Z",
    }
    path = local.path.parent / "sentinel/closeout-a1.json"
    f.write(path, closeout)
    r.tick()
    r = f.Reconciler(r.ctx, recruiter, r.runtime)
    r.tick()
    data = f.read(r.cursor_path)
    if outcome == "COMPLETE":
        assert any(e["kind"] == "invalid-result" for e in events(local.path))
        assert not data.get("complete")
    elif outcome == "NEVER_STARTED":
        assert data["landing_retries"] == 1
        f.write(path, closeout)
        r.tick()
        assert f.read(r.cursor_path)["degraded"]
    elif outcome == "malformed":
        assert any("Invalid Sentinel" in e["summary"] for e in events(local.path))
    else:
        assert ("implementer", "continue") in local.sent
        assert data["last_closeout"]["corroborated_citations"] == [
            str(local.inputs["plan_path"])
        ]
        assert data["last_closeout"]["uncorroborated_citations"] == [
            "/not-a-real-citation"
        ]


def test_sentinel_complete_requires_valid_owner_result(local, monkeypatch):
    r = reconciler(local, monkeypatch)
    result(local.path)
    r.consume({"outcome": "COMPLETE", "citations": []}, cursor := {})
    assert cursor["complete"]
    del local.panes["implementer"]
    r.tick()
    assert not local.sent


def test_dead_sentinel_two_probes_no_relaunch(local, monkeypatch):
    r = reconciler(local, monkeypatch)
    pane = f.read(local.path)["sentinel"]["pane_id"]
    del local.panes[pane]
    r.tick()
    r.tick()
    r.tick()
    assert [e["dedupe_key"] for e in events(local.path)] == ["flow1:sentinel-degraded"]
    assert len(local.panes) == 1


def test_sentinel_provider_exhaustion_degrades(local):
    path = Path(local.inputs["roster_path"])
    path.write_text(
        path.read_text().replace(
            "command: 'codex --output {output_path}'",
            "command: 'claude --output {output_path}'",
        )
    )
    receipt = controller.start_implementer(**local.inputs)
    assert receipt["state"] == "ready-degraded"
    assert any(e["dedupe_key"] == "flow1:sentinel-degraded" for e in events(local.path))


def test_finishing_prevents_all_nudges(local, monkeypatch):
    r = reconciler(local, monkeypatch)
    local.panes["implementer"]["agent_status"] = "idle"
    f.write(local.path.parent / "finishing.json", {"step": 1})
    assert r.nudge("await").outcome == "not-idle"
    assert r.nudge("sentinel").outcome == "not-idle"
    assert not local.sent


@pytest.mark.parametrize("invalid", ["malformed", "wrong-owner"])
def test_invalid_result_does_not_prevent_recovery(local, monkeypatch, invalid):
    r = reconciler(local, monkeypatch)
    if invalid == "malformed":
        (local.path.parent.parent / "implementer-result.json").write_text("{")
    else:
        result(local.path, run_id="another-run")
    local.panes["implementer"]["agent_status"] = "idle"
    r.tick()
    r.tick()
    assert local.sent[0] == ("implementer", "continue")


def test_hil_layer_degraded_start_and_cleanup_contract():
    root = HERE.parents[4]
    layer = (
        root / ".shared-llm/public/layers/slash-commands/common/claude/hil/command.md"
    ).read_text()
    assert "state: ready-degraded" in layer
    assert "startup_advisory` verbatim, then enter the await loop" in layer
    assert "Print every `cleanup` entry verbatim" in layer
    assert "post-terminal advisory" in layer and "flow1:cleanup-failed:" in layer
    assert "supervise=<bool>" in layer and "--no-supervise" in layer
    assert "both start and finish with shell `timeout: 600000`" in layer


@pytest.mark.parametrize(
    "mode",
    [
        "finished",
        "drain-timeout",
        "wait-expiry",
        "pid-reuse",
        "session-mismatch",
        "argv-mismatch",
        "stale-terminal",
    ],
)
def test_full_finish_run_watch_signal_and_bounded_drain(local, monkeypatch, mode):
    receipt = controller.start_implementer(**local.inputs)
    pid = 999
    owner = {
        "pid": pid,
        "process_start_time": "birth",
        "argv": ["python3", "run_watch.py", str(local.path.parent.parent)],
        "argv_marker": "run_watch.py",
        "herdr_session": "test",
        "workspace_id": "workspace",
        "pane_id": None,
        "agent_name": "run-watch",
    }
    local.births[pid] = "birth"
    local.argv[pid] = list(owner["argv"])
    receipt["ownership"]["run-watch"] = owner
    f.write(local.path, receipt)
    f.write(
        local.path.parent / "run-watch.json", {"ownership": owner, "state": "running"}
    )
    result(local.path)
    if mode == "pid-reuse":
        local.births[pid] = "other"
    elif mode == "session-mismatch":
        owner["herdr_session"] = "other"
        f.write(local.path, receipt)
    elif mode == "argv-mismatch":
        local.argv[pid] = ["another-process"]
    elif mode == "stale-terminal":
        f.write(
            local.path.parent / "run-watch.json",
            {"ownership": {**owner, "process_start_time": "old"}, "state": "finished"},
        )
    signals = []

    def terminate(pid, sig):
        signals.append(pid)
        if mode in ("finished", "drain-timeout"):
            f.write(
                local.path.parent / "run-watch.json",
                {
                    "ownership": owner,
                    "state": mode,
                    "remaining_request_ids": ["live-worker"]
                    if mode == "drain-timeout"
                    else [],
                },
            )

    monkeypatch.setattr(f.os, "kill", terminate)
    now = [0.0]
    monkeypatch.setattr(f.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        f.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    finish = controller.finish_implementer(local.path)
    outcome = next(e for e in finish["cleanup"] if e["target"] == "run-watch")
    assert outcome["status"] == (
        mode if mode in ("finished", "drain-timeout") else "cleanup-failed"
    )
    assert now[0] <= 480
    assert signals == (
        []
        if mode in ("pid-reuse", "session-mismatch", "argv-mismatch", "stale-terminal")
        else [pid]
    )
    assert local.closed[-1] == "implementer"
    if outcome["status"] == "cleanup-failed":
        assert events(local.path)[-1]["dedupe_key"] == "flow1:cleanup-failed:run-watch"


def test_concurrent_finish_serializes_and_returns_identical_record(local, monkeypatch):
    controller.start_implementer(**local.inputs)
    result(local.path)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    queue = context.Queue()
    close_log = local.root / "close-log"
    original = f.Panes.close

    def close(self, owner, target):
        if owner:
            with close_log.open("a") as stream:
                stream.write(target + "\n")
        return original(self, owner, target)

    monkeypatch.setattr(f.Panes, "close", close)

    def finish():
        barrier.wait()
        queue.put(controller.finish_implementer(local.path))

    processes = [context.Process(target=finish) for _ in range(2)]
    for child in processes:
        child.start()
    results = [queue.get(timeout=10) for _ in processes]
    for child in processes:
        child.join(10)
        assert child.exitcode == 0
    assert results[0] == results[1]
    assert close_log.read_text().splitlines() == ["sentinel", "manager", "implementer"]


def test_start_launches_watch_after_sentinel_and_records_owner(local, monkeypatch):
    roster = Path(local.inputs["roster_path"])
    roster.write_text(roster.read_text().replace("enabled: false", "enabled: true"))

    def spawn(path, roster_path):
        assert f.read(path.parent / "sentinel/launch.json")["state"] == "started"
        return {
            "pid": 999,
            "process_start_time": "birth",
            "argv_marker": "run-watch",
            "herdr_session": "test",
        }

    monkeypatch.setattr(f, "spawn_watch", spawn)
    receipt = controller.start_implementer(**local.inputs)
    assert receipt["ownership"]["run-watch"]["pid"] == 999


def test_await_loop_uses_reconciler_and_returns_cap_event(local, monkeypatch):
    r = reconciler(local, monkeypatch)
    local.panes["implementer"]["agent_status"] = "done"
    # Import boundary: use the same fake Runtime with the real authority in the
    # actual await loop, including journal delivery and acknowledgements.
    original_module = awaiter.importlib.util.module_from_spec

    def module(spec):
        loaded = original_module(spec)
        if spec.name == "upagent_await_flow1":

            class Loader:
                def exec_module(self, value):
                    value.Reconciler = lambda ctx: r

            spec.loader = Loader()
        return loaded

    monkeypatch.setattr(awaiter.importlib.util, "module_from_spec", module)
    answer = awaiter.await_event(
        local.path,
        timeout_ms=20,
        poll_ms=1,
        reconcile_ms=1,
        inactivity_ms=0,
        escalate_ms=0,
    )
    assert answer["kind"] == "await-heartbeat"
    assert local.sent[0] == ("implementer", "continue")
    assert not any(e["kind"] == "leader-stalled" for e in events(local.path))


def test_rpc_and_lock_wait_share_operation_deadline(local, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(f.time, "monotonic", lambda: now[0])

    @f.bounded
    def operation():
        now[0] += 479
        assert f.allocation(3) == 1
        now[0] += 1
        with pytest.raises(f.SupervisionError, match="480-second"):
            f.Panes(recruiter, {"herdr_session": "test"}).rpc(
                "pane", "close", "implementer"
            )

    operation()
    assert not local.closed


@pytest.mark.parametrize("complete", [True, False])
def test_finish_drains_registered_live_worker_through_watch(
    local, monkeypatch, complete
):
    receipt = controller.start_implementer(**local.inputs)
    support = f._load("run_watch_test")
    watch_module = support.watcher
    runtime = support.FakeRuntime(local.path.parent.parent)
    # The test helper creates its own receipt. Restore the actual start identity.
    f.write(local.path, receipt)
    runtime.ctx = awaiter.ImplementerContext(local.path)
    worker = runtime.add("registered-worker", status="working")
    now = [0.0]
    monkeypatch.setattr(f.time, "monotonic", lambda: now[0])
    watch = watch_module.Watch(
        local.path.parent.parent, {}, runtime, clock=lambda: now[0]
    )
    owner = {
        "pid": 999,
        "process_start_time": "birth",
        "argv": ["python3", "run_watch.py"],
        "argv_marker": "run_watch.py",
        "herdr_session": "test",
    }
    local.births[999] = "birth"
    local.argv[999] = owner["argv"]
    receipt["ownership"]["run-watch"] = owner
    f.write(local.path, receipt)
    watch.data["ownership"] = owner
    watch.save()
    result(local.path)

    def signal_watch(pid, signal):
        assert pid == 999
        watch.stop = True
        assert watch.tick()
        assert watch.data["remaining_request_ids"] == ["registered-worker"]

    monkeypatch.setattr(f.os, "kill", signal_watch)

    def advance(seconds):
        now[0] += seconds
        if complete:
            worker.terminal = True
        watch.tick()

    monkeypatch.setattr(f.time, "sleep", advance)
    finish = controller.finish_implementer(local.path)
    entry = next(e for e in finish["cleanup"] if e["target"] == "run-watch")
    assert entry["status"] == ("finished" if complete else "drain-timeout")
    assert entry["remaining_request_ids"] == ([] if complete else ["registered-worker"])
    assert now[0] <= 360
    assert local.closed[-1] == "implementer"


def test_event_order_retains_terminal_across_multiple_cleanup_advisories():
    contract = awaiter.contracts
    first = {"sequence": 1, "kind": "completed", "terminal": True, "event_id": "one"}
    previous = contract.validate_event_order(None, first)
    for sequence in (2, 3):
        previous = contract.validate_event_order(
            previous,
            {
                "sequence": sequence,
                "kind": "advisory",
                "terminal": False,
                "event_id": str(sequence),
                "dedupe_key": "flow1:cleanup-failed:test",
            },
        )
    with pytest.raises(contract.ContractError, match="terminal"):
        contract.validate_event_order(
            previous, {"sequence": 4, "kind": "progress", "event_id": "four"}
        )
    assert set(previous) == {"sequence", "kind", "terminal", "event_id", "dedupe_key"}


def test_finish_partial_start_before_any_pane(local, monkeypatch):
    monkeypatch.setattr(
        controller,
        "_start_gated",
        lambda *args: (_ for _ in ()).throw(
            controller.ImplementerStartError("no pane created")
        ),
    )
    with pytest.raises(controller.ImplementerStartError):
        controller.start_implementer(**local.inputs)
    finish = controller.finish_implementer(local.path, force=True)
    assert finish["verdict"] == "cancelled"
    assert all(entry["status"] == "not-started" for entry in finish["cleanup"])
    assert not local.closed


def test_gated_process_can_be_owned_before_harness_detection(local):
    local.panes["implementer"]["agent"] = None
    panes = f.Panes(recruiter, {"herdr_session": "test", "workspace_id": "workspace"})
    assert panes.capture("implementer", "plan-implementer-run")["pid"] == 100


def test_interrupted_result_step_repairs_terminal_event(local, monkeypatch):
    controller.start_implementer(**local.inputs)
    original = f.write

    def interrupt(path, value):
        original(path, value)
        if path.name == "finishing.json" and value["step"] == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(f, "write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        controller.finish_implementer(local.path, force=True)
    assert not events(local.path)
    monkeypatch.setattr(f, "write", original)
    controller.finish_implementer(local.path)
    assert events(local.path)[0]["kind"] == "cancelled"


def test_failed_health_closes_same_owned_process_after_exec(local, monkeypatch):
    def health(*args):
        local.argv[100] = ["claude", "--model", "wrong-model"]
        raise controller.ImplementerStartError("failed mechanical health")

    monkeypatch.setattr(controller, "_health", health)
    with pytest.raises(controller.ImplementerStartError):
        controller.start_implementer(**local.inputs)
    assert local.closed == ["implementer"]
    assert f.read(local.path)["state"] == "failed"


def test_partial_manager_launch_remains_a_cleanup_failure(local, monkeypatch):
    original = f.launch_role

    def launch(panes, role, directory, brief, output, **kwargs):
        if directory.name == "manager":
            f.write(
                directory / "launch.json",
                {"state": "launching", "agent_name": "partial-manager"},
            )
            raise f.SupervisionError("launch returned no ownership")
        return original(panes, role, directory, brief, output, **kwargs)

    monkeypatch.setattr(f, "launch_role", launch)
    receipt = controller.start_implementer(**local.inputs)
    assert receipt["state"] == "ready-degraded"
    finish = controller.finish_implementer(local.path, force=True)
    entry = next(entry for entry in finish["cleanup"] if entry["target"] == "manager")
    assert entry["status"] == "cleanup-failed"


def test_manager_health_failure_after_exec_still_closes_owned_pane(local, monkeypatch):
    original = local.health

    def health(pane, **kwargs):
        if pane.startswith("flow1-manager"):
            local.argv[local.panes[pane]["pid"]] = ["codex", "--new-argv-after-exec"]
            raise recruiter.RecruiterError("manager did not become healthy")
        return original(pane, **kwargs)

    monkeypatch.setattr(recruiter, "_wait_for_agent_health", health)
    receipt = controller.start_implementer(**local.inputs)
    assert receipt["manager"]["cleanup"]["status"] == "closed"
    assert receipt["manager"]["pane_id"] in local.closed
