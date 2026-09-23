# Aveling & Porter No. 5499 - final revision 14

Final package: `examples/technical-objects/aveling-porter-5499-blueprint-v14/`.
Builder: `tools/engineering_source_plates/build_aveling_5499_refined_invicta.py`.
Construction and checks: `tools/engineering_source_plates/aveling_5499_v14/`.

Revision 14 replaces the Invicta emblem with a drawing of the user-selected
GW Railwayana Auctions March 2025 lot 51 brass casting. The retained 1980 x 1529
photograph is hash-pinned. Its silhouette is isolated using brass colour and
adjacent dark casting edges, with highlights filled and a 5-pixel smoothing
radius. Potrace fits a single continuous outline of 220 line/cubic segments.
The frozen curves are used directly in normal builds. Thirty-nine named relief
paths follow the face, ears, mane, shoulder, belly, haunch, legs, hooves and tail;
reflections and surface defects are omitted.

The image-plane geometry is rotated 45 degrees clockwise into the upright
rearing pose, then uniformly scaled to 45.54 mm high. This is a presentation
rotation, not a calibrated perspective reconstruction. The existing revision-13
horse slot and plaque anchor are retained exactly. The emblem is approximately
44.19 mm wide, with the plaque on its left and matching vertical centres.

Every one of the 823 non-horse paths is unchanged from revision 13, including
the connected lower quadrant lever, rounded firebox wall, lowered engine,
steering chain occlusion, plaque, lettering and frame. One canopy-free side
elevation fills the page above the compact manufacturer/model, works number,
registration and year strip. Ground lines and wheel-perimeter guides remain
absent. No surveyed dimensions or factory tolerances are asserted.

All 863 paths use the configured White blueprint pens: 733 on 0.30 mm,
125 on 0.40 mm and five on 0.50 mm. The new horse has one 0.40 mm contour and
39 strokes of 0.30 mm relief. All twelve text blocks are regenerated from the
bundled plotter-grid-v4 font and compared with exported strokes, including
the curved worksplate heading. Cap heights and path lengths retain the
8-nib and 3-nib minimums. No raster content or live fonts enter the pen SVGs.

Verification includes the hash-pinned source photograph, source-to-paper
transforms and exact geometry of all 40 horse paths. All relief ink stays
inside the outline, with no crossed relief strokes. The entire non-horse
geometry is compared with the hash-pinned revision-13 master. The drawing
retains 116 format checks, 704 engine endpoints, 173 circular features and
299 spoke transitions; no unexplained engine gaps remain.

Actual No. 5499 photographs by Benjamin Matthews and Terry Pinnegar constrain
the engine. The supplied video frame supports width/projection interpretation.
The MERL Fowler S1021 sheet supplies composition inspiration only. The horse
uses GWRA lot 51; the separate worksplate adapts the user's GWRA No. 6882
reference to the requested No. 5499 wording. Credits and scope are retained
in the source ledger. Casting/worksplate dimensions are not assigned to this
engine. The older Preston Services image is retained as historical evidence.

The release contains SVG, pen layers, PNG, one-page A3 PDF, compiled plot job,
simulator, retained references, source overlay, checksums and portable rebuild
source. The snapshot includes the exact dependencies without changing the
shared runtime in the publishing repository. Nominal plotting is about
13 minutes with three pen loads. No physical machine is operated.
