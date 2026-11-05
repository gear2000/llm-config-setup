# Review PR compatibility

Assess affected external and persisted contracts under the appended contracts and the repository's declared support policy.

Example: `/pr-compatibility repo=. pr=42 scope="CLI defaults and stored-format migration"`

## Procedure

1. Establish the shared snapshot and inventory changed contracts and actual producers/consumers: APIs/errors, schemas, configuration/defaults, environment variables, CLI flags/output/exit codes, events, and persisted/serialized formats as relevant. Compare pinned old/new definitions and their callers, readers, and writers, including removed aliases, stricter validation, and altered semantics.
2. Locate accepted support versions, deprecation commitments, rollout order, and migration/breaking-change policy. Expose missing or ambiguous policy as a decision/evidence gap rather than inventing perpetual compatibility or permission to break consumers.
3. Build the applicable old/new producer-consumer matrix: clients/servers, mixed-version services, readers/writers, stored data, and events. Cite policy-backed exclusions and reasons for nonapplicability. Trace defaults, unknown/required fields, enum expansion, and error handling where they affect supported combinations.
4. Walk upgrade and rollback paths through migrations, dual-read/write periods, replay, cache/file reuse, and data already written by the new version. Determine whether an old version can read new data and what reversal loses. Separate policy-allowed documented irreversibility from accidental breaks and unverified recovery claims.
5. Compare versioning, migration instructions, release notes, coordination, and rollout gates with declared policy. Evaluate any explicitly authorized isolated compatibility probes without extending execution authority; otherwise state source evidence and runtime gaps.
6. Return the shared report with old/new behavior, supported combinations, consumer impact, policy basis, and migration/rollback evidence for each affected contract. Use `PR-COMPAT-001`, etc. for new findings. Cite concrete violated combinations; identify unknown consumers/policy in Limits. Keep this scoped conclusion separate from hosted readiness.
