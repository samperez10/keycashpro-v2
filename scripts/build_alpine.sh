#!/bin/sh
set -eu
unset ANDROID_DATA ANDROID_ROOT
[ -r /etc/alpine-release ] || { echo "Run this inside Alpine Linux." >&2; exit 1; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
ARCH=$(apk --print-arch)
case "$ARCH" in x86_64|aarch64) ;; *) echo "Unsupported Alpine architecture: $ARCH" >&2; exit 1 ;; esac
PYTHON=${KEYCASH_PYTHON:-python3}
"$PYTHON" -c "import httpx, bs4"
export PYO3_PYTHON=$(command -v "$PYTHON")
export CARGO_TARGET_DIR="$ROOT/target/alpine-$ARCH"
export RUSTFLAGS="-C target-feature=-crt-static"
cargo build --release --locked
BINARY="$CARGO_TARGET_DIR/release/keycash-solver"
readelf -l "$BINARY" | grep "ld-musl-" >/dev/null || { echo "Expected a dynamically linked musl executable." >&2; exit 1; }
"$BINARY" --root "$ROOT" --self-test
"$PYTHON" scripts/package.py --binary "$BINARY" --platform alpine --architecture "$ARCH" --archive
