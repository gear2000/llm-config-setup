# /update-shared-llm

Guides updating `.shared-llm/` layer content and getting it recomposed and
committed across the private repo and the public `llm-config-setup` kit.

## When to use

Any time the user describes a change to:
- A skill (language conventions, workflow guides, slash commands)
- An agent definition (persona body, description, model)
- A CLAUDE.md or AGENTS.md rule
- A common prompt fragment

Instead of hunting for the right file and remembering the recompose + review
workflow manually, invoke this skill.

---

## The model — one engine, two layer owners

- The engine (`tools/harness.py`, driven by `just`) lives **only** in the public
  `llm-config-setup` kit. It is never copied into a destination. It reads
  `~/.shared-llm.yaml` and rebuilds every configured repo with one `just update`.
- A registered destination's `.shared-llm/` is split into two trees:
  - **`public/`** — kit-synced. Rebuilt from the kit on every `just update`;
    never hand-edited.
  - **`this_repo/`** — repo-owned. The engine reads it during compose but never
    writes or prunes it.
- Every layer is owned by exactly one side:
  - **`common/`** layers are GENERIC and live in the **kit**. Editing one edits
    the public kit directly; `just update` copies it into every destination's
    `public/` tree and recomposes.
  - **`this_repo/`** layers are PRIVATE and live in the **repo**. They never sync
    anywhere.
- **The `common/` path is a hint, not a guarantee.** A file can be misplaced in
  `common/` and still contain proprietary content. Always read the actual file
  before treating it as public-safe. Never skip the content review step.

---

## The workflow

### Step 1 — Understand what to change

Ask the user what they want to change if it is not already clear. One question:
which skill/layer, and what should it say differently?

### Step 2 — Find the right layer file

Dispatch an Explore subagent to find the target file(s). Common (generic) layers
live in the kit under `.shared-llm/public/layers/`; private layers live in the repo under
`.shared-llm/this_repo/layers/`. Return the exact path(s) before proceeding.

### Step 3 — Edit the layer file(s)

Dispatch a subagent to make the described change. It edits only the identified
layer file(s) — never the composed outputs (those regenerate in the next step).

### Step 4 — Recompose and verify (idempotent)

Run `just update` from the kit checkout. It rebuilds every configured destination
from its `.shared-llm/` (copy -> compose -> link, plus the global home step). Run
it a **second** time: the `git diff` must be empty on the second run. Recompose is
idempotent — a non-empty second-run diff means the first run did not fully
regenerate, or another layer also needs editing.

(To recompose a single repo without the full flow, the low-level composer is
`python3 <kit>/tools/harness.py compose --shared-llm <repo>/.shared-llm --target <repo>`.)

### Step 5 — Content review (MANDATORY — never skip)

For every file changed in Step 3, read the full content and classify it:

| Classification | Meaning | Action |
|---|---|---|
| **PASS** | No private references, no proprietary content | Safe for the public kit |
| **NEEDS_CLEANUP** | One or two fixable references | Show the user the exact lines; strip them with approval; re-confirm PASS |
| **FLAG_PROPRIETARY** | Too much proprietary content, or clearly misplaced in `common/` | Tell the user: "This file is in `common/` but contains [X]. Clean it or move it to `this_repo/` before it ships." Wait for instruction. |

A `common/` (public) layer must contain **nothing** proprietary — no private
repo/product/codename, infra or host names, account IDs, credential paths, or
private design detail. `this_repo/` layers are private; they never sync, so
project detail there is expected — skip content review for them and note the
change was private-only.

### Step 6 — Commit

- A **`this_repo/`** (private) change: commit **only** to the private repo
  ({{PRIVATE_REPO}}), staging the layer plus its recomposed outputs
  (`.claude/skills/`, `.claude/agents/`, `CLAUDE.md`, `AGENTS.md` as applicable).
  It never touches the public kit.
- A **`common/`** (public) change: it already lives in the kit, so commit it in
  the public `llm-config-setup` repo — but **only after** the content review
  passes **and** an independent reviewer approves the diff (the kit is
  world-readable; see the kit's `CLAUDE.md` pre-push rule). Then commit the
  recomposed outputs in each private repo that consumes the layer.

---

## Adding a new skill or agent

If the user wants a NEW skill or agent (not just an edit):

1. Create the layer file under `.shared-llm/public/layers/<type>/common/<name>/` (generic,
   kit) or `.shared-llm/this_repo/layers/<type>/this_repo/<name>.md` (private, repo).
2. Create the compose recipe under `.shared-llm/public/compose/<type>/<name>.yaml` (kit) or
   `.shared-llm/this_repo/compose/<type>/<name>.yaml` (repo-owned override).
3. Run `just update` to generate the output.
4. A recipe that references only `common/` layers is public-safe — review it and add
   it to the kit. A recipe that references any `this_repo/` layer is private-only.

---

## Common mistakes to avoid

- Never skip content review because a file is in `common/`. Read the content.
- Never push the public kit without an independent proprietary-content review.
- Do not edit composed outputs directly — always edit the layer source and rerun
  `just update`.
