# Onboarding Checklist

This walks you from a clean machine to generated `CLAUDE.md` / `AGENTS.md` / skill files for your project. The primary path is the two install commands; the fill-in checklist (groups A–H) covers every TEMPLATE token.

---

## 0 — Install

**Prerequisites.** Install these first:

- `task` (go-task) — the single entrypoint for every command below.
- `python3.14` — runs the compose/sync engine. Set `PYTHON_BIN=/path/to/python3.14` if it is not on `PATH` as `python3.14`.
- PyYAML — the engine's one Python dependency: `python3.14 -m pip install pyyaml`.

The Pi runtime extras (`pi` binary, `tmux`) are optional — needed only for the Pi flow.

Two install surfaces. Run both for a fresh setup.

1. **Home pieces (all projects):**
   ```bash
   task install local
   ```
   Installs the general home skills (`python`, `nextjs`, `backend`, `golang`) plus routed slash-command skills. Pi standalone planning is `/planish` from the TypeScript extension; Pi workflow-suite commands are `/do-research`, `/do-plan-and-grill`, `/do-oneshot`, `/do-implement`, `/do-loop`, and `/do-full`; Claude Code gets the matching `cc-*` workflow-suite commands. The old standalone skill variants `/do-planish` and `/cc-planish` are intentionally removed. It also installs the 18 generic agents into `~/.claude/agents` and `~/.pi/agents`, the Pi runtime into `~/.pi`, and the `llm-compose` wrapper into `~/.local/bin`. Idempotent and non-clobbering. Add `-- --skip-pi-extensions` to skip the `pi install` network step.

2. **Your target repo (per repo):**
   ```bash
   task install repo -- /path/to/your/repo
   ```
   Copies the portable `.shared-llm/` layer tree, the compose engine, and a thin compose Taskfile into the repo, then prints the `TEMPLATE.*` stubs you must fill and pauses. (Set `INSTALL_REPO_YES=1` to skip the pause for automation.) It composes automatically once every stub is filled and renamed; while stubs remain, compose is skipped and the remaining stubs are reported.

After `install repo`, the rest of this checklist happens **inside your target repo** — that is where the `.shared-llm/` tree and engine now live.

**See what still needs you:** `find . -name 'TEMPLATE.*'` lists every unfilled stub. For each: fill it in (the groups below map every token to its file), **delete the `<!-- TEMPLATE … -->` banner**, then **rename it to drop the `TEMPLATE.` prefix** (e.g. `TEMPLATE.general.md` → `general.md`).

When you finish, both of these must return nothing:
```bash
grep -rn '{{\|TODO(project)' .shared-llm/layers/
find . -name 'TEMPLATE.*'
```
Then run `task compose:all` (or `llm-compose`) to generate your output files.

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

## H — Compose and the output convention

25. **Leak check** — run:
    ```bash
    grep -rn '{{\|TODO(project)' .shared-llm/layers/
    ```
    Must return nothing. Fix any remaining placeholders.

26. **Compose** — run:
    ```bash
    task compose:all
    # or, with the wrapper installed by `install local`:
    llm-compose --target . .shared-llm/compose/claude-md/root.yaml
    ```

27. **Where the outputs land — at your repo root, no manual move.** Recipe `output:` paths are **root-relative**, and `install repo` (and the thin Taskfile's `task compose:all`) compose with `--target .`. So the generated files land exactly where each harness reads them:

    - `CLAUDE.md` and `AGENTS.md` at the repo **root**
    - the per-repo Python skill at `.claude/skills/python/SKILL.md`
    - the 18 generic agent personas at `.claude/agents/<name>.md`

    `task compose:all` (the thin Taskfile copied in by `install repo`) composes only the **consumer-relevant** recipe groups — root `CLAUDE.md`/`AGENTS.md`, the skills, and the agents. It deliberately does **not** compose:

    - the home-only `global/` skills (`python`/`nextjs`/`backend`) — those install into `~/` via `task install local`, not into your repo;
    - the `example-package` / `example-service` **demo** recipes — illustrative samples only. If you want to see them, compose by hand into a throwaway area (`llm-compose --target /tmp/samples .shared-llm/compose/claude-md/example-package.yaml`); they are not deliverables and never land in your real `src/` tree.

    To produce a `CLAUDE.md` for one of your **real** packages/services, copy a leaf layer + its recipe (group F above) and point the new recipe's `output:` at the real path (e.g. `src/packages/<name>/CLAUDE.md`).

---

## I — Pi planning output directory (optional)

The Pi `/planish` command is the standalone TypeScript extension/register planner with browser-backed grill/review tools. It writes a `plan.md` + `plan.html` pair somewhere. Without configuration it defaults to `/tmp/planish/{date}/{slug}`, which is fine for throwaway use but inconvenient when you want plans versioned next to your work. For workflow-suite planning, use `/do-plan-and-grill` in Pi or `/cc-plan-and-grill` in Claude Code; `/do-planish` and `/cc-planish` are intentionally removed.

Put a `.planish.yaml` at your repo root (or any ancestor directory) to control where plans land:

```yaml
# .planish.yaml — controls where /planish writes plan.md + plan.html
dir: docs/plans/{date}/{slug}/v{n}
```

**`dir` is the only field.** The path resolves relative to the directory that holds `.planish.yaml`. Pi walks upward from cwd to find it, so one file at the repo root covers everything inside.

**Available tokens:**

| Token | Value |
|---|---|
| `{date}` | Today's date — `YYYY-MM-DD` |
| `{slug}` | Your topic, lowercased and hyphenated |
| `{n}` | Next version integer — auto-incremented by scanning siblings |

**Example outputs** for `dir: ops/mkdocs/docs/work-log/{date}/{slug}/plan` and topic `"redesign auth flow"`:

```
ops/mkdocs/docs/work-log/2026-06-29/redesign-auth-flow/plan/plan.md
ops/mkdocs/docs/work-log/2026-06-29/redesign-auth-flow/plan/plan.html
```

You can override the config file for a single run with `--dir <path>` passed to `/planish`, or by setting `$PLANISH_DIR`.

28. **Review outputs** — open the generated `CLAUDE.md`, `AGENTS.md`, and skill files at the repo root. Read them as an LLM would. Adjust the layer prose until the generated content reads naturally and accurately describes your project, then recompose.

29. **Commit the generated files** — your consumer repo should commit the generated `CLAUDE.md`, `AGENTS.md`, skill, and agent files. They are the deliverables and they sit at the locations each harness reads. (This kit, by contrast, gitignores its own `examples/` staging because it composes itself only to test the engine — it keeps a hand-maintained `CLAUDE.md` of its own.)

---

## I — Pi harness (optional)

`task install local` already wires the Pi runtime for you. The notes below cover customizing it.

30. **Re-wire on demand** — `task setup:pi` symlinks the bundled OWN extensions (including the `memsearch/` directory) and agent personas into `~/.pi/`, scaffolds `~/.pi/agent/settings.json` from the template (if absent), and installs the THIRD-PARTY extensions by running `tools/install-pi-extensions.sh`, which `pi install`s each pinned source from `.shared-llm/llm/pi/common/third-party-extensions.txt` (skipping any already present). There is no `npm ci` / vendored `node_modules` step. See `.shared-llm/llm/pi/common/THIRD-PARTY-EXTENSIONS.md`.

31. **Customize settings** — open `.shared-llm/llm/pi/common/settings.template.json` and adjust `defaultProvider`, `defaultModel`, and `defaultThinkingLevel` to match your environment. The template is applied only when `~/.pi/agent/settings.json` does not exist; edit your live settings file directly after first run. Its `packages` array starts empty on purpose — the third-party installer fills it; the pinned manifest (`third-party-extensions.txt`) is the single source of truth for the extension set.

32. **Extensions and agents** — `context-workflow.ts`, `iac-guard.ts`, the `memsearch/` extension, the review hub extensions (`codex-reviewer-hub.ts`, `doc-review-hub.ts`, `pr-review-hub.ts`), and the review agent personas (`codex-reviewer.md`, `doc-reviewer.md`, `pr-reviewer.md`, `iac-verifier.md`) are reusable as-is. They contain no project-specific references; adopt them without modification. (`memsearch` additionally needs the `memsearch` CLI on `PATH` or `uvx` available; without either it no-ops silently.)

33. **IaC safety gate** — `iac-guard.ts` auto-loads in every Pi session and forces human approval before any destructive `terraform` / `tofu` / `aws` / `kubectl` command runs (gray-zone updates are judged by the `iac-verifier` agent over the hub socket; fail-closed if the hub is down). Tune the allow/ask/gray verb tables at the top of `iac-guard.ts`. Launch with `task up` (needs `tmux`); `task status` / `task clean` manage the hub.
