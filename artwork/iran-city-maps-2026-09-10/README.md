# Tehran and Karaj — A3 city maps

Two separate A3 portrait prints in the existing city/university style: serif city name, coordinates and diamond compass above the map, double black border, coloured roads, blue waterways, green parks and purple landmarks.

These are detailed central-city compositions; their exact framing is recorded in `catalog.json`. Full qualifying streets, service roads, paths and railways use the house `plotter-faithful` recipe, 0.04 mm simplification, centreline roads and dotted water.

![Both city prints](comparison.png)

| City | Pen plotting download | Master | Preview | Print PDF |
|---|---|---|---|---|
| Tehran | [Download ZIP](https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator/releases/download/tehran-karaj-a3-2026-09-10/tehran-a3-pen-files.zip) | [SVG](tehran/tehran-a3-portrait.svg) | [PNG](tehran/tehran-a3-portrait.png) | [PDF](tehran/tehran-a3-portrait.pdf) |
| Karaj | [Download ZIP](https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator/releases/download/tehran-karaj-a3-2026-09-10/karaj-a3-pen-files.zip) | [SVG](karaj/karaj-a3-portrait.svg) | [PNG](karaj/karaj-a3-portrait.png) | [PDF](karaj/karaj-a3-portrait.pdf) |

[Import guide](SOFTWARE_IMPORT.md) · [Animated pen preview](simulation/cities.html) · [Catalogue](catalog.json) · [Source credits](ATTRIBUTION.md)

The downloads contain real SVG files and ordered pen layers. Print/import at **100%, A3 portrait, 297 × 420 mm**. PNGs are 254 DPI. The map source snapshot is 9 September 2026; exact extracts and hashes are included under `reproduction/`.

Rebuild into a new directory using Python 3.13, the existing production reproduction requirements, Pillow and Inkscape:

```bash
python scripts/build_iran_city_maps.py --output build/iran-city-rebuild
python scripts/verify_iran_city_maps.py --release build/iran-city-rebuild
```

Digital geometry and nominal simulation are verified separately from a physical pen/paper proof.
