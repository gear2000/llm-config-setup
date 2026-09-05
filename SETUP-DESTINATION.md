# Set up a destination repository

Give this file to an LLM (`@SETUP-DESTINATION.md`) when adding one target repository to an already-installed kit. Kit install, refresh, global harnesses, and UpAgent offering policy stay in [UPINSTALL.md](UPINSTALL.md).

Run every kit command from the cloned kit checkout, not from the destination.

**Do not write `~/.shared-llm.yaml` or the destination `.shared-llm/` until the user accepts the file map.** Collect facts and ask the two questions first.

---

## When to use

- Register a new destination in `~/.shared-llm.yaml`
- Seed that repo's `.shared-llm/this_repo/` layers and compose recipes
- Decide which folders get generated `CLAUDE.md` / `AGENTS.md`
- Fill `TEMPLATE.*` stubs for that destination (Appendix)
- Offer dest-contextual UpAgent specialists when this repo's practice is not in a kit persona

Not this file: fresh kit install, `just update` after a kit pull with no new dest, changing `-g` or machine offering sets. Those are [UPINSTALL.md](UPINSTALL.md). Authoring a dest specialist after the user says yes is [SETUP-SPECIALISTS.md](SETUP-SPECIALISTS.md).

---

## LLM workflow

### 1 — Inspect (read-only)

Done when you can name the kit checkout root, the destination git root, and whether `~/.shared-llm.yaml` already exists.

| Signal | Meaning |
|--------|---------|
| Kit checkout (`tools/harness.py`, `justfile`) | Commands run here |
| `~/.shared-llm.yaml` | Machine roster. If missing, stop and open UPINSTALL.md |
| Destination path the user named | Must be the git root, not a parent or archive copy |

Skip `.git`, `node_modules`, vendored trees, build output, and `.shared-llm` if you later walk the dest.

Explain `public/` (kit-synced, disposable) versus `this_repo/` (repo-owned) before any write.

### 2 — Collect dest facts

Ask only for unresolved facts:

- **`path`** — exact git root
- **`harnesses`** — `cc`, `pi`, `codex`, `cursor` (`cursor` uses the Codex surface). Default `cc,pi`
- **`placeholders:`** — only if a kit-synced `public/` layer still has `{{TOKEN}}`s. Values live in `~/.shared-llm.yaml`, never in a committed layer
- **`upagent.offering_sets`** — optional destination *replacement* (`just configure -d … --offering-sets`). Default: inherit machine `[standard]`
- Repo-root `.shared-llm.yaml` `work_log:` — different file from the machine roster; optional; start from the kit `.shared-llm.yaml.example`
- Justfile imports — only if the user wants them. The usual one is `import '.shared-llm/public/extensions/common/upagent/justfile'`

Do not put commands, models, providers, health checks, or secrets in the machine roster. Never invent private URLs, account IDs, credential paths, or token values.

Done when `path` and `harnesses` are known.

### 3 — Question 1 (do not walk yet)

Ask: **Do you have a structure in mind for where to deposit the LLM md files?**

- User names folders (and how far down): that list is the constraint. Later suggestions may only *add* directories if they accept them. Do not ignore their list.
- User asks you to suggest: wait for question 2 if they want the friendliness pass; otherwise a light placement-only walk of directories that already exist.

Done when you have a named list or an explicit "suggest for me".

### 4 — Question 2 (do not walk yet)

Ask: **Do you want me to analyze how LLM-friendly this codebase is, and suggest a non-disruptive way to compose and generate the LLM files?**

Non-disruptive: **add instruction files at directories that already exist.** Do not move packages, rewrite imports, invent new layers, or fix the architecture.

- Yes: one walk, then the report in step 5.
- No, and they already named folders: skip the score. Confirm that map (step 6).
- No, and they asked you to suggest: light placement-only walk of existing dirs, then step 6. No score.

Done when yes/no is recorded.

### 5 — Report (only if they asked for analysis)

Reuse the kit words in `.shared-llm/public/layers/llm/common/common/architecture.md`. Do not invent a parallel vocabulary.

Walk first-party dirs. Discover real paths (`src/` vs `source/`). Do not assume a Python monorepo.

**Score (not a fake 0–100).** Four dimensions, each **strong**, **mixed**, or **weak**, then one overall **1–5**:

| Overall | Meaning |
|---------|---------|
| **5 ready** | Deep modules, downward layering, seams an agent can sit in, context-sized |
| **3 usable** | Mixed. Nested files still help. List the gaps |
| **1 hostile** | Shallow or tangled. Still place files at the best existing dirs. Do not restructure |

Dimensions:

- **Deep modules** — narrow public interface, rich hidden implementation. Smell: ~10+ exported symbols, pass-through re-exports.
- **Downward layering** — packages under services. Higher services import lower services and packages, never the reverse.
- **Seams** — change behaviour at an interface without editing callers in place.
- **Context-sized** — one module plus its nested md file fits a session. God files fail this.

Also note, as evidence not extra scores: name agreement, entry-point locality, tests at the seam, missing glossary, fail-loud.

**Why.** One short block, e.g. `usable (3): layered packages/services, but common is a shallow pass-through and source/services/api is a god package.`

**Future work (do not fix).** Numbered deepening and layering gaps. You do not fix these for the user in this session.

**Quick fix (this session).** "We can at least generate LLM files in these directories: …" Existing folders only. Root `CLAUDE.md` / `AGENTS.md` always. Nested files match harnesses: `cc` → `CLAUDE.md`; `pi` / `codex` / `cursor` → `AGENTS.md`. Default `cc,pi` gets both.

If question 1 already named folders, the quick-fix list starts there. Proposed extras are opt-in.

Done when the score, why, future list, and quick-fix directory map exist.

### 6 — Ask if the map is acceptable

Ask: **Is this file map acceptable?**

Revise until yes. Do not mutate yet.

Done when the user accepts a concrete list of output paths (root plus any nested dirs).

### 7 — Question 3 (dest-contextual specialists)

The kit UpAgent hub already lists **generic** specialists (`clickhouse`, `kafka`, `backend`, …). A destination overlay adds personas that know **this repo's** practice so later agents can consult them.

After any walk (friendliness, light placement, or a short extra scan if they skipped both), notice stack **and** practice. Example: not only "this repo uses ClickHouse", but "materialized views are the transformation pipeline."

Ask, per finding: **I noticed [practice]. The kit has [generic specialist] / has none. Do you want me to analyze that area more deeply and add a dest-contextual specialist the UpAgent hub can consult?**

- Yes: open [SETUP-SPECIALISTS.md](SETUP-SPECIALISTS.md). Analyze and write dest-owned agent layers, compose recipes, and `this_repo/extensions/common/upagent/specialists.yaml` in the mutate step. Do not refactor the dest.
- No / empty list: skip. Kit specialists remain available when the hub runs in this dest.

Reuse a kit specialist when the generic persona is enough. Dest overlay is for the contextual half (MV pipelines, RisingWave jobs, this-repo Go `internal/` rules).

Done when the accepted specialist names are a concrete list (possibly empty).

### 8 — Mutate (only after accept)

Ask permission once more if dest files will change, then:

1. From the kit checkout: `just configure -d <repo> -l <harnesses>` (add `--offering-sets` or a `placeholders:` map only when collected). This writes `~/.shared-llm.yaml`.
2. Deposit `<repo>/.shared-llm/this_repo/`. Do not create `public/`; `just update` copies it.
   - Copy needed kit `TEMPLATE.*` stubs from `.shared-llm/public/layers/` (and recipes from `.shared-llm/public/compose/`) into the dest `this_repo/` tree, mirroring `layers/*/this_repo/` and `compose/`. Seed only stubs the accepted map needs. Delete unused stubs (`authoring.md`, `aws-execution-engine.md`, Python package leaves) when they do not apply.
   - Fill stubs from the tree and the user. Appendix groups A–J. Delete the `<!-- TEMPLATE -->` banner and drop the `TEMPLATE.` prefix.
   - For each accepted nested folder, add a layer plus a compose recipe under `this_repo/` (not a hand-written `CLAUDE.md`). Recipe `output:` is dest-root-relative, e.g. `source/packages/CLAUDE.md`. Adapt `TEMPLATE.example_package.md`, `TEMPLATE.example_service.md`, or `TEMPLATE.authoring.md`. Copy recipe shape from the kit `example-package.yaml` / `example-service.yaml` into `this_repo/compose/claude-md/` and/or `this_repo/compose/agents-md/`.
   - For each accepted dest specialist, follow [SETUP-SPECIALISTS.md](SETUP-SPECIALISTS.md) write steps (persona layer, description, `this_repo/compose/agents/<name>.yaml`, overlay `this_repo/extensions/common/upagent/specialists.yaml`).
3. Optional justfile import in the dest root justfile, only if the user chose it.
4. `just descriptions`, then `just update`, then `just update` again. The second run must be a no-op. If specialists were added, from the dest run `just upagent-specialists` and confirm the new names.

Never edit generated outputs (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/*/SKILL.md`, `.claude/agents/*.md`) by hand. Nested dest-owned recipes under `this_repo/compose/claude-md/` and `this_repo/compose/agents-md/` run on `just update`. Dest-owned agent recipes under `this_repo/compose/agents/` run too. Kit `example-package` / `example-service` recipes in `public/` do not.

Preserve foreign files. Fail loud on unresolved placeholders, malformed config, missing prerequisites, or conflicting non-owned files.

Done when `~/.shared-llm.yaml` lists this dest, dest `.shared-llm/this_repo/` has layers and recipes for the accepted map (and accepted specialists), generated files sit at those paths, and the second `just update` is idempotent.

### 9 — Verification

Summarize: dest path, harnesses, offering sets, accepted output paths, score if you ran analysis, accepted dest specialists (or none), future work left undone, files written.

---

## Appendix — TEMPLATE stub checklist

> **The destination split.** A registered destination's `.shared-llm/` has two trees (see the README's *destination split* section): **`public/`** — kit-synced, rebuilt by the engine on every `just update`, never hand-edited; and **`this_repo/`** — repo-owned, where your fillable stubs and recipes live. **This appendix is about `this_repo/`** — every file path below is one you edit or create there. The kit-synced `common` layers your recipes reference (kept under `public/`, never yours to edit) stay untouched. You do **not** create `public/`; the engine builds it on `just update`.

The rest of this appendix happens **inside your target repo** — that is where the `.shared-llm/` tree now lives. `just update` (re)builds the `public/` tree from the kit (never touching your `this_repo/` tree), composes its output files, and wires the per-harness skill links.

**See what still needs you:** `find . -name 'TEMPLATE.*'` lists every unfilled stub. For each: fill it in (the groups below map every token to its file), **delete the `<!-- TEMPLATE … -->` banner**, then **rename it to drop the `TEMPLATE.` prefix** (e.g. `TEMPLATE.general.md` → `general.md`).

When you finish, both of these must return nothing:

```bash
grep -rn '{{\|TODO(project)' .shared-llm/this_repo/
find . -name 'TEMPLATE.*'
```

Then run `just update` to (re)generate every registered destination's output files.

---

### A — Identity and naming

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

### B — Credentials and cloud

**Warning: never commit real secrets.** These files are tracked by git. Fill in the shape (paths, env var names, account IDs) — never actual token values.

> **Two fill paths.** The tokens in this group live in *your* `this_repo/` layers, so you fill them **by hand** here. If instead a **kit-synced `public/` layer** (a shared layer you do not own) carries a `{{TOKEN}}`, do not edit it — the engine fills it at build time from the destination's `placeholders:` map in `~/.shared-llm.yaml` (see the README's *Placeholder convention*), and stops the build if the value is missing. Either way, real values live only in your home config / secrets, never in a committed layer.

1. **Credential root path** — replace `{{CRED_ROOT}}` with the path to your secrets directory (e.g. `~/project/secrets/`). File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`, `## Credentials` section.

2. **Cloud region** — replace `{{CLOUD_REGION}}` (e.g. `us-east-1`, `eu-west-1`). File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`.

3. **Cloud account IDs** — replace `{{ACCOUNT_SAAS}}` and `{{ACCOUNT_TENANT}}` with your account identifiers (numeric IDs, project names, etc.). File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`.

4. **Credential entries** — fill in the bullet list under `## Credentials` with one entry per credential: name → env var → path. Keep entries to one line each.

---

### C — CI, build, deploy, and PyPI tooling

1. **CI build tool** — replace `{{CI_BUILD_TOOL}}` with your push-triggered CI system name (e.g. `GitHub Actions`, a self-hosted CI tool). Files: `general.md`, `src/packages.md`, `src/services.md`, `python.md`.

2. **CI deploy tool** — replace `{{CI_DEPLOY_TOOL}}` with your deploy automation system (e.g. a deploy pipeline or automation server). Same files.

3. **Test runner task** — replace `{{TEST_TASK}}` with your Taskfile target for running package tests (e.g. `task pkg:<name>:test:image`). File: `.shared-llm/this_repo/layers/skills/this_repo/python.md`.

4. **PyPI URLs** — replace `{{PYPI_INDEX_URL}}` and `{{PYPI_INDEX_URL_AUTH}}` with your internal registry URLs (unauthenticated and authenticated forms). Files: `src/packages.md`, `src/services.md`, `python.md`. Replace `{{PYPI_HOST}}` with the registry hostname.

5. **Deploy script** — in `.shared-llm/this_repo/layers/llm/this_repo/common/src/services.md`, fill in the `## Deploy gate` TODO: replace `{{DEPLOY_SCRIPT}}` with the path to your deploy helper (e.g. `tools/deploy.sh`).

---

### D — Layout, worktrees, and docs-check

1. **Worktree roots** — replace `{{WORKTREE_ROOT_CODE}}`, `{{WORKTREE_ROOT_OPS}}`, and `{{WORKTREE_ROOT_INFRA}}` with the actual paths on your machine. Delete the ops/infra lines if you have a single-repo layout. File: `.shared-llm/this_repo/layers/llm/this_repo/claude/claude.md`.

2. **Source glob** — replace `{{SOURCE_GLOB}}` with the path prefix for source files that trigger docs updates (e.g. `src/(packages|services)/`). File: `.shared-llm/this_repo/layers/llm/this_repo/claude/claude.md`.

3. **Docs update skill** — replace `{{DOCS_UPDATE_SKILL}}` with the skill name you use to update per-package/service docs (e.g. `/update-docs`). Delete the docs-check block entirely if you have no per-component docs. File: `.shared-llm/this_repo/layers/llm/this_repo/claude/claude.md`.

4. **Ops and infra repo names** — replace `{{OPS_REPO}}` and `{{INFRA_REPO}}` with your sibling repo names, or delete those `Key paths` bullets if you have a single-repo layout. File: `.shared-llm/this_repo/layers/llm/this_repo/common/general.md`.

---

### E — Cross-harness agents.md (adopt or delete)

1. **Decide** — `.shared-llm/this_repo/layers/llm/this_repo/codex/agents.md` is a skeleton for documenting shared-skills wiring across Claude Code, Codex, Pi, etc. If you use a single harness only, delete this file and remove `agents.md` from the inputs list in `.shared-llm/this_repo/compose/agents-md/root.yaml`. If you adopt it, fill in the three sections (`## Skills`, `## Hooks and MCP servers`, `## Session memories`) with your actual wiring.

---

### F — Nested directory and leaf files

Use the **accepted file map** from the workflow, not a hardcoded `src/packages` layout. Discover the real directories.

1. **Directory-level layer** — for a folder that holds many packages or services (e.g. `source/packages`, `source/services`), adapt `TEMPLATE.packages.md` / `TEMPLATE.services.md` / `TEMPLATE.authoring.md` and point a recipe `output:` at that folder's `CLAUDE.md` / `AGENTS.md`.

2. **Leaf layer** — when the accepted map includes one package or service dir, copy `.shared-llm/this_repo/layers/llm/this_repo/common/packages/example_package.md` (or `services/example_service.md`) to `.shared-llm/this_repo/layers/llm/this_repo/common/packages/<package_name>.md`. Fill in `{{PACKAGE_NAME}}` or `{{SERVICE_NAME}}`, type, notable modules, and gotchas.

3. **Matching recipes** — write dest-owned YAML under `.shared-llm/this_repo/compose/claude-md/` and/or `agents-md/` (copy shape from the kit `example-package.yaml` / `example-service.yaml`). Update `inputs` and `output`. These recipes run on `just update`. Do not copy them into `public/compose/`; the next update would treat them as kit files.

---

### G — Package hierarchy and service catalog

1. **Package hierarchy** — fill in the tier ladder in `.shared-llm/this_repo/layers/llm/this_repo/common/src/packages.md` under `## Package hierarchy`. List every package grouped by dependency tier.

2. **Service catalog** — fill in the service list in `.shared-llm/this_repo/layers/llm/this_repo/common/src/services.md` under `## Service catalog`. Group by type (frontend, Lambda, CLI, etc.).

3. **Gotchas** — fill in the `## Gotchas` sections in `src/packages.md` and `src/services.md` with project-specific naming quirks, historical exceptions, and non-obvious conventions. Record approved LLM-friendliness findings here when the user asked you to keep them.

4. **Shared utilities package** — replace `{{SHARED_UTIL_PACKAGE}}` in `.shared-llm/this_repo/layers/skills/this_repo/python.md` with your consolidation target package name (e.g. `myapp_commons`). This is the package where utility functions used by 2+ packages live.

---

### H — Compose and the output convention

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

3. **Where the outputs land.** Recipe `output:` paths are **root-relative** and resolve against the destination's root:

    - `CLAUDE.md` and `AGENTS.md` at the repo **root**
    - nested files at the accepted dirs (e.g. `source/packages/CLAUDE.md`)
    - the per-repo Python skill at `.claude/skills/python/SKILL.md`
    - the generic agent personas at `.claude/agents/<name>.md` (full roster: README → Inventory)

    `just update` composes the **consumer-relevant** recipe groups: public root `CLAUDE.md`/`AGENTS.md`, dest-owned recipes under `this_repo/compose/claude-md/` and `this_repo/compose/agents-md/` (including nested paths), plus skills, agents, and slash commands. It does **not** compose:

    - the home-only `global/` skills (`python`/`nextjs`/`backend`/`golang`/`herdr`/`clickhouse`/`kafka`/`lucidchart`/`drawio`/`create-html`) — those install into `~/` via the **global** step (`-g`), not into your repo;
    - the kit `example-package` / `example-service` **demo** recipes in `public/compose/` — they never land in your real source tree.

---

### I — Pi planning output directory (optional)

The Planish runtime provides the browser-backed grill/review tools used by `/do-plan` (annotation-only pages: sticky notes → Copy Feedback → paste the block back into the TUI). Without configuration it defaults to `/var/tmp/work-log/{date}/{slug}`, which is fine for throwaway use but inconvenient when you want plans versioned next to your work. Use `/do-plan` in Pi or `/cc-plan` in Claude Code for workflow-suite planning; `/do-planish` and `/cc-planish` are one-release warning aliases.

Put a `.shared-llm.yaml` with a `work_log:` block at your repo root (or any ancestor directory) to control where plans land and which hostname URLs use — start from the tracked `.shared-llm.yaml.example`:

```yaml
# .shared-llm.yaml — controls where the planning flows write plan.md + plan.html,
# and the hostname planning-flow URLs use (remote/Tailscale sessions)
work_log:
  dir: docs/plans/{date}/{slug}/v{n}
  host: your-machine-name   # optional — default localhost
```

**Two fields.** `work_log.dir` — where plans land; the path resolves relative to the directory that holds `.shared-llm.yaml`. `work_log.host` — optional; the machine name your browser uses to reach this box (e.g. a Tailscale name) when you work remotely. Every URL the planning flows hand out uses it instead of `localhost`, and the planish server (port 4390) binds `0.0.0.0` so those remote connections are accepted. `$WORK_LOG_HOST` overrides it for a single session, and `host` works standalone (with `dir` falling back to `/var/tmp/work-log/{date}/{slug}`).

**Which file wins.** Resolution walks upward from the current directory and takes the nearest `.shared-llm.yaml` that *contains* a `work_log:` mapping; a file without that key is skipped and the walk continues, so the machine-level destination roster at `~/.shared-llm.yaml` never shadows a repo's config. One file at your repo root covers everything inside it. A `work_log.dir` that is present but not a non-empty string fails loudly rather than falling back.

**Available tokens:**

| Token | Value |
|---|---|
| `{date}` | Today's date — `YYYY-MM-DD` |
| `{slug}` | Your topic, lowercased and hyphenated |
| `{type}` | `plan` |
| `{n}` | Next version integer — auto-incremented by scanning siblings |

**Example outputs** for `dir: ops/mkdocs/docs/work-log/{date}/{slug}/plan` and topic `"redesign auth flow"`:

```
ops/mkdocs/docs/work-log/2026-06-29/redesign-auth-flow/plan/plan.md
ops/mkdocs/docs/work-log/2026-06-29/redesign-auth-flow/plan/plan.html
```

You can override the config file for a single run with `--dir <path>` passed through the planning command, or by setting `$WORK_LOG_DIR`.

**Migrating from `.planish.yaml`.** The predecessor file (top-level `dir:` / `host:`) and the `$PLANISH_DIR` / `$PLANISH_HOST` variables are still honored as a last-resort fallback and warn on stderr when used. Move their values into a `work_log:` block in `.shared-llm.yaml`; the fallback goes away in a future release.

1. **Review outputs** — open the generated `CLAUDE.md`, `AGENTS.md`, and skill files. Read them as an LLM would. Adjust the layer prose until the generated content reads naturally and accurately describes your project, then recompose.

2. **Commit the generated files** — your consumer repo should commit the generated `CLAUDE.md`, `AGENTS.md`, skill, and agent files. They are the deliverables and they sit at the locations each harness reads. (This kit, by contrast, gitignores its own `examples/` staging because it composes itself only to test the engine — it keeps a hand-maintained `CLAUDE.md` of its own.)

---

### J — Pi harness (optional)

The **global** step of `just update` (run when `-g` includes `pi`) already wires the Pi runtime for you. The notes below cover customizing it.

1. **Re-wire on demand** — `just update` symlinks the bundled OWN extensions (including the `memsearch/` directory) and agent personas into `~/.pi/`, reconciles the kit-owned `herdr-config.toml` link at `~/.config/herdr/config.toml`, and scaffolds `~/.pi/agent/settings.json` from the template (if absent), as part of its global step. The THIRD-PARTY extensions are a separate step, `just pi-extensions`, which reconciles the pinned sources in `.shared-llm/public/llm/pi/common/third-party-extensions.txt` through the Pi CLI: it installs missing entries and removes only the explicitly retired package; unrelated user packages are untouched. There is no `npm ci` / vendored `node_modules` step. See `.shared-llm/public/llm/pi/common/THIRD-PARTY-EXTENSIONS.md`.

2. **Customize settings** — open `.shared-llm/public/llm/pi/common/settings.template.json` and adjust `defaultProvider`, `defaultModel`, and `defaultThinkingLevel` to match your environment. The template is applied only when `~/.pi/agent/settings.json` does not exist; edit your live settings file directly after first run. Its `packages` array starts empty on purpose — the third-party installer fills it; the pinned manifest (`third-party-extensions.txt`) is the single source of truth for the extension set.

3. **Extensions and agents** — `auto-compact.ts` (automatic native compaction at 50% of the active model's context window), `context-workflow.ts`, the `memsearch/` extension, the review hub extensions (`codex-reviewer-hub.ts`, `doc-review-hub.ts`, `pr-review-hub.ts`), and the review agent personas (`codex-reviewer.md`, `doc-reviewer.md`, `pr-reviewer.md`) are reusable as-is. They contain no project-specific references; adopt them without modification. (`memsearch` additionally needs the `memsearch` CLI on `PATH` or `uvx` available; without either it no-ops silently.)

4. **Launch group** — Start the review hub with `just hub` (needs `tmux`); `just pi-status` / `just pi-clean` manage it, and `just builder` launches a builder Pi.
