# Prepare a PR draft

Draft a title and body from the final committed diff using the appended contracts. Produce proposed text, not a hosted PR update.

Example: `/pr-prepare repo=. base=origin/main head=feature/cache target=main scope="draft committed candidate title/body"`

## Procedure

1. Establish the shared snapshot and inspect the full committed diff, relevant complete files, accepted requirements, and available test, compatibility, and rollout evidence. Label a prepublication candidate explicitly; do not include uncommitted work.
2. Separate stated intent from demonstrated changes. Account for the full diff, identify non-goals, and flag unexplained changes or missing decisions. Avoid inventing motivation or behavior unsupported by the inputs.
3. Compare a supplied or read-only accessible existing PR body against the exact diff. Identify stale descriptions, omitted changes, unsupported test claims, and compatibility/rollout mismatches. If the existing body is unavailable, record the comparison as UNVERIFIED rather than pretending no body exists.
4. Draft a concise title and proportionate body covering Intent, Changes, Non-goals, Tests and evidence, Compatibility, Rollout/rollback, and Unverified items. Cite commands/results and revisions where available; distinguish supplied results from execution and say "not run" when appropriate. Explain nonapplicable sections without inventing requirements.
5. Return the shared report followed by the draft and comparison summary, or place them within a stricter caller schema's allowed fields. Use `PR-PREP-001`, etc. for new findings if needed. Keep drafting criteria separate from code-review and hosted-readiness claims; a complete description does not establish either. Leave publication and use of the text to the owner.
