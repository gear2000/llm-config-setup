# llm-config-setup

A portable starter kit for composing AI-assistant instruction files (`CLAUDE.md`, `AGENTS.md`, skill files) from reusable markdown layers. See `README.md` for what it does and `ONBOARDING.md` for how to adopt it.

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

## Local vetting aid

A local, gitignored checklist lives under `.vetting/` (never committed — it would itself leak the terms it screens for). Run `.vetting/check.sh` before a push to scan tracked files against the known-proprietary denylist. A clean scan is **necessary but not sufficient**: it catches known strings only — the human/agent reviewer must still judge design and approach leakage that no denylist can catch.

---

# Working on this kit

- Edit the source **layers** under `layers/`; never hand-edit generated outputs.
- `python3 tools/compose-layers.py` (or `task compose:all`) regenerates outputs. In this repo, demo outputs land in `examples/` (gitignored) and never overwrite this governance file.
- When adding a layer, follow the placeholder convention (`{{TOKEN}}` + `<!-- TODO(project): … -->`); see `ONBOARDING.md`.
