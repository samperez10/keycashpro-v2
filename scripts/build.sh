#!/bin/sh
set -eu
KEYCASH_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$KEYCASH_ROOT"
KEYCASH_PYTHON="${KEYCASH_PYTHON:-$KEYCASH_ROOT/.venv/bin/python}"
"$KEYCASH_PYTHON" -c 'import httpx, bs4'
PYO3_PYTHON="$KEYCASH_PYTHON" cargo build --release --locked
./target/release/keycash-solver --root "$KEYCASH_ROOT" --self-test
