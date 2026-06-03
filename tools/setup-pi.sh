#!/usr/bin/env bash
# Wire this kit's Pi harness runtime config into ~/.pi/ so Pi can discover it.
# Run once per machine after cloning. Idempotent — safe to re-run.
#
# Sources live under layers/llm/pi/common/ (runtime config, NOT compose inputs):
#   extensions/context-workflow.ts        -> ~/.pi/agent/extensions/   (symlink)
#   extensions/codex-reviewer-hub.ts      -> ~/.pi/extensions/         (symlink)
#   agents/codex-reviewer.md              -> ~/.pi/agents/             (symlink)
#   settings.template.json                -> ~/.pi/agent/settings.json (scaffold if absent)
#   npm/{package.json,package-lock.json}  -> npm ci into ~/.pi/agent/npm/ (if node_modules missing)
#
# Usage:
#   tools/setup-pi.sh             # wire everything up
#   tools/setup-pi.sh --unlink    # remove the symlinks created earlier
#
# An existing real file in ~/.pi is migrated to a symlink only when byte-identical
# to this kit's copy; a divergent file is left untouched. --unlink never removes the
# live settings.json or the regenerable node_modules.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_SRC="$REPO_ROOT/layers/llm/pi/common"
EXT_SRC="$PI_SRC/extensions"
AGENTS_SRC="$PI_SRC/agents"
NPM_SRC="$PI_SRC/npm"
SETTINGS_TEMPLATE="$PI_SRC/settings.template.json"

HOME_PI="$HOME/.pi"
PI_AGENT_DIR="$HOME_PI/agent"
PI_AGENTS_DIR="$HOME_PI/agents"
PI_NPM_DIR="$PI_AGENT_DIR/npm"
PI_SETTINGS="$PI_AGENT_DIR/settings.json"

# extension name : destination dir (two distinct home locations by design)
PI_EXT_MAP=(
  "context-workflow.ts:$PI_AGENT_DIR/extensions"   # agent-scoped, auto-loaded
  "codex-reviewer-hub.ts:$HOME_PI/extensions"      # top-level, `pi -e` loaded
)

# A symlink is "ours" iff it resolves into this clone's layers/llm/pi/common tree.
is_ours() {  # link_target resolved_target
  case "$1" in "$PI_SRC"/*) return 0 ;; esac
  case "$2" in "$PI_SRC"/*) return 0 ;; esac
  return 1
}

if [[ "${1:-}" == "--unlink" ]]; then
  for pair in "${PI_EXT_MAP[@]}"; do
    name="${pair%%:*}"; dest_dir="${pair##*:}"; target="$dest_dir/$name"
    if [[ -L "$target" ]]; then
      lt="$(readlink "$target")"; rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
      if is_ours "$lt" "$rt"; then rm "$target"; echo "unlinked $target"; fi
    fi
  done
  target="$PI_AGENTS_DIR/codex-reviewer.md"
  if [[ -L "$target" ]]; then
    lt="$(readlink "$target")"; rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
    if is_ours "$lt" "$rt"; then rm "$target"; echo "unlinked $target"; fi
  fi
  # Never removed: $PI_SETTINGS (live/runtime-mutated) and $PI_NPM_DIR/node_modules (regenerable).
  exit 0
fi

# --- extensions: symlink into their two home locations ---
linked_ext=0; skipped_ext=0
for pair in "${PI_EXT_MAP[@]}"; do
  name="${pair%%:*}"; dest_dir="${pair##*:}"; src="$EXT_SRC/$name"
  [[ -f "$src" ]] || continue
  mkdir -p "$dest_dir"; target="$dest_dir/$name"
  if [[ -L "$target" ]]; then
    lt="$(readlink "$target")"
    [[ "$lt" == "$src" ]] && continue
    rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
    if is_ours "$lt" "$rt"; then rm "$target"
    else echo "skipping ext $name — $target exists (not our symlink)" >&2; skipped_ext=$((skipped_ext + 1)); continue; fi
  elif [[ -e "$target" ]]; then
    if cmp -s "$target" "$src"; then rm "$target"; echo "migrated ext $name -> repo-managed symlink"
    else echo "skipping ext $name — $target differs from repo copy; back it up and re-run" >&2; skipped_ext=$((skipped_ext + 1)); continue; fi
  fi
  ln -s "$src" "$target"; linked_ext=$((linked_ext + 1))
done

# --- codex-reviewer.md agent: symlink (migrate an existing identical real file) ---
ca="$AGENTS_SRC/codex-reviewer.md"; linked_agent=0
if [[ -f "$ca" ]]; then
  mkdir -p "$PI_AGENTS_DIR"; target="$PI_AGENTS_DIR/codex-reviewer.md"
  if [[ -L "$target" ]]; then
    lt="$(readlink "$target")"
    if [[ "$lt" != "$ca" ]]; then
      rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
      if is_ours "$lt" "$rt"; then rm "$target"; ln -s "$ca" "$target"; linked_agent=1; fi
    fi
  elif [[ -e "$target" ]]; then
    if cmp -s "$target" "$ca"; then rm "$target"; ln -s "$ca" "$target"; linked_agent=1; echo "migrated codex-reviewer.md -> repo-managed symlink"
    else echo "skipping codex-reviewer.md — $target differs from repo copy; back it up and re-run" >&2; fi
  else
    ln -s "$ca" "$target"; linked_agent=1
  fi
fi

# --- settings: scaffold from template only when absent (never clobber live copy) ---
if [[ -f "$SETTINGS_TEMPLATE" ]]; then
  mkdir -p "$PI_AGENT_DIR"
  if [[ -f "$PI_SETTINGS" ]]; then echo "settings: preserved existing $PI_SETTINGS"
  else cp "$SETTINGS_TEMPLATE" "$PI_SETTINGS"; echo "settings: scaffolded from template"; fi
fi

# --- npm deps: install into Pi's fixed path only when missing ---
if [[ -f "$NPM_SRC/package.json" ]]; then
  if [[ -d "$PI_NPM_DIR/node_modules" ]]; then echo "npm: node_modules present — skipping install"
  else
    mkdir -p "$PI_NPM_DIR"
    cp "$NPM_SRC/package.json" "$PI_NPM_DIR/package.json"
    [[ -f "$NPM_SRC/package-lock.json" ]] && cp "$NPM_SRC/package-lock.json" "$PI_NPM_DIR/package-lock.json"
    ( cd "$PI_NPM_DIR" && npm ci ) && echo "npm: installed into $PI_NPM_DIR"
  fi
fi

echo "pi setup: $linked_ext extensions linked ($skipped_ext skipped), codex-reviewer agent ($linked_agent linked)"
echo "verify: launch pi — /workflow should be available"
