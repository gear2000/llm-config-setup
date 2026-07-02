# llm-config-setup

A portable starter kit for assembling `CLAUDE.md`, `AGENTS.md`, skill files, and agent personas from composable markdown layers. One centralized engine reads a small config file, copies the reusable layers into each repo you register, composes the instruction files, and wires up the per-harness skill links — for every repo at once, from one command.

## What it does

LLM coding assistants (Claude Code, Codex, Pi, etc.) read instruction files — `CLAUDE.md`, `AGENTS.md`, skill `.md` files, agent persona `.md` files — to understand your project and to pick up specialized roles. Maintaining these by hand gets messy: duplicated prose, inconsistent tone, hard-to-update cross-cutting rules.

This kit treats those files as **build artifacts** assembled from **layers**:

- `.shared-llm/layers/` — the source prose, split into reusable layers.
- `.shared-llm/compose/` — YAML recipes that declare which layers to combine and where to write the output.
- `tools/harness.py` — the engine that reads recipes and writes output files, reconciles the skill symlinks, and runs the whole flow. It is the **one** engine, it lives **only** in this kit, and it is **never** copied into a repo you set up. (A per-repo copy used to drift out of sync and silently break Pi skill discovery; centralizing the engine is the core fix.)

You drive it with `just` against a single config file, `~/.shared-llm.yaml`, that lists your destinations. Register a repo once, then rebuild every registered repo with one `just update`.

## The command surface

```bash
just init -o mac|ubuntu          # one-time OS prereq check (python3 + just)
just configure -s ~/.shared-llm  # set the source hub (default; run once)
just configure -d /path/to/repo -l cc,pi   # register a destination repo + its harness list
just configure -g cc,pi          # set the GLOBAL (home / all-projects) harness list
just update                      # the headline command: copy → compose → link (+ global), every destination
just update -v                   # same, with per-file detail printed
```

- **Harness tokens** are `cc` (Claude Code), `pi` (Pi), and `codex` (Codex). A destination's `-l` list defaults to `cc,pi`.
- `just update` always writes a full run log under `/tmp/.shared-llm/log/<timestamp>.log`; `-v` also prints the per-file detail to the terminal.
- **Building blocks** (`just update` runs them in order; each is also callable on its own): `just copy`, `just compose`, `just link`, and — when a `global:` list is set — `just global`.
- `just pi-extensions` installs the pinned third-party Pi extensions via the `pi` CLI (a network step; no-op if `pi` is absent).
- `just test` runs the composer/flow test suite.
- The Pi launch group — `just hub`, `just builder`, `just pi-status`, `just pi-clean` — and the terraform workflow recipes (`just tf-implement`, `just tf-approve`, `just tf-auto`, `just tf-reviewer-cc` / `-pi`) are covered under "Pi harness runtime config" below.

## The config file — `~/.shared-llm.yaml`

`just configure` maintains this file; you rarely edit it by hand. Its shape:

```yaml
source: ~/.shared-llm            # the local hub: kit common content is copied here, then to each dest
global: [cc, pi]                 # home / all-projects harnesses to set up (omit to skip the global step)
destinations:
  - path: ~/project/repo/foo
    harnesses: [cc, pi]
  - path: ~/project/repo/bar
    harnesses: [cc, pi, codex]
```

`just update` reads this file and runs every operation centrally against the paths it lists. Because the engine is never copied into a destination, it can never drift out of sync with a per-repo copy of itself.

## The three operations

`just update` is three operations (plus an optional fourth):

1. **copy** — kit → hub (`~/.shared-llm`) → each destination's own `.shared-llm/`. It copies the **common** layers and recipes only; it **never** touches a destination's `this_repo/` overlays or its `compose/` recipes. It overwrites, and flags (does not block) when it overwrites a diverged local edit. There is no auto-prune — a removed common file is left in place.
2. **compose** — each destination composes from its **own** `.shared-llm/`, merging the freshly-copied common layers with that repo's `this_repo/` overlays. Outputs land at the destination's root (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/<name>/SKILL.md`, `.claude/agents/<name>.md`).
3. **link** — routes skills per harness **by name prefix**, then symlinks into each harness's **global** skill dir:
   - `do-*` → **Pi only**. During compose, `do-*` skills are moved out of `.claude/skills` (which Claude Code reads) into a Pi-only `<repo>/.pi-skills/` dir, so Claude never sees them; `link` then symlinks them into `~/.pi/agent/skills/`.
   - `cc-*` → **Claude Code only** (stays in `<repo>/.claude/skills/`, never linked to Pi/Codex).
   - everything else (**common**) → all harnesses (`.claude/skills` for Claude; symlinked into `~/.pi/agent/skills/` and `~/.agents/skills/` for Pi/Codex).

   Global dirs are used (not Pi's project-local `.pi/skills/`) because project-local skills load **only after a project is "trusted"** — that silently hid them. All destinations reconcile into each global dir together, so one never prunes another's links; a same-name collision across destinations warns (last wins). **Claude Code needs no link** — it reads `<repo>/.claude/` directly.
4. **global** (only when `global:` is set) — installs the home / all-projects pieces: the general home skills, the 18 generic agents, and the Pi + Claude runtime. See "The global step" below.

### Recipe path resolution — one rule

Every path in a compose recipe YAML — both `.shared-llm/...` inputs and any repo-relative path like `ops/...` — resolves against the **repo root**. There is no prefix-stripping and no dual-mode resolution: one rule, everywhere.

## The `.shared-llm/` layout

The repo splits into the **source tree** (`.shared-llm/`) and the **engine + config machinery** (`tools/`, `justfile`).

```
.shared-llm/
  layers/                         — SOURCE prose, split into reusable layers
    llm/                          — layers for CLAUDE.md / AGENTS.md
      common/common/response.md   — portable response-format rules (ready as-is)
      this_repo/                  — project-specific layers, shipped as TEMPLATE.* stubs
        common/TEMPLATE.general.md            — project identity, conventions, CI, credentials
        common/TEMPLATE.authoring.md          — example: CLAUDE.md for a special subdirectory
        common/TEMPLATE.aws-execution-engine.md — example: CLAUDE.md for a component with a contract
        common/src/TEMPLATE.packages.md       — shared context for all package CLAUDE.mds
        common/src/TEMPLATE.services.md       — shared context for all service CLAUDE.mds
        common/packages/TEMPLATE.example_package.md — per-package leaf template
        common/services/TEMPLATE.example_service.md — per-service leaf template
        claude/TEMPLATE.claude.md             — Claude Code-specific conventions (worktrees, planning)
        codex/TEMPLATE.agents.md              — cross-harness wiring (skills, hooks, MCP)
    skills/                       — layers for skill files
      common/python/              — language conventions, ready as-is (practices.md + description.md)
      common/nextjs/              — ready as-is
      common/backend/            — ready as-is
      this_repo/TEMPLATE.python.md — project-specific Python skill layer (stub)
    agents/                       — layers for agent personas
      common/<name>.md            — the 18 generic agent bodies (ready as-is, brand-free)
      common/<name>.description.md — one-line description for each agent's frontmatter
  compose/                        — RECIPES: which layers to combine, and where to write
    claude-md/root.yaml           — recipe: root CLAUDE.md
    claude-md/example-package.yaml — recipe: per-package CLAUDE.md
    claude-md/example-service.yaml — recipe: per-service CLAUDE.md
    agents-md/root.yaml           — recipe: root AGENTS.md
    skills/python.yaml            — recipe: per-repo python skill (general layer + this_repo layer)
    global/python.yaml            — recipe: GENERAL python skill (no this_repo layer; global step)
    global/nextjs.yaml            — recipe: general nextjs skill (global step)
    global/backend.yaml           — recipe: general backend skill (global step)
    slash-commands/               — recipes: the routed slash-command skills
    agents/<name>.yaml            — recipe: one generic agent persona (18 of them)
  llm/pi/common/                  — Pi harness runtime config (NOT a compose input; see below)
  llm/claude/common/              — Claude harness runtime config (NOT a compose input; see below)
tools/
  harness.py                      — the ONE engine: compose + config-driven copy/compose/link/global
  install-pi-extensions.sh        — `pi install` helper for the pinned third-party extensions
justfile                          — init / configure / update, the building blocks, tests, and the Pi launch group
ONBOARDING.md                     — token-by-token checklist for filling the TEMPLATE stubs
```

**layers vs compose vs llm/pi:**

- **layers** are pure source prose — never generated, never frontmatter. The portable `common/` layers ship ready-to-use; the `this_repo/` layers ship as `TEMPLATE.*` stubs you fill in.
- **compose** recipes are the build instructions: each YAML names the input layers, the output path, and (for skills/agents) the frontmatter name + description. The engine concatenates the inputs and writes the output. Every path in a recipe resolves against the repo root.
- **llm/pi** and **llm/claude** are runtime config for the coding agents (Pi extensions and agent personas; Claude hooks, statusline, settings). They are **not** compose inputs — their files are never concatenated into any output. The **global** step symlinks the Pi files into `~/.pi/` (reconciling: create missing, re-point drifted, prune renamed/deleted) and copies the Claude files into `~/.claude/`. See "Pi harness runtime config" and "Claude harness runtime config" below.

## Setting up a destination repo

There is no scaffolding command — a destination is set up by hand once, then driven by `just update` forever after:

1. **Copy the kit's `.shared-llm/` into the repo.** The `this_repo/` layers arrive as fillable `TEMPLATE.*` stubs.
2. **Fill the `TEMPLATE.*` stubs** (see `ONBOARDING.md`), deleting the `TEMPLATE.` prefix from each as you finish it.
3. **Register it:** `just configure -d /path/to/repo -l cc,pi`.
4. **Build it:** `just update`.

From then on, `just update` keeps every registered destination in sync: it re-copies the common layers (step 1's `this_repo/` overlays are never touched), recomposes, and re-links. The generated `CLAUDE.md`, `AGENTS.md`, skill, and agent files land at the repo root, ready to commit.

## Where compose outputs land

**Recipe `output:` paths are root-relative**, and they resolve against the destination's root. So a real destination's generated files land exactly where each harness reads them — `CLAUDE.md` and `AGENTS.md` at the root, `.claude/skills/<name>/SKILL.md`, `.claude/agents/<name>.md` — with no manual move.

A destination composes only the **consumer-relevant** recipe groups (root `CLAUDE.md`/`AGENTS.md`, the skills, the agents, the slash commands). It deliberately does **not** compose the home-only `global/` skills (those install into `~/` via the global step) or the `example-package` / `example-service` **demo** recipes (illustrative samples only — a consumer repo never gets a stray `src/packages/example_package/CLAUDE.md`).

**The kit never clobbers its own files when it composes itself.** This repo ships its `this_repo` layers as `TEMPLATE.*` stubs and keeps a hand-maintained `CLAUDE.md` / `README`. When the global step composes the kit's own home skills and agents, it stages them under `examples/` — a **gitignored** staging dir — before copying them into `$HOME`, so a self-compose never overwrites the kit's real root files. The `example-package` / `example-service` demo recipes compose under a gitignored `samples/` area and are excluded from every consumer install.

## The global step — home skills + runtime

When `~/.shared-llm.yaml` has a `global:` list, `just update` (or `just global` on its own) installs the pieces that live in `$HOME` and apply across every project. Each is foreign-safe: it never clobbers a divergent or foreign file, leaving it untouched with a warning.

1. **General home skills** — composes the `global/` recipes (`python`, `nextjs`, `backend`, `golang`) and the routed slash-command skills, then copies each into the home skill dir every wanted harness reads: `~/.claude/skills/`, `~/.pi/agent/skills/`, `~/.agents/skills/` (Codex). Pi standalone planning is `/planish` from the extension; the workflow-suite commands are `/do-*` on Pi and the matching `cc-*` on Claude Code. The removed `/do-planish` and `/cc-planish` standalone variants are not installed.
2. **The 18 generic agents** — composes the `agents/` recipes and copies each persona into the home agent dirs: `~/.claude/agents/` and `~/.pi/agents/`. Codex has no user-agent directory, so agents skip it — the engine never invents one.
3. **Pi runtime** — symlinks the bundled Pi extensions + agent personas into `~/.pi/` (reconciling: create / re-point / prune), and scaffolds `~/.pi/agent/settings.json` from the template only if absent.
4. **Claude runtime** — copies the generic hooks into `~/.claude/hooks/` and the statusline into `~/.claude/statusline.sh`, and scaffolds `~/.claude/settings.json` from `settings.template.json` only if absent (never clobbers per-machine tweaks).

Third-party Pi extensions are a separate network step — `just pi-extensions`.

### Pi global-vs-repo skill precedence

Pi loads a **global** skill before a repo-local one of the same name and keeps the first it finds. The **link** step prunes the stale global Pi links it created in earlier versions (so an obsolete global link can't shadow a new repo-local one), and **warns** when a repo-local skill name still collides with a global one it does not own — it surfaces the collision but does not police it.

## The 18 generic agents

The global step ships these brand-free agent personas into your home agent dirs. They contain no project-specific references — adopt them as-is. Each is composed from a body layer (`agents/common/<name>.md`) plus a one-line description (`agents/common/<name>.description.md`).

- **architecture** — deep-module / layering review and refactoring guidance.
- **backend** — backend service and API implementation.
- **code-review** — adversarial correctness and cleanup review of a diff.
- **database** — schema, migrations, and query work.
- **deployer** — deployment execution and live verification (clean logs, not just exit code).
- **devops** — CI/CD plumbing and environment wiring.
- **docs-writer** — post-implementation documentation.
- **frontend** — UI/routing implementation kept as a thin layer.
- **ci-trigger** — kick off a CI build and report its result.
- **ci-pipeline** — author / fix CI pipeline definitions.
- **monorepo-python** — Python conventions within a layered monorepo.
- **monorepo-pkgs** — package/service layering and dependency-order discipline.
- **phase-evaluator** — judge a phase PASSED / FAILED / BLOCKED against its verification.
- **plan-watchdog** — plan-adherence enforcement (flag / block / halt).
- **playwright-cli** — drive a browser for web testing and screenshots.
- **qa** — interactive bug-reporting and issue filing.
- **security** — security review scoped to real blast radius.
- **team-pulse** — heartbeat / liveness monitor for an agent team.

## The compose engine

Under the config-driven surface, `tools/harness.py` has a low-level `compose` subcommand that reads a single recipe YAML (or a directory of them) and writes the output. `just update` uses it per destination; the tests and the global staging compose call it directly. It decouples two roots:

- **source** — the `.shared-llm/` dir (layers + recipes). Inputs and descriptions resolve against it. Selected via `--shared-llm`, then `$SHARED_LLM_DIR`, then a walk-up for `.shared-llm/`.
- **target** — the output base where each recipe's `output:` path lands. Selected via `--target`, default the current directory.

Recipe types: `skill` (frontmatter name + description), `agent` (name + description + model, optional color), `claude-md` / `agents-md` (plain concatenated markdown, no frontmatter), `prompt` (a whole feature prompt assembled from an explicit manifest), `copy` (one file copied verbatim, executable bit preserved — for hook scripts and the statusline), and `settings` (JSON inputs deep-merged: dicts recurse, lists concatenate, scalars overlay-win — for a `settings.json` base plus a this_repo overlay). A recipe may also declare a `catalog:` partial injected before its `inputs`.

```bash
# low-level, mostly for tests / manual staging (the everyday path is `just update`):
python3 tools/harness.py compose .shared-llm/compose/claude-md/root.yaml --target /tmp/out
python3 tools/harness.py compose .shared-llm/compose/agents --target /tmp/out   # a SUBSET (a recipe dir)
```

## Quickstart

```bash
# 1. Python 3 + one dependency for the engine
python3 -m pip install pyyaml

# 2. Check prerequisites (python3 + just)
just init -o ubuntu        # or: just init -o mac

# 3. Point the engine at the source hub, and set the global harness list
just configure -s ~/.shared-llm
just configure -g cc,pi

# 4. Set up a destination repo: copy .shared-llm/ into it, fill the TEMPLATE.* stubs
#    (see ONBOARDING.md), then register it
just configure -d /path/to/your/repo -l cc,pi

# 5. Build everything — every destination, plus the global home pieces
just update
```

## Placeholder convention

The `this_repo` stubs use two marker styles:

- **Block-level guidance** — HTML comment: `<!-- TODO(project): explain what goes here -->`
- **Inline fill-in values** — double-brace token: `{{PROJECT_NAME}}`, `{{CRED_ROOT}}`, etc.

When onboarding is done, both of these return nothing:

```bash
grep -rn '{{\|TODO(project)' .shared-llm/layers/
find . -name 'TEMPLATE.*'
```

See `ONBOARDING.md` for the ordered, token-by-token fill checklist.

## Claude harness runtime config

`.shared-llm/llm/claude/common/` holds Claude Code-specific runtime config —
hooks, the statusline, and a home settings template. Like the Pi runtime config,
these are **not composed into prose**; the global step of `just update` places
them at the home level so they apply to every project:

- **`hooks/*.sh`** — four generic, project-agnostic quality hooks (prettier
  format, TypeScript check, console.log warn/audit). Copied into `~/.claude/hooks/`
  and wired in the settings template. They **self-gate by file type** — a
  TypeScript hook no-ops in a Python repo — so firing in every project is safe.
- **`statusline.sh`** — a dependency-free statusline (dir, branch, model, context
  bar). Copied to `~/.claude/statusline.sh`.
- **`settings.template.json`** — general Claude Code settings (statusline pointer,
  agent-teams flag, the generic hook wiring, `permissions.defaultMode: acceptEdits`,
  plugins). Scaffolded to `~/.claude/settings.json` **only when absent** — a
  re-run never overwrites your per-machine tweaks. Bump `defaultMode` or add
  personal permissions in `~/.claude/settings.local.json` (gitignored), which
  Claude Code merges on top.

For a repo that needs its **own** project-specific hooks, two optional
`TEMPLATE.*` stubs ship under `.shared-llm/llm/claude/this_repo/`
(`TEMPLATE.example-hook.py`, `TEMPLATE.settings-overlay.json`) — fill them in and
merge the overlay onto the base with a `type: settings` recipe, or delete them.

This is the machinery behind the two runtime compose types: **`type: copy`** (a file
copied verbatim, executable bit preserved) and **`type: settings`** (JSON inputs
deep-merged — dicts recurse, hook arrays concatenate, scalars overlay-win).

## Pi harness runtime config (optional)

`.shared-llm/llm/pi/common/` holds runtime config for the [Pi coding agent](https://github.com/earendil-works/pi-mono). This directory is **not a compose input** — its contents are never concatenated into `CLAUDE.md` or `AGENTS.md`. Instead, the global step of `just update` symlinks them directly into `~/.pi/` (reconciling: it creates missing links, re-points drifted ones, and prunes links whose source was renamed or deleted) so Pi can discover and load them at runtime.

**What it contains:**

- `extensions/planish.ts` — The Pi-native standalone `/planish` planner. This is a TypeScript extension/register command, not a markdown skill: it adds browser-backed `planish_grill` and `planish_submit_plan` tools and writes `plan.md` + `plan.html` for review. The old standalone markdown skill variants `/do-planish` and `/cc-planish` are intentionally removed; use `/do-plan-and-grill` or `/cc-plan-and-grill` for workflow-suite planning.
- `extensions/context-workflow.ts` — A Pi extension that wraps a structured write→test→review→fix→verify loop. Symlinked into `~/.pi/agent/extensions/` (auto-loaded by the Pi agent on startup).
- `extensions/iac-guard.ts` — A Pi extension that **gates destructive infrastructure commands**. It hooks `tool_call` (before a command runs) and inspects `terraform` / `tofu` / `aws` / `kubectl`: read/create operations run freely; destroys (`destroy` / `delete` / `terminate`) **always require human approval** via the native confirm dialog; gray-zone updates/replaces are judged by the `iac-verifier` agent. Fail-closed — any ambiguity, missing UI, or unavailable verifier falls back to human approval. Symlinked into `~/.pi/agent/extensions/` (auto-loaded). See the policy tables at the top of the file to tune which verbs are allow/ask/gray.
- `extensions/memsearch/` — A directory Pi extension (`index.ts` + `collection.ts`) that gives Pi memory over the **same shared store** Claude Code builds: per-project daily markdown logs under `<git-root>/.memsearch/memory/<YYYY-MM-DD>.md`, indexed into a per-project Milvus collection. The collection name is derived to **exactly match** memsearch's `derive-collection.sh`, so Pi and Claude converge on the same collection per repo.
  - **Recall — full parity.** Same shared store, same `memsearch` CLI Claude uses: a model-callable `memory_search` tool + `/recall <query>` return ranked hits from past sessions, `memory_expand` + `/recall-expand <hash>` open the full section, and on `session_start` it injects a one-line "memory available" hint.
  - **Capture — deliberately NOT full parity.** On `agent_end` it writes **deterministic** third-person notes (what the user asked, which tools the agent used, a clipped agent reply) — **lighter than Claude's and the memsearch codex reference plugin's default**, which run an LLM to summarize the turn. The deterministic path is chosen so capture never makes a blocking nested LLM call. It appends the notes (with a `<!-- session: turn: transcript: -->` anchor) to today's daily log synchronously (the markdown is the source of truth), then runs `memsearch index` in a detached child. If the Milvus Lite single-writer lock is contended (Claude indexing the same store at the same time), the child retries that condition only; on exhaustion it **fails loud** — writes to `~/.pi/agent/memsearch-index.log` and drops an `<!-- index-deferred: <ISO> reason=lock -->` breadcrumb in the daily md — and the next `session_start` re-index catches it up. Non-lock errors fail loud immediately. Nothing is lost because the markdown is already written.
  - Symlinked as a directory into `~/.pi/agent/extensions/` (auto-loaded; Pi loads `index.ts`, with `collection.ts` riding along as an imported helper). `collection.ts` holds just the derivation (Node built-ins only) so it can be unit-tested (`memsearch.test.ts`, golden value `ms_my_project_a26ceb5d` for the example path `/home/user/code/my-project`). Needs the `memsearch` CLI on `PATH` or `uvx` (ONNX embeddings, no API key); if neither is present it no-ops silently.
- `extensions/codex-reviewer-hub.ts`, `extensions/doc-review-hub.ts`, `extensions/pr-review-hub.ts` — Pi extensions that each listen on a Unix socket and dispatch a sub-agent request: adversarial code review, document review, and PR/branch review respectively. `extensions/hub-common.ts` is the shared socket/dispatch helper they import. The hub extensions are symlinked into `~/.pi/extensions/` (loaded when Pi is launched with `-e`).
- `agents/codex-reviewer.md`, `agents/doc-reviewer.md`, `agents/pr-reviewer.md` — The system prompts for the review agents invoked by the hub extensions. Symlinked into `~/.pi/agents/`.
- `agents/iac-verifier.md` — The system prompt for the gray-zone verifier the `iac-guard` gate consults (judges an update/apply's blast radius → ALLOW or ASK). Symlinked into `~/.pi/agents/`.
- `third-party-extensions.txt` — Pinned manifest of **third-party** Pi extensions, one `pi install` source per line. `tools/install-pi-extensions.sh` reads it and installs each via `pi install` (skipping any already present), so the set reproduces on any machine with one command. `just pi-extensions` runs that installer.
- `THIRD-PARTY-EXTENSIONS.md` — The per-extension reference: what each one does, its runtime deps, and the own-vs-third-party rule below.
- `settings.template.json` — A starter Pi settings file (provider, model, thinking level). Copied to `~/.pi/agent/settings.json` only if that file does not already exist, so your live settings are never overwritten. Its `packages` array starts empty — the installer fills it.

### Own vs third-party extensions — keep them separate

Two kinds of Pi extension, two install paths. **Do not mix them.**

- **OWN** (the `.ts` files authored in `extensions/` above) are **symlinked** into `~/.pi/` by the global step of `just update` — copied/layered, never installed from a registry.
- **THIRD-PARTY** are **installed from source** via `pi install`, declared as pinned sources in `third-party-extensions.txt` — never copied or vendored into this repo (no committed `node_modules`).

There is one install path for third-party extensions: `pi install` + the manifest. See `.shared-llm/llm/pi/common/THIRD-PARTY-EXTENSIONS.md` for the full set and per-extension runtime deps.

**Wiring (part of `just update` when `global:` includes `pi`):**

```bash
just update          # symlinks the OWN extensions + personas into ~/.pi/ (global step)
just pi-extensions   # installs the pinned THIRD-PARTY extensions via `pi install`
```

**Launching (requires `tmux`):**

```bash
just hub         # start the socket hub in tmux (serves the review sub-agent sockets + iac-verifier verdicts)
just builder     # launch a builder Pi (iac-guard auto-loads)
just pi-status   # show hub + socket state
just pi-clean    # stop the hub, remove stale sockets
```

The `iac-guard` gate auto-loads in every Pi session. The hub only needs to be running for the gray-zone verifier path — if it is down, the gate fails closed to human approval.

If `~/.pi/agent/extensions/context-workflow.ts` or `~/.pi/agents/codex-reviewer.md` already exists as a real file (not a symlink this kit manages), the global step leaves it untouched and reports it as foreign — it only ever creates, re-points, or prunes the symlinks that resolve into this repo family.

Third-party extensions are installed via `pi install` (into `~/.pi/agent/npm/node_modules/`), never committed here. `settings.template.json` is the template; your live `~/.pi/agent/settings.json` (runtime-mutated by Pi) is never tracked.

**Prerequisites:** Python 3 + PyYAML for the engine, `just` as the entrypoint, a Pi install (`npm install -g @earendil-works/pi-coding-agent`), `node` / `npm` on your `PATH`, and `tmux` (for the `just hub` / `just builder` launch group).

## Terraform workflow (Pi)

A small group of `just` recipes drives a reviewed terraform loop on Pi, gated by `iac-guard`:

```bash
just tf-reviewer-cc          # start the terraform reviewer (Claude Code) — run first
just tf-reviewer-pi          # or the reviewer on Pi
just tf-implement <plan>     # load a plan and write reviewed terraform until approved
just tf-approve              # human-gated apply/destroy with an agent-distilled plan table
just tf-auto <plan>          # implement, then human-gated apply
just tf-reviewer-down        # stop the reviewer
```

## Output-style caveat

An active `explanatory` or `learning` Claude Code output style (set via `/config`) competes with the brevity rule in `.shared-llm/layers/llm/common/common/response.md` and can override it. For terse, structured replies use the default or `concise` output style.

## What is intentionally out of scope

A shared cross-harness skill library (distributing skills across multiple AI assistants beyond the global-install path above) is intentionally out of scope for this kit — it is a candidate for a separate future kit.
