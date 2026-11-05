# pyright: reportMissingImports=false
"""Regressions for the independent headless review; all process/provider state is isolated."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from headless_test import Runtime, _inprocess, _json_request, api, r
from headless_test import runtime as runtime

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux headless regressions"
)


def _registered(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, owner: dict
) -> tuple:
    _inprocess(runtime, monkeypatch)
    validated = api.validate_request(
        api.contract.parse_argv(
            ["request", "--file", str(_json_request(runtime, headless=True))]
        ),
        runtime.cwd,
    )
    registered = api.PublicRequestStore().register(validated, None)
    ledger = r.JobLedger()
    order = r.load_order(registered.order_path)
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        1000,
        owner={
            **owner,
            "request_id": runtime.request_id,
            "generation": 1,
        },
    )
    assert token
    return registered, ledger, key, token, order


def _no_numeric_signal(*_args: Any, **_kwargs: Any) -> None:
    pytest.fail("headless supervisor reached numeric os.kill or legacy termination")


def test_shared_termination_entry_cannot_numerically_signal_headless(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = {**r.headless.runner_owner(r), "runner_boot_id": "fixture-previous-boot"}
    _request, ledger, key, _token, _order = _registered(runtime, monkeypatch, owner)
    lease = r._active_claim_for(ledger, key)
    calls = []
    original_stop = r.headless.stop_runner

    def stop(record: dict) -> bool:
        calls.append(record)
        return original_stop(record)

    monkeypatch.setattr(r.os, "kill", _no_numeric_signal)
    monkeypatch.setattr(r.headless, "stop_runner", stop)
    r._terminate_owned_runner(owner["runner_pid"], key)
    assert calls == [lease]
    assert r._reconcile_claim(ledger, key, lease, force=True)


@pytest.mark.parametrize("operation", ["cancel", "reconcile"])
def test_public_controls_stop_real_supervisor_only_via_pidfd(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    operation: str,
) -> None:
    runtime.env["FAKE_MODE"] = "sleep"
    accepted = runtime.request(wait=False)
    assert accepted.returncode == 0, accepted.stderr
    status = json.loads(accepted.stdout)
    _inprocess(runtime, monkeypatch)
    ledger = r.JobLedger()
    order = r.load_order(status["order_path"])
    key = ledger.key_for_order(order)
    lease = r._active_claim_for(ledger, key)
    assert lease and r.headless.runner_alive(lease)
    assert lease["runner_boot_id"] == r.headless.process.boot_id()
    recorded = json.loads((ledger.request_dir(key) / "runner.json").read_text())
    assert r.headless.runner_identity(recorded) == r.headless.runner_identity(lease)
    monkeypatch.setattr(r.os, "kill", _no_numeric_signal)
    monkeypatch.setattr(r, "_terminate_owned_runner", _no_numeric_signal)
    original_cleanup = r.headless.cleanup
    checks = []

    def checked_cleanup(*args: Any) -> dict:
        assert not r.headless.runner_alive(lease), (
            "journals inspected before proven launcher absence"
        )
        checks.append(True)
        return original_cleanup(*args)

    monkeypatch.setattr(r.headless, "cleanup", checked_cleanup)
    if operation == "cancel":
        token = runtime.root / "token"
        token.write_text(status["state"]["requester_control_token"])
        token.chmod(0o600)
        code = api.main(
            [
                "cancel",
                "--request",
                runtime.request_id,
                "--control-token-file",
                str(token),
                "--json",
            ]
        )
    else:
        lease["expires_at"] = 1
        ledger._write_json(ledger.active / "requests" / key / "lease.json", lease)
        code = api.main(["reconcile", "--json"])
    assert code == 0
    capsys.readouterr()
    receipt = ledger.completed_receipt(key, order)
    assert checks and receipt["verdict"] == "blocked"
    assert receipt["cleanup"]["verified_absent"] is True
    assert receipt["runner_boot_id"] == recorded["runner_boot_id"]
    marker = json.loads((ledger.request_dir(key) / "runner-completed.json").read_text())
    assert r.headless.runner_identity(marker) == r.headless.runner_identity(receipt)
    assert not r.headless.runner_alive(lease)


@pytest.mark.parametrize("operation", ["cancel", "forced-reconcile"])
@pytest.mark.parametrize(
    "scenario",
    [
        "old-boot",
        "birth-mismatch",
        "disappeared",
        "reuse-at-pin",
        "reuse-at-signal",
        "exit-at-signal",
    ],
)
def test_supervisor_identity_boundaries_never_signal_replacement(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    operation: str,
    scenario: str,
) -> None:
    process = r.headless.process
    fake_pid = 987654321
    owner = {
        "execution_mode": "headless",
        "runner_pid": fake_pid,
        "runner_start_time": "10",
        "runner_boot_id": process.boot_id(),
    }
    if scenario == "old-boot":
        owner["runner_boot_id"] = "fixture-previous-boot"
    registered, ledger, key, _token, order = _registered(runtime, monkeypatch, owner)
    lease = r._active_claim_for(ledger, key)
    assert lease
    monkeypatch.setattr(r.os, "kill", _no_numeric_signal)
    monkeypatch.setattr(r, "_terminate_owned_runner", _no_numeric_signal)
    real_open = process.os.pidfd_open
    gone = [scenario == "disappeared"]
    replaced = [scenario == "birth-mismatch"]
    opened = []
    sent = []

    def pinned_open(pid: int) -> int:
        assert pid == fake_pid
        opened.append(pid)
        if gone[0]:
            raise ProcessLookupError("fixture process exited")
        if scenario == "reuse-at-pin":
            replaced[0] = True
        # A real descriptor is used for poll/close, but signals are intercepted below.
        # No signal is ever delivered to pytest or any unrelated real process.
        return real_open(os.getpid())

    def current_proc(pid: int) -> dict | None:
        assert pid == fake_pid
        if gone[0]:
            return None
        return {
            "pid": pid,
            "start": "11" if replaced[0] else "10",
            "state": "S",
            "ppid": 1,
        }

    def pinned_send(fd: int, sig: int) -> None:
        assert scenario in ("reuse-at-signal", "exit-at-signal")
        assert fd != fake_pid and sig == signal.SIGTERM
        sent.append((fd, sig))
        if scenario == "exit-at-signal":
            gone[0] = True
            raise ProcessLookupError("fixture exit between check and pinned signal")
        replaced[0] = True

    monkeypatch.setattr(process.os, "pidfd_open", pinned_open)
    monkeypatch.setattr(process, "proc", current_proc)
    monkeypatch.setattr(process.signal, "pidfd_send_signal", pinned_send)
    original_cleanup = r.headless.cleanup

    def checked_cleanup(*args: Any) -> dict:
        assert not r.headless.runner_alive(lease)
        return original_cleanup(*args)

    monkeypatch.setattr(r.headless, "cleanup", checked_cleanup)
    if operation == "cancel":
        token_file = runtime.root / "token"
        token_file.write_text(lease["requester_control_token"])
        token_file.chmod(0o600)
        assert (
            api.main(
                [
                    "cancel",
                    "--request",
                    runtime.request_id,
                    "--control-token-file",
                    str(token_file),
                    "--json",
                ]
            )
            == 0
        )
        capsys.readouterr()
    else:
        assert r._reconcile_claim(ledger, key, lease, force=True)
    assert ledger.completed_result(key, order)["verdict"] == "blocked"
    assert not runtime.calls()
    if scenario == "old-boot":
        assert opened == []
    assert len(sent) == (1 if scenario in ("reuse-at-signal", "exit-at-signal") else 0)


@pytest.mark.parametrize("route", ["exact", "sweep"])
def test_terminal_supervisor_absence_uses_boot_identity_and_reconstruction(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    owner = {**r.headless.runner_owner(r), "runner_boot_id": "fixture-previous-boot"}
    registered, ledger, key, token, order = _registered(runtime, monkeypatch, owner)
    manifest = r.completion.build_manifest(
        order, ledger.request_dir(key), token, runtime.request_id
    )
    result = r._write_required_blocked_bundle(
        order, manifest, "fixture interrupted request"
    )
    assert ledger.finalize(
        key,
        token,
        order,
        result,
        cleanup={"status": "not-created", "verified_absent": True},
        defer_runner_completion=True,
    )
    (ledger.request_dir(key) / "runner.json").unlink()
    monkeypatch.setattr(r.os, "kill", _no_numeric_signal)
    monkeypatch.setattr(r.headless.process.os, "pidfd_open", _no_numeric_signal)
    # Numeric PID and birth match this live pytest process; only the old boot proves
    # that the recorded supervisor is absent. Receipt reconstruction must retain it.
    if route == "exact":
        assert r._reconcile_exact_request_runner(ledger, key)
    else:
        assert r._reconcile_terminal_runners(ledger) == 1
    recovered = json.loads((ledger.request_dir(key) / "runner.json").read_text())
    completed = json.loads(
        (ledger.request_dir(key) / "runner-completed.json").read_text()
    )
    assert (
        recovered["runner_boot_id"]
        == completed["runner_boot_id"]
        == owner["runner_boot_id"]
    )
    assert r.headless.runner_identity(completed) == r.headless.runner_identity(owner)


def test_incomplete_supervisor_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(r.headless.process.os, "pidfd_open", _no_numeric_signal)
    incomplete = {
        "execution_mode": "headless",
        "runner_pid": os.getpid(),
        "runner_start_time": "10",
    }
    for call in (
        r.headless.runner_alive,
        r.headless.stop_runner,
        r._recorded_runner_alive,
    ):
        with pytest.raises(RuntimeError, match="complete PID/birth/boot"):
            call(incomplete)


@pytest.mark.parametrize("route", ["exact", "sweep"])
def test_terminal_headless_identity_never_falls_back_to_pane_rules(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    owner = {**r.headless.runner_owner(r), "runner_boot_id": "fixture-previous-boot"}
    _registered_request, ledger, key, token, order = _registered(
        runtime, monkeypatch, owner
    )
    manifest = r.completion.build_manifest(
        order, ledger.request_dir(key), token, runtime.request_id
    )
    result = r._write_required_blocked_bundle(
        order, manifest, "fixture interrupted request"
    )
    assert ledger.finalize(
        key,
        token,
        order,
        result,
        cleanup={"status": "not-created", "verified_absent": True},
        defer_runner_completion=True,
    )
    path = ledger.request_dir(key) / "runner.json"
    record = json.loads(path.read_text())
    del record["execution_mode"]
    del record["runner_boot_id"]
    ledger._write_json(path, record)
    monkeypatch.setattr(r.os, "kill", _no_numeric_signal)
    monkeypatch.setattr(r.headless.process.os, "pidfd_open", _no_numeric_signal)
    with pytest.raises(RuntimeError, match="explicit execution_mode"):
        if route == "exact":
            r._reconcile_exact_request_runner(ledger, key)
        else:
            r._reconcile_terminal_runners(ledger)
    assert not (ledger.request_dir(key) / "runner-completed.json").exists()


@pytest.mark.parametrize("wait", [True, False])
def test_public_preclaim_exit_after_first_poll_gets_one_terminal_receipt(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    wait: bool,
) -> None:
    _inprocess(runtime, monkeypatch)
    monkeypatch.setattr(r, "LEASE_GRACE_SECONDS", 0)
    monkeypatch.setattr(r, "HEALTH_PROBE_SECONDS", 0.02)
    monkeypatch.setattr(r, "_order_timeout_ms", lambda _order: 300)
    marker = runtime.root / "first-poll"
    children = []
    polls = []

    def spawn(*_args: Any) -> Any:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                "exec('while not p.exists(): time.sleep(.005)'); time.sleep(.08); sys.exit(7)",
                str(marker),
            ],
            env=runtime.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        original_poll = child.poll

        def observed_poll() -> int | None:
            code = original_poll()
            polls.append(code)
            if len(polls) == 1:
                assert code is None
                marker.write_text("first poll observed alive")
            return code

        monkeypatch.setattr(child, "poll", observed_poll)
        return child

    monkeypatch.setattr(r, "_spawn_job", spawn)
    argv = [
        "request",
        "--file",
        str(_json_request(runtime, headless=True)),
        "--json",
        *(["--wait"] if wait else []),
    ]
    started = time.monotonic()
    assert api.main(argv) == 1
    assert time.monotonic() - started < 3
    status = json.loads(capsys.readouterr().out)
    assert polls[0] is None and 7 in polls
    assert len(children) == 1 and children[0].wait(timeout=2) == 7
    assert status["result"]["verdict"] == "blocked"
    assert "exited with code 7" in status["result"]["reason"]
    assert status["receipt"]["cleanup"]["verified_absent"] is True
    receipt = status["receipt"]
    assert api.main(argv) == (1 if wait else 0)
    replay = json.loads(capsys.readouterr().out)
    assert replay["attached"] is True and replay["receipt"] == receipt
    assert len(children) == 1 and not runtime.calls()


def test_public_rejects_truncated_valid_stdout_prefix(runtime: Runtime) -> None:
    runtime.env["FAKE_MODE"] = "truncated-prefix"
    result = runtime.request()
    assert result.returncode == 1, result.stderr
    status = json.loads(result.stdout)
    assert status["result"]["verdict"] == "blocked"
    assert "stdout capture was truncated" in status["result"]["reason"]
    attempt = status["receipt"]["cleanup"]["attempts"][0]
    path = Path(attempt["stdout_path"])
    assert attempt["output_bytes"][str(path)] > attempt["log_limit_bytes"]
    assert (
        json.loads(path.read_text())["is_error"] is False
    )  # Valid retained prefix is insufficient.
    assert len(runtime.calls()) == 1


@pytest.mark.parametrize("unrelated", [False, True])
@pytest.mark.parametrize("pruned", [False, True])
def test_public_verify_rejected_before_residents_reconciliation_or_writes(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    unrelated: bool,
    pruned: bool,
) -> None:
    result = runtime.request()
    assert result.returncode == 0, result.stderr
    if pruned:
        cleaned = runtime.command(
            "cleanup", "--request", runtime.request_id, "--apply", "--json"
        )
        assert cleaned.returncode == 0, cleaned.stderr
    resident = runtime.root / "runtime/specialists/residents/fixture/state.json"
    resident.parent.mkdir(parents=True)
    resident.write_text(
        json.dumps({"enabled": True, "cwd": str(runtime.cwd), "name": "backend"})
    )
    before = resident.read_bytes()
    orphan = runtime.root / "runtime/ledger/active/requests/unrelated-pane"
    if unrelated:
        orphan.mkdir(parents=True)
        (orphan / "lease.json").write_text(
            json.dumps({"token": "fixture", "expires_at": 1})
        )
    argv = [
        "verify",
        "--request",
        runtime.request_id,
        "--offering",
        "claude-sonnet-5-5",
        "--effort",
        "high",
        "--agent",
        "backend",
        "--json",
    ]
    try:
        verified = runtime.command(*argv)
        assert verified.returncode == 1
        assert "verify is unsupported for headless requests" in verified.stderr
        assert "Traceback" not in verified.stderr
        assert not list(runtime.root.rglob("verify-*"))
        assert (
            resident.read_bytes() == before and not (resident.parent / "lock").exists()
        )
        _inprocess(runtime, monkeypatch)
        monkeypatch.setattr(api.residents, "preflight", _no_numeric_signal)
        monkeypatch.setattr(r, "cmd_reconcile", _no_numeric_signal)
        with pytest.raises(
            SystemExit, match="verify is unsupported for headless requests"
        ):
            api.main(
                argv
            )  # The direct public entry must reject too, not only its client guard.
        assert not list(runtime.root.rglob("verify-*"))
        assert len(runtime.calls()) == 1
    finally:
        if unrelated:
            (orphan / "lease.json").unlink()
            orphan.rmdir()
