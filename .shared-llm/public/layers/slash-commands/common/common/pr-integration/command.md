# Review PR integration

Trace affected end-to-end component behavior using the appended contracts, not merely signatures or isolated helper tests.

Example: `/pr-integration repo=. pr=42 scope="request-to-queue-to-worker handoff"`

## Procedure

1. Establish the shared snapshot and select changed real API, CLI, event, job, or other entry points and their expected results. Read complete participating files, callers, wiring, configuration, registrations, and relevant tests. Bound the path to affected behavior and explain excluded handoffs.
2. Follow each path across components to observable outputs and side effects. At each handoff compare producer/consumer data shape, semantics, identity/context, ownership, serialization, ordering, and lifecycle. Check the target base or supported old/new combinations where integration spans versions.
3. Walk relevant error, cancellation, timeout, and partial-failure paths. Trace retries, idempotency/deduplication, transaction boundaries, and concurrent interleavings where the change affects them. Look for dropped context, hidden failure, duplicate effects, lost updates, and inconsistent state; seek counterevidence before reporting a defect.
4. Map end-to-end claims to assertions that actually cross affected handoffs. Identify mocks or local tests that bypass them. Separate static path evidence, supplied integration results, and explicitly authorized isolated probes; name missing environments or observations without broadening execution authority.
5. Return the shared report with a concise entrypoint-to-result handoff map, affected contracts, failure/recovery evidence, and limits. Use `PR-INT-001`, etc. for new findings. Propose focused verification at the broken handoff rather than a mandatory full-system test campaign; this scope does not establish hosted readiness.
