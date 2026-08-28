# Draw.io Practices

Use these rules for Draw.io / diagrams.net artifacts.

## File model

- Prefer native, uncompressed `.drawio` XML unless the user asks for another format.
- A valid file has `mxfile`, one or more `diagram` elements, an `mxGraphModel`, structural cells `id="0"` and `id="1"`, vertices/edges, geometry, and styles.
- Keep IDs unique. Every parent reference must exist. Every edge source/target must reference an existing cell when present.
- Use minimal useful pages, layers, and groups; do not add decoration that obscures the model.

## Editing existing diagrams

- Preserve unrelated pages, metadata, styles, IDs, and geometry unless the user asks to change them.
- Convert formats only when requested, and keep the original source file intact unless replacement is explicit.

## Validation and delivery

- Save to the requested output path. If no path is provided, save a clear `.drawio` filename in the current directory.
- Run semantic validation before delivery. Use the validator from the nearest available route: `validate-drawio.py` beside a packaged skill's `SKILL.md`, `~/.shared-llm/generated/skills/drawio/validate-drawio.py`, or a configured home skill symlink such as `~/.claude/skills/drawio/validate-drawio.py`, `~/.pi/agent/skills/drawio/validate-drawio.py`, or `~/.agents/skills/drawio/validate-drawio.py`. Invoke it as `python3 <validator> <file.drawio>`.
- Optional XSD/schema validation can catch structural shape issues, but it does not prove references are valid. Semantic validation is the unattended gate.
- If a renderer is available, open/render the sample and inspect labels and connectors. If not, report visual verification as incomplete.
- Require human approval before destructive overwrites, live sharing/permission changes, or migrations.
