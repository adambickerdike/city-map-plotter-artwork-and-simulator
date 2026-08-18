#!/usr/bin/env python3
"""Apply factual source precedence to the frozen forty-route hiking release.

The global terrain pass intentionally makes a complete, offline-renderable
catalog, but it cannot outrank route-embedded elevation, national terrain, or
an explicit ``ele`` tag on a named summit.  This deterministic post-processing
stage restores those stronger sources without re-querying the network.

Only elevation ordinates may move.  Segment IDs, modes, point counts and every
longitude/latitude pair are checked before any source elevation is restored.
The release water and landcover objects are never replaced.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn, Sequence

from city_map_plotter import hike_plates


RELEASE_ID = "hike-plates-release-v1"
EXPANSION_ID = "hike-plates-expansion-v1"
PEAK_ELEVATIONS_ID = "hike-explicit-elevations-v1"
POLICY_ID = "hiking-factual-source-precedence-v1"
RELIEF_TERRAIN_POLICY_ID = "hiking-relief-terrain-density-selection-v2"
GLOBAL_TERRAIN_SOURCE_PREFIX = "aws-mapzen-terrarium-"
OSM_PRINT_CREDIT = (
    "© OPENSTREETMAP / OPENSTREETMAP.ORG/COPYRIGHT"
)
OSM_PRINT_URL_PATTERN = re.compile(
    r"(?:(?:https?://)?(?:www\.)?)openstreetmap\.org/copyright/?",
    re.IGNORECASE,
)
OSM_CONTRIBUTORS_PATTERN = re.compile(
    r"(?:©\s*)?(?:openstreetmap(?:\s+contributors)?|osm\s+contributors)",
    re.IGNORECASE,
)
GLOBAL_REVIEW_NOTE = (
    "Commercial use remains blocked pending location-specific Mapzen "
    "terrain-provider attribution and rights review."
)
ASSEMBLY_REPLACEMENT_NOTE = (
    "North-up release terrain is replaced by the frozen global DEM pass."
)
NATIVE_TERRAIN_NOTE = (
    "North-up release terrain uses the source-native factual terrain restored "
    "by the audited source-precedence pass."
)
GLOBAL_PROFILE_NOTE = (
    "The global DEM is retained only for the route elevation profile because "
    "the route source has no complete embedded elevation series."
)
GLOBAL_RELIEF_NOTE = (
    "The global DEM is retained only for the terrain-relief edition after "
    "passing the audited full-field density and extent-binding gate."
)
RELIEF_MINIMUM_LEVEL_GAIN = 2
RELIEF_MINIMUM_LEVEL_GAIN_RATIO = 0.25
RELIEF_MINIMUM_LENGTH_GAIN_RATIO = 0.15
RELIEF_EXCEPTIONAL_LENGTH_GAIN_RATIO = 4.0
ELEVATION_FIELD_PREFIX = "elevation_"
SOURCE_OBJECT_PATTERN = re.compile(r"^node/([1-9][0-9]*)$")
VARIANT_TERRAIN_CREDITS: dict[str, dict[str, Any]] = {
    "RTE-GB-WHW-01": {
        "detailed_source_ref": "os-terrain-50-2026",
        "relief_source_ref": "aws-mapzen-terrarium-z9",
        "credit_lines": {
            "detailed-map": (
                "CONTAINS OS DATA © CROWN COPYRIGHT 2026 | "
                f"{OSM_PRINT_CREDIT} | PROFILE: MAPZEN AWS TERRAIN"
            ),
            "terrain-relief": (
                f"{OSM_PRINT_CREDIT} | PROFILE/RELIEF: MAPZEN AWS TERRAIN"
            ),
        },
    },
    "RTE-GB-GGW-01": {
        "detailed_source_ref": "os-terrain-50-2026",
        "relief_source_ref": "aws-mapzen-terrarium-z9",
        "credit_lines": {
            "detailed-map": (
                "OS DATA © CROWN COPYRIGHT 2026 | "
                f"{OSM_PRINT_CREDIT} | PROFILE: MAPZEN AWS TERRAIN"
            ),
            "terrain-relief": (
                f"{OSM_PRINT_CREDIT} | PROFILE/RELIEF: MAPZEN AWS TERRAIN"
            ),
        },
    },
    "RTE-GB-JMW-WALK-01": {
        "detailed_source_ref": "os-terrain-50-2026",
        "relief_source_ref": "aws-mapzen-terrarium-z9",
        "credit_lines": {
            "detailed-map": (
                "OS DATA © CROWN COPYRIGHT 2026 | "
                f"{OSM_PRINT_CREDIT} | PROFILE: MAPZEN AWS TERRAIN"
            ),
            "terrain-relief": (
                f"{OSM_PRINT_CREDIT} | PROFILE/RELIEF: MAPZEN AWS TERRAIN"
            ),
        },
    },
    "RTE-FR-ECR-995181": {
        "detailed_source_ref": "ign-france-contours-wfs-2026-08-03",
        "relief_source_ref": "aws-mapzen-terrarium-z9",
        "credit_lines": {
            "detailed-map": (
                "SOURCE: Parc national des Écrins / IGN LO2 | "
                f"{OSM_PRINT_CREDIT}"
            ),
            "terrain-relief": (
                f"SOURCE: Parc national des Écrins | {OSM_PRINT_CREDIT} | "
                "RELIEF: MAPZEN AWS TERRAIN"
            ),
        },
    },
}


class SourcePrecedenceError(ValueError):
    """Raised when source identity or provenance cannot be proved."""


def _fail(message: str) -> NoReturn:
    raise SourcePrecedenceError(message)


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=hike_plates._reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _fail(f"could not read {label} {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label} must contain a JSON object")
    return value


def _records_by_id(
    catalog: dict[str, Any],
    *,
    label: str,
    expected_id: str,
    expected_count: int,
) -> dict[str, dict[str, Any]]:
    if catalog.get("schema_version") != 1 or catalog.get("id") != expected_id:
        _fail(f"{label} must use schema 1 / id {expected_id!r}")
    records = catalog.get("plates")
    if not isinstance(records, list) or len(records) != expected_count:
        _fail(f"{label} must contain exactly {expected_count} plates")
    if not all(isinstance(record, dict) for record in records):
        _fail(f"{label} plates must all be objects")
    identifiers = [str(record.get("id") or "") for record in records]
    if any(not identifier for identifier in identifiers):
        _fail(f"{label} contains a plate without an id")
    if len(identifiers) != len(set(identifiers)):
        _fail(f"{label} repeats a plate id")
    return dict(zip(identifiers, records, strict=True))


def _load_explicit_legacy_records(
    catalog_path: Path,
    context_path: Path,
) -> list[dict[str, Any]]:
    """Load explicit legacy paths with the production validators and merge rules."""

    catalog = _load_json_object(catalog_path, label="legacy catalog")
    context_bundle = _load_json_object(context_path, label="legacy context")
    try:
        records = copy.deepcopy(hike_plates._validate_catalog(catalog))
    except hike_plates.MapPlotterError as exc:
        _fail(f"legacy catalog is invalid: {exc}")
    if (
        context_bundle.get("schema_version") != 3
        or context_bundle.get("id") != hike_plates.CONTEXT_BUNDLE_ID
    ):
        _fail(
            f"legacy context must use schema 3 / id {hike_plates.CONTEXT_BUNDLE_ID!r}"
        )
    overlays = context_bundle.get("records")
    if not isinstance(overlays, list) or len(overlays) != len(records):
        _fail("legacy context must contain exactly ten overlay records")
    records_by_id = {str(record["id"]): record for record in records}
    seen: set[str] = set()
    for raw_overlay in overlays:
        if not isinstance(raw_overlay, dict):
            _fail("legacy context overlays must be objects")
        subject_id = str(raw_overlay.get("subject_id") or "")
        if subject_id not in records_by_id or subject_id in seen:
            _fail(f"legacy context has unknown or repeated subject {subject_id!r}")
        seen.add(subject_id)
        sources = raw_overlay.get("sources")
        context = raw_overlay.get("context")
        backdrop = raw_overlay.get("backdrop")
        if not isinstance(sources, list) or not sources:
            _fail(f"legacy context {subject_id} has no sources")
        if not isinstance(context, dict) or not isinstance(backdrop, dict):
            _fail(f"legacy context {subject_id} has invalid context/backdrop data")
        target = records_by_id[subject_id]
        target["sources"].extend(copy.deepcopy(sources))
        for key in ("terrain", "relief_terrain", "landcover", "water"):
            if key in context:
                target["context"][key] = copy.deepcopy(context[key])
        target["backdrop"].update(copy.deepcopy(backdrop))
        if "credit_line" in raw_overlay:
            credit = raw_overlay["credit_line"]
            if not isinstance(credit, str) or not credit.strip():
                _fail(f"legacy context {subject_id} has an invalid credit_line")
            target["credit_line"] = credit
        target["notes"].append(
            f"Geographic context overlaid from {hike_plates.CONTEXT_BUNDLE_ID}."
        )
    try:
        return copy.deepcopy(
            [
                hike_plates._validate_record(
                    record,
                    label=f"merged plates[{index}]",
                )
                for index, record in enumerate(records)
            ]
        )
    except hike_plates.MapPlotterError as exc:
        _fail(f"merged legacy catalog is invalid: {exc}")


def load_legacy_records(
    *,
    catalog_path: Path | None = None,
    context_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Use fixed production data unless both explicit legacy paths are supplied."""

    if (catalog_path is None) != (context_path is None):
        _fail("--legacy-catalog and --legacy-context must be supplied together")
    if catalog_path is not None and context_path is not None:
        return _load_explicit_legacy_records(catalog_path, context_path)
    try:
        return hike_plates.load_hike_catalog()
    except hike_plates.MapPlotterError as exc:
        _fail(f"could not load production legacy catalog/context: {exc}")


def _legacy_records_by_id(
    records: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if len(records) != 10 or not all(isinstance(record, dict) for record in records):
        _fail("merged legacy input must contain exactly ten records")
    result = {str(record.get("id") or ""): record for record in records}
    if set(result) != set(hike_plates.EXPECTED_IDS) or "" in result:
        _fail("merged legacy IDs do not match the ten audited legacy routes")
    return result


def _checked_xy(point: Any, *, label: str) -> tuple[float, float]:
    if not isinstance(point, list) or len(point) < 2:
        _fail(f"{label} is not a longitude/latitude point")
    try:
        longitude = float(point[0])
        latitude = float(point[1])
    except (TypeError, ValueError):
        _fail(f"{label} has non-numeric longitude/latitude")
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        _fail(f"{label} has non-finite longitude/latitude")
    return longitude, latitude


def _assert_route_identity(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    subject_id: str,
) -> None:
    target_route = target.get("route")
    source_route = source.get("route")
    if not isinstance(target_route, dict) or not isinstance(source_route, dict):
        _fail(f"{subject_id}: route must be an object in both inputs")
    target_segments = target_route.get("segments")
    source_segments = source_route.get("segments")
    if not isinstance(target_segments, list) or not isinstance(source_segments, list):
        _fail(f"{subject_id}: route segments must be arrays")
    if len(target_segments) != len(source_segments):
        _fail(f"{subject_id}: route segment count changed before elevation merge")
    for segment_index, (target_segment, source_segment) in enumerate(
        zip(target_segments, source_segments, strict=True)
    ):
        if not isinstance(target_segment, dict) or not isinstance(source_segment, dict):
            _fail(f"{subject_id}: segment {segment_index} is not an object")
        target_identity = (target_segment.get("id"), target_segment.get("mode"))
        source_identity = (source_segment.get("id"), source_segment.get("mode"))
        if target_identity != source_identity:
            _fail(
                f"{subject_id}: segment {segment_index} identity changed "
                f"from {source_identity!r} to {target_identity!r}"
            )
        target_points = target_segment.get("points")
        source_points = source_segment.get("points")
        if not isinstance(target_points, list) or not isinstance(source_points, list):
            _fail(f"{subject_id}: segment {target_identity[0]} points are invalid")
        if len(target_points) != len(source_points):
            _fail(f"{subject_id}: segment {target_identity[0]} point count changed")
        for point_index, (target_point, source_point) in enumerate(
            zip(target_points, source_points, strict=True)
        ):
            target_xy = _checked_xy(
                target_point,
                label=f"{subject_id} target point {segment_index}/{point_index}",
            )
            source_xy = _checked_xy(
                source_point,
                label=f"{subject_id} source point {segment_index}/{point_index}",
            )
            if target_xy != source_xy:
                _fail(
                    f"{subject_id}: longitude/latitude changed at segment "
                    f"{target_identity[0]} point {point_index}: "
                    f"{source_xy!r} != {target_xy!r}"
                )


def _checked_extent(value: Any, *, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        _fail(f"{label} must be [west, south, east, north]")
    try:
        west, south, east, north = (float(item) for item in value)
    except (TypeError, ValueError):
        _fail(f"{label} contains a non-numeric coordinate")
    checked = (west, south, east, north)
    if not all(math.isfinite(item) for item in checked):
        _fail(f"{label} contains a non-finite coordinate")
    if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
        _fail(f"{label} is not a valid geographic extent")
    return checked


def _assert_global_terrain_extent_identity(
    record: dict[str, Any], *, subject_id: str
) -> None:
    """Prove the frozen global bundle belongs to this route and page window."""

    context = record.get("context")
    if not isinstance(context, dict):
        _fail(f"{subject_id}: context must be an object")
    terrain = context.get("terrain")
    if not isinstance(terrain, dict):
        _fail(f"{subject_id}: release terrain must be an object")
    source_ref = str(terrain.get("source_ref") or "")
    if not source_ref.startswith(GLOBAL_TERRAIN_SOURCE_PREFIX):
        return
    route_extent = _checked_extent(
        context.get("route_extent", context.get("extent")),
        label=f"{subject_id}.context.route_extent",
    )
    map_extent = _checked_extent(
        context.get("extent"),
        label=f"{subject_id}.context.extent",
    )
    if _checked_extent(
        terrain.get("route_extent"),
        label=f"{subject_id}.context.terrain.route_extent",
    ) != route_extent:
        _fail(f"{subject_id}: global terrain route extent identity changed")
    if _checked_extent(
        terrain.get("source_window_extent"),
        label=f"{subject_id}.context.terrain.source_window_extent",
    ) != map_extent:
        _fail(f"{subject_id}: global terrain map extent identity changed")
    derivation = record.get("terrain_derivation")
    global_derivation = (
        derivation.get("global_terrarium") if isinstance(derivation, dict) else None
    )
    if not isinstance(global_derivation, dict):
        _fail(f"{subject_id}: global terrain has no derivation evidence")
    if _checked_extent(
        global_derivation.get("route_extent"),
        label=f"{subject_id}.terrain_derivation.global_terrarium.route_extent",
    ) != route_extent:
        _fail(f"{subject_id}: global derivation route extent identity changed")
    if _checked_extent(
        global_derivation.get("map_extent"),
        label=f"{subject_id}.terrain_derivation.global_terrarium.map_extent",
    ) != map_extent:
        _fail(f"{subject_id}: global derivation map extent identity changed")
    binding = context.get("map_extent_binding")
    if not isinstance(binding, dict):
        _fail(f"{subject_id}: global terrain has no context map-extent binding")
    expected_binding_sha256 = hike_plates._canonical_json_sha256(binding)
    if any(
        item != expected_binding_sha256
        for item in (
            terrain.get("map_extent_binding_sha256"),
            global_derivation.get("map_extent_binding_sha256"),
        )
    ):
        _fail(f"{subject_id}: global terrain map-extent digest identity changed")


def _terrain_contour_paths(
    terrain: dict[str, Any],
) -> list[tuple[float, list[list[Any]]]]:
    result: list[tuple[float, list[list[Any]]]] = []
    contours = terrain.get("contours")
    if isinstance(contours, list):
        for contour in contours:
            if not isinstance(contour, dict):
                continue
            elevation = contour.get("elevation_m")
            paths = contour.get("paths")
            if (
                isinstance(elevation, (int, float))
                and not isinstance(elevation, bool)
                and isinstance(paths, list)
            ):
                result.append((float(elevation), paths))
    return result


def _terrain_density(
    terrain: dict[str, Any],
    *,
    map_extent: Sequence[float],
) -> dict[str, float | int]:
    west, south, east, north = (float(value) for value in map_extent)
    longitude_span = east - west
    latitude_span = north - south
    levels: set[float] = set()
    path_count = 0
    normalized_length = 0.0
    for elevation, paths in _terrain_contour_paths(terrain):
        levels.add(elevation)
        for path in paths:
            if not isinstance(path, list) or len(path) < 2:
                continue
            checked = [
                _checked_xy(point, label="terrain contour point") for point in path
            ]
            path_count += 1
            for first, second in zip(checked, checked[1:]):
                normalized_length += math.hypot(
                    (second[0] - first[0]) / longitude_span,
                    (second[1] - first[1]) / latitude_span,
                )
    return {
        "contour_level_count": len(levels),
        "contour_path_count": path_count,
        "normalized_full_field_length": round(normalized_length, 6),
    }


def _select_relief_terrain(
    *,
    native: dict[str, Any],
    global_candidate: dict[str, Any],
    map_extent: Sequence[float],
) -> tuple[bool, dict[str, Any]]:
    native_density = _terrain_density(native, map_extent=map_extent)
    global_density = _terrain_density(global_candidate, map_extent=map_extent)
    native_levels = int(native_density["contour_level_count"])
    global_levels = int(global_density["contour_level_count"])
    minimum_level_gain = max(
        RELIEF_MINIMUM_LEVEL_GAIN,
        math.ceil(native_levels * RELIEF_MINIMUM_LEVEL_GAIN_RATIO),
    )
    native_length = float(native_density["normalized_full_field_length"])
    global_length = float(global_density["normalized_full_field_length"])
    level_and_length_selected = (
        global_levels >= native_levels + minimum_level_gain
        and global_length
        >= native_length * (1.0 + RELIEF_MINIMUM_LENGTH_GAIN_RATIO)
    )
    # A narrow source-native corridor can carry nearly the same number of
    # levels as a page-wide DEM while still leaving most of the artwork blank.
    # Preserve the native bundle for the detailed map, but allow the bound
    # global candidate to drive the relief edition when it loses no levels and
    # contributes at least four times the normalized full-field linework.
    exceptional_full_field_selected = (
        global_levels >= native_levels
        and global_length
        >= native_length * RELIEF_EXCEPTIONAL_LENGTH_GAIN_RATIO
    )
    global_selected = level_and_length_selected or exceptional_full_field_selected
    evidence = {
        "policy_id": RELIEF_TERRAIN_POLICY_ID,
        "selected": "global-relief-terrain" if global_selected else "native-terrain",
        "selection_reason": (
            "level-and-length-gain"
            if level_and_length_selected
            else "exceptional-full-field-length-gain"
            if exceptional_full_field_selected
            else "native-precedence"
        ),
        "minimum_level_gain": minimum_level_gain,
        "minimum_length_gain_ratio": RELIEF_MINIMUM_LENGTH_GAIN_RATIO,
        "exceptional_length_gain_ratio": RELIEF_EXCEPTIONAL_LENGTH_GAIN_RATIO,
        "native": native_density,
        "global": global_density,
    }
    return global_selected, evidence


def _complete_route_elevation(record: dict[str, Any]) -> bool:
    route = record.get("route")
    segments = route.get("segments") if isinstance(route, dict) else None
    if not isinstance(segments, list) or not segments:
        return False
    points = [
        point
        for segment in segments
        if isinstance(segment, dict)
        for point in segment.get("points", [])
    ]
    if not points:
        return False
    for point in points:
        if not isinstance(point, list) or len(point) < 3:
            return False
        elevation = point[2]
        if (
            isinstance(elevation, bool)
            or not isinstance(elevation, (int, float))
            or not math.isfinite(float(elevation))
        ):
            return False
    return True


def _clear_elevation_fields(value: dict[str, Any]) -> None:
    for key in list(value):
        if key == "elevation_m" or key.startswith(ELEVATION_FIELD_PREFIX):
            value.pop(key, None)


def _copy_elevation_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    _clear_elevation_fields(target)
    for key, value in source.items():
        if key == "elevation_m" or key.startswith(ELEVATION_FIELD_PREFIX):
            target[key] = copy.deepcopy(value)


def _restore_route_elevation(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    subject_id: str,
) -> bool:
    if not _complete_route_elevation(source):
        return False
    target_route = target["route"]
    source_route = source["route"]
    for target_segment, source_segment in zip(
        target_route["segments"], source_route["segments"], strict=True
    ):
        restored_points: list[list[float]] = []
        for target_point, source_point in zip(
            target_segment["points"], source_segment["points"], strict=True
        ):
            longitude, latitude = _checked_xy(
                target_point,
                label=f"{subject_id}: target route point",
            )
            restored_points.append([longitude, latitude, float(source_point[2])])
        target_segment["points"] = restored_points
        for key in list(target_segment):
            if key.startswith(ELEVATION_FIELD_PREFIX):
                target_segment.pop(key, None)
        for key, value in source_segment.items():
            if key.startswith(ELEVATION_FIELD_PREFIX):
                target_segment[key] = copy.deepcopy(value)
    for key in list(target_route):
        if key.startswith(ELEVATION_FIELD_PREFIX):
            target_route.pop(key, None)
    for key, value in source_route.items():
        if key.startswith(ELEVATION_FIELD_PREFIX):
            target_route[key] = copy.deepcopy(value)
    elevation_source_ref = source_route.get("elevation_source_ref")
    if not isinstance(elevation_source_ref, str) or not elevation_source_ref:
        elevation_source_ref = source_route.get("source_ref")
    if not isinstance(elevation_source_ref, str) or not elevation_source_ref:
        _fail(f"{subject_id}: embedded route elevation has no source reference")
    target_route["elevation_source_ref"] = elevation_source_ref
    target_route.setdefault(
        "elevation_method",
        "route-source-embedded-elevation-v1",
    )
    target_route["profile_status"] = str(
        source_route.get("profile_status") or "source-elevation-sampled"
    )
    return True


def _feature_index(
    record: dict[str, Any],
    *,
    subject_id: str,
) -> dict[str, dict[str, Any]]:
    context = record.get("context")
    features = context.get("features") if isinstance(context, dict) else None
    if not isinstance(features, list):
        _fail(f"{subject_id}: context.features must be an array")
    relevant = [
        feature
        for feature in features
        if isinstance(feature, dict) and feature.get("kind") in {"peak", "pass"}
    ]
    identifiers = [str(feature.get("id") or "") for feature in relevant]
    if any(not identifier for identifier in identifiers):
        _fail(f"{subject_id}: peak/pass feature is missing an id")
    if len(identifiers) != len(set(identifiers)):
        _fail(f"{subject_id}: peak/pass feature ids are not unique")
    return dict(zip(identifiers, relevant, strict=True))


def _feature_source_object(feature: dict[str, Any]) -> str | None:
    osm_type = feature.get("osm_type")
    osm_id = feature.get("osm_id")
    if (
        isinstance(osm_type, str)
        and isinstance(osm_id, int)
        and not isinstance(osm_id, bool)
    ):
        return f"{osm_type}/{osm_id}"
    source_object = feature.get("source_object")
    return source_object if isinstance(source_object, str) else None


def _assert_feature_identity(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    subject_id: str,
    feature_id: str,
) -> None:
    if target.get("kind") != source.get("kind"):
        _fail(f"{subject_id}/{feature_id}: feature kind changed")
    target_object = _feature_source_object(target)
    source_object = _feature_source_object(source)
    if target_object != source_object:
        _fail(
            f"{subject_id}/{feature_id}: source object changed from "
            f"{source_object!r} to {target_object!r}"
        )
    if _checked_xy(
        target.get("point"), label=f"{subject_id}/{feature_id} target point"
    ) != _checked_xy(
        source.get("point"), label=f"{subject_id}/{feature_id} source point"
    ):
        _fail(f"{subject_id}/{feature_id}: source point changed")


def _is_explicit_source_elevation(feature: dict[str, Any]) -> bool:
    elevation = feature.get("elevation_m")
    source_ref = feature.get("elevation_source_ref")
    return (
        isinstance(elevation, (int, float))
        and not isinstance(elevation, bool)
        and math.isfinite(float(elevation))
        and isinstance(source_ref, str)
        and bool(source_ref)
        and not source_ref.startswith(GLOBAL_TERRAIN_SOURCE_PREFIX)
    )


def _is_global_inferred_elevation(feature: dict[str, Any]) -> bool:
    source_ref = feature.get("elevation_source_ref")
    method = str(feature.get("elevation_method") or "")
    return (
        isinstance(source_ref, str)
        and source_ref.startswith(GLOBAL_TERRAIN_SOURCE_PREFIX)
    ) or method.startswith("mapzen-terrarium-")


def _restore_feature_elevations(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    subject_id: str,
) -> tuple[int, int]:
    target_features = _feature_index(target, subject_id=subject_id)
    source_features = _feature_index(source, subject_id=subject_id)
    if set(target_features) != set(source_features):
        _fail(f"{subject_id}: peak/pass feature inventory changed")
    restored = 0
    removed = 0
    for feature_id, target_feature in target_features.items():
        source_feature = source_features[feature_id]
        _assert_feature_identity(
            target_feature,
            source_feature,
            subject_id=subject_id,
            feature_id=feature_id,
        )
        if _is_explicit_source_elevation(source_feature):
            _copy_elevation_fields(target_feature, source_feature)
            restored += 1
        elif _is_global_inferred_elevation(target_feature):
            _clear_elevation_fields(target_feature)
            removed += 1
    return restored, removed


def _source_index(
    record: dict[str, Any], *, subject_id: str
) -> dict[str, dict[str, Any]]:
    sources = record.get("sources")
    if not isinstance(sources, list):
        _fail(f"{subject_id}: sources must be an array")
    result: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            _fail(f"{subject_id}: source entries must be objects")
        source_id = str(source.get("id") or "")
        if not source_id or source_id in result:
            _fail(f"{subject_id}: source ids must be non-empty and unique")
        result[source_id] = source
    return result


def _referenced_source_ids(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "sources":
                continue
            if (key == "source_ref" or key.endswith("_source_ref")) and isinstance(
                child, str
            ):
                references.add(child)
            else:
                references.update(_referenced_source_ids(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_referenced_source_ids(child))
    return references


def _ensure_sources(
    target: dict[str, Any],
    source: dict[str, Any],
    references: set[str],
    *,
    subject_id: str,
) -> None:
    target_sources = _source_index(target, subject_id=subject_id)
    source_sources = _source_index(source, subject_id=subject_id)
    for source_id in sorted(references):
        if source_id in target_sources:
            continue
        source_record = source_sources.get(source_id)
        if source_record is None:
            _fail(
                f"{subject_id}: source {source_id!r} required by restored evidence "
                "is absent from its source catalog"
            )
        copied = copy.deepcopy(source_record)
        target["sources"].append(copied)
        target_sources[source_id] = copied


def _global_source_ids(record: dict[str, Any], *, subject_id: str) -> set[str]:
    return {
        source_id
        for source_id in _source_index(record, subject_id=subject_id)
        if source_id.startswith(GLOBAL_TERRAIN_SOURCE_PREFIX)
    }


def _uses_source(value: Any, source_id: str) -> bool:
    return source_id in _referenced_source_ids(value)


def _relabel_or_prune_global_sources(
    record: dict[str, Any], *, subject_id: str
) -> set[str]:
    retained_ids: set[str] = set()
    retained_sources: list[dict[str, Any]] = []
    for source in record["sources"]:
        source_id = str(source.get("id") or "")
        if not source_id.startswith(GLOBAL_TERRAIN_SOURCE_PREFIX):
            retained_sources.append(source)
            continue
        route_use = _uses_source(record.get("route"), source_id)
        context = record.get("context", {})
        terrain_use = _uses_source(context.get("terrain"), source_id)
        relief_terrain_use = _uses_source(context.get("relief_terrain"), source_id)
        feature_use = any(
            feature.get("elevation_source_ref") == source_id
            for feature in _feature_index(record, subject_id=subject_id).values()
        )
        if not (route_use or terrain_use or relief_terrain_use or feature_use):
            continue
        uses: list[str] = []
        if route_use:
            uses.append("sampled route elevations")
        if feature_use:
            uses.append("sampled named-feature elevations")
        if terrain_use:
            terrain = record["context"]["terrain"]
            terrain_roles = ["selected elevation contours"]
            if terrain.get("relief_strokes"):
                terrain_roles.append("frozen DEM-gradient fall lines")
            uses.append(" and ".join(terrain_roles))
        if relief_terrain_use:
            uses.append(
                "source-precedence-selected continuous elevation contours for "
                "paired v4.2 artworks"
            )
        relabelled = copy.deepcopy(source)
        relabelled["use"] = "cached Terrarium DEM tiles; " + ", ".join(uses)
        if (route_use or relief_terrain_use) and not terrain_use:
            relabelled["use"] += "; source-native terrain retained separately"
        retained_sources.append(relabelled)
        retained_ids.add(source_id)
    record["sources"] = retained_sources
    return retained_ids


def _sanitize_global_derivation(
    record: dict[str, Any],
    *,
    subject_id: str,
    native_terrain: bool,
    global_profile_retained: bool,
    global_relief_retained: bool,
    relief_selection: dict[str, Any] | None,
    removed_feature_elevations: int,
) -> None:
    derivation = record.get("terrain_derivation")
    if not isinstance(derivation, dict):
        derivation = {}
        record["terrain_derivation"] = derivation
    global_derivation = derivation.get("global_terrarium")
    if native_terrain and global_relief_retained:
        if not isinstance(global_derivation, dict):
            _fail(f"{subject_id}: selected global relief has no derivation evidence")
        global_derivation["output_scope"] = (
            "terrain-relief-and-route-elevation-profile"
            if global_profile_retained
            else "terrain-relief-only"
        )
    elif native_terrain:
        global_derivation = derivation.pop("global_terrarium", None)
        if global_profile_retained and isinstance(global_derivation, dict):
            retained_keys = (
                "route_points_total",
                "route_points_sampled",
                "route_points_nearest_nonnegative_fallback",
                "route_points_sea_surface_referenced",
                "ferry_segments_sea_surface_referenced",
                "profile_points_total",
                "profile_points_sampled",
                "profile_points_nearest_nonnegative_fallback",
                "zoom",
                "route_extent",
                "map_extent",
                "map_extent_binding_sha256",
                "source_window_sha256",
                "derived_window_sha256",
            )
            profile_derivation = {
                key: copy.deepcopy(global_derivation[key])
                for key in retained_keys
                if key in global_derivation
            }
            profile_derivation["source_ref"] = str(
                record["route"].get("elevation_source_ref") or ""
            )
            profile_derivation["output_scope"] = "route-elevation-profile-only"
            derivation["global_route_profile"] = profile_derivation
    retained_global_derivation = derivation.get("global_terrarium")
    if isinstance(retained_global_derivation, dict):
        global_derivation = retained_global_derivation
        previous_peak_count = global_derivation.get("peaks_sampled")
        global_derivation["peaks_sampled"] = 0
        global_derivation["named_feature_elevation_policy"] = "explicit-source-only-v1"
        if isinstance(previous_peak_count, int) and previous_peak_count > 0:
            global_derivation["superseded_peak_samples_removed"] = previous_peak_count
    derivation["source_precedence"] = {
        "policy_id": POLICY_ID,
        "native_terrain_restored": native_terrain,
        "global_route_profile_retained": global_profile_retained,
        "global_relief_terrain_retained": global_relief_retained,
        "global_named_feature_elevations_removed": removed_feature_elevations,
        "terrain_source_ref": str(
            record.get("context", {}).get("terrain", {}).get("source_ref") or ""
        ),
        "route_elevation_source_ref": str(
            record.get("route", {}).get("elevation_source_ref") or ""
        ),
        "relief_terrain_source_ref": str(
            record.get("context", {})
            .get("relief_terrain", {})
            .get("source_ref")
            or record.get("context", {}).get("terrain", {}).get("source_ref")
            or ""
        ),
    }
    if relief_selection is not None:
        derivation["source_precedence"]["relief_terrain_selection"] = copy.deepcopy(
            relief_selection
        )


def _restore_native_terrain(
    target: dict[str, Any],
    legacy: dict[str, Any],
    *,
    subject_id: str,
) -> None:
    target_context = target.get("context")
    legacy_context = legacy.get("context")
    if not isinstance(target_context, dict) or not isinstance(legacy_context, dict):
        _fail(f"{subject_id}: contexts must be objects")
    terrain = legacy_context.get("terrain")
    if not isinstance(terrain, dict):
        _fail(f"{subject_id}: merged legacy record has no native terrain")
    target_context["terrain"] = copy.deepcopy(terrain)
    _ensure_sources(
        target,
        legacy,
        _referenced_source_ids(terrain),
        subject_id=subject_id,
    )
    target_backdrop = target.get("backdrop")
    legacy_backdrop = legacy.get("backdrop")
    if isinstance(target_backdrop, dict) and isinstance(legacy_backdrop, dict):
        for key in ("status", "terrain"):
            if key in legacy_backdrop:
                target_backdrop[key] = copy.deepcopy(legacy_backdrop[key])


def _update_legacy_notes_and_credit(
    record: dict[str, Any],
    legacy: dict[str, Any],
    *,
    global_profile_retained: bool,
    global_relief_retained: bool,
) -> None:
    notes = record.get("notes")
    if not isinstance(notes, list):
        _fail(f"{record.get('id')}: notes must be an array")
    notes[:] = [
        note
        for note in notes
        if note != ASSEMBLY_REPLACEMENT_NOTE
        and (
            global_profile_retained
            or global_relief_retained
            or note != GLOBAL_REVIEW_NOTE
        )
    ]
    if NATIVE_TERRAIN_NOTE not in notes:
        notes.append(NATIVE_TERRAIN_NOTE)
    if global_profile_retained and GLOBAL_PROFILE_NOTE not in notes:
        notes.append(GLOBAL_PROFILE_NOTE)
    if global_relief_retained and GLOBAL_RELIEF_NOTE not in notes:
        notes.append(GLOBAL_RELIEF_NOTE)
    legacy_credit = legacy.get("credit_line")
    if not isinstance(legacy_credit, str) or not legacy_credit.strip():
        _fail(f"{record.get('id')}: legacy credit_line is missing")
    if (global_profile_retained or global_relief_retained) and (
        "MAPZEN" not in legacy_credit.upper()
    ):
        uses = "/".join(
            use
            for enabled, use in (
                (global_profile_retained, "PROFILE"),
                (global_relief_retained, "RELIEF"),
            )
            if enabled
        )
        record["credit_line"] = f"{legacy_credit} | {uses}: MAPZEN AWS TERRAIN"
    else:
        record["credit_line"] = legacy_credit


def _ensure_visible_route_attribution(record: dict[str, Any]) -> None:
    """Keep a non-ODbL route publisher in the plotted credit block."""

    subject_id = str(record.get("id") or "<unknown>")
    route = record.get("route")
    if not isinstance(route, dict):
        _fail(f"{subject_id}: route must be an object")
    sources = _source_index(record, subject_id=subject_id)
    route_source = sources.get(str(route.get("source_ref") or ""))
    if route_source is None:
        _fail(f"{subject_id}: route source is absent from the source register")
    if str(route_source.get("license") or "").upper().startswith("ODBL"):
        return
    attribution = " ".join(str(route_source.get("attribution") or "").split())
    if not attribution:
        _fail(f"{subject_id}: non-ODbL route source has no attribution")
    credit = str(record.get("credit_line") or "").strip()
    composition = record.get("composition")
    format_id = (
        str(composition.get("format_id") or "")
        if isinstance(composition, dict)
        else ""
    )
    visible_attribution = attribution
    publisher = " ".join(str(route_source.get("publisher") or "").split())
    if format_id.endswith("-landscape") and publisher:
        # Landscape hiking furniture has a deliberately narrow attribution
        # rail.  The complete licence attribution remains in SVG/manifest
        # source metadata; the plotted line names the publisher explicitly.
        visible_attribution = f"SOURCE: {publisher}"
        credit = credit.replace(attribution, visible_attribution)

    # Normalize any printed OSM URL before the route-publisher compaction.  The
    # final ODbL pass below restores a canonical project-name + URL clause for
    # every ODbL-backed record; this intermediate step must never discard a URL
    # already supplied by an upstream plate.
    credit_lines = []
    for raw_line in credit.split(" | "):
        line = raw_line.strip().replace(
            "© OSM CONTRIBUTORS", "© OPENSTREETMAP CONTRIBUTORS"
        )
        line = OSM_PRINT_URL_PATTERN.sub(
            "OPENSTREETMAP.ORG/COPYRIGHT",
            line,
        ).strip(" /")
        if line:
            credit_lines.append(line)
    credit = " | ".join(credit_lines)
    normalized_credit = " ".join(credit.replace("|", " ").split())
    if visible_attribution.casefold() not in normalized_credit.casefold():
        lines = [
            visible_attribution,
            *(line.strip() for line in credit.split(" | ")),
        ]
        lines = [line for line in lines if line]
        # The physical furniture contract permits at most three plotted
        # attribution lines.  Preserve the route publisher verbatim, keep a
        # final terrain-use line distinct, and coalesce any middle map/ODbL
        # clauses with slash separators rather than dropping a credit.
        if len(lines) > 3:
            lines = [lines[0], " / ".join(lines[1:-1]), lines[-1]]
        record["credit_line"] = " | ".join(lines)
    else:
        record["credit_line"] = credit

    # The Camino-style combination of an independent route publisher, IGN
    # terrain, OpenStreetMap context and a separate relief provider otherwise
    # creates one unplottably long middle line after the three-line fold.  Keep
    # every named provider/licence, use the standard IGN acronym, and move the
    # canonical OSM print credit onto its own physically legible line.
    final_lines = [
        line.strip()
        for line in str(record.get("credit_line") or "").split(" | ")
        if line.strip()
    ]
    if (
        format_id.endswith("-landscape")
        and len(final_lines) == 3
        and "IGN" in final_lines[1].upper()
        and "CC BY 4.0" in final_lines[1].upper()
        and "OPENSTREETMAP" in final_lines[1].upper()
    ):
        route_clause = re.sub(
            r"^SOURCE:\s*",
            "",
            final_lines[0],
            flags=re.IGNORECASE,
        )
        record["credit_line"] = " | ".join(
            (
                f"{route_clause} / IGN CC BY 4.0",
                OSM_PRINT_CREDIT,
                final_lines[2],
            )
        )


def _ensure_visible_odbl_attribution(record: dict[str, Any]) -> None:
    """Emit one canonical, plot-legible printed ODbL credit.

    ``|`` is the physical attribution-line separator.  The hiking furniture
    permits at most three lines, so provider clauses are compacted without
    dropping them while OpenStreetMap receives a dedicated canonical line.
    """

    subject_id = str(record.get("id") or "<unknown>")
    sources = _source_index(record, subject_id=subject_id)
    if not any(
        str(source.get("license") or "").upper().startswith("ODBL")
        for source in sources.values()
    ):
        return

    other_clauses: list[str] = []
    terrain_clauses: list[str] = []
    for raw_line in str(record.get("credit_line") or "").split(" | "):
        line = OSM_PRINT_URL_PATTERN.sub("", raw_line.strip())
        line = OSM_CONTRIBUTORS_PATTERN.sub("", line)
        if subject_id == "RTE-CH-AP6-01":
            line = re.sub(
                r"\bRELIEF\s+©?\s*SWISSTOPO\b",
                "",
                line,
                flags=re.IGNORECASE,
            )
        line = re.sub(r"(?:\s*/\s*){2,}", " / ", line).strip(" /;")
        if not line:
            continue
        upper = line.upper()
        target = (
            terrain_clauses
            if "MAPZEN" in upper
            or upper.startswith("PROFILE")
            or upper.startswith("RELIEF")
            else other_clauses
        )
        if line.casefold() not in {value.casefold() for value in target}:
            target.append(line)

    if len(other_clauses) + len(terrain_clauses) <= 2:
        lines = [*other_clauses, OSM_PRINT_CREDIT, *terrain_clauses]
    elif other_clauses and terrain_clauses:
        lines = [
            " / ".join(other_clauses),
            OSM_PRINT_CREDIT,
            " / ".join(terrain_clauses),
        ]
    elif other_clauses:
        lines = [
            other_clauses[0],
            " / ".join(other_clauses[1:]),
            OSM_PRINT_CREDIT,
        ]
    else:
        lines = [
            OSM_PRINT_CREDIT,
            terrain_clauses[0],
            " / ".join(terrain_clauses[1:]),
        ]
    lines = [line for line in lines if line]
    if len(lines) > 3:
        _fail(f"{subject_id}: ODbL print credit exceeds three physical lines")
    record["credit_line"] = " | ".join(lines)

    if subject_id == "RTE-CH-AP6-01":
        swisstopo = sources.get("swisstopo-swissaltiregio-2026-08-03")
        if swisstopo is None:
            _fail(f"{subject_id}: retained swisstopo comparison source is absent")
        swisstopo["use"] = (
            "retained native DEM and contour evidence for source-precedence "
            "comparison; not emitted by paired v4.2 artworks"
        )


def _bind_variant_terrain_credits(record: dict[str, Any]) -> None:
    """Freeze variant-specific face credits when terrain providers diverge."""

    subject_id = str(record.get("id") or "<unknown>")
    specification = VARIANT_TERRAIN_CREDITS.get(subject_id)
    if specification is None:
        record.pop("variant_credit_lines", None)
        return
    sources = record.get("sources")
    source_ids = {
        str(source.get("id") or "")
        for source in sources
        if isinstance(source, dict)
    } if isinstance(sources, list) else set()
    expected_source_ids = {
        str(specification["detailed_source_ref"]),
        str(specification["relief_source_ref"]),
    }
    # Synthetic unit fixtures reuse production subject IDs with deliberately
    # generic source names.  Only activate the production binding when its
    # exact provider inventory is present; once present, source drift fails.
    if not expected_source_ids <= source_ids:
        record.pop("variant_credit_lines", None)
        return
    context = record.get("context")
    if not isinstance(context, dict):
        _fail(f"{subject_id}: context is absent while binding terrain credits")
    detailed = context.get("terrain")
    relief = context.get("relief_terrain", detailed)
    detailed_source_ref = (
        str(detailed.get("source_ref") or "") if isinstance(detailed, dict) else ""
    )
    relief_source_ref = (
        str(relief.get("source_ref") or "") if isinstance(relief, dict) else ""
    )
    if (
        detailed_source_ref != specification["detailed_source_ref"]
        or relief_source_ref != specification["relief_source_ref"]
    ):
        _fail(
            f"{subject_id}: selected terrain sources changed before variant "
            "credit binding"
        )
    record["variant_credit_lines"] = copy.deepcopy(specification["credit_lines"])


def _validate_peak_overlay(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if value.get("schema_version") != 1 or value.get("id") != PEAK_ELEVATIONS_ID:
        _fail(f"peak elevations must use schema 1 / id {PEAK_ELEVATIONS_ID!r}")
    source = value.get("source")
    objects = value.get("objects")
    if not isinstance(source, dict) or not isinstance(objects, list) or not objects:
        _fail("peak elevations must contain source metadata and non-empty objects")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(objects):
        if not isinstance(item, dict):
            _fail(f"peak elevations object {index} must be an object")
        source_object = item.get("source_object")
        if not isinstance(source_object, str) or not SOURCE_OBJECT_PATTERN.fullmatch(
            source_object
        ):
            _fail(f"peak elevations object {index} must identify one OSM summit node")
        if source_object in result:
            _fail(f"peak elevations repeats {source_object}")
        version = item.get("version")
        timestamp = item.get("timestamp")
        name = item.get("name")
        display_label = item.get("label")
        elevation = item.get("elevation_m")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= 0
            or not isinstance(timestamp, str)
            or not timestamp.strip()
            or not isinstance(name, str)
            or not name.strip()
            or (
                display_label is not None
                and (
                    not isinstance(display_label, str)
                    or not display_label.strip()
                )
            )
            or isinstance(elevation, bool)
            or not isinstance(elevation, (int, float))
            or not math.isfinite(float(elevation))
        ):
            _fail(f"peak elevations object {source_object} has invalid evidence")
        injection_fields = {
            "feature_id",
            "subject_ids",
            "point",
            "source_ref",
            "snapshot_sha256",
        }
        present_injection_fields = injection_fields.intersection(item)
        if present_injection_fields:
            if present_injection_fields != injection_fields:
                _fail(
                    f"peak elevations object {source_object} has an incomplete "
                    "context-feature injection contract"
                )
            feature_id = item["feature_id"]
            subject_ids = item["subject_ids"]
            point = item["point"]
            source_ref = item["source_ref"]
            snapshot_sha256 = item["snapshot_sha256"]
            if (
                not isinstance(feature_id, str)
                or not feature_id.strip()
                or not isinstance(subject_ids, list)
                or not subject_ids
                or len(subject_ids) != len(set(subject_ids))
                or any(not isinstance(subject_id, str) or not subject_id for subject_id in subject_ids)
                or not isinstance(point, list)
                or len(point) != 2
                or any(
                    isinstance(coordinate, bool)
                    or not isinstance(coordinate, (int, float))
                    or not math.isfinite(float(coordinate))
                    for coordinate in point
                )
                or not -180.0 <= float(point[0]) <= 180.0
                or not -90.0 <= float(point[1]) <= 90.0
                or not isinstance(source_ref, str)
                or not source_ref
                or not isinstance(snapshot_sha256, str)
                or len(snapshot_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in snapshot_sha256
                )
            ):
                _fail(
                    f"peak elevations object {source_object} has invalid "
                    "context-feature injection evidence"
                )
            priority = item.get("priority", -10)
            label_required = item.get("label_required", False)
            distance_to_route_m = item.get("distance_to_route_m")
            route_axis_fraction = item.get("route_axis_fraction")
            if (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or priority < -100
                or priority > 100
                or not isinstance(label_required, bool)
                or (
                    distance_to_route_m is not None
                    and (
                        isinstance(distance_to_route_m, bool)
                        or not isinstance(distance_to_route_m, (int, float))
                        or not math.isfinite(float(distance_to_route_m))
                        or float(distance_to_route_m) < 0.0
                    )
                )
                or (
                    route_axis_fraction is not None
                    and (
                        isinstance(route_axis_fraction, bool)
                        or not isinstance(route_axis_fraction, (int, float))
                        or not math.isfinite(float(route_axis_fraction))
                        or not 0.0 <= float(route_axis_fraction) <= 1.0
                    )
                )
            ):
                _fail(
                    f"peak elevations object {source_object} has invalid "
                    "context ranking evidence"
                )
        result[source_object] = copy.deepcopy(item)
    return result


def _inject_missing_peak_features(
    working_by_id: dict[str, dict[str, Any]],
    origin_by_id: dict[str, dict[str, Any]],
    objects: dict[str, dict[str, Any]],
    *,
    retrieved_at: str,
) -> int:
    """Admit explicitly scoped OSM summit nodes absent from broad context queries.

    A broad Overpass label query can truthfully return zero results even when a
    separately audited summit node is known.  The overlay may add that exact
    node only when it freezes identity, coordinate, version, timestamp and a
    canonical payload SHA-256.  The same feature is inserted into the in-memory
    origin so source-precedence identity checks remain fail-closed and reruns do
    not depend on a previous generated release.
    """

    injected = 0
    for source_object, evidence in objects.items():
        subject_ids = evidence.get("subject_ids")
        if not isinstance(subject_ids, list):
            continue
        osm_id = int(source_object.split("/", 1)[1])
        feature = {
            "id": str(evidence["feature_id"]),
            "kind": "peak",
            "label": str(evidence.get("label") or evidence["name"]).upper(),
            "name": str(evidence["name"]),
            "point": [float(value) for value in evidence["point"]],
            "source_ref": str(evidence["source_ref"]),
            "source_url": f"https://www.openstreetmap.org/{source_object}",
            "source_object": source_object,
            "osm_type": "node",
            "osm_id": osm_id,
            "priority": int(evidence.get("priority", -10)),
            "label_required": bool(evidence.get("label_required", False)),
            "paths": [],
            "elevation_m": copy.deepcopy(evidence["elevation_m"]),
            "elevation_method": "osm-ele-tag",
            "elevation_source_ref": str(evidence["source_ref"]),
            "elevation_source_object": source_object,
            "elevation_source_object_version": evidence["version"],
            "elevation_source_object_timestamp": evidence["timestamp"],
            "elevation_source_snapshot_sha256": evidence["snapshot_sha256"],
        }
        if evidence.get("distance_to_route_m") is not None:
            feature["distance_to_route_m"] = round(
                float(evidence["distance_to_route_m"]), 1
            )
        if evidence.get("route_axis_fraction") is not None:
            feature["route_axis_fraction"] = round(
                float(evidence["route_axis_fraction"]), 6
            )
        snapshot = {
            "source_object": source_object,
            "version": evidence["version"],
            "timestamp": evidence["timestamp"],
            "retrieved_at": retrieved_at,
            "acquisition_url": (
                f"https://api.openstreetmap.org/api/0.6/{source_object}.json"
            ),
            "canonical_snapshot_sha256": evidence["snapshot_sha256"],
        }
        for subject_id in subject_ids:
            if subject_id not in working_by_id or subject_id not in origin_by_id:
                _fail(
                    f"peak elevation object {source_object} names unknown subject "
                    f"{subject_id!r}"
                )
            working_record = working_by_id[subject_id]
            origin_record = origin_by_id[subject_id]
            for record, is_working in (
                (origin_record, False),
                (working_record, True),
            ):
                sources = _source_index(record, subject_id=subject_id)
                source_ref = str(evidence["source_ref"])
                if source_ref not in sources:
                    _fail(
                        f"{subject_id}: explicit summit {source_object} source_ref "
                        f"{source_ref!r} is absent"
                    )
                features = record["context"]["features"]
                matching = [
                    candidate
                    for candidate in features
                    if isinstance(candidate, dict)
                    and _feature_source_object(candidate) == source_object
                ]
                if len(matching) > 1:
                    _fail(
                        f"{subject_id}: explicit summit {source_object} is duplicated"
                    )
                if matching:
                    _assert_feature_identity(
                        matching[0],
                        feature,
                        subject_id=subject_id,
                        feature_id=str(feature["id"]),
                    )
                else:
                    features.append(copy.deepcopy(feature))
                    if is_working:
                        injected += 1
                        feature_count = sources[source_ref].get("feature_count")
                        if isinstance(feature_count, int) and not isinstance(
                            feature_count, bool
                        ):
                            sources[source_ref]["feature_count"] = feature_count + 1
                if is_working:
                    snapshots = sources[source_ref].setdefault(
                        "explicit_node_snapshots", []
                    )
                    if not isinstance(snapshots, list):
                        _fail(
                            f"{subject_id}: {source_ref} explicit_node_snapshots "
                            "must be an array"
                        )
                    matching_snapshots = [
                        item
                        for item in snapshots
                        if isinstance(item, dict)
                        and item.get("source_object") == source_object
                    ]
                    if matching_snapshots and matching_snapshots != [snapshot]:
                        _fail(
                            f"{subject_id}: explicit summit {source_object} snapshot "
                            "evidence drifted"
                        )
                    if not matching_snapshots:
                        snapshots.append(copy.deepcopy(snapshot))
    return injected


def _apply_peak_overlay(
    records: Sequence[dict[str, Any]],
    overlay: dict[str, Any] | None,
) -> tuple[int, int]:
    if overlay is None:
        return 0, 0
    objects = _validate_peak_overlay(overlay)
    features_by_object: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        subject_id = str(record.get("id") or "<unknown>")
        for feature in _feature_index(record, subject_id=subject_id).values():
            source_object = _feature_source_object(feature)
            if source_object is not None:
                features_by_object.setdefault(source_object, []).append(feature)
    applied_features = 0
    matched_objects = 0
    for source_object, evidence in objects.items():
        matching = features_by_object.get(source_object, [])
        if not matching:
            _fail(f"peak elevation object {source_object} matches no catalog feature")
        matched_objects += 1
        for feature in matching:
            source_ref = feature.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref:
                _fail(f"peak feature {source_object} has no existing OSM source_ref")
            _clear_elevation_fields(feature)
            feature["elevation_m"] = copy.deepcopy(evidence["elevation_m"])
            feature["elevation_method"] = "osm-ele-tag"
            feature["elevation_source_ref"] = source_ref
            feature["elevation_source_object"] = source_object
            feature["elevation_source_object_version"] = evidence["version"]
            feature["elevation_source_object_timestamp"] = evidence["timestamp"]
            if evidence.get("snapshot_sha256"):
                feature["elevation_source_snapshot_sha256"] = evidence[
                    "snapshot_sha256"
                ]
            applied_features += 1
    return matched_objects, applied_features


def _validate_source_integrity(record: dict[str, Any], *, subject_id: str) -> None:
    sources = _source_index(record, subject_id=subject_id)
    missing = sorted(_referenced_source_ids(record) - set(sources))
    if missing:
        _fail(
            f"{subject_id}: final evidence refers to absent source(s): "
            + ", ".join(missing)
        )


def apply_source_precedence(
    release_catalog: dict[str, Any],
    legacy_records: Sequence[dict[str, Any]],
    expansion_catalog: dict[str, Any],
    *,
    peak_elevations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deep-copied release with stronger factual sources restored."""

    release_by_id = _records_by_id(
        release_catalog,
        label="release catalog",
        expected_id=RELEASE_ID,
        expected_count=40,
    )
    legacy_by_id = _legacy_records_by_id(legacy_records)
    expansion_by_id = _records_by_id(
        expansion_catalog,
        label="expansion catalog",
        expected_id=EXPANSION_ID,
        expected_count=30,
    )
    if set(expansion_by_id) & set(legacy_by_id):
        _fail("legacy and expansion subject IDs overlap")
    if set(release_by_id) != {*legacy_by_id, *expansion_by_id}:
        _fail("release IDs are not the exact union of legacy and expansion inputs")

    # Fail closed on the complete identity inventory before swapping a single Z.
    for subject_id, release_record in release_by_id.items():
        source_record = legacy_by_id.get(subject_id) or expansion_by_id[subject_id]
        _assert_route_identity(
            release_record,
            source_record,
            subject_id=subject_id,
        )
        _assert_global_terrain_extent_identity(
            release_record,
            subject_id=subject_id,
        )

    overlay_objects = (
        _validate_peak_overlay(peak_elevations)
        if peak_elevations is not None
        else {}
    )
    result = copy.deepcopy(release_catalog)
    working_by_id = {
        str(record["id"]): record
        for record in result["plates"]
        if isinstance(record, dict)
    }
    origin_by_id = {
        subject_id: copy.deepcopy(
            legacy_by_id.get(subject_id) or expansion_by_id[subject_id]
        )
        for subject_id in working_by_id
    }
    injected_peak_features = _inject_missing_peak_features(
        working_by_id,
        origin_by_id,
        overlay_objects,
        retrieved_at=(
            str(peak_elevations.get("retrieved_at") or "")
            if peak_elevations is not None
            else ""
        ),
    )
    native_profile_count = 0
    explicit_feature_count = 0
    removed_feature_count = 0
    global_relief_count = 0
    for subject_id, record in working_by_id.items():
        origin = origin_by_id[subject_id]
        restored, removed = _restore_feature_elevations(
            record,
            origin,
            subject_id=subject_id,
        )
        explicit_feature_count += restored
        removed_feature_count += removed
        native_terrain = subject_id in legacy_by_id
        native_profile = False
        global_relief = False
        relief_selection: dict[str, Any] | None = None
        if native_terrain:
            release_context = record.get("context")
            if not isinstance(release_context, dict):
                _fail(f"{subject_id}: release context must be an object")
            global_terrain = release_context.get("terrain")
            if not isinstance(global_terrain, dict) or not str(
                global_terrain.get("source_ref") or ""
            ).startswith(GLOBAL_TERRAIN_SOURCE_PREFIX):
                _fail(f"{subject_id}: legacy relief comparison needs global terrain")
            global_terrain = copy.deepcopy(global_terrain)
            _restore_native_terrain(
                record,
                legacy_by_id[subject_id],
                subject_id=subject_id,
            )
            native_bundle = record["context"]["terrain"]
            global_relief, relief_selection = _select_relief_terrain(
                native=native_bundle,
                global_candidate=global_terrain,
                map_extent=record["context"]["extent"],
            )
            if global_relief:
                record["context"]["relief_terrain"] = global_terrain
                global_relief_count += 1
            else:
                record["context"].pop("relief_terrain", None)
            native_profile = _restore_route_elevation(
                record,
                legacy_by_id[subject_id],
                subject_id=subject_id,
            )
            native_profile_count += int(native_profile)
            _ensure_sources(
                record,
                legacy_by_id[subject_id],
                _referenced_source_ids(record["route"]),
                subject_id=subject_id,
            )
        global_profile = str(
            record.get("route", {}).get("elevation_source_ref") or ""
        ).startswith(GLOBAL_TERRAIN_SOURCE_PREFIX)
        _sanitize_global_derivation(
            record,
            subject_id=subject_id,
            native_terrain=native_terrain,
            global_profile_retained=global_profile,
            global_relief_retained=global_relief,
            relief_selection=relief_selection,
            removed_feature_elevations=removed,
        )
        if native_terrain:
            _update_legacy_notes_and_credit(
                record,
                legacy_by_id[subject_id],
                global_profile_retained=global_profile,
                global_relief_retained=global_relief,
            )
        _ensure_visible_route_attribution(record)

    overlay_objects, overlay_features = _apply_peak_overlay(
        list(working_by_id.values()),
        peak_elevations,
    )
    retained_global_sources = 0
    for subject_id, record in working_by_id.items():
        retained = _relabel_or_prune_global_sources(
            record,
            subject_id=subject_id,
        )
        retained_global_sources += len(retained)
        if subject_id in legacy_by_id and not retained:
            notes = record.get("notes", [])
            if isinstance(notes, list):
                notes[:] = [note for note in notes if note != GLOBAL_REVIEW_NOTE]
        _ensure_visible_odbl_attribution(record)
        _bind_variant_terrain_credits(record)
        _validate_source_integrity(record, subject_id=subject_id)

    result["source_precedence"] = {
        "policy_id": POLICY_ID,
        "legacy_native_terrain_records": len(legacy_by_id),
        "legacy_native_profile_records": native_profile_count,
        "global_route_profile_records": sum(
            str(record.get("route", {}).get("elevation_source_ref") or "").startswith(
                GLOBAL_TERRAIN_SOURCE_PREFIX
            )
            for record in working_by_id.values()
        ),
        "global_relief_terrain_records": global_relief_count,
        "explicit_feature_elevations_restored": explicit_feature_count,
        "global_feature_elevations_removed": removed_feature_count,
        "peak_overlay_objects_matched": overlay_objects,
        "peak_overlay_feature_instances_applied": overlay_features,
        "peak_overlay_features_injected": injected_peak_features,
        "retained_global_source_records": retained_global_sources,
    }
    return result


def apply_peak_overlay_only(
    release_catalog: dict[str, Any],
    peak_elevations: dict[str, Any],
    *,
    subject_id: str,
) -> dict[str, Any]:
    """Apply newly audited peak evidence to one already-precedenced release.

    This narrow mode is for a release whose native/global precedence has
    already been resolved.  It must not replay that broader merge from an
    older global intermediate merely to admit one later frozen OSM node.
    """

    records = _records_by_id(
        release_catalog,
        label="release catalog",
        expected_id=RELEASE_ID,
        expected_count=40,
    )
    if subject_id not in records:
        _fail(f"overlay-only subject {subject_id!r} is absent")
    validated_objects = _validate_peak_overlay(peak_elevations)
    selected_objects = {
        source_object: evidence
        for source_object, evidence in validated_objects.items()
        if subject_id in evidence.get("subject_ids", [])
    }
    if not selected_objects:
        _fail(f"overlay-only subject {subject_id!r} has no scoped peak evidence")
    selected_overlay = copy.deepcopy(peak_elevations)
    selected_overlay["objects"] = list(selected_objects.values())

    result = copy.deepcopy(release_catalog)
    working_by_id = {
        str(record["id"]): record
        for record in result["plates"]
        if isinstance(record, dict)
    }
    selected_working = {subject_id: working_by_id[subject_id]}
    origins = {subject_id: copy.deepcopy(working_by_id[subject_id])}
    injected = _inject_missing_peak_features(
        selected_working,
        origins,
        selected_objects,
        retrieved_at=str(peak_elevations.get("retrieved_at") or ""),
    )
    matched, applied = _apply_peak_overlay(
        list(selected_working.values()),
        selected_overlay,
    )
    _validate_source_integrity(
        selected_working[subject_id],
        subject_id=subject_id,
    )
    summary = result.setdefault("source_precedence", {})
    if not isinstance(summary, dict):
        _fail("release source_precedence summary must be an object")
    if injected:
        for key, increment in (
            ("explicit_feature_elevations_restored", injected),
            ("peak_overlay_objects_matched", matched),
            ("peak_overlay_feature_instances_applied", applied),
            ("peak_overlay_features_injected", injected),
        ):
            previous = summary.get(key, 0)
            if not isinstance(previous, int) or isinstance(previous, bool):
                _fail(f"release source_precedence.{key} must be an integer")
            summary[key] = previous + increment
    return result


def apply_visible_attribution_only(
    release_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Normalize print credits without replaying any terrain precedence.

    Later factual refinements can legitimately change route segmentation or
    terrain-density selections after the broad precedence pass.  This narrow,
    idempotent mode updates only visible credit copy and the AP6 source-use
    description, leaving every geometry/elevation field byte-for-byte intact.
    """

    _records_by_id(
        release_catalog,
        label="release catalog",
        expected_id=RELEASE_ID,
        expected_count=40,
    )
    result = copy.deepcopy(release_catalog)
    for record in result["plates"]:
        subject_id = str(record.get("id") or "<unknown>")
        _ensure_visible_odbl_attribution(record)
        _validate_source_integrity(record, subject_id=subject_id)
    return result


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _fail(f"could not write {path}: {exc}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--expansion", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-catalog", type=Path)
    parser.add_argument("--legacy-context", type=Path)
    parser.add_argument(
        "--peak-elevations",
        type=Path,
        help="Optional audited hike-explicit-elevations-v1 object overlay.",
    )
    parser.add_argument(
        "--peak-overlay-only-subject",
        help=(
            "Apply only scoped peak evidence for one subject in an already-"
            "precedenced release."
        ),
    )
    parser.add_argument(
        "--visible-attribution-only",
        action="store_true",
        help=(
            "Normalize the installed release's plotted ODbL credit without "
            "replaying geometry, elevation, or terrain precedence."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        release = _load_json_object(args.release, label="release catalog")
        expansion = (
            _load_json_object(args.expansion, label="expansion catalog")
            if args.expansion is not None
            else None
        )
        peak_elevations = (
            _load_json_object(args.peak_elevations, label="peak elevations")
            if args.peak_elevations is not None
            else None
        )
        if args.visible_attribution_only:
            if args.peak_overlay_only_subject or expansion is not None:
                _fail(
                    "--visible-attribution-only cannot be combined with "
                    "--expansion or --peak-overlay-only-subject"
                )
            result = apply_visible_attribution_only(release)
        elif args.peak_overlay_only_subject:
            if peak_elevations is None:
                _fail("--peak-overlay-only-subject requires --peak-elevations")
            result = apply_peak_overlay_only(
                release,
                peak_elevations,
                subject_id=args.peak_overlay_only_subject,
            )
        else:
            if expansion is None:
                _fail("full source precedence requires --expansion")
            result = apply_source_precedence(
                release,
                load_legacy_records(
                    catalog_path=args.legacy_catalog,
                    context_path=args.legacy_context,
                ),
                expansion,
                peak_elevations=peak_elevations,
            )
        _write_atomic(args.output, result)
    except SourcePrecedenceError as exc:
        print(f"apply_hiking_source_precedence: {exc}", file=sys.stderr)
        return 2
    action = (
        "Normalized visible hiking attribution"
        if args.visible_attribution_only
        else "Applied factual source precedence to 40 routes"
    )
    print(f"{action} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
