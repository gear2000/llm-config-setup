"""Contract tests for Claude's direct phase-start guard hook."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


HOOK = (
    Path(__file__).resolve().parent.parent
    / ".shared-llm/public/llm/claude/common/hooks/herdr-phase-start-guard.py"
)


def _run(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )


def test_direct_phase_pane_injection_is_denied() -> None:
    completed = _run(
        {"tool_name": "Bash", "tool_input": {"command": "herdr pane run w:p '/herdr-phase --phase phase-0'"}}
    )

    decision = json.loads(completed.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_managed_phase_start_and_unrelated_commands_are_allowed() -> None:
    managed = _run(
        {"tool_name": "Bash", "tool_input": {"command": "just upagent-phase-start route.yaml run phase-0 1"}}
    )
    unrelated = _run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/phase"}})

    assert managed.stdout == ""
    assert unrelated.stdout == ""
