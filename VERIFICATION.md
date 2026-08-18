# Verification record

Verified locally on 18 August 2026 before the initial GitHub upload.

## Repository integrity

```text
catalog artifacts:          423
portfolio PNG files:        445
portfolio SVG files:        445
repository PNG files:       447
repository SVG files:       455
portfolio checksum entries: 2463 PASS
visible page-text audit:     PASS
```

Command:

```bash
python3 scripts/verify_repository.py --full
```

## Code verification

```text
focused tests:                64 PASS
Ruff lint:                    PASS
Ruff formatting:              PASS
mypy:                         PASS
generated viewer JavaScript:  PASS
```

The 64 tests are the 29 shared motion/job/controller tests in
`test_plotter_system.py` plus the 35 physical paper/nib/simulator truthfulness
tests in `test_paper_and_pens.py`.

## Augusta motion proof

Strict preflight, simulation, portable-viewer generation, plot-job compilation
and plot-job inspection all passed for the bundled Augusta master:

```text
strokes:               1,301
vertices:              9,846
pen-down distance:     19.66 m
pen-up distance:       11.65 m
pen loads:             9
optimised estimate:    21:37
uncalibrated interval: 18:22–24:51
```

The compiled job was SHA-bound and verified. Physical execution correctly
remained `BLOCKED` because the bundled pens are unmeasured and machine timing
is nominal.

## Launcher proof

`scripts/run_augusta_studio.sh --no-browser --port 0` was invoked from
`/home/adam`, outside the repository. The server selected a free loopback port,
returned `{"ok": true}` from `/health`, served the studio root with HTTP 200,
and shut down cleanly.

