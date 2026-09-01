# llm-config-setup

A portable starter kit for assembling `CLAUDE.md`, `AGENTS.md`, skill files, and agent personas from composable markdown layers. One centralized engine reads a small config file, copies the reusable layers into each repo you register, composes the instruction files, and wires up the per-harness skill links — for every repo at once, from one command.

## License

SPDX-License-Identifier: GPL-3.0-or-later

This project is **free and open source** software, licensed under the [GNU General Public License v3.0](LICENSE) (GPL-3.0-or-later).

You may use, modify, and redistribute this code freely. If you distribute this software — or a modified version of it — you must also make the corresponding source available under the same GPL terms. That copyleft requirement keeps derivatives open: contributions and changes cannot be turned into proprietary closed-source distributions.

See [LICENSE](LICENSE) for the full legal text.

## What it does

LLM coding assistants (Claude Code, Codex, Pi, etc.) read instruction files — `CLAUDE.md`, `AGENTS.md`, skill `.md` files, agent persona `.md` files — to understand your project and to pick up specialized roles. Maintaining these by hand gets messy: duplicated prose, inconsistent tone, hard-to-update cross-cutting rules.

This kit treats those files as **build artifacts** assembled from **layers**:

- `.shared-llm/public/layers/` — the source prose, split into reusable layers.
- `.shared-llm/public/compose/` — YAML recipes that declare which layers to combine and where to write the output.
- `tools/harness.py` — the engine that reads recipes and writes output files, reconciles the skill symlinks, and runs the whole flow. It is the **one** engine, it lives **only** in this kit, and it is **never** copied into a repo you set up. (A per-repo copy used to drift out of sync and silently break Pi skill discovery; centralizing the engine is the core fix.)

You drive it with `just` against a single config file, `~/.shared-llm.yaml`, that lists your destinations. Register a repo once, then rebuild every registered repo with one `just update`.

## The command surface

```bash
just init -o mac|ubuntu          # one-time OS prereq check (python3 + just)
just configure -s ~/.shared-llm  # set the source hub (default; run once)
just configure -d /path/to/repo -l cc,pi   # register a destination repo + its harness list
just configure -g cc,pi          # set the GLOBAL (home / all-projects) harness list
just configure --offering-sets standard,claudex  # optional machine UpAgent roster
just descriptions                # audit owned discovery descriptions without writing files
just update                      # the headline command: copy → compose → link (+ global), every destination
just update -v                   # same, with per-file detail printed
just reset                       # heavy hammer: delete all kit-owned state, then rebuild via update
```

- **Harness tokens** are `cc` (Claude Code), `pi` (Pi), `codex` (Codex), and `cursor` (Cursor Agent CLI, `cursor-agent`). A destination's `-l` list defaults to `cc,pi`. `cursor` is a surface alias of `codex`: the Cursor Agent CLI reads the same root `AGENTS.md` and discovers skills from the same `~/.agents/skills/` home dir (plus `.claude/skills/` for compat), so it rides the codex plumbing end to end — no cursor-specific files are generated.
- `just descriptions` audits owned skill/agent descriptions without copying, composing, linking, writing the manifest, or touching home directories. Public-kit descriptions hard-fail above 1,024 UTF-16 code units; destination-owned descriptions are reported with anonymized destination indexes by default and remain warn-only until those external repositories complete their own `.shared-llm/this_repo/` migration. Use `just descriptions --enforce-destinations` (or `SHARED_LLM_ENFORCE_DEST_DESCRIPTIONS=1 just update`) after that follow-up to enable full-roster hard enforcement.
- `just update` always writes a full run log under `/tmp/.shared-llm/log/<timestamp>.log`; `-v` also prints the per-file detail to the terminal.
- **Building blocks** (`just update` runs them in order; each is also callable on its own): `just copy`, `just compose`, `just link`, and — when a `global:` list is set — `just global`.
- `just pi-extensions` installs the pinned third-party Pi extensions via the `pi` CLI (a network step; no-op if `pi` is absent).
- `just test` runs the composer/flow test suite.
- The Pi launch group — `just hub`, `just builder`, `just pi-status`, `just pi-clean` — and the terraform workflow recipes (`just tf-implement`, `just tf-approve`, `just tf-auto`, `just tf-reviewer-cc` / `-pi`) are covered under "Pi harness runtime config" below.

## The config file — `~/.shared-llm.yaml`

`just configure` maintains this file; you rarely edit it by hand. Its shape:

```yaml
source: ~/.shared-llm            # the local hub: kit common content is copied here, then to each dest
global: [cc, pi]                 # home / all-projects harnesses to set up (omit to skip the global step)
upagent:
  offering_sets: [standard]      # optional; omission is exactly the same safe default

destinations:
  - path: ~/project/repo/foo
    harnesses: [cc, pi]
    placeholders:                # optional: build-time fill for {{TOKEN}}s in kit-synced public/ layers
      PROJECT_NAME: Foo
  - path: ~/project/repo/bar
    harnesses: [cc, pi, codex]
    upagent:
      offering_sets: [standard]  # optional replacement, not a merge with machine policy
```

`just update` reads this file and runs every operation centrally against the paths it lists. Because the engine is never copied into a destination, it can never drift out of sync with a per-repo copy of itself.

### Optional UpAgent offering sets

UpAgent offering selection is a focused allowlist, not a generic YAML merge. The field may contain only code-approved set names. Omission means `[standard]`. A machine can opt in with `just configure --offering-sets standard,claudex`; `just configure -d /path/to/repo --offering-sets standard` gives that destination a replacement and removes ClaudeX there. Unknown, duplicate, empty, or malformed selections stop the update.

The engine generates one deterministic `offerings.yaml` for each destination and one under `~/.shared-llm/generated/extensions/common/upagent/`. YAML can select approved sets only. Offering ids, harness commands, executables, models, providers, effort lists, completion semantics, health identities, management candidates, and preflights remain code-owned. UpAgent resolves the roster from the repository where the request starts, the main checkout for a linked worktree, then the home runtime. A request `--cwd` only selects the worker directory; it does not replace the active roster with the target directory's destination policy. It does not silently fall back to another offering.

> **Skill placement per harness** (`do-*` → Pi, `cc-*` → Claude, common → both) and how to verify it on any machine: see **[HARNESS-ROUTING.md](HARNESS-ROUTING.md)**.

## The three operations

Every destination's `.shared-llm/` is split into two trees with an explicit ownership boundary (see [The destination split](#the-destination-split-public-vs-this_repo) below):

- **`public/`** — kit-synced. The engine sweeps it wholesale on every update.
- **`this_repo/`** — repo-owned. The engine never writes or prunes it.

`just update` is three operations (plus an optional fourth):

1. **copy** — kit → hub (`~/.shared-llm`) → each destination's **`public/`** tree. It copies the **common** layers and runtime, and syncs the kit's **compose recipes** into `public/compose/` (translating their layer paths into split form). It **never** touches a destination's `this_repo/` tree. `public/layers/` and `public/compose/` are **swept wholesale** — a file the kit no longer ships is pruned; `public/llm/` runtime is copy-overwrite (it keeps build artifacts like `node_modules`). Build artifacts the kit `.gitignore`s (e.g. a compiled hub binary) never propagate.
2. **compose** — each destination composes from **both** trees: `public/compose/` (kit recipes) first, then `this_repo/compose/` (repo-owned recipes). A recipe references layers across both trees by explicit path (`.shared-llm/public/...`, `.shared-llm/this_repo/...`). Composing public first lets a `this_repo/compose/` recipe **override** a kit recipe at the same output path (last writer wins). Any `{{TOKEN}}` in a `public/` layer is filled at build time from the destination's `placeholders:` map (see [Placeholder convention](#placeholder-convention)); an unfilled token stops the build. Outputs land at the destination's root (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/<name>/SKILL.md`, `.claude/agents/<name>.md`).
3. **link** — routes skills per harness **by name prefix**, then symlinks into each harness's **global** skill dir:
   - `do-*` → **Pi only**. During compose, `do-*` skills are moved out of `.claude/skills` (which Claude Code reads) into a Pi-only `<repo>/.pi-skills/` dir, so Claude never sees them; `link` then symlinks them into `~/.pi/agent/skills/`.
   - `cc-*` → **Claude Code only** (stays in `<repo>/.claude/skills/`, never linked to Pi/Codex).
   - everything else (**common**) → all harnesses (`.claude/skills` for Claude; symlinked into `~/.pi/agent/skills/` and `~/.agents/skills/` for Pi/Codex).

   Global dirs are used (not Pi's project-local `.pi/skills/`) because project-local skills load **only after a project is "trusted"** — that silently hid them. All destinations reconcile into each global dir together, so one never prunes another's links; a same-name collision across destinations warns (last wins). **Claude Code needs no link** — it reads `<repo>/.claude/` directly.
4. **global** (only when `global:` is set) — installs the home / all-projects pieces: the general home skills, the generic agents (roster in the Inventory section below), and the Pi + Claude runtime. See "The global step" below.

### Recipe path resolution — one rule

Every path in a compose recipe YAML — both `.shared-llm/...` inputs and any repo-relative path like `ops/...` — resolves against the **repo root**. There is no prefix-stripping and no dual-mode resolution: one rule, everywhere. The `public/` vs `this_repo/` split (below) is expressed **in the path itself** (`.shared-llm/public/...` or `.shared-llm/this_repo/...`), not by a forking resolver.

### The destination split — `public/` vs `this_repo/`

A registered destination's `.shared-llm/` is split into two trees with an explicit ownership boundary:

```
<dest>/.shared-llm/
  public/                 — KIT-SYNCED. Rebuilt from the kit on every `just update`.
    layers/               — the common layers (copied from the kit)
    compose/              — the kit's recipes (paths translated to split form)
    llm/{claude,common,pi}/common/  — the runtime trees (copy-overwrite, not pruned)
  this_repo/              — REPO-OWNED. The engine never writes or prunes here.
    layers/               — this_repo layer overlays (your filled-in stubs)
    compose/              — your own recipes (+ any kit recipe you override)
    llm/                  — this_repo runtime overlays (e.g. a settings overlay)
    prompts/  skills/  extensions/  — repo-owned prompts, standalone skills, tool modules
```

- **`public/` is disposable.** Never hand-edit it — the next `just update` overwrites it (and prunes anything the kit dropped). To change a kit-synced layer, edit it in the kit and re-run.
- **`this_repo/` is yours.** The engine reads it during compose but never writes it. Put project-specific layers, recipes, prompts, and skills here.
- **Overriding a kit recipe.** Drop a recipe at the *same relative path* under `this_repo/compose/` (e.g. `this_repo/compose/agents/backend.yaml`). Compose runs `public/` first, then `this_repo/`, so your copy wins at the shared output path. Its inputs may pull layers from **either** tree by explicit path.
- **Kit recipes carry `public/`-form paths.** The kit's own `.shared-llm/` is public-only, so a kit recipe's inputs already point at `.shared-llm/public/layers/...`. Syncing a kit recipe into a destination is a `public/`→`public/` identity copy; a recipe may also pull the destination's own `.shared-llm/this_repo/...` overlay by explicit path.

## The `.shared-llm/` layout

The layout below is the **kit's own source tree**, which lives entirely under `.shared-llm/public/` (`public/layers/`, `public/compose/`, `public/llm/`, `public/extensions/`) — the kit is public-only. A *registered destination's* `.shared-llm/` adds a second `this_repo/` tree alongside `public/` (the split described in [The destination split](#the-destination-split-public-vs-this_repo)) — the engine copies the kit's `public/` content into the destination's `public/` (an identity copy) and keeps the repo's own content under `this_repo/`.

The repo splits into the **source tree** (`.shared-llm/`) and the **engine + config machinery** (`tools/`, `justfile`).

```
.shared-llm/
  public/                         — the kit's ENTIRE source tree (public-only; a destination adds this_repo/ alongside)
    layers/                       — SOURCE prose, split into reusable layers
      llm/                        — layers for CLAUDE.md / AGENTS.md
        common/common/response.md — portable response-format rules (ready as-is)
        this_repo/                — project-specific layers, shipped as TEMPLATE.* stubs
          common/TEMPLATE.general.md            — project identity, conventions, CI, credentials
          common/TEMPLATE.authoring.md          — example: CLAUDE.md for a special subdirectory
          common/TEMPLATE.aws-execution-engine.md — example: CLAUDE.md for a component with a contract
          common/src/TEMPLATE.packages.md       — shared context for all package CLAUDE.mds
          common/src/TEMPLATE.services.md       — shared context for all service CLAUDE.mds
          common/packages/TEMPLATE.example_package.md — per-package leaf template
          common/services/TEMPLATE.example_service.md — per-service leaf template
          claude/TEMPLATE.claude.md             — Claude Code-specific conventions (worktrees, planning)
          codex/TEMPLATE.agents.md              — cross-harness wiring (skills, hooks, MCP)
      skills/                     — layers for skill files
        common/python/            — language conventions, ready as-is (practices.md + description.md)
        common/nextjs/            — ready as-is
        common/backend/           — ready as-is
        this_repo/TEMPLATE.python.md — project-specific Python skill layer (stub)
      agents/                     — layers for agent personas
        common/<name>.md          — the generic agent bodies (ready as-is, brand-free; see Inventory)
        common/<name>.description.md — one-line description for each agent's frontmatter
    compose/                      — RECIPES: which layers to combine, and where to write
      claude-md/root.yaml         — recipe: root CLAUDE.md
      claude-md/example-package.yaml — recipe: per-package CLAUDE.md
      claude-md/example-service.yaml — recipe: per-service CLAUDE.md
      agents-md/root.yaml         — recipe: root AGENTS.md
      skills/python.yaml          — recipe: per-repo python skill (general layer + this_repo layer)
      global/python.yaml          — recipe: GENERAL python skill (no this_repo layer; global step)
      global/nextjs.yaml          — recipe: general nextjs skill (global step)
      global/backend.yaml         — recipe: general backend skill (global step)
      slash-commands/             — recipes: the routed slash-command skills
      agents/<name>.yaml          — recipe: one generic agent persona (roster count in Inventory)
    llm/pi/common/                — Pi harness runtime config (NOT a compose input; see below)
    llm/claude/common/            — Claude harness runtime config (NOT a compose input; see below)
    extensions/common/upagent/    — UpAgent runtime plus code-owned offering-set renderer
      offerings.d/                — approved `standard` and optional `claudex` declarations
      offerings-management.yaml   — fixed lifecycle candidates, separate from set selection
    extensions/this_repo/         — tool-module extensions (pi-hub, tf); justfile-imported
tools/
  harness.py                      — the ONE engine: compose + config-driven copy/compose/link/global
  install-pi-extensions.sh        — `pi install` helper for the pinned third-party extensions
justfile                          — init / configure / update, the building blocks, tests, and the Pi launch group
ONBOARDING.md                     — token-by-token checklist for filling the TEMPLATE stubs
```

**layers vs compose vs llm/pi:**

- **layers** are pure source prose — never generated, never frontmatter. The portable `common/` layers ship ready-to-use; the `this_repo/` layers ship as `TEMPLATE.*` stubs you fill in.
- **compose** recipes are the build instructions: each YAML names the input layers, the output path, and (for skills/agents) the frontmatter name + description. The engine concatenates the inputs and writes the output. Every path in a recipe resolves against the repo root.
- **llm/pi** and **llm/claude** are runtime config for the coding agents (Pi extensions and agent personas; Claude hooks, statusline, settings). They are **not** compose inputs — their files are never concatenated into any output. The **global** step copies both trees into `~/.shared-llm/generated/` (executable bits preserved) and symlinks them from there into `~/.pi/` and `~/.claude/` (reconciling: create missing, re-point drifted, prune renamed/deleted), recording every deployed path in `~/.shared-llm/manifest.json`. See "Pi harness runtime config" and "Claude harness runtime config" below.

## Setting up a destination repo

There is no scaffolding command — a destination is set up by hand once, then driven by `just update` forever after:

1. **Seed the repo-owned tree.** Copy the kit's `this_repo/` layer stubs under `<repo>/.shared-llm/this_repo/layers/` (mirroring the kit's `layers/*/this_repo/` structure) and any repo-owned recipes under `<repo>/.shared-llm/this_repo/compose/`. They arrive as fillable `TEMPLATE.*` stubs. You do **not** create `public/` — the engine builds it in step 4.
2. **Fill the `TEMPLATE.*` stubs** (see `ONBOARDING.md`), deleting the `TEMPLATE.` prefix from each as you finish it.
3. **Register it:** `just configure -d /path/to/repo -l cc,pi` (add a `placeholders:` map to its entry in `~/.shared-llm.yaml` if any kit-synced layer carries a `{{TOKEN}}` — see [Placeholder convention](#placeholder-convention)).
4. **Build it:** `just update` — this creates the `public/` tree from the kit and composes the outputs.
5. **Import the tool-module justfiles** you intend to use into the repo's root justfile — starting with `import '.shared-llm/public/extensions/common/upagent/justfile'`, which the `/upagent-pipeline` skill needs. `just update` copies the modules in but does not wire them up; see `ONBOARDING.md` step 3.

From then on, `just update` keeps every registered destination in sync: it rebuilds `public/` from the kit (your `this_repo/` tree is never touched), recomposes, and re-links. The generated `CLAUDE.md`, `AGENTS.md`, skill, and agent files land at the repo root, ready to commit.

## Troubleshooting — `just update` succeeds but a repo stays stale

`just update` only ever writes to the paths listed in `~/.shared-llm.yaml` — and that file is **per-machine**. When a repo keeps running an old `.shared-llm` runtime even though `just update` reports success, the update machinery is almost never the problem; the destination list is. Work through these scenarios in order.

First, on the affected machine, confirm what the engine is actually targeting:

```bash
cat ~/.shared-llm.yaml          # what does this machine think the destinations are?
just update -v                  # watch which paths it writes
```

### Scenario 1 — the destination path points at the wrong copy of the repo

The config lists a stale path: an archived copy, a moved/renamed checkout, or a parent/container folder instead of the exact repo root where you run the commands. `just update` faithfully refreshes that other folder, while the repo you actually work in either keeps a stale `.shared-llm/` or (if it was never registered under its real path) has none at all.

```
~/.shared-llm.yaml
└── destinations:
    └── ~/repos/archive/old-copy/foo    ← updated forever, used by nobody

~/repos/foo/                            ← the live repo you run `just <cmd>` in
└── .shared-llm/                        ✗ stale or missing — never targeted
```

This is especially easy to hit when moving between machines: absolute paths differ, and a path that was right on one machine can point at the wrong level (or a dead copy) on another.

**Diagnose:** in the affected repo, check where its commands resolve (`git rev-parse --show-toplevel`, `just --list`), then compare that root against every `path:` in `~/.shared-llm.yaml`. If no entry matches it exactly, this is your scenario.

**Fix:** edit the entry's `path:` to the exact repo root that runs the commands (keep its `harnesses:` and `placeholders:`), remove or correct the stale entry, then `just update -v`. Verify the repo-local `.shared-llm/public/` tree now contains the expected new files.

### Scenario 2 — the repo was never registered on this machine

`~/.shared-llm.yaml` does not travel with the kit or the repo. A repo registered on machine A is invisible to machine B until you register it there too.

**Fix:** on the affected machine, `just configure -d /path/to/repo -l cc,pi` (add the `placeholders:` map its layers need), then `just update`.

### Scenario 3 — the kit checkout itself is behind

The engine copies from the kit's working tree. If you `git pull`ed the kit on one machine but not this one, `just update` here dutifully deploys the old content everywhere.

**Fix:** `git pull` in the kit repo, then `just update`.

### The heavy hammer — `just reset`

If the config is verified correct and a repo still looks wrong (half-copied state, a runtime dir corrupted by hand-edits, drift you can't explain), `just reset` deletes all kit-owned state and rebuilds it from scratch:

- removes the hub's kit-synced content and every destination's `.shared-llm/public/` tree;
- never touches a destination's `this_repo/` tree, and never `~/.shared-llm/generated/` or `manifest.json` (home symlinks point into those — the rebuild reconciles them in place);
- then runs the full `update` flow, so everything kit-owned is regenerated fresh.

It is safe to run any time — everything it deletes is a build product the next update recreates.

### Verifying the fix

In the affected repo, after `just update -v`:

- the repo-local `.shared-llm/public/` tree contains the new files you expected (grep for a symbol you know shipped recently);
- `just --list` resolves the tool-module recipes from the repo-local runtime;
- `git status` shows no surprises — `.shared-llm/` is typically gitignored, and only the composed outputs (`CLAUDE.md`, skills, agents) change as tracked files.

## Where compose outputs land

**Recipe `output:` paths are root-relative**, and they resolve against the destination's root. So a real destination's generated files land exactly where each harness reads them — `CLAUDE.md` and `AGENTS.md` at the root, `.claude/skills/<name>/SKILL.md`, `.claude/agents/<name>.md` — with no manual move.

A destination composes only the **consumer-relevant** recipe groups (root `CLAUDE.md`/`AGENTS.md`, the skills, the agents, the slash commands). It deliberately does **not** compose the home-only `global/` skills (those install into `~/` via the global step) or the `example-package` / `example-service` **demo** recipes (illustrative samples only — a consumer repo never gets a stray `src/packages/example_package/CLAUDE.md`).

**The kit never clobbers its own files when it composes itself.** This repo ships its `this_repo` layers as `TEMPLATE.*` stubs and keeps a hand-maintained `CLAUDE.md` / `README`. When the global step composes the kit's own home skills and agents, it stages them under `examples/` — a **gitignored** staging dir — before copying them into `$HOME`, so a self-compose never overwrites the kit's real root files. The `example-package` / `example-service` demo recipes compose under a gitignored `samples/` area and are excluded from every consumer install.

## The global step — home skills + runtime

When `~/.shared-llm.yaml` has a `global:` list, `just update` (or `just global` on its own) installs the pieces that live in `$HOME` and apply across every project. Each is foreign-safe: it never clobbers a divergent or foreign file, leaving it untouched with a warning.

1. **General home skills** — composes the `global/` recipes (`python`, `nextjs`, `backend`, `golang`, `herdr`, `clickhouse`, `kafka`, `lucidchart`, `drawio`, `create-html`) and the routed slash-command skills, syncs each into the durable per-machine generated tree (`~/.shared-llm/generated/skills/`), and **symlinks** it into the home skill dir every wanted harness reads: `~/.claude/skills/`, `~/.pi/agent/skills/`, `~/.agents/skills` (Codex). `readlink` on any home skill answers "generated or handwritten"; a byte-identical pre-existing copy from the old copy mechanism is upgraded to a link in place. The workflow-suite commands are `/do-plan`, `/do-implement`, `/do-convert`, and `/do-full` on Pi, with matching `/cc-plan`, `/cc-implement`, `/cc-convert`, and `/cc-full` commands on Claude Code. Legacy planish / plan-and-grill / meta names are one-release warning aliases.
2. **The generic agents** — composes the `agents/` recipes (roster and count in the Inventory section), syncs each persona into `~/.shared-llm/generated/agents/`, and symlinks it into the home agent dirs: `~/.claude/agents/` and `~/.pi/agent/agents/`. Codex has no user-agent directory, so agents skip it — the engine never invents one.
3. **Pi runtime** — copies the bundled Pi extensions + agent personas into `~/.shared-llm/generated/` and symlinks them from there into `~/.pi/` (reconciling: create / re-point / prune), and scaffolds `~/.pi/agent/settings.json` from the template only if absent.
4. **Claude runtime** — copies the generic hooks and the statusline into the generated tree and links them into `~/.claude/hooks/` and `~/.claude/statusline.sh`, and scaffolds `~/.claude/settings.json` from `settings.template.json` only if absent — settings stays a **real file** on purpose: Claude Code mutates it at runtime, and a rename-style save would silently replace a symlink.
5. **Herdr config** — deploys `herdr-config.toml` the same way, as a generated copy linked at `~/.config/herdr/config.toml`: creates/repoints links managed by this kit and leaves a foreign real file or link untouched with a loud warning.
6. **UpAgent offering roster** — always materializes the machine-selected, code-validated roster at `~/.shared-llm/generated/extensions/common/upagent/offerings.yaml`, even when no global harness is selected, so repository-independent calls have the same explicit machine policy.

Home links always point into `~/.shared-llm/generated/`, never into the kit checkout, so moving or deleting the repository never breaks a live runtime.

Every path the global step deploys is recorded in `~/.shared-llm/manifest.json`. On the next run, a manifest entry whose recipe no longer produces it is **pruned** — but only when the deployed path is a symlink provably ours (one resolving into the generated tree). Scaffolded mutable settings stay real files and are **retained**: the engine deletes no real file, and drift in one is logged, never acted on. `just prune` runs the same reconciliation on demand. Composed `CLAUDE.md` / `AGENTS.md` in destination repos stay real committed files (portable to clones without the kit) and carry a `GENERATED by llm-config-setup` header instead.

Third-party Pi extensions are a separate network step — `just pi-extensions`. It reconciles the pinned manifest through the Pi CLI (installs missing entries and removes the one explicitly retired package) but deliberately leaves unrelated user-installed packages untouched.

### Pi global-vs-repo skill precedence

Pi loads a **global** skill before a repo-local one of the same name and keeps the first it finds. The **link** step prunes the stale global Pi links it created in earlier versions (so an obsolete global link can't shadow a new repo-local one), and **warns** when a repo-local skill name still collides with a global one it does not own — it surfaces the collision but does not police it.

### Reusable specialist skills and agents

The global step also installs reusable ClickHouse, Kafka, Lucidchart, Draw.io, and Create HTML skills and matching generic agents. Claude Code and Pi receive both global skills and agent personas. Codex/`cursor` receive the global skills through `~/.agents/skills/`; this kit does not create a Codex agent-persona directory, so UpAgent is the routed way to use the matching specialist persona. Lucidchart authentication belongs in user-local harness settings (OAuth MCP for interactive use, user-local API/MCP credentials for unattended use). Draw.io output uses semantic `.drawio` XML validation before delivery. Create HTML owns static presentation artifacts; frontend applications and browser-only automation stay with `frontend` and `playwright-cli`.

## Inventory

The skill, agent, and slash-command counts and tables below are **derived**, not hand-typed — `tools/gen_inventory.py` reads them straight from the compose recipes (`.shared-llm/public/compose/{skills,agents,slash-commands}/**/*.yaml`) and rewrites the block between the markers. Regenerate after adding, removing, or renaming a recipe:

```bash
just inventory
```

Re-running against unchanged recipes is a zero diff (idempotent) — safe to run any time, and safe to forget to run, since nothing here is hand-maintained.

<!-- BEGIN:inventory -->
<!-- GENERATED by tools/gen_inventory.py (`just inventory`) — do not hand-edit. Edit a compose recipe under .shared-llm/public/compose/{skills,agents,slash-commands}/ and re-run. -->

### Skills (3)

Per-repo skills — composed into a destination's `.claude/skills/<name>/SKILL.md` from a general layer plus an optional `this_repo` overlay.

| Name | Description |
| --- | --- |
| `golang` | General Go conventions. Use when writing, reviewing, or scaffolding Go code — covers core idioms,… |
| `python` | General Python conventions. Use when writing, reviewing, or scaffolding Python code — covers modern… |
| `update-shared-llm` | Update shared-llm skills, agents, CLAUDE.md/AGENTS.md rules, or layers. Edit source layers/recipes,… |

### Generic agents (36)

Brand-free agent personas the global step copies into `~/.claude/agents/` and `~/.pi/agent/agents/`. Adopt them as-is — they contain no project-specific references.

| Name | Description |
| --- | --- |
| `adversarial-evaluator` | Mandatory end-of-phase adversarial gate for a completed plan phase. Reviews finished work against… |
| `architecture` | Use when designing new modules, defining package boundaries, making architectural decisions, or… |
| `aws` | AWS infrastructure and operations specialist for architecture, IAM, networking, MSK provisioning,… |
| `backend` | Use for backend services, APIs, worker handlers, tests, and fail-loud error handling. Kafka client… |
| `ci-pipeline` | Use when creating or modifying CI pipeline configurations. Writes configs, validates syntax, and… |
| `ci-trigger` | Use when creating or managing trigger jobs on a dashboard that fronts a separate CI system,… |
| `clickhouse` | ClickHouse specialist for analytics schemas, ingestion, query tuning, replication, backup/restore,… |
| `code-review` | Use to review code written by other agents or by hand for quality, modularity, consistency, and… |
| `create-html` | Create HTML specialist for self-contained static presentation artifacts, inline assets, responsive… |
| `database` | Use for relational/PostgreSQL schema design, migrations, RLS, SQL, and data-access layers.… |
| `deployer` | Dedicated deployment, sync, and live-verification specialist: triggers deploy jobs, reads logs,… |
| `devops` | Use for infrastructure-as-code, cloud infrastructure (IAM, serverless functions, object storage),… |
| `docs-writer` | Use when writing or updating documentation pages. Reads current code to verify accuracy, writes and… |
| `drawio` | Draw.io specialist for native .drawio XML authoring/editing, semantic validation, preservation, and… |
| `frontend` | Use when building or modifying the frontend — pages, components, auth flows, or data fetching. |
| `github` | GitHub specialist for repository state, issues, pull requests, Actions, and evidence-backed… |
| `intake-clerk` | Normalizes imperfect broker envelopes without inventing or changing task, addressee, or execution… |
| `kafka` | Kafka specialist for topics, partitions, producers, consumers, delivery semantics, Connect/Streams,… |
| `lucidchart` | Lucidchart specialist for MCP-backed diagram creation/inspection, Standard Import fallback, setup… |
| `monorepo-pkgs` | Read-only governance auditor for Python monorepo packages: scaffolding, best practices, utility… |
| `monorepo-python` | Project-specific Python agent for a monorepo. Use when working on Python packages or deployable… |
| `phase-evaluator` | Optional independent evaluator for one plan phase. Reviews durable stage evidence from a fresh… |
| `plan-adversary` | Read-only adversarial reviewer for approved candidate plans. Challenges feasibility, missing… |
| `plan-watchdog` | Optional plan-phase conformance advisor. Reviews durable evidence for a managed phase and returns… |
| `planner` | Writes one small tracer-bullet implementation plan for a single issue from its issue and research… |
| `playwright-cli` | Use for end-to-end browser tests and interactive browser-driving sessions. Static HTML creation… |
| `qa` | Use to run test suites, validate behavior, perform regression checks, and verify end-to-end flows.… |
| `researcher` | Bounded read-only investigator for one issue. Reads the issue inside the given worktree and writes… |
| `reviewer` | Independent read-only reviewer for code, infrastructure, plans, and durable execution evidence. |
| `security` | Use when implementing auth flows, access-control policies, secrets management, or auditing security… |
| `team-pulse` | Mechanical result watcher for a plan run. Polls one durable result path and alerts its assigned… |
| `terraform` | Terraform infrastructure specialist that writes and validates IaC, produces plans, and gates… |
| `upagent-account-manager` | Dedicated LLM lifecycle owner for one UpAgent request; validates configuration and explains… |
| `upagent-checker` | Short-lived advisory observer that interprets one bounded UpAgent pane/process/result evidence… |
| `upagent-rescuer` | Short-lived advisory salvage assessor hired only for contradictory evidence about a vanished… |
| `upagent-sentinel` | Per-request UpAgent supervision pane, duty-bound to exactly one worker from liftoff to closeout;… |

### Slash-command skills (33)

Routed slash-command skills — `do-*` symlinks to Pi only, `cc-*` stays Claude-only. `cc/do-plan`, `cc/do-implement`, and `cc/do-convert --herdr` are the primary workflow surface; old planish/plan-and-grill/meta names are one-release aliases. Other common skills ship to every configured harness. Composed into a destination's `.claude/skills/<name>/SKILL.md`.

| Name | Description |
| --- | --- |
| `blast-radius` | Find what a change could break somewhere else before it ships, beyond the diff, and prove the one… |
| `cc-convert` | Claude Code converter: `/cc-convert --herdr <plan.md>` idempotently decomposes an approved big plan… |
| `cc-full` | Phone-friendly Claude Code composer: run `/cc-plan` exactly once, then either `/cc-implement` once… |
| `cc-implement` | Claude Code direct implementation: implement an approved `plan.md` in one fresh interactive TUI… |
| `cc-plan` | Claude Code planning front door: research, conditionally resolve design, grill with Planish, run… |
| `cc-plan-and-grill` | Deprecated alias for `/cc-plan`. Warns, then delegates to the new Claude Code planning front door;… |
| `cc-planish` | Deprecated alias for `/cc-plan`. Warns, then delegates; Planish remains the visual grill renderer… |
| `cc-research` | Claude Code research only: produce research.md, with no plan or implementation. Use fresh Explore… |
| `codex-delegate` | Delegate a routine coding, refactor, investigation, or review slice to Codex CLI as a peer… |
| `do-convert` | Pi converter: `/do-convert --herdr <plan.md>` idempotently decomposes an approved big plan into… |
| `do-full` | Phone-friendly Pi composer: run `/do-plan` exactly once, then either `/do-implement` once for… |
| `do-implement` | Pi direct implementation: implement an approved `plan.md` in one fresh interactive TUI path. It… |
| `do-plan` | Pi planning front door: research, conditionally resolve design, grill with Planish, run exactly two… |
| `do-plan-and-grill` | Deprecated alias for `/do-plan`. Warns, then delegates to the new Pi planning front door; it no… |
| `do-research` | Pi research only: produce research.md, with no plan or implementation. Use fresh Explore agents by… |
| `fail-loud` | Cross-language rule against silent failure. Apply when writing or reviewing any error handling —… |
| `grill-me` | Interview the user about a plan or design until shared understanding is reached. Use frontier… |
| `phase-leader` | Run one canonical plan phase in a cockpit pane under HERDR_ENV=1. Validates route safety, places… |
| `plain-speech` | How to talk to the user: repo ubiquitous language from CONTEXT.md, Simplified Technical English… |
| `playwright-cli` | Automates browser interactions for testing, forms, screenshots, and extraction. Static HTML… |
| `prd-to-plan` | Turn a PRD into a multi-phase implementation plan using tracer-bullet vertical slices, saved under… |
| `qa` | Interactive QA session where the user reports bugs conversationally. Clarifies, explores for… |
| `security` | Generic security best practices. Use when implementing auth flows, secrets management, IAM… |
| `tui-control` | Internal TUI plan controller for a checked plan.md and route.yaml under HERDR_ENV=1. Runs phase… |
| `unslop` | Cut AI tells from any writing and add human voice. Apply to every user-facing document, report,… |
| `upagent-cancel` | Authenticate and cancel one active UpAgent request at any lifecycle point using its existing… |
| `upagent-cleanup` | Dry-run or explicitly apply terminal-only UpAgent history pruning while retaining auditable,… |
| `upagent-get` | Read one UpAgent request's state, retained result, receipt, and typed artifact pointers, including… |
| `upagent-ls` | List active, terminal, or all requests known to the canonical machine-local UpAgent Recruiter… |
| `upagent-pipeline` | Run one issue-sized change end to end through a registry pipeline: optional research and plan… |
| `upagent-run` | Run one UpAgent task, anchored to your verified live caller pane when invoked inside Herdr, with an… |
| `wait-what` | Stop. That last message did not land: re-pitch it in plain speech. |
| `writing-for-agents` | Writing documents for agents: concise routing pointers, progressive disclosure, completion… |
<!-- END:inventory -->

## The compose engine

Under the config-driven surface, `tools/harness.py` has a low-level `compose` subcommand that reads a single recipe YAML (or a directory of them) and writes the output. `just update` uses it per destination; the tests and the global staging compose call it directly. It decouples two roots:

- **source** — the `.shared-llm/` dir (layers + recipes). Inputs and descriptions resolve against it. Selected via `--shared-llm`, then `$SHARED_LLM_DIR`, then a walk-up for `.shared-llm/`.
- **target** — the output base where each recipe's `output:` path lands. Selected via `--target`, default the current directory.

Recipe types: `skill` (frontmatter name + description), `agent` (name + description + model, optional color), `claude-md` / `agents-md` (plain concatenated markdown, no frontmatter), `prompt` (a whole feature prompt assembled from an explicit manifest), `copy` (one file copied verbatim, executable bit preserved — for hook scripts and the statusline), and `settings` (JSON inputs deep-merged: dicts recurse, lists concatenate, scalars overlay-win — for a `settings.json` base plus a this_repo overlay). A recipe may also declare a `catalog:` partial injected before its `inputs`.

```bash
# low-level, mostly for tests / manual staging (the everyday path is `just update`):
python3 tools/harness.py compose .shared-llm/public/compose/agents/backend.yaml --target /tmp/out
python3 tools/harness.py compose .shared-llm/public/compose/agents --target /tmp/out   # a SUBSET (a recipe dir)
```

## Quickstart

Want an LLM to drive setup safely? After cloning this kit, give it [INSTALL-PROMPT.md](INSTALL-PROMPT.md); it distinguishes the checkout from the generated source hub, asks before destination edits, and verifies two idempotent updates.

```bash
# 1. Python 3 + one dependency for the engine
python3 -m pip install pyyaml

# 2. Check prerequisites (python3 + just)
just init -o ubuntu        # or: just init -o mac

# 3. Point the engine at the source hub, and set the global harness list
just configure -s ~/.shared-llm
just configure -g cc,pi

# 4. Set up a destination repo: copy .shared-llm/ into it, fill the TEMPLATE.* stubs
#    (see ONBOARDING.md), then register it
just configure -d /path/to/your/repo -l cc,pi

# 5. Build everything — every destination, plus the global home pieces
just update
```

## Placeholder convention

The `this_repo` stubs use two marker styles:

- **Block-level guidance** — HTML comment: `<!-- TODO(project): explain what goes here -->`
- **Inline fill-in values** — double-brace token: `{{PROJECT_NAME}}`, `{{CRED_ROOT}}`, etc. The token shape is `{{UPPERCASE_UNDERSCORE}}` (all-caps letters, digits, underscores) — a lowercase or spaced `{{ ... }}` in an example code fence is left alone.

There are **two** ways a `{{TOKEN}}` gets its value:

1. **You fill the `this_repo` stub by hand** (the classic path). Replace the token in the layer prose, drop the `TEMPLATE.` prefix. This is for tokens that belong to layers *you* own under `this_repo/`. When done, `grep -rn '{{\|TODO(project)' .shared-llm/this_repo/` and `find . -name 'TEMPLATE.*'` both return nothing.
2. **The engine fills it at build time** from a per-destination `placeholders:` map — for tokens carried by a **kit-synced `public/` layer** (a shared layer that needs a per-repo value). Add the map to the destination's entry in `~/.shared-llm.yaml`:

   ```yaml
   destinations:
     - path: ~/project/repo/foo
       harnesses: [cc, pi]
       placeholders:
         PROJECT_NAME: Foo
         CRED_ROOT: ~/project/foo/secrets
   ```

   During compose, each `{{TOKEN}}` in a composed output is replaced from this map. **Any unfilled `{{TOKEN}}` in composed output stops the build** with a clear error naming the token and the file — kit-synced layers can therefore safely ship a placeholder, because a destination that forgets to supply the value fails loud instead of shipping a literal `{{TOKEN}}`. A recipe that pulls a `TEMPLATE.*` stub is exempt (a stub is deliberately unfilled). Placeholder **values live only in `~/.shared-llm.yaml`** (your home config), never in a committed layer.

See `ONBOARDING.md` for the ordered, token-by-token fill checklist.

## Claude harness runtime config

`.shared-llm/public/llm/claude/common/` holds Claude Code-specific runtime config —
hooks, the statusline, and a home settings template. Like the Pi runtime config,
these are **not composed into prose**; the global step of `just update` copies
them into `~/.shared-llm/generated/` (executable bits preserved) and links them
at the home level so they apply to every project — the home links resolve into
the generated tree, never into this checkout:

- **`hooks/*.sh`** — four generic, project-agnostic quality hooks (prettier
  format, TypeScript check, console.log warn/audit). Linked into `~/.claude/hooks/`
  and wired in the settings template. They **self-gate by file type** — a
  TypeScript hook no-ops in a Python repo — so firing in every project is safe.
- **`statusline.sh`** — a dependency-free statusline (dir, branch, model, context
  bar). Linked at `~/.claude/statusline.sh`.
- **`settings.template.json`** — general Claude Code settings (statusline pointer,
  agent-teams flag, the generic hook wiring, `permissions.defaultMode: acceptEdits`,
  plugins). Scaffolded to `~/.claude/settings.json` **only when absent**, and as
  a **real file, never a link** — Claude Code mutates it at runtime, and a re-run
  never overwrites your per-machine tweaks. Bump `defaultMode` or add
  personal permissions in `~/.claude/settings.local.json` (gitignored), which
  Claude Code merges on top.

For a repo that needs its **own** project-specific hooks, two optional
`TEMPLATE.*` stubs ship under `.shared-llm/public/llm/claude/this_repo/`
(`TEMPLATE.example-hook.py`, `TEMPLATE.settings-overlay.json`) — fill them in and
merge the overlay onto the base with a `type: settings` recipe, or delete them.

This is the machinery behind the two runtime compose types: **`type: copy`** (a file
copied verbatim, executable bit preserved) and **`type: settings`** (JSON inputs
deep-merged — dicts recurse, hook arrays concatenate, scalars overlay-win).

## Pi harness runtime config (optional)

`.shared-llm/public/llm/pi/common/` holds runtime config for the [Pi coding agent](https://github.com/earendil-works/pi-mono). This directory is **not a compose input** — its contents are never concatenated into `CLAUDE.md` or `AGENTS.md`. Instead, the global step of `just update` copies them into `~/.shared-llm/generated/` (executable bits preserved) and symlinks them from there into `~/.pi/` (reconciling: it refreshes the copies, creates missing links, re-points drifted ones, and prunes links whose source was renamed or deleted) so Pi can discover and load them at runtime. Every deployed path is recorded in `~/.shared-llm/manifest.json`, and no home link ever resolves into this checkout.

**What it contains:**

- `extensions/do-planish.ts` — The Pi Planish runtime. It registers the browser-backed `planish_grill` and `planish_submit_plan` tools used by `/do-plan` for annotation-only planning pages: sticky notes → Copy Feedback → paste the block back into the TUI. The legacy `/do-planish` slash command is a one-release warning alias to `/do-plan`. URLs honor the `work_log.host` field of `.shared-llm.yaml` (remote/Tailscale sessions).
- `extensions/context-workflow.ts` — A Pi extension that wraps a structured write→test→review→fix→verify loop. Symlinked into `~/.pi/agent/extensions/` (auto-loaded by the Pi agent on startup).
- `extensions/auto-compact.ts` — A model-relative context policy for Pi's long-lived TUI and RPC modes. After each `agent_settled` event, it reads Pi's authoritative active-model usage and triggers Pi's native compaction at 50% of the actual context window; print and JSON modes are deliberately excluded because their runtimes dispose immediately after a prompt settles. It does not replace Pi's summarizer: custom instructions focus the native summary on goals, constraints, decisions with rationale, evidence pointers, failed approaches, errors, and next steps. The policy re-arms only after fresh post-compaction usage falls below 50%, uses bounded exponential backoff, reports non-UI errors to stderr, invalidates stale callbacks on session/model changes, and exposes `/auto-compact-status` for inspection. Pi's built-in near-overflow compaction remains enabled as a second safety net.
- `extensions/disable-amazon-bedrock.ts` - A provider policy that replaces Pi's built-in Amazon Bedrock catalog with an empty model list. Bedrock is absent from model selection, explicit Bedrock selection fails as an unknown provider, and bare Claude aliases resolve only through direct Anthropic rather than silently selecting Bedrock.
- `extensions/memsearch/` — A directory Pi extension (`index.ts` + `collection.ts`) that gives Pi memory over the **same shared store** Claude Code builds: per-project daily markdown logs under `<git-root>/.memsearch/memory/<YYYY-MM-DD>.md`, indexed into a per-project Milvus collection. The collection name is derived to **exactly match** memsearch's `derive-collection.sh`, so Pi and Claude converge on the same collection per repo.
  - **Recall — full parity.** Same shared store, same `memsearch` CLI Claude uses: a model-callable `memory_search` tool + `/recall <query>` return ranked hits from past sessions, `memory_expand` + `/recall-expand <hash>` open the full section, and on `session_start` it injects a one-line "memory available" hint.
  - **Capture — deliberately NOT full parity.** On `agent_end` it writes **deterministic** third-person notes (what the user asked, which tools the agent used, a clipped agent reply) — **lighter than Claude's and the memsearch codex reference plugin's default**, which run an LLM to summarize the turn. The deterministic path is chosen so capture never makes a blocking nested LLM call. It appends the notes (with a `<!-- session: turn: transcript: -->` anchor) to today's daily log synchronously (the markdown is the source of truth), then runs `memsearch index` in a detached child. If the Milvus Lite single-writer lock is contended (Claude indexing the same store at the same time), the child retries that condition only; on exhaustion it **fails loud** — writes to `~/.pi/agent/memsearch-index.log` and drops an `<!-- index-deferred: <ISO> reason=lock -->` breadcrumb in the daily md — and the next `session_start` re-index catches it up. Non-lock errors fail loud immediately. Nothing is lost because the markdown is already written.
  - Symlinked as a directory into `~/.pi/agent/extensions/` (auto-loaded; Pi loads `index.ts`, with `collection.ts` riding along as an imported helper). `collection.ts` holds just the derivation (Node built-ins only) so it can be unit-tested (`memsearch.test.ts`, golden value `ms_my_project_a26ceb5d` for the example path `/home/user/code/my-project`). Needs the `memsearch` CLI on `PATH` or `uvx` (ONNX embeddings, no API key); if neither is present it no-ops silently.
- `extensions/codex-reviewer-hub.ts`, `extensions/doc-review-hub.ts`, `extensions/pr-review-hub.ts` — Pi extensions that each listen on a Unix socket and dispatch a sub-agent request: adversarial code review, document review, and PR/branch review respectively. `extensions/hub-common.ts` is the shared socket/dispatch helper they import. The hub extensions are symlinked into `~/.pi/extensions/` (loaded when Pi is launched with `-e`).
- `agents/codex-reviewer.md`, `agents/doc-reviewer.md`, `agents/pr-reviewer.md` — The system prompts for the review agents invoked by the hub extensions. Symlinked into `~/.pi/agent/agents/`.
- `third-party-extensions.txt` — Pinned manifest of **third-party** Pi extensions, one `pi install` source per line. `tools/install-pi-extensions.sh` reads it and installs each via `pi install` (skipping any already present), so the set reproduces on any machine with one command. `just pi-extensions` runs that installer.
- `THIRD-PARTY-EXTENSIONS.md` — The per-extension reference: what each one does, its runtime deps, and the own-vs-third-party rule below.
- `settings.template.json` — A starter Pi settings file (provider, model, thinking level). Scaffolded to `~/.pi/agent/settings.json` as a real file — never a link — and only if that file does not already exist, so your live settings are never overwritten. Its `packages` array starts empty — the installer fills it.

### Own vs third-party extensions — keep them separate

Two kinds of Pi extension, two install paths. **Do not mix them.**

- **OWN** (the `.ts` files authored in `extensions/` above) are **copied into `~/.shared-llm/generated/` and symlinked from there** into `~/.pi/` by the global step of `just update` — never installed from a registry. The same generated-copy-plus-manifest ownership model deploys the kit's `herdr-config.toml` to `~/.config/herdr/config.toml`.
- **THIRD-PARTY** are **installed from source** via the pinned manifest and `pi install` — never copied or vendored into this repo (no committed `node_modules`). The installer also removes only its explicit retired-package entry; it does not prune unrelated user packages.

There is one install path for third-party extensions: `pi install` + the manifest. See `.shared-llm/public/llm/pi/common/THIRD-PARTY-EXTENSIONS.md` for the full set and per-extension runtime deps.

**Wiring (part of `just update` when `global:` includes `pi`):**

```bash
just update          # deploys the OWN extensions + personas into ~/.pi/ via the generated tree (global step)
just pi-extensions   # installs the pinned THIRD-PARTY extensions via `pi install`
```

**Launching (requires `tmux`):**

```bash
just hub         # start the socket hub in tmux (serves the review sub-agent sockets)
just builder     # launch a builder Pi
just pi-status   # show hub + socket state
just pi-clean    # stop the hub, remove stale sockets
```

If `~/.pi/agent/extensions/context-workflow.ts` or `~/.pi/agent/agents/codex-reviewer.md` already exists as a real file (not a symlink this kit manages), the global step leaves it untouched and reports it as foreign — it only ever creates, re-points, or prunes the symlinks it owns per `~/.shared-llm/manifest.json`, which resolve into `~/.shared-llm/generated/`.

Third-party extensions are installed via `pi install` (into `~/.pi/agent/npm/node_modules/`), never committed here. `settings.template.json` is the template; your live `~/.pi/agent/settings.json` (runtime-mutated by Pi) is never tracked.

**Prerequisites:** Python 3 + PyYAML for the engine, `just` as the entrypoint, a Pi install (`npm install -g @earendil-works/pi-coding-agent`), `node` / `npm` on your `PATH`, and `tmux` (for the `just hub` / `just builder` launch group).

## Terraform workflow (Pi)

A small group of `just` recipes drives a reviewed terraform loop on Pi:

```bash
just tf-reviewer-cc          # start the terraform reviewer (Claude Code) — run first
just tf-reviewer-pi          # or the reviewer on Pi
just tf-implement <plan>     # load a plan and write reviewed terraform until approved
just tf-approve              # apply/destroy with an agent-distilled plan table
just tf-auto <plan>          # implement, then the plan-table apply
just tf-reviewer-down        # stop the reviewer
```

## Output-style caveat

An active `explanatory` or `learning` Claude Code output style (set via `/config`) competes with the brevity rule in `.shared-llm/public/layers/llm/common/common/response.md` and can override it. For terse, structured replies use the default or `concise` output style.

## What is intentionally out of scope

A shared cross-harness skill library (distributing skills across multiple AI assistants beyond the global-install path above) is intentionally out of scope for this kit — it is a candidate for a separate future kit.
