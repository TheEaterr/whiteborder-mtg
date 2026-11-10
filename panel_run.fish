# Test panel runner - writes the images into ./panel in this directory
# Usage: fish panel_run.fish
cd /home/eaterr/Code/magic_frame
set S /home/eaterr/Code/magic_frame/panel
mkdir -p $S

# entries: name|scryfall-url|extra white-border args (single token, e.g. --face=0)
# Adventures are not double-faced cards: Scryfall still lists two card_faces,
# but the card object holds the single image, so process face 0 only and keep
# a plain <name>.png output (no _faceN suffix).
set cards \
  "2015|https://scryfall.com/card/msc/806/lightning-bolt" \
  "2015_legendary|https://scryfall.com/card/tdc/281/atarka-world-render" \
  "1993|https://scryfall.com/card/lea/232/black-lotus" \
  "1993_legendary|https://scryfall.com/card/leg/216/adun-oakenshield" \
  "1997|https://scryfall.com/card/ody/312/abandoned-outpost" \
  "1997_legendary|https://scryfall.com/card/ody/58/aboshan-cephalid-emperor" \
  "2003|https://scryfall.com/card/unh/23/_____" \
  "2003_legendary|https://scryfall.com/card/mma/219/academy-ruins" \
  "future|https://scryfall.com/card/mb2/216/aether-vial" \
  "planeswalker_legendary|https://scryfall.com/card/2xm/56/jace-the-mind-sculptor" \
  "saga|https://scryfall.com/card/msc/41/age-of-ultron" \
  "saga_legendary|https://scryfall.com/card/ph18/4/the-legend-of-arena" \
  "modal_dfc|https://scryfall.com/card/znr/180/bala-ged-recovery-bala-ged-sanctuary" \
  "transform_legendary|https://scryfall.com/card/tla/203/aang-at-the-crossroads-aang-destined-savior" \
  "battle|https://scryfall.com/card/mom/230/invasion-of-alara-awaken-the-maelstrom" \
  "adventure_legendary|https://scryfall.com/card/mdd/106/hela-mistress-of-darkness-dark-tutor|--face=0"

for entry in $cards
    set parts (string split "|" $entry)
    set name $parts[1]
    set url $parts[2]
    set extra $parts[3]
    echo "=== $name"
    nix run .#default -- "$url" -o $S/$name.png $extra 2>&1 | tail -3
    sleep 0.2
end
