#!/bin/sh
set -eu
KEYCASH_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
for build in release debug; do
    if [ -x "$KEYCASH_ROOT/target/$build/keycash-solver" ]; then
        exec "$KEYCASH_ROOT/target/$build/keycash-solver" --root "$KEYCASH_ROOT" "$@"
    fi
done
printf '%s\n' 'Build first: sh scripts/build.sh' >&2
exit 1
