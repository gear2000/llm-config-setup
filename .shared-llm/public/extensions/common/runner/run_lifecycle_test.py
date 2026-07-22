# pyright: reportMissingImports=false
"""Hermetic tests for run lifecycle ownership and recovery."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "runner_run_lifecycle_tested", Path(__file__).with_name("run_lifecycle.py")
)
assert _spec and _spec.loader
life = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(life)
LifecycleError = life.LifecycleError


def _run_tree(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "plan.md").write_text("# Plan\n")
    (run_dir / "route.yaml").write_text(
        "phases:\n  phase-0:\n    lead:\n      llm_profile: cheap\n"
    )
    return run_dir


def _owner(
    pid: int = 11, start: str = "start-11", kind: str = "operator"
) -> dict[str, object]:
    return {"kind": kind, "process_pid": pid, "process_start_time": start}


@pytest.fixture(autouse=True)
def _isolated_recruiter_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "hub"))
    monkeypatch.setenv(life.RUNNER_TOKEN_DIR_ENV, str(tmp_path / "tokens"))
    monkeypatch.setattr(life.control, "STATE_FILE", tmp_path / "state/recruiter.json")


def test_owner_collision_becomes_observer_and_same_owner_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    monkeypatch.setattr(
        life.control,
        "_process_start_time",
        lambda pid: {11: "start-11", 22: "start-22"}.get(pid),
    )

    first = life.acquire_owner(run_dir, owner=_owner(11, "start-11"))
    same = life.acquire_owner(run_dir, owner=_owner(11, "start-11"))
    other = life.acquire_owner(run_dir, owner=_owner(22, "start-22"))

    assert first["role"] == "owner"
    assert same["role"] == "owner"
    assert same["token"] == first["token"]
    assert same["reason"] == "same owner refreshed"
    assert other["role"] == "observer"
    assert other["token"] is None


def test_stale_pid_reuse_requires_reconciliation_before_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    monkeypatch.setattr(life.control, "_process_start_time", lambda pid: "original")
    first = life.acquire_owner(run_dir, owner=_owner(11, "original"))
    monkeypatch.setattr(life.control, "_process_start_time", lambda pid: "reused")

    observer = life.acquire_owner(
        run_dir, owner=_owner(22, "new"), takeover_stale=False
    )
    assert observer["role"] == "observer"
    assert "reconcile" in observer["reason"]

    with pytest.raises(LifecycleError, match="requires a v1 reconciliation"):
        life.acquire_owner(run_dir, owner=_owner(22, "new"), takeover_stale=True)

    receipt = life.reconcile(run_dir)
    assert receipt["owner"]["status"]["state"] == "stale"
    monkeypatch.setattr(
        life.control,
        "_process_start_time",
        lambda pid: "new" if pid == 22 else "reused",
    )
    takeover = life.acquire_owner(run_dir, owner=_owner(22, "new"), takeover_stale=True)

    assert takeover["role"] == "owner"
    assert takeover["generation"] == first["generation"] + 1
    assert takeover["token"] != first["token"]


def test_corrupt_symlink_lease_is_refused(tmp_path: Path) -> None:
    run_dir = _run_tree(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n")
    (control / "run-owner.json").symlink_to(outside)

    with pytest.raises(LifecycleError, match="must not be a symlink"):
        life.read_owner_lease(run_dir)


def test_snapshot_schema_errors_and_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    monkeypatch.setattr(life.control, "_process_start_time", lambda pid: None)
    owner = life.acquire_owner(run_dir, owner=_owner())
    life.reconcile(run_dir)
    snap = life.snapshot(run_dir)

    assert snap["schema"] == life.SNAPSHOT_SCHEMA
    assert snap["run_state"]["source"] == "prose-untrusted"
    assert snap["shared_environment"]["state"] == "not-configured"
    assert snap["owner"]["token_sha256"] == life._token_hash(owner["token"])
    assert "token" not in snap["owner"]

    phase_result = run_dir / "phases/phase-0/phase-result.json"
    phase_result.parent.mkdir(parents=True)
    phase_result.write_text(json.dumps({"phase_id": "phase-0", "verdict": "passed"}))
    assert life.snapshot(run_dir)["run_state"]["source"] == "typed-phase-results"

    (run_dir / "control/run-terminal.json").write_text(
        json.dumps(
            {"state": "succeeded", "plan_id": "run", "summary_path": "run-status.md"}
        )
    )
    terminal = life.snapshot(run_dir)["run_state"]
    assert terminal["source"] == "typed-terminal"
    assert terminal["state"] == "succeeded"

    phase_result.write_text("[]\n")
    malformed = life.snapshot(run_dir)
    assert any(
        "phase-0 result must be a JSON object" in error["reason"]
        for error in malformed["source_errors"]
    )


def test_heartbeat_loop_exits_when_identity_or_token_stops_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    calls: list[str] = []
    sleeps: list[int] = []

    def heartbeat_once(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("beat")
        if len(calls) == 2:
            raise LifecycleError("heartbeat token does not match current lease")
        return {"role": "owner"}

    monkeypatch.setattr(life, "heartbeat_once", heartbeat_once)

    result = life.heartbeat_loop(
        run_dir,
        token="tok",
        interval_seconds=1,
        ttl_seconds=10,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert calls == ["beat", "beat"]
    assert sleeps == [1]
    assert result["state"] == "exited"
    assert "token does not match" in result["reason"]


def test_owner_token_file_is_0600_and_env_file_is_preferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    token_file = life.write_owner_token_file(run_dir, "safe-token")

    assert token_file.read_text().strip() == "safe-token"
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"

    monkeypatch.setenv(life.RUNNER_OWNER_TOKEN_FILE_ENV, str(token_file))
    monkeypatch.setenv(life.RUNNER_OWNER_TOKEN_ENV, "mismatched-capability")

    assert life.token_from_env_or_file() == "safe-token"


def test_owner_token_file_refuses_permissive_or_symlinked_files(tmp_path: Path) -> None:
    permissive = tmp_path / "token"
    permissive.write_text("safe-token\n")
    permissive.chmod(0o644)
    with pytest.raises(LifecycleError, match="0600 regular file"):
        life.read_owner_token_file(permissive)

    target = tmp_path / "target"
    target.write_text("safe-token\n")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(LifecycleError, match="0600 regular file"):
        life.read_owner_token_file(link)


def test_owner_token_writer_refuses_existing_symlink(tmp_path: Path) -> None:
    run_dir = _run_tree(tmp_path)
    outside = tmp_path / "outside-token"
    outside.write_text("old-token\n")
    token_path = life.owner_token_path(run_dir)
    token_path.symlink_to(outside)

    with pytest.raises(LifecycleError, match="owner token file must not be a symlink"):
        life.write_owner_token_file(run_dir, "safe-token")


def test_owner_token_directory_refuses_symlink_before_chmod(tmp_path: Path) -> None:
    target = tmp_path / "target-dir"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    (tmp_path / "tokens").symlink_to(target, target_is_directory=True)

    with pytest.raises(LifecycleError, match="same-user 0700 directory"):
        life._owner_token_dir()

    assert target.stat().st_mode & 0o777 == 0o755


def test_control_directory_refuses_symlink_before_chmod(tmp_path: Path) -> None:
    run_dir = _run_tree(tmp_path)
    target = tmp_path / "control-target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    (run_dir / "control").symlink_to(target, target_is_directory=True)

    with pytest.raises(LifecycleError, match="same-user 0700 directory"):
        life._control_dir(run_dir)

    assert target.stat().st_mode & 0o777 == 0o755


def test_token_source_conflicts_fail_and_empty_stdin_uses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = life.uuid.uuid4().hex
    monkeypatch.setenv(life.RUNNER_OWNER_TOKEN_ENV, capability)
    assert life.resolve_token_sources(None, None, "") == capability
    with pytest.raises(LifecycleError, match="sources conflict"):
        life.resolve_token_sources(capability, Path("other"), None)


def test_snapshot_reads_active_leaders_from_run_root_not_control(
    tmp_path: Path,
) -> None:
    run_dir = _run_tree(tmp_path)
    (run_dir / "active-leader-panes.json").write_text(
        json.dumps({"phase-0": {"pane_id": "root-pane"}})
    )
    (run_dir / "control").mkdir()
    (run_dir / "control/active-leader-panes.json").write_text(
        json.dumps({"phase-0": {"pane_id": "wrong-control-pane"}})
    )

    snap = life.snapshot(run_dir)

    assert snap["active_leaders"]["phase-0"]["pane_id"] == "root-pane"


def test_guard_allows_reads_and_blocks_wrong_or_stale_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    monkeypatch.setattr(life.control, "_process_start_time", lambda pid: "start-11")
    owner = life.acquire_owner(run_dir, owner=_owner())

    assert life.guard(run_dir, action="snapshot")["allowed"] is True
    with pytest.raises(LifecycleError, match="does not match"):
        life.guard(run_dir, action="mutation", token=f"{owner['token']}-other")
    assert (
        life.guard(run_dir, action="mutation", token=owner["token"])["allowed"] is True
    )

    lease_path = run_dir / "control/run-owner.json"
    lease = json.loads(lease_path.read_text())
    lease["heartbeat_at_ns"] = 1
    lease_path.write_text(json.dumps(lease))
    with pytest.raises(LifecycleError, match="stale"):
        life.guard(run_dir, action="mutation", token=owner["token"])


def test_cleanup_refuses_adopted_resources_and_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "tui": {
                    "herdr_session": "llm-lab-test",
                    "health": {},
                    "ownership": {"pane": {"pane_id": "pane-1", "state": "adopted"}},
                }
            }
        )
    )
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life, "_repo_git_state", lambda _repo: {"clean": True, "landed": True}
    )
    monkeypatch.setattr(
        life.control,
        "_herdr",
        lambda *args, **kwargs: pytest.fail("adopted pane must not close"),
    )

    with pytest.raises(LifecycleError, match="adopted pane"):
        life.cleanup(run_dir, repo=tmp_path, token="tok")

    report = json.loads((control / "cleanup-report.json").read_text())
    assert report["state"] == "refused"
    assert report["decisions"][0]["reason"] == "adopted pane"


def test_cleanup_requires_owner_token(tmp_path: Path) -> None:
    run_dir = _run_tree(tmp_path)

    with pytest.raises(LifecycleError, match="requires an owner token"):
        life.cleanup(run_dir, repo=tmp_path)


def test_cleanup_preflight_is_atomic_before_any_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life,
        "_repo_git_state",
        lambda _repo: {"clean": False, "landed": False, "reason": "dirty"},
    )
    monkeypatch.setattr(
        life,
        "_cleanup_decisions",
        lambda _run_dir, _source_errors: [{"action": "close", "pane_id": "pane-1"}],
    )
    monkeypatch.setattr(
        life.control,
        "_herdr",
        lambda *args, **kwargs: pytest.fail("dirty cleanup must not mutate"),
    )

    with pytest.raises(LifecycleError, match="dirty"):
        life.cleanup(run_dir, repo=tmp_path, token="tok")

    assert (
        json.loads((run_dir / "control/cleanup-report.json").read_text())["state"]
        == "refused"
    )


def test_cleanup_preserves_ambiguous_created_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "tui": {
                    "herdr_session": "llm-lab-test",
                    "ownership": {"pane": {"pane_id": "pane-1", "state": "created"}},
                }
            }
        )
    )
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life, "_repo_git_state", lambda _repo: {"clean": True, "landed": True}
    )
    monkeypatch.setattr(
        life.control,
        "_herdr",
        lambda *args, **kwargs: pytest.fail("ambiguous pane must not close"),
    )

    with pytest.raises(LifecycleError, match="missing startup health identity"):
        life.cleanup(run_dir, repo=tmp_path, token="tok")


def test_cleanup_preserves_all_when_phase_leader_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "tui": {
                    "herdr_session": "llm-lab-test",
                    "health": {
                        "cwd": str(run_dir),
                        "expected_agent": "claude",
                        "expected_process": "claude",
                        "process_pid": 123,
                        "process_start_time": "start-123",
                    },
                    "ownership": {"pane": {"pane_id": "tui-pane", "state": "created"}},
                }
            }
        )
    )
    (run_dir / "active-leader-panes.json").write_text(
        json.dumps({"phase-0": {"pane_id": "leader-pane"}})
    )
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life, "_repo_git_state", lambda _repo: {"clean": True, "landed": True}
    )
    monkeypatch.setattr(life, "_pane_identity_verified", lambda _identity: (True, "ok"))
    monkeypatch.setattr(
        life, "_live_pane_ids", lambda _session: {"tui-pane", "leader-pane"}
    )
    monkeypatch.setattr(
        life.control,
        "_herdr",
        lambda *args, **kwargs: pytest.fail(
            "no pane should close when any leader is ambiguous"
        ),
    )

    with pytest.raises(LifecycleError, match="missing structural pane ownership"):
        life.cleanup(run_dir, repo=tmp_path, token="tok")


def test_cleanup_malformed_active_leader_map_writes_report_before_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "tui": {
                    "herdr_session": "llm-lab-test",
                    "health": {
                        "cwd": str(run_dir),
                        "expected_agent": "claude",
                        "expected_process": "claude",
                        "process_pid": 123,
                        "process_start_time": "start-123",
                    },
                    "ownership": {"pane": {"pane_id": "tui-pane", "state": "created"}},
                }
            }
        )
    )
    (run_dir / "active-leader-panes.json").write_text("[]\n")
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life, "_repo_git_state", lambda _repo: {"clean": True, "landed": True}
    )
    monkeypatch.setattr(life, "_live_pane_ids", lambda _session: {"tui-pane"})
    monkeypatch.setattr(life, "_pane_identity_verified", lambda _identity: (True, "ok"))
    monkeypatch.setattr(
        life.control,
        "_herdr",
        lambda *args, **kwargs: pytest.fail(
            "malformed leader map must block all closes"
        ),
    )

    with pytest.raises(LifecycleError, match="active leader map is malformed"):
        life.cleanup(run_dir, repo=tmp_path, token="tok")

    report = json.loads((control / "cleanup-report.json").read_text())
    assert report["state"] == "refused"
    assert report["decisions"][-1]["resource"] == "phase-leaders"
    assert report["source_errors"]


def test_cleanup_closes_only_identity_validated_created_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "tui": {
                    "herdr_session": "llm-lab-test",
                    "health": {
                        "cwd": str(run_dir),
                        "expected_agent": "claude",
                        "expected_process": "claude",
                        "process_pid": 123,
                        "process_start_time": "start-123",
                    },
                    "ownership": {"pane": {"pane_id": "pane-1", "state": "created"}},
                }
            }
        )
    )
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life, "_repo_git_state", lambda _repo: {"clean": True, "landed": True}
    )
    monkeypatch.setattr(life, "_pane_identity_verified", lambda _identity: (True, "ok"))
    closed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        life.control, "_herdr", lambda *args, **kwargs: closed.append(args)
    )
    live_results = iter([{"pane-1"}, set()])
    monkeypatch.setattr(life, "_live_pane_ids", lambda _session: next(live_results))
    capability = life.uuid.uuid4().hex
    token_file = life.write_owner_token_file(run_dir, capability)

    report = life.cleanup(run_dir, repo=tmp_path, token=capability)

    assert report["state"] == "closed"
    assert report["owner_token_removed"] is True
    assert not token_file.exists()
    assert closed == [("pane", "close", "pane-1")]


def test_cleanup_treats_identity_recorded_absent_phase_leader_as_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    control = run_dir / "control"
    control.mkdir()
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "tui": {
                    "herdr_session": "llm-lab-test",
                    "health": {
                        "cwd": str(run_dir),
                        "expected_agent": "claude",
                        "expected_process": "claude",
                        "process_pid": 123,
                        "process_start_time": "start-123",
                    },
                    "ownership": {"pane": {"pane_id": "tui-pane", "state": "created"}},
                }
            }
        )
    )
    (run_dir / "active-leader-panes.json").write_text(
        json.dumps(
            {
                "phase-0": {
                    "health": {
                        "cwd": str(run_dir),
                        "expected_agent": "claude",
                        "expected_process": "claude",
                        "process_pid": 456,
                        "process_start_time": "start-456",
                    },
                    "herdr_session": "llm-lab-test",
                    "ownership": {
                        "pane": {"pane_id": "leader-pane", "state": "created"}
                    },
                    "pane_id": "leader-pane",
                }
            }
        )
    )
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life, "_repo_git_state", lambda _repo: {"clean": True, "landed": True}
    )
    monkeypatch.setattr(life, "_pane_identity_verified", lambda _identity: (True, "ok"))
    live_results = iter([{"tui-pane"}, {"tui-pane"}, set()])
    monkeypatch.setattr(life, "_live_pane_ids", lambda _session: next(live_results))
    closed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        life.control, "_herdr", lambda *args, **kwargs: closed.append(args)
    )

    report = life.cleanup(run_dir, repo=tmp_path, token="tok")

    assert report["state"] == "closed"
    assert closed == [("pane", "close", "tui-pane")]
    assert any(
        decision["action"] == "already-absent"
        and decision["resource"] == "phase-leader:phase-0"
        for decision in report["decisions"]
    )


def test_cleanup_reports_failure_when_closed_pane_remains_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    monkeypatch.setattr(life, "guard", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(
        life, "_repo_git_state", lambda _repo: {"clean": True, "landed": True}
    )
    monkeypatch.setattr(
        life,
        "_cleanup_decisions",
        lambda _run_dir, _source_errors: [
            {
                "action": "close",
                "herdr_session": "llm-lab-test",
                "pane_id": "pane-1",
            }
        ],
    )
    monkeypatch.setattr(life.control, "_herdr", lambda *args, **kwargs: None)
    monkeypatch.setattr(life, "_live_pane_ids", lambda _session: {"pane-1"})

    with pytest.raises(LifecycleError, match="remained live"):
        life.cleanup(run_dir, repo=tmp_path, token="tok")

    report = json.loads((run_dir / "control/cleanup-report.json").read_text())
    assert report["state"] == "cleanup-failed"
    assert report["closed"][0]["verified_absent"] is False


def test_repo_git_state_preserves_clean_but_unpushed_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True
    )
    (repo / "file.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "one"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "HEAD"],
        check=True,
        capture_output=True,
    )

    assert life._repo_git_state(repo)["landed"] is True

    (repo / "file.txt").write_text("two\n")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-am", "two"],
        check=True,
        capture_output=True,
    )
    state = life._repo_git_state(repo)

    assert state["clean"] is True
    assert state["landed"] is False
    assert "not reachable" in state["reason"]


def test_restart_takeover_fences_old_owner_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_tree(tmp_path)
    monkeypatch.setattr(
        life.control, "_process_start_time", lambda pid: "old" if pid == 1 else None
    )
    old = life.acquire_owner(run_dir, owner=_owner(1, "old"))
    assert life.guard(run_dir, action="mutation", token=old["token"])["allowed"] is True
    monkeypatch.setattr(life.control, "_process_start_time", lambda pid: None)
    life.reconcile(run_dir)
    monkeypatch.setattr(
        life.control, "_process_start_time", lambda pid: "new" if pid == 2 else None
    )
    new = life.acquire_owner(run_dir, owner=_owner(2, "new"), takeover_stale=True)

    with pytest.raises(LifecycleError, match="does not match"):
        life.guard(run_dir, action="mutation", token=old["token"])
    assert life.guard(run_dir, action="mutation", token=new["token"])["allowed"] is True
