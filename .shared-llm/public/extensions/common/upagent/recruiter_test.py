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
recruiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recruiter)  # type: ignore[union-attr]
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
