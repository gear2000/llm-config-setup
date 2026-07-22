# /meta-plan-check

Deprecated one-release alias. Validation is part of `/cc-convert --herdr` and `/do-convert --herdr`, and the run controller rechecks at `just run-start`.

Warn the user:

```text
WARNING: /meta-plan-check is deprecated as a separate user command. Run /cc-convert --herdr <plan.md> in Claude Code or /do-convert --herdr <plan.md> in Pi; the converter validates internally.
```

Then delegate to the matching harness converter when a source plan is provided:

```text
/cc-convert --herdr <plan.md>
/do-convert --herdr <plan.md>
```

If only a converted run directory was provided, tell the user to run:

```text
just run-start <converted-run-dir>
```

Do not maintain a separate active checking path in this alias.
