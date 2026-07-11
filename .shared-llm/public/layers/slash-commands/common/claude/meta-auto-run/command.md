# /meta-auto-run

The single kickoff workflow the Meta-ORCH brain sends to a freshly spawned Claude Code TUI session. The phase data travels as files on disk; the hub is used only for registration and the done ping.

`/meta-auto-run` is a thin conductor. It connects to the hub, invokes `/run_phase`, and pings the brain after the result file exists. The result file is the source of truth; the ping is only an accelerator.

## Invocation

```text
/meta-auto-run --plan <plan.md> --phase <instructions.md> --route <route.yaml> --output <results.md> --hub <loc>
```

Required:

- `--plan <plan.md>` — canonical meta plan.
- `--phase <instructions.md>` — instructions file for this attempt.
- `--route <route.yaml>` — resolved route profile with `llm_profiles` and inline `agent` names.
- `--output <results.md>` — phase result file to write.
- `--hub <loc>` — hub discovery JSON path or direct URL.

All five required flags must be present. Fail loud on any missing value.

## Execution — run these three steps in order

### Step 1 — Register on the hub

Run `/hub-connect <hub>` with the `--hub` value. If connection or registration fails, stop loud; there is no point doing phase work for a brain you cannot reach.

### Step 2 — Check runnable inputs, then run the phase

First run the same gate as `/meta-plan-check <plan.md> <route.yaml>`. If the check fails, stop before phase work; do not auto-convert inside `/meta-auto-run`.

Then run:

```text
/run_phase --plan <plan.md> --phase <instructions.md> --route <route.yaml> --output <results.md>
```

`/run_phase` reads the canonical plan, instructions, and route profile; acts as the phase Lead Agent; runs the shared five-stage worktree protocol; and writes `results.md` with a leading `PHASE_RESULT:` line.

### Step 3 — Ping the brain

Run:

```text
/response --hub <hub> --output <results.md> [--name <worker-name>] [--msg <msg_id>]
```

Always send the ping when possible, but never treat a ping failure as a phase failure if `results.md` was written correctly. The brain can detect completion from the file.

## Hard rules

1. All five required flags are mandatory.
2. Run `/hub-connect` → `/run_phase` → `/response` in order.
3. Thread `--route` through to `/run_phase`; routing never belongs in the plan body.
4. Check `plan.md + route.yaml` before invoking `/run_phase`; invalid inputs fail before semi-AFK work.
5. `results.md` is mandatory and is the source of truth.
6. The synchronized meta path uses phase Lead Agent → non-delegating stage agents. Do not use Claude team mode.
7. This command does not implement phase logic itself.
