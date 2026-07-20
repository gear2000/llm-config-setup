---
name: cc-planish
description: Deprecated alias for `/cc-plan`. Warns, then delegates; Planish remains the visual grill renderer inside `/cc-plan`.
argument-hint: <topic> [--dir <path>] [--adversarial-iterations N] [--adversary-profile <profile>]
---

# /cc-planish

Deprecated one-release alias for `/cc-plan`.

Warn the user:

```text
WARNING: /cc-planish is deprecated. Use /cc-plan; Planish remains the default visual grill renderer inside the planning flow.
```

Then delegate to:

```text
/cc-plan <same arguments>
```

Do not keep a separate standalone planning workflow here. This alias exists only for migration.
