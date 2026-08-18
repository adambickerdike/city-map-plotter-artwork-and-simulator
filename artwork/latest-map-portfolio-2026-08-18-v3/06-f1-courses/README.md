# Formula 1 circuit atlases — current-format v2.3

## What is here

Both promoted technical-review packages: 22 eligible current events and 19 curated former-event configurations, each in all six binding formats.

- Distinct examples: 246
- Master artwork: 246 SVG files and 246 PNG previews
- Plot manifests: 246
- Contact sheets: 12
- Status: Digital technical review passed; rights/physical holds remain.

**Required caveat:** Independent artwork, not an official F1/FIA product. Madrid is held from the current matrix; current OSM context is not an event-configuration or period reconstruction.

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
.venv/bin/python tools/build_f1_circuit_series.py \
  --all-renderable \
  --catalog src/city_map_plotter/data/f1-circuits-2026.json \
  --format all --dpi 254 \
  --output-dir review-output/f1-circuit-atlas-2026-v2.3-format-v1-2026-08-16 \
  --qa-profile review --generated-at 2026-08-16T00:00:00Z
.venv/bin/python tools/build_f1_circuit_series.py \
  --all-renderable \
  --catalog src/city_map_plotter/data/f1-circuits-legacy-v1.json \
  --format all --dpi 254 \
  --output-dir review-output/f1-circuit-atlas-legacy-v2.3-format-v1-2026-08-16-r2 \
  --qa-profile review --generated-at 2026-08-16T00:00:00Z
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
