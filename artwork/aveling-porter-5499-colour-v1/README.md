# Aveling & Porter No. 5499 - colour pen edition

**1904 R6 · Works No. 5499 · BS 8711 · A3 landscape**

[Open the drawing](index.html) · [A3 PDF](artwork/aveling-porter-5499-colour-preview.pdf) ·
[Plot SVG](artwork/aveling-porter-5499-colour.svg) · [Compare editions](evidence/comparison.html) ·
[Photographs and palette](evidence/reference-review.html) · [Plot simulator](plot/aveling-porter-5499-colour-viewer.html)

![Colour pen drawing of Aveling and Porter No. 5499](artwork/aveling-porter-5499-colour.png)

This colour edition uses the latest refined **revision-14 drawing** on white
paper. The retained photographs guide green bodywork and wheel spokes, red
frames and scraper arms, black metalwork and gold brass accents. The detailed
Invicta horse and worksplate lettering remain fine black engraving.

The palette uses the actual studio Green, Red, Black and Gold inks. It conveys
the photographed paint and material groups; it is not a measured paint match.
The real Gold pen is 1.00 mm, so it is reserved for boiler-band centres and
worksplate rims. Fine brass details remain in black to keep them clear.

## The refined drawing is retained

All **863 source paths** are carried over. **860 paths are identical**, including
all engine contours, the 40-path horse, the complete lettering and layout.
Three straight boiler-band centre-lines have their endpoints inset 0.80 mm to
keep the broader gold nib clear of adjoining outlines. No component is moved,
rescaled, redrawn or removed. The shortest upper band fragment stays fine black.

The accepted single side elevation spans **371.6 × 189.7 mm**, with the plaque
left of the horse and compact identification along the bottom. The connected
quadrant lever, rounded firebox, chain occlusion, staggered spokes and scraper
connections are retained. There is no ground line or wheel-perimeter guide.
The separate blueprint edition remains available unchanged.

## Plot and print

Use **A3 landscape, 420 × 297 mm, at 100% / actual size**, on white paper.
The PNG is 4200 × 2970. The PDF is a one-page colour print version. The master
and separate pen SVGs contain only single-pass stroke paths, with no filled
areas, raster images or live fonts.

The optimised job uses eight pen loads and estimates **15 minutes 27 seconds**
with the nominal simulation profile. Pen-up travel is approximately 8.8 m,
down from 21.8 m in document order. No physical machine was operated.

| Actual studio pen | Paths |
|---|---:|
| Gold 1.00 mm | 5 |
| Green 0.25 mm | 94 |
| Green 0.40 mm | 26 |
| Red 0.25 mm | 22 |
| Red 0.40 mm | 8 |
| Black 0.25 mm | 613 |
| Black 0.40 mm | 90 |
| Black 0.60 mm | 5 |

The horse uses Black 0.25/0.40 mm; a 1 mm gold nib would obscure its finer
relief. Roller outer contours and the outer page frame use Black 0.60 mm.

- [gold-1](artwork/aveling-porter-5499-colour.pen-01-gold-1.svg)
- [green-0-25](artwork/aveling-porter-5499-colour.pen-02-green-0-25.svg)
- [green-0-4](artwork/aveling-porter-5499-colour.pen-03-green-0-4.svg)
- [red-0-25](artwork/aveling-porter-5499-colour.pen-04-red-0-25.svg)
- [red-0-4](artwork/aveling-porter-5499-colour.pen-05-red-0-4.svg)
- [black-0-25](artwork/aveling-porter-5499-colour.pen-06-black-0-25.svg)
- [black-0-4](artwork/aveling-porter-5499-colour.pen-07-black-0-4.svg)
- [black-0-6](artwork/aveling-porter-5499-colour.pen-08-black-0-6.svg)

## Verification and source scope

All **272 format checks** and strict SVG preflight pass. The export audit checks
physical pen assignments and widths, the three-nib minimum path length,
eight-nib minimum cap height, complete lettering, all pen files against the
master, unchanged contours and the three constrained gold-line insets.

Twelve text blocks remain identical to the fully verified revision-14
single-stroke font geometry. The accepted engine geometry inherits its
verified joins, circular features and occlusions from the hash-pinned source.
Gold rims retain clear space to adjacent fine black rims.

[Colour plan](evidence/colour-plan.json) · [Verification](evidence/verification.json) ·
[Visual review](evidence/visual-review.json) · [Rebuild proof](evidence/rebuild-verification.json) ·
[Source ledger](evidence/sources.json)

Actual-engine photographs by Benjamin Matthews and Terry Pinnegar constrain
the engine and its colour groups. The MERL Fowler S1021 drawing informs
composition only. GW Railwayana Auctions supplies the Invicta casting and
separate No. 6882 worksplate references; the latter's lettering arrangement
is adapted to the user's No. 5499 wording. These references do not establish
surveyed No. 5499 dimensions. Image credits and scope remain in the source ledger.

## Portable rebuild

The package includes the frozen revision-14 SVG, reference evidence, colour
builder and the exact shared source used to export and verify the drawing.
The recipe has no dependency on a separately updated project checkout.

```bash
python3 -m venv .venv
.venv/bin/pip install -r recipe/requirements.txt
.venv/bin/python recipe/rebuild.py --output-dir /tmp/aveling-5499-colour-rebuild
```

Python 3.13 and Inkscape 1.4.4 were used. The command recreates the SVG, pen
layers, PNG, plot job, simulator, verification record and A3 PDF. Add
`--skip-pdf` to rebuild plot files without the conventional-print export.

After cloning the publishing repository, obtain the large-file objects with:

```bash
git lfs pull --include="artwork/aveling-porter-5499-colour-v1/**"
```

Run `sha256sum -c CHECKSUMS.sha256` inside this folder to verify the files.
The sibling ZIP contains the complete files, not Git LFS pointers.
