#!/bin/sh
# Ovis helpers behind `just ovis-pair` and `just ovis-token`.
#
# Ovis (the browser and phone client for herdr) keeps its settings, OVIS_TOKEN included,
# in the folder `herdr plugin config-dir devswha.ovis` names, and its code under a plugin
# root that moves when Ovis updates. Both are looked up through herdr, so nobody has to
# know either path.
#   pair   print a one-time pairing PIN (10 minutes, one device), the phone address and a QR
#   token  print the OVIS_TOKEN Ovis reads (`.env` wins over `env`, as it does in Ovis)
set -eu

PLUGIN_ID=devswha.ovis

die() {
    printf 'ovis: %s\n' "$1" >&2
    exit 1
}

usage() {
    printf 'usage: %s pair|token\n' "$0" >&2
    exit 2
}

plugin_root() {
    herdr plugin list --json | python3 -I -c '
import json, sys
plugins = json.load(sys.stdin).get("result", {}).get("plugins", [])
roots = [p.get("plugin_root") for p in plugins if p.get("plugin_id") == sys.argv[1]]
print(roots[0] if roots and roots[0] else "")
' "$PLUGIN_ID"
}

case "${1:-}" in
    pair | token) ;;
    *) usage ;;
esac
command -v herdr >/dev/null 2>&1 || die "herdr is not on PATH"

case "$1" in
    pair)
        root=$(plugin_root)
        [ -n "$root" ] || die "Ovis is not installed as a herdr plugin ($PLUGIN_ID)"
        cd "$root"
        exec sh "$root/scripts/with-bun.sh" "$root/scripts/plugin.ts" pair
        ;;
    token)
        dir=$(herdr plugin config-dir "$PLUGIN_ID")
        [ -n "$dir" ] || die "Ovis is not installed as a herdr plugin ($PLUGIN_ID)"
        value=""
        for file in "$dir/env" "$dir/.env"; do
            [ -f "$file" ] || continue
            found=$(sed -n 's/^OVIS_TOKEN=//p' "$file" | tail -n 1)
            if [ -n "$found" ]; then value=$found; fi
        done
        [ -n "$value" ] || die "no OVIS_TOKEN is set; add OVIS_TOKEN=<long random value> to $dir/env, then restart Ovis"
        printf '%s\n' "$value"
        ;;
esac
