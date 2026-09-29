"""Deterministic tests for the per-command UpAgent execution model."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _repo_root() -> Path:
    return HERE.parents[4]


@pytest.fixture(autouse=True)
def _canonical_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPAGENT_CANONICAL_REPO", str(_repo_root()))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


os.environ["UPAGENT_CANONICAL_REPO"] = str(_repo_root())
client = _load("upagent_per_command_client_test", "client.py")


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "UPAGENT_RUNTIME_DIR": str(tmp_path / "runtime"),
        "UPAGENT_HUB_DIR": str(tmp_path / "ledger"),
        "UPAGENT_STATE": str(tmp_path / "services.json"),
    }


def test_mutation_classifier_keeps_reads_lock_free() -> None:
    assert client._is_mutating("public", ["request"]) is True
    assert client._is_mutating("public", ["cleanup"]) is True
    assert client._is_mutating("public", ["await", "--request", "id"]) is True
    assert client._is_mutating("public", ["await-any", "--request", "id"]) is True
    assert client._is_mutating("public", ["status", "--json"]) is False
    assert client._is_mutating("public", ["lists", "workers"]) is False
    assert client._is_mutating("recruiter", ["--roster", "x", "reconcile"]) is True
    assert client._is_mutating("recruiter", ["await", "order.json"]) is True
    assert client._is_mutating("recruiter", ["await-any", "order.json"]) is True
    assert client._is_mutating("recruiter", ["status"]) is False
    assert client._is_mutating("phase-await", ["wait"]) is False
    assert client._is_mutating("phase-await", ["publish"]) is True
    assert client._is_mutating("implementer-await", ["wait"]) is False
    assert client._is_mutating("implementer-await", ["wait-answer"]) is False
    assert client._is_mutating("implementer-await", ["publish"]) is True
    assert client._is_mutating("implementer-await", ["respond"]) is True
    assert client._is_mutating("pipelines", ["list"]) is False
    assert client._is_mutating("pipelines", ["list", "--json"]) is False
    assert client._is_mutating("pipelines", ["launch", "rpi"]) is True
    # Fails closed: an unrecognized pipelines command is a mutation, never a lock-free read.
    assert client._is_mutating("pipelines", ["retire", "rpi"]) is True
    assert client._is_mutating("pipelines", []) is True


def test_public_capture_keeps_internal_records_out_of_json_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recruiter, public = client._load_command_modules("public", HERE)

    def internal_record() -> int:
        recruiter.print('REQUEST_ACCEPTED {"state": "running"}', flush=True)
        return 0

    with client.command_runtime.activate(tmp_path, _environment(tmp_path)):
        code, output = public._capture(internal_record)
        public._emit({"ok": True}, True, "ok")

    captured = capsys.readouterr()
    assert code == 0
    assert output == 'REQUEST_ACCEPTED {"state": "running"}\n'
    assert json.loads(captured.out) == {"ok": True}
    assert "REQUEST_ACCEPTED" not in captured.out


def test_public_self_healing_up_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recruiter, public = client._load_command_modules("public", HERE)
    calls: list[str] = []

    def state() -> str | None:
        return "pane-1" if calls else None

    def up(roster_path: str) -> int:
        calls.append(roster_path)
        recruiter.print('{"up": true}', flush=True)
        recruiter.command_runtime.write_stderr("layout warning\n")
        return 0

    monkeypatch.setattr(public.recruiter, "_recruiter_pane_from_state", state)
    monkeypatch.setattr(public.recruiter, "cmd_up", up)
    home = tmp_path / "home"
    roster_path = (
        home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    )
    roster_path.parent.mkdir(parents=True)
    roster_path.write_text(public.offerings.render_roster(["standard"]))
    environment = {**_environment(tmp_path), "HOME": str(home)}

    with client.command_runtime.activate(tmp_path, environment):
        assert public._cockpit_pane() == "pane-1"

    captured = capsys.readouterr()
    assert calls == [str(roster_path)]
    assert captured.out == ""
    assert captured.err == ""


def test_coarse_lock_excludes_a_concurrent_mutator(tmp_path: Path) -> None:
    _, recruiter = client._load_command_modules("recruiter", HERE)
    ledger_root = tmp_path / "ledger"
    script = f"""
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('child_recruiter', {str(HERE / "recruiter.py")!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with module.JobLedger(Path({str(ledger_root)!r}))._claim_lock('other'):
    print('acquired', flush=True)
"""
    with recruiter.JobLedger(ledger_root)._claim_lock("first"):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.15)
        assert process.poll() is None
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert stdout == "acquired\n"


def test_read_command_runs_while_mutation_lock_is_held(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    _, recruiter = client._load_command_modules("recruiter", HERE)
    with recruiter.JobLedger(tmp_path / "ledger")._claim_lock("held"):
        completed = subprocess.run(
            [sys.executable, str(HERE / "client.py"), "status", "--json"],
            cwd=HERE,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
        )
    assert completed.returncode == 0, completed.stderr
    status = json.loads(completed.stdout)
    assert status["execution_model"] == "per-command"
    assert status["services_ready"] is False


def test_each_dispatch_imports_a_fresh_current_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("UPAGENT_STATE", str(tmp_path / "services.json"))
    first_recruiter, first_target = client._load_command_modules("public", HERE)
    second_recruiter, second_target = client._load_command_modules("public", HERE)
    assert first_recruiter is not second_recruiter
    assert first_target is not second_target
    assert not (tmp_path / "runtime" / "hub.sock").exists()


def test_linked_worktree_client_reexecs_canonical_before_importing_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPAGENT_CANONICAL_REPO", raising=False)
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=main, check=True
    )
    subprocess.run(["git", "config", "user.name", "UpAgent Test"], cwd=main, check=True)
    destination = main / ".shared-llm/public/extensions/common/upagent"
    destination.parent.mkdir(parents=True)
    shutil.copytree(HERE, destination)
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "canonical"], cwd=main, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "drift", str(linked)],
        cwd=main,
        check=True,
    )
    local_client = linked / ".shared-llm/public/extensions/common/upagent/client.py"
    local_client.write_text(
        local_client.read_text().replace(
            "_canonical_client_bootstrap()\n",
            "_canonical_client_bootstrap()\nraise RuntimeError('drift runtime imported')\n",
            1,
        )
    )
    outside = tmp_path / "outside-caller-cwd"
    outside.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outside, check=True)
    completed = subprocess.run(
        [sys.executable, str(local_client), "status", "--json"],
        cwd=outside,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["execution_model"] == "per-command"


def test_bare_backed_worktree_client_uses_checked_out_main_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPAGENT_CANONICAL_REPO", raising=False)
    source = tmp_path / "source"
    bare = tmp_path / "repo.git"
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "UpAgent Test"], cwd=source, check=True
    )
    destination = source / ".shared-llm/public/extensions/common/upagent"
    destination.parent.mkdir(parents=True)
    shutil.copytree(HERE, destination)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "canonical"], cwd=source, check=True)
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "worktree", "add", "-q", str(main), "main"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "worktree",
            "add",
            "-q",
            "-b",
            "drift",
            str(linked),
            "main",
        ],
        check=True,
    )
    local_client = linked / ".shared-llm/public/extensions/common/upagent/client.py"
    local_client.write_text(
        local_client.read_text().replace(
            "_canonical_client_bootstrap()\n",
            "_canonical_client_bootstrap()\nraise RuntimeError('bare drift imported')\n",
            1,
        )
    )
    outside = tmp_path / "outside-caller-cwd"
    outside.mkdir()

    completed = subprocess.run(
        [sys.executable, str(local_client), "status", "--json"],
        cwd=outside,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["execution_model"] == "per-command"


def test_ambiguous_main_candidates_require_explicit_canonical_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPAGENT_CANONICAL_REPO", raising=False)
    one = tmp_path / "one"
    two = tmp_path / "two"
    for root in (one, two):
        path = root / ".shared-llm/public/extensions/common/upagent"
        path.mkdir(parents=True)
        (path / "client.py").write_text("")

    def fake_git(start: Path, *args: str) -> str:
        if args == ("worktree", "list", "--porcelain"):
            return (
                f"worktree {one}\nHEAD a\nbranch refs/heads/main\n\n"
                f"worktree {two}\nHEAD b\nbranch refs/heads/main\n"
            )
        if args == ("rev-parse", "--path-format=absolute", "--show-toplevel"):
            return str(start)
        raise AssertionError(args)

    monkeypatch.setattr(client, "_git", fake_git)

    with pytest.raises(RuntimeError, match="UPAGENT_CANONICAL_REPO"):
        client._canonical_repo_root(tmp_path)


def test_removed_hub_target_fails_loudly() -> None:
    with pytest.raises(client.ClientError, match="singleton Hub target was removed"):
        client.main(["--target", "hub", "status"])


# --- Legacy request/dispatch/verify intercept for resident specialists -------------------


def _isolate_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    environment = {**_environment(tmp_path), "HOME": str(home)}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    return environment


def _register_resident(tmp_path: Path) -> None:
    # The resident registry lives beside the ledger (`UPAGENT_HUB_DIR`'s parent).
    state = tmp_path / "specialists" / "residents" / "resident" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {"name": "advisor", "cwd": str(tmp_path), "enabled": True, "state": "ready"}
        )
    )


def _bad_order(tmp_path: Path, kind: str) -> Path:
    order = tmp_path / "order.json"
    if kind == "malformed":
        order.write_text("{not json")
    return order


def _valid_order_file(tmp_path: Path) -> Path:
    order = tmp_path / "order.json"
    order.write_text(
        json.dumps(
            {
                "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
                "phase_id": "phase-0",
                "stage_id": "stage-1-implementation",
                "harness": "claude",
                "model": "",
                "agent": "backend",
                "cwd": str(tmp_path),
                "instructions_path": str(tmp_path / "instructions.md"),
                "result_path": str(tmp_path / "result.json"),
                "cockpit_pane": "1-1",
            }
        )
    )
    return order


def _record_preflight(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    load = client._load

    def patched(name: str, path: Path):
        module = load(name, path)
        if name == "upagent_specialist_lifecycle_client":
            module.preflight = lambda cwd, cockpit=None: calls.append(
                ("preflight", cwd, cockpit)
            )
        return module

    monkeypatch.setattr(client, "_load", patched)


@pytest.mark.parametrize("resident", [False, True])
@pytest.mark.parametrize("kind", ["missing", "malformed"])
@pytest.mark.parametrize("command", ["request", "dispatch"])
def test_legacy_launch_invalid_order_reports_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    kind: str,
    resident: bool,
) -> None:
    _isolate_client(tmp_path, monkeypatch)
    if resident:
        _register_resident(tmp_path)
    calls: list = []
    _record_preflight(monkeypatch, calls)

    code = client.invoke(
        "recruiter", [command, str(_bad_order(tmp_path, kind))], tmp_path
    )

    # The Recruiter's own clean `recruiter: invalid order` exit, never a ContractError.
    assert code == 1
    assert "recruiter: invalid order" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("command", ["request", "dispatch", "verify"])
def test_legacy_launch_preflights_registered_resident_then_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _isolate_client(tmp_path, monkeypatch)
    _register_resident(tmp_path)
    calls: list = []
    _record_preflight(monkeypatch, calls)
    recruiter, module = client._load_command_modules("recruiter", HERE)
    monkeypatch.setattr(module, "main", lambda argv: calls.append(("main", argv)) or 0)
    order = str(_valid_order_file(tmp_path))

    assert client._invoke_module(module, [command, order], tmp_path) == 0

    assert calls == [("preflight", str(tmp_path), "1-1"), ("main", [command, order])]


def test_legacy_launch_without_residents_skips_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_client(tmp_path, monkeypatch)
    calls: list = []
    _record_preflight(monkeypatch, calls)
    recruiter, module = client._load_command_modules("recruiter", HERE)
    monkeypatch.setattr(module, "main", lambda argv: calls.append(("main", argv)) or 0)
    order = str(_valid_order_file(tmp_path))

    assert client._invoke_module(module, ["request", order], tmp_path) == 0

    assert calls == [("main", ["request", order])]
    assert not (tmp_path / "specialists").exists()


def test_legacy_request_missing_order_with_resident_has_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _isolate_client(tmp_path, monkeypatch)
    _register_resident(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            str(HERE / "client.py"),
            "--target",
            "recruiter",
            "request",
            str(tmp_path / "missing.json"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr
    assert "recruiter: invalid order" in completed.stderr
