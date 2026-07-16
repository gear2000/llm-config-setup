# Reliable Herdr Lifecycle Coordination

Status: design proposal; not yet implemented

This document describes how to make Herdr plan execution reliably return control to the correct
agent when a phase finishes, blocks, fails, disappears, or needs human input. It also evaluates
whether native harness sub-agents can replace the current plan and phase watchdog agents.

The central recommendation is:

1. Use a deterministic `phase-await` rendezvous as the primary plan coordination mechanism.
2. Treat native sub-agents as an optional ownership optimization when a parent and child use the
   same harness.
3. Keep durable mailboxes as the authoritative record and eventual-delivery mechanism.
4. Do not make an LLM watchdog responsible for reliable message transport.
5. Retain an LLM observer only where mechanical evidence is genuinely ambiguous.

## Executive summary

The current design can detect a problem and still fail to tell the owner about it. Recruiter and
watchdog notifications eventually call `herdr pane run`, but first wait for the recipient to become
`idle`. A TUI waiting on a phase or a phase leader blocked in a tool call is not idle. The condition
that makes notification important can therefore prevent notification from being delivered.

This is a circular dependency:

```text
phase leader blocks
        |
        v
watchdog notices the block
        |
        v
watchdog waits for TUI to become idle
        |
        v
TUI remains busy waiting for the phase leader
        |
        +---------------- notification never arrives
```

The primary correction is to reverse the relationship. The TUI should enter one deterministic
await operation after it starts a phase. That operation returns a typed tool result for every event
that requires the TUI to act. Returning the tool result naturally resumes the TUI's LLM turn; no
agent must paste text into a busy terminal.

```text
Claude Code TUI
    |
    +-- start phase leader: Claude Code, Pi, Codex, or another harness
    |
    `-- phase-await
            |
            +-- completed ------------> return typed event to TUI
            +-- blocked --------------> return typed event to TUI
            +-- needs human input ----> return typed event to TUI
            +-- leader disappeared ---> return typed event to TUI
            +-- worker warning -------> return typed event to TUI
            `-- timeout --------------> return typed event to TUI
```

This preserves Claude Code as the remote-facing TUI while allowing phase leaders and workers to use
other harnesses.

## Problem statement

Herdr has several distinct communication requirements:

- A phase leader must tell the TUI that a phase completed.
- A phase leader must tell the TUI when it cannot continue without a decision.
- A worker lifecycle owner must tell its requester that startup failed, work completed, or a limit
  was reached.
- A monitor must report contradictory or suspicious evidence without claiming certainty.
- A human must be notified when no agent acknowledges an urgent event.

The current implementation has durable lifecycle files, Herdr status information, and LLM
watchdogs, but the final delivery step is generally terminal input injection. That creates several
failure modes:

- The target is `working`, `blocked`, missing, or has moved to another pane.
- A bounded idle wait expires before the target is receptive.
- Terminal input is accepted by Herdr but not interpreted as a prompt by the harness.
- The target is inside a foreground tool call, so injected text can contaminate terminal input.
- A watcher terminates after observing quiet rather than a durable terminal condition.
- A notification is recorded but never acknowledged or retried.
- A stale pane address identifies a different pane after layout changes.
- Both sides wait for one another indefinitely.

The current code demonstrates the circular dependency directly:

- `.shared-llm/public/extensions/common/upagent/recruiter.py::_notify_requester` writes a durable
  requester message, then attempts best-effort Herdr delivery.
- `_submit_agent_prompt` waits for `idle` before resolving the current pane and running a prompt.
- Requester delivery currently uses a 250 ms idle timeout.
- `.shared-llm/public/layers/agents/common/plan-lifecycle-watchdog.md` instructs the watchdog to use
  the same wait-until-idle pattern and retry later.

The durable record is useful, but there is no receiver that guarantees the record becomes an agent
turn.

## Design principles

### Detection, recording, delivery, and decisions are separate responsibilities

Every lifecycle event passes through four independent stages:

```text
detect -> record -> deliver -> decide
```

- **Detect**: code or an LLM observes evidence.
- **Record**: code atomically persists a typed event.
- **Deliver**: a deterministic mechanism wakes the responsible owner.
- **Decide**: the owner, or a human when required, determines the action.

No component may report an event as delivered merely because it was detected or written to disk.
No watchdog may infer authority from its ability to observe.

### Durable state is authoritative

Pane text and agent status are observations. Files such as phase results, lifecycle receipts,
leases, and inbox messages are the durable record. Visible terminal state may corroborate durable
state but must not replace it.

### One owner controls each lifecycle

- The TUI owns the plan.
- The phase leader owns its phase.
- The Dedicated Account Manager owns one UpAgent request.
- The worker owns its assigned task but not its own lease or destruction policy.
- Monitors are advisory.
- The Recruiter Hub enforces contracts and performs mechanical lifecycle operations.

### A waiting owner must have a deterministic wake-up path

An owner must not remain inside an indefinite shell wait for only one success string. Its await
operation must return on every state that requires a decision, including errors and contradictions.

### Cross-harness behavior must not depend on harness-specific hooks

The high-level lifecycle contract remains portable. Harness-native adapters may optimize delivery,
but durable state, event types, authority, and acknowledgement rules must be consistent.

### Watchdogs advise; they do not keep the system alive

Removing or crashing a watchdog must reduce observability, not stop useful work. Reliable startup,
completion, timeout, and blocked-state propagation belong to deterministic code.

## Goals

- Keep Claude Code available as the TUI so its remote application remains usable.
- Permit phase leaders and workers to run under Claude Code, Pi, Codex, or another configured
  harness.
- Resume the TUI automatically when a phase reaches any actionable state.
- Prevent silent stalls.
- Preserve enough evidence to diagnose every transition after the fact.
- Avoid wasteful high-frequency polling.
- Avoid unsafe terminal input injection while an agent is active.
- Support bounded execution, explicit extension, and owner-authorized cancellation.
- Keep the Recruiter Hub deterministic and fail loud at contract boundaries.

## Non-goals

- A monitor does not decide whether work is correct.
- A monitor does not terminate a healthy worker based only on pane silence.
- The Recruiter does not override the requester except at an explicitly configured hard limit.
- The TUI does not directly manage every worker process.
- A native sub-agent relationship is not required across different harnesses.
- This proposal does not require a Claude Code-only hook.

## Alternatives

### Option 1: durable cross-harness agent bus

Option 1 gives every logical agent a durable inbox and a harness adapter.

```text
phase leader ----+
watchdog --------+--> durable agent inbox --> harness adapter --> TUI
recruiter -------+          |                       |
                            |                       +-- Pi native message
                            |                       +-- Claude SDK input
                            |                       +-- Codex adapter
                            |                       `-- safe Herdr fallback
                            `--> acknowledgement ledger
```

Messages move through explicit states:

```text
queued -> claimed -> presented -> acknowledged
```

Delivery workers retry until acknowledgement or expiry. Pane injection is only a presentation
adapter; it is never the durable transport.

#### Advantages

- Strongest cross-harness abstraction.
- Supports unsolicited messages at any time.
- Separates logical addresses from volatile pane identifiers.
- Can support multiple subscribers and escalation policies.
- Provides complete delivery evidence.

#### Costs

- Introduces a general messaging subsystem.
- Requires a reliable adapter for every harness.
- Claude Code interactive mode may still require delivery at a safe prompt boundary unless it is
  hosted through a programmatic SDK or streaming-input wrapper.
- Requires acknowledgement, deduplication, retry, expiry, and recovery logic.

#### Best use

Use this when arbitrary cross-harness agents must communicate throughout long-lived work, not only
at plan coordination boundaries.

### Option 2: deterministic `phase-await` rendezvous

Option 2 makes plan coordination request/response shaped. After starting a phase, the TUI invokes
one deterministic await operation. The operation observes all relevant phase event sources and
returns when the TUI has something to do.

```text
TUI                         phase controller                  phase leader
 |                                  |                              |
 |-- phase-start ------------------>|-- launch ------------------->|
 |<-- startup receipt --------------|                              |
 |                                  |                              |
 |-- phase-await ------------------>|                              |
 |       foreground tool call       |<-- durable/event evidence ---|
 |                                  |                              |
 |<-- {kind: "blocked", ...} -------|                              |
 |                                  |                              |
 |-- phase-command/decision ------->|----------------------------->|
 |-- phase-await ------------------>|                              |
 |                                  |<-- phase-result.json --------|
 |<-- {kind: "completed", ...} -----|                              |
```

The TUI is technically waiting, but it is waiting through a tool whose successful return becomes
the next model input. No third agent needs to inject a prompt.

#### Advantages

- Small and purpose-built.
- Works with a Claude Code TUI and a phase leader running under any harness.
- Fits the current phase-start and durable result structure.
- Makes blocked and failure states normal return values rather than out-of-band warnings.
- Eliminates the plan watchdog as a required component.
- Does not require a general asynchronous message bus.
- Naturally works with a remote-facing TUI because the TUI's existing tool call completes.

#### Costs

- Primarily solves hierarchical plan coordination, not arbitrary peer-to-peer messaging.
- The await operation must multiplex several sources correctly.
- A foreground tool can still have harness or transport time limits; the implementation must use
  bounded waits, heartbeats, or a resumable wait token.
- Truly unsolicited events outside an active await need a mailbox, native harness adapter, or human
  notification.

#### Best use

Use this as the primary mechanism for TUI-to-phase and phase-leader-to-worker orchestration.

### Option 3: native sub-agent ownership tree

Native sub-agents establish a parent/child relationship inside one harness.

```text
Claude Code TUI
`-- Claude phase leader sub-agent
    `-- returns one result to the TUI

Pi Dedicated Account Manager
`-- Pi worker sub-agent
    `-- returns one result to the manager
```

This is attractive because the parent naturally receives the child's final result. The parent also
has a clear reason to remain alive while its child is active.

#### Important limitation: native sub-agents are harness-local

A Claude Code parent can create a Claude Code sub-agent. A Pi parent can create a Pi child using a
Pi extension. A Claude Code TUI cannot create a native Pi or Codex child without invoking an
external process or tool. Once the child crosses that boundary, the system needs a transport or
rendezvous again.

#### Important limitation: child completion is not continuous supervision

A normal sub-agent invocation provides a final result to its parent. It does not automatically
provide a portable stream of intermediate lifecycle messages. A background child may complete
while the parent is busy, but harness-specific scheduling determines when the parent sees that
result.

#### Important limitation: a blocking sub-agent tool can recreate the same problem

If a Dedicated Account Manager invokes a worker sub-agent through a tool call that blocks until the
worker exits, the manager cannot negotiate with the requester during the run. The child is owned,
but the owner is unavailable. The correct implementation is a supervisor extension or controller
that owns the child process and can emit events into the parent session without occupying an
uninterruptible tool call.

#### Advantages

- Clear local ownership.
- Natural final-result propagation.
- Lower process and pane clutter when the harness manages children internally.
- Context isolation between parent and child.
- Good fit when one harness is intentionally used for both roles.

#### Costs

- Couples topology to a harness.
- May reduce visibility in Herdr because a child is not necessarily a pane.
- Parent and child can share a failure domain.
- A parent crash can orphan or terminate children depending on the harness implementation.
- Harder to move a worker to a different provider or harness.
- Native child semantics are not identical across Claude Code, Pi, and Codex.

#### Best use

Use native sub-agents as an optimization inside one lifecycle owner, especially a Pi Dedicated
Account Manager supervising a Pi worker. Do not make them the only plan coordination contract.

## Comparison

| Property | Durable bus | `phase-await` | Native sub-agent |
|---|---:|---:|---:|
| Cross-harness | Strong | Strong | Weak |
| Arbitrary unsolicited messages | Strong | Limited | Harness-specific |
| Simple plan completion | More machinery | Strongest | Strong when same harness |
| Claude Code remote TUI | Supported with adapter | Natural fit | Forces Claude child |
| Continuous parent availability | Yes | Yes after each event | Depends on implementation |
| Durable delivery history | Built in | Built in through event journal | Must be added |
| Pane independence | Yes | Yes | Usually |
| Implementation complexity | High | Medium | Low initially, medium in production |
| Portable contract | Strong | Strong | Weak |
| Recommended role | General messaging | Primary plan coordination | Local optimization |

## Recommended target architecture

Use a hybrid of Option 2 and narrowly scoped parts of Options 1 and 3.

```text
                                  human / remote app
                                         |
                                         v
                              +-----------------------+
                              | Claude Code TUI       |
                              | owns the plan         |
                              +----------+------------+
                                         |
                              phase-start / phase-await
                                         |
                                         v
                         +-------------------------------+
                         | deterministic phase controller|
                         | event journal + await tokens  |
                         +-----------+-------------------+
                                     |
                  +------------------+------------------+
                  |                                     |
                  v                                     v
       +------------------------+          +--------------------------+
       | phase leader           |          | human escalation        |
       | any configured harness |          | Herdr/system notification|
       +-----------+------------+          +--------------------------+
                   |
              UpAgent request
                   |
                   v
       +-------------------------+
       | Python Recruiter Hub    |
       | contracts and leases    |
       +------------+------------+
                    |
                    v
       +-------------------------+
       | Dedicated Account Mgr   |
       | preferably Pi           |
       +------------+------------+
                    |
        native child when useful,
        external worker otherwise
                    |
                    v
       +-------------------------+
       | UpAgent worker          |
       | any configured harness  |
       +-------------------------+
```

### Why Claude Code remains the TUI

The TUI is the human-facing owner. Keeping it in Claude Code preserves remote application access.
The TUI does not need every descendant to use Claude Code. Its only required integration is the
portable phase controller tool contract.

### Why Pi is a strong Account Manager host

Pi exposes extension APIs that can queue steering or follow-up messages while an agent is active.
A Pi extension can also own a child process, subscribe to its output, persist state, and inject a
message into the manager session without relying on terminal input. That makes Pi suitable for the
Dedicated Account Manager even when the actual worker uses another harness.

The process supervisor must remain deterministic. The LLM manager interprets misconfiguration,
ambiguous health, and requester intent; it does not implement PID tracking, timeouts, atomic writes,
or lease fencing in natural language.

### Why the plan watchdog becomes optional

The phase controller already has enough evidence to report:

- phase completion;
- an explicit blocked result;
- leader process disappearance;
- missing heartbeats;
- descendant lifecycle warnings;
- timeout boundaries; and
- contradictions between a terminal result and live process state.

An LLM watchdog is still useful when the evidence requires interpretation, such as deciding whether
a pane appears productively active or repetitively stuck. It should publish an advisory event into
the same journal. It should not be the component that wakes the TUI.

## Detailed `phase-await` contract

### Command shape

The final interface may be a CLI, MCP tool, or harness-native tool. The semantic contract should be
the same:

```text
upagent-phase-await \
  --receipt /absolute/path/to/control/phase-start.json \
  --after <sequence-number> \
  --timeout-ms <bounded-duration>
```

The operation returns exactly one event. The caller records its sequence number and invokes await
again after handling nonterminal events.

### Event envelope

```json
{
  "schema_version": 1,
  "event_id": "evt-opaque-id",
  "sequence": 17,
  "occurred_at": "2026-01-01T12:00:00Z",
  "run_id": "example-run",
  "phase_id": "phase-0",
  "pass": 1,
  "kind": "blocked",
  "severity": "attention",
  "terminal": false,
  "source": {
    "component": "phase-controller",
    "address": "logical-address-or-null"
  },
  "subject": {
    "role": "phase-leader",
    "address": "logical-address",
    "pane_id": "current-pane-id-or-null"
  },
  "summary": "Phase leader requires an owner decision before merge-back.",
  "evidence": [
    {
      "kind": "durable-file",
      "path": "/absolute/path/to/evidence.json",
      "detail": "Explicit blocked state"
    }
  ],
  "requested_action": "inspect-and-decide",
  "dedupe_key": "phase-0:pass-1:leader-blocked:merge-back",
  "ack_required": true
}
```

### Required event kinds

| Kind | Terminal | Meaning |
|---|---:|---|
| `startup-ready` | No | Leader passed mechanical startup checks. |
| `startup-degraded` | No | Leader runs, but optional observability is unavailable. |
| `progress` | No | Durable phase progress changed materially. Usually not returned unless requested. |
| `advisory` | No | An observer found evidence worth inspecting. |
| `needs-input` | No | The leader explicitly requires a requester or human decision. |
| `blocked` | No | Work cannot currently advance. |
| `worker-warning` | No | A descendant request published an actionable warning. |
| `leader-missing` | No | The recorded leader cannot be resolved or its process exited unexpectedly. |
| `soft-timeout` | No | A configured review boundary was reached; the owner may extend or cancel. |
| `hard-timeout` | Yes | The non-extendable limit was reached and enforced. |
| `completed` | Yes | A valid authoritative phase result was accepted. |
| `failed` | Yes | The phase ended in a validated failure state. |
| `cancelled` | Yes | The authorized owner cancelled the phase. |
| `await-heartbeat` | No | Bounded await expired without a material state change; safe to await again. |

### Why an await heartbeat exists

A single extremely long foreground tool call can be fragile across terminals, remote applications,
and harness timeouts. The await operation should therefore be bounded. On a quiet but healthy
phase, it returns `await-heartbeat` with a compact health summary. The TUI immediately invokes await
again without narrating routine heartbeats to the human.

This is low-frequency bounded waiting, not wasteful pane polling. The controller should primarily
use event subscriptions and filesystem notifications, with a slow reconciliation pass as recovery.

### Acknowledgement

Every actionable event has an explicit acknowledgement state:

```text
published -> returned-to-owner -> acknowledged -> resolved
```

The TUI acknowledges an event only after it has parsed the tool result. Resolution occurs after the
requested action is complete or the event is superseded by new durable evidence.

An unacknowledged urgent event is returned again after reconnect or timeout. It is not discarded
because one delivery attempt succeeded at the socket layer.

### Commands back to the phase

The TUI sends typed commands rather than conversational pane text:

```json
{
  "schema_version": 1,
  "command_id": "cmd-opaque-id",
  "in_response_to": "evt-opaque-id",
  "run_id": "example-run",
  "phase_id": "phase-0",
  "action": "continue",
  "detail": {
    "instruction": "Re-check the merge target after the unrelated working-tree change is resolved."
  },
  "issued_by": "tui-logical-address"
}
```

Allowed actions should be a closed set, for example:

- `continue`
- `provide-input`
- `inspect`
- `extend-soft-timeout`
- `retry-startup`
- `cancel`
- `acknowledge-only`

The controller validates authority before delivering the command.

## Phase controller state machine

```text
                  +----------+
                  | starting |
                  +----+-----+
                       |
              startup receipt valid
                       |
                       v
                  +---------+
       +--------->| running |<------------------+
       |          +----+----+                   |
       |               |                        |
       |      actionable condition              |
       |               |                        |
       |               v                        |
       |          +-----------+                 |
       |          | attention |-- owner action -+
       |          +-----+-----+
       |                |
       |        terminal evidence
       |                |
       |                v
       |      +---------------------+
       +------| recovery/retry      |
              +----------+----------+
                         |
                         v
        +-----------+  +--------+  +-----------+
        | completed |  | failed |  | cancelled |
        +-----------+  +--------+  +-----------+
```

Each transition is journaled atomically with:

- previous and next state;
- triggering evidence;
- responsible owner;
- correlation identifiers;
- timestamps;
- retry or generation number; and
- whether an acknowledgement is outstanding.

The state machine must recover from process restart by replaying the journal and reconciling live
state. It must never rely solely on in-memory timers.

## Dedicated Account Manager and worker ownership

### Recruiter responsibilities

The Python Recruiter Hub should:

- validate request syntax and roster references;
- allocate a stable request identity;
- acquire and fence a lease;
- create the Dedicated Account Manager deterministically;
- validate the manager process, harness identity, working directory, and logical address;
- persist startup evidence before reporting success;
- expose a durable requester event stream;
- enforce hard lifecycle limits;
- recover or close abandoned leases; and
- reject unauthorized cancellation.

The Recruiter should not interpret vague work requests. It hands ambiguous or invalid semantic
requests to the Dedicated Account Manager, which can converse with the requester.

### Account Manager responsibilities

The Dedicated Account Manager should:

- negotiate the worker profile with the requester;
- explain missing models, unsupported effort levels, or contradictory requirements;
- ask for a corrected prompt or configuration;
- request deterministic worker creation from its supervisor extension;
- validate mechanical startup evidence;
- interpret ambiguous health evidence when asked;
- report warnings and completion to the requester;
- ask the requester before a soft-timeout cancellation; and
- remain alive until the worker lifecycle is terminal and its final notification is acknowledged.

### Native child path

When the manager and worker use a compatible Pi configuration:

```text
Pi Account Manager session
    |
    `-- supervisor extension
            |
            +-- spawn child process/sub-agent
            +-- capture PID/session identity
            +-- verify prompt, cwd, model, and agent persona
            +-- stream lifecycle events
            +-- persist exit/result evidence
            `-- pi.sendMessage(...) to wake manager
```

The LLM manager must not call a blocking `run-subagent-until-exit` tool and become unavailable.
Instead, the supervisor returns a startup receipt immediately and owns the child asynchronously.
It injects a steering or follow-up message when a decision is required.

### External worker path

When the worker uses Claude Code, Codex, or another harness, the same manager contract remains in
force:

```text
Pi Account Manager
    |
    `-- deterministic supervisor
            |
            `-- external harness adapter -> worker process
```

The external child is not called a native sub-agent in the lifecycle contract. It is a supervised
worker. This distinction prevents assumptions about context sharing, message delivery, or teardown.

### Parent lifetime is necessary but insufficient

The manager must outlive its worker, but simply blocking manager shutdown while a child PID exists
does not provide reliable supervision. The manager also needs:

- a child startup handshake;
- periodic or event-driven liveness evidence;
- exit and signal capture;
- result validation;
- orphan adoption after supervisor restart;
- bounded shutdown;
- acknowledgement of the final requester notification; and
- a terminal manager result.

## Monitoring without a required watchdog LLM

### Mechanical evidence

Deterministic code can establish:

- whether a process exists;
- whether a Herdr agent address resolves;
- whether the expected harness and persona were detected;
- whether the working directory matches;
- whether a result file exists and validates;
- whether a lease is current;
- whether a timeout has elapsed;
- whether output or durable state changed;
- whether a child exited; and
- whether a notification was acknowledged.

### Ambiguous evidence

An LLM can help interpret:

- whether recent pane activity appears repetitive or productive;
- whether output suggests the worker is waiting for an answer;
- whether the work diverged from the request;
- whether contradictory evidence is probably benign; and
- how to explain a misconfiguration to the requester.

The LLM returns an assessment, not an action:

```json
{
  "assessment": "possibly-stalled",
  "confidence": 0.72,
  "evidence": ["..."],
  "recommended_action": "ask-requester-to-inspect",
  "destructive_authority": false
}
```

The deterministic controller records this as an `advisory` event. The normal await or mailbox path
delivers it.

### Suggested monitoring schedule

- Use event subscriptions for process exit, Herdr status transitions, and output matches.
- Use filesystem notifications for durable lifecycle files where available.
- Reconcile state slowly, such as every few minutes, to recover missed events.
- Invoke an LLM assessment only after a material transition or a sustained contradiction.
- Never invoke a new LLM check repeatedly while evidence is unchanged.

## Harness adapters

### Claude Code TUI

Preferred integration:

- The TUI calls `phase-start` and `phase-await` as normal tools.
- The await result becomes the next input to the Claude Code session.
- No Claude-specific hook is required.
- The session remains usable through Claude Code's remote application.

Optional future integration:

- Host the TUI through the Claude Agent SDK or streaming-input mode.
- Provide a native adapter for queued messages.
- Preserve the same event envelope and acknowledgement semantics.

Do not assume that sending terminal input to an interactive Claude Code pane is equivalent to SDK
message delivery.

### Pi Account Manager

Preferred integration:

- A Pi extension listens to the local supervisor or Unix socket.
- It calls `pi.sendMessage` or `pi.sendUserMessage` with the appropriate delivery mode.
- Messages can be queued as steering or follow-up work while the agent is active.
- The extension owns child process state outside the LLM context.

### Codex

Use a programmatic message interface when the active Codex surface exposes one. Otherwise, treat
Codex as an external supervised worker with durable result and lifecycle files. Do not invent
terminal-input guarantees that the harness does not provide.

### Generic Herdr fallback

Herdr provides event subscriptions, pane inspection, agent status, and terminal input. The fallback
adapter may wait for a safe prompt boundary and then submit one prompt atomically. It must:

- retain the message durably until acknowledged;
- resolve the logical address to a current pane immediately before delivery;
- never regard socket acceptance as agent acknowledgement;
- avoid injecting while a foreground program owns stdin;
- retry with backoff; and
- escalate to a human notification when urgent delivery remains unacknowledged.

## Human escalation

Human notification is the final safety net, not the primary agent transport.

Escalate when:

- an urgent event remains unacknowledged past its delivery objective;
- the TUI cannot be resolved;
- the phase owner disappeared;
- a hard timeout was enforced;
- repeated recovery attempts failed; or
- authority is ambiguous and a destructive operation is pending.

The notification should be short:

```text
Herdr plan needs attention
Phase phase-0 is blocked before merge-back. Open the TUI for details.
```

Detailed evidence remains in the event journal; it should not be dumped into a desktop toast.

## Failure scenarios and expected behavior

### Phase leader reports a dirty merge target

1. Leader writes a typed `needs-input` or `blocked` record.
2. Controller journals the event.
3. Active `phase-await` returns the event to the TUI.
4. TUI explains the issue briefly and requests human direction if required.
5. Human or TUI sends a typed command.
6. Controller delivers the command and the TUI awaits again.

No watchdog is required.

### Phase leader crashes without writing a result

1. Process exit or missing Herdr agent triggers reconciliation.
2. Controller publishes `leader-missing` with process and pane evidence.
3. `phase-await` returns to the TUI.
4. TUI chooses an authorized retry, recovery, or cancellation path.

### TUI disconnects while the phase continues

1. Controller continues recording events durably.
2. No phase is destroyed solely because the UI disconnected.
3. On resume, the TUI calls `phase-await --after <last-sequence>`.
4. The oldest unacknowledged actionable event is returned.
5. Urgent unacknowledged events may also generate a human notification.

### Account Manager crashes while its worker is alive

1. Supervisor retains the worker lease and process identity.
2. Recruiter starts or adopts a replacement manager generation according to policy.
3. Replacement receives the complete durable lifecycle snapshot.
4. Worker is not duplicated unless fencing proves the previous lease invalid.

### Worker finishes while manager is busy

1. Supervisor validates and promotes the worker result.
2. Supervisor emits a native Pi follow-up message or durable manager event.
3. Manager handles it at the next safe turn boundary.
4. Final requester notification is persisted and acknowledged.
5. Manager becomes terminal only afterward.

### Watchdog or optional LLM observer exits early

1. Controller records reduced observability.
2. The phase continues.
3. Deterministic completion, block, timeout, and crash detection remain active.
4. The TUI is notified only if the loss materially changes risk or violates a required route.

### A notification is written but not delivered

1. Message remains `queued` or `presented`, not `acknowledged`.
2. Delivery is retried or the active await returns it.
3. Urgent messages escalate after a configured objective.
4. Nothing records success merely because `pane run` returned zero.

### Stale pane address

1. Logical identity remains stable.
2. Adapter resolves the current pane immediately before presentation.
3. If resolution fails, delivery remains pending.
4. The controller reports `target-unavailable`; it does not send to a guessed pane.

### Soft timeout

1. Controller publishes `soft-timeout` before taking destructive action.
2. Requester may extend within a bounded decision window.
3. No reply transitions according to the declared request policy.
4. Every extension records its new deadline and authorizing identity.

### Hard timeout

1. Controller publishes impending hard-timeout notice when possible.
2. At the fixed boundary, deterministic code fences the lease and terminates according to policy.
3. Controller records the exact enforcement evidence.
4. Owner and human receive a terminal event.

## Authority and safety rules

| Actor | May observe | May advise | May continue/extend | May cancel | May force terminate |
|---|---:|---:|---:|---:|---:|
| Human | Yes | Yes | Yes | Yes | Yes |
| TUI | Plan and owned phases | Yes | Owned plan/phase | Owned plan/phase | Only through policy |
| Phase leader | Its phase/workers | Yes | Its phase/workers | Its requested workers | Only through Recruiter |
| Account Manager | One worker lifecycle | Yes | With requester policy | With requester authority | At declared hard limit |
| Recruiter Hub | All managed leases | Mechanical warnings | Enforces recorded decisions | Enforces authorized request | At declared hard limit |
| Watchdog/observer | Assigned scope | Yes | No | No | No |

Destructive operations require:

- the correct owner identity or an explicit hard-limit policy;
- a current generation and lease token;
- an idempotency key;
- a recorded reason; and
- a terminal receipt.

## Persistence layout

The exact paths may evolve, but the separation should remain clear:

```text
<run-root>/
`-- phases/
    `-- <phase-id>/
        `-- pass-<n>/
            `-- control/
                +-- phase-start.json
                +-- state.json
                +-- events/
                |   +-- 00000001.json
                |   `-- 00000002.json
                +-- acknowledgements/
                |   `-- <event-id>.json
                +-- commands/
                |   `-- <command-id>.json
                `-- awaiters/
                    `-- <token>.json
```

UpAgent request state remains under the Hub's state directory, with separate manager, worker,
result, event, and requester-message areas.

All writes that establish state transitions must use write-to-temporary plus atomic rename. Readers
must validate schemas and identities before accepting a file.

## Reliability requirements

### Idempotency

- Repeating `phase-start` with the same identity returns the existing valid receipt.
- Repeating `phase-await` after a lost response returns the same unacknowledged event.
- Repeating acknowledgement is harmless.
- Repeating an owner command with the same command ID does not execute twice.
- Worker startup uses generation and lease fencing.

### Ordering

- Events have a monotonic sequence within one phase pass.
- A terminal event cannot be superseded by a nonterminal event from an older generation.
- Commands reference the event they answer.
- Cross-process timestamps are diagnostic; sequence and generation establish order.

### Recovery

- Every daemon or controller can rebuild its state from durable files plus live process discovery.
- A restart never changes a successful event to pending.
- A stale worker cannot promote a result after losing its lease.
- An await client can reconnect from its last acknowledged sequence.

### Backpressure

- Progress events may be coalesced.
- Actionable events may not be dropped.
- Repeated identical advisories share a dedupe key and update evidence rather than creating turns.
- An LLM is not invoked merely to relay unchanged state.

### Observability

Each lifecycle should expose:

- current state;
- owner and logical address;
- current generation and lease;
- last durable progress time;
- last live observation time;
- pending actionable event count;
- oldest unacknowledged event age;
- current soft and hard deadlines; and
- terminal result or recovery reason.

## Implementation plan

### Stage 1: define contracts without changing runtime behavior

1. Add schemas or validators for phase events, acknowledgements, commands, and await cursors.
2. Define the closed event-kind and command-action sets.
3. Add atomic event-journal helpers.
4. Add unit tests for validation, ordering, deduplication, and recovery.
5. Document logical identity separately from Herdr pane identity.

### Stage 2: implement `phase-await`

1. Read and validate `phase-start.json`.
2. Subscribe to relevant Herdr status and process events.
3. Watch durable phase result and control paths.
4. Follow descendant UpAgent requester outboxes.
5. Reconcile on a slow bounded interval.
6. Return the oldest unacknowledged actionable event.
7. Return bounded `await-heartbeat` events during healthy silence.
8. Add reconnect and cancellation handling.

### Stage 3: change TUI orchestration

1. After `phase-start`, call `phase-await` rather than waiting for `done` or pane output.
2. Handle every event kind explicitly.
3. Acknowledge only after parsing the event.
4. Re-enter await after nonterminal events.
5. Produce a concise human summary for terminal success.
6. Ask the human only when the returned event actually requires human authority or information.

### Stage 4: demote the plan watchdog

1. Remove it from the required startup transaction.
2. Preserve optional observer configuration for ambiguous-state assessment.
3. Route observer findings through the phase event journal.
4. Verify observer loss cannot block phase startup or completion.
5. Remove prompt language that assigns transport reliability to the observer.

### Stage 5: add Pi manager supervision

1. Implement a Pi extension or companion supervisor with a local socket.
2. Spawn workers asynchronously and return a startup receipt immediately.
3. Track child identity, PID, harness session, cwd, model, persona, and lease.
4. Emit child lifecycle events.
5. Wake the manager with Pi's native message API.
6. Recover children after supervisor or manager restart.
7. Keep an external worker adapter for non-Pi workers.

### Stage 6: strengthen requester delivery

1. Replace the 250 ms one-shot requester delivery attempt with durable delivery state.
2. Add acknowledgement and retry.
3. Resolve panes only at presentation time.
4. Add human escalation for urgent unacknowledged messages.
5. Keep Herdr terminal injection as a safe-boundary fallback, not the primary success criterion.

### Stage 7: remove obsolete mechanisms

Only after the new path passes end-to-end fault tests:

1. Remove required plan-watchdog startup gates.
2. Remove stale-output completion matching.
3. Remove direct long foreground waits that listen only for success.
4. Remove any claim that `pane run` success proves agent receipt.
5. Retain compatibility readers long enough to finish existing runs safely.

## Testing strategy

### Contract tests

- Reject missing or unknown event kinds.
- Reject mismatched run, phase, pass, generation, or owner identity.
- Reject commands from unauthorized owners.
- Reject terminal regression.
- Accept duplicate acknowledgement idempotently.
- Preserve actionable events during deduplication.

### Deterministic integration tests

- Phase completes while TUI is inside `phase-await`; TUI receives `completed`.
- Leader writes `blocked`; TUI receives it without pane injection.
- Leader exits without a result; TUI receives `leader-missing`.
- Descendant worker startup fails; phase leader and TUI receive the correct scoped event.
- Await process restarts after event publication; the event is returned again.
- TUI disconnects and resumes from its last sequence.
- Optional watchdog never starts; phase still completes normally.
- Optional watchdog exits early; primary coordination still works.
- A stale pane ID is never used as a logical identity.
- A terminal result and live pane contradiction produces one deduplicated advisory.

### Fault-injection tests

- Kill the phase controller between temporary write and rename.
- Kill the controller after publishing but before returning an event.
- Kill the TUI after receiving but before acknowledging.
- Kill the Account Manager while its worker runs.
- Kill the worker after startup but before result creation.
- Corrupt a partial result file.
- Reuse an old generation's result.
- Delay Herdr status events.
- Close and recreate panes so public IDs change.
- Make the target permanently busy.
- Make the filesystem notification stream drop events and verify reconciliation recovers.

### End-to-end scenarios

1. Claude Code TUI, Claude Code phase leader, Pi manager, Codex worker.
2. Claude Code TUI, Pi phase leader, Pi manager, Pi native child.
3. Claude Code TUI disconnect and remote resume during a phase.
4. Dirty merge target requiring human input.
5. Soft timeout extended by requester.
6. Hard timeout with no requester response.
7. Worker completes while manager is busy.
8. Recruiter restarts with active leases.

### Acceptance criteria

- No actionable phase condition can remain known to the controller but invisible to an active
  awaiting TUI.
- Completion does not depend on pane scrollback matching.
- No watcher is required for a healthy run to finish.
- A blocked phase returns control to the TUI within a bounded delivery objective.
- Restarting any deterministic controller does not duplicate a worker or lose an event.
- Cross-harness workers remain supported.
- The Claude Code TUI remains usable through its remote application.
- No destructive action is taken solely from an LLM watchdog assessment.
- Every terminal lifecycle has a durable, validated receipt.

## Migration and compatibility

Existing active runs may still depend on watchdog receipts and old wait behavior. Do not replace
their runtime files in place.

Recommended migration:

1. Introduce versioned event and await contracts.
2. Let new runs opt into `coordination_version: 2`.
3. Keep old runs on their existing controller and watcher behavior.
4. Run both paths through focused test fixtures.
5. Make version 2 the default only after fault-injection scenarios pass.
6. Remove version 1 after no active run or retained fixture requires it.

The implementation must avoid running a global configuration update while a destination repository
is actively using an older copied runtime. Source changes and destination rollout are separate
operations.

## Open design decisions

The implementing team should decide and record:

1. Whether `phase-await` is a CLI command, an MCP tool, or both.
2. The maximum quiet await duration before returning an `await-heartbeat`.
3. Which Herdr events are authoritative enough to create actionable events directly.
4. How a phase leader publishes typed `needs-input` without depending on its pane.
5. Whether phase commands use files, a Unix socket, or both.
6. How long unacknowledged messages live after a terminal run.
7. Which urgent events trigger Herdr/system notifications.
8. Whether the Pi supervisor is embedded as an extension or runs as a companion process.
9. Whether Pi-native children remain visible as Herdr panes or only as logical child records.
10. Which worker harnesses support native steering and which are result-only adapters.
11. How a TUI authenticates its authority after reconnect or session resume.
12. Whether a plan-level journal aggregates phase events or only stores references.

## Review checklist

A design reviewer should challenge the proposal against these questions:

- Can the TUI be busy in a state where `phase-await` cannot return a tool result?
- Can a lost response be replayed without duplicating an action?
- Can a stale generation publish after recovery?
- Can an optional observer accidentally become a startup or completion gate?
- Can the Account Manager communicate while its child is working?
- Can an external child be adopted after the manager restarts?
- Does any path treat terminal input acceptance as message acknowledgement?
- Does any path use a mutable pane ID as durable identity?
- Can a non-owner terminate a worker?
- Is every hard timeout declared before work starts?
- Can a human see the important problem without reading a large transcript?
- Does the design remain useful when Claude Code, Pi, or Codex changes independently?

## References

- [Herdr socket API](https://herdr.dev/docs/socket-api/) documents long-lived event subscriptions,
  agent status changes, pane input, and user notifications.
- [Pi extension documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
  documents `pi.sendMessage`, steering and follow-up delivery, extension events, and the reference
  sub-agent extension.
- [Claude Code sub-agent documentation](https://code.claude.com/docs/en/sub-agents) describes
  foreground/background sub-agents and their parent-session behavior.
- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview) describes the
  programmatic Claude Code agent loop and session integration.
- [Claude Code agent view](https://code.claude.com/docs/en/agent-view) distinguishes independent
  background sessions from sub-agents that return results to one conversation.

## Final recommendation

Implement `phase-await` first. It directly fixes the observed plan stall without requiring a
general messaging platform or forcing every phase leader onto Claude Code.

Then make the Dedicated Account Manager a true asynchronous supervisor, preferably using Pi's
native extension messaging. Let it create a native Pi child when appropriate and an externally
supervised worker when a different harness is requested.

Keep the durable mailbox work as the foundation for recovery and eventual delivery. Add a full
cross-harness agent bus only when use cases require arbitrary unsolicited peer communication beyond
the plan and worker lifecycle boundaries.

The resulting rule is simple:

```text
deterministic code owns lifecycle and delivery
native harness features optimize local parent/child communication
LLMs interpret uncertainty and negotiate with owners
humans retain final authority for ambiguous destructive decisions
```
