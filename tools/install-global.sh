#!/usr/bin/env bash
# Install this kit's GENERAL convention skills into the user's HOME config — global,
# all-projects — ALONGSIDE (never replacing) the per-repo compose path.
#
# Same compose mechanism as the per-repo recipes: a YAML recipe assembles layer .md
# files into a SKILL.md. The ONLY thing different here is the DESTINATION = home.
# This script does two things:
#   1. compose  — run tools/compose-layers.py on the global recipe(s) to (re)generate
#                 the staged SKILL.md under examples/global-staging/ (engine unmodified).
#   2. install  — COPY each staged skill into the three home skill dirs every harness
#                 reads globally:
#                   ~/.claude/skills/<name>/SKILL.md
#                   ~/.codex/skills/<name>/SKILL.md
#                   ~/.pi/skills/<name>/SKILL.md
#
# Why COPY (not symlink): the staged file is a GENERATED artifact under examples/
# (gitignored). A symlink into home would dangle the moment staging is cleaned or
# regenerated, and would point at an ignored path. A copy leaves home self-consistent —
# a real file that survives staging cleanup. (setup-pi.sh symlinks because its sources
# are tracked, stable .ts files; a generated skill is the opposite case.)
#
# Safe-migration discipline (mirrors setup-pi.sh):
#   - Idempotent: re-running installs nothing new when home already matches.
#   - Never clobber a divergent/foreign file: an existing real file that does NOT match
#     the staged skill is left untouched with a warning (e.g. a project skill symlinked
#     into ~/.codex/skills/python by other tooling). A foreign symlink is left alone.
#   - --uninstall reverses ONLY what this script installed (a home skill byte-identical
#     to our staged copy). It never removes a foreign/divergent file.
# Fail loud: compose failure aborts; an occupied/divergent target is reported, never
# overwritten.
#
# Usage:
#   tools/install-global.sh             # compose + install into home
#   tools/install-global.sh --uninstall # remove only the skills this script installed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$REPO_ROOT/tools/compose-layers.py"
STAGING="$REPO_ROOT/examples/global-staging/skills"

# Global skills to install: "<skill-name>:<recipe path relative to repo root>".
# Add a line here to ship another general skill globally.
GLOBAL_SKILLS=(
  "python:layers/compose/global/python.yaml"
  "nextjs:layers/compose/global/nextjs.yaml"
  "backend:layers/compose/global/backend.yaml"
)

# Home skill dirs each harness reads globally.
HOME_SKILL_DIRS=(
  "$HOME/.claude/skills"
  "$HOME/.codex/skills"
  "$HOME/.pi/skills"
)

# --- uninstall: remove only home skills byte-identical to our staged copy ---
if [[ "${1:-}" == "--uninstall" ]]; then
  removed=0; kept=0
  for pair in "${GLOBAL_SKILLS[@]}"; do
    name="${pair%%:*}"
    staged="$STAGING/$name/SKILL.md"
    for base in "${HOME_SKILL_DIRS[@]}"; do
      target_dir="$base/$name"; target="$target_dir/SKILL.md"
      [[ -e "$target" || -L "$target" ]] || continue
      if [[ -L "$target" ]]; then
        echo "skip uninstall $target — it's a symlink (not installed by this script)" >&2
        kept=$((kept + 1)); continue
      fi
      if [[ -f "$staged" ]] && cmp -s "$target" "$staged"; then
        rm -f "$target"; removed=$((removed + 1)); echo "uninstalled $target"
        # Remove the skill dir if now empty (and not a symlink itself).
        [[ -d "$target_dir" && ! -L "$target_dir" ]] && rmdir "$target_dir" 2>/dev/null || true
      else
        echo "skip uninstall $target — differs from this kit's staged skill (not ours)" >&2
        kept=$((kept + 1))
      fi
    done
  done
  echo "global uninstall: $removed removed, $kept left untouched"
  exit 0
fi

# --- compose: (re)generate the staged skills from their recipes ---
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not on PATH" >&2; exit 1; }
[[ -f "$COMPOSE" ]] || { echo "error: compose engine not found: $COMPOSE" >&2; exit 1; }

for pair in "${GLOBAL_SKILLS[@]}"; do
  recipe="${pair##*:}"
  echo "compose: $recipe"
  python3 "$COMPOSE" "$recipe"   # fail loud — set -e aborts on non-zero
done

# --- install: copy each staged skill into the three home dirs ---
installed=0; uptodate=0; skipped=0
for pair in "${GLOBAL_SKILLS[@]}"; do
  name="${pair%%:*}"
  staged="$STAGING/$name/SKILL.md"
  if [[ ! -f "$staged" ]]; then
    echo "error: staged skill missing after compose: $staged" >&2; exit 1
  fi
  for base in "${HOME_SKILL_DIRS[@]}"; do
    target_dir="$base/$name"; target="$target_dir/SKILL.md"

    if [[ -L "$target" ]]; then
      echo "skip $name -> $target exists as a symlink (foreign — leaving it)" >&2
      skipped=$((skipped + 1)); continue
    fi
    if [[ -L "$target_dir" ]]; then
      echo "skip $name -> $target_dir is a symlinked dir (foreign — leaving it)" >&2
      skipped=$((skipped + 1)); continue
    fi
    if [[ -e "$target" ]]; then
      if cmp -s "$target" "$staged"; then uptodate=$((uptodate + 1)); continue
      else
        echo "skip $name -> $target exists and differs (not ours — back it up and re-run)" >&2
        skipped=$((skipped + 1)); continue
      fi
    fi

    mkdir -p "$target_dir"
    cp "$staged" "$target"
    installed=$((installed + 1)); echo "installed $name -> $target"
  done
done

echo "global install: $installed copied, $uptodate already current, $skipped skipped (foreign/divergent)"
echo "verify: a 'python' skill is available globally in Claude Code, Codex, and Pi"
