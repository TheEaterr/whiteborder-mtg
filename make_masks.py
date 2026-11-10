#!/usr/bin/env python3
"""make_masks.py - generate border masks for white_border.py from CardConjurer assets.

CardConjurer (github.com/Investigamer/cardconjurer) isolates each frame's border
as its own asset under data/images/cardImages/<frame>/<frame>MaskBorder.png (the
masks the app itself loads), and the legendary crown as m15MaskLegendCrown.png.
For m15 and later frames its own "White Border" fill uses the border sliver
masks m15MaskBorderSliver.png / m15MaskBorderSliverCrown.png ("Alternative
Border" / "Alternative Border (Crown)") - the crown itself is cut out and left
alone; we whiten what is around the crown. This script downloads those assets
and writes:

    masks/<key>_default.png     border to paint white
    masks/<key>_legendary.png   same with the legendary crown cut out (if available)

Frames without a data/images/cardImages pack (1993, 2003, battle) fall back to
the legacy img/frames/ tree.

Usage:
    nix develop --command python3 make_masks.py [--out masks] [--only 2015,future]

Requires pillow + numpy + cairosvg (provided by the flake devShell).
"""

import argparse
import io
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

CC_BASE = "https://raw.githubusercontent.com/Investigamer/cardconjurer/master"
UA = {"User-Agent": "magic-frame/1.0", "Accept": "*/*"}

# mask key -> CardConjurer assets (relative to CC_BASE). Prefer the canonical
# data/images/cardImages/ masks the app loads; fall back to img/frames/ for
# frames without a cardImages pack (1993, 2003, battle).
PACKS = {
    "1993": {"border": "img/frames/old/fourth/borderBlack.png"},
    "1997": {"border": "data/images/cardImages/seventh/seventhMaskBorder.png"},
    "2003": {"border": "img/frames/8th/border.svg"},
    # m15 and later (2015/2019 frames): use CardConjurer's own "White Border"
    # masks directly - the border sliver, and its crown-cutout variant for
    # legendary cards (m15MaskBorderSliverCrown.png leaves the crown alone)
    "2015": {
        "border": "data/images/cardImages/m15/m15MaskBorderSliver.png",
        "legendary": "data/images/cardImages/m15/m15MaskBorderSliverCrown.png",
    },
    "future": {"border": "data/images/cardImages/future/futureMaskBorder.png"},
    # frames with an m15-style title bar share the m15 legendary crown mask
    # (crown silhouette x 41..1458, y 40..250 envelopes their title masks);
    # the crown itself is cut OUT of the legendary mask, left unpainted
    "planeswalker": {
        "border": "data/images/cardImages/planeswalker/planeswalkerMaskBorder.png",
        "crown": "data/images/cardImages/m15/m15MaskLegendCrown.png",
    },
    "saga": {
        "border": "data/images/cardImages/saga/sagaMaskBorder.png",
        "crown": "data/images/cardImages/m15/m15MaskLegendCrown.png",
    },
    "modal": {
        "border": "data/images/cardImages/modal/modalMaskBorder.png",
        "crown": "data/images/cardImages/m15/m15MaskLegendCrown.png",
    },
    "battle": {
        "border": "img/frames/m15/battle/maskBorder.png",
        "crown": "data/images/cardImages/m15/m15MaskLegendCrown.png",
    },
}

SVG_OUT = (1500, 2100)  # match the resolution of the data/images/cardImages masks


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def to_mask(img):
    """Boolean mask from an image: alpha channel if present (incl. palette
    transparency), else luminance. CardConjurer assets encode shapes in alpha."""
    if img.mode == "RGBA" or "transparency" in img.info:
        return np.asarray(img.convert("RGBA").getchannel("A")) > 127
    return np.asarray(img.convert("L")) > 127


def ensure_portrait(mask):
    """Rotate landscape masks (e.g. battle frames) to portrait card orientation."""
    h, w = mask.shape
    if w > h:
        mask = np.asarray(
            Image.fromarray((mask * 255).astype(np.uint8)).transpose(Image.ROTATE_90)
        ) > 127
    return mask


def rasterize_svg(data):
    import cairosvg

    png = cairosvg.svg2png(bytestring=data, output_width=SVG_OUT[0], output_height=SVG_OUT[1])
    return to_mask(Image.open(io.BytesIO(png)))


def resize_mask(mask, w, h):
    return np.asarray(
        Image.fromarray((mask * 255).astype(np.uint8)).resize((w, h), Image.NEAREST)
    ) > 127


def save_mask(mask, path):
    Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), "L").save(path)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="masks", help="output directory (default: ./masks)")
    p.add_argument("--only", default=None, help="comma-separated mask keys to build (default: all)")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    keys = [k.strip() for k in args.only.split(",")] if args.only else list(PACKS)

    for key in keys:
        spec = PACKS[key]
        border_url = f"{CC_BASE}/{spec['border']}"
        data = fetch(border_url)
        border = rasterize_svg(data) if spec["border"].endswith(".svg") else to_mask(Image.open(io.BytesIO(data)))
        border = ensure_portrait(border)
        bh, bw = border.shape
        save_mask(border, out / f"{key}_default.png")
        print(f"{key}_default.png  <- {spec['border']}  ({bw}x{bh}, {100*border.mean():.1f}% border)")
        if spec.get("legendary"):
            # dedicated legendary mask file (e.g. m15MaskBorderSliverCrown.png)
            ldata = fetch(f"{CC_BASE}/{spec['legendary']}")
            legend = rasterize_svg(ldata) if spec["legendary"].endswith(".svg") else to_mask(Image.open(io.BytesIO(ldata)))
            legend = ensure_portrait(resize_mask(legend, bw, bh))
            save_mask(legend, out / f"{key}_legendary.png")
            print(f"{key}_legendary.png  <- {spec['legendary']}  ({100*legend.mean():.1f}%)")
        elif spec.get("crown"):
            cdata = fetch(f"{CC_BASE}/{spec['crown']}")
            crown = to_mask(Image.open(io.BytesIO(cdata)))
            crown = resize_mask(crown, bw, bh)
            # crown left alone: cut it out of the legendary mask
            save_mask(border & ~crown, out / f"{key}_legendary.png")
            print(f"{key}_legendary.png  <- minus {spec['crown']}  ({100*(border & ~crown).mean():.1f}%)")


if __name__ == "__main__":
    main()
