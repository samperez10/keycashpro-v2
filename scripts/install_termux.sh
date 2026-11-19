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
echo "Verifying package..."
if ! (cd "$ROOT" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then
  echo "Package integrity check failed:" >&2
  (cd "$ROOT" && sha256sum -c SHA256SUMS 2>&1 | grep -v ': OK$') >&2 || true
  exit 1
fi
echo "Installing Keycash Solver..."
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
cp "$LAUNCHER" "$BIN/keycash-solver"
chmod 700 "$BIN/keycash-solver"
add_to_path() {
  bin_dir=$1
  case ":${PATH:-}:" in
    *":$bin_dir:"*) return 0 ;;
  esac
  found=0
  added=0
  for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ -e "$rc" ] || continue
    found=1
    if grep -Fq "# Keycash Solver PATH" "$rc" 2>/dev/null; then
      continue
    fi
    printf '\n# Keycash Solver PATH\nexport PATH="%s:$PATH"\n' "$bin_dir" >> "$rc"
    added=1
  done
  if [ "$found" -eq 0 ]; then
    printf '# Keycash Solver PATH\nexport PATH="%s:$PATH"\n' "$bin_dir" > "$HOME/.profile"
    added=1
  fi
  if [ "$added" -eq 1 ]; then
    echo "Added $bin_dir to PATH for new shells."
  fi
}
add_to_path "$BIN"
echo "Installed: $LAUNCHER"
echo "Run: keycash (or keycash-solver)"
echo "Private account data is preserved in: $DATA"
