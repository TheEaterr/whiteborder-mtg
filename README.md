# magic_frame

Tools to turn black-bordered Magic: The Gathering cards into white-bordered
ones (proxies), using Scryfall scans. Everything is driven from Scryfall card
links or card names; no local card database is needed, only network access to
the Scryfall API.

## Tools

| Tool | What it does |
|---|---|
| `negate_border.py` (`negate-border`) | Negates the black border of a card: a Scryfall link, an image file, or a directory of images |
| `deck_to_pdf.py` (`deck-to-pdf`) | Turns a deck list (Magic Arena export format) into a print-ready PDF of white-bordered cards |
| `run_test_cards.sh` | Runs `negate_border.py` on every link in `test_cards.txt`, output in `test_cards_negated/` |
| `diff_masks.sh` | Diffs the file names of `ref_masks/` vs `ref_masks_clean/` |

## How the negation works

The border is never taken from the asked card directly (its black border is
hard to detect on a black card). Instead, for a given card the script finds a
**white reference card** on Scryfall with exactly the same frame
characteristics — frame era, layout, legendary crown, card type
(creature/other), frame effects (DFC marks, ...), holostamp — where the black
border is very distinct against the white frame. The border is detected on
that reference and stored as a grayscale **mask**, cached on disk:

- `ref_masks_clean/` — manually cleaned-up masks, looked up **first**
  (committed to git)
- `ref_masks/` — generated cache, new masks are written here (git-ignored)
- `card_cache/` — caches (git-ignored):
  - `<key>.png` + `<key>.json` — transformed card faces (key = card image URL +
    detection flags); a face already negated is reloaded instead of being
    downloaded and processed again
  - `name_<key>.json` — deck-list name → resolved printing (a repeat run of the
    same deck resolves every card with no Scryfall request)
  - `unl_<key>.json` — oracle id → Alpha→Unlimited decision

  HTTP requests are additionally paced ~100 ms apart so a full deck does not
  trip Scryfall's rate limit; 429 responses still pause and retry.

Mask files are named by a key describing the frame:

```
<era>_<base>[_<variant>][+<extras>]
```

e.g. `2015_default_creature+stamp`, `2015_saga+dfc+stamp`,
`2015_default_legendary_creature_crowned+stamp_triangle`, `2003_leveler`.
Era is the Scryfall frame (1993/1997/2003/2015/future); base is the layout
frame (default, planeswalker, saga/class/case, modal, split_*, battle, room,
leveler, spree, meld, leb); variant covers the legendary crown and, in the
2015 frame, creature vs non-creature; extras are frame-effect buckets, the
holostamp (`stamp`, `stamp_triangle`, ...) and `pw_back`. Each face of a
two-faced card gets the key of its own characteristics; a back side never
carries a stamp or DFC mark. Tokens and emblems (`token` /
`double_faced_token` / `emblem` layouts) use the default base of their frame
era - their border ring is the standard one.

When applying a mask, the card inside the border is inverted
(`255 - pixel`). The innermost few mask lines are not fully negated: there the
**new border color** (`255 -` the border color sampled from the card's own
bottom edge, so light) is blended in, each pixel weighted by its mask gray
value: `new = new_border * gray + old * (1 - gray)`. The alpha channel is
never touched, so rounded corners stay clean, and the copyright / rarity /
set info text and the holostamp in the bottom band are negated with the
border.

Because Scryfall scans of the same layout are not pixel-identical, a reference
mask is slightly adjusted (shift / grow / shrink by a few pixels) to fit the
asked card before being applied. Spacecraft, planeswalker and saga faces
(including creature-sagas) have a frame that differs *inside* the border ring,
so their mask is intersected with the one autodetected on the card itself.

When no white reference exists for a frame, the border is detected on the
asked card itself and the mask is cached under the same key (except for
spacecraft / creature-saga faces, whose self-detected mask is frame-specific
and not cached under the plain key).

Edge cases: cards whose oldest printing is Alpha (lea) are returned as the
Unlimited (2ed) printing as-is (2ed already has a white border); Alpha/Beta
and a few other off-scan sets (fbb/cei/ced/4bb) are never used; unsupported
layouts (scheme, vanguard, ...) are left unchanged. A 429
rate-limit response from Scryfall pauses for a few seconds before retrying.

## Setup

The nix flake provides everything (pillow, numpy, reportlab):

```sh
nix develop --command negate-border --help     # enter the dev shell
nix run .#default -- <scryfall-url>            # or run via nix directly
```

Without nix, any python3 with `pillow`, `numpy` (and `reportlab` for
`deck_to_pdf.py`) works.

## Usage

### negate_border.py

```sh
negate_border.py https://scryfall.com/card/msc/41/age-of-ultron -o out.png
negate_border.py card.jpg -o out.png        # local image: border detected on it
negate_border.py scans/ -o scans-negated/   # a whole directory
negate_border.py <url>                      # default output: <card>_negated.png
```

- `-o/--output`: output file (file/URL input) or directory (dir input).
- `--threshold` (default 32): max per-channel distance from the detected
  border color for a pixel to count as border when detecting it on a card.
- `--max-hole` (default 20000): largest enclosed pocket inside the border that
  still becomes part of the mask (covers the bottom-band text and holostamp).
- Transform / modal_dfc / double_faced_token cards produce one image per face
  (`<card>_face1_negated.png`, `<card>_face2_negated.png`, ...).
- `--related` (card links only): also negate the related tokens / emblems of
  the card (its Scryfall `all_parts`), saved next to the card's output as
  `<part name>_negated.png`.

### deck_to_pdf.py

```sh
deck_to_pdf.py decklist.txt -o deck.pdf
cat decklist.txt | deck_to_pdf.py -         # read the deck list from stdin
```

Deck list format (Magic Arena export):

```
Deck
4 Lightning Bolt
1 Valki, God of Lies // Tibalt, Cosmic Impostor

Sideboard
2 Negate
```

- Each name is resolved on Scryfall (paper, black border, non-promo, oldest
  printing) and negated like `negate_border.py` does.
- Double-faced cards (transform / modal_dfc / double_faced_token) are
  duplicated: every copy prints its front AND its back, so the pair can be
  sleeved together.
- `--related` also adds one copy of each card's related tokens / emblems (the
  card's Scryfall `all_parts`) to the PDF.
- `--page a4|letter` (default a4), `--gap MM` (default 0.0), `--no-guides`,
  `--no-sideboard`, plus the shared `--threshold` / `--max-hole`.
- `--backs` inserts a card-back page (`back.png` next to the script) after every
  front page, mirrored left-right. Front and back pages alternate (page 1 =
  fronts, page 2 = their backs, ...), so each page pair shares one sheet when
  printed double-sided with "flip on long edge" (the usual duplex default for
  portrait pages) at 100% scale - every card back then lands exactly behind its
  front.
- `--x-offset MM` / `--y-offset MM` shift the card-back pages horizontally /
  vertically relative to the front pages (positive = right / up) to compensate
  for imperfect double-sided printing; without `--backs` they shift all pages.

**Scale:** a real Magic card is 63 x 88 mm. Every image is placed at exactly
that size in the PDF (in points, 63 mm = 178.5827 pt), in a centered 3x3 grid
per page, regardless of the source image's resolution/DPI. Print at 100%
scale ("actual size", NOT "fit to page") and each card measures exactly
63 x 88 mm.

### Testing utilities

```sh
./run_test_cards.sh                 # negate every link in test_cards.txt
./diff_masks.sh                     # file-name diff of ref_masks/ vs ref_masks_clean/
./diff_masks.sh dirA dirB           # any two directories
```

## Adding a manual reference mask

Drop a grayscale PNG named `<key>.png` into `ref_masks_clean/` — fully white
(255) inside the border region, black outside; the inner-edge blend band is
derived automatically at apply time. It takes precedence over any generated
mask with the same key.
