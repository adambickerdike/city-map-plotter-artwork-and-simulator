#!/usr/bin/env python3
"""Audit the v4 hiking release as full-field pen-plotter compositions.

The release validator proves source and metadata integrity.  This companion
audit measures the paths that actually reached paper: geographic density,
field occupancy, continuous contour levels, the open bottom elevation band,
and the deliberate absence of inset panels, profile boxes and fall-line
scratches.

Examples::

    .venv/bin/python tools/audit_hiking_composition.py \
        output/hiking-series-paired-v4-2026-08-04

    .venv/bin/python tools/audit_hiking_composition.py output/hiking-series \
        --json review-output/composition.json \
        --markdown review-output/composition.md --fail-on-gate
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET

from shapely.geometry import LineString, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


FULL_FIELD_POLICY_ID = "full-field-continuous-context-v2"
FORBIDDEN_ROLES = frozenset(
    {
        "context-detail-inset-frame",
        "context-detail-inset-label",
        "context-detail-north-arrow",
        "context-detail-north-arrow-head",
        "context-detail-north-label",
        "profile-frame",
        "source-derived-dem-fall-line-hachure",
    }
)
PROFILE_ROLES = frozenset(
    {
        "profile-baseline",
        "source-elevation-profile",
        "profile-chainage-tick",
        "profile-chainage-station",
    }
)
EXPECTED_CHAINAGE_IDS = frozenset("ABCDE")
FAMILY_BY_LAYER = {
    "context_roads": "roads",
    "context_water": "water",
    "context_woodland": "green",
    "context_landcover": "green",
    "context_relief": "terrain",
    "context_relief_index": "terrain",
}
FAMILY_ORDER = ("roads", "water", "green", "terrain")
INDEX_CONTOUR_EQUIVALENT_WIDTH_RATIO = 0.40 / 0.25

# The density bands are the measured visual grammar of the approved controls:
# West Highland Way / Great Glen Way for context and Tour des Refuges for
# relief.  Maximums are plot-load gates as well as aesthetic limits.
GATES: dict[str, float] = {
    "detailed_min_density_mm_per_mm2": 0.075,
    "detailed_max_density_mm_per_mm2": 0.180,
    "relief_min_density_mm_per_mm2": 0.160,
    "relief_max_density_mm_per_mm2": 0.350,
    "detailed_min_occupied_fraction": 0.200,
    "relief_min_occupied_fraction": 0.300,
    "minimum_strong_family_mm": 5.0,
    "grid_cell_min_ink_mm": 0.25,
    "profile_band_height_mm": 13.8,
    "profile_gap_mm": 1.8,
    # A relief plate may sit below the ordinary minimum-density band only when
    # the frozen DEM itself proves that the whole field is nearly level.  This
    # is deliberately stricter than looking at the emitted SVG: sparse output
    # can never classify its own source as flat.
    "flat_max_smoothed_slope_deg": 0.75,
    "flat_max_contour_span_m": 150.0,
    "flat_min_source_contour_levels": 4.0,
    "flat_min_dem_valid_fraction": 0.95,
    "flat_min_gradient_sample_count": 1000.0,
    # Marine plates are assessed against full page area by default.  A narrow
    # alternative denominator is available only when a frozen DEM proves that
    # most of that rectangle is intentionally masked sea and still carries a
    # substantial, mountainous contour stack on the terrestrial domain.
    "marine_min_terrestrial_valid_fraction": 0.20,
    "marine_max_terrestrial_valid_fraction": 0.50,
    "marine_min_source_contour_levels": 8.0,
    "marine_min_source_contour_paths": 32.0,
    "marine_min_contour_span_m": 200.0,
    "marine_min_source_relief_strokes": 3.0,
    "marine_min_rendered_contour_levels": 8.0,
    "marine_min_rendered_contour_paths": 32.0,
    "marine_min_rendered_terrain_mm": 100.0,
}

TOKEN_RE = re.compile(r"[MLZmlz]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass
class GridMetrics:
    columns: int
    rows: int
    occupied_cells: int
    occupied_fraction: float
    occupied_columns: int
    occupied_rows: int
    maximum_empty_border_columns: int
    maximum_empty_border_rows: int


@dataclass
class ContractMetrics:
    full_field_policy: str | None
    context_view_path_count: int
    forbidden_role_counts: dict[str, int]
    source_terrain_field: str | None
    source_terrain_ref: str | None
    source_contour_level_count: int
    rendered_contour_level_count: int
    rendered_contour_levels_m: list[float]
    profile_path_count: int
    profile_is_unboxed: bool
    profile_is_in_bottom_band: bool
    map_chainage_ids: list[str]
    profile_chainage_ids: list[str]
    status: str
    findings: list[str]


@dataclass
class MainMapMetrics:
    bounds_mm: dict[str, float]
    bounds_source: str
    family_mm: dict[str, float]
    useful_mm: float
    density_mm_per_mm2: float
    physical_equivalent_density_mm_per_mm2: float
    flat_terrain_source_evidence: bool
    marine_terrestrial_source_evidence: bool
    marine_terrestrial_valid_fraction: float | None
    marine_terrestrial_density_mm_per_mm2: float | None
    marine_terrestrial_normalization_applied: bool
    minimum_density_exception_applied: bool
    strong_family_count: int
    geography_grid: GridMetrics
    visual_grid: GridMetrics
    status: str
    findings: list[str]


@dataclass
class ArtifactMetrics:
    artifact: str
    subject_id: str
    variant_id: str
    svg: str
    plot_json: str | None
    main_map: MainMapMetrics | None
    contract: ContractMetrics
    status: str
    findings: list[str]


@dataclass(frozen=True)
class TerrainEvidence:
    """The exact terrain bundle assigned to one rendered variant."""

    field: str | None
    source_ref: str | None
    contour_levels_m: frozenset[float]
    flat_source_evidence: bool
    marine_terrestrial_valid_fraction: float | None = None
    source_contour_path_count: int = 0


def _path_subpaths(path_data: str) -> list[list[tuple[float, float]]]:
    """Parse the renderer's intentionally small SVG path subset (M/L/Z)."""

    tokens = TOKEN_RE.findall(path_data)
    paths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    start: tuple[float, float] | None = None
    command: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token.upper()
            index += 1
            if command == "Z":
                if current and start is not None and current[-1] != start:
                    current.append(start)
                if len(current) >= 2:
                    paths.append(current)
                current = []
                start = None
                command = None
            continue
        if command not in {"M", "L"} or index + 1 >= len(tokens):
            raise ValueError(f"unsupported or malformed SVG path: {path_data[:80]!r}")
        point = (float(tokens[index]), float(tokens[index + 1]))
        index += 2
        if command == "M":
            if len(current) >= 2:
                paths.append(current)
            current = [point]
            start = point
            command = "L"
        else:
            current.append(point)
    if len(current) >= 2:
        paths.append(current)
    return paths


def _element_geometries(element: ET.Element) -> list[LineString]:
    path_data = element.get("d")
    if not path_data:
        return []
    return [LineString(points) for points in _path_subpaths(path_data)]


def _clip_geometries(
    geometries: Iterable[BaseGeometry], rect: Rect
) -> list[BaseGeometry]:
    clip = box(rect.x, rect.y, rect.right, rect.bottom)
    result: list[BaseGeometry] = []
    for geometry in geometries:
        clipped = geometry.intersection(clip)
        if not clipped.is_empty and clipped.length > 1e-9:
            result.append(clipped)
    return result


def _grid_metrics(
    geometries: Sequence[BaseGeometry],
    rect: Rect,
    *,
    columns: int,
    rows: int,
) -> GridMetrics:
    if rect.width <= 0.0 or rect.height <= 0.0:
        raise ValueError("grid bounds must have positive dimensions")
    merged = unary_union(tuple(geometries)) if geometries else LineString()
    occupied: set[tuple[int, int]] = set()
    threshold = GATES["grid_cell_min_ink_mm"]
    for row in range(rows):
        top = rect.y + rect.height * row / rows
        bottom = rect.y + rect.height * (row + 1) / rows
        for column in range(columns):
            left = rect.x + rect.width * column / columns
            right = rect.x + rect.width * (column + 1) / columns
            cell = box(left, top, right, bottom)
            if not merged.is_empty and merged.intersection(cell).length >= threshold:
                occupied.add((column, row))

    occupied_columns = {column for column, _row in occupied}
    occupied_rows = {row for _column, row in occupied}

    def border_run(indices: set[int], count: int) -> int:
        if not indices:
            return count
        return max(min(indices), count - 1 - max(indices))

    return GridMetrics(
        columns=columns,
        rows=rows,
        occupied_cells=len(occupied),
        occupied_fraction=round(len(occupied) / (columns * rows), 4),
        occupied_columns=len(occupied_columns),
        occupied_rows=len(occupied_rows),
        maximum_empty_border_columns=border_run(occupied_columns, columns),
        maximum_empty_border_rows=border_run(occupied_rows, rows),
    )


def _family_lengths(
    geometries: dict[str, list[BaseGeometry]],
) -> dict[str, float]:
    return {
        family: round(sum(geometry.length for geometry in geometries[family]), 3)
        for family in FAMILY_ORDER
    }


def _companion_plot_path(svg_path: Path) -> Path:
    return svg_path.with_suffix(".plot.json")


def _read_plot_payload(svg_path: Path) -> tuple[Path | None, dict[str, Any] | None]:
    plot_path = _companion_plot_path(svg_path)
    if not plot_path.is_file():
        return None, None
    try:
        payload = json.loads(plot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {plot_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{plot_path} does not contain a JSON object")
    return plot_path, payload


def _inner_field(plot_payload: dict[str, Any]) -> Rect:
    try:
        field = plot_payload["page"]["zones_mm"]["map_field"]
        return Rect(
            float(field["x"]) + 3.0,
            float(field["y"]) + 3.0,
            float(field["width"]) - 6.0,
            float(field["height"]) - 6.0,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("plot manifest has no valid page.zones_mm.map_field") from exc


def _composition_rects(
    plot_payload: dict[str, Any], *, has_profile: bool
) -> tuple[Rect, Rect | None]:
    inner = _inner_field(plot_payload)
    if not has_profile:
        return inner, None
    profile_height = GATES["profile_band_height_mm"]
    gap = GATES["profile_gap_mm"]
    map_height = inner.height - profile_height - gap
    if map_height <= 0.0:
        raise ValueError("v4 profile band leaves no positive map field")
    main = Rect(inner.x, inner.y, inner.width, map_height)
    profile = Rect(inner.x, main.bottom + gap, inner.width, profile_height)
    return main, profile


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _terrain_contour_levels(terrain: dict[str, Any]) -> frozenset[float]:
    contours = terrain.get("contours")
    result: set[float] = set()
    for contour in contours if isinstance(contours, list) else []:
        if not isinstance(contour, dict) or not contour.get("paths"):
            continue
        elevation = _finite_float(contour.get("elevation_m"))
        if elevation is not None:
            result.add(elevation)
    return frozenset(result)


def _has_truthful_flat_terrain_evidence(
    terrain: dict[str, Any], contour_levels: frozenset[float]
) -> bool:
    """Return true only for a frozen DEM that positively proves a flat field.

    The exception is intentionally source-driven.  It cannot be inferred from
    a sparse SVG, a short route profile, a place name, or a manually supplied
    flag.  The DEM derivation must disclose its smoothed whole-field maximum,
    show that the relief tracer found no eligible strokes, retain a useful
    factual contour stack, and bind the evidence to a frozen raster digest.
    """

    if terrain.get("status") != "source-derived-dtm-relief":
        return False
    source_ref = terrain.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        return False
    if not any(
        _valid_sha256(terrain.get(key))
        for key in (
            "derived_window_sha256",
            "source_window_sha256",
            "source_snapshot_sha256",
        )
    ):
        return False
    valid_fraction = _finite_float(terrain.get("derived_window_valid_fraction"))
    if valid_fraction is None or valid_fraction <= 0.0 or valid_fraction > 1.0:
        return False
    full_raster_coverage = valid_fraction >= GATES["flat_min_dem_valid_fraction"]
    surface_domain = terrain.get("surface_domain_policy")
    terrestrial_domain_coverage = False
    if isinstance(surface_domain, dict):
        applies_to = surface_domain.get("applies_to")
        bathymetric_cells = surface_domain.get("bathymetric_cell_count")
        terrestrial_domain_coverage = (
            surface_domain.get("id") == "terrestrial-nonnegative-source-cells-v1"
            and isinstance(bathymetric_cells, int)
            and not isinstance(bathymetric_cells, bool)
            and bathymetric_cells > 0
            and surface_domain.get("elevation_values_clamped") is False
            and isinstance(applies_to, list)
            and all(isinstance(item, str) for item in applies_to)
            and {"contours", "fall-lines"} <= set(applies_to)
        )
    if not full_raster_coverage and not terrestrial_domain_coverage:
        return False
    if len(contour_levels) < int(GATES["flat_min_source_contour_levels"]):
        return False
    if max(contour_levels) - min(contour_levels) > GATES["flat_max_contour_span_m"]:
        return False

    stroke_policy = terrain.get("relief_stroke_policy")
    if not isinstance(stroke_policy, dict):
        return False
    if stroke_policy.get("seed_slope_policy") != "global-page-smoothed-adaptive-v1":
        return False
    if (
        stroke_policy.get("candidate_count") != 0
        or stroke_policy.get("retained_count") != 0
    ):
        return False
    adaptive = stroke_policy.get("adaptive_seed_slope")
    if not isinstance(adaptive, dict):
        return False
    activation = _finite_float(adaptive.get("activation_slope_deg"))
    smoothed_maximum = _finite_float(adaptive.get("page_smoothed_maximum_slope_deg"))
    lattice_maximum = _finite_float(
        adaptive.get("page_smoothed_lattice_maximum_slope_deg")
    )
    if activation is None or smoothed_maximum is None or lattice_maximum is None:
        return False
    gradient_sample_count = _finite_float(
        adaptive.get("page_smoothed_gradient_sample_count")
    )
    if (
        terrestrial_domain_coverage
        and not full_raster_coverage
        and (
            gradient_sample_count is None
            or gradient_sample_count < GATES["flat_min_gradient_sample_count"]
        )
    ):
        return False
    permitted_maximum = min(activation, GATES["flat_max_smoothed_slope_deg"])
    return (
        activation > 0.0
        and smoothed_maximum < permitted_maximum
        and lattice_maximum < permitted_maximum
    )


def _marine_terrestrial_valid_fraction(
    terrain: dict[str, Any], contour_levels: frozenset[float]
) -> float | None:
    """Return a defensible terrestrial denominator for a marine relief field.

    This is source evidence, not an SVG-density heuristic.  It fails closed
    unless the terrain bundle is bound to frozen source and derived windows,
    explicitly masks bathymetry without clamping it into invented land, and
    contains both a broad contour range and a substantial source path stack.
    """

    if terrain.get("status") != "source-derived-dtm-relief":
        return None
    source_ref = terrain.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        return None
    if not all(
        _valid_sha256(terrain.get(key))
        for key in (
            "source_window_sha256",
            "derived_window_sha256",
            "source_tile_manifest_sha256",
        )
    ):
        return None

    valid_fraction = _finite_float(terrain.get("derived_window_valid_fraction"))
    if (
        valid_fraction is None
        or valid_fraction < GATES["marine_min_terrestrial_valid_fraction"]
        or valid_fraction > GATES["marine_max_terrestrial_valid_fraction"]
    ):
        return None
    surface_domain = terrain.get("surface_domain_policy")
    if not isinstance(surface_domain, dict):
        return None
    applies_to = surface_domain.get("applies_to")
    bathymetric_cells = surface_domain.get("bathymetric_cell_count")
    minimum_elevation = _finite_float(surface_domain.get("minimum_elevation_m"))
    if not (
        surface_domain.get("id") == "terrestrial-nonnegative-source-cells-v1"
        and minimum_elevation == 0.0
        and isinstance(bathymetric_cells, int)
        and not isinstance(bathymetric_cells, bool)
        and bathymetric_cells > 0
        and surface_domain.get("elevation_values_clamped") is False
        and isinstance(applies_to, list)
        and all(isinstance(item, str) for item in applies_to)
        and {"contours", "fall-lines"} <= set(applies_to)
    ):
        return None

    contours = terrain.get("contours")
    if not isinstance(contours, list):
        return None
    source_path_count = sum(
        len(contour.get("paths", []))
        for contour in contours
        if isinstance(contour, dict) and isinstance(contour.get("paths"), list)
    )
    if (
        len(contour_levels) < int(GATES["marine_min_source_contour_levels"])
        or source_path_count < int(GATES["marine_min_source_contour_paths"])
        or max(contour_levels) - min(contour_levels)
        < GATES["marine_min_contour_span_m"]
    ):
        return None

    stroke_policy = terrain.get("relief_stroke_policy")
    if not isinstance(stroke_policy, dict):
        return None
    candidate_count = stroke_policy.get("candidate_count")
    retained_count = stroke_policy.get("retained_count")
    if not (
        stroke_policy.get("algorithm_id") == "dem-gradient-fall-line-v1"
        and _valid_sha256(stroke_policy.get("geometry_manifest_sha256"))
        and isinstance(candidate_count, int)
        and not isinstance(candidate_count, bool)
        and isinstance(retained_count, int)
        and not isinstance(retained_count, bool)
        and candidate_count >= retained_count
        and retained_count >= int(GATES["marine_min_source_relief_strokes"])
    ):
        return None
    return valid_fraction


def _marine_terrestrial_density(
    *,
    raw_density: float,
    map_area_mm2: float,
    family_mm: dict[str, float],
    terrestrial_valid_fraction: float | None,
    rendered_contour_level_count: int,
    rendered_contour_path_count: int,
) -> float | None:
    """Normalize only terrain ink to proven terrestrial area for a min gate."""

    terrain_mm = family_mm.get("terrain", 0.0)
    if (
        terrestrial_valid_fraction is None
        or map_area_mm2 <= 0.0
        or terrain_mm < GATES["marine_min_rendered_terrain_mm"]
        or rendered_contour_level_count
        < int(GATES["marine_min_rendered_contour_levels"])
        or rendered_contour_path_count < int(GATES["marine_min_rendered_contour_paths"])
    ):
        return None
    raw_terrain_density = terrain_mm / map_area_mm2
    nonterrain_density = max(0.0, raw_density - raw_terrain_density)
    return nonterrain_density + raw_terrain_density / terrestrial_valid_fraction


def _terrain_evidence(
    plot_payload: dict[str, Any] | None, *, variant_id: str
) -> TerrainEvidence:
    if plot_payload is None:
        return TerrainEvidence(None, None, frozenset(), False)
    try:
        context = plot_payload["catalog_record"]["context"]
    except (KeyError, TypeError):
        return TerrainEvidence(None, None, frozenset(), False)
    if not isinstance(context, dict):
        return TerrainEvidence(None, None, frozenset(), False)

    field = "terrain"
    terrain = context.get(field)
    if variant_id == "terrain-relief" and isinstance(
        context.get("relief_terrain"), dict
    ):
        field = "relief_terrain"
        terrain = context[field]
    elif variant_id == "detailed-map":
        rendering = plot_payload.get("rendering")
        fallback = (
            rendering.get("detailed_terrain_source_policy")
            if isinstance(rendering, dict)
            else None
        )
        if (
            isinstance(fallback, dict)
            and fallback.get("policy_id")
            == "full-field-relief-fallback-for-sparse-native-context-v1"
        ):
            candidate = context.get("relief_terrain")
            selected_source_ref = fallback.get("selected_source_ref")
            if (
                isinstance(candidate, dict)
                and isinstance(selected_source_ref, str)
                and selected_source_ref
                and candidate.get("source_ref") == selected_source_ref
            ):
                field = "relief_terrain"
                terrain = candidate
            else:
                # The renderer declared a factual source switch that the
                # frozen catalog cannot prove.  Return no evidence so both
                # level and source reconciliation fail closed below.
                return TerrainEvidence(None, None, frozenset(), False)
    if not isinstance(terrain, dict):
        return TerrainEvidence(None, None, frozenset(), False)

    levels = _terrain_contour_levels(terrain)
    source_ref = terrain.get("source_ref")
    source_ref = source_ref if isinstance(source_ref, str) and source_ref else None
    contours = terrain.get("contours")
    source_contour_path_count = sum(
        len(contour.get("paths", []))
        for contour in (contours if isinstance(contours, list) else [])
        if isinstance(contour, dict) and isinstance(contour.get("paths"), list)
    )
    return TerrainEvidence(
        field=field,
        source_ref=source_ref,
        contour_levels_m=levels,
        flat_source_evidence=_has_truthful_flat_terrain_evidence(terrain, levels),
        marine_terrestrial_valid_fraction=_marine_terrestrial_valid_fraction(
            terrain, levels
        ),
        source_contour_path_count=source_contour_path_count,
    )


def _main_status(
    *,
    variant_id: str,
    density: float,
    grid: GridMetrics,
    family_mm: dict[str, float],
    physical_equivalent_density: float | None = None,
    flat_terrain_source_evidence: bool = False,
    marine_terrestrial_density: float | None = None,
) -> tuple[str, list[str], bool]:
    findings: list[str] = []
    if physical_equivalent_density is None:
        physical_equivalent_density = density
    minimum_density = GATES[
        "relief_min_density_mm_per_mm2"
        if variant_id == "terrain-relief"
        else "detailed_min_density_mm_per_mm2"
    ]
    maximum_density = GATES[
        "relief_max_density_mm_per_mm2"
        if variant_id == "terrain-relief"
        else "detailed_max_density_mm_per_mm2"
    ]
    minimum_occupancy = GATES[
        "relief_min_occupied_fraction"
        if variant_id == "terrain-relief"
        else "detailed_min_occupied_fraction"
    ]
    flat_density_exception_applied = (
        variant_id == "terrain-relief"
        and density < minimum_density
        and flat_terrain_source_evidence
    )
    marine_density_normalization_applied = (
        variant_id == "terrain-relief"
        and density < minimum_density
        and marine_terrestrial_density is not None
        and marine_terrestrial_density >= minimum_density
    )
    minimum_density_exception_applied = (
        flat_density_exception_applied or marine_density_normalization_applied
    )
    if density < minimum_density and not minimum_density_exception_applied:
        findings.append(
            f"geography density {density:.3f} is below {minimum_density:.3f} mm/mm2"
        )
    if physical_equivalent_density > maximum_density:
        findings.append(
            "width-normalized geography density "
            f"{physical_equivalent_density:.3f} exceeds "
            f"{maximum_density:.3f} mm/mm2"
        )
    if grid.occupied_fraction < minimum_occupancy:
        findings.append(
            f"geography occupancy {grid.occupied_fraction:.1%} is below "
            f"{minimum_occupancy:.1%}"
        )
    strong_families = sum(
        length >= GATES["minimum_strong_family_mm"] for length in family_mm.values()
    )
    if strong_families < 2:
        findings.append("main map carries fewer than two strong geography families")
    return (
        "fail" if findings else "pass",
        findings,
        minimum_density_exception_applied,
    )


def _profile_geometries_in_band(
    elements: Sequence[ET.Element], profile_rect: Rect | None
) -> tuple[int, bool]:
    profile_geometries = [
        geometry
        for element in elements
        if element.get("data-role") in PROFILE_ROLES
        for geometry in _element_geometries(element)
    ]
    if profile_rect is None or not profile_geometries:
        return len(profile_geometries), False
    permitted = box(
        profile_rect.x - 1e-6,
        profile_rect.y - 1e-6,
        profile_rect.right + 1e-6,
        profile_rect.bottom + 1e-6,
    )
    return len(profile_geometries), all(
        permitted.covers(item) for item in profile_geometries
    )


def audit_artifact(svg_path: Path) -> ArtifactMetrics:
    root = ET.parse(svg_path).getroot()
    plot_path, plot_payload = _read_plot_payload(svg_path)
    subject_id = svg_path.stem.split("--", 1)[0]
    variant_id = svg_path.stem.split("--", 1)[1] if "--" in svg_path.stem else ""
    artifact_id = svg_path.stem
    rendering: dict[str, Any] = {}
    if plot_payload is not None:
        artifact_id = str(plot_payload.get("artifact_id", artifact_id))
        subject_id = str(plot_payload.get("subject_id", subject_id))
        variant_id = str(plot_payload.get("variant_id", variant_id))
        raw_rendering = plot_payload.get("rendering")
        if isinstance(raw_rendering, dict):
            rendering = raw_rendering

    elements = [
        element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "path"
    ]
    roles = [str(element.get("data-role", "")) for element in elements]
    forbidden_counts = {
        role: roles.count(role) for role in sorted(FORBIDDEN_ROLES) if role in roles
    }
    context_view_count = sum(
        bool(element.get("data-context-view")) for element in elements
    )
    rendered_contours = [
        element
        for element in elements
        if element.get("data-role") == "source-derived-dtm-contour"
    ]
    rendered_levels = sorted(
        {
            value
            for element in rendered_contours
            if (value := _finite_float(element.get("data-elevation-m"))) is not None
        }
    )
    rendered_source_refs = {
        element.get("data-source-ref") for element in rendered_contours
    }
    terrain_evidence = _terrain_evidence(plot_payload, variant_id=variant_id)
    source_levels = terrain_evidence.contour_levels_m
    map_chainage_ids = sorted(
        {
            str(element.get("data-chainage-id"))
            for element in elements
            if element.get("data-role") == "map-chainage-label"
            and element.get("data-chainage-id")
        }
    )
    profile_chainage_ids = sorted(
        {
            str(element.get("data-chainage-id"))
            for element in elements
            if element.get("data-role") == "profile-chainage-label"
            and element.get("data-chainage-id")
        }
    )
    representation = rendering.get("route_representation")
    full_field_policy = (
        str(representation.get("sectional_detail_policy", "")) or None
        if isinstance(representation, dict)
        else None
    )

    main_map: MainMapMetrics | None = None
    findings: list[str] = []
    profile_path_count = 0
    profile_in_bottom_band = False
    if plot_payload is None:
        findings.append("main-map audit unavailable: companion .plot.json is missing")
    else:
        has_profile = any(role == "source-elevation-profile" for role in roles)
        main_rect, profile_rect = _composition_rects(
            plot_payload, has_profile=has_profile
        )
        profile_path_count, profile_in_bottom_band = _profile_geometries_in_band(
            elements, profile_rect
        )
        main_geometries: dict[str, list[BaseGeometry]] = defaultdict(list)
        index_contour_geometries: list[BaseGeometry] = []
        route_geometries: list[BaseGeometry] = []
        for element in elements:
            if element.get("data-context-view"):
                continue
            layer = element.get("data-logical-layer", "")
            geometries = _element_geometries(element)
            family = FAMILY_BY_LAYER.get(layer)
            if family is not None:
                main_geometries[family].extend(geometries)
            if (
                layer == "context_relief_index"
                and element.get("data-role") == "source-derived-dtm-contour"
            ):
                index_contour_geometries.extend(geometries)
            if layer == "hero_route" and str(element.get("data-role", "")).startswith(
                "source-sampled-route"
            ):
                route_geometries.extend(geometries)
        clipped_main = {
            family: _clip_geometries(main_geometries[family], main_rect)
            for family in FAMILY_ORDER
        }
        family_mm = _family_lengths(clipped_main)
        useful_mm = sum(family_mm.values())
        density = useful_mm / main_rect.area if main_rect.area > 0.0 else 0.0
        clipped_index_contours = _clip_geometries(
            index_contour_geometries,
            main_rect,
        )
        index_contour_mm = sum(
            float(geometry.length) for geometry in clipped_index_contours
        )
        physical_equivalent_useful_mm = useful_mm + (
            INDEX_CONTOUR_EQUIVALENT_WIDTH_RATIO - 1.0
        ) * index_contour_mm
        physical_equivalent_density = (
            physical_equivalent_useful_mm / main_rect.area
            if main_rect.area > 0.0
            else 0.0
        )
        geography = [
            geometry for family in FAMILY_ORDER for geometry in clipped_main[family]
        ]
        clipped_route = _clip_geometries(route_geometries, main_rect)
        geography_grid = _grid_metrics(geography, main_rect, columns=8, rows=7)
        visual_grid = _grid_metrics(
            [*geography, *clipped_route], main_rect, columns=8, rows=7
        )
        marine_valid_fraction = (
            terrain_evidence.marine_terrestrial_valid_fraction
            if variant_id == "terrain-relief"
            else None
        )
        marine_density = _marine_terrestrial_density(
            raw_density=density,
            map_area_mm2=main_rect.area,
            family_mm=family_mm,
            terrestrial_valid_fraction=marine_valid_fraction,
            rendered_contour_level_count=len(rendered_levels),
            rendered_contour_path_count=len(rendered_contours),
        )
        main_status, main_findings, minimum_density_exception_applied = _main_status(
            variant_id=variant_id,
            density=density,
            physical_equivalent_density=physical_equivalent_density,
            grid=geography_grid,
            family_mm=family_mm,
            flat_terrain_source_evidence=terrain_evidence.flat_source_evidence,
            marine_terrestrial_density=marine_density,
        )
        relief_minimum_density = GATES["relief_min_density_mm_per_mm2"]
        marine_normalization_applied = (
            variant_id == "terrain-relief"
            and density < relief_minimum_density
            and marine_density is not None
            and marine_density >= relief_minimum_density
        )
        main_map = MainMapMetrics(
            bounds_mm={
                "x": round(main_rect.x, 3),
                "y": round(main_rect.y, 3),
                "width": round(main_rect.width, 3),
                "height": round(main_rect.height, 3),
            },
            bounds_source="plot-json-map-field-plus-v4-open-profile-band",
            family_mm=family_mm,
            useful_mm=round(useful_mm, 3),
            density_mm_per_mm2=round(density, 4),
            physical_equivalent_density_mm_per_mm2=round(
                physical_equivalent_density,
                4,
            ),
            flat_terrain_source_evidence=terrain_evidence.flat_source_evidence,
            marine_terrestrial_source_evidence=marine_valid_fraction is not None,
            marine_terrestrial_valid_fraction=(
                round(marine_valid_fraction, 6)
                if marine_valid_fraction is not None
                else None
            ),
            marine_terrestrial_density_mm_per_mm2=(
                round(marine_density, 4) if marine_density is not None else None
            ),
            marine_terrestrial_normalization_applied=marine_normalization_applied,
            minimum_density_exception_applied=minimum_density_exception_applied,
            strong_family_count=sum(
                length >= GATES["minimum_strong_family_mm"]
                for length in family_mm.values()
            ),
            geography_grid=geography_grid,
            visual_grid=visual_grid,
            status=main_status,
            findings=main_findings,
        )

    contract_findings: list[str] = []
    if full_field_policy != FULL_FIELD_POLICY_ID:
        contract_findings.append("full-field continuous-context policy is missing")
    if context_view_count:
        contract_findings.append(
            f"{context_view_count} path(s) still use an inset data-context-view"
        )
    if forbidden_counts:
        contract_findings.append(
            "forbidden v4 roles remain: "
            + ", ".join(f"{role}={count}" for role, count in forbidden_counts.items())
        )
    if profile_path_count == 0:
        contract_findings.append("open bottom elevation profile is missing")
    elif not profile_in_bottom_band:
        contract_findings.append("profile geometry escapes the open bottom band")
    if set(map_chainage_ids) != EXPECTED_CHAINAGE_IDS:
        contract_findings.append("map A-E chainage station set is incomplete")
    if map_chainage_ids != profile_chainage_ids:
        contract_findings.append("map/profile chainage station IDs do not match")
    minimum_levels = min(4, len(source_levels))
    if (
        len(rendered_levels) < minimum_levels
        or not set(rendered_levels) <= source_levels
    ):
        contract_findings.append("rendered contour levels do not cover source evidence")
    if rendered_contours and terrain_evidence.source_ref is None:
        contract_findings.append("rendered contours have no assigned terrain source")
    elif rendered_contours and rendered_source_refs != {terrain_evidence.source_ref}:
        contract_findings.append(
            "rendered contour source does not match the variant terrain source"
        )
    if variant_id == "detailed-map" and len(rendered_levels) > min(
        8, len(source_levels)
    ):
        contract_findings.append("detailed-map exceeds its 4-8 contour-level grammar")
    if variant_id == "terrain-relief" and not any(
        role == "source-derived-contour-altitude-label" for role in roles
    ):
        contract_findings.append("relief edition has no factual contour-altitude label")

    contract = ContractMetrics(
        full_field_policy=full_field_policy,
        context_view_path_count=context_view_count,
        forbidden_role_counts=forbidden_counts,
        source_terrain_field=terrain_evidence.field,
        source_terrain_ref=terrain_evidence.source_ref,
        source_contour_level_count=len(source_levels),
        rendered_contour_level_count=len(rendered_levels),
        rendered_contour_levels_m=rendered_levels,
        profile_path_count=profile_path_count,
        profile_is_unboxed="profile-frame" not in roles,
        profile_is_in_bottom_band=profile_in_bottom_band,
        map_chainage_ids=map_chainage_ids,
        profile_chainage_ids=profile_chainage_ids,
        status="fail" if contract_findings else "pass",
        findings=contract_findings,
    )
    status = (
        "fail"
        if findings
        or contract.status == "fail"
        or (main_map is not None and main_map.status == "fail")
        else "pass"
    )
    return ArtifactMetrics(
        artifact=artifact_id,
        subject_id=subject_id,
        variant_id=variant_id,
        svg=str(svg_path.resolve()),
        plot_json=str(plot_path.resolve()) if plot_path is not None else None,
        main_map=main_map,
        contract=contract,
        status=status,
        findings=findings,
    )


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _discover_svg_paths(inputs: Sequence[Path]) -> list[Path]:
    paths: set[Path] = set()
    for item in inputs:
        if item.is_dir():
            candidates = item.rglob("*.svg")
        elif item.is_file() and item.suffix.lower() == ".svg":
            candidates = (item,)
        else:
            raise ValueError(f"input is not an SVG or directory: {item}")
        for candidate in candidates:
            if ".pen-" not in candidate.name:
                paths.add(candidate.resolve())
    return sorted(paths)


def _markdown_report(artifacts: Sequence[ArtifactMetrics]) -> str:
    lines = [
        "# Hiking v4 composition audit",
        "",
        "Measured from emitted SVG paths. Main-map bounds come from each companion "
        "plot manifest and exclude the unboxed 13.8 mm bottom elevation band.",
        "",
        "## Gates",
        "",
    ]
    lines.extend(f"- `{key}`: {value:g}" for key, value in GATES.items())
    lines.extend(
        [
            "",
            "The density bands are the approved full-field visual grammar: "
            "West Highland Way / Great Glen Way for context and Tour des Refuges "
            "for continuous relief. Insets, profile frames and fall-line hachures "
            "are hard failures regardless of density.",
            "A below-band relief density is accepted only when frozen source "
            "evidence proves either a genuinely flat field or a majority-marine "
            "field with substantial terrain on its terrestrial domain. Marine "
            "normalization changes only the terrain denominator used by the "
            "minimum-density gate. Occupancy, family diversity, raw maximum "
            "density and contour provenance remain hard gates.",
            "",
            "## Artifacts",
            "",
            "| Status | Artifact | Density raw / width-normalized | Occupancy | Contour levels | Contract |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for artifact in artifacts:
        if artifact.main_map is None:
            density = occupancy = "n/a"
        else:
            density = (
                f"{artifact.main_map.density_mm_per_mm2:.3f} / "
                f"{artifact.main_map.physical_equivalent_density_mm_per_mm2:.3f}"
            )
            occupancy = f"{artifact.main_map.geography_grid.occupied_fraction:.1%}"
        lines.append(
            f"| {artifact.status.upper()} | `{artifact.artifact}` | {density} | "
            f"{occupancy} | {artifact.contract.rendered_contour_level_count} | "
            f"{artifact.contract.status} |"
        )
    failures = [artifact for artifact in artifacts if artifact.status == "fail"]
    lines.extend(
        [
            "",
            "## Findings",
            "",
            f"- {len(artifacts)} artifact(s) audited; {len(failures)} failed.",
        ]
    )
    for artifact in artifacts:
        details = [*artifact.findings, *artifact.contract.findings]
        if artifact.main_map is not None:
            details.extend(artifact.main_map.findings)
        if details:
            lines.append(f"- `{artifact.artifact}`: {'; '.join(details)}")
    lines.append("")
    return "\n".join(lines)


def _print_summary(artifacts: Sequence[ArtifactMetrics]) -> None:
    for artifact in artifacts:
        if artifact.main_map is None:
            main = "main=n/a"
        else:
            main = (
                f"main={artifact.main_map.status} "
                f"density={artifact.main_map.density_mm_per_mm2:.3f}/"
                f"{artifact.main_map.physical_equivalent_density_mm_per_mm2:.3f} "
                f"occupancy={artifact.main_map.geography_grid.occupied_fraction:.1%}"
            )
        print(
            f"{artifact.status.upper():4} {artifact.artifact}: {main}; "
            f"contract={artifact.contract.status}; "
            f"contours={artifact.contract.rendered_contour_level_count}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Generated hiking SVG file(s) or release/pilot directories.",
    )
    parser.add_argument("--json", type=Path, help="Write the full JSON report.")
    parser.add_argument("--markdown", type=Path, help="Write a Markdown summary.")
    parser.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="Exit non-zero when any artifact fails a v4 composition gate.",
    )
    args = parser.parse_args(argv)
    try:
        svg_paths = _discover_svg_paths(args.inputs)
        if not svg_paths:
            raise ValueError("no non-pen-split SVG artifacts found")
        artifacts = [audit_artifact(path) for path in svg_paths]
    except (OSError, ET.ParseError, ValueError) as exc:
        parser.error(str(exc))

    _print_summary(artifacts)
    payload = {
        "schema_version": 4,
        "audit_id": "hiking-full-field-composition-svg-measurement-v4",
        "gates": GATES,
        "summary": {
            "artifact_count": len(artifacts),
            "pass_count": sum(artifact.status == "pass" for artifact in artifacts),
            "fail_count": sum(artifact.status == "fail" for artifact in artifacts),
        },
        "artifacts": [asdict(artifact) for artifact in artifacts],
    }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(_markdown_report(artifacts), encoding="utf-8")
    if args.fail_on_gate and any(artifact.status == "fail" for artifact in artifacts):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
