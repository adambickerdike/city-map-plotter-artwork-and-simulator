# Corrected personalised Carlisle map v4 — QA results

Run on 31 August 2026 from `/home/adam/Projects/city-map-plotter`.

## Release checks

- A3 plate validator: PASS, 3,108 checks.
- Fail-closed release verifier: PASS.
- Pinned source: PASS, 5,229 elements.
- Raw geometry integrity: PASS, zero failures.
- Highway completeness: PASS, zero unresolved in-frame source ways.
- Master and pen SVG parsing: PASS, one master plus eleven pen files.
- PNG: PASS, 2970 × 4200 pixels at 254 DPI.
- Plot job: PASS, 2,828 strokes and eleven pen loads; execution blocked.

## Requested corrections

- Original compass: PASS. The v4 compass has the same path bounds as v2:
  x 258.470, y 34.115, width 8.830, height 27.595 mm.
- Compass position: PASS, original far-right `memorabilia_compass` zone.
- Coordinate alignment: PASS. University title and coordinate ink both begin
  at x 25.635 mm.
- Footer font: PASS, bundled Hershey Serif display paths on Black 0.40 mm.
- Footer size: PASS, 8.024 mm name and 7.08 mm degree/year cap heights.
- Footer containment: PASS, including half-nib ink envelopes inside the map's
  horizontal edges.
- Carlisle station: PASS. Mandatory OSM `way/566812584` is selected and emits
  one physical landmark path.
- Fusehill buildings: PASS, all seventeen required footprints selected.
- Labels/rules: PASS, none emitted.

## Visual review

The full-resolution preview was inspected. The restored narrow compass sits at
the far right, the coordinates share the title's visual left edge, and the
larger serif footer reads as finished commemorative typography. The degree and
year remain comfortably inside the map's horizontal borders. Carlisle station
is visible as a purple footprint beside the principal rail corridor.

## Production hold

The release remains review-only because pen widths and machine timing are not
calibrated and 1,302 below-nib proximity pairs require human inspection.

## Code and reproducibility checks

- Focused geometry, furniture, city-layout, plotter, and Carlisle pipeline
  suite: PASS, 60 tests, including the fixed-generation-timestamp export.
- Format-contract suite excluding the repository-wide historical-output
  ratchet: PASS, 38 tests. Eleven overlap the focused suite, for 87 distinct
  relevant tests in total.
- Frozen map reproducibility: PASS for recipe, renderer contract, source
  contract, high-detail geometry, and Python geometry environment.
- Repeat-build determinism in one checkout: PASS after fixing generation
  metadata to the v4 edition date through `SOURCE_DATE_EPOCH=1788134400`.
- Static checks: PASS for Ruff, mypy, Python compilation, shell syntax,
  duplicate format-spec identity, and `git diff --check`.

The deliberately excluded historical-output ratchet covers unrelated older
artwork throughout `output/`; it has 46 known pre-existing nonconformant
subcases and does not include this v4 review release.

## Approved-map preservation check

Path-data digests for water areas, waterways, green space, road areas, local
roads, minor roads, paths, railways, major roads, secondary roads, and the map
frame are byte-identical to v3. The landmark-building layer retains 33 paths;
the sole source-object substitution is the requested station
(`way/566812584`) in place of the automatically selected Carlisle Civic Centre
(`way/222903738`).

## GitHub portability check

- Clean worktree based on the current GitHub `origin/main`: PASS.
- Complete pinned-source rebuild from that worktree: PASS.
- Shared plotter simulator/controller suite: PASS, 29 tests.
- Supplied plot-job digest and master-SVG binding: PASS.
- Package checksum ledger: PASS, 29 files plus the checksum ledger itself.
