#!/usr/bin/env python3
"""Audit the 44 city-style stadium masters, geographic detail, source hashes and pen jobs."""

# ruff: noqa: E402
from __future__ import annotations
from collections import Counter
from pathlib import Path
import json
import math
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artwork/uk-stadiums-city-style-2026-09-08"
ORIGINAL = ROOT / "artwork/uk-stadiums-overhead-2026-09-08"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools"), str(ROOT / "scripts")]
from city_map_plotter.production_header import verify_city_header, _bounds
from plotjob import verify_plot_job
from plotsim import preflight_svg
from verify_uk_stadiums import checked_path, digest, Links

SVG = "{http://www.w3.org/2000/svg}"
TOKEN = re.compile(r"[MLCZ]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def verify_club_header(root, manifest, club_name):
    clubs = [g for g in root.iter() if g.get("id") == "layer-poster_club"]
    if len(clubs) != 1 or clubs[0] not in list(root):
        raise ValueError("Football club header is missing or duplicated")
    club = clubs[0]
    if (
        club.get("data-copy") != club_name.upper()
        or manifest["stadium_city_header"]["club"] != club_name
    ):
        raise ValueError("Football club header copy differs from the catalogue")
    title = root.find(f"{SVG}g[@id='layer-poster_title']")
    coords = root.find(f"{SVG}g[@id='layer-poster_coordinates']")
    t, b, c = map(_bounds, (title, club, coords))
    tn, bn, cn = (float(g.get("data-plot-nib-mm")) for g in (title, club, coords))
    if abs(b[0] - t[0]) > 0.003 or abs(b[0] - c[0]) > 0.003:
        raise ValueError("Football club must share the header's left edge")
    if b[1] - bn / 2 - (t[3] + tn / 2) < bn or c[1] - cn / 2 - (b[3] + bn / 2) < bn:
        raise ValueError(
            "Football club must sit clearly between ground name and city/coordinates"
        )
    if float(club.get("data-cap-height-mm")) < 8 * bn:
        raise ValueError("Football club lettering is below its physical type floor")
    zone = manifest["page"]["zones_mm"]["city_club"]
    if (
        b[0] < zone["x"]
        or b[2] + bn / 2 > zone["x"] + zone["width"]
        or b[1] - bn / 2 < zone["y"]
        or b[3] + bn / 2 > zone["y"] + zone["height"]
    ):
        raise ValueError("Football club leaves its header zone")
    compass = root.find(f"{SVG}g[@id='layer-poster_compass']")
    if (
        b[2] + bn / 2
        >= _bounds(compass)[0] - float(compass.get("data-plot-nib-mm")) / 2
    ):
        raise ValueError("Football club overlaps the compass")
    return {"status": "passed", "club": club_name, "bounds_mm": list(b)}


def verify_native_geometry(root, overlay, manifest):
    """Independently regenerate every geographic endpoint/control point; do not trust path counts."""
    ref = overlay["georeference"]
    a = math.radians(ref["x_axis_degrees_from_east"])
    lat = ref["latitude"]
    lon = ref["longitude"]
    R = 6371008.8
    bbox = manifest["extent_wgs84"]
    field = manifest["page"]["map_bounds_mm"]

    def point(x, y):
        east = x * math.cos(a) - y * math.sin(a)
        north = x * math.sin(a) + y * math.cos(a)
        lng = lon + math.degrees(east / (R * math.cos(math.radians(lat))))
        lt = lat + math.degrees(north / R)
        return [
            field["x"]
            + (lng - bbox["west"]) / (bbox["east"] - bbox["west"]) * field["width"],
            field["y"]
            + (bbox["north"] - lt) / (bbox["north"] - bbox["south"]) * field["height"],
        ]

    paths = [p for p in root.iter(SVG + "path") if p.get("data-stadium-path-id")]
    actual = {p.get("data-stadium-path-id"): p for p in paths}
    sources = overlay["shell_paths"] + overlay["paths"]
    if len(paths) != len(actual) or set(actual) != {p["id"] for p in sources}:
        raise ValueError("Stadium detail path identity/coverage changed")
    maximum = 0.0
    curves = 0
    for source in sources:
        commands = source.get("commands_m")
        if commands:
            expected = []
            for c in commands:
                expected.append(c[0])
                for i in range(1, len(c), 2):
                    expected.extend(point(c[i], c[i + 1]))
            curves += any(c[0] == "C" for c in commands)
        else:
            expected = []
            for i, (x, y) in enumerate(source["points_m"]):
                expected.extend(["M" if i == 0 else "L", *point(x, y)])
            if source.get("closed"):
                expected.append("Z")
        tokens = TOKEN.findall(actual[source["id"]].get("d", ""))
        if len(tokens) != len(expected):
            raise ValueError("Stadium native command count changed: " + source["id"])
        for value, want in zip(tokens, expected):
            if isinstance(want, str):
                if value != want:
                    raise ValueError(
                        "Stadium native curve command changed: " + source["id"]
                    )
            else:
                error = abs(float(value) - want)
                maximum = max(maximum, error)
                if error > 0.001:
                    raise ValueError(
                        "Stadium native control point moved: " + source["id"]
                    )
    return {
        "paths": len(sources),
        "curved_paths": curves,
        "maximum_geographic_coordinate_error_mm": maximum,
    }


def verify(package=PACKAGE):
    catalog = json.loads(checked_path(package, "catalog.json").read_text())
    rows = catalog["stadiums"]
    if len(rows) != 44 or Counter(r["league"] for r in rows) != {
        "premier_league": 20,
        "championship": 24,
    }:
        raise ValueError("Stadium roster changed")
    originals = {
        s["id"]: s
        for s in json.loads((ORIGINAL / "catalog.json").read_text())["stadiums"]
    }
    if {r["id"] for r in rows} != set(originals):
        raise ValueError("Stadium identities differ from the source roster")
    inputs = json.loads((package / "reproduction/INPUTS.json").read_text())
    if inputs["original_stadium_catalog_sha256"] != digest(ORIGINAL / "catalog.json"):
        raise ValueError("Source stadium catalogue binding changed")
    for item in inputs["renderer_and_style"]:
        if digest(checked_path(ROOT, item["path"])) != item["sha256"]:
            raise ValueError("Frozen renderer or house style changed: " + item["path"])
    if len(inputs["sources"]) != len(rows):
        raise ValueError("Incomplete per-stadium source ledger")
    for item in inputs["sources"]:
        source_path = checked_path(package, item["path"])
        if (
            digest(source_path) != item["sha256"]
            or source_path.stat().st_size != item["bytes"]
        ):
            raise ValueError("Source ledger mismatch: " + item["path"])
    reports = []
    pens_total = 0
    for row in rows:
        paths = {}
        for role, r in row["files"].items():
            p = checked_path(package, r["path"])
            if digest(p) != r["sha256"] or p.stat().st_size != r["bytes"]:
                raise ValueError("Artifact hash mismatch: " + str(p))
            paths[role] = p
        m = json.loads(paths["manifest"].read_text())
        root = ET.parse(paths["svg"]).getroot()
        if root.get("width") != "297mm" or root.get("height") != "420mm":
            raise ValueError("Wrong A3 page")
        if digest(paths["svg"]) != m["svg_sha256"]:
            raise ValueError("Manifest source SVG binding changed")
        verify_city_header(root, m)
        verify_club_header(root, m, row["club"])
        coordinates = root.find(f"{SVG}g[@id='layer-poster_coordinates']").get(
            "data-copy"
        )
        if not coordinates.startswith(row["city"].upper() + " / "):
            raise ValueError("City is missing from header")
        source = originals[row["id"]]
        op = ORIGINAL / source["files"]["stadium_overlay"]["path"]
        if digest(op) != source["files"]["stadium_overlay"]["sha256"]:
            raise ValueError("Original stadium source changed")
        native = verify_native_geometry(root, json.loads(op.read_text()), m)
        extent = m["extent_wgs84"]
        previous = m["stadium_composition"]["previous_city_extent_wgs84"]
        for lo, hi in [("west", "east"), ("south", "north")]:
            ratio = (extent[hi] - extent[lo]) / (previous[hi] - previous[lo])
            if not math.isclose(ratio, 0.8, abs_tol=1e-6):
                raise ValueError("Map does not have the requested closer framing")
            if abs((extent[lo] + extent[hi] - previous[lo] - previous[hi]) / 2) > 1e-8:
                raise ValueError("Stadium map centre moved")
        pbf = m["reproduction"]["source_pbf"]
        p = checked_path(package, pbf["path"])
        if digest(p) != pbf["sha256"]:
            raise ValueError("Map source hash changed")
        pf = preflight_svg(paths["svg"])
        if pf.errors or not pf.metadata_complete:
            raise ValueError("Master SVG preflight failed")
        job = json.loads(paths["plotjob"].read_text())
        verify_plot_job(job)
        if (
            job["source"]["sha256"] != digest(paths["svg"])
            or job["safety"]["execution_allowed"]
        ):
            raise ValueError("Plot job source/status binding changed")
        if any(f["severity"] == "error" for f in job["safety"]["findings"]):
            raise ValueError("Plot job bounds failure")
        if m["plot_summary"]["pen_down_path_count"] != pf.path_count:
            raise ValueError("Final manifest stroke count stale")
        if sum(p["path_count"] for p in m["pen_sequence"]) != pf.path_count:
            raise ValueError("Final pen sequence count stale")
        master = Counter(
            (g.get("data-plot-pen-id"), p.get("d"))
            for g in root.findall(SVG + "g")
            for p in g.iter(SVG + "path")
        )
        split = Counter()
        for record in row["pens"]:
            p = checked_path(package, record["path"])
            if digest(p) != record["sha256"]:
                raise ValueError("Pen split hash changed")
            r = ET.parse(p).getroot()
            if preflight_svg(p).errors:
                raise ValueError("Pen split preflight failed")
            split.update(
                (g.get("data-plot-pen-id"), x.get("d"))
                for g in r.findall(SVG + "g")
                for x in g.iter(SVG + "path")
            )
        if master != split:
            raise ValueError("Pen split linework differs from master")
        expected_pen_paths = {checked_path(package, r["path"]) for r in row["pens"]}
        if set((paths["svg"].parent / "pens").glob("*.svg")) != expected_pen_paths:
            raise ValueError("Pen directory contains stale or missing files")
        pens_total += len(row["pens"])
        reports.append(
            {
                "id": row["id"],
                "header": "passed",
                "native_geometry": native,
                "strokes": pf.path_count,
            }
        )
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
        h, relative = line.split("  ", 1)
        if relative in inventory or digest(checked_path(package, relative)) != h:
            raise ValueError("Bad checksum: " + relative)
        inventory[relative] = h
    actual = {
        str(p.relative_to(package))
        for p in package.rglob("*")
        if p.is_file() and p.name != "CHECKSUMS.sha256"
    }
    if actual != set(inventory):
        raise ValueError("Incomplete release checksum inventory")
    return {
        "status": "passed",
        "stadiums": len(rows),
        "club_headers": len(rows),
        "stadium_linear_enlargement_vs_previous": 1.25,
        "native_stadium_paths": sum(r["native_geometry"]["paths"] for r in reports),
        "curved_stadium_paths": sum(
            r["native_geometry"]["curved_paths"] for r in reports
        ),
        "pen_svg_files": pens_total,
        "physical_execution_allowed": False,
        "artworks": reports,
    }


if __name__ == "__main__":
    try:
        result = verify()
        print(
            json.dumps({k: v for k, v in result.items() if k != "artworks"}, indent=2)
        )
    except (ValueError, KeyError, OSError, ET.ParseError) as exc:
        raise SystemExit(str(exc))
