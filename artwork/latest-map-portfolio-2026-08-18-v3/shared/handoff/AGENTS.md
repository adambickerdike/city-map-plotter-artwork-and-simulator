# Agent instructions — city-map-plotter

Before generating or changing any map, read `CODEX_MAP_HANDOFF.md` and
`docs/reproducibility/REPRODUCING_MAPS.md`. They define the frozen university
recipe, pinned-source workflow, and the files that must never be committed as
finished artwork.

Read this before changing anything that affects what reaches paper.

## The plate format specification is binding

**`docs/format/format-v1.json` is the contract for every generated sheet.**
`docs/format/FORMAT.md` explains it. Six formats, specified separately:

`a5-portrait` · `a5-landscape` · `a4-portrait` · `a4-landscape` · `a3-portrait` · `a3-landscape`

Before you touch page layout, zones, margins, type sizes, nib assignment, border drawing,
or SVG emission — read the spec first and conform to it. Do not invent a new layout constant.

**Never hand-edit `format-v1.json`.** Every value in it is derived. Edit the rules at the top
of `tools/build_format_spec.py` and regenerate:

```bash
python3 tools/build_format_spec.py
```

That keeps all six formats in step. A number typed directly into the JSON will be silently
overwritten on the next regeneration.

## Check your output

```bash
python3 tools/validate_format.py output/*.svg
```

Exit status is 0 only when every rule passes. `tests/test_format_conformance.py` ratchets the
committed examples: their failure counts may go **down**, never up.

## Where the drawing lives

`svg.py` was one file doing five jobs. Non-map drawing now has its own home:

| module | owns |
|---|---|
| `svgkit.py` | neutral emission primitives: number formatting, path data, per-stroke emission, the 3 x nib gate, layer records, physical group attributes |
| `furniture.py` | every mark that is not map linework -- border, frame, title, subtitle, details, legend, scale bar, north mark, attribution -- placed in the **named zones** of the selected plate |
| `textweight.py` | type weight, off the road compiler's own `physical._offset_positions` |
| `svg.py` | the map field, the manifest, and the document assembly |

`furniture.py` is also where the *verifier* lives: `typography_evidence()`
regenerates every piece of lettering from the design contract and compares
digests, and it calls the same `set_text_block()` the emitter does, so a themed
sheet can never be checked against a second implementation of its own layout.

## Design the plate live

```bash
python3 tools/theme_studio.py --theme chromatic-head-rail-v1 \
    --subject uk-university-york --serve --watch
```

Edit the theme file, watch the re-simulated plate. The preview *is*
`tools/build_plotsim_viewer.py` output from the real exporter, so the pen plan
and plot time on screen are the machine's. See `docs/themes/THEMES.md`.

## Course geometry is sourced or it is not drawn

Catalog marathons print `COURSE NOT INCLUDED` because no official route has been
imported. That rule holds. Rowing head courses are drawn because they can be
sourced end to end, not because the rule was relaxed:

* `tools/build_course_geometry.py` takes each start and finish from a **named
  OSM feature** matched to the organiser's published course description, cuts
  the **OSM river centre-line** between them, measures the cut, and refuses
  anything more than 12% from the published distance.
* `src/city_map_plotter/rowing.py` loads that generated file, frames the sheet
  on the course extent, and resolves the plate's `race_course` width into real
  pens — wider than any single colour nib, so it comes out as parallel offsets
  of the 0.40, never an invented mark.
* Both the published distance and the measured centre-line reach the manifest,
  and the claim scope says in words that the line is the centre-line and not the
  raced line.

If you add a course, add its sources with it. A red line on a sheet is a factual
claim about the world, and the only defence is that it can be checked.

## A crew plate names real people

`crew.py` validates the list before it is set: every seat that class has is
filled, nobody is named twice, and a rig is described in terms the boat
supports. The layout rules:

- **Head**: race name, then club/event/category, then city/river/coordinates,
  all on one left edge. The compass has a column to itself so nothing dodges it.
- **The crew runs top to bottom in boat order**, cox to bow, one line per seat,
  with the result beside it -- for a head, time and position out of the field;
  for a regatta, who was beaten and by how much.
- **A crew list shrinks together or not at all**, and the line spacing comes
  from the band's capacity rather than from this crew, so a four and an eight
  are set on the same rhythm.
- **No boat is drawn.** A plan of a racing shell is a poor picture at poster
  scale. This was tried and removed -- do not put it back.

The crew composition (`crew_zones_mm`) is a *different stack*, not a modified
one: `with_crew_zones()` swaps the layout onto it before anything is projected,
and the extent must be cropped to `crew_map_field_aspect` or the map ends up
narrower than its band and out of line with the title.

## A white halo has to be an absence of ink

Course labels -- bridges, mile marks, `MIDDLESEX`/`SURREY` -- are legible
because the map linework under them is **cut away**, not because anything white
is drawn. A plotter cannot draw white on white paper, so `knock_out_labels()`
subtracts the label boxes from the compiled strokes before the manifest, the
path counts and the ink are measured, and every one of those then describes what
is actually drawn.

Two limits are load-bearing, both learned by breaking them:

- **A cut may never remove a source way entirely.** The completeness audit
  correctly reads a vanished way as missing geometry, so a stroke the halo would
  consume whole is kept whole and counted.
- **Each fragment is gated on its own nib**, not on the label's. A piece that
  clears the floor for a 0.25 pen is still a dot under a 0.40 one.

Labels that cannot find clear paper -- off the course, off other labels, inside
the field -- are dropped whole and counted, never printed on top of something.

## Simulate before you plot
