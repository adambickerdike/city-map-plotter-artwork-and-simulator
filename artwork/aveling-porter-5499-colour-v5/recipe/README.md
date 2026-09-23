# Portable colour edition 5 recipe

Rebuild the colour SVG, pen layers, PNG, plot job, simulator, verification and
A3 PDF with `python rebuild.py --output-dir /tmp/aveling-colour-v5-rebuild`.
Install `requirements.txt` first; Inkscape is used for PNG/PDF export.

The accepted revision-14 SVG is the exact geometry source. All 863 of its path
shapes are kept unchanged as black outlines, with the engine linework one pen
heavier. The builder finds the enclosed paper cells between them. The paint
plan in `tools/engineering_source_plates/aveling_5499_colour_v5/` assigns the
cells to parts and fills each part with pen lines, keeping at least 0.22 mm
clear of black ink. `verify.py` rechecks the exported SVG independently.

Every line width is one of the studio's pens. Gold parts are several lines
from the studio's 0.40 mm gold pen, set by `GOLD_NIB_MM` in `inventory.py`;
there is no broad gold nib.

The snapshot and evidence need no separate project checkout. Use `--skip-pdf`
to omit the conventional-print PDF. No machine is operated.
