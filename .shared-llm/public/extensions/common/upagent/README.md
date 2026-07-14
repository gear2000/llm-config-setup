# UpAgent Hub — the Recruiter

The UpAgent Hub is where the actual work of a phase gets done. Its always-up **Recruiter**
takes a work order from a phase leader, hires a fresh worker for that one stage, collects the
worker's result, and reports back. It runs entirely over the Herdr socket — no Go message hub,
no tmux.

## Topology

```
Herdr session
├── ws: <slug>                 the run cockpit (phase leader + one worker pane at a time + TUI)
└── ws: shared-services        always up, plan-agnostic
    ├── recruiter  ← this module
    └── librarian  (Specialist Hub — sibling module)
```

The Recruiter pane lives in `shared-services`. It spawns each hired worker pane **into the
cockpit** — by splitting the cockpit pane the order names (`order.cockpit_pane`) — beside the
phase leader that ordered it, then closes it when the stage is done. (`herdr pane split` splits
an existing pane, so the order carries a pane to split from, not a workspace label.)

## The order → result contract (`contracts.py`)

Durable files are the source of truth; Herdr only carries the go/done signal.

- The phase leader writes `order.json` (harness, model, agent, effort, cwd/worktree,
  instructions path, result path, `cockpit_pane`) and signals the armed Recruiter pane:
  `herdr pane run <recruiter_pane> "recruit <order.json>"`. (`just upagent-up` arms a `recruit`
  shell function in the Recruiter pane and prints/persists its pane id.)
- The Recruiter validates and persists a copy-on-write request under
  `$UPAGENT_HUB_DIR` (default `~/.local/state/herdr/upagent-hub`), then immediately starts a
  hidden per-job runner and returns. The job runner atomically claims
  `active/requests/<hashed-order-id>/`, writes an authoritative lease plus a retained expiry
  index, splits its fresh worker pane from `order.cockpit_pane` (`--cwd` the worktree, `--env`
  any OTel vars), and runs the harness launch template for `order.harness`. Only that claim
  owner blocks on `herdr wait agent-status <worker> --status done` and then accepts only a
  strictly valid result file.
- The worker reads the instructions, does the one stage, and **before its pane closes** writes
  `result.json` (verdict `passed|failed|blocked`, a `revisit` list of stage-ids on failure,
  and a `full_log` pointer to its harness transcript) plus its `compacted.md` and handoff.
- The job owner validates the result (must echo the `order_id`), closes only its worker pane,
  writes immutable events plus an atomically replaced terminal snapshot, releases its active
  claim, and emits `ORDER <order_id> DONE`. If anything goes wrong it still writes a fail-loud
  `blocked` result and emits `DONE`, so the leader is never stranded — it reads the verdict and
  escalates per its budget. A request's immutable `request.json` and events are durable; its
  `state/latest.json` is the copy-on-write current view. The lease is authoritative; retained
  `active/by-expiry` entries are merely reaping indexes and must be token-checked before reuse.

`route.yaml` is authoritative for which harness/model/agent runs each stage. The Recruiter only
holds a mechanical per-harness launch template (`upagent.yaml`) — it never picks the agent.

A direct Codex worker uses this launcher shape; it is not routed through Pi:

```text
codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
  --model {model} -c model_reasoning_effort={effort} \
  "Read {instructions_path} ... write result.json to {result_path}."
```

## The roster (`upagent.yaml`) — how each harness launches

The launch templates are pre-hardened; leaders and TUIs never hand-craft a worker command.
Every template substitutes `{model}` / `{agent}` / `{effort}` / `{cwd}` /
`{instructions_path}` / `{result_path}` from the order (`{effort}` is resolved by the leader
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

## Adopt it

1. Copy this whole directory to the same relative path (public tool modules land under a
   destination's `.shared-llm/public/extensions/common/upagent/` via `just update`).
2. Copy `upagent.yaml.example` → `upagent.yaml` and adapt the launch templates to your
   harnesses. In a split destination the filled roster is repo-owned (`this_repo`).
3. Add `import '.shared-llm/public/extensions/common/upagent/justfile'` to the root justfile.

## Tests

`python3 -m pytest .shared-llm/extensions/common/upagent/ -q` covers the order/result contract
and the Recruiter's pure roster/launch-resolution logic. The Herdr-driving path is proven
end-to-end inside a live Herdr session.
