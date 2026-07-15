#!/usr/bin/env python3
"""Block Claude from bypassing the deterministic phase-start transaction."""

from __future__ import annotations

import json
import sys


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return
    data = json.loads(raw)
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input") or {}
    command = " ".join(str(tool_input.get("command", "")).split())
    if "herdr pane run" in command and "/herdr-phase" in command:
        deny(
            "BLOCKED: phase leaders must be started through `just upagent-phase-start`; "
            "a direct /herdr-phase pane injection bypasses the required watchdog."
        )


if __name__ == "__main__":
    try:
        main()
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        sys.stderr.write(f"[herdr-phase-start-guard] allowing tool after hook error: {error}\n")
