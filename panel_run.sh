#!/bin/sh
# Test panel runner - writes the images into ./panel next to this script.
# Usage: sh panel_run.sh
#
# POSIX sh (not fish) for portability. white_border.py is run through
# `nix run <repo>#default` when nix is available, otherwise with a local
# python3 that has pillow + numpy installed.
#
# Each link only identifies the card: the tool fetches its OLDEST printing
# (Alpha lea is skipped in favor of Beta leb) and picks legendary masks from
# frame_effects, so the entry names below are the mask variants actually
# produced, e.g. the lea Black Lotus URL resolves to leb, and old-frame
# legendaries (leg/lea, no crown) fall back to the _default mask.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
S="$SCRIPT_DIR/panel"
mkdir -p "$S"

if command -v nix >/dev/null 2>&1; then
    run() { nix run "$SCRIPT_DIR#default" -- "$@"; }
else
    run() { python3 "$SCRIPT_DIR/white_border.py" "$@"; }
fi

# entries: name|scryfall-url|extra white-border args (single token)
# Adventures are treated as single-faced (one image), so they get a plain
# <name>.png with no _faceN suffix.
cards='
leb|https://scryfall.com/card/lea/232/black-lotus
1993|https://scryfall.com/card/leg/216/adun-oakenshield
1997|https://scryfall.com/card/ody/312/abandoned-outpost
2003|https://scryfall.com/card/unh/23/_____
2015|https://scryfall.com/card/tdc/281/atarka-world-render
2015_legendary|https://scryfall.com/card/ltr/236/anduril-flame-of-the-west
future|https://scryfall.com/card/fut/49/blind-phantasm
planeswalker|https://scryfall.com/card/2xm/56/jace-the-mind-sculptor
saga|https://scryfall.com/card/msc/41/age-of-ultron
saga_legendary|https://scryfall.com/card/ph18/4/the-legend-of-arena
modal_dfc|https://scryfall.com/card/znr/180/bala-ged-recovery-bala-ged-sanctuary
transform_legendary|https://scryfall.com/card/tla/203/aang-at-the-crossroads-aang-destined-savior
battle|https://scryfall.com/card/mom/230/invasion-of-alara-awaken-the-maelstrom
adventure_legendary|https://scryfall.com/card/woe/230/kellan-the-fae-blooded-birthright-boon
spree|https://scryfall.com/card/otj/75/three-steps-ahead
'

echo "$cards" | while IFS='|' read -r name url extra; do
    [ -n "$name" ] || continue
    echo "=== $name"
    run "$url" -o "$S/$name.png" --masks-dir "$SCRIPT_DIR/masks" $extra 2>&1 | tail -n 3
    sleep 0.2
done
