# Hiking contour release audit — V4.2

Date: 2026-08-06  
Release: `output/hiking-series-paired-v4.2-2026-08-06`  
Preset: `HIKE-A5-V2`  
Status: **digital QA passed; review-only pending physical proof and rights review**

## Outcome

The complete 40-route hiking collection was rebuilt as 80 north-up A5 artworks:
one detailed context map and one terrain-relief map for every route. The release
keeps the airy West Highland Way / Great Glen Way context language and the
continuous Tour des Refuges relief language while making the contour hierarchy
explicit and plotter-safe.

All 80 artifacts pass the semantic package verifier and composition audit. The
full hiking test suite passed with 310 tests, and the changed Python surface is
clean under Ruff. SHA-256 verification passed for the release package.

## Contour hierarchy

- Minor contours use Grey 0.25 mm.
- Every positive fifth source interval is a true index contour using Grey
  0.40 mm. Zero metres is never promoted to an index.
- The interval comes from the declared rendered source interval when available,
  otherwise from the robust modal interval in the frozen source stack.
- Minor and index geometry are separate semantic layers and separate pen jobs.
  Arbitrary minor contours are never promoted merely to improve appearance.
- Index labels are drawn only on emitted, true index contours. Detailed maps do
  not carry contour-height labels; relief maps carry at most four.
- Copy masks preserve clearance around routes, labels, peaks, water, forests and
  other factual context. Fragments shorter than three nib widths are removed.
- If masking makes a selected level unplottable, the final SVG inventory and
  metadata record the omission rather than claiming a contour that is no longer
  present.

The Grey 0.40 pen is conditional. It appears immediately after Grey 0.25 in a
manifest only when the artwork contains a genuine index contour.

## Detail and plot-load control

Contour selection retains complete source levels rather than clipping arbitrary
pieces to meet a visual target. Optional minor levels are dropped before true
indices when the drawing would become too dense.

Density is evaluated as physical, width-normalized pen travel: Grey 0.40 is
weighted at `0.40 / 0.25 = 1.6` relative to Grey 0.25. The accepted maxima are
0.180 equivalent mm/mm² for detailed maps and 0.350 equivalent mm/mm² for relief
maps. The measured collection maxima are:

| Variant | Measured maximum | Gate |
| --- | ---: | ---: |
| Detailed map | 0.175618 | 0.180 |
| Terrain relief | 0.333689 | 0.350 |

The shortest emitted relief fragments also clear the three-nib physical floor:
0.751825 mm for Grey 0.25 and 1.418594 mm for Grey 0.40.

The relief edition retains forests, water, route context, named places, named
mountains and factual peak heights where frozen evidence supports them. The
elevation profile remains open and unboxed at the foot of the artwork, with
route distance aligned to profile position. Terrain snippets, framed contour
insets and fall-line scratches are prohibited.

## Factual and source boundaries

Each variant credits the terrain provider that actually supplied its emitted
contours. Split-source records no longer print a native DEM provider on a relief
plate when the selected relief contours came from the frozen global source.
Selected source references, contour inventories and rendered SVG paths agree
across all 80 artifacts.

V4.2 renders terrestrial elevation only. Bathymetry is deliberately absent:
negative values from composite terrain tiles do not establish a qualified
seabed source, chart datum, resolution or commercial licence. Ocean-depth work
must use a separately frozen and licensed bathymetric dataset.

The release remains review-only. Automated source binding and printed credits do
not grant commercial rights. Before sale, review the location-specific terrain
provider terms, route-data rights and required face attribution for every plate.

## Release inventory

- 40 routes and 2 variants per route
- 80 master SVG files
- 80 PNG plate previews plus 2 collection contact sheets
- 80 plot manifests
- 539 pen-separated SVG jobs
- 4,292 relief contour paths: 3,458 minor and 834 index
- 0 negative contour paths
- Relief altitude labels per plate: 20 plates with one, 18 with two, one with
  three and one with four true-index labels

The lower label count on some low-relief routes is intentional. A label is not
fabricated from a minor level merely to fill space; the open distance/elevation
profile still provides route minimum, maximum and chainage information.

## Verification

- Package QA: 80 passed, 0 failed
- Composition audit: 80 passed, 0 failed
- Full hiking test suite: 310 passed
- Ruff: clean on the changed hiking Python and tests
- Checksums: all release files verified
- Visual review: both full contact sheets plus representative flat, mountainous,
  detailed and relief plates reviewed at full size

The remaining production check is a real A5 plot on the intended stock, speed
and machine. In particular, verify that Grey 0.40 reads as an index hierarchy
without competing with feature boundaries, and calibrate all seven possible
pens before a sale edition.

## Review files

- Detailed collection: `output/hiking-series-paired-v4.2-2026-08-06/hikes/hikes-detailed-map-contact-sheet.png`
- Relief collection: `output/hiking-series-paired-v4.2-2026-08-06/hikes/hikes-terrain-relief-contact-sheet.png`
- Artifact index: `output/hiking-series-paired-v4.2-2026-08-06/ARTIFACTS.md`
- Pen-change guide: `output/hiking-series-paired-v4.2-2026-08-06/PEN-CHANGE-GUIDE.md`
- Package QA: `output/hiking-series-paired-v4.2-2026-08-06/qa-report.md`
- Composition audit: `output/hiking-series-paired-v4.2-2026-08-06/composition-audit.md`
- Source register: `output/hiking-series-paired-v4.2-2026-08-06/SOURCES.json`
- Licence register: `output/hiking-series-paired-v4.2-2026-08-06/LICENSES.txt`

