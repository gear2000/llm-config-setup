# phase-high-Fable

```text
Fable 5.1 low codes -> GPT 5.6 Sol high adversarial review
  -> required checks + pass -> next phase
```

## Models

- Coder: `claude-fable-5-1`, effort `low`.
- Independent adversarial reviewer: `pi-gpt-5-6-sol`, effort `high`.

## Procedure

1. Settle the repair/review limit with the human; record it in the execution packet before hiring. There is no default retry count.
2. Hire Fable for the assigned scope and original acceptance criteria. Hire a fresh Sol adversary against the resulting candidate. Return findings to Fable within the approved retry bound. Exhaustion means stop and ask the human.
3. Run the plan's required checks using its selected validation document. Advance only when the current candidate passes review and checks. Do not add an intermediate review or another phase audit. The assigned final combined-plan audit still runs.

## Controller requirements

Delegate production coding, fixes, tests and independent reviews through UpAgent. The controller owns scope, briefs, evidence inspection and next action; it does not code. Use one writer in the authorized checkout and fresh independent reviewers. Select existing personas and validate exact offerings/efforts before hiring; ask on missing choices or unavailable models, never substitute.

Record requests, receipts, candidate identity, commands, exit codes, findings and attempt counts in the run status. Resume without duplicate hires or reset counters. Changed code needs fresh relevant checks/reviews. Preserve human gates and original acceptance criteria. Only the root HIL talks to the human; an intermediate implementer relays questions through it. Ordinary workers do not hire workers. A merge needed by the approved plan is not final acceptance or push/deploy authority.
