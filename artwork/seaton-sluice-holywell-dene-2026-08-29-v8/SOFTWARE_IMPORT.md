# Software import — Seaton Sluice v8

This folder is the complete A3 landscape review package for the approved
Seaton Sluice, Holywell Dene and Seaton Delaval Hall plate. It contains the
master PNG/SVG, eleven ordered per-pen SVGs, render manifest, SHA-bound plot
job, portable simulator, pinned map source, source contract, QA evidence,
rebuild script and release checksums.

`SOFTWARE_IMPORT.json` is the machine-readable entry point. Paths in that file
are relative to this folder. The simulator/controller paths point back to the
shared, versioned tools in this repository.

The retained `docs/rebuild.sh` records the canonical renderer recipe and is
provenance evidence; it belongs with the source `city-map-plotter` workspace.
It is not required for importing or simulating the finished SVGs in this
artwork-and-simulator repository.

## Clone and verify

Git LFS is required for the real PNG, SVG and plot-manifest payloads:

```bash
git lfs install
git clone https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator.git
cd city-map-plotter-artwork-and-simulator
git lfs pull
python3 scripts/verify_repository.py --full
cd artwork/seaton-sluice-holywell-dene-2026-08-29-v8
sha256sum -c CHECKSUMS.sha256
cd ../..
```

## Open in the plotting studio

From the repository root:

```bash
scripts/run_seaton_sluice_studio.sh
```

Equivalent direct command:

```bash
python3 tools/plotter_studio.py \
  artwork/seaton-sluice-holywell-dene-2026-08-29-v8/artwork/seaton-sluice-holywell-dene-a3-landscape.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json
```

Inspect the supplied source-SHA-bound plot job:

```bash
python3 tools/plotter_control.py inspect \
  artwork/seaton-sluice-holywell-dene-2026-08-29-v8/simulation/seaton-sluice-holywell-dene.plotjob.json
```

For software that accepts a complete SVG, import
`artwork/seaton-sluice-holywell-dene-a3-landscape.svg`. For software that
expects one file per pen load, import the eleven files in `artwork/pen-svgs/`
in their numeric filename order. The `.plot.json` manifest carries layer,
physical pen and source evidence; the `.plotjob.json` carries the deterministic
motion plan and safety state.

## Safety boundary

The included AxiDraw-class profile is simulation-only. The supplied plot job
correctly has `execution_allowed: false`: effective pen widths and machine
timing are uncalibrated. Do not treat import success as permission for physical
execution. Use a measured device profile and follow
`docs/plotter/PLOTTER_SOFTWARE.md` before sending motion to hardware.
