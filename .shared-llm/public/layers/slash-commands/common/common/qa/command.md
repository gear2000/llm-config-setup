# QA Session

Run an interactive QA session. The user describes problems; you clarify, explore for context, and file durable issues using the project's domain language.

## For each issue

1. **Listen** — let the user describe it in their own words
2. **Clarify lightly** — at most 2-3 questions: expected vs actual, steps to reproduce, consistent or intermittent. If it's clear enough, just file it.
3. **Explore in background** — spawn a quick Agent(subagent_type=Explore) to learn the domain language and understand the feature's intended behavior. Goal is context, not a fix.
4. **Decide scope** — one issue or break it down? Break down only when concerns are clearly separable. Keep as one when symptoms share a root cause.
5. **File it** — use `gh issue create` or whatever tracking the user prefers.

## Issue quality rules

- Describe behaviors, not code
- Use the project's domain language
- Reproduction steps are mandatory
- No file paths or line numbers — they go stale
- Keep it concise

## Issue template

```markdown
## What happened
[Actual behavior]

## What I expected
[Expected behavior]

## Steps to reproduce
1. [Concrete steps using domain terms]

## Additional context
[Only if relevant]
```

After filing, ask: "Next issue, or are we done?"
