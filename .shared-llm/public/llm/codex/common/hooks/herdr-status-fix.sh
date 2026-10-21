#!/bin/sh
# Compatibility entry point for hooks.json files that still reference the 0.7.1 fix.
# Herdr 0.9.5 uses screen detection for Codex state and installs only its official
# SessionStart registration hook. The old working/idle reports would take authority
# away from that detector and can leave a long turn falsely idle.
# Keep this entry point harmless until the operator removes its SessionStart/Stop
# entries. Reinstall the official integration with `herdr integration install codex`.
set -eu
cat >/dev/null
