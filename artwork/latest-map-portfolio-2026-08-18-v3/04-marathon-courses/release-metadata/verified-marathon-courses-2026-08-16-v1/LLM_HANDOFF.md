# LLM handoff — marathon course plates

Do not merge this cohort with the older marathon city previews. The defining
gate is `source-contract/route-source-manifest.json`: each red line must resolve
to its normalized route, raw bytes, organiser evidence and accepted measurement.

Order of operations:

1. Verify all hashes in `CHECKSUMS.sha256`.
2. Read `CODEX_MAP_HANDOFF.md` and `docs/reproducibility/REPRODUCING_MAPS.md`.
3. Read the route and OSM source manifests before changing a course or extent.
4. Never trace a PDF/image or repair a route by drawing missing links.
5. Re-run the 42.195 km and largest-component gates after any source change.
6. Render only from `routes/normalized/` plus the matching pinned OSM JSON.
7. Require a non-empty `race_course` layer and embedded verification evidence.
8. Run `tools/validate_format.py`, PlotSim, PNG pairing and contact sheets.
9. Keep outputs under `review-output`; they are not finished physical artwork.

Valencia's normalization is intentionally selective: only the exact Placemark
`RECORRIDO · RACE LINE` is used. London extracts only `__ROUTE_DATA__.coordinates`.
Stockholm extracts only `SM26h.gpx` from the organiser ZIP. Boston assembles
only OSM relation/11680552. These transformations are evidence, not optional
cleanup.

Before calling any route “current,” re-check the organiser page. Berlin and
Sydney are consciously retained as the latest organiser-published 2025 vectors;
do not relabel them 2026 without a new official vector.
