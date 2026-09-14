# validate

Run the approved plan's integration, acceptance and other required checks with a lower-cost UpAgent worker. Use within phases and at the combined-plan boundary wherever the plan requires; cheaper execution does not mean weaker checks.

## Select one runner before execution

- `cursor-composer-2-5`, effort `default`.
- `pi-gpt-5-5`, effort explicitly chosen by the human.
- `cursor-grok-4-6`, effort `default`; high is already part of its native model.

Record the human's choice and existing persona in the execution packet; never substitute an unavailable offering.

## Procedure and requirements

Hire through UpAgent in the authorized checkout. Treat checks that may write files as mutating work: one writer at a time. Give the runner the exact required commands or acceptance criteria, candidate identity and expected evidence. It may run checks, not silently fix production code or weaken tests.

Require commands, exit codes, logs and candidate identity. Missing evidence, skipped required checks or nonzero required checks block advancement. Return failures to the controller for a coding hire within the owning phase's approved repair bounds. Recheck changed candidates. Preserve approval gates, especially for live infrastructure, destructive checks and merge operations.

The controller inspects evidence and records requests, receipts and results in the run status; it does not perform production fixes itself. Resume existing requests instead of duplicating them. Escalate uncertainty through the root HIL. A test pass is not an independent adversarial audit, final acceptance or push/deploy permission.
