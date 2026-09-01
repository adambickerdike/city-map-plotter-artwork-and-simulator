# Repository layout

## `artwork/`

`latest-map-portfolio-2026-08-18-v3/` is the immutable verified handoff.
Its numbered family folders each contain:

- `artwork/`: master SVG, PNG and plot-manifest triplets;
- `contact-sheets/`: overview sheets in PNG and SVG;
- `contracts/`: frozen or best-available factual/source evidence;
- `code/`: family-specific generator entry points;
- `docs/`: release, QA and reproduction guidance;
- `release-metadata/`: original reports, indexes, attribution and checksums;
- `README.md` and `LLM_HANDOFF.md`.

The portfolio root adds the gallery, global catalog, external attribution,
source-selection ledger, build validation, format validation, visible-text
audit and complete checksums.

## `tools/`

The simulator and controller form one pipeline:

```text
physical SVG
  -> strict SVG preflight
  -> millimetre strokes and pen identities
  -> document/merged/optimised ordering
  -> acceleration and junction-aware motion plan
  -> browser animation or SHA-bound plot job
  -> bounds proof and per-pen GRBL
  -> guarded acknowledged serial streaming
```

`plotsim.py` owns the geometry and motion model. `build_plotsim_viewer.py` and
`plotter_studio.py` consume that model for visualisation. `plotjob.py` consumes
the same model for deterministic jobs and hardware compilation.

## `plotter-profiles/`

- `axidraw-class-simulation-v1.json`: nominal, unmeasured simulation profile;
  not an AxiDraw EBB driver.
- `grbl-servo-template-v1.json`: intentionally non-executable starting point
  for a real, measured GRBL installation.

Schemas live in `docs/plotter/`.

## `src/` and `tests/`

`src/city_map_plotter/` contains the physical plate, nib, paper, style and
stroke-compilation modules needed by the focused truthfulness suite.
`test_plotter_system.py` exercises the shared motion/job/controller boundary;
`test_paper_and_pens.py` checks that paper formats, resolved nibs and simulator
reports describe the same physical plan; `test_repository_verification.py`
guards the distinction between non-plotted provenance and forbidden visible
provider/licence copy. Together they collect 68 tests.

## `examples/`

- `augusta-national/`: master SVG, PNG, plot manifest and pen-separated SVG
  jobs used by the simulator example.
- `generated-viewers/`: portable HTML viewers, example plot jobs and a studio
  screenshot. These are derived review outputs, not source artwork.

## `scripts/`

- `run_augusta_studio.sh`: resolves its own repository path, so it works even
  when invoked from another current directory.
- `verify_repository.py`: checks the exact catalog, artwork pairings, stored
  digests, release checksums and simulator files.
