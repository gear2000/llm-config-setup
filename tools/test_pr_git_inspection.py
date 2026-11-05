"""Prove the documented Git guard on disposable content, not model compliance."""

import hashlib
import os
from pathlib import Path
import subprocess


def test_optional_locks_preserves_index_during_status(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }

    def git(*args: str, guarded: bool = True) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            env={**env, "GIT_OPTIONAL_LOCKS": "0" if guarded else "1"},
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    git("-c", "init.templateDir=", "init", "-q")
    source = repo / "example.txt"
    source.write_text("unchanged content\n")
    git("add", "example.txt")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    index = repo / ".git/index"

    def index_state() -> tuple[str, int, int, int]:
        stat = index.stat()
        return (
            hashlib.sha256(index.read_bytes()).hexdigest(),
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    # Force a stat-cache miss without changing tracked content or assertions.
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    before = index_state()
    assert git("status", "--porcelain") == ""
    assert index_state() == before
    assert not (repo / ".git/index.lock").exists()

    # Control: the same command without the guard really does refresh this fixture.
    assert git("status", "--porcelain", guarded=False) == ""
    assert index_state()[0] != before[0]
    assert git("diff", "HEAD", "--", "example.txt") == ""
