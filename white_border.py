#!/usr/bin/env python3
"""white_border.py - recolor the border of a Magic: The Gathering card to white.

Given a Scryfall card link (https://scryfall.com/card/<set>/<collector-number>/<slug>),
downloads the card's png image and applies a border mask, painting the mask
region white. The link only identifies the card: its oldest printing is used,
except that Alpha (lea) is skipped in favor of Beta (leb).

Masks live in ./masks (override with --masks-dir) and are generated from
CardConjurer frame-pack assets by make_masks.py - CardConjurer isolates the
border per frame and the legendary crown separately, so masks adapt to every
frame (1993 old frame, 2003, 2015, future, planeswalker, saga, modal, battle)
and to legendary cards (crown excluded). Naming:

    masks/<key>_default.png     border to paint white, e.g. 2015_default.png
    masks/<key>_legendary.png   same with the legendary crown cut out - the crown
                                is left alone, we whiten what is around it
    masks/default.png           last-resort fallback

<key> comes from the card's layout (planeswalker/saga/modal/battle) or its
Scryfall frame year (1993, 1997, 2003, 2015, 2019->2015, future); the
_legendary variant is used when the printing's frame_effects contain
"legendary" (old frames render no crown, so the type line alone is not a
reliable signal). A mask may be
any size; it is resized (bilinear) to the card image and its grayscale values
are used directly as per-pixel blend weights for the white paint, multiplied
by the card's alpha mask so the rounded corners stay clean. Blur the masks
with blur_mask.py first and the border edge comes out soft instead of hard.
Every face of a double-faced card is processed (adventures count as
single-faced: their two card_faces share the card's one image).

Usage:
    white_border.py <scryfall-url> [-o OUTPUT] [--masks-dir DIR] [--face N]

Requires pillow + numpy (provided by the flake: nix run .#default -- <url>)
"""

import argparse
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

USER_AGENT = "magic-frame/1.0 (white-border card tool)"

DEFAULT_MASKS_DIR = "masks"  # relative to the current working directory

# Scryfall frame years without their own CardConjurer pack reuse another one
FRAME_ALIASES = {"2019": "2015"}


# ---------------------------------------------------------------- HTTP / API

def http_get(url, timeout=60):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, image/*, */*"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def scryfall_api_url(link):
    """Turn a scryfall.com card page URL into the equivalent api.scryfall.com URL."""
    u = urllib.parse.urlparse(link.strip())
    host = u.netloc.lower()
    path = u.path.strip("/")
    if "api.scryfall.com" in host:
        return f"https://api.scryfall.com/{path}"
    if "scryfall.com" in host:
        m = re.match(r"^card/([^/]+)/([^/]+)(?:/.*)?$", path)
        if m:
            set_code = urllib.parse.unquote(m.group(1))
            number = urllib.parse.unquote(m.group(2))
            q = urllib.parse.quote
            return f"https://api.scryfall.com/cards/{q(set_code)}/{q(number)}"
        if re.fullmatch(r"[0-9a-fA-F-]{36}", path):
            return f"https://api.scryfall.com/cards/{path.lower()}"
    raise SystemExit(f"error: not a Scryfall card link: {link}")


def get_card(link):
    data = json.loads(http_get(scryfall_api_url(link)))
    if data.get("object") == "error":
        raise SystemExit(f"error: Scryfall said: {data.get('details', data)}")
    card = oldest_printing(data)
    if (card.get("set"), str(card.get("collector_number"))) != \
            (data.get("set"), str(data.get("collector_number"))):
        print(f"oldest printing: {card.get('set_name')} #{card.get('collector_number')} "
              f"({card.get('set')}, {card.get('released_at')})")
    return card


def search_all(uri):
    """Follow a Scryfall search endpoint's pagination and return every result."""
    results = []
    while uri:
        data = json.loads(http_get(uri))
        if data.get("object") == "error":
            raise SystemExit(f"error: Scryfall said: {data.get('details', data)}")
        results.extend(data.get("data", []))
        uri = data.get("next_page") if data.get("has_more") else None
    return results


def oldest_printing(card):
    """Oldest printing of the card; Alpha (lea) is skipped in favor of Beta (leb)."""
    uri = card.get("prints_search_uri")
    if not uri and card.get("oracle_id"):
        uri = ("https://api.scryfall.com/cards/search?q=oracle_id:"
               + urllib.parse.quote(card["oracle_id"]) + "&unique=prints")
    if not uri:
        return card
    prints = [c for c in search_all(uri) if c.get("oracle_id") == card.get("oracle_id")]
    if not prints:
        return card
    prints.sort(key=lambda c: (c.get("released_at") or "", (c.get("set") or "").lower(),
                               str(c.get("collector_number") or "")))
    oldest = prints[0]
    if (oldest.get("set") or "").lower() == "lea":
        leb = next((c for c in prints if (c.get("set") or "").lower() == "leb"), None)
        if leb is not None:
            oldest = leb
    return oldest


def card_faces(card):
    if (card.get("layout") or "").lower() == "adventure":
        # adventures list two card_faces but share the card's single image
        return [card]
    return card.get("card_faces") or [card]


# ------------------------------------------------------------- image masks

def rounded_rect_mask(h, w):
    """Card-shaped mask for JPEGs (card fills the frame, corners are cut)."""
    r = max(2, round(0.045 * min(h, w)))
    yy, xx = np.mgrid[0:h, 0:w]
    inside = np.ones((h, w), bool)
    for cx, cy, zx, zy in (
        (r, r, xx < r, yy < r),
        (w - 1 - r, r, xx > w - 1 - r, yy < r),
        (r, h - 1 - r, xx < r, yy > h - 1 - r),
        (w - 1 - r, h - 1 - r, xx > w - 1 - r, yy > h - 1 - r),
    ):
        zone = zx & zy
        d2 = (xx - cx) ** 2 + (yy - cy) ** 2
        inside &= ~(zone & (d2 > r * r))
    return inside


def card_mask(img):
    """Boolean HxW mask of the card pixels (excludes transparent/background corners)."""
    if img.mode == "RGBA":
        return np.asarray(img.getchannel("A")) > 8
    a = np.asarray(img.convert("RGB"))
    return rounded_rect_mask(a.shape[0], a.shape[1])


# ------------------------------------------------------------ border masks

def mask_keys(card, face):
    """Mask lookup keys for a card face: legendary variant first, then defaults."""
    layout = (card.get("layout") or "").lower()
    frame = str(card.get("frame", ""))
    set = (card.get("set") or "").lower()
    tl = (face.get("type_line") or card.get("type_line") or "").lower()
    effects = {str(e).lower() for e in (card.get("frame_effects") or []) + (face.get("frame_effects") or [])}
    if set == "leb":
        base = "leb"
    elif "spree" in effects:
        base = "spree"
    elif layout == "planeswalker" or "planeswalker" in tl:
        base = "planeswalker"
    elif layout == "saga" or "saga" in tl:
        base = "saga"
    elif layout == "battle" or "battle" in tl:
        # check before modal_dfc/transform: MOM-style battles report
        # layout "transform" but have "Battle" in their type line
        base = "battle"
    elif layout == "modal_dfc":
        base = "modal"
    else:
        base = FRAME_ALIASES.get(frame, frame)
    keys = []
    if "legendary" in effects:
        keys.append(f"{base}_legendary")
    keys += [f"{base}_default", "default"]
    return keys


def load_border_mask(keys, w, h, masks_dir):
    """Load the first existing mask as a float HxW weight array in [0, 1], resized to the card."""
    tried = []
    for key in keys:
        path = masks_dir / f"{key}.png"
        tried.append(str(path))
        if path.is_file():
            m = Image.open(path)
            ch = m.getchannel("A") if m.mode == "RGBA" else m.convert("L")
            return np.asarray(ch.resize((w, h), Image.BILINEAR), dtype=np.float32) / 255.0, path.name
    raise SystemExit(f"error: no border mask found (tried: {', '.join(tried)})")


# ------------------------------------------------------------------ pipeline

def process_face(face, card, masks_dir, out_path):
    uris = face.get("image_uris") or card.get("image_uris") or {}
    png_url = uris.get("png")
    if not png_url:
        raise SystemExit(f"error: no png image for face {face.get('name')!r}")
    img = Image.open(io.BytesIO(http_get(png_url)))
    cmask = card_mask(img)
    if not cmask.any():
        raise SystemExit("error: could not find the card in the image")
    h, w = cmask.shape
    keys = mask_keys(card, face)
    src_mask, mask_name = load_border_mask(keys, w, h, masks_dir)
    weight = src_mask * cmask  # mask gray level x card alpha = paint weight
    out = img.convert("RGBA")
    arr = np.asarray(out).copy()
    a = weight[..., None]
    arr[..., :3] = (arr[..., :3].astype(np.float32) * (1 - a) + 255 * a).astype(np.uint8)
    Image.fromarray(arr, "RGBA").save(out_path)
    frac = 100.0 * weight.sum() / cmask.sum()
    return keys[0], mask_name, frac


def out_paths(card, n_faces, args):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", card.get("name", "card")).strip("_") or "card"
    if args.output:
        p = Path(args.output)
        if n_faces > 1:
            return [p.with_name(f"{p.stem}_face{i + 1}{p.suffix or '.png'}") for i in range(n_faces)]
        return [p]
    if n_faces == 1:
        return [Path(f"{name}_whiteborder.png")]
    return [Path(f"{name}_face{i + 1}_whiteborder.png") for i in range(n_faces)]


def main():
    p = argparse.ArgumentParser(
        prog="white-border",
        description="Recolor the border of an MTG card (from a Scryfall link) to white.",
    )
    p.add_argument("url", help="Scryfall card link, e.g. https://scryfall.com/card/mkm/123/some-card")
    p.add_argument("-o", "--output", help="output file (suffix _faceN is added for multi-face cards)")
    p.add_argument("--masks-dir", default=DEFAULT_MASKS_DIR,
                   help="directory with <key>_default.png / <key>_legendary.png masks (default: ./masks)")
    p.add_argument("--face", type=int, default=None, help="process only this face index (0-based)")
    args = p.parse_args()

    card = get_card(args.url)
    faces = card_faces(card)
    if args.face is not None:
        faces = faces[args.face : args.face + 1]
    paths = out_paths(card, len(faces), args)
    masks_dir = Path(args.masks_dir)

    print(f"card: {card.get('name')} ({card.get('set_name')}, #{card.get('collector_number')}, "
          f"frame {card.get('frame')}, layout {card.get('layout')})")
    for i, (face, path) in enumerate(zip(faces, paths)):
        key, mask_name, frac = process_face(face, card, masks_dir, path)
        label = f"face {i + 1} ({face.get('name', '')})" if len(faces) > 1 else ""
        print(f"  {label} key={key} mask={mask_name} "
              f"({frac:.1f}% of card recolored) -> {path}")


if __name__ == "__main__":
    main()
