#!/bin/sh
# Blur every mask in masks_raw into masks/ with one Gaussian blur (radius 1),
# skipping the old-frame masks that keep a hard edge:
#     1993*, 1997*, 2003*, future*, leb*
# Usage: sh blur_masks.sh [src_dir] [dst_dir] [radius]
#        defaults: masks_raw/ and masks/ next to this script, radius 1
# Needs python3 + pillow, or nix (runs via the flake devShell).

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC=${1:-"$SCRIPT_DIR/masks_raw"}
DST=${2:-"$SCRIPT_DIR/masks"}
RADIUS=${3:-1}

if [ ! -d "$SRC" ]; then
    echo "error: no such directory: $SRC" >&2
    exit 1
fi

if python3 -c "import PIL" >/dev/null 2>&1; then
    blur() { python3 "$SCRIPT_DIR/blur_mask.py" "$@"; }
elif command -v nix >/dev/null 2>&1; then
    blur() { nix develop "$SCRIPT_DIR" --command python3 "$SCRIPT_DIR/blur_mask.py" "$@"; }
else
    echo "error: need python3 with pillow installed, or nix for the flake devShell" >&2
    exit 1
fi

mkdir -p "$DST"
fail=0
for f in "$SRC"/*; do
    [ -f "$f" ] || continue
    name=${f##*/}
    case $name in
        1993*|1997*|2003*|future*|leb*)
            echo "skip: $name"
            continue
            ;;
    esac
    if blur "$f" -o "$DST/$name" --radius "$RADIUS"; then
        echo "ok: $name -> $DST/$name"
    else
        echo "failed: $name" >&2
        fail=1
    fi
done
exit $fail
