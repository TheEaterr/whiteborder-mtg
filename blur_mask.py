#!/usr/bin/env python3
"""blur_mask.py - slightly blur an image, e.g. to soften the edges of the masks.

The masks in ./masks are binary (0/255), so the painted border gets a hard,
aliased edge against the card art. Running a mask through this script feathers
that edge: the interior stays solid 255, only a narrow band around each edge
becomes a gradient. white_border.py uses the mask's grayscale values directly
as blend weights for the white paint, so blurred masks (in ./masks, or in a
separate directory via --masks-dir) produce a soft, anti-aliased border edge.

Usage:
    nix develop --command python3 blur_mask.py masks/2015_default.png
    nix develop --command python3 blur_mask.py masks -o masks-blurred --radius 3
    nix develop --command python3 blur_mask.py some_image.png -o soft.png --radius 1.5

Input may be a single image or a directory (all images in it are blurred into
the output directory). Default outputs: <name>_blurred.<ext> next to the
input file, or a <dir>-blurred/ directory next to the input directory.

Requires pillow (provided by the flake devShell).
"""

import argparse
from pathlib import Path

from PIL import Image, ImageFilter

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def blur_image(src, dst, radius):
    img = Image.open(src)
    if img.mode == "P":  # palette images don't filter well; keep transparency
        img = img.convert("RGBA")
    elif img.mode == "1":  # 1-bit bilevel: GaussianBlur needs a grayscale image
        img = img.convert("L")
    blurred = img.filter(ImageFilter.GaussianBlur(radius))
    blurred.save(dst)
    return img.size, img.mode


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="image file, or a directory of images to blur")
    p.add_argument("-o", "--output",
                   help="output file (input is a file) or directory (input is a dir); "
                        "default: <name>_blurred.<ext> / <dir>-blurred")
    p.add_argument("--radius", type=float, default=2.0,
                   help="Gaussian blur radius in px - keep small, this is a slight blur (default: 2.0)")
    args = p.parse_args()

    if args.radius <= 0:
        p.error("--radius must be > 0")

    src = Path(args.input)
    if src.is_dir():
        out_dir = Path(args.output) if args.output else src.parent / f"{src.name}-blurred"
        out_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in src.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES)
        if not files:
            raise SystemExit(f"error: no images ({', '.join(sorted(IMAGE_SUFFIXES))}) in {src}")
        for f in files:
            size, mode = blur_image(f, out_dir / f.name, args.radius)
            print(f"{f.name} -> {out_dir / f.name}  ({size[0]}x{size[1]}, {mode}, radius {args.radius})")
    elif src.is_file():
        out = Path(args.output) if args.output else src.with_name(f"{src.stem}_blurred{src.suffix}")
        size, mode = blur_image(src, out, args.radius)
        print(f"{src} -> {out}  ({size[0]}x{size[1]}, {mode}, radius {args.radius})")
    else:
        raise SystemExit(f"error: no such file or directory: {src}")


if __name__ == "__main__":
    main()
