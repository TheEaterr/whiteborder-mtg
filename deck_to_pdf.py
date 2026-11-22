#!/usr/bin/env python3
"""deck_to_pdf.py - print-ready PDF of white-bordered cards from a deck list.

Takes a deck list in the Magic Arena export format:

    Deck
    4 Lightning Bolt
    1 Valki, God of Lies // Tibalt, Cosmic Impostor

    Sideboard
    2 Negate

Each card name is resolved on Scryfall (paper, black border, non-promo,
oldest printing - the same resolution negate_border.py uses for a link) and
negated with negate_border.py's pipeline, so every card in the PDF is the
white-border version. Cards whose oldest printing is Alpha are taken from
Unlimited instead (2ed already has a white border) and used as-is.
Double-faced cards (transform / modal_dfc) are duplicated: every copy prints
its front AND its back, so the pair can be sleeved together.

Scale: a real Magic card is 63 x 88 mm. The PDF places every image at exactly
that size - in PDF points (1 pt = 1/72 inch, so 63 mm = 178.5827 pt) via
reportlab's drawImage(width=..., height=...), never relying on the image's
DPI metadata - in a centered 3x3 grid on each page, with a small gap between
cards and thin cut guides. Print at 100% scale ("actual size", NOT "fit to
page") and each card measures 63 x 88 mm.

Usage:
    deck_to_pdf.py DECKLIST [-o out.pdf] [--page a4|letter] [--gap MM]
    deck_to_pdf.py -                        # read the deck list from stdin

Requires pillow + numpy + reportlab (provided by the flake devShell:
nix develop --command python3 deck_to_pdf.py deck.txt -o deck.pdf)
"""

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas

import negate_border as nb

# a real Magic card is 63 x 88 mm; every image is placed at exactly this size
# in points (1 pt = 1/72 inch), regardless of the source image's resolution
CARD_W, CARD_H = 63.0 * mm, 88.0 * mm
COLS, ROWS = 3, 3                       # 9 cards per page
PAGE_SIZES = {"a4": A4, "letter": letter}

SECTION_RE = re.compile(r"^(deck|sideboard|commander|companion)$", re.IGNORECASE)
CARD_RE = re.compile(r"^(\d+)\s+(.+?)\s*$")


def parse_decklist(text):
    """Parse an Arena export into (main, sideboard): ordered (name, qty)
    lists. Section headers (Deck / Sideboard / Commander / Companion) switch
    the target list - everything but the sideboard counts as main deck.
    Unrecognized non-empty lines are returned as problems."""
    main, sideboard = [], []
    target = main
    problems = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = SECTION_RE.match(line)
        if m:
            target = sideboard if m.group(1).lower() == "sideboard" else main
            continue
        m = CARD_RE.match(line)
        if m and int(m.group(1)) >= 1:
            target.append((m.group(2), int(m.group(1))))
        else:
            problems.append(line)
    return main, sideboard, problems


def build_entries(deck, args):
    """Expand the deck list into [(label, image)] print entries: qty copies
    of each card, and for double-faced cards BOTH faces per copy (the card is
    duplicated). Card resolution and negation live in negate_border.py
    (card_by_name / process_card). Returns (entries, failed names)."""
    entries = []
    failures = []
    ns = SimpleNamespace(threshold=args.threshold, max_hole=args.max_hole)
    for name, qty in deck:
        card = nb.card_by_name(name)
        if card is None:
            print(f"  warning: no Scryfall printing found for '{name}' - skipped")
            failures.append(name)
            continue
        print(f"=== {qty}x {name}  ->  {card.get('set')}/{card.get('collector_number')} "
              f"({card.get('name')}, layout {card.get('layout')})")
        imgs = nb.process_card(card, ns)
        if not imgs:
            print(f"  warning: no image available for '{name}' - skipped")
            failures.append(name)
            continue
        for label, _img, stats in imgs:
            if stats is not None:
                print(f"  face '{label}': border color {stats.get('border')}, "
                      f"{stats['frac']:.1f}% of card negated, mask: {stats.get('mask')}")
        for _ in range(qty):
            entries.extend((label, img) for label, img, _stats in imgs)
    return entries, failures


def flatten(img):
    """RGBA -> RGB on white. The negated border is white, so the rounded
    corners flatten to white too (invisible on white paper); JPEG keeps the
    PDF small - the exact physical size is set at draw time, not via DPI."""
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    return img.convert("RGB")


def build_pdf(entries, out_path, page_name, gap_mm, guides):
    """Write the print-ready PDF: 3x3 grid of exactly 63x88 mm cards per page,
    centered, with `gap_mm` between cards and optional cut guides. Returns
    the page count."""
    pw, ph = PAGE_SIZES[page_name]
    gap = gap_mm * mm
    grid_w = COLS * CARD_W + (COLS - 1) * gap
    grid_h = ROWS * CARD_H + (ROWS - 1) * gap
    if grid_w > pw or grid_h > ph:
        raise SystemExit(f"error: a {COLS}x{ROWS} grid of 63x88 mm cards with a "
                         f"{gap_mm} mm gap does not fit a {page_name} page")
    ox, oy = (pw - grid_w) / 2.0, (ph - grid_h) / 2.0

    c = pdfcanvas.Canvas(str(out_path), pagesize=PAGE_SIZES[page_name])
    c.setTitle(Path(out_path).stem)
    tmp = Path(tempfile.mkdtemp(prefix="deck_to_pdf_"))
    try:
        jpgs = {}   # id(image) -> jpeg file: copies of one card share one image
        per_page = COLS * ROWS
        for idx, (_label, img) in enumerate(entries):
            if idx and idx % per_page == 0:
                c.showPage()
            col, row = (idx % per_page) % COLS, (idx % per_page) // COLS
            x = ox + col * (CARD_W + gap)
            y = oy + (ROWS - 1 - row) * (CARD_H + gap)   # top row first
            key = id(img)
            if key not in jpgs:
                p = tmp / f"card{len(jpgs):04d}.jpg"
                flatten(img).save(p, "JPEG", quality=92)
                jpgs[key] = p
            if guides:
                c.setStrokeColorRGB(0.8, 0.8, 0.8)
                c.setLineWidth(0.25)
                c.rect(x, y, CARD_W, CARD_H, stroke=1, fill=0)
            # exact physical size: width/height in points, image DPI ignored
            c.drawImage(str(jpgs[key]), x, y, width=CARD_W, height=CARD_H)
        c.save()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return (len(entries) + per_page - 1) // per_page


def main():
    p = argparse.ArgumentParser(
        prog="deck-to-pdf",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("decklist",
                   help="deck list file in the Magic Arena export format, or '-' for stdin")
    p.add_argument("-o", "--output",
                   help="output PDF (default: <decklist>.pdf, or deck.pdf for stdin)")
    p.add_argument("--page", choices=sorted(PAGE_SIZES), default="a4",
                   help="paper size of the PDF pages (default: a4)")
    p.add_argument("--gap", type=float, default=0.0,
                   help="gap between cards in mm (default: 1.0)")
    p.add_argument("--no-guides", action="store_true",
                   help="do not draw the light cut guides around each card")
    p.add_argument("--no-sideboard", action="store_true",
                   help="print only the main deck, skip the sideboard")
    p.add_argument("--threshold", type=int, default=32,
                   help="max per-channel distance from the detected border color for a "
                        "pixel to count as border when detecting it on a card "
                        "(default: 32)")
    p.add_argument("--max-hole", type=int, default=20000,
                   help="largest enclosed pocket inside the border that still becomes part "
                        "of the mask (default: 20000)")
    args = p.parse_args()

    if args.decklist == "-":
        text = sys.stdin.read()
        default_out = Path("deck.pdf")
    else:
        src = Path(args.decklist)
        if not src.is_file():
            raise SystemExit(f"error: no such file: {src}")
        text = src.read_text(encoding="utf-8")
        default_out = src.with_suffix(".pdf")
    out = Path(args.output) if args.output else default_out

    main_deck, sideboard, problems = parse_decklist(text)
    for line in problems:
        print(f"warning: unrecognized line skipped: {line!r}")
    deck = main_deck + ([] if args.no_sideboard else sideboard)
    if not deck:
        raise SystemExit("error: no cards found in the deck list")
    total = sum(q for _, q in deck)
    print(f"{len(deck)} unique card(s), {total} card(s) in the list"
          + ("" if args.no_sideboard else f" ({sum(q for _, q in sideboard)} in the sideboard)"))

    entries, failures = build_entries(deck, args)
    if not entries:
        raise SystemExit("error: no card could be resolved - nothing to print")
    pages = build_pdf(entries, out, args.page, args.gap, guides=not args.no_guides)
    print(f"done: {len(entries)} card image(s) on {pages} {args.page} page(s) "
          f"({COLS}x{ROWS} per page, each exactly 63x88 mm) -> {out}")
    print("print at 100% scale ('actual size'), not 'fit to page' - "
          "then every card measures 63x88 mm")
    if failures:
        print(f"warning: {len(failures)} card name(s) could not be resolved: "
              + ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
