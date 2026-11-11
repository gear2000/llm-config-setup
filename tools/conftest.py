"""Shared fixtures for the harness test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_kit_checkout_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `cmd_update` rebuilds the kit checkout's own offerings.yaml. Point that write
    # at a temp file so a test run never replaces the live hub's roster.
    monkeypatch.setenv("SHARED_LLM_KIT_ROSTER", str(tmp_path / "kit-offerings.yaml"))
