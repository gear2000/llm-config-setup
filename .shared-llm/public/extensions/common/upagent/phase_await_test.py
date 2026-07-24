# pyright: reportMissingImports=false
"""Unit tests for the deterministic phase-await loop. Pure stdlib — no Herdr needed.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/phase_await_test.py -q
"""

from __future__ import annotations

# Import the sibling module by path so the test runs from the repo root without packaging.
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_phase_await", Path(__file__).with_name("phase_await.py")
)
assert _spec and _spec.loader
phase_await = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phase_await)


def _receipt(tmp_path: Path, **over: object) -> Path:
    control = tmp_path / "sample-run" / "phases" / "phase-0" / "pass-1" / "control"
    control.mkdir(parents=True, exist_ok=True)
    receipt = {
        "state": "ready",
        "phase_id": "phase-0",
        "pass": 1,
        "leader_pane": "leader-pane",
    }
    receipt.update(over)
    path = control / "phase-start.json"
    path.write_text(json.dumps(receipt))
    return path


def _alive(pane: str) -> dict:
    return {"alive": True, "agent_status": "working"}


def _await(path: Path, **over: object) -> dict:
    kwargs: dict = {
        "timeout_ms": 2_000,
        "poll_ms": 10,
        "reconcile_ms": 60_000,
        "inactivity_ms": 0,
        "escalate_ms": 0,
        "probe": _alive,
        "notify": lambda title, body: True,
    }
    kwargs.update(over)
    return phase_await.await_event(path, **kwargs)


def test_receipt_must_be_ready_with_identity(tmp_path: Path) -> None:
    with pytest.raises(phase_await.AwaitError, match="ready"):
        phase_await.PhaseContext(_receipt(tmp_path, state="failed"))
    with pytest.raises(phase_await.AwaitError, match="leader_pane"):
        phase_await.PhaseContext(_receipt(tmp_path, leader_pane=""))
    ctx = phase_await.PhaseContext(_receipt(tmp_path))
    assert ctx.run_id == "sample-run"
    assert ctx.result_path.name == "phase-result.json"


def test_inbox_event_is_delivered_and_redelivered_until_acknowledged(
    tmp_path: Path,
) -> None:
    path = _receipt(tmp_path)
    ctx = phase_await.PhaseContext(path)
    ctx.inbox_dir.mkdir(parents=True)
    (ctx.inbox_dir / "q1.json").write_text(
        json.dumps({"kind": "needs-input", "summary": "which db?"})
    )

    first = _await(path)
    assert first["kind"] == "needs-input"
    # Not acknowledged: the same event replays even past its sequence cursor.
    second = _await(path, after=first["sequence"])
    assert second["event_id"] == first["event_id"]

    phase_await.record_ack(ctx, first["event_id"], "acknowledged", "owner")
    heartbeat = _await(path, after=first["sequence"], timeout_ms=100)
    assert heartbeat["kind"] == "await-heartbeat"


def test_malformed_inbox_envelope_is_quarantined_as_advisory(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    ctx = phase_await.PhaseContext(path)
    ctx.inbox_dir.mkdir(parents=True)
    (ctx.inbox_dir / "bad.json").write_text('{"kind": "vibes"}')

    event = _await(path)
    assert event["kind"] == "advisory"
    assert "bad.json" in event["summary"]
    assert (ctx.inbox_dir / "bad.rejected").is_file()


def test_passed_result_becomes_one_terminal_completed_event(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    ctx = phase_await.PhaseContext(path)
    ctx.result_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.result_path.write_text(json.dumps({"verdict": "passed", "pass": 1}))

    event = _await(path)
    assert event["kind"] == "completed"
    assert event["terminal"] is True

    phase_await.record_ack(ctx, event["event_id"], "acknowledged", "owner")
    with pytest.raises(phase_await.AwaitError, match="terminal"):
        _await(path, after=event["sequence"], timeout_ms=100)


def test_blocked_result_is_terminal_and_never_republishes(tmp_path: Path) -> None:
    """Regression: a dismissed blocked alert must not pop back up forever."""
    path = _receipt(tmp_path)
    ctx = phase_await.PhaseContext(path)
    ctx.result_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.result_path.write_text(json.dumps({"verdict": "blocked", "pass": 1}))

    event = _await(path)
    assert event["kind"] == "blocked"
    assert event["terminal"] is True

    # Even a fully resolved ack must not re-fire the alert.
    phase_await.record_ack(ctx, event["event_id"], "resolved", "owner")
    with pytest.raises(phase_await.AwaitError, match="terminal"):
        _await(path, after=event["sequence"], timeout_ms=300)
    events = phase_await.read_journal(ctx)
    assert [e["kind"] for e in events] == ["blocked"]


def test_result_for_another_pass_is_ignored(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    ctx = phase_await.PhaseContext(path)
    ctx.result_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.result_path.write_text(json.dumps({"verdict": "passed", "pass": 2}))

    event = _await(path, timeout_ms=100)
    assert event["kind"] == "await-heartbeat"


def test_dead_leader_is_confirmed_then_reported_urgent(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    event = _await(
        path,
        reconcile_ms=10,
        probe=lambda pane: {"alive": False, "agent_status": None},
    )
    assert event["kind"] == "leader-missing"
    assert event["severity"] == "urgent"


def test_stalled_leader_is_confirmed_then_reported_urgent(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    event = _await(
        path,
        reconcile_ms=10,
        probe=lambda pane: {"alive": True, "agent_status": "idle"},
    )
    assert event["kind"] == "leader-stalled"
    assert event["severity"] == "urgent"


def _old_urgent_event(ctx: Any) -> dict:
    event = {
        "schema_version": 1,
        "event_id": "evt-old",
        "sequence": 1,
        "occurred_at": "2020-01-01T00:00:00Z",
        "run_id": ctx.run_id,
        "phase_id": ctx.phase_id,
        "pass": ctx.pass_number,
        "kind": "leader-missing",
        "terminal": False,
        "severity": "urgent",
        "summary": "leader gone",
        "ack_required": True,
    }
    ctx.events_dir.mkdir(parents=True)
    (ctx.events_dir / "00000001.json").write_text(json.dumps(event))
    return event


def test_failed_human_notification_is_retried_next_sweep(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    ctx = phase_await.PhaseContext(path)
    _old_urgent_event(ctx)
    marker = ctx.acks_dir / "evt-old.notified"

    calls: list[str] = []

    def failing_notify(title: str, body: str) -> bool:
        calls.append(title)
        return False

    _await(path, escalate_ms=1, notify=failing_notify)
    assert calls, "urgent unacknowledged event must attempt a human notification"
    assert not marker.exists(), "a failed notification must stay unmarked for retry"

    _await(path, escalate_ms=1, notify=lambda title, body: True)
    assert marker.exists(), "a delivered notification is marked and not repeated"


def test_ack_states_cannot_move_backward(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    ctx = phase_await.PhaseContext(path)
    ctx.inbox_dir.mkdir(parents=True)
    (ctx.inbox_dir / "q1.json").write_text(
        json.dumps({"kind": "advisory", "summary": "note"})
    )
    event = _await(path)
    phase_await.record_ack(ctx, event["event_id"], "resolved", "owner")
    with pytest.raises(phase_await.AwaitError, match="regress"):
        phase_await.record_ack(ctx, event["event_id"], "acknowledged", "owner")
