"""tools/t3-setup.sh: one person's own always-on T3 Code server.

The checks live in tools/test_t3_setup.sh, because they source the bash script and call its
functions in a throwaway HOME (port choice, service.env, readiness, worktree default). This
file runs that suite under `just test`. Nothing here installs T3 or starts a server.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tools/test_t3_setup.sh"


@pytest.mark.skipif(shutil.which("ss") is None, reason="ss (iproute2) is required")
def test_t3_setup_suite() -> None:
    result = subprocess.run(
        ["bash", str(SUITE)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL " not in result.stdout, result.stdout
