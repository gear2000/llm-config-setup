# pyright: reportMissingImports=false
"""Unit tests for the Recruiter's pure core (roster load + launch resolution).

The Herdr-driving parts need a live Herdr and are proven end-to-end separately; these tests
cover the risky pure logic — roster validation and template substitution — with no Herdr.

Run: python3 -m pytest .shared-llm/extensions/common/upagent/recruiter_test.py -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading
import time

import pytest

_spec = importlib.util.spec_from_file_location("upagent_recruiter", Path(__file__).with_name("recruiter.py"))
assert _spec and _spec.loader
recruiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recruiter)
RecruiterError = recruiter.RecruiterError


def _order(**over) -> dict:
    base = {
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "phase_id": "phase-0",
        "stage_id": "stage-1-implementation",
        "harness": "claude",
        "model": "some-model",
        "agent": "backend",
        "cwd": "/tmp/wt",
        "instructions_path": "/tmp/wt/instructions.md",
        "result_path": "/tmp/wt/result.json",
        "cockpit_pane": "1-1",
    }
    base.update(over)
    return base


def _roster() -> dict:
    return {
        "harnesses": {
            "claude": (
                "claude --model {model} --agent {agent} id:{order_id} "
                "read:{instructions_path} write:{result_path}"
            ),
            "codex": (
                "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "
                "--model {model} -c model_reasoning_effort={effort} "
                "read:{instructions_path} write:{result_path}"
            ),
            "pi": "pi read:{instructions_path} write:{result_path}",
        }
    }


def _result(order_id: str, verdict: str = "passed") -> dict:
    result: dict[str, object] = {
        "order_id": order_id,
        "verdict": verdict,
        "full_log": "/tmp/worker.log",
    }
    if verdict == "failed":
        result["revisit"] = ["stage-1-implementation"]
    return result


def _cleanup(worker_pane: str | None = "worker-pane") -> dict:
    return {
        "status": "closed" if worker_pane else "not-created",
        "worker_pane": worker_pane,
        "verified_absent": True,
    }


def test_resolve_substitutes_fields() -> None:
    cmd = recruiter.resolve_launch_command(_order(), _roster())
    assert "--model some-model" in cmd
    assert "--agent backend" in cmd
    assert "read:/tmp/wt/instructions.md" in cmd
    assert "write:/tmp/wt/result.json" in cmd
    assert "id:phase-0.stage-1-implementation.pass-1.try-1" in cmd


def test_resolve_unknown_harness_fails() -> None:
    with pytest.raises(RecruiterError, match="no launch template for harness"):
        recruiter.resolve_launch_command(_order(harness="cursor"), _roster())


def test_resolve_codex_direct_launch_substitutes_model_effort_and_paths() -> None:
    cmd = recruiter.resolve_launch_command(
        _order(harness="codex", model="gpt-5.6-sol", effort="high"),
        _roster(),
    )

    assert cmd.startswith("codex exec --dangerously-bypass-approvals-and-sandbox")
    assert "--skip-git-repo-check" in cmd
    assert "--model gpt-5.6-sol" in cmd
    assert "model_reasoning_effort=high" in cmd
    assert "--agent" not in cmd
    assert "read:/tmp/wt/instructions.md" in cmd
    assert "write:/tmp/wt/result.json" in cmd


def test_resolve_effort_substitutes() -> None:
    roster = {"harnesses": {"claude": "claude --model {model} --effort {effort}"}}
    cmd = recruiter.resolve_launch_command(_order(effort="high"), roster)
    assert "--effort high" in cmd


def test_resolve_effort_absent_substitutes_empty() -> None:
    # An order without `effort` formats {effort} as "" (order.get default) — templates that
    # use the placeholder rely on the leader always resolving an effort (default `medium`).
    roster = {"harnesses": {"claude": "claude effort:[{effort}]"}}
    cmd = recruiter.resolve_launch_command(_order(), roster)
    assert "effort:[]" in cmd


def test_resolve_unknown_placeholder_fails() -> None:
    roster = {"harnesses": {"claude": "claude {model} {bogus_field}"}}
    with pytest.raises(RecruiterError, match="unknown placeholder"):
        recruiter.resolve_launch_command(_order(), roster)


def test_load_roster_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(RecruiterError, match="roster not found"):
        recruiter.load_roster(tmp_path / "nope.yaml")


def test_load_roster_empty_harnesses_fails(tmp_path: Path) -> None:
    p = tmp_path / "upagent.yaml"
    p.write_text("harnesses: {}\n")
    with pytest.raises(RecruiterError, match="non-empty"):
        recruiter.load_roster(p)


def test_load_roster_non_string_template_fails(tmp_path: Path) -> None:
    p = tmp_path / "upagent.yaml"
    p.write_text("harnesses:\n  claude: 42\n")
    with pytest.raises(RecruiterError, match="non-empty template string"):
        recruiter.load_roster(p)


def test_load_roster_accepts_codex_template(tmp_path: Path) -> None:
    p = tmp_path / "upagent.yaml"
    p.write_text("harnesses:\n  codex: 'codex exec --model {model}'\n")

    roster = recruiter.load_roster(p)

    assert roster["harnesses"]["codex"] == "codex exec --model {model}"


def test_load_roster_invalid_yaml_raises_recruiter_error(tmp_path: Path) -> None:
    # Invalid YAML must surface as RecruiterError (caught by the recruit fallback), not a raw
    # yaml.YAMLError that would escape past main().
    p = tmp_path / "upagent.yaml"
    p.write_text("harnesses: [unclosed\n")
    with pytest.raises(RecruiterError, match="invalid YAML"):
        recruiter.load_roster(p)


def test_write_blocked_result_fails_loud_on_unwritable_path(tmp_path: Path) -> None:
    # Without a valid result, callers must not publish terminal state or DONE.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    order = {
        "order_id": "oid",
        "result_path": str(blocker / "nested" / "result.json"),
        "stage_id": "stage-1-implementation",
    }
    with pytest.raises(OSError):
        recruiter._write_blocked_result(order, "boom")


def test_write_blocked_result_preserves_valid_existing_result(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    existing = _result("oid", verdict="failed")
    result_path.write_text(json.dumps(existing))
    order = {
        "order_id": "oid",
        "result_path": str(result_path),
        "stage_id": "stage-1-implementation",
    }

    parsed = recruiter._write_blocked_result(order, "wait timed out")

    assert parsed == existing
    assert json.loads(result_path.read_text()) == existing


def test_write_blocked_result_overwrites_stale_existing_result(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result("other-order")))
    order = {
        "order_id": "oid",
        "result_path": str(result_path),
        "stage_id": "stage-1-implementation",
    }

    parsed = recruiter._write_blocked_result(order, "wait timed out")

    written = json.loads(result_path.read_text())
    assert parsed == written
    assert written["order_id"] == "oid"
    assert written["verdict"] == "blocked"
    assert written["reason"] == "recruiter: wait timed out"


def test_load_valid_roster(tmp_path: Path) -> None:
    p = tmp_path / "upagent.yaml"
    p.write_text('harnesses:\n  claude: "claude {model}"\n')
    roster = recruiter.load_roster(p)
    assert "claude" in roster["harnesses"]


def test_default_roster_env_override(monkeypatch) -> None:
    monkeypatch.setenv("UPAGENT_CONFIG", "/custom/roster.yaml")
    assert recruiter.default_roster_path() == "/custom/roster.yaml"


def test_job_ledger_copy_on_write_and_idempotent_submit(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    order = _order()
    key, created = ledger.submit(order)
    assert created
    request = ledger.request_dir(key)
    assert json.loads((request / "request.json").read_text()) == order
    assert json.loads((request / "state/latest.json").read_text())["state"] == "queued"
    assert len(list((request / "events").glob("*.json"))) == 1
    assert not list(request.rglob("*.tmp"))
    assert ledger.submit(order) == (key, False)


def test_job_ledger_rejects_conflicting_duplicate_id(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    ledger.submit(_order())
    with pytest.raises(RecruiterError, match="collision"):
        ledger.submit(_order(agent="different"))


def test_job_ledger_concurrent_identical_submit_never_reads_partial_request(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    original_write_json = ledger._write_json
    first_request_written = threading.Event()
    release_first_submitter = threading.Event()
    outcomes: list[tuple[str, bool]] = []
    errors: list[BaseException] = []

    def delayed_write_json(path: Path, value: dict) -> None:
        original_write_json(path, value)
        if path.name == "request.json" and not first_request_written.is_set():
            first_request_written.set()
            assert release_first_submitter.wait(timeout=2)

    ledger._write_json = delayed_write_json

    def submit() -> None:
        try:
            outcomes.append(ledger.submit(_order()))
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=submit)
    first.start()
    assert first_request_written.wait(timeout=2)
    second = threading.Thread(target=submit)
    second.start()
    second.join(timeout=2)
    release_first_submitter.set()
    first.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(outcomes) == 2
    assert sum(created for _, created in outcomes) == 1


def test_job_ledger_finalization_publishes_valid_result_and_terminal_state(tmp_path: Path) -> None:
    root = tmp_path / "upagent-hub"
    ledger = recruiter.JobLedger(root)
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token and ledger.claim(key, order["order_id"], 1_000) is None
    lease = json.loads((root / "active/requests" / key / "lease.json").read_text())
    index = root / "active/by-expiry" / str(lease["expires_at"]) / f"{key}-{token}.json"
    assert index.is_file()
    cleanup = _cleanup()
    assert ledger.finalize(key, token, order, _result(order["order_id"]), cleanup=cleanup, exit_code=0)
    assert not (root / "active/requests" / key).exists()
    assert index.is_file()
    assert json.loads(Path(order["result_path"]).read_text()) == _result(order["order_id"])
    latest = json.loads((ledger.request_dir(key) / "state/latest.json").read_text())
    assert latest["state"] == "finished" and latest["verdict"] == "passed"
    receipt = json.loads((ledger.request_dir(key) / "receipt.json").read_text())
    assert receipt == {
        "cleanup": cleanup,
        "order_id": order["order_id"],
        "result_path": order["result_path"],
        "state": "finished",
        "verdict": "passed",
    }


def test_worker_instructions_end_with_one_literal_private_result_contract(tmp_path: Path) -> None:
    original = tmp_path / "instructions.md"
    original.write_text("Do the stage. An older brief mentioned /public/result.json.\n")
    private_result = tmp_path / "hub/results/token.json"
    generated = tmp_path / "hub/worker-instructions.md"

    recruiter._write_worker_instructions(_order(instructions_path=str(original)), private_result, generated)

    text = generated.read_text()
    assert text.endswith(
        "Write exactly one result JSON file to: " + str(private_result) + "\n"
        'Its `order_id` must be exactly: "phase-0.stage-1-implementation.pass-1.try-1"\n'
        "Do not write a result to any other path.\n"
    )


def test_run_order_creates_private_result_parent_before_worker_launch(tmp_path: Path, monkeypatch) -> None:
    public_result = tmp_path / "public-result.json"
    private_result = tmp_path / "hub" / "missing-results-dir" / "token.json"
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(result_path=str(public_result), instructions_path=str(instructions))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {"result": {"pane": {"pane_id": "worker-pane"}}}
        if args[:2] == ("pane", "split")
        else {"result": {"panes": []}},
    )

    def fake_herdr(*args: str) -> None:
        if args[:2] == ("pane", "run"):
            assert private_result.parent.is_dir()
            private_result.write_text(json.dumps(_result(order["order_id"])))

    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", lambda *args: True)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    code, result, cleanup = recruiter._run_order(str(order_path), str(roster_path), private_result)

    assert code == 0
    assert result == _result(order["order_id"])
    assert cleanup["verified_absent"] is True


def test_completion_monitor_wakes_on_stable_malformed_result(tmp_path: Path, monkeypatch) -> None:
    malformed = tmp_path / "private-result.json"
    malformed.write_text('{"order_id": null}')
    monkeypatch.setattr(recruiter, "INVALID_RESULT_SETTLE_SECONDS", 0.05)

    stop, ready, thread = recruiter._start_completion_monitor(_order(), malformed, 1_000)

    assert ready.wait(timeout=0.5)
    stop.set()
    thread.join(timeout=0.5)
    assert not thread.is_alive()


def test_recruit_completed_order_emits_done_without_spawning(tmp_path: Path, monkeypatch, capsys) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    assert ledger.finalize(key, token, order, _result(order["order_id"]), cleanup=_cleanup(None))
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("completed order must not spawn a job runner"),
    )

    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0
    assert capsys.readouterr().out == f"ORDER {order['order_id']} DONE\n"


def test_recruit_submits_and_spawns_without_waiting(tmp_path: Path, monkeypatch) -> None:
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(_order()))
    spawned = []
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter.subprocess, "Popen", lambda command, **kwargs: spawned.append((command, kwargs)))
    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0
    assert len(spawned) == 1
    assert spawned[0][0][-2] == "run-job" and spawned[0][1]["start_new_session"] is True
    key = recruiter.JobLedger().key(_order()["order_id"])
    assert (tmp_path / "hub/requests" / key / "state/latest.json").is_file()


def test_dispatch_blocks_on_job_process_and_returns_durable_receipt(tmp_path: Path, monkeypatch, capsys) -> None:
    order = _order(
        result_path=str(tmp_path / "result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    waits = []

    class Process:
        def wait(self, timeout: float) -> int:
            waits.append(timeout)
            ledger = recruiter.JobLedger()
            key = ledger.key(order["order_id"])
            token = ledger.claim(key, order["order_id"], 1_000)
            assert token
            assert ledger.finalize(key, token, order, _result(order["order_id"]), cleanup=_cleanup())
            return 0

    monkeypatch.setattr(recruiter, "_spawn_job", lambda key, roster: Process())

    assert recruiter.cmd_dispatch(str(order_path), "roster.yaml") == 0

    output = capsys.readouterr().out
    assert output.startswith("ORDER_RECEIPT ")
    assert json.loads(output.removeprefix("ORDER_RECEIPT "))["order_id"] == order["order_id"]
    assert waits


def test_expired_lease_with_recorded_worker_requires_runtime_reconciliation(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000, owner={"runner_pid": 123})
    assert token
    assert ledger.record_worker(key, token, "owned-worker", "cockpit")
    lease_path = ledger.active / "requests" / key / "lease.json"
    lease = json.loads(lease_path.read_text())
    lease["expires_at"] = int(time.time()) - 1
    ledger._write_json(lease_path, lease)

    assert ledger.claim(key, order["order_id"], 1_000) is None
    assert json.loads(lease_path.read_text())["worker_pane"] == "owned-worker"


def test_reconciler_closes_only_recorded_worker_and_publishes_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000, owner={"runner_pid": 999})
    assert token
    assert ledger.record_worker(key, token, "owned-worker", "cockpit")
    staging = ledger.result_staging_path(key, token)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(json.dumps(_result(order["order_id"])))
    closed = []
    monkeypatch.setattr(recruiter, "_runner_alive", lambda pid, candidate_key: False)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane: closed.append(pane) or {"status": "closed", "worker_pane": pane, "verified_absent": True},
    )

    assert recruiter.cmd_reconcile(force=True) == 0

    assert closed == ["owned-worker"]
    assert ledger.completed_result(key, order) == _result(order["order_id"])
    assert ledger.completed_receipt(key, order)["cleanup"]["worker_pane"] == "owned-worker"


def test_cleanup_failure_keeps_owned_lease_until_reconciler_verifies_absence(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    assert ledger.record_worker(key, token, "owned-worker", "cockpit")
    cleanup = {
        "status": "cleanup-failed",
        "worker_pane": "owned-worker",
        "verified_absent": False,
        "reason": "socket unavailable",
    }

    assert ledger.finalize(
        key,
        token,
        order,
        _result(order["order_id"], verdict="blocked"),
        cleanup=cleanup,
    )

    assert (ledger.active / "requests" / key / "lease.json").is_file()
    assert ledger.completed_receipt(key, order)["state"] == "cleanup-failed"


def test_worker_ownership_is_recorded_before_launch_command(tmp_path: Path, monkeypatch) -> None:
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(instructions_path=str(instructions), result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')
    private_result = tmp_path / "hub/results/token.json"
    recorded = threading.Event()
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {"result": {"pane": {"pane_id": "worker-pane", "workspace_id": "cockpit"}}}
        if args[:2] == ("pane", "split")
        else {"result": {"panes": []}},
    )

    def on_worker(worker_pane: str, workspace_id: str | None) -> threading.Event:
        assert worker_pane == "worker-pane" and workspace_id == "cockpit"
        recorded.set()
        return threading.Event()

    def fake_herdr(*args: str) -> None:
        if args[:2] == ("pane", "run"):
            assert recorded.is_set()
            private_result.write_text(json.dumps(_result(order["order_id"])))

    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", lambda *args: True)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path), str(roster_path), private_result, on_worker
    )

    assert code == 0 and result["verdict"] == "passed" and cleanup["verified_absent"] is True


def test_completion_monitor_returns_runner_promptly_after_promoting_stuck_status_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    result_path = tmp_path / "result.json"
    order = _order(
        result_path=str(result_path), instructions_path=str(tmp_path / "instructions.md"), timeout_ms=1_000
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n')
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_launched = threading.Event()
    status_wait_started = threading.Event()
    staging_paths: list[Path] = []
    closed_panes: list[str] = []
    worker_closed = threading.Event()
    status_wait_timeouts: list[str] = []
    outcomes: list[int] = []

    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {"result": {"pane": {"pane_id": "worker-pane"}}},
    )

    def fake_herdr(*args: str) -> None:
        if args[:2] == ("pane", "run"):
            staging_paths.append(Path(args[3].split("write:", maxsplit=1)[1]))
            worker_launched.set()
            return
        if args[:2] == ("pane", "close"):
            closed_panes.append(args[2])
            worker_closed.set()
            return
        raise AssertionError(f"unexpected herdr args: {args}")

    class NeverDoneProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

    def fake_popen(command: list[str], **kwargs: object) -> NeverDoneProcess:
        assert command[1:3] == ["wait", "agent-status"]
        status_wait_timeouts.append(command[-1])
        status_wait_started.set()
        return NeverDoneProcess()

    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(recruiter.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    runner = threading.Thread(target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path))))
    runner.start()
    assert worker_launched.wait(timeout=2)
    assert status_wait_started.wait(timeout=2)
    staging_paths[0].parent.mkdir(parents=True, exist_ok=True)
    staging_paths[0].write_text(json.dumps(_result(order["order_id"])))
    deadline = time.monotonic() + 2
    while not result_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(result_path.read_text()) == _result(order["order_id"])
    assert worker_closed.wait(timeout=2)
    assert closed_panes == ["worker-pane"]

    promoted_at = time.monotonic()
    runner.join(timeout=1)
    assert not runner.is_alive()
    assert time.monotonic() - promoted_at < 0.5
    assert outcomes == [0]
    assert status_wait_timeouts and set(status_wait_timeouts) == {"1000"}
    assert capsys.readouterr().out.count(f"ORDER {order['order_id']} DONE\n") == 1
    assert closed_panes == ["worker-pane"]


def test_codex_completion_monitor_promotes_private_result_when_agent_status_never_finishes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    result_path = tmp_path / "result.json"
    order = _order(
        harness="codex",
        model="gpt-5.6-sol",
        effort="high",
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
        timeout_ms=1_000,
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        "harnesses:\n"
        "  codex: >-\n"
        "    codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check\n"
        "    --model {model} -c model_reasoning_effort={effort}\n"
        "    read:{instructions_path} write:{result_path}\n"
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_launched = threading.Event()
    status_wait_started = threading.Event()
    staging_paths: list[Path] = []
    closed_panes: list[str] = []
    outcomes: list[int] = []

    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {"result": {"pane": {"pane_id": "codex-worker-pane"}}},
    )

    def fake_herdr(*args: str) -> None:
        if args[:2] == ("pane", "run"):
            launch = args[3]
            assert launch.startswith("codex exec --dangerously-bypass-approvals-and-sandbox")
            assert "--model gpt-5.6-sol" in launch
            assert "model_reasoning_effort=high" in launch
            staging_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
            worker_launched.set()
            return
        if args[:2] == ("pane", "close"):
            closed_panes.append(args[2])
            return
        raise AssertionError(f"unexpected herdr args: {args}")

    class NeverDoneProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

    def never_done(command: list[str], **kwargs: object) -> NeverDoneProcess:
        assert command[1:3] == ["wait", "agent-status"]
        status_wait_started.set()
        return NeverDoneProcess()

    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(recruiter.subprocess, "Popen", never_done)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    runner = threading.Thread(target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path))))
    runner.start()
    assert worker_launched.wait(timeout=2)
    assert status_wait_started.wait(timeout=2)
    assert staging_paths[0] != result_path
    staging_paths[0].parent.mkdir(parents=True, exist_ok=True)
    staging_paths[0].write_text(json.dumps(_result(order["order_id"])))

    deadline = time.monotonic() + 2
    while not result_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    runner.join(timeout=1)

    assert not runner.is_alive()
    assert outcomes == [0]
    assert json.loads(result_path.read_text()) == _result(order["order_id"])
    assert capsys.readouterr().out.count(f"ORDER {order['order_id']} DONE\n") == 1
    assert closed_panes == ["codex-worker-pane"]


def test_run_job_keeps_worker_result_when_status_wait_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    result_path = tmp_path / "result.json"
    order = _order(result_path=str(result_path), instructions_path=str(tmp_path / "instructions.md"))
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n')
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_result_paths: list[Path] = []

    def fake_herdr_json(*args: str) -> dict:
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "worker-pane"}}}
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": []}}
        raise AssertionError(f"unexpected herdr args: {args}")

    def fake_herdr(*args: str) -> None:
        if args[:2] == ("pane", "run"):
            worker_result_paths.append(Path(args[3].split("write:", maxsplit=1)[1]))
            return
        if args[:2] == ("pane", "close"):
            return
        raise AssertionError(f"unexpected herdr args: {args}")

    def fail_wait(*args: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        worker_result_paths[0].write_text(json.dumps(_result(order["order_id"], verdict="passed")))
        raise recruiter.RecruiterError("wait transport failed")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)
    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", fail_wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    output = capsys.readouterr()
    assert f"ORDER {order['order_id']} DONE" in output.out
    assert "kept existing worker result" in output.err
    assert json.loads(result_path.read_text()) == _result(order["order_id"], verdict="passed")


def test_expired_owner_cannot_finalize_before_replacement_claims(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    expired_token = ledger.claim(key, order["order_id"], 1_000)
    assert expired_token is not None
    expired_lease = {
        "order_id": order["order_id"],
        "token": expired_token,
        "expires_at": int(time.time()) - 1,
    }
    ledger._write_json(ledger.active / "requests" / key / "lease.json", expired_lease)

    assert not ledger.finalize(key, expired_token, order, _result(order["order_id"]), cleanup=_cleanup())
    assert not Path(order["result_path"]).exists()
    assert json.loads((ledger.request_dir(key) / "state/latest.json").read_text())["state"] == "running"
    assert (ledger.active / "requests" / key).is_dir()

    replacement_token = ledger.claim(key, order["order_id"], 1_000)
    assert replacement_token is not None and replacement_token != expired_token


def test_expired_lease_is_reclaimed_and_stale_index_cannot_remove_new_lease(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    order = _order()
    key, _ = ledger.submit(order)
    old_token = ledger.claim(key, order["order_id"], 1_000)
    assert old_token is not None
    old_lease_path = ledger.active / "requests" / key / "lease.json"
    expired_at = int(time.time()) - 1
    old_lease = {"order_id": order["order_id"], "token": old_token, "expires_at": expired_at}
    ledger._write_json(old_lease_path, old_lease)
    ledger._write_json(ledger.active / "by-expiry" / str(expired_at) / f"{key}-{old_token}.json", old_lease)

    new_token = ledger.claim(key, order["order_id"], 1_000)
    assert new_token is not None and new_token != old_token
    assert ledger.reap_expired(now=expired_at) == 0
    active_lease = json.loads((ledger.active / "requests" / key / "lease.json").read_text())
    assert active_lease["token"] == new_token
    assert not ledger.finalize(key, old_token, order, _result(order["order_id"]), cleanup=_cleanup())
    assert (ledger.active / "requests" / key).is_dir()


def test_stage_timeout_defaults_and_explicit_override() -> None:
    assert recruiter._default_timeout_ms("stage-1-implementation") == 10_800_000
    assert recruiter._default_timeout_ms("stage-2-adversarial-audit") == 10_800_000
    assert recruiter._default_timeout_ms("stage-3-integration-acceptance-seams") == recruiter.DEFAULT_TIMEOUT_MS
    assert _order(timeout_ms=123)["timeout_ms"] == 123


def test_run_order_rejects_cosmetic_result_before_done(tmp_path: Path, monkeypatch, capsys) -> None:
    result_path = tmp_path / "result.json"
    staging_path = tmp_path / "staging.json"
    order = _order(result_path=str(result_path), instructions_path=str(tmp_path / "instructions.md"))
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    order_path = tmp_path / "order.json"
    roster_path = tmp_path / "upagent.yaml"
    order_path.write_text(json.dumps(order))
    roster_path.write_text('harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n')

    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {"result": {"pane": {"pane_id": "worker-pane"}}},
    )

    def fake_wait(*args: object) -> bool:
        staging_path.write_text(
            json.dumps(
                {
                    "order_id": order["order_id"],
                    "verdict": "PASSED",
                    "full_log": "/tmp/worker.log",
                }
            )
        )
        return True

    monkeypatch.setattr(recruiter, "_wait_for_agent_status", fake_wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    code, result, cleanup = recruiter._run_order(str(order_path), str(roster_path), staging_path)
    assert code == 1
    assert result["verdict"] == "blocked"
    assert json.loads(staging_path.read_text())["verdict"] == "blocked"
    assert not result_path.exists()
    assert "ORDER" not in capsys.readouterr().out
    assert cleanup["verified_absent"] is True


def test_duplicate_popen_failure_cannot_finalize_live_owner(tmp_path: Path, monkeypatch, capsys) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    assert ledger.claim(key, order["order_id"], 1_000) is not None

    def fail_popen(*args: object, **kwargs: object) -> None:
        raise OSError("runner process unavailable")

    monkeypatch.setattr(recruiter.subprocess, "Popen", fail_popen)
    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 1

    assert not Path(order["result_path"]).exists()
    latest = json.loads((ledger.request_dir(key) / "state/latest.json").read_text())
    assert latest["state"] == "running"
    assert "DONE" not in capsys.readouterr().out


def test_recovered_lease_fences_stale_runner_result_and_terminal_state(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    old_token = ledger.claim(key, order["order_id"], 1_000)
    assert old_token is not None
    expired_lease = {"order_id": order["order_id"], "token": old_token, "expires_at": int(time.time()) - 1}
    ledger._write_json(ledger.active / "requests" / key / "lease.json", expired_lease)
    new_token = ledger.claim(key, order["order_id"], 1_000)
    assert new_token is not None and new_token != old_token

    assert not ledger.finalize(
        key, old_token, order, _result(order["order_id"], verdict="passed"), cleanup=_cleanup()
    )
    assert not Path(order["result_path"]).exists()
    assert ledger.finalize(
        key, new_token, order, _result(order["order_id"], verdict="blocked"), cleanup=_cleanup()
    )
    assert json.loads(Path(order["result_path"]).read_text())["verdict"] == "blocked"


def test_blocked_result_write_failure_leaves_request_nonterminal_and_silent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))

    def fail_popen(*args: object, **kwargs: object) -> None:
        raise OSError("runner process unavailable")

    original_write_json = recruiter.JobLedger._write_json

    def fail_staging_write(path: Path, value: dict) -> None:
        if path.parent.name == "results":
            raise OSError("disk full")
        original_write_json(path, value)

    monkeypatch.setattr(recruiter.subprocess, "Popen", fail_popen)
    monkeypatch.setattr(recruiter.JobLedger, "_write_json", staticmethod(fail_staging_write))
    with pytest.raises(OSError, match="disk full"):
        recruiter.cmd_recruit(str(order_path), "roster.yaml")

    ledger = recruiter.JobLedger()
    key = ledger.key(order["order_id"])
    assert json.loads((ledger.request_dir(key) / "state/latest.json").read_text())["state"] == "running"
    assert not Path(order["result_path"]).exists()
    assert "DONE" not in capsys.readouterr().out
