# Install or update llm-config-setup

Single entry point for installing the kit on a new machine, refreshing after a kit pull, or changing global harnesses and UpAgent offering policy. **Give this file to an LLM** (for example `@UPINSTALL.md`) or follow it yourself.

To **add or seed a destination repository**, stop and open [SETUP-DESTINATION.md](SETUP-DESTINATION.md) (`@SETUP-DESTINATION.md`). That file asks where LLM md files should land, optionally scores how LLM-friendly the repo is, then registers the dest and deposits `.shared-llm/this_repo/` so `just update` generates the files.

The kit centrally composes reusable public layers plus repository-owned layers into harness-specific `CLAUDE.md`, `AGENTS.md`, skills, agents, and Pi runtime links. **Run every kit command from the cloned kit checkout**, not from a destination repository.

A new user typically has none of the harnesses or third-party skills this kit assumes. The [Machine stack](#machine-stack-new-user) section is mandatory on a fresh machine. `just update` composes kit-owned files; it does **not** install Pi, Herdr, or third-party skills.

---

## When to use

- Fresh machine / first kit installation
- Refresh every registered destination after pulling the kit
- Change global harnesses, UpAgent offering sets, or home runtime

**Add a destination, seed `this_repo/`, or fill `TEMPLATE.*` stubs:** [SETUP-DESTINATION.md](SETUP-DESTINATION.md). Do not do that work from this file.

---

## LLM workflow

### 1 — Inspect before asking

Read-only checks first:

| Signal | Meaning |
|--------|---------|
| Kit checkout root (`tools/harness.py`, `justfile`) | Commands must run here |
| `~/.shared-llm.yaml` | Machine roster exists |
| `~/.shared-llm/manifest.json` | Home deployments recorded |
| `~/.shared-llm/generated/extensions/common/upagent/offerings.yaml` | UpAgent hub materialized |
| `herdr --version` | Must be **0.7.1** for the UpAgent hub |
| `command -v pi` / `pi --version` | Pi CLI (need >= 0.74.0 when `global` includes `pi`) |
| `command -v node` / `npx` | Needed for Pi, Lavish, Impeccable, `just misc` |
| `command -v claude` | Claude Code CLI when `global` includes `cc` |

Also inspect:

- current OS and whether this checkout is the kit root
- `python3`, PyYAML, and `just` availability
- `tmux` if they will use the Pi `just hub` / `just builder` launch group
- destination repository roots the user names (if they want a new dest, hand off to SETUP-DESTINATION.md)
- any repository-root `.shared-llm.yaml` or `.shared-llm.yaml.example` for optional work-log settings

**Branch:**

- **Not installed** — no usable `~/.shared-llm.yaml` → follow [Install path](#install-path-fresh-machine) below
- **Installed** — summarize current `source`, `global`, `upagent.offering_sets`, and `destinations`; ask what the user wants to change before mutating anything → follow [Update path](#update-path-already-installed)

**Ask before mutating:** **Do you want to set up a destination repository, or just install or update the kit?**

- **Just install or update the kit** — stay in this file. Do not open SETUP-DESTINATION.md.
- **Set up a destination** — if the kit is not installed yet, finish the install path first (machine stack + source hub + global harnesses), then stop and open [SETUP-DESTINATION.md](SETUP-DESTINATION.md). If the kit is already installed, stop now and open that file.
- **Both** — kit work in this file first, then SETUP-DESTINATION.md.

Ask only for unresolved facts: OS, kit checkout path, source hub path (default `~/.shared-llm`), desired global harnesses (`cc`, `codex`, `pi`, or `cursor`, which uses the Codex surface), optional UpAgent offering sets (`standard` by default, or `standard,claudex` by explicit opt-in). For a new user, default `global` is `cc,pi` and install the **full machine stack** unless they opt out of a named piece. Destination path, harnesses, placeholders, `this_repo/` stubs, and nested LLM files belong in SETUP-DESTINATION.md.

### 2 — Guardrails

Keep these files distinct:

- `~/.shared-llm.yaml` is the per-machine source/global/destination roster and UpAgent offering policy maintained by `just configure`.
- A repository-root `.shared-llm.yaml` or `.shared-llm.yaml.example` configures work-log output for planning flows; it is not the machine roster.

Prerequisites:

- Check `python3`, PyYAML, and `just`.
- Ask before installing OS packages or Python packages.
- If prerequisites are missing and the user declines installation, stop with exact missing items.

Never edit generated outputs (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/*/SKILL.md`, `.claude/agents/*.md`) by hand; edit layers/recipes and recompose. Preserve foreign files and links.

Fail loudly on unresolved placeholders, malformed config, missing prerequisites, conflicting non-owned files/links, missing credentials for optional features, or Herdr that is not **0.7.1**. Do not overwrite foreign files. Do not run `herdr update` or `curl -fsSL https://herdr.dev/install.sh | sh` (that installer always fetches **latest**, which breaks this kit's UpAgent hub).

Destination writes (`just configure -d`, seeding `.shared-llm/this_repo/`) happen only from SETUP-DESTINATION.md, and only after the user accepts a file map.

### 3 — Commands (always from the kit checkout)

- `just init -o <mac|ubuntu>` when an OS prereq check is useful.
- Omit `-s` to accept the default source hub, or run `just configure -s <deliberate-hub-path>` such as `~/.shared-llm` when the user deliberately chooses a hub path. Do not pass the kit checkout path to `-s`; the checkout is code, while `source:` is the generated hub copied into by the engine.
- `just configure -g <harnesses>` for global home skills/agents/runtime.
- `just configure -d <repo> -l <harnesses>` for each destination (from SETUP-DESTINATION.md after the user accepts the file map).
- `just configure --offering-sets standard,claudex` only when the machine should opt into ClaudeX; use `just configure -d <repo> --offering-sets standard` when one destination should replace the machine choice with the standard roster.
- `just herdr-pin` / `bash tools/install-herdr.sh` — Herdr **0.7.1** only. `just herdr-pin --check` verifies.
- `npm install -g @earendil-works/pi-coding-agent` then `just pi-extensions` (`tools/install-pi-extensions.sh`, manifest `.shared-llm/public/llm/pi/common/third-party-extensions.txt`) when `global` includes `pi`. If `just pi-extensions` prints `pi not found — skipping`, that is a failure: install Pi first and re-run.
- `just misc` / `bash tools/install-misc.sh` — Lavish, quota-axi, Impeccable, Plannotator. Then re-apply [docs/SKILL-CUSTOMIZATIONS.md](docs/SKILL-CUSTOMIZATIONS.md) if Lavish was overwritten.
- `just descriptions` before update.
- `just update` to build, then run it a second time to verify idempotence.

### 4 — Verification (both paths)

Finish only after `just descriptions` passes, `just update` passes twice, the second update is idempotent, `~/.shared-llm.yaml` has the intended source/global/destinations/UpAgent offering policy, generated destination files and home links point to the intended generated source, and the manifest is consistent.

Also verify the machine stack that this kit actually uses:

- `bash tools/install-herdr.sh --check` (Herdr **0.7.1**)
- if `global` includes `pi`: `pi --version` (>= 0.74.0) and `pi list` shows the uncommented pins from `third-party-extensions.txt`
- kit-composed `unslop` is linked under the home skill dir for each selected harness (`~/.claude/skills/unslop`, `~/.pi/agent/skills/unslop`, and/or `~/.agents/skills/unslop`)
- third-party skills from `just misc` are present where those installers put them (Lavish, Impeccable)

Summarize changed files, installed harnesses, registered repositories, selected offering sets, Herdr version, and optional next steps.

---

## Machine stack (new user)

`just update` never installs these. Ask before installing OS packages, Python packages, or global npm packages, then install what the user accepted. For a **new user who wants the whole kit**, install every row that matches their harness list. Default harness list is `cc,pi`.

### Engine (always)

| Need | How |
|------|-----|
| `just` | `just init -o ubuntu` or `just init -o mac` (checks `python3` + `just` only) |
| `python3` | On `PATH` as `python3`, or set `PYTHON_BIN` |
| PyYAML | `python3 -m pip install pyyaml` |
| Node.js 20+ / `npm` / `npx` | Required for Pi, `just pi-extensions`, `just misc`, Lavish CLI (`npx -y lavish-axi`) |

### Harness CLIs (match `just configure -g`)

| Token | Install |
|-------|---------|
| `cc` | Claude Code CLI so `claude` is on `PATH` |
| `pi` | `npm install -g @earendil-works/pi-coding-agent` (Pi >= 0.74.0). `tmux` if they will run `just hub` / `just builder`. |
| `codex` | Codex CLI |
| `cursor` | Cursor Agent CLI (`cursor-agent`). Same skill surface as `codex`. |

Kit **own** Pi extensions (the `.ts` files under `.shared-llm/public/llm/pi/common/extensions/`) are **not** `pi install`. The global step of `just update` copies them into `~/.shared-llm/generated/` and links them into `~/.pi/`.

### Herdr 0.7.1 (UpAgent hub — required)

The UpAgent recruiter is tested against **Herdr 0.7.1**. A newer binary breaks the hub.

```bash
just herdr-pin
# same script: bash tools/install-herdr.sh
just herdr-pin --check
```

That script downloads the GitHub release `v0.7.1` into `~/.local/bin/herdr`. It does **not** call `https://herdr.dev/install.sh` (that URL always installs latest).

- If a different version is already on `PATH`, the script exits non-zero unless you pass `--force`.
- After install, `herdr --version` must print `herdr 0.7.1`.
- Do **not** run `herdr update`, `brew upgrade herdr`, or `mise upgrade herdr`.
- `just update` deploys this repo's `herdr-config.toml` to `~/.config/herdr/config.toml` (with `version_check = false`). It does not install the `herdr` binary.

### Pi third-party extensions (when `global` includes `pi`)

After `pi` is on `PATH`:

```bash
just pi-extensions
# fail-loud equivalent: bash tools/install-pi-extensions.sh
# plan only: bash tools/install-pi-extensions.sh --dry-run
```

Manifest (pinned sources, one `pi install` line each): `.shared-llm/public/llm/pi/common/third-party-extensions.txt`.

Current pins: `pi-lens@3.8.45`, `pi-subagents@0.30.0` (floor), `pi-web-access@0.10.7`, `pi-powerline-footer@0.5.4`, `pi-markdown-preview@0.10.0`, `pi-simplify@0.2.2`, `pi-intercom@0.6.0`, `@ayulab/pi-rewind@0.4.2`. Details: [.shared-llm/public/llm/pi/common/THIRD-PARTY-EXTENSIONS.md](.shared-llm/public/llm/pi/common/THIRD-PARTY-EXTENSIONS.md).

Optional runtime for markdown-preview only (the kit does not install these): Pandoc, a Chromium-based browser. Missing Pandoc only disables preview/PDF; the extension still loads.

`pi-cursor-sdk` stays commented in the manifest unless this machine actually uses Cursor inside Pi.

### Third-party skills (not composed by the kit)

```bash
just misc
# same script: bash tools/install-misc.sh
```

`tools/install-misc.sh` runs:

1. Plannotator — `curl -fsSL https://plannotator.ai/install.sh | bash`
2. **Lavish** (Kun Chen, `kunchenguid/lavish-axi`) — `npx -y skills add kunchenguid/lavish-axi --skill lavish`
3. **quota-axi** — `npx -y skills add kunchenguid/quota-axi --skill quota-axi -g`
4. **Impeccable** — `npx -y impeccable install --providers=claude,codex,cursor,pi --scope=global` (user-global on purpose; do not project-install into this public checkout)

After Lavish, re-apply the ASCII-tree policy in [docs/SKILL-CUSTOMIZATIONS.md](docs/SKILL-CUSTOMIZATIONS.md) if the installer replaced `.claude/skills/lavish` or `.agents/skills/lavish`.

### Kit-composed skills (no extra installer)

These arrive when `just update` runs the global step (`-g` is set). Do **not** `npx skills add` them:

- **unslop** (slash-command skill; also used by `plain-speech`)
- `herdr`, `python`, `nextjs`, `backend`, `golang`, `clickhouse`, `kafka`, `lucidchart`, `drawio`, `create-html`
- routed slash commands (`/cc-*`, `/do-*`, and the common set)

Claude Code `settings.template.json` enables the official plugins `frontend-design` and `superpowers` when `~/.claude/settings.json` is first scaffolded. That is not `just misc`.

---

## Install path (fresh machine)

**0. Machine stack.** Follow [Machine stack](#machine-stack-new-user). Ask, then install engine prereqs, harness CLIs for the chosen `-g` list, Herdr 0.7.1, Pi extensions if `pi` is selected, and `just misc`.

The engine lives **only** in the kit and is never copied into your repo. You drive everything with `just` from the kit checkout, against a config file it maintains, `~/.shared-llm.yaml`.

1. **Point the engine at the source hub, and set the global (home) harness list:**

   ```bash
   just configure -s ~/.shared-llm
   just configure -g cc,pi
   ```

   The home skills/runtime part of the **global** step (enabled by `-g`) installs the general home skills (`python`/`nextjs`/`backend`/`golang`/`herdr`/`clickhouse`/`kafka`/`lucidchart`/`drawio`/`create-html`) plus routed slash-command skills (including **unslop**) into `~/.claude/skills`, `~/.pi/agent/skills`, and `~/.agents/skills`. Pi workflow-suite commands are `/do-research`, `/do-plan`, `/do-implement`, `/do-convert`, and `/do-full`; Claude Code gets the matching `/cc-research`, `/cc-plan`, `/cc-implement`, `/cc-convert`, and `/cc-full` commands. Legacy planish / plan-and-grill / meta names are one-release warning aliases. It also installs the generic agents (see the Inventory section in README) into `~/.claude/agents` and `~/.pi/agent/agents`, the Pi runtime into `~/.pi`, and the Claude hooks/statusline/settings into `~/.claude`. Codex/`cursor` receive skills only; UpAgent is the routed path to the matching specialist persona. It is idempotent and non-clobbering.

2. **UpAgent hub (new installs).** The safe default is the `standard` UpAgent roster (`offering_sets: [standard]`). The global step materializes the machine roster at `~/.shared-llm/generated/extensions/common/upagent/offerings.yaml` on every `just update`, even when no global harness is selected. The hub **requires Herdr 0.7.1** from step 0.

   - Opt in to ClaudeX machine-wide: `just configure --offering-sets standard,claudex`
   - Remove ClaudeX from one destination: `just configure -d /path/to/your-repo --offering-sets standard`
   - Destination `offering_sets` **replaces** the machine value for requests that start from that destination; a request `--cwd` does not switch policy to the target directory.
   - Do not put commands, models, providers, health checks, or preflight commands in `~/.shared-llm.yaml`; those fields are code-owned and unsupported configuration fails loudly.

3. **Destination (only if they asked for one).** Stop this file and open [SETUP-DESTINATION.md](SETUP-DESTINATION.md). Do not seed `this_repo/` or fill `TEMPLATE.*` stubs from this file. If they only wanted the kit, skip this step.

Then run [Verification](#4--verification-both-paths).

---

## Update path (already installed)

1. Summarize the existing `~/.shared-llm.yaml`: `source`, `global`, `upagent.offering_sets`, and every registered `destinations` entry (path, harnesses, placeholders, per-destination UpAgent overrides).
2. Note whether `~/.shared-llm/manifest.json` exists and whether `~/.shared-llm/generated/extensions/common/upagent/offerings.yaml` is present.
3. Run `bash tools/install-herdr.sh --check`. If it fails, stop and pin 0.7.1 (`just herdr-pin --force` only when replacing a wrong version the user agreed to replace). Do not `herdr update`.
4. Ask: **Do you want to set up a destination repository, or just install or update the kit?** Then what they want to change:

   | Intent | Actions |
   |--------|---------|
   | **Kit refresh only** | `just descriptions` → `just update` ×2 |
   | **Add destination** | Stop. Open [SETUP-DESTINATION.md](SETUP-DESTINATION.md) |
   | **Change global harnesses** | `just configure -g <harnesses>` → matching [Machine stack](#machine-stack-new-user) rows → update |
   | **Change UpAgent offerings** | `just configure --offering-sets …` (machine) or `just configure -d <repo> --offering-sets …` (destination) → update |
   | **Pi missing or extensions stale** | `npm install -g @earendil-works/pi-coding-agent` if needed, then `just pi-extensions` |
   | **Third-party skills (Lavish, Impeccable, …)** | `just misc`, then SKILL-CUSTOMIZATIONS.md for Lavish |
   | **Finish TEMPLATE stubs** | Stop. Open [SETUP-DESTINATION.md](SETUP-DESTINATION.md) Appendix |

5. Never assume “update” means overwrite foreign home files; the global step reconciles kit-owned symlinks only and leaves divergent foreign files untouched with a warning.

Then run [Verification](#4--verification-both-paths).
