#!/usr/bin/env bash
set -euo pipefail

input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // empty')

if [[ "$tool_name" != "Edit" ]]; then
  exit 0
fi

file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')
new_string=$(echo "$input" | jq -r '.tool_input.new_string // empty')

# Check if new code contains console.log
if echo "$new_string" | grep -q "console\.log"; then
  # Exclude test files from warnings
  if [[ ! "$file_path" =~ \.(test|spec)\.(ts|js|tsx|jsx)$ ]]; then
    echo "⚠️  Warning: Added console.log in $file_path - remember to remove before commit" >&2
  fi
fi
exit 0
