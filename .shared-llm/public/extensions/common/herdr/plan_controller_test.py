# pyright: reportMissingImports=false
"""Unit tests for deterministic TUI startup (coordination v2: no standing plan watchdog)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "herdr_plan_controller_tested", Path(__file__).with_name("plan_controller.py")
)
assert _spec and _spec.loader
plan_controller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan_controller)
PlanStartError = plan_controller.PlanStartError


@pytest.fixture(autouse=True)
def _resolved_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_resolve_current_herdr_session_name",
        lambda: "llm-lab-test",
    )
    monkeypatch.setattr(
        plan_controller.run_lifecycle,
        "start_background_heartbeat",
        lambda run_dir, token: {
            "state": "started",
            "token_sha256": plan_controller.run_lifecycle._token_hash(token),
        },
    )


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "sample-run"
    run_dir.mkdir()
    (run_dir / "plan.md").write_text("# Plan\n")
    (run_dir / "route.yaml").write_text(
        "llm_profiles:\n"
        "  cheap:\n"
        "    harness: claude\n"
        "    model: haiku\n"
        "    effort: low\n"
        "finalization_defaults:\n"
        "  watchdog_profile: cheap\n"
        "phases:\n"
        "  phase-0:\n"
        "    lead:\n"
        "      llm_profile: cheap\n"
        "      agent: phase-leader\n"
    )
    roster = tmp_path / "upagent.yaml"
    roster.write_text(
        "harnesses:\n"
        "  claude: 'claude --agent {agent} --model {model} read:{instructions_path}'\n"
    )
    return run_dir, roster


def _healthy_tui(**_: object) -> dict[str, object]:
    return {
        "herdr_session": "llm-lab-test",
        "health": {
            "healthy": True,
            "process_pid": 123,
            "process_start_time": "start-123",
        },
        "pane_id": "tui-pane",
        "workspace_id": "workspace-1",
    }


def test_main_resolves_relative_run_dir_from_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = _inputs(tmp_path)
    captured: dict[str, object] = {}

    def capture_start(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"state": "ready"}

    monkeypatch.chdir(Path(__file__).parent)
    monkeypatch.setattr(plan_controller, "start_plan", capture_start)

    assert (
        plan_controller.main(
            [run_dir.name, "--repo", str(tmp_path), "--slug", "sample-run"]
        )
        == 0
    )
    assert captured["run_dir"] == run_dir
    assert captured["repo"] == tmp_path


def test_tui_launch_names_exact_run_tree_and_verifies_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = _inputs(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {
                "root_pane": {
                    "pane_id": "tui-pane",
                    "tab_id": "control-tab",
                    "workspace_id": "workspace-1",
                }
            }
        },
    )
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_herdr",
        lambda *args, **kwargs: calls.append(args),
    )
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_wait_for_agent_health",
        lambda *args, **kwargs: {"healthy": True},
    )

    tui = plan_controller._create_tui(
        repo=tmp_path,
        plan_path=run_dir / "plan.md",
        route_path=run_dir / "route.yaml",
        slug="sample-run",
        harness="claude",
        owner_token="tok-test",
    )

    assert tui["workspace_id"] == "workspace-1"
    assert tui["control_tab_id"] == "control-tab"
    assert ("tab", "rename", "control-tab", "control") in calls
    run_call = next(call for call in calls if call[:2] == ("pane", "run"))
    assert f"--run-tree {run_dir}" in run_call[3]
    assert "--remote-control=sample-run" in run_call[3]
    assert "HERDR_RUN_OWNER_TOKEN=tok-test" in run_call[3]


def test_claude_tui_always_gets_remote_control_and_pi_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = _inputs(tmp_path)
    commands: dict[str, str] = {}

    def run_for(harness: str) -> str:
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            plan_controller.recruiter,
            "_herdr_json",
            lambda *args, **kwargs: {
                "result": {
                    "workspaces": [],
                    "root_pane": {
                        "pane_id": "tui-pane",
                        "tab_id": "control-tab",
                        "workspace_id": "workspace-1",
                    },
                }
            },
        )
        monkeypatch.setattr(
            plan_controller.recruiter,
            "_herdr",
            lambda *args, **kwargs: calls.append(args),
        )
        monkeypatch.setattr(
            plan_controller.recruiter,
            "_wait_for_agent_health",
            lambda *args, **kwargs: {"healthy": True},
        )
        plan_controller._create_tui(
            repo=tmp_path,
            plan_path=run_dir / "plan.md",
            route_path=run_dir / "route.yaml",
            slug="sample-run",
            harness=harness,
            owner_token="tok-test",
        )
        return next(call for call in calls if call[:2] == ("pane", "run"))[3]

    commands["claude"] = run_for("claude")
    commands["pi"] = run_for("pi")

    assert "--remote-control=sample-run" in commands["claude"]
    assert "--remote-control" not in commands["pi"]


def test_unified_mode_joins_the_existing_herdr_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default single-workspace mode splits a pane into the live `herdr` workspace's control
    tab instead of creating a per-run workspace."""
    run_dir, _ = _inputs(tmp_path)
    calls: list[tuple[str, ...]] = []
    placements: list[tuple[str, ...]] = []

    def herdr_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("workspace", "list"):
            return {
                "result": {
                    "workspaces": [{"label": "herdr", "workspace_id": "ws-herdr"}]
                }
            }
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": [{"pane_id": "recruiter-pane"}]}}
        if args[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "fresh-pane"}}}
        if args[:2] == ("pane", "get"):
            return {
                "result": {
                    "pane": {"tab_id": "control-tab", "workspace_id": "ws-herdr"}
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(plan_controller.recruiter, "_herdr_json", herdr_json)
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_herdr",
        lambda *args, **kwargs: calls.append(args),
    )
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_place_started_agent_in_role_tab",
        lambda pane_id, workspace_id, tab_role, split_direction, **kwargs: (
            placements.append((pane_id, workspace_id, tab_role, split_direction))
            or pane_id
        ),
    )
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_wait_for_agent_health",
        lambda *args, **kwargs: {"healthy": True},
    )

    tui = plan_controller._create_tui(
        repo=tmp_path,
        plan_path=run_dir / "plan.md",
        route_path=run_dir / "route.yaml",
        slug="sample-run",
        harness="claude",
        owner_token="tok-test",
    )

    assert tui["workspace_id"] == "ws-herdr"
    assert tui["workspace_mode"] == "single"
    assert placements == [("fresh-pane", "ws-herdr", "control", "right")]
    # The reused workspace's tabs are not renamed; only the pane is claimed and armed.
    assert ("tab", "rename", "control-tab", "control") not in calls
    assert ("pane", "rename", "fresh-pane", "tui-agent") in calls


def test_separate_workspaces_mode_creates_the_per_run_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = _inputs(tmp_path)
    created: list[tuple[str, ...]] = []

    def herdr_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("workspace", "create"):
            created.append(args)
            return {
                "result": {
                    "root_pane": {
                        "pane_id": "tui-pane",
                        "tab_id": "control-tab",
                        "workspace_id": "ws-run",
                    }
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(plan_controller.recruiter, "_herdr_json", herdr_json)
    monkeypatch.setattr(
        plan_controller.recruiter, "_herdr", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_wait_for_agent_health",
        lambda *args, **kwargs: {"healthy": True},
    )

    tui = plan_controller._create_tui(
        repo=tmp_path,
        plan_path=run_dir / "plan.md",
        route_path=run_dir / "route.yaml",
        slug="sample-run",
        harness="claude",
        owner_token="tok-test",
        separate_workspaces=True,
    )

    assert tui["workspace_mode"] == "separate"
    label = created[0][created[0].index("--label") + 1]
    assert label == "sample-run"


def test_tui_metadata_failure_closes_created_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = _inputs(tmp_path)
    calls: list[tuple[str, ...]] = []

    def herdr_json(*args: str, **kwargs: object) -> dict:
        if args[:2] == ("workspace", "list"):
            return {"result": {"workspaces": []}}
        if args[:2] == ("workspace", "create"):
            return {"result": {"root_pane": {"pane_id": "orphan-pane"}}}
        if args == ("pane", "get", "orphan-pane"):
            return {"result": {"pane": {}}}
        raise AssertionError(args)

    monkeypatch.setattr(plan_controller.recruiter, "_herdr_json", herdr_json)
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_herdr",
        lambda *args, **kwargs: calls.append(args),
    )

    with pytest.raises(PlanStartError, match="has no workspace_id"):
        plan_controller._create_tui(
            repo=tmp_path,
            plan_path=run_dir / "plan.md",
            route_path=run_dir / "route.yaml",
            slug="sample-run",
            harness="claude",
            owner_token="tok-test",
        )

    assert calls == [("pane", "close", "orphan-pane")]


def test_starts_tui_without_a_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    monkeypatch.setattr(plan_controller, "_create_tui", _healthy_tui)

    receipt = plan_controller.start_plan(
        run_dir=run_dir,
        slug="sample-run",
        tui_harness="claude",
        repo=tmp_path,
        roster_path=str(roster),
    )

    assert receipt["state"] == "ready"
    assert receipt["heartbeat"]["state"] == "started"
    assert "token" not in receipt["run_owner"]
    assert isinstance(receipt["run_owner"]["token_sha256"], str)
    assert receipt["tui"]["pane_id"] == "tui-pane"
    assert receipt["watchdog"]["state"] == "not-configured"
    assert not (run_dir / "plan-watchdog").exists()
    persisted = json.loads((run_dir / "control/plan-start.json").read_text())
    assert persisted == receipt
    assert oct((run_dir / "control").stat().st_mode & 0o777) == "0o700"
    with pytest.raises(PlanStartError, match="startup artifacts already exist"):
        plan_controller.start_plan(
            run_dir=run_dir,
            slug="sample-run",
            tui_harness="claude",
            repo=tmp_path,
            roster_path=str(roster),
        )


def test_finish_plan_writes_the_terminal_marker_only_after_summary_exists(
    tmp_path: Path,
) -> None:
    run_dir, _roster = _inputs(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "slug": "sample-run",
                "state": "ready",
                "tui": {"herdr_session": "llm-lab-test"},
            }
        )
    )

    with pytest.raises(PlanStartError, match="run summary not found"):
        plan_controller.finish_plan(
            run_dir=run_dir, slug="sample-run", state="succeeded"
        )

    (run_dir / "run-status.md").write_text("# Complete\n")
    with pytest.raises(PlanStartError, match="missing phase result"):
        plan_controller.finish_plan(run_dir=run_dir, slug=None, state="succeeded")

    phase_result = run_dir / "phases/phase-0/phase-result.json"
    phase_result.parent.mkdir(parents=True)
    phase_result.write_text(json.dumps({"phase_id": "phase-0", "verdict": "passed"}))
    marker = plan_controller.finish_plan(run_dir=run_dir, slug=None, state="succeeded")

    assert marker["plan_id"] == "sample-run"
    assert marker["state"] == "succeeded"
    assert marker["summary_path"] == str(run_dir / "run-status.md")
    assert json.loads((control / "run-terminal.json").read_text()) == marker


def test_finish_plan_requires_env_or_cli_owner_token_for_new_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _roster = _inputs(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    token = "owner-token"
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "run_owner": {
                    "generation": 1,
                    "role": "owner",
                    "token_sha256": plan_controller.run_lifecycle._token_hash(token),
                },
                "slug": "sample-run",
                "state": "ready",
                "tui": {"herdr_session": "llm-lab-test"},
            }
        )
    )
    (run_dir / "run-status.md").write_text("# Stopped\n")
    monkeypatch.delenv(plan_controller.run_lifecycle.HERDR_RUN_OWNER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        plan_controller.run_lifecycle,
        "guard",
        lambda *args, **kwargs: pytest.fail("finish must not depend on heartbeat guard"),
    )

    with pytest.raises(PlanStartError, match="requires HERDR_RUN_OWNER_TOKEN"):
        plan_controller.finish_plan(run_dir=run_dir, slug=None, state="stopped")
    with pytest.raises(PlanStartError, match="does not match"):
        plan_controller.finish_plan(
            run_dir=run_dir, slug=None, state="stopped", owner_token="wrong"
        )

    marker = plan_controller.finish_plan(
        run_dir=run_dir, slug=None, state="stopped", owner_token=token
    )

    assert marker["state"] == "stopped"
    assert json.loads((control / "run-terminal.json").read_text()) == marker


def test_finish_plan_rejects_a_non_object_start_receipt(tmp_path: Path) -> None:
    run_dir, _roster = _inputs(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (run_dir / "run-status.md").write_text("# Complete\n")
    (control / "plan-start.json").write_text("[]\n")

    with pytest.raises(PlanStartError, match="must be an object"):
        plan_controller.finish_plan(run_dir=run_dir, slug=None, state="stopped")


def test_finish_plan_rejects_missing_tui_session(tmp_path: Path) -> None:
    run_dir, _roster = _inputs(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "slug": "sample-run",
                "state": "ready-degraded",
                "watchdog": {"state": "unavailable"},
            }
        )
    )
    (run_dir / "run-status.md").write_text("# Complete\n")
    phase_result = run_dir / "phases/phase-0/phase-result.json"
    phase_result.parent.mkdir(parents=True)
    phase_result.write_text(json.dumps({"phase_id": "phase-0", "verdict": "passed"}))

    with pytest.raises(PlanStartError, match="TUI identity"):
        plan_controller.finish_plan(run_dir=run_dir, slug=None, state="succeeded")


def test_finish_plan_rejects_tui_without_session(tmp_path: Path) -> None:
    run_dir, _roster = _inputs(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "slug": "sample-run",
                "state": "ready",
                "tui": {"pane_id": "tui-pane"},
            }
        )
    )
    (run_dir / "run-status.md").write_text("# Complete\n")

    with pytest.raises(PlanStartError, match="no recorded Herdr session"):
        plan_controller.finish_plan(run_dir=run_dir, slug=None, state="stopped")


def test_finish_plan_rejects_tui_session_mismatch(tmp_path: Path) -> None:
    run_dir, _roster = _inputs(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "slug": "sample-run",
                "state": "ready",
                "tui": {"herdr_session": "other-session"},
            }
        )
    )
    (run_dir / "run-status.md").write_text("# Complete\n")
    phase_result = run_dir / "phases/phase-0/phase-result.json"
    phase_result.parent.mkdir(parents=True)
    phase_result.write_text(json.dumps({"phase_id": "phase-0", "verdict": "passed"}))

    with pytest.raises(PlanStartError, match="different Herdr session"):
        plan_controller.finish_plan(run_dir=run_dir, slug=None, state="succeeded")


def test_ready_degraded_receipt_with_tui_session_is_finishable(tmp_path: Path) -> None:
    run_dir, _roster = _inputs(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "slug": "sample-run",
                "state": "ready-degraded",
                "tui": {"herdr_session": "llm-lab-test"},
                "watchdog": {"state": "unavailable"},
            }
        )
    )
    (run_dir / "run-status.md").write_text("# Complete\n")
    phase_result = run_dir / "phases/phase-0/phase-result.json"
    phase_result.parent.mkdir(parents=True)
    phase_result.write_text(json.dumps({"phase_id": "phase-0", "verdict": "passed"}))

    marker = plan_controller.finish_plan(run_dir=run_dir, slug=None, state="succeeded")

    assert marker["plan_id"] == "sample-run"
    assert marker["state"] == "succeeded"


def test_tui_failure_is_terminal_and_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    monkeypatch.setattr(
        plan_controller,
        "_create_tui",
        lambda **kwargs: (_ for _ in ()).throw(PlanStartError("no TUI process")),
    )

    with pytest.raises(PlanStartError, match="no TUI process"):
        plan_controller.start_plan(
            run_dir=run_dir,
            slug="sample-run",
            tui_harness="claude",
            repo=tmp_path,
            roster_path=str(roster),
        )

    receipt = json.loads((run_dir / "control/plan-start.json").read_text())
    assert receipt["state"] == "failed"
    assert receipt["reason"] == "no TUI process"
    assert "token" not in receipt["run_owner"]


def test_lifecycle_error_during_start_is_durable_failed_not_preparing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    monkeypatch.setattr(
        plan_controller.run_lifecycle,
        "acquire_owner",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            plan_controller.run_lifecycle.LifecycleError("bad lease")
        ),
    )

    with pytest.raises(PlanStartError, match="bad lease"):
        plan_controller.start_plan(
            run_dir=run_dir,
            slug="sample-run",
            tui_harness="claude",
            repo=tmp_path,
            roster_path=str(roster),
        )

    receipt = json.loads((run_dir / "control/plan-start.json").read_text())
    assert receipt["state"] == "failed"
    assert receipt["reason"] == "bad lease"


def test_start_plan_never_touches_agent_panes_for_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    monkeypatch.setattr(plan_controller, "_create_tui", _healthy_tui)
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: pytest.fail("startup must not inject into panes"),
    )

    receipt = plan_controller.start_plan(
        run_dir=run_dir,
        slug="sample-run",
        tui_harness="claude",
        repo=tmp_path,
        roster_path=str(roster),
    )

    assert receipt["state"] == "ready"
