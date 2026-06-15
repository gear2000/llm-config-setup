<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to python.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# {{PROJECT_NAME}} — Python Conventions

## Package Structure

Packages live at `src/packages/{package_name}/`.

```
src/packages/{{PACKAGE_PREFIX}}_{name}/
├── pyproject.toml               # Build metadata, pinned major deps; distribution name = {{PACKAGE_PREFIX}}-{name}
├── Dockerfile                   # Production build image
├── Dockerfile.test              # Python 3.14 slim, test deps, pytest, ruff
├── ruff.toml                    # Shared lint config (copied into test image)
├── {{PACKAGE_PREFIX}}_{name}/
│   ├── __init__.py              # Version and public API exports
│   └── ...                      # Flat modules or subdirs as needed
└── tests/
    ├── conftest.py
    ├── unit/
    └── integration/
```

<!-- TODO(project): Replace {{PACKAGE_PREFIX}} with your project's naming prefix (e.g. myapp). Update the structure above if your package layout differs. -->

- **All new packages are `{{PACKAGE_PREFIX}}-` prefixed:** directory `{{PACKAGE_PREFIX}}_<name>` (snake_case), distribution name `{{PACKAGE_PREFIX}}-<name>` (hyphen) in pyproject.toml, registry repo `{{PACKAGE_PREFIX}}-<name>`.
- Directory name and import name must match (both snake_case).
- Use `pyproject.toml` for new packages.

## Service Structure

Python services live at `src/services/{service_name}/`.

```
src/services/{service_name}/
├── pyproject.toml
├── Dockerfile
├── {service_name}/
│   ├── __init__.py
│   └── ...
└── tests/
    ├── conftest.py
    ├── unit/
    └── integration/
```

## Library vs Service

State which at the top of the package's CLAUDE.md and at the top of `__init__.py`.

- **Library** — imported by other packages. Has a curated public API. `__all__` required with explicit exports.
- **Service** — invoked as Lambda handler, FastAPI mount, or CLI entry. Not imported as a library. `__all__ = []` with comment "service, not a library — invoked as <handler|mount|CLI>".

## PyPI Policy

**Published to your internal registry** (all library packages):

| Context | PyPI URL |
|---------|----------|
| In-cluster / CI | `{{PYPI_INDEX_URL}}` |
| Authenticated | `{{PYPI_INDEX_URL_AUTH}}` |

<!-- TODO(project): Replace {{PYPI_INDEX_URL}} and {{PYPI_INDEX_URL_AUTH}} with your registry URLs. -->

**Not published** (deployed as containers): all services under `src/services/`

Anti-patterns:
- No `COPY sibling_package/` in Dockerfiles — install from your registry.
- No `@ file:///...` path references.
- Always pin: `{{PACKAGE_PREFIX}}_commons>=0.1.0,<1.0`

## Testing Conventions

### Three Dockerfiles

Every package/service uses Docker containers for testing. Never a Makefile.

- `Dockerfile` — build/deployment image
- `Dockerfile.test` — unit + integration tests (packages and services)
- `Dockerfile.e2e` — E2E tests against live infrastructure (services only)

### Dockerfile.test Template

<!-- TODO(project): Adapt the template below for your project. Replace {{PACKAGE_PREFIX}} with your prefix and {{PYPI_HOST}} with your registry hostname. Delete this block if you use a different testing approach. -->

```dockerfile
FROM python:3.14-slim
WORKDIR /app

ARG REGISTRY_HOST={{PYPI_HOST}}
ARG REGISTRY_USER=admin
ARG REGISTRY_TOKEN=

COPY pyproject.toml README.md ./
COPY {{PACKAGE_PREFIX}}_{name}/ ./{{PACKAGE_PREFIX}}_{name}/
COPY tests/ ./tests/

# Lint gate — fails the build on lint errors
COPY ruff.toml ./
RUN pip install --no-cache-dir ruff mypy && ruff check .

# Install package + test deps via internal registry (token-authenticated when available)
RUN if [ -n "${REGISTRY_TOKEN}" ]; then \
        INDEX_URL="http://__token__:${REGISTRY_TOKEN}@${REGISTRY_HOST}/pypi/simple/"; \
    else \
        INDEX_URL="http://${REGISTRY_HOST}/pypi/simple/"; \
    fi && \
    pip install --no-cache-dir \
        --extra-index-url "${INDEX_URL}" \
        --trusted-host "${REGISTRY_HOST%:*}" \
        -e ".[test]"

ENTRYPOINT ["python", "-m", "pytest"]
CMD ["tests/unit/", "-v", "--tb=short"]
```

### Running Tests

```bash
# Via Taskfile (preferred):
{{TEST_TASK}}

# Via Docker directly:
docker build --network=host -f Dockerfile.test -t <name>-test:local .
docker run --rm <name>-test:local
```

<!-- TODO(project): Replace {{TEST_TASK}} with your Taskfile target (e.g. task pkg:<name>:test:image). -->

### Rules

- **No bare runtime in CI.** Never `npm`/`node`/`python`/`pytest` directly in CI steps. Always through `Dockerfile.*`.
- **No mocks in integration/E2E tests.** Build real implementations, fail hard on missing deps. The only acceptable mock location is unit tests, mocking at the boundary.
- **Run tests twice** to detect flakiness before declaring green.

## Consolidation

Utility functions used by 2+ packages live in `{{SHARED_UTIL_PACKAGE}}` (encoding, hashing, env-var parsing, JWT, caching, retry helpers).

<!-- TODO(project): Replace {{SHARED_UTIL_PACKAGE}} with your shared utilities package name (e.g. myapp_commons). -->

- Grep `{{SHARED_UTIL_PACKAGE}}` before writing a new utility.
- If the helper exists, import it. If it doesn't but feels reusable, add it to `{{SHARED_UTIL_PACKAGE}}` during the work — not after.
- Proactive consolidation during work, not deferred cleanup.

## Execution Loop

1. Read the package's CLAUDE.md (packages) or the service's CLAUDE.md (services)
2. **Sketch the public surface** — write down what `__init__.py` will export; revise until it's the smallest interface that does the job
3. **Consolidation pass** — grep `{{SHARED_UTIL_PACKAGE}}` for any utilities you're about to write; reuse or extend, don't duplicate
4. Write implementation
5. Write tests
6. Run tests via Docker (`{{TEST_TASK}}`)
7. Fix failures, re-run
8. **Run tests a second time** to detect flakiness
9. Run `task lint:fast` (ruff), fix lint issues
10. All green → deliver

Maximum 10 iterations. If still failing, report exact errors.
