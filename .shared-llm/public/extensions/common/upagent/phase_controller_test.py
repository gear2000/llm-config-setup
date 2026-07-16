"""Unit tests for deterministic phase-leader startup (coordination v2: no standing watchdog)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location(
    "upagent_phase_controller_tested", Path(__file__).with_name("phase_controller.py")
)
assert _spec and _spec.loader
phase_controller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phase_controller)
PhaseStartError = phase_controller.PhaseStartError


def _route(path: Path, *, watchdog_profile: bool = True) -> None:
    defaults = (
        "finalization_defaults:\n  watchdog_profile: cheap\n"
        if watchdog_profile
        else "finalization_defaults: {}\n"
    )
    path.write_text(
        "llm_profiles:\n"
        "  lead:\n"
        "    harness: claude\n"
        "    model: leader-model\n"
        "    effort: medium\n"
        "  cheap:\n"
        "    harness: claude\n"
        "    model: cheap-model\n"
        "    effort: low\n"
        f"{defaults}"
        "phases:\n"
        "  phase-0:\n"
        "    lead:\n"
        "      llm_profile: lead\n"
        "      agent: phase-leader\n"
    )


def _roster(path: Path, *, phase_leaders: bool = True) -> None:
    controller = (
        "phase_leaders:\n"
        "  claude: 'claude --agent {agent} --model {model} --effort {effort} read:{instructions_path}'\n"
        if phase_leaders
        else ""
    )
    path.write_text(
        "harnesses:\n"
        "  claude: 'claude --agent {agent} --model {model} read:{instructions_path}'\n"
        f"{controller}"
    )


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    run_root = tmp_path / "sample-run"
    run_root.mkdir()
    (run_root / "plan.md").write_text("# Plan\n")
    route = run_root / "route.yaml"
    _route(route)
    roster = tmp_path / "upagent.yaml"
    _roster(roster)
    return run_root, route, roster


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    started: list[str] = []
    closed: list[str] = []
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "tui-pane")
    monkeypatch.setattr(
        phase_controller.shutil, "which", lambda binary: f"/bin/{binary}"
    )
    monkeypatch.setattr(
        phase_controller,
        "_start_gated_leader",
        lambda name, tui, cwd, script: (
            started.append(name) or "leader-pane",
            "workspace-1",
        ),
    )
    monkeypatch.setattr(
        phase_controller, "_verify_gated_leader", lambda pane, script: None
    )
    monkeypatch.setattr(
        phase_controller,
        "_release_leader_gate",
        lambda path, request_id: path.unlink(),
    )
    monkeypatch.setattr(
        phase_controller,
        "_leader_health",
        lambda pane, cwd, profile, roster: {"healthy": True, "pane_id": pane},
    )
    monkeypatch.setattr(
        phase_controller.recruiter,
        "_close_worker_pane",
        lambda pane: closed.append(pane) or {"verified_absent": True},
    )
    return started, closed


def test_phase_start_releases_verified_leader_without_a_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, route, roster = _inputs(tmp_path)
    started, closed = _patch_runtime(monkeypatch)

    receipt = phase_controller.start_phase(
        route_path=route,
        run_root=run_root,
        phase_id="phase-0",
        pass_number=1,
        tui_pane="tui-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )

    control = run_root / "phases/phase-0/pass-1/control"
    assert started == ["phase-leader-sample-run-phase-0-p1"]
    assert closed == []
    assert not (control / "watchdog-ready.fifo").exists()
    assert receipt["state"] == "ready"
    assert receipt["watchdog"]["state"] == "not-configured"
    assert not (control.parent / "watchdog").exists()
    assert json.loads((run_root / "active-leader-panes.json").read_text()) == {
        "phase-0": "leader-pane"
    }


def test_phase_start_publishes_receipt_before_releasing_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, route, roster = _inputs(tmp_path)
    _started, _closed = _patch_runtime(monkeypatch)
    observed: dict[str, object] = {}

    def verify_release(gate: Path, request_id: str) -> None:
        receipt_path = gate.with_name("phase-start.json")
        observed.update(json.loads(receipt_path.read_text()))
        script = gate.with_name("launch-leader.sh").read_text()
        assert (
            f"export {phase_controller.recruiter.PHASE_START_RECEIPT_ENV}={receipt_path}"
            in script
        )
        gate.unlink()

    monkeypatch.setattr(phase_controller, "_release_leader_gate", verify_release)

    phase_controller.start_phase(
        route_path=route,
        run_root=run_root,
        phase_id="phase-0",
        pass_number=1,
        tui_pane="tui-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )

    assert observed["state"] == "leader-gated"
    assert observed["leader_pane"] == "leader-pane"
    assert observed["watchdog"]["state"] == "not-configured"


def test_not_configured_watchdog_receipt_is_not_degraded(tmp_path: Path) -> None:
    pass_dir = tmp_path / "run" / "phases" / "phase-0" / "pass-1"
    control = pass_dir / "control"
    control.mkdir(parents=True)
    (control / "phase-start.json").write_text(
        json.dumps(
            {
                "leader_pane": "leader-pane",
                "pass": 1,
                "phase_id": "phase-0",
                "state": "ready",
                "watchdog": {"state": "not-configured"},
            }
        )
    )
    stage_dir = pass_dir / "stages" / "stage-1-implementation" / "try-1"
    stage_dir.mkdir(parents=True)
    (stage_dir / "instructions.md").write_text("Do the stage.\n")
    order = {
        "agent": "backend",
        "cockpit_pane": "leader-pane",
        "instructions_path": str(stage_dir / "instructions.md"),
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "phase_id": "phase-0",
    }
    assert phase_controller.recruiter.phase_watchdog_warning(order) is None


def test_missing_phase_leader_template_fails_before_creating_a_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, route, roster = _inputs(tmp_path)
    _roster(roster, phase_leaders=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "tui-pane")
    monkeypatch.setattr(
        phase_controller.shutil, "which", lambda binary: f"/bin/{binary}"
    )

    with pytest.raises(PhaseStartError, match="phase_leaders.claude"):
        phase_controller.start_phase(
            route_path=route,
            run_root=run_root,
            phase_id="phase-0",
            pass_number=1,
            tui_pane="tui-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_live_prior_leader_is_never_destroyed_by_a_new_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, route, roster = _inputs(tmp_path)
    (run_root / "active-leader-panes.json").write_text('{"phase-0": "owned-pane"}\n')
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "tui-pane")
    monkeypatch.setattr(phase_controller, "_live_panes", lambda: {"owned-pane"})
    monkeypatch.setattr(
        phase_controller.shutil, "which", lambda binary: f"/bin/{binary}"
    )

    with pytest.raises(PhaseStartError, match="only its owning TUI"):
        phase_controller.start_phase(
            route_path=route,
            run_root=run_root,
            phase_id="phase-0",
            pass_number=1,
            tui_pane="tui-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_watchdog_profile_falls_back_to_leader_profile(tmp_path: Path) -> None:
    route = tmp_path / "route.yaml"
    _route(route, watchdog_profile=False)

    resolved = phase_controller._load_route(route, "phase-0")

    assert resolved["watchdog"]["profile_name"] == "lead"
    assert resolved["watchdog"]["profile"]["model"] == "leader-model"


def test_phase_start_refuses_to_run_outside_herdr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, route, roster = _inputs(tmp_path)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    with pytest.raises(PhaseStartError, match="HERDR_ENV=1"):
        phase_controller.start_phase(
            route_path=route,
            run_root=run_root,
            phase_id="phase-0",
            pass_number=1,
            tui_pane="tui-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_fifo_gate_releases_once_without_polling(tmp_path: Path) -> None:
    gate = tmp_path / "leader.fifo"
    phase_controller._create_leader_gate(gate)
    reader = os.open(gate, os.O_RDONLY | os.O_NONBLOCK)
    try:
        phase_controller._release_leader_gate(gate, "request-1")
        assert os.read(reader, 100) == b"request-1\n"
    finally:
        os.close(reader)


def test_fifo_gate_fails_loud_when_leader_is_not_waiting(tmp_path: Path) -> None:
    gate = tmp_path / "leader.fifo"
    phase_controller._create_leader_gate(gate)

    with pytest.raises(PhaseStartError, match="stopped waiting"):
        phase_controller._release_leader_gate(gate, "request-1")


def test_phase_start_cannot_claim_a_different_tui_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, route, roster = _inputs(tmp_path)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "actual-pane")

    with pytest.raises(PhaseStartError, match="does not match current Herdr pane"):
        phase_controller.start_phase(
            route_path=route,
            run_root=run_root,
            phase_id="phase-0",
            pass_number=1,
            tui_pane="other-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_gated_leader_is_one_atomic_herdr_start_below_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_herdr(*args: str) -> dict:
        calls.append(args)
        if args == ("pane", "get", "tui-pane"):
            return {
                "result": {
                    "pane": {
                        "pane_id": "tui-pane",
                        "tab_id": "tab-1",
                        "workspace_id": "ws-1",
                    }
                }
            }
        return {
            "result": {
                "agent": {
                    "name": "phase-leader",
                    "pane_id": "leader-pane",
                    "workspace_id": "ws-1",
                }
            }
        }

    monkeypatch.setattr(phase_controller.recruiter, "_herdr_json", fake_herdr)
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\n")

    assert phase_controller._start_gated_leader(
        "phase-leader", "tui-pane", tmp_path, script
    ) == ("leader-pane", "ws-1")
    assert calls[1] == (
        "agent",
        "start",
        "phase-leader",
        "--cwd",
        str(tmp_path),
        "--tab",
        "tab-1",
        "--split",
        "down",
        "--no-focus",
        "--",
        "bash",
        str(script),
    )


def test_public_roster_has_controller_template_for_every_worker_harness() -> None:
    roster = phase_controller.recruiter.load_roster(
        Path(__file__).with_name("upagent.yaml.example")
    )

    assert set(roster["phase_leaders"]) == set(roster["harnesses"])


def test_leader_start_failure_closes_the_gated_leader_and_reports_the_real_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: cleanup used to crash on an undefined helper, hiding the startup
    error and leaking both the half-started leader pane and its active mapping."""
    run_root, route, roster = _inputs(tmp_path)
    _started, closed = _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        phase_controller,
        "_leader_health",
        lambda pane, cwd, profile, roster: (_ for _ in ()).throw(
            PhaseStartError("leader never became healthy")
        ),
    )

    with pytest.raises(PhaseStartError, match="leader never became healthy"):
        phase_controller.start_phase(
            route_path=route,
            run_root=run_root,
            phase_id="phase-0",
            pass_number=1,
            tui_pane="tui-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )

    control = run_root / "phases/phase-0/pass-1/control"
    receipt = json.loads((control / "phase-start.json").read_text())
    assert receipt["state"] == "failed"
    assert "leader never became healthy" in receipt["reason"]
    assert closed == ["leader-pane"]
    assert not (control / "watchdog-ready.fifo").exists()
    assert json.loads((run_root / "active-leader-panes.json").read_text()) == {}


def test_leader_close_failure_during_cleanup_does_not_mask_the_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, route, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        phase_controller,
        "_leader_health",
        lambda pane, cwd, profile, roster: (_ for _ in ()).throw(
            PhaseStartError("leader never became healthy")
        ),
    )
    monkeypatch.setattr(
        phase_controller.recruiter,
        "_close_worker_pane",
        lambda pane: (_ for _ in ()).throw(
            phase_controller.recruiter.RecruiterError("close transport down")
        ),
    )

    with pytest.raises(PhaseStartError, match="leader never became healthy"):
        phase_controller.start_phase(
            route_path=route,
            run_root=run_root,
            phase_id="phase-0",
            pass_number=1,
            tui_pane="tui-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )
