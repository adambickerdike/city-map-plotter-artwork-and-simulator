# Aveling & Porter No. 5499 - colour edition 5 (pen lines)

**1904 R6 · Works No. 5499 · BS 8711 · A3 landscape**

[Open the drawing](index.html) · [A3 PDF](artwork/aveling-porter-5499-colour-hatched-preview.pdf) ·
[Plot SVG](artwork/aveling-porter-5499-colour-hatched.svg) · [Compare editions](evidence/comparison.html) ·
[Photographs and palette](evidence/reference-review.html) · [Plot simulator](plot/aveling-porter-5499-colour-hatched-viewer.html)

![Colour pen drawing of Aveling and Porter No. 5499 with strong black outlines](artwork/aveling-porter-5499-colour-hatched.png)

This edition colours in the latest refined **revision-14 drawing** like a
coloured engineering drawing. Every outline is black, apart from the thin gold
inner lines of the boiler bands, and every coloured mark is a pen line. It
needs only **five pens**: one of each ink, and just the fine 0.25 mm black.

- **Outlines:** the engine's linework is drawn one weight heavier than the
  blueprint, so it stays strong beside the colour. Every black line is the one
  Black 0.25 mm pen; the heavier lines are built from several 0.25 mm strokes
  laid side by side (see [The black drawing](#the-black-drawing)).
- **Black iron:** the wheels, hubs and flywheel are even black circles, 0.42 mm
  apart, so the iron reads black without going solid.
- **Through the rear wheel:** everything up to the tender's straight front
  edge is a black hatch, a shade lighter than the wheel rims; the green tender
  carries on beyond it.
- **Red-brown:** the fork, scrapers, their mounts and the chain spring bar
  are red lines with a brown line in every gap. This darkens the bright red
  toward the engine's red-brown paint. Both scrapers on the rear roll are
  filled from edge to edge, including the forward scraper's upper arm. Green
  tender surrounds the rear scraper.
- **Spokes:** every spoke is ruled with the same number of even lines, five
  on the rear wheel and four on the front roll. The lines always run along
  their own spoke's angle, carrying straight on through every piece of it:
  the root at the hub, and the slivers beside the fork.
- **Top-right badges:** the worksplate and Invicta horse have no colour, only
  their black engraving.
- **Gold:** the three brass boiler bands are solid gold from one black edge
  to the other: upright 0.40 mm gold lines about 0.42 mm apart. Each band's
  thin inner line is drawn in gold as one of them. The lines run unbroken from
  end to end of each band, under the thin boiler lines that cross it. They
  stop only where the pump rod passes in front of the band nearest the
  flywheel. The valves, whistle and the cylinder's maker plate are gold lines
  0.50 mm apart. No broad gold pen is needed.
- **Regulator rod:** the rod from the controls, behind the flywheel, to the
  front is one straight, level rod ending in a neat rounded bend down onto the
  motion plate. The open air under it, and between the lubricator's pipes, is
  left as white paper. The boiler below the motion plate beside the flywheel
  is green.
- **Iron without grey:** the chimney, smokebox, headstock, firebox and
  fittings are black-painted iron, hatched in black lines only. The line
  spacing sets the tone, so no grey pen is needed. The chimney and smokebox
  are shaded as cylinders: lines sparse in the light and close in shadow.
- **Other colour:** the boiler, cylinder, tender and spokes are green.

## The black drawing

The studio has no thick black pen, so **every black line is the Black 0.25 mm
pen**. The heavier lines are built up from 0.25 mm strokes that overlap, so
each one prints solid to its full width, with round ends:

| Line | Width | How it is drawn |
|---|---:|---|
| Fine details, fittings, lettering, badges | 0.25 mm | one stroke along the line |
| Engine linework | 0.40 mm | one narrow loop round the line |
| Principal outlines, sheet frame | 0.60 mm | a loop round the line plus the line itself |
| Roller tyres | 1.00 mm | two loops round the line plus the line itself |

The sheet frame is built inward from its line, so it stays inside the
plotter-safe area.

Engine linework is one weight heavier than the blueprint: 0.30 → 0.40 mm,
0.40 → 0.60 mm, and the roller tyres 0.50 → 1.00 mm. These keep their
original weights, so they do not close up:

- fasteners, rivets, chains and threads;
- small fittings, scrapers and controls;
- lettering, both badges and the sheet frame.

The revision-14 path shapes are kept byte-for-byte except the **regulator
rod**. Revision 14 drew the rod at different heights either side of the
flywheel, sloping at the controls, and ending in a shallow slant that notched
the motion plate. Here it is redrawn as one level rod with a rounded bend.
That reshapes 9 paths: the rod, the two brackets it passes through, and the
plate edge beside the lubricator. It also removes 2 paths, the old slanted
end. The other 852 shapes are unchanged.

The only source lines not drawn in black are the thin inner lines of the three
boiler bands (four paths). Each sits only 1.08 mm from its band's front edge,
too close to fit a gold line beside it. They are drawn in gold instead, so each
band reads as one gold bar between its black edges.

The colour is **2,612 added pen lines**, across 45 parts and 281 enclosed paper
cells. Every colour line keeps **at least 0.22 mm of white paper** from black
ink, so inks never mix and small pen-change offsets stay hidden. The one
exception is the band gold. It runs under the two thin boiler lines that cross
the bands, as the bands' gold inner lines do.

## Plot and print

Use **A3 landscape, 420 × 297 mm, at 100% / actual size**, on white paper.
The PNG is 4200 × 2970. The PDF is a one-page colour print version. The master
and pen SVGs contain only single-pass stroke paths: no fills, raster images or
live fonts.

Plot the pens in file order, light to dark, with each pen loaded once:

| Step | Actual studio pen | Paths | Use |
|---:|---|---:|---|
| 1 | Gold 0.40 mm | 57 | Boiler bands and their inner lines, valves, whistle, cylinder maker's plate |
| 2 | Green 0.25 mm | 855 | Green paintwork and spokes |
| 3 | Red 0.25 mm | 120 | Red of the fork, scrapers, scraper mounts and chain spring bar |
| 4 | Brown 0.25 mm | 95 | Brown lines between the red, darkening it to red-brown |
| 5 | Black 0.25 mm | 2,472 | 1,489 iron hatching and shade lines, then 983 strokes of black linework |

The optimised job estimates **50 minutes 6 seconds** (42:35 to 57:37) with
the nominal AxiDraw-class profile. It draws 3,599 strokes with 9.6 m of
pen-up travel; document order would take 57.3 minutes. No physical
machine was operated; pen and timing calibration remain the studio's usual
pre-plot steps.

Every line is drawn with one of five studio pens: Black 0.25, Green 0.25,
Red 0.25, Brown 0.25 and the 0.40 mm Gold. No grey pen, no thick black pen
and no broad gold nib are used. If the gold pen ever changes, set
`GOLD_NIB_MM` in
`recipe/tools/engineering_source_plates/aveling_5499_colour_v5/inventory.py`
and rebuild. The gold line spacing and clearances follow from it.

- [gold-0-4](artwork/aveling-porter-5499-colour-hatched.pen-01-gold-0-4.svg)
- [green-0-25](artwork/aveling-porter-5499-colour-hatched.pen-02-green-0-25.svg)
- [red-0-25](artwork/aveling-porter-5499-colour-hatched.pen-03-red-0-25.svg)
- [brown-0-25](artwork/aveling-porter-5499-colour-hatched.pen-04-brown-0-25.svg)
- [black-0-25](artwork/aveling-porter-5499-colour-hatched.pen-05-black-0-25.svg)

## Verification and source scope

All **188 format checks** and strict SVG preflight pass. `verify.py` rereads
the exported SVG and independently checks that:

- every outline shape is unchanged, apart from the documented regulator-rod
  redraw;
- every black line is the one Black 0.25 mm pen. Each heavier line's strokes
  cover at least 99% of its full width and never go beyond it;
- the four band inner lines are gold;
- the gold band inner lines meet black only where the blueprint's lines join
  them;
- the band gold runs under only the two documented boiler lines;
- the open air under the regulator rod and between the lubricator pipes has
  no colour, and every area corrected in review carries its ink;
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
- all five pen files together equal the master;
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
