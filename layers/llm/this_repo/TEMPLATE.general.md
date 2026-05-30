<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to general.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# {{PROJECT_NAME}}

<!-- TODO(project): Replace {{PROJECT_NAME}} with your project's name. Add a one-line description of what this repo contains and who it is for. -->

Source code only — CI configs, docs, and ops tools live in a sibling repo (reached via the `ops/` symlink) so worktrees stay light. The essentials below are inline on purpose: don't go hunting through docs for them.

## Coding conventions

**Read before write.** Read a file before editing it. Before producing any data structure, read the Pydantic model that defines it — models are the contract and live co-located in each package. Never guess a shape; read the schema.

**Respect package layering.** `src/packages/` are independent libraries. Imports point one way — a package must not reach "upward" into the app, or sideways into a sibling it shouldn't know about (the upward-import check enforces this).

**Build deep modules.** Favour a small, narrow interface over a large hidden implementation. No shallow pass-through wrappers, no leaking a module's internals across a package boundary. If you're threading the same detail through three layers, the boundary is in the wrong place.

**Fail loud; exceptions stay short and specific.** Catch only the specific exception you can actually handle, and keep the `try` body to the line(s) that can raise — let everything else propagate. No bare `except:`, no `except Exception` swallow, no `except … : pass`, no fake default to limp onward. A silent failure becomes a downstream mystery; a loud one gets fixed.

**No shortcuts that create downstream debt.** No mocks, stubs, or "graceful degradation" to pass a test — build the real thing or fail. Never hand-create a resource (DB table, IAM role, S3 bucket, any infra) to go green; if it's missing, the automation is broken — fix that and report the gap. Greenfield: move forward, no backwards-compat shims. There is no `dry_run` mode anywhere — strip it on sight.

## Running CI/CD

The **Taskfile is the central entry point for all automation** — building, deploying, and running integration, acceptance, and E2E tests. Use it first:

1. **`task <target>`** — the one place for build/deploy/test automation; always prefer it over a raw CLI command.
2. **No target for what you need?** Check the **{{CI_DEPLOY_TOOL}}** jobs — thin triggers that ultimately call task targets for live-infra flows.
3. **Not there either?** Ask the user, or add a new `task` target in the current convention.

**{{CI_BUILD_TOOL}}** is push-triggered, so it always runs the build-time checks — lint, unit tests, and (for packages) package publish — on every push.

<!-- TODO(project): Document any known intermittent CI step failures here (e.g. registry push timeouts, layer-cache blips) and how to distinguish them from real failures. Replace {{CI_BUILD_TOOL}} and {{CI_DEPLOY_TOOL}} with your actual tool names. -->

Every test and build runs in Docker — `Dockerfile.test` (unit + integration) and `Dockerfile.e2e` (services only); never `python`/`pytest`/`npm`/`node` bare in a CI step. Deploys run only through the {{CI_DEPLOY_TOOL}}/task path — never infra tools (e.g. `terraform apply`) by hand.

**Local quality gate — before you push, through `task`, never the tools bare:**

- `task lint:fast` — fast native linter. Run before every commit.
- `task lint:fix` — auto-fix safe issues.
- `task lint:full` — full Docker lint matching the CI image.
- `task lint:types` — type-checking on type-annotated packages.

<!-- TODO(project): Replace the lint task names above if your project uses different targets (e.g. task check, task typecheck). Add any project-specific quality-gate steps. -->

Loop: `lint:fast` → fix → push → watch {{CI_BUILD_TOOL}} → on failure, read the step logs, fix the **code**, push again. If a check fails, fix the code — never lower lint strictness, skip a CI stage, or suppress to go green.

## Credentials

<!-- TODO(project): Document your project's credentials here. Replace {{CRED_ROOT}} with the path to your credentials directory (e.g. ~/project/secrets/ or ~/creds/). Use the shape below — one bullet per credential. Never commit real values. -->

All tokens live under `{{CRED_ROOT}}` (gitignored) — source the relevant env file; never hard-code or paste tokens. Cloud region: `{{CLOUD_REGION}}`.

- **{{CI_BUILD_TOOL}}** (`<TOKEN_ENV_VAR>`) — `{{CRED_ROOT}}/<tool>/exports.env`
- **Package registry / Docker registry** (`<REGISTRY_TOKEN_ENV_VAR>`) — `{{CRED_ROOT}}/<registry>/exports.env`
- **{{CI_DEPLOY_TOOL}}** (`<DEPLOY_TOKEN_ENV_VAR>`, `<DEPLOY_URL_ENV_VAR>`) — `{{CRED_ROOT}}/<tool>/trigger.env`
- **Cloud account — SaaS hub** (account `{{ACCOUNT_SAAS}}`) — `{{CRED_ROOT}}/cloud/saas/exports.env`
- **Cloud account — target tenant** (account `{{ACCOUNT_TENANT}}`) — `{{CRED_ROOT}}/cloud/tenant/exports.env`
- **Cloud test user** (for E2E tests) — `{{CRED_ROOT}}/cloud/test-user/`

<!-- TODO(project): Add or remove credential entries as needed. Keep descriptions short: name → env var → path. -->

## Key paths

- **`src/packages/`** — Python libraries published to your package registry.
- **`src/services/`** — deployable services (Lambda, containers, or binaries).
- **`src/authoring/`** — IaC templates or configuration assets (delete if unused).
- **`.original/`** — legacy read-only reference (delete if unused).
- **`ops/`** — symlink to `{{OPS_REPO}}` (CI pipelines, docs, ops scripts). Gitignored; run `tools/setup-symlinks.sh` after a fresh clone.
- **`infra/`** — symlink to `{{INFRA_REPO}}` (standalone infra). Gitignored; same setup.

<!-- TODO(project): Replace {{OPS_REPO}} and {{INFRA_REPO}} with your sibling repo names, or delete those bullets if you have a single-repo layout. -->

## Design docs are a starting point, not authoritative

Docs centralized in your docs tool (e.g. mkdocs under the `ops/` symlink). Use them as a strong starting point for understanding a flow and as a map into the code — **not** as gospel. They drift. Lean on the code as the source of truth — read the doc to grasp intent and navigate, then confirm in the source. When it's genuinely unclear and a wrong guess could cause downstream problems, stop and ask the human rather than assume.
