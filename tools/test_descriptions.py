from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

TOOLS = Path(__file__).resolve().parent
HARNESS = TOOLS / "harness.py"
REPO = TOOLS.parent
SPECIALISTS = ["clickhouse", "kafka", "lucidchart", "drawio", "create-html"]
REPORT_CONTRACT = ".shared-llm/public/layers/agents/common/_report-contract.md"
STRICT_OUTPUT_AGENT_RECIPES = (
    "adversarial-evaluator",
    "code-review",
    "intake-clerk",
    "phase-evaluator",
    "plan-adversary",
    "plan-watchdog",
    "planner",
    "researcher",
    "team-pulse",
    "upagent-account-manager",
    "upagent-checker",
    "upagent-rescuer",
    "upagent-sentinel",
)
REWRITTEN_DESCRIPTION_SOURCES = {
    ".shared-llm/public/layers/agents/common/adversarial-evaluator.description.md",
    ".shared-llm/public/layers/agents/common/aws.description.md",
    ".shared-llm/public/layers/agents/common/backend.description.md",
    ".shared-llm/public/layers/agents/common/clickhouse.description.md",
    ".shared-llm/public/layers/agents/common/create-html.description.md",
    ".shared-llm/public/layers/agents/common/database.description.md",
    ".shared-llm/public/layers/agents/common/deployer.description.md",
    ".shared-llm/public/layers/agents/common/drawio.description.md",
    ".shared-llm/public/layers/agents/common/kafka.description.md",
    ".shared-llm/public/layers/agents/common/lucidchart.description.md",
    ".shared-llm/public/layers/agents/common/monorepo-pkgs.description.md",
    ".shared-llm/public/layers/agents/common/phase-evaluator.description.md",
    ".shared-llm/public/layers/agents/common/plan-adversary.description.md",
    ".shared-llm/public/layers/agents/common/plan-watchdog.description.md",
    ".shared-llm/public/layers/agents/common/playwright-cli.description.md",
    ".shared-llm/public/layers/agents/common/team-pulse.description.md",
    ".shared-llm/public/layers/skills/common/backend/description.md",
    ".shared-llm/public/layers/skills/common/clickhouse/description.md",
    ".shared-llm/public/layers/skills/common/create-html/description.md",
    ".shared-llm/public/layers/skills/common/drawio/description.md",
    ".shared-llm/public/layers/skills/common/kafka/description.md",
    ".shared-llm/public/layers/skills/common/lucidchart/description.md",
    ".shared-llm/public/layers/skills/common/update-shared-llm/description.md",
    ".shared-llm/public/layers/slash-commands/common/claude/cc-research/description.md",
    ".shared-llm/public/layers/slash-commands/common/claude/codex-delegate/description.md",
    ".shared-llm/public/layers/slash-commands/common/common/do-research/description.md",
    ".shared-llm/public/layers/slash-commands/common/common/grill-me/description.md",
    ".shared-llm/public/layers/slash-commands/common/common/phase-leader/description.md",
    ".shared-llm/public/layers/slash-commands/common/common/playwright-cli/description.md",
    ".shared-llm/public/layers/slash-commands/common/common/tui-control/description.md",
    ".shared-llm/public/layers/slash-commands/common/common/writing-for-agents/description.md",
    ".shared-llm/public/llm/pi/common/agents/tf-reviewer.md",
}


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "harness_descriptions_under_test", HARNESS
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _patch_home(module, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    module.HOME = home
    module.CONFIG_PATH = home / ".shared-llm.yaml"
    module.DEFAULT_SOURCE = home / ".shared-llm"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _minimal_kit(kit: Path, description: str = "short") -> None:
    _write(
        kit / ".shared-llm/public/layers/skills/common/demo/description.md", description
    )
    _write(kit / ".shared-llm/public/layers/skills/common/demo/practices.md", "body\n")
    _write(
        kit / ".shared-llm/public/compose/global/demo.yaml",
        "name: demo\n"
        "description: .shared-llm/public/layers/skills/common/demo/description.md\n"
        "inputs:\n"
        "  - .shared-llm/public/layers/skills/common/demo/practices.md\n"
        "output: global-staging/skills/demo/SKILL.md\n",
    )


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("a" * 300, 300),
        ("a" * 301, 301),
        ("a" * 1024, 1024),
        ("a" * 1025, 1025),
        ("😀" * 10, 20),
    ],
)
def test_utf16_description_boundaries(text: str, units: int) -> None:
    module = _load_harness()
    assert module.utf16_code_units(text) == units


def test_placeholder_expansion_is_measured_in_memory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_harness()
    _patch_home(module, tmp_path / "home")
    kit = tmp_path / "kit"
    _minimal_kit(kit, "{{DESC}}")
    module.__dict__["project_root"] = lambda: kit
    module.__dict__["KIT_SELF_PLACEHOLDERS"] = {"DESC": "x" * 301}

    items, errors = module.build_description_corpus({"destinations": [], "global": []})

    assert not errors
    assert [item.length for item in items] == [301]
    assert module.print_description_report(items, errors, enforce_destinations=False)
    assert "WARN public skill demo: 301 units" in capsys.readouterr().out


def test_destination_overages_are_staged_warn_only_until_enforced(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    _patch_home(module, tmp_path / "home")
    kit = tmp_path / "kit"
    _minimal_kit(kit)
    module.__dict__["project_root"] = lambda: kit
    dest = tmp_path / "dest"
    _write(
        dest / ".shared-llm/this_repo/skills/long/SKILL.md",
        "---\nname: long\ndescription: '" + "x" * 1025 + "'\n---\n\nbody\n",
    )
    cfg = {"destinations": [{"path": str(dest), "harnesses": ["cc"]}], "global": []}

    items, errors = module.build_description_corpus(cfg)

    assert not errors
    assert module.print_description_report(items, errors, enforce_destinations=False)
    assert not module.print_description_report(items, errors, enforce_destinations=True)


def test_destination_report_redacts_private_names_and_paths_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_harness()
    _patch_home(module, tmp_path / "home")
    kit = tmp_path / "kit"
    _minimal_kit(kit)
    module.__dict__["project_root"] = lambda: kit
    destination_repo = tmp_path / "llmtest-repo-alpha"
    destination_skill = "llmtest-skill-alpha"
    _write(
        destination_repo / f".shared-llm/this_repo/skills/{destination_skill}/SKILL.md",
        "---\nname: "
        + destination_skill
        + "\ndescription: '"
        + "x" * 301
        + "'\n---\n\nbody\n",
    )
    cfg = {
        "destinations": [{"path": str(destination_repo), "harnesses": ["cc"]}],
        "global": [],
    }

    items, errors = module.build_description_corpus(cfg)
    assert module.print_description_report(items, errors, enforce_destinations=False)
    out = capsys.readouterr().out

    assert "WARN destination#1 standalone-skill: 301 units" in out
    assert destination_skill not in out
    assert str(destination_repo) not in out
    assert "llmtest-repo-alpha" not in out


def test_destination_parse_errors_redact_private_names_and_paths_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_harness()
    _patch_home(module, tmp_path / "home")
    kit = tmp_path / "kit"
    _minimal_kit(kit)
    module.__dict__["project_root"] = lambda: kit
    destination_repo = tmp_path / "llmtest-repo-alpha"
    destination_skill = "llmtest-skill-alpha"
    _write(
        destination_repo / f".shared-llm/this_repo/skills/{destination_skill}/SKILL.md",
        "no frontmatter\n",
    )

    _, errors = module.build_description_corpus(
        {"destinations": [{"path": str(destination_repo)}]}
    )
    assert not module.print_description_report([], errors, enforce_destinations=False)
    out = capsys.readouterr().out

    assert "FAIL destination#1 standalone-skill: status=parse/source-error" in out
    assert destination_skill not in out
    assert str(destination_repo) not in out
    assert "llmtest-repo-alpha" not in out


def test_destination_recipe_errors_redact_private_names_paths_and_loader_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_harness()
    _patch_home(module, tmp_path / "home")
    kit = tmp_path / "kit"
    _minimal_kit(kit)
    module.__dict__["project_root"] = lambda: kit
    destination_repo = tmp_path / "llmtest-repo-alpha"
    recipe_name = "llmtest-recipe-alpha"
    _write(
        destination_repo / f".shared-llm/this_repo/compose/skills/{recipe_name}.yaml",
        "not: [valid\n",
    )

    items, errors = module.build_description_corpus(
        {"destinations": [{"path": str(destination_repo)}]}
    )
    assert not items or all(item.owner != "destination" for item in items)
    assert not module.print_description_report(
        items, errors, enforce_destinations=False
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "FAIL destination#1 recipe: status=parse/source-error" in combined
    assert recipe_name not in combined
    assert str(destination_repo) not in combined
    assert "llmtest-repo-alpha" not in combined


def test_malformed_standalone_frontmatter_is_reported(tmp_path: Path) -> None:
    module = _load_harness()
    _patch_home(module, tmp_path / "home")
    kit = tmp_path / "kit"
    _minimal_kit(kit)
    module.__dict__["project_root"] = lambda: kit
    dest = tmp_path / "dest"
    _write(dest / ".shared-llm/this_repo/skills/bad/SKILL.md", "no frontmatter\n")

    _, errors = module.build_description_corpus({"destinations": [{"path": str(dest)}]})

    assert len(errors) == 1
    assert errors[0].name == "bad"
    assert "frontmatter" in errors[0].message


def test_update_preflight_blocks_before_copy_compose_link_or_home_writes(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    home = tmp_path / "home"
    _patch_home(module, home)
    kit = tmp_path / "kit"
    _minimal_kit(kit, "x" * 1025)
    module.__dict__["project_root"] = lambda: kit
    dest = tmp_path / "dest"
    dest.mkdir()
    module.CONFIG_PATH.write_text(
        yaml.safe_dump(
            {
                "source": str(home / "hub"),
                "global": [],
                "destinations": [{"path": str(dest), "harnesses": ["cc"]}],
            }
        )
    )

    with pytest.raises(SystemExit):
        module.cmd_update(argparse.Namespace(verbose=False))

    assert not (home / "hub").exists()
    assert not (dest / ".shared-llm/public").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".pi").exists()
    assert not (home / ".agents").exists()
    assert not (home / ".shared-llm/manifest.json").exists()


def test_first_run_destination_public_description_refs_use_kit_source(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    home = tmp_path / "home"
    _patch_home(module, home)
    kit = tmp_path / "kit"
    _minimal_kit(kit)
    module.__dict__["project_root"] = lambda: kit
    dest = tmp_path / "dest"
    _write(
        dest / ".shared-llm/this_repo/compose/skills/demo.yaml",
        "name: demo\n"
        "description: .shared-llm/public/layers/skills/common/demo/description.md\n"
        "inputs:\n"
        "  - .shared-llm/public/layers/skills/common/demo/practices.md\n"
        "output: .claude/skills/demo/SKILL.md\n",
    )
    module.CONFIG_PATH.write_text(
        yaml.safe_dump(
            {
                "source": str(home / "hub"),
                "global": [],
                "destinations": [{"path": str(dest), "harnesses": ["cc"]}],
            }
        )
    )

    assert not (dest / ".shared-llm/public").exists()
    items, errors = module.build_description_corpus(
        yaml.safe_load(module.CONFIG_PATH.read_text())
    )
    assert not errors
    demo_item = next(item for item in items if item.owner == "destination")
    assert demo_item.source_owner == "public"
    assert (
        demo_item.source
        == kit / ".shared-llm/public/layers/skills/common/demo/description.md"
    )

    module.cmd_update(argparse.Namespace(verbose=False))

    assert (dest / ".shared-llm/public").is_dir()
    assert (dest / ".claude/skills/demo/SKILL.md").is_file()


def test_direct_global_preflight_blocks_before_home_writes(tmp_path: Path) -> None:
    module = _load_harness()
    home = tmp_path / "home"
    _patch_home(module, home)
    kit = tmp_path / "kit"
    _minimal_kit(kit, "x" * 1025)
    module.__dict__["project_root"] = lambda: kit
    module.CONFIG_PATH.write_text(
        yaml.safe_dump(
            {"source": str(home / "hub"), "global": ["cc"], "destinations": []}
        )
    )

    with pytest.raises(SystemExit):
        module.cmd_global(argparse.Namespace())

    assert not (home / ".claude").exists()
    assert not (home / ".shared-llm/generated").exists()
    assert not (home / ".shared-llm/manifest.json").exists()


def test_foreign_home_files_are_not_description_inputs(tmp_path: Path) -> None:
    module = _load_harness()
    home = tmp_path / "home"
    _patch_home(module, home)
    kit = tmp_path / "kit"
    _minimal_kit(kit)
    module.__dict__["project_root"] = lambda: kit
    _write(
        home / ".claude/skills/foreign/SKILL.md", "not frontmatter and very foreign\n"
    )

    items, errors = module.build_description_corpus(
        {"destinations": [], "global": ["cc"]}
    )

    assert not errors
    assert [item.name for item in items] == ["demo"]


def test_untracked_generated_agent_is_audited_while_ignored_files_are_excluded(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    kit = tmp_path / "kit"
    _write(kit / ".gitignore", ".claude/agents/ignored.md\n")
    _write(
        kit / ".claude/agents/untracked.md",
        "---\nname: untracked\ndescription: '" + "x" * 1025 + "'\n---\n\nbody\n",
    )
    _write(kit / ".claude/agents/ignored.md", "not frontmatter\n")
    subprocess.run(["git", "init", "-q", str(kit)], check=True)
    module.__dict__["project_root"] = lambda: kit

    items, errors = module._tracked_generated_description_items()

    assert not errors
    assert [item.name for item in items] == ["untracked"]
    assert items[0].length == 1025
    assert not module.print_description_report(
        items, errors, enforce_destinations=False
    )


def test_tracked_generated_git_failure_reports_error_without_glob_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_harness()
    kit = tmp_path / "llmtest-private-kit-name"
    _write(kit / ".claude/agents/untracked-private-name.md", "not frontmatter\n")
    module.__dict__["project_root"] = lambda: kit

    def fail_git(args, **kwargs):
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr(module.subprocess, "run", fail_git)

    items, errors = module._tracked_generated_description_items()

    assert items == []
    assert len(errors) == 1
    assert errors[0].owner == "public"
    assert errors[0].kind == "tracked-generated"
    assert errors[0].name == "git-discovery"
    assert errors[0].source == Path(".claude")
    assert "--exclude-standard" in errors[0].message
    assert "untracked-private-name" not in errors[0].message
    assert not module.print_description_report(
        items, errors, enforce_destinations=False
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "FAIL public tracked-generated git-discovery" in combined
    assert "untracked-private-name" not in combined
    assert "llmtest-private-kit-name" not in combined


def test_global_reconciliation_installs_new_skills_and_skips_codex_agents(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    home = tmp_path / "home"
    _patch_home(module, home)
    cfg = {"global": ["cc", "codex", "pi"], "destinations": [], "exclude": []}

    module.do_global_flow(cfg, module.RunLog(verbose=False))

    for name in SPECIALISTS:
        generated_skill = home / ".shared-llm/generated/skills" / name
        assert generated_skill.is_dir()
        assert (home / ".claude/skills" / name).is_symlink()
        assert (home / ".pi/agent/skills" / name).is_symlink()
        assert (home / ".agents/skills" / name).is_symlink()
        assert (home / ".claude/agents" / f"{name}.md").is_symlink()
        assert (home / ".pi/agent/agents" / f"{name}.md").is_symlink()
    assert not (home / ".agents/agents").exists()


def test_generated_global_skills_include_output_path_rules(tmp_path: Path) -> None:
    module = _load_harness()
    home = tmp_path / "home"
    _patch_home(module, home)
    cfg = {"global": ["cc"], "destinations": [], "exclude": []}

    module.do_global_flow(cfg, module.RunLog(verbose=False))

    for name, suffix in (("drawio", ".drawio"), ("create-html", ".html")):
        text = (home / f".shared-llm/generated/skills/{name}/SKILL.md").read_text()
        assert "Save to the requested output path" in text
        assert f"clear `{suffix}` filename in the current directory" in text


def test_generated_drawio_agent_has_reachable_validator_guidance(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    home = tmp_path / "home"
    _patch_home(module, home)
    cfg = {"global": ["cc", "codex", "pi"], "destinations": [], "exclude": []}

    module.do_global_flow(cfg, module.RunLog(verbose=False))

    generated_validator = (
        home / ".shared-llm/generated/skills/drawio/validate-drawio.py"
    )
    assert generated_validator.is_file()
    assert not (
        home / ".shared-llm/generated/skills/drawio/resources/validate-drawio.py"
    ).exists()
    skill_text = (home / ".shared-llm/generated/skills/drawio/SKILL.md").read_text()
    assert "~/.shared-llm/generated/skills/drawio/validate-drawio.py" in skill_text
    assert "~/.claude/skills/drawio/validate-drawio.py" in skill_text
    assert ".claude/skills/drawio/resources" not in skill_text
    for validator in (
        home / ".claude/skills/drawio/validate-drawio.py",
        home / ".pi/agent/skills/drawio/validate-drawio.py",
        home / ".agents/skills/drawio/validate-drawio.py",
    ):
        assert validator.is_file()

    text = (REPO / ".claude/agents/drawio.md").read_text()
    assert "This skill ships" not in text
    assert "~/.shared-llm/generated/skills/drawio/validate-drawio.py" in text
    assert "~/.claude/skills/drawio/validate-drawio.py" in text
    assert ".claude/skills/drawio/resources" not in text
    assert "python3 <validator> <file.drawio>" in text


def test_resource_composition_excludes_ignored_cache_artifacts(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    kit = tmp_path / "kit"
    out = tmp_path / "out"
    _write(kit / ".gitignore", "__pycache__/\n*.pyc\n.ruff_cache/\n")
    _write(
        kit / ".shared-llm/public/layers/skills/common/demo/description.md",
        "short",
    )
    _write(kit / ".shared-llm/public/layers/skills/common/demo/practices.md", "body")
    resources = kit / ".shared-llm/public/layers/skills/common/demo/resources"
    _write(resources / "keep.py", "print('ok')\n")
    _write(resources / "__pycache__/drop.pyc", "bytecode")
    _write(resources / ".ruff_cache/drop", "cache")
    _write(
        kit / ".shared-llm/public/compose/global/demo.yaml",
        "name: demo\n"
        "description: .shared-llm/public/layers/skills/common/demo/description.md\n"
        "inputs:\n"
        "  - .shared-llm/public/layers/skills/common/demo/practices.md\n"
        "resources: .shared-llm/public/layers/skills/common/demo/resources\n"
        "output: global-staging/skills/demo/SKILL.md\n",
    )
    subprocess.run(["git", "init", "-q", str(kit)], check=True)

    composer = module.Composer(
        kit,
        output_base=out,
        shared_root=kit / ".shared-llm/public",
    )
    composer.compose_one(kit / ".shared-llm/public/compose/global/demo.yaml")

    assert (out / "global-staging/skills/demo/keep.py").is_file()
    assert not (out / "global-staging/skills/demo/__pycache__").exists()
    assert not (out / "global-staging/skills/demo/.ruff_cache").exists()


def test_archive_resource_composition_warns_and_still_skips_cache_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_harness()
    kit = tmp_path / "kit"
    out = tmp_path / "out"
    _write(
        kit / ".shared-llm/public/layers/skills/common/demo/description.md",
        "short",
    )
    _write(kit / ".shared-llm/public/layers/skills/common/demo/practices.md", "body")
    resources = kit / ".shared-llm/public/layers/skills/common/demo/resources"
    _write(resources / "keep.py", "print('ok')\n")
    _write(resources / "__pycache__/drop.pyc", "bytecode")
    _write(
        kit / ".shared-llm/public/compose/global/demo.yaml",
        "name: demo\n"
        "description: .shared-llm/public/layers/skills/common/demo/description.md\n"
        "inputs:\n"
        "  - .shared-llm/public/layers/skills/common/demo/practices.md\n"
        "resources: .shared-llm/public/layers/skills/common/demo/resources\n"
        "output: global-staging/skills/demo/SKILL.md\n",
    )

    composer = module.Composer(
        kit,
        output_base=out,
        shared_root=kit / ".shared-llm/public",
    )
    composer.compose_one(kit / ".shared-llm/public/compose/global/demo.yaml")

    captured = capsys.readouterr()
    assert (
        "warning: git discovery unavailable for public resource exclusions"
        in captured.err
    )
    assert (out / "global-staging/skills/demo/keep.py").is_file()
    assert not (out / "global-staging/skills/demo/__pycache__").exists()


def test_resource_composition_blocks_when_git_ignored_discovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_harness()
    kit = tmp_path / "kit"
    out = tmp_path / "out"
    _write(
        kit / ".shared-llm/public/layers/skills/common/demo/description.md",
        "short",
    )
    _write(kit / ".shared-llm/public/layers/skills/common/demo/practices.md", "body")
    resources = kit / ".shared-llm/public/layers/skills/common/demo/resources"
    _write(resources / "cache.bin", "must not propagate")
    _write(
        kit / ".shared-llm/public/compose/global/demo.yaml",
        "name: demo\n"
        "description: .shared-llm/public/layers/skills/common/demo/description.md\n"
        "inputs:\n"
        "  - .shared-llm/public/layers/skills/common/demo/practices.md\n"
        "resources: .shared-llm/public/layers/skills/common/demo/resources\n"
        "output: global-staging/skills/demo/SKILL.md\n",
    )

    def fake_run(args, **kwargs):
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, stdout="true\n")
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    composer = module.Composer(
        kit,
        output_base=out,
        shared_root=kit / ".shared-llm/public",
    )

    with pytest.raises(SystemExit) as excinfo:
        composer.compose_one(kit / ".shared-llm/public/compose/global/demo.yaml")

    assert "git ignored-path discovery failed" in str(excinfo.value)
    assert not (out / "global-staging/skills/demo/cache.bin").exists()


def _drawio_module():
    path = (
        REPO
        / ".shared-llm/public/layers/skills/common/drawio/resources/validate-drawio.py"
    )
    spec = importlib.util.spec_from_file_location("validate_drawio_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALID_DRAWIO = """<mxfile><diagram name=\"Page-1\"><mxGraphModel><root>
<mxCell id=\"0\"/><mxCell id=\"1\" parent=\"0\"/>
<mxCell id=\"a\" value=\"A\" vertex=\"1\" parent=\"1\"><mxGeometry as=\"geometry\"/></mxCell>
<mxCell id=\"b\" value=\"B\" vertex=\"1\" parent=\"1\"><mxGeometry as=\"geometry\"/></mxCell>
<mxCell id=\"e\" edge=\"1\" parent=\"1\" source=\"a\" target=\"b\"><mxGeometry relative=\"1\" as=\"geometry\"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""


@pytest.mark.parametrize(
    ("mutated", "expected"),
    [
        (VALID_DRAWIO, []),
        (
            VALID_DRAWIO.replace('<mxCell id="b"', '<mxCell id="a"'),
            ["duplicate id a", "missing target b"],
        ),
        (
            VALID_DRAWIO.replace(
                'parent="1"><mxGeometry', 'parent="missing"><mxGeometry', 1
            ),
            ["missing parent missing"],
        ),
        (
            VALID_DRAWIO.replace('target="b"', 'target="missing"'),
            ["missing target missing"],
        ),
        (
            VALID_DRAWIO.replace('<mxCell id="0"/>', ""),
            ["missing structural cell id=0"],
        ),
        (
            VALID_DRAWIO.replace(
                '<mxCell id="a" value="A" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>',
                '<mxCell id="a" value="A" vertex="1" parent="1"/>',
            ),
            ["vertex a missing geometry"],
        ),
        (
            VALID_DRAWIO.replace(
                '<mxCell id="e" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"/></mxCell>',
                '<mxCell id="e" edge="1" parent="1" source="a" target="b"/>',
            ),
            ["edge e missing geometry"],
        ),
    ],
)
def test_drawio_semantic_validator(
    tmp_path: Path, mutated: str, expected: list[str]
) -> None:
    module = _drawio_module()
    path = tmp_path / "sample.drawio"
    path.write_text(mutated)

    errors = module.validate_drawio(path)

    for message in expected:
        assert any(message in error for error in errors)
    if not expected:
        assert errors == []


def test_new_specialist_agent_recipes_share_practices_and_report_contract_once() -> (
    None
):
    for name in SPECIALISTS:
        recipe = yaml.safe_load(
            (REPO / f".shared-llm/public/compose/agents/{name}.yaml").read_text()
        )
        inputs = recipe["inputs"]
        assert inputs[0] == f".shared-llm/public/layers/agents/common/{name}.md"
        assert (
            inputs.count(f".shared-llm/public/layers/skills/common/{name}/practices.md")
            == 1
        )
        assert inputs.count(REPORT_CONTRACT) == 1


def test_strict_output_agent_recipes_exclude_generic_report_contract() -> None:
    for name in STRICT_OUTPUT_AGENT_RECIPES:
        recipe = yaml.safe_load(
            (REPO / f".shared-llm/public/compose/agents/{name}.yaml").read_text()
        )
        assert REPORT_CONTRACT not in recipe["inputs"]


def test_new_global_skills_are_registered_for_home_install() -> None:
    module = _load_harness()
    for name in ["herdr", *SPECIALISTS]:
        assert name in module.GLOBAL_CONVENTION_SKILLS
        recipe = REPO / ".shared-llm/public" / module.GLOBAL_CONVENTION_SKILLS[name]
        assert recipe.is_file()


def test_global_skill_documentation_lists_match_registered_conventions() -> None:
    module = _load_harness()
    expected_readme = "`, `".join(module.GLOBAL_CONVENTION_SKILLS)
    assert f"`{expected_readme}`" in (REPO / "README.md").read_text()
    expected_onboarding = "`/`".join(module.GLOBAL_CONVENTION_SKILLS)
    assert f"`{expected_onboarding}`" in (REPO / "ONBOARDING.md").read_text()


def test_upagent_roster_contains_new_specialists_with_required_offering_and_locations() -> (
    None
):
    roster = yaml.safe_load(
        (
            REPO / ".shared-llm/public/extensions/common/upagent/specialists.yaml"
        ).read_text()
    )["specialists"]
    by_name = {entry["name"]: entry for entry in roster}
    for name in SPECIALISTS:
        entry = by_name[name]
        assert entry["location"] == f".claude/agents/{name}.md"
        assert entry["offering"] == "claude-sonnet-5"
        assert entry["effort"] == "medium"
        assert len(entry["description"]) < 160
        assert "..." not in entry["description"]


def test_description_matching_checklist_shape_and_routing_words() -> None:
    data = yaml.safe_load((REPO / "tools/description-matching.yaml").read_text())
    items = data["items"]
    by_source = {item["source"]: item for item in items}

    assert set(by_source) == REWRITTEN_DESCRIPTION_SOURCES
    assert len(by_source) == len(items)
    for source in REWRITTEN_DESCRIPTION_SOURCES:
        item = by_source[source]
        assert len(item["should_match"]) == 2
        assert all(
            isinstance(prompt, str) and prompt for prompt in item["should_match"]
        )
        assert isinstance(item["should_not_match"], str) and item["should_not_match"]
        assert item["routing_words"]
        text = (REPO / source).read_text().lower()
        for word in item["routing_words"]:
            assert str(word).lower() in text

    boundary_cases = data["boundary_cases"]
    assert "MSK" in boundary_cases["kafka_msk_mixed"]
    assert "Kafka" in boundary_cases["kafka_msk_mixed"]
    assert "generic" in boundary_cases["kafka_generic_queue"]
    assert "queue" in boundary_cases["kafka_generic_queue"]
    assert "event" in boundary_cases["kafka_unrelated_event_system"]


def test_install_prompt_contains_required_guardrails() -> None:
    text = (REPO / "INSTALL-PROMPT.md").read_text()
    required = [
        "just init -o <mac|ubuntu>",
        "just configure -g <harnesses>",
        "just configure -d <repo> -l <harnesses>",
        "just descriptions",
        "just update",
        "Omit `-s` to accept the default source hub",
        "just configure -s <deliberate-hub-path>",
        "such as `~/.shared-llm`",
        "Do not pass the kit checkout path to `-s`",
        "`~/.shared-llm.yaml` is the per-machine",
        "repository-root `.shared-llm.yaml`",
        "Ask before installing OS packages",
        "Never edit generated outputs",
        "passes twice",
    ]
    for needle in required:
        assert needle in text
    assert "just configure -s <kit" not in text
    assert "just configure -s /path/to/llm-config-setup" not in text
