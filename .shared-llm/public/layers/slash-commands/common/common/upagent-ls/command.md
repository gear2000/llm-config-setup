# List UpAgent requests

Use only the canonical Hub façade. Interpret `$ARGUMENTS` as exactly one optional view: `active`, `terminal`, or `all`; default to `active`. Reject other values instead of guessing.

Run:

```bash
just upagent lists --type workers --status <active|terminal|all> --json
```

Render request id, state, and available worker/manager address evidence. `pruned` entries are retained terminal tombstones, not active workers. This command is read-only: do not launch, await, cancel, reconcile, or clean anything.
