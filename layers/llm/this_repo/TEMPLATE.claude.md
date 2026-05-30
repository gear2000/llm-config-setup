<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to claude.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# Claude Code — Project Conventions

## Worktree-first across repos

Daily work happens in a worktree on a branch, not on `main`. When you start work on `<name>`, create a worktree of the **same name** on a branch of the **same name** in each repo you'll be touching:

- `{{WORKTREE_ROOT_CODE}}/<name>/` — this code repo
- `{{WORKTREE_ROOT_OPS}}/<name>/` — CI configs, docs, work-log
- `{{WORKTREE_ROOT_INFRA}}/<name>/` — infra (only if touched)

<!-- TODO(project): Replace {{WORKTREE_ROOT_CODE}}, {{WORKTREE_ROOT_OPS}}, and {{WORKTREE_ROOT_INFRA}} with the actual paths on your machine (e.g. ~/project/repos/myapp-trees, ~/project/repos/myapp-ops-trees, ~/project/repos/myapp-infra-trees). Delete the ops/infra lines if you work in a single repo. -->

All branches share the same name. `main` of every repo is the integration point — kept clean, merged into only after branches land.

## Every plan ends with a docs-check phase

When using `/do:plan*` or any planning skill in this repo, the final phase of every plan MUST be a docs-check that runs:

```bash
# Example — adapt {{SOURCE_GLOB}} and {{DOCS_UPDATE_SKILL}} for your project:
# changed=$(git diff main...HEAD --name-only | grep -E '^{{SOURCE_GLOB}}' || true)
# [ -z "$changed" ] && exit 0  # nothing touched — no-op pass
# # else: for each touched package/service, invoke {{DOCS_UPDATE_SKILL}} --<type> <name>
```

<!-- TODO(project): Replace {{SOURCE_GLOB}} with the path prefix that identifies source files needing doc updates (e.g. src/(packages|services)/). Replace {{DOCS_UPDATE_SKILL}} with the skill name you use to update per-component docs (e.g. /update-docs). Delete this phase if you have no per-package/service docs. -->

Phase no-ops cleanly when no relevant changes. No exceptions, no project-wide planning skill modifications — the rule lives here.

## CI delegation

Long-running CI wait calls can be delegated to a subagent so the main session stays responsive.

## Operating principles (apply to every agent task)

- **Delegate, don't do.** The team leader orchestrates — dispatches agents, reads their returns, decides next step. The leader does not read code or write files itself without explicit user approval. Subagent fresh-context windows keep the leader sharp and the work clean.
- **Default transport: subagents.** Fresh `Agent()` per unit of work, no `team_name`. Use `--team` only when you need TMUX visibility or lateral SendMessage.
- **Summon specialists ad-hoc.** Start with 1 agent. Add `deployer`, `code-review`, `plan-watchdog`, `database`, `security`, `devops` only when the work concretely needs them.
