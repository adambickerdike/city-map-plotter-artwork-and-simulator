# Golf course source contract v2

This contract freezes the geographic evidence for the **Twenty-Five Icons of
Golf** collection. The 25 deterministic gzip files in `source-extracts/` are
small, filtered snapshots acquired from the official OpenStreetMap API 0.6 map
endpoint on 2026-08-05. `source-manifest.json` records each request URL, raw
response SHA-256, extract SHA-256, root `leisure=golf_course` object, root
timestamp, feature counts, and the exact selection rule.

Each extract contains:

- the source boundary paths and exact root object/version;
- exactly one source centreline for every numbered hole 1–18;
- mapped tees, greens, fairways, bunkers, rough, water hazards and pins;
- mapped course paths, water, vegetation and buildings intersecting the
  course-selection geometry; and
- each retained object's OSM ID, version, timestamp, tags and WGS84 points.

The renderer does not fetch live data and never synthesises an absent hole or
surface. Augusta's separate nine-hole par-three course creates duplicate refs
1–9 inside the club boundary, so the contract records and applies the
source-object rule “longest mapped centreline per duplicate number.” Pinehurst
uses explicit `1 - #2`…`18 - #2` refs. Royal Melbourne West uses explicit
`1W`…`18W` refs. Multi-course roots fail closed unless a source tag identifies
the intended route: Turnberry uses `course:name=Ailsa`, Sunningdale uses
`golf:course:name=Sunningdale Old Course`, and Ballybunion uses
`name=Old Course`. Those selectors are persisted in each extract.

`tools/build_golf_catalog.py` verifies every compressed hash, projects every
course through one shared local-equirectangular metre grid, applies only a
0.5 m catalog simplification (well below physical plotting resolution), and
regenerates `src/city_map_plotter/data/golf-courses-v2.json`.

Refresh sources deliberately:

```bash
.venv/bin/python tools/acquire_golf_geometry.py --all
.venv/bin/python tools/build_golf_catalog.py
PYTHONPATH=src .venv/bin/python -m unittest tests.test_golf
```

The source-derived data remains © OpenStreetMap contributors under ODbL 1.0.
Official course and championship pages in the packaged catalog support names
and short factual context only; no proprietary course plan, logo, photography
or trade dress is traced. These drawings are review studies, not surveys,
scorecards, navigation aids, rankings, or evidence of endorsement.
