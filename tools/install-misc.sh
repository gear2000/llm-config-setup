#!/usr/bin/env bash
# Third-party skills and CLIs this kit expects on a fresh machine but does not
# compose. unslop is NOT listed here: it is a kit slash-command skill deployed
# by `just update` / the global step.
#
# Source of truth for Lavish local policy after this installer runs:
#   docs/SKILL-CUSTOMIZATIONS.md
#
# Usage (from the kit checkout, after node/npm are on PATH):
#   just misc
#   bash tools/install-misc.sh

set -euo pipefail

command -v npx >/dev/null 2>&1 || {
	echo "ERROR: npx not on PATH. Install Node.js (20+) so npm/npx work, then re-run." >&2
	exit 1
}

echo "install-misc: Plannotator"
curl -fsSL https://plannotator.ai/install.sh | bash

echo "install-misc: Lavish (kunchenguid/lavish-axi)"
npx -y skills add kunchenguid/lavish-axi --skill lavish

echo "install-misc: quota-axi (global)"
npx -y skills add kunchenguid/quota-axi --skill quota-axi -g

echo "install-misc: Impeccable (global, pbakaus/impeccable)"
# Provider-native global install so Claude Code, Codex, Cursor, and Pi all see it.
# Do not run this inside the kit checkout as a project install: that would drop
# skill files into this public repo.
npx -y impeccable install --providers=claude,codex,cursor,pi --scope=global

echo "install-misc: done"
echo "After Lavish, re-apply the ASCII-tree policy in docs/SKILL-CUSTOMIZATIONS.md"
echo "if the installer overwrote .claude/skills/lavish or .agents/skills/lavish."
