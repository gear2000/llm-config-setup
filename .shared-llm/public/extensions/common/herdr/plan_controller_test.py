"""Unit tests for deterministic TUI + plan-watchdog startup."""

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
    )
    roster = tmp_path / "upagent.yaml"
    roster.write_text(
        "harnesses:\n"
        "  claude: 'claude --agent {agent} --model {model} read:{instructions_path}'\n"
    )
    return run_dir, roster


def _healthy_tui(**_: object) -> dict[str, object]:
    return {
        "health": {"healthy": True},
        "pane_id": "tui-pane",
        "workspace_id": "workspace-1",
    }


def _healthy_watchdog(_: Path, __: str) -> dict[str, object]:
    return {
        "manager_address": "manager-address",
        "manager_pane": "manager-pane",
        "manager_workspace_id": "workspace-1",
        "request_id": "sample-run.plan-watchdog.workspace-1",
        "state": "running",
        "worker_address": "watchdog-address",
        "worker_pane": "watchdog-pane",
        "worker_workspace_id": "workspace-1",
    }


def test_tui_launch_names_exact_run_tree_and_verifies_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = _inputs(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_herdr_json",
        lambda *args: {
            "result": {
                "root_pane": {
                    "pane_id": "tui-pane",
                    "workspace_id": "workspace-1",
                }
            }
        },
    )
    monkeypatch.setattr(
        plan_controller.recruiter, "_herdr", lambda *args: calls.append(args)
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
    )

    assert tui["workspace_id"] == "workspace-1"
    run_call = next(call for call in calls if call[:2] == ("pane", "run"))
    assert f"--run-tree {run_dir}" in run_call[3]


def test_tui_metadata_failure_closes_created_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = _inputs(tmp_path)
    calls: list[tuple[str, ...]] = []

    def herdr_json(*args: str) -> dict:
        if args[:2] == ("workspace", "create"):
            return {"result": {"root_pane": {"pane_id": "orphan-pane"}}}
        if args == ("pane", "get", "orphan-pane"):
            return {"result": {"pane": {}}}
        raise AssertionError(args)

    monkeypatch.setattr(plan_controller.recruiter, "_herdr_json", herdr_json)
    monkeypatch.setattr(
        plan_controller.recruiter, "_herdr", lambda *args: calls.append(args)
    )

    with pytest.raises(PlanStartError, match="has no workspace_id"):
        plan_controller._create_tui(
            repo=tmp_path,
            plan_path=run_dir / "plan.md",
            route_path=run_dir / "route.yaml",
            slug="sample-run",
            harness="claude",
        )

    assert calls == [("pane", "close", "orphan-pane")]


def test_starts_tui_and_managed_watchdog_in_same_cockpit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    monkeypatch.setattr(plan_controller, "_create_tui", _healthy_tui)
    monkeypatch.setattr(plan_controller, "_request_watchdog", _healthy_watchdog)

    receipt = plan_controller.start_plan(
        run_dir=run_dir,
        slug="sample-run",
        tui_harness="claude",
        repo=tmp_path,
        roster_path=str(roster),
    )

    assert receipt["state"] == "ready"
    assert receipt["tui"]["pane_id"] == "tui-pane"
    assert receipt["watchdog"]["manager_pane"] == "manager-pane"
    assert receipt["watchdog"]["worker_pane"] == "watchdog-pane"
    persisted = json.loads((run_dir / "control/plan-start.json").read_text())
    assert persisted == receipt
    order = json.loads((run_dir / "plan-watchdog/order.json").read_text())
    assert order["agent"] == "plan-lifecycle-watchdog"
    assert order["cockpit_pane"] == "tui-pane"
    assert order["manager_placement"] == {
        "anchor_pane": "tui-pane",
        "mode": "requester",
    }
    assert order["mode"] == "direct"
    assert order["plan_id"] == "sample-run"
    assert order["step_id"] == "plan-watchdog"
    assert order["requester"]["address"] == "tui-pane"
    with pytest.raises(PlanStartError, match="startup artifacts already exist"):
        plan_controller.start_plan(
            run_dir=run_dir,
            slug="sample-run",
            tui_harness="claude",
            repo=tmp_path,
            roster_path=str(roster),
        )


def test_watchdog_failure_is_visible_but_does_not_fail_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(plan_controller, "_create_tui", _healthy_tui)
    monkeypatch.setattr(
        plan_controller,
        "_request_watchdog",
        lambda order, roster: (_ for _ in ()).throw(PlanStartError("bad profile")),
    )
    monkeypatch.setattr(
        plan_controller,
        "_notify_tui",
        lambda pane, message: messages.append((pane, message)) or None,
    )

    receipt = plan_controller.start_plan(
        run_dir=run_dir,
        slug="sample-run",
        tui_harness="claude",
        repo=tmp_path,
        roster_path=str(roster),
    )

    assert receipt["state"] == "ready-degraded"
    assert receipt["tui"]["pane_id"] == "tui-pane"
    assert receipt["watchdog"]["state"] == "unavailable"
    assert messages == [
        (
            "tui-pane",
            "PLAN_WATCHDOG_UNAVAILABLE: bad profile. Continue the run; do not wait for monitoring.",
        )
    ]


def test_bad_watchdog_profile_degrades_after_tui_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    (run_dir / "route.yaml").write_text(
        "llm_profiles: {}\nfinalization_defaults:\n  watchdog_profile: missing\n"
    )
    messages: list[str] = []
    monkeypatch.setattr(plan_controller, "_create_tui", _healthy_tui)
    monkeypatch.setattr(
        plan_controller,
        "_notify_tui",
        lambda pane, message: messages.append(message) or None,
    )

    receipt = plan_controller.start_plan(
        run_dir=run_dir,
        slug="sample-run",
        tui_harness="claude",
        repo=tmp_path,
        roster_path=str(roster),
    )

    assert receipt["state"] == "ready-degraded"
    assert "route.llm_profiles.missing must be an object" in receipt["reason"]
    assert messages and messages[0].startswith("PLAN_WATCHDOG_UNAVAILABLE:")


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


def test_workspace_mismatch_degrades_and_notifies_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, roster = _inputs(tmp_path)
    wrong = _healthy_watchdog(run_dir, str(roster))
    wrong["worker_workspace_id"] = "other-workspace"
    messages: list[str] = []
    monkeypatch.setattr(plan_controller, "_create_tui", _healthy_tui)
    monkeypatch.setattr(plan_controller, "_request_watchdog", lambda *_: wrong)
    monkeypatch.setattr(
        plan_controller,
        "_notify_tui",
        lambda pane, message: messages.append(message) or None,
    )

    receipt = plan_controller.start_plan(
        run_dir=run_dir,
        slug="sample-run",
        tui_harness="claude",
        repo=tmp_path,
        roster_path=str(roster),
    )

    assert receipt["state"] == "ready-degraded"
    assert "did not start in the TUI workspace" in messages[0]
    assert messages[0].startswith("PLAN_WATCHDOG_DEGRADED:")
    assert receipt["watchdog"]["state"] == "ready-misplaced"
    assert receipt["watchdog"]["worker_pane"] == "watchdog-pane"


def test_tui_notification_uses_idle_checked_atomic_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        plan_controller.recruiter,
        "_submit_agent_prompt",
        lambda target, message, idle_timeout_ms: calls.append(
            (target, message, idle_timeout_ms)
        ),
    )

    assert plan_controller._notify_tui("tui-pane", "Watchdog unavailable") is None
    assert calls == [("tui-pane", "Watchdog unavailable", 5_000)]
