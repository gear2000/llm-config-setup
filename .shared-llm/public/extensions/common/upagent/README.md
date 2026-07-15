# UpAgent Hub — the Recruiter

The UpAgent Hub is a universal lifecycle service for LLM workers. Its caller may be a phase
leader, TUI, Librarian, or another framework. The always-up Python **Recruiter** persists the
request, hires a low-cost Dedicated Account Manager, atomically launches and verifies the worker,
collects the result, and reports to the recorded requester. See [FUNDAMENTALS.md](FUNDAMENTALS.md)
for the authority model and use-case tree.

## Topology

```
Herdr session
├── ws: <slug>                 TUI + leader + workers/watchdog + their managers/checkers
└── ws: shared-services        always up, plan-agnostic
    ├── recruiter               deterministic Python Hub
    └── librarian  (Specialist Hub — sibling module)
```

The Recruiter pane is a visible status surface, never a command queue. Requests go directly to
the durable ledger. A request's manager, worker, and short-lived checkers start beside the
`order.cockpit_pane` through atomic `herdr agent start` calls, making the whole dedicated lifecycle
visible in one workspace. `manager_placement.mode: shared` remains an explicit opt-in for callers
that truly want a peripheral manager. Pane placement is role-based: workers split right, while
managers and one-shot checkers split down, forming a mixed grid instead of a vertical strip. Every
pane is closed only by its fenced lease owner.

Phase startup has its own deterministic front door:

```text
just upagent-phase-start <frozen-route.yaml> <run-tree> <phase-id> <pass-number>
```

It starts the leader behind a gate, submits the phase watchdog through the normal Recruiter
lifecycle, and releases the verified leader after recording either a healthy watchdog or an
explicit degraded warning. It returns `PHASE_STARTED` with `ready` or `ready-degraded`. Leader
startup failures still close the gated leader; watchdog failures never freeze useful phase work.
When `finalization_defaults.watchdog_profile` is omitted, controllers consistently reuse the
phase leader profile rather than dropping monitoring for a configuration that older routes allow.

The controller exports its receipt path to the released leader. The Recruiter inspects that
receipt and records degraded observability when it is missing, stale, or has no watchdog address,
but accepts the stage order. The warning is included in modern startup responses and stored in the
durable request ledger.

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
  If anything goes wrong it publishes a fail-loud `blocked` result/receipt. The lease records the
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

`python3 -m pytest .shared-llm/public/extensions/common/upagent/ -q` covers contracts, typed LLM
responses, request mailboxes, identity/lease fencing, startup health, timeout authority, roster
resolution, and cleanup behavior. The socket-driving path is also exercised in a live Herdr
session before release.
