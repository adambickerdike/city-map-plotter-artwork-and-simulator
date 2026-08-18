# Verified marathon course plates — 2026-08-16 v1

This is the corrected marathon cohort: **14 real vector course plates**, not
marathon-labelled city previews. Every example has a master SVG, a 254 DPI PNG,
a plot manifest, an accepted 42.195 km verification, a pinned OpenStreetMap
basemap, and retained organiser/route evidence.

No course was traced from a raster, converted from a photograph, or drawn by
the generator. London is extracted from the organiser-embedded Strava route;
Tokyo and Valencia come from organiser-linked KML; the GPX courses are exact
organiser downloads; Boston is verified OSM relation/11680552 checked against
the organiser's current course page/map.

Berlin and Sydney are labelled 2025 because those are the latest vector files
their organiser pages currently publish. The other organiser vectors are 2026.
This package is review-only: route-file redistribution rights, external map-data
attribution placement, physical pen calibration, and event-day change checks
remain release gates. The quadratic close-pair physical audit is also deferred
to the selected physical pen/stock proof; format, nib gating, PlotSim and SVG
structure are still checked in this digital cohort. The visual recipe is the
exact `output/marathon-series/marathon-style-lean.json` design the earlier good
course sheets used: full course framing, full qualifying city linework, the
same pen colours and the same `RACE COURSE` / measured distance / scale copy.
The course overprints the basemap without an optional clearance halo. Travel
ordering is deferred to the selected plotter/machine job rather than frozen
into these review masters.

| # | Plate | edition | binding format | measured km |
|---:|---|---:|---|---:|
| 1 | LONDON MARATHON | 2026 | a4-landscape | 42.329 |
| 2 | TOKYO MARATHON | 2026 | a4-landscape | 42.278 |
| 3 | VALENCIA MARATHON | 2026 | a4-landscape | 42.173 |
| 4 | BOSTON MARATHON | 2026 | a4-landscape | 42.417 |
| 5 | BERLIN MARATHON | 2025 | a4-landscape | 42.184 |
| 6 | SYDNEY MARATHON | 2025 | a4-portrait | 42.244 |
| 7 | MARRAKECH MARATHON | 2026 | a4-portrait | 42.841 |
| 8 | GENEVA MARATHON | 2026 | a4-landscape | 42.200 |
| 9 | ROTTERDAM MARATHON | 2026 | a4-portrait | 42.111 |
| 10 | FRANKFURT MARATHON | 2026 | a4-landscape | 42.372 |
| 11 | STOCKHOLM MARATHON | 2026 | a4-landscape | 42.047 |
| 12 | JAKARTA MARATHON | 2026 | a4-portrait | 42.817 |
| 13 | ZERMATT MARATHON | 2026 | a4-portrait | 42.587 |
| 14 | CÔTE D'AMOUR | 2026 | a4-landscape | 42.995 |

## Contents

- `plates/`: 14 SVG/PNG/plot-manifest triplets.
- `contact-sheets/`: complete and orientation-specific PNG/SVG sheets.
- `source-contract/routes/raw/`: exact downloaded evidence and route responses.
- `source-contract/routes/normalized/`: the only route files admitted to rendering.
- `source-contract/route-source-manifest.json`: source chain, hashes and transforms.
- `source-contract/route-verification.json`: measured-length/topology evidence.
- `source-contract/osm/`: exact pinned basemap responses.
- `source-contract/osm-source-manifest.json`: bbox/query/hash provenance.
- `code-and-contract/`: generator, course gate, renderer and binding plate spec.
- `LLM_HANDOFF.md`, `REPRODUCE.md`, `ATTRIBUTION.md`, `release-qa.json`, checksums.

The superseded `marathon-city-previews-2026-08-16-v3` cohort remains a valid
city-basemap study but is not a course-map source and is not used here.
