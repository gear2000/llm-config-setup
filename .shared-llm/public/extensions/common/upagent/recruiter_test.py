# pyright: reportMissingImports=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportArgumentType=false
"""Unit tests for the Recruiter's pure core (roster load + launch resolution).

The Herdr-driving parts need a live Herdr and are proven end-to-end separately; these tests
cover the risky pure logic — roster validation and template substitution — with no Herdr.

Run: python3 -m pytest .shared-llm/extensions/common/upagent/recruiter_test.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import re

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_recruiter", Path(__file__).with_name("recruiter.py")
)
assert _spec and _spec.loader
recruiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recruiter)
RecruiterError = recruiter.RecruiterError
ContractError = recruiter.ContractError


@pytest.fixture(autouse=True)
def _herdr_owner_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        recruiter, "_herdr_owner_record", lambda: {"herdr_session": "llm-lab-test"}
    )
    monkeypatch.setattr(recruiter, "_require_hub_authority", lambda: None)
    # Request intake probes the cockpit pane through herdr. These tests use fictional pane
    # ids and must not depend on whether a herdr server happens to be running on the machine,
    # so the probe is neutralized here. `salvage_test.py` exercises the real check.
    monkeypatch.setattr(recruiter, "verify_cockpit_pane", lambda *a, **k: None)


# Every submission door now hires a fresh intake clerk, so the real launcher is captured here for
# the tests that prove its own behavior. Everything else gets the deterministic double below.
_live_intake_clerk = recruiter._run_order_intake_clerk


def _fake_intake_clerk(raw_text, raw_path, roster_path, intake_key, **kwargs):
    """Stand in for the intake LLM: map unambiguous aliases, refuse anything it cannot form.

    It answers the way a competent clerk does — never inventing a value the submission does not
    contain — so lifecycle tests exercise the real intake seam without an LLM.
    """
    record = {
        "attempt": kwargs.get("attempt_number", 1),
        "attempt_name": "attempt-test-double",
        "brief_path": f"{raw_path}.brief.md",
        "output_path": f"{raw_path}.response.json",
        "ownership_path": f"{raw_path}.ownership.json",
        "cleanup": {"status": "closed", "worker_pane": None, "verified_absent": True},
    }

    def answer(**fields) -> tuple[SimpleNamespace, dict]:
        base = {
            "order": None,
            "refusal": None,
            "understood": (),
            "missing": (),
            "notes": (),
        }
        return SimpleNamespace(**{**base, **fields}), record

    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError:
        document = None
    if not isinstance(document, dict):
        return answer(
            refusal="the submission is not one JSON object naming a target agent",
            missing=list(recruiter.ORDER_INTAKE_NEVER_INVENTED),
        )
    order = {}
    for canonical, aliases in recruiter.ORDER_INTAKE_ALIASES.items():
        for alias in aliases:
            if alias in document:
                order[canonical] = document[alias]
                break
    missing = [
        field
        for field in recruiter.ORDER_INTAKE_NEVER_INVENTED
        if not isinstance(order.get(field), str) or not order[field]
    ]
    if missing:
        return answer(
            refusal="the submission does not name " + ", ".join(missing),
            understood=sorted(order),
            missing=missing,
        )
    return answer(order=order)


@pytest.fixture(autouse=True)
def _stub_intake_clerk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recruiter, "_run_order_intake_clerk", _fake_intake_clerk)


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


def _cursor_health_herdr(status: str, cwd: Path) -> Callable[..., dict]:
    def herdr_json(*args: str, **_kwargs: object) -> dict:
        if args[:2] == ("pane", "get"):
            return {
                "result": {
                    "pane": {
                        "agent": "cursor",
                        "agent_status": status,
                        "foreground_cwd": str(cwd),
                    }
                }
            }
        if args[:2] == ("pane", "process-info"):
            return {
                "result": {
                    "process_info": {
                        "foreground_processes": [
                            {
                                "cmdline": "cursor-agent --force --trust",
                                "name": "node",
                                "pid": 123,
                            }
                        ]
                    }
                }
            }
        raise AssertionError(args)

    return herdr_json


def test_cursor_reconnect_loop_fails_startup_before_idle_is_marked_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recruiter, "_herdr_json", _cursor_health_herdr("idle", tmp_path)
    )
    reconnect_output = (
        "Connection lost, reconnecting to the Cursor backend "
        "(attempt 5)...\nRetry attempt 5...\n"
    )
    monkeypatch.setattr(
        recruiter, "_pane_recent_output", lambda *a, **k: reconnect_output
    )

    with pytest.raises(RecruiterError, match="failed cursor startup"):
        recruiter._wait_for_agent_health(
            "cursor-pane",
            expected_agent="cursor",
            expected_process="cursor-agent",
            expected_cwd=str(tmp_path),
            timeout_ms=1_000,
            completion_order=_order(harness="cursor"),
        )


def test_cursor_idle_with_clean_output_is_startup_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cursor may report `idle` while its interactive TUI is starting; with the
    expected process present and no reconnect loop in the output, idle is healthy
    (a 45s blocked timeout here was a real field failure)."""
    monkeypatch.setattr(
        recruiter, "_herdr_json", _cursor_health_herdr("idle", tmp_path)
    )
    monkeypatch.setattr(recruiter, "_pane_recent_output", lambda *a, **k: "")

    health = recruiter._wait_for_agent_health(
        "cursor-pane",
        expected_agent="cursor",
        expected_process="cursor-agent",
        expected_cwd=str(tmp_path),
        timeout_ms=1_000,
        completion_order=_order(harness="cursor"),
    )

    assert health["healthy"] is True


def test_cursor_working_status_is_startup_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recruiter, "_herdr_json", _cursor_health_herdr("working", tmp_path)
    )
    monkeypatch.setattr(recruiter, "_pane_recent_output", lambda *a, **k: "")

    health = recruiter._wait_for_agent_health(
        "cursor-pane",
        expected_agent="cursor",
        expected_process="cursor-agent",
        expected_cwd=str(tmp_path),
        timeout_ms=1_000,
        completion_order=_order(harness="cursor"),
    )

    assert health["healthy"] is True
    assert health["agent_status"] == "working"


def test_cursor_done_status_is_startup_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fast interactive turn may finish before the first startup probe."""
    monkeypatch.setattr(
        recruiter, "_herdr_json", _cursor_health_herdr("done", tmp_path)
    )
    monkeypatch.setattr(recruiter, "_pane_recent_output", lambda *a, **k: "")

    health = recruiter._wait_for_agent_health(
        "cursor-pane",
        expected_agent="cursor",
        expected_process="cursor-agent",
        expected_cwd=str(tmp_path),
        timeout_ms=1_000,
        completion_order=_order(harness="cursor"),
    )

    assert health["healthy"] is True
    assert health["agent_status"] == "done"


def test_cursor_working_status_ignores_stale_reconnect_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recruiter, "_herdr_json", _cursor_health_herdr("working", tmp_path)
    )
    reconnect_output = (
        "Connection lost, reconnecting to the Cursor backend "
        "(attempt 5)...\nworker later recovered\n"
    )
    monkeypatch.setattr(
        recruiter, "_pane_recent_output", lambda *a, **k: reconnect_output
    )

    health = recruiter._wait_for_agent_health(
        "cursor-pane",
        expected_agent="cursor",
        expected_process="cursor-agent",
        expected_cwd=str(tmp_path),
        timeout_ms=1_000,
        completion_order=_order(harness="cursor"),
    )

    assert health["healthy"] is True
    assert health["agent_status"] == "working"


def test_cursor_generic_retry_output_is_not_a_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generic retry chatter is not the reconnect-failure pattern; an idle pane
    with a live process and non-matching output is healthy, not failed."""
    monkeypatch.setattr(
        recruiter, "_herdr_json", _cursor_health_herdr("idle", tmp_path)
    )
    monkeypatch.setattr(
        recruiter, "_pane_recent_output", lambda *a, **k: "Retry attempt 5"
    )

    health = recruiter._wait_for_agent_health(
        "cursor-pane",
        expected_agent="cursor",
        expected_process="cursor-agent",
        expected_cwd=str(tmp_path),
        timeout_ms=1_000,
        completion_order=_order(harness="cursor"),
    )

    assert health["healthy"] is True


def test_cursor_recent_output_read_failure_does_not_abort_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recruiter, "_herdr_json", _cursor_health_herdr("idle", tmp_path)
    )

    def fail_recent_output(*_args: object, **_kwargs: object) -> str:
        raise RecruiterError("pane read failed")

    monkeypatch.setattr(recruiter, "_pane_recent_output", fail_recent_output)
    monkeypatch.setattr(recruiter, "HEALTH_PROBE_SECONDS", 0)

    with pytest.raises(RecruiterError, match="did not become healthy"):
        recruiter._wait_for_agent_health(
            "cursor-pane",
            expected_agent="cursor",
            expected_process="cursor-agent",
            expected_cwd=str(tmp_path),
            timeout_ms=1,
            completion_order=_order(harness="cursor"),
        )


def test_cursor_workers_get_a_shorter_inactivity_check() -> None:
    cursor_order = _order(harness="cursor")
    claude_order = _order(harness="claude")

    assert recruiter._worker_inactivity_check_ms(cursor_order, 900_000) == 120_000
    assert recruiter._worker_inactivity_check_ms(claude_order, 900_000) == 900_000


def _phase_order(tmp_path: Path, **over: object) -> tuple[dict, Path]:
    """Create a conventional phase-tree order subject to the release capability."""
    stage = tmp_path / "run/phases/phase-0/pass-1/stages/stage-1-implementation/try-1"
    stage.mkdir(parents=True)
    instructions = stage / "instructions.md"
    instructions.write_text("# Worker\n")
    return _order(
        instructions_path=str(instructions),
        result_path=str(stage / "result.json"),
        **over,
    ), tmp_path / "run/phases/phase-0/pass-1/control/phase-start.json"


def test_phase_order_without_controller_receipt_is_degraded_not_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    order, receipt_path = _phase_order(tmp_path)
    monkeypatch.delenv(recruiter.PHASE_START_RECEIPT_ENV, raising=False)

    warning = recruiter.phase_receipt_warning(order)
    assert "has no phase-start receipt" in warning

    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            {
                "state": "watchdog-ready",
                "phase_id": "phase-0",
                "leader_pane": "1-1",
                "watchdog": {"worker_pane": "watchdog-pane"},
            }
        )
    )
    monkeypatch.setenv(recruiter.PHASE_START_RECEIPT_ENV, str(receipt_path))
    assert recruiter.phase_receipt_warning(order) is None


def test_invalid_phase_release_receipt_becomes_a_degraded_warning(
    tmp_path: Path, monkeypatch
) -> None:
    order, receipt_path = _phase_order(tmp_path)
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            {
                "state": "ready",
                "phase_id": "phase-0",
                "leader_pane": "some-other-pane",
                "watchdog": {},
            }
        )
    )
    monkeypatch.setenv(recruiter.PHASE_START_RECEIPT_ENV, str(receipt_path))

    assert "belongs to leader some-other-pane" in recruiter.phase_receipt_warning(order)


def test_phase_watchdog_bootstrap_order_is_exempt_from_release_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    order, _receipt_path = _phase_order(tmp_path, agent="phase-watchdog")
    monkeypatch.delenv(recruiter.PHASE_START_RECEIPT_ENV, raising=False)

    assert recruiter.phase_receipt_warning(order) is None


def test_legacy_recruit_without_receipt_starts_degraded_instead_of_stalling(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    order, _receipt_path = _phase_order(tmp_path)
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.delenv(recruiter.PHASE_START_RECEIPT_ENV, raising=False)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        recruiter,
        "_spawn_job",
        lambda key, roster: started.append((key, roster)),
    )

    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0

    assert len(started) == 1
    output = capsys.readouterr().out
    assert "ORDER phase-0.stage-1-implementation.pass-1.try-1 DEGRADED" in output
    ledger = recruiter.JobLedger()
    key = ledger.key_for_order(order)
    events = [
        json.loads(path.read_text())
        for path in (ledger.request_dir(key) / "events").glob("*.json")
    ]
    assert any(event["event"] == "phase-receipt-degraded" for event in events)


def test_missing_receipt_announcement_prints_once_per_phase_pass(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Every degraded order records its ledger event, but the pane sees one warning per pass."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.delenv(recruiter.PHASE_START_RECEIPT_ENV, raising=False)
    monkeypatch.setattr(recruiter, "_spawn_job", lambda key, roster: None)

    def submit(stage_id: str, pass_name: str, try_name: str) -> tuple[dict, str]:
        stage = (
            tmp_path / f"run/phases/phase-0/{pass_name}/stages/{stage_id}/{try_name}"
        )
        stage.mkdir(parents=True, exist_ok=True)
        instructions = stage / "instructions.md"
        instructions.write_text("# Worker\n")
        order = _order(
            order_id=f"phase-0.{stage_id}.{pass_name}.{try_name}",
            stage_id=stage_id,
            instructions_path=str(instructions),
            result_path=str(stage / "result.json"),
        )
        order_path = stage / "order.json"
        order_path.write_text(json.dumps(order))
        assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0
        return order, capsys.readouterr().out

    _first_order, first = submit("stage-1-implementation", "pass-1", "try-1")
    assert "DEGRADED" in first
    assert "has no phase-start receipt" in first

    quiet_order, second = submit("stage-2-adversarial-audit", "pass-1", "try-1")
    assert "DEGRADED" not in second

    _retry_order, third = submit("stage-1-implementation", "pass-1", "try-2")
    assert "DEGRADED" not in third

    # The quiet order still carries the durable degraded event for its receipt.
    ledger = recruiter.JobLedger()
    key = ledger.key_for_order(quiet_order)
    events = [
        json.loads(path.read_text())
        for path in (ledger.request_dir(key) / "events").glob("*.json")
    ]
    assert any(event["event"] == "phase-receipt-degraded" for event in events)

    # A fresh pass gets its own single announcement.
    _fresh_order, fresh = submit("stage-1-implementation", "pass-2", "try-1")
    assert "DEGRADED" in fresh


def test_legacy_recruit_rejects_an_invalid_order_before_any_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps({"order_id": "legacy-invalid-order"}))
    monkeypatch.setattr(
        recruiter.JobLedger,
        "submit",
        lambda *args, **kwargs: pytest.fail("invalid order must not reach the ledger"),
    )
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("invalid order must not start a job"),
    )

    with pytest.raises(RecruiterError, match="invalid order"):
        recruiter.cmd_recruit(str(order_path), "roster.yaml")


def test_worker_health_requires_expected_process_agent_and_cwd(monkeypatch) -> None:
    responses = {
        ("pane", "get", "worker-pane"): {
            "result": {
                "pane": {
                    "agent": "claude",
                    "agent_status": "working",
                    "cwd": "/tmp/wt",
                    "foreground_cwd": "/tmp/wt",
                    "pane_id": "worker-pane",
                }
            }
        },
        ("pane", "process-info", "--pane", "worker-pane"): {
            "result": {
                "process_info": {
                    "foreground_processes": [
                        {"name": "claude", "pid": 42, "cwd": "/tmp/wt"}
                    ]
                }
            }
        },
    }
    monkeypatch.setattr(
        recruiter, "_herdr_json", lambda *args, **kwargs: responses[args]
    )
    evidence = recruiter._wait_for_worker_health("worker-pane", _order(), 100)
    assert evidence["healthy"] is True
    assert evidence["process_pid"] == 42


def test_worker_health_detector_can_be_overridden_by_roster(monkeypatch) -> None:
    responses = {
        ("pane", "get", "worker-pane"): {
            "result": {
                "pane": {
                    "agent": "wrapped-agent",
                    "agent_status": "working",
                    "foreground_cwd": "/tmp/wt",
                }
            }
        },
        ("pane", "process-info", "--pane", "worker-pane"): {
            "result": {
                "process_info": {
                    "foreground_processes": [{"name": "wrapper", "pid": 42}]
                }
            }
        },
    }
    monkeypatch.setattr(
        recruiter, "_herdr_json", lambda *args, **kwargs: responses[args]
    )
    roster = {
        "health": {
            "claude": {"expected_agent": "wrapped-agent", "expected_process": "wrapper"}
        }
    }
    assert (
        recruiter._wait_for_worker_health("worker-pane", _order(), 100, roster)[
            "healthy"
        ]
        is True
    )


def test_worker_health_fails_fast_when_launch_returns_to_shell(monkeypatch) -> None:
    responses = {
        ("pane", "get", "worker-pane"): {
            "result": {
                "pane": {
                    "agent_status": "unknown",
                    "cwd": "/tmp/wt",
                    "foreground_cwd": "/tmp/wt",
                    "pane_id": "worker-pane",
                }
            }
        },
        ("pane", "process-info", "--pane", "worker-pane"): {
            "result": {"process_info": {"foreground_processes": []}}
        },
    }
    monkeypatch.setattr(
        recruiter, "_herdr_json", lambda *args, **kwargs: responses[args]
    )
    monkeypatch.setattr(recruiter, "STARTUP_FAILURE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(
        recruiter,
        "_pane_recent_output",
        lambda _pane, **kwargs: "unknown model: nope",
    )
    with pytest.raises(RecruiterError, match="expected claude process"):
        recruiter._wait_for_worker_health("worker-pane", _order(), 100)


def test_start_worker_splits_then_starts_the_named_agent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    calls = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        assert kwargs == {"herdr_session": "llm-lab-test"}
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "worker-pane"}}}
        return {
            "result": {
                "agent": {
                    "name": "upagent-req-abc-g1",
                    "pane_id": "worker-pane",
                    "workspace_id": "workspace-1",
                }
            }
        }

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    pane, workspace, address = recruiter._start_herdr_agent(
        "upagent-req-abc-g1",
        _order(cockpit_pane="leader-pane", env={"HERDR_ENV": "1"}),
        "claude --model some-model",
        herdr_session="llm-lab-test",
    )
    assert (pane, workspace, address) == (
        "worker-pane",
        "workspace-1",
        "upagent-req-abc-g1",
    )
    assert calls[1] == (
        "pane",
        "split",
        "leader-pane",
        "--direction",
        "right",
        "--cwd",
        "/tmp/wt",
        "--no-focus",
        "--env",
        "HERDR_ENV=1",
    )
    assert calls[2] == (
        "agent",
        "start",
        "upagent-req-abc-g1",
        "--kind",
        "claude",
        "--pane",
        "worker-pane",
        "--timeout",
        "180000",
        "--",
        "--model",
        "some-model",
    )


def test_start_worker_takes_the_kind_from_the_command_not_the_order_harness(
    monkeypatch, tmp_path
) -> None:
    """A checker on a cursor-harness order runs claude; the kind must follow the command."""

    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    calls: list[tuple[str, ...]] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "check-pane"}}}
        return {
            "result": {
                "agent": {
                    "name": "upagent-check-14",
                    "pane_id": "check-pane",
                    "workspace_id": "workspace-1",
                }
            }
        }

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    recruiter._start_herdr_agent(
        "upagent-check-14",
        _order(cockpit_pane="leader-pane", harness="cursor"),
        "claude --dangerously-skip-permissions --agent upagent-checker "
        "--model claude-sonnet-5 --effort low 'Read /brief.md'",
        herdr_session="llm-lab-test",
    )
    start = calls[2]
    assert start[:2] == ("agent", "start")
    assert start[start.index("--kind") + 1] == "claude"
    assert start[start.index("--") + 1 :] == (
        "--dangerously-skip-permissions",
        "--agent",
        "upagent-checker",
        "--model",
        "claude-sonnet-5",
        "--effort",
        "low",
        "Read /brief.md",
    )


def test_start_worker_maps_cursor_agent_executable_to_the_cursor_kind(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    calls: list[tuple[str, ...]] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "worker-pane"}}}
        return {
            "result": {
                "agent": {
                    "name": "worker",
                    "pane_id": "worker-pane",
                    "workspace_id": "workspace-1",
                }
            }
        }

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    recruiter._start_herdr_agent(
        "worker",
        _order(cockpit_pane="leader-pane", harness="cursor"),
        "cursor-agent --force --trust --model composer-2.5 'do the work'",
        herdr_session="llm-lab-test",
    )
    assert calls[2][calls[2].index("--kind") + 1] == "cursor"


def test_start_worker_retries_a_busy_pane_then_succeeds(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    attempts = {"count": 0}

    def fake_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "worker-pane"}}}
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RecruiterError("agent_pane_busy")
        return {
            "result": {
                "agent": {
                    "name": "worker",
                    "pane_id": "worker-pane",
                    "workspace_id": "workspace-1",
                }
            }
        }

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    monkeypatch.setattr(recruiter, "AGENT_PANE_READY_INTERVAL_SECONDS", 0)

    assert recruiter._start_herdr_agent(
        "worker",
        _order(cockpit_pane="leader-pane"),
        "claude --model some-model",
        herdr_session="llm-lab-test",
    ) == ("worker-pane", "workspace-1", "worker")
    assert attempts["count"] == 3


def test_start_worker_closes_the_split_pane_when_agent_start_fails(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    closed: list[tuple[str, ...]] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "worker-pane"}}}
        raise RecruiterError("unsupported_agent_kind")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    monkeypatch.setattr(
        recruiter, "_herdr", lambda *args, **kwargs: closed.append(args)
    )

    with pytest.raises(RecruiterError, match="unsupported_agent_kind"):
        recruiter._start_herdr_agent(
            "upagent-req-abc-g1",
            _order(cockpit_pane="leader-pane"),
            "claude --model some-model",
            herdr_session="llm-lab-test",
        )
    assert closed == [("pane", "close", "worker-pane")]


def test_start_worker_rejects_a_harness_with_no_herdr_kind(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))

    def fake_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        raise AssertionError(args)

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    with pytest.raises(RecruiterError, match="not a Herdr agent kind"):
        recruiter._start_herdr_agent(
            "upagent-req-abc-g1",
            _order(cockpit_pane="leader-pane", harness="bash"),
            "bash -lc true",
            herdr_session="llm-lab-test",
        )


def test_start_herdr_agent_honors_downward_role_placement(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    calls = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        assert kwargs == {"herdr_session": "llm-lab-test"}
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "manager-pane"}}}
        return {
            "result": {
                "agent": {
                    "name": "manager",
                    "pane_id": "manager-pane",
                    "workspace_id": "workspace-1",
                }
            }
        }

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)

    recruiter._start_herdr_agent(
        "manager",
        _order(cockpit_pane="leader-pane"),
        "claude --model some-model",
        split_direction="down",
        herdr_session="llm-lab-test",
    )

    split = calls[1]
    assert split[:2] == ("pane", "split")
    assert split[split.index("--direction") + 1] == "down"


def test_safe_agent_name_fits_herdr_name_limit_for_every_prefix() -> None:
    request_id = "801647cf-4a01-41b2-ab4d-2afe16af7f40"
    names = set()
    for prefix in ("upagent", "upagent-manager", "upagent-rescue", "upagent-sentinel"):
        for generation in (1, 12):
            name = recruiter._safe_agent_name(prefix, request_id, generation)
            assert len(name) <= recruiter.HERDR_AGENT_NAME_MAX, name
            assert re.fullmatch(r"[a-z][a-z0-9_-]*", name), name
            names.add(name)
    assert len(names) == 8


def test_safe_agent_name_disambiguates_ids_sharing_a_long_prefix() -> None:
    shared = "run-p01/phase-1/pass-1/stage-2-adversarial-audit"
    first = recruiter._safe_agent_name("upagent", f"{shared}-try1-primary", 1)
    second = recruiter._safe_agent_name("upagent", f"{shared}-try2-second", 1)
    assert first != second
    assert first.startswith("upagent-") and first.endswith("-g1")
    # Deterministic for the same id, and shell/Herdr-safe.
    assert first == recruiter._safe_agent_name("upagent", f"{shared}-try1-primary", 1)
    assert all(c.isalnum() or c in "-_" for c in first)


def test_start_herdr_agent_refuses_duplicate_agent_name(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))

    def fake_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("agent", "get"):
            return {"result": {"agent": {"name": "worker", "pane_id": "other-pane"}}}
        raise AssertionError(f"launch must not proceed past the name check: {args}")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    with pytest.raises(RecruiterError, match="already exists in Herdr"):
        recruiter._start_herdr_agent(
            "worker",
            _order(cockpit_pane="leader-pane"),
            "claude",
            herdr_session="llm-lab-test",
        )


def test_concurrent_same_name_launches_start_exactly_one_agent(
    monkeypatch, tmp_path
) -> None:
    """Two simultaneous launches of one name: the lock lets exactly one start."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    state = {"started": False}
    start_calls: list[tuple[str, ...]] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("agent", "get"):
            if state["started"]:
                return {"result": {"agent": {"name": args[2], "pane_id": "pane-first"}}}
            time.sleep(0.05)  # widen the check-then-act window
            raise RecruiterError("agent_not_found")
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "pane-first"}}}
        if args[:2] == ("agent", "start"):
            assert not state["started"], "second start raced past the name lock"
            state["started"] = True
            start_calls.append(args)
            return {
                "result": {
                    "agent": {
                        "name": args[2],
                        "pane_id": "pane-first",
                        "workspace_id": "workspace-1",
                    }
                }
            }
        raise AssertionError(f"unexpected herdr call: {args}")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    errors: list[BaseException] = []

    def launch() -> None:
        try:
            recruiter._start_herdr_agent(
                "worker",
                _order(cockpit_pane="leader-pane"),
                "claude --model some-model",
                herdr_session="llm-lab-test",
            )
        except RecruiterError as error:
            errors.append(error)

    threads = [threading.Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(start_calls) == 1
    assert len(errors) == 1
    assert "already exists in Herdr" in str(errors[0])


def test_start_herdr_agent_rejects_unknown_split_direction() -> None:
    with pytest.raises(RecruiterError, match="must be right or down"):
        recruiter._start_herdr_agent(
            "worker", _order(), "claude", split_direction="diagonal"
        )


def test_place_started_agent_creates_role_tab_from_the_live_pane(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        assert kwargs == {
            "herdr_session": "llm-lab-test",
            "timeout_seconds": recruiter.LAYOUT_COMMAND_TIMEOUT_SECONDS,
        }
        if args[:2] == ("tab", "list"):
            return {
                "result": {
                    "tabs": [
                        {
                            "label": "control",
                            "tab_id": "control-tab",
                            "workspace_id": "workspace-1",
                        }
                    ]
                }
            }
        if args[:2] == ("pane", "move"):
            return {
                "result": {
                    "move_result": {
                        "changed": True,
                        "pane": {
                            "pane_id": "worker-pane",
                            "tab_id": "workers-tab",
                            "workspace_id": "workspace-1",
                        },
                    }
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)

    pane = recruiter._place_started_agent_in_role_tab(
        "worker-pane",
        "workspace-1",
        "workers",
        split_direction="right",
        herdr_session="llm-lab-test",
    )

    assert pane == "worker-pane"
    assert calls[-1] == (
        "pane",
        "move",
        "worker-pane",
        "--new-tab",
        "--workspace",
        "workspace-1",
        "--label",
        "workers",
        "--no-focus",
    )


def test_place_started_agent_joins_existing_role_tab(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        assert kwargs == {
            "herdr_session": "llm-lab-test",
            "timeout_seconds": recruiter.LAYOUT_COMMAND_TIMEOUT_SECONDS,
        }
        if args[:2] == ("tab", "list"):
            return {
                "result": {
                    "tabs": [
                        {
                            "label": "oversight",
                            "tab_id": "oversight-tab",
                            "workspace_id": "workspace-1",
                        }
                    ]
                }
            }
        if args[:2] == ("pane", "list"):
            return {
                "result": {
                    "panes": [
                        {
                            "pane_id": "manager-pane",
                            "tab_id": "oversight-tab",
                            "workspace_id": "workspace-1",
                        }
                    ]
                }
            }
        if args[:2] == ("pane", "move"):
            return {
                "result": {
                    "move_result": {
                        "changed": True,
                        "pane": {
                            "pane_id": "watchdog-pane",
                            "tab_id": "oversight-tab",
                            "workspace_id": "workspace-1",
                        },
                    }
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)

    recruiter._place_started_agent_in_role_tab(
        "watchdog-pane",
        "workspace-1",
        "oversight",
        split_direction="down",
        herdr_session="llm-lab-test",
    )

    assert calls[-1] == (
        "pane",
        "move",
        "watchdog-pane",
        "--tab",
        "oversight-tab",
        "--split",
        "down",
        "--target-pane",
        "manager-pane",
        "--no-focus",
    )


def test_tab_placement_failure_keeps_the_started_agent_alive(
    monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    calls: list[tuple[str, ...]] = []
    closed: list[str] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        if args[:2] == ("agent", "get"):
            raise RecruiterError("agent_not_found")
        if args == ("pane", "get", "leader-pane"):
            return {
                "result": {
                    "pane": {
                        "pane_id": "worker-pane",
                        "tab_id": "control-tab",
                        "workspace_id": "workspace-1",
                    }
                }
            }
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "worker-pane"}}}
        if args[:2] == ("agent", "start"):
            return {
                "result": {
                    "agent": {
                        "name": "worker",
                        "pane_id": "worker-pane",
                        "workspace_id": "workspace-1",
                    }
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)
    monkeypatch.setattr(
        recruiter,
        "_place_started_agent_in_role_tab",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("tab placement failed")
        ),
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", closed.append)

    started = recruiter._start_herdr_agent(
        "worker",
        _order(cockpit_pane="leader-pane"),
        "claude --model some-model",
        tab_role="workers",
        herdr_session="llm-lab-test",
    )

    assert started == ("worker-pane", "workspace-1", "worker")
    assert closed == []
    assert "tab placement failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    (
        "split_direction",
        "target_fraction",
        "neighbor_direction",
        "neighbor_pane",
        "amount",
    ),
    [
        ("down", 0.20, "up", "upper-pane", "0.3"),
        ("right", 0.28, "left", "left-pane", "0.22"),
    ],
)
def test_resize_started_pane_shrinks_new_split(
    monkeypatch,
    split_direction: str,
    target_fraction: float,
    neighbor_direction: str,
    neighbor_pane: str,
    amount: str,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        assert kwargs == {
            "timeout_seconds": recruiter.LAYOUT_COMMAND_TIMEOUT_SECONDS,
            "herdr_session": None,
        }
        if args[0:2] == ("pane", "neighbor"):
            return {"result": {"neighbor": {"neighbor_pane_id": neighbor_pane}}}
        return {"result": {"resize": {"changed": True}}}

    monkeypatch.setattr(recruiter, "_herdr_json", fake_json)

    recruiter._resize_started_pane(
        "new-pane",
        split_direction=split_direction,
        target_fraction=target_fraction,
        role="managed role",
    )

    assert calls == [
        (
            "pane",
            "neighbor",
            "--direction",
            neighbor_direction,
            "--pane",
            "new-pane",
        ),
        (
            "pane",
            "resize",
            "--pane",
            neighbor_pane,
            "--direction",
            split_direction,
            "--amount",
            amount,
        ),
    ]


def test_resize_started_pane_warns_without_failing_lifecycle(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("resize unavailable")
        ),
    )

    recruiter._resize_started_pane(
        "watchdog-pane",
        split_direction="right",
        target_fraction=0.28,
        role="watchdog",
    )

    assert (
        "watchdog pane watchdog-pane layout adjustment failed"
        in capsys.readouterr().err
    )


def test_resize_started_pane_is_a_quiet_noop_for_first_pane_in_role_tab(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {
                "neighbor": {
                    "layout": {"panes": [{"pane_id": "only-pane"}]},
                    "pane_id": "only-pane",
                }
            }
        },
    )

    recruiter._resize_started_pane(
        "only-pane",
        split_direction="down",
        target_fraction=0.20,
        role="account manager",
    )

    assert capsys.readouterr().err == ""


def test_watchdog_worker_fraction_is_role_specific() -> None:
    assert recruiter._worker_pane_fraction({"agent": "plan-lifecycle-watchdog"}) == 0.28
    assert recruiter._worker_pane_fraction({"agent": "phase-watchdog"}) == 0.28
    assert recruiter._worker_pane_fraction({"agent": "docs-writer"}) is None


def test_worker_tab_role_separates_active_work_from_oversight() -> None:
    assert recruiter._worker_tab_role({"agent": "docs-writer"}) == "workers"
    assert (
        recruiter._worker_tab_role({"agent": "plan-lifecycle-watchdog"}) == "oversight"
    )
    assert recruiter._worker_tab_role({"agent": "phase-watchdog"}) == "oversight"


def test_herdr_json_converts_timeout_to_recruiter_error(monkeypatch) -> None:
    monkeypatch.setattr(
        recruiter.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            recruiter.subprocess.TimeoutExpired("herdr", 0.1)
        ),
    )

    with pytest.raises(RecruiterError, match="timed out after 0.1 seconds"):
        recruiter._herdr_json(
            "pane", "layout", timeout_seconds=0.1, herdr_session="llm-lab-test"
        )


def test_explicit_session_command_still_checks_herdr_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recruiter.shutil, "which", lambda command: None)
    monkeypatch.setattr(
        recruiter.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "missing Herdr must fail before subprocess"
        ),
    )

    with pytest.raises(RecruiterError, match="not found in PATH"):
        recruiter._herdr("pane", "close", "pane-1", herdr_session="llm-lab-test")


def test_current_herdr_session_resolves_by_socket_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/default.sock")
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)

    def fake_run(args, **kwargs):
        assert args == ["herdr", "session", "list", "--json"]
        return recruiter.subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "sessions": [
                        {
                            "default": True,
                            "name": "default",
                            "running": True,
                            "socket_path": "/tmp/default.sock",
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(recruiter.subprocess, "run", fake_run)

    assert recruiter._resolve_current_herdr_session_name() == "default"


def test_current_herdr_session_uses_real_status_payload_shape_when_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["herdr", "status", "--json"]:
            return recruiter.subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "client": {"session": None},
                        "server": {
                            "running": True,
                            "socket": "/tmp/default.sock",
                            "status": "running",
                        },
                    }
                ),
                "",
            )
        if args == ["herdr", "session", "list", "--json"]:
            return recruiter.subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "sessions": [
                            {
                                "default": True,
                                "name": "default",
                                "running": True,
                                "socket_path": "/tmp/default.sock",
                            }
                        ]
                    }
                ),
                "",
            )
        raise AssertionError(args)

    monkeypatch.setattr(recruiter.subprocess, "run", fake_run)

    assert recruiter._resolve_current_herdr_session_name() == "default"
    assert calls == [
        ["herdr", "status", "--json"],
        ["herdr", "session", "list", "--json"],
    ]


def test_session_name_validation_matches_herdr_charset() -> None:
    assert (
        recruiter._validate_herdr_session_name("lab.one_2-three") == "lab.one_2-three"
    )
    with pytest.raises(RecruiterError, match="unsupported characters"):
        recruiter._validate_herdr_session_name("lab:one")


def test_current_herdr_session_refuses_ambiguous_socket_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/same.sock")
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)

    def fake_run(args, **kwargs):
        return recruiter.subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "sessions": [
                        {
                            "default": False,
                            "name": "llm-lab-a",
                            "running": True,
                            "socket_path": "/tmp/same.sock",
                        },
                        {
                            "default": False,
                            "name": "llm-lab-b",
                            "running": True,
                            "socket_path": "/tmp/same.sock",
                        },
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(recruiter.subprocess, "run", fake_run)

    with pytest.raises(RecruiterError, match="expected exactly one"):
        recruiter._resolve_current_herdr_session_name()


def test_close_worker_pane_requires_recorded_session() -> None:
    with pytest.raises(RecruiterError, match="recorded Herdr session"):
        recruiter._close_worker_pane("pane-1")


def test_close_worker_pane_uses_global_session_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:4] == ["herdr", "--session", "llm-lab-test", "pane"]:
            if args[4] == "close":
                return recruiter.subprocess.CompletedProcess(args, 0, "", "")
            if args[4] == "list":
                return recruiter.subprocess.CompletedProcess(
                    args, 0, '{"result":{"panes":[]}}\n', ""
                )
        raise AssertionError(args)

    monkeypatch.setattr(recruiter.subprocess, "run", fake_run)

    cleanup = recruiter._close_worker_pane("pane-1", herdr_session="llm-lab-test")

    assert cleanup["verified_absent"] is True
    assert calls[0] == ["herdr", "--session", "llm-lab-test", "pane", "close", "pane-1"]
    assert calls[1] == ["herdr", "--session", "llm-lab-test", "pane", "list"]


def test_checker_cleanup_guards_layout_adjustment(tmp_path: Path, monkeypatch) -> None:
    order = _order(cwd=str(tmp_path))
    worker_result = tmp_path / "worker-result.json"
    worker_result.write_text(json.dumps(_result(order["order_id"])))
    ledger = recruiter.JobLedger(tmp_path / "hub")
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 60_000)
    assert token
    manager = {
        "address": "manager-address",
        "config": recruiter.llm_management.load_management_config(_roster()),
        "generation": 1,
        "herdr_session": "llm-lab-test",
        "lease_token": token,
        "pane": "manager-pane",
    }
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {"pane": {}}
            if args[0:2] == ("pane", "get")
            else {"result": {"process_info": {}}}
        },
    )
    monkeypatch.setattr(recruiter, "_pane_recent_output", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: ("checker-pane", "workspace", "checker-address"),
    )
    monkeypatch.setattr(
        recruiter,
        "_resize_started_pane",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    closed: list[str] = []
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: closed.append(pane) or _cleanup(pane),
    )

    with pytest.raises(KeyboardInterrupt):
        recruiter._run_one_shot_checker(
            ledger, key, order, manager, "worker-pane", worker_result, 1
        )

    assert closed == ["checker-pane"]


def _result(order_id: str, verdict: str = "passed") -> dict:
    result: dict[str, object] = {
        "order_id": order_id,
        "verdict": verdict,
        "full_log": "/tmp/worker.log",
    }
    if verdict == "failed":
        result["revisit"] = ["stage-1-implementation"]
    return result


def test_receipt_artifacts_carry_frozen_present_flags(tmp_path: Path) -> None:
    """Every receipt artifact entry records `present`, stat-frozen at publication.

    A receipt must never advertise a file that does not exist: a skipped optional summary is
    listed (the path stays known) but marked present=false, and required artifacts are always
    present=true — the field failure was a review receipt vouching for a compacted.md that
    was never written.
    """
    root = tmp_path / "upagent-hub"
    ledger = recruiter.JobLedger(root)
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token is not None
    recruiter.completion.ensure_publication_contract(order)
    manifest = recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    result = _result(order["order_id"])
    recruiter.JobLedger._write_json(manifest.artifact("result").staging_path, result)
    manifest.artifact("handoff").staging_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.artifact("handoff").staging_path.write_text("# Handoff\n")
    # compacted is deliberately never staged: optional here, so the job still publishes.
    assert ledger.finalize(key, token, order, result, cleanup=_cleanup(), exit_code=0)
    receipt = json.loads((ledger.request_dir(key) / "receipt.json").read_text())
    by_kind = {item["kind"]: item for item in receipt["artifacts"]}
    assert by_kind["result"]["present"] is True
    assert by_kind["result"]["required"] is True
    assert by_kind["handoff"]["present"] is True
    assert by_kind["compacted"]["present"] is False
    assert by_kind["compacted"]["required"] is False


def test_finalize_publishes_the_normalized_result_bytes(tmp_path: Path) -> None:
    """The reader repairs a string `revisit`; publication must ship the REPAIRED bytes.

    Without the write-back, every hub-side reader saw the normalized list while the
    caller-visible result.json kept the raw malformed string — two contradictory records
    of the same result.
    """
    root = tmp_path / "upagent-hub"
    ledger = recruiter.JobLedger(root)
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token is not None
    recruiter.completion.ensure_publication_contract(order)
    manifest = recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    raw = {
        "order_id": order["order_id"],
        "verdict": "passed",
        "revisit": "stage-1-implementation",
        "full_log": "/tmp/worker.log",
    }
    recruiter.JobLedger._write_json(manifest.artifact("result").staging_path, raw)
    manifest.artifact("compacted").staging_path.parent.mkdir(
        parents=True, exist_ok=True
    )
    manifest.artifact("compacted").staging_path.write_text("# Compact\n")
    manifest.artifact("handoff").staging_path.write_text("# Handoff\n")
    parsed = recruiter.load_result(
        manifest.artifact("result").staging_path, expected_order_id=order["order_id"]
    )
    assert parsed["revisit"] == ["stage-1-implementation"]

    assert ledger.finalize(key, token, order, parsed, cleanup=_cleanup(), exit_code=0)

    published = json.loads(Path(order["result_path"]).read_text())
    assert published["revisit"] == ["stage-1-implementation"]
    assert published["revisit_normalized"] == "stage-1-implementation"
    staged = json.loads(manifest.artifact("result").staging_path.read_text())
    assert staged["revisit"] == ["stage-1-implementation"]


def test_default_roster_precedence_repo_then_common_dir_then_kit(
    tmp_path: Path, monkeypatch
) -> None:
    """Roster resolution: repo-owned roster by walk-up, then the git common dir's main
    checkout (worktrees never contain the gitignored this_repo), then the kit default."""
    import subprocess as sp

    monkeypatch.delenv("UPAGENT_CONFIG", raising=False)
    main = tmp_path / "main"
    roster = main / ".shared-llm/this_repo/extensions/common/upagent/upagent.yaml"
    roster.parent.mkdir(parents=True)
    roster.write_text('harnesses:\n  claude: "claude {model}"\n')

    # 1. Walk-up from a subdirectory of the main checkout.
    sub = main / "src"
    sub.mkdir()
    monkeypatch.setattr(recruiter.command_runtime, "current_cwd", lambda: sub)
    assert recruiter.default_roster_path() == str(roster)

    # 2. A linked worktree OUTSIDE the main checkout resolves the main checkout's roster.
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin",
    }
    sp.run(["git", "init", "-q", str(main)], check=True, env=env)
    sp.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
        env=env,
    )
    worktree = tmp_path / "wt"
    sp.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(worktree)],
        check=True,
        env=env,
    )
    monkeypatch.setattr(recruiter.command_runtime, "current_cwd", lambda: worktree)
    assert recruiter.default_roster_path() == str(roster)

    # 3. No repo config anywhere: the kit-shipped default beside the engine.
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(recruiter.command_runtime, "current_cwd", lambda: plain)
    assert recruiter.default_roster_path() == str(recruiter.HERE / "upagent.yaml")
    assert (recruiter.HERE / "upagent.yaml").is_file()


def test_consult_receipt_publication_is_atomic_and_idempotent_under_concurrency(
    tmp_path: Path, monkeypatch
) -> None:
    """Repro for the reported consult-inside-review 'receipt-publication path collision'.

    A parent request's consult receipt and a retried publication of the same receipt may land
    near-simultaneously. Publication is temp-file + atomic rename at both the sidecar and the
    requester-keyed index, so every concurrent writer must leave only complete, parseable
    records — never a partial file, never an exception.
    """
    import threading

    monkeypatch.setattr(
        recruiter,
        "consult_index_entry_path",
        lambda requested_by, consult_id: (
            tmp_path / "index" / requested_by / f"{consult_id}.json"
        ),
    )
    receipt = {
        "consult_id": "consult-race-1",
        "order_receipt_state": "finished",
        "requested_by": "worker-parent",
        "answer_verdict": "cited",
    }
    sidecar = tmp_path / "consult" / "consult-receipt.json"
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            recruiter._publish_consult_receipt(dict(receipt), sidecar)
        except BaseException as error:  # noqa: BLE001 - the test asserts none occur
            errors.append(error)

    threads = [threading.Thread(target=publish) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    published = json.loads(sidecar.read_text())
    assert published["consult_id"] == "consult-race-1"
    index_entry = tmp_path / "index" / "worker-parent" / "consult-race-1.json"
    assert json.loads(index_entry.read_text())["consult_id"] == "consult-race-1"
    assert not list(sidecar.parent.glob("*.tmp"))
    assert not list(index_entry.parent.glob("*.tmp"))


def _cleanup(worker_pane: str | None = "worker-pane") -> dict:
    return {
        "status": "closed" if worker_pane else "not-created",
        "worker_pane": worker_pane,
        "verified_absent": True,
    }


def _manifest_for_private(order: dict, result_path: Path) -> Any:
    recruiter.completion.ensure_publication_contract(order)
    publication = order["artifact_publication"]
    return recruiter.completion.Manifest(
        order_id=order["order_id"],
        request_id=recruiter.lifecycle.request_identity(order),
        lease_token="test-lease",
        artifacts=(
            recruiter.completion.Artifact(
                "result", result_path, Path(order["result_path"]), "application/json"
            ),
            recruiter.completion.Artifact(
                "compacted",
                result_path.with_name("compacted.md"),
                Path(publication["compacted_path"]),
                "text/markdown",
            ),
            recruiter.completion.Artifact(
                "handoff",
                result_path.with_name("handoff.md"),
                Path(publication["handoff_path"]),
                "text/markdown",
            ),
        ),
    )


def _write_typed_worker_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    path.with_name("compacted.md").write_text("# Worker compacted evidence\n")
    path.with_name("handoff.md").write_text("# Worker handoff evidence\n")


def _finalize(
    ledger: Any,
    key: str,
    token: str,
    order: dict,
    result: dict,
    **kwargs: object,
) -> bool:
    """Stage the required typed bundle before exercising the real finalization commit."""
    recruiter.completion.ensure_publication_contract(order)
    manifest = recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    recruiter.JobLedger._write_json(manifest.artifact("result").staging_path, result)
    manifest.artifact("compacted").staging_path.parent.mkdir(
        parents=True, exist_ok=True
    )
    manifest.artifact("compacted").staging_path.write_text(
        "# Test compacted evidence\n"
    )
    manifest.artifact("handoff").staging_path.write_text("# Test handoff evidence\n")
    return ledger.finalize(key, token, order, result, **kwargs)


def _patch_approved_manager(monkeypatch) -> None:
    import dataclasses

    real_load = recruiter.llm_management.load_management_config
    monkeypatch.setattr(
        recruiter.llm_management,
        "load_management_config",
        lambda roster: dataclasses.replace(real_load(roster), mode="dedicated"),
    )

    def fake_account_manager(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "address": "manager-address",
            "config": real_load(args[4]),
            "decision": SimpleNamespace(decision="approved", message="approved"),
            "generation": 1,
            "herdr_session": kwargs.get("herdr_session"),
            "pane": "manager-pane",
            "workspace_id": "manager-workspace",
        }

    monkeypatch.setattr(recruiter, "_start_account_manager", fake_account_manager)
    monkeypatch.setattr(
        recruiter,
        "_ask_manager_about_startup",
        lambda *args: SimpleNamespace(
            assessment="healthy", message="startup validated"
        ),
    )
    monkeypatch.setattr(recruiter, "_notify_requester", lambda *args, **kwargs: None)


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


def test_configuration_inspection_finds_missing_agent_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do work.\n")
    monkeypatch.setattr(recruiter.shutil, "which", lambda binary: f"/bin/{binary}")
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / "result.json"),
        agent="not-installed-here",
    )
    roster = {"harnesses": {"claude": "claude --agent {agent} --model {model}"}}

    evidence = recruiter.inspect_worker_configuration(order, roster)

    assert evidence["valid"] is False
    assert any("not-installed-here" in error for error in evidence["errors"])


def test_configuration_inspection_accepts_existing_agent_and_binary(
    tmp_path: Path, monkeypatch
) -> None:
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do work.\n")
    agent = tmp_path / ".claude/agents/backend.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("# Backend\n")
    monkeypatch.setattr(recruiter.shutil, "which", lambda binary: f"/bin/{binary}")
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / "result.json"),
    )
    roster = {"harnesses": {"claude": "claude --agent {agent} --model {model}"}}

    assert recruiter.inspect_worker_configuration(order, roster)["valid"] is True


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


def test_load_roster_rejects_incomplete_health_override(tmp_path: Path) -> None:
    p = tmp_path / "upagent.yaml"
    p.write_text(
        "harnesses:\n  claude: 'claude --model {model}'\n"
        "health:\n  claude:\n    expected_agent: wrapped\n"
    )
    with pytest.raises(RecruiterError, match="expected_process"):
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
    assert (
        json.loads((request / "state/latest.json").read_text())["state"] == "requested"
    )
    assert len(list((request / "events").glob("*.json"))) == 1
    assert not list(request.rglob("*.tmp"))
    assert ledger.submit(order) == (key, False)


def test_job_ledger_rejects_conflicting_duplicate_id(tmp_path: Path) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    ledger.submit(_order())
    with pytest.raises(RecruiterError, match="collision"):
        ledger.submit(_order(agent="different"))


def test_job_ledger_scopes_same_human_order_id_to_each_run_result_path(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    first, _ = ledger.submit(_order(result_path=str(tmp_path / "run-a/result.json")))
    second, _ = ledger.submit(_order(result_path=str(tmp_path / "run-b/result.json")))
    assert first != second


def test_job_ledger_concurrent_identical_submit_never_reads_partial_request(
    tmp_path: Path,
) -> None:
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
    second.join(timeout=0.1)
    assert second.is_alive(), "the coarse mutation lock must exclude a second submitter"
    release_first_submitter.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(outcomes) == 2
    assert sum(created for _, created in outcomes) == 1


def test_job_ledger_finalization_publishes_valid_result_and_terminal_state(
    tmp_path: Path,
) -> None:
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
    assert _finalize(
        ledger,
        key,
        token,
        order,
        _result(order["order_id"]),
        cleanup=cleanup,
        exit_code=0,
    )
    assert not (root / "active/requests" / key).exists()
    assert index.is_file()
    assert json.loads(Path(order["result_path"]).read_text()) == _result(
        order["order_id"]
    )
    latest = json.loads((ledger.request_dir(key) / "state/latest.json").read_text())
    assert latest["state"] == "finished" and latest["verdict"] == "passed"
    receipt = json.loads((ledger.request_dir(key) / "receipt.json").read_text())
    assert {
        key: receipt[key]
        for key in (
            "cleanup",
            "generation",
            "order_id",
            "published_result_path",
            "request_id",
            "result_path",
            "state",
            "verdict",
        )
    } == {
        "cleanup": cleanup,
        "generation": 1,
        "order_id": order["order_id"],
        "published_result_path": str(ledger.published_result_path(key)),
        "request_id": recruiter.lifecycle.request_identity(order),
        "result_path": order["result_path"],
        "state": "finished",
        "verdict": "passed",
    }
    assert [item["kind"] for item in receipt["artifacts"]] == [
        "result",
        "compacted",
        "handoff",
    ]
    # The receipt only means something alongside the copy it vouches for.
    assert json.loads(ledger.published_result_path(key).read_text()) == _result(
        order["order_id"]
    )


def test_requester_decision_is_fenced_to_current_lease_and_extends_it(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000, owner={"generation": 1})
    assert token
    active_lease = ledger._lease(ledger.active / "requests" / key / "lease.json")
    ledger._snapshot(key, "running", **active_lease)
    lease = ledger.mark_awaiting_requester(key, token, "nonce-1", 1)
    assert lease is not None
    decision = recruiter.lifecycle.parse_requester_decision(
        json.dumps(
            {
                "request_id": recruiter.lifecycle.request_identity(order),
                "generation": 1,
                "action": "extend",
                "extension_ms": 60_000,
                "message": "Continue.",
            }
        ),
        recruiter.lifecycle.request_identity(order),
        1,
    )
    path = ledger.record_requester_decision(key, token, "nonce-1", decision)
    old_expiry = lease["expires_at"]
    new_expiry = ledger.extend_lease(key, token, 60_000)

    assert path.is_file()
    assert new_expiry > old_expiry
    assert ledger.state(key)["state"] == "running"


def test_retained_order_default_timeout_is_30_minutes_not_stage_default() -> None:
    ordinary = _order(stage_id="stage-1-implementation")
    retained = _order(
        stage_id="stage-1-implementation", completion_policy="requester_release"
    )
    explicit = {**retained, "timeout_ms": 7_200_000}

    assert recruiter._order_timeout_ms(ordinary) == 10_800_000
    assert recruiter._order_timeout_ms(retained) == 1_800_000
    assert recruiter._order_timeout_ms(explicit) == 7_200_000


def test_run_order_honors_authorized_extension_after_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    private_result = tmp_path / "private-result.json"
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(
        timeout_ms=10,
        result_path=str(tmp_path / "public-result.json"),
        instructions_path=str(instructions),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')
    waits = []

    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda name, execution_order, launch, **kwargs: (
            "worker-pane",
            "cockpit",
            name,
        ),
    )
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    def wait(_pane: str, timeout_ms: int, _finalized: object, **_: object) -> bool:
        waits.append(timeout_ms)
        if len(waits) == 1:
            raise recruiter.AgentWaitTimeout("cap reached")
        private_result.write_text(json.dumps(_result(order["order_id"])))
        return True

    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    timeouts = []

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        on_timeout=lambda number, finalized: timeouts.append(number) or 50,
        herdr_session="llm-lab-test",
        artifact_manifest=_manifest_for_private(order, private_result),
    )

    assert code == 0
    assert result["verdict"] == "passed"
    assert waits == [10, 50]
    assert timeouts == [1]
    assert cleanup["verified_absent"] is True


def test_run_order_blocks_result_when_startup_assessment_rejects_it(
    tmp_path: Path, monkeypatch
) -> None:
    private_result = tmp_path / "private-result.json"
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(
        result_path=str(tmp_path / "result.json"), instructions_path=str(instructions)
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')

    def start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        private_result.parent.mkdir(parents=True, exist_ok=True)
        private_result.write_text(json.dumps(_result(order["order_id"])))
        return "worker-pane", "cockpit", name

    monkeypatch.setattr(recruiter, "_start_herdr_agent", start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    code, result, _ = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        on_worker_healthy=lambda evidence: (_ for _ in ()).throw(
            RecruiterError("startup rejected")
        ),
        herdr_session="llm-lab-test",
        artifact_manifest=_manifest_for_private(order, private_result),
    )

    assert code == 1
    assert result["verdict"] == "blocked"
    assert "startup rejected" in result["reason"]


def test_submit_agent_prompt_uses_atomic_run_by_default(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(recruiter, "_herdr", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"agent": {"pane_id": "manager-pane"}}},
    )

    recruiter._submit_agent_prompt("manager-name", "Review evidence.", 5_000)

    assert calls == [
        ("agent", "wait", "manager-name", "--until", "idle", "--timeout", "5000"),
        ("pane", "run", "manager-pane", "Review evidence."),
    ]


def test_submit_agent_prompt_splits_cursor_paste_and_enter(monkeypatch) -> None:
    calls = []
    sleeps = []
    monkeypatch.setattr(recruiter, "_herdr", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"agent": {"pane_id": "cursor-pane"}}},
    )
    monkeypatch.setattr(recruiter.time, "sleep", sleeps.append)

    recruiter._submit_agent_prompt(
        "cursor-name",
        "Repair result.json.",
        5_000,
        paste_settle_seconds=recruiter.CURSOR_PROMPT_PASTE_SETTLE_SECONDS,
    )

    assert calls == [
        ("agent", "wait", "cursor-name", "--until", "idle", "--timeout", "5000"),
        ("pane", "send-text", "cursor-pane", "Repair result.json."),
        ("pane", "send-keys", "cursor-pane", "Enter"),
    ]
    assert sleeps == [recruiter.CURSOR_PROMPT_PASTE_SETTLE_SECONDS]


def test_timeout_waits_for_authenticated_requester_extension(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000, owner={"generation": 1})
    assert token
    active_lease = ledger._lease(ledger.active / "requests" / key / "lease.json")
    ledger._snapshot(key, "running", **active_lease)
    manager = {
        "address": "manager-name",
        "config": SimpleNamespace(
            account_manager=SimpleNamespace(timeout_ms=100), requester_grace_ms=1_000
        ),
        "generation": 1,
    }
    monkeypatch.setattr(recruiter, "_submit_agent_prompt", lambda *args, **kwargs: None)
    monkeypatch.setattr(recruiter, "_notify_requester", lambda *args, **kwargs: None)
    outcomes = []
    runner = threading.Thread(
        target=lambda: outcomes.append(
            recruiter._await_requester_timeout_decision(
                ledger, key, token, order, manager, "worker-pane", 1, threading.Event()
            )
        )
    )
    runner.start()
    deadline = time.monotonic() + 1
    state = ledger.state(key)
    while state.get("state") != "awaiting-requester" and time.monotonic() < deadline:
        time.sleep(0.01)
        state = ledger.state(key)
    assert state["state"] == "awaiting-requester"
    decision = recruiter.lifecycle.RequesterDecision(
        recruiter.lifecycle.request_identity(order),
        1,
        "extend",
        60_000,
        "Continue.",
    )
    ledger.record_requester_decision(key, token, state["decision_nonce"], decision)
    runner.join(timeout=1)

    assert not runner.is_alive()
    assert outcomes == [60_000]
    assert ledger.state(key)["state"] == "running"


def test_worker_instructions_have_no_result_only_fallback(tmp_path: Path) -> None:
    original = tmp_path / "instructions.md"
    original.write_text("Do the stage. An older brief mentioned /public/result.json.\n")
    order = _order(
        instructions_path=str(original),
        result_path=str(tmp_path / "public/result.json"),
    )
    manifest = _manifest_for_private(order, tmp_path / "private/result.json")
    generated = tmp_path / "hub/worker-instructions.md"

    recruiter._write_worker_instructions(
        order, manifest.artifact("result").staging_path, generated, manifest
    )

    text = generated.read_text()
    # Workers are still asked for every artifact even though publication tolerates a missing
    # summary — advertising the tolerance would stop the summaries from ever being written.
    assert "Write ALL of these artifacts" in text
    assert "compacted.md" in text and "handoff.md" in text
    assert '`verdict`: exactly one of "passed", "failed", or "blocked"' in text
    assert "`full_log`: a non-empty transcript path" in text
    assert "Write exactly one result JSON file" not in text


def test_retained_worker_instructions_checkpoint_before_terminal_artifacts(
    tmp_path: Path,
) -> None:
    original = tmp_path / "instructions.md"
    original.write_text("Implement the change.\n")
    order = _order(
        instructions_path=str(original),
        result_path=str(tmp_path / "public/result.json"),
        completion_policy="requester_release",
    )
    manifest = _manifest_for_private(order, tmp_path / "private/result.json")
    generated = tmp_path / "worker-instructions.md"

    recruiter._write_worker_instructions(
        order, manifest.artifact("result").staging_path, generated, manifest
    )

    text = generated.read_text()
    assert "retained review-loop assignment" in text
    assert "checkpoint-0001.json" in text
    assert "Do NOT write result.json" in text
    assert "Only REVIEW_RELEASE authorizes" in text


def test_retained_checkpoint_requires_review_evidence(tmp_path: Path) -> None:
    order = _order(completion_policy="requester_release")
    review_dir = tmp_path / "review"
    recruiter.JobLedger._write_json(
        review_dir / "checkpoint-0001.json",
        {
            "schema_version": 1,
            "order_id": order["order_id"],
            "sequence": 1,
            "summary": "looks done",
        },
    )
    with pytest.raises(RecruiterError, match="summary, tests, and changed_files"):
        recruiter._retained_checkpoint(review_dir, order, 1)


def test_unreleased_retained_result_is_never_preserved_after_wait_fault() -> None:
    order = _order(completion_policy="requester_release")
    assert (
        recruiter._may_preserve_worker_result(
            order, _result(order["order_id"]), startup_validated=True
        )
        is False
    )


def test_finalize_rejects_unreleased_retained_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    submitted = _order(
        result_path=str(tmp_path / "public/result.json"),
        completion_policy="requester_release",
    )
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(submitted)
    order = ledger.order(key)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={"generation": 1, "herdr_session": "test-session", "runner_pid": -1},
    )
    assert isinstance(token, str)
    result = _result(order["order_id"])
    recruiter.JobLedger._write_json(ledger.result_staging_path(key, token), result)
    with pytest.raises(RecruiterError, match="signed requester release"):
        ledger.finalize(
            key,
            token,
            order,
            result,
            cleanup={"status": "closed", "verified_absent": True},
        )


def test_retained_completion_monitor_quarantines_result_until_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    submitted = _order(
        result_path=str(tmp_path / "public/result.json"),
        completion_policy="requester_release",
    )
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(submitted)
    order = ledger.order(key)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={"generation": 1, "herdr_session": "test-session", "runner_pid": -1},
    )
    assert isinstance(token, str)
    active_lease = ledger._lease(ledger.active / "requests" / key / "lease.json")
    ledger._snapshot(key, "running", **active_lease)
    control_token = ledger.state(key)["requester_control_token"]
    manifest = recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    result_path = manifest.artifact("result").staging_path
    result_path.parent.mkdir(parents=True)
    stop, finalized, thread = recruiter._start_completion_monitor(
        order,
        result_path,
        1_000,
        artifact_manifest=manifest,
    )
    try:
        # Worker-visible staging cannot authorize its own release.
        recruiter.JobLedger._write_json(
            result_path.parent / "review/release.json",
            {
                "schema_version": 1,
                "order_id": order["order_id"],
                "lease_token": manifest.lease_token,
                "sequence": 1,
            },
        )
        result_path.write_text(json.dumps(_result(order["order_id"])))
        deadline = time.monotonic() + 2
        while result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not result_path.exists()
        assert not finalized.is_set()
        assert list((result_path.parent / "review/premature").glob("*/result.json"))

        review_dir = result_path.parent / "review"
        checkpoint_path = review_dir / "checkpoint-0001.json"
        recruiter.JobLedger._write_json(
            checkpoint_path,
            {
                "schema_version": 1,
                "order_id": order["order_id"],
                "request_id": recruiter.lifecycle.request_identity(order),
                "lease_token": token,
                "generation": 1,
                "sequence": 1,
                "summary": "ready for review",
                "tests": "pass",
                "changed_files": ["x.py"],
            },
        )
        checkpoint_sha256 = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
        authoritative = recruiter._review_release_path(result_path)
        recruiter.JobLedger._write_json(
            authoritative,
            {
                "schema_version": 1,
                "order_id": order["order_id"],
                "request_id": recruiter.lifecycle.request_identity(order),
                "lease_token": manifest.lease_token,
                "generation": 1,
                "sequence": 1,
                "checkpoint_sha256": checkpoint_sha256,
                "created_at_ns": time.time_ns(),
                "requester_signature": "0" * 64,
            },
        )
        result_path.write_text(json.dumps(_result(order["order_id"])))
        deadline = time.monotonic() + 2
        while result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not result_path.exists()
        assert not finalized.is_set()

        _release, reservation_id = ledger.authorize_review_release(
            key, control_token, token, order, 1, checkpoint_sha256
        )
        assert list((authoritative.parent / "quarantine").glob("*.json"))
        ledger.complete_review_release(key, token, reservation_id)
        result_path.write_text(json.dumps(_result(order["order_id"])))
        assert finalized.wait(timeout=2)
    finally:
        stop.set()
        thread.join(timeout=2)


def test_internal_retained_review_commands_continue_and_release_same_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    order = _order(
        result_path=str(tmp_path / "public/result.json"),
        completion_policy="requester_release",
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        120_000,
        owner={"generation": 1, "herdr_session": "test-session", "runner_pid": -1},
    )
    assert isinstance(token, str)
    assert ledger.record_worker(
        key, token, "worker-pane", "workspace", "worker-address"
    )
    assert ledger.mark_worker_healthy(key, token, {"healthy": True})
    control_token = ledger.state(key)["requester_control_token"]
    review_dir = ledger.result_staging_path(key, token).parent / "review"
    request_id = recruiter.lifecycle.request_identity(order)
    checkpoint1 = review_dir / "checkpoint-0001.json"
    recruiter.JobLedger._write_json(
        checkpoint1,
        {
            "schema_version": 1,
            "order_id": order["order_id"],
            "request_id": request_id,
            "lease_token": token,
            "generation": 1,
            "sequence": 1,
            "summary": "first pass",
            "tests": "pass",
            "changed_files": ["x.py"],
        },
    )
    checkpoint1_sha256 = hashlib.sha256(checkpoint1.read_bytes()).hexdigest()
    feedback = tmp_path / "feedback.md"
    feedback.write_text("Tighten the edge case.\n")
    prompts: list[str] = []
    cancellation_races: list[str] = []
    timeout_races: list[object] = []

    def capture_prompt(_address: str, message: str, **_kwargs: object) -> None:
        prompts.append(message)
        with pytest.raises(RecruiterError) as error:
            ledger.begin_cancel(key, control_token)
        cancellation_races.append(str(error.value))
        timeout_races.append(
            ledger.mark_awaiting_requester(
                key, token, f"nonce-{len(prompts)}", len(prompts)
            )
        )

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", capture_prompt)

    checkpoint1_bytes = checkpoint1.read_bytes()
    changed = json.loads(checkpoint1_bytes)
    changed["summary"] = "worker replaced the reviewed bytes"
    recruiter.JobLedger._write_json(checkpoint1, changed)
    with pytest.raises(RecruiterError, match="changed after requester inspection"):
        recruiter.cmd_review_continue(
            str(order_path), control_token, 1, checkpoint1_sha256, str(feedback)
        )
    checkpoint1.write_bytes(checkpoint1_bytes)

    assert (
        recruiter.cmd_review_continue(
            str(order_path), control_token, 1, checkpoint1_sha256, str(feedback)
        )
        == 0
    )
    assert "checkpoint-0002.json" in prompts[0]
    assert (review_dir / "feedback-0001.delivery.json").is_file()
    feedback_state = ledger.state(key)
    assert "review_delivery_sequence" not in feedback_state
    assert "review_delivery_reserved_at_ns" not in feedback_state

    checkpoint2 = review_dir / "checkpoint-0002.json"
    recruiter.JobLedger._write_json(
        checkpoint2,
        {
            "schema_version": 1,
            "order_id": order["order_id"],
            "request_id": request_id,
            "lease_token": token,
            "generation": 1,
            "sequence": 2,
            "summary": "revised pass",
            "tests": "pass",
            "changed_files": ["x.py"],
        },
    )
    checkpoint2_sha256 = hashlib.sha256(checkpoint2.read_bytes()).hexdigest()
    with pytest.raises(RecruiterError, match="latest retained checkpoint"):
        recruiter.cmd_review_release(
            str(order_path), control_token, 1, checkpoint1_sha256
        )

    def fail_release(*_args: object, **_kwargs: object) -> None:
        raise RecruiterError("injected release delivery failure")

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", fail_release)
    with pytest.raises(RecruiterError, match="injected release"):
        recruiter.cmd_review_release(
            str(order_path), control_token, 2, checkpoint2_sha256
        )
    assert ledger.state(key)["state"] == "running"
    assert not ledger.review_release_path(key, token).exists()

    _release, partial_reservation_id = ledger.authorize_review_release(
        key, control_token, token, order, 2, checkpoint2_sha256
    )
    claim_path = ledger.active / "requests" / key / "lease.json"
    partial_lease = ledger._lease(claim_path)
    partial_lease.pop("release_delivery_reservation_id")
    partial_lease.pop("release_delivery_reserved_at_ns")
    ledger._write_json(claim_path, partial_lease)
    ledger._snapshot(key, "running", **partial_lease)

    _release, stale_reservation_id = ledger.authorize_review_release(
        key, control_token, token, order, 2, checkpoint2_sha256
    )
    assert stale_reservation_id == partial_reservation_id
    stale_lease = ledger._lease(claim_path)
    stale_lease["release_delivery_reserved_at_ns"] = 0
    ledger._write_json(claim_path, stale_lease)

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", capture_prompt)
    assert (
        recruiter.cmd_review_release(
            str(order_path), control_token, 2, checkpoint2_sha256
        )
        == 0
    )
    assert "REVIEW_RELEASE" in prompts[1]
    release_record = json.loads(ledger.review_release_path(key, token).read_text())
    assert release_record["sequence"] == 2
    assert release_record["delivery_reservation_id"] == stale_reservation_id
    assert "review-delivering" in cancellation_races[0]
    assert "release-delivering" in cancellation_races[1]
    assert timeout_races == [None, None]
    final_state = ledger.state(key)
    assert final_state["state"] == "finalizing"
    assert "release_delivery_reservation_id" not in final_state
    assert "release_delivery_reserved_at_ns" not in final_state


def test_typed_worker_instructions_name_every_private_artifact_and_no_public_answer(
    tmp_path: Path,
) -> None:
    original = tmp_path / "instructions.md"
    public_answer = tmp_path / "public/answer.json"
    original.write_text(f"Legacy public destination: {public_answer}\n")
    order = _order(
        instructions_path=str(original), result_path=str(tmp_path / "result.json")
    )
    order["artifact_publication"] = {
        "schema_version": 1,
        "compacted_path": str(tmp_path / "compacted.md"),
        "handoff_path": str(tmp_path / "handoff.md"),
        "answer_path": str(public_answer),
        "consult_id": "consult-1",
        "mandatory_consults": [],
    }
    manifest = recruiter.completion.build_manifest(
        order, tmp_path / "hub/request", "token", "request-1"
    )
    assert manifest is not None
    generated = tmp_path / "hub/worker-instructions.md"

    recruiter._write_worker_instructions(
        order, manifest.artifact("result").staging_path, generated, manifest
    )

    final_contract = generated.read_text().split(
        "# Recruiter delivery contract (final and authoritative)", maxsplit=1
    )[1]
    for artifact in manifest.artifacts:
        assert str(artifact.staging_path) in final_contract
        assert str(artifact.public_path) not in final_contract
    assert "result.json" in final_contract
    assert "compacted.md" in final_contract
    assert "handoff.md" in final_contract
    assert "answer.json" in final_contract


def test_run_order_creates_private_result_parent_before_worker_launch(
    tmp_path: Path, monkeypatch
) -> None:
    public_result = tmp_path / "public-result.json"
    private_result = tmp_path / "hub" / "missing-results-dir" / "token.json"
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(
        agent="plan-lifecycle-watchdog",
        result_path=str(public_result),
        instructions_path=str(instructions),
        mode="direct",
        plan_id="sample-run",
        step_id="plan-watchdog",
        watchdog_terminal={
            "identity": "sample-run",
            "kind": "plan",
            "path": str(tmp_path / "control/run-terminal.json"),
        },
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        assert private_result.parent.is_dir()
        terminal_path = Path(order["watchdog_terminal"]["path"])
        terminal_path.parent.mkdir(parents=True)
        summary_path = tmp_path / "run-status.md"
        summary_path.write_text("# Complete\n")
        terminal_path.write_text(
            json.dumps(
                {
                    "plan_id": "sample-run",
                    "state": "succeeded",
                    "summary_path": str(summary_path),
                }
            )
        )
        private_result.write_text(json.dumps(_result(order["order_id"])))
        return "worker-pane", "cockpit", name

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(
        recruiter, "_wait_for_agent_status", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)
    lifecycle_events: list[str] = []

    def record_worker(*args: object) -> threading.Event:
        lifecycle_events.append("recorded")
        return threading.Event()

    def resize_worker(*args: object, **kwargs: object) -> None:
        assert lifecycle_events == ["recorded"]
        lifecycle_events.append("resized")

    monkeypatch.setattr(recruiter, "_resize_started_pane", resize_worker)

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        on_worker_launched=record_worker,
        herdr_session="llm-lab-test",
        artifact_manifest=_manifest_for_private(order, private_result),
    )

    assert code == 0
    assert result == _result(order["order_id"])
    assert cleanup["verified_absent"] is True
    assert lifecycle_events == ["recorded", "resized"]


def test_completion_monitor_accepts_a_valid_result_without_the_optional_summaries(
    tmp_path: Path,
) -> None:
    """Only result.json gates completion; the summaries are best-effort."""
    order = _order(result_path=str(tmp_path / "public/result.json"))
    private = tmp_path / "private/result.json"
    manifest = _manifest_for_private(order, private)
    private.parent.mkdir(parents=True)

    stop, ready, thread = recruiter._start_completion_monitor(
        order, private, 1_000, artifact_manifest=manifest
    )

    # Nothing staged yet: the monitor still waits.
    assert not ready.wait(timeout=0.2)
    private.write_text(json.dumps(_result(order["order_id"])))
    assert ready.wait(timeout=0.5)
    stop.set()
    thread.join(timeout=0.5)
    assert not thread.is_alive()


def test_completion_monitor_can_resume_after_a_premature_result(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / "private-result.json"
    order = _order(result_path=str(tmp_path / "public-result.json"))
    manifest = _manifest_for_private(order, result_path)
    stop, ready, thread = recruiter._start_completion_monitor(
        order, result_path, 1_000, artifact_manifest=manifest
    )

    _write_typed_worker_result(result_path, _result(order["order_id"]))
    assert ready.wait(timeout=0.5)
    result_path.unlink()
    ready.clear()
    time.sleep(0.1)
    assert thread.is_alive()

    _write_typed_worker_result(result_path, _result(order["order_id"]))
    assert ready.wait(timeout=0.5)
    stop.set()
    thread.join(timeout=0.5)
    assert not thread.is_alive()


def test_watchdog_terminal_gate_requires_durable_matching_plan_marker(
    tmp_path: Path,
) -> None:
    marker_path = tmp_path / "control/run-terminal.json"
    order = _order(
        agent="plan-lifecycle-watchdog",
        mode="direct",
        plan_id="sample-run",
        step_id="plan-watchdog",
        watchdog_terminal={
            "identity": "sample-run",
            "kind": "plan",
            "path": str(marker_path),
        },
    )
    result = _result(order["order_id"])

    assert "does not exist" in recruiter._watchdog_terminal_reason(order, result)

    marker_path.parent.mkdir()
    summary_path = tmp_path / "run-status.md"
    summary_path.write_text("# Complete\n")
    marker_path.write_text(
        json.dumps(
            {
                "plan_id": "sample-run",
                "state": "succeeded",
                "summary_path": str(summary_path),
            }
        )
    )
    assert recruiter._watchdog_terminal_reason(order, result) is None


def test_wait_fault_never_preserves_a_premature_watchdog_result(
    tmp_path: Path,
) -> None:
    marker_path = tmp_path / "control/run-terminal.json"
    order = _order(
        agent="plan-lifecycle-watchdog",
        mode="direct",
        plan_id="sample-run",
        step_id="plan-watchdog",
        watchdog_terminal={
            "identity": "sample-run",
            "kind": "plan",
            "path": str(marker_path),
        },
    )
    result = _result(order["order_id"])

    assert not recruiter._may_preserve_worker_result(
        order, result, startup_validated=True
    )

    marker_path.parent.mkdir()
    marker_path.write_text("not json")
    assert not recruiter._may_preserve_worker_result(
        order, result, startup_validated=True
    )


def test_run_order_keeps_watchdog_alive_after_premature_self_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_result = tmp_path / "hub/results/token.json"
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Watch the plan.\n")
    marker_path = tmp_path / "run/control/run-terminal.json"
    order = _order(
        agent="plan-lifecycle-watchdog",
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / "public-result.json"),
        mode="direct",
        plan_id="sample-run",
        step_id="plan-watchdog",
        watchdog_terminal={
            "identity": "sample-run",
            "kind": "plan",
            "path": str(marker_path),
        },
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')
    finalized = threading.Event()
    prompts: list[str] = []
    waits = 0

    def fake_start(*args: object, **kwargs: object) -> tuple[str, str, str]:
        private_result.parent.mkdir(parents=True, exist_ok=True)
        private_result.write_text(json.dumps(_result(order["order_id"])))
        finalized.set()
        return "watchdog-pane", "cockpit", "watchdog-address"

    def fake_wait(*args: object, **kwargs: object) -> bool:
        nonlocal waits
        waits += 1
        if waits == 2:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path = tmp_path / "run-status.md"
            summary_path.write_text("# Complete\n")
            marker_path.write_text(
                json.dumps(
                    {
                        "plan_id": "sample-run",
                        "state": "succeeded",
                        "summary_path": str(summary_path),
                    }
                )
            )
            private_result.write_text(json.dumps(_result(order["order_id"])))
            finalized.set()
        return False

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", fake_wait)
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda target, message, idle_timeout_ms, **kwargs: prompts.append(message),
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        on_worker_launched=lambda *args: finalized,
        herdr_session="llm-lab-test",
        artifact_manifest=_manifest_for_private(order, private_result),
    )

    assert code == 0
    assert result == _result(order["order_id"])
    assert cleanup["verified_absent"] is True
    assert waits == 2
    assert len(prompts) == 1
    assert "Resume monitoring" in prompts[0]
    assert len(list((private_result.parent / "premature-results").glob("*.json"))) == 1


def test_spawn_job_uses_a_detached_supervisor_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "ledger")
    key = "request-key"
    ledger.request_dir(key).mkdir(parents=True)
    roster = tmp_path / "roster.yaml"
    roster.write_text("{}\n")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    handle = object()

    def popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return handle

    monkeypatch.setattr(recruiter, "JobLedger", lambda: ledger)
    monkeypatch.setattr(recruiter.subprocess, "Popen", popen)

    assert recruiter._spawn_job(key, str(roster)) is handle
    argv, kwargs = calls[0]
    assert argv[-2:] == ["run-job", key]
    assert argv[1] == str(Path(recruiter.__file__).resolve())
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is recruiter.subprocess.DEVNULL
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["UPAGENT_HUB_DIR"] == str(ledger.root.resolve())


def test_recruit_completed_order_emits_done_without_spawning(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    assert _finalize(
        ledger, key, token, order, _result(order["order_id"]), cleanup=_cleanup(None)
    )
    monkeypatch.setattr(
        recruiter,
        "_spawn_job",
        lambda *args, **kwargs: pytest.fail(
            "completed order must not spawn a job runner"
        ),
    )

    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0
    assert capsys.readouterr().out == f"ORDER {order['order_id']} DONE\n"


def test_recruit_submits_and_spawns_without_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    # A door test keeps its result inside tmp_path: the legacy door writes a blocked result to
    # whatever result_path a submission names, and _order()'s default is a shared real directory.
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    spawned = []
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(
        recruiter,
        "_spawn_job",
        lambda key, roster: spawned.append((key, roster)),
    )
    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0
    assert len(spawned) == 1
    assert spawned[0][1] == "roster.yaml"
    key = recruiter.JobLedger().key_for_order(order)
    assert (tmp_path / "hub/requests" / key / "state/latest.json").is_file()


def test_dispatch_blocks_on_job_process_and_returns_durable_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    order = _order(
        result_path=str(tmp_path / "result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    polls = []

    class Process:
        def poll(self) -> int:
            polls.append(True)
            ledger = recruiter.JobLedger()
            key = ledger.key_for_order(order)
            token = ledger.claim(key, order["order_id"], 1_000)
            assert token
            assert _finalize(
                ledger,
                key,
                token,
                order,
                _result(order["order_id"]),
                cleanup=_cleanup(),
            )
            return 0

    monkeypatch.setattr(recruiter, "_spawn_job", lambda key, roster: Process())

    assert recruiter.cmd_dispatch(str(order_path), "roster.yaml") == 0

    output = capsys.readouterr().out
    assert output.startswith("ORDER_RECEIPT ")
    assert (
        json.loads(output.removeprefix("ORDER_RECEIPT "))["order_id"]
        == order["order_id"]
    )
    assert polls


def test_dispatch_reconciles_its_exited_dead_child_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _order(
        result_path=str(tmp_path / "result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
        timeout_ms=60_000,
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    claimed = False

    class ExitedProcess:
        def poll(self) -> int:
            nonlocal claimed
            if not claimed:
                ledger = recruiter.JobLedger()
                key = ledger.key_for_order(order)
                token = ledger.claim(
                    key,
                    order["order_id"],
                    60_000,
                    owner={"herdr_session": "test-session", "runner_pid": 999_999},
                )
                assert token
                # The dead child's watcher had already proven the idle deadline: the
                # typed `never-started` terminal is minted only over this event.
                ledger._event(
                    key,
                    "worker-never-started",
                    deadline_ms=300_000,
                    worker_pane="worker-pane",
                    attempt=1,
                )
                claimed = True
            return 9

    monkeypatch.setattr(recruiter, "_spawn_job", lambda _key, _roster: ExitedProcess())
    monkeypatch.setattr(recruiter, "_runner_alive", lambda _pid, _key: False)
    started = time.monotonic()

    assert recruiter.cmd_dispatch(str(order_path), "roster.yaml") == 1

    assert time.monotonic() - started < 1
    ledger = recruiter.JobLedger()
    key = ledger.key_for_order(order)
    receipt = ledger.completed_receipt(key, order)
    # An empty claim with no recorded first action reconciles as the typed
    # `never-started` terminal (startup-marker gate), not generic blocked.
    assert receipt["verdict"] == "never-started"
    assert not dict(ledger.active_claims())


def test_dispatch_returns_nonzero_for_a_terminal_blocked_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order(
        result_path=str(tmp_path / "result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert isinstance(token, str)
    blocked = {
        **_result(order["order_id"], "blocked"),
        "reason": "bounded task could not complete",
    }
    assert _finalize(ledger, key, token, order, blocked, cleanup=_cleanup())
    monkeypatch.setattr(
        recruiter,
        "_spawn_job",
        lambda key, roster: pytest.fail("re-ran an already terminal order"),
    )

    assert recruiter.cmd_dispatch(str(order_path), "roster.yaml") == 1


def _finished_order(tmp_path: Path, monkeypatch) -> tuple[dict, Path, object, str]:
    """Submit and finish one Stage 1 order, leaving a terminal ledger record behind."""
    order = _order(
        result_path=str(tmp_path / "result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    staging = ledger.result_staging_path(key, token)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(json.dumps(_result(order["order_id"])))
    assert _finalize(
        ledger, key, token, order, _result(order["order_id"]), cleanup=_cleanup()
    )
    monkeypatch.setattr(
        recruiter,
        "_spawn_job",
        lambda key, roster: pytest.fail("re-ran an already finished order"),
    )
    return order, order_path, ledger, key


def _receipts(capsys) -> list[dict]:
    return [
        json.loads(line.removeprefix("ORDER_RECEIPT "))
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("ORDER_RECEIPT ")
    ]


def test_dispatch_reconciles_terminal_order_whose_public_result_was_pruned(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A finished order outlives the run tree that owned its `result_path`.

    Dispatch must reconcile from the hub's own durable copy instead of crashing inside the
    strict result loader, and must keep answering with the same terminal receipt.
    """
    order, order_path, ledger, key = _finished_order(tmp_path, monkeypatch)
    Path(order["result_path"]).unlink()

    for _ in range(3):
        assert recruiter.cmd_dispatch(str(order_path), "roster.yaml") == 0

    receipts = _receipts(capsys)
    assert len(receipts) == 3
    assert all(receipt == receipts[0] for receipt in receipts)
    assert receipts[0]["verdict"] == "passed"
    assert ledger.completed_result(key, order) == _result(order["order_id"])


def test_dispatch_recovers_a_terminal_record_that_predates_the_receipt_pointer(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A receipt written before it named its durable copy still has the lease-private result."""
    order, order_path, ledger, key = _finished_order(tmp_path, monkeypatch)
    receipt_path = ledger.request_dir(key) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("published_result_path", None)
    ledger._write_json(receipt_path, receipt)
    (ledger.request_dir(key) / "published-result.json").unlink(missing_ok=True)
    Path(order["result_path"]).unlink()

    assert recruiter.cmd_dispatch(str(order_path), "roster.yaml") == 0

    assert _receipts(capsys)[0]["verdict"] == "passed"
    assert Path(order["result_path"]).is_file()


def test_dispatch_reports_evidence_when_a_terminal_result_cannot_be_recovered(
    tmp_path: Path, monkeypatch
) -> None:
    """No durable copy survives: refuse visibly with evidence, never a bare loader crash."""
    order, order_path, ledger, key = _finished_order(tmp_path, monkeypatch)
    Path(order["result_path"]).unlink()
    for durable in ledger.request_dir(key).rglob("*result*.json"):
        durable.unlink()

    with pytest.raises(RecruiterError) as failure:
        recruiter.cmd_dispatch(str(order_path), "roster.yaml")

    message = str(failure.value)
    assert order["order_id"] in message
    assert order["result_path"] in message
    assert str(ledger.request_dir(key) / "receipt.json") in message
    assert "passed" in message


def test_completed_result_returns_durable_when_the_public_read_always_oserrors(
    tmp_path: Path, monkeypatch
) -> None:
    """`load_result` can raise OSError on EVERY read of the public result — persistently unreadable,
    or a file that keeps racing. Reconciliation returns the hub's own validated durable object and
    must NOT re-read the public path it just wrote: that second read is the very loader crash this
    method exists to prevent."""
    order, order_path, ledger, key = _finished_order(tmp_path, monkeypatch)
    real_load = recruiter.load_result
    public_reads = {"n": 0}

    def always_racing_load(path, expected_order_id=None, **kwargs):
        if str(path) == order["result_path"]:
            public_reads["n"] += 1
            raise OSError("simulated: result.json unreadable on every read")
        return real_load(path, expected_order_id=expected_order_id, **kwargs)

    monkeypatch.setattr(recruiter, "load_result", always_racing_load)

    result = ledger.completed_result(key, order)

    assert result == _result(order["order_id"])
    assert (
        public_reads["n"] == 1
    )  # the initial read only; reconcile returns durable, never re-reads


def test_reconcile_returns_durable_when_republishing_the_public_result_oserrors(
    tmp_path: Path, monkeypatch
) -> None:
    """Republication is best-effort. If writing the recovered result back to the public path raises
    OSError (unwritable dir, disk full), reconciliation still returns the already-recovered durable
    object — it must never lose it or crash. The next dispatch reconciles again idempotently."""
    order, order_path, ledger, key = _finished_order(tmp_path, monkeypatch)
    Path(order["result_path"]).unlink()  # public gone -> ContractError enters reconcile
    real_write = ledger._write_json

    def failing_write(path, value):
        if str(path) == order["result_path"]:
            raise OSError("simulated: cannot republish (unwritable)")
        return real_write(path, value)

    monkeypatch.setattr(ledger, "_write_json", failing_write)

    result = ledger.completed_result(key, order)

    assert result == _result(order["order_id"])
    assert not Path(
        order["result_path"]
    ).exists()  # republish failed; the result was still returned


def test_a_terminal_record_refuses_with_evidence_when_the_hub_copy_is_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    """The public result is gone AND the hub's own durable copy raises OSError on read. The
    terminal record must refuse with evidence — a structured RecruiterError naming the receipt —
    not a bare loader crash out of `_durable_terminal_result`."""
    order, order_path, ledger, key = _finished_order(tmp_path, monkeypatch)
    Path(
        order["result_path"]
    ).unlink()  # public copy gone -> ContractError enters reconcile
    durable = str(ledger.published_result_path(key))
    real_load = recruiter.load_result

    def flaky_load(path, expected_order_id=None, **kwargs):
        if str(path) == durable:
            raise OSError("simulated: durable copy unreadable")
        return real_load(path, expected_order_id=expected_order_id, **kwargs)

    monkeypatch.setattr(recruiter, "load_result", flaky_load)

    with pytest.raises(RecruiterError) as failure:
        ledger.completed_result(key, order)

    message = str(failure.value)
    assert order["order_id"] in message
    assert str(ledger.request_dir(key) / "receipt.json") in message


def test_expired_lease_with_recorded_worker_requires_runtime_reconciliation(
    tmp_path: Path,
) -> None:
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


def test_reconciler_closes_only_recorded_worker_and_publishes_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    ledger = recruiter.JobLedger()
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        1_000,
        owner={"herdr_session": "llm-lab-owned", "runner_pid": 999},
    )
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
        lambda pane, **kwargs: (
            closed.append(pane)
            or {"status": "closed", "worker_pane": pane, "verified_absent": True}
        ),
    )

    assert recruiter.cmd_reconcile(force=True) == 0

    assert closed == ["owned-worker"]
    # The worker died after staging a valid result but before writing its summaries. That
    # result is real work and is published rather than discarded.
    recovered = ledger.completed_result(key, order)
    assert recovered["verdict"] == "passed"
    receipt = ledger.completed_receipt(key, order)
    assert receipt["cleanup"]["worker_pane"] == "owned-worker"
    for path in (
        Path(order["artifact_publication"]["compacted_path"]),
        Path(order["artifact_publication"]["handoff_path"]),
    ):
        assert not path.exists()


def test_force_reconcile_drains_live_inflight_launch_in_one_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order = _order(
        result_path=str(tmp_path / "result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "herdr_session": "test-session",
            "runner_pid": 999_999,
            "runner_start_time": "live-start",
        },
    )
    assert token
    launch_id = ledger.begin_launch(
        key,
        token,
        "worker",
        "inflight-agent",
        "test-session",
        order["cwd"],
    )
    alive = True
    terminated: list[int] = []

    def runner_alive(_pid: object, _key: str) -> bool:
        return alive

    def terminate(pid: object, _key: str) -> None:
        nonlocal alive
        assert isinstance(pid, int)
        terminated.append(pid)
        alive = False

    monkeypatch.setattr(recruiter, "_runner_alive", runner_alive)
    monkeypatch.setattr(recruiter, "_terminate_owned_runner", terminate)
    monkeypatch.setattr(recruiter, "_same_owner_process", lambda _owner: alive)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("agent_not_found")
        ),
    )

    assert recruiter.cmd_reconcile(force=True) == 0

    assert terminated == [999_999]
    journal = json.loads(ledger.launch_journal_path(key, launch_id).read_text())
    assert journal["state"] == "closed"
    assert journal["cleanup"]["verified_absent"] is True
    receipt = ledger.completed_receipt(key, order)
    assert receipt["state"] == "finished"
    assert receipt["cleanup"]["verified_absent"] is True
    assert not dict(ledger.active_claims())


def test_crash_reconcile_completes_missing_terminal_runner_marker(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(
        result_path=str(tmp_path / "result.json"),
        public_request={"payload_sha256": "b" * 64},
    )
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        1_000,
        owner={
            "request_id": recruiter.lifecycle.request_identity(order),
            "runner_pid": 999_999,
            "runner_start_time": "dead-start",
        },
    )
    assert token
    assert _finalize(
        ledger,
        key,
        token,
        order,
        _result(order["order_id"]),
        cleanup=_cleanup(),
        defer_runner_completion=True,
    )
    assert not (ledger.request_dir(key) / "runner-completed.json").exists()

    assert recruiter._reconcile_terminal_runners(ledger) == 1
    marker = json.loads((ledger.request_dir(key) / "runner-completed.json").read_text())
    assert marker["source"] == "crash-reconciler"


def test_missing_runner_json_is_reconstructed_after_dead_runner_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    payload_sha256 = "c" * 64
    order = _order(
        result_path=str(tmp_path / "result.json"),
        public_request={"payload_sha256": payload_sha256},
    )
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        1_000,
        owner={
            "herdr_session": "test-session",
            "request_id": recruiter.lifecycle.request_identity(order),
            "runner_pid": 999_998,
            "runner_start_time": "dead-winning-start",
        },
    )
    assert token
    # Deadline-proven: the never-started reconciliation below requires this event.
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane",
        attempt=1,
    )
    runner_path = ledger.request_dir(key) / "runner.json"
    runner_path.unlink()
    lease = ledger._lease(ledger.active / "requests" / key / "lease.json")

    # Simulate a crash after the reconciler's finalize commit but before its immediate marker.
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "mark_runner_completed", lambda *_args, **_kwargs: False)
        assert recruiter._reconcile_claim(ledger, key, lease, force=True)

    assert not dict(ledger.active_claims())
    assert (ledger.request_dir(key) / "receipt.json").is_file()
    assert not runner_path.exists()
    assert not (ledger.request_dir(key) / "runner-completed.json").exists()
    incomplete = dict(ledger.incomplete_terminal_runners())
    assert incomplete[key]["runner_pid"] == 999_998
    assert incomplete[key]["runner_start_time"] == "dead-winning-start"

    assert recruiter._reconcile_terminal_runners(ledger) == 1
    reconstructed = json.loads(runner_path.read_text())
    assert reconstructed["reconstructed_from_receipt"] is True
    marker = json.loads((ledger.request_dir(key) / "runner-completed.json").read_text())
    assert marker["source"] == "crash-reconciler"
    request_id = recruiter.lifecycle.request_identity(order)
    evidence = ledger.terminal_cleanup_evidence(key, request_id, payload_sha256)
    assert evidence["runner_completed"]["runner_pid"] == 999_998
    tombstone = {
        "request_id": request_id,
        "payload_sha256": payload_sha256,
        # An empty dead-runner claim with no recorded first action now reconciles as the
        # typed `never-started` terminal (startup-marker gate).
        "terminal_verdict": "never-started",
    }
    pruned = ledger.prune_terminal(
        key,
        request_id,
        payload_sha256,
        tombstone,
        verify_absence=recruiter._verify_terminal_cleanup_absence,
    )
    assert pruned["already_pruned"] is False
    assert (ledger.request_dir(key) / "tombstone.json").is_file()


def test_reconcile_paused_in_herdr_cleanup_does_not_hold_mutation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "first-result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        1_000,
        owner={"herdr_session": "test-session", "runner_pid": -1},
    )
    assert token
    lease = ledger._lease(ledger.active / "requests" / key / "lease.json")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    errors: list[BaseException] = []

    def paused_cleanup(_lease: dict) -> dict[str, object]:
        cleanup_started.set()
        assert release_cleanup.wait(timeout=5)
        return {"status": "closed", "worker_pane": None, "verified_absent": True}

    monkeypatch.setattr(recruiter, "_runner_alive", lambda _pid, _key: False)
    monkeypatch.setattr(recruiter, "_cleanup_lease_panes", paused_cleanup)

    def reconcile() -> None:
        try:
            recruiter._reconcile_claim(ledger, key, lease, force=True)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=reconcile)
    thread.start()
    assert cleanup_started.wait(timeout=5)
    second = _order(
        order_id="phase-0.stage-2-adversarial-audit.pass-1.try-1",
        stage_id="stage-2-adversarial-audit",
        result_path=str(tmp_path / "second-result.json"),
    )
    second_key, created = ledger.submit(second)
    assert created is True
    assert ledger.request_dir(second_key).is_dir()
    release_cleanup.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


def test_reconciler_refuses_recorded_pane_without_recorded_session(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    ledger = recruiter.JobLedger()
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000, owner={"runner_pid": 999})
    assert token
    assert ledger.record_worker(key, token, "owned-worker", "cockpit")
    staging = ledger.result_staging_path(key, token)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(json.dumps(_result(order["order_id"])))
    monkeypatch.setattr(recruiter, "_runner_alive", lambda pid, candidate_key: False)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: pytest.fail("ambiguous lease must not close a pane"),
    )

    assert recruiter.cmd_reconcile(force=True) == 0
    lease = json.loads((ledger.active / "requests" / key / "lease.json").read_text())
    assert lease["worker_pane"] == "owned-worker"


def test_cleanup_failure_keeps_owned_lease_until_reconciler_verifies_absence(
    tmp_path: Path,
) -> None:
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

    assert _finalize(
        ledger,
        key,
        token,
        order,
        _result(order["order_id"], verdict="blocked"),
        cleanup=cleanup,
    )

    assert (ledger.active / "requests" / key / "lease.json").is_file()
    assert ledger.completed_receipt(key, order)["state"] == "cleanup-failed"


def test_compatibility_order_gets_required_manifest_metadata_before_submit(
    tmp_path: Path,
) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))

    normalized = recruiter._strict_order(str(order_path))

    publication = normalized["artifact_publication"]
    assert publication["mandatory_consults"] == []
    assert Path(publication["compacted_path"]).is_absolute()
    assert Path(publication["handoff_path"]).is_absolute()
    assert json.loads(order_path.read_text()) == normalized


def test_worker_cleanup_failure_replaces_the_entire_success_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_path = tmp_path / "public/result.json"
    private_result = tmp_path / "private/result.json"
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(result_path=str(result_path), instructions_path=str(instructions))
    recruiter.completion.ensure_publication_contract(order)
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')
    manifest = _manifest_for_private(order, private_result)

    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: ("worker-pane", "workspace", "worker-address"),
    )
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )

    def finish(*args: object, **kwargs: object) -> bool:
        _write_typed_worker_result(private_result, _result(order["order_id"]))
        return True

    monkeypatch.setattr(recruiter, "_wait_for_agent_status", finish)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("worker close transport failed")
        ),
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        before_worker_cleanup=lambda: False,
        herdr_session="llm-lab-test",
        artifact_manifest=manifest,
    )

    assert code == 1 and result["verdict"] == "blocked"
    assert cleanup["verified_absent"] is False
    for kind in ("compacted", "handoff"):
        text = manifest.artifact(kind).staging_path.read_text().lower()
        assert "blocked" in text and "worker close transport failed" in text
        assert "worker compacted evidence" not in text


def test_run_order_repairs_a_malformed_finished_worker_bundle_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_path = tmp_path / "public/result.json"
    private_result = tmp_path / "private/result.json"
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(result_path=str(result_path), instructions_path=str(instructions))
    recruiter.completion.ensure_publication_contract(order)
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')
    manifest = _manifest_for_private(order, private_result)
    ledger = recruiter.JobLedger(tmp_path / "hub")
    key, _created = ledger.submit(order)

    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: ("worker-pane", "workspace", "worker-address"),
    )
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )

    def finish(*args: object, **kwargs: object) -> bool:
        private_result.parent.mkdir(parents=True, exist_ok=True)
        private_result.write_text(
            json.dumps({"order_id": order["order_id"], "full_log": "worker-session"})
        )
        manifest.artifact("compacted").staging_path.write_text("# Compact\n")
        manifest.artifact("handoff").staging_path.write_text("# Handoff\n")
        return True

    repairs: list[str] = []

    def repair(address: str, prompt: str, **kwargs: object) -> None:
        repairs.append(address)
        assert "COMPLETION_REPAIR 1/1" in prompt
        private_result.write_text(json.dumps(_result(order["order_id"])))

    monkeypatch.setattr(recruiter, "_wait_for_agent_status", finish)
    monkeypatch.setattr(recruiter, "_submit_agent_prompt", repair)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: {
            "status": "closed",
            "worker_pane": pane,
            "verified_absent": True,
        },
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        # The reactor now also reports salvage provenance; this callback wants only the
        # "Python authored a bundle" flag.
        before_worker_cleanup=lambda: recruiter._complete_typed_bundle(
            ledger,
            key,
            order,
            manifest,
            "worker-address",
            herdr_session="llm-lab-test",
        )[0],
        herdr_session="llm-lab-test",
        artifact_manifest=manifest,
    )

    assert code == 0
    assert result["verdict"] == "passed"
    assert repairs == ["worker-address"]
    assert cleanup["verified_absent"] is True


def test_manager_cleanup_failure_replaces_the_entire_success_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    order = _order(
        result_path=str(tmp_path / "public/result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)

    monkeypatch.setattr(
        recruiter,
        "inspect_worker_configuration",
        lambda *args, **kwargs: {"errors": []},
    )
    monkeypatch.setattr(
        recruiter,
        "_direct_manager",
        lambda *args, **kwargs: {
            "address": None,
            "config": args[0],
            "generation": 1,
            "health": None,
            "herdr_session": "llm-lab-test",
            "pane": "manager-pane",
            "workspace_id": None,
        },
    )

    def run_order(
        *args: object, **kwargs: object
    ) -> tuple[int, dict, dict[str, object]]:
        manifest = kwargs["artifact_manifest"]
        _write_typed_worker_result(
            manifest.artifact("result").staging_path, _result(order["order_id"])
        )
        return 0, _result(order["order_id"]), _cleanup("worker-pane")

    monkeypatch.setattr(recruiter, "_run_order", run_order)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("manager close transport failed")
        ),
    )
    monkeypatch.setattr(recruiter, "_notify_requester", lambda *args, **kwargs: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1
    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "blocked"
    for field in ("compacted_path", "handoff_path"):
        text = Path(order["artifact_publication"][field]).read_text().lower()
        assert "blocked" in text and "manager close transport failed" in text
        assert "worker compacted evidence" not in text


def test_typed_publication_receipt_precedes_terminal_event(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "public/result.json"))
    order["artifact_publication"] = {
        "schema_version": 1,
        "compacted_path": str(tmp_path / "public/compacted.md"),
        "handoff_path": str(tmp_path / "public/handoff.md"),
        "mandatory_consults": [],
    }
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    manifest = recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    assert manifest is not None
    recruiter.completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    manifest.artifact("result").staging_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.artifact("result").staging_path.write_text(
        json.dumps(_result(order["order_id"]))
    )
    manifest.artifact("compacted").staging_path.write_text("# Compact\n")
    manifest.artifact("handoff").staging_path.write_text("# Handoff\n")
    original_event = ledger._event
    terminal_events: list[str] = []

    def event(key_arg: str, event_name: str, **detail: object) -> None:
        if event_name == "finished":
            assert (ledger.request_dir(key) / "receipt.json").is_file()
            for artifact in manifest.artifacts:
                assert artifact.public_path.is_file()
            terminal_events.append(event_name)
        original_event(key_arg, event_name, **detail)

    monkeypatch.setattr(ledger, "_event", event)
    assert _finalize(
        ledger, key, token, order, _result(order["order_id"]), cleanup=_cleanup()
    )
    assert terminal_events == ["finished"]


def test_mandatory_consult_without_cited_hub_receipt_blocks_finalization(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "public/result.json"))
    order["artifact_publication"] = {
        "schema_version": 1,
        "compacted_path": str(tmp_path / "public/compacted.md"),
        "handoff_path": str(tmp_path / "public/handoff.md"),
        "mandatory_consults": [
            {"consult_id": "required-consult", "specialist": "api-reviewer"}
        ],
    }
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    manifest = recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    assert manifest is not None
    recruiter.completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    manifest.artifact("result").staging_path.parent.mkdir(parents=True, exist_ok=True)
    _write_typed_worker_result(
        manifest.artifact("result").staging_path,
        _result(order["order_id"], verdict="passed"),
    )
    manifest.artifact("compacted").staging_path.write_text("# Compact\n")
    manifest.artifact("handoff").staging_path.write_text("# Handoff\n")

    assert _finalize(
        ledger, key, token, order, _result(order["order_id"]), cleanup=_cleanup()
    )
    published = json.loads(Path(order["result_path"]).read_text())
    assert published["verdict"] == "blocked"
    assert "mandatory consultation gate" in published["reason"]
    assert ledger.completed_receipt(key, order)["verdict"] == "blocked"


def test_publication_fault_writes_no_receipt_or_terminal_event(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "public/result.json"))
    order["artifact_publication"] = {
        "schema_version": 1,
        "compacted_path": str(tmp_path / "public/compacted.md"),
        "handoff_path": str(tmp_path / "public/handoff.md"),
        "mandatory_consults": [],
    }
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    manifest = recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    assert manifest is not None
    recruiter.completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    for artifact in manifest.artifacts:
        artifact.staging_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.artifact("result").staging_path.write_text(
        json.dumps(_result(order["order_id"]))
    )
    manifest.artifact("compacted").staging_path.write_text("# Compact\n")
    manifest.artifact("handoff").staging_path.write_text("# Handoff\n")
    terminal_events: list[str] = []
    original_event = ledger._event

    def event(key_arg: str, event_name: str, **detail: object) -> None:
        if event_name in ("finished", "cleanup-failed"):
            terminal_events.append(event_name)
        original_event(key_arg, event_name, **detail)

    monkeypatch.setattr(ledger, "_event", event)
    monkeypatch.setattr(
        recruiter.completion,
        "project_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            recruiter.CompletionError("injected projection fault")
        ),
    )
    with pytest.raises(recruiter.CompletionError, match="projection fault"):
        _finalize(
            ledger, key, token, order, _result(order["order_id"]), cleanup=_cleanup()
        )
    assert not (ledger.request_dir(key) / "receipt.json").exists()
    assert terminal_events == []
    assert ledger.state(key)["state"] == "claimed"


def test_worker_ownership_is_recorded_before_startup_health_is_reported(
    tmp_path: Path, monkeypatch
) -> None:
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(
        instructions_path=str(instructions), result_path=str(tmp_path / "result.json")
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text('harnesses:\n  claude: "worker write:{result_path}"\n')
    private_result = tmp_path / "hub/results/token.json"
    recorded = threading.Event()
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda name, execution_order, launch, **kwargs: (
            "worker-pane",
            "cockpit",
            name,
        ),
    )

    def on_worker(
        worker_pane: str, workspace_id: str | None, worker_address: str
    ) -> threading.Event:
        assert worker_pane == "worker-pane" and workspace_id == "cockpit"
        assert worker_address
        recorded.set()
        return threading.Event()

    def healthy(*args: object, **kwargs: object) -> dict:
        assert recorded.is_set()
        private_result.write_text(json.dumps(_result(order["order_id"])))
        return {"healthy": True}

    monkeypatch.setattr(recruiter, "_wait_for_worker_health", healthy)
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(
        recruiter, "_wait_for_agent_status", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        on_worker,
        herdr_session="llm-lab-test",
        artifact_manifest=_manifest_for_private(order, private_result),
    )

    assert (
        code == 0
        and result["verdict"] == "passed"
        and cleanup["verified_absent"] is True
    )


def test_account_manager_is_health_checked_and_durably_addressed_before_approval(
    tmp_path: Path, monkeypatch
) -> None:
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / "result.json"),
    )
    ledger = recruiter.JobLedger(tmp_path / "hub")
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    decision = recruiter.lifecycle.ManagerDecision(
        recruiter.lifecycle.request_identity(order),
        1,
        "approved",
        "Configuration is coherent.",
    )
    launch_orders: list[dict] = []
    launch_directions: list[str] = []
    launch_tabs: list[str | None] = []
    resize_calls: list[tuple[str, str, float, str]] = []

    def start_manager(
        name: str,
        launch_order: dict,
        command: str,
        *,
        split_direction: str = "right",
        tab_role: str | None = None,
        herdr_session: str | None = None,
    ) -> tuple[str, str, str]:
        launch_orders.append(launch_order)
        launch_directions.append(split_direction)
        launch_tabs.append(tab_role)
        return "manager-pane", "cockpit-workspace", name

    monkeypatch.setattr(recruiter, "_start_herdr_agent", start_manager)

    def resize_manager(
        pane: str,
        *,
        split_direction: str,
        target_fraction: float,
        role: str,
        herdr_session: str | None = None,
    ) -> None:
        assert herdr_session == "llm-lab-test"
        lease = json.loads(
            (ledger.active / "requests" / key / "lease.json").read_text()
        )
        assert lease["manager_pane"] == pane
        resize_calls.append((pane, split_direction, target_fraction, role))

    monkeypatch.setattr(recruiter, "_resize_started_pane", resize_manager)
    monkeypatch.setattr(
        recruiter, "_wait_for_agent_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_wait_typed_file", lambda *args, **kwargs: decision)
    monkeypatch.setattr(recruiter, "_notify_requester", lambda *args, **kwargs: None)

    manager = recruiter._start_account_manager(
        ledger, key, token, order, _roster(), herdr_session="llm-lab-test"
    )

    lease = json.loads((ledger.active / "requests" / key / "lease.json").read_text())
    assert manager["decision"].decision == "approved"
    assert lease["manager_pane"] == "manager-pane"
    assert lease["manager_address"].startswith("upagent-manager-")
    assert lease["manager_workspace_id"] == "cockpit-workspace"
    assert launch_orders[0]["cockpit_pane"] == order["cockpit_pane"]
    assert launch_directions == ["down"]
    assert launch_tabs == ["oversight"]
    assert resize_calls == [("manager-pane", "down", 0.20, "account manager")]


def test_account_manager_crash_degrades_supervision_but_worker_still_terminalizes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Do the stage.\n")
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(result_path),
        management={"mode": "dedicated"},
    )
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    execution_orders: list[dict] = []
    messages: list[str] = []

    monkeypatch.setattr(
        recruiter,
        "_start_account_manager",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("manager process crashed")
        ),
    )

    def start_agent(
        ledger_arg: object,
        key_arg: str,
        token: str,
        role: str,
        name: str,
        execution_order: dict,
        launch: str,
        **kwargs: object,
    ) -> tuple[str, str, str, str]:
        assert role == "worker"
        execution_orders.append(execution_order)
        return "worker-pane", "workspace", "worker-address", "launch-id"

    def health(*args: object, **kwargs: object) -> dict[str, object]:
        staging = Path(execution_orders[0]["result_path"])
        _write_typed_worker_result(staging, _result(order["order_id"]))
        return {"healthy": True}

    monkeypatch.setattr(recruiter, "_start_fenced_ledger_agent", start_agent)
    monkeypatch.setattr(recruiter, "_wait_for_worker_health", health)
    monkeypatch.setattr(
        recruiter, "_wait_for_agent_status", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(
        recruiter.JobLedger, "mark_launch_closed", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        recruiter,
        "_notify_requester",
        lambda ledger, key, order, generation, message_type, *args, **kwargs: (
            messages.append(message_type)
        ),
    )

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0
    assert json.loads(result_path.read_text())["verdict"] == "passed"
    assert "account-manager-degraded" in messages
    assert "terminal" in messages
    assert f"ORDER {order['order_id']} DONE" in capsys.readouterr().out


def test_explicit_shared_manager_placement_uses_recruiter_pane(monkeypatch) -> None:
    order = _order(manager_placement={"mode": "shared"})
    monkeypatch.setattr(
        recruiter, "_recruiter_pane_from_state", lambda: "recruiter-pane"
    )

    assert recruiter._manager_anchor_pane(order) == "recruiter-pane"


def test_file_mailbox_requester_receives_correlated_lifecycle_message(
    tmp_path: Path, monkeypatch
) -> None:
    mailbox = tmp_path / "external-mailbox"
    order = _order(
        result_path=str(tmp_path / "result.json"),
        requester={"id": "external", "kind": "file-mailbox", "address": str(mailbox)},
    )
    ledger = recruiter.JobLedger(tmp_path / "hub")
    key, _ = ledger.submit(order)
    monkeypatch.setattr(
        recruiter, "_herdr", lambda *args: (_ for _ in ()).throw(AssertionError(args))
    )

    recruiter._notify_requester(
        ledger, key, order, 1, "worker-healthy", "Worker is healthy."
    )

    messages = recruiter.lifecycle.RequestMailbox(mailbox).read_all()
    assert messages[0]["type"] == "worker-healthy"
    assert messages[0]["request_id"] == recruiter.lifecycle.request_identity(order)


def test_completion_monitor_returns_runner_promptly_after_promoting_stuck_status_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
        timeout_ms=1_000,
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_launched = threading.Event()
    staging_paths: list[Path] = []
    closed_panes: list[str] = []
    worker_closed = threading.Event()
    outcomes: list[int] = []
    _patch_approved_manager(monkeypatch)

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        staging_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        worker_launched.set()
        return "worker-pane", "cockpit", name

    def fake_close(pane: str, **kwargs: object) -> dict:
        closed_panes.append(pane)
        worker_closed.set()
        return _cleanup(pane)

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", fake_close)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail(
            "interactive completion must not subscribe to turn-level done"
        ),
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)
    # The startup-activity watcher is unit-tested on its own; a live probe here would
    # only trip the strict Popen stub above from a background thread.
    monkeypatch.setattr(recruiter, "_watch_first_action", lambda *args, **kwargs: None)

    runner = threading.Thread(
        target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path)))
    )
    runner.start()
    assert worker_launched.wait(timeout=2)
    staging_paths[0].parent.mkdir(parents=True, exist_ok=True)
    _write_typed_worker_result(staging_paths[0], _result(order["order_id"]))
    deadline = time.monotonic() + 2
    while not result_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(result_path.read_text()) == _result(order["order_id"])
    assert worker_closed.wait(timeout=2)
    assert closed_panes == ["worker-pane", "manager-pane"]

    promoted_at = time.monotonic()
    runner.join(timeout=1)
    assert not runner.is_alive()
    assert time.monotonic() - promoted_at < 0.5
    assert outcomes == [0]
    assert capsys.readouterr().out.count(f"ORDER {order['order_id']} DONE\n") == 1
    assert closed_panes == ["worker-pane", "manager-pane"]


def test_codex_worker_survives_missing_startup_assessment_and_promotes_private_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
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
    notifications: list[str] = []
    _patch_approved_manager(monkeypatch)
    monkeypatch.setattr(
        recruiter,
        "_ask_manager_about_startup",
        lambda *args: (_ for _ in ()).throw(
            RecruiterError("assessment request_id mismatch")
        ),
    )
    monkeypatch.setattr(
        recruiter,
        "_notify_requester",
        lambda ledger, key, order, generation, message_type, *args, **kwargs: (
            notifications.append(message_type)
        ),
    )

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        assert launch.startswith(
            "codex exec --dangerously-bypass-approvals-and-sandbox"
        )
        assert "--model gpt-5.6-sol" in launch
        assert "model_reasoning_effort=high" in launch
        staging_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        worker_launched.set()
        return "codex-worker-pane", "cockpit", name

    def fake_close(pane: str, **kwargs: object) -> dict:
        closed_panes.append(pane)
        return _cleanup(pane)

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
        assert command[:4] == ["herdr", "--session", "llm-lab-test", "agent"]
        assert command[4] == "wait"
        status_wait_started.set()
        return NeverDoneProcess()

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", fake_close)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(recruiter.subprocess, "Popen", never_done)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)
    # The startup-activity watcher is unit-tested on its own; a live probe here would
    # only trip the strict `never_done` command-shape stub from a background thread.
    monkeypatch.setattr(recruiter, "_watch_first_action", lambda *args, **kwargs: None)

    runner = threading.Thread(
        target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path)))
    )
    runner.start()
    assert worker_launched.wait(timeout=2)
    assert status_wait_started.wait(timeout=2)
    assert staging_paths[0] != result_path
    staging_paths[0].parent.mkdir(parents=True, exist_ok=True)
    _write_typed_worker_result(staging_paths[0], _result(order["order_id"]))

    deadline = time.monotonic() + 2
    while not result_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    runner.join(timeout=1)

    assert not runner.is_alive()
    assert outcomes == [0]
    assert json.loads(result_path.read_text()) == _result(order["order_id"])
    assert capsys.readouterr().out.count(f"ORDER {order['order_id']} DONE\n") == 1
    assert closed_panes == ["codex-worker-pane", "manager-pane"]
    events = [
        json.loads(path.read_text())
        for path in (ledger.request_dir(key) / "events").glob("*.json")
    ]
    degraded = [
        event
        for event in events
        if event.get("event") == "worker-startup-assessment-degraded"
    ]
    assert len(degraded) == 1
    assert degraded[0]["reason"] == "assessment request_id mismatch"
    assert "worker-healthy-degraded" in notifications


def test_run_job_keeps_worker_result_when_status_wait_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_result_paths: list[Path] = []
    _patch_approved_manager(monkeypatch)

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        worker_result_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        return "worker-pane", "cockpit", name

    def fail_wait(*args: object, **kwargs: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        _write_typed_worker_result(
            worker_result_paths[0], _result(order["order_id"], verdict="passed")
        )
        raise recruiter.RecruiterError("wait transport failed")

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", fail_wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    output = capsys.readouterr()
    assert f"ORDER {order['order_id']} DONE" in output.out
    assert "kept existing worker result" in output.err
    assert json.loads(result_path.read_text()) == _result(
        order["order_id"], verdict="passed"
    )


def test_cleanup_waits_for_post_notification_runner_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    result_path = tmp_path / "result.json"
    payload_sha256 = "a" * 64
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
        public_request={"payload_sha256": payload_sha256},
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    worker_result_paths: list[Path] = []
    notification_started = threading.Event()
    release_notification = threading.Event()
    outcomes: list[int] = []
    _patch_approved_manager(monkeypatch)

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        worker_result_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        return "worker-pane", "cockpit", name

    def wait(*args: object, **kwargs: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        _write_typed_worker_result(
            worker_result_paths[0], _result(order["order_id"], verdict="passed")
        )
        return True

    def notify(
        _ledger: object,
        _key: str,
        _order: dict,
        _generation: int,
        message_type: str,
        _message: str,
        _detail: dict | None = None,
    ) -> None:
        if message_type == "terminal":
            notification_started.set()
            assert release_notification.wait(timeout=5)

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    monkeypatch.setattr(recruiter, "_live_pane_ids", lambda **_kwargs: set())
    monkeypatch.setattr(recruiter, "_notify_requester", notify)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    runner = threading.Thread(
        target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path)))
    )
    runner.start()
    assert notification_started.wait(timeout=5)
    assert (ledger.request_dir(key) / "receipt.json").is_file()
    assert not (ledger.request_dir(key) / "runner-completed.json").exists()
    request_id = recruiter.lifecycle.request_identity(order)
    tombstone = {
        "request_id": request_id,
        "payload_sha256": payload_sha256,
        "terminal_verdict": "passed",
    }
    with pytest.raises(RecruiterError, match="runner-completed"):
        ledger.prune_terminal(
            key,
            request_id,
            payload_sha256,
            tombstone,
            verify_absence=recruiter._verify_terminal_cleanup_absence,
        )
    release_notification.set()
    runner.join(timeout=5)
    assert not runner.is_alive()
    assert outcomes == [0]
    evidence = ledger.terminal_cleanup_evidence(key, request_id, payload_sha256)
    assert evidence["runner_completed"]["source"] == "supervisor"
    pruned = ledger.prune_terminal(
        key,
        request_id,
        payload_sha256,
        tombstone,
        verify_absence=recruiter._verify_terminal_cleanup_absence,
    )
    assert pruned["already_pruned"] is False
    assert (ledger.request_dir(key) / "tombstone.json").is_file()


def test_expired_owner_cannot_finalize_before_replacement_claims(
    tmp_path: Path,
) -> None:
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

    assert not _finalize(
        ledger,
        key,
        expired_token,
        order,
        _result(order["order_id"]),
        cleanup=_cleanup(),
    )
    assert not Path(order["result_path"]).exists()
    assert (
        json.loads((ledger.request_dir(key) / "state/latest.json").read_text())["state"]
        == "claimed"
    )
    assert (ledger.active / "requests" / key).is_dir()

    replacement_token = ledger.claim(key, order["order_id"], 1_000)
    assert replacement_token is not None and replacement_token != expired_token


def test_expired_lease_is_reclaimed_and_stale_index_cannot_remove_new_lease(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    order = _order()
    key, _ = ledger.submit(order)
    old_token = ledger.claim(key, order["order_id"], 1_000)
    assert old_token is not None
    old_lease_path = ledger.active / "requests" / key / "lease.json"
    expired_at = int(time.time()) - 1
    old_lease = {
        "order_id": order["order_id"],
        "token": old_token,
        "expires_at": expired_at,
    }
    ledger._write_json(old_lease_path, old_lease)
    ledger._write_json(
        ledger.active / "by-expiry" / str(expired_at) / f"{key}-{old_token}.json",
        old_lease,
    )

    new_token = ledger.claim(key, order["order_id"], 1_000)
    assert new_token is not None and new_token != old_token
    assert ledger.reap_expired(now=expired_at) == 0
    active_lease = json.loads(
        (ledger.active / "requests" / key / "lease.json").read_text()
    )
    assert active_lease["token"] == new_token
    assert not _finalize(
        ledger, key, old_token, order, _result(order["order_id"]), cleanup=_cleanup()
    )
    assert (ledger.active / "requests" / key).is_dir()


def test_stage_timeout_defaults_and_explicit_override() -> None:
    assert recruiter._default_timeout_ms("stage-1-implementation") == 10_800_000
    assert recruiter._default_timeout_ms("stage-2-adversarial-audit") == 10_800_000
    assert (
        recruiter._default_timeout_ms("stage-3-integration-acceptance-seams")
        == recruiter.DEFAULT_TIMEOUT_MS
    )
    assert _order(timeout_ms=123)["timeout_ms"] == 123


def test_run_order_rejects_cosmetic_result_before_done(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    result_path = tmp_path / "result.json"
    staging_path = tmp_path / "staging.json"
    order = _order(
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    order_path = tmp_path / "order.json"
    roster_path = tmp_path / "upagent.yaml"
    order_path.write_text(json.dumps(order))
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )

    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"pane": {"pane_id": "worker-pane"}}},
    )

    def fake_wait(*args: object, **kwargs: object) -> bool:
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
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        staging_path,
        herdr_session="llm-lab-test",
        artifact_manifest=_manifest_for_private(order, staging_path),
    )
    assert code == 1
    assert result["verdict"] == "blocked"
    assert json.loads(staging_path.read_text())["verdict"] == "blocked"
    assert not result_path.exists()
    assert "ORDER" not in capsys.readouterr().out
    assert cleanup["verified_absent"] is True


def test_duplicate_runner_start_failure_cannot_finalize_live_owner(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    assert ledger.claim(key, order["order_id"], 1_000) is not None

    def fail_runner(*args: object, **kwargs: object) -> None:
        raise OSError("runner thread unavailable")

    monkeypatch.setattr(recruiter, "_spawn_job", fail_runner)
    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 1

    assert not Path(order["result_path"]).exists()
    latest = json.loads((ledger.request_dir(key) / "state/latest.json").read_text())
    assert latest["state"] == "claimed"
    assert "DONE" not in capsys.readouterr().out


def test_session_resolution_failure_publishes_blocked_recruit_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(
        recruiter,
        "_herdr_owner_record",
        lambda: (_ for _ in ()).throw(RecruiterError("session unavailable")),
    )
    monkeypatch.setattr(
        recruiter,
        "_spawn_job",
        lambda *args, **kwargs: pytest.fail("unowned session must not launch a runner"),
    )

    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 1

    result = json.loads(Path(order["result_path"]).read_text())
    assert result["verdict"] == "blocked"
    assert "session unavailable" in result["reason"]
    assert f"ORDER {order['order_id']} DONE" in capsys.readouterr().out


def test_recovered_lease_fences_stale_runner_result_and_terminal_state(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    old_token = ledger.claim(key, order["order_id"], 1_000)
    assert old_token is not None
    expired_lease = {
        "order_id": order["order_id"],
        "token": old_token,
        "expires_at": int(time.time()) - 1,
    }
    ledger._write_json(ledger.active / "requests" / key / "lease.json", expired_lease)
    new_token = ledger.claim(key, order["order_id"], 1_000)
    assert new_token is not None and new_token != old_token

    assert not _finalize(
        ledger,
        key,
        old_token,
        order,
        _result(order["order_id"], verdict="passed"),
        cleanup=_cleanup(),
    )
    assert not Path(order["result_path"]).exists()
    assert _finalize(
        ledger,
        key,
        new_token,
        order,
        _result(order["order_id"], verdict="blocked"),
        cleanup=_cleanup(),
    )
    assert json.loads(Path(order["result_path"]).read_text())["verdict"] == "blocked"


def test_blocked_result_write_failure_leaves_request_nonterminal_and_silent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))

    def fail_runner(*args: object, **kwargs: object) -> None:
        raise OSError("runner thread unavailable")

    original_write_json = recruiter.JobLedger._write_json

    def fail_staging_write(path: Path, value: dict) -> None:
        if path.name == "result.json" and "artifacts" in path.parts:
            raise OSError("disk full")
        original_write_json(path, value)

    monkeypatch.setattr(recruiter, "_spawn_job", fail_runner)
    monkeypatch.setattr(
        recruiter.JobLedger, "_write_json", staticmethod(fail_staging_write)
    )
    with pytest.raises(OSError, match="disk full"):
        recruiter.cmd_recruit(str(order_path), "roster.yaml")

    ledger = recruiter.JobLedger()
    key = ledger.key_for_order(order)
    assert (
        json.loads((ledger.request_dir(key) / "state/latest.json").read_text())["state"]
        == "claimed"
    )
    assert not Path(order["result_path"]).exists()
    assert "DONE" not in capsys.readouterr().out


# --- coordination v2: direct lifecycle default and multiplexed awaits --------


def test_direct_lifecycle_runs_job_without_an_account_manager(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The default mode hires no standing manager LLM; Python owns the lifecycle."""
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)

    def no_manager(*args: object, **kwargs: object) -> dict:
        raise AssertionError("direct mode must not start an account manager")

    monkeypatch.setattr(recruiter, "_start_account_manager", no_manager)
    monkeypatch.setattr(recruiter, "_submit_agent_prompt", no_manager)
    worker_result_paths: list[Path] = []

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        worker_result_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        return "worker-pane", "cockpit", name

    def wait(*args: object, **kwargs: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        _write_typed_worker_result(
            worker_result_paths[0], _result(order["order_id"], verdict="passed")
        )
        return True

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    output = capsys.readouterr()
    assert f"ORDER {order['order_id']} DONE" in output.out
    assert json.loads(result_path.read_text())["verdict"] == "passed"
    healthy = [
        payload
        for _, payload in recruiter._mailbox_messages(ledger, key)
        if payload.get("type") == "worker-healthy"
    ]
    assert healthy and "direct lifecycle" in healthy[0]["message"]


def test_failed_launch_is_rescued_once_when_the_broker_advises_retry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """First launch explodes; the rescue broker says retry; the relaunch succeeds."""
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)

    attempts: list[str] = []
    worker_result_paths: list[Path] = []

    def flaky_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        attempts.append(name)
        if len(attempts) == 1:
            raise RecruiterError("pane split exploded")
        worker_result_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        return "worker-pane", "cockpit", name

    def wait(*args: object, **kwargs: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        _write_typed_worker_result(
            worker_result_paths[0], _result(order["order_id"], verdict="passed")
        )
        return True

    advice_calls: list[str] = []

    def advise(*args: object) -> str:
        advice_calls.append(str(args[-1]))
        return "retry-startup"

    monkeypatch.setattr(recruiter, "_startup_rescue_advice", advise)
    monkeypatch.setattr(recruiter, "_same_owner_process", lambda _journal: False)
    monkeypatch.setattr(recruiter, "_start_herdr_agent", flaky_start)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("agent_not_found")
        ),
    )
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    assert len(attempts) == 2, "exactly one rescue relaunch"
    assert advice_calls and "pane split exploded" in advice_calls[0]
    assert json.loads(result_path.read_text())["verdict"] == "passed"
    rescue = [
        payload
        for _, payload in recruiter._mailbox_messages(ledger, key)
        if payload.get("type") == "startup-rescue"
    ]
    assert rescue, "the requester must hear about the rescue relaunch"


def test_failed_launch_is_not_retried_when_the_broker_declines(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)

    attempts: list[str] = []

    def dead_start(*args: object, **kwargs: object) -> tuple[str, str, str]:
        attempts.append("try")
        raise RecruiterError("model flag rejected by the harness")

    monkeypatch.setattr(recruiter, "_startup_rescue_advice", lambda *a: "ask-requester")
    monkeypatch.setattr(recruiter, "_same_owner_process", lambda _journal: False)
    monkeypatch.setattr(recruiter, "_start_herdr_agent", dead_start)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RecruiterError("agent_not_found")
        ),
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    assert len(attempts) == 1, "a declined rescue must not relaunch"
    assert json.loads(result_path.read_text())["verdict"] == "blocked"
    declined = [
        payload
        for _, payload in recruiter._mailbox_messages(ledger, key)
        if payload.get("type") == "startup-rescue-declined"
    ]
    assert declined and declined[0]["detail"]["advice"] == "ask-requester"


def test_order_can_pin_a_dedicated_manager_when_the_roster_default_is_direct(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Sentinel supervision is covered in sentinel_test.py; this test's worker-shaped
    # herdr stubs must see only the launches it fakes.
    monkeypatch.setattr(recruiter, "_sentinel_enabled", lambda order: False)
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
        management={"mode": "dedicated"},
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)

    manager_calls: list[str] = []

    def fake_manager(
        ledger_arg: object,
        key_arg: str,
        token_arg: str,
        order_arg: dict,
        roster_arg: dict,
        generation_arg: int,
        validation_arg: dict,
        **kwargs: object,
    ) -> dict[str, object]:
        assert kwargs == {"herdr_session": "llm-lab-test"}
        manager_calls.append(order_arg["order_id"])
        config = recruiter.llm_management.load_management_config(roster_arg)
        return {
            "address": None,
            "config": config,
            "decision": recruiter.lifecycle.ManagerDecision(
                request_id=recruiter.lifecycle.request_identity(order_arg),
                generation=generation_arg,
                decision="approved",
                message="ok",
            ),
            "generation": generation_arg,
            "herdr_session": "llm-lab-test",
            "health": None,
            "pane": None,
            "workspace_id": None,
        }

    worker_result_paths: list[Path] = []

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        worker_result_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        return "worker-pane", "cockpit", name

    def wait(*args: object, **kwargs: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        _write_typed_worker_result(
            worker_result_paths[0], _result(order["order_id"], verdict="passed")
        )
        return True

    monkeypatch.setattr(recruiter, "_start_account_manager", fake_manager)
    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    assert manager_calls == [order["order_id"]], (
        "a dedicated-pinned order must hire the account manager even on a direct roster"
    )


def test_completion_ping_fires_only_past_the_threshold() -> None:
    calls: list[tuple[str, str]] = []

    def spy(order_id: str, verdict: str) -> None:
        calls.append((order_id, verdict))

    assert not recruiter._maybe_notify_completion(1_000, 600_000, "o1", "passed", spy)
    assert calls == []
    assert recruiter._maybe_notify_completion(600_000, 600_000, "o1", "passed", spy)
    assert calls == [("o1", "passed")]
    # Zero disables the ping entirely, however long the wait was.
    assert not recruiter._maybe_notify_completion(9_999_999, 0, "o1", "failed", spy)
    assert calls == [("o1", "passed")]


def test_await_threads_the_ping_threshold_through(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    assert _finalize(
        ledger,
        key,
        token,
        order,
        _result(order["order_id"]),
        cleanup=_cleanup(),
        exit_code=0,
    )

    seen: list[tuple[int, str, str]] = []

    def spy(
        waited_ms: float, threshold_ms: int, order_id: str, verdict: str, **_: object
    ) -> bool:
        seen.append((threshold_ms, order_id, verdict))
        return False

    monkeypatch.setattr(recruiter, "_maybe_notify_completion", spy)

    assert recruiter.cmd_await(str(order_path), notify_after_ms=123) == 0

    assert seen == [(123, order["order_id"], "passed")]
    assert "ORDER_RECEIPT" in capsys.readouterr().out


def _dead_runner_claim(
    tmp_path: Path, ledger: Any, suffix: str
) -> tuple[dict, Path, str]:
    instructions = tmp_path / f"instructions-{suffix}.md"
    instructions.write_text("# Worker\n")
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / f"result-{suffix}.json"),
    )
    order_path = tmp_path / f"order-{suffix}.json"
    order_path.write_text(json.dumps(order))
    key, _created = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "request_id": recruiter.lifecycle.request_identity(order),
            "runner_pid": -1,
            "runner_start_time": None,
        },
    )
    assert isinstance(token, str)
    return order, order_path, key


def test_await_reconciles_dead_runner_before_timeout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, order_path, key = _dead_runner_claim(tmp_path, ledger, "await")
    # Deadline-proven: the never-started reconciliation below requires this event.
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane",
        attempt=1,
    )

    assert recruiter.cmd_await(str(order_path), notify_after_ms=0) == 1

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    receipt = json.loads(lines[0].removeprefix("ORDER_RECEIPT "))
    assert receipt["request_id"] == recruiter.lifecycle.request_identity(order)
    # An empty dead-runner claim with no recorded first action reconciles as the typed
    # `never-started` terminal (startup-marker gate), not generic blocked.
    assert receipt["verdict"] == "never-started"
    assert (ledger.request_dir(key) / "runner-completed.json").is_file()
    assert not (ledger.active / "requests" / key).exists()


def test_verify_builds_an_independent_reviewer_order(
    tmp_path: Path, monkeypatch
) -> None:
    """The optional second opinion points a fresh reviewer at the finished work."""
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    _write_typed_worker_result(
        result_path, _result(order["order_id"], verdict="passed")
    )

    dispatched: list[str] = []

    def fake_dispatch(path: str, roster: str) -> int:
        dispatched.append(path)
        return 0

    monkeypatch.setattr(recruiter, "cmd_dispatch", fake_dispatch)

    assert recruiter.cmd_verify(str(order_path), "roster.yaml", model="some-model") == 0

    assert len(dispatched) == 1
    verify_order = json.loads(Path(dispatched[0]).read_text())
    assert verify_order["order_id"] == f"{order['order_id']}.verify"
    assert verify_order["stage_id"] == "stage-2-adversarial-audit"
    assert verify_order["agent"] == "reviewer"
    assert verify_order["cwd"] == order["cwd"]
    brief = Path(verify_order["instructions_path"]).read_text()
    assert order["result_path"] in brief
    assert "Adversarially check" in brief
    # The reviewer order must itself satisfy the order contract.
    recruiter.load_order(dispatched[0])


def test_verify_refuses_an_unfinished_order(tmp_path: Path) -> None:
    order = _order(
        cwd=str(tmp_path),
        result_path=str(tmp_path / "missing-result.json"),
        instructions_path=str(tmp_path / "instructions.md"),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))

    with pytest.raises(recruiter.ContractError):
        recruiter.cmd_verify(str(order_path), "roster.yaml")


def test_rescue_advice_survives_a_broken_filesystem(
    tmp_path: Path, monkeypatch
) -> None:
    """The real helper must never raise: a dead disk degrades to 'retry once anyway'."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    order = _order(cwd=str(tmp_path))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    manager = {
        "generation": 1,
        "config": recruiter.llm_management.load_management_config({}),
        "herdr_session": "llm-lab-test",
        "pane": None,
    }

    def broken_write(path: object, payload: object) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(recruiter.JobLedger, "_write_json", staticmethod(broken_write))

    advice = recruiter._startup_rescue_advice(
        ledger, key, order, manager, "pane split exploded"
    )

    assert advice == "retry-startup"


def test_rescue_advice_returns_the_brokers_recommendation(
    tmp_path: Path, monkeypatch
) -> None:
    """Exercise the real body: spawn, health, typed assessment, pane closed."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    order = _order(cwd=str(tmp_path))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 60_000)
    assert token
    manager = {
        "generation": 1,
        "config": recruiter.llm_management.load_management_config({}),
        "herdr_session": "llm-lab-test",
        "lease_token": token,
        "pane": None,
    }
    closed: list[str] = []
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda name, o, command, **kwargs: ("rescue-pane", "ws", "addr"),
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_health", lambda *a, **k: None)
    monkeypatch.setattr(
        recruiter,
        "_wait_typed_file",
        lambda path, timeout, parse: SimpleNamespace(
            assessment="startup-failed", recommended_action="ask-requester"
        ),
    )
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: closed.append(pane) or _cleanup(pane),
    )

    advice = recruiter._startup_rescue_advice(
        ledger, key, order, manager, "harness rejected the model flag"
    )

    assert advice == "ask-requester"
    assert closed == ["rescue-pane"], "the rescue pane must always be closed"


def test_manager_startup_advisory_cannot_reject_python_valid_worker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A manager observation cannot veto Python-verified startup or cause a relaunch."""
    result_path = tmp_path / "result.json"
    order = _order(
        cwd=str(tmp_path),
        result_path=str(result_path),
        instructions_path=str(tmp_path / "instructions.md"),
        management={"mode": "dedicated"},
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)

    def fake_manager(*args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs == {"herdr_session": "llm-lab-test"}
        roster_arg = args[4]
        return {
            "address": "manager-address",
            "config": recruiter.llm_management.load_management_config(roster_arg),
            "decision": recruiter.lifecycle.ManagerDecision(
                request_id=recruiter.lifecycle.request_identity(order),
                generation=1,
                decision="approved",
                message="ok",
            ),
            "generation": 1,
            "herdr_session": "llm-lab-test",
            "health": None,
            "pane": "manager-pane",
            "workspace_id": "ws",
        }

    def no_rescue(*args: object) -> str:
        raise AssertionError("a manager rejection must never reach the rescue broker")

    execution_orders: list[dict] = []

    def start_worker(
        name: str, execution_order: dict, command: str, **kwargs: object
    ) -> tuple[str, str, str]:
        execution_orders.append(execution_order)
        return "worker-pane", "cockpit", name

    def healthy(*args: object, **kwargs: object) -> dict[str, object]:
        staging = Path(execution_orders[0]["result_path"])
        _write_typed_worker_result(staging, _result(order["order_id"]))
        return {"healthy": True}

    monkeypatch.setattr(recruiter, "_start_account_manager", fake_manager)
    monkeypatch.setattr(recruiter, "_startup_rescue_advice", no_rescue)
    monkeypatch.setattr(recruiter, "_start_herdr_agent", start_worker)
    monkeypatch.setattr(recruiter, "_wait_for_worker_health", healthy)
    monkeypatch.setattr(
        recruiter, "_wait_for_agent_status", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        recruiter,
        "_ask_manager_about_startup",
        lambda *args: SimpleNamespace(
            assessment="startup-failed", message="wrong model requested"
        ),
    )
    monkeypatch.setattr(
        recruiter, "_close_worker_pane", lambda pane, **kwargs: _cleanup(pane)
    )
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    assert json.loads(result_path.read_text())["verdict"] == "passed"
    kinds = {
        payload.get("type") for _, payload in recruiter._mailbox_messages(ledger, key)
    }
    assert "worker-healthy-advisory" in kinds
    assert "startup-needs-requester" not in kinds
    assert "startup-rescue" not in kinds


def test_await_any_reports_terminal_receipt_tagged_with_request(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000)
    assert token
    assert _finalize(
        ledger,
        key,
        token,
        order,
        _result(order["order_id"]),
        cleanup=_cleanup(),
        exit_code=0,
    )

    assert recruiter.cmd_await_any([str(order_path)], timeout_ms=1_000) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("AWAIT_EVENT ")
    event = json.loads(line.removeprefix("AWAIT_EVENT "))
    assert event["kind"] == "completed"
    assert event["terminal"] is True
    assert event["request_id"] == recruiter.lifecycle.request_identity(order)
    assert event["receipt"]["verdict"] == "passed"


def test_await_any_reconciles_only_watched_dead_runner(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    watched, watched_path, watched_key = _dead_runner_claim(tmp_path, ledger, "watched")
    _unwatched, _unwatched_path, unwatched_key = _dead_runner_claim(
        tmp_path, ledger, "unwatched"
    )

    assert (
        recruiter.cmd_await_any(
            [str(watched_path)], timeout_ms=1_000, poll_seconds=0.01
        )
        == 0
    )

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0].removeprefix("AWAIT_EVENT "))
    assert event["kind"] == "blocked"
    assert event["request_id"] == recruiter.lifecycle.request_identity(watched)
    assert (ledger.request_dir(watched_key) / "runner-completed.json").is_file()
    assert not (ledger.request_dir(unwatched_key) / "receipt.json").exists()
    assert (ledger.active / "requests" / unwatched_key).is_dir()


def test_await_any_delivers_each_mailbox_message_once_via_cursor(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    key, _ = ledger.submit(order)
    request_id = recruiter.lifecycle.request_identity(order)
    ledger.publish_requester(key, request_id, 1, "worker-warning", "worker looks slow")

    assert recruiter.cmd_await_any([str(order_path)], timeout_ms=1_000) == 0
    first = json.loads(
        capsys.readouterr().out.strip().splitlines()[-1].removeprefix("AWAIT_EVENT ")
    )
    assert first["kind"] == "worker-warning"
    assert first["terminal"] is False
    assert first["cursor"] == {request_id: first["sequence"]}

    # Echoing the cursor back means the handled message never replays.
    assert (
        recruiter.cmd_await_any(
            [str(order_path)],
            timeout_ms=50,
            cursor_json=json.dumps(first["cursor"]),
            poll_seconds=0.01,
        )
        == 0
    )
    second = json.loads(
        capsys.readouterr().out.strip().splitlines()[-1].removeprefix("AWAIT_EVENT ")
    )
    assert second["kind"] == "await-heartbeat"


def test_await_any_rejects_duplicates_bad_cursor_and_unsubmitted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))

    with pytest.raises(recruiter.RecruiterError, match="has not been submitted"):
        recruiter.cmd_await_any([str(order_path)], timeout_ms=50)

    ledger.submit(order)
    with pytest.raises(recruiter.RecruiterError, match="more than once"):
        recruiter.cmd_await_any([str(order_path), str(order_path)], timeout_ms=50)
    with pytest.raises(recruiter.RecruiterError, match="cursor"):
        recruiter.cmd_await_any([str(order_path)], timeout_ms=50, cursor_json="[1]")
    with pytest.raises(recruiter.RecruiterError, match="at least one"):
        recruiter.cmd_await_any([], timeout_ms=50)


def test_report_state_surfaces_upagent_identity_in_herdr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(recruiter, "_herdr", lambda *args, **_: calls.append(args))

    recruiter._report_state("pane-1", "idle", "armed", herdr_session="llm-lab-test")

    assert calls == [
        (
            "pane",
            "report-agent",
            "pane-1",
            "--source",
            "upagent",
            "--agent",
            "upagent",
            "--state",
            "idle",
            "--message",
            "armed",
        )
    ]


def test_ensure_role_pane_defaults_to_the_upagent_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh bring-up creates an `upagent` workspace and matching visible role pane."""

    def fake_herdr_json(*args: str, **_: object) -> dict:
        if args[:2] == ("workspace", "list"):
            return {"result": {"workspaces": []}}
        if args[:2] == ("workspace", "create"):
            assert args[args.index("--label") + 1] == "upagent"
            return {
                "result": {
                    "workspace": {"workspace_id": "ws-upagent"},
                    "root_pane": {"pane_id": "pane-1"},
                }
            }
        raise AssertionError(f"unexpected herdr call: {args}")

    renames: list[tuple[str, ...]] = []
    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)
    monkeypatch.setattr(recruiter, "_herdr", lambda *a, **_: renames.append(a))

    workspace, pane, workspace_created, pane_created = recruiter._ensure_role_pane(
        recruiter.UPAGENT_PANE_LABEL,
        recruiter.UNIFIED_WORKSPACE_LABEL,
        "llm-lab-test",
    )

    assert recruiter.UNIFIED_WORKSPACE_LABEL == "upagent"
    assert recruiter.UPAGENT_PANE_LABEL == "upagent"
    assert (workspace, pane, workspace_created, pane_created) == (
        "ws-upagent",
        "pane-1",
        True,
        True,
    )
    assert ("pane", "rename", "pane-1", "upagent") in renames


def test_ensure_role_pane_migrates_legacy_visible_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bring-up renames retired labels in place instead of creating another workspace."""

    def fake_herdr_json(*args: str, **_: object) -> dict:
        if args[:2] == ("workspace", "list"):
            return {
                "result": {
                    "workspaces": [{"label": "herdr", "workspace_id": "ws-existing"}]
                }
            }
        if args[:2] == ("pane", "list"):
            return {
                "result": {
                    "panes": [{"pane_id": "pane-existing", "label": "recruiter"}]
                }
            }
        raise AssertionError(f"unexpected herdr call: {args}")

    renames: list[tuple[str, ...]] = []
    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)
    monkeypatch.setattr(recruiter, "_herdr", lambda *a, **_: renames.append(a))

    resolved = recruiter._ensure_role_pane(
        recruiter.UPAGENT_PANE_LABEL,
        recruiter.UNIFIED_WORKSPACE_LABEL,
        "llm-lab-test",
    )

    assert resolved == ("ws-existing", "pane-existing", False, False)
    assert ("workspace", "rename", "ws-existing", "upagent") in renames
    assert ("pane", "rename", "pane-existing", "upagent") in renames


def test_ensure_role_pane_rejects_duplicate_service_panes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_herdr_json(*args: str, **_: object) -> dict:
        if args[:2] == ("workspace", "list"):
            return {
                "result": {
                    "workspaces": [{"label": "upagent", "workspace_id": "ws-existing"}]
                }
            }
        if args[:2] == ("pane", "list"):
            return {
                "result": {
                    "panes": [
                        {"pane_id": "pane-current", "label": "upagent"},
                        {"pane_id": "pane-legacy", "label": "recruiter"},
                    ]
                }
            }
        raise AssertionError(f"unexpected herdr call: {args}")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)

    with pytest.raises(RecruiterError, match="multiple UpAgent service panes"):
        recruiter._ensure_role_pane(
            recruiter.UPAGENT_PANE_LABEL,
            recruiter.UNIFIED_WORKSPACE_LABEL,
            "llm-lab-test",
        )


def test_ensure_role_pane_rejects_switching_workspace_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Services already up under the other mode's label must fail loud, not split across two."""

    def fake_herdr_json(*args: str, **_: object) -> dict:
        if args[:2] == ("workspace", "list"):
            return {
                "result": {
                    "workspaces": [
                        {"label": "shared-services", "workspace_id": "ws-old"}
                    ]
                }
            }
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": [{"pane_id": "p1", "label": "upagent"}]}}
        raise AssertionError(f"unexpected herdr call: {args}")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)

    with pytest.raises(RecruiterError, match="upagent-down"):
        recruiter._ensure_role_pane(
            recruiter.UPAGENT_PANE_LABEL,
            recruiter.UNIFIED_WORKSPACE_LABEL,
            "llm-lab-test",
        )


def test_ensure_role_pane_separate_mode_reuses_shared_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--separate-workspaces keeps claiming the role pane in `shared-services` as before."""

    def fake_herdr_json(*args: str, **_: object) -> dict:
        if args[:2] == ("workspace", "list"):
            return {
                "result": {
                    "workspaces": [
                        {"label": "shared-services", "workspace_id": "ws-old"}
                    ]
                }
            }
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": [{"pane_id": "p1", "label": "upagent"}]}}
        raise AssertionError(f"unexpected herdr call: {args}")

    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)

    workspace, pane, workspace_created, pane_created = recruiter._ensure_role_pane(
        recruiter.UPAGENT_PANE_LABEL,
        recruiter.SHARED_SERVICES_WORKSPACE,
        "llm-lab-test",
    )

    assert (workspace, pane, workspace_created, pane_created) == (
        "ws-old",
        "p1",
        False,
        False,
    )


def test_cmd_up_records_only_pane_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state_path = tmp_path / "state" / "recruiter.json"
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)
    monkeypatch.setattr(recruiter, "load_roster", lambda path: {})
    monkeypatch.setattr(
        recruiter, "_resolve_current_herdr_session_name", lambda: "llm-lab-test"
    )
    monkeypatch.setattr(
        recruiter,
        "_ensure_role_pane",
        lambda role, workspace, session: ("ws-herdr", "recruiter-pane", True, True),
    )
    monkeypatch.setattr(
        recruiter,
        "_place_started_agent_in_role_tab",
        lambda pane, workspace, tab, split_direction, herdr_session: pane,
    )
    monkeypatch.setattr(recruiter, "_herdr", lambda *args, **kwargs: None)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)

    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("thin up must not start a process"),
    )

    assert recruiter.cmd_up("roster.yaml") == 0

    state = json.loads(state_path.read_text())
    assert state["herdr_session"] == "llm-lab-test"
    assert state["ownership"] == {
        "pane": {"pane_id": "recruiter-pane", "state": "created"}
    }
    assert "workspace" not in state["ownership"]
    assert "supervisor_pid" not in state
    assert "supervisor_token" not in state
    assert json.loads(capsys.readouterr().out)["reused"] is False


def test_cmd_down_closes_only_created_recruiter_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state_path = tmp_path / "state" / "recruiter.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "herdr_session": "llm-lab-test",
                "ownership": {
                    "pane": {"pane_id": "recruiter-pane", "state": "created"}
                },
                "recruiter_pane": "recruiter-pane",
            }
        )
    )
    closed: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)
    monkeypatch.setattr(recruiter, "cmd_reconcile", lambda force: 0)
    monkeypatch.setattr(
        recruiter,
        "_herdr",
        lambda *args, **kwargs: closed.append((args, kwargs)),
    )
    monkeypatch.setattr(recruiter, "_live_pane_ids", lambda **kwargs: set())

    assert recruiter.cmd_down() == 0
    assert recruiter.cmd_down() == 0

    assert closed == [
        (
            ("pane", "close", "recruiter-pane"),
            {"herdr_session": "llm-lab-test"},
        )
    ]
    assert not state_path.exists()
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert payloads[0]["cleanup"]["status"] == "closed"
    assert payloads[0]["cleanup"]["herdr_session"] == "llm-lab-test"
    assert payloads[1]["cleanup"]["status"] == "not-created"


def test_cmd_down_skips_adopted_recruiter_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state_path = tmp_path / "state" / "recruiter.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "herdr_session": "llm-lab-test",
                "ownership": {
                    "pane": {"pane_id": "recruiter-pane", "state": "adopted"}
                },
                "recruiter_pane": "recruiter-pane",
            }
        )
    )
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)
    monkeypatch.setattr(recruiter, "cmd_reconcile", lambda force: 0)
    monkeypatch.setattr(
        recruiter, "_herdr", lambda *args, **kwargs: pytest.fail("adopted pane closed")
    )

    assert recruiter.cmd_down() == 0

    assert not state_path.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["cleanup"]["status"] == "skipped-adopted"
    assert payload["cleanup"]["worker_pane"] == "recruiter-pane"


def test_cmd_down_malformed_legacy_state_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state" / "recruiter.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "herdr_session": "llm-lab-test",
                "recruiter_pane": "recruiter-pane",
            }
        )
    )
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)
    monkeypatch.setattr(recruiter, "cmd_reconcile", lambda force: 0)
    monkeypatch.setattr(
        recruiter,
        "_herdr",
        lambda *args, **kwargs: pytest.fail("malformed state closed"),
    )

    with pytest.raises(RecruiterError, match="structural pane ownership"):
        recruiter.cmd_down()

    assert state_path.exists()


def test_cmd_status_uses_recorded_state_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state_path = tmp_path / "state" / "recruiter.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps({"herdr_session": "llm-lab-test", "recruiter_pane": "pane"})
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)

    def herdr_json(*args: str, **kwargs: object) -> dict:
        calls.append(kwargs)
        return {
            "result": {
                "workspaces": [
                    {"label": recruiter.UNIFIED_WORKSPACE_LABEL, "workspace_id": "ws"}
                ]
            }
        }

    monkeypatch.setattr(recruiter, "_herdr_json", herdr_json)

    assert recruiter.cmd_status() == 0

    assert calls == [{"herdr_session": "llm-lab-test"}]
    assert "services: up (upagent)" in capsys.readouterr().out


def test_cmd_status_does_not_claim_unrelated_legacy_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "missing-state.json")
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {
                "workspaces": [{"label": "herdr", "workspace_id": "human-workspace"}]
            }
        },
    )

    assert recruiter.cmd_status() == 0
    assert capsys.readouterr().out == "services: down\n"


def test_cmd_status_recognizes_recorded_legacy_workspace_during_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state_path = tmp_path / "legacy-state.json"
    state_path.write_text(
        json.dumps(
            {
                "herdr_session": "llm-lab-test",
                "workspace_id": "legacy-services",
                "workspace_label": "herdr",
            }
        )
    )
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {
                "workspaces": [{"label": "herdr", "workspace_id": "legacy-services"}]
            }
        },
    )

    assert recruiter.cmd_status() == 0
    assert "services: up (herdr)" in capsys.readouterr().out


def test_cmd_status_missing_state_session_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state" / "recruiter.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"recruiter_pane": "pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)
    monkeypatch.setattr(
        recruiter, "_herdr_json", lambda *args, **kwargs: pytest.fail("ambient status")
    )

    with pytest.raises(RecruiterError, match="explicit recorded Herdr session"):
        recruiter.cmd_status()


@pytest.mark.parametrize(
    "door", [recruiter.cmd_recruit, recruiter.cmd_dispatch, recruiter.cmd_request]
)
def test_every_legacy_submission_door_uses_the_same_strict_parser(
    door, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def strict(order_path: str) -> dict:
        calls.append(order_path)
        raise RecruiterError("strict sentinel")

    monkeypatch.setattr(recruiter, "_strict_order", strict)
    with pytest.raises(RecruiterError, match="strict sentinel"):
        door("order.json", "roster.yaml")
    assert calls == ["order.json"]


def test_recruit_pane_door_forwards_exactly_one_strict_file_over_the_socket() -> None:
    door = recruiter._recruit_door_command("/tmp/roster with spaces.yaml")

    assert door.startswith("recruit() {")
    assert 'if [ "$#" -ne 1 ]' in door
    assert 'request -- "$1"' in door
    assert "expects exactly one strict order.json" in door
    assert "--target recruiter" in door
    assert "eval" not in door
    resolved_roster = str(Path("/tmp/roster with spaces.yaml").expanduser().resolve())
    assert f"'{resolved_roster}'" in door


def test_arbitrary_and_empty_request_materialization_has_been_removed() -> None:
    assert not hasattr(recruiter, "_request_input_path")


def test_alias_form_is_interpreted_by_the_clerk_not_by_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python owns no form-repair shortcut any more: even unambiguous aliases are the clerk's
    work, and Python only completes its own bookkeeping afterwards."""
    raw_text = json.dumps(
        {
            "harness": "claude",
            "model": "some-model",
            "agent": "backend",
            "workdir": str(tmp_path),
            "brief_path": str(tmp_path / "instructions.md"),
            "pane": "w1:p1",
            "stage": "stage-1-implementation",
        },
        separators=(",", ":"),
    )
    order_path = tmp_path / "order.json"
    order_path.write_text(raw_text)
    launches = []

    def clerk(*args, **kwargs):
        launches.append(kwargs["attempt_number"])
        return _fake_intake_clerk(*args, **kwargs)

    monkeypatch.setattr(recruiter, "_run_order_intake_clerk", clerk)

    repaired = recruiter._intake_order(str(order_path), "unused-roster.yaml")

    assert launches == [1]
    assert repaired["cwd"] == str(tmp_path)
    assert repaired["instructions_path"] == str(tmp_path / "instructions.md")
    assert repaired["cockpit_pane"] == "w1:p1"
    assert repaired["stage_id"] == "stage-1-implementation"
    assert repaired["order_id"].startswith("intake-")
    assert repaired["result_path"].endswith("-result.json")
    assert json.loads(order_path.read_text()) == repaired
    assert (tmp_path / "order.json.raw-submitted").read_bytes() == raw_text.encode()
    assert (
        json.loads((tmp_path / "order.json.interpreted.json").read_text()) == repaired
    )
    stamp = json.loads((tmp_path / "order.json.intake.json").read_text())
    assert stamp["mode"] == "intake-clerk" and stamp["attempts"] == 1
    assert (
        json.loads((tmp_path / "order.json.validation.json").read_text())["valid"]
        is True
    )


def test_order_intake_escalates_nested_envelope_to_one_clerk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = {
        "payload": {
            "harness": "claude",
            "model": "some-model",
            "persona": "backend",
            "workdir": str(tmp_path),
            "brief_path": str(tmp_path / "instructions.md"),
            "pane": "w1:p1",
            "stage": "stage-1-implementation",
        }
    }
    order_path = tmp_path / "nested.json"
    order_path.write_text(json.dumps(nested))
    calls = []

    def clerk(*args, **kwargs):
        calls.append((args, kwargs))
        return (
            SimpleNamespace(
                order={
                    "harness": "claude",
                    "model": "some-model",
                    "agent": "backend",
                    "cwd": str(tmp_path),
                    "instructions_path": str(tmp_path / "instructions.md"),
                    "cockpit_pane": "w1:p1",
                    "stage_id": "stage-1-implementation",
                },
                refusal=None,
                understood=(),
                missing=(),
                notes=("flattened payload",),
            ),
            {"attempt": 1, "cleanup": {"verified_absent": True}},
        )

    monkeypatch.setattr(recruiter, "_run_order_intake_clerk", clerk)
    repaired = recruiter._intake_order(str(order_path), "roster.yaml")

    assert len(calls) == 1
    assert repaired["agent"] == "backend"
    assert (
        json.loads((tmp_path / "nested.json.intake.json").read_text())["mode"]
        == "intake-clerk"
    )


def test_clerk_interpretation_still_faces_the_unchanged_strict_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "harness": "unknown-harness",
        "model": "some-model",
        "agent": "backend",
        "cwd": str(tmp_path),
        "instructions_path": str(tmp_path / "instructions.md"),
        "cockpit_pane": "w1:p1",
        "stage_id": "stage-1-implementation",
    }
    path = tmp_path / "order.json"
    path.write_text(json.dumps({"payload": values}))
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: (
            SimpleNamespace(
                order=values, refusal=None, understood=(), missing=(), notes=()
            ),
            {"attempt": 1},
        ),
    )

    with pytest.raises(
        RecruiterError, match="failed strict validation.*unknown harness"
    ):
        recruiter._intake_order(str(path), "roster.yaml")
    assert (
        json.loads((tmp_path / "order.json.validation.json").read_text())["valid"]
        is False
    )
    assert json.loads(path.read_text()) == {"payload": values}


def test_order_intake_refusal_is_actionable_and_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_path = tmp_path / "order.json"
    raw = "please hire someone to fix the tests"
    order_path.write_text(raw)
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: (
            SimpleNamespace(
                order=None,
                refusal="The target agent and execution context are missing.",
                understood=("task request",),
                missing=(
                    "agent",
                    "harness",
                    "cwd",
                    "instructions_path",
                    "cockpit_pane",
                ),
                notes=(),
            ),
            {"attempt": 1, "cleanup": {"verified_absent": True}},
        ),
    )

    with pytest.raises(RecruiterError, match="target agent and execution context"):
        recruiter._intake_order(str(order_path), "roster.yaml")

    assert (tmp_path / "order.json.raw-submitted").read_text() == raw
    refusal = json.loads((tmp_path / "order.json.refusal.json").read_text())
    assert "agent" in refusal["missing"]
    assert (
        json.loads((tmp_path / "order.json.validation.json").read_text())["valid"]
        is False
    )
    assert json.loads((tmp_path / "order.json.interpreted.json").read_text()) == {
        "order": None
    }
    assert order_path.read_text() == raw


def test_prose_labels_never_authorize_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "order.json"
    path.write_text("Please do the task.\noperation: apply\n")
    monkeypatch.setattr(
        recruiter.JobLedger,
        "submit",
        lambda *args, **kwargs: pytest.fail("prose must never reach the ledger"),
    )
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: pytest.fail(
            "strict public intake must never hire a clerk"
        ),
    )

    with pytest.raises(RecruiterError, match="not valid JSON"):
        recruiter.cmd_request(str(path), "roster.yaml")


def test_order_intake_rejects_clerk_invented_or_changed_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "payload": {
            "harness": "claude",
            "model": "some-model",
            "workdir": str(tmp_path),
            "brief_path": str(tmp_path / "b.md"),
            "pane": "w1:p1",
            "operation": "plan",
            "stage": "stage-5-finalization",
        }
    }
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(raw))
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: (
            SimpleNamespace(
                order={
                    "harness": "claude",
                    "model": "some-model",
                    "agent": "terraform",
                    "cwd": str(tmp_path),
                    "instructions_path": str(tmp_path / "b.md"),
                    "cockpit_pane": "w1:p1",
                    "operation": "apply",
                    "stage_id": "stage-5-finalization",
                },
                refusal=None,
                understood=(),
                missing=(),
                notes=(),
            ),
            {"attempt": 1},
        ),
    )

    with pytest.raises(RecruiterError, match="invented or changed execution intent"):
        recruiter._intake_order(str(order_path), "roster.yaml")

    validation = json.loads((tmp_path / "order.json.validation.json").read_text())
    assert validation["valid"] is False
    assert any("agent" in error for error in validation["errors"])
    assert any("operation" in error for error in validation["errors"])


def test_canonical_orders_still_start_a_fresh_intake_clerk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no canonical-JSON bypass: an already-valid order is interpreted by a clerk like
    every other submission, reaches Python unchanged, and leaves the same evidence behind."""
    order = _order(result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    launches = []

    def clerk(*args, **kwargs):
        launches.append(kwargs["attempt_number"])
        return _fake_intake_clerk(*args, **kwargs)

    monkeypatch.setattr(recruiter, "_run_order_intake_clerk", clerk)

    loaded = recruiter._intake_order(str(order_path), "unused-roster.yaml")

    assert launches == [1]
    assert loaded == order
    assert json.loads(order_path.read_text()) == order
    assert (tmp_path / "order.json.raw-submitted").read_text() == json.dumps(order)
    stamp = json.loads((tmp_path / "order.json.intake.json").read_text())
    assert stamp["mode"] == "intake-clerk" and stamp["changes"] == []
    assert stamp["clerk"]["attempt"] == 1


def _counting_clerk(rounds: list, mutate=None):
    """Wrap the intake double so a test can watch every bounded round it is given."""

    def clerk(raw_text, raw_path, roster_path, intake_key, **kwargs):
        attempt = kwargs["attempt_number"]
        rounds.append(
            {
                "attempt": attempt,
                "correction": kwargs["correction"],
                "intake_key": intake_key,
                "unknown_fields": list(kwargs["unknown_fields"]),
            }
        )
        response, record = _fake_intake_clerk(
            raw_text, raw_path, roster_path, intake_key, **kwargs
        )
        if mutate is not None:
            mutate(attempt, response)
        return response, record

    return clerk


@pytest.mark.parametrize(
    ("shape", "raw"),
    [
        ("canonical-json", json.dumps(_order())),
        ("malformed-json", '{"harness": "claude", "agent": '),
        ("prose", "please have someone fix the failing retry tests"),
        ("incomplete-object", json.dumps({"harness": "claude", "agent": "backend"})),
        ("unknown-fields", json.dumps({**_order(), "op": "apply-now"})),
        ("specialist-worded", "consult the docs specialist about the retry contract"),
    ],
)
def test_every_submission_shape_records_one_intake_clerk_launch(
    shape: str, raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical JSON, malformed JSON, prose, incomplete objects, unknown fields, and
    specialist-worded requests all reach the same fresh clerk. Python interprets none of them."""
    order_path = tmp_path / "order.json"
    order_path.write_text(raw)
    rounds: list = []
    monkeypatch.setattr(recruiter, "_run_order_intake_clerk", _counting_clerk(rounds))

    try:
        recruiter._intake_order(str(order_path), "roster.yaml")
    except recruiter.IntakeOutcomeError as error:
        assert error.outcome != "infrastructure-failure", shape

    assert rounds and rounds[0]["attempt"] == 1, (
        f"{shape} never reached the intake clerk"
    )
    assert (tmp_path / "order.json.raw-submitted").read_text() == raw
    stamp = json.loads((tmp_path / "order.json.intake.json").read_text())
    assert stamp["clerk"]["attempt_name"] == "attempt-test-double"


def test_strict_executable_requests_reach_request_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    submitted = _order(cwd=str(tmp_path), result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(submitted))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    monkeypatch.setattr(
        recruiter, "_spawn_job", lambda *args: SimpleNamespace(poll=lambda: None)
    )
    monkeypatch.setattr(
        recruiter.JobLedger,
        "state",
        lambda self, key: {
            "state": "running",
            "requester_control_token": "control-token",
            "worker_pane": "w1:p2",
        },
    )
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: pytest.fail(
            "strict request must not hire an intake clerk"
        ),
    )

    assert recruiter.cmd_request(str(order_path), "roster.yaml") == 0
    accepted = json.loads(capsys.readouterr().out.split("REQUEST_ACCEPTED ", 1)[1])
    assert accepted["state"] == "running"
    assert accepted["control_token"] == "control-token"
    assert not (tmp_path / "order.json.interpreted.json").exists()


def test_new_request_guard_runs_outside_mutation_lock_and_commit_runs_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    lock_depth = 0

    class TrackedLock:
        def __enter__(self) -> None:
            nonlocal lock_depth
            lock_depth += 1

        def __exit__(self, *_args: object) -> None:
            nonlocal lock_depth
            lock_depth -= 1

    monkeypatch.setattr(ledger, "_claim_lock", lambda _key: TrackedLock())
    guard_calls: list[int] = []
    commit_calls: list[int] = []

    key, created = ledger.submit_with_new_request_guard(
        order,
        lambda: guard_calls.append(lock_depth),
        on_create=lambda: commit_calls.append(lock_depth),
    )

    assert created is True
    assert ledger.order(key) == order
    assert guard_calls == [0]
    assert commit_calls == [1]


def test_direct_submit_can_win_while_new_request_preflight_is_in_progress(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    original = _order(
        result_path=str(tmp_path / "result.json"), cockpit_pane="original-pane"
    )
    rebound = {**original, "cockpit_pane": "candidate-pane"}
    guard_entered = threading.Event()
    release_guard = threading.Event()
    direct_done = threading.Event()
    candidate_result: list[tuple[str, bool]] = []
    direct_result: list[tuple[str, bool]] = []
    create_commits: list[str] = []

    def preflight_candidate() -> None:
        guard_entered.set()
        assert release_guard.wait(timeout=3)

    def submit_candidate() -> None:
        candidate_result.append(
            ledger.submit_with_new_request_guard(
                rebound,
                preflight_candidate,
                existing_order=original,
                on_create=lambda: create_commits.append("candidate"),
            )
        )

    def submit_direct() -> None:
        direct_result.append(ledger.submit(original))
        direct_done.set()

    candidate_thread = threading.Thread(target=submit_candidate)
    candidate_thread.start()
    assert guard_entered.wait(timeout=2)
    direct_thread = threading.Thread(target=submit_direct)
    direct_thread.start()
    assert direct_done.wait(timeout=2), "preflight must not hold the mutation lock"
    release_guard.set()
    candidate_thread.join(timeout=3)
    direct_thread.join(timeout=3)

    assert not candidate_thread.is_alive()
    assert not direct_thread.is_alive()
    assert direct_result[0][1] is True
    assert candidate_result == [(direct_result[0][0], False)]
    assert create_commits == []
    assert ledger.order(direct_result[0][0]) == original


def test_accepted_retry_reattaches_after_its_original_cockpit_pane_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cockpit liveness gates first intake, not an identical order already in the ledger."""
    order = _order(
        cwd=str(tmp_path),
        result_path=str(tmp_path / "result.json"),
        cockpit_pane="dead-original-pane",
    )
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    monkeypatch.setattr(
        recruiter,
        "verify_cockpit_pane",
        lambda *_args, **_kwargs: pytest.fail(
            "an accepted duplicate must not reapply first-intake pane liveness"
        ),
    )
    monkeypatch.setattr(
        recruiter,
        "_herdr_owner_record",
        lambda: {"herdr_session": "test-session"},
    )
    ledger = recruiter.JobLedger()
    key, created = ledger.submit(order)
    assert created is True

    def attach_runner(candidate: str, _roster: str) -> SimpleNamespace:
        assert candidate == key
        recruiter.JobLedger()._snapshot(
            key,
            "running",
            requester_control_token="control-token",
            worker_address="existing-worker",
            worker_pane="existing-worker-pane",
        )
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(recruiter, "_spawn_job", attach_runner)

    assert recruiter._request_order(order, "roster.yaml") == 0

    assert (
        json.loads((ledger.request_dir(key) / "request.json").read_text())[
            "cockpit_pane"
        ]
        == "dead-original-pane"
    )
    accepted = json.loads(capsys.readouterr().out.split("REQUEST_ACCEPTED ", 1)[1])
    assert accepted["worker_address"] == "existing-worker"


def test_simultaneous_async_spawn_loser_attaches_to_active_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _order(cwd=str(tmp_path), result_path=str(tmp_path / "result.json"))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    barrier = threading.Barrier(2)
    sequence_lock = threading.Lock()
    sequence = 0
    winner_pid = 424_242
    winner_claimed = threading.Event()

    class Handle:
        def __init__(self, loser: bool):
            self.loser = loser

        def poll(self) -> int | None:
            if not self.loser:
                return None
            ledger = recruiter.JobLedger()
            key = ledger.key_for_order(order)
            if not winner_claimed.is_set():
                token = ledger.claim(
                    key,
                    order["order_id"],
                    60_000,
                    owner={
                        "herdr_session": "test-session",
                        "runner_pid": winner_pid,
                        "runner_start_time": "winner-start",
                    },
                )
                assert token
                winner_claimed.set()
            return 0

    def spawn(_key: str, _roster: str) -> Handle:
        nonlocal sequence
        with sequence_lock:
            index = sequence
            sequence += 1
        barrier.wait(timeout=5)
        return Handle(loser=index == 0)

    def runner_alive(pid: object, key: str) -> bool:
        if pid != winner_pid:
            return False
        recruiter.JobLedger()._snapshot(
            key,
            "running",
            worker_address="winner-agent",
            worker_pane="winner-pane",
        )
        return True

    monkeypatch.setattr(recruiter, "_spawn_job", spawn)
    monkeypatch.setattr(recruiter, "_runner_alive", runner_alive)
    outcomes: list[int] = []
    errors: list[BaseException] = []

    def request() -> None:
        try:
            outcomes.append(recruiter._request_order(order, "roster.yaml"))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert outcomes == [0, 0]
    assert sequence == 2
    ledger = recruiter.JobLedger()
    key = ledger.key_for_order(order)
    claims = dict(ledger.active_claims())
    assert list(claims) == [key]
    assert claims[key]["runner_pid"] == winner_pid
    assert ledger.state(key)["state"] == "running"


@pytest.mark.parametrize("failure_type", [RuntimeError, OSError])
def test_detached_supervisor_start_failure_publishes_blocked_bundle_receipt_and_event(
    failure_type: type[OSError] | type[RuntimeError],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(tmp_path / "instructions.md"),
        result_path=str(tmp_path / "result.json"),
    )
    Path(order["instructions_path"]).write_text("Do the stage.\n")
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    monkeypatch.setattr(recruiter, "_require_hub_authority", lambda: None)
    monkeypatch.setattr(
        recruiter,
        "_herdr_owner_record",
        lambda: {"herdr_session": "test-session"},
    )

    def failing_popen(*_args: object, **_kwargs: object) -> None:
        raise failure_type("injected detached supervisor start failure")

    monkeypatch.setattr(recruiter.subprocess, "Popen", failing_popen)

    assert recruiter.cmd_request(str(order_path), "roster.yaml") == 1

    ledger = recruiter.JobLedger()
    key = ledger.key_for_order(order)
    request_dir = ledger.request_dir(key)
    result = json.loads(Path(order["result_path"]).read_text())
    receipt = json.loads((request_dir / "receipt.json").read_text())
    events = [
        json.loads(path.read_text())
        for path in sorted((request_dir / "events").glob("*.json"))
    ]
    assert result["verdict"] == "blocked"
    assert "injected detached supervisor start failure" in result["reason"]
    assert receipt["verdict"] == "blocked"
    assert events[-1]["event"] == "finished"
    assert events[-1]["completion_source"] == "runner-start-failure"
    assert (
        json.loads((request_dir / "artifact-manifest.json").read_text())[
            "schema_version"
        ]
        == 1
    )
    assert "REQUEST_TERMINAL" in capsys.readouterr().out


def test_non_executable_requests_are_rejected_by_python_without_a_clerk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_path = tmp_path / "order.json"
    order_path.write_text("please have someone look at the flaky retry test")
    monkeypatch.setattr(
        recruiter.JobLedger,
        "submit",
        lambda *args, **kwargs: pytest.fail(
            "invalid request must never reach the ledger"
        ),
    )
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: pytest.fail("invalid request must never hire a clerk"),
    )

    with pytest.raises(SystemExit, match="not valid JSON"):
        recruiter.main(["--roster", "roster.yaml", "dispatch", str(order_path)])


def test_invalid_clerk_output_is_corrected_by_the_same_clerk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Machine-validation errors go back to the intake clerk instead of becoming a refusal."""
    order_path = tmp_path / "order.json"
    order_path.write_text(
        json.dumps(
            {
                "harness": "claude",
                "model": "some-model",
                "agent": "backend",
                "workdir": str(tmp_path),
                "brief": str(tmp_path / "b.md"),
                "pane": "w1:p1",
                "stage": "stage-5-finalization",
            }
        )
    )
    rounds: list = []

    def drop_stage_once(attempt: int, response) -> None:
        if attempt == 1:
            response.order = {
                k: v for k, v in response.order.items() if k != "stage_id"
            }

    monkeypatch.setattr(
        recruiter, "_run_order_intake_clerk", _counting_clerk(rounds, drop_stage_once)
    )

    order = recruiter._intake_order(str(order_path), "roster.yaml")

    assert [entry["attempt"] for entry in rounds] == [1, 2]
    assert rounds[0]["correction"] is None
    assert rounds[1]["correction"]["errors"] == ["clerk dropped explicit stage_id"]
    assert "stage_id" not in rounds[1]["correction"]["order"]
    # A correction round must never be answered by the response it was sent to correct.
    assert rounds[0]["intake_key"] != rounds[1]["intake_key"]
    assert order["stage_id"] == "stage-5-finalization"
    assert (
        json.loads((tmp_path / "order.json.intake.json").read_text())["attempts"] == 2
    )


def test_correction_is_bounded_and_ends_as_an_intake_clerk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clerk that never corrects itself exhausts a bounded budget; it never loops forever and
    its failure is the Recruiter's to report, not the clerk's words."""
    order_path = tmp_path / "order.json"
    order_path.write_text(
        json.dumps(
            {
                "harness": "claude",
                "model": "some-model",
                "agent": "backend",
                "workdir": str(tmp_path),
                "brief": str(tmp_path / "b.md"),
                "pane": "w1:p1",
                "stage": "stage-5-finalization",
            }
        )
    )
    rounds: list = []
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        _counting_clerk(
            rounds,
            lambda _attempt, response: response.order.update(agent="terraform"),
        ),
    )

    with pytest.raises(recruiter.IntakeOutcomeError) as failure:
        recruiter._intake_order(str(order_path), "roster.yaml")

    assert len(rounds) == recruiter.INTAKE_ATTEMPT_LIMIT
    assert failure.value.outcome == "intake-clerk-failure"
    assert failure.value.exit_code == 4
    assert "clerk changed agent" in str(failure.value)
    validation = json.loads((tmp_path / "order.json.validation.json").read_text())
    assert validation["attempts"] == recruiter.INTAKE_ATTEMPT_LIMIT
    assert validation["authored_by"] == "recruiter"
    assert json.loads(order_path.read_text())["agent"] == "backend"


def test_strict_dispatch_never_depends_on_the_intake_clerk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(_order(result_path=str(tmp_path / "result.json"))))
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: pytest.fail(
            "strict dispatch must not hire an intake clerk"
        ),
    )
    monkeypatch.setattr(recruiter, "_dispatch_order", lambda order, roster: 0)

    assert recruiter.main(["--roster", "roster.yaml", "dispatch", str(order_path)]) == 0


def test_an_unreadable_submission_fails_before_intake_or_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "never-written.json"
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: pytest.fail(
            "missing file must not hire an intake clerk"
        ),
    )
    monkeypatch.setattr(
        recruiter.JobLedger,
        "submit",
        lambda *args, **kwargs: pytest.fail("missing file must not reach the ledger"),
    )

    with pytest.raises(SystemExit, match="order.json not found"):
        recruiter.main(["--roster", "roster.yaml", "dispatch", str(missing)])


def test_a_recognized_field_owns_its_own_subtree(tmp_path: Path) -> None:
    """`requester.id` is not a second `order_id` and `manager_placement.mode` is not a second
    `mode`; otherwise every richly-shaped submission would look self-contradictory."""
    supplied = {
        "order_id": "apply-step-1",
        "mode": "direct",
        "manager_placement": {"mode": "requester"},
        "requester": {"id": "leader", "kind": "file-mailbox", "address": str(tmp_path)},
    }
    raw = json.dumps(supplied)

    assert recruiter._raw_field_values(raw, "order_id") == ["apply-step-1"]
    assert recruiter._raw_field_values(raw, "mode") == ["direct"]
    assert recruiter._clerk_provenance_errors(raw, dict(supplied)) == []
    # Hoisting a value out of a recognized field's subtree is still invention, not provenance.
    assert "clerk invented stage_id" in recruiter._clerk_provenance_errors(
        raw, {**supplied, "stage_id": "requester"}
    )


def test_order_intake_preserves_direct_apply_authority_exactly(tmp_path: Path) -> None:
    approval = {
        "approved_by": "human",
        "approved_at": "2026-01-01T00:00:00Z",
        "nonce": "nonce-1",
        "plan_sha256": "a" * 64,
    }
    artifact = {"path": str(tmp_path / "plan.tfplan"), "sha256": "a" * 64}
    sloppy = {
        "order_id": "apply-step-1",
        "phase_id": "plan-x",
        "plan_id": "plan-x",
        "step_id": "step-1",
        "mode": "direct",
        "operation": "apply",
        "requires_apply": True,
        "approval": approval,
        "plan_artifact": artifact,
        "manager_placement": {"mode": "requester"},
        "requester": {
            "id": "leader",
            "kind": "file-mailbox",
            "address": str(tmp_path / "inbox"),
        },
        "env": {"SAFE": "yes"},
        "timeout": "120000",
        "harness": "claude",
        "model": "some-model",
        "agent": "terraform",
        "workdir": str(tmp_path),
        "brief": str(tmp_path / "instructions.md"),
        "pane": "w1:p1",
        "stage": "stage-5-finalization",
        "result_path": str(tmp_path / "r.json"),
    }
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(sloppy))

    repaired = recruiter._intake_order(str(order_path), "unused.yaml")

    for field in (
        "mode",
        "operation",
        "requires_apply",
        "approval",
        "plan_artifact",
        "manager_placement",
        "requester",
        "env",
        "plan_id",
        "step_id",
    ):
        assert repaired[field] == sloppy[field]
    assert repaired["timeout_ms"] == 120000


def test_explicit_invalid_stage_and_timeout_are_not_silently_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "harness": "claude",
        "model": "some-model",
        "agent": "backend",
        "cwd": str(tmp_path),
        "instructions_path": str(tmp_path / "b.md"),
        "cockpit_pane": "w1:p1",
        "stage_id": "stage-1",
        "timeout_ms": -1,
    }
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(raw))
    called = []

    def refuse(*args, **kwargs):
        called.append(True)
        return (
            SimpleNamespace(
                order=None,
                refusal="Explicit stage and timeout are invalid.",
                understood=(),
                missing=(),
                notes=(),
            ),
            {"attempt": 1},
        )

    monkeypatch.setattr(recruiter, "_run_order_intake_clerk", refuse)
    with pytest.raises(RecruiterError, match="Explicit stage and timeout"):
        recruiter._intake_order(str(order_path), "roster.yaml")
    assert called == [True]
    assert json.loads(order_path.read_text()) == raw


def test_conflicting_aliases_escalate_instead_of_silently_winning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "harness": "claude",
        "model": "some-model",
        "agent": "backend",
        "cwd": str(tmp_path / "one"),
        "workdir": str(tmp_path / "two"),
        "instructions_path": str(tmp_path / "b.md"),
        "cockpit_pane": "w1:p1",
        "stage_id": "stage-5-finalization",
    }
    path = tmp_path / "order.json"
    path.write_text(json.dumps(raw))
    called = []
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: (
            called.append(True)
            or SimpleNamespace(
                order=None,
                refusal="cwd aliases conflict",
                understood=(),
                missing=("unambiguous cwd",),
                notes=(),
            ),
            {"attempt": 1},
        ),
    )

    with pytest.raises(RecruiterError, match="cwd aliases conflict"):
        recruiter._intake_order(str(path), "roster.yaml")
    assert called == [True]
    assert json.loads(path.read_text()) == raw


def test_unknown_fields_escalate_instead_of_being_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "harness": "claude",
        "model": "some-model",
        "agent": "backend",
        "cwd": str(tmp_path),
        "instructions_path": str(tmp_path / "b.md"),
        "cockpit_pane": "w1:p1",
        "stage_id": "stage-5-finalization",
        "op": "apply-now",
    }
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(raw))
    called = []
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: (
            called.append(True)
            or SimpleNamespace(
                order=None,
                refusal="Unknown operation is ambiguous.",
                understood=(),
                missing=("operation",),
                notes=(),
            ),
            {"attempt": 1},
        ),
    )

    with pytest.raises(RecruiterError, match="ambiguous"):
        recruiter._intake_order(str(order_path), "roster.yaml")
    assert called == [True]
    assert (
        "op"
        in json.loads((tmp_path / "order.json.intake.json").read_text())[
            "unknown_fields"
        ]
    )


def test_intake_persistence_failure_never_rewrites_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(
        {
            "harness": "claude",
            "model": "some-model",
            "agent": "backend",
            "workdir": str(tmp_path),
            "brief": str(tmp_path / "b.md"),
            "pane": "w1:p1",
            "stage": "stage-5-finalization",
        }
    )
    path = tmp_path / "order.json"
    path.write_text(raw)
    monkeypatch.setattr(
        recruiter,
        "_persist_intake_success",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(RecruiterError, match="paper trail"):
        recruiter._intake_order(str(path), "unused.yaml")
    assert path.read_text() == raw


def test_intake_clerk_bootstrap_is_prejournaled_random_private_and_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"recruiter_pane": "trusted:pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    started = []
    cleaned = []

    def start(name, order, launch, **kwargs):
        ownership = recruiter._secure_json(Path(order["cwd"]) / "ownership.json")
        assert ownership["state"] == "launching"
        assert ownership["pane"] is None
        assert ownership["agent_name"] == name
        # Herdr's 32-character name limit leaves no room for the literal token, so the
        # binding is the recomputed name itself — which is what the Recruiter verifies.
        assert name == recruiter._intake_clerk_agent_name(
            ownership["intake_key"], ownership["lease_token"]
        )
        assert ownership["owner_start_time"]
        started.append((name, order, launch, kwargs))
        return "clerk:pane", "workspace", name

    def wait(path, _timeout, parser):
        value = {"refusal": "missing target", "understood": [], "missing": ["agent"]}
        recruiter._secure_write_json(path, value)
        return parser(json.dumps(value))

    monkeypatch.setattr(recruiter, "_start_herdr_agent", start)
    monkeypatch.setattr(recruiter, "_resize_started_pane", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        recruiter,
        "_wait_for_agent_health",
        lambda pane, **kwargs: {
            "healthy": True,
            "pane_id": pane,
            "detected_agent": "claude",
            "cwd_matches": True,
            "process_pid": None,
        },
    )
    monkeypatch.setattr(recruiter, "_wait_typed_file", wait)
    monkeypatch.setattr(
        recruiter,
        "_cleanup_intake_clerk",
        lambda ownership: (
            cleaned.append(dict(ownership))
            or {
                "status": "closed",
                "worker_pane": ownership["pane"],
                "verified_absent": True,
            }
        ),
    )

    response, record = _live_intake_clerk(
        "malformed", tmp_path / "order.json.raw-submitted", "roster.yaml", "abc123"
    )

    assert response.refusal == "missing target"
    assert len(started) == 1 and len(cleaned) == 1
    clerk_order = started[0][1]
    attempt = Path(clerk_order["cwd"])
    assert clerk_order["cockpit_pane"] == "trusted:pane"
    assert set(clerk_order) == {"cockpit_pane", "cwd"}
    assert attempt.parent == state.parent / "intake/attempts"
    assert attempt.name.startswith("attempt-") and attempt.name != "abc123"
    assert stat.S_IMODE(attempt.stat().st_mode) == 0o700
    assert record["attempt_name"] == attempt.name
    assert record["cleanup"]["verified_absent"] is True
    assert '--tools ""' in started[0][2]
    assert "--dangerously-skip-permissions" not in started[0][2]


def test_intake_clerk_unavailable_becomes_refusal_without_target_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recruiter, "_run_order_intake_clerk", _live_intake_clerk)
    state = tmp_path / "missing-state.json"
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    order_path = tmp_path / "order.json"
    order_path.write_text("not json")

    with pytest.raises(RecruiterError, match="intake clerk unavailable") as refusal:
        recruiter._intake_order(str(order_path), "roster.yaml")
    assert "agent" in str(refusal.value) and "instructions_path" in str(refusal.value)
    recorded = json.loads((tmp_path / "order.json.refusal.json").read_text())
    assert "agent" in recorded["missing"]
    # An unavailable clerk is the Recruiter's failure to report, never the clerk's own words.
    assert recorded["authored_by"] == "recruiter"
    assert refusal.value.outcome == "intake-clerk-failure"
    assert refusal.value.exit_code == 4


def test_order_intake_regenerates_unsafe_ids_deterministically(tmp_path: Path) -> None:
    sloppy = {
        "id": "../../escape",
        "harness": "claude",
        "model": "some-model",
        "agent": "backend",
        "workdir": str(tmp_path),
        "brief": str(tmp_path / "b.md"),
        "pane": "w1:p1",
        "stage": "stage-5-finalization",
    }
    first_path = tmp_path / "one.json"
    second_path = tmp_path / "two.json"
    raw = json.dumps(sloppy)
    first_path.write_text(raw)
    second_path.write_text(raw)

    first = recruiter._intake_order(str(first_path), "unused.yaml")
    second = recruiter._intake_order(str(second_path), "unused.yaml")

    assert first["order_id"] == second["order_id"]
    assert first["order_id"].startswith("intake-")
    assert "escape" not in first["order_id"]
    assert first["result_path"].startswith(str(tmp_path))


def _repairable_order(tmp_path: Path, order_id: str) -> dict:
    return {
        "order_id": order_id,
        "harness": "claude",
        "model": "some-model",
        "agent": "docs",
        "cwd": str(tmp_path),
        "instructions_path": str(tmp_path / "b.md"),
        "cockpit_pane": "w1:p1",
        "stage_id": "stage-5-finalization",
    }


def test_an_unsafe_consult_identity_is_refused_rather_than_regenerated(
    tmp_path: Path,
) -> None:
    """A LAUNDERING GUARD, not specialist vocabulary — and the reason a keyword sweep of the
    word `consult` must not take it out.

    Intake repairs an unsafe order id by regenerating it. That is right for an ordinary
    submission and wrong for one claiming the consult door's minted identity: regenerating
    would turn forged paperwork into a valid order under a fresh, legitimate-looking id. The
    rule is refuse, never rename. `_SAFE_ORDER_ID_RE` already excludes `/`, so the traversal
    fails the regex either way; this guard's whole contribution is WHICH branch it takes.
    """
    with pytest.raises(ContractError, match="cannot be regenerated"):
        recruiter._complete_order_form(
            _repairable_order(tmp_path, "consult-../../etc/passwd"),
            "raw",
            tmp_path / "order.json",
        )


def test_an_ordinary_unsafe_order_id_is_still_repaired(tmp_path: Path) -> None:
    """The other half of the same rule: the guard is narrow. An ordinary caller whose order id
    happens to be unusable gets it regenerated with the change recorded, exactly as before —
    a guard that refused everything would break every sloppy submission intake exists to fix."""
    order, changes = recruiter._complete_order_form(
        _repairable_order(tmp_path, "phase-1/stage-1"), "raw", tmp_path / "order.json"
    )

    assert recruiter._SAFE_ORDER_ID_RE.fullmatch(order["order_id"])
    assert any("regenerated unsafe order_id" in c for c in changes)


def test_unsafe_consult_prefixed_order_ids_are_refused_not_laundered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sloppy = {
        "id": "consult-../x",
        "harness": "claude",
        "model": "some-model",
        "agent": "docs",
        "workdir": str(tmp_path),
        "brief": str(tmp_path / "b.md"),
        "pane": "w1:p1",
        "stage": "stage-5-finalization",
    }
    path = tmp_path / "order.json"
    path.write_text(json.dumps(sloppy))
    monkeypatch.setattr(
        recruiter,
        "_run_order_intake_clerk",
        lambda *args, **kwargs: (
            SimpleNamespace(
                order=None,
                refusal="Unsafe consult identity cannot be repaired.",
                understood=(),
                missing=(),
                notes=(),
            ),
            {"attempt": 1},
        ),
    )

    with pytest.raises(RecruiterError, match="Unsafe consult identity"):
        recruiter._intake_order(str(path), "roster.yaml")


def test_identical_bytes_reuse_one_clerk_but_different_bytes_launch_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse is idempotency, not a bypass — the two halves of the contract, through the REAL seam.

    A byte-identical resubmission of the same order file reuses the first attempt's validated
    response: it does NOT launch a second clerk (the reuse key is the resolved path plus the exact
    submitted bytes). A submission whose bytes DIFFER — a genuinely different request, or a
    correction round — is a different request and launches a fresh clerk. This drives the real
    `_run_order_intake_clerk` reuse index, not a hand-built one, and counts actual launches.
    """
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"recruiter_pane": "trusted:pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    monkeypatch.setattr(
        recruiter, "_run_order_intake_clerk", _live_intake_clerk
    )  # the real seam

    order_a = _order(result_path=str(tmp_path / "result-a.json"))
    order_b = _order(agent="reviewer", result_path=str(tmp_path / "result-b.json"))
    order_path = tmp_path / "order.json"
    launches: list[str] = []
    current = {"order": order_a}

    def start(name, clerk_order, launch, **kwargs):
        launches.append(name)
        return "clerk:pane", "workspace", name

    def wait(path, _timeout, parser):
        value = {"order": current["order"]}
        recruiter._secure_write_json(path, value)
        return parser(json.dumps(value))

    monkeypatch.setattr(recruiter, "_start_herdr_agent", start)
    monkeypatch.setattr(recruiter, "_resize_started_pane", lambda *a, **k: None)
    monkeypatch.setattr(
        recruiter,
        "_wait_for_agent_health",
        lambda pane, **kwargs: {
            "healthy": True,
            "pane_id": pane,
            "detected_agent": "claude",
            "cwd_matches": True,
            "process_pid": None,
        },
    )
    monkeypatch.setattr(recruiter, "_wait_typed_file", wait)
    monkeypatch.setattr(
        recruiter,
        "_cleanup_intake_clerk",
        lambda ownership: {
            "status": "closed",
            "worker_pane": ownership["pane"],
            "verified_absent": True,
        },
    )

    # First submission of order A: one fresh clerk launches and its response is indexed.
    order_path.write_text(json.dumps(order_a))
    assert recruiter._intake_order(str(order_path), "roster.yaml") == order_a
    assert len(launches) == 1

    # Byte-identical resubmission of order A: reused, no second launch.
    order_path.write_text(json.dumps(order_a))
    assert recruiter._intake_order(str(order_path), "roster.yaml") == order_a
    assert (
        len(launches) == 1
    )  # the resubmission reused the first attempt's validated response

    # A DIFFERENT submission (order B): different bytes, so a fresh clerk launches.
    current["order"] = order_b
    order_path.write_text(json.dumps(order_b))
    assert recruiter._intake_order(str(order_path), "roster.yaml") == order_b
    assert len(launches) == 2  # different bytes are a different request — never reused


def test_identical_intake_reuses_only_a_validated_indexed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"recruiter_pane": "trusted:pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    layout = recruiter._prepare_intake_layout()
    attempt = recruiter._new_intake_attempt(layout)
    key = "stable-key"
    token = "a" * 32
    response = {"refusal": "agent missing", "understood": [], "missing": ["agent"]}
    response_path = attempt / "response.json"
    recruiter._secure_write_json(response_path, response)
    response_hash = recruiter.hashlib.sha256(
        recruiter._secure_file_bytes(response_path)
    ).hexdigest()
    cleanup = {"status": "closed", "verified_absent": True}
    recruiter._secure_write_json(
        attempt / "ownership.json",
        {
            "schema_version": 1,
            "intake_key": key,
            "attempt_name": attempt.name,
            "lease_token": token,
            "agent_name": recruiter._intake_clerk_agent_name(key, token),
            "state": "closed",
            "cleanup": cleanup,
            "response_sha256": response_hash,
        },
    )
    recruiter._secure_write_json(
        layout["index"] / f"{key}.json",
        {
            "schema_version": 1,
            "intake_key": key,
            "attempt_name": attempt.name,
            "lease_token": token,
            "response_sha256": response_hash,
        },
    )
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: pytest.fail(
            "a verified identical intake must not hire twice"
        ),
    )

    parsed, record = _live_intake_clerk(
        "bad", tmp_path / "order.raw-submitted", "roster.yaml", key
    )
    assert parsed.refusal == "agent missing"
    assert record["reused"] is True
    assert record["attempt_name"] == attempt.name


def test_post_start_journal_failure_closes_the_just_created_named_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"recruiter_pane": "trusted:pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    started = []
    cleaned = []

    def start(name, order, launch, **kwargs):
        started.append((name, order))
        return "clerk:pane", "workspace", name

    monkeypatch.setattr(recruiter, "_start_herdr_agent", start)
    monkeypatch.setattr(
        recruiter,
        "_record_started_intake_clerk",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("journal disk failure")),
    )
    monkeypatch.setattr(
        recruiter,
        "_wait_for_agent_health",
        lambda pane, **kwargs: {
            "healthy": True,
            "pane_id": pane,
            "detected_agent": "claude",
            "cwd_matches": True,
            "process_pid": None,
        },
    )
    monkeypatch.setattr(
        recruiter,
        "_cleanup_intake_clerk",
        lambda ownership: (
            cleaned.append(dict(ownership))
            or {
                "status": "closed",
                "worker_pane": ownership["pane"],
                "verified_absent": True,
            }
        ),
    )

    with pytest.raises(RecruiterError, match="journal disk failure"):
        _live_intake_clerk(
            "bad", tmp_path / "order.raw-submitted", "roster.yaml", "failure-key"
        )

    assert len(started) == 1 and len(cleaned) == 1
    assert cleaned[0]["pane"] == "clerk:pane"
    assert cleaned[0]["agent_name"] == started[0][0]
    attempt = Path(started[0][1]["cwd"])
    ownership = recruiter._secure_json(attempt / "ownership.json")
    assert ownership["state"] == "closed"
    assert ownership["cleanup"]["verified_absent"] is True


def test_uncertain_launch_stays_open_until_delayed_named_agent_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    layout = recruiter._prepare_intake_layout()
    attempt = recruiter._new_intake_attempt(layout)
    key = "crash-key"
    token = "b" * 32
    name = recruiter._intake_clerk_agent_name(key, token)
    ownership_path = attempt / "ownership.json"
    ownership = {
        "schema_version": 1,
        "intake_key": key,
        "attempt_name": attempt.name,
        "lease_token": token,
        "agent_name": name,
        "owner_pid": 999999999,
        "owner_start_time": "old-start",
        "expected_agent": "claude",
        "expected_process": "claude",
        "expected_cwd": str(attempt),
        "herdr_session": "llm-lab-test",
        "expires_at": int(time.time()) + 300,
        "pane": None,
        "state": "launching",
    }
    recruiter._secure_write_json(ownership_path, ownership)
    closed = []
    visibility = {"shown": False}

    def herdr_json(*args, **kwargs):
        if args[:2] == ("agent", "get"):
            if not visibility["shown"] or closed:
                raise RecruiterError("agent_not_found")
            return {"result": {"agent": {"name": name, "pane_id": "resolved-pane"}}}
        if args[:2] == ("pane", "get"):
            return {
                "result": {
                    "pane": {
                        "pane_id": "resolved-pane",
                        "agent": "claude",
                        "cwd": str(attempt),
                    }
                }
            }
        if args[:2] == ("pane", "process-info"):
            return {
                "result": {
                    "process_info": {
                        "foreground_processes": [
                            {"name": "claude", "pid": 123, "argv": ["claude"]}
                        ]
                    }
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(recruiter, "_herdr_json", herdr_json)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: (
            closed.append(pane)
            or {"status": "closed", "worker_pane": pane, "verified_absent": True}
        ),
    )

    assert recruiter._reconcile_intake_clerks(force=False) == 0
    first = recruiter._secure_json(ownership_path)
    assert first["state"] == "launch-uncertain"
    assert first["cleanup"]["verified_absent"] is False
    assert closed == []

    visibility["shown"] = True
    assert recruiter._reconcile_intake_clerks(force=False) == 1
    assert closed == ["resolved-pane"]
    recorded = recruiter._secure_json(ownership_path)
    assert recorded["state"] == "closed"
    assert recorded["cleanup"]["agent_name"] == name


def test_intake_rejects_precreated_symlink_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    outside = tmp_path / "attacker-controlled"
    outside.mkdir()
    (state.parent / "intake").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(recruiter, "STATE_FILE", state)

    with pytest.raises(RecruiterError, match="real broker-owned directory"):
        recruiter._prepare_intake_layout()
    assert not list(outside.iterdir())


def test_reuse_rejects_attempt_directory_swapped_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"recruiter_pane": "trusted:pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    layout = recruiter._prepare_intake_layout()
    outside = tmp_path / "outside-attempt"
    outside.mkdir(mode=0o700)
    attempt_name = "attempt-deadbeef"
    (layout["attempts"] / attempt_name).symlink_to(outside, target_is_directory=True)
    key = "symlink-key"
    recruiter._secure_write_json(
        layout["index"] / f"{key}.json",
        {
            "schema_version": 1,
            "intake_key": key,
            "attempt_name": attempt_name,
            "lease_token": "c" * 32,
            "response_sha256": "0" * 64,
        },
    )
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: pytest.fail(
            "unsafe reuse metadata must not start a clerk"
        ),
    )

    with pytest.raises(RecruiterError, match="real broker-owned directory"):
        _live_intake_clerk("bad", tmp_path / "raw", "roster.yaml", key)
    assert not list(outside.iterdir())


def test_reuse_index_cannot_escape_the_trusted_attempts_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"recruiter_pane": "trusted:pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: _roster())
    layout = recruiter._prepare_intake_layout()
    key = "escape-key"
    recruiter._secure_write_json(
        layout["index"] / f"{key}.json",
        {
            "schema_version": 1,
            "intake_key": key,
            "attempt_name": "../outside",
            "lease_token": "f" * 32,
            "response_sha256": "0" * 64,
        },
    )
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: pytest.fail("escaped reuse metadata must not launch"),
    )

    with pytest.raises(RecruiterError, match="invalid attempt directory name"):
        _live_intake_clerk("bad", tmp_path / "raw", "roster.yaml", key)


def test_stale_pane_id_is_never_closed_when_unique_agent_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "stale-key"
    token = "d" * 32
    ownership = {
        "intake_key": key,
        "lease_token": token,
        "agent_name": recruiter._intake_clerk_agent_name(key, token),
        "herdr_session": "llm-lab-test",
        "pane": "reused-pane",
    }
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"agent": {}}},
    )
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: pytest.fail(f"foreign pane {pane} must not be closed"),
    )

    cleanup = recruiter._cleanup_intake_clerk(ownership)
    assert cleanup["verified_absent"] is True
    assert cleanup["status"] == "already-absent"


def test_pane_id_mismatch_blocks_cleanup_instead_of_closing_foreign_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "mismatch-key"
    token = "e" * 32
    name = recruiter._intake_clerk_agent_name(key, token)
    ownership = {
        "intake_key": key,
        "lease_token": token,
        "agent_name": name,
        "herdr_session": "llm-lab-test",
        "pane": "recorded-old-pane",
    }
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {"agent": {"name": name, "pane_id": "different-pane"}}
        },
    )
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: pytest.fail(
            f"mismatched pane {pane} must not be closed"
        ),
    )

    cleanup = recruiter._cleanup_intake_clerk(ownership)
    assert cleanup["verified_absent"] is False
    assert cleanup["status"] == "cleanup-blocked"


@pytest.mark.parametrize(
    ("detected_agent", "cwd", "processes"),
    [
        ("other-agent", "/trusted", [{"name": "claude", "pid": 12}]),
        ("claude", "/foreign", [{"name": "claude", "pid": 12}]),
        ("claude", "/trusted", [{"name": "foreign", "pid": 12}]),
    ],
)
def test_intake_cleanup_requires_expected_agent_process_and_cwd(
    detected_agent: str,
    cwd: str,
    processes: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "identity-key"
    token = "7" * 32
    name = recruiter._intake_clerk_agent_name(key, token)
    ownership = {
        "intake_key": key,
        "lease_token": token,
        "agent_name": name,
        "pane": "owned-pane",
        "expected_agent": "claude",
        "expected_process": "claude",
        "expected_cwd": "/trusted",
        "herdr_session": "llm-lab-test",
    }

    def herdr_json(*args, **kwargs):
        if args[:2] == ("agent", "get"):
            return {"result": {"agent": {"name": name, "pane_id": "owned-pane"}}}
        if args[:2] == ("pane", "get"):
            return {
                "result": {
                    "pane": {
                        "pane_id": "owned-pane",
                        "agent": detected_agent,
                        "cwd": cwd,
                    }
                }
            }
        if args[:2] == ("pane", "process-info"):
            return {"result": {"process_info": {"foreground_processes": processes}}}
        raise AssertionError(args)

    monkeypatch.setattr(recruiter, "_herdr_json", herdr_json)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: pytest.fail(
            f"unverified pane {pane} must not be closed"
        ),
    )

    cleanup = recruiter._cleanup_intake_clerk(ownership)
    assert cleanup["verified_absent"] is False
    assert cleanup["status"] == "cleanup-blocked"


def test_pid_reuse_does_not_count_as_the_original_intake_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state/recruiter.json"
    state.parent.mkdir()
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    layout = recruiter._prepare_intake_layout()
    attempt = recruiter._new_intake_attempt(layout)
    ownership_path = attempt / "ownership.json"
    key = "pid-reuse-key"
    token = "9" * 32
    recruiter._secure_write_json(
        ownership_path,
        {
            "schema_version": 1,
            "attempt_name": attempt.name,
            "intake_key": key,
            "lease_token": token,
            "agent_name": recruiter._intake_clerk_agent_name(key, token),
            "state": "active",
            "owner_pid": 1234,
            "owner_start_time": "original-start",
            "expires_at": int(time.time()) + 300,
        },
    )
    monkeypatch.setattr(recruiter, "_process_start_time", lambda pid: "reused-start")
    cleaned = []
    monkeypatch.setattr(
        recruiter,
        "_cleanup_intake_clerk",
        lambda ownership: (
            cleaned.append(ownership)
            or {"status": "already-absent", "verified_absent": True}
        ),
    )

    assert recruiter._reconcile_intake_clerks(force=False) == 1
    assert len(cleaned) == 1
    assert recruiter._secure_json(ownership_path)["state"] == "closed"


# --- the specialist roster the kit actually ships -----------------------------
#
# `specialist_roster_contract_test.py` writes its own fixture rosters, so every one of its
# thirteen tests passes green against an EMPTY `specialists.yaml`. These two load the real
# shipped file, which is the only thing that catches "the merge works perfectly and the kit
# ships nothing to merge" — a destination with no overlay of its own would get an empty phone
# book, and an empty phone book reads to a worker as "no specialist owns this area".


_ENGINE = Path(recruiter.__file__).resolve()


def _kit_base_specialists() -> list[dict]:
    """The roster file as SHIPPED, read directly. Not through `load_specialist_roster()`: that
    walks up from cwd and would merge whatever overlay the enclosing repo happens to own."""
    return recruiter.yaml.safe_load(
        _ENGINE.with_name(recruiter.SPECIALIST_ROSTER_FILE).read_text()
    )["specialists"]


def test_the_shipped_kit_base_roster_lists_every_persona_the_kit_agents_compose() -> (
    None
):
    """The kit base is loaded in EVERY destination, so it is the whole phone book wherever a
    repo has not written an overlay. Each entry must name a persona the kit can actually
    launch: `agent:` is what the launch template substitutes, and a name with no compose
    recipe behind it hires a worker with no definition to read."""
    specialists = _kit_base_specialists()
    # .../<tree>/extensions/common/upagent/recruiter.py -> .../<tree>/compose/agents. Identical
    # in the kit and in every destination, both of which carry the composed agent recipes.
    recipes = {
        path.stem for path in (_ENGINE.parents[3] / "compose/agents").glob("*.yaml")
    }

    assert specialists, (
        "the kit ships no specialists — every destination's phone book is empty"
    )
    assert recipes, (
        "no agent compose recipes found; the path this test resolves has moved"
    )
    missing = sorted(
        entry["agent"] for entry in specialists if entry["agent"] not in recipes
    )
    assert not missing, f"specialists name personas the kit does not compose: {missing}"


def test_the_phone_book_caps_the_whole_line_not_just_the_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Found against a real destination roster, not by design.

    The migration gate caps an essay description and checks the rendered line, but its fixture
    specialist is named `essayist` — eight characters. A real repo-owned roster has names like
    `payments-integration-reviewer`, and `- **<29 chars>** (this repo) — ` is 48 characters of
    prefix before the description even starts. Capping the description alone therefore passed
    the gate while emitting 270-character lines into every stage brief. The budget belongs to
    the LINE, because the line is what rides in the brief.
    """
    repo = _specialist_world(tmp_path, monkeypatch)
    overlay = repo / recruiter.SPECIALIST_OVERLAY_REL
    overlay.write_text(
        recruiter.yaml.safe_dump(
            {
                "specialists": [
                    {
                        "name": "payments-integration-reviewer",
                        "description": "long ownership sentence " * 40,
                        "offering": "claude-opus-4-8",
                        "effort": "high",
                        "agent": "payments-integration-reviewer",
                    }
                ]
            }
        )
    )

    recruiter.cmd_specialists()

    rendered = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("- **")
    ]
    assert len(rendered) == 1
    assert len(rendered[0]) < recruiter.PHONE_BOOK_LINE_CAP
    assert rendered[0].endswith("...")
    assert "(this repo)" in rendered[0]


def test_a_specialist_with_no_description_falls_back_to_its_persona_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A roster entry may carry only `location:`. Without the fallback its phone-book line
    renders blank, and a worker cannot tell what the specialist covers — so it picks wrong, or
    not at all. Silent quality loss with no signal, which is why it is pinned here."""
    repo = _specialist_world(tmp_path, monkeypatch)
    overlay = repo / recruiter.SPECIALIST_OVERLAY_REL
    overlay.write_text(
        recruiter.yaml.safe_dump(
            {
                "specialists": [
                    {
                        "name": "reviewer",
                        "location": ".claude/agents/reviewer.md",
                        "offering": "claude-opus-4-8",
                        "effort": "high",
                        "agent": "reviewer",
                    }
                ]
            }
        )
    )
    persona = repo / ".claude/agents/reviewer.md"
    persona.parent.mkdir(parents=True)
    persona.write_text(
        "---\ndescription: Reads the diff and cites it.\nmodel: opus\n---\n\nBody.\n"
    )

    index = recruiter._specialist_index(recruiter.load_specialist_roster())

    assert index["reviewer"]["description"] == "Reads the diff and cites it."


def test_the_shipped_kit_base_roster_satisfies_the_loader_it_ships_for() -> None:
    """Shipping a roster the loader rejects turns every consult in every destination into a
    failure answer. Validate the real file through the real validator, not a fixture."""
    specialists = _kit_base_specialists()

    for entry in specialists:
        recruiter._validate_specialist({**entry, "origin": "kit-base"})
        assert entry.get("description"), (
            f"{entry['name']} has no description to choose it by"
        )


# --- the consult door ---------------------------------------------------------
#
# `specialist_roster_contract_test.py` pins the roster merge and the phone book; these pin the
# door itself. Everything here runs with `cmd_dispatch` replaced, because the door's own
# contract is what it builds and what it does with the answer — the lifecycle underneath it is
# the ordinary one and is covered above.


def _two_reviewers_roster() -> str:
    """A roster document that names `reviewer` twice — a malformed base or overlay."""
    return recruiter.yaml.safe_dump(
        {
            "specialists": [
                {
                    "name": "reviewer",
                    "description": "first",
                    "offering": "claude-opus-4-8",
                    "effort": "high",
                    "agent": "reviewer",
                },
                {
                    "name": "reviewer",
                    "description": "second",
                    "offering": "claude-sonnet-5",
                    "effort": "low",
                    "agent": "other",
                },
            ]
        }
    )


def test_consult_order_binds_same_id_to_the_canonical_payload(tmp_path: Path) -> None:
    artifacts = recruiter.consult_artifact_paths(tmp_path / "consult.json")
    entry = {
        "agent": "reviewer",
        "effort": "low",
        "offering_snapshot": {"harness": "claude", "model": "claude-sonnet-5"},
    }
    base = {
        "consult_id": "consult-1",
        "specialist": "reviewer",
        "question": "What is the contract?",
        "answer_path": str(tmp_path / "answer.json"),
    }
    first = recruiter.build_consult_order(
        base, entry, artifacts, cwd=str(tmp_path), cockpit_pane="pane"
    )
    attached = recruiter.build_consult_order(
        dict(base), entry, artifacts, cwd=str(tmp_path), cockpit_pane="pane"
    )
    changed = recruiter.build_consult_order(
        {**base, "question": "A changed question"},
        entry,
        artifacts,
        cwd=str(tmp_path),
        cockpit_pane="pane",
    )

    assert first == attached
    assert (
        first["artifact_publication"]["consult_payload_sha256"]
        != changed["artifact_publication"]["consult_payload_sha256"]
    )


def test_a_duplicate_specialist_name_in_the_overlay_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same name defined twice in ONE roster file is ambiguous — the last would silently win.
    The loader must fail loud, naming the file, before any base-under-overlay merge."""
    engine = tmp_path / "kit"
    engine.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    overlay = repo / recruiter.SPECIALIST_OVERLAY_REL
    overlay.parent.mkdir(parents=True)
    overlay.write_text(_two_reviewers_roster())
    monkeypatch.setattr(recruiter, "HERE", engine)
    monkeypatch.chdir(repo)

    with pytest.raises(recruiter.RecruiterError, match="defined more than once"):
        recruiter.load_specialist_roster()


def test_a_duplicate_specialist_name_in_the_kit_base_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard covers the kit base file too, reached with a CLEAN overlay present so the base is
    loaded as the base half of the merge. Ambiguity within a file is caught before it is merged
    away, never resolved to the last duplicate."""
    engine = tmp_path / "kit"
    engine.mkdir()
    (engine / recruiter.SPECIALIST_ROSTER_FILE).write_text(_two_reviewers_roster())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    overlay = repo / recruiter.SPECIALIST_OVERLAY_REL
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        recruiter.yaml.safe_dump(
            {
                "specialists": [
                    {
                        "name": "payments",
                        "description": "clean",
                        "offering": "claude-sonnet-5",
                        "effort": "medium",
                        "agent": "payments",
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(recruiter, "HERE", engine)
    monkeypatch.chdir(repo)

    with pytest.raises(recruiter.RecruiterError, match="defined more than once"):
        recruiter.load_specialist_roster()


def _specialist_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **entry_over: object
) -> Path:
    """A repo whose own overlay names one specialist, an up Recruiter, and cwd inside it.

    The engine dir ships no kit base here, so the overlay is the whole roster and `repo_root`
    anchors on the repository that owns it — the arrangement the cwd rule is about.
    """
    engine = tmp_path / "kit"
    engine.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    overlay = repo / recruiter.SPECIALIST_OVERLAY_REL
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        recruiter.yaml.safe_dump(
            {
                "specialists": [
                    {
                        "name": "reviewer",
                        "description": "Independent read-only review.",
                        "offering": "claude-opus-4-8",
                        "effort": "high",
                        "agent": "reviewer",
                        **entry_over,
                    }
                ]
            }
        )
    )
    state = tmp_path / "recruiter-state.json"
    state.write_text(json.dumps({"recruiter_pane": "ws1:%7"}))
    monkeypatch.setattr(recruiter, "HERE", engine)
    monkeypatch.setattr(recruiter, "STATE_FILE", state)
    monkeypatch.chdir(repo)
    return repo


def _consult_file(tmp_path: Path, **over: object) -> Path:
    consult = {
        "consult_id": "phase-2.stage-1.pass-1.consult-1",
        "specialist": "reviewer",
        "question": "Where is the retry budget enforced?",
        "answer_path": str(tmp_path / "answers" / "c1.answer.json"),
        **over,
    }
    path = tmp_path / "consults" / "c1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(consult))
    return path


def _answering_dispatch(answer: dict | None, seen: list[dict]):
    """Stand in for the whole worker lifecycle: record the order, write what the specialist would."""

    def dispatch(order_path: str, roster_path: str) -> int:
        path = Path(order_path)
        seen.append(json.loads(path.read_text()))
        if answer is not None:
            consult = path.with_name(path.name.removesuffix(".order.json"))
            target = Path(json.loads(consult.read_text())["answer_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(answer))
        return 0

    return dispatch


def _receipt(consult_path: Path) -> dict:
    return json.loads(
        consult_path.with_name(consult_path.name + ".receipt.json").read_text()
    )


def _cited_answer(consult_id: str = "phase-2.stage-1.pass-1.consult-1") -> dict:
    return {
        "consult_id": consult_id,
        "answer": "In the leader loop, before each try.",
        "citations": ["recruiter.py:134"],
    }


def test_a_consult_becomes_an_entirely_ordinary_upagent_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order carries NO consult-specific field. `_complete_order_form` rejects any key
    outside ORDER_INTAKE_ALIASES, so a `consult` block here would work until the day intake
    stops short-circuiting a canonical order — a latent failure with a long fuse. The link
    between consult and order lives in the sidecar receipt instead."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), seen)
    )

    assert recruiter.cmd_consult(str(consult), "roster.yaml") == 0

    order = seen[0]
    assert set(order) <= set(recruiter.ORDER_INTAKE_ALIASES) | {"offering_snapshot"}
    assert "consult" not in order
    assert order["offering_snapshot"]["id"] == "claude-opus-4-8"
    recruiter.contracts.parse_order(
        json.dumps(order)
    )  # must satisfy the ordinary contract
    assert order["agent"] == "reviewer"
    assert order["timeout_ms"] == recruiter.CONSULT_TIMEOUT_MS
    assert order["cockpit_pane"] == "ws1:%7"
    assert "management" not in order


def test_a_consult_order_is_not_filed_under_the_callers_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_phase_start_receipt` walks a brief's path for `phases/<phase_id>/pass-N/`. Reusing the
    caller's phase id would make every consult whose brief sits inside a phase tree emit a
    spurious `phase-receipt-degraded` event — noise that erodes a real signal."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), seen)
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    assert seen[0]["phase_id"] == recruiter.CONSULT_PHASE_ID == "consult"
    assert recruiter._phase_start_receipt(seen[0]) is None


def test_an_uncited_answer_is_refused_and_overwritten_with_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE EVIDENCE GATE, exercised through the door rather than through the contract alone.
    A specialist that delivers confident prose has run and delivered — its `result.json` is
    perfectly valid — so nothing but this refuses it."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)
    uncited = {"consult_id": "phase-2.stage-1.pass-1.consult-1", "answer": "Trust me."}
    monkeypatch.setattr(recruiter, "cmd_dispatch", _answering_dispatch(uncited, []))

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["answer_verdict"] == "rejected"
    assert "citations" in receipt["reason"]
    assert json.loads(Path(receipt["answer_path"]).read_text())["error"]


def test_a_specialist_signalled_failure_is_recorded_as_failed_not_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A specialist saying "I could not answer" is a legitimate terminal outcome and needs no
    citations. Collapsing it into `rejected` would lose the distinction between the specialist
    reporting a failure and the door refusing its evidence."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)
    signalled = {
        "consult_id": "phase-2.stage-1.pass-1.consult-1",
        "error": "repo unreadable",
    }
    monkeypatch.setattr(recruiter, "cmd_dispatch", _answering_dispatch(signalled, []))

    recruiter.cmd_consult(str(consult), "roster.yaml")

    assert _receipt(consult)["answer_verdict"] == "failed"


def test_a_consult_that_never_reaches_a_worker_still_answers_its_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ALWAYS-ANSWER GUARANTEE. A caller's wait is bounded by the door returning, so every
    failure path — including one where no worker ever ran — must leave a durable artifact
    rather than a missing file the caller cannot distinguish from a hang."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)

    def exploding(order_path: str, roster_path: str) -> int:
        raise RecruiterError("herdr socket is gone")

    monkeypatch.setattr(recruiter, "cmd_dispatch", exploding)

    assert recruiter.cmd_consult(str(consult), "roster.yaml") == 0

    receipt = _receipt(consult)
    assert receipt["answer_verdict"] == "rejected"
    answer = json.loads(Path(receipt["answer_path"]).read_text())
    assert "herdr socket is gone" in answer["error"]
    assert recruiter.contracts_consult.parse_answer(json.dumps(answer)) == answer


def test_a_bad_roster_still_answers_instead_of_stranding_the_consult(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roster load lives INSIDE the recoverable block on purpose: a consult must be
    answerable even when the thing that routes it is broken, or the caller only ever resolves
    on timeout."""
    engine = tmp_path / "kit"
    engine.mkdir()
    (engine / "specialists.yaml").write_text("specialists: []\n")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setattr(recruiter, "HERE", engine)
    monkeypatch.chdir(tmp_path / "repo")
    consult = _consult_file(tmp_path)

    recruiter.cmd_consult(str(consult), "roster.yaml")

    assert "non-empty `specialists:` list" in _receipt(consult)["reason"]


def test_an_unknown_specialist_is_refused_with_the_roster_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path, specialist="nobody")

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["answer_verdict"] == "rejected"
    assert "reviewer" in receipt["reason"]


def test_a_near_miss_specialist_name_is_resolved_deterministically_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guess the FORM, never the intent. A capitalisation must not cost an LLM hire or a
    failure answer — and the interpretation is written into the receipt, so a resolved name is
    never a silent rename."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path, specialist="Reviewer")
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), seen)
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["resolved_specialist"] == "reviewer"
    assert "matched 'reviewer' (case)" in receipt["resolution_note"]
    assert seen[0]["agent"] == "reviewer"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("reviewer", "reviewer"),
        ("Reviewer", "reviewer"),
        ("REVIEWER", "reviewer"),
        ("reviewer-agent", "reviewer"),
        ("payments", None),
        ("", None),
    ],
)
def test_specialist_name_resolution_is_deterministic(
    value: str, expected: str | None
) -> None:
    assert recruiter._resolve_specialist_name(value, ["reviewer", "qa"])[0] == expected


def test_an_ambiguous_specialist_name_is_never_guessed() -> None:
    """Two roster names that normalize to one string is exactly when a resolver must stop.
    Picking either would answer a question the caller did not ask."""
    assert recruiter._resolve_specialist_name("Docs", ["docs", "DOCS"]) == (None, None)


def test_a_consult_runs_in_the_directory_the_caller_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _specialist_world(tmp_path, monkeypatch)
    elsewhere = tmp_path / "worktree"
    elsewhere.mkdir()
    consult = _consult_file(tmp_path, cwd=str(elsewhere))
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), seen)
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    assert seen[0]["cwd"] == str(elsewhere)


def test_a_consult_naming_a_vanished_directory_falls_back_to_the_rosters_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fe96fba`, restated as a rule. Bring services up in a throwaway worktree, delete it, and
    every later consult used to die starting a process in a directory that no longer existed.
    The fallback is the roster's own repository, re-derived on this load — live by construction
    rather than a path recorded earlier — and the specialist answers about a tree that exists."""
    repo = _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path, cwd=str(tmp_path / "deleted-worktree"))
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), seen)
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    assert seen[0]["cwd"] == str(repo)
    assert _receipt(consult)["cwd"] == str(repo)


def test_a_stale_answer_from_a_previous_consult_is_removed_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reusing an answer_path is legal; reading the previous occupant's answer is not. The
    consult_id echo catches it afterwards, but only if the stale file is gone first — otherwise
    a worker that never wrote anything looks answered."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)
    stale = Path(json.loads(consult.read_text())["answer_path"])
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps(_cited_answer("an-older-consult")))
    monkeypatch.setattr(recruiter, "cmd_dispatch", _answering_dispatch(None, []))

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["answer_verdict"] == "rejected"
    assert "not found" in receipt["reason"]


def test_the_brief_states_the_citation_requirement_the_answer_is_judged_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The specialist is judged on citations, so it has to be TOLD about citations. The brief
    and `parse_answer` must not drift apart — a worker refused for a rule it never read is a
    guaranteed retry loop."""
    _specialist_world(tmp_path, monkeypatch, location=".claude/agents/reviewer.md")
    consult = _consult_file(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), seen)
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    brief = Path(seen[0]["instructions_path"]).read_text()
    assert "MUST carry a real file:line citation" in brief
    assert json.loads(consult.read_text())["answer_path"] not in brief
    assert "lease-private answer path" in brief
    assert "Recruiter delivery contract" in brief
    assert str(tmp_path / "repo" / ".claude/agents/reviewer.md") in brief


def test_a_consult_id_that_could_escape_a_path_becomes_a_safe_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller owns `consult_id` and it reaches the ledger as an order id, so it is digested
    rather than carried. A digest is safe by construction whatever the caller wrote."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path, consult_id="../../etc/passwd")
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter,
        "cmd_dispatch",
        _answering_dispatch({"consult_id": "../../etc/passwd", "error": "n/a"}, seen),
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    assert recruiter._SAFE_ORDER_ID_RE.fullmatch(seen[0]["order_id"])
    assert seen[0]["order_id"] == seen[0]["request_id"]
    assert ".." not in seen[0]["order_id"]


def test_a_consult_too_broken_to_answer_into_refuses_in_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path with no durable artifact to leave is the one path that may refuse: with no
    `answer_path` there is nowhere for a failure answer to go, so the caller has to be told
    directly, with what was missing named."""
    _specialist_world(tmp_path, monkeypatch)
    path = tmp_path / "consults" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all")

    with pytest.raises(RecruiterError, match="answer_path"):
        recruiter.cmd_consult(str(path), "roster.yaml")


def test_a_malformed_consult_that_names_an_answer_path_is_answered_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consult missing `question` cannot run, but it CAN be answered: the caller gets a
    failure naming the missing field and listing the roster, instead of a Python refusal it
    would have to parse out of stderr."""
    _specialist_world(tmp_path, monkeypatch)
    path = tmp_path / "consults" / "partial.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "consult_id": "c-9",
                "answer_path": str(tmp_path / "a.json"),
                "specialist": "reviewer",
            }
        )
    )

    assert recruiter.cmd_consult(str(path), "roster.yaml") == 0

    answer = json.loads((tmp_path / "a.json").read_text())
    assert "question" in answer["error"]
    assert "reviewer" in answer["error"]
    assert recruiter.contracts_consult.parse_answer(json.dumps(answer), "c-9") == answer


def test_the_receipt_names_the_ordinary_request_identity_the_ledger_knows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt is derived, never invented: every field traces to an ordinary UpAgent request
    id or result path. That is what makes a worker's `consults` claim resolvable rather than
    self-reported prose."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(
        tmp_path, requested_by="phase-2.stage-1-implementation.pass-1.try-1"
    )
    seen: list[dict] = []
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), seen)
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["answer_verdict"] == "cited"
    assert receipt["request_id"] == seen[0]["request_id"] == seen[0]["order_id"]
    assert receipt["requested_by"] == "phase-2.stage-1-implementation.pass-1.try-1"
    assert receipt["result_path"] == seen[0]["result_path"]
    assert receipt["order_receipt_state"] == "finished"


def test_the_consult_prints_one_machine_readable_receipt_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command returning IS the completion signal, so stdout carries evidence rather than a
    sentinel to wait for. One line, parseable, emitted on every path."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), [])
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    lines = [
        ln
        for ln in capsys.readouterr().out.splitlines()
        if ln.startswith("CONSULT_RECEIPT ")
    ]
    assert len(lines) == 1
    assert (
        json.loads(lines[0].removeprefix("CONSULT_RECEIPT "))["answer_verdict"]
        == "cited"
    )


# --- consult receipts a worker cannot forge (Tier 2) --------------------------
#
# A `consults` list used to be four keys of prose that nothing in the system ever read:
# `contracts.parse_result` did not look at it, and no ledger lookup resolved it. A worker could
# therefore bank a receipt for a consultation that never happened, which is the failure mode
# project memory records as "workers forged librarian paperwork". These pin the resolution.


def _worker_order(tmp_path: Path) -> dict:
    return _order(
        order_id="phase-2.stage-1-implementation.pass-1.try-1",
        cwd=str(tmp_path),
        instructions_path=str(tmp_path / "instructions.md"),
        result_path=str(tmp_path / "result.json"),
    )


def _claim(**over: str) -> dict:
    base = {
        "consult_id": "phase-2.stage-1.pass-1.consult-1",
        "specialist": "reviewer",
        "request_id": recruiter.consult_request_id("phase-2.stage-1.pass-1.consult-1"),
        "answer_path": "/tmp/answers/c1.answer.json",
    }
    base.update(over)
    return base


def _worker_result(claims: list[dict], order: dict) -> dict:
    return {
        "order_id": order["order_id"],
        "verdict": "passed",
        "full_log": "/tmp/session.jsonl",
        "consults": claims,
    }


def _broker_a_real_consult(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: dict, **over: object
) -> None:
    """Run one consult end to end so the Recruiter's own index records it, exactly as production
    does — through `cmd_consult`, never by writing the index entry directly. A test that forged
    the index would prove only that the reader reads."""
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path, requested_by=order["order_id"], **over)
    consult_id = json.loads(consult.read_text())["consult_id"]
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(consult_id), [])
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")


def test_a_worker_cannot_bank_a_receipt_for_a_consult_that_never_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE FORGERY GATE.

    This worker writes a perfectly well-formed `consults` entry — four non-empty strings, a
    correctly derived `request_id`, everything `contracts.parse_result` asks for — for a
    consultation it never made. Nothing about the claim itself is detectably wrong; the only
    thing that distinguishes it from a real one is that the Recruiter has no record of having
    brokered it. Publication must say so.
    """
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    order = _worker_order(tmp_path)
    forged = _worker_result([_claim()], order)

    stamp = recruiter.resolve_consult_claims(order, forged)

    assert stamp["consults_verified"] == []
    assert stamp["consults_unverified"] == [
        {
            "consult_id": "phase-2.stage-1.pass-1.consult-1",
            "request_id": _claim()["request_id"],
        }
    ]


def test_a_consult_that_really_happened_is_verified_from_the_hubs_own_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. Without it the gate above could be a check that never passes — everything
    would read as forged and the distinction would be worthless."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    order = _worker_order(tmp_path)
    _broker_a_real_consult(tmp_path, monkeypatch, order)

    stamp = recruiter.resolve_consult_claims(order, _worker_result([_claim()], order))

    assert stamp["consults_unverified"] == []
    assert stamp["consults_verified"] == [
        {
            "answer_verdict": "cited",
            "consult_id": "phase-2.stage-1.pass-1.consult-1",
            "request_id": _claim()["request_id"],
            "specialist": "reviewer",
        }
    ]


def test_a_consult_another_worker_made_cannot_be_claimed_as_your_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index is keyed by REQUESTER, not just by consult id. Otherwise one real consult
    anywhere in the run would launder every worker's claim to have made it."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    mine = _worker_order(tmp_path)
    theirs = _order(order_id="phase-2.stage-1-implementation.pass-1.try-2")
    _broker_a_real_consult(tmp_path, monkeypatch, theirs)

    assert (
        recruiter.resolve_consult_claims(mine, _worker_result([_claim()], mine))[
            "consults_verified"
        ]
        == []
    )
    assert recruiter.resolve_consult_claims(theirs, _worker_result([_claim()], theirs))[
        "consults_verified"
    ]


def test_a_real_consult_claimed_under_a_borrowed_request_id_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of the claim are checked against the record, not just the one in the path.
    Naming a consult that happened while attaching a different request id is still a false
    claim — the `request_id` is what the auditor would follow back to the ledger."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    order = _worker_order(tmp_path)
    _broker_a_real_consult(tmp_path, monkeypatch, order)

    borrowed = _worker_result([_claim(request_id="consult-somebodyelses")], order)

    assert recruiter.resolve_consult_claims(order, borrowed)["consults_verified"] == []


def test_a_worker_that_recorded_no_consults_key_gets_no_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent is not the same as empty. A worker that declared `consults: []` said something —
    "none applied" — and gets an explicit empty verdict; one that omitted the key never made a
    claim to resolve, and the receipt stays silent rather than inventing agreement."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    order = _worker_order(tmp_path)
    silent = {k: v for k, v in _worker_result([], order).items() if k != "consults"}

    assert recruiter.resolve_consult_claims(order, silent) == {}
    assert recruiter.resolve_consult_claims(order, _worker_result([], order)) == {
        "consults_verified": [],
        "consults_unverified": [],
    }


def test_a_consult_with_no_requester_is_unverifiable_but_never_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`requested_by` is worker-supplied, so a worker can omit it and lose attribution for a
    consult it really made. That fails in the SAFE direction: omission understates diligence,
    it can never overstate it. A worker cannot manufacture an entry by leaving fields out."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    order = _worker_order(tmp_path)
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path)  # no requested_by
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(_cited_answer(), [])
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    assert "index_path" not in _receipt(consult)
    assert (
        recruiter.resolve_consult_claims(order, _worker_result([_claim()], order))[
            "consults_verified"
        ]
        == []
    )


def test_a_consult_that_failed_before_any_specialist_ran_is_not_verifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consult that never reached a specialist worker cannot verify a worker's claim.

    An unknown specialist is rejected BEFORE dispatch, so no worker ran and no durable
    ORDER_RECEIPT was ever produced. The rejected receipt still carries `requested_by`, and that
    alone used to put it in the Recruiter's index — laundering a `consults` claim for a consultation
    that never happened. Only a consult whose order reached `order_receipt_state == "finished"`
    may enter the verified index.
    """
    order = _worker_order(tmp_path)
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(
        tmp_path, requested_by=order["order_id"], specialist="ghostwriter"
    )
    monkeypatch.setattr(
        recruiter,
        "cmd_dispatch",
        lambda *a, **k: pytest.fail(
            "an unknown specialist must fail before any worker runs"
        ),
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["answer_verdict"] == "rejected"
    assert (
        "order_receipt_state" not in receipt
    )  # the order never finished — no worker ran
    assert "index_path" not in receipt  # so nothing was recorded to verify against
    stamp = recruiter.resolve_consult_claims(order, _worker_result([_claim()], order))
    assert stamp["consults_verified"] == []
    assert stamp["consults_unverified"] == [
        {
            "consult_id": "phase-2.stage-1.pass-1.consult-1",
            "request_id": _claim()["request_id"],
        }
    ]


def test_a_consult_that_never_reached_the_recruiter_is_not_verifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guarantee through the other pre-dispatch door. With no Recruiter pane the consult
    is rejected before a worker could run, so its `requested_by` receipt must stay out of the
    index exactly as the unknown-specialist case does."""
    order = _worker_order(tmp_path)
    _specialist_world(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "recruiter-is-down.json")
    consult = _consult_file(
        tmp_path, requested_by=order["order_id"]
    )  # names the known reviewer
    monkeypatch.setattr(
        recruiter,
        "cmd_dispatch",
        lambda *a, **k: pytest.fail(
            "a down Recruiter must fail before any worker runs"
        ),
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["answer_verdict"] == "rejected"
    assert "order_receipt_state" not in receipt
    assert "index_path" not in receipt
    assert (
        recruiter.resolve_consult_claims(order, _worker_result([_claim()], order))[
            "consults_verified"
        ]
        == []
    )


def test_a_planted_index_entry_with_no_finished_order_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth for the forgery gate's read side. Even if some future path wrote an index
    entry for a consult whose order never finished, verification refuses it: a verified entry has
    to record `order_receipt_state == "finished"`, which only a completed dispatch ever sets."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    order = _worker_order(tmp_path)
    claim = _claim()
    entry_path = recruiter.consult_index_entry_path(
        order["order_id"], claim["consult_id"]
    )
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(
        json.dumps(
            {
                "consult_id": claim["consult_id"],
                "request_id": claim["request_id"],
                "requested_by": order["order_id"],
                "answer_verdict": "cited",
                # note: no order_receipt_state — this consult never ran to completion
            }
        )
    )

    stamp = recruiter.resolve_consult_claims(order, _worker_result([claim], order))

    assert stamp["consults_verified"] == []
    assert stamp["consults_unverified"] == [
        {"consult_id": claim["consult_id"], "request_id": claim["request_id"]}
    ]


def test_a_consult_whose_answer_failed_the_citation_gate_is_verifiable_as_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate OTHER side of the gate — the CITATION path specifically. A consult whose
    specialist RAN and returned a SUCCESS-shaped answer that the citation gate rejected (a citation
    that is not `file:line`) still finished its order, so it stays verifiable — carrying the
    `rejected` verdict the gate assigned (`parse_answer` raises `ConsultError`, which `cmd_consult`
    records as `rejected`, NOT `failed`). The gate excludes only a consult that never reached a
    specialist, never one that ran and produced an uncited answer."""
    order = _worker_order(tmp_path)
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path, requested_by=order["order_id"])
    consult_id = json.loads(consult.read_text())["consult_id"]
    # A SUCCESS answer (no `error`) whose citation is not `file:line` — this is what the citation
    # gate exists to reject, distinct from a specialist-signaled failure envelope.
    uncited_success = {
        "consult_id": consult_id,
        "answer": "In the leader loop, before each try.",
        "citations": ["foo.py"],  # no `:line` — fails CITATION_RE
    }
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(uncited_success, [])
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["order_receipt_state"] == "finished"  # the worker ran
    assert (
        receipt["answer_verdict"] == "rejected"
    )  # the citation gate rejected the uncited answer
    assert "index_path" in receipt  # still indexed, because it ran
    stamp = recruiter.resolve_consult_claims(order, _worker_result([_claim()], order))
    assert stamp["consults_verified"] == [
        {
            "answer_verdict": "rejected",
            "consult_id": consult_id,
            "request_id": _claim()["request_id"],
            "specialist": "reviewer",
        }
    ]


def test_a_consult_whose_specialist_signaled_failure_is_verifiable_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinct post-run case: the specialist itself SIGNALED failure via the `error` envelope,
    which `parse_answer` accepts WITHOUT applying the citation gate, so the verdict is `failed` (not
    the citation gate's `rejected`). The order still finished, so this too stays verifiable — and
    the two verdicts must not be conflated."""
    order = _worker_order(tmp_path)
    _specialist_world(tmp_path, monkeypatch)
    consult = _consult_file(tmp_path, requested_by=order["order_id"])
    consult_id = json.loads(consult.read_text())["consult_id"]
    signaled_failure = recruiter.contracts_consult.failure_answer(
        consult_id, "could not determine"
    )
    monkeypatch.setattr(
        recruiter, "cmd_dispatch", _answering_dispatch(signaled_failure, [])
    )

    recruiter.cmd_consult(str(consult), "roster.yaml")

    receipt = _receipt(consult)
    assert receipt["order_receipt_state"] == "finished"  # the worker ran
    assert (
        receipt["answer_verdict"] == "failed"
    )  # specialist-signaled failure, NOT the citation gate
    assert "index_path" in receipt  # still indexed, because it ran
    stamp = recruiter.resolve_consult_claims(order, _worker_result([_claim()], order))
    assert stamp["consults_verified"] == [
        {
            "answer_verdict": "failed",
            "consult_id": consult_id,
            "request_id": _claim()["request_id"],
            "specialist": "reviewer",
        }
    ]


def test_a_caller_controlled_consult_id_cannot_escape_the_hubs_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`consult_id` is written by the caller and becomes a path segment. Both segments are
    digests for exactly this reason — a raw id would let a consult write anywhere under (or
    above) the Recruiter's own state root."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")

    entry = recruiter.consult_index_entry_path("worker-1", "../../../../etc/passwd")

    assert ".." not in str(entry)
    assert recruiter.consult_index_dir() in entry.parents


def test_a_publication_stamps_the_receipt_the_stage_2_auditor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real publication path, not the resolver alone: the auditor reads
    the RECEIPT, so the stamp has to survive `JobLedger.finalize` and reach it."""
    monkeypatch.setattr(recruiter, "STATE_FILE", tmp_path / "state/recruiter.json")
    order = _worker_order(tmp_path)
    _broker_a_real_consult(tmp_path, monkeypatch, order)
    ledger = recruiter.JobLedger(tmp_path / "hub")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key, order["order_id"], 60_000, owner={"runner_pid": os.getpid()}
    )
    assert token is not None
    result = _worker_result([_claim(), _claim(consult_id="never-happened")], order)

    _finalize(
        ledger,
        key,
        token,
        order,
        result,
        cleanup={"status": "closed", "worker_pane": None, "verified_absent": True},
    )

    receipt = ledger.completed_receipt(key, order)
    assert [c["consult_id"] for c in receipt["consults_verified"]] == [
        "phase-2.stage-1.pass-1.consult-1"
    ]
    assert [c["consult_id"] for c in receipt["consults_unverified"]] == [
        "never-happened"
    ]


def test_a_malformed_consults_claim_is_refused_by_the_result_contract(
    tmp_path: Path,
) -> None:
    """Tier 1: shape. `parse_result` is pure and has no ledger, so it can say a claim is WELL
    FORMED but never that it is TRUE — that is the resolver's job above. What it can do is
    refuse bookkeeping that could not be resolved by anyone, which is what an entry missing its
    `request_id` is."""
    order = _worker_order(tmp_path)
    missing = _claim()
    del missing["request_id"]

    with pytest.raises(ContractError, match="request_id"):
        recruiter.contracts.parse_result(json.dumps(_worker_result([missing], order)))
    with pytest.raises(ContractError, match="consults"):
        recruiter.contracts.parse_result(
            json.dumps({**_worker_result([], order), "consults": "reviewer"})
        )


def test_a_wellformed_consults_list_still_passes_the_result_contract(
    tmp_path: Path,
) -> None:
    """The shape check must not become a reason a real result is thrown away."""
    order = _worker_order(tmp_path)

    parsed = recruiter.contracts.parse_result(
        json.dumps(_worker_result([_claim()], order))
    )

    assert parsed["consults"] == [_claim()]


def test_requester_control_proof_is_persisted_hashed_and_fences_the_old_lease(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(request_id="request-1")
    key, _ = ledger.submit(order)
    old_token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "herdr_session": "test-session",
            "request_id": "request-1",
            "runner_pid": -1,
        },
    )
    assert isinstance(old_token, str)
    control_token = ledger.state(key)["requester_control_token"]
    assert isinstance(control_token, str)
    control_text = ledger.control_path(key).read_text()
    assert control_token not in control_text
    assert ledger.verify_control_token(key, control_token)["generation"] == 1
    with pytest.raises(RecruiterError, match="control token"):
        ledger.begin_cancel(key, "wrong")

    cancellation = ledger.begin_cancel(key, control_token)

    assert cancellation["terminal"] is False
    assert cancellation["token"] != old_token
    active_lease = ledger._lease(ledger.active / "requests" / key / "lease.json")
    assert active_lease["token"] == cancellation["token"]
    assert active_lease["token"] != old_token
    assert (
        ledger.active
        / "by-expiry"
        / str(active_lease["expires_at"])
        / f"{key}-{active_lease['token']}.json"
    ).is_file()
    assert ledger.state(key)["state"] == "cancelling"


def test_fenced_runner_cannot_overwrite_cancellation_launch_cleanup(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(request_id="request-fenced-cleanup")
    key, _ = ledger.submit(order)
    old_token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "herdr_session": "test-session",
            "request_id": "request-fenced-cleanup",
            "runner_pid": -1,
        },
    )
    assert isinstance(old_token, str)
    launch_id = ledger.begin_launch(
        key,
        old_token,
        "worker",
        "owned-agent",
        "test-session",
        order["cwd"],
    )
    ledger.record_launch_created(
        key, old_token, launch_id, "owned-pane", "workspace", "address"
    )
    assert ledger.mark_launch_started(
        key, old_token, launch_id, "owned-pane", "workspace", "address"
    )
    control_token = ledger.state(key)["requester_control_token"]
    cancellation = ledger.begin_cancel(key, control_token)
    assert cancellation["token"] != old_token
    assert ledger.mark_launch_closed(
        key,
        launch_id,
        "owned-pane",
        {"status": "closed", "verified_absent": True},
    )

    assert not ledger.mark_launch_closed(
        key,
        launch_id,
        "owned-pane",
        {
            "status": "cleanup-pending",
            "verified_absent": False,
            "reason": "stale runner write",
        },
        expected_lease_token=old_token,
    )

    assert ledger.mark_launch_closed(
        key,
        launch_id,
        "owned-pane",
        {
            "status": "cleanup-pending",
            "verified_absent": False,
            "reason": "late reconciliation downgrade",
        },
    )
    journal = json.loads(ledger.launch_journal_path(key, launch_id).read_text())
    assert journal["state"] == "closed"
    assert journal["cleanup"]["verified_absent"] is True
    assert "stale runner write" not in json.dumps(journal)


def test_cross_process_cancel_waits_for_live_launch_owner(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(request_id="request-cross-process")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "herdr_session": "test-session",
            "request_id": "request-cross-process",
            "runner_pid": -1,
        },
    )
    assert isinstance(token, str)
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    launch_queue = context.Queue()

    def launch_owner() -> None:
        child_ledger = recruiter.JobLedger(tmp_path / "hub")
        launch_id = child_ledger.begin_launch(
            key,
            token,
            "worker",
            "owned-agent",
            "test-session",
            order["cwd"],
        )
        launch_queue.put(launch_id)
        ready.set()
        assert release.wait(timeout=5)
        child_ledger.record_launch_created(
            key, token, launch_id, "owned-pane", "workspace", "address"
        )
        assert not child_ledger.mark_launch_started(
            key, token, launch_id, "owned-pane", "workspace", "address"
        )
        assert child_ledger.mark_launch_closed(
            key,
            launch_id,
            "owned-pane",
            {"status": "closed", "verified_absent": True},
        )

    process = context.Process(target=launch_owner)
    process.start()
    assert ready.wait(timeout=5)
    launch_id = launch_queue.get(timeout=2)
    control_token = ledger.state(key)["requester_control_token"]
    ledger.begin_cancel(key, control_token)
    waiter_done = threading.Event()
    errors: list[BaseException] = []

    def wait_for_launch() -> None:
        try:
            recruiter._await_inflight_launches(ledger, key, timeout_seconds=5)
        except BaseException as error:
            errors.append(error)
        finally:
            waiter_done.set()

    waiter = threading.Thread(target=wait_for_launch)
    waiter.start()
    assert not waiter_done.wait(timeout=0.15)
    assert not (ledger.request_dir(key) / "receipt.json").exists()
    pending = recruiter._reconcile_exact_launch(
        ledger,
        key,
        launch_id,
        known_pane=None,
        herdr_session="test-session",
        allow_not_found_absent=True,
    )
    assert pending["status"] == "launch-in-flight"
    assert pending["verified_absent"] is False
    assert (
        json.loads(ledger.launch_journal_path(key, launch_id).read_text())["state"]
        == "launching"
    )
    release.set()
    process.join(timeout=5)
    waiter.join(timeout=5)
    assert process.exitcode == 0
    assert errors == []
    assert (
        json.loads(ledger.launch_journal_path(key, launch_id).read_text())["state"]
        == "closed"
    )


def test_exact_launch_reconciliation_never_closes_an_unverified_live_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(request_id="request-2")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "herdr_session": "test-session",
            "request_id": "request-2",
            "runner_pid": -1,
        },
    )
    assert isinstance(token, str)
    launch_id = ledger.begin_launch(
        key,
        token,
        "worker",
        "owned-agent",
        "test-session",
        order["cwd"],
    )
    ledger.record_launch_created(
        key, token, launch_id, "foreign-pane", "workspace", "address"
    )
    closed: list[str] = []

    def missing_agent(*_args: object, **_kwargs: object) -> dict:
        raise RecruiterError("not found")

    monkeypatch.setattr(recruiter, "_herdr_json", missing_agent)
    monkeypatch.setattr(
        recruiter,
        "_live_pane_ids",
        lambda **_kwargs: {"foreign-pane"},
    )
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **_kwargs: closed.append(pane) or {"verified_absent": True},
    )

    cleanup = recruiter._reconcile_exact_launch(
        ledger,
        key,
        launch_id,
        known_pane="foreign-pane",
        herdr_session="test-session",
        allow_not_found_absent=True,
        attempts=1,
    )

    assert cleanup["verified_absent"] is False
    assert "identity-verified" in cleanup["reason"]
    assert closed == []


# --- pane-gone classification in the agent-status wait --------------------


class _FakeWaitProcess:
    """Stands in for the `herdr wait agent-status` subprocess."""

    def __init__(self, returncode: int | None = None, stderr: str = "") -> None:
        self.returncode = returncode
        self._stderr = stderr
        self.terminated = False

    def poll(self) -> int | None:
        if self.terminated:
            self.returncode = -15
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return ("", self._stderr)


def _patch_wait_plumbing(monkeypatch, process: _FakeWaitProcess) -> None:
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(
        recruiter,
        "_herdr_argv",
        lambda args, session: (session, ["herdr", *args]),
    )
    monkeypatch.setattr(recruiter.subprocess, "Popen", lambda *args, **kwargs: process)


def test_interactive_done_is_not_terminal_without_durable_artifacts(
    monkeypatch,
) -> None:
    """Interactive completion never opens the Herdr done subscription."""
    finalized = threading.Event()
    finalized.set()
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail(
            "interactive workers must not subscribe to turn-level done"
        ),
    )

    assert (
        recruiter._wait_for_agent_status(
            "w1:p1",
            5_000,
            finalized,
            completion_style="interactive",
            expected_process="claude",
            herdr_session="test-session",
        )
        is False
    )


def test_interactive_wait_blocks_on_artifact_event_until_next_liveness_probe(
    monkeypatch,
) -> None:
    waits: list[float] = []

    class FinalizedOnWait:
        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            return True

    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 15.0)
    assert (
        recruiter._wait_for_agent_status(
            "w1:p1",
            30_000,
            FinalizedOnWait(),
            completion_style="interactive",
            expected_process="claude",
            herdr_session="test-session",
        )
        is False
    )
    assert len(waits) == 1
    assert waits[0] > 10.0


def test_interactive_wait_ends_on_proven_process_exit(monkeypatch) -> None:
    finalized = threading.Event()
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.0)
    monkeypatch.setattr(recruiter, "_worker_pane_confirmed_gone", lambda *a, **k: False)
    probes: list[tuple[str, str]] = []

    def process_gone(pane: str, expected: str, **_kwargs: object) -> bool:
        probes.append((pane, expected))
        return True

    monkeypatch.setattr(recruiter, "_worker_process_confirmed_gone", process_gone)

    assert (
        recruiter._wait_for_agent_status(
            "w1:p2",
            5_000,
            finalized,
            completion_style="interactive",
            expected_process="claude",
            herdr_session="test-session",
        )
        is True
    )
    assert probes == [("w1:p2", "claude")]


def test_wait_falls_through_when_pane_already_gone(monkeypatch) -> None:
    """Pane gone at wait start: herdr fails fast with a decode error, and a positive pane
    probe turns that into 'inspect the staged result' instead of blocked infrastructure."""
    process = _FakeWaitProcess(returncode=1, stderr="internal_error decoding pane get")
    _patch_wait_plumbing(monkeypatch, process)
    monkeypatch.setattr(
        recruiter,
        "_worker_pane_confirmed_gone",
        lambda pane, herdr_session=None, confirmations=1: True,
    )
    assert recruiter._wait_for_agent_status("w1:p1", 5_000, None) is True


def test_wait_still_raises_when_pane_probe_says_alive(monkeypatch) -> None:
    """A non-zero herdr exit with the pane still present stays a loud RecruiterError —
    classification comes from the probe, never from the exit code alone."""
    process = _FakeWaitProcess(returncode=1, stderr="internal_error decoding pane get")
    _patch_wait_plumbing(monkeypatch, process)
    monkeypatch.setattr(
        recruiter,
        "_worker_pane_confirmed_gone",
        lambda pane, herdr_session=None, confirmations=1: False,
    )
    with pytest.raises(recruiter.RecruiterError, match="failed"):
        recruiter._wait_for_agent_status("w1:p1", 5_000, None)


def test_wait_probe_ends_mid_wait_silence(monkeypatch) -> None:
    """Herdr 0.7.1 goes silent when the pane vanishes mid-wait; two consecutive positive
    probes end the wait instead of burning the remaining order budget (the ~2 h loss)."""
    process = _FakeWaitProcess(returncode=None)
    _patch_wait_plumbing(monkeypatch, process)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.01)
    probes: list[str] = []

    def gone(pane, herdr_session=None, confirmations=1):
        probes.append(pane)
        return True

    monkeypatch.setattr(recruiter, "_worker_pane_confirmed_gone", gone)
    assert recruiter._wait_for_agent_status("w1:p2", 60_000, None) is True
    assert len(probes) >= 2
    assert process.terminated


def test_process_confirmed_gone_requires_normal_repeated_absence(monkeypatch) -> None:
    responses: list[dict[str, object] | Exception] = []

    def fake_herdr_json(*_args: object, **_kwargs: object) -> dict[str, object]:
        answer = responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)
    monkeypatch.setattr(recruiter.time, "sleep", lambda _seconds: None)
    absent: dict[str, object] = {
        "result": {"process_info": {"foreground_processes": []}}
    }
    live: dict[str, object] = {
        "result": {
            "process_info": {
                "foreground_processes": [
                    {"name": "bash", "argv": ["bash", "-lc", "claude --model fable"]}
                ]
            }
        }
    }

    responses.clear()
    responses.extend((absent, absent))
    assert (
        recruiter._worker_process_confirmed_gone("w1:p1", "claude", confirmations=2)
        is True
    )
    responses.clear()
    responses.extend((absent, live))
    assert (
        recruiter._worker_process_confirmed_gone("w1:p1", "claude", confirmations=2)
        is False
    )
    responses.clear()
    responses.extend(
        (
            recruiter.RecruiterError("pane_not_found"),
            recruiter.RecruiterError("pane_not_found"),
        )
    )
    assert (
        recruiter._worker_process_confirmed_gone("w1:p1", "claude", confirmations=2)
        is True
    )
    responses.clear()
    responses.append(recruiter.RecruiterError("socket unavailable"))
    assert recruiter._worker_process_confirmed_gone("w1:p1", "claude") is False


def test_pane_confirmed_gone_trusts_only_positive_answers(monkeypatch) -> None:
    responses: list[object] = []

    def fake_herdr_json(*args, timeout_seconds=None, herdr_session=None):
        answer = responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(recruiter, "_herdr_json", fake_herdr_json)

    responses[:] = [recruiter.RecruiterError("pane get w1:p1 failed: pane_not_found")]
    assert recruiter._worker_pane_confirmed_gone("w1:p1") is True

    # A transport fault is NOT pane-gone.
    responses[:] = [
        recruiter.RecruiterError("herdr pane get timed out after 10 seconds")
    ]
    assert recruiter._worker_pane_confirmed_gone("w1:p1") is False

    # A live pane answers normally.
    responses[:] = [{"result": {"pane": {}}}]
    assert recruiter._worker_pane_confirmed_gone("w1:p1") is False

    # Two confirmations require two consecutive positive answers.
    responses[:] = [
        recruiter.RecruiterError("pane_not_found"),
        recruiter.RecruiterError("pane_not_found"),
    ]
    assert recruiter._worker_pane_confirmed_gone("w1:p1", confirmations=2) is True
    responses[:] = [
        recruiter.RecruiterError("pane_not_found"),
        {"result": {"pane": {}}},
    ]
    assert recruiter._worker_pane_confirmed_gone("w1:p1", confirmations=2) is False


# --- Role-aware launch state -------------------------------------------------


def _claimed_ledger(tmp_path: Path) -> tuple[Any, dict, str, str]:
    ledger = recruiter.JobLedger(tmp_path / "upagent-hub")
    order = _order()
    key, _created = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 60_000)
    assert isinstance(token, str)
    return ledger, order, key, token


def _launch(ledger: Any, key: str, token: str, role: str, pane: str) -> None:
    launch_id = ledger.begin_launch(key, token, role, f"{role}-agent", "sess", "/tmp")
    assert ledger.mark_launch_started(
        key, token, launch_id, pane, None, f"sess:{role}-agent"
    )


def test_worker_launch_enters_startup_check(tmp_path: Path) -> None:
    ledger, _order_value, key, token = _claimed_ledger(tmp_path)

    _launch(ledger, key, token, "worker", "1-2")

    assert ledger.state(key)["state"] == "startup-check"


def test_checker_launch_does_not_regress_running_state(tmp_path: Path) -> None:
    ledger, _order_value, key, token = _claimed_ledger(tmp_path)
    _launch(ledger, key, token, "worker", "1-2")
    assert ledger.mark_worker_healthy(key, token, {"healthy": True})
    assert ledger.state(key)["state"] == "running"

    _launch(ledger, key, token, "checker", "1-3")

    assert ledger.state(key)["state"] == "running"
    events = [item["event"] for item in ledger.events(key)]
    assert events.count("pane-started") == 2


def test_worker_relaunch_never_regresses_running_state(tmp_path: Path) -> None:
    ledger, _order_value, key, token = _claimed_ledger(tmp_path)
    _launch(ledger, key, token, "worker", "1-2")
    assert ledger.mark_worker_healthy(key, token, {"healthy": True})

    _launch(ledger, key, token, "worker", "1-4")

    assert ledger.state(key)["state"] == "running"
    events = [item["event"] for item in ledger.events(key)]
    assert "launch-state-suppressed" in events


def test_manager_launch_after_running_is_suppressed(tmp_path: Path) -> None:
    ledger, _order_value, key, token = _claimed_ledger(tmp_path)
    _launch(ledger, key, token, "worker", "1-2")
    assert ledger.mark_worker_healthy(key, token, {"healthy": True})

    _launch(ledger, key, token, "manager", "1-5")

    assert ledger.state(key)["state"] == "running"
    events = [item["event"] for item in ledger.events(key)]
    assert "launch-state-suppressed" in events


def test_initial_manager_launch_snapshots_manager_starting(tmp_path: Path) -> None:
    ledger, _order_value, key, token = _claimed_ledger(tmp_path)

    _launch(ledger, key, token, "manager", "1-1")

    assert ledger.state(key)["state"] == "manager-starting"


def test_claude_launch_pre_trusts_cwd_in_claude_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unattended Claude pane must never wedge on the folder-trust dialog."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(recruiter.Path, "home", staticmethod(lambda: home))
    cwd = tmp_path / "project"
    cwd.mkdir()

    recruiter._ensure_claude_folder_trust(str(cwd))

    data = json.loads((home / ".claude.json").read_text())
    entry = data["projects"][os.path.realpath(str(cwd))]
    assert entry["hasTrustDialogAccepted"] is True

    # Idempotent, and preserves other settings.
    data["projects"][os.path.realpath(str(cwd))]["allowedTools"] = ["Bash"]
    (home / ".claude.json").write_text(json.dumps(data))
    recruiter._ensure_claude_folder_trust(str(cwd))
    after = json.loads((home / ".claude.json").read_text())
    assert after["projects"][os.path.realpath(str(cwd))]["allowedTools"] == ["Bash"]


def test_corrupt_claude_json_fails_loud_instead_of_hanging_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{not json")
    monkeypatch.setattr(recruiter.Path, "home", staticmethod(lambda: home))

    with pytest.raises(RecruiterError, match="pre-trust"):
        recruiter._ensure_claude_folder_trust(str(tmp_path))
