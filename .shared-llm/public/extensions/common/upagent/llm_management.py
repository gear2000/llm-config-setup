"""Configuration and bounded prompts for UpAgent's LLM management roles."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import string


ROLE_TEMPLATE_FIELDS = {"brief_path", "cwd", "output_path"}
DEFAULT_ACCOUNT_MANAGER_COMMAND = (
    'claude --dangerously-skip-permissions --agent upagent-account-manager --model haiku --effort low '
    '"Read {brief_path}, perform that one lifecycle review, write {output_path}, then remain available."'
)
DEFAULT_CHECKER_COMMAND = (
    'claude --dangerously-skip-permissions --agent upagent-checker --model haiku --effort low '
    '"Read {brief_path}, perform that one bounded assessment, write {output_path}, then exit."'
)


class ManagementConfigError(ValueError):
    """An invalid LLM management-role configuration."""


@dataclass(frozen=True)
class ManagementRole:
    command: str
    expected_agent: str
    expected_process: str
    timeout_ms: int


@dataclass(frozen=True)
class ManagementConfig:
    account_manager: ManagementRole
    checker: ManagementRole
    startup_timeout_ms: int
    inactivity_check_ms: int
    requester_grace_ms: int


def _positive_int(value: object, field: str, default: int) -> int:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
        raise ManagementConfigError(f"management.{field} must be a positive integer")
    return candidate


def _role(raw: object, name: str, default_command: str) -> ManagementRole:
    value = {} if raw is None else raw
    if not isinstance(value, dict):
        raise ManagementConfigError(f"management.{name} must be an object")
    command = value.get("command", default_command)
    if not isinstance(command, str) or not command.strip():
        raise ManagementConfigError(f"management.{name}.command must be a non-empty string")
    fields = {field_name for _, field_name, _, _ in string.Formatter().parse(command) if field_name}
    unknown = fields - ROLE_TEMPLATE_FIELDS
    if unknown:
        raise ManagementConfigError(
            f"management.{name}.command uses unknown placeholder(s): {', '.join(sorted(unknown))}"
        )
    expected_agent = value.get("expected_agent", "claude")
    expected_process = value.get("expected_process", "claude")
    if not isinstance(expected_agent, str) or not expected_agent:
        raise ManagementConfigError(f"management.{name}.expected_agent must be a non-empty string")
    if not isinstance(expected_process, str) or not expected_process:
        raise ManagementConfigError(f"management.{name}.expected_process must be a non-empty string")
    timeout_ms = _positive_int(value.get("timeout_ms"), f"{name}.timeout_ms", 120_000)
    return ManagementRole(command, expected_agent, expected_process, timeout_ms)


def load_management_config(roster: dict) -> ManagementConfig:
    raw = roster.get("management", {})
    if not isinstance(raw, dict):
        raise ManagementConfigError("management must be an object")
    return ManagementConfig(
        account_manager=_role(raw.get("account_manager"), "account_manager", DEFAULT_ACCOUNT_MANAGER_COMMAND),
        checker=_role(raw.get("checker"), "checker", DEFAULT_CHECKER_COMMAND),
        startup_timeout_ms=_positive_int(raw.get("startup_timeout_ms"), "startup_timeout_ms", 45_000),
        inactivity_check_ms=_positive_int(raw.get("inactivity_check_ms"), "inactivity_check_ms", 900_000),
        requester_grace_ms=_positive_int(raw.get("requester_grace_ms"), "requester_grace_ms", 300_000),
    )


def render_role_command(role: ManagementRole, brief_path: Path, cwd: str, output_path: Path) -> str:
    return role.command.format(brief_path=brief_path, cwd=cwd, output_path=output_path)


def account_manager_brief(
    request_id: str,
    generation: int,
    order: dict,
    output_path: Path,
    mechanical_validation: dict | None = None,
) -> str:
    return f"""# Dedicated Account Manager review

You own conversation and interpretation for exactly one UpAgent request. Python owns durable
state and execution. Do not create, close, or kill a Herdr pane. Do not interrupt a pane or modify
the worker's result.

Request id: `{request_id}`
Generation: `{generation}`
Requested worker configuration:

```json
{json.dumps({key: order.get(key) for key in ('order_id', 'harness', 'model', 'agent', 'effort', 'cwd')}, indent=2)}
```

Python mechanical validation (authoritative for its stated facts):

```json
{json.dumps(mechanical_validation or {"valid": True, "errors": []}, indent=2)}
```

Decide whether the request is semantically coherent. An unsupported model, effort, persona, or
contradictory request is `needs-requester`; any mechanical validation error must also be
`needs-requester`; an unrecoverable unsafe request is `blocked`; otherwise it is `approved`.
Write exactly one JSON object to `{output_path}`:

```json
{{
  "request_id": "{request_id}",
  "generation": {generation},
  "decision": "approved|needs-requester|blocked",
  "message": "concise explanation for the requester"
}}
```
"""


def checker_brief(request_id: str, generation: int, evidence_path: Path, output_path: Path) -> str:
    return f"""# One-shot UpAgent lifecycle assessment

This assessment is advisory. Python supplied bounded mechanical evidence at `{evidence_path}`.
Read it and, only when useful, read the named worker pane's recent output.
Do not create, close, interrupt, or kill any pane. Do not declare a verdict for the worker's task.

Write exactly one JSON object to `{output_path}`:

```json
{{
  "request_id": "{request_id}",
  "generation": {generation},
  "assessment": "healthy|suspected-stall|startup-failed|completed|unknown",
  "confidence": 0.0,
  "evidence": ["specific observation"],
  "recommended_action": "none|ask-requester|retry-startup|inspect|extend|cancel",
  "message": "concise explanation for the requester"
}}
```
"""
