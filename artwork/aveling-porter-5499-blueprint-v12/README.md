# Aveling & Porter No. 5499 - custom blueprint, revision 12

**1904 R6 · Works No. 5499 · BS 8711**

[Open the drawing](index.html) · [Detail comparison](evidence/detail-review.html) ·
[Plot SVG](artwork/aveling-porter-5499-blueprint.svg) ·
[A3 PDF](artwork/aveling-porter-5499-blueprint-preview.pdf) ·
[References](evidence/reference-review.html) · [Plot simulator](plot/aveling-porter-5499-viewer.html)

The accepted single side elevation fills **371.6 × 189.7 mm**, centred horizontally above
the compact bottom identification strip. The top-right space carries the
Invicta horse and a separate enlarged oval worksplate.

## Body and badge refinement

The worksplate now sits **to the left of the horse**, with both centred on the
same horizontal line. Relative to revision 11, the worksplate is **20% larger**
and the horse **50% larger**. Their original proportions and stroke lettering
are preserved. The pair has a 6 mm gap and clears the engine by 8.49 mm.

The diagonal shoulder behind the rear wheel is replaced with a **continuous
upright aligned with the control housing above**, turning smoothly through
90 degrees into the rear deck. The corner is a tangent circular fillet;
the wheel and spokes correctly conceal the portions behind them.

The roller retains its lowered position and original size. Every other engine
path, the footer and frame are unchanged from revision 11. The far chain stays
behind the central return pipe, and ground and wheel-perimeter guides remain
absent. Physical pen widths remain 0.30, 0.40 and 0.50 mm.

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
| White 0.30 mm | Fine mechanics, worksplate and small footer lettering, inner frame and rules | 707 |
| White 0.40 mm | Main engine contours, emblem outline and larger footer lettering | 126 |
| White 0.50 mm | Roller outer contours and outer sheet frame | 5 |

[Layout comparison](evidence/detail-review.html) ·
[Font and pen verification](evidence/plotting-verification.json)

## Reference scope

Actual-engine photographs by Benjamin Matthews and Terry Pinnegar constrain
the engine. The supplied video frame supports the steering-wheel and width
interpretation. The [MERL Fowler S1021 sheet](https://merl.reading.ac.uk/news-and-views/blueprints-steam-ahead/)
informs composition and drafting language only.

The separate horse uses the [Preston Services casting photograph](https://prestonservices.co.uk/item/invicta-horse-aveling-porter/).
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

The exported master contains **620 engine paths and 838 total strokes**.
All 116 stock-format checks and strict SVG preflight pass. The checks cover:

- 700 open engine-contour ends, with no unexplained gaps; the cotter's free tips are intentional.
- 173 circular features, preserving radii to within 0.001 mm in exported geometry.
- 299 spoke transitions, with analytic tangent continuity and exported coordinates within the shared 0.001 mm rounding grid.
- Edge-on handwheel profile perpendicular to its shaft, forward shift, complete rear-structure shaft masking, and longer/lower-pivot release grip.
- Continuous near chain and correctly occluded far chain; direct spring-eye contact, leading front attachment stations, hidden far-side winding exit and no chain ink inside the central return pipe.
- Oval-to-band clearance, reduced spoke counts, widened rim bands and actual 0.50 mm rim strokes; centred three-cell footer and requested copy removal.
- Forward scraper contact, upward chimney taper and corner badge clearance.
- A vertical firebox wall aligned exactly with the upper housing, a smooth 90-degree corner and correct wheel occlusion. Exported corner radial error is below 0.001 mm.
- Exact separate badge transforms from revision 11, matching vertical centres and clear spacing. Every other engine path, footer and frame matches the previous master. Complete font-stroke and exact pen-width checks pass.
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
git lfs pull --include="artwork/aveling-porter-5499-blueprint-v12/**"
```

Run `sha256sum -c CHECKSUMS.sha256` inside this package to verify every file.
The sibling ZIP contains the complete package with real files, not LFS pointers.
Revision 11 remains available as the previous published edition. Earlier working revisions remain local archives.
