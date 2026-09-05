"""Black-box tests for the Herdr 0.7.1 pin installer."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools/install-herdr.sh"


def _run(
    tmp_path: Path,
    *args: str,
    herdr_version: str | None = "0.7.1",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if herdr_version is not None:
        herdr = bin_dir / "herdr"
        herdr.write_text(
            "#!/usr/bin/env bash\n"
            f"echo 'herdr {herdr_version}'\n"
        )
        herdr.chmod(0o755)
    env = os.environ.copy()
    # Isolate from the machine's real herdr (often ~/.local/bin/herdr).
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        text=True,
        capture_output=True,
        env=env,
        cwd=str(ROOT),
    )


def test_script_pins_0_7_1() -> None:
    text = SCRIPT.read_text()
    assert 'PINNED="${HERDR_PIN:-0.7.1}"' in text
    assert "herdr.dev/install.sh" in text
    assert "github.com/herdrdev/herdr/releases/download/v" in text


def test_check_passes_on_pinned_version(tmp_path: Path) -> None:
    result = _run(tmp_path, "--check")
    assert result.returncode == 0, result.stderr
    assert "0.7.1" in result.stdout


def test_check_fails_on_other_version(tmp_path: Path) -> None:
    result = _run(tmp_path, "--check", herdr_version="0.8.0")
    assert result.returncode != 0
    assert "0.8.0" in result.stderr
    assert "0.7.1" in result.stderr


def test_check_fails_when_herdr_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, "--check", herdr_version=None)
    assert result.returncode != 0


def test_install_is_noop_when_already_pinned(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "already on PATH" in result.stdout


def test_install_refuses_other_version_without_force(tmp_path: Path) -> None:
    result = _run(tmp_path, herdr_version="0.9.0")
    assert result.returncode != 0
    assert "--force" in result.stderr


def test_unknown_argument_is_error(tmp_path: Path) -> None:
    result = _run(tmp_path, "--dry-run")
    assert result.returncode == 2


def test_lone_double_dash_is_ignored(tmp_path: Path) -> None:
    result = _run(tmp_path, "--", "--check")
    assert result.returncode == 0, result.stderr
