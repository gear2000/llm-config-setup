# /meta-cc

Claude Code-side meta runner. This command is distinct from `cc-*` front-door workflow commands. Use `cc-*` to research, plan, or create a normal work plan; use `/meta-cc` to execute a canonical meta plan with a route profile.

## Invocation

```text
/meta-cc --plan <plan.md> --route <route.yaml> --output-dir <dir> [--start-phase <N>] [--max-phases <N>]
```

Required:

- `--plan` — canonical meta plan.
- `--route` — route profile with `llm_profiles` and inline `agent` names.
- `--output-dir` — result/evidence directory.

## Execution

1. Run the same gate as `/meta-plan-check <plan.md> <route.yaml>`. If it fails, stop before creating any phase Lead Agent and tell the user to run `/meta-plan-convert` or fix the files.
2. Validate and resolve the route profile:
   - every phase has `lead.llm_profile`, `lead.agent`, and deterministic `merge_back_at`;
   - every phase has all five stages with `llm_profile` and `agent`;
   - worktree branch template, green checks, and log checks are configured;
   - each profile exists;
   - each named agent resolves in Claude Code project/home agent directories or the selected harness context;
   - Stage 2 is independent from Stage 1.
3. For each phase, create one phase Lead Agent using the phase lead profile and agent name.
4. The phase Lead Agent runs the shared five-stage worktree protocol and writes a phase result file: Stage 1/2 on the temporary worktree branch, deterministic merge at Stage 3/4/5, then Stage 5 cleanup, green checks, and log review.
5. Read the phase result file as the source of truth before continuing.

## Relationship to `cc-*`

`cc-*` commands are front-door Claude Code workflow commands. They are comparable to Pi's `do-*` commands and are not meta runners. A plan produced by `/cc-plan-and-grill` may be normalized into canonical meta-plan shape and paired with a route profile, then executed with `/meta-cc`.

## Hard rules

- Do not auto-convert at execution time. `/meta-cc` only runs already-runnable `plan.md + route.yaml` inputs.
- No Claude team mode for synchronized meta execution.
- No nested delegation beyond: `/meta-cc` → phase Lead Agent → one non-delegating stage agent at a time.
- Advisors may be configured for a phase lead or stage agent when supported, but advisors do not write files, run commands, or create agents.
- Result files are the source of truth.
