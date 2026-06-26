#!/usr/bin/env bash
# Wire this kit's Pi harness runtime config into ~/.pi/ so Pi can discover it.
# Run once per machine after cloning. Idempotent — safe to re-run.
#
# All entries under .shared-llm/llm/pi/common/extensions/ are auto-discovered:
#   *.test.ts files                  — excluded (test files, not runtime)
#   *-hub.ts / hub-*.ts flat files   — ~/.pi/extensions/        (hub / `pi -e` scope)
#   all other flat .ts files         — ~/.pi/agent/extensions/   (agent-scoped, auto-loaded)
#   all directories                  — ~/.pi/agent/extensions/   (Pi loads <dir>/index.ts)
#
# Adding a new extension to .shared-llm/llm/pi/common/extensions/ is enough —
# re-running this script picks it up. No PI_EXT_MAP or PI_EXT_DIRS list to update.
#
# agents/*.md         -> ~/.pi/agents/             (symlink)
# settings.template.json -> ~/.pi/agent/settings.json (scaffolded once, never clobbered)
#
# Third-party extensions are NOT handled here — they are installed from source
# via `pi install` driven by tools/install-pi-extensions.sh (pinned list in
# .shared-llm/llm/pi/common/third-party-extensions.txt). This script calls that installer
# at the end. See .shared-llm/llm/pi/common/THIRD-PARTY-EXTENSIONS.md.
#
# Usage:
#   tools/setup-pi.sh             # wire everything up
#   tools/setup-pi.sh --unlink    # remove the symlinks created earlier
#   tools/setup-pi.sh --force     # re-create all managed symlinks (use after git pull)
#
# --force never clobbers foreign (non-managed) symlinks or real files.
# --unlink never removes the live settings.json or installed third-party extensions.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_SRC="$REPO_ROOT/.shared-llm/llm/pi/common"
EXT_SRC="$PI_SRC/extensions"
AGENTS_SRC="$PI_SRC/agents"
SETTINGS_TEMPLATE="$PI_SRC/settings.template.json"
EXT_INSTALLER="$REPO_ROOT/tools/install-pi-extensions.sh"

HOME_PI="$HOME/.pi"
PI_AGENT_DIR="$HOME_PI/agent"
PI_AGENTS_DIR="$HOME_PI/agents"
PI_SETTINGS="$PI_AGENT_DIR/settings.json"
PI_AGENT_EXT_DIR="$PI_AGENT_DIR/extensions"   # agent-scoped (auto-loaded)
PI_HUB_EXT_DIR="$HOME_PI/extensions"           # top-level (hub / `pi -e`)

FORCE=false
UNLINK=false
for _arg in "$@"; do
  case "$_arg" in
    --force)  FORCE=true ;;
    --unlink) UNLINK=true ;;
    *) echo "unknown argument: $_arg" >&2; exit 1 ;;
  esac
done
unset _arg

# A symlink is "ours" iff it resolves into this clone's .shared-llm/llm/pi/common tree.
is_ours() {  # link_target resolved_target
  case "$1" in "$PI_SRC"/*) return 0 ;; esac
  case "$2" in "$PI_SRC"/*) return 0 ;; esac
  return 1
}

# Destination dir for a flat .ts extension: hub scope or agent scope.
ext_dest_dir() {
  case "$1" in
    *-hub.ts|hub-*.ts) echo "$PI_HUB_EXT_DIR" ;;
    *) echo "$PI_AGENT_EXT_DIR" ;;
  esac
}

# Remove a managed symlink (skip if foreign or not a symlink).
_try_unlink() {
  local target="$1"
  if [[ -L "$target" ]]; then
    local lt rt
    lt="$(readlink "$target")"; rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
    if is_ours "$lt" "$rt"; then rm "$target"; echo "unlinked $target"; fi
  fi
}

if [[ "$UNLINK" == "true" ]]; then
  for _e in "$EXT_SRC"/*.ts; do
    [[ -f "$_e" ]] || continue; _n="$(basename "$_e")"
    [[ "$_n" == *.test.ts ]] && continue
    _try_unlink "$(ext_dest_dir "$_n")/$_n"
  done
  for _e in "$EXT_SRC"/*/; do
    [[ -d "$_e" ]] || continue
    _try_unlink "$PI_AGENT_EXT_DIR/$(basename "$_e")"
  done
  for _a in "$AGENTS_SRC"/*.md; do
    [[ -f "$_a" ]] || continue
    _try_unlink "$PI_AGENTS_DIR/$(basename "$_a")"
  done
  unset _e _n _a
  # Never remove: $PI_SETTINGS (live/runtime-mutated) and third-party extensions.
  exit 0
fi

# --- extensions: flat .ts files (auto-discovered) ---
linked_ext=0; skipped_ext=0
for _e in "$EXT_SRC"/*.ts; do
  [[ -f "$_e" ]] || continue
  _n="$(basename "$_e")"
  [[ "$_n" == *.test.ts ]] && continue
  _dest="$(ext_dest_dir "$_n")"; _target="$_dest/$_n"
  mkdir -p "$_dest"
  if [[ -L "$_target" ]]; then
    _lt="$(readlink "$_target")"; _rt="$(readlink -f "$_target" 2>/dev/null || realpath -m "$_target")"
    if [[ "$_lt" == "$_e" ]]; then
      if [[ "$FORCE" == "true" ]]; then rm "$_target"; ln -s "$_e" "$_target"; linked_ext=$((linked_ext + 1)); fi
      continue
    fi
    if is_ours "$_lt" "$_rt"; then rm "$_target"
    else echo "skipping $_n — $_target exists (not our symlink)" >&2; skipped_ext=$((skipped_ext + 1)); continue; fi
  elif [[ -e "$_target" ]]; then
    if cmp -s "$_target" "$_e"; then rm "$_target"; echo "migrated $_n -> repo-managed symlink"
    else echo "skipping $_n — $_target differs from repo copy; back it up and re-run" >&2; skipped_ext=$((skipped_ext + 1)); continue; fi
  fi
  ln -s "$_e" "$_target"; linked_ext=$((linked_ext + 1))
done

# --- extensions: directories (Pi loads <dir>/index.ts) ---
for _e in "$EXT_SRC"/*/; do
  [[ -d "$_e" ]] || continue
  _n="$(basename "$_e")"; _target="$PI_AGENT_EXT_DIR/$_n"
  mkdir -p "$PI_AGENT_EXT_DIR"
  if [[ -L "$_target" ]]; then
    _lt="$(readlink "$_target")"; _rt="$(readlink -f "$_target" 2>/dev/null || realpath -m "$_target")"
    if [[ "$_lt" == "${_e%/}" ]]; then
      if [[ "$FORCE" == "true" ]]; then rm "$_target"; ln -s "${_e%/}" "$_target"; linked_ext=$((linked_ext + 1)); fi
      continue
    fi
    if is_ours "$_lt" "$_rt"; then rm "$_target"
    else echo "skipping ext dir $_n — $_target exists (not our symlink)" >&2; skipped_ext=$((skipped_ext + 1)); continue; fi
  elif [[ -e "$_target" ]]; then
    echo "skipping ext dir $_n — $_target exists (not our symlink); back it up and re-run" >&2; skipped_ext=$((skipped_ext + 1)); continue
  fi
  ln -s "${_e%/}" "$_target"; linked_ext=$((linked_ext + 1))
done
unset _e _n _dest _target _lt _rt

# --- agent personas: auto-discovered from AGENTS_SRC ---
linked_agent=0
mkdir -p "$PI_AGENTS_DIR"
for _a in "$AGENTS_SRC"/*.md; do
  [[ -f "$_a" ]] || continue
  _n="$(basename "$_a")"; _target="$PI_AGENTS_DIR/$_n"
  if [[ -L "$_target" ]]; then
    _lt="$(readlink "$_target")"
    if [[ "$_lt" == "$_a" ]]; then
      if [[ "$FORCE" == "true" ]]; then rm "$_target"; ln -s "$_a" "$_target"; linked_agent=$((linked_agent + 1)); fi
      continue
    fi
    _rt="$(readlink -f "$_target" 2>/dev/null || realpath -m "$_target")"
    if is_ours "$_lt" "$_rt"; then rm "$_target"; ln -s "$_a" "$_target"; linked_agent=$((linked_agent + 1))
    else echo "skipping agent $_n — $_target exists (not our symlink)" >&2; fi
  elif [[ -e "$_target" ]]; then
    if cmp -s "$_target" "$_a"; then rm "$_target"; ln -s "$_a" "$_target"; linked_agent=$((linked_agent + 1)); echo "migrated $_n -> repo-managed symlink"
    else echo "skipping $_n — $_target differs from repo copy; back it up and re-run" >&2; fi
  else
    ln -s "$_a" "$_target"; linked_agent=$((linked_agent + 1))
  fi
done
unset _a _n _target _lt _rt

# --- settings: scaffold from template only when absent (never clobber live copy) ---
if [[ -f "$SETTINGS_TEMPLATE" ]]; then
  mkdir -p "$PI_AGENT_DIR"
  if [[ -f "$PI_SETTINGS" ]]; then echo "settings: preserved existing $PI_SETTINGS"
  else cp "$SETTINGS_TEMPLATE" "$PI_SETTINGS"; echo "settings: scaffolded from template"; fi
fi

# --- third-party extensions: install from source via `pi install` (pinned manifest) ---
# Single install path — no vendored npm copy. Fails loud if an install fails.
if [[ -x "$EXT_INSTALLER" ]]; then
  echo "third-party: installing from manifest via pi install"
  "$EXT_INSTALLER"
elif [[ "${PI_SKIP_EXTENSIONS:-0}" -eq 1 ]]; then
  echo "third-party Pi extensions: skipped (--skip-pi-extensions)"
else
  echo "third-party: skipped — $EXT_INSTALLER not found/executable" >&2
fi

echo "pi setup: $linked_ext extensions linked ($skipped_ext skipped), $linked_agent agent persona(s) linked"
echo "verify: launch pi — /workflow should be available; pi list shows third-party extensions"
