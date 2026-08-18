# Verified full-course marathon plates — 2026-08-16 v1

## What is here

Fourteen sourced full marathon courses: current organiser vectors where available, plus Boston's checked OSM course relation. Every route passes the distance and connected-geometry gates.

- Distinct examples: 14
- Master artwork: 14 SVG files and 14 PNG previews
- Plot manifests: 14
- Contact sheets: 3
- Status: Pinned-source, verified-course digital review cohort.

**Required caveat:** Course geometry is included and source-bound. Review-only until route redistribution/event rights, race-day change checks, external attribution placement, and physical proof are complete.

`artwork/` contains only master examples. The source release's `.pen-NN-*.svg`
files are physical machine jobs for those same designs, not additional
examples, so they are intentionally not duplicated here. Rebuild them with the
included generator when preparing a calibrated plot.

`contracts/` contains the frozen or best-available source evidence. `code/`
contains the domain entry points. Shared renderer code and the binding plate
contract live at `../shared/`. `release-metadata/` preserves the source
package's reports, indexes, licences, and original checksums.

## Reproduce

Run from the repository root after reading `AGENTS.md`, `CODEX_MAP_HANDOFF.md`,
and `docs/reproducibility/REPRODUCING_MAPS.md`:

```bash
.venv/bin/python tools/build_verified_marathon_course_series.py \
  --reuse-sources review-output/marathon-course-plates-verified-2026-08-16-v1/source-contract \
  --output-dir review-output/marathon-course-plates-reproduction
```

Then validate the master SVGs and simulate the plot order:

```bash
find <release-directory> -name '*.svg' ! -name '*.pen-*' -print0 | \
  xargs -0 .venv/bin/python tools/validate_format.py
python3 tools/plotsim.py <one-master.svg> --compare
```

The bundle's `CHECKSUMS.sha256`, `catalog.json`, and `BUILD-VALIDATION.json`
record the handoff copy itself.

Portfolio review copies print no OpenStreetMap/OSM wording on the plate.
Required map-data credit is retained in the portfolio-root `ATTRIBUTION.md`,
source metadata, and source contracts. Contact sheets are regenerated from the
portfolio PNGs and supplied as both PNG and SVG.
