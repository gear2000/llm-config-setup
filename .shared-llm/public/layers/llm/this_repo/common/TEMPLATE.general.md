<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to general.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# {{PROJECT_NAME}}

<!-- TODO(project): Replace {{PROJECT_NAME}} with your project's name. Add a one-line description of what this repo contains and who it is for. -->

Source code only. CI, docs, and ops live in a sibling repo via the `ops/` symlink. Essentials stay inline. Do not hunt through docs for them.

## Coding conventions

Read a file before you edit it. Before you produce a data structure, read the Pydantic model that defines it. Do not guess a shape.

Fail loud. Catch only the exception you can handle. Keep the `try` to the line that can raise. Do not use a bare `except:`. Do not swallow with `except Exception`. Do not `pass` on an exception. Do not fake a default to limp onward.

Build the real thing. Do not mock, stub, or degrade to pass a test. Do not hand-create infra (DB table, IAM role, S3 bucket) to go green. If it is missing, the automation is broken. Report the gap. Greenfield: no compatibility shims. There is no `dry_run` mode. Strip it on sight.

## Package architecture

Maintainable code is a hierarchy. Imports flow down only. Do not import upward.

Repo:

```
higher services
└── services
    └── higher-level packages
        └── lower-level packages
```

Packages sit at the bottom. A higher-level package is built on lower-level packages. A service is built on packages. A higher service is built on services and packages.

Inside one package:

```
Layer 4  entry points   main or lambda. Wire only.
Layer 3  application    orchestrates 0-2
Layer 2  domain         rules and models. No I/O.
Layer 1  adapters       one module per external system
Layer 0  primitives     types, constants, utilities. No external deps.
```

Same direction. Layer 4 sits on 3, on 2, on 1, on 0.

- Universal (0-1): stateless primitives. Do not import from a higher layer.
- High-context (2-3): environment-specific. Do not know user-facing product workflows.
- Service-contextual: one service only. Go `internal/`. Python `_internal/`. Do not publish it.

A deep module hides internals behind a narrow seam: the public interface. Test that module. Test how other code talks to it through that seam. Do not test the whole tree as one blob. Do not add a wrapper that only re-exports another library.

Before you create a package or a service, stop and ask.

Place logic at the lowest cohesive layer. Ask only if two or more services would share it.

If helpers pile up in an entry point, ask whether to add an internal module.

## Running CI/CD

The Taskfile is the entry point. Prefer `task <target>` over a raw CLI.

1. `task <target>`
2. No target? Check {{CI_DEPLOY_TOOL}} jobs.
3. Not there? Ask, or add a `task` target in the current convention.

{{CI_BUILD_TOOL}} runs on push: lint, unit tests, package publish.

<!-- TODO(project): Document any known intermittent CI step failures and how to distinguish them from real failures. Replace {{CI_BUILD_TOOL}} and {{CI_DEPLOY_TOOL}} with your actual tool names. -->

Tests and builds run in Docker (`Dockerfile.test`, `Dockerfile.e2e`). Do not run `python` / `pytest` / `npm` / `node` bare in CI. Deploys go through {{CI_DEPLOY_TOOL}} / task. Do not run infra tools by hand.

Local gate, through `task`:

- `task lint:fast`
- `task lint:fix`
- `task lint:full`
- `task lint:types`

<!-- TODO(project): Replace the lint task names if your project uses different targets. Add any project-specific quality-gate steps. -->

Loop: `lint:fast`, fix, push, watch {{CI_BUILD_TOOL}}. If a check fails, fix the code. Do not lower lint. Do not skip a CI stage. Do not suppress to go green.

## Credentials

<!-- TODO(project): Document your project's credentials here. Replace {{CRED_ROOT}} with the path to your credentials directory. One bullet per credential. Never commit real values. -->

Tokens live under `{{CRED_ROOT}}` (gitignored). Source the env file. Do not hard-code tokens. Cloud region: `{{CLOUD_REGION}}`.

- **{{CI_BUILD_TOOL}}** (`<TOKEN_ENV_VAR>`) — `{{CRED_ROOT}}/<tool>/exports.env`
- **Package registry / Docker registry** (`<REGISTRY_TOKEN_ENV_VAR>`) — `{{CRED_ROOT}}/<registry>/exports.env`
- **{{CI_DEPLOY_TOOL}}** (`<DEPLOY_TOKEN_ENV_VAR>`, `<DEPLOY_URL_ENV_VAR>`) — `{{CRED_ROOT}}/<tool>/trigger.env`
- **Cloud account, SaaS hub** (account `{{ACCOUNT_SAAS}}`) — `{{CRED_ROOT}}/cloud/saas/exports.env`
- **Cloud account, target tenant** (account `{{ACCOUNT_TENANT}}`) — `{{CRED_ROOT}}/cloud/tenant/exports.env`
- **Cloud test user** (for E2E tests) — `{{CRED_ROOT}}/cloud/test-user/`

<!-- TODO(project): Add or remove credential entries as needed. Keep descriptions short: name, env var, path. -->

## Key paths

- **`src/packages/`** — Python libraries published to your package registry.
- **`src/services/`** — deployable services (Lambda, containers, or binaries).
- **`src/authoring/`** — IaC templates or configuration assets (delete if unused).
- **`.original/`** — legacy read-only reference (delete if unused).
- **`ops/`** — symlink to `{{OPS_REPO}}` (CI pipelines, docs, ops scripts). Gitignored. Run `tools/setup-symlinks.sh` after a fresh clone.
- **`infra/`** — symlink to `{{INFRA_REPO}}` (standalone infra). Gitignored. Same setup.

<!-- TODO(project): Replace {{OPS_REPO}} and {{INFRA_REPO}} with your sibling repo names, or delete those bullets if you have a single-repo layout. -->

## Docs

Docs drift. Use them to navigate. Confirm in the source. If a wrong guess would hurt, ask.
