<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to golang.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# {{PROJECT_NAME}} — Go Conventions

Project-specific Go conventions. This layer sits on top of the general Go
conventions (`practices.md`); it records where THIS repo places business logic,
its canonical service to imitate, and its Go package inventory.

## Business logic placement

Business logic lives in lower-level packages by default — keep domain logic close
to the package that owns it.

Only ask the user when the same logic would be shared across two or more services.
In that case, ask: "Services A and B both need this — should I create a shared
package, or duplicate it?"

<!-- TODO(project): adjust the default above if your repo prefers a different placement rule. -->

## Reference service

<!-- TODO(project): name your canonical Go service and describe its internal layout so an
     agent has one worked example to imitate. Delete this whole section if you have no Go
     service yet. -->

`{{GO_REFERENCE_SERVICE}}` is the canonical Go service in this repo. Follow its
internal layout for new services:

```
cmd/<name>/main.go       — entry point(s): wire dependencies, expose the minimal public surface
internal/<adapter>/      — one sub-package per external system (queue, DB, HTTP client, cache)
internal/<domain>/       — pure domain logic, no I/O (models, rules, computation)
internal/<application>/  — orchestrate adapters + domain (roles, handlers, use cases)
```

Everything implementation-level is unexported or inside `internal/`. The entry
points wire the roles together — nothing else is public.

## Go packages in this repo

<!-- TODO(project): list your Go packages and services, one line each: path — what it is.
     Delete the placeholder rows once filled. -->

- `<path/to/library>` — FILL THIS OUT: a published Go library with a minimal public interface.
- `<path/to/service>` — FILL THIS OUT: a Go service binary; all logic under `internal/`.
- `<path/to/tooling>` — FILL THIS OUT: standalone Go tooling / helper binaries.

## New Go services

Follow the reference service: `cmd/<name>/` for entry points, all implementation
in `internal/<role>/`. No business logic at the binary root — wire only.
