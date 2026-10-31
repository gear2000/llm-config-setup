# herdr — agent skill (0.9.5)

before using this skill, check that `HERDR_ENV=1`. if it is not set to `1`, say you are not running inside a herdr-managed pane and stop. do not inspect or control the focused herdr pane from outside herdr.

you are running inside herdr, a terminal-native agent multiplexer. herdr gives you workspaces, tabs, and panes — each pane is a real terminal with its own shell, agent, server, or log stream — and you can control all of it from the cli.

this means you can:

- see what other panes and agents are doing
- create tabs for separate subcontexts inside one workspace
- split panes and run commands in them
- start servers, watch logs, and run tests in sibling panes
- wait for specific output before continuing
- wait for another agent to finish
- spawn more agent instances

the `herdr` binary is available in your PATH. its workspace, tab, pane, and agent commands talk to the running herdr instance over a local unix socket.

if you need the raw protocol or full api reference, read the [socket api docs](https://herdr.dev/docs/socket-api/).

## concepts

**workspaces** are project contexts. each workspace has one or more tabs. unless manually renamed, a workspace's label follows the first tab's root pane — usually the repo name, otherwise the root pane's current folder name.

**tabs** are subcontexts inside a workspace. each tab has one or more panes.

**panes** are terminal splits inside a tab. each pane runs its own process — a shell, an agent, a server, anything.

**agent status** is detected automatically by herdr. the api exposes one public field for it:

- `agent_status` — `idle`, `working`, `blocked`, `done`, `unknown`

`done` means the agent finished, but you have not looked at that finished pane yet.

plain shells still exist as panes, but herdr's sidebar agent section intentionally focuses on detected agents rather than listing every shell.

**ids** — workspace IDs look like `w1`, tab IDs like `w1:t1`, and pane IDs like `w1:p1`.
Treat them as opaque handles scoped to one server. Closed pane and tab IDs are not reused.
Moving a pane to another workspace changes its public ID; use
`result.move_result.pane.pane_id` from the move response. Keep the terminal identity for ownership checks.

Agent names must match `[a-z][a-z0-9_-]{0,31}` and be unique among live agents. A name
belongs to the occupant and clears when it exits. Agent commands accept that name or the
pane currently hosting it. A shell can exist without a named agent.

## discover yourself

see what panes exist and which one is focused:

```bash
herdr pane list
```

Your caller pane is `$HERDR_PANE_ID`; focus may belong to the user or another client.
Use explicit returned IDs for other panes and `--current` for your own pane.

list workspaces:

```bash
herdr workspace list
```

## tab management

list tabs in the current workspace:

```bash
herdr tab list --workspace w1
```

create a new tab:

```bash
herdr tab create --workspace w1
```

without `--label`, the new tab keeps the default numbered tab name.

create and name it in one step:

```bash
herdr tab create --workspace w1 --label "logs"
```

rename it:

```bash
herdr tab rename w1:t2 "logs"
```

focus it:

```bash
herdr tab focus w1:t2
```

close it:

```bash
herdr tab close w1:t2
```

## read another pane

see what is on another pane's screen:

```bash
herdr pane read w1:p1 --source recent --lines 50
```

- `--source visible` = current viewport
- `--source recent` = recent scrollback as rendered in the pane
- `--source recent-unwrapped` = recent terminal text with soft wraps joined back together

## split a pane and run a command

split your pane to the right and keep focus on your current pane:

```bash
herdr pane split w1:p2 --direction right --no-focus
```

that prints json with the new pane nested at `result.pane.pane_id`. parse that value, then run a command in that pane:

```bash
NEW_PANE=$(herdr pane split w1:p2 --direction right --no-focus | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
herdr pane run "$NEW_PANE" "npm run dev"
```

split downward instead:

```bash
herdr pane split w1:p2 --direction down --no-focus
```

## wait for output

block until specific text appears in a pane. useful for waiting on servers, builds, and tests.

for `--source recent`, matching uses unwrapped recent terminal text, so pane width and soft wrapping do not break matches. `pane read --source recent` still shows the pane as rendered. if you want to inspect the same transcript that the waiter matches, use `pane read --source recent-unwrapped`.

```bash
herdr pane wait-output w1:p3 --match "ready on port 3000" --timeout 30000
```

with regex:

```bash
herdr pane wait-output w1:p3 --regex "server.*ready" --timeout 30000
```

if it times out, exit code is `1`.

## wait for an agent status

block until another agent reaches a specific status:

```bash
herdr agent wait w1:p1 --until done --timeout 60000
```

The server pins the resolved occupant. Closing that pane returns `agent_not_running` promptly.
Without `--until`, agent waits accept settled idle, done, or blocked states. Reads do not mark
completion seen; explicit focus can change the server's done/idle distinction.

## send text or keys to a pane

send text without pressing Enter:

```bash
herdr pane send-text w1:p1 "hello from claude"
```

press Enter or other keys:

```bash
herdr pane send-keys w1:p1 Enter
```

`pane run` sends the text and then a real `Enter` key in one request:

```bash
herdr pane run w1:p1 "echo hello"
```

## workspace management

create a new workspace:

```bash
herdr workspace create --cwd /path/to/project
```

without `--label`, the new workspace keeps the default cwd-based name.

create and name one in one step:

```bash
herdr workspace create --cwd /path/to/project --label "api server"
```

create one without focusing it:

```bash
herdr workspace create --no-focus
```

focus a workspace:

```bash
herdr workspace focus w2
```

rename:

```bash
herdr workspace rename w1 "api server"
```

close:

```bash
herdr workspace close w2
```

A workspace with linked worktree workspaces requires `--group`. Use that flag only when the
whole group is authorized to close; it expands what is closed. Closing a shell does not stop
the background server. `--no-session` is no longer supported.

## close a pane

```bash
herdr pane close w1:p3
```

## recipes

### run a server and wait until it is ready

```bash
NEW_PANE=$(herdr pane split w1:p2 --direction right --no-focus | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
herdr pane run "$NEW_PANE" "npm run dev"
herdr pane wait-output "$NEW_PANE" --match "ready" --timeout 30000
herdr pane read "$NEW_PANE" --source recent --lines 20
```

### run tests in a separate pane and inspect the result

```bash
herdr pane split w1:p2 --direction down --no-focus
herdr pane run w1:p3 "cargo test"
herdr pane wait-output w1:p3 --match "test result" --timeout 60000
herdr pane read w1:p3 --source recent --lines 30
```

### check what another agent is working on

```bash
herdr pane list
herdr pane read w1:p1 --source recent --lines 80
```

### watch another pane robustly

use this pattern when you need to coordinate with a sibling pane:

```bash
# inspect what is already there
herdr pane read w1:p3 --source recent --lines 40

# wait only for the next output you expect
herdr pane wait-output w1:p3 --match "ready" --timeout 30000

# if you need to inspect the same transcript the waiter matched,
# read the unwrapped recent text directly
herdr pane read w1:p3 --source recent-unwrapped --lines 40
```

### start an agent and give it a task

Create layout first, then start an interactive harness bare. The startup command waits for
input readiness; an inline task that remains busy can exhaust that startup timeout.

```bash
NEW_PANE=$(herdr pane split "$HERDR_PANE_ID" --direction right --no-focus | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
herdr agent start reviewer --kind claude --pane "$NEW_PANE" --timeout 60000
herdr agent prompt reviewer "review the test coverage in src/api/" --wait --timeout 120000
```

`agent start` does not create topology. Pass native harness arguments after `--`.
Noninteractive `codex exec` keeps its positional task. Interactive turn completion is not
proof that an entire assignment is complete; validate the expected result files.

`agent prompt` submits paste and Enter together. A recognized dialog rejects input with
`agent_blocked`. A waited prompt must show working or blocked activity within five seconds,
or it returns `agent_prompt_stalled`. Read the pane before deciding what happened; a timeout
or stalled response does not prove non-delivery, so do not blindly send the same task again.
Use `herdr agent send-keys reviewer esc` for deliberate agent UI control.

UpAgent also waits for Cursor's official SessionStart registration before its first prompt.
The 0.9.5 ready flag can precede an input UI that accepts Enter. Reinstall integrations with
the pinned binary when changing versions; an unfamiliar first-run dialog can look idle.

### coordinate with another agent

```bash
herdr agent wait w1:p1 --until done --timeout 120000
herdr pane read w1:p1 --source recent --lines 100
```

## notes

- `workspace list`, `workspace create`, `tab list`, `tab create`, `tab get`, `tab focus`, `tab rename`, `tab close`, `pane list`, `pane get`, `pane split`, `pane wait-output`, and `agent wait` print json on success.
- `pane read` prints text, not json.
- `pane read --format ansi` or `pane read --ansi` returns a rendered ANSI snapshot for TUI feedback loops.
- `pane read --source recent-unwrapped` is useful when you want to inspect the same unwrapped transcript that `pane wait-output --source recent` matches against.
- `pane send-text`, `pane send-keys`, and `pane run` return JSON acknowledgements.
  A queued command is not proof of execution; inspect output or validate its artifacts.
- parse ids from `workspace create`, `tab create`, and `pane split` responses when you need new ids. `workspace create` returns `result.workspace`, `result.tab`, and `result.root_pane`. `tab create` returns `result.tab` and `result.root_pane`. for `pane split`, the new pane id is at `result.pane.pane_id`.
- use `pane read` for current output that already exists. `pane wait-output` searches existing output immediately and then waits for a match.
- `--no-focus` on split, tab create, and workspace create keeps your current terminal context focused.
- without `--label`, workspace create keeps cwd-based naming and tab create keeps numbered naming.
- `--label` on tab create and workspace create applies the custom name immediately.
- if you are running inside herdr, the `HERDR_ENV` environment variable is set to `1`.

Event subscriptions deliver live events, without replay. On `events_lost` or disconnect,
read current state and reconcile before subscribing again; preserve the original deadline.
A slow reader may lose its subscription, and an observer making no write progress for
30 seconds is disconnected. Use bounded server-owned waits for ordinary coordination.
