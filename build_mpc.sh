#!/bin/sh
# build_mpc.sh - turn a card list into a folder ready for mpcfill-custom-cards
# (https://github.com/hamstu/mpcfill-custom-cards).
#
# List format, one card per line:  "<copies> <card name>"
#     4 Black Lotus
#     1 Bala Ged Recovery // Bala Ged Sanctuary
#
# Each card is looked up on Scryfall by name and run through negate_border.py
# (via `nix run`, or local python3 with pillow+numpy). Output structure:
#     <outdir>/
#       cards/
#         default-back.png                                (from back.png next to this script)
#         NN - Name [c-N].png                             single-faced card
#         NN - Name [c-N] [b-NN - Name back].png          double-faced front
#         NN - Name back.png                              double-faced back (used via [b-...])
#
# Usage: sh build_mpc.sh <listfile> [outdir]     listfile "-" reads stdin; outdir defaults to ./mpc-cards

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LIST=${1:-}
OUT=${2:-./mpc-cards}

if [ -z "$LIST" ]; then
    echo "usage: sh $0 <listfile|-> [outdir]" >&2
    exit 1
fi
if [ "$LIST" != "-" ] && [ ! -r "$LIST" ]; then
    echo "error: cannot read list file: $LIST" >&2
    exit 1
fi

command -v curl >/dev/null 2>&1 || { echo "error: curl not found" >&2; exit 1; }

if command -v python3 >/dev/null 2>&1; then
    py() { python3 "$@"; }
elif command -v nix >/dev/null 2>&1; then
    py() { nix develop "$SCRIPT_DIR" --command python3 "$@"; }
else
    echo "error: need python3 (or nix) to parse Scryfall responses" >&2
    exit 1
fi

if python3 -c "import PIL" >/dev/null 2>&1; then
    run() { python3 "$SCRIPT_DIR/negate_border.py" "$@"; }
elif command -v nix >/dev/null 2>&1; then
    run() { nix run "$SCRIPT_DIR#default" -- "$@"; }
else
    echo "error: need python3+pillow or nix to run negate_border.py" >&2
    exit 1
fi

# helper: read a Scryfall "named" JSON from stdin, print "set/number" then the card name
LOOKUP_PY=$(mktemp)
trap 'rm -f "$LOOKUP_PY"' EXIT
cat > "$LOOKUP_PY" <<'EOF'
import json, sys
d = json.load(sys.stdin)
if d.get("object") == "error":
    sys.exit(1)
print(f"{d['set']}/{d['collector_number']}")
print(d["name"])
EOF

lookup() {  # $1 = card name -> prints "<set>/<number>" and "<name>", or fails
    curl -sG -A "magic-frame/1.0" "https://api.scryfall.com/cards/named" --data-urlencode "fuzzy=$1" | py "$LOOKUP_PY"
}

CARDS="$OUT/cards"
mkdir -p "$CARDS"
if [ -f "$SCRIPT_DIR/back.png" ]; then
    cp "$SCRIPT_DIR/back.png" "$CARDS/default-back.png"
else
    echo "warning: no back.png next to $0; add a default-back image to $CARDS yourself" >&2
fi

if [ "$LIST" = "-" ]; then SRC=/dev/stdin; else SRC=$LIST; fi

i=0
fail=0
while IFS= read -r line || [ -n "$line" ]; do
    line=$(printf '%s' "$line" | tr -d '\r')
    case $line in ''|'#'*) continue ;; esac
    case $line in *" "*) ;; *) echo "skip (bad line): $line" >&2; continue ;; esac
    num=${line%% *}
    name=$(printf '%s' "${line#* }" | sed 's|^ *||; s| *$||')
    case $num in ''|*[!0-9]*) echo "skip (bad count '$num'): $name" >&2; continue ;; esac
    [ "$num" -ge 1 ] || { echo "skip (count < 1): $name" >&2; continue; }

    i=$((i + 1))
    nn=$(printf '%02d' "$i")
    info=$(lookup "$name") || { echo "skip (not found on Scryfall): $name" >&2; fail=1; sleep 0.2; continue; }
    setnum=$(printf '%s\n' "$info" | sed -n 1p)
    cname=$(printf '%s\n' "$info" | sed -n 2p)
    sname=$(printf '%s' "$cname" | tr '/\\()[]' '______' | sed 's|^ *||; s| *$||; s|  *| |g')

    echo "=== [$num] $cname  ($setnum)"
    front="$CARDS/$nn - $sname [c-$num].png"
    # --mpc: negate-border at the MPC Autofill format (63x88 mm @ 800 DPI = 1984x2772 px)
    run "https://api.scryfall.com/cards/$setnum" -o "$front" --mpc 2>&1 | tail -n 3 || fail=1

    # double-faced card: turn the _face1/_face2 outputs into a [b-...] front/back pair
    f1="${front%.png}_face1.png"
    f2="${front%.png}_face2.png"
    if [ -f "$f1" ] && [ -f "$f2" ]; then
        mv "$f1" "$CARDS/$nn - $sname [c-$num] [b-$nn - $sname back].png"
        mv "$f2" "$CARDS/$nn - $sname back.png"
    elif [ ! -f "$front" ]; then
        echo "failed: no output for $cname" >&2
        fail=1
    fi
    sleep 0.2
done < "$SRC"

echo "done: $i card(s) -> $CARDS"
exit $fail
