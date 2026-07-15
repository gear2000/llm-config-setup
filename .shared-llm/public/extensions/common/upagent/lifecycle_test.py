from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

_spec = importlib.util.spec_from_file_location("upagent_lifecycle", Path(__file__).with_name("lifecycle.py"))
assert _spec and _spec.loader
lifecycle = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lifecycle
_spec.loader.exec_module(lifecycle)


def _order(**overrides: object) -> dict:
    value = {
        "order_id": "stage-1-try-1",
        "result_path": "/tmp/run-a/stage-1/result.json",
        "cockpit_pane": "w1:p2",
    }
    value.update(overrides)
    return value


def test_request_identity_scopes_human_order_id_by_result_destination() -> None:
    first = lifecycle.request_identity(_order())
    second = lifecycle.request_identity(_order(result_path="/tmp/run-b/stage-1/result.json"))

    assert first != second
    assert first.startswith("req-")
    assert lifecycle.request_identity(_order()) == first


def test_explicit_request_id_is_validated_and_preserved() -> None:
    assert lifecycle.request_identity(_order(request_id="req-client-123")) == "req-client-123"
    with pytest.raises(lifecycle.LifecycleError, match="request_id"):
        lifecycle.request_identity(_order(request_id="contains spaces"))


def test_requester_defaults_to_the_callers_cockpit_pane() -> None:
    assert lifecycle.requester_address(_order()) == lifecycle.RequesterAddress(
        requester_id="pane:w1:p2",
        kind="herdr-agent",
        address="w1:p2",
    )


def test_requester_accepts_a_generic_durable_address() -> None:
    address = lifecycle.requester_address(
        _order(requester={"id": "phase-leader-7", "kind": "file-mailbox", "address": "/tmp/inbox"})
    )
    assert address.requester_id == "phase-leader-7"
    assert address.kind == "file-mailbox"
    assert address.address == "/tmp/inbox"


def test_manager_decision_must_match_request_generation() -> None:
    valid = {
        "request_id": "req-abc",
        "generation": 2,
        "decision": "approved",
        "message": "Configuration is coherent.",
    }
    assert lifecycle.parse_manager_decision(json.dumps(valid), "req-abc", 2).decision == "approved"

    valid["generation"] = 1
    with pytest.raises(lifecycle.LifecycleError, match="generation"):
        lifecycle.parse_manager_decision(json.dumps(valid), "req-abc", 2)


def test_llm_assessment_is_advisory_and_structured() -> None:
    value = {
        "request_id": "req-abc",
        "generation": 2,
        "assessment": "suspected-stall",
        "confidence": 0.8,
        "evidence": ["no output activity", "process remains alive"],
        "recommended_action": "ask-requester",
        "message": "The worker may be waiting for input.",
    }
    parsed = lifecycle.parse_check_assessment(json.dumps(value), "req-abc", 2)
    assert parsed.assessment == "suspected-stall"
    assert parsed.recommended_action == "ask-requester"

    value["recommended_action"] = "kill-worker"
    with pytest.raises(lifecycle.LifecycleError, match="recommended_action"):
        lifecycle.parse_check_assessment(json.dumps(value), "req-abc", 2)


def test_requester_decision_is_scoped_and_bounded() -> None:
    value = {
        "request_id": "req-abc",
        "generation": 2,
        "action": "extend",
        "extension_ms": 3_600_000,
        "message": "The worker is making progress.",
    }
    parsed = lifecycle.parse_requester_decision(json.dumps(value), "req-abc", 2)
    assert parsed.action == "extend"
    assert parsed.extension_ms == 3_600_000

    value["extension_ms"] = lifecycle.MAX_EXTENSION_MS + 1
    with pytest.raises(lifecycle.LifecycleError, match="extension_ms"):
        lifecycle.parse_requester_decision(json.dumps(value), "req-abc", 2)

    value.update(action="cancel", extension_ms=None, generation=1)
    with pytest.raises(lifecycle.LifecycleError, match="generation"):
        lifecycle.parse_requester_decision(json.dumps(value), "req-abc", 2)


def test_mailbox_appends_immutable_correlated_messages(tmp_path: Path) -> None:
    mailbox = lifecycle.RequestMailbox(tmp_path)
    first = mailbox.publish("req-abc", 1, "worker-healthy", "Worker is ready.", {"pane_id": "w1:p3"})
    second = mailbox.publish("req-abc", 1, "result-ready", "Result is ready.")

    assert first != second
    messages = mailbox.read_all()
    assert [message["type"] for message in messages] == ["worker-healthy", "result-ready"]
    assert all(message["request_id"] == "req-abc" for message in messages)


def test_atomic_json_replaces_complete_document(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    lifecycle.write_json_atomic(path, {"state": "requested"})
    lifecycle.write_json_atomic(path, {"state": "running"})
    assert json.loads(path.read_text()) == {"state": "running"}
    assert not list(tmp_path.glob("*.tmp"))
