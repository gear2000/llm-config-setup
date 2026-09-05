"""End-to-end tests for the config-driven, centralized flow in harness.py:
configure -> copy -> compose -> link -> update, plus the drift/collision cases
the review (item 6) called out. The engine runs centrally against throwaway
destination paths; HOME is redirected so the config, hub, and global Pi dirs all
land under a tmp dir.

These are the verification for the redesign: they prove the engine never needs a
copy of itself in a destination, that copy overwrites+flags local edits to common
files, that compose reads a destination's OWN .shared-llm/ and resolves both
common and this_repo inputs, that repo-scoped pi/codex links are created, that a
stale global Pi link is cleaned up while a global collision is only warned, and
that a second update is a clean no-op.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml

TOOLS = Path(__file__).resolve().parent
HARNESS = TOOLS / "harness.py"


def _load():
    spec = importlib.util.spec_from_file_location("harness_cfg_under_test", HARNESS)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _patch_home(m, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    m.HOME = home
    m.CONFIG_PATH = home / ".shared-llm.yaml"
    m.DEFAULT_SOURCE = home / ".shared-llm"
    # Global skill dirs are computed from HOME at call time (_pi_global_skills /
    # _codex_global_skills), so patching HOME here is enough — no real home touched.


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _scaffold_dest(dest: Path) -> None:
    """A minimal destination in the SPLIT layout: a common layer under public/, a
    this_repo overlay under this_repo/, and one repo-owned skill recipe (in
    this_repo/compose/) that pulls a layer from EACH tree by explicit path."""
    s = dest / ".shared-llm"
    _write(s / "public/layers/skills/common/demo/description.md", "A demo skill.\n")
    _write(
        s / "public/layers/skills/common/demo/practices.md", "COMMON practices body.\n"
    )
    _write(s / "this_repo/layers/skills/this_repo/demo.md", "THIS_REPO overlay body.\n")
    _write(
        s / "this_repo/compose/skills/demo.yaml",
        yaml.safe_dump(
            {
                "type": "skill",
                "name": "demo",
                "description": ".shared-llm/public/layers/skills/common/demo/description.md",
                "inputs": [
                    ".shared-llm/public/layers/skills/common/demo/practices.md",
                    ".shared-llm/this_repo/layers/skills/this_repo/demo.md",
                ],
                "output": ".claude/skills/demo/SKILL.md",
            },
            sort_keys=False,
        ),
    )


def _add_slash_skill(dest: Path, name: str, scope: str) -> None:
    """Add a repo-owned slash-command skill recipe under this_repo/compose/
    (scope is the innermost dir, so harness_of resolves it)."""
    s = dest / ".shared-llm"
    base = f"this_repo/layers/slash-commands/this_repo/{scope}/{name}"
    _write(s / f"{base}/command.md", f"{name} body\n")
    _write(s / f"{base}/description.md", f"{name} desc\n")
    _write(
        s / f"this_repo/compose/slash-commands/this_repo/{scope}/{name}.yaml",
        yaml.safe_dump(
            {
                "type": "skill",
                "name": name,
                "description": f".shared-llm/{base}/description.md",
                "inputs": [f".shared-llm/{base}/command.md"],
                "output": f".claude/skills/{name}/SKILL.md",
            },
            sort_keys=False,
        ),
    )


def _cfg(m, dest: Path, harnesses):
    return {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [{"path": str(dest), "harnesses": list(harnesses)}],
    }


def _quiet(m):
    return m.RunLog(verbose=False)


# --- configure -------------------------------------------------------------


def test_configure_creates_and_is_idempotent(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"

    args = argparse.Namespace(
        source=None, dest=str(dest), list="cc,pi", global_list=None, exclude=None
    )
    m.cmd_configure(args)
    assert m.CONFIG_PATH.exists()
    cfg1 = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert cfg1["destinations"] == [
        {"path": str(dest.resolve()), "harnesses": ["cc", "pi"]}
    ]

    # Re-running with the same dest updates in place (no duplicate entry).
    m.cmd_configure(
        argparse.Namespace(
            source=None,
            dest=str(dest),
            list="cc,pi,codex",
            global_list=None,
            exclude=None,
        )
    )
    cfg2 = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert len(cfg2["destinations"]) == 1
    assert cfg2["destinations"][0]["harnesses"] == ["cc", "pi", "codex"]


def test_configure_rejects_unknown_harness(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")

    args = argparse.Namespace(
        source=None,
        dest=str(tmp_path / "dest"),
        list="cc,bogus",
        global_list=None,
        exclude=None,
    )
    with pytest.raises(SystemExit, match="bogus"):
        m.cmd_configure(args)


def test_offering_sets_default_machine_opt_in_and_destination_replacement(
    tmp_path: Path,
) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    destination = {"path": str(tmp_path / "repo"), "harnesses": ["pi"]}
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": [], "destinations": [destination]}

    assert m.selected_offering_sets(cfg) == ("standard",)
    assert m.selected_offering_sets(cfg, destination) == ("standard",)

    cfg["upagent"] = {"offering_sets": ["standard", "claudex"]}
    assert m.selected_offering_sets(cfg, destination) == ("standard", "claudex")

    destination["upagent"] = {"offering_sets": ["standard"]}
    assert m.selected_offering_sets(cfg, destination) == ("standard",)


def test_roster_materialization_is_deterministic_and_replacement_removes_claudex(
    tmp_path: Path,
) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    policy = m._upagent_offerings_module()
    inherited = tmp_path / "inherited.yaml"
    replaced = tmp_path / "replaced.yaml"

    assert m.materialize_offering_roster(("standard", "claudex"), inherited)
    assert not m.materialize_offering_roster(("standard", "claudex"), inherited)
    assert m.materialize_offering_roster(("standard",), replaced)

    assert list(policy.load_roster(inherited).offerings)[-1] == "claudex-gpt-5-6-sol"
    assert "claudex-gpt-5-6-sol" not in policy.load_roster(replaced).offerings


def test_configure_sets_machine_and_destination_offering_replacements(
    tmp_path: Path,
) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    common = {
        "source": None,
        "list": None,
        "global_list": None,
        "exclude": None,
    }
    m.cmd_configure(
        argparse.Namespace(
            **common,
            dest=None,
            offering_sets="standard,claudex",
        )
    )
    dest = tmp_path / "repo"
    m.cmd_configure(
        argparse.Namespace(
            **common,
            dest=str(dest),
            offering_sets="standard",
        )
    )

    cfg = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert cfg["upagent"] == {"offering_sets": ["standard", "claudex"]}
    assert cfg["destinations"][0]["upagent"] == {"offering_sets": ["standard"]}


def test_configure_destination_offering_sets_preserves_existing_harnesses(
    tmp_path: Path,
) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    destination = tmp_path / "repo"
    m.CONFIG_PATH.write_text(
        yaml.safe_dump(
            {
                "destinations": [
                    {
                        "path": str(destination),
                        "harnesses": ["cc", "pi", "codex"],
                    }
                ]
            }
        )
    )

    m.cmd_configure(
        argparse.Namespace(
            source=None,
            list=None,
            global_list=None,
            exclude=None,
            dest=str(destination),
            offering_sets="standard",
        )
    )

    cfg = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert cfg["destinations"][0]["harnesses"] == ["cc", "pi", "codex"]
    assert cfg["destinations"][0]["upagent"] == {"offering_sets": ["standard"]}


def test_config_rejects_unknown_malformed_and_duplicate_offering_sets(
    tmp_path: Path,
) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    for value in (
        ["standard", "foreign"],
        "standard",
        ["standard", "standard"],
        ["claudex"],
    ):
        m.CONFIG_PATH.write_text(yaml.safe_dump({"upagent": {"offering_sets": value}}))
        with pytest.raises(SystemExit, match="offering|UpAgent"):
            m.load_config()
    for block in ({}, None):
        m.CONFIG_PATH.write_text(yaml.safe_dump({"upagent": block}))
        with pytest.raises(SystemExit, match="upagent"):
            m.load_config()


@pytest.mark.parametrize(
    "content",
    [
        (
            "upagent:\n  offering_sets: [standard]\n"
            "upagent:\n  offering_sets: [standard, claudex]\n"
        ),
        (
            "destinations:\n"
            "  - path: /tmp/repo\n"
            "    harnesses: [pi]\n"
            "    upagent:\n"
            "      offering_sets: [standard]\n"
            "      offering_sets: [standard, claudex]\n"
        ),
    ],
)
def test_config_rejects_duplicate_yaml_keys(tmp_path: Path, content: str) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    m.CONFIG_PATH.write_text(content)

    with pytest.raises(SystemExit, match="duplicate key"):
        m.load_config()


# --- copy ------------------------------------------------------------------


def test_copy_propagates_new_and_flags_changed(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["cc", "pi"])

    # Seed the HUB (flat kit mirror) with a common file the dest doesn't have yet,
    # plus one it does (under public/) but with different content — a stale copy
    # that the wholesale public sweep must overwrite.
    hub = m.DEFAULT_SOURCE
    _write(hub / "layers/skills/common/demo/practices.md", "HUB practices v2.\n")
    _write(hub / "layers/llm/common/new-common.md", "brand new common file.\n")

    m.do_copy(cfg, _quiet(m))

    # New common file propagated into the destination's public/ tree.
    assert (
        dest / ".shared-llm/public/layers/llm/common/new-common.md"
    ).read_text() == "brand new common file.\n"
    # The pre-existing common file (public/) was overwritten with the hub version.
    assert (
        dest / ".shared-llm/public/layers/skills/common/demo/practices.md"
    ).read_text() == "HUB practices v2.\n"
    # The this_repo overlay was NEVER touched by copy.
    assert (
        dest / ".shared-llm/this_repo/layers/skills/this_repo/demo.md"
    ).read_text() == "THIS_REPO overlay body.\n"
    generated_roster = (
        dest / ".shared-llm/public/extensions/common/upagent/offerings.yaml"
    )
    assert m._upagent_offerings_module().load_roster(
        generated_roster
    ).selected_sets == ("standard",)


def test_copy_prunes_retired_hub_slash_command_layer(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["cc", "pi"])
    kit = tmp_path / "kit"
    _write(
        kit / ".shared-llm/public/layers/slash-commands/common/claude/kept/command.md",
        "kept\n",
    )
    m.__dict__["project_root"] = lambda: kit
    _write(
        m.DEFAULT_SOURCE / "layers/slash-commands/common/claude/kept/command.md",
        "old kept\n",
    )
    stale = m.DEFAULT_SOURCE / "layers/slash-commands/common/claude/retired/command.md"
    _write(stale, "retired\n")

    m.do_copy(cfg, _quiet(m))

    assert not stale.exists(), "retired hub command layer must not reach destinations"
    assert (
        dest / ".shared-llm/public/layers/slash-commands/common/claude/kept/command.md"
    ).exists()


def test_copy_syncs_public_recipes_wholesale_and_leaves_this_repo(
    tmp_path: Path,
) -> None:
    """copy syncs the kit's recipes into public/compose/ (translating flat paths to
    split), prunes a public recipe the kit no longer ships, and never touches a
    this_repo/compose/ recipe."""
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["cc", "pi"])

    # Fake KIT with one recipe that references BOTH a common layer and a this_repo
    # overlay via flat paths (the copy step must translate them).
    kit = tmp_path / "kit"
    kshared = kit / ".shared-llm/public"
    _write(
        kshared / "compose/agents/backend.yaml",
        yaml.safe_dump(
            {
                "type": "agent",
                "name": "backend",
                "model": "sonnet",
                "description": ".shared-llm/layers/agents/common/backend.description.md",
                "inputs": [
                    ".shared-llm/layers/agents/common/backend.md",
                    ".shared-llm/layers/agents/this_repo/backend.md",
                ],
                "output": ".claude/agents/backend.md",
            },
            sort_keys=False,
        ),
    )
    m.__dict__["project_root"] = lambda: kit
    # The recipe's layers must exist at the destination (the sync gates on this).
    # Common layers ride in via the hub (so the public sweep keeps them); the
    # overlay is repo-owned under this_repo/.
    hub = m.DEFAULT_SOURCE
    _write(hub / "layers/agents/common/backend.description.md", "A backend agent.\n")
    _write(hub / "layers/agents/common/backend.md", "common backend body\n")
    _write(
        dest / ".shared-llm/this_repo/layers/agents/this_repo/backend.md",
        "repo backend overlay\n",
    )

    # A STALE public recipe the kit no longer ships → must be pruned.
    _write(
        dest / ".shared-llm/public/compose/agents/gone.yaml",
        "type: agent\nname: gone\ninputs: []\noutput: x\n",
    )
    # A repo-owned recipe under this_repo/compose/ → must be preserved verbatim.
    this_repo_recipe = dest / ".shared-llm/this_repo/compose/skills/demo.yaml"
    demo_before = this_repo_recipe.read_text()

    m.do_copy(cfg, _quiet(m))

    synced = dest / ".shared-llm/public/compose/agents/backend.yaml"
    assert synced.exists(), "kit recipe not synced into public/compose/"
    text = synced.read_text()
    # Flat kit paths were translated to split form: common -> public, overlay -> this_repo.
    assert ".shared-llm/public/layers/agents/common/backend.md" in text
    assert ".shared-llm/this_repo/layers/agents/this_repo/backend.md" in text
    assert (
        ".shared-llm/layers/agents/common/backend.md" not in text
    )  # no lingering flat path
    # output is NOT translated (lands at the repo root).
    assert "output: .claude/agents/backend.md" in text
    # Stale public recipe pruned; this_repo recipe untouched.
    assert not (dest / ".shared-llm/public/compose/agents/gone.yaml").exists()
    assert this_repo_recipe.read_text() == demo_before


def test_copy_excludes_configured_recipe_and_prunes_stale_copy(tmp_path: Path) -> None:
    """A recipe whose kit source path is in the config `exclude` list is never
    synced into public/compose/, and a stale copy already there is pruned — while a
    non-excluded recipe still syncs normally (exclude is selective, not a nuke)."""
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["cc", "pi"])
    cfg["exclude"] = ["compose/skills/python.yaml"]

    # Fake KIT with two self-contained recipes (no layer refs → nothing gates the
    # sync): one kept, one named in exclude. Both WOULD sync absent the exclude,
    # so a missing python.yaml proves the exclude — not a missing-inputs skip.
    kit = tmp_path / "kit"
    kshared = kit / ".shared-llm/public"
    _write(
        kshared / "compose/agents/backend.yaml",
        yaml.safe_dump(
            {
                "type": "agent",
                "name": "backend",
                "inputs": [],
                "output": ".claude/agents/backend.md",
            },
            sort_keys=False,
        ),
    )
    _write(
        kshared / "compose/skills/python.yaml",
        yaml.safe_dump(
            {
                "type": "skill",
                "name": "python",
                "inputs": [],
                "output": ".claude/skills/python/SKILL.md",
            },
            sort_keys=False,
        ),
    )
    m.__dict__["project_root"] = lambda: kit

    # A stale copy of the EXCLUDED recipe already at the destination → must be pruned.
    _write(
        dest / ".shared-llm/public/compose/skills/python.yaml",
        "type: skill\nname: python\ninputs: []\noutput: x\n",
    )

    m.do_copy(cfg, _quiet(m))

    # Non-excluded recipe synced normally.
    assert (dest / ".shared-llm/public/compose/agents/backend.yaml").exists(), (
        "non-excluded recipe should still sync"
    )
    # Excluded recipe never synced, and its stale copy was pruned.
    assert not (dest / ".shared-llm/public/compose/skills/python.yaml").exists(), (
        "excluded recipe must be absent from the destination (skipped + stale copy pruned)"
    )


# --- compose ---------------------------------------------------------------


def test_compose_reads_destination_shared_llm_and_merges_overlay(
    tmp_path: Path,
) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    m.do_compose(_cfg(m, dest, ["cc", "pi"]), _quiet(m))

    out = (dest / ".claude/skills/demo/SKILL.md").read_text()
    assert "COMMON practices body." in out
    assert "THIS_REPO overlay body." in out
    # order: common input before this_repo input
    assert out.index("COMMON practices body.") < out.index("THIS_REPO overlay body.")


def test_compose_prunes_retired_meta_planner_skill(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    stale = dest / ".claude/skills/meta-cc-plan-and-grill/SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("---\nname: meta-cc-plan-and-grill\n---\nold wrapper\n")

    m.do_compose(_cfg(m, dest, ["cc", "pi"]), _quiet(m))

    assert not stale.parent.exists()


def test_compose_prunes_removed_legacy_runner_skill(tmp_path: Path) -> None:
    """Removing the old team recipe also removes its generated entrypoint."""
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    _add_slash_skill(dest, "team", "claude")
    command = (
        dest
        / ".shared-llm/this_repo/layers/slash-commands/this_repo/claude/team/command.md"
    )
    command.write_text("The meta-orchestrator injects ask_brain.\n")
    cfg = _cfg(m, dest, ["cc", "pi"])

    m.do_compose(cfg, _quiet(m))
    generated = dest / ".claude/skills/team/SKILL.md"
    assert generated.is_file()

    (
        dest / ".shared-llm/this_repo/compose/slash-commands/this_repo/claude/team.yaml"
    ).unlink()
    m.do_compose(cfg, _quiet(m))
    assert not generated.exists()


def test_compose_this_repo_nested_md_recipes_and_skips_public_examples(
    tmp_path: Path,
) -> None:
    """Dest-owned claude-md/agents-md recipes under this_repo/compose/ all run.
    Kit example-* recipes in public/compose/claude-md/ do not land in the dest."""
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    s = dest / ".shared-llm"
    _write(s / "public/layers/llm/common/root.md", "ROOT LAYER\n")
    _write(s / "public/layers/llm/this_repo/packages/example.md", "EXAMPLE LAYER\n")
    _write(s / "this_repo/layers/llm/this_repo/packages.md", "PACKAGES DIR LAYER\n")
    _write(s / "this_repo/layers/llm/this_repo/packages/foo.md", "FOO PACKAGE LAYER\n")
    _write(
        s / "public/compose/claude-md/root.yaml",
        yaml.safe_dump(
            {
                "type": "claude-md",
                "inputs": [".shared-llm/public/layers/llm/common/root.md"],
                "output": "CLAUDE.md",
            },
            sort_keys=False,
        ),
    )
    _write(
        s / "public/compose/claude-md/example-package.yaml",
        yaml.safe_dump(
            {
                "type": "claude-md",
                "inputs": [".shared-llm/public/layers/llm/this_repo/packages/example.md"],
                "output": "samples/src/packages/example_package/CLAUDE.md",
            },
            sort_keys=False,
        ),
    )
    _write(
        s / "this_repo/compose/claude-md/packages/foo.yaml",
        yaml.safe_dump(
            {
                "type": "claude-md",
                "inputs": [".shared-llm/this_repo/layers/llm/this_repo/packages/foo.md"],
                "output": "src/packages/foo/CLAUDE.md",
            },
            sort_keys=False,
        ),
    )
    _write(
        s / "this_repo/compose/agents-md/packages.yaml",
        yaml.safe_dump(
            {
                "type": "agents-md",
                "inputs": [".shared-llm/this_repo/layers/llm/this_repo/packages.md"],
                "output": "src/packages/AGENTS.md",
            },
            sort_keys=False,
        ),
    )

    m.do_compose(_cfg(m, dest, ["cc", "pi"]), _quiet(m))

    assert "ROOT LAYER" in (dest / "CLAUDE.md").read_text()
    assert "FOO PACKAGE LAYER" in (dest / "src/packages/foo/CLAUDE.md").read_text()
    assert "PACKAGES DIR LAYER" in (dest / "src/packages/AGENTS.md").read_text()
    assert not (dest / "samples/src/packages/example_package/CLAUDE.md").exists()


# --- link ------------------------------------------------------------------


def test_link_creates_global_pi_and_codex(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["cc", "pi", "codex"])
    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    # Links land in the GLOBAL (no-trust-gate) dirs, not repo-scoped.
    pi_link = home / ".pi/agent/skills/demo"
    codex_link = home / ".agents/skills/demo"
    assert (
        pi_link.is_symlink()
        and pi_link.resolve() == (dest / ".claude/skills/demo").resolve()
    )
    assert (
        codex_link.is_symlink()
        and codex_link.resolve() == (dest / ".claude/skills/demo").resolve()
    )
    # No repo-scoped .pi/skills is created.
    assert not (dest / ".pi/skills").exists()
    # cc reads .claude/ directly.
    assert (dest / ".claude/skills/demo/SKILL.md").exists()


def test_cursor_harness_aliases_codex_surface(tmp_path: Path) -> None:
    """A cursor-only destination gets the codex surface: skills linked into
    ~/.agents/skills (what the Cursor Agent CLI reads), nothing cursor-specific."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["cursor"])
    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    cursor_link = home / ".agents/skills/demo"
    assert (
        cursor_link.is_symlink()
        and cursor_link.resolve() == (dest / ".claude/skills/demo").resolve()
    )
    # No Pi links for a cursor-only destination.
    assert not (home / ".pi/agent/skills/demo").exists()


def test_parse_harnesses_accepts_cursor() -> None:
    m = _load()
    assert m.parse_harnesses("cc,cursor") == ["cc", "cursor"]
    assert m.wants_codex_surface(["cursor"])
    assert m.wants_codex_surface(["codex"])
    assert not m.wants_codex_surface(["cc", "pi"])


def test_link_prunes_our_stale_global_link_not_foreign(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["pi"])
    m.do_compose(cfg, _quiet(m))

    g = home / ".pi/agent/skills"
    g.mkdir(parents=True, exist_ok=True)
    # A stale link WE own (points into the dest) for a skill no longer present.
    ours_stale = g / "gone"
    ours_stale.symlink_to(dest / ".claude/skills/gone")
    # A foreign real dir that must never be touched.
    (g / "foreign").mkdir()
    (g / "foreign/SKILL.md").write_text("not ours\n")

    m.do_link(cfg, _quiet(m))

    assert (g / "demo").is_symlink()  # desired link created
    assert not ours_stale.exists()  # our stale link pruned
    assert (g / "foreign/SKILL.md").exists()  # foreign left untouched


def test_link_cleans_up_abandoned_repo_scoped_pi(tmp_path: Path) -> None:
    """The reverted repo-scoped approach left <repo>/.pi/skills links; link removes them."""
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["pi"])
    m.do_compose(cfg, _quiet(m))
    # simulate the old repo-scoped link
    rs = dest / ".pi/skills"
    rs.mkdir(parents=True)
    (rs / "demo").symlink_to(dest / ".claude/skills/demo")

    m.do_link(cfg, _quiet(m))
    assert not (dest / ".pi/skills").exists()  # abandoned repo-scoped dir cleaned


# --- update (orchestration + idempotency) ----------------------------------


def test_update_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    # A minimal kit that ships the demo's common layers, so the public sweep keeps
    # them (a real destination's public/ layers are always kit-provided).
    kit = tmp_path / "kit"
    _write(
        kit / ".shared-llm/public/layers/skills/common/demo/description.md",
        "A demo skill.\n",
    )
    _write(
        kit / ".shared-llm/public/layers/skills/common/demo/practices.md",
        "COMMON practices body.\n",
    )
    m.__dict__["project_root"] = lambda: kit
    # Seed the hub so copy has something to propagate.
    _write(m.DEFAULT_SOURCE / "layers/llm/common/x.md", "hub common.\n")

    cfg = _cfg(m, dest, ["cc", "pi", "codex"])
    m.save_config(cfg)

    m.cmd_update(argparse.Namespace(verbose=False))

    # First run created the global links.
    assert (dest / ".claude/skills/demo/SKILL.md").exists()
    assert (home / ".pi/agent/skills/demo").is_symlink()
    assert (home / ".agents/skills/demo").is_symlink()

    # Second run: global reconcile is a clean no-op (nothing created/repointed/pruned).
    c = m._reconcile_global(
        home / ".pi/agent/skills",
        m._common_pi_skills(dest),
        [dest],
        m.RunLog(verbose=False),
    )
    assert c["create"] == 0 and c["repoint"] == 0 and c["prune"] == 0


def test_update_materializes_home_roster_for_upagent_only_config(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    m.save_config(
        {
            "source": str(m.DEFAULT_SOURCE),
            "global": [],
            "destinations": [],
            "upagent": {"offering_sets": ["standard", "claudex"]},
        }
    )

    m.cmd_update(argparse.Namespace(verbose=False))

    path = home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    roster = m._upagent_offerings_module().load_roster(path)
    assert roster.selected_sets == ("standard", "claudex")


# --- global home-skill routing ---------------------------------------------


def test_global_routes_by_scope_and_excludes_do_star_from_cc(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": ["cc", "pi", "codex"],
        "destinations": [],
    }
    m.do_global(cfg, _quiet(m))

    cc = home / ".claude/skills"
    pi = home / ".pi/agent/skills"
    codex = home / ".agents/skills"

    # Convention skills go to all three.
    for base in (cc, pi, codex):
        assert (base / "python/SKILL.md").exists(), f"python missing from {base}"

    # Portable non-do-* command (qa) reaches cc, pi, codex.
    assert (cc / "qa/SKILL.md").exists()
    assert (pi / "qa/SKILL.md").exists()

    # do-* workflow commands reach Pi only: not cc, not codex.
    assert (pi / "do-plan/SKILL.md").exists()
    assert not (cc / "do-plan").exists(), "do-* must not land in Claude Code home"
    assert not (codex / "do-plan").exists(), "do-* must not land in Codex home"

    # Claude-scoped cc-* command reaches cc only.
    assert (cc / "cc-plan/SKILL.md").exists()
    assert not (pi / "cc-plan").exists()


def test_global_respects_configured_harness_subset(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    # Only pi configured globally — nothing should land in cc or codex home dirs.
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["pi"], "destinations": []}
    m.do_global(cfg, _quiet(m))
    assert (home / ".pi/agent/skills/python/SKILL.md").exists()
    assert not (home / ".claude/skills/python").exists()
    assert not (home / ".agents/skills/python").exists()


def test_global_leaves_foreign_and_divergent_untouched(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cc = home / ".claude/skills"
    # A divergent real dir at qa: must be left untouched (not ours).
    _write(cc / "qa/SKILL.md", "MY OWN qa skill — do not overwrite.\n")
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": []}
    m.do_global(cfg, _quiet(m))
    assert (cc / "qa/SKILL.md").read_text() == "MY OWN qa skill — do not overwrite.\n"


def test_do_star_is_pi_only_cc_star_is_claude_only(tmp_path: Path) -> None:
    """The core routing rule: do-* -> Pi only (out of Claude's .claude/skills),
    cc-* -> Claude only (never Pi/codex), common -> both."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)  # 'demo' = common
    _add_slash_skill(dest, "do-plan-and-grill", "common")  # do-* (recipe scope common)
    _add_slash_skill(dest, "do-loop", "claude")  # do-* even if recipe is claude-scoped
    _add_slash_skill(dest, "cc-plan-and-grill", "claude")  # cc-*
    cfg = _cfg(m, dest, ["cc", "pi", "codex"])
    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    cs = dest / ".claude/skills"
    pi = home / ".pi/agent/skills"
    codex = home / ".agents/skills"

    # Claude Code (.claude/skills): cc-* + common present, NO do-*
    assert (cs / "cc-plan-and-grill/SKILL.md").exists()
    assert (cs / "demo/SKILL.md").exists()
    assert not (cs / "do-plan-and-grill").exists(), (
        "do-* must be routed OUT of Claude's dir"
    )
    assert not (cs / "do-loop").exists()
    # do-* routed to the Pi-only source dir
    assert (dest / ".pi-skills/do-plan-and-grill/SKILL.md").exists()
    assert (dest / ".pi-skills/do-loop/SKILL.md").exists()

    # Pi: do-* + common, NO cc-*
    assert (pi / "do-plan-and-grill").is_symlink()
    assert (pi / "do-loop").is_symlink()
    assert (pi / "demo").is_symlink()
    assert not (pi / "cc-plan-and-grill").exists(), "cc-* must never reach Pi"

    # Codex: common only, no do-* (Pi-only) and no cc-* (Claude-only)
    assert (codex / "demo").is_symlink()
    assert not (codex / "do-plan-and-grill").exists()
    assert not (codex / "cc-plan-and-grill").exists()


def test_generic_upagent_slash_skills_compose_and_link_to_every_harness(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    kit = TOOLS.parent / ".shared-llm/public"
    shared = dest / ".shared-llm/public"
    names = (
        "upagent-run",
        "upagent-ls",
        "upagent-get",
        "upagent-cancel",
        "upagent-cleanup",
    )
    for name in names:
        shutil.copytree(
            kit / f"layers/slash-commands/common/common/{name}",
            shared / f"layers/slash-commands/common/common/{name}",
        )
        recipe = shared / f"compose/slash-commands/common/common/{name}.yaml"
        recipe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            kit / f"compose/slash-commands/common/common/{name}.yaml", recipe
        )
    cfg = _cfg(m, dest, ["cc", "pi", "codex"])

    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    commands = {
        "upagent-run": "request",
        "upagent-ls": "lists",
        "upagent-get": "get",
        "upagent-cancel": "cancel",
        "upagent-cleanup": "cleanup",
    }
    for name in names:
        skill = dest / f".claude/skills/{name}/SKILL.md"
        assert skill.is_file()
        assert f"just upagent {commands[name]}" in skill.read_text()
        assert (home / f".pi/agent/skills/{name}").is_symlink()
        assert (home / f".agents/skills/{name}").is_symlink()


def test_repo_pi_front_door_overrides_portable_do_recipe(tmp_path: Path) -> None:
    """The repository Pi front door is the final output when it replaces a portable do-* recipe."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    shared = dest / ".shared-llm"
    public_base = "public/layers/slash-commands/common/common/do-full"
    _write(shared / f"{public_base}/description.md", "portable description\n")
    _write(shared / f"{public_base}/command.md", "PORTABLE PI FRONT DOOR\n")
    _write(
        shared / "public/compose/slash-commands/common/common/do-full.yaml",
        yaml.safe_dump(
            {
                "type": "skill",
                "name": "do-full",
                "description": f".shared-llm/{public_base}/description.md",
                "inputs": [f".shared-llm/{public_base}/command.md"],
                "output": ".claude/skills/do-full/SKILL.md",
            },
            sort_keys=False,
        ),
    )
    _add_slash_skill(dest, "do-full", "pi")
    cfg = _cfg(m, dest, ["pi"])

    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    pi_front_door = dest / ".pi-skills/do-full/SKILL.md"
    assert pi_front_door.exists()
    assert "do-full body" in pi_front_door.read_text()
    assert "PORTABLE PI FRONT DOOR" not in pi_front_door.read_text()
    assert not (dest / ".claude/skills/do-full").exists()
    assert m.harness_of(dest, "do-full") == "pi"
    assert (
        home / ".pi/agent/skills/do-full"
    ).resolve() == pi_front_door.parent.resolve()


def test_route_do_skills_prunes_removed_command_output(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    _add_slash_skill(dest, "do-kept", "common")
    m.do_compose(_cfg(m, dest, ["pi"]), _quiet(m))
    stale = dest / ".pi-skills/do-retired/SKILL.md"
    _write(stale, "stale\n")

    m._route_do_skills(dest, _quiet(m))

    assert (dest / ".pi-skills/do-kept/SKILL.md").exists()
    assert not stale.exists(), "removed do-* output must not remain routable by Pi"


def test_compose_prunes_removed_cc_command_output(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    _add_slash_skill(dest, "cc-kept", "claude")
    stale = dest / ".claude/skills/cc-retired/SKILL.md"
    _write(stale, "stale\n")

    m.do_compose(_cfg(m, dest, ["cc"]), _quiet(m))

    assert (dest / ".claude/skills/cc-kept/SKILL.md").exists()
    assert not stale.exists(), (
        "removed cc-* output must not remain discoverable by Claude Code"
    )


def test_common_meta_plan_aliases_reach_all_non_prefix_harnesses(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    _add_slash_skill(dest, "meta-plan-convert", "common")
    _add_slash_skill(dest, "meta-plan-check", "common")
    cfg = _cfg(m, dest, ["pi", "codex"])

    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    pi = home / ".pi/agent/skills"
    codex = home / ".agents/skills"
    assert (pi / "meta-plan-convert").is_symlink()
    assert (pi / "meta-plan-check").is_symlink()
    assert (codex / "meta-plan-convert").is_symlink()
    assert (codex / "meta-plan-check").is_symlink()


def test_check_passes_after_update_and_fails_on_violation(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    _add_slash_skill(dest, "do-loop", "claude")
    _add_slash_skill(dest, "cc-loop", "claude")
    cfg = _cfg(m, dest, ["cc", "pi", "codex"])
    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    assert m.do_check(cfg, _quiet(m)) is True  # invariants hold after a clean update

    # Inject a violation: a do-* leaks into Claude's .claude/skills.
    _write(dest / ".claude/skills/do-sneaky/SKILL.md", "---\nname: do-sneaky\n---\n")
    assert m.do_check(cfg, _quiet(m)) is False


# --- global home runtime (agents + claude/pi runtime) ----------------------


def test_home_runtime_generates_machine_roster_without_global_harnesses(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [],
        "upagent": {"offering_sets": ["standard", "claudex"]},
    }

    m.do_home_runtime(cfg, _quiet(m))

    path = home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    roster = m._upagent_offerings_module().load_roster(path)
    assert roster.selected_sets == ("standard", "claudex")


def test_home_roster_refuses_symlinked_generated_parent(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    generated = home / ".shared-llm/generated"
    generated.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (generated / "extensions").symlink_to(outside, target_is_directory=True)
    target = generated / "extensions/common/upagent/offerings.yaml"

    with pytest.raises(m.GeneratedTreeError, match="symlink"):
        m.materialize_offering_roster(("standard",), target)
    assert not (outside / "common/upagent/offerings.yaml").exists()


def test_home_roster_replaces_equal_content_leaf_symlink(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    target = home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    expected = m._upagent_offerings_module().render_roster(["standard"])
    outside.write_text(expected)
    target.symlink_to(outside)

    assert m.materialize_offering_roster(("standard",), target)
    assert target.is_file() and not target.is_symlink()
    assert target.read_text() == expected
    assert outside.read_text() == expected


def test_home_runtime_copies_agents_and_excludes_codex(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": ["cc", "pi", "codex"],
        "destinations": [],
    }
    m.do_home_runtime(cfg, _quiet(m))

    # Generic agents land in the cc + pi home agent dirs (a known persona: backend).
    assert (home / ".claude/agents/backend.md").exists()
    # Pi reads personas from inside its config dir, not from ~/.pi/agents.
    assert (home / ".pi/agent/agents/backend.md").exists()
    assert not (home / ".pi/agents").exists()
    # Codex has no user-agent dir — never invented.
    assert not (home / ".codex/agents").exists()
    assert not (home / ".agents/agents").exists()


def test_home_runtime_migrates_legacy_pi_agents_dir(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc", "pi"], "destinations": []}

    # First run populates the new dir; seed the legacy dir the way the old engine
    # did — a byte-identical copy of a composed agent plus a persona symlink.
    m.do_home_runtime(cfg, _quiet(m))
    legacy = home / ".pi/agents"
    legacy.mkdir(parents=True)
    (legacy / "backend.md").write_bytes(
        (home / ".pi/agent/agents/backend.md").read_bytes()
    )
    persona = (
        m.project_root() / ".shared-llm/public/llm/pi/common/agents/doc-reviewer.md"
    )
    (legacy / "doc-reviewer.md").symlink_to(persona)
    # A DIVERGENT same-name file: qa IS a composed persona, but this copy has been
    # hand-edited and no longer matches the staged output. Nothing proves the kit
    # wrote these bytes, so migration must PRESERVE it — deleting on a name match
    # alone would be irreversible data loss.
    divergent = "---\nname: qa\ndescription: hand-edited local override\n---\n"
    (legacy / "qa.md").write_text(divergent)
    assert (legacy / "qa.md").read_text() != (
        home / ".pi/agent/agents/qa.md"
    ).read_text()
    (legacy / "someone-elses.md").write_text("---\nname: someone-elses\n---\n")

    m.do_home_runtime(cfg, _quiet(m))

    # Provably-ours entries are gone: the byte-identical copy and the kit symlink.
    assert not (legacy / "backend.md").exists()
    assert not (legacy / "doc-reviewer.md").is_symlink()
    # The divergent same-name file is PRESERVED, untouched — the data-loss guard:
    # a name match alone must never delete a file whose bytes we did not write.
    assert (legacy / "qa.md").exists()
    assert (legacy / "qa.md").read_text() == divergent
    # A genuinely foreign file (a name the kit does not compose) survives too.
    assert (legacy / "someone-elses.md").exists()
    # Because divergent/foreign files remain, the legacy dir is NOT removed.
    assert legacy.is_dir()

    # Once the user clears the preserved files, the next run removes the empty dir.
    (legacy / "qa.md").unlink()
    (legacy / "someone-elses.md").unlink()
    m.do_home_runtime(cfg, _quiet(m))
    assert not legacy.exists()


def test_home_runtime_installs_claude_and_pi_runtime(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc", "pi"], "destinations": []}
    m.do_home_runtime(cfg, _quiet(m))

    # Claude settings scaffolded from template.
    assert (home / ".claude/settings.json").exists()
    # Pi extensions symlinked into ~/.pi/agent/extensions.
    planish_ext = home / ".pi/agent/extensions/do-planish.ts"
    provider_policy_ext = home / ".pi/agent/extensions/disable-amazon-bedrock.ts"
    assert planish_ext.is_symlink()
    assert provider_policy_ext.is_symlink()
    # Pi settings scaffolded.
    assert (home / ".pi/agent/settings.json").exists()


def test_home_runtime_reconciles_managed_herdr_config(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["pi"], "destinations": []}

    m.do_home_runtime(cfg, _quiet(m))
    target = home / ".config/herdr/config.toml"
    assert target.is_symlink()
    # The link points at the DURABLE generated copy, never at the repo checkout,
    # so a moved or deleted kit clone cannot break the user's runtime.
    generated = home / ".shared-llm/generated/herdr-config.toml"
    assert target.resolve() == generated.resolve()
    assert (
        generated.read_bytes() == (m.project_root() / "herdr-config.toml").read_bytes()
    )

    counts = m.reconcile(
        m.plan_herdr_config(m.project_root()),
        m.repo_family(m.project_root()),
        plan_only=False,
        force=False,
        repo_root=m.project_root(),
    )
    assert counts["create"] == counts["repoint"] == counts["prune"] == 0


def test_herdr_config_preserves_foreign_destination(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    target = home / ".config/herdr/config.toml"
    _write(target, "mine = true\n")

    counts = m.reconcile(
        m.plan_herdr_config(m.project_root()),
        m.repo_family(m.project_root()),
        plan_only=False,
        force=False,
        repo_root=m.project_root(),
    )
    assert target.read_text() == "mine = true\n"
    assert counts["skip-foreign"] == 1


def test_herdr_config_repoints_and_prunes_managed_stale_link(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = tmp_path / "kit"
    source = kit / "herdr-config.toml"
    _write(source, "onboarding = false\n")
    target = home / ".config/herdr/config.toml"
    target.parent.mkdir(parents=True)
    # The pre-decoupling deployment: a link straight at the kit's own managed
    # path. It is ours, so this run repoints it at the durable generated copy.
    target.symlink_to(source)

    counts = m.reconcile(
        m.plan_herdr_config(kit),
        m.repo_family(kit),
        plan_only=False,
        force=False,
        repo_root=kit,
    )
    generated = home / ".shared-llm/generated/herdr-config.toml"
    assert target.resolve() == generated.resolve()
    assert generated.read_bytes() == source.read_bytes()
    assert counts["repoint"] == 1

    source.unlink()
    counts = m.reconcile(
        m.plan_herdr_config(kit),
        m.repo_family(kit),
        plan_only=False,
        force=False,
        repo_root=kit,
    )
    assert not target.exists() and not target.is_symlink()
    assert counts["prune"] == 1


def test_home_runtime_exclude_by_source_path_skips_install(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    # Baseline: without exclude, hooks + statusline install.
    base_cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": ["cc"],
        "destinations": [],
        "exclude": [],
    }
    m.do_home_runtime(base_cfg, _quiet(m))
    assert (home / ".claude/statusline.sh").exists()
    hooks_dir = home / ".claude/hooks"
    assert hooks_dir.is_dir() and any(hooks_dir.iterdir()), (
        "hooks should install without exclude"
    )

    # Now a fresh home WITH hooks excluded by source path.
    home2 = tmp_path / "home2"
    _patch_home(m, home2)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": ["cc"],
        "destinations": [],
        "exclude": ["llm/claude/common/hooks"],
    }
    m.do_home_runtime(cfg, _quiet(m))
    # hooks skipped, statusline still installed (not excluded)
    assert not (home2 / ".claude/hooks").exists() or not any(
        (home2 / ".claude/hooks").iterdir()
    )
    assert (home2 / ".claude/statusline.sh").exists()


def test_home_runtime_exclude_drops_pi_extension(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": ["pi"],
        "destinations": [],
        "exclude": ["llm/pi/common/extensions/do-planish.ts"],
    }
    m.do_home_runtime(cfg, _quiet(m))
    # do-planish.ts excluded, but other extensions still linked (dir not empty)
    ext = home / ".pi/agent/extensions"
    assert not (ext / "do-planish.ts").exists(), "excluded extension must not be linked"
    assert ext.is_dir() and any(ext.iterdir()), "other extensions still install"


def test_home_runtime_scaffold_never_clobbers_existing_settings(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    _write(home / ".claude/settings.json", '{"mine": true}\n')
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": []}
    m.do_home_runtime(cfg, _quiet(m))
    # Existing settings preserved verbatim.
    assert (home / ".claude/settings.json").read_text() == '{"mine": true}\n'


# --- public sweep / this_repo isolation ------------------------------------


def test_public_layers_swept_and_this_repo_untouched(tmp_path: Path) -> None:
    """copy sweeps public/layers/ wholesale (a layer the hub no longer ships is
    pruned) and never prunes anything under this_repo/."""
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    # Empty kit so the recipe sync is a no-op — isolate the layer sweep.
    empty_kit = tmp_path / "emptykit"
    (empty_kit / ".shared-llm").mkdir(parents=True)
    m.__dict__["project_root"] = lambda: empty_kit
    cfg = _cfg(m, dest, ["cc"])

    hub = m.DEFAULT_SOURCE
    _write(hub / "layers/skills/common/demo/practices.md", "hub practices\n")
    # A stale public layer the hub does NOT ship → must be swept.
    _write(dest / ".shared-llm/public/layers/skills/common/stale/old.md", "STALE\n")

    m.do_copy(cfg, _quiet(m))

    assert not (
        dest / ".shared-llm/public/layers/skills/common/stale/old.md"
    ).exists(), "stale public layer not swept"
    # this_repo layer untouched by the sweep.
    assert (
        dest / ".shared-llm/this_repo/layers/skills/this_repo/demo.md"
    ).read_text() == "THIS_REPO overlay body.\n"


# --- build-time placeholder fill -------------------------------------------


def _scaffold_placeholder_dest(dest: Path, *, template: bool = False) -> None:
    """A destination whose composed skill pulls a public/ layer carrying a
    {{PROJECT_NAME}} placeholder. With `template`, the body layer is a TEMPLATE.*
    stub under this_repo/ (exempt from the unfilled check)."""
    s = dest / ".shared-llm"
    _write(
        s / "public/layers/skills/common/tok/description.md",
        "desc for {{PROJECT_NAME}}\n",
    )
    if template:
        body = ".shared-llm/this_repo/layers/skills/this_repo/TEMPLATE.tok.md"
        _write(
            s / "this_repo/layers/skills/this_repo/TEMPLATE.tok.md",
            "Fill {{PROJECT_NAME}} here.\n",
        )
    else:
        body = ".shared-llm/public/layers/skills/common/tok/practices.md"
        _write(
            s / "public/layers/skills/common/tok/practices.md",
            "Build {{PROJECT_NAME}} the right way.\n",
        )
    _write(
        s / "this_repo/compose/skills/tok.yaml",
        yaml.safe_dump(
            {
                "type": "skill",
                "name": "tok",
                "description": ".shared-llm/public/layers/skills/common/tok/description.md",
                "inputs": [body],
                "output": ".claude/skills/tok/SKILL.md",
            },
            sort_keys=False,
        ),
    )


def test_placeholder_filled_from_config(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_placeholder_dest(dest)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [
            {
                "path": str(dest),
                "harnesses": ["cc"],
                "placeholders": {"PROJECT_NAME": "Acme"},
            }
        ],
    }
    m.do_compose(cfg, _quiet(m))
    out = (dest / ".claude/skills/tok/SKILL.md").read_text()
    assert "Build Acme the right way." in out  # body filled
    assert "description: desc for Acme" in out  # frontmatter description filled
    assert "{{PROJECT_NAME}}" not in out


def test_unfilled_placeholder_fails_loud(tmp_path: Path, capsys) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_placeholder_dest(dest)
    # No placeholders map → {{PROJECT_NAME}} cannot be filled.
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [{"path": str(dest), "harnesses": ["cc"]}],
    }
    with pytest.raises(SystemExit) as error:
        m.do_compose(cfg, _quiet(m))
    assert error.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "PROJECT_NAME" in err
    assert "tok/SKILL.md" in err
    # No partial/garbage output written for the failing recipe.
    assert not (dest / ".claude/skills/tok/SKILL.md").exists()


def test_template_input_exempt_from_placeholder_check(tmp_path: Path) -> None:
    """A recipe pulling a TEMPLATE.* stub is exempt — it composes without failing,
    leaving the placeholder in place (the kit's deliberately-unfilled convention)."""
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "dest"
    _scaffold_placeholder_dest(dest, template=True)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [{"path": str(dest), "harnesses": ["cc"]}],
    }
    m.do_compose(cfg, _quiet(m))  # must NOT raise
    out = (dest / ".claude/skills/tok/SKILL.md").read_text()
    assert "{{PROJECT_NAME}}" in out  # left unfilled, exempt


if __name__ == "__main__":
    import subprocess

    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))


# --- durable generated tree + manifest safety ------------------------------


def test_no_home_link_targets_the_repo_checkout(tmp_path: Path) -> None:
    """Full decoupling: every home symlink resolves into ~/.shared-llm/generated,
    never into the kit checkout — a moved or deleted clone cannot break the
    user's runtime."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc", "pi"], "destinations": []}
    m.do_global_flow(cfg, _quiet(m))

    kit = str(m.project_root())
    generated = home / ".shared-llm/generated"
    checked = 0
    for base in (home / ".claude", home / ".pi", home / ".agents", home / ".config"):
        if not base.is_dir():
            continue
        for entry in base.rglob("*"):
            if not entry.is_symlink():
                continue
            checked += 1
            resolved = entry.resolve()
            assert not str(resolved).startswith(kit + "/"), f"{entry} -> {resolved}"
            assert resolved.is_relative_to(generated.resolve()), f"{entry}"
    assert checked, "expected home symlinks to exist"
    # The whole pieces are COPIES under the generated tree, not links out of it.
    assert (generated / "claude/statusline.sh").is_file()
    assert any((generated / "pi/extensions").iterdir())
    assert (generated / "pi/agents/doc-reviewer.md").is_file()


def test_ownership_is_resolved_path_not_substring(tmp_path: Path) -> None:
    """A foreign path that merely SPELLS the generated marker is not ours."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    decoy = tmp_path / "foreign/.shared-llm/generated/x"
    _write(decoy, "not ours\n")
    link = home / "decoy-link"
    link.symlink_to(decoy)
    assert m._link_points_generated(link) is False

    real = home / ".shared-llm/generated/skills/thing"
    _write(real / "SKILL.md", "ours\n")
    ours = home / "ours-link"
    ours.symlink_to(real)
    assert m._link_points_generated(ours) is True

    # A DANGLING link into our generated tree is still ours (so it is prunable).
    dangling = home / "dangling-link"
    dangling.symlink_to(home / ".shared-llm/generated/skills/gone")
    assert m._link_points_generated(dangling) is True


def test_retired_recipe_output_is_not_resurrected_from_staging(tmp_path: Path) -> None:
    """Compose staging is cumulative on disk; an output left by a retired recipe
    must not be read back as current and redeployed."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": []}
    m.do_global_flow(cfg, _quiet(m))

    staging = m.project_root() / "examples"
    _write(staging / ".claude/agents/retired-agent.md", "---\nname: retired\n---\n")
    _write(staging / ".claude/skills/retired-skill/SKILL.md", "---\nname: r\n---\n")

    m.do_global_flow(cfg, _quiet(m))
    assert not (home / ".claude/agents/retired-agent.md").exists()
    assert not (home / ".claude/skills/retired-skill").exists()


def test_empty_global_list_prunes_previous_deployment(tmp_path: Path) -> None:
    """Empty `global:` means an empty desired set — everything previously
    deployed is pruned, identically across every entry point."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": []}
    m.do_global_flow(cfg, _quiet(m))
    agent = home / ".claude/agents/backend.md"
    skill = home / ".claude/skills/python"
    assert agent.is_symlink() and skill.is_symlink()

    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    assert not agent.exists() and not agent.is_symlink()
    assert not skill.exists() and not skill.is_symlink()
    # Settings are mutable and user-owned — never destructively pruned.
    assert (home / ".claude/settings.json").is_file()


def test_unreadable_manifest_prunes_nothing(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": []}
    m.do_global_flow(cfg, _quiet(m))
    agent = home / ".claude/agents/backend.md"
    assert agent.is_symlink()

    _write(m.manifest_path(), "{not json")
    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    # Fail closed: an untrustworthy manifest deletes nothing this run...
    assert agent.is_symlink()
    # ...but a fresh, valid manifest is written so the next run prunes normally.
    import json as _json

    assert _json.loads(m.manifest_path().read_text())["version"] == 1
    # Tracking resumes on the next run that deploys, so a later retirement prunes.
    m.do_global_flow(cfg, _quiet(m))
    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    assert not agent.is_symlink()


def test_preexisting_settings_are_never_adopted_or_deleted(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    _write(home / ".claude/settings.json", '{"mine": true}\n')
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": []}
    m.do_global_flow(cfg, _quiet(m))
    import json as _json

    recorded = _json.loads(m.manifest_path().read_text())["paths"]
    assert str(home / ".claude/settings.json") not in recorded

    # A settings file we DID create is recorded, survives the user editing it,
    # and keeps its original deployment hash rather than the mutated bytes.
    home2 = tmp_path / "home2"
    _patch_home(m, home2)
    m.do_global_flow(cfg, _quiet(m))
    settings = home2 / ".claude/settings.json"
    original = _json.loads(m.manifest_path().read_text())["paths"][str(settings)]
    assert original["kind"] == "settings"
    settings.write_text('{"edited": true}\n')
    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    assert settings.read_text() == '{"edited": true}\n'
    after = _json.loads(m.manifest_path().read_text())["paths"][str(settings)]
    assert after["sha256"] == original["sha256"]


def test_generated_sources_survive_a_run_with_only_foreign_targets(
    tmp_path: Path,
) -> None:
    """Retention follows the DESIRED recipe set, not successful deployment, so a
    run whose home targets are all foreign does not churn the generated tree."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    # A divergent real dir at every home skill target: nothing can be deployed.
    _write(home / ".claude/skills/python/SKILL.md", "mine\n")
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": []}
    m.do_global_flow(cfg, _quiet(m))
    generated = home / ".shared-llm/generated/skills/python"
    assert generated.is_dir()
    m.do_global_flow(cfg, _quiet(m))
    assert generated.is_dir()


# --- destination vs global ownership ---------------------------------------


def _add_common_skill(dest: Path, name: str) -> None:
    """A repo-owned common-scope skill under an arbitrary name, so a destination
    can want a name the global home flow also deploys."""
    s = dest / ".shared-llm"
    _write(s / f"this_repo/layers/skills/this_repo/{name}.md", f"{name} body\n")
    _write(
        s / f"this_repo/compose/skills/{name}.yaml",
        yaml.safe_dump(
            {
                "type": "skill",
                "name": name,
                "description": f".shared-llm/this_repo/layers/skills/this_repo/{name}.md",
                "inputs": [f".shared-llm/this_repo/layers/skills/this_repo/{name}.md"],
                "output": f".claude/skills/{name}/SKILL.md",
            },
            sort_keys=False,
        ),
    )


@pytest.mark.parametrize(
    "harness, skill_dir",
    [("pi", ".pi/agent/skills"), ("codex", ".agents/skills")],
)
def test_destination_takes_over_a_name_the_global_flow_deployed(
    tmp_path: Path, harness: str, skill_dir: str
) -> None:
    """A destination that wants a skill name the global flow currently owns takes
    it (destination wins), in ONE update — the generated link must not be judged
    foreign by the link step and then deleted as stale by the global step, which
    left the name with no link at all until a second update."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    target = home / skill_dir / "python"

    # A global run owns the name, pointing into the generated tree.
    m.do_global_flow(
        {"source": str(m.DEFAULT_SOURCE), "global": [harness], "destinations": []},
        _quiet(m),
    )
    assert target.is_symlink()
    assert target.resolve().is_relative_to((home / ".shared-llm/generated").resolve())

    # Now a destination wants its own `python`, and global is emptied.
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    _add_common_skill(dest, "python")
    cfg = _cfg(m, dest, [harness])
    log = _quiet(m)
    m.do_compose(cfg, log)
    # Exactly what cmd_update does: both home-link steps under one lock.
    with m.home_lock(log):
        m.do_link(cfg, log)
        m.do_global_flow(cfg, log, lock_held=True)

    assert target.is_symlink(), "the destination's skill must still be linked"
    assert target.resolve() == (dest / ".claude/skills/python").resolve()
    # The retired generated source is gone, and nothing points at it.
    assert not (home / ".shared-llm/generated/skills/python").exists()


def test_disabling_a_harness_retires_its_whole_generated_pieces(
    tmp_path: Path,
) -> None:
    """Emptying `global:` retires the whole pieces too — hooks, statusline, Pi
    extensions and personas, herdr config — not just skills and agents."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc", "pi"], "destinations": []}
    m.do_global_flow(cfg, _quiet(m))

    gen = home / ".shared-llm/generated"
    pieces = [
        gen / "claude/hooks",
        gen / "claude/statusline.sh",
        gen / "pi/extensions",
        gen / "pi/agents",
        gen / "herdr-config.toml",
    ]
    assert all(p.exists() for p in pieces), "baseline: every whole piece deployed"

    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    for p in pieces:
        if p.is_dir():
            assert sorted(p.iterdir()) == [], f"{p} still holds generated sources"
        else:
            assert not p.exists(), f"{p} survived the retirement"
    # And no home link survives pointing at any of it.
    for base in (home / ".claude", home / ".pi", home / ".config"):
        if not base.is_dir():
            continue
        for entry in base.rglob("*"):
            assert not entry.is_symlink(), f"{entry} should have been pruned"
