# Get one UpAgent request

Require exactly one canonical request id in `$ARGUMENTS`; do not infer an id from unrelated files.

Run the read-only command:

```bash
just upagent get --request <request-id> --json
```

Report submission/lifecycle state, terminal verdict when present, the retained result and receipt, and the result/compacted/handoff/receipt/log pointers. When `state` is `pruned`, explain the retained terminal state/timestamps and distinguish pointers marked `pruned` from caller-owned retained paths. Do not reconstruct, republish, await, launch, cancel, reconcile, or clean the request.
