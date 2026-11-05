## Regression-evidence contract

Applies to changed in-scope behaviors and regression claims. Relevant success, boundary, malformed-input, failure, retry, and concurrency cases follow the affected contract; record why excluded cases are not applicable rather than requiring every category universally.

- Each claimed behavior maps to a real entry point, relevant test, meaningful assertion, and expected observable result. Coverage percentage, test presence, or a green suite alone does not prove it.
- Mocks and fixtures do not bypass the behavior claimed to be tested. Malformed-input claims reach the actual parser/API/CLI boundary and assert the error and prohibited side effects, not merely an internal helper result.
- Execution evidence identifies command, revision, environment, selected cases, assertions, and outcome. Setup/import failures, unavailable services, timeouts, and flakiness are distinct from reproducible behavioral failure; repeated runs do not erase earlier outcomes.
- A negative-control claim requires the same immutable test and necessary harness files, a justified unfixed control commit, a pass on fixed code, and failure on unfixed code for the intended assertion. Unrelated failure or incompatible setup does not prove the regression claim. Copying authority and isolation follow the shared contract; assertions cannot be rewritten to obtain a desired result.
- Static inspection, supplied results, and observed execution support different claims. If the same-test control is not performed or cannot establish the claim, its execution evidence remains UNVERIFIED with the reason recorded.
