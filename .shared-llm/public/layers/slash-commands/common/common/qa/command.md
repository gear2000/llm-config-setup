# QA Session

Run an interactive QA session. The user describes problems; you clarify, explore for context, and prepare durable issues using the project's domain language. Publish only when the original request already authorizes filing issues or the user separately approves publication.

## For each issue

1. **Listen** — let the user describe it in their own words
2. **Clarify lightly** — at most 2-3 questions: expected vs actual, known steps, consistent or intermittent. A useful user report can remain UNVERIFIED; exact reproduction is not a prerequisite for intake.
3. **Explore for context** — read the relevant behavior and domain language, using an exploration helper if available. Goal is context, not a fix. Distinguish the user's report, your observations, and inferred causes. Run probes only in explicitly authorized disposable workspaces/storage, with isolation verified before execution.
4. **Check existing work** — search open and closed issues and PRs, including merged work. Read plausible matches and linked decisions. Use the heuristic: would the same fix resolve both? Similar symptoms alone do not establish a duplicate. Record queries, states, limits/pagination, access failures, and uncertainty; a capped search is not exhaustive. Check whether a claimed fix is present in the relevant version.
5. **Decide scope** — separate concerns only when they are independently fixable. Keep shared-root-cause symptoms together. Report duplicate/related links and distinguish the original from a later copy; finding a newer copy does not invalidate the original. Never automatically close, edit, or relabel existing issues or PRs.
6. **Draft or file** — use `gh issue create` or the preferred tracker only with publication authority. Otherwise return the draft and request approval. For a likely duplicate, report the existing item and proposed next action rather than automatically filing another.

## Issue quality rules

- Describe behaviors, not code
- Use the project's domain language
- Include known reproduction steps and verification status; when unknown, say so and retain the reported conditions without inventing a repro
- Label user-reported behavior separately from directly observed behavior and hypotheses
- For each behavior check, record the relevant version or snapshot and evidence: `PASS` for a checked expectation met, `FAIL` for an observed violation, or `UNVERIFIED` for missing/incomplete verification. Keep issue filing and duplicate disposition separate from these statuses.
- Include duplicate-search scope/limits and relevant issue or PR links
- No file paths or line numbers — they go stale
- Keep it concise

## Issue template

```markdown
## What happened
[User report; separately state any directly observed behavior and evidence]

## What I expected
[Expected behavior]

## Steps to reproduce
[Known steps using domain terms, or "Unknown; reported conditions: ..."]

## Verification and related work
[PASS / FAIL / UNVERIFIED per check; version and evidence; user-reported claims labelled separately; related links and duplicate-search scope/limits]

## Additional context
[Only if relevant]
```

After drafting, filing, or reporting a duplicate, ask: "Next issue, or are we done?"
