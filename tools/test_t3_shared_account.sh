#!/usr/bin/env bash
# Offline tests for tools/t3-shared-account.sh. Uses a throwaway HOME; touches no real config.
# Run by tools/test_t3_shared_account.py (`just test`), or directly.
# shellcheck disable=SC2016,SC2034  # assertions are eval strings; globals feed the sourced script
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/t3-shared-account.sh"
pass=0; fail=0
t() { if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }

export HOME; HOME="$(mktemp -d)"; trap 'rm -rf "$HOME"' EXIT
mkdir -p "$HOME/.local/bin"; printf '#!/bin/sh\nexit 0\n' >"$HOME/.local/bin/t3"; chmod +x "$HOME/.local/bin/t3"
export GIT_CONFIG_NOSYSTEM=1
unset T3_BIND_HOST T3_USERS
# shellcheck source=/dev/null
source "$script"
T3_BIN="$HOME/.local/bin/t3"; UNIT_PATH="$HOME/.local/bin:/usr/bin:/bin"

# --- user list
t "no users: refused"           '! ( load_users ) 2>/dev/null'
t "bad name: refused"           '! ( T3_USERS="Alice:3773" load_users ) 2>/dev/null'
t "shared port: refused"        '! ( T3_USERS="alice:3773 bob:3773" load_users ) 2>/dev/null'
t "low port: refused"           '! ( T3_USERS="alice:80" load_users ) 2>/dev/null'
T3_USERS="alice:3773 bob-b:3774" load_users
t "two users loaded"            '[[ "${NAMES[*]}" == "alice bob-b" && ${PORT[bob-b]} == 3774 ]]'
t "dash becomes underscore in env var names" '[[ $(var_prefix bob-b) == BOB_B ]]'

# --- dirs + env files (non-interactive)
make_dirs >/dev/null
t "user list saved (600)"       '[[ $(cat "$USERS_FILE") == $'"'"'alice:3773\nbob-b:3774'"'"' && $(stat -c %a "$USERS_FILE") == 600 ]]'
NAMES=(); PORT=(); load_users
t "re-run without T3_USERS uses the saved list" '[[ "${NAMES[*]}" == "alice bob-b" ]]'
export ALICE_GH_TOKEN=github_pat_aliceTOKEN123 ALICE_GIT_NAME="Alice Smith" ALICE_GIT_EMAIL=alice@example.com
export BOB_B_GH_TOKEN=ghp_bobTOKEN456 BOB_B_GIT_NAME="Bob Lee" BOB_B_GIT_EMAIL=bob@example.com
write_env_files </dev/null >/dev/null
t "env dir is 700"              '[[ $(stat -c %a "$CONF_DIR") == 700 ]]'
t "alice env is 600"            '[[ $(stat -c %a "$(env_file alice)") == 600 ]]'
t "alice env has her token"     'grep -qx "GH_TOKEN=github_pat_aliceTOKEN123" "$(env_file alice)"'
t "bob-b env has his"           'grep -qx "GH_TOKEN=ghp_bobTOKEN456" "$(env_file bob-b)"'
t "alice data dir is 700"       '[[ $(stat -c %a "$(data_dir alice)") == 700 ]]'
CHANGED=(); write_env_files </dev/null >/dev/null
t "re-run with same input changes nothing" '[[ ${#CHANGED[@]} == 0 ]]'
unset ALICE_GH_TOKEN; CHANGED=(); write_env_files </dev/null >/dev/null
t "re-run without token keeps the file" 'grep -q aliceTOKEN123 "$(env_file alice)" && [[ -z ${CHANGED[alice]:-} ]]'
t "bad name is refused"         '! ( ALICE_GH_TOKEN=ghp_x ALICE_GIT_NAME="Al \$HOME" write_env_files </dev/null >/dev/null 2>&1 )'

# --- systemd reads the env file the way we wrote it
envtest="$(systemd-run --user --quiet --wait --pipe -p EnvironmentFile="$(env_file alice)" /usr/bin/env 2>/dev/null || true)"
if [[ -n "$envtest" ]]; then
  t "systemd parses GIT_AUTHOR_NAME with a space" 'grep -qx "GIT_AUTHOR_NAME=Alice Smith" <<<"$envtest"'
  t "systemd parses GH_TOKEN"                    'grep -qx "GH_TOKEN=github_pat_aliceTOKEN123" <<<"$envtest"'
else echo "SKIP systemd-run env parse (no user manager)"; fi

# --- units
render_unit alice >"$HOME/t3-alice.service"
render_unit bob-b >"$HOME/t3-bob-b.service"
t "alice unit: port 3773"       'grep -qx "Environment=T3CODE_PORT=3773" "$HOME/t3-alice.service"'
t "bob-b unit: port 3774"       'grep -qx "Environment=T3CODE_PORT=3774" "$HOME/t3-bob-b.service"'
t "alice unit: data dir"        'grep -qx "Environment=T3CODE_HOME=$HOME/alice/t3_data" "$HOME/t3-alice.service"'
t "unit holds no token value"   '! grep -qE "aliceTOKEN123|bobTOKEN456" "$HOME/t3-alice.service" "$HOME/t3-bob-b.service"'
t "unit runs t3 serve"          'grep -qx "ExecStart=$HOME/.local/bin/t3 serve" "$HOME/t3-alice.service"'
if command -v systemd-analyze >/dev/null; then
  out="$(systemd-analyze verify --user "$HOME/t3-alice.service" "$HOME/t3-bob-b.service" 2>&1 || true)"
  t "systemd-analyze verify is clean" '[[ -z "$out" ]] || { echo "$out"; false; }'
else echo "SKIP systemd-analyze (not installed)"; fi

# --- worktree seed
seed_worktree_default >/dev/null
t "settings.json seeded"        'grep -q "\"defaultThreadEnvMode\": \"worktree\"" "$(data_dir alice)/userdata/settings.json"'
echo '{"x":1}' >"$(data_dir bob-b)/userdata/settings.json"
seed_worktree_default >/dev/null 2>&1
t "existing settings.json left alone" '[[ $(cat "$(data_dir bob-b)/userdata/settings.json") == "{\"x\":1}" ]]'

# --- git credential helper, with a stale store helper holding another account
git config --global credential.helper store
printf 'https://old-shared-user:OLDPASSWORD@github.com\n' >"$HOME/.git-credentials"
fill() { printf 'protocol=https\nhost=github.com\n\n' | env GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false "$@" git credential fill 2>/dev/null; }
t "before: store answers with the old account" 'fill GH_TOKEN=x | grep -q OLDPASSWORD'
configure_git_helper >/dev/null
configure_git_helper >/dev/null   # idempotent
t "helper set exactly twice (reset + ours)" '[[ $(git config --global --get-all "$GIT_HELPER_KEY" | wc -l) == 2 ]]'
t "alice's token answers"       'fill GH_TOKEN=github_pat_aliceTOKEN123 | grep -qx password=github_pat_aliceTOKEN123'
t "bob's token answers"         'fill GH_TOKEN=ghp_bobTOKEN456 | grep -qx password=ghp_bobTOKEN456'
t "old stored account never answers for github.com" '! fill GH_TOKEN=y | grep -q OLDPASSWORD'
t "no GH_TOKEN: no silent fallback" '! fill env -u GH_TOKEN | grep -q password='
t "other hosts still use store" 'printf "https://u:P@gitlab.com\n" >>"$HOME/.git-credentials"; printf "protocol=https\nhost=gitlab.com\n\n" | GIT_TERMINAL_PROMPT=0 git credential fill 2>/dev/null | grep -qx password=P'
t "verify() helper probe passes" 'helper_answers'

# --- bind host survives a re-run without T3_BIND_HOST
( BIND_HOST=100.64.0.1; render_unit alice ) >"$UNIT_DIR/t3-alice.service"
NAMES=(); PORT=(); load_users
t "re-run keeps the Tailscale bind host" '[[ $BIND_HOST == 100.64.0.1 ]]'
rm -f "$UNIT_DIR/t3-alice.service"
NAMES=(); PORT=(); load_users
t "no unit yet: loopback default" '[[ $BIND_HOST == 127.0.0.1 ]]'
NAMES=(); PORT=(); T3_BIND_HOST=100.99.0.2 load_users
t "T3_BIND_HOST wins when given" '[[ $BIND_HOST == 100.99.0.2 ]]'

# --- readiness: the port opens before the runtime file is written and before HTTP stops answering 503
rt="$(data_dir alice)/userdata/server-runtime.json"
rm -f "$rt"
# shellcheck disable=SC2317  # called by server_ready in the sourced script
curl() { echo "$FAKE_HTTP"; }
FAKE_HTTP=200
t "not ready: no runtime file yet"  '! server_ready alice'
printf '{"version":1,"pid":4242,"host":"127.0.0.1","port":3773}\n' >"$rt"
FAKE_HTTP=503
t "not ready: HTTP still 503"       '! server_ready alice'
FAKE_HTTP=200
t "ready: runtime file + HTTP 200"  'server_ready alice'
t "runtime pid matches"             'runtime_has_pid "$rt" 4242'
t "runtime pid mismatch caught"     '! runtime_has_pid "$rt" 424'
t "port 3773 is not 37730"          '! runtime_has_port "$rt" 377'
unset -f curl

# --- connect refuses unknown names
t "connect: unknown user refused" '! ( connect_user carol ) 2>/dev/null'

echo; echo "passed $pass, failed $fail"; ((fail == 0))
