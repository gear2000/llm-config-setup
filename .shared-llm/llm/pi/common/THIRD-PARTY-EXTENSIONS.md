# Third-party Pi extensions

Pi extensions in this kit come in **two kinds**, installed by **two different paths**. Keep them separate.

| Kind | Lives in | Wired by | Mechanism |
|------|----------|----------|-----------|
| **OWN** (authored here) | `.shared-llm/llm/pi/common/extensions/*.ts` | `tools/setup-pi.sh` | **Symlinked** into `~/.pi/` — copied/layered, never installed from a registry |
| **THIRD-PARTY** | a registry (npm/git) | `tools/install-pi-extensions.sh` | **Installed from source** via `pi install` — never copied/vendored into this repo |

This page is about the **third-party** set. For the own extensions, see `setup-pi.sh` and the README's "Pi harness runtime config" section.

## The rule

- Third-party extensions are **never vendored** (no committed `node_modules`, no copied `.ts`). They are declared as pinned sources in `third-party-extensions.txt` and installed with `pi install`.
- Own extensions are **never installed** from a registry. They are authored under `extensions/` and symlinked.
- One install path for third-party: `pi install` + the manifest. Do not add a parallel `npm ci` route.

## How `pi install` works (verified)

`pi install <source>` appends the source to the `packages` array in `~/.pi/agent/settings.json` **and** fetches it into `~/.pi/agent/npm/node_modules/`. Pi loads everything in that `packages` array on startup.

- **Idempotent by package name.** Re-installing an already-present package adds no duplicate array entry and exits 0. It matches on the package *name*, not the exact pinned string — so on a machine that already has `npm:pi-lens`, installing `npm:pi-lens@3.8.45` is a no-op and the existing entry is kept.
- **Pins are honored on first install.** `pi install npm:pkg@1.2.3` writes `npm:pkg@1.2.3` into the array on a machine that does not have `pkg` yet. To move an already-populated machine onto a new pin: `pi remove npm:pkg` then re-run the install script.
- Management: `pi list` (show installed), `pi update <source>` (update one), `pi remove <source>` (uninstall + drop from settings).
- Try one without installing: `pi -e <path-to-extension-index.ts>` for a single session (no settings write).

## Install the whole set (one command)

```bash
tools/install-pi-extensions.sh            # install everything in the manifest, skip what's present
tools/install-pi-extensions.sh --dry-run  # show what would run, change nothing
```

The script reads `third-party-extensions.txt`, skips anything already in `pi list` (matched by name), runs `pi install` for the rest, and **fails loud** (non-zero exit) if any install fails or a peer-dep / Pi-version requirement is unmet — never masked with `|| true`. Verify after: `pi list`.

`tools/setup-pi.sh` calls this script automatically, so `task setup:pi` wires the own extensions (symlinks) **and** installs the third-party set in one go.

## The set (pinned)

Each line below is exactly what `tools/install-pi-extensions.sh` runs. Source of truth is `third-party-extensions.txt`.

```bash
# Carried over from the original kit (the 5 that used to come in via npm ci):
pi install npm:pi-lens@3.8.45                  # inline diagnostics / linting lens
pi install npm:pi-subagents@0.25.0             # spawn and manage Pi subagents
pi install npm:pi-web-access@0.10.7            # fetch and search the web from a session
pi install npm:pi-powerline-footer@0.5.4       # powerline-style TUI status footer
pi install npm:@juicesharp/rpiv-btw@1.12.0     # review-plan-implement-verify workflow helper

# Added:
pi install npm:pi-markdown-preview@0.10.0      # rendered markdown / Mermaid / LaTeX preview
pi install npm:@plannotator/pi-extension@0.20.1 # browser-based plan / diff / PR review UI
```

### Runtime dependencies (the kit never installs these — install them yourself if you want the feature)

- **pi-markdown-preview** needs, at runtime:
  - **Pandoc** (+ a LaTeX engine such as `xelatex`) — for `/preview` rendering and PDF export. **Not present on this machine** as of writing; without it the PDF/preview rendering paths are unavailable, but the extension still loads. Install Pandoc separately (`apt install pandoc` / `brew install pandoc`); set `PANDOC_PATH` if it is off `PATH`.
  - A **Chromium-based browser** (Chrome / Brave / Edge / Chromium) for terminal/PNG preview. A Chrome binary is present here (`/usr/bin/google-chrome`). Set `PUPPETEER_EXECUTABLE_PATH` to override detection.
  - Optional: Mermaid CLI for Mermaid-in-PDF.
- **@plannotator/pi-extension** opens reviews in your **default browser**. Requires **Pi >= 0.74.0**.

## Skipped: pi-cursor-sdk (Pi too old)

`pi-cursor-sdk` (use Cursor's models inside Pi) is **commented out** in the manifest and **not installed**.

- The author **recommends Pi >= 0.79.1**. The Pi installed here is **0.75.4** — below that.
- Its npm peer metadata is *intentionally unpinned*, so `npm`/`pi install` would **not** block it — but the runtime path is unsupported below 0.79.1, so installing it now would be a silently-broken extension. We skip it on purpose.
- To enable later: update Pi to >= 0.79.1 (`pi update pi`, or `npm install -g @earendil-works/pi-coding-agent`), then uncomment the `npm:pi-cursor-sdk@0.1.42` line in `third-party-extensions.txt` and re-run `tools/install-pi-extensions.sh`.

## For another project / an LLM picking this up

To reproduce this exact set anywhere Pi is installed (>= 0.74.0):

1. Copy `third-party-extensions.txt` and `tools/install-pi-extensions.sh` into the project (or just run the seven `pi install` commands above).
2. Run `tools/install-pi-extensions.sh`.
3. Confirm with `pi list` — you should see the seven entries.
4. For pi-cursor-sdk, first ensure Pi >= 0.79.1, then uncomment its manifest line and re-run.
