#!/bin/sh
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -r /etc/alpine-release ] || { echo "This package is for Alpine Linux." >&2; exit 1; }
PREFIX=${HOME}/.local
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) [ "$#" -ge 2 ] || { echo "--prefix needs a directory" >&2; exit 2; }; PREFIX=$2; shift 2 ;;
    --detect) python3 -c "import json,platform,sys,sysconfig; print(json.dumps({\"architecture\":platform.machine(),\"python_version\":f\"{sys.version_info.major}.{sys.version_info.minor}\",\"python_soabi\":sysconfig.get_config_var(\"SOABI\")},indent=2))"; exit ;;
    -h|--help) echo "Usage: sh install.sh [--prefix DIR] [--detect]"; exit ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
command -v python3 >/dev/null 2>&1 || { echo "Install python3 first." >&2; exit 1; }
if ! python3 -c "import httpx,bs4" >/dev/null 2>&1; then
  if [ "$(id -u)" -eq 0 ]; then apk add --no-cache python3 py3-httpx py3-beautifulsoup4 ca-certificates; else echo "Install runtime packages: apk add python3 py3-httpx py3-beautifulsoup4 ca-certificates" >&2; exit 1; fi
fi
(cd "$ROOT" && sha256sum -c SHA256SUMS)
"$ROOT/keycash-solver" --root "$ROOT" --self-test >/dev/null
DEST="$PREFIX/lib/keycash-solver"
BIN="$PREFIX/bin"
DATA="$PREFIX/share/keycash-solver"
mkdir -p "$PREFIX/lib" "$BIN" "$DATA"
chmod 700 "$DATA"
STAGE="$PREFIX/lib/.keycash-stage-$$"
BACKUP="$PREFIX/lib/.keycash-previous"
mkdir "$STAGE"
cp "$ROOT/keycash-solver" "$ROOT/build.json" "$STAGE/"
chmod 700 "$STAGE/keycash-solver"
if [ -d "$BACKUP" ]; then mv "$BACKUP" "$BACKUP.old-$$"; fi
if [ -d "$DEST" ]; then mv "$DEST" "$BACKUP"; fi
if ! mv "$STAGE" "$DEST"; then [ -d "$BACKUP" ] && mv "$BACKUP" "$DEST"; exit 1; fi
if [ -d "$BACKUP" ]; then find "$BACKUP" -type f -delete; find "$BACKUP" -depth -type d -empty -delete; fi
LAUNCHER="$BIN/keycash"
printf "%s\n" "#!/bin/sh" "exec \"$DEST/keycash-solver\" --root \"$DEST\" --data-dir \"$DATA\" \"\$@\"" > "$LAUNCHER"
chmod 700 "$LAUNCHER"
echo "Installed: $LAUNCHER"
echo "Private account data is preserved in: $DATA"
