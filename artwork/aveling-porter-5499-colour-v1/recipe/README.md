# Portable colour edition recipe

Rebuild the colour SVG, pen layers, PNG, plot job, simulator, verification and
A3 PDF with `python rebuild.py --output-dir /tmp/aveling-colour-rebuild`.
Install `requirements.txt` first; Inkscape is used for PNG/PDF export.

The accepted revision-14 SVG is the exact geometry source. The colour builder
assigns available studio inks and introduces only the three documented gold
strap insets. Source modules for the original drawing are retained alongside
the colour policy. The snapshot and evidence need no separate project checkout.
Use `--skip-pdf` to omit the conventional-print PDF. No machine is operated.
