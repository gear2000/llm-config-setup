# Code Context

## Files Retrieved
1. `../no-mistakes/README.md` (lines 22-121) — product promise, triggers, isolation, supported agents, and delivery lifecycle.
2. `../no-mistakes/docs/src/content/docs/concepts/gate-model.md` (lines 1-210) — bare-repo proxy, hook/daemon/worktree architecture, state, lifecycle, and bypass semantics.
3. `../no-mistakes/docs/src/content/docs/concepts/pipeline.md` (lines 1-89) — fixed nine-step pipeline and pass meaning.
4. `../no-mistakes/docs/src/content/docs/reference/repo-config.md` (lines 1-180) — trusted configuration boundary, command execution, and project-instruction suppression.
5. `../no-mistakes/docs/src/content/docs/reference/global-config.md` (lines 1-173) — supported agent selection, fallbacks, and native CLI arguments.
6. `../no-mistakes/internal/gate/gate.go` (lines 20-213) — idempotent gate provisioning and remote/hook wiring.
7. `../no-mistakes/internal/git/hook.go` (lines 15-118) — non-blocking `post-receive` notification implementation.
8. `../no-mistakes/internal/agent/claude.go` (lines 18-238) — Claude print-mode integration, session resume, structured output, permissions, and settings-source handling.
9. `../no-mistakes/skills/no-mistakes/SKILL.md` (lines 1-220) — Claude/agent-facing AXI workflow, human gates, branch custody, and completion outcomes.
10. `../no-mistakes/.no-mistakes.yaml` (lines 1-23) — dogfood test/lint/format and documentation policy example.
11. `.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md` (lines 1-180) — Herdr's plan/phase orchestration scope and runtime lifecycle.
12. `herdr-config.toml` (lines 1-15) — local Herdr UI/runtime config only.

## Key Code

### What problem no-mistakes solves

It is a **pre-publication change-delivery gate**, not an implementation orchestrator. A developer pushes committed work to a local `no-mistakes` Git remote; a disposable worktree runs `intent → rebase → review → test → document → lint → push → pr → ci`, and only then forwards to the real target and opens/monitors a PR (`../no-mistakes/README.md:32-65`, `docs/.../pipeline.md:7-45`). Findings carry severity (`error`, `warning`, `info`) and action (`auto-fix`, `ask-user`, `no-op`), with judgment calls parked for a human (`docs/.../pipeline.md:64-71`; `skills/no-mistakes/SKILL.md:117-171`).

### Enforcement mechanism

`init` creates `~/.no-mistakes/repos/<id>.git`, adds an explicit `no-mistakes` remote, installs a managed `post-receive` hook, starts a daemon, and records state in SQLite (`docs/.../gate-model.md:25-48`; `internal/gate/gate.go:29-48,143-201`). The hook only notifies the daemon and **always exits 0**; its exit status cannot reject the already-local push (`internal/git/hook.go:15-18,30-36,72-108`). Actual publication is withheld until the pipeline's push step.

**High-severity limitation:** this is opt-in rather than universal enforcement. `origin` remains unchanged and ordinary `git push origin` bypasses the gate by design (`docs/.../gate-model.md:74-92`). It cannot replace policy-enforced protected branches/required CI for hostile or forgetful actors.

### Scope and lifecycle

- Fixed nine-step pipeline; steps cannot be reordered or extended, though commands, agents, auto-fix limits, ignores, evidence, and one-run skips are configurable (`docs/.../pipeline.md:73-89`).
- Runs are daemon-owned and crash-recoverable, use detached disposable worktrees, persist SQLite/log state, and expose TUI plus machine-readable AXI control (`docs/.../gate-model.md:94-210`).
- A push to the local gate returns quickly; validation continues asynchronously. AXI calls synchronously drive approval points. Final useful state is `checks-passed` (green, mergeable PR awaiting human merge); monitoring continues in background (`skills/no-mistakes/SKILL.md:99-194`).
- Safe fixes may be committed in the isolated run branch. During an active run the pipeline owns branch custody; callers must not edit/rebase independently (`skills/no-mistakes/SKILL.md:151-203`).

### Claude Code compatibility

Compatibility is first-class, not merely prompt-level:

- `init` installs `/no-mistakes` to `~/.claude/skills/no-mistakes/SKILL.md` and a shared agents skill location (`docs/.../gate-model.md:35-43`).
- Native Claude invocation uses `claude -p --verbose --output-format stream-json`, optional `--json-schema`, durable `--resume`, retries, and structured event parsing (`internal/agent/claude.go:18-118,152-197`).
- Default execution adds `--dangerously-skip-permissions` unless overridden (`internal/agent/claude.go:190-195`). **High severity:** gate agents therefore have broad host authority; isolation is a worktree convention, not an OS sandbox.
- For orchestration repositories, trusted `disable_project_settings: true` makes Claude use only user setting sources, excluding project/local `CLAUDE.md`, `AGENTS.md`, and settings; the gate fails closed if overrides defeat this (`docs/.../repo-config.md:91-118`; `internal/agent/claude.go:38-59,167-185`). This is especially relevant here so a reviewer does not adopt Herdr operator authority.
- Executable repo config (`commands`, `agent`, documentation instructions, suppression choice) is read from freshly fetched trusted default-branch config unless explicitly opted out, reducing pushed-branch command injection risk (`docs/.../repo-config.md:7-25`).

## Architecture

Herdr and no-mistakes operate at different layers:

- **Herdr:** takes a checked plan/route, creates a cockpit, delegates implementation through phase leaders/workers, backtracks phases, handles runtime decisions, and writes durable phase/run results (`.shared-llm/.../herdr-run/command.md:1-18,30-50,95-180`).
- **no-mistakes:** accepts the resulting committed feature branch, independently rebases/reviews/tests/doc-checks/lints it, then controls publication, PR creation, and CI monitoring.

Recommended placement:

```text
plan/route → Herdr implementation + phase evidence → committed feature branch
  → no-mistakes validation/delivery gate → upstream branch + PR + CI → human merge
```

It is therefore a **complement and enhancement**, not a Herdr replacement. It could replace only any ad hoc final review/push/PR/CI tail currently surrounding Herdr. It cannot replace Herdr's multi-phase decomposition, worker routing, cockpit, escalation, or IaC approval lifecycle. Conversely, Herdr does not provide no-mistakes' explicit local Git publication boundary or long-lived PR/CI monitor.

Integration should be a deliberate terminal handoff after Herdr publishes `run-terminal.json`/`run-status.md` and returns branch custody—not a no-mistakes run inside active Herdr phases. Configure this orchestration repo with `disable_project_settings: true`, explicit trusted test/lint commands, and review auto-fix `0`; pass the original plan/user objective as AXI `--intent`. Keep protected branches/required remote CI as the non-bypassable backstop.

## Recommendation

**Adopt experimentally as a post-Herdr delivery gate; do not replace Herdr.** The strongest value is independent review plus a single controlled path from finished branch to green PR. Pilot on one non-critical repo and verify branch-custody handoff, fix-commit behavior, CI-provider support, daemon recovery, and interaction with existing hooks. Do not describe it as mandatory enforcement unless direct pushes are separately blocked server-side.

Residual risks:

- **High:** named-remote bypass permits direct pushes to `origin`.
- **High:** native agents default to permission bypass and execute with maintainer credentials; disposable worktrees are not security sandboxes.
- **Medium:** fixed pipeline cannot embed Herdr-specific stage/evidence gates except through commands/instructions or tests.
- **Medium:** two lifecycle owners can conflict if no-mistakes starts before Herdr relinquishes the branch.
- **Medium:** local daemon/SQLite/worktrees add operational state and recovery burden.
- **Low:** supported provider/CI behavior and Claude CLI wire formats can drift; the sibling has extensive unit/e2e fixtures, but no tests were executed for this read-only scout.

## Start Here

Open `../no-mistakes/docs/src/content/docs/concepts/gate-model.md` first. It states the enforcement boundary and bypass semantics most important to deciding how it should sit after Herdr; then read `.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md` to define the exact custody handoff.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Concrete findings, severity-ranked risks, and recommendations cite 12 exact source/doc/config paths and line ranges."
    }
  ],
  "changedFiles": [
    ".lavish/research-no-mistakes.md"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "git -C ../no-mistakes status --short; git -C ../no-mistakes rev-parse --short HEAD; git diff --cached --name-only",
      "result": "passed",
      "summary": "Sibling was clean at commit 2d10688; current repository had no staged files."
    },
    {
      "command": "targeted ls/find/grep/read inspection of README, docs, source, tests/config surfaces",
      "result": "passed",
      "summary": "Mapped enforcement, pipeline, Claude adapter, skill lifecycle, configuration trust boundary, and Herdr handoff."
    }
  ],
  "validationOutput": [
    "Report written to /home/gary/project/repos/llm-config-setup/.lavish/research-no-mistakes.md",
    "No source or test files modified."
  ],
  "residualRisks": [
    "No test suite was run because the task was read-only analysis.",
    "Direct origin pushes bypass no-mistakes unless server-side controls prevent them.",
    "Default agent permission bypass is not an OS sandbox.",
    "Integration timing must prevent simultaneous Herdr and no-mistakes branch ownership."
  ],
  "noStagedFiles": true,
  "notes": "Recommendation: complement Herdr as a post-run validation/delivery gate; do not replace Herdr orchestration. Existing unrelated unstaged/untracked files in the current repository were left untouched."
}
```
