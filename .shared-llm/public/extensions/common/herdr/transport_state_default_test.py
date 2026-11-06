"""The runner stack's fallback state folder belongs to the user running it.

The TUI controller's layout locks live beside this state file. A fixed /tmp fallback made
those locks one machine-wide folder owned by the first account that ran. The fallback now
matches the Recruiter's: the caller's home. Keep the two defaults identical.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _fresh_transport(monkeypatch: pytest.MonkeyPatch, home: Path):
    monkeypatch.setenv("HOME", str(home))
    name = "herdr_transport_state_default_under_test"
    spec = importlib.util.spec_from_file_location(name, HERE / "herdr_transport.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_fallback_state_file_is_in_the_callers_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPAGENT_STATE", raising=False)
    transport = _fresh_transport(monkeypatch, tmp_path)
    assert transport.STATE_FILE == tmp_path / ".local/state/herdr/upagent/recruiter.json"


def test_explicit_state_path_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_STATE", str(tmp_path / "pinned.json"))
    transport = _fresh_transport(monkeypatch, tmp_path / "home")
    assert transport.STATE_FILE == tmp_path / "pinned.json"
