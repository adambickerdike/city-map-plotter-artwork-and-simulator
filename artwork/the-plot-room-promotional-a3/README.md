# The Plot Room — A3 promotional writing plots

The colour prints retain the original road, river, path, park and landmark colours from the city map, with **black outlines around the letters**. The blueprint prints use **white pen on dark blue**. All eight exports have **theplotroom.com** centred underneath in plotted lowercase lettering.

[Open the local gallery](index.html) · [Colour overview](colour-comparison.png) · [Blueprint overview](blueprint-comparison.png)

![Colour and blueprint examples](featured-comparison.png)

| Design | Colour | Blueprint |
|---|---|---|
| 01 — City stencil | [PNG](colour/01-city-stencil.png) · [PDF](colour/01-city-stencil.pdf) | [PNG](blueprint/01-city-stencil.png) · [PDF](blueprint/01-city-stencil.pdf) |
| 02 — Wide wordmark | [PNG](colour/02-wide-wordmark.png) · [PDF](colour/02-wide-wordmark.pdf) | [PNG](blueprint/02-wide-wordmark.png) · [PDF](blueprint/02-wide-wordmark.pdf) |
| 03 — Heritage serif | [PNG](colour/03-heritage-serif.png) · [PDF](colour/03-heritage-serif.pdf) | [PNG](blueprint/03-heritage-serif.png) · [PDF](blueprint/03-heritage-serif.pdf) |
| 04 — Ink and river | [PNG](colour/04-ink-and-river.png) · [PDF](colour/04-ink-and-river.pdf) | [PNG](blueprint/04-ink-and-river.png) · [PDF](blueprint/04-ink-and-river.pdf) |

Options 01 and 04 now share the original map colours and identical stencil geometry; their earlier option numbers are retained. Option 02 is landscape, and the others are portrait.

Each design includes an A3 vector PDF, 300 DPI PNG, smaller preview, display SVG, stroke-only `.plot.svg`, per-pen SVGs and a QA record. Print at **actual size / 100%**. The blueprint display SVG/PDF contains the dark blue background; its plotting SVG contains only white pen strokes. These are nominal pen simulations, with no physical machine enabled.

The original letter and map paths are preserved exactly. The website uses the existing vector stroke font. Colour records are checked against the source city SVG; preflight, website placement, pen parity, image dimensions and PDF page dimensions are checked during generation.

The original local studies remain in `build/the-plot-room-a3-outlined-blueprint-2026-09-08/`. The current outputs are here under `artwork/the-plot-room-promotional-a3/`.

## Rebuild and attribution

Run `scripts/build_plot_room_promotion.py` using the existing renderer environment with Pillow and Inkscape installed. The preserved input vectors are in `reproduction/inputs/`; the frozen font and format come from the existing production renderer. No new map download is needed. Run `scripts/verify_plot_room_promotion.py` to verify a downloaded package. Keep [ATTRIBUTION.md](ATTRIBUTION.md) with promotional use.
