#!/usr/bin/env python3
"""Deterministically start a Herdr TUI and its run-level lifecycle watchdog.

The launcher, rather than the TUI prompt, owns this startup transaction. A TUI is not
reported as started until its harness process is healthy.

Coordination v2 creates no standing plan-lifecycle watchdog. The TUI blocks inside
`upagent-phase-await` for every phase it runs, and urgent unacknowledged events escalate
to the human via `herdr notification`. The plan-start receipt keeps a `watchdog` block as
`not-configured` so older readers continue to work.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import time
from typing import Any, cast, Iterator
import uuid

import yaml


HERE = Path(__file__).resolve().parent
UPAGENT_DIR = HERE.parent / "upagent"
_recruiter_spec = importlib.util.spec_from_file_location(
    "upagent_plan_recruiter", UPAGENT_DIR / "recruiter.py"
)
if _recruiter_spec is None or _recruiter_spec.loader is None:
    raise RuntimeError("could not load UpAgent Recruiter")
recruiter = cast(Any, importlib.util.module_from_spec(_recruiter_spec))
_recruiter_spec.loader.exec_module(recruiter)

STARTUP_TIMEOUT_MS = 45_000
TUI_LAUNCHES = {
    "claude": (
        "claude --dangerously-skip-permissions",
        "claude",
        "claude",
    ),
    "pi": (
        "pi --approve",
        "pi",
        "pi",
    ),
}
PLAN_TERMINAL_STATES = ("succeeded", "stopped")
CONTROL_TAB_LABEL = "control"


class PlanStartError(RuntimeError):
    """A fail-loud plan startup contract or transaction fault."""


@contextmanager
def _exclusive_plan_start(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanStartError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanStartError(f"{field} must be a non-empty string")
    return value


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )[:80]


def _find_unified_workspace() -> dict | None:
    """The unified `herdr` workspace from a live `workspace list`, or None if absent."""
    workspaces = (
        recruiter._herdr_json("workspace", "list")
        .get("result", {})
        .get("workspaces", [])
    )
    for workspace in workspaces:
        if (
            isinstance(workspace, dict)
            and workspace.get("label") == recruiter.UNIFIED_WORKSPACE_LABEL
            and isinstance(workspace.get("workspace_id"), str)
            and workspace.get("workspace_id")
        ):
            return workspace
    return None


def _split_pane_into_control_tab(workspace_id: str) -> str:
    """A fresh pane for this TUI in the unified workspace's `control` tab (joined when present,
    created otherwise). Concurrent runs share the tab; the Recruiter's layout lock serializes
    the placement."""
    panes = (
        recruiter._herdr_json("pane", "list", "--workspace", workspace_id)
        .get("result", {})
        .get("panes", [])
    )
    anchor = next(
        (p["pane_id"] for p in panes if isinstance(p, dict) and p.get("pane_id")), None
    )
    if anchor is None:
        raise PlanStartError(
            f"unified workspace {workspace_id} has no pane to split from"
        )
    split = recruiter._herdr_json(
        "pane", "split", anchor, "--direction", "right", "--no-focus"
    )
    pane_id = split.get("result", {}).get("pane", {}).get("pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        raise PlanStartError("herdr pane split returned no pane_id")
    return recruiter._place_started_agent_in_role_tab(
        pane_id, workspace_id, CONTROL_TAB_LABEL, split_direction="right"
    )


def _create_tui(
    *,
    repo: Path,
    plan_path: Path,
    route_path: Path,
    slug: str,
    harness: str,
    separate_workspaces: bool = False,
) -> dict[str, object]:
    if harness not in TUI_LAUNCHES:
        raise PlanStartError(
            f"unknown TUI harness {harness!r}; expected one of "
            + ", ".join(TUI_LAUNCHES)
        )
    launch, expected_agent, expected_process = TUI_LAUNCHES[harness]
    if harness == "claude":
        # The cockpit must always be drivable from the phone: give the Claude Code TUI a
        # Remote Control session named after the run (= form keeps the trailing prompt
        # argument from being read as the session name).
        launch += f" --remote-control={shlex.quote(_safe_name(slug) or 'herdr-run')}"
    pane_id: str | None = None
    tab_id: str | None = None
    workspace_id: str | None = None
    unified = None if separate_workspaces else _find_unified_workspace()
    created_workspace = unified is None
    if created_workspace:
        # Separate mode gets a per-run `<slug>` workspace; the unified default creates the
        # one `herdr` workspace only when nothing (services included) has created it yet.
        label = slug if separate_workspaces else recruiter.UNIFIED_WORKSPACE_LABEL
        response = recruiter._herdr_json(
            "workspace", "create", "--cwd", str(repo), "--label", label, "--no-focus"
        )
        result = response.get("result", {})
        root_pane = result.get("root_pane", {}) if isinstance(result, dict) else {}
        pane_id = root_pane.get("pane_id") if isinstance(root_pane, dict) else None
        tab_id = root_pane.get("tab_id") if isinstance(root_pane, dict) else None
        workspace_id = (
            root_pane.get("workspace_id") if isinstance(root_pane, dict) else None
        )
    command = f"cd {shlex.quote(str(repo))} && {launch} " + shlex.quote(
        f"/herdr-run --plan {plan_path} --route {route_path} "
        f"--run-tree {plan_path.parent} --slug {slug}"
    )
    try:
        if not created_workspace and unified is not None:
            workspace_id = unified["workspace_id"]
            pane_id = _split_pane_into_control_tab(workspace_id)
        if not isinstance(pane_id, str) or not pane_id:
            raise PlanStartError("herdr workspace create returned no root pane_id")
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or not isinstance(tab_id, str)
            or not tab_id
        ):
            pane = (
                recruiter._herdr_json("pane", "get", pane_id)
                .get("result", {})
                .get("pane", {})
            )
            if not isinstance(workspace_id, str) or not workspace_id:
                workspace_id = (
                    pane.get("workspace_id") if isinstance(pane, dict) else None
                )
            if not isinstance(tab_id, str) or not tab_id:
                tab_id = pane.get("tab_id") if isinstance(pane, dict) else None
        if not isinstance(workspace_id, str) or not workspace_id:
            raise PlanStartError(f"TUI pane {pane_id} has no workspace_id")
        if not isinstance(tab_id, str) or not tab_id:
            raise PlanStartError(f"TUI pane {pane_id} has no tab_id")
        if created_workspace:
            recruiter._herdr("tab", "rename", tab_id, CONTROL_TAB_LABEL)
        recruiter._herdr("pane", "rename", pane_id, "tui-agent")
        recruiter._herdr("pane", "run", pane_id, command)
        health = recruiter._wait_for_agent_health(
            pane_id,
            expected_agent=expected_agent,
            expected_process=expected_process,
            expected_cwd=str(repo),
            timeout_ms=STARTUP_TIMEOUT_MS,
        )
    except (OSError, PlanStartError, recruiter.RecruiterError) as error:
        cleanup_error: str | None = None
        try:
            # Close the workspace only when this startup created it; a reused unified
            # workspace still hosts the services (and possibly other runs).
            if created_workspace and isinstance(workspace_id, str) and workspace_id:
                recruiter._herdr("workspace", "close", workspace_id)
            elif isinstance(pane_id, str) and pane_id:
                recruiter._herdr("pane", "close", pane_id)
        except recruiter.RecruiterError as cleanup:
            cleanup_error = str(cleanup)
        detail = f"TUI startup failed: {error}"
        if cleanup_error is not None:
            detail += f"; startup cleanup also failed: {cleanup_error}"
        raise PlanStartError(detail) from error
    return {
        "control_tab_id": tab_id,
        "health": health,
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "workspace_mode": "separate" if separate_workspaces else "single",
    }


def finish_plan(*, run_dir: Path, slug: str | None, state: str) -> dict[str, object]:
    """Publish the durable plan terminal fact that authorizes watchdog cleanup."""
    if not run_dir.is_absolute() or not run_dir.is_dir():
        raise PlanStartError(
            f"run dir must be an existing absolute directory: {run_dir}"
        )
    if state not in PLAN_TERMINAL_STATES:
        raise PlanStartError(
            "plan terminal state must be one of " + ", ".join(PLAN_TERMINAL_STATES)
        )
    summary_path = run_dir / "run-status.md"
    if not summary_path.is_file():
        raise PlanStartError(f"run summary not found: {summary_path}")
    receipt_path = run_dir / "control/plan-start.json"
    try:
        receipt = json.loads(receipt_path.read_text())
    except FileNotFoundError as error:
        raise PlanStartError(f"plan-start receipt not found: {receipt_path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise PlanStartError(f"plan-start receipt is invalid: {error}") from error
    if not isinstance(receipt, dict):
        raise PlanStartError("plan-start receipt must be an object")
    receipt_slug = receipt.get("slug")
    if not isinstance(receipt_slug, str) or not receipt_slug:
        raise PlanStartError("plan-start receipt has no valid slug")
    if slug and receipt_slug != slug:
        raise PlanStartError(f"plan-start receipt does not belong to plan {slug!r}")
    if receipt.get("state") not in ("ready", "ready-degraded"):
        raise PlanStartError("plan-start receipt is not in a finishable ready state")
    plan_id = slug or receipt_slug
    if state == "succeeded":
        route_path = run_dir / "route.yaml"
        try:
            route = yaml.safe_load(route_path.read_text())
        except FileNotFoundError as error:
            raise PlanStartError(f"route not found: {route_path}") from error
        except (OSError, yaml.YAMLError) as error:
            raise PlanStartError(f"route is invalid: {error}") from error
        route = _object(route, "route")
        phases = _object(route.get("phases"), "route.phases")
        if not phases:
            raise PlanStartError("route.phases must contain at least one phase")
        for phase_id in phases:
            if not isinstance(phase_id, str) or not phase_id:
                raise PlanStartError("route.phases keys must be non-empty strings")
            phase_result_path = run_dir / "phases" / phase_id / "phase-result.json"
            try:
                phase_result = json.loads(phase_result_path.read_text())
            except FileNotFoundError as error:
                raise PlanStartError(
                    f"successful plan is missing phase result: {phase_result_path}"
                ) from error
            except (OSError, json.JSONDecodeError) as error:
                raise PlanStartError(
                    f"phase result {phase_result_path} is invalid: {error}"
                ) from error
            if (
                not isinstance(phase_result, dict)
                or phase_result.get("phase_id") != phase_id
                or phase_result.get("verdict") != "passed"
            ):
                raise PlanStartError(
                    f"successful plan requires a passed matching phase result: {phase_result_path}"
                )
    marker_path = run_dir / "control/run-terminal.json"
    marker: dict[str, object] = {
        "at_ns": time.time_ns(),
        "plan_id": plan_id,
        "state": state,
        "summary_path": str(summary_path),
    }
    if marker_path.exists():
        try:
            existing = json.loads(marker_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise PlanStartError(
                f"existing plan terminal marker is invalid: {error}"
            ) from error
        if (
            not isinstance(existing, dict)
            or existing.get("plan_id") != plan_id
            or existing.get("state") != state
            or existing.get("summary_path") != str(summary_path)
        ):
            raise PlanStartError(
                "existing plan terminal marker conflicts with requested outcome"
            )
        return cast(dict[str, object], existing)
    _write_json_atomic(marker_path, marker)
    return marker


def _services_separate_workspaces() -> bool:
    """The workspace-mode choice persisted by the services' `up` (Recruiter STATE_FILE).
    Absent or unreadable state means the unified default (False)."""
    try:
        state = json.loads(recruiter.STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(state.get("separate_workspaces")) if isinstance(state, dict) else False


def start_plan(
    *,
    run_dir: Path,
    slug: str,
    tui_harness: str,
    repo: Path,
    roster_path: str,
    separate_workspaces: bool = False,
) -> dict[str, object]:
    if not run_dir.is_absolute() or not run_dir.is_dir():
        raise PlanStartError(
            f"run dir must be an existing absolute directory: {run_dir}"
        )
    if not repo.is_absolute() or not repo.is_dir():
        raise PlanStartError(f"repo must be an existing absolute directory: {repo}")
    if not slug.strip():
        raise PlanStartError("slug must be non-empty")
    with _exclusive_plan_start(run_dir / ".plan-start.lock"):
        return _start_plan_locked(
            run_dir=run_dir,
            slug=slug,
            tui_harness=tui_harness,
            repo=repo,
            roster_path=roster_path,
            separate_workspaces=separate_workspaces,
        )


def _start_plan_locked(
    *,
    run_dir: Path,
    slug: str,
    tui_harness: str,
    repo: Path,
    roster_path: str,
    separate_workspaces: bool = False,
) -> dict[str, object]:
    plan_path = run_dir / "plan.md"
    route_path = run_dir / "route.yaml"
    if not plan_path.is_file():
        raise PlanStartError(f"plan not found: {plan_path}")
    if not route_path.is_file():
        raise PlanStartError(f"route not found: {route_path}")

    control_dir = run_dir / "control"
    receipt_path = control_dir / "plan-start.json"
    if receipt_path.exists():
        raise PlanStartError(
            f"plan startup artifacts already exist under {run_dir}; use a fresh run directory"
        )

    _write_json_atomic(
        receipt_path,
        {
            "at_ns": time.time_ns(),
            "run_dir": str(run_dir),
            "slug": slug,
            "state": "preparing",
        },
    )
    try:
        tui = _create_tui(
            repo=repo,
            plan_path=plan_path,
            route_path=route_path,
            slug=slug,
            harness=tui_harness,
            separate_workspaces=separate_workspaces,
        )
    except (OSError, PlanStartError, recruiter.RecruiterError) as error:
        _write_json_atomic(
            receipt_path,
            {
                "at_ns": time.time_ns(),
                "reason": str(error),
                "run_dir": str(run_dir),
                "slug": slug,
                "state": "failed",
            },
        )
        raise

    receipt: dict[str, object] = {
        "at_ns": time.time_ns(),
        "run_dir": str(run_dir),
        "slug": slug,
        "state": "ready",
        "tui": tui,
        "watchdog": {
            "reason": "coordination v2: phase-await owns delivery and reconciliation; no standing watchdog is created",
            "state": "not-configured",
        },
    }
    _write_json_atomic(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-plan-start")
    parser.add_argument(
        "run_dir", type=Path, help="directory containing plan.md and route.yaml"
    )
    parser.add_argument(
        "--slug", help="cockpit/run label; defaults to the run directory name"
    )
    parser.add_argument("--harness", default="claude", choices=tuple(TUI_LAUNCHES))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--roster", default=recruiter.default_roster_path())
    parser.add_argument("--finish-state", choices=PLAN_TERMINAL_STATES)
    parser.add_argument(
        "--separate-workspaces",
        action="store_true",
        help="create a per-run <slug> workspace instead of joining the unified `herdr` "
        "workspace (the mode chosen at `just herdr-up` is inherited by default)",
    )
    args = parser.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    run_dir = args.run_dir.expanduser()
    if not run_dir.is_absolute():
        run_dir = repo / run_dir
    run_dir = run_dir.resolve()
    try:
        if args.finish_state is not None:
            result = finish_plan(
                run_dir=run_dir,
                slug=args.slug,
                state=args.finish_state,
            )
            print(f"PLAN_FINISHED {json.dumps(result, sort_keys=True)}", flush=True)
            return 0
        result = start_plan(
            run_dir=run_dir,
            slug=args.slug or run_dir.name,
            tui_harness=args.harness,
            repo=repo,
            roster_path=args.roster,
            separate_workspaces=args.separate_workspaces
            or _services_separate_workspaces(),
        )
    except (OSError, PlanStartError, recruiter.RecruiterError) as error:
        sys.exit(f"herdr-plan-start: {error}")
    print(f"PLAN_STARTED {json.dumps(result, sort_keys=True)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
