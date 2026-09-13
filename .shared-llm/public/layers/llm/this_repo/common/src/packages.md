<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to packages.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# src/packages/

Python packages for {{PROJECT_NAME}}. One package per directory.

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

## Package hierarchy

<!-- TODO(project): Document your package tier ladder. Example:

```
Tier 0 (base):        {{PACKAGE_PREFIX}}_commons
Tier 1 (foundation):  {{PACKAGE_PREFIX}}_auth, {{PACKAGE_PREFIX}}_db
Tier 2 (platform):    {{PACKAGE_PREFIX}}_api
```

Replace {{PACKAGE_PREFIX}}. List all packages by dependency tier. Tier 0 has no internal deps.
-->

## This directory

- Directory name maps to the registry repo: `{{PACKAGE_PREFIX}}_<name>` → `{{PACKAGE_PREFIX}}-<name>`.
- Tests run through `Dockerfile.test`. Do not run bare pytest.
- `__init__.py` is the contract. Libraries: explicit `__all__`. Services: `__all__ = []`.
- New packages are `{{PACKAGE_PREFIX}}_*`. Every package has `pyproject.toml`.
- {{CI_BUILD_TOOL}} runs unit tests on every registry push.

<!-- TODO(project): Replace {{PACKAGE_PREFIX}} and {{CI_BUILD_TOOL}}. -->

## Python

- Python 3.14. `list[str]`, `str | None`, `match`.
- Type-annotate every function signature.
- Pydantic for data models. No ORM. psycopg3 for Postgres. boto3 for AWS.
- pytest: `tests/unit/`, `tests/integration/`.
- Run `ruff check` before you deliver.

## Errors

Do not catch by default. Fail loud. Do not use `except Exception` or bare `except:`. Wrap only the expression that can fail. Add a catch only after a real failure.

## PyPI

| Context | URL |
|---------|-----|
| In-cluster / CI | `{{PYPI_INDEX_URL}}` |
| Authenticated | `{{PYPI_INDEX_URL_AUTH}}` |

<!-- TODO(project): Replace {{PYPI_INDEX_URL}} and {{PYPI_INDEX_URL_AUTH}}. -->

## CI/CD

- **{{CI_BUILD_TOOL}}** — build, unit tests, lint. On every push.
- **{{CI_DEPLOY_TOOL}}** — deploy, integration, E2E.
- Tests through Docker: `Dockerfile.test`, `Dockerfile.e2e` (services only).
- Do not run `python` / `pytest` / `npm` bare in CI.

## Gotchas

<!-- TODO(project): Naming exceptions, legacy spellings, packages that bypass conventions. -->
