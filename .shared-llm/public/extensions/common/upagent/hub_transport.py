#!/usr/bin/env python3
"""Small path and process helpers for per-command UpAgent execution.

UpAgent no longer has a socket protocol or resident Hub.  These helpers keep every linked
worktree on the same machine-local ledger while each invocation imports current source.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def canonical_repo_root(cwd: str | Path | None = None) -> Path:
    """Return the main checkout root shared by linked worktrees."""
    start = Path.cwd() if cwd is None else Path(cwd)
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=start,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"cannot resolve canonical UpAgent checkout from {start}: {error}"
        ) from error
    common_path = Path(common).resolve()
    return common_path.parent if common_path.name == ".git" else common_path


def canonical_module_path(filename: str, cwd: str | Path | None = None) -> Path:
    path = (
        canonical_repo_root(cwd)
        / ".shared-llm/public/extensions/common/upagent"
        / filename
    )
    if not path.is_file():
        raise RuntimeError(f"canonical UpAgent module is missing: {path}")
    return path.resolve()


def runtime_root(cwd: str | Path | None = None) -> Path:
    override = os.environ.get("UPAGENT_RUNTIME_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise RuntimeError("UPAGENT_RUNTIME_DIR must be absolute")
        return path.resolve()
    root = canonical_repo_root(cwd)
    repo_id = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    return Path.home() / ".local/state/herdr/upagent" / repo_id


def ledger_path(cwd: str | Path | None = None) -> Path:
    override = os.environ.get("UPAGENT_HUB_DIR")
    return (
        Path(override).expanduser().resolve()
        if override
        else runtime_root(cwd) / "ledger"
    )


def state_path(cwd: str | Path | None = None) -> Path:
    override = os.environ.get("UPAGENT_STATE")
    return (
        Path(override).expanduser().resolve()
        if override
        else runtime_root(cwd) / "services.json"
    )


def mutation_lock_path(cwd: str | Path | None = None) -> Path:
    """Return the one lock shared by the client and detached ledger owners."""
    return ledger_path(cwd).parent / "mutation.lock"


def process_start_time(pid: int) -> str | None:
    """Return Linux process birth ticks, or None when the process is absent."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None
