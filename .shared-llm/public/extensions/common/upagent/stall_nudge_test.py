# pyright: reportMissingImports=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportArgumentType=false
"""Unit tests for the hub-owned stall-nudge decision logic.

Pure Python, no herdr: the backoff ladder (immediate, then 5 min, then 15 min),
the hard cap of three nudges before escalation, durable state round-tripping,
idempotency keys, and provider derivation for the cross-provider sentinel gate.
Delivery, fencing, and ledger integration are drilled in sentinel_test.py.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/stall_nudge_test.py -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_stall_nudge", Path(__file__).with_name("stall_nudge.py")
)
assert _spec and _spec.loader
stall_nudge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stall_nudge)


# --- decide: backoff ladder and cap --------------------------------------------


def test_first_confirmed_stall_nudges_immediately() -> None:
    assert stall_nudge.decide({"nudges": []}, now=1000.0) == "nudge"


def test_second_stall_inside_the_first_backoff_window_holds() -> None:
    state = {"nudges": [{"at": 1000.0, "delivered": True, "digest": "d1"}]}
    assert stall_nudge.decide(state, now=1000.0 + 60) == "hold"


def test_second_stall_after_the_first_backoff_window_nudges() -> None:
    state = {"nudges": [{"at": 1000.0, "delivered": True, "digest": "d1"}]}
    after = 1000.0 + stall_nudge.NUDGE_BACKOFF_SECONDS[1] + 1
    assert stall_nudge.decide(state, now=after) == "nudge"


def test_third_stall_uses_the_longer_second_backoff() -> None:
    state = {
        "nudges": [
            {"at": 1000.0, "delivered": True, "digest": "d1"},
            {"at": 2000.0, "delivered": True, "digest": "d2"},
        ]
    }
    inside = 2000.0 + stall_nudge.NUDGE_BACKOFF_SECONDS[1] + 1
    assert stall_nudge.decide(state, now=inside) == "hold"
    after = 2000.0 + stall_nudge.NUDGE_BACKOFF_SECONDS[2] + 1
    assert stall_nudge.decide(state, now=after) == "nudge"


def test_the_cap_exhausts_after_three_recorded_nudges() -> None:
    state = {
        "nudges": [
            {"at": 1000.0, "delivered": True, "digest": "d1"},
            {"at": 2000.0, "delivered": True, "digest": "d2"},
            {"at": 9000.0, "delivered": True, "digest": "d3"},
        ]
    }
    assert stall_nudge.decide(state, now=10**9) == "exhausted"


def test_a_failed_delivery_still_counts_toward_the_cap() -> None:
    state = {
        "nudges": [
            {"at": 1000.0, "delivered": False, "digest": "d1"},
            {"at": 2000.0, "delivered": False, "digest": "d2"},
            {"at": 9000.0, "delivered": False, "digest": "d3"},
        ]
    }
    assert stall_nudge.decide(state, now=10**9) == "exhausted"


# --- durable state -------------------------------------------------------------


def test_state_round_trips_through_the_durable_file(tmp_path: Path) -> None:
    path = tmp_path / "nudges.json"
    assert stall_nudge.load_state(path) == {"nudges": []}
    state = stall_nudge.load_state(path)
    stall_nudge.record_nudge(state, at=1234.5, digest="abc", delivered=False)
    stall_nudge.save_state(path, state)
    loaded = stall_nudge.load_state(path)
    assert loaded["nudges"] == [{"at": 1234.5, "digest": "abc", "delivered": False}]


def test_a_corrupt_state_file_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "nudges.json"
    path.write_text("{ not json")
    with pytest.raises(stall_nudge.StallNudgeError):
        stall_nudge.load_state(path)


def test_the_same_digest_never_records_a_second_nudge() -> None:
    state = {"nudges": []}
    stall_nudge.record_nudge(state, at=1.0, digest="same", delivered=False)
    with pytest.raises(stall_nudge.StallNudgeError):
        stall_nudge.record_nudge(state, at=2.0, digest="same", delivered=True)
    assert len(state["nudges"]) == 1


def test_mark_delivered_flips_exactly_the_matching_intent() -> None:
    state = {"nudges": []}
    stall_nudge.record_nudge(state, at=1.0, digest="d1", delivered=False)
    stall_nudge.mark_delivered(state, "d1")
    assert state["nudges"][0]["delivered"] is True
    with pytest.raises(stall_nudge.StallNudgeError):
        stall_nudge.mark_delivered(state, "missing")


# --- idempotency key -----------------------------------------------------------


def test_evidence_digest_is_stable_and_input_sensitive() -> None:
    a = stall_nudge.evidence_digest(generation=1, attempt=2, nudge_index=0)
    b = stall_nudge.evidence_digest(generation=1, attempt=2, nudge_index=0)
    c = stall_nudge.evidence_digest(generation=2, attempt=2, nudge_index=0)
    assert a == b
    assert a != c


# --- provider derivation -------------------------------------------------------


def test_provider_of_maps_the_approved_harnesses() -> None:
    assert stall_nudge.provider_of("claude", "some-model") == "anthropic"
    assert stall_nudge.provider_of("codex", "gpt-5.6") == "openai"
    assert stall_nudge.provider_of("pi", "claude-opus-5") == "anthropic"
    assert stall_nudge.provider_of("pi", "gpt-5.6-sol") == "openai"
    assert stall_nudge.provider_of("cursor", "mystery") == "unknown"
    assert stall_nudge.provider_of("pi", "mystery") == "unknown"


def test_structurally_malformed_nudge_records_fail_loud_at_load(
    tmp_path: Path,
) -> None:
    cases = [
        {"nudges": [{}]},
        {"nudges": [{"at": "yesterday", "digest": "d", "delivered": True}]},
        {"nudges": [{"at": 1.0, "digest": "", "delivered": True}]},
        {"nudges": [{"at": 1.0, "digest": "d", "delivered": "yes"}]},
        {"nudges": [], "escalated": "yes"},
        {"nudges": ["not-a-dict"]},
    ]
    for case in cases:
        path = tmp_path / "nudges.json"
        path.write_text(json.dumps(case))
        with pytest.raises(stall_nudge.StallNudgeError):
            stall_nudge.load_state(path)
