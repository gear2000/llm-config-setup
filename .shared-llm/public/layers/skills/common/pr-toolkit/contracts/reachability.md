## Reachability contract

Applies to potentially unused changed code, exports, configuration, or assets and paths made unreachable by the change. Record reasons for exclusions; this is not a repository-wide deletion campaign.

- A dead-code claim has positive evidence about supported entry points, registration, packaging/export surfaces, and execution conditions. An absent grep reference alone is not proof of unreachability.
- Reflection, dynamic loading/imports, plugins, dependency injection, configuration references, generated registrations, scripts, and external consumers are considered where supported. Unknown external usage limits confidence rather than proving deadness.
- Unreachable, redundant, disabled-by-default, deprecated-but-supported, and externally consumed are distinct classifications. Conditional paths are assessed against supported configurations and environments.
- Evidence explains why the candidate cannot be reached within the stated scope and what uncertainty remains. Recommendations identify behavioral and compatibility consequences of removal; review does not authorize deletion.
