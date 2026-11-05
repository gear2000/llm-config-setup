## Operational-safety contract

Applies to changed in-scope build, deployment, infrastructure, or runtime-operation behavior. Record reasons for unaffected categories; this is not an obligation to exercise every operational system.

- Build inputs, dependency/artifact provenance, packaging, and deployment wiring produce the intended artifact and configuration through the affected path.
- Infrastructure, workflow, and runtime permissions remain appropriately bounded; secret injection, logging, and artifact retention do not expose sensitive data or broaden trust unintentionally.
- Relevant resource limits, health/readiness signals, monitoring, alerts, and diagnostic output support detection and handling of the changed failure modes. Claimed observability is tied to actual signals and consumers.
- Rollout gates, ordering, failure containment, and recovery prerequisites match the affected deployment contract. Evidence distinguishes configured intent from observed build/deploy/recovery results.
- This is a read-only operational assessment, not permission to trigger a build/deployment/job, apply infrastructure, rotate credentials, or exercise production recovery. Execution authority and isolation remain governed by the shared contract.
