# Stall-nudge live fire drill

Date: 2026-08-19

## Verdict

**DELIVERED-END-TO-END.** A real UpAgent worker became idle, a drill-only injected
`STALLED` closeout was consumed by the real Recruiter, the hub delivered the literal
`continue` through the worker's registered Herdr agent address, and the worker then
wrote its bundle. The request finished with `verdict: passed` and all three panes were
closed and verified absent.

- Public request id: `191d41e7-dadb-4821-9b96-32581dfd35ca`
- Order id: `public-191d41e7-dadb-4821-9b96-32581dfd35ca`
- Ledger key: `51a6cf2678066119a0bc757f829f1174b45f9c62cd2754bb87aced67acb82e56`
- Worker: `claude-fable-5`, effort `low`, persona `docs-writer`
- Worker cwd: `/tmp/upagent-stall-nudge-drill.9Zg40q` (a fresh `mktemp -d`, not the repository)
- Worker pane/address: `w22:pRS` / `upagent-191d41e7-dadb-4821-9b96-3258-51a6cf2678-g1`
- Sentinel pane: `w22:pRT`

## Commands

I first confirmed the approved offerings, then created an isolated cwd and prompt:

```bash
just upagent lists --type offerings --json

DRILL_CWD=$(mktemp -d /tmp/upagent-stall-nudge-drill.XXXXXX)
chmod 700 "$DRILL_CWD"
printf 'corroborated drill evidence\n' > "$DRILL_CWD/citation.txt"
cat > "$DRILL_CWD/prompt.md" <<'EOF'
This is a live stall-nudge delivery drill. Do not write any completion artifact and do not finish the assignment during your first turn. End your first turn idle and wait for a new agent prompt. Only when you receive the exact single-word prompt `continue`, write the result.json, compacted.md, and handoff.md files at the lease-private paths in your Recruiter instructions, with a passed verdict and a short note that `continue` arrived, then remain available as instructed. Do no other work.
EOF
```

The first submission attempt was refused before launch because the adopted service pane
in `services.json` no longer existed. No request was registered and no worker was
launched. Following the command's fail-loud remediation, I retried with a caller anchor
and an explicit fresh request id:

```bash
just upagent request \
  --request-id 191d41e7-dadb-4821-9b96-32581dfd35ca \
  --type worker \
  --offering claude-fable-5 \
  --effort low \
  --agent docs-writer \
  --prompt-file /tmp/upagent-stall-nudge-drill.9Zg40q/prompt.md \
  --cwd /tmp/upagent-stall-nudge-drill.9Zg40q \
  --duration-minutes 10 \
  --cockpit-pane "$HERDR_PANE_ID" \
  --json
```

The command returned `UPAGENT_REQUEST_ACCEPTED` with state `running`, worker pane
`w22:pRS`, and manager pane `w22:pRR`. I waited for
`sentinel/attempt-1/brief.md`, confirmed the worker had ended its first turn without
artifacts, and captured its pane with:

```bash
herdr pane read w22:pRS --source recent-unwrapped --lines 100
herdr agent get upagent-191d41e7-dadb-4821-9b96-3258-51a6cf2678-g1
```

The real health event reported `agent_status: idle`; a later Herdr read represented the
completed first turn as `done`. I then performed the one deliberate drill injection.
This JSON was written to the real Sentinel attempt's `closeout.json`; it was not emitted
by the Sentinel and is not evidence of organic stall classification:

```json
{
  "request_id": "191d41e7-dadb-4821-9b96-32581dfd35ca",
  "order_id": "public-191d41e7-dadb-4821-9b96-32581dfd35ca",
  "outcome": "STALLED",
  "interpretation": "DRILL-ONLY INJECTION: worker intentionally ended its first turn idle while awaiting the hub-owned literal continue nudge.",
  "citations": [
    "/tmp/upagent-stall-nudge-drill.9Zg40q/citation.txt"
  ],
  "bundle": null,
  "blocking_question": null,
  "exchanges": [],
  "progress_so_far": "Worker read its Recruiter instructions and intentionally entered the drill idle state; no completion artifacts were staged.",
  "last_alive": "Worker pane showed its explicit idle acknowledgement immediately before this injected closeout."
}
```

The injection time was `2026-08-19T11:25:18,033694621-04:00`. On the first one-second
observation after the write, both the delivery event and
`closeout.stalled-nudged-1.json` were present. I then waited for the normal result and
cleaned the terminal drill request:

```bash
just upagent await --request 191d41e7-dadb-4821-9b96-32581dfd35ca --json
just upagent cleanup --request 191d41e7-dadb-4821-9b96-32581dfd35ca --apply --json
```

Cleanup returned `status: cleaned` and retained the public tombstone.

## Exact ledger evidence

The hub first consumed and corroborated the injected closeout:

```json
{"at_ns":1787153118188710527,"blocking_question":null,"corroborated_citations":["/tmp/upagent-stall-nudge-drill.9Zg40q/citation.txt"],"event":"sentinel-closeout","exchanges":0,"first_action_recorded":true,"interpretation":"DRILL-ONLY INJECTION: worker intentionally ended its first turn idle while awaiting the hub-owned literal continue nudge.","outcome":"STALLED","uncorroborated_citations":[]}
```

It persisted intent before delivery, with matching digest
`62143654bdeb279dd8e57cb88ae15c7527c7ae6f29371dce9a55bfec8ad706df`:

```json
{"at_ns":1787153118209547945,"attempt":1,"digest":"62143654bdeb279dd8e57cb88ae15c7527c7ae6f29371dce9a55bfec8ad706df","event":"worker-nudge-intent","generation":1,"nudge_index":1,"worker_address":"upagent-191d41e7-dadb-4821-9b96-3258-51a6cf2678-g1"}
{"at_ns":1787153118244566726,"digest":"62143654bdeb279dd8e57cb88ae15c7527c7ae6f29371dce9a55bfec8ad706df","event":"worker-nudge-delivered"}
```

The persisted `nudges.json` record agreed:

```json
{
  "nudges": [
    {
      "at": 1787153118.2094145,
      "digest": "62143654bdeb279dd8e57cb88ae15c7527c7ae6f29371dce9a55bfec8ad706df",
      "delivered": true
    }
  ]
}
```

The Sentinel later consumed the valid-bundle wake and produced a terminal `COMPLETE`
closeout. The request's terminal event was:

```json
{"at_ns":1787153276459757112,"cleanup":{"herdr_session":"default","launches":[{"agent_name":"upagent-manager-191d41e7-dadb-4821-9b96-3258-51a6cf2678-g1","cleanup":{"herdr_session":"default","status":"closed","verified_absent":true,"worker_pane":"w22:pRR"},"launch_id":"manager-eb157f9a2c6e47168debcf715b4975fe","pane":"w22:pRR","role":"manager","state":"closed"},{"agent_name":"upagent-sentinel-191d41e7-dadb-4821-9b96-3258-51a6cf2678-g1","cleanup":{"herdr_session":"default","status":"closed","verified_absent":true,"worker_pane":"w22:pRT"},"launch_id":"sentinel-97861f36b5744b788fea287680225a1c","pane":"w22:pRT","role":"sentinel","state":"closed"},{"agent_name":"upagent-191d41e7-dadb-4821-9b96-3258-51a6cf2678-g1","cleanup":{"herdr_session":"default","startup_rejected":false,"startup_validated":true,"status":"closed","verified_absent":true,"worker_pane":"w22:pRS"},"launch_id":"worker-09889bc64f9f44eba33f872570a1d169","pane":"w22:pRS","role":"worker","state":"closed"}],"manager":{"herdr_session":"default","status":"closed","verified_absent":true,"worker_pane":"w22:pRR"},"sentinel":{"status":"not-created","verified_absent":true,"worker_pane":null},"startup_rejected":false,"startup_validated":true,"status":"closed","verified_absent":true,"worker_pane":"w22:pRS"},"completion_source":"result-or-agent-status","event":"finished","exit_code":0,"terminal_at_ns":1787153276457886865,"verdict":"passed"}
```

## Worker pane proof

The relevant unwrapped pane tail shows the first turn ending idle, the literal hub
prompt arriving as a new turn, and the worker immediately writing the lease-private
bundle:

```text
● This is a stall-nudge drill: I must not write any artifacts now.
  I'll end this turn idle and wait for the exact prompt continue
  before writing the result files.

  Idle — awaiting continue.

✻ Crunched for 8s

❯ continue

● Write(~/.local/state/herdr/upagent/446d1aa62bddfb96d01f/ledger/
       requests/51a6cf2678066119a0bc757f829f1174b45f9c62cd2754bb87a
       ced67acb82e56/artifacts/0888c8ccdb32459ea97d16f3450ca9ad/
       result.json)
  ⎿ Wrote 6 lines ...

● Write(.../artifacts/0888c8ccdb32459ea97d16f3450ca9ad/compacted.md)
  ⎿ Wrote 5 lines ...

● Write(.../artifacts/0888c8ccdb32459ea97d16f3450ca9ad/handoff.md)
  ⎿ Wrote 3 lines ...
```

The published worker result also states:

```json
{
  "full_log": "worker-session 0888c8ccdb32459ea97d16f3450ca9ad: stall-nudge delivery drill; first turn ended idle as instructed, exact single-word prompt 'continue' arrived, artifacts written on second turn.",
  "note": "continue arrived; drill completed as instructed",
  "order_id": "public-191d41e7-dadb-4821-9b96-32581dfd35ca",
  "revisit": [],
  "verdict": "passed"
}
```

## Deviations and limits

1. The first submission command was refused before launch because the recorded service
   pane was stale. Anchoring the retry to the live caller pane resolved it. This was a
   preflight deviation, not a nudge-ladder retry or failure.
2. `claude-haiku` was not an approved public worker offering on this machine. I used the
   cheapest listed Claude worker offering, `claude-fable-5` at low effort; the real
   Sentinel still used its shipped Haiku management command.
3. The `STALLED` closeout was intentionally injected, exactly as this drill specifies.
   This proves closeout consumption, fencing, durable intent, real Herdr prompt delivery,
   and worker resumption; it does not claim that the Sentinel independently diagnosed
   this artificial idle state.
4. The initial pane displayed Claude's draft/autosuggest text `continue` before the
   injection. It was not submitted and produced no worker turn or nudge ledger event.
   The proof above is the later submitted `❯ continue` turn followed by artifact writes,
   paired with the exact intent/delivered events.
