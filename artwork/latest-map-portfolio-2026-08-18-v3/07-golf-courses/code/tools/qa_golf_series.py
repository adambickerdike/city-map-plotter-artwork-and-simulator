#!/usr/bin/env python3
"""Run format, provenance, retrace, checksum, and PlotSim gates for golf."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from xml.etree import ElementTree as ET

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from city_map_plotter.golf import LABEL_CLEARANCE_MM, LABEL_RADIUS_MM


ROOT = Path(__file__).resolve().parent.parent
FORMAT_TOOL = ROOT / "tools" / "validate_format.py"
RETRACE_TOOL = ROOT / "tools" / "audit_svg_retraces.py"
PLOTSIM_TOOL = ROOT / "tools" / "plotsim.py"
DOCUMENT_LINE = re.compile(
    r"document\s+(?P<time>.+?)\s+down\s+(?P<down>[0-9.]+) m\s+"
    r"up\s+(?P<up>[0-9.]+) m\s+ratio\s+(?P<ratio>[0-9.]+)x\s+"
    r"lifts\s+(?P<lifts>\d+)\s+pens\s+(?P<pens>\d+)"
)
PATH_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
EXPECTED_CATALOG_COUNT = 25
EXPECTED_SERIES_ID = "golf-courses-v2"
EXPECTED_RELEASE_ID = "golf-course-series-v4"
EXPECTED_SERIES_TITLE = "TWENTY-FIVE ICONS OF GOLF"
EXPECTED_RENDERING_PRESET = "golf-clarity-course-a3-v4"
MINIMUM_ALL_WATER_DOT_SPACING_MM = 0.62
MINIMUM_AREA_WATER_DOT_SPACING_MM = 2.45
MAXIMUM_WATER_BEARING_BIN_FRACTION = 0.25
SERIALIZED_COORDINATE_TOLERANCE_MM = 0.002
GREEN_FILL_PHYSICAL_RADIUS_MM = 0.125
EXPECTED_GREEN_FILL_INSET_MM = 0.16
EXPECTED_GREEN_ROUTE_CLEARANCE_MM = 0.68

COURSE_BOUNDARY_RENDERING = "raw-root-boundary-omitted-selection-mask-only"
PLAYING_ENVELOPE_RENDERING = (
    "grey-0.40-derived-from-source-hole-routes-and-nearby-playing-surfaces"
    "-illustrative-not-property-or-official-boundary"
)
PLAYING_ENVELOPE_CLAIM = (
    "illustrative-envelope-not-property-or-official-course-boundary"
)
FAIRWAY_RENDERING = "green-0.25-source-outline-only"
GREEN_AND_TEE_RENDERING = (
    "green-0.40-source-outlines-with-green-only-green-0.25-fine-line-fill"
    "-tees-outline-only"
)
WATER_RENDERING = (
    "blue-0.40-area-outlines-with-blue-0.25-closed-dot-symbols-for-every"
    "-visible-area-linear-and-physically-narrow-water-source"
)
WATER_DOT_ROLES = frozenset(
    {
        "water-area-stipple-dot",
        "water-linear-stipple-dot",
        "water-narrow-boundary-stipple-dot",
        "water-narrow-source-stipple-dot",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )


def _path_points(path: ET.Element) -> list[tuple[float, float]]:
    values = [float(value) for value in PATH_NUMBER.findall(path.get("d", ""))]
    if len(values) < 4 or len(values) % 2:
        return []
    return list(zip(values[0::2], values[1::2], strict=True))


def _water_distribution(
    centres: list[tuple[float, float]],
) -> tuple[float | None, float | None]:
    """Return global minimum spacing and the strongest 15-degree NN bearing bin."""

    if len(centres) < 2:
        return None, None
    minimum_spacing = float("inf")
    nearest_bearings: list[float] = []
    for index, first in enumerate(centres):
        nearest_distance = float("inf")
        nearest_bearing = 0.0
        for other_index, second in enumerate(centres):
            if index == other_index:
                continue
            distance = math.dist(first, second)
            minimum_spacing = min(minimum_spacing, distance)
            if distance < nearest_distance:
                nearest_distance = distance
                nearest_bearing = (
                    math.degrees(math.atan2(second[1] - first[1], second[0] - first[0]))
                    % 180.0
                )
        nearest_bearings.append(nearest_bearing)
    bins = [0] * 12
    for bearing in nearest_bearings:
        bins[min(11, int(bearing // 15.0))] += 1
    return minimum_spacing, max(bins) / len(nearest_bearings)


def _manifest_logical_pen_ids(manifest: dict[str, Any]) -> dict[str, str]:
    logical_pen_ids: dict[str, str] = {}
    for layer in manifest.get("layers") or []:
        for logical_id in layer.get("logical_layers") or []:
            logical_pen_ids[str(logical_id)] = str(layer.get("pen_id"))
    return logical_pen_ids


def _closed_polygon(path: ET.Element) -> Polygon | None:
    points = _path_points(path)
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    polygon = Polygon(points)
    if not polygon.is_valid:
        repaired = polygon.buffer(0)
        if not isinstance(repaired, Polygon):
            return None
        polygon = repaired
    return polygon if not polygon.is_empty else None


def _svg_v4_style(
    svg: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Audit the v4 hierarchy and its final serialized water symbols."""

    failures: list[str] = []
    root = ET.parse(svg).getroot()
    paths = [element for element in root.iter() if element.tag.endswith("path")]
    roles = {path.get("data-role") for path in paths if path.get("data-role")}
    svg_logical_layers = {
        path.get("data-logical-layer")
        for path in paths
        if path.get("data-logical-layer")
    }
    logical_pen_ids = _manifest_logical_pen_ids(manifest)
    pen_ids = {str(step.get("pen_id")) for step in manifest.get("pen_sequence") or []}
    if "course_boundary" in svg_logical_layers or (
        "course_boundary" in logical_pen_ids
    ):
        failures.append("a visible course_boundary layer was emitted")
    if "black-0-6" in pen_ids:
        failures.append("the retired black-0-6 pen was emitted")
    if "water-hachure" in roles:
        failures.append("the retired water-hachure role was emitted")
    if "waterway" in roles:
        failures.append("a continuous waterway role was emitted instead of dots")
    if "water-stipple-dot" in roles:
        failures.append(
            "the legacy undifferentiated water-stipple-dot role was emitted"
        )
    if "fairway-fine-line-fill" in roles:
        failures.append("a forbidden fairway fine-line fill was emitted")
    if "tee-fine-line-fill" in roles:
        failures.append("a forbidden tee fine-line fill was emitted")
    fairway_paths = [
        path for path in paths if path.get("data-feature-kind") == "golf:fairway"
    ]
    if not fairway_paths or any(
        path.get("data-role") != "fairway-outline" for path in fairway_paths
    ):
        failures.append("fairways are not source-outline-only")
    tee_paths = [path for path in paths if path.get("data-feature-kind") == "golf:tee"]
    if not tee_paths or any(
        path.get("data-role") != "tee-outline" for path in tee_paths
    ):
        failures.append("tees are not source-outline-only")
    green_fill_paths = [
        path for path in paths if path.get("data-role") == "green-fine-line-fill"
    ]
    if any(path.get("data-feature-kind") != "golf:green" for path in green_fill_paths):
        failures.append("a green fill is not derived from a sourced green")
    green_fill_source_refs = {
        str(path.get("data-source-ref"))
        for path in green_fill_paths
        if path.get("data-source-ref")
    }
    green_coverage = (manifest.get("rendering") or {}).get("green_fill_coverage") or {}
    visible_green_count = green_coverage.get("visible_source_count")
    filled_green_count = green_coverage.get("filled_source_count")
    unfillable_green_refs = green_coverage.get("physically_unfillable_source_refs")
    if (
        green_coverage.get("fill_inset_mm") != EXPECTED_GREEN_FILL_INSET_MM
        or green_coverage.get("fill_pen_nib_mm") != 0.25
        or green_coverage.get("gold_route_clearance_mm")
        != EXPECTED_GREEN_ROUTE_CLEARANCE_MM
        or not isinstance(visible_green_count, int)
        or isinstance(visible_green_count, bool)
        or visible_green_count < 0
        or not isinstance(filled_green_count, int)
        or isinstance(filled_green_count, bool)
        or filled_green_count < 0
        or not isinstance(unfillable_green_refs, list)
        or unfillable_green_refs != []
        or filled_green_count != visible_green_count
        or green_coverage.get("uncovered_fillable_source_refs") != []
    ):
        failures.append("visible green-source fill coverage is incomplete")
    if filled_green_count != len(green_fill_source_refs):
        failures.append(
            "serialized green fills disagree with the manifest filled-source count"
        )
    green_outline_polygons: dict[str, list[Polygon]] = {}
    for path in paths:
        if path.get("data-role") != "green-outline":
            continue
        source_ref = path.get("data-source-ref")
        polygon = _closed_polygon(path)
        if source_ref and polygon is not None:
            green_outline_polygons.setdefault(source_ref, []).append(polygon)
    minimum_green_fill_outline_clearance = float("inf")
    for path in green_fill_paths:
        source_ref = path.get("data-source-ref")
        points = _path_points(path)
        source_polygons = green_outline_polygons.get(source_ref or "", [])
        if len(points) < 2 or not source_polygons:
            failures.append(
                f"green fill {source_ref or '<missing>'} lacks a source-matched outline"
            )
            continue
        fill_line = LineString(points)
        source_polygon = unary_union(source_polygons)
        minimum_green_fill_outline_clearance = min(
            minimum_green_fill_outline_clearance,
            fill_line.distance(source_polygon.boundary),
        )
        physical_fill = fill_line.buffer(
            GREEN_FILL_PHYSICAL_RADIUS_MM,
            cap_style="round",
            join_style="round",
        )
        if not source_polygon.buffer(SERIALIZED_COORDINATE_TOLERANCE_MM).covers(
            physical_fill
        ):
            failures.append(
                f"green fill {source_ref} extends beyond its source-matched outline"
            )

    envelope_paths = [
        path for path in paths if path.get("data-logical-layer") == "playing_envelope"
    ]
    if not envelope_paths:
        failures.append("the illustrative playing-area envelope is absent")
    elif any(
        path.get("data-role") != "playing-area-envelope"
        or path.get("data-claim-status") != PLAYING_ENVELOPE_CLAIM
        for path in envelope_paths
    ):
        failures.append("the playing-area envelope role or claim is invalid")

    expected_pens = {
        "playing_envelope": "grey-0-4",
        "fairways": "green-0-25",
        "greens_and_tees": "green-0-4",
        "water_stipple": "blue-0-25",
        "water": "blue-0-4",
    }
    for logical_id, expected_pen in expected_pens.items():
        actual_pen = logical_pen_ids.get(logical_id)
        required = logical_id in {
            "playing_envelope",
            "fairways",
            "greens_and_tees",
        }
        if (required and actual_pen != expected_pen) or (
            actual_pen is not None and actual_pen != expected_pen
        ):
            failures.append(f"{logical_id} uses {actual_pen}, expected {expected_pen}")

    water_dots = [
        path
        for path in paths
        if path.get("data-logical-layer") == "water_stipple"
        and path.get("data-role") in WATER_DOT_ROLES
    ]
    misplaced_water_dots = [
        path
        for path in paths
        if path.get("data-role") in WATER_DOT_ROLES
        and path.get("data-logical-layer") != "water_stipple"
    ]
    if misplaced_water_dots:
        failures.append("a water dot was emitted outside the water_stipple layer")

    centres: list[tuple[float, float]] = []
    area_centres: list[tuple[float, float]] = []
    closed_count = 0
    legal_count = 0
    role_counts: dict[str, int] = {}
    represented_source_refs: set[str] = set()
    for path in water_dots:
        role = str(path.get("data-role"))
        role_counts[role] = role_counts.get(role, 0) + 1
        represented_source_refs.update(
            source_ref
            for source_ref in (
                path.get("data-represented-source-refs")
                or path.get("data-source-ref")
                or ""
            ).split(",")
            if source_ref
        )
        points = _path_points(path)
        closed = path.get("d", "").rstrip().endswith("Z")
        if closed:
            closed_count += 1
        if len(points) < 3:
            continue
        closed_points = points if points[0] == points[-1] else [*points, points[0]]
        if LineString(closed_points).length + 1e-9 >= 0.75:
            legal_count += 1
        centre = (
            (min(x for x, _y in points) + max(x for x, _y in points)) / 2.0,
            (min(y for _x, y in points) + max(y for _x, y in points)) / 2.0,
        )
        centres.append(centre)
        if role == "water-area-stipple-dot":
            area_centres.append(centre)
    if closed_count != len(water_dots):
        failures.append("a water stipple mark is not a closed SVG path")
    if legal_count != len(water_dots):
        failures.append("a water stipple mark is shorter than the 3 x 0.25 mm floor")

    minimum_spacing, _all_dot_bearing_fraction = _water_distribution(centres)
    area_minimum_spacing, maximum_area_bearing_fraction = _water_distribution(
        area_centres
    )
    if minimum_spacing is not None and (
        minimum_spacing + SERIALIZED_COORDINATE_TOLERANCE_MM
        < MINIMUM_ALL_WATER_DOT_SPACING_MM
    ):
        failures.append("water-dot centres physically overlap below 0.62 mm")
    if area_minimum_spacing is not None and (
        area_minimum_spacing + SERIALIZED_COORDINATE_TOLERANCE_MM
        < MINIMUM_AREA_WATER_DOT_SPACING_MM
    ):
        failures.append("area-water-dot centre spacing is below 2.45 mm")
    if (
        len(area_centres) >= 30
        and maximum_area_bearing_fraction is not None
        and maximum_area_bearing_fraction > MAXIMUM_WATER_BEARING_BIN_FRACTION + 1e-12
    ):
        failures.append(
            "a 15-degree nearest-neighbour area-water-dot bearing bin exceeds 25%"
        )

    water_coverage = (manifest.get("rendering") or {}).get(
        "water_source_dot_coverage"
    ) or {}
    visible_source_count = water_coverage.get("visible_source_count")
    represented_source_count = water_coverage.get("represented_source_count")
    if (
        not isinstance(visible_source_count, int)
        or isinstance(visible_source_count, bool)
        or visible_source_count < 0
        or not isinstance(represented_source_count, int)
        or isinstance(represented_source_count, bool)
        or represented_source_count != visible_source_count
        or water_coverage.get("uncovered_source_refs") != []
    ):
        failures.append("visible water-source dot coverage is incomplete")
    if represented_source_count != len(represented_source_refs):
        failures.append(
            "serialized water-dot provenance disagrees with represented source count"
        )
    if water_coverage.get("dot_role_counts_final") != role_counts:
        failures.append("serialized water-dot role counts disagree with the manifest")

    return (
        {
            "playing_envelope_path_count": len(envelope_paths),
            "minimum_green_fill_outline_clearance_mm": (
                round(minimum_green_fill_outline_clearance, 6)
                if math.isfinite(minimum_green_fill_outline_clearance)
                else None
            ),
            "water_dot_count": len(water_dots),
            "water_dot_counts_by_role": role_counts,
            "represented_water_source_count": len(represented_source_refs),
            "closed_water_dot_count": closed_count,
            "legal_water_dot_count": legal_count,
            "minimum_all_water_dot_spacing_mm": (
                round(minimum_spacing, 3) if minimum_spacing is not None else None
            ),
            "minimum_area_water_dot_spacing_mm": (
                round(area_minimum_spacing, 3)
                if area_minimum_spacing is not None
                else None
            ),
            "maximum_15_degree_area_dot_nearest_bearing_bin_fraction": (
                round(maximum_area_bearing_fraction, 4)
                if maximum_area_bearing_fraction is not None
                else None
            ),
            "passed": not failures,
        },
        failures,
    )


def _svg_label_clearance(svg: Path) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    root = ET.parse(svg).getroot()
    paths = [element for element in root.iter() if element.tag.endswith("path")]
    markers = [
        path
        for path in paths
        if path.get("data-logical-layer") == "hole_markers"
        and path.get("data-role") == "hole-marker"
    ]
    marker_numbers = [int(path.get("data-hole-number", "0")) for path in markers]
    if len(markers) != 18 or sorted(marker_numbers) != list(range(1, 19)):
        failures.append("master SVG does not contain one marker path for holes 1-18")
    centres: dict[int, tuple[float, float]] = {}
    for marker in markers:
        points = _path_points(marker)
        number = marker.get("data-hole-number")
        if not points or number is None:
            continue
        centres[int(number)] = (
            (min(point[0] for point in points) + max(point[0] for point in points))
            / 2.0,
            (min(point[1] for point in points) + max(point[1] for point in points))
            / 2.0,
        )

    minimum_marker_separation = float("inf")
    centre_items = sorted(centres.items())
    for index, (_first_number, first) in enumerate(centre_items):
        for _second_number, second in centre_items[index + 1 :]:
            minimum_marker_separation = min(
                minimum_marker_separation,
                Point(first).distance(Point(second)),
            )
    required_separation = 2.0 * (LABEL_RADIUS_MM + LABEL_CLEARANCE_MM)
    if minimum_marker_separation <= required_separation:
        failures.append("hole-marker clearance disks overlap")

    mapped_lines = []
    for path in paths:
        logical_layer = path.get("data-logical-layer")
        if logical_layer in {"hole_markers", "hole_numbers", "map_reference"}:
            continue
        if path.get("data-feature-kind") is None:
            continue
        points = _path_points(path)
        if len(points) >= 2:
            mapped_lines.append(LineString(points))
    mapped = unary_union(mapped_lines) if mapped_lines else None
    minimum_map_clearance = float("inf")
    if mapped is not None:
        for _number, centre in centre_items:
            minimum_map_clearance = min(
                minimum_map_clearance,
                mapped.distance(Point(centre)),
            )
        if minimum_map_clearance + 1e-6 < (LABEL_RADIUS_MM + LABEL_CLEARANCE_MM):
            failures.append("mapped SVG ink enters a protected hole-number disk")

    leader_crossings = 0
    for leader in paths:
        if not (
            leader.get("data-logical-layer") == "hole_markers"
            and leader.get("data-role") == "hole-marker-leader"
        ):
            continue
        points = _path_points(leader)
        own_number = leader.get("data-hole-number")
        if len(points) < 2 or own_number is None:
            continue
        line = LineString(points)
        for number, centre in centre_items:
            if number == int(own_number):
                continue
            if line.distance(Point(centre)) + 1e-6 < required_separation / 2.0:
                leader_crossings += 1
    if leader_crossings:
        failures.append("a marker leader crosses another protected label disk")

    return (
        {
            "marker_count": len(markers),
            "minimum_marker_separation_mm": (
                round(minimum_marker_separation, 3)
                if math.isfinite(minimum_marker_separation)
                else None
            ),
            "minimum_map_clearance_mm": (
                round(minimum_map_clearance, 3)
                if math.isfinite(minimum_map_clearance)
                else None
            ),
            "leader_label_crossings": leader_crossings,
        },
        failures,
    )


def _verify_checksums(directory: Path) -> tuple[bool, list[str]]:
    path = directory / "CHECKSUMS.sha256"
    if not path.is_file():
        return False, ["CHECKSUMS.sha256 is missing"]
    failures: list[str] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError:
            failures.append(f"invalid checksum line {line_number}")
            continue
        target = directory / relative
        if not target.is_file():
            failures.append(f"missing checksummed file: {relative}")
        elif _sha256(target) != expected:
            failures.append(f"checksum mismatch: {relative}")
    return not failures, failures


def _refresh_checksums(directory: Path) -> None:
    files = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS.sha256"
    )
    (directory / "CHECKSUMS.sha256").write_text(
        "\n".join(
            f"{_sha256(path)}  {path.relative_to(directory).as_posix()}"
            for path in files
        )
        + "\n",
        encoding="utf-8",
    )


def _course_record(
    directory: Path, artifact: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    subject_id = str(artifact["subject_id"])
    svg = directory / str(artifact["svg"])
    manifest_path = directory / str(artifact["plot_manifest"])
    if not svg.is_file():
        failures.append("master SVG missing")
    if not manifest_path.is_file():
        failures.append("plot manifest missing")
        return {"subject_id": subject_id}, failures
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rendering = manifest.get("rendering") or {}
    summary = manifest.get("plot_summary") or {}
    sequence = manifest.get("pen_sequence") or []
    pen_ids = [step.get("pen_id") for step in sequence]
    if rendering.get("preset") != EXPECTED_RENDERING_PRESET:
        failures.append("manifest does not use the binding v4 golf preset")
    if manifest.get("subject_id") != subject_id:
        failures.append("subject identity mismatch")
    if rendering.get("course_hole_count") != 18:
        failures.append("course does not declare exactly 18 holes")
    if rendering.get("course_holes_numbered") != list(range(1, 19)):
        failures.append("numbered hole inventory is incomplete")
    if rendering.get("unmapped_features_invented") is not False:
        failures.append("source-only geometry claim is absent")
    if rendering.get("course_boundary_emitted") is not False or (
        rendering.get("course_boundary_rendering") != COURSE_BOUNDARY_RENDERING
    ):
        failures.append("raw root boundary is not certified as omitted")
    if (
        rendering.get("playing_envelope_emitted") is not True
        or rendering.get("playing_envelope_rendering") != PLAYING_ENVELOPE_RENDERING
    ):
        failures.append("illustrative playing-area envelope contract is absent")
    if rendering.get("fairway_rendering") != FAIRWAY_RENDERING:
        failures.append("fairway outline-only rendering contract is absent")
    if rendering.get("green_and_tee_rendering") != GREEN_AND_TEE_RENDERING:
        failures.append("green-only fill and tee outline contract is absent")
    green_coverage = rendering.get("green_fill_coverage") or {}
    visible_greens = green_coverage.get("visible_source_count")
    filled_greens = green_coverage.get("filled_source_count")
    unfillable_greens = green_coverage.get("physically_unfillable_source_refs")
    if (
        green_coverage.get("fill_inset_mm") != EXPECTED_GREEN_FILL_INSET_MM
        or green_coverage.get("fill_pen_nib_mm") != 0.25
        or green_coverage.get("gold_route_clearance_mm")
        != EXPECTED_GREEN_ROUTE_CLEARANCE_MM
        or not isinstance(visible_greens, int)
        or isinstance(visible_greens, bool)
        or visible_greens < 0
        or not isinstance(filled_greens, int)
        or isinstance(filled_greens, bool)
        or filled_greens < 0
        or not isinstance(unfillable_greens, list)
        or unfillable_greens != []
        or filled_greens != visible_greens
        or green_coverage.get("uncovered_fillable_source_refs") != []
    ):
        failures.append("visible green-source fill coverage is incomplete")
    if rendering.get("water_rendering") != WATER_RENDERING:
        failures.append("complete closed-dot water rendering contract is absent")
    water_coverage = rendering.get("water_source_dot_coverage") or {}
    visible_water_sources = water_coverage.get("visible_source_count")
    represented_water_sources = water_coverage.get("represented_source_count")
    if (
        not isinstance(visible_water_sources, int)
        or isinstance(visible_water_sources, bool)
        or visible_water_sources < 0
        or not isinstance(represented_water_sources, int)
        or isinstance(represented_water_sources, bool)
        or represented_water_sources != visible_water_sources
        or water_coverage.get("uncovered_source_refs") != []
    ):
        failures.append("visible water-source dot coverage is incomplete")
    if rendering.get("label_feature_overlap_mm") != 0.0:
        failures.append("hole-number clearance intersects mapped ink")
    masking = rendering.get("label_masking") or {}
    if masking.get("records_preserved_whole") != 0:
        failures.append("label masking could not preserve a complete source stroke")
    utilisation = rendering.get("fitted_geometry_working_rect_utilisation") or {}
    if not isinstance(utilisation.get("maximum"), (int, float)) or (
        utilisation["maximum"] < 0.97
    ):
        failures.append("fitted source geometry uses less than 97% of its working axis")
    if (
        rendering.get("scale_bar") is not True
        or rendering.get("north_mark") is not True
    ):
        failures.append("north/scale furniture incomplete")
    if len(pen_ids) != len(set(pen_ids)) or not pen_ids:
        failures.append("physical pen load repeats or is empty")
    if "black-0-6" in pen_ids:
        failures.append("retired black-0-6 pen remains in the physical sequence")
    coverage = summary.get("field_ink_coverage_upper_bound")
    if not isinstance(coverage, (int, float)) or coverage > 0.28:
        failures.append("field ink coverage exceeds 28%")
    manifest_travel = summary.get("travel_ratio")
    if not isinstance(manifest_travel, (int, float)) or manifest_travel >= 1.0:
        failures.append("manifest document travel ratio is not below 1.0")
    for layer in manifest.get("layers") or []:
        if not isinstance(layer.get("nib_mm"), (int, float)):
            failures.append(f"layer {layer.get('id')} omits nib_mm")
    if "course_boundary" in _manifest_logical_pen_ids(manifest):
        failures.append("manifest includes a forbidden course_boundary layer")

    if svg.is_file():
        svg_label_clearance, svg_label_failures = _svg_label_clearance(svg)
        failures.extend(svg_label_failures)
        svg_v4_style, svg_v4_failures = _svg_v4_style(svg, manifest)
        failures.extend(svg_v4_failures)
    else:
        svg_label_clearance = {}
        svg_v4_style = {}

    plotsim = _run([sys.executable, str(PLOTSIM_TOOL), str(svg), "--compare"])
    if plotsim.returncode != 0:
        failures.append("PlotSim failed: " + (plotsim.stderr or plotsim.stdout).strip())
        simulation: dict[str, Any] = {}
    else:
        line = next(
            (
                line
                for line in plotsim.stdout.splitlines()
                if line.strip().startswith("document")
            ),
            "",
        )
        match = DOCUMENT_LINE.search(line)
        if match is None:
            failures.append("PlotSim document summary could not be parsed")
            simulation = {}
        else:
            simulation = {
                "document_ratio": float(match.group("ratio")),
                "pen_down_m": float(match.group("down")),
                "pen_up_m": float(match.group("up")),
                "lifts": int(match.group("lifts")),
                "physical_pens": int(match.group("pens")),
            }
            if simulation["document_ratio"] >= 1.0:
                failures.append("PlotSim document travel ratio is not below 1.0")
            if simulation["physical_pens"] != len(pen_ids):
                failures.append(
                    "PlotSim pen count disagrees with the physical sequence"
                )
    return (
        {
            "subject_id": subject_id,
            "title": manifest.get("title"),
            "format_id": manifest.get("page", {}).get("format_id"),
            "scale_denominator": rendering.get("plan_scale_denominator"),
            "course_page_rotation_deg": rendering.get("course_page_rotation_deg"),
            "fitted_geometry_utilisation": utilisation,
            "label_feature_overlap_mm": rendering.get("label_feature_overlap_mm"),
            "svg_label_clearance": svg_label_clearance,
            "v4_style": svg_v4_style,
            "physical_pen_steps": len(pen_ids),
            "manifest_travel_ratio": manifest_travel,
            "field_ink_coverage": coverage,
            "plotsim": simulation,
            "passed": not failures,
            "failures": failures,
        },
        failures,
    )


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Golf course series QA",
        "",
        f"**Status:** {'PASS' if report['passed'] else 'FAIL'}",
        "",
        "| Course | Scale | Rotation | Fit | Pens | Travel | Coverage | Label overlap | Retrace | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for course in report["courses"]:
        simulation = course.get("plotsim") or {}
        lines.append(
            f"| {course['subject_id']} | 1:{course.get('scale_denominator')} | "
            f"{course.get('course_page_rotation_deg')} deg | "
            f"{100 * float(course.get('fitted_geometry_utilisation', {}).get('maximum') or 0):.1f}% | "
            f"{course.get('physical_pen_steps')} | {simulation.get('document_ratio', 'n/a')}x | "
            f"{100 * float(course.get('field_ink_coverage') or 0):.2f}% | "
            f"{float(course.get('label_feature_overlap_mm') or 0):.3f} mm | "
            f"0.000 mm | "
            f"{'PASS' if course.get('passed') else 'FAIL'} |"
        )
    if report["failures"]:
        lines.extend(["", "## Failures", ""])
        lines.extend(f"- {failure}" for failure in report["failures"])
    lines.extend(
        [
            "",
            "The collection remains review-only because its built-in physical pen widths are nominal and unmeasured.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Report only; do not write QA files/checksums.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    directory = args.directory.resolve()
    series_path = directory / "golf-course-series.json"
    if not series_path.is_file():
        print(f"qa-golf: {series_path} is missing", file=sys.stderr)
        return 2
    series = json.loads(series_path.read_text(encoding="utf-8"))
    artifacts = series.get("artifacts") or []
    failures: list[str] = []
    if series.get("series_id") != EXPECTED_SERIES_ID:
        failures.append(f"series_id must be {EXPECTED_SERIES_ID}")
    if series.get("release_id") != EXPECTED_RELEASE_ID:
        failures.append(f"release_id must be {EXPECTED_RELEASE_ID}")
    if series.get("rendering_preset") != EXPECTED_RENDERING_PRESET:
        failures.append(f"series rendering_preset must be {EXPECTED_RENDERING_PRESET}")
    if series.get("title") != EXPECTED_SERIES_TITLE:
        failures.append(f"series title must be {EXPECTED_SERIES_TITLE}")
    if series.get("catalog_count") != EXPECTED_CATALOG_COUNT:
        failures.append(
            f"packaged catalog must contain exactly {EXPECTED_CATALOG_COUNT} courses"
        )
    if (
        series.get("artifact_count") != EXPECTED_CATALOG_COUNT
        or len(artifacts) != EXPECTED_CATALOG_COUNT
    ):
        failures.append(
            f"collection must contain exactly {EXPECTED_CATALOG_COUNT} artifacts"
        )
    checksums_passed, checksum_failures = _verify_checksums(directory)
    failures.extend(checksum_failures)

    masters = [directory / str(artifact["svg"]) for artifact in artifacts]
    format_result = _run(
        [
            sys.executable,
            str(FORMAT_TOOL),
            *[str(path) for path in masters],
            "--warnings-as-errors",
            "--quiet",
        ]
    )
    if format_result.returncode != 0:
        failures.append(
            "binding format validation failed: "
            + (format_result.stdout or format_result.stderr).strip()
        )
    retrace_result = _run(
        [
            sys.executable,
            str(RETRACE_TOOL),
            *[str(path) for path in masters],
            "--scope",
            "all-physical",
            "--assert-safe",
            "--quiet",
            "--json",
        ]
    )
    retrace_payload: dict[str, Any] = {}
    try:
        retrace_payload = json.loads(retrace_result.stdout)
    except json.JSONDecodeError:
        pass
    if (
        retrace_result.returncode != 0
        or retrace_payload.get("certified_zero_retrace") is not True
    ):
        failures.append("all-physical zero-retrace certification failed")

    courses: list[dict[str, Any]] = []
    for artifact in artifacts:
        course, course_failures = _course_record(directory, artifact)
        courses.append(course)
        failures.extend(
            f"{course['subject_id']}: {failure}" for failure in course_failures
        )
    report = {
        "schema_version": 1,
        "series_id": series.get("series_id"),
        "artifact_count": len(artifacts),
        "passed": not failures,
        "review_mode": series.get("mode"),
        "production_ready": False,
        "checks": {
            "checksums": checksums_passed,
            "binding_format": format_result.returncode == 0,
            "zero_physical_retrace": retrace_payload.get("certified_zero_retrace")
            is True,
            "plot_simulation": all(course.get("plotsim") for course in courses),
            "v4_surface_contract": all(
                course.get("v4_style", {}).get("passed") is True for course in courses
            ),
        },
        "max_document_travel_ratio": max(
            float(course.get("plotsim", {}).get("document_ratio", 0))
            for course in courses
        )
        if courses
        else 0.0,
        "max_field_ink_coverage": (
            max(float(course.get("field_ink_coverage") or 0) for course in courses)
            if courses
            else 0.0
        ),
        "courses": courses,
        "failures": failures,
    }
    if not args.no_write:
        (directory / "qa-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (directory / "qa-report.md").write_text(_markdown(report), encoding="utf-8")
        _refresh_checksums(directory)
    print(
        f"{'PASS' if report['passed'] else 'FAIL'}: {len(courses)} courses; "
        f"max travel {report['max_document_travel_ratio']:.2f}x; "
        f"max field coverage {100 * report['max_field_ink_coverage']:.2f}%; "
        f"zero retrace={report['checks']['zero_physical_retrace']}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
