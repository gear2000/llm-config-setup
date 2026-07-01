default:
    @just --list

# ─── Harness setup: skills / agents / extensions ──────
# One Python tool (tools/harness.py) both composes layer files and reconciles the
# per-harness symlinks (pi, codex) — creating missing links, re-pointing drifted
# ones, and PRUNING links whose source was renamed or deleted. These recipes are
# the thin entry point; the Taskfile drives the same tool for compose in CI.

# Reconcile all pi/codex links (create + re-point + prune). Pass flags through,
# e.g. `just sync --plan` to preview, `just sync --harness pi`.
sync *flags:
    python3 tools/harness.py sync {{flags}}

# Remove every managed pi/codex link.
unlink *flags:
    python3 tools/harness.py unlink {{flags}}

# Compose skills/agents/CLAUDE.md from layers (same engine the Taskfile uses).
compose *args:
    python3 tools/harness.py compose {{args}}

# Full setup: reconcile every link, then ensure the third-party Pi companions.
setup: sync companions

# ─── Sync back to the private consumer repo ────────────
# This kit is the SOURCE for common/ layer content — when you edit/generate
# something here, pull it into the private repo with this. Auto-diffs every
# common/ layer tree (agents, llm, skills, slash-commands, the Claude/Pi
# harness runtime config) against the private repo and copies over anything
# new or changed, then recomposes + verifies there. Never touches this_repo/
# paths or compose/ recipe files — those wire proprietary overlays on the
# private side and stay untouched. See tools/sync-to-private.py for the scope.
#
# `just sync-to-private` (default path ../jiffy-rewrite-2026)
# `just sync-to-private /path/to/jiffy-rewrite-2026 --dry-run`
sync-to-private private_root="../jiffy-rewrite-2026" *flags:
    python3 tools/sync-to-private.py --private-root {{private_root}} {{flags}}

# Install the kit's pinned third-party Pi extensions from the manifest (idempotent; no-op without pi).
companions:
    @command -v pi >/dev/null || { echo "pi not found — skipping companions"; exit 0; }
    bash tools/install-pi-extensions.sh

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
