<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to services.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# src/services/

Deployable services. Every directory here ships somewhere — cloud functions, containers, or binaries.

## Python conventions

- Python 3.14, modern syntax, type-annotate all function signatures.
- Pydantic for data models. No ORM — psycopg3 for Postgres, boto3 for AWS.
- pytest for all tests. Run `ruff check` before delivering code.
- Services use `pyproject.toml`, not `setup.py`.
- Services declare `__all__ = []` in `__init__.py` with a comment stating invocation type (e.g. Lambda handler / FastAPI mount / CLI).
- Services are NOT published to the package registry. They ship as Docker images or built binaries.
- Tests run through Docker: `Dockerfile.test` for unit tests, `Dockerfile.e2e` for end-to-end.

## Error handling

- Default: do NOT catch errors.
- No broad catches (`except Exception`, bare `except:`).
- Never wrap large blocks in try/except.
- No anticipatory catches — let failures surface immediately.

## PyPI

Internal packages installed from your registry:

| Context | PyPI URL |
|---------|----------|
| In-cluster / CI | `{{PYPI_INDEX_URL}}` |
| Authenticated | `{{PYPI_INDEX_URL_AUTH}}` |

<!-- TODO(project): Replace {{PYPI_INDEX_URL}} and {{PYPI_INDEX_URL_AUTH}} with your registry URLs (same values as in src/packages.md). -->

## CI/CD

- **{{CI_BUILD_TOOL}}** — build, unit tests, linting. Triggered on every push.
- **{{CI_DEPLOY_TOOL}}** — deploy, integration tests, E2E tests (prefer headed over headless).
- Tests run through Docker: `Dockerfile.test` for unit + integration tests, `Dockerfile.e2e` for end-to-end.
- No bare runtime in CI — never `python`/`pytest`/`npm` directly in CI steps. Always through Dockerfile.

## Service catalog

<!-- TODO(project): List all services here. Group by type (frontend, cloud functions, CLI tools, etc.). For each service, give the directory path and a one-line description.

Example shape:
### Frontend
- **`frontend/`** — Next.js app, auth + database. Runs on dev:3001.

### Cloud Functions (Python, FastAPI)
- **`aws/{{PACKAGE_PREFIX}}-api/`** — Entry point for <action>.
- **`aws/{{PACKAGE_PREFIX}}-worker/`** — Background processing Lambda.

### CLI
- **`cli/{{PACKAGE_PREFIX}}-admin/`** — Admin tasks.
-->

## Deploy gate

<!-- TODO(project): Describe how deploys are triggered. Example:

{{CI_BUILD_TOOL}} runs unit tests on push. **It does not deploy to cloud.** Deploys are triggered via:

```
{{DEPLOY_SCRIPT}} <name>     # build + push + update one service
{{DEPLOY_SCRIPT}} check      # compare source_rev tag vs git HEAD; deploy drifted ones
```

Replace {{DEPLOY_SCRIPT}} with your deploy helper script path (e.g. tools/deploy.sh).
-->

## Gotchas

<!-- TODO(project): Document project-specific service gotchas — naming differences between directory and cloud function name, services that shell out to external tools, interop docs for cross-service flows, etc. -->
