"""Black-box tests for the Herdr 0.9.5 pin installer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools/install-herdr.sh"


def _run(
    tmp_path: Path,
    *args: str,
    herdr_version: str | None = "0.9.5",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if herdr_version is not None:
        herdr = bin_dir / "herdr"
        herdr.write_text(f"#!/usr/bin/env bash\necho 'herdr {herdr_version}'\n")
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


def test_script_pins_0_9_5() -> None:
    text = SCRIPT.read_text()
    assert 'PINNED="${HERDR_PIN:-0.9.5}"' in text
    assert "herdr.dev/install.sh" in text
    assert "github.com/herdrdev/herdr/releases/download/v" in text


def test_check_passes_on_pinned_version(tmp_path: Path) -> None:
    result = _run(tmp_path, "--check")
    assert result.returncode == 0, result.stderr
    assert "0.9.5" in result.stdout


def test_check_fails_on_other_version(tmp_path: Path) -> None:
    result = _run(tmp_path, "--check", herdr_version="0.8.0")
    assert result.returncode != 0
    assert "0.8.0" in result.stderr
    assert "0.9.5" in result.stderr


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


def _release_downloads(
    tmp_path: Path, version: str, *, digest: str = "matching"
) -> None:
    import hashlib
    import json

    payload = f"#!/usr/bin/env bash\necho 'herdr {version}'\n"
    expected = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    if digest == "missing":
        expected = None
    elif digest == "wrong":
        expected = "sha256:" + "0" * 64
    release = {"assets": [{"name": "herdr-linux-x86_64", "digest": expected}]}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
        "destination = Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "api = any('api.github.com' in arg for arg in sys.argv)\n"
        f"destination.write_text({json.dumps(release)!r} if api else {payload!r})\n"
    )
    curl.chmod(0o755)
    uname = bin_dir / "uname"
    uname.write_text(
        '#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else echo x86_64; fi\n'
    )
    uname.chmod(0o755)


def test_install_verifies_release_digest_before_replacing_binary(
    tmp_path: Path,
) -> None:
    _release_downloads(tmp_path, "0.9.5")
    target = tmp_path / "install"
    result = _run(
        tmp_path,
        "--force",
        herdr_version="0.7.1",
        extra_env={"HERDR_INSTALL_DIR": str(target)},
    )
    assert result.returncode == 0, result.stderr
    assert "Verified herdr-linux-x86_64 sha256:" in result.stdout
    assert "0.9.5" in (target / "herdr").read_text()


def test_digest_mismatch_preserves_installed_binary(tmp_path: Path) -> None:
    _release_downloads(tmp_path, "0.9.5", digest="wrong")
    target = tmp_path / "install"
    target.mkdir()
    (target / "herdr").write_bytes(b"original installation")
    result = _run(
        tmp_path,
        "--force",
        herdr_version="0.7.1",
        extra_env={"HERDR_INSTALL_DIR": str(target)},
    )
    assert result.returncode != 0 and "SHA-256 mismatch" in result.stderr
    assert (target / "herdr").read_bytes() == b"original installation"


def test_legacy_rollback_reports_missing_published_digest(tmp_path: Path) -> None:
    _release_downloads(tmp_path, "0.7.1", digest="missing")
    result = _run(
        tmp_path,
        "--force",
        herdr_version="0.9.5",
        extra_env={
            "HERDR_PIN": "0.7.1",
            "HERDR_INSTALL_DIR": str(tmp_path / "install"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "Skipping SHA-256: Herdr 0.7.1 publishes no digest" in result.stdout


def test_pinned_release_cannot_install_without_digest(tmp_path: Path) -> None:
    _release_downloads(tmp_path, "0.9.5", digest="missing")
    target = tmp_path / "install"
    result = _run(
        tmp_path,
        "--force",
        herdr_version="0.7.1",
        extra_env={"HERDR_INSTALL_DIR": str(target)},
    )
    assert result.returncode != 0 and "no valid SHA-256" in result.stderr
    assert not (target / "herdr").exists()
