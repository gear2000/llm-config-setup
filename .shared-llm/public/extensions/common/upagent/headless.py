"""Linux-local Claude one-shot lifecycle, isolated from every pane/helper path.

The Recruiter injects its ledger/contracts here. Only this module's narrow entry points
are called by existing dispatch, run-job, cancellation, and recovery boundaries.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "upagent_headless_process", HERE / "_headless_process.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("cannot load headless process supervision")
process = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(process)


def require_supported(r: Any, order: dict) -> None:
    if sys.platform != "linux":
        raise r.RecruiterError("headless execution requires Linux")
    r.contracts.parse_order(json.dumps(order))
    if order.get("execution_mode") != "headless" or "offering_snapshot" not in order:
        raise r.RecruiterError(
            "headless requires an explicit mode and immutable offering snapshot"
        )
    r.offering_catalog.render_headless_argv(order["offering_snapshot"], order["agent"])


def runner_owner(r: Any) -> dict:
    identity = process.identity(os.getpid())
    return {
        "execution_mode": "headless",
        "runner_pid": identity["pid"],
        "runner_start_time": identity["start"],
        "runner_boot_id": identity["boot_id"],
        "runner_argv": r._process_cmdline(os.getpid()),
        "runner_session_id": os.getsid(0),
    }


def runner_identity(owner: dict) -> dict:
    identity = {
        "pid": owner.get("runner_pid"),
        "start": owner.get("runner_start_time"),
        "boot_id": owner.get("runner_boot_id"),
    }
    if owner.get("execution_mode") != "headless":
        raise RuntimeError(
            "headless supervisor identity requires explicit execution_mode"
        )
    process.validate_identity(identity)
    return identity


def runner_metadata(owner: dict) -> dict:
    """Extra persisted fields for headless records; legacy pane records stay unchanged."""
    if owner.get("execution_mode") == "headless":
        identity = runner_identity(owner)
        return {"execution_mode": "headless", "runner_boot_id": identity["boot_id"]}
    return {}


def runner_alive(owner: dict) -> bool:
    return process.alive(runner_identity(owner))


def stop_runner(owner: dict) -> bool:
    """Stop only this complete identity; never signal a numeric supervisor PID."""
    identity = runner_identity(owner)
    process.signal_identity(identity, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not process.alive(identity):
            return True
        time.sleep(process.POLL_SECONDS)
    process.signal_identity(identity, signal.SIGKILL)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if not process.alive(identity):
            return True
        time.sleep(process.POLL_SECONDS)
    return not process.alive(identity)


def submit(r: Any, order: dict, roster_path: str, *, wait: bool) -> int:
    require_supported(r, order)
    ledger = r.JobLedger()
    key, _ = ledger.submit(order)
    owner = {
        **runner_owner(r),
        "request_id": r.lifecycle.request_identity(order),
        "generation": 1,
    }
    if ledger.completed_result(key, order) is None:
        try:
            child = r._spawn_job(key, roster_path)
        except OSError as error:
            if not r._terminalize_start_failure(ledger, key, order, owner, error):
                raise r.RecruiterError(
                    f"headless supervisor spawn failed: {error}"
                ) from error
            child = None
        deadline = time.monotonic() + 20
        while ledger.completed_result(key, order) is None:
            if not wait and ledger.state(key).get("state") == "running":
                return 0
            if child is not None and child.poll() is not None:
                r._settle_exited_spawn(
                    ledger,
                    key,
                    order,
                    owner,
                    returncode=child.returncode,
                    operation="headless",
                )
                child = None
            # A Popen handle can still exit before claiming after its first poll.
            # Do not abandon it to await until a durable winner exists.
            lease = r._active_claim_for(ledger, key)
            if lease is not None and wait:
                return r.cmd_await(str(ledger.request_dir(key) / "request.json"), 0)
            if time.monotonic() >= deadline:
                raise r.RecruiterError(
                    "headless supervisor did not reach running or terminal within 20 seconds; use the same request id to inspect/attach"
                )
            time.sleep(process.POLL_SECONDS)
    result = ledger.completed_result(key, order)
    return 0 if result["verdict"] == "passed" else 1


def _owned(ledger: Any, key: str, token: str) -> bool:
    path = ledger.active / "requests" / key / "lease.json"
    if not path.exists():
        return False
    return process.read_json(path)["token"] == token


def _set_running(ledger: Any, key: str, token: str, worker: dict) -> None:
    with ledger._claim_lock(key):
        if not _owned(ledger, key, token):
            return
        lease = process.read_json(ledger.active / "requests" / key / "lease.json")
        ledger._event(key, "headless-worker-started", worker=worker)
        ledger._snapshot(key, "running", **lease, worker_process=worker)


def cleanup(r: Any, ledger: Any, key: str) -> dict:
    """Wait for subreaper proof. Never equate a dead guardian with cleaned descendants."""
    root = ledger.request_dir(key) / "headless"
    directories = sorted(root.glob("*/config.json"))
    if not directories:
        return {
            "status": "not-created",
            "verified_absent": True,
            "worker_pane": None,
            "execution_mode": "headless",
        }
    attempts = []
    for config_path in directories:
        config = process.read_json(config_path)
        directory = config_path.parent
        if config["boot_id"] != process.boot_id():
            attempts.append({"status": "host-rebooted", "verified_absent": True})
            continue
        deadline = time.monotonic() + process.GATE_SECONDS + 7
        evidence = None
        while time.monotonic() < deadline:
            exit_path = directory / "exit.json"
            ready_path = directory / "ready.json"
            if exit_path.is_file():
                evidence = process.read_json(exit_path)
                if evidence.get("token") != config["token"]:
                    raise r.RecruiterError(
                        "headless exit evidence has the wrong lease identity"
                    )
                if not process.alive(evidence["guardian"]):
                    break
            elif ready_path.is_file() and not process.alive(
                process.read_json(ready_path)
            ):
                break
            time.sleep(process.POLL_SECONDS)
        if evidence is None or process.alive(evidence["guardian"]):
            launch_path = directory / "launch.json"
            if launch_path.is_file():
                launched = process.read_json(launch_path)
                if not process.alive(launched) and not (directory / "go.json").exists():
                    attempts.append(
                        {
                            "status": "not-created",
                            "verified_absent": True,
                            "reason": "guardian exited before launch gate release",
                        }
                    )
                    continue
            # A crashed/stuck guardian lost its subreaper proof. Stop only exact recorded
            # processes; do NOT claim absence of their potentially orphaned descendants.
            errors = []
            for name in ("worker.json", "launch.json"):
                record_path = directory / name
                if record_path.is_file():
                    record = process.read_json(record_path)
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            process.signal_identity(record, sig)
                        except OSError as error:
                            errors.append(str(error))
                        time.sleep(process.POLL_SECONDS)
            attempts.append(
                {
                    "status": "cleanup-failed",
                    "verified_absent": False,
                    "reason": "guardian exit/descendant cleanup not proven",
                    "directory": str(directory),
                    "errors": errors,
                }
            )
        else:
            attempts.append(
                {
                    **evidence["cleanup"],
                    "directory": str(directory),
                    "stdout_path": str(directory / "stdout.json"),
                    "stderr_path": str(directory / "stderr.log"),
                    "output_bytes": evidence.get("output_bytes", {}),
                    "log_limit_bytes": process.LOG_LIMIT,
                }
            )
    verified = all(item.get("verified_absent") is True for item in attempts)
    return {
        "status": "closed" if verified else "cleanup-failed",
        "verified_absent": verified,
        "worker_pane": None,
        "execution_mode": "headless",
        "attempts": attempts,
    }


def _read_stdout(directory: Path, evidence: dict) -> dict:
    path = directory / "stdout.json"
    counts = evidence.get("output_bytes")
    count = counts.get(str(path)) if isinstance(counts, dict) else None
    limit = evidence.get("log_limit_bytes")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit <= 0
    ):
        raise ValueError(
            "stdout capture has no valid durable byte-count/limit evidence"
        )
    if count > limit:
        raise ValueError(f"stdout capture was truncated: {count} bytes exceeds {limit}")
    if path.stat().st_size != count:
        raise ValueError("stdout capture size contradicts its durable byte count")
    return process.read_json(path)


def _result(
    r: Any,
    ledger: Any,
    key: str,
    order: dict,
    manifest: Any,
    clean: dict,
    *,
    recovery: bool = False,
) -> dict:
    directory = ledger.request_dir(key) / "headless" / manifest.lease_token
    reason = None
    exit_path = directory / "exit.json"
    if clean["verified_absent"] is not True:
        reason = "headless process/descendant cleanup could not be verified"
    elif not exit_path.is_file():
        reason = "headless worker has no durable exit evidence"
    else:
        evidence = process.read_json(exit_path)
        if evidence["token"] != manifest.lease_token:
            reason = "headless exit evidence lease mismatch"
        elif evidence.get("reason") is not None:
            reason = str(evidence["reason"])
        elif evidence.get("returncode") != 0:
            reason = f"Claude print process exited {evidence.get('returncode')}"
        else:
            try:
                output = _read_stdout(directory, evidence)
            except (OSError, ValueError) as error:
                reason = (
                    f"Claude print JSON is missing, truncated, or malformed: {error}"
                )
            else:
                if (
                    output.get("type") != "result"
                    or output.get("is_error") is not False
                    or output.get("subtype") != "success"
                ):
                    reason = (
                        "Claude print output did not report a non-error success result"
                    )
                elif output.get("permission_denials"):
                    reason = "Claude reported permission denials; headless does not waive approval gates"
    if reason is None:
        try:
            result = r.completion.validate_bundle(
                manifest,
                load_result=r.contracts.result_loader(order),
                load_answer=r.contracts_consult.load_answer,
            )
            if manifest.consult_id is not None and result["verdict"] == "passed":
                answer = r.contracts_consult.load_answer(
                    manifest.artifact("answer").staging_path, manifest.consult_id
                )
                if "error" in answer:
                    raise r.CompletionError(
                        "headless specialist reported passed but answer.json reports an error"
                    )
        except r.CompletionError as error:
            reason = f"headless one-shot completion is invalid (no same-live-worker repair): {error}"
        else:
            return result
    if recovery:
        reason = f"headless runner reconciliation: {reason}"
    ledger._event(key, "headless-blocked", reason=reason)
    result = r._write_required_blocked_bundle(
        order, manifest, reason, archive_overridden=True
    )
    result["full_log"] = str(ledger.request_dir(key) / "supervisor.log")
    ledger._write_json(manifest.artifact("result").staging_path, result)
    return result


def run(r: Any, key: str) -> int:
    ledger = r.JobLedger()
    order = ledger.order(key)
    require_supported(r, order)
    deadline = time.monotonic() + r._order_timeout_ms(order) / 1000
    owner = {
        **runner_owner(r),
        "generation": 1,
        "request_id": r.lifecycle.request_identity(order),
    }
    token = ledger.claim(
        key, order["order_id"], r._order_timeout_ms(order), owner=owner
    )
    if token is None:
        return 0
    manifest = r.completion.build_manifest(
        order, ledger.request_dir(key), token, r.lifecycle.request_identity(order)
    )
    directory = ledger.request_dir(key) / "headless" / token
    directory.mkdir(parents=True, mode=0o700)
    instructions = ledger.worker_instructions_path(key, token)
    r._write_worker_instructions(
        order, manifest.artifact("result").staging_path, instructions, manifest
    )
    argv = r.offering_catalog.render_headless_argv(
        order["offering_snapshot"], order["agent"]
    )
    executable = shutil.which(argv[0])
    if executable is None:
        raise r.RecruiterError("headless Claude executable is not on PATH")
    # Resolve ONCE: execvpe would retry later PATH entries on ENOENT (including a
    # broken shebang), potentially launching a different installed account/runtime.
    argv[0] = str(Path(executable).resolve())
    config = {
        "argv": argv,
        "cwd": order["cwd"],
        "env": order.get("env", {}),
        "stdin": str(instructions),
        "stdout": str(directory / "stdout.json"),
        "stderr": str(directory / "stderr.log"),
        "boot_id": process.boot_id(),
        "runner": runner_identity(owner),
        "token": token,
        "lease_path": str(ledger.active / "requests" / key / "lease.json"),
        "deadline": deadline,
    }
    process.write_json(directory / "config.json", config)
    guardian = None
    try:
        with (directory / "guardian.log").open("ab", buffering=0) as log:
            guardian = subprocess.Popen(
                [
                    sys.executable,
                    str(HERE / "_headless_process.py"),
                    str(directory / "config.json"),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError as error:
        # No child exists when Popen raises. Persist proof rather than leave an intent
        # looking like an unknown started worker during recovery.
        process.write_json(
            directory / "exit.json",
            {
                "token": token,
                "guardian": config["runner"],
                "returncode": None,
                "reason": f"guardian spawn failed: {error}",
                "cleanup": {"status": "not-created", "verified_absent": True},
            },
        )
        result = r._write_required_blocked_bundle(
            order, manifest, f"headless guardian spawn failed: {error}"
        )
        clean = {
            "status": "not-created",
            "verified_absent": True,
            "worker_pane": None,
            "execution_mode": "headless",
        }
    else:
        root = process.identity(guardian.pid)
        process.write_json(directory / "launch.json", root)
        while guardian.poll() is None:
            if time.monotonic() > config["deadline"] + 7:
                break  # Independent bound even if the subreaper itself stops progressing.
            ready_path = directory / "ready.json"
            if ready_path.is_file() and not (directory / "go.json").exists():
                ready = process.read_json(ready_path)
                if ready != root:
                    raise r.RecruiterError(
                        "headless guardian identity changed before gate release"
                    )
                if _owned(ledger, key, token):
                    process.write_json(directory / "go.json", ready)
            worker_path = directory / "worker.json"
            if worker_path.is_file() and ledger.state(key).get("state") == "claimed":
                _set_running(ledger, key, token, process.read_json(worker_path))
            time.sleep(process.POLL_SECONDS)
        if guardian.poll() is not None:
            guardian.wait()
        if not _owned(ledger, key, token):
            return 1  # Cancellation/reconciliation owns publication; never rewrite its bundle.
        clean = cleanup(r, ledger, key)
        result = _result(r, ledger, key, order, manifest, clean)
    code = 0 if result["verdict"] == "passed" else 1
    finalized = ledger.finalize(
        key,
        token,
        order,
        result,
        cleanup=clean,
        defer_runner_completion=True,
        exit_code=code,
        completion_source="claude-headless",
    )
    if finalized:
        ledger.mark_runner_completed(key, source="supervisor")
    return code if finalized else 1


def reconcile(r: Any, ledger: Any, key: str, lease: dict, *, force: bool) -> bool:
    order = ledger.order(key)
    alive = runner_alive(lease)
    if alive and not force and lease["expires_at"] > time.time():
        return False
    if alive and not stop_runner(lease):
        ledger._event(
            key,
            "headless-reconciliation-deferred",
            reason="owned launcher is not yet proven stopped",
        )
        return False
    clean = cleanup(r, ledger, key)
    manifest = r.completion.build_manifest(
        order,
        ledger.request_dir(key),
        lease["token"],
        r.lifecycle.request_identity(order),
    )
    r.completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    result = _result(r, ledger, key, order, manifest, clean, recovery=True)
    finalized = ledger.finalize(
        key,
        lease["token"],
        order,
        result,
        cleanup=clean,
        allow_expired=True,
        defer_runner_completion=True,
        exit_code=1,
        completion_source="headless-reconciler",
    )
    if finalized:
        ledger.mark_runner_completed(key, source="reconciler", require_dead=True)
    return finalized
