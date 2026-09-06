#!/usr/bin/env python3
"""Export a small website sample set from verified final map previews.

Requires Pillow only for rebuilding. The WebPs and JSON manifest can be used
directly by a website without installing the plotting renderer or Git LFS.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "artwork/production-maps-2026-09-06"
OUTPUT = ROOT / "website-examples"
REPOSITORY = "adambickerdike/city-map-plotter-artwork-and-simulator"
SELECTION = (
    ("newcastle-city", "Newcastle — city", "08-city-maps-uk", "newcastle-city-a3-portrait"),
    ("newcastle-university", "Newcastle — university", "01-university-cities-uk", "newcastle-university-a3-portrait"),
    ("cambridge-city", "Cambridge — city", "08-city-maps-uk", "004-uk-university-cambridge"),
    ("edinburgh-city", "Edinburgh — city", "08-city-maps-uk", "025-uk-university-edinburgh"),
    ("oxford-university", "Oxford — university", "01-university-cities-uk", "005-uk-university-oxford"),
    ("stanford-university", "Stanford — university", "02-university-cities-us", "002-us-university-stanford"),
    ("west-highland-way", "West Highland Way", "03-hiking-maps", "RTE-GB-WHW-01--terrain-relief"),
    ("seaton-sluice", "Seaton Sluice and Holywell Dene", "03-hiking-maps", "seaton-sluice-holywell-dene-a3-landscape"),
    ("london-marathon", "London Marathon", "04-marathon-courses", "001-london-marathon-2026"),
    ("henley-royal-regatta", "Henley Royal Regatta", "05-rowing-races", "henley-royal--a3-portrait"),
    ("monaco", "Monaco", "06-f1-courses", "monaco-2026--circuit-atlas-v2-a3-portrait"),
    ("silverstone", "Silverstone", "06-f1-courses", "great-britain-silverstone-2026--circuit-atlas-v2-a3-landscape"),
    ("st-andrews-old-course", "St Andrews — The Old Course", "07-golf-courses", "old-course-st-andrews"),
    ("augusta-national", "Augusta National", "07-golf-courses", "augusta-national"),
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    catalog = json.loads((RELEASE / "catalog.json").read_text())
    rows = {(a["domain"], a["id"]): a for a in catalog["artifacts"]}
    (OUTPUT / "images").mkdir(parents=True, exist_ok=True)
    examples = []
    sections = []
    for slug, title, domain, artifact_id in SELECTION:
        row = rows[domain, artifact_id]
        source = RELEASE / row["png"]["path"]
        if sha(source) != row["png"]["sha256"]:
            raise ValueError(f"Source preview changed: {source}")
        with Image.open(source) as original:
            preview = original.convert("RGB")
        preview.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        path = OUTPUT / "images" / f"{slug}.webp"
        preview.save(path, "WEBP", lossless=True, method=6)
        with Image.open(path) as decoded:
            if decoded.convert("RGB").tobytes() != preview.tobytes():
                raise ValueError(f"Web preview did not retain the resized pixels: {slug}")
        links = {
            role: "../" + (RELEASE / row[key]["path"]).relative_to(ROOT).as_posix()
            for role, key in (("master_svg", "svg"), ("full_size_png", "png"),
                              ("plot_job", "job"), ("plot_manifest", "manifest"))
        }
        examples.append({
            "slug": slug, "title": title, "domain": domain, "artifact_id": artifact_id,
            "format_id": row["format_id"], "image": path.relative_to(OUTPUT).as_posix(),
            "image_url": f"https://raw.githubusercontent.com/{REPOSITORY}/main/website-examples/images/{slug}.webp",
            "width": preview.width, "height": preview.height, "bytes": path.stat().st_size,
            "sha256": sha(path), "source_png_sha256": row["png"]["sha256"], **links,
        })
        sections.append(
            f"### {title}\n\n"
            f'<a href="{links["full_size_png"]}"><img src="images/{slug}.webp" width="420" alt="{title} map"></a>\n\n'
            f'[Website image](images/{slug}.webp) · [Full PNG]({links["full_size_png"]}) · [Plotting SVG]({links["master_svg"]})\n'
        )
    manifest = {
        "schema_version": 1, "release_id": catalog["release_id"], "example_count": len(examples),
        "image_format": "WebP", "maximum_edge_px": 1800,
        "description": "Lossless WebP encoding of resized final map previews, with no crop or added text.",
        "attribution": "../artwork/production-maps-2026-09-06/ATTRIBUTION.md",
        "full_catalog": "../artwork/production-maps-2026-09-06/catalog.json",
        "examples": examples,
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT / "README.md").write_text(
        "# Website map examples\n\n"
        "Fourteen examples from the current 457-map collection, including the updated Newcastle city and university maps. "
        "Every image comes from its final plotting artwork.\n\n"
        "Use the WebP files in [images/](images/) for the website. The [example manifest](manifest.json) includes titles, "
        "dimensions, direct image URLs, hashes and links to the corresponding plotting files. "
        "Copy the images into the website's public asset directory and use those local paths. "
        "These small WebPs are stored directly in Git and can also be downloaded individually without Git LFS.\n\n"
        "The [complete collection](../artwork/production-maps-2026-09-06/) has all 457 masters, previews and plot jobs. "
        "For its large files, clone with Git LFS and run `git lfs pull`. Use the master SVGs for plotting. "
        "Keep the [accompanying credits](../artwork/production-maps-2026-09-06/ATTRIBUTION.md) and each map's source terms with the relevant website listing.\n\n"
        + "\n".join(sections)
        + "\nRebuild these previews with `python scripts/build_website_examples.py` in an environment with Pillow installed.\n"
    )
    print(json.dumps({"examples": len(examples), "bytes": sum(x["bytes"] for x in examples)}, indent=2))


if __name__ == "__main__":
    main()
