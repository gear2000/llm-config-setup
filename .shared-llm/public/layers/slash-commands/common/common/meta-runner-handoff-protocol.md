# Shared meta-runner handoff protocol

Shared handoff contract for the meta runner. Deliberately generic and public. Baked into the workflow — it does not depend on any installed handoff skill or slash command.

Every worker (a stage/role agent) is hired fresh, does one job, then **writes a short handoff before its pane closes** so the next worker of that role — or the phase leader — resumes with immediate context instead of a cold start. Fresh context avoids drift; the handoff carries only the distilled signal forward.

## Where

Canonical per-role path inside the run tree, **versioned** (never overwritten):

```text
phases/<phase-id>/handoffs/<role>-vN.md
```

On spawn, a worker reads the latest `<role>-v*` for its role — or the full trail when it needs to see how the work evolved across passes.

## What (keep it to ~10 lines)

- Role · phase · pass.
- What I did — 1–3 lines.
- Key decisions + why — 1–3 lines.
- Alignment to the original plan — on-track, or the exact deviation and why.
- Open items / risks for the next worker.
- Pointers to artifacts (`plan.md`, changed files, this stage's `result.json`) — pointers, not re-summaries.

It is immediate context, not a report. Summarize your **actions and decisions**; **point** to the plan and code rather than paraphrasing them (a paraphrase drifts from the source).

## When

- Every worker writes its handoff as its last step, before the Recruiter closes its pane — alongside its `result.json` and `compacted.md`, whether the outcome is a pass, a fail, or `BLOCKED`.
- The phase leader reads the relevant handoffs before ordering the next stage's worker.
