# Scope leash — the brief block for overproducing models

Some models write far more code and far more tests than a stage needs. The leash is a
profile flag, never a hardcoded model list. Set `scope_leash: true` on any `llm_profiles`
entry (route-defaults.yaml carries the current picks), and the phase leader copies the
block below VERBATIM into every stage brief routed to that profile.

---

SCOPE DISCIPLINE (mandatory)

- Implement only what this stage requires. No speculative abstractions, no extra
  features, no drive-by refactors.
- Tests proportionate to the change: cover the contract, not every permutation.
- Prefer extending existing files over creating new ones.
- If you believe more work is genuinely required, say so in your result notes instead
  of doing it.

---
