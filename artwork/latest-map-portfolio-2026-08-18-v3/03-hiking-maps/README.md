# Hiking maps — paired contour release v4.2

## What is here

All 40 frozen routes, each as detailed-map and terrain-relief variants: 80 distinct A5 plates.

- Distinct examples: 80
- Master artwork: 80 SVG files and 80 PNG previews
- Plot manifests: 80
- Contact sheets: 2
- Status: Digital QA passed; review-only.

**Required caveat:** Not a navigation product. Physical proof, per-source rights, and intended-stock pen calibration remain required.

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
mapplot-hike build --all --format catalog \
  --output-dir output/hiking-series-paired-v4.2-2026-08-06
PYTHONPATH=src .venv/bin/python tools/qa_niche_series.py \
  output/hiking-series-paired-v4.2-2026-08-06
PYTHONPATH=src .venv/bin/python tools/audit_hiking_composition.py \
  output/hiking-series-paired-v4.2-2026-08-06
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
