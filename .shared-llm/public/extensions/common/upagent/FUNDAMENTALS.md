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
7. A missing or malformed LLM response becomes `needs-requester` or `blocked`; it never becomes
   guessed success.
8. The Requester owns continue/extend/cancel decisions. The Hub may act without a response only
   for valid terminal completion, proven orphan recovery, or a declared hard deadline after its
   grace period.
9. The Hub mechanically supervises Dedicated Account Managers. Managers do not recursively hire
   managers to watch managers.
10. No manager failure may leave an unowned worker. No cleanup receipt may claim success until
    the owned pane is verified absent.

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
├─ requires REQUEST_ACCEPTED with both verified pane addresses
├─ records a `watchdog-ready` receipt, then opens the leader gate with that receipt in its environment
├─ verifies the expected leader process, harness, and cwd
└─ atomically publishes PHASE_STARTED only after the complete pair is healthy
```

If validation or watchdog startup fails, the leader gate never opens and Python closes only the
leader pane created by that transaction. A live leader recorded by a prior owner is never
destroyed by a new start. The failure cause remains in `phase-start.json`. This makes “leader is
running without a watchdog” an invalid state rather than something a human must notice later.

The roster has separate `phase_leaders:` templates because a controller launch is not a stage
worker launch. Python refuses a missing template instead of trimming a worker command or guessing
how a harness should run a phase leader.

The receipt is also an enforced capability, not merely a status file. A conventional phase-tree
stage order is accepted only when its leader process inherited the exact
`UPAGENT_PHASE_START_RECEIPT` path and that receipt binds the same phase, leader pane, and live
watchdog address. Thus a manually-created leader may be visible, but it cannot dispatch phase
work. The phase-watchdog bootstrap order is the narrow exception: it is created by the controller
before the leader is released. This boundary is harness-neutral; TUI and phase-leader prompts must
also treat the controller as mandatory and report a failed start rather than improvising panes.
