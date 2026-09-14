# llm-config-setup

A portable starter kit for composing AI-assistant instruction files (`CLAUDE.md`, `AGENTS.md`, skill files, and agent personas) from reusable markdown layers. One centralized engine (`tools/harness.py`, driven by `just`) reads `~/.shared-llm.yaml` and runs every operation from the kit: `just configure -d <repo> -l cc,pi` registers a destination repo, and `just update` (re)builds every configured destination — copying the common layers into each repo's own `.shared-llm/`, composing its `CLAUDE.md` / `AGENTS.md` / skills / agents, and wiring the per-harness skill symlinks (plus the global home skills + runtime when a `global:` list is set). The engine lives only here and is never copied into a destination. See `README.md` for the full layout and model, `UPINSTALL.md` for kit install/update, `SETUP-DESTINATION.md` for adding a destination repository, and `SETUP-SPECIALISTS.md` for dest-contextual UpAgent specialists.

---

# ⚠️ THIS IS A PUBLIC REPOSITORY

Everything pushed here is world-readable, permanently. This kit was extracted from a private project and **deliberately stripped of everything proprietary**. Keeping it that way is a hard rule.

Nothing originating from any private or internal project may be transmitted: internal project, product, service or codename strings; internal infrastructure or tooling names; internal hostnames, URLs or endpoints; cloud account IDs, regions, resource names, credential paths, tokens or secrets; private design documents or implementation details, even paraphrased. Generic software-engineering practice is fine.

A Claude Code hook (`.claude/settings.json` → `PreToolUse` on `git commit`) runs this check on every commit; see `.claude/hooks/proprietary-check.md`. If it finds anything, remove it before committing. Beyond that, push freely. An adversarial review of a PR is a quality choice the human makes, never a gate on pushing.

---

# Background

This kit started as Claude-only tooling — snippets of markdown composed into a single `CLAUDE.md`. It was later generalized to also target Codex and a custom **Pi** harness, so the same layered source now produces `CLAUDE.md`, `AGENTS.md`, and Pi-native runtime config.

The guiding principle: **things that can be decomposed into reusable layers are layered and composed; things that cannot be meaningfully decomposed are kept as whole, directly-edited pieces** instead of being forced into the layering model.

- **Layered** — prose lives in `.shared-llm/public/layers/` and is concatenated by `.shared-llm/public/compose/` recipes into `CLAUDE.md`, `AGENTS.md`, and skill files.
- **Whole pieces** — runtime code and settings aren't decomposable prose, so they stay intact: `.shared-llm/public/llm/pi/common/` (Pi extensions like `context-workflow.ts` / `tf-approve.ts`, the `memsearch/` dir, agent persona files like `doc-reviewer.md` / `pr-reviewer.md`) and `.shared-llm/public/llm/claude/common/` (Claude Code hooks, statusline, settings templates) are deployed whole into `~/.pi/` and `~/.claude/` — except `settings.json`, which is scaffolded once as a real file because Claude Code mutates it at runtime. Neither is ever concatenated into an output file. Every home deployment goes through the durable per-machine generated tree `~/.shared-llm/generated/`: composed skills and agents, and the whole pieces (hooks, statusline, Pi extensions and personas, herdr config), are **copied** there, and the home paths are **symlinks into that tree** — never into this repository checkout, so the kit can move or disappear without breaking the runtime. Every deployment is recorded in `~/.shared-llm/manifest.json`, so `readlink` distinguishes generated from handwritten and `just update` (or `just prune`) removes deployments whose source was renamed or retired.

---

# Working on this kit

- **Edit the source under `.shared-llm/` — never hand-edit a generated output.** The source is the layer prose in `.shared-llm/public/layers/` (the `llm/`, `skills/`, and `agents/` trees) and the recipes in `.shared-llm/public/compose/`. The generated outputs (`CLAUDE.md`, `AGENTS.md`, `SKILL.md`, agent `.md` files) are build artifacts.
- `just update` runs the full flow (copy → compose → link, plus global) across every destination in `~/.shared-llm.yaml`; `just test` runs the composer/flow suite. When the kit composes its own home skills and agents (`just global`), the staged outputs land in `examples/` (gitignored) and never overwrite this governance file. **Never treat “home skills lag this checkout” as a finding.** `just update` is assumed whenever this kit is used.
- The Pi runtime under `.shared-llm/public/llm/pi/common/` is **not** a compose input — its files are copied into `~/.shared-llm/generated/` and linked from there into `~/.pi/` by the global step of `just update` (the `just global` reconciler refreshes the generated copies, creates missing links, re-points drifted ones, and prunes links whose source was renamed or deleted), never concatenated into any output. The same reconciliation deploys root `herdr-config.toml` to `~/.config/herdr/config.toml` through a generated copy; it never overwrites a foreign destination. Edit those whole-file sources directly.
- The engine is `tools/harness.py` — the ONE composer/reconciler, driven by the `justfile`. It lives only in this kit and is never copied into a destination (a per-repo copy used to drift and silently break Pi skill discovery). Install helpers in `tools/`: `install-pi-extensions.sh` (`just pi-extensions`), `install-herdr.sh` (`just herdr-pin`, Herdr **0.7.1** only), `install-misc.sh` (`just misc`: Lavish, Impeccable, quota-axi, Plannotator). Keep behavior and docs in sync when you change them.
- When adding a `this_repo` layer, follow the placeholder convention (`{{TOKEN}}` + `<!-- TODO(project): … -->`) and ship it as a `TEMPLATE.*` stub; see `SETUP-DESTINATION.md`.
