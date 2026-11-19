#!/data/data/com.termux/files/usr/bin/sh
set -eu
[ "${PREFIX:-}" = "/data/data/com.termux/files/usr" ] || { echo "Run this inside Termux." >&2; exit 1; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
[ "$(uname -m)" = "aarch64" ] || { echo "ARM64 Termux is required." >&2; exit 1; }
PYTHON=${KEYCASH_PYTHON:-python}
"$PYTHON" -m pip --version >/dev/null
DEPS="$ROOT/build/termux-python"
if [ -d "$DEPS" ]; then
    find "$DEPS" -type f -delete
    find "$DEPS" -depth -type d -empty -delete
fi
mkdir -p "$DEPS"
"$PYTHON" -m pip install --disable-pip-version-check --no-compile --upgrade --target "$DEPS" -r requirements.txt
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$DEPS"
export PYO3_PYTHON=$(command -v "$PYTHON")
export CARGO_TARGET_DIR="$ROOT/target/termux-aarch64"
cargo build --release --locked
BINARY="$CARGO_TARGET_DIR/release/keycash-solver"
"$BINARY" --root "$ROOT" --self-test
"$PYTHON" scripts/package.py --binary "$BINARY" --platform termux --architecture aarch64 --python-deps "$DEPS" --archive
