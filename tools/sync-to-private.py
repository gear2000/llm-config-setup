#!/usr/bin/env python3.14
"""Pull common/ layer changes made in this kit back into a private consumer repo.

Auto-diffs this repo's common/ layer trees against the private repo, but only
UPDATES files the private repo already has at the same path — it never creates
a new file there. A public common/ file the private repo doesn't have yet is
reported instead of copied: the private repo may not wire it in at all (a
generic file that happens to live under common/ but has no private recipe), or
adopting it may need a new compose recipe or a harness.py sync entry — that's a
human decision, not something safe to guess. This also means an already-mirrored
file (e.g. a Pi extension used via `harness.py sync` symlinks, not a compose
recipe) still gets kept current, since it's present-and-matching on the private
side regardless of which mechanism wires it in.

Never reads or writes anything under a `this_repo/` path segment, and never
touches compose/*.yaml recipe files — private recipes often mix a this_repo
overlay input into the same file (e.g. `architecture.yaml` adds a this_repo
description on top of the common body); overwriting them from the public
(common-only) version would silently delete that overlay wiring.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SYNC_ROOTS = [
    ".shared-llm/layers/agents/common",
    ".shared-llm/layers/llm/common",
    ".shared-llm/layers/skills/common",
    ".shared-llm/layers/slash-commands/common",
    ".shared-llm/llm/claude/common",
    ".shared-llm/llm/pi/common",
]


def iter_common_files(root: Path):
    for sync_root in SYNC_ROOTS:
        base = root / sync_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root)
                if "this_repo" not in rel.parts:  # defense in depth
                    yield rel


def private_dirty_lines(private_root: Path) -> list:
    result = subprocess.run(
        ["git", "-C", str(private_root), "status", "--short", "--", ".shared-llm"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def run_task(private_root: Path, target: str) -> int:
    print(f"\n--- task -d {private_root} {target} ---")
    return subprocess.run(["task", "-d", str(private_root), target]).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="../jiffy-rewrite-2026")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — copy nothing, run no compose")
    parser.add_argument("--force", action="store_true", help="Proceed even if the private repo has uncommitted .shared-llm/ changes")
    args = parser.parse_args()

    public_root = Path(__file__).resolve().parent.parent
    private_root = Path(args.private_root).resolve()

    if not private_root.is_dir():
        sys.exit(f"ERROR: private repo not found at {private_root}\nOverride with --private-root /path/to/jiffy-rewrite-2026")

    dirty = private_dirty_lines(private_root)
    if dirty and not args.force:
        print(f"ERROR: {private_root} has uncommitted .shared-llm/ changes — commit or stash first, or pass --force:")
        for line in dirty:
            print(f"  {line}")
        sys.exit(1)

    updated_files, new_files = [], []
    for rel in iter_common_files(public_root):
        src = public_root / rel
        dest = private_root / rel
        if not dest.exists():
            new_files.append(rel)
        elif src.read_bytes() != dest.read_bytes():
            updated_files.append(rel)

    print(f"Updated (private already has this file): {len(updated_files)}")
    for rel in updated_files:
        print(f"  UPDATED {rel}")
    if new_files:
        print(f"\nNew in public, private doesn't have it — not copied, needs a human call ({len(new_files)}):")
        for rel in new_files:
            print(f"  {rel}")

    if not updated_files:
        print("\nNothing to update — every file private already has matches.")
        return

    if args.dry_run:
        print("\n--dry-run: no files copied, no compose run.")
        return

    for rel in updated_files:
        dest = private_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(public_root / rel, dest)
    print(f"\nCopied {len(updated_files)} file(s) into {private_root}")

    if run_task(private_root, "compose:all") != 0:
        sys.exit(f"ERROR: task compose:all failed in {private_root}")
    if run_task(private_root, "compose:check") != 0:
        sys.exit(f"ERROR: task compose:check failed in {private_root} — composed outputs did not regenerate cleanly")

    print("\n--- private repo diff ---")
    subprocess.run(["git", "-C", str(private_root), "diff", "--stat"])
    print("\nDone. Review the diff above, then commit in the private repo.")


if __name__ == "__main__":
    main()
