#!/usr/bin/env python3
"""Rebuild all 44 stadium plates with the frozen city renderer and native stadium detail."""

# ruff: noqa: E402
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "artwork/production-maps-2026-09-06/reproduction"
SOURCE = ROOT / "artwork/uk-stadiums-overhead-2026-09-08"
DEST = ROOT / "artwork/uk-stadiums-city-style-2026-09-08"
WORK = ROOT / "build/stadium-house-work"
sys.path[:0] = [
    str(FROZEN / "renderer/src"),
    str(ROOT / "tools"),
    str(FROZEN / "tools"),
]
from shapely.geometry import Polygon, LineString, box
from shapely.ops import unary_union
from city_map_plotter.geometry import make_poster_layout
from city_map_plotter.models import BoundingBox
from city_map_plotter.stroke_font import stroke_text, text_width_mm
from city_map_plotter.svgkit import append_vector_strokes
from city_map_plotter.furniture import stroke_geometry_sha256, reliable_vector_strokes
from city_map_plotter.production_header import verify_city_header, _bounds
from plotsim import flatten_path, preflight_svg
from plotjob import compile_plot_job, load_device_profile, write_plot_job
from validate_format import validate, _parse_path

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"
for pre, uri in [
    ("", SVG),
    ("inkscape", INK),
    ("sodipodi", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"),
]:
    ET.register_namespace(pre, uri)


def S(x):
    return f"{{{SVG}}}{x}"


ROWS = json.loads((DEST / "reproduction/stadiums.json").read_text())
SPEC = json.loads(
    (FROZEN / "renderer/src/city_map_plotter/data/format-v1.json").read_text()
)
PROFILE = ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def dump(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def save(r, p):
    ET.ElementTree(r).write(p, encoding="utf-8", xml_declaration=True)


def record(p):
    return {
        "path": str(p.relative_to(DEST)),
        "sha256": sha(p),
        "bytes": p.stat().st_size,
    }


def parts(g):
    if g.is_empty:
        return
    if g.geom_type in ["LineString", "LinearRing"]:
        yield g
    elif hasattr(g, "geoms"):
        for v in g.geoms:
            yield from parts(v)


def dline(g):
    return "M " + " L ".join(f"{x:.5f},{y:.5f}" for x, y in g.coords)


def group(gid, label, ink, nib, col):
    return ET.Element(
        S("g"),
        {
            "id": gid,
            f"{{{INK}}}groupmode": "layer",
            f"{{{INK}}}label": label,
            "fill": "none",
            "stroke": col,
            "stroke-width": str(nib),
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
            "data-plot-ink": ink,
            "data-plot-pen-id": f"{ink.lower()}-{nib:g}".replace(".", "-"),
            "data-plot-nib-mm": str(nib),
            "data-plot-nominal-nib-mm": str(nib),
            "data-plot-strokes": "1",
            "data-plot-passes": "1",
            "data-plot-width-mm": str(nib),
            "data-plot-requested-width-mm": str(nib),
            "data-plot-width-fit-error-mm": "0",
            "data-plot-offset-pitch-mm": "0",
            "data-plot-width-fit-mode": "single-nib",
            "data-plot-calibration-state": "nominal-unmeasured",
            "data-plot-pen-profile": "actual-pens",
        },
    )


def city_header(root, m, row):
    g = root.find(f"{S('g')}[@id='layer-poster_coordinates']")
    b = _bounds(g)
    copy_text = row["city"].upper() + " / " + g.get("data-copy")
    cap = float(g.get("data-cap-height-mm"))
    zone = m["page"]["zones_mm"]["city_coordinates"]
    available = zone["x"] + zone["width"] - b[0]
    if text_width_mm(copy_text, cap_height_mm=cap) > available:
        raise ValueError("City/coordinates exceed header: " + row["id"])
    strokes = reliable_vector_strokes(
        stroke_text(copy_text, x_mm=b[0], y_mm=b[1], height_mm=cap, anchor="start"),
        nib_mm=0.25,
    )
    for e in list(g):
        g.remove(e)
    append_vector_strokes(g, strokes)
    g.set("data-copy", copy_text)
    g.set("data-coordinate-line-copy-json", json.dumps([copy_text]))
    g.set("data-copy-geometry-sha256", stroke_geometry_sha256(strokes))
    m["city_map"]["coordinates"] = copy_text
    m["city_map"]["visible_copy"] = [m["title"], copy_text, "N"]
    m["stadium_city_header"] = {
        "stadium": row["name"],
        "city": row["city"],
        "coordinates_source": row["georeference"],
        "copy": copy_text,
    }
    m["header_layout"] = verify_city_header(root, m)


def native_paths(overlay, layout):
    ref = overlay["georeference"]
    a = math.radians(ref["x_axis_degrees_from_east"])
    lat = ref["latitude"]
    lon = ref["longitude"]
    R = 6371008.8

    def project(x, y):
        east = x * math.cos(a) - y * math.sin(a)
        north = x * math.sin(a) + y * math.cos(a)
        return layout.project_to_page(
            lat + math.degrees(north / R),
            lon + math.degrees(east / (R * math.cos(math.radians(lat)))),
        )

    result = []
    polygons = []
    for family in ["shell_paths", "paths"]:
        for p in overlay[family]:
            commands = p.get("commands_m")
            if commands:
                bits = []
                for cmd in commands:
                    assert cmd[0] in ["M", "L", "C", "Z"]
                    nums = [
                        v
                        for i in range(1, len(cmd), 2)
                        for v in project(cmd[i], cmd[i + 1])
                    ]
                    bits.append(
                        cmd[0]
                        + (" " + " ".join(f"{v:.6f}" for v in nums) if nums else "")
                    )
                d = " ".join(bits)
            else:
                coords = [project(x, y) for x, y in p["points_m"]]
                d = dline(LineString(coords)) + (" Z" if p.get("closed") else "")
            result.append((family, p["id"], d, commands is not None))
            if family == "shell_paths" and p.get("closed"):
                for pts in flatten_path(d, 0.003):
                    poly = Polygon(pts).buffer(0)
                    if poly.area > 0:
                        polygons.append(poly)
    for ring in overlay.get("roof_footprint_m", []):
        polygons.append(Polygon([project(x, y) for x, y in ring]).buffer(0))
    # The author's field and roof-opening contours also occlude underlying basemap detail.
    for family, pid, d, native in result:
        if (
            "pitch" in pid
            and ("outline" in pid or "boundary" in pid)
            or pid == "roof-opening"
        ):
            for pts in flatten_path(d, 0.003):
                if pts[0] == pts[-1]:
                    polygons.append(Polygon(pts).buffer(0))
    assert polygons, "No sourced stadium occlusion polygons"
    return result, unary_union(polygons), project


def numerical_floor_exceptions(root, failures):
    """Retain exact-floor strokes misclassified by binary floating-point arithmetic."""
    exceptions = []
    for g in root.findall(S("g")):
        label = g.get(f"{{{INK}}}label", "")
        matching = [
            failure
            for failure in failures
            if failure.startswith(f"layer '{label}':")
            and "sub-nib strokes shorter than" in failure
        ]
        if not matching:
            continue
        floor = 3 * float(g.get("data-plot-nib-mm"))
        lengths = [
            sub.length_mm
            for path in g.iter(S("path"))
            for sub in _parse_path(path.get("d"))[0]
            if sub.length_mm < floor
        ]
        if lengths and all(floor - length < 1e-9 for length in lengths):
            exceptions.extend(matching)
    return exceptions


def build_one(row):
    sid = row["id"]
    local = WORK / sid
    local.mkdir(parents=True, exist_ok=True)
    source_pbf = DEST / "reproduction/sources" / f"{sid}.osm.pbf"
    base = local / "basemap.svg"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(FROZEN / "renderer/src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-B",
        "-m",
        "city_map_plotter",
        "export",
        "--bbox",
        *map(str, row["bbox"]),
        "--input-pbf",
        str(source_pbf),
        "--preset",
        "a3-balanced-poster",
        "--orientation",
        "portrait",
        "--poster-layout",
        "city-map",
        "--layers",
        "roads,water,railways,parks,buildings",
        "--style",
        str(FROZEN / "styles/university-memorabilia-v2.json"),
        "--water-fill",
        "dots",
        "--landmark-buildings",
        "--detail-profile",
        "plotter-faithful",
        "--simplify-mm",
        "0.04",
        "--road-style",
        "centreline",
        "--extent-fit",
        "contain",
        "--pen-profile",
        "actual-pens",
        "--no-scale-bar",
        "--no-scale-detail",
        "--optimise",
        "--frame",
        "--title",
        row["name"].upper(),
        "--attribution-mode",
        "external",
        "--external-attribution-placement",
        "Accompanying product page, packaging and ATTRIBUTION.md",
        "--output",
        str(base),
    ]
    if not base.exists():
        with (local / "render.log").open("w") as log:
            subprocess.run(
                command,
                env=env,
                cwd=ROOT,
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    m = json.loads(base.with_suffix(".plot.json").read_text())
    if m["source"]["provenance"]["content_sha256"] != sha(source_pbf):
        raise ValueError(
            f"{sid}: cached basemap source changed; remove its basemap intermediates"
        )
    root = ET.parse(base).getroot()
    br = validate(base, SPEC, None)
    base_numeric = numerical_floor_exceptions(root, br.failures)
    if any(f not in base_numeric for f in br.failures):
        raise ValueError(f"{sid} base format: {br.failures[:5]}")
    out = DEST / row["league"] / sid
    out.mkdir(parents=True, exist_ok=True)
    svg = out / f"{sid}.svg"
    overlay_path = SOURCE / row["files"]["stadium_overlay"]["path"]
    assert sha(overlay_path) == row["files"]["stadium_overlay"]["sha256"]
    overlay = json.loads(overlay_path.read_text())
    layout = make_poster_layout(
        BoundingBox(**m["extent_wgs84"]),
        format_id="a3-portrait",
        preset="a3-balanced-poster",
        poster_layout="city-map",
    )
    detail, mask, project = native_paths(overlay, layout)
    all_stadium = unary_union(
        [LineString(p) for _, _, d, _ in detail for p in flatten_path(d, 0.005)]
    )
    extent = box(*layout.clip_rect)
    assert extent.buffer(-0.25).covers(all_stadium)
    # Mapped stadium envelopes also serve as non-drawn occlusion surfaces for open stand outlines.
    for g in root.findall(S("g")):
        if g.get("id") != "layer-buildings":
            continue
        for p in g.iter(S("path")):
            if p.get("data-mapplot-landmark-role") != "stadium":
                continue
            for pts in flatten_path(p.get("d"), 0.005):
                if pts[0] == pts[-1]:
                    poly = Polygon(pts).buffer(0)
                    if poly.intersection(mask).area > poly.area * 0.2:
                        mask = mask.union(poly)
    mask = mask.buffer(0.25)
    removed = []
    clipped = []
    shortened = 0
    for g in root.findall(S("g")):
        lid = g.get("id", "")
        if lid in ["layer-frame", "layer-poster_border"] or lid.startswith(
            "layer-poster_"
        ):
            continue
        nib = float(g.get("stroke-width"))
        cut = mask.buffer(nib / 2)
        parents = {child: parent for parent in g.iter() for child in parent}
        for p in list(g.iter(S("path"))):
            paths = [LineString(pts) for pts in flatten_path(p.get("d"), 0.005)]
            if not any(line.intersects(cut) for line in paths):
                continue
            original_ref = p.get("data-osm-source-refs", p.get("data-osm-id", ""))
            parent = parents[p]
            idx = list(parent).index(p)
            parent.remove(p)
            count = 0
            for path in paths:
                for frag in parts(path.difference(cut)):
                    if frag.length < 3 * nib:
                        shortened += 1
                        continue
                    q = copy.deepcopy(p)
                    q.set("d", dline(frag))
                    q.set("data-stadium-occlusion", "roof-and-field")
                    parent.insert(idx + count, q)
                    count += 1
            (removed if count == 0 else clipped).append(
                {"layer": lid, "source_ref": original_ref}
            )
    stad_groups = {}
    subfloor = []
    for fam, pid, d, native in detail:
        is_shell = fam == "shell_paths"
        nib = 0.4 if is_shell else 0.25
        gid = "layer-stadium_shell" if is_shell else "layer-stadium_detail"
        if gid not in stad_groups:
            stad_groups[gid] = group(
                gid,
                "Stadium roof shell — Black 0.4"
                if is_shell
                else "Stadium roof and pitch detail — Black 0.25",
                "Black",
                nib,
                "#18181b" if is_shell else "#26333d",
            )
        attrs = {
            "d": d,
            "data-physical-stroke": "1",
            "data-stadium-path-id": pid,
            "data-stadium-family": fam,
            "data-stadium-native-curves": str(native).lower(),
            "data-stadium-source-overlay-sha256": sha(overlay_path),
        }
        length = sum(LineString(p).length for p in flatten_path(d, 0.003))
        if length < 3 * nib:
            subfloor.append({"id": pid, "length_mm": length, "nib_mm": nib})
            attrs["data-stadium-preserved-subfloor-detail"] = "true"
        ET.SubElement(stad_groups[gid], S("path"), attrs)
    for g in stad_groups.values():
        root.append(g)
    assert sum(len(g) for g in stad_groups.values()) == len(
        overlay["shell_paths"]
    ) + len(overlay["paths"])
    city_header(root, m, row)
    m["generator"] = "stadium-city-house-compositor/1"
    m["source_stadium"] = {
        "id": sid,
        "name": row["name"],
        "city": row["city"],
        "version": row["version"],
        "review_note": row["review_note"],
        "overlay": row["files"]["stadium_overlay"],
        "preserved_native_paths": len(detail),
        "native_curve_paths": sum(n for _, _, _, n in detail),
        "centering": overlay["centring"],
        "pitch_georeference": overlay["georeference"],
    }
    m["stadium_composition"] = {
        "zoom_out_width_factor": 1.15,
        "old_extent_wgs84": row["old_extent_wgs84"],
        "roof_occlusion": {
            "removed": removed,
            "clipped": clipped,
            "short_clipped_fragments_removed": shortened,
        },
        "native_detail_below_three_nibs_preserved": subfloor,
        "stadium_path_coverage": 1.0,
    }
    m["rendering"]["source_integrity_scope"] = (
        "Basemap checks before authored stadium integration; final roof knockouts and native detail are recorded in stadium_composition."
    )
    m["reproduction"] = {
        "renderer": "../../../production-maps-2026-09-06/reproduction/renderer",
        "source_pbf": record(source_pbf),
        "base_svg_sha256": sha(base),
        "source_snapshot_timestamp": "2026-08-06T20:21:21Z",
        "source_snapshot_note": "Same dated Great Britain source cohort as the existing Newcastle city collection.",
    }
    # Keep each physical pen in one consecutive block, including the added stadium lines.
    pen_order = {}
    for g in root.findall(S("g")):
        pen_order.setdefault(g.get("data-plot-pen-id"), len(pen_order))
    ordered = sorted(
        root.findall(S("g")), key=lambda g: pen_order[g.get("data-plot-pen-id")]
    )
    for g in ordered:
        root.remove(g)
    for g in ordered:
        root.append(g)
    # Rebuild every manifest layer from actual composed paths, retaining existing family metadata.
    old_layers = {x["svg_group_id"]: x for x in m["layers"]}
    layers = []
    for g in root.findall(S("g")):
        paths = list(g.iter(S("path")))
        if not paths:
            root.remove(g)
            continue
        lid = g.get("id")
        layer = old_layers.get(
            lid,
            {
                "id": lid.removeprefix("layer-"),
                "svg_group_id": lid,
                "label": g.get(f"{{{INK}}}label"),
            },
        )
        layer.update(
            path_count=len(paths),
            pen_down_distance_mm=round(
                sum(
                    LineString(p).length
                    for e in paths
                    for p in flatten_path(e.get("d"), 0.01)
                ),
                5,
            ),
            svg_layer_label=g.get(f"{{{INK}}}label"),
            nib_mm=float(g.get("stroke-width")),
            pen_id=g.get("data-plot-pen-id"),
            passes=1,
        )
        layers.append(layer)
    m["layers"] = layers
    m["rendering"]["document_layer_order"] = [x["id"] for x in layers]
    m["plot_summary"] = {
        "pen_down_path_count": sum(x["path_count"] for x in layers),
        "pen_down_distance_mm": sum(x["pen_down_distance_mm"] for x in layers),
    }
    m["production_readiness"] = {
        "production_ready": False,
        "mode": "review-only",
        "note": "Native stadium detail retained; nominal pen inventory. Review fine roof spacing on a physical proof.",
    }
    save(root, svg)
    m["svg_sha256"] = sha(svg)
    dump(svg.with_suffix(".plot.json"), m)
    pf = preflight_svg(svg)
    assert not pf.errors, [x.as_dict() for x in pf.errors]
    # The standard validator is also reported, including any physically short native architectural details.
    fr = validate(svg, SPEC, None)
    final_numeric = numerical_floor_exceptions(root, fr.failures)
    if fr.failures:
        # This exception preserves the user-specified native architecture, never alters the global floor.
        allowed = [
            s
            for s in fr.failures
            if (
                "3×" in s
                or "3 x" in s
                or "shorter" in s
                or "stroke length" in s
                or "sub-nib" in s
            )
            and (
                s.startswith("layer 'Stadium roof shell — Black 0.4':")
                or s.startswith("layer 'Stadium roof and pitch detail — Black 0.25':")
            )
        ]
        if any(f not in allowed + final_numeric for f in fr.failures):
            raise ValueError(f"{sid} composed format: {fr.failures[:8]}")
    else:
        allowed = []
    device, machine, _ = load_device_profile(PROFILE)
    job = compile_plot_job(
        svg, machine, profile_binding={"id": device["id"], "sha256": sha(PROFILE)}
    )
    job_path = svg.with_suffix(".plotjob.json")
    write_plot_job(job_path, job)
    assert job["safety"]["execution_allowed"] is False
    if any(f["severity"] == "error" for f in job["safety"]["findings"]):
        raise ValueError("Machine bounds failure")
    m["pen_sequence"] = []
    cursor = (0.0, 0.0)
    for i, pg in enumerate(job["pen_groups"], 1):
        pen = pg["pen"]
        distance = sum(stroke["length_mm"] for stroke in pg["strokes"])
        pen_up = 0.0
        for stroke in pg["strokes"]:
            points = stroke["points_mm"]
            pen_up += math.dist(cursor, points[0])
            cursor = points[-1]
        m["pen_sequence"].append(
            {
                "step": i,
                "pen": pen["label"],
                "pen_id": pen["id"],
                "pen_profile": "actual-pens",
                "ink": pen["ink"],
                "nib_mm": pen["nib_mm"],
                "nominal_nib_mm": pen["nominal_nib_mm"],
                "calibration_state": pen["calibration_state"],
                "path_count": len(pg["strokes"]),
                "pen_down_distance_mm": distance,
                "pen_up_travel_mm": pen_up,
                "layers": [x["id"] for x in layers if x["pen_id"] == pen["id"]],
                "timing_scope": "Exact compiled job order; nominal motion profile",
            }
        )
    m["plot_summary"].update(
        {
            "physical_pen_steps": len(job["pen_groups"]),
            "pen_changes": job["stats"]["pen_swaps"],
            "pen_down_distance_mm": job["stats"]["pen_down_mm"],
            "pen_up_travel_mm": job["stats"]["pen_up_mm"],
            "estimated_plot_seconds_including_pen_up": job["stats"]["total_seconds"],
            "timing_scope": "Final composed SVG compiled into the accompanying SHA-bound plot job; nominal profile.",
        }
    )
    m["plotjob"] = {
        "path": job_path.name,
        "sha256": sha(job_path),
        "source_sha256": job["source"]["sha256"],
    }
    dump(svg.with_suffix(".plot.json"), m)
    pen_files = []
    pd = out / "pens"
    pd.mkdir(exist_ok=True)
    for i, pg in enumerate(job["pen_groups"], 1):
        penid = pg["pen"]["id"]
        split = copy.deepcopy(root)
        for g in list(split):
            if g.tag == S("g") and g.get("data-plot-pen-id") != penid:
                split.remove(g)
        pp = pd / f"{sid}.pen-{i:02d}-{penid}.svg"
        save(split, pp)
        assert not preflight_svg(pp).errors
        pen_files.append(record(pp))
    subprocess.run(
        [
            "inkscape",
            str(svg),
            "--export-type=png",
            "--export-dpi=254",
            "--export-background=white",
            "--export-background-opacity=1",
            f"--export-filename={svg.with_suffix('.png')}",
        ],
        check=True,
        capture_output=True,
    )
    from PIL import Image

    image = Image.open(svg.with_suffix(".png"))
    assert image.size == (2970, 4200)
    thumb = image.copy()
    thumb.thumbnail((594, 840))
    thumb.save(out / "thumbnail.png")
    bounds = all_stadium.bounds
    pad = 3
    # A close-up is cropped from the exact final output, not another stadium drawing.
    crop = image.crop(
        (
            max(0, int((bounds[0] - pad) * 10)),
            max(0, int((bounds[1] - pad) * 10)),
            min(2970, math.ceil((bounds[2] + pad) * 10)),
            min(4200, math.ceil((bounds[3] + pad) * 10)),
        )
    )
    crop.save(out / "stadium-detail.png")
    qa = {
        "status": "passed",
        "id": sid,
        "city_header": m["header_layout"],
        "svg_preflight": pf.as_dict(),
        "stadium_paths_expected": len(detail),
        "stadium_paths_exported": sum(len(g) for g in stad_groups.values()),
        "stadium_native_curves_preserved": True,
        "map_bbox": m["extent_wgs84"],
        "zoom_out_width_factor": 1.15,
        "base_format_checks": br.checks,
        "base_format_failures": br.failures,
        "floating_point_floor_exceptions": {
            "tolerance_mm": 1e-9,
            "base": base_numeric,
            "composed": final_numeric,
        },
        "composed_format_checks": fr.checks,
        "format_failures": fr.failures,
        "native_detail_floor_exceptions": allowed,
        "nominal_stadium_subfloor_paths": subfloor,
        "bounds": "passed",
        "pen_splits": len(pen_files),
        "png_pixels": image.size,
    }
    dump(out / "QA.json", qa)
    rowout = {
        k: row[k]
        for k in [
            "id",
            "league",
            "name",
            "city",
            "version",
            "review_note",
            "georeference",
        ]
    }
    rowout["files"] = {
        k: record(p)
        for k, p in [
            ("svg", svg),
            ("png", svg.with_suffix(".png")),
            ("thumbnail", out / "thumbnail.png"),
            ("detail", out / "stadium-detail.png"),
            ("manifest", svg.with_suffix(".plot.json")),
            ("plotjob", job_path),
            ("qa", out / "QA.json"),
        ]
    }
    rowout["pens"] = pen_files
    dump(out / "record.json", rowout)
    print(
        f"COMPLETE {sid}: {len(detail)} stadium paths; {pf.path_count} total strokes",
        flush=True,
    )
    return rowout


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    selected = [r for r in ROWS if not args.ids or r["id"] in args.ids]
    errors = []
    result = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(build_one, row): row["id"] for row in selected}
        for future in as_completed(pending):
            try:
                result.append(future.result())
            except Exception as exc:
                import traceback

                traceback.print_exc()
                errors.append({"id": pending[future], "error": str(exc)})
    dump(WORK / "build-errors.json", errors)
    if errors:
        raise SystemExit(1)
    print("Completed", len(result), "stadium exports", flush=True)
