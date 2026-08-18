# Frozen 2026 circuit source contract

This contract freezes the factual and geographic evidence used by the 2026
circuit poster renderer.  The event registry is frozen at **2026-08-10** and
contains 22 WMSC-listed events plus the conditional Sepang event at calendar
position 16.  The called-off Sakhir and Jeddah events are retained only in the
explicit exclusion ledger.  Event identity and physical host country are
separate fields, so Sepang remains a Bahrain event hosted in Malaysia.

Official Formula 1 and FIA pages are stored as hash-bound evidence for calendar
and short factual transcription. Current FIA event PDFs may also be frozen as
reference-only factual documents, explicitly marked as prohibited geometry
sources. No standalone official map image, logo, photograph, or protected
graphic is acquired or traced. Circuit geometry and local context come from
immutable OpenStreetMap Overpass snapshots under ODbL 1.0, with the raw
selected-track response and raw derived-bounding-box context response retained
beside their deterministic union.

The offline compiler fails closed and exposes two non-equivalent geometry
tiers. `cartography-qualified-centreline` requires the selected relation, or an
explicit ordered way list, to form one exact-endpoint closed cycle without an
invented connector, an official race-page configuration identity, source
object lineage, and no more than 1.0% difference from the published length.
It permits only a visibly disclosed base map; missing direction, pit topology,
start/finish and turn stations stay absent. `source-qualified` additionally
requires the existing source-backed pit lane, source-backed start/finish anchor
and complete turn-station gates. Curvature-derived turn anchors are explicitly
labelled `geometric-turn-station-not-racing-apex`; they are plot-registration
stations, not claims about a driver's racing line.

Curated famous-course labels keep two independent lineages. The displayed
name is an exact copy from a frozen official textual source, while its drawing
anchor is an exact, separately frozen OSM way or set of ways. Registry records
contain source-object selections and tag assertions, never hand-entered
coordinates; the compiler neither snaps nor traces an inferred turn or apex.
Where an OSM context way only associates a name with a nearby part of the lap,
the plate visibly discloses the associative anchor instead of presenting it as
an official coordinate.

Nearby water, vegetation, buildings, grandstands, roads, and access roads are
retained only when backed by OSM object IDs, versions, tags, and the relevant
snapshot source reference.  Operational race-control overlays remain withheld
until a current 2026 FIA event circuit document is acquired and pinned.  Madrid
remains a homologation hold and Sepang remains a WMSC/calendar hold regardless
of whether source geometry can be assembled.

Acquire and build deliberately:

```bash
.venv/bin/python tools/acquire_f1_circuit_sources.py --all
.venv/bin/python tools/build_f1_circuit_catalog.py
.venv/bin/python tools/build_f1_circuit_catalog.py --check
```

For disjoint parallel acquisition, clone the official-source manifest and pass
each worker its own `--manifest PATH`; merge worker manifests by unique source
ID before the offline build.  Event output paths must remain disjoint.

`source-manifest.json` binds every payload and compressed payload hash, query,
endpoint, retrieval time, OSM base timestamp, rights statement, and raw
component snapshot.  `catalog-schema.json` describes the serialised contract;
`city_map_plotter.f1_circuits.validate_f1_catalog` is the stricter semantic
gate shared by the builder and renderer.

All generated products remain on production hold pending circuit-outline legal
clearance, physical pen calibration, and a plotted proof.  OSM-derived output
must carry “© OpenStreetMap contributors” and the OSM copyright URL.
