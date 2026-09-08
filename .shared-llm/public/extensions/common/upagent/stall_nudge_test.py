# pyright: reportMissingImports=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportArgumentType=false
"""Unit tests for the hub-owned stall-nudge decision logic.

Pure Python, no herdr: the backoff ladder (immediate, then 5 min, then 15 min),
the hard cap of three nudges before escalation, durable state round-tripping,
idempotency keys, status classification, idle episodes, schema migration, and
provider derivation for the cross-provider sentinel gate.
Delivery, fencing, and ledger integration are drilled in sentinel_test.py.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/stall_nudge_test.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import warnings
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_stall_nudge", Path(__file__).with_name("stall_nudge.py")
)
assert _spec and _spec.loader
stall_nudge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stall_nudge)

_offerings_spec = importlib.util.spec_from_file_location(
    "stall_nudge_test_offerings", Path(__file__).with_name("offerings.py")
)
assert _offerings_spec and _offerings_spec.loader
offerings = importlib.util.module_from_spec(_offerings_spec)
sys.modules[_offerings_spec.name] = offerings
_offerings_spec.loader.exec_module(offerings)


# --- classification order ------------------------------------------------------


@pytest.mark.parametrize(
    "status", ["working", "idle", "done", "blocked", None, "new-status"]
)
@pytest.mark.parametrize("pane_state", list(stall_nudge.PaneState))
@pytest.mark.parametrize("harness", list(offerings.COMPLETION_STYLES))
def test_valid_terminal_evidence_wins_over_every_other_signal(
    status: str | None, pane_state: stall_nudge.PaneState, harness: str
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", stall_nudge.PaneProbeWarning)
        assert (
            stall_nudge.classify(
                status, True, pane_state, offerings.completion_style(harness)
            )
            == stall_nudge.Verdict.FINISHED
        )


@pytest.mark.parametrize(
    "status", ["working", "idle", "done", "blocked", None, "new-status"]
)
@pytest.mark.parametrize("pane_state", list(stall_nudge.PaneState))
@pytest.mark.parametrize("harness", list(offerings.COMPLETION_STYLES))
def test_nonterminal_classification_checks_pane_then_style_then_status(
    status: str | None, pane_state: stall_nudge.PaneState, harness: str
) -> None:
    style = offerings.completion_style(harness)
    if pane_state == stall_nudge.PaneState.UNKNOWN:
        with pytest.warns(
            stall_nudge.PaneProbeWarning, match="pane probe is uncertain"
        ):
            assert (
                stall_nudge.classify(status, False, pane_state, style)
                == stall_nudge.Verdict.HOLD
            )
        return
    with warnings.catch_warnings():
        warnings.simplefilter("error", stall_nudge.PaneProbeWarning)
        if pane_state == stall_nudge.PaneState.GONE:
            expected = stall_nudge.Verdict.GONE
        elif harness == "codex":
            expected = stall_nudge.Verdict.HOLD
        elif status == "working":
            expected = stall_nudge.Verdict.WORKING
        elif status == "blocked":
            expected = stall_nudge.Verdict.HOLD
        elif status in ("idle", "done"):
            expected = stall_nudge.Verdict.NUDGE
        else:
            with pytest.raises(
                stall_nudge.StallNudgeError, match="unknown interactive pane status"
            ):
                stall_nudge.classify(status, False, pane_state, style)
            return
        assert stall_nudge.classify(status, False, pane_state, style) == expected


@pytest.mark.parametrize("harness", ["unknown", "", "Claude"])
def test_unknown_harness_fails_before_classification(harness: str) -> None:
    with pytest.raises(offerings.OfferingError, match="no declared completion style"):
        stall_nudge.classify(
            "idle",
            False,
            stall_nudge.PaneState.PRESENT,
            offerings.completion_style(harness),
        )


def test_invalid_pane_state_fails_loud() -> None:
    with pytest.raises(stall_nudge.StallNudgeError, match="unknown pane state"):
        stall_nudge.classify(
            "idle", False, "present", offerings.completion_style("claude")
        )


# --- idle episodes -------------------------------------------------------------


def test_idle_working_idle_opens_two_episodes_with_independent_caps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nudges.json"
    state = stall_nudge.load_state(path)
    style = offerings.completion_style("claude")
    stall_nudge.record_verdict(
        state, stall_nudge.classify("idle", False, stall_nudge.PaneState.PRESENT, style)
    )
    assert state["episode"] == 1
    for index, at in enumerate((0.0, 300.0, 1200.0)):
        assert stall_nudge.decide(state, now=at) == "nudge"
        digest = stall_nudge.evidence_digest(
            generation=1, attempt=1, nudge_index=index, episode=state["episode"]
        )
        stall_nudge.record_nudge(state, at=at, digest=digest, delivered=True)
    first_digest = state["nudges"][0]["digest"]
    state["escalated"] = True
    for status in ("idle", "done", "idle"):
        stall_nudge.record_verdict(
            state,
            stall_nudge.classify(status, False, stall_nudge.PaneState.PRESENT, style),
        )
        assert state["episode"] == 1
        assert state["escalated"] is True
        assert stall_nudge.decide(state, now=10**9) == "exhausted"
    stall_nudge.record_verdict(
        state,
        stall_nudge.classify("working", False, stall_nudge.PaneState.PRESENT, style),
    )
    assert state["episode"] == 1
    assert len(state["nudges"]) == 3
    assert state["last_verdict"] == "WORKING"
    closed = json.dumps(state, sort_keys=True)
    for _ in range(10):
        stall_nudge.record_verdict(state, stall_nudge.Verdict.WORKING)
        assert json.dumps(state, sort_keys=True) == closed
    stall_nudge.save_state(path, state)
    state = stall_nudge.load_state(path)
    stall_nudge.record_verdict(
        state, stall_nudge.classify("idle", False, stall_nudge.PaneState.PRESENT, style)
    )
    assert state == {
        "schema": 2,
        "episode": 2,
        "last_verdict": "NUDGE",
        "nudges": [],
        "escalated": False,
    }
    assert stall_nudge.decide(state, now=1201.0) == "nudge"
    assert (
        stall_nudge.evidence_digest(generation=1, attempt=1, nudge_index=0, episode=2)
        != first_digest
    )


def test_ten_working_probes_create_no_episode(tmp_path: Path) -> None:
    state = stall_nudge.load_state(tmp_path / "nudges.json")
    for _ in range(10):
        stall_nudge.record_verdict(
            state,
            stall_nudge.classify(
                "working",
                False,
                stall_nudge.PaneState.PRESENT,
                offerings.completion_style("pi"),
            ),
        )
        assert state == {
            "schema": 2,
            "episode": 0,
            "last_verdict": "WORKING",
            "nudges": [],
            "escalated": False,
        }
    stall_nudge.record_verdict(state, stall_nudge.Verdict.NUDGE)
    assert state["episode"] == 1


@pytest.mark.parametrize(
    "verdict",
    [
        stall_nudge.Verdict.HOLD,
        stall_nudge.Verdict.GONE,
        stall_nudge.Verdict.FINISHED,
    ],
)
@pytest.mark.parametrize(
    "previous", [None, stall_nudge.Verdict.WORKING, stall_nudge.Verdict.NUDGE]
)
def test_other_verdicts_preserve_episode_history(
    tmp_path: Path, verdict: stall_nudge.Verdict, previous: stall_nudge.Verdict | None
) -> None:
    state = stall_nudge.load_state(tmp_path / "nudges.json")
    if previous is not None:
        stall_nudge.record_verdict(state, previous)
    if previous == stall_nudge.Verdict.NUDGE:
        stall_nudge.record_nudge(state, at=1.0, digest="spent", delivered=False)
        state["escalated"] = True
    before = json.dumps(state, sort_keys=True)
    stall_nudge.record_verdict(state, verdict)
    assert json.dumps(state, sort_keys=True) == before
    stall_nudge.record_verdict(state, stall_nudge.Verdict.NUDGE)
    assert state["episode"] == 1
    assert len(state["nudges"]) == (1 if previous == stall_nudge.Verdict.NUDGE else 0)


def test_invalid_verdict_fails_without_changing_state(tmp_path: Path) -> None:
    state = stall_nudge.load_state(tmp_path / "nudges.json")
    before = state.copy()
    with pytest.raises(stall_nudge.StallNudgeError, match="unknown nudge verdict"):
        stall_nudge.record_verdict(state, "idle")
    assert state == before


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
    assert stall_nudge.load_state(path) == {
        "schema": 2,
        "episode": 0,
        "last_verdict": None,
        "nudges": [],
        "escalated": False,
    }
    state = stall_nudge.load_state(path)
    stall_nudge.record_nudge(state, at=1234.5, digest="abc", delivered=False)
    stall_nudge.save_state(path, state)
    loaded = stall_nudge.load_state(path)
    assert loaded == state
    assert loaded["episode"] == 1
    assert loaded["last_verdict"] == "NUDGE"
    assert loaded["nudges"] == [{"at": 1234.5, "digest": "abc", "delivered": False}]


@pytest.mark.parametrize("explicit_schema", [False, True])
@pytest.mark.parametrize("nudge_count", [0, 1, 3])
@pytest.mark.parametrize("escalated", [None, False, True])
def test_schema_one_migration_preserves_spent_nudges(
    tmp_path: Path, explicit_schema: bool, nudge_count: int, escalated: bool | None
) -> None:
    path = tmp_path / "nudges.json"
    legacy = {
        "nudges": [
            {
                "at": float(index * 1000),
                "digest": f"legacy-{index}",
                "delivered": index % 2 == 0,
            }
            for index in range(nudge_count)
        ]
    }
    if explicit_schema:
        legacy["schema"] = 1
    if escalated is not None:
        legacy["escalated"] = escalated
    path.write_text(json.dumps(legacy))
    state = stall_nudge.load_state(path)
    assert state == {
        "schema": 2,
        "episode": 1,
        "last_verdict": "NUDGE",
        "nudges": legacy["nudges"],
        "escalated": bool(escalated),
    }
    assert json.loads(path.read_text()) == legacy
    stall_nudge.record_verdict(state, stall_nudge.Verdict.NUDGE)
    assert state["episode"] == 1
    assert state["nudges"] == legacy["nudges"]
    assert stall_nudge.decide(state, now=10**9) == (
        "exhausted" if nudge_count == 3 else "nudge"
    )
    stall_nudge.save_state(path, state)
    assert stall_nudge.load_state(path) == state
    stall_nudge.record_verdict(state, stall_nudge.Verdict.WORKING)
    stall_nudge.record_verdict(state, stall_nudge.Verdict.NUDGE)
    assert state["episode"] == 2
    assert state["nudges"] == []
    assert state["escalated"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", 0),
        ("schema", 3),
        ("schema", True),
        ("schema", "2"),
        ("schema", 2.0),
        ("episode", -1),
        ("episode", True),
        ("episode", 1.0),
        ("episode", "1"),
        ("last_verdict", "idle"),
        ("last_verdict", []),
        ("escalated", "yes"),
    ],
)
def test_invalid_schema_two_fields_fail_loud(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "nudges.json"
    state = stall_nudge.load_state(path)
    state[field] = value
    path.write_text(json.dumps(state))
    with pytest.raises(stall_nudge.StallNudgeError):
        stall_nudge.load_state(path)


@pytest.mark.parametrize("field", ["episode", "last_verdict", "escalated", "nudges"])
def test_missing_schema_two_fields_fail_loud(tmp_path: Path, field: str) -> None:
    path = tmp_path / "nudges.json"
    state = stall_nudge.load_state(path)
    del state[field]
    path.write_text(json.dumps(state))
    with pytest.raises(stall_nudge.StallNudgeError):
        stall_nudge.load_state(path)


def test_a_directory_is_not_treated_as_missing_state(tmp_path: Path) -> None:
    with pytest.raises(stall_nudge.StallNudgeError, match="cannot read nudge state"):
        stall_nudge.load_state(tmp_path)


def test_a_corrupt_state_file_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "nudges.json"
    path.write_text("{ not json")
    with pytest.raises(stall_nudge.StallNudgeError):
        stall_nudge.load_state(path)


def test_invalid_text_encoding_is_a_typed_state_error(tmp_path: Path) -> None:
    path = tmp_path / "nudges.json"
    path.write_bytes(b"\xff")
    with pytest.raises(stall_nudge.StallNudgeError, match="cannot read nudge state"):
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
    assert a == stall_nudge.evidence_digest(
        generation=1, attempt=2, nudge_index=0, episode=1
    )
    for changed in ({"attempt": 3}, {"nudge_index": 1}, {"episode": 2}):
        inputs = {"generation": 1, "attempt": 2, "nudge_index": 0, "episode": 1}
        inputs.update(changed)
        assert a != stall_nudge.evidence_digest(**inputs)


# --- provider derivation -------------------------------------------------------


def test_provider_of_maps_the_approved_harnesses() -> None:
    assert stall_nudge.provider_of("claude", "some-model") == "anthropic"
    assert stall_nudge.provider_of("codex", "gpt-5.6") == "openai"
    assert stall_nudge.provider_of("pi", "claude-opus-5") == "anthropic"
    assert stall_nudge.provider_of("pi", "gpt-5.6-sol") == "openai"
    assert (
        stall_nudge.provider_of("pi", "openrouter/z-ai/glm-5.3-flash") == "openrouter"
    )
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
