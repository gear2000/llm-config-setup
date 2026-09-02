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
import os
import subprocess
import threading
import time
import uuid
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
    assert (
        contracts.parse_order(json.dumps(_order(sentinel=False)))["sentinel"] is False
    )
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


def _stalled_watch_with_worker_pane(
    ledger: Any, order: dict, key: str, manifest: Any
) -> Any:
    closeout_path = recruiter._sentinel_closeout_path(ledger, key, 1)
    closeout_path.parent.mkdir(parents=True, exist_ok=True)
    return recruiter._SentinelWatch(
        ledger,
        key,
        order,
        manifest,
        closeout_path,
        {"address": "sentinel-address", "worker_pane": "worker-pane"},
        herdr_session="default",
    )


def test_an_uncorroborated_stalled_over_a_live_worker_is_rejected_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: a STALLED closeout whose citations ALL failed corroboration may not
    terminalize a provably live worker on the Sentinel's word — Python re-probes the
    worker pane once, rejects the closeout back to the Sentinel, and continues the
    wait. A second uncorroborated STALLED is then accepted (the recheck is spent)."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_present", lambda *args, **kwargs: True
    )
    prompts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda address, message, **kwargs: prompts.append((address, message)),
    )
    watch = _stalled_watch_with_worker_pane(ledger, order, key, manifest)
    stalled = _closeout(
        order,
        outcome="STALLED",
        bundle=None,
        progress_so_far="claims with no evidence",
        last_alive="unknown",
    )
    watch.closeout_path.write_text(json.dumps(stalled))
    assert watch.poll() is None
    assert not watch.closeout_path.is_file()
    assert watch.closeout_path.with_name("closeout.stalled-rejected.json").is_file()
    assert prompts and prompts[0][0] == "sentinel-address"
    assert "SENTINEL_STALL_RECHECK" in prompts[0][1]
    assert "sentinel-stalled-rejected" in _events(ledger, key)

    watch.closeout_path.write_text(json.dumps(stalled))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()


def test_an_uncorroborated_stalled_without_positive_liveness_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-probe rejects only on a POSITIVE pane-get answer: a gone pane AND a
    transport/probe fault both answer not-present, and both accept the STALLED
    closeout immediately — probe uncertainty is never treated as liveness."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_present", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: pytest.fail("no recheck prompt for a gone worker"),
    )
    watch = _stalled_watch_with_worker_pane(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="STALLED",
                bundle=None,
                progress_so_far="unknown",
                last_alive="unknown",
            )
        )
    )
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    assert "sentinel-stalled-rejected" not in _events(ledger, key)


def test_a_corroborated_stalled_needs_no_worker_re_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STALLED closeout with at least one Python-corroborated citation is accepted
    without probing: the second look is only for claims with zero checked evidence."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    real_file = Path(order["cwd"]) / "evidence.txt"
    real_file.write_text("progress\n")
    monkeypatch.setattr(
        recruiter,
        "_worker_pane_confirmed_present",
        lambda *args, **kwargs: pytest.fail("corroborated STALLED must not probe"),
    )
    watch = _stalled_watch_with_worker_pane(ledger, order, key, manifest)
    watch.closeout_path.write_text(
        json.dumps(
            _closeout(
                order,
                outcome="STALLED",
                bundle=None,
                citations=[str(real_file)],
                progress_so_far="evidence written",
                last_alive="pulse 2",
            )
        )
    )
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()


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


def test_a_validated_bundle_nudges_the_sentinel_and_lapses_into_the_mechanical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NUDGE-TO-LAND: a validated staged bundle under a live Sentinel is not suppressed
    silently — the wake FILE (the one nudge channel, carrying the reason as content)
    wakes the Sentinel and opens one bounded landing window; when the window lapses
    with no closeout, the mechanical artifact path ends the wait (with the typed lapse
    recorded), instead of stranding the finished worker."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(recruiter, "SENTINEL_LANDING_WINDOW_SECONDS", 0.15)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: pytest.fail("the wake file is the one nudge channel"),
    )
    _stage_valid_result(manifest, order)
    monitor = threading.Event()
    monitor.set()
    watch = _watch(ledger, order, key, manifest)
    assert watch.supervising
    started = time.monotonic()
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
    # The wait held the landing window open before falling back mechanically.
    assert time.monotonic() - started >= 0.15
    # The wake file carries the reason as its content.
    assert watch.wake_path.read_text().strip() == "valid-bundle"
    events = _events(ledger, key)
    assert "sentinel-wake-valid-bundle" in events
    lapse = _event(ledger, key, "sentinel-window-lapsed")
    assert lapse["window"] == "landing"


def test_a_landing_window_clipped_by_the_hard_deadline_still_accepts_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (review finding 3): a valid bundle arriving with less than a full
    landing window left before the request cap must end the wait on the bundle — the
    active window lapse is checked BEFORE the generic timeout, so the request never
    times out over work that is already done."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    # Window far longer than the request cap: only the deadline can clip it.
    monkeypatch.setattr(recruiter, "SENTINEL_LANDING_WINDOW_SECONDS", 60.0)
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
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            300,
            monitor,
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert _event(ledger, key, "sentinel-window-lapsed")["window"] == "landing"


def test_the_exec_wait_shares_the_valid_bundle_nudge_and_landing_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 2: a still-live exec worker whose staged bundle already
    validated gets the same wake + bounded landing window as an interactive worker —
    the wait ends on the bundle at window lapse instead of holding for process exit,
    a pulse, or the hard deadline."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    # The long-lived stand-in for `herdr wait agent-status` on a live exec worker.
    monkeypatch.setattr(
        recruiter, "_herdr_argv", lambda args, session: ("default", ["sleep", "30"])
    )
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(recruiter, "COMPLETION_MONITOR_POLL_SECONDS", 0.01)
    monkeypatch.setattr(recruiter, "SENTINEL_LANDING_WINDOW_SECONDS", 0.15)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    _stage_valid_result(manifest, order)
    monitor = threading.Event()
    monitor.set()
    watch = _watch(ledger, order, key, manifest)
    started = time.monotonic()
    assert (
        recruiter._wait_for_agent_status(
            "worker-pane",
            60_000,
            monitor,
            completion_style="exec",
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert time.monotonic() - started < 10.0
    assert watch.wake_path.read_text().strip() == "valid-bundle"
    events = _events(ledger, key)
    assert "sentinel-wake-valid-bundle" in events
    assert _event(ledger, key, "sentinel-window-lapsed")["window"] == "landing"


def test_a_closeout_landing_inside_the_nudged_window_ends_the_wait_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The woken Sentinel lands COMPLETE inside its window: the wait ends through the
    closeout (closeout-as-trigger preserved), not the mechanical fallback."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    _stage_valid_result(manifest, order)
    watch = _watch(ledger, order, key, manifest)

    def sentinel_wakes_and_lands() -> None:
        # A stand-in Sentinel: wake on the file, act on the reason, close out.
        deadline = time.monotonic() + 5
        while not watch.wake_path.is_file():
            if time.monotonic() > deadline:  # pragma: no cover - test guard
                return
            time.sleep(0.005)
        assert watch.wake_path.read_text().strip() == "valid-bundle"
        watch.wake_path.unlink()
        watch.closeout_path.parent.mkdir(parents=True, exist_ok=True)
        watch.closeout_path.write_text(json.dumps(_closeout(order)))

    threading.Thread(target=sentinel_wakes_and_lands).start()
    monitor = threading.Event()
    monitor.set()
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
    events = _events(ledger, key)
    assert "sentinel-window-lapsed" not in events
    assert _event(ledger, key, "sentinel-closeout")["outcome"] == "COMPLETE"


def test_partial_staging_wakes_the_sentinel_without_ending_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging activity that has not validated (some-but-not-all files, or an invalid
    result) touches the wake file with its own typed event, so the worker gets its
    LANDING dialogue in seconds — while the wait itself continues to the closeout,
    the mechanical paths, or the deadline."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: pytest.fail("partial staging must not prompt"),
    )
    # A partial bundle: only the optional summary staged, no result.json.
    staged = manifest.artifact("compacted").staging_path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("partial summary\n")
    watch = _watch(ledger, order, key, manifest)
    with pytest.raises(recruiter.AgentWaitTimeout):
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            300,
            threading.Event(),
            sentinel_watch=watch,
            herdr_session="default",
        )
    assert watch.wake_path.is_file()
    assert "sentinel-wake-partial-staging" in _events(ledger, key)


def test_a_wake_write_racing_a_consumer_claim_is_never_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Producer/consumer interleaving (final-review must-fix 1): the publish is an
    atomic temp-write + rename, the consumer claims by renaming to a private name,
    and a publish after the claim lands as a fresh wake file — never a lost wake, and
    never an observable empty reason."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)

    # Publish, then the consumer claims atomically (the brief's `mv`).
    watch.touch_wake("valid-bundle")
    assert watch.wake_path.read_text().strip() == "valid-bundle"
    claimed = watch.wake_path.with_name(watch.wake_path.name + ".claimed")
    os.replace(watch.wake_path, claimed)
    assert not watch.wake_path.exists()

    # A publish racing (after) the claim republishes a complete file: the consumer's
    # next wait sees it, and the claimed copy is untouched.
    watch.touch_wake("valid-bundle")
    assert watch.wake_path.read_text().strip() == "valid-bundle"
    assert claimed.read_text().strip() == "valid-bundle"
    # No partially-written temp residue beside the wake file.
    residue = [
        item.name
        for item in watch.wake_path.parent.iterdir()
        if item.name.startswith(".wake") and item.name.endswith(".tmp")
    ]
    assert residue == []
    # The ledger event stays once-per-kind even across republishes.
    assert _events(ledger, key).count("sentinel-wake-valid-bundle") == 1


def test_landing_pass_first_observed_at_expired_deadline_is_mechanical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final-review must-fix 2: a bundle first observed with the request deadline
    already reached must lapse to the mechanical path immediately — never open a
    nominal window that the caller's generic timeout turns into AgentWaitTimeout."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)
    monitor = threading.Event()
    monitor.set()
    # The reviewer's direct probe: must not be None at an expired deadline.
    assert watch.landing_pass(monitor, time.monotonic() - 1) == "mechanical"
    assert _event(ledger, key, "sentinel-window-lapsed")["window"] == "landing"

    # And through the real loop: monitor validated, deadline effectively immediate.
    (tmp_path / "second").mkdir()
    ledger2, order2, key2, manifest2 = _claimed_request(
        tmp_path / "second", monkeypatch
    )
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    _stage_valid_result(manifest2, order2)
    watch2 = _watch(ledger2, order2, key2, manifest2)
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            1,
            monitor,
            sentinel_watch=watch2,
            herdr_session="default",
        )
        is False
    )
    assert _event(ledger2, key2, "sentinel-window-lapsed")["window"] == "landing"


class _FlipEvent(threading.Event):
    """The reviewer's deterministic probe: the first is_set() observation reports
    False (the landing pass misses the validation), every later one True (the monitor
    set the event right after). A set observed before the timeout decision must never
    be lost to AgentWaitTimeout."""

    def __init__(self) -> None:
        super().__init__()
        self._observed = False

    def is_set(self) -> bool:
        if not self._observed:
            self._observed = True
            return False
        self.set()
        return True


def test_a_validation_racing_the_deadline_is_reobserved_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check/use gap at the hard deadline (interactive): the landing pass observes an
    unset monitor, the monitor validates immediately after, and the deadline is
    reached — the timeout decision must re-observe the event and end the wait on the
    validated bundle with the typed lapse, never raise AgentWaitTimeout."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 5.0)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    _stage_valid_result(manifest, order)
    watch = _watch(ledger, order, key, manifest)
    monitor = _FlipEvent()
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            1,
            monitor,
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert monitor.is_set()
    assert _event(ledger, key, "sentinel-window-lapsed")["window"] == "landing"


def test_a_validation_racing_the_deadline_is_reobserved_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same check/use gap in the exec loop: the re-observation at the timeout
    decision accepts the validated bundle instead of raising."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "_herdr_available", lambda: None)
    monkeypatch.setattr(
        recruiter, "_herdr_argv", lambda args, session: ("default", ["sleep", "30"])
    )
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 5.0)
    monkeypatch.setattr(recruiter, "COMPLETION_MONITOR_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: False
    )
    _stage_valid_result(manifest, order)
    watch = _watch(ledger, order, key, manifest)
    monitor = _FlipEvent()
    assert (
        recruiter._wait_for_agent_status(
            "worker-pane",
            1,
            monitor,
            completion_style="exec",
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert monitor.is_set()
    assert _event(ledger, key, "sentinel-window-lapsed")["window"] == "landing"


def test_sentinel_death_inside_the_worker_gone_window_records_the_typed_lapse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final-review must-fix 3: when the Sentinel dies inside an already-open
    worker-gone closeout window, the exit carries BOTH the death reason
    (sentinel-dead) and the typed window lapse — no supervised-wait ending lacks a
    typed cause."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(recruiter, "COMPLETION_MONITOR_POLL_SECONDS", 0.005)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: True
    )
    closeout_path = recruiter._sentinel_closeout_path(ledger, key, 1)
    closeout_path.parent.mkdir(parents=True, exist_ok=True)
    watch = recruiter._SentinelWatch(
        ledger,
        key,
        order,
        manifest,
        closeout_path,
        {"address": "sentinel-address", "pane": "sentinel-pane"},
        herdr_session="default",
    )
    assert (
        recruiter._await_sentinel_closeout_after_worker_gone(
            watch, time.monotonic() + 30, reason="worker-gone"
        )
        is False
    )
    events = _events(ledger, key)
    assert "sentinel-dead" in events
    assert _event(ledger, key, "sentinel-window-lapsed")["window"] == "closeout"
    assert watch.wake_path.read_text().strip() == "worker-gone"


def test_a_dead_sentinel_pane_degrades_supervision_mid_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the Sentinel's own pane is probed on the poll cadence; on confirmed-gone the
    wait falls back to the mechanical paths for the rest of the wait instead of trusting
    the wait-entry supervision snapshot and stranding a finished worker."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)

    def only_sentinel_gone(pane: str, **kwargs: object) -> bool:
        return pane == "sentinel-pane"

    monkeypatch.setattr(recruiter, "_worker_pane_confirmed_gone", only_sentinel_gone)
    monkeypatch.setattr(
        recruiter, "_worker_process_confirmed_gone", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: pytest.fail("a dead sentinel must not be nudged"),
    )
    _stage_valid_result(manifest, order)
    monitor = threading.Event()
    closeout_path = recruiter._sentinel_closeout_path(ledger, key, 1)
    closeout_path.parent.mkdir(parents=True, exist_ok=True)
    watch = recruiter._SentinelWatch(
        ledger,
        key,
        order,
        manifest,
        closeout_path,
        {"address": "sentinel-address", "pane": "sentinel-pane"},
        herdr_session="default",
    )
    assert watch.supervising

    def fire_monitor() -> None:
        time.sleep(0.2)
        monitor.set()

    threading.Thread(target=fire_monitor).start()
    started = time.monotonic()
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            10_000,
            monitor,
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert time.monotonic() - started < 5.0
    assert not watch.supervising
    dead = _event(ledger, key, "sentinel-dead")
    assert dead["sentinel_pane"] == "sentinel-pane"


def test_a_worker_gone_with_a_validated_bundle_bypasses_the_closeout_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3: proven worker exit with an already-validated staged bundle takes the
    mechanical path immediately — the Sentinel has nothing to add, and the bypass is a
    typed ledger event."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(recruiter, "AGENT_WAIT_PANE_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(recruiter, "SENTINEL_CLOSEOUT_GRACE_SECONDS", 30.0)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda address, message, **kwargs: None,
    )
    _stage_valid_result(manifest, order)
    monitor = threading.Event()
    monitor.set()
    watch = _watch(ledger, order, key, manifest)
    started = time.monotonic()
    assert (
        recruiter._wait_for_interactive_completion(
            "worker-pane",
            "claude",
            60_000,
            monitor,
            sentinel_watch=watch,
            herdr_session="default",
        )
        is False
    )
    assert time.monotonic() - started < 5.0
    assert "sentinel-bypassed-at-exit" in _events(ledger, key)


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
    # The window opened with an awake Sentinel: the wake file was touched first,
    # carrying the reason, and the fallback is a typed lapse — the ledger alone says
    # why the supervised wait ended mechanically.
    assert watch.wake_path.read_text().strip() == "worker-gone"
    assert "sentinel-wake-worker-gone" in _events(ledger, key)
    assert _event(ledger, key, "sentinel-window-lapsed")["window"] == "closeout"


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
        wake_path=Path("/ledger/wake"),
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
        wake_path=Path("/ledger/wake"),
    )
    assert "If 5 minutes pass with no action" in standard


def test_the_brief_pulse_is_an_event_driven_bounded_wake_wait() -> None:
    """M2 (wake-file design): the pulse is a bounded wait on the attempt's wake file
    with the interval as fallback only. The exact path and the exact blocking
    one-liner — which prints the wake REASON before consuming the file — are threaded
    into the brief, the brief instructs the explicit command timeout that outlives the
    wait (the harness default would kill it mid-block), a valid-bundle wake forbids
    re-sleeping and goes straight to the closeout, and a Recruiter prompt overrides
    waiting."""
    llm = recruiter.llm_management
    wake = Path("/ledger/wake")
    brief = llm.sentinel_brief(
        "request-1",
        "order-1",
        "worker-pane",
        "/work/tree",
        Path("/ledger/closeout.json"),
        liftoff_deadline_ms=300_000,
        wake_path=wake,
    )
    assert "sleep 900" not in brief
    assert llm.SENTINEL_PULSE_MINUTES == 5
    pulse_seconds = llm.SENTINEL_PULSE_MINUTES * 60
    assert llm.SENTINEL_PULSE_COMMAND_TIMEOUT_MS > pulse_seconds * 1000
    assert llm.SENTINEL_PULSE_COMMAND_TIMEOUT_MS <= 600_000
    assert f"{llm.SENTINEL_PULSE_COMMAND_TIMEOUT_MS} ms" in brief
    assert (
        f'for i in $(seq {pulse_seconds}); do [ -e "{wake}" ] && break; '
        f'sleep 1; done; mv "{wake}" "{wake}.claimed" 2>/dev/null; '
        f'cat "{wake}.claimed" 2>/dev/null; rm -f "{wake}.claimed"'
    ) in brief
    # The wake reason drives the response; valid-bundle skips dialogue and re-sleep.
    assert "`valid-bundle`" in brief
    assert "Do NOT re-sleep" in brief
    assert "`partial-staging`" in brief
    assert "`worker-gone`" in brief
    assert "`never-started`" in brief
    assert "worker for quiet\n  IMMEDIATELY" in brief or "worker for quiet" in brief
    assert "OVERRIDES waiting" in brief
    # The prompt-based nudge channel is deleted: the wake file is the one channel.
    assert "SENTINEL_NUDGE_TO_LAND" not in brief


def test_staging_activity_tolerates_concurrent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 5: the worker owns its staging paths and may unlink/replace one
    between any check and read, so the probe uses one stat per artifact and treats a
    vanished file as no activity — never an escaped fault that blocks the request."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch = _watch(ledger, order, key, manifest)

    class _VanishingPath:
        def stat(self) -> object:
            raise FileNotFoundError("unlinked mid-probe")

    class _Artifact:
        def __init__(self, staging_path: object) -> None:
            self.staging_path = staging_path

    real = tmp_path / "staged.md"
    real.write_text("content\n")

    watch.manifest = type("M", (), {"artifacts": [_Artifact(_VanishingPath())]})()
    assert watch.staging_activity() is False

    watch.manifest = type(
        "M", (), {"artifacts": [_Artifact(_VanishingPath()), _Artifact(real)]}
    )()
    assert watch.staging_activity() is True


def test_a_malformed_sentinel_command_lands_in_the_degrade_path(
    tmp_path: Path,
) -> None:
    """Review finding 6: an unmatched quote in a configured sentinel command must
    become the ordinary hire-degrade diagnosis, never a ValueError that fails the
    worker lifecycle."""
    missing = recruiter._sentinel_persona_missing(
        'claude --agent "unclosed', str(tmp_path)
    )
    assert missing is not None
    assert "could not be parsed" in missing
    assert "management.sentinel.command" in missing


# --- Persona pre-check and degrade hygiene ---------------------------------------


def test_the_persona_pre_check_names_the_exact_missing_paths(
    tmp_path: Path,
) -> None:
    """S2: a missing Sentinel persona is diagnosed before any pane exists, the message
    names the candidate paths the operator must fix, and the answer is cached per
    process invocation instead of being re-diagnosed on every attempt."""
    agent = f"upagent-sentinel-test-{uuid.uuid4().hex}"
    command = f"claude --dangerously-skip-permissions --agent {agent} --model haiku"
    cwd = tmp_path / "wt"
    cwd.mkdir()
    missing = recruiter._sentinel_persona_missing(command, str(cwd))
    assert missing is not None
    assert repr(agent) in missing
    assert str(cwd / ".claude/agents" / f"{agent}.md") in missing
    assert str(Path.home() / ".claude/agents" / f"{agent}.md") in missing
    # Cached per invocation: creating the file later does not change this process's
    # cached answer (the next per-command invocation re-checks fresh).
    persona = cwd / ".claude/agents" / f"{agent}.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("persona\n")
    assert recruiter._sentinel_persona_missing(command, str(cwd)) == missing

    # A persona present from the start (fresh cache key via a fresh cwd) passes.
    other = tmp_path / "wt2"
    (other / ".claude/agents").mkdir(parents=True)
    (other / ".claude/agents" / f"{agent}.md").write_text("persona\n")
    assert recruiter._sentinel_persona_missing(command, str(other)) is None
    # A command with no --agent needs no persona file.
    assert recruiter._sentinel_persona_missing("claude --model haiku", str(cwd)) is None


def test_a_failed_sentinel_hire_degrades_supervision_and_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """S3 + S2 hygiene: a hire that fails (herdr pane limit, missing persona, ...)
    degrades supervision for the request instead of failing it — every attempt's
    degrade is a typed ledger event carrying the attempt number, but the requester is
    notified once per distinct reason per invocation, not spammed per attempt."""
    ledger, order, key, roster_path, drill = _drill_job(
        tmp_path, monkeypatch, first_action=False, grace_ms=50
    )

    # With no Sentinel there is no closeout to end the wait: the mechanical abort
    # does, exactly as the real watcher fires it on the proven deadline.
    def abort_watch(
        ledger_arg: Any,
        key_arg: str,
        pane: str,
        staging: Path,
        abort: threading.Event,
        stop: threading.Event,
        **kwargs: object,
    ) -> None:
        ledger_arg._event(
            key_arg,
            "worker-never-started",
            deadline_ms=300_000,
            idle_probes=recruiter.FIRST_ACTION_MIN_IDLE_PROBES,
            agent_status="idle",
            worker_pane=pane,
            attempt=kwargs["attempt"],
        )
        if kwargs.get("abort_on_deadline"):
            abort.set()

    monkeypatch.setattr(recruiter, "_watch_first_action", abort_watch)

    def refuse_hire(*args: object, **kwargs: object) -> dict[str, object]:
        raise recruiter.RecruiterError(
            "pane creation refused: workspace pane limit reached"
        )

    monkeypatch.setattr(recruiter, "_start_sentinel", refuse_hire)
    notified: list[str] = []
    monkeypatch.setattr(
        recruiter,
        "_notify_requester",
        lambda ledger_arg, key_arg, order_arg, generation, message_type, *rest: (
            notified.append(message_type)
        ),
    )

    # The never-started auto-retry gives two attempts; both hires fail.
    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    receipt = ledger.completed_receipt(key, order)
    assert receipt["verdict"] == "never-started"
    degrades = [
        item for item in ledger.events(key) if item.get("event") == "sentinel-degraded"
    ]
    assert [item["attempt"] for item in degrades] == [1, 2]
    assert all("pane limit" in item["reason"] for item in degrades)
    assert notified.count("sentinel-degraded") == 1


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
        self.sentinel_commands: list[str] = []
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
    harness: str = "claude",
    model: str = "some-model",
) -> tuple[Any, dict, str, Path, _Drill]:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    if git:
        _git_repo(worktree)
    instructions = worktree / "instructions.md"
    instructions.write_text("Do the stage.\n")
    order = _order(
        harness=harness,
        model=model,
        cwd=str(worktree),
        instructions_path=str(instructions),
        result_path=str(tmp_path / "public" / "result.json"),
        timeout_ms=timeout_ms,
    )
    roster_path = tmp_path / "upagent.yaml"
    roster = (
        f'harnesses:\n  {harness}: "{harness} read:{{instructions_path}} '
        'write:{result_path}"\n'
    )
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
        sentinel_role: Any,
        worker_pane: str,
        closeout_path: Path,
        generation: int,
        *,
        herdr_session: str,
        liftoff_deadline_ms: int,
    ) -> dict[str, object]:
        assert liftoff_deadline_ms > 0
        drill.hires.append(closeout_path)
        drill.sentinel_commands.append(sentinel_role.command)
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


@pytest.mark.parametrize(
    ("harness", "model", "worker_provider", "sentinel_provider", "command"),
    (
        (
            "claude",
            "claude-sonnet-5",
            "anthropic",
            "openai",
            recruiter.llm_management.DEFAULT_OPENAI_SENTINEL_COMMAND,
        ),
        (
            "codex",
            "gpt-5.6",
            "openai",
            "anthropic",
            recruiter.llm_management.DEFAULT_SENTINEL_COMMAND,
        ),
        (
            "pi",
            "openrouter/z-ai/glm-5.3-flash",
            "openrouter",
            "anthropic",
            recruiter.llm_management.DEFAULT_SENTINEL_COMMAND,
        ),
    ),
)
def test_each_retry_revalidates_and_hires_the_opposite_provider_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    harness: str,
    model: str,
    worker_provider: str,
    sentinel_provider: str,
    command: str,
) -> None:
    ledger, order, key, roster_path, drill = _drill_job(
        tmp_path,
        monkeypatch,
        first_action=False,
        harness=harness,
        model=model,
    )
    for attempt in (1, 2):
        _pre_write_closeout(
            ledger,
            key,
            order,
            attempt,
            outcome="NEVER_STARTED",
            bundle=None,
            interpretation="no first action",
        )

    assert recruiter.cmd_run_job(key, str(roster_path)) == 1

    assert drill.sentinel_commands == [command, command]
    hired = [
        item
        for item in ledger.requester_mailbox(key).read_all()
        if item.get("type") == "sentinel-hired"
    ]
    assert len(hired) == 2
    assert all(item["detail"]["worker_provider"] == worker_provider for item in hired)
    assert all(
        item["detail"]["sentinel_provider"] == sentinel_provider for item in hired
    )


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
    ledger, order, key, roster_path, drill = _drill_job(tmp_path, monkeypatch, git=True)
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


# --- Stall nudge ladder (hub-owned "continue") ---------------------------------


def _nudgeable(
    ledger: Any,
    order: dict,
    key: str,
    manifest: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[tuple[str, str]]]:
    """A STALLED-ready watch over a request with a live worker journal and a captured
    prompt channel: the preconditions under which the hub may nudge at all."""
    monkeypatch.setattr(
        recruiter,
        "_live_worker_journal",
        lambda *_args, **_kwargs: {
            "address": "worker-address",
            "attempt": 1,
            "generation": 1,
        },
    )
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_present", lambda *args, **kwargs: True
    )
    prompts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda address, message, **kwargs: prompts.append((address, message)),
    )
    closeout_path = recruiter._sentinel_closeout_path(ledger, key, 1)
    closeout_path.parent.mkdir(parents=True, exist_ok=True)
    watch = recruiter._SentinelWatch(
        ledger,
        key,
        order,
        manifest,
        closeout_path,
        {
            "address": "sentinel-address",
            "worker_pane": "worker-pane",
            "attempt": 1,
            "generation": 1,
        },
        herdr_session="default",
    )
    return watch, prompts


def _corroborated_stalled(order: dict) -> dict:
    evidence = Path(order["cwd"]) / "evidence.txt"
    evidence.write_text("progress\n")
    return _closeout(
        order,
        outcome="STALLED",
        bundle=None,
        citations=[str(evidence)],
        progress_so_far="halted after provider overload",
        last_alive="pulse 2",
    )


def test_a_confirmed_stall_over_a_live_worker_nudges_continue_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole feature in one line: a corroborated STALLED with a live worker journal
    delivers exactly the literal 'continue' to the worker's agent address, archives the
    closeout, prompts the Sentinel back to PULSE, and keeps the wait alive."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    assert not watch.closeout_path.is_file()
    assert watch.closeout_path.with_name("closeout.stalled-nudged-1.json").is_file()
    assert prompts[0] == ("worker-address", "continue")
    assert prompts[1][0] == "sentinel-address"
    assert "SENTINEL_STALL_NUDGED" in prompts[1][1]
    events = _events(ledger, key)
    assert events.index("worker-nudge-intent") < events.index("worker-nudge-delivered")
    intent = _event(ledger, key, "worker-nudge-intent")
    assert intent["nudge_index"] == 1
    assert intent["generation"] == 1
    assert intent["attempt"] == 1


def test_a_second_stall_inside_the_backoff_window_holds_without_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    worker_prompts = [item for item in prompts if item[0] == "worker-address"]
    assert len(worker_prompts) == 1
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    worker_prompts = [item for item in prompts if item[0] == "worker-address"]
    assert len(worker_prompts) == 1
    assert "worker-nudge-held" in _events(ledger, key)
    assert list(watch.closeout_path.parent.glob("closeout.stalled-held-2-*.json"))


def test_exhausted_nudges_escalate_once_to_the_requester_and_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    state_path = watch.closeout_path.with_name("nudges.json")
    state_path.write_text(
        json.dumps(
            {
                "nudges": [
                    {"at": 1.0, "digest": "d1", "delivered": True},
                    {"at": 2.0, "digest": "d2", "delivered": True},
                    {"at": 3.0, "digest": "d3", "delivered": False},
                ]
            }
        )
    )
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    escalation = _event(ledger, key, "worker-stall-escalation")
    assert escalation["nudges"] == 3
    mailbox = ledger.requester_mailbox(key).read_all()
    assert any(item["type"] == "worker-stall-escalation" for item in mailbox)
    assert not [item for item in prompts if item[0] == "worker-address"]


def test_a_nudge_is_rejected_in_requester_facing_and_terminal_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    state_file = ledger.request_dir(key) / "state" / "latest.json"
    snapshot = json.loads(state_file.read_text())
    snapshot["state"] = "awaiting-requester"
    state_file.write_text(json.dumps(snapshot))
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    rejected = _event(ledger, key, "worker-nudge-rejected")
    assert rejected["state"] == "awaiting-requester"
    assert not prompts


def test_a_failed_delivery_is_recorded_and_counts_toward_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, _prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)

    def _refuse(address: str, message: str, **kwargs: object) -> None:
        raise recruiter.RecruiterError("agent never went idle")

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", _refuse)
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    events = _events(ledger, key)
    assert "worker-nudge-intent" in events
    assert "worker-nudge-failed" in events
    assert "worker-nudge-delivered" not in events
    state = json.loads(watch.closeout_path.with_name("nudges.json").read_text())
    assert len(state["nudges"]) == 1
    assert state["nudges"][0]["delivered"] is False


def test_a_stall_without_a_live_worker_journal_raises_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *args, **kwargs: pytest.fail("no delivery without a live worker"),
    )
    watch = _stalled_watch_with_worker_pane(ledger, order, key, manifest)
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    assert "worker-nudge-intent" not in _events(ledger, key)


# --- Cross-provider sentinel selection (default-on) -----------------------------


def _management(**management: object) -> Any:
    return recruiter.llm_management.load_management_config({"management": management})


def _public_management() -> Any:
    roster = recruiter.load_roster(Path(__file__).with_name("offerings.yaml"))
    return recruiter.llm_management.load_management_config(roster)


def test_public_sentinel_candidates_preserve_order_and_filter_the_worker_provider() -> (
    None
):
    config = _public_management()

    anthropic = recruiter._resolve_sentinel_roles(
        _order(offering_snapshot={"provider": "anthropic"}), config
    )
    cursor = recruiter._resolve_sentinel_roles(
        _order(offering_snapshot={"provider": "cursor"}), config
    )
    openai = recruiter._resolve_sentinel_roles(
        _order(offering_snapshot={"provider": "openai"}), config
    )

    openrouter = recruiter._resolve_sentinel_roles(
        _order(offering_snapshot={"provider": "openrouter"}), config
    )

    assert [item.offering_id for item in anthropic] == [
        "pi-glm-5-3-flash",
        "cursor-composer-2-5",
        "pi-gpt-5-4-mini",
    ]
    assert [item.offering_id for item in cursor] == [
        "pi-glm-5-3-flash",
        "pi-gpt-5-4-mini",
    ]
    assert [item.offering_id for item in openai] == [
        "pi-glm-5-3-flash",
        "cursor-composer-2-5",
    ]
    assert [item.offering_id for item in openrouter] == [
        "cursor-composer-2-5",
        "pi-gpt-5-4-mini",
    ]


def test_sentinel_startup_failure_falls_back_in_candidate_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, _manifest = _claimed_request(tmp_path, monkeypatch)
    order["offering_snapshot"] = {"provider": "anthropic"}
    config = _public_management()
    selections = recruiter._resolve_sentinel_roles(order, config)
    attempted: list[str] = []

    def start(*args: object, **kwargs: object) -> dict[str, object]:
        role = args[5]
        attempted.append(role.expected_process)
        if "glm-5.3-flash" in role.command:
            raise recruiter.RecruiterError("glm startup refused")
        return {"pane": "sentinel-cursor"}

    monkeypatch.setattr(recruiter, "_start_sentinel", start)
    started, selected = recruiter._start_sentinel_candidates(
        ledger,
        key,
        "lease-token",
        order,
        config,
        selections,
        "worker-pane",
        tmp_path / "closeout.json",
        1,
        1,
        herdr_session="default",
        liftoff_deadline_ms=300_000,
    )

    assert attempted == ["pi", "cursor-agent"]
    assert started == {"pane": "sentinel-cursor"}
    assert selected.offering_id == "cursor-composer-2-5"
    failures = [
        item
        for item in ledger.events(key)
        if item["event"] == "sentinel-candidate-failed"
    ]
    assert [(item["offering_id"], item["reason"]) for item in failures] == [
        ("pi-glm-5-3-flash", "glm startup refused")
    ]


def test_sentinel_candidate_exhaustion_is_explicit_and_records_every_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, _manifest = _claimed_request(tmp_path, monkeypatch)
    order["offering_snapshot"] = {"provider": "anthropic"}
    config = _public_management()
    selections = recruiter._resolve_sentinel_roles(order, config)
    monkeypatch.setattr(
        recruiter,
        "_start_sentinel",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            recruiter.RecruiterError(f"{args[5].expected_process} failed")
        ),
    )

    with pytest.raises(recruiter.SentinelSelectionError) as raised:
        recruiter._start_sentinel_candidates(
            ledger,
            key,
            "lease-token",
            order,
            config,
            selections,
            "worker-pane",
            tmp_path / "closeout.json",
            1,
            2,
            herdr_session="default",
            liftoff_deadline_ms=300_000,
        )

    assert raised.value.reason_type == "sentinel-candidates-exhausted"
    assert "cursor-agent failed" in str(raised.value)
    assert "pi failed" in str(raised.value)
    failures = [
        item
        for item in ledger.events(key)
        if item["event"] == "sentinel-candidate-failed"
    ]
    assert [item["offering_id"] for item in failures] == [
        "pi-glm-5-3-flash",
        "cursor-composer-2-5",
        "pi-gpt-5-4-mini",
    ]
    assert all(item["attempt"] == 2 for item in failures)


def test_anthropic_worker_selects_the_openai_sentinel_command() -> None:
    selected = recruiter._resolve_sentinel_role(
        _order(offering_snapshot={"provider": "anthropic"}), _management()
    )

    assert selected.worker_provider == "anthropic"
    assert selected.sentinel_provider == "openai"
    assert (
        selected.role.command
        == recruiter.llm_management.DEFAULT_OPENAI_SENTINEL_COMMAND
    )


def test_openai_worker_selects_the_anthropic_sentinel_command() -> None:
    selected = recruiter._resolve_sentinel_role(
        _order(offering_snapshot={"provider": "openai"}), _management()
    )

    assert selected.worker_provider == "openai"
    assert selected.sentinel_provider == "anthropic"
    assert selected.role.command == recruiter.llm_management.DEFAULT_SENTINEL_COMMAND


def test_openrouter_worker_selects_the_anthropic_sentinel_command() -> None:
    selected = recruiter._resolve_sentinel_role(
        _order(offering_snapshot={"provider": "openrouter"}), _management()
    )

    assert selected.worker_provider == "openrouter"
    assert selected.sentinel_provider == "anthropic"
    assert selected.role.command == recruiter.llm_management.DEFAULT_SENTINEL_COMMAND
    assert set(_management().sentinels) == {"anthropic", "openai"}


@pytest.mark.parametrize(
    ("harness", "model", "worker_provider"),
    (
        ("codex", "gpt-5.6", "openai"),
        ("pi", "openrouter/z-ai/glm-5.3-flash", "openrouter"),
    ),
)
def test_worker_provider_falls_back_to_identity_only_without_snapshot_provider(
    harness: str, model: str, worker_provider: str
) -> None:
    selected = recruiter._resolve_sentinel_role(
        _order(harness=harness, model=model), _management()
    )

    assert selected.worker_provider == worker_provider
    assert selected.sentinel_provider == "anthropic"


def test_unknown_pinned_worker_provider_degrades_with_a_typed_reason() -> None:
    with pytest.raises(recruiter.SentinelSelectionError) as raised:
        recruiter._resolve_sentinel_role(
            _order(
                harness="claude",
                model="claude-sonnet-5",
                offering_snapshot={"provider": "unknown"},
            ),
            _management(),
        )

    assert raised.value.reason_type == "worker-provider-unknown"
    assert "unknown" in str(raised.value)


def test_missing_opposite_provider_role_degrades_with_a_typed_reason() -> None:
    config = _management()
    config.sentinels.pop("openai")

    with pytest.raises(recruiter.SentinelSelectionError) as raised:
        recruiter._resolve_sentinel_role(
            _order(offering_snapshot={"provider": "anthropic"}), config
        )

    assert raised.value.reason_type == "opposite-provider-sentinel-unavailable"
    assert "openai" in str(raised.value)


def test_a_stall_over_a_gone_worker_pane_never_nudges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live launch journal is not liveness: only a POSITIVELY present worker pane
    may receive a nudge — a gone pane keeps the exact pre-ladder blocked path."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_present", lambda *args, **kwargs: False
    )
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    assert "worker-nudge-intent" not in _events(ledger, key)
    assert not prompts


# --- Stall nudge: adversarial-review hardening ----------------------------------


def test_a_journal_from_another_attempt_or_generation_is_never_nudged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap fence: the started worker journal must match the watch's own attempt
    and generation exactly — a replacement worker keeps the pre-ladder behavior."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    monkeypatch.setattr(
        recruiter,
        "_live_worker_journal",
        lambda *_a, **_k: {"address": "worker-address", "attempt": 2, "generation": 2},
    )
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    assert "worker-nudge-intent" not in _events(ledger, key)
    assert not prompts


def test_an_unreadable_ledger_state_fails_closed_and_never_nudges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    state_file = ledger.request_dir(key) / "state" / "latest.json"
    state_file.write_text("{ not json")
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    assert "worker-nudge-intent" not in _events(ledger, key)
    assert not prompts


def test_a_mechanically_valid_bundle_supersedes_the_stall_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completion wins the race: when the staged bundle already validates, a STALLED
    closeout must never resume the finished worker — the wait continues into the
    ordinary completion path with the closeout archived, no nudge spent."""
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    _stage_valid_result(manifest, order)
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    assert not watch.closeout_path.is_file()
    assert watch.closeout_path.with_name("closeout.stalled-superseded.json").is_file()
    assert "worker-nudge-superseded" in _events(ledger, key)
    assert "worker-nudge-intent" not in _events(ledger, key)
    assert not [item for item in prompts if item[0] == "worker-address"]


def test_a_corrupt_nudge_state_falls_through_to_the_pre_ladder_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, _prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    watch.closeout_path.with_name("nudges.json").write_text("{ not json")
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    with pytest.raises(recruiter.SentinelStalledError):
        watch.poll()
    assert "worker-nudge-state-invalid" in _events(ledger, key)


def test_exhaustion_escalates_exactly_once_across_repeated_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, _prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    state_path = watch.closeout_path.with_name("nudges.json")
    state_path.write_text(
        json.dumps(
            {
                "nudges": [
                    {"at": 1.0, "digest": "d1", "delivered": True},
                    {"at": 2.0, "digest": "d2", "delivered": True},
                    {"at": 3.0, "digest": "d3", "delivered": True},
                ]
            }
        )
    )
    for _ in range(2):
        watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
        with pytest.raises(recruiter.SentinelStalledError):
            watch.poll()
    events = _events(ledger, key)
    assert events.count("worker-stall-escalation") == 1
    mailbox = ledger.requester_mailbox(key).read_all()
    assert (
        len([item for item in mailbox if item["type"] == "worker-stall-escalation"])
        == 1
    )
    assert json.loads(state_path.read_text()).get("escalated") is True


def test_repeated_held_closeouts_keep_distinct_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, _prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    for _ in range(2):
        watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
        assert watch.poll() is None
    held = list(watch.closeout_path.parent.glob("closeout.stalled-held-*.json"))
    assert len(held) == 2


def test_a_failed_delivery_is_reported_honestly_to_the_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    watch, _prompts = _nudgeable(ledger, order, key, manifest, monkeypatch)
    prompts: list[tuple[str, str]] = []

    def _refuse_worker(address: str, message: str, **kwargs: object) -> None:
        if address == "worker-address":
            raise recruiter.RecruiterError("agent never went idle")
        prompts.append((address, message))

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", _refuse_worker)
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    assert prompts and prompts[0][0] == "sentinel-address"
    assert "delivery failed" in prompts[0][1]
    assert "delivered" not in prompts[0][1].replace("delivery failed", "")


def test_a_cursor_worker_nudge_uses_the_paste_settle_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, order, key, manifest = _claimed_request(tmp_path, monkeypatch)
    order["harness"] = "cursor"
    calls: list[dict] = []
    monkeypatch.setattr(
        recruiter,
        "_live_worker_journal",
        lambda *_a, **_k: {"address": "worker-address", "attempt": 1, "generation": 1},
    )
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_present", lambda *a, **k: True
    )
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda address, message, **kwargs: calls.append({"address": address, **kwargs}),
    )
    closeout_path = recruiter._sentinel_closeout_path(ledger, key, 1)
    closeout_path.parent.mkdir(parents=True, exist_ok=True)
    watch = recruiter._SentinelWatch(
        ledger,
        key,
        order,
        manifest,
        closeout_path,
        {
            "address": "sentinel-address",
            "worker_pane": "worker-pane",
            "attempt": 1,
            "generation": 1,
        },
        herdr_session="default",
    )
    watch.closeout_path.write_text(json.dumps(_corroborated_stalled(order)))
    assert watch.poll() is None
    worker_calls = [c for c in calls if c["address"] == "worker-address"]
    assert worker_calls[0]["paste_settle_seconds"] == (
        recruiter.CURSOR_PROMPT_PASTE_SETTLE_SECONDS
    )
    assert worker_calls[0]["idle_timeout_ms"] == recruiter.NUDGE_PROMPT_IDLE_TIMEOUT_MS


def test_an_override_violating_disjointness_degrades_with_a_typed_reason() -> None:
    config = _management(
        sentinel={
            "command": "claude --agent upagent-sentinel --model haiku {brief_path}",
            "expected_agent": "claude",
            "expected_process": "claude",
        }
    )

    with pytest.raises(recruiter.SentinelSelectionError) as raised:
        recruiter._resolve_sentinel_role(
            _order(offering_snapshot={"provider": "anthropic"}), config
        )

    assert raised.value.reason_type == "sentinel-provider-conflict"
    assert "anthropic" in str(raised.value)


def test_a_disjoint_override_is_honored_as_is() -> None:
    config = _management(
        sentinel={
            "command": "claude --agent upagent-sentinel --model haiku {brief_path}",
            "expected_agent": "claude",
            "expected_process": "claude",
        }
    )

    selected = recruiter._resolve_sentinel_role(
        _order(offering_snapshot={"provider": "openai"}), config
    )

    assert selected.role is config.sentinel
    assert selected.sentinel_provider == "anthropic"


def test_the_sentinel_brief_makes_stalled_provisional() -> None:
    brief = recruiter.llm_management.sentinel_brief(
        "req-id",
        "order-id",
        "w1:p1",
        "/tmp/wt",
        Path("/tmp/closeout.json"),
        liftoff_deadline_ms=300_000,
        wake_path=Path("/tmp/wake"),
    )
    assert "SENTINEL_STALL_NUDGED" in brief
    assert "PROVISIONAL" in brief


def test_an_override_with_unprovable_provider_degrades_typed() -> None:
    """A management.sentinel command override whose executable proves no provider
    identity must raise the typed sentinel-provider-unknown selection error —
    never an approved hire, never a silent fallback."""
    config = recruiter.llm_management.load_management_config(
        {
            "management": {
                "sentinel": {
                    "command": "/opt/custom/mystery-watcher {brief_path} {output_path} {cwd}"
                }
            }
        }
    )
    assert config.sentinel_is_override
    with pytest.raises(recruiter.SentinelSelectionError) as caught:
        recruiter._resolve_sentinel_role(
            _order(harness="codex", model="gpt-5.6"), config
        )
    assert caught.value.reason_type == "sentinel-provider-unknown"
