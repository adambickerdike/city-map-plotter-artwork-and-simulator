# F1 circuit atlas v1→v2.3 implementation audit

Original audit: 2026-08-09. Updated against the live v2.3 semantics, the
current catalog frozen on 2026-08-10, and the legacy catalog frozen on
2026-08-11. The two v2.3 technical-review packages were reproducibly built and
promoted on 2026-08-11; commercial rights and physical plotted-proof gates
remain open.

## Decision

The dedicated circuit-atlas architecture remains worth implementing. Version
2.3 materially improves the product: the course is now physically explicit at
every paper size, while the exact source centreline remains independently
verifiable. It also adds conservative famous-section labels, source-qualified
actual track edges, an honest centreline-only tier, Green vegetation outlines,
dotted water, richer density-bounded context, source-backed current course
facts, all-renderable review packaging, and a separate multi-era former-F1
catalog.

The earlier generic sports circuit concept was unsuitable because it had no
frozen calendar ledger, exact topology, venue-context contract, responsive
A-series policy, or protection against calling an inferred line a racing line,
track width, official turn, or apex.

This audit distinguishes implemented source, frozen evidence, catalog
eligibility, generated review artifacts, physical proofs, and commercial
authorization. A technical pass never grants rights or implies affiliation
with Formula 1, the FIA, a promoter, team, or circuit owner.

## Live implementation status

| Area | Status | Evidence |
|---|---|---|
| Dedicated renderer | Implemented in the v2 family with v2.3 semantics | `src/city_map_plotter/f1_circuits.py`; `circuit-atlas-v2`; `circuit-atlas-rendering/v2` |
| CLI | Implemented | `src/city_map_plotter/f1_cli.py`; `mapplot-f1` entry point |
| Current calendar ledger | Frozen | 23 records: 22 amended-WMSC events plus conditional Sepang; called-off Sakhir/Jeddah excluded |
| Current source-qualified geometry | Partial by evidence | 15 complete `source-qualified`; 7 `cartography-qualified-centreline`; Madrid held |
| Current renderable review matrix | Technical-review package promoted | `review-output/f1-circuit-atlas-2026-v2.3-green-outline-water-facts`; 132/132 plates pass semantic and format QA; complete-catalog `--all` remains held |
| Current official fact block | Implemented fail-closed | Official length, venue-scoped `First Grand Prix`, and Formula 1-page `Fastest lap time`; incomplete/conflicting facts withheld |
| Former-F1 catalog | Frozen | 34 records; 19 renderable centreline studies and 15 held; multi-era identities and current-context disclosures |
| Source freezers | Implemented | `tools/acquire_f1_circuit_sources.py`; `tools/acquire_f1_legacy_sources.py` |
| Offline compilers | Implemented | `tools/build_f1_circuit_catalog.py`; `tools/build_f1_legacy_catalog.py` |
| Six-format atomic packager | Implemented | `tools/build_f1_circuit_series.py`; explicit `--all-renderable`; `HELD-EVENTS.json` |
| Independent semantic QA | Implemented | `tools/qa_f1_circuit_series.py` |
| Current offline catalog validation | Passing | 23 structurally valid records; 22 renderer-eligible; full-series-ready correctly false |
| Historical v1.2 review | Technical pass | 15 events × six formats = 90/90 plates; six contact sheets; 975 checksum entries replayed |
| v2.3 artifact release | Technical-review packages promoted; commercial release held | 132 current and 114 legacy plates pass technical QA; rights clearance and physical pen/paper proof remain outstanding |

Only frozen or locally serialized evidence supports the table. A network result
that has not entered the hash-bound manifest is not release evidence.

## Current-calendar evidence result

The factual ledger contains 22 events in the FIA WMSC amended calendar plus the
announced Bahrain Grand Prix at Sepang. Sepang remains conditional on final
agreements and official approvals, including WMSC approval. The April Sakhir
and Jeddah rounds were called off and remain exclusion records rather than
plates. Madrid remains subject to homologation.

Primary evidence is recorded in
`docs/f1-circuits/SOURCE_AUDIT_2026-08-10.md` and frozen beneath
`contracts/f1-circuits-2026/`. Calendar status, circuit identity, geometry
qualification, rights, and plotted-proof status are independent fields.

Current geometry partition:

- **15 full source-qualified:** Albert Park, Shanghai, Gilles-Villeneuve,
  Monaco, Barcelona-Catalunya, Red Bull Ring, Silverstone, Spa-Francorchamps,
  Zandvoort, Monza, Baku, Sepang, Circuit of the Americas, Interlagos, and Las
  Vegas;
- **7 centreline-only:** Suzuka, Miami, Hungaroring, Singapore, Mexico City,
  Lusail, and Yas Marina;
- **1 held:** Madrid, whose selected closed source course differs from the
  published length by 2.021640%, above the strict 1% gate.

The current catalog SHA-256 is
`b117b4a2f0b40277417fd255d80f80b7dcc936e97f614e56e7095bdf7179746e`.
Madrid's official-page text does not clear its independent geometry hold and
must not result in a plate.

For each renderer-eligible current event, the fact block is restricted to the
official page's circuit length, `First Grand Prix`, and `Fastest lap time`
fields. The first-GP year is explicitly venue-scoped; it is not a configuration
debut. The fastest time preserves exact page copy, driver, season, and integer
milliseconds, but is not relabelled as an independently verified all-time lap
record. Missing placeholders, partial structured data, conflicting visible
copy, or malformed times are withheld rather than repaired from another source.

The centreline-only tier is not a degraded full model. It earns an exact closed
selected OSM lap, source-object lineage, official configuration identity, and a
published-length discrepancy no greater than 1%. Missing start/finish anchors,
turn/apex stations, pit topology, direction, and operational overlays remain
absent and are visibly disclosed. No lap order, nearest point, or curvature
heuristic silently replaces the missing evidence.

## What v2 guarantees

1. The Red layer contains exactly one source-coordinate-parity
   `lap-centreline`, with source lap hash, model hash, source object lineage,
   and coordinate counts serialized into metadata.
2. Closed paired Red offset passes derive from that exact line and resolve to
   the format's physical course width: 0.8 mm on A5, 1.2 mm on A4, and 1.6 mm
   on A3. They are explicitly diagrammatic—not a racing line or surveyed width.
3. Offset failure is a build hold. An open, missing, empty, or sub-three-nib
   pass never falls back to a duplicate centreline.
4. Actual Grey track edges are independently source-qualified. A raceway area
   must cover at least 95% of the selected lap; linework must follow the lap;
   every emitted edge must also resolve at three Grey nibs on paper.
5. True apex wording is rejected without explicit evidence. `T`, `A`, `G`, and
   `S` distinguish official turn, true apex, geometric registration station,
   and other source-tagged station claims.
6. Purple pit lanes are assembled only as exact unbranched source-endpoint
   chains whose outer endpoints join the lap. No snapping or connector is
   allowed.
7. Named selected OSM lap ways become collision-solved section labels with
   exact copy and object lineage. Their status remains
   `osm-source-tagged-unverified-not-official`; they do not become official F1
   corner-name claims.
8. Labels are globally collision-solved against labels, leaders, the lap, pit,
   and protected evidence. Accepted boxes and leader corridors are subtracted
   from track-edge and context linework as bare paper, never a white halo.
9. Matching street host roads are subtracted in source space beneath the Red
   course. No visible white or coloured halo path is emitted.
10. Water, vegetation, stands, buildings, roads, kerbs, runoff, gravel, and
    venue structures retain feature and source-object lineage. Grass, park, and
    woodland use Green source-derived outline groups with no grass dots,
    stipple, or repeated interior grass symbols. Water uses closed Blue dots
    clipped to source geometry. Source-boundary-first clipping prevents false
    crop-edge outlines.
11. Every plate is north-up and derives A5/A4/A3 portrait and landscape from
    the same source geometry. Format identity is part of every artifact ID.
12. Catalog, SVG metadata, plot manifest, source register, and release index
    repeat or bind the relevant catalog/source/model digests. Drift fails QA.
13. Current course facts are exact official-page transcriptions of length,
    venue-scoped `First Grand Prix`, and the page-labelled `Fastest lap time`.
    Legacy plates retain length plus `F1 REFERENCE` and withhold fastest lap.
    Unsupported records are never fabricated.

## Paper and pen findings

The field palette remains physically stable: Grey 0.25, Green 0.25, Blue 0.25,
Purple 0.40, Red 0.40, and Black 0.25. Page furniture adds only the Black title
and copy weights bound by `format-v1`. No preview-only orange pen or white ink
is introduced.

The v2 course uses the rowing-style physical emphasis the earlier F1 studies
lacked. A5 uses the exact centreline plus one paired 0.2 mm offset. A4 uses
paired 0.2/0.4 mm offsets, and A3 uses paired 0.3/0.6 mm offsets. The resulting
target widths are exact for a 0.40 mm Red nib while retaining a single source
centreline record for provenance and QA.

The renderer's edge association audit found two false-positive v1 candidates:
the Austria polygon represented a nearby motocross raceway and the Monza
polygon represented a small unrelated raceway area. Neither covered the
selected Grand Prix lap. Version 2 omits both rather than calling them asphalt.
Interlagos retains a genuine qualifying raceway multipolygon, subject to the
paper-scale separation test in each format.

The previous whole-package findings remain relevant: serialized 0.001 mm
coordinates—not in-memory geometry—decide the three-nib path floor. Green
vegetation boundaries are retained or omitted as complete eligible source
groups; grass has no dotted or stippled interior. Closed Blue water dots remain
clipped to source water. Richer roads, buildings, stands, venue structures,
vegetation, and water are admitted only inside the 0.17 design-density target,
with 0.18 as a hard failure gate. The compact A5 attribution stays inside its
canonical zone. These policies preserve useful context without saturating the
sheet.

## Section labels and negative-space findings

Famous-part copy is sourced from the exact selected OSM lap ways. Repeated
case-folded names are one claim; the longest, centrally distributed fragment is
the visible anchor while every contributing object remains in lineage. A5/A4/A3
admit at most 4/7/10 section names. The selector prioritizes physical length and
lap distribution rather than a manually curated marketing list.

Section labels reserve collision space after the complete turn/station layout
and before ordinary context names. Labels never cover their own evidence or the
course. Their boxes and leaders are real knockout masks for eligible background
linework. Because the renderer subtracts from original source boundaries, the
knockout cannot manufacture a rectangular lake, forest, building, or asphalt
edge. The section layer also uses 1.5 times the ordinary label-separation
distance and serializes the exact minimum for independent QA.

## Former-F1 configuration findings

The former-F1 catalog is deliberately multi-era. A record may be
`exact-historic-source`, `current-surviving-equivalent`, or the more conservative
`current-source-f1-reference`. A historic reference year in the title never
backdates current OSM context.

The final catalog has 34 records: 19 renderable
`cartography-qualified-centreline` models and 15 holds. The renderable identity
split is one `exact-historic-source`, seven `current-surviving-equivalent`, and
11 `current-source-f1-reference`. The hold set includes two exact-cycle
candidates outside the 1% length gate—Paul Ricard and Magny-Cours—and 13 records
whose historic geometry remains unavailable.

Bahrain 2025 and Jeddah 2025 complete the renderable set at 0.141223% and
0.246323% length discrepancy. They are separate former-calendar references,
not the called-off 2026 current-calendar rounds. Jeddah's conservative identity
retains the OSM Formula E `fixme` disclosure and does not treat that tag as
official F1 configuration evidence.

Four additional renderable records are deliberately current-source-only:
Nordschleife / F1 reference 1976 (0.408324%), Brands Hatch Grand Prix / F1
reference 1986 (0.530439%), Estoril / F1 reference 1996 (0.200133%), and Kyalami
/ F1 reference 1993 (0.066033%). All four set
`current_surviving_equivalent=false`, visibly disclose current-source context,
and do not claim a period reconstruction. Their separate exact-period historic
records—including Kyalami 1985—remain held and are not superseded by these
current reference plates.

Every current context feature is marked `snapshot-current-not-backdated`, and
the visible subtitle says `CURRENT-SOURCE COURSE / F1 REFERENCE …`. Only an
exact historic geometry source may say `HISTORIC SOURCE COURSE / …`. A held
configuration has a null model and review status `held`; demolished forest
courses, superseded layouts, unresolved street links, open source geometry, and
modern courses that merely resemble a historic one remain held rather than
being connected or substituted.

The v2.3 fact policy deliberately stops short of a legacy record database.
Renderable former-F1 plates show the bound published length and explicit F1
reference season. `First Grand Prix` and fastest-lap copy are not inherited from
current-calendar pages; the fastest-lap line remains withheld. Exact historic
times, modern-course times, qualifying laps, and a selectively compared set of
race-result pages cannot be promoted to a configuration record without new,
configuration-matched frozen evidence and an explicit schema change.

The legacy audit also corrected a potential semantic trap: `Left/Right` in the
FIA licensed-circuit list is pole-position side, not lap direction. The builder
does not use that field as direction evidence; direction remains withheld unless
a configuration-specific operator or other factual source states it.

The legacy source manifest contains 57 frozen sources, including 21 OSM
snapshots, with zero acquisition errors. Its deterministic catalog SHA-256 is
`0a170f3764fe772d50be89c62df2cb1e47e8ba4ac8af9a0169ae7be1aeae1614`.
The offline `--check` rebuild passes. Focused legacy tests bind the multi-era
scope, length gate, source evidence, current-reference/period-hold separation,
named-section semantics, deterministic inputs, and renderer hold behavior.

## Adversarial QA coverage

The v2 semantic contract includes positive and tamper cases for:

- open, disconnected, duplicated, or source-drifted lap geometry;
- source-centreline coordinate/hash parity and paired-corridor group parity;
- corridor target width, allowed pass radii, closed offsets, and forbidden
  centreline fallback;
- false racing-line, surveyed-width, apex, track-edge, and official-section
  claims;
- track-area lap coverage, edge association, and paper resolvability;
- missing, duplicated, or silently capped stations;
- source copy, punctuation, object lineage, and unsupported text;
- label/label, label/route, leader, negative-space, and host-road-overdraw
  collisions;
- visible halos, white ink, raster images, digital fills, SVG text, and
  non-physical pens;
- grass dots/stipple/interior-symbol regressions, incomplete Green vegetation
  outline groups, invalid/open Blue water dots, and source-geometry escape;
- page-zone escape, serialized sub-three-nib marks, title-nib envelopes, and
  format overwrite;
- master/split-pen divergence, pen order, ink coverage, density, pen travel,
  plot time, and hero continuity;
- official fact source-copy/structured-data parity, venue scope for `First
  Grand Prix`, exact `Fastest lap time` parsing, fail-closed withholding,
  Madrid non-emission, and legacy fastest-lap non-publication;
- 23-record current-ledger integrity, Sepang conditionality, Madrid hold,
  multi-era identity semantics, and `HELD-EVENTS.json` parity.

## Packaging and review history

`tools/build_f1_circuit_series.py --all-renderable --qa-profile review` is the
canonical partial-catalog path. It emits every eligible model and preserves all
omitted records in `HELD-EVENTS.json`. The ordinary `--all` option remains the
exact complete-catalog gate and refuses any held record. The same builder is
catalog-driven, so current and former-F1 catalogs retain distinct release IDs.

The corrected historical v1.2 package remains at
`review-output/f1-circuit-atlas-2026-source-qualified-v1.2`. It contains 90
editable masters, 90 PNG previews, 90 plot manifests, 690 one-pen jobs, six
contact sheets, source/licence registers, a pen-change guide, semantic and
generic-format QA reports, and 975 replayed checksum entries. Its eight held
events were Suzuka, Miami, Hungaroring, Madrid, Singapore, Mexico City, Lusail,
and Yas Marina.

That result is preserved as history rather than rewritten. The live v2.3-semantic catalog
now renders seven of those eight as explicitly centreline-only studies; Madrid
remains held. A v2.3 package must carry `circuit-atlas-v2` identities and pass the
new corridor, boundary, section-label, hold-ledger, and legacy-aware gates. The
v1.2 artifacts do not acquire those claims retroactively.

The local v2.2 current and curated former-F1 review directories remain
historical evidence for the earlier visual contract. They are not renamed,
recounted, or treated as proof of v2.3's vegetation, water, context, or fact
semantics. The current v2.3-semantic catalog SHA-256 is
`b117b4a2f0b40277417fd255d80f80b7dcc936e97f614e56e7095bdf7179746e`.

The promoted current package contains 132 master SVGs, 132 PNG previews, 132
manifests, 995 one-pen SVG jobs, six contact sheets, and 1,407 replayed checksum
entries. The promoted legacy package contains 114 master SVGs, 114 PNG
previews, 114 manifests, 856 one-pen SVG jobs, six contact sheets, and 1,214
replayed checksum entries. Semantic and generic format QA pass every plate in
both packages, and the dedicated F1 regression matrix passes 277/277 tests.
Historical v1.2/v2.2 totals must not be quoted as v2.3 results; rights clearance
and physical plotted-proof gates remain open.

This pass does not manufacture missing facts. Eight current circuits and six
renderable legacy circuits have no source-qualified named course section, all
`Gxx` marks remain geometric stations rather than official turns or apexes,
context is density-selected rather than exhaustive, and Lusail/Yas Marina have
no qualified pit geometry. Those are explicit review-scope limits, not claims
silently filled by inference.

## Remaining release loop

For each current or former venue:

1. freeze the exact official facts and selected OSM circuit/context bytes;
2. verify visible/structured official fact parity, payload hashes, and compile
   offline;
3. hold the record if configuration identity, exact closure, the 1% length
   gate, or the claimed topology tier is not earned;
4. render a pilot format and run semantic plus generic format QA;
5. inspect whole page and map field for hierarchy, context relevance, Green
   vegetation outlines without grass dots, dotted Blue water, hard-density
   compliance, broken offsets, false boundaries, and text/leader collisions;
6. change only renderer selection/layout policy—never source geometry—to repair
   a visual failure;
7. render all six formats, replay QA and checksums, and perform a physical
   plotted proof;
8. attach rights clearance to the exact artifact before any commercial use.

## Release boundary

The system is suitable for source-backed design development and pen-plotter
proofs. It is not authorization to sell circuit merchandise. OpenStreetMap
licensing, circuit-outline and venue rights, event naming, sponsorship, and
Formula 1 intellectual-property rules are separate questions. Every emitted
manifest therefore remains `review-only` until exact physical pen calibration,
a plotted proof, and rights clearance are attached to that artifact.
