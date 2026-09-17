## Scope

Do exactly what was asked. Not less. Not more.

Do not project your training onto this codebase. A word or an approach from training may not apply here. Confirm it exists in this repository first.

Stay with this system's design. When a reviewer, a worker, or your own instinct says the code should work the way some other system or industry works, that is not a finding. The repository's existing mechanism is the standard the code is held to. A review finding is binding only when it shows the code breaks this system's own design, contract, or rule. Do not bend the system to a pattern it does not follow, and do not let repair loops drift toward one; if a finding asks for that, reject it and say why.

A loose ask: fill the gaps. A specific ask: follow it as written. Do not add polish, extra features, or extra abstraction.

Extend existing code. Do not invent a new file, pattern, or layer to do the same job.

Tests cover the contract. Do not cover every permutation.

Do not drive-by refactor. Do not clean up "while you are here."

If the work looks bigger or smaller than the ask, stop and ask. Do not guess and keep going.
