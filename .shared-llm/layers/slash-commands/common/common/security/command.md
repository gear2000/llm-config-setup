# Security Conventions

## Auth

- Validate auth at every entry point — API routes, Lambda handlers, serverless/edge functions
- Use short-lived tokens with explicit expiration
- Scope tokens to minimum required permissions (`{resource}:{action}`)
- Never trust client-supplied identity without server-side verification

## Secrets

- Secrets in environment variables only — never in code, config files, or logs
- No secrets in error messages or API responses
- Rotate secrets on any suspected exposure
- Use a secrets manager (AWS SSM, Secrets Manager) for runtime secrets

## IAM / Least Privilege

- No `Action: "*"` in any policy
- No `Resource: "*"` unless AWS explicitly requires it
- Scope to specific resource ARNs
- Use condition keys to restrict by account, source ARN, or tag
- Document any broad permissions with a comment explaining why

## Input Validation

- Validate all inputs at system boundaries — never trust user input
- Use a schema library (Pydantic, Zod) — no manual string parsing
- Reject unknown fields rather than ignoring them

## Code Review Red Flags

- Hardcoded credentials or tokens
- Broad exception handlers that swallow auth errors silently
- Missing auth checks on any handler or route
- Logging request bodies that may contain secrets
- SQL or shell string interpolation (injection vectors)
