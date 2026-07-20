# UpAgent Hub — the Recruiter

The UpAgent Hub is a universal lifecycle service for LLM workers. Its caller may be a phase
leader, TUI, or another framework. The always-up Python **Recruiter** persists the
request, uses Python-owned direct lifecycle by default, atomically launches and verifies the worker,
collects the result, and reports to the recorded requester. See [FUNDAMENTALS.md](FUNDAMENTALS.md)
for the authority model and use-case tree.

## Topology

```
Herdr session (default: single workspace)
└── ws: herdr                  services AND runs share one workspace as role tabs
    ├── tab: services          └── recruiter (deterministic Python Hub)
    └── tabs: control / workers / oversight   per-run panes

Herdr session (with `up --separate-workspaces`)
├── ws: <slug>                 TUI + leader + workers (+ opt-in managers / one-shot checkers)
└── ws: shared-services        always up, plan-agnostic
    └── recruiter               deterministic Python Hub
```

The Recruiter pane is a visible status surface, not a free-form command queue. Requests go directly
to the durable ledger. A narrow compatibility function accepts exactly `recruit <order-path>` and
forwards that opaque path to the normal verified request door; it never evaluates pane text. A
request's manager, worker, and short-lived checkers start beside the
`order.cockpit_pane` through atomic `herdr agent start` calls, making the whole dedicated lifecycle
visible in one workspace. `manager_placement.mode: shared` remains an explicit opt-in for callers
that truly want a peripheral manager. Pane placement is role-based: workers split right, while
managers and one-shot checkers split down, forming a mixed grid instead of a vertical strip. Every
watchdog targets 28% of its local horizontal split; managers and checkers target 20% of their local
vertical split. This bounded, best-effort resizing happens only after atomic agent startup and
ownership recording, so an unavailable layout control warns without blocking the worker. Every
pane is closed only by its fenced lease owner.

## LLM order intake (short leash)

Every submission door (`recruit`/`dispatch`/`request`) takes one path. Python owns transport; an
LLM owns interpretation:

1. Python preserves the exact submitted bytes, then launches one short-lived `intake-clerk` support
   role from the trusted Recruiter pane and a broker-owned scratch cwd. Every distinct submission
   goes through a clerk — canonical JSON, malformed JSON, prose, an incomplete object, unknown
   fields, and specialist-worded requests alike; a byte-identical resubmission of the same file with
   a clean prior attempt is idempotent and reuses that attempt's validated response rather than
   launching again. Python cannot end a request because of its schema, wording, order id, agent
   name, or intent; there is no canonical-order bypass and no Python form repair.
2. The clerk flattens or renames a JSON envelope, returns an already-canonical order unchanged, or
   explains why the submission is not executable. Prose labels are never execution authority: every
   required and authority-bearing value must come from an unambiguous JSON object through a known
   key or alias.
3. The interpreted order passes Python provenance checks and the unchanged strict contract. Those
   findings go back to the same clerk for a bounded number of correction rounds rather than
   becoming a Python refusal; each round gets its own reuse identity so a correction can never be
   answered by the response it was sent to correct. If a model wraps its one JSON object in exactly
   one whole-response Markdown JSON fence, Python removes that fence as form normalization; prose
   outside the fence and multiple objects remain invalid.
4. Python then supplies only its own bookkeeping defaults — identifiers, a result path, missing
   phase/stage bookkeeping, path anchoring — and hands the order to the unchanged worker lifecycle.

Every submission ends in exactly one visible outcome with durable evidence and its own exit code:

| Outcome | Marker | Exit | Authored by |
|---|---|---|---|
| accepted | `REQUEST_ACCEPTED` / `ORDER_RECEIPT` / `ORDER … DONE` | `0` | — |
| blocked | `REQUEST_BLOCKED` | `3` | intake clerk |
| intake clerk failure | `REQUEST_INTAKE_FAILED` | `4` | recruiter |
| infrastructure failure | `REQUEST_INFRASTRUCTURE_FAILED` | `5` | recruiter |

A non-executable request is blocked in the clerk's own words, recorded with `authored_by` so a
reader can tell a judgement from a machine fault. An unavailable clerk, an exhausted correction
budget, and a fault in the Hub's own machinery are each reported as themselves — never a hang, a
crash, or a guessed order.

The complete paper trail is written before the caller's order is atomically replaced:
`<order>.raw-submitted` contains the exact bytes, `.interpreted.json` the attempted canonical
interpretation, `.intake.json` the mode and changes, `.validation.json` the final gate, and
`.refusal.json` the reason when no safe interpretation exists. No paper trail means no execution.
The default clerk receives no filesystem, shell, web, or MCP tools. A trusted wrapper shell-quotes
the broker brief/output paths, feeds the brief to `claude --print --tools ""`, and atomically
captures the clerk's one-object stdout. `upagent.yaml` is trusted executable configuration: a
roster owner can override the shipped command and thereby broaden its capabilities. Python does
not claim to enforce no-tools on an arbitrary trusted override, so every override must be audited.
The clerk timeout is capped at `300000` ms even for trusted overrides. Persona instructions are
guidance; Python's structural provenance, typed-response parser, and strict order contract are the
enforcement boundary.

Each clerk attempt uses an unpredictable broker-created directory with mode `0700`. A pre-launch
journal records an unguessable agent name, lease token, owner PID and process start time before
Herdr creates a pane. Reuse requires a hash-matching response, ownership journal, and secure index.
Cleanup resolves that unique agent name and rechecks agent/process/cwd identity; it never closes a
stale pane id by itself. The reconciler can recover a crash before the pane id was journaled. A
not-found lookup while that launch is uncertain keeps the tiny journal open for later sweeps,
because the in-flight Herdr start may still publish the named agent. Reconciliation also rejects
symlinked or escaped attempt metadata. Interpreted orders still face all submission checks,
including the laundering guard that refuses an unsafe consult-shaped order id rather than
regenerating it into a valid-looking one.

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
the token to a per-run hashed 0600 file under `$HERDR_RUN_TOKEN_DIR` or the default same-user
0700 runtime token directory, then pass only `HERDR_RUN_OWNER_TOKEN_FILE` to the TUI and heartbeat process; `HERDR_RUN_OWNER_TOKEN` remains a
compatibility fallback. Recovery is explicit: use `just herdr-run-session-snapshot <run-dir>`,
then `just herdr-run-session-reconcile <run-dir>`, and only then start with stale takeover when
the reconciliation receipt proves the recorded owner is stale.

## The order → result contract (`contracts.py`)

Durable files are the source of truth; terminal text is display-only.

- The requester writes `order.json`, including a globally scoped `request_id` and
  `requester: {id, kind, address}`, then runs `just upagent-request <order.json>`. It returns only
  after both the manager and worker have verified startup, with their current addresses and a
  per-generation control token.
- The Recruiter validates and persists a copy-on-write request under
  `$UPAGENT_HUB_DIR` (default `~/.local/state/herdr/upagent-hub`). One runner atomically claims
  `active/requests/<scoped-request-id>/`, writes an authoritative generation lease, starts the
  manager, then launches the requested harness. Health means expected foreground process,
  detected harness, cwd, and typed manager assessment—not merely pane creation.
- Before launch, Python checks absolute paths, required model/effort values, harness-native model
  shape, executable presence, and (for Claude `--agent` routes) the actual persona file. Those
  facts are given to the manager. A bad request is explained to the requester and terminalized
  without ever creating a worker, even if the LLM mistakenly recommends approval.
- The Recruiter appends a final lease-specific delivery contract containing one private result
  path and the literal order id. The worker does the stage and writes exactly that one private
  `result.json` (verdict `passed|failed|blocked`, a `revisit` list of stage-ids on failure,
  and a `full_log` pointer to its harness transcript) plus its `compacted.md` and handoff.
- `just upagent-await <order.json>` waits in Python for a decision point or completion; no LLM
  loops over files. At inactivity checkpoints, a fresh cheap checker interprets one bounded pane
  and process snapshot, reports to the manager/requester, and exits.
- At a work cap, `upagent-await` returns `REQUESTER_DECISION_REQUIRED`. The requester may run
  `just upagent-respond <order> <control-token> <nonce> extend <milliseconds>` or `... cancel 0`.
  Without an answer during `management.requester_grace_ms`, the Hub performs the declared hard
  stop. Managers/checkers can recommend actions but cannot execute them.
- The job owner validates the result, closes only its recorded worker/manager panes, verifies
  absence, then publishes the public result plus `receipt.json`. `ORDER_RECEIPT` wakes the caller.
  If anything goes wrong it publishes a fail-loud `blocked` result/receipt. Publication also keeps
  the hub's own `published-result.json` and names it in the receipt, so a terminal record survives
  the pruning of the run tree that owns `result_path`: a later dispatch republishes that copy
  instead of failing in the strict result loader, and refuses with the evidence paths when no copy
  survives. The lease records the
  requester, manager, runner, Recruiter, worker, workspace, token, generation, and expiry; a
  small Python supervisor safely
  reconciles dead/expired owners. A request's immutable `request.json` and events are durable; its
  `state/latest.json` is the copy-on-write current view. The lease is authoritative; retained
  `active/by-expiry` entries are merely reaping indexes and must be token-checked before reuse.

`route.yaml` is authoritative for which harness/model/agent runs each worker. The Recruiter only
holds mechanical launch templates, separate phase-controller templates, and configurable
management-role commands in `upagent.yaml`; it never silently substitutes a requested worker.

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
ALWAYS written — a failure answer names the reason — so a caller's bounded wait resolves to a
legible outcome instead of a missing file.

**Who can be asked is a different roster from how to launch.** `upagent.yaml` is keyed by
harness and REPLACES across files, because half a launch command from each file is dangerous.
`specialists.yaml` is keyed by persona and MERGES: the kit base beside `recruiter.py` under this
repo's own `.shared-llm/this_repo/extensions/common/upagent/specialists.yaml` (template:
`specialists.yml.sample`). A destination that overrides one specialist keeps every other one the
kit ships — the opposite rule would shrink the phone book with no error, and a short phone book
reads to a worker as "no specialist owns this area".

## The roster (`upagent.yaml`) — how each harness launches

The launch templates are pre-hardened; leaders and TUIs never hand-craft a worker command.
Every template substitutes `{order_id}` / `{model}` / `{agent}` / `{effort}` / `{cwd}` /
`{instructions_path}` / the lease-private `{result_path}` (`{effort}` is resolved by the leader
from the route profile, `medium` when the profile omits it). Four properties every template
must keep (see `upagent.yaml.example` for the full rationale):

1. **Non-interactive.** Workers run unattended in panes — every template bypasses
   trust/permission prompts (`claude --dangerously-skip-permissions`,
   `codex exec --dangerously-bypass-approvals-and-sandbox`, `pi --approve`) or the hire hangs
   until the Recruiter's timeout.
2. **Harness-native model ids.** claude takes an alias or full name (plus `--effort`); Codex
   takes a bare model id such as `gpt-5.6-sol` plus `model_reasoning_effort`; pi takes
   `provider/id[:thinking]`, so pi's effort rides inside the model string. Codex has no
   `--agent` flag: its persona comes from the stage instructions.
3. **Codex completion uses the generic fenced monitor.** Codex does not reliably report a
   terminal Herdr agent-status transition. The Recruiter's token-scoped staging-result monitor
   validates and finalizes its result exactly like every other harness; there is no separate
   public-result polling path.
4. **pi runs insulated.** `--no-extensions` plus an explicit
   `-e $HOME/.pi/agent/extensions/herdr-agent-state.ts`. Discovery off means a broken
   globally-installed extension can never brick automation; the explicit `-e` keeps Herdr's
   pi integration loaded, which is what reports pane agent-status — without it,
   `herdr wait agent-status --status done` never fires and every pi hire times out to
   blocked. Workers are still full visible TUIs in panes (headless `-p` is never used);
   interactive pi sessions keep the whole extension set.

The `phase_leaders:` map is deliberately separate from `harnesses:`. A phase leader is launched
once with a controller assignment and held behind the phase-start gate; a stage worker receives a
lease-private result contract. `upagent-phase-start` fails before creating a pane when the selected
harness has no phase-leader template.

## Adopt it

1. Copy this whole directory to the same relative path (public tool modules land under a
   destination's `.shared-llm/public/extensions/common/upagent/` via `just update`).
2. Copy `upagent.yaml.example` → `upagent.yaml` and adapt the launch templates to your
   harnesses. In a split destination the filled roster is repo-owned (`this_repo`).
3. Add `import '.shared-llm/public/extensions/common/upagent/justfile'` to the root justfile.

## Tests

`just test` covers contracts, typed LLM responses, mandatory clerk intake and its bounded
correction rounds, the four standardized submission outcomes, exact audit
artifacts, request mailboxes, identity/lease fencing, startup health, timeout authority, roster
resolution, and cleanup behavior. The socket-driving path is also exercised in a live Herdr
session before release.
