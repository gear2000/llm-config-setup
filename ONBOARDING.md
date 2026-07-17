# Onboarding Checklist

This walks you from a clean machine to generated `CLAUDE.md` / `AGENTS.md` / skill files for your project. The model: **one centralized engine in the kit** (`tools/harness.py`, driven by `just`) reads `~/.shared-llm.yaml` and builds every repo you register. You seed your repo-owned `this_repo/` tree once, fill the TEMPLATE stubs (groups A–H below), register the repo, then run `just update`.

> **The destination split.** A registered destination's `.shared-llm/` has two trees (see the README's *destination split* section): **`public/`** — kit-synced, rebuilt by the engine on every `just update`, never hand-edited; and **`this_repo/`** — repo-owned, where your fillable stubs and recipes live. **This checklist is about `this_repo/`** — every file path below is one you edit or create there. The kit-synced `common` layers your recipes reference (kept under `public/`, never yours to edit) stay untouched. You do **not** create `public/`; the engine builds it in step 2 below.

---

## 0 — Install

**Prerequisites.** Install these first:

- `just` — the single entrypoint for every command below. Check with `just init -o ubuntu` (or `-o mac`).
- `python3` — runs the engine. Set `PYTHON_BIN=/path/to/python3` if it is not on `PATH` as `python3`.
- PyYAML — the engine's one Python dependency: `python3 -m pip install pyyaml`.

The Pi runtime extras (`pi` binary, `tmux`) are optional — needed only for the Pi flow.

The engine lives **only** in the kit and is never copied into your repo. You drive everything with `just` from the kit checkout, against a config file it maintains, `~/.shared-llm.yaml`.

1. **Point the engine at the source hub, and set the global (home) harness list:**

   ```bash
   just configure -s ~/.shared-llm
   just configure -g cc,pi
   ```

   The **global** step (run as part of `just update` when `-g` is set) installs the general home skills (`python`, `nextjs`, `backend`, `golang`) plus routed slash-command skills into `~/.claude/skills`, `~/.pi/agent/skills`, and `~/.agents/skills`. Pi standalone planning is `/do-planish` from the TypeScript extension; Pi workflow-suite commands are `/do-research`, `/do-plan-and-grill`, and `/do-full`; Claude Code gets the matching `cc-*` commands, including the standalone `/cc-planish` planner (the port of `/do-planish`). The global step also installs the generic agents (see the Inventory section in README) into `~/.claude/agents` and `~/.pi/agents`, the Pi runtime into `~/.pi`, and the Claude hooks/statusline/settings into `~/.claude`. It is idempotent and non-clobbering. Third-party Pi extensions are a separate step: `just pi-extensions`.

2. **Set up your target repo (per repo):**
   - Seed the repo-owned tree: copy the kit's `this_repo/` layer stubs under `<repo>/.shared-llm/this_repo/layers/` (mirroring the kit's `layers/*/this_repo/` structure) and any repo-owned recipes under `<repo>/.shared-llm/this_repo/compose/`. They arrive as fillable `TEMPLATE.*` stubs. Leave `public/` alone — the engine creates it.
   - Fill the stubs (groups A–H below), renaming each to drop the `TEMPLATE.` prefix.
   - Register the repo and build it:

     ```bash
     just configure -d /path/to/your/repo -l cc,pi
     just update
     ```

     (If a kit-synced `public/` layer carries a `{{TOKEN}}`, add a `placeholders:` map to this repo's entry in `~/.shared-llm.yaml` — see group B below and the README's *Placeholder convention*.)

The rest of this checklist happens **inside your target repo** — that is where the `.shared-llm/` tree now lives. `just update` (re)builds the `public/` tree from the kit (never touching your `this_repo/` tree), composes its output files, and wires the per-harness skill links.

**See what still needs you:** `find . -name 'TEMPLATE.*'` lists every unfilled stub. For each: fill it in (the groups below map every token to its file), **delete the `<!-- TEMPLATE … -->` banner**, then **rename it to drop the `TEMPLATE.` prefix** (e.g. `TEMPLATE.general.md` → `general.md`).

When you finish, both of these must return nothing:

```bash
grep -rn '{{\|TODO(project)' .shared-llm/this_repo/
find . -name 'TEMPLATE.*'
```

Then run `just update` to (re)generate every registered destination's output files.

---

## A — Identity and naming

1. **Project name** — replace `{{PROJECT_NAME}}` in:
   - `.shared-llm/this_repo/layers/llm/this_repo/common/general.md` (title line)
   - `.shared-llm/this_repo/layers/llm/this_repo/common/src/packages.md` (title line)
   - `.shared-llm/this_repo/layers/skills/this_repo/python.md` (title line)

2. **Package prefix** — replace `{{PACKAGE_PREFIX}}` in:
   - `.shared-llm/this_repo/layers/llm/this_repo/common/src/packages.md`
   - `.shared-llm/this_repo/layers/llm/this_repo/common/src/services.md`
   - `.shared-llm/this_repo/layers/skills/this_repo/python.md`

3. **Component names** — for `authoring.md` and `aws-execution-engine.md`, replace `{{COMPONENT_DESCRIPTION}}` and `{{COMPONENT_NAME}}` with a plain-English description of what each component does. Delete either file (and its recipe) if you have no equivalent component.

---

## B — Credentials and cloud

**Warning: never commit real secrets.** These files are tracked by git. Fill in the shape (paths, env var names, account IDs) — never actual token values.

> **Two fill paths.** The tokens in this group live in *your* `this_repo/` layers, so you fill them **by hand** here. If instead a **kit-synced `public/` layer** (a shared layer you do not own) carries a `{{TOKEN}}`, do not edit it — the engine fills it at build time from the destination's `placeholders:` map in `~/.shared-llm.yaml` (see the README's *Placeholder convention*), and stops the build if the value is missing. Either way, real values live only in your home config / secrets, never in a committed layer.

1. **Credential root path** — replace `{{CRED_ROOT}}` with the path to your secrets directory (e.g. `~/project/secrets/`). File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`, `## Credentials` section.

2. **Cloud region** — replace `{{CLOUD_REGION}}` (e.g. `us-east-1`, `eu-west-1`). File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`.

3. **Cloud account IDs** — replace `{{ACCOUNT_SAAS}}` and `{{ACCOUNT_TENANT}}` with your account identifiers (numeric IDs, project names, etc.). File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`.

4. **Credential entries** — fill in the bullet list under `## Credentials` with one entry per credential: name → env var → path. Keep entries to one line each.

---

## C — CI, build, deploy, and PyPI tooling

1. **CI build tool** — replace `{{CI_BUILD_TOOL}}` with your push-triggered CI system name (e.g. `GitHub Actions`, a self-hosted CI tool). Files: `general.md`, `src/packages.md`, `src/services.md`, `python.md`.

2. **CI deploy tool** — replace `{{CI_DEPLOY_TOOL}}` with your deploy automation system (e.g. a deploy pipeline or automation server). Same files.

3. **Test runner task** — replace `{{TEST_TASK}}` with your Taskfile target for running package tests (e.g. `task pkg:<name>:test:image`). File: `.shared-llm/this_repo/layers/skills/this_repo/python.md`.

4. **PyPI URLs** — replace `{{PYPI_INDEX_URL}}` and `{{PYPI_INDEX_URL_AUTH}}` with your internal registry URLs (unauthenticated and authenticated forms). Files: `src/packages.md`, `src/services.md`, `python.md`. Replace `{{PYPI_HOST}}` with the registry hostname.

5. **Deploy script** — in `.shared-llm/this_repo/layers/llm/this_repo/common/src/services.md`, fill in the `## Deploy gate` TODO: replace `{{DEPLOY_SCRIPT}}` with the path to your deploy helper (e.g. `tools/deploy.sh`).

---

## D — Layout, worktrees, and docs-check

1. **Worktree roots** — replace `{{WORKTREE_ROOT_CODE}}`, `{{WORKTREE_ROOT_OPS}}`, and `{{WORKTREE_ROOT_INFRA}}` with the actual paths on your machine. Delete the ops/infra lines if you have a single-repo layout. File: `.shared-llm/this_repo/layers/llm/this_repo/claude/claude.md`.

2. **Source glob** — replace `{{SOURCE_GLOB}}` with the path prefix for source files that trigger docs updates (e.g. `src/(packages|services)/`). File: `.shared-llm/this_repo/layers/llm/this_repo/claude/claude.md`.

3. **Docs update skill** — replace `{{DOCS_UPDATE_SKILL}}` with the skill name you use to update per-package/service docs (e.g. `/update-docs`). Delete the docs-check block entirely if you have no per-component docs. File: `.shared-llm/this_repo/layers/llm/this_repo/claude/claude.md`.

4. **Ops and infra repo names** — replace `{{OPS_REPO}}` and `{{INFRA_REPO}}` with your sibling repo names, or delete those `Key paths` bullets if you have a single-repo layout. File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`.

---

## E — Cross-harness agents.md (adopt or delete)

1. **Decide** — `.shared-llm/this_repo/layers/llm/this_repo/codex/agents.md` is a skeleton for documenting shared-skills wiring across Claude Code, Codex, Pi, etc. If you use a single harness only, delete this file and remove `agents.md` from the inputs list in `.shared-llm/this_repo/compose/agents-md/root.yaml`. If you adopt it, fill in the three sections (`## Skills`, `## Hooks and MCP servers`, `## Session memories`) with your actual wiring.

---

## F — Per-package and per-service leaves

1. **Copy `example_package.md` once per package** — for each package under `src/packages/`, copy `.shared-llm/this_repo/layers/llm/this_repo/common/packages/example_package.md` to `.shared-llm/this_repo/layers/llm/this_repo/common/packages/<package_name>.md`. Fill in `{{PACKAGE_NAME}}`, the type (Library/Service), notable modules, and gotchas.

2. **Copy `example_service.md` once per service** — for each service under `src/services/`, copy `.shared-llm/this_repo/layers/llm/this_repo/common/services/example_service.md` to `.shared-llm/this_repo/layers/llm/this_repo/common/services/<service_name>.md`. Fill in `{{SERVICE_NAME}}`, the invocation type, entry points, and gotchas.

3. **Create matching recipes** — for each leaf file created in steps 18–19, copy the corresponding example recipe:
    - Package: copy `.shared-llm/this_repo/compose/claude-md/example-package.yaml` → `.shared-llm/this_repo/compose/claude-md/packages/<package_name>.yaml`. Update `inputs` and `output`.
    - Service: copy `.shared-llm/this_repo/compose/claude-md/example-service.yaml` → `.shared-llm/this_repo/compose/claude-md/services/<service_name>.yaml`. Update `inputs` and `output`.
    - Do the same under `.shared-llm/this_repo/compose/agents-md/` if you produce per-component `AGENTS.md` files too.

---

## G — Package hierarchy and service catalog

1. **Package hierarchy** — fill in the tier ladder in `.shared-llm/this_repo/layers/llm/this_repo/common/src/packages.md` under `## Package hierarchy`. List every package grouped by dependency tier.

2. **Service catalog** — fill in the service list in `.shared-llm/this_repo/layers/llm/this_repo/common/src/services.md` under `## Service catalog`. Group by type (frontend, Lambda, CLI, etc.).

3. **Gotchas** — fill in the `## Gotchas` sections in `src/packages.md` and `src/services.md` with project-specific naming quirks, historical exceptions, and non-obvious conventions.

4. **Shared utilities package** — replace `{{SHARED_UTIL_PACKAGE}}` in `.shared-llm/this_repo/layers/skills/this_repo/python.md` with your consolidation target package name (e.g. `myapp_commons`). This is the package where utility functions used by 2+ packages live.

---

## H — Compose and the output convention

1. **Leak check** — run:

    ```bash
    grep -rn '{{\|TODO(project)' .shared-llm/this_repo/
    ```

    Must return nothing. Fix any remaining placeholders.

2. **Build** — run:

    ```bash
    just update        # (re)builds every registered destination; add -v for per-file detail
    ```

    (Once registered with `just configure -d`, your repo is rebuilt on every `just update`.)

3. **Where the outputs land — at your repo root, no manual move.** Recipe `output:` paths are **root-relative** and resolve against the destination's root, so the generated files land exactly where each harness reads them:

    - `CLAUDE.md` and `AGENTS.md` at the repo **root**
    - the per-repo Python skill at `.claude/skills/python/SKILL.md`
    - the generic agent personas at `.claude/agents/<name>.md` (full roster: README → Inventory)

    `just update` composes only the **consumer-relevant** recipe groups for a destination — root `CLAUDE.md`/`AGENTS.md`, the skills, the agents, and the slash commands. It deliberately does **not** compose:

    - the home-only `global/` skills (`python`/`nextjs`/`backend`) — those install into `~/` via the **global** step (`-g`), not into your repo;
    - the `example-package` / `example-service` **demo** recipes — illustrative samples only. If you want to see them, compose by hand into a throwaway area (`python3 tools/harness.py compose .shared-llm/this_repo/compose/claude-md/example-package.yaml --target /tmp/samples`); they are not deliverables and never land in your real `src/` tree.

    To produce a `CLAUDE.md` for one of your **real** packages/services, copy a leaf layer + its recipe (group F above) and point the new recipe's `output:` at the real path (e.g. `src/packages/<name>/CLAUDE.md`).

---

## I — Pi planning output directory (optional)

The Pi `/do-planish` command is the standalone TypeScript extension/register planner with browser-backed grill/review tools (annotation-only pages: sticky notes → Copy Feedback → paste the block back into the TUI). It writes a `plan.md` + `plan.html` pair somewhere. Without configuration it defaults to `/tmp/planish/{date}/{slug}`, which is fine for throwaway use but inconvenient when you want plans versioned next to your work. For workflow-suite planning, use `/do-plan-and-grill` in Pi or `/cc-plan-and-grill` in Claude Code. The standalone `/cc-planish` Claude Code skill mirrors this `/do-planish` flow against the same `.planish.yaml`.

Put a `.planish.yaml` at your repo root (or any ancestor directory) to control where plans land and which hostname URLs use:

```yaml
# .planish.yaml — controls where /do-planish writes plan.md + plan.html,
# and the hostname planning-flow URLs use (remote/Tailscale sessions)
dir: docs/plans/{date}/{slug}/v{n}
host: your-machine-name   # optional — default localhost
```

**Two fields.** `dir` — where plans land; the path resolves relative to the directory that holds `.planish.yaml`, and Pi walks upward from cwd to find the file, so one at the repo root covers everything inside. `host` — optional; the machine name your browser uses to reach this box (e.g. a Tailscale name) when you work remotely. Every URL the planning flows hand out uses it instead of `localhost`, and the planish server (port 4390) binds `0.0.0.0` so those remote connections are accepted. `$PLANISH_HOST` overrides it for a single session, and `host:` works standalone (with `dir` falling back to `/tmp/planish/{date}/{slug}`).

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

You can override the config file for a single run with `--dir <path>` passed to `/do-planish`, or by setting `$PLANISH_DIR`.

1. **Review outputs** — open the generated `CLAUDE.md`, `AGENTS.md`, and skill files at the repo root. Read them as an LLM would. Adjust the layer prose until the generated content reads naturally and accurately describes your project, then recompose.

2. **Commit the generated files** — your consumer repo should commit the generated `CLAUDE.md`, `AGENTS.md`, skill, and agent files. They are the deliverables and they sit at the locations each harness reads. (This kit, by contrast, gitignores its own `examples/` staging because it composes itself only to test the engine — it keeps a hand-maintained `CLAUDE.md` of its own.)

---

## I — Pi harness (optional)

The **global** step of `just update` (run when `-g` includes `pi`) already wires the Pi runtime for you. The notes below cover customizing it.

1. **Re-wire on demand** — `just update` symlinks the bundled OWN extensions (including the `memsearch/` directory) and agent personas into `~/.pi/`, reconciles the kit-owned `herdr-config.toml` link at `~/.config/herdr/config.toml`, and scaffolds `~/.pi/agent/settings.json` from the template (if absent), as part of its global step. The THIRD-PARTY extensions are a separate step, `just pi-extensions`, which reconciles the pinned sources in `.shared-llm/public/llm/pi/common/third-party-extensions.txt` through the Pi CLI: it installs missing entries and removes only the explicitly retired package; unrelated user packages are untouched. There is no `npm ci` / vendored `node_modules` step. See `.shared-llm/public/llm/pi/common/THIRD-PARTY-EXTENSIONS.md`.

2. **Customize settings** — open `.shared-llm/public/llm/pi/common/settings.template.json` and adjust `defaultProvider`, `defaultModel`, and `defaultThinkingLevel` to match your environment. The template is applied only when `~/.pi/agent/settings.json` does not exist; edit your live settings file directly after first run. Its `packages` array starts empty on purpose — the third-party installer fills it; the pinned manifest (`third-party-extensions.txt`) is the single source of truth for the extension set.

3. **Extensions and agents** — `context-workflow.ts`, the `memsearch/` extension, the review hub extensions (`codex-reviewer-hub.ts`, `doc-review-hub.ts`, `pr-review-hub.ts`), and the review agent personas (`codex-reviewer.md`, `doc-reviewer.md`, `pr-reviewer.md`) are reusable as-is. They contain no project-specific references; adopt them without modification. (`memsearch` additionally needs the `memsearch` CLI on `PATH` or `uvx` available; without either it no-ops silently.)

4. **Launch group** — Start the review hub with `just hub` (needs `tmux`); `just pi-status` / `just pi-clean` manage it, and `just builder` launches a builder Pi.
