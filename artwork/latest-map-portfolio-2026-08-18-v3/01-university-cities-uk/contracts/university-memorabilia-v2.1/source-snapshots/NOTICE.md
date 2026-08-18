# University source snapshot notice

The 50 compressed files in `overpass/` are frozen OpenStreetMap query
responses captured for the reviewed university-memorabilia v2.1.3 visual
cohort and consumed unchanged by the source-pinned v2.1.4 recipe. They are data
inputs, not project-authored source code.

Map data © OpenStreetMap contributors. OpenStreetMap data is available under
the Open Database License (ODbL) 1.0:

- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

`source-manifest.json` binds each subject ID to its exact compressed response,
canonical decoded JSON, acquisition query, OSM base timestamp, and reviewed
render extent by SHA-256. `CHECKSUMS.sha256` provides a direct whole-bundle
integrity check. Keep this notice, manifest, and checksum file with any copy of
the snapshot set.

These snapshots make the reviewed input geometry repeatable; they do not turn
the resulting plates into production-ready physical plots. Pen calibration,
visual QA, source attribution, and any other release gates remain mandatory.
