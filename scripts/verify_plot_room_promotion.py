#!/usr/bin/env python3
"""Verify the map colours, original stencil geometry and website on all promotional prints."""

# ruff: noqa: E402
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import struct
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artwork/the-plot-room-promotional-a3"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools"), str(ROOT / "scripts")]
from city_map_plotter.production_header import _bounds
from plotsim import preflight_svg
from verify_uk_stadiums import checked_path, digest, Links

S = "{http://www.w3.org/2000/svg}"
WEBSITE = "theplotroom.com"


def geometry(root, include_website=False):
    return Counter(
        (g.get("id"), p.get("d"))
        for g in root.findall(S + "g")
        for p in g.iter(S + "path")
        if include_website or g.get("id") != "layer-website"
    )


def verify_artwork(root, source, palette_root, treatment):
    if geometry(root) != geometry(source):
        raise ValueError("Original map or letter outline geometry changed")
    groups = root.findall(S + "g")
    websites = [g for g in groups if g.get("id") == "layer-website"]
    if len(websites) != 1 or websites[0].get("data-copy") != WEBSITE:
        raise ValueError("Website is missing or incorrect")
    website = websites[0]
    b = _bounds(website)
    width, height = float(root.get("width")[:-2]), float(root.get("height")[:-2])
    outline = next(g for g in groups if g.get("id") == "layer-letter-outlines")
    if b[1] - 0.2 <= _bounds(outline)[3] + float(outline.get("stroke-width")) / 2:
        raise ValueError("Website must sit underneath the lettering")
    if (
        abs((b[0] + b[2]) / 2 - width / 2) > 0.003
        or b[0] - 0.2 < 24
        or b[2] + 0.2 > width - 24
        or b[3] + 0.2 > height - 24
    ):
        raise ValueError("Website is not centred within the page margins")
    palette = {g.get("id"): g for g in palette_root.findall(S + "g")}
    for g in groups:
        if treatment == "blueprint":
            if g.get("stroke") != "#f7f6ee" or g.get("data-plot-ink") != "White":
                raise ValueError("Blueprint has a non-white drawing layer")
        elif g.get("id") in {"layer-letter-outlines", "layer-website"}:
            if g.get("stroke") != "#18181b" or g.get("data-plot-ink") != "Black":
                raise ValueError("Letter outlines and website must use black")
        else:
            original = palette[g.get("id")]
            for attr in ("stroke", "stroke-width", "data-plot-ink", "data-plot-pen-id"):
                if g.get(attr) != original.get(attr):
                    raise ValueError("Original city-map colour or pen changed")
        if any(p.get("stroke") or p.get("data-plot-ink") for p in g.iter(S + "path")):
            raise ValueError("A path overrides its layer's intended ink")
    return {"website": "passed", "map_geometry": "passed", "colours": "passed"}


def verify(package=PACKAGE):
    catalog = json.loads((package / "catalog.json").read_text())
    rows = catalog["exports"]
    if len(rows) != 8 or len({(r["id"], r["treatment"]) for r in rows}) != 8:
        raise ValueError("Expected eight distinct promotional exports")
    for row in rows:
        paths = {}
        for key, item in row["files"].items():
            p = checked_path(package, item["path"])
            if digest(p) != item["sha256"] or p.stat().st_size != item["bytes"]:
                raise ValueError("Artifact hash mismatch: " + item["path"])
            paths[key] = p
        qa = json.loads(paths["qa"].read_text())
        original = checked_path(package, qa["input"]["path"])
        palette = checked_path(ROOT, qa["palette_source"]["path"])
        if (
            digest(original) != qa["input"]["sha256"]
            or digest(palette) != qa["palette_source"]["sha256"]
        ):
            raise ValueError("Source artwork binding changed")
        root = ET.parse(paths["plot_svg"]).getroot()
        verify_artwork(
            root,
            ET.parse(original).getroot(),
            ET.parse(palette).getroot(),
            row["treatment"],
        )
        pf = preflight_svg(paths["plot_svg"])
        if pf.errors or not pf.metadata_complete:
            raise ValueError("Plot SVG preflight failed")
        display = ET.parse(paths["svg"]).getroot()
        if geometry(display, True) != geometry(root, True):
            raise ValueError("Display and plot linework differ")
        paper = display.find(f"{S}rect[@id='preview-paper-background']")
        if row["treatment"] == "blueprint" and (
            paper is None or paper.get("fill") != "#123b63"
        ):
            raise ValueError("Blueprint paper preview is missing")
        dims = (float(root.get("width")[:-2]), float(root.get("height")[:-2]))
        if dims not in [(297, 420), (420, 297)]:
            raise ValueError("Not an A3 plate")
        expected = (3508, 4961) if dims[0] < dims[1] else (4961, 3508)
        with paths["png"].open("rb") as f:
            png_header = f.read(24)
        if (
            png_header[:8] != b"\x89PNG\r\n\x1a\n"
            or struct.unpack(">II", png_header[16:24]) != expected
        ):
            raise ValueError("Incorrect 300 DPI PNG dimensions")
        master = Counter(
            (g.get("data-plot-pen-id"), p.get("d"))
            for g in root.findall(S + "g")
            for p in g.iter(S + "path")
        )
        splits = Counter()
        for item in row["pens"]:
            p = checked_path(package, item["path"])
            if digest(p) != item["sha256"] or preflight_svg(p).errors:
                raise ValueError("Pen export changed or failed preflight")
            r = ET.parse(p).getroot()
            splits.update(
                (g.get("data-plot-pen-id"), x.get("d"))
                for g in r.findall(S + "g")
                for x in g.iter(S + "path")
            )
        if master != splits:
            raise ValueError("Per-pen artwork differs from master")
    links = Links()
    links.feed((package / "index.html").read_text())
    for link in links.links:
        if (
            not link.startswith(("https://", "http://", "#"))
            and not (package / link).is_file()
        ):
            raise ValueError("Broken gallery link: " + link)
    inventory = {}
    for line in (package / "CHECKSUMS.sha256").read_text().splitlines():
        value, relative = line.split("  ", 1)
        if relative in inventory or digest(checked_path(package, relative)) != value:
            raise ValueError("Checksum mismatch: " + relative)
        inventory[relative] = value
    actual = {
        str(p.relative_to(package))
        for p in package.rglob("*")
        if p.is_file() and p.name != "CHECKSUMS.sha256"
    }
    if actual != set(inventory):
        raise ValueError("Incomplete promotional package inventory")
    return {
        "status": "passed",
        "promotional_prints": len(rows),
        "website": WEBSITE,
        "original_map_colours": "preserved",
        "original_stencil_geometry": "preserved",
    }


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
