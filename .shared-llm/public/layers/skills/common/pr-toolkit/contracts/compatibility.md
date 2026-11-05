## Compatibility contract

Applies to changed externally observable or persisted contracts and their affected consumers. Record why unchanged contract categories or unsupported combinations are excluded; compatibility is judged against declared support policy, not perpetual backward compatibility.

- Relevant contracts include APIs/errors, schemas, configuration keys/defaults, environment variables, CLI flags/output/exit codes, events, and persisted/serialized formats. Old/new semantics, removals, aliases, stricter validation, and defaults are accounted for where observable.
- Supported versions, deprecations, rollout order, and breaking-change policy have a repository or accepted-requirement basis. Missing policy is an evidence/decision gap, not permission to break consumers or an invented promise to support all versions.
- Evidence covers the applicable old/new producer-consumer combinations: clients/servers, mixed-version services, readers/writers, stored data, and events. Exclusions cite policy. Unknown external consumers remain explicit limitations.
- Defaults, unknown/required fields, enum expansion, and errors preserve the supported combinations. Defects identify a concrete violated combination and consumer impact.
- Intentional breaks are acceptable when policy-required versioning, coordination, migration instructions, release notes, and rollout gates are evidenced. Compatibility approval is not hosted readiness.
