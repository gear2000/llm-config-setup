# Next.js Conventions

General conventions for Next.js App Router projects. These are framework-level
practices — pick your own auth provider, data layer, component library, and test
runners; the rules below hold regardless of which you choose.

## Stack Baseline

- **App Router only** — no Pages Router in new code.
- **TypeScript** — no `any`; type props, API payloads, and hook returns.
- A single styling approach — don't mix utility CSS, CSS Modules, and CSS-in-JS in
  one codebase. Pick one and keep to it.
- One client-side data-fetching library for the whole app (don't hand-roll `fetch`
  in components alongside a fetching library — choose one path).

## Code Rules

- `'use client'` at the top of every client component; keep components server-side
  by default and only opt into the client when you need interactivity or browser APIs.
- No `fetch()` directly in page or component files — go through your data-fetching
  hooks so caching, auth headers, and error handling stay in one place.
- Every page handles three states explicitly: loading, error, and empty.
- Centralize class-name merging and component-variant helpers; don't re-implement
  them per component.
- Use one date/time utility consistently for formatting.

## Naming Conventions

| Thing | Convention |
|-------|-----------|
| Types / interfaces | PascalCase |
| Variables / functions | camelCase |
| Components | PascalCase |
| Route / directory segments | kebab-case |
| Env vars | SCREAMING_SNAKE_CASE |

## Keep the Frontend Thin

Next.js is for **UI rendering and routing**. Business logic, data processing, and
external-service calls belong in the backend, not in pages, components, or route
handlers. The frontend is a thin window into the backend.

- Keep external-service clients (SDKs, database clients) in a dedicated `lib/` (or
  `hooks/`) layer. Pages and components import from there — they never call an
  external service directly.
- **API routes are thin proxies:** validate the session, add auth headers, forward
  to the real backend, return the result. No business logic, no multi-step
  orchestration, no data transforms. If a route handler grows past ~10 lines of
  logic, that logic belongs in a backend service.
- Avoid framework features that pull business logic or data-caching back into the
  UI layer (server actions, route-level cache/revalidation helpers, module-level
  in-memory state). Prefer explicit client-side mutations and backend caching, so
  the frontend stays portable.

## API Route Patterns

- Validate auth at the top of every route — return `401` immediately on failure.
- Keep routes thin: auth check → fetch/mutate → return JSON.
- Use server-side helpers for auth and data access — never expose credentials or
  service tokens to the client.

## Data-Fetching Pattern

- Keep data-fetching hooks together (e.g. a single `hooks/use-api.ts`), not
  scattered across components.
- Support conditional fetching: pass a `null`/disabled key to skip the request
  until its dependencies are ready.
- A shared fetcher normalizes response shapes and centralizes the unauthorized
  (`401`) response — typically redirecting to the sign-in route.

```typescript
export function useItems(id: string | null) {
  // skip until `id` is known
  return useApi(id ? `/api/items/${id}` : null)
}
```

## Component Pattern

```typescript
'use client'
export default function MyPage() {
  const { data, isLoading, error } = useItems(id)
  if (isLoading) return <div>Loading...</div>
  if (error) return <div>Error: {error.message}</div>
  if (!data?.length) return <div>No items found</div>
  return <ul>{data.map(item => <li key={item.id}>{item.name}</li>)}</ul>
}
```

## Testing

- **Unit:** fast, no real network; mock at the boundary (auth provider, data layer).
- **Integration:** hit real dependencies with isolated fixtures; run serially when
  they share state.
- **End-to-end:** drive the real app in a browser; take `baseURL` from an env var,
  and seed auth/reset state in a global setup step.

## Dev Workflow

```bash
npm run dev                # dev server
npm test                   # unit tests
npm run test:integration   # integration tests
npm run test:e2e           # end-to-end tests
```
