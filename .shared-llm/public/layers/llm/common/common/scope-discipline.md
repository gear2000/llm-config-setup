## Scope

Do exactly what was asked. Not less. Not more.

The repository is the authority for its language, terms, context, architecture, and recorded design decisions. Model training and industry convention are fallback material only when the repository is silent. Do not replace a repository choice with a familiar or fashionable pattern.

Stay with this system's design. When a reviewer, a worker, or your own instinct says the code should work the way some other system or industry works, that is not a finding. A review finding is binding only when it shows the code breaks this system's own design, contract, or rule. Reject findings that ask the system to become something else. If the task appears to require a design change, stop and ask the human. Do not make that change on your own, and do not let a repair loop drift into it.

A loose ask: fill the gaps. A specific ask: follow it as written. Do not add polish, extra features, or extra abstraction.

Extend existing code. Do not invent a new file, pattern, or layer to do the same job.

Tests cover the contract. Do not cover every permutation.

Do not drive-by refactor. Do not clean up "while you are here."

If the work looks bigger or smaller than the ask, stop and ask. Do not guess and keep going.
