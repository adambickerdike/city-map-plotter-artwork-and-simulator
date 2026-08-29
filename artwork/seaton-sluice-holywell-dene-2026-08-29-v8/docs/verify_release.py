#!/usr/bin/env python3
"""Fail-closed QA for the Seaton Sluice cardinal-arrow release."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import struct
import subprocess
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


RELEASE = Path(__file__).resolve().parents[1]
REPO = RELEASE.parents[1]
STEM = "seaton-sluice-holywell-dene-a3-landscape"
MASTER = RELEASE / "artwork" / f"{STEM}.svg"
MANIFEST = RELEASE / "artwork" / f"{STEM}.plot.json"
PNG = RELEASE / "artwork" / f"{STEM}.png"
SOURCE = RELEASE / "sources" / "seaton-sluice-holywell-dene-overpass-2026-08-29.json.gz"
CONTRACT = RELEASE / "SOURCE-CONTRACT.json"
QUERY = RELEASE / "sources" / "OVERPASS_QUERY.ql"
PLOTJOB = RELEASE / "simulation" / "seaton-sluice-holywell-dene.plotjob.json"
VIEWER = RELEASE / "simulation" / "seaton-sluice-holywell-dene-plotsim.html"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def cache_identity(endpoint: str, query: str) -> str:
    normalized = re.sub(r"\[timeout:\d+\]", "[timeout:*]", query, count=1)
    digest = hashlib.sha256()
    for value in (endpoint, normalized):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise AssertionError(f"{path} is not a PNG with an IHDR header.")
    return struct.unpack(">II", header[16:24])


def inside(point: tuple[float, float], extent: dict[str, float]) -> bool:
    longitude, latitude = point
    return (
        extent["west"] <= longitude <= extent["east"]
        and extent["south"] <= latitude <= extent["north"]
    )


def points(element: dict[str, Any]) -> list[tuple[float, float]]:
    return [
        (float(point["lon"]), float(point["lat"]))
        for point in element.get("geometry", [])
        if isinstance(point, dict) and "lon" in point and "lat" in point
    ]


def check_checksums() -> int:
    checksum_file = RELEASE / "CHECKSUMS.sha256"
    if not checksum_file.exists():
        return 0
    rows = checksum_file.read_text(encoding="utf-8").splitlines()
    for row in rows:
        expected, relative = row.split("  ", 1)
        path = RELEASE / relative
        assert path.is_file(), f"Checksum target is missing: {relative}"
        assert file_sha256(path) == expected, f"Checksum mismatch: {relative}"
    return len(rows)


def verify() -> dict[str, Any]:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    query = QUERY.read_text(encoding="utf-8")
    snapshot_contract = contract["snapshot"]
    acquisition_contract = contract["acquisition"]
    focus = contract["focus_evidence"]
    expected_rendering = contract["render_contract"]

    assert hashlib.sha256(query.encode("utf-8")).hexdigest() == acquisition_contract[
        "query_sha256"
    ]
    assert cache_identity(acquisition_contract["endpoint"], query) == acquisition_contract[
        "cache_identity_sha256"
    ]
    assert SOURCE.stat().st_size == snapshot_contract["size_bytes"]
    assert file_sha256(SOURCE) == snapshot_contract["file_sha256"]
    cached_source = RELEASE / snapshot_contract["service_cache_path"]
    assert SOURCE.read_bytes() == cached_source.read_bytes()

    with gzip.open(SOURCE, "rt", encoding="utf-8") as stream:
        source_data = json.load(stream)
    assert canonical_sha256(source_data) == snapshot_contract["canonical_json_sha256"]
    assert len(source_data["elements"]) == snapshot_contract["element_count"]
    assert source_data["osm3s"]["timestamp_osm_base"] == acquisition_contract[
        "timestamp_osm_base"
    ]
    element_index = {
        f"{element.get('type')}/{element.get('id')}": element
        for element in source_data["elements"]
        if isinstance(element, dict)
    }

    extent = contract["subject"]["render_extent_wgs84"]
    burn_refs = contract["coverage_evidence"]["named_seaton_burn_source_chain"]
    previous_end: tuple[float, float] | None = None
    in_frame_burn_points: list[tuple[float, float]] = []
    for source_ref in burn_refs:
        element = element_index[source_ref]
        assert element.get("tags", {}).get("name") == "Seaton Burn"
        source_points = points(element)
        assert source_points
        if previous_end is not None:
            assert abs(source_points[0][0] - previous_end[0]) <= 1e-7
            assert abs(source_points[0][1] - previous_end[1]) <= 1e-7
        previous_end = source_points[-1]
        assert all(inside(point, extent) for point in source_points), (
            f"Complete Dene source is not inside frame: {source_ref}"
        )
        in_frame_burn_points.extend(source_points)
    assert min(point[0] for point in in_frame_burn_points) > extent["west"]
    assert max(point[0] for point in in_frame_burn_points) > -1.475
    assert min(point[1] for point in in_frame_burn_points) > extent["south"]
    assert max(point[1] for point in in_frame_burn_points) < extent["north"]

    hall_contract = focus["seaton_delaval_hall"]
    hall = element_index[hall_contract["source_ref"]]
    assert hall.get("tags", {}).get("name") == hall_contract["name"]
    assert hall.get("tags", {}).get("historic") == hall_contract["historic"]
    assert all(inside(point, extent) for point in points(hall))
    for source_ref in contract["coverage_evidence"][
        "rendered_coastline_source_refs"
    ]:
        assert element_index[source_ref].get("tags", {}).get("natural") == "coastline"

    for focus_id in ("seaton_sluice", "holywell_village"):
        record = focus[focus_id]
        cache = json.loads((RELEASE / record["geocoder_cache_path"]).read_text())
        match = next(
            item
            for item in cache
            if f"{item['osm_type']}/{item['osm_id']}" == record["source_ref"]
        )
        point = (float(match["lon"]), float(match["lat"]))
        assert point == (record["longitude"], record["latitude"])
        assert inside(point, extent) is record["inside_render_extent"]
    assert focus["holywell_village"]["inside_render_extent"] is True

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["title"] == "SEATON SLUICE"
    assert manifest["page"]["paper"] == "A3"
    assert manifest["page"]["orientation"] == "landscape"
    assert manifest["extent_wgs84"] == extent
    assert manifest["projection"]["approximate_scale_denominator"] == 13180
    assert manifest["city_map"]["coordinates"] == "55.0747 N / 1.4970 W"
    assert manifest["page"]["zones_mm"]["map_field"] == expected_rendering[
        "city_map_field_mm"
    ]
    assert manifest["source"]["timestamp"] == acquisition_contract[
        "timestamp_osm_base"
    ]
    provenance = manifest["source"]["provenance"]
    assert provenance["source_file_sha256"] == snapshot_contract["file_sha256"]
    assert provenance["canonical_source_data_sha256"] == snapshot_contract[
        "canonical_json_sha256"
    ]

    rendering = manifest["rendering"]
    for key in (
        "preset",
        "poster_layout",
        "detail_profile",
        "road_style",
        "water_fill",
        "extent_fit",
        "pen_profile",
        "attribution_mode",
    ):
        assert rendering[key] == expected_rendering[key]
    assert rendering["simplify_tolerance_mm"] == expected_rendering[
        "simplify_tolerance_mm"
    ]
    assert rendering["landmark_refs"] == expected_rendering["landmark_refs"]
    raw = rendering["raw_geometry_integrity"]
    assert raw["status"] == "verified"
    assert raw["supplied_source_geometry_complete"] is True
    assert raw["failure_count"] == 0
    highways = rendering["highway_completeness"]
    assert highways["unresolved_in_frame_count"] == 0
    assert highways["retained_unknown_in_frame_count"] == 0
    assert set(highways["missing_by_reason"]) <= {"physical_minimum_gate"}
    physical_evidence = highways["physical_minimum_omission_evidence"]
    assert physical_evidence["invalid_entry_count"] == 0
    assert physical_evidence["valid_entry_count"] > 0

    svg_text = MASTER.read_text(encoding="utf-8")
    for source_ref in [hall_contract["source_ref"], *burn_refs]:
        assert source_ref in svg_text, f"Required rendered source is absent: {source_ref}"
    for source_ref in contract["coverage_evidence"][
        "rendered_coastline_source_refs"
    ]:
        assert source_ref in svg_text
    root = ET.fromstring(svg_text)
    groups = {
        element.get("id"): element
        for element in root.iter()
        if element.get("id")
    }
    assert "layer-attribution" not in groups
    title_group = groups["layer-poster_title"]
    header_contract = expected_rendering["header_contract"]
    assert title_group.get("data-title-lines") == str(
        header_contract["title_lines"]
    )
    assert json.loads(title_group.get("data-title-line-copy-json", "[]")) == [
        "SEATON SLUICE",
    ]
    coordinate_group = groups["layer-poster_coordinates"]
    assert coordinate_group.get("data-copy") == header_contract[
        "coordinate_copy"
    ]
    assert coordinate_group.get("data-coordinate-layout") == header_contract[
        "coordinate_layout"
    ]
    assert json.loads(
        coordinate_group.get("data-coordinate-line-copy-json", "[]")
    ) == [header_contract["coordinate_copy"]]
    assert float(coordinate_group.get("data-coordinate-line-gap-mm", "nan")) == (
        header_contract["coordinate_line_gap_mm"]
    )
    assert float(coordinate_group.get("data-coordinate-tracking-mm", "nan")) == (
        header_contract["coordinate_tracking_mm"]
    )
    compass_group = groups["layer-poster_compass"]
    assert float(compass_group.get("data-plot-nib-mm", "nan")) == header_contract[
        "compass_nib_mm"
    ]
    assert compass_group.get("data-compass-style") == header_contract[
        "compass_style"
    ]
    assert compass_group.get("data-cardinal-axes") == ",".join(
        header_contract["compass_cardinal_axes"]
    )
    assert json.loads(compass_group.get("data-compass-geometry-json", "{}")) == (
        header_contract["compass_geometry"]
    )
    compass_paths = [path for path in compass_group if path.tag.endswith("path")]
    compass_components = [
        path.get("data-compass-component") for path in compass_paths
    ]
    assert compass_components.count("north-arrow-shaft") == 1
    assert compass_components.count("north-arrow-head") == 1
    assert compass_components.count("east-west-axis") == 1
    assert compass_components.count("north-label") > 0
    assert "cardinal-diamond" not in compass_components

    component_paths = {
        path.get("data-compass-component"): path
        for path in compass_paths
        if path.get("data-compass-component") != "north-label"
    }

    def component_points(component: str) -> list[tuple[float, float]]:
        numbers = [
            float(value)
            for value in re.findall(
                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
                component_paths[component].get("d", ""),
            )
        ]
        return list(zip(numbers[0::2], numbers[1::2]))

    shaft_bottom, shaft_tip = component_points("north-arrow-shaft")
    head_left, head_tip, head_right = component_points("north-arrow-head")
    west, east = component_points("east-west-axis")
    assert shaft_tip == head_tip
    assert shaft_bottom[0] == shaft_tip[0]
    assert shaft_tip[1] < shaft_bottom[1]
    assert head_left[0] < head_tip[0] < head_right[0]
    assert head_left[1] == head_right[1]
    assert west[0] < shaft_tip[0] < east[0]
    assert west[1] == east[1]
    assert shaft_tip[1] < west[1] < shaft_bottom[1]

    pen_files = sorted((RELEASE / "artwork" / "pen-svgs").glob("*.svg"))
    assert len(pen_files) == len(manifest["pen_files"]) == 11
    for path in pen_files:
        ET.parse(path)
    assert png_dimensions(PNG) == (4200, 2970)

    plotjob = json.loads(PLOTJOB.read_text(encoding="utf-8"))
    assert plotjob["source"]["sha256"] == file_sha256(MASTER)
    assert plotjob["preflight"]["path_count"] == manifest["plot_summary"][
        "pen_down_path_count"
    ]
    assert plotjob["safety"]["execution_allowed"] is False
    assert {item["code"] for item in plotjob["safety"]["findings"]} == {
        "unmeasured-pens",
        "uncalibrated-machine-timing",
    }
    assert VIEWER.stat().st_size > 100_000

    assert file_sha256(REPO / expected_rendering["format_spec_path"]) == (
        expected_rendering["format_spec_sha256"]
    )
    assert file_sha256(REPO / expected_rendering["style_path"]) == expected_rendering[
        "style_sha256"
    ]
    validation = subprocess.run(
        [
            str(REPO / ".venv" / "bin" / "python"),
            str(REPO / "tools" / "validate_format.py"),
            str(MASTER),
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": "src"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "[PASS]" in validation.stdout

    return {
        "schema_version": 1,
        "status": "pass",
        "release": RELEASE.name,
        "focus": {
            "approximate_scale_denominator": manifest["projection"][
                "approximate_scale_denominator"
            ],
            "seaton_sluice_inside": True,
            "holywell_village_inside": True,
            "seaton_delaval_hall_fully_inside": True,
            "seaton_delaval_hall_rendered": True,
            "dene_source_way_count": len(burn_refs),
            "rendered_coastline_way_count": len(
                contract["coverage_evidence"]["rendered_coastline_source_refs"]
            ),
        },
        "artwork": {
            "svg_sha256": file_sha256(MASTER),
            "png_sha256": file_sha256(PNG),
            "png_pixels": [4200, 2970],
            "pen_file_count": len(pen_files),
            "plot_stroke_count": plotjob["preflight"]["path_count"],
        },
        "source": {
            "snapshot_sha256": file_sha256(SOURCE),
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "timestamp_osm_base": source_data["osm3s"]["timestamp_osm_base"],
            "element_count": len(source_data["elements"]),
        },
        "render_audit": {
            "format_validator": validation.stdout.strip(),
            "coordinate_layout": header_contract["coordinate_layout"],
            "coordinate_copy": header_contract["coordinate_copy"],
            "coordinate_tracking_mm": header_contract["coordinate_tracking_mm"],
            "compass_nib_mm": header_contract["compass_nib_mm"],
            "compass_style": header_contract["compass_style"],
            "compass_cardinal_axes": header_contract["compass_cardinal_axes"],
            "compass_geometry": header_contract["compass_geometry"],
            "raw_geometry_failure_count": raw["failure_count"],
            "highway_unresolved_in_frame_count": highways[
                "unresolved_in_frame_count"
            ],
            "physical_minimum_evidence_invalid_count": physical_evidence[
                "invalid_entry_count"
            ],
            "physical_execution_allowed": plotjob["safety"]["execution_allowed"],
        },
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--write-report",
        type=Path,
        help="Also write the successful JSON result to this path.",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    report = verify()
    if args.write_report is None:
        report["checksum_file_entry_count"] = check_checksums()
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.write_report is not None:
        args.write_report.parent.mkdir(parents=True, exist_ok=True)
        args.write_report.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
