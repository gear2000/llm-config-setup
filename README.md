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

## Output-style caveat

An active `explanatory` or `learning` Claude Code output style (set via `/config`) competes with the brevity rule in `layers/llm/common/response.md` and can override it. For terse, structured replies use the default or `concise` output style.

## What is intentionally out of scope

A shared cross-harness skill library (distributing skills across multiple AI assistants) is intentionally out of scope for this kit — it is a candidate for a separate future kit.

## Running compose here

The example recipes write demo outputs under `examples/` (gitignored), so they never touch this repo's own `CLAUDE.md`. In your own project, change each recipe's `output:` path to a real location (e.g. `CLAUDE.md`, `AGENTS.md`, `.claude/skills/<name>/SKILL.md`) and commit those generated files.
