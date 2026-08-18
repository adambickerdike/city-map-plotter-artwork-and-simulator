# Hiking collection acceptance gate

`tools/qa_niche_series.py` is the release gate for the paired 40-route hiking
collection. It is intentionally stricter than the generic plate validator: a
technically valid red route silhouette is not accepted as a geographic hiking
map, and one artwork cannot stand in for both the map and relief editions.

Run it only after building all 40 catalogue subjects, both variants, PNGs and
split-pen jobs into a new, otherwise empty release directory:

```bash
mapplot-hike build --all \
  --format catalog \
  --output-dir output/hiking-series-paired-v4.2-2026-08-06 \
  --dpi 300

.venv/bin/python tools/qa_niche_series.py \
  output/hiking-series-paired-v4.2-2026-08-06 --dpi 300

.venv/bin/python tools/audit_hiking_composition.py \
  output/hiking-series-paired-v4.2-2026-08-06 \
  --json output/hiking-series-paired-v4.2-2026-08-06/composition-audit.json \
  --markdown output/hiking-series-paired-v4.2-2026-08-06/composition-audit.md \
  --fail-on-gate
```

PNG generation requires Inkscape; the two contact sheets require ImageMagick
`montage`; and exact PNG parity QA requires ImageMagick `compare`. All three
executables must be available on `PATH`. The QA command writes
`qa-report.json`, `qa-report.md`, and refreshed SHA-256 checksums. Exit status is
zero only when the whole paired collection passes.

## Collection contract

- Exactly the 40 frozen route IDs from `hike-plates-release-v1.json` must be
  present under the `hikes` domain.
- Every route must have exactly one `detailed-map` artifact and exactly one
  `terrain-relief` artifact: 40 of each and 80 masters overall. Extra, missing,
  duplicated or orphaned SVG, PNG or manifest artifacts fail the suite.
- Artifact identity is `<subject-id>--<variant-id>`. The master, preview and
  manifest names are respectively `<artifact-id>.svg`, `<artifact-id>.png` and
  `<artifact-id>.plot.json`; split jobs append
  `.pen-NN-<pen-id>.svg`.
- Every artifact has one editable master SVG, one 300 dpi PNG, one plot manifest
  and exactly one split SVG per active physical pen step.
- The build must index two separate 40-route contact sheets:
  `hikes/hikes-detailed-map-contact-sheet.png` and
  `hikes/hikes-terrain-relief-contact-sheet.png`.
- `SOURCES.json` must preserve one exact source list per subject, prove that the
  two variants share it, and keep the release marked review-only;
  `LICENSES.txt` must expose the commercial-clearance and provider-attribution
  review boundary to an operator without requiring them to inspect SVG metadata.
- Every sheet is binding A5 portrait or A5 landscape. Manifest dimensions,
  orientation, SVG millimetre dimensions, `viewBox`, index schema and PNG pixel
  dimensions must agree.
- The stored PNG is re-rendered from the master SVG and compared pixel for
  pixel. Split jobs must reproduce the corresponding master layer's geometry
  and source metadata exactly.

## Shared geographic contract

Both variants are north-up. Each catalogue record must state zero rotation and
`orientation_status: north-up`; each manifest must state that north is page-up;
and each artwork must show a path-drawn north arrow and `N`. A rotated-to-fit
map, an obsolete rotated north mark or missing orientation evidence fails.

Both variants are also one continuous, full-field composition. The rendering
policy is `full-field-continuous-context-v2`: no geography may carry a
`data-context-view`, and terrain/detail inset frames and their local north marks
are forbidden. Terrain is never reduced to disconnected snippets inside
dashboard boxes.

Every plate must contain the source-sampled hero route, factual terrain,
geographic markers and collision-safe context labels. The 30 expansion records
also carry explicit availability evidence for roads, hydrography and land cover:
a selected family must render, a genuine zero-result query may remain absent,
and source candidates may not disappear merely because they were inconvenient
to draw.

- Roads and tracks are single source centrelines, without invented casings or
  connectors.
- Rivers are source centrelines; lake and sea edges are source shorelines. No
  doubled bank echo is permitted.
- Woodland and grass marks must be bounded by or explicitly anchored to a
  selected source feature. Green decorative masses without source evidence
  fail. Retention metadata is reconciled after the contour-copy legibility
  mask: its final count must equal the unique land-cover source IDs that remain
  in the completed SVG, while the pre-mask count and any omitted records stay
  separately disclosed.
- Settlements, huts, waters and geographic regions are named only from the
  frozen context.
- Terrestrial contours are never treated as underwater depths. V4.2 renders no
  bathymetry because the frozen collection has no separately qualified source
  with audited resolution, vertical/chart datum, licence and provider
  attribution. Negative cells in a composite terrain tile do not qualify.
- Selected peaks use a mountain symbol. A summit height is printed only when it
  is carried by an explicit OSM `ele` tag or another named authoritative height
  source; raster-sampled or interpolated summit heights are forbidden. A named
  peak without explicit height evidence remains named without a number.
- Route-profile elevations are a separate factual claim from summit heights.
  The visible profile must cite the exact source and method in the route record,
  plus its datum whenever the source declares one. A source-native embedded
  route profile remains truthfully datum-unspecified if its publisher does not
  make that claim. Source-native embedded route elevation wins when it exists;
  a frozen global raster is only the fallback. Raster/profile extrema without
  separate exact evidence must read `SAMPLED ELEVATION / APPROX`; a DEM sample
  must never supply the printed height of a named peak or pass.
- Terrain follows `hiking-factual-source-precedence-v1`. The detailed edition
  prefers source-native OS/IGN/CNIG terrain, except for a frozen and disclosed
  sparse-corridor/full-field fallback. The relief edition may select the frozen
  global bundle when its recorded level-and-length evidence wins. Each manifest
  records the selected terrain source for its own variant, whether native
  evidence was retained only for comparison, and whether a global route-profile
  fallback was retained; those declarations and the visible provider credit
  must match the geometry actually emitted.

Every geographic or route path has a `data-source-ref` that resolves to a
manifest source with an HTTPS URL, licence, attribution, use, retrieval date and
frozen snapshot evidence. ODbL context features also retain their OSM element
identity and canonical feature URL. The complete source list embedded in SVG
metadata must equal the plot manifest.

Context paths carry the explicit disclosures
`curated-source-sampled-art-context`, `generalized-not-for-navigation`, and
`artwork-not-for-navigation`. Label glyphs carry deterministic label IDs and
bounding boxes; inconsistent or overlapping label boxes fail. This makes visual
review reproducible without pretending that the artwork is a navigation map.

Every route has one open elevation band at the bottom of the map field. It has
no rectangle or `profile-frame`. The map and profile repeat the same A-E
stations at 0%, 25%, 50%, 75% and 100% of cumulative geodesic source chainage.
The map label, profile point and profile tick for each station carry the same
source-chainage record and elevation metadata. If a published route total is
available, visible station kilometres are proportional positions on that total
and the caption says `PUBLISHED KM`. `data-chainage-m` and `data-distance-km`
always remain the measured cumulative geodesic source chainage; the separate
`data-displayed-distance-m/km` fields carry only the proportional printed
scale. Without a published total, the caption
says `MEASURED KM`. This proves map/profile correspondence without falsely
claiming that printed published kilometres are exact cumulative measurements
of the stored simplified line.

Profile chainage, A-E stations and sampled extrema are computed from the full
source inventory before drawing. If the emitted physical polyline would place
vertices closer than the 0.25 mm pen width, a disclosed adaptive RDP tolerance
ladder generalizes only that emitted line, retains the exact global extrema and
must finish at an average vertex pitch of at least 0.25 mm.

## Variant contract

The `detailed-map` edition gives the selected roads, tracks, water, bounded
forest/grass and place names the fuller contextual treatment. When the source
provides at least four usable contour levels it must retain 4-8 evenly
distributed, continuous factual levels; a source with fewer levels keeps all
that it can render. Whole-path contour budgeting must also retain deterministic
spatial representatives across the map width, so disconnected eastern or
western terrain cannot be discarded merely because another component at the
same level is longer. It has no contour-altitude labels.

The `terrain-relief` edition must contain elevation-valued factual contours and
source-valued contour-altitude labels. Both editions use the same factual
fifth-interval hierarchy: every non-index contour is Grey 0.25, and each
positive contour that is an absolute multiple of five times the declared (or
robustly inferred) minor interval is a Grey 0.40 index. Zero metres is never
made heavy because it would imitate a shoreline; an arbitrary minor level is
never promoted when a selected stack contains no true index. A detailed map
may therefore omit Grey 0.40 legitimately. Relief altitude copy is restricted
to one through four rendered true-index levels; detailed maps have none. No
contour key or inset box is drawn.

The relief edition remains an adaptive, continuous contour stack across the map
field. Neither paired variant renders DEM fall-line hachures: the short
scratch-like marks that weakened the previous series are a hard failure even
when a frozen derivation contains them. The relief edition retains selected
water, bounded grass/woodland and enough road context to remain a geographic
map. Its route elevation profile stays open and unboxed at the bottom of the
main field.

`tools/audit_hiking_composition.py` measures the emitted paths rather than
trusting catalogue counts. It fails on any inset/profile-frame/fall-line role,
an incomplete A-E station set, a profile escaping its open 13.8 mm bottom band,
or contour levels that do not reconcile with source evidence. It also applies
the visual density bands established by the approved controls: 0.075-0.180
mm/mm² for the West Highland Way/Great Glen context grammar and 0.160-0.350
mm/mm² for the Tour des Refuges relief grammar. Minimum-density gates use raw
centreline length so a thicker index nib cannot disguise missing geography.
Maximum-density gates use Grey-0.25-equivalent physical length, weighting every
Grey 0.40 index by `0.40 / 0.25 = 1.6`, so the wider nib cannot silently exceed
the practical pen load.

## Plot contract

The physical pen sequence is one contiguous visit each to Grey 0.25, optional
Grey 0.40, Blue 0.25, optional source-backed Green 0.25, Black 0.25, Black 0.60
and Red 0.40. Grey 0.40 is emitted immediately after Grey 0.25 only when the
selected contour inventory contains a genuine fifth-interval index, so an
artwork has at most seven pen loads. The same pen identities and order apply to
both variants. The red hero route is the final logical and physical pen load,
and every split job must be an exact geometry-and-metadata copy of its
corresponding master layer.

Visible artwork is path-only: live text, raster images, reused symbols, circles,
ellipses, rectangles, lines, polylines, polygons and foreign objects are
rejected. The binding validator additionally enforces absolute `M/L/C/Z`
commands, one SVG unit per millimetre, real pen metadata, the eight-nib type
floor, the three-nib stroke floor, safe clipping and non-empty contiguous
layers. Plot simulation must remain at or below a `2.0` pen-up/pen-down travel
ratio. No scale bar is drawn.

Ink coverage is recorded in the report as an advisory measurement only. It is
never an acceptance failure and must never be used by this gate to cull map
features or labels.

## Attribution, review and commercial boundary

OpenStreetMap-derived artwork needs a visible path-based OpenStreetMap credit;
SVG metadata alone is not sufficient for the printed work. The plotted copy
must name `OPENSTREETMAP` and print the exact human-readable URL
`OPENSTREETMAP.ORG/COPYRIGHT`. The approved compact A5 form is
`© OPENSTREETMAP / OPENSTREETMAP.ORG/COPYRIGHT`; a missing, hidden or mistyped
URL is a hard release failure. It remains at or above the 2 mm/eight-nib type
floor. Licence Ouverte, CC BY and other sources retain their source-specific
attribution wherever their terms require it, with the full source records
preserved in the manifest.

Visible provider claims must also match the terrain actually selected for the
variant. A source retained only as comparison/provenance evidence must say that
it is not emitted; it may not be presented on the plate as the rendered relief
provider. The source register remains complete even when such a non-emitted
provider is correctly omitted from the face credit.

The global terrain pass uses AWS-hosted Mapzen Terrarium data recorded as mixed
source terms with location-specific underlying-provider attribution review
required. AWS Open Data hosting is not itself blanket commercial permission.
Automated success therefore does not mean that a route is cleared for sale.
Resolve and document the provider obligations for every route before any
commercial edition.

The automated gate also does not replace full-size visual review or a
calibration plot. Inspect both 40-route contact sheets, then review all 80 PNGs
and all 80 SVGs at true A5 size for complete route bounds, legible geography,
honest water/green/road context, distinct map-versus-relief hierarchy, mountain
symbols and altitudes, a page-up north mark, attribution legibility, red-route
dominance and practical plotting. The manifests remain `review-only` until the
rights review, exact pens, paper stock, speed, machine settings and a physical
proof plot are signed off.
