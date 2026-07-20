# /meta-plan-convert

Deprecated one-release alias for the harness-specific Herdr converter.

Warn the user:

```text
WARNING: /meta-plan-convert is deprecated. Use /cc-convert --herdr <plan.md> in Claude Code or /do-convert --herdr <plan.md> in Pi.
```

Then delegate with the same source plan and output intent:

```text
/cc-convert --herdr <same arguments>
/do-convert --herdr <same arguments>
```

Choose the command that matches the current harness. Do not preserve a separate conversion or check workflow here.
