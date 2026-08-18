# Rowing heads — A5 course plates

Four head-race plates on the university-memorabilia composition: a proportional
Hershey Serif race title at top-left, coordinates and a header compass at
top-right, the course and the water it is rowed on in the map field, and a
four-cell fact footer in place of the writable graduate fields.

| plate | course | published distance | measured centre-line | scale |
|---|---|---|---|---|
| `horr-london` | Mortlake to Putney, River Thames | 4 miles 374 yds (6,779 m) | 6,962 m (+2.7%) | 1:41,248 |
| `pairs-head-london` | Chiswick Bridge to Harrods Wall, River Thames | approx 4.5 km | 4,620 m (+2.7%) | 1:31,258 |
| `henley-royal` | Temple Island to Poplar Point, River Thames | 1 mile 550 yds (2,112 m) | 1,978 m (−6.3%) | 1:16,765 |
| `head-of-the-charles` | BU Boathouse to Herter Park, Charles River | 3 miles (4,800 m) | 4,791 m (+0.2%) | 1:25,921 |

The measured column is the OSM river centre-line between the two named
endpoints. The published figure is what is printed on the plate.

## Where the course line comes from

Nothing here is traced by eye. `tools/build_course_geometry.py` derives every
course from OpenStreetMap and both halves are sourced:

* the **start and finish** are named OSM features — Chiswick Bridge, Putney
  Bridge, Harrods Wharf, Temple Island, Phyllis Court, the DeWolfe Boathouse,
  Herter Park — chosen to match the organiser's own published course
  description, which is cited per course in `rowing-courses-v1.json`;
* the **line between them** is the OSM `waterway=river` centre-line, merged into
  one run and cut at the projection of each endpoint.

The generator then measures what it cut and refuses anything more than 12% from
the published distance, because that means the wrong reach was taken rather than
honest river drift. Both figures travel into each `.plot.json` manifest under
`race_course`.

**What the red line is, and is not.** It is the river centre-line between two
named places. It is not a survey of the raced line: a head crew rows the stream,
not the middle, and on the Tideway the stream moves with the ebb. The distance
printed on the plate is the organiser's published figure, not the measured one.

## Sheet size

A5, the keepsake size, matching the university memorabilia series. The same four
races are rendered at A3 in [`../rowing-heads-a3/`](../rowing-heads-a3/) for a
wall print, where a 6.8 km course has more room and the street fabric around the
river is legible rather than suggested.

**Ink coverage plays no part in that choice.** It is measured and reported as an
advisory on every plate and never gates anything — see the "Ink budget —
advisory only" section of `AGENTS.md`. Every plate passes the format contract:

```
horr-london          PASS   (6,053 checks)   coverage 46.3% (advisory)
pairs-head-london    PASS   (4,247 checks)   coverage 38.2% (advisory)
henley-royal         PASS   (1,033 checks)
head-of-the-charles  PASS   (4,607 checks)   coverage 52.8% (advisory)
```

## Reproducing one plate

```bash
mapplot export \
  --rowing-course horr-london \
  --course-margin 0.15 \
  --preset a5-balanced-poster \
  --poster-layout rowing-course \
  --layers roads,water,railways,parks,buildings \
  --style styles/rowing-course-v1.json \
  --landmark-buildings \
  --water-fill dots \
  --detail-profile plotter-faithful \
  --simplify-mm 0.04 \
  --road-style centreline \
  --no-scale-bar \
  --optimise \
  --split-by-pen \
  --attribution-mode external \
  --external-attribution-placement "Product page, packaging, or caption adjacent to each artwork" \
  --output review-output/rowing-heads-a5/horr-london.svg

python3 tools/validate_format.py review-output/rowing-heads-a5/*.svg
python3 tools/build_contact_sheet.py \
  review-output/rowing-heads-a5/horr-london.png \
  review-output/rowing-heads-a5/pairs-head-london.png \
  review-output/rowing-heads-a5/henley-royal.png \
  review-output/rowing-heads-a5/head-of-the-charles.png \
  --out review-output/rowing-heads-a5/series-contact-sheet.png --columns 2
```

`--rowing-course` frames the sheet on the course's own extent, so the whole race
fits with air around it rather than being cropped by a city radius. Swap the
preset for `a3-balanced-poster` to get the wall print instead.

## Pens

The course is drawn in Red at the plate's `race_course` width — 0.80 mm on A5 —
which is wider than any single general-colour nib, so it is built the way the
road compiler builds a wide road: parallel offsets of the real 0.40 mm pen. That
makes the course the boldest mark on the sheet by weight as well as by colour,
and `styles/rowing-course-v1.json` keeps every road grey or black so nothing
competes with it.

Per-pen plates are written alongside each master as `*.pen-NN-*.svg`, one file
per pen load, in the order the manifest's pen sequence lists them.

## Before plotting

These are review artifacts. The pen inventory is nominal, so every plate is
labelled review-only: measure the pens on the intended stock, supply a measured
inventory and the exact pen-down speed, and clear the physical-conflict report
before sending anything to the machine.
