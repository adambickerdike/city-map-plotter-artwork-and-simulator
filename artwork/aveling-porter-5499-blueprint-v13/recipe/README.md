# Portable final drawing recipe

This folder contains the drawing code and the exact shared modules and data
observed during build, plot compilation and verification. Package checksums
pin those bytes. Python 3.13 and Inkscape 1.4.4 were used; Python dependencies
are pinned in `requirements.txt`.

Run `python rebuild.py --output-dir /tmp/aveling-5499-rebuild` after installing
the requirements. The output uses the retained references and creates the
SVG, pen layers, PNG, plot job, simulator, verification records and A3 PDF.
No machine is connected or operated. See the package README for setup.

Normal builds use the frozen source-based emblem curves. The optional
`prepare_emblem.py` records their original Potrace procedure and is not needed
for rebuilding. Image rights and source scope remain in the evidence ledger.
