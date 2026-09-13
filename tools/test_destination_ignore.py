"""Destination skip: config `ignore: true`, plus `just update --ignore` / `--only`.

`exclude:` skips compose recipes. Destination ignore is a different concept —
a registered dest stays in ~/.shared-llm.yaml but is not copied, composed,
linked, or aggregated until it is unignored (or a one-off CLI filter says so).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from test_config_flow import (
    _add_common_skill,
    _load,
    _patch_home,
    _quiet,
    _scaffold_dest,
    _write,
)


def _two_dests(tmp_path: Path, m):
    home = tmp_path / "home"
    _patch_home(m, home)
    kept = tmp_path / "kept"
    skipped = tmp_path / "skipped"
    _scaffold_dest(kept)
    _scaffold_dest(skipped)
    _write(m.DEFAULT_SOURCE / "layers/llm/common/new-common.md", "hub common.\n")
    return home, kept, skipped


def _minimal_kit(m, tmp_path: Path) -> None:
    """Dest recipes point at public demo layers; resolve them against a tiny kit."""
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


def test_config_ignore_skips_destination_copy_and_prints_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dest with ignore: true stays in the config but copy does not touch it."""
    m = _load()
    _, kept, skipped = _two_dests(tmp_path, m)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [
            {"path": str(kept), "harnesses": ["cc", "pi"]},
            {"path": str(skipped), "harnesses": ["cc", "pi"], "ignore": True},
        ],
    }

    m.do_copy(cfg, m.RunLog(verbose=False))

    propagated = ".shared-llm/public/layers/llm/common/new-common.md"
    assert (kept / propagated).read_text() == "hub common.\n"
    assert not (skipped / propagated).exists()
    out = capsys.readouterr().out
    assert f"  ⏭ ignoring {skipped} (ignore: true)" in out
    assert f"  ⏭ ignoring {kept} (ignore: true)" not in out


def test_config_ignore_skips_compose(tmp_path: Path) -> None:
    m = _load()
    _, kept, skipped = _two_dests(tmp_path, m)
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [
            {"path": str(kept), "harnesses": ["cc", "pi"]},
            {"path": str(skipped), "harnesses": ["cc", "pi"], "ignore": True},
        ],
    }

    m.do_compose(cfg, _quiet(m))

    assert (kept / ".claude/skills/demo/SKILL.md").exists()
    assert not (skipped / ".claude/skills/demo/SKILL.md").exists()


def _configure_ns(**overrides):
    base = dict(
        source=None,
        dest=None,
        list=None,
        global_list=None,
        exclude=None,
        offering_sets=None,
        ignore=False,
        unignore=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_configure_ignore_and_unignore_flip_the_config_key(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "repo"

    m.cmd_configure(_configure_ns(dest=str(dest), list="cc,pi"))
    cfg = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert "ignore" not in cfg["destinations"][0]

    m.cmd_configure(_configure_ns(dest=str(dest), ignore=True))
    cfg = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert cfg["destinations"][0]["ignore"] is True
    assert cfg["destinations"][0]["harnesses"] == ["cc", "pi"]
    assert cfg["destinations"][0]["path"] == str(dest.resolve())

    m.cmd_configure(_configure_ns(dest=str(dest), unignore=True))
    cfg = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert "ignore" not in cfg["destinations"][0]
    assert cfg["destinations"][0]["harnesses"] == ["cc", "pi"]


def test_configure_ignore_without_dest_fails_loud(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    with pytest.raises(SystemExit, match="--ignore"):
        m.cmd_configure(_configure_ns(ignore=True))


def test_configure_ignore_and_unignore_together_fails_loud(tmp_path: Path) -> None:
    m = _load()
    _patch_home(m, tmp_path / "home")
    dest = tmp_path / "repo"
    with pytest.raises(SystemExit, match="--ignore.*--unignore|--unignore.*--ignore"):
        m.cmd_configure(_configure_ns(dest=str(dest), ignore=True, unignore=True))


def _update_ns(*, verbose: bool = False, ignore=None, only=None):
    return argparse.Namespace(verbose=verbose, ignore=ignore or [], only=only or [])


def _save_two_dests(m, kept: Path, skipped: Path, *, ignore_skipped: bool = False):
    skipped_entry = {"path": str(skipped), "harnesses": ["cc", "pi"]}
    if ignore_skipped:
        skipped_entry["ignore"] = True
    cfg = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [
            {"path": str(kept), "harnesses": ["cc", "pi"]},
            skipped_entry,
        ],
    }
    m.save_config(cfg)
    return cfg


def test_update_ignore_flag_skips_named_destination(tmp_path: Path) -> None:
    m = _load()
    _, kept, skipped = _two_dests(tmp_path, m)
    _minimal_kit(m, tmp_path)
    _save_two_dests(m, kept, skipped)

    m.cmd_update(_update_ns(ignore=[skipped.name]))

    propagated = ".shared-llm/public/layers/llm/common/new-common.md"
    assert (kept / propagated).exists()
    assert not (skipped / propagated).exists()
    # One-off CLI skip must not write ignore: true into the config.
    saved = yaml.safe_load(m.CONFIG_PATH.read_text())
    assert "ignore" not in saved["destinations"][1]


def test_update_only_flag_runs_just_the_listed_destinations(tmp_path: Path) -> None:
    m = _load()
    _, kept, skipped = _two_dests(tmp_path, m)
    _minimal_kit(m, tmp_path)
    _save_two_dests(m, kept, skipped)

    m.cmd_update(_update_ns(only=[str(kept)]))

    propagated = ".shared-llm/public/layers/llm/common/new-common.md"
    assert (kept / propagated).exists()
    assert not (skipped / propagated).exists()


def test_update_matches_configured_path_expanded_form_and_basename(
    tmp_path: Path,
) -> None:
    m = _load()
    dest = {"path": str(tmp_path / "kept")}
    exact = dest["path"]
    assert m.destination_matches(dest, exact)
    assert m.destination_matches(dest, "kept")
    assert m.destination_matches(dest, str(Path(exact).expanduser()))
    assert not m.destination_matches(dest, "other")


def test_update_unmatched_selector_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    m = _load()
    _, kept, skipped = _two_dests(tmp_path, m)
    _save_two_dests(m, kept, skipped)

    with pytest.raises(SystemExit) as ei:
        m.cmd_update(_update_ns(ignore=["no-such-repo"]))
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "no-such-repo" in err
    assert str(kept) in err
    assert str(skipped) in err


def test_update_with_no_flags_and_no_ignore_keys_updates_every_destination(
    tmp_path: Path,
) -> None:
    """Regression: bare `just update` must keep today's all-destinations behaviour."""
    m = _load()
    _, kept, skipped = _two_dests(tmp_path, m)
    _minimal_kit(m, tmp_path)
    _save_two_dests(m, kept, skipped)

    m.cmd_update(argparse.Namespace(verbose=False))

    propagated = ".shared-llm/public/layers/llm/common/new-common.md"
    assert (kept / propagated).exists()
    assert (skipped / propagated).exists()
    assert (kept / ".claude/skills/demo/SKILL.md").exists()
    assert (skipped / ".claude/skills/demo/SKILL.md").exists()


def test_ignored_destination_contributes_nothing_to_home_skill_aggregation(
    tmp_path: Path,
) -> None:
    m = _load()
    _, kept, skipped = _two_dests(tmp_path, m)
    _add_common_skill(kept, "kept-only")
    _add_common_skill(skipped, "skipped-only")
    cfg_all = {
        "source": str(m.DEFAULT_SOURCE),
        "global": [],
        "destinations": [
            {"path": str(kept), "harnesses": ["pi"]},
            {"path": str(skipped), "harnesses": ["pi"]},
        ],
    }
    m.do_compose(cfg_all, _quiet(m))
    assert (kept / ".claude/skills/kept-only/SKILL.md").exists()
    assert (skipped / ".claude/skills/skipped-only/SKILL.md").exists()

    cfg_all["destinations"][1]["ignore"] = True
    pi_desired, _, _ = m.destination_home_skills(cfg_all)
    assert "kept-only" in pi_desired
    assert "skipped-only" not in pi_desired
    assert "demo" in pi_desired


def test_ignored_destination_keeps_existing_home_skill_links(tmp_path: Path) -> None:
    """Skipping a dest must not retire home skill links it already deployed."""
    m = _load()
    home, kept, skipped = _two_dests(tmp_path, m)
    _minimal_kit(m, tmp_path)
    _add_common_skill(skipped, "skipped-only")
    _save_two_dests(m, kept, skipped)

    m.cmd_update(_update_ns())
    link = home / ".pi/agent/skills/skipped-only"
    assert link.is_symlink()

    _save_two_dests(m, kept, skipped, ignore_skipped=True)
    m.cmd_update(_update_ns())
    assert link.is_symlink(), "ignored dest's existing home link must stay"
    assert link.resolve() == (skipped / ".claude/skills/skipped-only").resolve()


def test_global_still_runs_when_every_destination_is_ignored(tmp_path: Path) -> None:
    m = _load()
    home, kept, skipped = _two_dests(tmp_path, m)
    _minimal_kit(m, tmp_path)
    m.save_config(
        {
            "source": str(m.DEFAULT_SOURCE),
            "global": [],
            "upagent": {"offering_sets": ["standard"]},
            "destinations": [
                {"path": str(kept), "harnesses": ["cc"], "ignore": True},
                {"path": str(skipped), "harnesses": ["cc"], "ignore": True},
            ],
        }
    )

    m.cmd_update(_update_ns())

    roster = home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    assert roster.is_file()
    propagated = ".shared-llm/public/layers/llm/common/new-common.md"
    assert not (kept / propagated).exists()
    assert not (skipped / propagated).exists()
