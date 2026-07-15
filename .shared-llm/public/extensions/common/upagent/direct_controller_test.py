"""Unit tests for the direct-route order bridge."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location(
    "upagent_direct_controller", Path(__file__).with_name("direct_controller.py")
)
assert _spec and _spec.loader
direct_controller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(direct_controller)
DirectRunError = direct_controller.DirectRunError


def _route(path: Path, steps: str) -> None:
    path.write_text(
        "mode: direct\n"
        "llm_profiles:\n"
        "  operator:\n"
        "    harness: claude\n"
        "    model: configured-claude-model\n"
        "    effort: medium\n"
        "steps:\n"
        f"{steps}"
    )


def test_direct_steps_are_dependency_ordered_and_profiled(tmp_path: Path) -> None:
    route = tmp_path / "route.yaml"
    _route(
        route,
        "  plan:\n"
        "    llm_profile: operator\n"
        "    agent: terraform\n"
        "  review:\n"
        "    llm_profile: operator\n"
        "    agent: reviewer\n"
        "    kind: review\n"
        "    depends_on: [plan]\n",
    )

    assert direct_controller.step_order(route) == ["plan", "review"]
    assert direct_controller.step_config(route, "review") == {
        "agent": "reviewer",
        "harness": "claude",
        "model": "configured-claude-model",
        "effort": "medium",
        "kind": "review",
        "requires_apply": False,
        "review_of": ["plan"],
    }


def test_direct_apply_order_binds_human_approval_to_exact_artifact(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "worktree"
    run_root = tmp_path / "run"
    cwd.mkdir()
    run_root.mkdir()
    artifact = tmp_path / "approved.plan"
    artifact.write_text("exact plan")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    approval = {
        "approved_by": "human",
        "approved_at": "2026-07-15T00:00:00Z",
        "nonce": "approval-1",
        "plan_sha256": digest,
    }

    order = direct_controller.build_order(
        plan_id="network",
        step_id="apply-network",
        operation="apply",
        cwd=cwd,
        run_root=run_root,
        tui_pane="tui-pane",
        plan_artifact=artifact,
        approval=approval,
    )

    assert order["mode"] == "direct"
    assert order["operation"] == "apply"
    assert order["approval"] == approval
    assert order["plan_artifact"] == {"path": str(artifact), "sha256": digest}
    assert "Do not re-plan" in Path(order["instructions_path"]).read_text()


def test_direct_apply_rejects_non_object_or_stale_approval(tmp_path: Path) -> None:
    cwd = tmp_path / "worktree"
    run_root = tmp_path / "run"
    cwd.mkdir()
    run_root.mkdir()
    artifact = tmp_path / "approved.plan"
    artifact.write_text("exact plan")

    with pytest.raises(DirectRunError, match="human approval"):
        direct_controller.build_order(
            plan_id="network",
            step_id="apply-network",
            operation="apply",
            cwd=cwd,
            run_root=run_root,
            tui_pane="tui-pane",
            plan_artifact=artifact,
            approval=json.loads("[]"),
        )
    with pytest.raises(DirectRunError, match="does not match"):
        direct_controller.build_order(
            plan_id="network",
            step_id="apply-network",
            operation="apply",
            cwd=cwd,
            run_root=run_root,
            tui_pane="tui-pane",
            plan_artifact=artifact,
            approval={"plan_sha256": "stale"},
        )
