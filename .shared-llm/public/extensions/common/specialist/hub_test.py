"""Unit tests for Specialist Hub configuration resolution. No Herdr runtime is launched."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "specialist_hub", Path(__file__).with_name("hub.py")
)
assert _spec is not None
assert _spec.loader is not None
hub = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = hub
_spec.loader.exec_module(hub)


def _write_roster(path: Path, runtime_dir: Path, repo_root: Path | None = None) -> None:
    repo_root_line = f"repo_root: {repo_root}\n" if repo_root is not None else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "runtime_dir: " + str(runtime_dir) + "\n"
        + repo_root_line
        + "agents:\n"
        + "  - name: docs\n"
        + "    location: .claude/agents/docs.md\n"
        + "    cmd: 'claude -p {prompt} --agent docs'\n"
    )


def test_missing_repo_root_uses_roster_ancestor_from_nested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    invocation_dir = repo_root / "nested/invocation"
    invocation_dir.mkdir(parents=True)
    _write_roster(roster, tmp_path / "runtime")
    monkeypatch.delenv("SPECIALIST_HUB_CONFIG", raising=False)
    monkeypatch.chdir(invocation_dir)

    cfg = hub.load_config()

    assert cfg["repo_root"] == repo_root


def test_missing_repo_root_uses_roster_ancestor_from_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    _write_roster(roster, tmp_path / "runtime")
    outside_repository = tmp_path / "outside"
    outside_repository.mkdir()
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))
    monkeypatch.chdir(outside_repository)

    cfg = hub.load_config()

    assert cfg["repo_root"] == repo_root


def test_missing_repo_root_fails_when_roster_is_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = tmp_path / "not-a-repository/config/agents.yaml"
    _write_roster(roster, tmp_path / "runtime")
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))

    with pytest.raises(hub.ConfigError, match="could not find a repository root"):
        hub.load_config()


def test_relative_specialist_location_uses_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    definition = repo_root / ".claude/agents/docs.md"
    definition.parent.mkdir(parents=True)
    definition.write_text('---\ndescription: "Repository docs specialist."\n---\n')
    roster = tmp_path / "config/agents.yaml"
    _write_roster(roster, tmp_path / "runtime", repo_root)
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))

    cfg = hub.load_config()

    assert hub.resolve_specialist_location(cfg, ".claude/agents/docs.md") == definition
    assert hub._description(cfg, cfg["agents"][0]) == "Repository docs specialist."


def _run_codex_consult(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer_body: dict | None,
) -> tuple[dict, list[tuple[str, ...]], str, Path]:
    consult_id = "consult-codex-1"
    answer_path = tmp_path / "private/answer.json"
    consult_path = tmp_path / "consult.json"
    consult_path.write_text(
        json.dumps(
            {
                "consult_id": consult_id,
                "specialist": "python",
                "question": "What is the Python contract?",
                "answer_path": str(answer_path),
                "cwd": str(tmp_path),
            }
        )
    )
    runtime_dir = tmp_path / "runtime with spaces"
    (runtime_dir / "consults").mkdir(parents=True)
    prompt_file = runtime_dir / "consults" / f"{consult_id}.prompt.txt"
    captured_prompt_path = tmp_path / "captured-codex-stdin.txt"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n"
        "Path(os.environ['FAKE_CODEX_PROMPT_CAPTURE']).write_text(sys.stdin.read())\n"
        "answer = os.environ.get('FAKE_CODEX_ANSWER')\n"
        "if answer:\n"
        "    path = Path(os.environ['FAKE_CODEX_ANSWER_PATH'])\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_text(answer)\n"
    )
    fake_codex.chmod(0o755)
    cfg = {"runtime_dir": runtime_dir, "repo_root": tmp_path}
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(hub, "_require_herdr", lambda: None)
    monkeypatch.setattr(hub, "load_config", lambda: cfg)
    monkeypatch.setattr(
        hub,
        "read_index",
        lambda _cfg: {
            "python": {
                "location": "",
                "cmd": (
                    "codex exec --dangerously-bypass-approvals-and-sandbox "
                    "--skip-git-repo-check - < {prompt_file}"
                ),
            }
        },
    )
    monkeypatch.setattr(
        hub,
        "_read_state",
        lambda _cfg: {"librarian_pane": "librarian-pane", "repo_root": str(tmp_path)},
    )
    monkeypatch.setattr(hub, "CONSULT_TIMEOUT_MS", 2)
    monkeypatch.setattr(hub, "ANSWER_POLL_INTERVAL_SECONDS", 0.0001)

    def fake_herdr(*args: str, check: bool = True) -> object:
        del check
        calls.append(args)
        if args[:2] == ("pane", "split"):
            return hub.subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"result": {"pane": {"pane_id": "codex-specialist-pane"}}}),
                "",
            )
        if args[:2] == ("pane", "run"):
            env = dict(hub.os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["FAKE_CODEX_PROMPT_CAPTURE"] = str(captured_prompt_path)
            env["FAKE_CODEX_ANSWER_PATH"] = str(answer_path)
            if answer_body is not None:
                env["FAKE_CODEX_ANSWER"] = json.dumps(answer_body)
            return hub.subprocess.run(
                args[3], shell=True, check=True, capture_output=True, text=True, env=env
            )
        if args[:2] == ("pane", "close"):
            return hub.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected Herdr call: {args}")

    monkeypatch.setattr(hub, "_herdr", fake_herdr)
    hub.cmd_consult(hub.argparse.Namespace(consult_path=str(consult_path)))
    return (
        json.loads(answer_path.read_text()),
        calls,
        captured_prompt_path.read_text(),
        prompt_file,
    )


def test_codex_consult_polls_valid_private_answer_and_closes_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    valid = {
        "consult_id": "consult-codex-1",
        "answer": "Use the strict contract.",
        "citations": ["module.py:10"],
    }

    answer, calls, captured_prompt, prompt_file = _run_codex_consult(
        tmp_path, monkeypatch, valid
    )

    assert answer == valid
    launch = next(call[3] for call in calls if call[:2] == ("pane", "run"))
    assert launch.endswith(f"- < {hub.shlex.quote(str(prompt_file))}")
    assert captured_prompt == prompt_file.read_text()
    assert captured_prompt.startswith("You are the 'python' specialist answering ONE consult.")
    assert str(prompt_file) not in captured_prompt
    assert not any(call[:2] == ("wait", "agent-status") for call in calls)
    assert [call for call in calls if call[:2] == ("pane", "close")] == [
        ("pane", "close", "codex-specialist-pane")
    ]
    assert capsys.readouterr().out == "CONSULT consult-codex-1 DONE\n"


def test_codex_consult_rejects_malformed_private_answer_and_closes_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = {"consult_id": "consult-codex-1", "answer": "missing citations"}

    answer, calls, _captured_prompt, _prompt_file = _run_codex_consult(
        tmp_path, monkeypatch, malformed
    )

    assert "citations" in answer["error"]
    assert [call for call in calls if call[:2] == ("pane", "close")] == [
        ("pane", "close", "codex-specialist-pane")
    ]


def test_codex_consult_timeout_writes_failure_answer_and_closes_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, calls, _captured_prompt, _prompt_file = _run_codex_consult(
        tmp_path, monkeypatch, None
    )

    assert "timed out waiting" in answer["error"]
    assert [call for call in calls if call[:2] == ("pane", "close")] == [
        ("pane", "close", "codex-specialist-pane")
    ]


def test_runtime_dir_command_uses_roster_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_dir = tmp_path / "configured-runtime"
    roster = tmp_path / "config/agents.yaml"
    _write_roster(roster, runtime_dir, tmp_path / "repo")
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))
    monkeypatch.setattr(sys, "argv", ["hub.py", "runtime-dir"])

    hub.main()

    assert capsys.readouterr().out == f"{runtime_dir}\n"
