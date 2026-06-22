# /run-phase

Slash command that runs **ONE phase** of a plan with an **explicit, caller-supplied team**.
It reads the whole plan file, finds the phase, and runs exactly the agents the caller named on
that phase's work. Built to run INSIDE the `claude -p "/run-phase plan=… phase=… agents=…"`
worker the meta-orchestrator Pi extension spawns — so this skill is the worker's whole job for
one phase.

This is `/team` evolved. `/team` took `<team-name> <inline task>` and looked the roster up in a
registry. `/run-phase` takes a **plan file + a phase number + an explicit agent list**: the
work comes from the plan (not an inline string), and the roster comes from the **caller** (the
brain already chose the agents — there is no registry to look up). Everything else — the
`ask_brain` escalation contract, scratch-only unattended auto-fix — is the same.

**You are the orchestrating lead.** The worker running `/run-phase` IS the lead for this phase —
there is no separate team and no team leader to spawn. You read the plan, then dispatch each agent
in `agents=` as a **subagent** (the Task/Agent tool, `subagent_type=<name>`), read each return, and
iterate toward the phase goal. Subagents are the default transport (a fresh context window per unit
of work, no `team_name`); the headless phase worker has no TMUX and needs no lateral comms between
agents, so subagents are exactly right. (If `agents=` happens to include a generic `Lead`, that is
YOU — do not dispatch a `Lead` subagent.)

## What you already have (injected at spawn)

The Pi extension spawns you with an `ask_brain` MCP tool and an `ask_brain` system-prompt
addendum already in place (it pins them with `--mcp-config` + `--strict-mcp-config` +
`--append-system-prompt`). You do **not** create that tool — you USE it. `ask_brain` reaches
the resident leader (the brain) and **blocks until the leader answers**, then returns the
leader's guidance. The leader is who steers you: it either sharpens your next move and sends
guidance back down, or tells you to STOP and ask the human. This skill tells you WHEN to call
`ask_brain` and how to act on the reply.

If `ask_brain` is **not** available (you were invoked by hand, not by the extension — e.g. a
bare smoke-test run), do not crash. Say so once, then run the phase best-effort: report what
you would have escalated to the user inline instead of through `ask_brain`. The absence of the
back-channel is expected in a by-hand run; it is not an error.

## Invocation

```
/run-phase plan=<path> phase=<N> agents=<agent-a,agent-b,...>
```

- `plan=` — absolute path to the plan markdown file (the brain's plan).
- `phase=` — the phase number to run (Phase 0 is valid).
- `agents=` — a **comma-separated** list of agent names. These are the EXACT agents to run —
  the brain already chose them; you use them verbatim, you never pick your own.

These are `key=value` tokens in `$ARGUMENTS`. They may appear in any order. The agent list is a
single token (comma-separated, no spaces inside it).

Example (the launch template the brain uses — fill in only `<N>` and the agents):

```
/run-phase plan=~/smoke-plan.md phase=0 agents=code-review
```

## Execution

### Step 1 — Parse `$ARGUMENTS` (key=value tokens)

Parse `plan`, `phase`, and `agents` out of `$ARGUMENTS` by their `key=` prefixes (tokens may be
in any order):

- `PLAN` = the value of `plan=` (a filesystem path).
- `PHASE` = the value of `phase=` (the phase identifier — typically a number; `0` is valid).
- `AGENTS` = the value of `agents=`, split on commas into a list of agent names; trim whitespace
  around each name and drop empty entries.

Validate — **fail loud, do not guess a default for any of these:**

- **Empty `$ARGUMENTS`, or any of `plan=` / `phase=` / `agents=` missing** → print:

  ```
  Usage: /run-phase plan=<path> phase=<N> agents=<agent-a,agent-b,...>
  All three are required: a plan file, a phase number, and an explicit comma-separated agent list.
  ```

  and stop. Never run with a defaulted plan, a defaulted phase, or a guessed agent.
- **`agents=` present but the list is empty after splitting** (e.g. `agents=` or `agents=,`) →
  print the same usage line, noting `agents= resolved to an empty list`, and stop. A phase with
  no team is nothing to run.

### Step 2 — Read the plan and locate the phase (fail loud on both)

1. **Read the WHOLE plan file at `PLAN`.** If the file does not exist or cannot be read, FAIL
   LOUD and stop:

   ```
   ERROR: plan file not found / unreadable: <PLAN>
   ```

   Do not invent a plan, do not fall back to `$ARGUMENTS` as the task. The plan IS the work.

2. **Find Phase `PHASE` in the plan.** Look for a heading that names the phase — a `### Phase
   <PHASE>` section (also accept `## Phase <PHASE>` or a `Phase <PHASE>` heading / `Phase
   <PHASE> —` line; match the phase number, tolerate a trailing title). The phase's work is that
   section's body, down to the next phase heading (or end of file). Read the WHOLE plan for
   context first — an earlier phase or the plan's goal may bear on how you run this one — but
   **execute only Phase `PHASE`'s work.**

3. **Phase not found → FAIL LOUD and stop:**

   ```
   ERROR: phase "<PHASE>" not found in plan <PLAN>.
   Phases present: <list the phase headings you DID find>.
   ```

   List the phase headings you found so the caller sees the mismatch. Do not run the nearest
   phase, do not run Phase 0 as a fallback — a wrong phase silently does the wrong work.

### Step 3 — Resolve the agents (use exactly what was passed; fail loud on a bad name)

`AGENTS` is the roster — **verbatim from the caller.** Each name is an EXACT `.claude/agents/`
file name; use it as-is for the `subagent_type` of each subagent you dispatch.

- **You do NOT choose, add, or drop the work agents.** The brain already chose this roster for
  this phase. Run exactly the list you were given — no substitutions, no "I'd also add one",
  no dropping one that looks redundant. (Why: the brain picked the roster against the whole-plan
  context you don't have. Second-guessing it runs the wrong specialists.) The one agent NOT in
  this list — the mandatory `adversarial-evaluator` you dispatch at the end of the phase (Step 8) —
  is a built-in gate, not a roster choice; it is not subject to this rule.
- **An agent name that does not resolve to a real agent → FAIL LOUD. Stop immediately.** Print:

  ```
  ERROR: agent "<name>" (from agents=) is not a known agent. Cannot spawn it.
  Agents requested: <the full list>.
  ```

  Do not silently skip the bad name and run the rest — a partial roster is the wrong roster. Do
  not swap in a near-match. (Why loud: the caller named this agent on purpose; a typo or a renamed
  agent needs to surface, not be quietly dropped.)

### Step 4 — Dispatch the agents as subagents and run the phase

You ARE the lead. Dispatch each agent in `AGENTS` as a **subagent** via the Task/Agent tool —
`Agent(subagent_type=<agent-name>, …)`, the exact name the caller passed. Subagents are the
default transport for this worker: fresh context window per unit of work, no `team_name`, no TMUX
window. The headless phase worker needs none of the TeamCreate machinery (no shared TMUX to watch,
no lateral SendMessage between agents — every agent reports back to YOU on return), so subagents
are the right and only transport here.

1. For each agent name in `AGENTS`, call `Agent(subagent_type=<agent-name>, …)` — one dispatch
   per agent. Give each subagent:
   - **the phase's work** (from Step 2) as its objective — the goal, the steps, the verification
     the plan spells out for this phase,
   - its own role within the roster (the brain composed this roster for this phase; honor whatever
     mix it chose — a feature specialist owns the domain work, a `deployer` takes it live and
     confirms clean logs, a `phase-evaluator` judges PASSED / FAILED / BLOCKED),
   - that it reports back to YOU on return — when it hits a blocker it cannot resolve, it says so
     in its return, and YOU (the lead) are the one who decides the next step and calls `ask_brain`.
2. **You (the lead) orchestrate AND do the first-line checking — you do not do the domain work
   yourself.** Dispatch the subagents, read each return, decide the next step. You are the first
   line of defence on the plan: you scoped each work agent's task from the phase (Step 2), and as
   each one returns you **read its return and check it against the plan** — did it do what the
   phase asked, did it veer or creep beyond scope, did it leave the work half-done, is its "done"
   claim actually backed. When a return doesn't match the plan, **re-dispatch** to correct it (the
   same agent again with sharpened guidance, or the next agent in the roster); when the drift is a
   leader's-call or you can't resolve it, escalate via `ask_brain` (Step 5). Let the phase
   **iterate dynamically toward the goal** — this is NOT a rigid fixed-step ralph loop — until the
   phase goal is met. There is no live watcher patrolling the agents while they run: subagents do
   not run concurrently, so the checking happens **on each return** (you) and once more at the end
   of the phase (the mandatory adversarial-evaluator, Step 8).
3. If a unit of phase work needs a long-running background process that must survive across turns
   (a background build/deploy you poll later), keep the polling loop in YOUR turns (you, the lead,
   persist across the whole phase) — kick off the background work inside a subagent, capture what
   it started, and drive the wait yourself. Do not assume a subagent's own background process
   outlives its return.

### Step 5 — Escalate through `ask_brain` at the decide-or-ask threshold

You (the lead) are the single voice up to the brain. When the phase hits the threshold below, YOU
call `ask_brain(severity, message)` and act on the reply. Match the severities to the `ask_brain`
contract the extension injected (do not invent new ones):

| Severity     | Call it when…                                                                                          |
|--------------|--------------------------------------------------------------------------------------------------------|
| `"blocked"`  | the work cannot proceed without a decision — missing input, ambiguous scope, or a failure it cannot resolve on the repo's rails (a layer that won't build with everything below it confirmed good, a missing infra resource the automation should create but didn't). |
| `"decision"` | two valid paths and the choice is the leader's to make (which layer owns the fix, ship-now vs. tear-down-and-rebuild). |
| `"progress"` | a discovery that **changes the plan** — report it, then keep going. Not for routine status. |
| `"heartbeat"`| the periodic check-in (Step 6), so the leader can reach you even while you are head-down. |

Acting on the reply (this is the design's "loop on a snag"):

- The call **blocks** and returns the leader's guidance. **A simple surprise** → the leader
  sharpens your next move and sends guidance back down → apply it and continue iterating.
- **If the reply says STOP** (the leader judged the situation fundamentally wrong, or wants to
  ask the human) → **end the phase now.** Stop dispatching, wind down (Step 9), and report why you
  stopped. Do not push past a STOP.

Do **not** call `ask_brain` for routine progress that needs no decision — that is noise. Call it
at the threshold: blocked, a leader's-call choice, a plan-changing discovery, or the heartbeat.

**If `ask_brain` is not injected** (a bare by-hand run): you cannot escalate. At a threshold that
would have called `ask_brain`, make the safe best-effort call yourself, note inline what you
would have asked, and keep the unattended safety rail (Step 7 — scratch-only). Never fabricate a
leader reply.

### Step 6 — Heartbeat

On the heartbeat cadence the orchestrator injected at spawn (its `ask_brain` system-prompt
addendum states the exact N — by default every **12 significant steps**: a build, a deploy, a
layer fixed, a test run — not every tool call), call `ask_brain("heartbeat", "<one-line
status>")` so the leader can reach you even when you are head-down. Honor the injected value — do
not override it with a number of your own. Continue on its reply (unless it says STOP — then Step
5's STOP rule applies). This is the floor that makes a head-down `claude -p` reachable: between
subagent dispatches, count your significant steps and send the heartbeat when you cross the
cadence. (No `ask_brain` injected → skip the heartbeat; there is no one to reach.)

### Step 7 — Unattended auto-fix touches scratch only

You run unattended (no human at this keyboard — the human is watching the leader's TUI). So the
safety rule is strict:

- Small, obvious fixes you or a subagent can make to keep moving — a missing test fixture, a typo
  in a scratch script, a local note — go in **`scratch/` only**. Fix and continue.
- **Anything beyond scratch** — a real code change, a commit, a deploy, a destructive action, a
  structural decision — is NOT yours to do unattended. Route it up via `ask_brain` (Step 5) and
  let the leader decide (it will steer you, or STOP and ask the human). When in doubt, ask
  `ask_brain`; do not improvise on shared code.

This matches the orchestrator's guardrail: unattended runs auto-fix scratch only; real commits
wait for a human.

### Step 8 — Mandatory end-of-phase adversarial evaluation (the gate the phase passes through)

After the work agents from `agents=` have finished and YOUR first-line checking says the phase
goal looks met, you **ALWAYS** dispatch ONE more subagent before the phase can pass: the
`adversarial-evaluator`. This is a **built-in mandatory gate, not part of the work roster** — it
runs on **every** phase regardless of what `agents=` lists, and you never skip it, never ask the
brain whether to run it, and never substitute one of the work agents for it.

There is **no live watcher** in this design — subagents do not run concurrently, so nothing can
watch the work happen. The adversarial-evaluator is the end-of-phase replacement: it does not
watch live, it **reviews the finished phase** against the plan, on the strongest model at max
effort. You (first-line, on each return) and it (last gate, once at the end) are the whole of the
plan-adherence checking.

1. **Dispatch it as a subagent:** `Agent(subagent_type="adversarial-evaluator", …)`. Give it:
   - **the phase's goal + work** (from Step 2) — the plan section this phase was meant to
     accomplish; this is the contract it judges against,
   - **the work that was done** — the work agents' returns, the files touched, the commands run
     and their output, the diff, any evidence you collected. It will independently re-check claims
     it isn't given evidence for, so hand it everything you have.
2. **It returns a verdict — CLEARED or VEERED** (it defaults to VEERED when unsure). Act on it:
   - **CLEARED** → the phase PASSES. Proceed to wind-down (Step 9).
   - **VEERED** → the phase does **NOT** pass yet. Read its findings (each cites a file:line or the
     exact claim/step that veers from the plan) and **re-work the flagged piece**: re-dispatch the
     relevant work agent with the correction, then **re-run this gate** (dispatch a fresh
     adversarial-evaluator on the corrected work). Loop until it returns CLEARED. If you cannot
     resolve a finding yourself — it needs a leader's-call decision, or it's a real code change /
     commit / deploy beyond the scratch rail (Step 7) — **escalate via `ask_brain`** (Step 5) and
     act on the reply. **The phase does not pass until the adversarial-evaluator returns CLEARED**
     (or the brain STOPs the phase).
3. **The gate is not advisory.** A VEERED verdict you cannot clear is a blocked phase — route it up
   via `ask_brain`; never report a phase PASSED over a standing VEERED verdict.

(If `adversarial-evaluator` somehow does not resolve as a known agent, that is the same hard stop
as any unresolved agent — FAIL LOUD per Step 3. The gate is mandatory; a missing evaluator is a
broken setup, not a reason to skip the gate.)

### Step 9 — Wind down and emit the final verdict (`PHASE_RESULT:`)

When the phase reaches its end state (the work is done, the mandatory adversarial-evaluator
returned **CLEARED**, and any `phase-evaluator` in the roster returns PASSED), or when a STOP came
back from `ask_brain`:

1. Stop dispatching subagents — there is nothing to tear down (each subagent already returned when
   its dispatch finished; subagents do not linger).
   - **If this phase commits anything** (the brain authorised a real commit — rule 9), stage the
     files this phase changed **by explicit path** (`git add <path> …`, never `git add -A`/`.`) and
     `git commit` them in the **same step**, with no gap between staging and the commit — you share
     one git index with anything else running, so a blanket add or a staging gap would sweep
     unrelated work into your commit (rule 11).
2. Report the outcome to the leader: PASSED with what was done + evidence (pipeline numbers,
   verify results, clean-log confirmation, **and the adversarial-evaluator's CLEARED verdict**), or
   the real not-passed outcome with its reason (a standing VEERED the gate could not clear, a hard
   blocker, or a brain STOP). Name the plan + phase you ran and the agents you used, so the brain
   can judge it against the whole plan.
3. **Emit a machine-readable verdict as the LAST line of your report — this is mandatory.** The
   brain reads this line to record the phase's TRUE outcome in its durable ledger; resume re-runs
   anything that is not `passed`. Print **exactly** one line, nothing after it:

   ```
   PHASE_RESULT: passed|partial|blocked|failed
   ```

   Choose the verdict honestly from what actually happened — **NOT** from "my process is about to
   exit cleanly":

   | Verdict   | Emit it when…                                                                                          |
   |-----------|--------------------------------------------------------------------------------------------------------|
   | `passed`  | the phase's work is **fully done** AND the mandatory adversarial-evaluator returned **CLEARED**. Both. Nothing left for this phase. |
   | `partial` | real work landed but the phase goal is **not fully met** (some of the phase's done-checks are still unmet). |
   | `blocked` | you could **not proceed** — the adversarial-evaluator VEERED and you could not clear it, or a hard blocker stopped the work, or `ask_brain` returned STOP. |
   | `failed`  | the phase hit an error it could not get past. |

   **Never print `PHASE_RESULT: passed` when the evaluator VEERED, when any of the phase's
   done-checks is unmet, or when the work is otherwise incomplete.** A clean process exit is **not**
   a pass — if the work is not truly done and CLEARED, the honest verdict is `partial`, `blocked`,
   or `failed`. (If `ask_brain` was never injected — a bare by-hand run — still emit the line; it is
   your honest self-assessment of the phase.) If you finish without emitting this line, the brain
   cannot certify a pass and records the phase as `failed` (re-run) — so always emit it.

## Hard rules

1. **All three args required → FAIL LOUD on any missing** (Step 1). Never default the plan, the
   phase, or the agents.
2. **Missing plan file or phase-not-found → FAIL LOUD and stop** (Step 2). Never run the nearest
   phase or fall back to Phase 0.
3. **Run EXACTLY the agents passed** — verbatim from `agents=`. Do not choose, add, or drop any.
   The brain owns the roster; you run it.
4. **Only dispatch agents that exist** — an unresolved `agents=` name is a hard stop (Step 3).
   Never skip it and run a partial roster; never swap a near-match.
5. **You orchestrate AND do the first-line checking; the subagents do the work.** As the lead you
   dispatch each agent in `agents=` as a subagent (the Task/Agent tool), read each return and check
   it against the plan, and decide — you do not do the domain work yourself, and you never wrap the
   roster in a team. There is no live watcher; checking is on-return (you) plus the end-of-phase
   gate (rule 6).
6. **The end-of-phase adversarial-evaluator is a MANDATORY gate (Step 8).** After the work agents
   finish, you ALWAYS dispatch the `adversarial-evaluator` to review the finished phase against the
   plan — on EVERY phase, regardless of what `agents=` lists. The phase passes ONLY when it returns
   CLEARED. On VEERED, re-work the flagged piece and re-run the gate, or escalate via `ask_brain`;
   never report PASSED over a standing VEERED. The gate is not a roster choice and is never skipped.
7. **`ask_brain` is the only channel up** when injected. Use the injected tool; do not open your
   own socket. Match its severities (`blocked` / `decision` / `progress` / `heartbeat`) — do not
   invent new ones. When it is absent, proceed best-effort and note it — never fabricate a reply.
8. **STOP means stop.** When `ask_brain` returns STOP, end the phase — do not push past it.
9. **Scratch-only unattended auto-fix.** Real code / commits / deploys / destructive actions
   route up via `ask_brain`; you never do them unattended on your own judgment.
10. **Emit the final `PHASE_RESULT:` verdict line (Step 9), always, as the LAST line.** `passed`
    ONLY when the work is fully done AND the adversarial-evaluator CLEARED it; otherwise `partial`
    / `blocked` / `failed` — honestly. Never claim `passed` over a VEERED gate or incomplete work;
    a clean process exit is not a pass. The brain records this verdict and re-runs anything that is
    not `passed`.
11. **When you commit, stage your OWN changed files BY EXPLICIT PATH — never `git add -A` / `git add .`.**
    You share ONE git working tree and index with whatever else is running (an adjacent phase, the
    human, another tool). A blanket `git add -A`/`git add .` sweeps every unrelated change into your
    commit — that is the contamination this guards against. So `git add <path> <path> …` the exact
    files this phase changed, then commit them **immediately** in the same step — keep no gap between
    staging and `git commit`, so nothing else can land a change in the index between the two. Stage
    only what you changed; if you are unsure a file is yours, leave it out. (Commits remain
    scratch-only-unless-authorised per rule 9 — this rule governs HOW you commit once a commit is
    warranted, not WHETHER.)

## Layering this slash command sits on

```
Layer 0   The meta-orchestrator brain (Pi extension, or the SDK leader)
            picks the phase + the agents, then spawns:
              claude -p "/run-phase plan=<file> phase=<N> agents=<a,b,...>"
            injects: ask_brain MCP tool (--mcp-config --strict-mcp-config)
                     + ask_brain system-prompt addendum (--append-system-prompt)
            owns the SERVER side of ask_brain (the Unix-socket back-channel to the leader)
Layer 1   The plan file on disk
            ordered phases (Phase 0 first); each phase's body is its work
Layer 2   /run-phase slash command                       -- what you're reading
            parses plan=/phase=/agents=, reads the plan, locates the phase,
            dispatches the caller's roster as subagents (Task/Agent tool), iterates,
            does first-line plan-checking on each return, calls ask_brain (Layer 0)
            at the threshold, and ALWAYS runs the mandatory adversarial-evaluator gate
            at the end of the phase (CLEARED to pass)
```

The roster comes from the CALLER (Layer 0's brain), not from a registry inside this skill —
that is the one structural difference from `/team`. To run a phase by hand without the
orchestrator, pass the agents yourself on the command line — same roster dispatched as subagents,
no `ask_brain` channel (report escalations to the user inline instead).

## Out of scope

- Defining the `ask_brain` tool or its socket — that is the Pi extension's job; this skill only
  calls the injected tool.
- **Choosing the WORK agents** — that is the brain's job (the plan's phase lists them, or the
  brain picks them and passes them in). This skill runs the list it is given; it never selects a
  work roster. (The one agent this skill always adds itself — the mandatory end-of-phase
  `adversarial-evaluator` gate, Step 8 — is not a roster choice; it runs on every phase.)
- The domain work itself (onboarding / add-project / resources / scan / … steps) — owned by the
  specialist agents in the roster, which carry their own playbooks.
