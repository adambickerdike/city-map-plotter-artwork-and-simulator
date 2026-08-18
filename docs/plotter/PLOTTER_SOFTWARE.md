# Plotter Studio and hardware controller

The plotting system has two operator-facing applications backed by one motion
engine:

1. **Plotter Studio** loads City Map Plotter SVGs, predicts elapsed time, and
   animates every pen-up travel, pen-down stroke, lift, lower, and manual pen
   change at real time or accelerated speed.
2. **Plotter Control** compiles the same planned geometry to a hash-bound plot
   job, produces a pen-up bounds proof and one GRBL program per physical pen,
   and can stream an explicitly approved job to a configured controller.

The browser never has a hardware endpoint. The controller is a separate CLI
with independent profile, digest, calibration, bounds, homing, and execution
gates.

```text
SVG bytes
  -> strict physical-SVG preflight
  -> millimetre paths and physical pen identities
  -> deterministic pen grouping and endpoint ordering
  -> shared trapezoidal/junction-deviation motion plan
  -> animated viewer
  -> SHA-256-bound plot-job JSON
  -> page/work-area transform
  -> pen-up bounds proof + per-pen GRBL
  -> acknowledged serial streaming
```

## Run the interactive simulator

Use any hardware-canonical generated master SVG. Generated viewers and jobs
belong under `build/` or another ignored output directory; do not commit them
as artwork.

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_studio.py \
  build/golf-v4-envelope-review/augusta-national.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json
```

The studio opens at `http://127.0.0.1:8042/`. It binds only to loopback unless
`--allow-remote` is deliberately supplied. Use **Load SVG** or drop another SVG
on the stage. Change motion values in the machine panel and select **Re-plan**.
An SVG with red preflight findings may still be useful as a diagnostic preview,
but its estimate is not an executable hardware claim; compile remains blocked
until the listed content is baked or converted.

The UI provides:

- real-time, 4x, 16x, 64x, 256x, and 1024x playback;
- pause, restart, and random-access scrubbing;
- document and optimised stroke ordering;
- animated pen-up travel and the live tool position;
- human pen-change countdowns;
- physical nib widths, pen sequence, distances, lifts, loads, swaps, and ink area;
- nominal total time plus a calibrated uncertainty interval;
- native-page and A-series paper previews with millimetre rulers;
- strict-preflight findings beside the artwork.

For a portable, read-only viewer without the local upload server:

```bash
PYTHONPATH=src .venv/bin/python tools/build_plotsim_viewer.py \
  artwork.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json \
  --strict-svg \
  --out build/plotsim/artwork.html
```

## Compile the actual plot program

Compilation uses the same flattened points and ordering as the simulator:

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_control.py compile \
  artwork.svg \
  --profile plotter-profiles/my-machine.json \
  --order optimised \
  --out build/plot-jobs/artwork.plotjob.json

PYTHONPATH=src .venv/bin/python tools/plotter_control.py inspect \
  build/plot-jobs/artwork.plotjob.json
```

The job records:

- source filename, byte count, and SHA-256;
- the canonical SHA-256 of the exact device profile used to plan it;
- exact machine model and uncertainty;
- page and ink-envelope bounds;
- pen identity, nominal/effective nib, ink, and calibration state;
- every ordered, flattened millimetre point;
- pen-down/up distances, motion time, lifts, loads, swaps, and likely time range;
- all preflight errors, warnings, and production blockers;
- a canonical `job_sha256` over the entire program.

Editing one point, speed, pen, or safety finding invalidates the digest. The
controller verifies it again before export or streaming. Editing or selecting
a different device profile also blocks export: recompilation is required so
the simulated acceleration, feeds, timing, work area, axes, and servo behavior
cannot drift from the controller program. Compiling without `--profile` is
valid for simulation and inspection, but deliberately creates a simulation-only
job that cannot be exported to hardware.

The SHA-256 is an integrity fingerprint, not a digital signature or an
adversarial authorization mechanism. The verifier also recomputes geometry,
distances, timing, move counts, and the page envelope from the stored strokes,
but operators must still treat plot jobs and machine profiles as trusted local
configuration files.

## SVG input contract

Hardware compilation is intentionally narrower than browser SVG rendering. An
accepted SVG must have:

- positive physical `width` and `height` and a matching
  `viewBox="0 0 WIDTH HEIGHT"`, with one user unit equal to one millimetre;
- top-level Inkscape layers representing physical pen loads;
- `data-plot-pen-id`, `data-plot-ink`, and `data-plot-nib-mm` metadata;
- baked path geometry using `M`, `L`, `C`, `H`, `V`, and `Z` commands;
- no visible fills, unbaked transforms, clips, masks, filters, text, raster
  images, `<use>`, primitive shapes, partial opacity, SVG dashes/markers, CSS,
  or ignored SVG drawing elements;
- an ink envelope that remains on the declared page and a page that fits the
  configured work area directly or after an allowed 90-degree rotation.

This matches the repository's generated physical SVGs. General illustration
SVGs must first convert text and shapes to paths and bake transforms/effects.
The compiler reports unsupported content instead of silently dropping it.
For canonical plates also run:

```bash
python3 tools/validate_format.py artwork.svg
```

## Why the estimate is realistic, but not automatically exact

Timing uses a trapezoidal velocity profile with acceleration-limited entry and
exit, junction-deviation cornering, separate pen-down and pen-up feeds, explicit
servo delays, command latency, and human pen-change time. Curves are flattened
at the profile's physical tolerance before both simulation and execution.
The GRBL profile uses the firmware's Cartesian rule that resolves equal X/Y
axis acceleration limits along each segment and corner; the AxiDraw-class
simulation profile retains an isotropic model because it is not a GRBL device.

No nominal profile can know belt tension, firmware planner settings, servo
travel, USB latency, pen drag, paper, or the operator's actual change time. The
committed simulation profile therefore reports a visible ±15% interval and is
marked `nominal-unmeasured`. It is a useful planning estimate, not a production
claim.

Fit the timing scale from complete observed plots, using seconds:

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_control.py calibrate-time \
  plotter-profiles/my-machine.json \
  --observation build/plot-jobs/short.plotjob.json:646 \
  --observation build/plot-jobs/curves.plotjob.json:1078 \
  --observation build/plot-jobs/full-map.plotjob.json:1922 \
  --out plotter-profiles/my-machine-timing-v2.json
```

Each job must be bound to the input profile. The fitter takes the median
motor-only scale while holding the job's explicit servo, command-latency, and
operator swap times fixed; this avoids incorrectly scaling a 25-second human
pen change when only the motors ran slowly. It preserves the largest observed
whole-job residual as the uncertainty floor. Time from the first confirmed pen
load until the controller reports final `Idle`, and use varied short, long,
straight, curved, travel-heavy, and lift-heavy jobs. Timing calibration does
not measure ink width or make an artwork production-ready.

Calibration creates a new profile identity. Recompile the artwork against that
new profile before bounds export or execution:

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_control.py compile \
  artwork.svg \
  --profile plotter-profiles/my-machine-timing-v2.json \
  --out build/plot-jobs/artwork.plotjob.json
```

The same rule applies after any later hardware-verification edit.

## Configure a real controller

`grbl-servo-template-v1.json` is deliberately non-executable. Copy it to a new,
versioned machine-specific profile and establish all of the following on the
real machine:

- measured work width, height, paper origin, axis inversion, and page rotation;
- an explicit G54–G59 work coordinate and its measured persistent XYZ offset;
- verified GRBL baud rate, homing, and unlock behavior;
- locked live GRBL `$3`, `$11`, `$100`, `$101`, `$110`, `$111`, `$120`,
  `$121`, `$130`, and `$131` values matching axis direction, steps/mm,
  junction tolerance, maximum feeds, X/Y acceleration, and travel;
- locked `$30`, `$31`, and `$32` values when `M3`/`M4` spindle PWM drives the
  pen servo;
- the controller's exact pen-up and pen-down commands and safe servo range;
- conservative pen-up/down feed and acceleration limits;
- measured lift/lower delays and timed complete-plot samples;
- exact paper stock, pressure/height, and ten-specimen effective width for each
  physical pen;
- a successful dry bounds proof and physical registration plot.

Only after those checks should the profile set:

```json
{
  "calibration_state": "hardware-verified",
  "controller": {
    "execution_enabled": true,
    "coordinate_system": "G54",
    "expected_work_offset_mm": {"x": 0, "y": 0, "z": 0},
    "expected_settings": {
      "$3": 0,
      "$11": 0.05,
      "$30": 1000,
      "$31": 0,
      "$32": 0,
      "$100": 80,
      "$101": 80,
      "$110": 4800,
      "$111": 4800,
      "$120": 500,
      "$121": 500,
      "$130": 420,
      "$131": 297
    }
  }
}
```

The controller currently implements real GRBL serial streaming. The bundled
AxiDraw-class profile is a simulation model, not an EBB/AxiDraw hardware
driver. Add and test a controller adapter for the exact machine rather than
pretending that GRBL commands are portable across firmware families.
Plot-job v1's GRBL adapter supports explicit `M3 S…`/`M4 S…` spindle-PWM servo
positions and `M5`; it intentionally rejects Z-axis or arbitrary custom pen
commands because their motion and timing are not represented by this model.

Install serial support only on the control computer:

```bash
.venv/bin/pip install -e '.[plotter]'
```

## Bounds proof and G-code export

Always export and run the pen-up page outline first:

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_control.py export-gcode \
  build/plot-jobs/artwork.plotjob.json \
  --profile plotter-profiles/my-machine-timing-v2.json \
  --bounds-only \
  --out-dir build/plot-jobs/artwork-bounds
```

The bounds file never contains the profile's pen-down command. Plotter Control
can stream that same pen-up proof directly (remove the pen if possible):

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_control.py run \
  build/plot-jobs/artwork.plotjob.json \
  --profile plotter-profiles/my-machine-timing-v2.json \
  --port /dev/ttyACM0 \
  --bounds-only \
  --execute \
  --confirm-job-sha FULL_64_CHARACTER_DIGEST
```

Bounds-only execution permits artwork/pen calibration blockers because it
never lowers a pen, but profile, digest, firmware, work-area, and structural
SVG gates remain active.

Once the proof is physically correct, export the per-pen programs:

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_control.py export-gcode \
  build/plot-jobs/artwork.plotjob.json \
  --profile plotter-profiles/my-machine-timing-v2.json \
  --out-dir build/plot-jobs/artwork-gcode
```

File boundaries are manual pen-change boundaries. Each file uses absolute
millimetre coordinates, an explicit pen-up travel to every stroke, the exact
planned pen-down polyline, and dwell times matching the model; it leaves the
pen up at every file boundary. Plot-job v1 deliberately forbids an
implicit per-pen return-home move because hidden travel would make execution
disagree with the simulation. Export requires a new or empty output directory,
so stale programs from an older job cannot remain beside the current files.
Normal export enforces the same artwork safety gate as live execution; use
`--allow-review-output` only for an intentional calibration proof.

## Live execution

Live execution requires all of these independently:

- a valid job digest;
- `--execute`;
- an exact `--confirm-job-sha` match;
- a profile with `execution_enabled: true`;
- a profile with `calibration_state: hardware-verified`;
- live GRBL firmware settings matching the bound profile;
- no SVG, bounds, machine, or physical-pen blockers.

```bash
PYTHONPATH=src .venv/bin/python tools/plotter_control.py run \
  build/plot-jobs/artwork.plotjob.json \
  --profile plotter-profiles/my-machine-timing-v2.json \
  --port /dev/ttyACM0 \
  --execute \
  --confirm-job-sha FULL_64_CHARACTER_DIGEST
```

Start the command with no pen in the holder. After homing and coordinate
verification, Plotter Control explicitly commands the calibrated pen-up state
before it asks you to load the first pen.

Before homing, the streamer reads `$$` and refuses execution if the live GRBL
junction, calibration, feed, acceleration, direction, or travel settings differ
from the bound profile. It unlocks, has GRBL parse the complete job once in
`$C` check mode with no motor motion, then restores the unlock after check mode's
reset and homes. After homing it reads `$#`, verifies the selected work
coordinate's XYZ offset, and refuses any active temporary `G92` offset. The real
pass uses GRBL's character-counting flow control, never exceeds its receive-
buffer budget, and accounts for every `ok`/`error` response. This keeps the
look-ahead planner fed for dense curves while retaining a fail-fast dry parse
before any pen is loaded.
Because `ok` means “accepted into the controller” rather than “motion is
finished,” it also polls GRBL status until `Idle` after homing and at every pen
boundary. The next pen prompt cannot appear while buffered motion is still
running. Ctrl-C sends GRBL's immediate feed-hold byte and stops; any command,
alarm, or idle-timeout failure also sends feed hold. After an interruption,
manually lift the pen, reset the controller to discard buffered commands,
inspect the sheet, and re-home. Software is not a substitute for the machine's
physical emergency stop or for staying with a moving plotter.

For export or live execution, `--allow-review-output` exists for controlled
calibration proofs only. It can acknowledge the `unmeasured-pens` and
`uncalibrated-machine-timing` blockers; it can never bypass malformed SVG,
ignored content, fill, bounds, or other structural errors. It does not change
the job's recorded blockers or promote nominal artwork to production-ready.

## Verification

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_plotter_system.py \
  tests/test_paper_and_pens.py

.venv/bin/ruff check \
  tools/plotsim.py \
  tools/build_plotsim_viewer.py \
  tools/plotter_studio.py \
  tools/plotjob.py \
  tools/plotter_control.py
```

The tests bind deterministic jobs and digests, preflight refusal, uncertainty,
large-job ordering equivalence, page rotation, G-code feeds, per-pen programs,
and the invariant that a bounds preview never lowers the pen.
