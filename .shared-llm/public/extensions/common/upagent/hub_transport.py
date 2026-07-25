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

CANONICAL_REPO_ENV = "UPAGENT_CANONICAL_REPO"
MAIN_BRANCH_REF = "refs/heads/main"
UPAGENT_CLIENT_REL = Path(".shared-llm/public/extensions/common/upagent/client.py")


def _git(start: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=start,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"cannot resolve canonical UpAgent checkout from {start}: {error}"
        ) from error


def _worktree_records(porcelain: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in porcelain.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def _validated_checkout_root(path: Path, label: str) -> Path:
    root = Path(
        _git(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
    ).resolve()
    if root != path.resolve():
        raise RuntimeError(f"{label} must be a checkout root, got {path}")
    if not (root / UPAGENT_CLIENT_REL).is_file():
        raise RuntimeError(f"{label} does not contain UpAgent source: {root}")
    return root


def _explicit_repo_root() -> Path | None:
    value = os.environ.get(CANONICAL_REPO_ENV)
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"{CANONICAL_REPO_ENV} must be absolute")
    return _validated_checkout_root(path.resolve(), CANONICAL_REPO_ENV)


def _main_checkout_candidates(start: Path) -> list[Path]:
    records = _worktree_records(_git(start, "worktree", "list", "--porcelain"))
    candidates: list[Path] = []
    seen: set[Path] = set()
    for record in records:
        if "bare" in record or record.get("branch") != MAIN_BRANCH_REF:
            continue
        worktree = record.get("worktree")
        if not worktree:
            continue
        path = Path(worktree).resolve()
        if path in seen or not (path / UPAGENT_CLIENT_REL).is_file():
            continue
        candidates.append(_validated_checkout_root(path, "main worktree"))
        seen.add(path)
    return candidates


def canonical_repo_root(cwd: str | Path | None = None) -> Path:
    """Return the main checkout root shared by linked worktrees."""
    start = Path.cwd() if cwd is None else Path(cwd)
    explicit = _explicit_repo_root()
    if explicit is not None:
        return explicit
    candidates = _main_checkout_candidates(start)
    if len(candidates) == 1:
        return candidates[0]
    hint = f"set {CANONICAL_REPO_ENV} to an absolute checkout root"
    if not candidates:
        raise RuntimeError(
            f"no checked-out main branch UpAgent source found from {start}; {hint}"
        )
    raise RuntimeError(
        "ambiguous checked-out main branch UpAgent sources: "
        + ", ".join(str(path) for path in candidates)
        + f"; {hint}"
    )


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



