# Review PR security

Inspect affected security boundaries using the appended contracts, not an unrelated repository-wide checklist.

Example: `/pr-security repo=. pr=42 scope="authorization and outbound request changes"`

## Procedure

1. Establish the shared snapshot and identify changed assets, actors, entry points, and documented security expectations. Trace authentication, authorization, tenant isolation, and object ownership from actual entry points through affected data access or privileged effects. Locate the source of identity/context and inspect downstream enforcement.
2. Follow attacker-controlled inputs through intermediate transformations to applicable sensitive sinks: queries, commands, paths, templates, deserialization, outbound requests, or privileged actions. Inspect validation, encoding, parameterization, canonicalization, and redirect handling on that path. Distinguish reachable exposure from unused helpers and unsupported configurations.
3. Inspect changed secret handling, logs, errors, and telemetry for disclosure and retention consequences. For dependency/supply-chain changes, identify selected versions, advisory/provenance evidence, reachable uses, and exploit prerequisites. Record reasons for security categories that do not apply to this change.
4. For each suspected defect, reconstruct actor capabilities, credentials/settings, controlled input, missing boundary, sink, and concrete impact. Seek counterevidence in callers and necessary enforcement. Label the trace as static evidence or an observed explicitly authorized isolated probe; incomplete chains become verification gaps, not speculative vulnerabilities.
5. Return the shared report with revision-qualified entry, enforcement, and sink locations, exploit preconditions, and the smallest correction plus isolated verification approach. Use `PR-SEC-001`, etc. for new findings. Include incomplete traces and unavailable dependency evidence in Checks and Limits; approval covers only the requested security scope.
