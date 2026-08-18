# Codex handoff: reproduce and extend the map system

This repository is the canonical plotting-code repository. It is not the shop
website and it does not require committed finished maps. A Codex session working
on map generation must read this file and
`docs/reproducibility/REPRODUCING_MAPS.md` before changing or running the
renderer.

## Non-negotiable map-quality contract

- Generic map exports default to `faithful`: full qualifying roads, service
  roads, paths, railways, water and parks from the acquired source; joined
  centreline topology; 0.04 mm paper-space error.
- The reviewed university visual explicitly uses `plotter-faithful`, also at
  0.04 mm with centreline roads. It starts from the same complete cartographic
  selection and removes only residual fragments that are physically shorter
  than the real nib can reproduce.
- For generic city and university exports, never substitute the selective
  `plot` detail profile, adaptive detail, a smaller layer list, or a new style
  merely to make an export quicker. Transit context has a separate,
  network-scale policy and defaults to `mapplot-transit context --profile
  house`: the university/marathon palette and complete supported context at
  compact/ordinary metropolitan scale, with declared class-level scale gates
  only where a regional or national sheet cannot physically separate them.
  `--profile plot` is legacy low-ink compatibility, not the customer-map
  default.
- Never infer line widths from the preview. Use the versioned physical pen
  inventory and plate nib ladder.
- A repeatable map requires both code/settings and pinned source bytes. A live
  OpenStreetMap query creates a new edition even when the command is unchanged.

## First checks in every fresh clone

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements/dev-2026-08-05.txt
.venv/bin/python -m pip install -e .
.venv/bin/python tools/check_map_reproducibility.py
.venv/bin/python -m pytest -q
```

The checker is offline and read-only. It verifies the frozen university
renderer, recipe, style, vector font, format, ranked catalog, all 50 map
responses, hashes, and geometry-library versions. The transit release has its
own strict environment check. Its reviewed environment is CPython 3.13.9,
Shapely 2.1.2, GEOS 3.13.1, and jsonschema 4.26.0; do not use `--no-deps` with a
requirements file that does not pin jsonschema.

For visual framing of a new composition, the same renderer is also available
through `mapplot-web`. Its auto-detail mode never selects the detail-thinning
`plot` profile. Supply a saved JSON path in the UI before treating the result as
repeatable; the browser viewfinder itself is not a source contract.

## Recreate the reviewed university system

Use the builder; do not reconstruct its arguments manually:

```bash
.venv/bin/python tools/build_ranked_university_series.py \
  --output-dir review-output/university-memorabilia-ranked-2026-v2.1.4
```

It renders the 30 UK and 20 US subjects from exact per-subject saved JSON with
no Nominatim or Overpass request. The machine-readable definition is
`contracts/university-memorabilia-v2.1/render-recipe-v2.1.4.json`; the source
index is
`contracts/university-memorabilia-v2.1/source-snapshots/source-manifest.json`.

## Generate a new city or university in the same house style

For the first acquisition, use a deliberate centre and save the exact cache
file reported by the manifest. Then rerun with `--input-json`; that saved file,
its SHA-256, the centre/radius, and the command together define the new edition.

```bash
.venv/bin/mapplot export \
  --center LATITUDE LONGITUDE \
  --radius-km 2 \
  --preset a5-balanced-poster \
  --poster-layout university-memorabilia \
  --layers roads,water,railways,parks,buildings \
  --style styles/university-memorabilia-v2.json \
  --water-fill dots \
  --landmark-buildings \
  --detail-profile plotter-faithful \
  --simplify-mm 0.04 \
  --road-style centreline \
  --extent-fit contain \
  --pen-profile actual-pens \
  --no-scale-bar --no-scale-detail \
  --optimise --physical-audit --split-by-pen --frame \
  --title 'CITY NAME' \
  --attribution-mode external \
  --external-attribution-placement \
    'Accompanying product page, packaging, and series attribution file' \
  --output review-output/new-edition/city-name.svg
```

On the repeat run, add:

```text
--input-json contracts/<new-edition>/sources/city-name.json.gz
```

For a subject batch, create a schema-version-1 source manifest matching the
committed university example, then use
`mapplot catalog export --source-manifest PATH`. The batch validates every file
and injects its exact `--input-json`; missing or changed source bytes fail closed
and cannot fall back to the network.

## Reproduce or extend a rail/transit network

Read `docs/transit/README.md`, `docs/transit/SOURCE_CONTRACT.md`,
`docs/transit/COLOR_AND_PENS.md`, and `docs/transit/QA.md` first. For the
national physical plate also read
`docs/transit/GB_PHYSICAL_RAIL_LOCK_2026-08-07.md`; it names the latest audited
house output, exact source/recipe/artifact hashes, and superseded generations.
For customer-facing urban transit, also read
`docs/transit/HOUSE_TRANSIT_RELEASE_2026-08-07.md`. For the current dated
25-product Great Britain passenger-operator series and all-operator overview,
read `docs/transit/GB_PASSENGER_OPERATOR_MAPS_2026-08-08.md`; it freezes the
registry, source hashes, scale tiers, context recipe, and rebuild commands.
A “25-product” label here means a 24 + 1 evidence split: 24 unchanged OSM
operator-relation review proofs plus Hull Trains' separately sealed advertised
scale-aware cartographic corridor. Northern Ireland Railways and Northern
Ireland geometry are excluded; never call this a United Kingdom roster or an
official operational map.
A named passenger service is a separate sourced graph; never colour the generic
`railways` background and call it an operator route. Physical track can be
freight-only, a depot lead, disused, or served by another operator.

Five urban compilers are enabled: London Underground, Tyne and Wear Metro,
Glasgow Subway, Sheffield Supertram, and Manchester Metrolink. LNER, GWR,
Southern, and Northern have a hash-pinned public Network Rail WTT parser,
NaPTAN candidate generator, exact-node OSM rail graph, and fail-closed compiler.
They remain release-gated until the generated coordinate bindings, GW/SN
service classifications, and exact consecutive-edge selections receive human
review. Explicit OSM operator-tag snapshots may be generated only when their
artwork and ledger say that exact weaker claim; they are not WTT-compiled
operator maps. Never trace an operator diagram or use proximity to bypass the
gate.

Named GB operator maps must pass both the pinned Zoomstack MBTiles and the
pinned Great Britain OSM PBF to the enrichment tool. Zoomstack supplies quiet
roads, water, boundaries, and non-routable physical rail only. The country
outline must come from exact authored OSM `natural=coastline` ways; inferred
Zoomstack sea-fill boundaries are forbidden. The 2026-08-07 PBF invariant is
19,641 positive land-left cycles / 28,172 ways before the complete-GB bounds,
and 19,640 cycles / 28,171 ways after excluding exactly Rockall way
`1339962684`. The loader runs once per batch, then every contract retains
source-object lineage and audited 0.04 mm paper-space deviation.

The sealed standalone Hull route-only contract SHA-256 is
`78fd1181f0986643b2783a21e9e92dc1bf6a715f3bf4141d86178c3b14ab1368`;
its audit file is
`ba030f2ed25083e0431b5f3f6a4ee77bd4823b9e5ac4b50c018196f0a8b1c66e`
with canonical evidence
`353e04bb9f7fefb819c230926d7e072034f3192c5f1963008e29f66b7c3aa032`.
The 25-line mixed route-only contract is
`21f8bb33681b58e0494b8f9a49bb08563cff3d327c8f313304fbfdcb121a5564`;
its audit file is
`62b0ce73f903be1a84bbf212ccc8c14d1a6b3b2f0dcc5c54c8236590cc0cebfd`,
canonical evidence is
`64a934421224f99eb97484ba1814831ae7de96f0366e3ac4280d1207e9398b12`,
and its index is
`017ee7b3f5988606fce1f813753e87406ab4372644f453649410301ae241307b`.
Keep those sealed route-only results immutable. Their scale-aware house outputs
now live separately: Hull under the ordinary batch's `supplemental-hull/`
directory and the 25-product overview under
`review-output/transit-gb-passenger-operators-mixed-overview-house-v3-2026-08-08/`.
The ordinary batch itself is finalized at
`review-output/transit-gb-passenger-operators-house-scale-aware-v3-2026-08-08/`
with index SHA-256 `f23cae0fa3c15d71adf17b58967f77566e3915f715ae815791bb0468095025da`.
Its standalone Hull `supplemental-hull/index.json` is `506b3cfd…` and validates
the eight route/context/render/QA bindings without modifying renderer artifacts.

The mixed context contract `94a43165…` is normalized only for physical credit
copy by `tools/compact_mixed_overview_internal_attribution.py`, with that exact
input hash required. The resulting presentation contract is `bd5a2427…` and
its audit file is `8584fa7e…`. Eight first-party attributions share `City Map
Plotter evidence`; three records bound to the identical pinned Geofabrik PBF
share one complete OSM/copyright/database/ODbL notice. All 14 source bindings
and all route/context geometry remain present; Hull Trains, NaPTAN, and OS
records remain exact. The completed mixed SVG, PNG and manifest hashes are
`15877573…`, `31676a9e…`, and `a2a7bd4a…`. The final paired QA passes with
digest `9c1b3468…`; the 11-file mixed house index is `dcab7af6…` and revalidates
every indexed path, byte count, and SHA-256. The passing audit does not change
the review-only/non-production status. Never reuse the legacy 24-line overview
or ordinary 24-product house-render digests for those mixed-evidence artifacts.

Enabled is not the same as physically accepted. The reviewed Sheffield
candidate has 51 stations, 244 edges, eight patterns, 413/413 edge memberships,
zero declared breaks, and zero sub-floor trails. Its sole `1.005646 m`
connector is restricted to one catalog-pinned ordered consecutive-way pair and
its evidence ledger. London remains review-only: its generic route-relation
candidate has 22 declared source-relation gap occurrences and three sub-floor
trails after length-aware exact-node assembly. Do not add proximity joins or
edit the SVG. London needs a dedicated TfL Route Sequence plus exact-node OSM
representative-corridor compiler with station-to-station selection evidence.

### Current Great Britain physical-rail plate

Use only
`review-output/transit-v2-house/great-britain-physical-railways-house-v2/` as
the current local review generation; the sibling directories
`great-britain-physical-railways/` and
`great-britain-physical-railways-v2/` are superseded. The lock document records
exact `113,129` source/SVG feature parity, source SHA-256
`2ae12b1baa7f582c37a02b189a52865e09af9a4e162ccc61dc4e11d882047a0a`,
manifest hash
`6d85bfdb354061a5e219fc089f151c9df9f3ad312f6f141d90d20695beab5be7`,
SVG hash
`a7077fd4fc3d2e9db0b4fa0226966924997115c71b8c1196b3a0490031840ed6`,
PNG hash
`b3be96039737e74cd909c2962063ac24179f9daa34486aeacd250a46d6aa827d`,
zero invented connectors, and the path-portable explicit command. The current
source has zero Narrow Gauge features; Green 0.40 mm appears only in its legend.
All selected owned inventory entries remain `nominal-unmeasured`, so this is
review-only.

### Customer-facing transit map: attach house context first

This is the default workflow for artwork that a customer will see. Start from a
reviewed passenger-route contract, attach exact pinned geographic context with
the explicit `house` profile, then build the context-bearing contract. The
builder fails closed if context is absent. Do not add `--allow-route-only` here.

```bash
transit_network_id=glasgow-subway-2026
route_contract="contracts/transit-networks-v1/networks/${transit_network_id}.json"
house_contract="review-output/transit-v2-house/${transit_network_id}-house.contract.json"
house_output="review-output/transit-v2-house/${transit_network_id}-house"

.venv/bin/mapplot-transit context \
  "$route_contract" \
  --overpass-file path/to/exact-pinned-context.json \
  --source-id osm-context-house-2026-08-07 \
  --source-url https://overpass-api.de/api/interpreter \
  --retrieved-at 2026-08-07 \
  --profile house \
  --output "$house_contract"

.venv/bin/mapplot-transit build \
  "$house_contract" \
  --output-dir "$house_output" \
  --station-labels key

.venv/bin/python tools/qa_transit_series.py \
  "$house_contract" \
  --manifest "$house_output/${transit_network_id}.plot.json" \
  --output "review-output/transit-v2-house/${transit_network_id}-house-qa.json"
```

The pinned context file is part of the edition. Save and hash it; a live
Overpass request made later is different source data even if the query text is
unchanged. Use one or more hash-verified `.osm.pbf` files instead of
`--overpass-file` for extents above the live-query ceiling.

### Diagnostic route-only release lock

The tracked v1 lock deliberately verifies normalized service graphs without a
basemap. `--allow-route-only` is permitted only in this diagnostic loop. Its
SVGs are topology evidence, not customer artwork, not a gallery choice, and not
the default transit renderer result.

```bash
# Validate normalized inputs, the renderer source bundle, recipe, and exact
# reviewed geometry environment. Rendered bytes are not yet checked.
.venv/bin/python tools/validate_transit_release_lock.py --strict-environment

for transit_network_id in \
  glasgow-subway-2026 \
  tyne-wear-metro-2026 \
  sheffield-supertram-2026 \
  manchester-metrolink-2026 \
  london-underground-2026
do
  .venv/bin/mapplot-transit build \
    "contracts/transit-networks-v1/networks/${transit_network_id}.json" \
    --output-dir "review-output/transit-v1/locked-final/${transit_network_id}" \
    --station-labels key --allow-route-only --no-png \
    --generated-at 2026-08-07T12:00:00+00:00
done

# Release-lock v2 now verifies each master, the exact split-pen inventory, and
# the aggregate per-pen digest under this output root.
.venv/bin/python tools/validate_transit_release_lock.py \
  --strict-environment \
  --artifacts-root review-output/transit-v1/locked-final

# The fixed, timezone-aware generated-at value above is part of the recipe;
# do not replace it with the current time for a locked reproduction.
```

### Maintenance acquisition

Acquisition updates a service contract from current sources; it is not required
for a repeat build from an already frozen contract. After review, pass the new
contract through the customer house-context workflow above.

```bash
transit_network_id=glasgow-subway-2026
.venv/bin/mapplot-transit list
.venv/bin/mapplot-transit catalog "${transit_network_id}"
.venv/bin/mapplot-transit acquire "${transit_network_id}" \
  --user-agent "CityMapPlotter/1.0 contact@example.com" \
  --cache-dir review-output/transit-v1/source-cache \
  --output "review-output/transit-v1/contracts/${transit_network_id}.json"
```

Acquisition is a maintenance action. Preserve the exact cache bytes, SHA-256,
retrieval/validity dates, service date, source role, licence, and attribution.
Rendering must use the frozen normalized contract without network fallback.
Attach context from a separately pinned Overpass JSON or `.osm.pbf`; it never
repairs a missing service edge. Manchester uses `transit_tfgm.py`, not the OSM
compiler, and must keep the adjacent `.audit.json` ledger beside its contract.
Validate the Manchester contract and adjacent TfGM audit sidecar independently
with the command below.

```bash
.venv/bin/python tools/qa_tfgm_audit.py \
  path/to/manchester-metrolink-2026.json \
  path/to/manchester-metrolink-2026.audit.json \
  --catalog-network-id manchester-metrolink-2026
```

Use the default `--profile house` for the customer-map product and `--profile
detail` only for an explicitly reviewed archival-detail variant. `house` keeps
the university/marathon physical palette and all supported context classes for
compact and ordinary metropolitan extents. A very large metropolitan sheet
drops paths, special-purpose other-road classes and road-area clutter;
regional/national sheets retain only the hierarchy that remains distinguishable
at their physical scale. Within every enabled road/path/rail class, every
source-backed fragment reaches exact-endpoint topology assembly. Compact and
urban sheets retain non-degenerate sub-three-nib transport fragments with an
explicit review flag. Regional and national sheets then use the reviewed
university/marathon paper gate on each post-knockout serialized piece:
`max(0.5 mm, 3 × actual nib width)`, plus `(2 × nib width)^2` for a valid closed
area. Every removal is recorded in the deterministic omission ledger; the
operator/service route itself is never thinned by this context rule.
`--profile plot` remains a deliberate legacy low-ink option. `detail` attaches the full
supported vocabulary, but does not bypass renderer scale whitelists, clipping,
or geometric route/station/label knockouts.

Named operator products use exactly four computed tiers: compact through
`1:75,000`, urban over `1:75,000` through `1:250,000`, regional over
`1:250,000` through `1:750,000`, and national above `1:750,000`. Compact keeps
the complete house context; urban keeps strategic, major and secondary roads,
water lines and areas, coastline, green space and boundaries; regional keeps
strategic roads, water lines and areas, coastline and boundaries; national
keeps only coastline and boundaries. Generic physical rail is absent beneath a
named operator route. The service graph, route edges, patterns and memberships
are invariant across all four tiers; scale changes context and physical
presentation, not route evidence.

Valid closed sourced water-area rings retain their Blue 0.40 mm bank and gain
deterministic closed-dot stipple. Contained inner rings remain holes; open,
clipped, invalid, and unassigned inner rings remain outlines only. Linear water
is suppressed only inside a valid sourced surface. Unsized line-only water
remains a plain Blue 0.25 mm path. Water clears routes, stations, and labels by
geometry, never by plotting white.

The tracked release lock reproduces the route-only normalized inputs and, in
schema v2, verifies regenerated route-only SVGs when `--artifacts-root` is
supplied. Context contracts are intentionally not committed. A live Overpass
rerun is a new candidate, not an historical reproduction; exact context
reproduction requires the original hashed extract and a validated external
source pack. The tracked declaration is
`contracts/transit-networks-v1/context-source-pack-v1.json`, validated by
`context-source-pack-schema-v1.json`. It binds the route-contract hash, context
profile, bbox, raw compressed/uncompressed byte counts and hashes, source
identity/URI/date, exact query settings/hash, licence/attribution, and normalized
context-contract hash. Validate its metadata in a clean clone, then validate the
ten external files when the pack is mounted:

```bash
.venv/bin/python tools/validate_transit_context_source_pack.py
.venv/bin/python tools/validate_transit_context_source_pack.py \
  --source-pack-root review-output/transit-v1
```

The first command deliberately reports `source_pack_verified: false`; only the
second proves historical context bytes. Until those external payloads accompany
a release, the five plot contexts remain local review candidates rather than
locked route-only inputs.

`tools/qa_transit_series.py` is strict by default: declared gaps and route
trails shorter than three effective nib widths fail physical acceptance. Use
`--structural-only` only for a known unfinished source candidate, such as the
current London relation inventory. Structural mode still checks schema,
catalog/source/snapshot binding, terminal and station gates, edge parity,
station association, manifest binding, and readiness consistency. A structural
pass is not permission to plot or sell the artifact.

The geographic-skeleton ceiling also remains globally fixed at 3% for regional
and national sheets. The one ScotRail release reconciliation is a post-audit,
hash-bound exception for contract `4b9532b6…`, manifest `5d337753…`, and SVG
`adeb0803…`. Its coastline-plus-boundary coverage is `3.3269957693471484%`,
below the separate 3.5% exception ceiling, with no non-skeleton context. Keep
the raw hierarchy `passed: false`, its exact 3% limit and original finding in
the QA report. The reconciliation changes neither renderer bytes nor the global
rule and never promotes the artifact beyond `review-only` /
`production_ready: false`. The finalized ScotRail QA file is `ad2cb6d6…`.

The renderer does not delete a sourced passenger-route fragment merely because
it is below that handling floor. `transit-source-backed-subfloor-route-v1`
emits every route fragment longer than `0.000001 mm`, marks a sub-three-nib path
as review-required, and keeps it as a production blocker. Route parity is the
union of `data-transit-edge-ids` on the actual physical SVG paths, not the
planner's intended membership count; a missing or unexpected emitted edge makes
the render fail closed.

Normal named routes target `1.0 mm` at compact scale and `0.8 mm` at urban,
regional and national scale. A normal `0.8 mm` band is three owned `0.4 mm`
passes at `0.2 mm` pitch and offsets `-0.2`, `0`, and `+0.2 mm`; a compact
coloured `1.0 mm` band is four owned `0.4 mm` passes at the same pitch. The
all-operator overview is exactly one owned `0.4 mm` pass per product.

The sole native-width exception is bound to the dated Merseyrail network
`merseyrail-osm-explicit-operator-tag-2026-08-06`, line
`merseyrail-osm-explicit-operator-tag-snapshot`, product `merseyrail-2026` / key
`ME`, at automatic urban scale. Its baseline remains `0.8 mm`, but owned Gold
exists only as `gold-1`, so the individual sheet resolves to one native
`1.0 mm` pass. It remains `nominal-unmeasured`; there is no generic widening,
unowned nib synthesis or colour substitution, and the exception never applies
to the `0.4 mm` overview.

For a sub-three-nib multi-pass route, GEOS offsets are not trusted. The whole
centreline is rigidly translated at every planned offset using the endpoint-
chord normal (or the longest non-zero segment for a closed/coincident chord).
This guarantees distinct physical passes without a same-centre retrace, but on
a bent/hairpin fragment its `1.0 mm` claim is nominal/straight-run width rather
than an exact local perpendicular union. Keep the explicit review flag and do
not promote such an artifact to production-ready.

Never infer exact route width from `physical_union_width_mm` alone. Its required
scope is `straight-locally-parallel-runs`. A standard record is accepted only
when every planned stroke index is one continuous pen-down path and the 25%,
50%, and 75% normal-offset samples of every source segment are covered within
the in-memory tolerance. The independent SVG audit rebuilds those records and
allows only the separately declared `0.001 mm` coordinate-quantization budget.

If an open non-short GEOS offset is empty, multipart, or nonempty but partially
collapsed, use `source-segment-normal-smooth-join-review-offset`. It retains
every shifted source segment end-to-end, connects outside turns with tangent
circular arcs and inside turns with tangent-matched sampled cubics, retains
self-overlap, and concatenates one path per stroke index. It must carry
`nominal-review-required-not-exactly-certified`, review-required true, and
`exact_local_union_certified=false`.

A simple valid closed source cycle may use the same fallback. It must include
the final-to-first join, emit exactly one explicitly closed continuous path per
stroke index, pass shifted-segment coverage, and stay within
`abs(offset) * 5 / 3 + 0.000000001 mm` of the source. It records
`certified-closed-one-continuous-path-per-stroke-index` but remains nominal and
review-required, with `exact_local_union_certified=false`. Self-intersecting,
invalid, degenerate or zero-area cycles still fail closed.

`paper-corridor-v1` runs the unchanged membership-carrying reducer to a bounded
fixed point when a first pass leaves a sustained duplicate. Every retry must
strictly reduce the unresolved-run count and either path count or pen-down
distance; the final gate recomputes membership parity and deviation against the
original source records. Never accept an unresolved pass or remove provenance
to force convergence.

Reference RGB and physical ink are separate. Never claim an official numeric
colour when the record says sampled, community, or house value, and never claim
that a matching pen exists from the SVG preview. Approximate, unresolved, and
nominal-unmeasured mappings remain review-only. Operator-reference rights,
required attribution, exact-stock pen calibration, registration, and a physical
bounds proof are independent production gates. Do not edit a generated SVG to
repair topology or omit an inconvenient branch; fix the source contract or
compiler and rebuild.

The renderer currently writes `production_ready: false`; its mode is
`review-only`. Every selected pen remains `nominal-unmeasured` and must be
tested on the exact stock at the intended speed. Rights/attribution,
display-to-physical colour collisions, registration, a plotted physical proof
and bounds inspection must each pass independently before any production claim.

## Marathon rule

The city basemap may use the same high-detail rendering, but do not call it a
marathon course map without official GPX/KML or a verified route relation that
passes the course-length and provenance gates. Most catalog marathon entries
are intentionally labelled city basemap previews.

## F1 circuit atlas v2.3 handoff

The frozen amended 2026 catalog retains Madrid as a geometry hold. It binds the
amended-WMSC ledger plus the announced Sepang replacement, which remains
pending approval; called-off Sakhir and Jeddah remain excluded. Current catalog
SHA-256:
`b117b4a2f0b40277417fd255d80f80b7dcc936e97f614e56e7095bdf7179746e`.
An official event page does not clear Madrid's independent geometry failure, so
Madrid must not emit a plate.

The v2.3 visual contract uses Green source-derived outlines for grass, parks,
and woodland. Never restore dotted grass, grass stipple, hatching, or repeated
interior grass symbols. Water is deliberately differentiated with closed Blue
dots clipped to sourced water geometry, plus a sourced shoreline outline where
it survives clipping and the density gate. Include useful roads, access roads,
buildings, stands, pit/paddock structures, runoff, gravel, vegetation, and
water, but keep deterministic selection below the 0.17 design target and 0.18
hard field-density gate. Rich context is not an exhaustive inventory and must
not saturate the paper.

Current plates may print only the frozen official Formula 1 page's circuit
length, venue-scoped `First Grand Prix`, and exact `Fastest lap time` with
driver and season. Keep the Formula 1 field scope: do not relabel that value as
an independently researched all-time record. Missing, placeholder, malformed,
partial, or conflicting facts stay withheld. Never fill them from memory,
qualifying, an unbound results page, or a different circuit configuration.

The curated former-F1 catalog remains a conservative multi-era collection.
Renderable legacy plates show their bound length and explicit `F1 REFERENCE`
season; fastest lap remains withheld. Current-source legacy context remains
current and must not be presented as a period reconstruction. A historic or
modern lap time must not be published as a configuration record without a new
frozen configuration-matched source contract.

The renderer keeps one exact closed Red source centreline, clips only derived
paired offsets where independently detected non-adjacent course legs would
merge on paper, uses a Black bridge bracket only for source-proven grade
separation, and caps ordinary label leaders. Do not relabel `Gxx` geometric
stations as apexes or official turns. Unsupported famous-section names remain
omitted. Current OSM grandstand observations are not a race-weekend
seating/configuration source.

The promoted v2.3 technical-review packages are
`review-output/f1-circuit-atlas-2026-v2.3-green-outline-water-facts` and
`review-output/f1-circuit-atlas-legacy-v2.3-green-outline-water-facts`. The
current package has 132 plates, 995 one-pen SVG jobs, six contact sheets, and
1,407 replayed checksums; the legacy package has 114 plates, 856 one-pen SVG
jobs, six contact sheets, and 1,214 replayed checksums. Independent semantic
and format QA pass all 246 plates with zero failures, and the dedicated F1
regression matrix passes 277/277 tests. Earlier v1.2/v2.2 directories are
historical only. Both promoted packages remain review-only until
artifact-specific rights clearance and physical plotted-proof holds clear. Do
not remove the plotted OpenStreetMap attribution from ODbL-derived plates.

## What belongs in Git

Commit renderer code, styles, catalogs, source contracts, licences/notices,
recipes, tests, and documentation. Keep generated SVG, PNG, pen-split jobs,
reports, caches, `output/`, and `review-output/` out of Git. A generated image
is evidence to inspect locally, not a substitute for the reproducible inputs.

## Suggested prompt for another Codex session

> Work only in the city-map-plotter repository. Read CODEX_MAP_HANDOFF.md and
> docs/reproducibility/REPRODUCING_MAPS.md first. Preserve the faithful
> full-road fidelity invariant and the v2.1.4 university recipe. Pin every new
> source input and its hash; do not use live data for a claimed repeat build;
> do not commit generated maps. Run the reproducibility checker, Ruff, mypy,
> and pytest before proposing a push.
