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

`layers/llm/pi/common/` holds runtime config for the [Pi coding agent](https://github.com/badlogic/pi). This directory is **not a compose input** — its contents are never concatenated into `CLAUDE.md` or `AGENTS.md`. Instead, `tools/setup-pi.sh` symlinks them directly into `~/.pi/` so Pi can discover and load them at runtime.

**What it contains:**

- `extensions/context-workflow.ts` — A Pi extension that wraps a structured write→test→review→fix→verify loop. Symlinked into `~/.pi/agent/extensions/` (auto-loaded by the Pi agent on startup).
- `extensions/codex-reviewer-hub.ts` — A Pi extension that listens on a Unix socket and dispatches review requests to a second LLM for adversarial code review. Symlinked into `~/.pi/extensions/` (loaded when Pi is launched with `-e`).
- `agents/codex-reviewer.md` — The system prompt for the adversarial reviewer agent invoked by the hub extension. Symlinked into `~/.pi/agents/`.
- `npm/package.json` + `npm/package-lock.json` — The npm manifest for Pi's extension dependencies. `setup-pi.sh` runs `npm ci` into `~/.pi/agent/npm/` if `node_modules` is absent.
- `settings.template.json` — A starter Pi settings file (provider, model, thinking level, packages). Copied to `~/.pi/agent/settings.json` only if that file does not already exist, so your live settings are never overwritten.

**Setup:**

```bash
# Wire everything up (idempotent — safe to re-run)
task setup:pi

# Undo: remove the symlinks (leaves settings.json and node_modules)
task setup:pi:unlink
```

If `~/.pi/agent/extensions/context-workflow.ts` or `~/.pi/agents/codex-reviewer.md` already exists as a real file and is byte-identical to this kit's copy, `setup-pi.sh` migrates it to a repo-managed symlink automatically. If the files differ, the existing file is left untouched and a warning is printed.

`node_modules` is never committed. `settings.template.json` is the template; your live `~/.pi/agent/settings.json` (runtime-mutated by Pi) is never tracked.

**Prerequisites:** a Pi install (`npm install -g @mariozechner/pi`), and `node` / `npm` on your `PATH`.

## Output-style caveat

An active `explanatory` or `learning` Claude Code output style (set via `/config`) competes with the brevity rule in `layers/llm/common/response.md` and can override it. For terse, structured replies use the default or `concise` output style.

## What is intentionally out of scope

A shared cross-harness skill library (distributing skills across multiple AI assistants) is intentionally out of scope for this kit — it is a candidate for a separate future kit.

## Running compose here

The example recipes write demo outputs under `examples/` (gitignored), so they never touch this repo's own `CLAUDE.md`. In your own project, change each recipe's `output:` path to a real location (e.g. `CLAUDE.md`, `AGENTS.md`, `.claude/skills/<name>/SKILL.md`) and commit those generated files.
