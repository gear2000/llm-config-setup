"""Configuration and bounded prompts for UpAgent's LLM management roles."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import string
import uuid


ROLE_TEMPLATE_FIELDS = {"brief_path", "cwd", "output_path"}
MAX_INTAKE_CLERK_TIMEOUT_MS = 300_000
DEFAULT_ACCOUNT_MANAGER_COMMAND = (
    'claude --dangerously-skip-permissions --agent upagent-account-manager --model sonnet --effort low '
    '"Read {brief_path}, perform that one lifecycle review, write {output_path}, then remain available."'
)
DEFAULT_CHECKER_COMMAND = (
    'claude --dangerously-skip-permissions --agent upagent-checker --model haiku --effort low '
    '"Read {brief_path}, perform that one bounded assessment, write {output_path}, then exit."'
)
DEFAULT_INTAKE_CLERK_COMMAND = (
    'claude --print --output-format text --tools "" --agent intake-clerk '
    '--model sonnet --effort low < {brief_path}'
)


class ManagementConfigError(ValueError):
    """An invalid LLM management-role configuration."""


@dataclass(frozen=True)
class ManagementRole:
    command: str
    expected_agent: str
    expected_process: str
    timeout_ms: int


# How each request lifecycle is owned. KEEP IN SYNC with contracts.MANAGEMENT_MODES
# (both modules load standalone by path).
MANAGEMENT_MODES = ("direct", "dedicated")


@dataclass(frozen=True)
class ManagementConfig:
    account_manager: ManagementRole
    checker: ManagementRole
    intake_clerk: ManagementRole
    startup_timeout_ms: int
    inactivity_check_ms: int
    requester_grace_ms: int
    mode: str = "direct"
    # One automatic broker-advised relaunch when a worker launch fails or stalls before
    # health verification. The fast Python path stays the default; intelligence is hired
    # exactly at the failure point.
    rescue_on_startup_failure: bool = True


def _positive_int(value: object, field: str, default: int) -> int:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
        raise ManagementConfigError(f"management.{field} must be a positive integer")
    return candidate


def _role(
    raw: object,
    name: str,
    default_command: str,
    *,
    max_timeout_ms: int | None = None,
) -> ManagementRole:
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
    if max_timeout_ms is not None and timeout_ms > max_timeout_ms:
        raise ManagementConfigError(
            f"management.{name}.timeout_ms must be no greater than {max_timeout_ms}"
        )
    return ManagementRole(command, expected_agent, expected_process, timeout_ms)


def load_management_config(roster: dict) -> ManagementConfig:
    raw = roster.get("management", {})
    if not isinstance(raw, dict):
        raise ManagementConfigError("management must be an object")
    mode = raw.get("mode", "direct")
    if mode not in MANAGEMENT_MODES:
        raise ManagementConfigError("management.mode must be one of " + ", ".join(MANAGEMENT_MODES))
    rescue = raw.get("rescue_on_startup_failure", True)
    if not isinstance(rescue, bool):
        raise ManagementConfigError("management.rescue_on_startup_failure must be a boolean")
    return ManagementConfig(
        account_manager=_role(raw.get("account_manager"), "account_manager", DEFAULT_ACCOUNT_MANAGER_COMMAND),
        checker=_role(raw.get("checker"), "checker", DEFAULT_CHECKER_COMMAND),
        intake_clerk=_role(
            raw.get("intake_clerk"),
            "intake_clerk",
            DEFAULT_INTAKE_CLERK_COMMAND,
            max_timeout_ms=MAX_INTAKE_CLERK_TIMEOUT_MS,
        ),
        startup_timeout_ms=_positive_int(raw.get("startup_timeout_ms"), "startup_timeout_ms", 45_000),
        inactivity_check_ms=_positive_int(raw.get("inactivity_check_ms"), "inactivity_check_ms", 900_000),
        requester_grace_ms=_positive_int(raw.get("requester_grace_ms"), "requester_grace_ms", 300_000),
        mode=mode,
        rescue_on_startup_failure=rescue,
    )


def render_role_command(role: ManagementRole, brief_path: Path, cwd: str, output_path: Path) -> str:
    return role.command.format(brief_path=brief_path, cwd=cwd, output_path=output_path)


def render_intake_clerk_command(
    role: ManagementRole, brief_path: Path, cwd: str, output_path: Path
) -> str:
    """Render the trusted configured clerk command and atomically capture its stdout.

    The shipped command is no-tools. The roster is trusted executable configuration and may
    override that command, so roster changes require review. Every path substitution is
    shell-quoted; caller payload text never enters this command. The wrapper owns output
    persistence, so the shipped command needs no filesystem or shell tools.
    """
    command = role.command.format(
        brief_path=shlex.quote(str(brief_path)),
        cwd=shlex.quote(cwd),
        output_path=shlex.quote(str(output_path)),
    )
    temporary = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.stdout.tmp"
    )
    return (
        "set -eu; umask 077; "
        f"_out={shlex.quote(str(output_path))}; "
        f"_tmp={shlex.quote(str(temporary))}; "
        "trap 'rm -f -- \"$_tmp\"' EXIT HUP INT TERM; "
        f"{command} >\"$_tmp\"; "
        "mv -f -- \"$_tmp\" \"$_out\"; trap - EXIT HUP INT TERM"
    )


def intake_clerk_brief(raw_text: str, raw_path: Path, output_path: Path) -> str:
    """One bounded normalization assignment. The clerk interprets form, never authority."""
    return f"""# UpAgent intake envelope normalization

A caller submitted one imperfect work-order envelope. The exact submitted bytes are preserved at
`{raw_path}` and repeated below. Convert only its FORM into canonical order fields, or refuse.
Do not execute the task, inspect a repository, launch an agent, or authorize an operation.

NEVER invent or change any execution intent. This includes target harness/model/effort,
agent/persona, cwd, task/instructions_path, cockpit_pane/requester, lifecycle mode,
operation/apply/approval or plan artifact, env, timeout, management placement, plan/phase/step
identity, consult authority, and watchdog identity. Values in the interpreted order must already
be explicitly present in the submitted payload. If a required value is absent, conflicting, or
ambiguous, refuse and name it. Python alone may generate bookkeeping identifiers, a result path,
and missing phase/stage bookkeeping after it independently verifies provenance.

Return STRICT JSON as your only stdout. Python captures that stdout at `{output_path}` and runs
all provenance and contract checks. Return exactly one of these shapes and nothing else:

```json
{{"order": {{"harness": "...", "model": "...", "agent": "...", "cwd": "...", "instructions_path": "...", "cockpit_pane": "..."}}, "notes": ["form-only change"]}}
```

```json
{{"refusal": "what is missing or ambiguous", "understood": ["explicit value"], "missing": ["field"]}}
```

----- BEGIN EXACT SUBMISSION -----
{raw_text}
----- END EXACT SUBMISSION -----
"""


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
The route and roster are authoritative: do not infer that a model is review-only, coding-only, or
otherwise restricted from its name or prior model knowledge. Reject only an explicit mechanical
error or explicit supplied policy. For requester clarification, list concrete `requested_changes`.
Write exactly one JSON object to `{output_path}`:

```json
{{
  "request_id": "{request_id}",
  "generation": {generation},
  "decision": "approved|needs-requester|blocked",
  "message": "concise explanation for the requester",
  "requested_changes": ["optional concrete correction or clarification"]
}}
```
"""


def checker_brief(request_id: str, generation: int, evidence_path: Path, output_path: Path) -> str:
    return f"""# One-shot UpAgent lifecycle assessment

This assessment is advisory. Python supplied bounded mechanical evidence at `{evidence_path}`.
Read it and, only when useful, read the named worker pane's recent output.
Do not create, close, interrupt, or kill any pane. Do not declare a verdict for the worker's task.

The literal request id is `{request_id}` and the literal generation is `{generation}`. Copy those
values exactly into the response. Directory names, path components, pane names, and evidence fields
are not request ids; never derive or substitute an identity from them.

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
