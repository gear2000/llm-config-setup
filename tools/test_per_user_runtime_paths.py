"""`just update` run logs belong to the user who runs them.

Several unix accounts can share one machine, each with its own kit checkout. A single fixed
log folder in /tmp belongs to whoever created it first, so every other account's
`just update` failed when it opened its log. These tests pin one private folder per user.
"""

from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest

from test_config_flow import _load


def _user_dir(root: Path) -> Path:
    return root / f".shared-llm-{os.geteuid()}"


def test_update_log_lives_in_a_folder_named_for_this_user(tmp_path: Path) -> None:
    harness = _load()
    log = harness._log_path(tmp_path)
    assert log.parent == _user_dir(tmp_path) / "log"
    assert log.suffix == ".log"


def test_update_log_folder_defaults_to_tmp(tmp_path: Path) -> None:
    harness = _load()
    default = inspect.signature(harness._log_path).parameters["tmp_root"].default
    assert default == Path("/tmp")


def test_update_log_folder_is_private(tmp_path: Path) -> None:
    harness = _load()
    harness._log_path(tmp_path)
    assert stat.S_IMODE(_user_dir(tmp_path).lstat().st_mode) == 0o700


def test_update_log_refuses_a_folder_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    other_uid = os.geteuid() + 4242
    (tmp_path / f".shared-llm-{other_uid}").mkdir()
    monkeypatch.setattr(harness.os, "geteuid", lambda: other_uid)
    with pytest.raises(SystemExit, match="not owned by"):
        harness._log_path(tmp_path)


def test_update_log_refuses_a_symlinked_folder(tmp_path: Path) -> None:
    harness = _load()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _user_dir(tmp_path).symlink_to(elsewhere)
    with pytest.raises(SystemExit, match="real directory"):
        harness._log_path(tmp_path)
