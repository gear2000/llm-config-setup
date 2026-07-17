# llm-config-setup

A portable starter kit for composing AI-assistant instruction files (`CLAUDE.md`, `AGENTS.md`, skill files, and agent personas) from reusable markdown layers. One centralized engine (`tools/harness.py`, driven by `just`) reads `~/.shared-llm.yaml` and runs every operation from the kit: `just configure -d <repo> -l cc,pi` registers a destination repo, and `just update` (re)builds every configured destination — copying the common layers into each repo's own `.shared-llm/`, composing its `CLAUDE.md` / `AGENTS.md` / skills / agents, and wiring the per-harness skill symlinks (plus the global home skills + runtime when a `global:` list is set). The engine lives only here and is never copied into a destination. See `README.md` for the full layout and model, and `ONBOARDING.md` for how to adopt it.

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

- **Layered** — prose lives in `.shared-llm/public/layers/` and is concatenated by `.shared-llm/public/compose/` recipes into `CLAUDE.md`, `AGENTS.md`, and skill files.
- **Whole pieces** — runtime code and settings aren't decomposable prose, so they stay intact: `.shared-llm/public/llm/pi/common/` (Pi extensions like `context-workflow.ts` / `tf-approve.ts`, the `memsearch/` dir, agent persona files like `doc-reviewer.md` / `pr-reviewer.md`) is symlinked whole into `~/.pi/`; `.shared-llm/public/llm/claude/common/` (Claude Code hooks, statusline, settings templates) is copied whole into `~/.claude/`. Neither is ever concatenated into an output file.

---

# Working on this kit

- **Edit the source under `.shared-llm/` — never hand-edit a generated output.** The source is the layer prose in `.shared-llm/public/layers/` (the `llm/`, `skills/`, and `agents/` trees) and the recipes in `.shared-llm/public/compose/`. The generated outputs (`CLAUDE.md`, `AGENTS.md`, `SKILL.md`, agent `.md` files) are build artifacts.
- `just update` runs the full flow (copy → compose → link, plus global) across every destination in `~/.shared-llm.yaml`; `just test` runs the composer/flow suite. When the kit composes its own home skills and agents (`just global`), the staged outputs land in `examples/` (gitignored) and never overwrite this governance file.
- The Pi runtime under `.shared-llm/public/llm/pi/common/` is **not** a compose input — its files are symlinked into `~/.pi/` by the global step of `just update` (the `just global` reconciler creates missing links, re-points drifted ones, and prunes links whose source was renamed or deleted), never concatenated into any output. The same managed-link reconciliation deploys root `herdr-config.toml` to `~/.config/herdr/config.toml`; it never overwrites a foreign destination. Edit those whole-file sources directly.
- The engine is `tools/harness.py` — the ONE composer/reconciler, driven by the `justfile`. It lives only in this kit and is never copied into a destination (a per-repo copy used to drift and silently break Pi skill discovery). The only other script is `tools/install-pi-extensions.sh`, a `pi install` helper for the pinned third-party Pi extensions, run via `just pi-extensions`. Keep behavior and docs in sync when you change them.
- When adding a `this_repo` layer, follow the placeholder convention (`{{TOKEN}}` + `<!-- TODO(project): … -->`) and ship it as a `TEMPLATE.*` stub; see `ONBOARDING.md`.
