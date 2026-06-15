#!/usr/bin/env bash
# install-local.sh — install the GENERAL / home pieces of this kit into the user's
# HOME (all-projects, NOT repo-specific). Driven by `task install local`.
#
# Installs FOUR things, all $HOME-relative so a sandbox HOME redirects everything:
#   1. General home skills  — delegates to install-global.sh (composes the global
#                             python/nextjs/backend recipes, copies into the home
#                             skill dirs each harness reads).
#   2. The 18 generic agents — composes the agent recipes to staging, then copies
#                             each persona into the home AGENT dir(s). Claude Code
#                             reads user agents from ~/.claude/agents/; Pi from
#                             ~/.pi/agents/. Codex has NO user-agent directory
#                             concept (it has ~/.codex/skills but not /agents), so
#                             Codex is skipped — we never invent a dir.
#   3. Pi runtime           — delegates to setup-pi.sh (symlinks the bundled Pi
#                             extensions + agent personas, scaffolds settings) and
#                             install-pi-extensions.sh (the pinned third-party set).
#   4. The llm-compose wrapper — copies tools/templates/llm-compose to
#                             ~/.local/bin/llm-compose (executable).
#
# What this does NOT install: repo-specific files (the .shared-llm/ tree, generated
# CLAUDE.md/AGENTS.md). Those come from `task install repo`. The summary at the end
# states this explicitly.
#
# Safe-migration discipline (mirrors install-global.sh / setup-pi.sh):
#   - Idempotent: re-running installs nothing new when home already matches.
#   - Never clobber a divergent/foreign file: an existing real file that does NOT
#     match our staged copy, or an existing symlink we did not create, is left
#     untouched with a warning. Only a byte-identical copy counts as "ours".
#   - Fail loud: a compose failure aborts (set -e); an occupied/divergent target is
#     reported, never overwritten.
#
# Flags:
#   --skip-pi-extensions   skip install-pi-extensions.sh (the `pi install` network
#                          step). The sandbox gate uses this so the throwaway HOME
#                          never hits the network. setup-pi.sh's own symlink wiring
#                          still runs (offline, $HOME-relative).
#
# Usage:
#   tools/install-local.sh
#   tools/install-local.sh --skip-pi-extensions

set -euo pipefail

SKIP_PI_EXTENSIONS=0
for arg in "$@"; do
  case "$arg" in
    --skip-pi-extensions) SKIP_PI_EXTENSIONS=1 ;;
    *) echo "install-local: unknown argument: $arg" >&2; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$REPO_ROOT/tools/compose-layers.py"
INSTALL_GLOBAL="$REPO_ROOT/tools/install-global.sh"
SETUP_PI="$REPO_ROOT/tools/setup-pi.sh"
WRAPPER_SRC="$REPO_ROOT/tools/templates/llm-compose"

# Agent recipes now carry ROOT-RELATIVE outputs (.claude/agents/<name>.md). We
# stage them under the gitignored examples/ dir by composing with
# `--target $REPO_ROOT/examples`, so the kit's own tree is never polluted, then
# copy each persona into the home agent dir(s).
AGENT_RECIPE_DIR="$REPO_ROOT/.shared-llm/compose/agents"
AGENT_STAGING_BASE="$REPO_ROOT/examples"
AGENT_STAGING="$AGENT_STAGING_BASE/.claude/agents"

# Home agent dirs by harness. Claude Code + Pi read user agents; Codex does NOT
# (it has ~/.codex/skills but no ~/.codex/agents) — so Codex is intentionally absent.
HOME_AGENT_DIRS=(
  "$HOME/.claude/agents"
  "$HOME/.pi/agents"
)

BIN_DIR="$HOME/.local/bin"
WRAPPER_DST="$BIN_DIR/llm-compose"

command -v python3 >/dev/null 2>&1 || { echo "error: python3 not on PATH" >&2; exit 1; }
[[ -f "$COMPOSE" ]] || { echo "error: compose engine not found: $COMPOSE" >&2; exit 1; }
[[ -x "$INSTALL_GLOBAL" ]] || { echo "error: install-global.sh not found/executable: $INSTALL_GLOBAL" >&2; exit 1; }
[[ -x "$SETUP_PI" ]] || { echo "error: setup-pi.sh not found/executable: $SETUP_PI" >&2; exit 1; }
[[ -f "$WRAPPER_SRC" ]] || { echo "error: wrapper template not found: $WRAPPER_SRC" >&2; exit 1; }

echo "=============================================================="
echo " install local — general / home pieces (HOME=$HOME)"
echo "=============================================================="

# ---------------------------------------------------------------------------
# 1. General home skills (delegate to install-global.sh)
# ---------------------------------------------------------------------------
echo
echo ">>> [1/4] General home skills (install-global.sh)"
"$INSTALL_GLOBAL"

# ---------------------------------------------------------------------------
# 2. The 18 generic agents — compose to staging, copy to home agent dir(s)
# ---------------------------------------------------------------------------
echo
echo ">>> [2/4] Generic agents -> ${HOME_AGENT_DIRS[*]}"
[[ -d "$AGENT_RECIPE_DIR" ]] || { echo "error: agent recipe dir missing: $AGENT_RECIPE_DIR" >&2; exit 1; }

# Compose every agent recipe; --target pins staging under REPO_ROOT/examples/ so the
# root-relative .claude/agents/<name>.md outputs land at examples/.claude/agents/.
agent_recipes=("$AGENT_RECIPE_DIR"/*.yaml)
[[ -e "${agent_recipes[0]}" ]] || { echo "error: no agent recipes in $AGENT_RECIPE_DIR" >&2; exit 1; }
for recipe in "${agent_recipes[@]}"; do
  python3 "$COMPOSE" "$recipe" --target "$AGENT_STAGING_BASE" >/dev/null
done

agents_installed=0; agents_uptodate=0; agents_skipped=0
for staged in "$AGENT_STAGING"/*.md; do
  [[ -f "$staged" ]] || { echo "error: no staged agents after compose in $AGENT_STAGING" >&2; exit 1; }
  name="$(basename "$staged")"
  for base in "${HOME_AGENT_DIRS[@]}"; do
    target="$base/$name"
    if [[ -L "$target" ]]; then
      echo "skip $name -> $target is a symlink (foreign — leaving it)" >&2
      agents_skipped=$((agents_skipped + 1)); continue
    fi
    if [[ -e "$target" ]]; then
      if cmp -s "$target" "$staged"; then agents_uptodate=$((agents_uptodate + 1)); continue
      else
        echo "skip $name -> $target exists and differs (not ours — back it up and re-run)" >&2
        agents_skipped=$((agents_skipped + 1)); continue
      fi
    fi
    mkdir -p "$base"
    cp "$staged" "$target"
    agents_installed=$((agents_installed + 1))
  done
done
echo "agents: $agents_installed copied, $agents_uptodate already current, $agents_skipped skipped (foreign/divergent)"

# ---------------------------------------------------------------------------
# 3. Pi runtime (delegate to setup-pi.sh; it calls install-pi-extensions.sh)
# ---------------------------------------------------------------------------
echo
echo ">>> [3/4] Pi runtime (setup-pi.sh)"
if [[ "$SKIP_PI_EXTENSIONS" -eq 1 ]]; then
  echo "    (--skip-pi-extensions: running setup-pi.sh symlink wiring only, no 'pi install')"
  # setup-pi.sh runs install-pi-extensions.sh only if it is executable. Temporarily
  # make the third-party installer non-executable so the network step is skipped,
  # while the offline symlink wiring still runs. Restore the bit afterward.
  EXT_INSTALLER="$REPO_ROOT/tools/install-pi-extensions.sh"
  restore_x=0
  if [[ -x "$EXT_INSTALLER" ]]; then chmod -x "$EXT_INSTALLER"; restore_x=1; fi
  set +e
  PI_SKIP_EXTENSIONS=1 "$SETUP_PI"
  pi_rc=$?
  set -e
  [[ "$restore_x" -eq 1 ]] && chmod +x "$EXT_INSTALLER"
  [[ "$pi_rc" -eq 0 ]] || { echo "error: setup-pi.sh failed (rc=$pi_rc)" >&2; exit "$pi_rc"; }
else
  "$SETUP_PI"
fi

# ---------------------------------------------------------------------------
# 4. The llm-compose wrapper -> ~/.local/bin
# ---------------------------------------------------------------------------
echo
echo ">>> [4/4] llm-compose wrapper -> $WRAPPER_DST"
mkdir -p "$BIN_DIR"
wrapper_action="installed"
if [[ -L "$WRAPPER_DST" ]]; then
  echo "skip wrapper -> $WRAPPER_DST is a symlink (foreign — leaving it)" >&2
  wrapper_action="skipped (foreign symlink)"
elif [[ -e "$WRAPPER_DST" ]] && ! cmp -s "$WRAPPER_DST" "$WRAPPER_SRC"; then
  echo "skip wrapper -> $WRAPPER_DST exists and differs (not ours — back it up and re-run)" >&2
  wrapper_action="skipped (divergent)"
elif [[ -e "$WRAPPER_DST" ]]; then
  wrapper_action="already current"
else
  cp "$WRAPPER_SRC" "$WRAPPER_DST"; chmod +x "$WRAPPER_DST"
fi
echo "wrapper: $wrapper_action"

# ---------------------------------------------------------------------------
# Summary — what WAS installed and what was NOT (repo-specific lives in install repo)
# ---------------------------------------------------------------------------
cat <<EOF

==============================================================
 install local — SUMMARY
==============================================================
INSTALLED (general / home, all-projects):
  - skills      : python, nextjs, backend
                  -> ~/.claude/skills, ~/.codex/skills, ~/.pi/skills
  - agents      : $(ls -1 "$AGENT_STAGING" | wc -l | tr -d ' ') generic personas
                  -> ~/.claude/agents, ~/.pi/agents
                  ($agents_installed copied, $agents_uptodate current, $agents_skipped skipped)
  - pi runtime  : extensions + agent personas symlinked into ~/.pi$( [[ "$SKIP_PI_EXTENSIONS" -eq 1 ]] && echo " (third-party 'pi install' SKIPPED)" )
  - bin wrapper : llm-compose -> ~/.local/bin/llm-compose ($wrapper_action)

NOT installed here (these come from \`task install repo -- <dir>\`):
  - the per-repo .shared-llm/ layer tree
  - the per-repo compose engine (tools/compose-layers.py) + thin Taskfile
  - generated CLAUDE.md / AGENTS.md / per-repo skill files

NOT applicable:
  - ~/.codex/agents : Codex has no user-agent directory — skipped (not invented).

Next: run \`task install repo -- <dir>\` inside (or pointing at) a target repo to
lay down its .shared-llm/ tree and generate its CLAUDE.md / AGENTS.md.
EOF
