# pyright: reportMissingImports=false
"""Unit tests for the deterministic implementer-await loop. Pure stdlib — no Herdr needed."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

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
    run_root = tmp_path / "sample-run"
    (run_root / "implementer-result.json").write_text(
        json.dumps(
            {
                "verdict": "passed",
                "summary": "all slices landed",
                "run_root": str(run_root),
                "run_id": "sample-run",
            }
        )
    )
    event = _await(path)
    assert event["kind"] == "completed"


def test_stub_result_publishes_invalid_result_not_completed(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    (tmp_path / "sample-run" / "implementer-result.json").write_text(
        json.dumps({"verdict": "passed"})
    )
    event = _await(path)
    assert event["kind"] == "invalid-result"


def test_wrong_run_result_is_invalid(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    run_root = tmp_path / "sample-run"
    (run_root / "implementer-result.json").write_text(
        json.dumps(
            {
                "verdict": "passed",
                "summary": "leftover",
                "run_root": str(run_root),
                "run_id": "other-run",
            }
        )
    )
    event = _await(path)
    assert event["kind"] == "invalid-result"


def test_unreadable_result_publishes_invalid_result(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    (tmp_path / "sample-run" / "implementer-result.json").write_text("{not-json")
    event = _await(path)
    assert event["kind"] == "invalid-result"


def test_await_answer_times_out_without_writing_a_plan_result(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    ctx = implementer_await.ImplementerContext(path)
    with pytest.raises(implementer_await.AwaitError, match="timed out"):
        implementer_await.await_answer(path, "q-db", timeout_ms=40, poll_ms=10)
    assert not ctx.result_path.exists()


def test_await_answer_zero_timeout_waits_until_the_file_exists(tmp_path: Path) -> None:
    path = _receipt(tmp_path)
    answer = tmp_path / "human.md"
    answer.write_text("use postgres\n")
    implementer_await.write_answer(path, "q-db", answer)
    got = implementer_await.await_answer(path, "q-db", timeout_ms=0, poll_ms=10)
    assert "use postgres" in got["answer"]


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


def test_probe_leader_passes_the_receipt_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": {"pane": {"agent_status": "working"}}}),
        )

    monkeypatch.setattr(implementer_await.subprocess, "run", run)
    observed = implementer_await._probe_leader("pane-1", herdr_session="sess-9")
    assert observed == {"alive": True, "agent_status": "working"}
    assert seen == [["herdr", "--session", "sess-9", "pane", "get", "pane-1"]]


def _idle(pane: str) -> dict:
    return {"alive": True, "agent_status": "done"}


def _open_hire(
    tmp_path: Path,
    pane: str = "implementer-pane",
    *,
    runner_pid: int | None = None,
    runner_start_time: str | None = None,
    state: str | None = None,
    expires_at: int | None = None,
    worker_pane: str = "worker-pane-9",
) -> str:
    request_id = "hired-worker-1"
    key = "a" * 64
    ledger_root = tmp_path / "ledger"
    request_dir = ledger_root / "requests" / key
    request_dir.mkdir(parents=True)
    (request_dir / "request.json").write_text(
        json.dumps(
            {
                "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
                "cockpit_pane": pane,
                "request_id": request_id,
                "result_path": str(tmp_path / "missing-worker-result.json"),
            }
        )
    )
    lease: dict[str, object] = {
        "token": "tok",
        "expires_at": expires_at if expires_at is not None else int(time.time()) + 300,
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "worker_pane": worker_pane,
    }
    if runner_pid is not None:
        lease["runner_pid"] = runner_pid
        lease["runner_start_time"] = runner_start_time or "recorded-start"
    if runner_pid is not None or state is not None:
        lease_dir = ledger_root / "active" / "requests" / key
        lease_dir.mkdir(parents=True)
        (lease_dir / "lease.json").write_text(json.dumps(lease))
    if state is not None:
        (request_dir / "state").mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "state": state,
            "at_ns": 1,
            "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
            "worker_pane": worker_pane,
            **lease,
        }
        (request_dir / "state" / "latest.json").write_text(json.dumps(payload))
    return request_id


def test_idle_implementer_with_no_open_hire_publishes_leader_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    path = _receipt(tmp_path)
    event = _await(
        path,
        timeout_ms=400,
        poll_ms=10,
        reconcile_ms=30,
        probe=_idle,
    )
    assert event["kind"] == "leader-stalled"


def test_idle_implementer_waiting_on_open_hire_does_not_publish_leader_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    request_id = _open_hire(tmp_path)
    path = _receipt(tmp_path)
    event = _await(
        path,
        timeout_ms=400,
        poll_ms=10,
        reconcile_ms=30,
        probe=_idle,
    )
    assert event["kind"] != "leader-stalled"
    assert event["kind"] == "await-heartbeat"
    kinds = [
        json.loads(p.read_text())["kind"]
        for p in sorted((tmp_path / "sample-run" / "control" / "events").glob("*.json"))
    ]
    assert "leader-stalled" not in kinds
    waiting = implementer_await.open_hire_request_ids(
        implementer_await.ImplementerContext(path)
    )
    assert request_id in waiting


def test_idle_implementer_stalls_once_the_hire_has_result_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    _open_hire(tmp_path)
    (tmp_path / "missing-worker-result.json").write_text('{"verdict": "passed"}\n')
    path = _receipt(tmp_path)
    event = _await(
        path,
        timeout_ms=400,
        poll_ms=10,
        reconcile_ms=30,
        probe=_idle,
    )
    assert event["kind"] == "leader-stalled"
    assert implementer_await.open_hire_request_ids(
        implementer_await.ImplementerContext(path)
    ) == []


def test_await_emits_worker_missing_when_hire_runner_pid_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    _open_hire(
        tmp_path,
        runner_pid=999_999,
        runner_start_time="dead-start",
        state="running",
    )
    path = _receipt(tmp_path)
    event = _await(
        path,
        timeout_ms=400,
        poll_ms=10,
        reconcile_ms=30,
        probe=_alive,
    )
    assert event["kind"] == "worker-missing"
    assert event["request_id"] == "hired-worker-1"
    assert event["severity"] == "urgent"
    assert event["requested_action"] == "inspect-and-decide"
    assert "hired-worker-1" in event["summary"]
    assert "worker-pane-9" in event["summary"]
    assert "runner-pid-dead" in event["summary"]
    assert event["dedupe_key"] == "worker-missing:hired-worker-1"


def test_await_emits_worker_missing_when_awaiting_requester_deadline_lapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    _open_hire(
        tmp_path,
        state="awaiting-requester",
        expires_at=int(time.time()) - 30,
    )
    path = _receipt(tmp_path)
    event = _await(
        path,
        timeout_ms=400,
        poll_ms=10,
        reconcile_ms=30,
        probe=_alive,
    )
    assert event["kind"] == "worker-missing"
    assert event["request_id"] == "hired-worker-1"
    assert "awaiting-requester-expired" in event["summary"]
    assert "worker-pane-9" in event["summary"]
    assert event["dedupe_key"] == "worker-missing:hired-worker-1"
