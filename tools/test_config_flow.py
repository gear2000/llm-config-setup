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
import sys
from pathlib import Path

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
    """A minimal, self-contained destination .shared-llm/ with one common-routed
    skill recipe that pulls a common layer AND a this_repo overlay."""
    s = dest / ".shared-llm"
    _write(s / "layers/skills/common/demo/description.md", "A demo skill.\n")
    _write(s / "layers/skills/common/demo/practices.md", "COMMON practices body.\n")
    _write(s / "layers/skills/this_repo/demo.md", "THIS_REPO overlay body.\n")
    _write(s / "compose/skills/demo.yaml", yaml.safe_dump({
        "type": "skill",
        "name": "demo",
        "description": ".shared-llm/layers/skills/common/demo/description.md",
        "inputs": [
            ".shared-llm/layers/skills/common/demo/practices.md",
            ".shared-llm/layers/skills/this_repo/demo.md",
        ],
        "output": ".claude/skills/demo/SKILL.md",
    }, sort_keys=False))


def _add_slash_skill(dest: Path, name: str, scope: str) -> None:
    """Add a slash-command skill recipe at compose/slash-commands/common/<scope>/<name>.yaml."""
    s = dest / ".shared-llm"
    _write(s / f"layers/slash-commands/common/{scope}/{name}/command.md", f"{name} body\n")
    _write(s / f"layers/slash-commands/common/{scope}/{name}/description.md", f"{name} desc\n")
    _write(s / f"compose/slash-commands/common/{scope}/{name}.yaml", yaml.safe_dump({
        "type": "skill", "name": name,
        "description": f".shared-llm/layers/slash-commands/common/{scope}/{name}/description.md",
        "inputs": [f".shared-llm/layers/slash-commands/common/{scope}/{name}/command.md"],
        "output": f".claude/skills/{name}/SKILL.md",
    }, sort_keys=False))


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

    args = argparse.Namespace(source=None, dest=str(dest), list="cc,pi", global_list=None, exclude=None)
    m.cmd_configure(args)
    assert m.CONFIG_PATH.exists()
    cfg1 = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert cfg1["destinations"] == [{"path": str(dest.resolve()), "harnesses": ["cc", "pi"]}]

    # Re-running with the same dest updates in place (no duplicate entry).
    m.cmd_configure(argparse.Namespace(source=None, dest=str(dest), list="cc,pi,codex", global_list=None, exclude=None))
    cfg2 = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert len(cfg2["destinations"]) == 1
    assert cfg2["destinations"][0]["harnesses"] == ["cc", "pi", "codex"]


def test_configure_rejects_unknown_harness(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")

    args = argparse.Namespace(source=None, dest=str(tmp_path / "dest"), list="cc,bogus", global_list=None, exclude=None)
    try:
        m.cmd_configure(args)
        assert False, "expected SystemExit on unknown harness"
    except SystemExit as e:
        assert "bogus" in str(e)


# --- copy ------------------------------------------------------------------

def test_copy_propagates_new_and_flags_changed(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    dest = tmp_path / "dest"
    _scaffold_dest(dest)
    cfg = _cfg(m, dest, ["cc", "pi"])

    # Seed the HUB with a common file the dest doesn't have yet, plus one it does
    # have but with different content (a local edit that copy must overwrite+flag).
    hub = m.DEFAULT_SOURCE
    _write(hub / "layers/skills/common/demo/practices.md", "HUB practices v2.\n")
    _write(hub / "layers/llm/common/new-common.md", "brand new common file.\n")

    m.do_copy(cfg, _quiet(m))

    # New common file propagated into the destination's .shared-llm/.
    assert (dest / ".shared-llm/layers/llm/common/new-common.md").read_text() == "brand new common file.\n"
    # The pre-existing common file was overwritten with the hub version.
    assert (dest / ".shared-llm/layers/skills/common/demo/practices.md").read_text() == "HUB practices v2.\n"
    # The this_repo overlay was NEVER touched by copy.
    assert (dest / ".shared-llm/layers/skills/this_repo/demo.md").read_text() == "THIS_REPO overlay body.\n"


# --- compose ---------------------------------------------------------------

def test_compose_reads_destination_shared_llm_and_merges_overlay(tmp_path: Path) -> None:
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
    assert pi_link.is_symlink() and pi_link.resolve() == (dest / ".claude/skills/demo").resolve()
    assert codex_link.is_symlink() and codex_link.resolve() == (dest / ".claude/skills/demo").resolve()
    # No repo-scoped .pi/skills is created.
    assert not (dest / ".pi/skills").exists()
    # cc reads .claude/ directly.
    assert (dest / ".claude/skills/demo/SKILL.md").exists()


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

    assert (g / "demo").is_symlink()          # desired link created
    assert not ours_stale.exists()            # our stale link pruned
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
    c = m._reconcile_global(home / ".pi/agent/skills", m._common_pi_skills(dest), [dest], m.RunLog(verbose=False))
    assert c["create"] == 0 and c["repoint"] == 0 and c["prune"] == 0


# --- global home-skill routing ---------------------------------------------

def test_global_routes_by_scope_and_excludes_do_star_from_cc(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc", "pi", "codex"], "destinations": []}
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

    # do-* workflow commands reach pi (and codex) but NOT cc.
    assert (pi / "do-plan-and-grill/SKILL.md").exists()
    assert not (cc / "do-plan-and-grill").exists(), "do-* must not land in Claude Code home"

    # Claude-scoped cc-* command reaches cc only.
    assert (cc / "cc-plan-and-grill/SKILL.md").exists()
    assert not (pi / "cc-plan-and-grill").exists()


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
    _scaffold_dest(dest)                        # 'demo' = common
    _add_slash_skill(dest, "do-plan-and-grill", "common")   # do-* (recipe scope common)
    _add_slash_skill(dest, "do-loop", "claude")             # do-* even if recipe is claude-scoped
    _add_slash_skill(dest, "cc-plan-and-grill", "claude")   # cc-*
    cfg = _cfg(m, dest, ["cc", "pi", "codex"])
    m.do_compose(cfg, _quiet(m))
    m.do_link(cfg, _quiet(m))

    cs = dest / ".claude/skills"
    pi = home / ".pi/agent/skills"
    codex = home / ".agents/skills"

    # Claude Code (.claude/skills): cc-* + common present, NO do-*
    assert (cs / "cc-plan-and-grill/SKILL.md").exists()
    assert (cs / "demo/SKILL.md").exists()
    assert not (cs / "do-plan-and-grill").exists(), "do-* must be routed OUT of Claude's dir"
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

def test_home_runtime_copies_agents_and_excludes_codex(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc", "pi", "codex"], "destinations": []}
    m.do_home_runtime(cfg, _quiet(m))

    # Generic agents land in the cc + pi home agent dirs (a known persona: backend).
    assert (home / ".claude/agents/backend.md").exists()
    assert (home / ".pi/agents/backend.md").exists()
    # Codex has no user-agent dir — never invented.
    assert not (home / ".codex/agents").exists()
    assert not (home / ".agents/agents").exists()


def test_home_runtime_installs_claude_and_pi_runtime(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc", "pi"], "destinations": []}
    m.do_home_runtime(cfg, _quiet(m))

    # Claude settings scaffolded from template.
    assert (home / ".claude/settings.json").exists()
    # Pi extensions symlinked into ~/.pi/agent/extensions (do-planish.ts is bundled).
    ext = home / ".pi/agent/extensions/do-planish.ts"
    assert ext.is_symlink()
    # Pi settings scaffolded.
    assert (home / ".pi/agent/settings.json").exists()


def test_home_runtime_exclude_by_source_path_skips_install(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    # Baseline: without exclude, hooks + statusline install.
    base_cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": [], "exclude": []}
    m.do_home_runtime(base_cfg, _quiet(m))
    assert (home / ".claude/statusline.sh").exists()
    hooks_dir = home / ".claude/hooks"
    assert hooks_dir.is_dir() and any(hooks_dir.iterdir()), "hooks should install without exclude"

    # Now a fresh home WITH hooks excluded by source path.
    home2 = tmp_path / "home2"
    _patch_home(m, home2)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["cc"], "destinations": [],
           "exclude": ["llm/claude/common/hooks"]}
    m.do_home_runtime(cfg, _quiet(m))
    # hooks skipped, statusline still installed (not excluded)
    assert not (home2 / ".claude/hooks").exists() or not any((home2 / ".claude/hooks").iterdir())
    assert (home2 / ".claude/statusline.sh").exists()


def test_home_runtime_exclude_drops_pi_extension(tmp_path: Path) -> None:
    m = _load()
    home = tmp_path / "home"
    _patch_home(m, home)
    cfg = {"source": str(m.DEFAULT_SOURCE), "global": ["pi"], "destinations": [],
           "exclude": ["llm/pi/common/extensions/do-planish.ts"]}
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


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
