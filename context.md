# Code Context

## Files Retrieved
1. `.shared-llm/public/llm/pi/common/extensions/` (directory; see `tools/harness.py:744-754`) - exact authored-source directory; add the generic non-hub extension here as a `.ts` file.
2. `tools/harness.py` (lines 729-755, 766-816, 2035-2081) - runtime plan, destination selection, reconciliation, and global deployment call.
3. `tools/test_config_flow.py` (lines 607-683, 757-770) - pytest conventions for isolated HOME deployment and extension exclusion.
4. `.shared-llm/public/llm/pi/common/extensions/memsearch.test.ts` (lines 1-45) - adjacent zero-dependency TypeScript unit-test convention.
5. `justfile` (lines 88-98) - test commands and Node native type-stripping convention.
6. `README.md` (lines 376-406, 418-420) - primary Pi runtime inventory, ownership/install-path, wiring, and foreign-file behavior documentation.
7. `ONBOARDING.md` (lines 217-225) - adoption/customization documentation for globally wired Pi runtime extensions.
8. `AGENTS.md` (lines 43-46) - repository rule that Pi runtime is whole-file source, not a compose input.

## Key Code
- `tools/harness.py:744-755`: `plan_pi_runtime()` enumerates every direct child of `.shared-llm/public/llm/pi/common/extensions/`; directories go to `~/.pi/agent/extensions/`, and `.ts` files other than `*.test.ts` go there unless named `*-hub.ts`/`hub-*`, which go to `~/.pi/extensions/`.
- `tools/harness.py:766-816`: `reconcile()` creates missing links, re-points managed links, preserves foreign files/links, and scans desired destination directories to prune stale managed links.
- `tools/harness.py:2035-2081`: `do_home_runtime()` runs only for configured global harnesses; when `pi` is wanted it builds the plan, applies configured exclusions, and reconciles it.
- `tools/test_config_flow.py:667-683`: `test_home_runtime_installs_claude_and_pi_runtime` patches HOME to `tmp_path`, calls `do_home_runtime`, and asserts an authored Pi extension is symlinked under `~/.pi/agent/extensions/`.
- `tools/test_config_flow.py:757-770`: exclusion test uses a source-relative extension path and verifies it is absent while other extensions remain.
- `.shared-llm/public/llm/pi/common/extensions/memsearch.test.ts:1-45`: pure extension logic is tested without Pi runtime using imports, counters/failure collection, stdout summary, and nonzero exit on failure.
- `justfile:88-98`: Python flow tests run with pytest; TypeScript tests run with `node --experimental-strip-types`, with focused test recipes used for extension-local tests.

## Architecture
- Author the generic extension as `.shared-llm/public/llm/pi/common/extensions/<name>.ts` (`AGENTS.md:45`; `tools/harness.py:744-754`). A model-relative compaction ratio belongs in this runtime code, derived from the active model/context-window metadata rather than a fixed token threshold.
- No compose recipe, generated output, third-party manifest entry, or installer change is involved (`README.md:376-377,393-400`).
- `just update` reaches `do_home_runtime()` when `global:` includes `pi`; reconciliation symlinks a normal `.ts` extension to `~/.pi/agent/extensions/<name>.ts` (`README.md:402-406`; `tools/harness.py:2035-2081`).
- Add pure ratio/threshold/event-decision coverage beside the extension as `<name>.test.ts`, following `memsearch.test.ts:1-45`; add it to the `justfile:88-98` test surface. Extend `tools/test_config_flow.py:667-683` only if explicit deployment discovery for the new filename is desired.
- Document the new extension in the runtime inventory at `README.md:379-391`; update onboarding only if users need configuration/behavior guidance (`ONBOARDING.md:217-225`).

## Start Here
`.shared-llm/public/llm/pi/common/extensions/` — the extension is an authored whole runtime file, and `tools/harness.py:744-754` discovers it automatically by filename/type.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Scouting-only request completed without implementation or scope expansion; exact source, deployment, tests, and docs are cited above."
    }
  ],
  "changedFiles": ["context.md"],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "targeted find/grep/read inspection; git status --porcelain; git diff --cached --name-only",
      "result": "passed",
      "summary": "Located runtime and test paths; confirmed the index has no staged files."
    }
  ],
  "validationOutput": ["No repository implementation files modified; only requested context.md written."],
  "residualRisks": ["Pi extension event/model context APIs are not demonstrated by the deployment code and must be verified against the installed Pi API when implementing auto-compaction."],
  "noStagedFiles": true,
  "notes": "The worktree already contained unrelated modified/untracked files; none were changed or staged by this task."
}
```
