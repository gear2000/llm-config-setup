# Python Conventions

## Version & Syntax

**Python 3.14** — use modern syntax: `list[str]`, `str | None`, `match` statements. No `Optional[X]` — use `X | None`.

## Code Rules

- Type-annotate all function signatures
- Pydantic for data models and validation
- No ORM — use psycopg3 for Postgres, boto3 for AWS
- Pin major versions in dependencies: `pydantic>=2.0,<3.0`

## Structure

- `models/` — Pydantic models
- `services/` — business logic
- `util/` — shared helpers
- `tests/unit/` and `tests/integration/` — always separate

## Package Design — Deep Modules

Aim for **deep modules**: small public interface, lots of hidden implementation. The cost of a module is its interface; the value is what it hides (Ousterhout, *A Philosophy of Software Design*).

- **`__init__.py` is the public surface.** Library packages MUST declare `__all__` with explicit exports. Service packages MUST declare `__all__ = []` and a comment "service, not a library — invoked as <handler|mount|CLI>".
- **Hide internals.** Internal modules use `_` prefix or live in `_internal/`. Don't re-export internal exception types, adapter classes, or backend-specific implementations through the top-level `__init__.py`. If a caller has to know which backend you're using, the abstraction leaked.
- **Design the interface first.** Before writing implementation files, sketch what `__init__.py` will export. The smallest interface that lets callers do their work is the right one. If you can't sketch a 5-line interface, you don't yet understand the problem well enough to design a module.
- **Module size guideline.** Modules over ~300 LOC are a smell. Either split (extract a sub-concern into a sibling module) or deepen (move complex logic *inside* the existing module behind a narrower interface). A 600-line module with one public class is fine; a 600-line module with twenty public functions is not.

## Error Handling — Fail Loud

Default: do NOT catch errors. Let them break loud.

Two hard rules — never violate:

1. **Never use a broad catch.** No `except Exception`, no bare `except:`, no `catch (e)` without narrowing. Catch a named exception or don't catch.
2. **Never wrap big blocks in try/except.** The try wraps the smallest expression that can raise — usually one line. A 20-line try block hides which operation actually failed.

In new code especially, do not anticipate exceptions. Add a try/except only AFTER you have actually encountered the failure and have a concrete recovery action. Anticipation leads to swallowing — the broad catch hides the real bug, the test passes, corruption ships.

Catching to `pass` / `return None` / log-and-continue is forbidden.

```python
# Wrong — anticipatory catch, swallows whatever happens
try:
    user = get_user(uid)
except Exception:
    user = None

# Right — let it raise. Add a catch only when a real failure surfaces
# and you have a concrete recovery.
user = get_user(uid)
```

## Testing

- pytest for all tests
- Run tests twice before declaring green (catches flakiness)
- Unit tests: no external services, mock at the boundary
- Integration tests: real services, isolated fixtures

## Linting

Run `ruff check` before delivering code. Fix all issues — don't suppress warnings without a clear reason.

## Hierarchical package architecture

Python packages layer the same way Go packages do: lower-layer packages are independent
libraries; services sit on top of them. Imports point one direction — a package must not reach
upward into a service or sideways into a sibling it should not know about.

```
Layer 0 — primitives:   shared types, constants, utilities with no external deps
Layer 1 — adapters:     one module per external system (queue, DB, HTTP client, cache)
Layer 2 — domain:       domain logic with no I/O (models, rules, computation)
Layer 3 — application:  orchestrate layers 0-2 (handlers, use cases, roles)
Layer 4 — entry points: service handler / FastAPI app — wire everything, minimal public surface
```

Rules:
- A layer may only import from lower layers.
- `__init__.py` is the contract. Libraries: explicit `__all__`. Services: `__all__ = []`.
- Internal modules use `_` prefix or live in `_internal/`. Never re-export internals through `__init__.py`.

## Interface thickness

A package's public interface should hide complexity, not expose it.

Ask: would a caller need to understand the internals to use this correctly? If yes, push
complexity inward — the interface is too shallow.

Supporting smell: ~10+ exported names in `__all__` is a prompt to review whether the interface
is too wide, not an automatic reject.

## Placement decisions — LLM guidance

**DECISION POINT: business_logic_placement**

Trigger: you are about to place domain or business logic in a package.

- Default: lowest cohesive layer. Do not ask.
- Ask only when two or more services would share the same logic.
- Question: "Services A and B both need this — should I create a shared package, or duplicate it?"

Repo-level files override this default. Check `layers/skills/this_repo/python.md` if present.

---

**DECISION POINT: service_contextual_package**

Trigger: a Python service is growing large and helper logic is piling up in handler files.

Ask: "This service is getting large. Should I create a local `_internal/` module to organise
helpers, or keep it flat?"
