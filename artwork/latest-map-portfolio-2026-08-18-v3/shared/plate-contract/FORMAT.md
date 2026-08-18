# Plate Format System v1

**Authoritative file: `format-v1.json`.** This document explains it. Where the two disagree,
the JSON wins — it is what `tools/validate_format.py` reads.

Regenerate with `python3 tools/build_format_spec.py`. **Never hand-edit `format-v1.json`;**
edit the rules at the top of the generator so all six formats stay in step.

## What a "plate" is

A plate is a sheet, a border, a type scale, a nib ladder, and a field. Nothing in the
specification mentions maps. A city map is the first thing that fills the field; a car, an
aircraft three-view or a marathon elevation profile fills the same rectangle with the same
title block, the same pens, and the same validation.

Six formats, specified separately because portrait and landscape are genuinely different
compositions — not the same layout rotated:

`a5-portrait` · `a5-landscape` · `a4-portrait` · `a4-landscape` · `a3-portrait` · `a3-landscape`

## The two archetypes

**`stack` (portrait).** Title, subtitle, field, furniture, details, attribution — top to
bottom. The field takes everything left over.

**`rail` (landscape).** The field fills the sheet; all information stacks in a vertical rail
down the right-hand side, 26.5% of the content width. A bottom band on a wide sheet wastes a
disproportionate amount of paper and squashes the map into a letterbox, which is why
landscape does not reuse the portrait stack.

## The derivation rules

Every number is derived. The rules, in full:

| quantity | rule | A5 | A4 | A3 |
|---|---|---|---|---|
| Safe margin | `clamp(0.0405 × short_edge, 6, 12)`, snapped to 0.5 | 6.0 | 8.5 | 12.0 |
| Content inset | `2 × safe` | 12.0 | 17.0 | 24.0 |
| Gap | `0.5 × safe` | 3.0 | 4.25 | 6.0 |
| Title cap height | `0.0473 × short_edge` | 7.00 | 9.93 | 14.05 |

The title ratio was chosen so **A5 reproduces the 7.0 mm title the existing hand-tuned poster
already used**; A4 and A3 then follow the A-series 1:√2 progression automatically. The
detail role retains that poster's 2.35 mm scale. The other small-text roles are raised just
enough to clear the legibility floor of the real 0.25 and 0.40 mm studio pens.

### Type scale — multiples of the title cap

| role | ×title | A5 | A4 | A3 | nib role |
|---|---|---|---|---|---|
| title | 1.000 | 7.00 | 9.93 | 14.05 | heavy |
| detail | 0.336 | 2.35 | 3.34 | 4.72 | text |
| subtitle | 0.323 | 2.26 | 3.21 | 4.54 | text |
| legend | 0.323 | 2.26 | 3.21 | 4.54 | text |
| attribution | 0.286 | 2.00 | 2.84 | 4.02 | hairline |

### Nib ladders

The ladder is the real studio stock: 0.25 and 0.40 mm in the general colour set, 0.60 and
1.00 mm in black, 0.70 and 1.00 mm in white, and 1.00 mm in gold and silver. The table below
selects a physically available role for each sheet. Ink-specific availability remains a
second constraint: a numeric width on this ladder does not imply that every colour owns it.

| role | A5 | A4 | A3 | used for |
|---|---|---|---|---|
| hairline | 0.25 | 0.25 | 0.40 | attribution, graticule, fine hatch |
| text | 0.25 | 0.40 | 0.40 | detail, legend, subtitle |
| primary | 0.40 | 0.60 | 0.60 | secondary roads, frame, furniture |
| heavy | 0.60 | 1.00 | 1.00 | major roads, border, title |

## The three binding rules

**1. Minimum cap height = 8 × nib.** Below this a stroke font closes up and reads as a blot.
This is why A5 attribution is 2.00 mm for the smallest real 0.25 mm pen. All 30
role/format combinations clear the floor as specified.

**2. Minimum stroke length = 3 × nib.** Anything shorter plots as a dot. This is the gate
that would have prevented the 5,915-stroke green-space confetti found in the audit.

**3. Separate title lines leave one title nib of white paper.** The compositor buffers
each serialized title-line centreline by half the actual title nib, then requires a further
full nib between those two ink envelopes. Thus the minimum raw path-bounds gap is `2 × nib`.
The title zone itself must contain both minimum-cap lines and their outer half-nib envelopes:
`2 × minimum title cap + 3 × title nib`. This raises the A4 title band to 19.0 mm; A5 and A3
already had enough height. Every title path carries an explicit block ID, line index, and
line count so package QA compares different lines without mistaking joins within one glyph
or line for collisions. Horizontally, title centrelines are inset by half the title nib on
each side (`horizontal_ink_inset_mm`), so round-capped edge strokes remain inside the zone.

## Borders

Five styles, `double` is the default.

| style | geometry |
|---|---|
| `none` | no border |
| `hairline` | one rectangle on the safe margin, hairline nib |
| `double` | that rectangle plus an inner one offset by `0.25 × safe`, heavy nib |
| `rule` | horizontal rules above and below the content only |
| `corner` | corner ticks only — registration marks, no enclosing box |

## Resolved zones

All values millimetres, origin top-left, y increasing downward (SVG convention).

**a5-portrait** — stack, 148×210, safe 6, field aspect 0.962

| zone | x | y | width | height |
|---|---|---|---|---|
| title | 12 | 12 | 124 | 12.6 |
| subtitle | 12 | 27.6 | 124 | 4.522 |
| map_field | 12 | 35.122 | 124 | 128.924 |
| furniture | 12 | 167.046 | 124 | 5.426 |
| detail | 12 | 175.473 | 124 | 15.523 |
| attribution | 12 | 193.996 | 124 | 4.004 |

**a5-landscape** — rail, 210×148, safe 6, field aspect 1.078

| zone | x | y | width | height |
|---|---|---|---|---|
| map_field | 12 | 12 | 133.71 | 124 |
| title | 148.71 | 12 | 49.29 | 12.6 |
| subtitle | 148.71 | 27.6 | 49.29 | 4.522 |
| detail | 148.71 | 35.122 | 49.29 | 15.523 |
| furniture | 148.71 | 53.645 | 49.29 | 5.426 |
| attribution | 148.71 | 131.996 | 49.29 | 4.004 |

**a4-portrait** — stack, 210×297, safe 8.5, field aspect 0.967

| zone | x | y | width | height |
|---|---|---|---|---|
| title | 17 | 17 | 176 | 19.000 |
| subtitle | 17 | 40.250 | 176 | 6.416 |
| map_field | 17 | 50.916 | 176 | 180.929 |
| furniture | 17 | 236.095 | 176 | 7.699 |
| detail | 17 | 248.044 | 176 | 22.024 |
| attribution | 17 | 274.318 | 176 | 5.682 |

**a4-landscape** — rail, 297×210, safe 8.5, field aspect 1.074

| zone | x | y | width | height |
|---|---|---|---|---|
| map_field | 17 | 17 | 189.055 | 176 |
| title | 210.305 | 17 | 69.695 | 19.000 |
| subtitle | 210.305 | 40.250 | 69.695 | 6.416 |
| detail | 210.305 | 50.916 | 69.695 | 22.024 |
| furniture | 210.305 | 77.190 | 69.695 | 7.699 |
| attribution | 210.305 | 187.318 | 69.695 | 5.682 |

**a3-portrait** — stack, 297×420, safe 12, field aspect 0.967

| zone | x | y | width | height |
|---|---|---|---|---|
| title | 24 | 24 | 249 | 25.286 |
| subtitle | 24 | 55.286 | 249 | 9.076 |
| map_field | 24 | 70.362 | 249 | 257.558 |
| furniture | 24 | 333.921 | 249 | 10.891 |
| detail | 24 | 350.812 | 249 | 31.152 |
| attribution | 24 | 387.964 | 249 | 8.036 |

**a3-landscape** — rail, 420×297, safe 12, field aspect 1.074

| zone | x | y | width | height |
|---|---|---|---|---|
| map_field | 24 | 24 | 267.42 | 249 |
| title | 297.42 | 24 | 98.58 | 25.286 |
| subtitle | 297.42 | 55.286 | 98.58 | 9.076 |
| detail | 297.42 | 70.362 | 98.58 | 31.152 |
| furniture | 297.42 | 107.514 | 98.58 | 10.891 |
| attribution | 297.42 | 264.964 | 98.58 | 8.036 |

## Bridge subcomposition and pen roles

Every format also publishes `bridge_zones_mm`, an optional three-band
subcomposition derived inside `map_field`. The map field is inset by one `gap`;
the top and bottom label bands are `attribution cap + gap` high; and
`bridge_drawing` receives one further `gap` inset on each horizontal side. No
bridge renderer may replace these with private padding constants. The bridge
compiler currently selects A3 landscape, but all six records are generated so
a future paper-size option cannot create a second layout system.

For the binding A3 landscape bridge plate:

| bridge zone | x | y | width | height |
|---|---:|---:|---:|---:|
| `bridge_field_label` | 30.000 | 30.000 | 255.420 | 10.018 |
| `bridge_drawing` | 36.000 | 40.018 | 243.420 | 216.964 |
| `bridge_dimension_label` | 30.000 | 256.982 | 255.420 | 10.018 |

Bridge-domain manifests merge these three rectangles into their named zone
record. `validate_format.py` requires all three to match the selected format;
their presence is optional for non-bridge domains.

`bridge_pen_roles` is likewise generated for every format. The fine structural
end stays on the real 0.25/0.40 mm inventory; copy, primary and frame resolve
through the selected plate where shown:

| role | ink | A5 | A4 | A3 |
|---|---|---:|---:|---:|
| construction | Grey | 0.25 | 0.25 | 0.25 |
| context | Blue | 0.25 | 0.25 | 0.25 |
| dimension | Red | 0.25 | 0.25 | 0.25 |
| fine | Black | 0.25 | 0.25 | 0.25 |
| secondary | Black | 0.40 | 0.40 | 0.40 |
| copy | Black | 0.25 | 0.40 | 0.40 |
| primary | Black | 0.40 | 0.60 | 0.60 |
| frame | Black | 0.40 | 0.60 | 0.60 |

The ordinary title, border, subtitle, details and attribution continue to use
`nib_roles_mm`; only the field-frame mark uses the bridge `frame` role. Source-
profile geometry has an additional fail-closed consequence of the universal
`3 × nib` rule: every qualified B0 path must survive the resolved role at paper
scale, or the plate is rejected rather than emitted with missing detail.

## Circuit information subcomposition

Every format publishes `circuit_zones_mm`, a four-card factual composition for
circuit studies. It changes only the information area; the title, subtitle, map
field and attribution remain the ordinary plate zones. Landscape sheets divide
the right rail between the detail-zone top and the attribution gap into four
equal cards. Portrait sheets use a two-by-two grid spanning the existing
furniture and detail bands. Both arrangements use the plate's generated `gap`;
the renderer owns no private page padding.

The four binding zones are `circuit_course`, `circuit_history`,
`circuit_record`, and `circuit_drawing`. Each begins with a Black separator at
the selected text nib and contains a small label plus at most two value lines.
Copy uses the selected format's `detail` cap and text nib, so every glyph clears
the universal `8 × nib` cap-height floor. Separator segments are gated against
the universal `3 × nib` minimum-stroke rule. Circuit-domain manifests merge
all four rectangles into `page.zones_mm`, and `validate_format.py` compares them
to this generated contract.

## Technical-object subcomposition and pen roles

Every format publishes `technical_zones_mm`. They are derived by insetting
`map_field` by one plate `gap`, then resolving reusable full, top/bottom and
left/right panels from that rectangle. Hero and minimal compositions use
`technical_field`; multi-view, detail and evolution compositions choose among
the remaining named panels. Technical renderers may not introduce a second set
of private page padding constants.

For the normal A3 landscape technical plate:

| technical zone | x | y | width | height |
|---|---:|---:|---:|---:|
| `technical_field` | 30.000 | 30.000 | 255.420 | 237.000 |
| `technical_top` | 30.000 | 30.000 | 255.420 | 115.500 |
| `technical_bottom_left` | 30.000 | 151.500 | 124.710 | 115.500 |
| `technical_bottom_right` | 160.710 | 151.500 | 124.710 | 115.500 |
| `technical_left` | 30.000 | 30.000 | 124.710 | 237.000 |
| `technical_right_top` | 160.710 | 30.000 | 124.710 | 115.500 |
| `technical_right_bottom` | 160.710 | 151.500 | 124.710 | 115.500 |

`technical_pen_roles` binds illustration semantics to pens in the existing
inventory. At A3 the principal silhouette is Black 0.60 mm; structural edges,
copy and accents use 0.40 mm roles; fine detail, openings, verified internals,
hatching, construction, dimensions and context use real 0.25 mm colour roles.
All thirteen mappings are generated for every sheet. The technical compiler
then applies the universal three-nib path floor per resolved pen and rejects an
omitted identity-bearing path.

For `technical-objects` manifests, `validate_format.py` requires the full
technical zone set to match the selected plate. The manifest also records the
semantic pen mapping that produced each layer; no colour is required by a
composition name such as blueprint.

## Paper policy and the ink budget

`ink_budget.max_coverage` = 0.28 of the map field, coverage being
`Σ(stroke_length × nib) / field_area`. Above about a third the gaps between adjacent
lines fall below the nib width and the sheet reads as grey wash instead of a hierarchy.

The validator clips every parsed subpath to the **exact, closed map-field rectangle** before
measuring it. A line crossing the field contributes only its in-field portion; furniture
wholly outside contributes nothing. There are no semantic exemptions for ink physically in
the field: a frame segment on the field boundary and a north mark drawn inside it both count.

| sheet | field mm | area mm² | ink budget mm² |
|---|---|---|---|
| A5 portrait | 124 × 129 | 15,986 | 4,476 |
| A4 portrait | 176 × 182 | 32,038 | 8,971 |
| A3 portrait | 249 × 258 | 64,132 | 17,957 |

**Subject policy.** Maps: A5, A4 or A3, A4 preferred. Object schematics use
A5, A4 or A3 with a binding physical LOD ladder; A3 is preferred, while A5
keeps every identity view and omits only declared sub-size detail. Curated
single-route hiking plates may use A5 when their geographic hierarchy remains
legible at physical size. The 28% figure is reported as an advisory density reference, not a gate;
content is selected for the composition rather than removed to satisfy that
number. Full all-roads or dense topographic detail remains an A3 design.

### Two nib tables, deliberately different

`nib_roles_mm` drives type and furniture and **scales with the sheet**, because cap
heights grow and the 8 × nib floor would otherwise leave large lettering spidery.

`map_linework_nib_mm` drives map linework and **holds its fine end fixed**:

| role | A5 | A4 | A3 |
|---|---|---|---|
| hairline | 0.25 | 0.25 | 0.25 |
| text | 0.25 | 0.25 | 0.40 |
| primary | 0.40 | 0.60 | 0.60 |
| heavy | 0.60 | 1.00 | 1.00 |

Measured on the York all-roads plate: **89% of ink length is in the two finest
roles** (hairline 55%, text 34%). Scaling those with the paper cancels the area gain
— A3 with proportionally scaled nibs was 33.4% coverage, no better than A5. Holding
the fine end and scaling only primary/heavy takes the same plate to 27.2%.

Colour availability is a second constraint: only 0.25 and 0.40 exist in the general
colour set. A blue river asked for 0.60 on A3 is realised as two 0.40 offsets at
0.20 mm pitch — `pens.fit_pen_width` does this automatically.

`styles.MAP_LINEWORK_NIB_ROLES` binds each map layer to one of these four roles;
the width behind the role always comes from the plate being drawn. The same
composition on A4 therefore gets A4 pens without a second style table:

| layer | role | A5 | A4 |
|---|---|---|---|
| `roads_major` | heavy | 0.60 | 1.00 |
| `roads_secondary`, `water_areas`, `rivers` | primary | 0.40 | 0.60 |
| `roads_local` | text | 0.25 | 0.25 |
| everything else | hairline | 0.25 | 0.25 |

### Landmark buildings

Buildings are a capped, named-object feature, not a bulk layer, so each format
carries its own `landmark_buildings` block with two independent levers:

| key | lever | A5 | A4 | A3 |
|---|---|---|---|---|
| `nib_role` | weight, into `map_linework_nib_mm` | hairline (0.25) | text (0.25) | text (0.40) |
| `max_objects` | inclusion | 12 | 24 | 32 |
| `ink_budget_field_fraction` | inclusion | 0.004 | 0.008 | 0.012 |
| `minimum_area_scale` | inclusion | 1.5 | 1.0 | 0.75 |

A5's map field is roughly half A4's, so the object count and outline budget that
read as a legible set of landmarks on A4 silt up the smaller sheet. The role
quotas in `cartography.LANDMARK_ROLE_BUCKETS` are rescaled to `max_objects` in
proportion, and every role keeps at least one slot so a city never loses its
cathedral to arithmetic.

## Optional column splits

Everything above and below the map field is a full-width band. A theme that
wants its details *beside* the title rather than under the map needs a narrower
column, and it may not invent one -- the only zones anything may draw in are the
ones the generator derives. So each format also publishes `split_zones_mm`:

| zone | derivation |
|---|---|
| `head_main` / `head_rail` | the title+subtitle band, split at `RAIL_FRACTION` (26.5%) with one `gap` between |
| `foot_main` / `foot_rail` | the furniture+detail band, split the same way |

**a5-portrait**

| zone | x | y | width | height |
|---|---|---|---|---|
| head_main | 12 | 12 | 88.14 | 20.122 |
| head_rail | 103.14 | 12 | 32.86 | 20.122 |
| foot_main | 12 | 167.046 | 88.14 | 23.95 |
| foot_rail | 103.14 | 167.046 | 32.86 | 23.95 |

They are deliberately **not** in `zones_mm`: they are not part of the default
composition, a manifest is not required to carry them, and they never move
`map_field`, so a rearranged plate crops exactly the same city. A theme opts in
by naming one.

## Framing

`map_field_aspect` is the contract for extent selection. Portrait fields are ~0.96 and
landscape ~1.075 — both close to square, deliberately, so a city crop does not have to be
distorted much to fit.

The compiler must crop or expand the bounding box to that aspect before projecting;
`geometry.crop_bbox_to_aspect` and `expand_bbox_to_aspect` already exist for this. Use
**cover** (crop) for a named subject where the centre matters, and **contain** (expand) only
when the user gave an explicit bbox that must be shown in full. Letterboxing inside the field
is a conformance failure — it leaves dead paper the customer paid for.

## Validation

```bash
python3 tools/validate_format.py output/york.svg
python3 tools/validate_format.py examples/*.svg --quiet
python3 tools/validate_format.py output/*.svg --warnings-as-errors   # CI
```

Exit status is 0 only when every rule passes. Checked:

| # | check |
|---|---|
| 1 | Page size and viewBox match a known format exactly; 1 user unit = 1 mm |
| 2 | Every zone in the manifest matches the spec within 0.5 mm |
| 3 | Every layer nib is on that sheet's ladder |
| 4 | Every manifest layer carries a machine-readable `nib_mm` |
| 5 | No stroke shorter than `3 × nib` |
| 6 | No geometry outside the plotter-safe area |
| 7 | No empty layers |
| 8 | Document order requires no pen to be loaded twice |
| 9 | Path data is absolute `M`/`L`/`C`/`Z` only |
| 10 | Inkscape scaffolding present (`sodipodi:namedview`, mm document units) — warning |
| 11 | Pen sequence reports pen-up travel — warning |
| 12 | In-field ink coverage measured and reported — **advisory only**, never a pass/fail |

Current state of the shipped examples, for reference:

```
examples/sample-a3.svg           FAIL  — nibs off-ladder, 2 empty layers, 54 strokes
                                        outside the 12 mm safe area, no zones in manifest
examples/york-a5-clean-poster    FAIL  — 3 empty layers, 17 sub-nib strokes,
                                        9 pen loads for 6 pens, zones missing
```

Both are pre-specification output, so this is expected. They are the regression baseline.

## Integration

The spec is deliberately outside `src/`, so it can be adopted incrementally:

1. **Now** — run the validator on generated output; treat failures as a to-do list.
2. **Next** — have `make_layout` read `format-v1.json` and return its `zones` for all six
   formats, replacing `make_a5_clean_poster_layout`'s hardcoded `Rect(12, 28, 124, 124)`
   and the A5-only paper restriction.
3. **Then** — wire the validator into the test suite as a golden check, so no generation
   can regress off-format.
