<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to services.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# src/services/

Deployable services. Every directory here ships: cloud functions, containers, or binaries.

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

## Python

- Python 3.14. Type-annotate every function signature.
- Pydantic for data models. No ORM. psycopg3 for Postgres. boto3 for AWS.
- pytest. Run `ruff check` before you deliver.
- `pyproject.toml`, not `setup.py`.
- `__all__ = []` in `__init__.py`, with a comment for the invocation type (Lambda handler / FastAPI mount / CLI).
- Services are not published to the package registry. They ship as images or binaries.
- Tests through Docker: `Dockerfile.test`, `Dockerfile.e2e`.

## Errors

Do not catch by default. Fail loud. Do not use `except Exception` or bare `except:`. Wrap only the expression that can fail.

## PyPI

Internal packages from your registry:

| Context | URL |
|---------|-----|
| In-cluster / CI | `{{PYPI_INDEX_URL}}` |
| Authenticated | `{{PYPI_INDEX_URL_AUTH}}` |

<!-- TODO(project): Same values as src/packages.md. -->

## CI/CD

- **{{CI_BUILD_TOOL}}** — build, unit tests, lint. On every push.
- **{{CI_DEPLOY_TOOL}}** — deploy, integration, E2E.
- Tests through Docker. Do not run `python` / `pytest` / `npm` bare in CI.

## Service catalog

<!-- TODO(project): List every service. Path and one line each.

### Frontend
- **`frontend/`** — Next.js app.

### Cloud functions
- **`aws/{{PACKAGE_PREFIX}}-api/`** — Entry point.
- **`aws/{{PACKAGE_PREFIX}}-worker/`** — Background processing.

### CLI
- **`cli/{{PACKAGE_PREFIX}}-admin/`** — Admin tasks.
-->

## Deploy gate

<!-- TODO(project): How deploys are triggered. Example:

{{CI_BUILD_TOOL}} runs unit tests on push. It does not deploy.

```
{{DEPLOY_SCRIPT}} <name>
{{DEPLOY_SCRIPT}} check
```
-->

## Gotchas

<!-- TODO(project): Directory vs cloud name, env vars, deploy order. -->
