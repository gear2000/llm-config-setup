#!/usr/bin/env bash
# Offline tests for tools/t3-setup.sh. Uses a throwaway HOME; touches no real config and starts no T3 server.
# Run by tools/test_t3_setup.py (`just test`), or directly.
# shellcheck disable=SC2016,SC2034  # assertions are eval strings; globals feed the sourced script
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0
t() { if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }

export HOME; HOME="$(mktemp -d)"
listener_pid=""
trap '[[ -n "$listener_pid" ]] && kill "$listener_pid" 2>/dev/null; rm -rf "$HOME"' EXIT
# shellcheck source=/dev/null
source "$here/t3-setup.sh"

# A real listener on a free high port stands in for "another person's server".
busy="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
python3 -c 'import socket,sys,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(); time.sleep(60)' "$busy" &
listener_pid=$!
for _ in $(seq 1 50); do port_in_use "$busy" && break; sleep 0.1; done
t "test listener is up" 'port_in_use "$busy"'

# --- choosing a port
FIRST_PORT="$busy"; LAST_PORT=$((busy + 20))
WANT_PORT=""; choose_port
t "first free port skips a busy one" '[[ $PORT -gt $busy ]] && ! port_in_use "$PORT"'
WANT_PORT="$busy"
t "--port on someone else's port is refused" '! ( choose_port ) 2>/dev/null'
WANT_PORT="80"
t "--port below 1024 is refused" '! ( choose_port ) 2>/dev/null'
WANT_PORT="abc"
t "--port that is not a number is refused" '! ( choose_port ) 2>/dev/null'

# Free ports from the OS, so the tests never depend on what this machine is running.
read -r p1 p2 p3 < <(python3 -c '
import socket
ss = [socket.socket() for _ in range(3)]
for s in ss: s.bind(("127.0.0.1", 0))
print(*[s.getsockname()[1] for s in ss])')

# --- service.env keeps T3's own lines
mkdir -p "$T3_HOME"
printf 'T3CODE_LAN=false\n' >"$SERVICE_ENV"
set_service_env T3CODE_PORT "$p1" && changed=1 || changed=0
t "port written"                 'grep -qx T3CODE_PORT=$p1 "$SERVICE_ENV" && [[ $changed == 1 ]]'
t "T3's own line kept"           'grep -qx T3CODE_LAN=false "$SERVICE_ENV"'
t "service.env is 600"           '[[ $(stat -c %a "$SERVICE_ENV") == 600 ]]'
set_service_env T3CODE_PORT "$p1" && changed=1 || changed=0
t "same port again: no change"   '[[ $changed == 0 ]]'
set_service_env T3CODE_PORT "$p2" && changed=1 || changed=0
t "new port replaces the old one" '[[ $changed == 1 && $(grep -c ^T3CODE_PORT= "$SERVICE_ENV") == 1 ]] && grep -qx T3CODE_PORT=$p2 "$SERVICE_ENV"'
WANT_PORT=""; choose_port
t "re-run keeps the saved port"  '[[ $PORT == $p2 ]]'
WANT_PORT="$p3"; choose_port
t "--port wins over the saved port" '[[ $PORT == $p3 ]]'

# --- the port held by our own server is not a clash
mkdir -p "$(dirname "$RUNTIME")"
printf '{"version":1,"pid":%s,"port":%s}\n' "$$" "$busy" >"$RUNTIME"
# shellcheck disable=SC2317  # called by choose_port
server_pid_ok() { true; }   # pretend the runtime pid is our own t3code.service
WANT_PORT="$busy"; choose_port
t "--port on our own running server's port is fine" '[[ $PORT == "$busy" ]]'
unset -f server_pid_ok
# shellcheck source=/dev/null
source "$here/t3-setup.sh"
WANT_PORT="$busy"
t "a stale runtime file does not make a port ours" '! ( choose_port ) 2>/dev/null'

# --- runtime file reading and readiness
printf '{"version":1,"pid":4242,"host":"127.0.0.1","port":3773,"origin":"http://127.0.0.1:3773"}\n' >"$RUNTIME"
t "runtime port read"            '[[ $(runtime_port) == 3773 ]]'
t "runtime pid read"             '[[ $(runtime_pid) == 4242 ]]'
t "pid outside t3code.service is not ok" '! server_pid_ok'
server_pid_ok() { true; }
# shellcheck disable=SC2317  # called by server_ready
curl() { echo "$FAKE_HTTP"; }
FAKE_HTTP=503
t "not ready while HTTP answers 503" '! server_ready 3773'
FAKE_HTTP=200
t "ready: right port + HTTP 200" 'server_ready 3773'
t "not ready on another port"    '! server_ready 3774'
unset -f curl

# --- worktree default
seed_worktree_default >/dev/null
t "settings.json seeded"         'grep -q "\"defaultThreadEnvMode\": \"worktree\"" "$SETTINGS"'
echo '{"x":1}' >"$SETTINGS"
seed_worktree_default >/dev/null
t "existing settings.json left alone" '[[ $(cat "$SETTINGS") == "{\"x\":1}" ]]'

# --- T3 Connect: fall back to http2 when the network blocks QUIC
mkdir -p "$(dirname "$RELAY_LOG")"
# shellcheck disable=SC2317  # fakes called by ensure_connect
sleep() { :; }
REACH=0
# shellcheck disable=SC2317
connect_reachable() { [[ $REACH == 1 ]] || grep -q '^TUNNEL_TRANSPORT_PROTOCOL=http2' "$SERVICE_ENV"; }
# shellcheck disable=SC2317  # called by ensure_connect
t3() { echo "t3 $*" >>"$HOME/t3-calls"; }
: >"$RELAY_LOG"
sed -i '/^TUNNEL_TRANSPORT_PROTOCOL=/d' "$SERVICE_ENV"
t "unreachable, no QUIC errors: no switch" '! ensure_connect && ! grep -q TUNNEL_TRANSPORT_PROTOCOL "$SERVICE_ENV"'
echo '{"output":"ERR failed to dial to edge with quic: timeout"}' >"$RELAY_LOG"
t "QUIC blocked: switches to http2 and connects" 'ensure_connect && grep -qx TUNNEL_TRANSPORT_PROTOCOL=http2 "$SERVICE_ENV" && grep -q "t3 service restart" "$HOME/t3-calls"'
t "T3 port line kept by the switch" 'grep -q "^T3CODE_PORT=" "$SERVICE_ENV"'
sed -i 's/^TUNNEL_TRANSPORT_PROTOCOL=http2/TUNNEL_TRANSPORT_PROTOCOL=quic/' "$SERVICE_ENV"
: >"$HOME/t3-calls"
t "user-chosen transport is never overridden" '! ensure_connect && grep -qx TUNNEL_TRANSPORT_PROTOCOL=quic "$SERVICE_ENV" && [[ ! -s "$HOME/t3-calls" ]]'
REACH=1
t "reachable: nothing to do" 'ensure_connect'
unset -f sleep connect_reachable t3

# --- arguments
t "unknown argument refused"     '! ( main --bogus ) 2>/dev/null'
t "--port without a value refused" '! ( main --port ) 2>/dev/null'
t "root is refused"              '! ( id() { [[ $1 == -u ]] && echo 0 || command id "$@"; }; preflight ) >/dev/null 2>&1'

echo; echo "passed $pass, failed $fail"; ((fail == 0))
