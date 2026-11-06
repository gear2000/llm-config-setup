"""tools/t3-shared-account.sh: several T3 Code servers under one Unix account.

The checks live in tools/test_t3_shared_account.sh, because they source the bash script and
call its functions in a throwaway HOME (env files, units, user list, git push helper). This
file runs that suite under `just test`. Nothing here starts a T3 server or touches real config.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tools/test_t3_shared_account.sh"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_t3_shared_account_suite() -> None:
    result = subprocess.run(
        ["bash", str(SUITE)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL " not in result.stdout, result.stdout
