# UpAgent Hub — the Recruiter

The UpAgent Hub is a universal lifecycle service for LLM workers. Its caller may be a phase
leader, TUI, or another framework. The always-up Python **Recruiter** persists the
request, uses Python-owned direct lifecycle by default, atomically launches and verifies the worker,
collects the result, and reports to the recorded requester. See [FUNDAMENTALS.md](FUNDAMENTALS.md)
for the authority model and use-case tree.

## Canonical machine-local Hub

Every imported recipe invokes `client.py`, a transport-only client. From a main checkout or any
linked git worktree it resolves git's common directory, then connects to the same repository socket
at `/tmp/.upagent/hubs/<repo-id>/hub.sock`; `UPAGENT_SOCKET` is the absolute-path override. Only
`up` may start the canonical `hub.py`, and it starts the copy from the main checkout rather than the
caller's worktree. The Hub holds `hub.lock` for its full lifetime, publishes `identity.json`, and
performs a version-and-runtime-fingerprint handshake before accepting a command. The fingerprint
covers both the wire schema and the Python modules cached by the resident Hub, so `up` safely
replaces an idle Hub after deployment instead of continuing to run stale imports. Protocol v5
carries only the strictly validated Herdr caller fields required by controller operations
(`HERDR_ENV`, `HERDR_PANE_ID`, `HERDR_SOCKET_PATH`, `HERDR_SESSION`, and the private absolute
`RUNNER_OWNER_TOKEN_FILE` path when set); arbitrary environment variables and raw secrets never
cross the wire. Request-local stdin is bounded and accepted only for run-lifecycle `guard` or
`cleanup` with exactly one `--token-stdin`; it never enters Hub identity, status, logs, or the
process stdin. An incompatible v5 resident is restarted by `up` only after its live handshake and
canonical executable/PID identity verify. The resident then performs one server-side idle-stop
transaction: it closes launch admission, waits for already-admitted commands, checks both active
ledger claims and registered runner threads, and stops itself only when both are empty. Otherwise it
resumes admission and reports the activity; no client-side signal or separate resume command can
race the decision. Pre-v5 residents require a one-time explicit restart because they cannot provide
that atomic handoff. The Hub removes its own Herdr fields before creating the request context.
It imports canonical controllers under a narrow module-registration lock, then runs commands
concurrently with request-local cwd, immutable environment views, argument parsing, and structured
output sinks. Blocking awaits therefore do not stall status, response, reconciliation, or
controller operations; usage/errors stay with the requesting client; and background-runner output
cannot enter another request's response. Job runners and the reconciler are Hub-owned daemon threads, not
detached mutating subprocesses. A Hub exit therefore ends every writer before the OS releases
the lifetime lock. Status includes
`hub_instance_id`, exact PID/start identity, protocol version and runtime fingerprint, canonical
Recruiter path, socket, ledger, and Herdr session.

The client imports no Recruiter code and has no ledger, reconciliation, lifecycle-dispatch, or
worker-runner implementation. Public, controller, and compatibility recipes all cross the socket.
Direct `recruiter.py` and `public_api.py` execution fails loud. Authority is not an environment
marker: the running PID, canonical paths, published identity, lock-file inode, and already-held lock
descriptor must all prove the live Hub process. `tui_controller.py` and `run_lifecycle.py` are also
canonical socket targets and never import a checkout-local Recruiter or inspect its ledger. The
socket is deliberately open to machine-local callers (mode `0666`):
there is no registration, API key, JWT, TLS, allowlist, or worktree approval layer.

Every worker, manager, checker, and rescue helper persists a token-fenced
`launching` journal before `herdr agent start`, records the exact returned pane as `created`, then
compare-and-swaps it to `started`. A lost fence routes through bounded exact-agent cleanup. If
cleanup cannot be proved immediately, the job thread publishes no terminal receipt: the standing
supervisor retains the active lease, reconciles the journaled unique agent name and exact pane, and
only then terminalizes with that cleanup evidence. A crash between pane creation and ledger
publication therefore cannot leave an unowned agent or a false `verified_absent` receipt.

## Topology

```
Herdr session (default: single workspace)
└── ws: upagent                services AND runs share one workspace as role tabs
    ├── tab: services          └── upagent (deterministic Python Hub status)
    └── tabs: control / workers / oversight   per-run panes

Herdr session (with `up --separate-workspaces`)
├── ws: <slug>                 TUI + leader + workers (+ opt-in managers / one-shot checkers)
└── ws: shared-services        always up, plan-agnostic
    └── upagent                 deterministic Python Hub status
```

The visible `upagent` pane is the Recruiter's status surface, not a free-form command queue. On
first bring-up after this naming change, UpAgent renames its former `herdr` workspace and
`recruiter` pane in place; unrelated human-created workspaces are never claimed. Requests go through
one variadic façade and the canonical socket:

```text
just upagent --help
just upagent up
just upagent status [--request ID] [--json]
just upagent get --request ID [--json]
just upagent lists --type offerings|specialists|workers [--status active|terminal|all] [--json]
just upagent request --type worker --offering ID --effort LEVEL --agent PERSONA \
  --prompt-file /absolute/brief.md [--cwd /absolute/worktree] [--wait] [--json]
just upagent request --type specialist --specialist NAME \
  --prompt-file /absolute/question.md [--cwd /absolute/worktree] [--wait] [--json]
just upagent request --file /absolute/request.json [--wait] [--json]
just upagent await --request ID [--notify-after-ms MS] [--json]
just upagent await-any --request ID [--request ID ...] [--cursor JSON] [--timeout-ms MS] [--json]
just upagent verify --request ID --offering ID --effort LEVEL --agent PERSONA [--wait] [--json]
just upagent respond --request ID --control-token TOKEN --nonce NONCE \
  --action extend|cancel --extension-ms MS [--json]
just upagent cancel --request ID --control-token-file /absolute/private-token [--json]
just upagent cleanup (--request ID | --all-terminal) \
  [--older-than-seconds N] [--apply] [--json]
just upagent reconcile [--json]
```

## Strict public request boundary

Named flags and `--file` enter one closed `schema_version: 1` parser. A file must be one readable
absolute JSON object and may contain only `schema_version`, `request_id`, `type`, `offering`,
`effort`, `agent`, `specialist`, `prompt_file`, and `cwd`. `--file` is mutually exclusive with
request-defining flags; `--wait` and `--json` are invocation controls and never enter the immutable
payload. Worker requests require explicit offering, effort, persona, and prompt. Specialist
requests require a specialist and prompt and resolve that specialist's pinned offering. Unknown
keys, types, offerings, efforts, personas, specialists, relative/unreadable paths, and incompatible
flags fail before the ledger, manager, pane, or worker. There is no intake LLM, prose repair,
arbitrary argument materialization, or empty-request acceptance on this path.

A caller may supply one canonical lowercase hyphenated UUID or uppercase Crockford ULID; otherwise
Python generates a UUID. The Hub hashes the canonical immutable payload, including the resolved
offering snapshot and SHA-256 of the exact prompt bytes. It atomically snapshots those bytes before
submission. The public bridge durably transitions `registered` → `submitting` → `submitted`
under a per-request lock. An identical retry resumes a registration interrupted before submission;
a retry interrupted after Recruiter acceptance resubmits the same order and reattaches through the
Recruiter's idempotent ledger. Same id plus same hash attaches without another launch; same id plus
a changed hash returns `request_id_conflict` before request mutation. This remains true after
history pruning: the compact tombstone retains the immutable payload hash, so an identical retry
attaches and a changed payload still conflicts without launching.

### Read, cancel, and terminal cleanup

`status` without a request describes the Hub; `get --request ID` is the read-only request view. It
returns submission and lifecycle state, retained result and receipt values, and typed
result/compacted/handoff/receipt/log pointers. After pruning it reports `state: pruned`, the prior
terminal state/verdict/timestamp, and which Hub-owned pointers were pruned. It never reconstructs,
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
receipt whose `cleanup.verified_absent` is true, no active lease or Hub runner, no unresolved launch,
and a fresh read-only proof that every recorded pane remains absent. Active,
`awaiting-requester`, malformed, and `cleanup-failed` requests are refused for `--request` and
reported as skipped by `--all-terminal`. `--older-than-seconds N` uses the authoritative terminal
receipt/state timestamp and includes equality (`age >= N`).

Apply commits two individually atomic tombstones in recoverable order—private Recruiter request
first, then the Hub-owned public snapshot—and prunes only each tombstone's Hub-owned siblings.
An interruption between stores is completed idempotently by the next cleanup. It does not follow or
delete caller paths: the original caller prompt/run tree and
any artifact outside those two Hub directories remain untouched. The tombstone retains request id,
immutable payload hash, terminal verdict/timestamp, compact receipt/result values, typed pointer
status, requester-control proof, and cleanup timestamp. That is enough for `get`, listing,
authenticated terminal cancellation, audit, identical reattachment, and changed-hash conflict
after the disposable prompt/order/staging/event/launch history is gone. Repeating cleanup is a
no-op that also retries removal of a previously swapped Hub-owned residual.

`offerings.yaml` contains exactly nine validated stable ids: three Claude, one Codex, and five Pi.
The same parsed object drives text/JSON listing, request validation, specialist/lifecycle references,
and the immutable order snapshot. YAML selects only harness, model, and allowed efforts. It cannot
supply a public worker or Account Manager shell command. `offerings.py` renders child tokens:
Claude uses `--effort`,
Codex uses `-c model_reasoning_effort=...`, and Pi uses a provider-qualified `--model` plus explicit
`--thinking`. Legacy and controller recipes remain socket shims; their strict order files bypass no
Hub authority.

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
process. The Hub protocol whitelists that absolute, same-user private regular-file path and never
transports the raw `RUNNER_OWNER_TOKEN`; the raw variable remains only a non-protocol
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
  `$UPAGENT_HUB_DIR` (default `<socket-path>.ledger`, shared by the main checkout and its
  worktrees). One runner atomically claims
  `active/requests/<scoped-request-id>/`, writes an authoritative generation lease, attempts the
  advisory manager, then launches the requested harness. Worker health means the expected
  foreground process, detected harness, and cwd—not merely pane creation. Manager health and its
  typed assessment are reported separately and may degrade without vetoing worker startup.
- Before launch, Python checks absolute paths, required model/effort values, harness-native model
  shape, executable presence, and (for Claude `--agent` routes) the actual persona file. Those
  facts are given to the manager. A bad request is explained to the requester and terminalized
  without ever creating a worker, even if the LLM mistakenly recommends approval.
- Public requests use one dedicated advisory Account Manager resolved through the approved
  `claude-sonnet-5` offering at `low` effort with persona `upagent-account-manager`. Manager
  failure degrades supervision only: it cannot veto Python-valid startup, mutate a lease,
  publish artifacts, invent success, or terminalize the request. Fable remains explicit-only.
- Every accepted order carries `artifact_publication`. Compatibility/controller orders that omit it
  receive deterministic result-adjacent paths and an explicit mandatory-consult list before the
  first ledger mutation. The Recruiter writes a closed-schema typed manifest
  and appends literal lease-private paths for `result.json`, `compacted.md`, and `handoff.md`;
  specialist workers additionally receive `answer.json`. Workers never receive a public answer
  destination. The result carries `passed|failed|blocked`, `revisit`, and `full_log`; both markdown
  artifacts must contain text, and specialist answers must pass `contracts_consult`, including
  consult identity, success/error shape, and real `file:line` citations.
- `just upagent-await <order.json>` waits in Python for a decision point or completion; no LLM
  loops over files. At inactivity checkpoints, a fresh cheap checker interprets one bounded pane
  and process snapshot, reports to the manager/requester, and exits.
- At a work cap, `upagent-await` returns `REQUESTER_DECISION_REQUIRED`. The requester may run
  `just upagent-respond <order> <control-token> <nonce> extend <milliseconds>` or `... cancel 0`.
  Without an answer during `management.requester_grace_ms`, the Hub performs the declared hard
  stop. Managers/checkers can recommend actions but cannot execute them.
- The deterministic completion reactor validates the whole staged bundle. Missing or malformed
  required artifacts cause exactly one repair prompt to the same worker address—never a second
  worker—and one revalidation. If that still fails, Python writes a schema-valid blocked
  result/compacted/handoff bundle and, for specialists, a valid failure answer.
- Publication is ordered: validate private staging, prepare and atomically replace every public
  artifact, revalidate the public bundle, write `receipt.json`, then append the durable terminal
  event/state and requester notification. `upagent-await` wakes only from that post-receipt
  evidence; there is no pre-publication `result-ready` notification. If anything goes wrong it
  fails loud without a terminal receipt. Publication also keeps
  the hub's own `published-result.json` and names it in the receipt, so a terminal record survives
  the pruning of the run tree that owns `result_path`: a later dispatch republishes that copy
  instead of failing in the strict result loader, and refuses with the evidence paths when no copy
  survives. The lease records the
  requester, manager, runner, Recruiter, worker, workspace, token, generation, and expiry; a
  Hub-owned Python supervisor thread safely reconciles dead/expired owners. Crash recovery uses the
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
bundle unless every requirement resolves to a matching Hub-indexed receipt whose answer verdict is
`cited`. Missing, rejected, failed, borrowed, or forged claims fail the gate. Reading source files
directly is not consultation evidence.

**Who can be asked is separate from the offering catalogue.** `specialists.yaml` is keyed by
persona and MERGES: the kit base is
`.shared-llm/public/extensions/common/upagent/specialists.yaml`; a destination may add the
repo-owned overlay `.shared-llm/this_repo/extensions/common/upagent/specialists.yaml` (template:
`specialists.yml.sample`). Every specialist pins `offering` plus `effort`; both must resolve the
nine approved entries. A destination that overrides one specialist keeps every other kit entry.
The roster is loaded only for specialist requests and specialist listing: an invalid specialist
overlay cannot block an ordinary worker request.

## Public offerings and legacy controller roster

Public workers and their Account Managers never read a YAML launch command. `offerings.yaml`
selects exactly one approved harness/model identity and effort allowlist, and `offerings.py` renders
the exact child argv. Public Account Managers are always rendered from `claude-sonnet-5`, `low`,
and `upagent-account-manager`, even when a legacy `upagent.yaml` exists. That legacy file remains
only for explicitly route-driven controller compatibility, where existing phase routes still
provide raw harness/model profiles and may configure raw lifecycle-role commands. Four launch
properties remain load-bearing:

1. **Non-interactive.** Workers run unattended in panes — every template bypasses
   trust/permission prompts (`claude --dangerously-skip-permissions`,
   `codex exec --dangerously-bypass-approvals-and-sandbox`, `pi --approve`) or the hire hangs
   until the Recruiter's timeout.
2. **Harness-native model ids.** Claude takes its model plus `--effort`; Codex takes a bare model
   plus `-c model_reasoning_effort=...`; Pi takes a provider-qualified model plus a separate
   `--thinking` token. Codex and Pi have no `--agent` flag, so their persona comes from the lease
   instructions.
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
2. Use the shipped `offerings.yaml` unchanged for the public façade. Copy
   `upagent.yaml.example` → the repo-owned `this_repo` path only when legacy route/controller
   profiles still need it.
3. Add `import '.shared-llm/public/extensions/common/upagent/justfile'` to the root justfile.

## Tests

`just test` covers the closed public schema, zero-launch rejection, prompt hashing/snapshotting,
UUID/ULID idempotency and conflict behavior, the exact nine-entry text/JSON roster, exact Claude /
Codex / Pi child tokens, specialist offering resolution, request mailboxes, identity/lease fencing,
startup health, timeout authority, typed manifests, every missing/malformed artifact, one bounded
same-worker repair, manager degradation, specialist projection, mandatory-consult enforcement,
publication fault ordering, and cleanup. Focused Hub tests also cover non-serializing blocked
awaits, concurrent request-local parser/output isolation, caller-context whitelisting and pane
identity propagation, duplicate attachment to one live runner, typed thread-start-failure
terminalization, protocol mismatch, lifetime locking, socket override,
main/worktree discovery identity, forbidden direct bypasses, canonical child engine selection,
and launch fault compensation/reconciliation.
