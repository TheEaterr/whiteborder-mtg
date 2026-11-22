#!/usr/bin/env python3
"""negate_border.py - negate the black border of a Magic: The Gathering card.

Given a Scryfall card link, the border is not taken from the asked card
directly: the script first finds a reference card with all the same
characteristics (frame era / layout / legendary crown / frame effects /
holostamp)
but of the Magic color white -
e.g. a white saga such as Elspeth Conquers Death for the black saga Age of
Ultron - where the black border is very distinct against the white frame,
detects the border on that reference, and stores it as a grayscale mask in the
reference cache (ref_masks/, created on demand; override with --ref-dir).
Later runs load the cached mask instead of downloading the reference again.
If no white reference exists for a frame, the border is detected on the asked
card itself and the resulting mask is cached under the same key as well.

The border color of the processed card is sampled from its bottom edge and
reported, but the border region itself comes from the reference mask.

Inside the border the card is inverted (negative filter, 255 - pixel). The
innermost band of the border mask (FLAT_EDGE_PX + 1 lines) is not fully
negated: on those pixels the NEW border color of the card (255 minus the
border color sampled from its bottom edge, so light / white-ish) is blended
in, each pixel using its own mask gray value as the blend weight:
    new = new_border * gray + old * (1 - gray)
The band is stamped with an increasing gray gradient, so the blend gets
stronger toward the negated border; the gray values are read from the mask
itself, so masks with varying grays in the band are handled the same way.
The border region is grown by one pixel into the card interior first, so the
band sits right at the border's inner edge; the outermost pixel line of the
card is negated like the rest of the border.
Enclosed pockets inside the border (the copyright / rarity / set info text
in the black bottom band, and the holostamp - the regular oval stamp or the
Universes Beyond triangle stamp, whatever its position in the bottom band)
are part of the mask and are negated along with the border. The alpha
channel is never touched, so rounded corners stay clean.

A reference mask is built from another card's scan, and Scryfall scans of the
same layout are not pixel-identical, so before applying a reference mask it
is slightly adjusted (shifted by a few pixels and/or grown/shrunk by a pixel
or two) to fit the actual card: the adjustment minimizes the number of
covered non-black pixels while keeping the black border fully covered, is
limited to small moves, and never shrinks the mask away.

Local image files / directories are processed the same way, except that the
border is detected on the image itself (no Scryfall reference is used).

Usage:
    negate_border.py https://scryfall.com/card/<set>/<number>/<slug> [-o OUT] [flags]
    negate_border.py IMAGE [-o OUT] [flags]
    negate_border.py DIR [-o OUTDIR] [flags]

Detection flags: --threshold / --max-hole.
Output flags: --ref-dir (mask cache), --scale F, --mpc (MPC Autofill format:
63x88 mm card at 800 DPI = 1984x2772 px).

Default outputs: <name>_negated.<ext> next to the input file, <dir>-negated/
for a directory, <card>_negated.png for a URL (or <card>_faceN_negated.png per
face for transform / modal_dfc cards - second faces are only processed for
those layouts). Each face of a two-faced card gets the reference mask of its
own characteristics (e.g. a planeswalker back side gets the planeswalker
mask); a back side never carries a holostamp or the DFC mark.

Requires pillow + numpy (provided by the flake devShell:
nix develop --command python3 negate_border.py IMAGE)
"""

import argparse
import http.client
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path

import numpy as np
from PIL import Image

USER_AGENT = "magic-frame/1.0 (negate-border card tool)"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# mask values of the inner edge of the border mask, as stamped by
# float_mask_from_region: the innermost FLAT_EDGE_PX+1 lines get an increasing
# gray gradient (i+1)/(FLAT_EDGE_PX+1) from inside out (the blend band);
# elsewhere in the region mask 1 is fully negated (255 - pixel), 0 is
# untouched. At apply time the gray value of each mask pixel is read from the
# mask itself and used as the blend weight toward the NEW border color of the
# card (255 minus the detected border color, so light / white-ish):
#     new = new_border * gray + old * (1 - gray)
FLAT_EDGE_PX = 2

# mask alignment: a reference mask is built from another card's scan and
# Scryfall scans of the same layout are not pixel-identical, so before
# applying it the mask is nudged to fit the actual card. The search is
# limited to small adjustments, the cost is the number of covered NON-black
# pixels (kept low) plus a weighted count of border pixels left uncovered
# (never leave part of the black border visible), and an adjustment that
# would shrink the mask below ALIGN_MIN_COVER of its size is rejected
ALIGN_MAX_SHIFT = 3       # max shift in each direction, px
ALIGN_MAX_GROW = 2        # max grow (+) / shrink (-), px
ALIGN_BORDER_WEIGHT = 10  # weight of uncovered border pixels in the cost
ALIGN_MIN_COVER = 0.6     # min fraction of the original mask size to keep
ALIGN_THRESHOLD = 8       # max per-channel distance from the border color for
                          # a pixel to count as border during alignment

# mask cache next to the script; when the script itself lives in a read-only
# location (e.g. the nix store via `nix run`), fall back to ./ref_masks
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REF_DIR = ((_SCRIPT_DIR / "ref_masks") if os.access(_SCRIPT_DIR, os.W_OK)
                   else Path.cwd() / "ref_masks")

# Release date of Dominaria, the first set printed with the legendary crown
# on the type line - Scryfall's frame effect "legendary" (searchable as
# frame:legendary). Frame history: 1993 (Alpha) / 1997 (classic) / 2003
# (Eighth Edition, thicker border) / 2015 (M15, thinner border + holo stamp
# area) / future. The crown only exists on 2015/future-frame legendary cards
# from this date on, and never on planeswalkers (Scryfall does not tag them
# with frame:legendary)
CROWN_DATE = "2018-04-27"

# First release date of a Marvel's Spider-Man-wave modal_dfc printing (the
# pmei promo; the spm set itself is 2025-09-26, om1 2025-09-23): from this
# date on, modal_dfc fronts carry the same DFC-style mark as transform cards.
# Scryfall does not report that mark as a frame effect, so the dfc bucket is
# assumed from the release date on
SPIDERMAN_DATE = "2025-09-19"

# ---------------------------------------------------------------- frame model
# Scryfall frame effects mapped to the "bucket" they get in the reference key.
# All DFC marks are the same icon in the same spot, so they share one bucket -
# except fandfc, which is its own frame artwork
DFC_EFFECTS = {"sunmoondfc", "compasslanddfc", "originpwdfc", "mooneldrazidfc",
               "waxingandwaningmoondfc", "convertdfc", "upsidedowndfc"}
FRAME_EFFECT_BUCKETS = {"legendary": "legendary", "spree": "spree", "fandfc": "fandfc"}
FRAME_EFFECT_BUCKETS.update({e: "dfc" for e in DFC_EFFECTS})
# frame effects that do not change the border ring: fancy printings are ignored
# as references anyway (blandness), so these are explicitly ignored
IGNORED_FRAME_EFFECTS = {"miracle", "enchantment", "draft", "devoid", "tombstone",
                         "colorshifted", "inverted", "showcase", "extendedart",
                         "companion", "etched", "snow", "lesson", "shatteredglass",
                         "dark"}
# Scryfall search term matching a whole bucket (dfc = any DFC mark)
BUCKET_QUERY = {"dfc": "(" + " or ".join(f"frame:{e}" for e in sorted(DFC_EFFECTS)) + ")"}

_WARNED_EFFECTS = set()


def effect_bucket(effect, warn=True):
    """Bucket of a frame effect: FRAME_EFFECT_BUCKETS if known, None if the
    effect is explicitly ignored, otherwise the effect itself - unknown
    effects are logged once and taken into account, not silently dropped."""
    e = str(effect).lower()
    if e in FRAME_EFFECT_BUCKETS:
        return FRAME_EFFECT_BUCKETS[e]
    if e in IGNORED_FRAME_EFFECTS:
        return None
    if warn and e not in _WARNED_EFFECTS:
        _WARNED_EFFECTS.add(e)
        print(f"  warning: unknown frame effect '{effect}' - taking it into account")
    return e


def frame_era(frame):
    """Scryfall frame era - only 1993 / 1997 / 2003 / 2015 / future exist."""
    return str(frame or "2015")


def has_crown(card, face=None):
    """Whether the card face carries the legendary crown: Scryfall's frame
    effect "legendary", or - for printings without the frame_effects backfill -
    a legendary non-planeswalker on a 2015/future frame released from Dominaria
    (CROWN_DATE) on. Planeswalkers never carry the crown. The crown is the only
    legendary trait that changes the frame; without it, legendary and
    non-legendary cards share the same border ring."""
    face = face or {}
    tl = (face.get("type_line") or card.get("type_line") or "").lower()
    face_effects = face.get("frame_effects") if face is not card else None
    if face_effects is not None:
        effects = {str(e).lower() for e in face_effects}
    else:
        effects = {str(e).lower() for e in (card.get("frame_effects") or [])}
    if "legendary" in effects:
        return True
    return bool("legendary" in tl and "planeswalker" not in tl
                and frame_era(card.get("frame", "")) in ("2015", "future")
                and (card.get("released_at") or "") >= CROWN_DATE)


def stamp_of(card, face=None):
    """Raw Scryfall security_stamp of a card face (None = no stamp): "oval"
    for the regular holo stamp (M15 onward), "triangle" for the Universes
    Beyond stamp (different shape and position), other values for other
    special stamps."""
    ss = (face or {}).get("security_stamp") or card.get("security_stamp")
    return str(ss).lower() if ss else None


# stamps sharing the regular oval stamp's artwork and position are aliased
# to it
STAMP_ALIASES = {"acorn": "oval", "heart": "oval"}


def stamp_key(stamp):
    """Stamp component of a reference key: None (no stamp), "stamp" for the
    regular oval stamp (and its aliases, e.g. "acorn" or "heart"),
    "stamp_<value>" for
    any other stamp (e.g. "stamp_triangle" for the Universes Beyond stamp).
    Stamped and non-stamped cards - and UB vs regular stamps - get different
    masks."""
    if not stamp:
        return None
    s = str(stamp).lower()
    s = STAMP_ALIASES.get(s, s)
    return "stamp" if s == "oval" else f"stamp_{s}"


# layouts with their own frame (the base of the reference key); class and case
# share the saga frame. Rooms and battles are not detected from the layout
# but from the type line: rooms have a split layout, battles a transform one
# (layout:battle is not a Scryfall search qualifier)
LAYOUT_BASES = {"saga": "saga", "class": "saga", "case": "saga",
                "modal_dfc": "modal", "leveler": "leveler"}
# split cards are refined by subtype, detected from the keywords
SPLIT_KEYWORDS = (("fuse", "fuse"), ("aftermath", "aftermath"))
# layouts with the same border as a normal card of the same frame era;
# transform/meld marks come from the frame effects (dfc bucket)
LAYOUT_SAME_AS_NORMAL = {"flip", "adventure", "prepare", "mutate", "prototype",
                         "host", "meld"}
# layouts whose card physically has a second face with its own image - every
# face is processed; every other layout shares the single front image
MULTI_FACE_LAYOUTS = {"transform", "modal_dfc"}
# layouts without a usable card border - not supported
LAYOUT_UNSUPPORTED = {"scheme", "vanguard", "augment", "art_series", "planar",
                      "token", "emblem", "front_card", "reversible_card",
                      "double_faced_token"}

# frame effects / traits that make a printing less plain than another
SPECIAL_PRINTING_EFFECTS = {"showcase", "extendedart", "borderless", "etched",
                            "inverted", "fullart", "textless"}

# MakePlayingCards (MPC Autofill target): card face is 63x88 mm and the digital
# press prints at max 800 DPI, so the ideal upload is 63/25.4*800 x 88/25.4*800
MPC_CARD_MM = (63.0, 88.0)
MPC_MAX_DPI = 800
MPC_SIZE = (round(MPC_CARD_MM[0] / 25.4 * MPC_MAX_DPI),
            round(MPC_CARD_MM[1] / 25.4 * MPC_MAX_DPI))  # (1984, 2772)


# ---------------------------------------------------------------- HTTP / API

def http_get(url, timeout=60, retries=3):
    """GET a URL with retries (Scryfall/scans CDN occasionally cut connections)."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT,
                              "Accept": "application/json, image/*, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Scryfall signals "no results" for searches with a 404 whose
                # body is a JSON error object - return it, don't retry
                return e.read()
            last = e
            if attempt + 1 < retries:
                time.sleep(1.0 * (attempt + 1))
        except (urllib.error.URLError, http.client.IncompleteRead, OSError) as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(1.0 * (attempt + 1))
    raise last


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


# injected into every /cards/search query: paper printings only ((game:paper) -
# digital cards have different images), black-bordered default printings, no
# promos, and neither Alpha nor Beta (lea/leb - their scans are off)
COMMON_QUERY_TOKENS = ("(game:paper)", "border:black", "-is:promo", "is:default",
                       "-set:lea", "-set:leb")


def add_common_filters(uri):
    """Add the COMMON_QUERY_TOKENS to the q parameter of a Scryfall
    /cards/search URI (other URIs are returned unchanged); a token already
    present in either polarity is not added again."""
    u = urllib.parse.urlsplit(uri)
    if not u.path.endswith("/cards/search"):
        return uri
    params = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    for i, (k, v) in enumerate(params):
        if k != "q":
            continue
        tokens = v.split()
        missing = [t for t in COMMON_QUERY_TOKENS
                   if t.lstrip("-") not in tokens
                   and "-" + t.lstrip("-") not in tokens]
        if missing:
            params[i] = (k, " ".join(missing + tokens))
        break
    return urllib.parse.urlunsplit(
        (u.scheme, u.netloc, u.path, urllib.parse.urlencode(params), u.fragment))


def search_all(uri, paginate=True):
    """Follow a Scryfall search endpoint's pagination and return every result.

    Every search is restricted to paper printings ((game:paper) is injected
    into the query) - digital-only cards (Arena/MTGO) have different images.
    With paginate=False only the first page of results is fetched."""
    uri = add_common_filters(uri)
    results = []
    while uri:
        data = json.loads(http_get(uri))
        if data.get("object") == "error":
            raise SystemExit(f"error: Scryfall said: {data.get('details', data)}")
        results.extend(data.get("data", []))
        uri = data.get("next_page") if (paginate and data.get("has_more")) else None
    return results


def search_cards(query, paginate=True):
    """Scryfall search; returns [] when nothing matches."""
    url = "https://api.scryfall.com/cards/search?" + urllib.parse.urlencode(
        {"q": query, "unique": "prints"})
    try:
        return search_all(url, paginate=paginate)
    except SystemExit:
        return []


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


def blandness(c):
    """0 for a plain printing; higher = more special (promo, full-art, borderless, ...)."""
    score = 0
    if c.get("promo"):
        score += 1
    if c.get("full_art"):
        score += 1
    if c.get("textless"):
        score += 1
    if (c.get("border_color") or "").lower() == "borderless":
        score += 1
    if {str(e).lower() for e in c.get("frame_effects") or []} & SPECIAL_PRINTING_EFFECTS:
        score += 1
    return score


def oldest_printing(card):
    """Oldest printing of the card; Alpha (lea) is never chosen - when the
    oldest printing would be Alpha, the next-oldest set is taken instead
    (Beta, leb, for Alpha cards) - and a foil-only printing is never chosen
    when a non-foil one exists.

    When that oldest set has several printings of the card, the blandest one
    (plain border, no promo/full-art/showcase/etc.) wins over the collector
    number order."""
    uri = card.get("prints_search_uri")
    if not uri and card.get("oracle_id"):
        uri = ("https://api.scryfall.com/cards/search?q=oracle_id:"
               + urllib.parse.quote(card["oracle_id"]) + "&unique=prints")
    if not uri:
        return card
    prints = [c for c in search_all(uri) if c.get("oracle_id") == card.get("oracle_id")]
    if not prints:
        return card
    nonfoil = [c for c in prints if not (c.get("foil") and not c.get("nonfoil"))]
    if nonfoil:
        prints = nonfoil
    non_lea = [c for c in prints if (c.get("set") or "").lower() != "lea"]
    if non_lea:
        prints = non_lea
    prints.sort(key=lambda c: (c.get("released_at") or "", (c.get("set") or "").lower(),
                               blandness(c), str(c.get("collector_number") or "")))
    return prints[0]


def card_faces(card):
    """Faces of the card to process: every face for MULTI_FACE_LAYOUTS
    (transform, modal_dfc - the physical card has a second side with its own
    image); all other layouts share the single front image, so only the front
    face is processed."""
    faces = card.get("card_faces") or []
    if (card.get("layout") or "").lower() in MULTI_FACE_LAYOUTS:
        return faces or [card]
    return [faces[0]] if faces else [card]


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


# ------------------------------------------------------------- mask helpers

def shift(mask, dy, dx):
    """mask shifted by (dy, dx), zero-padded at the borders."""
    out = np.zeros_like(mask)
    h, w = mask.shape
    out[max(0, dy):h - max(0, -dy), max(0, dx):w - max(0, -dx)] = \
        mask[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
    return out


def shift_clamp(mask, dy, dx):
    """mask shifted by (dy, dx), edge-replicated at the image borders - the
    card is cut off at the image edge, so its mask continues beyond it."""
    h, w = mask.shape
    ys = np.clip(np.arange(h) - dy, 0, h - 1)
    xs = np.clip(np.arange(w) - dx, 0, w - 1)
    return mask[np.ix_(ys, xs)]


def dilate8(mask):
    out = mask.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                out |= shift(mask, dy, dx)
    return out


def erode4(mask):
    out = mask.copy()
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        out &= shift(mask, dy, dx)
    return out


def erode4_clamp(mask, card):
    """erode4 over the card domain: neighbors outside the card (image border
    or rounded corners) count as inside the mask - the card is cut off at
    the image edge, so the mask's outer edge is not eroded, only its inner
    boundary moves."""
    out = mask.copy()
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        out &= shift_clamp(mask, dy, dx) | ~shift(card, dy, dx)
    return out


def dilate_n(mask, n):
    out = mask
    for _ in range(n):
        out = dilate8(out)
    return out


def erode_n_clamp(mask, n, card):
    out = mask
    for _ in range(n):
        out = erode4_clamp(out, card)
    return out


def flood_from_edges(conn, seed):
    """All pixels of conn reachable from seed by 8-connected growth."""
    region = seed.copy()
    while True:
        new = dilate8(region) & conn
        if new.sum() == region.sum():
            return new
        region = new


def fill_enclosed_pockets(region, card, max_hole):
    """Fill pockets of card enclosed by region, up to max_hole pixels each.

    Returns the pockets to add to the region. The card interior is also
    enclosed by the border but is one huge component, so the size cap keeps
    it out; the copyright / rarity / set info text inside the bottom band and
    the holostamp (the regular oval stamp, or the Universes Beyond triangle
    stamp in its own spot of the bottom band) are small pockets and get
    filled - whatever its position, the stamp is negated with the border.
    """
    if not region.any():
        return np.zeros_like(region)
    # everything not reachable from outside the card without crossing the border
    outside = flood_from_edges(~region, ~card)
    enclosed = card & ~region & ~outside
    filled = np.zeros_like(region)
    remaining = enclosed
    while remaining.any():
        ys, xs = np.where(remaining)
        seed = np.zeros_like(remaining)
        seed[ys[0], xs[0]] = True
        # grow the whole component first: a partial grow would fragment the
        # card interior into pieces that could pass the size cap
        pocket = flood_from_edges(remaining, seed)
        if pocket.sum() <= max_hole:
            filled |= pocket
        remaining &= ~pocket
    return filled


# ------------------------------------------------------- border detection

def detect_border_color(sub, card):
    """Median RGB of the card's bottom edge rows (the last rows of the card)."""
    h = card.shape[0]
    px = sub[h - 3:, :, :3][card[h - 3:, :]]
    if px.size == 0:
        return None
    return np.median(px, axis=0)


def detect_region(img, threshold=32, max_hole=20000):
    """Detect the border region of a card image.

    Returns (region mask or None, border color as RGB array or None, border
    color hex)."""
    full = np.asarray(img.convert("RGBA"))
    card_full = card_mask(img)
    ys, xs = np.where(card_full)
    if ys.size == 0:
        raise SystemExit("error: could not find the card in the image")
    y0, y1 = ys.min(), ys.max() + 1
    x0, x1 = xs.min(), xs.max() + 1

    sub = full[y0:y1, x0:x1]          # view; the card touches all four crop edges
    card = card_full[y0:y1, x0:x1]

    border = detect_border_color(sub, card)
    hexc = "none" if border is None else \
        "#{:02X}{:02X}{:02X}".format(*(int(round(c)) for c in border))
    if border is None:
        return None, None, hexc

    # border pixels = within threshold of the detected border color (per channel)
    dist = np.abs(sub[..., :3].astype(np.int16) - border.astype(np.int16)).max(axis=2)
    black = (dist <= threshold) & card

    # border = those pixels connected to the card's outer edge, flooded over the
    # raw mask (no dilation) so the region follows the real border shape and
    # cannot leak through anti-aliased gaps into the card interior
    edge = card & ~erode4(card)        # outermost 1px layer of the card
    region = flood_from_edges(black, black & edge)
    # enclosed pockets (copyright / rarity / set info text and holostamp in
    # the bottom band)
    region |= fill_enclosed_pockets(region, card, max_hole)

    region_full = np.zeros(card_full.shape, bool)
    region_full[y0:y1, x0:x1] = region
    return region_full, border, hexc


def inner_lines(region, card, n):
    """The n innermost 1px lines of region, from inside out (region is not
    modified)."""
    lines = []
    r = region.copy()
    outside = card & ~region
    for _ in range(n):
        line = r & dilate8(outside)
        if not line.any():
            break
        lines.append(line)
        r &= ~line
        outside |= line
    return lines


def float_mask_from_region(region, card):
    """Grayscale float mask from a card-clipped border region: 1.0 inside
    (fully negated), 0 outside; the innermost FLAT_EDGE_PX+1 lines get the
    gray gradient (i+1)/(FLAT_EDGE_PX+1) from inside out (the blend band).
    The gray values are read from the mask at apply time and used as blend
    weights, so masks with varying grays in the band work the same way."""
    m = np.zeros(region.shape, np.float32)
    m[region] = 1.0
    for i, line in enumerate(inner_lines(region, card, FLAT_EDGE_PX + 1)):
        m[line] = (i + 1) / (FLAT_EDGE_PX + 1)
    return m


def mask_from_region(region, card):
    """Grayscale float mask from a binary border region: the region is grown by
    one pixel into the card interior first, so the blend band at the inner
    edge is not part of the border itself."""
    return float_mask_from_region(dilate8(region) & card, card)


def resize_mask(mask, h, w):
    """Resize a mask to (h, w) with NEAREST, so the mask gray levels survive."""
    if mask.shape != (h, w):
        mask = np.asarray(
            Image.fromarray((mask * 255).round().astype(np.uint8), "L")
            .resize((w, h), Image.NEAREST), dtype=np.float32) / 255.0
    return mask


# ------------------------------------------------------- mask alignment

def align_mask(mask, img, border, threshold=ALIGN_THRESHOLD):
    """Nudge a reference mask (shift / grow / shrink) to fit the card image.

    Scryfall scans of the same layout differ by a few pixels, so the mask
    built from the reference card can sit slightly off on the asked card.
    The covered region is searched over shifts of +/- ALIGN_MAX_SHIFT px and
    grows/shrinks of +/- ALIGN_MAX_GROW px; the cost of a candidate is the
    number of covered pixels that are NOT border-colored (non-black covered,
    kept low) plus ALIGN_BORDER_WEIGHT times the number of border pixels it
    leaves uncovered (the black border must stay fully covered). The identity
    transform is always a candidate and wins ties, so a mask that already fits
    is returned unchanged; an adjustment shrinking the mask below
    ALIGN_MIN_COVER of its original size is rejected.

    Returns (mask, info or None) - info describes the applied adjustment."""
    if border is None:
        return mask, None
    arr = np.asarray(img.convert("RGB"))
    card = card_mask(img)
    dist = np.abs(arr.astype(np.int16) - np.asarray(border, dtype=np.int16)).max(axis=2)
    nonblack = (dist > threshold) & card
    region = mask > 0
    if not region.any():
        return mask, None
    # the card's own border, detected in a band around the reference mask -
    # the band keeps the flood from leaking into a dark card frame
    band = dilate_n(region, ALIGN_MAX_SHIFT + ALIGN_MAX_GROW + 3) & card
    black = (dist <= threshold) & card
    edge = card & ~erode4(card)
    local = black & band
    local_border = flood_from_edges(local, local & edge)
    if not local_border.any():
        return mask, None
    total_border = int(local_border.sum())
    base = int(region.sum())
    shifts = sorted(((dy, dx)
                     for dy in range(-ALIGN_MAX_SHIFT, ALIGN_MAX_SHIFT + 1)
                     for dx in range(-ALIGN_MAX_SHIFT, ALIGN_MAX_SHIFT + 1)),
                    key=lambda s: abs(s[0]) + abs(s[1]))
    grow_order = [0]
    for k in range(1, ALIGN_MAX_GROW + 1):
        grow_order += [k, -k]
    grows = {}
    excls = {}    # innermost FLAT_EDGE_PX+1 lines of each grown region - they
    # are not fully negated (the blend band), so they must not count as
    # covered non-black
    for g in grow_order:
        if g == 0:
            r = region
        elif g > 0:
            r = dilate_n(region, g)
        else:
            # shrink: pull the inner boundary in, keep the outer edge (the
            # card is cut off at the image edge)
            r = erode_n_clamp(region, -g, card)
        grows[g] = r & card
        ls = inner_lines(grows[g], card, FLAT_EDGE_PX + 1)
        excls[g] = np.logical_or.reduce(ls) if ls else np.zeros_like(grows[g])
    best = None  # (cost, dy, dx, g, fp, fn)
    for dy, dx in shifts:
        for g, r in grows.items():
            rc = shift_clamp(r, dy, dx) & card
            excl = shift_clamp(excls[g], dy, dx) & card
            fp = int((rc & ~excl & nonblack).sum())             # covered non-black px
            fn = total_border - int((rc & local_border).sum())  # border px uncovered
            cost = fp + ALIGN_BORDER_WEIGHT * fn
            if best is None or cost < best[0]:
                best = (cost, dy, dx, g, fp, fn)
    _cost, dy, dx, g, fp, fn = best
    if (dy, dx, g) == (0, 0, 0):
        return mask, None
    r = shift_clamp(grows[g], dy, dx) & card
    if r.sum() < ALIGN_MIN_COVER * base:
        return mask, None
    m = float_mask_from_region(r, card)
    info = {"shift": (dy, dx), "grow": g, "fp": fp, "fn": fn}
    return m, info


# ------------------------------------------------------- reference cards

# Everything needed to find / identify the white reference of one card face
RefSpec = namedtuple("RefSpec", ["key", "base", "layout", "era", "effects",
                                 "buckets", "typecat", "crowned", "stamp",
                                 "is_back", "pw_back"])


def ref_key(card, face):
    """Cache key + search ingredients for the white reference of a card face.

    The key is a composition of every characteristic that can change the
    border ring, not an exclusive choice - it always starts with the frame
    era (the "date"):
        <era>_<base>[_<variant>][+<extras>]
    base: leb, spree, planeswalker, room (Enchantment — Room, split layout),
    battle (Battle — ..., detected from the type line since layout:battle is
    not a Scryfall qualifier), split_<fuse|aftermath|regular>, saga (also
    class/case), modal (modal_dfc), leveler, meld (the back side of a
    meld card - the front carries the meld mark and is a normal card), or
    "default" for normal and the same-as-normal layouts - e.g. leveler exists
    both as 2003_leveler and 2015_leveler;
    variant: legendary_<creature|other>_crowned when the face carries the
    legendary crown - the only legendary trait that changes the frame (without
    the crown, legendary and non-legendary cards share the same border ring,
    and planeswalkers never carry a crown); for the default and modal bases
    also creature/other - but only in the 2015 frame, where creature and
    non-creature frames differ (power/toughness box; modal cards can be
    creatures on either face, and vehicles count as creatures); the other
    frames share one border ring;
    extras: one per relevant frame-effect bucket (dfc, fandfc, unknown) plus
    the holostamp (stamp, or stamp_<value>, e.g. stamp_triangle for the
    Universes Beyond stamp), plus pw_back for the back side of a transform
    card that is a planeswalker on both sides - a unique frame no other card
    has.
    A transform front always carries a DFC mark, so when Scryfall does not
    report the frame effect the dfc bucket is assumed (battles are exempt -
    their transform layout carries no DFC mark). From Marvel's Spider-Man
    (SPIDERMAN_DATE) on, modal_dfc fronts carry the same DFC mark, which
    Scryfall does not report either - the dfc bucket is assumed from the
    release date.
    Each face of a two-faced card gets the characteristics of its own face:
    the face's frame_effects are used when Scryfall provides them (otherwise
    the card-level ones), and the type line is face-first - so e.g. the
    planeswalker back side of a modal_dfc card gets the planeswalker base. A
    back side (second face of a transform/modal_dfc card, or the back of a
    meld card) never carries a holostamp and never the DFC mark.
    Examples: 2015_default_creature+stamp, 2015_default_other+stamp,
    2015_default_creature+dfc+stamp,
    2015_default_legendary_creature_crowned+stamp, 2003_leveler, 2015_saga.
    """
    layout = (card.get("layout") or "").lower()
    era = frame_era(card.get("frame", ""))
    card_set = (card.get("set") or "").lower()
    tl = (face.get("type_line") or card.get("type_line") or "").lower()
    # per-face frame effects: when the face carries its own (non-None)
    # frame_effects they are used alone, otherwise the card-level effects
    # apply - so a card-level mark (e.g. "legendary") does not leak onto a
    # back face that does not have it itself
    face_effects = face.get("frame_effects") if face is not card else None
    if face_effects is not None:
        effects = {str(e).lower() for e in face_effects}
    else:
        effects = {str(e).lower() for e in (card.get("frame_effects") or [])}
    keywords = {str(k).lower() for k in (card.get("keywords") or [])
                + (face.get("keywords") or [])}
    if card_set == "leb":
        base = "leb"
    elif "spree" in effects:
        base = "spree"
    elif "planeswalker" in tl:
        # planeswalker frames differ per era (2003 Lorwyn frame vs 2015+),
        # the era is carried by the key prefix
        base = "planeswalker"
    elif "room" in tl:
        # rooms (Enchantment — Room) have a split layout but a frame of their
        # own - checked before the split branch
        base = "room"
    elif "battle" in tl:
        # battles (Battle — Siege) are transform-layout cards; the card type
        # identifies them since layout:battle is not a Scryfall qualifier
        base = "battle"
    elif layout == "split":
        sub = next((s for kw, s in SPLIT_KEYWORDS if kw in keywords), "regular")
        base = f"split_{sub}"
    elif layout in LAYOUT_BASES:
        base = LAYOUT_BASES[layout]
    elif layout == "meld" and re.search(r"[a-z]$", str(card.get("collector_number") or "")):
        # the back side of a meld card (letter-suffixed collector number, e.g.
        # emn/15b) is its own layout; the front is a normal card
        base = "meld"
    elif layout not in LAYOUT_SAME_AS_NORMAL and layout != "normal":
        print("warning: unsupported layout")
        base = "default"
    else:
        # normal cards and the same-as-normal layouts (flip, adventure,
        # prepare, mutate, prototype, host, meld front) share the frame-era
        # border; transform/meld marks come from the frame effects (dfc
        # bucket)
        base = "default"
    # a back side never has a holostamp and never the DFC mark: the second
    # face of a transform/modal_dfc card, or the back of a meld card
    faces = card.get("card_faces") or []
    is_back = base == "meld" or (bool(faces) and face is not faces[0])
    # the back side of a transform card that is a planeswalker on both sides
    # has a unique frame no other card has
    pw_back = (is_back and layout == "transform" and len(faces) > 1
               and all("planeswalker" in (f.get("type_line") or "").lower()
                       for f in faces))
    crowned = has_crown(card, face)
    # card type matters for the frame: creature and non-creature frames
    # differ (power/toughness box), planeswalkers have their own frame.
    # Vehicles (Artifact — Vehicle) count as creatures: they carry the
    # power/toughness box, so their frame is the creature frame
    if "planeswalker" in tl:
        typecat = "planeswalker"
    elif "creature" in tl or "vehicle" in tl:
        typecat = "creature"
    else:
        typecat = "other"
    if crowned and typecat != "planeswalker":
        variant = f"legendary_{typecat}_crowned"
    elif base in ("default", "modal") and era == "2015":
        # only the 2015 frame differentiates creature and non-creature
        # default/modal frames (power/toughness box) - the
        # 1993/1997/2003/future frames share one border ring
        variant = typecat
    else:
        variant = "default"
    # frame-effect buckets ("legendary" is carried by the crowned variant,
    # "spree" by the base) plus the holostamp, all as "+" extras of the key
    buckets = sorted({b for b in (effect_bucket(e) for e in effects) if b})
    if is_back:
        # a back side never carries the DFC mark
        buckets = [b for b in buckets if b not in ("dfc", "fandfc")]
    elif not {"dfc", "fandfc"} & set(buckets) and (
            (layout == "transform" and base != "battle")
            or (layout == "modal_dfc"
                and (card.get("released_at") or "") >= SPIDERMAN_DATE)):
        # a transform front always carries a DFC mark - when Scryfall does
        # not report the frame effect, the dfc bucket is assumed (fandfc is
        # its own mark and counts as one). Battles are exempt: their
        # transform layout carries no DFC mark. From Marvel's Spider-Man
        # (SPIDERMAN_DATE) on, a modal_dfc front carries the same mark,
        # which Scryfall does not report either
        buckets = sorted(set(buckets) | {"dfc"})
    stamp = None if is_back else stamp_of(card, face)
    extras = {b for b in buckets if b not in ("legendary", "spree")}
    if stamp:
        extras.add(stamp_key(stamp))
    if pw_back:
        extras.add("pw_back")
    key = f"{era}_{base}" + (f"_{variant}" if variant != "default" else "")
    if extras:
        key += "+" + "+".join(sorted(extras))
    return RefSpec(key, base, layout, era, effects, buckets, typecat,
                   crowned, stamp, is_back, pw_back)


def ref_search_query(base, layout, era, buckets, typecat,
                     crowned=False, stamp=None, pw_back=False, back=False,
                     released_at=""):
    """Scryfall query for a white card with the same frame characteristics.

    Frame era, layout, the legendary crown (frame effect "legendary", Dominaria
    2018-04-27 onward - the only legendary trait that changes the frame), the
    card type (creature / non-creature - for the default and modal bases in
    the 2015 frame only, or when crowned; not queried for a back side, see
    back=), the frame-effect buckets and the
    holostamp (stamp: raw security_stamp of the asked card, None = no stamp,
    queried directly as stamp:<value>, aliased so e.g. "acorn" queries as
    stamp:oval) are
    matched with layout: / frame: / kw: / t: / stamp: qualifiers - and every
    characteristic the asked card does NOT have is negated as well
    (-frame:..., -layout:..., -kw:..., -t:...), so results only contain cards
    with exactly the same frame. pw_back queries the unique back frame of
    transform cards that are planeswalkers on both sides.
    A dfc bucket assumed from the release date (modal_dfc fronts from
    Marvel's Spider-Man / SPIDERMAN_DATE on - Scryfall reports no frame
    effect for that mark) is not queried as a frame effect; candidates are
    matched client-side through their own key instead.
    Alpha (lea) is always excluded - Beta (leb) is taken instead."""
    want = set(buckets or ()) - {"legendary", "spree"}
    if (layout == "modal_dfc" and "dfc" in want
            and (released_at or "") >= SPIDERMAN_DATE):
        # the dfc mark of a Spider-Man-era modal_dfc front is not a reported
        # frame effect: query as if the card had no dfc bucket, the key
        # comparison on the candidates adds it back
        want = want - {"dfc"}
    if pw_back:
        # the unique back frame of transform cards that are planeswalkers on
        # both sides: only another such card has it (on its back side)
        parts = ["layout:transform", "t:planeswalker", "t:legendary",
                 f"frame:{era}"]
    elif base.startswith("split_"):
        parts = ["layout:split", f"frame:{era}"]
        sub = base.split("_", 1)[1]
        if sub != "regular":
            parts.append(f"kw:{sub}")
    elif base == "planeswalker":
        # layout:planeswalker matches nothing on Scryfall, t:planeswalker does;
        # constrain the frame era as well (2003 Lorwyn frame vs 2015+)
        parts = ["t:planeswalker", f"frame:{era}"]
    elif base == "spree":
        parts = ["kw:spree", f"frame:{era}"]
    elif base == "room":
        # rooms have a split layout but their own frame: query the room type
        parts = ["t:room", f"frame:{era}"]
    elif base == "battle":
        # layout:battle is not a Scryfall search qualifier - the card type
        # identifies battles
        parts = ["t:battle", f"frame:{era}"]
    elif base in ("saga", "modal", "leveler", "meld"):
        # class/case share the saga frame, so query sagas for them
        lay = "saga" if base == "saga" else layout
        parts = [f"layout:{lay}", f"frame:{era}"]
    else:
        # normal and the same-as-normal layouts: the frame era alone
        parts = [f"frame:{era}"]
    parts.append("-set:lea")
    parts.append("-set:leb")           # Alpha and Beta are never chosen
    parts.append("c=w")                # Magic color white: border very distinct
    # the holostamp is part of the mask, so the reference must carry the same
    # stamp - searched directly (stamp:<value>, aliased via STAMP_ALIASES so
    # e.g. "acorn" queries as the oval stamp, which shares its artwork and
    # position). For no stamp no query clause is needed - the client-side key
    # comparison in find_white_reference drops wrongly-stamped candidates
    # (frames older than 2015 never had stamps, and there the key comparison
    # passes trivially)
    if stamp:
        s = str(stamp).lower()
        parts.append("stamp:" + STAMP_ALIASES.get(s, s))
    if crowned:
        # the legendary crown (frame effect "legendary", Dominaria 2018+);
        # t:legendary, not is:legendary - the latter matches non-legendary
        # cards
        parts.append("t:legendary")
        parts.append("frame:legendary")
    elif not pw_back:
        # no crown on the asked card: crowned legendaries (frame:legendary)
        # are excluded. Without the crown legendary does not change the
        # frame, so no positive legendary clause is needed either
        parts.append("-frame:legendary")
    if not back and (crowned or (base in ("default", "modal") and era == "2015")):
        # creature and non-creature frames differ (power/toughness box) only
        # in the 2015 frame (default and modal bases), or when crowned; the
        # other frames share one border ring. Skipped for a back side: card
        # level type qualifiers match any face, so they would exclude
        # candidates whose back face has the wanted type - the client-side
        # key comparison on the back face filters those instead
        if typecat == "creature":
            parts.append("t:creature")
        elif typecat == "other":
            parts.extend(["-t:creature", "-t:planeswalker"])
    # one search term per frame-effect bucket present on the card (dfc = any
    # DFC mark), and a negation per known bucket it does not have
    for b in sorted(want):
        parts.append(BUCKET_QUERY.get(b, f"frame:{b}"))
    if not pw_back and base != "battle":
        # battles are transform-layout without a DFC mark (their own base)
        if "dfc" not in want:
            parts.extend(f"-frame:{e}" for e in sorted(DFC_EFFECTS))
        if "fandfc" not in want:
            parts.append("-frame:fandfc")
        if "dfc" not in want and "fandfc" not in want:
            # no DFC mark on the asked card: plain cards only
            parts.append("-layout:transform")
    if base != "modal":
        parts.append("-layout:modal_dfc")
    if base != "spree":
        parts.append("-kw:spree")
    # spacecraft frames differ inside the border ring, so a spacecraft is never
    # used as a reference (a spacecraft card gets the plain reference mask,
    # intersected with its own autodetected mask at apply time)
    parts.append("-t:spacecraft")
    return " ".join(parts)


def card_png(c):
    """PNG image URI of a card - card-level, or the first face carrying one
    (multi-faced cards such as transform cards only have image_uris on their
    faces, not on the card itself)."""
    png = (c.get("image_uris") or {}).get("png")
    if not png:
        for f in c.get("card_faces") or []:
            png = (f.get("image_uris") or {}).get("png")
            if png:
                break
    return png


def candidate_key(c, back=False):
    """Reference key of a search-result card, computed on its front face - or
    on its back face when back=True. For a back-side comparison the stamp and
    DFC-mark extras are stripped (back sides never carry them), so a plain
    card can match a back face of the same frame."""
    faces = c.get("card_faces") or []
    face = faces[-1] if (back and len(faces) > 1) else (faces[0] if faces else c)
    key = ref_key(c, face).key
    if back:
        head, _, extras = key.partition("+")
        keep = [e for e in extras.split("+")
                if e and not e.startswith("stamp") and e not in ("dfc", "fandfc")]
        key = head + ("+" + "+".join(keep) if keep else "")
    return key


def reference_png(ref, back=False):
    """PNG image URI of the reference card: its back face for a back-side mask
    (only multi-faced references carry one), else the usual first image."""
    faces = ref.get("card_faces") or []
    if back and len(faces) > 1:
        png = (faces[-1].get("image_uris") or {}).get("png")
        if png:
            return png
    return card_png(ref)


def find_white_reference(query, key=None, back=False):
    """The one useful mono-white printing for the query, or None: only the first
    page of search results is fetched. When key is given, a candidate is kept
    only if its own reference key equals it - every aspect of the card must
    match in both directions: a characteristic the asked card has (frame era,
    layout/base, legendary crown, card type, frame-effect buckets, holostamp,
    meld back, ...) is required of the candidate, and one it does not have
    excludes the candidate (no crown for a crownless card, no DFC mark for a
    non-transform card, no stamp for an unstamped one, ...). With back=True
    the candidate's back face is compared. Among the matches the blandest
    (plain border, no promo/full-art/showcase/...), oldest one is picked. When
    no mono-white candidate exists, the first candidate with white in one of
    its faces and black in none of them is used instead (a black-containing
    face means a black frame, where the border is not distinct). A foil-only
    printing and an Alpha (lea) printing are never selected when another
    candidate exists."""
    results = search_cards(query, paginate=False)
    # white in the card's colors or in one of its faces, and a png somewhere
    # (two-faced cards can have a null card-level colors and no card-level
    # image_uris while a face is white and has the image, e.g. battles)
    cands = [c for c in results
             if card_png(c)
             and ("W" in (c.get("colors") or [])
                  or any("W" in (f.get("colors") or [])
                         for f in (c.get("card_faces") or [])))]
    # a foil-only printing is never selected when a non-foil one exists
    nonfoil = [c for c in cands if not (c.get("foil") and not c.get("nonfoil"))]
    if nonfoil:
        cands = nonfoil
    # Alpha (lea) is never chosen - Beta (leb) or another set instead
    non_lea = [c for c in cands if (c.get("set") or "").lower() != "lea"]
    if non_lea:
        cands = non_lea
    if key is not None:
        # exact match on every aspect of the card, in both directions
        cands = [c for c in cands if candidate_key(c, back) == key]
    mono = [c for c in cands if c.get("colors") == ["W"]]
    if not mono:
        # fallback: the first candidate with white in one of its faces and
        # black in none of them - a black-containing face means a black frame,
        # where the border is not distinct against it
        for c in cands:
            fcs = [set(f.get("colors") or []) for f in (c.get("card_faces") or [c])]
            if any("W" in fc for fc in fcs) and not any("B" in fc for fc in fcs):
                mono = [c]
                break
    pool = mono or cands
    if not pool:
        return None
    pool.sort(key=lambda c: (blandness(c), c.get("released_at") or "",
                             (c.get("set") or "").lower(), str(c.get("collector_number") or "")))
    return pool[0]


def reference_mask(key, card, face, ref_dir, args):
    """Grayscale border mask for the key, from the cache or built from a white
    reference card. Returns (mask float HxW or None, source description)."""
    path = ref_dir / f"{key}.png"
    if path.is_file():
        m = np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0
        return m, f"cached {path}"

    spec = ref_key(card, face)
    query = ref_search_query(spec.base, spec.layout, spec.era, spec.buckets,
                             spec.typecat, crowned=spec.crowned,
                             stamp=spec.stamp, pw_back=spec.pw_back,
                             back=spec.is_back,
                             released_at=card.get("released_at") or "")
    ref = find_white_reference(query, key=key, back=spec.is_back)
    if ref is None:
        return None, None
    print(f"  generating new mask '{key}' from white reference "
          f"'{ref.get('name')}' ({ref.get('set')}/{ref.get('collector_number')}, "
          f"stamp {ref.get('security_stamp') or 'none'})")
    png = reference_png(ref, back=spec.is_back)
    img = Image.open(io.BytesIO(http_get(png)))
    region, _border, border_hex = detect_region(
        img, threshold=args.threshold, max_hole=args.max_hole)
    if region is None or not region.any():
        return None, None
    mask = mask_from_region(region, card_mask(img))
    ref_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray((mask * 255).round().astype(np.uint8), "L").save(path)
    print(f"  saved new mask to {path}")
    src = (f"reference '{ref.get('name')}' ({ref.get('set')}/{ref.get('collector_number')}, "
           f"border {border_hex}) -> {path}")
    return mask, src


# ------------------------------------------------------------------ pipeline

def apply_mask(img, mask, border=None, scale=1.0, mpc=False):
    """Negate the card inside the border mask.

    mask 1 (resized to the card, NEAREST so the mask levels survive) -> pixel
    inverted (255 - pixel); 0 -> untouched. The inner edge pixels
    (0 < mask < 1: the blend band lines of the mask) blend the NEW border
    color of the card into the pixel, each with its own mask gray value read
    from the mask as the blend weight:
        new = new_border * gray + old * (1 - gray)
    where new_border = 255 - border (border: the detected border color of this
    card, so light / white-ish). Without a border color the inner edge pixels
    are blended toward the negated pixel the same way.
    Returns (image, weight, frac, inner_edge)."""
    arr = np.asarray(img.convert("RGBA")).copy()
    cmask = card_mask(img)
    h, w = cmask.shape
    mask = resize_mask(mask, h, w)
    weight = (mask >= 1.0) & cmask                       # full negate
    inner = (mask > 0.0) & (mask < 1.0) & cmask          # inner edge
    a = weight[..., None].astype(np.float32)
    rgb = arr[..., :3].astype(np.float32)
    arr[..., :3] = np.round(rgb * (1 - a) + (255.0 - rgb) * a).astype(np.uint8)
    gray = mask[inner].astype(np.float32)   # blend weight = mask gray value
    if border is not None:
        new_border = 255.0 - np.asarray(border, dtype=np.float32)  # light, ~white
        arr[..., :3][inner] = np.round(
            new_border * gray[:, None] + rgb[inner] * (1.0 - gray[:, None])
        ).astype(np.uint8)
    else:
        neg = 255.0 - rgb[inner]
        arr[..., :3][inner] = np.round(
            neg * gray[:, None] + rgb[inner] * (1.0 - gray[:, None])
        ).astype(np.uint8)
    out = Image.fromarray(arr, "RGBA")
    if mpc:
        out = out.resize(MPC_SIZE, Image.LANCZOS)
    elif scale != 1.0:
        out = out.resize((max(1, round(out.width * scale)),
                          max(1, round(out.height * scale))), Image.LANCZOS)
    frac = 100.0 * weight.sum() / max(1, cmask.sum())
    return out, weight.astype(np.float32), frac, inner


def process_image(img, args, card=None, face=None):
    """Negate the border of one card image. Returns (output image, stats).

    With Scryfall metadata (card/face) the border mask comes from the cached
    white reference for the card's characteristics; otherwise (or when no
    reference exists) it is detected on the image itself - and, when a key is
    known, cached under that key so later runs load it instead of
    re-detecting. Spacecraft (Artifact — Spacecraft) have a frame that differs
    inside the border ring: their mask is the intersection of the reference
    mask a non-spacecraft card would use and the mask autodetected on the card
    itself."""
    region, border, border_hex = detect_region(
        img, threshold=args.threshold, max_hole=args.max_hole)
    stats = {"found": False, "frac": 0.0, "border": border_hex, "mask": "none"}

    mask = None
    src = None
    key = None
    spacecraft = False
    if card is not None:
        layout = (card.get("layout") or "").lower()
        if layout in LAYOUT_UNSUPPORTED:
            print(f"  warning: layout '{layout}' is not supported "
                  f"- leaving the card unchanged")
            return img.convert("RGBA"), stats
        f = face or card
        tl = (f.get("type_line") or card.get("type_line") or "").lower()
        spacecraft = "spacecraft" in tl
        key = ref_key(card, f).key
        mask, src = reference_mask(key, card, f, Path(args.ref_dir), args)
        if mask is None:
            print(f"  warning: no white reference card found for '{key}' "
                  f"- detecting the border on the card itself")
        else:
            # the mask comes from another card's scan: nudge it to fit this
            # one (self-detected masks are skipped - they already fit)
            cmask = card_mask(img)
            mask = resize_mask(mask, cmask.shape[0], cmask.shape[1])
            if spacecraft and region is not None and region.any():
                # spacecraft frames differ inside the border ring: intersect
                # the reference mask (the one a non-spacecraft card of the
                # same characteristics would use) with the autodetected mask
                inter = (mask > 0) & region
                if inter.any():
                    mask = float_mask_from_region(inter, cmask)
                    src = f"{src} (intersected with the autodetected mask: spacecraft)"
                else:
                    print("  warning: empty intersection with the autodetected "
                          "mask - using the autodetected mask alone")
                    mask = float_mask_from_region(region, cmask)
                    src = f"{src} (autodetected only: spacecraft)"
            mask, info = align_mask(mask, img, border)
            if info is None:
                # no adjustment: re-derive the mask levels so old cache
                # entries (without the inner edge band) get them too
                mask = float_mask_from_region(mask > 0, cmask)
            if info:
                print(f"  adjusted mask to the card: shift {info['shift']}, "
                      f"grow {info['grow']} -> {info['fp']} non-black px covered, "
                      f"{info['fn']} border px uncovered")
                src = f"{src} (aligned: shift {info['shift']}, grow {info['grow']})"
    if mask is None:
        if region is None or not region.any():
            print(f"  warning: could not detect a border (color {border_hex}), "
                  f"leaving the card unchanged")
            return img.convert("RGBA"), stats
        mask = mask_from_region(region, card_mask(img))
        src = "detected on the card itself"
        if key is not None and not spacecraft:
            # spacecraft masks are not cached under the plain key: their frame
            # differs, the mask is intersected with the autodetected one above
            # when a reference exists
            # cache the self-detected mask under the key too, so later runs
            # with the same characteristics load it from the cache
            ref_dir = Path(args.ref_dir)
            path = ref_dir / f"{key}.png"
            print(f"  generating new mask '{key}' from the card itself "
                  f"'{card.get('name')}' (no white reference available)")
            ref_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray((mask * 255).round().astype(np.uint8), "L").save(path)
            print(f"  saved new mask to {path}")
            src = f"self-detected on '{card.get('name')}' -> {path}"

    out, _weight, frac, inner = apply_mask(img, mask, border=border,
                                           scale=args.scale, mpc=args.mpc)
    stats.update(found=True, frac=frac, mask=src, inner=int(inner.sum()))
    return out, stats


def report(tag, dst, out, stats):
    print(f"{tag} -> {dst}  (border color {stats.get('border')}, mask: {stats.get('mask')}, "
          f"{stats['frac']:.1f}% of card negated, {stats.get('inner', 0)} px inner edge "
          f"flat/blended, {out.width}x{out.height})")


def process_file(src, dst, args):
    out, stats = process_image(Image.open(src), args)
    out.save(dst)
    report(src, dst, out, stats)


def process_dir(src, args):
    out_dir = Path(args.output) if args.output else src.parent / f"{src.name}-negated"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in src.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES)
    if not files:
        raise SystemExit(f"error: no images ({', '.join(sorted(IMAGE_SUFFIXES))}) in {src}")
    for f in files:
        process_file(f, out_dir / f.name, args)


def process_url(link, args):
    card = get_card(link)
    faces = card_faces(card)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", card.get("name", "card")).strip("_") or "card"
    for i, face in enumerate(faces):
        uris = face.get("image_uris") or card.get("image_uris") or {}
        png = uris.get("png")
        if not png:
            print(f"  face {i + 1} ({face.get('name', '')}): no png, skipped")
            continue
        img = Image.open(io.BytesIO(http_get(png)))
        out, stats = process_image(img, args, card=card, face=face)
        if args.output:
            p = Path(args.output)
            dst = p if len(faces) == 1 else p.with_name(f"{p.stem}_face{i + 1}{p.suffix or '.png'}")
        else:
            dst = Path(f"{name}_negated.png" if len(faces) == 1
                        else f"{name}_face{i + 1}_negated.png")
        out.save(dst)
        report(f"  face {i + 1} ({face.get('name', '')})", dst, out, stats)


def main():
    p = argparse.ArgumentParser(
        prog="negate-border",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="card image file, a directory of images, or a Scryfall card link")
    p.add_argument("-o", "--output",
                   help="output file (file/URL input) or directory (dir input); "
                        "default: <name>_negated.<ext> / <dir>-negated/ / <card>_negated.png")
    p.add_argument("--threshold", type=int, default=32,
                   help="max per-channel distance from the detected border color for a "
                        "pixel to count as border when detecting it on a card "
                        "(default: 32)")
    p.add_argument("--max-hole", type=int, default=20000,
                   help="largest enclosed pocket inside the border that still becomes part "
                        "of the mask - covers the copyright / rarity / set info text and "
                        "the holostamp in the bottom band but not the card interior "
                        "(default: 20000)")
    p.add_argument("--ref-dir", default=str(DEFAULT_REF_DIR),
                   help=f"directory for the cached white-reference border masks "
                        f"(default: {DEFAULT_REF_DIR})")
    p.add_argument("--scale", type=float, default=1.0,
                   help="upscale the output by this factor (LANCZOS) (default: 1.0)")
    p.add_argument("--mpc", action="store_true",
                   help=f"resize output to the MPC Autofill format: 63x88 mm card at the "
                        f"press's max 800 DPI = {MPC_SIZE[0]}x{MPC_SIZE[1]} px PNG "
                        f"(overrides --scale)")
    args = p.parse_args()

    if args.input.startswith(("http://", "https://")):
        process_url(args.input, args)
    else:
        src = Path(args.input)
        if src.is_dir():
            process_dir(src, args)
        elif src.is_file():
            dst = Path(args.output) if args.output else src.with_name(f"{src.stem}_negated{src.suffix}")
            process_file(src, dst, args)
        else:
            raise SystemExit(f"error: no such file or directory: {src}")


if __name__ == "__main__":
    main()
