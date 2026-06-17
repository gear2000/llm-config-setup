# Go Conventions

## Core idioms

- Use structs + methods, not classes. There is no inheritance — use composition and embedding.
- Pointer receiver `*T` when the method mutates state. Value receiver `T` when it only reads.
- Interfaces are implicit. Satisfy one by having the right methods; never declare `implements`.
- The last return value is `error` when a function can fail. Always check `err != nil` — never discard it with `_`.
- Unexported identifiers (lowercase) are the default. Export only what callers outside the package need.
- `nil` is the zero value for pointers, interfaces, maps, slices, channels, and functions.
- `_` discards a value intentionally. `ok` is the boolean success signal. `err` is the error value.
- Prefer dependency injection over package-level globals. Mutable globals are a concurrency hazard.

## Fail loud

- Return `error` up the call stack. Wrap with context: `fmt.Errorf("loading order %s: %w", id, err)`.
- Never swallow errors with a blank `_` or a silent `if err != nil { return }` that drops context.
- Panic only for programmer errors (invariant violations that should never occur at runtime).

## Package layering

Go packages layer the same way Python packages do: lower-layer packages are independent
libraries; services sit on top of them. Imports point one direction — a package must not reach
upward into a service or sideways into a sibling it shouldn't know about.

```
Layer 0 (primitives):   shared types, constants, small utilities with no external deps
Layer 1 (adapters):     each wraps one external system (queue, cache, HTTP client, DB)
Layer 2 (domain):       business logic with no I/O (models, rules, computation)
Layer 3 (application):  orchestrate layers 0-2 (use cases, handlers, roles)
Layer 4 (entry points): cmd/ or server root — wire everything, expose minimal public surface
```

Rules:
- A layer may only import from lower layers.
- No circular imports. The compiler enforces this; don't fight it.
- `internal/` packages are the right home for everything that is not the public entry point.

## Deep modules

A deep module has a narrow public interface and a rich hidden implementation. An LLM (or a
human) reading the codebase should understand what a package does from its exported symbols
without reading every file inside it.

- Keep exported types, functions, and methods to the minimum a caller needs.
- Move implementation depth into `internal/` sub-packages or unexported symbols within the package.
- No shallow pass-through wrappers. If a file only re-exports another package's symbols, the boundary is in the wrong place.
- `internal/` is enforced by the Go toolchain — code outside the module root cannot import it. Use it aggressively.

**`internal/` is the public surface analog of Python's `__all__`.** Everything unexported or inside `internal/` is hidden. The exported symbols at the package root are the contract.

Example layout for a service binary:
```
cmd/myservice/main.go        — entry point, wires deps, starts server
internal/adapters/           — one sub-package per external system
internal/domain/             — pure business logic, no I/O
internal/handlers/           — application layer, calls domain + adapters
```

## Testing

- Test files live alongside the code they test (`foo_test.go` next to `foo.go`).
- Use the `_test` package suffix for black-box tests that exercise the exported interface only.
- Integration tests go in `_integration_test.go` files, gated with a build tag or env check.
- Prefer table-driven tests for functions with multiple input/output cases.
- Interfaces make DI easy — pass a mock that satisfies the interface in tests; never reach into `internal/` from a test of an outer package.

## Concurrency

- Prefer channels for communication between goroutines; prefer mutexes for protecting shared state.
- Always pass `context.Context` as the first argument of any function that can block or be cancelled.
- A goroutine that can panic must recover and surface the error — a bare `go func()` that panics will crash the whole process.
