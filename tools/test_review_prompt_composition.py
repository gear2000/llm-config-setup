"""Check real review/skill recipes, evidence rules, and shipped reference files.

These are packaging and instruction-contract tests, not claims about model behavior.
All compose outputs stay under pytest's temporary directory.
"""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / ".shared-llm/public"
EVIDENCE = PUBLIC / "layers/agents/common/_review-evidence.md"
LICENSE = PUBLIC / "layers/agents/common/_owainlewis-license.md"
AGENTS = (
    "architecture",
    "security",
    "code-review",
    "plan-adversary",
    "playwright-cli",
    "qa",
    "github",
    "reviewer",
)
SKILLS = (
    "common/claude/cc-plan",
    "common/common/do-plan",
    "common/common/prd-to-plan",
    "common/common/qa",
    "common/common/refactor-safe",
    "common/common/audit-bugs",
    "common/common/writing-for-agents",
)


def compose(recipe: Path, target: Path) -> Path:
    """Compose one actual kit recipe without touching a configured destination."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/harness.py"),
            "compose",
            str(recipe),
            "--shared-llm",
            str(ROOT / ".shared-llm"),
            "--target",
            str(target),
            "--placeholder",
            "OPS_REPO=your-repo-ops",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = yaml.safe_load(recipe.read_text())
    return target / config["output"]


@pytest.mark.parametrize("name", AGENTS)
def test_review_agents_include_shared_evidence_and_license(
    name: str, tmp_path: Path
) -> None:
    recipe = PUBLIC / "compose/agents" / f"{name}.yaml"
    output = compose(recipe, tmp_path)
    content = output.read_text()
    assert content.count(EVIDENCE.read_text().strip()) == 1
    assert content.count(LICENSE.read_text().strip()) == 1
    assert (PUBLIC / "layers/agents/common" / f"{name}.md").read_text().strip() in content
    first = output.read_bytes()
    compose(recipe, tmp_path)
    assert output.read_bytes() == first


@pytest.mark.parametrize("name", SKILLS)
def test_adapted_skills_ship_license_and_compose_idempotently(
    name: str, tmp_path: Path
) -> None:
    recipe = PUBLIC / "compose/slash-commands" / f"{name}.yaml"
    output = compose(recipe, tmp_path)
    content = output.read_text()
    assert content.count(LICENSE.read_text().strip()) == 1
    _, frontmatter, _ = content.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == Path(name).name
    assert len(metadata["description"].encode("utf-16-le")) // 2 <= 1024
    first = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    compose(recipe, tmp_path)
    second = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert second == first


def test_prompt_evaluation_reference_is_available_beside_installed_skill(
    tmp_path: Path,
) -> None:
    output = compose(
        PUBLIC / "compose/slash-commands/common/common/writing-for-agents.yaml",
        tmp_path,
    )
    assert "references/evaluation.md" in output.read_text()
    reference = output.parent / "references/evaluation.md"
    source = (
        PUBLIC
        / "layers/slash-commands/common/common/writing-for-agents/resources/references/evaluation.md"
    )
    assert reference.read_bytes() == source.read_bytes()
    assert LICENSE.read_text().strip() in reference.read_text()


def test_whole_file_pr_reviewer_retains_verdicts_and_attribution() -> None:
    content = (PUBLIC / "llm/pi/common/agents/pr-reviewer.md").read_text()
    assert "## Verdict: APPROVED | APPROVED WITH NITS | CHANGES REQUESTED" in content
    for field in ("**Severity**", "**Location**", "**Issue**", "**Fix**"):
        assert field in content
    assert LICENSE.read_text().strip() in content
    for status in ("PASS", "FAIL", "UNVERIFIED"):
        assert status in content


def test_review_prompts_survive_public_copy_and_destination_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct kit composition alone misses inputs outside the synced trees."""
    spec = importlib.util.spec_from_file_location(
        "review_prompt_test_harness", ROOT / "tools/harness.py"
    )
    assert spec is not None and spec.loader is not None
    harness = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, harness)
    spec.loader.exec_module(harness)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(harness, "HOME", home)
    monkeypatch.setattr(harness, "CONFIG_PATH", home / ".shared-llm.yaml")
    monkeypatch.setattr(harness, "DEFAULT_SOURCE", home / ".shared-llm")
    destination = tmp_path / "destination"
    config = {
        "source": str(harness.DEFAULT_SOURCE),
        "global": [],
        "destinations": [
            {
                "path": str(destination),
                "harnesses": ["cc", "pi"],
                "placeholders": {"OPS_REPO": "your-repo-ops"},
            }
        ],
    }
    log = harness.RunLog(verbose=False)
    harness.do_copy(config, log)
    harness.do_compose(config, log)
    for name in AGENTS:
        text = (destination / f".claude/agents/{name}.md").read_text()
        assert EVIDENCE.read_text().strip() in text
        assert LICENSE.read_text().strip() in text
    for name in ("refactor-safe", "audit-bugs", "writing-for-agents"):
        text = (destination / f".claude/skills/{name}/SKILL.md").read_text()
        assert LICENSE.read_text().strip() in text
    reference = destination / ".claude/skills/writing-for-agents/references/evaluation.md"
    assert LICENSE.read_text().strip() in reference.read_text()


def test_qa_source_never_reintroduces_application_edit_permission() -> None:
    content = (PUBLIC / "layers/agents/common/qa.md").read_text()
    assert "NEVER modify application source code" in content
    assert "Fix test bugs AND source code bugs" not in content
    assert "Never weaken tests" in content
