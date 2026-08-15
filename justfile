default:
    @just --list

# ─── llm-config-setup: one engine, centralized ───────────────────────────
# tools/harness.py is the ONLY engine and lives ONLY here — it is never copied
# into a destination. It reads ~/.shared-llm.yaml and runs every copy/compose/
# link/global operation centrally against the destination paths listed there.
# The command surface is small on purpose: init once, configure once per repo,
# update whenever layers change.
#
# Tool modules are self-contained directories (script + config + own justfile),
# imported below. PUBLIC modules live under .shared-llm/public/extensions/common/
# and are kit-synced to every destination (import the same lines there);
# this_repo modules under .shared-llm/public/extensions/this_repo/ are repo-owned.

import '.shared-llm/public/extensions/common/upagent/justfile'
import '.shared-llm/public/extensions/common/herdr/justfile'
import '.shared-llm/public/extensions/common/runner/justfile'
import '.shared-llm/public/extensions/common/iac/justfile'
import '.shared-llm/public/extensions/this_repo/pi-hub/justfile'
import '.shared-llm/public/extensions/this_repo/tf/justfile'

# One-time OS prerequisite check (python3 + just). e.g. `just init -o mac`.
init *args:
    ${PYTHON_BIN:-python3} tools/harness.py init {{args}}

# Create/update ~/.shared-llm.yaml. Examples:
#   just configure -s ~/.shared-llm
#   just configure -d /path/to/repo -l cc,pi
#   just configure -g cc,pi                     # set the global harness list
configure *args:
    ${PYTHON_BIN:-python3} tools/harness.py configure {{args}}

# The headline command: copy → compose → link (+ global) across every configured
# destination. `just update -v` prints per-file detail (always written to the log
# under /tmp/.shared-llm/log/).
update *args:
    ${PYTHON_BIN:-python3} tools/harness.py update {{args}}

# Verify skill placement per harness (do-* Pi-only, cc-* Claude-only, common both).
# Run this on any machine after `just update` to confirm the layout is correct.
check:
    ${PYTHON_BIN:-python3} tools/harness.py check

# Regenerate the derived inventory block in README.md (skill/agent/slash-command
# counts + tables) from the compose recipes. Run after adding, removing, or
# renaming a recipe. Idempotent — a second run is a zero diff.
inventory:
    ${PYTHON_BIN:-python3} tools/gen_inventory.py

# ── Hidden building blocks (run independently; `just update` runs them in order)
[private]
copy:
    ${PYTHON_BIN:-python3} tools/harness.py copy

[private]
compose:
    ${PYTHON_BIN:-python3} tools/harness.py compose-dests

[private]
link:
    ${PYTHON_BIN:-python3} tools/harness.py link

[private]
global:
    ${PYTHON_BIN:-python3} tools/harness.py global

# Remove manifest-tracked home deployments that no recipe produces anymore
# (renamed/retired skills, agents, hooks). Same pruning `just update` runs.
prune:
    ${PYTHON_BIN:-python3} tools/harness.py prune

# Install the kit's pinned third-party Pi extensions from the manifest (idempotent;
# no-op without pi). Wraps the `pi install` network path — kept as a helper because
# it drives the pi CLI, not the file composer.
pi-extensions:
    @command -v pi >/dev/null || { echo "pi not found — skipping"; exit 0; }
    bash tools/install-pi-extensions.sh

# ─── Kit self-hosting ─────────────────────────────────
# The kit tracks its OWN composed slash-command skills under .claude/skills/{cc,do}-*
# (it self-hosts the workflow suite). Regenerate them after editing a slash-command
# layer. The common layers ship {{OPS_REPO}} as a placeholder (a real destination
# fills it from ~/.shared-llm.yaml `placeholders:`); here we fill it with the generic
# 'your-repo-ops' so the kit's tracked outputs stay byte-identical. Stages into the
# gitignored examples/ dir, then copies back ONLY the skills the kit already tracks —
# it never adds untracked composed skills to .claude/skills/.
selfcompose:
    rm -rf examples/self
    ${PYTHON_BIN:-python3} tools/harness.py compose .shared-llm/public/compose/slash-commands --target examples/self --placeholder OPS_REPO=your-repo-ops
    for d in .claude/skills/*/; do n=$(basename "$d"); case "$n" in lavish) continue ;; esac; cp "examples/self/.claude/skills/$n/SKILL.md" "$d/SKILL.md" || exit 1; done
    @echo "selfcompose: regenerated the kit's tracked slash-command skills (.claude/skills/{cc,do}-*)"

# ─── Tests ────────────────────────────────────────────
# Python composer/flow tests, plus the zero-dep Node type-stripping unit tests.
test:
    ${PYTHON_BIN:-python3} -m pytest tools/ .shared-llm/public/extensions/common/ -q
    node --experimental-strip-types .shared-llm/public/llm/pi/common/meta-plan/meta-plan-schema.test.ts
    node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/auto-compact.test.ts
    node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/disable-amazon-bedrock.test.ts
    node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/resolver-parity.test.ts

# Runs planish_resolve.py and both Pi extensions that delegate to it over one corpus.
test-resolver-parity:
    node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/resolver-parity.test.ts

test-auto-compact:
    node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/auto-compact.test.ts

test-provider-policy:
    node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/disable-amazon-bedrock.test.ts

test-memsearch:
    node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/memsearch.test.ts

test-meta-plan:
    node --experimental-strip-types .shared-llm/public/llm/pi/common/meta-plan/meta-plan-schema.test.ts
