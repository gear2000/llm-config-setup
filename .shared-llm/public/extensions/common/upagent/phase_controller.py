#!/usr/bin/env python3
"""Deterministic phase startup for the Herdr meta-runner.

The TUI supplies durable run inputs; this controller owns the mechanical transition from
"no phase" to "healthy leader". The phase leader is held behind a filesystem gate until the
durable phase-start receipt records its identity, then released and health-checked.
Coordination v2 creates no standing watchdog: the owner blocks in `upagent-phase-await`
on the receipt this controller writes. A terminal startup receipt is published only after
the leader process itself is healthy.
"""

from __future__ import annotations

import errno
import fcntl
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import yaml

HERE = Path(__file__).resolve().parent
_runtime_name = "upagent_command_runtime"
if _runtime_name in sys.modules:
    command_runtime = sys.modules[_runtime_name]
else:
    _runtime_spec = importlib.util.spec_from_file_location(
        _runtime_name, HERE / "command_runtime.py"
    )
    if _runtime_spec is None or _runtime_spec.loader is None:
        raise RuntimeError("could not load UpAgent command runtime")
    command_runtime = importlib.util.module_from_spec(_runtime_spec)
    sys.modules[_runtime_name] = command_runtime
    _runtime_spec.loader.exec_module(command_runtime)

recruiter: Any = None


def _bind_recruiter_runtime(runtime: Any) -> None:
    """Accept the per-command client's canonical Recruiter module."""
    global recruiter
    if recruiter is not None and recruiter is not runtime:
        raise RuntimeError("phase controller Recruiter runtime is already bound")
    recruiter = runtime


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


def _request_cwd() -> Path:
    return command_runtime.current_cwd()


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
    if harness not in recruiter.LEGACY_CONTROLLER_HARNESSES:
        raise PhaseStartError(
            f"route.llm_profiles.{name}.harness must be one of {', '.join(recruiter.LEGACY_CONTROLLER_HARNESSES)}"
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


def _live_panes(herdr_session: str | None = None) -> set[str]:
    response = recruiter._herdr_json("pane", "list", herdr_session=herdr_session)
    panes = response.get("result", {}).get("panes", [])
    return {
        pane["pane_id"]
        for pane in panes
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
    }


def _active_leaders(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PhaseStartError(
            f"active leader map {path} is unreadable: {error}"
        ) from error
    if not isinstance(value, dict):
        raise PhaseStartError(
            f"active leader map {path} must contain phase-id leader records"
        )
    leaders: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            raise PhaseStartError(
                f"active leader map {path} has a legacy or malformed entry; inspect it "
                "and repair or remove it only after confirming the recorded pane is safe"
            )
        pane_id = item.get("pane_id")
        herdr_session = item.get("herdr_session")
        if not isinstance(pane_id, str) or not isinstance(herdr_session, str):
            raise PhaseStartError(
                f"active leader map {path} has a legacy or malformed entry; inspect it "
                "and repair or remove it only after confirming the recorded pane is safe"
            )
        leaders[key] = item
    return leaders


def _set_active_leader(
    path: Path,
    phase_id: str,
    pane_id: str | None,
    herdr_session: str | None,
    *,
    health: dict[str, object] | None = None,
    workspace_id: str | None = None,
) -> None:
    with _exclusive_phase_start(path.with_name(".active-leader-panes.lock")):
        leaders = _active_leaders(path)
        if pane_id is None:
            leaders.pop(phase_id, None)
        else:
            if herdr_session is None:
                raise PhaseStartError("active leader record requires herdr_session")
            leaders[phase_id] = {
                "health": health,
                "pane_id": pane_id,
                "herdr_session": herdr_session,
                "ownership": {"pane": {"pane_id": pane_id, "state": "created"}},
                **({"workspace_id": workspace_id} if workspace_id else {}),
            }
        _write_json_atomic(path, cast(dict[str, object], leaders))


def _start_gated_leader(
    name: str, tui_pane: str, cwd: Path, script_path: Path, herdr_session: str
) -> tuple[str, str]:
    tui = (
        recruiter._herdr_json("pane", "get", tui_pane, herdr_session=herdr_session)
        .get("result", {})
        .get("pane", {})
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
        herdr_session=herdr_session,
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


def _verify_gated_leader(pane_id: str, script_path: Path, herdr_session: str) -> None:
    process_info = (
        recruiter._herdr_json(
            "pane", "process-info", "--pane", pane_id, herdr_session=herdr_session
        )
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


def _leader_health(
    pane_id: str,
    cwd: Path,
    profile: dict[str, str],
    roster: dict[str, Any],
    herdr_session: str,
) -> dict[str, object]:
    health = roster.get("health", {}).get(profile["harness"], {})
    result = recruiter._wait_for_agent_health(
        pane_id,
        expected_agent=health.get(
            "expected_agent", recruiter.EXPECTED_HARNESS_AGENT[profile["harness"]]
        ),
        expected_process=health.get(
            "expected_process", recruiter.EXPECTED_HARNESS_PROCESS[profile["harness"]]
        ),
        expected_cwd=str(cwd),
        timeout_ms=STARTUP_TIMEOUT_MS,
        herdr_session=herdr_session,
    )
    process_pid = result.get("process_pid")
    if isinstance(process_pid, int):
        result["process_start_time"] = recruiter._process_start_time(process_pid)
    return result


def _leader_expected_health(
    cwd: Path, profile: dict[str, str], roster: dict[str, Any], pane_id: str
) -> dict[str, object]:
    health = roster.get("health", {}).get(profile["harness"], {})
    return {
        "cwd": str(cwd),
        "expected_agent": health.get(
            "expected_agent", recruiter.EXPECTED_HARNESS_AGENT[profile["harness"]]
        ),
        "expected_process": health.get(
            "expected_process", recruiter.EXPECTED_HARNESS_PROCESS[profile["harness"]]
        ),
        "healthy": False,
        "pane_id": pane_id,
    }


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
    """Start one verified leader. Coordination v2 creates no standing phase watchdog."""
    if command_runtime.getenv("HERDR_ENV") != "1":
        raise PhaseStartError(
            "phase startup must run inside a Herdr-managed pane (HERDR_ENV=1)"
        )
    current_pane = command_runtime.getenv("HERDR_PANE_ID")
    if current_pane is not None and current_pane != tui_pane:
        raise PhaseStartError(
            f"owning TUI pane {tui_pane} does not match current Herdr pane {current_pane}"
        )
    herdr_session = recruiter._resolve_current_herdr_session_name()
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
    receipt_path = control_dir / "phase-start.json"
    # Historical filename kept for launch-template compatibility; it gates on the receipt.
    gate_path = control_dir / "watchdog-ready.fifo"
    script_path = control_dir / "launch-leader.sh"
    leader_instructions = control_dir / "leader-instructions.md"
    active_path = run_root / "active-leader-panes.json"
    lock_path = run_root / "phases" / phase_id / ".phase-start.lock"
    release_token = f"{slug}.{phase_id}.pass-{pass_number}.phase-start"

    with _exclusive_phase_start(lock_path):
        if receipt_path.is_file():
            try:
                existing = json.loads(receipt_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise PhaseStartError(
                    f"phase-start receipt {receipt_path} is unreadable: {error}"
                ) from error
            if isinstance(existing, dict) and existing.get("state") == "ready":
                if existing.get("herdr_session") != herdr_session:
                    raise PhaseStartError(
                        "phase-start receipt belongs to a different Herdr session"
                    )
                existing_leader_pane = existing.get("leader_pane")
                if isinstance(
                    existing_leader_pane, str
                ) and existing_leader_pane in _live_panes(herdr_session):
                    return cast(dict[str, object], existing)
                raise PhaseStartError(
                    "phase-start receipt says ready but its leader is no longer live"
                )
            raise PhaseStartError(
                f"phase start already has non-ready state at {receipt_path}"
            )

        leaders = _active_leaders(active_path)
        prior = leaders.get(phase_id)
        prior_pane = prior.get("pane_id") if isinstance(prior, dict) else None
        prior_session = prior.get("herdr_session") if isinstance(prior, dict) else None
        if prior is not None and prior_session != herdr_session:
            raise PhaseStartError(
                f"phase {phase_id} has an active leader in a different Herdr session"
            )
        if (
            isinstance(prior_pane, str)
            and prior_pane
            and prior_pane in _live_panes(herdr_session)
        ):
            raise PhaseStartError(
                f"phase {phase_id} already has live leader {prior_pane}; only its owning TUI may end that lifecycle"
            )
        if prior_pane is not None:
            _set_active_leader(active_path, phase_id, None, None)
        if gate_path.exists():
            raise PhaseStartError(
                f"phase-start artifacts already exist under {control_dir.parent}"
            )

        leader_command = (
            f"/phase-leader --phase {shlex.quote(phase_id)} --plan {shlex.quote(str(plan_path))} "
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
        # `bash -lc` sources the user's login profile (PATH setup for nvm/pyenv/asdf-style
        # tool managers), but a profile that also `cd`s (common in shared dev environments)
        # would silently override the pane's cwd. Re-assert cwd as the last word inside the
        # login shell's own command so the leader always lands where it was told to.
        launch_in_cwd = f"cd {shlex.quote(str(cwd))} && {launch}"
        _write_text_atomic(
            script_path,
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f"IFS= read -r phase_release_token < {shlex.quote(str(gate_path))}\n"
            f"rm -f {shlex.quote(str(gate_path))}\n"
            f'[[ "$phase_release_token" == {shlex.quote(release_token)} ]]\n'
            f"export {recruiter.PHASE_START_RECEIPT_ENV}={shlex.quote(str(receipt_path))}\n"
            f"exec bash -lc {shlex.quote(launch_in_cwd)}\n",
            executable=True,
        )
        leader_pane: str | None = None
        ready = False
        try:
            _create_leader_gate(gate_path)
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "pass": pass_number,
                    "phase_id": phase_id,
                    "herdr_session": herdr_session,
                    "state": "preparing",
                    "tui_pane": tui_pane,
                },
            )
            leader_pane, workspace_id = _start_gated_leader(
                _safe_name(slug, phase_id, pass_number),
                tui_pane,
                cwd,
                script_path,
                herdr_session,
            )
            _verify_gated_leader(leader_pane, script_path, herdr_session)
            expected_health = _leader_expected_health(
                cwd, lead_profile, roster, leader_pane
            )
            _set_active_leader(
                active_path,
                phase_id,
                leader_pane,
                herdr_session,
                health=expected_health,
                workspace_id=workspace_id,
            )
            watchdog_receipt: dict[str, object] = {
                "reason": "coordination v2: phase-await owns delivery and reconciliation; no standing watchdog is created",
                "state": "not-configured",
            }
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "health": expected_health,
                    "leader_pane": leader_pane,
                    "herdr_session": herdr_session,
                    "ownership": {
                        "leader": {"pane_id": leader_pane, "state": "created"},
                        "pane": {"pane_id": leader_pane, "state": "created"},
                    },
                    "pass": pass_number,
                    "phase_id": phase_id,
                    "state": "leader-gated",
                    "tui_pane": tui_pane,
                    "watchdog": watchdog_receipt,
                    "workspace_id": workspace_id,
                },
            )
            _release_leader_gate(gate_path, release_token)
            leader_health = _leader_health(
                leader_pane, cwd, lead_profile, roster, herdr_session
            )
            _set_active_leader(
                active_path,
                phase_id,
                leader_pane,
                herdr_session,
                health=leader_health,
                workspace_id=workspace_id,
            )
            receipt = {
                "at_ns": time.time_ns(),
                "health": leader_health,
                "leader_health": leader_health,
                "leader_pane": leader_pane,
                "herdr_session": herdr_session,
                "ownership": {
                    "leader": {"pane_id": leader_pane, "state": "created"},
                    "pane": {"pane_id": leader_pane, "state": "created"},
                },
                "pass": pass_number,
                "phase_id": phase_id,
                "state": "ready",
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
                    "herdr_session": herdr_session,
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
                gate_path.unlink(missing_ok=True)
                if leader_pane is not None:
                    try:
                        recruiter._close_worker_pane(
                            leader_pane, herdr_session=herdr_session
                        )
                    except (
                        recruiter.RecruiterError,
                        OSError,
                        subprocess.SubprocessError,
                    ) as close_error:
                        # Never let cleanup mask the startup error that got us here.
                        command_runtime.write_stderr(
                            f"phase-start cleanup: could not close gated leader {leader_pane}: {close_error}\n"
                        )
                    active = _active_leaders(active_path).get(phase_id)
                    if (
                        isinstance(active, dict)
                        and active.get("pane_id") == leader_pane
                        and active.get("herdr_session") == herdr_session
                    ):
                        _set_active_leader(active_path, phase_id, None, None)


def main(argv: list[str] | None = None) -> int:
    parser = command_runtime.ArgumentParser(prog="upagent-phase-start")
    parser.add_argument("route", type=Path, help="frozen run route.yaml")
    parser.add_argument("run_root", type=Path, help="frozen run tree root")
    parser.add_argument("phase", help="phase id from route.yaml")
    parser.add_argument("pass_number", type=int, help="positive phase pass number")
    parser.add_argument(
        "--cwd",
        type=Path,
        default=_request_cwd(),
        help="leader/watchdog working directory",
    )
    parser.add_argument(
        "--tui-pane",
        default=command_runtime.getenv("HERDR_PANE_ID"),
        help="owning TUI pane",
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
