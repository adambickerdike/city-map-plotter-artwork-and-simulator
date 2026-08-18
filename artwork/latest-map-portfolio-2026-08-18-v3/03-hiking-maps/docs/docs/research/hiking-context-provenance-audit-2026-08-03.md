# Hiking context and provenance audit

Date: 2026-08-03
Scope: pre-redesign baseline of the ten hiking plates in
`src/city_map_plotter/data/hike-plates-v1.json`
Decision status: implementation plan, not legal advice

This document records the catalog and renderer as inspected before the
2026-08-03 contextual-geography migration. Its “current implementation”
findings are baseline evidence, not a claim about later source revisions. The
hiking-only release audit and generated QA report determine the state of a
subsequent build.

## Executive decision

The hiking plates are a strong next product family, but they are not yet ready to be sold as factual landscape artworks. All ten current records contain route geometry and source credits, but none contains a context, elevation, or labels key. The renderer fills that gap with synthetic sinusoidal contours and vegetation marks and reserves an elevation-profile panel even when no elevation exists. Those devices are useful as an explicitly stylized prototype, but must not be presented as mountains, woodland, rivers, coastlines, or route profiles derived from the place.

Implementation is worthwhile if it is treated as a new schema-v2, source-derived pipeline. The safe minimum is:

1. Keep the existing, credited route geometry unchanged.
2. Acquire immutable national mapping and elevation snapshots for each route.
3. Project every source into an explicit local projected CRS.
4. derive context, relief, profile, and labels offline into content-addressed assets;
5. render only verified assets, omitting any missing layer rather than inventing it;
6. preserve visible licence attribution and ship a machine-readable source manifest.

The immediate visual milestone should be four proof plates, selected to exercise distinct problems:

- Great Glen Way: linked lochs, shoreline exclusivity, relief sidewalls, woodland.
- Hebridean Way: complex coast, islands, two ferry legs, sea labels, minimal woodland.
- Tour des refuges: high-resolution mountain relief, streams, peaks, compact A5 scale.
- Camino Francés: continent-scale A5 overview with deliberate contextual restraint.

After these pass geometry, provenance, plotter, and visual QA, the same system can generate all ten.

## Findings in the current implementation

### Catalog gap

The ten records in hike-plates-v1.json entirely lack the following keys:

- context
- elevation
- labels

They are absent rather than present with null values. Existing per-route source lists contain only one or two references and predominantly document route geometry.

The current records are:

| Plate | Current route source | Approximate route fit | Consequence at A5 |
|---|---|---:|---|
| West Highland Way | OpenStreetMap | 1.09 km/mm | Major lochs, ranges, and a few settlements only |
| Hebridean Way | OpenStreetMap | 1.61 km/mm | Coast and ferries dominate; tiny islands must be culled physically |
| Great Glen Way | OpenStreetMap | 0.81 km/mm | The loch chain can remain legible |
| John Muir Way | OpenStreetMap | 1.10 km/mm | Landscape layout; coast/estuary and settlements matter more than relief |
| Via Alpina | OpenStreetMap | 1.58 km/mm | Range and lake context; sparse peaks |
| Alpine Passes Trail | OpenStreetMap | 1.87 km/mm | Cross-border DEM and attribution problem |
| Tour des refuges | Parc national des Écrins | 0.09 km/mm | Rich local relief, peaks, streams, and vegetation are practical |
| Tour du Pic de Valsenestre | Parc national des Écrins | 0.10 km/mm | Rich local relief and settlement context are practical |
| Laugavegur | OpenStreetMap | 0.38 km/mm | Relief and hydrography; woodland should normally be absent |
| Camino Francés | FEAACS/CNIG | 4.39 km/mm | A5 is an overview: cities and regions, not local streams or peaks |

The metres-per-millimetre values are planning estimates from the current route extents and an approximately 126 by 90 mm map region. Production must recompute them from the final projected display extent and actual map rectangle.

### Projection and framing blocker

The current geographic transform uses longitude multiplied by cosine of latitude. It does not apply composition.recommended_crs. Its bounding box comes only from the route points, and it does not expand that projected extent to the final map rectangle's aspect ratio.

This must be replaced before national vector or DEM context is introduced. Otherwise:

- distance, slope, simplification, and physical scale are inconsistent;
- independently acquired layers can drift;
- wide routes can leave large dead areas or be incorrectly letterboxed;
- cross-border and high-latitude plates are especially fragile.

The new framing invariant is: project first, contain the complete route, apply physical padding, then expand the projected bounding box symmetrically to the actual post-layout map-rectangle aspect. Never crop a complete route merely to fill paper.

### Synthetic backdrop and empty profile

The current backdrop creates sinusoidal contours and V-shaped vegetation symbols. They are not derived from terrain or land cover. The layout also reserves roughly one fifth of the composition for a profile even when no embedded elevation exists, producing a framed “NO EMBEDDED ELEVATION” area.

For saleable factual plates:

- synthetic context is forbidden;
- a missing factual layer is omitted;
- the profile region is allocated only when a verified elevation profile exists;
- the recovered area is returned to the map when the profile is absent.

### Pen inventory caveat

The current hiking pen order is sound as a six-pen production template:

| Order | Nominal pen | Role |
|---:|---|---|
| 1 | grey 0.25 mm | terrain relief |
| 2 | blue 0.25 mm | water and ferry legs |
| 3 | green 0.25 mm | woodland |
| 4 | black 0.25 mm | labels and profile |
| 5 | black 0.60 mm | title and border |
| 6 | red 0.40 mm | route, plotted last |

Unused pens should be omitted and remaining order made contiguous. These dimensions are nominal inventory values, not proof of the physical pen stock, ink, paper, or machine calibration. Each production pen/paper combination still needs a dated calibration specimen and measured result before widths are described as exact.

The existing format rules remain binding: for a 0.25 mm nib, a mark must be at least 0.75 mm long and a line cap or isolated dot must be at least 2.0 mm. Ink coverage and plot duration are useful reports, but ink coverage must not become a rejection gate or a reason to remove factual content.

## Recommended authoritative sources

National data is preferred to live web services or stitched consumer map tiles. Product landing pages should be saved in the manifest, while raw archives, metadata, licences, checksums, and retrieval timestamps should be preserved in the artifact store.

### Great Britain

Use one coordinated Ordnance Survey stack:

- OS OpenMap Local for surface water, tidal water, canonical coastline where available, woodland, built context, and local vector reference. It is an open 1:10,000 product updated approximately every six months.
  https://www.ordnancesurvey.co.uk/products/os-open-map-local
- OS Open Rivers for a continuous high-level watercourse network, particularly where cartographic linework in other products is interrupted at bridges.
  https://www.ordnancesurvey.co.uk/products/os-open-rivers
- OS Terrain 50 for a 50 m bare-earth DTM and 10 m contours.
  https://www.ordnancesurvey.co.uk/products/os-terrain-50
- OS Open Names for settlements, water names, seas, hills and mountains, ranges, woodland and forest names.
  https://www.ordnancesurvey.co.uk/products/os-open-names

The OS Open Names local-type vocabulary explicitly covers City, Town, Village, Hamlet, Bay, Estuary, Inland Water, Sea, Tidal Water, Woodland or Forest, Hill or Mountain, and Hill or Mountain Ranges:

https://docs.os.uk/os-downloads/products/addresses-and-names-portfolio/os-open-names/os-open-names-technical-specification/local-type

Do not render an OS OpenMap Local coast and an OS Terrain 50 coast together. Choose one canonical source-defined shoreline. A typical visible acknowledgement is:

Contains OS data © Crown copyright and database right [year].

The exact current acknowledgement must be checked at release:

https://www.ordnancesurvey.co.uk/customers/public-sector/public-sector-licensing/copyright-acknowledgments

### Switzerland

Use:

- swissTLM3D for hydrography, land cover, settlement context, and landscape structure.
  https://www.swisstopo.admin.ch/en/landscape-model-swisstlm3d
- swissNAMES3D for peaks, passes, watercourses, settlements, lakes, and geographic regions.
  https://www.swisstopo.admin.ch/en/landscape-model-swissnames3d
- swissALTI3D for bare-earth elevation within Switzerland.
  https://www.swisstopo.admin.ch/en/height-model-swissalti3d

Free swisstopo geodata generally permits commercial use and redistribution, subject to the source-reference conditions. The release must use the precise attribution specified for the chosen product, normally including ©swisstopo:

https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices

swissALTIRegio is technically attractive for cross-border relief because it extends beyond Switzerland:

https://www.swisstopo.admin.ch/en/height-model-swissaltiregio

It is not the default recommendation for the Alpine Passes Trail. Its current multi-country mosaic requires a long multi-source credit and includes foreign height systems that are not transformed to LN02. Use it only after the full attribution fits the product and the datum caveat is recorded. The safer alternative is an explicit swissALTI3D plus French RGE ALTI mosaic, with both licences, both vertical references, an overlap/seam report, and no hidden crop at the national border.

### France

Use:

- BD TOPO for hydrography, land-sea boundaries, water bodies, hydrographic names, orographic detail, settlements, named places, and vegetation areas.
  https://geoservices.ign.fr/bdtopo
- RGE ALTI 5 m for a stable, consistent terrain snapshot for the two current loops.
  https://geoservices.ign.fr/rgealti

The BD TOPO technical documentation identifies the required themes and classes:

https://documentation.geoservices.ign.fr/?BDTopo=&id_classe=0&id_theme=97

RGE ALTI updates stopped in 2024 while LiDAR HD became its successor. Do not silently mix partial LiDAR HD coverage with RGE ALTI in the first release. A frozen RGE ALTI 5 m edition is sufficient at A5 and easier to reproduce.

The French Open Licence 2.0 permits commercial derivative use but requires the source and its last update date to be identified:

https://www.data.gouv.fr/pages/legal/licences/etalab-2.0

Do not use SCAN 25 or SCAN 100 raster map imagery as the contextual source. IGN warns that downloadable or printed value-added hiking uses of those scan services can fall outside free use:

https://geoservices.ign.fr/services-web-issus-des-scans-ign

Use Hautes-Alpes department 05 for Tour des refuges and Isère department 38 for Tour du Pic de Valsenestre. Preserve the existing Parc national des Écrins route provenance separately.

### Iceland

Use:

- IS 50V vector base-map layers for coastline, hydrography, land cover, geographic names, and elevation-related context, in EPSG:3057.
  https://www.natt.is/en/resources/geospatial-data/base-map-data
- the official geographical-names data for factual labels.
  https://www.natt.is/en/resources/geospatial-data/geographical-names
- IcelandDEM for relief and route-profile derivation. The available instructions describe a 10 m ArcticDEM-derived median mosaic.
  https://leidbeiningar.natt.is/instruction/3dprinting

The open data is published under CC BY 4.0 and requires the dataset, author, and licence to be identified:

https://www.natt.is/en/resources/open-data

Treat IcelandDEM as a derived surface/elevation model for artwork relief until the package metadata confirms its vertical datum and terrain/surface semantics. Do not publish precise peak or ascent values from it without that confirmation.

Laugavegur is a highland, substantially treeless composition. Green woodland marks should be omitted unless the selected official land-cover edition contains a relevant, physically legible class inside the display extent.

### Spain

Use:

- Base Topográfica Nacional thematic data for hydrography, orography, settlements, nature, and landscape;
- PNOA MDT25 as the nationwide 25 m bare-earth orthometric terrain model;
- SIOSE High Resolution only if one internally consistent edition covers the full Camino display extent.

Official references:

- https://www.ign.es/web/en/ign/portal/cbg-area-cartografia
- https://pnoa.ign.es/pnoa-lidar/modelo-digital-del-terreno
- https://pnoa.ign.es/ca/web/portal/pnoa-lidar/productos-a-descarga
- https://www.siose.es/web/guest/descripcion-ar
- https://centrodedescargas.cnig.es/

CNIG download conditions permit broad reuse, including commercial reuse, with attribution compatible with CC BY 4.0. The exact derived-work formula must be taken from the current conditions and stored with the snapshot:

https://www.ign.es/resources/licencia/Condiciones_licenciaUso_IGN.pdf

At roughly 4.4 km/mm, Camino Francés cannot show local woodland, minor streams, or individual peaks coherently on one A5 sheet. Prefer endpoints, major cities, a few regional or range labels, and only the strongest water or terrain structure. Omit green if a single consistent SIOSE edition cannot be proven. A staged A5 series or A3 plate can later carry local landscape detail.

ESA WorldCover 2021 v200 is an acceptable immutable fallback for exact tree-cover classes where no national open layer is viable. It is 10 m, CC BY 4.0, older than the national sources, and has reported global accuracy around 76.7 percent; it must be labelled as a fallback, not silently mixed:

https://esa-worldcover.org/en/data-access

## OpenStreetMap and attribution constraint

Seven current routes are derived from OpenStreetMap. Their printed plates cannot simply remove the words OpenStreetMap while retaining that geometry. OpenStreetMap requires attribution, and printed works must provide the copyright URL when a hyperlink is impossible:

- https://www.openstreetmap.org/copyright
- https://osmfoundation.org/wiki/Licence/Licence_and_Legal_FAQ

SVG metadata alone is not a substitute for attribution visible to the person receiving the print. To make an OSM credit disappear, replace that route geometry with an independently licensed official, organiser-supplied, or user-owned trace and document the replacement provenance. Otherwise retain an appropriate visible credit.

When OSM is needed as a fallback:

- acquire a date-stamped regional PBF, not a live Overpass response;
- record its provider, timestamp, size, and both upstream and local SHA-256;
- extract with complete ways and explicit tag filters;
- keep OSM-derived tables separate from national-data tables so the databases are not silently conflated;
- preserve any applicable derivative-database or data-offer obligations.

Geofabrik provides public date-stamped PBF extracts and checksum information, but is an unofficial distributor:

- https://download.geofabrik.de/
- https://download.geofabrik.de/technical.html

The production renderer must never access Overpass or any other live map service.

## Route-by-route acquisition recommendation

| Plate | Context | Elevation | Labels | A5 emphasis |
|---|---|---|---|---|
| West Highland Way | OS OpenMap Local plus Open Rivers | OS Terrain 50 | OS Open Names | Loch Lomond and major hydrography; sparse relief; optional woodland |
| Hebridean Way | OS OpenMap Local; retain sourced ferry geometry separately | OS Terrain 50 | OS Open Names | Route-containing islands, two ferries, sea names; aggressively remove sub-legibility islets; omit or minimise woodland |
| Great Glen Way | OS OpenMap Local plus Open Rivers | OS Terrain 50 | OS Open Names | Loch Lochy, Loch Oich, Loch Ness and their continuous connecting channel; moderate relief and woodland |
| John Muir Way | OS OpenMap Local plus Open Rivers | OS Terrain 50 | OS Open Names | Coast, estuary, settlements, woodland; very sparse relief; landscape orientation |
| Via Alpina | swissTLM3D | swissALTI3D | swissNAMES3D | Major ranges and lakes; few peaks; landscape orientation |
| Alpine Passes Trail | swissTLM3D plus explicit French edge data where needed | swissALTI3D plus RGE ALTI, or fully credited swissALTIRegio | swissNAMES3D plus BD TOPO names | Cross-border seam QA; range labels; no dense peak catalogue |
| Tour des refuges | BD TOPO 05 | RGE ALTI 5 m | BD TOPO named places and orographic detail | Rich hachures, streams, vegetation, two to four peaks |
| Tour du Pic de Valsenestre | BD TOPO 38 | RGE ALTI 5 m | BD TOPO named places and orographic detail | Rich loop relief, streams, settlement, two to four peaks |
| Laugavegur | IS 50V | IcelandDEM | official Icelandic names | Relief, hydrography and geographic names; no invented forest or sea |
| Camino Francés | BTN | PNOA MDT25 | BTN settlements and orography | Overview only: endpoints, major cities, regions/ranges; omit local clutter |

## Schema v2

Schema v1 should remain reproducible and unchanged. The factual pipeline should introduce hike-plates-v2 with explicit source, artifact, context, elevation, and label contracts.

### Source record

Each source is independent and must state what it contributed:

    {
      "id": "src-os-terrain50-2026a",
      "publisher": "Ordnance Survey",
      "product": "OS Terrain 50",
      "edition": "2026a",
      "retrieved_at": "2026-08-03T12:00:00Z",
      "landing_url": "https://www.ordnancesurvey.co.uk/products/os-terrain-50",
      "resolved_download_url": "...",
      "artifact_refs": ["raw-sha256-..."],
      "use": ["elevation-profile", "relief-hachures"],
      "rights_status": "reviewed",
      "license": {
        "id": "OGL-3.0",
        "url": "...",
        "commercial_use": true,
        "derivatives": true,
        "redistribution": true
      },
      "attribution": "Contains OS data © Crown copyright and database right 2026.",
      "license_snapshot_ref": "license-sha256-..."
    }

Unknown rights_status, absent attribution, or absent licence snapshots must fail a release build.

### Context record

    {
      "status": "source-derived-artwork-context",
      "navigation_status": "artwork-not-for-navigation",
      "display": {
        "crs": "EPSG:27700",
        "extent_strategy": "route-contain-expand-to-map-rect",
        "extent_projected": [0, 0, 0, 0],
        "extent_wgs84": [0, 0, 0, 0],
        "map_rect_mm": [0, 0, 0, 0],
        "estimated_m_per_mm": 0,
        "padding_mm": 6
      },
      "asset_ref": "derived-context-gpkg-sha256-...",
      "selection_policy": "hike-a5-context-v1",
      "layers": {
        "coast_water": {
          "source_refs": ["src-..."],
          "input_layers": ["..."],
          "filter": "...",
          "geometry_policy": "hydro-exclusivity-v1",
          "selection": "...",
          "pen_id": "blue-0-25"
        },
        "woodland": {
          "source_refs": ["src-..."],
          "input_layers": ["..."],
          "filter": "exact documented woodland classes only",
          "geometry_policy": "woodland-hachure-v1",
          "selection": "...",
          "pen_id": "green-0-25"
        },
        "settlements": {
          "source_refs": ["src-..."],
          "selection": "hike-a5-labels-v1",
          "pen_id": "black-0-25"
        },
        "peaks": {
          "source_refs": ["src-..."],
          "selection": "hike-a5-labels-v1",
          "pen_id": "black-0-25"
        }
      }
    }

### Elevation record

    {
      "status": "source-derived",
      "dem_source_ref": "src-...",
      "raw_artifact_refs": ["raw-sha256-..."],
      "horizontal_crs": "EPSG:...",
      "vertical_crs": "source-declared value",
      "nodata": -9999,
      "profile": {
        "sample_spacing_m": 50,
        "interpolation": "bilinear",
        "asset_ref": "derived-elevation-profile-sha256-..."
      },
      "relief": {
        "algorithm": "downslope-hachures-v1",
        "parameters": {
          "slope_floor_deg": 8,
          "seed_spacing_mm": [1.8, 3.0],
          "line_length_mm": [0.9, 2.4],
          "stroke_width_mm": 0.25,
          "smoothing_sigma_cells": 1.0,
          "seed": 0,
          "route_exclusion_mm": 1.2,
          "water_exclusion_mm": 0.6,
          "label_exclusion_mm": 1.0
        },
        "asset_ref": "derived-hachures-sha256-..."
      }
    }

These relief parameters are version-one tunables, not universal truths. Freeze their exact values in every asset recipe. Calculate slope and aspect from a smoothed, projected DEM; use deterministic blue-noise seeds and trace short downslope lines. Express slope through spacing and line length, not a sub-nib width. Every emitted mark must still pass the physical 0.75 mm minimum for a 0.25 mm nib.

### Label record

    {
      "status": "source-selected",
      "policy": "hike-a5-labels-v1",
      "asset_ref": "derived-labels-sha256-...",
      "records": [
        {
          "id": "label-...",
          "source_ref": "src-...",
          "source_feature_id": "...",
          "class": "settlement|peak|water|range|sea|endpoint",
          "source_text": "...",
          "display_text": "...",
          "language": "...",
          "geometry_mode": "source-point|source-line|source-polygon|editorial-anchor",
          "anchor_projected": [0, 0],
          "elevation_m": null,
          "vertical_crs": null,
          "rank": 1,
          "selection_reason": "..."
        }
      ]
    }

Preserve both source and displayed text. A range name requires a sourced geometry or an explicit editorial anchor tied to a cited source and rationale; it must not be inferred from DEM relief. Peak elevation can be printed only when its elevation source and vertical reference are known.

### Raw and derived artifact records

    {
      "id": "raw-sha256-...",
      "original_filename": "...",
      "immutable_or_resolved_url": "...",
      "retrieved_at": "...",
      "upstream_checksum": "...",
      "sha256": "...",
      "bytes": 0,
      "edition": "...",
      "metadata_sha256": "...",
      "license_snapshot_sha256": "..."
    }

    {
      "id": "derived-context-gpkg-sha256-...",
      "media_type": "application/geopackage+sqlite3",
      "storage_uri": "artifact-store/sha256/ab/...",
      "sha256": "...",
      "bytes": 0,
      "crs": "EPSG:...",
      "bbox": [0, 0, 0, 0],
      "parents": ["raw-sha256-..."],
      "toolchain": {
        "name": "hike-context-builder",
        "version": "...",
        "container_digest": "sha256:..."
      },
      "parameters": {},
      "created_at": "..."
    }

Every plate should reference a plot manifest recording the exact SVG digest, derived-asset digests, pen order, nominal pen widths, page dimensions, renderer version, and validation result.

## Offline snapshot and build design

Suggested storage:

    artifact-store/sha256/ab/<full-hash>
    snapshots/hike-context/<plate-id>/<edition>/
      manifest.json
      source-licenses/
      recipes/
      derived/context.gpkg
      derived/elevation-profile.json
      derived/hachures.json
      derived/labels.json
      qa/

Rules:

- resolve any “latest” URL only during acquisition;
- hash the downloaded archive and all extracted members;
- preserve upstream metadata and licence text beside the source manifest;
- make raw artifacts read-only;
- pin pyproj/PROJ, GDAL or rasterio, Shapely, and osmium versions in a container digest;
- perform filtering, repairing, clipping, projection, and simplification only in acquisition/derivation;
- keep the renderer network-disabled and make it consume only derived artifacts;
- do not commit very large raw national datasets to Git; commit manifests, recipes, small derived assets, or use a content-addressed remote artifact store;
- ship SVG, PNG preview, plot manifest, SOURCES.json, LICENSES.txt, checksums, and any ODbL subset/recipe offer required for the release.

The present environment has Shapely but does not have a complete pinned projection/raster acquisition stack. Add that stack explicitly rather than relying on ambient executables.

Validation must fail closed on a missing file, hash mismatch, unknown source reference, non-finite coordinate, unresolved CRS, invalid nodata handling, unknown rights status, or missing required attribution. It must never fall back from a missing factual asset to synthetic terrain.

## A5 rendering policy

### Water and coast

Use hydro-exclusivity-v1:

- one canonical coastline, using the selected source's defined shoreline;
- one waterbody polygon rendered as one closed shoreline;
- for a physically wide river, render factual banks and suppress its centreline;
- for a narrow river or stream, render one factual centreline and suppress polygon banks;
- never render polygon boundary and centreline for the same reach;
- never duplicate mean-high-water and low-water coastlines;
- never invent a connection through a bridge gap;
- use a continuous source network, such as OS Open Rivers, when continuity matters;
- render ferry legs as a distinct dashed blue feature, never as a red bridge.

“Thicker river” should mean a truthful bank-to-bank geometry or, on a deliberately hydro-heavy reduced-pen plate, a native 0.40 mm blue pen. It must not be faked with parallel offset lines. The current six-pen plan has only blue 0.25 mm; adding both blue nibs while retaining every other role would make seven pens. A future role-based pen plan may substitute blue 0.40 for blue 0.25 when green or grey is omitted.

Do topology-preserving simplification in projected coordinates and rely on round line joins/caps for plotter quality. Do not apply arbitrary Chaikin smoothing to factual shorelines; angular coastlines usually indicate an inadequate source resolution or excessive simplification.

### Woodland

- accept only documented woodland, forest, or tree-cover classes;
- never equate every green land-cover class with woodland;
- repair and dissolve polygons, subtract water, and clip to the display extent;
- generate deterministic, sparse, page-space V or tick hachures;
- make every 0.25 mm-pen mark at least 0.75 mm long;
- omit a polygon too small to hold one complete compliant mark;
- avoid the route, water, labels, title area, and attribution area;
- omit the green pen entirely where the route has no relevant, sourced woodland.

### Relief

Derive relief from the frozen DEM:

1. mosaic and clip the raw DEM in its documented horizontal and vertical references;
2. reproject into the plate CRS;
3. downsample only to the resolution needed by the final page;
4. smooth deterministically;
5. compute Horn 3 by 3 slope and aspect;
6. place deterministic blue-noise seeds above the slope threshold;
7. trace short downslope hachures;
8. apply route, water, and label exclusion masks;
9. run physical mark-length validation.

Do not use DEM contours or hachures to invent mountain names. Do not mix vertical systems without recording and testing the transformation or seam.

### Labels

For long A5 routes:

- always label both endpoints;
- normally select four to eight major settlements;
- select zero to three peaks;
- select at most one or two range, region, lake, or sea names;
- place labels by deterministic collision priority;
- keep 0.25 mm text caps at or above 2.0 mm;
- retain source IDs for every factual label.

The compact French loops can support two to four peaks and more detailed water labels. Camino Francés should generally omit individual peaks. Laugavegur should privilege official Icelandic geographic and hydrographic names. Hebridean Way should privilege island, sound, and sea names.

### Attribution layout

The existing approximately 4 mm credit band, and especially the narrow landscape credit area, is not sufficient for every multi-source release. Reduce the number of publishers where possible, but do not solve the issue by deleting credits. Reserve a larger visible credit region, use a verso or accompanying certificate for expanded credits, and ship full digital licences and manifests. Before sale, verify whether the precise licence permits abbreviated visible credit with expanded information elsewhere; do not assume SVG metadata or a verso alone satisfies it.

## Quality gates

### Automated

- all source and artefact references resolve and hashes match;
- rights status and required attribution are present;
- every geometry has a declared CRS and finite coordinates;
- display extent contains the full route and fills the true map-rectangle aspect;
- route geometry digest remains unchanged from the approved v1 source unless a separately reviewed replacement is intentional;
- geometries are valid after repair and clipping;
- hydrography has no duplicate banks/centrelines or duplicate coast source;
- elevation nodata and vertical reference are explicit;
- all hachures pass minimum physical length;
- all strokes meet at least three times nominal nib width;
- all caps and isolated marks meet at least eight times nominal nib width;
- no more than six used pens, ordered contiguously, with no empty pen layers;
- labels retain source reference, feature ID, class, and collision result;
- format validator and plot simulation pass at 1:1 output size.

Report estimated plot time and ink coverage, but do not fail or cull the plate on ink coverage.

### Visual route proofs

- Hebridean Way: islands are recognisable, both ferry legs are distinct, and no double coastline appears.
- Great Glen Way: the lochs read as one geographic sequence, each shoreline is single, and channels are not broken at bridges.
- French loops: hachures follow genuine slope, streams remain legible, and route red stays dominant.
- Laugavegur: no invented forest, conventional blue hydrography, and no unsupported sea context.
- Camino Francés: the full route remains contained and legible without a carpet of minor streams, peaks, or land-cover symbols.

Each proof should be inspected as the SVG, as a raster PNG at intended print size, and in the plot simulator. At least one physical calibration plot is required before the style is called production-ready.

## Prioritised implementation plan

### P0 — factual integrity and reproducibility

1. Create schema v2 and its fail-closed validator.
2. Implement a real CRS pipeline and map-rectangle aspect expansion.
3. Add the content-addressed artifact/source/license manifest.
4. Disable synthetic backdrops for production plates.
5. Allocate the elevation panel only when a verified profile exists.
6. Preserve visible OpenStreetMap attribution for the seven OSM route plates, or source reviewed replacement routes.

### P1 — four proof plates

1. Acquire and freeze Great Britain, France, Spain, and Iceland source snapshots.
2. Implement water exclusivity, woodland hachures, DEM relief, profile sampling, and label selection as independently versioned derivations.
3. Generate Great Glen, Hebridean, Tour des refuges, and Camino proofs.
4. Run automated, visual, and physical plotter QA; freeze approved parameters.

### P2 — complete all ten

1. Add the remaining British routes.
2. Add Swiss data and resolve the Alpine Passes cross-border elevation/credit decision.
3. Add Tour du Pic de Valsenestre and Laugavegur.
4. Generate final SVG and PNG previews plus per-plate plot/source manifests.

### P3 — product refinement

1. Offer A3 or staged A5 editions for Camino and other long routes.
2. Add user-owned GPX ingestion with privacy controls and per-route provenance.
3. Add calibrated role-based pen substitution without exceeding six pens.
4. Build a release dashboard for source edition age, attribution, artifact hashes, plot time, and physical calibration status.

## Release decision

Proceed, but do not bulk-generate the ten “finished” contextual hiking products from the current v1 renderer. The first implementation milestone is the schema/provenance/projection foundation and four contrasting proof plates. Once those pass, the remaining routes are mostly data acquisition and editorial selection rather than a new rendering problem.

The defining product rule is simple: every factual line, mark, elevation, and name must be traceable to an immutable source snapshot or omitted. That rule produces cleaner artwork as well as safer, reproducible commercial editions.
