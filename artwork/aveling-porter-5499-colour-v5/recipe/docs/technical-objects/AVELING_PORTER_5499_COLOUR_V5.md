# Aveling & Porter No. 5499 - colour edition 5 (pen lines only)

Package: `examples/technical-objects/aveling-porter-5499-colour-v5/`.
Builder: `tools/engineering_source_plates/build_aveling_5499_colour_v5.py`.
Plan, painters and verifier: `tools/engineering_source_plates/aveling_5499_colour_v5/`.

Edition 5 revises edition 4 after review:

- **Scrapers:** the rear scraper (bracket, boss, arm and blade) is pure Red
  lines. The open space between its arm and the spring rod is green tender. The
  forward scraper's bearing and adjuster mounts are red with its arm.
- **Black iron:** the wheels, hubs and flywheel are close, even Black circles
  (0.38 mm apart), so they read as black iron.
- **Outlines:** engine linework is drawn one pen heavier than the blueprint:
  0.30 → Black 0.40, 0.40 → Black 0.60 and the roller tyres 0.50 → Black 1.00.
  These keep their original weights:
  - fasteners, rivets, chains and threads;
  - small fittings, scrapers and controls;
  - features under 3 mm;
  - lettering, the badges and the sheet frame.
- **Through the rear wheel:** everything up to the tender's straight front edge
  (x = 318.865 mm), and anything above its top rim (y = 150.335 mm), is black
  iron in Black lines about 0.69 mm apart (36% ink). That is a shade lighter
  than the wheel rims, so the wheel stands in front of it. The green tender
  continues beyond that edge.
- **Top-right badges:** the worksplate and Invicta horse carry no colour, only
  their black engraving.

The source is the hash-pinned final revision-14 SVG. All 863 path shapes are
kept byte-for-byte; only their black pen weights follow the mapping above
(`regions.black_pen`), which the verifier recomputes independently.

## Unchanged from edition 4

- Colour is only single-pass pen lines inside enclosed paper cells, at least
  0.22 mm clear of black ink.
- Every spoke is ruled with a fixed count of lines parallel to its edges: five
  on the rear wheel, four on the front roll.
- The front roll interior stays paper.
- Cylinders are graded lines lit from the upper left.
- Gold marks the boiler bands, safety valves, whistle, lubricator and the maker
  plate on the cylinder.

## Fine gold

The studio has no broad gold pen, so the edition carries its own inventory
(`inventory.py`, id `studio-pens-fine-gold`): the studio template with the
1.00 mm gold replaced by the studio's 0.40 mm gold. Every gold part is several fine lines
0.50 mm apart:

- two per boiler band;
- four per valve column;
- eight on the whistle;
- seventeen on the cylinder's maker plate.

The gold pen is 0.40 mm, confirmed by the user. Change `GOLD_NIB_MM` and
rebuild if the gold pen changes.

## Plotting

Eight pen loads: Gold 0.40, Green, Red, Grey, Black 0.25, 0.40, 0.60, 1.00.
The optimised nominal simulation is about 54 minutes, with 4,029 strokes;
document order would take 61 minutes.

`verify.py` independently checks:

- that the outline shapes are unchanged and on their mapped pens;
- that the fills match the plan and stay contained;
- the paper gaps, and that the top-right badges carry no colour;
- that the spoke lines are straight and parallel;
- the pen files, lettering and the binding format validator.
