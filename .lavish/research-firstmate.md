# Firstmate as a Herdr replacement, enhancement, or complement

**Snapshot reviewed:** sibling `../firstmate` at `c12bdea`.

## Recommendation

**Use Firstmate as a complement and source of orchestration patterns, not as a drop-in Herdr replacement.** Firstmate is an agent distro/orchestrator; Herdr is only one experimental session-provider backend beneath it. The strongest reusable value for Claude Code is Firstmate's durable lifecycle, watcher queue, hook backstops, worktree safety, and recovery rules. Replacing an existing Herdr-centered workflow wholesale would import a large opinionated stack (`tasks-axi`, treehouse, `gh-axi`, no-mistakes, many shell scripts, state conventions, skills, and hooks). A lower-risk path is to adopt selected mechanisms incrementally, while retaining Herdr for native pane state/events if those are valuable.

Firstmate does **not** provide a generic declarative phase/stage engine for Claude Code. Its stages are an operational contract implemented across instructions and scripts: intake → brief → isolated spawn → supervision → validation/delivery mode → approval/merge → fail-closed teardown (`../firstmate/AGENTS.md:210-309`). Thus it is better viewed as an enhancement/reference implementation than a small stage orchestrator.

## Concrete findings

### 1. Claude Code orchestration and hooks — strong enhancement candidate

- Claude wiring is compact at the hook boundary: SessionStart nudge, three Bash PreToolUse guards, and a Stop guard (`../firstmate/.claude/settings.json:1-44`). The actual behavior remains in repository scripts, avoiding large JSON hook logic.
- Session start is a deterministic ordered reconciliation: acquire per-home lock, run bootstrap/sweeps only when locked, drain the durable wake queue, emit context/fleet digests, then emit one harness-specific supervision protocol (`../firstmate/AGENTS.md:116-142`). Lock refusal makes the session read-only (`:126-135`).
- Claude uses tracked background-notify supervision plus a PreToolUse continuity gate; the Stop hook blocks a blind turn when work exists and the watcher is unhealthy (`../firstmate/docs/architecture.md:50-63`; `../firstmate/docs/turnend-guard.md:39-83`). This directly addresses Claude Code's tendency to stop while delegated work remains active.
- **Severity: medium integration risk.** The safeguards assume Firstmate's `FM_HOME`, session lock, task metadata, watcher beacon, and script namespace. Copying only `.claude/settings.json` will not work; the predicate and state model must come with it or be adapted.

### 2. Durable state and supervision — strongest differentiator

- Actionable events are persisted to `state/.wake-queue` **before** detector state advances, enabling recovery after a missed watcher process exit (`../firstmate/docs/architecture.md:11-25`). Status logs are explicitly append-only events, not current state (`:26-33`).
- Current state is reconciled from authoritative validation run state, then backend busy evidence, then status events; dead panes do not cause stale event prose to be trusted (`../firstmate/docs/architecture.md:27-33`). Fleet snapshots provide a structured JSON contract instead of repeated raw-file parsing (`:34-36`).
- Watcher lifecycle has identity-matched locks, freshness beacons, typed failures, bounded lifecycle logs, and home-scoped restart (`../firstmate/docs/architecture.md:50-63`). Away mode adds a bash sub-supervisor that injects only into an affirmatively empty composer and actively alarms if escalation delivery wedges (`:65-75`).
- Durable unresolved decisions are promoted into backlog holds with stable keys and idempotent retry identities; scout teardown verifies the decision inventory before deleting source state (`../firstmate/docs/decision-hold-lifecycle.md:8-38`).
- **Severity: low conceptual risk / high implementation volume.** These are robust patterns, but distributed across numerous scripts and conventions rather than a small library/API.

### 3. Worktrees, lifecycle, merge, and teardown — mature but opinionated

- Every ship/scout spawn must resolve a genuine git worktree root distinct from the primary checkout (`../firstmate/AGENTS.md:238-247`; `../firstmate/bin/fm-spawn.sh:1-78`). Treehouse supplies worktrees for Herdr/tmux/zellij/cmux; Herdr remains session-only (`../firstmate/docs/herdr-backend.md:55-64`).
- Delivery modes are explicit: `no-mistakes`, `direct-PR`, and `local-only`; merge authority is separately controlled by captain approval or `yolo` (`../firstmate/AGENTS.md:249-283`). PR merges must use `fm-pr-merge.sh`; local merges are clean fast-forward-only (`../firstmate/AGENTS.md:258-267`; `../firstmate/bin/fm-merge-local.sh:1-68`).
- Teardown is notably fail-closed: dirty or unlanded ship work is preserved; squash-merge/content equivalence and deleted PR heads are accounted for; stale git locks are removed only after a shared proof (`../firstmate/bin/fm-teardown.sh:1-79`). Scouts require both a report and durable decision inventory (`../firstmate/AGENTS.md:293-309`).
- **Severity: medium adoption risk.** The lifecycle assumes specific branch names, registries, backlog behavior, GitHub helpers, and validation tooling. Selective reuse should begin with safety predicates, not wholesale script copying.

### 4. Recovery and restart behavior — stronger than terminal-only supervision

- Restart recovery treats disk records and live backend inventory, not conversation memory, as authoritative (`../firstmate/AGENTS.md:167-180`). A dead endpoint does not imply lost work; recovery first checks branch-matched validation state, then proves agent ownership and reuses the existing worktree (`../firstmate/.agents/skills/stuck-crewmate-recovery/SKILL.md:16-37`).
- Relaunch escalation preserves uncommitted changes and commits, uses the same task identity/brief, and refuses to allocate a second worktree while the first is unaccounted for (`../firstmate/.agents/skills/stuck-crewmate-recovery/SKILL.md:24-37`).
- Secondmates provide isolated persistent supervisor homes and durable leases, but materially increase architecture and operational complexity (`../firstmate/docs/architecture.md:138-168`).

### 5. Herdr adapter/lab evidence — valuable complement, not replacement

- Herdr support is explicitly experimental and version-gated; it supplies native busy/idle/blocked state and protocol-16 push events, while treehouse still owns worktrees (`../firstmate/docs/herdr-backend.md:1-38`; `../firstmate/docs/architecture.md:78-92`).
- Container shape is workspace-per-Firstmate-home and tab-per-task, with home-scoped recovery (`../firstmate/docs/herdr-backend.md:61-74`; `../firstmate/docs/architecture.md:91-92`). Metadata records session/workspace/tab/pane IDs (`../firstmate/docs/herdr-backend.md:161-174`).
- The guarded lab (`../firstmate/bin/fm-herdr-lab.sh:1-31,87-159,256-290`) is highly reusable safety work: names must start `fm-lab-`, default is forbidden, every command gets explicit `--session`, destructive calls re-check non-default identity, and a default-session fleet-state tripwire must remain unchanged.
- **Severity: high historical safety warning.** A label-based adopted-workspace prune once killed the captain's live pane and watcher. The fix tracks created-vs-adopted ownership structurally and is unit/E2E tested (`../firstmate/docs/herdr-backend.md:133-159`). This is compelling evidence to reuse the lab/ownership rules and avoid ad hoc Herdr cleanup.
- Herdr has real terminal transport/classification edge cases (e.g. control-byte marker loss and composer-shape detection), documented with live E2Es (`../firstmate/docs/herdr-backend.md:196-220` and later incident sections). Native agent state alone is not sufficient for safe prompt injection.

## Replacement vs. complement assessment

| Option | Assessment |
|---|---|
| Replace Herdr with Firstmate | **No.** They are different layers; Firstmate still needs a runtime backend and treats Herdr as experimental. Default/reference backend is tmux. |
| Run Firstmate on Herdr | **Viable complement**, if willing to accept the full Firstmate distro/toolchain and experimental backend status. Gains native state/events plus durable orchestration. |
| Port Firstmate patterns into current Herdr flow | **Recommended.** Prioritize: isolated lab/session targeting, durable wake queue, session lock + beacon, SessionStart reconciliation, Claude Stop/PreToolUse continuity checks, worktree identity checks, and fail-closed teardown. |
| Adopt full Firstmate lifecycle | Consider only if its ship/scout model, task backlog, treehouse, GitHub/no-mistakes tooling, and captain/secondmate semantics align with product goals. |

## Suggested incremental adoption order

1. **Herdr lab safety and explicit session targeting** (`bin/fm-herdr-lab.sh`, `docs/herdr-backend.md`).
2. **Durable state primitives**: queue-before-advance, append-only events versus reconciled current state, per-home ownership.
3. **Claude hooks**: SessionStart reconciliation, Stop blind-turn guard, then PreToolUse continuity gate.
4. **Worktree/teardown safety**: distinct-root assertion and preserve-on-uncertainty behavior.
5. Only then evaluate watcher/AFK daemon and full delivery/merge machinery.

## Residual risks / open questions

- No live Herdr or Claude Code test was run for this scout; conclusions rely on code/docs and the repository's recorded test evidence.
- Firstmate is shell-heavy and convention-heavy; there is no narrow stable orchestration API. Upstream changes may make a forked subset drift.
- Herdr backend remains experimental and depends on `herdr` protocol compatibility plus `jq`; native state must still be corroborated for safe injection.
- The target repo already had unrelated unstaged/untracked files before this report. Nothing was staged by this task.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Concrete findings and severity/risk are cited to ../firstmate/AGENTS.md, docs/architecture.md, docs/herdr-backend.md, docs/decision-hold-lifecycle.md, .claude/settings.json, recovery skill, and lifecycle scripts with line ranges."
    }
  ],
  "changedFiles": [
    ".lavish/research-firstmate.md"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "find/grep/read targeted README, docs, skills, hooks, backend, spawn, merge, and teardown files under ../firstmate",
      "result": "passed",
      "summary": "Mapped lifecycle, durable state, worktrees, supervision, recovery, merge hooks, and Herdr adapter/lab evidence."
    },
    {
      "command": "git -C ../firstmate status --short; git status --short; git -C ../firstmate rev-parse --short HEAD",
      "result": "passed",
      "summary": "Firstmate was clean at c12bdea; target repo had pre-existing unstaged/untracked files and no staged entries were shown."
    },
    {
      "command": "nl -ba selected files with sed line ranges",
      "result": "passed",
      "summary": "Captured exact line-number evidence for citations."
    }
  ],
  "validationOutput": [
    "Static research only; no runtime tests requested or run.",
    "Sibling ../firstmate working tree was clean before report creation."
  ],
  "residualRisks": [
    "No live Claude Code or Herdr verification was run.",
    "Firstmate's mechanisms are distributed across a large opinionated shell/tooling stack rather than a stable narrow API.",
    "Herdr backend is explicitly experimental and has documented historical cleanup and terminal-classification incidents."
  ],
  "noStagedFiles": true,
  "notes": "Recommendation: complement Herdr by selectively adopting Firstmate safety/orchestration mechanisms; do not treat Firstmate as a drop-in Herdr replacement."
}
```
