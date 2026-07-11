"""Unit tests for the UpAgent order/result contracts. Pure stdlib — no Herdr needed.

Run: python3 -m pytest .shared-llm/extensions/common/upagent/contracts_test.py -q
"""

from __future__ import annotations

import json

import pytest

# Import the sibling module by path so the test runs from the repo root without packaging.
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "upagent_contracts", Path(__file__).with_name("contracts.py")
)
contracts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contracts)  # type: ignore[union-attr]
ContractError = contracts.ContractError


def _valid_order() -> dict:
    return {
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "phase_id": "phase-0",
        "stage_id": "stage-1-implementation",
        "harness": "claude",
        "model": "",
        "agent": "backend",
        "cwd": "/tmp/wt",
        "instructions_path": "/tmp/wt/instructions.md",
        "result_path": "/tmp/wt/result.json",
        "cockpit_pane": "1-1",
    }


def _valid_result() -> dict:
    return {
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "verdict": "passed",
        "full_log": "session://transcript/abc.jsonl",
    }


def test_valid_order_parses() -> None:
    order = contracts.parse_order(json.dumps(_valid_order()))
    assert order["stage_id"] == "stage-1-implementation"


def test_order_missing_key_fails() -> None:
    bad = _valid_order()
    del bad["cwd"]
    with pytest.raises(ContractError, match="cwd"):
        contracts.parse_order(json.dumps(bad))


def test_order_missing_cockpit_pane_fails() -> None:
    bad = _valid_order()
    del bad["cockpit_pane"]
    with pytest.raises(ContractError, match="cockpit_pane"):
        contracts.parse_order(json.dumps(bad))


def test_order_unknown_stage_fails() -> None:
    bad = _valid_order()
    bad["stage_id"] = "stage-9-nope"
    with pytest.raises(ContractError, match="unknown stage_id"):
        contracts.parse_order(json.dumps(bad))


def test_order_unknown_harness_fails() -> None:
    bad = _valid_order()
    bad["harness"] = "emacs"
    with pytest.raises(ContractError, match="unknown harness"):
        contracts.parse_order(json.dumps(bad))


def test_order_bad_env_fails() -> None:
    bad = _valid_order()
    bad["env"] = {"OK": 3}
    with pytest.raises(ContractError, match="env"):
        contracts.parse_order(json.dumps(bad))


def test_order_env_optional_absent_ok() -> None:
    contracts.parse_order(json.dumps(_valid_order()))  # no env key → fine


def test_not_json_fails() -> None:
    with pytest.raises(ContractError, match="not valid JSON"):
        contracts.parse_order("{not json")


def test_valid_result_parses() -> None:
    r = contracts.parse_result(json.dumps(_valid_result()))
    assert r["verdict"] == "passed"


def test_result_bad_verdict_fails() -> None:
    bad = _valid_result()
    bad["verdict"] = "great"
    with pytest.raises(ContractError, match="verdict"):
        contracts.parse_result(json.dumps(bad))


def test_result_order_id_mismatch_fails() -> None:
    with pytest.raises(ContractError, match="does not match"):
        contracts.parse_result(json.dumps(_valid_result()), expected_order_id="other-id")


def test_failed_verdict_requires_revisit() -> None:
    bad = _valid_result()
    bad["verdict"] = "failed"
    with pytest.raises(ContractError, match="must name at least one stage"):
        contracts.parse_result(json.dumps(bad))


def test_failed_verdict_with_revisit_ok() -> None:
    r = _valid_result()
    r["verdict"] = "failed"
    r["revisit"] = ["stage-1-implementation"]
    parsed = contracts.parse_result(json.dumps(r))
    assert parsed["revisit"] == ["stage-1-implementation"]


def test_revisit_unknown_stage_fails() -> None:
    bad = _valid_result()
    bad["verdict"] = "failed"
    bad["revisit"] = ["stage-1-implementation", "stage-42"]
    with pytest.raises(ContractError, match="not a recognized stage"):
        contracts.parse_result(json.dumps(bad))


def test_advisor_decision_valid() -> None:
    r = _valid_result()
    r["decision"] = "stop-ask-human"
    assert contracts.parse_result(json.dumps(r))["decision"] == "stop-ask-human"


def test_advisor_decision_unknown_fails() -> None:
    r = _valid_result()
    r["decision"] = "shrug"
    with pytest.raises(ContractError, match="decision"):
        contracts.parse_result(json.dumps(r))


def test_result_without_decision_ok() -> None:
    # decision is optional — a normal (non-advisor) result omits it.
    assert "decision" not in contracts.parse_result(json.dumps(_valid_result()))
