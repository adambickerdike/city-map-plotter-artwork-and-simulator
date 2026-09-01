# Artwork, data and hardware notice

## Review status

This repository is a public review and engineering handoff. Inclusion does
not imply that an artwork is cleared for public sale, event branding,
commercial merchandising, or production plotting.

Family-specific rights, source, physical-proof and non-endorsement caveats are
binding. Read each numbered portfolio folder's `README.md`,
`LLM_HANDOFF.md`, `contracts/`, `docs/`, and `release-metadata/`.

## Map-data attribution

Map-data credit is deliberately external to the plotted pages. The portfolio
root `ATTRIBUTION.md` records the required OpenStreetMap contributor and ODbL
credit. Embedded SVG metadata, plot manifests, source contracts, organiser
sources, hashes and licence records remain part of the evidence package.

No shipped production SVG draws map-provider or map-data-licence wording on
the page, and promoted viewers and galleries do not display it either.
`python3 scripts/verify_repository.py` enforces that rule across the complete
repository. The same credit must still accompany a public product in its
listing, packaging or other external attribution surface.

Do not remove or weaken that evidence when publishing or deriving an artwork.

## Event and venue names

Marathon, rowing, Formula 1 and golf subjects may involve organiser, event,
venue, trademark, database, or route-redistribution considerations. The
repository does not grant rights held by third parties and is not an official
product of those organisations.

## Physical plotting

The bundled AxiDraw-class profile is simulation-only. The GRBL profile is a
non-executable template. A real machine must have a measured, hardware-verified
profile and must pass every controller, calibration, page, work-offset,
preflight and bounds gate documented in `docs/plotter/PLOTTER_SOFTWARE.md`.

No broad copyright or hardware-safety licence is created by this handoff.
