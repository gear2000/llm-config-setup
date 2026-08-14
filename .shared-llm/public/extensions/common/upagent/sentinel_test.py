# pyright: reportMissingImports=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportArgumentType=false
"""Unit tests and induced-failure drills for the Phase 2 Sentinel.

One haiku pane per request, duty-bound to one worker; its typed closeout.json is an
additional teardown trigger the Recruiter's wait loop watches. Pinned here:

- the closed closeout contract (four outcomes, identity, citations, exchange cap);
- mechanical citation re-verification (the rescuer rule applied to the Sentinel);
- the wait-loop closeout handling for each outcome, including the invalid-COMPLETE
  reject-and-one-more-landing-round path and the fooled-Sentinel-never-passes guarantee;
- the liftoff address relay and the ledger-logged requester→worker message channel;
- induced-failure drills through the real `cmd_run_job` lifecycle: worker dead before
  first action, mid-work, after work-but-before-bundle, and a dead Sentinel (backstop) —
  each landing in its exact typed outcome with both panes reaped.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/sentinel_test.py -q
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_recruiter_sentinel", Path(__file__).with_name("recruiter.py")
)
assert _spec and _spec.loader
recruiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recruiter)
completion = recruiter.completion
contracts = recruiter.contracts
contracts_sentinel = recruiter.contracts_sentinel
ContractError = recruiter.ContractError
SentinelContractError = recruiter.SentinelContractError
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
        ["git", "config", "user.name", "Sentinel Test"],
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


def _events(ledger: Any, key: str) -> list[str]:
    return [item["event"] for item in ledger.events(key)]


def _event(ledger: Any, key: str, name: str) -> dict:
    matches = [item for item in ledger.events(key) if item["event"] == name]
    assert matches, f"no {name} event in {_events(ledger, key)}"
    return matches[-1]


def _closeout(order: dict, **over: object) -> dict:
    value: dict[str, object] = {
        "request_id": recruiter.lifecycle.request_identity(order),
        "order_id": order["order_id"],
        "outcome": "COMPLETE",
        "interpretation": "the worker finished and the bundle is verified on disk",
        "citations": [],
        "bundle": "/abs/bundle/result.json",
        "blocking_question": None,
        "exchanges": [],
    }
    value.update(over)
    return value


def _parse(order: dict, closeout: dict) -> dict:
    return contracts_sentinel.parse_closeout(
        json.dumps(closeout),
        recruiter.lifecycle.request_identity(order),
        order["order_id"],
    )


# --- Closeout contract ---------------------------------------------------------


def test_each_typed_outcome_parses() -> None:
    order = _order()
    for outcome, extra in (
        ("COMPLETE", {}),
        ("NEVER_STARTED", {"bundle": None}),
        (
            "STALLED",
            {
                "bundle": None,
                "progress_so_far": "fix 1 committed",
                "last_alive": "tool output 14:02",
            },
        ),
        ("FINALIZATION_FAILED", {"bundle": None}),
    ):
        parsed = _parse(order, _closeout(order, outcome=outcome, **extra))
        assert parsed["outcome"] == outcome


def test_an_unknown_outcome_is_rejected() -> None:
    order = _order()
    with pytest.raises(SentinelContractError, match="outcome"):
        _parse(order, _closeout(order, outcome="DONE"))


def test_a_mismatched_identity_is_rejected() -> None:
    order = _order()
    with pytest.raises(SentinelContractError, match="identity"):
        _parse(order, _closeout(order, order_id="somebody-else"))


def test_unknown_keys_are_rejected() -> None:
    order = _order()
    with pytest.raises(SentinelContractError, match="unknown keys"):
        _parse(order, _closeout(order, verdict="passed"))


def test_a_missing_interpretation_is_rejected() -> None:
    order = _order()
    with pytest.raises(SentinelContractError, match="interpretation"):
        _parse(order, _closeout(order, interpretation="  "))


def test_complete_without_a_bundle_is_rejected() -> None:
    order = _order()
    with pytest.raises(SentinelContractError, match="bundle"):
        _parse(order, _closeout(order, bundle=None))


def test_stalled_requires_progress_and_last_alive() -> None:
    order = _order()
    with pytest.raises(SentinelContractError, match="progress_so_far"):
        _parse(order, _closeout(order, outcome="STALLED", bundle=None))


def test_the_landing_exchange_cap_is_enforced() -> None:
    order = _order()
    exchange = {"question": "finished?", "answer": "yes", "verified": False}
    with pytest.raises(SentinelContractError, match="capped"):
        _parse(order, _closeout(order, exchanges=[exchange] * 4))
    parsed = _parse(order, _closeout(order, exchanges=[exchange] * 3))
    assert len(parsed["exchanges"]) == 3


def test_malformed_exchanges_and_citations_are_rejected() -> None:
    order = _order()
    with pytest.raises(SentinelContractError, match="exchange"):
        _parse(order, _closeout(order, exchanges=[{"question": "finished?"}]))
    with pytest.raises(SentinelContractError, match="citations"):
        _parse(order, _closeout(order, citations=["ok", ""]))


# --- Citation re-verification --------------------------------------------------


def test_citations_are_mechanically_corroborated(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git_repo(worktree)
    sha = _commit(worktree, "real-work.txt")
    real_file = worktree / "real-work.txt"

    corroborated, uncorroborated = contracts_sentinel.verify_citations(
        [
            sha,
            "f" * 40,
            str(real_file),
            str(worktree / "missing.txt"),
            "confident prose about a commit",
        ],
        file_exists=lambda path: Path(path).is_file(),
        commit_exists=lambda value: recruiter.commit_exists_in_worktree(
            str(worktree), value
        ),
        scope_roots=(worktree,),
    )

    assert corroborated == [sha, str(real_file)]
    assert uncorroborated == [
        "f" * 40,
        str(worktree / "missing.txt"),
        "confident prose about a commit",
    ]


def test_an_existing_path_outside_the_request_scope_never_corroborates(
    tmp_path: Path,
) -> None:
    """Reviewer repro: /etc/passwd exists on every machine — an existing path only
    corroborates inside the request's own territory, and the out-of-scope hit is
    recorded as exactly that."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    in_scope = worktree / "real-work.txt"
    in_scope.write_text("work\n")

    corroborated, uncorroborated = contracts_sentinel.verify_citations(
        ["/etc/passwd", str(in_scope)],
        file_exists=lambda path: Path(path).is_file(),
        commit_exists=lambda value: False,
        scope_roots=(worktree,),
    )

    assert corroborated == [str(in_scope)]
    assert uncorroborated == ["/etc/passwd (out-of-scope)"]


def test_without_scope_roots_no_path_citation_corroborates(tmp_path: Path) -> None:
    """No scope roots means no territory to check against: every path citation stays
    uncorroborated, however real the file is."""
    real_file = tmp_path / "real.txt"
    real_file.write_text("real\n")

    corroborated, uncorroborated = contracts_sentinel.verify_citations(
        [str(real_file)],
        file_exists=lambda path: Path(path).is_file(),
        commit_exists=lambda value: False,
        scope_roots=(),
    )

    assert corroborated == []
    assert uncorroborated == [f"{real_file} (out-of-scope)"]


def test_a_symlink_inside_scope_cannot_smuggle_an_outside_target(
    tmp_path: Path,
) -> None:
    """Scope containment happens on the RESOLVED path, so a link planted in the
    worktree pointing at an outside file stays uncorroborated."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    link = worktree / "innocent.txt"
    link.symlink_to(outside)

    corroborated, uncorroborated = contracts_sentinel.verify_citations(
        [str(link)],
        file_exists=lambda path: Path(path).is_file(),
        commit_exists=lambda value: False,
        scope_roots=(worktree,),
    )

    assert corroborated == []
    assert uncorroborated == [f"{link} (out-of-scope)"]


# --- Hiring policy -------------------------------------------------------------


def test_sentinel_supervision_is_default_on_with_a_typed_opt_out() -> None:
    assert recruiter._sentinel_enabled(_order())
    assert not recruiter._sentinel_enabled(_order(sentinel=False))
    assert recruiter._sentinel_enabled(_order(sentinel=True))
    assert not recruiter._sentinel_enabled(
        _order(agent=next(iter(recruiter.WATCHDOG_AGENTS)))
    )
    assert not recruiter._sentinel_enabled(
        _order(completion_policy="requester_release")
    )


def test_parse_order_types_the_sentinel_flag() -> None:
    assert contracts.parse_order(json.dumps(_order(sentinel=False)))["sentinel"] is False
    with pytest.raises(ContractError, match="sentinel"):
        contracts.parse_order(json.dumps(_order(sentinel="no")))


def test_each_attempt_gets_its_own_closeout_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(_order())
    first = recruiter._sentinel_closeout_path(ledger, key, 1)
    second = recruiter._sentinel_closeout_path(ledger, key, 2)
    assert first != second
    assert first.name == "closeout.json"


# --- Liftoff address relay ------------------------------------------------------


def test_the_first_action_watcher_relays_the_worker_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _order_value, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "FIRST_ACTION_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {"result": {"pane": {"agent_status": "working"}}},
    )
    relayed: list[dict] = []
    recruiter._watch_first_action(
        ledger,
        key,
        "worker-pane",
        (manifest.artifact("result").staging_path,),
        threading.Event(),
        threading.Event(),
        abort_on_deadline=True,
        herdr_session="default",
        attempt=1,
        on_first_action=relayed.append,
    )
    assert relayed and relayed[0]["worker_pane"] == "worker-pane"
    assert recruiter._first_action_event(ledger, key) is not None


# --- Closeout watch (poll) ------------------------------------------------------


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
        order,
        ledger.request_dir(key),
        token,
        recruiter.lifecycle.request_identity(order),
    )
    completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    return ledger, order, key, manifest


def _watch(
    ledger: Any,
    order: dict,
    key: str,
    manifest: Any,
    *,
    address: str | None = "sentinel-address",
) -> Any:
    closeout_path = recruiter._sentinel_closeout_path(ledger, key, 1)
    closeout_path.parent.mkdir(parents=True, exist_ok=True)
    return recruiter._SentinelWatch(
        ledger,
        key,
        order,
        manifest,
        closeout_path,
        {"address": address} if address is not None else {},
        herdr_session="default",
    )


def _stage_valid_result(manifest: Any, order: dict) -> None:
    staging = manifest.artifact("result").staging_path
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(
        json.dumps(
            {
                "order_id": order["order_id"],
                "verdict": "passed",
                "reason": "did the work",
                "full_log": "worker-session",
            }
        )
    )


def test_poll_waits_on_an_absent_closeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    assert _watch(ledger, order, key, manifest).poll() is None


def test_poll_reports_a_malformed_closeout_once_and_keeps_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text("{ not json")
    assert watch.poll() is None
    assert watch.poll() is None
    events = _events(ledger, key)
    assert events.count("sentinel-closeout-invalid") == 1


def test_a_never_started_closeout_raises_the_typed_liftoff_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="NEVER_STARTED",
                bundle=None,
                interpretation="five minutes of banner and no tool action",
            )
        )
    )
    with pytest.raises(recruiter.WorkerNeverStartedError, match="NEVER_STARTED"):
        watch.poll()
    assert _event(ledger, key, "sentinel-closeout")["outcome"] == "NEVER_STARTED"


def test_a_stalled_closeout_raises_with_progress_and_last_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero citations survived verification, so every Sentinel-authored fragment in the
    wait-ending fault — the prose the published blocked reason is built from — must be
    explicitly marked uncorroborated, with Python's epilogue named as the authority."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="STALLED",
                bundle=None,
                progress_so_far="fix 1 committed and verified",
                last_alive="tool output at pulse 3",
            )
        )
    )
    with pytest.raises(recruiter.SentinelStalledError) as caught:
        watch.poll()
    message = str(caught.value)
    assert "no sentinel citation survived Python verification" in message
    assert "sentinel interpretation (uncorroborated):" in message
    assert "progress_so_far (uncorroborated): fix 1 committed and verified" in message
    assert "last_alive (uncorroborated): tool output at pulse 3" in message


def test_a_corroborated_citation_leads_the_published_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a citation DOES survive Python verification it leads the reason as checked
    fact — but the Sentinel's prose is LLM-authored and never mechanically checkable,
    so it keeps its uncorroborated marker regardless: a verified citation must never
    launder the interpretation around it."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    real_file = Path(order["cwd"]) / "half-done.txt"
    real_file.write_text("partial\n")
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="STALLED",
                bundle=None,
                citations=[str(real_file)],
                progress_so_far="fix 1 written to half-done.txt",
                last_alive="tool output at pulse 2",
            )
        )
    )
    with pytest.raises(recruiter.SentinelStalledError) as caught:
        watch.poll()
    message = str(caught.value)
    assert f"Python-verified citations: {real_file}" in message
    assert "sentinel interpretation (uncorroborated):" in message
    assert "progress_so_far (uncorroborated): fix 1 written to half-done.txt" in message
    assert "last_alive (uncorroborated): tool output at pulse 2" in message


def test_an_out_of_scope_citation_never_corroborates_in_the_published_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer repro end to end: a Sentinel citing /etc/passwd must not make its prose
    look checked — the existing-but-out-of-scope path is discarded with its marker."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="STALLED",
                bundle=None,
                citations=["/etc/passwd"],
                progress_so_far="everything is fine",
                last_alive="just now",
            )
        )
    )
    with pytest.raises(recruiter.SentinelStalledError) as caught:
        watch.poll()
    message = str(caught.value)
    assert "no sentinel citation survived Python verification" in message
    assert "/etc/passwd (out-of-scope)" in message
    assert "sentinel interpretation (uncorroborated):" in message


def test_a_finalization_failed_closeout_raises_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="FINALIZATION_FAILED",
                bundle=None,
                exchanges=[
                    {"question": "finished?", "answer": "yes", "verified": False}
                ]
                * 3,
            )
        )
    )
    with pytest.raises(recruiter.SentinelFinalizationFailedError):
        watch.poll()
    assert _event(ledger, key, "sentinel-closeout")["exchanges"] == 3


def test_a_complete_closeout_over_a_valid_bundle_ends_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    _stage_valid_result(manifest, order)
    watch.closeout_path.write_text(json.dumps(_closeout(order)))
    assert watch.poll() == "complete"


def test_an_invalid_complete_is_rejected_with_one_more_landing_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject path: archive the claim, prompt the Sentinel, wait; the second COMPLETE over
    a now-valid bundle ends the wait."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    prompts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda address, message, **kwargs: prompts.append((address, message)),
    )
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(json.dumps(_closeout(order)))

    assert watch.poll() is None
    assert not watch.closeout_path.is_file()
    assert watch.closeout_path.with_name("closeout.rejected-1.json").is_file()
    assert prompts and prompts[0][0] == "sentinel-address"
    assert "SENTINEL_LANDING_RETRY" in prompts[0][1]
    assert "sentinel-complete-rejected" in _events(ledger, key)

    _stage_valid_result(manifest, order)
    watch.closeout_path.write_text(json.dumps(_closeout(order)))
    assert watch.poll() == "complete"


def test_a_fooled_sentinel_complete_can_never_mint_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second invalid COMPLETE ends the wait into the ordinary reactor, which blocks when
    its one same-worker repair reaches nobody. No path publishes `passed`."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    prompts: list[str] = []
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda address, message, **kwargs: prompts.append(address),
    )
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(json.dumps(_closeout(order)))
    assert watch.poll() is None  # one landing round granted
    watch.closeout_path.write_text(json.dumps(_closeout(order)))
    assert watch.poll() == "complete"  # retry spent; reactor takes over
    assert "sentinel-complete-unvalidated" in _events(ledger, key)

    # The reactor's repair reaches nobody and the ambiguity rescuer is out of scope.
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
    ledger._event(key, "worker-first-action", signal="agent-status")
    python_blocked, salvage = recruiter._complete_typed_bundle(
        ledger, key, order, manifest, "worker-address", herdr_session="default"
    )
    assert python_blocked is True
    assert salvage is not None
    assert salvage["result"]["verdict"] == "blocked"


def test_the_backstop_times_out_when_no_closeout_ever_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead Sentinel changes nothing: the hard deadline still ends the wait."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    watch = _watch(ledger, order, key, manifest)
    with pytest.raises(recruiter.AgentWaitTimeout):
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            150,
            threading.Event(),
            sentinel_watch=watch,
            herdr_session="default",
        )


def test_a_staged_bundle_alone_does_not_end_a_supervised_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closeout file is THE teardown trigger while a live Sentinel supervises: even a
    valid staged bundle plus a fired artifact monitor must not end the wait — the Sentinel
    verifies the bundle in LANDING and writes COMPLETE."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    _stage_valid_result(manifest, order)
    monitor = threading.Event()
    monitor.set()
    watch = _watch(ledger, order, key, manifest)
    assert watch.supervising
    with pytest.raises(recruiter.AgentWaitTimeout):
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            200,
            monitor,
            sentinel_watch=watch,
            herdr_session="default",
        )


def test_a_degraded_sentinel_leaves_the_mechanical_paths_in_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-hired Sentinel must not strand a finished worker: without a live hire the
    artifact monitor ends the wait exactly as before."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 5.0)
    _stage_valid_result(manifest, order)
    monitor = threading.Event()
    monitor.set()
    watch = _watch(ledger, order, key, manifest, address=None)
    assert not watch.supervising
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            monitor,
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )


def test_worker_death_grants_a_bounded_closeout_window_then_mechanical_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proven pane death under supervision does not immediately end the wait: the window
    lapses without a closeout and only then does the mechanical death path take over."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(recruiter, "SENTINEL_CLOSEOUT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: True
    )
    watch = _watch(ledger, order, key, manifest)
    started = time.monotonic()
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            threading.Event(),
            sentinel_watch=watch,
            herdr_session="default",
        )
        is True
    )
    # The wait outlived the death probe by at least the closeout window.
    assert time.monotonic() - started >= 0.1


def test_a_closeout_landing_inside_the_death_window_ends_the_wait_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker pane dies, then the Sentinel lands COMPLETE inside its window: the wait
    ends through the closeout, not the mechanical death path."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.01)
    _stage_valid_result(manifest, order)
    watch = _watch(ledger, order, key, manifest)

    def gone_and_then_closeout(*args: object, **kwargs: object) -> bool:
        # The closeout appears only once the worker is already proven dead, so only the
        # in-window grace poll can consume it.
        if not watch.closeout_path.is_file():
            watch.closeout_path.parent.mkdir(parents=True, exist_ok=True)
            watch.closeout_path.write_text(json.dumps(_closeout(order)))
        return True

    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", gone_and_then_closeout
    )
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            threading.Event(),
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert _event(ledger, key, "sentinel-closeout")["outcome"] == "COMPLETE"


# --- Mechanical never-started abort under supervision ----------------------------


def test_the_sentinel_closeout_is_polled_before_the_mechanical_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer repro inverted: with the mechanical abort already fired AND a Sentinel
    NEVER_STARTED closeout on disk, the wait must end through the Sentinel's typed
    closeout (its evidence recorded), never through the bare mechanical abort."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="NEVER_STARTED",
                bundle=None,
                interpretation="banner only; no tool action before the deadline",
            )
        )
    )
    abort = threading.Event()
    abort.set()
    with pytest.raises(
        recruiter.WorkerNeverStartedError, match="sentinel closeout NEVER_STARTED"
    ):
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            threading.Event(),
            never_started_abort=abort,
            sentinel_watch=watch,
            herdr_session="default",
        )
    assert _event(ledger, key, "sentinel-closeout")["outcome"] == "NEVER_STARTED"


def test_the_mechanical_abort_grants_the_closeout_grace_window_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a live Sentinel the mechanical never-started deadline opens the same bounded
    closeout window as worker death; only its lapse hands the wait to the mechanical
    abort."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "SENTINEL_CLOSEOUT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(recruiter, "COMPLETION_MONITOR_POLL_SECONDS", 0.01)
    watch = _watch(ledger, order, key, manifest)
    abort = threading.Event()
    abort.set()
    started = time.monotonic()
    with pytest.raises(
        recruiter.WorkerNeverStartedError, match="no first observable action"
    ):
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            threading.Event(),
            never_started_abort=abort,
            sentinel_watch=watch,
            herdr_session="default",
        )
    assert time.monotonic() - started >= 0.1


def test_a_closeout_landing_inside_the_abort_window_ends_the_wait_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abort fires, then the Sentinel lands COMPLETE inside its grace window: the
    wait ends through the closeout, not the mechanical never-started fault."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "COMPLETION_MONITOR_POLL_SECONDS", 0.01)
    _stage_valid_result(manifest, order)
    watch = _watch(ledger, order, key, manifest)
    abort = threading.Event()
    abort.set()

    original_poll = watch.poll
    polls: list[int] = []

    def late_closeout() -> str | None:
        # The closeout appears only on the SECOND poll — after the abort has already
        # fired — so only the in-window grace poll can consume it.
        polls.append(1)
        if len(polls) == 2 and not watch.closeout_path.is_file():
            watch.closeout_path.parent.mkdir(parents=True, exist_ok=True)
            watch.closeout_path.write_text(json.dumps(_closeout(order)))
        return original_poll()

    monkeypatch.setattr(watch, "poll", late_closeout)
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            threading.Event(),
            never_started_abort=abort,
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert _event(ledger, key, "sentinel-closeout")["outcome"] == "COMPLETE"


def test_a_degraded_sentinel_lets_the_mechanical_abort_fire_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-hired Sentinel owns nothing: the mechanical never-started abort ends the
    wait at once, with no grace window to strand the request."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest, address=None)
    assert not watch.supervising
    abort = threading.Event()
    abort.set()
    started = time.monotonic()
    with pytest.raises(
        recruiter.WorkerNeverStartedError, match="no first observable action"
    ):
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            5_000,
            threading.Event(),
            never_started_abort=abort,
            sentinel_watch=watch,
            herdr_session="default",
        )
    assert time.monotonic() - started < 1.0


def test_the_sentinel_brief_carries_the_effective_clamped_liftoff_deadline() -> None:
    """The brief's LIFTOFF instructions must always match the mechanical deadline: a
    one-minute order clamps the watcher to 30 s (`_first_action_deadline_ms`), so the
    hired Sentinel is told 30 seconds — never a hardcoded five minutes."""
    clamped = recruiter._first_action_deadline_ms(60_000)
    assert clamped == 30_000
    brief = recruiter.llm_management.sentinel_brief(
        "request-1",
        "order-1",
        "worker-pane",
        "/work/tree",
        Path("/ledger/closeout.json"),
        liftoff_deadline_ms=clamped,
    )
    assert "If 30 seconds pass with no action" in brief
    assert "minutes pass with no action" not in brief
    standard = recruiter.llm_management.sentinel_brief(
        "request-1",
        "order-1",
        "worker-pane",
        "/work/tree",
        Path("/ledger/closeout.json"),
        liftoff_deadline_ms=recruiter._first_action_deadline_ms(1_800_000),
    )
    assert "If 5 minutes pass with no action" in standard


# --- Requester→worker logged message channel ------------------------------------


def _messageable_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict, str, str, Path]:
    ledger, order, key, _manifest = _claimed_request(tmp_path, monkeypatch)
    lease = ledger._lease(ledger.active / "requests" / key / "lease.json")
    token = lease["token"]
    control_token = lease["requester_control_token"]
    launch_id = ledger.begin_launch(
        key, token, "worker", "upagent-worker-1", "default", order["cwd"]
    )
    ledger.record_launch_created(
        key, token, launch_id, "worker-pane", "workspace", "worker-address"
    )
    assert ledger.mark_launch_started(
        key, token, launch_id, "worker-pane", "workspace", "worker-address"
    )
    order_path = ledger.request_dir(key) / "request.json"
    token_file = tmp_path / "control-token"
    token_file.write_text(control_token)
    token_file.chmod(0o600)
    message_file = tmp_path / "message.md"
    message_file.write_text("status? what are you on?\n")
    return ledger, order, key, str(order_path), token_file


def test_requester_messages_are_ledger_logged_then_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger, _order_value, key, order_path, token_file = _messageable_request(
        tmp_path, monkeypatch
    )
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda address, message, **kwargs: delivered.append((address, message)),
    )
    assert (
        recruiter.cmd_message_worker(
            order_path, str(token_file), str(tmp_path / "message.md")
        )
        == 0
    )
    logged = _event(ledger, key, "requester-worker-message")
    assert logged["message"] == "status? what are you on?"
    assert logged["worker_pane"] == "worker-pane"
    assert delivered == [("worker-address", "status? what are you on?")]
    assert json.loads(capsys.readouterr().out)["delivered"] is True


def test_a_wrong_control_token_neither_logs_nor_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _order_value, key, order_path, _token_file = _messageable_request(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: pytest.fail("must not deliver unauthenticated"),
    )
    wrong = tmp_path / "wrong-token"
    wrong.write_text("not-the-token")
    with pytest.raises(recruiter.RecruiterError, match="control token"):
        recruiter.cmd_message_worker(
            order_path, str(wrong), str(tmp_path / "message.md")
        )
    assert "requester-worker-message" not in _events(ledger, key)


# --- Induced-failure drills through cmd_run_job ---------------------------------


class _Drill:
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.staging: list[Path] = []
        self.hires: list[Path] = []
        self.prompts: list[tuple[str, str]] = []
        self.launched = threading.Event()
        self.hired = threading.Event()


def _drill_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout_ms: int = 10_000,
    first_action: bool = True,
    git: bool = False,
    grace_ms: int | None = None,
) -> tuple[Any, dict, str, Path, _Drill]:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    if git:
        _git_repo(worktree)
    instructions = worktree / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(
        cwd=str(worktree),
        instructions_path=str(instructions),
        result_path=str(tmp_path / "public" / "result.json"),
        timeout_ms=timeout_ms,
    )
    roster_path = tmp_path / "upagent.yaml"
    roster = 'harnesses:\n  claude: "claude read:{instructions_path} write:{result_path}"\n'
    if grace_ms is not None:
        roster += f"management:\n  requester_grace_ms: {grace_ms}\n"
    roster_path.write_text(roster)
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    drill = _Drill()

    monkeypatch.setattr(recruiter, "_ensure_claude_folder_trust", lambda cwd: None)
    monkeypatch.setattr(recruiter, "_report_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.05)
    monkeypatch.setattr(recruiter, "COMPLETION_MONITOR_POLL_SECONDS", 0.05)
    monkeypatch.setattr(recruiter, "HEALTH_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    def fake_start(
        name: str, execution_order: dict, launch: str, **kwargs: object
    ) -> tuple[str, str, str]:
        drill.staging.append(Path(launch.split("write:", maxsplit=1)[1]))
        drill.launched.set()
        return "worker-pane", "cockpit", name

    monkeypatch.setattr(recruiter, "_start_herdr_agent", fake_start)
    monkeypatch.setattr(
        recruiter, "_wait_for_worker_health", lambda *args, **kwargs: {"healthy": True}
    )

    def fake_close(pane: str, **kwargs: object) -> dict:
        drill.closed.append(pane)
        return {"status": "closed", "worker_pane": pane, "verified_absent": True}

    monkeypatch.setattr(recruiter, "_close_worker_pane", fake_close)

    def fake_watch(
        ledger_arg: Any,
        key_arg: str,
        pane: str,
        staging: Path,
        abort: threading.Event,
        stop: threading.Event,
        **kwargs: object,
    ) -> None:
        if first_action:
            marker = {
                "signal": "agent-status",
                "agent_status": "working",
                "worker_pane": pane,
                "attempt": kwargs["attempt"],
            }
            ledger_arg._event(key_arg, "worker-first-action", **marker)
            callback = kwargs.get("on_first_action")
            if callable(callback):
                callback(marker)
        else:
            # The real watcher records the deadline verdict after enough proven idle
            # probes; the never-started terminal is minted only over this event, and
            # only for the attempt stamped on it.
            ledger_arg._event(
                key_arg,
                "worker-never-started",
                deadline_ms=300_000,
                idle_probes=recruiter.FIRST_ACTION_MIN_IDLE_PROBES,
                agent_status="idle",
                worker_pane=pane,
                attempt=kwargs["attempt"],
            )

    monkeypatch.setattr(recruiter, "_watch_first_action", fake_watch)

    def fake_sentinel(
        ledger_arg: Any,
        key_arg: str,
        token: str,
        order_arg: dict,
        config: Any,
        worker_pane: str,
        closeout_path: Path,
        generation: int,
        *,
        herdr_session: str,
        liftoff_deadline_ms: int,
    ) -> dict[str, object]:
        assert liftoff_deadline_ms > 0
        drill.hires.append(closeout_path)
        drill.hired.set()
        return {
            "address": "sentinel-address",
            "closeout_path": closeout_path,
            "launch_id": None,
            "pane": "sentinel-pane",
            "workspace_id": None,
        }

    monkeypatch.setattr(recruiter, "_start_sentinel", fake_sentinel)

    def fake_prompt(address: str, message: str, **kwargs: object) -> None:
        drill.prompts.append((address, message))
        raise recruiter.RecruiterError("agent_not_found")

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", fake_prompt)
    return ledger, order, key, roster_path, drill


def _pre_write_closeout(
    ledger: Any, key: str, order: dict, attempt: int, **over: object
) -> Path:
    path = recruiter._sentinel_closeout_path(ledger, key, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_closeout(order, **over)))
    return path


def test_drill_worker_dead_before_first_action_lands_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Drill: kill the worker before its first action. The Sentinel's NEVER_STARTED
    closeout ends each attempt; the salvage facts independently agree; the auto-retry
    fires exactly once; the terminal is the typed `never-started`; both panes reaped."""
    ledger, order, key, roster_path, drill = _drill_job(
        tmp_path, monkeypatch, first_action=False
    )
    for attempt in (1, 2):
        _pre_write_closeout(
            ledger,
            key,
            order,
            attempt,
            outcome="NEVER_STARTED",
            bundle=None,
            interpretation="five minutes of banner and no tool action",
        )

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "never-started"
    assert receipt["synthesis_path"] == "never-started"
    events = _events(ledger, key)
    assert events.count("sentinel-closeout") == 2
    assert events.count("never-started-auto-retry") == 1
    assert drill.closed == [
        "worker-pane",
        "sentinel-pane",
        "worker-pane",
        "sentinel-pane",
    ]
    assert receipt["cleanup"]["verified_absent"] is True


def test_drill_worker_dead_mid_work_lands_stalled_blocked_with_interpretation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Drill: kill the worker mid-work. The STALLED closeout blocks the request with the
    Sentinel's interpretation attached and the Phase 1 epilogue in the result."""
    ledger, order, key, roster_path, drill = _drill_job(tmp_path, monkeypatch)
    (Path(order["cwd"]) / "half-done.txt").write_text("partial\n")
    _pre_write_closeout(
        ledger,
        key,
        order,
        1,
        outcome="STALLED",
        bundle=None,
        interpretation="worker went quiet after fix 1; nudge unanswered",
        blocking_question="is fix 2 still in scope after the API change?",
        progress_so_far="fix 1 written to half-done.txt",
        last_alive="tool output at pulse 2",
    )

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "blocked"
    published = json.loads(Path(order["result_path"]).read_text())
    assert "sentinel closeout STALLED" in published["reason"]
    # Zero citations survived verification: the sentinel prose travels, but explicitly
    # marked, and the published reason names Python's epilogue as the authority.
    assert "no sentinel citation survived Python verification" in published["reason"]
    assert (
        "sentinel interpretation (uncorroborated): worker went quiet after fix 1; "
        "nudge unanswered" in published["reason"]
    )
    assert "epilogue" in published
    # The cold handoff is first-class on the published result AND the receipt.
    question = "is fix 2 still in scope after the API change?"
    assert published["blocking_question"] == question
    assert receipt["blocking_question"] == question
    assert _event(ledger, key, "sentinel-closeout")["outcome"] == "STALLED"
    assert drill.closed == ["worker-pane", "sentinel-pane"]
    assert "never-started-auto-retry" not in _events(ledger, key)


def test_drill_worker_dead_before_bundle_lands_finalization_failed_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Drill: kill the worker after the work but before the bundle. FINALIZATION_FAILED
    blocks with the corroborated commit citation and the landed commit in the epilogue."""
    ledger, order, key, roster_path, drill = _drill_job(
        tmp_path, monkeypatch, git=True
    )
    sha = _commit(Path(order["cwd"]), "finished-work.txt")
    _pre_write_closeout(
        ledger,
        key,
        order,
        1,
        outcome="FINALIZATION_FAILED",
        bundle=None,
        interpretation="the work landed but three exchanges produced no bundle",
        citations=[sha],
        exchanges=[{"question": "finished?", "answer": "yes", "verified": False}] * 3,
    )

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "blocked"
    published = json.loads(Path(order["result_path"]).read_text())
    assert "sentinel closeout FINALIZATION_FAILED" in published["reason"]
    # The corroborated commit leads the published reason as Python-checked fact; the
    # Sentinel's own prose stays explicitly marked — a verified citation never
    # launders the interpretation around it.
    assert f"Python-verified citations: {sha}" in published["reason"]
    assert "sentinel interpretation (uncorroborated):" in published["reason"]
    assert [item["sha"] for item in published["epilogue"]["landed_commits"]] == [sha]
    closeout_event = _event(ledger, key, "sentinel-closeout")
    assert closeout_event["corroborated_citations"] == [sha]
    assert drill.closed == ["worker-pane", "sentinel-pane"]


def test_drill_dead_sentinel_falls_to_the_hard_timeout_backstop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Drill: kill the Sentinel too — no closeout ever appears. The hard timeout and the
    ordinary blocked path fire unchanged, and both panes are still reaped."""
    ledger, order, key, roster_path, drill = _drill_job(
        tmp_path, monkeypatch, timeout_ms=400, grace_ms=50
    )

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "blocked"
    published = json.loads(Path(order["result_path"]).read_text())
    assert "exceeded its cap" in published["reason"]
    assert "sentinel-closeout" not in _events(ledger, key)
    assert drill.closed == ["worker-pane", "sentinel-pane"]
    assert receipt["cleanup"]["verified_absent"] is True


def test_drill_complete_closeout_publishes_passed_through_ordinary_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The healthy landing: a COMPLETE closeout over a valid staged bundle tears down into
    the ordinary validation and publishes `passed`; both panes reaped."""
    ledger, order, key, roster_path, drill = _drill_job(tmp_path, monkeypatch)

    outcome: list[int] = []
    runner = threading.Thread(
        target=lambda: outcome.append(recruiter.cmd_run_job(key, str(roster_path)))
    )
    runner.start()
    assert drill.hired.wait(timeout=5)
    staging = drill.staging[0]
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(
        json.dumps(
            {
                "order_id": order["order_id"],
                "verdict": "passed",
                "reason": "did the work",
                "full_log": "worker-session",
            }
        )
    )
    _pre_write_closeout(ledger, key, order, 1, bundle=str(staging))
    runner.join(timeout=10)
    assert not runner.is_alive()

    assert outcome == [0]
    # The wait must have ended by CONSUMING the closeout, not through the old staged-
    # artifact path: only `_SentinelWatch.poll` writes the sentinel-closeout event.
    closeout_event = _event(ledger, key, "sentinel-closeout")
    assert closeout_event["outcome"] == "COMPLETE"
    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "passed"
    assert drill.closed == ["worker-pane", "sentinel-pane"]
    assert json.loads(Path(order["result_path"]).read_text())["verdict"] == "passed"
