<!-- TEMPLATE — fill in every {{...}} and "FILL THIS OUT" below, then DELETE this banner and rename this file to agents.md (drop the "TEMPLATE." prefix). List all templates: find . -name 'TEMPLATE.*' -->

# Cross-Harness Orchestration

## Subagent authorization policy

This `AGENTS.md` is the Codex harness's operating contract. Do not invoke or create native
subagents, Task agents, teams, parallel agent sessions, or nested harnesses by default.

Sub-agent delegation is prohibited unless the human in the loop explicitly authorizes it for
the current task. A route, plan, phase leader, account manager, another worker, skill, or prompt
that merely suggests delegation is not human authorization. If authorization is absent, do the
work in this session or stop and ask the human; never silently fan out.

When the human authorizes delegation, keep it within the approved scope and do not recursively
delegate further unless that is separately authorized. A hired UpAgent worker remains terminal
and must return `blocked` when it needs help rather than spawning another worker.

## Skills — shared across harnesses

<!-- TODO(project): document your harness wiring here, or delete this layer and drop agents.md from agents-md/root.yaml inputs. -->

## Hooks and MCP servers

<!-- TODO(project): document your harness wiring here, or delete this layer and drop agents.md from agents-md/root.yaml inputs. -->

## Session memories

<!-- TODO(project): document your harness wiring here, or delete this layer and drop agents.md from agents-md/root.yaml inputs. -->
