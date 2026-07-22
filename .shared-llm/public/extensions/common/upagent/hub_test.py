# pyright: reportMissingImports=false
"""Focused single-Hub, discovery, bypass, and launch-transaction tests."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = _load("hub_test_transport", "hub_transport.py")
hub = _load("hub_test_hub", "hub.py")
client = _load("hub_test_client", "client.py")
recruiter = _load("hub_test_recruiter", "recruiter.py")


def _wait_for_protocol_ready(socket_path: Path, cwd: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client._probe_protocol_ready(socket_path, cwd)
        if response is not None:
            return response
        time.sleep(0.01)
    pytest.fail(f"Hub did not complete a protocol handshake at {socket_path}")


def _start_runtime(tmp_path: Path) -> tuple[Any, threading.Thread, Path]:
    socket_path = tmp_path / "hub.sock"
    runtime = hub.HubRuntime(socket_path, HERE / "recruiter.py")
    thread = threading.Thread(target=runtime.serve)
    thread.start()
    _wait_for_protocol_ready(socket_path, tmp_path.resolve())
    return runtime, thread, socket_path


def _stop_runtime(runtime: Any, thread: threading.Thread) -> None:
    runtime.stop()
    thread.join(timeout=3)
    assert not thread.is_alive()


def _order(tmp_path: Path) -> dict:
    return {
        "order_id": "phase-0.stage-1-implementation.pass-1.try-1",
        "phase_id": "phase-0",
        "stage_id": "stage-1-implementation",
        "harness": "claude",
        "model": "model",
        "agent": "backend",
        "cwd": str(tmp_path),
        "instructions_path": str(tmp_path / "instructions.md"),
        "result_path": str(tmp_path / "result.json"),
        "cockpit_pane": "leader-pane",
    }


def _cleanup(pane: str) -> dict[str, object]:
    return {
        "status": "closed",
        "worker_pane": pane,
        "verified_absent": True,
    }


def test_protocol_handshake_publishes_complete_hub_identity(tmp_path: Path) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    try:
        response = client._round_trip(
            socket_path, "hub", ["status"], cwd=tmp_path.resolve()
        )
        identity = json.loads(response["stdout"])
        assert identity["hub_instance_id"] == runtime.instance_id
        assert identity["pid"] == os.getpid()
        assert identity["protocol_version"] == transport.PROTOCOL_VERSION
        assert identity["protocol_fingerprint"] == transport.PROTOCOL_FINGERPRINT
        assert identity["process_start_time"] == transport.process_start_time(
            os.getpid()
        )
        assert identity["canonical_engine_path"] == str(
            (HERE / "recruiter.py").resolve()
        )
        assert identity["socket_path"] == str(socket_path.resolve())
        assert "herdr_session" in identity
    finally:
        _stop_runtime(runtime, thread)


def test_caller_context_is_strictly_whitelisted_and_validated(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "owner.token"
    token_file.write_text("safe-token\n")
    token_file.chmod(0o600)
    environment = {
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "caller-pane",
        "HERDR_SESSION": "caller-session",
        "HERDR_SOCKET_PATH": "/tmp/herdr/caller.sock",
        "RUNNER_OWNER_TOKEN_FILE": str(token_file),
        "RUNNER_OWNER_TOKEN": "raw-token-must-not-cross-the-wire",
        "UNRELATED_SECRET": "must-not-cross-the-wire",
    }
    assert transport.caller_context(environment) == {
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "caller-pane",
        "HERDR_SESSION": "caller-session",
        "HERDR_SOCKET_PATH": "/tmp/herdr/caller.sock",
        "RUNNER_OWNER_TOKEN_FILE": str(token_file.resolve()),
    }
    with pytest.raises(transport.ProtocolError, match="unknown keys"):
        transport.validate_caller_context(
            {"HERDR_ENV": "1", "UNRELATED_SECRET": "forbidden"}
        )
    with pytest.raises(transport.ProtocolError, match="must be '1'"):
        transport.validate_caller_context({"HERDR_ENV": "true"})
    with pytest.raises(transport.ProtocolError, match="absolute path"):
        transport.validate_caller_context({"HERDR_SOCKET_PATH": "relative.sock"})
    with pytest.raises(transport.ProtocolError, match="unknown keys"):
        transport.validate_caller_context({"RUNNER_OWNER_TOKEN": "raw-token"})
    with pytest.raises(transport.ProtocolError, match="absolute path"):
        transport.validate_caller_context(
            {"RUNNER_OWNER_TOKEN_FILE": "relative.token"}
        )
    token_file.chmod(0o644)
    with pytest.raises(transport.ProtocolError, match="same-user private"):
        transport.validate_caller_context(
            {"RUNNER_OWNER_TOKEN_FILE": str(token_file)}
        )
    with pytest.raises(client.transport.ProtocolError, match="unknown keys"):
        client._round_trip(
            Path("/does/not/exist.sock"),
            "hub",
            ["status"],
            cwd=HERE,
            caller_context={"UNRELATED_SECRET": "forbidden"},
        )


def test_server_rejects_non_whitelisted_caller_context(tmp_path: Path) -> None:
    runtime = hub.HubRuntime(tmp_path / "hub.sock", HERE / "recruiter.py")
    with pytest.raises(hub.transport.ProtocolError, match="unknown keys"):
        runtime._dispatch(
            {
                "argv": ["status"],
                "caller_context": {"API_TOKEN": "forbidden"},
                "cwd": str(tmp_path.resolve()),
                "protocol_fingerprint": transport.PROTOCOL_FINGERPRINT,
                "protocol_version": transport.PROTOCOL_VERSION,
                "stdin": None,
                "target": "hub",
                "type": "request",
            }
        )


def test_phase_start_request_retains_caller_pane_through_clean_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in transport.CALLER_CONTEXT_KEYS:
        monkeypatch.delenv(name, raising=False)
    runtime, thread, socket_path = _start_runtime(tmp_path)
    request_context = {
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "caller-pane",
        "HERDR_SESSION": "caller-session",
        "HERDR_SOCKET_PATH": "/tmp/herdr/caller.sock",
    }
    try:
        mismatch = client._round_trip(
            socket_path,
            "phase-controller",
            [
                "route.yaml",
                str(tmp_path),
                "phase-1",
                "1",
                "--tui-pane",
                "different-pane",
            ],
            cwd=tmp_path.resolve(),
            caller_context=request_context,
        )
        assert mismatch["exit_code"] == 1
        assert "does not match current Herdr pane caller-pane" in mismatch["stderr"]
    finally:
        _stop_runtime(runtime, thread)

    runtime, thread, socket_path = _start_runtime(tmp_path)
    phase = runtime._load_target("phase-controller", hub.TARGETS["phase-controller"])
    observed: dict[str, object] = {}

    def fake_start_phase(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        observed["request_pane"] = hub.command_runtime.getenv("HERDR_PANE_ID")
        observed["request_session"] = hub.command_runtime.getenv("HERDR_SESSION")
        observed["request_socket"] = hub.command_runtime.getenv("HERDR_SOCKET_PATH")
        observed["request_herdr_env"] = hub.command_runtime.getenv("HERDR_ENV")
        return {
            "state": "ready",
            "tui_pane": kwargs["tui_pane"],
        }

    monkeypatch.setattr(phase, "start_phase", fake_start_phase)
    try:
        response = client._round_trip(
            socket_path,
            "phase-controller",
            ["route.yaml", str(tmp_path), "phase-1", "1"],
            cwd=tmp_path.resolve(),
            caller_context=request_context,
        )
        assert response["exit_code"] == 0
        assert "PHASE_STARTED" in response["stdout"]
        assert response["stderr"] == ""
        assert observed["tui_pane"] == "caller-pane"
        assert observed["request_pane"] == "caller-pane"
        assert observed["request_session"] == "caller-session"
        assert observed["request_socket"] == "/tmp/herdr/caller.sock"
        assert observed["request_herdr_env"] == "1"
        assert all(
            os.environ.get(name) is None for name in transport.CALLER_CONTEXT_KEYS
        )
    finally:
        _stop_runtime(runtime, thread)


def test_clean_hub_finish_inherits_only_caller_owner_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (*transport.CALLER_CONTEXT_KEYS, transport.RAW_OWNER_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    runtime, thread, socket_path = _start_runtime(tmp_path)
    run_dir = tmp_path / "run"
    control = run_dir / "control"
    control.mkdir(parents=True)
    token = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    token_file = tmp_path / "owner.token"
    token_file.write_text(f"{token}\n")
    token_file.chmod(0o600)
    (run_dir / "run-status.md").write_text("# Stopped\n")
    (control / "plan-start.json").write_text(
        json.dumps(
            {
                "run_owner": {
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest()
                },
                "slug": "run",
                "state": "ready",
                "tui": {"herdr_session": "caller-session"},
            }
        )
    )
    plan = runtime._load_target("tui-controller", hub.TARGETS["tui-controller"])
    monkeypatch.setattr(
        plan.control,
        "_resolve_current_herdr_session_name",
        lambda: "caller-session",
    )
    observed_context: dict[str, str | None] = {}
    original_finish_plan = plan.finish_plan

    def observing_finish_plan(**kwargs: object) -> dict[str, object]:
        observed_context["token_file"] = plan.command_runtime.getenv(
            transport.OWNER_TOKEN_FILE_ENV
        )
        observed_context["raw_token"] = plan.command_runtime.getenv(
            transport.RAW_OWNER_TOKEN_ENV
        )
        return original_finish_plan(**kwargs)

    monkeypatch.setattr(plan, "finish_plan", observing_finish_plan)
    monkeypatch.setenv(transport.OWNER_TOKEN_FILE_ENV, str(token_file))
    raw_token = hashlib.sha256(f"raw:{tmp_path}".encode()).hexdigest()
    monkeypatch.setenv(transport.RAW_OWNER_TOKEN_ENV, raw_token)
    try:
        response = client._round_trip(
            socket_path,
            "tui-controller",
            [str(run_dir), "--finish-state", "stopped"],
            cwd=tmp_path.resolve(),
        )
        assert response["exit_code"] == 0
        assert response["stderr"] == ""
        assert "PLAN_FINISHED" in response["stdout"]
        assert (
            json.loads((control / "run-terminal.json").read_text())["state"]
            == "stopped"
        )
        assert observed_context == {
            "token_file": str(token_file.resolve()),
            "raw_token": None,
        }
        assert os.environ[transport.RAW_OWNER_TOKEN_ENV] == raw_token
    finally:
        _stop_runtime(runtime, thread)


def test_run_lifecycle_recipes_send_parent_repo_before_subcommand_through_hub(
    tmp_path: Path,
) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    observed: list[list[str]] = []

    class CapturingLifecycle:
        @staticmethod
        def main(argv: list[str]) -> int:
            observed.append(argv)
            return 0

    runtime._target_modules["run-lifecycle"] = CapturingLifecycle
    repo = HERE.parents[4]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    recipes = [
        ("run-session-start", []),
        ("run-session-heartbeat", []),
        ("run-session-snapshot", []),
        ("run-session-reconcile", []),
        ("run-session-guard", ["mutation"]),
        ("run-session-cleanup", []),
    ]
    environment = {
        **os.environ,
        transport.SOCKET_ENV: str(socket_path),
        "PYTHON_BIN": sys.executable,
    }
    try:
        for recipe, extra in recipes:
            process = subprocess.run(
                [
                    "just",
                    "--justfile",
                    str(repo / "justfile"),
                    recipe,
                    str(run_dir),
                    *extra,
                ],
                cwd=repo,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert process.returncode == 0, process.stderr
    finally:
        _stop_runtime(runtime, thread)

    expected_commands = [
        "session-start",
        "heartbeat",
        "snapshot",
        "reconcile",
        "guard",
        "cleanup",
    ]
    assert len(observed) == len(expected_commands)
    for argv, command in zip(observed, expected_commands, strict=True):
        assert argv[:3] == ["--repo", str(repo), command]
        assert argv[3] == str(run_dir)
    assert observed[4][4:] == ["--action", "mutation"]


def test_public_offering_listing_crosses_the_socket_as_json(tmp_path: Path) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    try:
        response = client._round_trip(
            socket_path,
            "public",
            ["lists", "--type", "offerings", "--json"],
            cwd=tmp_path.resolve(),
        )
        rows = json.loads(response["stdout"])
        assert response["exit_code"] == 0
        assert len(rows) == 9
        assert rows[0]["id"] == "claude-fable-5"
        assert rows[-1]["id"] == "pi-gpt-5-4-mini"
    finally:
        _stop_runtime(runtime, thread)


def test_blocked_dispatch_does_not_serialize_other_commands_or_capture_runner_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    wait_started = threading.Event()
    release_wait = threading.Event()
    runner_printed = threading.Event()

    class ConcurrentTarget:
        @staticmethod
        def main(argv: list[str]) -> int:
            command = argv[0]
            if command == "await":
                wait_started.set()

                def runner_output() -> None:
                    hub.command_runtime.command_print("BACKGROUND_RUNNER_ONLY")
                    runner_printed.set()

                threading.Thread(
                    target=hub.command_runtime.run_detached,
                    args=(runner_output,),
                ).start()
                assert release_wait.wait(timeout=3)
                hub.command_runtime.command_print("AWAIT_COMPLETE")
                return 0
            hub.command_runtime.command_print(
                f"{command.upper()} {hub.command_runtime.current_cwd()}"
            )
            return 0

    runtime._target_modules["public"] = ConcurrentTarget
    runtime._target_modules["tui-controller"] = ConcurrentTarget
    waited: list[dict[str, Any]] = []
    waiter = threading.Thread(
        target=lambda: waited.append(
            client._round_trip(socket_path, "public", ["await"], cwd=tmp_path)
        )
    )
    waiter.start()
    try:
        assert wait_started.wait(timeout=2)
        assert runner_printed.wait(timeout=2)
        started = time.monotonic()
        responses = [
            client._round_trip(socket_path, "public", [command], cwd=tmp_path)
            for command in ("respond", "status", "reconcile")
        ]
        responses.append(
            client._round_trip(
                socket_path, "tui-controller", ["controller"], cwd=tmp_path
            )
        )
        assert time.monotonic() - started < 1
        assert waiter.is_alive()
        assert [response["stdout"].split()[0] for response in responses] == [
            "RESPOND",
            "STATUS",
            "RECONCILE",
            "CONTROLLER",
        ]
        assert all(
            str(tmp_path.resolve()) in response["stdout"] for response in responses
        )
        assert all(
            "BACKGROUND_RUNNER_ONLY" not in response["stdout"] for response in responses
        )
    finally:
        release_wait.set()
        waiter.join(timeout=3)
        _stop_runtime(runtime, thread)
    assert waited and waited[0]["stdout"] == "AWAIT_COMPLETE\n"
    assert "BACKGROUND_RUNNER_ONLY" not in waited[0]["stdout"]
    assert "BACKGROUND_RUNNER_ONLY" in capsys.readouterr().out


def test_concurrent_malformed_command_is_request_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    barrier = threading.Barrier(3)
    responses: dict[str, dict[str, Any]] = {}

    def malformed() -> None:
        barrier.wait(timeout=2)
        responses["malformed"] = client._round_trip(
            socket_path, "phase-await", ["wait"], cwd=tmp_path.resolve()
        )

    def healthy() -> None:
        barrier.wait(timeout=2)
        responses["healthy"] = client._round_trip(
            socket_path,
            "public",
            ["lists", "--type", "offerings", "--json"],
            cwd=tmp_path.resolve(),
        )

    malformed_thread = threading.Thread(target=malformed)
    healthy_thread = threading.Thread(target=healthy)
    malformed_thread.start()
    healthy_thread.start()
    capsys.readouterr()
    barrier.wait(timeout=2)
    try:
        malformed_thread.join(timeout=3)
        healthy_thread.join(timeout=3)
        assert not malformed_thread.is_alive()
        assert not healthy_thread.is_alive()
        malformed_response = responses["malformed"]
        healthy_response = responses["healthy"]
        assert malformed_response["exit_code"] == 2
        assert "usage: upagent-phase-await wait" in malformed_response["stderr"]
        assert "required" in malformed_response["stderr"]
        assert malformed_response["stdout"] == ""
        assert healthy_response["exit_code"] == 0
        assert healthy_response["stderr"] == ""
        assert "usage:" not in healthy_response["stdout"]
        assert capsys.readouterr().err == ""
    finally:
        _stop_runtime(runtime, thread)


def test_protocol_mismatch_fails_before_request_dispatch(tmp_path: Path) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    try:
        with pytest.raises(client.ClientError, match="incompatible"):
            client._round_trip(
                socket_path,
                "hub",
                ["status"],
                cwd=tmp_path.resolve(),
                protocol_version=transport.PROTOCOL_VERSION + 1,
            )
    finally:
        _stop_runtime(runtime, thread)


def test_schema_fingerprint_mismatch_fails_before_request_dispatch(
    tmp_path: Path,
) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    try:
        with pytest.raises(client.ClientError, match="fingerprint is incompatible"):
            client._round_trip(
                socket_path,
                "hub",
                ["status"],
                cwd=tmp_path.resolve(),
                protocol_fingerprint="0" * 64,
            )
    finally:
        _stop_runtime(runtime, thread)


def test_lifetime_lock_excludes_a_second_hub(tmp_path: Path) -> None:
    first = hub.HubRuntime(tmp_path / "hub.sock", HERE / "recruiter.py")
    first.acquire()
    try:
        identity = json.loads(first.identity_path.read_text())
        assert identity["hub_instance_id"] == first.instance_id
        second = hub.HubRuntime(tmp_path / "hub.sock", HERE / "recruiter.py")
        with pytest.raises(hub.HubError, match="another UpAgent Hub"):
            second.acquire()
    finally:
        first.release()


def test_socket_override_is_absolute_and_discoverable(
    tmp_path: Path, monkeypatch
) -> None:
    override = tmp_path / "custom.sock"
    monkeypatch.setenv(transport.SOCKET_ENV, str(override))
    assert transport.socket_path(tmp_path) == override
    monkeypatch.setenv(transport.SOCKET_ENV, "relative.sock")
    with pytest.raises(transport.ProtocolError, match="absolute"):
        transport.socket_path(tmp_path)


def test_main_checkout_and_linked_worktree_share_identity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(transport.SOCKET_ENV, raising=False)
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "topic", str(worktree)],
        check=True,
    )

    assert transport.canonical_repo_root(repo) == repo.resolve()
    assert transport.canonical_repo_root(worktree) == repo.resolve()
    assert transport.repository_id(repo) == transport.repository_id(worktree)
    shared_socket = transport.socket_path(repo)
    assert shared_socket == transport.socket_path(worktree)

    runtime = hub.HubRuntime(shared_socket, HERE / "recruiter.py")
    thread = threading.Thread(target=runtime.serve)
    thread.start()
    _wait_for_protocol_ready(shared_socket, repo.resolve())
    try:
        main_identity = client._round_trip(
            shared_socket, "hub", ["status"], cwd=repo.resolve()
        )["identity"]
        worktree_identity = client._round_trip(
            shared_socket, "hub", ["status"], cwd=worktree.resolve()
        )["identity"]
        assert main_identity == worktree_identity
        assert main_identity["hub_instance_id"] == runtime.instance_id
        assert main_identity["canonical_engine_path"] == str(
            (HERE / "recruiter.py").resolve()
        )
    finally:
        _stop_runtime(runtime, thread)
        runtime.lock_path.unlink(missing_ok=True)
        shared_socket.parent.rmdir()


def test_thin_client_and_imported_recipes_have_no_checkout_local_bypass() -> None:
    client_text = (HERE / "client.py").read_text()
    upagent_just = (HERE / "justfile").read_text()
    runner_just = (HERE.parent / "runner" / "justfile").read_text()
    assert 'spec_from_file_location("upagent_recruiter"' not in client_text
    assert "JobLedger" not in client_text
    assert "cmd_dispatch" not in client_text
    assert "run-job" not in client_text
    assert "recruiter.py" not in upagent_just
    assert "recruiter.py" not in runner_just
    for target in ("public_api.py", "direct_controller.py", "phase_controller.py"):
        assert "recruiter.py" not in (HERE / target).read_text()
    assert "client.py" in upagent_just and "client.py" in runner_just


def test_direct_recruiter_cli_bypass_is_forbidden(tmp_path: Path) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in (recruiter.HUB_INSTANCE_ENV, recruiter.HUB_ENGINE_ENV)
    }
    process = subprocess.run(
        [sys.executable, str(HERE / "recruiter.py"), "status"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "direct Recruiter execution is forbidden" in process.stderr


def test_supervisor_and_runner_have_no_detached_subprocess_path() -> None:
    assert "subprocess.Popen" not in inspect.getsource(recruiter._spawn_job)
    assert "subprocess.Popen" not in inspect.getsource(recruiter.cmd_up)
    assert "threading.Thread" in inspect.getsource(recruiter._JobThread.__init__)
    assert "threading.Thread" in inspect.getsource(recruiter.cmd_up)


def test_job_runner_is_registered_before_start_and_reconciliation_waits_through_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_thread = threading.Thread
    launch_barrier = threading.Barrier(2)
    release_start = threading.Event()
    launch_done = threading.Event()
    probe_done = threading.Event()
    launch_errors: list[BaseException] = []
    probe_results: list[bool] = []
    key = "barrier-launch-runner"

    class BarrierThread:
        def __init__(self, **_kwargs: object):
            self._alive = False

        def start(self) -> None:
            self._alive = True
            launch_barrier.wait(timeout=2)
            assert release_start.wait(timeout=2)

        def is_alive(self) -> bool:
            return self._alive

        def join(self, _timeout: float | None = None) -> None:
            self._alive = False

    def launch() -> None:
        try:
            recruiter._spawn_job(key, "roster.yaml")
        except (RuntimeError, AssertionError) as error:
            launch_errors.append(error)
        finally:
            launch_done.set()

    def probe() -> None:
        probe_results.append(recruiter._runner_alive(os.getpid(), key))
        probe_done.set()

    monkeypatch.setattr(recruiter, "_require_hub_authority", lambda: None)
    monkeypatch.setattr(recruiter.threading, "Thread", BarrierThread)
    launcher = native_thread(target=launch)
    launcher.start()
    try:
        launch_barrier.wait(timeout=2)
        # _spawn_job holds the canonical registry lock from registration through start. A
        # reconciler arriving at this barrier must wait, not misclassify the launching runner.
        observer = native_thread(target=probe)
        observer.start()
        assert not probe_done.wait(timeout=0.1)
        release_start.set()
        assert launch_done.wait(timeout=2)
        assert probe_done.wait(timeout=2)
        launcher.join(timeout=2)
        observer.join(timeout=2)
        assert launch_errors == []
        assert probe_results == [True]
        assert recruiter._JOB_THREADS[key].thread.is_alive()
    finally:
        release_start.set()
        launcher.join(timeout=2)
        with recruiter._JOB_THREADS_LOCK:
            recruiter._JOB_THREADS.pop(key, None)


def test_duplicate_spawn_attaches_to_the_registered_live_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "duplicate-live-runner"
    constructed: list[object] = []

    class LiveThread:
        def is_alive(self) -> bool:
            return True

    class Handle:
        thread = LiveThread()

        def start(self) -> None:
            return None

    monkeypatch.setattr(recruiter, "_require_hub_authority", lambda: None)
    monkeypatch.setattr(
        recruiter,
        "_JobThread",
        lambda *_args: constructed.append(Handle()) or constructed[-1],
    )
    try:
        first = recruiter._spawn_job(key, "roster.yaml")
        second = recruiter._spawn_job(key, "roster.yaml")
        assert first is second
        assert recruiter._JOB_THREADS[key] is first
        assert len(constructed) == 1
        assert recruiter._runner_alive(os.getpid(), key) is True
    finally:
        with recruiter._JOB_THREADS_LOCK:
            recruiter._JOB_THREADS.pop(key, None)


def test_missing_handle_for_owned_claim_reconciles_instead_of_contending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _order(tmp_path)
    ledger = recruiter.JobLedger(tmp_path / "ledger")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key, order["order_id"], 60_000, owner={"runner_pid": os.getpid()}
    )
    assert token is not None
    reconciled: list[tuple[str, str]] = []
    monkeypatch.setattr(recruiter, "_require_hub_authority", lambda: None)
    monkeypatch.setattr(recruiter, "JobLedger", lambda: ledger)
    monkeypatch.setattr(
        recruiter,
        "_JobThread",
        lambda *_args: pytest.fail("an owned durable claim must not start a contender"),
    )
    monkeypatch.setattr(
        recruiter,
        "_reconcile_claim",
        lambda owner, candidate, lease, *, force: (
            reconciled.append((candidate, lease["token"])) or True
        ),
    )

    assert recruiter._spawn_job(key, "roster.yaml") is None
    assert reconciled == [(key, token)]


def test_job_runner_start_failure_removes_registered_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "failed-launch-runner"

    class FailingThread:
        def __init__(self, **_kwargs: object):
            pass

        def start(self) -> None:
            raise RuntimeError("injected thread start failure")

    monkeypatch.setattr(recruiter, "_require_hub_authority", lambda: None)
    monkeypatch.setattr(recruiter.threading, "Thread", FailingThread)

    with pytest.raises(RuntimeError, match="injected thread start failure"):
        recruiter._spawn_job(key, "roster.yaml")

    assert key not in recruiter._JOB_THREADS


def test_repeated_cold_start_waits_for_handshake_before_first_request(
    tmp_path: Path,
) -> None:
    for attempt in range(30):
        root = tmp_path / f"attempt-{attempt}"
        root.mkdir()
        runtime, thread, socket_path = _start_runtime(root)
        try:
            response = client._round_trip(
                socket_path, "hub", ["status"], cwd=root.resolve()
            )
            assert response["identity"]["hub_instance_id"] == runtime.instance_id
        finally:
            _stop_runtime(runtime, thread)


def test_production_cold_start_repeatedly_publishes_protocol_readiness(
    tmp_path: Path,
) -> None:
    for attempt in range(10):
        socket_path = tmp_path / f"production-{attempt}.sock"
        pid: int | None = None
        try:
            client._start_canonical_hub(socket_path, HERE)
            response = client._probe_protocol_ready(socket_path, HERE)
            assert response is not None
            pid = response["identity"]["pid"]
            assert isinstance(pid, int)
            # Startup returned only after a complete status exchange, so the first caller after
            # readiness must connect without the old pathname-exists/ConnectionRefused race.
            assert (
                client._round_trip(socket_path, "hub", ["status"], cwd=HERE)[
                    "exit_code"
                ]
                == 0
            )
        finally:
            if pid is not None:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    waited, _status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        break
                    time.sleep(0.01)
                else:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            assert not socket_path.exists()


def test_startup_failure_reports_durable_log_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_hub = tmp_path / "failing_hub.py"
    failing_hub.write_text(
        "import sys\nprint('injected startup diagnostic', file=sys.stderr, flush=True)\n"
        "raise SystemExit(23)\n"
    )
    engine = tmp_path / "recruiter.py"
    engine.write_text("# startup-test engine placeholder\n")
    monkeypatch.setattr(
        client.transport,
        "canonical_module_path",
        lambda filename, _cwd: failing_hub if filename == "hub.py" else engine,
    )
    monkeypatch.setattr(client.transport, "canonical_repo_root", lambda _cwd: tmp_path)
    socket_path = tmp_path / "failing.sock"

    with pytest.raises(
        client.ClientError, match="injected startup diagnostic"
    ) as failure:
        client._start_canonical_hub(socket_path, tmp_path)

    log_path = client._hub_log_path(socket_path)
    assert str(log_path) in str(failure.value)
    assert "injected startup diagnostic" in log_path.read_text()
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert log_path.stat().st_uid == os.getuid()


def test_production_client_start_keeps_supervisor_diagnostics_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_herdr = fake_bin / "herdr"
    fake_herdr.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            args = sys.argv[1:]
            if args == ["session", "list", "--json"]:
                print(json.dumps({"sessions": [{"name": "diagnostic-session", "running": True, "socket_path": "/tmp/diagnostic-herdr.sock"}]}))
                raise SystemExit(0)
            if args[:2] == ["--session", "diagnostic-session"]:
                args = args[2:]
            if args == ["workspace", "list"]:
                print(json.dumps({"result": {"workspaces": [{"label": "shared-services", "workspace_id": "workspace-1"}]}}))
                raise SystemExit(0)
            if args == ["pane", "list", "--workspace", "workspace-1"]:
                print(json.dumps({"result": {"panes": [{"label": "recruiter", "pane_id": "recruiter-pane"}]}}))
                raise SystemExit(0)
            if args[:2] == ["pane", "run"] or args[:2] == ["pane", "report-agent"]:
                raise SystemExit(0)
            print(f"unexpected fake herdr command: {args}", file=sys.stderr)
            raise SystemExit(2)
            """
        )
    )
    fake_herdr.chmod(0o755)
    roster = tmp_path / "roster.yaml"
    roster.write_text('harnesses:\n  claude: "claude {instructions_path}"\n')
    state_path = tmp_path / "runtime/recruiter.json"
    ledger_path = tmp_path / "runtime/ledger"
    socket_path = tmp_path / "runtime/hub.sock"
    socket_path.parent.mkdir()
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("UPAGENT_STATE", str(state_path))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(ledger_path))

    pid: int | None = None
    try:
        client._start_canonical_hub(socket_path, HERE)
        ready = client._probe_protocol_ready(socket_path, HERE)
        assert ready is not None
        pid = ready["identity"]["pid"]
        assert isinstance(pid, int)
        response = client._round_trip(
            socket_path,
            "recruiter",
            ["--roster", str(roster), "up", "--separate-workspaces"],
            cwd=HERE,
            caller_context={
                "HERDR_SESSION": "diagnostic-session",
                "HERDR_SOCKET_PATH": "/tmp/diagnostic-herdr.sock",
            },
        )
        assert response["exit_code"] == 0, response["stderr"]
        assert json.loads(response["stdout"])["supervisor_pid"] == pid

        bad_claim = ledger_path / "active/requests/bad-claim"
        bad_claim.mkdir(parents=True)
        (bad_claim / "lease.json").write_text("{}\n")
        log_path = client._hub_log_path(socket_path)
        diagnostic = "recruiter: supervisor reconciliation remains pending:"
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if log_path.read_text().count(diagnostic) >= 2:
                break
            time.sleep(0.05)
        else:
            pytest.fail(
                f"supervisor did not persist repeated diagnostics: {log_path.read_text()}"
            )

        # Two warnings from separate reconciliation intervals prove the supervisor survived its
        # first background write. A fresh handshake proves the owning Hub survived as well.
        assert log_path.read_text().count(diagnostic) >= 2
        assert log_path.stat().st_mode & 0o777 == 0o600
        assert log_path.stat().st_uid == os.getuid()
        assert (
            client._round_trip(socket_path, "hub", ["status"], cwd=HERE)["exit_code"]
            == 0
        )
        os.kill(pid, 0)
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                waited, _status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    break
                time.sleep(0.01)
            else:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        assert not socket_path.exists()


def test_forged_environment_cannot_authorize_direct_recruiter(tmp_path: Path) -> None:
    runtime = hub.HubRuntime(tmp_path / "hub.sock", HERE / "recruiter.py")
    runtime.acquire()
    try:
        environment = {
            **os.environ,
            recruiter.HUB_INSTANCE_ENV: runtime.instance_id,
            recruiter.HUB_ENGINE_ENV: str((HERE / "recruiter.py").resolve()),
            recruiter.HUB_PATH_ENV: str((HERE / "hub.py").resolve()),
            recruiter.HUB_PID_ENV: str(runtime.pid),
            recruiter.HUB_LOCK_FD_ENV: str(runtime._lock_stream.fileno()),
            recruiter.HUB_SOCKET_ENV: str(runtime.socket_path),
        }
        process = subprocess.run(
            [sys.executable, str(HERE / "recruiter.py"), "status"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert process.returncode != 0
        assert "Hub authority" in process.stderr
    finally:
        runtime.release()


def test_direct_public_api_execution_is_forbidden(tmp_path: Path) -> None:
    process = subprocess.run(
        [sys.executable, str(HERE / "public_api.py"), "status"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "direct Recruiter execution is forbidden" in process.stderr


@pytest.mark.parametrize("role", ["worker", "manager", "checker", "rescue"])
def test_launch_commit_fault_closes_exact_created_pane(
    role: str, tmp_path: Path, monkeypatch
) -> None:
    order = _order(tmp_path)
    ledger = recruiter.JobLedger(tmp_path / "ledger")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={"herdr_session": "test-session"},
    )
    assert token
    observed_states: list[str] = []

    def start(*args, **kwargs):
        journals = ledger.launch_journals()
        observed_states.append(journals[-1][1]["state"])
        return f"{role}-pane", "workspace", f"{role}-address"

    closed: list[str] = []
    monkeypatch.setattr(recruiter, "_start_herdr_agent", start)
    monkeypatch.setattr(ledger, "mark_launch_started", lambda *args: False)
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: closed.append(pane) or _cleanup(pane),
    )

    with pytest.raises(
        recruiter.RecruiterError, match="before .* pane .* was committed"
    ):
        recruiter._start_fenced_ledger_agent(
            ledger,
            key,
            token,
            role,
            f"agent-{role}",
            order,
            "true",
            herdr_session="test-session",
        )

    assert observed_states == ["launching"]
    assert closed == [f"{role}-pane"]
    journal = ledger.launch_journals()[-1][1]
    assert journal["state"] == "closed"
    assert journal["cleanup"]["worker_pane"] == f"{role}-pane"


def test_intake_launch_commit_fault_compensates_exact_pane(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "state" / "recruiter.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"recruiter_pane": "trusted-pane"}))
    monkeypatch.setattr(recruiter, "STATE_FILE", state_path)
    monkeypatch.setattr(
        recruiter,
        "load_roster",
        lambda _path: {"harnesses": {"claude": "claude {instructions_path}"}},
    )
    monkeypatch.setattr(
        recruiter, "_resolve_current_herdr_session_name", lambda: "test-session"
    )
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: ("intake-pane", "workspace", "intake-agent"),
    )
    monkeypatch.setattr(
        recruiter,
        "_record_started_intake_clerk",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            recruiter.RecruiterError("injected intake journal CAS fault")
        ),
    )
    closed: list[str] = []
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: closed.append(pane) or _cleanup(pane),
    )
    monkeypatch.setattr(
        recruiter,
        "_cleanup_intake_clerk",
        lambda ownership: {
            "status": "already-absent",
            "worker_pane": ownership.get("pane"),
            "verified_absent": True,
        },
    )

    with pytest.raises(recruiter.RecruiterError, match="intake clerk failed"):
        recruiter._run_order_intake_clerk(
            "{}", tmp_path / "submission.json", "roster.yaml", "intake-key"
        )

    assert closed == ["intake-pane"]
    ownership_paths = list(
        (state_path.parent / "intake/attempts").glob("*/ownership.json")
    )
    assert len(ownership_paths) == 1
    ownership = recruiter._secure_json(ownership_paths[0])
    assert ownership["pane"] == "intake-pane"
    assert ownership["state"] == "closed"
    assert ownership["cleanup"]["verified_absent"] is True


def test_reconciliation_closes_only_agent_resolved_exact_pane(
    tmp_path: Path, monkeypatch
) -> None:
    order = _order(tmp_path)
    ledger = recruiter.JobLedger(tmp_path / "ledger")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={"herdr_session": "test-session"},
    )
    assert token
    launch_id = ledger.begin_launch(
        key, token, "checker", "checker-agent", "test-session", str(tmp_path)
    )
    assert ledger.mark_launch_started(
        key, token, launch_id, "exact-pane", "workspace", "checker-agent"
    )
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {"agent": {"name": "checker-agent", "pane_id": "exact-pane"}}
        },
    )
    closed: list[str] = []
    monkeypatch.setattr(
        recruiter,
        "_close_worker_pane",
        lambda pane, **kwargs: closed.append(pane) or _cleanup(pane),
    )

    assert recruiter._reconcile_launch_journals(ledger, force=True) == 1
    assert closed == ["exact-pane"]
    assert ledger.launch_journals()[-1][1]["state"] == "closed"


def test_compensation_failure_defers_receipt_until_exact_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _order(tmp_path)
    ledger = recruiter.JobLedger(tmp_path / "ledger")
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={"herdr_session": "test-session", "runner_pid": 999999999},
    )
    assert token
    monkeypatch.setattr(
        recruiter,
        "_start_herdr_agent",
        lambda *args, **kwargs: ("orphan-pane", "workspace", "exact-agent"),
    )
    monkeypatch.setattr(ledger, "mark_launch_started", lambda *args: False)
    monkeypatch.setattr(
        recruiter,
        "_herdr_json",
        lambda *args, **kwargs: {
            "result": {"agent": {"name": "exact-agent", "pane_id": "orphan-pane"}}
        },
    )
    cleanup_allowed = False
    closed: list[str] = []

    def close(pane: str, **kwargs: object) -> dict[str, object]:
        if not cleanup_allowed:
            raise recruiter.RecruiterError("injected compensation outage")
        closed.append(pane)
        return _cleanup(pane)

    monkeypatch.setattr(recruiter, "_close_worker_pane", close)
    with pytest.raises(recruiter.FencedLaunchError) as failure:
        recruiter._start_fenced_ledger_agent(
            ledger,
            key,
            token,
            "worker",
            "exact-agent",
            order,
            "true",
            herdr_session="test-session",
        )
    assert failure.value.cleanup["verified_absent"] is False
    journal = ledger.launch_journals()[-1][1]
    assert journal["pane"] == "orphan-pane"
    assert journal["agent_name"] == "exact-agent"
    assert journal["state"] == "cleanup-pending"
    assert not (ledger.request_dir(key) / "receipt.json").exists()

    cleanup_allowed = True
    monkeypatch.setattr(recruiter, "_runner_alive", lambda pid, candidate: False)
    assert recruiter._reconcile_launch_journals(ledger, force=False) == 1
    lease = dict(ledger.active_claims()[0][1])
    assert recruiter._reconcile_claim(ledger, key, lease, force=False) is True

    assert closed == ["orphan-pane"]
    receipt = ledger.completed_receipt(key, order)
    assert receipt["cleanup"]["verified_absent"] is True
    launch_evidence = receipt["cleanup"]["launches"][0]
    assert launch_evidence["agent_name"] == "exact-agent"
    assert launch_evidence["pane"] == "orphan-pane"
    assert launch_evidence["cleanup"]["verified_absent"] is True
    events = [
        json.loads(path.read_text())
        for path in sorted((ledger.request_dir(key) / "events").glob("*.json"))
    ]
    cleanup_index = max(
        index
        for index, event in enumerate(events)
        if event["event"] == "pane-launch-closed"
        and event["cleanup"].get("verified_absent") is True
    )
    terminal_index = next(
        index
        for index, event in enumerate(events)
        if event["event"] in ("finished", "cleanup-failed")
    )
    assert cleanup_index < terminal_index


def test_token_stdin_is_request_local_bounded_and_direct_hub_equivalent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, thread, socket_path = _start_runtime(tmp_path)
    lifecycle_target = runtime._load_target(
        "run-lifecycle", hub.TARGETS["run-lifecycle"]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "plan.md").write_text("# Plan\n")
    (run_dir / "route.yaml").write_text("phases: {}\n")
    process_start = lifecycle_target.control._process_start_time(os.getpid())
    owner = lifecycle_target.acquire_owner(
        run_dir,
        owner={
            "kind": "operator",
            "process_pid": os.getpid(),
            "process_start_time": process_start,
        },
    )
    token = owner["token"]
    argv = [
        "--repo",
        str(tmp_path),
        "guard",
        str(run_dir),
        "--action",
        "mutation",
        "--token-stdin",
    ]
    try:
        direct = subprocess.run(
            [sys.executable, str(hub.TARGETS["run-lifecycle"]), *argv],
            cwd=tmp_path,
            input=f"{token}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        response = client._round_trip(
            socket_path,
            "run-lifecycle",
            argv,
            cwd=tmp_path.resolve(),
            request_stdin=f"{token}\n",
        )
        assert direct.returncode == response["exit_code"] == 0
        assert json.loads(direct.stdout) == json.loads(response["stdout"])
        assert token not in response["stdout"]
        assert token not in response["stderr"]
        assert token not in json.dumps(response["identity"])
        assert token not in runtime.identity_path.read_text()
    finally:
        _stop_runtime(runtime, thread)

    class RefuseRead(io.StringIO):
        def read(self, *args: object, **kwargs: object) -> str:
            pytest.fail("stdin was read for an operation that did not request it")

    monkeypatch.setattr(sys, "stdin", RefuseRead("secret"))
    assert client._read_requested_stdin("hub", ["status"]) is None
    with pytest.raises(transport.ProtocolError, match="allowed only"):
        transport.validate_request_stdin(
            "run-lifecycle", ["snapshot", str(run_dir), "--token-stdin"], "secret"
        )
    with pytest.raises(transport.ProtocolError, match="exceeds"):
        transport.validate_request_stdin(
            "run-lifecycle",
            ["guard", str(run_dir), "--action", "mutation", "--token-stdin"],
            "x" * (transport.MAX_REQUEST_STDIN_BYTES + 1),
        )


def test_old_v2_zero_worker_hub_is_safely_replaced_by_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    common = repo / ".shared-llm/public/extensions/common"
    shutil.copytree(HERE.parent, common)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    socket_path = tmp_path / "migration/hub.sock"
    socket_path.parent.mkdir()
    hub_path = common / "upagent/hub.py"
    engine_path = common / "upagent/recruiter.py"
    legacy_source = textwrap.dedent(
        """
        import argparse, json, os, signal, socket, time, uuid
        from pathlib import Path

        parser = argparse.ArgumentParser()
        parser.add_argument("command")
        parser.add_argument("--socket", required=True, type=Path)
        parser.add_argument("--engine", required=True, type=Path)
        args = parser.parse_args()
        path = args.socket.resolve()
        identity_path = path.with_name(path.name + ".identity.json")
        ledger = path.with_name(path.name + ".ledger")
        (ledger / "active/requests").mkdir(parents=True)
        identity = {
            "canonical_engine_path": str(args.engine.resolve()),
            "herdr_session": "unbound",
            "hub_instance_id": str(uuid.uuid4()),
            "hub_path": str(Path(__file__).resolve()),
            "ledger_path": str(ledger.resolve()),
            "pid": os.getpid(),
            "protocol_version": 2,
            "socket_path": str(path),
            "started_at_ns": time.time_ns(),
        }
        stopping = False
        def stop(*unused):
            global stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path.unlink(missing_ok=True)
        server.bind(str(path))
        server.listen(8)
        server.settimeout(0.1)
        identity_path.write_text(json.dumps(identity))
        identity_path.chmod(0o644)
        try:
            while not stopping:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    stream = connection.makefile("rwb", buffering=0)
                    hello = json.loads(stream.readline())
                    if set(hello) != {"protocol_version", "type"} or hello.get("protocol_version") != 2:
                        stream.write(b'{"error":"legacy-v2","protocol_version":2,"type":"error"}\\n')
                        continue
                    stream.write((json.dumps({"identity": identity, "protocol_version": 2, "type": "hello"}) + "\\n").encode())
                    json.loads(stream.readline())
                    response = {"exit_code": 0, "identity": identity, "stderr": "", "stdout": json.dumps(identity), "type": "response"}
                    stream.write((json.dumps(response) + "\\n").encode())
        finally:
            server.close()
            path.unlink(missing_ok=True)
            identity_path.unlink(missing_ok=True)
        """
    )
    hub_path.write_text(legacy_source)
    legacy = subprocess.Popen(
        [
            sys.executable,
            str(hub_path),
            "serve",
            "--socket",
            str(socket_path),
            "--engine",
            str(engine_path),
        ],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    identity_path = socket_path.with_name(f"{socket_path.name}.identity.json")
    deadline = time.monotonic() + 3
    while not identity_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert identity_path.is_file()
    old_identity = json.loads(identity_path.read_text())
    assert old_identity["protocol_version"] == 2
    shutil.copy2(HERE / "hub.py", hub_path)
    monkeypatch.setenv(transport.SOCKET_ENV, str(socket_path))
    try:
        client._ensure_current_hub_for_up(socket_path, repo)
        legacy.wait(timeout=3)
        response = client._round_trip(socket_path, "hub", ["status"], cwd=repo)
        current_identity = response["identity"]
        assert current_identity["protocol_version"] == transport.PROTOCOL_VERSION
        assert (
            current_identity["protocol_fingerprint"] == transport.PROTOCOL_FINGERPRINT
        )
        assert current_identity["pid"] != old_identity["pid"]
    finally:
        if legacy.poll() is None:
            legacy.terminate()
            legacy.wait(timeout=3)
        if identity_path.is_file():
            resident = json.loads(identity_path.read_text())
            resident_pid = resident.get("pid")
            if isinstance(resident_pid, int) and resident_pid != os.getpid():
                os.kill(resident_pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while identity_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
