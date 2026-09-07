# /do-full

Deprecated. This composer is disabled.

Warn the user and stop. Do not plan, convert, implement, or start a run.

```text
ERROR: /do-full is disabled.
Plan with /do-plan. After the approved plan.md exists, run Flow 1 from a
Claude Code HIL pane (Claude Code remote app):

  /hil --plan <plan.md> --offering <id> --effort <effort>

That starts a HIL relay to a plan-implementer. Do not use /do-implement or
just run-start unless the human explicitly asks for those fallbacks.
```

Do not call `/do-plan`, `/do-implement`, `/do-convert`, or `just run-start` from this command.
