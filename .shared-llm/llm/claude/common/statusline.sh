#!/bin/bash
# Simplified Claude Code statusline — 2 lines, no emoji, no broken features

input=$(cat)

# ---- check jq availability ----
HAS_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAS_JQ=1
fi

# ---- color helpers ----
use_color=1
[ -n "$NO_COLOR" ] && use_color=0

dir_color() { [ "$use_color" -eq 1 ] && printf '\033[38;5;117m'; }    # sky blue
git_color() { [ "$use_color" -eq 1 ] && printf '\033[38;5;150m'; }    # soft green
model_color() { [ "$use_color" -eq 1 ] && printf '\033[38;5;147m'; }  # light purple
context_color() { [ "$use_color" -eq 1 ] && printf '\033[38;5;210m'; }  # pastel red
rst() { [ "$use_color" -eq 1 ] && printf '\033[0m'; }

progress_bar() {
  pct="${1:-0}"; width="${2:-10}"
  [[ "$pct" =~ ^[0-9]+$ ]] || pct=0; ((pct<0))&&pct=0; ((pct>100))&&pct=100
  filled=$(( pct * width / 100 )); empty=$(( width - filled ))
  printf '%*s' "$filled" '' | tr ' ' '='
  printf '%*s' "$empty" '' | tr ' ' '-'
}

# ---- extract data ----
if [ "$HAS_JQ" -eq 1 ]; then
  current_dir=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // "unknown"' 2>/dev/null | sed "s|^$HOME|~|g")
  model_name=$(echo "$input" | jq -r '.model.display_name // "Claude"' 2>/dev/null)
else
  current_dir=$(echo "$input" | grep -o '"workspace"[[:space:]]*:[[:space:]]*{[^}]*"current_dir"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"current_dir"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' | sed 's/\\\\/\//g')
  if [ -z "$current_dir" ] || [ "$current_dir" = "null" ]; then
    current_dir=$(echo "$input" | grep -o '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' | sed 's/\\\\/\//g')
  fi
  [ -z "$current_dir" ] && current_dir="unknown"
  current_dir=$(echo "$current_dir" | sed "s|^$HOME|~|g")
  model_name=$(echo "$input" | grep -o '"model"[[:space:]]*:[[:space:]]*{[^}]*"display_name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"display_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
  [ -z "$model_name" ] && model_name="Claude"
fi

# ---- git branch ----
git_branch=""
if git rev-parse --git-dir >/dev/null 2>&1; then
  git_branch=$(git branch --show-current 2>/dev/null || git rev-parse --short HEAD 2>/dev/null)
fi

# ---- context window ----
context_used_pct=""
if [ "$HAS_JQ" -eq 1 ]; then
  CONTEXT_SIZE=$(echo "$input" | jq -r '.context_window.context_window_size // 200000' 2>/dev/null)
  USAGE=$(echo "$input" | jq '.context_window.current_usage' 2>/dev/null)
  if [ "$USAGE" != "null" ] && [ -n "$USAGE" ]; then
    CURRENT_TOKENS=$(echo "$USAGE" | jq '(.input_tokens // 0) + (.cache_creation_input_tokens // 0) + (.cache_read_input_tokens // 0)' 2>/dev/null)
    if [ -n "$CURRENT_TOKENS" ] && [ "$CURRENT_TOKENS" -gt 0 ] 2>/dev/null; then
      context_used_pct=$(( CURRENT_TOKENS * 100 / CONTEXT_SIZE ))
      (( context_used_pct < 0 )) && context_used_pct=0
      (( context_used_pct > 100 )) && context_used_pct=100
    fi
  fi
fi

# ---- render ----
# Line 1: directory  branch  model
printf '%s%s%s' "$(dir_color)" "$current_dir" "$(rst)"
if [ -n "$git_branch" ]; then
  printf '  %s%s%s' "$(git_color)" "$git_branch" "$(rst)"
fi
printf '  %s%s%s' "$(model_color)" "$model_name" "$(rst)"

# Line 2: context usage
if [ -n "$context_used_pct" ]; then
  context_bar=$(progress_bar "$context_used_pct" 10)
  printf '\n%sContext: %d%% [%s]%s' "$(context_color)" "$context_used_pct" "$context_bar" "$(rst)"
else
  printf '\n%sContext: --%% [----------]%s' "$(context_color)" "$(rst)"
fi
printf '\n'
