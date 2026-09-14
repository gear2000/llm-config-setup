# phase-low-Luna

```text
GPT 5.6 Luna max codes -> Sonnet 5 high reviews
  -> repair/review -> required checks + pass -> next phase
```

Default easy-tier document while GPT 5.6 Luna is discounted. The reviewer is always the opposite model family from the coder.

## Models

- Coder: `pi-gpt-5-6-luna`, effort `max`.
- Independent reviewer: `claude-sonnet-5`, effort `high`.

## Procedure

1. Before hiring, obtain the explicit coding/review repair limit from the human; record it in the execution packet. There is no default retry count.
2. Hire Luna for the assigned scope and original acceptance criteria. Hire a fresh Sonnet reviewer against its candidate. Return findings to Luna; keep the same coder model through bounded retries. Exhaustion means stop and ask the human.
3. Run the plan's required checks using its selected validation document. Advance only with passing review and checks on the current candidate. Do not add a phase audit. The assigned final combined-plan audit still runs.

## Controller requirements

Delegate production coding, fixes, tests and independent reviews through UpAgent. The controller owns scope, briefs, evidence inspection and next action; it does not code. Use one writer in the authorized checkout and fresh independent reviewers. Select existing personas and validate exact offerings/efforts before hiring; ask on missing choices or unavailable models, never substitute.

Record requests, receipts, candidate identity, commands, exit codes, findings and attempt counts in the run status. Resume without duplicate hires or reset counters. Changed code needs fresh relevant checks/reviews. Preserve human gates and original acceptance criteria. Only the root HIL talks to the human; an intermediate implementer relays questions through it. Ordinary workers do not hire workers. A merge needed by the approved plan is not final acceptance or push/deploy authority.
