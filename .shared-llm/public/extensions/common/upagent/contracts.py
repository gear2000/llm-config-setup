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

# Required keys on an order.json the leader writes.
ORDER_REQUIRED = (
    "order_id",       # unique per work order, e.g. "phase-0.stage-1-implementation.pass-1.try-1"
    "phase_id",       # e.g. "phase-0"
    "stage_id",       # one of RECOGNIZED_STAGE_IDS
    "harness",        # resolved from the route llm_profile: one of KNOWN_HARNESSES
    # NOTE: `model` is validated separately below — it is required present but MAY be empty
    # (claude resolves its model from --model on the harness side), so it is not in this
    # non-empty-required set.
    "agent",          # persona name from the route stage entry
    "cwd",            # absolute path the worker runs in (the phase worktree)
    "instructions_path",  # absolute path to the stage brief the worker reads
    "result_path",    # absolute path the worker MUST write result.json to
    "cockpit_pane",   # id of an existing pane IN the cockpit workspace to split the worker
                      # from (Herdr `pane split` takes a source pane, not a workspace label).
                      # /herdr-run creates the cockpit and threads this pane id down.
)

# Required keys on a result.json the worker writes.
RESULT_REQUIRED = (
    "order_id",       # MUST echo the order's order_id
    "verdict",        # one of VERDICTS
    "full_log",       # pointer to the harness transcript (absolute path or session id)
)


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
            f"order.json: unknown stage_id {order['stage_id']!r}; "
            f"expected one of {', '.join(RECOGNIZED_STAGE_IDS)}"
        )
    if order["harness"] not in KNOWN_HARNESSES:
        raise ContractError(
            f"order.json: unknown harness {order['harness']!r}; "
            f"expected one of {', '.join(KNOWN_HARNESSES)}"
        )
    # `model` is required present but may be empty (claude resolves it from --model on the
    # harness side); enforce the key exists and is a string.
    if not isinstance(order.get("model"), str):
        raise ContractError("order.json: `model` must be a string (may be empty)")
    # Optional `env` must be a flat str->str map when present (injected via `pane split --env`).
    env = order.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ContractError("order.json: `env` must be a map of string->string")
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
        raise ContractError(
            f"result.json: verdict {result['verdict']!r} must be one of {', '.join(VERDICTS)}"
        )
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
            raise ContractError(
                f"result.json: revisit stage {stage!r} is not a recognized stage id"
            )
    if result["verdict"] == "failed" and not revisit:
        raise ContractError(
            "result.json: a `failed` verdict must name at least one stage to `revisit`"
        )

    # Optional advisor ruling. Present only on an advisor order's result; must be recognized.
    decision = result.get("decision")
    if decision is not None and decision not in ADVISOR_DECISIONS:
        raise ContractError(
            f"result.json: decision {decision!r} must be one of {', '.join(ADVISOR_DECISIONS)}"
        )
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
