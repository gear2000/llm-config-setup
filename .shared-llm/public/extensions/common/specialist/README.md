# Specialist Hub — the Librarian

An always-up **Librarian** pane that answers repo-knowledge questions on demand. A worker
(or a phase leader) asks "who knows about X, and where is it?"; the Librarian routes the
question to the right **specialist**, spawns that specialist as a short-lived pane, and hands
back a cited answer. The worker never loads every specialist's context — it borrows one, just
in time.

This is the **substrate** Specialist Hub from the UpAgent runner, re-homed onto Herdr. There
is no tmux and no Go message hub anywhere in the engine — everything goes over the `herdr`
CLI (which drives a running Herdr over its unix socket).

## Topology

```
Herdr session
└── ws: shared-services            always up · plan-agnostic
    └── librarian (root pane)      owns the routing map; runs `consult <path>` per question
        └── <specialist>           TRANSIENT pane, split per consult, closed after it answers
```

The Librarian is the workspace's root pane. It holds the routing map built from `agents.yaml`
(`name -> {description, location, cmd}`). Each specialist is spawned only when needed, loads
**only its own** definition file, answers, and its pane is closed. One specialist runs at a
time per consult.

## Consult protocol (files + signal)

Identical in shape to the UpAgent order/result pattern — one auditable mechanism, durable JSON
files as the source of truth, Herdr carrying only the go/done signal:

```
caller:     write  <runtime>/consults/<id>.json   { consult_id, specialist, question, answer_path }
caller:     herdr pane run <librarian> "consult <runtime>/consults/<id>.json"
librarian:  validate + route the consult (unknown specialist ⇒ fail loud)
librarian:  herdr pane split <librarian> --direction down --cwd <repo>   (transient specialist)
librarian:  herdr pane run <specialist> "<cmd, briefed with the question + answer contract>"
librarian:  wait for agent-status=done, or poll private answer_path for `codex exec`
specialist: write answer.json  { consult_id, answer, citations: ["file:line", ...] }
librarian:  validate answer.json → herdr pane close <specialist> → print "CONSULT <id> DONE"
caller:     herdr wait output <librarian> --match "CONSULT <id> DONE" --timeout <ms> → read answer.json
```

**Always bound the caller's wait with `--timeout`** — Herdr's `wait output` blocks forever without
it. The Librarian **always emits `CONSULT <id> DONE`** once the consult_id is known: on its error
paths (unknown specialist, hub not up, a herdr fault, a bad/missing specialist answer) it first
writes a **failure answer.json** (`{ consult_id, error }`) and still emits the sentinel, so the
caller's bounded wait resolves promptly instead of hanging. The caller treats a failure answer —
or a timeout (only possible when the consult_id was unrecoverable) — as a failed/unanswered consult.

Codex specialists are the completion-signal exception: Codex does not reliably report
`agent-status=done`, so a roster command containing `codex exec` is completed by polling that
consult's private `answer_path`. `{prompt_file}` expands to a shell-quoted path, not the file
contents. Codex must therefore read that file through stdin:

```yaml
cmd: "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check - < {prompt_file}"
```

The answer is still validated through the exact same strict contract, the transient pane is still
closed in the common cleanup path, and timeout still writes a failure answer plus
`CONSULT <id> DONE`.

`consult.json` and `answer.json` are validated fail-loud by `contracts_consult.py`
(`load_consult` / `load_answer`): required keys and a `consult_id` echo check that rejects a stale
answer. A **success** answer needs a **non-empty `citations` list of `file:line` references** — a
specialist answer with no sources is a contract violation; a **failure** answer instead carries a
non-empty `error` string (and needs no citations), mirroring the Recruiter's `blocked` result.

## Commands

`just specialist-hub <cmd>` (imports `hub.py`):

| Command | What it does |
|---|---|
| `up` | Create/attach the `shared-services` workspace + Librarian pane; write `index.json`; arm the `consult` dispatch. Idempotent — re-running while up just re-arms the pane. |
| `down` | Close the Librarian pane, remove runtime state. |
| `status` | Librarian pane health + roster size. |
| `reindex` | Rebuild `index.json` from `agents.yaml`. |
| `consult <consult.json>` | The per-question handler run **inside** the Librarian pane (spawns the specialist, waits, validates, signals done). You do not call this directly — the caller signals it via `herdr pane run`. |

Runtime files live in one directory (default `/tmp/.herdr-specialist`): `state.json` (workspace
+ Librarian pane ids), `index.json` (the roster), and `consults/` (where callers drop
`consult.json` and the Librarian writes each briefing).

## Adopting it

1. The engine is public and kit-synced — it lands in your repo at
   `.shared-llm/public/extensions/common/specialist/`. Do not edit it there; it is regenerated.
2. Import the module justfile from your root justfile:
   `import '.shared-llm/public/extensions/common/specialist/justfile'`
3. Fill the roster and list your specialists. This filled roster is **yours** — the kit ships
   only the generic template. `hub.py` resolves it in this order:
   1. `$SPECIALIST_HUB_CONFIG`, if set (explicit override);
   2. else the repo-owned `this_repo` roster it walks up from cwd to find:
      `.shared-llm/this_repo/extensions/common/specialist/agents.yaml` — put your copy here for
      zero-config discovery;
   3. else `agents.yaml` beside the engine (the kit's own adoption).
4. Every roster `cmd` must contain `{prompt_file}` or `{prompt}` so the Librarian can inject the
   question and the answer.json contract. `hub.py` fails loud at load time if one is missing.

Requires `HERDR_ENV=1` (run inside Herdr) and the `herdr` CLI on PATH.
