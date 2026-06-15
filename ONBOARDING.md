# Onboarding Checklist

**First, see what needs you:** `find . -name 'TEMPLATE.*'` lists every unfilled template stub. For each one: fill it in (the groups below map every token to its file), **delete the `<!-- TEMPLATE … -->` banner**, then **rename it to drop the `TEMPLATE.` prefix** (e.g. `TEMPLATE.general.md` → `general.md`).

Work through the groups in order. At each step the relevant file and token(s) are listed.
When you finish, both of these must return nothing: `grep -rn '{{\|TODO(project)' .shared-llm/layers/` and `find . -name 'TEMPLATE.*'`.
Then run `task compose:all` (or `python3 tools/compose-layers.py`) to generate your output files.

---

## A — Identity and naming

1. **Project name** — replace `{{PROJECT_NAME}}` in:
   - `.shared-llm/layers/llm/this_repo/common/general.md` (title line)
   - `.shared-llm/layers/llm/this_repo/common/src/packages.md` (title line)
   - `.shared-llm/layers/skills/this_repo/python.md` (title line)

2. **Package prefix** — replace `{{PACKAGE_PREFIX}}` in:
   - `.shared-llm/layers/llm/this_repo/common/src/packages.md`
   - `.shared-llm/layers/llm/this_repo/common/src/services.md`
   - `.shared-llm/layers/skills/this_repo/python.md`

3. **Component names** — for `authoring.md` and `aws-execution-engine.md`, replace `{{COMPONENT_DESCRIPTION}}` and `{{COMPONENT_NAME}}` with a plain-English description of what each component does. Delete either file (and its recipe) if you have no equivalent component.

---

## B — Credentials and cloud

**Warning: never commit real secrets.** These files are tracked by git. Fill in the shape (paths, env var names, account IDs) — never actual token values.

4. **Credential root path** — replace `{{CRED_ROOT}}` with the path to your secrets directory (e.g. `~/project/secrets/`). File: `.shared-llm/layers/llm/this_repo/common/general.md`, `## Credentials` section.

5. **Cloud region** — replace `{{CLOUD_REGION}}` (e.g. `us-east-1`, `eu-west-1`). File: `.shared-llm/layers/llm/this_repo/common/general.md`.

6. **Cloud account IDs** — replace `{{ACCOUNT_SAAS}}` and `{{ACCOUNT_TENANT}}` with your account identifiers (numeric IDs, project names, etc.). File: `.shared-llm/layers/llm/this_repo/common/general.md`.

7. **Credential entries** — fill in the bullet list under `## Credentials` with one entry per credential: name → env var → path. Keep entries to one line each.

---

## C — CI, build, deploy, and PyPI tooling

8. **CI build tool** — replace `{{CI_BUILD_TOOL}}` with your push-triggered CI system name (e.g. `GitHub Actions`, a self-hosted CI tool). Files: `general.md`, `src/packages.md`, `src/services.md`, `python.md`.

9. **CI deploy tool** — replace `{{CI_DEPLOY_TOOL}}` with your deploy automation system (e.g. a deploy pipeline or automation server). Same files.

10. **Test runner task** — replace `{{TEST_TASK}}` with your Taskfile target for running package tests (e.g. `task pkg:<name>:test:image`). File: `.shared-llm/layers/skills/this_repo/python.md`.

11. **PyPI URLs** — replace `{{PYPI_INDEX_URL}}` and `{{PYPI_INDEX_URL_AUTH}}` with your internal registry URLs (unauthenticated and authenticated forms). Files: `src/packages.md`, `src/services.md`, `python.md`. Replace `{{PYPI_HOST}}` with the registry hostname.

12. **Deploy script** — in `.shared-llm/layers/llm/this_repo/common/src/services.md`, fill in the `## Deploy gate` TODO: replace `{{DEPLOY_SCRIPT}}` with the path to your deploy helper (e.g. `tools/deploy.sh`).

---

## D — Layout, worktrees, and docs-check

13. **Worktree roots** — replace `{{WORKTREE_ROOT_CODE}}`, `{{WORKTREE_ROOT_OPS}}`, and `{{WORKTREE_ROOT_INFRA}}` with the actual paths on your machine. Delete the ops/infra lines if you have a single-repo layout. File: `.shared-llm/layers/llm/this_repo/claude/claude.md`.

14. **Source glob** — replace `{{SOURCE_GLOB}}` with the path prefix for source files that trigger docs updates (e.g. `src/(packages|services)/`). File: `.shared-llm/layers/llm/this_repo/claude/claude.md`.

15. **Docs update skill** — replace `{{DOCS_UPDATE_SKILL}}` with the skill name you use to update per-package/service docs (e.g. `/update-docs`). Delete the docs-check block entirely if you have no per-component docs. File: `.shared-llm/layers/llm/this_repo/claude/claude.md`.

16. **Ops and infra repo names** — replace `{{OPS_REPO}}` and `{{INFRA_REPO}}` with your sibling repo names, or delete those `Key paths` bullets if you have a single-repo layout. File: `.shared-llm/layers/llm/this_repo/common/general.md`.

---

## E — Cross-harness agents.md (adopt or delete)

17. **Decide** — `.shared-llm/layers/llm/this_repo/codex/agents.md` is a skeleton for documenting shared-skills wiring across Claude Code, Codex, Pi, etc. If you use a single harness only, delete this file and remove `agents.md` from the inputs list in `.shared-llm/compose/agents-md/root.yaml`. If you adopt it, fill in the three sections (`## Skills`, `## Hooks and MCP servers`, `## Session memories`) with your actual wiring.

---

## F — Per-package and per-service leaves

18. **Copy `example_package.md` once per package** — for each package under `src/packages/`, copy `.shared-llm/layers/llm/this_repo/common/packages/example_package.md` to `.shared-llm/layers/llm/this_repo/common/packages/<package_name>.md`. Fill in `{{PACKAGE_NAME}}`, the type (Library/Service), notable modules, and gotchas.

19. **Copy `example_service.md` once per service** — for each service under `src/services/`, copy `.shared-llm/layers/llm/this_repo/common/services/example_service.md` to `.shared-llm/layers/llm/this_repo/common/services/<service_name>.md`. Fill in `{{SERVICE_NAME}}`, the invocation type, entry points, and gotchas.

20. **Create matching recipes** — for each leaf file created in steps 18–19, copy the corresponding example recipe:
    - Package: copy `.shared-llm/compose/claude-md/example-package.yaml` → `.shared-llm/compose/claude-md/packages/<package_name>.yaml`. Update `inputs` and `output`.
    - Service: copy `.shared-llm/compose/claude-md/example-service.yaml` → `.shared-llm/compose/claude-md/services/<service_name>.yaml`. Update `inputs` and `output`.
    - Do the same under `.shared-llm/compose/agents-md/` if you produce per-component `AGENTS.md` files too.

---

## G — Package hierarchy and service catalog

21. **Package hierarchy** — fill in the tier ladder in `.shared-llm/layers/llm/this_repo/common/src/packages.md` under `## Package hierarchy`. List every package grouped by dependency tier.

22. **Service catalog** — fill in the service list in `.shared-llm/layers/llm/this_repo/common/src/services.md` under `## Service catalog`. Group by type (frontend, Lambda, CLI, etc.).

23. **Gotchas** — fill in the `## Gotchas` sections in `src/packages.md` and `src/services.md` with project-specific naming quirks, historical exceptions, and non-obvious conventions.

24. **Shared utilities package** — replace `{{SHARED_UTIL_PACKAGE}}` in `.shared-llm/layers/skills/this_repo/python.md` with your consolidation target package name (e.g. `myapp_commons`). This is the package where utility functions used by 2+ packages live.

---

## H — Finalize

25. **Leak check** — run:
    ```bash
    grep -rn '{{\|TODO(project)' .shared-llm/layers/
    ```
    Must return nothing. Fix any remaining placeholders.

26. **Compose** — run:
    ```bash
    task compose:all
    # or:
    python3 tools/compose-layers.py
    ```
    Confirm it exits without errors and writes the expected output files.

27. **Review outputs** — open `CLAUDE.md`, `AGENTS.md`, and `.claude/skills/python/SKILL.md`. Read them as an LLM would. Adjust layer prose until the generated content reads naturally and accurately describes your project.

28. **Commit generated files** — unlike this kit (which gitignores its own example outputs), your consumer repo should commit the generated `CLAUDE.md`, `AGENTS.md`, and skill files. They are the deliverables. Remove the relevant lines from `.gitignore` before committing.

---

## I — Pi harness (optional)

If you use the [Pi coding agent](https://github.com/earendil-works/pi-mono), this kit ships ready-to-use Pi runtime config under `.shared-llm/llm/pi/common/`.

29. **Wire it up** — run once per machine after cloning:
    ```bash
    task setup:pi
    ```
    This symlinks the bundled OWN extensions (including the `memsearch/` directory) and agent personas into `~/.pi/`, scaffolds `~/.pi/agent/settings.json` from the template (if absent), and installs the THIRD-PARTY extensions by running `tools/install-pi-extensions.sh`, which `pi install`s each pinned source from `.shared-llm/llm/pi/common/third-party-extensions.txt` (skipping any already present). There is no `npm ci` / vendored `node_modules` step — `pi install` fetches each extension itself. See `.shared-llm/llm/pi/common/THIRD-PARTY-EXTENSIONS.md`.

30. **Customize settings** — open `.shared-llm/llm/pi/common/settings.template.json` and adjust `defaultProvider`, `defaultModel`, and `defaultThinkingLevel` to match your environment. The template is applied only when `~/.pi/agent/settings.json` does not exist; edit your live settings file directly after first run. Its `packages` array starts empty on purpose — the third-party installer fills it; the pinned manifest (`third-party-extensions.txt`) is the single source of truth for the extension set.

31. **Extensions and agents** — `context-workflow.ts`, `iac-guard.ts`, the `memsearch/` extension, `codex-reviewer-hub.ts`, `codex-reviewer.md`, and `iac-verifier.md` are reusable as-is. They contain no project-specific references; you can adopt them without modification. (`memsearch` additionally needs the `memsearch` CLI on `PATH` or `uvx` available; without either it no-ops silently.)

32. **IaC safety gate** — `iac-guard.ts` auto-loads in every Pi session and forces human approval before any destructive `terraform` / `tofu` / `aws` / `kubectl` command runs (gray-zone updates are judged by the `iac-verifier` agent over the hub socket; fail-closed if the hub is down). Tune the allow/ask/gray verb tables at the top of `iac-guard.ts`. Launch with `task up` (needs `tmux`); `task status` / `task clean` manage the hub.
