# Verification record

The original portfolio was verified locally and from a clean GitHub clone on
18 August 2026. The standalone Seaton Sluice v8 extension was verified locally
and from a fresh GitHub clone on 29 August 2026. The personalised Carlisle v4
package was verified locally on 31 August 2026 before publication.

## Repository integrity

```text
catalog artifacts:          423
portfolio PNG files:        445
portfolio SVG files:        445
repository PNG files:       451
repository SVG files:       483
portfolio checksum entries: 2463 PASS
Seaton checksum entries:    32 PASS
Carlisle checksum entries:  32 PASS
visible page-text audit:     PASS
```

Command:

```bash
python3 scripts/verify_repository.py --full
```

## Published-repository proof

The GitHub repository was cloned into an empty temporary directory with
LFS smudging initially disabled. All remote objects were then fetched and
checked out before verification:

```text
Git tracked paths:          2,617
LFS tracked paths:          1,328
LFS payload:                3,193,815,853 bytes
LFS checkout:               PASS
Git LFS fsck:               PASS
clean working tree:         PASS
full checksum audit:        PASS
```

The Seaton extension was then checked from a separate depth-one clone of remote
commit `f5bf1aedfb424c128b163928807d1ff29f338888`. Git LFS downloaded the
14 package payloads directly from GitHub; all 32 release checksums passed and
the remote controller successfully inspected the SHA-bound plot job:

```text
Git tracked paths:          2,653
Seaton LFS paths:           14
Seaton checksum entries:    32 PASS
remote plot-job inspection: PASS
remote master bytes:        2,921,678
remote PNG bytes:           2,666,456
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

Ruff lint and formatting cover the simulator/controller tools, repository
verification script and focused tests. The copied renderer modules are retained
as generation-source evidence and are not reformatted by this release.

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

## Seaton Sluice v8 import proof

The standalone A3 package passed its own release verifier, all 32 release
checksums, the repository import-manifest audit and controller inspection:

```text
master SVG SHA-256:      e6bf7558193133487625d2ce1b272189d5b4811d793e9912cbba86624f875e81
PNG SHA-256:             6bd7e6d4cc9fd34e62ae1ca11881ce416a3036c6edd9612d12ab88d718d9f94b
plot-job SHA-256:        36fd6fbbb6d4eb567d554e7a134ab822cba7fbaac7958d0ccc3de1c39ca79cf1
master paths:            2,742
vertices:                36,402
pen loads:               11
estimated time:          37:31
uncalibrated interval:   31:53–43:09
physical execution:      BLOCKED as designed
```

## Carlisle v4 import proof

The final personalised A3 portrait package passed all release checksums, the
repository import-manifest audit, 3,108 plate-format checks and controller
inspection:

```text
master SVG SHA-256:      131c4daf7cf6cad25a8f15d6177c13de3cc34b477cdd392262a61da16ad3fa2d
PNG SHA-256:             ec1f48e34bd8819368fb2bfb3081794f3196e56ca9a8b3d2472f59d380a9f67a
plot-job SHA-256:        fb6aaf74d6fb87708b2bda7922068d80fda07ec19808130c46021c8e8bf127c0
master paths:            2,828
vertices:                45,130
pen loads:               11
Carlisle station paths:  1 in Purple 0.25 mm load 04
estimated time:          41:58
uncalibrated interval:   35:40–48:15
physical execution:      BLOCKED as designed
```

The source contract requires Carlisle railway-station building
`way/566812584` and all seventeen accepted Fusehill university buildings. The
station path is present in the master and the registration-matched pen-04 SVG.

## Launcher proof

`scripts/run_augusta_studio.sh --no-browser --port 0` was invoked from
`/home/adam`, outside the repository. The server selected a free loopback port,
returned `{"ok": true}` from `/health`, served the studio root with HTTP 200,
and shut down cleanly.

`scripts/run_seaton_sluice_studio.sh --no-browser --port 0` independently
selected a free loopback port, loaded the v8 master, returned `{"ok": true}`
from `/health`, served the studio root with HTTP 200, and shut down cleanly.

`scripts/run_carlisle_university_studio.sh --no-browser --port 0` independently
selected a free loopback port, loaded the final Carlisle master, returned
`{"ok": true}` from `/health`, served the studio root with HTTP 200, and shut
down cleanly.
