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

Scale: a real Magic card is 63 x 88 mm. Every image is placed at exactly that
size in PDF points (1 pt = 1/72 inch, so 63 mm = 178.5827 pt) via reportlab's
drawImage(width=..., height=...), never relying on the image's DPI metadata -
in a centered 3x3 grid per page, with an optional gap and thin corner cut
marks: at every corner of every card a 4 mm cross (2 mm on each side), with
each arm extended to the paper edge when no card blocks that direction.
Print at 100% scale ("actual size", NOT "fit to page") and each card measures
63 x 88 mm.

With --backs, a card-back page (back.png next to this script) is inserted after
every front page, mirrored left-right: print double-sided with "flip on long
edge" (the default duplex mode for portrait pages) at 100% scale and each card
back lands exactly behind its front. Front and back pages alternate (page 1 =
fronts, page 2 = their backs, ...), so each page pair shares one sheet.
--x-offset MM shifts the back pages horizontally relative to the fronts to
compensate for imperfect double-sided printing.

Usage:
    deck_to_pdf.py DECKLIST [-o out.pdf] [--page a4|letter] [--gap MM] [--backs]
                                 [--x-offset MM]
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


def build_pdf(entries, out_path, page_name, gap_mm, guides, backs=False, back_path=None,
              x_offset_mm=0.0):
    """Write the print-ready PDF: 3x3 grid of exactly 63x88 mm cards per page,
    centered, with `gap_mm` between cards and optional cut guides. With
    `backs`, a card-back page is added after each front page, mirrored
    left-right so duplex "flip on long edge" puts each back exactly behind
    its front. `x_offset_mm` shifts content horizontally: with `backs`, only
    the back pages (to compensate imperfect double-sided printing); without
    `backs`, all pages. Returns (front_pages, total_pages)."""
    pw, ph = PAGE_SIZES[page_name]
    gap = gap_mm * mm
    x_off = x_offset_mm * mm
    grid_w = COLS * CARD_W + (COLS - 1) * gap
    grid_h = ROWS * CARD_H + (ROWS - 1) * gap
    if grid_w + abs(x_off) > pw or grid_h > ph:
        raise SystemExit(f"error: a {COLS}x{ROWS} grid of 63x88 mm cards with a "
                         f"{gap_mm} mm gap and a {x_offset_mm} mm x-offset does not "
                         f"fit a {page_name} page")
    ox, oy = (pw - grid_w) / 2.0, (ph - grid_h) / 2.0
    per_page = COLS * ROWS

    def slot_xy(col, row, dx=0.0):   # top row first
        return ox + col * (CARD_W + gap) + dx, oy + (ROWS - 1 - row) * (CARD_H + gap)

    c = pdfcanvas.Canvas(str(out_path), pagesize=PAGE_SIZES[page_name])
    c.setTitle(Path(out_path).stem)
    tmp = Path(tempfile.mkdtemp(prefix="deck_to_pdf_"))
    try:
        jpgs = {}   # (tag, id(image)) -> jpeg file: copies of one card share one image

        def jpeg_of(img, tag):
            key = (tag, id(img))
            if key not in jpgs:
                p = tmp / f"{tag}{len(jpgs):04d}.jpg"
                flatten(img).save(p, "JPEG", quality=92)
                jpgs[key] = p
            return jpgs[key]

        def draw_card(jpg, col, row, dx=0.0):
            x, y = slot_xy(col, row, dx)
            # exact physical size: width/height in points, image DPI ignored
            c.drawImage(str(jpg), x, y, width=CARD_W, height=CARD_H)

        def draw_guides(slots, dx=0.0):
            # corner cut marks only: at every corner of every occupied card a
            # cross whose arms are 4 mm long (2 mm on each side of the corner).
            # An arm extends to the paper edge when no card blocks that
            # direction; if a card further away blocks, the arm ends at that
            # card's edge. Drawn after the images so the marks stay visible
            # over the white borders (gap=0).
            c.setStrokeColorRGB(0.5, 0.5, 0.5)
            c.setLineWidth(0.25)
            occupied = set(slots)
            arm = 2.0 * mm
            sx, sy = CARD_W + gap, CARD_H + gap
            corners = set()
            for col, row in occupied:
                x, y = slot_xy(col, row, dx)
                corners.update(((x, y), (x + CARD_W, y),
                                (x, y + CARD_H), (x + CARD_W, y + CARD_H)))
            for cx, cy in corners:
                k = round((cx - ox - dx) / sx)      # vertical grid line, 0..COLS
                m = round((cy - oy) / sy)           # horizontal grid line, 0..ROWS
                rows_adj = [r for r in (ROWS - 1 - m, ROWS - m) if 0 <= r < ROWS]
                cols_adj = [c for c in (k - 1, k) if 0 <= c < COLS]

                def h_end(direction):   # -1 = left, +1 = right
                    start = k - 1 if direction < 0 else k
                    for c in range(start, -1 if direction < 0 else COLS, direction):
                        if 0 <= c < COLS and any((c, r) in occupied for r in rows_adj):
                            if c == start:
                                return cx + direction * arm   # 2 mm onto the adjacent card
                            # stop at the blocking card's near edge
                            return ox + (c + 1) * sx + dx if direction < 0 else ox + c * sx + dx
                    return 0.0 if direction < 0 else pw           # paper edge

                def v_end(direction):   # -1 = down, +1 = up
                    start = ROWS - 1 - m if direction > 0 else ROWS - m
                    # row index decreases as y increases, hence -direction
                    for r in range(start, -1 if direction > 0 else ROWS, -direction):
                        if 0 <= r < ROWS and any((c, r) in occupied for c in cols_adj):
                            if r == start:
                                return cy + direction * arm   # 2 mm onto the adjacent card
                            # stop at the blocking card's near edge
                            if direction < 0:
                                return oy + (ROWS - 1 - r) * sy + CARD_H
                            return oy + (ROWS - 1 - r) * sy
                    return 0.0 if direction < 0 else ph           # paper edge

                c.line(h_end(-1), cy, h_end(1), cy)
                c.line(cx, v_end(-1), cx, v_end(1))

        back_img = Image.open(back_path) if backs else None
        back_jpg = jpeg_of(back_img, "back") if backs else None
        # with backs, the x-offset compensates duplex misalignment: it shifts the
        # back pages relative to the front pages; without backs, everything shifts
        front_dx = 0.0 if backs else x_off
        back_dx = x_off if backs else 0.0
        front_pages = (len(entries) + per_page - 1) // per_page
        for p_idx in range(front_pages):
            if p_idx:
                c.showPage()
            slots = []
            for idx in range(p_idx * per_page, min((p_idx + 1) * per_page, len(entries))):
                col, row = (idx % per_page) % COLS, (idx % per_page) // COLS
                slots.append((col, row))
                draw_card(jpeg_of(entries[idx][1], "card"), col, row, front_dx)
            if guides:
                draw_guides(slots, front_dx)
            if backs:
                # duplex "flip on long edge" mirrors the sheet left-right, so the
                # back of the front card at (col, row) goes to (COLS-1-col, row)
                c.showPage()
                back_slots = [(COLS - 1 - col, row) for col, row in slots]
                for col, row in back_slots:
                    draw_card(back_jpg, col, row, back_dx)
                if guides:
                    draw_guides(back_slots, back_dx)
        c.save()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return front_pages, front_pages * (2 if backs else 1)


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
                   help="gap between cards in mm (default: 0.0)")
    p.add_argument("--no-guides", action="store_true",
                   help="do not draw the corner cut marks on the cards")
    p.add_argument("--no-sideboard", action="store_true",
                   help="print only the main deck, skip the sideboard")
    p.add_argument("--backs", action="store_true",
                   help="insert a card-back page (back.png next to this script) after "
                        "each front page, mirrored left-right for double-sided printing "
                        "with 'flip on long edge' - each back lands exactly behind its front")
    p.add_argument("--x-offset", type=float, default=0.0, metavar="MM",
                   help="horizontal offset in mm: with --backs, shifts only the card-back "
                        "pages relative to the fronts to compensate for imperfect "
                        "double-sided printing (positive = right); without --backs, "
                        "shifts all pages (default: 0.0)")
    nb.add_detection_args(p)
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

    back_path = None
    if args.backs:
        back_path = Path(__file__).resolve().parent / "back.png"
        if not back_path.is_file():
            raise SystemExit(f"error: --backs needs back.png next to the script "
                             f"(not found: {back_path})")

    entries, failures = build_entries(deck, args)
    if not entries:
        raise SystemExit("error: no card could be resolved - nothing to print")
    front_pages, _ = build_pdf(entries, out, args.page, args.gap,
                               guides=not args.no_guides, backs=args.backs,
                               back_path=back_path, x_offset_mm=args.x_offset)
    print(f"done: {len(entries)} card image(s) on {front_pages} {args.page} front page(s)"
          + (f" plus {front_pages} card-back page(s)" if args.backs else "")
          + f" ({COLS}x{ROWS} per page, each exactly 63x88 mm) -> {out}")
    print("print at 100% scale ('actual size'), not 'fit to page' - "
          "then every card measures 63x88 mm")
    if args.backs:
        print("print double-sided with 'flip on long edge': front and back pages "
              "alternate (page 1 fronts / page 2 their backs), so each card back "
              "lands exactly behind its front")
    if failures:
        print(f"warning: {len(failures)} card name(s) could not be resolved: "
              + ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
