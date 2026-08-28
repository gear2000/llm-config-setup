# Kafka Practices

Use these rules for Kafka code, configuration, design, and diagnosis.

## Discovery first

- Identify Kafka version, cluster mode, broker topology, client libraries, and managed-service boundaries.
- Treat KRaft as the current default; diagnose legacy ZooKeeper clusters explicitly and plan migrations carefully.
- Prefer upstream Kafka documentation for changing details.

## Topics and capacity

- Design topics, partitions, replication, keys, and ordering from throughput, retention, consumer parallelism, and recovery needs.
- Account for retention, compaction, quotas, TLS/SASL, ACLs, and broker/client limits.

## Producers and consumers

- Set producer durability, idempotence, transactions, retries, batching, compression, and backpressure deliberately.
- Diagnose consumer groups, offsets, rebalances, lag, poison records, dead-letter handling, and delivery semantics with evidence.
- Plan schema evolution and compatibility; use the registry/format conventions the repository already has.

## Ecosystem and safety

- Cover Kafka Connect connectors, offsets, transforms, and failure recovery when data movement is involved.
- Cover Kafka Streams state stores, changelogs, repartitioning, and exactly-once settings when stream processing is involved.
- Validate code/config locally where possible and require human approval before live broker changes, destructive operations, migrations, sharing changes, or permission changes.
