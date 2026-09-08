# Using the latest Premier League and Championship stadiums

This package contains the existing 20 Premier League and 24 Championship
selections, checked on 8 September 2026. The delivered drawings are copied
unchanged, including **Etihad v10**, Old Trafford/St James' Park v9, and the
latest individual versions for the other grounds. Version numbers differ
because only edited stadiums advanced; use `catalog.json`, not a common suffix.

## Get the files

The repository uses Git LFS for SVG, PNG and `.plot.json` files. In an existing
clone, pull the latest `main` and fetch this package's artwork:

```bash
git pull --ff-only
git lfs pull --include="artwork/uk-stadiums-overhead-2026-09-08/**"
python3 scripts/verify_uk_stadiums.py
```

For a new clone without downloading the unrelated multi-gigabyte portfolio:

```bash
git lfs install
GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator.git
cd city-map-plotter-artwork-and-simulator
git lfs pull --include="artwork/uk-stadiums-overhead-2026-09-08/**"
python3 scripts/verify_uk_stadiums.py
```

Open `artwork/uk-stadiums-overhead-2026-09-08/index.html` locally for all 44
full maps and close-ups. GitHub displays HTML source rather than running the
gallery. Its [Markdown file index](README.md) also links to every stadium.
GitHub's ordinary source ZIP may contain LFS pointers instead of the artwork.

## Choose the right asset

All `files.*.path` entries in [catalog.json](catalog.json) are relative to this
package directory, and include SHA-256 hashes and byte lengths.

| Catalogue entry | Use |
|---|---|
| `map_svg` | Complete editable A3 city map, with the reviewed stadium already placed |
| `map_preview`, `detail_preview` | Full map and close-up PNGs for browsing |
| `stadium_svg` | Standalone editable A2 stadium study, preserving native curves |
| `stadium_overlay` | Stadium and field geometry in local **ground metres**, with geographic registration |
| `map_manifest` | Original sheet, projection, layer and source metadata; not an executable plot job |
| `overlay_manifest`, `stadium_manifest` | Original provenance, validation and reference-era caveats |

## Put a stadium into another map

Read the selected `stadium_overlay` JSON. Render **both** `shell_paths` and
`paths`; the first contains the main external structure, the second contains
roof detail and playing-field markings. Rendering only `paths` loses the
outer stadium. Do not add another mapped stadium perimeter over the shell.

For each path, prefer `commands_m` when present. These are native absolute SVG
commands (`M`, `L`, `C`, `Z`) in metres; transform every coordinate pair,
including both control points of a cubic. `points_m` is a sampled fallback,
not another stroke to draw on top. Where no `commands_m` exists, use
`points_m` and honour `closed`. This preserves the smooth curves and avoids
the doubled outlines we removed.

The overlay's geographic origin is `georeference.latitude/longitude`, with
local +x rotated counter-clockwise by `x_axis_degrees_from_east` from east.
Local +y is perpendicular to +x, not SVG's downward page axis. Using angle
`a` in radians, local coordinates `(x, y)` map to:

```text
east_m  = x*cos(a) - y*sin(a)
north_m = x*sin(a) + y*cos(a)
longitude = origin_longitude + degrees(east_m / (6371008.8*cos(radians(origin_latitude))))
latitude  = origin_latitude  + degrees(north_m / 6371008.8)
```

Project those positions into the destination map. This is the original local
equirectangular convention, not a geodetic survey. Keep the existing field
position and scale; do **not** centre it on the bounding box of the stadium's
annexes. `centring` records the paired stand-front datum and actual field
centre, which need not coincide with `(0, 0)`.

Clip map paths that would be hidden under the actual roof. Where supplied,
`roof_footprint_m` records occlusion surfaces, not additional drawable lines.
Some clean stand boundaries are deliberately open and cannot define a filled
roof polygon. Preserve outside streets; do not use a white fill as a plotter
mask. The delivered full-map SVG already contains the reviewed city-context
clipping and is the safest ready-composed option.

Nominal stadium pen widths are 0.40 mm shell and 0.25 mm detail. They are paper
nib widths, **not** ground metres. Check physical spacing again if the stadium
is drawn at a different scale. Existing A2/A3 SVGs preserve their own paper
dimensions and pen assignments. No simulator jobs or hardware calibration are
claimed by this upload; compile and preflight new jobs for the intended sheet
and machine before plotting.

## Evidence and limits

The original manifests are retained byte-for-byte. Their historical source
paths identify the author's workspace; older editions, source caches,
generator code, and private Google/Esri comparison captures are intentionally
not bundled. They are provenance references, not broken download promises.
Use `catalog.json` for the available files. This is a reusable artwork export,
not a complete from-source rebuild environment.

Read the per-stadium `review_note` and original manifests before changing
details. In particular, Etihad's drawing does not certify the completed 2026
North Stand extension; Wrexham shows the reference-era open Kop end; London
Stadium uses the pre-solar reference. All physical production remains subject
to pen/paper tests. The [attribution and rights notice](ATTRIBUTION.md) must
accompany onward reuse.
