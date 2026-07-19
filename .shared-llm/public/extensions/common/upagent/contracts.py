"""UpAgent order/result contracts — the single source of truth for the files the
phase leader and a worker exchange through the Recruiter.

Two JSON files cross the boundary:

  order.json   — the phase leader writes it, the Recruiter reads it to hire a worker.
  result.json  — the worker writes it before its pane closes; the leader reads it as
                 the authoritative outcome of the stage.

Everything here is fail-loud: a malformed order or result raises `ContractError` with a
precise message rather than being silently tolerated. The Recruiter refuses to hire on a
bad order; the leader treats a missing/bad result as a blocked stage.

This module is pure stdlib and has no Herdr dependency, so it is unit-testable without a
running Herdr instance (see contracts_test.py).
"""

from __future__ import annotations

import json
from pathlib import Path

# Verdicts a worker may report. `passed` advances; `failed` loops back per `revisit`;
# `blocked` means the worker could not proceed and needs the leader/human.
VERDICTS = ("passed", "failed", "blocked")

# The rulings an ADVISOR order may add via the optional `decision` field (an advisor is hired
# like any worker on budget exhaustion; it reports verdict `passed` plus its ruling here). The
# controller reads `decision` to decide whether to keep going, keep looping, or stop for a human.
ADVISOR_DECISIONS = ("continue", "loop", "stop-ask-human")

# The stage ids a `revisit` list may name — the six recognized stages. Kept in sync with
# meta-plan-schema.ts RECOGNIZED_STAGE_IDS.
RECOGNIZED_STAGE_IDS = (
    "stage-0-alignment",
    "stage-1-implementation",
    "stage-2-adversarial-audit",
    "stage-3-integration-acceptance-seams",
    "stage-4-upstream-dag-verification",
    "stage-5-finalization",
)

# Harnesses the roster (upagent.yaml) may map a launch template for.
KNOWN_HARNESSES = ("claude", "codex", "pi", "cursor")
MANAGER_PLACEMENT_MODES = ("shared", "requester", "workspace")
# Per-order lifecycle-ownership override. KEEP IN SYNC with
# llm_management.MANAGEMENT_MODES (both modules load standalone by path).
MANAGEMENT_MODES = ("direct", "dedicated")
OPERATIONS = ("plan", "apply")
WATCHDOG_KINDS = {
    "phase-watchdog": "phase",
    "plan-lifecycle-watchdog": "plan",
}

# Required keys on an order.json the leader writes.
ORDER_REQUIRED = (
    "order_id",  # unique per work order, e.g. "phase-0.stage-1-implementation.pass-1.try-1"
    "phase_id",  # e.g. "phase-0"
    "stage_id",  # one of RECOGNIZED_STAGE_IDS
    "harness",  # resolved from the route llm_profile: one of KNOWN_HARNESSES
    # NOTE: `model` is validated separately below — it is required present but MAY be empty
    # (claude resolves its model from --model on the harness side), so it is not in this
    # non-empty-required set.
    "agent",  # persona name from the route stage entry
    "cwd",  # absolute path the worker runs in (the phase worktree)
    "instructions_path",  # absolute path to the stage brief the worker reads
    "result_path",  # absolute path the worker MUST write result.json to
    "cockpit_pane",  # id of an existing pane IN the cockpit workspace to split the worker
    # from (Herdr `pane split` takes a source pane, not a workspace label).
    # /herdr-run creates the cockpit and threads this pane id down.
)

# Required keys on a result.json the worker writes.
RESULT_REQUIRED = (
    "order_id",  # MUST echo the order's order_id
    "verdict",  # one of VERDICTS
    "full_log",  # pointer to the harness transcript (absolute path or session id)
)

# Required keys on each entry of a result's optional `consults` list — the worker's record of
# the specialists it asked. `request_id` is the load-bearing one: it is an ordinary UpAgent
# request id, which is what lets the Recruiter resolve the claim against its own ledger instead
# of taking the worker's word for it.
CONSULT_CLAIM_REQUIRED = ("consult_id", "specialist", "request_id", "answer_path")


class ContractError(ValueError):
    """A malformed order.json or result.json. Raised fail-loud; never swallowed."""


def _require_str(obj: dict, key: str, where: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or val == "":
        raise ContractError(f"{where}: `{key}` must be a non-empty string (got {val!r})")
    return val


def parse_order(text: str) -> dict:
    """Validate + return an order dict from raw JSON text. Fail-loud on any problem."""
    try:
        order = json.loads(text)
    except json.JSONDecodeError as e:
        raise ContractError(f"order.json is not valid JSON: {e}") from e
    if not isinstance(order, dict):
        raise ContractError("order.json must be a JSON object")

    for key in ORDER_REQUIRED:
        _require_str(order, key, "order.json")

    if order["stage_id"] not in RECOGNIZED_STAGE_IDS:
        raise ContractError(
            f"order.json: unknown stage_id {order['stage_id']!r}; expected one of {', '.join(RECOGNIZED_STAGE_IDS)}"
        )
    if order["harness"] not in KNOWN_HARNESSES:
        raise ContractError(
            f"order.json: unknown harness {order['harness']!r}; expected one of {', '.join(KNOWN_HARNESSES)}"
        )
    # `model` is required present but may be empty (claude resolves it from --model on the
    # harness side); enforce the key exists and is a string.
    if not isinstance(order.get("model"), str):
        raise ContractError("order.json: `model` must be a string (may be empty)")
    # `effort` is optional. When present it must be a string; the leader resolves it from the
    # route llm_profile (`medium` when the profile omits it), so a template that uses {effort}
    # for Claude or Codex never formats an empty value.
    if "effort" in order and not isinstance(order["effort"], str):
        raise ContractError("order.json: `effort` must be a string when present")
    # `timeout_ms` is optional. bool is an int subclass, so reject it explicitly: a timeout
    # must be an actual, positive integer before the Recruiter starts any runner work.
    if "timeout_ms" in order:
        timeout_ms = order["timeout_ms"]
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise ContractError("order.json: `timeout_ms` must be a positive integer when present")
    # Optional `env` must be a flat str->str map when present (injected via `pane split --env`).
    env = order.get("env")
    if env is not None and (
        not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
    ):
        raise ContractError("order.json: `env` must be a map of string->string")
    request_id = order.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ContractError("order.json: `request_id` must be a non-empty string when present")
    requester = order.get("requester")
    if requester is not None:
        if not isinstance(requester, dict):
            raise ContractError("order.json: `requester` must be an object when present")
        for field in ("id", "kind", "address"):
            value = requester.get(field)
            if not isinstance(value, str) or not value:
                raise ContractError(f"order.json: `requester.{field}` must be a non-empty string")
    placement = order.get("manager_placement")
    if placement is not None:
        if not isinstance(placement, dict):
            raise ContractError("order.json: `manager_placement` must be an object when present")
        mode = placement.get("mode")
        if mode not in MANAGER_PLACEMENT_MODES:
            raise ContractError("order.json: `manager_placement.mode` must be one of " + ", ".join(MANAGER_PLACEMENT_MODES))
        for field in ("workspace_id", "workspace_label", "anchor_pane"):
            value = placement.get(field)
            if value is not None and (not isinstance(value, str) or not value):
                raise ContractError(f"order.json: `manager_placement.{field}` must be a non-empty string when present")
        if mode == "workspace" and not placement.get("workspace_id") and not placement.get("workspace_label"):
            raise ContractError("order.json: workspace manager placement needs `workspace_id` or `workspace_label`")
        if placement.get("workspace_id") and placement.get("workspace_label"):
            raise ContractError("order.json: manager placement may specify `workspace_id` or `workspace_label`, not both")
    management = order.get("management")
    if management is not None:
        if not isinstance(management, dict):
            raise ContractError("order.json: `management` must be an object when present")
        unknown_management = set(management) - {"mode"}
        if unknown_management:
            raise ContractError(
                "order.json: `management` supports only `mode`; unknown: "
                + ", ".join(sorted(unknown_management))
            )
        if management.get("mode") not in MANAGEMENT_MODES:
            raise ContractError(
                "order.json: `management.mode` must be one of " + ", ".join(MANAGEMENT_MODES)
            )
    operation = order.get("operation", "plan")
    if operation not in OPERATIONS:
        raise ContractError("order.json: `operation` must be one of " + ", ".join(OPERATIONS))
    if "requires_apply" in order and not isinstance(order["requires_apply"], bool):
        raise ContractError("order.json: `requires_apply` must be a boolean when present")
    mode = order.get("mode", "phase")
    if mode not in ("phase", "direct"):
        raise ContractError("order.json: `mode` must be `phase` or `direct`")
    if mode == "direct":
        for field in ("plan_id", "step_id"):
            if not isinstance(order.get(field), str) or not order[field]:
                raise ContractError(f"order.json: direct orders require a non-empty `{field}`")
    expected_watchdog_kind = WATCHDOG_KINDS.get(order["agent"])
    if expected_watchdog_kind is not None:
        terminal = order.get("watchdog_terminal")
        if not isinstance(terminal, dict):
            raise ContractError(
                "order.json: watchdog orders require a `watchdog_terminal` object"
            )
        kind = terminal.get("kind")
        if kind != expected_watchdog_kind:
            raise ContractError(
                "order.json: `watchdog_terminal.kind` must be "
                f"{expected_watchdog_kind!r} for agent {order['agent']!r}"
            )
        path = terminal.get("path")
        if not isinstance(path, str) or not path or not Path(path).is_absolute():
            raise ContractError(
                "order.json: `watchdog_terminal.path` must be an absolute path"
            )
        identity = terminal.get("identity")
        if not isinstance(identity, str) or not identity:
            raise ContractError(
                "order.json: `watchdog_terminal.identity` must be a non-empty string"
            )
        expected_identity = (
            order.get("plan_id") if kind == "plan" else order.get("phase_id")
        )
        if identity != expected_identity:
            raise ContractError(
                "order.json: `watchdog_terminal.identity` must match "
                f"{('plan_id' if kind == 'plan' else 'phase_id')}"
            )
    if operation == "apply":
        approval, artifact = order.get("approval"), order.get("plan_artifact")
        if not isinstance(approval, dict):
            raise ContractError("order.json: apply operation requires an `approval` object")
        if not isinstance(artifact, dict):
            raise ContractError("order.json: apply operation requires a `plan_artifact` object")
        for field in ("approved_by", "approved_at", "nonce", "plan_sha256"):
            if not isinstance(approval.get(field), str) or not approval[field]:
                raise ContractError(f"order.json: `approval.{field}` must be a non-empty string")
        for field in ("path", "sha256"):
            if not isinstance(artifact.get(field), str) or not artifact[field]:
                raise ContractError(f"order.json: `plan_artifact.{field}` must be a non-empty string")
        if approval["plan_sha256"] != artifact["sha256"]:
            raise ContractError("order.json: approval.plan_sha256 must match plan_artifact.sha256")
    return order


def parse_result(text: str, expected_order_id: str | None = None) -> dict:
    """Validate + return a result dict from raw JSON text. Fail-loud on any problem.

    When `expected_order_id` is given, the result's order_id MUST match it — this catches a
    stale result.json left over from a prior try at the same path.
    """
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise ContractError(f"result.json is not valid JSON: {e}") from e
    if not isinstance(result, dict):
        raise ContractError("result.json must be a JSON object")

    for key in RESULT_REQUIRED:
        _require_str(result, key, "result.json")

    if result["verdict"] not in VERDICTS:
        raise ContractError(f"result.json: verdict {result['verdict']!r} must be one of {', '.join(VERDICTS)}")
    if expected_order_id is not None and result["order_id"] != expected_order_id:
        raise ContractError(
            f"result.json: order_id {result['order_id']!r} does not match the order "
            f"{expected_order_id!r} — stale or mismatched result file"
        )

    # `revisit` (optional) must be a list of recognized stage ids. Required to be non-empty
    # when the verdict is `failed` (a failure with nowhere to go back to is a contract bug).
    revisit = result.get("revisit", [])
    if not isinstance(revisit, list) or not all(isinstance(s, str) for s in revisit):
        raise ContractError("result.json: `revisit` must be a list of stage-id strings")
    for stage in revisit:
        if stage not in RECOGNIZED_STAGE_IDS:
            raise ContractError(f"result.json: revisit stage {stage!r} is not a recognized stage id")
    if result["verdict"] == "failed" and not revisit:
        raise ContractError("result.json: a `failed` verdict must name at least one stage to `revisit`")

    # `consults` (optional) is the worker's own record of the specialists it asked. Shape only:
    # this module is pure and has no ledger access, so it can say a claim is WELL FORMED but
    # never that it is TRUE. The Recruiter resolves each claim against its own consult index at
    # publication and stamps `consults_verified` / `consults_unverified` on the receipt.
    consults = result.get("consults")
    if consults is not None:
        if not isinstance(consults, list):
            raise ContractError("result.json: `consults` must be a list when present")
        for claim in consults:
            if not isinstance(claim, dict):
                raise ContractError(f"result.json: every `consults` entry must be an object: {claim!r}")
            for field in CONSULT_CLAIM_REQUIRED:
                if not isinstance(claim.get(field), str) or not claim[field]:
                    raise ContractError(
                        f"result.json: `consults` entry needs a non-empty `{field}` "
                        f"(got {claim.get(field)!r}); the phone book in the brief names all four"
                    )

    # Optional advisor ruling. Present only on an advisor order's result; must be recognized.
    decision = result.get("decision")
    if decision is not None and decision not in ADVISOR_DECISIONS:
        raise ContractError(f"result.json: decision {decision!r} must be one of {', '.join(ADVISOR_DECISIONS)}")
    return result


def load_order(path: str | Path) -> dict:
    """Read + validate an order.json file. Fail-loud if missing or malformed."""
    p = Path(path)
    if not p.is_file():
        raise ContractError(f"order.json not found: {p}")
    return parse_order(p.read_text())


def load_result(path: str | Path, expected_order_id: str | None = None) -> dict:
    """Read + validate a result.json file. Fail-loud if missing or malformed."""
    p = Path(path)
    if not p.is_file():
        raise ContractError(f"result.json not found: {p}")
    return parse_result(p.read_text(), expected_order_id)


# ---------------------------------------------------------------------------
# Coordination v2 contracts — typed lifecycle events, owner commands, and acks.
# ---------------------------------------------------------------------------

COORDINATION_SCHEMA_VERSION = 1

EVENT_KINDS: dict[str, bool] = {
    "startup-ready": False,
    "startup-degraded": False,
    "progress": False,
    "advisory": False,
    "needs-input": False,
    # A blocked phase ends its attempt: the owner decides, then replays as a fresh pass.
    "blocked": True,
    "worker-warning": False,
    "worker-missing": False,
    "leader-missing": False,
    "leader-stalled": False,
    "inactivity-checkpoint": False,
    "decision-required": False,
    "soft-timeout": False,
    "hard-timeout": True,
    "completed": True,
    "failed": True,
    "cancelled": True,
    "await-heartbeat": False,
}
EVENT_SEVERITIES = ("info", "attention", "urgent")
COMMAND_ACTIONS = (
    "continue",
    "provide-input",
    "inspect",
    "extend-soft-timeout",
    "retry-startup",
    "cancel",
    "acknowledge-only",
)
ACK_STATES = ("published", "returned-to-owner", "acknowledged", "resolved")
EVENT_REQUIRED = ("event_id", "run_id", "kind", "summary", "occurred_at")


def parse_event(
    text: str,
    *,
    expected_run_id: str | None = None,
    expected_phase_id: str | None = None,
) -> dict:
    try:
        event = json.loads(text)
    except json.JSONDecodeError as e:
        raise ContractError(f"event: not valid JSON: {e}") from e
    if not isinstance(event, dict):
        raise ContractError("event: must be a JSON object")
    version = event.get("schema_version")
    if version != COORDINATION_SCHEMA_VERSION:
        raise ContractError(
            f"event: schema_version must be {COORDINATION_SCHEMA_VERSION} (got {version!r})"
        )
    for key in EVENT_REQUIRED:
        _require_str(event, key, "event")
    kind = event["kind"]
    if kind not in EVENT_KINDS:
        raise ContractError(
            f"event: unknown kind {kind!r}; expected one of {', '.join(sorted(EVENT_KINDS))}"
        )
    terminal = event.get("terminal")
    if not isinstance(terminal, bool):
        raise ContractError("event: `terminal` must be a boolean")
    if terminal != EVENT_KINDS[kind]:
        raise ContractError(
            f"event: kind {kind!r} must have terminal={EVENT_KINDS[kind]} (got {terminal})"
        )
    sequence = event.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ContractError("event: `sequence` must be a positive integer")
    severity = event.get("severity", "info")
    if severity not in EVENT_SEVERITIES:
        raise ContractError(
            f"event: severity {severity!r} must be one of {', '.join(EVENT_SEVERITIES)}"
        )
    if expected_run_id is not None and event["run_id"] != expected_run_id:
        raise ContractError(
            f"event: run_id {event['run_id']!r} does not match expected {expected_run_id!r}"
        )
    phase_id = event.get("phase_id")
    if phase_id is not None and (not isinstance(phase_id, str) or not phase_id):
        raise ContractError("event: `phase_id` must be a non-empty string when present")
    if expected_phase_id is not None and phase_id != expected_phase_id:
        raise ContractError(
            f"event: phase_id {phase_id!r} does not match expected {expected_phase_id!r}"
        )
    request_id = event.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ContractError("event: `request_id` must be a non-empty string when present")
    generation = event.get("generation")
    if generation is not None and (
        isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0
    ):
        raise ContractError("event: `generation` must be a positive integer when present")
    ack_required = event.get("ack_required", False)
    if not isinstance(ack_required, bool):
        raise ContractError("event: `ack_required` must be a boolean")
    evidence = event.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
        raise ContractError("event: `evidence` must be a list of objects")
    dedupe_key = event.get("dedupe_key")
    if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key):
        raise ContractError("event: `dedupe_key` must be a non-empty string when present")
    return event


def validate_event_order(previous: dict | None, current: dict) -> dict:
    if previous is None:
        return current
    if current["sequence"] <= previous["sequence"]:
        raise ContractError(
            f"event order: sequence {current['sequence']} must exceed {previous['sequence']}"
        )
    if previous.get("terminal"):
        prev_gen = previous.get("generation", 1)
        cur_gen = current.get("generation", 1)
        if cur_gen <= prev_gen:
            raise ContractError(
                "event order: terminal event "
                f"{previous['event_id']!r} (generation {prev_gen}) cannot be followed by "
                f"{current['event_id']!r} (generation {cur_gen})"
            )
    return current


def parse_phase_command(text: str, *, expected_run_id: str | None = None) -> dict:
    try:
        command = json.loads(text)
    except json.JSONDecodeError as e:
        raise ContractError(f"command: not valid JSON: {e}") from e
    if not isinstance(command, dict):
        raise ContractError("command: must be a JSON object")
    version = command.get("schema_version")
    if version != COORDINATION_SCHEMA_VERSION:
        raise ContractError(
            f"command: schema_version must be {COORDINATION_SCHEMA_VERSION} (got {version!r})"
        )
    for key in ("command_id", "run_id", "action", "issued_by"):
        _require_str(command, key, "command")
    if command["action"] not in COMMAND_ACTIONS:
        raise ContractError(
            f"command: action {command['action']!r} must be one of {', '.join(COMMAND_ACTIONS)}"
        )
    if expected_run_id is not None and command["run_id"] != expected_run_id:
        raise ContractError(
            f"command: run_id {command['run_id']!r} does not match expected {expected_run_id!r}"
        )
    in_response_to = command.get("in_response_to")
    if in_response_to is not None and (not isinstance(in_response_to, str) or not in_response_to):
        raise ContractError("command: `in_response_to` must be a non-empty string when present")
    detail = command.get("detail")
    if detail is not None and not isinstance(detail, dict):
        raise ContractError("command: `detail` must be an object when present")
    extension_ms = command.get("extension_ms")
    if command["action"] == "extend-soft-timeout":
        if isinstance(extension_ms, bool) or not isinstance(extension_ms, int) or extension_ms <= 0:
            raise ContractError(
                "command: extend-soft-timeout requires a positive integer `extension_ms`"
            )
    return command


def parse_ack(text: str, *, expected_event_id: str | None = None) -> dict:
    try:
        ack = json.loads(text)
    except json.JSONDecodeError as e:
        raise ContractError(f"ack: not valid JSON: {e}") from e
    if not isinstance(ack, dict):
        raise ContractError("ack: must be a JSON object")
    for key in ("event_id", "state", "actor", "occurred_at"):
        _require_str(ack, key, "ack")
    if ack["state"] not in ACK_STATES:
        raise ContractError(
            f"ack: state {ack['state']!r} must be one of {', '.join(ACK_STATES)}"
        )
    if expected_event_id is not None and ack["event_id"] != expected_event_id:
        raise ContractError(
            f"ack: event_id {ack['event_id']!r} does not match expected {expected_event_id!r}"
        )
    return ack


def validate_ack_transition(previous_state: str | None, new_state: str) -> str:
    if new_state not in ACK_STATES:
        raise ContractError(f"ack: state {new_state!r} must be one of {', '.join(ACK_STATES)}")
    if previous_state is None:
        return new_state
    if previous_state not in ACK_STATES:
        raise ContractError(f"ack: unknown previous state {previous_state!r}")
    if ACK_STATES.index(new_state) < ACK_STATES.index(previous_state):
        raise ContractError(
            f"ack: state may not regress from {previous_state!r} to {new_state!r}"
        )
    return new_state
