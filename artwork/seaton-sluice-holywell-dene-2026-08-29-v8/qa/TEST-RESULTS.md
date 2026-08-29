# Test results

Final verification date: 2026-08-29 (GMT).

## Release and focus checks

- End-to-end offline rebuild recipe: pass.
- Binding plate validator: pass, 3,201 checks for `a3-landscape`.
- Visual inspection of the 4200 × 2970 PNG: pass. The approved v7 map crop,
  full-width map field and one-line title are unchanged.
- Coordinate geometry: one right-aligned line,
  `55.0747 N   /   1.4970 W`, with three spaces on each side of `/` and
  0.25 mm of additional physical tracking between characters.
- Compass: pass. The former diamond is absent. A separate upward shaft and open
  arrowhead form the north arrow, and one horizontal west-east axis intersects
  the shaft below the arrowhead. All three are independent non-retracing paths
  in the separate header cell on the real black 0.25 mm pen.
- Scale: approximately 1:13,180.
- Seaton Sluice reference point: inside the printed extent.
- Seaton Delaval Hall `way/60578761`: fully inside, required by the command and
  present in the master SVG as a purple heritage footprint.
- Connected Seaton Burn/Holywell Dene source chain: all points of all five ways
  verified inside the frame and present in the SVG.
- Coastline: two intersecting source ways verified and present.
- Raw selected-source geometry: zero failures.
- In-frame highway audit: zero unresolved objects and zero unknown values.
- Physical-minimum evidence: zero invalid entries and zero residual sub-nib
  trails.
- Plot-job preflight: 2,742 paths, 36,402 vertices, 11 pen loads and safe page
  bounds. The optimised simulation measures 27.65 m pen-down and 16.03 m
  pen-up, with a nominal 37:31 estimate (31:53–43:09 uncalibrated range).
  Hardware execution is deliberately blocked for unmeasured pens and nominal
  machine timing.

## Regression checks

- Focused city-map, stroke-font, map, furniture, format, OSM, completeness,
  raw-geometry, physical, retrace-audit and plotter suite: 238 passed,
  1 deliberately deselected.
- Ruff for the changed generator, renderer, tests and release Python files:
  pass.
- Python compilation, Bash syntax and release verifier: pass.

The deselected repository-wide ratchet checks every SVG already present under
`output/`. Its unrelated pre-existing `output/collection-contact-sheet.svg` is
a 10,076 × 10,076 mm portfolio sheet rather than one of the six binding paper
formats. It is not part of this release and was not altered. The v8 master
passes the same validator outright.
