# City Map Plotter

City Map Plotter turns OpenStreetMap vector data into an exact-size SVG for pen plotting and Inkscape editing. It reads source vectors rather than tracing a screenshot, so roads originate from mapped centre-lines and rivers, railways, parks, and buildings stay independently editable.

The exporter now provides:

- place, center/radius, or explicit bounding-box input;
- cached OpenStreetMap acquisition through Overpass, saved JSON, or an optional local `.osm.pbf` reader;
- road, path, water, park, railway, building, and boundary classification;
- A-series, Letter, Legal, or custom paper sizes in millimetres;
- portrait, landscape, or automatic orientation;
- baked clipping and physical-output simplification;
- named Inkscape layers and editable feature metadata;
- numeric ink, nib, parallel-stroke, and repeat-pass settings that compile to drawable paths;
- path ordering and safe reversal to reduce pen-up travel, with before/after reporting;
- a JSON manifest with physical pen order, distances, lift counts, and timing estimates;
- optional page-sized SVGs split by physical pen;
- embedded OpenStreetMap attribution by default, with an explicit recorded
  external-placement mode for clean artwork;
- resumable, hash-verified PNG previews at a caller-selected positive DPI;
- A5 clean- and balanced-poster presets with a safe outer border, title and city-detail zones;
- an **opt-in** `ink-balanced` profile for callers who explicitly want a plate
  held to the coverage figure: it removes exactly coincident water-boundary ink,
  reserves a visible sample of every requested feature family, and selects the
  remaining road/water/rail/context geometry by an auditable semantic and
  spatial policy. It is not a default — outside this profile, ink coverage is
  measured and reported as an advisory and never culls anything;
- road-network filtering and merging for small-format plotting;
- banked-river centreline suppression, bridge knockouts, and restrained Bézier smoothing.

Plotter Studio now animates the actual ordered motion and predicts a calibrated
time range. The separate, fail-closed Plotter Control tool compiles the same
motion to a hash-bound job, pen-up bounds proof, and per-pen GRBL programs; live
serial execution requires an exact job digest and a hardware-verified device
profile. Nominal inventories still produce an explicit `review-only` SVG and
cannot silently cross that gate. See
[`docs/plotter/PLOTTER_SOFTWARE.md`](docs/plotter/PLOTTER_SOFTWARE.md).

The core is a vector-first map compiler:

```text
place / center / bbox → Overpass, saved JSON, or local PBF vectors → tag classification
→ role-aware areas + local projection → topology-aware simplification + exact clipping
→ optional verified A5 ink-budget selection → nib/offset/pass compilation
→ travel ordering
→ millimetre page layout → Inkscape SVG + ordered pen manifest
```

## Repository scope

This repository contains the renderer, command-line tools, tests, styles,
portable examples, and the pinned source contracts needed to reproduce the
York-derived university series, standalone architecture catalog, and the five
reviewed urban-transit route-only candidates. In particular,
`contracts/university-memorabilia-v2.1/` preserves the audited base renderer,
exact v2.1.4 correctness/source-pinning overrides, and all 50 dated
subject-specific map responses. `contracts/transit-networks-v1/networks/`
preserves the five normalized passenger-service graphs and Manchester's audit
ledger. The transit raw acquisition caches and large context snapshots remain
external release inputs; the tracked normalized graphs do not make those
historical source bytes recoverable from a live service.

Generated maps, previews, general service caches, and release directories are
local artifacts and are intentionally excluded from Git. Deliberately tracked
reproducibility inputs include the ODbL-noticed university source snapshots,
the five normalized transit route contracts and Manchester ledger, and the
transit context-pack metadata declaration. The ten large context payload files
remain external. These are input/evidence contracts, not generated gallery
output. Commands below may write to `output/` or `review-output/`; create those
outputs after cloning rather than expecting them to be present in the
repository.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

To recreate the reviewed high-detail university maps rather than generating a
new edition from live data, follow
[`docs/reproducibility/REPRODUCING_MAPS.md`](docs/reproducibility/REPRODUCING_MAPS.md).
It pins the Python geometry environment, renderer, style, font, catalog, and
all 50 source responses, and provides a read-only reproducibility check before
rendering.

For an agent-assisted handoff, start with
[`CODEX_MAP_HANDOFF.md`](CODEX_MAP_HANDOFF.md). It tells Codex which fidelity
rules are non-negotiable, how to pin a new city's source bytes, which exact
university command to use, and which generated artifacts must stay out of Git.

## Rail and transit network plates

Passenger-service routes use their own sourced graph. They are never inferred
from the ordinary `railways` basemap layer: that layer proves mapped physical
track context, not that a named operator or line currently serves it. London
Underground, Tyne and Wear Metro, Glasgow Subway, Sheffield Supertram, and
Manchester Metrolink have enabled acquisition compilers. The dated Great
Britain product system now covers 24 current domestic passenger-service
groupings plus Eurostar's British section; see
[`docs/transit/GB_PASSENGER_OPERATOR_MAPS_2026-08-08.md`](docs/transit/GB_PASSENGER_OPERATOR_MAPS_2026-08-08.md).
It includes a hash-pinned Network Rail WTT/NaPTAN/exact-rail-graph compiler and
non-approving review-pack generator. The exact operational Hull gate remains
closed until the generated coordinate bindings, service classifications, and
exact edge selections have been reviewed; the compiler never turns a timing
point list or nearby track into an invented route. A separate, sealed Hull
advertised cartographic corridor now supplies the non-operational `HT*` line in
the route-only mixed overview.

Route thickness defaults are physical: compact networks target `1.0 mm`,
urban/regional/national named-operator maps target `0.8 mm` (normally three
owned `0.4 mm` passes at `0.2 mm` pitch), and the shared all-operator overview
uses one `0.4 mm` pass per line.
The union-width claim is exact only on `straight-locally-parallel-runs`.
Every planned pass must be one continuous pen-down path and cover deterministic
shifted samples of every source segment. Empty, multipart, or partially
collapsed open offsets use a complete-segment, tangent-matched smooth fallback
that is machine-marked nominal/review-required; a closed fallback fails closed.
This prevents a nonempty but truncated geometry result from silently removing
the fine route detail carried by its source membership.

### Dated Great Britain mixed-evidence route proof

The sealed 2026-08-08 route-only result contains 24 unchanged OSM
operator-relation review proofs plus one separately hashed Hull Trains
advertised scale-aware cartographic corridor. `HT*` is not an OSM relation,
operational-track, platform, crossover-use or WTT-transition claim. Its ten
advertised stations remain in the standalone proof and are deliberately absent
from the national overview. The exact operational policy remains blocked.

The mixed contract has 25 lines, 369,339 unique route edges, zero station
markers, zero context and one owned 0.4 mm pass per product. Contract SHA-256 is
`21f8bb33681b58e0494b8f9a49bb08563cff3d327c8f313304fbfdcb121a5564`;
mixed audit file SHA-256 is
`62b0ce73f903be1a84bbf212ccc8c14d1a6b3b2f0dcc5c54c8236590cc0cebfd`;
its canonical evidence digest is
`64a934421224f99eb97484ba1814831ae7de96f0366e3ac4280d1207e9398b12`.
Use the exact six-hash command and claim boundary in the
[dated GB operator workflow](docs/transit/GB_PASSENGER_OPERATOR_MAPS_2026-08-08.md).
That immutable mixed release is route-only and contains no context or render
artifacts. Its separately bound scale-aware house derivative lives at
`review-output/transit-gb-passenger-operators-mixed-overview-house-v3-2026-08-08/`;
the dated workflow pins its context, presentation, SVG, PNG, manifest, and
paired-QA state without rewriting the sealed route evidence. Its final paired
QA passes (`9c1b3468…`) and its 11-file index is `dcab7af6…`; both retain the
explicit review-only, non-production status.

Customer artwork uses the university/marathon `house` context by default. The
reproducible workflow is an already reviewed route contract, exact pinned
context, offline rendering, then independent QA. Do this before considering the
separate route-only diagnostic lock:

```bash
transit_id=glasgow-subway-2026
route_contract="contracts/transit-networks-v1/networks/${transit_id}.json"
house_contract="review-output/transit-v2-house/${transit_id}-house.contract.json"
house_output="review-output/transit-v2-house/${transit_id}-house"

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
  --manifest "$house_output/${transit_id}.plot.json" \
  --output "review-output/transit-v2-house/${transit_id}-house-qa.json"
```

`mapplot-transit build` fails closed when pinned geographic context is absent.
`--allow-route-only` is an explicit diagnostic escape hatch, never the customer
default and never a gallery source. The tracked route-only byte lock is checked
separately:

```bash
# Verify normalized route contracts, renderer bindings, recipe, and geometry
# environment before building diagnostic route-only evidence.
.venv/bin/python tools/validate_transit_release_lock.py --strict-environment

for transit_id in \
  glasgow-subway-2026 \
  tyne-wear-metro-2026 \
  sheffield-supertram-2026 \
  manchester-metrolink-2026 \
  london-underground-2026
do
  .venv/bin/mapplot-transit build \
    "contracts/transit-networks-v1/networks/${transit_id}.json" \
    --output-dir "review-output/transit-v1/locked-final/${transit_id}" \
    --station-labels key --allow-route-only --no-png \
    --generated-at 2026-08-07T12:00:00+00:00
done

# Verify the regenerated master and exact per-pen SVG bytes against release lock v2.
.venv/bin/python tools/validate_transit_release_lock.py \
  --strict-environment \
  --artifacts-root review-output/transit-v1/locked-final
```

An explicit maintenance acquisition is required only when making a new service
edition. `.venv/bin/mapplot-transit acquire manchester-metrolink-2026` selects the
dedicated TfGM functional-geometry/dated-GTFS compiler and writes an adjacent
audit sidecar.
Acquisition caches and hashes the exact response bytes and records retrieval,
validity, service date, source use, licence, and attribution; rendering reads
the frozen contract and never refreshes it. Context is a separately hashed
Overpass JSON or `.osm.pbf` input. The five route-only release contracts and
Manchester audit ledger are deliberately tracked under
`contracts/transit-networks-v1/networks/` and bound by
`release-lock-v1.json`. The v2 lock binds a deterministic renderer source
bundle and the expected master/per-pen SVGs as well as the normalized inputs.
Its render recipe also pins the timezone-aware `generated_at` value shown in
the loop above; pass that exact value so timestamp-bearing build metadata is
repeatable rather than dependent on wall-clock time.
The validator without `--artifacts-root` checks the declarations but reports
that rendered bytes remain unverified; the post-build invocation above checks
the exact output bytes. Keep live caches, context contracts, SVGs, previews,
manifests, and pen jobs in `review-output/`: a live Overpass URL plus a hash can
detect drift but cannot recreate historical bytes. Exact context reproduction
requires the pinned extract in an external release asset. The tracked
`contracts/transit-networks-v1/context-source-pack-v1.json` declaration and its
schema bind all five raw snapshots and normalized context contracts. Run
`tools/validate_transit_context_source_pack.py` for clean-clone metadata checks;
add `--source-pack-root review-output/transit-v1` when those ten external files
are present to verify their exact bytes.

Context water uses the university-derived physical language without inventing
river width. Valid sourced water-area rings retain their banks and gain Blue
0.25 mm closed-dot stipple; contained inner rings remain holes. Open, clipped,
or invalid rings remain outlines only. A water centreline is suppressed only
inside a valid sourced polygon, while line-only waterways remain plain Blue
0.25 mm paths. Routes, stations, and labels clear the water by geometric
subtraction, never white ink. The manifest and independent QA bind those claims
to the emitted SVG.

Reference line RGB and physical ink are separate contracts. Published,
sampled, community, and house display colours retain their provenance; an
approximate, unresolved, or nominal-unmeasured pen mapping remains review-only.
Commercial output also fails on unresolved source rights or required
attribution. Production needs the exact pens, stock, speed, and ten-specimen
calibration plus a physical bounds/registration review.

See the [transit workflow](docs/transit/README.md), the pinned
[Great Britain physical-rail lock](docs/transit/GB_PHYSICAL_RAIL_LOCK_2026-08-07.md),
the current [house transit release](docs/transit/HOUSE_TRANSIT_RELEASE_2026-08-07.md),
[source rules](docs/transit/SOURCE_CONTRACT.md),
[colour and physical-pen rules](docs/transit/COLOR_AND_PENS.md),
[QA gates](docs/transit/QA.md),
[dated network audit](docs/transit/NETWORK_AUDIT_2026-08-06.md), and the
[network contract](contracts/transit-networks-v1/README.md).

## Standalone architecture studies

`mapplot-architecture` builds a separate A3 portrait collection of stadiums,
landmarks, buildings, and an in-house metric house concept. Each plate combines
a source-qualified plan with a policy-qualified vertical view, then writes a
master SVG, 300 dpi preview, manifest, and one SVG job per physical pen. A tagged
height does not by itself authorise an axonometric drawing:
`vertical_display_policy` can request a diagrammatic extrusion, retain only a
height reference, or gate the view on height-bearing footprint coverage.

```bash
mapplot-architecture list
mapplot-architecture build --all \
  --output-dir review-output/architecture-technical-v2.1 \
  --dpi 300

.venv/bin/python tools/qa_architecture_series.py \
  review-output/architecture-technical-v2.1 --dpi 300
```

OpenStreetMap footprint dimensions are visibly marked `APPROX`; explicit height
tags are marked `TAGGED / UNVERIFIED`. OSM plans say `FIT TO FIELD` and do not
print a numeric scale; the denominator remains machine-readable for
reproducibility. The authored house concept uses its exact metric model at a
printed standard 1:100 scale and remains labelled `NOT AS-BUILT`.

For `coverage-gated` source views, at least 80% height coverage permits the normal
diagrammatic extrusion, 30–<80% produces a partial extrusion with the unheighted
plan retained flat, and below 30% suppresses massing. Roof-identity suppression
uses `height-reference-only`: The O2, Buckingham Palace, and
St Paul's Cathedral have plans and tagged-height references but no massing;
Edinburgh Castle is also suppressed because its tagged components cover only
0.62% of the envelope. The renderer also suppresses axonometric ribs closer than
0.8 mm, draws only front-facing tagged base edges, and deduplicates coincident
same-pen segments while retaining combined source provenance. It does not invent
facades, windows, seating, roofs, sections, or structural details.

The final series and QA gate pass are under
`review-output/architecture-technical-v2.1`. These remain source-derived
architectural studies, not surveys, construction drawings, or as-built records.
They are explicitly **review-only**: nominal pen widths are unmeasured until the
exact pens, paper stock, speed, and pressure have a dated calibration, and
commercial rights review is still required.

See the [architecture pipeline and evidence contract](docs/architecture/ARCHITECTURE.md),
the [v2.1 release audit](docs/audit-2026-08-03-architecture-v2.1.md),
and the [open-source backend audit](docs/research/architecture-backends-2026-08-03.md).
The generated artifact catalog and QA reports are written inside the local
release directory and are intentionally not committed.

## Bridge source-profile studies

`mapplot-bridges` builds A3 landscape, side-on technical studies. Finished
artwork must come from a pinned elevation drawing: published dimensions alone
do not establish cable sag, tower shape, arch geometry, hanger spacing, or
anchorages. Four source-derived profiles currently qualify for a normal build:
Brooklyn Bridge `hero-v3` and the Bay Bridge West Bay W2-W3 span from calibrated
Library of Congress HAER sheets, the Forth Bridge north main span from
Westhofen's 1890 engineering elevation, and the Iron Bridge from John Record's
1782 one-set rib elevation.

Each artwork view is clipped at source-derived focus components so its
recognition-rich span or rib uses the generated `bridge_drawing` zone instead
of shrinking complete approaches or repeated spans. Equal axes are retained;
nothing is vertically stretched. The published recognition dimension must use
at least 75% of that drawing zone, and projected trace uncertainty must remain
below half the 0.25 mm fine nib. A qualified source profile also has a
zero-omission physical invariant: every B0 hero path must survive its real pen's
three-nib floor, or the build fails instead of silently deleting detail.

```bash
mapplot-bridges list
mapplot-bridges build --all \
  --output-dir review-output/bridge-main-span-v6 --dpi 300

.venv/bin/python tools/qa_bridge_series.py \
  review-output/bridge-main-span-v6 --dpi 300
```

The catalog also retains five dimension-generated models as development
fixtures. They are marked `dimension-schematic`, excluded from a normal
`--all` build, and cannot pass release QA. `--include-schematics` makes them
available for explicit renderer development only. They are not substitutes for
the additional source elevations now in the sourcing/import queue. In
particular, the available Golden Gate elevation is too coarse at A3 and has no
recorded reuse licence; the exact Eads Bridge HAER drawing has not yet been
downloaded and byte-verified; and the authentic 1919 Quebec Bridge plate still
misses the half-nib error gate at its current scan resolution. None of those
three is promoted to source-profile artwork.

See the [bridge source/import/fidelity workflow](docs/bridges/BRIDGES.md) and
[bridge source contract](contracts/bridges-v1/README.md).

## Engineered-object technical plates

`mapplot-objects` prepares and renders evidence-bearing engineered-object
studies. It accepts local canonical vector geometry or cleaned visible-view
photo reconstructions, preserves evidence and rights metadata per primitive,
and fails closed when a requested three-view, exploded view, cutaway or
model-specific identity is not supported by the source. The built-in fictional
records are renderer test fixtures, not representations of real products and
not saleable collection geometry.

```bash
mapplot-objects list
mapplot-objects render \
  --catalog-file /path/to/source-qualified-technical-catalog.json \
  --all \
  --format-id a3-landscape \
  --output-dir review-output/source-qualified-collection \
  --png-dpi 300

python3 tools/validate_format.py \
  review-output/technical-cmp-r1/*.svg
```

Fifteen compositions cover hero profiles, verified multi-view and sectional
plates, patent/workshop treatments, owner commissions, evolution collections,
minimal contours, bounded-density ink studies and two-/three-/four-view
orthographic layouts. A5/A4/A3 use a physical detail ladder while keeping every
requested source view; A3 landscape is the preferred collector master. Source-image
edges are candidates only: the preparation pipeline corrects perspective,
links and smooths contours, removes noise and duplicates, classifies visible
features and returns a limitation instead of emitting a raw edge map when the
reference is inadequate.

The former 15-car, 12-aircraft and 10-watercraft parametric collections are
retired from the CLI. Their facts remain useful research, but their contours
were project-authored illustrative geometry rather than imported technical
outlines. `--collection cars|aircraft|boats` now fails closed. A real subject
can be rendered only from a source-qualified catalog with complete per-view
geometry; a missing view is never replaced by a procedural silhouette.

See the [technical-object evidence, asset and rendering contract](docs/technical-objects/TECHNICAL_OBJECTS.md).
The [source geometry policy](docs/technical-objects/SOURCE_GEOMETRY_POLICY_V2.md)
defines the binding no-fallback and reuse-rights gates. The
[v1 engineering collection record](docs/technical-objects/ENGINEERING_COLLECTIONS_V1.md)
is retained only as a description of superseded review work.
The [watch-movement workflow](docs/technical-objects/WATCH_MOVEMENTS.md) adds
hash-pinned native-PDF and one-bit source replay, subject-bound scaling, a
21-movement source audit, and review pilots for Omega 1861A, Rolex 3135 and
Seiko 6R20/6R21. It never substitutes generated movement geometry for a missing
technical source.
Generated plates and previews remain local review artifacts and are not
committed as finished artwork.

## Sports, route, circuit, and stadium artwork

`mapplot-sports` builds fourteen structurally distinct sports concepts on the
canonical plate renderer, from minimal route lines and elevation studies to
river courses, sailing charts, circuit telemetry, stadium plans, and season
collections. Hero geometry and quantitative traces are supplied and
source-labelled; the compiler does not map-match routes, fabricate telemetry,
or invent missing stadium architecture.

```bash
mapplot-sports presets
mapplot-sports build sports-record.json --no-png
mapplot-sports route activity.gpx \
  --id first-marathon --title "First Marathon" \
  --confirm-rights --privacy-reviewed
```

See the [sports data, composition, and plotting contract](docs/sports/SPORTS_ARTWORK.md).
The twelve-case test fixture is
[`tests/fixtures/sports-art-v1.json`](tests/fixtures/sports-art-v1.json).
Generated SVGs, previews, manifests, and pen jobs remain local artifacts.

## F1 circuit atlas v2.3 semantics

`mapplot-f1` is the dedicated, source-backed racetrack renderer for the frozen
2026 ledger and a separate multi-era former-F1 catalog. It preserves exactly
one closed Red source centreline, then adds closed paired Red passes at the
format's physical target width: 0.8 mm on A5, 1.2 mm on A4, and 1.6 mm on A3.
That visible corridor is diagrammatic—not a racing line or surveyed asphalt
width. Actual Grey track edges require independent source/lap association and
paper-scale qualification.

```bash
mapplot-f1 list
mapplot-f1 validate
mapplot-f1 build great-britain-silverstone-2026 --format a4-landscape \
  --output-dir output/f1-silverstone-review

# Rebuild the promoted v2.3 technical-review packages.
python3 tools/build_f1_circuit_series.py \
  --all-renderable \
  --catalog src/city_map_plotter/data/f1-circuits-2026.json \
  --format all \
  --output-dir review-output/f1-circuit-atlas-2026-v2.3-green-outline-water-facts \
  --dpi 254 --qa-profile review --generated-at 2026-08-11T00:00:00Z

python3 tools/build_f1_circuit_series.py \
  --all-renderable \
  --catalog src/city_map_plotter/data/f1-circuits-legacy-v1.json \
  --format all \
  --output-dir review-output/f1-circuit-atlas-legacy-v2.3-green-outline-water-facts \
  --dpi 254 --qa-profile review --generated-at 2026-08-11T00:00:00Z
```

`mapplot-f1 batch` is a review convenience: it constructs every selected
event/format plate in memory before writing, so an unresolved selection leaves
no partial artifacts. The staged `tools/build_f1_circuit_series.py` workflow is
the canonical atomic release builder. `--all-renderable` records every omitted
entry and its exact ordered reasons in `HELD-EVENTS.json`; `--all` remains the
fail-closed complete-catalog gate. A technical QA pass never clears the
separate rights or physical-proof holds.

All A5, A4 and A3 portrait/landscape variants share one north-up geographic
model while applying paper-specific course width, labels, and context density.
Green is reserved for source-derived grass, park, and woodland outlines. Grass
has no stipple, dotted fill, or repeated interior grass symbols. Blue water is
distinguished by closed plotter-safe dots clipped to sourced water geometry,
with sourced shoreline outlines where the feature and density budget permit.
Roads, access roads, buildings, stands, pit/paddock structures, runoff, gravel,
vegetation, and water provide richer surroundings, but deterministic selection
must still remain below the hard physical density gate. "Detailed" never means
an exhaustive or ink-saturated venue inventory.

Current-calendar plates may print only three course facts transcribed from the
frozen official Formula 1 event page: circuit length, `First Grand Prix`, and
the page's exact `Fastest lap time` value with driver and season. `First Grand
Prix` is a venue-history field, not proof that the drawn configuration debuted
that year. `Fastest lap time` is the official page label; the atlas does not
rename it an all-time record or derive a replacement from race results. A
missing, placeholder, partial, or conflicting value is visibly withheld.
Madrid remains held at the geometry gate and receives no plate even if its
official page contains factual text.

Former-F1 plates use the deliberately conservative line `LENGTH … / F1
REFERENCE …`; their fastest-lap fact remains withheld unless a future catalog
binds an exact configuration-matched official fact. They never borrow a modern
course record, infer a historic record, or backdate present-day context. The
same rule prevents invented apexes and famous-section names: `Gxx` means a
geometric station, not an official turn or apex, and unsupported copy is
omitted.

The current catalog contains the amended-WMSC ledger plus the announced Sepang
replacement; Sepang remains conditional and the called-off Sakhir and Jeddah
rounds remain exclusion records. Madrid is the sole current geometry hold.
Current catalog SHA-256:
`b117b4a2f0b40277417fd255d80f80b7dcc936e97f614e56e7095bdf7179746e`.

The curated former-F1 catalog remains a non-exhaustive set of exact historic,
surviving-equivalent, and conservative current-source reference studies.
Current-source context is marked `snapshot-current-not-backdated`; the
current-source Nordschleife, Brands Hatch, Estoril, and Kyalami studies remain
separate from their held exact-period records. Bahrain 2025 and Jeddah 2025 are
former-calendar references, not the called-off 2026 rounds.

Named course sections remain source-tagged, non-official, lineage-bound, and
collision-solved. Label clearances are bare-paper knockouts, never white ink.
Non-adjacent course-leg clearances may clip only derived corridor offsets; the
exact Red centreline remains unchanged. Source-proven grade separations may use
a Black bridge bracket without implying surveyed width.

The promoted v2.3 technical-review packages are
`review-output/f1-circuit-atlas-2026-v2.3-green-outline-water-facts` and
`review-output/f1-circuit-atlas-legacy-v2.3-green-outline-water-facts`. The
current package contains 132 plates (22 events x six formats), 995 one-pen SVG
jobs, six contact sheets, and 1,407 replayable checksum entries. The legacy
package contains 114 plates (19 references x six formats), 856 one-pen SVG
jobs, six contact sheets, and 1,214 replayable checksum entries. Semantic and
generic format QA pass every plate in both packages with zero failures.
The dedicated F1 regression matrix passes 277/277 tests on 2026-08-11.
Earlier v1.2 and v2.2 packages remain historical artifacts and do not acquire
v2.3 claims retroactively.

See the [F1 source, visual and QA contract](docs/f1-circuits/F1_CIRCUIT_ATLAS.md).
Generated circuit art remains local and uncommitted. Any resulting package
remains review-only until artifact-specific rights clearance and physical
plotted-proof holds clear.

## Academic, scientific, thesis, and graduation artwork

`mapplot-academic` builds evidence-bearing commemorative plates from
rights-cleared research data, structured equations, vector figures, schematics,
device layers, scalar fields, molecular/crystal coordinates, campus geometry,
timelines, and patent views. Sixteen distinct compositions cover paper and
thesis frontispieces, graph landscapes, equation centrepieces, experimental
paths, device blueprints, graduation coordinates, research journeys,
microscopy contours, publication collections, and restrained scientific
minimalism.

```bash
mapplot-academic presets
mapplot-academic validate examples/academic/graph-as-landscape.json
mapplot-academic build examples/academic/graph-as-landscape.json \
  --output-dir output/academic-graph-example --dpi 300

python3 tools/validate_format.py output/academic-graph-example/*.svg
```

The generator preserves explicit linear/log axes, values, units, gaps, extrema,
error bars, uncertainty, fit/measurement identity, equation structure,
schematic topology, materials, sites, bonds, and supplied captions. Output is
layered plotter paths only—heatmaps become selected contours or line hatching,
and screenshots enter a visible manual-review path instead of being silently
traced. Local CSV, JSON, and conservative SVG inputs are hash-bound; publisher
PDF scraping, raster production assets, protected branding, and invented
scientific content are rejected.

See the [academic artwork input, integrity, rights, and rendering contract](docs/academic/ACADEMIC_ARTWORK.md).
Generated masters, previews, manifests, and pen jobs remain local artifacts and
are not committed as finished artwork.

## Twenty-Five Icons of Golf

`mapplot-golf` builds 25 clarity-first A3 portrait course studies from frozen
OpenStreetMap objects. Every plate contains exactly 18 numbered source hole
centrelines plus the mapped surfaces and context admitted by renderer preset
`golf-clarity-course-a3-v4`. Missing surface detail remains absent; official
club and championship pages verify only names and concise context and are never
traced for geometry.

```bash
mapplot-golf build --all \
  --output-dir output/golf-course-series-v4 --dpi 180

find output/golf-course-series-v4 -maxdepth 1 -name '*.svg' \
  ! -name '*.pen-*' -print0 | \
  xargs -0 .venv/bin/python tools/validate_format.py --warnings-as-errors

PYTHONPATH=src .venv/bin/python tools/qa_golf_series.py \
  output/golf-course-series-v4
```

The batch writes 25 editable masters, previews, plot manifests, one registered
SVG per physical pen load, a contact sheet, collection manifest, and checksums.
The consistent physical palette uses grey for selected paths and inset sand
hachures; Green 0.25 mm source outlines without fill for fairways; Green 0.40 mm
outline-only tees; and clipped fine-line fill inside every visible sourced
green. The 0.16 mm fill inset contains the full fine-pen mark and the build
fails if any green cannot retain a legal fill stroke. Whole playing-surface
objects must intersect the exact 18-hole
routing's 60 m context, excluding remote practice and adjacent-course detail.
Every
visible water source uses closed Blue 0.25 mm dots: irregular dots inside area
water, source-line-centred dots for line-only waterways, source-boundary dots
for physically narrow polygons, source-anchored interior symbols for the rare
overlapping tiny polygon that cannot carry a separate boundary dot, and
water-side dots derived from oriented
coastlines. No continuous line-only waterway or cross-water hatch is emitted.

The raw catalog/root course boundary remains an invisible selection mask.
Preset v4 draws a separate Grey 0.40 mm illustrative playing-area envelope,
derived from the exact sourced holes and nearby mapped playing surfaces. That
envelope is not an official course boundary, property boundary or survey claim.
Red draws the collision-cleared hole markers, black handles hole numbers and
reference/copy work, and the real 1.00 mm gold pen draws the 18 hero centrelines.
Every sheet uses a deterministic page-fit rotation and fitted metric scale, with
a truthful north arrow in the furniture zone, visible ODbL attribution, and
document travel ratio below 1.0. Final SVG QA requires all 450 number disks to
remain clear of mapped ink and of one another.

These are explicitly review-only maps: the built-in inventory is nominal and
unmeasured, so the collection cannot become production-ready until the exact
pens, stock, speed, and pressure have a dated physical calibration. See the
[golf source contract](contracts/golf-courses-v2/README.md) and
[series workflow](docs/golf/GOLF.md).

## Hiking maps

The same physical page contract supports a separate `mapplot-hike` collection
of 40 source-backed hiking subjects. Every subject has two distinct, north-up
A5 artworks, yielding 80 editable master SVGs in a complete build:

- `detailed-map` follows the West Highland Way / Great Glen Way visual model: a
  full-field context map of selected water, woodland and grassland, roads and
  tracks, settlements, geographic names, peaks and quiet factual contours; and
- `terrain-relief` follows the Tour des Refuges visual model: continuous
  full-field elevation contours, with the route and enough sourced geographic
  context to remain legible as a map.

Neither edition uses local terrain snippets, inset boxes or decorative
fall-line hachures. Both variants use the same route identity and north-up
geometry. Factual minor contours use Grey 0.25; true fifth-interval index
contours use Grey 0.40 when the selected source levels contain one. This makes
the seventh pen load conditional rather than inventing a heavy contour on a
sparse source. An unboxed elevation profile sits at the bottom of the plate;
its A–E distance stations use the exact same chainage locations as the A–E
markers on the map. Selected peaks use a mountain symbol. A peak or pass height
is printed only when an approved source supplies that elevation explicitly;
the renderer does not infer a named summit height from nearby DEM terrain.
Raster-derived profile extrema are visibly labelled `SAMPLED ELEVATION /
APPROX`. When a published route total exists, A–E kilometre copy is scaled
proportionally to it and labelled `PUBLISHED KM`; otherwise it is labelled
`MEASURED KM`. The measured cumulative distance of a simplified source line is
retained separately in SVG metadata and is never promoted to an exact official
route length.

Roads remain single source centrelines; rivers remain source centrelines or
shorelines; grass and woodland are plotted only inside, or explicitly anchored
to, selected source features. A source query that finds no usable water, road
or land-cover feature is recorded as such instead of being repaired with
invented scenery. Deterministic source precedence preserves a route's declared
geometry and visible credit, retains stronger native terrain and embedded
profiles, and admits global terrain only when it materially improves factual
contour evidence.

Each artwork writes an editable SVG, a 300 dpi PNG, a plot manifest and one SVG
job per active physical pen. Artifact names preserve both identities, for
example `RTE-US-AT-01--detailed-map.svg` and
`RTE-US-AT-01--terrain-relief.svg`.

```bash
mapplot-hike list
mapplot-hike build --all \
  --format catalog \
  --output-dir output/hiking-series-paired-v4.2-2026-08-06 \
  --dpi 300
```

`--format catalog` uses each subject's audited A5 composition. A binding format
can be forced for custom studies, but the complete release gate accepts only the
catalogue's A5 portrait or A5 landscape binding. The batch also writes an HTML
gallery, `ARTIFACTS.md`, checksums, `PEN-CHANGE-GUIDE.md`, a paired-variant
`SOURCES.json`, a human-readable `LICENSES.txt`, and two separate 40-route
contact sheets:

- `hikes/hikes-detailed-map-contact-sheet.png`
- `hikes/hikes-terrain-relief-contact-sheet.png`

PNG generation requires Inkscape. Contact-sheet generation and exact PNG parity
QA require ImageMagick's `montage` and `compare` commands respectively. Install
the project development dependencies before running the gate, and make sure all
three executables are available on `PATH`.

Run its fail-closed QA matrix with:

```bash
.venv/bin/python tools/audit_hiking_composition.py \
  output/hiking-series-paired-v4.2-2026-08-06 \
  --json output/hiking-series-paired-v4.2-2026-08-06/composition-audit.json \
  --markdown output/hiking-series-paired-v4.2-2026-08-06/composition-audit.md \
  --fail-on-gate

.venv/bin/python tools/qa_niche_series.py \
  output/hiking-series-paired-v4.2-2026-08-06 --dpi 300
```

After the full build and QA gate succeed, its local artifact index is written to
`output/hiking-series-paired-v4.2-2026-08-06/ARTIFACTS.md`. Generated release
directories remain uncommitted local artifacts.

These examples are deliberately marked **review-only**. The actual pen widths
must be calibrated on the intended stock and speed, and hiking plates are
decorative artwork rather than navigation. The global relief pass uses
AWS-hosted Mapzen Terrarium tiles whose records explicitly require
location-specific review of the underlying provider terms and attribution.
Neither AWS Open Data hosting nor an automated QA pass grants commercial
clearance. Before sale, resolve every route's terrain-provider obligations and
retain each source-specific visible credit required by OpenStreetMap, Licence
Ouverte, CC BY, or another applicable source licence; SVG metadata alone is not
a substitute for required printed attribution. ODbL-backed hiking plates print
`© OPENSTREETMAP / OPENSTREETMAP.ORG/COPYRIGHT` visibly at the binding 2 mm
type floor.

V4.2 does not render bathymetry. Negative values present in some composite
terrain tiles have no audited provider, resolution, licence or chart-datum
lineage in this catalogue. Seabed contours require a separately frozen and
licensed bathymetric source; terrestrial DEM values are never repurposed as
ocean depth.

The visual hierarchy takes high-level inspiration from the [LAW Illustrates West Highland Way
print](https://www.lawillustrates.com/products/west-highland-way-print), but no
artwork, vector geometry, icon, or label arrangement is traced or copied. See
the [paired hiking acceptance contract](docs/hiking-qa-acceptance.md), the
[v4.2 contour release audit](docs/audit-2026-08-06-hiking-contours-v4.2.md),
the [historical paired release audit](docs/audit-2026-08-04-hiking-paired-release.md),
the [historical ten-route release audit](docs/audit-2026-08-03-hiking-only-release.md),
and the [context/provenance audit](docs/research/hiking-context-provenance-audit-2026-08-03.md).

A caller-owned GPX 1.0/1.1 track or route can be turned into the same local,
review-only hiking format. Both acknowledgements are deliberately required so
that a home/start location is not published accidentally and third-party tracks
are not silently copied:

```bash
mapplot-hike gpx my-hike.gpx \
  --title 'OUR SKYE CROSSING' \
  --subtitle 'MAY 2026 / PERSONAL ROUTE / NOT FOR NAVIGATION' \
  --format a5-portrait \
  --output-dir output/our-skye-crossing \
  --confirm-rights \
  --privacy-reviewed
```

## Quick start

Python 3.11 or newer is required. Shapely is a required runtime dependency and is installed automatically for cartographic geometry operations. From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For reproducible local `.osm.pbf` ingestion, install the optional free/open-source
PyOsmium/libosmium backend:

```bash
python -m pip install -e '.[pbf]'
```

Contributors can install the pinned lint/type-check toolchain and Shapely stubs
with `python -m pip install -e '.[pbf,dev]'`.

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. Confirm the installation with `mapplot --version`; use `mapplot export --help` to see every option.

First, verify the exporter offline with the included saved response:

```bash
mapplot export \
  --bbox -0.14 51.50 -0.12 51.51 \
  --input-json tests/fixtures/sample-overpass.json \
  --paper A3 \
  --orientation landscape \
  --frame \
  --output output/offline-example.svg
```

The saved file must be an Overpass API JSON response whose coverage includes the requested bbox. No User-Agent is needed for this offline form.

For a real download, public OpenStreetMap services require an identifying application User-Agent. Put your own contact address in it:

```bash
export CITY_MAP_PLOTTER_USER_AGENT='CityMapPlotter/0.1 (contact: you@example.com)'
```

The simplest real export uses a place name and a radius:

```bash
mapplot export \
  --place 'York, England' \
  --radius-km 5 \
  --paper A3 \
  --orientation landscape \
  --margin-mm 12 \
  --frame \
  --output output/york.svg
```

The radius is the crop radius, so `5` produces a map about 10 km across. If it is omitted for `--place`, the exporter uses a bounded radius around the geocoded centre instead of Nominatim's potentially very large administrative boundary: 5 km for the standard preset and 2.5 km for either A5 poster preset. Pass `--radius-km` whenever the composition needs a different crop.

An exact bounding box is useful when framing a known course:

```bash
mapplot export \
  --bbox -0.145 51.495 -0.105 51.520 \
  --paper A3 \
  --orientation landscape \
  --output output/london.svg
```

The bounding-box order is **west south east north**: longitude comes first. In contrast, `--center` uses **latitude longitude**. All coordinates are WGS84 decimal degrees.

## Interactive framing web UI

`mapplot-web` serves a local browser UI for choosing the composition visually:
a live world map that can be dragged and zoomed anywhere, behind a viewfinder
whose aspect ratio is taken from the exact plate contract the export will use
(the poster map field for `a5/a4/a3` presets, or the sheet minus margins and
the attribution footer for a standard export). The readout shows the framed
bounding box, physical frame size, and approximate scale in real time, and an
optional auto-detail mode chooses layers and a detail profile from the frame
width. It never selects the detail-thinning `plot` profile: wide frames retain
the complete road selection through `plotter-faithful`, while close city frames
use `faithful` with every building footprint.

```bash
export CITY_MAP_PLOTTER_USER_AGENT='CityMapPlotter/0.2 (contact: you@example.com)'
mapplot-web
```

Pressing **Export plotter SVG** submits the framed extent to the ordinary
`mapplot export` pipeline, run as a local subprocess job: every cartographic,
physical, provenance, and attribution guarantee of the CLI applies unchanged,
and the finished SVG plus `.plot.json` manifest appear in the browser and under
`output/webui/<job-id>/`. A saved Overpass JSON path can be supplied for
offline, reproducible renders.

The interactive basemap is drawn from public vector tiles
([OpenFreeMap](https://openfreemap.org/), OpenStreetMap data) so its road
detail follows zoom in real time; those tiles are only the viewfinder's
background and are never traced into the plot. Frames larger than the public
Overpass safety area are refused in the browser before any service call, and
place search remains a deliberate one-search-per-Enter Nominatim lookup. The
server binds `127.0.0.1` by default and is a single-operator tool, not a
public web service; do not expose it to the open internet as-is.

## University and marathon subject catalog

The bundled, versioned catalog turns editorial research into stable map inputs. It currently contains 108 subjects in five collections:

- all 24 Russell Group universities as distinct campus products;
- all 14 United States entries in QS Best Student Cities 2027;
- 15 additional United States campus towns represented by a QS World University Rankings 2027 top-100 institution, restoring smaller places that the student-city population rule excludes;
- the top 25 non-UK European entries in QS Best Student Cities 2027;
- 30 globally significant marathons selected from the World Athletics 2025 competition-performance ranking, with Cape Town added for its confirmed 2027 Major status.

Every collection stores its cut-off date, exact selection rule, source URLs, ranks or status where applicable, and ordered entries. Inspect the methodology before using the word “top” in product copy:

```bash
mapplot catalog collections
mapplot catalog list --collection uk-russell-group
mapplot catalog list --collection us-student-cities-qs-2027 --json
mapplot catalog show marathon-boston
```

The binding memorabilia release uses all five collections as one themed,
108-product cohort. A partial or legacy collection selection is useful for a
pilot, but it cannot pass the complete-series release audit.

Catalog IDs can be rendered directly. Their stored WGS84 seed point avoids a separate place lookup; `--radius-km` can override the subject's preview radius:

```bash
mapplot export \
  --subject uk-university-york \
  --paper A3 \
  --orientation landscape \
  --frame \
  --output output/university-of-york.svg
```

Pass `--catalog-file path/to/catalog.json` to either `catalog` or `export` to use another schema-version-1 catalog. The loader rejects malformed coordinates, duplicate IDs, broken collection references, non-contiguous positions, and subjects that are not assigned to a collection.

Marathon subjects do not contain verified course geometry. The city-map CLI refuses their stored preview extent unless `--radius-km` is supplied explicitly; with that override it produces and labels only a city-centred basemap preview. Each record links to an official route source and is marked `pending_official_route`. The separate `mapplot-hike gpx` command can render a caller-owned local track, but it does not make a marathon course official or verified. A future course workflow must keep route geometry edition-specific and verify it against an official source rather than infer a race route from the basemap or copy proprietary route artwork.

### Resumable collection exports

Plan a collection run before making any map-service requests:

```bash
mapplot catalog export \
  --all-collections \
  --output-dir output/catalog \
  --dry-run \
  --png --png-dpi 254 \
  --export-args --theme city-memorabilia-a5-series-v1
```

Remove `--dry-run` to render, repeat `--collection` for an exact group of
collections, or replace the selectors with `--all-collections`. Everything
after `--export-args` is parsed by the ordinary `mapplot export` command and
must come last, so fidelity, style, physical-pen, local-PBF, cache, and future
pen-profile options do not have a separate batch implementation. Subject
identity, extent, title, output paths, and saved `--input-json` are deliberately
batch-controlled. The theme applies purpose-specific copy: institutions retain
their full names, student-city subjects use the city, and marathon candidates
are explicitly labelled `CITY BASEMAP PREVIEW` / `COURSE NOT INCLUDED`. This
avoids five different Russell Group products all being titled only “London”.
One saved Overpass crop cannot safely stand in for many different cities.

Outputs use stable paths such as
`output/catalog/uk-russell-group/001-uk-university-birmingham.svg`, with the
manifest and optional PNG beside each SVG. A JSON batch report is written under
the output root by default. Re-running the same command resumes it: every
required SVG, manifest, and PNG is structurally validated and checked against
its recorded SHA-256 hash. Interrupted or failed items can be retried, while an
edited or missing completed artifact is never overwritten silently. Use
`--limit N` for a pilot, `--keep-going` to finish later items after a recorded
failure, and `--overwrite` only when intentionally starting a replacement run.

Batch completion is a resumability check, not the final plate audit. The strict
series release contract selects all 108 products from all five catalog
collections: 24 Russell Group institutions, 14 ranked U.S. student cities, 15
additional U.S. top-100 campus localities, 25 non-UK European student cities,
and 30 marathon-city basemaps. Inkscape must be
available on `PATH` for PNG rendering and ImageMagick's `identify` must be
available for opacity QA. Run the binding validator, provenance checks, exact
scope/settings checks, raster-size check, and companion-file generator with:

```bash
.venv/bin/python tools/qa_catalog_series.py \
  output/catalog/selected-collections.batch.json \
  --expected-count 108
```

The command succeeds only when every warning is resolved. It then writes
`QA_REPORT.json`, `CATALOG.md`, and the required series `ATTRIBUTION.md` beside
the batch report. Its scope is derived from all five bundled collections;
custom catalogs need their own declared QA contract.

Preview QA may accept an explicitly `live-overpass-unpinned` source cohort.
`--release-mode production` rejects it: all 108 items must bind the same
content-pinned PBF, normalized header snapshot/cutoff timestamp, and proven
header-bounds coverage. QA reconstructs the plan from the report's locked,
normalized export arguments, so release inputs such as `--input-pbf` and
`--pen-inventory` are verified rather than treated as unexpected options. Its
top-level `validation_contract` hashes both QA implementations independently
of the plotted artwork's edition signature.

Public-service runs are sequential and wait two seconds between subjects by default; `--delay-seconds` can increase that pause. A shared `--input-pbf` skips the inter-item service delay and is preferable when the file covers every selected extent. The command does not submit a plot job. Marathon items remain explicitly labelled and reported as `CITY BASEMAP PREVIEW`; the batch never claims or synthesizes unverified course geometry.

The binding A5 plate contract permits at most 28% map-field ink coverage. A
literal all-detail city can exceed that limit even with the finest available
pen, so the clean A5 series uses `ink-balanced`: it starts with the same full
topology-aware preparation as `plotter-faithful`, normalizes duplicate water
boundaries, reserves visible representation for every requested family, then
fills remaining capacity using road topology, geographic distribution and
stable semantic priorities. Every source-derived group is capacity-cullable;
every deliberate budget omission and every sub-nib physical omission has a
separate verified ledger in the manifest. This is not a claim that every
OpenStreetMap micro-segment appears on A5.

For a larger-sheet release whose requirement is *all acquired detail*, use a
source-lineage-complete profile only after validating the chosen format and
physical ink coverage. This claim is relative to the supplied OSM source; it
cannot certify that the source itself maps every real-world feature.
`plotter-faithful` omits only isolated marks below `3 x nib`;
`faithful` refuses even those physical omissions. Travel optimisation changes
drawing order and direction only, so `--no-optimise` does not preserve more
geometry.

The theme is a versioned rule base rather than a convenience preset. It binds
the plate format, named text zones, single-stroke font geometry, copy rules,
palette, semantic nib roles, and canonical rendering flags. Its resolved
edition signature also hashes the exact inventory and stock tone. The SVG,
manifest, and batch contract all carry that signature, so a changed font,
palette, pen plan, format, or inventory cannot silently resume as the same
edition. Theme-owned flags reject ad-hoc overrides; create a new versioned
theme when the design genuinely changes. See `docs/themes/THEMES.md`.

For the actual world-wide render, use a local planet extract
that covers every subject, or run geographic collections against appropriate
regional extracts; do not turn the public Overpass service into an unattended
bulk backend. The batch report is resumable and hash-verifies every completed
SVG/manifest pair and any requested PNG.

Place lookup follows the [public Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/) and is used only for a deliberate, single search. Results are cached, requests are kept at least one second apart, and autocomplete is not implemented. Do not use that public endpoint as the backend for a commercial product.

## Output

For `output/york.svg`, the program writes:

- `output/york.svg` — the editable master with emitted semantic layers arranged into contiguous physical-pen steps;
- `output/york.plot.json` — page geometry, source provenance, physical layer statistics, ordered pen instructions, and plot diagnostics.

Open the SVG in Inkscape. The file already has an exact physical width and height, a matching viewBox, rounded line caps, no fills, and coordinates baked into each path. Map geometry is physically clipped, so a plotter that ignores SVG clip paths cannot travel outside the map frame.

Add `--split-by-pen` to write one registration-compatible, page-sized SVG for each physical pen step. By default those files sit beside the master; use `--pen-output-dir DIR` to choose another directory. They are still ordinary SVGs, not hardware-specific plot jobs, and the software does not send them to a machine.

The default attribution footer is single-line vector lettering, so it is
directly plottable and included in the Black pen step. Clean commercial artwork
can instead use `--attribution-mode external` together with the required
`--external-attribution-placement` description. That mode removes only the
visible lettering: source credit, licence and provenance remain in SVG metadata
and the manifest. The recorded external placement must actually be supplied
with public output, for example on packaging, at point of sale, or in a printed
acknowledgement.

### Detail profiles

The default `--detail-profile faithful` is the all-road path: it keeps every
qualifying source feature and uses a small, topology-validated 0.04 mm page
error budget for joined, rounded road output. `plotter-faithful` makes the same
full cartographic selection, then joins compatible short road edges and omits
only residual marks below the selected nib's physical reliability floor.
Select `--detail-profile plot` explicitly only when intentional poster
simplification and feature selection matter more than broad road coverage:

```bash
mapplot export \
  --place 'York, England' \
  --radius-km 2 \
  --preset a5-balanced-poster \
  --detail-profile faithful \
  --layers roads,water,railways,parks \
  --output output/york-faithful.svg
```

`faithful` is the source-preservation profile for acquired features
that have been extracted inside the selected crop. It defaults to
`--simplify-mm 0.04`, keeps private/service roads, paths, short network edges, and
redundant river centrelines, bypasses poster detail budgets and several
pen-friendly suppressions, joins compatible source ways through protected
topology, and defaults roads to source centrelines. The active cartographic and physical
stages fail rather than silently lose tracked source features, while
below-nib conflicts are retained and reported. This is not a raw OSM mirror:
clipping, projection, and relation processing still apply. Within the padded
acquisition extent, road acquisition requests every intersecting `highway=*`
way. Documented road/path types, `area=yes`
highway perimeters, and active `highway=construction` geometry are retained;
construction is classified by its `construction=*` target. A genuinely
unfamiliar highway value falls back to `roads_other` and is listed under
`source.provenance.highway_coverage` instead of disappearing. Explicit
proposed/inactive lifecycle values and known non-route objects such as
platforms remain excluded. Correct cycle streets retain their ordinary class
through `bicycle_road=yes` or `cyclestreet=yes`; escalators remain paths
through `highway=steps` plus `conveying=*`. Pass `--road-style multi` or
`--road-style single-nib` explicitly
if a faithful export should use weighted road offsets. Use `--simplify-mm 0`
for exact source vertices with no centreline rounding. The `.plot.json`
manifest records the profile, effective simplification tolerance, source
lineage, and physical-resolution warnings. Dense faithful maps can be too
close-spaced for the selected nib, so inspect those warnings and use a larger
sheet, tighter crop, or finer pen where needed.

Before travel sorting, faithful road edges are assembled into continuous pen
trails only when their exact endpoints touch and their semantic layer, physical
pen, grade, offset, and pass all match. The compiler consumes every input edge
once, adds no connector, preserves total pen-down length and the full source-ref
set, and reports the before/after trail count. Residual trails below `3 x` the
effective mark stay in the lossless review SVG, with a per-layer count and
remediation; they block `--production` rather than being stretched, overdrawn,
or silently deleted.

The light-stock poster palette keeps fine families physically distinct: Blue
for water, Green for parks, Black for the road hierarchy, Grey for paths and
minor-road context, and a dedicated Red 0.25 mm railway pen drawn after the
transport network. Purple remains available for optional boundaries. White is
reserved for dark stock, while Gold and Silver are broad accent pens rather
than substitutes for fine geographic linework.

`plotter-faithful` uses that same cartographic selection and validated road
topology. It first assembles compatible short road edges into exact,
edge-disjoint trails without changing their geometry or source lineage. Any
remaining isolated trail or non-road fragment below `3 × nib` is then omitted
instead of being plotted as a dot. The manifest keeps both the zero-loss
cartographic lineage and the explicit physical omission ledger, so this is a
physically honest high-detail plate rather than a claim that an A5 pen can
reproduce geometry smaller than its own mark.

The roads family also acquires `area:highway=*` street-surface micromapping.
In `faithful`, its closed boundaries are emitted in the independent
`road_areas` layer with their own source lineage; they never replace or merge
into the routable centreline graph. The restrained `plot` profile suppresses
this potentially dense perimeter layer.

If a positive `--simplify-mm` is supplied with `faithful`, roads are processed
as one non-planar source graph. OSM node IDs (from PBF when available), shared
vertices, endpoints, junctions, bridge/tunnel/layer changes, and semantic
transitions are fixed anchors. Only anchor-to-anchor spans are simplified. The
budget first reserves 0.000778 mm for worst-case coordinate quantization; up to
35% of the remainder (capped at 0.03 mm) is used for sampled corner rounding.
Simplification, rounding, and serialization together therefore stay within
`--simplify-mm`. The export fails if connectivity, protected endpoints, or the
symmetric combined error-bound check fails. Ordinary geometric crossings are
not invented as junctions. At zero tolerance, compatible degree-two ways are
still joined to avoid needless pen lifts, but every source vertex remains exact
inside the compiler and every contributing source ID remains in
`data-osm-source-refs`.

The below-nib proximity scan is on by default for `faithful` and off by default
for `plot`, because dense city crops require more spatial comparisons. Override
that choice with `--physical-audit` or `--no-physical-audit`. The report measures
actual emitted nib marks, excludes a small neighbourhood around ordinary point
junctions, keeps all geometry, and records whether its explicit candidate-pair
safety limit was reached. A truncated count is labelled as a lower bound; it is
never presented as complete. Production requires a complete scan. If a reviewed
composition deliberately contains nearby marks that will merge, record that
sign-off with `--accept-physical-conflicts`; this flag never waives residual
sub-nib trails, an incomplete scan, or an out-of-tolerance width fit.

For a first physical plot:

1. Open **Layer → Layers and Objects** and hide or delete unwanted semantic layers.
2. Inspect road and river coverage, especially around the official course.
3. For public output, retain the vector attribution layer or, when external
   attribution mode was selected, distribute the recorded credit on packaging,
   at point of sale, or in the supplied acknowledgement.
4. Preserve the document page size and plot at 100%, with no printer scaling.
5. Run the plotter's bounds/preview mode before fitting a pen.

In the generic default renderer, parks, water areas, and buildings are outline
linework rather than polygon-derived fills or hatching. Outer/inner area
topology is used for internal calculations, but emitted boundaries and holes
must still be checked visually. Product-specific university and transit
compositions can add their separately documented physical closed-path water
stipple; neither uses a CSS/raster fill.

The built-in single-stroke font already draws poster titles, details, attribution, and the labels for the north mark and scale bar. Street-name labels are deliberately unimplemented because road-name selection, baseline placement, collision avoidance, and density control still need a cartographic placement pass—not because a plotter-safe font is unavailable.

## A5 clean poster

Use the fixed portrait composition when the output is intended as a finished A5 artwork:

```bash
mapplot export \
  --bbox -1.08735 53.95585 -1.07665 53.96215 \
  --input-json saved-york-overpass.json \
  --preset a5-clean-poster \
  --layers roads,water \
  --title YORK \
  --subtitle 'NORTH YORKSHIRE / ENGLAND' \
  --detail 'RIVERS OUSE AND FOSS' \
  --detail '53.9590 N / 1.0820 W' \
  --detail 'APPROX SCALE 1:5650' \
  --output output/york-a5-clean-poster.svg
```

This preset is exactly `148 × 210 mm` and is bound to the generated
`a5-portrait` contract. It has a double 0.6 mm border inset 6 mm, an exact title
and subtitle stack, a `124 × 128.924 mm` map field beginning at 35.122 mm, and
separate furniture, detail, and attribution zones. With the default
`--detail-profile faithful`, every qualifying road—including service roads and
paths—is retained; compatible source ways are joined without losing source
lineage, then rounded within the declared paper-space error budget. Select
`--detail-profile plot` only when intentional poster cleanup is wanted. Broad
rivers use Blue 0.4, while streams and other narrow waterways use Blue 0.25.

The poster uses actual single-stroke lettering and vector SVG paths—never screen-font `<text>` elements, raster masks, or white eraser strokes. Pen assignments are recorded in the `.plot.json` manifest. Numeric nib, stroke-count, and pass settings compile into the emitted paths, but the result still depends on loading the listed physical pen and calibrating the machine.

## A5 balanced poster and city series

Use `a5-balanced-poster` for the richer memorabilia composition. It uses the same
exact A5 zones and non-square map field. In `faithful` mode it retains all
qualifying minor roads, service roads, and paths; the balanced preset affects the
visual treatment, not the all-road retention guarantee. In `plot` mode it may
apply the documented selective cleanup for a lighter drawing.

A 2.0 km radius gives a view approximately 4 km across. In the fixed 124 mm map field this is approximately `1:32,300`, which is a useful starting point for showing the wider city without losing the central street pattern:

```bash
mapplot export \
  --place 'York, England' \
  --radius-km 2.0 \
  --preset a5-balanced-poster \
  --layers roads,water,railways,parks \
  --title YORK \
  --subtitle 'NORTH YORKSHIRE / ENGLAND' \
  --detail 'RIVERS OUSE AND FOSS' \
  --detail '53.9591 N / 1.0815 W' \
  --detail 'APPROX SCALE 1:32300' \
  --output output/york-a5-balanced.svg
```

The bundled five-city reference series uses this common framing and density treatment:

- [York](examples/city-series/york-a5-balanced.svg)
- [Newcastle](examples/city-series/newcastle-a5-balanced.svg)
- [Durham](examples/city-series/durham-a5-balanced.svg)
- [Exeter](examples/city-series/exeter-a5-balanced.svg)
- [Southampton](examples/city-series/southampton-a5-balanced.svg)

Each SVG has an adjacent `.plot.json` manifest containing its ordered pen plan and measured pen-down distances. The stated scale is approximate because the precise projected extent varies slightly by latitude and place centre.

### Personalised university memorabilia

The `university-memorabilia` composition keeps the same map field but replaces
the centred copy with a proportional Hershey Serif city title at top-left,
small coordinates at top-right, a dedicated header compass, and blank or
populated name/degree/honours/years fields in the footer. Surface water can be
stippled with real closed pen paths, while `--landmark-buildings` applies
semantic, physical-size, category-quota, and ink-budget gates instead of
drawing every ordinary building:

```bash
mapplot export \
  --center 53.9591 -1.0815 \
  --radius-km 2 \
  --input-json york-overpass-2026-08-02.json.gz \
  --preset a5-balanced-poster \
  --poster-layout university-memorabilia \
  --layers roads,water,railways,parks,buildings \
  --style styles/university-memorabilia-v2.json \
  --water-fill dots \
  --landmark-buildings \
  --detail-profile plotter-faithful \
  --simplify-mm 0.04 \
  --road-style centreline \
  --title YORK \
  --output review-output/university-memorabilia-v2/york.svg
```

Add `--person-name`, `--degree`, `--honours`, and `--years` to print supplied
values; omit them for a writable template. The memorabilia layout suppresses
the legacy map-overlay furniture automatically because its compass has a clear
header zone. For live Overpass acquisition, `--landmark-buildings` also requests
the exact semantic candidate superset rather than every ordinary house, shed,
and garage. This keeps wide metropolitan compositions tractable; lifecycle,
identity, physical-size, quota, and ink-budget rejection still happens locally.
For a very large city, `tools/fetch_sharded_snapshot.py` can fetch the same
padded composition one family at a time and merge it deterministically. It
fails if two shards contain conflicting versions of one OSM object and embeds
the shard hashes, timestamps, extent, and selector policy in the saved JSON.

At a 2 km radius, retaining every fine road on A5 exceeds the 28% map-field ink
coverage reference figure. That figure is **advisory only** — the validator
reports coverage as an advisory, never a failure, and it is not a reason to cull
detail or change sheet. Density is a deliberate house preference; pick the sheet
the composition wants and let the number be reported.

What does keep a file under `review-output/` rather than `output/` is physical
readiness: a nominal (unmeasured) pen inventory, an unresolved physical-conflict
report, or an unpinned source.

### Rowing head courses

The `rowing-course` composition is the same keepsake header and footer with the
race in place of the graduate: a serif race title, coordinates, a header
compass, the course drawn over the water it is rowed on, and a four-cell fact
footer instead of blank personalisation fields.

```bash
mapplot export \
  --rowing-course horr-london \
  --course-margin 0.15 \
  --preset a5-balanced-poster \
  --poster-layout rowing-course \
  --layers roads,water,railways,parks,buildings \
  --style styles/rowing-course-v1.json \
  --landmark-buildings --water-fill dots \
  --detail-profile plotter-faithful --simplify-mm 0.04 --road-style centreline \
  --no-scale-bar --optimise --split-by-pen \
  --attribution-mode external \
  --external-attribution-placement "Product page or caption adjacent to each artwork" \
  --output review-output/rowing-heads-a5/horr-london.svg
```

`--rowing-course` frames the sheet on the course's own extent, so the whole race
fits with air around it instead of being cropped by a city radius. Four courses
ship: `horr-london`, `pairs-head-london`, `henley-royal`, `head-of-the-charles`.

**The course line is sourced, not traced.** Catalog marathons print `COURSE NOT
INCLUDED` because nobody has imported an official route. A head course is a
tractable case: it is the river, between two places the organiser publishes. So
`tools/build_course_geometry.py` takes the start and finish from named OSM
features matched to the organiser's own course description, cuts the OSM river
centre-line between them, measures the result, and refuses anything more than
12% from the published distance. The published figure and the measured
centre-line both travel into the manifest's `race_course` block. The drawn line
is the centre-line, not a survey of the raced line, and the plate says so.

The course is drawn in Red at the plate's `race_course` width — 0.80 mm on A5,
1.60 mm on A3 — which is wider than any single general-colour nib, so it is
built the way a wide road is: parallel offsets of the real 0.40 mm pen.

#### Naming the crew

`--poster-layout rowing-crew` with a `--crew-file` turns the course plate into a
crew keepsake: the course, then one block with the crew top to bottom in boat
order on the left and the result on the right.

```bash
mapplot export \
  --rowing-course horr-london --course-margin 0.15 \
  --crew-file examples/crews/thames-eight.json \
  --poster-layout rowing-crew \
  --preset a5-balanced-poster \
  --layers roads,water,railways,parks,buildings \
  --style styles/rowing-course-v1.json \
  --landmark-buildings --water-fill dots \
  --detail-profile plotter-faithful --simplify-mm 0.04 --road-style centreline \
  --no-scale-bar --optimise \
  --output review-output/rowing-crew-a5/thames-rc-eight-horr.svg
```

`result` in the crew file is free-form label/value rows, up to nine: a head
wants time, position out of how many and division; a regatta wants who was
beaten, the verdict in lengths, the time and the round.

Seven classes are supported — `8+ 4+ 4- 4x 2- 2x 1x`. Bow and stroke print as
words, the rest by seat number. A missing, repeated or non-existent seat is
refused by name, and a crew name too long for its column fails rather than being
set smaller than the others. Line spacing is fixed by the band, so a four and an
eight are set on the same rhythm.

The crew composition is a generated zone stack (`crew_zones_mm`) and the extent
is cropped to its own field aspect, so the map fills its band and lines up with
the title. The commands above generate worked examples under the local
`review-output/rowing-crew-a5/` directory.

Two rendered series can be generated locally. `review-output/rowing-heads-a5/`
is the keepsake size that matches the university memorabilia composition, and
`review-output/rowing-heads-a3/` is the wall print, where a 6.8 km course has
room and the street fabric around the river is legible rather than suggested.
Each carries a contact sheet and a `SERIES.md` with the per-course measurements.
Sheet size is a composition decision; ink coverage is advisory and plays no
part in it.

## Layers and density

Defaults are `roads,water,railways,parks`. Add dense or potentially expensive layers explicitly:

```bash
mapplot export ... --layers roads,water,railways,parks,buildings,boundaries
```

Buildings can make a city export very large. Administrative boundary relations can also be expensive. For reproducible local rendering or externally orchestrated batches, use a dated regional `.osm.pbf` rather than repeatedly querying a public Overpass instance:

```bash
mapplot export \
  --bbox -1.25 53.85 -0.90 54.10 \
  --input-pbf data/yorkshire-latest.osm.pbf \
  --layers roads,water,railways,parks,buildings \
  --output output/york-region.svg
```

`--input-pbf` and `--input-json` are mutually exclusive. A bbox, center/radius, or catalog subject still defines the exact composition. With a bbox/center/catalog subject the PBF path makes no OSM service call; `--place` still uses Nominatim to resolve the place name. The PBF itself is never modified.

The optional PBF reader streams way-node locations and uses libosmium's multipolygon assembler, retaining outer/inner ring association, source way or relation IDs, node references, tags, object version/timestamp/changeset/user metadata, and relation-member references in the canonical feature model. The plot manifest records the resolved input path, byte size, source-header metadata, extraction bbox, SHA-256 of the full PBF, and a second SHA-256 of the canonical extracted feature set. These hashes let a later build verify that it used exactly the same source and extraction result. They do not turn area outlines into fills or hatching.

Roads are separated into major, secondary, local, other, and path layers based on their OpenStreetMap `highway=*` value. Road merging never crosses a normalized bridge/tunnel/layer grade transition. Every SVG map path retains `data-osm-source-refs` (including every contributor to a merged path), its direct type/ID when singular, relevant source tags, and its name in a `<title>` when one exists. The manifest records exact visible-source coverage at cartographic compilation, physical compilation, and final SVG emission; a faithful export fails if any stage loses a tracked source reference.

`rendering.highway_completeness` uses schema 2. Its unqualified `complete` field is true only when the retained live Overpass query proves broad, bbox-covering acquisition of `way["highway"]` plus way/relation `area:highway`, requests unbounded inline geometry, and every expected supplied road object survives extraction through final SVG. `pipeline_complete_for_supplied_source` is narrower: it proves loss-free processing only for the raw Overpass-shaped objects actually supplied. A saved JSON response can therefore be pipeline-complete while `complete` remains false when its original query is unavailable. PBF input does not expose raw Overpass-shaped objects to this oracle, so `source_available`, `pipeline_complete_for_supplied_source`, and `complete` are false there by design; use the PBF content/canonical-feature hashes and the cartographic/physical source-lineage reports as its reproducibility and retention evidence.

`rendering.raw_geometry_integrity` is the complementary selected-layer audit. For live or saved Overpass JSON it validates IDs, duplicate consistency, every selected way and relation-way-member coordinate list, multipolygon roles and assembled parts, and exact visible raw-to-canonical geometry preservation across roads, water, rail, parks, buildings, and boundaries. Null separators, missing/empty members, non-finite or out-of-range coordinates, zero-length parts, unsupported geometry-role members, and changed/dropped canonical parts are explicit failures. `faithful` fails before writing an SVG; `plot` may continue only with a prominent manifest warning and a complete finding ledger. This field still describes only objects present in the supplied response. For PBF it is explicitly `not_audited`; the PBF hashes and downstream lineage remain the applicable evidence.

## Paper and styling

Supported presets are A5, A4, A3, A2, A1, A0, Letter, and Legal. For arbitrary stock:

```bash
mapplot export ... \
  --paper custom \
  --width-mm 500 \
  --height-mm 350 \
  --orientation landscape
```

Preview colors, labels, drawing order, and the physical pen model can be overridden with JSON:

```bash
mapplot export ... --style examples/style.json
```

Each layer has machine-readable `ink`, `nib_mm`, `strokes`, and `passes` fields. `nib_mm` sets the emitted SVG stroke width and the physical-resolution gates. A stroke count greater than one creates real parallel paths at nib-relative spacing; a pass count greater than one creates explicit repeat paths. The older `pen` and `stroke_width_mm` fields remain accepted as label/width aliases, while `stroke` is only the screen color.

### Physical pen inventory and calibration

Exports default to `--pen-profile actual-pens`. This is a conservative nominal
template for the working palette: Black/Blue/Green/Grey/Purple/Red 0.25 and 0.4,
Black 0.6 and 1.0, White 0.7 and 1.0, and Gold/Silver 1.0. It does not pretend
that “all colours” is an exhaustive SKU list, and it never invents white or
metallic fine pens. Use a custom schema-v1 inventory to record every real barrel
in the studio.

Width fitting is one-pass first. A compatible real nib must be within
`max(0.05 mm, 15%)` of the requested width. Parallel offsets are used only for a
target wider than every compatible single nib, and only when an exact 2–6-line
construction has a safe pitch between 0.5 and 0.9 measured mark widths. The
fitter chooses the fewest lines, then the widest compatible pen. Repeating the
same centreline changes opacity, not width: it is never selected to make a line
thicker, requires `--allow-repeat-passes`, is capped at two passes, and remains
production-blocked until opacity/drying has been calibrated separately on the
chosen stock.

The built-in widths are nominal and therefore produce a production-calibration warning. Generate the A3 plot-and-measure card before final output:

```bash
mapplot pens calibration \
  --output output/pen-calibration.svg \
  --stock-id bristol-250gsm \
  --stock-tone light \
  --pen-down-speed axidraw-25-percent
```

The card contains ten independently plottable 100 mm, one-pass specimens for
each selected pen. The conservative light/mid-stock card excludes White (160
specimens for 16 of the built-in pens); a dark-stock card selects only White,
Gold, and Silver (40 specimens for four pens) because ordinary inks have no
opacity evidence there. Every exclusion and reason is recorded. After drying,
measure each specimen at its midpoint. Use the median as
`effective_width_mm`; the loader recomputes both the median and sample
coefficient of variation and rejects CV above 10%. Offset-band, opacity, and
repeat-pass tests are deliberately separate from this width calibration. The
adjacent `.pens.json` records the stable run/specimen IDs, instructions, and a
machine-readable result schema.

A custom inventory is closed schema version 1. It requires inventory provenance,
one exact stock, explicit stable pen IDs, and an explicit calibration state.
Unmeasured pens omit all measurement fields. A measured entry additionally
contains the exact stock/speed run and all ten specimens; the abbreviated shape
below shows the required nesting (copy the ten IDs emitted by the calibration
manifest rather than inventing them):

```json
{
  "schema_version": 1,
  "id": "my-measured-pens",
  "label": "Measured on Bristol 250 gsm",
  "provenance": {
    "recorded_by": "Adam",
    "recorded_at": "2026-08-02T14:30:00+00:00",
    "method": "A3 ten-specimen one-pass width card"
  },
  "stock": {
    "id": "bristol-250gsm",
    "label": "Bristol 250 gsm",
    "tone": "light",
    "finish": "smooth"
  },
  "pens": [
    {
      "id": "black-fine-a",
      "ink": "Black",
      "nominal_nib_mm": 0.25,
      "effective_width_mm": 0.27,
      "calibration_state": "measured",
      "substrate": "bristol-250gsm",
      "calibration": {
        "run_id": "my-measured-pens-bristol-250gsm-light-width-v1",
        "stock_id": "bristol-250gsm",
        "pen_down_speed": "axidraw-25-percent",
        "specimens": [
          {"id": "black-fine-a-width-01", "width_mm": 0.27},
          {"id": "black-fine-a-width-02", "width_mm": 0.27},
          {"id": "black-fine-a-width-03", "width_mm": 0.27},
          {"id": "black-fine-a-width-04", "width_mm": 0.27},
          {"id": "black-fine-a-width-05", "width_mm": 0.27},
          {"id": "black-fine-a-width-06", "width_mm": 0.27},
          {"id": "black-fine-a-width-07", "width_mm": 0.27},
          {"id": "black-fine-a-width-08", "width_mm": 0.27},
          {"id": "black-fine-a-width-09", "width_mm": 0.27},
          {"id": "black-fine-a-width-10", "width_mm": 0.27}
        ],
        "median_width_mm": 0.27,
        "coefficient_of_variation": 0.0
      }
    }
  ]
}
```

The identical values above are only a schema illustration, not measurement data
for somebody else's pen. Add a measured record for every colour/nib the selected
style uses, then request the conservative gate explicitly:

```bash
mapplot export ... \
  --pen-inventory path/to/pens.json \
  --stock-id bristol-250gsm \
  --stock-tone light \
  --pen-down-speed axidraw-25-percent \
  --production \
  --split-by-pen
```

Format-ladder checks use the nominal nib; clipping, minimum-length gates,
preview widths, tolerance checks, and offsets use the measured mark. Every SVG
group, path, layer record, and pen-load step retains the requested/achieved
width, error, pitch, stable pen ID, calibration state, and operating conditions.

`--production` also fails closed when retained trails are shorter than
`3 x effective mark`, the below-nib separation scan is missing or truncated,
nearby marks have not been explicitly reviewed, or the achieved width exceeds
`max(0.05 mm, 15%)`. At map scale, local/other roads and paths must represent no
more than 12 m per plotted mark and major-road marks no more than 25 m. These
checks do not remove detail from review output; they tell you when to use a
larger sheet, tighter extent, or finer measured pen.

Road compilation is selected with `--road-style`:

- `multi` (the `plot` default) uses the configured nibs and rank-aware parallel offsets;
- `centreline` (the `faithful` default) emits one path on each retained road centre-line;
- `single-nib` assigns all road layers one black nib and expresses hierarchy through additional offset paths; set its nib with `--nib-mm`.

Offsets are clipped and may conservatively fall back to a centre-line when a complete companion set cannot be generated. The manifest records requested and achieved physical widths, passes, omissions, and warnings. These values form a machine-readable drawing plan, but the software cannot detect the pens installed or guarantee real ink width without hardware calibration.

### Travel ordering and per-pen output

Travel optimisation is enabled by default. Within each semantic layer it chooses the nearest endpoint and may safely reverse open paths; it does not join strokes into a new network or perform a global optimal-route search. Use `--no-optimise` to skip this endpoint reordering.

The manifest reports map-path pen-up distance before and after optimisation,
reversed paths, lifts, and pen-down distance. It also measures the exact emitted
document-order travel from home to every path for each pen load and gives an
estimate including that pen-up motion. Return-home travel and manual pen-change
time remain excluded and are disclosed. Neither estimate is a promise of
hardware runtime.

To prepare separate files without manually toggling layers:

```bash
mapplot export ... \
  --road-style single-nib \
  --nib-mm 0.25 \
  --split-by-pen \
  --pen-output-dir output/york-pens
```

The master and `.plot.json` remain the source of truth. Split files preserve the same page and registration and contain one physical pen step each; no AxiDraw, HPGL, G-code, or other hardware driver is included.

## Reproducible/offline rendering

Downloaded Overpass responses are cached under `~/.cache/city-map-plotter`. The manifest records the exact cache file used. Pass that file through `--input-json` to reproduce one render without any service call. Saved JSON can certify loss-free processing of its supplied objects, but without the original query it cannot certify acquisition scope. For repeated local runs, prefer `--input-pbf`: its content and canonical-feature hashes are recorded in `source.provenance`, while its raw-highway completeness fields remain explicitly unavailable and false. An explicit extent is still required because it defines the composition and crop. The resumable collection runner invokes this same single-composition path once per catalog subject and records each artifact hash.

A themed `--production` run is stricter: it accepts only a content-pinned PBF
with a valid UTC snapshot/cutoff timestamp and header bounds covering the
complete padded acquisition extent. A batch binds every city to that one PBF
SHA-256 and re-hashes the PBF and pen inventory immediately before each render.
This proves a consistent supplied source cohort; it does not prove that a
third-party PBF was unfiltered or that OpenStreetMap contains every real-world
feature.

Every export also records SHA-256 hashes for the source file and canonicalized
source data. Live/cached Overpass exports retain the exact query and its hash.
This lets an audit replay the cache file, re-hash its decoded JSON, and bind the
manifest to the exact acquisition input rather than relying on a filename or
timestamp alone.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The generated six-format plate contract lives in `docs/format/format-v1.json`;
run `python tools/validate_format.py path/to/map.svg` for a detailed pre-plot
report. The identical contract is packaged with installed wheels, and tests
ratchet generator/docs/package byte identity. New A5 poster output reads the
canonical zones and type/nib roles directly, including its double border,
physical lettering floors, nominal-nib ladder, and per-pen travel schedule.

## Accuracy and source responsibility

For the implemented algorithms, invariants, manifest fields, production PBF
workflow, and measured limits, see [the fidelity pipeline](docs/fidelity-pipeline.md).

OpenStreetMap is an excellent vector source but is community-maintained and does not guarantee completeness or survey accuracy. Always inspect a generated course map against the official marathon route and current local information. A future GPX input will be treated as a separate route overlay; the route should never be inferred from the basemap.

The geometry pipeline uses Shapely and a local equirectangular projection intended for small city extents; local PBF input additionally requires the optional PyOsmium/libosmium backend. The projection preserves a city drawing well, but the reported scale is approximate and must not be used for surveying or official course-distance measurement. A future large-extent or measurement-grade engine should use a suitable local projected coordinate system and report its error bounds.

Do not scrape Google Maps, Apple Maps, or rendered OpenStreetMap tiles for this pipeline. Apart from licensing and service-policy problems, raster tracing loses road identity and topology. Use licensed source vectors.

OpenStreetMap data is available under the ODbL. For printed maps and physical
artwork, follow the [OSMF attribution guidance](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines).
Embedded mode draws the credit and full copyright URL on the sheet. External
mode removes that visible footer only when the same credit and URL are actually
supplied with the work on packaging, at point of sale, or in an accompanying
acknowledgement; SVG metadata alone is not the public attribution.

## Next milestones

1. Import and validate edition-specific GPX marathon routes, then render a clearly separate course overlay.
2. Emit polygon-derived closed area boundaries and choose a hole-aware, physically calibrated hatch/fill language.
3. Add street-label candidate selection, single-stroke baseline placement, collision avoidance, and density controls.
4. Research per-subject extents and add offline golden renders for the full catalog matrix.
5. Extend the framing web UI with palette preview, and, separately, add a guarded plot-job layer with bounds checks, explicit pen-change confirmation, and optional AxiDraw/HPGL/G-code drivers.
