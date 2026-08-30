# Porsche 911 2.0 Targa technical blueprint v1

This is the complete portable A3-landscape plotting package for the 1967
Porsche 911 2.0 Targa technical plate. Its car geometry comes from the retained
native SVG identified by the publisher as a 1967 Porsche 911 Targa. No patent
raster, bitmap trace, generated contour, or hand-drawn vehicle geometry is
used.

Every emitted car stroke is solid. The artwork contains no dashed or dotted
stroke styling. The visible plate prints only the vehicle title and technical
data; source, creator, licence, pen, process, and view records remain in the
package evidence and SVG metadata.

## Use the artwork

Use `artwork/porsche-911-2-0-targa-technical-blueprint.svg` as the plot master.
The PNG beside it is a review preview, not plotting geometry.

- Document: A3 landscape, exactly 420 x 297 mm
- Scale: 100%; disable "fit to page" and all automatic scaling
- Stock: blue paper
- Physical ink: White only
- Layer 1: `white-0-3`, 0.30 mm, native car geometry
- Layer 2: `white-0-4`, 0.40 mm, technical copy and rules
- Layer 3: `white-0-5`, 0.50 mm, title and double border
- Layer order: 0.30 mm, then 0.40 mm, then 0.50 mm

The SVG's blue page colour is preview CSS only. It is not a rectangle, fill,
or motor path and must not be plotted.

## Package contents

- `artwork/`: editable plot-ready SVG, adjacent physical manifest, and PNG
- `plot/porsche-911-2-0-targa.plot-manifest.json`: page, layer, pen, path, and
  production-readiness record
- `plot/porsche-911-2-0-targa.optimised.plotjob.json`: deterministic,
  SVG-hash-bound simulated plot job
- `plot/porsche-911-2-0-targa.optimised-timeline.json`: complete optimised
  motion timeline
- `plot/axidraw-class-simulation-v1.json`: nominal non-executable simulation
  profile
- `plot/porsche-911-2-0-targa.vector-provenance.json`: zero-raster vector
  normalization evidence
- `evidence/technical-facts.json`: claim-to-source factual ledger
- `evidence/porsche-911-targa-native-source.svg`: retained original native SVG
- `evidence/source-record.json`: geometry source identity, licence, and hash
- `SOFTWARE_IMPORT.md` and `SOFTWARE_IMPORT.json`: clone, import, viewer,
  software, integrity, and safety entry points
- `simulation/porsche-911-2-0-targa-plotsim.html`: self-contained browser
  viewer
- `PLOTTER_HANDOFF.md`: machine and import instructions
- `CHECKSUMS.sha256`: hashes for every package file

## Simulation record

Open the complete interactive studio from any directory:

```bash
/path/to/city-map-plotter-artwork-and-simulator/scripts/run_porsche_911_targa_studio.sh
```

Or open `simulation/porsche-911-2-0-targa-plotsim.html` directly for portable,
server-free playback of the same physical motion plan.

The included nominal simulation is 478 strokes, 4,251 vertices, three pen
loads, and approximately 8 minutes 15 seconds. This estimate is not a hardware
calibration.

Do not execute the included plot job directly on hardware. First replace the
simulation profile with the exact machine work area, origin, controller,
measured pen widths, speeds, servo delays, and a timed calibration. See
`PLOTTER_HANDOFF.md`.
