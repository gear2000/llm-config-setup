## Boundary-tracing contract

Applies where changed in-scope behavior crosses input, identity, component, or effect boundaries. For an unaffected boundary, record why it is not applicable rather than inventing a failed or unverified check.

- A trace connects a real entry point, input or actor, intermediate transformations, enforcement points, and observable result or sensitive effect. An internal helper alone does not establish behavior at the public boundary.
- Claimed validation or enforcement must necessarily occur on the affected path, including relevant alternate/error paths. Checks in unrelated callers do not protect this path.
- Evidence identifies where identity, tenant context, data shape, or ownership changes, and whether each receiver preserves the originating contract.
- Malformed or rejected input evidence covers both the expected error and prohibited side effects. Incomplete traces identify the missing handoff rather than assuming safety or reachability.
