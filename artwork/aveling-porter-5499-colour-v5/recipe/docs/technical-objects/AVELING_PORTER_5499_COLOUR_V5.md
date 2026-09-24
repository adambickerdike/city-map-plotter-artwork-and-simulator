# Aveling & Porter No. 5499 - colour edition 5 (pen lines only)

Package: `examples/technical-objects/aveling-porter-5499-colour-v5/`.
Builder: `tools/engineering_source_plates/build_aveling_5499_colour_v5.py`.
Plan, painters and verifier: `tools/engineering_source_plates/aveling_5499_colour_v5/`.

Edition 5 revises edition 4 after review:

- **Scrapers:** the rear scraper (bracket, boss, arm and blade) is red-brown.
  The open space between its arm and the spring rod is green tender. The
  forward scraper's bearing and adjuster mounts are red-brown with its arm.
- **Black iron:** the wheels, hubs and flywheel are even black circles
  (0.42 mm apart), so they read as black iron without going solid.
- **Outlines:** engine linework is drawn one weight heavier than the
  blueprint: 0.30 → 0.40 mm, 0.40 → 0.60 mm and the roller tyres
  0.50 → 1.00 mm. Every black line is the one Black 0.25 mm pen (see
  [One fine black pen](#one-fine-black-pen)). These keep their original
  weights:
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
- **Boiler bands:** solid gold from one black edge to the other and from end
  to end (see [Boiler bands](#boiler-bands)).
- **Regulator rod:** redrawn straight and level (see
  [Regulator rod](#regulator-rod)).
- **Review corrections:** see [Review corrections](#review-corrections).

The source is the hash-pinned final revision-14 SVG. Its path shapes are kept
byte-for-byte, except the regulator-rod redraw (`regions.GEOMETRY_EDITS`).
Each path's weight comes from the mapping above (`regions.outline_weight`),
with Gold for the four band inner lines. The verifier recomputes the mapping
independently.

## Unchanged from edition 4

- Colour is only single-pass pen lines inside enclosed paper cells, at least
  0.22 mm clear of black ink. The band gold is the one exception: it runs
  under the boiler lines that cross the bands.
- Every spoke is ruled with a fixed count of lines parallel to its edges: five
  on the rear wheel, four on the front roll.
- The front roll interior stays paper.
- Cylinders are graded lines lit from the upper left.
- Gold marks the boiler bands, safety valves, whistle, lubricator and the maker
  plate on the cylinder.

## One fine black pen

The studio has no thick black pen; its black is a single 0.25 mm pen. The
edition inventory (`inventory.py`, id `studio-pens-fine-black`) therefore lists
Black 0.25 mm as the only black. Every black line is drawn with it, and each
heavier line is built from overlapping 0.25 mm strokes
(`regions.stroke_rings` and `regions.outline_strokes`):

| Weight | Width | Strokes |
|---|---:|---|
| fine | 0.25 mm | the line itself |
| engine | 0.40 mm | one loop 0.075 mm either side of the line |
| principal | 0.60 mm | a loop 0.175 mm either side, plus the line itself |
| silhouette | 1.00 mm | loops 0.375 and 0.1875 mm either side, plus the line itself |

A loop is the outline of the line widened by its distance. The pen following
it inks from half a nib inside to half a nib outside that distance. With the
strokes no more than 0.19 mm apart, every line prints solid to exactly its
width, with round ends and joins. The black ink therefore covers the same
band as before, and every colour fill and clearance is unchanged. The sheet
frame sits on the edge of the plotter-safe area, so it is built inward: the
line itself plus two inner loops.

The verifier rebuilds each heavier line's ink from its exported strokes. It
checks that the ink never goes beyond the line's width and covers at least
99% of it.

## Fine gold

The studio has no broad gold pen, so the inventory also replaces the template's
1.00 mm gold with the studio's 0.40 mm gold. Every gold part is several fine
lines. The fittings are lined 0.50 mm apart:

- four per valve column;
- eight on the whistle;
- seventeen on the cylinder's maker plate.

The gold pen is 0.40 mm, confirmed by the user. Change `GOLD_NIB_MM` and
rebuild if the gold pen changes. The whistle's top, above the valve lever, is
left uncoloured.

## Boiler bands

Each boiler band is drawn with a black edge either side and a thin inner line
only 1.08 mm from its front edge. A 0.40 mm gold line cannot fit beside it
with the 0.22 mm white gap, so that side of each band was left white.

The inner lines (model paths 150, 151, 156 and 159) are therefore drawn in
Gold, unchanged in shape. They no longer divide the paper, so each band is one
cell between its black edges. The band painter fills it with five upright gold
lines about 0.42 mm apart, from one edge to the other, with the inner line as
one of them.

Each gold line runs the band's whole length, unbroken. Where a thin black line
crosses the band, the gold carries on beneath it, as the band's inner line
does. That happens at the boiler's lower line near every band's foot, and at
the motion plate's lower edge on the band nearest the flywheel. The gold
stops only where the pump rod passes in front of that band. Two pieces of a
band with a gap under `design.BAND_BRIDGE_MM` (1.5 mm) between them are
joined; the pump rod's gap is 2.9 mm. The verifier checks that the band gold
crosses only those two lines (`design.BAND_CROSSING_LINES`).

These four inner lines are the only source lines not drawn in black. They reach
black ink only where the blueprint's lines join them. Elsewhere they keep
0.22 mm of white paper from black and from every other colour; the verifier
checks both.

## Regulator rod

The regulator rod runs from the reversing lever on the platform, behind the
flywheel, to the front. Revision 14 drew it badly:

- sloping behind the lever;
- level beyond the flywheel, but at a different height;
- ending in a shallow slant that notched the motion plate beside the
  lubricator.

This edition redraws it as one straight, level rod, 1.82 mm thick (106.655 to
108.475 mm), from the lever boss to a rounded bend. The bend turns the rod
straight down onto the motion plate, just clear of the lubricator's pipe.
The two brackets it passes through open to match. The plate's top edge now
runs unbroken from the lubricator's base.

That reshapes 9 paths and removes 2, the old slanted end; they are listed in
`regions.GEOMETRY_EDITS`.

## Review corrections

The first review against the side photograph found five faults, now corrected:

- **Air under the regulator rod:** the open air under the rod, on both sides
  of the flywheel, had been hatched black; it is now paper. The lubricator,
  its pedestal and pipe, the rod and its stay stay black.
- **Between the lubricator pipes:** the space between the pedestal and the
  pipe had been hatched black; it is now paper.
- **Boiler beside the flywheel:** the boiler barrel seen between the motion
  plate and the pump rod had been drawn as black iron; it is now green.
- **Top of the band nearest the flywheel:** the band now runs in gold up to
  the motion plate.
- **Front roll:** the sliver of the top-right far spoke, visible beside the
  fork, is now ruled green.

The second review asked for the following:

- **Bands:** continuous gold, not broken up by horizontal lines.
- **Rear-roll scrapers:** filled better. Both scrapers now use a solid painter
  (`painters._solid`):
  - strips are ruled edge to edge with red and brown alternating no more than
    0.30 mm apart;
  - other shapes take loops following their outline;
  - specks too small to hold a clean line are left paper.
- **Forward scraper's upper arm:** red. A seed of the black horn plates had
  claimed its interior; the arm is now red-brown.
- **Whistle top:** no gold line.
- **Front spokes:** every line runs along its own spoke's angle and carries
  straight on through every piece of that spoke. On each wheel, `ruled_set`
  gives a piece the lines of the full spoke whose band covers most of it. A
  spoke hidden for its whole length is ruled on the line through the wheel
  centre, as with the slivers beside the fork.
- **One fine black pen**, the **regulator rod**, and slightly less dense
  **black rings** (0.38 → 0.42 mm apart).

`design.OPEN_AIR` and `design.MUST_STAY_PAPER` list points that must stay
paper, and `design.MUST_CARRY` lists points that must carry a given ink. The
verifier fails if any of them regresses.

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
The two rear-roll scrapers use the solid painter, so they are red-brown right
to their edges. The brown pen is plotted straight after the red. The verifier
checks that every red part carries brown lines.

## Plotting

Five pen loads: Gold 0.40, Green, Red, Brown and Black, all 0.25 except the
gold. The optimised nominal simulation is about 50 minutes, with 3,599
strokes; document order would take 57 minutes.

`verify.py` independently checks:

- that the outline shapes are unchanged except the documented rod redraw, and
  drawn with the one Black pen (Gold for the band inner lines);
- that each heavier line's strokes cover its width and stay within it;
- that the gold band inner lines reach black only where the blueprint's lines
  join them, and that the band gold crosses only the documented boiler lines;
- that open air stays paper and every corrected area carries its ink;
- that the fills match the plan and stay contained;
- the paper gaps, and that the top-right badges carry no colour;
- that the spoke lines are straight and parallel;
- the pen files, lettering and the binding format validator.
