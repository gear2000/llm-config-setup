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

# Only check TypeScript files
if [[ ! "$file_path" =~ \.(ts|tsx)$ ]]; then
  exit 0
fi

# Find project root with tsconfig.json
project_root=$(pwd)
while [[ "$project_root" != "/" ]]; do
  if [[ -f "$project_root/tsconfig.json" ]]; then
    break
  fi
  project_root=$(dirname "$project_root")
done

if [[ ! -f "$project_root/tsconfig.json" ]]; then
  echo "Warning: tsconfig.json not found, skipping TypeScript check" >&2
  exit 0
fi

# Find tsc: local node_modules → global → skip
if [[ -f "$project_root/node_modules/.bin/tsc" ]]; then
  tsc="$project_root/node_modules/.bin/tsc"
elif command -v tsc &>/dev/null; then
  tsc="tsc"
else
  echo "Warning: tsc not found, skipping TypeScript check" >&2
  exit 0
fi

# Run TypeScript check
cd "$project_root"
if ! $tsc --noEmit 2>&1; then
  echo "⚠️  TypeScript errors detected - review above output" >&2
fi
exit 0
