default:
    @just --list

# ─── llm-config-setup: one engine, centralized ───────────────────────────
# tools/harness.py is the ONLY engine and lives ONLY here — it is never copied
# into a destination. It reads ~/.shared-llm.yaml and runs every copy/compose/
# link/global operation centrally against the destination paths listed there.
# The command surface is small on purpose: init once, configure once per repo,
# update whenever layers change.

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

# Install the kit's pinned third-party Pi extensions from the manifest (idempotent;
# no-op without pi). Wraps the `pi install` network path — kept as a helper because
# it drives the pi CLI, not the file composer.
pi-extensions:
    @command -v pi >/dev/null || { echo "pi not found — skipping"; exit 0; }
    bash tools/install-pi-extensions.sh

# ─── Tests ────────────────────────────────────────────
# Python composer/flow tests, plus the zero-dep Node type-stripping unit tests.
test:
    ${PYTHON_BIN:-python3} -m pytest tools/ -q

test-iac-guard:
    node --experimental-strip-types .shared-llm/llm/pi/common/extensions/iac-guard.test.ts

test-memsearch:
    node --experimental-strip-types .shared-llm/llm/pi/common/extensions/memsearch.test.ts

# ─── Pi launch group (hub + builder; requires tmux) ───
# Start the codex/iac-verifier socket hub detached in tmux (idempotent).
hub:
    tmux has-session -t pi-hub 2>/dev/null || tmux new-session -d -s pi-hub 'pi -e ~/.pi/extensions/codex-reviewer-hub.ts'
    @echo "pi-hub up — tmux attach -t pi-hub to watch"

# Launch a builder Pi (iac-guard auto-loads; the hub must be up for gray-zone verdicts).
builder:
    pi

# Show hub + socket state.
pi-status:
    @tmux has-session -t pi-hub 2>/dev/null && echo "hub: up" || echo "hub: down"
    @test -S ~/.pi/codex-reviewer.sock && echo "socket: present" || echo "socket: absent"

# Stop the hub and remove stale sockets.
pi-clean:
    tmux kill-session -t pi-hub 2>/dev/null || true
    rm -f ~/.pi/codex-reviewer.sock
    @echo "clean: hub stopped, sockets removed"

# ─── Terraform Workflow ───────────────────────────────
# Extensions (auto-loaded from ~/.pi/agent/extensions/): tf-implement.ts,
#   tf-approve.ts, iac-guard.ts, planish.ts
# Agent:      ~/.pi/agents/tf-reviewer.md

# Load a plan and run the implement loop (write reviewed terraform until approved).
tf-implement plan:
    TF_PLAN_PATH={{plan}} pi -e ~/.pi/agent/extensions/tf-implement.ts

# Human-gated terraform apply/destroy with agent-distilled plan table
tf-approve:
    pi -e ~/.pi/agent/extensions/iac-guard.ts -e ~/.pi/agent/extensions/tf-approve.ts

# Full workflow: plan + implement reviewed code, then human-gated apply
tf-auto plan:
    TF_PLAN_PATH={{plan}} pi -e ~/.pi/agent/extensions/tf-implement.ts && \
    pi -e ~/.pi/agent/extensions/iac-guard.ts -e ~/.pi/agent/extensions/tf-approve.ts

# Start the terraform reviewer (Claude Code) — run before tf-implement or tf-approve
tf-reviewer-cc:
    tmux has-session -t tf-reviewer 2>/dev/null || tmux new-session -d -s tf-reviewer 'claude'
    for i in $(seq 1 60); do \
        tmux capture-pane -p -t tf-reviewer 2>/dev/null | grep -qi 'bypass permissions' && break; \
        [ "$i" = "60" ] && { echo "tf-reviewer-cc: TUI never showed ready footer in 60s" >&2; exit 1; }; \
        sleep 1; \
    done
    tmux send-keys -t tf-reviewer 'Read ~/.pi/agents/tf-reviewer.md and follow its instructions.'
    sleep 1
    tmux send-keys -t tf-reviewer Enter

# Start the terraform reviewer (Pi) — run before tf-implement or tf-approve
tf-reviewer-pi model="openai-codex/gpt-5.5":
    tmux has-session -t tf-reviewer 2>/dev/null || tmux new-session -d -s tf-reviewer 'pi --model {{model}} --thinking high -a --no-session'
    for i in $(seq 1 60); do \
        pane=$(tmux capture-pane -p -t tf-reviewer 2>/dev/null); \
        echo "$$pane" | grep -q 'Press any key to continue' && tmux send-keys -t tf-reviewer Enter; \
        echo "$$pane" | grep -qE '^ *> *$$' && break; \
        [ "$$i" = "60" ] && { echo "tf-reviewer-pi: pi never showed ready prompt in 60s" >&2; exit 1; }; \
        sleep 1; \
    done
    tmux send-keys -t tf-reviewer 'Read ~/.pi/agents/tf-reviewer.md and follow its instructions.'
    sleep 1
    tmux send-keys -t tf-reviewer Enter

# Stop the terraform reviewer
tf-reviewer-down:
    tmux kill-session -t tf-reviewer 2>/dev/null || true
