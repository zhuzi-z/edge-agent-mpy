#!/bin/sh
# Count lines of code across git-tracked files.
# The vendored firmware/micropython submodule is skipped automatically.
set -eu

cd "$(git rev-parse --show-toplevel)"

data=$(git ls-files | while IFS= read -r f; do
    [ -f "$f" ] || continue
    lines=$(wc -l < "$f")
    code=$(awk '!/^[[:space:]]*#/ && NF' "$f" | wc -l)
    case "$f" in
        src/*) dir=src ;;
        tests/*) dir=tests ;;
        skills/*) dir=skills ;;
        firmware/*) dir=firmware ;;
        *) dir=. ;;
    esac
    printf '%s %s %s %s\n' "$dir" "$lines" "$code" "$f"
done)

echo "$data" | awk '
    { files++; total += $2; code += $3 }
    END { printf "Total: %d files, %d lines (%d non-blank/non-comment)\n", files, total, code }'

echo
echo "By directory:"
echo "$data" | awk '{ t[$1] += $2; c[$1] += $3 } END { for (d in t) printf "%s %d %d\n", d, t[d], c[d] }' |
    sort -k2,2nr | awk '{ printf "  %-10s %7d total %7d code\n", $1, $2, $3 }'

echo
echo "By extension:"
echo "$data" | awk '
    { ext = $4
      if (sub(/.*\./, "", ext) == 0) ext = $4
      t[ext] += $2; n[ext]++ }
    END { for (e in t) printf "%s %d %d\n", e, t[e], n[e] }' |
    sort -k2,2nr | awk '{ printf "  %-10s %7d lines in %4d files\n", $1, $2, $3 }'

echo
echo "Top 10 largest files:"
echo "$data" | sort -k2,2nr | head -10 | awk '{ printf "  %7d  %s\n", $2, $4 }'
