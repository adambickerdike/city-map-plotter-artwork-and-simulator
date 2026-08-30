# Software import — Shelby Cobra 427 technical blueprint v1

This folder is a self-contained A3-landscape artwork and simulation handoff.
The SVG is the plotting master; the PNG is a review preview. The included
machine profile and plot job are simulation-only and deliberately block
physical execution until the receiving machine and exact pens are calibrated.

## Clone and verify

```bash
git lfs install
git clone https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator.git
cd city-map-plotter-artwork-and-simulator
git lfs pull
sha256sum -c artwork/shelby-cobra-427-technical-blueprint-v1/CHECKSUMS.sha256
python3 scripts/verify_repository.py --full
```

## Open the interactive studio

The launcher resolves the repository root itself, so it works from any current
directory:

```bash
/path/to/city-map-plotter-artwork-and-simulator/scripts/run_shelby_cobra_studio.sh
```

The studio opens at <http://127.0.0.1:8042/>. For a server-free review, open
`simulation/shelby-cobra-427-plotsim.html` directly in a browser.

## Import into another plotting application

Import `artwork/shelby-cobra-427-technical-blueprint.svg` at exactly 420 x
297 mm. Do not scale, fit, centre, outline, hatch, or trace it. Plot the three
top-level physical groups in document order:

1. `layer-pen-white-0-3` — White 0.30 mm — car geometry;
2. `layer-pen-white-0-4` — White 0.40 mm — technical copy and rules;
3. `layer-pen-white-0-5` — White 0.50 mm — title and double border.

The blue page colour is preview CSS, not drawable geometry. Full machine and
stock setup requirements are in `PLOTTER_HANDOFF.md`; machine-readable paths,
hashes, dimensions, pen order, and safety state are in `SOFTWARE_IMPORT.json`.
