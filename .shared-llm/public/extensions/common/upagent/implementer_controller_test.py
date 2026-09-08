"""Unit tests for deterministic plan-implementer startup."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import shlex
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
    monkeypatch.setattr(
        implementer_controller.recruiter,
        "_place_started_agent_in_role_tab",
        lambda pane, workspace, role, **kwargs: pane,
    )
    monkeypatch.setattr(
        implementer_controller,
        "_live_panes",
        lambda herdr_session=None: {"implementer-pane", "hil-pane"},
    )
    return started, closed


def test_implementer_start_releases_verified_controller_without_a_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    started, closed = _patch_runtime(monkeypatch)
    monkeypatch.delenv("UPAGENT_CANONICAL_REPO", raising=False)

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
    assert implementer_controller.CANONICAL_REPO_ENV not in script
    assert "/plan-implementer --plan" in (run_root / "control" / "instructions.md").read_text()
    assert receipt["hil_pane"] == "hil-pane"
    assert receipt["ownership"]["leader"]["pane_id"] == "implementer-pane"


def test_start_script_exports_canonical_repo_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    checkout = tmp_path / "canonical-checkout"
    checkout.mkdir()
    _patch_runtime(monkeypatch)
    monkeypatch.setenv("UPAGENT_CANONICAL_REPO", str(checkout))
    receipt = implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    script = (run_root / "control" / "start.sh").read_text()
    expected = f"export {implementer_controller.CANONICAL_REPO_ENV}={shlex.quote(str(checkout.resolve()))}"
    assert expected in script
    assert receipt["canonical_repo"] == str(checkout.resolve())


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
    failed = json.loads((run_root / "control" / "implementer-start.json").read_text())
    assert failed["state"] == "failed"
    assert "never became healthy" in failed["reason"]


def test_gate_release_sees_implementer_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    seen: list[dict] = []

    def _release(path, request_id):
        seen.append(json.loads((path.parent / "implementer-start.json").read_text()))
        path.unlink()

    monkeypatch.setattr(implementer_controller, "_release_gate", _release)
    implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    assert seen[0]["state"] == "implementer-gated"
    assert seen[0]["implementer_pane"] == "implementer-pane"
    assert seen[0]["hil_pane"] == "hil-pane"


def test_ready_receipt_reattach_requires_live_matching_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    first = implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    again = implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    assert again["implementer_pane"] == first["implementer_pane"]
    assert first["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert (run_root / "plan.md").read_text() == plan.read_text()
    monkeypatch.setattr(implementer_controller, "_live_panes", lambda herdr_session=None: set())
    with pytest.raises(ImplementerStartError, match="no longer live"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_ready_receipt_rejects_offering_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    with pytest.raises(ImplementerStartError, match="offering"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="codex-gpt-5-6-sol",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_failed_receipt_requires_a_new_run_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    (run_root / "control").mkdir(parents=True)
    (run_root / "control" / "implementer-start.json").write_text(
        json.dumps(
            {
                "state": "failed",
                "reason": "implementer never became healthy",
                "herdr_session": "llm-lab-test",
                "run_id": "sample-run",
            }
        )
    )
    with pytest.raises(ImplementerStartError, match="new run-root"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_finish_closes_only_the_recorded_implementer_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _started, closed = _patch_runtime(monkeypatch)
    receipt = implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    (run_root / "implementer-result.json").write_text(
        json.dumps(
            {
                "verdict": "passed",
                "summary": "done",
                "run_root": str(run_root),
                "run_id": "sample-run",
            }
        )
    )
    finished = implementer_controller.finish_implementer(
        run_root / "control" / "implementer-start.json"
    )
    assert finished["closed_pane"] == "implementer-pane"
    assert closed == ["implementer-pane"]
    assert receipt["hil_pane"] == "hil-pane"
    assert finished["force"] is False


def test_finish_force_closes_without_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _started, closed = _patch_runtime(monkeypatch)
    implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    receipt_path = run_root / "control" / "implementer-start.json"
    with pytest.raises(ImplementerStartError, match="implementer-result"):
        implementer_controller.finish_implementer(receipt_path)
    finished = implementer_controller.finish_implementer(receipt_path, force=True)
    assert finished["closed_pane"] == "implementer-pane"
    assert finished["result"] is None
    assert finished["force"] is True
    assert closed == ["implementer-pane"]


def test_start_gated_closes_a_mismatched_workspace_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    def herdr_json(*args: object, **kwargs: object) -> dict[str, object]:
        if args[:2] == ("pane", "get"):
            return {"result": {"pane": {"tab_id": "tab-1", "workspace_id": "ws-hil"}}}
        if args[:2] == ("agent", "start"):
            return {
                "result": {
                    "agent": {"pane_id": "leaked-pane", "workspace_id": "ws-other"}
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(implementer_controller.recruiter, "_herdr_json", herdr_json)
    monkeypatch.setattr(
        implementer_controller.recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: closed.append(pane),
    )
    with pytest.raises(ImplementerStartError, match="workspace"):
        implementer_controller._start_gated(
            "name", "hil-pane", tmp_path, tmp_path / "start.sh", "llm-lab-test"
        )
    assert closed == ["leaked-pane"]


def test_release_gate_retries_enxio_then_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "gate"
    opens = {"n": 0}
    written: list[bytes] = []

    def fake_open(target: object, flags: int) -> int:
        opens["n"] += 1
        if opens["n"] < 3:
            raise OSError(errno.ENXIO, "No such device or address")
        return 7

    monkeypatch.setattr(implementer_controller.os, "open", fake_open)
    monkeypatch.setattr(
        implementer_controller.os,
        "write",
        lambda fd, data: written.append(data) or len(data),
    )
    monkeypatch.setattr(implementer_controller.os, "close", lambda fd: None)
    monkeypatch.setattr(implementer_controller.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(implementer_controller.time, "monotonic", lambda: 0.0)
    implementer_controller._release_gate(path, "token")
    assert opens["n"] == 3
    assert written == [b"token\n"]


def test_start_places_the_implementer_in_the_control_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    placed: list[tuple[str, str, str]] = []
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        implementer_controller.recruiter,
        "_place_started_agent_in_role_tab",
        lambda pane, workspace, role, **kwargs: placed.append((pane, workspace, role))
        or pane,
    )
    implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    assert placed == [("implementer-pane", "workspace-1", "control")]


def test_start_rejects_a_leftover_result_without_a_ready_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    run_root.mkdir()
    (run_root / "implementer-result.json").write_text(
        json.dumps(
            {
                "verdict": "passed",
                "summary": "old run",
                "run_root": str(run_root),
                "run_id": "sample-run",
            }
        )
    )
    with pytest.raises(ImplementerStartError, match="new run-root"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )


def test_ready_receipt_rejects_a_changed_source_plan_without_rewriting_frozen_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    frozen = (run_root / "plan.md").read_text()
    other = tmp_path / "other-plan.md"
    other.write_text("# Plan: different\n\nGoal: do not overwrite frozen copy\n")
    with pytest.raises(ImplementerStartError, match="plan does not match"):
        implementer_controller.start_implementer(
            plan_path=other,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )
    assert (run_root / "plan.md").read_text() == frozen
    assert frozen != other.read_text()


def test_ready_receipt_rejects_a_tampered_frozen_plan_without_reattaching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    first = implementer_controller.start_implementer(
        plan_path=plan,
        offering_id="claude-sonnet-5",
        effort="medium",
        run_root=run_root,
        hil_pane="hil-pane",
        cwd=tmp_path,
        roster_path=str(roster),
    )
    frozen = run_root / "plan.md"
    original = frozen.read_text()
    frozen.write_text("# TAMPERED FROZEN PLAN\n")
    with pytest.raises(ImplementerStartError, match="frozen plan does not match"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )
    assert frozen.read_text() == "# TAMPERED FROZEN PLAN\n"
    assert original == plan.read_text()
    assert first["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()


def test_start_rejects_leftover_control_events_without_a_ready_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, run_root, roster = _inputs(tmp_path)
    _patch_runtime(monkeypatch)
    events = run_root / "control" / "events"
    events.mkdir(parents=True)
    (events / "00000001.json").write_text(
        json.dumps({"kind": "completed", "terminal": True, "summary": "stale"})
    )
    with pytest.raises(ImplementerStartError, match="new run-root"):
        implementer_controller.start_implementer(
            plan_path=plan,
            offering_id="claude-sonnet-5",
            effort="medium",
            run_root=run_root,
            hil_pane="hil-pane",
            cwd=tmp_path,
            roster_path=str(roster),
        )
    assert not (run_root / "plan.md").exists()


def test_example_roster_has_plan_implementer_templates_for_every_controller_harness() -> (
    None
):
    roster = implementer_controller.recruiter.load_roster(
        Path(__file__).with_name("upagent.yaml.example")
    )
    assert set(roster["plan_implementers"]) == set(roster["phase_leaders"])
    assert set(roster["plan_implementers"]) <= set(roster["harnesses"])
