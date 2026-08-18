# Golf course series workflow

## Scope

The collection is titled **Twenty-Five Icons of Golf**, not an objective world
ranking. It is a curated championship and architectural cross-section selected
only where the frozen source resolves exactly 18 numbered hole centrelines:

1. Augusta National
2. The Old Course, St Andrews
3. Pebble Beach Golf Links
4. Pinehurst No. 2
5. Oakmont Country Club
6. Shinnecock Hills Golf Club
7. Muirfield
8. Carnoustie Championship
9. Royal County Down
10. Dunluce Links at Royal Portrush
11. Royal Melbourne West
12. Cypress Point Club
13. Royal St George's
14. Royal Birkdale
15. Royal Troon Old Course
16. Turnberry Ailsa
17. Royal Dornoch Championship
18. Sunningdale Old
19. Ballybunion Old
20. Winged Foot West
21. National Golf Links of America
22. Seminole Golf Club
23. Whistling Straits
24. Hirono Golf Club
25. Cabot Cliffs

This is deliberately not claimed to be geographically comprehensive or a
ranked 1–25. It combines globally recognisable major venues, Open links and
celebrated architectural courses, subject to the strict complete-source gate.

## Physical drawing language

The full-detail edition is bound to `a3-portrait`. This is the format contract's
dense-map escape hatch: A4 remains preferred for ordinary maps, while the A3
field keeps 18 labels, fine hazards and source context above physical nib and
type floors. Its drawing contract is the renderer preset
`golf-clarity-course-a3-v4`.

| Meaning | Physical pen |
|---|---|
| selected course paths and inset bunker hachures | Grey 0.25 mm |
| illustrative playing-area envelope | Grey 0.40 mm |
| fairway outlines and fine-line fill inside greens only | Green 0.25 mm |
| green outlines; outline-only tee outlines | Green 0.40 mm |
| every closed water-dot symbol | Blue 0.25 mm |
| water and hazard outlines | Blue 0.40 mm |
| hole marker leaders/circles | Red 0.25 mm |
| hole numbers, buildings, north and scale | Black 0.40 mm |
| plate border/title | Black 1.00 mm |
| numbered hole centrelines | Gold 1.00 mm |

Every role uses a pen that exists in the studio inventory. The gold accent is
intentionally broad; there is no fictional fine gold pen. Furniture type comes
from the A3 `nib_roles_mm` table and map linework from
`map_linework_nib_mm`. All type is at least eight nibs high and every retained
path is at least three nibs long.

The raw catalog/root course boundary is deliberately kept as a selection mask
and is never emitted. It can describe a parcel-like extent, multiple pieces or
an open extraction edge rather than the playing course. Preset v4 instead draws
one Grey 0.40 mm `playing-area-envelope`. This is an explicitly illustrative
symbol derived from the exact 18 sourced hole centrelines and nearby mapped
playing surfaces. It participates in page fitting but is not an official course
boundary, property boundary or survey claim.

Fairways retain only their Green 0.25 mm source outlines. Tees retain only their
Green 0.40 mm source outlines. Greens alone receive an inset, polygon-clipped
Green 0.25 mm fine-line fill inside their Green 0.40 mm source outlines. The
fine pen is inset by 0.16 mm so its full 0.25 mm mark stays inside the sourced
polygon while meeting the heavier outline cleanly. Fill is removed beneath the
Gold route corridor and label clearance; the longest legal interior fallback
ensures every visible sourced green in the 25-course release has a fill mark.
The build fails if any later source green cannot meet that contract. It does
not invent a silhouette where a surface is unmapped. Whole
source playing-surface objects are admitted only when they intersect the exact
18-hole routing's 60 m context, excluding adjacent-course and practice context.
Generic grass/meadow,
whole-course rough, and vegetation polygons remain omitted. Missing mapped
silhouettes remain visibly missing rather than being inferred from a hole
route.

Release QA independently polygonises each source-matched green outline and
buffers every emitted fill centreline by the physical 0.125 mm radius of its
Green 0.25 mm pen. All 514 visible green sources pass that containment gate;
all also retain the 0.68 mm centreline clearance from the Gold 1.00 mm route.

Every visible water source is represented by physical closed Blue 0.25 mm dot
symbols. Area water receives irregular dots in its sourced polygon interior;
line-only waterways receive dots centred directly on the sourced line; and a
physically narrow polygon that cannot contain an interior dot receives dots on
its exact source boundary rather than an invented width or centreline. If exact
boundary dots physically collide on overlapping tiny polygons, the last-resort
symbol is centred inside the exact sourced polygon and explicitly tagged as a
source-anchored point symbol, not a contained area fill. For an
oriented OpenStreetMap coastline, its source direction identifies the
water-right side and drives bounded sea-side dots; the derived mask edge is
never drawn. Sourced area and shoreline outlines remain Blue 0.40 mm where they
are physically drawable. Every dot is a closed loop that clears the three-nib
stroke floor, and label masking removes it whole rather than leaving a crescent.
No continuous line-only waterway, cross-water hatch or invented sea edge is
emitted.

The renderer rotates each course deterministically to use the long axis of the
binding A3 field, fits the complete playing course, hazards, water and
illustrative playing envelope without fitting to the raw selection boundary or
incidental remote context, and rounds the metric scale denominator upward to
the next 100. The map is therefore not north-up; a rotated true-north arrow and
measured scale bar occupy the binding furniture zone below the field.

Each of the 18 red marker disks is solved against nib-expanded mapped strokes,
other marker disks and existing leaders. The black plotter-font number receives
a 0.9 mm paper-white clearance halo. A build fails instead of accepting a
crossed, duplicated or overlapping number, and the independent SVG QA repeats
that geometry check on the final master rather than trusting manifest metadata.

## Build and acceptance

```bash
mapplot-golf build --all \
  --output-dir output/golf-course-series-v4 --dpi 180

find output/golf-course-series-v4 -maxdepth 1 -name '*.svg' \
  ! -name '*.pen-*' -print0 | \
  xargs -0 .venv/bin/python tools/validate_format.py --warnings-as-errors

PYTHONPATH=src .venv/bin/python tools/qa_golf_series.py \
  output/golf-course-series-v4

for plate in output/golf-course-series-v4/*.svg; do
  PYTHONPATH=src .venv/bin/python tools/plotsim.py "$plate" --compare
done

(cd output/golf-course-series-v4 && sha256sum -c CHECKSUMS.sha256)
```

The internal batch gate requires preset `golf-clarity-course-a3-v4`, exactly one
marker for each of 18 sourced hole routes, zero mapped-ink/number overlap, and
disjoint markers and leaders. It also requires clipped fine-line fill on greens
only, outline-only fairways and tees, a closed-dot representation for every
visible water source, no continuous line-only waterway or water hachure, no
emitted raw course boundary, and one provenance-labelled Grey 0.40 mm
illustrative playing-area envelope. At least 97% of the limiting working-field
axis must be used, with complete north/scale furniture, one load per distinct
pen, ink coverage at or below 28%, and a document travel ratio below 1.0. The
format validator remains the binding paper gate, and PlotSim remains the
machine-order acceptance check.

The batch calls itself `review-only-nominal-unmeasured-pens`. Passing geometry,
format and motion QA does not calibrate a real pen. Production status remains
blocked until every selected pen is measured on the intended stock at the
intended machine speed/pressure and the commercial/non-endorsement review is
complete.
