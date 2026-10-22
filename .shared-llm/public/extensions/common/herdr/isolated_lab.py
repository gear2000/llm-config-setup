#!/usr/bin/env python3
"""Reproduce a Herdr 0.9.5 lab without deploying into the operator's home.

  python3 isolated_lab.py init /var/tmp/herdr095-lab --binary /path/to/herdr
  python3 isolated_lab.py exec /var/tmp/herdr095-lab -- herdr status --json
  python3 isolated_lab.py exec /var/tmp/herdr095-lab -- just upagent help

The release binary must already be downloaded. Initialization verifies its published
digest, copies credentials (never links them), installs only lab integrations, and
provisions a guarded named server. Keep the lab for evidence; explicit teardown uses
lab_session.py's ownership and fleet checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import site
import subprocess
import sys

import lab_session

RELEASE_SHA256 = "14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d"
MANIFEST = "isolated-lab.json"
CHECKOUT = Path(__file__).resolve().parents[5]


def environment(root: Path, manifest: dict[str, str]) -> dict[str, str]:
    """Replace routing and writable harness roots; do not inherit the caller's pane."""
    root = root.resolve(strict=True)
    home = root / "home"
    if home.is_symlink() or home.resolve() == Path(pwd.getpwuid(os.getuid()).pw_dir):
        raise lab_session.LabError("lab home must be separate from the login home")
    if not home.is_dir() or home.stat().st_uid != os.getuid():
        raise lab_session.LabError("lab home must be an owned directory")
    session = lab_session._validate_lab_session(manifest["session"])
    socket = home / ".config/herdr/sessions" / session / "herdr.sock"
    if len(os.fsencode(socket)) >= 108:
        raise lab_session.LabError(
            "lab socket path is too long; use a shorter lab root"
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("HERDR_", "UPAGENT_", "CODEX_", "CLAUDE_", "CURSOR_", "PI_")
        )
        and key not in {"CLAUDECODE", "BASH_ENV", "ENV"}
    }
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "TMPDIR": str(root / "tmp"),
            "CODEX_HOME": str(home / ".codex"),
            "CURSOR_CONFIG_DIR": str(home / ".cursor"),
            "PI_CODING_AGENT_DIR": str(home / ".pi/agent"),
            "HERDR_CONFIG_PATH": str(home / ".config/herdr/config.toml"),
            "HERDR_SESSION": session,
            "HERDR_SOCKET_PATH": str(socket),
            "HERDR_DISABLE_SOUND": "1",
            "UPAGENT_STATE": str(root / "state/services.json"),
            "UPAGENT_HUB_DIR": str(root / "state/ledger"),
            "UPAGENT_RUNTIME_DIR": str(root / "runtime/upagent"),
            "UPAGENT_CANONICAL_REPO": manifest["checkout"],
            "LLM_HERDR_LAB_STATE_DIR": str(root / "ownership"),
            "PYTHONPYCACHEPREFIX": str(root / "cache/pycache"),
            "PYTHONPATH": manifest["pythonpath"],
            "DISABLE_AUTOUPDATER": "1",
            "PATH": str(root / "bin") + os.pathsep + env["PATH"],
        }
    )
    return env


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def initialize(root: Path, binary: Path, session: str, auth_home: Path) -> None:
    session = lab_session._validate_lab_session(session)
    binary = binary.resolve(strict=True)
    if hashlib.sha256(binary.read_bytes()).hexdigest() != RELEASE_SHA256:
        raise lab_session.LabError(
            "binary does not match the Herdr v0.9.5 Linux x86_64 release digest"
        )
    # Reusing a directory could adopt a server or overwrite a handwritten configuration.
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    root = root.resolve(strict=True)
    home = root / "home"
    for directory in (
        home,
        root / "bin",
        root / "state",
        root / "runtime",
        root / "cache",
        root / "tmp",
        root / "ownership",
        root / "work",
        root / "evidence",
    ):
        directory.mkdir(mode=0o700)
    shutil.copyfile(binary, root / "bin/herdr")
    (root / "bin/herdr").chmod(0o700)
    manifest = {
        "session": session,
        "checkout": str(CHECKOUT),
        "pythonpath": site.getusersitepackages(),
    }
    env = environment(root, manifest)
    _write_json(root / MANIFEST, manifest)
    # Copy only authentication, not production settings, hooks, or session state. OAuth
    # refreshes may replace the lab copy; there is no symlink back into the source home.
    for relative in (
        ".claude/.credentials.json",
        ".codex/auth.json",
        ".pi/agent/auth.json",
    ):
        destination = home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(auth_home / relative, destination)
        destination.chmod(0o400)
    cursor_auth = auth_home / ".config/cursor/auth.json"
    if cursor_auth.is_file():
        destination = home / ".config/cursor/auth.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cursor_auth, destination)
        destination.chmod(0o400)
    claude = json.loads((auth_home / ".claude.json").read_text())
    claude_keys = (
        "oauthAccount",
        "hasCompletedOnboarding",
        "lastOnboardingVersion",
        "hasAvailableSubscription",
        "bypassPermissionsModeAccepted",
    )
    _write_json(
        home / ".claude.json",
        {
            **{key: claude[key] for key in claude_keys if key in claude},
            "autoUpdates": False,
            "projects": {str(root / "work"): {"hasTrustDialogAccepted": True}},
        },
    )
    _write_json(home / ".claude/settings.json", {})
    personas = CHECKOUT / ".claude/agents"
    if personas.is_dir():
        target = home / ".claude/agents"
        target.mkdir()
        for persona in personas.glob("*.md"):
            shutil.copyfile(persona, target / persona.name)
    _write_json(home / ".pi/agent/settings.json", {"packages": []})
    (home / ".cursor").mkdir()
    (home / ".codex/config.toml").write_text(
        'check_for_update_on_startup = false\ncli_auth_credentials_store = "file"\n'
    )
    config = home / ".config/herdr/config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "onboarding = false\n[server]\nheadless_cols = 160\nheadless_rows = 50\n"
        '[terminal]\ndefault_shell = "/bin/bash"\nshell_mode = "non_login"\n'
        "[update]\nversion_check = false\nmanifest_check = false\n"
        "[session]\nresume_agents_on_restore = false\n[ui.sound]\nenabled = false\n"
    )
    for harness in ("pi", "claude", "codex", "cursor"):
        subprocess.run(
            ["herdr", "integration", "install", harness], env=env, check=True
        )
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("lab_session.py")),
            "provision",
            session,
        ],
        env=env,
        check=True,
    )
    print(f"Lab ready at {root}; all writable harness files and state are beneath it.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("root", type=Path)
    init.add_argument("--binary", type=Path, required=True)
    init.add_argument("--session", default="llm-lab-095")
    init.add_argument("--auth-home", type=Path, default=Path.home())
    run = sub.add_parser("exec")
    run.add_argument("root", type=Path)
    run.add_argument("command", nargs=argparse.REMAINDER)
    stop = sub.add_parser("teardown")
    stop.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.action == "init":
        initialize(args.root, args.binary, args.session, args.auth_home)
        return 0
    root = args.root.resolve(strict=True)
    manifest = json.loads((root / MANIFEST).read_text())
    env = environment(root, manifest)
    if args.action == "teardown":
        command = [
            sys.executable,
            str(Path(__file__).with_name("lab_session.py")),
            "teardown",
            manifest["session"],
        ]
    else:
        command = args.command
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            parser.error("exec requires a command after --")
    os.chdir(manifest["checkout"])
    os.execvpe(command[0], command, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
