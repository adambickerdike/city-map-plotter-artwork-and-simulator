# Examples

## Aveling & Porter No. 5499

The [Aveling & Porter No. 5499 final blueprint](../artwork/aveling-porter-5499-blueprint-v14/index.html)
is a single large A3 side elevation of the 1904 R6, BS 8711. Revision 14 redraws
the upright Invicta horse from the supplied GW Railwayana Auctions casting
photograph, with a continuous contour and 39 sculpted relief details. All
engine, plaque and lettering paths are retained. White 0.30, 0.40 and 0.50 mm
pen widths and complete stroke lettering are verified.
[The complete package](../artwork/aveling-porter-5499-blueprint-v14/README.md)
includes artwork, previews, photographic comparison, simulator, checksums and
portable rebuild source. [Download the full ZIP](../artwork/aveling-porter-5499-blueprint-v14.zip).

A separate [colour pen edition](../artwork/aveling-porter-5499-colour-v1/index.html)
uses that same refined drawing on white paper, with photograph-informed green,
red, black and brass-gold linework. It includes eight verified pen layers,
an optimised plot job, an A3 colour PDF and a comparison with the blueprint.
[Colour package and instructions](../artwork/aveling-porter-5499-colour-v1/README.md) ·
[Download the colour ZIP](../artwork/aveling-porter-5499-colour-v1.zip).

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
