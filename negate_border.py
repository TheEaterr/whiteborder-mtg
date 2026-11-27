#!/usr/bin/env python3
"""negate_border.py - negate the black border of a Magic: The Gathering card.

Given a Scryfall card link, the border is not taken from the asked card
directly: the script finds a reference card with the same frame
characteristics (frame era / layout / legendary crown / frame effects /
holostamp) but of the Magic color white - e.g. a white saga such as Elspeth
Conquers Death for the black saga Age of Ultron - where the border is very
distinct against the white frame, detects it there, and caches it as a
grayscale mask (ref_masks/, created on demand; manually cleaned-up masks in
ref_masks_clean/ take precedence). Later runs load the cached mask instead
of downloading the reference again. When no white reference exists, the
border is detected on the asked card itself and cached under the same key.
The border color of the processed card itself is sampled from its bottom
edge and reported.

Transformed cards are cached too (card_cache/, keyed by the card image URL
and the detection flags): a face already negated is reloaded from disk
instead of being downloaded and processed again. Card-name resolutions (deck
lists, name_*.json) and Alpha->Unlimited decisions (unl_*.json) are cached
there as well, and HTTP requests are paced ~100 ms apart, so a repeat run of
the same deck list makes no Scryfall request at all.

Inside the border the card is inverted (255 - pixel). The innermost
FLAT_EDGE_PX + 1 mask lines are not fully negated: there the NEW border color
(255 - the sampled border color, so light) is blended in, each pixel using
its mask gray value as the blend weight:
    new = new_border * gray + old * (1 - gray)
Enclosed pockets in the bottom band (copyright / rarity / set info text,
holostamp) are negated with the border. The alpha channel is never touched,
so rounded corners stay clean.

A reference mask comes from another card's scan, which is not pixel-identical,
so it is slightly adjusted (shift / grow / shrink by a few pixels) to fit the
asked card before being applied. Spacecraft, planeswalker and saga faces have
a frame that differs inside the border ring: their mask is intersected with
the one autodetected on the card itself.

Local image files / directories are processed the same way, except that the
border is detected on the image itself (no Scryfall reference is used).

Usage:
    negate_border.py https://scryfall.com/card/<set>/<number>/<slug> [-o OUT] [flags]
    negate_border.py IMAGE [-o OUT] [flags]
    negate_border.py DIR [-o OUTDIR] [flags]

Detection flags: --threshold / --max-hole.

Default outputs: <name>_negated.<ext> next to the input file, <dir>-negated/
for a directory, <card>_negated.png for a URL (or <card>_faceN_negated.png per
face for transform / modal_dfc cards - second faces are only processed for
those layouts). Each face of a two-faced card gets the mask of its own
characteristics; a back side never carries a holostamp or the DFC mark. Cards
whose oldest printing is Alpha are returned as the Unlimited (2ed) printing
as-is (2ed already has a white border). Unsupported layouts (scheme,
vanguard, token, ...) are left unchanged.

Requires pillow + numpy (provided by the flake devShell:
nix develop --command python3 negate_border.py IMAGE)
"""

import argparse
import hashlib
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

# masks are stored binary (fully white inside the region, black outside);
# the inner-edge blend band is derived at apply time (float_mask_from_region):
# the innermost FLAT_EDGE_PX+1 lines get a gray gradient (i+1)/(FLAT_EDGE_PX+1),
# read back as the blend weight toward the new border color when applying
FLAT_EDGE_PX = 2

# mask alignment: a reference mask comes from another card's scan, which is
# not pixel-identical, so it is nudged to fit (small shifts / grows only).
# Cost = covered non-border px + ALIGN_BORDER_WEIGHT * uncovered border px;
# adjustments shrinking the mask below ALIGN_MIN_COVER of its size are rejected
ALIGN_MAX_SHIFT = 3       # max shift in each direction, px
ALIGN_MAX_GROW = 2        # max grow (+) / shrink (-), px
ALIGN_BORDER_WEIGHT = 10  # weight of uncovered border pixels in the cost
ALIGN_MIN_COVER = 0.6     # min fraction of the original mask size to keep
ALIGN_THRESHOLD = 8       # max per-channel distance from the border color for
                          # a pixel to count as border during alignment

# mask caches next to the script: ref_masks_clean/ holds manually cleaned-up
# masks and is looked up FIRST, ref_masks/ is the generated cache new masks
# are saved to. When the script itself lives in a read-only location (e.g.
# the nix store via `nix run`), fall back to ./ref_masks_clean and ./ref_masks
_SCRIPT_DIR = Path(__file__).resolve().parent
if os.access(_SCRIPT_DIR, os.W_OK):
    CLEAN_REF_DIR = _SCRIPT_DIR / "ref_masks_clean"
    REF_DIR = _SCRIPT_DIR / "ref_masks"
else:
    CLEAN_REF_DIR = Path.cwd() / "ref_masks_clean"
    REF_DIR = Path.cwd() / "ref_masks"
REF_DIRS = (CLEAN_REF_DIR, REF_DIR)   # mask lookup order: clean masks first

# transformed-card cache, same location rule as the mask caches: card_cache/
# holds <key>.png (the negated face) + <key>.json (its stats), keyed by the
# face's image URL and the detection flags, so a card is downloaded and
# negated only once
if os.access(_SCRIPT_DIR, os.W_OK):
    CARD_CACHE_DIR = _SCRIPT_DIR / "card_cache"
else:
    CARD_CACHE_DIR = Path.cwd() / "card_cache"


def card_cache_files(png_url, args):
    key = hashlib.sha1(
        f"{png_url}|t{args.threshold}|h{args.max_hole}".encode()).hexdigest()[:16]
    return CARD_CACHE_DIR / f"{key}.png", CARD_CACHE_DIR / f"{key}.json"


def load_cached_face(png_url, args):
    """(image, stats) of a face already transformed, from card_cache/, or None."""
    p, j = card_cache_files(png_url, args)
    if not p.is_file():
        return None
    img = Image.open(p)
    img.load()
    stats = json.loads(j.read_text()) if j.is_file() else None
    return img, stats


def save_cached_face(png_url, args, img, stats):
    CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p, j = card_cache_files(png_url, args)
    img.save(p)
    j.write_text(json.dumps(stats))


def name_cache_file(name):
    """Resolution cache file for a deck-list card name (any card name, face
    name, ... as written in the deck list)."""
    key = hashlib.sha1(f"name|{name.strip().lower()}".encode()).hexdigest()[:16]
    return CARD_CACHE_DIR / f"name_{key}.json"


def unlimited_cache_file(oracle_id):
    """Resolution cache file for the Alpha->Unlimited decision of an oracle id."""
    key = hashlib.sha1(f"unl|{oracle_id}".encode()).hexdigest()[:16]
    return CARD_CACHE_DIR / f"unl_{key}.json"


def cached_mask_path(key):
    """Cached mask file for a key: the manually cleaned ref_masks_clean/
    folder is looked up first, then the generated ref_masks/ cache."""
    for d in REF_DIRS:
        p = d / f"{key}.png"
        if p.is_file():
            return p
    return None

# Dominaria: first set with the legendary crown on the type line (Scryfall
# frame effect "legendary"). The crown only exists on 2015/future-frame
# legendary cards from this date on, and never on planeswalkers
CROWN_DATE = "2018-04-27"

# first Marvel's Spider-Man-wave modal_dfc printing: from this date on,
# modal_dfc fronts carry the DFC-style mark, which Scryfall does not report
# as a frame effect, so the dfc bucket is assumed from the release date
SPIDERMAN_DATE = "2025-09-19"

# ---------------------------------------------------------------- frame model
# Scryfall frame effects -> "bucket" in the reference key. All DFC marks are
# one icon in one spot, so they share the dfc bucket - fandfc is its own art
DFC_EFFECTS = {"sunmoondfc", "compasslanddfc", "originpwdfc", "mooneldrazidfc",
               "waxingandwaningmoondfc", "convertdfc", "upsidedowndfc"}
FRAME_EFFECT_BUCKETS = {"legendary": "legendary", "spree": "spree", "fandfc": "fandfc"}
FRAME_EFFECT_BUCKETS.update({e: "dfc" for e in DFC_EFFECTS})
# frame effects that do not change the border ring (fancy printings are
# excluded as references by blandness anyway)
IGNORED_FRAME_EFFECTS = {"miracle", "enchantment", "draft", "devoid", "tombstone",
                         "colorshifted", "inverted", "showcase", "extendedart",
                         "companion", "etched", "snow", "lesson", "shatteredglass",
                         "dark"}
# Scryfall search term matching a whole bucket (dfc = any DFC mark)
BUCKET_QUERY = {"dfc": "(" + " or ".join(f"frame:{e}" for e in sorted(DFC_EFFECTS)) + ")"}

_WARNED_EFFECTS = set()


def effect_bucket(effect, warn=True):
    """Bucket of a frame effect: known bucket, None if explicitly ignored, else
    the effect itself - unknown effects are logged once, not silently dropped."""
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
    """Scryfall frame era (1993 / 1997 / 2003 / 2015 / future)."""
    return str(frame or "2015")


def has_crown(card, face=None):
    """Whether the face carries the legendary crown: a legendary non-
    planeswalker on a 2015/future frame released from Dominaria (CROWN_DATE)
    on. The crown is the only legendary trait that changes the frame."""
    face = face or {}
    tl = (face.get("type_line") or card.get("type_line") or "").lower()
    return bool("legendary" in tl and "planeswalker" not in tl
                and frame_era(card.get("frame", "")) in ("2015", "future")
                and (card.get("released_at") or "") >= CROWN_DATE)


def stamp_of(card, face=None):
    """Raw security_stamp of a face (None = no stamp): "oval" regular holo
    stamp, "triangle" Universes Beyond, ..."""
    ss = (face or {}).get("security_stamp") or card.get("security_stamp")
    return str(ss).lower() if ss else None


# stamps sharing the oval stamp's artwork and position are aliased to it
STAMP_ALIASES = {"acorn": "oval", "heart": "oval"}
# every security_stamp value (the stamp: qualifier's domain): "no stamp" is
# queried as a negation of each value, so a card without a stamp never takes
# a stamped reference
STAMP_VALUES = {"oval", "triangle", "acorn", "heart", "arena", "circle"}


def stamp_key(stamp):
    """Stamp part of a reference key: None, "stamp" (oval + aliases), or
    "stamp_<value>" - stamped and non-stamped cards get different masks."""
    if not stamp:
        return None
    s = str(stamp).lower()
    s = STAMP_ALIASES.get(s, s)
    return "stamp" if s == "oval" else f"stamp_{s}"


# layouts with their own frame (the key base); class/case share the saga
# frame. Rooms and battles come from the type line instead: rooms have a
# split layout, battles a transform one (layout:battle is not searchable)
LAYOUT_BASES = {"saga": "saga", "class": "saga", "case": "saga",
                "modal_dfc": "modal", "leveler": "leveler"}
# split cards are refined by subtype, from the keywords
SPLIT_KEYWORDS = (("fuse", "fuse"), ("aftermath", "aftermath"))
# layouts with the same border as a normal card of the same era; their
# transform/meld marks come from the frame effects (dfc bucket)
LAYOUT_SAME_AS_NORMAL = {"flip", "adventure", "prepare", "mutate", "prototype",
                         "host", "meld"}
# layouts whose card physically has a second face with its own image
MULTI_FACE_LAYOUTS = {"transform", "modal_dfc"}
# layouts without a usable card border - not supported
LAYOUT_UNSUPPORTED = {"scheme", "vanguard", "augment", "art_series", "planar",
                      "token", "emblem", "front_card", "reversible_card",
                      "double_faced_token"}

# frame effects / traits that make a printing less plain than another
SPECIAL_PRINTING_EFFECTS = {"showcase", "extendedart", "borderless", "etched",
                            "inverted", "fullart", "textless"}

# ---------------------------------------------------------------- HTTP / API

# on a 429 (rate limited) take a small break before continuing; a Retry-After
# header sent with the 429 is honored when it asks for a longer wait
RATE_LIMIT_SLEEP = 5.0

# Scryfall asks for ~50-100 ms between requests; pace them so processing a
# whole deck does not trip the rate limit
MIN_REQUEST_INTERVAL = 0.1
_last_request_at = 0.0


def http_get(url, timeout=60, retries=3):
    """GET a URL with retries (Scryfall/scans CDN occasionally cut connections).
    Requests are paced to at least MIN_REQUEST_INTERVAL apart; a 429 rate-limit
    response pauses for RATE_LIMIT_SLEEP (or Retry-After) before retrying."""
    global _last_request_at
    last = None
    for attempt in range(retries):
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
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
                if e.code == 429:
                    delay = RATE_LIMIT_SLEEP
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                    print(f"  rate limited (HTTP 429) - waiting {delay:.0f}s "
                          f"before continuing")
                    time.sleep(delay)
                else:
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


# injected into every /cards/search query: paper only ((game:paper) - digital
# cards have different images), black-bordered default printings, no promos,
# and none of the off-scan sets (lea/leb, fbb/cei/ced/4bb) - the ONE place
# they are excluded; alpha_unlimited_printing() opts out (common=False)
# because it has to see Alpha to detect Alpha-oldest cards
COMMON_QUERY_TOKENS = ("(game:paper)", "border:black", "-is:promo", "is:default",
                       "-set:lea", "-set:leb", "-set:fbb", "-set:cei", "-set:ced", "-set:4bb")


def add_common_filters(uri):
    """Add the COMMON_QUERY_TOKENS to the q parameter of a /cards/search URI
    (other URIs unchanged); a token already present in either polarity is not
    added again."""
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


def search_all(uri, paginate=True, common=True):
    """Follow a search endpoint's pagination, return every result. With
    common=False the COMMON_QUERY_TOKENS are not injected (Alpha included);
    with paginate=False only the first page is fetched."""
    if common:
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


def printing_order(c):
    """Sort key of a printing: oldest first, then set, then the blandest one
    (plain border, no promo/full-art/showcase/etc.), then collector number."""
    return (c.get("released_at") or "", (c.get("set") or "").lower(),
            blandness(c), str(c.get("collector_number") or ""))


def prefer_nonfoil(cards):
    """Drop foil-only printings when a non-foil one exists."""
    nonfoil = [c for c in cards if not (c.get("foil") and not c.get("nonfoil"))]
    return nonfoil or cards


def prints_uri(card):
    """Scryfall search URI for every printing of a card."""
    uri = card.get("prints_search_uri")
    if not uri and card.get("oracle_id"):
        uri = ("https://api.scryfall.com/cards/search?q=oracle_id:"
               + urllib.parse.quote(card["oracle_id"]) + "&unique=prints")
    return uri


def all_printings(card, common=True):
    """Every printing of a card (same oracle id), sorted by printing_order,
    non-foil preferred. With common=False the COMMON_QUERY_TOKENS are not
    injected (Alpha included) - only alpha_unlimited_printing() needs that."""
    uri = prints_uri(card)
    if not uri:
        return []
    prints = [c for c in search_all(uri, common=common)
              if c.get("oracle_id") == card.get("oracle_id")]
    prints = prefer_nonfoil(prints)
    prints.sort(key=printing_order)
    return prints


def oldest_printing(card):
    """Oldest printing of the card, blandest first within the oldest set
    (Alpha/Beta and the other off-scan sets are already excluded by the
    COMMON_QUERY_TOKENS injected into every search)."""
    prints = all_printings(card)
    return prints[0] if prints else card


def alpha_unlimited_printing(card):
    """The Unlimited (2ed) printing when the card's oldest printing is Alpha
    (lea), else None. Alpha scans are off, so such a card is not run through
    the tool - Unlimited is returned as-is. This is the only search that
    includes Alpha (common=False): it has to see lea to detect it. The
    decision is cached in card_cache/ by oracle id."""
    key = card.get("oracle_id") or card.get("id") or ""
    cf = unlimited_cache_file(key)
    if cf.is_file():
        return json.loads(cf.read_text())
    prints = all_printings(card, common=False)
    unl = None
    if prints and (prints[0].get("set") or "").lower() == "lea":
        candidates = [c for c in prints if (c.get("set") or "").lower() == "2ed"]
        unl = min(candidates, key=printing_order) if candidates else None
    CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(unl))
    return unl


def card_by_name(name):
    """Scryfall card for a plain card name (deck lists). The resolution is
    cached in card_cache/ by (lower-cased) name - a repeat run of the same
    deck list resolves every card with no Scryfall request. The cached value
    may be None (name not found)."""
    cf = name_cache_file(name)
    if cf.is_file():
        return json.loads(cf.read_text())
    card = _resolve_card_by_name(name)
    CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(card))
    return card


def _resolve_card_by_name(name):
    """Exact-name search (printings whose own name is exactly the asked one are
    preferred - an exact-name search also matches face names of other cards,
    e.g. "Lightning Bolt" matches "Emeritus of Conflict // Lightning Bolt"),
    then the oldest printing like get_card() does for a link. Falls back to
    Scryfall's fuzzy lookup (a deck list may carry only one face's name of a
    DFC)."""
    hits = search_cards(f'!"{name}"', paginate=False)
    exact = [c for c in hits if (c.get("name") or "").lower() == name.lower()]
    pool = exact or hits
    if pool:
        return oldest_printing(min(pool, key=printing_order))
    uri = "https://api.scryfall.com/cards/named?" + urllib.parse.urlencode({"fuzzy": name})
    data = json.loads(http_get(uri))
    if data.get("object") == "error":
        return None
    return oldest_printing(data)


def card_faces(card):
    """Faces to process: every face for MULTI_FACE_LAYOUTS (transform,
    modal_dfc - the card physically has a second side), else the front only."""
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
    """Pockets of card enclosed by region, up to max_hole px each, to add to
    the region: the copyright / rarity / set info text and the holostamp in
    the bottom band. The cap keeps the card interior (one huge component) out."""
    if not region.any():
        return np.zeros_like(region)
    # not reachable from outside the card without crossing the border
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
    """Detect the border region of a card image. Returns (region or None,
    border RGB or None, border color hex)."""
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

    # within threshold of the border color (max per channel)
    dist = np.abs(sub[..., :3].astype(np.int16) - border.astype(np.int16)).max(axis=2)
    black = (dist <= threshold) & card

    # border = those pixels connected to the card's outer edge, flooded over
    # the raw mask (no dilation) so the region follows the real border shape
    # and cannot leak through anti-aliased gaps into the card interior
    edge = card & ~erode4(card)        # outermost 1px layer of the card
    region = flood_from_edges(black, black & edge)
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
    """Grayscale float mask from a binary region: 1.0 inside, 0 outside, and
    the gray gradient (i+1)/(FLAT_EDGE_PX+1) on the innermost FLAT_EDGE_PX+1
    lines (the blend band - read back as the blend weight at apply time)."""
    m = np.zeros(region.shape, np.float32)
    m[region] = 1.0
    for i, line in enumerate(inner_lines(region, card, FLAT_EDGE_PX + 1)):
        m[line] = (i + 1) / (FLAT_EDGE_PX + 1)
    return m


def mask_from_region(region, card):
    """Binary float mask from a border region, grown 1px into the card so the
    blend band sits at the border's inner edge. The band is not stored: it is
    derived at apply time, so saved masks stay fully white."""
    return (dilate8(region) & card).astype(np.float32)


def resize_mask(mask, h, w):
    """Resize a mask to (h, w) with NEAREST, so the mask gray levels survive."""
    if mask.shape != (h, w):
        mask = np.asarray(
            Image.fromarray((mask * 255).round().astype(np.uint8), "L")
            .resize((w, h), Image.NEAREST), dtype=np.float32) / 255.0
    return mask


# ------------------------------------------------------- mask alignment

def align_mask(mask, img, border, threshold=ALIGN_THRESHOLD):
    """Nudge a reference mask (shift +/- ALIGN_MAX_SHIFT, grow/shrink +/-
    ALIGN_MAX_GROW) to fit the card. Cost = covered non-border px +
    ALIGN_BORDER_WEIGHT * uncovered border px; the identity wins ties, and a
    shrink below ALIGN_MIN_COVER of the original size is rejected. Returns
    (mask, info or None)."""
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
    info = {"shift": (dy, dx), "grow": g, "fp": fp, "fn": fn}
    return r.astype(np.float32), info   # binary; the blend band is added at apply time


# ------------------------------------------------------- reference cards

# Everything needed to find / identify the white reference of one card face
RefSpec = namedtuple("RefSpec", ["key", "base", "layout", "era", "buckets",
                                 "typecat", "crowned", "stamp",
                                 "is_back", "pw_back"])


def ref_key(card, face):
    """Cache key + search ingredients for the white reference of a card face.

    Key grammar: <era>_<base>[_<variant>][+<extras>]
    base: leb, spree, planeswalker (planeswalker_back on a back side), room,
    battle (from the type line - layout:battle is not searchable),
    split_<fuse|aftermath|regular>, saga (also class/case), modal (modal_dfc,
    composable: modal_planeswalker / modal_battle / ...), leveler, meld (the
    back of a meld card), or "default" for normal and same-as-normal layouts.
    variant: legendary_<creature|other>_crowned when the face carries the
    crown (the only legendary trait that changes the frame), else
    creature/other for default/modal bases in the 2015 frame (power/toughness
    box; vehicles count as creatures) - other frames share one border ring.
    extras: frame-effect buckets (dfc, fandfc, ...), the holostamp (stamp or
    stamp_<value>), pw_back (back of a both-sides-planeswalker transform card
    - a unique frame).

    Non-obvious rules: a back side (second face of transform/modal_dfc, or a
    meld back) never carries a stamp or DFC mark. A transform front (and a
    meld front) always carries a DFC mark, so the dfc bucket is assumed when
    Scryfall does not report it (battles excepted); from SPIDERMAN_DATE on,
    so is a modal_dfc front. Each face uses its own frame effects / type line.
    A face that is both a creature and a saga keeps the creature frame (the
    saga inner frame is handled at apply time, by mask intersection).
    Examples: 2015_default_creature+stamp, 2015_saga+dfc+stamp,
    2015_default_legendary_creature_crowned+stamp, 2003_leveler, 2015_saga.
    """
    layout = (card.get("layout") or "").lower()
    era = frame_era(card.get("frame", ""))
    card_set = (card.get("set") or "").lower()
    tl = (face.get("type_line") or card.get("type_line") or "").lower()
    # the face's own frame_effects when present, else the card-level ones; on
    # a two-faced card "legendary" is reconciled with this face's type line
    # (the card-level effects are the union of both faces')
    face_effects = face.get("frame_effects") if face is not card else None
    if face_effects is not None:
        effects = {str(e).lower() for e in face_effects}
    else:
        effects = {str(e).lower() for e in (card.get("frame_effects") or [])}
    if len(card.get("card_faces") or []) > 1 and face is not card:
        if "legendary" in tl and "planeswalker" not in tl:
            effects.add("legendary")
        else:
            effects.discard("legendary")
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
        # rooms (Enchantment — Room) have a split layout but their own frame
        base = "room"
    elif "battle" in tl:
        # battles (Battle — Siege) are transform-layout cards; the type line
        # identifies them since layout:battle is not searchable
        base = "battle"
    elif any(t in tl for t in ("saga", "class", "case")):
        # sagas / class / case (shared saga frame), also from the type line so
        # e.g. a transform card's saga front gets the saga base
        base = "saga"
    elif layout == "split":
        sub = next((s for kw, s in SPLIT_KEYWORDS if kw in keywords), "regular")
        base = f"split_{sub}"
    elif layout in LAYOUT_BASES:
        base = LAYOUT_BASES[layout]
    elif layout == "meld" and re.search(r"[a-z]$", str(card.get("collector_number") or "")):
        # the back of a meld card (letter-suffixed collector number, e.g.
        # emn/15b) is its own layout; the front is a normal card
        base = "meld"
    elif layout in MULTI_FACE_LAYOUTS:
        base = "default"
    elif layout not in LAYOUT_SAME_AS_NORMAL and layout != "normal":
        print(f"warning: unsupported layout: {layout}, using default.")
        base = "default"
    else:
        # normal and same-as-normal layouts share the frame-era border
        base = "default"
    if layout == "modal_dfc" and base != "modal":
        # modal is not exclusive with the other bases: a modal_dfc face that
        # is a planeswalker / battle / ... carries the modal frame on top
        base = f"modal_{base}"
    if base in ("saga", "modal_saga") and ("creature" in tl or "vehicle" in tl):
        # a face that is both a creature and a saga (Enchantment Creature —
        # Saga, whatever the card layout) keeps the creature frame; the saga
        # inner frame is handled at apply time (mask intersection)
        base = "default" if base == "saga" else "modal"
    # a back side (second face of transform/modal_dfc, or a meld back) never
    # has a holostamp and never the DFC mark
    faces = card.get("card_faces") or []
    is_back = base == "meld" or (bool(faces) and face is not faces[0])
    if is_back and base.endswith("planeswalker"):
        # a planeswalker back side has a frame of its own (modal_..._back for
        # a modal_dfc face)
        base = base + "_back"
    # the back of a transform card that is a planeswalker on both sides has a
    # unique frame no other card has
    pw_back = (is_back and layout == "transform" and len(faces) > 1
               and all("planeswalker" in (f.get("type_line") or "").lower()
                       for f in faces))
    crowned = has_crown(card, face)
    # vehicles (Artifact — Vehicle) count as creatures: power/toughness box
    if "planeswalker" in tl:
        typecat = "planeswalker"
    elif "creature" in tl or "vehicle" in tl:
        typecat = "creature"
    else:
        typecat = "other"
    if crowned and typecat != "planeswalker":
        variant = f"legendary_{typecat}_crowned"
    elif base in ("default", "modal") and era == "2015":
        # only the 2015 frame differentiates creature / non-creature
        # default/modal frames; the other frames share one border ring
        variant = typecat
    else:
        variant = "default"
    # frame-effect buckets ("legendary" is carried by the crowned variant,
    # "spree" by the base) plus the holostamp, as "+" extras of the key
    buckets = sorted({b for b in (effect_bucket(e) for e in effects) if b})
    if is_back:
        buckets = [b for b in buckets if b not in ("dfc", "fandfc")]
    elif not {"dfc", "fandfc"} & set(buckets) and (
            (layout == "transform" and base != "battle")
            or layout == "meld"
            or (layout == "modal_dfc"
                and (card.get("released_at") or "") >= SPIDERMAN_DATE)):
        # transform/meld fronts always carry the DFC mark (battles excepted),
        # and modal_dfc fronts from SPIDERMAN_DATE on - assumed when Scryfall
        # does not report it
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
    return RefSpec(key, base, layout, era, buckets, typecat,
                   crowned, stamp, is_back, pw_back)


def ref_search_query(base, layout, era, buckets, typecat,
                     crowned=False, stamp=None, pw_back=False, back=False,
                     released_at=""):
    """Scryfall query for a white card with the same frame characteristics.

    layout: / frame: / kw: / t: / stamp: qualifiers, and every characteristic
    the asked card does NOT have is negated as well (-frame:..., -t:..., ...),
    so only exact-frame matches come back. pw_back queries the unique back
    frame of both-sides-planeswalker transform cards; a composite modal_<base>
    queries the <base> plus layout:modal_dfc. A dfc bucket assumed from the
    release date (SPIDERMAN_DATE) is not queried - candidates are key-matched
    client-side. lea/leb are always excluded (Alpha/Beta scans are off)."""
    want = set(buckets or ()) - {"legendary", "spree"}
    # a composite modal_X base (modal is not exclusive with the other bases):
    # query the X base and require the modal layout on top of it
    modal = base.startswith("modal_")
    sub_base = base.split("_", 1)[1] if modal else base
    if sub_base.endswith("_back"):
        # a planeswalker back side (planeswalker_back / modal_planeswalker_back):
        # queried like a planeswalker front, the back= flag selects the backs
        sub_base = sub_base[:-len("_back")]
    if (layout == "modal_dfc" and "dfc" in want
            and (released_at or "") >= SPIDERMAN_DATE):
        # the dfc mark of a Spider-Man-era modal_dfc front is not a reported
        # frame effect: query without the dfc bucket, the client-side key
        # comparison adds it back
        want = want - {"dfc"}
    if pw_back:
        # only another both-sides-planeswalker transform card has this frame
        # (on its back side)
        parts = ["layout:transform", "t:planeswalker", "t:legendary",
                 f"frame:{era}"]
    elif sub_base.startswith("split_"):
        parts = ["layout:split", f"frame:{era}"]
        kw = sub_base.split("_", 1)[1]
        if kw != "regular":
            parts.append(f"kw:{kw}")
    elif sub_base == "planeswalker":
        # layout:planeswalker matches nothing on Scryfall, t:planeswalker does
        parts = ["t:planeswalker", f"frame:{era}"]
    elif sub_base == "spree":
        parts = ["kw:spree", f"frame:{era}"]
    elif sub_base == "room":
        parts = ["t:room", f"frame:{era}"]
    elif sub_base == "battle":
        parts = ["t:battle", f"frame:{era}"]
    elif sub_base in ("saga", "modal", "leveler", "meld"):
        # class/case share the saga frame, so query sagas for them
        lay = "saga" if sub_base == "saga" else layout
        parts = [f"layout:{lay}", f"frame:{era}"]
    else:
        # normal and same-as-normal layouts: the frame era alone
        parts = [f"frame:{era}"]
    if modal:
        parts.append("layout:modal_dfc")
    parts.append("-set:lea")
    parts.append("-set:leb")           # Alpha and Beta are never chosen
    parts.append("c=w")                # Magic color white: border very distinct
    # the reference must carry the same stamp: stamp:<value> directly
    # (aliased via STAMP_ALIASES), or every value negated for no stamp
    if stamp:
        s = str(stamp).lower()
        parts.append("stamp:" + STAMP_ALIASES.get(s, s))
    else:
        parts.extend(f"-stamp:{s}" for s in sorted(STAMP_VALUES))
    if crowned:
        # t:legendary, not is:legendary - the latter matches non-legendary
        parts.append("t:legendary")
        parts.append("frame:legendary")
    elif not pw_back:
        # no crown on the asked card: exclude crowned legendaries
        parts.append("-frame:legendary")
    if not back and (crowned or (sub_base in ("default", "modal") and era == "2015")):
        # creature / non-creature frames differ only in the 2015 default/modal
        # frames or when crowned. Skipped for back sides: card-level t: matches
        # any face, the client-side key check on the back face filters instead
        if typecat == "creature":
            parts.append("t:creature")
        elif typecat == "other":
            parts.extend(["-t:creature", "-t:planeswalker"])
    # one term per wanted frame-effect bucket (dfc = any DFC mark), negations
    # for the known absent ones
    for b in sorted(want):
        parts.append(BUCKET_QUERY.get(b, f"frame:{b}"))
    if not pw_back and base != "battle" and not back:
        # skipped for back sides: a back never carries a DFC mark either, so
        # transform-layout candidates are allowed (their back face is what
        # gets compared client-side)
        if "dfc" not in want:
            parts.extend(f"-frame:{e}" for e in sorted(DFC_EFFECTS))
        if "fandfc" not in want:
            parts.append("-frame:fandfc")
        if "dfc" not in want and "fandfc" not in want:
            parts.append("-layout:transform")
    if not modal and base != "modal":
        parts.append("-layout:modal_dfc")
    if sub_base != "spree":
        parts.append("-kw:spree")
    # a spacecraft is never a reference: its frame differs inside the border
    # ring (it gets the plain mask, intersected with its autodetected one)
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
    """Reference key of a search-result card, on its back face when back=True
    and it has one, else its front. A back-face key carries no stamp/dfc
    extras (ref_key drops them), so a plain card can match a back face; a
    single-faced candidate keeps its full key, so a stamped card cannot match
    an unstamped back."""
    faces = c.get("card_faces") or []
    face = faces[-1] if (back and len(faces) > 1) else (faces[0] if faces else c)
    return ref_key(c, face).key


def reference_png(ref, back=False):
    """PNG URI of the reference card: its back face for a back-side mask (only
    multi-faced references carry one), else the usual first image."""
    faces = ref.get("card_faces") or []
    if back and len(faces) > 1:
        png = (faces[-1].get("image_uris") or {}).get("png")
        if png:
            return png
    return card_png(ref)


def find_white_reference(query, key=None, back=False):
    """The one useful white printing for the query, or None (first page only).
    With key, a candidate must have exactly the same reference key in both
    directions (every characteristic the asked card has is required, every
    one it lacks excludes it); with back=True its back face is compared.
    Prefers the blandest, oldest mono-white (colors == ["W"]) candidate, and
    falls back to the first candidate with white in a face and no black in any
    (a black face means a black frame, where the border is not distinct).
    Non-foil preferred; Alpha/Beta never appear (excluded by the query)."""
    results = search_cards(query, paginate=False)
    # white in the card's colors or in one of its faces, and a png somewhere
    # (two-faced cards can have a null card-level colors and no card-level
    # image_uris while a face is white and has the image, e.g. battles)
    cands = [c for c in results
             if card_png(c)
             and ("W" in (c.get("colors") or [])
                  or any("W" in (f.get("colors") or [])
                         for f in (c.get("card_faces") or [])))]
    cands = prefer_nonfoil(cands)
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


def reference_mask(key, card, face, args):
    """Border mask for the key: from the cache (ref_masks_clean/ first, then
    ref_masks/) or built from a white reference card and saved to ref_masks/.
    Returns (mask float HxW or None, source description)."""
    path = cached_mask_path(key)
    if path is not None:
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
    path = REF_DIR / f"{key}.png"
    REF_DIR.mkdir(parents=True, exist_ok=True)
    Image.fromarray((mask * 255).round().astype(np.uint8), "L").save(path)
    print(f"  saved new mask to {path}")
    src = (f"reference '{ref.get('name')}' ({ref.get('set')}/{ref.get('collector_number')}, "
           f"border {border_hex}) -> {path}")
    return mask, src


# ------------------------------------------------------------------ pipeline

def apply_mask(img, mask, border=None):
    """Negate the card inside the mask. The binary mask is resized to the card
    (NEAREST) and the inner-edge blend band is derived from it
    (float_mask_from_region): mask 1 -> pixel inverted (255 - pixel), 0 ->
    untouched; band pixels (0 < mask < 1) blend the NEW border color
    (255 - border, so light) in with the mask gray as weight:
        new = new_border * gray + old * (1 - gray)
    Returns (image, weight, frac, inner_edge)."""
    arr = np.asarray(img.convert("RGBA")).copy()
    cmask = card_mask(img)
    h, w = cmask.shape
    mask = resize_mask(mask, h, w)
    # the blend band is not stored in the mask: derive it here from the
    # binary region, so saved masks stay fully white
    mask = float_mask_from_region(mask > 0, cmask)
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
    frac = 100.0 * weight.sum() / max(1, cmask.sum())
    return out, weight.astype(np.float32), frac, inner


def process_image(img, args, card=None, face=None):
    """Negate the border of one card image. Returns (output image, stats).

    With Scryfall metadata (card/face) the mask comes from the cached white
    reference for the card's characteristics; otherwise (or when no reference
    exists) it is detected on the image itself - and, when a key is known,
    cached under that key. Spacecraft / planeswalker / saga faces (creature-
    sagas included, though they use the creature mask) have a frame that
    differs inside the border ring: their mask is the reference mask
    intersected with the one autodetected on the card itself."""
    region, border, border_hex = detect_region(
        img, threshold=args.threshold, max_hole=args.max_hole)
    stats = {"found": False, "frac": 0.0, "border": border_hex, "mask": "none"}

    mask = None
    src = None
    key = None
    intersect_why = None   # set when the frame differs inside the border ring
    if card is not None:
        layout = (card.get("layout") or "").lower()
        if layout in LAYOUT_UNSUPPORTED:
            print(f"  warning: layout '{layout}' is not supported "
                  f"- leaving the card unchanged")
            return img.convert("RGBA"), stats
        f = face or card
        tl = (f.get("type_line") or card.get("type_line") or "").lower()
        spec = ref_key(card, f)
        key = spec.key
        if "spacecraft" in tl:
            intersect_why = "spacecraft"
        elif "planeswalker" in tl:
            intersect_why = "planeswalker"
        elif spec.base == "saga" or any(t in tl for t in ("saga", "class", "case")):
            # sagas (and class / case, which share the saga frame) have the
            # chapter track inside the border ring - also on a creature-saga
            # face, which gets the creature mask (see ref_key) but still has
            # the saga inner frame
            intersect_why = "saga"
        mask, src = reference_mask(key, card, f, args)
        if mask is None:
            print(f"  warning: no white reference card found for '{key}' "
                  f"- detecting the border on the card itself")
        else:
            # the mask comes from another card's scan: nudge it to fit this one
            cmask = card_mask(img)
            mask = resize_mask(mask, cmask.shape[0], cmask.shape[1])
            if intersect_why and region is not None and region.any():
                # intersect the reference mask with the autodetected one
                inter = (mask > 0) & region
                if inter.any():
                    mask = inter.astype(np.float32)
                    src = f"{src} (intersected with the autodetected mask: {intersect_why})"
                else:
                    print("  warning: empty intersection with the autodetected "
                          "mask - using the autodetected mask alone")
                    mask = region.astype(np.float32)
                    src = f"{src} (autodetected only: {intersect_why})"
            # masks stay binary here; the blend band is derived at apply time
            mask, info = align_mask(mask, img, border)
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
        if (key is not None and intersect_why != "spacecraft"
                and not (intersect_why == "saga" and spec.base != "saga")):
            # not cached under the plain key when the frame differs inside the
            # border ring but the key is not specific to it: spacecraft always,
            # and creature-sagas (plain creature key, but the mask carries the
            # chapter track). Saga / planeswalker keys are frame-specific, so
            # their self-detected masks can be cached
            path = REF_DIR / f"{key}.png"
            print(f"  generating new mask '{key}' from the card itself "
                  f"'{card.get('name')}' (no white reference available)")
            REF_DIR.mkdir(parents=True, exist_ok=True)
            Image.fromarray((mask * 255).round().astype(np.uint8), "L").save(path)
            print(f"  saved new mask to {path}")
            src = f"self-detected on '{card.get('name')}' -> {path}"

    out, _weight, frac, inner = apply_mask(img, mask, border=border)
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


def negate_card_faces(card, args):
    """Negate every face of a Scryfall card; returns [(face, image, stats)].
    Each transformed face is saved to card_cache/ and reloaded from there on
    later runs instead of being downloaded and negated again."""
    results = []
    for i, face in enumerate(card_faces(card)):
        uris = face.get("image_uris") or card.get("image_uris") or {}
        png = uris.get("png")
        if not png:
            print(f"  face {i + 1} ({face.get('name', '')}): no png, skipped")
            continue
        cached = load_cached_face(png, args)
        if cached is not None:
            out, stats = cached
            print(f"  face {i + 1} ({face.get('name', '')}): loaded from card cache")
        else:
            out, stats = process_image(Image.open(io.BytesIO(http_get(png))),
                                       args, card=card, face=face)
            save_cached_face(png, args, out, stats)
        results.append((face, out, stats))
    return results


def process_card(card, args):
    """Process a whole Scryfall card into [(label, image, stats)]: the negated
    faces (both for transform / modal_dfc), or - when the oldest printing is
    Alpha - the Unlimited printing as-is with stats None (2ed already has a
    white border). Used by process_url and by deck_to_pdf.py."""
    unl = alpha_unlimited_printing(card)
    if unl is not None:
        png = card_png(unl)
        if png:
            print(f"  oldest printing of '{card.get('name')}' is Alpha (lea) - using the "
                  f"Unlimited printing ({unl.get('set_name')} #{unl.get('collector_number')}) "
                  f"as-is, not negated")
            cached = load_cached_face(png, args)
            if cached is not None:
                print("  image loaded from card cache")
                return [(unl.get("name") or card.get("name") or "card", *cached)]
            img = Image.open(io.BytesIO(http_get(png))).convert("RGBA")
            save_cached_face(png, args, img, None)
            return [(unl.get("name") or card.get("name") or "card", img, None)]
    return [(face.get("name") or card.get("name") or "card", out, stats)
            for face, out, stats in negate_card_faces(card, args)]


def process_url(link, args):
    card = get_card(link)
    results = process_card(card, args)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", card.get("name", "card")).strip("_") or "card"
    multi = len(results) > 1
    for i, (label, out, stats) in enumerate(results):
        if args.output:
            p = Path(args.output)
            dst = p if not multi else p.with_name(f"{p.stem}_face{i + 1}{p.suffix or '.png'}")
        else:
            dst = Path(f"{name}_negated.png" if not multi
                        else f"{name}_face{i + 1}_negated.png")
        out.save(dst)
        if stats is None:
            print(f"  -> {dst}  (Unlimited printing, not processed: "
                  f"{out.width}x{out.height})")
        else:
            report(f"  face {i + 1} ({label})", dst, out, stats)


def add_detection_args(p):
    """--threshold / --max-hole flags, shared by both CLIs (negate_border.py
    and deck_to_pdf.py)."""
    p.add_argument("--threshold", type=int, default=32,
                   help="max per-channel distance from the detected border color for a "
                        "pixel to count as border when detecting it on a card "
                        "(default: 32)")
    p.add_argument("--max-hole", type=int, default=20000,
                   help="largest enclosed pocket inside the border that still becomes part "
                        "of the mask - covers the copyright / rarity / set info text and "
                        "the holostamp in the bottom band but not the card interior "
                        "(default: 20000)")


def main():
    p = argparse.ArgumentParser(
        prog="negate-border",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="card image file, a directory of images, or a Scryfall card link")
    p.add_argument("-o", "--output",
                   help="output file (file/URL input) or directory (dir input); "
                        "default: <name>_negated.<ext> / <dir>-negated/ / <card>_negated.png")
    add_detection_args(p)
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
