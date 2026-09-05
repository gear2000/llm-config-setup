#!/usr/bin/env bash
# Pin Herdr to the version this kit's UpAgent hub is tested against.
#
# UpAgent's recruiter was written against Herdr 0.7.1. A newer Herdr (including
# whatever https://herdr.dev/install.sh currently ships) breaks the hub: wait
# subscriptions go silent when a pane vanishes, and later protocol/CLI changes
# are untested here. Do not run `herdr update`, `brew upgrade herdr`, or the
# unpinned herdr.dev installer on a machine that runs this kit.
#
# Source of the binary: GitHub release tag v0.7.1, not herdr.dev/latest.json.
#
# Usage:
#   tools/install-herdr.sh           # install 0.7.1 if missing; no-op if already 0.7.1
#   tools/install-herdr.sh --check   # verify the pin, mutate nothing
#   tools/install-herdr.sh --force   # replace a different installed version with 0.7.1
#
# Override the pin only for a deliberate kit-side bump (then re-test UpAgent):
#   HERDR_PIN=0.7.1 tools/install-herdr.sh

set -euo pipefail

PINNED="${HERDR_PIN:-0.7.1}"
INSTALL_DIR="${HERDR_INSTALL_DIR:-$HOME/.local/bin}"
RELEASE_BASE="https://github.com/herdrdev/herdr/releases/download/v${PINNED}"

CHECK_ONLY=0
FORCE=0
for arg in "$@"; do
	case "$arg" in
	--check) CHECK_ONLY=1 ;;
	--force) FORCE=1 ;;
	--) ;;
	-h | --help)
		sed -n '12,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,2\}//'
		exit 0
		;;
	*)
		echo "ERROR: unknown argument '$arg'." >&2
		echo "       Supported: --check, --force, --help." >&2
		exit 2
		;;
	esac
done

installed_version() {
	if ! command -v herdr >/dev/null 2>&1; then
		return 1
	fi
	herdr --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

fail_wrong_version() {
	local got="$1"
	echo "ERROR: Herdr on PATH is ${got:-missing}, this kit requires ${PINNED}." >&2
	if command -v herdr >/dev/null 2>&1; then
		echo "       binary: $(command -v herdr)" >&2
	fi
	echo "       A newer Herdr breaks the UpAgent hub. Do not run \`herdr update\`." >&2
	echo "       Install the pin: bash tools/install-herdr.sh --force" >&2
	echo "       Official unpinned installer (do not use): curl -fsSL https://herdr.dev/install.sh | sh" >&2
	exit 1
}

if ((CHECK_ONLY)); then
	got="$(installed_version || true)"
	if [[ "$got" != "$PINNED" ]]; then
		fail_wrong_version "$got"
	fi
	echo "herdr ${PINNED} ($(command -v herdr))"
	exit 0
fi

got="$(installed_version || true)"
if [[ "$got" == "$PINNED" ]]; then
	echo "herdr ${PINNED} already on PATH ($(command -v herdr))"
	exit 0
fi
if [[ -n "$got" ]] && ((!FORCE)); then
	fail_wrong_version "$got"
fi

OS="$(uname -s)"
case "$OS" in
Linux) os="linux" ;;
Darwin) os="macos" ;;
*)
	echo "ERROR: unsupported OS: $OS (need Linux or macOS)" >&2
	exit 1
	;;
esac

ARCH="$(uname -m)"
case "$ARCH" in
x86_64 | amd64) arch="x86_64" ;;
aarch64 | arm64) arch="aarch64" ;;
*)
	echo "ERROR: unsupported architecture: $ARCH" >&2
	exit 1
	;;
esac

asset="herdr-${os}-${arch}"
url="${RELEASE_BASE}/${asset}"
echo "installing Herdr ${PINNED} (${asset}) into ${INSTALL_DIR}"

command -v curl >/dev/null 2>&1 || {
	echo "ERROR: requires curl" >&2
	exit 1
}

mkdir -p "$INSTALL_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if ! curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "$url" -o "${tmp}/herdr"; then
	echo "ERROR: download failed from ${url}" >&2
	echo "       Manual fallback: https://github.com/herdrdev/herdr/releases/tag/v${PINNED}" >&2
	exit 1
fi
chmod +x "${tmp}/herdr"
mv "${tmp}/herdr" "${INSTALL_DIR}/herdr"

case ":${PATH}:" in
*":${INSTALL_DIR}:"*) ;;
*)
	echo "WARNING: ${INSTALL_DIR} is not in PATH. Add:" >&2
	echo "  export PATH=\"${INSTALL_DIR}:\$PATH\"" >&2
	;;
esac

export PATH="${INSTALL_DIR}:${PATH}"
got="$(installed_version || true)"
if [[ "$got" != "$PINNED" ]]; then
	echo "ERROR: installed ${INSTALL_DIR}/herdr but PATH still resolves to ${got:-missing}." >&2
	if command -v herdr >/dev/null 2>&1; then
		echo "       which herdr: $(command -v herdr)" >&2
	fi
	echo "       Put ${INSTALL_DIR} first on PATH, or remove the other herdr binary." >&2
	exit 1
fi

echo "installed herdr ${PINNED} ($(command -v herdr))"
echo "do not run: herdr update   (that would leave 0.7.1)"
