# Hiking `route_plate` audit and production plan

**Date:** 2026-08-02

**Status:** implementation specification; ten commercial candidates frozen for prototype production

**Default format:** A5 portrait `route_plate`

**Scope:** hiking-route art only. These plates are not navigation products.

## Decision

The hiking branch is worth building. It fits the existing plotter system unusually well: a route is naturally a single physical stroke, contours produce attractive low-density texture, an elevation profile adds personal meaning without requiring raster imagery, and one template can support both a curated catalogue and customer-supplied GPX files.

The safe product is **not** “a prettier hiking map.” It is an authored, reproducible `route_plate` made from a licensed route trace, a licensed elevation model, a deliberately sparse context layer, and a recorded transformation pipeline. That distinction matters commercially. Some famous trail names and alignments are protected or licensed separately from the underlying geography. In particular, French GR/GRP route alignments and marks must not enter the sellable catalogue merely because similar linework can be found in an open map database.

Ten candidates are approved here for prototyping:

1. West Highland Way
2. Hebridean Way — walking route
3. Great Glen Way
4. John Muir Way — walking route
5. Via Alpina, SwitzerlandMobility Route 1
6. Alpine Passes Trail, SwitzerlandMobility Route 6
7. Tour des refuges en Vallouise en 5 jours
8. Tour du Pic de Valsenestre
9. Laugavegur
10. Camino Francés, official `ES01c` canonical stage chain

They are “commercial candidates,” not a claim of final legal clearance. Before sale, each needs the licence evidence, data snapshot, attribution, physical pen calibration, plot inspection, and release checklist defined below. No candidate should be advertised for navigation or safety use.

## What “ready to sell” means

A route plate reaches release state only when all of the following are true:

- Route identity, direction, endpoints, variant policy, retrieval time, version and checksum are frozen.
- Every geometry and elevation source has a licence that permits the intended commercial use; all required attributions appear on the art and in the accompanying product record.
- The route is derived from vectors, not traced from a screenshot or commercial map tile.
- Route discontinuities, ferries, alternates and out-and-back sections are represented honestly.
- The A5 SVG passes the repository format validator with zero errors and the plot simulation stays inside the agreed density and travel limits.
- The SVG has been converted to absolute `M/L/C/Z` paths, contains no live text, arcs, raster images, hidden geometry or empty layers, and uses millimetres one-to-one.
- A plot on the actual paper with the actual stocked pens has passed visual and dimensional inspection.
- Product copy says “decorative route artwork — not for navigation” and does not imply that the route is current, complete, open, safe or passable.

## Binding A5 `route_plate` contract

This specification inherits `docs/format/FORMAT.md`; the values below are not a second format system.

### Page and format zones

| Item | A5 portrait value |
|---|---:|
| page | `148 × 210 mm` |
| safe inset | `6 mm` |
| content inset | `12 mm` |
| title zone | `x=12, y=12, w=124, h=12.600 mm` |
| subtitle zone | `x=12, y=27.600, w=124, h=4.522 mm` |
| map field | `x=12, y=35.122, w=124, h=128.924 mm` |
| furniture zone | `x=12, y=167.046, w=124, h=5.426 mm` |
| detail zone | `x=12, y=175.473, w=124, h=15.523 mm` |
| attribution zone | `x=12, y=193.996, w=124, h=4.004 mm` |
| field area | `15,986.576 mm²` |
| hard maximum plotted coverage | `4,476.241 mm²` / `28%` |
| production target | `≤24%` to retain validator and real-ink headroom |

Use the standard heavy double border. Do not add a scale bar, north arrow, OpenStreetMap wordmark, map-tile watermark or cartographic legend unless a particular plate has a real interpretive need. Required source attribution is plain small text in the detail/attribution zones, never removed.

### Internal map-field composition

The following subdivision is internal to the `map_field`; it does not create new page-format zones:

| Subfield | Rectangle | Use |
|---|---|---|
| route map | `x=12, y=35.122, w=124, h=98.000 mm` | north-up route, terrain, hydrography, labels and stage marks |
| breathing gap | `x=12, y=133.122, w=124, h=3.000 mm` | completely unplotted |
| elevation profile | `x=12, y=136.122, w=124, h=27.924 mm` | route-distance/elevation line and restrained annotations |

The route-map working rectangle is inset a further 2 mm on all sides. Expand the projected route bounding box to the working rectangle’s aspect ratio and then add 5% route padding. Never trim the route merely to make the composition fuller. The profile samples x from cumulative geodesic source chainage and elevation from the declared route source or DEM; do not derive either from paper-space coordinates. A–E labels may use a published route total only as a proportional display scale (`PUBLISHED KM`), while the simplified geometry measurement remains separate metadata. Without a published total, use `MEASURED KM`. Unverified profile extrema must be labelled `SAMPLED ELEVATION / APPROX`.

Zone content is also fixed. The title is the route name. The subtitle is `START — FINISH · VARIANT · DISPLAY DISTANCE`, omitting any disputed value. The furniture line contains only verified metrics, normally `DISTANCE · ASCENT · STAGES/DAYS`. The detail zone allows up to three short lines for source edition, variant, segment/safety and provenance facts. The attribution zone is reserved for the mandatory compact licence/source line; when several credits cannot fit legibly, move secondary credits into the detail zone rather than reducing type below minimum size.

Title cap height is `7.000 mm`; subtitle is `2.261 mm`; detail text is `2.352 mm`; labels/legend are `2.261 mm`; attribution is `2.002 mm`. All lettering is converted to plot-ready single-line paths. Preserve accents and diacritics exactly.

### One shared physical pen plan

The stock-aware plan below is the default for every candidate in this audit. A plate can omit an empty colour, but it may not renumber around an undeclared layer or revisit a pen later.

| Plot order | Logical pen | Nib | Layer role | Production rule |
|---:|---|---:|---|---|
| 1 | Grey | `0.25 mm` | contours, terrain hachures | sparse; never simulate shading with dense fills |
| 2 | Blue | `0.25 mm` | coast, lakes, rivers and ferry connectors | water only; minimum dash/on segment `≥0.75 mm`, target `≥1.50 mm` |
| 3 | Black | `0.25 mm` | context lines, stage/control marks, labels, elevation profile, detail and attribution | all small type at or above the format cap-height minima |
| 4 | Black | `0.60 mm` | title and standard double border | title cap `7.0 mm`, comfortably above the `4.8 mm` physical minimum |
| 5 | Red | `0.40 mm` | hero route | one centreline, round joins/caps, plotted last; no doubled “road” rails |

This is five distinct pen loads with one contiguous visit to each pen. It is a one-pass, zero-offset plan. The physical inventory ID, stock count, measured effective mark, calibration card and substrate must be bound at production time; a colour/nib name in this document is not proof that a usable pen is on hand.

Minimum plotted feature lengths are `0.75 mm` for 0.25, `1.20 mm` for 0.40 and `1.80 mm` for 0.60. Suggested field coverage allocation is 13% contours/terrain, 2.5% hydrography, 2.5% black context and labels, 2% route, and at least 4% uncommitted safety headroom beneath the 24% production target. Simplification must reduce pointless pen motion, not erase route topology.

## Source and rights architecture

### Separate four source roles

Every plate record must distinguish:

1. **Identity source:** confirms the trail’s public name, direction, approximate length and stage concept.
2. **Geometry source:** supplies the line actually drawn.
3. **Elevation source:** supplies contours and the elevation profile.
4. **Context source:** supplies selected coast, river, lake, settlement or path features.

An official tourism page can be a good identity source while still being unsuitable as geometry. Never download or trace an official site’s illustrated map unless its licence explicitly permits that use. Likewise, an open elevation model does not grant rights to a separately protected route alignment or trail mark.

### OpenStreetMap rule

Seven candidates below use a frozen OpenStreetMap route relation, and most use selected OSM context vectors. OpenStreetMap is licensed under ODbL; attribution is mandatory. The public copyright page is the canonical notice: [OpenStreetMap copyright and licence](https://www.openstreetmap.org/copyright). For a printed or SVG Produced Work, include a readable line such as:

`© OpenStreetMap contributors · openstreetmap.org/copyright`

The [OSMF Produced Work guideline](https://osmfoundation.org/wiki/Licence/Community_Guidelines/Produced_Work_-_Guideline) and [licence FAQ](https://osmfoundation.org/wiki/Licence/Licence_and_Legal_FAQ) should be kept in the release evidence. A build must archive the relation version, members, member roles/order, retrieval timestamp and checksum, plus the exact context query. If the build makes a derivative database rather than only a Produced Work, publish that database under ODbL or publish reproducible extraction/alteration instructions that satisfy the obligation. This should be reviewed once by counsel before catalogue launch.

Do not use the standard OSM raster tiles, screenshots, Google Maps imagery, Strava heatmaps, Komoot maps or similar rendered maps as production input.

For a reproducible relation snapshot, query the named relation at a recorded replication time and recurse through relation, way and node members with metadata (the Overpass pattern is `relation(<id>); (._; >>;); out meta;`). Archive the raw response before normalization. A plain `/relation/<id>` response or the relation’s tags alone is not sufficient for nested stage relations. The public relation pages for the selected sources are [16287](https://www.openstreetmap.org/relation/16287), [7610425](https://www.openstreetmap.org/relation/7610425), [126572](https://www.openstreetmap.org/relation/126572), [49215](https://www.openstreetmap.org/relation/49215), [12359033](https://www.openstreetmap.org/relation/12359033), [18021781](https://www.openstreetmap.org/relation/18021781) and [1225037](https://www.openstreetmap.org/relation/1225037).

### Elevation and national data sources

| Territory | Approved elevation source | Commercial-use evidence and required credit |
|---|---|---|
| Great Britain | [OS Terrain 50](https://www.ordnancesurvey.co.uk/products/os-terrain-50), 50 m DTM / supplied 10 m contours | OS OpenData terms; use `Contains OS data © Crown copyright and database right 2026`. See [OS OpenData](https://www.ordnancesurvey.co.uk/products/open-data) and [product support](https://www.ordnancesurvey.co.uk/products/product-support). |
| Switzerland | [swissALTI3D](https://www.swisstopo.admin.ch/en/height-model-swissalti3d), actual OGD tiles, resampled to 25 m for artwork | Free geodata permits commercial use with source credit: `Federal Office of Topography swisstopo` or `©swisstopo`; retain [terms](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices) and [FAQ](https://www.swisstopo.admin.ch/en/faq-free-geodata). Product-page sample files are test material and must not be embedded in a product. |
| France | IGN RGE ALTI, actual 5 m tiles resampled to 25 m | Retain the [RGE ALTI specification](https://geoservices.ign.fr/sites/default/files/2021-07/DC_RGEALTI_2-0.pdf) and the [IGN open-data policy](https://www.ign.fr/institut/des-donnees-et-logiciels-ouverts-au-service-de-la-nation); record the exact Etalab/Open Licence notice shipped with the tiles. Do not substitute SCAN 25 raster cartography. |
| Iceland | IS 50V elevation in EPSG:3057 | [Náttúrufræðistofnun base-map data](https://www.natt.is/en/resources/geospatial-data/base-map-data), under [CC BY 4.0 open-data terms](https://www.natt.is/en/resources/open-data). Credit dataset, author and licence, e.g. `“IS 50V Elevation” by Náttúrufræðistofnun · CC BY 4.0`. |
| Spain | PNOA-LiDAR MDT25 second coverage, 25 m COG | [Official MDT description](https://pnoa.ign.es/pnoa-lidar/modelo-digital-del-terreno) and [download products](https://pnoa.ign.es/ca/web/portal/pnoa-lidar/productos-a-descarga). Record the exact file-detail credit; current derivative wording is of the form `Obra derivada de MDT25-cob2 2015–2021 · CC BY 4.0 · scne.es`. |

Year strings must be generated from the current licence/source requirement at build time, not copied blindly from this audit.

### France rights red line

The Fédération Française de la Randonnée states that GR/GRP routes and associated marks are protected and that third-party reproduction or exploitation requires a written arrangement. See its [federal intellectual-property notice](https://www.ffrandonnee.fr/la-federation/qui-sommes-nous/la-propriete-intellectuelle-federale) and [MaRando terms](https://www.ffrandonnee.fr/conditions-generales-d-utilisation-ma-rando). The Tour du Mont Blanc site also reserves its site content under its [legal notice](https://www.montourdumontblanc.com/en/legal-notice.html).

Therefore GR20, GR10, GR65-labelled products, GRP products, and Tour du Mont Blanc are placed in a **deferred rights queue**. Do not sell them until written permission or a route-specific legal assessment confirms what geometry, name and marks may be used. An OSM or data.gouv copy of similar geometry does not erase third-party route rights.

The two French prototypes selected here come from the Parc national des Écrins open GeoJSON and currently have `balisage=null`; two otherwise attractive alternatives were rejected because their data identified GR/GRP marking. The selected records still need final commercial clearance against the dataset licence and any local route rights. Use only the geometry and factual fields licensed with the dataset; do not reuse page photographs or descriptive prose, which carry separate rights.

The data.gouv record marks the route dataset as Licence Ouverte / Open Licence 2.0 and exposes the live [Écrins GeoJSON](https://data.ecrins-parcnational.fr/files/randos_pne_schema.geojson). The release evidence must retain that dataset record and its licence, not merely the direct download URL. Use an attribution such as `Source: Parc national des Écrins — Randonnees du Parc national des Écrins · Licence Ouverte 2.0`, normalized to the exact dataset title/credit at build time.

## Frozen catalogue

| ID | Plate | Exact variant and direction | Frozen primary geometry | Prototype rights state |
|---|---|---|---|---|
| `RTE-GB-WHW-01` | West Highland Way | Milngavie → Fort William | OSM relation `16287`, v345, 2026-04-11 | amber: ODbL workflow + official identity check |
| `RTE-GB-HEB-WALK-01` | Hebridean Way | walking route, Vatersay → Stornoway | OSM relation `7610425`, v81, 2026-07-07 | amber: ODbL; ferries must remain segmented |
| `RTE-GB-GGW-01` | Great Glen Way | Fort William → Inverness, current main route | OSM relation `126572`, v157, 2026-07-01 | amber: current improvement and distance conflict |
| `RTE-GB-JMW-WALK-01` | John Muir Way | walking relation only, Helensburgh → Dunbar | OSM relation `49215`, v379, 2026-01-20 | amber: do not merge cycling braids |
| `RTE-CH-VA1-01` | Via Alpina | Route 1, Vaduz/Gaflei → Montreux | OSM relation `12359033`, v12, 2026-01-10 | amber: ODbL + SwitzerlandMobility identity |
| `RTE-CH-AP6-01` | Alpine Passes Trail | current Route 6, Corviglia/St. Moritz → Saint-Gingolph VS | OSM relation `18021781`, v30, 2025-10-27 | amber: old relation is deleted; current chain only |
| `RTE-FR-ECR-976000` | Tour des refuges en Vallouise | five-day Ailefroide loop | Écrins UUID `9caad71a-…22ab`, modified 2026-07-06 | green/amber: open dataset; final local-rights check |
| `RTE-FR-ECR-995181` | Tour du Pic de Valsenestre | La Chapelle-en-Valjouffrey loop | Écrins UUID `090e3adf-…e94f`, modified 2026-01-16 | green/amber: open dataset; final local-rights check |
| `RTE-IS-LAUG-01` | Laugavegur | Landmannalaugar → Þórsmörk | OSM relation `1225037`, v48, 2025-09-13 | amber: ODbL + official identity check |
| `RTE-ES-CAM-ES01C` | Camino Francés | `ES01c`, Puente la Reina → Santiago, canonical `a` stages | CNIG/FEAACS official GPX/SHP, current 2026 edition | green/amber: CC BY 4.0; variant filter is critical |

All OSM versions above were rechecked against the live OSM API on 2026-08-02. Each build must fetch and freeze a new snapshot rather than silently assuming these versions remain current.

## Ten production plate briefs

### 1. `RTE-GB-WHW-01` — West Highland Way

**Identity.** Use the title `WEST HIGHLAND WAY` and subtitle `MILNGAVIE — FORT WILLIAM · 154 KM`. The [official route site](https://www.westhighlandway.org/the-route/) identifies the route as 96 miles / 154 km and is an identity check only. Do not ingest an official-site GPS file without a separately recorded reuse licence.

**Geometry and terrain.** Freeze OSM relation `16287`, version 345, timestamp `2026-04-11T17:24:15Z`, including full recursive members and roles. Use OSM context only for major lochs, watercourses and the eight named control settlements. Use OS Terrain 50 in EPSG:27700 for contours/profile. Compute length independently from the frozen line; show the official 154 km in the subtitle and store the computed value separately.

**Composition.** North-up EPSG:27700 crop with 5% padding. Use 100 m grey contours and label only 500 m indices where they do not cross the route. The conventional eight-section structure has nine ordered boundaries: Milngavie, Drymen, Rowardennan, Inverarnan, Tyndrum, Inveroran, Kingshouse, Kinlochleven and Fort William. Plot all nine as small black diamonds, label at most six in the map, and retain the full ordered list in metadata. The profile carries kilometre ticks and the same nine boundary diamonds, with no filled area beneath it.

**Exact plot layers.** (1) Grey 0.25: selected 100 m Terrain 50 contours; (2) Blue 0.25: Loch Lomond and major hydrography; (3) Black 0.25: nine section-boundary diamonds, settlement labels, profile, detail and both source credits; (4) Black 0.60: title and double border; (5) Red 0.40: one continuous route centreline. One load per pen, route last.

**Required uncertainty and QA.** The identity site is not the geometry licence. Validate ordered endpoints within 100 m of Milngavie and Fort William controls, all relation members resolved, no unexplained line gap above 20 m, no alternative relation folded in, and official/computed distance delta reported. Verify that the long north–south route remains entirely inside the clip and that labels at Loch Lomond do not obscure the red line. Footer: `Decorative route artwork — not for navigation` plus OSM and OS credits.

### 2. `RTE-GB-HEB-WALK-01` — Hebridean Way, walking

**Identity.** Title `HEBRIDEAN WAY`; subtitle `VATERSAY — STORNOWAY · WALKING · 252 KM`. The [official route overview](https://www.visitouterhebrides.co.uk/routes) and [official walking leaflet](https://www.visitouterhebrides.co.uk/dbimgs/1608_BB%20Hebridean%20WayWalking%20Leaflet_V4_SCREEN_FINAL.pdf) describe a 156 mile / 252 km walk through ten islands and two ferry crossings. The walking route ends at Stornoway. Do not extend it to the Butt of Lewis; that conflates it with cycling material.

**Geometry and terrain.** Freeze OSM relation `7610425`, version 81, timestamp `2026-07-07T16:07:09Z`. Persist every disconnected land segment and ferry mode separately. Use EPSG:27700, OS Terrain 50 contours/profile, and sparse OSM coast/hydrography. The product record must contain a mode sequence such as `walk / ferry / walk / ferry / walk`, derived from relation roles/tags and manually verified against official identity material.

**Composition.** Use 50 m contours because island relief and route scale would disappear at 100 m. Label six island groups, not every settlement. Plot the walking trace in red only on land. Plot ferry connectors in Blue 0.25 with a clearly dashed pattern; never draw a red continuous path across open water. The official walking presentation divides the journey into twelve sections: resolve and plot the expected thirteen ordered section boundaries, then map-label no more than seven. Profile sections are separated by blue ferry glyphs and blank x-gaps rather than invented sea-level interpolation.

**Exact plot layers.** (1) Grey 0.25: selected 50 m contours; (2) Blue 0.25: coastline, major lochs and two dashed ferry connectors; (3) Black 0.25: island labels, thirteen section-boundary diamonds, segmented profile, ferry captions, legal/detail text; (4) Black 0.60: title and double border; (5) Red 0.40: land-only walking segments. One contiguous visit per pen.

**Required uncertainty and QA.** Automatically reject a build if it has one unbroken red polyline across ferry gaps, if its northern endpoint is not Stornoway, or if cycling relation members appear. Check ten-island topology, two ferry transitions, stage order and long-axis crop. The detail line must warn that ferries, tides, weather and path conditions change and the plate is not a current transport or navigation guide.

### 3. `RTE-GB-GGW-01` — Great Glen Way

**Identity.** Title `GREAT GLEN WAY`; subtitle should initially read `FORT WILLIAM — INVERNESS` without a printed distance. The [Highland Council’s 17 June 2026 update](https://www.highland.gov.uk/news/article/17313/great_glen_way_route_improvements_now_open) reports more than 3 km of new/improved path and gives 118 km / 79 miles, values that are not mutually consistent. OSM relation `126572` tags 120 km. A sellable plate must not conceal that discrepancy.

**Geometry and terrain.** Freeze OSM relation `126572`, version 157, timestamp `2026-07-01T22:28:20Z`, after the announced works. Ignore the relation tag typo `Invereness`; use the official spelling `Inverness`. Resolve only the current main member chain, EPSG:27700. Use Terrain 50 for 100 m contours/profile and OSM for sparse lochs, canal/water and settlements.

**Composition.** The Great Glen’s linear loch geometry is the principal Blue 0.25 motif. Use seven control labels: Fort William, Gairlochy, Laggan, Fort Augustus, Invermoriston, Drumnadrochit and Inverness. Where high- and low-route options occur, do not merge them into a single average line; this first plate displays only the member chain classified and manually signed off as the main variant. Record excluded branch IDs.

**Exact plot layers.** (1) Grey 0.25: 100 m contours, aggressively thinned around text; (2) Blue 0.25: canal/major lochs and River Ness; (3) Black 0.25: seven controls, profile, variant note, detail and credits; (4) Black 0.60: title and double border; (5) Red 0.40: current main route only.

**Required uncertainty and QA.** A release is blocked until the frozen relation is compared with Highland Council’s post-improvement public alignment or written confirmation. Store `official_text_km=118`, `official_text_miles=79`, `osm_tag_km=120` and `computed_km` as separate fields. Never auto-select one as “truth.” Add a test that fails if the route snapshot predates the 17 June 2026 improvement without an explicit `historic_alignment=true` flag.

### 4. `RTE-GB-JMW-WALK-01` — John Muir Way, walking

**Identity.** Title `JOHN MUIR WAY`; subtitle `HELENSBURGH — DUNBAR · WALKING · 215 KM`. The [official walking-route page](https://johnmuirway.org/doing-route/walking-route) distinguishes walking and cycling options; the [official activity guide](https://johnmuirway.org/assets/Education-Resources/23ee86a017/John-Muir-Way-activity-guide.pdf) gives 134 miles / 215 km from Helensburgh to Dunbar.

**Geometry and terrain.** Freeze OSM walking relation `49215`, version 379, timestamp `2026-01-20T19:03:46Z`. The variant policy is `walking_only`; recursively resolving a cycling superroute or nearby cycleway is a hard error. Use EPSG:27700, Terrain 50 and sparse OSM hydro/context.

**Composition.** Use 50 m contours because the route crosses lowland as well as hill country. The ten official walking sections produce eleven ordered boundary controls; plot all eleven as diamonds but label no more than eight. The map must preserve the broad west–east story and the coast at Dunbar without turning central-belt urban context into a dense road map. In the profile, use all eleven boundaries and kilometre ticks; do not exaggerate the vertical scale without recording it.

**Exact plot layers.** (1) Grey 0.25: selected 50 m contours; (2) Blue 0.25: Loch Lomond fringe, Forth/coast and major rivers; (3) Black 0.25: eleven section-boundary diamonds, up to eight labels, profile, walking-variant notice and credits; (4) Black 0.60: title/border; (5) Red 0.40: walking relation only.

**Required uncertainty and QA.** Endpoint checks are Helensburgh and Dunbar; compare named controls in order, confirm relation network/type, and reject any member marked as a cycling-only variant. Visually inspect route/contour collisions in the Campsies and route/water crossings near the Forth. The detail zone must state `Walking alignment; cycling route differs`.

### 5. `RTE-CH-VA1-01` — Via Alpina, Route 1

**Identity.** Title `VIA ALPINA`; subtitle `ROUTE 1 · VADUZ/GAFLEI — MONTREUX · 390 KM`. The [official SwitzerlandMobility Route 1 page](https://schweizmobil.ch/en/hiking-in-switzerland/route-1) reports about 390 km, 20 stages and very large cumulative ascent/descent. It is an identity/stage reference, not a licence to copy its rendered map.

**Geometry and terrain.** Freeze OSM relation `12359033`, version 12, timestamp `2026-01-10T22:48:23Z`, and record the relation’s `ref=1`. Use EPSG:2056 and actual swissALTI3D OGD tiles, resampled to 25 m. Use OSM for a very limited lake/river/settlement context. Do not use swisstopo sample downloads in a product.

**Composition.** Use 250 m contours with only 1000 m index labels. The 20 stages produce 21 ordered stage boundaries; plot all 21 as small black diamonds in map and profile and label eight major controls at most. Because the route spans Switzerland, retain a wide north-up crop rather than rotating it. Record the first-stage identity carefully: source descriptions may distinguish Vaduz and Gaflei, so the geometry endpoint and display wording must both be explicit.

**Exact plot layers.** (1) Grey 0.25: selected 250 m swissALTI3D contours; (2) Blue 0.25: major lakes and rivers; (3) Black 0.25: 21 stage-boundary diamonds, eight labels, profile, source/difficulty note and OSM/swisstopo credits; (4) Black 0.60: title and double border; (5) Red 0.40: frozen Route 1 centreline.

**Required uncertainty and QA.** Require exactly 20 ordered stages in the release record, endpoint proximity checks at the declared first/last controls, no Route 6 members, 99.5% DEM sampling coverage and no nodata interpolation across mountain voids. The map is art, not a mountain-hiking grading or access guide; current official conditions always supersede it.

### 6. `RTE-CH-AP6-01` — Alpine Passes Trail, Route 6

**Identity.** Title `ALPINE PASSES TRAIL`; subtitle `ROUTE 6 · CORVIGLIA — SAINT-GINGOLPH · 669 KM`. Validate the current route identity against [SwitzerlandMobility Route 6](https://schweizmobil.ch/en/hiking-in-switzerland/route-6).

**Critical regression.** The old OSM relation `132518` is deleted and returns HTTP 410. It represented an obsolete route concept/start and must be permanently placed in a negative regression fixture. The current source is OSM relation `18021781`, version 30, timestamp `2025-10-27T06:27:06Z`, tagged from Corviglia (St. Moritz) to Saint-Gingolph VS with a distance of 669 km. No cache migration may silently fall back to the old ID.

**Geometry and terrain.** Recursively freeze relation `18021781`, its stage subrelations and ordered members. Use EPSG:2056, actual swissALTI3D tiles resampled to 25 m, and sparse OSM hydro/context. The implementation must derive the current stage count from the frozen relation and reconcile it with the official page; do not hard-code an old guidebook’s stage count.

**Composition.** Use 250 m contours and 1000 m index labels. Display a diamond for every verified current stage endpoint, but label no more than eight representative controls. Because a 669 km mountain route creates dense linework at A5, the route gets priority over minor contour loops; cull grey geometry inside a 0.45 mm paper halo around the red route and label paths.

**Exact plot layers.** (1) Grey 0.25: culled 250 m contours; (2) Blue 0.25: only major lakes/rivers; (3) Black 0.25: current stage diamonds, up to eight labels, profile and credits; (4) Black 0.60: title/border; (5) Red 0.40: relation `18021781` only.

**Required uncertainty and QA.** Negative test: input relation `132518` must fail with `obsolete_source_id`. Positive tests: first control Corviglia/St. Moritz, last Saint-Gingolph VS, `ref=6`, all subrelations resolved, no missing stage, and official/current stage count recorded. This plate cannot release while relation membership and official stage sequence disagree.

### 7. `RTE-FR-ECR-976000` — Tour des refuges en Vallouise en 5 jours

**Identity and open geometry.** Use the title `TOUR DES REFUGES`; subtitle `VALLOUISE · 5 JOURS · 46.32 KM`. The official open record is on the [Écrins route page](https://rando.ecrins-parcnational.fr/trek/976000-Tour-des-refuges-en-Vallouise-en-5-jours), with geometry distributed in the [Parc national des Écrins open dataset](https://www.data.gouv.fr/datasets/randonnees-du-parc-national-des-ecrins). Frozen feature:

- `id_local=976000`
- `uuid=9caad71a-cc8f-4b0e-a8e4-6588d6ad22ab`
- Ailefroide → Ailefroide, LineString
- source length 46,320 m; source ascent/descent +3345 / −3345 m
- source altitude range 1511–2688 m; duration 120 h; difficulty `Moyen`
- `balisage=null`; modified `2026-07-06`
- WGS84 bbox at audit: `6.3884585,44.8658797,6.4457231,44.9379504`

The audit download on 2026-08-02 had SHA-256 `020d7df4f565520a937b18ab643627e70fdab4fed9ec440bb8b85ae210323333`; refresh and freeze a new checksum at build time because the dataset updates frequently.

**Terrain/context.** Use EPSG:2154 and actual RGE ALTI 5 m tiles resampled to 25 m for a restrained 100 m contour interval; label 500 m indices. The route geometry comes from Écrins, not OSM. If OSM hydrography/settlements are used, add OSM attribution; otherwise prefer compatible IGN/open national hydro data with its exact licence record. Never copy photos or narrative text from the page.

**Composition.** The source describes a five-day itinerary, but a single parent LineString does not prove precise daily break coordinates. Until child itineraries or official stage coordinates are machine-resolved, label only Ailefroide and verified refuge/control coordinates. Do not invent five equally spaced stages. In the profile, show verified controls only and record that out-and-back portions are intentional.

**Exact plot layers.** (1) Grey 0.25: RGE ALTI 100 m contours; (2) Blue 0.25: licensed torrents/glacial hydro context; (3) Black 0.25: verified refuges/controls, profile, factual source fields, Open Licence/IGN and any OSM credit; (4) Black 0.60: title/border; (5) Red 0.40: exact Écrins LineString.

**Required uncertainty and QA.** Confirm `balisage` remains null at build time; any later GR/GRP value moves the plate back to rights review. Verify loop closure within source precision, preserve repeated/out-and-back coordinates, compare computed/source length, and require explicit stage-coordinate provenance before drawing stage diamonds. Safety copy should mention high-altitude, steep/cabled and weather-sensitive terrain only in paraphrased factual form, followed by the art-only notice.

### 8. `RTE-FR-ECR-995181` — Tour du Pic de Valsenestre

**Identity and open geometry.** Title `TOUR DU PIC DE VALSENESTRE`; subtitle `VALJOUFFREY · 45.28 KM`. Use the [official route record](https://rando.ecrins-parcnational.fr/trek/995181-Tour-du-Pic-de-Valsenestre) and the same [Écrins open dataset](https://www.data.gouv.fr/datasets/randonnees-du-parc-national-des-ecrins). Frozen feature:

- `id_local=995181`
- `uuid=090e3adf-336c-4ce7-ab96-6741f9fee94f`
- La Chapelle-en-Valjouffrey → same start, LineString
- source length 45,280 m; +2576 / −2589 m
- source altitude range 976–2468 m; duration 120 h; difficulty `Moyen`
- `balisage=null`; modified `2026-01-16`
- WGS84 bbox at audit: `6.0166060,44.8572971,6.1811409,44.9078006`

**Terrain/context.** Use EPSG:2154, RGE ALTI resampled to 25 m, 100 m contours and 500 m indices. Use only appropriately licensed hydrography and settlement points. The striking east–west shape and high relief need no dense road network.

**Composition.** Fit the entire loop with 5% padding and preserve its closure. Label La Chapelle-en-Valjouffrey and only verified valley/refuge controls. Derive profile x from the ordered source line. If the source LineString’s first/last coordinates differ slightly, retain the recorded endpoints and draw closure only when a defined tolerance confirms it; never snap a large gap for appearance.

**Exact plot layers.** (1) Grey 0.25: 100 m RGE ALTI contours; (2) Blue 0.25: principal rivers/torrents; (3) Black 0.25: verified controls, loop direction cue, profile, factual details and all credits; (4) Black 0.60: title/border; (5) Red 0.40: frozen Écrins LineString.

**Required uncertainty and QA.** Recheck UUID, modification date, licence and `balisage=null`; compare source and computed length/ascent; ensure the start/end control is exactly one visual point; and review contour collisions at narrow switchbacks. No GR/GRP mark or logo may appear.

### 9. `RTE-IS-LAUG-01` — Laugavegur

**Identity.** Title `LAUGAVEGUR`; subtitle `LANDMANNALAUGAR — ÞÓRSMÖRK · 54 KM`. The [Iceland Touring Association route page](https://www.fi.is/en/hiking-trails/trails/view/laugavegur) is the identity/safety reference and describes a roughly 54–55 km, four-day route. Preserve Icelandic spelling in path text.

**Geometry and terrain.** Freeze OSM relation `1225037`, version 48, timestamp `2025-09-13T22:41:13Z`. Use Iceland Lambert 2004 / EPSG:3057, IS 50V elevation and suitable licensed hydrography. Store official/display distance and computed relation length separately.

**Composition.** Use 100 m contours, with 500 m index labels only. Stage controls: Landmannalaugar, Hrafntinnusker, Álftavatn/Hvanngil, Emstrur and Þórsmörk. Show all five in profile; label no more than five on map. River-crossing symbols are small black open diamonds/cross marks only where verified. They are not assurances of bridges or passability.

**Exact plot layers.** (1) Grey 0.25: selected IS 50V 100 m contours; (2) Blue 0.25: major rivers/lakes; (3) Black 0.25: five stage controls, verified crossing symbols, profile, CC BY/OSM credit and art-only warning; (4) Black 0.60: title/border; (5) Red 0.40: frozen route centreline.

**Required uncertainty and QA.** Ensure route direction and diacritics are correct, all five controls occur in order, and no crossing symbol is inferred solely from a line intersection. The release note must flag seasonal opening, weather and unbridged-river risk without presenting operational advice. Validate the entire relation against current official route identity before each edition.

### 10. `RTE-ES-CAM-ES01C` — Camino Francés, canonical official chain

**Identity and geometry.** Title `CAMINO FRANCÉS`; subtitle `PUENTE LA REINA — SANTIAGO · ES01C`. Use the official [CNIG Camino de Santiago download product](https://centrodedescargas.cnig.es/CentroDescargas/camino-santiago), supplied in GPX/KML/SHP in ETRS89, rather than an OSM approximation. The current official ArcGIS joined record identifies `ES01c` at 684.21 km; the audit observed an edition timestamp `2026-05-11T17:10:55Z` and a [CNIG update notice dated 2026-06-08](https://centrodedescargas.cnig.es/CentroDescargas/novedades?codSerie=CSANT). Freeze the exact downloaded archive, file list and hashes during implementation.

Variant policy is strict: concatenate canonical stages `05a` through `33a`, inclusive, in order. Exclude every `b/c/d/e/f` variant unless a future plate declares a different catalogue ID. The [official ArcGIS layer schema](https://nco.ign.es/server/rest/services/nco/CaminoSantiago/MapServer/1) is useful for field validation, but the downloaded GPX/SHP snapshot is the release source.

**Licence.** Product attribution is CC BY 4.0 FEAACS. Retain the exact licence page shipped with the archive; the derivative wording is:

`Obra derivada de Rutas de Caminos de Santiago 2020–2026 · CC BY 4.0 · FEAACS`

The exact year range must follow the current download record. CNIG describes the trace as an aid rather than a definitive guide, which aligns with the art-only product policy. A current file-detail record is available through the [CNIG detail service](https://centrodedescargas.cnig.es/CentroDescargas/detalleArchivo?sec=11060014).

**Terrain and composition.** Use EPSG:3035 for a north-up European overview after geodesic length calculation; use MDT25 second-coverage tiles for the profile and sparse 200 m contours, with 1000 m index labels. The 29 canonical stage segments should resolve to 30 unique ordered boundaries; plot all 30 as small diamonds but label only seven major cities/controls. Never print all stage names over the route; put a compact stage range and total in the detail zone. Retain a broad horizontal crop; do not rotate the peninsula to make the line larger.

**Exact plot layers.** (1) Grey 0.25: selected 200 m MDT25 contours; (2) Blue 0.25: licensed major rivers only; (3) Black 0.25: 30 stage-boundary diamonds, seven labels, profile, `05a–33a` detail and FEAACS/MDT25 credits; (4) Black 0.60: title/border; (5) Red 0.40: concatenated canonical `a` stages only.

**Required uncertainty and QA.** Require 29 unique stage IDs in monotonic numeric order, exact endpoint controls, no excluded suffixes, no gaps/overlaps caused by stage concatenation, and a reported delta between source 684.21 km and computed length. A fixture containing `05a,06b,07a` must fail variant validation. The release record must preserve each constituent file’s checksum rather than only the merged output.

## Future customer-GPX import architecture

The curated catalogue and customer uploads should converge after ingestion, but they must not share an unsafe “just turn any polyline into art” front door.

### Import contract

Support GPX 1.0 and 1.1. The canonical 1.1 specification states that coordinates are WGS84 and elevations are metric; see [Topografix GPX 1.1](https://www.topografix.com/gpx/1/1/) and the [developer guidance](https://www.topografix.com/gpx_for_developers.asp). Parse XML with a hardened parser such as `defusedxml`, external entities disabled, and explicit archive/file/point limits. Never parse GPX using regex or line splitting.

Preserve semantic structure:

- `trk` is not `rte`; `wpt` is not automatically a stage.
- Every `trkseg` is a logically separate segment. Do not bridge segments by default; a new segment can represent GPS loss, a ferry, a pause or disconnected travel.
- Multiple tracks/routes require an explicit user selection or declared merge order.
- Latitude/longitude must be finite and inside legal ranges. Reject NaN, infinity and malformed coordinates.
- Elevation and time are optional. Timestamps need not exist; if present, flag non-monotonic sequences but do not reorder geometry silently.

Store the immutable original file, SHA-256, media type, parser version and import timestamp. Normalized coordinates live in a separate record. Every repair—deduplication, spike removal, segment join, endpoint trim—must be an explicit logged transform with before/after metrics.

### Rights and privacy gate

Before processing, require the customer to confirm that they created the file or have commercial reproduction rights. A publicly reachable Strava, Komoot, AllTrails or Wikiloc URL is not a geometry licence; reject scraping/import-by-URL absent an API and terms that explicitly permit the product use.

Default privacy behaviour:

- local processing where practical; no third-party upload;
- remove timestamps, creator/email fields and unknown extensions from the art pipeline;
- do not print waypoint names without an explicit preview/selection;
- offer endpoint trimming or coarse endpoint displacement for home-origin activities;
- retain the original only under the product’s documented retention policy;
- show a preview of every label and metadata field that will appear on the plate.

### Elevation policy

If valid recorded `ele` values cover at least 95% of route points and pass range/spike checks, they may drive the profile. Otherwise sample the approved regional DEM. Never silently blend recorded elevation and DEM elevation. Record the source used for every sample range.

Distance is geodesic on WGS84. For an art-friendly but reproducible ascent calculation, retain raw points, resample by distance (default 25 m, configurable 20–30 m), apply a documented smoothing/filter window, and calculate cumulative gain/loss from the filtered series. Store raw and filtered totals. Labels must say whether elevation is `recorded` or `DEM-derived`; they must not imply survey accuracy.

### Import-to-plate pipeline

```text
immutable GPX
  → secure parse and structural report
  → rights/privacy gate
  → explicit track/segment/mode selection
  → coordinate and topology normalization
  → projection choice + DEM acquisition/sampling
  → route/context/contour geometry generation
  → A5 route_plate composition
  → physical pen resolution
  → SVG validation + plot simulation
  → visual and physical QA
  → release bundle
```

Projection is selected from the route’s geography, not from a global default. A route spanning a UTM boundary can use an appropriate national CRS or local azimuthal/equidistant projection; the selection and EPSG/PROJ string are metadata. All route lengths remain geodesic regardless of display projection.

## Required metadata schema

At minimum, every curated or customer plate stores:

```yaml
subject_kind: route_plate
catalog_id: RTE-...
title: ...
route_identity:
  name: ...
  variant_policy: ...
  direction: ...
  navigation_status: art_only
sources:
  identity: [{url, retrieved_at, role}]
  geometry: [{provider, source_id, version, timestamp, url, sha256, license, attribution}]
  elevation: [{product, release, tiles, vertical_datum, sha256, license, attribution}]
  context: [{provider, query_or_files, timestamp, sha256, license, attribution}]
route_metrics:
  source_distance_km: ...
  computed_distance_km: ...
  source_ascent_m: ...
  computed_ascent_m: ...
  ascent_method: ...
  sample_interval_m: ...
  smoothing: ...
segments:
  - {mode: walk|ferry|unknown, source_ref: ..., start_control: ..., end_control: ...}
stages: [{id, name, coordinate_source, sequence}]
composition:
  format: route_plate
  paper: A5_portrait
  projection: EPSG:...
  route_bbox_projected: [...]
  contour_interval_m: ...
  index_contour_interval_m: ...
pens:
  - {order: 1, inventory_id: ..., color: Grey, nib_mm: 0.25, effective_mark_mm: ..., passes: 1, offset_mm: 0}
  # one record for every plotted pen
validation:
  generator_version: ...
  validator_version: ...
  plotsim_version: ...
  coverage_percent: ...
  travel_ratio: ...
  errors: 0
release:
  warnings: [decorative_art_not_navigation]
  physical_plot_id: ...
  reviewer: ...
  reviewed_at: ...
```

OSM records additionally need relation version, full member IDs/versions/roles/order and replication timestamp. A relation’s name/distance tags alone are insufficient. Ways may join only at exact or tolerance-verified endpoints; gaps above 20 m become explicit discontinuities unless a source-supported transform resolves them.

## Automated and visual QA

### Source and rights tests

- Geometry/elevation/context source IDs, URLs, retrieval timestamps, hashes, licence identifiers and exact attribution are nonempty.
- Every current downloaded file hashes to the frozen release manifest.
- Relation/API failures never trigger a fallback to a differently named or obsolete source.
- OSM output contains the compact OSM attribution; OS/swisstopo/IGN/Náttúrufræðistofnun/FEAACS credits appear when their data is used.
- French records fail closed if `balisage` changes to GR/GRP or the licence evidence disappears.
- Customer GPX cannot proceed without the rights declaration and privacy preview.

### Geometry and route tests

- At least two finite route points; legal coordinate ranges; no NaN/inf.
- Preserve all declared segments and modes; no automatic ferry, GPS-gap or stage bridging.
- Endpoints are within the route-specific tolerance of frozen controls, normally 100 m; loop closure is checked separately.
- Stage/control sequence is monotonic and variant IDs meet the plate’s policy.
- Full route is inside the map clip with the required padding.
- Simplification preserves endpoints, stage controls, segment boundaries and topology. Paper-space maximum displacement should be `≤0.08 mm`; compare simplified and source lines with a documented Hausdorff/Fréchet metric.
- Computed length changes by less than 1% through normalization/simplification. Larger changes block release.

### Elevation tests

- DEM coverage along the route is at least 99.5%; unresolved nodata is a failure, not zero elevation.
- CRS and vertical datum are recorded; horizontal and vertical units are metres.
- Profile first/last and stage x-coordinates correspond to the route-distance model.
- Re-running the same source, sampling interval and filter yields the same ascent/descent.
- Source and computed distance/ascent discrepancies are retained, not overwritten.

### Format and physical tests

- `1 SVG unit = 1 mm`; exact A5 dimensions and required zones.
- Only absolute `M/L/C/Z`; no arcs, relative commands, live text, images, masks that conceal geometry, or empty layers.
- Contiguous layer and pen order; each distinct pen appears once; no offsets; one pass.
- Nibs belong to `[0.25, 0.40, 0.60, 0.70, 1.00]` and match inventory colour constraints.
- Cap-height and minimum-path rules pass for the resolved physical nibs.
- Field coverage `≤28%`; production target `≤24%`.
- Plot simulation optimized travel ratio is preferably `<1.0` and must remain `<2.0`; investigate any pen revisit or excess pen-up travel.
- Actual inventory, stock, calibration and paper/substrate gates pass. Until then output is labelled `review_only`.

### Visual review

Render a PNG preview at print resolution and inspect both full size and thumbnail. The reviewer signs off that:

- red route hierarchy remains clear without appearing as double rails;
- contour density describes terrain without moiré or heavy grey masses;
- water and route crossings are intelligible;
- no label or stage diamond hides a critical bend or segment break;
- no accidental line bridges GPX segments or ferry gaps;
- long routes and closed loops are fully inside the clip;
- profile, diacritics, control order and title are correct;
- all attribution is legible at physical A5 size;
- the plate feels intentionally sparse enough for a pen plotter, not like a reduced GIS export.

Each candidate needs one adversarial visual fixture: wrong variant, missing stage, clipped endpoint, bridged segment, obsolete source or missing credit. A candidate is not fully implemented until the bad fixture fails for the expected reason.

## Implementation order and acceptance milestones

1. **Build the generic `route_plate` schema and renderer** using a synthetic/local fixture. Acceptance: exact A5 zones, five-pen plan, segment-aware route rendering, profile and zero validator errors.
2. **Implement OSM relation snapshotting and attribution evidence.** Acceptance: recursive member/version manifest, reproducible context extraction and ODbL release bundle.
3. **Produce West Highland Way and Hebridean Way pilots.** They exercise linear terrain and ferry segmentation. Acceptance: both pass automated/visual QA and one physical A5 plot each.
4. **Add national DEM adapters** for Terrain 50, swissALTI3D, RGE ALTI, IS 50V and MDT25 with tile/hash/licence manifests.
5. **Produce the remaining eight catalogue candidates** and their adversarial fixtures. Do not batch-release; each gets individual source and physical review.
6. **Add secure GPX import and privacy/rights preview.** Acceptance: segment preservation, endpoint trimming, explicit elevation provenance and malicious/oversized XML fixtures.
7. **Catalogue release gate.** Counsel/licence review, calibrated stock check, physical proofs, product copy and immutable source bundles complete.

The best first commercial-quality pair is West Highland Way plus a customer-GPX plate. The first proves curated source/release discipline; the second validates the broader personalised-product opportunity. Hebridean Way should be the first segmentation stress test, and Alpine Passes Trail Route 6 should be the source-regression test. French GR/TMB subjects remain explicitly out of the sellable queue until rights are resolved.
