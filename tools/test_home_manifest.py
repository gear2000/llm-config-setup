"""Regression tests for the home-deployment contracts: the durable generated
tree, the ~/.shared-llm/manifest.json ownership record, and the single global
flow every entry point (`update`, `global`, `prune`) routes through.

These pin the safety rules the redesign turns on: a file the kit did not create
is never adopted and never deleted; ownership is decided on a RESOLVED path, not
a substring; a retired recipe's deployment (home link, generated source, manifest
entry) disappears on the next run; a manifest we cannot trust prunes NOTHING; and
a byte-identical legacy copy is upgraded to a symlink while a divergent one is
preserved.

HOME is redirected to a tmp dir in every test, and the tests that need to retire
a recipe run against a COPY of the kit, so neither the real home nor the checkout
is ever touched.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

TOOLS = Path(__file__).resolve().parent
HARNESS = TOOLS / "harness.py"
KIT = TOOLS.parent


def _load():
    spec = importlib.util.spec_from_file_location("harness_manifest_under_test", HARNESS)
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


def _kit_copy(m, tmp_path: Path) -> Path:
    """A throwaway copy of the kit's compose sources, so a test may retire a
    recipe without mutating the checkout. project_root() is patched to it."""
    kit = tmp_path / "llm-config-setup"
    shutil.copytree(KIT / ".shared-llm", kit / ".shared-llm")
    shutil.copy2(KIT / "herdr-config.toml", kit / "herdr-config.toml")
    m.__dict__["project_root"] = lambda: kit
    return kit


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _quiet(m):
    return m.RunLog(verbose=False)


def _cfg(m, harnesses, destinations=None):
    return {
        "source": str(m.DEFAULT_SOURCE),
        "global": list(harnesses),
        "destinations": destinations or [],
    }


def _paths(m) -> dict:
    return json.loads(m.manifest_path().read_text())["paths"]


# --- entry points ----------------------------------------------------------


def _run_entry(m, entry: str, cfg: dict) -> None:
    """Drive the global flow the way a user would, through each CLI surface."""
    m.save_config(cfg)
    if entry == "global":
        m.cmd_global(argparse.Namespace())
    elif entry == "prune":
        argv = sys.argv
        sys.argv = ["harness.py", "prune"]
        try:
            m.main()
        finally:
            sys.argv = argv
    elif entry == "update":
        m.cmd_update(argparse.Namespace(verbose=False))
    else:  # pragma: no cover - test wiring error
        raise AssertionError(entry)


def _home_snapshot(m, home: Path) -> list[tuple[str, str]]:
    """Kind of every path under the home surfaces the global flow owns."""
    out: list[tuple[str, str]] = []
    roots = [
        home / ".claude",
        home / ".pi",
        home / ".agents",
        home / ".shared-llm/generated",
    ]
    for root in roots:
        if not root.exists():
            continue
        for entry in sorted(root.rglob("*")):
            kind = (
                "link"
                if entry.is_symlink()
                else "dir"
                if entry.is_dir()
                else "file"
            )
            out.append((str(entry.relative_to(home)), kind))
    return sorted(out)


def _seed_dest(dest: Path) -> dict:
    """A cc-only destination: `update` accepts the config, and the link step
    touches no home path, so the home surface stays comparable to `global`."""
    (dest / ".shared-llm").mkdir(parents=True, exist_ok=True)
    return {
        "path": str(dest),
        "harnesses": ["cc"],
        "placeholders": {"OPS_REPO": "your-repo-ops"},
    }


def test_every_entry_point_prunes_identically_when_global_is_emptied(
    tmp_path: Path,
) -> None:
    """update / global / prune all route through the one global flow, so an
    emptied `global:` list leaves byte-identical home state whichever is used."""
    m = _load()
    snapshots = {}
    for entry in ("update", "global", "prune"):
        home = tmp_path / f"home-{entry}"
        _patch_home(m, home)
        dest = tmp_path / f"dest-{entry}"
        deployed = _cfg(m, ["cc", "pi"], [_seed_dest(dest)])
        _run_entry(m, "global", deployed)
        assert (home / ".claude/agents/backend.md").is_symlink()

        _run_entry(m, entry, {**deployed, "global": []})
        snapshots[entry] = _home_snapshot(m, home)
        # Whatever was deployed is gone; the mutable settings file is not.
        assert not (home / ".claude/agents/backend.md").is_symlink()
        assert not (home / ".claude/skills/python").exists()
        assert (home / ".claude/settings.json").is_file()

    assert snapshots["update"] == snapshots["global"] == snapshots["prune"]


def test_foreign_settings_survive_every_entry_point(tmp_path: Path) -> None:
    """A settings.json the user already had is never adopted into the manifest
    and never deleted — through each entry point, deploying and un-deploying."""
    m = _load()
    for entry in ("update", "global", "prune"):
        home = tmp_path / f"home-{entry}"
        _patch_home(m, home)
        mine = home / ".claude/settings.json"
        _write(mine, '{"mine": true}\n')
        dest = tmp_path / f"dest-{entry}"
        deployed = _cfg(m, ["cc"], [_seed_dest(dest)])

        _run_entry(m, "global", deployed)
        assert str(mine) not in _paths(m), "pre-existing settings must not be adopted"
        assert mine.read_text() == '{"mine": true}\n'

        _run_entry(m, entry, {**deployed, "global": []})
        assert mine.is_file() and not mine.is_symlink()
        assert mine.read_text() == '{"mine": true}\n'
        assert str(mine) not in _paths(m)


# --- ownership -------------------------------------------------------------


def test_foreign_link_spelling_the_marker_is_not_ours(tmp_path: Path) -> None:
    """A link whose target merely CONTAINS `/.shared-llm/generated/` under some
    other root is foreign: never repointed, never pruned."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    decoy = tmp_path / "foreign/.shared-llm/generated/python"
    _write(decoy / "SKILL.md", "foreign skill\n")
    target = home / ".claude/skills/python"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(decoy)

    assert m._link_points_generated(target) is False

    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))
    assert target.is_symlink() and target.resolve() == decoy.resolve()
    assert str(target) not in _paths(m), "a skipped foreign target is not recorded"

    # And an emptied global list does not prune it either.
    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    assert target.is_symlink() and target.resolve() == decoy.resolve()
    assert (decoy / "SKILL.md").read_text() == "foreign skill\n"


def test_link_into_the_kit_checkout_is_repointed_to_generated(tmp_path: Path) -> None:
    """An old-style link straight into the repo checkout is ours, so it is
    migrated onto the durable generated copy."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = _kit_copy(m, tmp_path)
    stale_source = kit / ".shared-llm/public/llm/pi/common/agents/doc-reviewer.md"
    assert stale_source.is_file()
    target = home / ".claude/agents/backend.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(stale_source)

    m.do_global_flow(_cfg(m, ["cc"]), _quiet(m))

    generated = home / ".shared-llm/generated/agents/backend.md"
    assert target.is_symlink() and target.resolve() == generated.resolve()


# --- retirement ------------------------------------------------------------


def test_retired_skill_recipe_removes_link_generated_and_manifest(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    _kit_copy(m, tmp_path)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))

    link = home / ".claude/skills/python"
    generated = home / ".shared-llm/generated/skills/python"
    assert link.is_symlink() and generated.is_dir()
    assert str(link) in _paths(m)

    # Retire the recipe (a rename is the same thing from the flow's side).
    m.GLOBAL_CONVENTION_SKILLS = {
        k: v for k, v in m.GLOBAL_CONVENTION_SKILLS.items() if k != "python"
    }
    m.do_global_flow(cfg, _quiet(m))

    assert not link.exists() and not link.is_symlink()
    assert not generated.exists()
    assert str(link) not in _paths(m)
    # Its siblings are untouched.
    assert (home / ".claude/skills/golang").is_symlink()


def test_retired_agent_is_pruned_and_not_resurrected_from_staging(
    tmp_path: Path,
) -> None:
    """Fresh-staging contract: a stale .md left in the compose staging dir by an
    earlier run must not be redeployed once its recipe is gone, and the home
    link the earlier run made is pruned."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = _kit_copy(m, tmp_path)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))

    link = home / ".claude/agents/backend.md"
    generated = home / ".shared-llm/generated/agents/backend.md"
    staged = kit / "examples/.claude/agents/backend.md"
    assert link.is_symlink() and generated.is_file() and staged.is_file()

    (kit / ".shared-llm/public/compose/agents/backend.yaml").unlink()
    assert staged.is_file(), "staging is cumulative — the stale output is still there"

    m.do_global_flow(cfg, _quiet(m))

    assert not staged.exists(), "staging must be cleared before compose"
    assert not link.exists() and not link.is_symlink()
    assert not generated.exists()
    assert str(link) not in _paths(m)
    assert (home / ".claude/agents/qa.md").is_symlink()


# --- corrupted manifest ----------------------------------------------------


def _assert_prunes_nothing(m, home: Path, cfg: dict, capsys) -> None:
    link = home / ".claude/agents/backend.md"
    skill = home / ".claude/skills/python"
    generated = home / ".shared-llm/generated/skills/python"
    assert link.is_symlink() and skill.is_symlink() and generated.is_dir()

    m.do_global_flow({**cfg, "global": []}, _quiet(m))

    assert link.is_symlink(), "a distrusted manifest must delete nothing"
    assert skill.is_symlink()
    assert generated.is_dir(), "generated orphans survive too"
    out = capsys.readouterr().out
    assert "PRUNING NOTHING" in out, "the refusal must be logged loudly"
    # A fresh, valid manifest is written so the next run prunes normally.
    assert json.loads(m.manifest_path().read_text())["version"] == 1


def test_truncated_manifest_prunes_nothing(tmp_path: Path, capsys) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))
    text = m.manifest_path().read_text()
    m.manifest_path().write_text(text[: len(text) // 2])
    _assert_prunes_nothing(m, home, cfg, capsys)


def test_wrong_schema_manifest_prunes_nothing(tmp_path: Path, capsys) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))
    m.manifest_path().write_text(json.dumps({"version": 99, "paths": {}}))
    _assert_prunes_nothing(m, home, cfg, capsys)


def test_manifest_with_bad_path_entries_prunes_nothing(tmp_path: Path, capsys) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))
    m.manifest_path().write_text(
        json.dumps({"version": 1, "paths": {"/some/path": "not-a-dict"}})
    )
    _assert_prunes_nothing(m, home, cfg, capsys)


# --- scaffolded settings ---------------------------------------------------


def test_scaffolded_settings_keep_deployment_hash_and_are_never_deleted(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))

    settings = home / ".claude/settings.json"
    first = _paths(m)[str(settings)]
    assert first["kind"] == "settings"

    settings.write_text('{"hand-edited": true}\n')
    m.do_global_flow(cfg, _quiet(m))
    assert _paths(m)[str(settings)]["sha256"] == first["sha256"], (
        "re-hashing would launder the user's edit into 'what we deployed'"
    )
    assert settings.read_text() == '{"hand-edited": true}\n'

    # Dropping cc from the global list must not delete the mutable file.
    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    assert settings.read_text() == '{"hand-edited": true}\n'
    assert _paths(m)[str(settings)]["sha256"] == first["sha256"]


# --- legacy copy migration -------------------------------------------------


def test_identical_legacy_copies_migrate_and_divergent_ones_are_preserved(
    tmp_path: Path,
) -> None:
    """The old mechanism wrote real dirs/files into home. A byte-identical one
    is upgraded to a symlink; a divergent one is left alone — and finalize does
    not delete it either."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))

    skill = home / ".claude/skills/python"
    agent = home / ".claude/agents/backend.md"
    other = home / ".claude/skills/golang"
    gen_skill = skill.resolve()
    gen_agent = agent.resolve()

    # Replace the links with real copies: one identical, one divergent.
    skill.unlink()
    shutil.copytree(gen_skill, skill)
    agent.unlink()
    shutil.copy2(gen_agent, agent)
    other.unlink()
    _write(other / "SKILL.md", "my own golang notes\n")
    divergent_agent = home / ".claude/agents/qa.md"
    divergent_agent.unlink()
    _write(divergent_agent, "my own qa persona\n")

    m.do_global_flow(cfg, _quiet(m))

    assert skill.is_symlink() and skill.resolve() == gen_skill
    assert agent.is_symlink() and agent.resolve() == gen_agent
    assert not other.is_symlink() and (other / "SKILL.md").read_text() == (
        "my own golang notes\n"
    )
    assert not divergent_agent.is_symlink()
    assert divergent_agent.read_text() == "my own qa persona\n"

    # Un-deploying everything still refuses to delete the divergent real paths.
    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    assert (other / "SKILL.md").read_text() == "my own golang notes\n"
    assert divergent_agent.read_text() == "my own qa persona\n"
    assert not skill.exists() and not agent.exists()


# --- generated header ------------------------------------------------------


def test_composed_claude_md_and_agents_md_carry_the_generated_header(
    tmp_path: Path,
) -> None:
    m = _load()
    root = tmp_path / "repo"
    _write(root / ".shared-llm/layers/llm/common/intro.md", "Intro body.\n")
    for ctype, out in (("claude-md", "CLAUDE.md"), ("agents-md", "AGENTS.md")):
        _write(
            root / f".shared-llm/compose/{ctype}.yaml",
            yaml.safe_dump(
                {
                    "type": ctype,
                    "inputs": [".shared-llm/layers/llm/common/intro.md"],
                    "output": out,
                },
                sort_keys=False,
            ),
        )
    composer = m.Composer(root, output_base=root, shared_root=root / ".shared-llm")
    composer.compose_dir(root / ".shared-llm/compose")

    for out in ("CLAUDE.md", "AGENTS.md"):
        text = (root / out).read_text()
        assert text.startswith(m.GENERATED_HEADER), f"{out} lacks the GENERATED header"
        assert "Intro body." in text


def _dangling_links(home: Path) -> list[Path]:
    out: list[Path] = []
    for rel in (".claude", ".pi", ".agents", ".config"):
        root = home / rel
        if not root.is_dir():
            continue
        for entry in root.rglob("*"):
            if entry.is_symlink() and not entry.exists():
                out.append(entry)
    return out


def test_corrupt_manifest_is_quarantined_and_ownership_is_rebuilt(
    tmp_path: Path,
) -> None:
    """Deploy, corrupt the manifest, then retire everything twice.

    The first run must delete nothing, but it must NOT forget the links already
    on disk: replacing the manifest with an empty record would let the second run
    prune the generated sources as orphans while the home links survive, leaving
    every one of them dangling."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = _cfg(m, ["cc"])
    m.do_global_flow(cfg, _quiet(m))
    agent = home / ".claude/agents/backend.md"
    skill = home / ".claude/skills/python"
    assert agent.is_symlink() and skill.is_symlink()

    m.manifest_path().write_text("{ this is not json")
    m.do_global_flow({**cfg, "global": []}, _quiet(m))

    # Nothing deleted, the unusable file kept for inspection, ownership rebuilt.
    assert agent.is_symlink() and skill.is_symlink()
    assert list(m.manifest_path().parent.glob("manifest.json.corrupt-*"))
    rebuilt = _paths(m)
    assert str(agent) in rebuilt and str(skill) in rebuilt

    m.do_global_flow({**cfg, "global": []}, _quiet(m))
    assert not agent.is_symlink() and not skill.is_symlink()
    assert _dangling_links(home) == [], "no link may survive its generated source"


# --- manifest schema is a deletion contract --------------------------------


def _manifest_is_rejected(m, entry_key: str, meta) -> bool:
    m.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    m.manifest_path().write_text(
        json.dumps({"version": 1, "paths": {entry_key: meta}})
    )
    return not m.HomeManifest().previous_ok


def test_manifest_only_accepts_paths_it_could_have_deployed(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    gen = str(home / ".shared-llm/generated/skills/x")
    good = str(home / ".claude/skills/x")

    assert not _manifest_is_rejected(m, good, {"kind": "link", "source": gen})

    rejected = {
        "outside every managed home root": (
            "/tmp/outside-home-link",
            {"kind": "link", "source": gen},
        ),
        "under HOME but not a managed root": (
            str(home / "Documents/x"),
            {"kind": "link", "source": gen},
        ),
        "relative destination": ("relative/path", {"kind": "link", "source": gen}),
        "unknown kind": (good, {"kind": "wat", "source": gen}),
        "link with no source": (good, {"kind": "link"}),
        "link source we never write": (
            good,
            {"kind": "link", "source": "/tmp/foreign-target"},
        ),
        # `file` was dropped when its last writer went away: an accepted kind
        # with no writer is a deletion path nothing exercises.
        "the retired file kind": (good, {"kind": "file", "sha256": "a" * 64}),
        "settings at a path that is not one of the two": (
            good,
            {"kind": "settings", "sha256": "a" * 64},
        ),
    }
    for label, (key, meta) in rejected.items():
        assert _manifest_is_rejected(m, key, meta), f"accepted {label}"


def test_a_manifest_naming_a_path_outside_home_deletes_nothing(tmp_path: Path) -> None:
    """The concrete danger the schema exists to stop: an arbitrary path recorded
    as a link must never become an unlink."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    victim_target = tmp_path / "victim-target"
    victim_target.write_text("someone else's file\n")
    victim = tmp_path / "outside-home-link"
    victim.symlink_to(victim_target)

    m.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    m.manifest_path().write_text(
        json.dumps(
            {
                "version": 1,
                "paths": {
                    str(victim): {"kind": "link", "source": str(victim_target)}
                },
            }
        )
    )
    m.do_global_flow(_cfg(m, []), _quiet(m))
    assert victim.is_symlink() and victim_target.is_file()


# --- failure atomicity -----------------------------------------------------


def test_a_failed_directory_swap_rolls_the_old_version_back(tmp_path: Path) -> None:
    """Between the two renames of a dir swap every home link dangles, so a failed
    second rename must put the old dir straight back."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    gen = home / ".shared-llm/generated/skills/demo"
    _write(gen / "SKILL.md", "old\n")
    staged = tmp_path / "staged/demo"
    _write(staged / "SKILL.md", "new\n")
    link = home / ".claude/skills/demo"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(gen)

    real_rename = m.os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the rename that moves the new version into place
            raise OSError("injected failure")
        return real_rename(src, dst)

    m.os.rename = flaky
    try:
        with pytest.raises(OSError):
            m._sync_generated_dir(staged, gen)
    finally:
        m.os.rename = real_rename

    assert gen.is_dir(), "the old version must be back where the links point"
    assert (gen / "SKILL.md").read_text() == "old\n"
    assert link.resolve() == gen.resolve() and link.exists()


def test_generated_sources_outlive_planning_and_die_only_in_the_commit_phase(
    tmp_path: Path,
) -> None:
    """Planning must not delete a retired generated source: its home link is
    still standing at that point, and an exception before reconciliation would
    strand the link dangling forever. The end-of-run commit phase drops it."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = _kit_copy(m, tmp_path)
    stray = home / ".shared-llm/generated/pi/extensions/retired.ts"
    _write(stray, "// retired\n")
    stray_herdr = home / ".shared-llm/generated/herdr-config.toml"
    _write(stray_herdr, "stale = true\n")
    (kit / "herdr-config.toml").unlink()

    m.plan_pi_runtime(kit)
    m.plan_herdr_config(kit)
    assert stray.is_file(), "planning must not delete a source a link may still use"
    assert stray_herdr.is_file()

    m.do_global_flow(_cfg(m, ["pi"]), _quiet(m))
    assert not stray.exists() and not stray_herdr.exists()


# --- modes are part of identity --------------------------------------------


def test_mode_only_drift_is_resynced(tmp_path: Path) -> None:
    """Home runs hooks THROUGH the generated copy, so a hook that lost its
    executable bit is broken even though its bytes are right."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    src = tmp_path / "hook.sh"
    src.write_text("#!/bin/sh\necho hi\n")
    src.chmod(0o755)
    gen = home / ".shared-llm/generated/claude/hooks/hook.sh"
    _write(gen, "#!/bin/sh\necho hi\n")
    gen.chmod(0o644)

    m._sync_generated_file(src, gen)
    assert gen.stat().st_mode & 0o777 == 0o755

    # Directory comparison sees it too, so a skill dir is not "equal" on bytes.
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / "run.sh", "x\n")
    _write(b / "run.sh", "x\n")
    (a / "run.sh").chmod(0o755)
    (b / "run.sh").chmod(0o644)
    assert m._dirs_equal(a, b) is False


def test_a_real_file_with_a_different_mode_is_not_adopted(tmp_path: Path) -> None:
    """A copy the user chmod-ed is a deliberate local change, not the stale copy
    the old mechanism left behind — it is preserved, not replaced by a link."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    source = tmp_path / "source.md"
    source.write_text("body\n")
    source.chmod(0o644)
    target = home / ".claude/agents/thing.md"
    _write(target, "body\n")
    target.chmod(0o700)

    assert m._link_file(source, target, _quiet(m)) == "skip"
    assert not target.is_symlink() and target.read_text() == "body\n"


# --- settings are never written through ------------------------------------


@pytest.mark.parametrize(
    "rel, harness",
    [(".claude/settings.json", "cc"), (".pi/agent/settings.json", "pi")],
)
def test_a_dangling_settings_symlink_is_preserved(
    tmp_path: Path, rel: str, harness: str, capsys
) -> None:
    """exists() is False for a dangling symlink, so treating it as absent would
    copy straight through the link and create its target."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    settings = home / rel
    settings.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere/settings.json"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    settings.symlink_to(elsewhere)

    m.do_global_flow(_cfg(m, [harness]), _quiet(m))

    assert settings.is_symlink() and not elsewhere.exists(), (
        "the kit must not write through a symlink it did not create"
    )
    assert "is a symlink" in capsys.readouterr().out
    assert str(settings) not in _paths(m), "never adopted"


# --- the generated tree is never reached through a symlink ------------------


@pytest.mark.parametrize(
    "rel", ["", "skills", "claude/hooks", "pi/extensions"]
)
def test_pruning_refuses_to_follow_a_symlinked_generated_path(
    tmp_path: Path, rel: str, capsys
) -> None:
    """A symlinked generated root or namespace points at someone else's
    directory. Following it would enumerate — and empty — their files while the
    symlink itself survived."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    outside = tmp_path / "not-ours"
    outside.mkdir()
    _write(outside / "precious.md", "handwritten, not generated\n")

    hijacked = home / ".shared-llm/generated" / rel if rel else home / ".shared-llm/generated"
    hijacked.parent.mkdir(parents=True, exist_ok=True)
    hijacked.symlink_to(outside)

    m.do_global_flow(_cfg(m, []), _quiet(m))

    assert (outside / "precious.md").read_text() == "handwritten, not generated\n"
    assert hijacked.is_symlink(), "the foreign link itself is left in place"
    assert "refusing to prune" in capsys.readouterr().out


def test_syncing_refuses_to_write_through_a_symlinked_namespace(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    outside = tmp_path / "not-ours"
    outside.mkdir()
    gen_skills = home / ".shared-llm/generated/skills"
    gen_skills.parent.mkdir(parents=True, exist_ok=True)
    gen_skills.symlink_to(outside)
    staged = tmp_path / "staged/demo"
    _write(staged / "SKILL.md", "x\n")

    with pytest.raises(m.GeneratedTreeError):
        m._sync_generated_dir(staged, gen_skills / "demo")
    assert list(outside.iterdir()) == []


# --- an interrupted run must not become a dangling link ---------------------


def test_a_source_a_live_link_still_uses_is_never_deleted(tmp_path: Path) -> None:
    """The manifest is written AFTER the links, so a run interrupted in between
    leaves live links nothing recorded. Cleanup scans the disk for those links,
    so it cannot delete their sources out from under them."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    m.do_global_flow(_cfg(m, ["cc"]), _quiet(m))

    # Exactly the state an interrupted deployment leaves: link and source on
    # disk, manifest with no record of either.
    orphan_src = home / ".shared-llm/generated/skills/interrupted"
    _write(orphan_src / "SKILL.md", "---\nname: interrupted\n---\n")
    orphan_link = home / ".claude/skills/interrupted"
    orphan_link.symlink_to(orphan_src)

    m.do_global_flow(_cfg(m, []), _quiet(m))

    assert orphan_link.is_symlink() and orphan_link.exists(), "must not dangle"
    assert (orphan_src / "SKILL.md").is_file()
    assert _dangling_links(home) == []


# --- control files are opened without following symlinks --------------------


def test_the_lock_file_is_not_opened_through_a_symlink(tmp_path: Path) -> None:
    """Opening the predictable lock path the obvious way follows a planted
    symlink and TRUNCATES its target before any locking happens."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    victim = tmp_path / "victim.txt"
    victim.write_text("important\n")
    lock = home / m.LOCK_PATH_REL
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.symlink_to(victim)

    with pytest.raises(SystemExit):
        with m.home_lock(_quiet(m)):
            pass
    assert victim.read_text() == "important\n"


def test_the_manifest_temp_file_is_not_predictable(tmp_path: Path) -> None:
    """A symlink planted at the old, guessable temp name redirected the write
    and was then renamed onto manifest.json."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    victim = tmp_path / "victim.json"
    victim.write_text("important\n")
    m.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    (m.manifest_path().parent / ".tmp-manifest.json").symlink_to(victim)

    m.do_global_flow(_cfg(m, ["cc"]), _quiet(m))

    assert victim.read_text() == "important\n"
    assert m.manifest_path().is_file() and not m.manifest_path().is_symlink()
    assert json.loads(m.manifest_path().read_text())["version"] == 1


# --- traversal spellings are not valid manifest keys ------------------------


def test_a_traversal_spelling_is_rejected_and_deletes_nothing(tmp_path: Path) -> None:
    """`/home/u/.claude/../../victim` passes a LEXICAL containment check against
    /home/u/.claude while the filesystem resolves it somewhere else."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    victim_target = tmp_path / "victim-target"
    victim_target.write_text("someone else's file\n")
    victim = tmp_path / "victim-link"
    victim.symlink_to(victim_target)

    traversal = f"{home}/.claude/../../{victim.name}"
    assert Path(traversal).resolve() == victim.resolve()
    m.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    m.manifest_path().write_text(
        json.dumps(
            {
                "version": 1,
                "paths": {
                    traversal: {
                        "kind": "link",
                        "source": str(home / ".shared-llm/generated/skills/x"),
                    }
                },
            }
        )
    )
    assert m.HomeManifest().previous_ok is False

    m.do_global_flow(_cfg(m, []), _quiet(m))
    assert victim.is_symlink() and victim_target.is_file()


# --- legacy ownership is containment, not substrings ------------------------


def test_a_decoy_path_spelling_the_family_and_marker_is_foreign(
    tmp_path: Path,
) -> None:
    """A backup copy of the kit elsewhere on disk spells both the repo-family
    token and a managed marker; substring matching called that ours and unlinked
    it."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = _kit_copy(m, tmp_path)
    family = m.repo_family(kit)

    decoy_src = (
        tmp_path
        / f"foreign-{family}-backup/.shared-llm/public/llm/pi/common/extensions/x.ts"
    )
    _write(decoy_src, "// someone else's extension\n")
    live = home / ".pi/agent/extensions/x.ts"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.symlink_to(decoy_src)
    dangling = home / ".pi/agent/extensions/gone.ts"
    dangling.symlink_to(decoy_src.with_name("gone.ts"))

    assert m.link_is_ours(live, family, kit) is False
    assert m.link_is_ours(dangling, family, kit) is False

    m.reconcile(
        m.LinkPlan({}, [home / ".pi/agent/extensions"]),
        family,
        plan_only=False,
        force=False,
        repo_root=kit,
    )
    assert live.is_symlink() and dangling.is_symlink(), "foreign links survive"


# --- update retires an emptied configuration --------------------------------


def test_update_with_an_emptied_config_still_prunes(tmp_path: Path) -> None:
    """Removing the last destination and emptying `global:` is a RETIREMENT, not
    a no-op: the headline command must still take the lock and clean up."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    m.save_config(_cfg(m, ["cc"]))
    m.cmd_update(argparse.Namespace(verbose=False))
    agent = home / ".claude/agents/backend.md"
    assert agent.is_symlink()

    m.save_config(_cfg(m, []))
    m.cmd_update(argparse.Namespace(verbose=False))
    assert not agent.is_symlink() and not agent.exists()
    assert _dangling_links(home) == []


def test_update_on_a_machine_that_never_deployed_still_errors(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    m.save_config(_cfg(m, []))
    with pytest.raises(SystemExit):
        m.cmd_update(argparse.Namespace(verbose=False))


# --- reconstruction records what its own schema accepts ---------------------


@pytest.mark.parametrize("dangling", [False, True])
def test_reconstruction_records_relative_links_absolutely(
    tmp_path: Path, dangling: bool
) -> None:
    """A relative literal recorded verbatim is rejected by our own schema on the
    next run, so recovery would quarantine forever instead of progressing."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    m.do_global_flow(_cfg(m, ["cc"]), _quiet(m))

    source = home / ".shared-llm/generated/skills/relative-demo"
    if not dangling:
        _write(source / "SKILL.md", "---\nname: relative-demo\n---\n")
    link = home / ".claude/skills/relative-demo"
    link.symlink_to(Path(os.path.relpath(source, link.parent)))
    assert not os.path.isabs(os.readlink(link))

    m.manifest_path().write_text("{ corrupt")
    m.do_global_flow(_cfg(m, []), _quiet(m))

    recorded = _paths(m).get(str(link))
    assert recorded, "the live relative link must be re-adopted"
    assert recorded["source"] == str(source), "recorded absolute, not relative"
    # The proof it matters: the next run trusts the manifest instead of
    # quarantining again, and retires the link cleanly.
    assert m.HomeManifest().previous_ok is True
    m.do_global_flow(_cfg(m, []), _quiet(m))
    assert not link.is_symlink()


# --- every swap type rolls back ---------------------------------------------


@pytest.mark.parametrize(
    "old_kind, new_is_dir",
    [("dir", True), ("file", True), ("symlink", True), ("dir", False), ("file", False)],
)
def test_every_swap_shape_rolls_back_on_failure(
    tmp_path: Path, old_kind: str, new_is_dir: bool
) -> None:
    """Whatever the old artifact is, it must be renamed aside rather than
    unlinked: unlinking first makes a failed rename unrecoverable."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    gen = home / ".shared-llm/generated/pi/extensions/thing"
    gen.parent.mkdir(parents=True, exist_ok=True)
    if old_kind == "dir":
        _write(gen / "inner.ts", "old\n")
    elif old_kind == "file":
        gen.write_text("old\n")
    else:
        gen.symlink_to(tmp_path / "old-target")

    new = tmp_path / "new"
    if new_is_dir:
        _write(new / "inner.ts", "new\n")
    else:
        new.write_text("new\n")

    real_rename, real_replace = m.os.rename, m.os.replace

    def boom(src, dst):
        if Path(src) == new:
            raise OSError("injected failure")
        return real_rename(src, dst)

    m.os.rename, m.os.replace = boom, boom
    try:
        with pytest.raises(OSError):
            m._swap_in(new, gen)
    finally:
        m.os.rename, m.os.replace = real_rename, real_replace

    assert gen.exists() or gen.is_symlink(), "the old artifact must be back"
    if old_kind == "dir":
        assert (gen / "inner.ts").read_text() == "old\n"
    elif old_kind == "file":
        assert gen.read_text() == "old\n"
    else:
        assert gen.is_symlink() and os.readlink(gen) == str(tmp_path / "old-target")


# --- directory equality is a deletion proof, so it reads bytes --------------


def _same_stat_different_bytes(a: Path, b: Path) -> None:
    """Two files a shallow comparison calls identical: same size, same mtime,
    same mode — different contents."""
    _write(a / "SKILL.md", "AAAA\n")
    _write(b / "SKILL.md", "BBBB\n")
    for p in (a / "SKILL.md", b / "SKILL.md"):
        p.chmod(0o644)
        os.utime(p, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))


def test_same_size_and_mtime_but_different_bytes_is_not_equal(tmp_path: Path) -> None:
    m = _load()
    a, b = tmp_path / "generated", tmp_path / "mine"
    _same_stat_different_bytes(a, b)
    assert m._dirs_equal(a, b) is False


def test_a_divergent_real_skill_survives_migration_and_pruning(tmp_path: Path) -> None:
    """`_dirs_equal` is what authorises `rmtree` of a real dir, so a file that
    only LOOKS identical by stat must not authorise anything."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    generated = home / ".shared-llm/generated/skills/demo"
    target = home / ".claude/skills/demo"
    _same_stat_different_bytes(generated, target)

    assert m._link_skill_dir(generated, target, _quiet(m)) == "skip"
    assert not target.is_symlink()
    assert (target / "SKILL.md").read_text() == "BBBB\n"

    removed = m._prune_stale_skill(
        "demo", generated, set(), {"cc": home / ".claude/skills"}, _quiet(m)
    )
    assert removed == 0
    assert (target / "SKILL.md").read_text() == "BBBB\n"


# --- a retired name is not a licence to delete ------------------------------


def test_a_divergent_deprecated_skill_is_preserved(tmp_path: Path, capsys) -> None:
    """Every independently authored skill at that path carries that same
    frontmatter name, so the name proves nothing about who wrote it."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    name = m.DEPRECATED_GLOBAL_SKILLS[0]
    mine = home / ".claude/skills" / name
    _write(mine / "SKILL.md", f"---\nname: {name}\n---\nmy own version\n")
    _write(mine / "notes.txt", "my notes\n")

    removed = m._prune_deprecated_global({"cc": home / ".claude/skills"}, _quiet(m))

    assert removed == 0
    assert (mine / "SKILL.md").read_text().endswith("my own version\n")
    assert (mine / "notes.txt").is_file()
    assert "PRESERVED" in capsys.readouterr().out


def test_a_deprecated_deployment_we_own_is_removed(tmp_path: Path) -> None:
    """The other half: a link we own, and a copy identical to what we shipped,
    are still cleaned up."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    name = m.DEPRECATED_GLOBAL_SKILLS[0]
    generated = home / ".shared-llm/generated/skills" / name
    _write(generated / "SKILL.md", f"---\nname: {name}\n---\nkit version\n")

    linked = home / ".claude/skills" / name
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(generated)
    copied = home / ".pi/agent/skills" / name
    copied.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(generated, copied)

    removed = m._prune_deprecated_global(
        {"cc": home / ".claude/skills", "pi": home / ".pi/agent/skills"}, _quiet(m)
    )
    assert removed == 2
    assert not linked.exists() and not linked.is_symlink()
    assert not copied.exists()


# --- destination links are retired too --------------------------------------


def _dest_with_skill(dest: Path, name: str) -> dict:
    """A destination repo offering one common-scope skill of its own."""
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
    _write(dest / f".claude/skills/{name}/SKILL.md", f"---\nname: {name}\n---\n")
    return {"path": str(dest), "harnesses": ["pi", "codex"]}


def _locked_update(m, cfg: dict) -> None:
    """Exactly what cmd_update does: both home-link steps under one lock, and one
    manifest transaction spanning them."""
    log = _quiet(m)
    with m.home_lock(log):
        manifest = m.HomeManifest()
        m.do_link(cfg, log, manifest)
        m.do_global_flow(cfg, log, lock_held=True, manifest=manifest)


@pytest.mark.parametrize("skill_dir", [".pi/agent/skills", ".agents/skills"])
def test_removing_the_last_destination_retires_its_home_links(
    tmp_path: Path, skill_dir: str
) -> None:
    """With the destination gone from the config, its root is no longer proof of
    anything and the link step is not called for that harness — the manifest
    record is the only remaining evidence that we made the link."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    cfg = _cfg(m, [], [_dest_with_skill(dest, "custom")])
    _locked_update(m, cfg)

    link = home / skill_dir / "custom"
    assert link.is_symlink() and link.resolve() == (dest / ".claude/skills/custom")
    assert _paths(m)[str(link)]["kind"] == "repo-link"

    _locked_update(m, _cfg(m, [], []))
    assert not link.is_symlink() and not link.exists()
    assert _dangling_links(home) == []


def test_removing_one_of_several_destinations_retires_only_its_links(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    keep = _dest_with_skill(tmp_path / "keep", "kept-skill")
    drop = _dest_with_skill(tmp_path / "drop", "dropped-skill")
    _locked_update(m, _cfg(m, [], [keep, drop]))

    kept = home / ".pi/agent/skills/kept-skill"
    dropped = home / ".pi/agent/skills/dropped-skill"
    assert kept.is_symlink() and dropped.is_symlink()

    _locked_update(m, _cfg(m, [], [keep]))
    assert kept.is_symlink(), "the surviving destination keeps its link"
    assert not dropped.is_symlink() and not dropped.exists()


def test_a_destination_dropping_a_harness_retires_that_harness_links(
    tmp_path: Path,
) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    entry = _dest_with_skill(dest, "custom")
    _locked_update(m, _cfg(m, [], [entry]))
    assert (home / ".agents/skills/custom").is_symlink()

    _locked_update(m, _cfg(m, [], [{**entry, "harnesses": ["pi"]}]))
    assert (home / ".pi/agent/skills/custom").is_symlink(), "pi is still wanted"
    assert not (home / ".agents/skills/custom").is_symlink(), "codex was dropped"


def test_an_unrecorded_link_into_a_live_destination_is_still_retired(
    tmp_path: Path,
) -> None:
    """The upgrade path: links deployed before this version exist with no
    manifest record at all. When their destination is still configured but has
    dropped the harness, the reconcile pass is the only thing that can retire
    them — so it must run for both dirs even with nothing desired."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    entry = _dest_with_skill(dest, "custom")

    # Deployed by an older run: link on disk, nothing in the manifest.
    stale = home / ".agents/skills/custom"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.symlink_to(dest / ".claude/skills/custom")
    assert not m.manifest_path().exists()

    # The destination is still configured, but codex is no longer one of its
    # harnesses, so nothing desires that link any more.
    _locked_update(m, _cfg(m, [], [{**entry, "harnesses": ["pi"]}]))

    assert not stale.is_symlink() and not stale.exists()
    assert (home / ".pi/agent/skills/custom").is_symlink(), "pi is untouched"


def test_a_retired_destination_hands_the_name_back_to_the_global_skill(
    tmp_path: Path,
) -> None:
    """The name must come back in the SAME run, not be left unlinked until the
    next one."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    entry = _dest_with_skill(dest, "python")
    _locked_update(m, _cfg(m, ["pi"], [entry]))
    link = home / ".pi/agent/skills/python"
    assert link.resolve() == (dest / ".claude/skills/python")

    _locked_update(m, _cfg(m, ["pi"], []))
    assert link.is_symlink()
    assert link.resolve() == (home / ".shared-llm/generated/skills/python").resolve()


def test_a_repo_link_record_only_deletes_a_link_that_still_matches(
    tmp_path: Path,
) -> None:
    """The recorded source is the deletion proof: if the user has repointed the
    link since, it is no longer ours to remove."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _locked_update(m, _cfg(m, [], [_dest_with_skill(dest, "custom")]))
    link = home / ".pi/agent/skills/custom"

    elsewhere = tmp_path / "my-own/custom"
    _write(elsewhere / "SKILL.md", "mine\n")
    link.unlink()
    link.symlink_to(elsewhere)

    _locked_update(m, _cfg(m, [], []))
    assert link.is_symlink() and link.resolve() == elsewhere.resolve()


# --- containment alone is not ownership -------------------------------------


@pytest.mark.parametrize("dangling", [False, True])
def test_a_custom_link_to_an_unrelated_checkout_file_is_foreign(
    tmp_path: Path, dangling: bool
) -> None:
    """A user's own link to a README or script inside the checkout is contained
    in it, but lands on no managed sub-path — unlinking it would destroy a live
    configuration this tool never created."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = _kit_copy(m, tmp_path)
    unrelated = kit / "README.md"
    if not dangling:
        _write(unrelated, "# the kit's readme\n")

    custom = home / ".pi/agent/extensions/custom.ts"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.symlink_to(unrelated)

    assert m.link_is_ours(custom, m.repo_family(kit), kit) is False
    assert m._link_file(kit / "herdr-config.toml", custom, _quiet(m)) == "skip"

    counts = m.reconcile(
        m.LinkPlan({}, [home / ".pi/agent/extensions"]),
        m.repo_family(kit),
        plan_only=False,
        force=False,
        repo_root=kit,
    )
    assert counts["prune"] == 0
    assert custom.is_symlink()


def test_a_link_into_a_managed_checkout_subpath_is_still_ours(tmp_path: Path) -> None:
    """The other half of the rule: pre-decoupling links into the managed
    sub-paths must still be recognised, or migration would stop working."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = _kit_copy(m, tmp_path)
    family = m.repo_family(kit)
    for rel in (
        ".shared-llm/public/llm/pi/common/extensions/do-planish.ts",
        ".shared-llm/public/llm/pi/common/agents/doc-reviewer.md",
        "herdr-config.toml",
    ):
        link = home / ".pi/agent/extensions" / Path(rel).name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(kit / rel)
        assert m.link_is_ours(link, family, kit) is True, rel


# --- a destination repo is not a licence to prune anything inside it --------


@pytest.mark.parametrize("skill_dir", [".pi/agent/skills", ".agents/skills"])
def test_a_link_to_an_unrelated_file_in_a_destination_is_foreign(
    tmp_path: Path, skill_dir: str
) -> None:
    """Ownership is `<dest>/.claude/skills/<name>` or `<dest>/.pi-skills/<name>`,
    not "anywhere below a configured repo" — a user's own link to the repo's
    README lives under the destination too."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    entry = _dest_with_skill(dest, "custom")
    _write(dest / "README.md", "# the destination's readme\n")

    custom = home / skill_dir / "custom-notes"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.symlink_to(dest / "README.md")
    assert m._resolves_into(custom, [dest]) is False

    # Neither the desired pass nor the orphan sweep may touch it.
    _locked_update(m, _cfg(m, [], [entry]))
    assert custom.is_symlink() and custom.resolve() == (dest / "README.md")
    _locked_update(m, _cfg(m, [], []))
    assert custom.is_symlink() and custom.resolve() == (dest / "README.md")


def test_a_real_skill_link_into_a_destination_is_still_ours(tmp_path: Path) -> None:
    """The other half: the direct children we actually deploy stay recognised,
    including a dangling one whose target has been deleted."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    live = home / ".pi/agent/skills/live"
    live.parent.mkdir(parents=True, exist_ok=True)
    _write(dest / ".claude/skills/live/SKILL.md", "---\nname: live\n---\n")
    live.symlink_to(dest / ".claude/skills/live")
    routed = home / ".pi/agent/skills/routed"
    routed.symlink_to(dest / f"{m.PI_ONLY_SKILLS_DIR}/routed")  # dangling

    assert m._resolves_into(live, [dest]) is True
    assert m._resolves_into(routed, [dest]) is True


# --- managed markers are anchored at the repo root --------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "docs/.claude/skills/custom/SKILL.md",
        "docs/.claude/agents/custom.md",
        "examples/.shared-llm/public/llm/pi/common/extensions/x.ts",
        "examples/.shared-llm/public/llm/pi/common/agents/x.md",
        "examples/.shared-llm/public/llm/claude/common/hooks/x.sh",
        "samples/herdr-config.toml",
    ],
)
@pytest.mark.parametrize("dangling", [False, True])
def test_a_nested_decoy_inside_the_checkout_is_foreign(
    tmp_path: Path, rel: str, dangling: bool
) -> None:
    """A managed path must START at the repo root. Matched anywhere, a docs
    example or a sample file reads as a deployment we made — and reconcile would
    unlink the user's live link to it."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = _kit_copy(m, tmp_path)
    decoy = kit / rel
    if not dangling:
        _write(decoy, "not a deployment\n")

    link = home / ".pi/agent/extensions" / Path(rel).name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(decoy)

    assert m.link_is_ours(link, m.repo_family(kit), kit) is False
    counts = m.reconcile(
        m.LinkPlan({}, [home / ".pi/agent/extensions"]),
        m.repo_family(kit),
        plan_only=False,
        force=False,
        repo_root=kit,
    )
    assert counts["prune"] == 0 and link.is_symlink()


def test_a_sibling_worktree_deployment_is_still_ours(tmp_path: Path) -> None:
    """Anchoring tolerates exactly one leading component for a `<family>-trees`
    container, which is how sibling worktree checkouts are laid out."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    kit = tmp_path / "llm-config-setup"
    kit.mkdir()
    m.__dict__["project_root"] = lambda: kit
    family = m.repo_family(kit)

    wt = tmp_path / f"{family}-trees/feature-x"
    managed = wt / ".shared-llm/public/llm/pi/common/extensions/x.ts"
    _write(managed, "// worktree copy\n")
    nested = wt / "docs/.shared-llm/public/llm/pi/common/extensions/x.ts"
    _write(nested, "// a docs example\n")

    ours = home / ".pi/agent/extensions/x.ts"
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.symlink_to(managed)
    theirs = home / ".pi/agent/extensions/nested.ts"
    theirs.symlink_to(nested)

    assert m.link_is_ours(ours, family, kit) is True
    assert m.link_is_ours(theirs, family, kit) is False


# --- the deletion proof includes the dirs' own modes ------------------------


def test_root_only_mode_drift_blocks_migration_and_pruning(tmp_path: Path) -> None:
    """Same bytes, same child modes — but the user chmod-ed the directory itself,
    which is a deliberate local change, not a copy we may delete."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    generated = home / ".shared-llm/generated/skills/demo"
    target = home / ".claude/skills/demo"
    for root in (generated, target):
        _write(root / "SKILL.md", "---\nname: demo\n---\n")
        (root / "SKILL.md").chmod(0o644)
    generated.chmod(0o755)
    target.chmod(0o700)

    assert m._dirs_equal(generated, target) is False
    assert m._link_skill_dir(generated, target, _quiet(m)) == "skip"
    assert not target.is_symlink() and (target / "SKILL.md").is_file()

    assert (
        m._prune_stale_skill(
            "demo", generated, set(), {"cc": home / ".claude/skills"}, _quiet(m)
        )
        == 0
    )
    assert (target / "SKILL.md").is_file()

    name = m.DEPRECATED_GLOBAL_SKILLS[0]
    deprecated_gen = home / ".shared-llm/generated/skills" / name
    deprecated = home / ".claude/skills" / name
    for root in (deprecated_gen, deprecated):
        _write(root / "SKILL.md", f"---\nname: {name}\n---\n")
        (root / "SKILL.md").chmod(0o644)
    deprecated_gen.chmod(0o755)
    deprecated.chmod(0o700)
    assert m._prune_deprecated_global({"cc": home / ".claude/skills"}, _quiet(m)) == 0
    assert (deprecated / "SKILL.md").is_file()


# --- a destination link can change source in one run ------------------------


def test_a_moved_destination_transitions_its_link_in_one_run(tmp_path: Path) -> None:
    """The old target is no longer under any configured root, so it reads as
    foreign; only the previous run's own record proves we made it. Without that,
    the link is skipped and then retired as stale, and the skill is missing until
    another update."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    old = tmp_path / "old-repo"
    _locked_update(m, _cfg(m, [], [_dest_with_skill(old, "custom")]))
    link = home / ".pi/agent/skills/custom"
    assert link.resolve() == (old / ".claude/skills/custom")

    new = tmp_path / "new-repo"
    shutil.copytree(old, new)
    shutil.rmtree(old)
    _locked_update(m, _cfg(m, [], [_dest_with_skill(new, "custom")]))

    assert link.is_symlink(), "the skill must not go missing for a whole run"
    assert link.resolve() == (new / ".claude/skills/custom")
    assert _paths(m)[str(link)]["source"] == str(new / ".claude/skills/custom")


def test_a_changed_collision_winner_transitions_in_one_run(tmp_path: Path) -> None:
    """Same defect via the other route: two destinations offering one name, and
    the winner changes."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    first = _dest_with_skill(tmp_path / "first", "shared")
    second = _dest_with_skill(tmp_path / "second", "shared")
    _locked_update(m, _cfg(m, [], [first, second]))
    link = home / ".pi/agent/skills/shared"
    assert link.resolve() == (tmp_path / "second/.claude/skills/shared")

    # Reversing the order reverses the winner (last destination wins).
    _locked_update(m, _cfg(m, [], [second, first]))
    assert link.is_symlink()
    assert link.resolve() == (tmp_path / "first/.claude/skills/shared")


# --- `just link` records what it deploys ------------------------------------


def test_standalone_link_records_so_a_later_update_can_retire(tmp_path: Path) -> None:
    """`just link` is a home-mutating entry point, so it owes the manifest a
    record — otherwise removing the last destination strands its links."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    cfg = _cfg(m, [], [_dest_with_skill(dest, "custom")])
    m.save_config(cfg)
    m.cmd_link(argparse.Namespace())

    link = home / ".pi/agent/skills/custom"
    assert link.is_symlink()
    assert _paths(m)[str(link)]["kind"] == "repo-link"

    m.save_config(_cfg(m, [], []))
    m.cmd_update(argparse.Namespace(verbose=False))
    assert not link.is_symlink() and not link.exists()
    assert _dangling_links(home) == []


def test_standalone_link_keeps_unrelated_records(tmp_path: Path) -> None:
    """It records repo links WITHOUT retiring the global deployment it did not
    run — the settings and generated records must survive untouched."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    cfg = _cfg(m, ["cc"], [_dest_with_skill(dest, "custom")])
    m.do_global_flow(cfg, _quiet(m))
    before = _paths(m)
    agent = home / ".claude/agents/backend.md"
    assert agent.is_symlink() and str(agent) in before

    m.save_config(cfg)
    m.cmd_link(argparse.Namespace())

    after = _paths(m)
    assert agent.is_symlink(), "the global deployment must not be retired"
    assert after[str(agent)] == before[str(agent)]
    assert str(home / ".claude/settings.json") in after
    assert after[str(home / ".pi/agent/skills/custom")]["kind"] == "repo-link"


# --- global/prune must not retire a skill the config still wants ------------


@pytest.mark.parametrize("skill_dir", [".pi/agent/skills", ".agents/skills"])
def test_global_only_keeps_a_still_wanted_link_whose_source_moved(
    tmp_path: Path, skill_dir: str, capsys
) -> None:
    """`global` and `prune` skip the link step, so they cannot repoint a link
    whose destination moved. Recording only exact matches made them drop the
    record instead — and finalize then deleted a skill the config still asks
    for."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    old = tmp_path / "old-repo"
    _locked_update(m, _cfg(m, [], [_dest_with_skill(old, "custom")]))
    link = home / skill_dir / "custom"
    assert link.is_symlink()

    new = tmp_path / "new-repo"
    shutil.copytree(old, new)
    shutil.rmtree(old)
    moved = _cfg(m, [], [_dest_with_skill(new, "custom")])

    # The global-only path: no do_link, so nothing repoints the link.
    m.do_global_flow(moved, _quiet(m))

    assert link.is_symlink(), "a still-wanted skill must not be retired"
    assert _paths(m)[str(link)]["kind"] == "repo-link"
    assert "run `just update`" in capsys.readouterr().out

    # And a real update repoints it, as the warning says.
    _locked_update(m, moved)
    assert link.resolve() == (new / ".claude/skills/custom")


def test_global_only_keeps_a_link_whose_collision_winner_changed(
    tmp_path: Path,
) -> None:
    """Same defect through the other route: two destinations offering one name,
    reordered so the winner changes, with no link step to repoint it."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    first = _dest_with_skill(tmp_path / "first", "shared")
    second = _dest_with_skill(tmp_path / "second", "shared")
    _locked_update(m, _cfg(m, [], [first, second]))
    link = home / ".pi/agent/skills/shared"
    assert link.resolve() == (tmp_path / "second/.claude/skills/shared")

    m.do_global_flow(_cfg(m, [], [second, first]), _quiet(m))
    assert link.is_symlink(), "still wanted under the same name — keep it"
    assert link.resolve() == (tmp_path / "second/.claude/skills/shared")
    assert str(link) in _paths(m)


def test_global_only_still_retires_a_link_no_config_wants(tmp_path: Path) -> None:
    """The carry-forward is narrow: it applies only while the path is still
    desired. A destination that is gone is still retired by the global path."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _locked_update(m, _cfg(m, [], [_dest_with_skill(dest, "custom")]))
    link = home / ".pi/agent/skills/custom"
    assert link.is_symlink()

    m.do_global_flow(_cfg(m, [], []), _quiet(m))
    assert not link.is_symlink() and not link.exists()


# --- orphan cleanup respects anything a live link depends on ----------------


def test_a_live_link_to_a_file_inside_a_generated_dir_protects_the_dir(
    tmp_path: Path, capsys
) -> None:
    """Protecting only the exact target let the cleanup delete the DIRECTORY
    containing it, dangling the link."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    m.do_global_flow(_cfg(m, ["cc"]), _quiet(m))

    source_dir = home / ".shared-llm/generated/skills/interrupted"
    _write(source_dir / "SKILL.md", "---\nname: interrupted\n---\n")
    link = home / ".claude/skills/interrupted-note.md"
    link.symlink_to(source_dir / "SKILL.md")

    m.do_global_flow(_cfg(m, []), _quiet(m))

    assert source_dir.is_dir() and (source_dir / "SKILL.md").is_file()
    assert link.is_symlink() and link.exists()
    assert _dangling_links(home) == []
    assert "keeping" in capsys.readouterr().out


def test_a_live_link_to_a_namespace_dir_protects_its_children(tmp_path: Path) -> None:
    """A link at the namespace itself resolves THROUGH it, so emptying it is the
    same breakage one level up."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    m.do_global_flow(_cfg(m, ["cc"]), _quiet(m))

    agents = home / ".shared-llm/generated/agents"
    assert any(agents.iterdir())
    link = home / ".claude/all-agents"
    link.symlink_to(agents)

    m.do_global_flow(_cfg(m, []), _quiet(m))

    assert sorted(agents.iterdir()), "the linked dir must not be emptied"
    assert link.is_symlink() and link.exists()
    assert _dangling_links(home) == []


# --- manifest validation matches the writer exactly -------------------------


def test_a_nested_repo_link_source_is_rejected(tmp_path: Path) -> None:
    """This code only ever writes a DIRECT skill child. Accepting a nested path
    lets a hand-edited manifest name someone else's symlink and have finalize
    delete it."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    victim_target = tmp_path / "repo/.claude/skills/real/nested"
    _write(victim_target / "SKILL.md", "someone else's\n")
    victim = home / ".pi/agent/skills/real"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.symlink_to(victim_target)

    m.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    m.manifest_path().write_text(
        json.dumps(
            {
                "version": 1,
                "paths": {
                    str(victim): {"kind": "repo-link", "source": str(victim_target)}
                },
            }
        )
    )
    assert m.HomeManifest().previous_ok is False

    m.do_global_flow(_cfg(m, []), _quiet(m))
    assert victim.is_symlink() and victim.resolve() == victim_target.resolve()


@pytest.mark.parametrize(
    "rel, ok",
    [
        ("repo/.claude/skills/real", True),
        ("repo/.pi-skills/do-thing", True),
        ("repo/.claude/skills/real/nested", False),
        ("repo/.claude/agents/real", False),
        ("repo/skills/real", False),
    ],
)
def test_repo_link_sources_must_be_direct_skill_children(rel: str, ok: bool) -> None:
    m = _load()
    assert m._looks_like_destination_skill(f"/tmp/{rel}") is ok


# --- crash residue from a half-finished handback ----------------------------


def _interrupted_handback(m, home: Path, tmp_path: Path) -> tuple[Path, Path, dict]:
    """Reproduce the state a killed update leaves behind.

    An update retired the destination, handed the name to a generated skill, and
    died after repointing the home link but before persisting that. So the
    durable manifest still says repo-link while the live link already points into
    the generated tree — and the config wants the destination path again."""
    dest = tmp_path / "dest"
    cfg = _cfg(m, [], [_dest_with_skill(dest, "custom")])
    _locked_update(m, cfg)
    link = home / ".pi/agent/skills/custom"
    assert _paths(m)[str(link)]["kind"] == "repo-link"

    generated = home / ".shared-llm/generated/skills/custom"
    _write(generated / "SKILL.md", "---\nname: custom\n---\ngenerated\n")
    link.unlink()
    link.symlink_to(generated)  # …and then the process died, manifest untouched
    return link, generated, cfg


@pytest.mark.parametrize("entry", ["global", "prune"])
@pytest.mark.parametrize("runs", [1, 2, 3])
def test_an_interrupted_handback_is_preserved_not_deleted(
    tmp_path: Path, entry: str, runs: int, capsys
) -> None:
    """Neither global-only entry point may delete a skill the config still asks
    for just because the durable record and the live link disagree.

    Preservation has to be a FIXED POINT: the first run rewrites the record as
    kind "link", so a second and third run must recognise their own output and
    preserve it again rather than reading it as a stale deployment."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    link, generated, cfg = _interrupted_handback(m, home, tmp_path)

    for _ in range(runs):
        _run_entry(m, entry, cfg)
        assert link.is_symlink() and link.exists(), "the live link must survive"
        assert (generated / "SKILL.md").is_file(), "and so must its source"
        assert _paths(m)[str(link)] == {"kind": "link", "source": str(generated)}

    assert _dangling_links(home) == []
    assert "interrupted handback" in capsys.readouterr().out


@pytest.mark.parametrize("entry", [None, "global", "prune"])
def test_a_full_update_finishes_the_interrupted_handback(
    tmp_path: Path, entry: str | None
) -> None:
    """The warning's promise: `just update` runs the link step, so it transitions
    the path back to the destination the config actually wants — whether or not
    global-only runs preserved the state first."""
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    link, _, cfg = _interrupted_handback(m, home, tmp_path)
    if entry is not None:
        _run_entry(m, entry, cfg)
        _run_entry(m, entry, cfg)

    _locked_update(m, cfg)

    assert link.resolve() == (tmp_path / "dest/.claude/skills/custom")
    assert _paths(m)[str(link)] == {
        "kind": "repo-link",
        "source": str(tmp_path / "dest/.claude/skills/custom"),
    }


if __name__ == "__main__":
    import subprocess

    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
