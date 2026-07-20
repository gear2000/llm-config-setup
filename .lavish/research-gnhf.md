# GNHF assessment against Herdr

## Recommendation

**Use GNHF as a complement, not a replacement.** It is a strong single-agent, single-objective “Ralph loop” and could replace a *bounded implementation worker loop* where incremental commits are desirable. It cannot replace Herdr’s multi-role phase/stage routing, durable typed coordination, visible native agent panes, human decision events, or independent audit/escalation structure. A useful enhancement seam is to offer GNHF as an explicitly selected execution backend for low/medium-risk implementation slices, with Herdr retaining plan ownership, review, and acceptance.

## Concrete findings

### Execution model and orchestration boundary

- GNHF is one in-process `Orchestrator` driving one `Agent` repeatedly. Its public state/events cover iteration number, token totals, commits, failures, waiting, abort, and stop; there is no worker graph, role routing, mailbox, or phase DAG (`../gnhf/src/core/orchestrator.ts:31-79,93-115`). The loop builds one prompt, invokes the same adapter, records an iteration, applies caps/stop condition, and backs off after hard errors (`../gnhf/src/core/orchestrator.ts:244-350,390-427`).
- Each iteration is deliberately a smallest verifiable increment. Cross-iteration context is only the objective plus `.gnhf/runs/<id>/notes.md`; agents must not commit, because GNHF commits centrally (`../gnhf/src/templates/iteration-prompt.ts:25-63`; `../gnhf/src/core/orchestrator.ts:583-592`).
- **High replacement gap:** Herdr’s boundary is hierarchical and heterogeneous: the TUI owns phase decisions, a fresh phase leader owns stages, and the Recruiter owns worker lifecycle through deterministic commands and typed receipts (`.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md:1-5,75-113`; `.shared-llm/public/layers/slash-commands/common/common/herdr-phase/command.md:45-83`). GNHF has no equivalent for independent auditors, advisor profiles, per-stage model/harness selection, requester authorization, or multiple in-flight orders.
- GNHF can isolate parallel *separate runs* with Git worktrees, but does not coordinate them (`../gnhf/README.md:175-190`). This is concurrency by process/branch, not orchestration.

### UI and observability

- GNHF’s UI is a polished local alternate-screen terminal renderer. It shows elapsed time, input/output tokens, commit count, current message/error, iteration moons, terminal title, and graceful-stop hints (`../gnhf/src/renderer.ts:16-37,54-78,134-197`). It also emits a permanent exit summary and writes a lifecycle debug JSONL plus raw per-iteration agent JSONL (`../gnhf/README.md:313-318`).
- **Medium limitation:** observability is process-local and terminal/log based. There is no socket/API, remote control, pane-level process view, typed owner event queue, or multi-agent topology. Herdr intentionally runs native visible TUIs in panes and exposes them in its sidebar/remote list (`.shared-llm/public/extensions/common/herdr/justfile:1-15,74-96`).
- GNHF’s bundled skill defines “Hands-Off” and host-supervised “Companion” modes, but Companion supervision is polling/review/relaunch by an outer agent, not a runtime control protocol (`../gnhf/skills/gnhf/SKILL.md:10-55,91-111`).

### Persistence, recovery, and safety

- Run-local state is clone-local under `.gnhf/runs/<runId>/`: prompt, notes, output schema, base commit, stop condition, commit convention, debug log, and iteration JSONL. It is excluded through `.git/info/exclude`, not committed (`../gnhf/src/core/run.ts:23-49,157-178,181-241`). Resume reconstructs from these files and iteration filenames (`../gnhf/src/core/run.ts:244-270`; `../gnhf/README.md:159-161`).
- Each successful iteration becomes an unsigned Git commit; failures normally run `git reset --hard`; commit failures uniquely preserve the dirty work for the next iteration to repair (`../gnhf/README.md:153-155`; `../gnhf/src/core/orchestrator.ts:583-698`). Runtime caps and stop condition resume, but max iteration/token caps themselves are not persisted (`../gnhf/README.md:280-281`).
- **High integration risk:** forced stop, token abort, permanent errors, and ordinary failed iterations can invoke `resetHard` (`../gnhf/src/core/orchestrator.ts:197-241,529-568,683-700`). GNHF requires a clean tree in normal/current-branch modes, reducing risk, but embedding it inside a Herdr-owned worktree still requires exclusive ownership and explicit protection of Herdr result/control files. Herdr is forward-only and treats durable result files as authority, so an unscoped GNHF reset is incompatible with a shared working directory (`.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md:143-151,183-188`).
- **Medium recovery gap:** GNHF recovers iteration history, not active process ownership. There are no leases, acknowledgements, replayable mailbox events, leader liveness reconciliation, or terminal lifecycle authority comparable to Herdr’s `phase-start.json`, `phase-result.json`, event ack/re-await, and `run-terminal.json` controller publication (`.shared-llm/public/layers/slash-commands/common/common/herdr-run/command.md:105-139,206-220`).

### Claude Code compatibility

- Native compatibility is concrete: it spawns `claude -p <prompt> --verbose --output-format stream-json --json-schema <schema>` and defaults to `--dangerously-skip-permissions` unless the user supplies a permission mode (`../gnhf/src/core/agents/claude.ts:133-158,199-230`). It consumes assistant usage/messages and Claude’s structured result, then terminates a lingering process tree after a grace period (`../gnhf/src/core/agents/claude.ts:345-375`). Custom CLI-compatible binaries and extra args are supported (`../gnhf/README.md:298-310`).
- **Medium compatibility distinction:** this is headless Claude Code, not a native interactive Claude pane. It should still read repository `CLAUDE.md` through normal Claude Code behavior, but GNHF does not itself load/compose this kit’s skills or start Claude Remote Control. Herdr’s launcher explicitly starts a visible Claude TUI with Remote Control (`.shared-llm/public/extensions/common/herdr/justfile:79-96`).
- GNHF supports Claude, Codex, Copilot, Pi, Rovo Dev, OpenCode, and ACP adapters (`../gnhf/README.md:326-338`). Package metadata is lean Node >=20 ESM, version 0.1.42; runtime dependencies are only `commander` and `js-yaml`, while ACP is currently a dev dependency bundled at build/package time and should be verified in the published artifact (`../gnhf/package.json:1-48`).

## Exact integration seams

1. **Best seam — bounded Herdr worker backend:** add an opt-in roster/route execution mode for a low-risk implementation order that launches `gnhf --worktree --agent <resolved-agent> --max-iterations N --stop-when <stage acceptance> <prompt>`. GNHF owns its worktree/branch and commits; a normal Herdr worker/reviewer then translates the branch outcome into the existing `result.json` contract. Do **not** let GNHF write Recruiter receipts or phase results directly.
2. **Simpler seam — sibling skill/CLI:** install/copy `skills/gnhf/SKILL.md` through this kit and expose a separate `just gnhf-run` entry point. This complements `just herdr-plan` for tasks that do not need a canonical plan, multiple roles, independent audits, or live remote steering. The skill already formalizes outer-agent review (`../gnhf/skills/gnhf/SKILL.md:1-18,91-111`).
3. **Observability enhancement to borrow:** token/commit totals, permanent exit summary, structured lifecycle JSONL with cause chains, and terminal-title status are useful patterns Herdr could adopt without replacing its topology (`../gnhf/README.md:157-160,313-318`).
4. **Do not integrate in-place on a Herdr stage worktree:** GNHF’s automatic commits and hard resets conflict with Herdr’s stage ownership, forward-only semantics, and durable control artifacts. Isolation must be process + worktree + branch, followed by explicit review/merge.
5. **No direct replacement seam:** GNHF’s `Agent` interface (`run(prompt,cwd,{onUsage,onMessage,signal,logPath})`) is an internal adapter boundary, not a multi-agent orchestration API (`../gnhf/src/core/agents/types.ts:116-145`). Replacing Herdr would require adding a scheduler, typed durable event transport, ownership leases, phase/stage schemas, remote UI, and human decision protocol—effectively rebuilding Herdr around GNHF’s loop.

## Test evidence and residual questions

- The source has 37 `src/**/*.test.ts` files and 4 `e2e/**/*.test.ts` files. Coverage includes orchestrator failure/reset/interrupt/token behavior (`../gnhf/src/core/orchestrator.test.ts`), native adapters including Claude (`../gnhf/src/core/agents/claude.test.ts`), renderer/diff behavior (`../gnhf/src/renderer.test.ts`, `../gnhf/src/renderer-diff.test.ts`), Git/run/config, and CLI/ACP E2E (`../gnhf/e2e/e2e-cli.test.ts`, `../gnhf/e2e/e2e-acp.test.ts`).
- Tests could not be executed because `pnpm` is absent in this environment. Published-package ACP bundling, actual Claude CLI compatibility with the installed local version, crash recovery during `git commit/reset`, and behavior when Herdr control files coexist in the cwd remain runtime validation items.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Concrete execution, UI, persistence, Claude compatibility, integration findings and severity-rated risks cite exact paths and line ranges throughout this report."
    }
  ],
  "changedFiles": [
    ".lavish/research-gnhf.md"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "find ../gnhf/src -name '*.test.ts' | wc -l; find ../gnhf/e2e -name '*.test.ts' | wc -l",
      "result": "passed",
      "summary": "Found 37 source test files and 4 E2E test files."
    },
    {
      "command": "cd ../gnhf && pnpm test",
      "result": "failed",
      "summary": "Validation could not start: pnpm is not installed (exit 127)."
    },
    {
      "command": "git -C ../gnhf status --short; git diff --cached --name-only",
      "result": "passed",
      "summary": "Sibling checkout was clean and the current repository had no staged files before report creation."
    }
  ],
  "validationOutput": [
    "Static source/package/test inspection completed.",
    "pnpm test: /bin/bash: pnpm: command not found"
  ],
  "residualRisks": [
    "Test suite was not executed because pnpm is unavailable.",
    "Published npm artifact ACP bundling was not inspected.",
    "No live Claude Code or Herdr/GNHF interoperability run was performed.",
    "Automatic git reset/commit behavior requires an isolated worktree before any Herdr integration."
  ],
  "noStagedFiles": true,
  "notes": "Recommendation: complement Herdr with an isolated bounded-worker or separate CLI/skill path; do not replace Herdr. The only file written is this requested research artifact."
}
```
