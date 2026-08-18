# UK university cities — ranked v2.1.4

## What is here

The Times and Sunday Times Good University Guide 2026 top 30, as frozen in the ranked-university catalog.

- Distinct examples: 30
- Master artwork: 30 SVG files and 30 PNG previews
- Plot manifests: 30
- Contact sheets: 1
- Status: Frozen exact-source digital review cohort.

**Required caveat:** Review-only until physical pen calibration and the documented external attribution/rights checks are complete.

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
.venv/bin/python tools/check_map_reproducibility.py
.venv/bin/python tools/build_ranked_university_series.py \
  --output-dir review-output/university-memorabilia-ranked-2026-v2.1.4
.venv/bin/python tools/finalize_ranked_university_series.py \
  review-output/university-memorabilia-ranked-2026-v2.1.4/ranked-universities.batch.json
.venv/bin/python tools/qa_ranked_university_series.py \
  review-output/university-memorabilia-ranked-2026-v2.1.4/ranked-universities.batch.json --release-mode review
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
