#!/usr/bin/env python3
"""Build the two Iran city plates from pinned inputs and the existing house renderer."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artwork/iran-city-maps-2026-09-10"
FROZEN = ROOT / "artwork/production-maps-2026-09-06/reproduction"
sys.path[:0] = [str(FROZEN / "renderer/src"), str(ROOT / "tools"), str(FROZEN / "tools")]

from PIL import Image  # noqa: E402
from city_map_plotter.production_copy import clean_customer_copy, geometry_digest  # noqa: E402
from city_map_plotter.production_header import HEADER_IDS, refresh_city_header  # noqa: E402
from build_production_map_release import (  # noqa: E402
    _refresh_layer_counts, pen_splits, record, sha, write_json,
)
from plotjob import compile_plot_job, load_device_profile, verify_plot_job, write_plot_job  # noqa: E402
from validate_format import validate  # noqa: E402

PROFILE = ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"
RELEASE_URL = "https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator/releases"
TAG = "tehran-karaj-a3-2026-09-10"


def check_inputs():
    recipe = json.loads((PACKAGE / "reproduction/recipe.json").read_text())
    for item in recipe["inputs"]:
        path = ROOT / item["path"]
        if sha(path) != item["sha256"]:
            raise ValueError(f"Pinned input changed: {path}")
    return recipe


def render(row, recipe, raw_dir):
    svg = raw_dir / row["id"] / f"{row['id']}-a3-portrait.svg"
    svg.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(PYTHONPATH=str(FROZEN / "renderer/src"), PYTHONDONTWRITEBYTECODE="1",
               SOURCE_DATE_EPOCH=str(recipe["source_date_epoch"]))
    command = [sys.executable, "-B", "-m", "city_map_plotter", "export",
               "--center", *map(str, row["center"]), "--radius-km", str(row["radius_km"]),
               "--input-pbf", str(PACKAGE / f"reproduction/sources/{row['id']}.osm.pbf"),
               "--style", str(FROZEN / "styles/university-memorabilia-v2.json"),
               "--title", row["title"], "--output", str(svg), *recipe["common_arguments"]]
    with (svg.parent / "render.log").open("w") as log:
        subprocess.run(command, env=env, cwd=ROOT, check=True, stdout=log, stderr=subprocess.STDOUT)
    return svg


def finish(row, source, output):
    folder = output / row["id"]
    folder.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(source.with_suffix(".plot.json").read_text())
    source_pbf = PACKAGE / f"reproduction/sources/{row['id']}.osm.pbf"
    if manifest["source"]["provenance"]["content_sha256"] != sha(source_pbf):
        raise ValueError("Raw artwork does not match the pinned city extract")
    root = ET.parse(source).getroot()
    before = geometry_digest(root, exclude_furniture=True, exclude_group_ids=HEADER_IDS)
    header = refresh_city_header(root, manifest)
    copy_report = clean_customer_copy(root, manifest)
    if before != geometry_digest(root, exclude_furniture=True, exclude_group_ids=HEADER_IDS):
        raise ValueError("Presentation changed geographic linework")
    header["map_geometry_sha256"] = before
    _refresh_layer_counts(root, manifest)
    svg = folder / source.name
    ET.ElementTree(root).write(svg, encoding="utf-8", xml_declaration=True)
    raw_profile, machine, _ = load_device_profile(PROFILE)
    job = compile_plot_job(svg, machine, strict_svg=True, order="optimised",
                           profile_binding={"id": raw_profile["id"], "sha256": sha(PROFILE)})
    verify_plot_job(job)
    if any(x["severity"] == "error" for x in job["safety"]["findings"]):
        raise ValueError("Machine geometry check failed")
    job_path = svg.with_suffix(".plotjob.json")
    write_plot_job(job_path, job)
    pens = pen_splits(root, svg, output, job)
    png, pdf = svg.with_suffix(".png"), svg.with_suffix(".pdf")
    subprocess.run(["inkscape", str(svg), "--export-type=png", "--export-area-page",
                    "--export-dpi=254", "--export-background=white", "--export-background-opacity=255",
                    f"--export-filename={png}"], check=True, capture_output=True)
    subprocess.run(["inkscape", str(svg), "--export-type=pdf", "--export-area-page",
                    f"--export-filename={pdf}"], check=True, capture_output=True)
    thumbnail = folder / "preview.png"
    with Image.open(png) as im:
        if im.size != (2970, 4200):
            raise ValueError("A3 raster dimensions changed")
        im.thumbnail((850, 1202), Image.Resampling.LANCZOS)
        im.convert("RGB").save(thumbnail)
    manifest["outputs"] = {"svg": record(svg, output), "png": record(png, output),
                           "pdf": record(pdf, output), "plot_job": record(job_path, output), "pen_files": pens}
    manifest["source"]["provenance"]["path"] = f"reproduction/sources/{row['id']}.osm.pbf"
    manifest["pen_files"] = pens
    manifest["plot_summary"] = {"stroke_count": job["geometry"]["stroke_count"],
                                "vertex_count": job["geometry"]["vertex_count"],
                                "pen_loads": len(pens), "motion_stats": job["stats"],
                                "basis": "Final serialized plot job"}
    manifest["pen_sequence"] = [
        {"step": i, "pen_id": g["pen"]["id"], "nib_mm": g["pen"]["nib_mm"],
         "path_count": len(g["strokes"]), "pen_down_mm": sum(s["length_mm"] for s in g["strokes"])}
        for i, g in enumerate(job["pen_groups"], 1)]
    manifest["city_edition"] = {**row, "source_snapshot": "2026-09-09T20:21:20Z",
                                "map_geometry_sha256": before}
    manifest["digital_release"] = {"id": PACKAGE.name, "software_import_ready": True,
                                   "physical_execution_allowed": False}
    manifest_path = svg.with_suffix(".plot.json")
    write_json(manifest_path, manifest)
    spec = json.loads((FROZEN / "renderer/src/city_map_plotter/data/format-v1.json").read_text())
    report = validate(svg, spec, None)
    if not report.passed:
        write_json(folder / "format-failures.json", report.failures)
        raise ValueError(f"{row['id']} format: {report.failures[:8]}")
    result = {**row, "svg": record(svg, output), "png": record(png, output),
              "pdf": record(pdf, output), "preview": record(thumbnail, output),
              "manifest": record(manifest_path, output), "job": record(job_path, output),
              "pen_files": pens, "visible_copy": copy_report["final_visible_copy"],
              "extent_wgs84": manifest["extent_wgs84"], "stroke_count": job["geometry"]["stroke_count"],
              "qa": {"format": "passed", "format_checks": report.checks,
                     "strict_svg": "passed", "pen_geometry_parity": "passed",
                     "map_geometry_unchanged": True, "density_advisories": report.advisories}}
    write_json(folder / "QA.json", result["qa"])
    print(f"Finished {row['title']}: {result['stroke_count']} strokes, {len(pens)} pens", flush=True)
    return result


def package(output, rows, downloads):
    write_json(output / "catalog.json", {"schema_version": 1, "release_id": PACKAGE.name,
               "title": "Tehran and Karaj", "paper": "A3 portrait / 297 x 420 mm",
               "style": "university-memorabilia-v2", "detail_profile": "plotter-faithful",
               "download_release": f"{RELEASE_URL}/tag/{TAG}", "cities": rows})
    images = [Image.open(output / r["preview"]["path"]).convert("RGB") for r in rows]
    sheet = Image.new("RGB", (sum(im.width for im in images) + 90, max(im.height for im in images) + 60), "#eeede9")
    x = 30
    for im in images:
        sheet.paste(im, (x, 30))
        x += im.width + 30
    sheet.save(output / "comparison.png")
    links, cards = [], []
    for row in rows:
        download = f"{RELEASE_URL}/download/{TAG}/{row['id']}-a3-pen-files.zip"
        links.append(f"| {row['title'].title()} | [Download ZIP]({download}) | [SVG]({row['svg']['path']}) | [PNG]({row['png']['path']}) | [PDF]({row['pdf']['path']}) |")
        cards.append(f'<article><a href="{row["png"]["path"]}"><img src="{row["preview"]["path"]}" alt="{row["title"]} A3 city map"></a><h2>{row["title"]}</h2><p>{row["scope"]}</p><a href="{download}">Download pen files</a> · <a href="{row["pdf"]["path"]}">PDF</a></article>')
    (output / "README.md").write_text("# Tehran and Karaj — A3 city maps\n\nTwo separate A3 portrait prints in the existing city/university style: serif city name, coordinates and diamond compass above the map, double black border, coloured roads, blue waterways, green parks and purple landmarks.\n\nThese are detailed central-city compositions; their exact framing is recorded in `catalog.json`. Full qualifying streets, service roads, paths and railways use the house `plotter-faithful` recipe, 0.04 mm simplification, centreline roads and dotted water.\n\n![Both city prints](comparison.png)\n\n| City | Pen plotting download | Master | Preview | Print PDF |\n|---|---|---|---|---|\n" + "\n".join(links) + "\n\n[Import guide](SOFTWARE_IMPORT.md) · [Animated pen preview](simulation/cities.html) · [Catalogue](catalog.json) · [Source credits](ATTRIBUTION.md)\n\nThe downloads contain real SVG files and ordered pen layers. Print/import at **100%, A3 portrait, 297 × 420 mm**. PNGs are 254 DPI. The map source snapshot is 9 September 2026; exact extracts and hashes are included under `reproduction/`.\n\nRebuild into a new directory using Python 3.13, the existing production reproduction requirements, Pillow and Inkscape:\n\n```bash\npython scripts/build_iran_city_maps.py --output build/iran-city-rebuild\npython scripts/verify_iran_city_maps.py --release build/iran-city-rebuild\n```\n\nDigital geometry and nominal simulation are verified separately from a physical pen/paper proof.\n")
    (output / "SOFTWARE_IMPORT.md").write_text("# Import Tehran or Karaj\n\n1. Download and unzip the city ZIP.\n2. Use Load SVG / Import SVG in the plotting software and choose `<city>-a3-portrait.svg`.\n3. Keep A3 portrait, 297 × 420 mm, at 100% / actual size.\n4. Use the SVG's ink and nib assignments. Alternatively load the numbered SVGs in `pens/` in order, preserving their common page origin and size.\n\nThe PDF and PNG are print/viewing previews. The master and per-pen SVGs are stroke-only vectors with the full header and border included. Use your calibrated machine profile when compiling a hardware job; the bundled plot job and animation use a nominal simulation profile.\n\nIf cloning the repository, run `git lfs pull --include=\"artwork/iran-city-maps-2026-09-10/**\"` to retrieve real files. Source credits accompany the download in ATTRIBUTION.md.\n")
    (output / "index.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tehran and Karaj — The Plot Room</title><style>body{margin:30px auto;padding:0 24px;max-width:1300px;background:#eeede9;color:#26333d;font:16px system-ui}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:30px}img{width:100%}a{color:inherit}p{line-height:1.6}</style><h1>Tehran &amp; Karaj</h1><p>Two separate A3 city prints in the house colours.</p><main>' + ''.join(cards) + '</main><p><a href="SOFTWARE_IMPORT.md">Import guide</a> · <a href="simulation/cities.html">Animated pen preview</a> · <a href="ATTRIBUTION.md">Source credits</a></p></html>')
    (output / "simulation").mkdir(exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "tools/build_plotsim_viewer.py"),
                    *[str(output / r["svg"]["path"]) for r in rows], "--strict-svg",
                    "--machine-profile", str(PROFILE), "--out", str(output / "simulation/cities.html")],
                   check=True, capture_output=True)
    files = sorted(p for p in output.rglob("*") if p.is_file() and p.name != "CHECKSUMS.sha256")
    (output / "CHECKSUMS.sha256").write_text(''.join(f"{sha(p)}  {p.relative_to(output).as_posix()}\n" for p in files))
    downloads.mkdir(parents=True, exist_ok=True)
    archives = []
    for row in rows:
        paths = sorted((output / row["id"]).rglob("*"))
        paths = [p for p in paths if p.is_file()] + [output / "SOFTWARE_IMPORT.md", output / "ATTRIBUTION.md"]
        archive = downloads / f"{row['id']}-a3-pen-files.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p in paths:
                z.write(p, p.relative_to(output).as_posix())
            z.writestr("CHECKSUMS.sha256", ''.join(f"{sha(p)}  {p.relative_to(output).as_posix()}\n" for p in paths))
        with zipfile.ZipFile(archive) as z:
            if z.testzip() is not None:
                raise ValueError("Invalid plotting archive")
            for p in paths:
                if z.read(p.relative_to(output).as_posix()) != p.read_bytes():
                    raise ValueError("Archive differs from published artwork")
        archives.append(archive)
    (downloads / "SHA256SUMS.txt").write_text(''.join(f"{sha(p)}  {p.name}\n" for p in archives))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-raw", type=Path, help="Reuse this edition's already rendered raw SVGs")
    parser.add_argument("--downloads", type=Path, default=ROOT / "build/iran-city-downloads-2026-09-10")
    args = parser.parse_args()
    output = args.output.resolve()
    if (output / "catalog.json").exists():
        raise SystemExit("Use a new output directory to preserve the released edition.")
    recipe = check_inputs()
    if output != PACKAGE:
        shutil.copytree(PACKAGE / "reproduction", output / "reproduction")
        shutil.copyfile(PACKAGE / "ATTRIBUTION.md", output / "ATTRIBUTION.md")
    rows = json.loads((PACKAGE / "reproduction/cities.json").read_text())
    raw_dir = args.reuse_raw or output / ".intermediates"
    def build(row):
        source = (raw_dir / row["id"] / f"{row['id']}-a3-portrait.svg") if args.reuse_raw else render(row, recipe, raw_dir)
        return finish(row, source, output)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finished = list(pool.map(build, rows))
    if not args.reuse_raw:
        shutil.rmtree(raw_dir)
    package(output, finished, args.downloads)
    print(f"Packaged two city maps: {output}")


if __name__ == "__main__":
    main()
