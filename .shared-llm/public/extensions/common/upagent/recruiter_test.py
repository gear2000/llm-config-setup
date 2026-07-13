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

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_recruiter", Path(__file__).with_name("recruiter.py")
)
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
            "codex": "codex exec read:{instructions_path} write:{result_path}",
        }
    }


def test_resolve_substitutes_fields() -> None:
    cmd = recruiter.resolve_launch_command(_order(), _roster())
    assert "--model some-model" in cmd
    assert "--agent backend" in cmd
    assert "read:/tmp/wt/instructions.md" in cmd
    assert "write:/tmp/wt/result.json" in cmd


def test_resolve_unknown_harness_fails() -> None:
    with pytest.raises(RecruiterError, match="no launch template for harness"):
        recruiter.resolve_launch_command(_order(harness="pi"), _roster())


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


def test_load_roster_invalid_yaml_raises_recruiter_error(tmp_path: Path) -> None:
    # Invalid YAML must surface as RecruiterError (caught by the recruit fallback), not a raw
    # yaml.YAMLError that would escape past main().
    p = tmp_path / "upagent.yaml"
    p.write_text("harnesses: [unclosed\n")
    with pytest.raises(RecruiterError, match="invalid YAML"):
        recruiter.load_roster(p)


def test_write_blocked_result_is_best_effort_on_unwritable_path(tmp_path: Path) -> None:
    # result_path under a *file* (not a dir) can't be written; _write_blocked_result must not
    # raise (it runs from the except path and must never skip the DONE emission).
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    order = {"order_id": "oid", "result_path": str(blocker / "nested" / "result.json"),
             "stage_id": "stage-1-implementation"}
    recruiter._write_blocked_result(order, "boom")  # must return without raising


def test_load_valid_roster(tmp_path: Path) -> None:
    p = tmp_path / "upagent.yaml"
    p.write_text('harnesses:\n  claude: "claude {model}"\n')
    roster = recruiter.load_roster(p)
    assert "claude" in roster["harnesses"]


def test_recover_order_fields_valid(tmp_path: Path) -> None:
    p = tmp_path / "order.json"
    p.write_text(json.dumps({"order_id": "oid-1", "result_path": "/tmp/r.json", "junk": True}))
    assert recruiter._recover_order_fields(str(p)) == ("oid-1", "/tmp/r.json")


def test_recover_order_fields_missing_keys(tmp_path: Path) -> None:
    p = tmp_path / "order.json"
    p.write_text(json.dumps({"order_id": "oid-1"}))  # no result_path
    assert recruiter._recover_order_fields(str(p)) is None


def test_recover_order_fields_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "order.json"
    p.write_text("{not json")
    assert recruiter._recover_order_fields(str(p)) is None


def test_recover_order_fields_absent_file(tmp_path: Path) -> None:
    assert recruiter._recover_order_fields(str(tmp_path / "nope.json")) is None


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


def test_job_ledger_exclusive_claim_terminal_cleanup_and_index_retention(tmp_path: Path) -> None:
    root = tmp_path / "upagent-hub"
    ledger = recruiter.JobLedger(root)
    key, _ = ledger.submit(_order())
    token = ledger.claim(key, _order()["order_id"], 1_000)
    assert token and ledger.claim(key, _order()["order_id"], 1_000) is None
    lease = json.loads((root / "active/requests" / key / "lease.json").read_text())
    index = root / "active/by-expiry" / str(lease["expires_at"]) / f"{key}-{token}.json"
    assert index.is_file()
    ledger.finish(key, token, "passed", exit_code=0)
    assert not (root / "active/requests" / key).exists()
    assert index.is_file()
    latest = json.loads((ledger.request_dir(key) / "state/latest.json").read_text())
    assert latest["state"] == "finished" and latest["verdict"] == "passed"


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


def test_stage_timeout_defaults_and_explicit_override() -> None:
    assert recruiter._default_timeout_ms("stage-1-implementation") == 10_800_000
    assert recruiter._default_timeout_ms("stage-2-adversarial-audit") == 10_800_000
    assert recruiter._default_timeout_ms("stage-3-integration-acceptance-seams") == recruiter.DEFAULT_TIMEOUT_MS
    assert (_order(timeout_ms=123)["timeout_ms"] or recruiter._default_timeout_ms("stage-1-implementation")) == 123


def test_load_normalized_result_logs_cosmetic_repairs(tmp_path: Path, capsys) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"order_id": "oid", "verdict": "VERIFICATION_PASSED", "full_log": ["log"]}))
    result = recruiter._load_normalized_result(result_path, "oid")
    assert result["verdict"] == "passed" and "auto-corrected" in capsys.readouterr().err
