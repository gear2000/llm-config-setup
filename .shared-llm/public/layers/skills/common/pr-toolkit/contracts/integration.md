## Integration contract

Applies where changed in-scope behavior interacts with other components or execution stages. Record a reason for excluded handoffs or nonapplicability; unrelated end-to-end systems are not mandatory review targets.

- The affected end-to-end path works from a real API, CLI, event, job, or other entry point through component handoffs to its externally observable result. Compatible helper signatures alone do not establish integration.
- Producers and consumers agree on data shape, semantics, identity/context, ownership, ordering, and lifecycle at each affected handoff, including wiring, configuration, registration, and serialization where relevant.
- Errors, cancellation, timeouts, and partial failures propagate or recover as required without hidden success or unintended side effects. Retry policy, idempotency/deduplication, transaction boundaries, and concurrency preserve the affected contract where applicable.
- Evidence connects actual callers, receivers, and assertions across the path. Mocks or local unit tests that bypass a handoff do not establish that handoff; missing real-path evidence is explicit.
