# pyright: reportMissingImports=false
"""Isolated public requests against a deterministic executable, never a real provider."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = load("headless_test_api", "public_api.py")
r = load("headless_test_recruiter", "recruiter.py")
api._bind_recruiter_runtime(r)

FAKE = r"""#!/usr/bin/env python3
import json, os, re, signal, subprocess, sys, time
from pathlib import Path
assert "--print" in sys.argv and "--output-format" in sys.argv
assert "--dangerously-skip-permissions" not in sys.argv
assert sys.argv[sys.argv.index("--agent") + 1] == "backend"
assert not any(key.startswith("HERDR_") for key in os.environ)
with open(os.environ["FAKE_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv) + "\n")
brief = sys.stdin.read()
assert "same-live-worker repair" in brief and "remain in your session" not in brief
mode = os.environ.get("FAKE_MODE", "success")
if mode in ("sleep", "descendant", "daemon", "apparent-final"):
    if mode in ("descendant", "daemon"):
        code = "import os,signal,time; "
        if mode == "daemon":
            code += "os.setsid(); "
        code += "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)"
        child = subprocess.Popen([sys.executable, "-c", code])
        Path(os.environ["FAKE_CHILD"]).write_text(str(child.pid))
    if mode == "apparent-final":
        print(json.dumps({"type":"result", "subtype":"success", "is_error":False}), flush=True)
    time.sleep(120)
if mode == "pressure":
    sys.stderr.write("x" * (20 * 1024 * 1024))
if mode == "stdout-pressure":
    sys.stdout.write("x" * (20 * 1024 * 1024))
if mode not in ("missing", "error", "malformed-output"):
    result_path = Path(re.search(r"- result.json: (.*?)  ", brief).group(1))
    result_path.parent.mkdir(parents=True, exist_ok=True)
    order_id = re.search(r'`order_id`: exactly "(.*?)"', brief).group(1)
    verdict = "failed" if mode == "failed" else "passed"
    result = {"order_id":order_id, "verdict":verdict, "full_log":"fake transcript"}
    if mode == "failed":
        result["revisit"] = ["stage-5-finalization"]
    if mode == "malformed-artifact":
        result_path.write_text("not json")
    else:
        result_path.write_text(json.dumps(result))
    if mode == "contradictory":
        report = Path(re.search(r"- compacted.md: (.*)", brief).group(1))
        report.write_text("Problems found.\nVERDICT: VEERED\n")
    if mode in ("answer", "answer-error"):
        answer_path = Path(re.search(r"- answer.json: (.*)", brief).group(1))
        consult_id = re.search(r'"consult_id":"(.*?)"', brief).group(1)
        answer = {"consult_id":consult_id, "answer":"fixture answer", "citations":["fixture.py:1"]}
        if mode == "answer-error":
            answer = {"consult_id":consult_id, "error":"fixture could not answer"}
        answer_path.write_text(json.dumps(answer))
    if mode == "exit-descendant":
        child = subprocess.Popen([sys.executable, "-c", "import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)"])
        Path(os.environ["FAKE_CHILD"]).write_text(str(child.pid))
if mode == "malformed-output":
    print("looks successful but not JSON")
else:
    output = json.dumps({"type":"result", "subtype":"success", "is_error": mode == "error",
                         "permission_denials": ["Bash"] if mode == "denied" else []})
    if mode == "truncated-prefix":
        sys.stdout.write(output + " " * (16 * 1024 * 1024 - len(output)) + '{"type":"result","is_error":true}')
    else:
        print(output)
if mode == "nonzero":
    sys.exit(3)
"""


class Runtime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cwd = root / "repo"
        self.cwd.mkdir()
        (self.cwd / ".git").mkdir()
        self.home = root / "home"
        personas = self.home / ".claude/agents"
        personas.mkdir(parents=True)
        (personas / "backend.md").write_text(
            "---\nname: backend\ndescription: Fixture\n---\nDo the task.\n"
        )
        roster = self.cwd / api.offerings.ROSTER_RELATIVE_PATH
        roster.parent.mkdir(parents=True)
        roster.write_text(api.offerings.render_roster(["standard"]))
        self.bin = root / "bin"
        self.bin.mkdir()
        executable = self.bin / "claude"
        executable.write_text(FAKE)
        executable.chmod(0o700)
        for name in ("herdr", "pi", "codex", "claudex", "cursor-agent"):
            trap = self.bin / name
            trap.write_text('#!/bin/sh\necho "$0 $*" >> "$FORBIDDEN_CALLS"\nexit 98\n')
            trap.chmod(0o700)
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "UPAGENT_CANONICAL_REPO": str(REPO),
            "UPAGENT_RUNTIME_DIR": str(root / "runtime"),
            "UPAGENT_HUB_DIR": str(root / "runtime/ledger"),
            "UPAGENT_STATE": str(root / "runtime/services.json"),
            "FAKE_CALLS": str(root / "calls"),
            "FAKE_CHILD": str(root / "child"),
            "FORBIDDEN_CALLS": str(root / "forbidden"),
        }
        self.prompt = self.cwd / "prompt.md"
        self.prompt.write_text("Perform the fixture task.\n")
        self.request_id = str(uuid.uuid4())

    def command(
        self, *args: str, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HERE / "client.py"), *args],
            env=self.env,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def request(
        self, *args: str, wait: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.command(
            "request",
            "--type",
            "worker",
            "--request-id",
            self.request_id,
            "--offering",
            "claude-sonnet-5-5",
            "--effort",
            "high",
            "--agent",
            "backend",
            "--prompt-file",
            str(self.prompt),
            "--cwd",
            str(self.cwd),
            "--headless",
            "--json",
            *(["--wait"] if wait else []),
            *args,
        )

    def status(self) -> dict:
        result = self.command("get", "--request", self.request_id, "--json")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def calls(self) -> list:
        path = self.root / "calls"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )


@pytest.fixture
def runtime(tmp_path: Path) -> Any:
    if sys.platform != "linux":
        pytest.skip("headless process integration is Linux-only")
    value = Runtime(tmp_path)
    yield value
    # Tests must never leave workers or touch the active machine runtime.
    records = list((tmp_path / "runtime/ledger/active/requests").glob("*/lease.json"))
    for path in records:
        lease = json.loads(path.read_text())
        status = value.status()
        token = (
            status["state"].get("requester_control_token")
            or lease["requester_control_token"]
        )
        token_file = tmp_path / "teardown-token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        value.command(
            "cancel",
            "--request",
            value.request_id,
            "--control-token-file",
            str(token_file),
            "--json",
        )
    assert not (tmp_path / "forbidden").exists(), (
        "Herdr or a helper executable was invoked"
    )


def test_public_fast_success_and_idempotency(runtime: Runtime) -> None:
    result = runtime.request()
    assert result.returncode == 0, (result.stdout, result.stderr)
    status = json.loads(result.stdout)
    assert status["result"]["verdict"] == "passed"
    assert status["receipt"]["cleanup"]["verified_absent"] is True
    assert status["payload"]["headless"] is True
    order = json.loads(Path(status["order_path"]).read_text())
    assert "cockpit_pane" not in order and order["management"] == {"mode": "direct"}
    assert len(runtime.calls()) == 1
    replay = runtime.request()
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["attached"] is True
    assert len(runtime.calls()) == 1
    # Same immutable id cannot switch back to pane mode, even after terminality.
    changed = runtime.command(
        "request", "--file", str(_json_request(runtime, headless=False)), "--json"
    )
    assert changed.returncode != 0 and "request_id_conflict" in changed.stderr
    assert len(runtime.calls()) == 1


def test_headless_pruning_keeps_mode_identity_and_idempotency(runtime: Runtime) -> None:
    result = runtime.request()
    assert result.returncode == 0, result.stderr
    cleaned = runtime.command(
        "cleanup", "--request", runtime.request_id, "--apply", "--json"
    )
    assert cleaned.returncode == 0, cleaned.stderr
    status = runtime.status()
    assert status["pruned"] is True and status["result"]["verdict"] == "passed"
    replay = runtime.request()
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["attached"] is True
    conflict = runtime.command(
        "request", "--file", str(_json_request(runtime, headless=False)), "--json"
    )
    assert conflict.returncode == 1 and "request_id_conflict" in conflict.stderr
    assert len(runtime.calls()) == 1


def _json_request(runtime: Runtime, **updates: Any) -> Path:
    value = {
        "schema_version": 1,
        "request_id": runtime.request_id,
        "type": "worker",
        "offering": "claude-sonnet-5-5",
        "effort": "high",
        "agent": "backend",
        "prompt_file": str(runtime.prompt),
        "cwd": str(runtime.cwd),
        **updates,
    }
    path = runtime.root / "request-input.json"
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "result artifact"),
        ("malformed-artifact", "invalid"),
        ("malformed-output", "JSON"),
        ("stdout-pressure", "JSON"),
        ("error", "non-error success"),
        ("nonzero", "exited 3"),
        ("contradictory", "consistency"),
        ("denied", "permission denials"),
        ("failed", None),
    ],
)
def test_public_rejects_unproven_success(
    runtime: Runtime, mode: str, reason: str | None
) -> None:
    runtime.env["FAKE_MODE"] = mode
    result = runtime.request()
    assert result.returncode == 1, (result.stdout, result.stderr)
    status = json.loads(result.stdout)
    assert status["result"]["verdict"] == ("failed" if mode == "failed" else "blocked")
    assert status["receipt"]["cleanup"]["verified_absent"] is True
    if reason:
        assert reason in status["result"]["reason"]
    assert len(runtime.calls()) == 1  # No repair, recovery helper, or silent retry.


def test_output_backpressure_is_drained_and_bounded(runtime: Runtime) -> None:
    runtime.env["FAKE_MODE"] = "pressure"
    result = runtime.request()
    assert result.returncode == 0, result.stderr
    stderr = next(
        (runtime.root / "runtime/ledger/requests").glob("*/headless/*/stderr.log")
    )
    assert stderr.stat().st_size == r.headless.process.LOG_LIMIT
    attempt = json.loads(result.stdout)["receipt"]["cleanup"]["attempts"][0]
    assert attempt["output_bytes"][str(stderr)] == 20 * 1024 * 1024


@pytest.mark.parametrize(
    "updates",
    [
        {"offering": "pi-gpt-6-1-sol"},
        {"keep_open": True},
        {"sentinel": True},
        {"headless": "yes"},
    ],
)
def test_unsupported_json_combinations_never_launch(
    runtime: Runtime, updates: dict
) -> None:
    path = _json_request(runtime, headless=True)
    value = json.loads(path.read_text())
    value.update(updates)
    path.write_text(json.dumps(value))
    result = runtime.command("request", "--file", str(path), "--json")
    assert result.returncode != 0
    assert runtime.calls() == []
    assert not (runtime.root / "runtime/ledger/requests").exists()


def test_cockpit_and_file_flag_conflicts_never_launch(runtime: Runtime) -> None:
    result = runtime.request("--cockpit-pane", "not-a-pane")
    assert result.returncode != 0 and "cockpit-pane" in result.stderr
    path = _json_request(runtime, headless=True)
    result = runtime.command("request", "--file", str(path), "--headless")
    assert result.returncode != 0 and "mutually exclusive" in result.stderr
    assert not runtime.calls()


def test_nonlinux_is_rejected_and_legacy_hashes_unchanged(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _json_request(runtime, headless=True)
    args = api.contract.parse_argv(["request", "--file", str(path)])
    monkeypatch.setattr(api.sys, "platform", "darwin")
    with pytest.raises(api.PublicError, match="Linux"):
        api.validate_request(args, runtime.cwd)
    monkeypatch.setenv("HOME", str(runtime.home))
    path = _json_request(runtime)
    original = api.validate_request(
        api.contract.parse_argv(["request", "--file", str(path)]), runtime.cwd
    )
    path = _json_request(runtime, headless=False)
    explicit = api.validate_request(
        api.contract.parse_argv(["request", "--file", str(path)]), runtime.cwd
    )
    assert original.payload_sha256 == explicit.payload_sha256
    assert "headless" not in original.payload
    registered = api.PublicRequestStore(runtime.root / "public-store").register(
        original, "original-pane"
    )
    order = r.load_order(registered.order_path)
    assert order["cockpit_pane"] == "original-pane" and order["management"] == {
        "mode": "dedicated"
    }


def test_exec_spawn_failure_is_terminal_without_helpers(runtime: Runtime) -> None:
    (runtime.bin / "claude").write_text("#!/definitely/missing/interpreter\n")
    result = runtime.request()
    assert result.returncode == 1, result.stderr
    status = json.loads(result.stdout)
    assert "exited 127" in status["result"]["reason"]
    assert status["receipt"]["cleanup"]["verified_absent"] is True


def wait_file(path: Path, seconds: float = 10) -> str:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.is_file():
            return path.read_text()
        time.sleep(0.025)
    raise AssertionError(f"fixture file did not appear: {path}")


@pytest.mark.parametrize("mode", ["descendant", "daemon"])
def test_public_authenticated_cancel_cleans_descendants(
    runtime: Runtime, mode: str
) -> None:
    runtime.env["FAKE_MODE"] = mode
    result = runtime.request(wait=False)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    token = runtime.root / "token"
    token.write_text(status["state"]["requester_control_token"])
    token.chmod(0o600)
    child_pid = int(wait_file(runtime.root / "child"))
    birth = r.headless.process.identity(child_pid)
    cancelled = runtime.command(
        "cancel",
        "--request",
        runtime.request_id,
        "--control-token-file",
        str(token),
        "--json",
    )
    assert cancelled.returncode == 0, (cancelled.stdout, cancelled.stderr)
    terminal = runtime.status()
    assert terminal["receipt"]["cancelled"] is True
    assert terminal["receipt"]["cleanup"]["verified_absent"] is True
    assert not r.headless.process.alive(birth)
    again = runtime.command(
        "cancel",
        "--request",
        runtime.request_id,
        "--control-token-file",
        str(token),
        "--json",
    )
    assert again.returncode == 0


def test_normal_exit_cleans_daemonized_descendant(runtime: Runtime) -> None:
    runtime.env["FAKE_MODE"] = "exit-descendant"
    result = runtime.request()
    assert result.returncode == 0, result.stderr
    child_pid = int(wait_file(runtime.root / "child"))
    assert r._process_start_time(child_pid) is None


def test_supervisor_crash_reconciles_without_helpers(runtime: Runtime) -> None:
    runtime.env["FAKE_MODE"] = "daemon"
    result = runtime.request(wait=False)
    assert result.returncode == 0, result.stderr
    child_pid = int(wait_file(runtime.root / "child"))
    child = r.headless.process.identity(child_pid)
    state = runtime.status()["state"]
    assert r.headless.process.signal_identity(
        r.headless.runner_identity(state), signal.SIGKILL
    )
    reconciled = runtime.command("reconcile", "--json")
    assert reconciled.returncode == 0, reconciled.stderr
    status = runtime.status()
    assert status["result"]["verdict"] == "blocked"
    assert "supervisor disappeared" in status["result"]["reason"]
    assert status["receipt"]["cleanup"]["verified_absent"] is True
    assert not r.headless.process.alive(child)
    assert len(runtime.calls()) == 1


def test_total_timeout_after_apparent_final_output(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # Exercise real public parsing/registration/subprocess run-job, shortening only the
    # persisted fixture order's duration so the regression does not take 60 seconds.
    for key, value in runtime.env.items():
        monkeypatch.setenv(key, value)
    runtime.env["FAKE_MODE"] = "apparent-final"
    monkeypatch.setenv("FAKE_MODE", "apparent-final")
    monkeypatch.chdir(runtime.cwd)
    r._bind_command_runtime(
        runtime.root / "runtime/ledger", runtime.root / "runtime/services.json"
    )
    r._set_command_authorized(True)
    monkeypatch.setattr(r, "print", r.command_runtime.command_print, raising=False)
    monkeypatch.setattr(api, "print", api.command_runtime.command_print, raising=False)
    original = api.PublicRequestStore.register

    def short_registration(self: Any, request: Any, pane: Any) -> Any:
        registered = original(self, request, pane)
        order = r.load_order(registered.order_path)
        order["timeout_ms"] = 500
        r.JobLedger._write_json(registered.order_path, order)
        return registered

    monkeypatch.setattr(api.PublicRequestStore, "register", short_registration)
    code = api.main(
        [
            "request",
            "--file",
            str(_json_request(runtime, headless=True)),
            "--wait",
            "--json",
        ]
    )
    assert code == 1
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["result"]["verdict"] == "blocked"
    assert "total headless deadline exceeded" in status["result"]["reason"]
    assert status["receipt"]["cleanup"]["verified_absent"] is True


@pytest.mark.parametrize(
    "mode,expected",
    [("answer", "passed"), ("success", "blocked"), ("answer-error", "blocked")],
)
def test_headless_specialist_requires_typed_answer(
    runtime: Runtime, mode: str, expected: str
) -> None:
    runtime.env["FAKE_MODE"] = mode
    result = runtime.command(
        "request",
        "--type",
        "specialist",
        "--request-id",
        runtime.request_id,
        "--specialist",
        "backend",
        "--prompt-file",
        str(runtime.prompt),
        "--cwd",
        str(runtime.cwd),
        "--headless",
        "--wait",
        "--json",
    )
    assert result.returncode == (0 if expected == "passed" else 1), result.stderr
    status = json.loads(result.stdout)
    assert status["result"]["verdict"] == expected
    answer = next(item for item in status["artifacts"] if item["kind"] == "answer")
    assert answer["present"] is True
    assert len(runtime.calls()) == 1


def test_guardian_crash_never_claims_descendant_cleanup(runtime: Runtime) -> None:
    runtime.env["FAKE_MODE"] = "sleep"
    result = runtime.request(wait=False)
    assert result.returncode == 0, result.stderr
    directory = next((runtime.root / "runtime/ledger/requests").glob("*/headless/*"))
    guardian = json.loads(wait_file(directory / "ready.json"))
    worker = json.loads(wait_file(directory / "worker.json"))
    assert r.headless.process.signal_identity(guardian, signal.SIGKILL)
    # The supervisor observes the guardian crash, stops the recorded worker, and
    # publishes cleanup-failed rather than inventing proof for untracked descendants.
    awaited = runtime.command("await", "--request", runtime.request_id, "--json")
    assert awaited.returncode == 1, awaited.stderr
    status = json.loads(awaited.stdout)
    assert status["state"]["state"] == "cleanup-failed"
    assert status["result"]["verdict"] == "blocked"
    assert status["receipt"]["cleanup"]["verified_absent"] is False
    assert not r.headless.process.alive(worker)
    assert len(runtime.calls()) <= 1


def test_guardian_gate_fenced_before_exec_never_launches(runtime: Runtime) -> None:
    directory = runtime.root / "gate-fixture"
    directory.mkdir()
    lease = directory / "lease.json"
    lease.write_text(json.dumps({"token": "fixture", "expires_at": time.time() + 60}))
    config = {
        "argv": [str(runtime.bin / "claude")],
        "cwd": str(runtime.cwd),
        "env": {},
        "stdin": str(runtime.prompt),
        "stdout": str(directory / "stdout.json"),
        "stderr": str(directory / "stderr.log"),
        "boot_id": r.headless.process.boot_id(),
        "runner": r.headless.process.identity(os.getpid()),
        "token": "fixture",
        "lease_path": str(lease),
        "deadline": time.monotonic() + 5,
    }
    path = directory / "config.json"
    path.write_text(json.dumps(config))
    with (
        (directory / "guardian.log").open("wb") as log,
        subprocess.Popen(
            [sys.executable, str(HERE / "_headless_process.py"), str(path)],
            env=runtime.env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        ) as guardian,
    ):
        wait_file(directory / "ready.json")
        # No go.json: cancellation before the first gate must leave no worker.
        r.headless.process.write_json(
            lease, {"token": "cancelled", "expires_at": time.time() + 60}
        )
        assert guardian.wait(timeout=5) == 0
    evidence = json.loads((directory / "exit.json").read_text())
    assert evidence["worker"] is None and evidence["cleanup"]["verified_absent"] is True
    assert "fenced" in evidence["reason"]
    assert not runtime.calls()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process identity regression")
def test_birth_and_boot_identity_mismatch_never_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = r.headless.process
    current = process.identity(os.getpid())

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("stale process identity reached a signal")

    monkeypatch.setattr(process.signal, "pidfd_send_signal", forbidden)
    assert (
        process.signal_identity({**current, "start": "wrong"}, signal.SIGTERM) is False
    )
    assert (
        process.signal_identity({**current, "boot_id": "old-boot"}, signal.SIGTERM)
        is False
    )
    # Simulate reuse between pidfd_open and the post-open birth recheck.
    original_open = process.os.pidfd_open
    original_proc = process.proc
    calls = []

    def reused_open(pid: int) -> int:
        fd = original_open(pid)
        calls.append(pid)
        monkeypatch.setattr(
            process, "proc", lambda _pid: {**original_proc(pid), "start": "reused"}
        )
        return fd

    monkeypatch.setattr(process.os, "pidfd_open", reused_open)
    assert process.signal_identity(current, signal.SIGTERM) is False
    assert calls == [current["pid"]]


def test_expired_headless_lease_cannot_be_blindly_reclaimed(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in runtime.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(r, "_BOUND_LEDGER_ROOT", runtime.root / "runtime/ledger")
    validated = api.validate_request(
        api.contract.parse_argv(
            ["request", "--file", str(_json_request(runtime, headless=True))]
        ),
        runtime.cwd,
    )
    registered = api.PublicRequestStore().register(validated, None)
    order = r.load_order(registered.order_path)
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        100,
        owner={**r.headless.runner_owner(r), "runner_boot_id": "fixture-previous-boot"},
    )
    assert token
    path = ledger.active / "requests" / key / "lease.json"
    lease = json.loads(path.read_text())
    lease["expires_at"] = int(time.time()) - 1
    ledger._write_json(path, lease)
    assert (
        ledger.claim(key, order["order_id"], 100, owner={"execution_mode": "headless"})
        is None
    )
    assert path.exists()
    assert r._reconcile_claim(ledger, key, lease, force=False)
    assert ledger.completed_result(key, order)["verdict"] == "blocked"


def _inprocess(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in runtime.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(runtime.cwd)
    monkeypatch.setattr(r, "_BOUND_LEDGER_ROOT", runtime.root / "runtime/ledger")
    monkeypatch.setattr(r, "STATE_FILE", runtime.root / "runtime/services.json")
    monkeypatch.setattr(r, "_COMMAND_AUTHORIZED", True)
    monkeypatch.setattr(r, "print", r.command_runtime.command_print, raising=False)
    monkeypatch.setattr(api, "print", api.command_runtime.command_print, raising=False)


@pytest.mark.parametrize("boundary", ["supervisor", "guardian"])
def test_python_spawn_failures_publish_truthful_receipts(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, capsys: Any, boundary: str
) -> None:
    _inprocess(runtime, monkeypatch)

    def fail_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("fixture spawn unavailable")

    if boundary == "supervisor":
        monkeypatch.setattr(r, "_spawn_job", fail_spawn)
    else:
        original_popen = subprocess.Popen

        def guardian_failure(argv: list[str], *args: Any, **kwargs: Any) -> Any:
            if len(argv) > 1 and argv[1] == str(HERE / "_headless_process.py"):
                raise OSError("fixture guardian spawn unavailable")
            return original_popen(argv, *args, **kwargs)

        monkeypatch.setattr(r.headless.subprocess, "Popen", guardian_failure)

        def inline_runner(key: str, _roster: str) -> None:
            assert r.headless.run(r, key) == 1

        monkeypatch.setattr(r, "_spawn_job", inline_runner)
    code = api.main(
        [
            "request",
            "--file",
            str(_json_request(runtime, headless=True)),
            "--wait",
            "--json",
        ]
    )
    assert code == 1
    status = json.loads(capsys.readouterr().out)
    assert status["result"]["verdict"] == "blocked"
    assert "spawn unavailable" in status["result"]["reason"]
    assert status["receipt"]["cleanup"]["verified_absent"] is True
    assert not runtime.calls()


def test_headless_intake_and_cancel_ignore_unrelated_pane_history(
    runtime: Runtime,
) -> None:
    # A pane-mode orphan deliberately has no usable Herdr/ledger metadata. The
    # headless operation must not sweep it or depend on its transport being available.
    orphan = runtime.root / "runtime/ledger/active/requests/unrelated-pane"
    orphan.mkdir(parents=True)
    (orphan / "lease.json").write_text(
        json.dumps({"token": "unrelated", "expires_at": 1})
    )
    try:
        runtime.env["FAKE_MODE"] = "sleep"
        result = runtime.request(wait=False)
        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        token = runtime.root / "token"
        token.write_text(status["state"]["requester_control_token"])
        token.chmod(0o600)
        cancelled = runtime.command(
            "cancel",
            "--request",
            runtime.request_id,
            "--control-token-file",
            str(token),
            "--json",
        )
        assert cancelled.returncode == 0, cancelled.stderr
        assert json.loads((orphan / "lease.json").read_text()) == {
            "token": "unrelated",
            "expires_at": 1,
        }
    finally:
        (orphan / "lease.json").unlink()
        orphan.rmdir()


def test_cancel_stops_launcher_before_not_created_cleanup(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inprocess(runtime, monkeypatch)
    validated = api.validate_request(
        api.contract.parse_argv(
            ["request", "--file", str(_json_request(runtime, headless=True))]
        ),
        runtime.cwd,
    )
    registered = api.PublicRequestStore().register(validated, None)
    order = r.load_order(registered.order_path)
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    assert ledger.claim(
        key,
        order["order_id"],
        1000,
        owner={**r.headless.runner_owner(r), "runner_boot_id": "fixture-previous-boot"},
    )
    control_token = ledger.state(key)["requester_control_token"]
    events = []
    original_cleanup = r.headless.cleanup

    def stopped(*_args: Any) -> bool:
        events.append("launcher-stopped")
        return True

    def checked(*args: Any) -> dict:
        assert events == ["launcher-stopped"]
        events.append("cleanup-checked")
        return original_cleanup(*args)

    monkeypatch.setattr(r.headless, "stop_runner", stopped)
    monkeypatch.setattr(r.headless, "cleanup", checked)
    outcome = r.cmd_cancel(str(registered.order_path), control_token)
    assert outcome["cancelled"] is True
    assert events[:2] == ["launcher-stopped", "cleanup-checked"]
    assert outcome["receipt"]["cleanup"]["verified_absent"] is True
    assert not runtime.calls()
