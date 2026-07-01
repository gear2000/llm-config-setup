#!/usr/bin/env bash
# Install this kit's GENERAL skills AND slash-command skills into the user's HOME
# config — global, all-projects — ALONGSIDE (never replacing) the per-repo compose path.
#
# TWO skill families, same mechanism (a YAML recipe assembles layer .md files into a
# skill dir; the ONLY thing different here is the DESTINATION = home):
#   A. Convention skills — python, nextjs, backend, golang. Individual recipes under
#      .shared-llm/compose/global/, staged to examples/global-staging/skills/<name>/.
#   B. Slash-command skills — the .shared-llm/compose/slash-commands/ GROUP. These
#      are routed by recipe scope instead of blindly copied everywhere:
#        - Claude Code gets Claude-specific skills plus portable non-do-* skills.
#        - Pi gets portable/common skills, including do-*.
#        - Codex is kept on the existing common/codex route, but is not a focus.
#
# This script does two things:
#   1. compose  — run tools/harness.py on the recipes/group to (re)generate the staged
#                 skill dirs under examples/ (engine unmodified).
#   2. install  — COPY each staged skill DIRECTORY into only the home skill dirs that
#                 should receive it. A whole-directory copy (not just SKILL.md) lets
#                 bundled resources travel intact.
#
# Why COPY (not symlink): the staged file is a GENERATED artifact under examples/
# (gitignored). A symlink into home would dangle the moment staging is cleaned or
# regenerated, and would point at an ignored path. A copy leaves home self-consistent —
# a real file that survives staging cleanup. (harness.py sync symlinks because its
# sources are tracked, stable .ts files; a generated skill is the opposite case.)
#
# Safe-migration discipline (mirrors harness.py sync):
#   - Idempotent: re-running installs nothing new when home already matches.
#   - Never clobber a divergent/foreign file: an existing real file that does NOT match
#     the staged skill is left untouched with a warning. A foreign symlink is left alone.
#   - --uninstall reverses ONLY the current routed destinations and only when a home
#     skill is byte-identical to our staged copy. It never removes a foreign/divergent file.
#   - install prunes stale slash-command copies from destinations that no longer receive
#     that command, but only when the stale copy is byte-identical to this kit's staged
#     output. This removes old do-* installs from ~/.claude/skills safely.
# Fail loud: compose failure aborts; an occupied/divergent target is reported, never
# overwritten.
#
# Usage:
#   tools/install-global.sh             # compose + install into home
#   tools/install-global.sh --uninstall # remove only the skills this script currently routes

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$REPO_ROOT/tools/harness.py"
PYTHON_BIN="${PYTHON_BIN:-python3.14}"
# Convention recipes carry staging-relative outputs (global-staging/skills/<name>/SKILL.md).
# Slash-command recipes carry root-relative outputs (.claude/skills/<name>/SKILL.md).
# Stage BOTH under the gitignored examples/ dir by composing with --target examples,
# so the kit's own tree is never polluted, then copy each skill dir into routed home dirs.
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

# Home skill dirs by harness. Pi reads user skills from ~/.pi/agent/skills.
CLAUDE_SKILLS="$HOME/.claude/skills"
CODEX_SKILLS="$HOME/.codex/skills"
PI_SKILLS="$HOME/.pi/agent/skills"
ALL_HOME_SKILL_DIRS=("$CLAUDE_SKILLS" "$CODEX_SKILLS" "$PI_SKILLS")

# Removed standalone planner skills. /planish is the Pi standalone planner;
# /do-plan-and-grill and /cc-plan-and-grill are the workflow-suite planners.
DEPRECATED_SKILLS=("do-planish" "cc-planish")

# slash_harness_of <name> — derive recipe scope from compose/slash-commands/**/<name>.yaml.
# Mirrors tools/harness.py:harness_of(). For the current layout this returns
# "claude" for common/claude/<name>.yaml and "common" for common/common/<name>.yaml.
slash_harness_of() {
  local name="$1" hit
  hit="$(find "$SLASH_RECIPE_DIR" -type f -name "$name.yaml" -print -quit)"
  if [[ -z "$hit" ]]; then
    printf 'unknown'
    return
  fi
  basename "$(dirname "$hit")"
}

# slash_skill_bases <name> — populate global DEST_BASES with the home skill dirs
# this slash-command skill should be copied into.
slash_skill_bases() {
  local name="$1" scope
  scope="$(slash_harness_of "$name")"
  DEST_BASES=()
  case "$scope" in
    claude)
      DEST_BASES=("$CLAUDE_SKILLS")
      ;;
    common)
      # Pi gets every remaining portable/common command, including workflow-suite do-*.
      # Codex keeps the existing common route, but is not the focus of this change.
      DEST_BASES=("$PI_SKILLS" "$CODEX_SKILLS")
      # Claude Code also gets portable non-do-* commands such as qa/security, but
      # never receives do-*; workflow-suite commands have cc-* counterparts in Claude Code.
      if [[ "$name" != do-* ]]; then
        DEST_BASES+=("$CLAUDE_SKILLS")
      fi
      ;;
    codex)
      DEST_BASES=("$CODEX_SKILLS")
      ;;
    pi)
      DEST_BASES=("$PI_SKILLS")
      ;;
    *)
      echo "skip $name — cannot route slash-command skill (unknown recipe scope)" >&2
      ;;
  esac
}

contains_base() {
  local needle="$1" base
  shift
  for base in "$@"; do
    [[ "$base" == "$needle" ]] && return 0
  done
  return 1
}

# install_skill_dir_to_bases <staged_skill_dir> <name> <base>...
# Copy one staged skill DIRECTORY into selected home skill dirs, preserving
# safe-migration discipline. Bumps installed/uptodate/skipped in caller scope.
install_skill_dir_to_bases() {
  local staged_dir="$1" name="$2" base target_dir
  shift 2
  for base in "$@"; do
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

# uninstall_skill_dir_from_bases <staged_skill_dir> <name> <base>...
# Remove only byte-identical directories from the selected routed destinations.
uninstall_skill_dir_from_bases() {
  local staged_dir="$1" name="$2" base target_dir
  shift 2
  for base in "$@"; do
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

# prune_disallowed_skill_dir <staged_skill_dir> <name> <allowed-base>...
# On install, remove old copies from destinations that no longer route this command,
# but only if identical to staged.
prune_disallowed_skill_dir() {
  local staged_dir="$1" name="$2" base target_dir
  shift 2
  for base in "${ALL_HOME_SKILL_DIRS[@]}"; do
    contains_base "$base" "$@" && continue
    target_dir="$base/$name"
    [[ -e "$target_dir" || -L "$target_dir" ]] || continue
    if [[ -L "$target_dir" ]]; then
      echo "skip stale $target_dir — it's a symlink (foreign — leaving it)" >&2
      stale_kept=$((stale_kept + 1)); continue
    fi
    if [[ -d "$target_dir" ]] && diff -rq "$target_dir" "$staged_dir" >/dev/null 2>&1; then
      rm -rf "$target_dir"; stale_removed=$((stale_removed + 1)); echo "removed stale $target_dir"
    else
      echo "skip stale $target_dir — differs from this kit's staged skill (not ours)" >&2
      stale_kept=$((stale_kept + 1))
    fi
  done
}

remove_deprecated_skill_dirs() {
  local name base target_dir skill_file
  for name in "${DEPRECATED_SKILLS[@]}"; do
    for base in "${ALL_HOME_SKILL_DIRS[@]}"; do
      target_dir="$base/$name"
      skill_file="$target_dir/SKILL.md"
      [[ -e "$target_dir" || -L "$target_dir" ]] || continue
      if [[ -L "$target_dir" ]]; then
        echo "skip deprecated $target_dir — it's a symlink (foreign — leaving it)" >&2
        deprecated_kept=$((deprecated_kept + 1)); continue
      fi
      if [[ -f "$skill_file" ]] && grep -qE "^name:[[:space:]]*$name$" "$skill_file"; then
        rm -rf "$target_dir"
        deprecated_removed=$((deprecated_removed + 1)); echo "removed deprecated $target_dir"
      else
        echo "skip deprecated $target_dir — no matching SKILL.md name (not ours)" >&2
        deprecated_kept=$((deprecated_kept + 1))
      fi
    done
  done
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "error: $PYTHON_BIN not on PATH (set PYTHON_BIN=/path/to/python3.14)" >&2; exit 1; }
[[ -f "$COMPOSE" ]] || { echo "error: compose engine not found: $COMPOSE" >&2; exit 1; }
[[ -d "$SLASH_RECIPE_DIR" ]] || { echo "error: slash-command recipe dir missing: $SLASH_RECIPE_DIR" >&2; exit 1; }

# --- compose: (re)generate ALL staged skills (both families) from their recipes ---
# Done up front for BOTH install and uninstall, so dir-level comparison below
# always has a fresh reference copy to diff against.
for pair in "${GLOBAL_SKILLS[@]}"; do
  recipe="${pair##*:}"
  echo "compose: $recipe"
  # --target pins output under REPO_ROOT/examples/ (gitignored staging) regardless of
  # cwd; the engine finds the .shared-llm source by walking up from its own location.
  # fail loud — set -e.
  "$PYTHON_BIN" "$COMPOSE" compose "$recipe" --target "$STAGING_BASE"
done
echo "compose: slash-commands group ($SLASH_RECIPE_DIR)"
"$PYTHON_BIN" "$COMPOSE" compose "$SLASH_RECIPE_DIR" --target "$STAGING_BASE"
# Clear deprecated generated staging dirs left from older runs; their recipes are gone.
for deprecated in "${DEPRECATED_SKILLS[@]}"; do
  rm -rf "$SLASH_STAGING/$deprecated"
done

# Names of the slash-command skills = basenames of the staged dirs (handles colons,
# e.g. do-plan-and-grill / codex-delegate).
slash_staged_dirs=("$SLASH_STAGING"/*)
[[ -e "${slash_staged_dirs[0]}" ]] || { echo "error: no slash-command skills staged in $SLASH_STAGING" >&2; exit 1; }

# --- uninstall: remove only home skill dirs identical to our staged copy ---
if [[ "${1:-}" == "--uninstall" ]]; then
  removed=0; kept=0; deprecated_removed=0; deprecated_kept=0
  for pair in "${GLOBAL_SKILLS[@]}"; do
    name="${pair%%:*}"
    uninstall_skill_dir_from_bases "$STAGING/$name" "$name" "${ALL_HOME_SKILL_DIRS[@]}"
  done
  for staged_dir in "${slash_staged_dirs[@]}"; do
    name="$(basename "$staged_dir")"
    slash_skill_bases "$name"
    if [[ "${#DEST_BASES[@]}" -eq 0 ]]; then
      continue
    fi
    uninstall_skill_dir_from_bases "$staged_dir" "$name" "${DEST_BASES[@]}"
  done
  remove_deprecated_skill_dirs
  echo "global uninstall: $removed removed, $kept left untouched; $deprecated_removed deprecated removed, $deprecated_kept deprecated left untouched"
  exit 0
fi

# --- install: copy each staged skill DIRECTORY into the routed home dirs ---
installed=0; uptodate=0; skipped=0; stale_removed=0; stale_kept=0; deprecated_removed=0; deprecated_kept=0
for pair in "${GLOBAL_SKILLS[@]}"; do
  name="${pair%%:*}"
  staged_dir="$STAGING/$name"
  [[ -d "$staged_dir" ]] || { echo "error: staged skill missing after compose: $staged_dir" >&2; exit 1; }
  install_skill_dir_to_bases "$staged_dir" "$name" "${ALL_HOME_SKILL_DIRS[@]}"
done
for staged_dir in "${slash_staged_dirs[@]}"; do
  name="$(basename "$staged_dir")"
  slash_skill_bases "$name"
  if [[ "${#DEST_BASES[@]}" -eq 0 ]]; then
    skipped=$((skipped + 1)); continue
  fi
  install_skill_dir_to_bases "$staged_dir" "$name" "${DEST_BASES[@]}"
  prune_disallowed_skill_dir "$staged_dir" "$name" "${DEST_BASES[@]}"
done

remove_deprecated_skill_dirs

echo "global install: $installed copied, $uptodate already current, $skipped skipped (foreign/divergent); $stale_removed stale removed, $stale_kept stale left untouched; $deprecated_removed deprecated removed, $deprecated_kept deprecated left untouched"

cat <<'EOF'
verify:
  - Pi standalone planning: /planish from the TypeScript extension (planish.ts), linked by tools/harness.py sync.
  - Pi workflow suite: /do-research, /do-plan-and-grill, /do-oneshot, /do-implement, /do-loop, /do-full from ~/.pi/agent/skills.
  - Claude Code workflow suite: matching cc-* commands from ~/.claude/skills.
  - Removed standalone skill variants: /do-planish and /cc-planish are intentionally not installed.
EOF
