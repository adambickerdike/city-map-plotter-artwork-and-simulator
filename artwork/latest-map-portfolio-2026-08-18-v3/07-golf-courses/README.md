# Twenty-Five Icons of Golf — v4

## What is here

All 25 curated courses that pass the exact 18-hole source gate, rendered with golf-clarity-course-a3-v4.

- Distinct examples: 25
- Master artwork: 25 SVG files and 25 PNG previews
- Plot manifests: 25
- Contact sheets: 1
- Status: Digital QA passed; review-only nominal unmeasured pens.

**Required caveat:** Not an objective ranking or official course product. Commercial rights/non-endorsement review and physical calibration remain.

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
mapplot-golf build --all \
  --output-dir output/golf-course-series-v4 --dpi 180
PYTHONPATH=src .venv/bin/python tools/qa_golf_series.py \
  output/golf-course-series-v4
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
