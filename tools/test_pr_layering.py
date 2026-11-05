"""PR agent/skill parity and shared-source propagation, not model evaluation."""

from pathlib import Path
import shutil

import pytest
import yaml

from test_review_prompt_composition import (
    AGENTS,
    EVIDENCE,
    LICENSE,
    PR_CONTRACTS,
    PR_NAMES,
    PR_SHARED,
    PUBLIC,
    ROOT,
    compose,
)


DOMAINS = {
    "pr-review": (
        "general",
        "boundary-tracing",
        "security",
        "integration",
        "compatibility",
        "migration-rollback",
        "architecture",
        "operational-safety",
        "reachability",
        "regression",
        "readiness",
    ),
    "pr-security": ("security", "boundary-tracing"),
    "pr-tests": ("regression", "integration"),
    "pr-compatibility": ("compatibility", "migration-rollback"),
    "pr-readiness": ("readiness",),
    "pr-integration": ("integration", "boundary-tracing", "compatibility"),
    "pr-dead-code": ("reachability",),
    "pr-devops": ("operational-safety", "migration-rollback"),
    "pr-architecture": ("architecture", "integration"),
    "pr-prepare": ("description",),
    "pr-recheck": ("recheck",),
}


def pair_recipes(name: str) -> tuple[Path, Path]:
    return (
        PUBLIC / f"compose/agents/{name}.yaml",
        PUBLIC / f"compose/slash-commands/common/common/{name}.yaml",
    )


@pytest.mark.parametrize("name", PR_NAMES)
def test_agent_is_thin_wrapper_around_exact_matching_skill(
    name: str, tmp_path: Path
) -> None:
    agent_recipe, skill_recipe = pair_recipes(name)
    agent = yaml.safe_load(agent_recipe.read_text())
    skill = yaml.safe_load(skill_recipe.read_text())
    wrapper = PUBLIC / f"layers/agents/common/{name}.md"
    command = PUBLIC / f"layers/slash-commands/common/common/{name}/command.md"
    assert agent["name"] == skill["name"] == name
    assert agent["inputs"] == [str(wrapper.relative_to(ROOT)), *skill["inputs"]]
    assert skill["inputs"][0] == str(command.relative_to(ROOT))
    assert agent["description"] == skill["description"]
    assert len(wrapper.read_text().splitlines()) <= 5
    assert f"`{name}`" in wrapper.read_text()
    assert len(command.read_text().splitlines()) < 100
    assert len(skill["inputs"]) == len(set(skill["inputs"]))
    for path in skill["inputs"]:
        assert path.startswith(".shared-llm/public/layers/")
    expected = [command, PR_SHARED[0], PR_SHARED[1], EVIDENCE, PR_SHARED[2]]
    expected += [PR_CONTRACTS / f"{domain}.md" for domain in DOMAINS[name]]
    expected += [LICENSE]
    assert skill["inputs"] == [str(path.relative_to(ROOT)) for path in expected]
    agent_text = compose(agent_recipe, tmp_path).read_text()
    skill_text = compose(skill_recipe, tmp_path).read_text()
    skill_body = skill_text.split("---", 2)[2].strip()
    agent_body = agent_text.split("---", 2)[2].strip()
    # The existing composer joins markdown source layers with one newline.
    assert agent_body == wrapper.read_text().strip() + "\n" + skill_body
    for path in expected:
        assert skill_body.count(path.read_text().strip()) == 1
    for text in (agent_text, skill_text):
        metadata = yaml.safe_load(text.split("---", 2)[1])
        assert len(metadata["description"].encode("utf-16-le")) // 2 <= 300


@pytest.mark.parametrize(
    "contract", ("snapshot", "security", "integration", "migration-rollback")
)
def test_contract_edit_reaches_every_paired_consumer(
    contract: str, tmp_path: Path
) -> None:
    """Mutate disposable SOURCE, never generated outputs or the user's checkout."""
    source = PR_CONTRACTS / f"{contract}.md"
    source_rel = str(source.relative_to(ROOT))
    isolated = tmp_path / "source"
    consumers = []
    for name in PR_NAMES:
        agent_recipe, skill_recipe = pair_recipes(name)
        skill = yaml.safe_load(skill_recipe.read_text())
        if source_rel not in skill["inputs"]:
            continue
        consumers.append(name)
        for recipe in (agent_recipe, skill_recipe):
            config = yaml.safe_load(recipe.read_text())
            paths = [recipe, ROOT / config["description"]]
            paths += [ROOT / path for path in config["inputs"]]
            for path in paths:
                destination = isolated / path.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
    assert len(consumers) >= 2
    marker = f"PR-LAYER-PROPAGATION-{contract}"
    changed = isolated / source_rel
    changed.write_text(changed.read_text() + f"\n{marker}\n")
    for name in consumers:
        for recipe in pair_recipes(name):
            copied = isolated / recipe.relative_to(ROOT)
            output = compose(copied, tmp_path / "output", isolated / ".shared-llm")
            assert output.read_text().count(marker) == 1
    assert marker not in source.read_text()


def test_pr_contract_boundaries_and_legacy_isolation() -> None:
    content = "\n".join(path.read_text() for path in PR_SHARED)
    for required in (
        "They do not review uncommitted changes",
        "multiple merge bases",
        "revision-qualified reads",
        "synthetic merge commit",
        "read the remote head and base again",
        "Offline review",
        "Do not mix reports",
        "Report-only",
        "or spawn workers",
        "only with explicit authorization",
        "default home databases",
        "A setup failure does not prove a regression",
        "An APPROVED review is not permission to merge",
        "not a replacement for the existing Pi PR reviewer",
        "APPROVED | APPROVED WITH NITS | CHANGES REQUESTED",
        "PASS / FAIL / UNVERIFIED",
        "Retain existing criterion and finding IDs",
    ):
        assert required in content
    assert not (PUBLIC / "layers/agents/common/_pr-review-contract.md").exists()
    for name in AGENTS:
        recipe = PUBLIC / f"compose/agents/{name}.yaml"
        assert "pr-toolkit/contracts/" not in recipe.read_text()


def test_default_general_review_and_domain_evidence_guards() -> None:
    """Instruction regression checks; these do not prove model compliance."""
    command = PUBLIC / "layers/slash-commands/common/common/pr-review/command.md"
    for text in (
        "self-contained general code review",
        "no specialist reports are required",
        "full merge-base-to-head diff",
        "complete affected files",
        "do not make specialist dispatch a prerequisite",
        "unresolved dissent",
    ):
        assert text in command.read_text()
    required = {
        "reachability": (
            "absent grep reference alone",
            "Reflection",
            "external consumers",
        ),
        "readiness": (
            "Skipped or absent execution stays UNVERIFIED",
            "gate-applicability",
            "synthetic merge",
        ),
        "regression": ("same immutable test", "assertions cannot be rewritten"),
        "operational-safety": ("not permission to trigger", "apply infrastructure"),
        "recheck": (
            "FIXED",
            "OPEN",
            "UNVERIFIED",
            "SUPERSEDED",
            "Every inherited finding ID",
        ),
    }
    for contract, phrases in required.items():
        content = (PR_CONTRACTS / f"{contract}.md").read_text()
        for phrase in phrases:
            assert phrase in content


def test_toolkit_names_have_one_pair_and_every_contract_has_a_consumer() -> None:
    agents = {p.stem for p in (PUBLIC / "compose/agents").glob("pr-*.yaml")}
    skills = {
        p.stem
        for p in (PUBLIC / "compose/slash-commands/common/common").glob("pr-*.yaml")
    }
    assert agents == skills == set(PR_NAMES) == set(DOMAINS)
    expected_contracts = {"snapshot", "authority", "report"}
    expected_contracts.update(
        domain for domains in DOMAINS.values() for domain in domains
    )
    assert {path.stem for path in PR_CONTRACTS.glob("*.md")} == expected_contracts
