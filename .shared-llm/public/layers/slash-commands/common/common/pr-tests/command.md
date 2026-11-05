# Review PR regression evidence

Assess whether tests establish the changed behavior using the appended contracts. A green suite is not a substitute for behavioral evidence.

Example: `/pr-tests repo=. pr=42 scope="parser validation and retry regression claims"`

## Procedure

1. Establish the shared snapshot and map each accepted behavior/regression claim to its real entry point, test, meaningful assertion, and expected observable result. Select relevant success, boundary, malformed-input, failure, retry, and concurrency cases; explain exclusions rather than requiring every category.
2. Inspect assertions, mocks, and fixtures for bypassed changed paths or tests that would pass with wrong behavior. Trace malformed input through the actual parser, API, CLI, or other public boundary to validation and outcome. Inspect the asserted error and prohibited side effects, not only helper behavior.
3. Separate static test inspection, supplied CI evidence, and actual execution. For authorized execution, record command, revision, environment, selected cases, assertions, and every outcome. Classify setup/import failures, unavailable services, timeouts, and flaky results separately from reproducible behavioral failures; do not silently rerun until green.
4. If a same-test negative control is explicitly authorized, identify fixed/unfixed commits and justify the control. Follow the shared disposable-copy exception with identical existing test/harness files; compare whether fixed passes and unfixed fails for the intended assertion. Record incompatible setup or unrelated failure as an unproven regression claim. Propose missing assertions/test designs for the owner rather than authoring tests or rewriting assertions during review.
5. Return the shared report and behavior-to-assertion map. Use `PR-TEST-001`, etc. for new findings and preserve criterion IDs. Distinguish coverage gaps, demonstrated test defects, and execution limits. State whether the same-test control was performed, its results, or why it remains UNVERIFIED. Approval concerns regression evidence, not current hosted readiness.
