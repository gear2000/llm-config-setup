# Review PR reachability

Assess potentially unused or unreachable changed behavior under the appended contracts. Report candidates and evidence; do not delete anything.

Example: `/pr-dead-code repo=. pr=42 scope="retired adapter and exported helpers"`

## Procedure

1. Establish the shared snapshot and identify changed symbols, exports, branches, registrations, configuration, or assets that may be unused, plus paths the change disconnects. Bound the investigation to affected code and record exclusions.
2. Trace supported entry points and caller chains. Use reference searches as leads, then inspect import/export and package surfaces, build/generated registrations, scripts, configuration, dependency injection, reflection, dynamic loading, and plugins as relevant. Determine whether external consumers can reach public interfaces.
3. Compare old/new wiring and supported configurations to distinguish unreachable code from disabled-by-default features, conditional paths, deprecations, redundancy, and supported external use. Seek positive evidence for unreachability rather than treating absent grep references as proof.
4. Examine tests and documentation that demonstrate intended use or support commitments. Record dynamic or external-consumer uncertainty explicitly; use only already authorized isolated probes if static evidence cannot resolve a bounded question.
5. Return the shared report with each candidate's classification, supported entrypoint/configuration coverage, evidence, and remaining uncertainty. Use `PR-DEAD-001`, etc. for new findings. Explain the behavioral/compatibility consequence of a proposed cleanup and how the owner could verify it; leave deletions and other edits to the owner.
