# Package Architecture

## Hierarchical layered architecture

All code follows a strict layered hierarchy. Packages sit below services. Imports flow downward
only — a lower package must never import from a package or service above it.

```
Layer 0 — primitives:   shared types, constants, utilities with no external deps
Layer 1 — adapters:     one module per external system (queue, DB, HTTP client, cache)
Layer 2 — domain:       domain logic with no I/O (models, rules, computation)
Layer 3 — application:  orchestrate layers 0-2 (handlers, use cases, roles)
Layer 4 — entry points: main / lambda root — wire everything, minimal public surface
```

Three package classifications:

- **Universal** (layers 0-1): stateless technical primitives. Cannot import from a higher layer.
- **High-context** (layers 2-3): opinionated about the technical environment; may contain domain
  logic but must not know about user-facing product workflows.
- **Service-contextual**: shapes generic packages into service-specific concepts for one service
  only. In Go, this is `internal/` (compiler-enforced). In Python, a local `_internal/` module.
  Never published globally.

## Deep module contract

A deep module has a narrow public interface and a rich hidden implementation.

Ask: would a caller need to understand the internals to use this correctly? If yes, the interface
is too shallow — push complexity inward.

Supporting smell: ~10+ exported symbols (types + functions + methods combined) is a prompt to
review whether the interface is too wide, not an automatic reject.

No pass-through packages. A module that only re-exports another library without adding value,
transforming data, or altering configuration adds cost (an interface) without adding depth.

## LLM placement decisions

Identify the target layer, design the minimum public interface, verify imports flow downward only.

---
**DECISION POINT: new_package_or_service**

Trigger: you are about to create a new package or a new service.

Always stop and ask the user before creating:
- "What layer does this belong to — Universal, High-context, or Service-contextual?"
- "Which services will use this? One, or more than one?"
- "Should this be a new shared package, or stay internal to the one service that needs it?"

---
**DECISION POINT: business_logic_placement**

Trigger: you are placing domain or business logic and the right layer is not obvious.

- Default: lowest cohesive layer. Do not ask.
- Ask only when two or more services would share the same logic.
- Question: "Services A and B both need this — should I create a shared package, or duplicate it?"

Repo-level overrides may permit business logic in lower-level packages by default.
Check `layers/skills/this_repo/<language>.md` if present.

---
**DECISION POINT: service_contextual_package**

Trigger: a service is growing large and helper logic is piling up in entry-point files.

Ask: "This service is getting large. Should I create an internal module to organise helpers
(`internal/` in Go, `_internal/` in Python), or keep it flat?"
