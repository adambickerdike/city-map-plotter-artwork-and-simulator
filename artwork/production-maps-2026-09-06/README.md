# Map collection — production-maps-2026-09-06

457 final digital masters with matching 254 DPI PNGs, thumbnails, per-pen SVGs and verified plot jobs. Open [the gallery](index.html) or use [catalog.json](catalog.json) to select a map.

All map and course geometry is preserved from the approved editions. Source credits and process notes accompany the files rather than being drawn. Newcastle is newly rendered from the pinned Tyne and Wear extract with the full plotter-faithful house recipe.

All 84 city-style headers place the name at the top left, coordinates underneath, and a vertically centred compass in the right column. This includes UK and US university editions, UK city editions and Seaton. Map linework and footer personalisation are preserved.

Import the master SVG into the plotting studio. Use the ordered files under each map's `pens/` directory for separate physical pen runs. Every path in the catalog is relative to this directory. The `.plotjob.json` records the final master's SHA-256 and the exact serialized motion.

Run `python scripts/verify_production_maps.py --full` from the repository root after cloning with Git LFS. Digital format/import QA passes; the supplied machine profile remains a simulation profile. Physical pen/stock calibration and a plotted proof remain required, as do any unresolved source-specific release conditions recorded in the original manifests.
