# Reproducing the reviewed high-detail maps

This is the reference procedure for making maps that match the reviewed
York-derived university series. Do not reconstruct the command from screenshots
or rely on whichever OpenStreetMap response happens to be live that day.

The repeatable result is defined by four versioned inputs:

1. the university v2.1.4 release contract (the reviewed v2.1.3 visual renderer
   plus fail-closed subject-specific source pinning);
2. the `university-memorabilia-v2` style and bundled vector font;
3. the exact 50 subject records and their pinned map centres;
4. the exact 50 saved Overpass responses captured on 3 August 2026.

The builder verifies those inputs by SHA-256 and fails before rendering if a
file is missing, altered, or assigned to the wrong subject. The saved-source
mode uses `--input-json` for every item, so it cannot silently fall through to
a live network response.

## What “same detail” means

The ordinary `mapplot export` default remains `faithful`. It is the safest
generic default for maximum source retention: every qualifying acquired road,
including service roads and paths, stays in the cartographic result. It resolves
to a 0.04 mm paper-space error budget and joined centreline roads.

The reviewed university and recent marathon city plates explicitly use
`plotter-faithful`. It begins with the same full cartographic selection, the same
0.04 mm tolerance, and the same centreline road topology, then records and omits
only residual fragments smaller than three physical nib widths. Those fragments
cannot be reproduced reliably by the selected pen. Therefore:

- use the generic `faithful` default when source retention is the priority;
- use the versioned recipe's explicit `plotter-faithful` when matching the
  reviewed, physically drawable posters;
- never use `plot` unless deliberate visual thinning is wanted.

Changing only the detail-profile flag cannot reproduce a university poster.
Paper, crop, layout, families, style, dotted water, landmark policy, inventory,
and source bytes are equally part of the contract.

## Exact environment

The reviewed SVG cohort used:

- CPython 3.13.9;
- NumPy 2.5.1;
- Shapely 2.1.2 with GEOS 3.13.1;
- Inkscape 1.4.4 for the optional PNG previews.

The SVG contains no system-font text. Its Hershey Serif display face and all
other lettering are bundled vector paths, so installing a similar desktop font
does not substitute for the packaged asset.

From a clean clone:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements/university-v2.1.3.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python tools/check_map_reproducibility.py
```

Add `--strict-tools` to the final command when byte-consistent PNG previews are
required. Inkscape is not involved in SVG geometry, so a different or absent
Inkscape only affects raster preview reproducibility.

## Build the complete 30 UK / 20 US cohort

Run the builder from the repository root:

```bash
.venv/bin/python tools/build_ranked_university_series.py \
  --output-dir review-output/university-memorabilia-ranked-2026-v2.1.4-rebuild
```

Pinned subject-specific JSON is the default. No Overpass or Nominatim request
is needed. The command deliberately produces review output because the bundled
`actual-pens` inventory contains nominal rather than measured widths.

After the build:

```bash
.venv/bin/python tools/qa_ranked_university_series.py \
  review-output/university-memorabilia-ranked-2026-v2.1.4-rebuild
```

The exact visual recipe enforced by the builder is:

| Setting | Locked value |
| --- | --- |
| Sheet | A5 portrait, balanced poster |
| Crop | 2 km radius around the catalogued institution centre |
| Layout | `university-memorabilia` |
| Families | roads, water, railways, parks, landmark buildings |
| Style | `university-memorabilia-v2` |
| Water | bank outlines plus physical dot fill |
| Detail | `plotter-faithful`, 0.04 mm, centreline roads |
| Extent fit | contain |
| Pens | `actual-pens` inventory only |
| Furniture | frame and dedicated header compass; no scale bar/detail |
| Output | optimised master SVG, manifest, and registration-matched pen splits |

The source response manifest is
`contracts/university-memorabilia-v2.1/source-snapshots/source-manifest.json`.
Every entry records compressed-file, canonical-JSON, and query hashes plus the
OSM base timestamp and reviewed extent. The renderer and style pins are in
`contracts/university-memorabilia-v2.1/README.md`.

## A single new city versus an exact historical plate

For a new city, use current data deliberately. Preserve the reviewed rendering
recipe but save the acquired response and keep its hash with the artifact. A
live request is expected to change as contributors update the map.

For an exact historical university plate, use its subject's file from the
pinned source manifest. Do not replace it with a new download, even if the new
map is more current. A source refresh is a new cohort and needs a new contract
ID, review run, and checksums.

## Expected non-visual differences

`generated_at`, absolute output paths, and optional PNG encoder metadata can
differ between runs. The reproducibility gate binds source geometry, renderer,
style, font, format, catalog, layer settings, physical pen plan, and semantic
SVG structure. A timestamp difference is not a road-detail difference.

## Marathon status

The recent marathon city studies share the high-detail
`plotter-faithful`/0.04 mm/centreline map treatment, but they are not yet an
equivalent frozen release contract. Most catalogued marathon routes still lack
verified official course geometry, so the repository must not present those
city basemaps as exact course maps. Freeze and verify official route sources,
styles, and map snapshots before calling a marathon cohort reproducible.

## Physical plotting remains a separate gate

Reproducible SVG bytes do not prove that a nominal 0.25 mm pen draws 0.25 mm on
a particular paper at a particular speed. Before sale or production, record the
exact stock, pressure/speed settings, ten-specimen measured calibration, bounds
preview, and visual sign-off. Keep OpenStreetMap attribution with every public
output as declared in the source snapshot notice.
