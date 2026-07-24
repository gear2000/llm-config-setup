#!/usr/bin/env python3
"""Per-command UpAgent entry point.

Every invocation imports the current canonical source and executes exactly one command.  There is
no resident Hub, socket handshake, module cache, or restart step.  Mutating command entry points
share one machine-local advisory lock; read-only commands never acquire it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _canonical_client_bootstrap() -> None:
    """Re-exec the main-checkout client before importing any UpAgent runtime module."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=HERE,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"cannot resolve canonical UpAgent client from {HERE}: {error}"
        ) from error
    common_path = Path(common).resolve()
    repo_root = common_path.parent if common_path.name == ".git" else common_path
    canonical = (
        repo_root / ".shared-llm/public/extensions/common/upagent/client.py"
    ).resolve()
    current = Path(__file__).resolve()
    if canonical != current:
        if not canonical.is_file():
            raise RuntimeError(f"canonical UpAgent client is missing: {canonical}")
        os.execv(sys.executable, [sys.executable, str(canonical), *sys.argv[1:]])


_canonical_client_bootstrap()


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ClientError(f"could not load UpAgent target {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


transport = _load("upagent_hub_transport", HERE / "hub_transport.py")
public_contract = _load("upagent_public_contract_client", HERE / "public_contract.py")
command_runtime = _load("upagent_command_runtime", HERE / "command_runtime.py")

TARGETS = {
    "public": "public_api.py",
    "recruiter": "recruiter.py",
    "phase-controller": "phase_controller.py",
    "phase-await": "phase_await.py",
    "direct-controller": "direct_controller.py",
}
RUNNER_TARGETS = {
    "tui-controller": "tui_controller.py",
    "run-lifecycle": "run_lifecycle.py",
}
RECRUITER_TARGETS = frozenset(
    ("public", "recruiter", "phase-controller", "direct-controller")
)
READ_ONLY_PUBLIC = frozenset(("help", "status", "get", "lists", "await", "await-any"))
READ_ONLY_RECRUITER = frozenset(("status", "specialists", "await", "await-any"))


class ClientError(RuntimeError):
    """A per-command discovery or dispatch fault."""


def _recruiter_command(argv: list[str]) -> str | None:
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--roster":
            if index + 1 >= len(argv):
                return None
            index += 2
        elif item.startswith("--roster="):
            index += 1
        else:
            return item
    return None


def _is_mutating(target: str, argv: list[str]) -> bool:
    """Classify command entry points; unknown commands fail closed as mutations."""
    command = argv[0] if argv else None
    if target == "public":
        return command not in READ_ONLY_PUBLIC
    if target == "recruiter":
        return _recruiter_command(argv) not in READ_ONLY_RECRUITER
    if target == "phase-await":
        return command != "wait"
    if target == "direct-controller":
        return command != "steps"
    return target not in ("tui-controller", "run-lifecycle")


def _canonical_target_path(target: str, _cwd: Path) -> Path:
    if target in TARGETS:
        return transport.canonical_module_path(TARGETS[target], HERE)
    if target in RUNNER_TARGETS:
        return (
            transport.canonical_repo_root(HERE)
            / ".shared-llm/public/extensions/common/runner"
            / RUNNER_TARGETS[target]
        ).resolve()
    raise ClientError(f"unknown command target: {target}")


def _load_command_modules(target: str, cwd: Path) -> tuple[Any, Any]:
    recruiter_path = transport.canonical_module_path("recruiter.py", HERE)
    recruiter = _load("upagent_recruiter_command", recruiter_path)
    ledger = transport.ledger_path(HERE)
    state = transport.state_path(HERE)
    recruiter._bind_command_runtime(ledger, state)
    if target == "recruiter":
        return recruiter, recruiter
    module = _load(
        f"upagent_target_{target.replace('-', '_')}",
        _canonical_target_path(target, cwd),
    )
    if target in RECRUITER_TARGETS:
        binder = getattr(module, "_bind_recruiter_runtime", None)
        if binder is None:
            raise ClientError(f"target {target} cannot bind the Recruiter runtime")
        binder(recruiter)
    return recruiter, module


def _reconciliation_needed(recruiter: Any) -> bool:
    ledger = recruiter.JobLedger()
    active = ledger.active / "requests"
    try:
        has_active = any(entry.is_dir() for entry in active.iterdir())
    except FileNotFoundError:
        has_active = False
    except OSError as error:
        raise ClientError(f"cannot inspect active UpAgent claims: {error}") from error
    return has_active or bool(ledger.incomplete_terminal_runners())


def _invoke_module(module: Any, argv: list[str], cwd: Path) -> int:
    environment = dict(os.environ)
    environment["UPAGENT_HUB_DIR"] = str(transport.ledger_path(HERE))
    environment["UPAGENT_STATE"] = str(transport.state_path(HERE))
    with command_runtime.activate(cwd, environment):
        try:
            returned = module.main(argv)
        except SystemExit as error:
            if error.code is None:
                return 0
            if isinstance(error.code, int):
                return error.code
            command_runtime.write_stderr(f"{error.code}\n")
            return 1
    return int(returned or 0)


def invoke(target: str, argv: list[str], cwd: Path) -> int:
    """Import current source, opportunistically reconcile, and execute one command."""
    recruiter, module = _load_command_modules(target, cwd)
    mutating = _is_mutating(target, argv)
    if mutating:
        recruiter._set_command_authorized(True)
        try:
            # Reconciliation performs process and Herdr work without an outer lock. Its
            # token-fenced JobLedger CAS methods acquire the one coarse lock only while each
            # durable mutation commits.
            if _reconciliation_needed(recruiter) and not (
                target == "recruiter" and _recruiter_command(argv) == "run-job"
            ):
                recruiter.cmd_reconcile(force=False, emit=False)
            return _invoke_module(module, argv, cwd)
        finally:
            recruiter._set_command_authorized(False)
    return _invoke_module(module, argv, cwd)


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    target = "public"
    if command[:1] == ["--target"]:
        if len(command) < 2:
            raise ClientError("--target requires a value")
        target, command = command[1], command[2:]
    if target == "hub":
        raise ClientError("the singleton Hub target was removed; use `upagent status`")
    if target not in {*TARGETS, *RUNNER_TARGETS}:
        raise ClientError(f"unknown command target: {target}")
    if target == "public":
        if not command or command in (["--help"], ["help"]):
            sys.stdout.write(public_contract.help_text())
            return 0
        try:
            public_contract.parse_argv(command)
        except public_contract.PublicCommandError as error:
            raise ClientError(str(error)) from error
    if not command:
        raise ClientError("an UpAgent command is required")
    return invoke(target, command, Path.cwd().resolve())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ClientError, RuntimeError) as error:
        raise SystemExit(f"upagent-client: {error}") from error
