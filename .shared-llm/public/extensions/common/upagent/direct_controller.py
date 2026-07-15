#!/usr/bin/env python3
"""Deterministic bridge from a direct route step to an UpAgent order."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
RECRUITER = HERE / "recruiter.py"
DIRECT_STEP_KINDS = ("implement", "review")


class DirectRunError(RuntimeError):
    """A malformed direct route or invalid direct-step lifecycle."""


def _route(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DirectRunError(f"route file not found: {path}")
    try:
        value = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as error:
        raise DirectRunError(
            f"direct route is unreadable or invalid YAML: {error}"
        ) from error
    if not isinstance(value, dict) or value.get("mode") != "direct":
        raise DirectRunError("direct controller requires route.yaml mode: direct")
    return value


def step_order(path: Path) -> list[str]:
    steps = _route(path).get("steps")
    if not isinstance(steps, dict) or not steps:
        raise DirectRunError("direct route must define a non-empty steps mapping")
    deps: dict[str, list[str]] = {}
    for name, value in steps.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise DirectRunError("direct route steps must be named mappings")
        items = value.get("depends_on", [])
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise DirectRunError(
                f"direct route step {name}.depends_on must be a list of strings"
            )
        unknown = [i for i in items if i not in steps]
        if unknown:
            raise DirectRunError(
                f"direct route step {name} references unknown steps: {', '.join(unknown)}"
            )
        deps[name] = items
    out: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise DirectRunError(f"direct route dependency cycle includes {name}")
        if name in done:
            return
        visiting.add(name)
        for dep in deps[name]:
            visit(dep)
        visiting.remove(name)
        done.add(name)
        out.append(name)

    for name in steps:
        visit(name)
    return out


def step_config(path: Path, step_id: str) -> dict[str, Any]:
    route = _route(path)
    steps, profiles = route.get("steps"), route.get("llm_profiles")
    if (
        not isinstance(steps, dict)
        or not isinstance(profiles, dict)
        or not isinstance(steps.get(step_id), dict)
    ):
        raise DirectRunError(f"direct route has no step {step_id}")
    step = steps[step_id]
    profile = profiles.get(step.get("llm_profile"))
    if not isinstance(profile, dict) or not all(
        isinstance(step.get(k), str) and step[k] for k in ("llm_profile", "agent")
    ):
        raise DirectRunError(f"direct step {step_id} references an unknown profile")
    harness, model = profile.get("harness"), profile.get("model")
    if not all(isinstance(v, str) and v for v in (harness, model)):
        raise DirectRunError(f"direct step {step_id} has incomplete profile routing")
    kind = step.get("kind", "implement")
    requires_apply = step.get("requires_apply", False)
    if (
        kind not in DIRECT_STEP_KINDS
        or not isinstance(requires_apply, bool)
        or (kind == "review" and requires_apply)
    ):
        raise DirectRunError(
            f"direct step {step_id} has invalid kind or requires_apply"
        )
    deps = step.get("depends_on", [])
    if not isinstance(deps, list) or not all(isinstance(v, str) for v in deps):
        raise DirectRunError(
            f"direct step {step_id}.depends_on must be a list of strings"
        )
    return {
        "agent": step["agent"],
        "harness": harness,
        "model": model,
        "kind": kind,
        "requires_apply": requires_apply,
        "review_of": deps if kind == "review" else [],
        **(
            {"effort": profile["effort"]}
            if isinstance(profile.get("effort"), str) and profile["effort"]
            else {}
        ),
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_order(
    *,
    plan_id: str,
    step_id: str,
    operation: str,
    cwd: Path,
    run_root: Path,
    tui_pane: str,
    approval: dict[str, str] | None = None,
    plan_artifact: Path | None = None,
    workspace_id: str | None = None,
    workspace_label: str | None = None,
    harness: str = "claude",
    model: str = "configured-claude-model",
    agent: str = "terraform",
    effort: str | None = None,
    requires_apply: bool = False,
    kind: str = "implement",
    review_of: list[str] | None = None,
) -> dict[str, Any]:
    if (
        operation not in ("plan", "apply")
        or kind not in DIRECT_STEP_KINDS
        or (kind == "review" and operation == "apply")
    ):
        raise DirectRunError("review steps cannot create apply orders")
    if (
        not cwd.is_absolute()
        or not cwd.is_dir()
        or not run_root.is_absolute()
        or not run_root.is_dir()
        or not tui_pane
    ):
        raise DirectRunError(
            "direct worker needs existing absolute cwd/run root and TUI pane"
        )
    if not all(
        isinstance(value, str) and value
        for value in (plan_id, step_id, harness, model, agent)
    ):
        raise DirectRunError(
            "direct worker needs non-empty plan, step, harness, model, and agent identifiers"
        )
    root = run_root / "steps" / step_id
    root.mkdir(parents=True, exist_ok=True)
    instructions = root / f"{operation}-instructions.md"
    result = root / f"{operation}-result.json"
    placement: dict[str, str] = {"mode": "requester", "anchor_pane": tui_pane}
    if workspace_id or workspace_label:
        if workspace_id and workspace_label:
            raise DirectRunError(
                "manager placement cannot specify both workspace_id and workspace_label"
            )
        placement = {
            "mode": "workspace",
            "anchor_pane": tui_pane,
            **(
                {"workspace_id": workspace_id}
                if workspace_id
                else {"workspace_label": workspace_label}
            ),
        }
    order: dict[str, Any] = {
        "order_id": f"{plan_id}.{step_id}.{operation}.{time.time_ns()}",
        "mode": "direct",
        "plan_id": plan_id,
        "step_id": step_id,
        "phase_id": f"direct-{plan_id}",
        "stage_id": "stage-1-implementation",
        "harness": harness,
        "model": model,
        "agent": agent,
        "cwd": str(cwd),
        "instructions_path": str(instructions),
        "result_path": str(result),
        "cockpit_pane": tui_pane,
        "manager_placement": placement,
        "operation": operation,
        "requires_apply": requires_apply,
        "step_kind": kind,
        "review_of": review_of or [],
    }
    order["request_id"] = order["order_id"]
    if effort:
        order["effort"] = effort
    if operation == "apply":
        if (
            not plan_artifact
            or not plan_artifact.is_file()
            or not isinstance(approval, dict)
        ):
            raise DirectRunError(
                "apply requires a readable plan artifact and human approval"
            )
        digest = _digest(plan_artifact)
        if approval.get("plan_sha256") != digest:
            raise DirectRunError(
                "approval plan_sha256 does not match the current plan artifact"
            )
        order["plan_artifact"] = {"path": str(plan_artifact), "sha256": digest}
        order["approval"] = approval
        text = f"# Direct IaC apply\n\nApply only approved artifact `{plan_artifact}` with SHA-256 `{digest}`. Do not re-plan, broaden scope, or destroy resources.\n"
    elif kind == "review":
        text = f"# Direct adversarial review\n\nReview completed direct step(s) `{', '.join(review_of or [])}` in `{cwd}`. This is a read-only independent review: do not modify files, apply, destroy, or approve anything.\n"
    else:
        text = f"# Direct IaC plan\n\nImplement only direct plan step `{step_id}` in `{cwd}`. Run init/validate/plan as appropriate; never run apply or destroy.\n"
    instructions.write_text(
        text
        + "Do not create or invoke subagents unless the human explicitly authorized that delegation. Write the normal UpAgent result and exit.\n"
    )
    result.unlink(missing_ok=True)
    return order


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("steps", "order", "apply-order"))
    p.add_argument("--route", type=Path, required=True)
    p.add_argument("--plan-id")
    p.add_argument("--step-id")
    p.add_argument("--cwd", type=Path)
    p.add_argument("--run-root", type=Path)
    p.add_argument("--tui-pane")
    p.add_argument("--approval", type=Path)
    p.add_argument("--plan-artifact", type=Path)
    a = p.parse_args()
    if a.command == "steps":
        print(json.dumps(step_order(a.route)))
        return 0
    if not all((a.plan_id, a.step_id, a.cwd, a.run_root, a.tui_pane)):
        raise DirectRunError("missing direct order arguments")
    approval = (
        json.loads(a.approval.read_text())
        if a.command == "apply-order" and a.approval
        else None
    )
    order = build_order(
        plan_id=a.plan_id,
        step_id=a.step_id,
        operation="apply" if a.command == "apply-order" else "plan",
        cwd=a.cwd,
        run_root=a.run_root,
        tui_pane=a.tui_pane,
        approval=approval,
        plan_artifact=a.plan_artifact,
        **step_config(a.route, a.step_id),
    )
    path = Path(order["instructions_path"]).with_name(
        f"{order['operation']}-order.json"
    )
    path.write_text(json.dumps(order, indent=2) + "\n")
    return subprocess.run(
        [sys.executable, str(RECRUITER), "dispatch", str(path)], check=False
    ).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DirectRunError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"direct-controller: {error}") from error
