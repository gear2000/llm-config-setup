from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lifecycle = _load("lifecycle")
management = _load("llm_management")


def test_default_management_roles_are_bounded_and_low_effort() -> None:
    config = management.load_management_config({"harnesses": {"claude": "claude {model}"}})
    assert config.account_manager.timeout_ms > 0
    assert config.checker.timeout_ms > 0
    assert "upagent-account-manager" in config.account_manager.command
    assert "upagent-checker" in config.checker.command


def test_management_config_rejects_unknown_placeholders() -> None:
    roster = {
        "harnesses": {"claude": "claude {model}"},
        "management": {"account_manager": {"command": "claude {not_allowed}"}},
    }
    with pytest.raises(management.ManagementConfigError, match="not_allowed"):
        management.load_management_config(roster)


def test_account_manager_brief_contains_one_typed_output_contract(tmp_path: Path) -> None:
    order = {"order_id": "stage-1", "harness": "claude", "model": "some-model", "agent": "qa"}
    output = tmp_path / "manager-decision.json"
    text = management.account_manager_brief("req-abc", 1, order, output)
    assert str(output) in text
    assert '"decision": "approved|needs-requester|blocked"' in text
    assert "Do not create, close, or kill" in text


def test_account_manager_brief_exposes_authoritative_mechanical_errors(tmp_path: Path) -> None:
    output = tmp_path / "decision.json"
    text = management.account_manager_brief(
        "req-abc",
        1,
        {"order_id": "stage-1", "harness": "claude", "model": "bad", "agent": "missing"},
        output,
        {"valid": False, "errors": ["launch executable is not on PATH"]},
    )

    assert "launch executable is not on PATH" in text
    assert "any mechanical validation error" in text


def test_checker_brief_forbids_authoritative_actions(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"pane_exists": True}))
    output = tmp_path / "assessment.json"
    text = management.checker_brief("req-abc", 1, evidence, output)
    assert "advisory" in text.lower()
    assert "Do not create, close, interrupt, or kill" in text
    assert "The literal request id is `req-abc`" in text
    assert "never derive or substitute an identity" in text
