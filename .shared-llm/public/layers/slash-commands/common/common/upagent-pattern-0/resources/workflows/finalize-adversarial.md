# finalize-adversarial

Audit the complete combined candidate after every phase and required check has passed. An earlier merge required by the approved integration/acceptance plan does not replace this gate.

## Select reviewers before execution

Choose one model, or two distinct models for a very big plan:
- `claude-fable-5-1`, effort `low`.
- `pi-gpt-5-6-sol`, effort `high`.
- `pi-gpt-6-astra`, effort `medium`.

Record the explicit selection and existing reviewer personas in the execution packet. When choosing two, also settle sequential versus parallel execution. There is no fixed work-tier mapping, silent fallback or automatic third review.

## Procedure and requirements

Hire fresh independent adversaries through UpAgent. Give each the approved plan, complete diff/candidate identity, phase outcomes and validation evidence. Each audits the full combined scope, not merely its preferred phase. Two parallel reviews are read-only against the same frozen candidate with no writer active.

All selected reviews must pass against the accepted candidate. One pass cannot override another review's findings. Report failures to the owning controller and root HIL; do not silently mark completion or start an unlimited repair loop. Human-authorized repairs go through the owning phase and require fresh relevant checks and final audits for the changed candidate.

The controller owns scope, evidence inspection and decisions; it delegates production fixes and independent review. Record requests, receipts, findings, candidate identity and outcomes in run status. Resume without duplicate requests or reset counters. Keep original acceptance criteria, human gates and worktree authorization. A final pass does not grant push, deployment or rollback permission.
