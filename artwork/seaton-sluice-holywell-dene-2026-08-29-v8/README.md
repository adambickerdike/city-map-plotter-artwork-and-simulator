# Seaton Sluice, Holywell Dene and Seaton Delaval Hall — v8

This edition keeps the approved v7 A3 map, crop, title and one-line coordinate
treatment while replacing the former diamond compass. The final header cell
now contains a true upward north arrow: a clean vertical shaft and open
arrowhead, intersected below the arrowhead by one horizontal west-east axis.
The three components are independent non-retracing paths on the real black
0.25 mm pen.

Latitude and longitude remain one right-aligned line:
`55.0747 N   /   1.4970 W`, with three spaces around the slash and 0.25 mm of
additional physical tracking between every character.

The 372 × 217.714 mm full-width map, one-line `SEATON SLUICE` title and
approximately 1:13,180 geographic crop are unchanged. Every point of all five
connected Seaton Burn/Holywell Dene source ways remains inside the frame, as do
Seaton Sluice and the complete sourced Seaton Delaval Hall footprint.

## Finished files

- `artwork/seaton-sluice-holywell-dene-a3-landscape.svg` — master vector plate.
- `artwork/seaton-sluice-holywell-dene-a3-landscape.png` — 254 DPI preview,
  4200 × 2970 pixels.
- `artwork/seaton-sluice-holywell-dene-a3-landscape.plot.json` — complete
  render, source, completeness, landmark and physical-audit manifest.
- `artwork/pen-svgs/` — 11 page-sized SVGs, one for each pen load.
- `simulation/seaton-sluice-holywell-dene-plotsim.html` — portable animated
  viewer with document and optimised orderings.
- `simulation/seaton-sluice-holywell-dene.plotjob.json` — source-SHA-bound,
  optimised simulation job.

## Factual focus

Seaton Delaval Hall is not inferred or sketched: `way/60578761` is required on
the render command and its entire sourced footprint must remain inside the
plate. The Dene is the connected chain of five named `Seaton Burn` source ways;
v8 verifies every source point in all five ways lies inside the render extent.
Two sourced coastline ways construct the visible coast and sea.

This remains a basemap composition. It does not claim that any line is a
walking route, race route or official course.

## Rebuild and verify

From the repository root:

```bash
review-output/seaton-sluice-holywell-dene-2026-08-29-v8/docs/rebuild.sh
.venv/bin/python \
  review-output/seaton-sluice-holywell-dene-2026-08-29-v8/docs/verify_release.py
```

The rebuild is offline and reads the pinned snapshot. SVG geometry is
deterministic; the embedded generation timestamp changes on each render.

## Plot status

This is intentionally review-only. Physical execution remains blocked until
the exact machine timing, paper stock and effective pen widths are measured.
The deterministic simulation contains 2,742 strokes and 36,402 vertices across
11 pen loads: 27.65 m pen-down, 16.03 m optimised pen-up travel and 2,742 lifts.
Its nominal estimate is 37:31, with an uncalibrated range of 31:53–43:09. The
614 below-nib separation candidates remain unaccepted for human review.

The external OpenStreetMap credit in `ATTRIBUTION.md` must accompany any public
use of the clean plate.
