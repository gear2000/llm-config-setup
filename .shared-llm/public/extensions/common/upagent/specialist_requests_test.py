"""Hermetic tests for the resident public-request replay boundary.

Covers the dead-owner replay gap (independent review item 9): a `resident.json` marker left
`running` by a caller process that is provably gone must report `blocked` without ever
redelivering the question, in both `submit` (the replay path) and `status` (the read path),
using the same predicate for both.
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module() -> Any:
    return load("specialist_requests")


@pytest.fixture
def residents(tmp_path) -> Any:
    # A minimal stand-in: only `_read`/`_write`/`_private_dir`/`WAIT_SECONDS` are needed by
    # the code paths under test here (`submit`'s replay branch and `status`).
    lifecycle = load("specialist_lifecycle")
    return lifecycle


class FakeRegistered:
    def __init__(self, request_dir: Path):
        self.request_dir = request_dir
        self.request_id = "req-1"
        # `submit` reads `record["payload"]` unconditionally before ever looking at the
        # marker; a resident-typed payload keeps that read valid for every test here.
        self.record: dict = {
            "payload": {
                "type": "specialist",
                "specialist": "advisor",
                "cwd": str(request_dir),
            }
        }


def _api(*, dead: bool) -> Any:
    return SimpleNamespace(
        PublicError=RuntimeError,
        recruiter=SimpleNamespace(
            _process_start_time=lambda pid: None if dead else "same-birth"
        ),
    )


def test_submit_replay_with_dead_owner_reports_blocked_without_redelivery(
    tmp_path, module, residents
):
    request_dir = tmp_path / "req"
    request_dir.mkdir()
    registered = FakeRegistered(request_dir)
    marker_path = module.marker(registered)
    residents._write(
        marker_path,
        {
            "state": "running",
            "started_at": time.time(),
            "owner_pid": 999999,
            "owner_birth": "a-birth-that-will-never-match-again",
        },
    )
    calls: list[Any] = []

    def tripwire(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        raise AssertionError("must never redeliver")

    residents.consult = tripwire  # fresh module instance per test; no restore needed
    result = module.submit(_api(dead=True), residents, registered, "caller")
    assert result == (1, False)
    assert not calls
    # The marker is left exactly as it was for an operator to inspect -- never silently
    # rewritten to a terminal state, and never treated as proof delivery failed.
    assert residents._read(marker_path)["state"] == "running"


def test_submit_replay_with_live_owner_still_blocks_as_unresolved(
    tmp_path, module, residents
):
    request_dir = tmp_path / "req"
    request_dir.mkdir()
    registered = FakeRegistered(request_dir)
    marker_path = module.marker(registered)
    residents._write(
        marker_path,
        {
            "state": "running",
            "started_at": time.time(),
            "owner_pid": os.getpid(),
            "owner_birth": "same-birth",
        },
    )
    with pytest.raises(RuntimeError, match="unresolved"):
        module.submit(_api(dead=False), residents, registered, "caller")
    assert residents._read(marker_path)["state"] == "running"


def test_status_reports_blocked_for_dead_owner_without_mutating_marker(
    tmp_path, module, residents
):
    request_dir = tmp_path / "req"
    request_dir.mkdir()
    registered = FakeRegistered(request_dir)
    registered.record = {
        "payload": {"type": "specialist"},
        "payload_sha256": "sha",
        "offering_snapshot": {},
    }
    marker_path = module.marker(registered)
    residents._write(
        marker_path,
        {
            "state": "running",
            "started_at": time.time(),
            "owner_pid": 999999,
            "owner_birth": "a-birth-that-will-never-match-again",
        },
    )
    value = module.status(_api(dead=True), residents, registered)
    assert value["state"]["state"] == "blocked"
    assert "reason" in value["state"]
    assert residents._read(marker_path)["state"] == "running"
