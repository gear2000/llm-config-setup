# Third-party skill customizations

This file records local policy changes made to externally managed skills. It is not a changelog: it is the durable checklist for preserving intentional local behavior when a skill is updated or reinstalled.

## Update workflow

```text
before updating a third-party skill
├── read this file
├── note the skill's local requirements
└── capture the current diff from upstream

update or reinstall the skill
└── upstream files may be replaced

after updating
├── let the installer refresh the upstream skill lock
├── reapply each local requirement
├── reconcile it with upstream changes
└── test the effective skill
```

- Keep requirements here, outside directories that an upstream installer may replace.
- Record behavior and intent, not a brittle copy of the complete upstream file.
- Never silently discard a customization because upstream wording or structure changed.
- Add one section per customized third-party skill.

## Lavish

- **Upstream:** `kunchenguid/lavish-axi`
- **Managed files:** `.agents/skills/lavish/SKILL.md` and `.claude/skills/lavish/SKILL.md`
- **Local requirement:** ASCII trees and ASCII flows are the default for decisions, relationships, workflows, architecture, state, and cause and effect.
- **Fallback:** If ASCII cannot represent the subject clearly and accurately, say so and use another suitable visual structure.
- **Mermaid:** Use Mermaid only when the user explicitly requests Mermaid.
- **Skill lock:** Let the upstream installer refresh `skills-lock.json` before reapplying these local changes. Do not rewrite the lock merely to hide intentional local divergence.
- **Update check:** After reinstalling Lavish, confirm both managed copies enforce these requirements in their visual guidance and diagram playbook routing.
