#!/usr/bin/env bash
# Wire this kit's Pi harness runtime config into ~/.pi/ so Pi can discover it.
# Run once per machine after cloning. Idempotent — safe to re-run.
#
# Sources live under .shared-llm/llm/pi/common/ (runtime config, NOT compose inputs):
#   extensions/context-workflow.ts        -> ~/.pi/agent/extensions/   (symlink)
#   extensions/iac-guard.ts               -> ~/.pi/agent/extensions/   (symlink, IaC safety gate, auto-loaded)
#   extensions/memsearch/                 -> ~/.pi/agent/extensions/   (symlinked DIR; index.ts auto-loaded, memory recall+capture)
#   extensions/codex-reviewer-hub.ts      -> ~/.pi/extensions/         (symlink, iac-guard approval hub)
#   extensions/doc-review-hub.ts          -> ~/.pi/extensions/         (symlink, document-review hub)
#   extensions/pr-review-hub.ts           -> ~/.pi/extensions/         (symlink, PR-review hub)
#   extensions/hub-common.ts              -> ~/.pi/extensions/         (symlink, shared hub plumbing)
#   agents/codex-reviewer.md              -> ~/.pi/agents/             (symlink)
#   agents/iac-verifier.md                -> ~/.pi/agents/             (symlink)
#   agents/doc-reviewer.md                -> ~/.pi/agents/             (symlink)
#   agents/pr-reviewer.md                 -> ~/.pi/agents/             (symlink)
#   settings.template.json                -> ~/.pi/agent/settings.json (scaffold if absent)
#
# Third-party extensions are NOT handled here as a copy — they are installed from
# source via `pi install` driven by tools/install-pi-extensions.sh (pinned list in
# .shared-llm/llm/pi/common/third-party-extensions.txt). This script calls that installer
# at the end. See .shared-llm/llm/pi/common/THIRD-PARTY-EXTENSIONS.md.
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
PI_SRC="$REPO_ROOT/.shared-llm/llm/pi/common"
EXT_SRC="$PI_SRC/extensions"
AGENTS_SRC="$PI_SRC/agents"
SETTINGS_TEMPLATE="$PI_SRC/settings.template.json"
EXT_INSTALLER="$REPO_ROOT/tools/install-pi-extensions.sh"

HOME_PI="$HOME/.pi"
PI_AGENT_DIR="$HOME_PI/agent"
PI_AGENTS_DIR="$HOME_PI/agents"
PI_SETTINGS="$PI_AGENT_DIR/settings.json"

# extension name : destination dir (two distinct home locations by design)
PI_EXT_MAP=(
  "context-workflow.ts:$PI_AGENT_DIR/extensions"   # agent-scoped, auto-loaded
  "iac-guard.ts:$PI_AGENT_DIR/extensions"          # agent-scoped, auto-loaded (IaC safety gate)
  "codex-reviewer-hub.ts:$HOME_PI/extensions"      # top-level, `pi -e` loaded (iac-guard approval hub)
  "doc-review-hub.ts:$HOME_PI/extensions"          # top-level, `pi -e` loaded (document-review hub)
  "pr-review-hub.ts:$HOME_PI/extensions"           # top-level, `pi -e` loaded (PR-review hub)
  "hub-common.ts:$HOME_PI/extensions"              # shared plumbing imported by the two review hubs
)

# DIRECTORY extensions: symlink the whole dir so Pi auto-loads its index.ts and the
# sibling helpers ride along un-discovered (a bare *.ts helper at the top level would
# fail Pi's "must export a factory" check — that is why memsearch is a directory).
# name : destination dir
PI_EXT_DIRS=(
  "memsearch:$PI_AGENT_DIR/extensions"             # agent-scoped, auto-loaded (memory recall+capture)
)

# agent personas symlinked into ~/.pi/agents/
PI_AGENT_FILES=(codex-reviewer.md iac-verifier.md doc-reviewer.md pr-reviewer.md)

# A symlink is "ours" iff it resolves into this clone's .shared-llm/llm/pi/common tree.
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
  for pair in "${PI_EXT_DIRS[@]}"; do
    name="${pair%%:*}"; dest_dir="${pair##*:}"; target="$dest_dir/$name"
    if [[ -L "$target" ]]; then
      lt="$(readlink "$target")"; rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
      if is_ours "$lt" "$rt"; then rm "$target"; echo "unlinked $target"; fi
    fi
  done
  for amd in "${PI_AGENT_FILES[@]}"; do
    target="$PI_AGENTS_DIR/$amd"
    if [[ -L "$target" ]]; then
      lt="$(readlink "$target")"; rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
      if is_ours "$lt" "$rt"; then rm "$target"; echo "unlinked $target"; fi
    fi
  done
  # Never removed: $PI_SETTINGS (live/runtime-mutated) and installed third-party
  # extensions (managed via `pi install`/`pi remove`, not by this script).
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

# --- directory extensions: symlink the whole dir (Pi loads its index.ts) ---
for pair in "${PI_EXT_DIRS[@]}"; do
  name="${pair%%:*}"; dest_dir="${pair##*:}"; src="$EXT_SRC/$name"
  [[ -d "$src" ]] || continue
  mkdir -p "$dest_dir"; target="$dest_dir/$name"
  if [[ -L "$target" ]]; then
    lt="$(readlink "$target")"
    [[ "$lt" == "$src" ]] && continue
    rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
    if is_ours "$lt" "$rt"; then rm "$target"
    else echo "skipping ext dir $name — $target exists (not our symlink)" >&2; skipped_ext=$((skipped_ext + 1)); continue; fi
  elif [[ -e "$target" ]]; then
    echo "skipping ext dir $name — $target exists (not our symlink); back it up and re-run" >&2; skipped_ext=$((skipped_ext + 1)); continue
  fi
  ln -s "$src" "$target"; linked_ext=$((linked_ext + 1))
done

# --- agent personas: symlink (migrate an existing identical real file) ---
linked_agent=0
for amd in "${PI_AGENT_FILES[@]}"; do
  ca="$AGENTS_SRC/$amd"
  [[ -f "$ca" ]] || continue
  mkdir -p "$PI_AGENTS_DIR"; target="$PI_AGENTS_DIR/$amd"
  if [[ -L "$target" ]]; then
    lt="$(readlink "$target")"
    [[ "$lt" == "$ca" ]] && continue
    rt="$(readlink -f "$target" 2>/dev/null || realpath -m "$target")"
    if is_ours "$lt" "$rt"; then rm "$target"; ln -s "$ca" "$target"; linked_agent=$((linked_agent + 1))
    else echo "skipping agent $amd — $target exists (not our symlink)" >&2; fi
  elif [[ -e "$target" ]]; then
    if cmp -s "$target" "$ca"; then rm "$target"; ln -s "$ca" "$target"; linked_agent=$((linked_agent + 1)); echo "migrated $amd -> repo-managed symlink"
    else echo "skipping $amd — $target differs from repo copy; back it up and re-run" >&2; fi
  else
    ln -s "$ca" "$target"; linked_agent=$((linked_agent + 1))
  fi
done

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
else
  echo "third-party: skipped — $EXT_INSTALLER not found/executable" >&2
fi

echo "pi setup: $linked_ext extensions linked ($skipped_ext skipped), $linked_agent agent persona(s) linked"
echo "verify: launch pi — /workflow should be available; pi list shows third-party extensions"
