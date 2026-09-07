# pyright: reportMissingImports=false
"""Unit tests for the deterministic implementer-await loop. Pure stdlib — no Herdr needed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_implementer_await", Path(__file__).with_name("implementer_await.py")
)
assert _spec and _spec.loader
implementer_await = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(implementer_await)


def _receipt(tmp_path: Path, **over: object) -> Path:
    control = tmp_path / "sample-run" / "control"
    control.mkdir(parents=True, exist_ok=True)
    receipt = {
        "state": "ready",
        "phase_id": "plan",
        "pass": 1,
        "run_id": "sample-run",
        "implementer_pane": "implementer-pane",
    }
    receipt.update(over)
    path = control / "implementer-start.json"
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
    return implementer_await.await_event(path, **kwargs)


def test_receipt_must_be_ready_with_identity(tmp_path: Path) -> None:
    with pytest.raises(implementer_await.AwaitError, match="ready"):
        implementer_await.ImplementerContext(_receipt(tmp_path, state="failed"))
    with pytest.raises(implementer_await.AwaitError, match="implementer_pane"):
        implementer_await.ImplementerContext(_receipt(tmp_path, implementer_pane=""))
    ctx = implementer_await.ImplementerContext(_receipt(tmp_path))
    assert ctx.run_id == "sample-run"
    assert ctx.result_path.name == "implementer-result.json"


def test_needs_input_is_delivered_until_acknowledged(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    ctx = implementer_await.ImplementerContext(path)
    ctx.inbox_dir.mkdir(parents=True)
    (ctx.inbox_dir / "q1.json").write_text(
        json.dumps(
            {
                "kind": "needs-input",
                "summary": "which db?",
                "dedupe_key": "q-db",
            }
        )
    )

    first = _await(path)
    assert first["kind"] == "needs-input"
    assert first["dedupe_key"] == "q-db"
    replay = _await(path, after=first["sequence"])
    assert replay["event_id"] == first["event_id"]
    implementer_await.record_ack(ctx, first["event_id"], "acknowledged", "owner")


def test_result_file_promotes_completed(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    (tmp_path / "sample-run" / "implementer-result.json").write_text(
        json.dumps({"verdict": "passed"})
    )
    event = _await(path)
    assert event["kind"] == "completed"


def test_respond_and_wait_answer(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    answer = tmp_path / "human.md"
    answer.write_text("use postgres\n")
    written = implementer_await.write_answer(path, "q-db", answer)
    assert written["question_id"] == "q-db"
    got = implementer_await.await_answer(path, "q-db", timeout_ms=200, poll_ms=10)
    assert "use postgres" in got["answer"]


def test_question_id_rejects_path_separators(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    with pytest.raises(implementer_await.AwaitError, match="path-safe"):
        implementer_await.write_answer(path, "../escape", tmp_path / "x")


def test_needs_input_publish_requires_question_id(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    rc = implementer_await.main(
        [
            "publish",
            "--receipt",
            str(path),
            "--kind",
            "needs-input",
            "--summary",
            "which db?",
        ]
    )
    assert rc == 2


def test_publish_question_id_becomes_needs_input_dedupe_key(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    rc = implementer_await.main(
        [
            "publish",
            "--receipt",
            str(path),
            "--kind",
            "needs-input",
            "--summary",
            "which db?",
            "--question-id",
            "q-db",
        ]
    )
    assert rc == 0
    event = _await(path)
    assert event["kind"] == "needs-input"
    assert event["dedupe_key"] == "q-db"
