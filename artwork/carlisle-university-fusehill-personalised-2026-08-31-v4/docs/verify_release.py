#!/usr/bin/env python3
"""Fail-closed QA for the personalised Cumbria Fusehill-focus plate."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


RELEASE = Path(__file__).resolve().parents[1]


def repository_root() -> Path:
    for candidate in RELEASE.parents:
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "src" / "city_map_plotter").is_dir()
        ):
            return candidate
    raise RuntimeError("Could not locate the city-map-plotter repository root.")


REPO = repository_root()
STEM = "carlisle-university-fusehill-personalised-a3-portrait"
MASTER = RELEASE / "artwork" / f"{STEM}.svg"
MANIFEST = RELEASE / "artwork" / f"{STEM}.plot.json"
PNG = RELEASE / "artwork" / f"{STEM}.png"
SOURCE = RELEASE / "sources" / "carlisle-city-overpass-2026-08-30.json.gz"
CONTRACT = RELEASE / "SOURCE-CONTRACT.json"
QUERY = RELEASE / "sources" / "OVERPASS_QUERY.ql"
PLOTJOB = (
    RELEASE
    / "simulation"
    / "carlisle-university-fusehill-personalised.plotjob.json"
)
VIEWER = (
    RELEASE
    / "simulation"
    / "carlisle-university-fusehill-personalised-plotsim.html"
)

SVG_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


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


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise AssertionError(f"{path} is not a PNG with an IHDR header.")
    return struct.unpack(">II", header[16:24])


def svg_path_bounds(elements: list[ET.Element]) -> dict[str, float]:
    points: list[tuple[float, float]] = []
    for element in elements:
        values = [float(value) for value in SVG_NUMBER.findall(element.get("d", ""))]
        points.extend(zip(values[0::2], values[1::2]))
    assert points, "SVG path group contains no geometry."
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "x": round(min(xs), 3),
        "y": round(min(ys), 3),
        "width": round(max(xs) - min(xs), 3),
        "height": round(max(ys) - min(ys), 3),
    }


def source_points(element: dict[str, Any]) -> list[tuple[float, float]]:
    return [
        (float(point["lon"]), float(point["lat"]))
        for point in element.get("geometry", [])
        if isinstance(point, dict) and "lon" in point and "lat" in point
    ]


def point_inside_extent(
    point: tuple[float, float], extent: dict[str, float]
) -> bool:
    longitude, latitude = point
    return (
        extent["west"] <= longitude <= extent["east"]
        and extent["south"] <= latitude <= extent["north"]
    )


def bounds_inside_extent(
    bounds: dict[str, float], extent: dict[str, float]
) -> bool:
    return (
        extent["west"] <= float(bounds["minlon"])
        and float(bounds["maxlon"]) <= extent["east"]
        and extent["south"] <= float(bounds["minlat"])
        and float(bounds["maxlat"]) <= extent["north"]
    )


def bounds_intersect_extent(
    bounds: dict[str, float], extent: dict[str, float]
) -> bool:
    return not (
        float(bounds["maxlon"]) < extent["west"]
        or float(bounds["minlon"]) > extent["east"]
        or float(bounds["maxlat"]) < extent["south"]
        or float(bounds["minlat"]) > extent["north"]
    )


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
    snapshot = contract["snapshot"]
    acquisition = contract["acquisition"]
    rendering_contract = contract["render_contract"]

    assert hashlib.sha256(query.encode("utf-8")).hexdigest() == acquisition[
        "query_sha256"
    ]
    assert SOURCE.stat().st_size == snapshot["size_bytes"]
    assert file_sha256(SOURCE) == snapshot["file_sha256"]
    with gzip.open(SOURCE, "rt", encoding="utf-8") as stream:
        source_data = json.load(stream)
    assert canonical_sha256(source_data) == snapshot["canonical_json_sha256"]
    assert len(source_data["elements"]) == snapshot["element_count"]
    assert source_data["osm3s"]["timestamp_osm_base"] == acquisition[
        "timestamp_osm_base"
    ]

    element_index = {
        f"{element.get('type')}/{element.get('id')}": element
        for element in source_data["elements"]
        if isinstance(element, dict)
    }
    render_extent = contract["subject"]["render_extent_wgs84"]
    required = contract["coverage_evidence"]["required_rendered_features"]
    for record in required:
        element = element_index[record["source_ref"]]
        assert element.get("tags", {}).get("name") == record["name"]
        policy = record["extent_policy"]
        points = source_points(element)
        bounds = element.get("bounds")
        assert policy in {"contained", "intersects"}
        assert points or isinstance(bounds, dict)
        if policy == "contained":
            if points:
                assert all(point_inside_extent(point, render_extent) for point in points)
            else:
                assert isinstance(bounds, dict)
                assert bounds_inside_extent(bounds, render_extent)
        elif points:
            assert any(point_inside_extent(point, render_extent) for point in points)
        else:
            assert isinstance(bounds, dict)
            assert bounds_intersect_extent(bounds, render_extent)

    omitted = contract["coverage_evidence"][
        "physically_omitted_university_features"
    ]
    assert len(omitted) == 1
    omitted_element = element_index[omitted[0]["source_ref"]]
    assert omitted_element.get("tags", {}).get("building") == "university"
    assert omitted[0]["projected_area_mm2"] < omitted[0]["minimum_area_mm2"]

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["generated_at"] == rendering_contract["generated_at"]
    assert manifest["title"] == "UNIVERSITY OF CUMBRIA"
    assert manifest["page"]["paper"] == "A3"
    assert manifest["page"]["orientation"] == "portrait"
    assert manifest["extent_wgs84"] == render_extent
    assert manifest["projection"]["center_latitude"] == 54.894000000000005
    assert manifest["projection"]["center_longitude"] == -2.92625
    assert manifest["projection"]["approximate_scale_denominator"] == (
        rendering_contract["expected_approximate_scale_denominator"]
    )
    assert manifest["page"]["zones_mm"]["map_field"] == rendering_contract[
        "map_field_mm"
    ]

    memorabilia = manifest["memorabilia"]
    assert memorabilia["variant"] == "clean-personalised"
    assert memorabilia["coordinates"] == "54.8940 N   /   2.9263 W"
    assert memorabilia["blank_template"] is False
    assert memorabilia["personalisation"] == {
        "person_name": "Stuart R. Nelis",
        "degree": "BSc Applied Psychology",
        "honours": "",
        "years": "2024",
    }
    for zone in rendering_contract["personalisation_zones"]:
        assert zone in manifest["page"]["zones_mm"]

    source = manifest["source"]
    assert source["timestamp"] == acquisition["timestamp_osm_base"]
    provenance = source["provenance"]
    assert provenance["acquisition_mode"] == "pinned-json"
    assert provenance["source_pinned"] is True
    assert provenance["source_file_sha256"] == snapshot["file_sha256"]
    assert provenance["canonical_source_data_sha256"] == snapshot[
        "canonical_json_sha256"
    ]
    highway_source = provenance["highway_coverage"]
    assert highway_source["source_highway_way_count"] == 3967
    assert highway_source["classified_highway_way_count"] == 3967
    assert highway_source["unknown_highway_way_count"] == 0

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
        "memorabilia_variant",
    ):
        assert rendering[key] == rendering_contract[key]
    assert rendering["simplify_tolerance_mm"] == rendering_contract[
        "simplify_tolerance_mm"
    ]
    assert rendering["landmark_buildings"] is True
    required_landmark_refs = contract["coverage_evidence"][
        "required_landmark_refs"
    ]
    assert rendering["landmark_refs"] == required_landmark_refs
    assert provenance["landmark_ref_acquisition"]["requested_refs"] == (
        required_landmark_refs
    )
    assert rendering["scale_bar"] is False
    assert rendering["visible_attribution"] is False
    assert rendering["external_attribution_placement"] == (
        "Accompanying map release ATTRIBUTION.md"
    )
    landmark_cleanup = rendering["cartographic_cleanup"]["landmark_buildings"]
    expected_landmarks = contract["coverage_evidence"][
        "expected_landmark_selection"
    ]
    selection = landmark_cleanup["selection"]
    assert selection["selected_object_count"] == expected_landmarks[
        "selected_object_count"
    ]
    assert selection["required_group_count"] == expected_landmarks[
        "required_group_count"
    ]
    dispositions = landmark_cleanup["must_have"]["dispositions"]
    assert [item["requested_ref"] for item in dispositions] == required_landmark_refs
    assert all(item["status"] == "selected" for item in dispositions)
    assert all(item["svg_path_count"] > 0 for item in dispositions)
    station_disposition = next(
        item
        for item in dispositions
        if item["requested_ref"] == "way/566812584"
    )
    assert station_disposition["status"] == "selected"
    assert station_disposition["svg_path_count"] == expected_landmarks[
        "station_svg_path_count"
    ]
    raw = rendering["raw_geometry_integrity"]
    assert raw["status"] == "verified"
    assert raw["supplied_source_geometry_complete"] is True
    assert raw["failure_count"] == 0
    highways = rendering["highway_completeness"]
    # Saved bare Overpass JSON does not embed the query, so the renderer keeps
    # this tri-state field unknown.  This release binds and hashes the exact
    # query independently above instead of promoting the field to True.
    assert highways["acquisition_scope_complete"] is None
    assert highways["unresolved_in_frame_count"] == 0
    assert highways["retained_unknown_in_frame_count"] == 0
    assert set(highways["missing_by_reason"]) <= {"physical_minimum_gate"}
    physical_evidence = highways["physical_minimum_omission_evidence"]
    assert physical_evidence["invalid_entry_count"] == 0
    assert physical_evidence["valid_entry_count"] > 0

    readiness = manifest["production_readiness"]
    assert readiness["production_ready"] is False
    assert readiness["mode"] == "review-only"
    assert readiness["residual_sub_nib_trail_count"] == 0
    assert readiness["conflict_scan_complete"] is True
    assert readiness["unresolved_below_nib_separation_pair_count"] == (
        rendering_contract["expected_unresolved_physical_conflict_count"]
    )
    assert readiness["physical_conflicts_accepted"] is False

    svg_text = MASTER.read_text(encoding="utf-8")
    for record in required:
        assert record["source_ref"] in svg_text, (
            f"Required rendered source is absent: {record['source_ref']}"
        )
    assert omitted[0]["source_ref"] not in svg_text
    root = ET.fromstring(svg_text)
    metadata = next(
        element for element in root if element.tag.endswith("}metadata")
    )
    assert json.loads(metadata.text or "{}")["generated_at"] == rendering_contract[
        "generated_at"
    ]
    groups = {
        element.get("id"): element
        for element in root.iter()
        if element.get("id")
    }
    assert "layer-attribution" not in groups
    title_group = groups["layer-poster_title"]
    assert title_group.get("data-copy") == "UNIVERSITY OF CUMBRIA"
    assert title_group.get("data-layout-zone") == "memorabilia_city_title"
    assert title_group.get("data-title-lines") == "1"
    assert json.loads(title_group.get("data-title-line-copy-json", "[]")) == [
        "UNIVERSITY OF CUMBRIA"
    ]
    coordinate_group = groups["layer-poster_coordinates"]
    assert coordinate_group.get("data-copy") == "54.8940 N   /   2.9263 W"
    assert coordinate_group.get("data-layout-zone") == "subtitle"
    assert coordinate_group.get("data-coordinate-layout") == "inline"
    assert coordinate_group.get("data-coordinate-align") == "left"
    assert coordinate_group.get("data-coordinate-align-reference") == (
        "title-ink-left"
    )
    assert float(coordinate_group.get("data-coordinate-tracking-mm", "nan")) == 0.25
    title_bounds = svg_path_bounds(list(title_group))
    coordinate_bounds = svg_path_bounds(list(coordinate_group))
    expected_ink_left = rendering_contract["header_contract"][
        "coordinate_and_title_ink_left_mm"
    ]
    assert title_bounds["x"] == coordinate_bounds["x"] == expected_ink_left
    compass_group = groups["layer-poster_compass"]
    assert float(compass_group.get("data-plot-nib-mm", "nan")) == 0.4
    assert compass_group.get("data-layout-zone") == "memorabilia_compass"
    assert compass_group.get("data-compass-style") == "diamond-cardinal"
    assert svg_path_bounds(list(compass_group)) == rendering_contract[
        "header_contract"
    ]["compass_path_bounds_mm"]
    personalisation_group = groups["layer-poster_personalisation"]
    assert json.loads(
        personalisation_group.get("data-fields-json", "{}")
    ) == memorabilia["personalisation"]
    assert personalisation_group.get("data-memorabilia-variant") == (
        "clean-personalised"
    )
    assert personalisation_group.get("data-labels-visible") == "false"
    assert personalisation_group.get("data-write-lines-visible") == "false"
    personalisation_contract = rendering_contract["personalisation_contract"]
    assert personalisation_group.get("data-value-font-role") == "display-serif"
    assert personalisation_group.get("data-stroke-font-id") == (
        personalisation_contract["font_id"]
    )
    assert float(personalisation_group.get("data-plot-nib-mm", "nan")) == (
        personalisation_contract["nib_mm"]
    )
    assert personalisation_group.get("data-horizontal-bounds-zone") == (
        personalisation_contract["horizontal_bounds_zone"]
    )
    personalisation_paths = list(personalisation_group)
    assert {path.get("data-field-part") for path in personalisation_paths} == {
        "value"
    }
    assert {
        path.get("data-personalisation-field")
        for path in personalisation_paths
    } == {"person_name", "degree", "years"}
    year_paths = [
        path
        for path in personalisation_paths
        if path.get("data-personalisation-field") == "years"
    ]
    assert year_paths
    assert all(path.get("data-field-align") == "right" for path in year_paths)
    name_paths = [
        path
        for path in personalisation_paths
        if path.get("data-personalisation-field") == "person_name"
    ]
    degree_paths = [
        path
        for path in personalisation_paths
        if path.get("data-personalisation-field") == "degree"
    ]
    assert {
        float(path.get("data-cap-height-mm", "nan")) for path in name_paths
    } == {personalisation_contract["name_cap_height_mm"]}
    assert {
        float(path.get("data-cap-height-mm", "nan")) for path in degree_paths
    } == {personalisation_contract["secondary_cap_height_mm"]}
    assert all(
        json.loads(path.get("data-layout-zone-span-json", "[]"))
        == ["memorabilia_degree", "memorabilia_honours"]
        for path in degree_paths
    )
    personalisation_bounds = svg_path_bounds(personalisation_paths)
    map_field = rendering_contract["map_field_mm"]
    half_nib = personalisation_contract["nib_mm"] / 2
    assert personalisation_bounds["x"] - half_nib >= map_field["x"]
    assert (
        personalisation_bounds["x"]
        + personalisation_bounds["width"]
        + half_nib
        <= map_field["x"] + map_field["width"]
    )

    pen_files = sorted((RELEASE / "artwork" / "pen-svgs").glob("*.svg"))
    assert len(pen_files) == len(manifest["pen_files"]) == 11
    for path in pen_files:
        ET.parse(path)
    assert png_dimensions(PNG) == (2970, 4200)

    plotjob = json.loads(PLOTJOB.read_text(encoding="utf-8"))
    assert plotjob["source"]["sha256"] == file_sha256(MASTER)
    assert plotjob["preflight"]["path_count"] == manifest["plot_summary"][
        "pen_down_path_count"
    ] == rendering_contract["expected_plot_path_count"]
    assert plotjob["stats"]["pen_loads"] == 11
    assert plotjob["safety"]["execution_allowed"] is False
    assert {item["code"] for item in plotjob["safety"]["findings"]} == {
        "unmeasured-pens",
        "uncalibrated-machine-timing",
    }
    assert VIEWER.stat().st_size > 100_000

    assert file_sha256(REPO / rendering_contract["format_spec_path"]) == (
        rendering_contract["format_spec_sha256"]
    )
    assert file_sha256(REPO / rendering_contract["style_path"]) == rendering_contract[
        "style_sha256"
    ]
    for relative, expected in contract["renderer_contract"]["files_sha256"].items():
        assert file_sha256(REPO / relative) == expected, (
            f"Renderer contract changed: {relative}"
        )

    validation = subprocess.run(
        [
            sys.executable,
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
        "subject": {
            "institution": "University of Cumbria",
            "map_subject": "Carlisle — Fusehill Street focus",
            "center": [54.894, -2.92625],
            "approximate_scale_denominator": rendering_contract[
                "expected_approximate_scale_denominator"
            ],
            "required_rendered_feature_count": len(required),
            "required_landmark_count": len(required_landmark_refs),
            "required_university_building_count": 17,
            "required_station_building_count": 1,
            "physically_omitted_university_building_count": len(omitted),
        },
        "artwork": {
            "svg_sha256": file_sha256(MASTER),
            "png_sha256": file_sha256(PNG),
            "png_pixels": [2970, 4200],
            "pen_file_count": len(pen_files),
            "plot_stroke_count": plotjob["preflight"]["path_count"],
        },
        "source": {
            "snapshot_sha256": file_sha256(SOURCE),
            "canonical_json_sha256": canonical_sha256(source_data),
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "timestamp_osm_base": source_data["osm3s"]["timestamp_osm_base"],
            "element_count": len(source_data["elements"]),
        },
        "render_audit": {
            "format_validator": validation.stdout.strip(),
            "raw_geometry_failure_count": raw["failure_count"],
            "highway_unresolved_in_frame_count": highways[
                "unresolved_in_frame_count"
            ],
            "physical_minimum_evidence_invalid_count": physical_evidence[
                "invalid_entry_count"
            ],
            "physical_execution_allowed": plotjob["safety"]["execution_allowed"],
            "unaccepted_physical_conflict_count": readiness[
                "unresolved_below_nib_separation_pair_count"
            ],
        },
        "simulation": {
            "nominal_seconds": plotjob["stats"]["total_seconds"],
            "low_seconds": plotjob["stats"]["total_low_seconds"],
            "high_seconds": plotjob["stats"]["total_high_seconds"],
            "pen_down_mm": plotjob["stats"]["pen_down_mm"],
            "pen_up_mm": plotjob["stats"]["pen_up_mm"],
            "pen_loads": plotjob["stats"]["pen_loads"],
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
