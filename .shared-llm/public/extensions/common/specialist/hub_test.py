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
        + "    harness: claude\n"
        + "    model: haiku\n"
        + "    agent: docs\n"
        + "    effort: low\n"
    )


def test_consult_orders_pin_a_dedicated_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every consult gets a broker regardless of the roster's lifecycle default."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    _write_roster(roster, tmp_path / "runtime", repo_root)
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))
    cfg = hub.load_config()
    hub.paths(cfg)["consults"].mkdir(parents=True, exist_ok=True)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("question brief\n")
    consult = {
        "consult_id": "c-123",
        "specialist": "docs",
        "question": "where does composition happen?",
        "answer_path": str(tmp_path / "answer.json"),
    }
    entry = {
        "name": "docs",
        "location": ".claude/agents/docs.md",
        "harness": "claude",
        "model": "haiku",
        "agent": "docs",
        "effort": "low",
    }

    order_path, _result_path = hub._specialist_order(
        cfg, consult, entry, prompt_file, "w1R:p2", str(repo_root)
    )

    order = json.loads(order_path.read_text())
    assert order["management"] == {"mode": "dedicated"}


def test_librarian_sidebar_label_names_the_consult_door(tmp_path: Path) -> None:
    """The pane label must not present the Librarian as a live idle agent."""
    message = hub._librarian_status_message(4)
    assert "broker mailbox" in message
    assert "just specialist-hub consult" in message
    assert "never paste" in message
    assert "4 specialists indexed" in message


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


def test_legacy_cmd_roster_is_normalized_from_compose_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    recipe = repo_root / ".shared-llm/this_repo/compose/agents/adversarial-evaluator.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("type: agent\nname: adversarial-evaluator\nmodel: sonnet\n")
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    roster.parent.mkdir(parents=True)
    roster.write_text(
        "runtime_dir: " + str(tmp_path / "runtime") + "\n"
        "agents:\n"
        "  - name: adversarial-evaluator\n"
        "    location: .claude/agents/adversarial-evaluator.md\n"
        "    cmd: \"claude -p {prompt} --dangerously-skip-permissions --agent adversarial-evaluator\"\n"
    )
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))

    cfg = hub.load_config()

    assert cfg["agents"][0]["harness"] == "claude"
    assert cfg["agents"][0]["agent"] == "adversarial-evaluator"
    assert cfg["agents"][0]["model"] == "sonnet"


def test_legacy_cmd_roster_uses_empty_model_when_compose_recipe_omits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    roster.parent.mkdir(parents=True)
    roster.write_text(
        "runtime_dir: " + str(tmp_path / "runtime") + "\n"
        "agents:\n"
        "  - name: missing-recipe\n"
        "    location: .claude/agents/missing-recipe.md\n"
        "    cmd: \"claude -p {prompt} --dangerously-skip-permissions --agent missing-recipe\"\n"
    )
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))

    cfg = hub.load_config()

    assert cfg["agents"][0]["harness"] == "claude"
    assert cfg["agents"][0]["agent"] == "missing-recipe"
    assert cfg["agents"][0]["model"] == ""


def _run_managed_consult(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer_body: dict | None,
) -> tuple[dict, dict, str, Path]:
    consult_id = "consult-managed-1"
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
    cfg = {"runtime_dir": runtime_dir, "repo_root": tmp_path}
    orders: list[dict] = []

    monkeypatch.setattr(hub, "_require_herdr", lambda: None)
    monkeypatch.setattr(hub, "load_config", lambda: cfg)
    monkeypatch.setattr(
        hub,
        "read_index",
        lambda _cfg: {
            "python": {
                "location": "",
                "harness": "codex",
                "model": "configured-codex-model",
                "agent": "python",
                "effort": "medium",
            }
        },
    )
    monkeypatch.setattr(
        hub,
        "_read_state",
        lambda _cfg: {"librarian_pane": "librarian-pane", "repo_root": str(tmp_path)},
    )
    def fake_dispatch(order_path: Path, cwd: str) -> None:
        assert cwd == str(tmp_path)
        orders.append(json.loads(order_path.read_text()))
        if answer_body is None:
            raise hub.subprocess.TimeoutExpired(["recruiter", "dispatch"], 1)
        answer_path.parent.mkdir(parents=True, exist_ok=True)
        answer_path.write_text(json.dumps(answer_body))

    monkeypatch.setattr(hub, "_dispatch_specialist", fake_dispatch)
    hub.cmd_consult(hub.argparse.Namespace(consult_path=str(consult_path)))
    return (
        json.loads(answer_path.read_text()),
        orders[0],
        prompt_file.read_text(),
        prompt_file,
    )


def test_consult_routes_specialist_through_an_upagent_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    valid = {
        "consult_id": "consult-managed-1",
        "answer": "Use the strict contract.",
        "citations": ["module.py:10"],
    }

    answer, order, prompt, prompt_file = _run_managed_consult(
        tmp_path, monkeypatch, valid
    )

    assert answer == valid
    assert order["harness"] == "codex"
    assert order["agent"] == "python"
    assert order["requester"]["id"] == "specialist-librarian"
    assert order["instructions_path"] == str(prompt_file)
    assert prompt.startswith("You are the 'python' specialist answering ONE consult.")
    assert capsys.readouterr().out == "CONSULT consult-managed-1 DONE\n"


def test_managed_consult_rejects_malformed_private_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = {"consult_id": "consult-managed-1", "answer": "missing citations"}

    answer, _order, _prompt, _prompt_file = _run_managed_consult(
        tmp_path, monkeypatch, malformed
    )

    assert "citations" in answer["error"]


def test_managed_consult_timeout_writes_failure_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, _order, _prompt, _prompt_file = _run_managed_consult(
        tmp_path, monkeypatch, None
    )

    assert "timed out" in answer["error"]


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


def _write_base_roster(base_dir: Path) -> Path:
    """A synthetic kit base roster in a fake engine dir (with a .git marker above it)."""
    (base_dir / ".git").mkdir(parents=True, exist_ok=True)
    engine_dir = base_dir / "engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    (engine_dir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: docs\n"
        "    description: base docs specialist\n"
        "    harness: claude\n"
        "    model: sonnet\n"
        "    agent: docs\n"
        "  - name: reviewer\n"
        "    description: base reviewer\n"
        "    harness: claude\n"
        "    model: opus\n"
        "    agent: reviewer\n"
    )
    return engine_dir


def _write_overlay_roster(repo_root: Path, runtime_dir: Path) -> Path:
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    overlay = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "runtime_dir: " + str(runtime_dir) + "\n"
        "agents:\n"
        "  - name: reviewer\n"
        "    description: repo reviewer with private routing\n"
        "    harness: claude\n"
        "    model: haiku\n"
        "    agent: reviewer\n"
        "  - name: payments\n"
        "    description: repo-only payments specialist\n"
        "    harness: claude\n"
        "    model: sonnet\n"
        "    agent: payments\n"
    )
    return overlay


def test_overlay_merges_on_top_of_kit_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Base ∪ overlay: same-named overlay entries clobber base, both rosters stay available."""
    engine_dir = _write_base_roster(tmp_path / "kit")
    repo_root = tmp_path / "repo"
    _write_overlay_roster(repo_root, tmp_path / "runtime")
    monkeypatch.delenv("SPECIALIST_HUB_CONFIG", raising=False)
    monkeypatch.setattr(hub, "HERE", engine_dir)
    monkeypatch.chdir(repo_root)

    cfg = hub.load_config()

    by_name = {a["name"]: a for a in cfg["agents"]}
    assert set(by_name) == {"docs", "reviewer", "payments"}
    assert by_name["docs"]["origin"] == "kit-base"
    assert by_name["reviewer"]["origin"] == "this-repo"
    assert by_name["reviewer"]["model"] == "haiku"  # overlay clobbers the base entry
    assert by_name["payments"]["origin"] == "this-repo"
    assert cfg["overridden"] == ["reviewer"]
    assert cfg["repo_root"] == repo_root


def test_env_override_skips_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$SPECIALIST_HUB_CONFIG stays a single-file override: the kit base is not merged in."""
    engine_dir = _write_base_roster(tmp_path / "kit")
    repo_root = tmp_path / "repo"
    overlay = _write_overlay_roster(repo_root, tmp_path / "runtime")
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(overlay))
    monkeypatch.setattr(hub, "HERE", engine_dir)
    monkeypatch.chdir(repo_root)

    cfg = hub.load_config()

    assert {a["name"] for a in cfg["agents"]} == {"reviewer", "payments"}
    assert all(a["origin"] == "override" for a in cfg["agents"])
    assert cfg["overridden"] == []


def test_kit_base_alone_when_repo_has_no_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_dir = _write_base_roster(tmp_path / "kit")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.delenv("SPECIALIST_HUB_CONFIG", raising=False)
    monkeypatch.setattr(hub, "HERE", engine_dir)
    monkeypatch.chdir(outside)

    cfg = hub.load_config()

    assert {a["name"] for a in cfg["agents"]} == {"docs", "reviewer"}
    assert all(a["origin"] == "kit-base" for a in cfg["agents"])
    assert cfg["repo_root"] == tmp_path / "kit"


def test_roster_prints_a_paste_ready_brief_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine_dir = _write_base_roster(tmp_path / "kit")
    repo_root = tmp_path / "repo"
    _write_overlay_roster(repo_root, tmp_path / "runtime")
    monkeypatch.delenv("SPECIALIST_HUB_CONFIG", raising=False)
    monkeypatch.setattr(hub, "HERE", engine_dir)
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(sys, "argv", ["hub.py", "roster"])

    hub.main()

    out = capsys.readouterr().out
    assert "MANDATORY" in out
    assert "- **docs** — base docs specialist" in out
    assert "- **payments** (this repo) — repo-only payments specialist" in out
    assert "just specialist-hub consult" in out
    assert "CONSULT <id> DONE" in out
    assert "`consults` in your result.json" in out


def test_roster_json_prints_the_merged_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine_dir = _write_base_roster(tmp_path / "kit")
    repo_root = tmp_path / "repo"
    _write_overlay_roster(repo_root, tmp_path / "runtime")
    monkeypatch.delenv("SPECIALIST_HUB_CONFIG", raising=False)
    monkeypatch.setattr(hub, "HERE", engine_dir)
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(sys, "argv", ["hub.py", "roster", "--json"])

    hub.main()

    index = json.loads(capsys.readouterr().out)
    assert index["reviewer"]["origin"] == "this-repo"
    assert index["docs"]["origin"] == "kit-base"


def test_roster_caps_each_specialist_to_one_brief_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The phone book rides inside every stage brief, so essay-length roster descriptions are
    cut to their first line and hard-capped."""
    engine_dir = _write_base_roster(tmp_path / "kit")
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        "runtime_dir: " + str(tmp_path / "runtime") + "\n"
        "agents:\n"
        "  - name: essayist\n"
        "    description: |\n"
        "      " + "long first line " * 30 + "\n"
        "      second line that must never appear\n"
        "    harness: claude\n"
        "    model: sonnet\n"
        "    agent: essayist\n"
    )
    monkeypatch.delenv("SPECIALIST_HUB_CONFIG", raising=False)
    monkeypatch.setattr(hub, "HERE", engine_dir)
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(sys, "argv", ["hub.py", "roster"])

    hub.main()

    out = capsys.readouterr().out
    essayist_line = next(line for line in out.splitlines() if "essayist" in line)
    assert "second line" not in out
    assert len(essayist_line) < 260
    assert essayist_line.endswith("...")


def test_specialist_orders_carry_the_recruiter_consult_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Librarian stamps the Recruiter-issued token so brokered consults are verifiable."""
    recruiter_state = tmp_path / "recruiter.json"
    recruiter_state.write_text(json.dumps({"consult_token": "issued-token"}))
    monkeypatch.setenv("UPAGENT_STATE", str(recruiter_state))
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    _write_roster(roster, tmp_path / "runtime", repo_root)
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))
    cfg = hub.load_config()
    hub.paths(cfg)["consults"].mkdir(parents=True, exist_ok=True)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("brief\n")
    consult = {
        "consult_id": "c-tok-1",
        "specialist": "docs",
        "question": "q",
        "answer_path": str(tmp_path / "answer.json"),
    }
    entry = {"name": "docs", "harness": "claude", "model": "haiku", "agent": "docs"}

    order_path, _ = hub._specialist_order(cfg, consult, entry, prompt_file, "w1:p1", str(repo_root))

    assert json.loads(order_path.read_text())["consult_token"] == "issued-token"


def test_specialist_orders_omit_the_token_when_recruiter_never_issued_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_STATE", str(tmp_path / "absent-recruiter.json"))
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    roster = repo_root / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
    _write_roster(roster, tmp_path / "runtime", repo_root)
    monkeypatch.setenv("SPECIALIST_HUB_CONFIG", str(roster))
    cfg = hub.load_config()
    hub.paths(cfg)["consults"].mkdir(parents=True, exist_ok=True)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("brief\n")
    consult = {
        "consult_id": "c-tok-2",
        "specialist": "docs",
        "question": "q",
        "answer_path": str(tmp_path / "answer.json"),
    }
    entry = {"name": "docs", "harness": "claude", "model": "haiku", "agent": "docs"}

    order_path, _ = hub._specialist_order(cfg, consult, entry, prompt_file, "w1:p1", str(repo_root))

    assert "consult_token" not in json.loads(order_path.read_text())


def test_consult_flips_the_librarian_sidebar_working_then_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Librarian pane must show the truth: working while a consult routes, idle with a
    served tally after — so a bypassed Librarian is distinguishable from a busy one."""
    reports: list[tuple[str, ...]] = []
    monkeypatch.setattr(hub, "_herdr", lambda *a, **k: reports.append(a))

    _run_managed_consult(tmp_path, monkeypatch, {
        "consult_id": "consult-managed-1",
        "answer": "the contract",
        "citations": ["src/x.py:1"],
    })

    states = [(a[a.index("--state") + 1], a[a.index("--message") + 1]) for a in reports if "--state" in a]
    assert states[0][0] == "working"
    assert "consult-managed-1" in states[0][1]
    assert states[-1][0] == "idle"
    assert "consult(s) served" in states[-1][1]
