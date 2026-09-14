# Code Context

## Files Retrieved
1. `tools/harness.py` (lines 697-704, 733-757, 899-954) - documents and implements Pi whole-runtime planning; currently handles only extensions and personas.
2. `tools/harness.py` (lines 2549-2579, 3191-3259) - defines manifest-safe home roots and generated namespaces used for orphan retirement.
3. `tools/harness.py` (lines 3949-4005, 4260-4310) - global flow gates runtime on `global: [pi]`, reconciles the Pi plan, and records links/generated sources.
4. `.shared-llm/public/llm/pi/common/` (directory listing) - portable Pi source tree; contains `agents/`, `extensions/`, settings, and third-party-extension docs/manifest, but no `themes/` directory today.
5. `tools/test_config_flow.py` (lines 1194-1210, 1490-1525, 1727-1750) - full runtime deployment, generated-tree portability, and harness-disable retirement tests.
6. `tools/test_home_manifest.py` (lines 609-632, 713-735) - verifies planning does not prematurely delete generated artifacts and guards against pruning through symlinked generated namespaces.
7. `README.md` (lines 103-111, 180-208, 292-307) - describes copy/global runtime architecture and deployed Pi runtime inventory.
8. `UPINSTALL.md` (lines 124-130) - installation documentation distinguishing kit-owned Pi runtime from `pi install` packages.

## Key Code

### Review findings

- **Medium — themes are not currently deployable by `just update`.** `plan_pi_runtime()` explicitly scans only `.shared-llm/public/llm/pi/common/agents/*.md` and `.../extensions/*`, creates generated copies under `~/.shared-llm/generated/pi/{agents,extensions}`, and returns only the agent/extension home directories for reconciliation (`tools/harness.py:899-954`). Merely adding a theme JSON under the Pi common tree would copy it to destination `public/` trees through the existing common-runtime sync, but would not install it globally.

- **Medium — retirement support must be extended with deployment support.** Generated cleanup only enumerates `pi/extensions` and `pi/agents` (`tools/harness.py:2565-2574`). A new generated `pi/themes` tree must be added to `GENERATED_DIR_NAMESPACES`; otherwise a renamed/deleted theme source would survive as an orphan. The manifest entry format itself needs no new kind: ordinary theme symlinks can use existing `record_link`, and `~/.pi` is already an allowed managed home root (`tools/harness.py:2557-2560, 3020-3062`).

- **Low — documentation currently promises only extensions/personas/settings.** `README.md:298` and `UPINSTALL.md:130` omit themes, so behavior and portable source placement would be undiscoverable without doc updates.

### Minimal change recommendation

1. Add the portable theme source as `.shared-llm/public/llm/pi/common/themes/<theme-name>.json` (validate it against Pi's theme schema separately).
2. Extend `plan_pi_runtime()` in `tools/harness.py:899-954` with:
   - source directory: `llm/pi/common/themes`
   - generated directory: `~/.shared-llm/generated/pi/themes`
   - home destination: `~/.pi/agent/themes/<theme-name>.json`
   - only regular `*.json` entries; honor the existing `exclude` predicate
   - include `~/.pi/agent/themes` in `LinkPlan.dest_dirs` so retired managed links are reconciled.
3. Add `"pi/themes"` to `GENERATED_DIR_NAMESPACES` at `tools/harness.py:2568-2574`. No manifest version/schema bump is needed because this remains a normal generated-backed `kind: link` under the already-managed `.pi` root.
4. Keep the existing `do_home_runtime()` loop (`tools/harness.py:4280-4299`): it already records every returned desired link and marks every generated source. Thus the new theme is deployed only when `global` includes `pi`, is portable because home links target the durable generated copy, and is retired by normal reconciliation when removed or Pi is disabled.
5. Update comments/docstrings near `tools/harness.py:697-704, 899-910, 4280-4282` from “extensions/personas” to “extensions/personas/themes.”

### Tests to add/update

- `tools/test_config_flow.py:1194-1210`: assert the shipped theme is a symlink at `~/.pi/agent/themes/<name>.json`, resolves under `~/.shared-llm/generated/pi/themes/`, and has source-identical bytes.
- `tools/test_config_flow.py:1490-1525`: include/assert the generated theme in the “no home link targets checkout” portability test.
- `tools/test_config_flow.py:1727-1750`: add `gen / "pi/themes"` to whole-piece retirement and ensure no theme home link survives after `global` becomes empty.
- `tools/test_home_manifest.py:609-632`: optionally add a retired generated theme fixture to prove it survives planning and is removed only in commit.
- `tools/test_home_manifest.py:713-735`: add `pi/themes` to the symlinked-generated-namespace parameterization so cleanup cannot traverse a hostile namespace symlink.

### Documentation paths

- `README.md:180-208`: identify themes as Pi whole-runtime source, not compose input.
- `README.md:292-305`: change Pi runtime inventory to extensions, personas, themes, and settings; state theme destination `~/.pi/agent/themes/` and generated source `~/.shared-llm/generated/pi/themes/`.
- `UPINSTALL.md:124-130`: mention kit-owned themes alongside kit-owned extensions and that `just update` (not `pi install`) deploys them.
- `AGENTS.md` background/runtime paragraphs should be kept consistent because this root governance file explicitly enumerates Pi whole pieces and generated namespaces, though it is not generated output.

## Architecture

`just update` runs the global home-runtime flow. When `~/.shared-llm.yaml` includes `pi` in `global`, `do_home_runtime()` calls `plan_pi_runtime()`. The planner copies authored whole pieces from the checkout into the durable machine-local generated tree, then returns desired home-link mappings. `reconcile()` creates/repoints/removes only provably managed symlinks; `HomeManifest` records those links and generated sources, then prunes stale generated artifacts after links are safely retired. Themes should follow this exact path rather than being scaffolded into settings or installed through the third-party package manifest.

## Start Here

Open `tools/harness.py:899-954`. It is the narrow planner to extend; all manifest recording is already generic downstream in `tools/harness.py:4280-4299`.

## Residual Risks

- The installed Pi package was not available in the searched global Node locations, so this scout could not locally verify the exact current theme JSON schema or whether the project’s pinned Pi version imposes required color keys. Confirm against Pi >= 0.74.0 documentation/tests before authoring the theme.
- A foreign real file or foreign symlink at the same theme destination will intentionally be preserved, meaning the portable theme can be skipped on machines with a collision; this matches existing foreign-safe runtime policy.
- `PUBLIC_COPY_DIRS` already includes all of `llm/pi/common`, so no copy-list change appears necessary; tests should still prove destination syncing if theme presence in destination `public/` is a requirement beyond global deployment.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "review-findings identify the missing theme planner and cleanup namespace with exact paths/line ranges and severity; residual-risks explicitly cover schema validation and collision behavior"
    }
  ],
  "changedFiles": [],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "grep/find/read inspection of tools/harness.py, tools/test_config_flow.py, tools/test_home_manifest.py, README.md, UPINSTALL.md, and .shared-llm/public/llm/pi/common",
      "result": "passed",
      "summary": "Confirmed existing Pi runtime data flow and that no theme source or deployment branch exists."
    },
    {
      "command": "git status --porcelain=v1; git diff --cached --name-only",
      "result": "passed",
      "summary": "No staged files; context.md was already a modified requested report artifact before replacement."
    }
  ],
  "validationOutput": [],
  "residualRisks": [
    "Exact Pi theme JSON schema was not locally verifiable because the installed package source was unavailable in searched global Node locations.",
    "Foreign destination collisions are preserved by design and may skip theme deployment."
  ],
  "noStagedFiles": true,
  "notes": "Read-only investigation: no source, test, or documentation files were edited; only the requested context.md findings artifact was written."
}
```
