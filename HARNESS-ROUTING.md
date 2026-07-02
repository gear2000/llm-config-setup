# Harness skill routing

How composed skills land in each harness, and how to verify it on any machine.

## The goal — skill placement per harness

Skills route **by name prefix**:

| Skill kind            | Pi  | Claude Code | Codex |
|-----------------------|:---:|:-----------:|:-----:|
| `do-*` (workflow)     | ✅  | ❌          | ❌    |
| `cc-*` (Claude workflow) | ❌ | ✅       | ❌    |
| common (`qa`, `python`, `security`, …) | ✅ | ✅ | ✅ |

- `do-*` are Pi-only workflow commands.
- `cc-*` are their Claude Code counterparts (Claude-only).
- Everything else is common and goes everywhere the destination is configured for.

## Why it's built this way

Claude Code reads `<repo>/.claude/skills/` **directly** — so anything composed there is
visible to it. To keep `do-*` out of Claude, compose **moves `do-*` out of
`.claude/skills` into a Pi-only `<repo>/.pi-skills/` dir**. Pi and Codex read **global**
dirs (`~/.pi/agent/skills`, `~/.agents/skills`), not a repo-local dir, because Pi's
project-local `.pi/skills/` only loads *after a project is "trusted"* — which silently
hides skills. Global dirs have no such gate.

```
.shared-llm/layers ──compose──► classify each skill BY NAME
                                        │
              ┌─────────────────────────┼──────────────────────────┐
            do-*                       cc-*                       common
              │                         │                           │
              ▼                         ▼                    ┌───────┼────────┐
   <repo>/.pi-skills/         <repo>/.claude/skills/         ▼       ▼        ▼
   (moved out of .claude)      (Claude reads directly)   .claude/  Pi link  Codex link
              │                         │                  skills     │         │
       link ► ~/.pi/agent/skills        └── Claude Code ────┘         ▼         ▼
              │                                              ~/.pi/agent/  ~/.agents/
              ▼                                                 skills       skills
             Pi
```

## Set up on a machine

```sh
just init -o mac|ubuntu                    # prereq check: python3 + just
just configure -d /path/to/repo -l cc,pi   # register a destination (harnesses: cc,pi,codex)
just configure -g cc,pi                     # (optional) global/all-projects home skills
just update                                 # copy → compose → link (+ global)
```

`just update` is the one command you re-run whenever layers change. Add `-v` for
per-file detail; a full log is always written under `/tmp/.shared-llm/log/`.

## Verify placement

```sh
just check
```

Asserts the invariants and prints PASS/FAIL per harness:

```
PASS  Pi ~/.pi/agent/skills has NO cc-*
PASS  Pi ~/.pi/agent/skills has no broken links
PASS  [<repo>] .claude/skills has NO do-*
PASS  [<repo>] .pi-skills is do-* only
```

Then confirm in the harnesses themselves: launch `pi` in the repo (shows `do-*`, no
`cc-*`); open Claude Code in the repo (shows `cc-*`, no `do-*`).
