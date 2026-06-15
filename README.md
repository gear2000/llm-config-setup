# llm-config-setup

A portable starter kit for assembling `CLAUDE.md`, `AGENTS.md`, skill files, and agent personas from composable markdown layers. Install it into your home and into a target repo with two commands, fill in the placeholders, run compose, and get instruction files tailored to your project.

## What it does

LLM coding assistants (Claude Code, Codex, Pi, etc.) read instruction files — `CLAUDE.md`, `AGENTS.md`, skill `.md` files, agent persona `.md` files — to understand your project and to pick up specialized roles. Maintaining these by hand gets messy: duplicated prose, inconsistent tone, hard-to-update cross-cutting rules.

This kit treats those files as **build artifacts** assembled from **layers**:

- `.shared-llm/layers/` — the source prose, split into reusable layers.
- `.shared-llm/compose/` — YAML recipes that declare which layers to combine and where to write the output.
- `tools/compose-layers.py` — the engine that reads recipes and writes output files.

Two commands install it:

- `task install local` — the home pieces (general skills, the 18 generic agents, the Pi runtime, the `llm-compose` wrapper).
- `task install repo -- <dir>` — sets up a target repo (copies the layer tree + engine + a thin Taskfile, then composes its `CLAUDE.md` / `AGENTS.md`).

## The `.shared-llm/` layout

The repo splits into the **source tree** (`.shared-llm/`) and the **install/engine machinery** (`tools/`, `Taskfile.yml`).

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
    global/python.yaml            — recipe: GENERAL python skill (no this_repo layer; home install)
    global/nextjs.yaml            — recipe: general nextjs skill (home install)
    global/backend.yaml           — recipe: general backend skill (home install)
    agents/<name>.yaml            — recipe: one generic agent persona (18 of them)
  llm/pi/common/                  — Pi harness runtime config (NOT a compose input; see below)
tools/
  compose-layers.py               — the compose engine
  install-local.sh                — `task install local` worker (home pieces)
  install-repo.sh                 — `task install repo` worker (target repo)
  install-global.sh               — composes + installs the general home skills
  setup-pi.sh                     — symlinks the Pi runtime into ~/.pi
  install-pi-extensions.sh        — installs the pinned third-party Pi extensions
  templates/
    llm-compose                   — the wrapper copied to ~/.local/bin/llm-compose
    llm.Taskfile.yml              — the thin compose Taskfile copied into a target repo
Taskfile.yml                      — install:*, compose:*, setup:*, plus the Pi launch group
ONBOARDING.md                     — token-by-token checklist for filling the TEMPLATE stubs
```

**layers vs compose vs llm/pi:**

- **layers** are pure source prose — never generated, never frontmatter. The portable `common/` layers ship ready-to-use; the `this_repo/` layers ship as `TEMPLATE.*` stubs you fill in.
- **compose** recipes are the build instructions: each YAML names the input layers, the output path, and (for skills/agents) the frontmatter name + description. The engine concatenates the inputs and writes the output.
- **llm/pi** is the Pi coding agent's runtime config (extensions, agent personas, settings template). It is **not** a compose input — its files are never concatenated into any output. `tools/setup-pi.sh` symlinks them straight into `~/.pi/`. See "Pi harness runtime config" below.

## The install model

There are two install surfaces. They are independent — run one, the other, or both.

### `task install local` — the home pieces (all projects)

Installs everything that lives in your `$HOME` and applies across every project:

1. **General home skills** — composes the `global/` recipes (`python`, `nextjs`, `backend`) and copies each `SKILL.md` into the home skill dir every harness reads: `~/.claude/skills/`, `~/.codex/skills/`, `~/.pi/skills/`.
2. **The 18 generic agents** — composes the `agents/` recipes and copies each persona into the home agent dirs: `~/.claude/agents/` and `~/.pi/agents/`. (Codex has no user-agent directory, so it is skipped — the installer never invents one.)
3. **Pi runtime** — delegates to `setup-pi.sh` (symlinks the bundled Pi extensions + agent personas, scaffolds settings) and `install-pi-extensions.sh` (the pinned third-party extensions).
4. **The `llm-compose` wrapper** — copies `tools/templates/llm-compose` to `~/.local/bin/llm-compose` (executable).

It is idempotent and safe: a re-run installs nothing new when home already matches, and it never clobbers a divergent or foreign file (an existing file that does not match this kit's copy, or a symlink it did not create, is left untouched with a warning).

```bash
task install local
# skip the third-party `pi install` network step:
task install local -- --skip-pi-extensions
```

### `task install repo -- <dir>` — set up a target repo (per repo)

Makes a target repo **self-contained**:

1. Copies the portable `.shared-llm/` layer tree, the compose engine (`tools/compose-layers.py`), and a thin compose Taskfile into `<dir>`. The `this_repo` layers arrive as fillable `TEMPLATE.*` stubs. (If `<dir>` already has a `Taskfile.yml`, the thin one is dropped alongside as `tools/llm.Taskfile.yml` to include, rather than overwriting yours.)
2. Prints the list of `TEMPLATE.*` stubs you must fill, then pauses. Set `INSTALL_REPO_YES=1` (or pipe non-TTY stdin) to skip the pause for automation.
3. Composes against the target — but only if every stub is filled and renamed. While stubs remain, compose is skipped and the remaining stubs are reported (you cannot compose against an unfilled stub).
4. Prints a summary of what was generated and what is not installed here (the home pieces come from `task install local`).

```bash
task install repo -- /path/to/repo
# default target is the current directory:
task install repo
# non-interactive (skip the fill pause):
INSTALL_REPO_YES=1 task install repo -- /path/to/repo
```

After the repo is set up, you fill the stubs (see `ONBOARDING.md`), then recompose with the thin Taskfile it carries or the wrapper:

```bash
cd /path/to/repo
task compose:all        # uses the thin Taskfile copied in by install repo
# or:
llm-compose             # the wrapper installed by `install local`
```

## The `llm-compose` wrapper

`llm-compose` (installed to `~/.local/bin` by `task install local`) is a thin wrapper around the compose engine. It carries no logic of its own: it finds the nearest `.shared-llm/` root (walking up from `$PWD`, or `$SHARED_LLM_DIR` if set), locates the engine sitting next to it (`<repo>/tools/compose-layers.py`, copied in by `install repo`), and execs it — passing every argument straight through.

```bash
llm-compose                                          # compose all recipes
llm-compose .shared-llm/compose/claude-md/root.yaml  # compose one
llm-compose --target /some/out                       # override the output base
```

The wrapper is a convenience, not a requirement: a repo set up via `install repo` carries its own engine and thin Taskfile, so it can always recompose with `task compose:all` even without the wrapper.

## The 18 generic agents

`task install local` ships these brand-free agent personas into your home agent dirs. They contain no project-specific references — adopt them as-is. Each is composed from a body layer (`agents/common/<name>.md`) plus a one-line description (`agents/common/<name>.description.md`).

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

Compose one on its own with `task compose:agent -- <name>`, or all of them with `task compose:agents`.

## The compose engine

`tools/compose-layers.py` reads a recipe YAML and writes one output file. It decouples two roots:

- **source** — the `.shared-llm/` dir (layers + recipes). Inputs and descriptions resolve against it. Selected via `--shared-llm`, then `$SHARED_LLM_DIR`, then a walk-up for `.shared-llm/`.
- **target** — the output base where each recipe's `output:` path lands. Selected via `--target`, default the current directory.

Recipe types: `skill` (frontmatter name + description), `agent` (name + description + model, optional color), `claude-md` / `agents-md` (plain concatenated markdown, no frontmatter), and `prompt` (a whole feature prompt assembled from an explicit manifest). A recipe may also declare a `catalog:` partial injected before its `inputs`.

```bash
python3 tools/compose-layers.py                                     # compose all
python3 tools/compose-layers.py .shared-llm/compose/claude-md/root.yaml  # compose one
# or via Task:
task compose:all
task compose:claude-md -- root
task compose:skill -- python
task compose:agent -- deployer
```

## Where compose outputs land in this repo

In this repo the example recipes write demo outputs under `examples/` (gitignored), so composing here never touches this repo's own `CLAUDE.md`. That staging path is just a default. When you set up a real project with `task install repo`, the engine composes with `--target <dir>` so each recipe's `output:` lands inside that repo (e.g. `CLAUDE.md`, `AGENTS.md`, `.claude/skills/<name>/SKILL.md`). See `ONBOARDING.md` for how a consumer points outputs where they want them and commits the generated files.

## Quickstart

```bash
# 1. One dependency for the engine
pip install pyyaml

# 2. Install the home pieces (skills, agents, Pi runtime, llm-compose)
task install local

# 3. Set up a target repo
task install repo -- /path/to/your/repo

# 4. Fill the TEMPLATE.* stubs it printed (see ONBOARDING.md), then compose
cd /path/to/your/repo
task compose:all
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

## Pi harness runtime config (optional)

`.shared-llm/llm/pi/common/` holds runtime config for the [Pi coding agent](https://github.com/earendil-works/pi-mono). This directory is **not a compose input** — its contents are never concatenated into `CLAUDE.md` or `AGENTS.md`. Instead, `tools/setup-pi.sh` symlinks them directly into `~/.pi/` so Pi can discover and load them at runtime. `task install local` runs this for you.

**What it contains:**

- `extensions/context-workflow.ts` — A Pi extension that wraps a structured write→test→review→fix→verify loop. Symlinked into `~/.pi/agent/extensions/` (auto-loaded by the Pi agent on startup).
- `extensions/iac-guard.ts` — A Pi extension that **gates destructive infrastructure commands**. It hooks `tool_call` (before a command runs) and inspects `terraform` / `tofu` / `aws` / `kubectl`: read/create operations run freely; destroys (`destroy` / `delete` / `terminate`) **always require human approval** via the native confirm dialog; gray-zone updates/replaces are judged by the `iac-verifier` agent. Fail-closed — any ambiguity, missing UI, or unavailable verifier falls back to human approval. Symlinked into `~/.pi/agent/extensions/` (auto-loaded). See the policy tables at the top of the file to tune which verbs are allow/ask/gray.
- `extensions/memsearch/` — A directory Pi extension (`index.ts` + `collection.ts`) that gives Pi memory over the **same shared store** Claude Code builds: per-project daily markdown logs under `<git-root>/.memsearch/memory/<YYYY-MM-DD>.md`, indexed into a per-project Milvus collection. The collection name is derived to **exactly match** memsearch's `derive-collection.sh`, so Pi and Claude converge on the same collection per repo.
  - **Recall — full parity.** Same shared store, same `memsearch` CLI Claude uses: a model-callable `memory_search` tool + `/recall <query>` return ranked hits from past sessions, `memory_expand` + `/recall-expand <hash>` open the full section, and on `session_start` it injects a one-line "memory available" hint.
  - **Capture — deliberately NOT full parity.** On `agent_end` it writes **deterministic** third-person notes (what the user asked, which tools the agent used, a clipped agent reply) — **lighter than Claude's and the memsearch codex reference plugin's default**, which run an LLM to summarize the turn. The deterministic path is chosen so capture never makes a blocking nested LLM call. It appends the notes (with a `<!-- session: turn: transcript: -->` anchor) to today's daily log synchronously (the markdown is the source of truth), then runs `memsearch index` in a detached child. If the Milvus Lite single-writer lock is contended (Claude indexing the same store at the same time), the child retries that condition only; on exhaustion it **fails loud** — writes to `~/.pi/agent/memsearch-index.log` and drops an `<!-- index-deferred: <ISO> reason=lock -->` breadcrumb in the daily md — and the next `session_start` re-index catches it up. Non-lock errors fail loud immediately. Nothing is lost because the markdown is already written.
  - Symlinked as a directory into `~/.pi/agent/extensions/` (auto-loaded; Pi loads `index.ts`, with `collection.ts` riding along as an imported helper). `collection.ts` holds just the derivation (Node built-ins only) so it can be unit-tested (`memsearch.test.ts`, golden value `ms_my_project_a26ceb5d` for the example path `/home/user/code/my-project`). Needs the `memsearch` CLI on `PATH` or `uvx` (ONNX embeddings, no API key); if neither is present it no-ops silently.
- `extensions/codex-reviewer-hub.ts`, `extensions/doc-review-hub.ts`, `extensions/pr-review-hub.ts` — Pi extensions that each listen on a Unix socket and dispatch a sub-agent request: adversarial code review, document review, and PR/branch review respectively. `extensions/hub-common.ts` is the shared socket/dispatch helper they import. The hub extensions are symlinked into `~/.pi/extensions/` (loaded when Pi is launched with `-e`).
- `agents/codex-reviewer.md`, `agents/doc-reviewer.md`, `agents/pr-reviewer.md` — The system prompts for the review agents invoked by the hub extensions. Symlinked into `~/.pi/agents/`.
- `agents/iac-verifier.md` — The system prompt for the gray-zone verifier the `iac-guard` gate consults (judges an update/apply's blast radius → ALLOW or ASK). Symlinked into `~/.pi/agents/`.
- `third-party-extensions.txt` — Pinned manifest of **third-party** Pi extensions, one `pi install` source per line. `tools/install-pi-extensions.sh` reads it and installs each via `pi install` (skipping any already present), so the set reproduces on any machine with one command. `setup-pi.sh` calls that installer.
- `THIRD-PARTY-EXTENSIONS.md` — The per-extension reference: what each one does, its runtime deps, and the own-vs-third-party rule below.
- `settings.template.json` — A starter Pi settings file (provider, model, thinking level). Copied to `~/.pi/agent/settings.json` only if that file does not already exist, so your live settings are never overwritten. Its `packages` array starts empty — the installer fills it.

### Own vs third-party extensions — keep them separate

Two kinds of Pi extension, two install paths. **Do not mix them.**

- **OWN** (the `.ts` files authored in `extensions/` above) are **symlinked** into `~/.pi/` by `setup-pi.sh` — copied/layered, never installed from a registry.
- **THIRD-PARTY** are **installed from source** via `pi install`, declared as pinned sources in `third-party-extensions.txt` — never copied or vendored into this repo (no committed `node_modules`).

There is one install path for third-party extensions: `pi install` + the manifest. See `.shared-llm/llm/pi/common/THIRD-PARTY-EXTENSIONS.md` for the full set and per-extension runtime deps.

**Setup (also run by `task install local`):**

```bash
# Wire everything up (idempotent — safe to re-run)
task setup:pi

# Undo: remove the symlinks (leaves settings.json and installed third-party extensions)
task setup:pi:unlink
```

**Launching (requires `tmux`):**

```bash
task up        # symlinks + socket hub (in tmux) + builder Pi — one command
task hub       # just the hub (serves the review sub-agent sockets + iac-verifier verdicts)
task status    # show hub + socket state
task clean     # stop the hub, remove stale sockets
```

The `iac-guard` gate auto-loads in every Pi session. The hub only needs to be running for the gray-zone verifier path — if it is down, the gate fails closed to human approval. These launch tasks are named unprefixed so a higher-level Taskfile can import them (`includes: { pi: { taskfile: ./Taskfile.yml, dir: . } }` → `task pi:up`), or run them directly with `task -d <this-dir> up`.

If `~/.pi/agent/extensions/context-workflow.ts` or `~/.pi/agents/codex-reviewer.md` already exists as a real file and is byte-identical to this kit's copy, `setup-pi.sh` migrates it to a repo-managed symlink automatically. If the files differ, the existing file is left untouched and a warning is printed.

Third-party extensions are installed via `pi install` (into `~/.pi/agent/npm/node_modules/`), never committed here. `settings.template.json` is the template; your live `~/.pi/agent/settings.json` (runtime-mutated by Pi) is never tracked.

**Prerequisites:** a Pi install (`npm install -g @earendil-works/pi-coding-agent`), `node` / `npm` on your `PATH`, and `tmux` (for the `task up` / `task hub` launch group).

## Output-style caveat

An active `explanatory` or `learning` Claude Code output style (set via `/config`) competes with the brevity rule in `.shared-llm/layers/llm/common/common/response.md` and can override it. For terse, structured replies use the default or `concise` output style.

## What is intentionally out of scope

A shared cross-harness skill library (distributing skills across multiple AI assistants beyond the home-install path above) is intentionally out of scope for this kit — it is a candidate for a separate future kit.
