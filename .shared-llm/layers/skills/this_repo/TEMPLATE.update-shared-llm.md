<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to update-shared-llm.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

## {{PROJECT_NAME}} — repo-specific vetting and commit notes

This overlay records what is specific to THIS repo's update-shared-llm workflow —
the generic steps live in the common `workflow.md` above.

<!-- TODO(project): fill in the three items below. Everything here is PRIVATE (this_repo/)
     and never syncs to the public kit, so concrete names/paths/tooling are fine. -->

- **Pre-push vetting gate.** FILL THIS OUT — the exact check this repo runs before
  a change reaches the public kit (e.g. a proprietary-content scan script, a
  PreToolUse commit hook, a required independent reviewer). Name the command.

- **Strings that must NEVER reach a `common/` layer or the public kit.** FILL THIS
  OUT — the concrete private repo/product/codename, infra and host names, account
  IDs, and internal paths this repo must keep out of anything public.

- **Commit conventions.** FILL THIS OUT — commit-message format and which paths to
  stage for a private-repo change vs a public-kit change.
