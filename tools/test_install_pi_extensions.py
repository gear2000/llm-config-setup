"""Black-box tests for the third-party Pi extension reconciler."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools/install-pi-extensions.sh"


def _mock_pi(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    pi = bin_dir / "pi"
    pi.write_text("""#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  --version) echo 'pi 0.test' ;;
  list)
    [[ "${PI_FAIL:-}" != list ]] || exit 42
    printf '%s\\n' "${PI_LIST:-}"
    ;;
  install|remove)
    [[ "${PI_FAIL:-}" != "$1" ]] || exit 43
    printf '%s %s\\n' "$1" "$2" >> "$PI_CALLS"
    ;;
  *) exit 44 ;;
esac
""")
    pi.chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "PI_CALLS": str(calls)})
    return env, calls


def _run(tmp_path: Path, manifest: str, installed: str = "", fail: str = "") -> tuple[subprocess.CompletedProcess[str], Path]:
    manifest_path = tmp_path / "extensions.txt"
    manifest_path.write_text(manifest)
    env, calls = _mock_pi(tmp_path)
    env.update({"PI_EXTENSIONS_MANIFEST": str(manifest_path), "PI_LIST": installed})
    if fail:
        env["PI_FAIL"] = fail
    result = subprocess.run(["bash", str(SCRIPT)], text=True, capture_output=True, env=env)
    return result, calls


def test_installs_manifest_and_removes_only_retired_hypa(tmp_path: Path) -> None:
    result, calls = _run(
        tmp_path,
        "npm:pi-lens@1.2.3\nnpm:@scope/pkg@2.0.0\n",
        "npm:unrelated@9\nnpm:@hypabolic/pi-hypa@0.1.6\n",
    )
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "remove npm:@hypabolic/pi-hypa",
        "install npm:pi-lens@1.2.3",
        "install npm:@scope/pkg@2.0.0",
    ]


def test_present_manifest_entries_and_absent_hypa_are_noops(tmp_path: Path) -> None:
    result, calls = _run(
        tmp_path,
        "npm:pi-lens@1.2.3\nnpm:@scope/pkg@2.0.0\n",
        "npm:pi-lens@1.2.3\nnpm:@scope/pkg@2.0.0\nnpm:unrelated@9\n",
    )
    assert result.returncode == 0, result.stderr
    assert not calls.exists()


def test_cli_failures_are_nonzero(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, "npm:pi-lens@1.2.3\n", fail="list")
    assert result.returncode != 0

    result, _ = _run(tmp_path, "npm:pi-lens@1.2.3\n", fail="install")
    assert result.returncode != 0

    result, _ = _run(tmp_path, "npm:pi-lens@1.2.3\n", "npm:@hypabolic/pi-hypa@0.1.6\n", "remove")
    assert result.returncode != 0
