#!/bin/sh
# run_test_cards.sh - run negate_border.py on every card link in test_cards.txt
# and store the negated images in ./test_cards_negated/ (git-ignored).
#
# Usage:
#   nix develop --command ./run_test_cards.sh            # recommended (pillow + numpy)
#   ./run_test_cards.sh [list-file]                      # any python3 with pillow+numpy
#
# Lines in the list that are not card links (e.g. scryfall.com/search?q=...)
# are skipped; failures don't stop the batch, they are counted at the end.

set -u
cd "$(dirname "$0")"

# re-exec inside the nix devShell when pillow/numpy are missing
if ! python3 -c 'import numpy, PIL' 2>/dev/null; then
    if command -v nix >/dev/null 2>&1; then
        exec nix develop --command "$0" "$@"
    fi
    echo "error: python3 needs numpy and pillow (run via: nix develop --command $0)" >&2
    exit 1
fi

LIST="${1:-test_cards.txt}"
OUT=test_cards_negated
mkdir -p "$OUT"

ok=0; failed=0; skipped=0
while IFS= read -r link || [ -n "$link" ]; do
    link=$(printf '%s' "$link" | tr -d '[:space:]')
    [ -z "$link" ] && continue
    case "$link" in '#'*) continue ;; esac
    case "$link" in
        *scryfall.com/card/*) ;;
        *) echo "skip (not a card link): $link"; skipped=$((skipped + 1)); continue ;;
    esac
    # output name from the URL slug (last path segment, without any query)
    slug=${link##*/}
    slug=${slug%%\?*}
    out="$OUT/$slug.png"
    echo "=== $link -> $out"
    if python3 negate_border.py "$link" -o "$out"; then
        ok=$((ok + 1))
    else
        echo "FAILED: $link" >&2
        failed=$((failed + 1))
    fi
done < "$LIST"

echo
echo "done: $ok negated, $failed failed, $skipped skipped (output in $OUT/)"
[ "$failed" -eq 0 ]
