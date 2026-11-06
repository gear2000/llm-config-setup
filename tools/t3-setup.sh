#!/usr/bin/env bash
# t3-setup.sh: set up YOUR OWN always-on T3 Code server, so you can use it from your phone.
# How-to (for people and for agents): docs/T3-SETUP.md
#
# Run it as yourself (your own Unix login on the server), never as root.
# One admin step comes first, once per person:   sudo loginctl enable-linger <your login>
#
# Usage:
#   bash tools/t3-setup.sh                 set up: install t3, pick a port, start the service,
#                                          then sign in to T3 Connect (prints a link + code)
#   bash tools/t3-setup.sh --port 3780     use this port (default: the one already set, else the
#                                          first free port from 3773 up)
#   bash tools/t3-setup.sh --no-connect    set up the service only; run `t3 up --connect` later
#   bash tools/t3-setup.sh verify          only run the checks
#
# Safe to re-run: it keeps your port and settings and restarts only when the port changes.

set -euo pipefail

T3_HOME="$HOME/.t3"                     # T3's own default home: database, settings, worktrees, program
SERVICE_ENV="$T3_HOME/service.env"      # read by T3's service at every start
SETTINGS="$T3_HOME/userdata/settings.json"
RUNTIME="$T3_HOME/userdata/server-runtime.json"
UNIT="t3code.service"                   # the unit `t3 service install` creates
FIRST_PORT=3773                         # T3's default port
LAST_PORT=3872

PORT=""
WANT_PORT=""
CONNECT=1

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok   %s\n' "$*"; }
warn() { printf '    WARN %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

session_env() {
  # `sudo -u` and `su` sessions often lack this; systemctl --user then fails with "Failed to connect to bus".
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  PATH="$HOME/.local/bin:$PATH"
}

port_in_use() { [[ -n "$(ss -Hltn "sport = :$1" 2>/dev/null)" ]]; }
valid_port()  { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1024 && $1 <= 65535 )); }

# The port this account's own running T3 server holds, if any.
own_port() {
  local p
  p="$(runtime_port)"
  [[ -n "$p" ]] && server_pid_ok && printf '%s' "$p"
  return 0
}

# Readers of files that may not exist yet: a missing file means "nothing set", never an error.
read_file()        { [[ -f "$1" ]] && cat "$1"; return 0; }
service_env_port() { read_file "$SERVICE_ENV" | sed -n 's/^T3CODE_PORT=//p' | tail -n1; }

# --port wins; else the port already in service.env; else the first free port from 3773 up.
choose_port() {
  local current mine p
  current="$(service_env_port)"
  mine="$(own_port)"
  if [[ -n "$WANT_PORT" ]]; then
    valid_port "$WANT_PORT" || die "bad port '$WANT_PORT' (use 1024-65535)"
    p="$WANT_PORT"
  elif [[ -n "$current" ]]; then
    valid_port "$current" || die "$SERVICE_ENV has a bad T3CODE_PORT '$current'"
    p="$current"
  else
    for ((p = FIRST_PORT; p <= LAST_PORT; p++)); do
      [[ "$p" == "$mine" ]] && break
      port_in_use "$p" || break
    done
    ((p <= LAST_PORT)) || die "no free port between $FIRST_PORT and $LAST_PORT; pass --port"
  fi
  # A port held by this account's own T3 server is fine; anything else is a clash.
  if port_in_use "$p" && [[ "$p" != "$mine" ]]; then
    die "port $p is taken by another program or another person's server. Pick another:  --port <n>"
  fi
  PORT="$p"
}

# Set KEY=VALUE in service.env, keeping every other line (T3 writes its own entries there too).
# Returns 0 when the file changed.
set_service_env() {
  local key="$1" value="$2" tmp
  mkdir -p "$T3_HOME"
  [[ -f "$SERVICE_ENV" ]] || : >"$SERVICE_ENV"
  if grep -qx "$key=$value" "$SERVICE_ENV" && [[ "$(grep -c "^$key=" "$SERVICE_ENV")" == 1 ]]; then
    return 1
  fi
  tmp="$(mktemp "$SERVICE_ENV.XXXXXX")"
  { grep -v "^$key=" "$SERVICE_ENV" || true; printf '%s=%s\n' "$key" "$value"; } >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$SERVICE_ENV"
  return 0
}

# ---------------------------------------------------------------- setup steps
preflight() {
  say "1. Checks (you are: $(id -un))"
  [[ "$(id -u)" != 0 ]] || die "do not run this as root. Run it as the person who will use the server:
  sudo -iu <their login> bash $0"
  session_env
  command -v ss >/dev/null || die "ss not found (package iproute2)"

  local linger
  linger="$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null || true)"
  [[ "$linger" == "yes" ]] || die "your server would stop when you log out and not start at boot.
  An admin must run this once:  sudo loginctl enable-linger $(id -un)
  Then run this script again."
  ok "starts at boot (lingering is on)"

  systemctl --user show-environment >/dev/null 2>&1 || die "cannot reach your systemd user manager.
  Log in directly (ssh $(id -un)@<server>) or check that $XDG_RUNTIME_DIR exists."
  ok "systemd user manager reachable"

  if ! command -v t3 >/dev/null; then
    say "Installing T3 Code (official installer from t3.codes)"
    curl -fsSL https://t3.codes/install.sh | sh
    hash -r
    command -v t3 >/dev/null || die "t3 is still not on PATH after installing"
  fi
  ok "t3: $(command -v t3) -> $(readlink -f "$(command -v t3)")"
}

check_logins() {
  say "2. Agent and GitHub logins (yours, in this account)"
  local any=0
  if command -v claude >/dev/null; then
    any=1
    if [[ -f "$HOME/.claude/.credentials.json" ]]; then ok "claude: logged in"
    else warn "claude is installed but not logged in. Run:  claude auth login"; fi
  fi
  if command -v codex >/dev/null; then
    any=1
    if [[ -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ]]; then ok "codex: logged in"
    else warn "codex is installed but not logged in. Run:  codex login"; fi
  fi
  [[ "$any" == 1 ]] || warn "no agent found. Install Claude Code (https://claude.com/product/claude-code) or Codex, then log in"
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then ok "GitHub: gh is logged in"
  else warn "GitHub not set up (optional). Run 'gh auth login', or add a token in the app: Settings > Source control > GitHub"; fi
}

set_port() {
  say "3. Port"
  choose_port
  if set_service_env T3CODE_PORT "$PORT"; then PORT_CHANGED=1; ok "port $PORT saved in $SERVICE_ENV"
  else PORT_CHANGED=0; ok "port $PORT (already set)"; fi
}

seed_worktree_default() {
  say "4. New threads start in a git worktree"
  if [[ -f "$SETTINGS" ]]; then
    if grep -q '"defaultThreadEnvMode"[[:space:]]*:[[:space:]]*"worktree"' "$SETTINGS"; then ok "already set"
    else ok "left your existing settings alone (change it in the app: Settings > Threads > Workspace)"; fi
    return
  fi
  install -d -m 700 "$(dirname "$SETTINGS")"
  printf '{\n  "defaultThreadEnvMode": "worktree"\n}\n' >"$SETTINGS"
  chmod 600 "$SETTINGS"
  ok "set in $SETTINGS"
}

start_service() {
  say "5. Always-on service"
  local was_installed=0
  systemctl --user cat "$UNIT" >/dev/null 2>&1 && was_installed=1
  if [[ "$CONNECT" == 1 ]]; then
    cat <<EOF
    Next, t3 prints a link and a short code for T3 Connect:
      - open the link on your phone or any browser
      - sign in, or create your T3 login there
      - check that the code matches, then approve
    Use the same T3 login in the phone app afterwards.

EOF
    t3 up --connect
  else
    t3 service install
  fi
  if [[ "$was_installed" == 1 && "${PORT_CHANGED:-0}" == 1 ]]; then
    warn "port changed: restarting the service (this stops running agent turns)"
    t3 service restart
  fi
}

# ---------------------------------------------------------------- verify
FAILS=0
check() { # check "ok text" "warn text" command...
  local good="$1" bad="$2"; shift 2
  if "$@"; then ok "$good"; else warn "$bad"; FAILS=$((FAILS + 1)); fi
}
runtime_port()  { read_file "$RUNTIME" | sed -nE 's/.*"port"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -n1; }
runtime_pid()   { read_file "$RUNTIME" | sed -nE 's/.*"pid"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -n1; }
# The service's main process is T3's launcher; the server is its child and writes its own pid.
server_pid_ok() { local pid; pid="$(runtime_pid)"; [[ -n "$pid" ]] && grep -q "/$UNIT" "/proc/$pid/cgroup" 2>/dev/null; }
http_code()     { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$1/" || true; }
# The port opens before the server is ready: it writes its runtime file and stops answering 503 a moment later.
server_ready()  {
  [[ "$(runtime_port)" == "$1" ]] && server_pid_ok || return 1
  command -v curl >/dev/null || return 0
  local code; code="$(http_code "$1")"
  [[ "$code" != 000 && "$code" != 503 ]]
}

verify() {
  say "6. Verify"
  session_env
  local port i
  port="$(service_env_port)"
  [[ -n "$port" ]] || { warn "no T3CODE_PORT in $SERVICE_ENV; run setup first"; FAILS=$((FAILS + 1)); port=$FIRST_PORT; }

  # The first start runs database migrations; give it time.
  for ((i = 0; i < 120; i++)); do server_ready "$port" && break; sleep 1; done

  check "service running"             "service not running. Log: t3 service status" systemctl --user --quiet is-active "$UNIT"
  check "service starts at boot"      "service not enabled. Run: t3 service install" systemctl --user --quiet is-enabled "$UNIT"
  check "server listens on port $port (set in $SERVICE_ENV)" \
        "server is not on port $port (runtime file says '$(runtime_port)')" test "$(runtime_port)" == "$port"
  check "server process belongs to $UNIT (pid $(runtime_pid))" \
        "runtime file is stale or not from $UNIT" server_pid_ok
  if command -v curl >/dev/null; then
    check "web app answers on http://127.0.0.1:$port/" "web app not ready on port $port (status $(http_code "$port"))" server_ready "$port"
  fi

  printf '\n    -- t3 status\n'
  t3 status 2>&1 | sed 's/^/    /' || true

  if ((FAILS > 0)); then die "$FAILS check(s) failed (see WARN lines above)"; fi
  say "All checks passed."
  cat <<EOF

    On your phone:
      1. Install "T3 Code" from the App Store or Google Play (or open https://app.t3.codes)
      2. Sign in with the T3 login you approved the link with
      3. Pick this server from the list and start a thread
    Not connected yet? Run:  t3 up --connect
EOF
}

main() {
  local cmd="setup"
  while (($#)); do
    case "$1" in
      setup|verify) cmd="$1" ;;
      --port) WANT_PORT="${2:-}"; [[ -n "$WANT_PORT" ]] || die "--port needs a number"; shift ;;
      --port=*) WANT_PORT="${1#--port=}" ;;
      --no-connect) CONNECT=0 ;;
      -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
      *) die "unknown argument '$1' (try --help)" ;;
    esac
    shift
  done
  if [[ "$cmd" == verify ]]; then verify; return; fi
  preflight
  check_logins
  set_port
  seed_worktree_default
  start_service
  verify
}

# Run only when executed, so tests can source the functions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
