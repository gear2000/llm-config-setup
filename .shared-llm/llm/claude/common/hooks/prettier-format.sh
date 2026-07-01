#!/usr/bin/env bash
set -euo pipefail

input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // empty')

# Only run on Edit tool
if [[ "$tool_name" != "Edit" ]]; then
  exit 0
fi

file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')
if [[ -z "$file_path" || ! -f "$file_path" ]]; then
  exit 0
fi

# Only format JS/TS files
if [[ ! "$file_path" =~ \.(ts|tsx|js|jsx)$ ]]; then
  exit 0
fi

# Find prettier: local node_modules → global → skip
if [[ -f "node_modules/.bin/prettier" ]]; then
  prettier="node_modules/.bin/prettier"
elif command -v prettier &>/dev/null; then
  prettier="prettier"
else
  echo "Warning: prettier not found, skipping format" >&2
  exit 0
fi

# Format the file
$prettier --write "$file_path" 2>&1 || true
exit 0
