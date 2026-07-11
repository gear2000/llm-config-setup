# Shared meta-runner handoff protocol

Shared handoff contract for Meta-CC, Meta-ORCH, and Meta-Herdr. Deliberately generic and public. Baked into the workflow — it does not depend on any installed handoff skill or slash command.

Every role/stage agent is spawned fresh, does one job, then **writes a short handoff before it returns** so its replacement resumes with immediate context instead of a cold start. Fresh context avoids drift; the handoff carries only the distilled signal forward.

## Where

Canonical per-role path, **versioned** (never overwritten):

```text
.meta/handoffs/<phase-id>/<role>-vN.md
```

On spawn, an agent reads the latest `<role>-v*` for its role — or the full trail when it needs to see how the work evolved.

## What (keep it to ~10 lines)

- Role · phase · iteration.
- What I did — 1–3 lines.
- Key decisions + why — 1–3 lines.
- Alignment to the original plan — on-track, or the exact deviation and why.
- Open items / risks for the next agent.
- Pointers to artifacts (`plan.md`, changed files) — pointers, not re-summaries.

It is immediate context, not a report. Summarize your **actions and decisions**; **point** to the plan and code rather than paraphrasing them (a paraphrase drifts from the source).

## When

- Every stage/role agent writes its handoff as the last step before returning a result or `BLOCKED`.
- The phase Lead Agent reads the relevant handoffs before creating the next stage agent.
