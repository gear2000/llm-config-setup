# pyright: reportMissingImports=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportArgumentType=false
"""Unit tests for the Phase 1 mechanical gates.

Four gates, each pinned here against the field failure it exists to stop:

- startup marker — the worker's first observable action is recorded in the ledger, and
  "accepted but no first action" becomes the distinct typed `never-started` terminal (F1:
  a worker accepted, pane spawned, zero observable activity ever);
- harness epilogue — every Python-authored blocked bundle carries the mechanical evidence
  of what the run left behind, so work-without-bundle can no longer read as empty (F2);
- verdict/artifact consistency — a `passed` result whose own artifact files hold the real
  findings is invalid and forces re-evaluation instead of publishing (F3);
- auto-retry-once — a `never-started` terminal with zero side effects is retried once at
  the recruiter layer, mechanically.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/mechanical_gates_test.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_recruiter_gates", Path(__file__).with_name("recruiter.py")
)
assert _spec and _spec.loader
recruiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recruiter)
completion = recruiter.completion
contracts = recruiter.contracts
ContractError = recruiter.ContractError
CompletionError = completion.CompletionError


@pytest.fixture(autouse=True)
def _herdr_owner_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        recruiter, "_herdr_owner_record", lambda: {"herdr_session": "default"}
    )


def _order(**over: object) -> dict:
    base: dict[str, object] = {
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "phase_id": "phase-0",
        "stage_id": "stage-1-implementation",
        "harness": "claude",
        "model": "some-model",
        "agent": "backend",
        "cwd": "/tmp/wt",
        "instructions_path": "/tmp/wt/instructions.md",
        "result_path": "/tmp/wt/result.json",
        "cockpit_pane": "1-1",
    }
    base.update(over)
    return base


def _git_repo(path: Path) -> None:
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Gates Test"],
    ):
        subprocess.run(argv, cwd=path, check=True, capture_output=True)


def _commit(path: Path, name: str) -> str:
    (path / name).write_text(name)
    subprocess.run(["git", "add", name], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", name], cwd=path, check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _claimed_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cwd: Path | None = None
) -> tuple[Any, dict, str, Any]:
    """One submitted, claimed order with its lease manifest, hub outside the worktree."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    worktree = cwd or (tmp_path / "wt")
    worktree.mkdir(exist_ok=True)
    instructions = worktree / "instructions.md"
    instructions.write_text("# Worker\n")
    order = _order(
        cwd=str(worktree),
        instructions_path=str(instructions),
        result_path=str(worktree / "result.json"),
    )
    key, _created = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "request_id": recruiter.lifecycle.request_identity(order),
            "runner_pid": -1,
            "runner_start_time": None,
        },
    )
    assert isinstance(token, str)
    manifest = completion.build_manifest(
        order, ledger.request_dir(key), token, recruiter.lifecycle.request_identity(order)
    )
    completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    return ledger, order, key, manifest


def _events(ledger: Any, key: str) -> list[str]:
    return [item["event"] for item in ledger.events(key)]


# --- Gate 1: startup marker + typed never-started -----------------------------


def test_a_worker_cannot_type_the_never_started_verdict_itself() -> None:
    """`never-started` is Python-synthesized vocabulary; the strict path rejects it."""
    text = json.dumps(
        {"order_id": "o-1", "verdict": "never-started", "full_log": "session"}
    )
    with pytest.raises(ContractError, match="verdict"):
        contracts.parse_result(text)
    parsed = contracts.parse_result(text, allow_synthesized=True)
    assert parsed["verdict"] == "never-started"


def test_the_watcher_records_the_first_observable_action_in_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _order_value, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"pane": {"agent_status": "working"}}},
    )
    abort = threading.Event()
    recruiter._watch_first_action(
        ledger,
        key,
        "worker-pane",
        (manifest.artifact("result").staging_path,),
        abort,
        threading.Event(),
        abort_on_deadline=True,
        herdr_session="default",
        attempt=1,
    )
    marker = recruiter._first_action_event(ledger, key)
    assert marker is not None
    assert marker["signal"] == "agent-status"
    assert marker["agent_status"] == "working"
    assert marker["attempt"] == 1
    assert not abort.is_set()


def test_the_watcher_types_a_proven_idle_deadline_as_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _order_value, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_DEADLINE_MS", 100)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_MIN_IDLE_PROBES", 2)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"pane": {"agent_status": "idle"}}},
    )
    monkeypatch.setattr(
        recruiter, "_pane_recent_output", lambda *args, **kwargs: "static prompt"
    )
    abort = threading.Event()
    recruiter._watch_first_action(
        ledger,
        key,
        "worker-pane",
        (manifest.artifact("result").staging_path,),
        abort,
        threading.Event(),
        abort_on_deadline=True,
        herdr_session="default",
        attempt=1,
    )
    assert recruiter._first_action_event(ledger, key) is None
    assert recruiter._never_started_deadline_fired(ledger, key, attempt=1)
    assert abort.is_set()


def test_the_first_action_deadline_is_clamped_inside_the_order_timeout() -> None:
    """Precedence: a valid short order (1-minute caps are allowed) must reach the
    never-started classification before its hard timeout fires, so the effective liftoff
    deadline is half the order cap and never more than the standard 5 minutes."""
    assert recruiter._first_action_deadline_ms(60_000) == 30_000
    assert recruiter._first_action_deadline_ms(1_800_000) == 300_000
    assert recruiter._first_action_deadline_ms(600_000) == 300_000


def test_the_watcher_honors_the_clamped_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _order_value, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_MIN_IDLE_PROBES", 2)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"pane": {"agent_status": "idle"}}},
    )
    monkeypatch.setattr(
        recruiter, "_pane_recent_output", lambda *args, **kwargs: "static prompt"
    )
    abort = threading.Event()
    started = time.monotonic()
    recruiter._watch_first_action(
        ledger,
        key,
        "worker-pane",
        (manifest.artifact("result").staging_path,),
        abort,
        threading.Event(),
        abort_on_deadline=True,
        herdr_session="default",
        attempt=1,
        deadline_ms=100,
    )
    # The module deadline stayed at 5 minutes; only the clamped per-order value applied.
    assert time.monotonic() - started < 5
    assert abort.is_set()
    events = [item for item in ledger.events(key) if item["event"] == "worker-never-started"]
    assert events and events[0]["deadline_ms"] == 100


def test_an_unobservable_pane_never_aborts_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe faults are not evidence: the deadline degrades instead of aborting."""
    ledger, _order_value, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_DEADLINE_MS", 60)

    def unreadable(*args: object, **kwargs: object) -> dict:
        raise recruiter.RecruiterError("herdr unavailable")

    monkeypatch.setattr(recruiter, "_herdr_json", unreadable)
    abort = threading.Event()
    recruiter._watch_first_action(
        ledger,
        key,
        "worker-pane",
        (manifest.artifact("result").staging_path,),
        abort,
        threading.Event(),
        abort_on_deadline=True,
        herdr_session="default",
        attempt=1,
    )
    assert not abort.is_set()
    assert not recruiter._never_started_deadline_fired(ledger, key, attempt=1)
    assert "worker-first-action-watch-degraded" in _events(ledger, key)


def test_the_interactive_wait_raises_the_typed_never_started_fault() -> None:
    abort = threading.Event()
    abort.set()
    with pytest.raises(recruiter.WorkerNeverStartedError):
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            threading.Event(),
            never_started_abort=abort,
            herdr_session="default",
        )


def test_fixture_f1_never_started_worker_lands_in_the_typed_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 replayed: accepted, pane spawned and healthy, zero observable activity ever.

    The reactor must not wake the do-nothing worker with a repair prompt, and the terminal
    must be the typed `never-started` — never the generic lifecycle-blocked bucket.
    """
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane",
        attempt=1,
    )

    def no_repair(*args: object, **kwargs: object) -> None:
        raise AssertionError("a never-started worker must not receive a repair prompt")

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", no_repair)
    python_blocked, salvage = recruiter._complete_typed_bundle(
        ledger,
        key,
        order,
        manifest,
        "worker-address",
        herdr_session="default",
        attempt=1,
    )
    assert python_blocked is True
    assert salvage is not None
    assert salvage["result"]["verdict"] == "never-started"
    assert salvage["finalize_kwargs"]["synthesis_path"] == "never-started"
    assert salvage["finalize_kwargs"]["confirmation"] == "unconfirmed"
    assert "completion-repair-skipped-never-started" in _events(ledger, key)
    assert "never-started-classified" in _events(ledger, key)
    staged = json.loads(manifest.artifact("result").staging_path.read_text())
    assert staged["verdict"] == "never-started"
    assert "never started" in staged["reason"]


def test_a_recorded_first_action_keeps_an_empty_miss_in_the_blocked_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is the discriminator: with it, the same empty evidence stays blocked."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    ledger._event(key, "worker-first-action", signal="agent-status")
    triaged = recruiter._salvage_or_blocked(
        ledger, key, order, manifest, "wait fault"
    )
    assert triaged["result"]["verdict"] == "blocked"
    assert "never-started-classified" not in _events(ledger, key)


def test_dirty_worktree_evidence_vetoes_the_never_started_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer repro: absent result, no commits, no first action, deadline proven — but an
    uncommitted worker-change.txt on disk. That is a side effect, so the auto-retryable
    `never-started` terminal must not be minted; the miss routes to the ordinary blocked
    path with the dirty evidence in tow."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git_repo(worktree)
    # Backdate the baseline commit so it can never read as this order's landed work.
    (worktree / "baseline.txt").write_text("baseline\n")
    subprocess.run(
        ["git", "add", "baseline.txt"], cwd=worktree, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=worktree,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
        },
    )
    ledger, order, key, manifest = _claimed_request(
        tmp_path, monkeypatch, cwd=worktree
    )
    (worktree / "worker-change.txt").write_text("uncommitted side effect\n")
    ledger._event(
        key, "worker-never-started", deadline_ms=300_000, worker_pane="worker-pane"
    )
    triaged = recruiter._salvage_or_blocked(ledger, key, order, manifest, "wait fault")
    assert triaged["result"]["verdict"] == "blocked"
    assert "never-started-classified" not in _events(ledger, key)
    evidence = triaged["finalize_kwargs"]["salvage_evidence"]
    assert evidence["dirty_worktree"]["dirty_path_count"] >= 1
    assert any(
        "worker-change.txt" in line
        for line in evidence["dirty_worktree"]["dirty_paths"]
    )
    assert triaged["result"]["epilogue"]["dirty_worktree"]["dirty_path_count"] >= 1


@pytest.mark.parametrize("kind", ["compacted", "handoff"])
def test_any_staged_artifact_file_vetoes_the_never_started_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Reviewer repro: absent result.json, deadline proven — but the worker staged a
    non-empty compacted.md/handoff.md. Staging ANY artifact is a side effect, so the
    auto-retryable `never-started` terminal must not be minted; the miss routes to the
    ordinary blocked path with the staged file named in the evidence."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    staged = manifest.artifact(kind).staging_path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("# Notes the worker wrote before it went quiet\n")
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane",
        attempt=1,
    )
    triaged = recruiter._salvage_or_blocked(
        ledger, key, order, manifest, "wait fault", attempt=1
    )
    assert triaged["result"]["verdict"] == "blocked"
    assert "never-started-classified" not in _events(ledger, key)
    evidence = triaged["finalize_kwargs"]["salvage_evidence"]
    assert [item["kind"] for item in evidence["staged_artifact_files"]] == [kind]
    assert [item["kind"] for item in triaged["result"]["epilogue"]["staged_artifacts"]] == [
        kind
    ]


def test_a_prior_attempts_deadline_proof_cannot_authorize_a_later_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline event is attempt-scoped: attempt 1's proof mints attempt 1's typed
    terminal but never attempt 2's — a retry whose watch observed nothing stays in the
    blocked bucket instead of inheriting stale proof."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane",
        attempt=1,
    )
    assert recruiter._never_started_deadline_fired(ledger, key, attempt=1)
    assert not recruiter._never_started_deadline_fired(ledger, key, attempt=2)
    triaged = recruiter._salvage_or_blocked(
        ledger, key, order, manifest, "wait fault", attempt=2
    )
    assert triaged["result"]["verdict"] == "blocked"
    assert "never-started-classified" not in _events(ledger, key)


def test_the_reconciler_scope_requires_the_deadline_to_be_the_latest_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconciler cannot know the attempt (`attempt=None`): there the proof holds
    only while the deadline event is the request's most recent startup-marker
    observation. A later attempt's marker — even a degraded watch — supersedes it."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane",
        attempt=1,
    )
    assert recruiter._never_started_deadline_fired(ledger, key, attempt=None)
    ledger._event(
        key,
        "worker-first-action-watch-degraded",
        reason="startup-activity probes could not observe the pane",
        idle_probes=0,
        worker_pane="worker-pane-2",
        attempt=2,
    )
    assert not recruiter._never_started_deadline_fired(ledger, key, attempt=None)
    triaged = recruiter._salvage_or_blocked(ledger, key, order, manifest, "wait fault")
    assert triaged["result"]["verdict"] == "blocked"
    assert "never-started-classified" not in _events(ledger, key)


def test_reconciler_rejects_a_prior_deadline_after_an_auto_retry_begins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempt 1's deadline cannot terminalize attempt 2 when the runner dies before
    attempt 2's watcher emits any startup marker."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane-1",
        attempt=1,
    )
    ledger._event(key, "never-started-auto-retry", attempt=2)

    assert not recruiter._never_started_deadline_fired(ledger, key, attempt=None)
    triaged = recruiter._salvage_or_blocked(
        ledger, key, order, manifest, "runner reconciliation: runner died"
    )
    assert triaged["result"]["verdict"] == "blocked"
    assert "epilogue" in triaged["result"]
    assert "salvage_evidence" in triaged["finalize_kwargs"]
    assert "never-started-classified" not in _events(ledger, key)


def test_reconciler_accepts_the_current_attempts_own_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely current deadline remains sufficient proof for never-started."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    ledger._event(key, "never-started-auto-retry", attempt=2)
    ledger._event(
        key,
        "worker-never-started",
        deadline_ms=300_000,
        worker_pane="worker-pane-2",
        attempt=2,
    )

    assert recruiter._never_started_deadline_fired(ledger, key, attempt=None)
    triaged = recruiter._salvage_or_blocked(
        ledger, key, order, manifest, "runner reconciliation: runner died"
    )
    assert triaged["result"]["verdict"] == "never-started"
    assert triaged["finalize_kwargs"]["synthesis_path"] == "never-started"


def test_the_watcher_counts_any_staged_artifact_as_the_first_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that staged a handoff.md acted, even with result.json still absent and
    the pane idle: the watcher records the marker instead of the deadline verdict."""
    ledger, _order_value, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"pane": {"agent_status": "idle"}}},
    )
    handoff = manifest.artifact("handoff").staging_path
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# Started notes\n")
    abort = threading.Event()
    recruiter._watch_first_action(
        ledger,
        key,
        "worker-pane",
        tuple(item.staging_path for item in manifest.artifacts),
        abort,
        threading.Event(),
        abort_on_deadline=True,
        herdr_session="default",
        attempt=1,
    )
    marker = recruiter._first_action_event(ledger, key)
    assert marker is not None
    assert marker["signal"] == "staged-artifact"
    assert not abort.is_set()


def test_an_empty_miss_without_the_deadline_event_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`never-started` is deadline-proven vocabulary: with zero side effects but no
    recorded worker-never-started event, the miss stays in the blocked bucket instead of
    authorizing an auto-retry."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    triaged = recruiter._salvage_or_blocked(ledger, key, order, manifest, "wait fault")
    assert triaged["result"]["verdict"] == "blocked"
    assert "never-started-classified" not in _events(ledger, key)


# --- Gate 2: harness epilogue bundle ------------------------------------------


def test_fixture_f2_work_without_bundle_can_no_longer_produce_an_empty_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2 replayed: the worker committed real work, then stopped without its bundle.

    The harness — not the model — assembles the evidence into the blocked result: the
    landed commit, the touched files, and the artifact files the worker did write.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git_repo(worktree)
    ledger, order, key, manifest = _claimed_request(
        tmp_path, monkeypatch, cwd=worktree
    )
    sha = _commit(worktree, "fix-one.txt")
    (worktree / "abandoned-fix-two.txt").write_text("half done\n")
    handoff = manifest.artifact("handoff").staging_path
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# Worker notes before it stopped\n")

    result = recruiter._write_required_blocked_bundle(
        order, manifest, "worker stopped without finalizing"
    )

    epilogue = result["epilogue"]
    assert [item["sha"] for item in epilogue["landed_commits"]] == [sha]
    assert epilogue["dirty_worktree"]["dirty_path_count"] >= 1
    assert any(
        "abandoned-fix-two.txt" in line
        for line in epilogue["dirty_worktree"]["dirty_paths"]
    )
    assert [item["kind"] for item in epilogue["staged_artifacts"]] == ["handoff"]
    assert epilogue["git_worktree"] == str(worktree)
    on_disk = json.loads(manifest.artifact("result").staging_path.read_text())
    assert on_disk["verdict"] == "blocked"
    assert on_disk["epilogue"] == epilogue


def test_the_epilogue_stays_honest_outside_a_git_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    result = recruiter._write_required_blocked_bundle(order, manifest, "wait fault")
    epilogue = result["epilogue"]
    assert epilogue["git_worktree"] is None
    assert epilogue["staged_artifacts"] == []
    assert "landed_commits" not in epilogue


# --- Gate 3: verdict/artifact consistency -------------------------------------


def _staged_bundle(
    manifest: Any, result: dict, compacted: str = "", handoff: str = ""
) -> None:
    staging = manifest.artifact("result").staging_path
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(json.dumps(result))
    if compacted:
        manifest.artifact("compacted").staging_path.write_text(compacted)
    if handoff:
        manifest.artifact("handoff").staging_path.write_text(handoff)


FINDINGS_REPORT = (
    "# Findings report\n\n9 findings, 3 of them blockers. Details per finding follow.\n"
)


def test_fixture_f3_passed_with_empty_findings_and_a_full_report_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 replayed: `passed`, empty findings, while the artifact file holds the report."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "findings": [],
            "full_log": "worker-session",
        },
        compacted=FINDINGS_REPORT,
    )
    with pytest.raises(CompletionError, match="verdict/artifact consistency"):
        completion.validate_bundle(
            manifest,
            load_result=contracts.result_loader(order),
            load_answer=recruiter.contracts_consult.load_answer,
        )


def test_explicitly_empty_findings_stay_invalid_despite_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact field repro: `passed`, `findings: []`, and a substantive `reason` beside a
    full findings report. Prose in reason/summary/verdict_document must not excuse the
    explicitly-empty findings claim."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "findings": [],
            "reason": "all claims held",
            "full_log": "worker-session",
        },
        compacted=FINDINGS_REPORT,
    )
    with pytest.raises(CompletionError, match="explicitly empty"):
        completion.validate_bundle(
            manifest,
            load_result=contracts.result_loader(order),
            load_answer=recruiter.contracts_consult.load_answer,
        )


def test_a_result_without_a_findings_key_stays_valid_beside_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime calibration: ordinary workers never carry `findings` at all; their
    substantive `reason` beside non-empty artifacts is a legitimate shape, not F3."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "reason": "implemented and tested",
            "full_log": "worker-session",
        },
        compacted="# Handoff notes\n\nEverything landed; see commits.\n",
    )
    result = completion.validate_bundle(
        manifest,
        load_result=contracts.result_loader(order),
        load_answer=recruiter.contracts_consult.load_answer,
    )
    assert result["verdict"] == "passed"


def test_a_veered_report_beside_a_passed_verdict_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "reason": "all claims held",
            "full_log": "worker-session",
        },
        compacted="# Review\n\nThe work veered from the plan.\n\nVERDICT: VEERED",
    )
    with pytest.raises(CompletionError, match="VERDICT: VEERED"):
        completion.validate_bundle(
            manifest,
            load_result=contracts.result_loader(order),
            load_answer=recruiter.contracts_consult.load_answer,
        )


@pytest.mark.parametrize(
    "tail",
    [
        # Reviewer repros: arbitrary decoration around the phrase must not let a
        # veered report slip past the end-to-end consistency gate.
        "~~VERDICT: VEERED~~",
        "> **VERDICT: VEERED**",
        "VERDICT: VEERED)",
        "- VERDICT: VEERED -",
        "VERDICT: VEERED.",
        "Verdict: VEERED",
    ],
)
def test_a_decorated_veered_tail_cannot_evade_the_consistency_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "reason": "all claims held",
            "full_log": "worker-session",
        },
        compacted=f"# Review\n\nThe work veered from the plan.\n\n{tail}",
    )
    with pytest.raises(CompletionError, match="verdict/artifact consistency"):
        completion.validate_bundle(
            manifest,
            load_result=contracts.result_loader(order),
            load_answer=recruiter.contracts_consult.load_answer,
        )


def test_a_mid_document_mention_of_a_veered_verdict_is_not_a_contradiction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the report's LAST non-blank line states its outcome: prose that merely
    mentions a veered verdict mid-document must not trip the gate."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "reason": "all claims held",
            "full_log": "worker-session",
        },
        compacted=(
            "# Review\n\nA prior round returned VERDICT: VEERED on this work.\n\n"
            "Every finding from that round is now resolved.\n"
        ),
    )
    result = completion.validate_bundle(
        manifest,
        load_result=contracts.result_loader(order),
        load_answer=recruiter.contracts_consult.load_answer,
    )
    assert result["verdict"] == "passed"


def test_the_contradictory_tail_matcher_searches_through_any_decoration() -> None:
    assert completion.artifact_ends_with_contradictory_tail("VERDICT: VEERED")
    assert completion.artifact_ends_with_contradictory_tail("~~VERDICT: VEERED~~")
    assert completion.artifact_ends_with_contradictory_tail("> **VERDICT: VEERED**")
    assert completion.artifact_ends_with_contradictory_tail("VERDICT: VEERED)\n\n")
    assert completion.artifact_ends_with_contradictory_tail("verdict:VEERED")
    assert completion.artifact_ends_with_contradictory_tail("`Verdict :  veered`")
    assert completion.artifact_ends_with_contradictory_tail(
        "the VERDICT: VEERED case was fixed"
    )
    assert not completion.artifact_ends_with_contradictory_tail("VERDICT: CLEARED")
    assert not completion.artifact_ends_with_contradictory_tail("")


def test_a_passed_result_that_states_its_findings_remains_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "findings": ["one concrete finding, resolved"],
            "full_log": "worker-session",
        },
        compacted=FINDINGS_REPORT,
    )
    result = completion.validate_bundle(
        manifest,
        load_result=contracts.result_loader(order),
        load_answer=recruiter.contracts_consult.load_answer,
    )
    assert result["verdict"] == "passed"


def test_a_bare_passed_result_without_artifact_files_remains_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate needs both halves: no artifact files means nothing to contradict."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "findings": [],
            "full_log": "worker-session",
        },
    )
    result = completion.validate_bundle(
        manifest,
        load_result=contracts.result_loader(order),
        load_answer=recruiter.contracts_consult.load_answer,
    )
    assert result["verdict"] == "passed"


def test_the_consistency_gate_forces_re_evaluation_through_the_one_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reactor's one same-worker repair IS the forced re-evaluation."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "findings": [],
            "full_log": "worker-session",
        },
        compacted=FINDINGS_REPORT,
    )
    repairs: list[str] = []

    def repair(address: str, prompt: str, **kwargs: object) -> None:
        repairs.append(address)
        assert "verdict/artifact consistency" in prompt
        manifest.artifact("result").staging_path.write_text(
            json.dumps(
                {
                    "order_id": order["order_id"],
                    "verdict": "failed",
                    "revisit": ["stage-1-implementation"],
                    "findings": ["9 findings, 3 blockers — see compacted.md"],
                    "full_log": "worker-session",
                }
            )
        )

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", repair)
    python_blocked, salvage = recruiter._complete_typed_bundle(
        ledger,
        key,
        order,
        manifest,
        "worker-address",
        herdr_session="default",
    )
    assert repairs == ["worker-address"]
    assert python_blocked is False
    assert salvage is None
    staged = json.loads(manifest.artifact("result").staging_path.read_text())
    assert staged["verdict"] == "failed"


def test_an_unrepaired_inconsistent_passed_is_never_salvaged_as_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail loud, never silently accept: exhausted repair ends blocked, and the salvage
    inspection classifies the staged self-report `inconsistent`, not `valid`."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    _staged_bundle(
        manifest,
        {
            "order_id": order["order_id"],
            "verdict": "passed",
            "findings": [],
            "full_log": "worker-session",
        },
        compacted=FINDINGS_REPORT,
    )
    # The one repair reaches nobody (the worker is gone) and the ambiguity rescuer is
    # out of scope here; the identity passthrough keeps the mechanical outcome.
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            recruiter.RecruiterError("agent_not_found")
        ),
    )
    monkeypatch.setattr(
        recruiter,
        "_rescue_ambiguous_salvage",
        lambda ledger_arg, key_arg, order_arg, evidence: evidence,
    )
    python_blocked, salvage = recruiter._complete_typed_bundle(
        ledger,
        key,
        order,
        manifest,
        "worker-address",
        herdr_session="default",
    )
    assert python_blocked is True
    assert salvage is not None
    assert salvage["result"]["verdict"] == "blocked"
    assert (
        salvage["finalize_kwargs"]["salvage_evidence"]["staged_result_state"]
        == "inconsistent"
    )
    staged = json.loads(manifest.artifact("result").staging_path.read_text())
    assert staged["verdict"] == "blocked"


# --- Gate 4: auto-retry-once on never-started ---------------------------------


def _run_job_scaffolding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict, str, Path]:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    instructions = worktree / "instructions.md"
    instructions.write_text("# Worker\n")
    order = _order(
        cwd=str(worktree),
        instructions_path=str(instructions),
        result_path=str(worktree / "public" / "result.json"),
    )
    roster_path = tmp_path / "upagent.yaml"
    roster_path.write_text(
        'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    )
    ledger = recruiter.JobLedger()
    key, _created = ledger.submit(order)
    monkeypatch.setattr(
        recruiter,
        "inspect_worker_configuration",
        lambda *args, **kwargs: {"errors": []},
    )
    monkeypatch.setattr(
        recruiter,
        "_direct_manager",
        lambda *args, **kwargs: {
            "address": None,
            "config": args[0],
            "generation": 1,
            "health": None,
            "herdr_session": "default",
            "pane": None,
            "workspace_id": None,
        },
    )
    monkeypatch.setattr(recruiter, "_notify_requester", lambda *args, **kwargs: None)
    return ledger, order, key, roster_path


def _cleanup(worker_pane: str | None = "worker-pane") -> dict[str, object]:
    return {
        "status": "closed" if worker_pane else "not-created",
        "worker_pane": worker_pane,
        "verified_absent": True,
        "startup_validated": True,
        "startup_rejected": False,
    }


def test_a_never_started_terminal_auto_retries_exactly_once_and_can_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger, order, key, roster_path = _run_job_scaffolding(tmp_path, monkeypatch)
    attempts: list[int] = []

    def run_order(*args: object, **kwargs: object) -> tuple[int, dict, dict]:
        manifest = kwargs["artifact_manifest"]
        attempt = kwargs["attempt"]
        attempts.append(attempt)
        if attempt == 1:
            result = recruiter._write_never_started_bundle(
                order, manifest, "never started: nothing observed"
            )
            return 1, result, _cleanup()
        result = {
            "order_id": order["order_id"],
            "verdict": "passed",
            "reason": "second worker did the work",
            "full_log": "worker-session",
        }
        recruiter.JobLedger._write_json(
            manifest.artifact("result").staging_path, result
        )
        return 0, result, _cleanup()

    monkeypatch.setattr(recruiter, "_run_order", run_order)
    assert recruiter.cmd_run_job(key, str(roster_path)) == 0
    assert attempts == [1, 2]
    assert _events(ledger, key).count("never-started-auto-retry") == 1
    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "passed"
    published = json.loads(Path(order["result_path"]).read_text())
    assert published["verdict"] == "passed"
    # Attempt 1's never-started prose must not be published beside attempt 2's result.
    compacted = Path(order["artifact_publication"]["compacted_path"])
    assert not compacted.exists() or "Never started" not in compacted.read_text()


def test_the_auto_retry_carries_the_closeout_blocking_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Cold handoff into the mechanical retry: a blocking question recorded off attempt 1's
    Sentinel closeout reaches attempt 2's Python-composed brief."""
    ledger, order, key, roster_path = _run_job_scaffolding(tmp_path, monkeypatch)
    question = "which env file should the worker load?"
    seen: list[tuple[int, object]] = []

    def run_order(*args: object, **kwargs: object) -> tuple[int, dict, dict]:
        manifest = kwargs["artifact_manifest"]
        attempt = kwargs["attempt"]
        seen.append((attempt, kwargs.get("carried_blocking_question")))
        if attempt == 1:
            ledger._event(
                key,
                "sentinel-closeout",
                outcome="NEVER_STARTED",
                interpretation="no tool action within the liftoff deadline",
                corroborated_citations=[],
                uncorroborated_citations=[],
                blocking_question=question,
                exchanges=0,
                first_action_recorded=False,
            )
            result = recruiter._write_never_started_bundle(
                order, manifest, "never started: nothing observed"
            )
            return 1, result, _cleanup()
        result = {
            "order_id": order["order_id"],
            "verdict": "passed",
            "reason": "second worker did the work",
            "full_log": "worker-session",
        }
        recruiter.JobLedger._write_json(
            manifest.artifact("result").staging_path, result
        )
        return 0, result, _cleanup()

    monkeypatch.setattr(recruiter, "_run_order", run_order)
    assert recruiter.cmd_run_job(key, str(roster_path)) == 0
    assert seen == [(1, None), (2, question)]


def test_the_python_composed_brief_injects_the_carried_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    destination = tmp_path / "composed.md"
    recruiter._write_worker_instructions(
        order,
        manifest.artifact("result").staging_path,
        destination,
        manifest,
        carried_question="should the schema bump be major?",
    )
    text = destination.read_text()
    assert "# Open question from the previous attempt" in text
    assert "should the schema bump be major?" in text
    assert text.index("Open question") < text.index("Recruiter delivery contract")


def test_a_second_never_started_outcome_surfaces_typed_without_more_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger, order, key, roster_path = _run_job_scaffolding(tmp_path, monkeypatch)
    attempts: list[int] = []

    def run_order(*args: object, **kwargs: object) -> tuple[int, dict, dict]:
        manifest = kwargs["artifact_manifest"]
        attempts.append(kwargs["attempt"])
        result = recruiter._write_never_started_bundle(
            order, manifest, "never started: nothing observed"
        )
        return 1, result, _cleanup()

    monkeypatch.setattr(recruiter, "_run_order", run_order)
    assert recruiter.cmd_run_job(key, str(roster_path)) == 1
    assert attempts == [1, 2]
    assert _events(ledger, key).count("never-started-auto-retry") == 1
    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "never-started"
    assert receipt["synthesis_path"] == "never-started"
    assert receipt["confirmation"] == "unconfirmed"


def test_a_blocked_terminal_never_triggers_the_never_started_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger, order, key, roster_path = _run_job_scaffolding(tmp_path, monkeypatch)
    attempts: list[int] = []

    def run_order(*args: object, **kwargs: object) -> tuple[int, dict, dict]:
        manifest = kwargs["artifact_manifest"]
        attempts.append(kwargs["attempt"])
        result = recruiter._write_required_blocked_bundle(
            order, manifest, "worker ran and blocked"
        )
        return 1, result, _cleanup()

    monkeypatch.setattr(recruiter, "_run_order", run_order)
    assert recruiter.cmd_run_job(key, str(roster_path)) == 1
    assert attempts == [1]
    assert "never-started-auto-retry" not in _events(ledger, key)
    assert ledger.completed_receipt(key, order)["verdict"] == "blocked"
