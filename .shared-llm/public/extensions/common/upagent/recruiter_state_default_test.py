"""The Recruiter's fallback state file belongs to the user running it.

`client.py` normally binds state under the caller's home. When a module runs without that
binding, the fallback used to be one fixed /tmp path, which the first account to run claimed
for every other account on the machine. The fallback now lives in the caller's home.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _fresh_recruiter(monkeypatch: pytest.MonkeyPatch, home: Path):
    monkeypatch.setenv("HOME", str(home))
    name = "upagent_recruiter_state_default_under_test"
    spec = importlib.util.spec_from_file_location(name, HERE / "recruiter.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_fallback_state_file_is_in_the_callers_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPAGENT_STATE", raising=False)
    recruiter = _fresh_recruiter(monkeypatch, tmp_path)
    assert recruiter.STATE_FILE == tmp_path / ".local/state/herdr/upagent/recruiter.json"


def test_explicit_state_path_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_STATE", str(tmp_path / "pinned.json"))
    recruiter = _fresh_recruiter(monkeypatch, tmp_path / "home")
    assert recruiter.STATE_FILE == tmp_path / "pinned.json"
