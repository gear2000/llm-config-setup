# Review PR operational safety

Review changed build, deploy, infrastructure, and operational paths read-only under the appended contracts. Do not trigger workflows/deployments or apply infrastructure.

Example: `/pr-devops repo=. pr=42 scope="build permissions and staged deployment recovery"`

## Procedure

1. Establish the shared snapshot and inventory affected build inputs, dependency/artifact provenance, packaging, workflow definitions, deployment manifests, infrastructure, and runtime configuration. Trace source-to-artifact-to-runtime wiring; explain operational categories outside the changed scope.
2. Inspect trust and permission changes across build, deployment, and runtime identities. Follow secret sources/injection and possible exposure through logs, errors, telemetry, caches, and artifacts. Compare required privileges with the actual affected actions without exposing secret values.
3. Inspect relevant resource bounds, health/readiness checks, monitoring, alerts, and diagnostic signals. Trace changed failure modes to their detection and response consumers; distinguish configured observability from evidence that it works.
4. Walk rollout order, gates, mixed-version/configuration periods, failure containment, rollback, and recovery prerequisites. Account for persisted state and data written by the new version; identify irreversible transitions and policy-backed recovery expectations.
5. Correlate source/configuration evidence with permitted existing build/deploy/check observations, retaining their revisions and times. Record unobserved operational claims and the smallest safe verification the owner would need; do not exercise live systems or treat read access as execution permission.
6. Return the shared report with affected delivery paths, permissions/data exposure, detection signals, rollout/recovery evidence, and limits. Use `PR-OPS-001`, etc. for new findings. Keep source-supported operational conclusions separate from current hosted readiness or a claim that deployment was tested.
