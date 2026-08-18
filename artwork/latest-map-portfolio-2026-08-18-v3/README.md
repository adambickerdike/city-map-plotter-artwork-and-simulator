# Latest map portfolio — 18 August 2026

This review-only handoff collects the newest authoritative or best-available
local examples across the seven requested map families. It contains **423
distinct plates**, each as editable SVG, PNG preview, and plot manifest, plus
**22 contact sheets**. The complete file inventory is bound
by `CHECKSUMS.sha256` and the artwork index is `catalog.json`.

| Family | Plates | Status |
|---|---:|---|
| [UK university cities — ranked v2.1.4](01-university-cities-uk/README.md) | 30 | Frozen exact-source digital review cohort. |
| [US university cities — ranked v2.1.4](02-university-cities-us/README.md) | 20 | Frozen exact-source digital review cohort. |
| [Hiking maps — paired contour release v4.2](03-hiking-maps/README.md) | 80 | Digital QA passed; review-only. |
| [Verified full-course marathon plates — 2026-08-16 v1](04-marathon-courses/README.md) | 14 | Pinned-source, verified-course digital review cohort. |
| [Rowing race courses — A5 and A3, 8 August 2026](05-rowing-races/README.md) | 8 | Pinned-source rowing-course digital review cohort. |
| [Formula 1 circuit atlases — current-format v2.3](06-f1-courses/README.md) | 246 | Digital technical review passed; rights/physical holds remain. |
| [Twenty-Five Icons of Golf — v4](07-golf-courses/README.md) | 25 | Digital QA passed; review-only nominal unmeasured pens. |

Open `index.html` for the complete visual gallery. Each family folder has the
same predictable layout: `artwork/`, `contact-sheets/`, `contracts/`, `code/`,
`docs/`, `release-metadata/`, `README.md`, and `LLM_HANDOFF.md`.

## Rebuild this handoff index

After rebuilding the seven source releases with the commands in their family
READMEs, run from the repository root:

```bash
.venv/bin/python tools/build_latest_map_portfolio.py --check-only
.venv/bin/python tools/build_latest_map_portfolio.py \
  --output review-output/latest-map-portfolio-2026-08-18-v3
```

The compiler stages atomically, refuses to overwrite an existing destination,
requires exact SVG/PNG/manifest pairing, validates every copied master against
the binding format, and writes a complete checksum inventory.

## Important boundaries

- The university v2.1.4 cohort is rebuilt from its frozen 50-subject source and
  renderer contracts, split here into UK 30 and US 20.
- Hiking v4.2, F1 v2.3, and golf v4 passed their documented digital gates but
  remain review-only pending the stated rights and physical-proof work.
- The marathon folder contains 14 verified full-course plates using the exact
  established `output/marathon-series` visual recipe. Organiser vectors, the
  normalized geometry, course checks, and matching basemap snapshots are pinned.
- The rowing folder contains all four latest sourced race courses in both A5
  and A3. Each line is the measured river centre-line between named endpoints;
  the included contract records the organiser descriptions and source geometry.
- F1 includes both promoted v2.3 packages: 132 current-calendar plates and 114
  former-event plates. Madrid remains a declared hold, not an approximated map.
- `.pen-NN-*.svg` jobs are deliberately excluded because they duplicate the
  master design once per physical pen. The included code regenerates them.
- No portfolio plate or regenerated contact sheet prints OpenStreetMap/OSM
  wording. The legally required map-data credit is externalized to
  `ATTRIBUTION.md`; source/licence metadata and evidence remain intact.

## Safe rebuild order

1. Read `shared/handoff/AGENTS.md`, `CODEX_MAP_HANDOFF.md`, and
   `REPRODUCING_MAPS.md`.
2. Treat `shared/plate-contract/format-v1.json` as binding; edit its builder,
   never the JSON, if the global format contract changes.
3. Rebuild a source cohort to a new output directory using its family README.
4. Run `tools/validate_format.py` on every master SVG and inspect PlotSim plus
   the full contact sheets.
5. Build a new portfolio path; this compiler refuses to overwrite an existing
   package.

Generated output remains ignored by Git and must not be committed as finished
artwork.
