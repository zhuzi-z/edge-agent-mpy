#!/bin/bash
# Unit tests on the MicroPython unix port.
#
# Requirements: a MicroPython unix binary (v1.28+ standard variant).
# Override with MICROPY=/path/to/micropython.
#
# Usage:
#   ./tests/ut/test_unix.sh                    # run all tests/ut/test_*.py
#   ./tests/ut/test_unix.sh test_server.py     # run a single file
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MICROPY="${MICROPY:-$(command -v micropython 2>/dev/null || echo /usr/bin/micropython)}"

# Module resolution: app code, shared stubs, test helpers, mpy unittest.
# `.frozen` first so the real frozen `asyncio` (and other stdlib) takes
# precedence over any file-system shim in tests/stubs.
export MICROPYPATH=".frozen:$ROOT/src:$ROOT/tests/stubs:$ROOT/tests/ut:$ROOT/tests:$ROOT/tests/mpy"

if ! "$MICROPY" -c "import sys; assert sys.implementation.name == 'micropython'" 2>/dev/null; then
    echo "error: no working MicroPython unix binary found ('$MICROPY')." >&2
    echo "       Build ports/unix from github.com/micropython/micropython or set MICROPY=..." >&2
    exit 2
fi
echo "MicroPython: $("$MICROPY" --version 2>&1 | head -1)"

cd "$ROOT" || exit 2
pass=0; fail=0; failed=""
for f in "$ROOT"/tests/ut/test_*.py; do
    name="$(basename "$f")"
    if [ -n "${1:-}" ] && [ "$name" != "$1" ]; then continue; fi
    printf '== %-26s ' "$name"
    t0=$(date +%s%N)
    if out=$("$MICROPY" -X heapsize=16M "$f" 2>&1); then
        t1=$(date +%s%N); ms=$(( (t1 - t0) / 1000000 ))
        summary=$(printf '%s\n' "$out" | awk '/^Ran [0-9]+ tests/{printf "Ran %2d tests", $2; exit}')
        printf "PASS  %s  (%5dms)\n" "${summary}" "${ms}"
        pass=$((pass + 1))
    else
        echo "FAIL"
        printf '%s\n' "$out" | sed 's/^/   /'
        fail=$((fail + 1))
        failed="$failed $name"
    fi
done

echo
if [ "$fail" -eq 0 ]; then
    echo "unit tests: $pass files passed"
else
    echo "unit tests: $pass passed, $fail FAILED ($failed )"
fi
[ "$fail" -eq 0 ]
