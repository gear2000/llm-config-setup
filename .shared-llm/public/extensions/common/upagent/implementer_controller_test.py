"""Unit tests for deterministic plan-implementer startup."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_implementer_controller_tested",
    Path(__file__).with_name("implementer_controller.py"),
)
assert _spec and _spec.loader
implementer_controller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(implementer_controller)
_recruiter_spec = importlib.util.spec_from_file_location(
    "implementer_controller_test_recruiter", Path(__file__).with_name("recruiter.py")
)
assert _recruiter_spec and _recruiter_spec.loader
recruiter = importlib.util.module_from_spec(_recruiter_spec)
sys.modules[_recruiter_spec.name] = recruiter
_recruiter_spec.loader.exec_module(recruiter)
implementer_controller._bind_recruiter_runtime(recruiter)
ImplementerStartError = implementer_controller.ImplementerStartError


@pytest.fixture(autouse=True)
def _resolved_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        implementer_controller.recruiter,
        "_resolve_current_herdr_session_name",
        lambda: "llm-lab-test",
    )


def _roster(path: Path, *, plan_implementers: bool = True) -> None:
    controller = (
        "plan_implementers:\n"
        "  claude: 'claude --model {model} --effort {effort} read:{instructions_path}'\n"
        if plan_implementers
        else ""
    )
    path.write_text(
        "harnesses:\n"
        "  claude: 'claude --agent {agent} --model {model} read:{instructions_path}'\n"
        f"{controller}"
    )


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    plan = tmp_path / "origin" / "plan.md"
    plan.parent.mkdir()
    plan.write_text("# Plan: example\n\nGoal: ship it\n")
    run_root = tmp_path / "sample-run"
    roster = tmp_path / "upagent.yaml"
    _roster(roster)
    return plan, run_root, roster


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    started: list[str] = []
    closed: list[str] = []
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "hil-pane")
    monkeypatch.setattr(
        implementer_controller.shutil, "which", lambda binary: f"/bin/{binary}"
    )
    monkeypatch.setattr(
        implementer_controller,
        "_resolve_offering",
        lambda offering_id, effort, cwd: {
            "effort": effort,
            "harness": "claude",
            "id": offering_id,
            "model": "claude-sonnet-5",
        },
    )
    monkeypatch.setattr(
        implementer_controller,
        "_start_gated",
        lambda name, hil, cwd, script, herdr_session: (
            started.append(name) or "implementer-pane",
            "workspace-1",
        ),
    )
    monkeypatch.setattr(
        implementer_controller,
        "_verify_gated",
        lambda pane, script, herdr_session: None,
    )
    monkeypatch.setattr(
        implementer_controller,
        "_release_gate",
        lambda path, request_id: path.unlink(),
    )
    monkeypatch.setattr(
        implementer_controller,
        "_health",
        lambda pane, cwd, profile, roster, herdr_session: {
            "cwd": str(cwd),
            "expected_agent": "claude",
            "expected_process": "claude",
            "healthy": True,
            "pane_id": pane,
            "process_pid": 123,
            "process_start_time": "start-123",
        },
    )
    monkeypatch.setattr(
        implementer_controller.recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: closed.append(pane) or {"verified_absent": True},
    )
    return started, closed


def test_implementer_start_releases_verified_controller_without_a_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    started, closed = _patch_runtime(monkeypatch)

    receipt = implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )

    assert receipt["state"] == "ready"
    assert receipt["implementer_pane"] == "implementer-pane"
    assert receipt["leader_pane"] == "implementer-pane"
    assert receipt["watchdog"]["state"] == "not-configured"
    assert receipt["offering"] == "claude-sonnet-5"
    assert (run_root / "plan.md").read_text() == plan.read_text()
    assert started == ["plan-implementer-sample-run"]
    assert closed == []
    script = (run_root / "control" / "start.sh").read_text()
    assert implementer_controller.IMPLEMENTER_START_RECEIPT_ENV in script
    assert "/plan-implementer --plan" in (run_root / "control" / "instructions.md").read_text()


def test_implementer_start_requires_herdr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.setenv("HERDR_PANE_ID", "hil-pane")
    with pytest.raises(ImplementerStartError, match="HERDR_ENV=1"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_missing_plan_implementers_template_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _roster(roster, plan_implementers=False)
    _patch_runtime(monkeypatch)
    with pytest.raises(ImplementerStartError, match="plan_implementers.claude"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_startup_failure_closes_the_gated_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _started, closed = _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        implementer_controller,
        "_health",
        lambda pane, cwd, profile, roster, herdr_session: (_ for _ in ()).throw(
            ImplementerStartError("implementer never became healthy")
        ),
    )

    with pytest.raises(ImplementerStartError, match="implementer never became healthy"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )

    assert closed == ["implementer-pane"]
    assert not (run_root / "control" / "implementer-ready.fifo").exists()


def test_example_roster_has_plan_implementer_templates_for_every_controller_harness() -> (
    None
):
    roster = implementer_controller.recruiter.load_roster(
        Path(__file__).with_name("upagent.yaml.example")
    )
    assert set(roster["plan_implementers"]) == set(roster["phase_leaders"])
    assert set(roster["plan_implementers"]) <= set(roster["harnesses"])
