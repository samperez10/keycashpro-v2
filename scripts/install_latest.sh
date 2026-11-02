#!/bin/sh
set -eu
umask 077

REPOSITORY=${KEYCASH_REPOSITORY:-samperez10/keycashpro-v2}
BASE="https://github.com/$REPOSITORY/releases/latest/download"

[ -r /etc/alpine-release ] || { echo "This installer requires Alpine Linux." >&2; exit 1; }
for command in curl tar sha256sum apk; do
    command -v "$command" >/dev/null 2>&1 || { echo "Missing required command: $command" >&2; exit 1; }
done
ARCH=$(apk --print-arch)
case "$ARCH" in
    x86_64) ;;
    *) echo "No Keycash release is available for Alpine $ARCH." >&2; exit 1 ;;
esac

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
NAME="keycash-solver-alpine-$ARCH.tar.gz"
echo "Downloading Keycash Solver for Alpine $ARCH..."
download "$BASE/$NAME" "$WORK/$NAME"
download "$BASE/$NAME.sha256" "$WORK/$NAME.sha256"
(cd "$WORK" && sha256sum -c "$NAME.sha256")
tar -xzf "$WORK/$NAME" -C "$WORK"
echo "Archive verified. Starting installer..."
sh "$WORK/keycash-solver-alpine-$ARCH/install.sh" "$@"
