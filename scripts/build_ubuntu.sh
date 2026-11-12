#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || { echo "Ubuntu is required." >&2; exit 1; }
case "${VERSION_ID:-}" in
  24.04|26.04) UBUNTU_VERSION=$VERSION_ID ;;
  *) echo "Ubuntu 24.04 or 26.04 LTS is required." >&2; exit 1 ;;
esac
[ "$(dpkg --print-architecture)" = "amd64" ] || { echo "Ubuntu amd64 is required." >&2; exit 1; }
export PYO3_PYTHON=${KEYCASH_PYTHON:-python3}
export CARGO_TARGET_DIR="$ROOT/target/ubuntu-$UBUNTU_VERSION-x86_64"
cargo build --release --locked
BINARY="$CARGO_TARGET_DIR/release/keycash-solver"
"$BINARY" --root "$ROOT" --self-test
python3 scripts/package.py --binary "$BINARY" --platform "ubuntu-$UBUNTU_VERSION" --architecture x86_64 --archive
