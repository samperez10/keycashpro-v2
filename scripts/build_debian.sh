#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
. /etc/os-release
[ "${ID:-}" = "debian" ] || { echo "Debian is required." >&2; exit 1; }
[ "${VERSION_ID:-}" = "13" ] || { echo "Debian 13 (Trixie) is required." >&2; exit 1; }
[ "$(dpkg --print-architecture)" = "amd64" ] || { echo "Debian amd64 is required." >&2; exit 1; }
export PYO3_PYTHON=${KEYCASH_PYTHON:-python3}
export CARGO_TARGET_DIR="$ROOT/target/debian-13-x86_64"
cargo build --release --locked
BINARY="$CARGO_TARGET_DIR/release/keycash-solver"
"$BINARY" --root "$ROOT" --self-test
python3 scripts/package.py --binary "$BINARY" --platform debian-13 --architecture x86_64 --archive
