# Examples

## Aveling & Porter No. 5499

The [Aveling & Porter No. 5499 final blueprint](../artwork/aveling-porter-5499-blueprint-v12/index.html)
is a single large A3 side elevation of the 1904 R6, BS 8711. Revision 12 places
the larger worksplate left of the enlarged horse and gives the firebox a
continuous upright and rounded corner behind the rear wheel. Stroke lettering
and White 0.30, 0.40 and 0.50 mm pen assignments are verified.
[The complete package](../artwork/aveling-porter-5499-blueprint-v12/README.md)
includes artwork, previews, references, simulator, checksums and portable rebuild
source. [Download the full ZIP](../artwork/aveling-porter-5499-blueprint-v12.zip).

## Augusta National

`augusta-national/` contains the source example referenced by the simulator
documentation:

- one master SVG;
- one PNG preview;
- one plot manifest;
- nine pen-separated SVG machine jobs.

The cleaned nominal simulation reports 1,255 strokes, 9,588 vertices, an
estimated 21:04 duration, and an uncalibrated 17:55–24:14 range. Those numbers
are a planning estimate, not a calibrated production claim.

The plotted provider/licence rail has been removed from the master, preview
and black 0.40 mm pen layer. Source and licence evidence remains in non-plotted
metadata and `ARTWORK_AND_DATA_NOTICE.md`. The bundled optimized plot job and
portable viewer are SHA-bound to this exact cleaned master.

## Generated viewers

`generated-viewers/` contains portable HTML viewers, sample plot jobs and a
studio screenshot. They are derived review artifacts. The promoted
`augusta-national` viewer and plot job are regenerated from the shipped master;
use an ignored `build/` directory for experimental viewers.

Open `generated-viewers/augusta-national.html` for the portable Augusta
simulation.
