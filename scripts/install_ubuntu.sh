#!/bin/sh
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || { echo "This package requires Ubuntu." >&2; exit 1; }
case "${VERSION_ID:-}" in
  24.04|26.04) ;;
  *) echo "This package requires Ubuntu 24.04 or 26.04 LTS." >&2; exit 1 ;;
esac
[ "$(dpkg --print-architecture)" = "amd64" ] || { echo "This package supports Ubuntu amd64 only." >&2; exit 1; }
grep -Fq "\"platform\": \"ubuntu-$VERSION_ID\"" "$ROOT/build.json" || {
  echo "This archive does not match Ubuntu $VERSION_ID." >&2
  exit 1
}

PREFIX=${HOME}/.local
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) [ "$#" -ge 2 ] || { echo "--prefix needs a directory" >&2; exit 2; }; PREFIX=$2; shift 2 ;;
    -h|--help) echo "Usage: sh install.sh [--prefix DIR]"; exit ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

install_runtime() {
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-httpx python3-bs4 ca-certificates
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-httpx python3-bs4 ca-certificates
  else
    echo "Install runtime packages: python3 python3-httpx python3-bs4 ca-certificates" >&2
    exit 1
  fi
}
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import httpx,bs4" >/dev/null 2>&1; then
  install_runtime
fi
command -v sha256sum >/dev/null 2>&1 || { echo "Missing required command: sha256sum" >&2; exit 1; }
(cd "$ROOT" && sha256sum -c SHA256SUMS)
"$ROOT/keycash-solver" --root "$ROOT" --self-test >/dev/null

DEST="$PREFIX/lib/keycash-solver"
BIN="$PREFIX/bin"
DATA="$PREFIX/share/keycash-solver"
STAGE="$PREFIX/lib/.keycash-stage-$$"
BACKUP="$PREFIX/lib/.keycash-previous"
mkdir -p "$PREFIX/lib" "$BIN" "$DATA"
chmod 700 "$DATA"
mkdir "$STAGE"
cp "$ROOT/keycash-solver" "$ROOT/build.json" "$STAGE/"
chmod 700 "$STAGE/keycash-solver"
if [ -d "$BACKUP" ]; then find "$BACKUP" -type f -delete; find "$BACKUP" -depth -type d -empty -delete; fi
if [ -d "$DEST" ]; then mv "$DEST" "$BACKUP"; fi
if ! mv "$STAGE" "$DEST"; then [ -d "$BACKUP" ] && mv "$BACKUP" "$DEST"; exit 1; fi
if [ -d "$BACKUP" ]; then find "$BACKUP" -type f -delete; find "$BACKUP" -depth -type d -empty -delete; fi
LAUNCHER="$BIN/keycash"
printf "%s\n" "#!/bin/sh" "exec \"$DEST/keycash-solver\" --root \"$DEST\" --data-dir \"$DATA\" \"\$@\"" > "$LAUNCHER"
chmod 700 "$LAUNCHER"
echo "Installed: $LAUNCHER"
echo "Run: keycash"
echo "Private account data is preserved in: $DATA"
