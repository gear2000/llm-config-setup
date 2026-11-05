## Architecture contract

Applies to substantive changed ownership, dependency, lifecycle, or component-contract decisions. Record why unaffected architectural concerns are not applicable. Naming or stylistic preferences alone are not architecture defects.

- Responsibilities and ownership of state, resources, invariants, and failure handling remain explicit and coherent with accepted requirements and documented architectural constraints.
- Dependencies point in permitted directions; new coupling, cycles, cross-layer access, and public contracts do not undermine required isolation or independent evolution.
- Affected interfaces and lifecycle boundaries preserve invariants across callers and implementations, including extension points and alternate implementations where supported.
- Evidence connects the changed structure to a concrete behavioral, reliability, changeability, or ownership consequence. Departures from an established pattern need an evidenced violated constraint, not an assumption that all differences are wrong.
- Proposed corrections address the smallest substantive issue; unrelated redesign is outside this review.
