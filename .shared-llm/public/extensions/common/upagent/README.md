# UpAgent — the Recruiter

UpAgent coordinates durable LLM worker lifecycles for a phase leader, TUI, or another framework.
The Python **Recruiter** persists each request, launches and verifies the worker, validates its
result, closes owned panes, publishes a receipt, and releases the lease. See
[FUNDAMENTALS.md](FUNDAMENTALS.md) for the authority model and use-case tree.

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
just upagent lists --type offerings|specialists|workers [--status active|terminal|all] [--json]
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

The engine assembles `offerings.yaml` from code-approved names under `offerings.d/`. Omitted machine configuration selects only `standard`, which contains the existing eighteen stable ids: three Claude, one Codex, eight Cursor, and six Pi. Selecting `[standard, claudex]` adds exactly `claudex-gpt-5-6-sol`; a destination `[standard]` replacement removes it. Management policy stays in `offerings-management.yaml`, so set selection cannot change specialist defaults, lifecycle commands, or management candidates.

The same parsed object drives text/JSON listing, request validation, specialist/lifecycle references, and the immutable order snapshot. YAML contains declarations only. Code pins every approved set member, harness, model, provider, effort list, completion style, health identity, executable, command renderer, and preflight. Unknown sets, partial sets, duplicate ids, changed fields, commands in YAML, and management references to absent offerings fail loudly. The runtime roster resolves from the request's starting repository, then `$UPAGENT_CANONICAL_REPO` for the same git repo, then the main checkout for a linked worktree, then the generated home roster.

`offerings.py` renders child tokens: Claude uses `--effort`, Codex uses `-c model_reasoning_effort=...`, and Pi uses a provider-qualified `--model` plus explicit `--thinking`. Cursor has no effort control: its only allowed selection is the canonical `default` effort. ClaudeX uses the `claudex` executable with the exact `gpt-5.6-sol` model and remains interactive; because the wrapper uses `exec`, Herdr health still requires the final `claude` process. Before any ClaudeX worker pane is created, UpAgent requires both `claudex` and `claudex-doctor`, runs `claudex-doctor gpt-5.6-sol`, and blocks on a missing executable, proxy/OAuth failure, or absent model. It never substitutes native Claude. Legacy and controller recipes use the same per-command dispatcher; their strict order files bypass no lifecycle validation, but ClaudeX is accepted only through public offering snapshots.

A request's manager, worker, and short-lived checkers start beside `order.cockpit_pane` through
atomic `herdr agent start` calls. Pane placement remains role-based and every pane is closed only by
its fenced lease owner.

Phase startup has its own deterministic front door:

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
  candidates in `offerings.yaml`: `cursor-composer-2-5` at `default`, then
  `pi-gpt-5-4-mini` at `low`, both with the `upagent-account-manager` brief. The Recruiter removes
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
  is accepted. A confirmed stall over a provably live worker does not end the wait
  immediately either: the hub runs its own nudge ladder first — Python (never the
  Sentinel) delivers the one literal payload `continue` through the worker's agent
  address (the agent-idle prompt path, so a busy tool or foreground state refuses
  delivery), with an intent-before-delivery record under an idempotency key
  `(generation, attempt, nudge_index)`, a backoff ladder (immediate, then 5, then
  15 minutes) and a hard cap of 3, all persisted beside the closeout in
  `nudges.json` and in typed ledger events (`worker-nudge-intent` /
  `worker-nudge-delivered` / `worker-nudge-failed` / `worker-nudge-held` /
  `worker-nudge-rejected`). Completion always wins the race: a staged bundle that
  already validates supersedes the stall (`worker-nudge-superseded`) instead of
  resuming a finished worker. A STALLED closeout is provisional in the Sentinel's
  own brief — it stays idle for the hub's disposition and resumes PULSE on
  `SENTINEL_STALL_NUDGED` — so later rungs are actually reachable. Nudges are
  refused outright in requester-facing,
  release, cancelling, and terminal states (checked at classification AND re-checked
  immediately before delivery, failing CLOSED on unreadable state; nudge delivery
  uses a short ~10s idle wait — a stalled worker is already idle, a busy worker is
  not stalled and fails the rung — so the gate-to-send window is seconds, an
  accepted residual rather than a mutation-lock reservation), for a journal
  whose attempt/generation is not this watch's own, for requests with no started
  worker launch, and for a worker pane that is not POSITIVELY present; corrupt
  nudge state is recorded (`worker-nudge-state-invalid`) and falls through to the
  exact pre-ladder blocked path; retained
  keep-open workers never get a Sentinel at all, so their designed idle checkpoint
  is never classified as a stall. Exhausted nudges publish exactly one durable
  `worker-stall-escalation` requester-mailbox event (idempotent via a durable
  `escalated` flag in the nudge records, published-then-flagged so a crash risks a
  rare duplicate, never a lost escalation) pointing at the Python-owned
  nudge records and archived closeouts, and only then does the stall end the wait
  exactly as before. Cross-provider supervision is mandatory on every hire,
  including retries. Public worker offerings pin code-owned provider metadata in
  their immutable snapshots; legacy orders fall back to known harness/model identity
  only when no snapshot provider exists. For the public roster, the Recruiter filters the
  YAML-ordered Sentinel candidates (`cursor-composer-2-5`/default, then
  `pi-gpt-5-4-mini`/low) by provider and tries every eligible startup in order. Each failure is
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

## Public offerings and legacy controller roster

Public workers and management candidates never read a YAML launch command. `offerings.yaml`
selects approved harness/model identities and effort allowlists, plus the candidate order;
`offerings.py` validates every reference and renders the exact child argv. Public Account Manager,
Checker, and Sentinel candidates are `cursor-composer-2-5`/default first, then
`pi-gpt-5-4-mini`/low. The Recruiter filters the worker's provider and falls back on startup
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
UUID/ULID idempotency and conflict behavior, the exact 18-entry text/JSON roster, exact Claude /
Codex / Cursor / Pi child tokens, Cursor default-effort canonicalization and interactive repair,
role-aware launch-state transitions, Codex exec-style no-live-repair completion, specialist
offering resolution, request mailboxes, identity/lease fencing,
startup health, timeout authority, typed manifests, every missing/malformed artifact, one bounded
same-worker repair, manager degradation, specialist projection, mandatory-consult enforcement,
publication fault ordering, and cleanup. Focused per-command tests also cover mutation exclusion, lock-free pure reads, fresh module imports,
detached-supervisor launch failure, duplicate attachment to one live runner, main/worktree shared
state, thin service verbs, and launch fault compensation/reconciliation.
