# Refactor safely

Simplify the requested code while preserving its behavior. Use the repository's existing planning and testing workflow; this is a bounded refactoring procedure, not a replacement plan or TDD system.

## Procedure

1. **Bound the change.** Identify the target, callers, tests, and applicable repository instructions. State what must stay unchanged: public interfaces, data shapes and persistence, error behavior, and user-visible behavior. Separate requested behavior changes from this refactor; ask before expanding scope.
2. **Characterize first.** Run the relevant existing checks against the unchanged implementation. Where coverage is missing, add focused characterization tests and run them against that same unchanged implementation before editing production code. Record the baseline, including pre-existing failures. Use authorized test environments with disposable storage; never run mutating checks against real user data. If behavior cannot be exercised, explain the gap and agree on alternative evidence before proceeding; do not claim preservation for unverified behavior.
3. **Simplify in small steps.** Prefer clear names, less duplication, simpler control flow, and fewer unnecessary abstractions. After each small edit, run the checks that cover its affected behavior. Investigate any difference before continuing; undo only your own failing edit when necessary. Leave unrelated cleanup and product changes out.
4. **Verify the whole delta.** Run affected tests and broader repository checks appropriate to the change. Inspect the final diff for interface, data, error, and user-behavior changes, including callers outside the edited files. A passing narrow test is not proof of every preserved contract.

## Return

Report what became simpler, the preserved contracts, baseline and final check results, and any remaining unverified behavior. For each preserved criterion, record the revision or working-tree snapshot, command or inspection, and evidence: `PASS` when checked and preserved, `FAIL` when behavior changed against the contract, or `UNVERIFIED` when evidence is missing. State the missing check for each gap. Keep these statuses separate from the overall completion claim; unresolved preservation checks prevent an unqualified success claim. Keep the working changes available for review. Do not automatically commit, push, or open a pull request.
