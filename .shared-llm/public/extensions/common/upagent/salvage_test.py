# pyright: reportMissingImports=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportArgumentType=false
"""Unit tests for mechanical salvage and the on-demand Rescuer.

A worker whose pane vanished before it could publish still leaves its work on disk. These
tests pin the two halves of recovering it: the mechanical inspection that decides from files
and the ledger alone, and the Rescuer whose every citation must re-verify before it counts.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/salvage_test.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "upagent_recruiter", Path(__file__).with_name("recruiter.py")
)
assert _spec and _spec.loader
recruiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recruiter)
ContractError = recruiter.ContractError


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


def _dead_runner_claim(
    tmp_path: Path, ledger: Any, suffix: str
) -> tuple[dict, Path, str]:
    """One owned order whose runner PID is dead, so reconciliation must terminalize it."""
    instructions = tmp_path / f"instructions-{suffix}.md"
    instructions.write_text("# Worker\n")
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / f"result-{suffix}.json"),
    )
    order_path = tmp_path / f"order-{suffix}.json"
    order_path.write_text(json.dumps(order))
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
    return order, order_path, key


def _git_repo(path: Path) -> None:
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Salvage Test"],
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


def _manifest(ledger: Any, key: str, order: dict) -> Any:
    lease = json.loads((ledger.active / "requests" / key / "lease.json").read_text())
    return recruiter.completion.build_manifest(
        order,
        ledger.request_dir(key),
        lease["token"],
        recruiter.lifecycle.request_identity(order),
    )


def _staging_result_path(ledger: Any, key: str, order: dict) -> Path:
    return _manifest(ledger, key, order).artifact("result").staging_path


def _reconciled_receipt(order_path: Path, capsys: Any) -> dict:
    assert recruiter.cmd_await(str(order_path), notify_after_ms=0) == 1
    line = capsys.readouterr().out.strip().splitlines()[0]
    return json.loads(line.removeprefix("ORDER_RECEIPT "))


# --- Phase 1: mechanical salvage --------------------------------------------


def test_reconciliation_salvages_an_order_whose_commit_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The lost-commit case: the worker's self-report died, its commit did not."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    _git_repo(tmp_path)
    order, order_path, key = _dead_runner_claim(tmp_path, ledger, "salvage")
    assert not _staging_result_path(ledger, key, order).exists()
    sha = _commit(tmp_path, "landed.txt")

    receipt = _reconciled_receipt(order_path, capsys)

    assert receipt["verdict"] == "salvaged-done"
    assert receipt["synthesis_path"] == "salvaged-mechanical"
    assert receipt["confirmation"] == "unconfirmed"
    evidence = receipt["salvage_evidence"]
    assert evidence["outcome"] == "salvageable"
    assert evidence["staged_result_state"] == "absent"
    assert [item["sha"] for item in evidence["landed_commits"]] == [sha]
    assert evidence["landed_commits"][0]["author_name"] == "Salvage Test"
    assert evidence["landed_commits"][0]["author_email"] == "test@example.invalid"
    published = json.loads(Path(receipt["published_result_path"]).read_text())
    assert published["verdict"] == "salvaged-done"
    assert published["confirmation"] == "unconfirmed"
    assert sha in published["reason"]
    assert "authorship not verified" in published["reason"]


def test_reconciliation_blocks_when_nothing_reached_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A clean miss keeps the pre-existing blocked terminal and the clean synthesis path."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    _order_value, order_path, _key = _dead_runner_claim(tmp_path, ledger, "empty")

    receipt = _reconciled_receipt(order_path, capsys)

    assert receipt["verdict"] == "blocked"
    assert receipt["synthesis_path"] == "clean"
    assert receipt["confirmation"] == "confirmed"
    assert receipt["salvage_evidence"]["outcome"] == "empty"
    assert receipt["salvage_evidence"]["git_worktree"] is None


def test_untimed_order_never_counts_pre_existing_history_as_salvage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a recorded start there is no window, so git contributes NO evidence."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    _git_repo(tmp_path)
    _commit(tmp_path, "ancient.txt")
    order, _order_path, key = _dead_runner_claim(tmp_path, ledger, "untimed")
    for event in (ledger.request_dir(key) / "events").iterdir():
        event.unlink()

    evidence = recruiter.inspect_salvage(
        ledger, key, order, _manifest(ledger, key, order)
    )

    assert evidence["order_started_at_ns"] is None
    assert evidence["landed_commits"] == []
    assert evidence["outcome"] == "empty"


def test_clean_salvage_outcomes_never_hire_the_rescuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Neither a clean hit nor a clean miss may spend a model on an unambiguous answer."""

    def refuse(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("the rescuer was hired for an unambiguous inspection")

    monkeypatch.setattr(recruiter, "_rescue_ambiguous_salvage", refuse)
    hit = tmp_path / "hit"
    hit.mkdir()
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(hit / "hub"))
    hit_ledger = recruiter.JobLedger()
    _git_repo(hit)
    _order_a, hit_path, _hit_key = _dead_runner_claim(hit, hit_ledger, "hit")
    _commit(hit, "hit.txt")
    assert _reconciled_receipt(hit_path, capsys)["verdict"] == "salvaged-done"

    miss = tmp_path / "miss"
    miss.mkdir()
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(miss / "hub"))
    miss_ledger = recruiter.JobLedger()
    _order_b, miss_path, _miss_key = _dead_runner_claim(miss, miss_ledger, "miss")
    assert _reconciled_receipt(miss_path, capsys)["verdict"] == "blocked"


def test_the_live_reactor_salvages_a_dead_worker_whose_commit_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real route for a mid-wait pane death: the order terminalizes through the
    completion reactor and never reaches the reconciler. Salvage must triage here too."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    _git_repo(tmp_path)
    order, _order_path, key = _dead_runner_claim(tmp_path, ledger, "reactor")
    manifest = _manifest(ledger, key, order)
    staging = manifest.artifact("result").staging_path
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(json.dumps({"order_id": order["order_id"]}))
    sha = _commit(tmp_path, "reactor-landed.txt")

    # No worker address: the one allowed same-worker repair cannot be sent, which is exactly
    # what a vanished pane looks like to the reactor.
    blocked, salvage = recruiter._complete_typed_bundle(
        ledger, key, order, manifest, None, herdr_session="test-session"
    )

    assert blocked is True
    assert salvage is not None
    assert salvage["result"]["verdict"] == "salvaged-done"
    assert salvage["finalize_kwargs"]["allow_synthesized"] is True
    assert salvage["finalize_kwargs"]["synthesis_path"] == "salvaged-mechanical"
    assert salvage["finalize_kwargs"]["confirmation"] == "unconfirmed"
    assert sha in salvage["result"]["reason"]
    assert json.loads(staging.read_text())["verdict"] == "salvaged-done"


def test_the_live_reactor_still_blocks_when_no_work_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _dead_runner_claim(tmp_path, ledger, "reactor-empty")
    manifest = _manifest(ledger, key, order)
    staging = manifest.artifact("result").staging_path
    staging.parent.mkdir(parents=True, exist_ok=True)

    blocked, salvage = recruiter._complete_typed_bundle(
        ledger, key, order, manifest, None, herdr_session="test-session"
    )

    assert blocked is True
    # No mechanical evidence survived, so the order stays blocked — but the inspection still
    # ran, and its evidence rides along on `salvage` so the receipt can cite what was checked.
    assert salvage is not None
    assert set(salvage["finalize_kwargs"]) == {"salvage_evidence"}
    assert salvage["finalize_kwargs"]["salvage_evidence"]["outcome"] != "salvageable"
    assert json.loads(staging.read_text())["verdict"] == "blocked"


def test_live_path_blocked_receipt_after_inspection_carries_salvage_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published receipt, not just `_complete_typed_bundle`'s return, keeps the evidence.

    Reproduces the exact finalize fork `cmd_run_job` uses: `salvage["result"]` is published
    when the reactor already authored a bundle from inspection, and `salvage["finalize_kwargs"]`
    (here just `salvage_evidence`, since nothing was actually salvaged) rides along. Before the
    fix this order's `salvage` was `None` here, so those kwargs never reached `finalize`.
    """
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _dead_runner_claim(tmp_path, ledger, "reactor-empty-live")
    manifest = _manifest(ledger, key, order)
    staging = manifest.artifact("result").staging_path
    staging.parent.mkdir(parents=True, exist_ok=True)
    token = json.loads((ledger.active / "requests" / key / "lease.json").read_text())["token"]

    blocked, salvage = recruiter._complete_typed_bundle(
        ledger, key, order, manifest, None, herdr_session="test-session"
    )
    assert blocked is True
    assert salvage is not None

    # Same fork `cmd_run_job` runs right before its `ledger.finalize` call.
    result = salvage["result"] if salvage is not None else json.loads(staging.read_text())
    finalized = ledger.finalize(
        key,
        token,
        order,
        result,
        cleanup={"verified_absent": True},
        defer_runner_completion=True,
        exit_code=1,
        completion_source="result-or-agent-status",
        **({} if salvage is None else salvage["finalize_kwargs"]),
    )
    assert finalized is True

    receipt = json.loads((ledger.request_dir(key) / "receipt.json").read_text())
    assert receipt["verdict"] == "blocked"
    assert receipt["salvage_evidence"]["outcome"] != "salvageable"


# --- Phase 2: on-demand Rescuer ----------------------------------------------


def _ambiguous_claim(
    tmp_path: Path, ledger: Any, suffix: str
) -> tuple[dict, Path, str]:
    """A dead order whose staged result exists but will not validate."""
    order, order_path, key = _dead_runner_claim(tmp_path, ledger, suffix)
    staging = _staging_result_path(ledger, key, order)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(json.dumps({"order_id": order["order_id"]}))
    return order, order_path, key


def test_only_contradictory_evidence_is_marked_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _ambiguous_claim(tmp_path, ledger, "ambiguous")

    evidence = recruiter.inspect_salvage(
        ledger, key, order, _manifest(ledger, key, order)
    )

    assert evidence["outcome"] == "ambiguous"
    assert evidence["staged_result_state"] == "invalid"


def _rescued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: dict,
    *,
    worktree: str | None,
) -> dict:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _ambiguous_claim(tmp_path, ledger, "rescue")

    def fake_run(_config: Any, _brief: Path, _cwd: str, output_path: Path) -> None:
        output_path.write_text(json.dumps(verdict))

    monkeypatch.setattr(recruiter, "_run_rescuer", fake_run)
    evidence = {
        "outcome": "ambiguous",
        "synthesis_path": "salvaged-mechanical",
        "staged_result_path": str(_staging_result_path(ledger, key, order)),
        "staged_result_state": "invalid",
        "git_worktree": worktree,
        "landed_commits": [],
        "ledger_artifacts_written": [],
        "order_started_at_ns": 1,
    }
    return recruiter._rescue_ambiguous_salvage(ledger, key, order, evidence)


def _rescuer_order(tmp_path: Path) -> dict:
    return _order(cwd=str(tmp_path), result_path=str(tmp_path / "result-rescue.json"))


def test_rescuer_salvage_is_accepted_only_after_every_citation_re_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git_repo(work)
    sha = _commit(work, "rescued.txt")
    cited_file = work / "artifact.json"
    cited_file.write_text(json.dumps({"ok": True}))
    order = _rescuer_order(tmp_path)

    rescued = _rescued(
        tmp_path,
        monkeypatch,
        {
            "request_id": recruiter.lifecycle.request_identity(order),
            "order_id": order["order_id"],
            "verdict": "salvageable-done",
            "cited_commits": [sha],
            "cited_files": [str(cited_file)],
            "message": "the fix commit landed and its artifact parses",
        },
        worktree=str(work),
    )

    assert rescued["outcome"] == "salvageable"
    assert rescued["synthesis_path"] == "salvaged-rescuer"
    assert rescued["rescuer_uncorroborated"] == []
    assert any(sha in fact for fact in rescued["rescuer_corroborated"])


def test_file_only_citation_is_never_sufficient_to_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corroborated file with no corroborated commit must still be rejected.

    Otherwise a worker that writes a contract-invalid-but-parseable result.json forces this
    `ambiguous` path, and any file the Rescuer can point at (even one it did not write) would
    be enough to mark the order done without a single commit ever having landed.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git_repo(work)
    _commit(work, "unrelated.txt")
    cited_file = work / "artifact.json"
    cited_file.write_text(json.dumps({"ok": True}))
    order = _rescuer_order(tmp_path)

    rescued = _rescued(
        tmp_path,
        monkeypatch,
        {
            "request_id": recruiter.lifecycle.request_identity(order),
            "order_id": order["order_id"],
            "verdict": "salvageable-done",
            "cited_commits": [],
            "cited_files": [str(cited_file)],
            "message": "the artifact parses, no commit was cited",
        },
        worktree=str(work),
    )

    assert rescued["outcome"] == "ambiguous"
    assert rescued["synthesis_path"] == "salvaged-mechanical"
    assert rescued["rescuer_uncorroborated"] == []
    assert any("artifact.json" in fact for fact in rescued["rescuer_corroborated"])


def test_citing_the_order_own_staging_file_never_corroborates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cited file inside the order's staging directory is worker-authored bytes.

    Without this, a worker that writes a contract-invalid-but-parseable result.json into its
    own staging directory forces the `ambiguous` path, and a Rescuer could then cite that very
    staged file to satisfy the corroboration gate with the worker's own output. Even with a
    real corroborated commit also cited, the staging-dir citation keeps the order `ambiguous`
    — the gate rejects the WHOLE verdict when any citation fails to corroborate.
    """
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _ambiguous_claim(tmp_path, ledger, "staging-cite")
    staging_file = _staging_result_path(ledger, key, order)
    _git_repo(tmp_path)
    sha = _commit(tmp_path, "rescued.txt")

    def fake_run(_config: Any, _brief: Path, _cwd: str, output_path: Path) -> None:
        output_path.write_text(
            json.dumps(
                {
                    "request_id": recruiter.lifecycle.request_identity(order),
                    "order_id": order["order_id"],
                    "verdict": "salvageable-done",
                    "cited_commits": [sha],
                    "cited_files": [str(staging_file)],
                    "message": "the staged result.json holds the answer",
                }
            )
        )

    monkeypatch.setattr(recruiter, "_run_rescuer", fake_run)
    evidence = {
        "outcome": "ambiguous",
        "synthesis_path": "salvaged-mechanical",
        "staged_result_path": str(staging_file),
        "staged_result_state": "invalid",
        "git_worktree": str(tmp_path),
        "landed_commits": [],
        "ledger_artifacts_written": [],
        "order_started_at_ns": 1,
    }

    rescued = recruiter._rescue_ambiguous_salvage(ledger, key, order, evidence)

    assert rescued["outcome"] == "ambiguous"
    assert rescued["synthesis_path"] == "salvaged-mechanical"
    assert any(
        "own staging directory" in fact for fact in rescued["rescuer_uncorroborated"]
    )
    assert any(sha in fact for fact in rescued["rescuer_corroborated"])


def test_uncorroborated_rescuer_claim_is_recorded_and_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt-forgery lesson: an unverifiable citation never marks work done."""
    work = tmp_path / "work"
    work.mkdir()
    _git_repo(work)
    _commit(work, "real.txt")
    order = _rescuer_order(tmp_path)

    rescued = _rescued(
        tmp_path,
        monkeypatch,
        {
            "request_id": recruiter.lifecycle.request_identity(order),
            "order_id": order["order_id"],
            "verdict": "salvageable-done",
            "cited_commits": ["0" * 40],
            "cited_files": [str(tmp_path / "never-written.json")],
            "message": "trust me, it landed",
        },
        worktree=str(work),
    )

    assert rescued["outcome"] == "ambiguous"
    assert rescued["synthesis_path"] == "salvaged-mechanical"
    assert rescued["rescuer_corroborated"] == []
    assert len(rescued["rescuer_uncorroborated"]) == 2
    assert any("does not resolve" in fact for fact in rescued["rescuer_uncorroborated"])
    assert any("does not exist" in fact for fact in rescued["rescuer_uncorroborated"])


def test_a_rescuer_verdict_other_than_salvageable_done_is_never_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git_repo(work)
    sha = _commit(work, "real.txt")
    for verdict in ("truly-blocked", "rerun"):
        # A fresh hub per iteration: one ledger key may only be claimed once.
        base = tmp_path / f"run-{verdict}"
        base.mkdir()
        order = _rescuer_order(base)
        rescued = _rescued(
            base,
            monkeypatch,
            {
                "request_id": recruiter.lifecycle.request_identity(order),
                "order_id": order["order_id"],
                "verdict": verdict,
                "cited_commits": [sha],
                "cited_files": [],
                "message": "a real commit, but not this order's deliverable",
            },
            worktree=str(work),
        )
        assert rescued["outcome"] == "ambiguous"
        assert rescued["rescuer_verdict"] == verdict


def test_rescuer_verdict_identity_and_vocabulary_are_enforced() -> None:
    good = {
        "request_id": "req-1",
        "order_id": "order-1",
        "verdict": "truly-blocked",
        "cited_commits": [],
        "cited_files": [],
        "message": "nothing survived",
    }
    assert recruiter.parse_rescuer_verdict(json.dumps(good), "req-1", "order-1")

    with pytest.raises(ContractError, match="does not match"):
        recruiter.parse_rescuer_verdict(json.dumps(good), "req-2", "order-1")
    with pytest.raises(ContractError, match="must be one of"):
        recruiter.parse_rescuer_verdict(
            json.dumps({**good, "verdict": "done"}), "req-1", "order-1"
        )
    with pytest.raises(ContractError, match="cited_commits"):
        recruiter.parse_rescuer_verdict(
            json.dumps({**good, "cited_commits": "abc"}), "req-1", "order-1"
        )
    with pytest.raises(ContractError, match="non-empty `message`"):
        recruiter.parse_rescuer_verdict(
            json.dumps({**good, "message": "  "}), "req-1", "order-1"
        )


def test_an_unavailable_rescuer_names_its_reason_and_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise recruiter.RecruiterError("rescuer command exited 127: claude: not found")

    monkeypatch.setattr(recruiter, "_run_rescuer", explode)
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _ambiguous_claim(tmp_path, ledger, "unavailable")
    evidence = {
        "outcome": "ambiguous",
        "synthesis_path": "salvaged-mechanical",
        "staged_result_path": str(_staging_result_path(ledger, key, order)),
        "staged_result_state": "invalid",
        "git_worktree": None,
        "landed_commits": [],
        "ledger_artifacts_written": [],
        "order_started_at_ns": 1,
    }

    rescued = recruiter._rescue_ambiguous_salvage(ledger, key, order, evidence)

    assert rescued["outcome"] == "ambiguous"
    assert "claude: not found" in rescued["rescuer_unavailable"]
    assert any(
        item["event"] == "salvage-rescuer-unavailable" for item in ledger.events(key)
    )


# --- Phase 3: fail loud on a stale cockpit pane ------------------------------


def test_intake_refuses_a_request_whose_cockpit_pane_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rejection names the pane, at intake, before anything is launched."""
    probed: list[tuple[str, int]] = []

    def gone(pane: str, *, herdr_session: str | None = None, confirmations: int = 1):
        probed.append((pane, confirmations))
        return True

    monkeypatch.setattr(recruiter, "_worker_pane_confirmed_gone", gone)

    with pytest.raises(recruiter.RecruiterError) as caught:
        recruiter.verify_cockpit_pane(_order(cockpit_pane="w1:p1"))

    assert "cockpit_pane_not_found: w1:p1" in str(caught.value)
    assert "leader-restamp" in str(caught.value)
    assert "just upagent up" not in str(caught.value)
    # Two confirmations: a transport fault must never read as a missing pane.
    assert probed == [("w1:p1", 2)]


def test_public_stale_pane_error_names_same_id_ad_hoc_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recruiter, "_worker_pane_confirmed_gone", lambda *args, **kwargs: True
    )

    with pytest.raises(recruiter.RecruiterError) as caught:
        recruiter.verify_cockpit_pane(
            _order(cockpit_pane="w1:p1", public_request={"type": "worker"})
        )

    message = str(caught.value)
    assert "just upagent up" in message
    assert "SAME request id" in message
    assert '--cockpit-pane "$HERDR_PANE_ID"' in message
    assert "leader-restamp" not in message


def test_intake_accepts_a_request_whose_cockpit_pane_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recruiter, "_worker_pane_confirmed_gone", lambda *a, **k: False)
    assert recruiter.verify_cockpit_pane(_order(cockpit_pane="w1:p2")) is None


def test_the_request_door_rejects_a_stale_pane_before_reaching_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "_require_hub_authority", lambda: None)
    roster = recruiter.load_roster(Path(__file__).with_name("upagent.yaml"))
    monkeypatch.setattr(recruiter, "load_roster", lambda _path: roster)
    monkeypatch.setattr(recruiter, "_worker_pane_confirmed_gone", lambda *a, **k: True)
    monkeypatch.setattr(
        recruiter,
        "_spawn_job",
        lambda *a, **k: pytest.fail("a stale-pane request must never launch a worker"),
    )
    order = _order(cwd=str(tmp_path), result_path=str(tmp_path / "result.json"))
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(order))

    with pytest.raises(recruiter.RecruiterError, match="cockpit_pane_not_found: 1-1"):
        recruiter.cmd_request_strict(str(order_path), "roster.yaml")

    assert not recruiter.JobLedger().requests.exists()


def test_a_launch_failure_carries_its_root_cause_chain_to_the_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal message alone used to hide the real cause in the supervisor log."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    instructions = tmp_path / "instructions.md"
    instructions.write_text("# Worker\n")
    order = _order(
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / "result.json"),
    )
    key, _created = ledger.submit(order)

    try:
        try:
            raise recruiter.RecruiterError(
                "herdr pane get w1:p1 -> pane_not_found"
            )
        except recruiter.RecruiterError as inner:
            raise RuntimeError("could not create the worker agent") from inner
    except RuntimeError as error:
        assert recruiter._terminalize_start_failure(
            ledger,
            key,
            order,
            {
                "generation": 1,
                "request_id": recruiter.lifecycle.request_identity(order),
                "runner_pid": os.getpid(),
                "runner_start_time": None,
            },
            error,
        )

    receipt = ledger.completed_receipt(key, order)
    assert receipt["error_chain"] == [
        "RuntimeError: could not create the worker agent",
        "RecruiterError: herdr pane get w1:p1 -> pane_not_found",
    ]
    published = json.loads(Path(receipt["published_result_path"]).read_text())
    assert "could not create the worker agent" in published["reason"]
    assert "pane_not_found" in published["reason"]


def test_error_chain_walks_causes_and_survives_a_cycle() -> None:
    try:
        try:
            raise ValueError("root")
        except ValueError as inner:
            raise KeyError("middle") from inner
    except KeyError as error:
        chain = recruiter.error_chain(error)

    assert chain == ["KeyError: 'middle'", "ValueError: root"]

    lonely = OSError("only one")
    assert recruiter.error_chain(lonely) == ["OSError: only one"]


# --- Phase 4: consult close-out inversion + leader restamp -------------------


def test_a_consult_worker_idles_instead_of_closing_its_own_pane() -> None:
    """Regression guard: consults already travel through the close-out inversion."""
    brief = recruiter.build_consult_brief(
        {"consult_id": "c-1", "specialist": "backend", "question": "why?"},
        "/repo/specialists/backend.md",
        "/repo",
    )

    assert "go idle" in brief
    assert "do NOT exit or close your pane" in brief
    assert "the Recruiter validates your files and closes the pane" in brief


def _phase_start(tmp_path: Path, leader_pane: str) -> Path:
    path = tmp_path / "phase-start.json"
    path.write_text(
        json.dumps(
            {"phase_id": "phase-2", "leader_pane": leader_pane, "state": "ready"}
        )
    )
    return path


def test_leader_restamp_rewrites_the_receipt_and_logs_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(
        recruiter, "_herdr_json", lambda *a, **k: {"result": {"pane": {"id": "w1:p2"}}}
    )
    path = _phase_start(tmp_path, "w1:p1")

    assert recruiter.cmd_leader_restamp(str(path), "w1:p2") == 0

    assert json.loads(path.read_text())["leader_pane"] == "w1:p2"
    # Untouched fields survive the rewrite.
    assert json.loads(path.read_text())["phase_id"] == "phase-2"
    printed = json.loads(
        capsys.readouterr().out.strip().removeprefix("LEADER_RESTAMPED ")
    )
    assert printed["previous"] == "w1:p1"
    assert printed["current"] == "w1:p2"
    logged = json.loads(
        (recruiter.JobLedger().root / "leader-restamp.log").read_text().strip()
    )
    assert logged["previous"] == "w1:p1"
    assert logged["current"] == "w1:p2"


def test_leader_restamp_refuses_a_pane_it_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing an unverified id would strand the run exactly as the drift did."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))

    def missing(*_args: object, **_kwargs: object):
        raise recruiter.RecruiterError("pane_not_found")

    monkeypatch.setattr(recruiter, "_herdr_json", missing)
    path = _phase_start(tmp_path, "w1:p1")

    with pytest.raises(recruiter.RecruiterError, match="restamp refused"):
        recruiter.cmd_leader_restamp(str(path), "w1:p9")

    assert json.loads(path.read_text())["leader_pane"] == "w1:p1"


def test_leader_restamp_refuses_an_empty_pane_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(recruiter, "_herdr_json", lambda *a, **k: {"result": {}})
    path = _phase_start(tmp_path, "w1:p1")

    with pytest.raises(recruiter.RecruiterError, match="no pane record"):
        recruiter.cmd_leader_restamp(str(path), "w1:p9")

    assert json.loads(path.read_text())["leader_pane"] == "w1:p1"


def test_leader_restamp_rejects_a_receipt_with_no_leader_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(
        recruiter, "_herdr_json", lambda *a, **k: {"result": {"pane": {"id": "w1:p1"}}}
    )
    path = tmp_path / "phase-start.json"
    path.write_text(json.dumps({"phase_id": "phase-2"}))

    with pytest.raises(recruiter.RecruiterError, match="no leader_pane"):
        recruiter.cmd_leader_restamp(str(path), "w1:p1")


def test_a_worker_can_never_author_a_salvaged_verdict_itself() -> None:
    forged = json.dumps(
        {
            "order_id": "order-1",
            "verdict": "salvaged-done",
            "reason": "I salvaged myself",
            "full_log": "none",
        }
    )
    with pytest.raises(ContractError, match="must be one of passed, failed, blocked"):
        recruiter.parse_result(forged, expected_order_id="order-1")
    assert (
        recruiter.parse_result(
            forged, expected_order_id="order-1", allow_synthesized=True
        )["verdict"]
        == "salvaged-done"
    )


# --- Exec-style completion (codex) and interactive repair (cursor) -----------


def _exec_dead_claim(
    tmp_path: Path, ledger: Any, suffix: str, harness: str
) -> tuple[dict, Path, str]:
    instructions = tmp_path / f"instructions-{suffix}.md"
    instructions.write_text("# Worker\n")
    order = _order(
        harness=harness,
        cwd=str(tmp_path),
        instructions_path=str(instructions),
        result_path=str(tmp_path / f"result-{suffix}.json"),
    )
    order_path = tmp_path / f"order-{suffix}.json"
    order_path.write_text(json.dumps(order))
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
    return order, order_path, key


def test_codex_exec_missing_bundle_never_prompts_the_absent_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Codex exec worker disappears, so a stale address cannot authorize repair."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an exec-style worker was prompted for a live repair")

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", refuse)
    ledger = recruiter.JobLedger()
    order, _order_path, key = _exec_dead_claim(
        tmp_path, ledger, "exec-codex", "codex"
    )
    manifest = _manifest(ledger, key, order)
    staging = manifest.artifact("result").staging_path
    staging.parent.mkdir(parents=True, exist_ok=True)

    blocked, salvage = recruiter._complete_typed_bundle(
        ledger, key, order, manifest, "sess:stale-worker-address",
        herdr_session="test-session",
    )

    assert blocked is True
    assert salvage is not None
    published = json.loads(staging.read_text())
    assert published["verdict"] == "blocked"
    assert "agent_not_found" not in published["reason"]
    assert "exec-style" in published["reason"]
    events = [item["event"] for item in ledger.events(key)]
    assert "completion-repair-unavailable" in events


def test_codex_exec_valid_bundle_completes_without_any_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a valid exec bundle must never trigger a repair")

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", refuse)
    ledger = recruiter.JobLedger()
    order, _order_path, key = _exec_dead_claim(tmp_path, ledger, "exec-valid", "codex")
    manifest = _manifest(ledger, key, order)
    staging = manifest.artifact("result").staging_path
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(
        json.dumps(
            {
                "order_id": order["order_id"],
                "verdict": "passed",
                "revisit": [],
                "reason": "did the work",
                "full_log": "worker log",
            }
        )
    )

    blocked, salvage = recruiter._complete_typed_bundle(
        ledger, key, order, manifest, "sess:worker", herdr_session="test-session"
    )

    assert blocked is False
    assert salvage is None


def test_cursor_missing_bundle_gets_one_same_worker_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _exec_dead_claim(
        tmp_path, ledger, "interactive-cursor", "cursor"
    )
    manifest = _manifest(ledger, key, order)
    prompts: list[tuple[str, str]] = []
    repair_polls: list[float] = []

    def repair(target: str, prompt: str, **kwargs: object) -> None:
        assert (
            kwargs["paste_settle_seconds"]
            == recruiter.CURSOR_PROMPT_PASTE_SETTLE_SECONDS
        )
        prompts.append((target, prompt))

    def finish_asynchronous_repair(seconds: float) -> None:
        repair_polls.append(seconds)
        staging = manifest.artifact("result").staging_path
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(
            json.dumps(
                {
                    "order_id": order["order_id"],
                    "verdict": "passed",
                    "revisit": [],
                    "reason": "repaired the missing result",
                    "full_log": "cursor session",
                }
            )
        )

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", repair)
    monkeypatch.setattr(recruiter.time, "sleep", finish_asynchronous_repair)

    blocked, salvage = recruiter._complete_typed_bundle(
        ledger,
        key,
        order,
        manifest,
        "sess:live-cursor",
        herdr_session="test-session",
    )

    assert blocked is False
    assert salvage is None
    assert len(prompts) == 1
    assert len(repair_polls) == 1
    assert prompts[0][0] == "sess:live-cursor"
    assert "COMPLETION_REPAIR 1/1" in prompts[0][1]
    events = [item["event"] for item in ledger.events(key)]
    assert "completion-repair-unavailable" not in events


def test_claude_done_without_initial_bundle_waits_for_same_worker_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude reports a completed turn before an artifact repair turn has written its files.

    The repair prompt is asynchronous. Revalidating immediately used to close the healthy Claude
    pane and publish ``blocked`` even though the worker had completed its real work.
    """
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _exec_dead_claim(
        tmp_path, ledger, "interactive-claude", "claude"
    )
    manifest = _manifest(ledger, key, order)
    prompts: list[tuple[str, str]] = []
    repair_polls: list[float] = []

    def repair(target: str, prompt: str, **kwargs: object) -> None:
        assert kwargs["paste_settle_seconds"] == 0.0
        prompts.append((target, prompt))

    def finish_asynchronous_repair(seconds: float) -> None:
        repair_polls.append(seconds)
        staging = manifest.artifact("result").staging_path
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(
            json.dumps(
                {
                    "order_id": order["order_id"],
                    "verdict": "passed",
                    "revisit": [],
                    "reason": "reported the already-completed work",
                    "full_log": "claude session",
                }
            )
        )

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", repair)
    monkeypatch.setattr(recruiter.time, "sleep", finish_asynchronous_repair)

    blocked, salvage = recruiter._complete_typed_bundle(
        ledger,
        key,
        order,
        manifest,
        "sess:live-claude",
        herdr_session="test-session",
    )

    assert blocked is False
    assert salvage is None
    assert len(prompts) == 1
    assert len(repair_polls) == 1
    assert prompts[0][0] == "sess:live-claude"
    assert "COMPLETION_REPAIR 1/1" in prompts[0][1]
    assert (
        json.loads(manifest.artifact("result").staging_path.read_text())["verdict"]
        == "passed"
    )


def test_same_worker_repair_wait_is_bounded() -> None:
    def never_valid() -> dict:
        raise recruiter.CompletionError("still invalid")

    with pytest.raises(
        recruiter.CompletionError,
        match="did not produce a valid artifact bundle within 0 ms",
    ):
        recruiter._wait_for_completion_repair(never_valid, 0)


def test_codex_exec_post_start_commit_is_still_salvaged_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setattr(
        recruiter,
        "_submit_agent_prompt",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no live repair")),
    )
    ledger = recruiter.JobLedger()
    _git_repo(tmp_path)
    order, _order_path, key = _exec_dead_claim(tmp_path, ledger, "exec-commit", "codex")
    manifest = _manifest(ledger, key, order)
    sha = _commit(tmp_path, "exec-landed.txt")

    blocked, salvage = recruiter._complete_typed_bundle(
        ledger, key, order, manifest, "sess:worker", herdr_session="test-session"
    )

    assert blocked is True
    assert salvage is not None
    assert salvage["result"]["verdict"] == "salvaged-done"
    assert sha in salvage["result"]["reason"]


# --- Dirty-worktree evidence --------------------------------------------------


def test_exec_dirty_worktree_is_evidence_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncommitted edits are recorded so the receipt never implies a clean worktree,
    but unattributable dirty state must not flip the outcome."""
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    _git_repo(tmp_path)
    (tmp_path / "baseline.txt").write_text("baseline")
    subprocess.run(
        ["git", "add", "baseline.txt"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"},
    )
    order, _order_path, key = _dead_runner_claim(tmp_path, ledger, "dirty")
    (tmp_path / "uncommitted.py").write_text("print('worker was here')\n")
    (tmp_path / "baseline.txt").write_text("edited")

    evidence = recruiter.inspect_salvage(
        ledger, key, order, _manifest(ledger, key, order)
    )

    assert evidence["outcome"] == "empty"
    dirty = evidence["dirty_worktree"]
    assert dirty["dirty_path_count"] >= 2
    assert any("uncommitted.py" in line for line in dirty["dirty_paths"])
    assert "never success" in dirty["attribution"]


def test_salvage_outside_git_records_no_dirty_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    ledger = recruiter.JobLedger()
    order, _order_path, key = _dead_runner_claim(tmp_path, ledger, "no-git")

    evidence = recruiter.inspect_salvage(
        ledger, key, order, _manifest(ledger, key, order)
    )

    assert evidence["dirty_worktree"] is None
