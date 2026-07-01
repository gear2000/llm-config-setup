#!/usr/bin/env python3
"""TEMPLATE — a project-specific PreToolUse guard skeleton.

Fill this in for your repo (or delete it). It models the allow / deny / ask
pattern a PreToolUse hook uses to gate risky tool calls — e.g. block a bare
build/deploy command that should go through your task runner, or ask before
creating a new top-level module.

To adopt it:
  1. Rename this file to drop the TEMPLATE. prefix (e.g. `guard.py`).
  2. Replace the example FORBIDDEN patterns with your own.
  3. Wire it in your project `.claude/settings.json` under hooks.PreToolUse
     (see TEMPLATE.settings-overlay.json), or delete both if you don't need it.

Fail-open-but-loud: any unexpected error prints a traceback to stderr and
ALLOWS the tool. A guard hook must never brick the session on its own bug.
"""
import json
import re
import sys


def _emit(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def deny(reason: str) -> None:
    _emit("deny", reason)


def allow() -> None:
    # No output -> fall through to the normal permission flow.
    sys.exit(0)


# EXAMPLE: bare commands you want to force through a task runner instead.
# Anchored to command position so a mere mention (in an echo, a commit message)
# is not blocked — only an actual invocation.
_CMD_START = r"(?:^|[;&|(]\s*|\bsudo\s+)"
FORBIDDEN = [
    (_CMD_START + r"(?:terraform|tofu)\s+(?:apply|destroy)\b", "terraform/tofu apply or destroy"),
]


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        allow()
    data = json.loads(raw)
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    if tool == "Bash":
        cmd = " ".join((tool_input.get("command", "") or "").split())
        for rx, label in FORBIDDEN:
            if re.search(rx, cmd):
                deny(f"BLOCKED: `{label}` — route it through your task runner instead.")
        allow()

    allow()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        sys.stderr.write(
            "[example-hook] ERROR (allowing tool; fix the hook):\n" + traceback.format_exc()
        )
        sys.exit(0)
