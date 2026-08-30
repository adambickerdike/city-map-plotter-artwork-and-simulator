# Plotter handoff

## Master and geometry

Import `artwork/shelby-cobra-427-technical-blueprint.svg` without scaling. Its
width and height are expressed in millimetres and its viewBox is `0 0 420 297`.
The SHA-256 of the master is
`464200f14c563045eb5e671b1ec11f5d86fd9f72ba9041d5fa892ff5abefc8f7`.

The document contains paths only. There are no SVG text objects, raster images,
or drawable blue-background objects. All visible lettering has already been
converted to editable stroke paths.

## Pen groups

Plot the three top-level SVG groups in document order:

| SVG group | Physical tool | Purpose | Paths |
|---|---:|---|---:|
| `layer-pen-white-0-3` | White 0.30 mm | Native car linework | 166 |
| `layer-pen-white-0-4` | White 0.40 mm | Technical copy and rules | 355 |
| `layer-pen-white-0-5` | White 0.50 mm | Title and frame | 21 |

Use one pass per path. Do not outline, hatch, expand strokes, merge separate
paths, infer missing contours, or convert the PNG back into vectors.

## Page placement

- Page: 420 x 297 mm, landscape
- Safe border begins 12 mm inside the page
- Plot at 100% physical scale
- Disable driver-level fit, crop, centring, and stroke-width substitution
- Confirm that the machine can accommodate the complete A3 page before loading
  a job
- Establish and verify work coordinates on scrap stock before using the final
  blue sheet

The SVG uses normal screen coordinates with the origin at the page's upper
left. The hardware adapter must perform any required axis or origin conversion;
do not rewrite the artwork coordinates casually.

## Simulation record

The bundled optimised job is bound to the master SVG hash and nominal
`axidraw-class-simulation-v1` profile:

- Job SHA-256: `6589d6d7b2c94e68cf5e3c1cd69f3af15e23fb9487c6fb49b9bcdc65397b9a86`
- 542 strokes
- 7,924 vertices
- 13.96 m pen-down motion
- 4.08 m pen-up travel
- 542 lifts
- Nominal estimate: 9:34
- Uncalibrated range: 8:08-11:00

The profile sets `execution_enabled` to false. Its work area and timing are
nominal, not measurements of the receiving machine.

## Repository simulator

From any current directory, run the location-independent launcher:

```bash
/path/to/city-map-plotter-artwork-and-simulator/scripts/run_shelby_cobra_studio.sh
```

For a server-free review, open
`simulation/shelby-cobra-427-plotsim.html` directly in a browser. Both use the
same physical motion engine and nominal simulation profile as the bundled
SHA-bound plot job.

## Physical release gate

Before hardware execution, create a machine-specific profile and record:

1. measured A3 work area and verified orientation;
2. work origin and coordinate system;
3. effective widths of the exact White 0.30, 0.40, and 0.50 mm pens on the
   intended blue stock;
4. pen-up/down servo commands and delays;
5. measured drawing and travel speeds;
6. a timed calibration plot;
7. a dry-run bounds proof with the pen raised.

The bundled manifest correctly reports the current artifact as review-only.
That safety block is intentional.

## Integrity checks

From this package directory:

```bash
sha256sum -c CHECKSUMS.sha256
```

The SVG passed the A3 format contract, strict vector-only preflight, solid-line
audit, and the complete 50-car collection audit. The original native SVG and
the factual evidence stream are retained separately under `evidence/`.
