#!/usr/bin/env bash
set -euo pipefail

# This runs at session end - audit all modified files for console.log

# Get list of modified files from git
modified_files=$(git diff --name-only HEAD 2>/dev/null || true)

if [[ -z "$modified_files" ]]; then
  exit 0
fi

# Check each modified JS/TS file for console.log
found_logs=false
while IFS= read -r file; do
  if [[ "$file" =~ \.(ts|tsx|js|jsx)$ && -f "$file" ]]; then
    # Exclude test files
    if [[ ! "$file" =~ \.(test|spec)\.(ts|js|tsx|jsx)$ ]]; then
      if grep -n "console\.log" "$file" 2>/dev/null; then
        echo "⚠️  Found console.log in: $file" >&2
        found_logs=true
      fi
    fi
  fi
done <<< "$modified_files"

if [[ "$found_logs" == "true" ]]; then
  echo "" >&2
  echo "🔍 Session audit: console.log statements found in modified files" >&2
  echo "   Consider removing them before committing" >&2
fi

exit 0
