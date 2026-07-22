---
name: cc-plan-and-grill
description: Deprecated alias for `/cc-plan`. Warns, then delegates to the new Claude Code planning front door; it no longer creates `route.yaml`, checks, implements, or starts the checked run.
---

# /cc-plan-and-grill

Deprecated one-release alias for `/cc-plan`.

Warn the user:

```text
WARNING: /cc-plan-and-grill is deprecated. Use /cc-plan; it always grills, conditionally resolves design, and runs the default plan-adversary review loop.
```

Then delegate to:

```text
/cc-plan <same arguments>
```

Do not create `route.yaml`, run conversion, run a separate check, implement, or start the checked run in this alias.
