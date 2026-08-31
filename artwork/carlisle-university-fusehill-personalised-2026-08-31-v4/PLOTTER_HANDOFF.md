# Plotter handoff — Carlisle personalised map v4

## Import master

Import
`artwork/carlisle-university-fusehill-personalised-a3-portrait.svg` at exactly
100% scale. The page is A3 portrait, 297 × 420 mm. Disable fit-to-page,
automatic centring, stroke expansion, path joining, and driver-side line-width
substitution. The SVG contains physical paths rather than editable SVG text.

The PNG is a visual preview only. Never trace or plot the PNG.

## Pen order

Plot the supplied page-sized files under `artwork/pen-svgs/` in numerical order:

1. Blue 0.40 mm — water areas
2. Blue 0.25 mm — waterways
3. Green 0.25 mm — green space
4. Purple 0.25 mm — landmark buildings, including Carlisle railway station
5. Grey 0.25 mm — road edges, local/minor roads, paths, and railways
6. Red 0.40 mm — major roads
7. Red 0.25 mm — secondary roads
8. Black 0.60 mm — map frame
9. Black 1.00 mm — A3 safe border
10. Black 0.40 mm — title, original compass, and personalised footer
11. Black 0.25 mm — coordinates

Every pen SVG retains the complete A3 page, registration, and physical stroke
width. Plot one pass per emitted path. Carlisle station is pinned source
geometry (`way/566812584`) in pen file 04; it is not an inferred symbol.

## Inspect and simulate

The portable simulator can be opened directly:

`simulation/carlisle-university-fusehill-personalised-plotsim.html`

From the repository root, the interactive studio is:

```bash
scripts/run_carlisle_university_studio.sh
```

Equivalent direct command:

```bash
python3 tools/plotter_studio.py \
  artwork/carlisle-university-fusehill-personalised-2026-08-31-v4/artwork/carlisle-university-fusehill-personalised-a3-portrait.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json
```

Inspect the supplied SHA-bound job with:

```bash
python3 tools/plotter_control.py inspect \
  artwork/carlisle-university-fusehill-personalised-2026-08-31-v4/simulation/carlisle-university-fusehill-personalised.plotjob.json
```

The nominal simulation is 2,828 strokes, 45,130 vertices, 40.93 m pen-down,
16.11 m pen-up, eleven pen loads, and 41:58 total, with an uncalibrated
35:40–48:15 range.

## Physical execution gate

The included profile is deliberately simulation-only and the supplied plot job
has `execution_allowed: false`. Before physical execution, create a profile for
the receiving machine and record its measured work area, origin, axes, speeds,
pen-up/down commands, servo delays, paper stock, and the effective width of all
eleven pens. Then compile a new SVG-hash-bound job using that profile.

The artwork audit also reports 1,302 below-nib proximity candidates. These need
human review before production approval. Do not bypass those gates merely to
make the controller run; the master and individual pen SVGs are complete, but
hardware readiness depends on the actual machine and pens.

## Integrity

From this package directory:

```bash
sha256sum -c CHECKSUMS.sha256
```

Keep `ATTRIBUTION.md` with every public copy. The attribution is external and
does not add visible marks to the plotted sheet.
