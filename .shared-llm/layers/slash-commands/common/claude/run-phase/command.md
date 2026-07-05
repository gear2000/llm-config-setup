# /run-phase

Run **one phase** of a canonical meta plan with a caller-supplied route profile. This is the argument-style sibling of `/run_phase`; it is used by Meta-ORCH / Meta-CC launch paths that pass key-value arguments instead of file-command flags.

## Invocation

```text
/run-phase plan=<path> phase=<N> route=<route.yaml>
```

Required:

- `plan=` — canonical meta plan.
- `phase=` — phase number or phase id.
- `route=` — resolved route profile with `llm_profiles` and inline `agent` names.

Fail loud on missing arguments, unreadable files, or an unknown phase.

## Execution

1. Read the whole plan and route profile, then run the same gate as `/meta-plan-check <plan.md> <route.yaml>`. If it fails, stop before phase work.
2. Locate the requested phase and validate the route profile for that phase:
   - `lead.llm_profile` and `lead.agent` exist;
   - all five stages exist;
   - `merge_back_at` is Stage 3, Stage 4, or Stage 5;
   - worktree branch template, green checks, and log checks are configured;
   - every stage has `llm_profile` and `agent`;
   - referenced profiles exist;
   - named agents resolve in the selected harness/project context;
   - Stage 2 is independent from Stage 1.
3. Act as the phase Lead Agent.
4. Run the shared five-stage worktree protocol:
   - pre-flight dependency/import safety;
   - Stage 1 unit tests + implementation on the temporary worktree branch;
   - Stage 2 adversarial audit on the same temporary worktree branch, including a hard gate for unused intake / accepted-but-ignored inputs using AST-aware, call-site, static-analysis, and semantic checks where available;
   - Stage 3 integration/acceptance seam testing, merging only if `merge_back_at` selects Stage 3;
   - Stage 4 upstream DAG dependent verification, merging only if `merge_back_at` selects Stage 4;
   - Stage 5 finalization: merge if needed, verify main, destroy temp worktree/branch, run green checks, and inspect logs.
5. Emit a final verdict line:

   ```text
   PHASE_RESULT: passed|partial|blocked|failed
   ```

## Route profile, not global agent list

The synchronized meta path does not use a single global worker roster. It uses a route profile where every phase lead and every stage has explicit `llm_profile` and `agent` fields. Legacy `agents=`-style global rosters are not enough for this protocol; convert them into a route profile before running.

## Escalation

If `ask_brain` is injected, use it when the phase is blocked, a leader decision is needed, or a plan-changing discovery occurs. If `ask_brain` is absent, fail loud or report what would have been escalated; never fabricate a leader reply.

## Hard rules

- Canonical plan body stays free of runner/stage routing.
- Do not auto-convert at phase execution time; invalid inputs fail before semi-AFK work.
- Stage agents and advisors are non-delegating.
- No Claude team mode for synchronized meta execution.
- Stage 2 must be independent from Stage 1.
- Runtime leads must follow `merge_back_at`; they do not choose merge timing during execution.
- Never report `passed` unless all required stages, Stage 5 green checks, log review, and cleanup passed.
