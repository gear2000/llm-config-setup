# Dest-contextual UpAgent specialists

Give this file to an LLM (`@SETUP-SPECIALISTS.md`) when adding **destination-owned** specialists to the UpAgent hub roster. Kit install stays in [UPINSTALL.md](UPINSTALL.md). Destination file map and `this_repo/` seed stay in [SETUP-DESTINATION.md](SETUP-DESTINATION.md). The dest-setup workflow opens this file after it notices how this repo uses a technology.

The kit already ships **generic** specialists (`clickhouse`, `kafka`, `backend`, …) in `.shared-llm/public/extensions/common/upagent/specialists.yaml`. A dest specialist exists only when **this repo's practice** is not in that generic body. Example: the kit `clickhouse` persona covers schemas and ops; a dest `clickhouse-mv-pipeline` persona covers *this* codebase using materialized views as a transformation pipeline.

Run kit commands from the kit checkout. Write dest files only after the user accepts the specialist list.

One kit specialist is not domain-shaped: `model-picker`. Every hiring agent (plan-implementer, `/upagent-run`, the pattern converters) consults it before a hire, and it answers from `.shared-llm/public/extensions/common/upagent/staffing-guide.yaml`: which offering and effort a kind of work gets, which seats need a human yes first (`requires_approval`), which are out of quota (`unavailable_until`), and whether the brief is small enough for the seat. Nothing enforces it; edit the guide to change the advice.

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

## Optional resident specialists

Residents keep one interactive specialist session available for repeated questions.
They are off by default. No daemon, cron entry, or Herdr upgrade is installed.

From the destination working directory, using the kit's justfile:

```bash
just --justfile /path/to/kit/justfile upagent-specialist-up "backend,kafka"
just --justfile /path/to/kit/justfile upagent-specialist-status
just --justfile /path/to/kit/justfile upagent-specialist-restart "backend"
just --justfile /path/to/kit/justfile upagent-specialist-down "backend,kafka"
```

Use names from `upagent-specialists`. Startup needs a running Herdr session and a
live `HERDR_PANE_ID`, or the existing UpAgent services pane. The selected offering
must use interactive Pi, Claude, ClaudeX, or Cursor. Codex exec is not resident-capable.

- The lifetime is fixed at **two hours**. There is no time flag.
- `up` is idempotent and does not reset a healthy instance's clock.
- `down` disables the specialist before cleanup. Later requests cannot revive it.
- `restart` verifies cleanup before creating a replacement.
- Status reports the registry plus live identity checks, including expired,
  missing, and uncertain instances. It does not rotate anything.

Public agent requests and legacy client request/dispatch commands refresh enabled
specialists before launching their worker. Phase, implementer, and pipeline launch
commands also check the invocation context. Failed refresh blocks the launch.
Direct Python calls that bypass the client are not covered. With no requests,
expired specialists stay running until the next check or an explicit lifecycle
command. There are no idle model calls.

Residents are scoped to the **same resolved working directory**, not merely the
same specialist name. A different worktree or `--cwd` does not borrow the session.
Use the same directory for `up` and consultations. Definition, offering, and
root `AGENTS.md`/`CLAUDE.md` changes trigger replacement. Other repository files
can change within two hours; specialists are instructed to check current sources
before answering. This is not a source cache, a sandbox, or a token-savings guarantee.

A replacement must acknowledge its generation after reading its context, and the
command must verify the named Herdr agent, pane, process birth, working directory,
and idle state. Readiness has a three-minute bound. Each resident answers one
question at a time under a cross-process lock; a background refresh (before a
worker launch or another request) skips a resident whose lock is currently held
instead of waiting for it, so one busy resident never stalls unrelated work — the
answering path still re-verifies identity and freshness itself, so a skipped
rotation never authorizes a stale answer. Waiting for a busy resident and
answering a question each have a ten-minute bound. Missing or uncertain delivery
is reported rather than retried blindly, and delivery that is known to have never
reached the resident restores it to ready without waiting for the full bound.
A turn that fails validation, or whose deadline passes while the pane is
verifiably idle, is recorded as a failed or abandoned turn and the resident
becomes reusable on the very next call — no operator `restart` is required.
Cleanup never interrupts an unresolved answer, and never closes or replaces a
pane whose ownership cannot be verified; a pane confirmed gone is recycled
safely, and a merely uncertain or still-working one stays blocked.

### Consultation behavior

`upagent-consult` and public `upagent request --type specialist` reuse enabled
residents. Disabled specialists retain the existing fresh-worker path.
**Resident public requests block until their answer is collected**, including
when `--wait` is omitted. They publish private answer/result/receipt artifacts;
`get`, `status`, and `await` can read the durable result. Repeating a request ID
reattaches instead of sending the question again. If the process that submitted
the question is confirmed gone and delivery is unresolved, repeating the request
ID reports blocked rather than either replaying the question or hanging forever.
Resident questions do not issue Recruiter control tokens and do not support
`cancel`, `respond`, `verify`, `await-any`, retained-worker review, or public
request cleanup.

Resident receipts describe a completed question, not a finished Recruiter job:
they never carry `order_receipt_state`, because no Recruiter order or worker ever
runs for a resident turn. **`upagent-consult`'s resident path does satisfy
`mandatory_consults`/`consults_verified`** — a successfully executed turn is
indexed under its requester with `resident_turn_state: finished` plus the exact
resident `generation`/`turn` that produced the validated answer, and the same
`resolve_consult_claims` reader that checks a finished Recruiter job accepts
that signal too. A pre-run rejection, an uncompleted turn, or a stale/mismatched
answer is never indexed, exactly like a cold consult rejected before any
specialist ran. `upagent-consult`'s resident path only refreshes the TARGET
specialist before answering; it never refreshes every other enabled resident in
the working directory first, so one unrelated wedged or slow-to-verify resident
cannot cost an otherwise-unrelated question its answer. The public `upagent
request --type specialist` path is unrelated to this: it never calls the consult
door, so it is never indexed and never satisfies `mandatory_consults` — keep
that distinction when inspecting a worker's consultation evidence. The
Recruiter supervisor and job lifecycle remain unchanged.

The registry and generation-specific turn files live beside the machine-local
UpAgent ledger under `specialists/`. Resident directories use mode 0700 and
controller-written JSON uses 0600; each delivered question is also written to
its own private 0600 file, and the resident is pointed at that file rather than
being sent the question text inline. Old generation evidence is retained. A
failed, timed-out, or interrupted `agent start` leaves its generation `starting`
with no pane. Herdr does not cancel a queued start, so that generation is never
replaced by a new one. The next `up`, `restart`, or refresh resumes it under the
same `warm-<generation>` name, which Herdr allows only one live agent to hold:
it adopts the named agent if that start did land (same harness, working
directory, and a live process), or starts it again. `down` never launches. If no
agent holds the name, `down` reports the specialist `starting` and disabled, and
a later `down` or `up` checks the name again. Only a genuine identity mismatch —
wrong working directory, wrong process, or a pane that is still active — fails
closed. Do not delete the
registry to bypass a fail-closed block, since that can lose ownership of a
still-running pane. Use status to identify the recorded session and pane before
manual investigation. No live-provider residency or two-hour soak test is
implied by the unit suite.
