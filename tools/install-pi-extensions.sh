#!/usr/bin/env bash
# Install this kit's third-party Pi extensions from a pinned manifest, via the
# native `pi install` path (appends to the `packages` array in
# ~/.pi/agent/settings.json and fetches the source).
#
# Source of truth: .shared-llm/public/llm/pi/common/third-party-extensions.txt
#   - one `pi install` source per non-comment line (e.g. `npm:pi-lens@3.8.45`)
#   - blank lines and `#` comments are ignored
#
# Idempotent: a source already present in `pi list` (matched by package name) is
# skipped, so a second run installs nothing new and exits 0.
#
# Fail loud: any `pi install` failure aborts with a non-zero exit. No `|| true`,
# no masking. A peer-dep / Pi-version gap surfaces as a hard error from `pi`.
#
# Usage:
#   tools/install-pi-extensions.sh            # install everything in the manifest
#
# This installs THIRD-PARTY extensions only. Our OWN authored extensions
# (extensions/*.ts) are symlinked by harness.py sync — never installed. Keep the two
# paths separate (see .shared-llm/public/llm/pi/common/THIRD-PARTY-EXTENSIONS.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${PI_EXTENSIONS_MANIFEST:-$REPO_ROOT/.shared-llm/public/llm/pi/common/third-party-extensions.txt}"

command -v pi >/dev/null 2>&1 || {
	echo "ERROR: 'pi' not on PATH. Install it: npm install -g @earendil-works/pi-coding-agent" >&2
	exit 1
}
[[ -f "$MANIFEST" ]] || {
	echo "ERROR: manifest not found: $MANIFEST" >&2
	exit 1
}

PI_VERSION="$(pi --version 2>&1 | head -1 | tr -d '[:space:]')" # pi prints version to stderr
echo "pi $PI_VERSION — installing third-party extensions from $(basename "$MANIFEST")"

# Snapshot currently-installed package names (the segment after `npm:`/`git:`,
# minus any @version / @ref suffix) so we can skip what is already present.
# `pi list` is authoritative: a failure is a real CLI failure and must abort.
installed_sources="$(pi list)"

# package-name key for a manifest source line, for the skip-check.
#   npm:@scope/pkg@1.2.3 -> @scope/pkg     npm:pkg@1.2.3 -> pkg
#   git:github.com/u/repo@vTag -> github.com/u/repo
name_of() {
	local src="${1#npm:}"
	src="${src#git:}"
	case "$src" in
	@*)
		# A scoped package's leading @ is part of its name; strip a version only
		# when a second @ exists.
		if [[ "${src#@}" == *"@"* ]]; then printf '%s' "${src%@*}"; else printf '%s' "$src"; fi
		;;
	*) printf '%s' "${src%%@*}" ;;
	esac
}

installed_names="$(printf '%s\n' "$installed_sources" |
	grep -oE '(npm|git):[^[:space:]]+' |
	while IFS= read -r source; do
		name_of "$source"
		printf '\n'
	done |
	sort -u || true)"

# pi-hypa is retired, not opt-in. Remove it only when present; unrelated
# user-installed packages stay outside this manifest reconciler's ownership.
if printf '%s\n' "$installed_names" | grep -qxF '@hypabolic/pi-hypa'; then
	echo "  remove  npm:@hypabolic/pi-hypa  (retired)"
	pi remove npm:@hypabolic/pi-hypa
fi

installed_count=0 skipped_count=0 planned_count=0
while IFS= read -r raw || [[ -n "$raw" ]]; do
	line="${raw%%#*}" # strip trailing comment
	line="$(printf '%s' "$line" | tr -d '[:space:]')"
	[[ -z "$line" ]] && continue
	planned_count=$((planned_count + 1))

	key="$(name_of "$line")"
	if printf '%s\n' "$installed_names" | grep -qxF "$key"; then
		echo "  skip   $line  (already present as '$key')"
		skipped_count=$((skipped_count + 1))
		continue
	fi

	echo "  install $line"
	if ! pi install "$line"; then # fail loud — no masking
		echo "ERROR: 'pi install $line' failed (peer-dep / Pi-version gap, or network)." >&2
		echo "       Installed Pi is $PI_VERSION. Check the extension's required Pi version." >&2
		exit 1
	fi
	installed_count=$((installed_count + 1))
done <"$MANIFEST"

echo "done: $installed_count installed, $skipped_count already present ($planned_count in manifest)"
echo "verify: pi list"
