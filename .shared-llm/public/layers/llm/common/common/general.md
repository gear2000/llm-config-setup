# global agent instructions

- Never use the em dash "—". Use plain dash "-" instead
- When writing commit messages, NEVER auto-add your agent name as co-author
- Never manually modify CHANGELOG.md files or any files that are marked as auto-generated
- When making technical decisions, do not give much weight to development cost.
  Instead, prefer quality, simplicity, robustness, scalability, and long term maintainability.
- When doing bug fixes, always start with reproducing the bug in an E2E setting as closely aligned with how an end user would experience it as possible.
  This makes sure you find the real problem so your fix will actually solve it.
- When end-to-end testing a product, be picky about the UI you see and be obsessed with pixel perfection.
  If something clearly looks off, even if it is not directly related to what you are doing, try to get it fixed along the way.
- Apply that same high standard to engineering excellence: lint, test failures, and test flakiness.
  If you see one, even if it is not caused by what you are working on right now, still get it fixed.

## Stay grounded in THIS repository

DO NOT do any of the following. These are hard prohibitions, not preferences:

- **Do not create empty stubs.** Do not create placeholders, `pass`/no-op bodies,
  hardcoded fake values, or "TODO: implement later" code - ever - unless the human in
  the loop explicitly approved that exact stub. If you cannot fully implement
  something, stop and say so loudly. Do not hand back work that silently contains
  unfinished pieces.
- **Do not invent your own patterns.** Do not import patterns, libraries,
  abstractions, or "standard" methodologies from your training or from research and
  force them into this repository. Before writing code, read how this repository
  already does it - neighboring files, existing utilities, naming, error handling,
  test style - and build on that.
- **Do not assume something is acceptable just because it solves the problem.** You
  must solve it the way it is instructed, and in the way that fits the context of
  this repository. A solution that "works" but ignores the instructions or the
  repo's conventions is wrong.
- **Do not go off context. Do not invent your own context.** The instructions you
  were given and the conventions already in this repository are the source of truth.
  Follow them exactly, even where they differ from what your training, general best
  practice, or outside research says. This repository may deliberately not follow
  what the research says - we follow what best solves the problem in the context of
  this codebase, and that context wins.
- **Do not invent APIs, functions, config keys, file paths, or behavior.** If you
  have not read it in this repository or its documentation, verify it exists before
  using it.
- **Do not silently deviate.** When your instructions and the repository's existing
  conventions genuinely conflict, or you are tempted to deviate from the
  instructions for any reason, do not pick one on your own: stop and ask the human
  in the loop.
