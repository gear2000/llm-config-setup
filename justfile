default:
    @just --list

# ─── Terraform Workflow ───────────────────────────────
# Extensions: ~/.pi/extensions/tf-write.ts, tf-approve.ts
# Agent:      ~/.pi/agents/tf-reviewer.md

# Autonomous AFK loop: write and review terraform code until approved
tf-write plan:
    TF_PLAN_PATH={{plan}} pi -e ~/.pi/extensions/tf-write.ts

# Human-gated terraform apply/destroy with agent-distilled plan table
tf-approve:
    pi -e ~/.pi/extensions/iac-guard.ts -e ~/.pi/extensions/tf-approve.ts

# Full workflow: write reviewed code then human-gated apply
tf-auto plan:
    TF_PLAN_PATH={{plan}} pi -e ~/.pi/extensions/tf-write.ts && \
    pi -e ~/.pi/extensions/iac-guard.ts -e ~/.pi/extensions/tf-approve.ts

# Start the terraform reviewer (Claude Code) — run before tf-write or tf-approve
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

# Start the terraform reviewer (Pi) — run before tf-write or tf-approve
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
