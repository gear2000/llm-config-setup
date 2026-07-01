#!/usr/bin/env bash
# Install this kit's GENERAL skills AND slash-command skills into the user's HOME
# config — global, all-projects — ALONGSIDE (never replacing) the per-repo compose path.
#
# TWO skill families, same mechanism (a YAML recipe assembles layer .md files into a
# skill dir; the ONLY thing different here is the DESTINATION = home):
#   A. Convention skills — python, nextjs, backend, golang. Individual recipes under
#      .shared-llm/compose/global/, staged to examples/global-staging/skills/<name>/.
#   B. Slash-command skills — the .shared-llm/compose/slash-commands/ GROUP (do-planish,
#      qa, security, playwright-cli, response, run-phase, …). Composed as a directory,
#      staged to examples/.claude/skills/<name>/. These were authored but historically
#      never wired into any installer — this script now ships them too.
#
# This script does two things:
#   1. compose  — run tools/harness.py on the recipes/group to (re)generate the staged
#                 skill dirs under examples/ (engine unmodified).
#   2. install  — COPY each staged skill DIRECTORY into the three home skill dirs every
#                 harness reads globally:
#                   ~/.claude/skills/<name>/        (SKILL.md + any bundled resources/)
#                   ~/.codex/skills/<name>/
#                   ~/.pi/agent/skills/<name>/      (Pi reads <agentDir>/skills, agentDir=~/.pi/agent)
#                 A whole-directory copy (not just SKILL.md) so a skill that ships
#                 reference files — e.g. playwright-cli's references/ — travels intact.
#
# Why COPY (not symlink): the staged file is a GENERATED artifact under examples/
# (gitignored). A symlink into home would dangle the moment staging is cleaned or
# regenerated, and would point at an ignored path. A copy leaves home self-consistent —
# a real file that survives staging cleanup. (harness.py sync symlinks because its sources
# are tracked, stable .ts files; a generated skill is the opposite case.)
#
# Safe-migration discipline (mirrors harness.py sync):
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
COMPOSE="$REPO_ROOT/tools/harness.py"
# Convention recipes carry staging-relative outputs (global-staging/skills/<name>/SKILL.md).
# Slash-command recipes carry root-relative outputs (.claude/skills/<name>/SKILL.md).
# Stage BOTH under the gitignored examples/ dir by composing with --target examples,
# so the kit's own tree is never polluted, then copy each skill dir into the home dirs.
STAGING_BASE="$REPO_ROOT/examples"
STAGING="$STAGING_BASE/global-staging/skills"          # family A lands here
SLASH_STAGING="$STAGING_BASE/.claude/skills"           # family B lands here

# Family A — convention skills: "<skill-name>:<recipe path relative to repo root>".
# Add a line here to ship another general skill globally.
GLOBAL_SKILLS=(
  "python:.shared-llm/compose/global/python.yaml"
  "nextjs:.shared-llm/compose/global/nextjs.yaml"
  "backend:.shared-llm/compose/global/backend.yaml"
  "golang:.shared-llm/compose/global/golang.yaml"
)

# Family B — slash-command skills: a whole recipe GROUP (directory). The engine
# recurses it and composes every recipe within (claude-scoped + portable alike).
SLASH_RECIPE_DIR="$REPO_ROOT/.shared-llm/compose/slash-commands"

# Home skill dirs each harness reads globally.
HOME_SKILL_DIRS=(
  "$HOME/.claude/skills"
  "$HOME/.codex/skills"
  "$HOME/.pi/agent/skills"
)

# install_skill_dir <staged_skill_dir> <name> — copy one staged skill DIRECTORY into
# every home skill dir, preserving safe-migration discipline (idempotent; never clobber
# a foreign symlink, a non-dir, or a divergent dir). Bumps the installed/uptodate/skipped
# counters in the caller's scope.
install_skill_dir() {
  local staged_dir="$1" name="$2" base target_dir
  for base in "${HOME_SKILL_DIRS[@]}"; do
    target_dir="$base/$name"
    if [[ -L "$target_dir" ]]; then
      echo "skip $name -> $target_dir is a symlink (foreign — leaving it)" >&2
      skipped=$((skipped + 1)); continue
    fi
    if [[ -d "$target_dir" ]]; then
      if diff -rq "$target_dir" "$staged_dir" >/dev/null 2>&1; then
        uptodate=$((uptodate + 1)); continue
      fi
      echo "skip $name -> $target_dir exists and differs (not ours — back it up and re-run)" >&2
      skipped=$((skipped + 1)); continue
    fi
    if [[ -e "$target_dir" ]]; then
      echo "skip $name -> $target_dir exists as a non-directory (foreign — leaving it)" >&2
      skipped=$((skipped + 1)); continue
    fi
    mkdir -p "$base"
    cp -R "$staged_dir" "$target_dir"
    installed=$((installed + 1)); echo "installed $name -> $target_dir"
  done
}

command -v python3 >/dev/null 2>&1 || { echo "error: python3 not on PATH" >&2; exit 1; }
[[ -f "$COMPOSE" ]] || { echo "error: compose engine not found: $COMPOSE" >&2; exit 1; }
[[ -d "$SLASH_RECIPE_DIR" ]] || { echo "error: slash-command recipe dir missing: $SLASH_RECIPE_DIR" >&2; exit 1; }

# --- compose: (re)generate ALL staged skills (both families) from their recipes ---
# Done up front for BOTH install and uninstall, so the dir-level comparison below
# always has a fresh reference copy to diff against.
for pair in "${GLOBAL_SKILLS[@]}"; do
  recipe="${pair##*:}"
  echo "compose: $recipe"
  # --target pins output under REPO_ROOT/examples/ (gitignored staging) regardless of
  # cwd; the engine finds the .shared-llm source by walking up from its own location.
  # fail loud — set -e.
  python3 "$COMPOSE" compose "$recipe" --target "$STAGING_BASE"
done
echo "compose: slash-commands group ($SLASH_RECIPE_DIR)"
python3 "$COMPOSE" compose "$SLASH_RECIPE_DIR" --target "$STAGING_BASE"

# Names of the slash-command skills = basenames of the staged dirs (handles colons,
# e.g. do-planish / codex-delegate).
slash_staged_dirs=("$SLASH_STAGING"/*)
[[ -e "${slash_staged_dirs[0]}" ]] || { echo "error: no slash-command skills staged in $SLASH_STAGING" >&2; exit 1; }

# --- uninstall: remove only home skill dirs identical to our staged copy ---
if [[ "${1:-}" == "--uninstall" ]]; then
  removed=0; kept=0
  uninstall_skill_dir() {
    local staged_dir="$1" name="$2" base target_dir
    for base in "${HOME_SKILL_DIRS[@]}"; do
      target_dir="$base/$name"
      [[ -e "$target_dir" || -L "$target_dir" ]] || continue
      if [[ -L "$target_dir" ]]; then
        echo "skip uninstall $target_dir — it's a symlink (not installed by this script)" >&2
        kept=$((kept + 1)); continue
      fi
      if [[ -d "$target_dir" ]] && diff -rq "$target_dir" "$staged_dir" >/dev/null 2>&1; then
        rm -rf "$target_dir"; removed=$((removed + 1)); echo "uninstalled $target_dir"
      else
        echo "skip uninstall $target_dir — differs from this kit's staged skill (not ours)" >&2
        kept=$((kept + 1))
      fi
    done
  }
  for pair in "${GLOBAL_SKILLS[@]}"; do
    name="${pair%%:*}"; uninstall_skill_dir "$STAGING/$name" "$name"
  done
  for staged_dir in "${slash_staged_dirs[@]}"; do
    uninstall_skill_dir "$staged_dir" "$(basename "$staged_dir")"
  done
  echo "global uninstall: $removed removed, $kept left untouched"
  exit 0
fi

# --- install: copy each staged skill DIRECTORY into the three home dirs ---
installed=0; uptodate=0; skipped=0
for pair in "${GLOBAL_SKILLS[@]}"; do
  name="${pair%%:*}"
  staged_dir="$STAGING/$name"
  [[ -d "$staged_dir" ]] || { echo "error: staged skill missing after compose: $staged_dir" >&2; exit 1; }
  install_skill_dir "$staged_dir" "$name"
done
for staged_dir in "${slash_staged_dirs[@]}"; do
  install_skill_dir "$staged_dir" "$(basename "$staged_dir")"
done

echo "global install: $installed copied, $uptodate already current, $skipped skipped (foreign/divergent)"

# NOTE: Claude Code merged custom commands INTO skills — a skill at
# ~/.claude/skills/<name>/SKILL.md already creates the typeable /<name>. So there is
# no separate ~/.claude/commands/ step: installing the slash-command SKILLS above is
# what makes /qa, /do-planish, /security, … available. Skill/command names MUST be
# hyphenated (do-planish), NOT colon-namespaced — user-level colons are reserved for
# plugins, and Pi rejects them too; that is why every name here uses hyphens.
echo "verify: /qa, /do-planish, /security, … work in Claude Code (skills create the command); skills also load in Codex and Pi"
