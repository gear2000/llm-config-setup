# /meta-connect

Connect **this** Claude session to a running meta-orchestrator **hub** — the Go message hub a
Pi brain starts with `/hub start`. You read the hub's discovery JSON, health-check it, register
this session as an agent, and confirm. After that the session is **on the hub** and can wait for
orders, reply, and message peers (the primitives are listed at the bottom).

This is the worker-side counterpart to the Pi-side `/hub start|status|stop`. The hub itself is the
binary at `…/meta-orchestrator/hub/hub.go`; everything below talks to it over plain HTTP with curl.

## Argument — the hub location

`$ARGUMENTS` (optional) — the hub's **discovery JSON path** (the file the hub writes with its
`url` + `pid`). Default: `$HOME/.meta-orch/hub.json` (what `/hub start` writes when given no
`--json`). For a specific brain, pass its JSON path, e.g. `/meta-connect ~/.meta-orch/A.json`.

A second token, if present, is **this worker's name** on the hub (so the brain can address it by
name); if omitted, generate one. The name is the worker's address: the brain `/send`s to it and
the worker reads `/next?session=<name>`.

## What you do — exact steps (Bash + curl, fail loud)

1. **Resolve the JSON path.** First token of `$ARGUMENTS` if given, else `$HOME/.meta-orch/hub.json`.
2. **Read the hub URL from the JSON:**
   ```
   URL=$(python3 -c "import json;print(json.load(open('<path>'))['url'])")
   ```
   If the file is missing, unreadable, or has no `url` → **STOP and report**, verbatim cause:
   *"No hub at `<path>`. Start one on the Pi side first: `/hub start` (or pass the right JSON path)."*
   Do **not** invent a URL.
3. **Health-check the hub:** `curl -fsS "$URL/health"` must return HTTP 200 (`{"ok":true,…}`). If it
   fails → **STOP**: *"Hub recorded at `<path>` isn't answering — stale JSON or the hub is down."*
4. **Pick the session name.** Second token of `$ARGUMENTS` if given, else generate
   `worker-$(openssl rand -hex 3)` (or any short unique id).
5. **Register this session:**
   ```
   curl -fsS -XPOST "$URL/register" -d "{\"session\":\"<name>\"}"
   ```
   Expect `{"ok":"true","session":"<name>"}`. A non-2xx or a different shape → **STOP** and show it.
6. **Report connected**, one line: `connected to <URL> as <name>  (json <path>)`.

## After connecting — the primitives (reference; this command does NOT loop on them)

- **Wait for an order** (blocks until the brain sends one):
  `curl -fsS "$URL/next?session=<name>"` → `{"msg_id","from","to","prompt"}`.
- **Reply to an order:**
  `curl -fsS -XPOST "$URL/respond" -d "{\"msg\":\"<msg_id>\",\"body\":\"<your result>\"}"`.
- **Message a peer and wait for the reply:**
  `curl -fsS -XPOST "$URL/send" -d "{\"from\":\"<name>\",\"to\":\"<peer>\",\"prompt\":\"…\"}"` → `{"msg_id"}`,
  then `curl -fsS "$URL/await?msg=<msg_id>"` (blocks) → `{"msg_id","body","error"}`.

## Fail loud

Missing/unreadable JSON, no `url`, a failed `/health`, or a non-2xx `/register` → **STOP** with the
specific reason above. Never fabricate a connection or proceed as if connected when it isn't.
