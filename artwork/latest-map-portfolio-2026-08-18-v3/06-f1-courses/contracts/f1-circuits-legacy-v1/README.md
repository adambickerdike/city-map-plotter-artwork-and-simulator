# Former-F1 configuration source contract

This contract is a multi-era companion to `f1-circuits-2026`. It does not
trace Formula 1, FIA, circuit-owner, or third-party map artwork. Renderable
records are assembled only from exact, frozen OpenStreetMap relations or
ordered way selections; official sources establish configuration identity,
published length, reference-season event participation, and change history.
Lap direction is published only when a configuration-specific source states it;
otherwise it is visibly withheld. In particular, the FIA licensed-circuit
ledger's Left/Right column is pole position, not lap direction.

`configuration_reference_season` identifies the F1-era configuration used for
the plate title and seasonal context filter. The top-level integer `season`
remains the catalog release/freeze year for compatibility and is never shown as
the historic configuration year.

Three renderable identity tiers are permitted:

* `exact-historic-source`: the OSM object itself declares the historic era.
* `current-surviving-equivalent`: current OSM geometry is used only where
  authoritative history supports the surviving configuration. Current place
  names and context remain current-source facts and are not backdated.
* `current-source-f1-reference`: the current OSM course passes topology and
  length gates and the venue/configuration year is source-backed, but the work
  is explicitly not claimed as a surviving or period reconstruction.

Holds are first-class records. Demolished, materially altered, open, ambiguous,
or merely visually similar layouts have no normalized model and cannot render.
No software may infer a connector to close a source gap.

The frozen release contains 34 records: 19 renderable centreline plates and 15
holds. Bahrain/Sakhir and Jeddah are retained as 2025 reference-season plates
because their 2026 rounds were called off; they are not presented as 2026
calendar events. Every renderable centreline is closed exactly and differs by
no more than 1.0% from its cited published length.

Nordschleife, Brands Hatch GP, Estoril, and Kyalami also have separate
`current-source-f1-reference` plates beside their exact-period holds. These
plates deliberately show today's surviving course and current surroundings;
their subtitle names the F1 reference year, but their identity explicitly
rejects historic equivalence.

Source snapshots are produced by `tools/acquire_f1_legacy_sources.py`; the
offline normalized catalog is built by `tools/build_f1_legacy_catalog.py`.

```bash
.venv/bin/python tools/acquire_f1_legacy_sources.py --all
.venv/bin/python tools/build_f1_legacy_catalog.py
.venv/bin/python tools/build_f1_legacy_catalog.py --check
.venv/bin/pytest -q tests/test_f1_legacy.py
```
