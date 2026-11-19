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
echo "Verifying package..."
if ! (cd "$ROOT" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then
  echo "Package integrity check failed:" >&2
  (cd "$ROOT" && sha256sum -c SHA256SUMS 2>&1 | grep -v ': OK$') >&2 || true
  exit 1
fi
echo "Installing Keycash Solver..."
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
