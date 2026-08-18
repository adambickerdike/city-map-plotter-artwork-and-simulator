# Reproduce from pinned source evidence

Run from the city-map-plotter repository root. This mode performs no route or
basemap download; it copies and verifies the existing source contract, then
reruns the course gate, renderer, PNG export, PlotSim, format validator and
contact-sheet builder.

```bash
.venv/bin/python tools/build_verified_marathon_course_series.py   --reuse-sources review-output/marathon-course-plates-verified-2026-08-16-v1/source-contract   --output-dir review-output/marathon-course-plates-reproduction
```

For a fresh current-source audit, omit `--reuse-sources`. A fresh run may yield
different hashes if an organiser has replaced its official vector or page.
Those changes must be reviewed; they are not silently treated as the same
course edition.
