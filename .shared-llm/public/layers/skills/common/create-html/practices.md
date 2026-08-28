# Create HTML Practices

Use these rules when the deliverable is a static `.html` artifact.

## Output contract

- Save to the requested output path. If no path is provided, save a clear `.html` filename in the current directory.
- Produce one self-contained static `.html` file by default. Inline CSS and JavaScript when useful. Use no external dependency or network asset unless the user asks for it.
- Focus on presentation: no server, framework, annotation controls, review loop, or application scaffold. Frontend application work belongs to `frontend`.
- Mention Lavish only as an optional external annotation/review workflow when the user asks for review tooling; it is not a kit-owned matching target.
- Use Mermaid or another source format only when requested.

## Design quality

- Use semantic markup, readable typography, clear hierarchy, responsive layout, and accessibility basics.
- Prevent horizontal overflow: wrap long tokens, constrain media, and test narrow widths.
- Make the file open directly from disk without a server or network.

## Verification

- Use available browser automation for local-file loading, console errors, overflow, and broken assets. `playwright-cli` owns browser driving/test mechanics; combine practices for mixed create-plus-browser tasks.
- If browser tooling is unavailable, report visual verification as incomplete rather than claiming success.
- Require human approval before overwriting important files or changing sharing/permissions.
