---
name: cc-full
description: Disabled composer. Warns and stops. Use `/cc-plan` then `/hil --plan <plan.md>` for Flow 1 execution. Use when someone invokes `/cc-full`.
---

# /cc-full

Deprecated. This composer is disabled.

Warn the user and stop. Do not plan, convert, implement, or start a run.

```text
ERROR: /cc-full is disabled.
Plan with /cc-plan. After the approved plan.md exists, run Flow 1:

  /hil --plan <plan.md> --offering <id> --effort <effort>

That starts a HIL relay to a plan-implementer. Do not use /cc-implement or
just run-start unless the human explicitly asks for those fallbacks.
```

Do not call `/cc-plan`, `/cc-implement`, `/cc-convert`, or `just run-start` from this command.
