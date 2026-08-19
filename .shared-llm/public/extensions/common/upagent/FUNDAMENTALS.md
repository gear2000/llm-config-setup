# UpAgent lifecycle fundamental

The **UpAgent Recruiter** is a universal durable lifecycle owner for an LLM worker. Its caller may
be a TUI, phase leader, another agent, or an unrelated framework. Callers use one
request contract and do not need to understand Claude, Codex, Pi, Cursor, Herdr process details,
or cleanup mechanics.

The fundamental separation is:

```text
Requester                  owns intent and consequential decisions
Strict Python boundary     validates one closed schema; never repairs or guesses intent
Dedicated Account Manager  observes and advises on one request; never owns authority
Python Recruiter           owns facts, leases, validation, execution, publication, and terminality
UpAgent worker             owns the requested work and its result
One-shot check agent       advises on ambiguous evidence only
```

## Use case: request one worker

```text
REQUESTER
│
├─ writes the requested work and worker preferences
└─ submits the request with a durable reply address
   │
   ▼
PYTHON RECRUITER
│
├─ accepts only one closed `schema_version: 1` JSON object or the equivalent named flags
├─ rejects unknown keys, wrong types, invalid combinations, unapproved offerings, and bad paths
│  before creating a ledger entry, Account Manager, pane, or worker
├─ never invokes an intake LLM, repairs prose, guesses a field, or materializes arbitrary arguments
├─ snapshots the exact prompt bytes and approved offering selection as immutable evidence
├─ makes same-id/same-payload retries idempotent and rejects same-id/changed-payload conflicts
├─ assigns a globally unique request identity and generation
├─ persists ownership, deadlines, and an event ledger atomically
└─ attempts one code-rendered `claude-sonnet-5`/low Dedicated Account Manager;
   manager failure records degraded supervision and does not stop the worker lifecycle
   │
   ▼
DEDICATED ACCOUNT MANAGER (LLM)
│
├─ observes bounded lifecycle evidence for one request
├─ explains ambiguity and may recommend one artifact repair
├─ proposes structured advisory actions to the Recruiter
└─ never approves/rejects Python-valid startup, mutates leases, publishes, or terminalizes
   │
   ▼
PYTHON RECRUITER
│
├─ validates any proposed manager action against the request and ownership token
├─ continues under direct Python supervision when the manager is unavailable
├─ atomically starts the worker through Herdr
├─ proves that the expected process and detected agent became healthy
└─ publishes worker-healthy before anybody may report "running"
   │
   ▼
UPAGENT WORKER
│
├─ performs only the requested work
├─ stages lease-private result.json (mandatory — it carries the verdict)
├─ stages compacted.md and handoff.md when it can (best-effort summaries)
├─ additionally stages answer.json when it is a specialist (mandatory)
└─ in an interactive harness, remains addressable until Recruiter cleanup; exec harnesses exit
   │
   ▼
PYTHON LIFECYCLE MONITOR
│
├─ observes pane existence, process identity, Herdr agent status, result validity, and deadlines
├─ publishes mechanical evidence; it never claims to understand whether work is meaningful
└─ requests a one-shot LLM check only when evidence is ambiguous or suspicious
   │
   ▼
DEDICATED ACCOUNT MANAGER (WHEN AVAILABLE)
│
├─ explains completion, misconfiguration, suspected stalls, and timeouts to the Requester
└─ asks the Requester to continue, extend, inspect, or cancel when a decision is required
   │
   ▼
PYTHON RECRUITER
│
├─ executes the authorized action
├─ performs staged termination after an unanswered hard deadline
├─ closes only the pane owned by the current lease generation
├─ verifies absence before releasing ownership
├─ validates staging; requests at most one repair from the same worker/address
├─ writes a Python-authored blocked bundle if that one repair fails
└─ projects and revalidates every public artifact before receipt and terminal evidence
```

## Use case: retain one worker for controller review

A managed requester may opt one worker into `completion_policy: requester_release` (the public façade exposes this as `--keep-open`). The worker performs a coding pass, writes a numbered lease-private checkpoint, and returns to idle in the same Herdr pane/session. The requester inspects the worktree and tests, then authenticates with the original control-token file to send feedback to that same worker or release it for terminal artifact publication. Checkpoints and feedback are durable; Herdr prompt injection only wakes the already-owned worker. The original lease deadline covers both work and review and may be extended through the existing requester-owned timeout decision. Only release authorizes a normal terminal bundle and cleanup; cancellation, timeout, or an early worker exit may still produce a Python-authored blocked terminal outcome. Ordinary workers remain one-shot.

## Reliability invariants

1. Every request has a globally unique durable identity; a caller's human-readable order id is not
   used as a global namespace.
2. Every state change is an atomic durable event with a request id and generation.
3. Every wait is bounded by a monotonic deadline. Retries are bounded and idempotent. Before
   Recruiter acceptance, invocation-only cockpit routing may refresh on an identical retry; after
   acceptance, the exact order and its pane are immutable and attachment needs no live caller pane.
4. Terminal keystrokes and pane scrollback are never a request, acknowledgement, or verdict.
5. `pane created` is not `worker healthy`. Health requires the expected foreground process,
   detected harness, expected working directory, and a non-terminal agent state.
6. Python records mechanical facts and rejects invalid public input without launching anything.
   An LLM may interpret bounded lifecycle ambiguity only after Python has accepted the closed
   request schema; it cannot repair intake, override ownership, invent configuration, or execute
   an unvalidated action. Persona text is guidance. Typed parsing, immutable offering snapshots,
   and the strict order contract enforce safety; prose labels cannot authorize execution.
7. Account Manager output is advisory. A missing, malformed, or crashed manager degrades
   supervision and is reported, but Python still runs mechanically valid work and produces a
   terminal bundle. After Python proves worker process, harness, and cwd health, no manager
   classification may veto startup. An advisory bookkeeping fault may not destroy a proven healthy
   worker, publish success, or leave the request non-terminal.
8. The Requester owns continue/extend/cancel decisions. The Recruiter may act without a response only
   for valid terminal completion, proven orphan recovery, or a declared hard deadline after its
   grace period.
9. The Recruiter mechanically supervises Dedicated Account Managers. Managers do not recursively hire
   managers to watch managers.
10. No manager failure may leave an unowned worker. No cleanup receipt may claim success until
    the owned pane is verified absent. Every launched lifecycle role is journaled before launch
    with a random agent name/lease token and the owner's PID plus process start time. Reconciliation
    resolves that name and verifies agent, process, cwd, and lease identity; a stale pane id alone
    is never enough to close a pane. If an owner dies during an in-flight start, one not-found
    lookup keeps the journal `launch-uncertain` and open for later reconciliation sweeps.
    Cancellation cannot publish verified absence while a live owner holds a `launching` journal;
    it waits without `flock` until the owner commits its pane identity or dies.
11. A Dedicated Account Manager defaults to the requester's cockpit workspace, beside its worker.
    Shared-services placement is explicit rather than an invisible default.
12. Cockpit placement is role-separated without weakening startup atomicity. The `control` tab
    holds the TUI and current phase leader, `workers` holds active stage workers, and `oversight`
    holds opt-in Account Managers and one-shot checkers. Role tabs are created lazily by
    moving the first live agent pane itself, so there are no placeholder shells. A cross-process
    workspace lock prevents concurrent hires from creating duplicate role tabs. Placement
    completes before the pane address is published. Within a role tab, workers split right and
    support roles split downward. All layout calls are bounded and presentation-only: failure
    leaves the healthy agent in its source tab, emits a warning, and never changes worker health,
    ownership, or lifecycle state.
13. (Legacy runs only.) A watchdog's own `result.json` is not completion authority. Its order names a durable terminal
    record owned by the plan or phase controller. If the watchdog writes a result before that
    record is terminal, the Recruiter archives the premature result, keeps the lifecycle open, and tells
    the same watchdog to resume. Only the matching durable terminal record permits cleanup.
14. Every command imports current canonical source and exits; no resident Python process can cache
    stale code. Fire-and-forget work uses a detached `run-job` supervisor whose PID is durable lease
    evidence. One machine-local `flock` serializes individual token-fenced ledger mutations;
    reconciliation performs process/Herdr work outside it and acquires it only for each CAS commit.
    It is never held across Herdr waits or a worker lifetime. Reads remain
    lock-free. A caller receives `REQUEST_ACCEPTED` when verified startup finishes.
15. Interactive-harness `done` means one LLM turn ended, not that the assignment ended. Claude,
    Pi, and Cursor remain live until a valid typed bundle, positively confirmed pane/process exit,
    or the request deadline. Codex exec keeps process-turn completion semantics. Under a live
    Sentinel the closeout file supersedes the bundle/exit triggers (see 20). A terminal record
    always answers with exactly one structured outcome. Typed completion
    validates lease-private staging, projects every public artifact, revalidates public paths,
    writes the receipt, and only then writes terminal state/event/requester evidence. Await cannot
    wake from pre-receipt `result-ready` evidence. Publication writes the validated result, the
    Recruiter's own durable copy of it, and the receipt naming that copy together. History pruning
    additionally requires matching `runner-completed.json`, written only after the detached
    supervisor's final notification; crash reconciliation may write it only after proving that
    supervisor dead. When the caller's `result_path` is later absent or unreadable, the Recruiter republishes from that
    copy; when no trustworthy copy survives, it refuses visibly with the order id, the loader's
    reason, the receipt path, and the recorded verdict. A terminal record never crashes the result
    loader and never silently reopens finished work.
16. Every accepted path has a required typed manifest. Compatibility input is normalized before
    ledger mutation, never given a result-only fallback. Once an interactive wait legitimately
    reaches completion with a missing or malformed required bundle, it receives exactly one repair
    request at the original worker address. A completely absent bundle keeps a live worker under
    supervision until valid artifacts, positively confirmed process/pane exit, or the deadline;
    turn-level `done` is not a repair or terminal boundary. A second worker is never launched. One
    failed revalidation, crash reconciliation, or worker/manager cleanup failure regenerates one
    Python-authored schema-valid blocked three-file bundle; specialists also receive a valid failure
    answer. Success prose is never retained beside a blocked result.
17. Retained review is explicit and release-gated. A valid checkpoint keeps the same worker pane live; it is not a terminal result. Feedback and release require the current requester control token and current lease identity. Premature terminal artifacts are quarantined, and a worker that exits before release blocks rather than entering artifact repair. The default order shape and one-shot cleanup path are unchanged when no completion policy is present.
18. Mandatory consultations are machine-readable `{consult_id, specialist}` requirements. A stage
    can pass only with matching Recruiter-verified receipts whose answers are cited successes. Absent,
    rejected, failed, borrowed, or forged claims block finalization; direct source reading is not
    consult evidence.
19. Startup is proven twice: worker health, then a first observable action recorded in the
    ledger. The liftoff deadline is clamped to the smaller of 5 minutes and half the order's
    own work cap, so a never-started worker is always classified before the hard timeout
    backstop can claim even the shortest valid order. The typed `never-started` verdict is
    minted only deadline-proven — the recorded worker-never-started event, scoped to the
    same worker attempt so a prior attempt's proof never authorizes a later one, plus
    genuinely zero side effects: no staged artifact file of any kind and no dirty-worktree
    evidence — and is auto-retried exactly once with a fresh worker; any side effect routes
    the miss to the ordinary blocked path.
    Python-authored blocked terminals carry the harness epilogue (commits, files touched,
    staged artifact files), and a `passed` result is published only after the validator parses
    the worker's own artifact files — a report that contradicts the verdict, or an explicitly
    empty findings record beside a non-empty report, forces re-evaluation instead of a silent
    accept.
20. Sentinel supervision is default-on and advisory-only. One cheap Sentinel pane per worker
    attempt watches liftoff → pulse → landing → closeout. Its pulse is event-driven on a
    per-attempt wake file that carries the wake REASON as its content — Python is the
    only writer (staging activity, valid or
    partial, and proven worker death), the Sentinel the only consumer via a bounded
    wake-wait run under an explicit command timeout, with a short interval as the
    fallback timer. While it is live its typed
    `closeout.json` is THE teardown trigger — a validated staged bundle wakes the
    Sentinel to close out now (the wake file is the one channel — published by an
    atomic temp-write-and-rename, claimed by the Sentinel with an atomic rename, and
    republished while the window is open, so a write racing a claim is never lost —
    and a `valid-bundle` reason forbids re-sleeping) under one bounded landing window of at
    least a full pulse block, shared by the interactive and exec waits, whose lapse —
    checked before the generic timeout, so a deadline-clipped window still ends on the
    validated bundle — falls back to the mechanical artifact path with a typed lapse
    event, and proven worker pane/process
    death — which also touches the wake file so the window opens on an awake
    Sentinel, and whose own window lapse is the same typed event — or the mechanical
    never-started
    deadline, whose LIFTOFF the live Sentinel owns with a brief that carries the same
    clamped deadline — first opens a bounded closeout window before the mechanical path
    resumes, skipped entirely when a valid staged bundle already exists at proven exit
    (bypassed-at-exit) — yet the closeout is never an authority. Supervision is
    re-evaluated throughout the wait: the Sentinel's own pane is probed on the worker's
    probe cadence, and a confirmed-gone Sentinel degrades supervision to the mechanical
    paths for the rest of the wait. Python re-verifies every closeout
    citation (a path citation corroborates only inside the request's own worktree/cwd or
    ledger territory), validates a claimed COMPLETE against the ordinary bundle contract
    (one extra landing round on rejection), and routes NEVER_STARTED / STALLED /
    FINALIZATION_FAILED through the same salvage triage and epilogue paths as mechanical
    faults; a published blocked reason lists Python-verified citations separately as
    checked fact, with the Sentinel's LLM-authored interpretation attached but always
    marked uncorroborated, and a closeout `blocking_question` surfaces
    on the published result, the receipt, and any Python-composed retry brief. A STALLED
    closeout with zero corroborated citations may not terminalize a provably live worker:
    Python re-probes the worker pane once and rejects the closeout back to the Sentinel
    for one re-check only on a positive pane answer — probe uncertainty accepts the
    original claim. A confirmed stall over a positively present worker is nudged by the
    hub before it may end the wait: Python alone delivers the one literal payload
    `continue` through the worker's agent-idle prompt path, under an
    intent-before-delivery idempotency record, a backoff ladder, and a hard cap of 3,
    all durable in ledger events and `nudges.json`; a staged bundle that already
    validates supersedes the stall (completion wins the race), the Sentinel's brief
    makes STALLED provisional so it stays for later rungs, and nudges are refused in
    requester-facing/terminal states (checked again immediately before delivery,
    failing closed on unreadable state), for mismatched attempt/generation journals,
    and for gone panes; keep-open workers are never
    classified as stalled (they carry no Sentinel), corrupt nudge state falls through
    to the pre-ladder blocked path, and exhausted nudges publish exactly
    one typed requester escalation (durable idempotency flag) before the stall ends
    the wait as before. An opt-in
    cross-provider gate (`UPAGENT_REQUIRE_CROSS_PROVIDER_SENTINEL=1`) degrades a
    same-provider (or unprovable) sentinel hire to mechanical supervision with a typed
    reason rather than hiring silently. The Sentinel
    holds no kill switch and never outlives its worker attempt; a failed hire — a
    missing persona (diagnosed pre-pane with the exact paths, once per invocation) or a
    refused pane creation alike — degrades supervision and leaves the
    mechanical paths fully in charge, and a dead Sentinel changes nothing: the hard
    deadline, salvage, and rescuer backstops fire unchanged. Every supervision state
    change (hired, degraded, dead, wake-file touches by kind — `valid-bundle`,
    `partial-staging`, `worker-gone`, and the truthful `never-started` for the liftoff
    deadline — window-lapsed by window, bypassed-at-exit,
    closeout consumed, stalled-rejected) is a distinct typed ledger event. The liftoff address
    relay hands the requester the worker's pane; requester→worker messages travel only
    through the ledger-logged message command.

## Lifecycle states

```text
requested → manager-starting → manager-ready | manager-degraded
    → spawning → startup-check → running
    → awaiting-requester → completing → artifacts-projected → receipt-written → finished

negotiating/startup-check/running
    → needs-requester → retrying | cancelling | blocked

running
    → awaiting-review → running (feedback) | finalizing (release)
    → timeout-warning → awaiting-requester
    → extended | cancelling

manager or runner loss
    → orphan-recovery → retrying | blocked
```

The authoritative state is the durable ledger. Herdr panes are current addresses and visual
surfaces; pane ids may change or be reused and therefore never serve as durable identity.

## Deterministic and intelligent evidence

| Question | Owner |
|---|---|
| Does the pane exist? | Python |
| Is the expected process running? | Python |
| Does the working directory match? | Python |
| Is the result structurally valid and correctly identified? | Python |
| Has a lease or hard deadline expired? | Python |
| Does startup output describe an unsupported model or agent? | LLM manager |
| Is a quiet worker plausibly thinking, waiting for input, or stuck? | One-shot LLM check |
| Should the worker continue past its deadline? | Requester |
| May a pane be terminated? | Python, after validating owner authority or hard-deadline policy |

The LLM roles consume bounded evidence snapshots and return typed assessments. Python remains
correct when an LLM is unavailable: invalid public intake is rejected directly with zero ledger,
manager, pane, or worker launch, while Account Manager failure is recorded as degraded supervision
and the Python-owned worker lifecycle continues to a terminal bundle under the configured deadline
policy.

## Use case: start one plan

Plan startup is one deterministic transaction; liveness comes from blocking awaits, not observers:

```text
just run-start <run-dir>
└─ PYTHON PLAN CONTROLLER
   ├─ takes an exclusive run-start lock
   ├─ creates and health-checks the TUI in a fresh cockpit
   ├─ names the TUI/leader tab `control`
   ├─ writes a per-run hashed 0600 token file under the same-user 0700 runtime token directory
   ├─ passes RUNNER_OWNER_TOKEN_FILE, not a raw token, to the TUI
   ├─ writes control/plan-start.json with the TUI address
   └─ writes control/plan-start.json (watchdog block: `not-configured` by design)
   ...
   TUI per phase: upagent-phase-start → blocking upagent-phase-await loop
   ...
   └─ TUI writes the final run-status.md
      └─ just run-session-finish <run-dir> succeeded|stopped
         ├─ atomically writes control/run-terminal.json
         ├─ fences stale owners by token hash
         └─ cleanup closes only structurally owned, identity-verified panes
```

The cockpit tabs appear as their roles become active:

```text
plan workspace
├─ control     TUI + current phase leader
├─ workers     active stage UpAgent workers
└─ oversight   opt-in Account Managers + one-shot checkers
```

There is no standing watchdog at either level. The TUI's blocking `upagent-phase-await` and the
leader's blocking `upagent-await`/`upagent-await-any` reconcile durable state against live Herdr
state every sweep; contradictions surface as typed `leader-missing`/`leader-stalled` events, quiet
surfaces as `inactivity-checkpoint`, and urgent unacknowledged events escalate to the human via
`herdr notification`. A TUI startup fault is terminal because no run owner exists.

Quiet panes, a completed LLM turn, or a one-shot checker deciding that its current check is done cannot
end this lifecycle. The plan controller requires the final run summary before it publishes the
terminal marker.

Recovery is deliberate. A second launcher that finds a fresh live owner becomes an observer; a
stale owner still requires `just run-session-snapshot <run-dir>` and
`just run-session-reconcile <run-dir>` before takeover. Mutating commands read the owner
token from `RUNNER_OWNER_TOKEN_FILE` or an explicit `--token-file`; `--token-stdin` is
supported only by the `guard` and `cleanup` lifecycle operations for one-off recovery. Those
runner commands read the bounded token directly from their own process stdin; UpAgent has no
socket transport or resident target. Avoid passing raw owner tokens in process command lines.

## Use case: start one phase

Phase startup is a single deterministic transaction, not a sequence the TUI LLM must remember:

```text
TUI
└─ just upagent-phase-start <route> <run-root> <phase> <pass>
   │
   ▼
PYTHON PHASE CONTROLLER
├─ validates the frozen route, selected profiles, roster, paths, and owning TUI pane
├─ starts the phase leader behind a closed filesystem gate
├─ records the exact leader pane under the phase/pass transaction
├─ opens the leader gate once the durable receipt records its identity
├─ verifies the expected leader process, harness, and cwd
└─ atomically publishes PHASE_STARTED as `ready` (watchdog block: `not-configured`)
```

Leader validation failures still keep the gate closed and close only the leader created by that
transaction. There is no standing phase watchdog in coordination v2; the `watchdog` receipt block
is `not-configured` by design. A live leader recorded by a prior owner is never destroyed by a new
start.

The roster has separate `phase_leaders:` templates because a controller launch is not a stage
worker launch. Python refuses a missing template instead of trimming a worker command or guessing
how a harness should run a phase leader.

The receipt is an observability record, not a work capability. The Recruiter inspects conventional
phase-tree orders and durably records a `phase-receipt-degraded` event when the receipt is
missing or stale (legacy runs; a `not-configured` watchdog block is by design, never degraded),
but it still accepts the work. A missing receipt is announced once per phase pass — the first
order atomically claims a marker beside the receipt path and later orders record quietly. This
prevents monitoring infrastructure from freezing the plan and keeps the pane readable. The TUI
prompt remains harness-neutral and treats the controller as the mandatory normal path; a stale
or mistaken client can continue degraded instead of hanging.

Every compatibility rejection with a usable `order_id` and `result_path` writes a terminal
`blocked` result before emitting the old `ORDER … DONE` marker. Modern requesters receive typed
receipts directly. No caller should wait forever for success evidence that can no longer arrive.
