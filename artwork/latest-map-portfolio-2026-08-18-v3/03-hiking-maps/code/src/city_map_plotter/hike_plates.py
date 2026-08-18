"""Source-aware hiking route plates and a privacy-conscious GPX importer.

Catalog geometry is deliberately described as source-sampled artwork.  It is
not routing data and this module never fills gaps between source segments.  A
small, explicitly labelled stylized backdrop gives the pen plot some terrain
texture without pretending to be a topographic or navigational map.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence
from xml.etree import ElementTree as ET

from shapely import difference as geometry_difference
from shapely import make_valid, set_precision
from shapely.affinity import rotate
from shapely.geometry import LineString, Point as GeometryPoint, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PEN_ORDER,
    PENS_BY_ID,
    PlateArtwork,
    Rect,
    add_text,
    circle_stroke,
    context_for,
    plotter_copy,
    polyline_length_mm,
    rectangle_stroke,
    text_strokes_fit,
)
from .route_chainage import ChainageStation, RouteChainage, geodesic_distance_m
from .stroke_font import text_width_mm


CATALOG_PATH = Path(__file__).with_name("data") / "hike-plates-v1.json"
CATALOG_ID = "hike-plates-v1"
CONTEXT_BUNDLE_PATH = Path(__file__).with_name("data") / "hike-context-v3.json"
CONTEXT_BUNDLE_ID = "hike-context-v3"
RELEASE_CATALOG_PATH = Path(__file__).with_name("data") / "hike-plates-release-v1.json"
RELEASE_CATALOG_ID = "hike-plates-release-v1"
EXPANSION_RECIPE_PATH = (
    Path(__file__).with_name("data") / "hike-expansion-recipes-v1.json"
)
SUBJECT_KIND = "route_plate"
PEN_PLAN_ID = "HIKE-A5-V2"
HIKE_PENS = (
    "grey-0-25",
    "grey-0-4",
    "blue-0-25",
    "green-0-25",
    "black-0-25",
    "black-0-6",
    "red-0-4",
)
HIKE_VARIANTS = ("detailed-map", "terrain-relief")
SEGMENT_MODES = {"walk", "ferry", "alternate"}
PROFILE_STATUSES = {
    "not-embedded",
    "source-elevation-sampled",
    "recorded-elevation-sampled",
    "partial-elevation-not-rendered",
}
PROFILE_EXTREMA_APPROXIMATE_STATUS = "sampled-approximate"
PROFILE_EXTREMA_EXACT_STATUS = "source-verified-exact"
PROFILE_EXTREMA_APPROXIMATE_POLICY_ID = "sampled-elevation-approximate-extrema-v1"
PROFILE_EXTREMA_EXACT_POLICY_ID = "source-verified-exact-extrema-v1"
PROFILE_PHYSICAL_GENERALIZATION_POLICY_ID = (
    "adaptive-physical-rdp-nib-pitch-preserve-global-extrema-v1"
)
PROFILE_PHYSICAL_SIMPLIFICATION_TOLERANCE_MM = 0.10
PROFILE_PHYSICAL_MAXIMUM_TOLERANCE_MM = 0.30
PROFILE_PHYSICAL_TOLERANCE_STEP_MM = 0.025
PROFILE_TARGET_PEN_WIDTH_MM = 0.25
TERRESTRIAL_ROUTE_ELEVATION_METHOD = (
    "mapzen-terrarium-terrestrial-bilinear-or-nearest-nonnegative-v1"
)
TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID = "terrestrial-walk-nonnegative-source-sample-v1"
MIXED_ROUTE_SAMPLING_POLICY_ID = (
    "mode-aware-terrestrial-and-ferry-sea-surface-reference-v1"
)
FERRY_SEA_SURFACE_POLICY_ID = "explicit-ferry-sea-surface-reference-0m-v1"
FALL_LINE_RUNTIME_RENDERING_POLICY_ID = (
    "factual-dem-fall-lines-runtime-clearance-cluster-v1"
)
FALL_LINE_NO_CLUSTER_OMISSION_REASON = "no-legible-cluster-after-clearance"
FALL_LINE_NO_SOURCE_OMISSION_REASON = "no-frozen-fall-lines"
CONTEXT_KINDS = {
    "coast",
    "hut",
    "park",
    "peak",
    "range",
    "river",
    "sea",
    "settlement",
    "water",
    "woodland",
    "pass",
    "grass",
    "road",
}
CONTEXT_FAMILY_KINDS = {
    "roads": frozenset({"road"}),
    "hydrography": frozenset({"river", "water", "coast", "sea"}),
    "landcover": frozenset({"woodland", "grass"}),
}
LAND_COVER_PATH_ROLES = frozenset(
    {
        "source-sampled-landcover-boundary",
        "source-bounded-landcover-band",
        "source-bounded-woodland-symbol",
        "source-anchored-woodland-symbol",
        "source-bounded-grass-symbol",
        "source-anchored-grass-symbol",
    }
)
MIN_FINE_STROKE_MM = 0.75
MIN_CONTEXT_AREA_MM2 = 0.32
MIN_LINEAR_LANDCOVER_AREA_MM2 = 0.015
MIN_LINEAR_LANDCOVER_BOUNDARY_MM = 2.5
MIN_CLOSED_COAST_AREA_MM2 = 0.25
MIN_WOODLAND_OUTLINE_AREA_MM2 = 8.0
MAX_SYMBOLIC_WOODLAND_ROUTE_DISTANCE_M = 10_000.0
MAX_GENERIC_SYMBOLIC_WOODLAND_ROUTE_DISTANCE_M = 20_000.0
AREA_PRECISION_MM = 0.001
REPEATED_LABEL_CLEARANCE_MM = 14.0
MIN_HERO_ROUTE_STROKE_MM = 3.0 * PENS_BY_ID["red-0-4"].mark_width_mm
LEADER_HERO_ROUTE_CLEARANCE_MM = 0.65
CONTOUR_LABEL_GEOGRAPHY_CLEARANCE_MM = 0.3
CONTOUR_LABEL_ROUTE_LANDMASS_PRIORITY_MM = 8.0
CONTOUR_LABEL_FOREIGN_COPY_CLEARANCE_MM = 4.0
CONTOUR_LABEL_HERO_ROUTE_CLEARANCE_MM = 0.9
CONTOUR_HIERARCHY_POLICY_ID = "factual-fifth-index-grey-pen-hierarchy-v1"
CONTOUR_MINOR_PEN_ID = "grey-0-25"
CONTOUR_INDEX_PEN_ID = "grey-0-4"
CONTOUR_INDEX_MULTIPLE = 5
MAP_CHAINAGE_STATION_RADIUS_MM = 1.25
MAP_CHAINAGE_RESERVATION_RADIUS_MM = 1.65
MAP_CHAINAGE_ROUTE_CLEARANCE_RADIUS_MM = 1.0
MAX_CONTEXT_LABEL_DISPLACEMENT_MM = 24.0
MAX_ROUTE_CONTROL_LABEL_DISPLACEMENT_MM = 32.0
GREEN_RETENTION_ROUTE_BANDS = 5
GREEN_RETENTION_MAX_FEATURES = {
    "detailed-map": 8,
    "terrain-relief": 6,
}
GREEN_RETENTION_INK_CLEARANCE_MM = 0.35
CATALOG_FORMATS = {"a5-portrait", "a5-landscape"}
EXPECTED_OSM_RELATIONS = {
    "RTE-GB-WHW-01": (16287, 345),
    "RTE-GB-HEB-WALK-01": (7610425, 81),
    "RTE-GB-GGW-01": (126572, 157),
    "RTE-GB-JMW-WALK-01": (49215, 379),
    "RTE-CH-VA1-01": (12359033, 12),
    "RTE-CH-AP6-01": (18021781, 30),
    "RTE-IS-LAUG-01": (1225037, 48),
}
EXPECTED_IDS = {
    *EXPECTED_OSM_RELATIONS,
    "RTE-FR-ECR-976000",
    "RTE-FR-ECR-995181",
    "RTE-ES-CAM-ES01C",
}
GPX_NAMESPACES = {
    "http://www.topografix.com/GPX/1/0": "1.0",
    "http://www.topografix.com/GPX/1/1": "1.1",
}
MAX_GPX_BYTES = 20 * 1024 * 1024
MAX_GPX_POINTS = 200_000
MAX_GPX_SEGMENTS = 2_000
Point = tuple[float, float]
GeoPoint = tuple[float, float, float | None]


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid hiking plate data: {message}")


def _required_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _required_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be a non-empty array.")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        _fail(f"{label} must be a finite number.")
    return number


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _route_point(value: Any, label: str) -> GeoPoint:
    if not isinstance(value, list) or len(value) not in {2, 3}:
        _fail(f"{label} must be [longitude, latitude] with optional elevation.")
    longitude = _finite_number(value[0], f"{label}[0]")
    latitude = _finite_number(value[1], f"{label}[1]")
    if not -180.0 <= longitude <= 180.0:
        _fail(f"{label} longitude is outside -180..180.")
    if not -90.0 <= latitude <= 90.0:
        _fail(f"{label} latitude is outside -90..90.")
    elevation = None
    if len(value) == 3:
        elevation = _finite_number(value[2], f"{label}[2]")
        if not -12_000.0 <= elevation <= 100_000.0:
            _fail(f"{label} elevation is outside the supported metric range.")
    return (longitude, latitude, elevation)


def _haversine_m(
    first: Sequence[float | None], second: Sequence[float | None]
) -> float:
    first_longitude = first[0]
    first_latitude = first[1]
    second_longitude = second[0]
    second_latitude = second[1]
    if (
        first_longitude is None
        or first_latitude is None
        or second_longitude is None
        or second_latitude is None
    ):
        raise MapPlotterError("A distance coordinate cannot be missing.")
    longitude_1 = math.radians(first_longitude)
    latitude_1 = math.radians(first_latitude)
    longitude_2 = math.radians(second_longitude)
    latitude_2 = math.radians(second_latitude)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = longitude_2 - longitude_1
    value = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2.0) ** 2
    )
    return 2.0 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(value)))


def _validate_source(source: Any, subject_id: str, index: int) -> dict[str, Any]:
    value = _required_dict(source, f"{subject_id}.sources[{index}]")
    for key in (
        "id",
        "publisher",
        "url",
        "license",
        "attribution",
        "use",
        "retrieved_at",
    ):
        _required_text(value.get(key), f"{subject_id}.sources[{index}].{key}")
    if not value["url"].startswith("https://"):
        _fail(f"{subject_id}.sources[{index}].url must use HTTPS.")
    return value


def _validate_context(
    context: Any,
    *,
    subject_id: str,
    source_ids: Sequence[str],
    route_points: Sequence[GeoPoint],
) -> dict[str, Any]:
    value = _required_dict(context, f"{subject_id}.context")
    if value.get("status") != "curated-source-sampled-art-context":
        _fail(f"{subject_id}.context must identify its source-sampled art status.")
    if value.get("geometry_status") != "generalized-not-for-navigation":
        _fail(f"{subject_id}.context must remain generalized and non-navigational.")
    context_source_ref = _required_text(
        value.get("source_ref"), f"{subject_id}.context.source_ref"
    )
    if context_source_ref not in source_ids:
        _fail(f"{subject_id}.context.source_ref does not name a source.")

    extent_raw = value.get("extent")
    if not isinstance(extent_raw, list) or len(extent_raw) != 4:
        _fail(f"{subject_id}.context.extent must be [west, south, east, north].")
    west, south, east, north = (
        _finite_number(item, f"{subject_id}.context.extent[{index}]")
        for index, item in enumerate(extent_raw)
    )
    if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
        _fail(f"{subject_id}.context.extent is invalid.")
    for longitude, latitude, _ in route_points:
        if not (west <= longitude <= east and south <= latitude <= north):
            _fail(f"{subject_id}.context.extent must contain every route point.")
    map_extent = value.get("map_extent")
    if map_extent is not None:
        _validate_extent_identity(
            map_extent,
            extent_raw,
            label=f"{subject_id}.context.map_extent",
        )

    rotation = _finite_number(
        value.get("rotation_deg", 0.0), f"{subject_id}.context.rotation_deg"
    )
    if not -45.0 <= rotation <= 45.0:
        _fail(f"{subject_id}.context.rotation_deg must stay within -45..45.")
    orientation = value.get("orientation_status")
    expected_orientation = (
        "north-up" if abs(rotation) < 1e-9 else "rotated-to-fit-artwork"
    )
    if orientation != expected_orientation:
        _fail(
            f"{subject_id}.context.orientation_status must be {expected_orientation!r}."
        )

    features = _required_list(value.get("features"), f"{subject_id}.context.features")
    seen_ids: set[str] = set()
    kinds: set[str] = set()
    for index, feature_raw in enumerate(features):
        feature = _required_dict(feature_raw, f"{subject_id}.context.features[{index}]")
        feature_id = _required_text(
            feature.get("id"), f"{subject_id}.context.features[{index}].id"
        )
        if feature_id in seen_ids:
            _fail(f"{subject_id}.context repeats feature id {feature_id!r}.")
        seen_ids.add(feature_id)
        kind = feature.get("kind")
        if kind not in CONTEXT_KINDS:
            _fail(f"{subject_id}.{feature_id} has unsupported context kind {kind!r}.")
        kinds.add(str(kind))
        _required_text(feature.get("label"), f"{subject_id}.{feature_id}.label")
        _route_point(feature.get("point"), f"{subject_id}.{feature_id}.point")
        if feature.get("source_ref") not in source_ids:
            _fail(f"{subject_id}.{feature_id} has an unknown context source_ref.")
        display_label = feature.get("display_label", True)
        if not isinstance(display_label, bool):
            _fail(f"{subject_id}.{feature_id}.display_label must be boolean.")
        label_required = feature.get("label_required", False)
        if not isinstance(label_required, bool):
            _fail(f"{subject_id}.{feature_id}.label_required must be boolean.")
        if label_required and not display_label:
            _fail(
                f"{subject_id}.{feature_id}.label_required conflicts with "
                "display_label=false."
            )
        if label_required and kind not in {"settlement", "hut", "pass", "peak"}:
            _fail(
                f"{subject_id}.{feature_id}.label_required needs a supported "
                "marker-and-label context kind."
            )
        if kind == "road" and feature.get("road_class") not in {
            "major",
            "secondary",
            "local",
            "track",
        }:
            _fail(f"{subject_id}.{feature_id} has an unsupported road class.")
        elevation_m = feature.get("elevation_m")
        if elevation_m is not None:
            elevation = _finite_number(
                elevation_m, f"{subject_id}.{feature_id}.elevation_m"
            )
            if kind not in {"peak", "pass"} or not -500.0 <= elevation <= 9_000.0:
                _fail(f"{subject_id}.{feature_id} has an invalid feature elevation.")
            _required_text(
                feature.get("elevation_method"),
                f"{subject_id}.{feature_id}.elevation_method",
            )
            elevation_source_ref = _required_text(
                feature.get("elevation_source_ref"),
                f"{subject_id}.{feature_id}.elevation_source_ref",
            )
            if elevation_source_ref not in source_ids:
                _fail(f"{subject_id}.{feature_id}.elevation_source_ref is unknown.")
        source_url = feature.get("source_url")
        if source_url is not None and (
            not isinstance(source_url, str) or not source_url.startswith("https://")
        ):
            _fail(f"{subject_id}.{feature_id}.source_url must use HTTPS.")
        paths_raw = feature.get("paths", [])
        if not isinstance(paths_raw, list):
            _fail(f"{subject_id}.{feature_id}.paths must be an array.")
        if kind in {"grass", "road"} and not paths_raw:
            _fail(f"{subject_id}.{feature_id} must contain source geometry.")
        for path_index, path_raw in enumerate(paths_raw):
            if not isinstance(path_raw, list) or len(path_raw) < 2:
                _fail(f"{subject_id}.{feature_id}.paths[{path_index}] is too short.")
            checked = [
                _route_point(
                    point,
                    f"{subject_id}.{feature_id}.paths[{path_index}][{point_index}]",
                )
                for point_index, point in enumerate(path_raw)
            ]
            if not any(_haversine_m(checked[0], point) > 0.01 for point in checked[1:]):
                _fail(f"{subject_id}.{feature_id}.paths[{path_index}] is degenerate.")
    if "family_evidence" in value:
        _validate_context_family_evidence(
            value["family_evidence"],
            subject_id=subject_id,
            features=features,
        )
    terrain = value.get("terrain")
    if "settlement" not in kinds:
        _fail(f"{subject_id}.context must include at least one settlement.")
    if not kinds.intersection({"peak", "range", "pass"}) and terrain is None:
        _fail(f"{subject_id}.context must include source-anchored relief context.")

    if terrain is not None:
        _validate_terrain_context(
            terrain,
            subject_id=subject_id,
            source_ids=source_ids,
        )
        _validate_terrain_extent_binding(
            terrain,
            context=value,
            subject_id=subject_id,
            field_name="terrain",
        )
    relief_terrain = value.get("relief_terrain")
    if relief_terrain is not None:
        _validate_terrain_context(
            relief_terrain,
            subject_id=subject_id,
            source_ids=source_ids,
        )
        _validate_terrain_extent_binding(
            relief_terrain,
            context=value,
            subject_id=subject_id,
            field_name="relief_terrain",
        )
    landcover = value.get("landcover")
    if landcover is not None:
        _validate_landcover_context(
            landcover,
            subject_id=subject_id,
            source_ids=source_ids,
        )
    water = value.get("water")
    if water is not None:
        _validate_water_context(
            water,
            subject_id=subject_id,
            source_ids=source_ids,
        )
    return value


def _validate_extent_identity(
    raw: Any,
    expected: Any,
    *,
    label: str,
) -> None:
    if (
        not isinstance(raw, list)
        or len(raw) != 4
        or not isinstance(expected, list)
        or len(expected) != 4
    ):
        _fail(f"{label} must be a four-value extent bound to its context.")
    checked = [
        _finite_number(item, f"{label}[{index}]") for index, item in enumerate(raw)
    ]
    expected_checked = [
        _finite_number(item, f"{label}.expected[{index}]")
        for index, item in enumerate(expected)
    ]
    if checked != expected_checked:
        _fail(f"{label} does not match its context extent binding.")


def _validate_terrain_extent_binding(
    terrain: dict[str, Any],
    *,
    context: dict[str, Any],
    subject_id: str,
    field_name: str,
) -> None:
    """Fail closed when a frozen DEM bundle declares a different map window."""

    prefix = f"{subject_id}.context.{field_name}"
    if "route_extent" in terrain:
        _validate_extent_identity(
            terrain["route_extent"],
            context.get("route_extent", context["extent"]),
            label=f"{prefix}.route_extent",
        )
    if "source_window_extent" in terrain:
        _validate_extent_identity(
            terrain["source_window_extent"],
            context["extent"],
            label=f"{prefix}.source_window_extent",
        )
    if "map_extent_binding_sha256" in terrain:
        binding = context.get("map_extent_binding")
        if not isinstance(binding, dict) or terrain["map_extent_binding_sha256"] != (
            _canonical_json_sha256(binding)
        ):
            _fail(f"{prefix}.map_extent_binding_sha256 does not match context.")


def _validate_context_family_evidence(
    raw: Any,
    *,
    subject_id: str,
    features: Sequence[dict[str, Any]],
) -> None:
    evidence = _required_list(raw, f"{subject_id}.context.family_evidence")
    if len(evidence) != len(CONTEXT_FAMILY_KINDS):
        _fail(f"{subject_id}.context.family_evidence must describe three families.")
    seen: set[str] = set()
    for index, raw_item in enumerate(evidence):
        label = f"{subject_id}.context.family_evidence[{index}]"
        item = _required_dict(raw_item, label)
        family = item.get("family")
        if family not in CONTEXT_FAMILY_KINDS or family in seen:
            _fail(f"{label}.family is unsupported or repeated.")
        seen.add(str(family))
        candidate_count = item.get("source_candidate_count")
        selected_count = item.get("selected_feature_count")
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < 0
            or isinstance(selected_count, bool)
            or not isinstance(selected_count, int)
            or selected_count < 0
            or selected_count > candidate_count
        ):
            _fail(f"{label} has invalid candidate/selection counts.")
        actual_selected = sum(
            str(feature.get("kind", "")) in CONTEXT_FAMILY_KINDS[str(family)]
            for feature in features
        )
        if selected_count != actual_selected:
            _fail(f"{label}.selected_feature_count does not match context.features.")
        assessed_count = item.get("page_legibility_assessed_feature_count", 0)
        legible_count = item.get("page_legible_feature_count", 0)
        sub_legible_count = item.get("sub_legible_feature_count", 0)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (assessed_count, legible_count, sub_legible_count)
        ) or not (
            assessed_count <= selected_count
            and legible_count <= assessed_count
            and sub_legible_count == assessed_count - legible_count
        ):
            _fail(f"{label} has invalid page-legibility counts.")
        expected_status = (
            "source-features-selected-sub-legible-at-page-scale"
            if selected_count and assessed_count and legible_count == 0
            else "source-features-selected"
            if selected_count
            else "source-candidates-unrenderable"
            if candidate_count
            else "source-query-zero-results"
        )
        if item.get("status") != expected_status:
            _fail(f"{label}.status does not match its evidence counts.")
        query_groups = item.get("query_groups")
        if (
            not isinstance(query_groups, list)
            or not query_groups
            or not all(isinstance(group, str) and group for group in query_groups)
        ):
            _fail(f"{label}.query_groups must be a non-empty string array.")
    if seen != set(CONTEXT_FAMILY_KINDS):
        _fail(f"{subject_id}.context.family_evidence is incomplete.")


def _validated_context_path(
    raw: Any,
    *,
    label: str,
    closed: bool = False,
) -> list[GeoPoint]:
    if not isinstance(raw, list) or len(raw) < (4 if closed else 2):
        _fail(f"{label} is too short.")
    points = [
        _route_point(point, f"{label}[{index}]") for index, point in enumerate(raw)
    ]
    if not any(_haversine_m(points[0], point) > 0.01 for point in points[1:]):
        _fail(f"{label} is degenerate.")
    if closed and _haversine_m(points[0], points[-1]) > 0.05:
        _fail(f"{label} must be an explicitly closed ring.")
    return points


def _validate_terrain_context(
    terrain: Any,
    *,
    subject_id: str,
    source_ids: Sequence[str],
) -> None:
    value = _required_dict(terrain, f"{subject_id}.context.terrain")
    if value.get("status") != "source-derived-dtm-relief":
        _fail(f"{subject_id}.context.terrain has an unsupported status.")
    if value.get("source_ref") not in source_ids:
        _fail(f"{subject_id}.context.terrain.source_ref does not name a source.")
    _required_text(
        value.get("derivation_id"),
        f"{subject_id}.context.terrain.derivation_id",
    )
    areas = value.get("areas", [])
    elevation_masks = value.get("elevation_masks", [])
    relief_strokes = value.get("relief_strokes", [])
    contours = value.get("contours", [])
    if (
        not isinstance(areas, list)
        or not isinstance(elevation_masks, list)
        or not isinstance(relief_strokes, list)
        or not isinstance(contours, list)
    ):
        _fail(
            f"{subject_id}.context.terrain areas/elevation_masks/"
            "relief_strokes/contours must be arrays."
        )
    if not contours:
        _fail(
            f"{subject_id}.context.terrain must contain elevation-valued contours; "
            "area masks alone are not rendered."
        )
    seen_ids: set[str] = set()
    for index, raw_area in enumerate(areas):
        area = _required_dict(raw_area, f"{subject_id}.context.terrain.areas[{index}]")
        area_id = _required_text(
            area.get("id"), f"{subject_id}.context.terrain.areas[{index}].id"
        )
        if area_id in seen_ids:
            _fail(f"{subject_id}.context.terrain repeats area id {area_id!r}.")
        seen_ids.add(area_id)
        minimum = _finite_number(
            area.get("minimum_elevation_m"),
            f"{subject_id}.context.terrain.areas[{index}].minimum_elevation_m",
        )
        if not -500.0 <= minimum <= 9_000.0:
            _fail(f"{subject_id}.context.terrain area elevation is implausible.")
        _validated_context_path(
            area.get("outer"),
            label=f"{subject_id}.context.terrain.areas[{index}].outer",
            closed=True,
        )
        holes = area.get("holes", [])
        if not isinstance(holes, list):
            _fail(f"{subject_id}.context.terrain area holes must be an array.")
        for hole_index, hole in enumerate(holes):
            _validated_context_path(
                hole,
                label=(
                    f"{subject_id}.context.terrain.areas[{index}].holes[{hole_index}]"
                ),
                closed=True,
            )
    seen_mask_ids: set[str] = set()
    for index, raw_mask in enumerate(elevation_masks):
        label = f"{subject_id}.context.terrain.elevation_masks[{index}]"
        mask = _required_dict(raw_mask, label)
        mask_id = _required_text(mask.get("id"), f"{label}.id")
        if mask_id in seen_mask_ids:
            _fail(f"{subject_id}.context.terrain repeats elevation mask {mask_id!r}.")
        seen_mask_ids.add(mask_id)
        minimum = _finite_number(
            mask.get("minimum_elevation_m"), f"{label}.minimum_elevation_m"
        )
        if not -500.0 <= minimum <= 9_000.0:
            _fail(f"{label}.minimum_elevation_m is implausible.")
        if mask.get("render_as_hachure") is not True:
            _fail(f"{label} must explicitly opt in to hachure rendering.")
        if mask.get("geometry_policy") != (
            "source-derived-threshold-polygon-no-invented-links-v1"
        ):
            _fail(f"{label} has an unsupported geometry policy.")
        geometry_sha256 = _required_text(
            mask.get("geometry_sha256"), f"{label}.geometry_sha256"
        )
        if len(geometry_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in geometry_sha256
        ):
            _fail(f"{label}.geometry_sha256 must be lower-case SHA-256.")
        area_m2 = _finite_number(
            mask.get("derived_area_m2"), f"{label}.derived_area_m2"
        )
        distance_m = _finite_number(
            mask.get("distance_to_route_m"), f"{label}.distance_to_route_m"
        )
        if area_m2 <= 0.0 or distance_m < 0.0:
            _fail(f"{label} has invalid metric provenance.")
        _validated_context_path(mask.get("outer"), label=f"{label}.outer", closed=True)
        holes = mask.get("holes", [])
        if not isinstance(holes, list):
            _fail(f"{label}.holes must be an array.")
        for hole_index, hole in enumerate(holes):
            _validated_context_path(
                hole,
                label=f"{label}.holes[{hole_index}]",
                closed=True,
            )
        if geometry_sha256 != _canonical_json_sha256(
            {"outer": mask["outer"], "holes": holes}
        ):
            _fail(f"{label}.geometry_sha256 does not match its canonical rings.")
        rendering = _required_dict(mask.get("rendering"), f"{label}.rendering")
        spacing_mm = _finite_number(
            rendering.get("spacing_mm"), f"{label}.rendering.spacing_mm"
        )
        along_pitch_mm = _finite_number(
            rendering.get("along_pitch_mm"),
            f"{label}.rendering.along_pitch_mm",
        )
        nominal_segment_length_mm = _finite_number(
            rendering.get("nominal_segment_length_mm"),
            f"{label}.rendering.nominal_segment_length_mm",
        )
        angle_deg = _finite_number(
            rendering.get("angle_deg"), f"{label}.rendering.angle_deg"
        )
        inset_mm = _finite_number(
            rendering.get("inset_mm"), f"{label}.rendering.inset_mm"
        )
        minimum_stroke_mm = _finite_number(
            rendering.get("minimum_stroke_mm"),
            f"{label}.rendering.minimum_stroke_mm",
        )
        maximum_strokes = rendering.get("maximum_strokes_per_area")
        if not 3.0 <= spacing_mm <= 4.0:
            _fail(f"{label}.rendering.spacing_mm must be 3..4 mm.")
        if not 5.0 <= along_pitch_mm <= 6.0:
            _fail(f"{label}.rendering.along_pitch_mm must be 5..6 mm.")
        if not -90.0 <= angle_deg <= 90.0 or not 0.0 <= inset_mm <= 1.5:
            _fail(f"{label}.rendering angle/inset is outside the supported range.")
        if minimum_stroke_mm < 2.5:
            _fail(
                f"{label}.rendering.minimum_stroke_mm violates the elevation-mask "
                "hachure floor."
            )
        if not minimum_stroke_mm <= nominal_segment_length_mm <= 3.5:
            _fail(f"{label}.rendering nominal hachure length is invalid.")
        if (
            isinstance(maximum_strokes, bool)
            or not isinstance(maximum_strokes, int)
            or not 3 <= maximum_strokes <= 96
        ):
            _fail(f"{label}.rendering.maximum_strokes_per_area is invalid.")
        if rendering.get("perimeter_rendered") is not False:
            _fail(f"{label}.rendering must expressly suppress the perimeter.")
        if rendering.get("treatment") != (
            "source-mask-clipped-short-hachure-no-perimeter-v2"
        ):
            _fail(f"{label}.rendering treatment is unsupported.")
    if relief_strokes or "relief_stroke_policy" in value:
        if value.get("relief_algorithm_id") != "dem-gradient-fall-line-v1":
            _fail(f"{subject_id}.context.terrain relief algorithm is unsupported.")
        policy = _required_dict(
            value.get("relief_stroke_policy"),
            f"{subject_id}.context.terrain.relief_stroke_policy",
        )
        if policy.get("algorithm_id") != "dem-gradient-fall-line-v1":
            _fail(f"{subject_id}.context.terrain relief policy is unsupported.")
        minimum_seed_slope_deg = _finite_number(
            policy.get("minimum_seed_slope_deg"),
            f"{subject_id}.context.terrain.relief_stroke_policy.minimum_seed_slope_deg",
        )
        seed_slope_policy = policy.get("seed_slope_policy")
        if seed_slope_policy == "all-at-or-above-4deg-v1":
            if minimum_seed_slope_deg != 4.0:
                _fail(f"{subject_id}.context.terrain fixed slope gate is invalid.")
        elif seed_slope_policy == "global-page-smoothed-adaptive-v1":
            if not 0.15 <= minimum_seed_slope_deg <= 4.0:
                _fail(f"{subject_id}.context.terrain adaptive slope gate is invalid.")
            adaptive = _required_dict(
                policy.get("adaptive_seed_slope"),
                f"{subject_id}.context.terrain.relief_stroke_policy.adaptive_seed_slope",
            )
            if (
                adaptive.get("selected_minimum_seed_slope_deg")
                != minimum_seed_slope_deg
                or adaptive.get("page_smoothed_slope_percentile") != 75.0
                or adaptive.get("activation_slope_deg") != 0.75
            ):
                _fail(
                    f"{subject_id}.context.terrain adaptive slope evidence is invalid."
                )
            _finite_number(
                adaptive.get("page_smoothed_percentile_slope_deg"),
                f"{subject_id}.context.terrain adaptive percentile slope",
            )
            _finite_number(
                adaptive.get("page_smoothed_maximum_slope_deg"),
                f"{subject_id}.context.terrain adaptive maximum slope",
            )
        else:
            _fail(f"{subject_id}.context.terrain slope policy is unsupported.")
        minimum_trace_slope_deg = _finite_number(
            policy.get("minimum_trace_slope_deg"),
            f"{subject_id}.context.terrain.relief_stroke_policy.minimum_trace_slope_deg",
        )
        if not 0.05 <= minimum_trace_slope_deg <= 1.5:
            _fail(f"{subject_id}.context.terrain trace slope gate is invalid.")
        if seed_slope_policy == "global-page-smoothed-adaptive-v1" and (
            adaptive.get("selected_minimum_trace_slope_deg") != minimum_trace_slope_deg
        ):
            _fail(f"{subject_id}.context.terrain adaptive trace evidence is invalid.")
        if seed_slope_policy == "all-at-or-above-4deg-v1" and (
            minimum_trace_slope_deg != 1.5
        ):
            _fail(f"{subject_id}.context.terrain fixed trace slope gate is invalid.")
        if policy.get("emission_order") != "spatial-serpentine-lattice-v1":
            _fail(f"{subject_id}.context.terrain relief emission order is invalid.")
        if (
            policy.get("cluster_connectivity_distance_mm") != 6.0
            or policy.get("minimum_cluster_strokes") != 3
            or policy.get("minimum_cluster_total_mm") != 9.0
        ):
            _fail(f"{subject_id}.context.terrain relief cluster policy is invalid.")
        binding_sha256 = _required_text(
            policy.get("binding_transform_sha256"),
            f"{subject_id}.context.terrain.relief_stroke_policy.binding_transform_sha256",
        )
        if len(binding_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in binding_sha256
        ):
            _fail(f"{subject_id}.context.terrain binding transform hash is invalid.")
        _required_text(
            policy.get("binding_format_id"),
            f"{subject_id}.context.terrain.relief_stroke_policy.binding_format_id",
        )
        minimum_seed_elevation_m = _finite_number(
            policy.get("minimum_seed_elevation_m"),
            f"{subject_id}.context.terrain.relief_stroke_policy.minimum_seed_elevation_m",
        )
        maximum_strokes = policy.get("maximum_strokes")
        if (
            isinstance(maximum_strokes, bool)
            or not isinstance(maximum_strokes, int)
            or not 1 <= maximum_strokes <= 140
            or len(relief_strokes) > maximum_strokes
        ):
            _fail(f"{subject_id}.context.terrain fall-line cap is invalid.")
        maximum_total_mm = _finite_number(
            policy.get("maximum_total_mm"),
            f"{subject_id}.context.terrain.relief_stroke_policy.maximum_total_mm",
        )
        if maximum_total_mm > 650.0:
            _fail(f"{subject_id}.context.terrain fall-line budget exceeds 650 mm.")
        seen_relief_ids: set[str] = set()
        geometry_manifest: list[dict[str, str]] = []
        page_total_mm = 0.0
        previous_serpentine_key: tuple[int, int] | None = None
        for index, raw_stroke in enumerate(relief_strokes):
            label = f"{subject_id}.context.terrain.relief_strokes[{index}]"
            stroke = _required_dict(raw_stroke, label)
            stroke_id = _required_text(stroke.get("id"), f"{label}.id")
            if stroke_id in seen_relief_ids:
                _fail(f"{subject_id}.context.terrain repeats fall line {stroke_id!r}.")
            seen_relief_ids.add(stroke_id)
            points = stroke.get("points")
            _validated_context_path(points, label=f"{label}.points")
            _route_point(stroke.get("seed"), f"{label}.seed")
            slope_deg = _finite_number(
                stroke.get("seed_slope_deg"), f"{label}.seed_slope_deg"
            )
            mean_slope_deg = _finite_number(
                stroke.get("mean_slope_deg"), f"{label}.mean_slope_deg"
            )
            aspect_deg = _finite_number(
                stroke.get("seed_aspect_deg"), f"{label}.seed_aspect_deg"
            )
            seed_elevation_m = _finite_number(
                stroke.get("seed_elevation_m"), f"{label}.seed_elevation_m"
            )
            page_length_mm = _finite_number(
                stroke.get("page_length_mm"), f"{label}.page_length_mm"
            )
            if (
                not minimum_seed_slope_deg <= slope_deg <= 90.0
                or not 0.0 <= mean_slope_deg <= 90.0
                or not 0.0 <= aspect_deg < 360.0
            ):
                _fail(f"{label} has invalid slope/aspect provenance.")
            if seed_elevation_m < minimum_seed_elevation_m:
                _fail(f"{label} is below the configured seed elevation gate.")
            if not 3.0 <= page_length_mm <= 7.01:
                _fail(f"{label}.page_length_mm is outside the physical contract.")
            page_total_mm += page_length_mm
            geometry_sha256 = _required_text(
                stroke.get("geometry_sha256"), f"{label}.geometry_sha256"
            )
            if geometry_sha256 != _canonical_json_sha256(points):
                _fail(f"{label}.geometry_sha256 does not match its points.")
            if stroke.get("algorithm_id") != "dem-gradient-fall-line-v1":
                _fail(f"{label}.algorithm_id is unsupported.")
            lattice = _required_dict(
                stroke.get("seed_lattice"), f"{label}.seed_lattice"
            )
            row = lattice.get("row")
            column = lattice.get("column")
            if (
                isinstance(row, bool)
                or not isinstance(row, int)
                or row < 0
                or isinstance(column, bool)
                or not isinstance(column, int)
                or column < 0
            ):
                _fail(f"{label}.seed_lattice row/column are invalid.")
            _finite_number(lattice.get("page_x_mm"), f"{label}.seed_lattice.page_x_mm")
            _finite_number(lattice.get("page_y_mm"), f"{label}.seed_lattice.page_y_mm")
            serpentine_key = (row, column if row % 2 == 0 else -column)
            if (
                previous_serpentine_key is not None
                and serpentine_key < previous_serpentine_key
            ):
                _fail(
                    f"{subject_id}.context.terrain fall lines are not serpentine ordered."
                )
            previous_serpentine_key = serpentine_key
            provenance = _required_dict(stroke.get("provenance"), f"{label}.provenance")
            if provenance.get("source_ref") != value["source_ref"]:
                _fail(f"{label}.provenance source does not match terrain source.")
            if provenance.get("derived_window_sha256") != value.get(
                "derived_window_sha256"
            ):
                _fail(f"{label}.provenance window hash does not match terrain.")
            geometry_manifest.append(
                {"id": stroke_id, "geometry_sha256": geometry_sha256}
            )
        if page_total_mm > maximum_total_mm + 0.1:
            _fail(f"{subject_id}.context.terrain fall lines exceed their budget.")
        manifest_sha256 = _canonical_json_sha256(geometry_manifest)
        if (
            policy.get("geometry_manifest_sha256") != manifest_sha256
            or value.get("relief_geometry_manifest_sha256") != manifest_sha256
        ):
            _fail(f"{subject_id}.context.terrain fall-line manifest is invalid.")
    for index, raw_contour in enumerate(contours):
        contour = _required_dict(
            raw_contour, f"{subject_id}.context.terrain.contours[{index}]"
        )
        _finite_number(
            contour.get("elevation_m"),
            f"{subject_id}.context.terrain.contours[{index}].elevation_m",
        )
        paths = contour.get("paths")
        if not isinstance(paths, list) or not paths:
            _fail(f"{subject_id}.context.terrain contour paths must be non-empty.")
        for path_index, path in enumerate(paths):
            _validated_context_path(
                path,
                label=(
                    f"{subject_id}.context.terrain.contours[{index}]."
                    f"paths[{path_index}]"
                ),
            )


def _validate_landcover_context(
    landcover: Any,
    *,
    subject_id: str,
    source_ids: Sequence[str],
) -> None:
    value = _required_dict(landcover, f"{subject_id}.context.landcover")
    if value.get("status") != "source-sampled-landcover-polygons":
        _fail(f"{subject_id}.context.landcover has an unsupported status.")
    if value.get("source_ref") not in source_ids:
        _fail(f"{subject_id}.context.landcover.source_ref does not name a source.")
    features = value.get("features")
    if not isinstance(features, list) or not features:
        _fail(f"{subject_id}.context.landcover.features must be non-empty.")
    allowed_classes = {"forest", "heath", "scrub", "woodland"}
    seen_ids: set[str] = set()
    for index, raw_feature in enumerate(features):
        feature = _required_dict(
            raw_feature, f"{subject_id}.context.landcover.features[{index}]"
        )
        feature_id = _required_text(
            feature.get("id"),
            f"{subject_id}.context.landcover.features[{index}].id",
        )
        if feature_id in seen_ids:
            _fail(f"{subject_id}.context.landcover repeats id {feature_id!r}.")
        seen_ids.add(feature_id)
        if feature.get("class") not in allowed_classes:
            _fail(f"{subject_id}.{feature_id} has unsupported land-cover class.")
        _required_text(
            feature.get("source_object"),
            f"{subject_id}.context.landcover.features[{index}].source_object",
        )
        area_m2 = _finite_number(
            feature.get("area_m2"),
            f"{subject_id}.context.landcover.features[{index}].area_m2",
        )
        if area_m2 <= 0.0:
            _fail(f"{subject_id}.{feature_id}.area_m2 must be positive.")
        _validated_context_path(
            feature.get("outer"),
            label=f"{subject_id}.context.landcover.features[{index}].outer",
            closed=True,
        )
        holes = feature.get("holes", [])
        if not isinstance(holes, list):
            _fail(f"{subject_id}.{feature_id}.holes must be an array.")
        for hole_index, hole in enumerate(holes):
            _validated_context_path(
                hole,
                label=(
                    f"{subject_id}.context.landcover.features[{index}]."
                    f"holes[{hole_index}]"
                ),
                closed=True,
            )


def _validate_source_object(value: Any, label: str) -> str:
    source_object = _required_text(value, label)
    object_type, separator, identifier = source_object.partition("/")
    if (
        separator != "/"
        or object_type not in {"node", "way", "relation"}
        or not identifier.isdigit()
        or int(identifier) <= 0
    ):
        _fail(f"{label} must be a canonical OSM object identifier.")
    return source_object


def _validate_water_context(
    water: Any,
    *,
    subject_id: str,
    source_ids: Sequence[str],
) -> None:
    value = _required_dict(water, f"{subject_id}.context.water")
    if value.get("status") != "source-sampled-hydrography":
        _fail(f"{subject_id}.context.water has an unsupported status.")
    if value.get("source_ref") not in source_ids:
        _fail(f"{subject_id}.context.water.source_ref does not name a source.")
    _required_text(
        value.get("derivation_id"), f"{subject_id}.context.water.derivation_id"
    )
    areas = value.get("areas")
    coastlines = value.get("coastlines")
    rivers = value.get("rivers")
    labels = value.get("labels", [])
    if not isinstance(areas, list):
        _fail(f"{subject_id}.context.water.areas must be an array.")
    if not isinstance(coastlines, list):
        _fail(f"{subject_id}.context.water.coastlines must be an array.")
    if not isinstance(rivers, list):
        _fail(f"{subject_id}.context.water.rivers must be an array.")
    if not (areas or coastlines or rivers):
        _fail(f"{subject_id}.context.water must contain derived hydrography.")
    if not isinstance(labels, list):
        _fail(f"{subject_id}.context.water.labels must be an array.")
    seen_ids: set[str] = set()
    for index, raw_area in enumerate(areas):
        area = _required_dict(raw_area, f"{subject_id}.context.water.areas[{index}]")
        area_id = _required_text(
            area.get("id"), f"{subject_id}.context.water.areas[{index}].id"
        )
        if area_id in seen_ids:
            _fail(f"{subject_id}.context.water repeats id {area_id!r}.")
        seen_ids.add(area_id)
        if area.get("class") not in {"lake", "reservoir", "river-water"}:
            _fail(f"{subject_id}.{area_id} has unsupported water class.")
        _validate_source_object(
            area.get("source_object"),
            f"{subject_id}.context.water.areas[{index}].source_object",
        )
        if (
            _finite_number(
                area.get("area_m2"),
                f"{subject_id}.context.water.areas[{index}].area_m2",
            )
            <= 0.0
        ):
            _fail(f"{subject_id}.{area_id}.area_m2 must be positive.")
        _validated_context_path(
            area.get("outer"),
            label=f"{subject_id}.context.water.areas[{index}].outer",
            closed=True,
        )
        holes = area.get("holes", [])
        if not isinstance(holes, list):
            _fail(f"{subject_id}.{area_id}.holes must be an array.")
        for hole_index, hole in enumerate(holes):
            _validated_context_path(
                hole,
                label=(
                    f"{subject_id}.context.water.areas[{index}].holes[{hole_index}]"
                ),
                closed=True,
            )
    for collection_name, collection in (("coastlines", coastlines), ("rivers", rivers)):
        for index, raw_line in enumerate(collection):
            line = _required_dict(
                raw_line, f"{subject_id}.context.water.{collection_name}[{index}]"
            )
            line_id = _required_text(
                line.get("id"),
                f"{subject_id}.context.water.{collection_name}[{index}].id",
            )
            if line_id in seen_ids:
                _fail(f"{subject_id}.context.water repeats id {line_id!r}.")
            seen_ids.add(line_id)
            representative_source = _validate_source_object(
                line.get("source_object"),
                (
                    f"{subject_id}.context.water.{collection_name}[{index}]."
                    "source_object"
                ),
            )
            source_objects = line.get("source_objects")
            if not isinstance(source_objects, list) or not source_objects:
                _fail(
                    f"{subject_id}.context.water.{collection_name}[{index}]."
                    "source_objects must be non-empty."
                )
            checked_source_objects = [
                _validate_source_object(
                    source_object,
                    (
                        f"{subject_id}.context.water.{collection_name}[{index}]."
                        f"source_objects[{source_index}]"
                    ),
                )
                for source_index, source_object in enumerate(source_objects)
            ]
            if representative_source != checked_source_objects[0] or len(
                checked_source_objects
            ) != len(set(checked_source_objects)):
                _fail(
                    f"{subject_id}.{line_id} must use a unique, representative-first "
                    "source_objects list."
                )
            if (
                _finite_number(
                    line.get("length_m"),
                    f"{subject_id}.context.water.{collection_name}[{index}].length_m",
                )
                <= 0.0
            ):
                _fail(f"{subject_id}.{line_id}.length_m must be positive.")
            paths = line.get("paths")
            if not isinstance(paths, list) or not paths:
                _fail(f"{subject_id}.{line_id}.paths must be non-empty.")
            for path_index, path in enumerate(paths):
                _validated_context_path(
                    path,
                    label=(
                        f"{subject_id}.context.water.{collection_name}[{index}]."
                        f"paths[{path_index}]"
                    ),
                )
    for index, raw_label in enumerate(labels):
        label = _required_dict(raw_label, f"{subject_id}.context.water.labels[{index}]")
        label_id = _required_text(
            label.get("id"), f"{subject_id}.context.water.labels[{index}].id"
        )
        if label_id in seen_ids:
            _fail(f"{subject_id}.context.water repeats id {label_id!r}.")
        seen_ids.add(label_id)
        if label.get("kind") not in {"river", "sea", "water"}:
            _fail(f"{subject_id}.{label_id} has unsupported hydro label kind.")
        _required_text(
            label.get("label"), f"{subject_id}.context.water.labels[{index}].label"
        )
        _route_point(
            label.get("point"), f"{subject_id}.context.water.labels[{index}].point"
        )
        _validate_source_object(
            label.get("source_object"),
            f"{subject_id}.context.water.labels[{index}].source_object",
        )


def _validate_record(record: Any, *, label: str = "record") -> dict[str, Any]:
    value = _required_dict(record, label)
    required = {
        "id",
        "subject_kind",
        "title",
        "subtitle",
        "details",
        "credit_line",
        "sources",
        "route",
        "backdrop",
        "context",
        "composition",
        "scale_status",
        "evidence_status",
        "rights_status",
        "notes",
    }
    missing = sorted(required - set(value))
    if missing:
        _fail(f"{label} is missing {', '.join(missing)}.")
    subject_id = _required_text(value["id"], f"{label}.id")
    if value["subject_kind"] != SUBJECT_KIND:
        _fail(f"{subject_id} must use subject_kind={SUBJECT_KIND!r}.")
    _required_text(value["title"], f"{subject_id}.title")
    _required_text(value["subtitle"], f"{subject_id}.subtitle")
    details = value["details"]
    if (
        not isinstance(details, list)
        or len(details) != 3
        or not all(isinstance(item, str) and item.strip() for item in details)
    ):
        _fail(f"{subject_id}.details must contain exactly three text lines.")
    if not any("ARTWORK / NOT FOR NAVIGATION" in item.upper() for item in details):
        _fail(f"{subject_id} must visibly state ARTWORK / NOT FOR NAVIGATION.")
    _required_text(value["credit_line"], f"{subject_id}.credit_line")
    variant_credit_lines = value.get("variant_credit_lines")
    if variant_credit_lines is not None:
        checked_variant_credits = _required_dict(
            variant_credit_lines, f"{subject_id}.variant_credit_lines"
        )
        if set(checked_variant_credits) != set(HIKE_VARIANTS):
            _fail(
                f"{subject_id}.variant_credit_lines must define exactly "
                f"{HIKE_VARIANTS!r}."
            )
        for variant_id in HIKE_VARIANTS:
            _required_text(
                checked_variant_credits[variant_id],
                f"{subject_id}.variant_credit_lines.{variant_id}",
            )

    sources = _required_list(value["sources"], f"{subject_id}.sources")
    checked_sources = [
        _validate_source(source, subject_id, index)
        for index, source in enumerate(sources)
    ]
    source_ids = [source["id"] for source in checked_sources]
    if len(source_ids) != len(set(source_ids)):
        _fail(f"{subject_id} repeats a source id.")

    route = _required_dict(value["route"], f"{subject_id}.route")
    if route.get("geometry_status") != "source-sampled-not-navigational":
        _fail(f"{subject_id} must label catalog geometry as source-sampled.")
    if route.get("navigation_status") != "artwork-not-for-navigation":
        _fail(f"{subject_id} must remain explicitly non-navigational.")
    if route.get("coordinate_order") != "lon-lat-ele-optional":
        _fail(f"{subject_id} uses an unsupported coordinate order.")
    if route.get("source_ref") not in source_ids:
        _fail(f"{subject_id}.route.source_ref does not name a source.")
    profile_status = route.get("profile_status")
    if profile_status not in PROFILE_STATUSES:
        _fail(f"{subject_id} has invalid profile_status {profile_status!r}.")
    extrema_evidence = route.get("elevation_extrema_evidence")
    checked_extrema: tuple[float, float] | None = None
    if extrema_evidence is not None:
        evidence = _required_dict(
            extrema_evidence,
            f"{subject_id}.route.elevation_extrema_evidence",
        )
        if evidence.get("status") != PROFILE_EXTREMA_EXACT_STATUS:
            _fail(f"{subject_id} has an unsupported elevation extrema claim.")
        if evidence.get("source_ref") not in source_ids:
            _fail(f"{subject_id} elevation extrema source is unknown.")
        _required_text(
            evidence.get("method"),
            f"{subject_id}.route.elevation_extrema_evidence.method",
        )
        exact_minimum_m = _finite_number(
            evidence.get("minimum_m"),
            f"{subject_id}.route.elevation_extrema_evidence.minimum_m",
        )
        exact_maximum_m = _finite_number(
            evidence.get("maximum_m"),
            f"{subject_id}.route.elevation_extrema_evidence.maximum_m",
        )
        if exact_maximum_m < exact_minimum_m:
            _fail(f"{subject_id} elevation extrema are reversed.")
        checked_extrema = (exact_minimum_m, exact_maximum_m)

    segments = _required_list(route.get("segments"), f"{subject_id}.route.segments")
    elevations = 0
    point_count = 0
    ferry_count = 0
    ferry_point_count = 0
    terrestrial_route_point_count = 0
    checked_route_points: list[GeoPoint] = []
    segment_elevation_rows: list[
        tuple[str, str, int, dict[str, Any] | None, list[GeoPoint]]
    ] = []
    segment_ids: set[str] = set()
    for segment_index, segment_value in enumerate(segments):
        segment = _required_dict(
            segment_value, f"{subject_id}.route.segments[{segment_index}]"
        )
        segment_id = _required_text(
            segment.get("id"), f"{subject_id}.route.segments[{segment_index}].id"
        )
        if segment_id in segment_ids:
            _fail(f"{subject_id} repeats segment id {segment_id!r}.")
        segment_ids.add(segment_id)
        mode = segment.get("mode")
        if mode not in SEGMENT_MODES:
            _fail(f"{subject_id}.{segment_id} has invalid segment mode.")
        if mode == "alternate":
            if segment.get("source_role") != "alternative":
                _fail(
                    f"{subject_id}.{segment_id} must preserve its source "
                    "alternative role."
                )
            osm_way_id = segment.get("osm_way_id")
            if (
                isinstance(osm_way_id, bool)
                or not isinstance(osm_way_id, int)
                or osm_way_id <= 0
            ):
                _fail(f"{subject_id}.{segment_id} needs a positive OSM way id.")
            source_length = _finite_number(
                segment.get("source_length_m"),
                f"{subject_id}.{segment_id}.source_length_m",
            )
            if source_length <= 0.0:
                _fail(f"{subject_id}.{segment_id}.source_length_m must be positive.")
        ferry_count += int(mode == "ferry")
        if segment.get("source_ref") not in source_ids:
            _fail(f"{subject_id}.{segment_id} has an unknown source_ref.")
        points = _required_list(
            segment.get("points"), f"{subject_id}.{segment_id}.points"
        )
        if len(points) < 2:
            _fail(f"{subject_id}.{segment_id} needs at least two route points.")
        checked = [
            _route_point(point, f"{subject_id}.{segment_id}.points[{index}]")
            for index, point in enumerate(points)
        ]
        if not any(_haversine_m(checked[0], point) > 0.01 for point in checked[1:]):
            _fail(f"{subject_id}.{segment_id} is degenerate.")
        point_count += len(checked)
        if mode == "ferry":
            ferry_point_count += len(checked)
        else:
            terrestrial_route_point_count += len(checked)
        raw_segment_policy = segment.get("elevation_sampling_policy")
        segment_policy = (
            raw_segment_policy if isinstance(raw_segment_policy, dict) else None
        )
        segment_elevation_rows.append(
            (segment_id, str(mode), len(checked), segment_policy, checked)
        )
        checked_route_points.extend(checked)
        elevations += sum(point[2] is not None for point in checked)
    if not any(segment.get("mode") == "walk" for segment in segments):
        _fail(f"{subject_id} must contain a walking segment.")

    # A physically simplified A5 hero line can be too coarse to support a
    # truthful long-distance elevation profile.  When present,
    # ``profile_segments`` is the complete, source-ordered geometry used only
    # for chainage and elevation; ``segments`` remains the map geometry.
    profile_elevation_rows = segment_elevation_rows
    profile_point_count = point_count
    profile_elevation_count = elevations
    raw_profile_segments = route.get("profile_segments")
    if raw_profile_segments is not None:
        profile_segments = _required_list(
            raw_profile_segments,
            f"{subject_id}.route.profile_segments",
        )
        if not profile_segments:
            _fail(f"{subject_id}.route.profile_segments must not be empty.")
        profile_elevation_rows = []
        profile_point_count = 0
        profile_elevation_count = 0
        profile_ids: set[str] = set()
        for segment_index, segment_value in enumerate(profile_segments):
            segment = _required_dict(
                segment_value,
                f"{subject_id}.route.profile_segments[{segment_index}]",
            )
            segment_id = _required_text(
                segment.get("id"),
                f"{subject_id}.route.profile_segments[{segment_index}].id",
            )
            if segment_id in profile_ids:
                _fail(f"{subject_id} repeats profile segment id {segment_id!r}.")
            profile_ids.add(segment_id)
            if segment.get("mode") != "walk":
                _fail(f"{subject_id}.{segment_id} profile geometry must be walking.")
            if segment.get("source_ref") not in source_ids:
                _fail(f"{subject_id}.{segment_id} has an unknown source_ref.")
            points = _required_list(
                segment.get("points"), f"{subject_id}.{segment_id}.points"
            )
            if len(points) < 2:
                _fail(f"{subject_id}.{segment_id} needs at least two profile points.")
            checked = [
                _route_point(
                    point,
                    f"{subject_id}.{segment_id}.points[{point_index}]",
                )
                for point_index, point in enumerate(points)
            ]
            if not any(
                _haversine_m(checked[0], point) > 0.01 for point in checked[1:]
            ):
                _fail(f"{subject_id}.{segment_id} is degenerate.")
            raw_policy = segment.get("elevation_sampling_policy")
            policy = raw_policy if isinstance(raw_policy, dict) else None
            profile_elevation_rows.append(
                (segment_id, "walk", len(checked), policy, checked)
            )
            profile_point_count += len(checked)
            profile_elevation_count += sum(point[2] is not None for point in checked)
    if profile_status == "source-elevation-sampled":
        if (
            profile_elevation_count != profile_point_count
            or profile_point_count < 10
        ):
            _fail(f"{subject_id} source profile requires elevation on every point.")
    if checked_extrema is not None:
        profile_elevations = [
            float(point[2])
            for _segment_id, mode, _count, _policy, points in profile_elevation_rows
            if mode == "walk"
            for point in points
            if point[2] is not None
        ]
        if (
            not profile_elevations
            or not math.isclose(
                min(profile_elevations),
                checked_extrema[0],
                rel_tol=0.0,
                abs_tol=0.01,
            )
            or not math.isclose(
                max(profile_elevations),
                checked_extrema[1],
                rel_tol=0.0,
                abs_tol=0.01,
            )
        ):
            _fail(
                f"{subject_id} exact elevation extrema do not occur in the "
                "source profile."
            )

    route_sampling_policy = route.get("elevation_sampling_policy")
    if (
        profile_status == "source-elevation-sampled"
        and ferry_count
        and (
            not isinstance(route_sampling_policy, dict)
            or route_sampling_policy.get("id") != MIXED_ROUTE_SAMPLING_POLICY_ID
        )
    ):
        _fail(
            f"{subject_id} sampled ferry route requires a mode-aware elevation "
            "sampling policy."
        )
    if (
        isinstance(route_sampling_policy, dict)
        and route_sampling_policy.get("id") == MIXED_ROUTE_SAMPLING_POLICY_ID
    ):
        if not ferry_count or ferry_point_count <= 0:
            _fail(f"{subject_id} mixed route sampling policy requires a ferry.")
        ferry_reference = route_sampling_policy.get("ferry_reference")
        if (
            route_sampling_policy.get("minimum_valid_elevation_m") != 0.0
            or route_sampling_policy.get("clamping") is not False
            or route_sampling_policy.get("source_sampled_point_count")
            != terrestrial_route_point_count
            or route_sampling_policy.get("ferry_segment_count") != ferry_count
            or route_sampling_policy.get("ferry_sea_surface_reference_point_count")
            != ferry_point_count
            or not isinstance(ferry_reference, dict)
            or ferry_reference.get("id") != FERRY_SEA_SURFACE_POLICY_ID
            or ferry_reference.get("reference_elevation_m") != 0.0
            or ferry_reference.get("terrarium_sampling") is not False
            or ferry_reference.get("nearest_land_fallback") is not False
            or ferry_reference.get("bathymetry_clamping") is not False
        ):
            _fail(f"{subject_id} mixed route sampling policy is inconsistent.")
        bilinear_count = route_sampling_policy.get("bilinear_sample_count")
        fallback_count = route_sampling_policy.get("nearest_eligible_fallback_count")
        if (
            isinstance(bilinear_count, bool)
            or not isinstance(bilinear_count, int)
            or bilinear_count < 0
            or isinstance(fallback_count, bool)
            or not isinstance(fallback_count, int)
            or fallback_count < 0
            or bilinear_count + fallback_count != terrestrial_route_point_count
        ):
            _fail(f"{subject_id} terrestrial route sample counts are inconsistent.")
        elevation_source_ref = route.get("elevation_source_ref")
        for segment_id, mode, count, segment_policy, checked in segment_elevation_rows:
            if mode == "ferry":
                if (
                    segment_policy is None
                    or segment_policy.get("id") != FERRY_SEA_SURFACE_POLICY_ID
                    or segment_policy.get("reference_elevation_m") != 0.0
                    or segment_policy.get("point_count") != count
                    or segment_policy.get("terrarium_source_sample_count") != 0
                    or segment_policy.get("nearest_land_fallback_count") != 0
                    or segment_policy.get("bathymetric_value_clamp_count") != 0
                    or any(point[2] != 0.0 for point in checked)
                ):
                    _fail(
                        f"{subject_id}.{segment_id} lacks an explicit, unclamped "
                        "0 m ferry sea-surface reference."
                    )
                continue
            segment_bilinear_count = (
                segment_policy.get("bilinear_sample_count")
                if segment_policy is not None
                else None
            )
            segment_fallback_count = (
                segment_policy.get("nearest_eligible_fallback_count")
                if segment_policy is not None
                else None
            )
            if (
                segment_policy is None
                or segment_policy.get("id") != TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID
                or segment_policy.get("source_ref") != elevation_source_ref
                or segment_policy.get("minimum_valid_elevation_m") != 0.0
                or segment_policy.get("point_count") != count
                or segment_policy.get("clamping") is not False
                or isinstance(segment_bilinear_count, bool)
                or not isinstance(segment_bilinear_count, int)
                or segment_bilinear_count < 0
                or isinstance(segment_fallback_count, bool)
                or not isinstance(segment_fallback_count, int)
                or segment_fallback_count < 0
                or segment_bilinear_count + segment_fallback_count != count
            ):
                _fail(
                    f"{subject_id}.{segment_id} terrestrial elevation evidence "
                    "is inconsistent."
                )

    controls = _required_list(route.get("controls"), f"{subject_id}.route.controls")
    kinds: set[str] = set()
    for index, control_value in enumerate(controls):
        control = _required_dict(control_value, f"{subject_id}.controls[{index}]")
        kind = control.get("kind")
        if not isinstance(kind, str) or kind not in {"start", "finish", "stage"}:
            _fail(f"{subject_id}.controls[{index}] has invalid kind.")
        kinds.add(kind)
        _required_text(control.get("name"), f"{subject_id}.controls[{index}].name")
        _route_point(control.get("point"), f"{subject_id}.controls[{index}].point")
        if control.get("source_ref") not in source_ids:
            _fail(f"{subject_id}.controls[{index}] has an unknown source_ref.")
    if not {"start", "finish"}.issubset(kinds):
        _fail(f"{subject_id} needs source-backed start and finish controls.")

    _validate_context(
        value["context"],
        subject_id=subject_id,
        source_ids=source_ids,
        route_points=checked_route_points,
    )

    backdrop = _required_dict(value["backdrop"], f"{subject_id}.backdrop")
    if backdrop.get("status") not in {"source-derived", "stylized"}:
        _fail(f"{subject_id} has an unsupported backdrop status.")
    if backdrop.get("terrain") not in {
        "stylized-contour-lines",
        "source-anchored-relief-hachures",
        "source-derived-dtm-relief",
    }:
        _fail(f"{subject_id} has an unsupported terrain backdrop.")
    if backdrop.get("status") == "source-derived" and not isinstance(
        value["context"].get("terrain"), dict
    ):
        _fail(f"{subject_id} source-derived backdrop needs a terrain bundle.")
    seed = backdrop.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        _fail(f"{subject_id}.backdrop.seed must be an integer.")

    composition = _required_dict(value["composition"], f"{subject_id}.composition")
    if composition.get("pen_plan") != PEN_PLAN_ID:
        _fail(f"{subject_id} must use pen plan {PEN_PLAN_ID}.")
    _required_text(composition.get("recommended_crs"), f"{subject_id}.recommended_crs")
    if composition.get("format_id") not in CATALOG_FORMATS:
        _fail(f"{subject_id}.composition.format_id must be route-shaped A5.")
    for key in ("scale_status", "evidence_status", "rights_status"):
        _required_text(value[key], f"{subject_id}.{key}")
    notes = value["notes"]
    if (
        not isinstance(notes, list)
        or not notes
        or not all(isinstance(note, str) and note.strip() for note in notes)
    ):
        _fail(f"{subject_id}.notes must contain at least one note.")

    expected_relation = EXPECTED_OSM_RELATIONS.get(subject_id)
    if expected_relation is not None:
        actual = (route.get("relation_id"), route.get("relation_version"))
        if actual != expected_relation:
            _fail(
                f"{subject_id} must freeze OSM relation/version "
                f"{expected_relation!r}, not {actual!r}."
            )
    if subject_id == "RTE-GB-HEB-WALK-01" and ferry_count != 2:
        _fail("The Hebridean walking plate must preserve exactly two ferry segments.")
    if subject_id == "RTE-CH-AP6-01" and route.get("relation_id") == 132518:
        _fail("Deleted Alpine Passes relation 132518 is forbidden.")
    if subject_id == "RTE-ES-CAM-ES01C":
        actual_stages = [segment["id"] for segment in segments]
        expected_stages = [f"{number:02d}a" for number in range(5, 33)]
        if actual_stages != expected_stages:
            _fail("Camino artwork must use the contiguous 05a-32a source chain.")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant {value!r} is forbidden.")


def _validate_catalog(payload: Any) -> list[dict[str, Any]]:
    root = _required_dict(payload, "root")
    if root.get("schema_version") != 1:
        _fail("schema_version must be 1.")
    if root.get("id") != CATALOG_ID:
        _fail(f"id must be {CATALOG_ID!r}.")
    if root.get("subject_kind") != SUBJECT_KIND:
        _fail(f"root subject_kind must be {SUBJECT_KIND!r}.")
    plan = _required_dict(root.get("pen_plan"), "pen_plan")
    if plan.get("id") != PEN_PLAN_ID or tuple(plan.get("pens", ())) != HIKE_PENS:
        _fail(f"pen_plan must use the binding {HIKE_PENS!r} order.")
    if len(HIKE_PENS) > 7 or len(set(HIKE_PENS)) != len(HIKE_PENS):
        _fail("hiking pen plan must contain at most seven unique pens.")
    for pen_id in HIKE_PENS:
        if pen_id not in PENS_BY_ID or pen_id not in PEN_ORDER:
            _fail(f"pen plan uses unavailable pen {pen_id!r}.")
    records = _required_list(root.get("plates"), "plates")
    if len(records) != 10:
        _fail("catalog must contain exactly ten audited hiking plates.")
    validated = [
        _validate_record(record, label=f"plates[{index}]")
        for index, record in enumerate(records)
    ]
    ids = [record["id"] for record in validated]
    if len(ids) != len(set(ids)) or set(ids) != EXPECTED_IDS:
        _fail("catalog IDs must exactly match the ten audited hiking candidates.")
    return validated


def load_hike_catalog() -> list[dict[str, Any]]:
    """Load a deep copy of the strictly validated ten-route catalog."""

    try:
        payload = json.loads(
            CATALOG_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(
            f"Could not load hiking catalog {CATALOG_PATH}: {exc}"
        ) from exc
    records = copy.deepcopy(_validate_catalog(payload))
    try:
        context_payload = json.loads(
            CONTEXT_BUNDLE_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(
            f"Could not load hiking context bundle {CONTEXT_BUNDLE_PATH}: {exc}"
        ) from exc
    context_root = _required_dict(context_payload, "context bundle")
    if context_root.get("schema_version") != 3:
        _fail("context bundle schema_version must be 3.")
    if context_root.get("id") != CONTEXT_BUNDLE_ID:
        _fail(f"context bundle id must be {CONTEXT_BUNDLE_ID!r}.")
    overlays = context_root.get("records")
    if not isinstance(overlays, list):
        _fail("context bundle records must be an array.")
    records_by_id = {record["id"]: record for record in records}
    seen_ids: set[str] = set()
    for overlay_index, raw_overlay in enumerate(overlays):
        overlay = _required_dict(
            raw_overlay, f"context bundle records[{overlay_index}]"
        )
        subject_id = _required_text(
            overlay.get("subject_id"),
            f"context bundle records[{overlay_index}].subject_id",
        )
        if subject_id in seen_ids or subject_id not in records_by_id:
            _fail(f"context bundle has unknown or repeated subject {subject_id!r}.")
        seen_ids.add(subject_id)
        target = records_by_id[subject_id]
        sources = overlay.get("sources")
        if not isinstance(sources, list) or not sources:
            _fail(f"context bundle {subject_id} sources must be non-empty.")
        target["sources"].extend(copy.deepcopy(sources))
        context_overlay = _required_dict(
            overlay.get("context"), f"context bundle {subject_id}.context"
        )
        for key in ("terrain", "relief_terrain", "landcover", "water"):
            if key in context_overlay:
                target["context"][key] = copy.deepcopy(context_overlay[key])
        backdrop_overlay = _required_dict(
            overlay.get("backdrop"), f"context bundle {subject_id}.backdrop"
        )
        target["backdrop"].update(copy.deepcopy(backdrop_overlay))
        if "credit_line" in overlay:
            target["credit_line"] = _required_text(
                overlay["credit_line"], f"context bundle {subject_id}.credit_line"
            )
        target["notes"].append(f"Geographic context overlaid from {CONTEXT_BUNDLE_ID}.")
    return copy.deepcopy(
        [
            _validate_record(record, label=f"merged plates[{index}]")
            for index, record in enumerate(records)
        ]
    )


def _release_expected_ids() -> set[str]:
    try:
        recipes = json.loads(
            EXPANSION_RECIPE_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(
            f"Could not load hiking expansion recipes {EXPANSION_RECIPE_PATH}: {exc}"
        ) from exc
    root = _required_dict(recipes, "expansion recipes")
    if root.get("schema_version") != 1 or root.get("id") != "hike-expansion-recipes-v1":
        _fail("expansion recipe binding is invalid.")
    raw_routes = root.get("routes")
    if not isinstance(raw_routes, list) or len(raw_routes) != 30:
        _fail("expansion recipe binding must contain exactly thirty routes.")
    identifiers = {
        _required_text(route.get("id"), f"expansion recipes.routes[{index}].id")
        for index, route in enumerate(raw_routes)
        if isinstance(route, dict)
    }
    if len(identifiers) != 30:
        _fail("expansion recipe IDs must be thirty unique values.")
    return {*EXPECTED_IDS, *identifiers}


def _validate_release_terrain_bundle(
    record: dict[str, Any],
    terrain: dict[str, Any],
    *,
    field_name: str,
) -> None:
    subject_id = str(record["id"])
    terrain_source_ref = str(terrain.get("source_ref", ""))
    terrain_source = next(
        (
            source
            for source in record["sources"]
            if source.get("id") == terrain_source_ref
        ),
        None,
    )
    if not isinstance(terrain_source, dict):
        _fail(f"{subject_id} {field_name} source is absent from the source register.")
    for contour in terrain.get("contours", []):
        geometry_sha256 = contour.get("geometry_sha256")
        if geometry_sha256 is not None and geometry_sha256 != (
            _canonical_json_sha256(contour.get("paths"))
        ):
            _fail(
                f"{subject_id} release {field_name} contour geometry hash is invalid."
            )
    if not terrain_source_ref.startswith("aws-mapzen-terrarium-"):
        return
    surface_policy = terrain.get("surface_domain_policy")
    if (
        not isinstance(surface_policy, dict)
        or surface_policy.get("id") != "terrestrial-nonnegative-source-cells-v1"
        or surface_policy.get("minimum_elevation_m") != 0.0
        or surface_policy.get("elevation_values_clamped") is not False
    ):
        _fail(f"{subject_id} global {field_name} lacks terrestrial-domain proof.")
    derived_window_sha256 = str(terrain.get("derived_window_sha256", ""))
    source_window_sha256 = str(terrain.get("source_window_sha256", ""))
    if (
        terrain_source.get("derived_window_sha256") != derived_window_sha256
        or terrain_source.get("source_window_sha256") != source_window_sha256
        or any(
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in (derived_window_sha256, source_window_sha256)
        )
    ):
        _fail(f"{subject_id} global {field_name}/source window binding is invalid.")


def _validate_release_context_query_groups(
    subject_id: str,
    query_groups: list[Any],
) -> None:
    """Validate frozen Overpass evidence, including disclosed review pilots."""

    group_ids = [
        str(group.get("id", ""))
        for group in query_groups
        if isinstance(group, dict)
    ]
    index_group_ids = [
        group_id for group_id in group_ids if not group_id.startswith("geometry")
    ]
    geometry_group_ids = [
        group_id for group_id in group_ids if group_id.startswith("geometry")
    ]
    expected_index_group_inventories = {
        ("areas", "labels", "linear"),
        ("areas", "areas-distributed", "labels", "linear"),
    }
    expected_batch_ids = [
        f"geometry-batch-{index:02d}"
        for index in range(1, len(geometry_group_ids) + 1)
    ]
    pilot_geometry_ids = ["geometry-cache-union-pilot"]
    if (
        len(group_ids) != len(query_groups)
        or len(set(group_ids)) != len(group_ids)
        or tuple(sorted(index_group_ids)) not in expected_index_group_inventories
        or not geometry_group_ids
        or (
            geometry_group_ids != ["geometry"]
            and geometry_group_ids != expected_batch_ids
            and geometry_group_ids != pilot_geometry_ids
        )
    ):
        _fail(f"{subject_id} context source query-group inventory is invalid.")
    for group in query_groups:
        if not isinstance(group, dict):
            _fail(f"{subject_id} context query-group evidence is invalid.")
        query_sha256 = str(group.get("query_sha256", ""))
        snapshot_sha256 = str(group.get("snapshot_sha256", ""))
        if (
            not isinstance(group.get("result_count"), int)
            or isinstance(group.get("result_count"), bool)
            or group["result_count"] < 0
            or len(query_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in query_sha256
            )
            or len(snapshot_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in snapshot_sha256
            )
        ):
            _fail(f"{subject_id} context query-group evidence is invalid.")

    geometry_groups = [
        group
        for group in query_groups
        if str(group.get("id", "")).startswith("geometry")
    ]
    if geometry_group_ids == pilot_geometry_ids:
        pilot = geometry_groups[0]
        requested_way_count = pilot.get("requested_way_count")
        missing_way_count = pilot.get("missing_way_count")
        if (
            pilot.get("pilot_cache_only") is not True
            or not isinstance(requested_way_count, int)
            or isinstance(requested_way_count, bool)
            or requested_way_count <= 0
            or not isinstance(missing_way_count, int)
            or isinstance(missing_way_count, bool)
            or missing_way_count < 0
            or pilot["result_count"] + missing_way_count != requested_way_count
            or any(
                field in pilot
                for field in ("batch_index", "batch_count", "way_id_count")
            )
        ):
            _fail(
                f"{subject_id} cache-union pilot evidence is inconsistent or "
                "not marked pilot-only."
            )
        return

    if geometry_group_ids == ["geometry"]:
        return

    batch_count = len(geometry_group_ids)
    for batch_index, group in enumerate(geometry_groups, start=1):
        if (
            group.get("batch_index") != batch_index
            or group.get("batch_count") != batch_count
            or not isinstance(group.get("way_id_count"), int)
            or isinstance(group.get("way_id_count"), bool)
            or group["way_id_count"] <= 0
        ):
            _fail(f"{subject_id} geometry batch evidence is inconsistent.")


def _validate_release_catalog(payload: Any) -> list[dict[str, Any]]:
    root = _required_dict(payload, "release root")
    if root.get("schema_version") != 1 or root.get("id") != RELEASE_CATALOG_ID:
        _fail(f"release catalog must use schema 1 and id {RELEASE_CATALOG_ID!r}.")
    if root.get("subject_kind") != SUBJECT_KIND:
        _fail(f"release root subject_kind must be {SUBJECT_KIND!r}.")
    plan = _required_dict(root.get("pen_plan"), "release pen_plan")
    if plan.get("id") != PEN_PLAN_ID or tuple(plan.get("pens", ())) != HIKE_PENS:
        _fail("release pen plan does not match the seven-pen hiking binding.")
    records = root.get("plates")
    if not isinstance(records, list) or len(records) != 40:
        _fail("release catalog must contain exactly forty route subjects.")
    validated = [
        _validate_record(record, label=f"release plates[{index}]")
        for index, record in enumerate(records)
    ]
    identifiers = [str(record["id"]) for record in validated]
    if set(identifiers) != _release_expected_ids() or len(set(identifiers)) != 40:
        _fail("release IDs do not match the ten legacy and thirty expansion routes.")
    expansion_ids = _release_expected_ids() - EXPECTED_IDS
    for record in validated:
        subject_id = str(record["id"])
        context = record["context"]
        if (
            float(context.get("rotation_deg", 0.0)) != 0.0
            or context.get("orientation_status") != "north-up"
        ):
            _fail(f"{subject_id} release geometry must be north-up.")
        if not isinstance(context.get("terrain"), dict):
            _fail(f"{subject_id} release must contain frozen factual terrain.")
        route = record["route"]
        if not all(
            len(point) == 3
            for segment in route["segments"]
            for point in segment["points"]
        ):
            _fail(f"{subject_id} release route points must carry sampled altitude.")
        source_ids = {str(source["id"]) for source in record["sources"]}
        if any(
            str(source.get("license", "")).upper().startswith("ODBL")
            for source in record["sources"]
        ):
            visible_credit = str(record.get("credit_line", "")).upper()
            if (
                "OPENSTREETMAP" not in visible_credit
                or "OPENSTREETMAP.ORG/COPYRIGHT" not in visible_credit
            ):
                _fail(
                    f"{subject_id} release ODbL credit must visibly print "
                    "OpenStreetMap and "
                    "OPENSTREETMAP.ORG/COPYRIGHT."
                )
        profile_source_ref = str(
            route.get("elevation_source_ref") or route.get("source_ref") or ""
        )
        if (
            route.get("profile_status") != "source-elevation-sampled"
            or profile_source_ref not in source_ids
        ):
            _fail(f"{subject_id} release profile lacks source provenance.")
        for terrain_field in ("terrain", "relief_terrain"):
            terrain = context.get(terrain_field)
            if isinstance(terrain, dict):
                _validate_release_terrain_bundle(
                    record,
                    terrain,
                    field_name=terrain_field,
                )
        for feature in context["features"]:
            if feature.get("kind") not in {"peak", "pass"}:
                continue
            if feature.get("elevation_m") is None:
                continue
            method = str(feature.get("elevation_method", ""))
            if method not in {
                "osm-ele-tag",
                "authoritative-gazetteer-height",
                "official-source-elevation",
            }:
                _fail(
                    f"{subject_id}.{feature['id']} named height is not an explicit "
                    "summit/pass value."
                )
            if method == "osm-ele-tag" and (
                feature.get("osm_type") != "node"
                or not isinstance(feature.get("osm_id"), int)
            ):
                _fail(
                    f"{subject_id}.{feature['id']} OSM elevation must belong to "
                    "an explicit node."
                )
        if subject_id in expansion_ids:
            evidence = context.get("family_evidence")
            if not isinstance(evidence, list):
                _fail(
                    f"{subject_id} expansion context lacks family availability evidence."
                )
            for family in evidence:
                if family.get("status") == "source-candidates-unrenderable":
                    _fail(
                        f"{subject_id} has source candidates for {family.get('family')} "
                        "but preserved no renderable feature."
                    )
            context_source = next(
                (
                    source
                    for source in record["sources"]
                    if source.get("id") == context.get("source_ref")
                ),
                None,
            )
            query_groups = (
                context_source.get("query_groups")
                if isinstance(context_source, dict)
                else None
            )
            if (
                not isinstance(context_source, dict)
                or context_source.get("query_contract_id")
                != "hiking-overpass-context-v2"
                or context_source.get("acquisition_endpoint")
                != "https://wiki.openstreetmap.org/wiki/Overpass_API"
                or not isinstance(query_groups, list)
            ):
                _fail(f"{subject_id} context source lacks exact query-group evidence.")
            _validate_release_context_query_groups(subject_id, query_groups)
    return validated


def load_hike_release_catalog() -> list[dict[str, Any]]:
    """Load the 40-route, north-up paired-artwork release catalogue."""

    try:
        payload = json.loads(
            RELEASE_CATALOG_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(
            f"Could not load hiking release catalog {RELEASE_CATALOG_PATH}: {exc}"
        ) from exc
    return copy.deepcopy(_validate_release_catalog(payload))


def _layout(field: Rect, *, has_profile: bool) -> tuple[Rect, Rect]:
    """Reserve most of the binding field for geography, not empty furniture."""

    inner = field.inset(3.0)
    if not has_profile:
        return inner, Rect(inner.x, inner.bottom, inner.width, 0.0)
    gap = 1.8
    profile_height = 13.8
    map_rect = Rect(
        inner.x,
        inner.y,
        inner.width,
        inner.height - gap - profile_height,
    )
    profile_rect = Rect(
        inner.x,
        map_rect.bottom + gap,
        inner.width,
        profile_height,
    )
    return map_rect, profile_rect


def _route_rect(map_rect: Rect) -> Rect:
    """Return the shared geographic binding inside the visible map field."""

    return Rect(
        map_rect.x + 2.5,
        map_rect.y + 2.5,
        map_rect.width - 5.0,
        map_rect.height - 5.0,
    )


def _unwrap_longitude(longitude: float, crosses_antimeridian: bool) -> float:
    if crosses_antimeridian and longitude < 0.0:
        return longitude + 360.0
    return longitude


def _geographic_transform(
    segments: Sequence[dict[str, Any]],
    rect: Rect,
    *,
    extent: Sequence[float] | None = None,
    rotation_deg: float = 0.0,
) -> tuple[list[list[Point]], Callable[[Sequence[float]], Point]]:
    raw_points = [point for segment in segments for point in segment["points"]]
    if len(raw_points) < 2:
        raise MapPlotterError("A route needs at least two geographic points.")
    longitudes = [float(point[0]) for point in raw_points]
    crosses_antimeridian = max(longitudes) - min(longitudes) > 180.0
    if extent is None:
        west = min(longitudes)
        east = max(longitudes)
        south = min(float(point[1]) for point in raw_points)
        north = max(float(point[1]) for point in raw_points)
    else:
        west, south, east, north = (float(item) for item in extent)
    mean_latitude = (south + north) / 2.0
    cosine = max(math.cos(math.radians(mean_latitude)), 1e-6)
    centre_x = (
        (
            _unwrap_longitude(west, crosses_antimeridian)
            + _unwrap_longitude(east, crosses_antimeridian)
        )
        / 2.0
    ) * cosine
    centre_y = (south + north) / 2.0
    angle = math.radians(rotation_deg)
    rotation_cosine = math.cos(angle)
    rotation_sine = math.sin(angle)

    def projected(point: Sequence[float]) -> Point:
        longitude = _unwrap_longitude(float(point[0]), crosses_antimeridian) * cosine
        latitude = float(point[1])
        delta_x = longitude - centre_x
        delta_y = latitude - centre_y
        return (
            centre_x + delta_x * rotation_cosine - delta_y * rotation_sine,
            centre_y + delta_x * rotation_sine + delta_y * rotation_cosine,
        )

    bounds_points = [
        projected((west, south)),
        projected((west, north)),
        projected((east, south)),
        projected((east, north)),
    ]
    projected_points = (
        bounds_points
        if extent is not None
        else [projected(point) for point in raw_points]
    )
    min_x = min(point[0] for point in projected_points)
    max_x = max(point[0] for point in projected_points)
    min_y = min(point[1] for point in projected_points)
    max_y = max(point[1] for point in projected_points)
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x <= 1e-12 and span_y <= 1e-12:
        raise MapPlotterError("Route coordinates collapse to one geographic point.")
    span_x = max(span_x, 1e-12)
    span_y = max(span_y, 1e-12)
    scale = min(rect.width / span_x, rect.height / span_y)
    used_width = span_x * scale
    used_height = span_y * scale
    offset_x = rect.x + (rect.width - used_width) / 2.0
    offset_y = rect.y + (rect.height - used_height) / 2.0

    def physical(point: Sequence[float]) -> Point:
        x, y = projected(point)
        return (
            offset_x + (x - min_x) * scale,
            offset_y + (max_y - y) * scale,
        )

    return (
        [[physical(point) for point in segment["points"]] for segment in segments],
        physical,
    )


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    denominator = delta_x * delta_x + delta_y * delta_y
    if denominator <= 1e-18:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    ratio = (
        (point[0] - start[0]) * delta_x + (point[1] - start[1]) * delta_y
    ) / denominator
    ratio = min(1.0, max(0.0, ratio))
    nearest = (start[0] + ratio * delta_x, start[1] + ratio * delta_y)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def _simplify(points: Sequence[Point], tolerance_mm: float = 0.04) -> list[Point]:
    """Iterative Douglas-Peucker; endpoints and source order are retained."""

    if len(points) <= 2:
        return list(points)
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start_index, end_index = stack.pop()
        start = points[start_index]
        end = points[end_index]
        furthest_index = -1
        furthest_distance = -1.0
        for index in range(start_index + 1, end_index):
            distance = _point_segment_distance(points[index], start, end)
            if distance > furthest_distance:
                furthest_distance = distance
                furthest_index = index
        if furthest_index >= 0 and furthest_distance > tolerance_mm:
            keep.add(furthest_index)
            stack.append((start_index, furthest_index))
            stack.append((furthest_index, end_index))
    return [points[index] for index in sorted(keep)]


def _dash_strokes(
    points: Sequence[Point], *, dash_mm: float = 2.0, gap_mm: float = 1.2
) -> list[list[Point]]:
    if len(points) < 2:
        return []
    strokes: list[list[Point]] = []
    drawing = True
    remaining = dash_mm
    current_stroke: list[Point] = [points[0]]
    current = points[0]
    for target in points[1:]:
        delta_x = target[0] - current[0]
        delta_y = target[1] - current[1]
        distance = math.hypot(delta_x, delta_y)
        if distance <= 1e-12:
            continue
        direction_x = delta_x / distance
        direction_y = delta_y / distance
        while distance > 1e-12:
            step = min(remaining, distance)
            next_point = (
                current[0] + direction_x * step,
                current[1] + direction_y * step,
            )
            if drawing:
                current_stroke.append(next_point)
            current = next_point
            distance -= step
            remaining -= step
            if remaining <= 1e-9:
                if drawing and polyline_length_mm(current_stroke) >= 0.75:
                    strokes.append(current_stroke)
                drawing = not drawing
                remaining = dash_mm if drawing else gap_mm
                current_stroke = [current]
    if drawing and polyline_length_mm(current_stroke) >= 0.75:
        strokes.append(current_stroke)
    return strokes


def _alternate_dash_strokes(points: Sequence[Point]) -> list[list[Point]]:
    """Dash a sourced alternate while keeping both attachment points inked."""

    length = polyline_length_mm(points)
    if length < 2.4:
        return [list(points)]
    gap_mm = 0.9
    dash_count = max(2, int(round((length + gap_mm) / 3.3)))
    while dash_count > 2:
        dash_mm = (length - (dash_count - 1) * gap_mm) / dash_count
        if dash_mm >= 1.2:
            break
        dash_count -= 1
    dash_mm = (length - (dash_count - 1) * gap_mm) / dash_count
    if dash_mm < 1.2:
        return [list(points)]
    return _dash_strokes(points, dash_mm=dash_mm, gap_mm=gap_mm)


def _hero_route_strokes(
    points: Sequence[Point], *, offset_mm: float = 0.28
) -> list[tuple[list[Point], float]]:
    """Build a centreline plus two overlapping display offsets for route weight."""

    if len(points) < 2:
        return []
    source = LineString(points)
    result: list[tuple[list[Point], float]] = [(list(points), 0.0)]
    for offset in (-offset_mm, offset_mm):
        shifted = source.offset_curve(
            offset,
            quad_segs=8,
            join_style="round",
            mitre_limit=2.0,
        )
        for line in _geometry_lines(shifted):
            if line.length + 1e-9 < MIN_HERO_ROUTE_STROKE_MM:
                continue
            normalized = _line_points(line)
            if polyline_length_mm(normalized) + 1e-9 >= MIN_HERO_ROUTE_STROKE_MM:
                result.append((normalized, offset))
    return result


def _hero_route_station_clearance_strokes(
    points: Sequence[Point], station_clearance: BaseGeometry
) -> list[list[Point]]:
    """Break display ink only beneath exact map-chainage station glyphs.

    The factual source line and every A--E anchor remain unchanged.  This is a
    small underprint clearance for the final red pen pass, not a displacement
    or a substitute route geometry.  Keeping the operation in page space also
    makes the declared millimetre radius auditable in the SVG.
    """

    if len(points) < 2:
        return []
    if station_clearance.is_empty:
        return [list(points)]
    visible = LineString(points).difference(station_clearance)
    return [
        _line_points(line)
        for line in _geometry_lines(visible)
        if float(line.length) + 1e-9 >= MIN_HERO_ROUTE_STROKE_MM
    ]


def _diamond(point: Point, radius_mm: float = 1.1) -> list[Point]:
    x, y = point
    return [
        (x, y - radius_mm),
        (x + radius_mm, y),
        (x, y + radius_mm),
        (x - radius_mm, y),
        (x, y - radius_mm),
    ]


def _clip_segment_to_extent(
    start: Point, end: Point, extent: Sequence[float]
) -> tuple[Point, Point] | None:
    west, south, east, north = (float(item) for item in extent)
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    lower = 0.0
    upper = 1.0
    for coefficient, distance in (
        (-delta_x, start[0] - west),
        (delta_x, east - start[0]),
        (-delta_y, start[1] - south),
        (delta_y, north - start[1]),
    ):
        if abs(coefficient) <= 1e-15:
            if distance < 0.0:
                return None
            continue
        ratio = distance / coefficient
        if coefficient < 0.0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None
    return (
        (start[0] + lower * delta_x, start[1] + lower * delta_y),
        (start[0] + upper * delta_x, start[1] + upper * delta_y),
    )


def _clip_geo_path(
    path: Sequence[Sequence[float]], extent: Sequence[float]
) -> list[list[Point]]:
    """Clip linework without inventing links across excursions outside the view."""

    clipped: list[list[Point]] = []
    current: list[Point] = []
    for first_raw, second_raw in zip(path, path[1:]):
        first = (float(first_raw[0]), float(first_raw[1]))
        second = (float(second_raw[0]), float(second_raw[1]))
        segment = _clip_segment_to_extent(first, second, extent)
        if segment is None:
            if len(current) >= 2:
                clipped.append(current)
            current = []
            continue
        clipped_start, clipped_end = segment
        if (
            current
            and math.hypot(
                current[-1][0] - clipped_start[0], current[-1][1] - clipped_start[1]
            )
            > 1e-8
        ):
            if len(current) >= 2:
                clipped.append(current)
            current = []
        if not current:
            current.append(clipped_start)
        if (
            math.hypot(current[-1][0] - clipped_end[0], current[-1][1] - clipped_end[1])
            > 1e-10
        ):
            current.append(clipped_end)
    if len(current) >= 2:
        clipped.append(current)
    return clipped


def _clip_geo_polygon(
    path: Sequence[Sequence[float]], extent: Sequence[float]
) -> list[Point]:
    """Sutherland-Hodgman clip for hatchable source polygons."""

    polygon = [(float(point[0]), float(point[1])) for point in path]
    if (
        len(polygon) >= 2
        and math.hypot(polygon[0][0] - polygon[-1][0], polygon[0][1] - polygon[-1][1])
        <= 1e-10
    ):
        polygon.pop()
    west, south, east, north = (float(item) for item in extent)

    def clip_edge(
        points: list[Point],
        *,
        inside: Callable[[Point], bool],
        intersection: Callable[[Point, Point], Point],
    ) -> list[Point]:
        if not points:
            return []
        result: list[Point] = []
        previous = points[-1]
        previous_inside = inside(previous)
        for current in points:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    result.append(intersection(previous, current))
                result.append(current)
            elif previous_inside:
                result.append(intersection(previous, current))
            previous = current
            previous_inside = current_inside
        return result

    def vertical(first: Point, second: Point, x: float) -> Point:
        ratio = (x - first[0]) / (second[0] - first[0])
        return (x, first[1] + ratio * (second[1] - first[1]))

    def horizontal(first: Point, second: Point, y: float) -> Point:
        ratio = (y - first[1]) / (second[1] - first[1])
        return (first[0] + ratio * (second[0] - first[0]), y)

    polygon = clip_edge(
        polygon,
        inside=lambda point: point[0] >= west,
        intersection=lambda first, second: vertical(first, second, west),
    )
    polygon = clip_edge(
        polygon,
        inside=lambda point: point[0] <= east,
        intersection=lambda first, second: vertical(first, second, east),
    )
    polygon = clip_edge(
        polygon,
        inside=lambda point: point[1] >= south,
        intersection=lambda first, second: horizontal(first, second, south),
    )
    polygon = clip_edge(
        polygon,
        inside=lambda point: point[1] <= north,
        intersection=lambda first, second: horizontal(first, second, north),
    )
    if len(polygon) < 3:
        return []
    return [*polygon, polygon[0]]


def _geometry_sort_key(geometry: BaseGeometry) -> tuple[float, ...]:
    minimum_x, minimum_y, maximum_x, maximum_y = geometry.bounds
    return (
        round(minimum_y, 3),
        round(minimum_x, 3),
        round(maximum_y, 3),
        round(maximum_x, 3),
        -round(float(getattr(geometry, "length", 0.0)), 3),
    )


def _geometry_polygons(
    geometry: BaseGeometry,
    *,
    minimum_area_mm2: float = MIN_CONTEXT_AREA_MM2,
) -> list[Polygon]:
    """Return valid polygon parts in deterministic page-space order."""

    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        candidates: Sequence[BaseGeometry] = (geometry,)
    elif geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        candidates = tuple(
            child
            for part in geometry.geoms  # type: ignore[attr-defined]
            for child in _geometry_polygons(
                part,
                minimum_area_mm2=minimum_area_mm2,
            )
        )
    else:
        return []
    polygons = [
        candidate
        for candidate in candidates
        if isinstance(candidate, Polygon)
        and not candidate.is_empty
        and candidate.area >= minimum_area_mm2
    ]
    return sorted(polygons, key=_geometry_sort_key)


def _geometry_lines(geometry: BaseGeometry) -> list[LineString]:
    """Return line parts in deterministic page-space order."""

    if geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        line = LineString(geometry.coords)  # type: ignore[attr-defined]
        return [line] if line.length >= MIN_FINE_STROKE_MM else []
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return _geometry_lines(geometry.boundary)
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        lines = [
            child
            for part in geometry.geoms  # type: ignore[attr-defined]
            for child in _geometry_lines(part)
        ]
        return sorted(lines, key=_geometry_sort_key)
    return []


def _closed_source_path(path: Sequence[Sequence[float]]) -> bool:
    if len(path) < 4:
        return False
    return _haversine_m(path[0], path[-1]) <= 1.0


def _closed_path_page_area_mm2(
    path: Sequence[Sequence[float]],
    physical_point: Callable[[Sequence[float]], Point],
) -> float:
    if not _closed_source_path(path):
        return 0.0
    try:
        geometry = make_valid(Polygon([physical_point(point) for point in path]))
    except (TypeError, ValueError):
        return 0.0
    return sum(float(polygon.area) for polygon in _geometry_polygons(geometry))


def _page_polygon_parts(
    outer: Sequence[Sequence[float]],
    *,
    holes: Sequence[Sequence[Sequence[float]]] = (),
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    simplify_mm: float = 0.08,
    minimum_area_mm2: float = MIN_CONTEXT_AREA_MM2,
) -> list[Polygon]:
    """Validate and crop a sourced polygon without inventing closure bridges."""

    if not _closed_source_path(outer) or any(
        not _closed_source_path(hole) for hole in holes
    ):
        return []
    shell = [physical_point(point) for point in outer]
    interior_rings = []
    for hole in holes:
        ring = [physical_point(point) for point in hole]
        if len(ring) >= 4 and LineString(ring).length >= MIN_FINE_STROKE_MM:
            interior_rings.append(ring)
    geometry: BaseGeometry = make_valid(Polygon(shell, interior_rings))
    geometry = geometry.intersection(
        box(map_rect.left, map_rect.top, map_rect.right, map_rect.bottom)
    )
    if geometry.is_empty:
        return []
    geometry = geometry.simplify(simplify_mm, preserve_topology=True)
    geometry = set_precision(geometry, grid_size=AREA_PRECISION_MM)
    return _geometry_polygons(
        make_valid(geometry),
        minimum_area_mm2=minimum_area_mm2,
    )


def _label_exclusion_geometry(label_boxes: Sequence[Rect]) -> BaseGeometry:
    geometries: list[BaseGeometry] = [
        box(
            label.left - 0.5,
            label.top - 0.5,
            label.right + 0.5,
            label.bottom + 0.5,
        )
        for label in label_boxes
    ]
    if not geometries:
        return Polygon()
    return make_valid(unary_union(geometries))


def _context_exclusion_geometry(
    route_lines: Sequence[Sequence[Point]],
    label_boxes: Sequence[Rect],
) -> BaseGeometry:
    """Mask contextual texture once so the route and labels stay readable."""

    geometries: list[BaseGeometry] = [
        LineString(line).buffer(0.9, cap_style="round", join_style="round")
        for line in route_lines
        if len(line) >= 2
    ]
    label_exclusion = _label_exclusion_geometry(label_boxes)
    if not label_exclusion.is_empty:
        geometries.append(label_exclusion)
    if not geometries:
        return Polygon()
    return make_valid(unary_union(geometries))


def _fall_line_exclusion_geometry(
    route_lines: Sequence[Sequence[Point]],
    label_boxes: Sequence[Rect],
    *,
    water: dict[str, Any] | None,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
) -> BaseGeometry:
    """Apply the disclosed route, label and hydrographic fall-line clearances."""

    geometries: list[BaseGeometry] = [
        LineString(line).buffer(1.1, cap_style="round", join_style="round")
        for line in route_lines
        if len(line) >= 2
    ]
    geometries.extend(
        box(
            label.left - 0.5,
            label.top - 0.5,
            label.right + 0.5,
            label.bottom + 0.5,
        )
        for label in label_boxes
    )
    if isinstance(water, dict):
        for area in water.get("areas", []):
            geometries.extend(
                polygon.buffer(0.4, cap_style="round", join_style="round")
                for polygon in _page_polygon_parts(
                    area["outer"],
                    holes=area.get("holes", []),
                    physical_point=physical_point,
                    map_rect=map_rect,
                    simplify_mm=0.1,
                )
            )
        for coastline in water.get("coastlines", []):
            for path in coastline.get("paths", []):
                geometries.extend(
                    LineString(stroke).buffer(
                        0.4, cap_style="round", join_style="round"
                    )
                    for stroke in _page_line_strokes(
                        path,
                        physical_point=physical_point,
                        map_rect=map_rect,
                    )
                    if len(stroke) >= 2
                )
        for river in water.get("rivers", []):
            for path in river.get("paths", []):
                geometries.extend(
                    LineString(stroke).buffer(
                        0.4, cap_style="round", join_style="round"
                    )
                    for stroke in _page_line_strokes(
                        path,
                        physical_point=physical_point,
                        map_rect=map_rect,
                    )
                    if len(stroke) >= 2
                )
    if not geometries:
        return Polygon()
    return make_valid(unary_union(geometries))


def _line_points(geometry: LineString) -> list[Point]:
    return [(round(float(x), 3), round(float(y), 3)) for x, y in geometry.coords]


def _masked_boundary_strokes(
    polygon: Polygon,
    *,
    exclusion: BaseGeometry,
) -> list[list[Point]]:
    geometry: BaseGeometry = polygon.boundary
    if not exclusion.is_empty:
        geometry = geometry.difference(exclusion)
    return [_line_points(line) for line in _geometry_lines(geometry)]


def _bounded_area_strokes(
    polygon: Polygon,
    *,
    spacing_mm: float,
    angle_deg: float,
    inset_mm: float,
    exclusion: BaseGeometry,
    phase: int,
    limit: int = 96,
    precision_mm: float | None = None,
) -> list[list[Point]]:
    """Create long, source-bounded plotter bands inside one polygon."""

    drawable: BaseGeometry = polygon.buffer(-inset_mm, join_style="round")
    if drawable.is_empty:
        return []
    if not drawable.is_valid:
        polygon_parts = _geometry_polygons(make_valid(drawable))
        if not polygon_parts:
            return []
        drawable = unary_union(polygon_parts)
    if precision_mm is not None:
        drawable = set_precision(drawable, grid_size=precision_mm)
    if not exclusion.is_empty:
        drawable = (
            geometry_difference(drawable, exclusion, grid_size=precision_mm)
            if precision_mm is not None
            else drawable.difference(exclusion)
        )
    if drawable.is_empty:
        return []
    origin = (float(polygon.centroid.x), float(polygon.centroid.y))
    rotated = rotate(drawable, -angle_deg, origin=origin, use_radians=False)
    minimum_x, minimum_y, maximum_x, maximum_y = rotated.bounds
    phase_mm = (phase % 997) / 997.0 * spacing_mm
    y = math.floor((minimum_y - phase_mm) / spacing_mm) * spacing_mm + phase_mm
    lines: list[LineString] = []
    while y <= maximum_y + 1e-9:
        cutter = LineString([(minimum_x - spacing_mm, y), (maximum_x + spacing_mm, y)])
        for part in _geometry_lines(rotated.intersection(cutter)):
            restored = rotate(part, angle_deg, origin=origin, use_radians=False)
            for restored_part in _geometry_lines(restored):
                if restored_part.length >= MIN_FINE_STROKE_MM:
                    lines.append(restored_part)
        y += spacing_mm
    lines.sort(key=_geometry_sort_key)
    if len(lines) > limit:
        stride = len(lines) / float(limit)
        lines = [
            lines[min(int(index * stride), len(lines) - 1)] for index in range(limit)
        ]
    return [_line_points(line) for line in lines]


def _bounded_short_hachure_strokes(
    polygon: Polygon,
    *,
    row_spacing_mm: float,
    along_pitch_mm: float,
    segment_length_mm: float,
    angle_deg: float,
    inset_mm: float,
    exclusion: BaseGeometry,
    phase: int,
    minimum_stroke_mm: float,
    limit: int,
) -> list[list[Point]]:
    """Create deterministic short, single-direction source-mask hachures."""

    if (
        row_spacing_mm <= 0.0
        or along_pitch_mm <= 0.0
        or segment_length_mm < minimum_stroke_mm
        or limit <= 0
    ):
        return []
    drawable: BaseGeometry = polygon.buffer(-inset_mm, join_style="round")
    if drawable.is_empty:
        return []
    if not drawable.is_valid:
        polygon_parts = _geometry_polygons(make_valid(drawable))
        if not polygon_parts:
            return []
        drawable = unary_union(polygon_parts)
    drawable = set_precision(drawable, grid_size=AREA_PRECISION_MM)
    if not exclusion.is_empty:
        drawable = geometry_difference(
            drawable,
            exclusion,
            grid_size=AREA_PRECISION_MM,
        )
    if drawable.is_empty:
        return []
    polygon_parts = _geometry_polygons(make_valid(drawable))
    if not polygon_parts:
        return []
    drawable = unary_union(polygon_parts)

    origin = (float(polygon.centroid.x), float(polygon.centroid.y))
    rotated = rotate(drawable, -angle_deg, origin=origin, use_radians=False)
    minimum_x, minimum_y, maximum_x, maximum_y = rotated.bounds
    row_phase_mm = (phase % 997) / 997.0 * row_spacing_mm
    column_phase_mm = ((phase // 997) % 991) / 991.0 * along_pitch_mm
    y = (
        math.floor((minimum_y - row_phase_mm) / row_spacing_mm) * row_spacing_mm
        + row_phase_mm
    )
    half_length = segment_length_mm / 2.0
    lines: list[LineString] = []
    row_index = 0
    while y <= maximum_y + 1e-9:
        stagger = along_pitch_mm / 2.0 if row_index % 2 else 0.0
        row_column_phase = column_phase_mm + stagger
        x = (
            math.floor((minimum_x - row_column_phase) / along_pitch_mm) * along_pitch_mm
            + row_column_phase
        )
        while x <= maximum_x + 1e-9:
            cutter = LineString([(x - half_length, y), (x + half_length, y)])
            for part in _geometry_lines(rotated.intersection(cutter)):
                if part.length + 1e-9 < minimum_stroke_mm:
                    continue
                restored = rotate(part, angle_deg, origin=origin, use_radians=False)
                for restored_part in _geometry_lines(restored):
                    if restored_part.length + 1e-9 >= minimum_stroke_mm:
                        lines.append(restored_part)
            x += along_pitch_mm
        y += row_spacing_mm
        row_index += 1
    lines.sort(key=_geometry_sort_key)
    if len(lines) > limit:
        stride = len(lines) / float(limit)
        lines = [
            lines[min(int((index + 0.5) * stride), len(lines) - 1)]
            for index in range(limit)
        ]
    return [_line_points(line) for line in lines]


def _inward_formlines(
    polygon: Polygon,
    *,
    exclusion: BaseGeometry,
    spacing_mm: float = 1.7,
    count: int = 3,
) -> list[list[Point]]:
    """Sparse illustrative form-lines bounded by a sourced range area."""

    lines: list[LineString] = []
    for index in range(1, count + 1):
        inset = polygon.buffer(-spacing_mm * index, join_style="round")
        if inset.is_empty:
            break
        geometry: BaseGeometry = inset.boundary
        if not exclusion.is_empty:
            geometry = geometry.difference(exclusion)
        lines.extend(_geometry_lines(geometry))
    return [_line_points(line) for line in sorted(lines, key=_geometry_sort_key)]


def _page_line_strokes(
    path: Sequence[Sequence[float]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    exclusion: BaseGeometry | None = None,
    smoothing: bool = False,
) -> list[list[Point]]:
    if len(path) < 2:
        return []
    points = [physical_point(point) for point in path]
    if smoothing:
        points = _smooth_context_line(points)
    geometry: BaseGeometry = LineString(points).intersection(
        box(map_rect.left, map_rect.top, map_rect.right, map_rect.bottom)
    )
    if exclusion is not None and not exclusion.is_empty:
        geometry = geometry.difference(exclusion)
    if geometry.is_empty:
        return []
    geometry = geometry.simplify(0.08, preserve_topology=True)
    geometry = set_precision(geometry, grid_size=AREA_PRECISION_MM)
    return [_line_points(line) for line in _geometry_lines(geometry)]


@dataclass(frozen=True)
class _StitchedContextPath:
    kind: str
    label: str
    road_class: str | None
    source_ref: str
    points: tuple[Point, ...]
    feature_ids: tuple[str, ...]
    osm_elements: tuple[str, ...]
    source_urls: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class _PageContextLine:
    source: _StitchedContextPath
    points: tuple[Point, ...]
    length_mm: float
    visible_length_mm: float
    route_axis: float
    route_distance_mm: float


def _exact_endpoint_stitch(
    fragments: Sequence[Sequence[Sequence[float]]],
) -> list[tuple[list[Point], tuple[int, ...]]]:
    """Join only paths sharing an exactly equal source endpoint.

    The returned fragment indices make the provenance union explicit.  No
    tolerance, interpolation, or nearest-neighbour connector is permitted.
    """

    chains: list[tuple[list[Point], list[int]]] = []
    for fragment_index, raw_fragment in enumerate(fragments):
        points = [
            (float(point[0]), float(point[1]))
            for point in raw_fragment
            if len(point) >= 2
        ]
        if len(points) < 2:
            continue
        chains.append((points, [fragment_index]))

    changed = True
    while changed:
        changed = False
        for first_index in range(len(chains)):
            first, first_members = chains[first_index]
            for second_index in range(first_index + 1, len(chains)):
                second, second_members = chains[second_index]
                merged: list[Point] | None = None
                if first[-1] == second[0]:
                    merged = [*first, *second[1:]]
                elif first[-1] == second[-1]:
                    merged = [*first, *reversed(second[:-1])]
                elif first[0] == second[-1]:
                    merged = [*second, *first[1:]]
                elif first[0] == second[0]:
                    merged = [*reversed(second), *first[1:]]
                if merged is None:
                    continue
                chains[first_index] = (
                    merged,
                    [*first_members, *second_members],
                )
                chains.pop(second_index)
                changed = True
                break
            if changed:
                break
    return [
        (points, tuple(sorted(members)))
        for points, members in chains
        if len(points) >= 2
    ]


def _stitched_context_paths(
    features: Sequence[dict[str, Any]],
    *,
    kinds: frozenset[str] = frozenset({"road", "river", "coast"}),
) -> list[_StitchedContextPath]:
    """Stitch same-name/ref factual linear fragments before page filtering."""

    grouped: dict[
        tuple[str, str, str, str],
        list[tuple[dict[str, Any], Sequence[Sequence[float]]]],
    ] = {}
    for feature in features:
        kind = str(feature.get("kind", ""))
        if kind not in kinds:
            continue
        road_class = str(feature.get("road_class", "")) if kind == "road" else ""
        identity = _label_identity(feature.get("label", ""))
        if identity in {"road", "trackroad", "river", "stream", "coast", "water"}:
            # These are acquisition fallbacks, not shared OSM names/refs.  An
            # exact junction remains factual, but treating the fallback copy
            # as identity could concatenate unrelated unnamed ways through it.
            identity = f"{identity}:{feature.get('id', '')}"
        source_ref = str(feature.get("source_ref", ""))
        key = (kind, road_class, identity, source_ref)
        for path in feature.get("paths", []):
            if isinstance(path, list) and len(path) >= 2:
                grouped.setdefault(key, []).append((feature, path))

    output: list[_StitchedContextPath] = []
    for key in sorted(grouped):
        kind, road_class, _identity, source_ref = key
        fragments = grouped[key]
        for points, member_indices in _exact_endpoint_stitch(
            [fragment[1] for fragment in fragments]
        ):
            member_features = [fragments[index][0] for index in member_indices]
            feature_ids = tuple(
                sorted({str(feature["id"]) for feature in member_features})
            )
            osm_elements = tuple(
                sorted(
                    {
                        f"{feature['osm_type']}/{feature['osm_id']}"
                        for feature in member_features
                        if feature.get("osm_type") is not None
                        and feature.get("osm_id") is not None
                    }
                )
            )
            source_urls = tuple(
                sorted(
                    {
                        str(feature["source_url"])
                        for feature in member_features
                        if feature.get("source_url")
                    }
                )
            )
            output.append(
                _StitchedContextPath(
                    kind=kind,
                    label=str(member_features[0].get("label", "")),
                    road_class=road_class or None,
                    source_ref=source_ref,
                    points=tuple(points),
                    feature_ids=feature_ids,
                    osm_elements=osm_elements,
                    source_urls=source_urls,
                    priority=min(
                        int(feature.get("priority", 9)) for feature in member_features
                    ),
                )
            )
    return output


def _route_axis_measure(
    point: GeometryPoint,
    route_lines: Sequence[Sequence[Point]],
) -> tuple[float, float]:
    """Return page-route progress and distance for contextual selection."""

    geometries = [LineString(line) for line in route_lines if len(line) >= 2]
    total_length = sum(float(line.length) for line in geometries)
    if total_length <= 1e-9:
        return 0.5, math.inf
    offset = 0.0
    best_distance = math.inf
    best_axis = 0.5
    for line in geometries:
        distance = float(line.distance(point))
        along = float(line.project(point))
        if distance < best_distance:
            best_distance = distance
            best_axis = (offset + along) / total_length
        offset += float(line.length)
    return min(1.0, max(0.0, best_axis)), best_distance


def _page_context_line_candidates(
    stitched: Sequence[_StitchedContextPath],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    exclusion: BaseGeometry,
) -> list[_PageContextLine]:
    candidates: list[_PageContextLine] = []
    route_geometry = unary_union(
        [LineString(line) for line in route_lines if len(line) >= 2]
    )
    for source in stitched:
        for stroke in _page_line_strokes(
            source.points,
            physical_point=physical_point,
            map_rect=map_rect,
            exclusion=exclusion,
            smoothing=True,
        ):
            length_mm = polyline_length_mm(stroke)
            if length_mm + 1e-9 < MIN_FINE_STROKE_MM:
                continue
            midpoint = LineString(stroke).interpolate(0.5, normalized=True)
            visible_geometry: BaseGeometry = LineString(stroke)
            if not route_geometry.is_empty:
                visible_geometry = visible_geometry.difference(
                    route_geometry.buffer(0.48, cap_style="round", join_style="round")
                )
            visible_length_mm = sum(
                float(line.length) for line in _geometry_lines(visible_geometry)
            )
            route_axis, route_distance_mm = _route_axis_measure(
                midpoint,
                route_lines,
            )
            candidates.append(
                _PageContextLine(
                    source=source,
                    points=tuple(stroke),
                    length_mm=length_mm,
                    visible_length_mm=visible_length_mm,
                    route_axis=route_axis,
                    route_distance_mm=route_distance_mm,
                )
            )
    return candidates


def _select_page_context_lines(
    candidates: Sequence[_PageContextLine],
    *,
    maximum: int,
    route_bands: int,
) -> list[_PageContextLine]:
    """Prefer page-legible crossings distributed along the route axis."""

    if maximum <= 0 or route_bands <= 0:
        return []

    def score(
        candidate: _PageContextLine,
    ) -> tuple[float, float, float, float, str]:
        return (
            0.0 if candidate.route_distance_mm <= 0.9 else 1.0,
            float(candidate.source.priority),
            -candidate.visible_length_mm,
            candidate.route_distance_mm,
            candidate.source.feature_ids[0],
        )

    ordered = sorted(candidates, key=score)
    selected: list[_PageContextLine] = []
    selected_ids: set[int] = set()
    for band in range(route_bands):
        in_band = [
            (index, candidate)
            for index, candidate in enumerate(ordered)
            if min(int(candidate.route_axis * route_bands), route_bands - 1) == band
        ]
        if not in_band:
            continue
        index, candidate = min(in_band, key=lambda item: score(item[1]))
        selected.append(candidate)
        selected_ids.add(index)
        if len(selected) >= maximum:
            break
    for index, candidate in enumerate(ordered):
        if len(selected) >= maximum:
            break
        if index in selected_ids:
            continue
        selected.append(candidate)
    return sorted(
        selected,
        key=lambda candidate: (
            candidate.route_axis,
            candidate.source.priority,
            candidate.source.feature_ids,
        ),
    )


def _smooth_context_line(
    points: Sequence[Point], *, iterations: int = 2, max_shift_mm: float = 0.18
) -> list[Point]:
    """Bounded moving-average smoothing for context only; route is never touched."""

    result = list(points)
    closed = (
        len(result) >= 4
        and math.hypot(result[0][0] - result[-1][0], result[0][1] - result[-1][1])
        <= 1e-8
    )
    for _ in range(iterations):
        if len(result) < 4:
            break
        working = result[:-1] if closed else result
        updated = list(working)
        indices = range(len(working)) if closed else range(1, len(working) - 1)
        for index in indices:
            previous = working[(index - 1) % len(working)]
            current = working[index]
            following = working[(index + 1) % len(working)]
            target = (
                (previous[0] + 2.0 * current[0] + following[0]) / 4.0,
                (previous[1] + 2.0 * current[1] + following[1]) / 4.0,
            )
            delta_x = target[0] - current[0]
            delta_y = target[1] - current[1]
            distance = math.hypot(delta_x, delta_y)
            if distance > max_shift_mm:
                scale = max_shift_mm / distance
                delta_x *= scale
                delta_y *= scale
            updated[index] = (current[0] + delta_x, current[1] + delta_y)
        result = [*updated, updated[0]] if closed else updated
    return result


def _relief_hachures(centre: Point, *, scale: float = 1.0) -> list[list[Point]]:
    strokes: list[list[Point]] = []
    for row, count in enumerate((3, 5, 7)):
        y = centre[1] + (1.7 + row * 1.55) * scale
        spacing = 1.35 * scale
        start_x = centre[0] - spacing * (count - 1) / 2.0
        for index in range(count):
            x = start_x + index * spacing
            length = (1.0 + 0.18 * ((index + row) % 3)) * scale
            strokes.append(
                [
                    (x - 0.36 * scale, y + length / 2.0),
                    (x + 0.36 * scale, y - length / 2.0),
                ]
            )
    return strokes


def _ridge_fans(centre: Point, *, scale: float = 1.0) -> list[list[Point]]:
    """Open, non-contour ridge fans anchored to sourced peak/range points."""

    strokes: list[list[Point]] = []
    for ring in (1.0, 1.55, 2.1):
        width = 2.5 * ring * scale
        height = 1.15 * ring * scale
        strokes.append(
            [
                (centre[0] - width, centre[1] + height),
                (centre[0] - width * 0.48, centre[1] - height * 0.15),
                (centre[0], centre[1] + height * 0.25),
                (centre[0] + width * 0.48, centre[1] - height * 0.34),
                (centre[0] + width, centre[1] + height),
            ]
        )
    return strokes


def _placed_ridge_symbol(
    anchor: Point,
    *,
    scale: float,
    map_rect: Rect,
    exclusion: BaseGeometry,
) -> tuple[list[list[Point]], Point] | None:
    """Place one small symbolic ridge near its source anchor without collisions."""

    offsets = (
        (0.0, 5.0),
        (0.0, -5.0),
        (6.0, 0.0),
        (-6.0, 0.0),
        (5.0, 4.0),
        (-5.0, 4.0),
        (5.0, -4.0),
        (-5.0, -4.0),
    )
    for offset_x, offset_y in offsets:
        centre = (anchor[0] + offset_x, anchor[1] + offset_y)
        symbol = _ridge_fans(centre, scale=scale)[:2]
        if not all(
            map_rect.left + 0.4 <= point[0] <= map_rect.right - 0.4
            and map_rect.top + 0.4 <= point[1] <= map_rect.bottom - 0.4
            for stroke in symbol
            for point in stroke
        ):
            continue
        if any(LineString(stroke).intersects(exclusion) for stroke in symbol):
            continue
        return symbol, centre
    return None


def _evergreen_strokes(centre: Point, *, scale: float = 1.0) -> list[list[Point]]:
    x, y = centre
    return [
        [
            (x, y - 1.6 * scale),
            (x - 0.95 * scale, y - 0.25 * scale),
            (x - 0.35 * scale, y - 0.25 * scale),
            (x - 1.15 * scale, y + 0.85 * scale),
            (x + 1.15 * scale, y + 0.85 * scale),
            (x + 0.35 * scale, y - 0.25 * scale),
            (x + 0.95 * scale, y - 0.25 * scale),
            (x, y - 1.6 * scale),
        ],
        [(x, y + 0.75 * scale), (x, y + 2.0 * scale)],
    ]


def _grass_tuft_strokes(centre: Point, *, scale: float = 1.0) -> list[list[Point]]:
    """Small source-anchored grass mark for sub-legible polygons."""

    x, y = centre
    return [
        [(x - 1.0 * scale, y + 0.65 * scale), (x - 0.35 * scale, y - 0.7 * scale)],
        [(x, y + 0.75 * scale), (x, y - 0.75 * scale)],
        [(x + 1.0 * scale, y + 0.65 * scale), (x + 0.35 * scale, y - 0.7 * scale)],
    ]


def _water_symbol_strokes(centre: Point, *, scale: float = 1.0) -> list[list[Point]]:
    """Compact wave mark for a sourced water body below polygon resolution."""

    x, y = centre
    return [
        [
            (x - 1.25 * scale, y),
            (x - 0.55 * scale, y - 0.3 * scale),
            (x + 0.1 * scale, y + 0.3 * scale),
            (x + 0.8 * scale, y),
        ],
        [
            (x - 0.8 * scale, y + 0.65 * scale),
            (x - 0.1 * scale, y + 0.35 * scale),
            (x + 0.55 * scale, y + 0.95 * scale),
            (x + 1.25 * scale, y + 0.65 * scale),
        ],
    ]


_CONTEXT_SYMBOL_OFFSETS: tuple[Point, ...] = (
    (0.0, 0.0),
    (0.0, -1.8),
    (1.8, 0.0),
    (0.0, 1.8),
    (-1.8, 0.0),
    (1.3, -1.3),
    (1.3, 1.3),
    (-1.3, 1.3),
    (-1.3, -1.3),
    (0.0, -2.8),
    (2.8, 0.0),
    (0.0, 2.8),
    (-2.8, 0.0),
    (2.0, -2.0),
    (2.0, 2.0),
    (-2.0, 2.0),
    (-2.0, -2.0),
)
_CONTEXT_SYMBOL_RETENTION_OFFSETS: tuple[Point, ...] = (
    *_CONTEXT_SYMBOL_OFFSETS,
    (0.0, -4.2),
    (4.2, 0.0),
    (0.0, 4.2),
    (-4.2, 0.0),
    (3.2, -3.2),
    (3.2, 3.2),
    (-3.2, 3.2),
    (-3.2, -3.2),
    (0.0, -6.0),
    (6.0, 0.0),
    (0.0, 6.0),
    (-6.0, 0.0),
)


@dataclass(frozen=True)
class _GreenRetentionCandidate:
    """One selected source feature eligible for an A5 no-area-claim mark."""

    feature: dict[str, Any]
    feature_index: int
    kind: str
    anchor: Point
    route_axis: float
    route_distance_mm: float
    route_band: int


def _green_retention_candidates(
    features: Sequence[dict[str, Any]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    route_bands: int = GREEN_RETENTION_ROUTE_BANDS,
) -> list[_GreenRetentionCandidate]:
    """Project factual green anchors and assign deterministic route-axis bands.

    The candidate point is the source feature's own representative point.  It
    is never used to imply a polygon boundary: a retained marker explicitly
    carries a no-area-claim rendering status.
    """

    if route_bands <= 0:
        return []
    candidates: list[_GreenRetentionCandidate] = []
    for feature_index, feature in enumerate(features, start=1):
        kind = str(feature.get("kind", ""))
        point = feature.get("point")
        if (
            kind not in {"woodland", "grass"}
            or not isinstance(point, (list, tuple))
            or len(point) < 2
            or not feature.get("source_ref")
        ):
            continue
        anchor = physical_point(point)
        # A source anchor may sit just outside the clipped field while one of
        # its exact polygon fragments crosses it.  Retention offsets extend by
        # at most 6 mm, so more remote anchors can never yield an honest mark.
        if not (
            map_rect.left - 6.0 <= anchor[0] <= map_rect.right + 6.0
            and map_rect.top - 6.0 <= anchor[1] <= map_rect.bottom + 6.0
        ):
            continue
        route_axis, route_distance_mm = _route_axis_measure(
            GeometryPoint(anchor), route_lines
        )
        route_band = min(int(route_axis * route_bands), route_bands - 1)
        candidates.append(
            _GreenRetentionCandidate(
                feature=feature,
                feature_index=feature_index,
                kind=kind,
                anchor=anchor,
                route_axis=route_axis,
                route_distance_mm=route_distance_mm,
                route_band=route_band,
            )
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.route_band,
            candidate.kind,
            int(candidate.feature.get("priority", 9)),
            float(candidate.feature.get("distance_to_route_m", math.inf)),
            candidate.route_distance_mm,
            str(candidate.feature.get("id", "")),
        ),
    )


def _placed_context_symbol(
    anchor: Point,
    *,
    symbol_factory: Callable[[Point], list[list[Point]]],
    map_rect: Rect,
    exclusion: BaseGeometry,
    offsets: Sequence[Point] = _CONTEXT_SYMBOL_OFFSETS,
) -> tuple[list[list[Point]], float, float] | None:
    for offset_x, offset_y in offsets:
        centre = (anchor[0] + offset_x, anchor[1] + offset_y)
        symbol = symbol_factory(centre)
        if not all(
            map_rect.left + 0.4 <= point[0] <= map_rect.right - 0.4
            and map_rect.top + 0.4 <= point[1] <= map_rect.bottom - 0.4
            for stroke in symbol
            for point in stroke
        ):
            continue
        if any(
            not exclusion.is_empty and LineString(stroke).intersects(exclusion)
            for stroke in symbol
        ):
            continue
        return symbol, offset_x, offset_y
    return None


def _add_source_anchored_feature_symbol(
    layer: ArtworkLayer,
    *,
    anchor: Point,
    map_rect: Rect,
    exclusion: BaseGeometry,
    source_ref: str,
    sequence: int,
    attributes: dict[str, str],
    semantic_class: str,
    offsets: Sequence[Point] = _CONTEXT_SYMBOL_OFFSETS,
) -> int:
    if semantic_class == "woodland":
        role = "source-anchored-woodland-symbol"
        symbol_factory = partial(_evergreen_strokes, scale=0.65)
    elif semantic_class == "grass":
        role = "source-anchored-grass-symbol"
        symbol_factory = partial(_grass_tuft_strokes, scale=0.72)
    elif semantic_class == "water":
        role = "source-anchored-water-symbol"
        symbol_factory = partial(_water_symbol_strokes, scale=0.72)
    else:
        return 0
    selected = _placed_context_symbol(
        anchor,
        symbol_factory=symbol_factory,
        map_rect=map_rect,
        exclusion=exclusion,
        offsets=offsets,
    )
    if selected is None:
        return 0
    symbol, offset_x, offset_y = selected
    for stroke in symbol:
        layer.add(
            stroke,
            source_ref=source_ref,
            role=role,
            sequence=sequence,
            attributes={
                **attributes,
                "data-feature-class": semantic_class,
                "data-area-rendering": "source-anchor-symbol-no-area-claim-v2",
                "data-source-anchor": "feature-representative-point",
                "data-symbol-offset-mm": f"{offset_x:g},{offset_y:g}",
            },
        )
    return len(symbol)


def _green_layer_exclusion(
    layer: ArtworkLayer,
    *,
    base: BaseGeometry,
) -> BaseGeometry:
    """Protect already accepted green ink from retained marker collisions."""

    rendered = [
        LineString(record.points).buffer(
            GREEN_RETENTION_INK_CLEARANCE_MM,
            cap_style="round",
            join_style="round",
        )
        for record in layer.records
        if len(record.points) >= 2
    ]
    if not rendered:
        return base
    return make_valid(unary_union((base, *rendered)))


def _retain_source_anchored_green_symbols(
    layer: ArtworkLayer,
    *,
    features: Sequence[dict[str, Any]],
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    area_exclusion: BaseGeometry,
    attributes: dict[str, str],
    variant_id: str | None,
) -> dict[str, Any]:
    """Retain sparse factual green marks across occupied route-axis bands.

    Exact source polygons remain the preferred rendering.  This pass only
    helps selected polygons that became sub-legible or were fully consumed by
    route/copy masks.  It first covers otherwise empty route-axis bands, then
    missing land-cover kinds, and finally missing kind/band pairs.  Every mark
    stays tied to the source representative point, declares that it makes no
    area claim, and is rejected when no collision-free position exists.
    """

    candidates = _green_retention_candidates(
        features,
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
    )
    candidate_by_id = {
        str(candidate.feature["id"]): candidate for candidate in candidates
    }
    rendered_ids = {
        str(record.attributes["data-feature-id"])
        for record in layer.records
        if record.attributes.get("data-feature-id") in candidate_by_id
    }
    initial_rendered_ids = set(rendered_ids)
    maximum_features = GREEN_RETENTION_MAX_FEATURES.get(
        str(variant_id), GREEN_RETENTION_MAX_FEATURES["detailed-map"]
    )
    available_budget = max(0, maximum_features - len(rendered_ids))
    attempted_ids: set[str] = set()
    retained_ids: list[str] = []
    represented_bands = {
        candidate_by_id[feature_id].route_band for feature_id in rendered_ids
    }
    represented_kinds = {
        candidate_by_id[feature_id].kind for feature_id in rendered_ids
    }
    represented_slots = {
        (
            candidate_by_id[feature_id].kind,
            candidate_by_id[feature_id].route_band,
        )
        for feature_id in rendered_ids
    }

    def candidate_score(
        candidate: _GreenRetentionCandidate,
    ) -> tuple[int, float, float, str]:
        return (
            int(candidate.feature.get("priority", 9)),
            float(candidate.feature.get("distance_to_route_m", math.inf)),
            candidate.route_distance_mm,
            str(candidate.feature["id"]),
        )

    def try_scope(scope: Sequence[_GreenRetentionCandidate]) -> bool:
        nonlocal available_budget
        if available_budget <= 0:
            return False
        for candidate in sorted(scope, key=candidate_score):
            feature = candidate.feature
            feature_id = str(feature["id"])
            if feature_id in rendered_ids or feature_id in attempted_ids:
                continue
            attempted_ids.add(feature_id)
            retention_attributes = {
                **attributes,
                "data-feature-id": feature_id,
                "data-feature-kind": candidate.kind,
                "data-landcover-class": candidate.kind,
                "data-landcover-retention-policy": (
                    "source-anchor-route-axis-band-after-collision-gate-v2"
                ),
                "data-route-axis": f"{candidate.route_axis:.4f}",
                "data-route-axis-band": (
                    f"{candidate.route_band + 1}/{GREEN_RETENTION_ROUTE_BANDS}"
                ),
            }
            if feature.get("osm_type") is not None:
                retention_attributes["data-osm-element"] = (
                    f"{feature['osm_type']}/{feature['osm_id']}"
                )
            if feature.get("source_url"):
                retention_attributes["data-source-url"] = str(feature["source_url"])
            if feature.get("distance_to_route_m") is not None:
                retention_attributes["data-distance-to-route-m"] = (
                    f"{float(feature['distance_to_route_m']):g}"
                )
            exclusion = _green_layer_exclusion(layer, base=area_exclusion)
            if not _add_source_anchored_feature_symbol(
                layer,
                anchor=candidate.anchor,
                map_rect=map_rect,
                exclusion=exclusion,
                source_ref=str(feature["source_ref"]),
                sequence=candidate.feature_index,
                attributes=retention_attributes,
                semantic_class=candidate.kind,
                offsets=_CONTEXT_SYMBOL_RETENTION_OFFSETS,
            ):
                continue
            available_budget -= 1
            rendered_ids.add(feature_id)
            retained_ids.append(feature_id)
            represented_bands.add(candidate.route_band)
            represented_kinds.add(candidate.kind)
            represented_slots.add((candidate.kind, candidate.route_band))
            return True
        return False

    candidates_by_band: dict[int, list[_GreenRetentionCandidate]] = {}
    candidates_by_kind: dict[str, list[_GreenRetentionCandidate]] = {}
    candidates_by_slot: dict[tuple[str, int], list[_GreenRetentionCandidate]] = {}
    for candidate in candidates:
        candidates_by_band.setdefault(candidate.route_band, []).append(candidate)
        candidates_by_kind.setdefault(candidate.kind, []).append(candidate)
        candidates_by_slot.setdefault(
            (candidate.kind, candidate.route_band), []
        ).append(candidate)

    # Page coverage comes first: no early-route cluster may consume the entire
    # green pen allowance while later selected source bands disappear.
    for route_band in sorted(candidates_by_band):
        if route_band in represented_bands:
            continue
        scope = sorted(
            candidates_by_band[route_band],
            key=lambda candidate: (
                0 if candidate.kind not in represented_kinds else 1,
                *candidate_score(candidate),
            ),
        )
        try_scope(scope)

    # If the selection contains both woodland and grass, make a fair attempt
    # to retain both visual languages before filling additional band slots.
    for kind in ("woodland", "grass"):
        if kind in candidates_by_kind and kind not in represented_kinds:
            try_scope(candidates_by_kind[kind])

    for route_band in range(GREEN_RETENTION_ROUTE_BANDS):
        for kind in ("woodland", "grass"):
            slot = (kind, route_band)
            if slot in represented_slots or slot not in candidates_by_slot:
                continue
            try_scope(candidates_by_slot[slot])

    return {
        "policy_id": "source-anchor-route-axis-band-after-collision-gate-v2",
        "route_axis_band_count": GREEN_RETENTION_ROUTE_BANDS,
        "retention_target_unique_green_features": maximum_features,
        "preferred_source_geometry_exceeds_target": (
            len(initial_rendered_ids) > maximum_features
        ),
        "selected_source_feature_count": len(candidates),
        "initial_rendered_feature_count": len(initial_rendered_ids),
        "retained_feature_count": len(retained_ids),
        "final_rendered_feature_count": len(rendered_ids),
        "occupied_source_bands": sorted(candidates_by_band),
        "rendered_route_axis_bands": sorted(represented_bands),
        "retained_feature_ids": retained_ids,
        "area_claim": "none-for-retained-symbols",
    }


def _grass_symbol_centres(
    polygon: Polygon,
    *,
    exclusion: BaseGeometry,
    seed: int,
) -> list[Point]:
    """Find sparse factual grass anchors away from route and label masks."""

    available: BaseGeometry = make_valid(polygon.buffer(-0.8))
    if available.is_empty:
        return []
    if not exclusion.is_empty:
        available = make_valid(available.difference(exclusion.buffer(0.2)))
    if available.is_empty:
        return []
    parts = sorted(_geometry_polygons(available), key=lambda item: -item.area)
    candidates: list[Point] = [
        (float(point.x), float(point.y))
        for part in parts
        if part.area >= 0.5
        for point in (part.representative_point(),)
    ]
    minimum_x, minimum_y, maximum_x, maximum_y = available.bounds
    pitch = 3.8
    phase_x = (seed % 7) * pitch / 7.0
    phase_y = ((seed // 7) % 7) * pitch / 7.0
    y = minimum_y + phase_y
    while y <= maximum_y and len(candidates) < 40:
        x = minimum_x + phase_x
        while x <= maximum_x and len(candidates) < 40:
            if available.covers(GeometryPoint(x, y)):
                candidates.append((x, y))
            x += pitch
        y += pitch
    selected: list[Point] = []
    for candidate in candidates:
        if any(
            math.hypot(candidate[0] - x, candidate[1] - y) < 4.0 for x, y in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= 5:
            break
    return selected


def _woodland_symbol_centres(polygon: Polygon, *, seed: int) -> list[Point]:
    if polygon.is_empty or polygon.area < 4.0:
        return []
    minimum_x, minimum_y, maximum_x, maximum_y = polygon.bounds
    interior = polygon.buffer(-1.0)
    if interior.is_empty:
        return []
    representative = interior.representative_point()
    centres: list[Point] = [(float(representative.x), float(representative.y))]
    row = 0
    y = minimum_y + 2.5
    while y < maximum_y - 2.5 and len(centres) < 6:
        x = minimum_x + 2.5 + ((seed + row * 11) % 5) * 0.55
        while x < maximum_x - 2.5 and len(centres) < 6:
            candidate = GeometryPoint(x, y)
            if interior.contains(candidate) and all(
                candidate.distance(GeometryPoint(existing)) >= 5.0
                for existing in centres
            ):
                centres.append((x, y))
            x += 7.8
        y += 7.0
        row += 1
    return centres


@dataclass(frozen=True)
class _LabelPlacement:
    feature_id: str
    kind: str
    text: str
    point: Point
    x: float
    y: float
    anchor: str
    box: Rect
    source_ref: str
    source_url: str | None
    displaced: bool
    source_displacement_mm: float
    maximum_displacement_mm: float


@dataclass(frozen=True)
class _NorthArrowPlacement:
    tip: Point
    base: Point
    box: Rect


def _north_arrow_placement(
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
) -> _NorthArrowPlacement:
    """Reserve a compact page-up compass mark away from the route."""

    route_geometry: BaseGeometry = (
        unary_union([LineString(line) for line in route_lines if len(line) >= 2])
        if route_lines
        else Polygon()
    )
    x_positions = (
        map_rect.right - 3.2,
        map_rect.left + 3.2,
        map_rect.left + map_rect.width * 0.75,
        map_rect.left + map_rect.width * 0.25,
        map_rect.centre[0],
    )
    y_positions = (
        map_rect.top + 4.2,
        map_rect.bottom - 6.2,
        map_rect.top + map_rect.height * 0.38,
        map_rect.top + map_rect.height * 0.68,
    )
    candidates: list[tuple[float, _NorthArrowPlacement]] = []
    for y in y_positions:
        for x in x_positions:
            reserved = Rect(x - 2.4, y - 3.6, 4.8, 9.8)
            reserved_geometry = box(
                reserved.left,
                reserved.top,
                reserved.right,
                reserved.bottom,
            )
            clearance = (
                float(route_geometry.distance(reserved_geometry))
                if not route_geometry.is_empty
                else math.inf
            )
            placement = _NorthArrowPlacement(
                tip=(x, y),
                base=(x, y + 4.6),
                box=reserved,
            )
            if clearance >= 0.8:
                return placement
            candidates.append((clearance, placement))
    # A long route can legitimately touch every coarse candidate.  Keeping
    # the farthest page position is deterministic; subsequent label placement
    # still reserves the full compass box.
    return max(candidates, key=lambda item: item[0])[1]


def _rectangles_overlap(first: Rect, second: Rect, *, gap: float = 0.7) -> bool:
    return not (
        first.right + gap <= second.left
        or second.right + gap <= first.left
        or first.bottom + gap <= second.top
        or second.bottom + gap <= first.top
    )


def _point_to_rect_distance(point: Point, rect: Rect) -> float:
    """Return the shortest page-space distance from a point to a rectangle."""

    delta_x = max(rect.left - point[0], 0.0, point[0] - rect.right)
    delta_y = max(rect.top - point[1], 0.0, point[1] - rect.bottom)
    return math.hypot(delta_x, delta_y)


def _map_chainage_reservation(point: Point) -> Rect:
    """Reserve the exact on-route station symbol and its centred A--E copy."""

    radius = MAP_CHAINAGE_RESERVATION_RADIUS_MM
    return Rect(point[0] - radius, point[1] - radius, 2.0 * radius, 2.0 * radius)


def _coincident_chainage_label_layout(
    anchor: Point, map_rect: Rect
) -> tuple[dict[str, Point], Rect]:
    """Place coincident A/E copy with room for factual anchor leaders.

    Loop endpoints share one geographic symbol.  Printing both letters on that
    symbol makes neither legible; silently nudging them, however, makes the copy
    look like two geographic points.  This deterministic layout keeps the
    marker at the exact anchor and reserves the displaced letters plus their
    short leaders before context placement.
    """

    x, y = anchor
    candidates = (
        {"A": (x - 3.8, y), "E": (x + 3.8, y)},
        (
            {"A": (x + 3.8, y - 0.9), "E": (x + 3.8, y + 0.9)}
            if x <= map_rect.centre[0]
            else {"A": (x - 3.8, y - 0.9), "E": (x - 3.8, y + 0.9)}
        ),
        {"A": (x, y - 3.8), "E": (x, y + 3.8)},
    )
    for centres in candidates:
        minimum_x = min(x - 1.8, *(point[0] - 1.0 for point in centres.values()))
        maximum_x = max(x + 1.8, *(point[0] + 1.0 for point in centres.values()))
        minimum_y = min(y - 1.8, *(point[1] - 1.2 for point in centres.values()))
        maximum_y = max(y + 1.8, *(point[1] + 1.2 for point in centres.values()))
        reservation = Rect(
            minimum_x,
            minimum_y,
            maximum_x - minimum_x,
            maximum_y - minimum_y,
        )
        if (
            reservation.left >= map_rect.left + 0.4
            and reservation.right <= map_rect.right - 0.4
            and reservation.top >= map_rect.top + 0.4
            and reservation.bottom <= map_rect.bottom - 0.4
        ):
            return centres, reservation
    # Route geometry is inset from the map field, so the inward-facing side
    # candidate above normally fits.  Fail closed to the exact compact symbol
    # if a caller supplies an unusually tight synthetic field.
    return {"A": anchor, "E": anchor}, _map_chainage_reservation(anchor)


def _chainage_label_leader(anchor: Point, label_centre: Point) -> list[Point] | None:
    """Return a short straight leader from the factual marker to displaced copy."""

    delta_x = label_centre[0] - anchor[0]
    delta_y = label_centre[1] - anchor[1]
    distance = math.hypot(delta_x, delta_y)
    if distance <= 3.0:
        return None
    direction_x = delta_x / distance
    direction_y = delta_y / distance
    start = (anchor[0] + 1.8 * direction_x, anchor[1] + 1.8 * direction_y)
    end = (
        label_centre[0] - 1.0 * direction_x,
        label_centre[1] - 1.0 * direction_y,
    )
    if math.hypot(end[0] - start[0], end[1] - start[1]) < MIN_FINE_STROKE_MM:
        return None
    return [start, end]


def _label_identity(value: object) -> str:
    """Return a spacing/punctuation-insensitive identity for map copy."""

    return "".join(
        character
        for character in plotter_copy(str(value)).casefold()
        if character.isalnum()
    )


def _label_base_identity(feature: dict[str, Any]) -> str:
    """Return the source name before renderer-owned informational suffixes."""

    declared = _label_identity(feature.get("_base_label_identity", ""))
    if declared:
        return declared
    identity = _label_identity(feature.get("label", ""))
    elevation = feature.get("elevation_m")
    if (
        isinstance(elevation, (int, float))
        and not isinstance(elevation, bool)
        and math.isfinite(float(elevation))
    ):
        suffix = _label_identity(f"/ {float(elevation):.0f} M")
        if suffix and identity.endswith(suffix):
            base_identity = identity[: -len(suffix)]
            if base_identity:
                return base_identity
    return identity


def _label_repeat_family(kind: str) -> str:
    """Group only source kinds that describe the same repeated map name."""

    return "hydrography" if kind in {"river", "water"} else kind


def _label_information_order(
    feature: dict[str, Any], *, default_priority: int = 9
) -> tuple[int, int, int, int, int, str]:
    """Prefer richer deterministic copy before applying proximity de-duplication."""

    elevation = feature.get("elevation_m")
    has_elevation = (
        isinstance(elevation, (int, float))
        and not isinstance(elevation, bool)
        and math.isfinite(float(elevation))
    )
    raw_priority = feature.get("priority", default_priority)
    priority = (
        int(raw_priority)
        if isinstance(raw_priority, (int, float)) and not isinstance(raw_priority, bool)
        else default_priority
    )
    rendered_identity = _label_identity(feature.get("label", ""))
    base_identity = _label_base_identity(feature)
    extra_information = max(0, len(rendered_identity) - len(base_identity))
    kind_rank = {"river": 0, "water": 1}.get(str(feature.get("kind", "")), 0)
    return (
        0 if feature.get("route_control") else 1,
        0 if has_elevation else 1,
        priority,
        -extra_information,
        kind_rank,
        str(feature.get("id", "")),
    )


def _segment_intersects_rect(start: Point, end: Point, rect: Rect) -> bool:
    return (
        _clip_segment_to_extent(
            start,
            end,
            (rect.left, rect.top, rect.right, rect.bottom),
        )
        is not None
    )


def _safe_label_leader(
    start: Point,
    label_box: Rect,
    *,
    map_rect: Rect,
    obstacle_boxes: Sequence[Rect],
    route_lines: Sequence[Sequence[Point]],
    existing_leaders: Sequence[Sequence[Point]] = (),
    minimum_clearance_mm: float = 0.3,
    maximum_length_mm: float | None = None,
) -> list[Point] | None:
    """Route a label leader without crossing foreign copy or plotted leaders.

    Labels are selected before their leaders are emitted.  A direct anchor-to-copy
    segment can therefore be clear at both ends yet cut through a third label.  The
    candidate router treats every foreign label/reserved panel as a hard obstacle,
    keeps clear of the hero route after leaving the source marker, and rejects
    leader/leader crossings.  The deterministic orthogonal alternatives are useful
    on a pen plotter: they need no invented geographic curve and make every pen-up
    decision explicit.
    """

    target = (
        min(max(start[0], label_box.left), label_box.right),
        min(max(start[1], label_box.top), label_box.bottom),
    )
    if math.hypot(start[0] - target[0], start[1] - target[1]) < 0.75:
        return None

    obstacles: BaseGeometry = (
        unary_union(
            [
                box(item.left, item.top, item.right, item.bottom).buffer(
                    minimum_clearance_mm,
                    cap_style="square",
                    join_style="mitre",
                )
                for item in obstacle_boxes
            ]
        )
        if obstacle_boxes
        else Polygon()
    )
    route_geometry: BaseGeometry = (
        unary_union(
            [LineString(line) for line in route_lines if len(line) >= 2]
        ).buffer(
            LEADER_HERO_ROUTE_CLEARANCE_MM,
            cap_style="round",
            join_style="round",
        )
        if route_lines
        else Polygon()
    )
    leader_obstacles: BaseGeometry = (
        unary_union(
            [
                LineString(line).buffer(
                    minimum_clearance_mm,
                    cap_style="round",
                    join_style="round",
                )
                for line in existing_leaders
                if len(line) >= 2
            ]
        )
        if existing_leaders
        else Polygon()
    )

    x_corridors = {
        (start[0] + target[0]) / 2.0,
        min(start[0], target[0]) - 2.2,
        max(start[0], target[0]) + 2.2,
    }
    y_corridors = {
        (start[1] + target[1]) / 2.0,
        min(start[1], target[1]) - 2.2,
        max(start[1], target[1]) + 2.2,
    }
    for item in obstacle_boxes:
        x_corridors.update((item.left - 0.8, item.right + 0.8))
        y_corridors.update((item.top - 0.8, item.bottom + 0.8))

    raw_candidates: list[list[Point]] = [
        [start, target],
        [start, (target[0], start[1]), target],
        [start, (start[0], target[1]), target],
    ]
    raw_candidates.extend(
        [start, (corridor, start[1]), (corridor, target[1]), target]
        for corridor in sorted(x_corridors)
    )
    raw_candidates.extend(
        [start, (start[0], corridor), (target[0], corridor), target]
        for corridor in sorted(y_corridors)
    )

    candidates: list[tuple[float, int, tuple[Point, ...], list[Point]]] = []
    for raw in raw_candidates:
        candidate: list[Point] = []
        for point in raw:
            if (
                not candidate
                or math.hypot(point[0] - candidate[-1][0], point[1] - candidate[-1][1])
                > 1e-9
            ):
                candidate.append(point)
        if len(candidate) < 2:
            continue
        if any(
            point[0] < map_rect.left + 0.25
            or point[0] > map_rect.right - 0.25
            or point[1] < map_rect.top + 0.25
            or point[1] > map_rect.bottom - 0.25
            for point in candidate
        ):
            continue
        geometry: BaseGeometry = LineString(candidate)
        if not route_geometry.is_empty and geometry.intersects(route_geometry):
            # Route-control and pass anchors can truthfully sit on the red route.
            # Do not draw a black leader through that red ink: remove only the
            # initial portion inside the disclosed route-clearance envelope.  A
            # later route re-entry produces multiple visible parts and is rejected.
            if not route_geometry.covers(GeometryPoint(start)):
                continue
            route_clear_parts = _geometry_lines(geometry.difference(route_geometry))
            if len(route_clear_parts) != 1:
                continue
            route_clear = route_clear_parts[0]
            route_points = _line_points(route_clear)
            if math.hypot(
                route_points[0][0] - target[0],
                route_points[0][1] - target[1],
            ) < math.hypot(
                route_points[-1][0] - target[0],
                route_points[-1][1] - target[1],
            ):
                route_points.reverse()
            if (
                math.hypot(
                    route_points[-1][0] - target[0],
                    route_points[-1][1] - target[1],
                )
                > 0.01
            ):
                continue
            candidate = route_points
            geometry = LineString(candidate)
        if geometry.length < 0.75:
            continue
        if maximum_length_mm is not None and geometry.length > maximum_length_mm + 1e-9:
            continue
        if not obstacles.is_empty and geometry.intersects(obstacles):
            continue
        if not route_geometry.is_empty and geometry.intersects(
            route_geometry.buffer(-0.005)
        ):
            continue
        if not leader_obstacles.is_empty and geometry.intersects(leader_obstacles):
            continue
        length = float(geometry.length)
        bends = max(len(candidate) - 2, 0)
        candidates.append(
            (
                length + bends * 0.8,
                bends,
                tuple((round(x, 6), round(y, 6)) for x, y in candidate),
                candidate,
            )
        )
    return min(candidates)[3] if candidates else None


def _context_label_leaders(
    placements: Sequence[_LabelPlacement],
    *,
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    reserved_boxes: Sequence[Rect] = (),
    existing_leaders: Sequence[Sequence[Point]] = (),
) -> tuple[dict[str, list[Point]], tuple[str, ...]]:
    """Resolve all displaced context leaders against the completed label plan."""

    result: dict[str, list[Point]] = {}
    omitted: list[str] = []
    completed: list[list[Point]] = [list(line) for line in existing_leaders]
    for placement in placements:
        if not placement.displaced:
            continue
        obstacles = [
            other.box
            for other in placements
            if other.feature_id != placement.feature_id
        ]
        obstacles.extend(reserved_boxes)
        leader = _safe_label_leader(
            placement.point,
            placement.box,
            map_rect=map_rect,
            obstacle_boxes=obstacles,
            route_lines=route_lines,
            existing_leaders=completed,
            maximum_length_mm=placement.maximum_displacement_mm,
        )
        if leader is None:
            omitted.append(placement.feature_id)
            continue
        result[placement.feature_id] = leader
        completed.append(leader)
    return result, tuple(omitted)


def _mask_geography_for_contour_labels(
    layers: Sequence[ArtworkLayer],
    label_boxes: Sequence[Rect],
    *,
    clearance_mm: float = CONTOUR_LABEL_GEOGRAPHY_CLEARANCE_MM,
) -> dict[str, Any]:
    """Clip overview geography around boxed contour-altitude copy.

    Contour labels are chosen from factual terrain after the first terrain pass,
    while roads, water and landcover are added later.  Applying the mask to the
    completed overview is therefore the only order-independent way to guarantee
    clear pen copy.  The operation never moves or redraws source geometry: it
    retains exact line substrings outside each box and records every clipped or
    omitted source stroke.
    """

    if not label_boxes:
        return {
            "policy_id": "boxed-contour-copy-geography-mask-v1",
            "clearance_mm": clearance_mm,
            "label_box_count": 0,
            "affected_record_count": 0,
            "omitted_record_count": 0,
            "emitted_part_count": 0,
            "affected_layers": [],
            "role_statistics": {},
        }
    mask = unary_union(
        [
            box(item.left, item.top, item.right, item.bottom).buffer(
                clearance_mm,
                cap_style="square",
                join_style="mitre",
            )
            for item in label_boxes
        ]
    )
    affected_record_count = 0
    omitted_record_count = 0
    emitted_part_count = 0
    affected_layers: list[str] = []
    role_statistics: dict[str, dict[str, int]] = {}
    for layer in layers:
        revised = []
        layer_affected = False
        for record in layer.records:
            # Local detail panels have their own frame, scale and copy plan.  Main
            # contour labels are deliberately never placed in those reserved boxes.
            if record.attributes.get("data-context-view"):
                revised.append(record)
                continue
            geometry = LineString(record.points)
            if not geometry.intersects(mask):
                revised.append(record)
                continue
            layer_affected = True
            affected_record_count += 1
            role = str(record.role or "unclassified")
            role_summary = role_statistics.setdefault(
                role,
                {
                    "affected_record_count": 0,
                    "omitted_record_count": 0,
                    "emitted_part_count": 0,
                },
            )
            role_summary["affected_record_count"] += 1
            minimum_fragment_mm = 3.0 * layer.pen.mark_width_mm
            parts = [
                part
                for part in _geometry_lines(geometry.difference(mask))
                if float(part.length) + 1e-9 >= minimum_fragment_mm
            ]
            if not parts:
                omitted_record_count += 1
                role_summary["omitted_record_count"] += 1
                continue
            emitted_part_count += len(parts)
            role_summary["emitted_part_count"] += len(parts)
            for part_index, part in enumerate(parts, start=1):
                clipped = copy.copy(record)
                clipped.points = _line_points(part)
                clipped.attributes = {
                    **record.attributes,
                    "data-copy-legibility-mask-policy": (
                        "boxed-contour-copy-geography-mask-v1"
                    ),
                    "data-copy-legibility-mask-clearance-mm": f"{clearance_mm:g}",
                    "data-source-geometry-treatment": (
                        "exact-page-substring-clipped-never-displaced"
                    ),
                    "data-copy-legibility-mask-minimum-fragment-mm": (
                        f"{minimum_fragment_mm:g}"
                    ),
                    "data-copy-legibility-mask-part": (f"{part_index}/{len(parts)}"),
                }
                revised.append(clipped)
        layer.records[:] = revised
        if layer_affected:
            affected_layers.append(layer.id)
    return {
        "policy_id": "boxed-contour-copy-geography-mask-v1",
        "clearance_mm": clearance_mm,
        "label_box_count": len(label_boxes),
        "affected_record_count": affected_record_count,
        "omitted_record_count": omitted_record_count,
        "emitted_part_count": emitted_part_count,
        "affected_layers": affected_layers,
        "role_statistics": role_statistics,
        "source_geometry_policy": "exact-page-substrings-clipped-never-displaced",
    }


def _reconcile_green_context_after_contour_label_mask(
    layer: ArtworkLayer,
    rendering: dict[str, Any],
    *,
    role_statistics: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Reconcile retained green-feature counts with final emitted SVG ink.

    Green retention runs before contour-altitude labels are known.  The final
    copy-legibility mask may therefore remove every stroke belonging to one of
    those retained source features.  Preserve the pre-mask count for audit,
    then make the primary final count describe unique source IDs that actually
    remain in the completed woodland layer.
    """

    rendered_feature_ids = sorted(
        {
            str(feature_id)
            for record in layer.records
            if record.role in LAND_COVER_PATH_ROLES
            and not record.attributes.get("data-context-view")
            and (feature_id := record.attributes.get("data-feature-id"))
        }
    )
    pre_mask_count = int(rendering.get("final_rendered_feature_count", 0))
    affected_record_count = sum(
        int(summary.get("affected_record_count", 0))
        for role, summary in role_statistics.items()
        if role in LAND_COVER_PATH_ROLES
    )
    omitted_record_count = sum(
        int(summary.get("omitted_record_count", 0))
        for role, summary in role_statistics.items()
        if role in LAND_COVER_PATH_ROLES
    )
    emitted_part_count = sum(
        int(summary.get("emitted_part_count", 0))
        for role, summary in role_statistics.items()
        if role in LAND_COVER_PATH_ROLES
    )
    rendering.update(
        {
            "pre_copy_mask_final_rendered_feature_count": pre_mask_count,
            "copy_mask_affected_landcover_record_count": affected_record_count,
            "copy_mask_omitted_landcover_record_count": omitted_record_count,
            "copy_mask_emitted_landcover_path_part_count": emitted_part_count,
            "copy_mask_omitted_feature_count": max(
                0,
                pre_mask_count - len(rendered_feature_ids),
            ),
            "final_rendered_feature_count": len(rendered_feature_ids),
            "post_copy_mask_rendered_feature_ids": rendered_feature_ids,
            "final_count_policy": (
                "unique-rendered-source-feature-ids-after-copy-mask-v1"
            ),
        }
    )
    return rendering


def _reconcile_fall_lines_after_contour_label_mask(
    layer: ArtworkLayer,
    rendering: dict[str, Any],
    *,
    role_statistics: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Re-run the disclosed cluster gate after boxed-copy clipping.

    One retained DEM fall line can become two plotted substrings when a contour
    label is knocked out of its middle.  The manifest must count those final
    pen paths, not the pre-mask source candidate.  Reapplying the same factual
    cluster gate also prevents one or two isolated remnants from masquerading
    as a valid relief cluster.
    """

    candidates = [
        record
        for record in layer.records
        if record.role == "source-derived-dem-fall-line-hachure"
        and not record.attributes.get("data-context-view")
    ]
    pre_mask_eligible = int(rendering["clearance_eligible_path_count"])
    pre_mask_rejected = int(rendering["cluster_rejected_path_count"])
    pre_mask_retained = int(rendering["retained_path_count"])
    retained_indices = _runtime_fall_line_cluster_indices(
        [record.points for record in candidates],
        connectivity_distance_mm=float(rendering["cluster_connectivity_distance_mm"]),
        minimum_strokes=int(rendering["minimum_cluster_strokes"]),
        minimum_total_mm=float(rendering["minimum_cluster_total_mm"]),
    )
    candidate_ids = {id(record): index for index, record in enumerate(candidates)}
    layer.records[:] = [
        record
        for record in layer.records
        if id(record) not in candidate_ids
        or candidate_ids[id(record)] in retained_indices
    ]
    retained_count = len(retained_indices)
    candidate_count = len(candidates)
    rejected_count = candidate_count - retained_count
    source_count = int(rendering["source_stroke_count"])
    fall_line_mask = role_statistics.get(
        "source-derived-dem-fall-line-hachure",
        {
            "affected_record_count": 0,
            "omitted_record_count": 0,
            "emitted_part_count": 0,
        },
    )
    rendering.update(
        {
            "clearance_eligible_path_count": candidate_count,
            "cluster_rejected_path_count": rejected_count,
            "retained_path_count": retained_count,
            "omission_reason": (
                FALL_LINE_NO_SOURCE_OMISSION_REASON
                if source_count == 0
                else FALL_LINE_NO_CLUSTER_OMISSION_REASON
                if retained_count == 0
                else None
            ),
            "final_count_policy": ("rendered-overview-path-parts-after-copy-mask-v1"),
            "pre_copy_mask_clearance_eligible_path_count": pre_mask_eligible,
            "pre_copy_mask_cluster_rejected_path_count": pre_mask_rejected,
            "pre_copy_mask_retained_path_count": pre_mask_retained,
            "copy_mask_candidate_path_count": candidate_count,
            "copy_mask_affected_path_count": int(
                fall_line_mask["affected_record_count"]
            ),
            "copy_mask_omitted_path_count": int(fall_line_mask["omitted_record_count"]),
            "copy_mask_emitted_path_part_count": int(
                fall_line_mask["emitted_part_count"]
            ),
            "post_copy_mask_cluster_rejected_path_count": rejected_count,
            "copy_mask_policy_id": "boxed-contour-copy-geography-mask-v1",
        }
    )
    return rendering


def _hatch_conflicts(
    stroke: Sequence[Point],
    *,
    route_lines: Sequence[Sequence[Point]],
    label_boxes: Sequence[Rect],
    route_clearance_mm: float = 1.15,
) -> bool:
    expanded_boxes = [
        Rect(box.x - 0.6, box.y - 0.6, box.width + 1.2, box.height + 1.2)
        for box in label_boxes
    ]
    for first, second in zip(stroke, stroke[1:]):
        if any(_segment_intersects_rect(first, second, box) for box in expanded_boxes):
            return True
        midpoint = ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
        for line in route_lines:
            for route_start, route_end in zip(line, line[1:]):
                if (
                    min(
                        _point_segment_distance(first, route_start, route_end),
                        _point_segment_distance(midpoint, route_start, route_end),
                        _point_segment_distance(second, route_start, route_end),
                    )
                    < route_clearance_mm
                ):
                    return True
    return False


def _label_candidates(
    point: Point,
    *,
    text: str,
    kind: str,
    source_ref: str,
    source_url: str | None,
    feature_id: str,
    map_rect: Rect,
    route_control: bool = False,
    allow_gutter: bool = False,
    required_context_label: bool = False,
) -> list[_LabelPlacement]:
    if kind == "range":
        cap_height = 3.2
    elif kind in {"sea", "coast", "park"}:
        cap_height = 2.3
    else:
        cap_height = 2.0
    width = text_width_mm(plotter_copy(text), cap_height_mm=cap_height)
    maximum_displacement_mm = (
        MAX_ROUTE_CONTROL_LABEL_DISPLACEMENT_MM
        if route_control
        else 48.0
        if required_context_label
        else MAX_CONTEXT_LABEL_DISPLACEMENT_MM
    )
    ordinary_candidates: list[tuple[float, float, str, bool]] = [
        (point[0] + 2.0, point[1] - cap_height / 2.0, "start", False),
        (point[0] - 2.0, point[1] - cap_height / 2.0, "end", False),
        (point[0], point[1] - cap_height - 2.0, "middle", True),
        (point[0], point[1] + 2.0, "middle", True),
        (point[0] + 5.0, point[1] - cap_height / 2.0, "start", True),
        (point[0] - 5.0, point[1] - cap_height / 2.0, "end", True),
        (point[0] + 9.0, point[1] - cap_height - 2.4, "start", True),
        (point[0] - 9.0, point[1] + 2.4, "end", True),
        (point[0] + 13.0, point[1] + 2.4, "start", True),
        (point[0] - 13.0, point[1] - cap_height - 2.4, "end", True),
    ]
    endpoint_candidates: list[tuple[float, float, str, bool]] = [
        (point[0] + 3.0, point[1] - cap_height - 4.0, "start", True),
        (point[0] - 3.0, point[1] - cap_height - 4.0, "end", True),
        (point[0] + 3.0, point[1] + 4.0, "start", True),
        (point[0] - 3.0, point[1] + 4.0, "end", True),
    ]
    edge_candidate = (
        (map_rect.left + 1.0, map_rect.top + 1.0, "start", True)
        if point[0] <= map_rect.centre[0]
        else (map_rect.right - 1.0, map_rect.top + 1.0, "end", True)
    )
    gutter_y = min(
        max(point[1] - cap_height / 2.0, map_rect.top + 1.0),
        map_rect.bottom - cap_height - 1.0,
    )
    gutter_candidates = [
        (map_rect.left + 1.0, gutter_y, "start", True),
        (map_rect.right - 1.0, gutter_y, "end", True),
    ]
    prefer_right = bool(
        int(hashlib.sha256(feature_id.encode("utf-8")).hexdigest()[:2], 16) % 2
    )
    if prefer_right:
        gutter_candidates.reverse()
    if allow_gutter and not route_control:
        band_ys = sorted(
            {
                min(
                    max(map_rect.top + map_rect.height * fraction, map_rect.top + 1.0),
                    map_rect.bottom - cap_height - 1.0,
                )
                for fraction in (0.14, 0.3, 0.46, 0.62, 0.78, 0.9)
            },
            key=lambda candidate_y: (abs(candidate_y - gutter_y), candidate_y),
        )
        for band_y in band_ys:
            pair = [
                (map_rect.left + 1.0, band_y, "start", True),
                (map_rect.right - 1.0, band_y, "end", True),
            ]
            if prefer_right:
                pair.reverse()
            gutter_candidates.extend(pair)
    if not allow_gutter:
        gutter_candidates = []
    candidates = (
        endpoint_candidates + [edge_candidate] + gutter_candidates + ordinary_candidates
        if route_control
        else ordinary_candidates + gutter_candidates + [edge_candidate]
        if required_context_label
        else ordinary_candidates + gutter_candidates
    )
    result: list[_LabelPlacement] = []
    for x, y, anchor, displaced in candidates:
        if anchor == "start":
            left = x
        elif anchor == "end":
            left = x - width
        else:
            left = x - width / 2.0
        # v4 accents rise by at most 1.3 font units.  Reserve that ascent in
        # collision and crop checks instead of pretending the plotted mark is
        # contained by the unaccented capital box.
        box = Rect(left - 0.35, y - 0.55, width + 0.7, cap_height + 0.9)
        if (
            box.left < map_rect.left + 0.4
            or box.right > map_rect.right - 0.4
            or box.top < map_rect.top + 0.4
            or box.bottom > map_rect.bottom - 0.4
        ):
            continue
        source_displacement_mm = _point_to_rect_distance(point, box)
        if source_displacement_mm > maximum_displacement_mm + 1e-9:
            continue
        result.append(
            _LabelPlacement(
                feature_id=feature_id,
                kind=kind,
                text=text,
                point=point,
                x=x,
                y=y,
                anchor=anchor,
                box=box,
                source_ref=source_ref,
                source_url=source_url,
                displaced=displaced,
                source_displacement_mm=source_displacement_mm,
                maximum_displacement_mm=maximum_displacement_mm,
            )
        )
    return result


def _place_labels(
    features: Sequence[dict[str, Any]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    reserved_boxes: Sequence[Rect] = (),
    geography_avoidance: BaseGeometry | None = None,
) -> tuple[list[_LabelPlacement], int]:
    occupied: list[Rect] = list(reserved_boxes)
    placements: list[_LabelPlacement] = []
    placed_kind_counts: dict[str, int] = {}
    placed_repeat_points: dict[tuple[str, str], list[Point]] = {}
    omitted = 0
    priority = {
        "settlement": 0,
        "hut": 1,
        "pass": 1,
        "water": 2,
        "sea": 2,
        "river": 2,
        "coast": 2,
        "peak": 3,
        "range": 4,
        "park": 4,
    }
    nearby_repeat_groups: dict[tuple[str, str], list[tuple[str, Point]]] = {}
    for feature in features:
        kind = str(feature.get("kind", ""))
        repeat_key = (_label_repeat_family(kind), _label_base_identity(feature))
        if repeat_key[1]:
            nearby_repeat_groups.setdefault(repeat_key, []).append(
                (str(feature.get("id", "")), physical_point(feature["point"]))
            )
    nearby_repeat_ids: set[str] = set()
    for repeat_candidates in nearby_repeat_groups.values():
        for index, (left_id, left_point) in enumerate(repeat_candidates):
            for right_id, right_point in repeat_candidates[index + 1 :]:
                if (
                    math.hypot(
                        left_point[0] - right_point[0],
                        left_point[1] - right_point[1],
                    )
                    < REPEATED_LABEL_CLEARANCE_MM
                ):
                    nearby_repeat_ids.update((left_id, right_id))
    guaranteed_peak_id = next(
        (
            str(feature["id"])
            for feature in sorted(
                features,
                key=lambda feature: _label_information_order(
                    feature,
                    default_priority=priority.get(str(feature["kind"]), 9),
                ),
            )
            if feature.get("kind") == "peak" and feature.get("elevation_m") is not None
        ),
        None,
    )
    required_label_ids = {
        str(feature["id"])
        for feature in features
        if feature.get("label_required") is True
    }
    hidden_required_ids = {
        str(feature["id"])
        for feature in features
        if feature.get("label_required") is True
        and feature.get("display_label") is False
    }
    if hidden_required_ids:
        raise MapPlotterError(
            "Required context labels cannot be hidden: "
            + ", ".join(sorted(hidden_required_ids))
        )
    guaranteed_mountain_id = guaranteed_peak_id or next(
        (
            str(feature["id"])
            for feature in sorted(
                features,
                key=lambda feature: _label_information_order(
                    feature,
                    default_priority=priority.get(str(feature["kind"]), 9),
                ),
            )
            if feature.get("kind") in {"peak", "range", "pass"}
        ),
        None,
    )
    guaranteed_hydro_features = [
        feature
        for feature in sorted(
            features,
            key=lambda feature: _label_information_order(
                feature,
                default_priority=priority.get(str(feature["kind"]), 9),
            ),
        )
        if feature.get("kind") in {"water", "sea", "river", "coast"}
        and feature.get("display_label") is not False
        and bool(str(feature.get("label", "")).strip())
    ]

    def placement_order(feature: dict[str, Any]) -> tuple[object, ...]:
        information = _label_information_order(
            feature,
            default_priority=priority.get(str(feature["kind"]), 9),
        )
        return (
            0
            if feature.get("route_control")
            else 1
            if str(feature["id"]) in required_label_ids
            else 2
            if str(feature["id"]) == guaranteed_mountain_id
            else 3,
            *information[:3],
            0 if str(feature["id"]) in nearby_repeat_ids else 1,
            *information[3:],
        )

    ordered = sorted(features, key=placement_order)
    for feature in ordered:
        if feature.get("display_label") is False:
            continue
        kind = str(feature["kind"])
        if kind not in priority:
            continue
        feature_id = str(feature["id"])
        required = feature_id in required_label_ids
        if kind == "peak" and placed_kind_counts.get(kind, 0) >= 5 and not required:
            omitted += 1
            continue
        point = physical_point(feature["point"])
        repeat_key = (_label_repeat_family(kind), _label_base_identity(feature))
        # Linear OSM objects are often split into several adjacent ways.  At
        # A5, labelling each fragment produces a stack of identical river or
        # road names rather than useful geographic context.  Keep repetitions
        # only when they are far enough apart to orient a reader elsewhere on
        # the map.
        if not required and repeat_key[1] and any(
            math.hypot(point[0] - previous[0], point[1] - previous[1])
            < REPEATED_LABEL_CLEARANCE_MM
            for previous in placed_repeat_points.get(repeat_key, [])
        ):
            continue
        label_candidates = _label_candidates(
            point,
            text=str(feature["label"]),
            kind=kind,
            source_ref=str(feature["source_ref"]),
            source_url=feature.get("source_url"),
            feature_id=str(feature["id"]),
            map_rect=map_rect,
            route_control=bool(feature.get("route_control")),
            allow_gutter=(
                bool(feature.get("route_control"))
                or str(feature["id"]) in required_label_ids
                or str(feature["id"]) == guaranteed_mountain_id
                or str(feature["id"]) in nearby_repeat_ids
            ),
            required_context_label=str(feature["id"]) in required_label_ids,
        )
        selected = None
        for candidate in label_candidates:
            if any(_rectangles_overlap(candidate.box, box) for box in occupied):
                continue
            if (
                geography_avoidance is not None
                and not geography_avoidance.is_empty
                and str(feature["id"]) not in required_label_ids
                and box(
                    candidate.box.left,
                    candidate.box.top,
                    candidate.box.right,
                    candidate.box.bottom,
                )
                .buffer(0.55, cap_style="square", join_style="mitre")
                .intersects(geography_avoidance)
            ):
                continue
            route_box = Rect(
                candidate.box.x - 0.55,
                candidate.box.y - 0.55,
                candidate.box.width + 1.1,
                candidate.box.height + 1.1,
            )
            if any(
                _segment_intersects_rect(first, second, route_box)
                for line in route_lines
                for first, second in zip(line, line[1:])
            ):
                continue
            selected = candidate
            break
        if selected is None:
            if required:
                raise MapPlotterError(
                    "Required context label "
                    f"{feature_id!r} has no collision-safe page placement."
                )
            omitted += 1
            continue
        occupied.append(selected.box)
        placements.append(selected)
        placed_kind_counts[kind] = placed_kind_counts.get(kind, 0) + 1
        placed_repeat_points.setdefault(repeat_key, []).append(point)
    if guaranteed_hydro_features and not any(
        placement.kind in {"water", "sea", "river", "coast"} for placement in placements
    ):
        selected_hydro: tuple[dict[str, Any], _LabelPlacement] | None = None
        for allow_gutter in (False, True):
            options: list[tuple[float, int, str, dict[str, Any], _LabelPlacement]] = []
            for feature in guaranteed_hydro_features:
                point = physical_point(feature["point"])
                for candidate_index, candidate in enumerate(
                    _label_candidates(
                        point,
                        text=str(feature["label"]),
                        kind=str(feature["kind"]),
                        source_ref=str(feature["source_ref"]),
                        source_url=feature.get("source_url"),
                        feature_id=str(feature["id"]),
                        map_rect=map_rect,
                        allow_gutter=allow_gutter,
                    )
                ):
                    if any(_rectangles_overlap(candidate.box, box) for box in occupied):
                        continue
                    if (
                        geography_avoidance is not None
                        and not geography_avoidance.is_empty
                        and box(
                            candidate.box.left,
                            candidate.box.top,
                            candidate.box.right,
                            candidate.box.bottom,
                        )
                        .buffer(0.55, cap_style="square", join_style="mitre")
                        .intersects(geography_avoidance)
                    ):
                        continue
                    route_box = Rect(
                        candidate.box.x - 0.55,
                        candidate.box.y - 0.55,
                        candidate.box.width + 1.1,
                        candidate.box.height + 1.1,
                    )
                    if any(
                        _segment_intersects_rect(first, second, route_box)
                        for line in route_lines
                        for first, second in zip(line, line[1:])
                    ):
                        continue
                    target_x = min(
                        max(point[0], candidate.box.left), candidate.box.right
                    )
                    target_y = min(
                        max(point[1], candidate.box.top), candidate.box.bottom
                    )
                    leader_mm = math.hypot(point[0] - target_x, point[1] - target_y)
                    options.append(
                        (
                            leader_mm,
                            candidate_index,
                            str(feature["id"]),
                            feature,
                            candidate,
                        )
                    )
            if options:
                _, _, _, feature, selected = min(options, key=lambda item: item[:3])
                selected_hydro = (feature, selected)
                break
        if selected_hydro is not None:
            guaranteed_hydro, selected = selected_hydro
            point = physical_point(guaranteed_hydro["point"])
            occupied.append(selected.box)
            placements.append(selected)
            kind = str(guaranteed_hydro["kind"])
            placed_kind_counts[kind] = placed_kind_counts.get(kind, 0) + 1
            placed_repeat_points.setdefault(
                (
                    _label_repeat_family(kind),
                    _label_base_identity(guaranteed_hydro),
                ),
                [],
            ).append(point)
            omitted = max(0, omitted - 1)
    missing_required_ids = required_label_ids - {
        placement.feature_id for placement in placements
    }
    if missing_required_ids:
        raise MapPlotterError(
            "Required context labels were not placed: "
            + ", ".join(sorted(missing_required_ids))
        )
    return placements, omitted


def _context_role(kind: str) -> str:
    return {
        "water": "source-sampled-lake-boundary",
        "coast": "source-sampled-coastline",
        "river": "source-sampled-river-centreline",
    }.get(kind, "source-sampled-context-line")


def _add_green_polygon(
    layer: ArtworkLayer,
    polygon: Polygon,
    *,
    source_ref: str,
    sequence: int,
    attributes: dict[str, str],
    seed: int,
    semantic_class: str,
    exclusion: BaseGeometry,
    woodland_symbols: bool,
    boundary_exclusion: BaseGeometry | None = None,
) -> int:
    active_boundary_exclusion = (
        exclusion if boundary_exclusion is None else boundary_exclusion
    )
    if polygon.area < 2.2:
        # Area alone is a poor legibility test for long, narrow forest or
        # moor polygons.  Preserve an exact source boundary when its linework
        # is printable at A5; only genuinely line-sublegible polygons fall
        # through to the expressly non-area-claim symbol treatment.
        if (
            polygon.area < MIN_LINEAR_LANDCOVER_AREA_MM2
            or polygon.length < MIN_LINEAR_LANDCOVER_BOUNDARY_MM
        ):
            return 0
        emitted = 0
        for stroke in _masked_boundary_strokes(
            polygon,
            exclusion=active_boundary_exclusion,
        ):
            layer.add(
                stroke,
                source_ref=source_ref,
                role="source-sampled-landcover-boundary",
                sequence=sequence,
                attributes={
                    **attributes,
                    "data-landcover-class": semantic_class,
                    "data-area-rendering": (
                        "source-boundary-perimeter-legible-narrow-area-v1"
                    ),
                    "data-source-page-area-mm2": f"{polygon.area:.4f}",
                    "data-source-page-perimeter-mm": f"{polygon.length:.3f}",
                },
            )
            emitted += 1
        return emitted
    emitted = 0
    if semantic_class == "grass":
        # Large factual moor/grass areas need a readable boundary as well as
        # sparse interior texture.  Small or heavily masked polygons still
        # fall back to source-bounded tufts instead of arbitrary fragments.
        if polygon.area >= 12.0:
            for stroke in _masked_boundary_strokes(
                polygon,
                exclusion=active_boundary_exclusion,
            ):
                layer.add(
                    stroke,
                    source_ref=source_ref,
                    role="source-sampled-landcover-boundary",
                    sequence=sequence,
                    attributes={
                        **attributes,
                        "data-landcover-class": semantic_class,
                        "data-area-rendering": "source-boundary-outline-v5",
                    },
                )
                emitted += 1
        for symbol_centre in _grass_symbol_centres(
            polygon,
            exclusion=exclusion,
            seed=seed + sequence,
        ):
            symbol = _grass_tuft_strokes(symbol_centre, scale=0.58)
            if any(
                not exclusion.is_empty and LineString(stroke).intersects(exclusion)
                for stroke in symbol
            ):
                continue
            for stroke in symbol:
                layer.add(
                    stroke,
                    source_ref=source_ref,
                    role="source-bounded-grass-symbol",
                    sequence=sequence,
                    attributes={
                        **attributes,
                        "data-landcover-class": semantic_class,
                        "data-area-rendering": (
                            "source-polygon-interior-grass-symbol-v1"
                        ),
                    },
                )
                emitted += 1
        return emitted
    # A tiny exact outline reads as an unexplained green island at A5.  The
    # reference language is clearer when small woods use a tree symbol and
    # only substantial, coherent source polygons carry an area boundary.
    if not woodland_symbols or polygon.area >= MIN_WOODLAND_OUTLINE_AREA_MM2:
        for stroke in _masked_boundary_strokes(
            polygon,
            exclusion=active_boundary_exclusion,
        ):
            layer.add(
                stroke,
                source_ref=source_ref,
                role="source-sampled-landcover-boundary",
                sequence=sequence,
                attributes={
                    **attributes,
                    "data-landcover-class": semantic_class,
                    "data-area-rendering": "source-boundary-outline-v4",
                },
            )
            emitted += 1
    if polygon.area >= 18.0:
        for stroke in _bounded_area_strokes(
            polygon,
            spacing_mm=4.5 if woodland_symbols else 4.0,
            angle_deg=14.0,
            inset_mm=0.7,
            exclusion=exclusion,
            phase=seed + sequence * 17,
            limit=18,
        ):
            layer.add(
                stroke,
                source_ref=source_ref,
                role="source-bounded-landcover-band",
                sequence=sequence,
                attributes={
                    **attributes,
                    "data-landcover-class": semantic_class,
                    "data-area-rendering": "source-boundary-bounded-bands-v4",
                },
            )
            emitted += 1
    if woodland_symbols:
        for symbol_centre in _woodland_symbol_centres(polygon, seed=seed + sequence):
            symbol = _evergreen_strokes(symbol_centre, scale=0.65)
            if any(
                not exclusion.is_empty and LineString(stroke).intersects(exclusion)
                for stroke in symbol
            ):
                continue
            for stroke in symbol:
                layer.add(
                    stroke,
                    source_ref=source_ref,
                    role="source-bounded-woodland-symbol",
                    sequence=sequence,
                    attributes={
                        **attributes,
                        "data-landcover-class": semantic_class,
                        "data-area-rendering": "source-boundary-symbol-v3",
                    },
                )
                emitted += 1
    return emitted


_WOODLAND_SYMBOL_OFFSETS: tuple[Point, ...] = (
    *_CONTEXT_SYMBOL_OFFSETS,
    (0.8, -2.8),
    (2.8, -0.8),
    (2.8, 0.8),
    (0.8, 2.8),
    (-0.8, 2.8),
    (-2.8, 0.8),
    (-2.8, -0.8),
    (-0.8, -2.8),
)
_GENERIC_WOODLAND_RETENTION_OFFSETS: tuple[Point, ...] = (
    *_WOODLAND_SYMBOL_OFFSETS,
    (0.0, -4.2),
    (0.0, 6.0),
)


def _add_source_anchored_woodland_symbol(
    layer: ArtworkLayer,
    polygon: Polygon,
    *,
    map_rect: Rect,
    exclusion: BaseGeometry,
    source_ref: str,
    sequence: int,
    attributes: dict[str, str],
    semantic_class: str,
    offsets: Sequence[Point] = _WOODLAND_SYMBOL_OFFSETS,
) -> int:
    """Mark a factual small woodland without pretending to draw its area."""

    anchor = polygon.representative_point()
    anchor_point = (float(anchor.x), float(anchor.y))
    # A small display displacement is preferable to either crossing the hero
    # route or pretending a sub-legible polygon outline is an area.  Metadata
    # records the exact offset and expressly disclaims an area boundary.
    selected: tuple[list[list[Point]], float, float] | None = None
    for offset_x, offset_y in offsets:
        centre = (anchor_point[0] + offset_x, anchor_point[1] + offset_y)
        symbol = _evergreen_strokes(centre, scale=0.65)
        if not all(
            map_rect.left + 0.4 <= point[0] <= map_rect.right - 0.4
            and map_rect.top + 0.4 <= point[1] <= map_rect.bottom - 0.4
            for stroke in symbol
            for point in stroke
        ):
            continue
        if any(
            not exclusion.is_empty and LineString(stroke).intersects(exclusion)
            for stroke in symbol
        ):
            continue
        selected = (symbol, offset_x, offset_y)
        break
    if selected is None:
        return 0
    symbol, offset_x, offset_y = selected
    for stroke in symbol:
        layer.add(
            stroke,
            source_ref=source_ref,
            role="source-anchored-woodland-symbol",
            sequence=sequence,
            attributes={
                **attributes,
                "data-landcover-class": semantic_class,
                "data-area-rendering": "source-anchored-symbol-no-area-claim-v1",
                "data-source-anchor": "polygon-representative-point",
                "data-symbol-offset-mm": f"{offset_x:g},{offset_y:g}",
            },
        )
    return len(symbol)


def _runtime_fall_line_cluster_indices(
    strokes: Sequence[Sequence[Point]],
    *,
    connectivity_distance_mm: float,
    minimum_strokes: int,
    minimum_total_mm: float,
) -> set[int]:
    """Reapply the factual cluster floor after clearance clipping."""

    geometries = [LineString(stroke) for stroke in strokes]
    remaining = set(range(len(geometries)))
    retained: set[int] = set()
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component = [seed]
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            neighbours = [
                candidate
                for candidate in sorted(remaining)
                if geometries[current].distance(geometries[candidate])
                <= connectivity_distance_mm
            ]
            for candidate in neighbours:
                remaining.remove(candidate)
                component.append(candidate)
                frontier.append(candidate)
        component_length_mm = sum(
            polyline_length_mm(strokes[index]) for index in component
        )
        if (
            len(component) >= minimum_strokes
            and component_length_mm >= minimum_total_mm
        ):
            retained.update(component)
    return retained


def _terrain_reservation_boxes(
    terrain: dict[str, Any],
    *,
    map_rect: Rect,
    physical_point: Callable[[Sequence[float]], Point],
    route_lines: Sequence[Sequence[Point]],
    water: dict[str, Any] | None,
    forbidden_boxes: Sequence[Rect] = (),
) -> list[Rect]:
    """Reserve a few dense factual fall-line cells before placing labels."""

    relief_strokes = terrain.get("relief_strokes", [])
    if not relief_strokes:
        return []
    base_exclusion = _fall_line_exclusion_geometry(
        route_lines,
        (),
        water=water,
        physical_point=physical_point,
        map_rect=map_rect,
    )
    columns = 4
    rows = 4
    buckets: dict[tuple[int, int], list[LineString]] = {}
    for fall_line in relief_strokes:
        for stroke in _page_line_strokes(
            fall_line["points"],
            physical_point=physical_point,
            map_rect=map_rect,
            exclusion=base_exclusion,
        ):
            if polyline_length_mm(stroke) < 3.0:
                continue
            geometry = LineString(stroke)
            midpoint = geometry.interpolate(0.5, normalized=True)
            column = min(
                int((float(midpoint.x) - map_rect.left) / map_rect.width * columns),
                columns - 1,
            )
            row = min(
                int((float(midpoint.y) - map_rect.top) / map_rect.height * rows),
                rows - 1,
            )
            buckets.setdefault((max(column, 0), max(row, 0)), []).append(geometry)

    candidates: list[tuple[float, int, Rect]] = []
    for cell, geometries in buckets.items():
        total_mm = sum(float(geometry.length) for geometry in geometries)
        if len(geometries) < 3 or total_mm < 9.0:
            continue
        minimum_x, minimum_y, maximum_x, maximum_y = unary_union(geometries).bounds
        reserved = Rect(
            max(map_rect.left + 0.3, minimum_x - 0.7),
            max(map_rect.top + 0.3, minimum_y - 0.7),
            0.0,
            0.0,
        )
        right = min(map_rect.right - 0.3, maximum_x + 0.7)
        bottom = min(map_rect.bottom - 0.3, maximum_y + 0.7)
        reserved = Rect(
            reserved.x,
            reserved.y,
            max(right - reserved.x, 1.0),
            max(bottom - reserved.y, 1.0),
        )
        if any(
            _rectangles_overlap(reserved, item, gap=0.5) for item in forbidden_boxes
        ):
            continue
        candidates.append((-total_mm, cell[1] * columns + cell[0], reserved))

    selected: list[Rect] = []
    for _negative_length, _cell_index, candidate in sorted(candidates):
        if any(_rectangles_overlap(candidate, item, gap=1.0) for item in selected):
            continue
        selected.append(candidate)
        if len(selected) >= 3:
            break
    return selected


@dataclass(frozen=True)
class _ContourKeyPlacement:
    text: str
    levels_m: tuple[float, ...]
    box: Rect


@dataclass(frozen=True)
class _ContourHierarchy:
    """Physical minor/index classification for already-sourced contour levels."""

    classes_by_level_m: dict[float, str]
    minor_interval_m: float | None
    index_interval_m: float | None
    index_levels_m: tuple[float, ...]
    intermediate_levels_m: tuple[float, ...]
    interval_basis: str

    def classification(self, elevation_m: float) -> str:
        return self.classes_by_level_m.get(float(elevation_m), "minor")

    def as_dict(
        self,
        *,
        minor_pen_id: str,
        index_pen_id: str,
        minor_pen_width_mm: float,
        index_pen_width_mm: float,
    ) -> dict[str, Any]:
        return {
            "policy_id": CONTOUR_HIERARCHY_POLICY_ID,
            "minor_pen_id": minor_pen_id,
            "index_pen_id": index_pen_id,
            "minor_pen_width_mm": minor_pen_width_mm,
            "index_pen_width_mm": index_pen_width_mm,
            "index_every_n_minor_levels": CONTOUR_INDEX_MULTIPLE,
            "minor_interval_m": self.minor_interval_m,
            "index_interval_m": self.index_interval_m,
            "index_levels_m": list(self.index_levels_m),
            "intermediate_levels_m": list(self.intermediate_levels_m),
            "interval_basis": self.interval_basis,
            "fallback_index_used": False,
            "zero_elevation_index_suppressed": True,
            "bathymetry_status": "not-rendered-no-qualified-source",
        }


def _contour_nominal_interval(levels_m: Sequence[float]) -> float | None:
    """Infer the source level interval without inventing intermediate geometry.

    Frozen terrain bundles contain explicit elevation-valued levels, sometimes
    with gaps because whole contour levels were removed by an upstream physical
    page budget.  The modal adjacent interval recovers their dominant declared
    rhythm without allowing one irregular retained gap to create a false,
    unusually fine interval.  Millimetre-of-elevation integerization avoids
    binary-float comparison drift.
    """

    ordered_units = sorted({round(float(level) * 1_000.0) for level in levels_m})
    deltas = [
        second - first
        for first, second in zip(ordered_units, ordered_units[1:])
        if second > first
    ]
    if not deltas:
        return None
    frequencies: dict[int, int] = {}
    for delta in deltas:
        frequencies[delta] = frequencies.get(delta, 0) + 1
    interval_units = min(
        frequencies,
        key=lambda value: (-frequencies[value], value),
    )
    if interval_units <= 0:
        return None
    return interval_units / 1_000.0


def _is_elevation_multiple(elevation_m: float, interval_m: float) -> bool:
    if interval_m <= 0.0:
        return False
    quotient = float(elevation_m) / interval_m
    return math.isclose(quotient, round(quotient), rel_tol=0.0, abs_tol=1e-7)


def _contour_hierarchy(
    terrain: dict[str, Any],
    source_contours: Sequence[dict[str, Any]],
    selected_contours: Sequence[dict[str, Any]],
) -> _ContourHierarchy:
    """Classify factual levels into minor, intermediate and physical index ink.

    Classical fifth-interval index contours are resolved against absolute
    elevation zero, but the zero-metre land contour is deliberately never made
    heavy: on coastal plates that would imitate or fight the sourced shoreline.
    A sparse selected stack with no true fifth-interval level remains entirely
    fine-line terrain; an arbitrary intermediate contour is never promoted and
    called an index.  No contour is interpolated, shifted, connected or
    otherwise invented by this classification.
    """

    source_levels = sorted(
        {float(contour["elevation_m"]) for contour in source_contours}
    )
    selected_levels = sorted(
        {float(contour["elevation_m"]) for contour in selected_contours}
    )
    rendered_interval = terrain.get("rendered_contour_interval_m")
    if (
        isinstance(rendered_interval, (int, float))
        and not isinstance(rendered_interval, bool)
        and math.isfinite(float(rendered_interval))
        and float(rendered_interval) > 0.0
    ):
        minor_interval_m = float(rendered_interval)
        interval_basis = "declared-rendered-contour-interval"
    else:
        declared_levels = terrain.get("contour_levels_m")
        minor_interval_m = (
            _contour_nominal_interval(declared_levels)
            if isinstance(declared_levels, list)
            else None
        )
        interval_basis = "declared-source-level-inventory-modal-interval"
        if minor_interval_m is None:
            minor_interval_m = _contour_nominal_interval(source_levels)
            interval_basis = "renderable-source-level-modal-interval"
    index_interval_m = (
        minor_interval_m * CONTOUR_INDEX_MULTIPLE
        if minor_interval_m is not None
        else None
    )
    index_levels: list[float] = []
    if index_interval_m is not None:
        index_levels = [
            level
            for level in selected_levels
            if level > 0.0 and _is_elevation_multiple(level, index_interval_m)
        ]

    intermediate_levels: list[float] = []
    if minor_interval_m is not None:
        intermediate_interval_m = minor_interval_m * 2.0
        intermediate_levels = [
            level
            for level in selected_levels
            if level not in index_levels
            and level > 0.0
            and _is_elevation_multiple(level, intermediate_interval_m)
        ]
    index_set = set(index_levels)
    # Intermediate rhythmic levels remain useful provenance, but this is a
    # deliberately two-pen physical hierarchy: every non-index contour is a
    # Grey 0.25 minor.  Keeping the emitted class vocabulary binary makes pen
    # partition QA exact and avoids implying a third line weight that does not
    # exist on paper.
    classes = {
        level: "index" if level in index_set else "minor"
        for level in selected_levels
    }
    return _ContourHierarchy(
        classes_by_level_m=classes,
        minor_interval_m=minor_interval_m,
        index_interval_m=index_interval_m,
        index_levels_m=tuple(index_levels),
        intermediate_levels_m=tuple(intermediate_levels),
        interval_basis=interval_basis,
    )


def _contour_key_text(levels: Sequence[float]) -> str:
    ordered = sorted(set(float(level) for level in levels))
    if len(ordered) >= 2:
        intervals = [
            round(ordered[index + 1] - ordered[index], 6)
            for index in range(len(ordered) - 1)
        ]
        if intervals and max(intervals) - min(intervals) <= 1e-6:
            return (
                f"CONTOURS / {ordered[0]:.0f}-{ordered[-1]:.0f} M / "
                f"{intervals[0]:.0f} M"
            )
    displayed = ordered
    if len(displayed) > 4:
        step = (len(displayed) - 1) / 3.0
        displayed = sorted({displayed[round(index * step)] for index in range(4)})
    return "CONTOURS / " + " / ".join(f"{level:.0f}" for level in displayed) + " M"


def _contour_key_placement(
    terrain: dict[str, Any],
    *,
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    forbidden_boxes: Sequence[Rect] = (),
) -> _ContourKeyPlacement | None:
    levels = tuple(
        sorted(
            {float(contour["elevation_m"]) for contour in terrain.get("contours", [])}
        )
    )
    if not levels:
        return None
    copy_text = _contour_key_text(levels)
    width = min(
        text_width_mm(plotter_copy(copy_text), cap_height_mm=2.0) + 1.4,
        map_rect.width - 2.0,
    )
    height = 3.2
    candidates = (
        Rect(map_rect.left + 1.0, map_rect.top + 1.0, width, height),
        Rect(map_rect.right - width - 1.0, map_rect.top + 1.0, width, height),
        Rect(map_rect.left + 1.0, map_rect.bottom - height - 1.0, width, height),
        Rect(
            map_rect.right - width - 1.0, map_rect.bottom - height - 1.0, width, height
        ),
        Rect(map_rect.centre[0] - width / 2.0, map_rect.top + 1.0, width, height),
        Rect(
            map_rect.centre[0] - width / 2.0,
            map_rect.bottom - height - 1.0,
            width,
            height,
        ),
    )
    route_geometry = unary_union(
        [LineString(line) for line in route_lines if len(line) >= 2]
    )
    ranked: list[tuple[float, int, Rect]] = []
    for index, candidate in enumerate(candidates):
        if any(
            _rectangles_overlap(candidate, item, gap=0.7) for item in forbidden_boxes
        ):
            continue
        geometry = box(
            candidate.left,
            candidate.top,
            candidate.right,
            candidate.bottom,
        )
        clearance = (
            float(route_geometry.distance(geometry))
            if not route_geometry.is_empty
            else math.inf
        )
        if clearance < 0.8:
            continue
        ranked.append((-clearance, index, candidate))
    if not ranked:
        return None
    return _ContourKeyPlacement(
        text=copy_text,
        levels_m=levels,
        box=min(ranked)[2],
    )


def _add_derived_terrain(
    layer: ArtworkLayer,
    *,
    index_layer: ArtworkLayer | None = None,
    terrain: dict[str, Any],
    map_rect: Rect,
    physical_point: Callable[[Sequence[float]], Point],
    exclusion: BaseGeometry,
    fall_line_exclusion: BaseGeometry,
    attributes: dict[str, str],
    label_layer: ArtworkLayer | None = None,
    variant_id: str | None = None,
    contour_key: _ContourKeyPlacement | None = None,
    protected_fall_line_boxes: Sequence[Rect] = (),
    label_obstacle_boxes: Sequence[Rect] = (),
    route_lines: Sequence[Sequence[Point]] = (),
    rendered_label_boxes: list[Rect] | None = None,
    rendered_leader_paths: list[list[Point]] | None = None,
    retain_all_contours: bool = False,
    detailed_level_limit: int = 6,
    relief_equivalent_density_target: float = 0.315,
) -> dict[str, Any]:
    """Render factual minor/index contours and frozen DEM-gradient fall lines."""

    source_ref = str(terrain["source_ref"])
    common = {
        **attributes,
        "data-relief-status": "source-derived-dtm",
        "data-derivation-id": str(terrain["derivation_id"]),
    }
    fall_line_sequence = 1_000
    clipped_fall_lines: list[tuple[dict[str, Any], int, list[Point]]] = []
    # The paired release uses continuous elevation contours as its terrain
    # grammar.  Frozen DEM fall lines remain valid source evidence for legacy
    # studies, but their short scratch-like marks are deliberately absent from
    # both the context-map and contour-relief editions.
    relief_strokes = terrain.get("relief_strokes", []) if variant_id is None else []
    for fall_line in relief_strokes:
        fall_line_sequence += 1
        for stroke in _page_line_strokes(
            fall_line["points"],
            physical_point=physical_point,
            map_rect=map_rect,
            exclusion=fall_line_exclusion,
        ):
            if polyline_length_mm(stroke) < 3.0:
                continue
            clipped_fall_lines.append((fall_line, fall_line_sequence, stroke))
    policy = terrain.get("relief_stroke_policy", {})
    retained_fall_line_indices = _runtime_fall_line_cluster_indices(
        [stroke for _, _, stroke in clipped_fall_lines],
        connectivity_distance_mm=float(
            policy.get("cluster_connectivity_distance_mm", 6.0)
        ),
        minimum_strokes=int(policy.get("minimum_cluster_strokes", 3)),
        minimum_total_mm=float(policy.get("minimum_cluster_total_mm", 9.0)),
    )
    protected_geometry: BaseGeometry = (
        unary_union(
            [
                box(item.left, item.top, item.right, item.bottom)
                for item in protected_fall_line_boxes
            ]
        )
        if protected_fall_line_boxes
        else Polygon()
    )
    protected_fall_line_indices = {
        index
        for index, (_fall_line, _sequence, stroke) in enumerate(clipped_fall_lines)
        if not protected_geometry.is_empty
        and protected_geometry.intersects(LineString(stroke))
    }
    for index, (fall_line, sequence, stroke) in enumerate(clipped_fall_lines):
        if index not in retained_fall_line_indices:
            continue
        layer.add(
            stroke,
            source_ref=source_ref,
            role="source-derived-dem-fall-line-hachure",
            sequence=sequence,
            attributes={
                **common,
                "data-fall-line-id": str(fall_line["id"]),
                "data-relief-algorithm-id": str(fall_line["algorithm_id"]),
                "data-geometry-sha256": str(fall_line["geometry_sha256"]),
                "data-seed-slope-deg": f"{float(fall_line['seed_slope_deg']):g}",
                "data-mean-slope-deg": f"{float(fall_line['mean_slope_deg']):g}",
                "data-seed-aspect-deg": f"{float(fall_line['seed_aspect_deg']):g}",
                "data-seed-elevation-m": f"{float(fall_line['seed_elevation_m']):g}",
                "data-derived-window-sha256": str(
                    fall_line["provenance"]["derived_window_sha256"]
                ),
                "data-derived-page-length-mm": f"{float(fall_line['page_length_mm']):g}",
                "data-runtime-minimum-mm": "3",
                "data-route-clearance-mm": "1.1",
                "data-label-clearance-mm": "0.5",
                "data-water-clearance-mm": "0.4",
                "data-runtime-cluster-policy": "6mm-min3-min9mm-v1",
                "data-runtime-terrain-reservation": (
                    "prelabel-dense-source-cell-v1"
                    if index in protected_fall_line_indices
                    else "none"
                ),
            },
        )
    source_fall_line_count = len(terrain.get("relief_strokes", []))
    retained_fall_line_count = len(retained_fall_line_indices)
    fall_line_omission_reason: str | None = None
    if source_fall_line_count == 0:
        fall_line_omission_reason = FALL_LINE_NO_SOURCE_OMISSION_REASON
    elif retained_fall_line_count == 0:
        fall_line_omission_reason = FALL_LINE_NO_CLUSTER_OMISSION_REASON
    fall_line_rendering = {
        "policy_id": FALL_LINE_RUNTIME_RENDERING_POLICY_ID,
        "source_stroke_count": source_fall_line_count,
        "clearance_eligible_path_count": len(clipped_fall_lines),
        "cluster_rejected_path_count": (
            len(clipped_fall_lines) - retained_fall_line_count
        ),
        "retained_path_count": retained_fall_line_count,
        "minimum_stroke_mm": 3.0,
        "cluster_connectivity_distance_mm": float(
            policy.get("cluster_connectivity_distance_mm", 6.0)
        ),
        "minimum_cluster_strokes": int(policy.get("minimum_cluster_strokes", 3)),
        "minimum_cluster_total_mm": float(policy.get("minimum_cluster_total_mm", 9.0)),
        "omission_reason": fall_line_omission_reason,
    }
    hachure_exclusion = (
        make_valid(
            set_precision(
                exclusion.buffer(0.2, cap_style="round", join_style="round"),
                grid_size=AREA_PRECISION_MM,
            )
        )
        if not exclusion.is_empty
        else exclusion
    )
    mask_sequence = 5_000
    masks = (
        []
        if relief_strokes or variant_id is not None
        else terrain.get("elevation_masks", [])
    )
    for mask in masks:
        # Legacy terrain.areas deliberately remain inert.  A source bundle has
        # to opt each new DEM threshold polygon into this disclosed treatment.
        if mask.get("render_as_hachure") is not True:
            continue
        mask_sequence += 1
        threshold_m = float(mask["minimum_elevation_m"])
        geometry_sha256 = str(mask["geometry_sha256"])
        rendering = mask["rendering"]
        minimum_stroke_mm = float(rendering["minimum_stroke_mm"])
        maximum_strokes = int(rendering["maximum_strokes_per_area"])
        mask_strokes: list[list[Point]] = []
        for part_index, polygon in enumerate(
            _page_polygon_parts(
                mask["outer"],
                holes=mask.get("holes", []),
                physical_point=physical_point,
                map_rect=map_rect,
            )
        ):
            for stroke in _bounded_short_hachure_strokes(
                polygon,
                row_spacing_mm=float(rendering["spacing_mm"]),
                along_pitch_mm=float(rendering["along_pitch_mm"]),
                segment_length_mm=float(rendering["nominal_segment_length_mm"]),
                angle_deg=float(rendering["angle_deg"]),
                inset_mm=float(rendering["inset_mm"]),
                exclusion=hachure_exclusion,
                phase=int(geometry_sha256[:8], 16) + part_index * 37,
                minimum_stroke_mm=minimum_stroke_mm,
                limit=maximum_strokes,
            ):
                mask_strokes.append(stroke)
        if len(mask_strokes) > maximum_strokes:
            stride = len(mask_strokes) / float(maximum_strokes)
            mask_strokes = [
                mask_strokes[
                    min(
                        int((index + 0.5) * stride),
                        len(mask_strokes) - 1,
                    )
                ]
                for index in range(maximum_strokes)
            ]
        if len(mask_strokes) < 3:
            continue
        for stroke in mask_strokes:
            layer.add(
                stroke,
                source_ref=source_ref,
                role="source-derived-elevation-mask-hachure",
                sequence=mask_sequence,
                attributes={
                    **common,
                    "data-mask-id": str(mask["id"]),
                    "data-elevation-threshold-m": f"{threshold_m:g}",
                    "data-mask-geometry-sha256": geometry_sha256,
                    "data-mask-geometry-policy": str(mask["geometry_policy"]),
                    "data-mask-rendering": str(rendering["treatment"]),
                    "data-perimeter-rendered": "false",
                    "data-minimum-component-strokes": "3",
                    "data-clearance-expansion-mm": "0.2",
                    "data-hachure-row-spacing-mm": str(rendering["spacing_mm"]),
                    "data-hachure-along-pitch-mm": str(rendering["along_pitch_mm"]),
                    "data-hachure-nominal-length-mm": str(
                        rendering["nominal_segment_length_mm"]
                    ),
                },
            )
    source_contours = list(terrain.get("contours", []))
    contours = source_contours

    def projected_contour_length(contour: dict[str, Any]) -> float:
        return sum(
            polyline_length_mm(stroke)
            for path in contour["paths"]
            for stroke in _page_line_strokes(
                path,
                physical_point=physical_point,
                map_rect=map_rect,
                exclusion=exclusion,
            )
            if polyline_length_mm(stroke) >= 2.5
        )

    contour_selection = {
        "policy_id": "all-source-levels-v1",
        "source_level_count": len(source_contours),
        "selected_level_count": len(source_contours),
        "selected_levels_m": [
            float(contour["elevation_m"]) for contour in source_contours
        ],
    }
    if variant_id == "detailed-map" and not retain_all_contours and len(contours) > 1:
        # A context map needs a coherent index-contour stack, not detached low
        # and high fragments.  Start with at most six evenly distributed
        # source levels, then step down (never below four when four exist) if
        # their page-projected ink alone would overpower an A5 context map.
        # This preserves whole factual contours and controls pen load without
        # inventing, shortening, or locally cherry-picking terrain fragments.
        maximum_level_count = min(
            len(source_contours),
            max(4, min(int(detailed_level_limit), 8)),
        )
        minimum_level_count = min(len(source_contours), 4)
        target_density = 0.145

        def selected_for_count(level_count: int) -> list[dict[str, Any]]:
            elevations = [
                float(contour["elevation_m"]) for contour in source_contours
            ]
            low = min(elevations)
            high = max(elevations)
            targets = [
                low + (high - low) * index / max(level_count - 1, 1)
                for index in range(level_count)
            ]
            available = set(range(len(source_contours)))
            selected_indices: set[int] = set()
            for target in targets:
                if not available:
                    break
                selected_index = min(
                    available,
                    key=lambda index: (
                        abs(elevations[index] - target),
                        elevations[index],
                        index,
                    ),
                )
                selected_indices.add(selected_index)
                available.remove(selected_index)
            return [
                contour
                for index, contour in enumerate(source_contours)
                if index in selected_indices
            ]

        selected_density = math.inf
        for level_count in range(
            maximum_level_count,
            minimum_level_count - 1,
            -1,
        ):
            density_candidate = selected_for_count(level_count)
            candidate_length = sum(
                projected_contour_length(contour) for contour in density_candidate
            )
            selected_density = candidate_length / max(
                map_rect.width * map_rect.height,
                1e-9,
            )
            contours = density_candidate
            if selected_density <= target_density:
                break
        selection_policy_id = "whole-level-page-density-cap-v1"
        if (
            selected_density > target_density
            and minimum_level_count >= 3
            and terrain.get("detailed_density_rebalance_policy")
            == "level-banded-minimum-ink-v1"
        ):
            # Exceptionally long continental contours can exceed the A5 ink
            # ceiling even at the four-level semantic minimum.  Preserve the
            # lowest/highest levels, divide the interior elevation inventory
            # into ordered bands, and retain the shortest complete source level
            # in each band.  No contour is clipped into decorative snippets.
            ordered_contours = sorted(
                source_contours,
                key=lambda contour: float(contour["elevation_m"]),
            )
            interior = ordered_contours[1:-1]
            slots = minimum_level_count - 2
            balanced = [ordered_contours[0], ordered_contours[-1]]
            for slot in range(slots):
                start = math.floor(slot * len(interior) / slots)
                finish = math.floor((slot + 1) * len(interior) / slots)
                band = interior[start:finish]
                if band:
                    balanced.append(
                        min(
                            band,
                            key=lambda contour: (
                                projected_contour_length(contour),
                                float(contour["elevation_m"]),
                            ),
                        )
                    )
            selected_ids = {id(contour) for contour in balanced}
            contours = [
                contour
                for contour in source_contours
                if id(contour) in selected_ids
            ]
            selected_density = sum(
                projected_contour_length(contour) for contour in contours
            ) / max(map_rect.width * map_rect.height, 1e-9)
            selection_policy_id = (
                "whole-level-page-density-cap-level-banded-minimum-ink-v1"
            )
        contour_selection = {
            "policy_id": selection_policy_id,
            "source_level_count": len(source_contours),
            "selected_level_count": len(contours),
            "selected_levels_m": [
                float(contour["elevation_m"]) for contour in contours
            ],
            "predicted_terrain_density_mm_per_mm2": round(selected_density, 4),
            "target_terrain_density_mm_per_mm2": target_density,
            "minimum_level_count": minimum_level_count,
            "maximum_level_count": maximum_level_count,
        }
    elif variant_id == "terrain-relief" and len(source_contours) > 1:
        # A heavier index pen changes physical ink even though SVG centreline
        # length is unchanged.  Budget against Grey-0.25-equivalent length so
        # compact alpine plates do not become denser merely because their true
        # fifth-interval levels moved to Grey 0.40.  Every retained level stays
        # complete; factual index levels are mandatory and the optional minor
        # inventory is sampled evenly over elevation when a cap is necessary.
        source_hierarchy = _contour_hierarchy(
            terrain,
            source_contours,
            source_contours,
        )
        mandatory_levels = set(source_hierarchy.index_levels_m)
        width_ratio = (
            PENS_BY_ID[CONTOUR_INDEX_PEN_ID].mark_width_mm
            / PENS_BY_ID[CONTOUR_MINOR_PEN_ID].mark_width_mm
        )
        target_equivalent_density = max(
            0.10,
            min(float(relief_equivalent_density_target), 0.315),
        )

        def equivalent_length(items: Sequence[dict[str, Any]]) -> float:
            return sum(
                projected_contour_length(contour)
                * (
                    width_ratio
                    if float(contour["elevation_m"]) in mandatory_levels
                    else 1.0
                )
                for contour in items
            )

        def relief_selected_for_count(level_count: int) -> list[dict[str, Any]]:
            mandatory = [
                contour
                for contour in source_contours
                if float(contour["elevation_m"]) in mandatory_levels
            ]
            optional = [
                contour
                for contour in source_contours
                if float(contour["elevation_m"]) not in mandatory_levels
            ]
            slots = max(0, min(level_count - len(mandatory), len(optional)))
            if slots >= len(optional):
                selected_optional = optional
            elif slots == 1:
                selected_optional = [optional[len(optional) // 2]]
            elif slots > 1:
                step = (len(optional) - 1) / float(slots - 1)
                selected_optional = [
                    optional[round(index * step)] for index in range(slots)
                ]
            else:
                selected_optional = []
            selected_ids = {id(contour) for contour in (*mandatory, *selected_optional)}
            return [
                contour
                for contour in source_contours
                if id(contour) in selected_ids
            ]

        minimum_level_count = min(
            len(source_contours),
            max(4, len(mandatory_levels)),
        )
        selected_equivalent_density = equivalent_length(contours) / max(
            map_rect.width * map_rect.height,
            1e-9,
        )
        if selected_equivalent_density > target_equivalent_density:
            for level_count in range(
                len(source_contours) - 1,
                minimum_level_count - 1,
                -1,
            ):
                candidate = relief_selected_for_count(level_count)
                candidate_density = equivalent_length(candidate) / max(
                    map_rect.width * map_rect.height,
                    1e-9,
                )
                contours = candidate
                selected_equivalent_density = candidate_density
                if candidate_density <= target_equivalent_density:
                    break
        contour_selection = {
            "policy_id": (
                "whole-level-index-preserving-physical-density-cap-v1"
                if len(contours) < len(source_contours)
                else "all-source-levels-physical-density-pass-v1"
            ),
            "source_level_count": len(source_contours),
            "selected_level_count": len(contours),
            "selected_levels_m": [
                float(contour["elevation_m"]) for contour in contours
            ],
            "mandatory_index_levels_m": sorted(mandatory_levels),
            "predicted_physical_equivalent_terrain_density_mm_per_mm2": round(
                selected_equivalent_density,
                4,
            ),
            "target_physical_equivalent_terrain_density_mm_per_mm2": (
                target_equivalent_density
            ),
            "equivalent_width_reference_mm": (
                PENS_BY_ID[CONTOUR_MINOR_PEN_ID].mark_width_mm
            ),
            "index_width_ratio": width_ratio,
            "minimum_level_count": minimum_level_count,
        }
    elif variant_id == "detailed-map" and retain_all_contours:
        contour_selection["policy_id"] = "all-source-levels-density-floor-retry-v1"

    # Selection operates on frozen source levels, but pre-existing context-copy
    # reservations can consume every plotter-safe fragment of a very short
    # level.  Classify only levels that still have at least one >=2.5 mm page
    # stroke; otherwise hierarchy metadata could advertise a Grey 0.40 load
    # that the physical SVG does not contain (notably PCT 2000 m).  Preserve the
    # pre-clearance inventory explicitly rather than pretending it was drawn.
    pre_clearance_contours = list(contours)
    contours = [
        contour
        for contour in pre_clearance_contours
        if projected_contour_length(contour) > 0.0
    ]
    if len(contours) != len(pre_clearance_contours):
        pre_clearance_levels = [
            float(contour["elevation_m"]) for contour in pre_clearance_contours
        ]
        rendered_levels = [float(contour["elevation_m"]) for contour in contours]
        contour_selection["pre_clearance_selected_levels_m"] = pre_clearance_levels
        contour_selection["pre_clearance_selected_level_count"] = len(
            pre_clearance_levels
        )
        contour_selection["omitted_after_context_copy_clearance_levels_m"] = sorted(
            set(pre_clearance_levels) - set(rendered_levels)
        )
        contour_selection["selected_levels_m"] = rendered_levels
        contour_selection["selected_level_count"] = len(rendered_levels)
        mandatory_levels = contour_selection.get("mandatory_index_levels_m")
        if isinstance(mandatory_levels, list):
            contour_selection["pre_clearance_mandatory_index_levels_m"] = list(
                mandatory_levels
            )
            contour_selection["mandatory_index_levels_m"] = [
                float(level)
                for level in mandatory_levels
                if float(level) in set(rendered_levels)
            ]

    hierarchy = _contour_hierarchy(terrain, source_contours, contours)
    minor_pen_id = layer.pen_id
    index_pen_id = index_layer.pen_id if index_layer is not None else layer.pen_id
    minor_pen_width_mm = (
        PENS_BY_ID[minor_pen_id].mark_width_mm
        if minor_pen_id in PENS_BY_ID
        else PENS_BY_ID[CONTOUR_MINOR_PEN_ID].mark_width_mm
    )
    index_pen_width_mm = (
        minor_pen_width_mm
        if index_pen_id == minor_pen_id
        else PENS_BY_ID[index_pen_id].mark_width_mm
        if index_pen_id in PENS_BY_ID
        else PENS_BY_ID[CONTOUR_INDEX_PEN_ID].mark_width_mm
    )
    index_width_ratio = index_pen_width_mm / minor_pen_width_mm
    physical_equivalent_density = sum(
        projected_contour_length(contour)
        * (
            index_width_ratio
            if hierarchy.classification(float(contour["elevation_m"])) == "index"
            else 1.0
        )
        for contour in contours
    ) / max(map_rect.width * map_rect.height, 1e-9)
    contour_selection[
        "predicted_physical_equivalent_terrain_density_mm_per_mm2"
    ] = round(physical_equivalent_density, 4)
    contour_selection["equivalent_width_reference_mm"] = (
        minor_pen_width_mm
    )
    contour_selection["index_width_ratio"] = index_width_ratio
    hierarchy_metadata = hierarchy.as_dict(
        minor_pen_id=minor_pen_id,
        index_pen_id=index_pen_id,
        minor_pen_width_mm=minor_pen_width_mm,
        index_pen_width_mm=index_pen_width_mm,
    )
    fall_line_rendering["contour_selection"] = contour_selection
    fall_line_rendering["contour_hierarchy"] = hierarchy_metadata
    common["data-contour-selection-policy"] = str(
        contour_selection["policy_id"]
    )
    common["data-source-contour-level-count"] = str(len(source_contours))
    common["data-selected-contour-level-count"] = str(len(contours))
    common["data-contour-hierarchy-policy"] = CONTOUR_HIERARCHY_POLICY_ID
    common["data-contour-minor-interval-m"] = (
        "none"
        if hierarchy.minor_interval_m is None
        else f"{hierarchy.minor_interval_m:g}"
    )
    common["data-contour-index-interval-m"] = (
        "none"
        if hierarchy.index_interval_m is None
        else f"{hierarchy.index_interval_m:g}"
    )
    common["data-bathymetry-status"] = "not-rendered-no-qualified-source"

    contour_sequence = 10_000
    contour_strokes: list[
        tuple[dict[str, Any], int, float, str, list[Point]]
    ] = []
    for contour in contours:
        elevation = float(contour["elevation_m"])
        contour_class = hierarchy.classification(elevation)
        for path in contour["paths"]:
            contour_sequence += 1
            for stroke in _page_line_strokes(
                path,
                physical_point=physical_point,
                map_rect=map_rect,
                exclusion=exclusion,
            ):
                if polyline_length_mm(stroke) < 2.5:
                    continue
                contour_strokes.append(
                    (contour, contour_sequence, elevation, contour_class, stroke)
                )

    label_boxes: list[Rect] = []
    pending_contour_labels: list[
        tuple[float, str, str, str, Rect, Point, float, float]
    ] = []
    if variant_id == "terrain-relief" and label_layer is not None and contour_strokes:
        # Contour copy is informational furniture, not geography.  It must
        # never sit on top of the red itinerary; moving/clipping the route to
        # accommodate a label would falsify the artwork.  Leaders already
        # avoid the hero route, and the label box now follows the same rule.
        route_copy_exclusion: BaseGeometry = (
            unary_union(
                [LineString(line) for line in route_lines if len(line) >= 2]
            ).buffer(
                CONTOUR_LABEL_HERO_ROUTE_CLEARANCE_MM,
                cap_style="round",
                join_style="round",
            )
            if route_lines
            else Polygon()
        )
        by_elevation: dict[
            float, list[tuple[dict[str, Any], int, float, str, list[Point]]]
        ] = {}
        for item in contour_strokes:
            by_elevation.setdefault(item[2], []).append(item)
        route_geography: BaseGeometry = (
            unary_union(
                [LineString(line) for line in route_lines if len(line) >= 2]
            )
            if route_lines
            else Polygon()
        )
        all_levels = sorted(by_elevation)
        route_bearing_levels = [
            elevation
            for elevation in all_levels
            if not route_geography.is_empty
            and any(
                LineString(item[4]).length >= 3.5
                and LineString(item[4]).distance(route_geography)
                <= CONTOUR_LABEL_ROUTE_LANDMASS_PRIORITY_MM
                for item in by_elevation[elevation]
            )
        ]
        index_levels = [
            elevation
            for elevation in all_levels
            if hierarchy.classification(elevation) == "index"
        ]
        route_bearing_index_levels = [
            elevation for elevation in route_bearing_levels if elevation in index_levels
        ]
        levels = list(route_bearing_index_levels or index_levels)
        if len(levels) > 4:
            step = (len(levels) - 1) / 3.0
            levels = sorted({levels[round(index * step)] for index in range(4)})
        else:
            levels = sorted(set(levels))
        contour_level_selection_policy = (
            "route-bearing-index-levels-first-v2"
            if route_bearing_index_levels
            else "all-factual-index-levels-fallback-v2"
            if index_levels
            else "no-factual-index-level-available-v2"
        )
        for elevation in levels:
            copy_text = f"{elevation:.0f} M"
            width = text_width_mm(copy_text, cap_height_mm=2.0) + 1.2
            candidate: Rect | None = None
            candidate_anchor: Point | None = None
            selected_contour: dict[str, Any] | None = None
            selected_route_distance_mm = math.inf
            selected_page_length_mm = 0.0

            def component_score(
                item: tuple[dict[str, Any], int, float, str, list[Point]],
            ) -> tuple[float, float, float, int, str]:
                contour, sequence, _, _, stroke = item
                line = LineString(stroke)
                length = float(line.length)
                if route_geography.is_empty:
                    return (
                        0.0,
                        0.0,
                        -length,
                        sequence,
                        str(contour.get("id", "")),
                    )
                route_distance = float(line.distance(route_geography))
                return (
                    0.0
                    if route_distance <= CONTOUR_LABEL_ROUTE_LANDMASS_PRIORITY_MM
                    else 1.0,
                    route_distance,
                    -length,
                    sequence,
                    str(contour.get("id", "")),
                )

            # The longest contour component is often a remote island or an
            # adjacent mountain block.  Prefer a component on, or close to,
            # the itinerary-bearing landmass.  If copy cannot fit there, try
            # the remaining factual components in the same deterministic
            # order; no contour geometry is removed or moved.
            ordered_components = sorted(
                by_elevation[elevation], key=component_score
            )
            # Only enforce the near-route component subset when this exact
            # index level qualified for the route-bearing preference.  When
            # no index level reaches the itinerary, ``levels`` deliberately
            # falls back to factual remote indices; filtering those against
            # unrelated route-bearing minor levels would empty the candidate
            # inventory and suppress every honest altitude label.
            if elevation in route_bearing_index_levels:
                ordered_components = [
                    item
                    for item in ordered_components
                    if LineString(item[4]).length >= 3.5
                    and LineString(item[4]).distance(route_geography)
                    <= CONTOUR_LABEL_ROUTE_LANDMASS_PRIORITY_MM
                ]
            for contour, _, _, _, stroke in ordered_components:
                line = LineString(stroke)
                if line.length < 3.5:
                    continue
                route_distance_mm = (
                    float(line.distance(route_geography))
                    if not route_geography.is_empty
                    else math.inf
                )
                # Exhaust positions directly on the selected component before
                # considering displaced copy.  This keeps altitude text tied
                # to its factual contour whenever the copy plan permits it.
                for offset_x, offset_y in (
                    (0.0, 0.0),
                    (0.0, -3.2),
                    (0.0, 3.2),
                    (4.2, 0.0),
                    (-4.2, 0.0),
                    (6.4, 0.0),
                    (-6.4, 0.0),
                    (6.4, -3.2),
                    (-6.4, -3.2),
                    (6.4, 3.2),
                    (-6.4, 3.2),
                    (10.4, 0.0),
                    (-10.4, 0.0),
                    (14.4, 0.0),
                    (-14.4, 0.0),
                    (10.4, -4.8),
                    (-10.4, -4.8),
                    (10.4, 4.8),
                    (-10.4, 4.8),
                    (20.0, 0.0),
                    (-20.0, 0.0),
                    (26.0, 0.0),
                    (-26.0, 0.0),
                    (20.0, -6.4),
                    (-20.0, -6.4),
                    (20.0, 6.4),
                    (-20.0, 6.4),
                    # Long linear routes can leave their contour stack on one
                    # side of a label-dense itinerary while preserving a
                    # generous clear field above or below it.  Exhaust those
                    # vertical positions before giving up the only factual
                    # index label.  These remain copy displacements only: the
                    # contour anchor and source geometry are unchanged, and
                    # the later leader router still avoids the hero route and
                    # all foreign copy.
                    (0.0, -9.6),
                    (0.0, 9.6),
                    (0.0, -12.8),
                    (0.0, 12.8),
                    (0.0, -16.0),
                    (0.0, 16.0),
                    (8.0, -9.6),
                    (-8.0, -9.6),
                    (8.0, 9.6),
                    (-8.0, 9.6),
                    (14.0, -12.8),
                    (-14.0, -12.8),
                    (14.0, 12.8),
                    (-14.0, 12.8),
                    (22.0, -16.0),
                    (-22.0, -16.0),
                    (22.0, 16.0),
                    (-22.0, 16.0),
                ):
                    if (
                        offset_x == 0.0
                        and offset_y == 0.0
                        and float(line.length) < width + 5.0
                    ):
                        # Inline copy would consume this entire short index
                        # component after the two 2.5 mm plotter-fragment
                        # floors.  Prefer displaced, leader-addressable copy so
                        # the labelled factual contour remains visible.
                        continue
                    for fraction in (
                        0.5,
                        0.32,
                        0.68,
                        0.18,
                        0.82,
                        0.08,
                        0.92,
                        0.04,
                        0.96,
                    ):
                        point = line.interpolate(fraction, normalized=True)
                        proposed = Rect(
                            float(point.x) + offset_x - width / 2.0,
                            float(point.y) + offset_y - 1.4,
                            width,
                            2.8,
                        )
                        if (
                            proposed.left < map_rect.left + 0.5
                            or proposed.right > map_rect.right - 0.5
                            or proposed.top < map_rect.top + 0.5
                            or proposed.bottom > map_rect.bottom - 0.5
                        ):
                            continue
                        proposed_geometry = box(
                            proposed.left,
                            proposed.top,
                            proposed.right,
                            proposed.bottom,
                        )
                        if (
                            (
                                not exclusion.is_empty
                                and exclusion.intersects(proposed_geometry)
                            )
                            or (
                                not route_copy_exclusion.is_empty
                                and route_copy_exclusion.intersects(
                                    proposed_geometry
                                )
                            )
                            or any(
                                _rectangles_overlap(
                                    proposed,
                                    existing,
                                    gap=CONTOUR_LABEL_FOREIGN_COPY_CLEARANCE_MM,
                                )
                                for existing in label_obstacle_boxes
                            )
                            or any(
                                _rectangles_overlap(proposed, existing, gap=2.0)
                                for existing in label_boxes
                            )
                        ):
                            continue
                        candidate = proposed
                        candidate_anchor = (float(point.x), float(point.y))
                        selected_contour = contour
                        selected_route_distance_mm = route_distance_mm
                        selected_page_length_mm = float(line.length)
                        break
                    if candidate is not None:
                        break
                if candidate is not None:
                    break
            if candidate is None:
                continue
            label_boxes.append(candidate)
            if candidate_anchor is None or selected_contour is None:
                continue
            contour_id = str(
                selected_contour.get("id", f"contour-{elevation:g}")
            )
            pending_contour_labels.append(
                (
                    elevation,
                    contour_id,
                    hierarchy.classification(elevation),
                    copy_text,
                    candidate,
                    candidate_anchor,
                    selected_route_distance_mm,
                    selected_page_length_mm,
                )
            )

        completed_contour_leaders: list[list[Point]] = []
        for (
            elevation,
            contour_id,
            contour_class,
            copy_text,
            candidate,
            candidate_anchor,
            selected_route_distance_mm,
            selected_page_length_mm,
        ) in pending_contour_labels:
            label_identity = f"contour-altitude-{contour_id}"
            foreign_boxes = [item for item in label_boxes if item is not candidate]
            foreign_boxes.extend(label_obstacle_boxes)
            leader = _safe_label_leader(
                candidate_anchor,
                candidate,
                map_rect=map_rect,
                obstacle_boxes=foreign_boxes,
                route_lines=route_lines,
                existing_leaders=completed_contour_leaders,
            )
            label_attributes = {
                **common,
                "data-label-id": label_identity,
                "data-label-box": (
                    f"{candidate.x:.3f},{candidate.y:.3f},"
                    f"{candidate.width:.3f},{candidate.height:.3f}"
                ),
                "data-contour-id": contour_id,
                "data-elevation-m": f"{elevation:g}",
                "data-contour-class": contour_class,
                "data-minimum-foreign-copy-clearance-mm": (
                    f"{CONTOUR_LABEL_FOREIGN_COPY_CLEARANCE_MM:g}"
                ),
                "data-hero-route-clearance-mm": (
                    f"{CONTOUR_LABEL_HERO_ROUTE_CLEARANCE_MM:g}"
                ),
                "data-contour-component-selection-policy": (
                    "route-bearing-landmass-near-route-first-v1"
                ),
                "data-contour-level-selection-policy": (
                    contour_level_selection_policy
                ),
                "data-contour-route-distance-mm": (
                    "none"
                    if not math.isfinite(selected_route_distance_mm)
                    else f"{selected_route_distance_mm:.3f}"
                ),
                "data-contour-page-length-mm": f"{selected_page_length_mm:.3f}",
                "data-contour-route-priority-threshold-mm": (
                    f"{CONTOUR_LABEL_ROUTE_LANDMASS_PRIORITY_MM:g}"
                ),
            }
            if leader is not None:
                completed_contour_leaders.append(leader)
                if rendered_leader_paths is not None:
                    rendered_leader_paths.append(leader)
                label_layer.add(
                    leader,
                    source_ref=source_ref,
                    role="source-derived-contour-altitude-leader",
                    attributes={
                        **common,
                        "data-feature-id": label_identity,
                        "data-contour-id": contour_id,
                        "data-elevation-m": f"{elevation:g}",
                        "data-contour-class": contour_class,
                        "data-leader-policy": (
                            "foreign-copy-route-and-leader-clearance-v1"
                        ),
                        "data-leader-routing-policy": (
                            "foreign-copy-route-and-leader-clearance-v1"
                        ),
                        "data-minimum-copy-clearance-mm": "0.3",
                        "data-hero-route-clearance-mm": (
                            f"{LEADER_HERO_ROUTE_CLEARANCE_MM:g}"
                        ),
                    },
                )
            add_text(
                label_layer,
                copy_text,
                x_mm=candidate.centre[0],
                y_mm=candidate.y + 0.4,
                preferred_cap_mm=2.0,
                minimum_cap_mm=2.0,
                maximum_width_mm=max(candidate.width - 0.6, 2.0),
                anchor="middle",
                source_ref=source_ref,
                role="source-derived-contour-altitude-label",
                attributes=label_attributes,
            )

    if variant_id == "terrain-relief" and label_layer is not None and contour_key:
        label_boxes.append(contour_key.box)
        contour_key_identity = "contour-altitude-key"
        add_text(
            label_layer,
            contour_key.text,
            x_mm=contour_key.box.centre[0],
            y_mm=contour_key.box.y + 0.6,
            preferred_cap_mm=2.0,
            minimum_cap_mm=2.0,
            maximum_width_mm=max(contour_key.box.width - 0.8, 2.0),
            anchor="middle",
            source_ref=source_ref,
            role="source-derived-contour-altitude-key",
            attributes={
                **common,
                "data-label-id": contour_key_identity,
                "data-label-box": (
                    f"{contour_key.box.x:.3f},{contour_key.box.y:.3f},"
                    f"{contour_key.box.width:.3f},{contour_key.box.height:.3f}"
                ),
                "data-contour-levels-m": ",".join(
                    f"{level:g}" for level in contour_key.levels_m
                ),
                "data-orientation-policy": "north-up",
            },
        )

    if rendered_label_boxes is not None:
        rendered_label_boxes.extend(label_boxes)

    contour_label_exclusion: BaseGeometry = (
        unary_union(
            [
                box(item.left, item.top, item.right, item.bottom).buffer(0.18)
                for item in label_boxes
            ]
        )
        if label_boxes
        else Polygon()
    )
    for contour, sequence, elevation, contour_class, stroke in contour_strokes:
        geometry: BaseGeometry = LineString(stroke)
        if not contour_label_exclusion.is_empty:
            geometry = geometry.difference(contour_label_exclusion)
        contour_id = str(contour.get("id", f"contour-{elevation:g}"))
        for part in _geometry_lines(geometry):
            points = _line_points(part)
            if polyline_length_mm(points) < 2.5:
                continue
            target_layer = (
                index_layer
                if contour_class == "index" and index_layer is not None
                else layer
            )
            target_pen_width_mm = (
                index_pen_width_mm
                if target_layer is index_layer and index_layer is not None
                else minor_pen_width_mm
            )
            target_layer.add(
                points,
                source_ref=source_ref,
                role="source-derived-dtm-contour",
                sequence=sequence,
                attributes={
                    **common,
                    "data-contour-id": contour_id,
                    "data-elevation-m": f"{elevation:g}",
                    "data-contour-class": contour_class,
                    "data-contour-pen-width-mm": (
                        f"{target_pen_width_mm:g}"
                    ),
                    "data-contour-index-fallback": "false",
                },
            )
    return fall_line_rendering


def _reconcile_rendered_contour_inventory(
    artwork: PlateArtwork,
    *,
    map_rect: Rect,
    minor_layer: ArtworkLayer,
    index_layer: ArtworkLayer,
    label_layer: ArtworkLayer,
) -> None:
    """Make final hierarchy metadata describe emitted, post-mask geometry.

    Copy-legibility masks deliberately remove exact source substrings and can
    occasionally consume the last plotter-safe component of one elevation.
    The final manifest must describe what reaches paper, while retaining the
    earlier selected inventory as an explicit omission ledger.
    """

    def contour_levels(layer: ArtworkLayer) -> set[float]:
        return {
            float(record.attributes["data-elevation-m"])
            for record in layer.records
            if record.role == "source-derived-dtm-contour"
            and "data-elevation-m" in record.attributes
        }

    minor_levels = contour_levels(minor_layer)
    index_levels = contour_levels(index_layer)
    rendered_levels = sorted(minor_levels | index_levels)
    rendered_index_levels = sorted(index_levels)

    # A label for a level whose final contour vanished would be an unsupported
    # claim.  This normally stays a no-op because placement protects short
    # components, but fail closed after every subsequent geography mask too.
    label_roles = {
        "source-derived-contour-altitude-label",
        "source-derived-contour-altitude-leader",
    }
    label_layer.records[:] = [
        record
        for record in label_layer.records
        if record.role not in label_roles
        or float(record.attributes.get("data-elevation-m", "nan")) in index_levels
    ]

    hierarchy = artwork.rendering_metadata.get("terrain_contour_hierarchy")
    if isinstance(hierarchy, dict):
        prior_index_levels = [
            float(level) for level in hierarchy.get("index_levels_m", [])
        ]
        prior_intermediate_levels = [
            float(level) for level in hierarchy.get("intermediate_levels_m", [])
        ]
        if prior_index_levels != rendered_index_levels:
            hierarchy["pre_mask_index_levels_m"] = prior_index_levels
            hierarchy["omitted_index_levels_after_legibility_masks_m"] = sorted(
                set(prior_index_levels) - index_levels
            )
        hierarchy["index_levels_m"] = rendered_index_levels
        hierarchy["rendered_minor_levels_m"] = sorted(minor_levels)
        hierarchy["intermediate_levels_m"] = sorted(
            set(prior_intermediate_levels) & set(rendered_levels)
        )

    selection = artwork.rendering_metadata.get("terrain_contour_selection")
    if isinstance(selection, dict):
        prior_levels = [float(level) for level in selection.get("selected_levels_m", [])]
        if prior_levels != rendered_levels:
            selection["pre_mask_selected_levels_m"] = prior_levels
            selection["pre_mask_selected_level_count"] = len(prior_levels)
            selection["omitted_after_legibility_masks_levels_m"] = sorted(
                set(prior_levels) - set(rendered_levels)
            )
        selection["selected_levels_m"] = rendered_levels
        selection["selected_level_count"] = len(rendered_levels)
        area_mm2 = max(map_rect.width * map_rect.height, 1e-9)
        minor_mm = sum(
            polyline_length_mm(record.points)
            for record in minor_layer.records
            if record.role == "source-derived-dtm-contour"
        )
        index_mm = sum(
            polyline_length_mm(record.points)
            for record in index_layer.records
            if record.role == "source-derived-dtm-contour"
        )
        raw_density = (minor_mm + index_mm) / area_mm2
        equivalent_density = (
            minor_mm
            + index_mm
            * PENS_BY_ID[CONTOUR_INDEX_PEN_ID].mark_width_mm
            / PENS_BY_ID[CONTOUR_MINOR_PEN_ID].mark_width_mm
        ) / area_mm2
        if "predicted_terrain_density_mm_per_mm2" in selection:
            selection["pre_mask_predicted_terrain_density_mm_per_mm2"] = selection[
                "predicted_terrain_density_mm_per_mm2"
            ]
            selection["predicted_terrain_density_mm_per_mm2"] = round(
                raw_density, 4
            )
        if (
            "predicted_physical_equivalent_terrain_density_mm_per_mm2"
            in selection
        ):
            selection[
                "pre_mask_predicted_physical_equivalent_terrain_density_mm_per_mm2"
            ] = selection[
                "predicted_physical_equivalent_terrain_density_mm_per_mm2"
            ]
        selection[
            "predicted_physical_equivalent_terrain_density_mm_per_mm2"
        ] = round(equivalent_density, 4)
        selection["final_rendered_terrain_density_mm_per_mm2"] = round(
            raw_density, 4
        )

    selected_count = str(len(rendered_levels))
    for layer in (minor_layer, index_layer, label_layer):
        for record in layer.records:
            if record.role in {
                "source-derived-dtm-contour",
                "source-derived-contour-altitude-label",
                "source-derived-contour-altitude-leader",
            }:
                record.attributes["data-selected-contour-level-count"] = selected_count

    terrain_summary = artwork.rendering_metadata.get("terrain_fall_lines")
    if isinstance(terrain_summary, dict):
        if isinstance(hierarchy, dict):
            terrain_summary["contour_hierarchy"] = copy.deepcopy(hierarchy)
        if isinstance(selection, dict):
            terrain_summary["contour_selection"] = copy.deepcopy(selection)


def _add_derived_landcover(
    layer: ArtworkLayer,
    *,
    landcover: dict[str, Any],
    map_rect: Rect,
    physical_point: Callable[[Sequence[float]], Point],
    area_exclusion: BaseGeometry,
    boundary_exclusion: BaseGeometry,
    attributes: dict[str, str],
    seed: int,
) -> None:
    source_ref = str(landcover["source_ref"])
    generic_retention = (
        str(landcover.get("derivation_id", "")) == "osm-pbf-generic-forest-woodland-v1"
    )
    maximum_symbol_distance_m = (
        MAX_GENERIC_SYMBOLIC_WOODLAND_ROUTE_DISTANCE_M
        if generic_retention
        else MAX_SYMBOLIC_WOODLAND_ROUTE_DISTANCE_M
    )
    symbol_offsets = (
        _GENERIC_WOODLAND_RETENTION_OFFSETS
        if generic_retention
        else _WOODLAND_SYMBOL_OFFSETS
    )
    symbolic_objects: set[str] = set()
    for feature_index, feature in enumerate(landcover["features"], start=1):
        semantic_class = str(feature["class"])
        source_object = str(feature["source_object"])
        feature_attributes = {
            **attributes,
            "data-landcover-id": str(feature["id"]),
            "data-source-object": source_object,
            "data-osm-element": source_object,
            "data-source-url": (
                f"https://www.openstreetmap.org/{feature['source_object']}"
            ),
            "data-source-area-m2": f"{float(feature['area_m2']):.1f}",
        }
        for polygon in _page_polygon_parts(
            feature["outer"],
            holes=feature.get("holes", []),
            physical_point=physical_point,
            map_rect=map_rect,
            simplify_mm=0.22,
        ):
            source_physical_area = float(
                feature.get("source_object_physical_area_mm2", polygon.area)
            )
            if source_physical_area < 2.2 or polygon.area < 2.2:
                if (
                    semantic_class in {"forest", "woodland"}
                    and source_object not in symbolic_objects
                    and float(feature.get("distance_to_route_m", math.inf))
                    <= maximum_symbol_distance_m
                    and _add_source_anchored_woodland_symbol(
                        layer,
                        polygon,
                        map_rect=map_rect,
                        exclusion=area_exclusion,
                        source_ref=source_ref,
                        sequence=feature_index,
                        attributes=feature_attributes,
                        semantic_class=semantic_class,
                        offsets=symbol_offsets,
                    )
                ):
                    symbolic_objects.add(source_object)
                continue
            emitted = _add_green_polygon(
                layer,
                polygon,
                source_ref=source_ref,
                sequence=feature_index,
                attributes=feature_attributes,
                seed=seed,
                semantic_class=semantic_class,
                exclusion=area_exclusion,
                woodland_symbols=semantic_class in {"forest", "woodland"},
                boundary_exclusion=boundary_exclusion,
            )
            if (
                emitted == 0
                and semantic_class in {"forest", "woodland"}
                and source_object not in symbolic_objects
                and float(feature.get("distance_to_route_m", math.inf))
                <= maximum_symbol_distance_m
                and _add_source_anchored_woodland_symbol(
                    layer,
                    polygon,
                    map_rect=map_rect,
                    exclusion=area_exclusion,
                    source_ref=source_ref,
                    sequence=feature_index,
                    attributes=feature_attributes,
                    semantic_class=semantic_class,
                    offsets=symbol_offsets,
                )
            ):
                symbolic_objects.add(source_object)


def _osm_feature_attributes(
    *,
    attributes: dict[str, str],
    feature_id: str,
    source_object: str,
    source_objects: Sequence[str] | None = None,
) -> dict[str, str]:
    result = {
        **attributes,
        "data-feature-id": feature_id,
        "data-source-object": source_object,
        "data-osm-element": source_object,
        "data-source-url": f"https://www.openstreetmap.org/{source_object}",
    }
    if source_objects is not None:
        result["data-osm-elements"] = ",".join(source_objects)
        result["data-source-object-count"] = str(len(source_objects))
    return result


def _add_derived_water(
    layer: ArtworkLayer,
    *,
    water: dict[str, Any],
    map_rect: Rect,
    physical_point: Callable[[Sequence[float]], Point],
    exclusion: BaseGeometry,
    attributes: dict[str, str],
) -> None:
    """Render one truthful shoreline or centreline per selected water feature."""

    source_ref = str(water["source_ref"])
    derivation = str(water["derivation_id"])
    sequence = 0
    for area in water["areas"]:
        sequence += 1
        area_attributes = _osm_feature_attributes(
            attributes=attributes,
            feature_id=str(area["id"]),
            source_object=str(area["source_object"]),
        )
        area_attributes.update(
            {
                "data-water-class": str(area["class"]),
                "data-derivation-id": derivation,
                "data-source-area-m2": f"{float(area['area_m2']):.1f}",
                "data-area-rendering": "single-source-shoreline-v4",
            }
        )
        for polygon in _page_polygon_parts(
            area["outer"],
            holes=area.get("holes", []),
            physical_point=physical_point,
            map_rect=map_rect,
            simplify_mm=0.1,
        ):
            for boundary in _masked_boundary_strokes(polygon, exclusion=exclusion):
                layer.add(
                    boundary,
                    source_ref=source_ref,
                    role="source-sampled-lake-boundary",
                    sequence=sequence,
                    attributes=area_attributes,
                )
    for coastline in water["coastlines"]:
        sequence += 1
        coast_attributes = _osm_feature_attributes(
            attributes=attributes,
            feature_id=str(coastline["id"]),
            source_object=str(coastline["source_object"]),
            source_objects=[str(item) for item in coastline["source_objects"]],
        )
        coast_attributes.update(
            {
                "data-water-class": "marine",
                "data-derivation-id": derivation,
                "data-source-length-m": f"{float(coastline['length_m']):.1f}",
                "data-area-rendering": "continuous-source-coastline-v4",
                "data-closed-chain": str(bool(coastline.get("closed_chain"))).lower(),
            }
        )
        for path in coastline["paths"]:
            if (
                coastline.get("closed_chain") is True
                and _closed_source_path(path)
                and _closed_path_page_area_mm2(path, physical_point)
                < MIN_CLOSED_COAST_AREA_MM2
            ):
                continue
            for stroke in _page_line_strokes(
                path,
                physical_point=physical_point,
                map_rect=map_rect,
                exclusion=exclusion,
            ):
                layer.add(
                    stroke,
                    source_ref=source_ref,
                    role="source-sampled-coastline",
                    sequence=sequence,
                    attributes=coast_attributes,
                )
    for river in water["rivers"]:
        sequence += 1
        river_attributes = _osm_feature_attributes(
            attributes=attributes,
            feature_id=str(river["id"]),
            source_object=str(river["source_object"]),
            source_objects=[str(item) for item in river["source_objects"]],
        )
        river_attributes.update(
            {
                "data-water-class": "river",
                "data-derivation-id": derivation,
                "data-source-length-m": f"{float(river['length_m']):.1f}",
            }
        )
        for path in river["paths"]:
            for stroke in _page_line_strokes(
                path,
                physical_point=physical_point,
                map_rect=map_rect,
                exclusion=exclusion,
            ):
                layer.add(
                    stroke,
                    source_ref=source_ref,
                    role="source-sampled-river-centreline",
                    sequence=sequence,
                    attributes=river_attributes,
                )


def _add_stitched_linear_context(
    *,
    road_layer: ArtworkLayer,
    water_layer: ArtworkLayer,
    features: Sequence[dict[str, Any]],
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    label_exclusion: BaseGeometry,
    attributes: dict[str, str],
    variant_id: str | None,
    include_hydro: bool,
) -> tuple[float, float]:
    """Render stitched, legible source lines distributed along the route."""

    stitched = _stitched_context_paths(
        features,
        kinds=(
            frozenset({"road", "river", "coast"})
            if include_hydro
            else frozenset({"road"})
        ),
    )
    road_candidates = _road_page_context_candidates(
        [source for source in stitched if source.kind == "road"],
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
        exclusion=label_exclusion,
        variant_id=variant_id,
    )
    selected_roads = _select_page_context_lines(
        road_candidates,
        maximum=4 if variant_id == "terrain-relief" else 8,
        route_bands=3 if variant_id == "terrain-relief" else 6,
    )
    road_mm = 0.0
    for sequence, candidate in enumerate(selected_roads, start=1):
        source = candidate.source
        metadata = {
            **attributes,
            "data-feature-id": source.feature_ids[0],
            "data-feature-ids": ",".join(source.feature_ids),
            "data-feature-kind": "road",
            "data-road-class": str(source.road_class or "local"),
            "data-road-rendering": "single-centreline-no-casing-v2",
            "data-stitch-policy": "exact-endpoint-same-name-or-ref-v1",
            "data-source-fragment-count": str(len(source.feature_ids)),
            "data-route-axis": f"{candidate.route_axis:.4f}",
            "data-page-length-mm": f"{candidate.length_mm:.3f}",
        }
        if source.osm_elements:
            metadata["data-osm-element"] = source.osm_elements[0]
            metadata["data-osm-elements"] = ",".join(source.osm_elements)
            metadata["data-source-object-count"] = str(len(set(source.osm_elements)))
        if source.source_urls:
            metadata["data-source-url"] = source.source_urls[0]
        road_layer.add(
            candidate.points,
            source_ref=source.source_ref,
            role=f"source-sampled-road-{source.road_class or 'local'}",
            sequence=sequence,
            attributes=metadata,
        )
        road_mm += candidate.length_mm

    water_mm = 0.0
    if include_hydro:
        water_candidates = _page_context_line_candidates(
            [source for source in stitched if source.kind in {"river", "coast"}],
            physical_point=physical_point,
            map_rect=map_rect,
            route_lines=route_lines,
            exclusion=label_exclusion,
        )
        selected_water = _select_page_context_lines(
            water_candidates,
            maximum=5 if variant_id == "terrain-relief" else 9,
            route_bands=4 if variant_id == "terrain-relief" else 7,
        )
        for sequence, candidate in enumerate(selected_water, start=1):
            source = candidate.source
            metadata = {
                **attributes,
                "data-feature-id": source.feature_ids[0],
                "data-feature-ids": ",".join(source.feature_ids),
                "data-feature-kind": source.kind,
                "data-water-class": source.kind,
                "data-stitch-policy": "exact-endpoint-same-name-or-ref-v1",
                "data-source-fragment-count": str(len(source.feature_ids)),
                "data-route-axis": f"{candidate.route_axis:.4f}",
                "data-page-length-mm": f"{candidate.length_mm:.3f}",
                "data-smoothing-max-displacement-mm": "0.36",
            }
            if source.osm_elements:
                metadata["data-osm-element"] = source.osm_elements[0]
                metadata["data-osm-elements"] = ",".join(source.osm_elements)
                metadata["data-source-object-count"] = str(
                    len(set(source.osm_elements))
                )
            if source.source_urls:
                metadata["data-source-url"] = source.source_urls[0]
            water_layer.add(
                candidate.points,
                source_ref=source.source_ref,
                role=_context_role(source.kind),
                sequence=sequence,
                attributes=metadata,
            )
            water_mm += candidate.length_mm
    return road_mm, water_mm


def _road_page_context_candidates(
    road_sources: Sequence[_StitchedContextPath],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    exclusion: BaseGeometry,
    variant_id: str | None,
) -> list[_PageContextLine]:
    """Prefer arterial relief roads only when a printable stroke survives."""

    if variant_id == "terrain-relief":
        preferred_sources = [
            source
            for source in road_sources
            if source.road_class in {"major", "secondary"}
        ]
        preferred_candidates = _page_context_line_candidates(
            preferred_sources,
            physical_point=physical_point,
            map_rect=map_rect,
            route_lines=route_lines,
            exclusion=exclusion,
        )
        if preferred_candidates:
            return preferred_candidates
    return _page_context_line_candidates(
        road_sources,
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
        exclusion=exclusion,
    )


def _road_label_avoidance(
    features: Sequence[dict[str, Any]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    fixed_exclusion: BaseGeometry,
    variant_id: str | None,
) -> tuple[BaseGeometry, dict[str, Any]]:
    """Reserve only the factual road strokes that the overview would select."""

    road_sources = [
        source
        for source in _stitched_context_paths(
            features,
            kinds=frozenset({"road"}),
        )
        if source.kind == "road"
    ]
    candidates = _road_page_context_candidates(
        road_sources,
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
        exclusion=fixed_exclusion,
        variant_id=variant_id,
    )
    selected = _select_page_context_lines(
        candidates,
        maximum=4 if variant_id == "terrain-relief" else 8,
        route_bands=3 if variant_id == "terrain-relief" else 6,
    )
    geometry: BaseGeometry = (
        unary_union([LineString(candidate.points) for candidate in selected])
        if selected
        else Polygon()
    )
    return geometry, {
        "policy_id": "selected-source-road-before-copy-placement-v1",
        "selected_stroke_count": len(selected),
        "selected_source_feature_ids": sorted(
            {
                feature_id
                for candidate in selected
                for feature_id in candidate.source.feature_ids
            }
        ),
        "reserved_road_length_mm": round(
            sum(candidate.length_mm for candidate in selected),
            3,
        ),
        "copy_clearance_mm": 0.55,
        "source_geometry_policy": "exact-page-line-no-displacement",
    }


def _narrow_green_label_avoidance(
    features: Sequence[dict[str, Any]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    fixed_exclusion: BaseGeometry,
) -> tuple[BaseGeometry, dict[str, Any]]:
    """Reserve exact line-legible boundaries before placing map copy."""

    lines: list[LineString] = []
    feature_ids: set[str] = set()
    for feature in features:
        if feature.get("kind") not in {"woodland", "grass"}:
            continue
        for path in feature.get("paths", []):
            for polygon in _page_polygon_parts(
                path,
                physical_point=physical_point,
                map_rect=map_rect,
                minimum_area_mm2=MIN_LINEAR_LANDCOVER_AREA_MM2,
            ):
                if (
                    polygon.area >= 2.2
                    or polygon.length < MIN_LINEAR_LANDCOVER_BOUNDARY_MM
                ):
                    continue
                boundary: BaseGeometry = polygon.boundary
                if not fixed_exclusion.is_empty:
                    boundary = boundary.difference(fixed_exclusion)
                visible_lines = _geometry_lines(boundary)
                if not visible_lines:
                    continue
                lines.extend(visible_lines)
                feature_ids.add(str(feature["id"]))
    geometry: BaseGeometry = unary_union(lines) if lines else Polygon()
    return geometry, {
        "policy_id": "perimeter-legible-source-boundary-before-copy-v1",
        "selected_source_feature_ids": sorted(feature_ids),
        "selected_boundary_path_count": len(lines),
        "reserved_boundary_length_mm": round(
            sum(float(line.length) for line in lines),
            3,
        ),
        "copy_clearance_mm": 0.55,
        "area_geometry_policy": "exact-source-boundary-no-enlargement",
    }


@dataclass(frozen=True)
class _ContextDetailFocus:
    kind: str
    label: str
    points: tuple[Point, ...]


def _context_detail_needs(
    features: Sequence[dict[str, Any]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
) -> tuple[bool, bool, bool]:
    stitched = _stitched_context_paths(features)
    candidates = _page_context_line_candidates(
        stitched,
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
        exclusion=Polygon(),
    )
    road_lengths = [
        candidate.length_mm
        for candidate in candidates
        if candidate.source.kind == "road"
    ]
    water_lengths = [
        candidate.length_mm
        for candidate in candidates
        if candidate.source.kind in {"river", "coast"}
    ]
    green_areas = [
        polygon.area
        for feature in features
        if feature.get("kind") in {"woodland", "grass"}
        for path in feature.get("paths", [])
        for polygon in _page_polygon_parts(
            path,
            physical_point=physical_point,
            map_rect=map_rect,
        )
    ]
    return (
        any(feature.get("kind") == "road" for feature in features)
        and max(road_lengths, default=0.0) < 1.2,
        any(feature.get("kind") in {"river", "coast", "water"} for feature in features)
        and max(water_lengths, default=0.0) < 2.0,
        any(feature.get("kind") in {"woodland", "grass"} for feature in features)
        and max(green_areas, default=0.0) < 2.2,
    )


def _context_detail_focus(
    features: Sequence[dict[str, Any]],
    *,
    need_road: bool,
    need_water: bool,
    need_landcover: bool,
) -> _ContextDetailFocus | None:
    stitched = _stitched_context_paths(features)
    kind_order: list[frozenset[str]] = []
    if need_road:
        kind_order.append(frozenset({"road"}))
    if need_water:
        kind_order.append(frozenset({"river", "coast"}))
    for kinds in kind_order:
        candidates = [source for source in stitched if source.kind in kinds]
        if candidates:
            source = max(
                candidates,
                key=lambda item: (
                    sum(
                        _haversine_m(first, second)
                        for first, second in zip(item.points, item.points[1:])
                    ),
                    -item.priority,
                    item.feature_ids,
                ),
            )
            return _ContextDetailFocus(
                kind=source.kind,
                label=source.label,
                points=source.points,
            )
    if need_landcover:
        landcover_candidates = [
            (feature, path)
            for feature in features
            if feature.get("kind") in {"woodland", "grass"}
            for path in feature.get("paths", [])
            if isinstance(path, list) and len(path) >= 4
        ]
        if landcover_candidates:
            feature, path = max(
                landcover_candidates,
                key=lambda item: (
                    abs(
                        sum(
                            float(first[0]) * float(second[1])
                            - float(second[0]) * float(first[1])
                            for first, second in zip(item[1], item[1][1:])
                        )
                    ),
                    str(item[0].get("id", "")),
                ),
            )
            return _ContextDetailFocus(
                kind=str(feature["kind"]),
                label=str(feature["label"]),
                points=tuple((float(point[0]), float(point[1])) for point in path),
            )
    return None


def _section_road_focuses(
    features: Sequence[dict[str, Any]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    route_lines: Sequence[Sequence[Point]],
    maximum: int = 4,
) -> list[_ContextDetailFocus]:
    """Choose source-faithful road-window fallbacks nearest the route.

    Continental overviews can collapse even a selected road to less than one
    printable millimetre.  Framing the longest road in the acquisition is not
    sufficient: its centre may be hundreds of metres from the route, so adding
    a route anchor makes the local window sparse.  Rank stitched source roads
    by their actual page distance to the route, then use only an exact-vertex
    subpath around the closest approach as the framing seed.  The detail panel
    still renders the complete clipped source inventory; this helper neither
    synthesizes nor truncates plotted road geometry.
    """

    route_parts = [LineString(line) for line in route_lines if len(line) >= 2]
    if not route_parts or maximum <= 0:
        return []
    route_geometry = unary_union(route_parts)
    candidates: list[tuple[float, int, float, str, _ContextDetailFocus]] = []
    for source in _stitched_context_paths(
        features,
        kinds=frozenset({"road"}),
    ):
        page_points = [physical_point(point) for point in source.points]
        source_geometry = LineString(page_points)
        if source_geometry.length <= 1e-9:
            continue
        source_length_m = sum(
            _haversine_m(first, second)
            for first, second in zip(source.points, source.points[1:])
        )
        # Find the closest source location by testing exact source vertices
        # against the complete route geometry; the subpath remains entirely
        # source sampled.
        nearest_index = min(
            range(len(page_points)),
            key=lambda index: GeometryPoint(page_points[index]).distance(
                route_geometry
            ),
        )
        cumulative_m = [0.0]
        for first, second in zip(source.points, source.points[1:]):
            cumulative_m.append(cumulative_m[-1] + _haversine_m(first, second))
        centre_m = cumulative_m[nearest_index]
        window_indices = [
            index
            for index, distance_m in enumerate(cumulative_m)
            if max(0.0, centre_m - 250.0)
            <= distance_m
            <= min(source_length_m, centre_m + 250.0)
        ]
        if len(window_indices) < 2:
            window_indices = list(
                range(
                    max(0, nearest_index - 1),
                    min(len(source.points), nearest_index + 2),
                )
            )
        if len(window_indices) < 2:
            continue
        focus = _ContextDetailFocus(
            kind="road",
            label=source.label,
            points=tuple(source.points[index] for index in window_indices),
        )
        road_class_rank = {
            "major": 0,
            "secondary": 1,
            "local": 2,
        }.get(str(source.road_class or "local"), 3)
        candidates.append(
            (
                float(source_geometry.distance(route_geometry)),
                road_class_rank,
                -source_length_m,
                source.feature_ids[0],
                focus,
            )
        )
    candidates.sort(key=lambda candidate: candidate[:4])
    return [candidate[4] for candidate in candidates[:maximum]]


def _section_context_focuses(
    features: Sequence[dict[str, Any]],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    route_lines: Sequence[Sequence[Point]],
    maximum: int,
    avoid_axes: Sequence[float] = (),
    excluded_kinds: frozenset[str] = frozenset(),
) -> list[_ContextDetailFocus]:
    """Choose distinct source-context windows along a compressed route."""

    candidates: list[tuple[float, float, float, str, _ContextDetailFocus]] = []
    for source in _stitched_context_paths(features):
        if source.kind in excluded_kinds:
            continue
        page = [physical_point(point) for point in source.points]
        geometry = LineString(page)
        if geometry.length <= 1e-9:
            continue
        midpoint = geometry.interpolate(0.5, normalized=True)
        axis, route_distance = _route_axis_measure(midpoint, route_lines)
        candidates.append(
            (
                axis,
                route_distance,
                float(geometry.length),
                source.feature_ids[0],
                _ContextDetailFocus(
                    kind=source.kind,
                    label=source.label,
                    points=source.points,
                ),
            )
        )
    for feature in features:
        kind = str(feature.get("kind", ""))
        if kind not in {"water", "woodland", "grass"} or kind in excluded_kinds:
            continue
        for path_index, raw_path in enumerate(feature.get("paths", [])):
            if not isinstance(raw_path, list) or len(raw_path) < 2:
                continue
            points = tuple(
                (float(point[0]), float(point[1]))
                for point in raw_path
                if isinstance(point, list) and len(point) >= 2
            )
            if len(points) < 2:
                continue
            geometry = LineString([physical_point(point) for point in points])
            if geometry.length <= 1e-9:
                continue
            midpoint = geometry.interpolate(0.5, normalized=True)
            axis, route_distance = _route_axis_measure(midpoint, route_lines)
            candidates.append(
                (
                    axis,
                    route_distance,
                    float(geometry.length),
                    f"{feature.get('id', '')}-{path_index}",
                    _ContextDetailFocus(
                        kind=kind,
                        label=str(feature.get("label", kind)).upper(),
                        points=points,
                    ),
                )
            )
    if not candidates or maximum <= 0:
        return []

    selected: list[tuple[float, float, float, str, _ContextDetailFocus]] = []
    for target in (0.18, 0.82, 0.34, 0.66, 0.5, 0.08, 0.92):
        if len(selected) >= maximum:
            break
        options = [
            candidate
            for candidate in candidates
            if candidate not in selected
            and all(abs(candidate[0] - axis) >= 0.16 for axis in avoid_axes)
            and all(abs(candidate[0] - previous[0]) >= 0.2 for previous in selected)
        ]
        if not options:
            options = [
                candidate for candidate in candidates if candidate not in selected
            ]
        if not options:
            break
        selected.append(
            min(
                options,
                key=lambda candidate: (
                    abs(candidate[0] - target),
                    candidate[1],
                    -candidate[2],
                    candidate[3],
                ),
            )
        )
    return [candidate[4] for candidate in selected]


def _official_route_distance_km(route: dict[str, Any]) -> float | None:
    """Return the positive published distance across the two frozen schemas.

    The original Camino record predates the expansion catalog and names the
    field ``official_record_distance_km``.  Expansion records use
    ``official_distance_km``.  Both describe the same published total and must
    drive the A--E display scale; measured source-geometry chainage remains in
    separate SVG metadata.
    """

    raw_distance = route.get(
        "official_distance_km",
        route.get("official_record_distance_km"),
    )
    if (
        isinstance(raw_distance, (int, float))
        and not isinstance(raw_distance, bool)
        and math.isfinite(float(raw_distance))
        and float(raw_distance) > 0.0
    ):
        return float(raw_distance)
    return None


def _route_representation_metrics(
    record: dict[str, Any],
    *,
    route_lines: Sequence[Sequence[Point]],
    map_rect: Rect,
) -> tuple[float, float]:
    """Return kilometres per plotted millimetre and route-bbox field usage."""

    route_length_mm = sum(
        polyline_length_mm(line) for line in route_lines if len(line) >= 2
    )
    official_distance_km = _official_route_distance_km(record["route"]) or 0.0
    kilometres_per_mm = (
        official_distance_km / route_length_mm
        if route_length_mm > 1e-9 and official_distance_km > 0.0
        else 0.0
    )
    points = [point for line in route_lines for point in line]
    if not points or map_rect.width <= 0.0 or map_rect.height <= 0.0:
        return kilometres_per_mm, 0.0
    width = max(point[0] for point in points) - min(point[0] for point in points)
    height = max(point[1] for point in points) - min(point[1] for point in points)
    field_fraction = max(
        0.0,
        min(1.0, width * height / (map_rect.width * map_rect.height)),
    )
    return kilometres_per_mm, field_fraction


def _source_extent_field_fraction(
    extent: Sequence[float],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
) -> float:
    """Return the share of the overview field backed by acquired source extent."""

    if len(extent) != 4 or map_rect.width <= 0.0 or map_rect.height <= 0.0:
        return 0.0
    west, south, east, north = (float(value) for value in extent)
    corners = [
        physical_point((west, south)),
        physical_point((east, south)),
        physical_point((east, north)),
        physical_point((west, north)),
    ]
    width = max(point[0] for point in corners) - min(point[0] for point in corners)
    height = max(point[1] for point in corners) - min(point[1] for point in corners)
    return max(
        0.0,
        min(1.0, width * height / (map_rect.width * map_rect.height)),
    )


def _terrain_detail_focuses(
    terrain: dict[str, Any],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    map_rect: Rect,
    route_lines: Sequence[Sequence[Point]],
    maximum: int = 2,
    avoid_axes: Sequence[float] = (),
) -> list[_ContextDetailFocus]:
    """Choose separated source-terrain windows along a compressed overview."""

    if maximum <= 0:
        return []
    candidates: list[tuple[int, float, float, float, str, tuple[Point, ...]]] = []
    for source_rank, collection in (
        # A contour usually spans enough source terrain to make a useful local
        # window.  An isolated fall line is a poor framing seed even though it
        # remains valid relief geometry once the window has been chosen.
        (0, terrain.get("contours", [])),
        (1, terrain.get("relief_strokes", [])),
    ):
        if not isinstance(collection, list):
            continue
        for item_index, item in enumerate(collection):
            raw_paths = (
                item.get("paths", []) if source_rank == 0 else [item.get("points", [])]
            )
            for path_index, raw_path in enumerate(raw_paths):
                if not isinstance(raw_path, list) or len(raw_path) < 2:
                    continue
                points = tuple(
                    (float(point[0]), float(point[1]))
                    for point in raw_path
                    if isinstance(point, list) and len(point) >= 2
                )
                if len(points) < 2:
                    continue
                page = [physical_point(point) for point in points]
                geometry = LineString(page)
                if geometry.length <= 1e-9:
                    continue
                midpoint = geometry.interpolate(0.5, normalized=True)
                if (
                    not box(
                        map_rect.left,
                        map_rect.top,
                        map_rect.right,
                        map_rect.bottom,
                    )
                    .buffer(0.01)
                    .intersects(midpoint)
                ):
                    continue
                axis, route_distance = _route_axis_measure(midpoint, route_lines)
                vertical_fraction = min(
                    1.0,
                    max(0.0, (float(midpoint.y) - map_rect.top) / map_rect.height),
                )
                identifier = str(item.get("id", f"terrain-{item_index}-{path_index}"))
                candidates.append(
                    (
                        source_rank,
                        axis,
                        route_distance,
                        vertical_fraction,
                        identifier,
                        points,
                    )
                )
    if not candidates:
        return []

    selected: list[tuple[int, float, float, float, str, tuple[Point, ...]]] = []
    targets = (0.18, 0.82, 0.34, 0.66, 0.5, 0.08, 0.92)
    for target in targets:
        if len(selected) >= maximum:
            break
        options = [
            item
            for item in candidates
            if item not in selected
            and all(abs(item[1] - axis) >= 0.16 for axis in avoid_axes)
            and all(abs(item[1] - previous[1]) >= 0.2 for previous in selected)
        ]
        if not options:
            options = [item for item in candidates if item not in selected]
        if not options:
            break
        selected.append(
            min(
                options,
                key=lambda item: (
                    item[0],
                    abs(item[1] - target),
                    item[2],
                    item[4],
                ),
            )
        )

    focuses: list[_ContextDetailFocus] = []
    for _rank, _axis, _distance, vertical, identifier, points in selected:
        region = (
            "NORTH" if vertical < 0.36 else "SOUTH" if vertical > 0.64 else "CENTRAL"
        )
        focuses.append(
            _ContextDetailFocus(
                kind="terrain",
                label=f"{region} TERRAIN",
                points=points,
            )
        )
    return focuses


def _route_section_focuses(
    record: dict[str, Any],
    *,
    physical_point: Callable[[Sequence[float]], Point],
    maximum: int = 2,
    avoid_axes: Sequence[float] = (),
) -> list[_ContextDetailFocus]:
    """Create honest north-up local windows around separated route sections."""

    if maximum <= 0:
        return []
    source_points: list[Point] = []
    cumulative: list[float] = []
    distance_m = 0.0
    previous: Point | None = None
    for segment in record["route"]["segments"]:
        for raw_point in segment["points"]:
            point = (float(raw_point[0]), float(raw_point[1]))
            if previous is not None:
                distance_m += _haversine_m(previous, point)
            source_points.append(point)
            cumulative.append(distance_m)
            previous = point
        # Do not fabricate distance across explicitly separate source segments.
        previous = None
    if len(source_points) < 2 or distance_m <= 1e-9:
        return []

    official_distance_km = _official_route_distance_km(record["route"]) or 0.0
    half_fraction = min(
        0.1,
        max(0.015, 50.0 / max(official_distance_km, 1.0)),
    )
    available_targets = [
        target
        for target in (0.18, 0.82, 0.34, 0.66, 0.5, 0.08, 0.92)
        if all(abs(target - axis) >= 0.16 for axis in avoid_axes)
    ]
    if not available_targets:
        available_targets = [0.18, 0.82, 0.34, 0.66, 0.5, 0.08, 0.92]

    focuses: list[_ContextDetailFocus] = []
    for target in available_targets:
        if len(focuses) >= maximum:
            break
        lower = max(0.0, target - half_fraction) * distance_m
        upper = min(1.0, target + half_fraction) * distance_m
        indices = [
            index
            for index, distance in enumerate(cumulative)
            if lower <= distance <= upper
        ]
        if len(indices) < 3:
            centre_index = min(
                range(len(cumulative)),
                key=lambda index: abs(cumulative[index] / distance_m - target),
            )
            start = max(0, centre_index - 2)
            end = min(len(source_points), centre_index + 3)
            indices = list(range(start, end))
        points = tuple(source_points[index] for index in indices)
        if len(points) < 2:
            continue
        midpoint = physical_point(points[len(points) // 2])
        page_points = [physical_point(point) for point in source_points]
        minimum_y = min(point[1] for point in page_points)
        maximum_y = max(point[1] for point in page_points)
        vertical = (midpoint[1] - minimum_y) / max(maximum_y - minimum_y, 1e-9)
        region = (
            "NORTH" if vertical < 0.36 else "SOUTH" if vertical > 0.64 else "CENTRAL"
        )
        focuses.append(
            _ContextDetailFocus(
                kind="terrain",
                label=f"{region} TERRAIN",
                points=points,
            )
        )
    return focuses


def _focus_route_axis(
    focus: _ContextDetailFocus,
    *,
    physical_point: Callable[[Sequence[float]], Point],
    route_lines: Sequence[Sequence[Point]],
) -> float:
    page = [physical_point(point) for point in focus.points]
    centre = LineString(page).interpolate(0.5, normalized=True)
    return _route_axis_measure(centre, route_lines)[0]


def _context_detail_extent(
    focus: _ContextDetailFocus,
    *,
    context_extent: Sequence[float],
    route_segments: Sequence[dict[str, Any]],
    drawing_rect: Rect,
) -> tuple[float, float, float, float]:
    west, south, east, north = (float(value) for value in context_extent)
    focus_longitudes = [point[0] for point in focus.points]
    focus_latitudes = [point[1] for point in focus.points]
    centre = (
        (min(focus_longitudes) + max(focus_longitudes)) / 2.0,
        (min(focus_latitudes) + max(focus_latitudes)) / 2.0,
    )
    route_points = [
        (float(point[0]), float(point[1]))
        for segment in route_segments
        for point in segment["points"]
    ]
    if focus.kind == "road":
        cosine = max(math.cos(math.radians(centre[1])), 1e-6)
        centre_geometry = GeometryPoint(centre[0] * cosine, centre[1])
        nearest_candidates: list[tuple[float, Point]] = []
        for segment in route_segments:
            route_geometry = LineString(
                [
                    (float(point[0]) * cosine, float(point[1]))
                    for point in segment["points"]
                ]
            )
            if route_geometry.length <= 1e-12:
                continue
            nearest_geometry = route_geometry.interpolate(
                route_geometry.project(centre_geometry)
            )
            nearest_candidates.append(
                (
                    float(nearest_geometry.distance(centre_geometry)),
                    (float(nearest_geometry.x) / cosine, float(nearest_geometry.y)),
                )
            )
        nearest_route = (
            min(nearest_candidates, key=lambda candidate: candidate[0])[1]
            if nearest_candidates
            else min(route_points, key=lambda point: _haversine_m(point, centre))
        )
    else:
        nearest_route = min(
            route_points,
            key=lambda point: _haversine_m(point, centre),
        )
    focus_longitude_span = max(focus_longitudes) - min(focus_longitudes)
    focus_latitude_span = max(focus_latitudes) - min(focus_latitudes)
    include_route_anchor = focus.kind not in {"woodland", "grass"} or (
        abs(nearest_route[0] - centre[0]) <= max(focus_longitude_span * 3.0, 1e-5)
        and abs(nearest_route[1] - centre[1]) <= max(focus_latitude_span * 3.0, 1e-5)
    )
    longitudes = [*focus_longitudes]
    latitudes = [*focus_latitudes]
    if include_route_anchor:
        longitudes.append(nearest_route[0])
        latitudes.append(nearest_route[1])
    centre_longitude = (min(longitudes) + max(longitudes)) / 2.0
    centre_latitude = (min(latitudes) + max(latitudes)) / 2.0
    focus_padding = 1.08 if focus.kind == "road" else 1.55
    minimum_extent_fraction = (
        0.0005
        if focus.kind in {"woodland", "grass"}
        else 0.00025
        if focus.kind == "road"
        else 0.008
    )
    longitude_span = max(
        (max(longitudes) - min(longitudes)) * focus_padding,
        (east - west) * minimum_extent_fraction,
        1e-6,
    )
    latitude_span = max(
        (max(latitudes) - min(latitudes)) * focus_padding,
        (north - south) * minimum_extent_fraction,
        1e-6,
    )
    cosine = max(math.cos(math.radians(centre_latitude)), 1e-6)
    target_aspect = drawing_rect.width / drawing_rect.height
    current_aspect = longitude_span * cosine / latitude_span
    if current_aspect < target_aspect:
        longitude_span = latitude_span * target_aspect / cosine
    else:
        latitude_span = longitude_span * cosine / target_aspect
    longitude_span = min(longitude_span, east - west)
    latitude_span = min(latitude_span, north - south)
    centre_longitude = min(
        max(centre_longitude, west + longitude_span / 2.0),
        east - longitude_span / 2.0,
    )
    centre_latitude = min(
        max(centre_latitude, south + latitude_span / 2.0),
        north - latitude_span / 2.0,
    )
    return (
        centre_longitude - longitude_span / 2.0,
        centre_latitude - latitude_span / 2.0,
        centre_longitude + longitude_span / 2.0,
        centre_latitude + latitude_span / 2.0,
    )


def _context_detail_rect(
    map_rect: Rect,
    *,
    route_lines: Sequence[Sequence[Point]],
    occupied_boxes: Sequence[Rect],
    emphasized: bool = False,
) -> Rect | None:
    route_geometry = unary_union(
        [LineString(line) for line in route_lines if len(line) >= 2]
    )
    sizes = (
        (
            (min(42.0, map_rect.width * 0.36), min(31.0, map_rect.height * 0.33)),
            (min(38.0, map_rect.width * 0.33), min(28.0, map_rect.height * 0.30)),
            (min(34.0, map_rect.width * 0.29), min(24.0, map_rect.height * 0.27)),
            (min(30.0, map_rect.width * 0.26), min(22.0, map_rect.height * 0.24)),
            (min(27.0, map_rect.width * 0.23), min(20.0, map_rect.height * 0.22)),
        )
        if emphasized
        else ((min(34.0, map_rect.width * 0.29), min(24.0, map_rect.height * 0.27)),)
    )
    candidates: list[tuple[float, float, int, Rect]] = []
    sequence = 0
    for width, height in sizes:
        x_positions = (
            map_rect.left + 1.0,
            map_rect.right - width - 1.0,
            map_rect.left + (map_rect.width - width) / 2.0,
        )
        y_positions = (
            (
                map_rect.top + 1.0,
                map_rect.top + (map_rect.height - height) / 2.0,
                map_rect.bottom - height - 1.0,
            )
            if emphasized
            else (
                map_rect.top + 6.0,
                map_rect.top + (map_rect.height - height) / 2.0,
                map_rect.bottom - height - 1.0,
            )
        )
        for y in y_positions:
            for x in x_positions:
                sequence += 1
                candidate = Rect(x, y, width, height)
                if any(
                    _rectangles_overlap(candidate, occupied, gap=0.8)
                    for occupied in occupied_boxes
                ):
                    continue
                geometry = box(
                    candidate.left,
                    candidate.top,
                    candidate.right,
                    candidate.bottom,
                )
                clearance = (
                    float(route_geometry.distance(geometry))
                    if not route_geometry.is_empty
                    else math.inf
                )
                if clearance < 1.2:
                    continue
                candidates.append(
                    (
                        -candidate.width * candidate.height,
                        -clearance,
                        sequence,
                        candidate,
                    )
                )
    return min(candidates)[3] if candidates else None


def _add_context_detail_inset(
    artwork: PlateArtwork,
    *,
    context: dict[str, Any],
    record: dict[str, Any],
    inset: Rect,
    focus: _ContextDetailFocus,
    road_layer: ArtworkLayer,
    water_layer: ArtworkLayer,
    woodland_layer: ArtworkLayer,
    terrain_layer: ArtworkLayer,
    frame_layer: ArtworkLayer,
    label_layer: ArtworkLayer,
    attributes: dict[str, str],
    seed: int,
    variant_id: str | None,
) -> tuple[float, float, float, float, float, float] | None:
    """Add one useful, independently scaled north-up source detail panel.

    The panel is transactional.  Source windows that collapse to a decorative
    frame plus the red route are rolled back so the caller can try another
    factual focus without leaving empty furniture in the artwork.
    """
    hero = artwork.layer("hero_route", "Source-sampled walking route", "red-0-4")
    mutable_layers = (
        road_layer,
        water_layer,
        woodland_layer,
        terrain_layer,
        frame_layer,
        label_layer,
        hero,
    )
    record_starts = {id(layer): len(layer.records) for layer in mutable_layers}
    drawing = Rect(inset.x + 1.2, inset.y + 4.2, inset.width - 2.4, inset.height - 5.4)
    local_extent = _context_detail_extent(
        focus,
        context_extent=context["extent"],
        route_segments=record["route"]["segments"],
        drawing_rect=drawing,
    )
    _local_route_lines, local_point = _geographic_transform(
        record["route"]["segments"],
        drawing,
        extent=local_extent,
        rotation_deg=0.0,
    )
    detail_attributes = {
        **attributes,
        "data-context-view": "framed-north-up-route-detail",
        "data-detail-focus-kind": focus.kind,
        "data-detail-focus-label": focus.label,
        "data-detail-extent": ",".join(f"{value:.6f}" for value in local_extent),
        "data-orientation-policy": "north-up",
        "data-scale-policy": "independent-local-source-detail",
        "data-detail-quality-policy": "minimum-useful-source-geography-v1",
    }
    frame_layer.add(
        rectangle_stroke(inset),
        source_ref=str(context["source_ref"]),
        role="context-detail-inset-frame",
        attributes=detail_attributes,
    )
    preferred_header = f"N-UP / {plotter_copy(focus.label).upper()}"
    focus_identity = _label_identity(focus.label)
    header = (
        preferred_header
        if focus_identity
        and not focus_identity.isdigit()
        and focus_identity
        not in {"road", "river", "stream", "water", "woodland", "grass"}
        and text_width_mm(preferred_header, cap_height_mm=2.0)
        <= inset.width - 6.0 + 1e-9
        else (
            "N-UP TERRAIN"
            if focus.kind == "terrain"
            else "N-UP LAND"
            if focus.kind in {"woodland", "grass"}
            else "N-UP DETAIL"
        )
    )
    add_text(
        label_layer,
        header,
        x_mm=inset.left + 1.2,
        y_mm=inset.top + 0.7,
        preferred_cap_mm=2.0,
        minimum_cap_mm=2.0,
        maximum_width_mm=inset.width - 6.0,
        source_ref=str(context["source_ref"]),
        role="context-detail-inset-label",
        attributes=detail_attributes,
    )
    north_x = inset.right - 2.6
    frame_layer.add(
        [(north_x, inset.top + 3.4), (north_x, inset.top + 1.1)],
        source_ref=str(context["source_ref"]),
        role="context-detail-north-arrow",
        attributes=detail_attributes,
    )
    frame_layer.add(
        [
            (north_x - 0.5, inset.top + 2.0),
            (north_x, inset.top + 1.1),
            (north_x + 0.5, inset.top + 2.0),
        ],
        source_ref=str(context["source_ref"]),
        role="context-detail-north-arrow-head",
        attributes=detail_attributes,
    )

    local_route_geometry_parts: list[LineString] = []
    for segment in record["route"]["segments"]:
        for stroke in _page_line_strokes(
            segment["points"],
            physical_point=local_point,
            map_rect=drawing,
        ):
            if polyline_length_mm(stroke) + 1e-9 < MIN_HERO_ROUTE_STROKE_MM:
                continue
            local_route_geometry_parts.append(LineString(stroke))
            hero.add(
                stroke,
                source_ref=str(segment["source_ref"]),
                role="context-detail-source-route",
                attributes=detail_attributes,
            )
    local_route_geometry: BaseGeometry = (
        unary_union(local_route_geometry_parts)
        if local_route_geometry_parts
        else Polygon()
    )
    local_area_exclusion = (
        local_route_geometry.buffer(0.75, cap_style="round", join_style="round")
        if not local_route_geometry.is_empty
        else Polygon()
    )

    terrain_mm = 0.0
    terrain = context.get("terrain")
    if isinstance(terrain, dict):
        before = len(terrain_layer.records)
        _add_derived_terrain(
            terrain_layer,
            terrain=terrain,
            map_rect=drawing,
            physical_point=local_point,
            exclusion=local_area_exclusion,
            fall_line_exclusion=local_area_exclusion,
            attributes={
                **detail_attributes,
                "data-terrain-view": "local-source-detail",
            },
            label_layer=None,
            # A detailed-map panel remains map-led even when terrain is its
            # focus: retain all factual contours at the local scale, but never
            # borrow relief-only fall-line hachures from the paired edition.
            variant_id=variant_id,
            retain_all_contours=(
                variant_id == "detailed-map" and focus.kind == "terrain"
            ),
        )
        terrain_mm = sum(
            polyline_length_mm(item.points) for item in terrain_layer.records[before:]
        )

    road_mm = 0.0
    water_mm = 0.0
    green_mm = 0.0
    stitched = _stitched_context_paths(context["features"])
    for sequence, source in enumerate(stitched, start=1):
        target_layer = road_layer if source.kind == "road" else water_layer
        role = (
            f"source-sampled-road-{source.road_class or 'local'}"
            if source.kind == "road"
            else _context_role(source.kind)
        )
        for stroke in _page_line_strokes(
            source.points,
            physical_point=local_point,
            map_rect=drawing,
            smoothing=True,
        ):
            length_mm = polyline_length_mm(stroke)
            if length_mm < MIN_FINE_STROKE_MM:
                continue
            source_attributes = {
                **detail_attributes,
                "data-feature-id": source.feature_ids[0],
                "data-feature-ids": ",".join(source.feature_ids),
                "data-stitch-policy": "exact-endpoint-same-name-or-ref-v1",
                "data-source-fragment-count": str(len(source.feature_ids)),
            }
            if source.osm_elements:
                source_attributes["data-osm-element"] = source.osm_elements[0]
                source_attributes["data-osm-elements"] = ",".join(source.osm_elements)
                source_attributes["data-source-object-count"] = str(
                    len(set(source.osm_elements))
                )
            if source.source_urls:
                source_attributes["data-source-url"] = source.source_urls[0]
            target_layer.add(
                stroke,
                source_ref=source.source_ref,
                role=role,
                sequence=sequence,
                attributes=source_attributes,
            )
            if source.kind == "road":
                road_mm += length_mm
            else:
                water_mm += length_mm

    for sequence, feature in enumerate(context["features"], start=1):
        kind = str(feature["kind"])
        if kind not in {"water", "woodland", "grass"}:
            continue
        feature_attributes = {
            **detail_attributes,
            "data-feature-id": str(feature["id"]),
            "data-feature-kind": kind,
        }
        if feature.get("osm_type") is not None:
            feature_attributes["data-osm-element"] = (
                f"{feature['osm_type']}/{feature['osm_id']}"
            )
        if feature.get("source_url"):
            feature_attributes["data-source-url"] = str(feature["source_url"])
        for path in feature.get("paths", []):
            for polygon in _page_polygon_parts(
                path,
                physical_point=local_point,
                map_rect=drawing,
                simplify_mm=0.06,
            ):
                if kind == "water":
                    for boundary in _masked_boundary_strokes(
                        polygon,
                        exclusion=local_area_exclusion,
                    ):
                        if polyline_length_mm(boundary) < MIN_FINE_STROKE_MM:
                            continue
                        water_layer.add(
                            boundary,
                            source_ref=str(feature["source_ref"]),
                            role="source-sampled-lake-boundary",
                            sequence=sequence,
                            attributes={
                                **feature_attributes,
                                "data-water-class": "inland",
                                "data-area-rendering": "local-detail-source-shoreline-v1",
                            },
                        )
                        water_mm += polyline_length_mm(boundary)
                else:
                    before = len(woodland_layer.records)
                    emitted = _add_green_polygon(
                        woodland_layer,
                        polygon,
                        source_ref=str(feature["source_ref"]),
                        sequence=sequence,
                        attributes=feature_attributes,
                        seed=seed + sequence,
                        semantic_class=kind,
                        exclusion=local_area_exclusion,
                        woodland_symbols=kind == "woodland",
                    )
                    if emitted == 0:
                        anchor = polygon.representative_point()
                        _add_source_anchored_feature_symbol(
                            woodland_layer,
                            anchor=(float(anchor.x), float(anchor.y)),
                            map_rect=drawing,
                            exclusion=local_area_exclusion,
                            source_ref=str(feature["source_ref"]),
                            sequence=sequence,
                            attributes={
                                **feature_attributes,
                                "data-landcover-class": kind,
                            },
                            semantic_class=kind,
                        )
                    green_mm += sum(
                        polyline_length_mm(item.points)
                        for item in woodland_layer.records[before:]
                    )
    drawing_geometry = box(
        drawing.left,
        drawing.top,
        drawing.right,
        drawing.bottom,
    )
    geography_layers = (road_layer, water_layer, woodland_layer, terrain_layer)
    family_lengths_list: list[float] = []
    useful_geometries: list[BaseGeometry] = []
    for target_layer in geography_layers:
        family_length = 0.0
        for item in target_layer.records[record_starts[id(target_layer)] :]:
            if len(item.points) < 2:
                continue
            clipped = LineString(item.points).intersection(drawing_geometry)
            if clipped.is_empty:
                continue
            family_length += float(clipped.length)
            useful_geometries.append(clipped)
        family_lengths_list.append(family_length)
    road_mm, water_mm, green_mm, terrain_mm = family_lengths_list
    family_lengths = (road_mm, water_mm, green_mm, terrain_mm)
    useful_geography_mm = sum(family_lengths)
    useful_family_count = sum(length >= 3.0 for length in family_lengths)
    strongest_family_mm = max(family_lengths, default=0.0)
    useful_density = useful_geography_mm / max(drawing.width * drawing.height, 1e-9)
    merged_geography = (
        unary_union(useful_geometries) if useful_geometries else LineString()
    )
    occupied_cells = 0
    grid_columns = 8
    grid_rows = 5
    for row in range(grid_rows):
        cell_top = drawing.top + drawing.height * row / grid_rows
        cell_bottom = drawing.top + drawing.height * (row + 1) / grid_rows
        for column in range(grid_columns):
            cell_left = drawing.left + drawing.width * column / grid_columns
            cell_right = drawing.left + drawing.width * (column + 1) / grid_columns
            if (
                not merged_geography.is_empty
                and merged_geography.intersection(
                    box(cell_left, cell_top, cell_right, cell_bottom)
                ).length
                >= 0.25
            ):
                occupied_cells += 1
    occupied_fraction = occupied_cells / float(grid_columns * grid_rows)
    accepted = (
        useful_geography_mm >= 20.0
        and useful_density >= 0.02
        and occupied_fraction >= 0.15
        and (useful_family_count >= 2 or strongest_family_mm >= 30.0)
        and (
            focus.kind != "terrain"
            or terrain_mm >= 20.0
            or (useful_geography_mm >= 30.0 and useful_family_count >= 2)
        )
    )
    if not accepted:
        for target_layer in mutable_layers:
            del target_layer.records[record_starts[id(target_layer)] :]
        return None

    quality_attributes = {
        "data-detail-useful-geography-mm": f"{useful_geography_mm:.3f}",
        "data-detail-useful-family-count": str(useful_family_count),
        "data-detail-useful-density-mm-per-mm2": f"{useful_density:.4f}",
        "data-detail-occupied-grid-fraction": f"{occupied_fraction:.4f}",
        "data-detail-minimum-useful-geography-mm": "20",
        "data-detail-minimum-density-mm-per-mm2": "0.02",
        "data-detail-minimum-occupied-grid-fraction": "0.15",
    }
    for target_layer in mutable_layers:
        for item in target_layer.records[record_starts[id(target_layer)] :]:
            item.attributes.update(quality_attributes)
    return (
        road_mm,
        water_mm,
        green_mm,
        terrain_mm,
        useful_density,
        occupied_fraction,
    )


def _terrain_for_variant(
    context: dict[str, Any],
    *,
    variant_id: str | None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the factual terrain bundle assigned to one artwork variant."""

    if variant_id == "terrain-relief":
        relief = context.get("relief_terrain")
        if isinstance(relief, dict):
            return relief
    if variant_id == "detailed-map" and isinstance(record, dict):
        relief = context.get("relief_terrain")
        precedence = (
            record.get("terrain_derivation", {})
            .get("source_precedence", {})
            .get("relief_terrain_selection", {})
        )
        native_evidence = precedence.get("native", {})
        relief_evidence = precedence.get("global", {})
        native_length = native_evidence.get("normalized_full_field_length")
        relief_length = relief_evidence.get("normalized_full_field_length")
        native_levels = native_evidence.get("contour_level_count")
        relief_levels = relief_evidence.get("contour_level_count")
        # Several authoritative terrain products are narrow route corridors.
        # They remain the primary native evidence, but at A5 their page-wide
        # context edition can be almost empty.  Reuse the already-selected,
        # extent-bound relief bundle when it loses no levels and provides a
        # material full-field gain; the detailed renderer still applies its
        # stricter index-contour density cap below.
        if (
            isinstance(relief, dict)
            and isinstance(native_length, (int, float))
            and not isinstance(native_length, bool)
            and isinstance(relief_length, (int, float))
            and not isinstance(relief_length, bool)
            and isinstance(native_levels, int)
            and not isinstance(native_levels, bool)
            and isinstance(relief_levels, int)
            and not isinstance(relief_levels, bool)
            and float(native_length) < 5.0
            and float(relief_length) >= max(float(native_length) * 2.0, 8.0)
            and relief_levels >= native_levels
        ):
            return relief
    terrain = context.get("terrain")
    return terrain if isinstance(terrain, dict) else None


def _add_context(
    artwork: PlateArtwork,
    map_rect: Rect,
    record: dict[str, Any],
    physical_point: Callable[[Sequence[float]], Point],
    route_lines: Sequence[Sequence[Point]],
    *,
    variant_id: str | None,
    chainage_station_boxes: Sequence[Rect] = (),
) -> None:
    context = record["context"]
    attributes = {
        "data-context-status": context["status"],
        "data-geometry-status": context["geometry_status"],
        "data-navigation-status": "artwork-not-for-navigation",
    }
    water_layer = artwork.layer(
        "context_water", "Source-sampled coast and water", "blue-0-25"
    )
    woodland_layer = artwork.layer(
        "context_woodland",
        "Source-sampled land-cover boundaries and bounded texture",
        "green-0-25",
    )
    road_layer = artwork.layer(
        "context_roads",
        "Source-sampled road and track centrelines",
        "grey-0-25",
    )
    relief_layer = artwork.layer(
        "context_relief",
        "Source-derived minor terrain contours and disclosed geographic forms",
        CONTOUR_MINOR_PEN_ID,
    )
    relief_index_layer = artwork.layer(
        "context_relief_index",
        "Source-derived fifth-interval index contours",
        CONTOUR_INDEX_PEN_ID,
    )
    relief_label_layer = artwork.layer(
        "context_relief_labels",
        "Source-valued contour altitude labels",
        "grey-0-25",
    )
    designation_layer = artwork.layer(
        "context_designations",
        "Protected and named landscape boundaries",
        "grey-0-25",
    )
    marker_layer = artwork.layer(
        "context_markers", "Context feature markers", "black-0-25"
    )
    label_layer = artwork.layer(
        "context_labels", "Collision-safe geographic labels", "black-0-25"
    )

    label_features: list[dict[str, Any]] = []
    for raw_feature in context["features"]:
        if raw_feature.get("display_label", True) is False:
            continue
        feature = dict(raw_feature)
        feature["_base_label_identity"] = _label_identity(feature["label"])
        if (
            feature.get("kind") in {"peak", "pass"}
            and feature.get("elevation_m") is not None
        ):
            feature["label"] = (
                f"{feature['label']} / {float(feature['elevation_m']):.0f} M"
            )
        label_features.append(feature)
    water = context.get("water")
    if isinstance(water, dict):
        existing_labels = {
            _label_identity(feature["label"]) for feature in label_features
        }
        for hydro_label in water.get("labels", []):
            label_identity = _label_identity(hydro_label["label"])
            if label_identity in existing_labels:
                continue
            source_object = str(hydro_label["source_object"])
            label_features.append(
                {
                    **hydro_label,
                    "source_ref": str(water["source_ref"]),
                    "osm_type": source_object.split("/", 1)[0],
                    "osm_id": int(source_object.split("/", 1)[1]),
                }
            )
            existing_labels.add(label_identity)
    existing_by_label: dict[str, dict[str, Any]] = {}
    for feature in label_features:
        identity = _label_base_identity(feature)
        existing = existing_by_label.get(identity)
        if existing is None or _label_information_order(
            feature
        ) < _label_information_order(existing):
            existing_by_label[identity] = feature
    for control in record["route"]["controls"]:
        if control["kind"] not in {"start", "finish", "stage"}:
            continue
        control_priority = -2 if control["kind"] in {"start", "finish"} else -1
        control_identity = _label_identity(control["name"])
        existing = existing_by_label.get(control_identity)
        if existing is not None:
            existing["priority"] = min(
                int(existing.get("priority", 9)), control_priority
            )
            existing["route_control"] = True
            if (
                existing.get("kind") == "pass"
                and existing.get("elevation_m") is not None
            ):
                existing["label"] = (
                    f"{str(control['name']).upper()} / "
                    f"{float(existing['elevation_m']):.0f} M"
                )
            continue
        label_features.append(
            {
                "id": f"route-control-{control['kind']}-{len(label_features) + 1}",
                "kind": "settlement",
                "label": str(control["name"]).upper(),
                "point": control["point"],
                "source_ref": control["source_ref"],
                "priority": control_priority,
                "route_control": True,
            }
        )
        existing_by_label[control_identity] = label_features[-1]

    north_arrow = (
        _north_arrow_placement(map_rect, route_lines)
        if variant_id is not None
        else None
    )
    terrain = _terrain_for_variant(
        context,
        variant_id=variant_id,
        record=record,
    )
    native_terrain = context.get("terrain")
    if (
        variant_id == "detailed-map"
        and isinstance(terrain, dict)
        and isinstance(native_terrain, dict)
        and terrain is not native_terrain
    ):
        artwork.rendering_metadata["detailed_terrain_source_policy"] = {
            "policy_id": "full-field-relief-fallback-for-sparse-native-context-v1",
            "native_source_ref": str(native_terrain.get("source_ref") or ""),
            "selected_source_ref": str(terrain.get("source_ref") or ""),
            "selection_evidence": copy.deepcopy(
                record.get("terrain_derivation", {})
                .get("source_precedence", {})
                .get("relief_terrain_selection", {})
            ),
        }
    station_reservations = list(chainage_station_boxes)
    fixed_reservations: list[Rect] = [*station_reservations]
    if north_arrow is not None:
        fixed_reservations.append(north_arrow.box)
    # Continuous contours run across the full geographic field.  Relief cells
    # no longer reserve dashboard-like rectangles from the label plan.
    terrain_reservations: list[Rect] = []
    detail_needs = (
        _context_detail_needs(
            context["features"],
            physical_point=physical_point,
            map_rect=map_rect,
            route_lines=route_lines,
        )
        if variant_id is not None
        else (False, False, False)
    )
    if variant_id is not None:
        # The paired artworks are single full-field compositions.  Source
        # families that are too small at overview scale remain truthfully
        # omitted instead of being magnified into floating inset boxes.
        detail_needs = (False, False, False)
    kilometres_per_mm, route_field_fraction = _route_representation_metrics(
        record,
        route_lines=route_lines,
        map_rect=map_rect,
    )
    source_extent_field_fraction = _source_extent_field_fraction(
        context.get("route_extent", context["extent"]),
        physical_point=physical_point,
        map_rect=map_rect,
    )
    sectional_detail = False
    artwork.rendering_metadata["route_representation"] = {
        "official_km_per_overview_mm": round(kilometres_per_mm, 3),
        "route_bbox_field_fraction": round(route_field_fraction, 4),
        "source_extent_field_fraction": round(source_extent_field_fraction, 4),
        "sectional_detail_policy": (
            "full-field-continuous-context-v2"
            if variant_id is not None
            else "single-overview-v1"
        ),
    }

    detail_focuses: list[_ContextDetailFocus] = []

    def append_unique_focuses(
        focuses: Sequence[_ContextDetailFocus],
    ) -> None:
        for focus in focuses:
            if focus not in detail_focuses:
                detail_focuses.append(focus)

    if not sectional_detail:
        if any(detail_needs):
            primary_focus = _context_detail_focus(
                context["features"],
                need_road=detail_needs[0],
                need_water=detail_needs[1],
                need_landcover=detail_needs[2],
            )
            if primary_focus is not None:
                append_unique_focuses((primary_focus,))
        if (
            variant_id == "detailed-map"
            and detail_needs[2]
            and not any(focus.kind in {"woodland", "grass"} for focus in detail_focuses)
        ):
            land_focus = _context_detail_focus(
                context["features"],
                need_road=False,
                need_water=False,
                need_landcover=True,
            )
            if land_focus is not None:
                append_unique_focuses((land_focus,))
        detail_focuses = detail_focuses[:2]
    else:
        # A compressed continental overview needs two independently useful
        # windows, not two retries from the same evidence family.  Keep road
        # retries together so the first panel can find one printable sourced
        # road, while reserving non-road and terrain/route-section candidates
        # ahead of the final candidate cap.  Only one road panel may later be
        # accepted by the transactional rendering loop.
        road_focuses = (
            _section_road_focuses(
                context["features"],
                physical_point=physical_point,
                route_lines=route_lines,
                maximum=4,
            )
            if detail_needs[0]
            and any(feature.get("kind") == "road" for feature in context["features"])
            else []
        )
        primary_context_focus = _context_detail_focus(
            context["features"],
            need_road=False,
            need_water=detail_needs[1],
            need_landcover=detail_needs[2],
        )
        selection_anchors = [
            *road_focuses[:1],
            *((primary_context_focus,) if primary_context_focus is not None else ()),
        ]
        avoid_axes = [
            _focus_route_axis(
                focus,
                physical_point=physical_point,
                route_lines=route_lines,
            )
            for focus in selection_anchors
        ]
        nonroad_focuses: list[_ContextDetailFocus] = []
        if primary_context_focus is not None:
            nonroad_focuses.append(primary_context_focus)
        for focus in _section_context_focuses(
            context["features"],
            physical_point=physical_point,
            route_lines=route_lines,
            maximum=4,
            avoid_axes=avoid_axes,
            excluded_kinds=frozenset({"road"}),
        ):
            if focus not in nonroad_focuses:
                nonroad_focuses.append(focus)

        terrain_focuses = (
            _terrain_detail_focuses(
                terrain,
                physical_point=physical_point,
                map_rect=map_rect,
                route_lines=route_lines,
                maximum=4,
                avoid_axes=avoid_axes,
            )
            if isinstance(terrain, dict)
            else []
        )
        section_focuses = _route_section_focuses(
            record,
            physical_point=physical_point,
            maximum=2,
            avoid_axes=avoid_axes,
        )
        terrain_or_section_focuses: list[_ContextDetailFocus] = []
        for focus in (*terrain_focuses, *section_focuses):
            if focus not in terrain_or_section_focuses:
                terrain_or_section_focuses.append(focus)

        if variant_id == "terrain-relief":
            # Relief plates reserve both the strongest context family that the
            # overview cannot show and factual local landform evidence.  The
            # context retry block is evaluated in the larger first inset: very
            # short sourced roads often fail in the smaller second inset,
            # whereas terrain windows retain useful geometry there.  Terrain
            # is still ahead of every unrelated retry and cannot be starved by
            # a road-only queue.
            append_unique_focuses(road_focuses if road_focuses else nonroad_focuses[:1])
            append_unique_focuses(terrain_or_section_focuses)
            append_unique_focuses(nonroad_focuses)
        else:
            # Map-led plates first make a sub-legible road printable when one
            # is actually needed, then prefer other context.  The factual
            # terrain/route-section fallback is deliberately inserted before
            # retries are truncated, preventing a road-only candidate queue.
            append_unique_focuses(road_focuses)
            append_unique_focuses(nonroad_focuses[:1])
            append_unique_focuses(terrain_or_section_focuses[:1])
            append_unique_focuses(nonroad_focuses[1:2])
            append_unique_focuses(terrain_or_section_focuses[1:])
            append_unique_focuses(nonroad_focuses[2:])

        if len(detail_focuses) < 2:
            broad_context_focus = _context_detail_focus(
                context["features"],
                need_road=True,
                need_water=True,
                need_landcover=True,
            )
            if broad_context_focus is not None:
                append_unique_focuses((broad_context_focus,))
        detail_focuses = detail_focuses[:8]
    detail_focus = detail_focuses[0] if detail_focuses else None
    secondary_detail_focus = detail_focuses[1] if len(detail_focuses) >= 2 else None
    detail_inset = (
        _context_detail_rect(
            map_rect,
            route_lines=route_lines,
            occupied_boxes=(*fixed_reservations, *terrain_reservations),
            emphasized=sectional_detail,
        )
        if detail_focus is not None
        else None
    )
    secondary_detail_inset = (
        _context_detail_rect(
            map_rect,
            route_lines=route_lines,
            occupied_boxes=(
                *fixed_reservations,
                *terrain_reservations,
                *((detail_inset,) if detail_inset is not None else ()),
            ),
            emphasized=sectional_detail,
        )
        if secondary_detail_focus is not None
        else None
    )
    contour_key = None
    placement_reservations = [*fixed_reservations, *terrain_reservations]
    if detail_inset is not None:
        placement_reservations.append(detail_inset)
    if secondary_detail_inset is not None:
        placement_reservations.append(secondary_detail_inset)
    if contour_key is not None:
        placement_reservations.append(contour_key.box)
    fixed_copy_exclusion = _label_exclusion_geometry(placement_reservations)
    road_copy_avoidance, road_copy_metadata = _road_label_avoidance(
        context["features"],
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
        fixed_exclusion=fixed_copy_exclusion,
        variant_id=variant_id,
    )
    if road_copy_metadata["selected_stroke_count"]:
        artwork.rendering_metadata["road_copy_reservation"] = road_copy_metadata
    green_copy_avoidance: BaseGeometry = Polygon()
    if not isinstance(context.get("landcover"), dict):
        green_copy_avoidance, green_copy_metadata = _narrow_green_label_avoidance(
            context["features"],
            physical_point=physical_point,
            map_rect=map_rect,
            fixed_exclusion=fixed_copy_exclusion,
        )
        if green_copy_metadata["selected_boundary_path_count"]:
            artwork.rendering_metadata["green_copy_reservation"] = (
                green_copy_metadata
            )
    copy_avoidance = make_valid(
        unary_union((road_copy_avoidance, green_copy_avoidance))
    )
    placements, omitted = _place_labels(
        label_features,
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
        reserved_boxes=placement_reservations,
        geography_avoidance=copy_avoidance,
    )
    label_boxes = [placement.box for placement in placements]
    label_boxes.extend(station_reservations)
    if north_arrow is not None:
        label_boxes.append(north_arrow.box)
    if contour_key is not None:
        label_boxes.append(contour_key.box)
    if detail_inset is not None:
        label_boxes.append(detail_inset)
    if secondary_detail_inset is not None:
        label_boxes.append(secondary_detail_inset)
    label_exclusion = _label_exclusion_geometry(label_boxes)
    exclusion = _context_exclusion_geometry(route_lines, label_boxes)
    fall_line_exclusion = _fall_line_exclusion_geometry(
        route_lines,
        label_boxes,
        water=water if isinstance(water, dict) else None,
        physical_point=physical_point,
        map_rect=map_rect,
    )
    west, south, east, north = (float(value) for value in context["extent"])
    extent_ring = [
        physical_point((west, south)),
        physical_point((east, south)),
        physical_point((east, north)),
        physical_point((west, north)),
        physical_point((west, south)),
    ]
    clip_edge_exclusion = LineString(extent_ring).buffer(
        0.12, cap_style="flat", join_style="mitre"
    )
    area_exclusion = make_valid(unary_union((exclusion, clip_edge_exclusion)))
    green_boundary_exclusion = make_valid(
        unary_union((label_exclusion, clip_edge_exclusion))
    )
    seed = int(record["backdrop"]["seed"])

    terrain_label_boxes: list[Rect] = []
    terrain_leader_paths: list[list[Point]] = []
    main_retain_all_contours = False
    if isinstance(terrain, dict):
        fall_line_rendering = _add_derived_terrain(
            relief_layer,
            index_layer=relief_index_layer,
            label_layer=relief_label_layer,
            terrain=terrain,
            map_rect=map_rect,
            physical_point=physical_point,
            exclusion=label_exclusion,
            fall_line_exclusion=fall_line_exclusion,
            attributes=attributes,
            variant_id=variant_id,
            contour_key=contour_key,
            protected_fall_line_boxes=terrain_reservations,
            label_obstacle_boxes=label_boxes,
            route_lines=route_lines,
            rendered_label_boxes=terrain_label_boxes,
            rendered_leader_paths=terrain_leader_paths,
            retain_all_contours=main_retain_all_contours,
        )
        artwork.rendering_metadata["terrain_contour_selection"] = copy.deepcopy(
            fall_line_rendering.get("contour_selection", {})
        )
        artwork.rendering_metadata["terrain_contour_hierarchy"] = copy.deepcopy(
            fall_line_rendering.get("contour_hierarchy", {})
        )
        if variant_id == "terrain-relief":
            artwork.rendering_metadata["terrain_fall_lines"] = fall_line_rendering
    landcover = context.get("landcover")
    if isinstance(landcover, dict):
        _add_derived_landcover(
            woodland_layer,
            landcover=landcover,
            map_rect=map_rect,
            physical_point=physical_point,
            area_exclusion=area_exclusion,
            boundary_exclusion=green_boundary_exclusion,
            attributes=attributes,
            seed=seed,
        )
    if isinstance(water, dict):
        _add_derived_water(
            water_layer,
            water=water,
            map_rect=map_rect,
            physical_point=physical_point,
            exclusion=exclusion,
            attributes=attributes,
        )

    overview_road_mm, overview_linear_water_mm = _add_stitched_linear_context(
        road_layer=road_layer,
        water_layer=water_layer,
        features=context["features"],
        physical_point=physical_point,
        map_rect=map_rect,
        route_lines=route_lines,
        label_exclusion=label_exclusion,
        attributes=attributes,
        variant_id=variant_id,
        include_hydro=not isinstance(water, dict),
    )

    marine_water_ids = {
        "gare-loch",
        "loch-etive",
        "loch-leven",
        "loch-linnhe",
        "loch-long",
    }
    has_real_terrain = isinstance(terrain, dict)
    has_systematic_landcover = isinstance(landcover, dict)
    has_systematic_water = isinstance(water, dict)
    anchored_symbol_points: dict[str, list[Point]] = {
        "woodland": [],
        "grass": [],
        "water": [],
    }
    for feature_index, feature in enumerate(context["features"], start=1):
        kind = str(feature["kind"])
        feature_id = str(feature["id"])
        feature_attributes = {
            **attributes,
            "data-feature-id": feature_id,
            "data-feature-kind": kind,
        }
        if feature.get("osm_type") is not None:
            feature_attributes["data-osm-element"] = (
                f"{feature['osm_type']}/{feature['osm_id']}"
            )
        if feature.get("source_url"):
            feature_attributes["data-source-url"] = str(feature["source_url"])
        if feature.get("distance_to_route_m") is not None:
            feature_attributes["data-distance-to-route-m"] = (
                f"{float(feature['distance_to_route_m']):g}"
            )

        point = physical_point(feature["point"])
        source_paths = (
            ()
            if kind in {"sea", "road", "river", "coast"}
            or (has_systematic_water and kind in {"coast", "river", "water"})
            else feature.get("paths", [])
        )
        area_geometry_emitted = False
        for path in source_paths:
            if kind in {"woodland", "grass"} and not has_systematic_landcover:
                for polygon in _page_polygon_parts(
                    path,
                    physical_point=physical_point,
                    map_rect=map_rect,
                    minimum_area_mm2=MIN_LINEAR_LANDCOVER_AREA_MM2,
                ):
                    emitted = _add_green_polygon(
                        woodland_layer,
                        polygon,
                        source_ref=str(feature["source_ref"]),
                        sequence=feature_index,
                        attributes=feature_attributes,
                        seed=seed,
                        semantic_class=kind,
                        exclusion=area_exclusion,
                        woodland_symbols=kind == "woodland",
                        boundary_exclusion=green_boundary_exclusion,
                    )
                    if emitted == 0 and kind == "woodland":
                        emitted = _add_source_anchored_woodland_symbol(
                            woodland_layer,
                            polygon,
                            map_rect=map_rect,
                            exclusion=area_exclusion,
                            source_ref=str(feature["source_ref"]),
                            sequence=feature_index,
                            attributes=feature_attributes,
                            semantic_class=kind,
                        )
                    elif emitted == 0 and kind == "grass":
                        anchor = polygon.representative_point()
                        symbol = _grass_tuft_strokes(
                            (float(anchor.x), float(anchor.y)), scale=0.72
                        )
                        if all(
                            map_rect.left + 0.4 <= point[0] <= map_rect.right - 0.4
                            and map_rect.top + 0.4 <= point[1] <= map_rect.bottom - 0.4
                            for stroke in symbol
                            for point in stroke
                        ) and not any(
                            not area_exclusion.is_empty
                            and LineString(stroke).intersects(area_exclusion)
                            for stroke in symbol
                        ):
                            for stroke in symbol:
                                woodland_layer.add(
                                    stroke,
                                    source_ref=str(feature["source_ref"]),
                                    role="source-anchored-grass-symbol",
                                    sequence=feature_index,
                                    attributes={
                                        **feature_attributes,
                                        "data-landcover-class": "grass",
                                        "data-area-rendering": (
                                            "source-anchored-symbol-no-area-claim-v1"
                                        ),
                                    },
                                )
                            emitted = len(symbol)
                    area_geometry_emitted = area_geometry_emitted or emitted > 0
            elif kind == "park" and feature.get("display_boundary") is True:
                for polygon in _page_polygon_parts(
                    path,
                    physical_point=physical_point,
                    map_rect=map_rect,
                ):
                    area_geometry_emitted = True
                    for boundary in _masked_boundary_strokes(
                        polygon, exclusion=area_exclusion
                    ):
                        for dash in _dash_strokes(boundary, dash_mm=1.35, gap_mm=1.0):
                            designation_layer.add(
                                dash,
                                source_ref=feature["source_ref"],
                                role="source-sampled-designation-boundary",
                                sequence=feature_index,
                                attributes={
                                    **feature_attributes,
                                    "data-area-rendering": (
                                        "designation-boundary-no-landcover-claim-v3"
                                    ),
                                },
                            )
            elif kind == "range" and not has_real_terrain:
                for polygon in _page_polygon_parts(
                    path,
                    physical_point=physical_point,
                    map_rect=map_rect,
                ):
                    area_geometry_emitted = True
                    for formline in _inward_formlines(
                        polygon, exclusion=area_exclusion
                    ):
                        relief_layer.add(
                            formline,
                            source_ref=feature["source_ref"],
                            role="stylized-relief-formline",
                            sequence=feature_index,
                            attributes={
                                **feature_attributes,
                                "data-relief-status": ("stylized-source-area-anchored"),
                            },
                        )
            elif kind == "water" and feature_id not in marine_water_ids:
                for polygon in _page_polygon_parts(
                    path,
                    physical_point=physical_point,
                    map_rect=map_rect,
                ):
                    emitted = 0
                    for boundary in _masked_boundary_strokes(
                        polygon, exclusion=area_exclusion
                    ):
                        water_layer.add(
                            boundary,
                            source_ref=feature["source_ref"],
                            role="source-sampled-lake-boundary",
                            sequence=feature_index,
                            attributes={
                                **feature_attributes,
                                "data-water-class": "inland",
                                "data-area-rendering": "single-source-shoreline-v4",
                            },
                        )
                        emitted += 1
                    area_geometry_emitted = area_geometry_emitted or emitted > 0
            elif kind in {"water", "coast", "river"}:
                marine_path = path
                if (
                    kind == "water"
                    and feature_id in marine_water_ids
                    and _closed_source_path(path)
                ):
                    marine_path = path[:-1]
                for stroke in _page_line_strokes(
                    marine_path,
                    physical_point=physical_point,
                    map_rect=map_rect,
                    # At continental A5 scales a truthful river may project to
                    # only a millimetre and coincide with the route.  Keep its
                    # exact centreline; the route is a later, wider pen pass.
                    exclusion=label_exclusion,
                    smoothing=True,
                ):
                    water_layer.add(
                        stroke,
                        source_ref=feature["source_ref"],
                        role=(
                            "source-sampled-marine-shoreline"
                            if feature_id in marine_water_ids
                            else _context_role(kind)
                        ),
                        sequence=feature_index,
                        attributes={
                            **feature_attributes,
                            "data-water-class": (
                                "marine" if feature_id in marine_water_ids else kind
                            ),
                            "data-smoothing-max-displacement-mm": "0.36",
                        },
                    )

        if (
            kind in {"woodland", "grass"}
            and not has_systematic_landcover
            and not area_geometry_emitted
            and all(
                math.hypot(point[0] - previous[0], point[1] - previous[1]) >= 4.0
                for previous in anchored_symbol_points[kind]
            )
        ):
            area_geometry_emitted = bool(
                _add_source_anchored_feature_symbol(
                    woodland_layer,
                    anchor=point,
                    map_rect=map_rect,
                    exclusion=area_exclusion,
                    source_ref=str(feature["source_ref"]),
                    sequence=feature_index,
                    attributes={
                        **feature_attributes,
                        "data-landcover-class": kind,
                    },
                    semantic_class=kind,
                )
            )
            if area_geometry_emitted:
                anchored_symbol_points[kind].append(point)
        elif (
            kind == "water"
            and not has_systematic_water
            and feature_id not in marine_water_ids
            and not area_geometry_emitted
            and all(
                math.hypot(point[0] - previous[0], point[1] - previous[1]) >= 4.0
                for previous in anchored_symbol_points["water"]
            )
        ):
            area_geometry_emitted = bool(
                _add_source_anchored_feature_symbol(
                    water_layer,
                    anchor=point,
                    map_rect=map_rect,
                    exclusion=area_exclusion,
                    source_ref=str(feature["source_ref"]),
                    sequence=feature_index,
                    attributes={
                        **feature_attributes,
                        "data-water-class": "inland",
                    },
                    semantic_class="water",
                )
            )
            if area_geometry_emitted:
                anchored_symbol_points["water"].append(point)
        if (
            not has_real_terrain
            and kind in {"peak", "range", "pass"}
            and not area_geometry_emitted
            and map_rect.left <= point[0] <= map_rect.right
            and map_rect.top <= point[1] <= map_rect.bottom
        ):
            fan_scale = 0.92 if kind == "range" else 0.62
            placed_symbol = _placed_ridge_symbol(
                point,
                scale=fan_scale,
                map_rect=map_rect,
                exclusion=label_exclusion,
            )
            if placed_symbol is not None:
                symbol, centre = placed_symbol
                for fan in symbol:
                    relief_layer.add(
                        fan,
                        source_ref=feature["source_ref"],
                        role="stylized-ridge-symbol",
                        sequence=feature_index,
                        attributes={
                            **feature_attributes,
                            "data-relief-status": "stylized-point-symbol",
                            "data-symbol-displacement-mm": (
                                f"{centre[0] - point[0]:.3f},{centre[1] - point[1]:.3f}"
                            ),
                        },
                    )

    if not has_systematic_landcover:
        artwork.rendering_metadata["green_context_retention"] = (
            _retain_source_anchored_green_symbols(
                woodland_layer,
                features=context["features"],
                physical_point=physical_point,
                map_rect=map_rect,
                route_lines=route_lines,
                area_exclusion=area_exclusion,
                attributes=attributes,
                variant_id=variant_id,
            )
        )

    if (
        variant_id == "detailed-map"
        and isinstance(terrain, dict)
        and not main_retain_all_contours
    ):
        overview_layers = (
            road_layer,
            water_layer,
            woodland_layer,
            relief_layer,
            relief_index_layer,
        )
        overview_geography_mm = sum(
            polyline_length_mm(item.points)
            for overview_layer in overview_layers
            for item in overview_layer.records
            if not item.attributes.get("data-context-view")
        )
        overview_density = overview_geography_mm / max(
            map_rect.width * map_rect.height,
            1e-9,
        )
        overview_equivalent_mm = sum(
            polyline_length_mm(item.points)
            * (
                PENS_BY_ID[CONTOUR_INDEX_PEN_ID].mark_width_mm
                / PENS_BY_ID[CONTOUR_MINOR_PEN_ID].mark_width_mm
                if overview_layer is relief_index_layer
                else 1.0
            )
            for overview_layer in overview_layers
            for item in overview_layer.records
            if not item.attributes.get("data-context-view")
        )
        overview_equivalent_density = overview_equivalent_mm / max(
            map_rect.width * map_rect.height,
            1e-9,
        )
        if overview_density < 0.075:
            terrain_derivation_id = str(terrain["derivation_id"])
            relief_layer.records[:] = [
                item
                for item in relief_layer.records
                if item.attributes.get("data-derivation-id") != terrain_derivation_id
            ]
            relief_index_layer.records[:] = [
                item
                for item in relief_index_layer.records
                if item.attributes.get("data-derivation-id") != terrain_derivation_id
            ]
            retry_rendering = _add_derived_terrain(
                relief_layer,
                index_layer=relief_index_layer,
                label_layer=relief_label_layer,
                terrain=terrain,
                map_rect=map_rect,
                physical_point=physical_point,
                exclusion=label_exclusion,
                fall_line_exclusion=fall_line_exclusion,
                attributes=attributes,
                variant_id=variant_id,
                contour_key=contour_key,
                protected_fall_line_boxes=terrain_reservations,
                label_obstacle_boxes=label_boxes,
                route_lines=route_lines,
                rendered_label_boxes=terrain_label_boxes,
                rendered_leader_paths=terrain_leader_paths,
                retain_all_contours=False,
                detailed_level_limit=8,
            )
            artwork.rendering_metadata["terrain_contour_selection"] = copy.deepcopy(
                retry_rendering.get("contour_selection", {})
            )
            artwork.rendering_metadata["terrain_contour_hierarchy"] = copy.deepcopy(
                retry_rendering.get("contour_hierarchy", {})
            )
            artwork.rendering_metadata["overview_terrain_policy"] = {
                "policy_id": "adaptive-expanded-contours-below-density-floor-v3",
                "preflight_geography_density_mm_per_mm2": round(
                    overview_density,
                    4,
                ),
                "activation_density_mm_per_mm2": 0.075,
                "expanded_maximum_level_count": 8,
                "selected_level_count": int(
                    retry_rendering.get("contour_selection", {}).get(
                        "selected_level_count",
                        0,
                    )
                ),
            }
        elif overview_equivalent_density > 0.18:
            terrain_derivation_id = str(terrain["derivation_id"])
            relief_layer.records[:] = [
                item
                for item in relief_layer.records
                if item.attributes.get("data-derivation-id") != terrain_derivation_id
            ]
            relief_index_layer.records[:] = [
                item
                for item in relief_index_layer.records
                if item.attributes.get("data-derivation-id") != terrain_derivation_id
            ]
            retry_rendering = _add_derived_terrain(
                relief_layer,
                index_layer=relief_index_layer,
                label_layer=relief_label_layer,
                terrain=terrain,
                map_rect=map_rect,
                physical_point=physical_point,
                exclusion=label_exclusion,
                fall_line_exclusion=fall_line_exclusion,
                attributes=attributes,
                variant_id=variant_id,
                contour_key=contour_key,
                protected_fall_line_boxes=terrain_reservations,
                label_obstacle_boxes=label_boxes,
                route_lines=route_lines,
                rendered_label_boxes=terrain_label_boxes,
                rendered_leader_paths=terrain_leader_paths,
                retain_all_contours=False,
                detailed_level_limit=4,
            )
            artwork.rendering_metadata["terrain_contour_selection"] = copy.deepcopy(
                retry_rendering.get("contour_selection", {})
            )
            artwork.rendering_metadata["terrain_contour_hierarchy"] = copy.deepcopy(
                retry_rendering.get("contour_hierarchy", {})
            )
            artwork.rendering_metadata["overview_terrain_policy"] = {
                "policy_id": "physical-index-density-cap-level-reduction-v1",
                "preflight_geography_density_mm_per_mm2": round(
                    overview_density,
                    4,
                ),
                "preflight_physical_equivalent_density_mm_per_mm2": round(
                    overview_equivalent_density,
                    4,
                ),
                "maximum_physical_equivalent_density_mm_per_mm2": 0.18,
                "reduced_maximum_level_count": 4,
                "selected_level_count": int(
                    retry_rendering.get("contour_selection", {}).get(
                        "selected_level_count",
                        0,
                    )
                ),
            }

    if variant_id == "terrain-relief" and isinstance(terrain, dict):
        map_area_mm2 = max(map_rect.width * map_rect.height, 1e-9)
        terrain_minor_mm = sum(
            polyline_length_mm(item.points)
            for item in relief_layer.records
            if item.role == "source-derived-dtm-contour"
            and not item.attributes.get("data-context-view")
        )
        terrain_index_mm = sum(
            polyline_length_mm(item.points)
            for item in relief_index_layer.records
            if item.role == "source-derived-dtm-contour"
            and not item.attributes.get("data-context-view")
        )
        index_width_ratio = (
            PENS_BY_ID[CONTOUR_INDEX_PEN_ID].mark_width_mm
            / PENS_BY_ID[CONTOUR_MINOR_PEN_ID].mark_width_mm
        )
        terrain_equivalent_mm = (
            terrain_minor_mm + index_width_ratio * terrain_index_mm
        )
        other_geography_mm = sum(
            polyline_length_mm(item.points)
            for overview_layer in (
                road_layer,
                water_layer,
                woodland_layer,
                relief_layer,
            )
            for item in overview_layer.records
            if not item.attributes.get("data-context-view")
            and item.role != "source-derived-dtm-contour"
        )
        preflight_equivalent_density = (
            terrain_equivalent_mm + other_geography_mm
        ) / map_area_mm2
        if preflight_equivalent_density > 0.35:
            # Leave 0.005 mm/mm2 of serialization/mask headroom below the hard
            # composition cap.  Only the complete optional minor-level
            # inventory is reconsidered; every true index remains mandatory.
            target_terrain_density = min(
                0.315,
                max(0.10, 0.345 - other_geography_mm / map_area_mm2),
            )
            terrain_derivation_id = str(terrain["derivation_id"])
            relief_layer.records[:] = [
                item
                for item in relief_layer.records
                if item.attributes.get("data-derivation-id") != terrain_derivation_id
            ]
            relief_index_layer.records[:] = [
                item
                for item in relief_index_layer.records
                if item.attributes.get("data-derivation-id") != terrain_derivation_id
            ]
            relief_label_layer.records[:] = [
                item
                for item in relief_label_layer.records
                if item.attributes.get("data-derivation-id") != terrain_derivation_id
            ]
            terrain_label_boxes.clear()
            terrain_leader_paths.clear()
            retry_rendering = _add_derived_terrain(
                relief_layer,
                index_layer=relief_index_layer,
                label_layer=relief_label_layer,
                terrain=terrain,
                map_rect=map_rect,
                physical_point=physical_point,
                exclusion=label_exclusion,
                fall_line_exclusion=fall_line_exclusion,
                attributes=attributes,
                variant_id=variant_id,
                contour_key=contour_key,
                protected_fall_line_boxes=terrain_reservations,
                label_obstacle_boxes=label_boxes,
                route_lines=route_lines,
                rendered_label_boxes=terrain_label_boxes,
                rendered_leader_paths=terrain_leader_paths,
                retain_all_contours=False,
                relief_equivalent_density_target=target_terrain_density,
            )
            artwork.rendering_metadata["terrain_contour_selection"] = copy.deepcopy(
                retry_rendering.get("contour_selection", {})
            )
            artwork.rendering_metadata["terrain_contour_hierarchy"] = copy.deepcopy(
                retry_rendering.get("contour_hierarchy", {})
            )
            artwork.rendering_metadata["terrain_fall_lines"] = retry_rendering
            artwork.rendering_metadata["overview_terrain_policy"] = {
                "policy_id": "relief-physical-index-composition-cap-v1",
                "preflight_physical_equivalent_density_mm_per_mm2": round(
                    preflight_equivalent_density,
                    4,
                ),
                "maximum_physical_equivalent_density_mm_per_mm2": 0.35,
                "target_terrain_equivalent_density_mm_per_mm2": round(
                    target_terrain_density,
                    4,
                ),
                "selected_level_count": int(
                    retry_rendering.get("contour_selection", {}).get(
                        "selected_level_count",
                        0,
                    )
                ),
            }

    if terrain_label_boxes:
        legibility_mask = _mask_geography_for_contour_labels(
            (
                relief_layer,
                relief_index_layer,
                road_layer,
                water_layer,
                woodland_layer,
                designation_layer,
            ),
            terrain_label_boxes,
        )
        artwork.rendering_metadata["contour_altitude_legibility_mask"] = legibility_mask
        green_retention = artwork.rendering_metadata.get("green_context_retention")
        if isinstance(green_retention, dict):
            _reconcile_green_context_after_contour_label_mask(
                woodland_layer,
                green_retention,
                role_statistics=legibility_mask["role_statistics"],
            )
        fall_line_summary = artwork.rendering_metadata.get("terrain_fall_lines")
        if variant_id == "terrain-relief" and isinstance(fall_line_summary, dict):
            _reconcile_fall_lines_after_contour_label_mask(
                relief_layer,
                fall_line_summary,
                role_statistics=legibility_mask["role_statistics"],
            )
        if legibility_mask["affected_record_count"]:
            artwork.notes = (
                *artwork.notes,
                "Overview geography crossing boxed contour-altitude copy was "
                "clipped only inside the disclosed 0.3 mm legibility mask; "
                "source geometry was never displaced.",
            )

    if isinstance(terrain, dict):
        _reconcile_rendered_contour_inventory(
            artwork,
            map_rect=map_rect,
            minor_layer=relief_layer,
            index_layer=relief_index_layer,
            label_layer=relief_label_layer,
        )

    detail_metadata: list[dict[str, Any]] = []
    missing_detail_count = 0
    rejected_detail_count = 0
    detail_focus_index = 0
    rendered_road_detail = False
    for detail_index, inset in enumerate(
        (detail_inset, secondary_detail_inset), start=1
    ):
        if inset is None:
            continue
        accepted_focus: _ContextDetailFocus | None = None
        metrics: tuple[float, float, float, float, float, float] | None = None
        while detail_focus_index < len(detail_focuses):
            candidate_focus = detail_focuses[detail_focus_index]
            detail_focus_index += 1
            if rendered_road_detail and candidate_focus.kind == "road":
                continue
            metrics = _add_context_detail_inset(
                artwork,
                context=context,
                record=record,
                inset=inset,
                focus=candidate_focus,
                road_layer=road_layer,
                water_layer=water_layer,
                woodland_layer=woodland_layer,
                terrain_layer=relief_layer,
                frame_layer=designation_layer,
                label_layer=label_layer,
                attributes=attributes,
                seed=seed + detail_index * 101 + detail_focus_index * 17,
                variant_id=variant_id,
            )
            if metrics is not None:
                accepted_focus = candidate_focus
                rendered_road_detail = (
                    rendered_road_detail or candidate_focus.kind == "road"
                )
                break
            rejected_detail_count += 1
        if accepted_focus is None or metrics is None:
            missing_detail_count += 1
            continue
        detail_metadata.append(
            {
                "status": "source-geometry-rendered",
                "orientation": "north-up",
                "focus_kind": accepted_focus.kind,
                "focus_label": accepted_focus.label,
                "overview_road_mm": round(overview_road_mm, 3),
                "overview_linear_water_mm": round(overview_linear_water_mm, 3),
                "detail_road_mm": round(metrics[0], 3),
                "detail_water_mm": round(metrics[1], 3),
                "detail_landcover_mm": round(metrics[2], 3),
                "detail_terrain_mm": round(metrics[3], 3),
                "detail_useful_geography_mm": round(sum(metrics[:4]), 3),
                "detail_useful_family_count": sum(
                    length >= 3.0 for length in metrics[:4]
                ),
                "detail_density_mm_per_mm2": round(metrics[4], 4),
                "detail_occupied_grid_fraction": round(metrics[5], 4),
                "quality_policy": "minimum-useful-source-geography-v1",
            }
        )
    if detail_metadata:
        artwork.rendering_metadata["context_detail_insets"] = detail_metadata
    if missing_detail_count:
        artwork.notes = (
            *artwork.notes,
            f"{missing_detail_count} local context detail panel(s) omitted: no "
            "candidate met the collision and minimum useful sourced-geography "
            "gates.",
        )
    if rejected_detail_count:
        artwork.notes = (
            *artwork.notes,
            f"{rejected_detail_count} local source window candidate(s) rejected "
            "before plotting because they were decoratively sparse.",
        )

    leader_paths, leader_omissions = _context_label_leaders(
        placements,
        map_rect=map_rect,
        route_lines=route_lines,
        reserved_boxes=(*placement_reservations, *terrain_label_boxes),
        existing_leaders=terrain_leader_paths,
    )
    marker_features = {str(feature["id"]): feature for feature in label_features}
    required_placements = {
        placement.feature_id: placement
        for placement in placements
        if marker_features.get(placement.feature_id, {}).get("label_required") is True
    }
    for required_id, placement in required_placements.items():
        if (
            placement.source_displacement_mm > 3.0
            and required_id not in leader_paths
        ):
            raise MapPlotterError(
                "Required context label "
                f"{required_id!r} needs a source leader, but no collision-safe "
                "leader could be routed."
            )
    station_reservation_geometry: BaseGeometry = (
        unary_union(
            [
                box(item.left, item.top, item.right, item.bottom)
                for item in station_reservations
            ]
        )
        if station_reservations
        else Polygon()
    )
    suppressed_station_marker_count = 0
    preserved_station_peak_marker_count = 0
    for placement in placements:
        feature = marker_features[placement.feature_id]
        marker_point = placement.point
        peak_source_leader: list[Point] | None = None
        marker_geometry: BaseGeometry = Polygon()
        if placement.kind in {"settlement", "hut", "pass"}:
            marker_geometry = GeometryPoint(placement.point).buffer(0.85)
        elif placement.kind == "peak":
            marker_geometry = LineString(
                [
                    (placement.point[0] - 0.95, placement.point[1] + 0.65),
                    placement.point,
                    (placement.point[0] + 0.95, placement.point[1] + 0.65),
                ]
            ).buffer(0.125, cap_style="round", join_style="round")
        marker_conflicts_with_station = (
            not marker_geometry.is_empty
            and not station_reservation_geometry.is_empty
            and marker_geometry.intersects(station_reservation_geometry)
        )
        # A station reservation is deliberately wider than its plotted glyph.
        # Suppress ordinary duplicate place markers inside it, but do not erase
        # an independently sourced, explicitly elevated summit.  Its triangle
        # remains centred on the exact source coordinate and records the narrow
        # exception in the SVG; the A/E equivalence count therefore continues
        # to describe only markers that were actually omitted.
        preserve_elevated_peak = (
            marker_conflicts_with_station
            and placement.kind == "peak"
            and feature.get("elevation_m") is not None
            and bool(str(feature.get("elevation_method") or ""))
            and bool(str(feature.get("elevation_source_ref") or ""))
        )
        if preserve_elevated_peak:
            route_geometry = unary_union(
                [LineString(line) for line in route_lines if len(line) >= 2]
            )
            foreign_copy = unary_union(
                [
                    box(item.box.left, item.box.top, item.box.right, item.box.bottom)
                    for item in placements
                    if item.feature_id != placement.feature_id
                ]
            )
            for offset_x, offset_y in (
                (1.4, -3.0),
                (-1.4, -3.0),
                (2.4, -1.8),
                (-2.4, -1.8),
                (2.4, 1.8),
                (-2.4, 1.8),
                (3.2, -2.6),
                (-3.2, -2.6),
                (3.2, 2.6),
                (-3.2, 2.6),
            ):
                candidate = (
                    placement.point[0] + offset_x,
                    placement.point[1] + offset_y,
                )
                candidate_geometry = LineString(
                    [
                        (candidate[0] - 0.95, candidate[1] + 0.65),
                        candidate,
                        (candidate[0] + 0.95, candidate[1] + 0.65),
                    ]
                ).buffer(0.125, cap_style="round", join_style="round")
                if (
                    candidate_geometry.bounds[0] < map_rect.left + 0.4
                    or candidate_geometry.bounds[2] > map_rect.right - 0.4
                    or candidate_geometry.bounds[1] < map_rect.top + 0.4
                    or candidate_geometry.bounds[3] > map_rect.bottom - 0.4
                    or candidate_geometry.intersects(station_reservation_geometry)
                    or (
                        not foreign_copy.is_empty
                        and candidate_geometry.intersects(foreign_copy)
                    )
                    or (
                        not route_geometry.is_empty
                        and candidate_geometry.intersects(route_geometry.buffer(0.35))
                    )
                ):
                    continue
                marker_point = candidate
                marker_geometry = candidate_geometry
                peak_source_leader = [placement.point, candidate]
                break
            else:
                # Fail closed if no short, copy-clear displacement exists.
                preserve_elevated_peak = False
        suppress_marker = marker_conflicts_with_station and not preserve_elevated_peak
        if suppress_marker and feature.get("label_required") is True:
            raise MapPlotterError(
                "Required context marker "
                f"{placement.feature_id!r} has no collision-safe page placement."
            )
        if suppress_marker:
            suppressed_station_marker_count += 1
        elif preserve_elevated_peak:
            preserved_station_peak_marker_count += 1
        if placement.kind in {"settlement", "hut", "pass"}:
            if not suppress_marker:
                marker_layer.add(
                    circle_stroke(placement.point, 0.72, segments=16),
                    source_ref=placement.source_ref,
                    role=f"{placement.kind}-marker",
                    attributes={
                        **attributes,
                        "data-feature-id": placement.feature_id,
                    },
                )
        elif placement.kind == "peak":
            elevation_attributes: dict[str, str] = {}
            if feature.get("elevation_m") is not None:
                elevation_attributes = {
                    "data-elevation-m": f"{float(feature['elevation_m']):g}",
                    "data-elevation-method": str(feature["elevation_method"]),
                    "data-elevation-source-ref": str(feature["elevation_source_ref"]),
                }
            if not suppress_marker:
                station_attributes = (
                    {
                        "data-station-reservation-exception": (
                            "exact-elevated-peak-marker-preserved-v1"
                        )
                    }
                    if preserve_elevated_peak
                    else {}
                )
                marker_layer.add(
                    [
                        (marker_point[0] - 0.95, marker_point[1] + 0.65),
                        marker_point,
                        (marker_point[0] + 0.95, marker_point[1] + 0.65),
                    ],
                    source_ref=placement.source_ref,
                    role="peak-marker",
                    attributes={
                        **attributes,
                        "data-feature-id": placement.feature_id,
                        **elevation_attributes,
                        **station_attributes,
                        "data-marker-source-x-mm": f"{placement.point[0]:.3f}",
                        "data-marker-source-y-mm": f"{placement.point[1]:.3f}",
                        "data-marker-displacement-mm": (
                            f"{math.hypot(marker_point[0] - placement.point[0], marker_point[1] - placement.point[1]):.3f}"
                        ),
                    },
                )
                if peak_source_leader is not None:
                    marker_layer.add(
                        peak_source_leader,
                        source_ref=placement.source_ref,
                        role="peak-marker-source-leader",
                        attributes={
                            **attributes,
                            "data-feature-id": placement.feature_id,
                            "data-marker-routing-policy": (
                                "short-station-clear-displacement-v1"
                            ),
                            **elevation_attributes,
                        },
                    )
        leader = leader_paths.get(placement.feature_id)
        if leader is not None:
            marker_layer.add(
                leader,
                source_ref=placement.source_ref,
                role="context-label-leader",
                attributes={
                    **attributes,
                    "data-feature-id": placement.feature_id,
                    "data-leader-routing-policy": (
                        "foreign-copy-route-and-leader-clearance-v1"
                    ),
                    "data-minimum-copy-clearance-mm": "0.3",
                    "data-hero-route-clearance-mm": (
                        f"{LEADER_HERO_ROUTE_CLEARANCE_MM:g}"
                    ),
                    "data-source-label-displacement-mm": (
                        f"{placement.source_displacement_mm:.3f}"
                    ),
                    "data-maximum-leader-length-mm": (
                        f"{placement.maximum_displacement_mm:g}"
                    ),
                    "data-leader-length-mm": f"{polyline_length_mm(leader):.3f}",
                },
            )
        if placement.kind == "range":
            cap_height = 2.8
        elif placement.kind in {"sea", "coast", "park"}:
            cap_height = 2.3
        else:
            cap_height = 2.0
        label_attributes = {
            **attributes,
            "data-label-id": placement.feature_id,
            "data-label-copy": plotter_copy(placement.text),
            "data-label-box": (
                f"{placement.box.x:.3f},{placement.box.y:.3f},"
                f"{placement.box.width:.3f},{placement.box.height:.3f}"
            ),
            "data-source-label-displacement-mm": (
                f"{placement.source_displacement_mm:.3f}"
            ),
            "data-maximum-source-label-displacement-mm": (
                f"{placement.maximum_displacement_mm:g}"
            ),
        }
        if placement.source_url:
            label_attributes["data-source-url"] = placement.source_url
        if feature.get("elevation_m") is not None:
            label_attributes.update(
                {
                    "data-elevation-m": f"{float(feature['elevation_m']):g}",
                    "data-elevation-method": str(feature["elevation_method"]),
                    "data-elevation-source-ref": str(feature["elevation_source_ref"]),
                }
            )
        label_role = {
            "river": "water-label",
            "coast": "sea-label",
        }.get(placement.kind, f"{placement.kind}-label")
        label_layer.add_many(
            text_strokes_fit(
                placement.text,
                x_mm=placement.x,
                y_mm=placement.y,
                preferred_cap_mm=cap_height,
                maximum_width_mm=max(placement.box.width - 0.7, 2.0),
                pen_id=label_layer.pen_id,
                anchor=placement.anchor,
                minimum_cap_mm=2.0,
            ),
            source_ref=placement.source_ref,
            role=label_role,
            attributes=label_attributes,
        )
    if station_reservations:
        artwork.rendering_metadata["chainage_station_reservations"] = {
            "count": len(station_reservations),
            "radius_mm": MAP_CHAINAGE_RESERVATION_RADIUS_MM,
            "context_markers_suppressed": suppressed_station_marker_count,
            "elevated_peak_markers_preserved_at_station": (
                preserved_station_peak_marker_count
            ),
            "policy": "exact-route-station-copy-clearance-v1",
        }
    if omitted:
        artwork.notes = (
            *artwork.notes,
            f"{omitted} lower-priority context label(s) omitted to prevent collisions.",
        )
    if leader_omissions:
        artwork.notes = (
            *artwork.notes,
            f"{len(leader_omissions)} displaced context label leader(s) omitted: "
            "no route clear of foreign copy, the hero route and earlier leaders.",
        )
    sourced_elevated_peaks = [
        feature
        for feature in label_features
        if feature.get("kind") == "peak" and feature.get("elevation_m") is not None
    ]
    if sourced_elevated_peaks and not any(
        placement.kind == "peak" for placement in placements
    ):
        artwork.notes = (
            *artwork.notes,
            "Source peak symbol/altitude omitted: no page-safe placement remained "
            "outside the route, compass, terrain reserves, and higher-priority "
            "route-control copy.",
        )

    rotation = float(context["rotation_deg"])
    if north_arrow is not None:
        north_attributes = {
            **attributes,
            "data-orientation-policy": "north-up",
            "data-north-is-page-up": "true",
        }
        marker_layer.add(
            [north_arrow.base, north_arrow.tip],
            source_ref=context["source_ref"],
            role="north-arrow",
            attributes=north_attributes,
        )
        marker_layer.add(
            [
                (north_arrow.tip[0] - 0.8, north_arrow.tip[1] + 1.7),
                north_arrow.tip,
                (north_arrow.tip[0] + 0.8, north_arrow.tip[1] + 1.7),
            ],
            source_ref=context["source_ref"],
            role="north-arrow-head",
            attributes=north_attributes,
        )
        add_text(
            marker_layer,
            "N",
            x_mm=north_arrow.tip[0],
            y_mm=north_arrow.tip[1] - 2.8,
            preferred_cap_mm=2.0,
            minimum_cap_mm=2.0,
            maximum_width_mm=2.5,
            anchor="middle",
            source_ref=context["source_ref"],
            role="north-label",
            attributes=north_attributes,
        )
    elif abs(rotation) >= 1e-9:
        angle = math.radians(rotation)
        north_end = (map_rect.right - 4.0, map_rect.top + 4.0)
        north_base = (
            north_end[0] + 5.0 * math.sin(angle),
            north_end[1] + 5.0 * math.cos(angle),
        )
        marker_layer.add(
            [north_base, north_end],
            source_ref=context["source_ref"],
            role="rotated-north-arrow",
            attributes=attributes,
        )
        marker_layer.add(
            [
                (north_end[0] - 0.8, north_end[1] + 1.7),
                north_end,
                (north_end[0] + 0.8, north_end[1] + 1.7),
            ],
            source_ref=context["source_ref"],
            role="rotated-north-arrow-head",
            attributes=attributes,
        )
        add_text(
            marker_layer,
            "N",
            x_mm=north_end[0],
            y_mm=north_end[1] - 2.8,
            preferred_cap_mm=2.0,
            minimum_cap_mm=2.0,
            maximum_width_mm=2.5,
            anchor="middle",
            source_ref=context["source_ref"],
            role="rotated-north-label",
            attributes=attributes,
        )


def _stylized_backdrop(
    artwork: PlateArtwork, map_rect: Rect, record: dict[str, Any]
) -> None:
    backdrop = record["backdrop"]
    seed = int(backdrop["seed"])
    contours = artwork.layer(
        "stylized_terrain", "Stylized terrain contours", "grey-0-25"
    )
    contour_count = 7
    for row in range(contour_count):
        points: list[Point] = []
        base = map_rect.y + map_rect.height * (row + 1) / (contour_count + 1)
        amplitude = map_rect.height * (0.025 + ((seed + row * 13) % 5) * 0.004)
        phase = ((seed * 17 + row * 29) % 360) * math.pi / 180.0
        for index in range(29):
            ratio = index / 28.0
            x = map_rect.x + ratio * map_rect.width
            wave = math.sin(ratio * math.pi * (2.0 + row % 3) + phase)
            secondary = math.sin(ratio * math.pi * 5.0 + phase / 2.0) * 0.28
            y = base + amplitude * (wave + secondary)
            y = min(map_rect.bottom - 1.0, max(map_rect.top + 1.0, y))
            points.append((x, y))
        contours.add(
            points,
            source_ref=record["route"]["source_ref"],
            role="stylized-contour",
            sequence=row + 1,
            attributes={"data-backdrop-status": "stylized"},
        )

    if backdrop.get("vegetation") != "stylized-hachures":
        return
    vegetation = artwork.layer(
        "stylized_vegetation", "Stylized vegetation hachures", "green-0-25"
    )
    for index in range(9):
        column = index % 3
        band = index // 3
        x = map_rect.x + 6.0 + column * (map_rect.width - 12.0) / 2.0
        x += ((seed + index * 7) % 9 - 4) * 0.32
        y = map_rect.y + 9.0 + band * min(17.0, (map_rect.height - 18.0) / 2.0)
        y += ((seed + index * 11) % 7 - 3) * 0.35
        size = 1.15
        vegetation.add(
            [(x - size, y + size), (x, y - size), (x + size, y + size)],
            source_ref=record["route"]["source_ref"],
            role="stylized-vegetation-hachure",
            sequence=index + 1,
            attributes={"data-backdrop-status": "stylized"},
        )


@dataclass(frozen=True)
class _ElevationProfileGeometry:
    chainage: RouteChainage
    stations: tuple[ChainageStation, ...]
    lines: tuple[tuple[Point, ...], ...]
    station_points: tuple[Point, ...]
    drawing: Rect
    minimum_m: float
    maximum_m: float
    source_point_count: int
    rendered_point_count: int
    generalization_policy_id: str
    generalization_tolerance_mm: float | None
    global_extrema_vertices_preserved: bool

    def __iter__(self):
        """Keep the former private ``(lines, min, max)`` test API compatible."""

        return iter((self.lines, self.minimum_m, self.maximum_m))


def _physically_generalized_profile_run(
    points: Sequence[Point],
    *,
    protected_indices: set[int],
    tolerance_mm: float = PROFILE_PHYSICAL_SIMPLIFICATION_TOLERANCE_MM,
) -> tuple[Point, ...]:
    """Apply physical RDP while retaining audited full-source extrema.

    Simplification is performed only after chainage and elevation have been
    resolved.  It therefore cannot move A--E stations or alter the full-source
    extrema used by the factual disclosure.  Splitting at the exact global
    minimum and maximum forces RDP to retain those two vertices as endpoints.
    """

    if len(points) < 3:
        return tuple(points)
    boundaries = sorted({0, len(points) - 1, *protected_indices})
    result: list[Point] = []
    for start, finish in zip(boundaries, boundaries[1:]):
        simplified = _simplify(points[start : finish + 1], tolerance_mm)
        if result and simplified and simplified[0] == result[-1]:
            simplified = simplified[1:]
        result.extend(simplified)
    return tuple(result)


def _profile_extrema_disclosure(
    route: dict[str, Any], profile: _ElevationProfileGeometry
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Return truthful elevation and distance-label disclosure metadata.

    The profile x geometry is always sampled along cumulative source chainage.
    When a published total exists, however, the visible A--E kilometre labels
    are proportional positions on that published total.  Keep those two claims
    explicit so a simplified source line is never presented as an exact route
    measurement.
    """

    evidence = route.get("elevation_extrema_evidence")
    official_distance_km = _official_route_distance_km(route)
    distance_label_basis = (
        "official-total-proportional-to-source-chainage-v1"
        if official_distance_km is not None
        else "measured-source-chainage-v1"
    )
    distance_caption = "PUBLISHED KM" if official_distance_km is not None else "MEASURED KM"
    if isinstance(evidence, dict) and evidence.get("status") == PROFILE_EXTREMA_EXACT_STATUS:
        minimum_m = float(evidence["minimum_m"])
        maximum_m = float(evidence["maximum_m"])
        status = PROFILE_EXTREMA_EXACT_STATUS
        policy_id = PROFILE_EXTREMA_EXACT_POLICY_ID
        caption = (
            f"SOURCE-VERIFIED ELEVATION {minimum_m:.0f}-{maximum_m:.0f} M"
            f" / {distance_caption}"
        )
        source_ref = str(evidence["source_ref"])
    else:
        minimum_m = profile.minimum_m
        maximum_m = profile.maximum_m
        status = PROFILE_EXTREMA_APPROXIMATE_STATUS
        policy_id = PROFILE_EXTREMA_APPROXIMATE_POLICY_ID
        elevation_caption = (
            "RECORDED ELEVATION"
            if route.get("profile_status") == "recorded-elevation-sampled"
            else "SAMPLED ELEVATION"
        )
        caption = (
            f"{elevation_caption} / APPROX {minimum_m:.0f}-{maximum_m:.0f} M"
            f" / {distance_caption}"
        )
        source_ref = str(route.get("elevation_source_ref") or route["source_ref"])
    attributes = {
        "data-elevation-extrema-status": status,
        "data-elevation-extrema-policy": policy_id,
        "data-elevation-extrema-source-ref": source_ref,
        "data-elevation-extrema-caption": caption,
        "data-distance-label-basis": distance_label_basis,
    }
    if official_distance_km is not None:
        attributes["data-official-total-distance-km"] = f"{official_distance_km:g}"
    metadata = {
        "status": status,
        "policy_id": policy_id,
        "source_ref": source_ref,
        "minimum_m": round(minimum_m, 3),
        "maximum_m": round(maximum_m, 3),
        "caption": caption,
        "distance_label_basis": distance_label_basis,
    }
    if official_distance_km is not None:
        metadata["official_total_distance_km"] = official_distance_km
    return caption, attributes, metadata


def _ordered_profile_segments(route: dict[str, Any]) -> list[list[Sequence[float]]]:
    """Orient the primary sourced segments from the named start to finish.

    OSM relation members are not guaranteed to arrive in walking order.  This
    nearest-endpoint traversal changes only member order and direction; it does
    not create connector geometry or count the gaps between source members.
    Explicit alternatives remain map context and never become profile chainage.
    """

    # A separately frozen profile inventory is already source-ordered.  Never
    # apply the legacy nearest-endpoint traversal or its early-finish break to
    # it: either operation can silently omit later official stages.
    profile_segments = route.get("profile_segments")
    if isinstance(profile_segments, list):
        return [
            list(segment["points"])
            for segment in profile_segments
            if isinstance(segment, dict)
            and segment.get("mode") == "walk"
            and len(segment.get("points", ())) >= 2
        ]

    candidates = [
        (index, list(segment["points"]))
        for index, segment in enumerate(route["segments"])
        if segment.get("mode") == "walk" and len(segment.get("points", ())) >= 2
    ]
    if not candidates:
        return []
    controls = route["controls"]
    start = next(control for control in controls if control["kind"] == "start")
    finish = next(control for control in controls if control["kind"] == "finish")
    start_lon_lat = (float(start["point"][0]), float(start["point"][1]))
    finish_lon_lat = (float(finish["point"][0]), float(finish["point"][1]))
    loop = geodesic_distance_m(start_lon_lat, finish_lon_lat) <= 50.0
    finish_endpoint_distance = min(
        geodesic_distance_m(
            finish_lon_lat,
            (float(endpoint[0]), float(endpoint[1])),
        )
        for _index, points in candidates
        for endpoint in (points[0], points[-1])
    )
    finish_tolerance_m = max(1_000.0, finish_endpoint_distance + 25.0)

    ordered: list[list[Sequence[float]]] = []
    remaining = list(candidates)
    current = start_lon_lat
    while remaining:
        choices: list[tuple[float, int, bool, int]] = []
        for remaining_index, (source_index, points) in enumerate(remaining):
            first = (float(points[0][0]), float(points[0][1]))
            last = (float(points[-1][0]), float(points[-1][1]))
            choices.extend(
                (
                    (
                        geodesic_distance_m(current, first),
                        source_index,
                        False,
                        remaining_index,
                    ),
                    (
                        geodesic_distance_m(current, last),
                        source_index,
                        True,
                        remaining_index,
                    ),
                )
            )
        _gap_m, _source_index, reverse, selected_index = min(choices)
        _original_index, selected = remaining.pop(selected_index)
        oriented = list(reversed(selected)) if reverse else selected
        ordered.append(oriented)
        current = (float(oriented[-1][0]), float(oriented[-1][1]))
        if (
            not loop
            and geodesic_distance_m(current, finish_lon_lat) <= finish_tolerance_m
        ):
            break
    return ordered


def _elevation_profile(
    route: dict[str, Any] | Sequence[dict[str, Any]], profile_rect: Rect
) -> _ElevationProfileGeometry | None:
    if not isinstance(route, dict):
        segments = list(route)
        primary = [
            segment
            for segment in segments
            if segment.get("mode") == "walk" and segment.get("points")
        ]
        if not primary:
            return None
        route = {
            "segments": segments,
            "controls": [
                {"kind": "start", "point": primary[0]["points"][0]},
                {"kind": "finish", "point": primary[-1]["points"][-1]},
            ],
        }
    ordered_segments = _ordered_profile_segments(route)
    if not ordered_segments:
        return None
    chainage = RouteChainage.from_segments(ordered_segments)
    extent = chainage.elevation_extent_m
    if chainage.is_degenerate or not chainage.has_complete_elevation or extent is None:
        return None
    minimum, maximum = extent
    elevation_span = max(maximum - minimum, 1.0)
    drawing = Rect(
        profile_rect.x + 1.2,
        profile_rect.y + 2.7,
        profile_rect.width - 2.4,
        5.5,
    )
    physical_runs: list[list[Point]] = []
    for run in chainage.profile_runs():
        physical = [
            (
                drawing.x + drawing.width * sample.route_fraction,
                drawing.bottom
                - drawing.height
                * (float(sample.elevation_m) - minimum)
                / elevation_span,
            )
            for sample in run
            if sample.elevation_m is not None
        ]
        if len(physical) >= 2:
            physical_runs.append(physical)
    if not physical_runs:
        return None
    source_point_count = sum(len(run) for run in physical_runs)
    baseline_lines = [tuple(_simplify(run, 0.04)) for run in physical_runs]
    baseline_rendered_point_count = sum(len(line) for line in baseline_lines)
    baseline_pitch_mm = drawing.width / max(
        baseline_rendered_point_count - len(baseline_lines),
        1,
    )
    generalize = baseline_pitch_mm < PROFILE_TARGET_PEN_WIDTH_MM
    if generalize:
        # Screen y is inverted: the source maximum elevation is the smallest
        # physical y, while the source minimum is the largest.  Protect one
        # exact occurrence of each across the complete source inventory.
        indexed_points = [
            (run_index, point_index, point)
            for run_index, run in enumerate(physical_runs)
            for point_index, point in enumerate(run)
        ]
        maximum_location = min(indexed_points, key=lambda item: item[2][1])
        minimum_location = max(indexed_points, key=lambda item: item[2][1])
        protected_by_run: dict[int, set[int]] = {}
        for run_index, point_index, _point in (maximum_location, minimum_location):
            protected_by_run.setdefault(run_index, set()).add(point_index)
        selected_tolerance_mm = PROFILE_PHYSICAL_SIMPLIFICATION_TOLERANCE_MM
        while True:
            lines = [
                _physically_generalized_profile_run(
                    run,
                    protected_indices=protected_by_run.get(run_index, set()),
                    tolerance_mm=selected_tolerance_mm,
                )
                for run_index, run in enumerate(physical_runs)
            ]
            rendered_count = sum(len(line) for line in lines)
            rendered_pitch_mm = drawing.width / max(
                rendered_count - len(lines),
                1,
            )
            if rendered_pitch_mm >= PROFILE_TARGET_PEN_WIDTH_MM:
                break
            if (
                selected_tolerance_mm
                >= PROFILE_PHYSICAL_MAXIMUM_TOLERANCE_MM - 1e-9
            ):
                raise MapPlotterError(
                    "Elevation profile remains denser than its 0.25 mm pen "
                    "after the maximum disclosed physical simplification."
                )
            selected_tolerance_mm = min(
                PROFILE_PHYSICAL_MAXIMUM_TOLERANCE_MM,
                round(
                    selected_tolerance_mm + PROFILE_PHYSICAL_TOLERANCE_STEP_MM,
                    3,
                ),
            )
        generalization_policy_id = PROFILE_PHYSICAL_GENERALIZATION_POLICY_ID
        generalization_tolerance_mm: float | None = selected_tolerance_mm
    else:
        lines = baseline_lines
        generalization_policy_id = "douglas-peucker-0.04mm-v1"
        generalization_tolerance_mm = None
    if not lines:
        return None
    stations = chainage.stations_at_fractions(
        (0.0, 0.25, 0.5, 0.75, 1.0),
        station_ids=("A", "B", "C", "D", "E"),
    )
    station_points = tuple(
        (
            drawing.x + drawing.width * station.route_fraction,
            drawing.bottom
            - drawing.height * (float(station.elevation_m) - minimum) / elevation_span,
        )
        for station in stations
        if station.elevation_m is not None
    )
    if len(station_points) != len(stations):
        return None
    rendered_points = [point for line in lines for point in line]
    global_extrema_vertices_preserved = (
        any(math.isclose(point[1], drawing.top, abs_tol=1e-9) for point in rendered_points)
        and any(
            math.isclose(point[1], drawing.bottom, abs_tol=1e-9)
            for point in rendered_points
        )
    )
    return _ElevationProfileGeometry(
        chainage=chainage,
        stations=stations,
        lines=tuple(lines),
        station_points=station_points,
        drawing=drawing,
        minimum_m=minimum,
        maximum_m=maximum,
        source_point_count=source_point_count,
        rendered_point_count=sum(len(line) for line in lines),
        generalization_policy_id=generalization_policy_id,
        generalization_tolerance_mm=generalization_tolerance_mm,
        global_extrema_vertices_preserved=global_extrema_vertices_preserved,
    )


def _chainage_attributes(
    station: ChainageStation, route: dict[str, Any]
) -> dict[str, str]:
    official_distance_km = _official_route_distance_km(route)
    displayed_distance_km = (
        official_distance_km * station.route_fraction
        if official_distance_km is not None
        else station.distance_km
    )
    attributes = {
        **station.as_svg_attributes(),
        "data-chainage-m": f"{station.distance_m:.3f}",
        "data-distance-km": f"{station.distance_km:.6f}",
        "data-measured-chainage-m": f"{station.distance_m:.3f}",
        "data-displayed-distance-m": f"{displayed_distance_km * 1_000.0:.3f}",
        "data-displayed-distance-km": f"{displayed_distance_km:.6f}",
        "data-chainage-basis": "source-geometry-cumulative-geodesic-v1",
        "data-distance-label-basis": (
            "official-total-proportional-to-source-chainage-v1"
            if official_distance_km is not None
            else "measured-source-chainage-v1"
        ),
        "data-route-source-ref": str(route["source_ref"]),
        "data-profile-status": str(route["profile_status"]),
    }
    if official_distance_km is not None:
        attributes["data-official-total-distance-km"] = f"{official_distance_km:g}"
    elevation_source_ref = route.get("elevation_source_ref")
    if elevation_source_ref:
        attributes["data-elevation-source-ref"] = str(elevation_source_ref)
    if route.get("elevation_method"):
        attributes["data-elevation-method"] = str(route["elevation_method"])
    if route.get("elevation_datum"):
        attributes["data-elevation-datum"] = str(route["elevation_datum"])
    return attributes


def _add_chainage_letter(
    layer: ArtworkLayer,
    station: ChainageStation,
    centre: Point,
    *,
    route: dict[str, Any],
    source_ref: str,
    role: str,
) -> None:
    add_text(
        layer,
        station.station_id,
        x_mm=centre[0],
        y_mm=centre[1] - 1.0,
        preferred_cap_mm=2.0,
        minimum_cap_mm=2.0,
        maximum_width_mm=2.0,
        anchor="middle",
        source_ref=source_ref,
        role=role,
        attributes=_chainage_attributes(station, route),
    )


def _profile_distance_copy(station: ChainageStation, route: dict[str, Any]) -> str:
    distance_km = float(
        _chainage_attributes(station, route)["data-displayed-distance-km"]
    )
    return f"{station.station_id} {distance_km:.1f} KM"


def _add_route_and_profile(
    artwork: PlateArtwork,
    record: dict[str, Any],
    map_rect: Rect,
    profile_rect: Rect,
    *,
    variant_id: str | None,
) -> None:
    route = record["route"]
    segments = route["segments"]
    route_rect = _route_rect(map_rect)
    context_spec = record.get("context")
    physical_segments, physical_point = _geographic_transform(
        segments,
        route_rect,
        extent=context_spec["extent"] if context_spec else None,
        rotation_deg=float(context_spec["rotation_deg"]) if context_spec else 0.0,
    )
    controls = route["controls"]
    start = next(control for control in controls if control["kind"] == "start")
    finish = next(control for control in controls if control["kind"] == "finish")
    profile_geometry = _elevation_profile(route, profile_rect)
    start_point = physical_point(start["point"])
    finish_point = physical_point(finish["point"])
    same_control = (
        math.hypot(start_point[0] - finish_point[0], start_point[1] - finish_point[1])
        < 1.5
    )
    station_page_points = (
        {
            station.station_id: physical_point(station.map_point)
            for station in profile_geometry.stations
        }
        if profile_geometry is not None
        else {}
    )
    station_by_id = (
        {station.station_id: station for station in profile_geometry.stations}
        if profile_geometry is not None
        else {}
    )
    reuse_start = (
        "A" in station_page_points
        and math.hypot(
            station_page_points["A"][0] - start_point[0],
            station_page_points["A"][1] - start_point[1],
        )
        <= 1.5
    )
    reuse_finish = (
        "E" in station_page_points
        and math.hypot(
            station_page_points["E"][0] - finish_point[0],
            station_page_points["E"][1] - finish_point[1],
        )
        <= 1.5
    )
    coincident_label_centres: dict[str, Point] = {}
    coincident_label_reservation: Rect | None = None
    if same_control and reuse_start and reuse_finish:
        coincident_label_centres, coincident_label_reservation = (
            _coincident_chainage_label_layout(start_point, map_rect)
        )
    chainage_station_box_list = [
        _map_chainage_reservation(point) for point in station_page_points.values()
    ]
    if coincident_label_reservation is not None:
        chainage_station_box_list.append(coincident_label_reservation)
    chainage_station_boxes = tuple(chainage_station_box_list)
    if context_spec:
        _add_context(
            artwork,
            map_rect,
            record,
            physical_point,
            physical_segments,
            variant_id=variant_id,
            chainage_station_boxes=chainage_station_boxes,
        )
    unique_station_page_points = (
        tuple(
            dict.fromkeys(
                (round(point[0], 9), round(point[1], 9))
                for point in station_page_points.values()
            )
        )
        if variant_id in HIKE_VARIANTS
        else ()
    )
    station_route_clearance: BaseGeometry = (
        unary_union(
            [
                GeometryPoint(point).buffer(
                    MAP_CHAINAGE_ROUTE_CLEARANCE_RADIUS_MM,
                    quad_segs=16,
                )
                for point in unique_station_page_points
            ]
        )
        if unique_station_page_points
        else Polygon()
    )
    if unique_station_page_points:
        artwork.rendering_metadata["chainage_route_clearance"] = {
            "station_anchor_count": len(unique_station_page_points),
            "radius_mm": MAP_CHAINAGE_ROUTE_CLEARANCE_RADIUS_MM,
            "policy": "exact-station-glyph-underprint-break-v1",
            "factual_anchor_displaced": False,
            "profile_parity_preserved": True,
        }
    hero = artwork.layer("hero_route", "Source-sampled walking route", "red-0-4")
    ferries = artwork.layer("ferry_segments", "Source ferry segments", "blue-0-25")
    omitted_short = 0
    for index, (segment, physical) in enumerate(
        zip(segments, physical_segments), start=1
    ):
        simplified = _simplify(physical, 0.04)
        attributes = {
            "data-segment-mode": segment["mode"],
            "data-geometry-status": route["geometry_status"],
            "data-navigation-status": route["navigation_status"],
        }
        if segment["mode"] == "alternate":
            attributes.update(
                {
                    "data-source-role": str(segment["source_role"]),
                    "data-osm-element": f"way/{segment['osm_way_id']}",
                }
            )
        if segment["mode"] == "ferry":
            for dash in _dash_strokes(simplified):
                ferries.add(
                    dash,
                    source_ref=segment["source_ref"],
                    role="ferry-connector",
                    sequence=index,
                    attributes=attributes,
                )
        elif segment["mode"] == "alternate":
            for dash in _alternate_dash_strokes(simplified):
                hero.add(
                    dash,
                    source_ref=segment["source_ref"],
                    role="source-sampled-alternate-route",
                    sequence=index,
                    attributes=attributes,
                )
        elif polyline_length_mm(simplified) + 1e-9 >= MIN_HERO_ROUTE_STROKE_MM:
            for route_stroke, offset_mm in _hero_route_strokes(simplified):
                visible_strokes = _hero_route_station_clearance_strokes(
                    route_stroke,
                    station_route_clearance,
                )
                for visible_stroke in visible_strokes:
                    hero.add(
                        visible_stroke,
                        source_ref=segment["source_ref"],
                        role=(
                            "source-sampled-route"
                            if abs(offset_mm) <= 1e-9
                            else "source-sampled-route-display-offset"
                        ),
                        sequence=index,
                        attributes={
                            **attributes,
                            "data-route-offset-mm": f"{offset_mm:g}",
                            "data-effective-route-width-mm": "0.96",
                            "data-map-chainage-clearance-policy": (
                                "exact-station-glyph-underprint-break-v1"
                            ),
                            "data-map-chainage-clearance-radius-mm": (
                                f"{MAP_CHAINAGE_ROUTE_CLEARANCE_RADIUS_MM:g}"
                            ),
                            "data-source-route-displaced": "false",
                        },
                    )
        else:
            omitted_short += 1

    annotation = artwork.layer("route_annotations", "Route annotations", "black-0-25")
    annotation.add(
        _diamond(start_point),
        source_ref=start["source_ref"],
        role="start-finish" if same_control else "start",
        attributes=(
            _chainage_attributes(station_by_id["A"], route) if reuse_start else None
        ),
    )
    if not same_control:
        annotation.add(
            _diamond(finish_point),
            source_ref=finish["source_ref"],
            role="finish",
            attributes=(
                _chainage_attributes(station_by_id["E"], route)
                if reuse_finish
                else None
            ),
        )

    reused_station_ids: set[str] = set()
    if reuse_start:
        reused_station_ids.add("A")
    if reuse_finish and not same_control:
        reused_station_ids.add("E")
    if same_control and reuse_start and reuse_finish:
        annotation.add(
            circle_stroke(start_point, 1.8, segments=24),
            source_ref=finish["source_ref"],
            role="map-chainage-station",
            attributes=_chainage_attributes(station_by_id["E"], route),
        )
        reused_station_ids.add("E")

    if profile_geometry is not None:
        for station in profile_geometry.stations:
            centre = station_page_points[station.station_id]
            if station.station_id not in reused_station_ids:
                annotation.add(
                    circle_stroke(centre, MAP_CHAINAGE_STATION_RADIUS_MM, segments=20),
                    source_ref=route["source_ref"],
                    role="map-chainage-station",
                    attributes=_chainage_attributes(station, route),
                )
            if station.station_id in coincident_label_centres:
                centre = coincident_label_centres[station.station_id]
                leader = _chainage_label_leader(
                    station_page_points[station.station_id], centre
                )
                if leader is not None:
                    annotation.add(
                        leader,
                        source_ref=str(route["source_ref"]),
                        role="map-chainage-label-leader",
                        attributes={
                            **_chainage_attributes(station, route),
                            "data-label-displacement-policy": (
                                "leader-to-coincident-loop-station-v1"
                            ),
                            "data-factual-anchor-x-mm": (
                                f"{station_page_points[station.station_id][0]:.3f}"
                            ),
                            "data-factual-anchor-y-mm": (
                                f"{station_page_points[station.station_id][1]:.3f}"
                            ),
                        },
                    )
            _add_chainage_letter(
                annotation,
                station,
                centre,
                route=route,
                source_ref=(
                    str(start["source_ref"])
                    if station.station_id == "A" and reuse_start
                    else str(finish["source_ref"])
                    if station.station_id == "E" and reuse_finish
                    else str(route["source_ref"])
                ),
                role="map-chainage-label",
            )
    if context_spec:
        pass
    elif same_control:
        control_copy = f"LOOP / {start['name']}"
        add_text(
            annotation,
            control_copy,
            x_mm=map_rect.centre[0],
            y_mm=map_rect.y + 0.4,
            preferred_cap_mm=2.0,
            minimum_cap_mm=2.0,
            maximum_width_mm=map_rect.width - 2.0,
            anchor="middle",
            source_ref=start["source_ref"],
            role="control-label",
        )
    else:
        add_text(
            annotation,
            f"START / {start['name']}",
            x_mm=map_rect.x + 0.5,
            y_mm=map_rect.y + 0.4,
            preferred_cap_mm=2.0,
            minimum_cap_mm=2.0,
            maximum_width_mm=map_rect.width * 0.48,
            source_ref=start["source_ref"],
            role="control-label",
        )
        add_text(
            annotation,
            f"FINISH / {finish['name']}",
            x_mm=map_rect.right - 0.5,
            y_mm=map_rect.y + 0.4,
            preferred_cap_mm=2.0,
            minimum_cap_mm=2.0,
            maximum_width_mm=map_rect.width * 0.48,
            anchor="end",
            source_ref=finish["source_ref"],
            role="control-label",
        )

    if profile_geometry is not None:
        elevation_source_ref = str(
            route.get("elevation_source_ref") or route["source_ref"]
        )
        profile_caption, extrema_attributes, extrema_metadata = (
            _profile_extrema_disclosure(route, profile_geometry)
        )
        artwork.rendering_metadata["profile_extrema_disclosure"] = extrema_metadata
        profile_generalization_metadata = {
            "policy_id": profile_geometry.generalization_policy_id,
            "target_pen_width_mm": PROFILE_TARGET_PEN_WIDTH_MM,
            "physical_simplification_tolerance_mm": (
                profile_geometry.generalization_tolerance_mm
            ),
            "source_point_count": profile_geometry.source_point_count,
            "rendered_vertex_count": profile_geometry.rendered_point_count,
            "rendered_vertex_density_per_mm": round(
                profile_geometry.rendered_point_count
                / max(profile_geometry.drawing.width, 1e-9),
                4,
            ),
            "average_vertex_pitch_mm": round(
                profile_geometry.drawing.width
                / max(
                    profile_geometry.rendered_point_count
                    - len(profile_geometry.lines),
                    1,
                ),
                4,
            ),
            "chainage_station_basis": "complete-source-inventory-before-generalization-v1",
            "extrema_basis": "complete-source-inventory-before-generalization-v1",
            "global_extrema_vertices_preserved": (
                profile_geometry.global_extrema_vertices_preserved
            ),
        }
        artwork.rendering_metadata["profile_physical_generalization"] = (
            profile_generalization_metadata
        )
        profile_attributes = {
            "data-profile-status": route["profile_status"],
            "data-distance-axis": "source-geometry-cumulative-geodesic-v1",
            "data-measured-distance-m": f"{profile_geometry.chainage.total_distance_m:.3f}",
            "data-elevation-min-m": f"{profile_geometry.minimum_m:.3f}",
            "data-elevation-max-m": f"{profile_geometry.maximum_m:.3f}",
            "data-profile-generalization-policy": (
                profile_geometry.generalization_policy_id
            ),
            "data-profile-source-point-count": str(
                profile_geometry.source_point_count
            ),
            "data-profile-rendered-vertex-count": str(
                profile_geometry.rendered_point_count
            ),
            "data-profile-average-vertex-pitch-mm": (
                f"{profile_geometry.drawing.width / max(profile_geometry.rendered_point_count - len(profile_geometry.lines), 1):.4f}"
            ),
            "data-profile-target-pen-width-mm": f"{PROFILE_TARGET_PEN_WIDTH_MM:g}",
            "data-profile-extrema-basis": (
                "complete-source-inventory-before-generalization-v1"
            ),
            "data-profile-global-extrema-vertices-preserved": (
                str(profile_geometry.global_extrema_vertices_preserved).lower()
            ),
            **extrema_attributes,
        }
        if profile_geometry.generalization_tolerance_mm is not None:
            profile_attributes["data-profile-simplification-tolerance-mm"] = (
                f"{profile_geometry.generalization_tolerance_mm:g}"
            )
        if route.get("elevation_method"):
            profile_attributes["data-elevation-method"] = str(route["elevation_method"])
        if route.get("elevation_datum"):
            profile_attributes["data-elevation-datum"] = str(route["elevation_datum"])
        profile_guides = artwork.layer(
            "profile_guides", "Unboxed elevation profile guides", "grey-0-25"
        )
        profile_guides.add(
            [
                (profile_geometry.drawing.left, profile_geometry.drawing.bottom),
                (profile_geometry.drawing.right, profile_geometry.drawing.bottom),
            ],
            source_ref=elevation_source_ref,
            role="profile-baseline",
            attributes=profile_attributes,
        )
        for index, line in enumerate(profile_geometry.lines, start=1):
            if polyline_length_mm(line) >= 0.75:
                annotation.add(
                    line,
                    source_ref=elevation_source_ref,
                    role="source-elevation-profile",
                    sequence=index,
                    attributes=profile_attributes,
                )
        add_text(
            annotation,
            profile_caption,
            x_mm=profile_rect.centre[0],
            y_mm=profile_rect.y,
            preferred_cap_mm=2.0,
            minimum_cap_mm=2.0,
            maximum_width_mm=profile_rect.width - 2.4,
            anchor="middle",
            source_ref=elevation_source_ref,
            role="profile-status",
            attributes=profile_attributes,
        )
        label_width = max(7.0, profile_geometry.drawing.width / 5.0 - 0.5)
        for station, point in zip(
            profile_geometry.stations, profile_geometry.station_points
        ):
            station_attributes = _chainage_attributes(station, route)
            profile_guides.add(
                [
                    (point[0], profile_geometry.drawing.bottom),
                    (point[0], profile_geometry.drawing.bottom + 0.85),
                ],
                source_ref=elevation_source_ref,
                role="profile-chainage-tick",
                attributes=station_attributes,
            )
            annotation.add(
                circle_stroke(point, 0.43, segments=16),
                source_ref=elevation_source_ref,
                role="profile-chainage-station",
                attributes=station_attributes,
            )
            anchor = (
                "start"
                if station.station_id == "A"
                else "end"
                if station.station_id == "E"
                else "middle"
            )
            add_text(
                annotation,
                _profile_distance_copy(station, route),
                x_mm=point[0],
                y_mm=profile_rect.bottom - 2.0,
                preferred_cap_mm=2.0,
                minimum_cap_mm=2.0,
                maximum_width_mm=label_width,
                anchor=anchor,
                source_ref=elevation_source_ref,
                role="profile-chainage-label",
                attributes=station_attributes,
            )
    if omitted_short:
        artwork.notes = (
            *artwork.notes,
            f"{omitted_short} source fragment(s) fell below the physical "
            f"{MIN_HERO_ROUTE_STROKE_MM:g} mm route floor.",
        )


def _build_record(
    record: dict[str, Any],
    format_id: str,
    *,
    variant_id: str | None = None,
) -> PlateArtwork:
    context = context_for(format_id)
    if context.plate["sheet"] not in {"A5", "A4", "A3"}:
        raise MapPlotterError("Hiking route plates require an A-series format.")
    details = list(record["details"])
    if variant_id is not None:
        variant_copy = {
            "detailed-map": "DETAILED MAP / NORTH UP",
            "terrain-relief": "TERRAIN RELIEF / NORTH UP",
        }[variant_id]
        detail_index = next(
            (
                index
                for index, line in enumerate(details)
                if "NORTH UP" in str(line).upper()
            ),
            1 if len(details) >= 2 else len(details),
        )
        if detail_index < len(details):
            details[detail_index] = variant_copy
        else:
            details.append(variant_copy)
    variant_credit_lines = record.get("variant_credit_lines")
    credit_line = (
        str(variant_credit_lines[variant_id])
        if variant_id is not None and isinstance(variant_credit_lines, dict)
        else str(record["credit_line"])
    )
    artwork = PlateArtwork(
        subject_id=str(record.get("subject_id", record["id"])),
        domain="hikes",
        subject_kind=SUBJECT_KIND,
        title=record["title"],
        subtitle=record["subtitle"],
        details=tuple(details),
        credit_line=credit_line,
        scale_status=record["scale_status"],
        evidence_status=record["evidence_status"],
        rights_status=record["rights_status"],
        sources=tuple(copy.deepcopy(record["sources"])),
        context=context,
        layers=[],
        variant_id=variant_id,
        pen_order=HIKE_PENS,
        rendering_preset=(
            f"hiking-{variant_id}-a5-v1"
            if variant_id is not None
            else "hiking-map-a5-v2"
        ),
        notes=tuple(record["notes"]),
        catalog_record=copy.deepcopy(record),
        rendering_metadata=(
            {
                "hiking_variant": variant_id,
                "orientation_policy": "north-up",
                "north_is_page_up": True,
            }
            if variant_id is not None
            else {}
        ),
    )
    profile_segments = record["route"].get(
        "profile_segments", record["route"]["segments"]
    )
    has_profile = all(
        len(point) == 3
        for segment in profile_segments
        for point in segment["points"]
    )
    map_rect, profile_rect = _layout(context.field, has_profile=has_profile)
    if "context" not in record:
        _stylized_backdrop(artwork, map_rect, record)
    _add_route_and_profile(
        artwork,
        record,
        map_rect,
        profile_rect,
        variant_id=variant_id,
    )
    return artwork


def build_hike_plate(
    record: dict[str, Any],
    format_id: str = "a5-portrait",
    *,
    variant_id: str | None = None,
) -> PlateArtwork:
    """Build one editable, source-labelled hiking route plate."""

    if variant_id is not None and variant_id not in HIKE_VARIANTS:
        raise MapPlotterError(
            f"Unknown hiking variant {variant_id!r}; choose {', '.join(HIKE_VARIANTS)}."
        )
    prepared = copy.deepcopy(record)
    if variant_id is not None:
        source_rotation = float(prepared["context"].get("rotation_deg", 0.0))
        prepared["context"]["rotation_deg"] = 0.0
        prepared["context"]["orientation_status"] = "north-up"
        if abs(source_rotation) >= 1e-9:
            for terrain_key in ("terrain", "relief_terrain"):
                terrain = prepared["context"].get(terrain_key)
                if isinstance(terrain, dict):
                    terrain["relief_strokes"] = []
                    terrain["elevation_masks"] = []
    validated = copy.deepcopy(_validate_record(prepared))
    return _build_record(validated, format_id, variant_id=variant_id)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str | None:
    if not tag.startswith("{"):
        return None
    return tag[1:].split("}", 1)[0]


def _point_child_text(node: ET.Element, name: str) -> str | None:
    for child in node:
        if _local_name(child.tag) == name:
            return child.text
    return None


def _parse_gpx_point(node: ET.Element, label: str) -> tuple[GeoPoint, bool]:
    try:
        longitude = float(node.attrib["lon"])
        latitude = float(node.attrib["lat"])
    except (KeyError, ValueError) as exc:
        raise MapPlotterError(f"{label} needs numeric lon/lat attributes.") from exc
    if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
        raise MapPlotterError(f"{label} longitude is non-finite or out of range.")
    if not math.isfinite(latitude) or not -90.0 <= latitude <= 90.0:
        raise MapPlotterError(f"{label} latitude is non-finite or out of range.")
    elevation_text = _point_child_text(node, "ele")
    elevation = None
    if elevation_text is not None:
        try:
            elevation = float(elevation_text)
        except ValueError as exc:
            raise MapPlotterError(f"{label} has invalid elevation.") from exc
        if not math.isfinite(elevation) or not -12_000.0 <= elevation <= 100_000.0:
            raise MapPlotterError(f"{label} elevation is non-finite or out of range.")
    time_text = _point_child_text(node, "time")
    has_time = time_text is not None
    if time_text is not None:
        try:
            datetime.fromisoformat(time_text.strip().replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise MapPlotterError(f"{label} has invalid ISO-8601 time.") from exc
    return (longitude, latitude, elevation), has_time


def _clean_gpx_segment(points: Sequence[GeoPoint], label: str) -> list[GeoPoint]:
    cleaned: list[GeoPoint] = []
    for point in points:
        if cleaned and point[:2] == cleaned[-1][:2]:
            if cleaned[-1][2] is None and point[2] is not None:
                cleaned[-1] = point
            continue
        cleaned.append(point)
    if len(cleaned) < 2:
        raise MapPlotterError(f"{label} is degenerate after duplicate removal.")
    length = 0.0
    for first, second in zip(cleaned, cleaned[1:]):
        length += _haversine_m(first, second)
    if length < 1.0:
        raise MapPlotterError(f"{label} is shorter than the one-metre input floor.")
    return cleaned


def _parse_gpx(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise MapPlotterError(f"Could not read GPX {path}: {exc}") from exc
    if not path.is_file():
        raise MapPlotterError(f"GPX path is not a regular file: {path}")
    if stat.st_size <= 0 or stat.st_size > MAX_GPX_BYTES:
        raise MapPlotterError(f"GPX must be between 1 byte and {MAX_GPX_BYTES} bytes.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MapPlotterError(f"Could not read GPX {path}: {exc}") from exc
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise MapPlotterError("GPX document types and entities are forbidden.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MapPlotterError(f"Malformed GPX XML: {exc}") from exc
    if _local_name(root.tag) != "gpx":
        raise MapPlotterError("Document root must be gpx.")
    namespace = _namespace(root.tag)
    declared_version = root.get("version")
    namespace_version = GPX_NAMESPACES.get(namespace or "")
    if declared_version not in {"1.0", "1.1"}:
        raise MapPlotterError("Only GPX 1.0 and 1.1 are supported.")
    if namespace_version is not None and namespace_version != declared_version:
        raise MapPlotterError("GPX namespace and version disagree.")
    if namespace is not None and namespace not in GPX_NAMESPACES:
        raise MapPlotterError(f"Unsupported GPX namespace {namespace!r}.")

    segments: list[dict[str, Any]] = []
    point_count = 0
    time_count = 0
    track_number = 0
    route_number = 0
    for child in root:
        kind = _local_name(child.tag)
        if kind == "trk":
            track_number += 1
            segment_number = 0
            for track_segment in child:
                if _local_name(track_segment.tag) != "trkseg":
                    continue
                segment_number += 1
                parsed: list[GeoPoint] = []
                for point_node in track_segment:
                    if _local_name(point_node.tag) != "trkpt":
                        continue
                    point, has_time = _parse_gpx_point(
                        point_node,
                        f"track {track_number} segment {segment_number} point {len(parsed) + 1}",
                    )
                    parsed.append(point)
                    time_count += int(has_time)
                    point_count += 1
                    if point_count > MAX_GPX_POINTS:
                        raise MapPlotterError(
                            f"GPX exceeds the {MAX_GPX_POINTS} point limit."
                        )
                if parsed:
                    cleaned = _clean_gpx_segment(
                        parsed, f"track {track_number} segment {segment_number}"
                    )
                    segments.append(
                        {
                            "id": f"trk-{track_number:03d}-seg-{segment_number:03d}",
                            "mode": "walk",
                            "source_ref": "customer-gpx",
                            "points": [
                                [point[0], point[1]]
                                if point[2] is None
                                else [point[0], point[1], point[2]]
                                for point in cleaned
                            ],
                        }
                    )
        elif kind == "rte":
            route_number += 1
            parsed = []
            for point_node in child:
                if _local_name(point_node.tag) != "rtept":
                    continue
                point, has_time = _parse_gpx_point(
                    point_node, f"route {route_number} point {len(parsed) + 1}"
                )
                parsed.append(point)
                time_count += int(has_time)
                point_count += 1
                if point_count > MAX_GPX_POINTS:
                    raise MapPlotterError(
                        f"GPX exceeds the {MAX_GPX_POINTS} point limit."
                    )
            if parsed:
                cleaned = _clean_gpx_segment(parsed, f"route {route_number}")
                segments.append(
                    {
                        "id": f"rte-{route_number:03d}",
                        "mode": "walk",
                        "source_ref": "customer-gpx",
                        "points": [
                            [point[0], point[1]]
                            if point[2] is None
                            else [point[0], point[1], point[2]]
                            for point in cleaned
                        ],
                    }
                )
        if len(segments) > MAX_GPX_SEGMENTS:
            raise MapPlotterError(f"GPX exceeds the {MAX_GPX_SEGMENTS} segment limit.")
    if not segments:
        raise MapPlotterError("GPX contains no non-degenerate trkseg or rte geometry.")
    retained_points = sum(len(segment["points"]) for segment in segments)
    elevation_points = sum(
        len(point) == 3 for segment in segments for point in segment["points"]
    )
    return segments, {
        "version": declared_version,
        "input_point_count": point_count,
        "retained_point_count": retained_points,
        "segment_count": len(segments),
        "elevation_point_count": elevation_points,
        "time_point_count": time_count,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_hike_from_gpx(
    path: Path,
    *,
    title: str,
    subtitle: str = "",
    format_id: str = "a5-portrait",
) -> PlateArtwork:
    """Build a local GPX route plate without joining source segments.

    Timestamps are validated but intentionally omitted from the artwork and
    manifest-facing catalog record.  The caller still needs to establish that
    the GPX may be reproduced commercially.
    """

    path = Path(path)
    if not isinstance(title, str) or not title.strip():
        raise MapPlotterError("A custom GPX plate needs a non-empty title.")
    if not isinstance(subtitle, str):
        raise MapPlotterError("GPX subtitle must be text.")
    segments, summary = _parse_gpx(path)
    distance_m = sum(
        _haversine_m(first, second)
        for segment in segments
        for first, second in zip(segment["points"], segment["points"][1:])
    )
    point_total = summary["retained_point_count"]
    elevation_total = summary["elevation_point_count"]
    if elevation_total == point_total:
        profile_status = "recorded-elevation-sampled"
        profile_copy = "RECORDED GPX ELEVATION / STYLIZED TERRAIN"
    elif elevation_total:
        profile_status = "partial-elevation-not-rendered"
        profile_copy = "PARTIAL ELEVATION OMITTED / STYLIZED TERRAIN"
    else:
        profile_status = "not-embedded"
        profile_copy = "NO ELEVATION / STYLIZED TERRAIN"
    first = segments[0]["points"][0][:2]
    finish = segments[-1]["points"][-1][:2]
    source = {
        "id": "customer-gpx",
        "publisher": "User-supplied local GPX",
        "url": "local-gpx:private-input",
        "license": "user-rights-declaration-required",
        "attribution": "Customer GPX; commercial rights not verified",
        "use": "local route and optional recorded elevation",
        "sha256": _sha256(path),
        "gpx_version": summary["version"],
    }
    record = {
        "id": f"GPX-{source['sha256'][:12].upper()}",
        "subject_kind": SUBJECT_KIND,
        "title": title.strip(),
        "subtitle": subtitle.strip() or "CUSTOM GPX / SOURCE-SAMPLED",
        "details": [
            f"{distance_m / 1000.0:.1f} KM / {len(segments)} SOURCE SEGMENTS",
            profile_copy,
            "ARTWORK / NOT FOR NAVIGATION",
        ],
        "credit_line": "CUSTOM GPX / LOCAL SOURCE / RIGHTS REVIEW",
        "sources": [source],
        "route": {
            "geometry_status": "source-sampled-not-navigational",
            "navigation_status": "artwork-not-for-navigation",
            "coordinate_order": "lon-lat-ele-optional",
            "source_ref": "customer-gpx",
            "segments": segments,
            "controls": [
                {
                    "kind": "start",
                    "name": "GPX START",
                    "point": first,
                    "source_ref": "customer-gpx",
                },
                {
                    "kind": "finish",
                    "name": "GPX FINISH",
                    "point": finish,
                    "source_ref": "customer-gpx",
                },
            ],
            "profile_status": profile_status,
            "computed_distance_km": round(distance_m / 1000.0, 3),
        },
        "backdrop": {
            "status": "stylized",
            "terrain": "stylized-contour-lines",
            "vegetation": "stylized-hachures",
            "water": "none",
            "seed": int(source["sha256"][:8], 16) % 997,
        },
        "composition": {
            "pen_plan": PEN_PLAN_ID,
            "recommended_crs": "local-equirectangular-artwork",
        },
        "scale_status": "source-sampled-local-equirectangular",
        "evidence_status": "customer-gpx",
        "rights_status": "user-rights-unverified",
        "notes": [
            "GPX segments are preserved and never bridged automatically.",
            "Times were validated and stripped from artwork metadata.",
            (
                f"Input points {summary['input_point_count']}; retained "
                f"{summary['retained_point_count']}; time values stripped "
                f"{summary['time_point_count']}."
            ),
        ],
        "gpx_import_summary": summary,
    }
    return _build_record(record, format_id)
