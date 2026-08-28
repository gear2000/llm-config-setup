# ClickHouse Practices

Use these rules for ClickHouse design, code/configuration edits, and operational diagnosis.

## Discovery first

- Identify the ClickHouse version, edition, topology, and deployment method before giving version-specific advice.
- Prefer upstream ClickHouse documentation for changing details; cite it when behavior depends on a release.
- Separate local repository changes from live cluster actions.

## Schema and storage design

- Design MergeTree-family tables from query patterns, retention, ingestion rate, and mutation needs.
- Choose ordering keys for filtering and locality; keep primary-key prefixes aligned with the most selective common predicates.
- Use partitioning for lifecycle management, not as a high-cardinality index substitute.
- Consider codecs, TTLs, projections/materialized views, and aggregate states only when they match measured access patterns.

## Ingestion and evolution

- Batch inserts; avoid tiny continuous writes unless the architecture explicitly handles them.
- Plan deduplication, idempotency, and schema evolution before changing producer code.
- Treat migrations, mutations, and backfills as operationally risky; estimate volume and merge impact.

## Query and operations

- Diagnose with `EXPLAIN`, query logs, system tables, memory settings, merge/mutation state, and part counts.
- Review distributed tables, replication, Keeper, backup, restore, and disaster recovery as topology-specific concerns.
- Validate changes locally or against a non-production target when possible.
- Require human approval before live changes, destructive operations, migrations, sharing changes, or permission changes.
