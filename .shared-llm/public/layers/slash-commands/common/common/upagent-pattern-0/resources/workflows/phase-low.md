# phase-low

```text
Composer 2.5 codes -> Cursor Grok 4.6 high reviews
  -> repair/review -> GPT 5.5 high audits the phase
  -> required checks + pass -> next phase
```

## Models

- Coder: `cursor-composer-2-5`, effort `default`.
- Independent reviewer: `cursor-grok-4-6`, effort `default`. The native model already encodes high; do not pass `--effort high`.
- Independent phase auditor: `pi-gpt-5-5`, effort `high`.

## Procedure

1. Hire the coder for the assigned scope and original acceptance criteria. Hire a fresh reviewer against the resulting candidate. Send findings back to the coder; allow up to five total coding/review rounds. If review has not passed, stop and ask the human.
2. After review passes, hire the phase auditor. On failure, allow at most two additional coding/review rounds, then a second audit. If either bound is exhausted without a pass, stop and ask the human; do not add reviews silently.
3. Run the plan's required checks using its selected validation document. Advance only with passing review, audit and checks for the current candidate. The final combined-plan audit still runs when assigned.

## Controller requirements

Delegate production coding, fixes, tests and independent reviews through UpAgent. The controller owns scope, briefs, evidence inspection and next action; it does not code. Use one writer in the authorized checkout and fresh independent reviewers. Select existing personas and validate exact offerings/efforts before hiring; ask on missing choices or unavailable models, never substitute.

Record requests, receipts, candidate identity, commands, exit codes, findings and attempt counts in the run status. Resume without duplicate hires or reset counters. Changed code needs fresh relevant checks/reviews. Preserve human gates and original acceptance criteria. Only the root HIL talks to the human; an intermediate implementer relays questions through it. Ordinary workers do not hire workers. A merge needed by the approved plan is not final acceptance or push/deploy authority.
