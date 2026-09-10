#!/usr/bin/env python3
"""Verify the two city prints, pinned inputs, physical SVGs and plotting downloads."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import struct
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artwork/iran-city-maps-2026-09-10"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools"), str(ROOT / "scripts")]
from city_map_plotter.production_copy import geometry_digest, visible_copies  # noqa: E402
from city_map_plotter.production_header import HEADER_IDS, verify_city_header  # noqa: E402
from plotjob import verify_plot_job  # noqa: E402
from plotsim import preflight_svg  # noqa: E402
from verify_uk_stadiums import checked_path, digest, Links  # noqa: E402

SVG = "{http://www.w3.org/2000/svg}"


def strokes(root):
    return Counter((g.get("data-plot-pen-id"), g.get("stroke"), g.get("stroke-width"), p.get("d"))
                   for g in root.findall(SVG + "g") for p in g.iter(SVG + "path"))


def verify(package=PACKAGE):
    catalog = json.loads(checked_path(package, "catalog.json").read_text())
    rows = catalog["cities"]
    if len(rows) != 2 or {r["id"] for r in rows} != {"tehran", "karaj"}:
        raise ValueError("Expected exactly two separate city maps")
    recipe = json.loads((package / "reproduction/recipe.json").read_text())
    for item in recipe["inputs"]:
        rel = Path(item["path"])
        path = (package / rel.relative_to(PACKAGE.relative_to(ROOT))) if rel.is_relative_to(PACKAGE.relative_to(ROOT)) else checked_path(ROOT, item["path"])
        if digest(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError("Changed reproduction input: " + item["path"])
    cities = {r["id"]: r for r in json.loads((package / "reproduction/cities.json").read_text())}
    style = json.loads((ROOT / "artwork/production-maps-2026-09-06/reproduction/styles/university-memorabilia-v2.json").read_text())["layers"]
    results = []
    for row in rows:
        for item in [row[k] for k in ["svg", "png", "pdf", "preview", "manifest", "job"]] + row["pen_files"]:
            path = checked_path(package, item["path"])
            if digest(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
                raise ValueError("Changed city artifact: " + item["path"])
        svg = package / row["svg"]["path"]
        root = ET.parse(svg).getroot()
        m = json.loads((package / row["manifest"]["path"]).read_text())
        if root.get("width") != "297mm" or root.get("height") != "420mm":
            raise ValueError("Master must be A3 portrait in millimetres")
        verify_city_header(root, m)
        if visible_copies(root, m) != row["visible_copy"] or m["title"] != row["title"]:
            raise ValueError("City header copy changed")
        map_hash = geometry_digest(root, exclude_furniture=True, exclude_group_ids=HEADER_IDS)
        if map_hash != m["city_edition"]["map_geometry_sha256"]:
            raise ValueError("Map geometry changed after rendering")
        rendering = m["rendering"]
        for key, expected in {"detail_profile": "plotter-faithful", "road_style": "centreline",
                              "simplify_tolerance_mm": 0.04, "water_fill": "dots", "pen_profile": "actual-pens"}.items():
            if rendering[key] != expected:
                raise ValueError("House rendering recipe changed: " + key)
        if set(m["families"]) != {"roads", "water", "railways", "parks", "buildings"}:
            raise ValueError("A city feature family is missing")
        for layer, expected in style.items():
            group = root.find(f"{SVG}g[@id='layer-{layer}']")
            if group is None or group.get("stroke") != expected["stroke"]:
                raise ValueError("Missing or recoloured house layer: " + layer)
        provenance = m["source"]["provenance"]
        if provenance["content_sha256"] != digest(package / f"reproduction/sources/{row['id']}.osm.pbf"):
            raise ValueError("City source binding changed")
        if m["source"]["timestamp"] != "2026-09-09T20:21:20Z":
            raise ValueError("City source timestamp changed")
        canonical = provenance["canonical_features"]
        if any(canonical[k] for k in ["failed_way_area_assembly_count", "failed_relation_area_assembly_count", "invalid_geometry_count"]):
            raise ValueError("Source geometry assembly failure")
        expected = cities[row["id"]]
        extent = m["extent_wgs84"]
        if extent != row["extent_wgs84"]:
            raise ValueError("Catalogue framing changed")
        lat = (extent["north"] + extent["south"]) / 2
        lon = (extent["east"] + extent["west"]) / 2
        if not math.isclose(lat, expected["center"][0], abs_tol=1e-9) or not math.isclose(lon, expected["center"][1], abs_tol=1e-9):
            raise ValueError("Map centre changed")
        radius = math.radians(extent["north"] - extent["south"]) * 6371.0088 / 2
        if not math.isclose(radius, expected["radius_km"], abs_tol=0.001):
            raise ValueError("Map zoom changed")
        w, s, e, n = expected["extract_bbox_wsen"]
        acquired = provenance["extraction"]["bbox_wgs84"]
        if not (w <= acquired["west"] < acquired["east"] <= e and s <= acquired["south"] < acquired["north"] <= n):
            raise ValueError("Saved extract does not cover the full acquisition extent")
        pf = preflight_svg(svg)
        if pf.errors or not pf.metadata_complete:
            raise ValueError("Strict master preflight failed")
        job = json.loads((package / row["job"]["path"]).read_text())
        verify_plot_job(job)
        if job["source"]["sha256"] != digest(svg) or job["safety"]["execution_allowed"]:
            raise ValueError("Plot job source/profile mismatch")
        if job["geometry"]["stroke_count"] != row["stroke_count"] or pf.path_count != row["stroke_count"]:
            raise ValueError("Plot stroke counts disagree")
        split_strokes = Counter()
        for i, item in enumerate(row["pen_files"], 1):
            path = package / item["path"]
            pen = ET.parse(path).getroot()
            if pen.get("viewBox") != root.get("viewBox") or preflight_svg(path).errors:
                raise ValueError("Pen registration or preflight failure")
            if item["step"] != i or item["pen_id"] != job["pen_groups"][i-1]["pen"]["id"]:
                raise ValueError("Pen loading order mismatch")
            split_strokes.update(strokes(pen))
        if split_strokes != strokes(root):
            raise ValueError("Pen splits lose or duplicate master geometry")
        if set((svg.parent / "pens").glob("*.svg")) != {package / r["path"] for r in row["pen_files"]}:
            raise ValueError("Stale or missing per-pen SVG")
        with (package / row["png"]["path"]).open("rb") as f:
            head = f.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", head[16:24]) != (2970, 4200):
            raise ValueError("PNG is not A3 at 254 DPI")
        results.append({"city": row["id"], "strokes": row["stroke_count"], "pen_files": len(row["pen_files"]),
                        "source_coverage": "extract contains full acquisition bounds"})
    links = Links()
    links.feed((package / "index.html").read_text())
    for link in links.links:
        if not link.startswith(("https://", "http://", "#")):
            checked_path(package, link)
    indexed = set()
    for line in (package / "CHECKSUMS.sha256").read_text().splitlines():
        h, rel = line.split("  ", 1)
        if rel in indexed or digest(checked_path(package, rel)) != h:
            raise ValueError("Bad checksum: " + rel)
        indexed.add(rel)
    actual = {p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file() and p.name != "CHECKSUMS.sha256"}
    if actual != indexed:
        raise ValueError("Incomplete checksum inventory")
    return {"status": "passed", "maps": 2, "checksummed_files": len(indexed), "cities": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=PACKAGE)
    args = parser.parse_args()
    print(json.dumps(verify(args.release.resolve()), indent=2))
