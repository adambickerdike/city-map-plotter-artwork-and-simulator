# F1 circuit atlas v2.3 semantics

This subsystem produces detailed, editable, north-up pen-plotter studies for
the current 2026 Formula 1 venue ledger and a separate multi-era former-F1
configuration catalog. It uses the physical-paper and source-evidence rules of
the hiking, rowing, and city-map series, with a rowing-inspired, format-bound
course corridor around one exact closed source centreline. Version 2.3 also
binds a restrained official-fact block and replaces dotted/symbol-filled grass
with Green source-derived vegetation outlines while keeping water visibly
dotted.

The collection is independent artwork. It is not an official Formula 1, FIA,
promoter, team, or circuit-owner product. A technical pass never clears the
separate rights or physical-proof holds.

## Current-calendar scope frozen on 2026-08-10

The current catalog separates facts that must not be conflated:

1. the amended FIA World Motor Sport Council calendar;
2. later official amendments or venue announcements;
3. geometry qualification for the stated circuit configuration;
4. rights clearance and a physical plotted proof.

The ledger contains the 22 events in the amended WMSC calendar plus the
announced Sepang replacement. Sepang remains `announced-pending-WMSC`, subject
to the final agreements and approval process stated by Formula 1 and the FIA.
The called-off Sakhir and Jeddah events remain only in
`excluded_calendar_events`. Madrid remains subject to homologation and is also
the sole current geometry hold.

Primary calendar evidence:

- <https://www.fia.com/news/2026-fia-sporting-calendars-approved-world-motor-sport-council>
- <https://www.formula1.com/en/latest/article/bahrain-and-saudi-arabian-grands-prix-will-not-take-place-in-april.1hnqllVG85RSt8pbFc5Ivx.1hnqllVG85RSt8pbFc5Ivx>
- <https://www.formula1.com/en/latest/article/formula-1-and-fia-confirm-malaysia-will-join-2026-calendar-as-host-venue-for-bahrain-grand-prix.6lL7vjFEM2VVynRHvg1TCf>

Calendar membership is not geometry evidence. Circuit facts are bound to
official event pages; reusable linework is bound separately to frozen,
versioned OpenStreetMap objects. No FIA, F1, circuit-owner, broadcast, or
third-party map image is traced.

The frozen current catalog earns these geometry tiers:

| Geometry tier | Count | Permitted claim |
|---|---:|---|
| `source-qualified` | 15 | Exact lap plus every additional topology claim required by the complete normalized-model gate |
| `cartography-qualified-centreline` | 7 | Exact closed source lap and factual context; missing start/finish, turn stations, pit topology, direction, or operational detail stays omitted and visibly disclosed |
| `provisional` / held | 1 | No plate; Madrid's selected source lap differs from its published length by 2.021640%, above the 1% gate |

The renderer-eligible records now form the promoted technical-review package
`review-output/f1-circuit-atlas-2026-v2.3-green-outline-water-facts`: 22
eligible events x six formats = 132 plates. This is not a complete-catalog or
commercial release: Madrid remains recorded in `HELD-EVENTS.json`, the
complete-catalog `--all` gate continues to fail closed, and rights plus
physical-proof holds remain open.

The current catalog SHA-256 is
`b117b4a2f0b40277417fd255d80f80b7dcc936e97f614e56e7095bdf7179746e`.
This digest identifies the catalog, not a completed v2.3 artifact package.

## Official course-fact contract

Current-calendar fact copy is transcribed only from each frozen official
Formula 1 event page. The permitted visible fields are:

- the published circuit length;
- the page's `First Grand Prix` year; and
- the page's exact `Fastest lap time`, driver, and season.

`First Grand Prix` is scoped as a venue-history fact. It is not configuration
debut evidence and does not prove that the currently drawn layout existed in
that year. `Fastest lap time` is retained as the Formula 1 page's own field
label and scope; the atlas does not promote it to an independently researched
all-time lap record. Times are parsed to integer milliseconds for consistency
while their exact source copy is preserved.

Visible and structured official-page values must agree. A missing placeholder,
partial record, malformed time, conflicting duplicate, or absent frozen page
is withheld in the internal evidence ledger; it is never filled from memory,
qualifying, an unbound results page, or another configuration. The sheet uses a
neutral `EDITION` card in that case instead of printing audit language as
display copy. Madrid remains a geometry hold and emits no plate, irrespective
of the factual text available on its event page.

## What the Red course means

The v2-family renderer preserves exactly one Red 0.40 `lap-centreline` path in source
coordinate order. SVG metadata binds its source-coordinate count, projected
coordinate count, source-lap digest, complete model digest, and source objects.
It is never replaced by a smoothed, snapped, or visually reconstructed route.

The renderer then derives closed paired Red offset passes from that exact
centreline. Together they make the circuit explicit on paper in the same
physical visual grammar as the rowing-course studies:

| Paper | Bound target width | Logical Red passes | Offset radii |
|---|---:|---:|---|
| A5 | 0.8 mm | 3 | 0.2 mm |
| A4 | 1.2 mm | 5 | 0.2 and 0.4 mm |
| A3 | 1.6 mm | 5 | 0.3 and 0.6 mm |

These passes are a **diagrammatic course corridor**. They do not claim surveyed
asphalt width, kerb-to-kerb width, vehicle trajectory, racing line, or apex.
The plate and manifest state this explicitly. If an inner or outer offset is
empty, open, missing, below the three-nib physical floor, or otherwise loses
parity, the artifact is held; the renderer does not duplicate the centreline
as a fallback.

## Actual track edge and asphalt evidence

The diagrammatic Red corridor and a sourced Grey track edge are separate
claims. A nearby `highway=raceway` polygon is not automatically the Grand Prix
surface. Each candidate is qualified independently:

- a source area must contain at least 95% of the selected lap;
- source linework must follow the lap within the renderer's bounded length and
  paper-distance tests;
- the resulting edge must remain at least three Grey nibs from the centreline
  at the selected paper scale.

Only a candidate passing both lap association and paper resolvability is drawn
as `track-boundary`. This rejects the unrelated motocross raceway near Austria
and the small unrelated raceway area near Monza while retaining the genuine
Interlagos asphalt multipolygon where it resolves. An omitted edge is recorded
in metadata; the Red corridor is never presented as evidence that the edge was
surveyed.

## Turns, sections, and operational claims

An apex is driver-, car-, setup-, condition-, and lap-dependent. It cannot be
recovered reliably from OSM curvature. Therefore:

- `A` means a true apex only when an explicit permitted source identifies it;
- `T` means a source-backed official turn station;
- `G` means a geometric registration station, not an official turn or apex;
- `S` means another source-tagged station;
- every available station is retained once, without a silent number cap;
- unsupported operational overlays are omitted and ledgered.

Black direction arrows appear only when current event-specific evidence
establishes lap direction. A Black start/finish gate appears only when a
coordinate-bearing source anchors it to the lap. When direction is not sourced,
the course card simply identifies the drawing as a `NORTH-UP COURSE PLAN`;
audit-only omission states stay in the manifest.

Named selected OSM lap ways provide the famous-section annotation layer. The
exact section geometry, source-name key, source copy, OSM object lineage, and
all fragments merged into a repeated label are retained. Names are case-folded
only for duplicate detection and have status
`osm-source-tagged-unverified-not-official`; they are not promoted to official
F1 corner nomenclature. Paper limits are four names on A5, seven on A4, and ten
on A3. Longer and well-distributed sections are selected deterministically, and
every paper-gated or collision-omitted name is recorded. Section names use 1.5
times the ordinary label-separation distance; the exact physical minimum is
serialized as `minimum_section_label_separation_mm`.

Operational terminology is document- and event-specific in 2026. DRS,
Straight Mode, detection/activation, or similar layers cannot be enabled by
vocabulary alone. A layer may appear only after the exact current-season event
document is frozen, versioned, source-bound, and permitted for that factual
use.

## Pen grammar

The mapped field uses these six stable physical pens, in order when present:

| Step | Pen | Use |
|---|---|---|
| 1 | Grey 0.25 | qualified track edges, roads, access roads, kerbs, runoff, gravel, and ordinary buildings |
| 2 | Green 0.25 | source-derived grass, park, and woodland outlines; no grass dots or stipple |
| 3 | Blue 0.25 | closed water dots clipped to sourced water, plus qualified shoreline outlines |
| 4 | Purple 0.40 | sourced pit lane, paddock, grandstands, garages, pit buildings, and principal venue structures |
| 5 | Red 0.40 | one exact source centreline plus the format-bound diagrammatic corridor offsets |
| 6 | Black 0.25 | labels, leaders, north mark, start/finish, direction, and stations |

Page furniture reuses Black 0.25 where possible and adds only the format-owned
Black copy/title weight required by `format-v1`: Black 0.60 on A5 and Black
0.40/1.00 on A4/A3. Field pens and furniture pens remain separate manifest
inventories. There is no physical orange pen in this contract.

All geometry is stroke-only. Grass is never represented by a dotted fill,
stipple, hatch, or repeated interior grass glyph. Selected grass, park, and
woodland features use Green source-derived boundary outlines. Blue water uses
closed, plotter-safe dots clipped inside sourced water geometry; a sourced
shoreline outline may remain where it survives clipping and the same density
gate. A polygon too small to contain even one closed 0.25 mm stipple mark is
omitted as a complete source feature instead of being misrepresented as
undotted water. Stands, runoff, gravel, and structures retain their own sourced
outline or sparse-mark grammar rather than digital fills.

Vegetation outline groups are indivisible: density selection retains or omits
the complete eligible source feature rather than leaving arbitrary fragments.
Each format reserves up to 0.025 mm/mm² inside the shared 0.17 design target
for those Green outlines before weaker general context is pruned. Retained
polygon water keeps its Blue stipple; water is culled only as a complete source
feature if the hard fallback requires it. The 0.18 hard gate remains
fail-closed. Source boundaries are clipped before conversion to visible
linework so the viewport edge cannot become an invented shoreline, park edge,
or building wall.

## Geographic context and negative paper

Every plate is north-up and fits the circuit plus its surrounding source
context into the binding map field:

- a street or park venue shows selected host roads, access roads, buildings,
  parks, vegetation outlines, shorelines, and dotted water;
- a permanent venue may show a qualified track edge, pit lane, paddock, stands,
  runoff, gravel, access roads, Green grass/woodland outlines, dotted water,
  and nearby structures;
- a hybrid venue uses the source-backed subset of both.

The v2.3 objective is richer surrounding context, not exhaustive context.
Selection prioritizes course-adjacent and named venue features, major host
roads, larger or named buildings, stands, meaningful vegetation, and water,
then removes weaker detail deterministically before the hard density limit can
be crossed. Missing source features remain missing; detail is never sketched or
duplicated merely to make a field look busy.

The current event registry freezes an explicit `atlas_context_mode` for every
event; it is not silently inferred from `site_type` at render time. Interlagos
is intentionally `hybrid` despite being a permanent circuit, because its atlas
needs both venue and meaningful surrounding urban fabric. A conflicting CLI
override is rejected. The A4 building gate is 60 source footprints, and the
density pass removes weak or small anonymous footprints before meaningful
named or larger buildings.

Grandstands have a deliberately narrower claim than other venue context. They
are frozen current-OSM `building=grandstand` footprints only: not a 2026 event
configuration, not an FIA seating plan, and not evidence that a stand is
temporary, operational, open, or present on race weekend. The top-level tags
must exactly match a versioned embedded OSM way/relation carrying the same
`building=grandstand` tag. A5, A4, and A3 select at most 10, 24, and 48 stands,
respectively, by in-viewport geometry, meaningful name, then stable feature ID.
The independent release QA recomputes that selection from the source geometry;
it does not trust renderer-authored counts or omission reasons. When stands are
drawn, the course-drawing card uses the concise scope `CURRENT MAP /
GRANDSTANDS`. Source provenance remains machine-readable in the manifest and
the one legally required map-data credit remains in the attribution zone; the
sheet does not turn internal verification vocabulary into display copy.

Matching host-road strokes are subtracted in source space beneath the Red lap
so they cannot double-print. No white or coloured halo path is emitted.

Turn/station labels are solved as a complete inventory. Named course-section
labels reserve space next, before ordinary context names. The global solver
rejects label/label, label/lap, label/pit, and leader collisions. Accepted label
boxes and leader corridors are then subtracted from eligible track-edge and
context linework. The resulting knockout is uninked paper, not a white stroke,
and source-boundary subtraction prevents a false rectangular map feature.

Suzuka's grade crossing remains non-nodal. Exact OSM way `175231434` must be a
contiguous run of the selected lap, carry affirmative `bridge` and non-zero
`layer` tags in both its embedded object and exact source record, and remain
bound to that lineage. Every independently recomputed lap self-intersection
must match exactly one such source-proven overpass. It produces a four-part
Black 0.25 deck bracket—two perpendicular terminals and two parallel rails—
after the uninterrupted Red pass. The cue is symbolic, never a white mask,
surveyed-width claim, or hidden Grey overlay. Monaco's tunnel remains a
distinct source-tagged section rather than an arbitrary dashed decoration.

Derived paired Red corridor passes also have an independent local-clearance
policy. Non-adjacent course legs separated by at least one quarter lap are
tested at paper scale; if their nominal drawn edges would approach more closely
than three Red nibs, only the derived offsets are locally clipped to bare
paper. The exact closed source centreline remains present and unchanged. The
manifest records every clearance zone and the QA suite recomputes both the
source-space candidates and the page-space masks without trusting that ledger.

Framing is fitted only from the lap plus sourced pit linework. Raw raceway
polygons and relations are qualification candidates for visible track edges,
not framing authority. This prevents a large or unrelated boundary—such as
Nürburgring relation `19275020`—from shrinking the Grand Prix course. QA
independently reconstructs the exact structural bounds, scale, north-up
transform, lap coordinates, scale denominator, and hero utilization from source
geometry. There is no additional percentage-based geographic margin: the
lap-plus-pit extent is contained at the largest scale that fits the sheet's
physical overlay-clearance rectangle, touching one of its two limiting axes.
The context viewport is calculated separately as the exact source rectangle
that maps to the complete map field, so surrounding detail may use spare paper
without shrinking the course or projecting outside the field.

## Responsive paper policy

All products use the same source model in exactly six formats:

- `a5-portrait`
- `a5-landscape`
- `a4-portrait`
- `a4-landscape`
- `a3-portrait`
- `a3-landscape`

The renderer changes selection density, corridor width, and label limits—not
geographic truth. Maximum course fitting takes precedence over discretionary
peripheral context; when the larger projected course increases physical ink,
whole unlabelled context features are removed by the existing deterministic
0.17 target / 0.18 hard density policy:

| Format | Required content policy |
|---|---|
| A5 | 0.8 mm course corridor; complete available station inventory; up to 6 sourced station names, 4 section names, and 4 context names; primary context, selected Green vegetation outlines, and dotted Blue water |
| A4 | 1.2 mm corridor; complete available station inventory; up to 12 sourced station names, 7 section names, and 10 context names; pit/venue structures and richer selected context below the hard density gate |
| A3 | 1.6 mm corridor; complete available station/name inventory where source-backed, up to 10 section names and 22 context names; maximum source-resolvable context within the density and physical-size gates, never exhaustive fill |

All serialized linework must satisfy the three-nib path floor. Field copy is at
least eight times its assigned nib width. Format identity is part of every
artifact ID, preventing portrait/landscape or paper-size overwrite.

## Source contract

`tools/acquire_f1_circuit_sources.py` freezes the bytes used by the current
catalog. `tools/build_f1_circuit_catalog.py` compiles offline and rendering has
no live-service fallback. Each OSM record includes stable object type, ID,
version, timestamp, tags, and frozen source reference when provided by the API.

Official fact records have field-level source binding. Length and `First Grand
Prix` retain the official page reference; `Fastest lap time` additionally
retains its exact time copy, integer milliseconds, driver, season, source label,
and claim scope. The builder accepts the fastest-lap field only when the visible
page copy and structured Formula 1 page data agree. A season results index,
current OSM tag, circuit illustration, or unsupported historical recollection
is not evidence for that field.

The canonical geometry digest is SHA-256 over the complete
`circuit.geometry.model` JSON after recursively removing
`geometry_sha256` and `source_geometry_sha256`, serialized with sorted keys,
compact separators, and non-finite numbers disabled. The digest is repeated in
the catalog, SVG metadata, plot manifest, and release index. A mismatch is a
hard failure.

The existing current-calendar OSM v1 snapshots remain immutable. Rail/tram,
footpath, parking/apron, barrier/fence, and spectator-bridge categories require
a new query version and snapshot identity; they must not be silently claimed
from a query that did not request them.

OpenStreetMap-derived work visibly retains:

`© OpenStreetMap contributors / openstreetmap.org/copyright`

Open map-data licensing does not clear circuit-outline, event-name, promoter,
venue, sponsorship, or merchandising rights.

## Former-F1 multi-era catalog

`src/city_map_plotter/data/f1-circuits-legacy-v1.json` is a separate
`legacy-f1-configurations` catalog. `configuration_reference_season` is the F1
era referred to by the plate; the catalog's top-level 2026 season is only its
release/freeze year.

The frozen ledger is a non-exhaustive, curated former-F1 review edition with 34
selected former/current-reference configurations: 19 have renderable
centreline models and 15 remain held. It is not a claim to contain every famous
historic Formula 1 configuration. The renderable set is
Imola, Hockenheim Grand Prix course, Fuji, Portimão, Mugello, Buddh, Istanbul,
Jerez, Watkins Glen, Sepang 2017, Nürburgring Grand Prix course, Donington,
Mosport, Bahrain 2025, Jeddah 2025, and four explicitly current-source-only
references: Nordschleife / F1 reference 1976, Brands Hatch Grand Prix / F1
reference 1986, Estoril / F1 reference 1996, and Kyalami / F1 reference 1993.
Paul Ricard and Magny-Cours fail the 1% length gate; the other 13 held records
have unavailable historic geometry. All held records stay in
`HELD-EVENTS.json` rather than receiving a modern or visually inferred
substitute.

Bahrain 2025 and Jeddah 2025 are former-calendar reference records, with length
discrepancies of 0.141223% and 0.246323% respectively. They are distinct from
the called-off 2026 Sakhir and Jeddah events in the current catalog's exclusion
ledger. Jeddah also retains its source warning about a selected segment carrying
an OSM Formula E `fixme`; it is not treated as official configuration evidence.

The four added current-source reference records pass the length gate at
0.408324% (Nordschleife), 0.530439% (Brands Hatch), 0.200133% (Estoril), and
0.066033% (Kyalami). Each has
`configuration_identity.current_surviving_equivalent=false`: it is a useful
current course study associated with the stated F1 reference year, not exact
period geometry. The separate exact-period Nordschleife 1976, Brands Hatch
1986, Estoril 1996, and Kyalami 1985 records remain held. Their current
reference plates do not clear or replace those holds, and their context remains
`snapshot-current-not-backdated`.

It permits three conservative configuration identities:

- `exact-historic-source`: the frozen OSM object itself identifies the historic
  era;
- `current-surviving-equivalent`: current geometry is used only when
  authoritative history supports the surviving configuration;
- `current-source-f1-reference`: current source geometry is a conservative F1
  reference course, without claiming exact historic equivalence.

Every renderable legacy model is `cartography-qualified-centreline`. A held
identity has a null model and cannot render; demolished, materially altered,
open, ambiguous, or merely similar modern layouts are never closed with an
inferred connector or substituted for the named historic course.

The FIA licensed-circuit list's `Left/Right` field describes pole position, not
lap direction. It is never used as direction evidence. Legacy direction stays
withheld unless an operator or another configuration-specific factual source
states it explicitly.

Context around a surviving course is current-source evidence, not a historic
reconstruction. Every such feature is marked
`snapshot-current-not-backdated`, and the visible subtitle begins
`CURRENT-SOURCE COURSE / F1 REFERENCE …`. Only an exact historic source may use
`HISTORIC SOURCE COURSE / …`. This prevents today's buildings, roads, stands,
or place names from being presented as a period map.

Legacy course facts are equally conservative. A renderable former-F1 plate
prints its bound published length and explicit `F1 REFERENCE` season. It does
not inherit the current-calendar `First Grand Prix` or `Fastest lap time` block;
the third card instead presents the relevant `CURRENT`, `HISTORIC`, or
`SURVIVING COURSE STUDY` edition. A future legacy fastest-lap claim would
require a separately frozen official source and explicit proof that the fact
belongs to the drawn configuration; neither a venue-level time nor a
modern-layout record may be substituted. No record is reconstructed by
comparing a hand-picked set of race results.

An exact-historic course can still use present-day surrounding context. Its
course-drawing card therefore says `CURRENT MAP / VENUE CONTEXT`, or `CURRENT
MAP / VENUE + GRANDSTANDS` when stand footprints are visible. QA derives the
complete expected stroke count from the frozen plotter font and measures its
physical extent, so a surviving single glyph carrying the full `data-copy`
attribute cannot satisfy the gate. The legal map-data credit is kept separate
and quiet at the bottom of the rail.

## Build and audit commands

Validate the current offline catalog:

```bash
.venv/bin/python -m city_map_plotter.f1_cli list
.venv/bin/python -m city_map_plotter.f1_cli validate
```

`--require-all-renderable` is the deliberate complete-catalog check and exits
non-zero while Madrid is held. To build every eligible current event and retain
Madrid in a machine-readable hold ledger:

```bash
.venv/bin/python tools/build_f1_circuit_series.py \
  --all-renderable \
  --catalog src/city_map_plotter/data/f1-circuits-2026.json \
  --format all \
  --dpi 254 \
  --output-dir review-output/f1-circuit-atlas-2026-v2.3-green-outline-water-facts \
  --qa-profile review \
  --generated-at 2026-08-11T00:00:00Z
```

`--all-renderable` is review-only. It selects every eligible normalized model
and writes `HELD-EVENTS.json` with the catalog count, selected IDs, every held
ID, source references, geometry state, generic reason codes, exact ordered
catalog hold reasons, configuration reference season, identity status, and
disclosure. It fails if any held record lacks a non-empty declared hold reason.
`SOURCES.json` mirrors the same exact list in each held event binding. `--all`
remains the exact all-record gate and refuses a partial catalog. Explicit
`--event` IDs are available for pilots.

Acquire, compile, and build the former-F1 catalog separately:

```bash
.venv/bin/python tools/acquire_f1_legacy_sources.py --all
.venv/bin/python tools/build_f1_legacy_catalog.py --check
.venv/bin/python tools/build_f1_circuit_series.py \
  --all-renderable \
  --catalog src/city_map_plotter/data/f1-circuits-legacy-v1.json \
  --format all \
  --dpi 254 \
  --output-dir review-output/f1-circuit-atlas-legacy-v2.3-green-outline-water-facts \
  --qa-profile review \
  --generated-at 2026-08-11T00:00:00Z
```

The series builder stages and atomically promotes editable master SVGs, PNG
previews, one-pen SVG jobs, plot manifests, contact sheets, gallery, source and
licence registers, pen-change guide, release index, checksums, semantic F1 QA,
generic format QA, and `HELD-EVENTS.json`. It promotes only after the selected
event × format Cartesian product has unique IDs and passes the selected review
gate.

Audit either package against its exact catalog:

```bash
.venv/bin/python tools/qa_f1_circuit_series.py \
  review-output/f1-circuit-atlas-2026-v2.3-green-outline-water-facts \
  --catalog-file src/city_map_plotter/data/f1-circuits-2026.json \
  --write-reports

.venv/bin/python tools/qa_f1_circuit_series.py \
  review-output/f1-circuit-atlas-legacy-v2.3-green-outline-water-facts \
  --catalog-file src/city_map_plotter/data/f1-circuits-legacy-v1.json \
  --write-reports
```

These are the promoted v2.3 technical-review packages. Both remain review-only
pending rights clearance and a physical plotted proof on the intended stock.

## Review history and current status

The immutable v1.2 review package at
`review-output/f1-circuit-atlas-2026-source-qualified-v1.2` remains the
historical baseline: 15 source-qualified events × six formats = 90 plates.
Semantic F1 QA and generic format QA passed all 90 artifacts, 975 checksums
replayed, and six contact sheets were generated. That package predates the v2
diagrammatic corridor, centreline-only publication tier, qualified boundary
test, and OSM section-name layer; it is not relabelled as v2.

The live v2.3-semantic current catalog has 22 renderable events: 15 full-source
models and seven visibly centreline-only models. Madrid is retained in the
catalog and in `HELD-EVENTS.json`, not approximated to fill the matrix. Sepang
remains visibly conditional even though its geometry model is source-qualified.

The current catalog SHA-256 is
`b117b4a2f0b40277417fd255d80f80b7dcc936e97f614e56e7095bdf7179746e`.
The earlier v2.2 current and legacy review directories remain historical
snapshots only. The promoted current v2.3 package has 132 master SVGs, 132 PNG
previews, 132 manifests, 995 one-pen SVG jobs, six contact sheets, and 1,407
replayed checksum entries. Its semantic and format QA both pass 132/132. The
promoted legacy package
`review-output/f1-circuit-atlas-legacy-v2.3-green-outline-water-facts` has 114
master SVGs, 114 PNG previews, 114 manifests, 856 one-pen SVG jobs, six contact
sheets, and 1,214 replayed checksum entries; both QA suites pass 114/114.
The dedicated F1 regression matrix passes 277/277 tests. These technical
passes still leave rights and physical plotted-proof holds open.

## Release gates

The independent F1 QA suite verifies, at minimum:

- one exact closed Red source centreline, source-coordinate and digest parity,
  and the required closed paired corridor passes at the bound paper width;
- no racing-line, surveyed-width, fake-apex, inferred-connector, tracing, raster
  image, digital fill, white ink, or SVG text claim;
- Green vegetation is emitted as source-derived outline groups, with no grass
  stipple/dots/interior grass symbols; Blue water dots remain closed, source
  clipped, and independently attributable;
- complete-model start/finish, turn/station, pit, and direction topology, or the
  exact visible omission disclosures permitted by the centreline-only tier;
- candidate track edges independently pass lap association and three-nib paper
  separation;
- named section copy remains source-tagged, non-official, source-lineage bound,
  collision-free, and paper-gated with omissions recorded;
- zero label/label, forbidden label/route, or leader collisions and real
  negative-paper context knockouts;
- zero duplicate host-road ink beneath the lap;
- catalog, SVG, manifest, index, source-object, and geometry-hash parity;
- current official length, venue-scoped `First Grand Prix`, and Formula 1-page
  `Fastest lap time` copy/source parity; malformed or absent facts are withheld,
  Madrid emits no plate, and legacy fastest-lap facts remain withheld;
- exact page size, safe zones, six-format IDs, physical pens, and split/master
  parity;
- bounded ink coverage, the 0.17 design-density target and 0.18 hard gate,
  deterministic whole-feature context omissions, pen-down and pen-up travel,
  pen count, and estimated plot time;
- current-calendar status, Sepang conditionality, legacy configuration identity,
  current-context disclosure, and complete held-event ledger integrity.
- independently recomputed context mode, stand viewport/priority/sheet-limit
  selection, complete stand omission partitions, and full plotted disclosure
  stroke inventory;
- independently recomputed non-adjacent-course local-clearance zones, open
  offset-group parity, and proof that only derived offsets enter those masks;
- lap-plus-pit-only framing parity and Suzuka's exact, source-bound four-part
  Black bridge bracket after an unchanged Red centreline.

Passing these technical gates still produces a review artifact. QA reports
`technical_pass` independently from `rights_hold`, `physical_proof_hold`, and
`commercial_release_authorized`. Production requires exact pen/paper
calibration, a plotted proof, and commercial rights clearance for the specific
artifact.
