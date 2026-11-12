#!/data/data/com.termux/files/usr/bin/sh
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

[ "${PREFIX:-}" = "/data/data/com.termux/files/usr" ] || { echo "Run this installer inside Termux." >&2; exit 1; }
[ "$(uname -m)" = "aarch64" ] || { echo "This release supports ARM64 Termux only." >&2; exit 1; }
if ! command -v python >/dev/null 2>&1; then
    command -v pkg >/dev/null 2>&1 || { echo "Termux package manager not found." >&2; exit 1; }
    echo "Installing the Python runtime..."
    pkg install -y python
fi
command -v sha256sum >/dev/null 2>&1 || { echo "Install coreutils: pkg install coreutils" >&2; exit 1; }
(cd "$ROOT" && sha256sum -c SHA256SUMS)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/python" "$ROOT/keycash-solver" --root "$ROOT" --self-test >/dev/null

DEST="$PREFIX/lib/keycash-solver"
BIN="$PREFIX/bin"
DATA="$HOME/.local/share/keycash-solver"
STAGE="$PREFIX/lib/.keycash-stage-$$"
BACKUP="$PREFIX/lib/.keycash-previous"
mkdir -p "$PREFIX/lib" "$BIN" "$DATA"
chmod 700 "$DATA"
mkdir "$STAGE"
cp -R "$ROOT/." "$STAGE/"
chmod 700 "$STAGE/keycash-solver"
if [ -d "$BACKUP" ]; then find "$BACKUP" -type f -delete; find "$BACKUP" -depth -type d -empty -delete; fi
if [ -d "$DEST" ]; then mv "$DEST" "$BACKUP"; fi
if ! mv "$STAGE" "$DEST"; then [ -d "$BACKUP" ] && mv "$BACKUP" "$DEST"; exit 1; fi
if [ -d "$BACKUP" ]; then find "$BACKUP" -type f -delete; find "$BACKUP" -depth -type d -empty -delete; fi
LAUNCHER="$BIN/keycash"
printf "%s\n" "#!/data/data/com.termux/files/usr/bin/sh" "export PYTHONDONTWRITEBYTECODE=1" "export PYTHONPATH=\"$DEST/python\${PYTHONPATH:+:\$PYTHONPATH}\"" "exec \"$DEST/keycash-solver\" --root \"$DEST\" --data-dir \"$DATA\" \"\$@\"" > "$LAUNCHER"
chmod 700 "$LAUNCHER"
echo "Installed: $LAUNCHER"
echo "Run: keycash"
echo "Private account data is preserved in: $DATA"
