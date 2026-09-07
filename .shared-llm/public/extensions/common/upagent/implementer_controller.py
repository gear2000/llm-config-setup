#!/usr/bin/env python3
"""Deterministic plan-implementer startup for Flow 1 (HIL proxy).

The human-started HIL pane supplies an approved plan.md plus an offering. This
controller owns the mechanical transition from "no implementer" to "healthy
controller pane". The implementer waits behind a filesystem gate until the
durable implementer-start receipt records its identity, then is released and
health-checked. The HIL blocks in upagent-implementer-await on that receipt.
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

IMPLEMENTER_START_RECEIPT_ENV = "UPAGENT_IMPLEMENTER_START_RECEIPT"
IMPLEMENTER_PHASE_ID = "plan"
STARTUP_TIMEOUT_MS = 45_000
IMPLEMENTER_TEMPLATE_FIELDS = (
    "agent",
    "cwd",
    "effort",
    "instructions_path",
    "model",
)
IMPLEMENTER_AGENT = "plan-implementer"


class ImplementerStartError(RuntimeError):
    """A fail-loud implementer-start contract or transaction fault."""


def _bind_recruiter_runtime(runtime: Any) -> None:
    global recruiter
    if recruiter is not None and recruiter is not runtime:
        raise RuntimeError("implementer controller Recruiter runtime is already bound")
    recruiter = runtime


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


def _create_gate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkfifo(path, 0o600)
    except FileExistsError as error:
        raise ImplementerStartError(f"implementer gate already exists: {path}") from error


def _release_gate(path: Path, request_id: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as error:
        if error.errno == errno.ENXIO:
            raise ImplementerStartError(
                f"plan-implementer stopped waiting on gate {path}"
            ) from error
        raise
    try:
        os.write(descriptor, f"{request_id}\n".encode())
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _resolve_offering(offering_id: str, effort: str, cwd: Path) -> dict[str, str]:
    catalog = recruiter.offering_catalog
    roster = catalog.load_roster(catalog.resolve_roster_path(cwd))
    snapshot = roster.resolve(offering_id, effort)
    harness = snapshot["harness"]
    model = snapshot["model"]
    selected = snapshot["selected_effort"]
    if not isinstance(harness, str) or not isinstance(model, str):
        raise ImplementerStartError(
            f"offering {offering_id!r} snapshot is missing harness/model"
        )
    if not isinstance(selected, str):
        raise ImplementerStartError(
            f"offering {offering_id!r} snapshot is missing selected_effort"
        )
    return {
        "effort": selected,
        "harness": harness,
        "id": offering_id,
        "model": model,
    }


def _resolve_launch(
    roster: dict[str, Any],
    profile: dict[str, str],
    cwd: Path,
    instructions: Path,
) -> str:
    harness = profile["harness"]
    templates = roster.get("plan_implementers")
    if not isinstance(templates, dict) or harness not in templates:
        raise ImplementerStartError(
            f"roster needs a plan_implementers.{harness} launch template; "
            "implementer startup will not guess an interactive/controller command"
        )
    template = templates[harness]
    if not isinstance(template, str) or not template.strip():
        raise ImplementerStartError(
            f"roster plan_implementers.{harness} must be a non-empty template"
        )
    fields = {
        "agent": IMPLEMENTER_AGENT,
        "cwd": str(cwd),
        "effort": profile["effort"],
        "instructions_path": str(instructions),
        "model": profile["model"],
    }
    try:
        launch = template.format(**fields)
    except KeyError as error:
        raise ImplementerStartError(
            f"plan_implementers.{harness} references unknown placeholder {error}; allowed: "
            + ", ".join(f"{{{field}}}" for field in IMPLEMENTER_TEMPLATE_FIELDS)
        ) from error
    try:
        words = shlex.split(launch)
    except ValueError as error:
        raise ImplementerStartError(
            f"plan-implementer launch is not valid shell syntax: {error}"
        ) from error
    if not words:
        raise ImplementerStartError("plan-implementer launch resolved to an empty command")
    if shutil.which(words[0]) is None:
        raise ImplementerStartError(
            f"plan-implementer executable is not on PATH: {words[0]}"
        )
    return launch


def _start_gated(
    name: str, hil_pane: str, cwd: Path, script_path: Path, herdr_session: str
) -> tuple[str, str]:
    hil = (
        recruiter._herdr_json("pane", "get", hil_pane, herdr_session=herdr_session)
        .get("result", {})
        .get("pane", {})
    )
    tab_id = hil.get("tab_id") if isinstance(hil, dict) else None
    workspace_id = hil.get("workspace_id") if isinstance(hil, dict) else None
    if not isinstance(tab_id, str) or not tab_id:
        raise ImplementerStartError(f"HIL pane {hil_pane} has no tab_id")
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
        raise ImplementerStartError(
            "herdr agent start returned no plan-implementer pane_id"
        )
    returned_workspace = agent.get("workspace_id") if isinstance(agent, dict) else None
    if (
        isinstance(workspace_id, str)
        and isinstance(returned_workspace, str)
        and returned_workspace != workspace_id
    ):
        raise ImplementerStartError(
            f"plan-implementer started in workspace {returned_workspace}, expected {workspace_id}"
        )
    return pane_id, returned_workspace if isinstance(returned_workspace, str) else ""


def _verify_gated(pane_id: str, script_path: Path, herdr_session: str) -> None:
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
        raise ImplementerStartError(
            f"plan-implementer pane {pane_id} is not waiting on its startup gate"
        )


def _health(
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


def _safe_name(slug: str) -> str:
    raw = f"plan-implementer-{slug}"
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in raw
    )[:80]


def start_implementer(
    *,
    plan_path: Path,
    offering_id: str,
    effort: str,
    run_root: Path,
    hil_pane: str,
    cwd: Path,
    roster_path: str,
) -> dict[str, object]:
    if command_runtime.getenv("HERDR_ENV") != "1":
        raise ImplementerStartError(
            "implementer startup must run inside a Herdr-managed pane (HERDR_ENV=1)"
        )
    current_pane = command_runtime.getenv("HERDR_PANE_ID")
    if current_pane is not None and current_pane != hil_pane:
        raise ImplementerStartError(
            f"owning HIL pane {hil_pane} does not match current Herdr pane {current_pane}"
        )
    herdr_session = recruiter._resolve_current_herdr_session_name()
    if not plan_path.is_absolute() or not plan_path.is_file():
        raise ImplementerStartError(
            f"plan must be an existing absolute file: {plan_path}"
        )
    if not offering_id.strip():
        raise ImplementerStartError("offering id is required")
    if not effort.strip():
        raise ImplementerStartError("effort is required")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ImplementerStartError(f"cwd must be an existing absolute directory: {cwd}")
    run_root.mkdir(parents=True, exist_ok=True)
    if not run_root.is_absolute() or not run_root.is_dir():
        raise ImplementerStartError(
            f"run_root must be an existing absolute directory: {run_root}"
        )

    frozen_plan = run_root / "plan.md"
    if frozen_plan.resolve() != plan_path.resolve():
        shutil.copy2(plan_path, frozen_plan)

    try:
        profile = _resolve_offering(offering_id, effort, cwd)
    except recruiter.OfferingError as error:
        raise ImplementerStartError(str(error)) from error
    roster = recruiter.load_roster(roster_path)
    slug = run_root.name
    control_dir = run_root / "control"
    receipt_path = control_dir / "implementer-start.json"
    gate_path = control_dir / "implementer-ready.fifo"
    instructions = control_dir / "instructions.md"
    script_path = control_dir / "start.sh"
    release_token = f"{slug}.plan-implementer"
    lock_path = run_root / ".implementer-start.lock"

    with _exclusive(lock_path):
        if receipt_path.is_file():
            try:
                existing = json.loads(receipt_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise ImplementerStartError(
                    f"implementer-start receipt {receipt_path} is unreadable: {error}"
                ) from error
            if isinstance(existing, dict) and existing.get("state") in (
                "ready",
                "ready-degraded",
            ):
                if existing.get("herdr_session") != herdr_session:
                    raise ImplementerStartError(
                        "implementer-start receipt belongs to a different Herdr session"
                    )
                existing_pane = existing.get("implementer_pane") or existing.get(
                    "leader_pane"
                )
                if isinstance(existing_pane, str) and existing_pane:
                    return cast(dict[str, object], existing)
            raise ImplementerStartError(
                f"implementer start already has non-ready state at {receipt_path}"
            )
        if gate_path.exists():
            raise ImplementerStartError(
                f"implementer-start artifacts already exist under {control_dir}"
            )

        assignment = (
            f"/plan-implementer --plan {shlex.quote(str(frozen_plan))} "
            f"--run-root {shlex.quote(str(run_root))} "
            f"--offering {shlex.quote(offering_id)} --effort {shlex.quote(profile['effort'])}"
        )
        _write_text_atomic(
            instructions,
            "# Plan-implementer startup\n\n"
            f"Configured agent/persona: `{IMPLEMENTER_AGENT}`.\n\n"
            "Run exactly this controller command and own the plan until it writes "
            "implementer-result.json:\n\n"
            f"```text\n{assignment}\n```\n",
        )
        launch = _resolve_launch(roster, profile, cwd, instructions)
        launch_in_cwd = f"cd {shlex.quote(str(cwd))} && {launch}"
        _write_text_atomic(
            script_path,
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f"IFS= read -r implementer_release_token < {shlex.quote(str(gate_path))}\n"
            f"rm -f {shlex.quote(str(gate_path))}\n"
            f'[[ "$implementer_release_token" == {shlex.quote(release_token)} ]]\n'
            f"export {IMPLEMENTER_START_RECEIPT_ENV}={shlex.quote(str(receipt_path))}\n"
            f"exec bash -lc {shlex.quote(launch_in_cwd)}\n",
            executable=True,
        )
        implementer_pane: str | None = None
        ready = False
        try:
            _create_gate(gate_path)
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "effort": profile["effort"],
                    "harness": profile["harness"],
                    "herdr_session": herdr_session,
                    "model": profile["model"],
                    "offering": offering_id,
                    "pass": 1,
                    "phase_id": IMPLEMENTER_PHASE_ID,
                    "plan_path": str(frozen_plan),
                    "run_id": slug,
                    "state": "starting",
                    "watchdog": {
                        "reason": "Flow 1: implementer-await owns delivery; no standing watchdog",
                        "state": "not-configured",
                    },
                },
            )
            implementer_pane, workspace_id = _start_gated(
                _safe_name(slug), hil_pane, cwd, script_path, herdr_session
            )
            _verify_gated(implementer_pane, script_path, herdr_session)
            _release_gate(gate_path, release_token)
            health = _health(implementer_pane, cwd, profile, roster, herdr_session)
            receipt = {
                "at_ns": time.time_ns(),
                "effort": profile["effort"],
                "harness": profile["harness"],
                "health": health,
                "herdr_session": herdr_session,
                "implementer_pane": implementer_pane,
                "leader_pane": implementer_pane,
                "model": profile["model"],
                "offering": offering_id,
                "pass": 1,
                "phase_id": IMPLEMENTER_PHASE_ID,
                "plan_path": str(frozen_plan),
                "run_id": slug,
                "state": "ready",
                "watchdog": {
                    "reason": "Flow 1: implementer-await owns delivery; no standing watchdog",
                    "state": "not-configured",
                },
                **({"workspace_id": workspace_id} if workspace_id else {}),
            }
            _write_json_atomic(receipt_path, receipt)
            ready = True
            return receipt
        finally:
            if not ready:
                gate_path.unlink(missing_ok=True)
                if implementer_pane is not None:
                    try:
                        recruiter._close_worker_pane(
                            implementer_pane, herdr_session=herdr_session
                        )
                    except (
                        recruiter.RecruiterError,
                        OSError,
                        subprocess.SubprocessError,
                    ) as close_error:
                        command_runtime.write_stderr(
                            "implementer-start cleanup: could not close gated "
                            f"implementer {implementer_pane}: {close_error}\n"
                        )


def main(argv: list[str] | None = None) -> int:
    parser = command_runtime.ArgumentParser(prog="upagent-implementer-start")
    parser.add_argument("plan", type=Path, help="approved plan.md")
    parser.add_argument("offering", help="offering id from just upagent lists")
    parser.add_argument("effort", help="effort the offering permits")
    parser.add_argument("run_root", type=Path, help="durable Flow 1 run tree")
    parser.add_argument(
        "--cwd",
        type=Path,
        default=_request_cwd(),
        help="implementer working directory",
    )
    parser.add_argument(
        "--hil-pane",
        default=command_runtime.getenv("HERDR_PANE_ID"),
        help="owning HIL pane",
    )
    parser.add_argument(
        "--roster", default=recruiter.default_roster_path(), help="UpAgent roster"
    )
    args = parser.parse_args(argv)
    if not args.hil_pane:
        parser.error("--hil-pane is required when HERDR_PANE_ID is not set")
    try:
        result = start_implementer(
            plan_path=args.plan.expanduser().resolve(),
            offering_id=args.offering,
            effort=args.effort,
            run_root=args.run_root.expanduser().resolve(),
            hil_pane=args.hil_pane,
            cwd=args.cwd.expanduser().resolve(),
            roster_path=args.roster,
        )
    except (
        OSError,
        ImplementerStartError,
        recruiter.RecruiterError,
        recruiter.OfferingError,
    ) as error:
        sys.exit(f"upagent-implementer-start: {error}")
    print(f"IMPLEMENTER_STARTED {json.dumps(result, sort_keys=True)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
