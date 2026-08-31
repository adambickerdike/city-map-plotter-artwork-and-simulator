# Software import — personalised Carlisle map v4

This folder is the complete A3 portrait review package for the final
University of Cumbria / Carlisle plate. It contains the master PNG and SVG,
eleven ordered per-pen SVGs, render manifest, SHA-bound plot job, portable
simulator, nominal simulation profile, exact pinned map source, source
contract, QA evidence, rebuild recipe, LLM handoff and release checksums.

`SOFTWARE_IMPORT.json` is the machine-readable entry point. Paths in that file
are relative to this folder. The simulator/controller paths bind the release
to the shared, versioned tools in this repository.

The retained `docs/rebuild.sh` and `docs/verify_release.py` record the canonical
renderer process and fail-closed QA. The exact renderer-file hashes are sealed
in `SOURCE-CONTRACT.json`; the rebuild belongs in the source
`city-map-plotter` workspace and is not required to import or simulate the
finished SVGs here.

## Clone and verify

Git LFS is required for the real PNG, SVG and plot-manifest payloads:

```bash
git lfs install
git clone https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator.git
cd city-map-plotter-artwork-and-simulator
git lfs pull
python3 scripts/verify_repository.py --full
cd artwork/carlisle-university-fusehill-personalised-2026-08-31-v4
sha256sum -c CHECKSUMS.sha256
cd ../..
```

## Open in the plotting studio

From the repository root:

```bash
scripts/run_carlisle_university_studio.sh
```

Equivalent direct command:

```bash
python3 tools/plotter_studio.py \
  artwork/carlisle-university-fusehill-personalised-2026-08-31-v4/artwork/carlisle-university-fusehill-personalised-a3-portrait.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json
```

Inspect the supplied source-SHA-bound job:

```bash
python3 tools/plotter_control.py inspect \
  artwork/carlisle-university-fusehill-personalised-2026-08-31-v4/simulation/carlisle-university-fusehill-personalised.plotjob.json
```

For software that accepts a complete SVG, import
`artwork/carlisle-university-fusehill-personalised-a3-portrait.svg`. For
software that expects one file per pen load, import the eleven files in
`artwork/pen-svgs/` in numeric filename order. The `.plot.json` manifest
carries layer, source and physical-pen evidence; the `.plotjob.json` carries
the deterministic motion plan and its safety state. The PNG is preview-only
and must never be traced for plotting.

## Safety boundary

The included AxiDraw-class profile is simulation-only. The supplied plot job
correctly has `execution_allowed: false`: effective pen widths and machine
timing are uncalibrated, and 1,302 proximity candidates still require human
review. Use a measured device profile and follow
`docs/plotter/PLOTTER_SOFTWARE.md` before sending motion to hardware.
