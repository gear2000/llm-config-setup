#!/usr/bin/env bash
# t3-shared-account.sh: always-on T3 Code servers for several people under ONE Unix account.
#
# Each person gets their own T3 server (own port, own data folder, own GitHub token).
# All servers share the account's agent logins (claude, codex), because HOME stays the same.
# How-to: docs/T3-SHARED-ACCOUNT.md
#
#   <account home>/
#   ├── <name>/                     that person's projects
#   │   └── t3_data/                T3 database, settings, worktrees (T3CODE_HOME)
#   └── .config/
#       ├── t3-users/users          the saved user list ("name:port" per line)
#       ├── t3-users/<name>.env     GH_TOKEN + git identity (mode 600)
#       └── systemd/user/t3-<name>.service
#
# Run as the shared account, from a login shell (ssh <account>@host, or sudo -iu <account>).
# Root is needed once, before the first run:   sudo loginctl enable-linger <account>
#
# Usage:
#   T3_USERS="alice:3773 bob:3774" ./t3-shared-account.sh    set up (safe to re-run; the list is saved)
#   ./t3-shared-account.sh                                   re-run with the saved list
#   ./t3-shared-account.sh verify                            only run the checks
#   ./t3-shared-account.sh connect alice                     link alice's server to alice's own T3 account
#
# Inputs, asked for (hidden) when unset and a terminal is attached. NAME is the user name in capitals:
#   NAME_GH_TOKEN   NAME_GIT_NAME   NAME_GIT_EMAIL
#   T3_BIND_HOST    address to listen on (default: the one already in the units, else 127.0.0.1).
#                   T3 Connect works with 127.0.0.1; set a Tailscale 100.x address only for direct tailnet access.
# On a re-run, a user's env file is kept unless you pass that user's token again.

set -euo pipefail

ACCOUNT="$(id -un)"
CONF_DIR="$HOME/.config/t3-users"
USERS_FILE="$CONF_DIR/users"
UNIT_DIR="$HOME/.config/systemd/user"
NAMES=()
declare -A PORT=()
declare -A CHANGED=()
BIND_HOST=""
T3_BIN=""      # set by preflight
UNIT_PATH=""   # set by preflight

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok   %s\n' "$*"; }
warn() { printf '    WARN %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

user_home() { printf '%s/%s' "$HOME" "$1"; }
data_dir()  { printf '%s/%s/t3_data' "$HOME" "$1"; }
env_file()  { printf '%s/%s.env' "$CONF_DIR" "$1"; }
unit_name() { printf 't3-%s.service' "$1"; }
var_prefix() { local p="${1^^}"; printf '%s' "${p//-/_}"; }

port_in_use() { [[ -n "$(ss -Hltn "sport = :$1" 2>/dev/null)" ]]; }

# Users come from T3_USERS ("name:port ..."), else from the list saved by an earlier run.
load_users() {
  local spec="${T3_USERS:-}" entry name port
  if [[ -z "$spec" && -f "$USERS_FILE" ]]; then spec="$(tr '\n' ' ' <"$USERS_FILE")"; fi
  [[ -n "${spec// /}" ]] || die 'no users given. Example:  T3_USERS="alice:3773 bob:3774" '"$0"
  NAMES=(); PORT=()
  for entry in $spec; do
    name="${entry%%:*}"; port="${entry#*:}"
    [[ "$name" =~ ^[a-z][a-z0-9_-]{0,30}$ ]] || die "bad user name '$name' (use lowercase letters, digits, - or _)"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
      die "bad port '$port' for $name (use 1024-65535)"
    fi
    [[ -z "${PORT[$name]:-}" ]] || die "user '$name' is listed twice"
    local other; for other in "${NAMES[@]}"; do [[ "${PORT[$other]}" != "$port" ]] || die "port $port is given to both $other and $name"; done
    NAMES+=("$name"); PORT[$name]="$port"
  done
  # Keep the address chosen on an earlier run, so a plain re-run or `verify` never moves a server back to loopback.
  BIND_HOST="${T3_BIND_HOST:-$(sed -n 's/^Environment=T3CODE_HOST=//p' "$UNIT_DIR/$(unit_name "${NAMES[0]}")" 2>/dev/null | head -n1 || true)}"
  BIND_HOST="${BIND_HOST:-127.0.0.1}"
}

save_users() {
  local name
  for name in "${NAMES[@]}"; do printf '%s:%s\n' "$name" "${PORT[$name]}"; done | write_if_changed "$USERS_FILE" 600 || true
}

# Write stdin to $1 with mode $2. Returns 0 when the content changed, 1 when it did not.
write_if_changed() {
  local target="$1" mode="$2" tmp
  tmp="$(mktemp "${target}.XXXXXX")"   # mktemp creates the file 0600, so a secret is never readable mid-write
  cat >"$tmp"
  chmod "$mode" "$tmp"
  if [[ -f "$target" ]] && cmp -s "$tmp" "$target"; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$target"
  return 0
}

session_env() {
  # `sudo -u` and `su` sessions often lack this; systemctl --user then fails with "Failed to connect to bus".
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  PATH="$HOME/.local/bin:$PATH"
}

# ---------------------------------------------------------------- 0. preflight
preflight() {
  say "0. Preflight (account: $ACCOUNT, users: ${NAMES[*]})"
  [[ "$(id -u)" != 0 ]] || die "do not run this as root. Run it as the shared account: sudo -iu <account> $0"
  local passwd_home
  passwd_home="$(getent passwd "$ACCOUNT" | cut -d: -f6)"
  [[ "$HOME" == "$passwd_home" ]] || die "HOME is '$HOME', but $ACCOUNT's home is '$passwd_home'. Use a login shell: sudo -iu $ACCOUNT"
  session_env

  local linger
  linger="$(loginctl show-user "$ACCOUNT" --property=Linger --value 2>/dev/null || true)"
  [[ "$linger" == "yes" ]] || die "lingering is off, so the servers would stop at logout and not start at boot.
  Fix (as an admin):  sudo loginctl enable-linger $ACCOUNT
  Then re-run this script as $ACCOUNT."
  ok "lingering is on"

  systemctl --user show-environment >/dev/null 2>&1 || die "cannot reach the systemd user manager.
  Check: ls -ld $XDG_RUNTIME_DIR   (exists once lingering is on)
  Or log in directly: ssh $ACCOUNT@<host>"
  ok "systemd user manager reachable"

  T3_BIN="$(command -v t3 || true)"
  [[ -n "$T3_BIN" && -x "$T3_BIN" ]] || die "t3 not found. Install it as $ACCOUNT:  curl -fsSL https://t3.codes/install.sh | sh"
  # Show the install path (~/.t3/runtime/versions/<v>/t3). Never run bare `t3`: it starts a server.
  ok "t3: $T3_BIN -> $(readlink -f "$T3_BIN")"
  command -v git >/dev/null || die "git not found"
  command -v ss  >/dev/null || die "ss not found (package iproute2)"

  local found_provider=0
  if command -v claude >/dev/null; then
    found_provider=1
    if [[ -f "$HOME/.claude/.credentials.json" ]]; then ok "claude: logged in (~/.claude/.credentials.json)"
    else warn "claude found but ~/.claude/.credentials.json is missing; run 'claude auth login' as $ACCOUNT"; fi
  fi
  if command -v codex >/dev/null; then
    found_provider=1
    if [[ -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ]]; then ok "codex: logged in (~/.codex/auth.json)"
    else warn "codex found but ~/.codex/auth.json is missing; run 'codex login' as $ACCOUNT"; fi
  fi
  [[ "$found_provider" == 1 ]] || warn "neither claude nor codex is on PATH; T3 will have no agent to run"

  # T3's built-in service owns ~/.t3 and T3's default port 3773.
  if systemctl --user is-enabled t3code.service >/dev/null 2>&1 \
     || systemctl --user is-active t3code.service >/dev/null 2>&1; then
    die "T3's built-in t3code.service is installed for $ACCOUNT. It takes port 3773 and ~/.t3.
  Remove it first:  t3 service uninstall"
  fi
  ok "no built-in t3code.service"

  local name
  for name in "${NAMES[@]}"; do
    if port_in_use "${PORT[$name]}" && ! systemctl --user is-active "$(unit_name "$name")" >/dev/null 2>&1; then
      die "port ${PORT[$name]} ($name) is already taken by something else:
$(ss -Hltnp "sport = :${PORT[$name]}" 2>/dev/null)"
    fi
  done
  ok "ports are free (or held by our own units)"

  # Service units do not get the login shell PATH. Pin the dirs of every tool T3 may launch.
  local tool dir parts=()
  for tool in t3 claude codex gh git node npm bun cursor-agent opencode; do
    if command -v "$tool" >/dev/null; then
      dir="$(dirname "$(command -v "$tool")")"
      [[ " ${parts[*]-} " == *" $dir "* ]] || parts+=("$dir")
    fi
  done
  for dir in /usr/local/sbin /usr/local/bin /usr/sbin /usr/bin /sbin /bin; do
    [[ " ${parts[*]-} " == *" $dir "* ]] || parts+=("$dir")
  done
  UNIT_PATH="$(IFS=:; printf '%s' "${parts[*]}")"
  ok "unit PATH: $UNIT_PATH"
}

# ---------------------------------------------------------------- 1. folders
make_dirs() {
  say "1. Folders"
  local name
  install -d -m 700 "$CONF_DIR"
  mkdir -p "$UNIT_DIR"
  save_users
  for name in "${NAMES[@]}"; do
    mkdir -p "$(user_home "$name")"
    install -d -m 700 "$(data_dir "$name")"
    ok "$(user_home "$name")/  and  $(data_dir "$name")/"
  done
}

# ---------------------------------------------------------------- 2. per-user env files
ask() { # ask VAR "prompt" [secret]
  local var="$1" prompt="$2" secret="${3:-}" value="${!1:-}"
  if [[ -z "$value" ]]; then
    [[ -t 0 ]] || die "$var is not set and there is no terminal to ask on"
    if [[ -n "$secret" ]]; then read -rsp "    $prompt: " value; echo
    else read -rp "    $prompt: " value; fi
  fi
  printf -v "$var" '%s' "$value"
}

check_value() { # systemd EnvironmentFile safety: no quotes, backslashes, $, backticks or newlines
  [[ -n "$2" && "$2" != *[\"\\\$\`]* && "$2" != *$'\n'* ]] || die "$1 is empty or contains \" \\ \$ \` or a newline"
}

write_env_files() {
  say "2. Per-user env files (mode 600, in $CONF_DIR)"
  local name up file tok gname gemail
  for name in "${NAMES[@]}"; do
    up="$(var_prefix "$name")"
    file="$(env_file "$name")"
    tok="${up}_GH_TOKEN"; gname="${up}_GIT_NAME"; gemail="${up}_GIT_EMAIL"
    if [[ -f "$file" && -z "${!tok:-}" ]]; then
      ok "$file kept (pass ${tok} to replace it)"
      continue
    fi
    ask "$tok" "$name: GitHub token" secret
    ask "$gname" "$name: git author name (e.g. Alice Smith)"
    ask "$gemail" "$name: git author email"
    [[ "${!tok}" =~ ^[A-Za-z0-9_]+$ ]] || die "$tok does not look like a GitHub token"
    check_value "$gname" "${!gname}"
    check_value "$gemail" "${!gemail}"
    if write_if_changed "$file" 600 <<EOF
# T3 Code env for $name. Read by $(unit_name "$name"). Keep mode 600.
GH_TOKEN=${!tok}
GIT_AUTHOR_NAME="${!gname}"
GIT_AUTHOR_EMAIL=${!gemail}
GIT_COMMITTER_NAME="${!gname}"
GIT_COMMITTER_EMAIL=${!gemail}
EOF
    then CHANGED[$name]=1; ok "$file written"
    else ok "$file unchanged"; fi
  done
}

# ---------------------------------------------------------------- 3. git push uses the server's GH_TOKEN
GIT_HELPER_KEY='credential.https://github.com.helper'
# shellcheck disable=SC2016  # $1 and $GH_TOKEN must expand when git runs the helper, not now.
GIT_HELPER_CMD='!f() { test "$1" = get || exit 0; test -n "$GH_TOKEN" || exit 0; echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'

configure_git_helper() {
  say "3. Git credential helper (HTTPS pushes to github.com use the server's own GH_TOKEN)"
  git config --global --unset-all "$GIT_HELPER_KEY" >/dev/null 2>&1 || true
  # The empty entry clears helpers set earlier (store, gh, a system one) so they cannot answer with another account.
  git config --global --add "$GIT_HELPER_KEY" ''
  git config --global --add "$GIT_HELPER_KEY" "$GIT_HELPER_CMD"
  ok "set in ~/.gitconfig: [credential \"https://github.com\"]"
}

# ---------------------------------------------------------------- 4. new threads default to a git worktree
seed_worktree_default() {
  say "4. Default new threads to 'New worktree'"
  local name settings
  for name in "${NAMES[@]}"; do
    settings="$(data_dir "$name")/userdata/settings.json"
    if [[ -f "$settings" ]]; then
      if grep -q '"defaultThreadEnvMode"[[:space:]]*:[[:space:]]*"worktree"' "$settings"; then
        ok "$name: already set"
      else
        warn "$name: $settings exists, left alone. Set it in the app: Settings > Threads > Workspace > New worktree"
      fi
      continue
    fi
    install -d -m 700 "$(dirname "$settings")"
    printf '{\n  "defaultThreadEnvMode": "worktree"\n}\n' >"$settings"
    chmod 600 "$settings"
    ok "$name: seeded $settings"
  done
}

# ---------------------------------------------------------------- 5. systemd user units
render_unit() { # render_unit NAME
  local name="$1"
  cat <<EOF
# T3 Code server for $name. Generated by t3-shared-account.sh; re-run the script rather than editing.
[Unit]
Description=T3 Code server ($name, port ${PORT[$name]})
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$(user_home "$name")
# HOME stays $HOME, so every server shares the account's claude/codex logins.
# Do not set CLAUDE_CONFIG_DIR or CODEX_HOME here.
Environment=T3CODE_HOME=$(data_dir "$name")
Environment=T3CODE_PORT=${PORT[$name]}
Environment=T3CODE_HOST=$BIND_HOST
Environment=T3CODE_AUTO_BOOTSTRAP_PROJECT_FROM_CWD=false
Environment="PATH=$UNIT_PATH"
# GH_TOKEN and GIT_AUTHOR_* / GIT_COMMITTER_* (kept out of this file: units are readable, env files are mode 600)
EnvironmentFile=$(env_file "$name")
ExecStart=$T3_BIN serve
# Same choices as T3's own unit: stop the server first, survive an OOM-killed agent, always come back.
KillMode=mixed
OOMPolicy=continue
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
}

install_units() {
  say "5. systemd user units in $UNIT_DIR"
  local name unit
  for name in "${NAMES[@]}"; do
    unit="$UNIT_DIR/$(unit_name "$name")"
    if render_unit "$name" | write_if_changed "$unit" 644; then
      CHANGED[$name]=1; ok "$unit written"
    else
      ok "$unit unchanged"
    fi
  done
  systemctl --user daemon-reload
  for name in "${NAMES[@]}"; do
    unit="$(unit_name "$name")"
    systemctl --user --quiet enable "$unit"
    if ! systemctl --user is-active "$unit" >/dev/null 2>&1; then
      systemctl --user start "$unit"; ok "$unit enabled and started"
    elif [[ -n "${CHANGED[$name]:-}" ]]; then
      warn "$unit config changed: restarting (this stops $name's running agent turns)"
      systemctl --user restart "$unit"
    else
      ok "$unit already running with current config"
    fi
  done
}

# ---------------------------------------------------------------- 6. verify
FAILS=0
ENVDUMP=""
check() { # check "ok text" "warn text" command...
  local good="$1" bad="$2"; shift 2
  if "$@"; then ok "$good"; else warn "$bad"; FAILS=$((FAILS + 1)); fi
}
runtime_has_port() { [[ -f "$1" ]] && grep -Eq "\"port\"[[:space:]]*:[[:space:]]*$2([^0-9]|\$)" "$1"; }
runtime_has_pid()  { [[ -f "$1" ]] && grep -Eq "\"pid\"[[:space:]]*:[[:space:]]*$2([^0-9]|\$)" "$1"; }
http_code()        { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$BIND_HOST:$1/" || true; }
# The port opens before the server is ready: it writes its runtime file and stops answering 503 a moment later.
server_ready() { # server_ready NAME
  local port="${PORT[$1]}" code
  runtime_has_port "$(data_dir "$1")/userdata/server-runtime.json" "$port" || return 1
  command -v curl >/dev/null || return 0
  code="$(http_code "$port")"
  [[ "$code" != 000 && "$code" != 503 ]]
}
env_has()          { grep -Eq "$1" <<<"$ENVDUMP"; }
helper_answers()   {
  printf 'protocol=https\nhost=github.com\n\n' \
    | GH_TOKEN=verify-probe GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false \
      git credential fill 2>/dev/null | grep -qx 'password=verify-probe'
}

verify() {
  say "6. Verify"
  session_env
  local name unit port pid i code runtime
  FAILS=0
  for name in "${NAMES[@]}"; do
    unit="$(unit_name "$name")"; port="${PORT[$name]}"
    printf '\n    -- %s (%s, port %s)\n' "$name" "$unit" "$port"

    # The first start runs database migrations; give it time.
    for ((i = 0; i < 120; i++)); do server_ready "$name" && break; sleep 1; done
    if ! port_in_use "$port"; then
      warn "nothing listens on $port after 120s. Log: journalctl --user -u $unit -n 50"
      FAILS=$((FAILS + 1)); continue
    fi
    ok "listening on $(ss -Hltn "sport = :$port" | awk '{print $4}' | paste -sd' ')"
    check "unit active" "unit not active" systemctl --user --quiet is-active "$unit"
    check "unit enabled (starts at boot)" "unit not enabled" systemctl --user --quiet is-enabled "$unit"

    # Proves T3CODE_HOME took effect: the running server wrote its runtime file under this user's data folder.
    runtime="$(data_dir "$name")/userdata/server-runtime.json"
    pid="$(systemctl --user show -p MainPID --value "$unit")"
    check "data folder in use: $runtime records port $port" \
          "$runtime missing or does not record port $port (still starting? log: journalctl --user -u $unit)" \
          runtime_has_port "$runtime" "$port"
    check "runtime file belongs to the running server (pid $pid)" \
          "runtime file pid is not the unit's MainPID $pid (stale file?)" runtime_has_pid "$runtime" "$pid"

    if [[ -n "$pid" && "$pid" != 0 && -r "/proc/$pid/environ" ]]; then
      ENVDUMP="$(tr '\0' '\n' <"/proc/$pid/environ")"
      check "GH_TOKEN present in the server env (value not shown)" "GH_TOKEN missing from the server env" env_has '^GH_TOKEN=.'
      check "T3CODE_HOME=$(data_dir "$name")" "T3CODE_HOME not set as expected" env_has "^T3CODE_HOME=$(data_dir "$name")\$"
      check "git identity: $(grep '^GIT_AUTHOR_EMAIL=' <<<"$ENVDUMP" || true)" "GIT_AUTHOR_EMAIL missing" env_has '^GIT_AUTHOR_EMAIL=.'
      ENVDUMP=""
    else
      warn "cannot read the server's environment (MainPID=$pid)"; FAILS=$((FAILS + 1))
    fi

    if command -v curl >/dev/null; then
      code="$(http_code "$port")"
      check "web app answers on http://$BIND_HOST:$port/ (status $code)" \
            "web app not ready on http://$BIND_HOST:$port/ (status $code)" server_ready "$name"
    fi
  done

  printf '\n    -- shared checks\n'
  check "default ~/.t3 holds no database (only the t3 program lives there)" \
        "$HOME/.t3/userdata/statev2.sqlite exists: something ran T3 without T3CODE_HOME (a bare 't3' or 't3 serve')" \
        test ! -e "$HOME/.t3/userdata/statev2.sqlite"
  check "git push to github.com will use the server's GH_TOKEN" \
        "git credential helper did not return GH_TOKEN; check: git config --global --get-all $GIT_HELPER_KEY" \
        helper_answers

  print_next_steps
  ((FAILS == 0)) || die "$FAILS check(s) failed (see WARN lines above)"
  say "All checks passed."
}

print_next_steps() {
  local name
  say "Next: connect each person (they sign in to the T3 app; no SSH for them)"
  for name in "${NAMES[@]}"; do printf '    %s connect %s\n' "$0" "$name"; done
  cat <<EOF

    Never run these as $ACCOUNT: 't3' (bare), 't3 up', 't3 connect' (bare), 't3 service install', 't3 uninstall'.
    After 't3 update':  systemctl --user restart t3-<name>   (every user)
EOF
}

# ---------------------------------------------------------------- connect
# Link one person's server to THEIR OWN T3 account, then restart it.
# The sign-in is stored in that person's data folder, so each server shows up only in its owner's app.
connect_user() {
  local name="${1:-}"
  [[ -n "$name" && -n "${PORT[$name]:-}" ]] || die "usage: $0 connect <${NAMES[*]}>"
  session_env
  say "Linking $name's server to $name's T3 account"
  cat <<EOF
    t3 prints a link and a short code below. Send both to $name.
    $name opens the link, signs in (or creates a T3 account), checks the code matches, and approves.
EOF
  t3 connect link --base-dir "$(data_dir "$name")"
  systemctl --user restart "$(unit_name "$name")"
  ok "$(unit_name "$name") restarted with T3 Connect on"
  cat <<EOF

    Tell $name:
      1. Install the T3 Code app (iPhone, Android or desktop), or open https://app.t3.codes
      2. Sign in with the same T3 account
      3. Pick the server from the list
EOF
}

main() {
  case "${1:-setup}" in
    setup)
      load_users
      preflight
      make_dirs
      write_env_files
      configure_git_helper
      seed_worktree_default
      install_units
      verify
      ;;
    verify)  load_users; verify ;;
    connect) load_users; connect_user "${2:-}" ;;
    *) die "usage: $0 [setup|verify|connect <name>]" ;;
  esac
}

# Run only when executed, so tests can source the functions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
