from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
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


def test_rescue_flag_defaults_on_and_rejects_non_boolean() -> None:
    assert management.load_management_config({}).rescue_on_startup_failure is True
    off = management.load_management_config(
        {"management": {"rescue_on_startup_failure": False}}
    )
    assert off.rescue_on_startup_failure is False
    with pytest.raises(management.ManagementConfigError):
        management.load_management_config(
            {"management": {"rescue_on_startup_failure": "yes"}}
        )


def test_default_management_roles_are_bounded_and_low_effort() -> None:
    config = management.load_management_config({"harnesses": {"claude": "claude {model}"}})
    assert config.account_manager.timeout_ms > 0
    assert config.checker.timeout_ms > 0
    assert 0 < config.intake_clerk.timeout_ms <= management.MAX_INTAKE_CLERK_TIMEOUT_MS
    assert "upagent-account-manager" in config.account_manager.command
    assert "upagent-checker" in config.checker.command
    assert "intake-clerk" in config.intake_clerk.command
    assert '--tools ""' in config.intake_clerk.command
    assert "--print" in config.intake_clerk.command
    assert "< {brief_path}" in config.intake_clerk.command
    assert "--dangerously-skip-permissions" not in config.intake_clerk.command
    assert "Read" not in config.intake_clerk.command
    assert "Write" not in config.intake_clerk.command
    assert "Bash" not in config.intake_clerk.command


def test_intake_clerk_timeout_is_capped_without_capping_other_roles() -> None:
    maximum = management.MAX_INTAKE_CLERK_TIMEOUT_MS
    accepted = management.load_management_config(
        {
            "management": {
                "intake_clerk": {"timeout_ms": maximum},
                "account_manager": {"timeout_ms": maximum + 1},
                "checker": {"timeout_ms": maximum + 2},
            }
        }
    )
    assert accepted.intake_clerk.timeout_ms == maximum
    assert accepted.account_manager.timeout_ms == maximum + 1
    assert accepted.checker.timeout_ms == maximum + 2

    with pytest.raises(management.ManagementConfigError, match="no greater than"):
        management.load_management_config(
            {"management": {"intake_clerk": {"timeout_ms": maximum + 1}}}
        )


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


def test_intake_clerk_brief_protects_execution_intent(tmp_path: Path) -> None:
    raw_path = tmp_path / "order.raw-submitted"
    output = tmp_path / "clerk-output.json"
    text = management.intake_clerk_brief(
        '{"payload": {"harness": "claude", "agent": "qa"}}',
        raw_path,
        output,
    )

    assert str(raw_path) in text and str(output) in text
    assert '"order": {' in text and '"refusal":' in text
    assert "NEVER invent or change" in text
    for field in (
        "harness/model/effort",
        "agent/persona",
        "cwd",
        "instructions_path",
        "cockpit_pane/requester",
        "operation/apply/approval",
        "env",
        "timeout",
        "watchdog",
    ):
        assert field in text


def test_intake_clerk_stdout_wrapper_quotes_trusted_paths(tmp_path: Path) -> None:
    directory = tmp_path / "space ; touch SHOULD_NOT_EXIST"
    directory.mkdir()
    brief = directory / "brief file.md"
    output = directory / "response file.json"
    brief.write_text('{"refusal":"quoted","understood":[],"missing":[]}')
    role = management.ManagementRole(
        command='printf %s "$(cat -- {brief_path})"',
        expected_agent="shell",
        expected_process="printf",
        timeout_ms=1000,
    )

    command = management.render_intake_clerk_command(role, brief, str(directory), output)
    completed = subprocess.run(
        ["bash", "-lc", command], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_text() == brief.read_text()
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert not list(directory.glob(".*.stdout.tmp"))


def test_checker_brief_forbids_authoritative_actions(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"pane_exists": True}))
    output = tmp_path / "assessment.json"
    text = management.checker_brief("req-abc", 1, evidence, output)
    assert "advisory" in text.lower()
    assert "Do not create, close, interrupt, or kill" in text
    assert "The literal request id is `req-abc`" in text
    assert "never derive or substitute an identity" in text


def test_rescuer_role_is_configured_like_every_other_management_role() -> None:
    config = management.load_management_config({"harnesses": {"claude": "claude {model}"}})
    assert config.rescuer.timeout_ms > 0
    assert "upagent-rescuer" in config.rescuer.command
    assert "--effort low" in config.rescuer.command
    assert "{brief_path}" in config.rescuer.command
    assert "{output_path}" in config.rescuer.command

    overridden = management.load_management_config(
        {"management": {"rescuer": {"timeout_ms": 60_000, "expected_agent": "codex"}}}
    )
    assert overridden.rescuer.timeout_ms == 60_000
    assert overridden.rescuer.expected_agent == "codex"

    with pytest.raises(management.ManagementConfigError, match="not_allowed"):
        management.load_management_config(
            {"management": {"rescuer": {"command": "claude {not_allowed}"}}}
        )


def test_rescuer_brief_carries_the_evidence_bundle_and_one_typed_verdict(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"kind": "salvage-rescue"}))
    output = tmp_path / "verdict.json"

    text = management.rescuer_brief("req-abc", "order-1", evidence, output)

    assert str(evidence) in text and str(output) in text
    for source in ("ledger tail", "staging directory listing", "git log", "pane capture"):
        assert source in text
    for verdict in management.RESCUER_VERDICTS:
        assert verdict in text
    assert '"cited_commits"' in text and '"cited_files"' in text


def test_rescuer_brief_forbids_authoritative_actions(tmp_path: Path) -> None:
    """Same fence as the checker: this role reads evidence, it never acts on the request."""
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"kind": "salvage-rescue"}))
    text = management.rescuer_brief(
        "req-abc", "order-1", evidence, tmp_path / "verdict.json"
    )

    assert "advisory" in text.lower()
    assert "Do not create, close, interrupt, or kill" in text
    assert "Do not launch, delegate to, or resume a worker" in text
    assert "Do not write, repair, move, or delete any artifact" in text
    assert "You cannot mark work done" in text
    assert "Python re-verifies every fact you cite" in text
    assert "The literal request id is `req-abc`" in text
    assert "never derive or substitute an identity" in text


def test_default_openai_sentinel_role_expects_the_pi_harness() -> None:
    """The code-owned fallback (roster without management.sentinels) must be
    self-consistent: DEFAULT_OPENAI_SENTINEL_COMMAND launches pi, so its health
    identity must be pi — expecting claude would fail every hire on legacy rosters."""
    config = management.load_management_config({"management": {}})
    role = config.sentinels["openai"]
    assert role.command == management.DEFAULT_OPENAI_SENTINEL_COMMAND
    assert role.expected_agent == "pi"
    assert role.expected_process == "pi"
    anthropic = config.sentinels["anthropic"]
    assert anthropic.expected_agent == "claude"
    assert anthropic.expected_process == "claude"
