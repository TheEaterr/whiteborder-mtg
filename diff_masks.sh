#!/bin/sh
# diff_masks.sh - diff the file names of two mask directories.
# Usage: diff_masks.sh [DIR_A [DIR_B]]
# Defaults to ref_masks/ and ref_masks_clean/ next to this script.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
a=${1:-"$here/ref_masks"}
b=${2:-"$here/ref_masks_clean"}
ta=$(mktemp)
tb=$(mktemp)
trap 'rm -f "$ta" "$tb"' EXIT
ls "$a" | sort >"$ta"
ls "$b" | sort >"$tb"
echo "only in $a:"
comm -23 "$ta" "$tb" | sed 's/^/  /'
echo "only in $b:"
comm -13 "$ta" "$tb" | sed 's/^/  /'
echo "common: $(comm -12 "$ta" "$tb" | wc -l | tr -d ' ') files"
