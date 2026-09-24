# Aveling & Porter No. 5499 - colour edition 5 (pen lines)

**1904 R6 · Works No. 5499 · BS 8711 · A3 landscape**

[Open the drawing](index.html) · [A3 PDF](artwork/aveling-porter-5499-colour-hatched-preview.pdf) ·
[Plot SVG](artwork/aveling-porter-5499-colour-hatched.svg) · [Compare editions](evidence/comparison.html) ·
[Photographs and palette](evidence/reference-review.html) · [Plot simulator](plot/aveling-porter-5499-colour-hatched-viewer.html)

![Colour pen drawing of Aveling and Porter No. 5499 with strong black outlines](artwork/aveling-porter-5499-colour-hatched.png)

This edition colours in the latest refined **revision-14 drawing** like a
coloured engineering drawing. Every outline is black and every coloured mark
is a pen line.

- **Outlines:** the engine's linework is drawn one pen heavier than the
  blueprint, so it stays strong beside the colour.
- **Black iron:** the wheels, hubs and flywheel are close, even Black circles.
- **Through the rear wheel:** everything up to the tender's straight front
  edge is a black hatch, a shade lighter than the wheel rims; the green tender
  carries on beyond it.
- **Red-brown:** the fork, scrapers, their mounts and the chain spring bar
  are red lines with a brown line in every gap. This darkens the bright red
  toward the engine's red-brown paint. Green tender surrounds the rear
  scraper.
- **Spokes:** every spoke is ruled with the same number of even lines: five on
  the rear wheel, four on the front roll.
- **Top-right badges:** the worksplate and Invicta horse have no colour, only
  their black engraving.
- **Gold:** the brass bands, valves, whistle and the cylinder's maker plate
  are each built from several 0.40 mm gold lines, 0.50 mm apart, so no broad
  gold pen is needed.
- **Iron without grey:** the chimney, smokebox, headstock, firebox and
  fittings are black-painted iron, hatched in black lines only. The line
  spacing sets the tone, so no grey pen is needed. The chimney and smokebox
  are shaded as cylinders: lines sparse in the light and close in shadow.
- **Other colour:** the boiler, cylinder, tender and spokes are green.

## The black drawing

All **863 revision-14 path shapes** are kept byte-for-byte. Engine linework is
one pen heavier than the blueprint: 0.30 → Black 0.40, 0.40 → Black 0.60, and
the roller tyres 0.50 → Black 1.00. These keep their original weights, so they
do not close up:

- fasteners, rivets, chains and threads;
- small fittings, scrapers and controls;
- lettering, both badges and the sheet frame.

The colour is **2,689 added pen lines**, across 45 parts and 283 enclosed paper
cells. Every coloured ink edge keeps **at least 0.22 mm of white paper** from
black ink, so inks never mix and small pen-change offsets stay hidden.

## Plot and print

Use **A3 landscape, 420 × 297 mm, at 100% / actual size**, on white paper.
The PNG is 4200 × 2970. The PDF is a one-page colour print version. The master
and pen SVGs contain only single-pass stroke paths: no fills, raster images or
live fonts.

Plot the pens in file order, light to dark, with each pen loaded once:

| Step | Actual studio pen | Paths | Use |
|---:|---|---:|---|
| 1 | Gold 0.40 mm | 43 | Boiler bands, valves, whistle, cylinder maker's plate |
| 2 | Green 0.25 mm | 852 | Green paintwork and spokes |
| 3 | Red 0.25 mm | 110 | Red of the fork, scrapers, scraper mounts and chain spring bar |
| 4 | Brown 0.25 mm | 87 | Brown lines between the red, darkening it to red-brown |
| 5 | Black 0.25 mm | 2,185 | 1,597 iron hatching and shade lines, then 588 fine details, lettering and badges |
| 6 | Black 0.40 mm | 208 | Engine linework |
| 7 | Black 0.60 mm | 63 | Principal engine outlines and sheet frame |
| 8 | Black 1.00 mm | 4 | Roller tyre silhouettes |

The optimised job estimates **48 minutes 31 seconds** (41:14 to 55:47) with
the nominal AxiDraw-class profile. It draws 3,552 strokes with 13.3 m of
pen-up travel; document order would take 55.3 minutes. No physical
machine was operated; pen and timing calibration remain the studio's usual
pre-plot steps.

Every line width is one of the studio's pens: Black 0.25/0.40/0.60/1.00,
Green, Red and Brown 0.25, and the 0.40 mm Gold. No grey pen and no broad gold
nib are used.
If the gold pen ever changes, set `GOLD_NIB_MM` in
`recipe/tools/engineering_source_plates/aveling_5499_colour_v5/inventory.py`
and rebuild. The gold line spacing and clearances follow from it.

- [gold-0-4](artwork/aveling-porter-5499-colour-hatched.pen-01-gold-0-4.svg)
- [green-0-25](artwork/aveling-porter-5499-colour-hatched.pen-02-green-0-25.svg)
- [red-0-25](artwork/aveling-porter-5499-colour-hatched.pen-03-red-0-25.svg)
- [brown-0-25](artwork/aveling-porter-5499-colour-hatched.pen-04-brown-0-25.svg)
- [black-0-25](artwork/aveling-porter-5499-colour-hatched.pen-05-black-0-25.svg)
- [black-0-4](artwork/aveling-porter-5499-colour-hatched.pen-06-black-0-4.svg)
- [black-0-6](artwork/aveling-porter-5499-colour-hatched.pen-07-black-0-6.svg)
- [black-1](artwork/aveling-porter-5499-colour-hatched.pen-08-black-1.svg)

## Verification and source scope

All **269 format checks** and strict SVG preflight pass. `verify.py` rereads
the exported SVG and independently checks that:

- all 863 outline shapes are unchanged and on the pen the documented weight
  mapping gives;
- the exported lines exactly match the fill plan;
- each line lies inside its own part;
- the measured paper gap to black ink is at least 0.2219 mm;
- no grey pen is used anywhere;
- every red part carries brown lines between its red lines;
- every gold part is at least two fine gold lines, all on the fine gold pen;
- no colour reaches the top-right badges;
- every spoke line is straight;
- the lines within each spoke are parallel to within 0.08°;
- the rim-to-spoke gaps meet the minimum;
- the pens have eight-nib cap heights and three-nib path lengths;
- all 12 lettering blocks are complete;
- all eight pen files together equal the master;
- the manifest and plot job agree.

[Fill plan](evidence/fill-plan.json) · [Verification](evidence/verification.json) ·
[Visual review](evidence/visual-review.json) · [Rebuild proof](evidence/rebuild-verification.json) ·
[Source ledger](evidence/sources.json)

Actual-engine photographs by Benjamin Matthews and Terry Pinnegar constrain
the engine and its colour groups. The MERL Fowler S1021 drawing informs
composition only. GW Railwayana Auctions supplies the Invicta casting and
separate No. 6882 worksplate references; the latter's lettering arrangement
is adapted to the user's No. 5499 wording. These references do not establish
surveyed No. 5499 dimensions or calibrated paint shades. Image credits and
scope remain in the source ledger.

## Portable rebuild

The package includes the frozen revision-14 SVG, reference evidence, the
colour builder and the exact shared source used to export and verify the
drawing. The recipe has no dependency on a separately updated project
checkout.

```bash
python3 -m venv .venv
.venv/bin/pip install -r recipe/requirements.txt
.venv/bin/python recipe/rebuild.py --output-dir /tmp/aveling-5499-colour-v5-rebuild
```

Python 3.13 and Inkscape 1.4.4 were used. The command recreates the SVG, pen
layers, PNG, plot job, simulator, verification record and A3 PDF. Add
`--skip-pdf` to rebuild plot files without the conventional-print export. An
independent rebuild reproduced the SVG, PNG, all pen files, plot job,
simulator and fill plan byte-for-byte.

After cloning the publishing repository, obtain the large-file objects with:

```bash
git lfs pull --include="artwork/aveling-porter-5499-colour-v5/**"
```

Run `sha256sum -c CHECKSUMS.sha256` inside this folder to verify the files.
The sibling ZIP contains the complete files, not Git LFS pointers.
