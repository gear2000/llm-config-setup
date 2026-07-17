# Specialist Hub — the Librarian

An always-up **Librarian** pane that answers repo-knowledge questions on demand. A worker
(or a phase leader) asks "who knows about X, and where is it?"; the Librarian routes the
question to the right **specialist**, asks the Recruiter to manage that short-lived worker, and
hands back a cited answer. The caller never loads every specialist's context — it borrows one,
just in time.

This is the **substrate** Specialist Hub from the UpAgent runner, re-homed onto Herdr. There
is no tmux and no Go message hub anywhere in the engine — everything goes over the `herdr`
CLI (which drives a running Herdr over its unix socket).

## Topology

```
Herdr session (default: single workspace)
└── ws: herdr                      one workspace for services AND runs
    ├── tab: services              ├── recruiter · └── librarian
    └── (runs add control/workers/oversight tabs here)

Herdr session (with `up --separate-workspaces`)
└── ws: shared-services            services-only workspace · plan-agnostic
    ├── librarian                  owns only specialist routing
    └── recruiter                  owns every specialist worker lifecycle
```

The Librarian holds the routing map built from the **merged rosters** — the kit's base
`agents.yaml` beside the engine plus the destination-owned `this_repo` overlay, same-named
overlay entries clobbering base ones
(`name -> {description, location, harness, model, agent, effort, origin}`). Each specialist is an ordinary
UpAgent request with its own Dedicated Account Manager, verified startup, lease, timeout policy,
and cleanup — the consult order pins `management.mode: dedicated`, so consults always get that
broker even though the roster default is the direct Python lifecycle. The Librarian never starts
or closes an LLM pane.

## Consult protocol (files + signal)

Identical in shape to the UpAgent order/result pattern — one auditable mechanism, durable JSON
files as the source of truth, Herdr carrying only the go/done signal:

```
caller:     write  <runtime>/consults/<id>.json   { consult_id, specialist, question, answer_path }
caller:     invoke `specialist-hub consult <runtime>/consults/<id>.json`
librarian:  validate + route the consult (unknown specialist ⇒ fail loud)
librarian:  write a normal UpAgent order + cited-answer brief
recruiter:  create manager → atomically start/verify specialist → monitor result/deadline
specialist: write answer.json  { consult_id, answer, citations: ["file:line", ...] }
librarian:  receive durable UpAgent receipt → validate answer.json → print "CONSULT <id> DONE"
caller:     read answer.json
```

**Always bound the caller's wait with `--timeout`** — Herdr's `wait output` blocks forever without
it. The Librarian **always emits `CONSULT <id> DONE`** once the consult_id is known: on its error
paths (unknown specialist, hub not up, a herdr fault, a bad/missing specialist answer) it first
writes a **failure answer.json** (`{ consult_id, error }`) and still emits the sentinel, so the
caller's bounded wait resolves promptly instead of hanging. The caller treats a failure answer —
or a timeout (only possible when the consult_id was unrecoverable) — as a failed/unanswered consult.

Codex is no longer a special case. The Recruiter's generic lease-private result monitor handles
harnesses whose Herdr status lags, while the same strict answer contract applies to all specialists.

Two guarantees make the broker visible and unforgeable:

- **Live sidebar state.** While a consult routes, the Librarian's sidebar entry flips to
  `working` (naming the consult and specialist) and returns to `idle` with a served tally —
  so an idle-looking Librarian during heavy consulting means consults are bypassing it, not
  that it is unused.
- **Brokered-only consults.** The Recruiter issues a `consult_token` at its `up`; the
  Librarian stamps it on every order it authors. The Recruiter refuses any consult-shaped
  order (a `specialist-consult` phase/order id or a `specialist-librarian` requester) without
  the current token — a hand-written imitation of the Librarian's paperwork fails loudly with
  the correct door named.

`consult.json` and `answer.json` are validated fail-loud by `contracts_consult.py`
(`load_consult` / `load_answer`): required keys and a `consult_id` echo check that rejects a stale
answer. A **success** answer needs a **non-empty `citations` list of `file:line` references** — a
specialist answer with no sources is a contract violation; a **failure** answer instead carries a
non-empty `error` string (and needs no citations), mirroring the Recruiter's `blocked` result.

## Commands

`just specialist-hub <cmd>` (imports `hub.py`):

| Command | What it does |
|---|---|
| `up [--separate-workspaces]` | Create/attach the services workspace (unified `herdr` by default; `shared-services` with the flag) + Librarian pane; write `index.json`; arm the `consult` dispatch. Idempotent — re-running while up just re-arms the pane. |
| `down` | Close the Librarian pane, remove runtime state. |
| `status` | Librarian pane health + merged roster size (kit-base vs this-repo counts). |
| `reindex` | Rebuild `index.json` from the merged rosters. |
| `roster [--json]` | Print the merged roster as a paste-ready stage-brief block — the "phone book" a phase leader embeds VERBATIM in every worker's `instructions.md` (names, descriptions, exact consult mechanics). `--json` prints the raw merged index. |
| `consult <consult.json>` | Route one question through a managed UpAgent specialist, validate its answer, and signal done. |

Runtime files live in one directory (default `/tmp/.herdr-specialist`): `state.json` (workspace
+ Librarian pane ids), `index.json` (the roster), and `consults/` (where callers drop
`consult.json` and the Librarian writes each briefing).

## Adopting it

1. The engine is public and kit-synced — it lands in your repo at
   `.shared-llm/public/extensions/common/specialist/`. Do not edit it there; it is regenerated.
2. Import the module justfile from your root justfile:
   `import '.shared-llm/public/extensions/common/specialist/justfile'`
3. The kit ships a live **base roster** (`agents.yaml` beside the engine — generic public
   roles on public model aliases) that always loads. Add your own specialists in the
   repo-owned overlay `.shared-llm/this_repo/extensions/common/specialist/agents.yaml`
   (template: `agents.yml.sample`): `hub.py` merges base ∪ overlay, and a same-named overlay
   entry **clobbers** the base one — so both rosters are available for runs and yours wins on
   conflict. `$SPECIALIST_HUB_CONFIG` remains a single-file override that skips the merge.
4. Every new specialist should have explicit `harness`, `model`, `agent`, and optional `effort`.
   Executable commands live only in the UpAgent roster. `hub.py` still normalizes retired
   direct-Claude `cmd` entries in memory so older destination-owned rosters can boot; migrate
   those rosters to the explicit fields when you touch them.

Requires `HERDR_ENV=1` (run inside Herdr) and the `herdr` CLI on PATH.
