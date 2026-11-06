"""`just ovis-pair` / `just ovis-token`: reach Ovis without knowing where it lives.

Ovis (the herdr browser and phone client) keeps its settings, including OVIS_TOKEN, in the
folder `herdr plugin config-dir devswha.ovis` names, and its code under a plugin root that
changes when it updates. tools/ovis.sh looks both up through herdr, so a person never has
to know either path. These tests stand in a fake herdr and a fake Ovis checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/ovis.sh"


def _fake_herdr(tmp_path: Path, *, installed: bool = True) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    plugin_root = tmp_path / "ovis-checkout"
    (plugin_root / "scripts").mkdir(parents=True)
    # Ovis's own launcher finds bun; the fake records what it was asked to run.
    (plugin_root / "scripts/with-bun.sh").write_text(
        '#!/bin/sh\necho "bun-ran: $* (cwd $(pwd))"\n'
    )
    config_dir = tmp_path / "ovis-config"
    config_dir.mkdir()
    plugins = (
        [{"plugin_id": "devswha.ovis", "plugin_root": str(plugin_root)}] if installed else []
    )
    listing = json.dumps({"result": {"plugins": plugins}})
    herdr = bin_dir / "herdr"
    herdr.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  'plugin list --json') printf '%s\\n' '{listing}' ;;\n"
        f"  'plugin config-dir devswha.ovis') printf '%s\\n' '{config_dir}' ;;\n"
        '  *) echo "unexpected herdr $*" >&2; exit 9 ;;\n'
        "esac\n"
    )
    herdr.chmod(0o755)
    return bin_dir, plugin_root, config_dir


def _run(bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OVIS_")}
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    return subprocess.run(
        ["sh", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
    )


def test_pair_runs_ovis_pair_from_its_own_checkout(tmp_path: Path) -> None:
    bin_dir, plugin_root, _ = _fake_herdr(tmp_path)
    result = _run(bin_dir, "pair")
    assert result.returncode == 0, result.stderr
    assert f"bun-ran: {plugin_root}/scripts/plugin.ts pair (cwd {plugin_root})" in result.stdout


def test_token_prints_the_token_ovis_reads(tmp_path: Path) -> None:
    bin_dir, _, config_dir = _fake_herdr(tmp_path)
    (config_dir / "env").write_text("HOST=127.0.0.1\nOVIS_TOKEN=abc123\nPORT=7317\n")
    result = _run(bin_dir, "token")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "abc123\n"


def test_token_in_dot_env_wins_like_ovis(tmp_path: Path) -> None:
    # Ovis reads `env`, then `.env`; where both set a key, `.env` wins.
    bin_dir, _, config_dir = _fake_herdr(tmp_path)
    (config_dir / "env").write_text("OVIS_TOKEN=from-env\n")
    (config_dir / ".env").write_text("OVIS_TOKEN=from-dot-env\n")
    assert _run(bin_dir, "token").stdout == "from-dot-env\n"


def test_token_missing_fails_and_says_where_to_set_it(tmp_path: Path) -> None:
    bin_dir, _, config_dir = _fake_herdr(tmp_path)
    (config_dir / "env").write_text("HOST=127.0.0.1\n")
    result = _run(bin_dir, "token")
    assert result.returncode == 1
    assert "OVIS_TOKEN" in result.stderr
    assert str(config_dir / "env") in result.stderr


def test_ovis_not_installed_fails_loud(tmp_path: Path) -> None:
    bin_dir, _, _ = _fake_herdr(tmp_path, installed=False)
    result = _run(bin_dir, "pair")
    assert result.returncode == 1
    assert "Ovis is not installed" in result.stderr


@pytest.mark.parametrize("args", [(), ("bogus",)])
def test_unknown_command_prints_usage(tmp_path: Path, args: tuple[str, ...]) -> None:
    bin_dir, _, _ = _fake_herdr(tmp_path)
    result = _run(bin_dir, *args)
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_justfile_exposes_both_recipes() -> None:
    justfile = (ROOT / "justfile").read_text()
    assert "\novis-pair:\n    @sh tools/ovis.sh pair\n" in justfile
    assert "\novis-token:\n    @sh tools/ovis.sh token\n" in justfile
