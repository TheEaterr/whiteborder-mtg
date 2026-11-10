#!/bin/sh
# Convert every file in masks_raw to grayscale in place:
#     magick <file> -colorspace Gray <file>
# Usage: sh gray_masks.sh [dir]   (default: masks_raw next to this script)

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DIR=${1:-"$SCRIPT_DIR/masks_raw"}

if ! command -v magick >/dev/null 2>&1; then
    echo "error: ImageMagick 7 'magick' not found in PATH" >&2
    exit 1
fi
if [ ! -d "$DIR" ]; then
    echo "error: no such directory: $DIR" >&2
    exit 1
fi

fail=0
for f in "$DIR"/*; do
    [ -f "$f" ] || continue
    if magick "$f" -colorspace Gray "$f"; then
        echo "ok: $f"
    else
        echo "failed: $f" >&2
        fail=1
    fi
done
exit $fail
