# Shared cc/do-convert --herdr contract

Convert an already-approved big plan into the runnable Herdr input pair. This is the only user-facing conversion/checking seam.

## Invocation

```text
/<command> --herdr <plan.md> [--out <converted-run-dir>] [--non-interactive]
```

HTML input is not supported. If the user passes `plan.html`, ask for the paired Markdown file.

## Output shape

```text
<converted-run-dir>/
├── plan.md
├── route.yaml
├── conversion-review.html
└── conversion-receipt.json
```

The command is idempotent. Re-running with the same source plan and same explicit routing decisions must produce the same `plan.md`, `route.yaml`, and receipt, or explain the material input change.

## Conversion workflow

1. Verify that the source plan is approved for implementation. If approval is missing or ambiguous, stop and ask for it; do not silently upgrade a draft into an implementation plan.
2. Preserve the upstream big plan unchanged as source evidence. The converted `plan.md` is a Herdr execution artifact, not a rewrite of the approved plan of record.
3. Split the plan into vertical phases. Each phase must be independently reviewable and have checkable `Done:` criteria.
4. Assign Herdr route semantics:
   - base stages: `stage-1-implementation`, `stage-2-adversarial-audit`, `stage-3-integration-acceptance-seams`, `stage-4-upstream-dag-verification`, `stage-5-finalization`;
   - `accuracy: medium | high | max`;
   - `stage-0-alignment` exactly when `accuracy` is `high` or `max`;
   - `second_llm_profile` exactly when `accuracy` is `max`;
   - `kind: iac` only for infrastructure-apply phases;
   - `parallel_group` only when the human explicitly accepts the risk of parallel phase start;
   - `merge_back_at` at Stage 3, Stage 4, or Stage 5.
5. Recommend an optional integration-construction phase when several phase outputs must be wired together before candidate-level validation.
6. Move all profile, model, harness, agent, worktree, merge, finalization, and log-check details out of `plan.md` into `route.yaml`.
7. Ask for every route value that cannot be inferred from explicit source text or configured public defaults. Never invent profiles, models, harnesses, agents, green-check commands, log sources, environment names, account details, or deployment gates.
8. If decomposition exposes an unresolved architecture or product fork, return:

   ```text
   DESIGN_REQUIRED
   ```

   Include the evidence, the unresolved decision, and the instruction to return to `/cc-plan` or `/do-plan`. Do not guess.
9. Produce a Planish-style static `conversion-review.html` showing the phase split, route choices, independence assumptions, candidate-level gates, and any deferred/not-configured items. The user annotates it and pastes feedback. Iterate until the conversion is approved.
10. Validate internally using the same runnable rules documented in `.shared-llm/public/llm/pi/common/meta-plan/meta-plan-format.md`. The user does not run a separate check command.
11. On pass, report the converted run directory and the public launcher:

    ```text
    just run-start <converted-run-dir>
    ```

## Deferred external gates

If the approved plan requires an external candidate gate such as an exact-SHA shared environment check, and that gate is not configured in public route inputs, record it as a deferred `not-configured` gate in the receipt and conversion review. Do not invent private infrastructure, account names, regions, URLs, credentials, external candidate-gate configuration, or cloud deployment mechanics.

## Hard rules

- `--herdr` is required. Without it, stop and say this converter currently supports only Herdr conversion.
- Conversion performs validation itself; do not ask the user to run `/meta-plan-check`.
- Run startup rechecks the frozen pair, but conversion must still validate before declaring the output runnable.
- A TODO route is not runnable. Either resolve the value with the human or return a clear non-runnable result.
- Do not implement the plan and do not start the checked run from this command.
