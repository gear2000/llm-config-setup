# Clean terminal UpAgent history

Map `$ARGUMENTS` to exactly this grammar:

```text
cleanup (--request ID | --all-terminal) [--older-than-seconds N] [--apply] [--json]
```

Require one selector. `N` must be a non-negative integer. Run through the canonical façade:

```bash
just upagent cleanup <selector> [--older-than-seconds N] [--apply] --json
```

Dry-run is the default. Never add `--apply` unless the user explicitly requested mutation after seeing or knowingly bypassing the dry-run. Explain each `planned`, `cleaned`, `already-pruned`, or `skipped` outcome.

Cleanup is history pruning only. It must never be used to stop runtime, kill panes/processes, or remove a caller-owned prompt/run tree. Active, awaiting-requester, malformed, cleanup-failed, too-young, live-lease, live-runner, and unresolved-pane requests are ineligible. An inclusive threshold means `age >= N`. Applied cleanup retains the Hub tombstone needed by `/upagent-get`, authenticated terminal cancellation, audit, same-hash attachment, and changed-hash conflict.
