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


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
    assert client._is_mutating("public", ["status", "--json"]) is False
    assert client._is_mutating("public", ["lists", "workers"]) is False
    assert client._is_mutating("recruiter", ["--roster", "x", "reconcile"]) is True
    assert client._is_mutating("recruiter", ["status"]) is False
    assert client._is_mutating("phase-await", ["wait"]) is False
    assert client._is_mutating("phase-await", ["publish"]) is True


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
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
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


def test_removed_hub_target_fails_loudly() -> None:
    with pytest.raises(client.ClientError, match="singleton Hub target was removed"):
        client.main(["--target", "hub", "status"])
