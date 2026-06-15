<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to packages.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# src/packages/

Python packages for the {{PROJECT_NAME}} platform. One package per directory.

## Package hierarchy

<!-- TODO(project): Document your package tier ladder here. Example shape:

```
Tier 0 (base):        {{PACKAGE_PREFIX}}_commons
Tier 1 (foundation):  {{PACKAGE_PREFIX}}_auth, {{PACKAGE_PREFIX}}_db
Tier 2 (platform):    {{PACKAGE_PREFIX}}_api
```

Replace {{PACKAGE_PREFIX}} with your project's naming prefix (e.g. myapp). List all packages, grouped by dependency tier (Tier 0 = no internal deps; higher tiers build on lower ones).
-->

## This directory specifically

- One repo per package in your registry. Directory name maps to the registry repo: `{{PACKAGE_PREFIX}}_<name>` → `{{PACKAGE_PREFIX}}-<name>`. Sync via your sync script.
- `Dockerfile.test` is the test entry point for every package. Tests always run through Docker, never bare pytest.
- `__init__.py` is the contract. Libraries: explicit `__all__`. Services: `__all__ = []`.
- All new packages are `{{PACKAGE_PREFIX}}_*` prefixed. All packages have `pyproject.toml`.
- {{CI_BUILD_TOOL}} runs unit tests on every registry push.

<!-- TODO(project): Replace {{PACKAGE_PREFIX}} with your package naming prefix (e.g. myapp). Replace {{CI_BUILD_TOOL}} with your CI system. -->

## Python conventions

- Python 3.14. Use modern syntax: `list[str]`, `str | None`, `match`.
- Type-annotate all function signatures.
- Pydantic for data models. No ORM — psycopg3 for Postgres, boto3 for AWS.
- pytest for all tests (unit in `tests/unit/`, integration in `tests/integration/`).
- Run `ruff check` before delivering code.
- Deep modules: keep the public interface small; hide implementation detail.

## Error handling

- Default: do NOT catch errors. Let them break loud.
- No broad catches: `except Exception` and bare `except:` are forbidden.
- Never wrap large blocks in try/except — wrap the smallest expression that can actually fail.
- No anticipatory catches — add try/except only after encountering a real failure.

## PyPI

**Published to your internal registry** (all packages):

| Context | PyPI URL |
|---------|----------|
| In-cluster / CI | `{{PYPI_INDEX_URL}}` |
| Authenticated | `{{PYPI_INDEX_URL_AUTH}}` |

<!-- TODO(project): Replace {{PYPI_INDEX_URL}} with your unauthenticated in-cluster PyPI URL and {{PYPI_INDEX_URL_AUTH}} with the authenticated form. Replace {{PYPI_HOST}} with the registry hostname (used in --trusted-host). -->

## CI/CD

- **{{CI_BUILD_TOOL}}** — build, unit tests, linting. Triggered on every push.
- **{{CI_DEPLOY_TOOL}}** — deploy, integration tests, E2E tests (prefer headed over headless).
- Tests run through Docker: `Dockerfile.test` for unit + integration tests, `Dockerfile.e2e` for end-to-end (services only).
- No bare runtime in CI — never `python`/`pytest`/`npm` directly in CI steps. Always through Dockerfile.

## Gotchas

<!-- TODO(project): Document project-specific gotchas here — naming exceptions, legacy spellings that must be preserved, packages that bypass normal conventions for historical reasons, etc. -->
