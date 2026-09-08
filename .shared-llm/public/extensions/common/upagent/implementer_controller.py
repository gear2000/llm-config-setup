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
import hashlib
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

_spec = importlib.util.spec_from_file_location(
    "upagent_implementer_contracts", HERE / "contracts.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("could not load UpAgent contracts")
contracts = cast(Any, importlib.util.module_from_spec(_spec))
_spec.loader.exec_module(contracts)

recruiter: Any = None

IMPLEMENTER_START_RECEIPT_ENV = "UPAGENT_IMPLEMENTER_START_RECEIPT"
CANONICAL_REPO_ENV = "UPAGENT_CANONICAL_REPO"
IMPLEMENTER_PHASE_ID = "plan"
STARTUP_TIMEOUT_MS = 45_000
GATE_RELEASE_WAIT_S = 2.0
GATE_RELEASE_SLEEP_S = 0.05
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
    deadline = time.monotonic() + GATE_RELEASE_WAIT_S
    while True:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                if error.errno == errno.ENXIO:
                    raise ImplementerStartError(
                        f"plan-implementer stopped waiting on gate {path}"
                    ) from error
                raise
            time.sleep(GATE_RELEASE_SLEEP_S)
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
        try:
            recruiter._close_worker_pane(pane_id, herdr_session=herdr_session)
        except (
            recruiter.RecruiterError,
            OSError,
            subprocess.SubprocessError,
        ) as close_error:
            command_runtime.write_stderr(
                "implementer-start cleanup: could not close mismatched "
                f"implementer {pane_id}: {close_error}\n"
            )
        raise ImplementerStartError(
            f"plan-implementer started in workspace {returned_workspace}, expected {workspace_id}"
        )
    return pane_id, returned_workspace if isinstance(returned_workspace, str) else ""


def _live_panes(herdr_session: str | None = None) -> set[str]:
    response = recruiter._herdr_json("pane", "list", herdr_session=herdr_session)
    panes = response.get("result", {}).get("panes", [])
    return {
        pane["pane_id"]
        for pane in panes
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str)
    }


def _place_in_control_tab(
    pane_id: str, workspace_id: str, herdr_session: str
) -> str:
    if not workspace_id:
        raise ImplementerStartError(
            "plan-implementer start returned no workspace_id; cannot place it in the control tab"
        )
    return cast(
        str,
        recruiter._place_started_agent_in_role_tab(
            pane_id,
            workspace_id,
            "control",
            split_direction="down",
            herdr_session=herdr_session,
        ),
    )


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


def _plan_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_repo_export() -> str | None:
    value = command_runtime.getenv(CANONICAL_REPO_ENV)
    if value is None or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ImplementerStartError(
            f"{CANONICAL_REPO_ENV} must be an existing absolute directory: {value}"
        )
    return str(path.resolve())


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

    try:
        profile = _resolve_offering(offering_id, effort, cwd)
    except recruiter.OfferingError as error:
        raise ImplementerStartError(str(error)) from error
    roster = recruiter.load_roster(roster_path)
    slug = run_root.name
    frozen_plan = run_root / "plan.md"
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
            if isinstance(existing, dict) and existing.get("state") == "failed":
                raise ImplementerStartError(
                    f"implementer start already failed at {receipt_path}; use a new run-root"
                )
            if isinstance(existing, dict) and existing.get("state") in (
                "ready",
                "ready-degraded",
            ):
                if existing.get("herdr_session") != herdr_session:
                    raise ImplementerStartError(
                        "implementer-start receipt belongs to a different Herdr session"
                    )
                if existing.get("hil_pane") != hil_pane:
                    raise ImplementerStartError(
                        "implementer-start receipt belongs to a different HIL pane"
                    )
                if existing.get("offering") != offering_id:
                    raise ImplementerStartError(
                        "implementer-start receipt offering does not match this launch"
                    )
                if existing.get("effort") != profile["effort"]:
                    raise ImplementerStartError(
                        "implementer-start receipt effort does not match this launch"
                    )
                recorded_digest = existing.get("plan_sha256")
                if recorded_digest != _plan_digest(plan_path):
                    raise ImplementerStartError(
                        "implementer-start receipt plan does not match this launch"
                    )
                try:
                    frozen_digest = _plan_digest(frozen_plan)
                except OSError as error:
                    raise ImplementerStartError(
                        f"implementer-start frozen plan is missing or unreadable: {error}"
                    ) from error
                if recorded_digest != frozen_digest:
                    raise ImplementerStartError(
                        "implementer-start frozen plan does not match the recorded digest"
                    )
                existing_plan = existing.get("plan_path")
                if (
                    not isinstance(existing_plan, str)
                    or Path(existing_plan).resolve() != frozen_plan.resolve()
                ):
                    raise ImplementerStartError(
                        "implementer-start receipt plan_path does not match this launch"
                    )
                existing_pane = existing.get("implementer_pane") or existing.get(
                    "leader_pane"
                )
                if isinstance(existing_pane, str) and existing_pane:
                    if existing_pane not in _live_panes(herdr_session):
                        raise ImplementerStartError(
                            "implementer-start receipt says ready but its implementer is no longer live"
                        )
                    return cast(dict[str, object], existing)
            raise ImplementerStartError(
                f"implementer start already has non-ready state at {receipt_path}"
            )
        leftover_result = run_root / "implementer-result.json"
        if leftover_result.is_file():
            raise ImplementerStartError(
                f"run root already has {leftover_result}; use a new run-root"
            )
        if control_dir.exists() and any(control_dir.iterdir()):
            raise ImplementerStartError(
                f"run root already has control artifacts under {control_dir}; use a new run-root"
            )
        if gate_path.exists():
            raise ImplementerStartError(
                f"implementer-start artifacts already exist under {control_dir}"
            )
        if frozen_plan.resolve() != plan_path.resolve():
            shutil.copy2(plan_path, frozen_plan)
        plan_sha256 = _plan_digest(frozen_plan)
        canonical_repo = _canonical_repo_export()
        canonical_fields = (
            {"canonical_repo": canonical_repo} if canonical_repo is not None else {}
        )
        env_exports = (
            f"export {IMPLEMENTER_START_RECEIPT_ENV}={shlex.quote(str(receipt_path))}\n"
        )
        if canonical_repo is not None:
            env_exports += (
                f"export {CANONICAL_REPO_ENV}={shlex.quote(canonical_repo)}\n"
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
            "Wait until $UPAGENT_IMPLEMENTER_START_RECEIPT has `state: ready` (or "
            "`ready-degraded`) before hiring. Run exactly this controller command and "
            "own the plan until it writes implementer-result.json:\n\n"
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
            f"{env_exports}"
            f"exec bash -lc {shlex.quote(launch_in_cwd)}\n",
            executable=True,
        )
        implementer_pane: str | None = None
        ready = False
        watchdog = {
            "reason": "Flow 1: implementer-await owns delivery; no standing watchdog",
            "state": "not-configured",
        }
        try:
            _create_gate(gate_path)
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "effort": profile["effort"],
                    "harness": profile["harness"],
                    "herdr_session": herdr_session,
                    "hil_pane": hil_pane,
                    "model": profile["model"],
                    "offering": offering_id,
                    "pass": 1,
                    "phase_id": IMPLEMENTER_PHASE_ID,
                    "plan_path": str(frozen_plan),
                    "plan_sha256": plan_sha256,
                    "run_id": slug,
                    "state": "preparing",
                    "watchdog": watchdog,
                    **canonical_fields,
                },
            )
            implementer_pane, workspace_id = _start_gated(
                _safe_name(slug), hil_pane, cwd, script_path, herdr_session
            )
            _verify_gated(implementer_pane, script_path, herdr_session)
            implementer_pane = _place_in_control_tab(
                implementer_pane, workspace_id, herdr_session
            )
            ownership = {
                "leader": {"pane_id": implementer_pane, "state": "created"},
                "pane": {"pane_id": implementer_pane, "state": "created"},
            }
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "effort": profile["effort"],
                    "harness": profile["harness"],
                    "herdr_session": herdr_session,
                    "hil_pane": hil_pane,
                    "implementer_pane": implementer_pane,
                    "leader_pane": implementer_pane,
                    "model": profile["model"],
                    "offering": offering_id,
                    "ownership": ownership,
                    "pass": 1,
                    "phase_id": IMPLEMENTER_PHASE_ID,
                    "plan_path": str(frozen_plan),
                    "plan_sha256": plan_sha256,
                    "run_id": slug,
                    "state": "implementer-gated",
                    "watchdog": watchdog,
                    **canonical_fields,
                    **({"workspace_id": workspace_id} if workspace_id else {}),
                },
            )
            _release_gate(gate_path, release_token)
            health = _health(implementer_pane, cwd, profile, roster, herdr_session)
            receipt = {
                "at_ns": time.time_ns(),
                "effort": profile["effort"],
                "harness": profile["harness"],
                "health": health,
                "herdr_session": herdr_session,
                "hil_pane": hil_pane,
                "implementer_pane": implementer_pane,
                "leader_pane": implementer_pane,
                "model": profile["model"],
                "offering": offering_id,
                "ownership": ownership,
                "pass": 1,
                "phase_id": IMPLEMENTER_PHASE_ID,
                "plan_path": str(frozen_plan),
                "plan_sha256": plan_sha256,
                "run_id": slug,
                "state": "ready",
                "watchdog": watchdog,
                **canonical_fields,
                **({"workspace_id": workspace_id} if workspace_id else {}),
            }
            _write_json_atomic(receipt_path, receipt)
            ready = True
            return receipt
        except (
            ImplementerStartError,
            recruiter.RecruiterError,
            recruiter.OfferingError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            _write_json_atomic(
                receipt_path,
                {
                    "at_ns": time.time_ns(),
                    "effort": profile["effort"],
                    "harness": profile["harness"],
                    "herdr_session": herdr_session,
                    "hil_pane": hil_pane,
                    "implementer_pane": implementer_pane,
                    "offering": offering_id,
                    "pass": 1,
                    "phase_id": IMPLEMENTER_PHASE_ID,
                    "plan_path": str(frozen_plan),
                    "plan_sha256": plan_sha256,
                    "reason": str(error),
                    "run_id": slug,
                    "state": "failed",
                    **canonical_fields,
                },
            )
            raise
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


def finish_implementer(receipt_path: Path, *, force: bool = False) -> dict[str, object]:
    receipt_path = receipt_path.resolve()
    if not receipt_path.is_file():
        raise ImplementerStartError(f"implementer-start receipt not found: {receipt_path}")
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ImplementerStartError(
            f"implementer-start receipt unreadable: {error}"
        ) from error
    if not isinstance(receipt, dict) or receipt.get("state") not in (
        "ready",
        "ready-degraded",
    ):
        raise ImplementerStartError(
            "finish requires a ready implementer-start receipt"
        )
    pane = receipt.get("implementer_pane") or receipt.get("leader_pane")
    hil_pane = receipt.get("hil_pane")
    run_id = receipt.get("run_id")
    if not isinstance(pane, str) or not pane:
        raise ImplementerStartError("implementer-start receipt has no implementer_pane")
    if pane == hil_pane:
        raise ImplementerStartError("refusing to close the HIL pane")
    if not isinstance(run_id, str) or not run_id:
        raise ImplementerStartError("implementer-start receipt has no run_id")
    run_root = receipt_path.parent.parent
    result_path = run_root / "implementer-result.json"
    result: dict[str, object] | None
    try:
        result = contracts.parse_implementer_result(
            result_path.read_text(),
            expected_run_root=run_root,
            expected_run_id=run_id,
        )
    except (OSError, contracts.ContractError) as error:
        if not force:
            raise ImplementerStartError(
                f"finish requires a valid implementer-result.json: {error}"
            ) from error
        result = None
    session = receipt.get("herdr_session")
    recruiter._close_worker_pane(
        pane, herdr_session=session if isinstance(session, str) else None
    )
    return {
        "closed_pane": pane,
        "force": force,
        "hil_pane": hil_pane,
        "result": result,
        "run_id": run_id,
    }


def _cmd_finish(argv: list[str]) -> int:
    parser = command_runtime.ArgumentParser(prog="upagent-implementer-finish")
    parser.add_argument("receipt", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="close the recorded implementer pane even when implementer-result.json is missing",
    )
    args = parser.parse_args(argv)
    result = finish_implementer(args.receipt.expanduser().resolve(), force=args.force)
    print(f"IMPLEMENTER_FINISHED {json.dumps(result, sort_keys=True)}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "finish":
        try:
            return _cmd_finish(argv[1:])
        except (
            OSError,
            ImplementerStartError,
            recruiter.RecruiterError,
            recruiter.OfferingError,
        ) as error:
            sys.exit(f"upagent-implementer-finish: {error}")
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
