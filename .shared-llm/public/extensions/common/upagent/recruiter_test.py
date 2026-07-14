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
            "claude": "claude --model {model} --agent {agent} read:{instructions_path} write:{result_path}",
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


def test_resolve_substitutes_fields() -> None:
    cmd = recruiter.resolve_launch_command(_order(), _roster())
    assert "--model some-model" in cmd
    assert "--agent backend" in cmd
    assert "read:/tmp/wt/instructions.md" in cmd
    assert "write:/tmp/wt/result.json" in cmd


def test_resolve_unknown_harness_fails() -> None:
    with pytest.raises(RecruiterError, match="no launch template for harness"):
        recruiter.resolve_launch_command(_order(harness="cursor"), _roster())


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


def test_load_roster_rejects_codex_template(tmp_path: Path) -> None:
    p = tmp_path / "upagent.yaml"
    p.write_text("harnesses:\n  codex: 'codex exec {model}'\n")
    with pytest.raises(RecruiterError, match="unsupported"):
        recruiter.load_roster(p)


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
    assert ledger.finalize(key, token, order, _result(order["order_id"]), exit_code=0)
    assert not (root / "active/requests" / key).exists()
    assert index.is_file()
    assert json.loads(Path(order["result_path"]).read_text()) == _result(order["order_id"])
    latest = json.loads((ledger.request_dir(key) / "state/latest.json").read_text())
    assert latest["state"] == "finished" and latest["verdict"] == "passed"


def test_recruit_completed_order_emits_done_without_spawning(tmp_path: Path, monkeypatch, capsys) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    Path(order["result_path"]).write_text(json.dumps(_result(order["order_id"])))
    ledger._snapshot(key, "finished", verdict="passed")
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


def test_completion_monitor_publishes_staging_result_while_status_wait_is_stuck(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    result_path = tmp_path / "result.json"
    order = _order(result_path=str(result_path), instructions_path=str(tmp_path / "instructions.md"))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n')
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_launched = threading.Event()
    release_status_wait = threading.Event()
    staging_paths: list[Path] = []
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
        if args[:2] == ("wait", "agent-status"):
            assert release_status_wait.wait(timeout=2)
            return
        if args[:2] == ("pane", "close"):
            return
        raise AssertionError(f"unexpected herdr args: {args}")

    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    runner = threading.Thread(target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path))))
    runner.start()
    assert worker_launched.wait(timeout=2)
    staging_paths[0].parent.mkdir(parents=True, exist_ok=True)
    staging_paths[0].write_text(json.dumps(_result(order["order_id"])))
    deadline = time.monotonic() + 2
    while not result_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(result_path.read_text()) == _result(order["order_id"])

    release_status_wait.set()
    runner.join(timeout=2)
    assert not runner.is_alive()
    assert outcomes == [1]
    assert capsys.readouterr().out.count(f"ORDER {order['order_id']} DONE\n") == 1


def test_run_job_keeps_worker_result_when_status_wait_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    result_path = tmp_path / "result.json"
    order = _order(result_path=str(result_path), instructions_path=str(tmp_path / "instructions.md"))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n')
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_result_paths: list[Path] = []

    def fake_herdr_json(*args: str) -> dict:
        assert args[:2] == ("pane", "split")
        return {"result": {"pane": {"pane_id": "worker-pane"}}}

    def fake_herdr(*args: str) -> None:
        if args[:2] == ("pane", "run"):
            worker_result_paths.append(Path(args[3].split("write:", maxsplit=1)[1]))
            return
        if args[:2] == ("wait", "agent-status"):
            worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
            worker_result_paths[0].write_text(json.dumps(_result(order["order_id"], verdict="passed")))
            raise recruiter.RecruiterError("timed out waiting for done status")
        if args[:2] == ("pane", "close"):
            return
        raise AssertionError(f"unexpected herdr args: {args}")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)
    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
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

    assert not ledger.finalize(key, expired_token, order, _result(order["order_id"]))
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
    assert not ledger.finalize(key, old_token, order, _result(order["order_id"]))
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
    order_path = tmp_path / "order.json"
    roster_path = tmp_path / "upagent.yaml"
    order_path.write_text(json.dumps(order))
    roster_path.write_text('harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n')

    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {"result": {"pane": {"pane_id": "worker-pane"}}},
    )

    def fake_herdr(*args: str) -> None:
        if args[:2] == ("wait", "agent-status"):
            staging_path.write_text(
                json.dumps(
                    {
                        "order_id": order["order_id"],
                        "verdict": "PASSED",
                        "full_log": "/tmp/worker.log",
                    }
                )
            )

    monkeypatch.setattr(recruiter, "_herdr", fake_herdr)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    code, result = recruiter._run_order(str(order_path), str(roster_path), staging_path)
    assert code == 1
    assert result["verdict"] == "blocked"
    assert json.loads(staging_path.read_text())["verdict"] == "blocked"
    assert not result_path.exists()
    assert "ORDER" not in capsys.readouterr().out


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

    assert not ledger.finalize(key, old_token, order, _result(order["order_id"], verdict="passed"))
    assert not Path(order["result_path"]).exists()
    assert ledger.finalize(key, new_token, order, _result(order["order_id"], verdict="blocked"))
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
