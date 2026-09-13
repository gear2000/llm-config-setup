<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to aws-execution-engine.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# {{COMPONENT_NAME}}

<!-- TODO(project): What this component does, how it is invoked, what callers depend on. Replace {{COMPONENT_NAME}}. -->

{{COMPONENT_DESCRIPTION}}

## Package architecture

Maintainable code is a hierarchy. Imports flow down only. Do not import upward.

Repo:

```
higher services
└── services
    └── higher-level packages
        └── lower-level packages
```

Packages sit at the bottom. A higher-level package is built on lower-level packages. A service is built on packages. A higher service is built on services and packages.

Inside one package:

```
Layer 4  entry points   main or lambda. Wire only.
Layer 3  application    orchestrates 0-2
Layer 2  domain         rules and models. No I/O.
Layer 1  adapters       one module per external system
Layer 0  primitives     types, constants, utilities. No external deps.
```

Same direction. Layer 4 sits on 3, on 2, on 1, on 0.

- Universal (0-1): stateless primitives. Do not import from a higher layer.
- High-context (2-3): environment-specific. Do not know user-facing product workflows.
- Service-contextual: one service only. Go `internal/`. Python `_internal/`. Do not publish it.

A deep module hides internals behind a narrow seam: the public interface. Test that module. Test how other code talks to it through that seam. Do not test the whole tree as one blob. Do not add a wrapper that only re-exports another library.

Before you create a package or a service, stop and ask.

Place logic at the lowest cohesive layer. Ask only if two or more services would share it.

If helpers pile up in an entry point, ask whether to add an internal module.

## Conventions

- The public contract lives in `CONTRACT.md` at the component root. Update it in the same commit as any field change.
- <!-- TODO(project): add one or two conventions specific to this component, then delete this comment -->
