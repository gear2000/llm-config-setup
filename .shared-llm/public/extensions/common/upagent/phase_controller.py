#!/usr/bin/env python3
"""Deterministic phase startup for the Herdr meta-runner.

The TUI supplies durable run inputs; this controller owns the mechanical transition from
"no phase" to "healthy leader with healthy-or-degraded watchdog evidence". The phase leader is
held behind a filesystem gate while the watchdog's ordinary UpAgent request is attempted. A
watchdog failure is recorded but does not block the leader. A terminal startup receipt is
published only after the leader process itself is healthy.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Iterator, cast
import uuid

import yaml


HERE = Path(__file__).resolve().parent
_recruiter_spec = importlib.util.spec_from_file_location(
    "upagent_phase_recruiter", HERE / "recruiter.py"
)
if _recruiter_spec is None or _recruiter_spec.loader is None:
    raise RuntimeError("could not load UpAgent Recruiter")
recruiter = cast(Any, importlib.util.module_from_spec(_recruiter_spec))
_recruiter_spec.loader.exec_module(recruiter)

PHASE_WATCHDOG_TIMEOUT_MS = 10 * 60 * 60 * 1000
STARTUP_TIMEOUT_MS = 45_000
PHASE_LEADER_TEMPLATE_FIELDS = (
    "agent",
    "cwd",
    "effort",
    "instructions_path",
    "model",
    "phase_id",
)


class PhaseStartError(RuntimeError):
    """A fail-loud phase-start contract or transaction fault."""


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


def _write_text_atomic(path: Path, value: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    if executable:
        temporary.chmod(0o700)
    os.replace(temporary, path)


def _create_leader_gate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkfifo(path, 0o600)
    except FileExistsError as error:
        raise PhaseStartError(f"phase leader gate already exists: {path}") from error


def _release_leader_gate(path: Path, request_id: str) -> None:
    """Release an already-waiting leader without a polling loop or an unbounded FIFO open."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as error:
        if error.errno == errno.ENXIO:
            raise PhaseStartError(
                f"phase leader stopped waiting on gate {path}"
            ) from error
        raise
    try:
        os.write(descriptor, f"{request_id}\n".encode())
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_phase_start(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PhaseStartError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PhaseStartError(f"{field} must be a non-empty string")
    return value


def _load_route(route_path: Path, phase_id: str) -> dict[str, Any]:
    if not route_path.is_absolute() or not route_path.is_file():
        raise PhaseStartError(f"route must be an existing absolute file: {route_path}")
    try:
        route = yaml.safe_load(route_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise PhaseStartError(
            f"route {route_path} is unreadable or invalid YAML: {error}"
        ) from error
    route = _object(route, "route")
    profiles = _object(route.get("llm_profiles"), "route.llm_profiles")
    phases = _object(route.get("phases"), "route.phases")
    phase = _object(phases.get(phase_id), f"route.phases.{phase_id}")
    lead = _object(phase.get("lead"), f"route.phases.{phase_id}.lead")
    lead_profile_name = _string(
        lead.get("llm_profile"), f"route.phases.{phase_id}.lead.llm_profile"
    )
    lead_profile = _object(
        profiles.get(lead_profile_name), f"route.llm_profiles.{lead_profile_name}"
    )
    defaults = _object(
        route.get("finalization_defaults"), "route.finalization_defaults"
    )
    watchdog_profile_name = defaults.get("watchdog_profile", lead_profile_name)
    watchdog_profile_name = _string(
        watchdog_profile_name, "route.finalization_defaults.watchdog_profile"
    )
    watchdog_profile = _object(
        profiles.get(watchdog_profile_name),
        f"route.llm_profiles.{watchdog_profile_name}",
    )
    return {
        "lead": {
            "agent": _string(lead.get("agent"), f"route.phases.{phase_id}.lead.agent"),
            "profile": _validated_profile(lead_profile, lead_profile_name),
            "profile_name": lead_profile_name,
        },
        "watchdog": {
            "profile": _validated_profile(watchdog_profile, watchdog_profile_name),
            "profile_name": watchdog_profile_name,
        },
    }


def _validated_profile(profile: dict[str, Any], name: str) -> dict[str, str]:
    harness = _string(profile.get("harness"), f"route.llm_profiles.{name}.harness")
    if harness not in recruiter.KNOWN_HARNESSES:
        raise PhaseStartError(
            f"route.llm_profiles.{name}.harness must be one of {', '.join(recruiter.KNOWN_HARNESSES)}"
        )
    return {
        "effort": _string(
            profile.get("effort", "medium"), f"route.llm_profiles.{name}.effort"
        ),
        "harness": harness,
        "model": _string(profile.get("model"), f"route.llm_profiles.{name}.model"),
    }


def _resolve_leader_launch(
    roster: dict[str, Any],
    phase_id: str,
    lead: dict[str, Any],
    cwd: Path,
    instructions: Path,
) -> str:
    profile = cast(dict[str, str], lead["profile"])
    harness = profile["harness"]
    templates = roster.get("phase_leaders")
    if not isinstance(templates, dict) or harness not in templates:
        raise PhaseStartError(
            f"roster needs a phase_leaders.{harness} launch template; "
            "phase startup will not guess an interactive/controller command"
        )
    template = templates[harness]
    if not isinstance(template, str) or not template.strip():
        raise PhaseStartError(
            f"roster phase_leaders.{harness} must be a non-empty template"
        )
    fields = {
        "agent": lead["agent"],
        "cwd": str(cwd),
        "effort": profile["effort"],
        "instructions_path": str(instructions),
        "model": profile["model"],
        "phase_id": phase_id,
    }
    try:
        launch = template.format(**fields)
    except KeyError as error:
        raise PhaseStartError(
            f"phase_leaders.{harness} references unknown placeholder {error}; allowed: "
            + ", ".join(f"{{{field}}}" for field in PHASE_LEADER_TEMPLATE_FIELDS)
        ) from error
    try:
        words = shlex.split(launch)
    except ValueError as error:
        raise PhaseStartError(
            f"phase leader launch is not valid shell syntax: {error}"
        ) from error
    if not words:
        raise PhaseStartError("phase leader launch resolved to an empty command")
    if shutil.which(words[0]) is None:
        raise PhaseStartError(f"phase leader executable is not on PATH: {words[0]}")
    return launch


def _live_panes() -> set[str]:
    response = recruiter._herdr_json("pane", "list")
    panes = response.get("result", {}).get("panes", [])
    return {
        pane["pane_id"]
        for pane in panes
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
    }


def _active_leaders(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PhaseStartError(
            f"active leader map {path} is unreadable: {error}"
        ) from error
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise PhaseStartError(
            f"active leader map {path} must contain only phase-id to pane-id strings"
        )
    return value


def _set_active_leader(path: Path, phase_id: str, pane_id: str | None) -> None:
    with _exclusive_phase_start(path.with_name(".active-leader-panes.lock")):
        leaders = _active_leaders(path)
        if pane_id is None:
            leaders.pop(phase_id, None)
        else:
            leaders[phase_id] = pane_id
        _write_json_atomic(path, cast(dict[str, object], leaders))


def _start_gated_leader(
    name: str, tui_pane: str, cwd: Path, script_path: Path
) -> tuple[str, str]:
    tui = (
        recruiter._herdr_json("pane", "get", tui_pane).get("result", {}).get("pane", {})
    )
    tab_id = tui.get("tab_id") if isinstance(tui, dict) else None
    workspace_id = tui.get("workspace_id") if isinstance(tui, dict) else None
    if not isinstance(tab_id, str) or not tab_id:
        raise PhaseStartError(f"TUI pane {tui_pane} has no tab_id")
    response = recruiter._herdr_json(
        "agent",
        "start",
        name,
        "--cwd",
        str(cwd),
        "--tab",
        tab_id,
        "--split",
        "down",
        "--no-focus",
        "--",
        "bash",
        str(script_path),
    )
    agent = response.get("result", {}).get("agent", {})
    pane_id = agent.get("pane_id") if isinstance(agent, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        raise PhaseStartError("herdr agent start returned no phase leader pane_id")
    returned_workspace = agent.get("workspace_id") if isinstance(agent, dict) else None
    if (
        isinstance(workspace_id, str)
        and isinstance(returned_workspace, str)
        and returned_workspace != workspace_id
    ):
        raise PhaseStartError(
            f"phase leader started in workspace {returned_workspace}, expected {workspace_id}"
        )
    return pane_id, returned_workspace if isinstance(returned_workspace, str) else ""


def _verify_gated_leader(pane_id: str, script_path: Path) -> None:
    process_info = (
        recruiter._herdr_json("pane", "process-info", "--pane", pane_id)
        .get("result", {})
        .get("process_info", {})
    )
    processes = (
        process_info.get("foreground_processes", [])
        if isinstance(process_info, dict)
        else []
    )
    if not any(
        str(script_path) in str(process.get("cmdline", ""))
        for process in processes
        if isinstance(process, dict)
    ):
        raise PhaseStartError(
            f"phase leader pane {pane_id} is not waiting on its startup gate"
        )


def _request_watchdog(order_path: Path, roster_path: str) -> dict[str, object]:
    command = [
        sys.executable,
        str(HERE / "recruiter.py"),
        "--roster",
        roster_path,
        "request",
        str(order_path),
    ]
    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode != 0:
        detail = (
            process.stderr.strip()
            or process.stdout.strip()
            or f"exit {process.returncode}"
        )
        raise PhaseStartError(f"phase watchdog startup failed: {detail}")
    marker = next(
        (
            line
            for line in process.stdout.splitlines()
            if line.startswith("REQUEST_ACCEPTED ")
        ),
        None,
    )
    if marker is None:
        raise PhaseStartError(
            f"phase watchdog did not reach healthy startup: {process.stdout.strip()}"
        )
    try:
        response = json.loads(marker.removeprefix("REQUEST_ACCEPTED "))
    except json.JSONDecodeError as error:
        raise PhaseStartError(
            f"phase watchdog returned malformed startup evidence: {error}"
        ) from error
    if not isinstance(response, dict) or response.get("state") != "running":
        raise PhaseStartError("phase watchdog startup response is not a running object")
    for field in (
        "manager_pane",
        "manager_workspace_id",
        "worker_pane",
        "worker_workspace_id",
        "request_id",
    ):
        if not isinstance(response.get(field), str) or not response[field]:
            raise PhaseStartError(f"phase watchdog startup response has no {field}")
    return response


def _leader_health(
    pane_id: str, cwd: Path, profile: dict[str, str], roster: dict[str, Any]
) -> dict[str, object]:
    health = roster.get("health", {}).get(profile["harness"], {})
    return recruiter._wait_for_agent_health(
        pane_id,
        expected_agent=health.get(
            "expected_agent", recruiter.EXPECTED_HARNESS_AGENT[profile["harness"]]
        ),
        expected_process=health.get(
            "expected_process", recruiter.EXPECTED_HARNESS_PROCESS[profile["harness"]]
        ),
        expected_cwd=str(cwd),
        timeout_ms=STARTUP_TIMEOUT_MS,
    )


def _safe_name(slug: str, phase_id: str, pass_number: int) -> str:
    raw = f"phase-leader-{slug}-{phase_id}-p{pass_number}"
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in raw
    )[:80]


def start_phase(
    *,
    route_path: Path,
    run_root: Path,
    phase_id: str,
    pass_number: int,
    tui_pane: str,
    cwd: Path,
    roster_path: str,
) -> dict[str, object]:
    """Start one leader/watchdog pair and return only after both are mechanically healthy."""
    if os.environ.get("HERDR_ENV") != "1":
        raise PhaseStartError(
            "phase startup must run inside a Herdr-managed pane (HERDR_ENV=1)"
        )
    current_pane = os.environ.get("HERDR_PANE_ID")
    if current_pane is not None and current_pane != tui_pane:
        raise PhaseStartError(
            f"owning TUI pane {tui_pane} does not match current Herdr pane {current_pane}"
        )
    if pass_number <= 0:
        raise PhaseStartError("pass must be a positive integer")
    if not run_root.is_absolute() or not run_root.is_dir():
        raise PhaseStartError(
            f"run_root must be an existing absolute directory: {run_root}"
        )
    if not cwd.is_absolute() or not cwd.is_dir():
        raise PhaseStartError(f"cwd must be an existing absolute directory: {cwd}")
    plan_path = run_root / "plan.md"
    if not plan_path.is_file():
        raise PhaseStartError(f"frozen run plan does not exist: {plan_path}")
    route = _load_route(route_path, phase_id)
    roster = recruiter.load_roster(roster_path)
    slug = run_root.name
    control_dir = run_root / "phases" / phase_id / f"pass-{pass_number}" / "control"
    watchdog_dir = control_dir.parent / "watchdog"
    receipt_path = control_dir / "phase-start.json"
    gate_path = control_dir / "watchdog-ready.fifo"
    script_path = control_dir / "launch-leader.sh"
    leader_instructions = control_dir / "leader-instructions.md"
    watchdog_instructions = watchdog_dir / "instructions.md"
    watchdog_order_path = watchdog_dir / "order.json"
    watchdog_result_path = watchdog_dir / "result.json"
    active_path = run_root / "active-leader-panes.json"
    lock_path = run_root / "phases" / phase_id / ".phase-start.lock"
    request_id = f"{slug}.{phase_id}.pass-{pass_number}.phase-watchdog"
    order_id = f"{slug}-{phase_id}-pass-{pass_number}-phase-watchdog"

    with _exclusive_phase_start(lock_path):
        if receipt_path.is_file():
            try:
                existing = json.loads(receipt_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise PhaseStartError(
                    f"phase-start receipt {receipt_path} is unreadable: {error}"
                ) from error
            if isinstance(existing, dict) and existing.get("state") == "ready":
                leader_pane = existing.get("leader_pane")
                watchdog = existing.get("watchdog")
                watchdog_pane = (
                    watchdog.get("worker_pane") if isinstance(watchdog, dict) else None
                )
                live = _live_panes()
                if (
                    isinstance(leader_pane, str)
                    and leader_pane in live
                    and isinstance(watchdog_pane, str)
                    and watchdog_pane in live
                ):
                    return cast(dict[str, object], existing)
                raise PhaseStartError(
                    "phase-start receipt says ready but its leader/watchdog pair is no longer fully live"
                )
            raise PhaseStartError(
                f"phase start already has non-ready state at {receipt_path}"
            )

        leaders = _active_leaders(active_path)
        prior = leaders.get(phase_id)
        if prior is not None and prior in _live_panes():
            raise PhaseStartError(
                f"phase {phase_id} already has live leader {prior}; only its owning TUI may end that lifecycle"
            )
        if prior is not None:
            _set_active_leader(active_path, phase_id, None)
        if (
            watchdog_order_path.exists()
            or watchdog_result_path.exists()
            or gate_path.exists()
        ):
            raise PhaseStartError(
                f"phase-start artifacts already exist under {control_dir.parent}"
            )

        leader_command = (
            f"/herdr-phase --phase {shlex.quote(phase_id)} --plan {shlex.quote(str(plan_path))} "
            f"--route {shlex.quote(str(route_path))} --run-root {shlex.quote(str(run_root))}"
        )
        lead = cast(dict[str, Any], route["lead"])
        _write_text_atomic(
            leader_instructions,
            "# Phase leader startup\n\n"
            f"Configured agent/persona: `{lead['agent']}`. Follow its installed definition when the harness "
            "does not have a native agent-selection flag.\n\n"
            f"Run exactly this phase-controller command and own the phase until it writes its terminal result:\n\n"
            f"```text\n{leader_command}\n```\n",
        )
        lead_profile = cast(dict[str, str], lead["profile"])
        launch = _resolve_leader_launch(
            roster, phase_id, lead, cwd, leader_instructions
        )
        _write_text_atomic(
            script_path,
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f"IFS= read -r watchdog_request_id < {shlex.quote(str(gate_path))}\n"
            f"rm -f {shlex.quote(str(gate_path))}\n"
            f'[[ "$watchdog_request_id" == {shlex.quote(request_id)} ]]\n'
            f"export {recruiter.PHASE_START_RECEIPT_ENV}={shlex.quote(str(receipt_path))}\n"
            f"exec bash -lc {shlex.quote(launch)}\n",
            executable=True,
        )
        leader_pane: str | None = None
        ready = False
        try:
            _create_leader_gate(gate_path)
            _write_text_atomic(
                watchdog_instructions,
                "# Phase watchdog assignment\n\n"
                f"Watch phase `{phase_id}` pass `{pass_number}` for TUI pane `{tui_pane}`.\n\n"
                f"- Leader pane: written into `{receipt_path}` after startup validation.\n"
                f"- Phase startup receipt: `{receipt_path}`; do not diagnose the gated leader before state is `ready`.\n"
                f"- Authoritative phase result: `{run_root / 'phases' / phase_id / 'phase-result.json'}`; "
                f"accept only a result whose pass is `{pass_number}`.\n"
                f"- Run status: `{run_root / 'run-status.md'}`.\n"
                f"- Descendant orders: `{run_root / 'phases' / phase_id / f'pass-{pass_number}' / 'stages'}`.\n"
                "- Durable UpAgent state: `$UPAGENT_HUB_DIR` or its documented default.\n\n"
                "Use bounded waits and evidence snapshots. Pane silence or `done` is advisory, never a verdict. "
                "Do not close, interrupt, or advance any pane. Finish only when the matching phase result is terminal "
                "or when specific durable and visible evidence supports a blocked watchdog result.\n",
            )
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "pass": pass_number,
                    "phase_id": phase_id,
                    "state": "preparing",
                    "tui_pane": tui_pane,
                },
            )
        except (PhaseStartError, OSError):
            gate_path.unlink(missing_ok=True)
            raise
        try:
            leader_pane, workspace_id = _start_gated_leader(
                _safe_name(slug, phase_id, pass_number), tui_pane, cwd, script_path
            )
            _verify_gated_leader(leader_pane, script_path)
            _set_active_leader(active_path, phase_id, leader_pane)
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "leader_pane": leader_pane,
                    "pass": pass_number,
                    "phase_id": phase_id,
                    "state": "leader-gated",
                    "tui_pane": tui_pane,
                    "workspace_id": workspace_id,
                },
            )
            watchdog_profile = cast(dict[str, str], route["watchdog"]["profile"])
            watchdog_order: dict[str, object] = {
                "agent": "phase-watchdog",
                "cockpit_pane": leader_pane,
                "cwd": str(cwd),
                "effort": watchdog_profile["effort"],
                "harness": watchdog_profile["harness"],
                "instructions_path": str(watchdog_instructions),
                "manager_placement": {
                    "anchor_pane": leader_pane,
                    "mode": "requester",
                },
                "model": watchdog_profile["model"],
                "order_id": order_id,
                "phase_id": phase_id,
                "request_id": request_id,
                "requester": {
                    "address": tui_pane,
                    "id": f"tui:{tui_pane}",
                    "kind": "herdr-agent",
                },
                "result_path": str(watchdog_result_path),
                "stage_id": "stage-5-finalization",
                "timeout_ms": PHASE_WATCHDOG_TIMEOUT_MS,
                "watchdog_terminal": {
                    "identity": phase_id,
                    "kind": "phase",
                    "path": str(
                        run_root / "phases" / phase_id / "phase-result.json"
                    ),
                },
            }
            _write_json_atomic(watchdog_order_path, watchdog_order)
            watchdog_ready = False
            try:
                recruiter.load_order(watchdog_order_path)
                watchdog = _request_watchdog(watchdog_order_path, roster_path)
                if (
                    watchdog["manager_workspace_id"] != workspace_id
                    or watchdog["worker_workspace_id"] != workspace_id
                ):
                    raise PhaseStartError(
                        "phase watchdog manager/worker did not start in the leader workspace"
                    )
            except (
                PhaseStartError,
                recruiter.ContractError,
                recruiter.RecruiterError,
                OSError,
                subprocess.SubprocessError,
            ) as watchdog_error:
                watchdog_receipt: dict[str, object] = {
                    "order_path": str(watchdog_order_path),
                    "reason": str(watchdog_error),
                    "request_id": request_id,
                    "state": "unavailable",
                }
            else:
                watchdog_ready = True
                watchdog_receipt = {
                    "manager_address": watchdog.get("manager_address"),
                    "manager_pane": watchdog["manager_pane"],
                    "manager_workspace_id": watchdog["manager_workspace_id"],
                    "order_path": str(watchdog_order_path),
                    "request_id": watchdog["request_id"],
                    "state": "ready",
                    "worker_address": watchdog.get("worker_address"),
                    "worker_pane": watchdog["worker_pane"],
                    "worker_workspace_id": watchdog["worker_workspace_id"],
                }
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "leader_pane": leader_pane,
                    "pass": pass_number,
                    "phase_id": phase_id,
                    "state": "watchdog-ready"
                    if watchdog_ready
                    else "watchdog-degraded",
                    "tui_pane": tui_pane,
                    "watchdog": watchdog_receipt,
                    "workspace_id": workspace_id,
                },
            )
            _release_leader_gate(gate_path, request_id)
            leader_health = _leader_health(leader_pane, cwd, lead_profile, roster)
            receipt = {
                "at_ns": time.time_ns(),
                "leader_health": leader_health,
                "leader_pane": leader_pane,
                "pass": pass_number,
                "phase_id": phase_id,
                "state": "ready" if watchdog_ready else "ready-degraded",
                "tui_pane": tui_pane,
                "watchdog": watchdog_receipt,
                "workspace_id": workspace_id,
            }
            _write_json_atomic(receipt_path, receipt)
            ready = True
            return receipt
        except (
            PhaseStartError,
            recruiter.ContractError,
            recruiter.RecruiterError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "leader_pane": leader_pane,
                    "pass": pass_number,
                    "phase_id": phase_id,
                    "reason": str(error),
                    "state": "failed",
                    "tui_pane": tui_pane,
                },
            )
            raise
        finally:
            if not ready:
                if leader_pane is not None:
                    recruiter._close_worker_pane(leader_pane)
                    if _active_leaders(active_path).get(phase_id) == leader_pane:
                        _set_active_leader(active_path, phase_id, None)
                gate_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="upagent-phase-start")
    parser.add_argument("route", type=Path, help="frozen run route.yaml")
    parser.add_argument("run_root", type=Path, help="frozen run tree root")
    parser.add_argument("phase", help="phase id from route.yaml")
    parser.add_argument("pass_number", type=int, help="positive phase pass number")
    parser.add_argument(
        "--cwd", type=Path, default=Path.cwd(), help="leader/watchdog working directory"
    )
    parser.add_argument(
        "--tui-pane", default=os.environ.get("HERDR_PANE_ID"), help="owning TUI pane"
    )
    parser.add_argument(
        "--roster", default=recruiter.default_roster_path(), help="UpAgent roster"
    )
    args = parser.parse_args(argv)
    if not args.tui_pane:
        parser.error("--tui-pane is required when HERDR_PANE_ID is not set")
    try:
        result = start_phase(
            route_path=args.route.expanduser().resolve(),
            run_root=args.run_root.expanduser().resolve(),
            phase_id=args.phase,
            pass_number=args.pass_number,
            tui_pane=args.tui_pane,
            cwd=args.cwd.expanduser().resolve(),
            roster_path=args.roster,
        )
    except (OSError, PhaseStartError, recruiter.RecruiterError) as error:
        sys.exit(f"upagent-phase-start: {error}")
    print(f"PHASE_STARTED {json.dumps(result, sort_keys=True)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
