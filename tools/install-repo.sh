#!/usr/bin/env bash
# install-repo.sh — set up a TARGET repo to use this kit standalone. Driven by
# `task install repo -- <dir=.>`.
#
# Makes the target SELF-CONTAINED: it copies the portable .shared-llm/ layer tree,
# the compose engine (tools/compose-layers.py), and a thin Taskfile into <dir> so
# the repo can recompose on its own — no dependency on a central engine or on the
# ~/.local/bin/llm-compose wrapper being installed. (The wrapper still works against
# it; it is a convenience, not a requirement.)
#
# Steps:
#   1. Copy .shared-llm/ + tools/compose-layers.py + a thin tools/llm.Taskfile.yml
#      into <dir>. The this_repo layer files arrive as fillable TEMPLATE.* stubs.
#   2. INTERACTIVE PAUSE: print the TEMPLATE stub list the user must fill, then wait.
#      Bypass for automation: set INSTALL_REPO_YES=1 (or pipe a non-tty stdin) to
#      proceed without waiting.
#   3. Compose against <dir> (engine --target <dir>) to generate CLAUDE.md/AGENTS.md
#      at the REPO ROOT, plus the skills and the 18 agents at their proper paths
#      (.claude/skills/<name>/SKILL.md, .claude/agents/<name>.md). Only the
#      CONSUMER-relevant recipe groups are composed — NOT the home-only `global/`
#      skills (those install via `task install local`) and NOT the
#      `example-package` / `example-service` DEMO recipes (illustrative samples,
#      kept out of the consumer's real tree). If TEMPLATE.* stubs are still
#      unfilled, compose is SKIPPED (you cannot compose against unfilled stubs)
#      and the remaining stubs are reported.
#   4. Print a SUMMARY: what was generated + what is NOT installed (home pieces come
#      from `task install local`).
#
# What this does NOT install: home / all-projects pieces (global skills, the home
# agent personas, Pi runtime, the ~/.local/bin wrapper). Those come from
# `task install local`. The summary states this explicitly.
#
# Safe-migration discipline: never clobber an existing .shared-llm/ or engine in the
# target — if <dir> already has them, they are left untouched with a warning (re-run
# against a clean dir, or remove them first). Fail loud on a bad target.
#
# Usage:
#   tools/install-repo.sh                 # target = current directory
#   tools/install-repo.sh /path/to/repo
#   INSTALL_REPO_YES=1 tools/install-repo.sh /path/to/repo   # non-interactive

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_SHARED="$REPO_ROOT/.shared-llm"
SRC_ENGINE="$REPO_ROOT/tools/compose-layers.py"
SRC_TASKFILE="$REPO_ROOT/tools/templates/llm.Taskfile.yml"

TARGET="${1:-.}"
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd)"

[[ -d "$SRC_SHARED" ]] || { echo "error: source .shared-llm not found: $SRC_SHARED" >&2; exit 1; }
[[ -f "$SRC_ENGINE" ]] || { echo "error: compose engine not found: $SRC_ENGINE" >&2; exit 1; }
[[ -f "$SRC_TASKFILE" ]] || { echo "error: thin Taskfile template not found: $SRC_TASKFILE" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not on PATH" >&2; exit 1; }

echo "=============================================================="
echo " install repo — set up target: $TARGET"
echo "=============================================================="

# ---------------------------------------------------------------------------
# 1. Copy the portable tree + engine + thin Taskfile (never clobber)
# ---------------------------------------------------------------------------
echo
echo ">>> [1/3] Copy .shared-llm/ + engine + thin Taskfile into target"

if [[ -e "$TARGET/.shared-llm" ]]; then
  echo "skip .shared-llm -> $TARGET/.shared-llm already exists (leaving it; remove it to re-init)" >&2
else
  cp -R "$SRC_SHARED" "$TARGET/.shared-llm"
  echo "copied .shared-llm/ -> $TARGET/.shared-llm"
fi

mkdir -p "$TARGET/tools"
if [[ -e "$TARGET/tools/compose-layers.py" ]] && ! cmp -s "$TARGET/tools/compose-layers.py" "$SRC_ENGINE"; then
  echo "skip engine -> $TARGET/tools/compose-layers.py exists and differs (leaving it)" >&2
else
  cp "$SRC_ENGINE" "$TARGET/tools/compose-layers.py"; chmod +x "$TARGET/tools/compose-layers.py"
  echo "copied engine -> $TARGET/tools/compose-layers.py"
fi

if [[ -e "$TARGET/Taskfile.yml" ]]; then
  echo "note: $TARGET/Taskfile.yml exists — installing the compose Taskfile alongside as tools/llm.Taskfile.yml" >&2
  cp "$SRC_TASKFILE" "$TARGET/tools/llm.Taskfile.yml"
  echo "copied thin Taskfile -> $TARGET/tools/llm.Taskfile.yml (include it, or run: task -t tools/llm.Taskfile.yml compose:all)"
else
  cp "$SRC_TASKFILE" "$TARGET/Taskfile.yml"
  echo "copied thin Taskfile -> $TARGET/Taskfile.yml"
fi

# ---------------------------------------------------------------------------
# 2. List TEMPLATE stubs + interactive pause (with bypass)
# ---------------------------------------------------------------------------
echo
echo ">>> [2/3] Fill the TEMPLATE stubs"

list_stubs() { find "$TARGET/.shared-llm" -name 'TEMPLATE.*' | sort; }

mapfile -t STUBS < <(list_stubs)
if [[ "${#STUBS[@]}" -eq 0 ]]; then
  echo "no TEMPLATE.* stubs found (already filled?)"
else
  echo "The following template stubs must be filled in, then renamed to drop the"
  echo "'TEMPLATE.' prefix (e.g. TEMPLATE.general.md -> general.md):"
  echo
  for s in "${STUBS[@]}"; do echo "  - ${s#"$TARGET"/}"; done
  echo
  echo "See ONBOARDING.md (copied into the kit) for the token-by-token fill guide."
fi

if [[ "${INSTALL_REPO_YES:-}" == "1" ]]; then
  echo "INSTALL_REPO_YES=1 — proceeding non-interactively (no wait)."
elif [[ ! -t 0 ]]; then
  echo "stdin is not a TTY — proceeding non-interactively (no wait)."
else
  read -r -p "Press Enter once the stubs above are filled and renamed (Ctrl-C to abort)... " _
fi

# ---------------------------------------------------------------------------
# 3. Compose against the target (only if stubs are filled), then summarize
# ---------------------------------------------------------------------------
echo
echo ">>> [3/3] Compose against target"

mapfile -t REMAINING < <(list_stubs)
composed=0
if [[ "${#REMAINING[@]}" -gt 0 ]]; then
  echo "Skipping compose — ${#REMAINING[@]} TEMPLATE.* stub(s) still unfilled:" >&2
  for s in "${REMAINING[@]}"; do echo "  - ${s#"$TARGET"/}" >&2; done
  echo "Fill + rename them, then run:  task -d $TARGET compose:all  (or: cd $TARGET && llm-compose)" >&2
else
  echo "All stubs filled — composing consumer recipes into $TARGET (outputs land at the repo root) ..."
  # Compose ONLY the consumer-relevant recipe groups so the outputs land at the
  # consumer's root (CLAUDE.md, AGENTS.md, .claude/skills/*, .claude/agents/*).
  # Deliberately excluded:
  #   - compose/global/*       home-only skills (install via `task install local`)
  #   - the example-package / example-service DEMO recipes under compose/claude-md/
  #     (illustrative samples — not consumer deliverables; never written into the
  #     consumer's real src/ tree).
  ENGINE="$TARGET/tools/compose-layers.py"
  python3 "$ENGINE" --shared-llm "$TARGET/.shared-llm" --target "$TARGET" "$TARGET/.shared-llm/compose/claude-md/root.yaml"
  python3 "$ENGINE" --shared-llm "$TARGET/.shared-llm" --target "$TARGET" "$TARGET/.shared-llm/compose/agents-md/root.yaml"
  python3 "$ENGINE" --shared-llm "$TARGET/.shared-llm" --target "$TARGET" "$TARGET/.shared-llm/compose/skills"
  python3 "$ENGINE" --shared-llm "$TARGET/.shared-llm" --target "$TARGET" "$TARGET/.shared-llm/compose/agents"
  composed=1
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "=============================================================="
echo " install repo — SUMMARY"
echo "=============================================================="
echo "Target: $TARGET"
echo
echo "INSTALLED into the target repo (self-contained):"
echo "  - .shared-llm/        the portable layer tree (this_repo = TEMPLATE.* stubs)"
echo "  - tools/compose-layers.py   the compose engine (recompose standalone)"
echo "  - Taskfile / tools/llm.Taskfile.yml   thin compose targets"
if [[ "$composed" -eq 1 ]]; then
  echo
  echo "GENERATED (composed against the filled stubs):"
  # Show the freshly generated outputs (recipe output: paths, target-relative).
  for f in CLAUDE.md AGENTS.md; do
    [[ -f "$TARGET/$f" ]] && echo "  - $f"
  done
  find "$TARGET/src" -name 'CLAUDE.md' 2>/dev/null | sed "s#$TARGET/#  - #" || true
  find "$TARGET/.claude/skills" -name 'SKILL.md' 2>/dev/null | sed "s#$TARGET/#  - #" || true
  find "$TARGET/.claude/agents" -name '*.md' 2>/dev/null | sed "s#$TARGET/#  - #" || true
else
  echo
  echo "NOT YET GENERATED: CLAUDE.md / AGENTS.md — fill the stubs above, then compose."
fi
echo
echo "NOT installed here (these come from \`task install local\`):"
echo "  - global home skills (~/.claude, ~/.codex, ~/.pi /skills)"
echo "  - the generic agent personas in ~/.claude/agents, ~/.pi/agents"
echo "  - Pi runtime (~/.pi extensions + agents)"
echo "  - the ~/.local/bin/llm-compose wrapper"
