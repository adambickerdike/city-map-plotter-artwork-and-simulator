# LLM handoff — University of Cumbria / personalised Carlisle v4

## Accepted outcome

The accepted plate is A3 portrait at approximately 1:10,796. It retains the
v2/v3 Fusehill-centred extent and corrects the header, footer, and station
priority requested on 31 August 2026.

## Non-negotiable decisions

1. Keep `UNIVERSITY OF CUMBRIA` at upper left.
2. Keep `54.8940 N   /   2.9263 W` immediately below it. The title and
   coordinate path bounds must both start at x = 25.635 mm.
3. Use the original narrow `diamond-cardinal` compass in
   `memorabilia_compass`. Its exact path bounds are x 258.470, y 34.115,
   width 8.830, height 27.595 mm. Do not put it back in `head_rail`.
4. Set the footer in `hershey-serif-medium-ascii-v1` on Black 0.40 mm.
5. Draw `Stuart R. Nelis` at 8.024 mm cap height.
6. Draw `BSc Applied Psychology` and right-aligned `2024` at 7.08 mm.
7. Draw no labels, honours copy, or writing rules.
8. Keep every footer ink envelope within the horizontal bounds of `map_field`.
9. Require OSM `way/566812584`, the pinned `building=train_station` footprint,
   and require one corresponding physical SVG path.
10. Retain all seventeen mandatory Fusehill campus buildings. Do not force the
    physically sub-floor unnamed outbuilding `way/1108502395`.
11. Keep bbox `-2.9460 54.8815 -2.9065 54.9065`, A3 portrait,
    `university-memorabilia`, `clean-personalised`, `plotter-faithful`, 0.04 mm,
    centreline roads, dotted water, and `actual-pens`.
12. Rebuild only from the pinned JSON; a live refresh is a new edition.
13. Keep `SOURCE_DATE_EPOCH=1788134400`; it fixes generation metadata at
    `2026-08-31T00:00:00+00:00` and removes time-based rebuild drift. The
    stored master hash is the portable package identity; rebuilding from a
    different checkout path records that path in source provenance and binds a
    correspondingly new plot job.
14. Keep external OpenStreetMap attribution with every public copy.
15. Do not enable hardware execution without calibration and human inspection.

## Station selection consequence

The A3 landmark cap remains 32 objects. Reserving Carlisle station displaces
the lower-priority automatically selected Carlisle Civic Centre footprint
(`way/222903738`). This is deliberate and recorded in `SOURCE-CONTRACT.json`;
the renderer's global object/ink budget was not silently raised.

## Reproduction hashes

- Pinned source SHA-256:
  `ea43b048925265b1a541502f1bf9cb7ee060df235f828a9b547d442e0173b47a`
- Canonical source data:
  `71b2cfde629fb1dcea40f920e0a43e294f98374e747d69104ddea775e77fdbe6`
- Exact query:
  `5a082835e885f67fe452bf837dfd68ce4857acc0f5cbe5d51d3c6f1ed18d0c36`
- Master SVG:
  `131c4daf7cf6cad25a8f15d6177c13de3cc34b477cdd392262a61da16ad3fa2d`
