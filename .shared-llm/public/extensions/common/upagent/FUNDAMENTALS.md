# UpAgent lifecycle fundamental

The **UpAgent Recruiter Hub** is a universal lifecycle owner for an LLM worker. Its caller may
be a TUI, phase leader, Librarian, another agent, or an unrelated framework. Callers use one
request contract and do not need to understand Claude, Codex, Pi, Cursor, Herdr process details,
or cleanup mechanics.

The fundamental separation is:

```text
Requester                  owns intent and consequential decisions
Dedicated Account Manager  owns conversation and interpretation
Python Recruiter Hub       owns facts, durable state, and execution
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
PYTHON RECRUITER HUB
│
├─ validates the request
├─ assigns a globally unique request identity and generation
├─ persists ownership, deadlines, and an event ledger atomically
└─ creates one Dedicated Account Manager
   │
   ▼
DEDICATED ACCOUNT MANAGER (LLM)
│
├─ validates the requested harness/model/agent combination semantically
├─ explains a bad or ambiguous request to the Requester
├─ proposes structured lifecycle actions to the Hub
└─ never creates, closes, or kills a pane directly
   │
   ▼
PYTHON RECRUITER HUB
│
├─ validates the proposed action against the request and ownership token
├─ atomically starts the worker through Herdr
├─ proves that the expected process and detected agent became healthy
└─ publishes worker-healthy before anybody may report "running"
   │
   ▼
UPAGENT WORKER
│
├─ performs only the requested work
├─ writes one lease-private result
└─ exits when its work is complete
   │
   ▼
PYTHON LIFECYCLE MONITOR
│
├─ observes pane existence, process identity, Herdr agent status, result validity, and deadlines
├─ publishes mechanical evidence; it never claims to understand whether work is meaningful
└─ requests a one-shot LLM check only when evidence is ambiguous or suspicious
   │
   ▼
DEDICATED ACCOUNT MANAGER
│
├─ explains completion, misconfiguration, suspected stalls, and timeouts to the Requester
└─ asks the Requester to continue, extend, inspect, or cancel when a decision is required
   │
   ▼
PYTHON RECRUITER HUB
│
├─ executes the authorized action
├─ performs staged termination after an unanswered hard deadline
├─ closes only the pane owned by the current lease generation
├─ verifies absence before releasing ownership
└─ publishes the result and durable lifecycle receipt
```

## Reliability invariants

1. Every request has a globally unique Hub identity; a caller's human-readable order id is not
   used as a global namespace.
2. Every state change is an atomic durable event with a request id and generation.
3. Every wait is bounded by a monotonic deadline. Retries are bounded and idempotent.
4. Terminal keystrokes and pane scrollback are never a request, acknowledgement, or verdict.
5. `pane created` is not `worker healthy`. Health requires the expected foreground process,
   detected harness, expected working directory, and a non-terminal agent state.
6. Python records mechanical facts. An LLM interprets ambiguity but cannot override ownership,
   invent configuration, or execute an unvalidated lifecycle action.
7. A missing or malformed pre-launch manager decision becomes `needs-requester` or `blocked`; it
   never becomes guessed success. After Python has mechanically proved worker process, harness, and
   cwd health, a missing or malformed advisory startup assessment becomes `worker-healthy-degraded`
   and the worker continues. An advisory bookkeeping fault may not destroy a proven healthy worker.
8. The Requester owns continue/extend/cancel decisions. The Hub may act without a response only
   for valid terminal completion, proven orphan recovery, or a declared hard deadline after its
   grace period.
9. The Hub mechanically supervises Dedicated Account Managers. Managers do not recursively hire
   managers to watch managers.
10. No manager failure may leave an unowned worker. No cleanup receipt may claim success until
    the owned pane is verified absent.
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
    record is terminal, the Hub archives the premature result, keeps the lifecycle open, and tells
    the same watchdog to resume. Only the matching durable terminal record permits cleanup.
14. Detached job runners inherit no request command pipes. A caller receives the mechanically
    verified `REQUEST_ACCEPTED` response when startup finishes; a background runner cannot keep a
    captured stdout or stderr descriptor open and turn healthy startup into a false timeout.

## Lifecycle states

```text
requested → manager-starting → manager-ready → negotiating
    → spawning → startup-check → running
    → result-ready → awaiting-requester → completing → cleanup-verified → finished

negotiating/startup-check/running
    → needs-requester → retrying | cancelling | blocked

running
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
correct when an LLM is unavailable: it records the failure, informs the Requester, and follows
the configured deadline policy without silently losing the request.

## Use case: start one plan

Plan startup is one deterministic transaction; liveness comes from blocking awaits, not observers:

```text
just herdr-plan <run-dir>
└─ PYTHON PLAN CONTROLLER
   ├─ takes an exclusive run-start lock
   ├─ creates and health-checks the TUI in a fresh cockpit
   ├─ names the TUI/leader tab `control`
   ├─ writes control/plan-start.json with the TUI address
   └─ writes control/plan-start.json (watchdog block: `not-configured` by design)
   ...
   TUI per phase: upagent-phase-start → blocking upagent-phase-await loop
   ...
   └─ TUI writes the final run-status.md
      └─ just herdr-plan-finish <run-dir> succeeded|stopped
         ├─ atomically writes control/run-terminal.json
         ├─ authorizes the watchdog's final result
         └─ Recruiter closes only the owned watchdog and Account Manager panes
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

Quiet panes, a completed LLM turn, or the watchdog deciding that its current check is done cannot
end this lifecycle. The plan controller requires the final run summary before it publishes the
terminal marker.

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
transaction. A watchdog startup failure is different: it is an observability degradation, not a
work failure. Python records the cause, releases the healthy leader, returns `ready-degraded`, and
lets the TUI continue AFK. A live leader recorded by a prior owner is never destroyed by a new
start.

The roster has separate `phase_leaders:` templates because a controller launch is not a stage
worker launch. Python refuses a missing template instead of trimming a worker command or guessing
how a harness should run a phase leader.

The receipt is an observability record, not a work capability. The Recruiter inspects conventional
phase-tree orders and durably records a `phase-watchdog-degraded` event when the receipt is
missing or stale (legacy runs; a `not-configured` watchdog block is by design, never degraded),
but it still accepts the work. This prevents monitoring infrastructure
from freezing the plan. The TUI prompt remains harness-neutral and treats the controller as the
mandatory normal path; a stale or mistaken client can continue degraded instead of hanging.

Every compatibility rejection with a usable `order_id` and `result_path` writes a terminal
`blocked` result before emitting the old `ORDER … DONE` marker. Modern requesters receive typed
receipts directly. No caller should wait forever for success evidence that can no longer arrive.
