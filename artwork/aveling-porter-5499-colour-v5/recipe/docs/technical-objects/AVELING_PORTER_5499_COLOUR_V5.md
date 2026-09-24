# Aveling & Porter No. 5499 - colour edition 5 (pen lines only)

Package: `examples/technical-objects/aveling-porter-5499-colour-v5/`.
Builder: `tools/engineering_source_plates/build_aveling_5499_colour_v5.py`.
Plan, painters and verifier: `tools/engineering_source_plates/aveling_5499_colour_v5/`.

Edition 5 revises edition 4 after review:

- **Scrapers:** the rear scraper (bracket, boss, arm and blade) is red-brown.
  The open space between its arm and the spring rod is green tender. The
  forward scraper's bearing and adjuster mounts are red-brown with its arm.
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
- **Boiler bands:** solid gold from one black edge to the other (see
  [Boiler bands](#boiler-bands)).
- **Open air and review corrections:** see
  [Review corrections](#review-corrections).

The source is the hash-pinned final revision-14 SVG. All 863 path shapes are
kept byte-for-byte. Only their pens follow the mapping above
(`regions.outline_pen`): the Black weights, plus Gold for the four band inner
lines. The verifier recomputes the mapping independently.

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
1.00 mm gold replaced by the studio's 0.40 mm gold. Every gold part is several
fine lines. The fittings are lined 0.50 mm apart:

- four per valve column;
- eight on the whistle;
- seventeen on the cylinder's maker plate.

The gold pen is 0.40 mm, confirmed by the user. Change `GOLD_NIB_MM` and
rebuild if the gold pen changes.

## Boiler bands

Each boiler band is drawn with a black edge either side and a thin inner line
only 1.08 mm from its front edge. A 0.40 mm gold line cannot fit beside it
with the 0.22 mm white gap, so that side of each band was left white.

The inner lines (model paths 150, 151, 156 and 159) are therefore drawn in
Gold, unchanged in shape. They no longer divide the paper, so each band is one
cell between its black edges. The band painter fills it with five upright gold
lines about 0.42 mm apart, from one edge to the other, with the inner line as
one of them. The band reads as a solid gold bar.

The short pieces of band are too short for upright lines, so they take level
gold lines across the band's full width. These are the pieces below the
boiler's lower line, and on the band nearest the flywheel, the pieces above
the pump rod up to the motion plate. Every band is gold from end to end.

These four lines are the only source lines not drawn in black. They reach
black ink only where the blueprint's lines join them. Elsewhere they keep
0.22 mm of white paper from black and from every other colour; the verifier
checks both.

## Review corrections

A review against the side photograph found five faults, now corrected:

- **Air under the regulator rod:** the rod runs from the handle, behind the
  flywheel, to the front. The open air under it, on both sides of the
  flywheel, had been hatched black; it is now paper. The lubricator, its
  pedestal and pipe, the rod and its stay stay black.
- **Between the lubricator pipes:** the space between the pedestal and the
  pipe had been hatched black; it is now paper.
- **Boiler beside the flywheel:** the boiler barrel seen between the motion
  plate and the pump rod had been drawn as black iron; it is now green.
- **Top of the band nearest the flywheel:** the band now runs in gold up to
  the motion plate.
- **Front roll:** the sliver of the top-right far spoke, visible beside the
  fork, is now ruled green.

The whistle's top is now gold as well. `design.OPEN_AIR` lists points that
must stay paper, and `design.MUST_CARRY` lists points that must carry a
given ink. The verifier fails if any of them regresses.

## No grey pen

The studio has no grey pen, so the black-painted iron is hatched in Black
alone and its tone comes from line spacing:

- **Plates** (headstock, firebox, horn plates, fittings): lines 0.89 mm apart
  at the upper left, closing to 0.69 mm at the lower right.
- **Chimney and smokebox:** graded as cylinders, from 14% ink in the light to
  46% in shadow.
- **Collars, rings and controls:** lines 0.70 mm apart.
- **Bright steel rods:** lines 1.0 mm apart.

The iron seen behind the rear wheel (36% ink) stays a touch darker than the
firebox in front of it. Grey is left out of the edition inventory, and the
verifier fails if any grey appears.

## Red-brown

The studio's red pen is too bright on its own. The fork, scrapers, scraper
mounts and chain spring bar are therefore red-brown: Red 0.25 lines 0.60 mm
apart, with a Brown 0.25 line in every gap. Red and brown alternate 0.30 mm
apart and read as a deeper, warmer red, close to the engine's red-brown paint.
The brown pen is plotted straight after the red. The verifier checks that
every red part carries brown lines.

## Plotting

Eight pen loads: Gold 0.40, Green, Red, Brown, Black 0.25, 0.40, 0.60, 1.00.
The optimised nominal simulation is about 48 minutes, with 3,497 strokes;
document order would take 55 minutes.

`verify.py` independently checks:

- that the outline shapes are unchanged and on their mapped pens;
- that the gold band inner lines reach black only where the blueprint's lines
  join them;
- that open air stays paper and every corrected area carries its ink;
- that the fills match the plan and stay contained;
- the paper gaps, and that the top-right badges carry no colour;
- that the spoke lines are straight and parallel;
- the pen files, lettering and the binding format validator.
