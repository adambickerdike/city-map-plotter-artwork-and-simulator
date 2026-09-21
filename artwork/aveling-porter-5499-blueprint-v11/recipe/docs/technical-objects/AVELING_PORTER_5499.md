# Aveling & Porter No. 5499 - final revision 11

Final package: `examples/technical-objects/aveling-porter-5499-blueprint-v11/`.
Builder: `tools/engineering_source_plates/build_aveling_5499_layout.py`.
Construction and checks: `tools/engineering_source_plates/aveling_5499_v11/`.

Revision 11 lowers the complete engine by one A3 format gap (6 mm), preserving
its 371.6 mm width and all component paths. The horse and worksplate are
uniformly enlarged by 10% about the upper-right layout anchor. Their copy and
spacing scale with them, while the actual pen widths remain unchanged.
The footer and frame do not move. Layout verification compares the exported
paths with the hash-pinned revision 10 master. The badge stays clear of the
controls, and all geometry/font/pen checks continue to pass.

The far chain remains behind the central return pipe. Ground and wheel guides
remain absent.

The final A3 landscape sheet has one large canopy-free side elevation, an Invicta
horse and enlarged worksplate in the upper right, and a compact maker/model,
works/registration and year strip. It prints No. 5499, R6, BS 8711 and 1904.
Hidden construction and simplified staggered spokes remain illustrative; this
is not a factory drawing or dimensional survey.

All 841 plotted paths use the configured White blueprint pens: 707 on 0.30 mm,
129 on 0.40 mm and five on 0.50 mm. All twelve text blocks are regenerated from
the bundled plotter-grid-v4 font and compared with exported strokes, including
the curved worksplate heading. There are no missing character components.
Cap heights and stroke lengths pass the eight-nib and three-nib floors.

Verification includes 116 format checks, 706 engine endpoints, 173 circular
features and 299 spoke transitions. No unexplained contour gaps remain.
The far-chain audit checks the actual pipe mask and exported geometry; no
chain ink remains inside the tube. The SHA-pinned revision 10 master verifies the exact engine translation,
uniform badge enlargement and unchanged footer/frame.

Actual No. 5499 photographs by Benjamin Matthews and Terry Pinnegar constrain
the engine; the supplied video frame supports width/projection interpretation.
The MERL Fowler S1021 sheet supplies composition inspiration only. The horse
uses the Preston Services casting photograph; the separate worksplate adapts
the user's GWRA No. 6882 reference to the No. 5499 wording. Credits and scope
remain in the source ledger. No dimensions from the other engine are asserted.

The release contains the SVG, separate pen layers, PNG, one-page A3 PDF, plot
job, simulator, retained references, audit evidence, portable rebuild source
and checksum ledger. The rebuild snapshot isolates the exact dependencies
without changing the shared runtime in the publishing repository.

Nominal plotting is about 13 minutes. No physical machine is operated.
The complete final package is published to the artwork-and-simulator repository.
