# Import the city-style stadium collection

Pull `main` and fetch these LFS assets:

```bash
git pull --ff-only
git lfs pull --include="artwork/uk-stadiums-city-style-2026-09-08/**,artwork/uk-stadiums-overhead-2026-09-08/**"
python3 scripts/verify_stadium_city_maps.py
```

Open `index.html` locally for the 44-ground gallery. GitHub renders the Markdown index; its HTML links show source until opened locally.

Each catalogue `files` path is relative to the release directory. `svg` is the final A3 master; `png` is its 254 DPI preview; `detail` is cropped from that exact preview; `plotjob` is compiled from the final SVG; `pens` contains absolute-page, registration-matched layers in the compiled load order. The two files in `simulation/` animate the actual exported strokes.

Print at 100% / actual size, A3 portrait 297 × 420 mm. Nominal pen widths remain 0.25/0.40 mm colours and the existing black inventory. The retained tiny architectural details are listed for physical proofing in each QA file. No hardware was operated or enabled.

## Rebuild

The existing frozen renderer is at `artwork/production-maps-2026-09-06/reproduction/renderer`. Its relevant source/style files are hashed in this edition's `reproduction/INPUTS.json`. The original stadium overlays are bound to the unchanged source handoff. Per-ground reference-complete map extracts are included; no live map query is needed.

Use Python 3.13 with the existing production reproduction requirements plus Pillow. From the repository root:

```bash
python3 scripts/build_stadium_city_maps.py --workers 4
python3 scripts/package_stadium_city_maps.py
python3 scripts/verify_stadium_city_maps.py
```

The builder's local intermediate maps are under ignored `build/stadium-house-work/closer-club-v2/`. The cache binds the render command and source bytes, so changing the framing regenerates the basemap. Original overlays retain their source editions and reference-era caveats; see the original handoff for stadium reuse in other projects.
