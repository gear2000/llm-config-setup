"""Hub-owned stall-nudge decision logic (pure, no herdr).

A provider-overload halt leaves a worker idle with its conversation intact; a single
literal "continue" resumes it. The Recruiter's wait loop calls into this module when a
Sentinel STALLED closeout survives Python's re-probe: `decide` applies the backoff
ladder and hard cap over durable state, `record_nudge`/`mark_delivered` keep the
intent-before-delivery idempotency record, and `provider_of` derives the provider
identity used by the cross-provider sentinel gate. Delivery, generation/lease fencing,
and ledger events stay in recruiter.py — nothing here touches a pane.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# The one allowed payload (nudge-only vocabulary): never instructions or task content.
NUDGE_PAYLOAD = "continue"
NUDGE_CAP = 3
# Seconds that must elapse after nudge N before nudge N+1 (index 0 = first nudge,
# eligible immediately on a confirmed stall).
NUDGE_BACKOFF_SECONDS = (0.0, 300.0, 900.0)

_PROVIDER_BY_HARNESS = {"claude": "anthropic", "codex": "openai"}
_MODEL_PREFIX_PROVIDERS = (
    ("openrouter/", "openrouter"),
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o", "openai"),
)


class StallNudgeError(ValueError):
    """A malformed durable state file or an idempotency violation."""


def decide(state: dict, now: float) -> str:
    """Classify the next action for a confirmed stall: nudge, hold, or exhausted."""
    nudges = state["nudges"]
    if len(nudges) >= NUDGE_CAP:
        return "exhausted"
    if nudges and now < nudges[-1]["at"] + NUDGE_BACKOFF_SECONDS[len(nudges)]:
        return "hold"
    return "nudge"


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"nudges": []}
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise StallNudgeError(f"corrupt nudge state at {path}: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("nudges"), list):
        raise StallNudgeError(f"malformed nudge state at {path}")
    for item in state["nudges"]:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("at"), (int, float))
            and not isinstance(item.get("at"), bool)
            and isinstance(item.get("digest"), str)
            and item["digest"]
            and isinstance(item.get("delivered"), bool)
        ):
            raise StallNudgeError(f"malformed nudge record at {path}: {item!r}")
    if "escalated" in state and not isinstance(state["escalated"], bool):
        raise StallNudgeError(f"malformed escalated flag at {path}")
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def record_nudge(state: dict, *, at: float, digest: str, delivered: bool) -> None:
    if any(item["digest"] == digest for item in state["nudges"]):
        raise StallNudgeError(f"nudge intent {digest} already recorded")
    state["nudges"].append({"at": at, "digest": digest, "delivered": delivered})


def mark_delivered(state: dict, digest: str) -> None:
    for item in state["nudges"]:
        if item["digest"] == digest:
            item["delivered"] = True
            return
    raise StallNudgeError(f"no recorded nudge intent {digest}")


def evidence_digest(*, generation: int, attempt: int, nudge_index: int) -> str:
    payload = json.dumps(
        {"generation": generation, "attempt": attempt, "nudge_index": nudge_index},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def provider_of(harness: str, model: str) -> str:
    """Best-effort provider identity for the cross-provider sentinel gate."""
    fixed = _PROVIDER_BY_HARNESS.get(harness)
    if fixed is not None:
        return fixed
    lowered = model.lower()
    for prefix, provider in _MODEL_PREFIX_PROVIDERS:
        if lowered.startswith(prefix):
            return provider
    return "unknown"
