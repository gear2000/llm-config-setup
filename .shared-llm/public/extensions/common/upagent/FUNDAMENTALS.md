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
12. Cockpit placement is role-balanced without weakening startup atomicity. Account Managers and
    one-shot checkers split downward; workers split right; phase leaders split downward. Herdr
    therefore forms readable rows and corners instead of an unbounded strip of narrow columns.

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

## Use case: start one watched plan

Plan-level monitoring is created by the deterministic launcher, not remembered by the TUI:

```text
just herdr-plan <run-dir>
└─ PYTHON PLAN CONTROLLER
   ├─ takes an exclusive run-start lock
   ├─ creates and health-checks the TUI in a fresh cockpit
   ├─ writes control/plan-start.json with the TUI address
   └─ submits one plan-lifecycle-watchdog order to the Recruiter
      ├─ Dedicated Account Manager lives beside the TUI
      └─ plan watchdog lives beside the TUI
         ├─ watches the TUI and durable run state
         ├─ discovers each managed or unmanaged phase leader
         └─ sends advisory state changes to both the TUI and current leader
```

The plan watchdog does not replace the per-phase watchdog. It connects orchestration levels: the
plan watchdog watches TUI-to-leader handoff for the whole run, while each phase watchdog watches
one leader and its descendants. Neither has destructive authority. A plan-watchdog startup fault
is atomically recorded as `ready-degraded` and sent to the healthy TUI; it cannot freeze or cancel
the run. A TUI startup fault is terminal because no run owner exists.

## Use case: start one watched phase

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
├─ submits one ordinary phase-watchdog order to the Recruiter Hub
│  └─ Recruiter creates and verifies its Account Manager and watchdog worker
├─ attempts REQUEST_ACCEPTED with both verified pane addresses
├─ records `watchdog-ready` or an explicit degraded warning, then opens the leader gate
├─ verifies the expected leader process, harness, and cwd
└─ atomically publishes PHASE_STARTED as `ready` or `ready-degraded`
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
phase-tree orders and durably records a `phase-watchdog-degraded` event when the receipt or live
watchdog address is absent, but it still accepts the work. This prevents monitoring infrastructure
from freezing the plan. The TUI prompt remains harness-neutral and treats the controller as the
mandatory normal path; a stale or mistaken client can continue degraded instead of hanging.

Every compatibility rejection with a usable `order_id` and `result_path` writes a terminal
`blocked` result before emitting the old `ORDER … DONE` marker. Modern requesters receive typed
receipts directly. No caller should wait forever for success evidence that can no longer arrive.
