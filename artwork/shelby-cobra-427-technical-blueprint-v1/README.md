# Shelby Cobra 427 technical blueprint v1

This is the complete, portable A3-landscape plotting package for the revised
Shelby Cobra technical plate. The artwork contains the four retained native-SVG
views and the 1965 test-car specifications. No licence, source, creator, pen,
stock, process, or view labels are printed on the plate.

## Use the artwork

Use `artwork/shelby-cobra-427-technical-blueprint.svg` as the plot master. The
PNG beside it is a review preview, not plotting geometry.

- Document: A3 landscape, exactly 420 x 297 mm
- Scale: 100%; disable "fit to page" and all automatic scaling
- Stock: blue paper
- Physical ink: White only
- Layer 1: `white-0-3`, 0.30 mm, car geometry
- Layer 2: `white-0-4`, 0.40 mm, technical copy and rules
- Layer 3: `white-0-5`, 0.50 mm, title and double border
- Layer order: 0.30 mm, then 0.40 mm, then 0.50 mm

The SVG's blue page colour is preview CSS only. It is not a rectangle, fill, or
motor path and must not be plotted.

## Package contents

- `artwork/`: editable plot-ready SVG, its adjacent physical manifest, and the
  high-resolution PNG preview
- `plot/shelby-cobra-427.plot-manifest.json`: page, layer, pen, path, and
  production-readiness record
- `plot/shelby-cobra-427.optimised.plotjob.json`: deterministic, SVG-hash-bound
  simulated plot job
- `plot/shelby-cobra-427.optimised-timeline.json`: complete optimised motion
  timeline
- `plot/axidraw-class-simulation-v1.json`: nominal simulation profile; physical
  execution is deliberately disabled
- `plot/shelby-cobra-427.vector-provenance.json`: zero-raster normalization
  evidence
- `evidence/technical-facts.json`: claim-to-source factual ledger
- `evidence/shelby-cobra-native-source.svg`: retained original native vector
  geometry
- `evidence/source-record.json`: geometry-source identity and hash
- `SOFTWARE_IMPORT.md` and `SOFTWARE_IMPORT.json`: clone, import, viewer,
  software, integrity, and safety entry points
- `simulation/shelby-cobra-427-plotsim.html`: self-contained browser viewer
- `PLOTTER_HANDOFF.md`: machine and import instructions
- `CHECKSUMS.sha256`: hashes for every package file

## Simulate after pulling

Open the complete interactive studio from any directory:

```bash
/path/to/city-map-plotter-artwork-and-simulator/scripts/run_shelby_cobra_studio.sh
```

Or open `simulation/shelby-cobra-427-plotsim.html` directly for a portable,
server-free playback of the same physical motion plan.

The included nominal simulation is 542 strokes, 7,924 vertices, three pen
loads, and approximately 9 minutes 34 seconds. This estimate is not a hardware
calibration.

Do not execute the included plot job directly on hardware. First replace the
simulation profile with the exact machine work area, origin, controller,
measured pen widths, speeds, servo delays, and a timed calibration. See
`PLOTTER_HANDOFF.md`.
