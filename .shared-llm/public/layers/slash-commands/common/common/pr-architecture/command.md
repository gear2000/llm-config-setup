# Review PR architecture

Assess substantive ownership, dependency, and contract changes using the appended contracts. This is not a style review or a mandate to redesign the repository.

Example: `/pr-architecture repo=. pr=42 scope="service ownership and storage interface boundary"`

## Procedure

1. Establish the shared snapshot and read accepted requirements, relevant architecture decisions, affected full files, callers, implementations, and tests. Identify changed responsibilities and constraints; explain unaffected architectural areas rather than judging the entire system.
2. Map ownership of state, resources, invariants, and failure handling before and after the change. Trace lifecycle and extension points, alternate implementations, and responsibilities at boundaries. Identify ambiguous or conflicting ownership with a concrete consequence.
3. Follow dependency direction and public contracts across components. Inspect introduced cycles, coupling, cross-layer access, and abstraction leakage against documented constraints and supported evolution. Do not treat a different pattern or naming choice as a defect by itself.
4. Trace affected real entrypoints and component handoffs to check whether the structure preserves behavior, context, errors, and recovery. Use tests and callers to challenge architectural concerns; distinguish a source/design conclusion from an observed runtime result.
5. Return the shared report with a concise responsibility/dependency map, violated constraints and concrete consequences, alternatives where a decision is missing, and evidence gaps. Use `PR-ARCH-001`, etc. for new findings. Recommend the smallest substantive correction; keep unrelated redesign, stylistic preferences, and hosted readiness outside this conclusion.
