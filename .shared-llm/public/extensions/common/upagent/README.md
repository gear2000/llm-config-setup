# UpAgent — the Recruiter

UpAgent coordinates durable LLM worker lifecycles for a phase leader, TUI, or another framework.
The Python **Recruiter** persists each request, launches and verifies the worker, validates its
result, closes owned panes, publishes a receipt, and releases the lease. See
[FUNDAMENTALS.md](FUNDAMENTALS.md) for the authority model and use-case tree.

The hub is tested against **Herdr 0.7.1**. A newer binary breaks wait/subscription behavior.
Pin it from the kit checkout with `just herdr-pin` (`tools/install-herdr.sh`). Do not run
`herdr update` or the unpinned `https://herdr.dev/install.sh` installer. Machine setup:
[UPINSTALL.md](../../../../../UPINSTALL.md).

## Three execution flows

The human talks to one **HIL agent** — a Claude Code pane they started themselves inside Herdr
(`HERDR_ENV=1`). Automation never creates that pane. `just run-start` stays Flow 3 only.

```text
human  (Claude Code remote app)
  └── HIL  (Claude Code, human-started, already in a Herdr pane)
        │
        ├── Flow 1   /hil → plan-implementer controller → Recruiter → workers
        ├── Flow 2   /upagent-run and /upagent-pipeline (HIL hires workers itself)
        └── Flow 3   /tui-control → phase-leader → Recruiter → workers
                     (phased meta-run via just run-start; used least)
```

- **Flow 1** takes an approved `plan.md`. No `route.yaml`. `/hil` is a relay; the
  plan-implementer is a gated controller like a phase leader. Default is delegate / review /
  stop-and-ask. After `/cc-plan`, this is the default execution path.
- **Flow 2** is a single UpAgent hire (or a named pipeline) from the HIL pane. Unchanged.
- **Flow 3** is the checked `plan.md` + `route.yaml` path. `just run-start` launches
  `/tui-control` (TUI agent / phased HIL); that controller creates one `/phase-leader` per
  phase.

```text
human
  └── HIL  (/hil)                    Claude Code · relay only
        └── plan-implementer         pi/claude/codex/cursor · smart
              └── Recruiter
                    ├── slice workers
                    ├── reviewers / adversaries
                    └── specialists (consults)
```

The implementer lands in the `control` tab beside the already-running HIL pane; hired workers
still move to `workers`.

## Per-command execution

Every recipe invokes `client.py`. Before importing any UpAgent runtime module or classifying the
command, a linked-worktree client resolves the source checkout folder from
`$UPAGENT_CANONICAL_REPO` when set, otherwise from exactly one checked-out `main` worktree. Ambiguity
fails loudly. That process imports current source from that checkout folder, uses repository-scoped
machine-local state, runs one command, and exits. There is no resident Python Hub, Unix command
socket, protocol handshake, module cache, or restart step; a re-sync is visible to the next
post-cutover command by construction. The hard cutover cannot retrofit an arbitrarily old
pre-cutover binary that never implemented re-exec from the source checkout folder; such legacy
processes must be retired explicitly.

The distributed lock is one machine-local `flock` shared by commands and detached supervisors.
Individual token-fenced ledger writes/CAS methods hold it only while committing durable
transitions; reconciliation never wraps its process or Herdr work in an outer lock, and the lock is
never held during Herdr waits or a worker lifetime. Status, get, and list are pure reads and remain
lock-free. `await` and `await-any` do not hold the distributed lock while waiting or polling; if an
await sweep proves the exact recorded runner process died and must repair shared request state, it
acquires the distributed lock briefly for that fenced ledger mutation only. Fire-and-forget
requests start a standalone `recruiter.py run-job` supervisor with `start_new_session=True`; its PID
and ownership are written into the durable lease. Concurrent duplicate submissions may briefly
start competing children; the child that loses the atomic claim exits, while its caller attaches to
the proven live winner instead of reporting a false startup failure. Blocking dispatch polls its
child and immediately attaches, reconciles a dead claim, or terminalizes a child that exits without
ownership. Each mutating lifecycle command checks for orphaned active claims before its requested
operation. Reconciliation uses a bounded launch/claim fixpoint so killing an in-flight owner and
closing its newly-dead launch happen in one invocation. If a crash occurs after the winning active
lease is published but before `runner.json`, the terminal receipt retains that lease's exact
PID/birth pair; crash reconciliation reconstructs only from that immutable identity before
publishing `runner-completed.json`. A losing claimant can never overwrite it. Pure status/get/list
reads remain lock-free. `$UPAGENT_RUNTIME_DIR`, `$UPAGENT_HUB_DIR`, and `$UPAGENT_STATE` may
override the shared runtime, ledger, and service-state paths.

`up` and `down` are thin, idempotent presentation verbs. `up` ensures the services pane and writes
its state file; it starts no daemon. `down` terminalizes owned active work, closes only a verified
owned services pane, and removes the state. Public requests recreate missing service state on
demand, so `up` is a convenience rather than a prerequisite.

Every worker, manager, checker, and rescue helper persists a token-fenced
`launching` journal before `herdr agent start`, records the exact returned pane as `created`, then
compare-and-swaps it to `started`. Cancellation rotates the lease fence, then waits without holding
`flock` while any live owner remains in `launching`; verified absence is forbidden until that owner
commits the pane identity or dies. A lost fence routes through bounded exact-agent cleanup. If
cleanup cannot be proved immediately, the detached supervisor publishes no terminal receipt. The
next command reconciles the active lease, journaled unique agent name, and exact pane, and only then
terminalizes with that cleanup evidence. A crash between pane creation and ledger
publication therefore cannot leave an unowned agent or a false `verified_absent` receipt.

## Topology

```
Herdr session (default: single workspace)
└── ws: upagent                services AND runs share one workspace as role tabs
    ├── tab: services          └── upagent (optional deterministic status surface)
    └── tabs: control / workers / oversight   per-run panes

Herdr session (with `up --separate-workspaces`)
├── ws: <slug>                 TUI + leader + workers (+ opt-in managers / one-shot checkers)
└── ws: shared-services        optional status surface, plan-agnostic
    └── upagent                 optional deterministic status surface
```

The visible `upagent` pane is the Recruiter's status surface, not a free-form command queue. On
first bring-up after this naming change, UpAgent renames its former `herdr` workspace and
`recruiter` pane in place; unrelated human-created workspaces are never claimed. Requests go through
one variadic per-command façade:

```text
just upagent --help
just upagent up
just upagent status [--request ID] [--json]
just upagent get --request ID [--json]
just upagent lists --type offerings|plan-implementers|specialists|workers [--status active|terminal|all] [--json]
just upagent request --type worker --offering ID --effort LEVEL --agent PERSONA \
  --prompt-file /absolute/brief.md [--cwd /absolute/worktree] \
  [--duration-minutes 1..120] [--keep-open] [--no-sentinel] [--cockpit-pane LIVE_PANE] \
  [--wait] [--json]
just upagent request --type specialist --specialist NAME \
  --prompt-file /absolute/question.md [--cwd /absolute/worktree] \
  [--cockpit-pane LIVE_PANE] [--wait] [--json]
just upagent request --file /absolute/request.json [--cockpit-pane LIVE_PANE] [--wait] [--json]
just upagent await --request ID [--notify-after-ms MS] [--json]
just upagent await-any --request ID [--request ID ...] [--cursor JSON] [--timeout-ms MS] [--json]
just upagent verify --request ID --offering ID --effort LEVEL --agent PERSONA [--wait] [--json]
just upagent respond --request ID --control-token TOKEN --nonce NONCE \
  --action extend|cancel --extension-ms MS [--json]
just upagent review-await --request ID [--after N] [--timeout-ms MS] [--json]
just upagent review-continue --request ID --checkpoint N --checkpoint-sha256 SHA256 \
  --prompt-file /absolute/feedback.md --control-token-file /absolute/private-token [--json]
just upagent review-release --request ID --checkpoint N --checkpoint-sha256 SHA256 \
  --control-token-file /absolute/private-token [--json]
just upagent cancel --request ID --control-token-file /absolute/private-token [--json]
just upagent cleanup (--request ID | --all-terminal) \
  [--older-than-seconds N] [--apply] [--json]
just upagent reconcile [--json]
```

## Strict public request boundary

Named flags and `--file` enter one closed `schema_version: 1` parser. A file must be one readable
absolute JSON object and may contain only `schema_version`, `request_id`, `type`, `offering`,
`effort`, `agent`, `specialist`, `prompt_file`, `cwd`, `duration_minutes`, and `keep_open`.
Duration must be an integer from 1 through 120; omission uses the 60-minute public default.
`keep_open` must be boolean, applies only to workers, and maps to the release-gated retained
lifecycle. `--file` is mutually exclusive with request-defining flags; `--cockpit-pane`, `--wait`,
and `--json` are invocation controls and never enter the immutable payload. Worker requests
require explicit offering, effort, persona, and prompt. Specialist
requests require a specialist and prompt and resolve that specialist's pinned offering. Unknown
keys, types, offerings, efforts, personas, specialists, relative/unreadable paths, and incompatible
flags fail before the ledger, manager, pane, or worker. There is no intake LLM, prose repair,
arbitrary argument materialization, or empty-request acceptance on this path.

Offering policy is resolved from the repository where the UpAgent request starts. A `--cwd` target
chooses where the worker runs; it does not opt the request into or out of ClaudeX by pointing at
another registered destination.

`--cockpit-pane` is an invocation-only override for anchoring the order to the caller's own pane.
It may accompany named flags or `--file`, but it is not a request-defining flag, does not enter the
closed request object or immutable payload hash, and is not an allowed key inside a request file.
Before Recruiter acceptance, a supplied value must be a non-empty pane ID that a fresh pane listing
proves live in the current Herdr session; omission resolves the current service pane on demand. A
same-ID/same-payload retry of a refused or interrupted pre-acceptance submission refreshes only this
routing field and keeps every immutable request field unchanged. Once
the Recruiter has accepted the exact order, placement is frozen: active, terminal, and pruned
attachments preserve the accepted pane and do not require any current caller/service pane to exist.
On `cockpit_pane_not_found`, ad-hoc callers run `just upagent up` and retry the same request ID
(optionally with `--cockpit-pane "$HERDR_PANE_ID"`); relaunched phase leaders use
`just leader-restamp`.

A caller may supply one canonical lowercase hyphenated UUID or uppercase Crockford ULID; otherwise
Python generates a UUID. UpAgent hashes the canonical immutable payload, including the resolved
offering snapshot and SHA-256 of the exact prompt bytes. It atomically snapshots those bytes before
submission. The public bridge durably transitions `registered` → `submitting` → `submitted`
under a per-request lock. An identical retry resumes a registration interrupted before submission;
a retry interrupted after Recruiter acceptance resubmits the same order and reattaches through the
Recruiter's idempotent ledger. Same id plus same hash attaches without another launch; same id plus
a changed hash returns `request_id_conflict` before request mutation. This remains true after
history pruning: the compact tombstone retains the immutable payload hash, so an identical retry
attaches and a changed payload still conflicts without launching.

### Retained review loop

`--keep-open` is an opt-in controller workflow; the default remains one-shot. It is incompatible with request `--wait`: retained work must submit asynchronously so the originating response can return the private control token before the first checkpoint decision. The same initial lease and requested duration cover coding plus review. After its first pass the worker writes a lease-private numbered checkpoint and returns to idle without writing terminal artifacts. `review-await` blocks until the next checkpoint without consuming an LLM polling loop. The owning requester inspects the real diff and tests, preserves the checkpoint SHA-256 returned by review status/await, then authenticates with the original private control-token file and that digest to either send `review-continue` feedback into the same idle Herdr session or write `review-release` and request final artifacts. Feedback names the next checkpoint sequence. Only a lease-fenced release allows the completion monitor to accept terminal artifacts and clean up the pane; premature terminal bundles are quarantined. Worker exit before release fails loud as blocked. Existing timeout extension and cancellation remain valid while reviewing. Public `/upagent-run` callers use `just upagent review-await|review-continue|review-release`; phase leaders operating on their own strict stage order use `just upagent-review-await`, `just upagent-review-continue`, and `just upagent-review-release`.

### Read, cancel, and terminal cleanup

`status` without a request describes the per-command runtime; `get --request ID` is the read-only request view. It
returns submission and lifecycle state, retained result and receipt values, typed
result/compacted/handoff/receipt/log pointers, and — when nudge events exist — a compact
nudge summary (attempt count, delivered/failed/held counts, escalation state, and last
event time). Requests with no nudge history show no summary. After pruning it reports `state: pruned`, the prior
terminal state/verdict/timestamp, and which runtime-owned pointers were pruned. It never reconstructs,
republishes, or mutates an artifact. Mutation credentials are redacted from `status`, `get`,
`await`, listing, cancellation output, and tombstones; the requester control token appears only in
the originating asynchronous `request` response after healthy startup, whose caller must store it
privately if later cancellation is needed. A same-hash attachment never receives that capability.

Any active request can be cancelled with its existing requester control token stored in an
absolute, same-user, non-symlink regular file with no group/world access:
`cancel --request ID --control-token-file /private/token`. Unlike `respond ... cancel`, this
command is not tied to a timeout decision nonce and never places the token value in process
arguments. Under the request fence it rotates the lease token, serializes against
pane creation, closes only exact journaled and identity-verified worker/manager/checker launches,
and publishes the ordinary schema-valid `blocked` bundle with `cancelled: true` receipt evidence.
It introduces no new verdict value. A wrong or stale control token fails without mutation. If terminal publication
wins the race, cancellation authenticates and returns that already-published result idempotently.

`cleanup` prunes history; it never cancels or terminates runtime. It is a dry-run unless `--apply`
is explicit. A request is eligible only after successful terminal `finished` state, a terminal
receipt whose `cleanup.verified_absent` is true, a matching durable `runner-completed.json` written
after the supervisor's final requester notification, no active lease, no unresolved launch, and a
fresh read-only proof that every recorded pane remains absent. Active,
`awaiting-requester`, malformed, and `cleanup-failed` requests are refused for `--request` and
reported as skipped by `--all-terminal`. `--older-than-seconds N` uses the authoritative terminal
receipt/state timestamp and includes equality (`age >= N`).

Apply commits two individually atomic tombstones in recoverable order—private Recruiter request
first, then the runtime-owned public snapshot—and prunes only each tombstone's runtime-owned siblings.
An interruption between stores is completed idempotently by the next cleanup. It does not follow or
delete caller paths: the original caller prompt/run tree and
any artifact outside those two runtime directories remain untouched. The tombstone retains request id,
immutable payload hash, terminal verdict/timestamp, compact receipt/result values, typed pointer
status, requester-control proof, and cleanup timestamp. That is enough for `get`, listing,
authenticated terminal cancellation, audit, identical reattachment, and changed-hash conflict
after the disposable prompt/order/staging/event/launch history is gone. Repeating cleanup is a
no-op that also retries removal of a previously swapped runtime-owned residual.

The engine assembles `offerings.yaml` from code-approved names under `offerings.d/`. Omitted machine configuration selects only `standard`, which contains the existing seventeen stable ids: four Claude, four Codex, two Cursor, and seven Pi. Selecting `[standard, claudex]` adds exactly `claudex-gpt-5-6-sol`; a destination `[standard]` replacement removes it. Management policy stays in `offerings-management.yaml`, so set selection cannot change specialist defaults, lifecycle commands, or management candidates.

The same parsed object drives text/JSON listing, request validation, specialist/lifecycle references, and the immutable order snapshot. YAML contains declarations only. Code pins every approved set member, harness, model, provider, effort list, completion style, health identity, executable, command renderer, and preflight. Unknown sets, partial sets, duplicate ids, changed fields, commands in YAML, and management references to absent offerings fail loudly. The runtime roster resolves from the request's starting repository, then `$UPAGENT_CANONICAL_REPO` for the same git repo, then the main checkout for a linked worktree, then the generated home roster.

`offerings.py` renders child tokens: Claude uses `--effort`, Codex uses `-c model_reasoning_effort=...`, and Pi uses a provider-qualified `--model` plus explicit `--thinking`. Cursor has no effort control: its only allowed selection is the canonical `default` effort. ClaudeX uses the `claudex` executable with the exact `gpt-5.6-sol` model and remains interactive; because the wrapper uses `exec`, Herdr health still requires the final `claude` process. Before any ClaudeX worker pane is created, UpAgent requires both `claudex` and `claudex-doctor`, runs `claudex-doctor gpt-5.6-sol`, and blocks on a missing executable, proxy/OAuth failure, or absent model. It never substitutes native Claude. Legacy and controller recipes use the same per-command dispatcher; their strict order files bypass no lifecycle validation, but ClaudeX is accepted only through public offering snapshots.

A request's manager, worker, and short-lived checkers start beside `order.cockpit_pane` through
atomic `herdr agent start` calls. Pane placement remains role-based and every pane is closed only by
its fenced lease owner.

Flow 1 implementer startup has its own deterministic front door. The HIL never splits a pane
or types `/plan-implementer` itself:

```text
just upagent-implementer-start <plan.md> <offering> <effort> <run-root>
just upagent-implementer-await <run-root>/control/implementer-start.json
```

`--offering` / `--effort` are required (fail loud; no silent default). The controller starts
the implementer behind a gate, writes `implementer-start.json`, and health-checks it. It
returns `IMPLEMENTER_STARTED` with `ready`. The receipt records the implementer pane
before the gate releases, then Python health-checks and writes `ready`. A failed start
writes `state: failed`; retry uses a new run-root. The implementer is placed in the
`control` tab. The HIL then blocks in `upagent-implementer-await`
which returns one typed event per call (`completed`, `blocked`, `failed`, `needs-input`,
`invalid-result`, `leader-missing`, `leader-stalled`, `inactivity-checkpoint`,
`await-heartbeat`, …). On
`needs-input`, quote the question to the human, then
`just upagent-implementer-respond <receipt> <question-id> <answer-file>`. The implementer
blocks in `just upagent-implementer-await-answer` until that answer file exists
(`timeout_ms=0`). After a valid `implementer-result.json`,
`just upagent-implementer-finish <receipt>` closes only the recorded implementer pane.
On `cancelled` / `hard-timeout` without a result file, pass `--force`. Claude Code's
HIL await uses `timeout_ms=590000` under a 600 s shell-tool cap and re-enters the
same wait if the tool is killed. Feature-branch checkouts prefix every `just upagent*`
call with `$UPAGENT_CANONICAL_REPO`; `start.sh` copies that env into the implementer pane. Durable files are
truth; pane text is display-only.

The plan-implementer hires workers with `just upagent request` (public façade), not
Recruiter `order.json`.

Phase startup (Flow 3) has its own deterministic front door:

```text
just upagent-phase-start <frozen-route.yaml> <run-tree> <phase-id> <pass-number>
```

It starts the leader behind a gate, releases it once the durable `phase-start.json` receipt
exists, and health-checks it. It returns `PHASE_STARTED` with `ready`; the receipt's `watchdog`
block reads `not-configured` by design — coordination v2 has **no standing watchdog**. The owner
blocks in `just upagent-phase-await <receipt>` which returns one typed event per call (`completed`,
`blocked`, `needs-input`, `leader-missing`, `leader-stalled`, `inactivity-checkpoint`,
`await-heartbeat`, …). Urgent unacknowledged events escalate to the human via `herdr notification`.
Leader startup failures still close the gated leader.

The controller exports its receipt path to the released leader. The Recruiter inspects that
receipt and records a `phase-receipt-degraded` event when it is missing or stale (a
`not-configured` watchdog block is by design, never degraded), but accepts the stage order. A
missing receipt means the phase kickoff (`just upagent-phase-start`) never ran for that pass;
the warning says so plainly, is announced once per phase pass instead of once per order, and is
stored in the durable request ledger and modern startup responses.

Run-level ownership uses a private token only for mutating lifecycle operations. New starts write
the token to a per-run hashed 0600 file under `$RUNNER_TOKEN_DIR` or the default same-user
0700 runtime token directory, then pass only `RUNNER_OWNER_TOKEN_FILE` to the TUI and heartbeat
process. The runner accepts only that absolute, same-user private regular-file path and never needs the raw
`RUNNER_OWNER_TOKEN` in a cross-process command protocol; the raw variable remains only a non-protocol
compatibility fallback. Recovery is explicit: use `just run-session-snapshot <run-dir>`,
then `just run-session-reconcile <run-dir>`, and only then start with stale takeover when
the reconciliation receipt proves the recorded owner is stale.

### Flow 1 implementer lifecycle

`just upagent-implementer-start <plan> <offering> <effort> <run-root> [--no-supervise]`
verifies the implementer process, cwd, rendered model and effort, and agent identity.
It closes the advisory Account Manager after output or timeout, starts a provider-disjoint
Sentinel, then starts run-watch when enabled. A manager concern or unavailable supervision
returns `ready-degraded` with a literal `startup_advisory`. The HIL prints it and awaits events.
`--no-supervise` skips all three helpers and restores the previous await behavior.

Supervised awaits share the nudge authority with run-watch. Two idle/done probes send
`continue` and `SENTINEL_STALL_NUDGED`; only exhaustion publishes `leader-stalled`.
Exec-style implementers never receive a status nudge. Sentinel launches, closeouts, wake
messages and the cursor across awaits live under `control/sentinel/`.

Start and finish validate a 480-second budget; the HIL gives both recipes a 600-second
shell timeout. `just upagent-implementer-finish <receipt> [--force]` records progress under
`.implementer-finish.lock` in `control/finishing.json`, fences further implementer nudges,
closes owned helpers, drains run-watch, and closes the implementer. It verifies pane,
agent, PID, process birth and argv before cleanup. A retry resumes the cursor. Forced
finish without a valid result records `verdict: cancelled`. The HIL prints the final
`control/implementer-finish.json` cleanup entries and any `flow1:cleanup-failed:*`
advisories. Only those advisories may follow a terminal journal event.

### HIL human inbox

`/hil` requires a literal `--offering` and validates it against the
plan-implementer listing. Before starting, it prints
`offering=<id> harness=<harness> effort=<effort> supervise=<bool>`.
It awaits events in 60-second windows. After every return, it atomically writes
new unsolicited human messages to `<run-root>/inbox/msg-<seq>.json` with
`{seq, text, at_ns, acked: false}`. This directory is separate from
`control/inbox/`, which carries events for the HIL.

While any envelope remains unacknowledged, the HIL calls:

```bash
just upagent-implementer-inject <receipt>
```

The JSON response contains the shared authority's `outcome`, `reason`, and
`episode`, plus `envelope_seqs` listing all unacknowledged messages. A working
pane returns `not-idle`; a later idle call sends the fixed `read your inbox`
prompt once for the pending batch. Duplicate calls do not repeat a recorded
delivery. Human text never enters a pane command. Inbox delivery shares the
identity, terminal-result, finishing, and idle checks used by run-watch, and
never spends the `continue` cap. Exec-style implementers receive no prompt.

The implementer reads envelopes at slice boundaries and on that fixed prompt,
acts on each message, and quotes it with its sequence and action in
`implementer-status.md`. It then atomically sets `acked: true` and retains the
file. Delivery is not acknowledgement. A crash after sending but before recording
delivery can repeat the fixed prompt; consumers check acknowledgements and the
recorded action before repeating work.

To roll back Phase 2, revert the inbox relay additions in the HIL and
plan-implementer command layers, then run `just update` from the main checkout.
Keep the Phase 3 registry writer in `plan-implementer/hire.md` and the Phase 1
degraded-start and finish handling. Local tests compose both layer variants and
exercise injection through the real recipe with fake Herdr. Live phone delivery
within two await cycles still requires acceptance on a supervised live run.

### Standalone Flow 1 run-watch

After each accepted public request, the implementer redacts its control token and runs:

```bash
just upagent-register-worker <run-root> <redacted-request.json>
just upagent-run-watch <run-root> [--roster <roster.yaml>]
```

The first command atomically records the response's request id, payload hash, order id,
generation, and placement time under `control/workers/`. Identical attachments are
idempotent. A changed generation fails instead of replacing the record. The run root
comes from the implementer's invocation; two runs sharing a cwd have separate registries.

Run-watch reads only these requests from the canonical ledger. It checks their identities
and generations against the current worker launch before each send. It also supervises
the implementer identified by `control/implementer-start.json`. Validated terminal evidence
wins over pane absence. Interactive idle/done panes use the shared nudge authority with
trigger `run-watch`; working panes close idle episodes, and exec panes receive no prompt.
Unknown probes hold. Gone and blocked panes produce hourly advisories in the HIL journal.
Run-watch does not request a Sentinel recheck or write task verdicts.

After the nudge cap, one provider-disjoint Checker reads one saved evidence snapshot.
Only one Checker is active per run; other exhausted targets wait for a later sweep.
A busy authority lock defers that target instead of blocking Checker lease cleanup.
Its typed assessment becomes an advisory. Run-watch closes and verifies its pane as soon
as output or process exit is observed, or after a four-minute wait with one minute reserved
for cleanup. Two consecutive empty assessments also produce an hourly provider advisory.

Both authored `offerings-management.yaml` and legacy `upagent.yaml` accept this root block:

```yaml
run_watch:
  enabled: true
  interval_minutes: 5
  drain_minutes: 5
```

Omitting the block uses those defaults. Set `enabled: false` to roll back standalone
supervision. Unknown keys, non-boolean `enabled`, and non-positive or non-integer minute
values fail before a pane is touched. The process polls every five seconds and records its
PID, process birth time, argv, Herdr session, effective policy, last sweep, advisories, and
Checker ownership in `control/run-watch.json`. A process lock permits one owner per run.
`--once` performs one poll/sweep, closes any assessment it started, and records `stopped`.

SIGTERM or `control/implementer-finish.json` starts a drain. Registered live workers remain
supervised until none remain or the drain deadline passes. The final state is `finished`
or `drain-timeout`, with remaining request ids. A missing or failed start receipt records
`orphaned`. Phase 1's lifecycle controller will own automatic startup and finish-budget
validation; this command can already be started by hand.

## The order → result contract (`contracts.py`)

Durable files are the source of truth; terminal text is display-only.

- The requester writes `order.json`, including a globally scoped `request_id` and
  `requester: {id, kind, address}`, then runs `just upagent-request <order.json>`. It returns after
  Python verifies worker startup, with the worker address and a per-generation control token.
  A healthy Account Manager address is included when available; manager startup or assessment
  failure is reported as degraded supervision and never prevents mechanically valid work from
  reaching `running`.
- The Recruiter validates and persists a copy-on-write request under
  `$UPAGENT_HUB_DIR` (default repository-scoped machine-local state shared by the main checkout and
  its worktrees). One runner atomically claims
  `active/requests/<scoped-request-id>/`, writes an authoritative generation lease, attempts the
  advisory manager, then launches the requested harness. Worker health means the expected
  foreground process, detected harness, and cwd—not merely pane creation. Manager health and its
  typed assessment are reported separately and may degrade without vetoing worker startup.
- Before launch, Python checks absolute paths, required model/effort values, harness-native model
  shape, executable presence, ClaudeX proxy/OAuth/model readiness, and (for Claude `--agent` routes) the actual persona file. Those
  facts are given to the manager. A bad request is explained to the requester and terminalized
  without ever creating a worker, even if the LLM mistakenly recommends approval.
- Public requests use one dedicated advisory Account Manager selected from the ordered approved
  candidates in `offerings.yaml`: `cursor-composer-2-5` at `default`, then `pi-gpt-5-4-mini` at
  `low`, all with the `upagent-account-manager` brief. The Recruiter removes
  same-provider candidates, tries the first eligible candidate, and records each startup failure
  before trying the next. Exhaustion degrades supervision only: it cannot veto Python-valid
  startup, mutate a lease, publish artifacts, invent success, or terminalize the request.
- Every accepted order carries `artifact_publication`. Compatibility/controller orders that omit it
  receive deterministic result-adjacent paths and an explicit mandatory-consult list before the
  first ledger mutation. The Recruiter writes a closed-schema typed manifest
  and appends literal lease-private paths for `result.json`, `compacted.md`, and `handoff.md`;
  specialist workers additionally receive `answer.json`. Workers never receive a public answer
  destination. Only `result.json` — and `answer.json` for a specialist — is mandatory: the result
  carries `passed|failed|blocked`, `revisit`, and `full_log`, and specialist answers must pass
  `contracts_consult`, including consult identity, success/error shape, and real `file:line`
  citations. `compacted.md` and `handoff.md` are best-effort summaries with no schema; an absent or
  blank one is skipped at publication rather than failing an otherwise finished job, because a later
  reader can rebuild both from the result.
- `just upagent-await <order.json>` waits in Python for a decision point or completion; no LLM
  loops over files. At inactivity checkpoints, a fresh cheap checker interprets one bounded pane
  and process snapshot, reports to the manager/requester, and exits.
- At a work cap, `upagent-await` returns `REQUESTER_DECISION_REQUIRED`. The requester may run
  `just upagent-respond <order> <control-token> <nonce> extend <milliseconds>` or `... cancel 0`.
  Without an answer during `management.requester_grace_ms`, the Recruiter performs the declared hard
  stop. Managers/checkers can recommend actions but cannot execute them.
- The deterministic completion reactor validates the staged bundle and permits exactly one repair
  prompt to the same interactive worker address—never a second worker—when a legitimately completed
  wait reaches it with missing or malformed mandatory artifacts. A completely absent bundle does
  not turn an interactive harness's turn-level `done` signal into completion; while the process
  remains live, supervision continues until the bundle appears, the pane or expected process is
  positively confirmed gone, or the request reaches its deadline. If repair or deterministic
  recovery still cannot validate the bundle, Python writes a schema-valid blocked
  result/compacted/handoff bundle and, for specialists, a valid failure answer. A missing optional
  summary never triggers a repair and never blocks.
- Four mechanical reliability gates run inside this lifecycle. A startup marker records the
  worker's first observable action in the ledger once Python proves health (agent activity, a
  staged artifact, or changed pane output). The liftoff deadline is the smaller of 5 minutes
  and half the order's own work cap, so on a valid short order (1-minute caps are allowed)
  the never-started classification always fires before the hard request timeout can claim
  the request — the hard timeout stays the ultimate backstop. The typed `never-started`
  terminal is deadline-proven and side-effect-free: it requires the recorded
  worker-never-started deadline event — attempt-scoped, so a prior attempt's proof never
  authorizes a later attempt's mint — AND no staged artifact file of any kind (result.json,
  compacted.md, handoff.md alike), no landed commits, no recorded first action, and a clean
  worktree (uncommitted paths — attributable or not — and staged artifacts alike route the
  miss to the ordinary blocked path with the epilogue evidence instead). It is a
  distinct synthesized verdict — never worker vocabulary, like `salvaged-done` — and the
  Recruiter auto-retries that outcome exactly once with a fresh worker before it
  surfaces (awaiting callers still see the blocked bucket; the receipt keeps the typed
  verdict). Every Python-authored blocked bundle carries a harness epilogue — landed commits,
  files touched, and the staged artifact files the worker actually wrote — so a worker that
  did work but skipped its bundle can no longer produce an empty result. And before any
  `passed` publishes, the bundle validator parses the worker's non-empty artifact files: an
  explicitly empty `findings` beside a non-empty report, or a report whose last non-blank
  line states `VERDICT: VEERED` (markdown emphasis, trailing punctuation, and case are
  normalized away), invalidates the verdict and forces the one same-worker re-evaluation
  instead of a silent accept; an unrepaired inconsistent result blocks and is never salvaged
  as "a valid staged result".
- On top of those gates, every ordinary request is Sentinel-supervised by default
  (`--no-sentinel` opts out; watchdogs and retained review workers never get one). The
  Recruiter hires one provider-disjoint Herdr pane per worker attempt, duty-bound to that worker:
  LIFTOFF corroborates the startup marker and, once a first real tool action is proven, the
  requester receives the worker's live pane address; PULSE is event-driven on the
  attempt's wake file (beside its closeout.json — Python is the only writer, touching it
  on worker staging activity, valid or partial, and on proven worker death; the Sentinel
  is the only consumer, blocking on it with a bounded wake-wait run under an explicit
  command timeout that outlives the wait, then reading the wake REASON from the file's
  content before consuming it — a `valid-bundle` wake means write the COMPLETE closeout
  immediately, never re-sleep), with a 5-minute interval as the fallback timer, reading
  the pane tail and
  git/fs deltas each wake and nudging once before declaring a stall; LANDING steers
  finalization by dialogue under a hard never-believe-the-worker rule — only bundle files
  verified on disk count — with at most 3 exchanges. The Sentinel's one typed
  `closeout.json` (outcome `COMPLETE | NEVER_STARTED | STALLED | FINALIZATION_FAILED`, plus
  interpretation, citations, bundle, blocking_question, exchanges) is THE teardown trigger
  while the Sentinel is live: when the artifact monitor validates a staged bundle under a
  live Sentinel, the Recruiter does not suppress that signal silently — it publishes
  the wake file (the ONE wake channel: an atomic temp-write-and-rename, claimed by the
  Sentinel with an atomic rename and republished while the window is open, so a write
  racing a claim is never lost) so the
  Sentinel closes out now and holds one bounded landing window of at
  least a full pulse block, shared by the interactive and exec waits alike; a
  closeout inside the window keeps closeout-as-trigger, and a lapsed window — or the
  hard deadline clipping it, which is checked before the generic timeout — ends the
  wait on the validated bundle with the typed lapse recorded. A positively dead
  worker pane/process — or the mechanical never-started deadline, whose LIFTOFF the live
  Sentinel owns (its brief carries the same clamped deadline the watcher enforces) —
  first opens a bounded closeout window — a few minutes — for the Sentinel to land its
  closeout before the mechanical path takes over; when a valid staged bundle already
  exists at proven worker exit, that window is skipped (bypassed-at-exit) because the
  Sentinel has nothing left to add and the bundle is Python-validated either way.
  Supervision is re-checked throughout the wait, never snapshotted at wait entry: the
  Sentinel's own pane is probed on the same cadence as the worker's, and a
  confirmed-gone Sentinel degrades supervision back to the mechanical paths for the rest
  of the wait, so a dead Sentinel can never strand a finished worker until the hard
  timeout. Python re-verifies every citation
  before it counts (an absolute-path citation corroborates only when it exists INSIDE the
  request's own territory — the worktree/cwd subtree or the ledger directory; an
  existing-but-out-of-scope path like `/etc/passwd` is discarded as out-of-scope) and a
  COMPLETE closeout ends the wait only into the ordinary bundle validation — an invalid
  COMPLETE is rejected with exactly one more landing round — so a fooled Sentinel may end
  a request early but can never cause a false `passed`. A blocked terminal's published
  reason lists Python-verified citations separately as checked fact; the Sentinel's
  interpretation and progress prose always travel but, being LLM-authored and never
  mechanically checkable, always carry their explicit `(uncorroborated)` marker — a
  verified citation never launders the prose around it — and a closeout's
  `blocking_question` is surfaced first-class on the
  published result.json, the receipt, and any Python-composed retry brief. A STALLED
  closeout whose citations ALL failed corroboration does not terminalize a provably live
  worker on the Sentinel's word: Python re-probes the worker pane once and, only on a
  POSITIVE pane answer (probe uncertainty is never treated as liveness), rejects the
  closeout back to the Sentinel for one re-check before a repeat claim
  is accepted. Python also probes interactive workers about every 20 seconds. Two
  consecutive `idle` or `done` probes without a validated bundle trigger the literal
  `continue`, even when no Sentinel closeout exists or the Sentinel could not start.
  `working` closes the idle episode; `blocked` and uncertain probes hold. Exec-style,
  retained keep-open, and watchdog workers receive no automatic continue.

  Both status and Sentinel triggers use `nudge_authority.py`. One per-target lock
  protects `<JobLedger.root>/nudge/<target-id>.json` and its intent-before-delivery
  records. The target hashes the Herdr session, workspace, pane, exact agent name,
  owner kind, and request identity. Each idle episode has a cap of three attempts,
  with the first immediate, the second after five minutes, and the third after another
  fifteen minutes. Failed or aborted sends spend a rung. Both triggers share that cap.
  The authority rechecks identity, pane status, ledger permission, and validated
  terminal evidence immediately before submitting through the existing agent prompt
  path. Valid terminal evidence wins even when the pane has vanished. A recovered
  `working` worker supersedes a stale STALLED closeout.

  A `nudge` journal event records `trigger`, `episode`, and `outcome`; the existing
  `worker-nudge-*` events still support the public summary. Delivery sends
  `SENTINEL_STALL_NUDGED` to an available Sentinel so it resumes its finalization duty.
  Exhaustion publishes `worker-stall-escalation` once per episode and marks that
  episode escalated after publication. A crash before the flag may repeat the notice.
  Invalid state fails loudly; requester decisions, cancellation, and finalization
  forbid delivery. The target lock serializes nudges, while lifecycle mutations retain
  their own lock and the bounded final probe-to-submit race.

  `management.status_first` defaults to `true`. Set it to `false` in the authored
  `offerings-management.yaml` and regenerate `offerings.yaml` to disable only status
  delivery for public requests. Legacy orders read the same switch in `upagent.yaml`.
  Omission means true; strings, numbers, null, and other non-booleans fail validation.
  `management-start` journals the effective value. Sentinel-triggered recovery and
  bundle finalization still run with the switch off.

  Cross-provider supervision is mandatory on every hire,
  including retries. Public worker offerings pin code-owned provider metadata in
  their immutable snapshots; legacy orders fall back to known harness/model identity
  only when no snapshot provider exists. For the public roster, the Recruiter filters the
  YAML-ordered Sentinel candidates (`cursor-composer-2-5`/default, then `pi-gpt-5-4-mini`/low)
  by provider and tries every eligible startup in order. Each failure is
  recorded; only exhaustion degrades to mechanical supervision through the existing
  `sentinel-degraded` path with a typed `reason_type`. An explicit legacy
  `management.sentinel` command remains an override: its command
  identity must prove a provider distinct from this worker or it degrades fail-closed.
  Both resolved providers are recorded on the durable `sentinel-hired` requester
  event. No environment flag is required. The Sentinel
  never kills anything and never outlives its worker attempt. When the hire fails —
  including a missing `upagent-sentinel` persona for the claude sentinel command (the pi sentinel carries no persona flag and runs on its self-contained brief, so the pre-check does not apply to it), which is diagnosed before any pane is
  created with the exact missing paths named in the degrade event (checked once per
  invocation), and a pane creation refused by a herdr error or limit — supervision
  degrades for that request and the
  mechanical paths stay fully in charge (a never-hired Sentinel cannot strand a finished
  worker); the requester is notified once per distinct degrade reason, while the ledger
  records every attempt's degrade. When a live Sentinel dies and no closeout ever
  appears mid-window, the hard timeout,
  salvage triage, and epilogue backstop fire unchanged. Every supervision state change
  is a distinct typed ledger event — `sentinel-hired`, `sentinel-degraded`,
  `sentinel-dead`, `sentinel-wake-valid-bundle`, `sentinel-wake-partial-staging`,
  `sentinel-wake-worker-gone`, `sentinel-wake-never-started` (the truthful liftoff
  reason: the worker may still be live but idle),
  `sentinel-window-lapsed` (with its `window`: `landing` or `closeout`),
  `sentinel-bypassed-at-exit`, `sentinel-closeout` (consumed), `sentinel-stalled-rejected`
  — so an operator can reconstruct a request's supervision from the ledger alone.
  Requester→worker messages go through
  `just upagent-message <order.json> <control-token-file> <message-file>`, which logs each
  message to the durable ledger before delivery.
- Publication is ordered: validate private staging, prepare and atomically replace every public
  artifact, revalidate the public bundle, write `receipt.json`, then append the durable terminal
  event/state and requester notification. `upagent-await` wakes only from that post-receipt
  evidence; there is no pre-publication `result-ready` notification. If anything goes wrong it
  fails loud without a terminal receipt. Publication also keeps
  the ledger's own `published-result.json` and names it in the receipt, so a terminal record survives
  the pruning of the run tree that owns `result_path`: a later dispatch republishes that copy
  instead of failing in the strict result loader, and refuses with the evidence paths when no copy
  survives. The lease records the
  requester, manager, detached supervisor, Recruiter, worker, workspace, token, generation, and
  expiry. Opportunistic per-command reconciliation safely drains dead/expired owners. Crash
  recovery uses the
  manifest's typed staging paths and replaces an unvalidated or incomplete bundle with one
  deterministic blocked bundle. A request's immutable `request.json` and events are durable; its
  `state/latest.json` is the copy-on-write current view. The lease is authoritative; retained
  `active/by-expiry` entries are merely reaping indexes and must be token-checked before reuse.

`route.yaml` is authoritative for which harness/model/agent runs each legacy/controller worker.
For those explicitly non-public paths, the Recruiter holds mechanical launch templates, separate
phase-controller templates, and configurable management-role commands in `upagent.yaml`; it never
silently substitutes a requested worker. Public requests never use those raw lifecycle commands:
they load `offerings.yaml`, and `offerings.py` renders the approved Account Manager command.

A direct Codex worker uses this launcher shape; it is not routed through Pi:

```text
codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
  --model {model} -c model_reasoning_effort={effort} \
  "Read {instructions_path} ... write result.json to {result_path}."
```

## Consulting a specialist (`specialists.yaml`) — who can be asked

Asking a specialist is not a second mechanism. A consult is an ordinary UpAgent order placed by
an ordinary door, so it uses the same ledger, the same lease, the same verified startup and the
same published result as any stage worker. Two commands:

```text
just upagent-specialists              # the phone book, paste-ready for a stage brief
just upagent-consult <consult.json>   # ask one question; BLOCKS until the answer is terminal
```

The caller writes `consult.json` — `{consult_id, specialist, question, answer_path}`, plus
optional `cwd` and `requested_by` — and the door does the rest: it resolves the specialist
against the merged roster, briefs a fresh worker, dispatches it in-process, and validates what
comes back. Beside the `consult.json` it leaves `.brief.md`, `.order.json`,
`.upagent-result.json` and `.receipt.json`.

Two files cross the boundary and they answer different questions. `result.json` is the ordinary
lifecycle receipt: the specialist worker ran and delivered. `answer.json` is the consult's
product, and `contracts_consult.parse_answer` is the only mechanical check anywhere in the repo
that an answer carries real `file:line` citations rather than confident prose. An answer is
ALWAYS projected by Python — either a validated cited answer or a Python-authored contract-valid
failure answer — before the consult receipt/event. Public `answer_path` never exposes the
lease-private staging path, so a caller's bounded wait resolves to a legible outcome instead of a
missing or worker-published file. Consult orders carry a canonical payload SHA-256: the same
consult/request id with identical payload attaches, while a changed question or other payload
conflicts instead of reusing stale work.

Orders may declare `artifact_publication.mandatory_consults` as a list of
`{consult_id, specialist}` requirements. An otherwise-passing result is changed to a blocked
bundle unless every requirement resolves to a matching Recruiter-indexed receipt whose answer verdict is
`cited`. Missing, rejected, failed, borrowed, or forged claims fail the gate. Reading source files
directly is not consultation evidence.

**Who can be asked is separate from the offering catalogue.** `specialists.yaml` is keyed by
persona and MERGES: the kit base is
`.shared-llm/public/extensions/common/upagent/specialists.yaml`; a destination may add the
repo-owned overlay `.shared-llm/this_repo/extensions/common/upagent/specialists.yaml` (template:
`specialists.yml.sample`). Every specialist pins `offering` plus `effort`; both must resolve
against the approved offering roster. A destination that overrides one specialist keeps every other kit entry.
The roster is loaded only for specialist requests and specialist listing: an invalid specialist
overlay cannot block an ordinary worker request.

Kit personas are generic (`clickhouse`, `kafka`, `backend`, …). A dest overlay is for **this
repo's practice** the generic body does not cover (for example ClickHouse materialized views used
as a transformation pipeline). Destination setup asks before adding those; the agent prompt is
the kit root [SETUP-SPECIALISTS.md](../../../../../SETUP-SPECIALISTS.md). After `just update`,
`just upagent-specialists` run from the destination lists kit names plus the overlay.

## Public offerings and legacy controller roster

Public workers and management candidates never read a YAML launch command. `offerings.yaml`
selects approved harness/model identities and effort allowlists, plus the candidate order;
`offerings.py` validates every reference and renders the exact child argv. Public Account Manager,
Checker, and Sentinel candidates are `cursor-composer-2-5`/default first, then
`pi-gpt-5-4-mini`/low. The Recruiter filters the worker's
provider and falls back on startup
failure. A legacy `upagent.yaml` remains only for explicitly route-driven controller compatibility,
where existing phase routes still provide raw harness/model profiles and may configure singular
raw lifecycle-role commands; the public candidate-list schema is rejected there. Four launch
properties remain load-bearing:

### Changing models and management defaults

Edit the kit source, not a generated hub or destination copy:

- `offerings.d/standard.yaml` and `offerings.d/claudex.yaml` are the human-edited offering-set
  fragments. `offerings-management.yaml` sets the ordered Account Manager, Checker, and Sentinel
  candidates. `offerings.yaml` is generated by `just update`.
- `offerings.py` owns the matching `APPROVED` and `APPROVED_SETS` allowlists plus the command
  renderer. Adding, removing, or renaming an offering requires the fragment entry and these
  allowlists to change together; YAML cannot supply executable commands.
- `offerings_test.py` covers exact roster membership, provider metadata, effort policy, candidate
  order, and rendered command tokens. Recruiter tests cover provider filtering and startup fallback.
- For Cursor, run `cursor-agent models` and copy the exact model id, including its embedded effort
  tier. Cursor offerings use `efforts: [default]` because the model id already selects that tier.

After a change, run the focused offering and Recruiter tests, run `just update` twice, confirm the
second update reports zero changes, then run `just test`. The update copies this source into the
shared hub and every registered destination.

1. **Unattended.** Workers start without operator input — every template bypasses
   trust/permission prompts (`claude --dangerously-skip-permissions`,
   `codex exec --dangerously-bypass-approvals-and-sandbox`, `pi --approve`,
   `cursor-agent --force --trust`) or the hire hangs until the Recruiter's timeout. Harnesses
   that support an interactive TUI keep it visible in the Herdr pane.
2. **Harness-native model ids.** Claude takes its model plus `--effort`; Codex takes a bare model
   plus `-c model_reasoning_effort=...`; Pi takes a provider-qualified model plus a separate
   `--thinking` token. Codex and Pi have no `--agent` flag, so their persona comes from the lease
   instructions.
3. **Harness-native completion.** `codex exec` exits when its turn ends and its Herdr agent
   disappears, so its offering declares `completion_style: exec`: the Recruiter's monitor waits
   for a valid staged bundle or confirmed process/pane exit, then uses mechanical salvage-or-block
   when the bundle is missing or invalid. Claude, Pi, and Cursor are interactive: Herdr `done`
   means only that one LLM turn ended, so the Recruiter ignores it for terminality and waits for a
   valid typed bundle, a positively absent pane/process, or the request deadline. Their live TUIs
   remain addressable; once that legitimate boundary is reached, a missing or malformed bundle gets
   exactly one same-worker `COMPLETION_REPAIR` prompt, followed by a bounded wait for the repaired
   bundle before cleanup.
   Cursor's launch prompt repeats the final
   delivery verification gate, and its follow-ups use separate text and Enter
   actions after a short settle; one atomic paste+Enter leaves the TUI input visibly drafted but
   does not submit it.
4. **pi runs insulated.** `--no-extensions` plus an explicit
   `-e $HOME/.pi/agent/extensions/herdr-agent-state.ts`. Discovery off means a broken
   globally-installed extension can never brick automation; the explicit `-e` keeps Herdr's
   pi integration loaded, which reports live working/idle/blocked status for supervision.
   Turn-level `done` is UI telemetry, not completion authority; durable typed artifacts remain
   authoritative. Workers are still full visible TUIs in panes (headless `-p` is never used);
   interactive pi sessions keep the whole extension set.

The `phase_leaders:` map is deliberately separate from `harnesses:`. A phase leader is launched
once with a controller assignment and held behind the phase-start gate; a stage worker receives a
lease-private result contract. `upagent-phase-start` fails before creating a pane when the selected
harness has no phase-leader template.

`plan_implementers:` is the same kind of map for Flow 1. `upagent-implementer-start` fails
before creating a pane when the selected offering's harness has no plan-implementer template.
ClaudeX has none — it is a worker offering, not a controller.

## Pipelines (`pipelines.yaml`) — what shape of work to run

A pipeline is one named work shape: an ordered stage list, which of those stages are optional,
where the human review gate sits, and how many phases the implement stage may grow to.
`pipelines.yaml` holds the registry and `pipelines.py` validates it the same way `offerings.py`
validates the offering roster — strict keys, no silent defaults, and every failure names the file
and the field. Nothing in this engine interprets a pipeline; the stages are read by the
`/upagent-pipeline` skill inside the launched pane, so a typo in a stage name or a review gate has
to be an error here rather than a pane quietly running a different shape. Pipeline ids, stage ids,
and gate values all come from closed sets owned by `pipelines.py` (`SUPPORTED_PIPELINES`,
`SUPPORTED_STAGES`, `SUPPORTED_SKIP_GATES`): `reserach` is rejected at load rather than becoming a
stage that never runs, and `rpii` is rejected rather than listing and launching a pane the skill
has no route section for.

Adding a pipeline is therefore a **code change, not a registry edit** — three things move together:
an entry in `pipelines.yaml`, a `## Pipeline: <id>` route section in the `/upagent-pipeline` skill
layer, and an id in `SUPPORTED_PIPELINES`. `pipelines_test.py` asserts that the shipped registry's
ids equal `SUPPORTED_PIPELINES` exactly and that the skill's route sections match them one for one,
so the registry and the skill cannot drift apart.

No pipeline is ever gateless. When a pipeline's `review_gate` names a stage that `optional_stages`
allows a flag to skip — `rpi` gates on `plan`, and `--skip-plan` skips it — the pipeline must
declare a `skip_gate`, and skipping the stage MOVES human approval rather than removing it.
`rpi`'s `skip_gate: issue-approval` means the human approves the issue and the stated approach
before implementation. A missing `skip_gate` on a skippable gate is a load failure, and so is a
`skip_gate` on a pipeline whose gate can never be skipped.

```
just upagent-list-pipelines           # id, stages, description (`?` marks an optional stage)
just upagent-list-pipelines --json    # the validated record: gates, skip gate, max_phases
just upagent-pipeline rpi docs/issues/dry-run-flag.md
just upagent-pipeline rpi --skip-research https://github.com/org/repo/issues/42
```

`<issue-location>` is a LOCATION — a file path or a tracker reference the pipeline can fetch
verbatim — never the issue text itself. The skill stops loudly on anything it cannot resolve, so
`just upagent-pipeline rpi add a --dry-run flag` is a stop, not a shortcut.

`just upagent-pipeline` puts an interactive Claude TUI in the unified `upagent` workspace's
`control` tab — the same placement `just run-start` uses — preloaded with
`/upagent-pipeline <name> <args>`. The name is resolved against the registry BEFORE any pane is
created, so a typo costs a message rather than an orphaned pane. A Herdr server that is not
running fails loud with the command to start it, and a startup that never becomes healthy closes
what the launch created (its own pane in an adopted workspace, the whole workspace only when the
launch created that too).

The recipe passes `{{args}}` through the calling shell, exactly like every other `*args` recipe in
this kit: arguments are word-split there, so a location containing a space arrives as two
arguments and `$(...)` is expanded before Python ever sees it. Keep issue locations space-free at
the command line, or start the session and give the location to `/upagent-pipeline` inside it.
`pipeline_prompt` re-quotes with `shlex.join` and rejects control characters, but that is
defense-in-depth on what survives the recipe shell — it is not the outermost gate.

Unlike `specialists.yaml`, the pipeline registry is kit base only — there is deliberately no repo
overlay. A repo has to be able to name its own specialists; a pipeline carries `review_gate`,
which IS the human approval step, so a same-named repo entry could drop a gate rather than merely
shorten a phone book. Adding an overlay later is additive; removing one is not.

## Adopt it

1. Copy this whole directory to the same relative path (public tool modules land under a
   destination's `.shared-llm/public/extensions/common/upagent/` via `just update`).
2. Let `just update` generate `offerings.yaml` for the public façade from the configured
   offering sets. Copy `upagent.yaml.example` → the repo-owned `this_repo` path only when legacy
   route/controller profiles still need it.
3. Add `import '.shared-llm/public/extensions/common/upagent/justfile'` to the root justfile.

### Platform support

Linux and macOS. Liveness fencing needs exact process birth identity and argv:
Linux reads `/proc/<pid>/stat` and `/proc/<pid>/cmdline`; macOS uses sysctl
syscalls (`KERN_PROC_PID` for the microsecond birth timestamp, `KERN_PROCARGS2`
for exact argv — no `ps` subprocess). The shared implementation lives in
`process_identity.py` (intentionally duplicated in `common/herdr/herdr_transport.py`
for the runner stack). On any other platform the entrypoints fail loud instead of
letting liveness checks silently fail open.

## Tests

`just test` covers the closed public schema, zero-launch rejection, prompt hashing/snapshotting,
UUID/ULID idempotency and conflict behavior, the exact standard text/JSON roster, optional ClaudeX
union, exact Claude / ClaudeX / Codex / Cursor / Pi child tokens, Cursor default-effort canonicalization and interactive repair,
role-aware launch-state transitions, Codex exec-style no-live-repair completion, specialist
offering resolution, request mailboxes, identity/lease fencing,
startup health, timeout authority, typed manifests, every missing/malformed artifact, one bounded
same-worker repair, manager degradation, specialist projection, mandatory-consult enforcement,
publication fault ordering, and cleanup. Focused per-command tests also cover mutation exclusion, lock-free pure reads, fresh module imports,
detached-supervisor launch failure, duplicate attachment to one live runner, main/worktree shared
state, thin service verbs, and launch fault compensation/reconciliation.
