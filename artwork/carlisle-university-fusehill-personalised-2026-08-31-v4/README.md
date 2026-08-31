# University of Cumbria — corrected personalised Carlisle map v4

This A3 portrait edition corrects the v3 header and footer while preserving the
approved Fusehill-centred crop. The original narrow diamond compass is restored
to its original far-right memorabilia zone. The coordinates sit beneath the
university title and their first plotted ink edge is exactly aligned with the
title's first ink edge.

The footer now uses the bundled Hershey Serif display face on a 0.40 mm pen:

- `Stuart R. Nelis` at 8.024 mm cap height;
- `BSc Applied Psychology` at 7.08 mm cap height; and
- right-aligned `2024` at 7.08 mm cap height.

No field labels or writing rules are drawn. A physical bounds gate proves that
the footer ink remains between the map field's left and right borders.

Carlisle railway station is now mandatory source geometry. Its pinned OSM
building, `way/566812584`, is selected and reaches the SVG as one physical
purple landmark path. The map crop, supported roads, water, parks, railways,
and all seventeen mandatory Fusehill university buildings remain present.

## Finished files

- `artwork/carlisle-university-fusehill-personalised-a3-portrait.svg` — master
  A3 vector plate.
- `artwork/carlisle-university-fusehill-personalised-a3-portrait.png` — 254 DPI,
  2970 × 4200 pixel preview.
- `artwork/carlisle-university-fusehill-personalised-a3-portrait.plot.json` —
  source, composition, completeness, pen, and physical-audit manifest.
- `artwork/pen-svgs/` — eleven page-sized SVGs, one per physical pen load.
- `simulation/carlisle-university-fusehill-personalised-plotsim.html` — portable
  animated simulator.
- `simulation/carlisle-university-fusehill-personalised.plotjob.json` —
  deterministic SVG-SHA-bound simulation job.
- `simulation/axidraw-class-simulation-v1.json` — nominal, non-executable
  machine profile used for the included timing simulation.
- `sources/carlisle-city-overpass-2026-08-30.json.gz` — exact pinned map source.
- `SOURCE-CONTRACT.json` — crop, typography, station, university-feature, and
  renderer contract.
- `PLOTTER_HANDOFF.md` — physical pen order, import settings, inspection
  commands, and calibration gates.
- `docs/rebuild.sh` — complete offline rebuild and QA process.

## Rebuild and verify

The canonical offline rebuild belongs in the source `city-map-plotter`
workspace. Place this release at the same relative package path there, then run:

```bash
artwork/carlisle-university-fusehill-personalised-2026-08-31-v4/docs/rebuild.sh
PYTHONPATH=src .venv/bin/python \
  artwork/carlisle-university-fusehill-personalised-2026-08-31-v4/docs/verify_release.py
```

The rebuild uses only the pinned source, fixes `SOURCE_DATE_EPOCH` to the v4
edition date, and fails if the original compass bounds, title/coordinate
alignment, footer type, footer bounds, station path, campus buildings, source
bytes, format, or renderer hashes drift. The stored artifact hashes are the
portable release identity; a rebuild in a different checkout records that
checkout's absolute pinned-source path in provenance and therefore creates a
new SVG hash and a newly bound plot job without changing the plotted geometry.
The rebuild recipe is retained here as provenance; it is not required to open,
simulate, inspect, or import the supplied finished SVG files.

## Plot status

This remains a review artifact. The simulator estimates 41:58, with an
uncalibrated 35:40–48:15 range, 40.93 m of pen-down travel, 16.11 m of pen-up
travel, 2,828 lifts, and eleven pen loads. Hardware execution remains blocked
until pen widths, paper, and machine timing are measured and 1,302 proximity
candidates receive human sign-off.

`ATTRIBUTION.md` must accompany every public use of the clean SVG or PNG.
