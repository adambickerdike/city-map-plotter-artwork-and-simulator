# Aveling & Porter No. 5499 - custom blueprint, revision 14

**1904 R6 · Works No. 5499 · BS 8711**

[Open the drawing](index.html) · [Detail comparison](evidence/detail-review.html) ·
[Plot SVG](artwork/aveling-porter-5499-blueprint.svg) ·
[A3 PDF](artwork/aveling-porter-5499-blueprint-preview.pdf) ·
[References](evidence/reference-review.html) · [Plot simulator](plot/aveling-porter-5499-viewer.html)

The accepted single side elevation fills **371.6 × 189.7 mm**, centred horizontally above
the compact bottom identification strip. The top-right space carries the
Invicta horse and a separate enlarged oval worksplate.

## Refined Invicta horse

The horse is rebuilt from the **user-selected GW Railwayana Auctions casting
photograph**, with one continuous smooth outline and 39 named relief details.
The face, ears, eye, nostril, flowing mane, shoulder, haunch, leg tendons,
hoof joints and tail follow that retained image. Small reflections, scratches
and corrosion marks are omitted so the engraved form remains clear on paper.

The photographed casting is rotated 45 degrees clockwise into an upright
rearing pose, then scaled uniformly to 45.54 mm tall. This is a presentation
rotation, not a calibrated perspective reconstruction. The enlarged plaque
remains to its left, aligned on the same centre. The comparison page includes
the photograph and an overlay of the exact contour and relief curves.

All **823 non-horse paths** match revision 13 exactly, including the engine,
connected lower quadrant lever, plaque, lettering and sheet frame. The horse
uses the existing 0.30 mm detail and 0.40 mm outline pens.

All 12 text blocks use the bundled **plotter-grid-v4 single-stroke font**.
An export audit regenerates each complete text block, including the curved
worksplate heading, and compares its strokes against the plotted paths. There
are no missing glyph strokes, live fonts, filled font outlines or raster text.
Every cap height is at least eight times its assigned nib width; every plotted
path meets the three-nib length floor. The smallest copy is 3.00 mm on 0.30 mm.

The master, three separate pen files and plot job use the configured
`white-blueprint-pens` inventory, with one pass and no artificial stroke
thickening. SVG widths and physical pen metadata agree exactly:

| Pen | Use | Plotted paths |
|---|---|---:|
| White 0.30 mm | Fine mechanics, worksplate and small footer lettering, inner frame and rules | 733 |
| White 0.40 mm | Main engine contours, emblem outline and larger footer lettering | 125 |
| White 0.50 mm | Roller outer contours and outer sheet frame | 5 |

[Layout comparison](evidence/detail-review.html) ·
[Font and pen verification](evidence/plotting-verification.json)

## Reference scope

Actual-engine photographs by Benjamin Matthews and Terry Pinnegar constrain
the engine. The supplied video frame supports the steering-wheel and width
interpretation. The [MERL Fowler S1021 sheet](https://merl.reading.ac.uk/news-and-views/blueprints-steam-ahead/)
informs composition and drafting language only.

The separate horse uses the [GW Railwayana Auctions March 2025 lot 51 photograph](https://www.gwra.co.uk/auctions/aveling-porter-invicta-road-roller-cast-brass-hors-2025mar-0051.html).
The former Preston Services reference is retained as historical evidence only.
The oval worksplate adapts the arrangement in the user's
[GW Railwayana Auctions reference](https://www.gwra.co.uk/auctions/worksplate-by-royal-letters-patent-no-6882-aveling-2021nov-0388.html).
That reference is **No. 6882**, not 5499: only its oval proportions and lettering
arrangement are adapted, with the user's No. 5499 wording. Its dimensions and
other engine specifications are not assigned to this engine. The letters are
redrawn as legible plotting strokes, not presented as an engraving facsimile.

This is an **interpretive side elevation, not to scale**. Partly hidden
construction and the simplified staggered spoke arrangement are reconstructed for the requested illustration, not measured factory
geometry. No unverified overall
dimensions, boiler pressure or horsepower are printed.

## Plot and print

Use **A3 landscape, 420 × 297 mm, at 100% / actual size**.

- [White 0.30 mm](artwork/aveling-porter-5499-blueprint.pen-01-white-0-3.svg): fine mechanical lines, small copy, inner frame and rules.
- [White 0.40 mm](artwork/aveling-porter-5499-blueprint.pen-02-white-0-4.svg): principal contours and identity copy.
- [White 0.50 mm](artwork/aveling-porter-5499-blueprint.pen-03-white-0-5.svg): outer frame and roller outer contours.

The master and pen SVGs contain stroke paths only. Blue is the stock-preview
colour; the PDF includes a blue background for conventional printing. The PNG
is 4200 × 2970. Nominal plot time is about **13 minutes**, with three pen loads.
Physical execution uses the shared workflow's normal measured-pen and timing
calibration requirements; no machine was operated for this drawing revision.

## Verification

The exported master contains **622 engine paths and 863 total strokes**.
All 116 stock-format checks and strict SVG preflight pass. The checks cover:

- 704 open engine-contour ends, with no unexplained gaps; the cotter's free tips are intentional.
- 173 circular features, preserving radii to within 0.001 mm in exported geometry.
- 299 spoke transitions, with analytic tangent continuity and exported coordinates within the shared 0.001 mm rounding grid.
- Edge-on handwheel profile perpendicular to its shaft, forward shift, complete rear-structure shaft masking, and longer/lower-pivot release grip.
- Continuous near chain and correctly occluded far chain; direct spring-eye contact, leading front attachment stations, hidden far-side winding exit and no chain ink inside the central return pipe.
- Oval-to-band clearance, reduced spoke counts, widened rim bands and actual 0.50 mm rim strokes; centred three-cell footer and requested copy removal.
- Forward scraper contact, upward chimney taper and corner badge clearance.
- A vertical firebox wall aligned exactly with the upper housing, a smooth 90-degree corner and correct wheel occlusion. Exported corner radial error is below 0.001 mm.
- The lower quadrant lever shares the upper handle axis and meets the quadrant and occluding rear rim within 0.002 mm; no new lever ink crosses opaque wheel or chassis material.
- All 823 non-horse paths match revision 13 exactly. The source hash, 45-degree rotation, uniform scale and all 40 horse paths are verified against the frozen source vectors. The 39 reliefs remain inside the outline without crossed strokes. Complete font-stroke and exact pen-width checks pass.
- Source hashes, master/plot-job identity, equivalent pen layers and the final one-page A3 PDF.

The geometric checks establish drawing consistency, not dimensional authenticity.
The scoped `heritage_drawing`, `heritage_footer` and `heritage_badge` zones follow
the user's custom layout; the shared format specification is unchanged.

[Source ledger](evidence/sources.json) · [Landmarks](evidence/reference-landmarks.json) ·
[Reconstruction](evidence/reconstruction.json) · [Verification](evidence/verification.json) ·
[Visual review](evidence/visual-review.json)

Image rights remain with the photographers and publishers. Retained references
support local review; the package does not assert a publication licence.

## Rebuild from this package

`recipe/` includes the drawing modules and the exact shared Python source and
format data used by the builder, verifier and simulator. It does not rely on
a separately updated checkout. Python 3.13 and Inkscape 1.4.4 were used.

```bash
python3 -m venv .venv
.venv/bin/pip install -r recipe/requirements.txt
.venv/bin/python recipe/rebuild.py --output-dir /tmp/aveling-5499-rebuild
```

The command rebuilds SVG/PNG artwork, compiles the plot job, runs drawing/font/
pen checks, creates the simulator and exports the blue-background A3 PDF.
The retained evidence is copied into the chosen output folder. Source bytes
and the format contract are pinned in the package checksums. An independent
rebuild was checked against the final master SVG.

After cloning the GitHub repository, fetch this package's large-file objects:

```bash
git lfs pull --include="artwork/aveling-porter-5499-blueprint-v14/**"
```

Run `sha256sum -c CHECKSUMS.sha256` inside this package to verify every file.
The sibling ZIP contains the complete package with real files, not LFS pointers.
Revision 13 remains available as the previous published edition. Earlier working revisions remain local archives.
