#!/usr/bin/env python3
"""Deterministically start a Herdr TUI and its run-level lifecycle watchdog.

The launcher, rather than the TUI prompt, owns this startup transaction.  A TUI is not
reported as started until its harness process is healthy.  The plan watchdog is then hired
through the ordinary UpAgent lifecycle into the same cockpit.  Watchdog failure is durable and
visible but deliberately degraded: monitoring must never prevent a healthy plan from running.
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
import subprocess
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
WATCHDOG_REQUEST_TIMEOUT_SECONDS = 180
PLAN_WATCHDOG_TIMEOUT_MS = 10 * 60 * 60 * 1000
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


def _load_watchdog_profile(route_path: Path) -> dict[str, str]:
    if not route_path.is_absolute() or not route_path.is_file():
        raise PlanStartError(f"route must be an existing absolute file: {route_path}")
    try:
        route = yaml.safe_load(route_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise PlanStartError(
            f"route {route_path} is unreadable or invalid YAML: {error}"
        ) from error
    route = _object(route, "route")
    profiles = _object(route.get("llm_profiles"), "route.llm_profiles")
    defaults = _object(
        route.get("finalization_defaults"), "route.finalization_defaults"
    )
    configured_profile = defaults.get("watchdog_profile")
    if configured_profile is None:
        phases = _object(route.get("phases"), "route.phases")
        if not phases:
            raise PlanStartError(
                "route.phases must contain a phase when watchdog_profile is omitted"
            )
        first_phase_name, first_phase_value = next(iter(phases.items()))
        first_phase = _object(
            first_phase_value, f"route.phases.{first_phase_name}"
        )
        lead = _object(
            first_phase.get("lead"), f"route.phases.{first_phase_name}.lead"
        )
        profile_name = _string(
            lead.get("llm_profile"),
            f"route.phases.{first_phase_name}.lead.llm_profile",
        )
    else:
        profile_name = _string(
            configured_profile,
            "route.finalization_defaults.watchdog_profile",
        )
    profile = _object(profiles.get(profile_name), f"route.llm_profiles.{profile_name}")
    harness = _string(
        profile.get("harness"), f"route.llm_profiles.{profile_name}.harness"
    )
    if harness not in recruiter.KNOWN_HARNESSES:
        raise PlanStartError(
            f"route.llm_profiles.{profile_name}.harness must be one of "
            + ", ".join(recruiter.KNOWN_HARNESSES)
        )
    return {
        "effort": _string(
            profile.get("effort", "low"),
            f"route.llm_profiles.{profile_name}.effort",
        ),
        "harness": harness,
        "model": _string(
            profile.get("model"), f"route.llm_profiles.{profile_name}.model"
        ),
        "name": profile_name,
    }


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )[:80]


def _create_tui(
    *, repo: Path, plan_path: Path, route_path: Path, slug: str, harness: str
) -> dict[str, object]:
    if harness not in TUI_LAUNCHES:
        raise PlanStartError(
            f"unknown TUI harness {harness!r}; expected one of "
            + ", ".join(TUI_LAUNCHES)
        )
    launch, expected_agent, expected_process = TUI_LAUNCHES[harness]
    response = recruiter._herdr_json(
        "workspace", "create", "--cwd", str(repo), "--label", slug, "--no-focus"
    )
    result = response.get("result", {})
    root_pane = result.get("root_pane", {}) if isinstance(result, dict) else {}
    pane_id = root_pane.get("pane_id") if isinstance(root_pane, dict) else None
    workspace_id = (
        root_pane.get("workspace_id") if isinstance(root_pane, dict) else None
    )
    command = f"cd {shlex.quote(str(repo))} && {launch} " + shlex.quote(
        f"/herdr-run --plan {plan_path} --route {route_path} "
        f"--run-tree {plan_path.parent} --slug {slug}"
    )
    try:
        if not isinstance(pane_id, str) or not pane_id:
            raise PlanStartError("herdr workspace create returned no root pane_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            pane = (
                recruiter._herdr_json("pane", "get", pane_id)
                .get("result", {})
                .get("pane", {})
            )
            workspace_id = pane.get("workspace_id") if isinstance(pane, dict) else None
        if not isinstance(workspace_id, str) or not workspace_id:
            raise PlanStartError(f"TUI pane {pane_id} has no workspace_id")
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
            if isinstance(workspace_id, str) and workspace_id:
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
        "health": health,
        "pane_id": pane_id,
        "workspace_id": workspace_id,
    }


def _request_watchdog(order_path: Path, roster_path: str) -> dict[str, object]:
    command = [
        sys.executable,
        str(UPAGENT_DIR / "recruiter.py"),
        "--roster",
        roster_path,
        "request",
        str(order_path),
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=WATCHDOG_REQUEST_TIMEOUT_SECONDS,
    )
    if process.returncode != 0:
        detail = (
            process.stderr.strip() or process.stdout.strip() or str(process.returncode)
        )
        raise PlanStartError(f"plan watchdog startup failed: {detail}")
    marker = next(
        (
            line
            for line in process.stdout.splitlines()
            if line.startswith("REQUEST_ACCEPTED ")
        ),
        None,
    )
    if marker is None:
        raise PlanStartError(
            f"plan watchdog did not reach healthy startup: {process.stdout.strip()}"
        )
    try:
        response = json.loads(marker.removeprefix("REQUEST_ACCEPTED "))
    except json.JSONDecodeError as error:
        raise PlanStartError(
            f"plan watchdog returned malformed startup evidence: {error}"
        ) from error
    if not isinstance(response, dict) or response.get("state") != "running":
        raise PlanStartError("plan watchdog startup response is not a running object")
    return cast(dict[str, object], response)


def _notify_tui(pane_id: str, message: str) -> str | None:
    try:
        recruiter._submit_agent_prompt(pane_id, message, idle_timeout_ms=5_000)
    except (OSError, recruiter.RecruiterError) as error:
        return str(error)
    return None


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
        raise PlanStartError(
            f"plan-start receipt does not belong to plan {slug!r}"
        )
    if receipt.get("state") not in ("ready", "ready-degraded"):
        raise PlanStartError(
            "plan-start receipt is not in a finishable ready state"
        )
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
            raise PlanStartError(f"existing plan terminal marker is invalid: {error}") from error
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


def start_plan(
    *,
    run_dir: Path,
    slug: str,
    tui_harness: str,
    repo: Path,
    roster_path: str,
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
        )


def _start_plan_locked(
    *,
    run_dir: Path,
    slug: str,
    tui_harness: str,
    repo: Path,
    roster_path: str,
) -> dict[str, object]:
    plan_path = run_dir / "plan.md"
    route_path = run_dir / "route.yaml"
    if not plan_path.is_file():
        raise PlanStartError(f"plan not found: {plan_path}")
    if not route_path.is_file():
        raise PlanStartError(f"route not found: {route_path}")

    control_dir = run_dir / "control"
    watchdog_dir = run_dir / "plan-watchdog"
    receipt_path = control_dir / "plan-start.json"
    order_path = watchdog_dir / "order.json"
    instructions_path = watchdog_dir / "instructions.md"
    result_path = watchdog_dir / "result.json"
    terminal_path = control_dir / "run-terminal.json"
    if receipt_path.exists() or order_path.exists() or result_path.exists():
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

    tui_pane = cast(str, tui["pane_id"])
    workspace_id = cast(str, tui["workspace_id"])
    request_id = f"{_safe_name(slug)}.plan-watchdog.{workspace_id}"
    order_id = f"{_safe_name(slug)}-plan-watchdog-{workspace_id}"
    watchdog: dict[str, object] | None = None
    try:
        profile = _load_watchdog_profile(route_path)
        _write_text_atomic(
            instructions_path,
            "# Plan lifecycle watchdog assignment\n\n"
            f"Watch the Herdr plan run `{slug}` on behalf of TUI pane `{tui_pane}`.\n\n"
            f"- Cockpit workspace: `{workspace_id}`\n"
            f"- Durable run directory: `{run_dir}`\n"
            f"- Plan-start receipt: `{receipt_path}`\n"
            f"- Authoritative plan terminal marker: `{terminal_path}`\n"
            f"- Active leader map: `{run_dir / 'active-leader-panes.json'}`\n"
            f"- Phase records: `{run_dir / 'phases'}`\n\n"
            "First resolve your own current Herdr address and send PLAN_WATCHDOG_READY to the TUI. "
            "Then monitor the TUI and every phase leader in this workspace. Prefer phase-start receipts "
            "and active-leader-panes.json; also inspect newly appearing cockpit panes so a manually "
            "launched, unmanaged leader is not invisible. When a leader appears, send it one short "
            "introduction and tell the TUI which leader you observed. When phase-result.json becomes "
            "terminal, an UpAgent requester outbox has an unacknowledged lifecycle message, or a "
            "TUI/leader is idle, done, missing, or contradicted by durable state, send a "
            "concise evidence-based advisory to the TUI and affected leader. Wait until the target agent is "
            "idle, resolve its current pane with `herdr agent get`, and submit the advisory atomically with "
            "`herdr pane run`; never paste unsubmitted text with `herdr agent send`, and never send a shell "
            "command. Alert only on state transitions so you do not spam. Never "
            "create, interrupt, close, or advance an agent. Pane silence and agent status are evidence, not "
            "a verdict. Use bounded waits. The only completion authority is the exact plan terminal marker "
            "listed above. Do not write your result merely because panes are quiet, a turn is done, or you "
            "believe your current check is complete. Continue monitoring until that marker exists and matches "
            f"plan id `{slug}`.\n",
        )
        order: dict[str, object] = {
            "agent": "plan-lifecycle-watchdog",
            "cockpit_pane": tui_pane,
            "cwd": str(repo),
            "effort": profile["effort"],
            "harness": profile["harness"],
            "instructions_path": str(instructions_path),
            "manager_placement": {
                "anchor_pane": tui_pane,
                "mode": "requester",
            },
            "mode": "direct",
            "model": profile["model"],
            "order_id": order_id,
            "phase_id": "plan",
            "plan_id": slug,
            "request_id": request_id,
            "requester": {
                "address": tui_pane,
                "id": f"tui:{tui_pane}",
                "kind": "herdr-agent",
            },
            "result_path": str(result_path),
            "stage_id": "stage-5-finalization",
            "step_id": "plan-watchdog",
            "timeout_ms": PLAN_WATCHDOG_TIMEOUT_MS,
            "watchdog_terminal": {
                "identity": slug,
                "kind": "plan",
                "path": str(terminal_path),
            },
        }
        _write_json_atomic(order_path, order)
        recruiter.load_order(order_path)
        watchdog = _request_watchdog(order_path, roster_path)
        if (
            watchdog.get("manager_workspace_id") != workspace_id
            or watchdog.get("worker_workspace_id") != workspace_id
        ):
            raise PlanStartError(
                "plan watchdog manager/worker did not start in the TUI workspace"
            )
    except (
        OSError,
        PlanStartError,
        recruiter.ContractError,
        recruiter.RecruiterError,
        subprocess.SubprocessError,
    ) as error:
        warning = str(error)
        watchdog_running = watchdog is not None
        notice = (
            "PLAN_WATCHDOG_DEGRADED: "
            if watchdog_running
            else "PLAN_WATCHDOG_UNAVAILABLE: "
        )
        notification_error = _notify_tui(
            tui_pane,
            notice + warning + ". Continue the run; do not wait for monitoring.",
        )
        watchdog_receipt: dict[str, object] = {
            "order_path": str(order_path),
            "state": "ready-misplaced" if watchdog_running else "unavailable",
        }
        if watchdog_running:
            watchdog_receipt.update(
                {
                    "manager_address": watchdog.get("manager_address"),
                    "manager_pane": watchdog.get("manager_pane"),
                    "manager_workspace_id": watchdog.get("manager_workspace_id"),
                    "request_id": watchdog.get("request_id"),
                    "worker_address": watchdog.get("worker_address"),
                    "worker_pane": watchdog.get("worker_pane"),
                    "worker_workspace_id": watchdog.get("worker_workspace_id"),
                }
            )
        receipt: dict[str, object] = {
            "at_ns": time.time_ns(),
            "reason": warning,
            "run_dir": str(run_dir),
            "slug": slug,
            "state": "ready-degraded",
            "tui": tui,
            "watchdog": watchdog_receipt,
        }
        if notification_error is not None:
            receipt["notification_error"] = notification_error
    else:
        receipt = {
            "at_ns": time.time_ns(),
            "run_dir": str(run_dir),
            "slug": slug,
            "state": "ready",
            "tui": tui,
            "watchdog": {
                "manager_address": watchdog.get("manager_address"),
                "manager_pane": watchdog.get("manager_pane"),
                "order_path": str(order_path),
                "request_id": watchdog.get("request_id"),
                "state": "ready",
                "worker_address": watchdog.get("worker_address"),
                "worker_pane": watchdog.get("worker_pane"),
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
    args = parser.parse_args(argv)
    run_dir = args.run_dir.expanduser().resolve()
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
            repo=args.repo.expanduser().resolve(),
            roster_path=args.roster,
        )
    except (OSError, PlanStartError, recruiter.RecruiterError) as error:
        sys.exit(f"herdr-plan-start: {error}")
    print(f"PLAN_STARTED {json.dumps(result, sort_keys=True)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
