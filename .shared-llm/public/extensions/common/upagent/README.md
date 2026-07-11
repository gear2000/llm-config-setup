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

- The phase leader writes `order.json` (harness, model, agent, cwd/worktree, instructions
  path, result path, `cockpit_pane`) and signals the armed Recruiter pane:
  `herdr pane run <recruiter_pane> "recruit <order.json>"`. (`just upagent-up` arms a `recruit`
  shell function in the Recruiter pane and prints/persists its pane id.)
- The Recruiter splits a fresh worker pane from `order.cockpit_pane` (`--cwd` the worktree,
  `--env` any OTel vars), runs the harness launch template for `order.harness`, and blocks on
  `herdr wait agent-status <worker> --status done`.
- The worker reads the instructions, does the one stage, and **before its pane closes** writes
  `result.json` (verdict `passed|failed|blocked`, a `revisit` list of stage-ids on failure,
  and a `full_log` pointer to its harness transcript) plus its `compacted.md` and handoff.
- The Recruiter validates the result (must echo the `order_id`), closes the worker pane, and
  emits `ORDER <order_id> DONE`. If anything goes wrong it still writes a fail-loud `blocked`
  result and emits `DONE`, so the leader is never stranded — it reads the verdict and escalates
  per its budget.

`route.yaml` is authoritative for which harness/model/agent runs each stage. The Recruiter only
holds a mechanical per-harness launch template (`upagent.yaml`) — it never picks the agent.

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
