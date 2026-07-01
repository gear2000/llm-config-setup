#!/usr/bin/env bash
# install-local.sh — install the GENERAL / home pieces of this kit into the user's
# HOME (all-projects, NOT repo-specific). Driven by `task install local`.
#
# Installs FIVE things, all $HOME-relative so a sandbox HOME redirects everything:
#   1. General home skills  — delegates to install-global.sh (composes the global
#                             python/nextjs/backend recipes, copies into the home
#                             skill dirs each harness reads).
#   2. The 18 generic agents — composes the agent recipes to staging, then copies
#                             each persona into the home AGENT dir(s). Claude Code
#                             reads user agents from ~/.claude/agents/; Pi from
#                             ~/.pi/agents/. Codex has NO user-agent directory
#                             concept (it has ~/.codex/skills but not /agents), so
#                             Codex is skipped — we never invent a dir.
#   3. Pi runtime           — wires the bundled Pi extensions + agent personas into
#                             ~/.pi via `harness.py sync` (create / re-point / prune),
#                             scaffolds ~/.pi/agent/settings.json from the template
#                             when absent, and installs the pinned third-party set via
#                             install-pi-extensions.sh.
#   4. Claude runtime       — copies the generic hooks (~/.claude/hooks/) + statusline
#                             (~/.claude/statusline.sh), and scaffolds
#                             ~/.claude/settings.json from settings.template.json ONLY
#                             when absent (never clobbers per-machine tweaks). The
#                             hooks self-gate by file type, so home-level is safe.
#   5. The llm-compose wrapper — copies tools/templates/llm-compose to
#                             ~/.local/bin/llm-compose (executable).
#
# What this does NOT install: repo-specific files (the .shared-llm/ tree, generated
# CLAUDE.md/AGENTS.md). Those come from `task install repo`. The summary at the end
# states this explicitly.
#
# Safe-migration discipline (mirrors install-global.sh / harness.py sync):
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
#                          never hits the network. The `harness.py sync` symlink
#                          wiring still runs (offline, $HOME-relative).
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
COMPOSE="$REPO_ROOT/tools/harness.py"
INSTALL_GLOBAL="$REPO_ROOT/tools/install-global.sh"
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
[[ -f "$WRAPPER_SRC" ]] || { echo "error: wrapper template not found: $WRAPPER_SRC" >&2; exit 1; }

echo "=============================================================="
echo " install local — general / home pieces (HOME=$HOME)"
echo "=============================================================="

# ---------------------------------------------------------------------------
# 1. General home skills (delegate to install-global.sh)
# ---------------------------------------------------------------------------
echo
echo ">>> [1/5] General home skills (install-global.sh)"
"$INSTALL_GLOBAL"

# ---------------------------------------------------------------------------
# 2. The 18 generic agents — compose to staging, copy to home agent dir(s)
# ---------------------------------------------------------------------------
echo
echo ">>> [2/5] Generic agents -> ${HOME_AGENT_DIRS[*]}"
[[ -d "$AGENT_RECIPE_DIR" ]] || { echo "error: agent recipe dir missing: $AGENT_RECIPE_DIR" >&2; exit 1; }

# Compose every agent recipe; --target pins staging under REPO_ROOT/examples/ so the
# root-relative .claude/agents/<name>.md outputs land at examples/.claude/agents/.
agent_recipes=("$AGENT_RECIPE_DIR"/*.yaml)
[[ -e "${agent_recipes[0]}" ]] || { echo "error: no agent recipes in $AGENT_RECIPE_DIR" >&2; exit 1; }
for recipe in "${agent_recipes[@]}"; do
  python3 "$COMPOSE" compose "$recipe" --target "$AGENT_STAGING_BASE" >/dev/null
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
# 3. Pi runtime — symlink wiring + settings scaffold + third-party extensions.
#    Done directly here (no separate helper script): harness.py sync reconciles
#    the Pi/Codex symlinks (extensions + agent personas), then we scaffold
#    settings.json when absent and install the pinned third-party set.
# ---------------------------------------------------------------------------
echo
echo ">>> [3/5] Pi runtime (harness.py sync + settings + third-party extensions)"

# (a) symlink wiring: create missing links, re-point drifted ones, prune orphans.
python3 "$COMPOSE" sync

# (b) settings: scaffold from the template only when absent (never clobber the live copy).
SETTINGS_TEMPLATE="$REPO_ROOT/.shared-llm/llm/pi/common/settings.template.json"
PI_SETTINGS="$HOME/.pi/agent/settings.json"
if [[ -f "$SETTINGS_TEMPLATE" ]]; then
  mkdir -p "$HOME/.pi/agent"
  if [[ -f "$PI_SETTINGS" ]]; then echo "settings: preserved existing $PI_SETTINGS"
  else cp "$SETTINGS_TEMPLATE" "$PI_SETTINGS"; echo "settings: scaffolded from template"; fi
fi

# (c) third-party extensions: install from the pinned manifest (unless --skip-pi-extensions).
EXT_INSTALLER="$REPO_ROOT/tools/install-pi-extensions.sh"
if [[ "$SKIP_PI_EXTENSIONS" -eq 1 ]]; then
  echo "    (--skip-pi-extensions: skipping the third-party 'pi install' step)"
elif [[ -x "$EXT_INSTALLER" ]]; then
  "$EXT_INSTALLER"
else
  echo "third-party: skipped — $EXT_INSTALLER not found/executable" >&2
fi

# ---------------------------------------------------------------------------
# 4. Claude runtime — generic hooks + statusline + settings scaffold (home level).
#    Hooks + statusline are copied into ~/.claude (never clobber a divergent or
#    foreign copy). ~/.claude/settings.json is scaffolded from the template ONLY
#    when absent, so re-running never wipes per-machine tweaks (same discipline as
#    the Pi settings scaffold above). The generic hooks self-gate by file type, so
#    firing in every project is safe.
# ---------------------------------------------------------------------------
echo
echo ">>> [4/5] Claude runtime (hooks + statusline + settings)"

CLAUDE_SRC="$REPO_ROOT/.shared-llm/llm/claude/common"
CLAUDE_HOME="$HOME/.claude"

# (a) generic hooks -> ~/.claude/hooks (never clobber a divergent/foreign copy)
if [[ -d "$CLAUDE_SRC/hooks" ]]; then
  mkdir -p "$CLAUDE_HOME/hooks"
  hooks_installed=0; hooks_uptodate=0; hooks_skipped=0
  for hook in "$CLAUDE_SRC/hooks"/*; do
    [[ -f "$hook" ]] || continue
    name="$(basename "$hook")"; target="$CLAUDE_HOME/hooks/$name"
    if [[ -L "$target" ]]; then
      echo "skip hook $name -> $target is a symlink (foreign — leaving it)" >&2
      hooks_skipped=$((hooks_skipped + 1)); continue
    fi
    if [[ -e "$target" ]]; then
      if cmp -s "$target" "$hook"; then hooks_uptodate=$((hooks_uptodate + 1)); continue
      else
        echo "skip hook $name -> $target exists and differs (not ours — back it up and re-run)" >&2
        hooks_skipped=$((hooks_skipped + 1)); continue
      fi
    fi
    cp "$hook" "$target"; chmod +x "$target"; hooks_installed=$((hooks_installed + 1))
  done
  echo "hooks: $hooks_installed copied, $hooks_uptodate current, $hooks_skipped skipped (foreign/divergent)"
fi

# (b) statusline -> ~/.claude/statusline.sh (never clobber a divergent/foreign copy)
if [[ -f "$CLAUDE_SRC/statusline.sh" ]]; then
  target="$CLAUDE_HOME/statusline.sh"
  if [[ -L "$target" ]]; then echo "statusline: skip -> $target is a symlink (foreign)" >&2
  elif [[ -e "$target" ]] && ! cmp -s "$target" "$CLAUDE_SRC/statusline.sh"; then
    echo "statusline: skip -> $target exists and differs (not ours — back it up and re-run)" >&2
  elif [[ -e "$target" ]]; then echo "statusline: already current"
  else mkdir -p "$CLAUDE_HOME"; cp "$CLAUDE_SRC/statusline.sh" "$target"; chmod +x "$target"; echo "statusline: installed"; fi
fi

# (c) settings: scaffold from the template only when absent (never clobber live tweaks).
if [[ -f "$CLAUDE_SRC/settings.template.json" ]]; then
  mkdir -p "$CLAUDE_HOME"
  if [[ -f "$CLAUDE_HOME/settings.json" ]]; then echo "settings: preserved existing $CLAUDE_HOME/settings.json"
  else cp "$CLAUDE_SRC/settings.template.json" "$CLAUDE_HOME/settings.json"; echo "settings: scaffolded from template"; fi
fi

# ---------------------------------------------------------------------------
# 5. The llm-compose wrapper -> ~/.local/bin
# ---------------------------------------------------------------------------
echo
echo ">>> [5/5] llm-compose wrapper -> $WRAPPER_DST"
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
  - skills      : python, nextjs, backend, golang
                  + slash-command skills (do-planish, do-research, do-plan-and-grill,
                    qa, security, prd-to-plan, fail-loud, grill-me, playwright-cli,
                    response, hub-connect, meta-auto-run, run-phase, run_phase,
                    codex-delegate)
                  -> ~/.claude/skills, ~/.codex/skills, ~/.pi/skills
                  (these skill dirs ARE the typeable commands: /qa, /do-planish, …
                   — Claude Code merged commands into skills, so no separate
                   ~/.claude/commands/ step is needed)
  - agents      : $(ls -1 "$AGENT_STAGING" | wc -l | tr -d ' ') generic personas
                  -> ~/.claude/agents, ~/.pi/agents
                  ($agents_installed copied, $agents_uptodate current, $agents_skipped skipped)
  - pi runtime  : extensions + agent personas symlinked into ~/.pi$( [[ "$SKIP_PI_EXTENSIONS" -eq 1 ]] && echo " (third-party 'pi install' SKIPPED)" )
  - claude cfg  : generic hooks -> ~/.claude/hooks, statusline -> ~/.claude/statusline.sh,
                  settings scaffolded to ~/.claude/settings.json (only if absent — never clobbered)
  - bin wrapper : llm-compose -> ~/.local/bin/llm-compose ($wrapper_action)

NOT installed here (these come from \`task install repo -- <dir>\`):
  - the per-repo .shared-llm/ layer tree
  - the per-repo compose engine (tools/harness.py) + thin Taskfile
  - generated CLAUDE.md / AGENTS.md / per-repo skill files

NOT applicable:
  - ~/.codex/agents : Codex has no user-agent directory — skipped (not invented).

Next: run \`task install repo -- <dir>\` inside (or pointing at) a target repo to
lay down its .shared-llm/ tree and generate its CLAUDE.md / AGENTS.md.
EOF
