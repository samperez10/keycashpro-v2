#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "26.04" ] || { echo "Ubuntu 26.04 LTS is required." >&2; exit 1; }
[ "$(dpkg --print-architecture)" = "amd64" ] || { echo "Ubuntu amd64 is required." >&2; exit 1; }
export PYO3_PYTHON=${KEYCASH_PYTHON:-python3}
export CARGO_TARGET_DIR="$ROOT/target/ubuntu-26.04-x86_64"
cargo build --release --locked
BINARY="$CARGO_TARGET_DIR/release/keycash-solver"
"$BINARY" --root "$ROOT" --self-test
python3 scripts/package.py --binary "$BINARY" --platform ubuntu-26.04 --architecture x86_64 --archive
