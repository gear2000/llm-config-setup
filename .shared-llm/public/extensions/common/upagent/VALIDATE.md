# UpAgent hub end-to-end validation

Reusable brief for an agent running inside the live Herdr cockpit: Claude Code, Pi, or Codex,
whichever you use as the cockpit. Run it after installing the kit on a new machine, after every
Herdr version change, after every kit update that touches the hub, and before trusting the hub
again after an incident. Hand the whole file to the cockpit agent as its task. It needs a
machine where `just update` has run and at least one harness is authenticated.

Time to allocate: about 45 minutes of agent time without the optional D9 hold drill, about
70 minutes with it, plus about 10 minutes of your own time for first-run screens on a fresh
Herdr version. Drills run one at a time, so the clock does not shrink with parallelism.

---

You are validating that the UpAgent hub works end to end on THIS machine, inside the live
Herdr session you are running in (`HERDR_ENV=1`). The goal is a PASS/FAIL verdict per drill
with evidence paths, so a broken hub is caught here and not during real work.

Budget: about 45 minutes without the optional hold drill, about 70 minutes with it.
Run drills one at a time. Never run two hires at once.

## Hard rules

- Never run `herdr update`, `brew upgrade herdr`, `just herdr-pin --force`, or stop the default
  Herdr server. Never write under `~/.pi`, `~/.claude`, `~/.codex`, `~/.cursor`, `~/.config/herdr`.
- Never run `just update` or `just global`.
- Control tokens stay in mode-0600 files. Never print `request.json` before redaction.
- A failed or blocked drill is a result. Record it and move to the next drill. Never relaunch
  silently, never "fix" the hub, never edit recruiter.py. If a drill leaves panes behind, list
  them and leave them; do not close panes except in drill D7, where closing is the test.
- Stop early only if the static checks in step 1 fail. Then report and stop.

## Step 1: static checks (2 min)

If this checkout is not on `main` (you are validating a branch before merge), no checked-out
`main` holds the UpAgent source, and every `just upagent` call fails with "no checked-out main
branch UpAgent source found". Then run every `just upagent` call in this brief with
`UPAGENT_CANONICAL_REPO=<this checkout's absolute path>` set. The hub passes it on to workers.

Run and record the output of each:

```bash
herdr --version                      # expected: the version `just herdr-pin --check` requires
herdr integration status             # expected: pi, claude, codex, cursor present; note versions
just herdr-pin --check               # expected: exit 0
grep -c herdr-status-fix ~/.codex/hooks.json 2>/dev/null || true   # expected: 0 (legacy 0.7.1 hook, see UPINSTALL)
herdr session list --json | python3 -c 'import json,sys; print([s["name"] for s in json.load(sys.stdin)["sessions"]])'
just upagent lists --type offerings --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["offerings"] if isinstance(d, dict) else d), "offerings")'
just upagent status --json | head -c 2000   # expected: "services_ready": true
just upagent lists --type workers --status active --json   # expected: [] (no active requests before you begin)
```

The required Herdr version is the pin in `tools/install-herdr.sh`.
If the version, the pin check, or the offerings list fail, report and stop.

Then record which harness CLIs exist and are logged in (`command -v claude pi codex cursor-agent`,
and each one's own auth status). Drills for a missing or unauthenticated harness are skipped, not
failed; say so in the report.

## Step 2: kit suite on the real home (3 min)

```bash
just test 2>&1 | tail -3
```

Record the passed count and exit code. Expected: 0 failures. A red suite is a stop condition.

## Step 3: a scratch repository for workers (1 min)

```bash
V=/var/tmp/upagent-validate-$(date +%Y%m%d-%H%M)
mkdir -p "$V/repo" && cd "$V/repo" && git init -q && git commit -q --allow-empty -m init
mkdir -p "$V/evidence"
```

Use `$V/repo` as `--cwd` for every hire. Keep every run directory; copy each drill's
`request.json` (redacted), `terminal.json`, result bundle, and a `herdr pane list` (it prints JSON)
snapshot taken right after terminalization into `$V/evidence/<drill>/`.

## Step 4: the standard worker task

Every worker drill uses this task text, written to the run dir as `prompt.md` with the
`upagent-run` skill's run-dir layout (UUID request id, mode 0700 dir, 0600 prompt):

```
You are a validation worker. Do exactly this, in order, then finish.
1. Run a foreground computation that takes about 100 seconds of wall clock and uses CPU
   (for example a Python loop that counts until time.monotonic() passes 100 s). Do not use
   sleep. Do not use a background job. Do not ask for permission; this is authorized.
2. Compute the sum of the integers 1 through 100 and state the result.
3. Write a file named SUM.txt containing only that number in the repository root.
4. Deliver your typed result bundle the way the hub instructions tell you to, with
   verdict passed and the sum in the summary.
```

Record for each drill: request id, offering, effort, persona, wall-clock from `just upagent
request` returning to `just upagent await` returning, the terminal verdict, whether
`SUM.txt` exists with 5050, whether a Sentinel closeout exists (or the ledger shows `sentinel-bypassed-at-exit`, which
skips the closeout when a valid bundle landed at worker exit), and whether every pane the
hub created is gone afterwards.

For every drill, before awaiting, confirm the brief landed:
- `<run-dir>/prompt.md` exists, mode 0600, and contains the task text.
- `<run-dir>/request.json` is redacted (no `requester_control_token` key) and
  `<run-dir>/control-token` exists with mode 0600.
- The public request dir under the hub state (`just upagent get --request <id> --json`) shows
  `prompt.md` and `order.json` (the instructions are the offering's `prompt_addendum` inside
  `order.json`), and the worker's `cwd` is `$V/repo`.

## Step 5: drills

Hire with: `just upagent request --type worker --request-id "$id" --offering <offering>
--effort <effort> --agent backend --prompt-file "$run_dir/prompt.md" --cwd "$V/repo"
--cockpit-pane "$HERDR_PANE_ID" --duration-minutes 10 --json > "$run_dir/request.json"`,
redact the token, then `just upagent await --request "$id" --json > "$run_dir/terminal.json"`.
Delete `SUM.txt` between drills.

Use the cheapest guide-backed seat for each harness: `staffing-guide.yaml` names
Sonnet 5.5 / medium as the short-check fallback, Sol 6.1 / medium as the next fallback,
and Composer 2.5 / default as the short-check primary. Use Sol's matching Codex and
ClaudeX offerings from `offerings.d/` for those harnesses. Luna remains in the offering
roster for compatibility but is retired from staffing seats. These are static selections;
step 1 still checks authentication and optional-offering availability on the machine.

| Drill | Offering / effort | What it proves | Pass condition |
| --- | --- | --- | --- |
| D1 codex exec | `codex-gpt-6-1-sol` / medium | exec launch, server-owned wait, typed closeout, Codex hooks state | verdict passed, 5050, panes gone, under 6 min |
| D2 claude interactive | `claude-sonnet-5-5` / medium | bare start, prompt after ready, one continuous `working` turn through the 100 s computation, no nudge | verdict passed, 5050, no `nudge` event in the ledger, panes gone |
| D3 pi interactive | `pi-gpt-6-1-sol` / medium | insulated Pi launch, agent-state extension loaded | verdict passed, 5050, panes gone |
| D4 cursor | `cursor-composer-2-5` / default | Cursor readiness wait, prompt delivery | verdict passed, 5050, panes gone (if Cursor is not installed or not authenticated, record skipped and continue) |
| D5 claudex | `claudex-gpt-6-1-sol` / medium | proxy-backed Claude launch | verdict passed, 5050, panes gone (skip with a note if the `claudex` offering set is not selected or the proxy is not running) |
| D6 nested request | `codex-gpt-6-1-sol` / medium, task below | a worker can place and await its own request; the inner runner runs detached from the worker's shell tool | inner request terminalizes passed (before the outer, since the outer awaits it); outer verdict passed and its result names the inner request |
| D7 pane close | `pi-gpt-6-1-sol` / medium | pane-loss detection and typed blocked closeout | within 30 s of your `herdr pane close`, ledger shows `sentinel-wake-worker-gone` (or a `worker-nudge-probe` with `pane_state: gone`); terminal verdict blocked, not a hang; panes gone |
| D8 timeout | `claude-sonnet-5-5` / medium, `--duration-minutes 2`, 300 s task | deadline handling | ledger shows `timeout-warning` while the computation still runs; await returns `awaiting-requester` and the requester message names extend/cancel; after you cancel with `just upagent cancel --request <id> --control-token-file <run-dir>/control-token`, no hub pane and no worker process remain |
| D9 (optional, 25 min) waiting worker | `claude-sonnet-5-5` / medium, `--duration-minutes 35`, task below | idle-with-background-job worker is nudged and escalated, not killed | ledger shows nudges and `worker-stall-escalation`; no STALLED termination; the worker completes passed |

D6 task text: replace step 1 of the standard task with "Place one UpAgent request yourself
from your shell using the upagent-run skill, offering codex-gpt-6-1-sol effort medium, with the
standard validation task, duration 10 minutes, and await it. Report its request id and verdict."
Append the standard task text below the D6 task, labelled "The standard validation task:", so
the worker can use it as the inner prompt.

D7 procedure: hire with the standard task, wait until `just upagent get` shows `running` and the
worker pane shows `working`, then run `herdr pane close <worker-pane-id>` from the cockpit.
Time from the close to the first of those two worker-gone ledger events.

D8 procedure: standard task with step 1 changed to about 300 seconds (both numbers), and
`--duration-minutes 2`. The 2-minute clock starts when the worker is healthy, not when the
request returns, so a 100 s task would finish before it. Expect `timeout-warning` in the ledger
at about 2 minutes after `worker-healthy`, while the computation still runs. `await` then
returns exit 2 with state `awaiting-requester` and a `decision_nonce`. The extend/cancel choice
is in the requester `timeout-warning` message, not in the await JSON; that message holds a
control token, so read only its wording, never print it. Cancel. Record that the panes closed
and that no process of the worker (for example its computation in `$V/repo`) is still running.

D9 task text: "Start a background job that sleeps 25 minutes, then return to your prompt and
wait for it without doing anything else. When it finishes, write SUM.txt containing 5050 and
deliver a passed bundle." Use `--duration-minutes 35` so the deadline does not end it. Expect
up to three nudges, a `worker-stall-escalation` once the nudge cap is spent, and then completion.
A `sleep` job uses no CPU, so it earns no `worker-stall-held` hold (FUNDAMENTALS.md: the hold
needs a pending STALLED closeout and real progress); no event of that name is expected. This is
the only drill allowed to take 25 minutes.

## Step 6: report

Produce one table, one row per drill: drill, offering, elapsed seconds, verdict, SUM.txt
correct (y/n), Sentinel closeout present (y/n), panes gone (y/n), evidence path. Then:

- Overall: PASS only if every drill whose harness is installed and authenticated passed, with
  D1, D2, D3, D6, D7, D8 required when their harness exists, and no nudge fired in D2. A harness
  that is not installed or not authenticated is "skipped", never a failure. D9 is informational.
- The `herdr --version`, integration versions, and `just test` count from steps 1 and 2.
- Every drill that did not pass, with the terminal reason and the ledger tail.
- Anything left behind: panes, active requests, files outside `$V`.
- Do not propose fixes. The reader decides whether to roll back using UPINSTALL.md.
