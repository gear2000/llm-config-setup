# llm-config-setup

A portable starter kit for composing AI-assistant instruction files (`CLAUDE.md`, `AGENTS.md`, skill files, and agent personas) from reusable markdown layers. Two install commands — `task install local` (home pieces: general skills, the 18 generic agents, the Pi runtime, the `llm-compose` wrapper) and `task install repo -- <dir>` (set up a target repo) — plus a `task compose:*` build step. See `README.md` for the full layout and install model, and `ONBOARDING.md` for how to adopt it.

---

# ⚠️ THIS IS A PUBLIC REPOSITORY — pre-push vetting is MANDATORY

Everything pushed here is world-readable, permanently, and may be cached or indexed even after deletion. This kit was extracted from a private project and **deliberately stripped of everything proprietary**. Keeping it that way is a hard rule.

## Before ANY push — no exceptions

1. **Vet the full diff for proprietary content.** Nothing originating from any private or internal project may be transmitted. This includes, but is not limited to:
   - Internal/private **project, product, service, or codename** strings.
   - Internal **infrastructure or tooling** names (CI systems, registries, schedulers, hosts).
   - Internal **hostnames, URLs, endpoints**, or network/cluster details.
   - **Cloud account IDs, regions, resource names**, credential paths, tokens, or secrets of any kind.
   - Private **design documents, architecture, design criteria, data models, or implementation approach** — even paraphrased. Generic software-engineering practice is fine; anything that reveals how a specific private system is built is not.

2. **An independent reviewer must PASS the change before it is pushed.** The reviewer is a human — or a separate agent that did **not** author the change — who reads the entire diff against the categories above and explicitly approves. An author vetting their own work does not count.

3. **If anything proprietary is found: STOP.** Do not push. Remove it, re-stage, and re-review from step 1.

**Never run `git push` here without a passing independent review.** When in doubt, do not push — ask a human.

A Claude Code hook (`.claude/settings.json` → `PreToolUse` on `git commit`) runs this same check automatically on every commit as a first line of defense — see `.claude/hooks/proprietary-check.md`. It supplements, not replaces, the independent human/agent review required before push.

---

# Background

This kit started as Claude-only tooling — snippets of markdown composed into a single `CLAUDE.md`. It was later generalized to also target Codex and a custom **Pi** harness, so the same layered source now produces `CLAUDE.md`, `AGENTS.md`, and Pi-native runtime config.

The guiding principle: **things that can be decomposed into reusable layers are layered and composed; things that cannot be meaningfully decomposed are kept as whole, directly-edited pieces** instead of being forced into the layering model.

- **Layered** — prose lives in `.shared-llm/layers/` and is concatenated by `.shared-llm/compose/` recipes into `CLAUDE.md`, `AGENTS.md`, and skill files.
- **Whole pieces** — runtime code and settings aren't decomposable prose, so they stay intact: `.shared-llm/llm/pi/common/` (Pi extensions like `context-workflow.ts` / `iac-guard.ts`, the `memsearch/` dir, agent persona files like `doc-reviewer.md` / `pr-reviewer.md`) is symlinked whole into `~/.pi/`; `.shared-llm/llm/claude/common/` (Claude Code hooks, statusline, settings templates) is copied whole into `~/.claude/`. Neither is ever concatenated into an output file.

---

# Working on this kit

- **Edit the source under `.shared-llm/` — never hand-edit a generated output.** The source is the layer prose in `.shared-llm/layers/` (the `llm/`, `skills/`, and `agents/` trees) and the recipes in `.shared-llm/compose/`. The generated outputs (`CLAUDE.md`, `AGENTS.md`, `SKILL.md`, agent `.md` files) are build artifacts.
- `python3.14 tools/harness.py compose` (or `PYTHON_BIN=python3.14 task compose:all`) regenerates outputs. In this repo, demo outputs land in `examples/` (gitignored) and never overwrite this governance file.
- The Pi runtime under `.shared-llm/llm/pi/common/` is **not** a compose input — its files are symlinked into `~/.pi/` by `python3.14 tools/harness.py sync`, which reconciles the links (creates missing ones, re-points drifted ones, and prunes links whose source was renamed or deleted), never concatenated into any output. Edit those `.ts` / `.md` files directly.
- The install machinery lives in `tools/` (`install-local.sh`, `install-repo.sh`, `install-global.sh`, `harness.py`, `install-pi-extensions.sh`) with copy-time templates in `tools/templates/` (the `llm-compose` wrapper and the thin per-repo `llm.Taskfile.yml`). Keep behavior and docs in sync when you change them.
- When adding a `this_repo` layer, follow the placeholder convention (`{{TOKEN}}` + `<!-- TODO(project): … -->`) and ship it as a `TEMPLATE.*` stub; see `ONBOARDING.md`.
