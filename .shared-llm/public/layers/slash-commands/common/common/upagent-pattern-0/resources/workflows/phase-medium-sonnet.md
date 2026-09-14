# phase-medium-sonnet

```text
Sonnet 5 high codes -> GPT 5.5 high reviews
  -> repair/review -> GPT 5.6 Sol high audits the phase
  -> required checks + pass -> next phase
```

## Models

- Coder: `claude-sonnet-5`, effort `high`.
- Independent reviewer: `pi-gpt-5-5`, effort `high`.
- Independent phase auditor: `pi-gpt-5-6-sol`, effort `high`.

## Procedure

1. Before hiring, obtain explicit coding/review and audit-repair limits from the human; record them in the execution packet. There is no default retry count.
2. Hire Sonnet for the assigned scope. Hire a fresh GPT reviewer against its candidate. Return findings to Sonnet; keep the same coder model through bounded retries.
3. Once review passes, hire Sol for the phase audit. Return failures to the coder within the approved bounds, then repeat affected review/audit. Exhausted bounds mean stop and ask, not another automatic hire.
4. Run the plan's required checks using its selected validation document. Advance only with passing review, audit and checks on the current candidate. The assigned final combined-plan audit still runs.

## Controller requirements

Delegate production coding, fixes, tests and independent reviews through UpAgent. The controller owns scope, briefs, evidence inspection and next action; it does not code. Use one writer in the authorized checkout and fresh independent reviewers. Select existing personas and validate exact offerings/efforts before hiring; ask on missing choices or unavailable models, never substitute.

Record requests, receipts, candidate identity, commands, exit codes, findings and attempt counts in the run status. Resume without duplicate hires or reset counters. Changed code needs fresh relevant checks/reviews. Preserve human gates and original acceptance criteria. Only the root HIL talks to the human; an intermediate implementer relays questions through it. Ordinary workers do not hire workers. A merge needed by the approved plan is not final acceptance or push/deploy authority.
