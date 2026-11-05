## General code-review contract

A general review is a self-contained assessment of the requested code scope, not merely specialist selection or report aggregation. Supplied specialist reports are optional evidence, never a prerequisite.

- The assessment covers the changed behavior and reachable affected paths: correctness, security, compatibility, integration, regression protection, and maintainability. Operational safety, architecture, and reachability matter where the change affects them.
- Evidence includes the diff, complete relevant files, actual callers/consumers, and relevant tests. Intent, a green subset, or a summary alone cannot establish code correctness.
- Maintainability findings require a concrete consequence for ownership, evolution, or reliable behavior; personal style preferences are not blocking defects.
- Domain obligations apply only to changed in-scope behavior and its affected contracts. Record an explicit rationale when a domain or check is not applicable; nonapplicability is not automatically FAIL or UNVERIFIED. This is not a mandate to run every check or audit unrelated repository code. Applicable checks with insufficient evidence remain gaps.
- Optional supplied reports retain their reviewer provenance, source IDs, unresolved dissent, and evidence type. Repeated passes by one reviewer are not independent reviews; agreement cannot establish unreviewed coverage.
- General code-review approval does not establish current hosted readiness. That is a separate, explicitly scoped assessment.
