# llm-config-setup

A portable starter kit for assembling `CLAUDE.md`, `AGENTS.md`, and skill files from composable markdown layers. Clone it, fill in the placeholders, run one command, and get instruction files tailored to your project.

## What it does

LLM coding assistants (Claude Code, Codex, etc.) read instruction files — `CLAUDE.md`, `AGENTS.md`, skill `.md` files — to understand your project. Maintaining these by hand gets messy: duplicated prose, inconsistent tone, hard-to-update cross-cutting rules.

This kit solves that by treating instruction files as **build artifacts** assembled from **layers**:

- `layers/llm/common/` — portable rules that apply to any project (response format, coding philosophy).
- `layers/llm/this_repo/` — project-specific context: what the codebase is, how CI works, where credentials live.
- `layers/skills/` — skill definitions used by the assistant as slash commands.
- `layers/compose/` — YAML recipes that declare which layers to combine and where to write the output.
- `tools/compose-layers.py` — the engine that reads recipes and writes output files.

Running `task compose:all` (or `python3 tools/compose-layers.py`) regenerates every output file from its layers.

## Directory layout

Files under `this_repo/` and `skills/this_repo/` ship as `TEMPLATE.<name>.md` stubs — fill them in, delete the banner, and rename to drop the `TEMPLATE.` prefix. The `common/` files are ready-to-use as-is.

```
layers/
  llm/
    common/response.md              — portable response-format rules (copy verbatim)
    this_repo/
      general.md                    — project identity, coding conventions, CI, credentials
      claude.md                     — Claude Code-specific conventions (worktrees, planning)
      agents.md                     — cross-harness wiring (skills, hooks, MCP)
      authoring.md                  — example: CLAUDE.md for a special subdirectory
      aws-execution-engine.md       — example: CLAUDE.md for a component with a contract
      src/packages.md               — shared context for all package CLAUDE.mds
      src/services.md               — shared context for all service CLAUDE.mds
      packages/example_package.md  — per-package leaf template
      services/example_service.md  — per-service leaf template
  skills/
    common/python/
      practices.md                  — language conventions (copy verbatim)
      description.md                — one-line skill description (lightly de-branded)
    this_repo/python.md             — project-specific Python skill layer
  compose/
    claude-md/root.yaml             — recipe: root CLAUDE.md
    claude-md/example-package.yaml — recipe: per-package CLAUDE.md
    claude-md/example-service.yaml — recipe: per-service CLAUDE.md
    agents-md/root.yaml             — recipe: root AGENTS.md
    skills/python.yaml              — recipe: python skill
tools/
  compose-layers.py                 — the engine
Taskfile.yml                        — compose:all, compose:skill, compose:claude-md, etc.
ONBOARDING.md                       — ordered checklist to fill every placeholder
```

## Quickstart

```bash
# 1. Install the one dependency
pip install pyyaml

# 2. See which files need you (TEMPLATE.* = unfilled stubs)
find . -name 'TEMPLATE.*'

# 3. For each: fill in every {{...}} and "FILL THIS OUT", delete the banner,
#    then rename it to drop the "TEMPLATE." prefix (TEMPLATE.general.md -> general.md).
#    See ONBOARDING.md for the field-by-field checklist.

# 4. Generate your CLAUDE.md / AGENTS.md / skill files
python3 tools/compose-layers.py
# or, with Task installed:
task compose:all
```

## Placeholder convention

Stubs use two marker styles:

- **Block-level guidance** — HTML comment: `<!-- TODO(project): explain what goes here -->`
- **Inline fill-in values** — double-brace token: `{{PROJECT_NAME}}`, `{{CRED_ROOT}}`, etc.

When you are done onboarding, this command returns nothing:

```bash
grep -rn '{{\|TODO(project)' layers/
```

See `ONBOARDING.md` for the ordered fill-in checklist.

## Pi harness runtime config (optional)

`layers/llm/pi/common/` holds runtime config for the [Pi coding agent](https://github.com/earendil-works/pi-mono). This directory is **not a compose input** — its contents are never concatenated into `CLAUDE.md` or `AGENTS.md`. Instead, `tools/setup-pi.sh` symlinks them directly into `~/.pi/` so Pi can discover and load them at runtime.

**What it contains:**

- `extensions/context-workflow.ts` — A Pi extension that wraps a structured write→test→review→fix→verify loop. Symlinked into `~/.pi/agent/extensions/` (auto-loaded by the Pi agent on startup).
- `extensions/iac-guard.ts` — A Pi extension that **gates destructive infrastructure commands**. It hooks `tool_call` (before a command runs) and inspects `terraform` / `tofu` / `aws` / `kubectl`: read/create operations run freely; destroys (`destroy` / `delete` / `terminate`) **always require human approval** via the native confirm dialog; gray-zone updates/replaces are judged by the `iac-verifier` agent. Fail-closed — any ambiguity, missing UI, or unavailable verifier falls back to human approval. Symlinked into `~/.pi/agent/extensions/` (auto-loaded). See the policy tables at the top of the file to tune which verbs are allow/ask/gray.
- `extensions/memsearch/` — A directory Pi extension (`index.ts` + `collection.ts`) that gives Pi memory over the **same shared store** Claude Code builds: per-project daily markdown logs under `<git-root>/.memsearch/memory/<YYYY-MM-DD>.md`, indexed into a per-project Milvus collection. The collection name is derived to **exactly match** memsearch's `derive-collection.sh`, so Pi and Claude converge on the same collection per repo.
  - **Recall — full parity.** Same shared store, same `memsearch` CLI Claude uses: a model-callable `memory_search` tool + `/recall <query>` return ranked hits from past sessions, `memory_expand` + `/recall-expand <hash>` open the full section, and on `session_start` it injects a one-line "memory available" hint.
  - **Capture — deliberately NOT full parity.** On `agent_end` it writes **deterministic** third-person notes (what the user asked, which tools the agent used, a clipped agent reply) — **lighter than Claude's and the memsearch codex reference plugin's default**, which run an LLM to summarize the turn. The deterministic path is chosen so capture never makes a blocking nested LLM call. It appends the notes (with a `<!-- session: turn: transcript: -->` anchor) to today's daily log synchronously (the markdown is the source of truth), then runs `memsearch index` in a detached child. If the Milvus Lite single-writer lock is contended (Claude indexing the same store at the same time), the child retries that condition only; on exhaustion it **fails loud** — writes to `~/.pi/agent/memsearch-index.log` and drops an `<!-- index-deferred: <ISO> reason=lock -->` breadcrumb in the daily md — and the next `session_start` re-index catches it up. Non-lock errors fail loud immediately. Nothing is lost because the markdown is already written.
  - Symlinked as a directory into `~/.pi/agent/extensions/` (auto-loaded; Pi loads `index.ts`, with `collection.ts` riding along as an imported helper). `collection.ts` holds just the derivation (Node built-ins only) so it can be unit-tested (`memsearch.test.ts`, golden value `ms_my_project_a26ceb5d` for the example path `/home/user/code/my-project`). Needs the `memsearch` CLI on `PATH` or `uvx` (ONNX embeddings, no API key); if neither is present it no-ops silently.
- `extensions/codex-reviewer-hub.ts` — A Pi extension that listens on a Unix socket and dispatches sub-agent requests — adversarial code review (default), and the `iac-verifier` gray-zone verdicts for the gate above. Symlinked into `~/.pi/extensions/` (loaded when Pi is launched with `-e`).
- `agents/codex-reviewer.md` — The system prompt for the adversarial reviewer agent invoked by the hub extension. Symlinked into `~/.pi/agents/`.
- `agents/iac-verifier.md` — The system prompt for the gray-zone verifier the `iac-guard` gate consults (judges an update/apply's blast radius → ALLOW or ASK). Symlinked into `~/.pi/agents/`.
- `third-party-extensions.txt` — Pinned manifest of **third-party** Pi extensions, one `pi install` source per line. `tools/install-pi-extensions.sh` reads it and installs each via `pi install` (skipping any already present), so the set reproduces on any machine with one command. `setup-pi.sh` calls that installer.
- `THIRD-PARTY-EXTENSIONS.md` — The per-extension reference: what each one does, its runtime deps, and the own-vs-third-party rule below.
- `settings.template.json` — A starter Pi settings file (provider, model, thinking level). Copied to `~/.pi/agent/settings.json` only if that file does not already exist, so your live settings are never overwritten. Its `packages` array starts empty — the installer fills it.

### Own vs third-party extensions — keep them separate

Two kinds of Pi extension, two install paths. **Do not mix them.**

- **OWN** (the `.ts` files authored in `extensions/` above) are **symlinked** into `~/.pi/` by `setup-pi.sh` — copied/layered, never installed from a registry.
- **THIRD-PARTY** are **installed from source** via `pi install`, declared as pinned sources in `third-party-extensions.txt` — never copied or vendored into this repo (no committed `node_modules`).

There is one install path for third-party extensions: `pi install` + the manifest. The previous `npm ci` route is gone. See `layers/llm/pi/common/THIRD-PARTY-EXTENSIONS.md` for the full set and per-extension runtime deps.

**Setup:**

```bash
# Wire everything up (idempotent — safe to re-run)
task setup:pi

# Undo: remove the symlinks (leaves settings.json and installed third-party extensions)
task setup:pi:unlink
```

**Launching (requires `tmux`):**

```bash
task up        # symlinks + socket hub (in tmux) + builder Pi — one command
task hub       # just the hub (serves codex reviews + iac-verifier verdicts)
task status    # show hub + socket state
task clean     # stop the hub, remove stale sockets
```

The `iac-guard` gate auto-loads in every Pi session. The hub only needs to be running for the gray-zone verifier path — if it is down, the gate fails closed to human approval. These launch tasks are named unprefixed so a higher-level Taskfile can import them (`includes: { pi: { taskfile: ./Taskfile.yml, dir: . } }` → `task pi:up`), or run them directly with `task -d <this-dir> up`.

If `~/.pi/agent/extensions/context-workflow.ts` or `~/.pi/agents/codex-reviewer.md` already exists as a real file and is byte-identical to this kit's copy, `setup-pi.sh` migrates it to a repo-managed symlink automatically. If the files differ, the existing file is left untouched and a warning is printed.

Third-party extensions are installed via `pi install` (into `~/.pi/agent/npm/node_modules/`), never committed here. `settings.template.json` is the template; your live `~/.pi/agent/settings.json` (runtime-mutated by Pi) is never tracked.

**Prerequisites:** a Pi install (`npm install -g @earendil-works/pi-coding-agent`), `node` / `npm` on your `PATH`, and `tmux` (for the `task up` / `task hub` launch group).

## Output-style caveat

An active `explanatory` or `learning` Claude Code output style (set via `/config`) competes with the brevity rule in `layers/llm/common/response.md` and can override it. For terse, structured replies use the default or `concise` output style.

## What is intentionally out of scope

A shared cross-harness skill library (distributing skills across multiple AI assistants) is intentionally out of scope for this kit — it is a candidate for a separate future kit.

## Running compose here

The example recipes write demo outputs under `examples/` (gitignored), so they never touch this repo's own `CLAUDE.md`. In your own project, change each recipe's `output:` path to a real location (e.g. `CLAUDE.md`, `AGENTS.md`, `.claude/skills/<name>/SKILL.md`) and commit those generated files.
