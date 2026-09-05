# Dest-contextual UpAgent specialists

Give this file to an LLM (`@SETUP-SPECIALISTS.md`) when adding **destination-owned** specialists to the UpAgent hub roster. Kit install stays in [UPINSTALL.md](UPINSTALL.md). Destination file map and `this_repo/` seed stay in [SETUP-DESTINATION.md](SETUP-DESTINATION.md). The dest-setup workflow opens this file after it notices how this repo uses a technology.

The kit already ships **generic** specialists (`clickhouse`, `kafka`, `backend`, …) in `.shared-llm/public/extensions/common/upagent/specialists.yaml`. A dest specialist exists only when **this repo's practice** is not in that generic body. Example: the kit `clickhouse` persona covers schemas and ops; a dest `clickhouse-mv-pipeline` persona covers *this* codebase using materialized views as a transformation pipeline.

Run kit commands from the kit checkout. Write dest files only after the user accepts the specialist list.

---

## When to use

- Dest setup noticed ClickHouse, RisingWave, Kafka, Go, Terraform, or another stack, **and** a this-repo convention (MV pipelines, naming, folder layout, gotchas)
- An existing dest needs a consultable specialist for future agents
- The UpAgent hub in this dest must list that specialist (`just upagent-specialists`)

Not this file: adding a public kit specialist everyone gets (that is a kit change). Changing offering ids (that is [prompts/UPDATE_UPAGENT_OFFERINGS.md](prompts/UPDATE_UPAGENT_OFFERINGS.md)).

---

## LLM workflow

### 1 — Detect (do not write yet)

Done when each finding names the technology, the this-repo practice, and whether a kit specialist already covers the generic half.

Walk first-party code. Skip `.git`, `node_modules`, vendored trees, build output, `.shared-llm`.

Look for **how** the repo uses the stack, not only that the name appears:

| Signal | Kit specialist (reuse) | Dest specialist only if |
|--------|------------------------|-------------------------|
| ClickHouse SQL, servers, dictionaries | `clickhouse` | Materialized views as a pipeline, repo-specific MV naming, insert chains |
| Kafka topics, consumers | `kafka` | This repo's topic taxonomy, exactly-once conventions |
| Postgres / migrations | `database` | A this-repo migration ritual the generic persona lacks |
| Terraform | `terraform` | Module layout or apply gates unique here |
| Go modules | none in kit base | Idioms, `internal/` layout, or generate steps unique here |
| RisingWave | none in kit base | This repo's RisingWave jobs, checkpoints, sinks |
| Next.js / frontend | `frontend` | App-router or auth conventions unique here |

Read the kit roster at `.shared-llm/public/extensions/common/upagent/specialists.yaml` before proposing a new name. Reuse the kit entry when the generic persona is enough.

### 2 — Propose and ask

For each dest specialist, one short block:

- **Name** — dest-owned, hyphenated, not a kit name (`clickhouse-mv-pipeline`, not `clickhouse`)
- **Why kit X is not enough** — the this-repo practice
- **What future agents should consult it for**

Ask: **I noticed [practice] in this codebase. The kit already has [generic specialist] / has none. Do you want me to analyze that area more deeply and add a dest-contextual specialist the UpAgent hub can consult?**

Do not analyze deeply or write files until yes. Skip any finding they decline.

Done when the accepted specialist names are a concrete list (possibly empty).

### 3 — Analyze the accepted areas

Done when the persona body can name real paths, invariants, and gotchas in this repo.

Read the modules behind the practice. Record:

- Where the pipeline / package lives
- The public seam (who inserts, who reads, what must stay ordered)
- Naming and layering rules a future agent would violate
- What to hand off to a kit specialist (`clickhouse` for cluster ops, this dest specialist for the MV graph)

Do not refactor the dest. This pass writes a consultable persona, not a redesign.

### 4 — Write source (never generated outputs)

For each accepted specialist, add dest-owned source under `<dest>/.shared-llm/this_repo/`:

1. **Persona layer** — `layers/agents/this_repo/<name>.md`. This-repo facts only. Narrow interface: when to consult, what files matter, invariants, gotchas, when to hand off to a kit specialist.
2. **Description** — `layers/agents/this_repo/<name>.description.md`. Routing pointer: domain + trigger + boundary. Stay well under 1,024 UTF-16 code units (warn yourself above 300).
3. **Compose recipe** — `.shared-llm/this_repo/compose/agents/<name>.yaml`:

   ```yaml
   type: agent
   name: clickhouse-mv-pipeline
   model: sonnet
   description: .shared-llm/this_repo/layers/agents/this_repo/clickhouse-mv-pipeline.description.md
   inputs:
     - .shared-llm/this_repo/layers/agents/this_repo/clickhouse-mv-pipeline.md
     - .shared-llm/public/layers/agents/common/_report-contract.md
   output: .claude/agents/clickhouse-mv-pipeline.md
   ```

   Include the kit `_report-contract.md`. Pull a kit practices layer only when it still applies (e.g. clickhouse practices plus the dest MV body).

4. **Roster overlay** — create or edit `.shared-llm/this_repo/extensions/common/upagent/specialists.yaml` from the kit template `specialists.yml.sample`. Merge-by-name over the kit base. New entries only; do not copy the whole kit roster.

   ```yaml
   specialists:
     - name: clickhouse-mv-pipeline
       location: .claude/agents/clickhouse-mv-pipeline.md
       description: "This repo's ClickHouse MV pipelines: insert chains, naming, and consult gates."
       offering: claude-sonnet-5
       effort: medium
       agent: clickhouse-mv-pipeline
   ```

   `offering` and `effort` must exist on the dest's approved roster (`standard` unless they replaced it). `agent` matches the compose `name`. `location` is dest-root-relative.

Never hand-edit `.claude/agents/<name>.md`. Never put dest practice into a kit `common/` layer.

### 5 — Generate and check the phone book

From the kit checkout:

```bash
just descriptions
just update
just update
```

From the **destination** (so the overlay loads):

```bash
just upagent-specialists
```

Done when the new name is listed, `.claude/agents/<name>.md` exists as composed output, and the second `just update` is a no-op.

Future agents consult it with `just upagent-consult` (see the UpAgent README). A worker that must ask this specialist before changing that area lists it under `artifact_publication.mandatory_consults`.
