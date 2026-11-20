#!/usr/bin/env python3
"""negate_border.py - negate the black border of a Magic: The Gathering card.

Given a Scryfall card link, the border is not taken from the asked card
directly: the script first finds a reference card with all the same
characteristics (frame era / layout / legendary / frame effects / holostamp)
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

Inside the border the card is inverted (negative filter, 255 - pixel); the
border region is grown by one pixel into the card interior and the innermost
line of that grown region carries mask value 0.5, applied lightened
(max(original, 255 - original) per channel) so no dark line can appear
between the white border and the card, and that line is not part of the
border itself. The outermost pixel line of the card is negated like the rest
of the border.
Enclosed pockets inside the border (the copyright / rarity / set info text
in the black bottom band, and the holostamp - the regular oval stamp or the
Universes Beyond triangle stamp, whatever its position in the bottom band)
are part of the mask and are negated along with the border. The alpha
channel is never touched, so rounded corners stay clean.

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
those layouts).

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
from pathlib import Path

import numpy as np
from PIL import Image

USER_AGENT = "magic-frame/1.0 (negate-border card tool)"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# mask value of the innermost line of pixels of the border region (one pixel
# inside the border); applied lightened - max(original, 255 - original) per
# channel - so it can never be darker than the negated border and no dark
# line shows between the white border and the card
INNER_EDGE_MARK = 0.5

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
    return str(frame or "") or "2015"


def stamp_of(card, face=None):
    """Raw Scryfall security_stamp of a card face (None = no stamp): "oval"
    for the regular holo stamp (M15 onward), "triangle" for the Universes
    Beyond stamp (different shape and position), other values for other
    special stamps."""
    ss = (face or {}).get("security_stamp") or card.get("security_stamp")
    return str(ss).lower() if ss else None


def stamp_key(stamp):
    """Stamp component of a reference key: None (no stamp), "stamp" for the
    regular oval stamp, "stamp_<value>" for any other stamp (e.g.
    "stamp_triangle" for the Universes Beyond stamp). Stamped and
    non-stamped cards - and UB vs regular stamps - get different masks."""
    if not stamp:
        return None
    return "stamp" if stamp == "oval" else f"stamp_{stamp}"


# layouts with their own frame (the base of the reference key); class and case
# share the saga frame
LAYOUT_BASES = {"saga": "saga", "class": "saga", "case": "saga",
                "battle": "battle", "modal_dfc": "modal", "leveler": "leveler"}
# split cards are refined by subtype, detected from the keywords
SPLIT_KEYWORDS = (("fuse", "fuse"), ("aftermath", "aftermath"))
# layouts with the same border as a normal card of the same frame era;
# transform/meld marks come from the frame effects (dfc bucket)
LAYOUT_SAME_AS_NORMAL = {"flip", "adventure", "prepare", "mutate", "prototype",
                         "host", "transform", "meld"}
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


def add_common_filters(uri):
    """Add (game:paper) to the q parameter of a Scryfall /cards/search URI
    (other URIs are returned unchanged)."""
    u = urllib.parse.urlsplit(uri)
    if not u.path.endswith("/cards/search"):
        return uri
    params = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    for i, (k, v) in enumerate(params):
        if k == "q" and "(game:paper)" not in v.split() and "-(game:paper)" not in v.split():
            params[i] = (k, "(game:paper) " + v)
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
    """Oldest printing of the card; Alpha (lea) is skipped in favor of Beta (leb).

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
    prints.sort(key=lambda c: (c.get("released_at") or "", (c.get("set") or "").lower(),
                               blandness(c), str(c.get("collector_number") or "")))
    oldest = prints[0]
    if (oldest.get("set") or "").lower() == "lea":
        leb = next((c for c in prints if (c.get("set") or "").lower() == "leb"), None)
        if leb is not None:
            oldest = leb
    return oldest


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

    Returns (region mask or None, border color hex)."""
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
        return None, hexc

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
    return region_full, hexc


def mask_from_region(region, card):
    """Grayscale float mask from a binary border region: the region is grown by
    one pixel into the card interior first, so the lightened line is not part
    of the border itself. 1 inside the grown region, INNER_EDGE_MARK on its
    innermost line of pixels (applied lightened), 0 outside."""
    region = dilate8(region) & card
    inner = region & dilate8(card & ~region)
    m = np.zeros(region.shape, np.float32)
    m[region] = 1.0
    m[inner] = INNER_EDGE_MARK
    return m


# ------------------------------------------------------- reference cards

def ref_key(card, face):
    """Cache key + search ingredients for the white reference of a card face.

    The key is a composition of every characteristic that can change the
    border ring, not an exclusive choice - it always starts with the frame
    era (the "date"):
        <era>_<base>[_<variant>][+<extras>]
    base: leb, spree, planeswalker, split_<fuse|aftermath|regular>, saga (also
    class/case), battle, modal (modal_dfc), leveler, meld (the back side of a
    meld card - the front carries the meld mark and is a normal card), or
    "default" for normal and the same-as-normal layouts - e.g. leveler exists
    both as 2003_leveler and 2015_leveler;
    variant: legendary (crowned, plus creature/non-creature) when legendary;
    extras: one per relevant frame-effect bucket (dfc, fandfc, unknown) plus
    the holostamp (stamp, or stamp_<value>, e.g. stamp_triangle for the
    Universes Beyond stamp).
    Examples: 2015_default, 2015_default+stamp, 2015_default+dfc+stamp,
    2015_default_legendary_creature_crowned+stamp, 2003_leveler, 2015_saga.
    """
    layout = (card.get("layout") or "").lower()
    era = frame_era(card.get("frame", ""))
    card_set = (card.get("set") or "").lower()
    tl = (face.get("type_line") or card.get("type_line") or "").lower()
    effects = {str(e).lower() for e in (card.get("frame_effects") or [])
               + (face.get("frame_effects") or [])}
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
    elif layout == "split":
        sub = next((s for kw, s in SPLIT_KEYWORDS if kw in keywords), "regular")
        base = f"split_{sub}"
    elif layout in LAYOUT_BASES:
        base = LAYOUT_BASES[layout]
    elif layout == "meld" and re.search(r"[a-z]$", str(card.get("collector_number") or "")):
        # the back side of a meld card (letter-suffixed collector number, e.g.
        # emn/15b) is its own layout; the front is a normal card
        base = "meld"
    else:
        # normal and LAYOUT_SAME_AS_NORMAL share the frame-era border
        base = "default"
    legendary = "legendary" in effects or "legendary" in tl
    # the legendary crown (frame effect "legendary") was introduced with
    # Dominaria (CROWN_DATE) and only exists on 2015/future-frame legendary
    # non-planeswalkers; printings without the frame_effects backfill are
    # placed by release date
    crowned = ("legendary" in effects or (
        legendary and "planeswalker" not in tl
        and frame_era(card.get("frame", "")) in ("2015", "future")
        and (card.get("released_at") or "") >= CROWN_DATE))
    # card type matters for the frame: legendary creature and non-creature
    # frames differ (power/toughness box), planeswalkers have their own frame
    if "planeswalker" in tl:
        typecat = "planeswalker"
    elif "creature" in tl:
        typecat = "creature"
    else:
        typecat = "other"
    if legendary:
        variant = "legendary" if typecat == "planeswalker" else f"legendary_{typecat}"
        if crowned and typecat != "planeswalker":
            variant += "_crowned"
    else:
        variant = "default"
    # frame-effect buckets ("legendary" is carried by the variant, "spree" by
    # the base) plus the holostamp, all as "+" extras of the key
    buckets = sorted({b for b in (effect_bucket(e) for e in effects) if b})
    stamp = stamp_of(card, face)
    extras = {b for b in buckets if b not in ("legendary", "spree")}
    if stamp:
        extras.add(stamp_key(stamp))
    key = f"{era}_{base}" + (f"_{variant}" if variant != "default" else "")
    if extras:
        key += "+" + "+".join(sorted(extras))
    return key, base, layout, era, legendary, effects, buckets, typecat, crowned, stamp


def ref_search_query(base, layout, era, legendary, buckets, typecat,
                     crowned=False, stamp=None):
    """Scryfall query for a white card with the same frame characteristics.

    Frame era, layout, the legendary crown (frame effect "legendary", Dominaria
    2018-04-27 onward), the frame-effect buckets and the holostamp (stamp:
    raw security_stamp of the asked card, None = no stamp) are matched with
    layout: / frame: / date / kw: / r: / is: qualifiers."""
    if base.startswith("split_"):
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
    elif base in ("saga", "battle", "modal", "leveler", "meld"):
        # class/case share the saga frame, so query sagas for them
        lay = "saga" if base == "saga" else layout
        parts = [f"layout:{lay}", f"frame:{era}"]
    else:
        # normal and the same-as-normal layouts: the frame era alone
        parts = [f"frame:{era}"]
    parts.append("c=w")                # Magic color white: border very distinct
    # the holostamp is part of the mask, so the reference must carry the same
    # stamp: the regular oval stamp (r>=rare, not Universes Beyond) or a
    # Universes Beyond stamp such as the triangle (is:universesbeyond). For
    # no stamp no query clause is needed - the client-side security_stamp
    # check in find_white_reference drops stamped candidates (frames older
    # than 2015 never had stamps, and there the check passes trivially)
    if stamp == "oval":
        parts.extend(["r>=rare", "-is:universesbeyond"])
    elif stamp:
        parts.extend(["r>=rare", "is:universesbeyond"])
    if legendary:
        # t:legendary, not is:legendary - the latter matches non-legendary cards
        parts.append("t:legendary")
        if crowned:
            # the legendary crown: frame effect "legendary" (Dominaria 2018+)
            parts.append("frame:legendary")
        elif era in ("2015", "future"):
            # crownless 2015-frame printings all predate Dominaria
            parts.append(f"date<{CROWN_DATE}")
        if typecat == "creature":
            parts.append("t:creature")
        elif typecat == "other":
            parts.extend(["-t:creature", "-t:planeswalker"])
    # one search term per frame-effect bucket (dfc = any DFC mark)
    for b in sorted(set(buckets) - {"legendary", "spree"}):
        parts.append(BUCKET_QUERY.get(b, f"frame:{b}"))
    return " ".join(parts)


def find_white_reference(query, legendary=False, typecat=None, era=None,
                         crowned=False, buckets=None, stamp=None, meld_back=False):
    """The one useful mono-white printing for the query, or None: only the first
    page of search results is fetched, then the blandest (plain border, no
    promo/full-art/showcase/...), oldest one is picked. When legendary is set,
    non-legendary results are dropped client-side (Scryfall's is:legendary is
    unreliable, hence t:legendary in the query plus this check); typecat
    ('creature' / 'planeswalker' / 'other'), frame era, crown era (the
    frame:legendary effect, Dominaria 2018-04-27 onward), the frame-effect
    buckets and the holostamp (stamp: raw security_stamp, checked client-side
    because it is not a search qualifier) are checked the same way, so e.g. a
    crownless, DFC-less or wrongly-stamped reference is never used for a
    crowned / transforming / stamped card. When no mono-white candidate exists,
    the first candidate with white in one of its faces and black in none of
    them is used instead (a black-containing face means a black frame, where
    the border is not distinct)."""
    results = search_cards(query, paginate=False)
    cands = [c for c in results
             if (c.get("image_uris") or {}).get("png") and "W" in (c.get("colors") or [])]
    if meld_back:
        # only other meld backs (letter-suffixed collector numbers) match
        cands = [c for c in cands
                 if re.search(r"[a-z]$", str(c.get("collector_number") or ""))]
    if era:
        cands = [c for c in cands if str(c.get("frame")) == str(era)]
    cands = [c for c in cands if stamp_of(c) == (stamp or None)]
    if legendary:
        cands = [c for c in cands if "legendary" in (c.get("type_line") or "").lower()]
        if crowned:
            cands = [c for c in cands
                     if "legendary" in {str(e).lower()
                                        for e in (c.get("frame_effects") or [])}
                     or (c.get("released_at") or "") >= CROWN_DATE]
        elif era in ("2015", "future"):
            cands = [c for c in cands if (c.get("released_at") or "") < CROWN_DATE]
    want = set(buckets or ()) - {"legendary", "spree"}
    if buckets is not None:
        cands = [c for c in cands
                 if {b for b in (effect_bucket(e, warn=False)
                                 for e in (c.get("frame_effects") or [])) if b}
                 - {"legendary", "spree"} == want]
    if typecat == "creature":
        cands = [c for c in cands if "creature" in (c.get("type_line") or "").lower()]
    elif typecat == "planeswalker":
        cands = [c for c in cands if "planeswalker" in (c.get("type_line") or "").lower()]
    elif typecat == "other":
        cands = [c for c in cands
                 if "creature" not in (c.get("type_line") or "").lower()
                 and "planeswalker" not in (c.get("type_line") or "").lower()]
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

    (_, base, layout, era, legendary, _effects, buckets, typecat, crowned,
     stamp) = ref_key(card, face)
    query = ref_search_query(base, layout, era, legendary, buckets, typecat,
                             crowned=crowned, stamp=stamp)
    ref = find_white_reference(query, legendary=legendary, typecat=typecat,
                               era=era, crowned=crowned, buckets=buckets,
                               stamp=stamp, meld_back=(base == "meld"))
    if ref is None:
        return None, None
    print(f"  generating new mask '{key}' from white reference "
          f"'{ref.get('name')}' ({ref.get('set')}/{ref.get('collector_number')}, "
          f"stamp {ref.get('security_stamp') or 'none'})")
    png = (ref.get("image_uris") or {}).get("png")
    img = Image.open(io.BytesIO(http_get(png)))
    region, border_hex = detect_region(
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

def apply_mask(img, mask, scale=1.0, mpc=False):
    """Negate the card inside the border mask.

    mask 1 (resized to the card, NEAREST so the 0 / 0.5 / 1 values survive) ->
    pixel inverted (255 - pixel); the innermost border line (0 < mask < 1) ->
    lightened to max(original, 255 - original) per channel (opaque, so no
    dark line can show between the negated border and the card); 0 ->
    untouched. Returns (image, weight, frac, inner)."""
    arr = np.asarray(img.convert("RGBA")).copy()
    cmask = card_mask(img)
    h, w = cmask.shape
    if mask.shape != (h, w):
        mask = np.asarray(
            Image.fromarray((mask * 255).round().astype(np.uint8), "L")
            .resize((w, h), Image.NEAREST), dtype=np.float32) / 255.0
    weight = (mask >= 1.0) & cmask          # full negate
    inner = (mask > 0.0) & (mask < 1.0) & cmask  # innermost line -> blend
    a = weight[..., None].astype(np.float32)
    rgb = arr[..., :3].astype(np.float32)
    arr[..., :3] = np.round(rgb * (1 - a) + (255.0 - rgb) * a).astype(np.uint8)
    # innermost line: lightened (never darker than the negated border), so
    # the white border blends into the card without a dark line - negating a
    # light card-edge pixel would produce a black line on white cards
    arr[..., :3][inner] = np.maximum(rgb[inner], 255.0 - rgb[inner]).astype(np.uint8)
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
    re-detecting."""
    region, border_hex = detect_region(
        img, threshold=args.threshold, max_hole=args.max_hole)
    stats = {"found": False, "frac": 0.0, "border": border_hex, "mask": "none"}

    mask = None
    src = None
    key = None
    if card is not None:
        layout = (card.get("layout") or "").lower()
        if layout in LAYOUT_UNSUPPORTED:
            print(f"  warning: layout '{layout}' is not supported "
                  f"- leaving the card unchanged")
            return img.convert("RGBA"), stats
        key = ref_key(card, face or card)[0]
        mask, src = reference_mask(key, card, face or card, Path(args.ref_dir), args)
        if mask is None:
            print(f"  warning: no white reference card found for '{key}' "
                  f"- detecting the border on the card itself")
    if mask is None:
        if region is None or not region.any():
            print(f"  warning: could not detect a border (color {border_hex}), "
                  f"leaving the card unchanged")
            return img.convert("RGBA"), stats
        mask = mask_from_region(region, card_mask(img))
        src = "detected on the card itself"
        if key is not None:
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

    out, _weight, frac, inner = apply_mask(img, mask, scale=args.scale, mpc=args.mpc)
    stats.update(found=True, frac=frac, mask=src, inner=int(inner.sum()))
    return out, stats


def report(tag, dst, out, stats):
    print(f"{tag} -> {dst}  (border color {stats.get('border')}, mask: {stats.get('mask')}, "
          f"{stats['frac']:.1f}% of card negated, {stats.get('inner', 0)} px inner line "
          f"lightened, {out.width}x{out.height})")


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
