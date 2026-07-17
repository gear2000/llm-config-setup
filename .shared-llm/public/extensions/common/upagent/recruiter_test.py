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
from types import SimpleNamespace

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

    assert "belongs to leader some-other-pane" in recruiter.phase_receipt_warning(
        order
    )


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
    started: list[list[str]] = []
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda command, **kwargs: started.append(command),
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
    monkeypatch.setattr(
        recruiter.subprocess, "Popen", lambda command, **kwargs: None
    )

    def submit(stage_id: str, pass_name: str, try_name: str) -> tuple[dict, str]:
        stage = tmp_path / f"run/phases/phase-0/{pass_name}/stages/{stage_id}/{try_name}"
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


def test_legacy_recruit_rejection_writes_blocked_result_and_terminal_marker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    result_path = tmp_path / "result.json"
    order_path = tmp_path / "order.json"
    order_path.write_text(
        json.dumps(
            {
                "order_id": "legacy-invalid-order",
                "stage_id": "stage-1-implementation",
                "result_path": str(result_path),
            }
        )
    )
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("invalid order must not start a job"),
    )

    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 1

    result = json.loads(result_path.read_text())
    assert result["order_id"] == "legacy-invalid-order"
    assert result["verdict"] == "blocked"
    assert "invalid order" in result["reason"]
    assert "ORDER legacy-invalid-order DONE" in capsys.readouterr().out


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
    monkeypatch.setattr(recruiter, "_herdr_json", lambda *args: responses[args])
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
    monkeypatch.setattr(recruiter, "_herdr_json", lambda *args: responses[args])
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
    monkeypatch.setattr(recruiter, "_herdr_json", lambda *args: responses[args])
    monkeypatch.setattr(recruiter, "STARTUP_FAILURE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(
        recruiter, "_pane_recent_output", lambda _pane: "unknown model: nope"
    )
    with pytest.raises(RecruiterError, match="expected claude process"):
        recruiter._wait_for_worker_health("worker-pane", _order(), 100)


def test_start_worker_is_one_atomic_herdr_agent_start(monkeypatch) -> None:
    calls = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        assert kwargs == {}
        if args == ("pane", "get", "leader-pane"):
            return {
                "result": {"pane": {"tab_id": "tab-1", "workspace_id": "workspace-1"}}
            }
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
        _order(cockpit_pane="leader-pane"),
        "claude --model some-model",
    )
    assert (pane, workspace, address) == (
        "worker-pane",
        "workspace-1",
        "upagent-req-abc-g1",
    )
    start = calls[1]
    assert start[:4] == ("agent", "start", "upagent-req-abc-g1", "--cwd")
    assert "--" in start
    assert start[-3:] == ("bash", "-lc", "claude --model some-model")


def test_start_herdr_agent_honors_downward_role_placement(monkeypatch) -> None:
    calls = []

    def fake_json(*args: str, **kwargs: object) -> dict:
        calls.append(args)
        assert kwargs == {}
        if args == ("pane", "get", "leader-pane"):
            return {"result": {"pane": {"tab_id": "tab-1"}}}
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
    )

    start = calls[1]
    split_index = start.index("--split")
    assert start[split_index + 1] == "down"


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
            "timeout_seconds": recruiter.LAYOUT_COMMAND_TIMEOUT_SECONDS
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
        "worker-pane", "workspace-1", "workers", split_direction="right"
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
            "timeout_seconds": recruiter.LAYOUT_COMMAND_TIMEOUT_SECONDS
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
        "watchdog-pane", "workspace-1", "oversight", split_direction="down"
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


def test_tab_placement_failure_keeps_the_started_agent_alive(monkeypatch, capsys) -> None:
    calls: list[tuple[str, ...]] = []
    closed: list[str] = []

    def fake_json(*args: str) -> dict:
        calls.append(args)
        if args == ("pane", "get", "leader-pane"):
            return {
                "result": {
                    "pane": {
                        "tab_id": "control-tab",
                        "workspace_id": "workspace-1",
                    }
                }
            }
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
        "claude",
        tab_role="workers",
    )

    assert started == ("worker-pane", "workspace-1", "worker")
    assert closed == []
    assert "tab placement failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("split_direction", "target_fraction", "neighbor_direction", "neighbor_pane", "amount"),
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
            "timeout_seconds": recruiter.LAYOUT_COMMAND_TIMEOUT_SECONDS
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

    assert "watchdog pane watchdog-pane layout adjustment failed" in capsys.readouterr().err


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
        recruiter._worker_tab_role({"agent": "plan-lifecycle-watchdog"})
        == "oversight"
    )
    assert recruiter._worker_tab_role({"agent": "phase-watchdog"}) == "oversight"


def test_herdr_json_converts_timeout_to_recruiter_error(monkeypatch) -> None:
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(
        recruiter.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            recruiter.subprocess.TimeoutExpired("herdr", 0.1)
        ),
    )

    with pytest.raises(RecruiterError, match="timed out after 0.1 seconds"):
        recruiter._herdr_json("pane", "layout", timeout_seconds=0.1)


def test_checker_cleanup_guards_layout_adjustment(tmp_path: Path, monkeypatch) -> None:
    order = _order(cwd=str(tmp_path))
    worker_result = tmp_path / "worker-result.json"
    worker_result.write_text(json.dumps(_result(order["order_id"])))
    ledger = recruiter.JobLedger(tmp_path / "hub")
    key, _ = ledger.submit(order)
    manager = {
        "address": "manager-address",
        "config": recruiter.llm_management.load_management_config(_roster()),
        "generation": 1,
        "pane": "manager-pane",
    }
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {
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
    monkeypatch.setattr(recruiter, "_close_worker_pane", closed.append)

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


def _cleanup(worker_pane: str | None = "worker-pane") -> dict:
    return {
        "status": "closed" if worker_pane else "not-created",
        "worker_pane": worker_pane,
        "verified_absent": True,
    }


def _patch_approved_manager(monkeypatch) -> None:
    import dataclasses

    real_load = recruiter.llm_management.load_management_config
    monkeypatch.setattr(
        recruiter.llm_management,
        "load_management_config",
        lambda roster: dataclasses.replace(real_load(roster), mode="dedicated"),
    )
    monkeypatch.setattr(
        recruiter,
        "_start_account_manager",
        lambda *args: {
            "address": "manager-address",
            "decision": SimpleNamespace(decision="approved", message="approved"),
            "generation": 1,
            "pane": "manager-pane",
        },
    )
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
    second.join(timeout=2)
    release_first_submitter.set()
    first.join(timeout=2)

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
    assert ledger.finalize(
        key, token, order, _result(order["order_id"]), cleanup=cleanup, exit_code=0
    )
    assert not (root / "active/requests" / key).exists()
    assert index.is_file()
    assert json.loads(Path(order["result_path"]).read_text()) == _result(
        order["order_id"]
    )
    latest = json.loads((ledger.request_dir(key) / "state/latest.json").read_text())
    assert latest["state"] == "finished" and latest["verdict"] == "passed"
    receipt = json.loads((ledger.request_dir(key) / "receipt.json").read_text())
    assert receipt == {
        "cleanup": cleanup,
        "generation": 1,
        "order_id": order["order_id"],
        "request_id": recruiter.lifecycle.request_identity(order),
        "result_path": order["result_path"],
        "state": "finished",
        "verdict": "passed",
    }


def test_requester_decision_is_fenced_to_current_lease_and_extends_it(
    tmp_path: Path,
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000, owner={"generation": 1})
    assert token
    lease = ledger.mark_awaiting_requester(key, token, "nonce-1", 1)
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
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    def wait(_pane: str, timeout_ms: int, _finalized: object) -> bool:
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
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    code, result, _ = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        on_worker_healthy=lambda evidence: (_ for _ in ()).throw(
            RecruiterError("startup rejected")
        ),
    )

    assert code == 1
    assert result["verdict"] == "blocked"
    assert "startup rejected" in result["reason"]


def test_submit_agent_prompt_waits_for_idle_and_submits_enter_atomically(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(recruiter, "_herdr", lambda *args: calls.append(args))
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args: {"result": {"agent": {"pane_id": "manager-pane"}}},
    )

    recruiter._submit_agent_prompt("manager-name", "Review evidence.", 5_000)

    assert calls == [
        ("agent", "wait", "manager-name", "--status", "idle", "--timeout", "5000"),
        ("pane", "run", "manager-pane", "Review evidence."),
    ]


def test_timeout_waits_for_authenticated_requester_extension(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = recruiter.JobLedger(tmp_path / "hub")
    order = _order(result_path=str(tmp_path / "result.json"))
    key, _ = ledger.submit(order)
    token = ledger.claim(key, order["order_id"], 1_000, owner={"generation": 1})
    assert token
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


def test_worker_instructions_end_with_one_literal_private_result_contract(
    tmp_path: Path,
) -> None:
    original = tmp_path / "instructions.md"
    original.write_text("Do the stage. An older brief mentioned /public/result.json.\n")
    private_result = tmp_path / "hub/results/token.json"
    generated = tmp_path / "hub/worker-instructions.md"

    recruiter._write_worker_instructions(
        _order(instructions_path=str(original)), private_result, generated
    )

    text = generated.read_text()
    assert text.endswith(
        "Write exactly one result JSON file to: " + str(private_result) + "\n"
        'Its `order_id` must be exactly: "phase-0.stage-1-implementation.pass-1.try-1"\n'
        "Do not write a result to any other path.\n"
    )


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
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", lambda *args: True)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)
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
    )

    assert code == 0
    assert result == _result(order["order_id"])
    assert cleanup["verified_absent"] is True
    assert lifecycle_events == ["recorded", "resized"]


def test_completion_monitor_wakes_on_stable_malformed_result(
    tmp_path: Path, monkeypatch
) -> None:
    malformed = tmp_path / "private-result.json"
    malformed.write_text('{"order_id": null}')
    monkeypatch.setattr(recruiter, "INVALID_RESULT_SETTLE_SECONDS", 0.05)

    stop, ready, thread = recruiter._start_completion_monitor(
        _order(), malformed, 1_000
    )

    assert ready.wait(timeout=0.5)
    stop.set()
    thread.join(timeout=0.5)
    assert not thread.is_alive()


def test_completion_monitor_can_resume_after_a_premature_result(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / "private-result.json"
    order = _order()
    stop, ready, thread = recruiter._start_completion_monitor(
        order, result_path, 1_000
    )

    result_path.write_text(json.dumps(_result(order["order_id"])))
    assert ready.wait(timeout=0.5)
    result_path.unlink()
    ready.clear()
    time.sleep(0.1)
    assert thread.is_alive()

    result_path.write_text(json.dumps(_result(order["order_id"])))
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

    def fake_wait(*args: object) -> bool:
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
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", fake_wait)
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda target, message, idle_timeout_ms: prompts.append(message),
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path),
        str(roster_path),
        private_result,
        on_worker_launched=lambda *args: finalized,
    )

    assert code == 0
    assert result == _result(order["order_id"])
    assert cleanup["verified_absent"] is True
    assert waits == 2
    assert len(prompts) == 1
    assert "Resume monitoring" in prompts[0]
    assert len(list((private_result.parent / "premature-results").glob("*.json"))) == 1


def test_spawn_job_detaches_all_standard_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    recruiter._spawn_job("request-key", "roster.yaml")

    assert calls[0][1] == {
        "stdin": recruiter.subprocess.DEVNULL,
        "stdout": recruiter.subprocess.DEVNULL,
        "stderr": recruiter.subprocess.DEVNULL,
        "start_new_session": True,
    }


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
    assert ledger.finalize(
        key, token, order, _result(order["order_id"]), cleanup=_cleanup(None)
    )
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail(
            "completed order must not spawn a job runner"
        ),
    )

    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0
    assert capsys.readouterr().out == f"ORDER {order['order_id']} DONE\n"


def test_recruit_submits_and_spawns_without_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(_order()))
    spawned = []
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(
        recruiter.subprocess,
        "Popen",
        lambda command, **kwargs: spawned.append((command, kwargs)),
    )
    assert recruiter.cmd_recruit(str(order_path), "roster.yaml") == 0
    assert len(spawned) == 1
    assert spawned[0][0][-2] == "run-job" and spawned[0][1]["start_new_session"] is True
    key = recruiter.JobLedger().key_for_order(_order())
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
    waits = []

    class Process:
        def wait(self, timeout: float) -> int:
            waits.append(timeout)
            ledger = recruiter.JobLedger()
            key = ledger.key_for_order(order)
            token = ledger.claim(key, order["order_id"], 1_000)
            assert token
            assert ledger.finalize(
                key, token, order, _result(order["order_id"]), cleanup=_cleanup()
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
    assert waits


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
        lambda pane: (
            closed.append(pane)
            or {"status": "closed", "worker_pane": pane, "verified_absent": True}
        ),
    )

    assert recruiter.cmd_reconcile(force=True) == 0

    assert closed == ["owned-worker"]
    assert ledger.completed_result(key, order) == _result(order["order_id"])
    assert (
        ledger.completed_receipt(key, order)["cleanup"]["worker_pane"] == "owned-worker"
    )


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

    assert ledger.finalize(
        key,
        token,
        order,
        _result(order["order_id"], verdict="blocked"),
        cleanup=cleanup,
    )

    assert (ledger.active / "requests" / key / "lease.json").is_file()
    assert ledger.completed_receipt(key, order)["state"] == "cleanup-failed"


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

    def healthy(*args: object) -> dict:
        assert recorded.is_set()
        private_result.write_text(json.dumps(_result(order["order_id"])))
        return {"healthy": True}

    monkeypatch.setattr(recruiter, "_wait_for_worker_health", healthy)
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", lambda *args: True)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    code, result, cleanup = recruiter._run_order(
        str(order_path), str(roster_path), private_result, on_worker
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
    ) -> tuple[str, str, str]:
        launch_orders.append(launch_order)
        launch_directions.append(split_direction)
        launch_tabs.append(tab_role)
        return "manager-pane", "cockpit-workspace", name

    monkeypatch.setattr(recruiter, "_start_herdr_agent", start_manager)
    def resize_manager(
        pane: str, *, split_direction: str, target_fraction: float, role: str
    ) -> None:
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

    manager = recruiter._start_account_manager(ledger, key, token, order, _roster())

    lease = json.loads((ledger.active / "requests" / key / "lease.json").read_text())
    assert manager["decision"].decision == "approved"
    assert lease["manager_pane"] == "manager-pane"
    assert lease["manager_address"].startswith("upagent-manager-")
    assert lease["manager_workspace_id"] == "cockpit-workspace"
    assert launch_orders[0]["cockpit_pane"] == order["cockpit_pane"]
    assert launch_directions == ["down"]
    assert launch_tabs == ["oversight"]
    assert resize_calls == [("manager-pane", "down", 0.20, "account manager")]


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
    status_wait_started = threading.Event()
    staging_paths: list[Path] = []
    closed_panes: list[str] = []
    worker_closed = threading.Event()
    status_wait_timeouts: list[str] = []
    outcomes: list[int] = []
    _patch_approved_manager(monkeypatch)

    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        staging_paths.append(Path(launch.split("write:", maxsplit=1)[1]))
        worker_launched.set()
        return "worker-pane", "cockpit", name

    def fake_close(pane: str) -> dict:
        closed_panes.append(pane)
        worker_closed.set()
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

    def fake_popen(command: list[str], **kwargs: object) -> NeverDoneProcess:
        assert command[1:3] == ["wait", "agent-status"]
        status_wait_timeouts.append(command[-1])
        status_wait_started.set()
        return NeverDoneProcess()

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", fake_close)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(recruiter.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    runner = threading.Thread(
        target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path)))
    )
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
    assert closed_panes == ["worker-pane", "manager-pane"]

    promoted_at = time.monotonic()
    runner.join(timeout=1)
    assert not runner.is_alive()
    assert time.monotonic() - promoted_at < 0.5
    assert outcomes == [0]
    assert status_wait_timeouts and set(status_wait_timeouts) == {"1000"}
    assert capsys.readouterr().out.count(f"ORDER {order['order_id']} DONE\n") == 1
    assert closed_panes == ["worker-pane", "manager-pane"]


def test_codex_worker_survives_missing_startup_assessment_and_promotes_private_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
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
        lambda ledger, key, order, generation, message_type, *args, **kwargs: notifications.append(
            message_type
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

    def fake_close(pane: str) -> dict:
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
        assert command[1:3] == ["wait", "agent-status"]
        status_wait_started.set()
        return NeverDoneProcess()

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", fake_close)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(recruiter.subprocess, "Popen", never_done)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    runner = threading.Thread(
        target=lambda: outcomes.append(recruiter.cmd_run_job(key, str(roster_path)))
    )
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

    def fail_wait(*args: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        worker_result_paths[0].write_text(
            json.dumps(_result(order["order_id"], verdict="passed"))
        )
        raise recruiter.RecruiterError("wait transport failed")

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", fail_wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    output = capsys.readouterr()
    assert f"ORDER {order['order_id']} DONE" in output.out
    assert "kept existing worker result" in output.err
    assert json.loads(result_path.read_text()) == _result(
        order["order_id"], verdict="passed"
    )


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

    assert not ledger.finalize(
        key, expired_token, order, _result(order["order_id"]), cleanup=_cleanup()
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
    assert not ledger.finalize(
        key, old_token, order, _result(order["order_id"]), cleanup=_cleanup()
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

    code, result, cleanup = recruiter._run_order(
        str(order_path), str(roster_path), staging_path
    )
    assert code == 1
    assert result["verdict"] == "blocked"
    assert json.loads(staging_path.read_text())["verdict"] == "blocked"
    assert not result_path.exists()
    assert "ORDER" not in capsys.readouterr().out
    assert cleanup["verified_absent"] is True


def test_duplicate_popen_failure_cannot_finalize_live_owner(
    tmp_path: Path, monkeypatch, capsys
) -> None:
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
    assert latest["state"] == "claimed"
    assert "DONE" not in capsys.readouterr().out


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

    assert not ledger.finalize(
        key,
        old_token,
        order,
        _result(order["order_id"], verdict="passed"),
        cleanup=_cleanup(),
    )
    assert not Path(order["result_path"]).exists()
    assert ledger.finalize(
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

    def fail_popen(*args: object, **kwargs: object) -> None:
        raise OSError("runner process unavailable")

    original_write_json = recruiter.JobLedger._write_json

    def fail_staging_write(path: Path, value: dict) -> None:
        if path.parent.name == "results":
            raise OSError("disk full")
        original_write_json(path, value)

    monkeypatch.setattr(recruiter.subprocess, "Popen", fail_popen)
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

    def wait(*args: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        worker_result_paths[0].write_text(
            json.dumps(_result(order["order_id"], verdict="passed"))
        )
        return True

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

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

    def wait(*args: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        worker_result_paths[0].write_text(
            json.dumps(_result(order["order_id"], verdict="passed"))
        )
        return True

    advice_calls: list[str] = []

    def advise(*args: object) -> str:
        advice_calls.append(str(args[-1]))
        return "retry-startup"

    monkeypatch.setattr(recruiter, "_startup_rescue_advice", advise)
    monkeypatch.setattr(recruiter, "_start_herdr_agent", flaky_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

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
    monkeypatch.setattr(recruiter, "_start_herdr_agent", dead_start)
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

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
    ) -> dict[str, object]:
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

    def wait(*args: object) -> bool:
        worker_result_paths[0].parent.mkdir(parents=True, exist_ok=True)
        worker_result_paths[0].write_text(
            json.dumps(_result(order["order_id"], verdict="passed"))
        )
        return True

    monkeypatch.setattr(recruiter, "_start_account_manager", fake_manager)
    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_wait_for_agent_status", wait)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 0

    assert manager_calls == [order["order_id"]], (
        "a dedicated-pinned order must hire the account manager even on a direct roster"
    )


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
    manager = {
        "generation": 1,
        "config": recruiter.llm_management.load_management_config({}),
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
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: closed.append(pane))

    advice = recruiter._startup_rescue_advice(
        ledger, key, order, manager, "harness rejected the model flag"
    )

    assert advice == "ask-requester"
    assert closed == ["rescue-pane"], "the rescue pane must always be closed"


def test_manager_startup_rejection_is_not_rescued(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A dedicated manager's explicit refusal is a ruling; no automatic relaunch."""
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

    def fake_manager(*args: object) -> dict[str, object]:
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
            "health": None,
            "pane": "manager-pane",
            "workspace_id": "ws",
        }

    def no_rescue(*args: object) -> str:
        raise AssertionError("a manager rejection must never reach the rescue broker")

    monkeypatch.setattr(recruiter, "_start_account_manager", fake_manager)
    monkeypatch.setattr(recruiter, "_startup_rescue_advice", no_rescue)
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda name, o, command, **kwargs: ("worker-pane", "cockpit", name),
    )
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args: {"healthy": True}
    )
    monkeypatch.setattr(
        recruiter,
        "_ask_manager_about_startup",
        lambda *args: SimpleNamespace(
            assessment="startup-failed", message="wrong model requested"
        ),
    )
    monkeypatch.setattr(recruiter, "_close_worker_pane", lambda pane: _cleanup(pane))
    monkeypatch.setattr(recruiter, "_report_state", lambda *args: None)

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    assert json.loads(result_path.read_text())["verdict"] == "blocked"
    kinds = {
        payload.get("type") for _, payload in recruiter._mailbox_messages(ledger, key)
    }
    assert "startup-needs-requester" in kinds
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
    assert ledger.finalize(
        key, token, order, _result(order["order_id"]), cleanup=_cleanup(), exit_code=0
    )

    assert recruiter.cmd_await_any([str(order_path)], timeout_ms=1_000) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("AWAIT_EVENT ")
    event = json.loads(line.removeprefix("AWAIT_EVENT "))
    assert event["kind"] == "completed"
    assert event["terminal"] is True
    assert event["request_id"] == recruiter.lifecycle.request_identity(order)
    assert event["receipt"]["verdict"] == "passed"


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
