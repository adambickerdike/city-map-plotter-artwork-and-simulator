# Plotting software import

1. Clone the artwork repository with Git LFS and run `git lfs pull`.
2. Run `python scripts/verify_production_maps.py --full`.
3. Open `artwork/production-maps-2026-09-06/index.html` and choose the master SVG.
4. Launch `python tools/plotter_studio.py path/to/map.svg --machine-profile plotter-profiles/axidraw-class-simulation-v1.json`, or drag the SVG into the running studio.
5. The corresponding `.plotjob.json` can be inspected with `python tools/plotter_control.py inspect path/to/map.plotjob.json`.

Use the master or its ordered pen splits, not a thumbnail or contact sheet. The bundled jobs are ready for software import and simulation; compile for your calibrated device profile before physical execution.
