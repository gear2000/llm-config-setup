# Shared cc/do-implement contract

Implement an approved big plan directly in one fresh human-in-the-loop TUI. Do not decompose it for Herdr.

## Invocation

```text
/<command> <approved-plan.md>
```

## Workflow

1. Verify the plan is approved. If approval is unclear, stop and ask.
2. Start from a clean, fresh implementation context where the harness supports that. Keep the session interactive so the human can answer product, safety, or scope questions.
3. Read the plan, repository instructions, and relevant code before editing.
4. Implement the plan directly in the current repo, preserving unrelated dirty/untracked work.
5. Run the plan's checks and any focused tests required by the changed surface.
6. Report changed files, verification, and any remaining risks.

## Hard rules

- Do not call `/cc-convert`, `/do-convert`, `just run-start`, `/tui-control`, or `/phase-leader`.
- Do not create `route.yaml` or runner phase files.
- Do not perform a second planning/adversarial review loop. The input plan is already approved.
- If the plan reveals a material unresolved architecture or product fork during implementation, stop for the human instead of filling the gap silently.
