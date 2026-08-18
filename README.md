# City Map Plotter — artwork portfolio and physical simulator

This private review repository contains the complete verified map portfolio
assembled on 18 August 2026 and the software used to simulate and compile
physical pen-plotter motion.

It is deliberately a handoff and review package, not a declaration that every
map is cleared for sale or that any bundled profile is safe for hardware.

## Included artwork

The portfolio contains 423 distinct plates. Every plate has an editable SVG,
a PNG preview, and a plot manifest. All 22 contact sheets are supplied in both
SVG and PNG.

| Family | Plates |
|---|---:|
| UK university cities | 30 |
| US university cities | 20 |
| Hiking maps | 80 |
| Verified full-course marathons | 14 |
| Sourced rowing races, A5 and A3 | 8 |
| Formula 1 courses | 246 |
| Golf courses | 25 |
| **Total** | **423** |

The exact release is under
[`artwork/latest-map-portfolio-2026-08-18-v3/`](artwork/latest-map-portfolio-2026-08-18-v3/).
Open its [`index.html`](artwork/latest-map-portfolio-2026-08-18-v3/index.html)
for the visual gallery and its
[`catalog.json`](artwork/latest-map-portfolio-2026-08-18-v3/catalog.json) for
the machine-readable inventory.

The repository has 447 PNG files and 455 SVG files in total. That includes the
complete portfolio, all contact sheets, and the Augusta simulator example with
its pen-separated SVG jobs.

## Clone the complete files

PNG, SVG and plot-manifest assets are stored with Git LFS. The 122 MiB F1
source catalog and 61 MiB hiking release contract are also tracked explicitly.
Install Git LFS before cloning or pulling so the working tree contains the real
files rather than pointer files.

```bash
git lfs install
git clone https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator.git
cd city-map-plotter-artwork-and-simulator
git lfs pull
python3 scripts/verify_repository.py --full
```

GitHub source archives do not include LFS objects by default. Clone with Git
and run `git lfs pull` for a complete copy.

## Set up the simulator

The motion engine and browser studio use the Python standard library. The test
suite needs `jsonschema` and `pytest`; physical GRBL serial execution also
needs `pyserial`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
```

Run the bundled Augusta example from the repository root:

```bash
.venv/bin/python tools/plotter_studio.py \
  examples/augusta-national/augusta-national.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json
```

Or use the location-independent helper from any current directory:

```bash
/path/to/city-map-plotter-artwork-and-simulator/scripts/run_augusta_studio.sh
```

The studio opens at <http://127.0.0.1:8042/>. It supports SVG loading and drop,
strict preflight, real-time or 4–1024× playback, scrubbing, pen-up travel,
strokes, lifts, swaps, physical nib widths, ordering choices, and timing
ranges.

A portable read-only viewer is included at
[`examples/generated-viewers/augusta-national.html`](examples/generated-viewers/augusta-national.html).

## Motion software

All operator-facing tools use the same deterministic physical motion planner:

| File | Responsibility |
|---|---|
| [`tools/plotter_studio.py`](tools/plotter_studio.py) | Local browser studio, upload/drop, re-planning, animation and timing controls |
| [`tools/plotter_control.py`](tools/plotter_control.py) | Thin command entry point for compile, inspect, G-code export, calibration and guarded execution |
| [`tools/plotjob.py`](tools/plotjob.py) | SHA-bound plot jobs, bounds proof, GRBL compilation and fail-closed serial streaming |
| [`tools/plotsim.py`](tools/plotsim.py) | SVG preflight, curve flattening, ordering, travel and acceleration/junction timing |
| [`tools/build_plotsim_viewer.py`](tools/build_plotsim_viewer.py) | Portable embedded HTML viewer generation |
| [`tools/plotsim_viewer.tmpl`](tools/plotsim_viewer.tmpl) | Interactive viewer UI |

The full operator and safety documentation is
[`docs/plotter/PLOTTER_SOFTWARE.md`](docs/plotter/PLOTTER_SOFTWARE.md).

Generate a portable viewer:

```bash
.venv/bin/python tools/build_plotsim_viewer.py \
  artwork.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json \
  --strict-svg \
  --out build/plotsim/artwork.html
```

Compile and inspect a deterministic plot job:

```bash
.venv/bin/python tools/plotter_control.py compile \
  artwork.svg \
  --profile plotter-profiles/my-machine.json \
  --order optimised \
  --out build/plot-jobs/artwork.plotjob.json

.venv/bin/python tools/plotter_control.py inspect \
  build/plot-jobs/artwork.plotjob.json
```

## Hardware safety boundary

Physical execution is intentionally disabled by the bundled profiles.
`axidraw-class-simulation-v1.json` is a timing model, not an EBB/AxiDraw
driver. `grbl-servo-template-v1.json` is a non-executable template.

GRBL execution remains blocked until an exact machine profile supplies and
verifies work coordinates, controller settings, page bounds, pen widths,
timing calibration, homing behavior, and serial configuration. Never bypass
those gates to make a job run.

## Verification

```bash
python3 scripts/verify_repository.py --full
.venv/bin/pytest -q tests/test_plotter_system.py
.venv/bin/pytest -q tests/test_paper_and_pens.py
.venv/bin/ruff check tools tests scripts
.venv/bin/mypy tools/plotsim.py tools/plotjob.py \
  tools/build_plotsim_viewer.py tools/plotter_studio.py
```

The source portfolio already records:

- 64 focused simulator, controller and physical-pen truthfulness tests;
- 423 format-valid master SVGs;
- 22 PNG and 22 SVG contact sheets;
- complete SHA-256 inventory coverage;
- zero visible OpenStreetMap/OSM wording on artwork or contact-sheet pages;
- external attribution and source/licence evidence retained in documentation,
  manifests, metadata and contracts.

See [`ARTWORK_AND_DATA_NOTICE.md`](ARTWORK_AND_DATA_NOTICE.md) before changing
repository visibility or redistributing the artwork.
