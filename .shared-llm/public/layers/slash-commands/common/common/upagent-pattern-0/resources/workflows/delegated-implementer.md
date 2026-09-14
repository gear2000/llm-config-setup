# delegated-implementer

```text
Human <-> HIL <-> plan-implementer -> UpAgent workers
```

Reuse Flow 1's `/hil` start/relay/finish and `/plan-implementer` worker-hire contract. The human explicitly selects an offering/effort from the plan-implementers roster. The implementer receives the approved plan, phase-to-document assignments, full selected documents and settled run choices in the frozen Pattern 0 execution packet.

## Procedure and requirements

The implementer reads the assigned document before each phase, owns scope and briefs, hires the specified workers, inspects actual evidence, records results and attempt counts, and asks the human through the HIL when blocked. Use existing request registration, inbox, needs-input, await and completion protocols unchanged.

Delegate all production coding, fixes, validation and independent reviews through UpAgent. Pattern 0 does not use Flow 1's local-coding exception. One authorized writer at a time; fresh independent reviewers; explicit existing personas and supported offerings/efforts. Missing choices or unavailable models mean ask, not substitute.

At most one intermediate workflow controller. This implementer hires ordinary workers, not another implementer, phase leader or TUI controller. Ordinary workers cannot hire workers. Existing broker lifecycle helpers and explicitly allowed specialist consults remain support, not another workflow-controller level.

Follow plan dependencies and required checks; advance only on evidence for the current candidate. Resume recorded requests and counters without resets. Run all assigned final audits before a passing implementer result. Keep the original acceptance criteria and human approval gates; a merged candidate is not automatically accepted, pushed or deployed. The root HIL remains the human's only contact.
