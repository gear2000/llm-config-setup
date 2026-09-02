# LLM-guided install prompt

Use this prompt after cloning `llm-config-setup` when you want Claude Code, Codex, Pi, or another capable coding LLM to install the kit on a machine or add a repository to an existing installation.

```text
You are helping install llm-config-setup. The kit centrally composes reusable public layers plus repository-owned layers into harness-specific CLAUDE.md, AGENTS.md, skills, agents, and Pi runtime links. Run every kit command from the cloned kit checkout, not from a destination repository.

Modes:
1. Fresh machine / kit installation.
2. Add another destination repository to an existing installation.

First inspect before asking questions:
- current OS and whether this checkout is the kit root;
- `python3`, PyYAML, and `just` availability;
- current `~/.shared-llm.yaml` if it exists;
- destination repository roots the user names;
- any repository-root `.shared-llm.yaml` or `.shared-llm.yaml.example` for optional work-log settings.

Ask only for unresolved facts: OS, kit checkout path, source hub path (default `~/.shared-llm`), desired global harnesses (`cc`, `codex`, `pi`, or `cursor`, which uses the Codex surface), destination repository paths and harnesses, project placeholder values, optional UpAgent offering sets (`standard` by default, or `standard,claudex` by explicit opt-in), optional work-log config, optional Pi extensions, and optional justfile module imports.

Keep these files distinct:
- `~/.shared-llm.yaml` is the per-machine source/global/destination roster and UpAgent offering policy maintained by `just configure`.
- A repository-root `.shared-llm.yaml` or `.shared-llm.yaml.example` configures work-log output for planning flows; it is not the machine roster.

Prerequisites:
- Check `python3`, PyYAML, and `just`.
- Ask before installing OS packages or Python packages.
- If prerequisites are missing and the user declines installation, stop with exact missing items.

Commands, always run from the kit checkout:
- `just init -o <mac|ubuntu>` when an OS prereq check is useful.
- Omit `-s` to accept the default source hub, or run `just configure -s <deliberate-hub-path>` such as `~/.shared-llm` when the user deliberately chooses a hub path. Do not pass the kit checkout path to `-s`; the checkout is code, while `source:` is the generated hub copied into by the engine.
- `just configure -g <harnesses>` for global home skills/agents/runtime.
- `just configure -d <repo> -l <harnesses>` for each destination.
- `just configure --offering-sets standard,claudex` only when the machine should opt into ClaudeX; use `just configure -d <repo> --offering-sets standard` when one destination should replace the machine choice with the standard roster.
- `just descriptions` before update.
- `just update` to build, then run it a second time to verify idempotence.

Before changing a destination repository:
1. Inspect it and explain the `.shared-llm/public/` versus `.shared-llm/this_repo/` split.
2. Ask permission before modifying destination files.
3. Seed only needed repository-owned `TEMPLATE.*` stubs under `.shared-llm/this_repo/`.
4. Gather real project values from the user; never invent private details, URLs, account IDs, credential paths, or secrets.
5. Never edit generated outputs (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/*/SKILL.md`, `.claude/agents/*.md`) by hand; edit layers/recipes and recompose.
6. Preserve foreign files and links. Add justfile imports or run `just pi-extensions` only if the user chooses those features.

Fail loudly on unresolved placeholders, malformed config, missing prerequisites, conflicting non-owned files/links, or missing credentials for optional features. Do not overwrite foreign files.

Finish only after `just descriptions` passes, `just update` passes twice, the second update is idempotent, `~/.shared-llm.yaml` has the intended source/global/destinations/UpAgent offering policy, generated destination files and home links point to the intended generated source, and the manifest is consistent. Summarize changed files, installed harnesses, registered repositories, selected offering sets, and optional next steps.
```
