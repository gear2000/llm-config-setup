# /response

A **small done-ping** to the meta-orchestrator hub. After `/run_phase` has written `results.md` to
disk, this command reads the leading verdict out of that file and POSTs a short message to the hub
telling the brain the phase is done — the verdict plus the **path** to `results.md` (NOT the whole
report; the brain reads the file itself).

**The results.md file is the real signal; this ping is just the accelerator.** The brain can already
detect completion by watching `results.md` appear on disk — `/response` only lets it react sooner
without waiting on its file poll. So this command stays small and never duplicates the report into
the hub message.

## Invocation

```
/response --hub <hub-json-or-url> --output <results.md> [--name <worker-name>] [--msg <msg_id>]
```

- `--hub <hub-json-or-url>` — the hub's discovery JSON path (the file with `url` + `pid`, default
  `$HOME/.meta-orch/hub.json`) OR a direct hub `url`. Resolve a JSON path to its `url` the same way
  `/hub-connect` does.
- `--output <results.md>` — absolute path to the results file `/run_phase` just wrote. You read its
  FIRST line (`PHASE_RESULT: <verdict>`) and reference its path in the ping.
- `--name <worker-name>` — optional. This worker's name/address on the hub (the one used at
  `/hub-connect`). Used as the `from` when sending back to the brain.
- `--msg <msg_id>` — optional. If the brain sent the order over the hub and you have its `msg_id`,
  reply to it with `/respond`. If absent, send a fresh message back to the brain with `/send`.

These are `--flag value` tokens in `$ARGUMENTS`; they may appear in any order.

## Execution

### Step 1 — Parse + validate (fail loud)

- **`--hub` missing** → STOP: `Usage: /response --hub <hub-json-or-url> --output <results.md> [--name <worker-name>] [--msg <msg_id>]`.
- **`--output` missing or unreadable** → STOP: `ERROR: results file not found / unreadable: <output>`.
  Without the results file there is nothing to ping about.

### Step 2 — Resolve the hub URL

If `--hub` looks like a URL (`http://…`), use it directly. Otherwise treat it as a discovery JSON
path and read the `url` from it (`python3 -c "import json;print(json.load(open('<path>'))['url'])"`),
exactly as `/hub-connect`. Missing file / no `url` → STOP:
`ERROR: no hub url at <hub>` — do not invent a URL.

### Step 3 — Read the verdict from results.md

Read the **FIRST line** of `--output`. It must be `PHASE_RESULT: <verdict>`. Extract `<verdict>`
(`passed` / `partial` / `blocked` / `failed`). If the first line is not a `PHASE_RESULT:` line, STOP:
`ERROR: <output> does not start with a PHASE_RESULT: line — /run_phase did not write it correctly.`
Do not guess a verdict.

### Step 4 — POST the short ping (fail loud on non-2xx)

Build a small message — the verdict + the results.md path, NOT the report body:

```
phase done: PHASE_RESULT: <verdict>. results file: <absolute path to results.md> (the real signal — read it).
```

- **`--msg` given** → reply to that order:
  ```
  curl -fsS -XPOST "$URL/respond" -d "{\"msg\":\"<msg_id>\",\"body\":\"<the message above>\"}"
  ```
- **no `--msg`** → send a fresh message back to the brain (`from` = `--name` if given, else the
  worker name you registered with; `to` = `brain`):
  ```
  curl -fsS -XPOST "$URL/send" -d "{\"from\":\"<name>\",\"to\":\"brain\",\"prompt\":\"<the message above>\"}"
  ```

A **non-2xx** response → **STOP and report it** (the HTTP status + body). Never pretend the ping
landed when it did not.

### Step 5 — Report

One line: `pinged <URL>: PHASE_RESULT: <verdict> (results <path>)`. Then, in prose, make it
unmistakable: **the results.md file is the real signal the brain judges by — this ping only made the
brain react sooner.** If the ping had failed, the phase result would still be on disk for the brain
to read; the ping is the accelerator, not the source of truth.

## Hard rules

1. **`--hub` and a readable `--output` are required → FAIL LOUD** (Step 1). No ping without a results
   file to point at.
2. **Read the verdict from the results file's FIRST line** (Step 3) — never fabricate a verdict, and
   STOP if the file does not start with `PHASE_RESULT:`.
3. **Keep the ping small** (Step 4): the verdict + the results.md path only. Do NOT inline the report
   — the brain reads the file.
4. **Non-2xx on the POST → FAIL LOUD** (Step 4). Never claim a delivered ping that wasn't.
5. **The results file is the source of truth; `/response` is the accelerator** — say so in the
   report (Step 5).
