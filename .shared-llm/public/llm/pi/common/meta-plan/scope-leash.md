# Scope leash — the brief block for overproducing models

Some models write far more code, take far more steps, and second-guess far more than a
stage needs — whatever the stage's role. The leash is a profile flag, never a hardcoded
model list. Set `scope_leash: true` on any `llm_profiles` entry (route-defaults.yaml
carries the current picks), and the phase leader copies the block below VERBATIM into
every stage brief routed to that profile.

---

SCOPE DISCIPLINE (mandatory)

- Do exactly what this stage's brief calls for — no more. Extra thoroughness the stage
  did not ask for is not a virtue; it is more for the human to read and more that can
  go wrong.
- Implementing: implement only what this stage requires. No speculative abstractions,
  no extra features, no drive-by refactors. Tests proportionate to the change — cover
  the contract, not every permutation. Prefer extending existing files over creating
  new ones.
- Reviewing, auditing, advising, or judging: reach your verdict as soon as you have
  enough evidence for it. Do not add extra investigation passes, extra re-reads, or
  extra rounds of second-guessing beyond what this stage's decision requires.
- If you believe more work is genuinely required, say so in your result notes instead
  of doing it.

---
