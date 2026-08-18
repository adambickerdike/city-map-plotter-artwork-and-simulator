# 2026 F1 source and geometry audit — 10 August 2026

## Decision

The frozen series is a 23-record factual model, not a claim that 23 events
have WMSC approval:

- 22 events are listed in the FIA World Motor Sport Council calendar.
- The Bahrain Grand Prix at Sepang on 2–4 October is an announced replacement
  subject to final agreements and official approvals, including WMSC approval.
- The original Bahrain/Sakhir and Saudi Arabia/Jeddah April rounds were called
  off and remain in the exclusion ledger, not the plate series.
- Madrid remains subject to circuit homologation in the FIA calendar.

Primary calendar evidence:

- [FIA original 2026 calendar](https://www.fia.com/news/fia-and-formula-1-announce-2026-calendar)
- [FIA WMSC amended 2026 calendar](https://www.fia.com/news/2026-fia-sporting-calendars-approved-world-motor-sport-council)
- [F1 Bahrain and Saudi Arabia cancellation](https://www.formula1.com/en/latest/article/bahrain-and-saudi-arabian-grands-prix-will-not-take-place-in-april.1hnqllVG85RSt8pbFc5Ivx.1hnqllVG85RSt8pbFc5Ivx)
- [F1/FIA conditional Sepang announcement](https://www.formula1.com/en/latest/article/formula-1-and-fia-confirm-malaysia-will-join-2026-calendar-as-host-venue-for-bahrain-grand-prix.6lL7vjFEM2VVynRHvg1TCf)

The source registry, exact retrieved payloads, hashes, and source-use metadata
are frozen in `contracts/f1-circuits-2026`. No live network fallback is allowed
during catalog building or rendering.

## Honest geometry tiers

The catalog now separates two earned geometry claims:

1. `source-qualified` — the existing complete normalized-model gate.
2. `cartography-qualified-centreline` — an exact closed selected OSM
   centreline, source-object lineage, an official Formula 1 configuration
   identity, and an official-length discrepancy no greater than 1%. This tier
   makes no implied claim about start/finish placement, official turn/apex
   stations, pit topology, lap direction, or event-specific operational
   overlays. Every omission remains explicit in the model and must be visibly
   disclosed by the renderer.

Earned counts are 15 `source-qualified`, 7
`cartography-qualified-centreline`, and 1 unresolved. Calendar and homologation
approval remain independent release gates.

## Eight-circuit audit

| Circuit | Closed OSM lap / official length | Direction evidence | Current FIA event document | Earned tier | Material hold or omission |
|---|---:|---|---|---|---|
| Suzuka | 5806.544 m / 5807 m (0.007853%) | Withheld: official F1 value is `8`, describing figure-eight topology rather than lap orientation | FIA v3, 25 Mar 2026 | `cartography-qualified-centreline` | No coordinate-bearing reusable S/F or turn anchors; direction withheld |
| Miami | 5409.786 m / 5412 m (0.040909%) | Official F1 `Anti-Clockwise` | FIA v5, 30 Apr 2026 | `cartography-qualified-centreline` | No coordinate-bearing reusable S/F or turn anchors |
| Hungaroring | 4366.616 m / 4381 m (0.328327%) | Official F1 `Clockwise` | FIA v2, 21 Jul 2026 | `cartography-qualified-centreline` | No coordinate-bearing reusable S/F or turn anchors |
| Madrid | 5525.492 m / 5416 m (2.021640%) | Official F1 `Clockwise` | Not yet available | `provisional` / model withheld | Exceeds 1% gate; FIA homologation pending; pit and anchors unqualified |
| Singapore | 4967.421 m / 4927 m (0.820398%) | Official F1 `Anti-Clockwise` | Not yet available | `cartography-qualified-centreline` | No coordinate-bearing reusable S/F or turn anchors |
| Mexico City | 4311.742 m / 4304 m (0.179879%) | Official F1 `Clockwise` | Not yet available | `cartography-qualified-centreline` | No coordinate-bearing reusable S/F or turn anchors |
| Lusail | 5425.656 m / 5419 m (0.122827%) | Official F1 `Clockwise` | Not yet available | `cartography-qualified-centreline` | Ambiguous/unjoined pit candidates withheld; no reusable anchors |
| Yas Marina | 5296.895 m / 5281 m (0.300985%) | Official F1 `Anti-Clockwise` | Not yet available | `cartography-qualified-centreline` | Pit candidate fails exact lap joins and is withheld; no reusable anchors |

The three current event PDFs are frozen only as factual reference documents:

- [2026 Japanese GP circuit document](https://www.fia.com/system/files/decision-document/2026_japanese_grand_prix_-_competition_notes_-_circuit_map_pit_lane_drawing_emergency_exits_map_battery_containment_area_and_red_zone.pdf)
- [2026 Miami GP circuit document](https://www.fia.com/system/files/decision-document/2026_miami_grand_prix_-_competition_notes_-_circuit_map_pit_lane_drawing_emergency_exits_map_and_red_zone.pdf)
- [2026 Hungarian GP circuit document](https://www.fia.com/system/files/decision-document/2026_hungarian_grand_prix_-_competition_notes_-_circuit_map_pit_lane_drawing_emergency_exits_map_and_red_zone.pdf)

They are marked `prohibited-reference-only-no-tracing`. No FIA/F1 graphic is
used as a geometry source or traced into reusable coordinates.

## Layout facts retained without false precision

- Suzuka: 18 turns; F1-described S Curves, Degner Curves, 130R and Hairpin.
- Miami: 19 turns; F1-described Turn 14–15 chicane.
- Hungaroring: 14 turns and the circuit's 2026 official names: Piquet,
  Hamilton, Spring, Mansell, Mogyoród, Driving Center, Buda/Pest, Danube,
  Alesi, Schumacher, Senna and Szisz.
- Madrid: 22 turns; official circuit source identifies La Monumental as Turn
  12, but no coordinate anchor is claimed.
- Singapore: 19 turns in the current configuration.
- Mexico City: 17 turns; F1 describes Peraltada and the stadium section.
- Lusail: 16 turns; the main straight is source-described.
- Yas Marina: 16 turns; the official circuit describes North Hairpin, Marsa
  Corner and Hotel Section.

Named OSM lap ways are preserved separately as
`osm-source-tagged-unverified-not-official`, with exact way geometry and OSM
object lineage. Whole-circuit name repetition is suppressed. These labels do
not become official corner-name claims unless a separate authoritative source
supports that claim.

## Remaining holds

- Madrid needs a homologated current configuration and a centreline source
  within the length gate before its model can be rendered.
- Sepang may be rendered at its earned geometry tier only with an explicit
  `announced-pending-WMSC` status; it must not be presented as WMSC-confirmed.
- Future event operational overlays remain withheld until the current 2026 FIA
  event document is available and frozen.
- A production release still requires circuit-outline legal clearance and
  physical pen calibration/proof. These gates were not weakened by the
  centreline-only cartography tier.

## Context-source v2 note

Rail/tram, footpath, parking/apron, barrier/fence, and spectator/bridge context
should be acquired under a new versioned Overpass query and snapshot identity.
The existing v1 snapshots remain immutable; new categories must not be silently
claimed from data that the v1 query did not request.

## Context and rendering release clarifications — 11 August 2026

Every current-event registry record now freezes one explicit
`atlas_context_mode` (`permanent`, `urban`, or `hybrid`). This choice is source
catalog state, not a renderer heuristic or permitted release override.
Interlagos is explicitly `hybrid` while its physical `site_type` remains
`permanent`; the A4 proof retains the deterministic 60-building context gate
and remains below the 0.18 field-density ceiling.

Grandstand geometry is observational only. A record is eligible only when its
top-level tags and a versioned embedded OSM way/relation agree exactly on
`building=grandstand`, and it carries the frozen-current temporality and
non-operational claim scope. It has no `valid_for_season`, event-configuration,
FIA-configuration, or operational claim. Selection is independently replayed
from the source footprint against the plotted source viewport, meaningful-name
then ID priority, and fixed limits of A5=10, A4=24, A3=48. Every selected stand
must either emit Purple 0.40 source footprint ink or carry an explicit
density/geometry omission; every source-unselected stand must have the exact
outside-extent or paper-count reason. Those detailed audit states remain in the
manifest. The sheet itself uses the restrained course-drawing scope `CURRENT
MAP / GRANDSTANDS`, with source provenance carried by the single legal credit
in the attribution zone.

The source fit uses lap and pit geometry only. Unqualified raw raceway
boundaries are excluded from framing, including Nürburgring relation
`19275020`. No percentage-based geographic margin is applied: the structural
extent fills one axis of the physical overlay-clearance rectangle exactly.
The context viewport is independently inverse-mapped from the complete map
field. QA reconstructs both extents, the exact fit scale, denominator,
north-up source-to-paper transform, serialized lap coordinates, and hero
utilization.

Suzuka way `175231434` is the only source object permitted to produce its
grade-separation cue. The embedded tags must exactly match the selected lap
object, its geometry must be an exact contiguous cyclic subsequence, and it
must carry both an affirmative bridge tag and non-zero layer. The renderer
adds two fixed 1.25 mm Black 0.25 perpendicular endpoint bars after the
unbroken Red course. They make no track-width claim and use no white ink.

Legacy exact-historic course geometry does not backdate present-day surrounding
context. Such plates use `CURRENT MAP / VENUE CONTEXT`, or `CURRENT MAP / VENUE
+ GRANDSTANDS` when a stand footprint appears, in the course-drawing card. The
release gate derives the scope copy's exact path count from the frozen
stroke-font contract and measures its union extent; a partial or one-glyph
serialization fails even if it retains the full metadata copy string.
