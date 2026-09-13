<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to claude.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# Claude Code

Work happens in a worktree on a branch, not on `main`. Same branch name in every repo you touch:

- `{{WORKTREE_ROOT_CODE}}/<name>/`  this code repo
- `{{WORKTREE_ROOT_OPS}}/<name>/`  CI, docs, work-log
- `{{WORKTREE_ROOT_INFRA}}/<name>/`  infra (only if touched)

<!-- TODO(project): Replace {{WORKTREE_ROOT_CODE}}, {{WORKTREE_ROOT_OPS}}, and {{WORKTREE_ROOT_INFRA}} with the actual paths. Delete the ops/infra lines if you work in a single repo. -->

`main` stays the integration point.

Every `/do-plan*` plan ends with a docs-check. No relevant changes: no-op.

```bash
# Example. Adapt {{SOURCE_GLOB}} and {{DOCS_UPDATE_SKILL}}:
# changed=$(git diff main...HEAD --name-only | grep -E '^{{SOURCE_GLOB}}' || true)
# [ -z "$changed" ] && exit 0
# # else: for each touched package/service, invoke {{DOCS_UPDATE_SKILL}} --<type> <name>
```

<!-- TODO(project): Replace {{SOURCE_GLOB}} and {{DOCS_UPDATE_SKILL}}. Delete this phase if you have no per-package/service docs. -->

Long CI waits can go to a subagent so this session stays responsive.

Start with one agent. Add a specialist only when the work needs it. The leader does not read or write code unless the user approved it.
