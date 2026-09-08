"""Hub-owned stall-nudge decision logic (pure, no herdr).

A provider-overload halt leaves a worker idle with its conversation intact; a single
literal "continue" resumes it. The Recruiter's wait loop calls into this module when a
Sentinel STALLED closeout survives Python's re-probe: `decide` applies the backoff
ladder and hard cap over durable state, `classify` supplies status-first verdicts,
`record_verdict` tracks idle episodes, and `record_nudge`/`mark_delivered` keep the
intent-before-delivery idempotency record, and `provider_of` derives the provider
identity used by the cross-provider sentinel gate. Delivery, generation/lease fencing,
and ledger events stay in recruiter.py — nothing here touches a pane.
"""

from __future__ import annotations

import hashlib
import json
import os
import warnings
from enum import Enum
from pathlib import Path

# The one allowed payload (nudge-only vocabulary): never instructions or task content.
NUDGE_PAYLOAD = "continue"
NUDGE_CAP = 3
# Seconds that must elapse after nudge N before nudge N+1 (index 0 = first nudge,
# eligible immediately on a confirmed stall).
NUDGE_BACKOFF_SECONDS = (0.0, 300.0, 900.0)

_PROVIDER_BY_HARNESS = {
    "claude": "anthropic",
    "claudex": "openai",
    "codex": "openai",
}
_MODEL_PREFIX_PROVIDERS = (
    ("openrouter/", "openrouter"),
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o", "openai"),
)


class StallNudgeError(ValueError):
    """An invalid pane status, malformed state, or idempotency violation."""


class PaneProbeWarning(RuntimeWarning):
    """The pane probe is uncertain; hold without declaring the pane gone."""


class PaneState(Enum):
    PRESENT = "present"
    GONE = "gone"
    UNKNOWN = "unknown"


class Verdict(Enum):
    FINISHED = "FINISHED"
    WORKING = "WORKING"
    NUDGE = "NUDGE"
    HOLD = "HOLD"
    GONE = "GONE"


def classify(
    status: str | None,
    terminal_valid: bool,
    pane_state: PaneState,
    completion_style: str,
) -> Verdict:
    """Evaluate owner-validated terminal evidence before liveness and status.

    Callers derive completion_style from the validated harness via offerings.py.
    terminal_valid means validated terminal evidence, never file existence.
    """
    if terminal_valid:
        return Verdict.FINISHED
    if pane_state == PaneState.GONE:
        return Verdict.GONE
    if pane_state == PaneState.UNKNOWN:
        warnings.warn(
            "pane probe is uncertain; holding", PaneProbeWarning, stacklevel=2
        )
        return Verdict.HOLD
    if pane_state != PaneState.PRESENT:
        raise StallNudgeError(f"unknown pane state {pane_state!r}")
    if completion_style != "interactive":
        return Verdict.HOLD
    if status == "working":
        return Verdict.WORKING
    if status == "blocked":
        return Verdict.HOLD
    if status in ("idle", "done"):
        return Verdict.NUDGE
    raise StallNudgeError(f"unknown interactive pane status {status!r}")


def record_verdict(state: dict, verdict: Verdict) -> None:
    """Apply an episode transition before decide; the caller persists the state.

    Only confirmed NUDGE and WORKING verdicts change episodes. last_verdict
    records the last of those verdicts, so HOLD cannot erase a recovery or reset
    a spent cap. Closed episodes retain their records until the next idle probe.
    """
    if not isinstance(verdict, Verdict):
        raise StallNudgeError(f"unknown nudge verdict {verdict!r}")
    if verdict not in (Verdict.NUDGE, Verdict.WORKING):
        return
    if verdict == Verdict.NUDGE and state["last_verdict"] in (
        None,
        Verdict.WORKING.value,
    ):
        state["episode"] += 1
        state["nudges"] = []
        state["escalated"] = False
    state["last_verdict"] = verdict.value


def decide(state: dict, now: float) -> str:
    """Classify the next action for a confirmed stall: nudge, hold, or exhausted."""
    nudges = state["nudges"]
    if len(nudges) >= NUDGE_CAP:
        return "exhausted"
    if nudges and now < nudges[-1]["at"] + NUDGE_BACKOFF_SECONDS[len(nudges)]:
        return "hold"
    return "nudge"


def load_state(path: Path) -> dict:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {
            "schema": 2,
            "episode": 0,
            "last_verdict": None,
            "nudges": [],
            "escalated": False,
        }
    except (OSError, UnicodeError) as error:
        raise StallNudgeError(f"cannot read nudge state at {path}: {error}") from error
    try:
        state = json.loads(raw)
    except ValueError as error:
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
    schema = state.get("schema", 1)
    if type(schema) is not int or schema not in (1, 2):
        raise StallNudgeError(f"unsupported nudge state schema at {path}: {schema!r}")
    if schema == 1:
        # Original files had no schema field. Their spent cap belongs to episode 1.
        state.update(schema=2, episode=1, last_verdict=Verdict.NUDGE.value)
        state.setdefault("escalated", False)
    if (
        type(state.get("episode")) is not int
        or state["episode"] < 0
        or "last_verdict" not in state
        or state["last_verdict"]
        not in (None, Verdict.NUDGE.value, Verdict.WORKING.value)
        or not isinstance(state.get("escalated"), bool)
    ):
        raise StallNudgeError(f"malformed nudge episode at {path}")
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def record_nudge(state: dict, *, at: float, digest: str, delivered: bool) -> None:
    # Existing callers record a confirmed stall without an explicit classification.
    if state.get("schema") == 2:
        record_verdict(state, Verdict.NUDGE)
    if any(item["digest"] == digest for item in state["nudges"]):
        raise StallNudgeError(f"nudge intent {digest} already recorded")
    state["nudges"].append({"at": at, "digest": digest, "delivered": delivered})


def mark_delivered(state: dict, digest: str) -> None:
    for item in state["nudges"]:
        if item["digest"] == digest:
            item["delivered"] = True
            return
    raise StallNudgeError(f"no recorded nudge intent {digest}")


def evidence_digest(
    *, generation: int, attempt: int, nudge_index: int, episode: int = 1
) -> str:
    """Identify an intent within an episode; legacy callers use episode 1."""
    payload = json.dumps(
        {
            "generation": generation,
            "attempt": attempt,
            "nudge_index": nudge_index,
            "episode": episode,
        },
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
