#!/bin/sh
set -eu
umask 077

REPOSITORY=${KEYCASH_REPOSITORY:-samperez10/keycashpro-v2}
BASE="https://github.com/$REPOSITORY/releases/latest/download"

if [ "${PREFIX:-}" = "/data/data/com.termux/files/usr" ]; then
    PLATFORM=termux
    ARCH=$(uname -m)
    [ "$ARCH" = "aarch64" ] || { echo "No Keycash release is available for Termux $ARCH." >&2; exit 1; }
elif [ -r /etc/alpine-release ]; then
    PLATFORM=alpine
    command -v apk >/dev/null 2>&1 || { echo "Missing required command: apk" >&2; exit 1; }
    ARCH=$(apk --print-arch)
    [ "$ARCH" = "x86_64" ] || { echo "No Keycash release is available for Alpine $ARCH." >&2; exit 1; }
elif [ -r /etc/os-release ]; then
    . /etc/os-release
    [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "26.04" ] || {
        echo "This installer supports Ubuntu 26.04 LTS only." >&2
        exit 1
    }
    PLATFORM=ubuntu-26.04
    command -v dpkg >/dev/null 2>&1 || { echo "Missing required command: dpkg" >&2; exit 1; }
    DPKG_ARCH=$(dpkg --print-architecture)
    [ "$DPKG_ARCH" = "amd64" ] || { echo "No Keycash release is available for Ubuntu $DPKG_ARCH." >&2; exit 1; }
    ARCH=x86_64
else
    echo "This installer supports ARM64 Termux, x86_64 Alpine, and Ubuntu 26.04 LTS amd64." >&2
    exit 1
fi
for command in curl tar sha256sum; do
    command -v "$command" >/dev/null 2>&1 || { echo "Missing required command: $command" >&2; exit 1; }
done
download() {
    url=$1
    destination=$2
    if [ -n "${GH_TOKEN:-}" ]; then
        curl -fsSL -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/octet-stream" "$url" -o "$destination"
    else
        curl -fsSL "$url" -o "$destination"
    fi
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/keycash-install.XXXXXX")
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT HUP INT TERM
NAME="keycash-solver-$PLATFORM-$ARCH.tar.gz"
echo "Downloading Keycash Solver for $PLATFORM $ARCH..."
download "$BASE/$NAME" "$WORK/$NAME"
download "$BASE/$NAME.sha256" "$WORK/$NAME.sha256"
(cd "$WORK" && sha256sum -c "$NAME.sha256")
tar -xzf "$WORK/$NAME" -C "$WORK"
echo "Archive verified. Starting installer..."
sh "$WORK/keycash-solver-$PLATFORM-$ARCH/install.sh" "$@"
