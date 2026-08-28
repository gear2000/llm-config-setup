# Lucidchart Practices

Use these rules for Lucidchart work. Never place credentials in repository files, prompts, artifacts, or logs.

## Supported execution paths

1. **Interactive harness:** use the official Lucid MCP endpoint `https://mcp.lucid.app/mcp` with OAuth configured in user-local harness settings. If the organization requires admin enablement, tell the user to enable Lucid MCP for the account first.
2. **Unattended UpAgent:** check whether the hired harness inherited a working Lucid MCP connection. If not, stop and provide non-secret setup instructions. Autonomous API-key MCP configuration may live only in user-local settings.

## Standard Import fallback

- Use Standard Import only for create-only workflows. It is not a substitute for search, edit, share, or export.
- When MCP is unavailable but create-only API import is explicitly appropriate, build a `.lucid` ZIP containing `document.json` and send multipart form data to the documented `POST https://api.lucid.co/v1/documents` endpoint. Include `file`, import `type`, and `product` (`lucidchart` or `lucidspark`) fields, and set the file MIME type to `x-application/vnd.lucid.standardImport`. Use only a user-local bearer credential.
- Re-check Lucid's current Standard Import documentation before implementing an import; report any mismatch and stop.

## Delivery and safety

- Do not treat `lucid-package` as a diagram CLI.
- Inspect the created document or an export before delivery.
- Sharing and permission changes are request-only and require human approval.
- If MCP and the needed API capability are unavailable, report missing setup and stop rather than producing a different format.
