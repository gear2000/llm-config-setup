# Onboarding Checklist

**First, see what needs you:** `find . -name 'TEMPLATE.*'` lists every unfilled template stub. For each one: fill it in (the groups below map every token to its file), **delete the `<!-- TEMPLATE … -->` banner**, then **rename it to drop the `TEMPLATE.` prefix** (e.g. `TEMPLATE.general.md` → `general.md`).

Work through the groups in order. At each step the relevant file and token(s) are listed.
When you finish, both of these must return nothing: `grep -rn '{{\|TODO(project)' layers/` and `find . -name 'TEMPLATE.*'`.
Then run `task compose:all` (or `python3 tools/compose-layers.py`) to generate your output files.

---

## A — Identity and naming

1. **Project name** — replace `{{PROJECT_NAME}}` in:
   - `layers/llm/this_repo/general.md` (title line)
   - `layers/llm/this_repo/src/packages.md` (title line)
   - `layers/skills/this_repo/python.md` (title line)

2. **Package prefix** — replace `{{PACKAGE_PREFIX}}` in:
   - `layers/llm/this_repo/src/packages.md`
   - `layers/llm/this_repo/src/services.md`
   - `layers/skills/this_repo/python.md`

3. **Component names** — for `authoring.md` and `aws-execution-engine.md`, replace `{{COMPONENT_DESCRIPTION}}` and `{{COMPONENT_NAME}}` with a plain-English description of what each component does. Delete either file (and its recipe) if you have no equivalent component.

---

## B — Credentials and cloud

**Warning: never commit real secrets.** These files are tracked by git. Fill in the shape (paths, env var names, account IDs) — never actual token values.

4. **Credential root path** — replace `{{CRED_ROOT}}` with the path to your secrets directory (e.g. `~/project/secrets/`). File: `layers/llm/this_repo/general.md`, `## Credentials` section.

5. **Cloud region** — replace `{{CLOUD_REGION}}` (e.g. `us-east-1`, `eu-west-1`). File: `layers/llm/this_repo/general.md`.

6. **Cloud account IDs** — replace `{{ACCOUNT_SAAS}}` and `{{ACCOUNT_TENANT}}` with your account identifiers (numeric IDs, project names, etc.). File: `layers/llm/this_repo/general.md`.

7. **Credential entries** — fill in the bullet list under `## Credentials` with one entry per credential: name → env var → path. Keep entries to one line each.

---

## C — CI, build, deploy, and PyPI tooling

8. **CI build tool** — replace `{{CI_BUILD_TOOL}}` with your push-triggered CI system name (e.g. `GitHub Actions`, a self-hosted CI tool). Files: `general.md`, `src/packages.md`, `src/services.md`, `python.md`.

9. **CI deploy tool** — replace `{{CI_DEPLOY_TOOL}}` with your deploy automation system (e.g. a deploy pipeline or automation server). Same files.

10. **Test runner task** — replace `{{TEST_TASK}}` with your Taskfile target for running package tests (e.g. `task pkg:<name>:test:image`). File: `layers/skills/this_repo/python.md`.

11. **PyPI URLs** — replace `{{PYPI_INDEX_URL}}` and `{{PYPI_INDEX_URL_AUTH}}` with your internal registry URLs (unauthenticated and authenticated forms). Files: `src/packages.md`, `src/services.md`, `python.md`. Replace `{{PYPI_HOST}}` with the registry hostname.

12. **Deploy script** — in `layers/llm/this_repo/src/services.md`, fill in the `## Deploy gate` TODO: replace `{{DEPLOY_SCRIPT}}` with the path to your deploy helper (e.g. `tools/deploy.sh`).

---

## D — Layout, worktrees, and docs-check

13. **Worktree roots** — replace `{{WORKTREE_ROOT_CODE}}`, `{{WORKTREE_ROOT_OPS}}`, and `{{WORKTREE_ROOT_INFRA}}` with the actual paths on your machine. Delete the ops/infra lines if you have a single-repo layout. File: `layers/llm/this_repo/claude.md`.

14. **Source glob** — replace `{{SOURCE_GLOB}}` with the path prefix for source files that trigger docs updates (e.g. `src/(packages|services)/`). File: `layers/llm/this_repo/claude.md`.

15. **Docs update skill** — replace `{{DOCS_UPDATE_SKILL}}` with the skill name you use to update per-package/service docs (e.g. `/update-docs`). Delete the docs-check block entirely if you have no per-component docs. File: `layers/llm/this_repo/claude.md`.

16. **Ops and infra repo names** — replace `{{OPS_REPO}}` and `{{INFRA_REPO}}` with your sibling repo names, or delete those `Key paths` bullets if you have a single-repo layout. File: `layers/llm/this_repo/general.md`.

---

## E — Cross-harness agents.md (adopt or delete)

17. **Decide** — `layers/llm/this_repo/agents.md` is a skeleton for documenting shared-skills wiring across Claude Code, Codex, Pi, etc. If you use a single harness only, delete this file and remove `agents.md` from the inputs list in `layers/compose/agents-md/root.yaml`. If you adopt it, fill in the three sections (`## Skills`, `## Hooks and MCP servers`, `## Session memories`) with your actual wiring.

---

## F — Per-package and per-service leaves

18. **Copy `example_package.md` once per package** — for each package under `src/packages/`, copy `layers/llm/this_repo/packages/example_package.md` to `layers/llm/this_repo/packages/<package_name>.md`. Fill in `{{PACKAGE_NAME}}`, the type (Library/Service), notable modules, and gotchas.

19. **Copy `example_service.md` once per service** — for each service under `src/services/`, copy `layers/llm/this_repo/services/example_service.md` to `layers/llm/this_repo/services/<service_name>.md`. Fill in `{{SERVICE_NAME}}`, the invocation type, entry points, and gotchas.

20. **Create matching recipes** — for each leaf file created in steps 18–19, copy the corresponding example recipe:
    - Package: copy `layers/compose/claude-md/example-package.yaml` → `layers/compose/claude-md/packages/<package_name>.yaml`. Update `inputs` and `output`.
    - Service: copy `layers/compose/claude-md/example-service.yaml` → `layers/compose/claude-md/services/<service_name>.yaml`. Update `inputs` and `output`.
    - Do the same under `layers/compose/agents-md/` if you produce per-component `AGENTS.md` files too.

---

## G — Package hierarchy and service catalog

21. **Package hierarchy** — fill in the tier ladder in `layers/llm/this_repo/src/packages.md` under `## Package hierarchy`. List every package grouped by dependency tier.

22. **Service catalog** — fill in the service list in `layers/llm/this_repo/src/services.md` under `## Service catalog`. Group by type (frontend, Lambda, CLI, etc.).

23. **Gotchas** — fill in the `## Gotchas` sections in `src/packages.md` and `src/services.md` with project-specific naming quirks, historical exceptions, and non-obvious conventions.

24. **Shared utilities package** — replace `{{SHARED_UTIL_PACKAGE}}` in `layers/skills/this_repo/python.md` with your consolidation target package name (e.g. `myapp_commons`). This is the package where utility functions used by 2+ packages live.

---

## H — Finalize

25. **Leak check** — run:
    ```bash
    grep -rn '{{\|TODO(project)' layers/
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

If you use the [Pi coding agent](https://github.com/earendil-works/pi-mono), this kit ships ready-to-use Pi runtime config under `layers/llm/pi/common/`.

29. **Wire it up** — run once per machine after cloning:
    ```bash
    task setup:pi
    ```
    This symlinks the bundled extensions and codex-reviewer agent into `~/.pi/`, scaffolds `~/.pi/agent/settings.json` from the template (if absent), and installs npm dependencies via `npm ci` (if `node_modules` is missing).

30. **Customize settings** — open `layers/llm/pi/common/settings.template.json` and adjust `defaultProvider`, `defaultModel`, `defaultThinkingLevel`, and `packages` to match your environment. The template is applied only when `~/.pi/agent/settings.json` does not exist; edit your live settings file directly after first run.

31. **Extensions and agent** — `context-workflow.ts`, `codex-reviewer-hub.ts`, and `codex-reviewer.md` are reusable as-is. They contain no project-specific references; you can adopt them without modification.
