# Backend Code Conventions

General conventions for backend services and APIs, independent of language,
framework, or cloud. They cover how to shape an endpoint, structure a module,
handle errors, and test — the parts that stay the same whatever stack you run on.

## Design the Contract First

Before writing a handler body, sketch the request and response: what comes in,
what goes out, what can fail. A typed/validated model for both request and
response beats raw dictionaries — it documents the contract and catches bad input
at the edge.

## Module Structure

- **Keep modules small and deep.** A small public interface over a lot of hidden
  implementation. Modules that grow past a few hundred lines are a smell — either
  split out a sub-concern or push complexity behind a narrower interface.
- **Hide internals.** Don't re-export internal helpers, adapters, or
  backend-specific types through a package's public surface. If a caller has to
  know which backend you use, the abstraction has leaked.
- **No circular imports.** If two modules import each other, a shared concern
  wants to be extracted into a third.
- **Consolidate shared helpers.** Generic utilities (formatting, encoding, hashing,
  env-var parsing, retry, caching, token handling) belong in one shared location —
  not copy-pasted per service. Move a helper to the shared spot *before* it grows
  callers and tests, not after.

## Error Handling — Fail Loud

Default: do **not** catch errors. Let them break loud.

Two hard rules — never violate:

1. **Never use a broad catch.** No catch-all of the base exception type, no bare
   catch, no `catch (e)` without narrowing. Catch a named/specific error or don't
   catch.
2. **Never wrap big blocks.** The `try` wraps the smallest expression that can
   raise — usually one line. A 20-line try block hides which operation failed.

In new code especially, do not anticipate exceptions. Add a catch only AFTER a
real failure surfaces and you have a concrete recovery action. Anticipation leads
to swallowing — the broad catch hides the real bug, the test passes, corruption
ships. Catching to `pass` / return-empty / log-and-continue is forbidden.

```text
# Wrong — anticipatory catch, swallows whatever happens
try:
    user = get_user(uid)
except <broad>:
    user = None

# Right — let it raise. Add a catch only when a real failure surfaces
# and you have a concrete recovery.
user = get_user(uid)
```

## API Response Format

Keep responses uniform across every endpoint.

- **Success:** the payload under a single key.
  ```json
  { "data": { } }
  ```
- **List + pagination:** cursor-based; the consumer checks for the cursor's
  presence to know whether more data exists.
  ```json
  { "data": [ ], "pagination": { "next_cursor": "abc123" } }
  ```
- **Error:** a flat string, not a nested object.
  ```json
  { "error": "<message>" }
  ```

## HTTP Status Codes

| Code | Usage |
|------|-------|
| 200 | OK — successful request |
| 201 | Created — resource created |
| 400 | Bad Request — invalid input |
| 401 | Unauthorized — missing/invalid auth |
| 403 | Forbidden — authenticated but not permitted |
| 404 | Not Found — resource doesn't exist |
| 500 | Server Error — internal error |

## Auth

- Validate auth at the **top** of every endpoint; reject before doing any work.
- Never expose credentials or service tokens to clients.
- Use the right mechanism per caller: session/cookie for browsers, bearer token
  for service-to-service, short-lived single-use tokens for callbacks.

## Timestamps

- Always ISO 8601, always UTC. Example: `2025-02-26T14:30:00Z`.

## Testing

- **Unit tests** mock external services at the boundary; name them for the
  function and scenario; arrange/act/assert.
- **Integration tests** hit real dependencies with isolated fixtures.
- Run the tests yourself and confirm green before delivering — and run them twice
  to catch flakiness. Lint before delivery and fix issues rather than suppressing
  them.
