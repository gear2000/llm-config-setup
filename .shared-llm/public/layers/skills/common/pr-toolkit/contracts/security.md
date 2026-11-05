## Security contract

Applies to changed in-scope trust boundaries, sensitive data/effects, and affected dependency or supply-chain behavior. Record why unaffected security concerns are not applicable; this is not a repository-wide security audit.

- Authentication, authorization, tenant isolation, and object ownership hold from the real entry point to affected data access or privileged effects. Identity/context provenance and downstream enforcement are evidenced.
- Attacker-controlled inputs cannot bypass the required validation, encoding, parameterization, canonicalization, or redirect policy on reachable paths to queries, commands, paths, templates, deserialization, outbound requests, or privileged actions.
- Changed secret handling, logs, errors, and telemetry respect disclosure and retention requirements. Evidence names redacted locations and data categories, not secret values.
- Dependency findings identify the selected version, relevant advisory or provenance evidence, reachable use, and exploit prerequisites. A package name or scanner label alone does not prove exploitability.
- A vulnerability finding establishes actor capabilities, credentials/settings, controllable input, missing enforcement, reachable sink, and concrete impact, with caller/enforcement counterevidence considered. Static demonstration and observed exploitation are distinct evidence types; incomplete chains remain limitations, not confirmed vulnerabilities.
