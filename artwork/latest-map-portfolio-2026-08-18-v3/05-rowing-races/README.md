# Rowing race courses — A5 and A3, 8 August 2026

## What is here

The latest complete sourced rowing-course release: Head of the River Race, Pairs Head, Henley Royal Regatta, and Head of the Charles Regatta, each rendered as A5 and A3 portrait plates.

- Distinct examples: 8
- Master artwork: 8 SVG files and 8 PNG previews
- Plot manifests: 8
- Contact sheets: 2
- Status: Pinned-source rowing-course digital review cohort.

**Required caveat:** Each course line is the measured river centre-line between organiser-described named endpoints, not a survey of the raced line. Event/source rights, race-day change checks, and physical proof remain required.

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
for preset in a5-balanced-poster a3-balanced-poster; do
  for course in horr-london pairs-head-london henley-royal head-of-the-charles; do
    mapplot export --rowing-course "$course" --course-margin 0.15 \
      --preset "$preset" --poster-layout rowing-course \
      --layers roads,water,railways,parks,buildings \
      --style styles/rowing-course-v1.json --landmark-buildings \
      --water-fill dots --detail-profile plotter-faithful \
      --simplify-mm 0.04 --road-style centreline --no-scale-bar \
      --optimise --split-by-pen --attribution-mode external \
      --external-attribution-placement "Product page, packaging, or caption adjacent to each artwork" \
      --output "review-output/rowing-reproduction/${preset%%-*}/$course.svg"
  done
done
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
