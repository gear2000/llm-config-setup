---
name: do-plan-and-grill
description: Deprecated alias for `/do-plan`. Warns, then delegates to the new Pi planning front door; it no longer creates `route.yaml`, checks, implements, or starts the checked run.
---

# /do-plan-and-grill

Deprecated one-release alias for `/do-plan`.

Warn the user:

```text
WARNING: /do-plan-and-grill is deprecated. Use /do-plan; it always grills, conditionally resolves design, and runs the default plan-adversary review loop.
```

Then delegate to:

```text
/do-plan <same arguments>
```

Do not create `route.yaml`, run conversion, run a separate check, implement, or start the checked run in this alias.
