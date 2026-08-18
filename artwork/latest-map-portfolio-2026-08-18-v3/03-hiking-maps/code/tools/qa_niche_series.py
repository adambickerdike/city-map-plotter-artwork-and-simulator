#!/usr/bin/env python3
"""Fail-closed QA for the paired 40-route geographic hiking-map release."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "tools" / "validate_format.py"
PLOTSIM = ROOT / "tools" / "plotsim.py"
PYTHON = ROOT / ".venv" / "bin" / "python"
SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

EXPECTED_VARIANTS = ("detailed-map", "terrain-relief")
EXPECTED_SUBJECT_COUNT = 40
EXPECTED_ARTIFACT_COUNT = EXPECTED_SUBJECT_COUNT * len(EXPECTED_VARIANTS)

A5_FORMATS: dict[str, tuple[float, float, str]] = {
    "a5-portrait": (148.0, 210.0, "portrait"),
    "a5-landscape": (210.0, 148.0, "landscape"),
}

REQUIRED_LOGICAL_LAYERS = frozenset(
    {
        "context_water",
        "context_relief",
        "context_markers",
        "context_labels",
        "hero_route",
        "plate_copy",
        "plate_attribution",
    }
)
WATER_ROLES = frozenset(
    {
        "source-sampled-lake-boundary",
        "source-sampled-marine-shoreline",
        "source-sampled-coastline",
        "source-sampled-river-centreline",
        "source-anchored-water-symbol",
    }
)
ROAD_ROLES = frozenset(
    {
        "source-sampled-road-major",
        "source-sampled-road-secondary",
        "source-sampled-road-local",
        "source-sampled-road-track",
    }
)
LAND_COVER_ROLES = frozenset(
    {
        "source-sampled-landcover-boundary",
        "source-bounded-landcover-band",
        "source-bounded-woodland-symbol",
        "source-anchored-woodland-symbol",
        "source-bounded-grass-symbol",
        "source-anchored-grass-symbol",
    }
)
GREEN_POST_MASK_FINAL_COUNT_POLICY_ID = (
    "unique-rendered-source-feature-ids-after-copy-mask-v1"
)
CONTOUR_COPY_MASK_POLICY_ID = "boxed-contour-copy-geography-mask-v1"
GREEN_POST_MASK_TRIGGER_FIELDS = frozenset(
    {
        "pre_copy_mask_final_rendered_feature_count",
        "copy_mask_affected_landcover_record_count",
        "copy_mask_omitted_landcover_record_count",
        "copy_mask_emitted_landcover_path_part_count",
        "copy_mask_omitted_feature_count",
        "post_copy_mask_rendered_feature_ids",
        "final_count_policy",
    }
)
GREEN_POST_MASK_REQUIRED_FIELDS = GREEN_POST_MASK_TRIGGER_FIELDS | {
    "final_rendered_feature_count"
}
FAMILY_EVIDENCE_NAMES = frozenset({"roads", "hydrography", "landcover"})
FAMILY_FEATURE_KINDS = {
    "roads": frozenset({"road"}),
    "hydrography": frozenset({"river", "water", "coast", "sea"}),
    "landcover": frozenset({"woodland", "grass"}),
}
FULL_FIELD_CONTEXT_POLICY_ID = "full-field-continuous-context-v2"
SOURCE_PRECEDENCE_POLICY_ID = "hiking-factual-source-precedence-v1"
DETAILED_TERRAIN_FALLBACK_POLICY_ID = (
    "full-field-relief-fallback-for-sparse-native-context-v1"
)
GLOBAL_TERRAIN_SOURCE_PREFIX = "aws-mapzen-terrarium-"
EXPLICIT_ELEVATION_METHODS = frozenset(
    {
        "osm-ele-tag",
        "authoritative-gazetteer-height",
        "official-source-elevation",
    }
)
CHAINAGE_STATIONS = {
    "A": 0.0,
    "B": 0.25,
    "C": 0.5,
    "D": 0.75,
    "E": 1.0,
}
CHAINAGE_SHARED_ATTRIBUTES = (
    "data-chainage-id",
    "data-chainage-m",
    "data-distance-km",
    "data-measured-chainage-m",
    "data-displayed-distance-m",
    "data-displayed-distance-km",
    "data-route-fraction",
    "data-longitude",
    "data-latitude",
    "data-elevation-m",
    "data-elevation-status",
    "data-source-vertex-before",
    "data-source-vertex-after",
    "data-source-segment-fraction",
    "data-chainage-basis",
    "data-distance-label-basis",
    "data-route-source-ref",
    "data-profile-status",
    "data-official-total-distance-km",
    "data-elevation-source-ref",
    "data-elevation-method",
    "data-elevation-datum",
)
PROFILE_EXTREMA_APPROXIMATE_STATUS = "sampled-approximate"
PROFILE_EXTREMA_EXACT_STATUS = "source-verified-exact"
PROFILE_EXTREMA_APPROXIMATE_POLICY_ID = "sampled-elevation-approximate-extrema-v1"
PROFILE_EXTREMA_EXACT_POLICY_ID = "source-verified-exact-extrema-v1"
FORBIDDEN_PAIRED_ROLES = frozenset(
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
# Kept as metadata vocabulary for catalogues produced before the v4 full-field
# contract.  Paired v4 artwork must never emit the corresponding marks.
FALL_LINE_RUNTIME_RENDERING_POLICY_ID = (
    "factual-dem-fall-lines-runtime-clearance-cluster-v1"
)
FALL_LINE_NO_CLUSTER_OMISSION_REASON = "no-legible-cluster-after-clearance"
FALL_LINE_NO_SOURCE_OMISSION_REASON = "no-frozen-fall-lines"
RELIEF_ROLES = frozenset(
    {
        "source-derived-upland-boundary",
        "source-derived-upland-band",
        "source-derived-elevation-mask-hachure",
        "source-derived-dem-fall-line-hachure",
        "source-derived-dtm-contour",
        "source-sampled-contour",
        "stylized-relief-formline",
        "stylized-ridge-symbol",
    }
)
RELIEF_STATUSES = frozenset(
    {
        "stylized-source-anchored",
        "stylized-source-area-anchored",
        "source-anchored",
        "source-sampled",
        "source-derived-dtm",
        "stylized-point-symbol",
    }
)
MARKER_ROLES = frozenset(
    {
        "settlement-marker",
        "peak-marker",
        "range-marker",
        "pass-marker",
        "hut-marker",
    }
)
MARKER_LABEL_ROLES = frozenset(
    {"settlement-label", "hut-label", "pass-label", "peak-label"}
)
ROUTE_STATION_RESERVATION_POLICY_ID = "exact-route-station-copy-clearance-v1"
ROUTE_STATION_RESERVATION_RADIUS_MM = 1.65
PLACE_LABEL_ROLES = frozenset({"settlement-label", "hut-label"})
MOUNTAIN_LABEL_ROLES = frozenset({"peak-label", "range-label", "pass-label"})
HYDRO_LABEL_ROLES = frozenset({"water-label", "sea-label"})
CONTEXT_LABEL_ROLES = PLACE_LABEL_ROLES | MOUNTAIN_LABEL_ROLES | HYDRO_LABEL_ROLES
CONTOUR_LABEL_ROLES = frozenset(
    {
        "source-derived-contour-altitude-label",
        "source-derived-contour-altitude-key",
    }
)
LABEL_LEADER_ROLES = frozenset(
    {"context-label-leader", "source-derived-contour-altitude-leader"}
)
LABEL_LEADER_ROUTING_POLICY_ID = "foreign-copy-route-and-leader-clearance-v1"
MINIMUM_LABEL_LEADER_CLEARANCE_MM = 0.30
LABEL_GEOGRAPHY_LAYERS = frozenset(
    {
        "context_roads",
        "context_water",
        "context_woodland",
        "context_landcover",
        "context_relief",
        "context_relief_index",
        "context_designations",
        "hero_route",
    }
)
LABEL_GEOGRAPHY_ROLE_FILTERS = {
    # This layer also owns detail-inset frames and north arrows. Those are
    # page furniture, not geographic context, and are intentionally outside
    # the contour-copy mask applied by the renderer.
    "context_designations": frozenset({"source-sampled-designation-boundary"}),
}
CONTEXT_LOGICAL_LAYERS = frozenset(
    {
        "context_water",
        "context_woodland",
        "context_landcover",
        "context_roads",
        "context_relief",
        "context_relief_index",
        "context_designations",
        "context_markers",
        "context_labels",
    }
)
CONTEXT_ATTRIBUTES = {
    "data-context-status": "curated-source-sampled-art-context",
    "data-geometry-status": "generalized-not-for-navigation",
    "data-navigation-status": "artwork-not-for-navigation",
}
_OSM_ELEMENT = re.compile(r"^(?:node|way|relation)/[1-9][0-9]*$")
_MM = re.compile(r"^([0-9]+(?:\.[0-9]+)?)mm$")
_SVG_NUMBER_PATTERN = r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
_SVG_MOVE_PAIR = re.compile(
    rf"\s*M\s*({_SVG_NUMBER_PATTERN})(?:\s*,\s*|\s+)({_SVG_NUMBER_PATTERN})"
)
_SVG_LINE_PAIR = re.compile(
    rf"\s*L\s*({_SVG_NUMBER_PATTERN})(?:\s*,\s*|\s+)({_SVG_NUMBER_PATTERN})"
)
_SVG_LINEAR_TOKEN = re.compile(rf"\s*(?:,\s*)?([MLZ]|{_SVG_NUMBER_PATTERN})")


def _release_subject_ids() -> frozenset[str]:
    """Resolve the release inventory from the production hiking catalogue."""

    from city_map_plotter.hike_plates import load_hike_release_catalog

    records = load_hike_release_catalog()
    identifiers = [
        str(record.get("id", "")).strip()
        for record in records
        if isinstance(record, dict)
    ]
    if (
        len(records) != EXPECTED_SUBJECT_COUNT
        or len(identifiers) != EXPECTED_SUBJECT_COUNT
        or any(not identifier for identifier in identifiers)
        or len(set(identifiers)) != EXPECTED_SUBJECT_COUNT
    ):
        raise ValueError(
            "the hiking release catalogue must contain exactly 40 unique subject IDs"
        )
    return frozenset(identifiers)


def _catalog_declares_landcover(manifest: dict[str, Any]) -> bool:
    catalog_record = manifest.get("catalog_record") or {}
    context = catalog_record.get("context") if isinstance(catalog_record, dict) else {}
    features = context.get("features") if isinstance(context, dict) else []
    derived = context.get("landcover") if isinstance(context, dict) else None
    if isinstance(derived, dict) and derived.get("features"):
        return True
    return any(
        isinstance(feature, dict)
        and str(feature.get("kind", "")).casefold()
        in {
            "woodland",
            "grass",
            "landcover",
            "vegetation",
            "forest",
            "green_space",
        }
        and bool(feature.get("paths"))
        for feature in (features or [])
    )


def _catalog_family_evidence(
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]] | None:
    catalog_record = manifest.get("catalog_record") or {}
    context = catalog_record.get("context") if isinstance(catalog_record, dict) else {}
    raw = context.get("family_evidence") if isinstance(context, dict) else None
    if not isinstance(raw, list):
        return None
    return {
        str(item.get("family")): item
        for item in raw
        if isinstance(item, dict) and item.get("family")
    }


def _terrain_for_variant(
    context: dict[str, Any],
    variant_id: str,
    *,
    rendering: dict[str, Any],
    catalog_record: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, bool]:
    """Return the terrain bundle that the renderer uses for an edition.

    A terrain-relief edition uses the independently selected
    ``relief_terrain`` when present and otherwise falls back to ``terrain``.
    Detailed maps normally use native ``terrain``; the one permitted exception
    is an evidence-backed sparse-native policy that selects the same frozen
    ``relief_terrain`` with the detailed renderer's stricter density cap.
    """

    policy = rendering.get("detailed_terrain_source_policy")
    if variant_id == "terrain-relief":
        relief = context.get("relief_terrain")
        if isinstance(relief, dict):
            return "relief_terrain", relief, policy is None
    elif variant_id == "detailed-map" and policy is not None:
        native = context.get("terrain")
        relief = context.get("relief_terrain")
        derivation = catalog_record.get("terrain_derivation")
        precedence = (
            derivation.get("source_precedence")
            if isinstance(derivation, dict)
            else None
        )
        selection = (
            precedence.get("relief_terrain_selection")
            if isinstance(precedence, dict)
            else None
        )
        native_evidence = (
            selection.get("native") if isinstance(selection, dict) else None
        )
        relief_evidence = (
            selection.get("global") if isinstance(selection, dict) else None
        )
        native_length = (
            native_evidence.get("normalized_full_field_length")
            if isinstance(native_evidence, dict)
            else None
        )
        relief_length = (
            relief_evidence.get("normalized_full_field_length")
            if isinstance(relief_evidence, dict)
            else None
        )
        native_levels = (
            native_evidence.get("contour_level_count")
            if isinstance(native_evidence, dict)
            else None
        )
        relief_levels = (
            relief_evidence.get("contour_level_count")
            if isinstance(relief_evidence, dict)
            else None
        )
        selection_is_eligible = (
            isinstance(native_length, (int, float))
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
        )
        valid = (
            isinstance(policy, dict)
            and policy.get("policy_id") == DETAILED_TERRAIN_FALLBACK_POLICY_ID
            and isinstance(native, dict)
            and isinstance(relief, dict)
            and bool(str(native.get("source_ref") or ""))
            and bool(str(relief.get("source_ref") or ""))
            and policy.get("native_source_ref") == native.get("source_ref")
            and policy.get("selected_source_ref") == relief.get("source_ref")
            and policy.get("selection_evidence") == selection
            and selection_is_eligible
        )
        # Return the declared candidate even when invalid so downstream
        # contour/source checks remain diagnostic; the explicit boolean keeps
        # the metadata switch fail-closed.
        return (
            "relief_terrain",
            relief if isinstance(relief, dict) else None,
            valid,
        )
    terrain = context.get("terrain")
    return "terrain", terrain if isinstance(terrain, dict) else None, policy is None


def _terrain_release_provenance_refs(
    catalog_record: dict[str, Any],
) -> set[str]:
    """Collect terrain sources that remain intentionally release-referenced.

    A paired release can retain a native terrain source for the detailed map
    and a denser global source for the relief map.  Neither is superseded when
    it is named by a frozen terrain bundle or the explicit source-precedence
    record.  An arbitrary source-list entry still does not qualify.
    """

    refs: set[str] = set()
    context = catalog_record.get("context")
    if isinstance(context, dict):
        for field in ("terrain", "relief_terrain"):
            bundle = context.get(field)
            if isinstance(bundle, dict) and bundle.get("source_ref"):
                refs.add(str(bundle["source_ref"]))
    derivation = catalog_record.get("terrain_derivation")
    precedence = (
        derivation.get("source_precedence") if isinstance(derivation, dict) else None
    )
    if isinstance(precedence, dict):
        for field in (
            "terrain_source_ref",
            "relief_terrain_source_ref",
            "route_elevation_source_ref",
        ):
            if precedence.get(field):
                refs.add(str(precedence[field]))
    return refs


def _path_feature_ids(path: ET.Element) -> set[str]:
    """Read one or a stitched union of frozen feature IDs from an SVG path."""

    raw = path.get("data-feature-ids") or path.get("data-feature-id") or ""
    return {item for item in raw.split(",") if item}


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        raise ValueError(f"{path} is not a valid PNG")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def _run(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rewrite_checksums(series_dir: Path) -> None:
    destination = series_dir / "CHECKSUMS.sha256"
    candidates = sorted(
        path for path in series_dir.rglob("*") if path.is_file() and path != destination
    )
    destination.write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(series_dir)}\n" for path in candidates
        ),
        encoding="ascii",
    )


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _source_has_frozen_snapshot(source: dict[str, Any]) -> bool:
    """Accept frozen raw evidence or an exact, reproducible raster window."""

    dual_route_hash_fields = (
        "waymarked_snapshot_sha256",
        "osm_relation_snapshot_sha256",
    )
    declares_dual_route_evidence = any(
        field in source for field in dual_route_hash_fields
    ) or (
        source.get("id") == "osm-route"
        and "waymarked trails" in str(source.get("publisher", "")).casefold()
    )
    if declares_dual_route_evidence:
        relation_id = source.get("relation_id")
        relation_version = source.get("relation_version")
        relation_timestamp = str(source.get("relation_timestamp") or "")
        acquisition_url = str(source.get("acquisition_url") or "")
        route_url = str(source.get("url") or "")
        try:
            parsed_timestamp = datetime.fromisoformat(
                relation_timestamp.removesuffix("Z") + "+00:00"
            )
        except ValueError:
            parsed_timestamp = None
        parsed_offset = (
            parsed_timestamp.utcoffset() if parsed_timestamp is not None else None
        )
        acquisition = urlsplit(acquisition_url)
        return (
            all(
                re.fullmatch(r"[0-9a-f]{64}", str(source.get(field) or "")) is not None
                for field in dual_route_hash_fields
            )
            and isinstance(relation_id, int)
            and not isinstance(relation_id, bool)
            and relation_id > 0
            and isinstance(relation_version, int)
            and not isinstance(relation_version, bool)
            and relation_version > 0
            and re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
                r"[0-9]{2}(?:\.[0-9]+)?Z",
                relation_timestamp,
            )
            is not None
            and relation_timestamp.endswith("Z")
            and parsed_timestamp is not None
            and parsed_offset is not None
            and parsed_offset.total_seconds() == 0.0
            and route_url == f"https://www.openstreetmap.org/relation/{relation_id}"
            and acquisition.scheme == "https"
            and acquisition.netloc == "hiking.waymarkedtrails.org"
            and acquisition.path == f"/api/v1/details/relation/{relation_id}"
            and not acquisition.fragment
            and bool(str(source.get("retrieved_at") or "").strip())
        )

    aggregate = str(
        source.get("snapshot_sha256")
        or source.get("raw_snapshot_sha256")
        or source.get("query_set_sha256")
        or ""
    )
    if re.fullmatch(r"[0-9a-f]{64}", aggregate):
        return True
    derived_window = str(source.get("derived_window_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", derived_window):
        raster_url = str(source.get("source_raster_url") or "")
        valid_fraction = source.get("derived_window_valid_fraction")
        return (
            raster_url.startswith("https://")
            and bool(str(source.get("retrieved_at") or "").strip())
            and isinstance(valid_fraction, (int, float))
            and not isinstance(valid_fraction, bool)
            and 0.0 < float(valid_fraction) <= 1.0
        )
    included = [
        item
        for item in (source.get("source_files") or [])
        if isinstance(item, dict) and item.get("included") is not False
    ]
    return bool(included) and all(
        re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))) for item in included
    )


def _close(left: Any, right: float, *, tolerance: float = 1e-6) -> bool:
    return (
        isinstance(left, (int, float))
        and math.isfinite(float(left))
        and math.isclose(float(left), right, rel_tol=0.0, abs_tol=tolerance)
    )


def _millimetres(value: str | None) -> float | None:
    match = _MM.fullmatch(value or "")
    return float(match.group(1)) if match else None


def _valid_coverage_measurement(value: Any) -> bool:
    """Coverage is measured for review, never compared with a budget gate."""

    return (
        isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _physical_layers(root: ET.Element) -> list[ET.Element]:
    return [
        child
        for child in root
        if child.tag == f"{{{SVG_NS}}}g"
        and child.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer"
    ]


def _paths_by_logical_layer(root: ET.Element) -> dict[str, list[ET.Element]]:
    result: dict[str, list[ET.Element]] = {}
    for physical in _physical_layers(root):
        for logical in physical:
            if logical.tag != f"{{{SVG_NS}}}g":
                continue
            logical_id = logical.get("data-logical-layer")
            if not logical_id:
                continue
            paths = list(logical.findall(f".//{{{SVG_NS}}}path"))
            result.setdefault(logical_id, []).extend(paths)
    return result


def _roles(paths: list[ET.Element]) -> set[str]:
    return {role for path in paths if (role := path.get("data-role")) is not None}


def _parse_label_box(value: str | None) -> tuple[float, float, float, float] | None:
    try:
        numbers = tuple(float(part) for part in str(value).split(","))
    except (TypeError, ValueError):
        return None
    if (
        len(numbers) != 4
        or not all(math.isfinite(number) for number in numbers)
        or numbers[2] <= 0
        or numbers[3] <= 0
    ):
        return None
    return numbers  # type: ignore[return-value]


def _boxes_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tolerance: float = 0.05,
) -> bool:
    left_x, left_y, left_width, left_height = left
    right_x, right_y, right_width, right_height = right
    return not (
        left_x + left_width <= right_x + tolerance
        or right_x + right_width <= left_x + tolerance
        or left_y + left_height <= right_y + tolerance
        or right_y + right_height <= left_y + tolerance
    )


def _parse_linear_ml_path(value: str | None) -> list[tuple[float, float]] | None:
    """Parse the absolute, linear path subset emitted for label leaders."""

    if not isinstance(value, str) or not value.strip():
        return None
    move = _SVG_MOVE_PAIR.match(value)
    if move is None:
        return None
    points = [(float(move.group(1)), float(move.group(2)))]
    position = move.end()
    while position < len(value):
        line = _SVG_LINE_PAIR.match(value, position)
        if line is None:
            if not value[position:].strip():
                position = len(value)
                break
            return None
        points.append((float(line.group(1)), float(line.group(2))))
        position = line.end()
    if (
        len(points) < 2
        or not all(
            math.isfinite(coordinate) for point in points for coordinate in point
        )
        or all(
            math.hypot(end[0] - start[0], end[1] - start[1]) <= 1e-12
            for start, end in zip(points, points[1:])
        )
    ):
        return None
    return points


def _parse_absolute_linear_subpaths(
    value: str | None,
) -> list[list[tuple[float, float]]] | None:
    """Parse the renderer's absolute M/L/Z geography path subset exactly."""

    if not isinstance(value, str) or not value.strip():
        return None
    tokens: list[str] = []
    position = 0
    while position < len(value):
        token = _SVG_LINEAR_TOKEN.match(value, position)
        if token is None:
            if not value[position:].strip():
                break
            return None
        tokens.append(token.group(1))
        position = token.end()

    subpaths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    start: tuple[float, float] | None = None
    index = 0
    while index < len(tokens):
        command = tokens[index]
        index += 1
        if command == "Z":
            if not current or start is None:
                return None
            if current[-1] != start:
                current.append(start)
            if len(current) >= 2:
                subpaths.append(current)
            current = []
            start = None
            continue
        if command not in {"M", "L"} or index + 1 >= len(tokens):
            return None
        try:
            point = (float(tokens[index]), float(tokens[index + 1]))
        except ValueError:
            return None
        index += 2
        if not all(math.isfinite(coordinate) for coordinate in point):
            return None
        if command == "M":
            if len(current) >= 2:
                subpaths.append(current)
            current = [point]
            start = point
        elif not current:
            return None
        else:
            current.append(point)
    if len(current) >= 2:
        subpaths.append(current)
    return subpaths or None


def _point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator <= 1e-24:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    fraction = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator,
        ),
    )
    nearest = (start[0] + fraction * dx, start[1] + fraction * dy)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def _point_is_in_box(
    point: tuple[float, float], box: tuple[float, float, float, float]
) -> bool:
    x, y, width, height = box
    return x <= point[0] <= x + width and y <= point[1] <= y + height


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    epsilon = 1e-12

    def cross(
        origin: tuple[float, float],
        end: tuple[float, float],
        point: tuple[float, float],
    ) -> float:
        return (end[0] - origin[0]) * (point[1] - origin[1]) - (end[1] - origin[1]) * (
            point[0] - origin[0]
        )

    def on_segment(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> bool:
        return (
            min(start[0], end[0]) - epsilon
            <= point[0]
            <= max(start[0], end[0]) + epsilon
            and min(start[1], end[1]) - epsilon
            <= point[1]
            <= max(start[1], end[1]) + epsilon
            and abs(cross(start, end, point)) <= epsilon
        )

    first_side_a = cross(first_start, first_end, second_start)
    first_side_b = cross(first_start, first_end, second_end)
    second_side_a = cross(second_start, second_end, first_start)
    second_side_b = cross(second_start, second_end, first_end)
    if (
        (first_side_a > epsilon and first_side_b < -epsilon)
        or (first_side_a < -epsilon and first_side_b > epsilon)
    ) and (
        (second_side_a > epsilon and second_side_b < -epsilon)
        or (second_side_a < -epsilon and second_side_b > epsilon)
    ):
        return True
    return any(
        (abs(side) <= epsilon and on_segment(point, segment_start, segment_end))
        for side, point, segment_start, segment_end in (
            (first_side_a, second_start, first_start, first_end),
            (first_side_b, second_end, first_start, first_end),
            (second_side_a, first_start, second_start, second_end),
            (second_side_b, first_end, second_start, second_end),
        )
    )


def _segment_box_distance(
    start: tuple[float, float],
    end: tuple[float, float],
    label_box: tuple[float, float, float, float],
) -> float:
    if _point_is_in_box(start, label_box) or _point_is_in_box(end, label_box):
        return 0.0
    x, y, width, height = label_box
    corners = (
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
    )
    edges = tuple(zip(corners, corners[1:] + corners[:1]))
    if any(
        _segments_intersect(start, end, edge_start, edge_end)
        for edge_start, edge_end in edges
    ):
        return 0.0
    return min(
        min(
            _point_segment_distance(start, edge_start, edge_end),
            _point_segment_distance(end, edge_start, edge_end),
            _point_segment_distance(edge_start, start, end),
            _point_segment_distance(edge_end, start, end),
        )
        for edge_start, edge_end in edges
    )


def _polyline_box_distance(
    points: list[tuple[float, float]],
    label_box: tuple[float, float, float, float],
) -> float:
    return min(
        _segment_box_distance(start, end, label_box)
        for start, end in zip(points, points[1:])
    )


def _trim_polyline_start(
    points: list[tuple[float, float]], distance: float
) -> list[tuple[float, float]]:
    """Remove a physical distance from the start of a linear polyline."""

    if distance <= 0.0:
        return list(points)
    remaining = distance
    for index, (start, end) in enumerate(zip(points, points[1:])):
        segment_length = math.hypot(end[0] - start[0], end[1] - start[1])
        if segment_length <= 1e-12:
            continue
        if segment_length <= remaining + 1e-12:
            remaining -= segment_length
            continue
        fraction = remaining / segment_length
        clipped_start = (
            start[0] + fraction * (end[0] - start[0]),
            start[1] + fraction * (end[1] - start[1]),
        )
        return [clipped_start, *points[index + 1 :]]
    return []


def _trim_polyline_endpoint_clearance(
    points: list[tuple[float, float]], clearance: float
) -> list[tuple[float, float]]:
    """Return only the leader interior beyond its allowed endpoint zones."""

    # Exact-boundary contact is permitted, matching the >= clearance rule used
    # for foreign-copy boxes.  The tiny epsilon removes that boundary point from
    # the collision geometry without masking any coordinate-scale crossing.
    trim_distance = max(0.0, clearance) + 1e-9
    trimmed = _trim_polyline_start(points, trim_distance)
    if len(trimmed) < 2:
        return []
    trimmed = list(
        reversed(_trim_polyline_start(list(reversed(trimmed)), trim_distance))
    )
    return trimmed if len(trimmed) >= 2 else []


def _polylines_intersect(
    first: list[tuple[float, float]], second: list[tuple[float, float]]
) -> bool:
    return any(
        _segments_intersect(first_start, first_end, second_start, second_end)
        for first_start, first_end in zip(first, first[1:])
        for second_start, second_end in zip(second, second[1:])
    )


def _path_context_scope(path: ET.Element) -> tuple[str, str]:
    """Keep overview and independent local-detail geometries separate."""

    return (
        str(path.get("data-context-view", "")),
        str(path.get("data-detail-extent", "")),
    )


def _check_label_leader_geometry_clearance(
    *,
    subject_id: str,
    paths_by_layer: dict[str, list[ET.Element]],
    failures: list[str],
) -> None:
    """Reject route and leader crossings outside legitimate anchor endpoints."""

    leaders: list[
        tuple[
            ET.Element,
            str,
            str,
            float,
            tuple[str, str],
            list[tuple[float, float]],
        ]
    ] = []
    all_paths = [path for paths in paths_by_layer.values() for path in paths]
    for index, path in enumerate(all_paths, start=1):
        role = str(path.get("data-role", ""))
        if role not in LABEL_LEADER_ROLES:
            continue
        points = _parse_linear_ml_path(path.get("d"))
        if points is None:
            # The primary leader validation reports the malformed path.
            continue
        identity = str(path.get("data-feature-id", "")).strip() or f"<leader-{index}>"
        declared_clearance = _finite_number(path.get("data-minimum-copy-clearance-mm"))
        clearance = max(
            MINIMUM_LABEL_LEADER_CLEARANCE_MM,
            declared_clearance
            if declared_clearance is not None and declared_clearance >= 0.0
            else 0.0,
        )
        leaders.append(
            (
                path,
                role,
                identity,
                clearance,
                _path_context_scope(path),
                points,
            )
        )

    routes_by_scope: dict[tuple[str, str], list[list[tuple[float, float]]]] = {}
    for path in paths_by_layer.get("hero_route", []):
        if path.get("data-role") != "source-sampled-route":
            continue
        subpaths = _parse_absolute_linear_subpaths(path.get("d"))
        _check(
            subpaths is not None,
            (
                f"{subject_id}: hero route is not an absolute linear M/L/Z path "
                "for leader collision QA"
            ),
            failures,
        )
        if subpaths is not None:
            routes_by_scope.setdefault(_path_context_scope(path), []).extend(subpaths)

    for _path, role, identity, clearance, scope, points in leaders:
        interior = _trim_polyline_endpoint_clearance(points, clearance)
        if len(interior) < 2:
            continue
        if any(
            _polylines_intersect(interior, route)
            for route in routes_by_scope.get(scope, [])
        ):
            _check(
                False,
                (
                    f"{subject_id}: {role} for {identity!r} crosses the hero route "
                    f"beyond its {clearance:.2f} mm endpoint clearance"
                ),
                failures,
            )

    for index, left in enumerate(leaders):
        (
            _left_path,
            left_role,
            left_identity,
            left_clearance,
            left_scope,
            left_points,
        ) = left
        left_interior = _trim_polyline_endpoint_clearance(left_points, left_clearance)
        if len(left_interior) < 2:
            continue
        for right in leaders[index + 1 :]:
            (
                _right_path,
                right_role,
                right_identity,
                right_clearance,
                right_scope,
                right_points,
            ) = right
            if left_scope != right_scope:
                continue
            right_interior = _trim_polyline_endpoint_clearance(
                right_points, right_clearance
            )
            if len(right_interior) < 2 or not _polylines_intersect(
                left_interior, right_interior
            ):
                continue
            _check(
                False,
                (
                    f"{subject_id}: {left_role} for {left_identity!r} crosses "
                    f"{right_role} for {right_identity!r} beyond their "
                    f"{left_clearance:.2f}/{right_clearance:.2f} mm endpoint clearances"
                ),
                failures,
            )


def _check_label_geography_clearance(
    *,
    subject_id: str,
    paths_by_layer: dict[str, list[ET.Element]],
    failures: list[str],
) -> None:
    """Reject plotted geography crossing any boxed context or contour copy."""

    boxed_roles = CONTEXT_LABEL_ROLES | CONTOUR_LABEL_ROLES
    labels: dict[
        tuple[tuple[str, str], str],
        tuple[str, tuple[float, float, float, float]],
    ] = {}
    for paths in paths_by_layer.values():
        for path in paths:
            role = str(path.get("data-role", ""))
            if role not in boxed_roles:
                continue
            label_id = str(path.get("data-label-id", "")).strip()
            label_box = _parse_label_box(path.get("data-label-box"))
            if not label_id or label_box is None:
                # Primary label validation reports missing or malformed metadata.
                continue
            labels.setdefault((_path_context_scope(path), label_id), (role, label_box))

    collisions: set[tuple[str, str, str, str]] = set()
    for layer_id in sorted(LABEL_GEOGRAPHY_LAYERS):
        for path in paths_by_layer.get(layer_id, []):
            geography_role = str(path.get("data-role", "untyped path"))
            allowed_roles = LABEL_GEOGRAPHY_ROLE_FILTERS.get(layer_id)
            if allowed_roles is not None and geography_role not in allowed_roles:
                continue
            subpaths = _parse_absolute_linear_subpaths(path.get("d"))
            _check(
                subpaths is not None,
                (
                    f"{subject_id}: {layer_id}/{geography_role} is not an "
                    "absolute linear M/L/Z path for label collision QA"
                ),
                failures,
            )
            if subpaths is None:
                continue
            scope = _path_context_scope(path)
            for (label_scope, label_id), (label_role, label_box) in labels.items():
                if scope != label_scope:
                    continue
                if any(
                    _polyline_box_distance(points, label_box) <= 1e-9
                    for points in subpaths
                ):
                    collisions.add((label_id, label_role, layer_id, geography_role))

    for label_id, label_role, layer_id, geography_role in sorted(collisions):
        _check(
            False,
            (
                f"{subject_id}: {layer_id}/{geography_role} crosses boxed "
                f"{label_role} {label_id!r}"
            ),
            failures,
        )


def _check_label_leader_clearance(
    *,
    subject_id: str,
    paths_by_layer: dict[str, list[ET.Element]],
    failures: list[str],
) -> None:
    """Verify leader identity and physical clearance from every foreign label."""

    boxed_label_roles = CONTEXT_LABEL_ROLES | CONTOUR_LABEL_ROLES
    labels_by_id: dict[
        str, tuple[tuple[float, float, float, float], str, str | None]
    ] = {}
    anonymous_labels: dict[
        tuple[str, str, tuple[float, float, float, float]],
        tuple[tuple[float, float, float, float], str, str | None],
    ] = {}
    all_paths = [path for paths in paths_by_layer.values() for path in paths]
    for path in all_paths:
        role = path.get("data-role", "")
        if role not in boxed_label_roles or path.get("data-label-box") is None:
            continue
        box = _parse_label_box(path.get("data-label-box"))
        _check(
            box is not None,
            f"{subject_id}: {role} has an invalid data-label-box",
            failures,
        )
        if box is None:
            continue
        label_id = str(path.get("data-label-id", "")).strip()
        contour_id = str(path.get("data-contour-id", "")).strip() or None
        if not label_id:
            anonymous_labels.setdefault(
                (role, contour_id or "", box), (box, role, contour_id)
            )
            _check(
                False,
                f"{subject_id}: boxed {role} has no deterministic label ID",
                failures,
            )
            continue
        previous = labels_by_id.setdefault(label_id, (box, role, contour_id))
        _check(
            previous == (box, role, contour_id),
            f"{subject_id}: boxed label {label_id!r} has inconsistent routing metadata",
            failures,
        )

    boxed_labels = [
        (label_id, *record) for label_id, record in sorted(labels_by_id.items())
    ]
    boxed_labels.extend(
        (f"<anonymous-{index}>", *record)
        for index, record in enumerate(anonymous_labels.values(), start=1)
    )
    leaders = [
        path for path in all_paths if path.get("data-role") in LABEL_LEADER_ROLES
    ]
    for index, leader in enumerate(leaders, start=1):
        role = str(leader.get("data-role"))
        target_id = str(leader.get("data-feature-id", "")).strip()
        identity = target_id or f"<leader-{index}>"
        target = labels_by_id.get(target_id)
        expected_label_roles = (
            CONTEXT_LABEL_ROLES
            if role == "context-label-leader"
            else frozenset({"source-derived-contour-altitude-label"})
        )
        _check(
            bool(target_id)
            and target is not None
            and target[1] in expected_label_roles,
            (
                f"{subject_id}: {role} target {identity!r} does not resolve to its "
                "own boxed label"
            ),
            failures,
        )
        _check(
            leader.get("data-leader-routing-policy") == LABEL_LEADER_ROUTING_POLICY_ID,
            f"{subject_id}: {role} for {identity!r} has invalid routing policy metadata",
            failures,
        )
        declared_clearance = _finite_number(
            leader.get("data-minimum-copy-clearance-mm")
        )
        _check(
            declared_clearance is not None
            and declared_clearance >= MINIMUM_LABEL_LEADER_CLEARANCE_MM,
            f"{subject_id}: {role} for {identity!r} has invalid clearance metadata",
            failures,
        )
        if role == "source-derived-contour-altitude-leader":
            contour_id = str(leader.get("data-contour-id", "")).strip()
            _check(
                bool(contour_id)
                and target is not None
                and target[1] == "source-derived-contour-altitude-label"
                and target[2] == contour_id,
                f"{subject_id}: contour leader for {identity!r} has invalid contour routing metadata",
                failures,
            )

        points = _parse_linear_ml_path(leader.get("d"))
        _check(
            points is not None,
            f"{subject_id}: {role} for {identity!r} is not an absolute linear M/L path",
            failures,
        )
        if points is None:
            continue
        required_clearance = max(
            MINIMUM_LABEL_LEADER_CLEARANCE_MM,
            declared_clearance
            if declared_clearance is not None and declared_clearance >= 0.0
            else 0.0,
        )
        own_label_id = (
            target_id
            if target is not None and target[1] in expected_label_roles
            else None
        )
        for foreign_id, foreign_box, _foreign_role, _foreign_contour_id in boxed_labels:
            if foreign_id == own_label_id:
                continue
            distance = _polyline_box_distance(points, foreign_box)
            _check(
                distance + 1e-9 >= required_clearance,
                (
                    f"{subject_id}: {role} for {identity!r} has {distance:.3f} mm "
                    f"clearance from foreign label {foreign_id!r}; requires "
                    f">={required_clearance:.2f} mm"
                ),
                failures,
            )


def _master_group_signature(group: ET.Element) -> list[tuple[str, ...]]:
    """Compare split jobs with their source group without relying on whitespace."""

    return [
        (
            path.get("d", ""),
            path.get("data-logical-layer", ""),
            path.get("data-role", ""),
            path.get("data-source-ref", ""),
            path.get("data-sequence", ""),
        )
        for path in group.findall(f".//{{{SVG_NS}}}path")
    ]


def _raster_parity(
    svg_path: Path,
    png_path: Path,
    *,
    dpi: float,
) -> tuple[bool, str]:
    """Re-rasterize the SVG and demand exact preview pixel parity."""

    inkscape = shutil.which("inkscape")
    compare = shutil.which("compare")
    if inkscape is None or compare is None:
        missing = [
            name
            for name, executable in (
                ("Inkscape", inkscape),
                ("ImageMagick compare", compare),
            )
            if executable is None
        ]
        return False, f"missing {' and '.join(missing)}"
    with tempfile.TemporaryDirectory(prefix="mapplot-hike-qa-") as temporary:
        reference = Path(temporary) / "reference.png"
        code, output = _run(
            [
                inkscape,
                str(svg_path),
                "--export-type=png",
                "--export-area-page",
                f"--export-dpi={dpi:g}",
                "--export-background=white",
                "--export-background-opacity=255",
                f"--export-filename={reference}",
            ]
        )
        if code != 0 or not reference.is_file():
            return False, f"reference rasterization failed: {output}"
        code, output = _run(
            [compare, "-metric", "AE", str(png_path), str(reference), "null:"]
        )
        return code == 0, output or "pixel difference reported"


def _check_a5_contract(
    *,
    subject_id: str,
    entry: dict[str, Any],
    manifest: dict[str, Any],
    root: ET.Element,
    failures: list[str],
) -> None:
    page = manifest.get("page") or {}
    format_id = page.get("format_id")
    expected = A5_FORMATS.get(str(format_id))
    _check(expected is not None, f"{subject_id}: format is not binding A5", failures)
    if expected is None:
        return
    width_mm, height_mm, orientation = expected
    _check(
        entry.get("format_id") == format_id,
        f"{subject_id}: index/manifest format drift",
        failures,
    )
    _check(
        page.get("paper") == "A5", f"{subject_id}: manifest paper is not A5", failures
    )
    _check(
        page.get("orientation") == orientation,
        f"{subject_id}: A5 orientation drift",
        failures,
    )
    _check(
        _close(page.get("width_mm"), width_mm),
        f"{subject_id}: manifest page width drift",
        failures,
    )
    _check(
        _close(page.get("height_mm"), height_mm),
        f"{subject_id}: manifest page height drift",
        failures,
    )
    _check(
        _close(_millimetres(root.get("width")), width_mm),
        f"{subject_id}: SVG width is not {width_mm:g} mm",
        failures,
    )
    _check(
        _close(_millimetres(root.get("height")), height_mm),
        f"{subject_id}: SVG height is not {height_mm:g} mm",
        failures,
    )
    try:
        view_box = tuple(float(value) for value in root.get("viewBox", "").split())
    except ValueError:
        view_box = ()
    _check(
        len(view_box) == 4
        and all(math.isfinite(value) for value in view_box)
        and all(
            math.isclose(actual, wanted, rel_tol=0.0, abs_tol=1e-6)
            for actual, wanted in zip(view_box, (0.0, 0.0, width_mm, height_mm))
        ),
        f"{subject_id}: SVG viewBox is not one-user-unit-per-mm A5",
        failures,
    )


def _marker_label_identity(value: object) -> str:
    """Match the renderer's punctuation-insensitive identity for route copy."""

    replacements = str.maketrans(
        {
            "·": "/",
            "•": "/",
            "–": "-",
            "—": "-",
            "−": "-",
            "’": "'",
            "‘": "'",
            "“": '"',
            "”": '"',
            "×": "X",
            "→": ">",
            "←": "<",
            "…": "...",
        }
    )
    plotter_copy = " ".join(str(value).translate(replacements).split())
    return "".join(character for character in plotter_copy.casefold() if character.isalnum())


def _route_control_marker_features(
    catalog_record: dict[str, Any],
) -> tuple[list[tuple[str, str, dict[str, Any]]], list[str]]:
    """Reconcile the renderer's start/finish label identities fail closed.

    Existing source labels are accepted only when exactly one label candidate
    matches the control name, point and source.  Otherwise the renderer-owned
    synthetic feature ID is reproduced from the frozen candidate list.
    """

    problems: list[str] = []
    route = catalog_record.get("route")
    context = catalog_record.get("context")
    if not isinstance(route, dict) or not isinstance(context, dict):
        return [], ["catalog route/context metadata is missing"]
    raw_features = context.get("features")
    if not isinstance(raw_features, list):
        return [], ["catalog context features are missing"]

    label_features: list[dict[str, Any]] = []
    for raw_feature in raw_features:
        if not isinstance(raw_feature, dict) or raw_feature.get("display_label") is False:
            continue
        feature = dict(raw_feature)
        feature["_qa_base_label_identity"] = _marker_label_identity(
            feature.get("label", "")
        )
        elevation = feature.get("elevation_m")
        if (
            feature.get("kind") in {"peak", "pass"}
            and isinstance(elevation, (int, float))
            and not isinstance(elevation, bool)
            and math.isfinite(float(elevation))
        ):
            feature["label"] = f"{feature.get('label', '')} / {float(elevation):.0f} M"
        label_features.append(feature)

    water = context.get("water")
    if isinstance(water, dict):
        existing_labels = {
            _marker_label_identity(feature.get("label", ""))
            for feature in label_features
        }
        raw_water_labels = water.get("labels")
        if isinstance(raw_water_labels, list):
            for raw_label in raw_water_labels:
                if not isinstance(raw_label, dict):
                    continue
                identity = _marker_label_identity(raw_label.get("label", ""))
                if not identity or identity in existing_labels:
                    continue
                feature = dict(raw_label)
                feature.setdefault("source_ref", water.get("source_ref"))
                feature["_qa_base_label_identity"] = identity
                label_features.append(feature)
                existing_labels.add(identity)

    raw_controls = route.get("controls")
    if not isinstance(raw_controls, list):
        return [], ["route controls are missing"]
    controls_by_kind: dict[str, list[dict[str, Any]]] = {"start": [], "finish": []}
    for raw_control in raw_controls:
        if isinstance(raw_control, dict) and raw_control.get("kind") in controls_by_kind:
            controls_by_kind[str(raw_control["kind"])].append(raw_control)
    if any(len(controls) != 1 for controls in controls_by_kind.values()):
        return [], ["exactly one start and one finish control are required"]

    reconciled: list[tuple[str, str, dict[str, Any]]] = []
    for kind, station_id in (("start", "A"), ("finish", "E")):
        control = controls_by_kind[kind][0]
        identity = _marker_label_identity(control.get("name", ""))
        point = control.get("point")
        source_ref = str(control.get("source_ref") or "")
        if (
            not identity
            or not isinstance(point, list)
            or len(point) < 2
            or _finite_number(point[0]) is None
            or _finite_number(point[1]) is None
            or not source_ref
        ):
            problems.append(f"{kind} control has incomplete name/point/source metadata")
            continue
        matching = [
            feature
            for feature in label_features
            if str(feature.get("_qa_base_label_identity") or "") == identity
        ]
        if len(matching) > 1:
            problems.append(f"{kind} control label identity is ambiguous")
            continue
        if matching:
            feature = matching[0]
            feature_point = feature.get("point")
            feature_longitude = (
                _finite_number(feature_point[0])
                if isinstance(feature_point, list) and len(feature_point) >= 2
                else None
            )
            feature_latitude = (
                _finite_number(feature_point[1])
                if isinstance(feature_point, list) and len(feature_point) >= 2
                else None
            )
            if (
                feature.get("kind") not in {"settlement", "hut", "pass", "peak"}
                or feature_longitude is None
                or feature_latitude is None
                or not math.isclose(
                    feature_longitude, float(point[0]), rel_tol=0.0, abs_tol=1e-9
                )
                or not math.isclose(
                    feature_latitude, float(point[1]), rel_tol=0.0, abs_tol=1e-9
                )
                or str(feature.get("source_ref") or "") != source_ref
                or not str(feature.get("id") or "")
            ):
                problems.append(
                    f"{kind} control label does not match its exact source coordinate"
                )
                continue
        else:
            feature = {
                "id": f"route-control-{kind}-{len(label_features) + 1}",
                "kind": "settlement",
                "label": str(control.get("name") or "").upper(),
                "point": list(point),
                "source_ref": source_ref,
                "_qa_base_label_identity": identity,
            }
            label_features.append(feature)
        reconciled.append((kind, station_id, feature))
    return reconciled, problems


def _route_stations_are_marker_equivalent(
    *,
    manifest: dict[str, Any],
    paths_by_layer: dict[str, list[ET.Element]],
) -> tuple[bool, list[str]]:
    """Accept A/E station marks only for exactly reconciled suppressed markers."""

    rendering = manifest.get("rendering")
    reservations = (
        rendering.get("chainage_station_reservations")
        if isinstance(rendering, dict)
        else None
    )
    if not isinstance(reservations, dict):
        return False, []
    suppressed = reservations.get("context_markers_suppressed")
    # Metadata without a positive suppression claim is not an attempted marker
    # equivalence and receives the ordinary "no geographic markers" failure.
    if not isinstance(suppressed, int) or isinstance(suppressed, bool) or suppressed <= 0:
        return False, []

    problems: list[str] = []
    if reservations.get("policy") != ROUTE_STATION_RESERVATION_POLICY_ID:
        problems.append("reservation policy drift")
    if reservations.get("count") != len(CHAINAGE_STATIONS):
        problems.append("reservation count is not the A-E station count")
    if not _close(
        reservations.get("radius_mm"), ROUTE_STATION_RESERVATION_RADIUS_MM
    ):
        problems.append("reservation radius drift")

    catalog_record = manifest.get("catalog_record")
    if not isinstance(catalog_record, dict):
        return False, [*problems, "catalog record is missing"]
    route = catalog_record.get("route")
    if not isinstance(route, dict) or not str(route.get("source_ref") or ""):
        return False, [*problems, "route source metadata is missing"]
    expected, control_problems = _route_control_marker_features(catalog_record)
    problems.extend(control_problems)
    if len(expected) != 2:
        problems.append("start/finish marker-label reconciliation is incomplete")

    all_marker_label_paths = [
        path
        for path in paths_by_layer.get("context_labels", [])
        if path.get("data-role") in MARKER_LABEL_ROLES
    ]
    expected_label_ids = {
        str(feature.get("id")) for _kind, _station_id, feature in expected
    }
    # Other geographic marker labels (for example an independently sourced
    # peak) are not substitutes for the two route-control markers.  Restrict
    # the equivalence proof to the exact start/finish identities so those
    # additional labels cannot either satisfy or invalidate it.
    marker_label_paths = [
        path
        for path in all_marker_label_paths
        if path.get("data-label-id") in expected_label_ids
    ]
    actual_label_ids = {
        str(path.get("data-label-id"))
        for path in marker_label_paths
        if path.get("data-label-id")
    }
    if (
        len(actual_label_ids) != 2
        or actual_label_ids != expected_label_ids
        or suppressed != len(actual_label_ids)
    ):
        problems.append(
            "suppressed marker count/identity does not exactly match the A/E labels"
        )
    for _kind, _station_id, feature in expected:
        feature_id = str(feature.get("id") or "")
        expected_role = f"{feature.get('kind')}-label"
        expected_source_ref = str(feature.get("source_ref") or "")
        feature_paths = [
            path
            for path in marker_label_paths
            if path.get("data-label-id") == feature_id
        ]
        if not feature_paths or any(
            path.get("data-role") != expected_role
            or path.get("data-source-ref") != expected_source_ref
            for path in feature_paths
        ):
            problems.append(f"{feature_id or 'route control'} label provenance drift")

    all_paths = [path for paths in paths_by_layer.values() for path in paths]
    map_labels = _chainage_paths_by_id(all_paths, "map-chainage-label")
    if set(map_labels) != set(CHAINAGE_STATIONS):
        problems.append("A-E map chainage labels are incomplete")
    controls = {
        str(control.get("kind")): control
        for control in (route.get("controls") or [])
        if isinstance(control, dict) and control.get("kind") in {"start", "finish"}
    }
    for kind, station_id, feature in expected:
        control = controls.get(kind)
        role_paths = [
            path
            for path in paths_by_layer.get("route_annotations", [])
            if path.get("data-role") == kind
            and path.get("data-chainage-id") == station_id
        ]
        label_paths = map_labels.get(station_id, [])
        role_signature = _chainage_signature(role_paths)
        label_signature = _chainage_signature(label_paths)
        if (
            not isinstance(control, dict)
            or not role_paths
            or role_signature is None
            or role_signature != label_signature
        ):
            problems.append(f"{kind}/{station_id} chainage mark metadata drift")
            continue
        point = control.get("point")
        path = role_paths[0]
        longitude = _finite_number(path.get("data-longitude"))
        latitude = _finite_number(path.get("data-latitude"))
        fraction = _finite_number(path.get("data-route-fraction"))
        expected_fraction = CHAINAGE_STATIONS[station_id]
        if (
            not isinstance(point, list)
            or len(point) < 2
            or longitude is None
            or latitude is None
            or not math.isclose(longitude, float(point[0]), rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(latitude, float(point[1]), rel_tol=0.0, abs_tol=1e-9)
            or fraction is None
            or not math.isclose(fraction, expected_fraction, rel_tol=0.0, abs_tol=1e-9)
            or any(
                candidate.get("data-source-ref")
                != str(feature.get("source_ref") or "")
                for candidate in role_paths
            )
            or path.get("data-route-source-ref") != str(route.get("source_ref"))
            or path.get("data-chainage-basis")
            != "source-geometry-cumulative-geodesic-v1"
            or path.get("data-profile-status") != str(route.get("profile_status") or "")
        ):
            problems.append(f"{kind}/{station_id} source coordinate/provenance drift")
    return not problems, problems


def _check_hiking_semantics(
    *,
    subject_id: str,
    manifest: dict[str, Any],
    root: ET.Element,
    failures: list[str],
) -> None:
    """Fail closed when a route silhouette is passed off as a geographic map."""

    paths_by_layer = _paths_by_logical_layer(root)
    logical_layers = set(paths_by_layer)
    _check(
        REQUIRED_LOGICAL_LAYERS <= logical_layers,
        f"{subject_id}: missing geographic logical layers "
        f"{sorted(REQUIRED_LOGICAL_LAYERS - logical_layers)}",
        failures,
    )
    roles_by_layer = {
        layer_id: _roles(paths) for layer_id, paths in paths_by_layer.items()
    }
    catalog_record = manifest.get("catalog_record") or {}
    catalog_context = (
        catalog_record.get("context") if isinstance(catalog_record, dict) else {}
    )
    catalog_features = (
        catalog_context.get("features") if isinstance(catalog_context, dict) else []
    )
    catalog_features = catalog_features if isinstance(catalog_features, list) else []
    mountain_label_candidate_exists = any(
        isinstance(feature, dict)
        and feature.get("kind") in {"peak", "range", "pass"}
        and feature.get("display_label") is not False
        and bool(str(feature.get("label", "")).strip())
        and feature.get("point")
        for feature in catalog_features
    )
    systematic_water = (
        catalog_context.get("water") if isinstance(catalog_context, dict) else None
    )
    hydro_label_candidate_exists = any(
        isinstance(feature, dict)
        and feature.get("kind") in {"water", "sea", "river", "coast"}
        and feature.get("display_label") is not False
        and bool(str(feature.get("label", "")).strip())
        and feature.get("point")
        for feature in catalog_features
    ) or (
        isinstance(systematic_water, dict)
        and any(
            isinstance(label, dict)
            and label.get("display_label") is not False
            and bool(str(label.get("label", "")).strip())
            and label.get("point")
            for label in (systematic_water.get("labels") or [])
        )
    )
    _check(
        "source-sampled-lake-shore-echo"
        not in roles_by_layer.get("context_water", set()),
        f"{subject_id}: legacy doubled shoreline echo was rendered",
        failures,
    )
    family_evidence = _catalog_family_evidence(manifest)
    semantic_requirements = [
        ("mountain relief", "context_relief", RELIEF_ROLES),
        ("place labels", "context_labels", PLACE_LABEL_ROLES),
    ]
    marker_roles_present = bool(
        roles_by_layer.get("context_markers", set()) & MARKER_ROLES
    )
    marker_equivalent = False
    if not marker_roles_present:
        marker_equivalent, marker_equivalence_problems = (
            _route_stations_are_marker_equivalent(
                manifest=manifest,
                paths_by_layer=paths_by_layer,
            )
        )
        failures.extend(
            f"{subject_id}: invalid marker-equivalent route stations: {problem}"
            for problem in marker_equivalence_problems
        )
    _check(
        marker_roles_present or marker_equivalent,
        f"{subject_id}: no geographic markers",
        failures,
    )
    if mountain_label_candidate_exists:
        semantic_requirements.append(
            ("mountain/range/pass labels", "context_labels", MOUNTAIN_LABEL_ROLES)
        )
    raw_hydro_selected = (
        family_evidence.get("hydrography", {}).get("selected_feature_count")
        if family_evidence is not None
        else 1
    )
    hydro_selected = (
        isinstance(raw_hydro_selected, int)
        and not isinstance(raw_hydro_selected, bool)
        and raw_hydro_selected > 0
    )
    if hydro_selected:
        semantic_requirements.append(("water/coast", "context_water", WATER_ROLES))
        if hydro_label_candidate_exists:
            semantic_requirements.append(
                ("water/sea labels", "context_labels", HYDRO_LABEL_ROLES)
            )
    for description, layer_id, accepted_roles in semantic_requirements:
        _check(
            bool(roles_by_layer.get(layer_id, set()) & accepted_roles),
            f"{subject_id}: no {description}",
            failures,
        )
    landcover_is_declared = _catalog_declares_landcover(manifest)
    landcover_roles = roles_by_layer.get(
        "context_woodland", set()
    ) | roles_by_layer.get("context_landcover", set())
    if landcover_is_declared:
        _check(
            bool(landcover_roles & LAND_COVER_ROLES),
            f"{subject_id}: declared source-backed land cover was not rendered",
            failures,
        )
    else:
        _check(
            not (landcover_roles & LAND_COVER_ROLES),
            f"{subject_id}: green land cover was rendered without a catalog source feature",
            failures,
        )
    catalog_record = manifest.get("catalog_record") or {}
    context = catalog_record.get("context") if isinstance(catalog_record, dict) else {}
    features = context.get("features") if isinstance(context, dict) else []
    roads_are_declared = any(
        isinstance(feature, dict)
        and feature.get("kind") == "road"
        and bool(feature.get("paths"))
        for feature in (features or [])
    )
    # Expansion records carry explicit selection evidence.  Selected source
    # lines may all disappear after exact page clipping, the 0.75 mm physical
    # stroke floor, and copy/route clearance.  Their post-clipping SVG
    # provenance is reconciled in the paired-v4 check below; selection alone
    # is not a promise that ink will be emitted.  Legacy records without that
    # evidence retain the stricter historical requirement.
    if roads_are_declared and family_evidence is None:
        _check(
            bool(roles_by_layer.get("context_roads", set()) & ROAD_ROLES),
            f"{subject_id}: declared source-backed roads were not rendered",
            failures,
        )
    _check(
        "source-sampled-route" in roles_by_layer.get("hero_route", set()),
        f"{subject_id}: hero route is not source-sampled",
        failures,
    )

    sources = manifest.get("sources") or []
    sources_by_id = {
        str(source.get("id")): source
        for source in sources
        if isinstance(source, dict) and source.get("id")
    }
    used_source_refs: set[str] = set()
    for logical_id, paths in paths_by_layer.items():
        for path in paths:
            path_layer = path.get("data-logical-layer")
            _check(
                path_layer == logical_id,
                f"{subject_id}: path/logical-layer metadata drift in {logical_id}",
                failures,
            )
            role = path.get("data-role", "")
            if logical_id in CONTEXT_LOGICAL_LAYERS or logical_id == "hero_route":
                source_ref = path.get("data-source-ref")
                _check(
                    bool(source_ref),
                    f"{subject_id}: {logical_id}/{role or 'untyped path'} has no source ref",
                    failures,
                )
                if source_ref:
                    used_source_refs.add(source_ref)
                    _check(
                        source_ref in sources_by_id,
                        f"{subject_id}: path cites unknown source {source_ref!r}",
                        failures,
                    )
            elif role in {
                "source-elevation-profile",
                "profile-baseline",
                "profile-chainage-tick",
                "profile-chainage-station",
                "profile-chainage-label",
                "profile-status",
            }:
                source_ref = path.get("data-source-ref")
                _check(
                    bool(source_ref),
                    f"{subject_id}: {role} has no source ref",
                    failures,
                )
                if source_ref:
                    used_source_refs.add(source_ref)
                    _check(
                        source_ref in sources_by_id,
                        f"{subject_id}: {role} cites unknown source {source_ref!r}",
                        failures,
                    )
            if logical_id in CONTEXT_LOGICAL_LAYERS:
                for attribute, expected in CONTEXT_ATTRIBUTES.items():
                    _check(
                        path.get(attribute) == expected,
                        f"{subject_id}: {logical_id}/{role or 'untyped path'} has invalid {attribute}",
                        failures,
                    )
            if logical_id in {"context_relief", "context_relief_index"}:
                _check(
                    path.get("data-relief-status") in RELIEF_STATUSES,
                    f"{subject_id}: relief path lacks a source/derivation disclosure",
                    failures,
                )
            if role in CONTEXT_LABEL_ROLES:
                _check(
                    bool(path.get("data-label-id")),
                    f"{subject_id}: {role} glyph has no deterministic label ID",
                    failures,
                )
                _check(
                    _parse_label_box(path.get("data-label-box")) is not None,
                    f"{subject_id}: {role} glyph has no valid label box",
                    failures,
                )
            source_ref = path.get("data-source-ref")
            source = sources_by_id.get(source_ref or "", {})
            if (
                logical_id
                in {
                    "context_water",
                    "context_woodland",
                    "context_landcover",
                    "context_roads",
                }
                and str(source.get("license", "")).upper().startswith("ODBL")
                and role in WATER_ROLES | LAND_COVER_ROLES
            ):
                osm_element = path.get("data-osm-element")
                source_url = path.get("data-source-url")
                _check(
                    bool(osm_element and _OSM_ELEMENT.fullmatch(osm_element)),
                    f"{subject_id}: ODbL context path lacks an OSM element identifier",
                    failures,
                )
                _check(
                    bool(
                        source_url
                        and source_url == f"https://www.openstreetmap.org/{osm_element}"
                    ),
                    f"{subject_id}: ODbL context path lacks its canonical feature URL",
                    failures,
                )
                osm_elements = path.get("data-osm-elements")
                if osm_elements is not None:
                    elements = osm_elements.split(",")
                    _check(
                        bool(elements)
                        and all(_OSM_ELEMENT.fullmatch(element) for element in elements)
                        and len(elements) == len(set(elements))
                        and elements[0] == osm_element,
                        f"{subject_id}: stitched ODbL path has invalid source elements",
                        failures,
                    )
                    try:
                        declared_count = int(path.get("data-source-object-count", ""))
                    except ValueError:
                        declared_count = -1
                    _check(
                        declared_count == len(elements),
                        f"{subject_id}: stitched ODbL source count drift",
                        failures,
                    )

    catalog_record = manifest.get("catalog_record") or {}
    catalog_context = (
        catalog_record.get("context") if isinstance(catalog_record, dict) else {}
    )
    systematic_water = (
        catalog_context.get("water") if isinstance(catalog_context, dict) else None
    )
    if isinstance(systematic_water, dict):
        if subject_id == "RTE-GB-WHW-01":
            _check(
                systematic_water.get("status") == "source-sampled-hydrography"
                and systematic_water.get("derivation_id")
                == "osm-pbf-whw-hydrography-stitched-v5",
                f"{subject_id}: systematic hydrography contract drift",
                failures,
            )
            _check(
                len(systematic_water.get("areas") or []) == 10
                and len(systematic_water.get("coastlines") or []) == 2
                and len(systematic_water.get("rivers") or []) == 12,
                f"{subject_id}: systematic hydrography selection-count drift",
                failures,
            )
            _check(
                len(paths_by_layer.get("context_water", [])) <= 125,
                f"{subject_id}: systematic hydrography was fragmented during rendering",
                failures,
            )
        else:
            collections = [
                systematic_water.get(key) for key in ("areas", "coastlines", "rivers")
            ]
            _check(
                systematic_water.get("status") == "source-sampled-hydrography"
                and systematic_water.get("derivation_id")
                == "osm-pbf-generic-hydrography-stitched-v1"
                and all(isinstance(collection, list) for collection in collections)
                and any(collection for collection in collections),
                f"{subject_id}: generic systematic hydrography contract drift",
                failures,
            )

    _check(
        bool(used_source_refs),
        f"{subject_id}: no path provenance was emitted",
        failures,
    )
    _check(
        used_source_refs <= set(sources_by_id),
        f"{subject_id}: SVG source refs are not covered by the manifest",
        failures,
    )
    for source_ref in sorted(used_source_refs):
        source = sources_by_id.get(source_ref, {})
        _check(
            _source_has_frozen_snapshot(source),
            f"{subject_id}: used source {source_ref!r} has no frozen SHA-256 evidence",
            failures,
        )

    label_boxes: dict[str, tuple[float, float, float, float]] = {}
    for layer_id in ("context_labels", "context_relief_labels"):
        for path in paths_by_layer.get(layer_id, []):
            if path.get("data-role") not in CONTEXT_LABEL_ROLES | CONTOUR_LABEL_ROLES:
                continue
            label_id = path.get("data-label-id")
            box = _parse_label_box(path.get("data-label-box"))
            if not label_id or box is None:
                continue
            previous = label_boxes.setdefault(label_id, box)
            _check(
                previous == box,
                f"{subject_id}: label {label_id!r} has inconsistent boxes",
                failures,
            )
    labelled = sorted(label_boxes.items())
    for index, (left_id, left_box) in enumerate(labelled):
        for right_id, right_box in labelled[index + 1 :]:
            _check(
                not _boxes_overlap(left_box, right_box),
                f"{subject_id}: label boxes overlap ({left_id!r}, {right_id!r})",
                failures,
            )
    _check_label_leader_clearance(
        subject_id=subject_id,
        paths_by_layer=paths_by_layer,
        failures=failures,
    )
    _check_label_leader_geometry_clearance(
        subject_id=subject_id,
        paths_by_layer=paths_by_layer,
        failures=failures,
    )
    _check_label_geography_clearance(
        subject_id=subject_id,
        paths_by_layer=paths_by_layer,
        failures=failures,
    )

    sequence = manifest.get("pen_sequence") or []
    _check(bool(sequence), f"{subject_id}: empty pen sequence", failures)
    if sequence:
        final_step = sequence[-1]
        _check(
            final_step.get("pen_id") == "red-0-4"
            and "hero_route" in (final_step.get("layers") or []),
            f"{subject_id}: the red hero route is not the last pen load",
            failures,
        )
        _check(
            all(
                "hero_route" not in (step.get("layers") or []) for step in sequence[:-1]
            ),
            f"{subject_id}: hero route appears before the final pen load",
            failures,
        )
    physical_layers = _physical_layers(root)
    if physical_layers:
        _check(
            physical_layers[-1].get("id") == "layer-pen-red-0-4"
            and physical_layers[-1].find(
                f"{{{SVG_NS}}}g[@data-logical-layer='hero_route']"
            )
            is not None,
            f"{subject_id}: SVG document does not physically end with the hero route pen",
            failures,
        )

    attribution_paths = [
        path
        for layer_id in ("plate_attribution", "plate_copy")
        for path in paths_by_layer.get(layer_id, [])
        if path.get("data-role") == "attribution"
    ]
    rendering = manifest.get("rendering") or {}
    credit_line = str((manifest.get("source") or {}).get("attribution", ""))
    _check(
        rendering.get("visible_attribution") is True and bool(attribution_paths),
        f"{subject_id}: required visible attribution paths are absent",
        failures,
    )
    if any(
        str(source.get("license", "")).upper().startswith("ODBL")
        for source in sources_by_id.values()
    ):
        _check(
            "openstreetmap" in credit_line.casefold(),
            f"{subject_id}: visible credit does not name OpenStreetMap",
            failures,
        )
        _check(
            "openstreetmap.org/copyright" in credit_line.casefold(),
            f"{subject_id}: visible credit omits OPENSTREETMAP.ORG/COPYRIGHT",
            failures,
        )
    catalog_record = manifest.get("catalog_record") or {}
    route = catalog_record.get("route") if isinstance(catalog_record, dict) else None
    route_source_ref = (
        str(route.get("source_ref") or "") if isinstance(route, dict) else ""
    )
    route_elevation_source_ref = (
        str(route.get("elevation_source_ref") or "")
        if isinstance(route, dict)
        else ""
    )
    route_source = sources_by_id.get(route_source_ref)
    if route_source is not None and not str(
        route_source.get("license", "")
    ).upper().startswith("ODBL"):
        required_route_credit = " ".join(
            str(route_source.get("attribution") or "").split()
        )
        required_route_publisher = " ".join(
            str(route_source.get("publisher") or "").split()
        )
        normalized_credit = " ".join(credit_line.replace("|", " ").split())
        _check(
            bool(required_route_credit)
            and (
                required_route_credit.casefold() in normalized_credit.casefold()
                or (
                    bool(required_route_publisher)
                    and required_route_publisher.casefold()
                    in normalized_credit.casefold()
                )
            ),
            f"{subject_id}: visible credit omits the non-ODbL route source",
            failures,
        )

    if any(
        source.get("provider_attribution_review_required") is True
        for source in sources_by_id.values()
    ):
        rights = manifest.get("rights") or {}
        _check(
            rights.get("status") == "review-required",
            f"{subject_id}: unresolved provider attribution is not fail-closed",
            failures,
        )

    terrain_provenance_refs = _terrain_release_provenance_refs(catalog_record)
    _check(
        terrain_provenance_refs <= set(sources_by_id),
        f"{subject_id}: terrain release provenance cites an unknown source",
        failures,
    )
    for source_ref, source in sources_by_id.items():
        use = str(source.get("use") or "").casefold()
        looks_like_replaced_terrain = any(
            token in use for token in ("terrain", "contour", "dem", "dtm")
        )
        _check(
            not (
                looks_like_replaced_terrain
                and source_ref not in used_source_refs
                and source_ref not in {route_source_ref, route_elevation_source_ref}
                and source_ref not in terrain_provenance_refs
            ),
            f"{subject_id}: superseded unreferenced terrain source remains registered",
            failures,
        )

    metadata = root.find(f"{{{SVG_NS}}}metadata")
    try:
        svg_metadata = json.loads(metadata.text or "") if metadata is not None else {}
    except json.JSONDecodeError:
        svg_metadata = {}
    _check(
        svg_metadata.get("subject_id") == subject_id
        and svg_metadata.get("domain") == "hikes"
        and svg_metadata.get("sources") == sources,
        f"{subject_id}: embedded SVG provenance drifts from the manifest",
        failures,
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _chainage_paths_by_id(
    paths: list[ET.Element], role: str
) -> dict[str, list[ET.Element]]:
    grouped: dict[str, list[ET.Element]] = {}
    for path in paths:
        if path.get("data-role") != role:
            continue
        station_id = str(path.get("data-chainage-id", ""))
        grouped.setdefault(station_id, []).append(path)
    return grouped


def _chainage_signature(paths: list[ET.Element]) -> tuple[str | None, ...] | None:
    signatures = {
        tuple(path.get(attribute) for attribute in CHAINAGE_SHARED_ATTRIBUTES)
        for path in paths
    }
    return next(iter(signatures)) if len(signatures) == 1 else None


def _expected_profile_extrema_disclosure(
    route: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    official_distance = _published_route_distance_km(route)
    distance_label_basis = (
        "official-total-proportional-to-source-chainage-v1"
        if official_distance is not None
        else "measured-source-chainage-v1"
    )
    evidence = route.get("elevation_extrema_evidence")
    if evidence is None:
        source_ref = str(route.get("elevation_source_ref") or route.get("source_ref") or "")
        return (
            PROFILE_EXTREMA_APPROXIMATE_STATUS,
            PROFILE_EXTREMA_APPROXIMATE_POLICY_ID,
            source_ref,
            distance_label_basis,
        )
    if (
        not isinstance(evidence, dict)
        or evidence.get("status") != PROFILE_EXTREMA_EXACT_STATUS
        or not str(evidence.get("source_ref") or "")
        or not str(evidence.get("method") or "")
        or _finite_number(evidence.get("minimum_m")) is None
        or _finite_number(evidence.get("maximum_m")) is None
    ):
        return None
    return (
        PROFILE_EXTREMA_EXACT_STATUS,
        PROFILE_EXTREMA_EXACT_POLICY_ID,
        str(evidence["source_ref"]),
        distance_label_basis,
    )


def _published_route_distance_km(route: dict[str, Any]) -> float | None:
    raw_distance = route.get(
        "official_distance_km",
        route.get("official_record_distance_km"),
    )
    distance = _finite_number(raw_distance)
    return distance if distance is not None and distance > 0.0 else None


def _expected_profile_extrema_caption(
    route: dict[str, Any], minimum_m: float, maximum_m: float
) -> str:
    distance_caption = (
        "PUBLISHED KM"
        if _published_route_distance_km(route) is not None
        else "MEASURED KM"
    )
    evidence = route.get("elevation_extrema_evidence")
    if isinstance(evidence, dict) and evidence.get("status") == PROFILE_EXTREMA_EXACT_STATUS:
        minimum_m = float(evidence["minimum_m"])
        maximum_m = float(evidence["maximum_m"])
        return (
            f"SOURCE-VERIFIED ELEVATION {minimum_m:.0f}-{maximum_m:.0f} M"
            f" / {distance_caption}"
        )
    elevation_caption = (
        "RECORDED ELEVATION"
        if route.get("profile_status") == "recorded-elevation-sampled"
        else "SAMPLED ELEVATION"
    )
    return (
        f"{elevation_caption} / APPROX {minimum_m:.0f}-{maximum_m:.0f} M"
        f" / {distance_caption}"
    )


def _check_chainage_profile_contract(
    *,
    artifact_id: str,
    route: dict[str, Any],
    all_paths: list[ET.Element],
    failures: list[str],
) -> None:
    """Require one unboxed A--E system with explicit distance semantics."""

    all_roles = _roles(all_paths)
    _check(
        "profile-frame" not in all_roles,
        f"{artifact_id}: boxed elevation profile frame is forbidden",
        failures,
    )
    _check(
        "profile-baseline" in all_roles
        and "source-elevation-profile" in all_roles,
        f"{artifact_id}: unboxed bottom elevation profile is incomplete",
        failures,
    )
    expected_extrema = _expected_profile_extrema_disclosure(route)
    _check(
        expected_extrema is not None,
        f"{artifact_id}: elevation extrema evidence is malformed",
        failures,
    )
    disclosure_paths = [
        path
        for path in all_paths
        if path.get("data-role")
        in {"profile-baseline", "source-elevation-profile", "profile-status"}
    ]
    if expected_extrema is not None:
        (
            expected_status,
            expected_policy,
            expected_source_ref,
            expected_distance_basis,
        ) = expected_extrema
        minimum_m = (
            _finite_number(disclosure_paths[0].get("data-elevation-min-m"))
            if disclosure_paths
            else None
        )
        maximum_m = (
            _finite_number(disclosure_paths[0].get("data-elevation-max-m"))
            if disclosure_paths
            else None
        )
        expected_caption = (
            _expected_profile_extrema_caption(route, minimum_m, maximum_m)
            if minimum_m is not None and maximum_m is not None
            else ""
        )
        _check(
            bool(disclosure_paths)
            and any(
                path.get("data-role") == "profile-status"
                for path in disclosure_paths
            )
            and all(
                path.get("data-elevation-extrema-status") == expected_status
                and path.get("data-elevation-extrema-policy") == expected_policy
                and path.get("data-elevation-extrema-source-ref")
                == expected_source_ref
                and path.get("data-elevation-extrema-caption") == expected_caption
                and path.get("data-distance-label-basis")
                == expected_distance_basis
                for path in disclosure_paths
            ),
            f"{artifact_id}: profile extrema or kilometre labels lack the "
            "required evidence disclosure",
            failures,
        )
    map_paths = _chainage_paths_by_id(all_paths, "map-chainage-label")
    profile_paths = _chainage_paths_by_id(all_paths, "profile-chainage-label")
    profile_ticks = _chainage_paths_by_id(all_paths, "profile-chainage-tick")
    profile_stations = _chainage_paths_by_id(
        all_paths, "profile-chainage-station"
    )
    expected_ids = set(CHAINAGE_STATIONS)
    for description, grouped in (
        ("map labels", map_paths),
        ("profile labels", profile_paths),
        ("profile ticks", profile_ticks),
        ("profile stations", profile_stations),
    ):
        _check(
            set(grouped) == expected_ids,
            f"{artifact_id}: A-E chainage {description} are incomplete",
            failures,
        )
    if set(map_paths) != expected_ids or set(profile_paths) != expected_ids:
        return

    previous_measured_m = -math.inf
    for station_id, expected_fraction in CHAINAGE_STATIONS.items():
        map_signature = _chainage_signature(map_paths[station_id])
        profile_signature = _chainage_signature(profile_paths[station_id])
        tick_signature = _chainage_signature(profile_ticks.get(station_id, []))
        station_signature = _chainage_signature(
            profile_stations.get(station_id, [])
        )
        _check(
            map_signature is not None
            and map_signature == profile_signature == tick_signature
            and map_signature == station_signature,
            f"{artifact_id}: chainage station {station_id} does not map to the "
            "same profile distance/elevation",
            failures,
        )
        path = map_paths[station_id][0]
        fraction = _finite_number(path.get("data-route-fraction"))
        chainage_m = _finite_number(path.get("data-chainage-m"))
        distance_km = _finite_number(path.get("data-distance-km"))
        measured_m = _finite_number(path.get("data-measured-chainage-m"))
        displayed_m = _finite_number(path.get("data-displayed-distance-m"))
        displayed_km = _finite_number(path.get("data-displayed-distance-km"))
        _check(
            fraction is not None
            and math.isclose(
                fraction, expected_fraction, rel_tol=0.0, abs_tol=1e-9
            ),
            f"{artifact_id}: chainage station {station_id} is not at "
            f"{expected_fraction:.0%} of route distance",
            failures,
        )
        _check(
            chainage_m is not None
            and distance_km is not None
            and math.isclose(
                chainage_m, distance_km * 1_000.0, rel_tol=0.0, abs_tol=0.01
            ),
            f"{artifact_id}: chainage station {station_id} measured distance "
            "drifts from its source-chainage axis",
            failures,
        )
        _check(
            chainage_m is not None
            and measured_m is not None
            and math.isclose(
                chainage_m, measured_m, rel_tol=0.0, abs_tol=0.01
            ),
            f"{artifact_id}: chainage station {station_id} falsely labels a "
            "display distance as measured source chainage",
            failures,
        )
        _check(
            displayed_m is not None
            and displayed_km is not None
            and math.isclose(
                displayed_m,
                displayed_km * 1_000.0,
                rel_tol=0.0,
                abs_tol=0.01,
            ),
            f"{artifact_id}: chainage station {station_id} displayed distance "
            "metadata is inconsistent",
            failures,
        )
        published_distance_km = _published_route_distance_km(route)
        expected_displayed_m = (
            published_distance_km * 1_000.0 * expected_fraction
            if published_distance_km is not None
            else measured_m
        )
        _check(
            displayed_m is not None
            and expected_displayed_m is not None
            and math.isclose(
                displayed_m,
                expected_displayed_m,
                rel_tol=0.0,
                abs_tol=0.01,
            ),
            f"{artifact_id}: chainage station {station_id} printed-distance "
            "axis does not match its disclosed basis",
            failures,
        )
        _check(
            measured_m is not None and measured_m > previous_measured_m,
            f"{artifact_id}: measured A-E chainage is not strictly increasing",
            failures,
        )
        if measured_m is not None:
            previous_measured_m = measured_m
        _check(
            path.get("data-chainage-basis")
            == "source-geometry-cumulative-geodesic-v1"
            and path.get("data-route-source-ref") == str(route.get("source_ref", ""))
            and path.get("data-profile-status")
            == str(route.get("profile_status", "")),
            f"{artifact_id}: chainage station {station_id} lacks route-source "
            "measurement provenance",
            failures,
        )


def _check_paired_hiking_variant_semantics(
    *,
    subject_id: str,
    variant_id: str,
    manifest: dict[str, Any],
    root: ET.Element,
    failures: list[str],
) -> None:
    """Check the v4 full-field, north-up paired hiking-map contract."""

    artifact_id = f"{subject_id}--{variant_id}"
    rendering = manifest.get("rendering") or {}
    _check(
        variant_id in EXPECTED_VARIANTS,
        f"{artifact_id}: unsupported hiking variant {variant_id!r}",
        failures,
    )
    _check(
        rendering.get("hiking_variant") == variant_id,
        f"{artifact_id}: rendering variant metadata drift",
        failures,
    )
    _check(
        rendering.get("orientation_policy") == "north-up"
        and rendering.get("north_is_page_up") is True
        and rendering.get("north_mark") is True,
        f"{artifact_id}: north-up rendering metadata is missing",
        failures,
    )

    catalog_record = manifest.get("catalog_record") or {}
    data_snapshot = (
        catalog_record.get("data_snapshot")
        if isinstance(catalog_record, dict)
        else None
    )
    _check(
        isinstance(data_snapshot, str)
        and bool(data_snapshot.strip())
        and manifest.get("data_snapshot") == data_snapshot
        and (manifest.get("source") or {}).get("timestamp") == data_snapshot,
        f"{artifact_id}: release data_snapshot did not propagate into the manifest",
        failures,
    )
    raw_context = (
        catalog_record.get("context") if isinstance(catalog_record, dict) else None
    )
    context = raw_context if isinstance(raw_context, dict) else {}
    rotation = context.get("rotation_deg")
    _check(
        _finite_number(rotation) == 0.0
        and context.get("orientation_status") == "north-up",
        f"{artifact_id}: catalog geometry is not explicitly north-up",
        failures,
    )

    paths_by_layer = _paths_by_logical_layer(root)
    all_paths = [path for paths in paths_by_layer.values() for path in paths]
    all_roles = _roles(all_paths)
    context_features = context.get("features")
    required_features = {
        str(feature.get("id")): feature
        for feature in context_features
        if isinstance(feature, dict)
        and feature.get("id")
        and feature.get("label_required") is True
    } if isinstance(context_features, list) else {}
    for required_id in sorted(required_features):
        labels = [
            path
            for path in all_paths
            if path.get("data-label-id") == required_id
        ]
        markers = [
            path
            for path in all_paths
            if path.get("data-feature-id") == required_id
            and path.get("data-role")
            in {"settlement-marker", "hut-marker", "pass-marker", "peak-marker"}
        ]
        _check(
            bool(labels) and bool(markers),
            f"{artifact_id}: required context feature {required_id!r} lacks its "
            "rendered marker or label",
            failures,
        )
        if labels:
            displacement = _finite_number(
                labels[0].get("data-source-label-displacement-mm")
            )
            maximum = _finite_number(
                labels[0].get("data-maximum-source-label-displacement-mm")
            )
            leaders = [
                path
                for path in all_paths
                if path.get("data-feature-id") == required_id
                and path.get("data-role") == "context-label-leader"
            ]
            _check(
                displacement is not None
                and maximum is not None
                and displacement <= maximum + 1e-9
                and (displacement <= 3.0 or bool(leaders)),
                f"{artifact_id}: required context feature {required_id!r} is "
                "too far from its source without a collision-safe leader",
                failures,
            )
    _check(
        not {"rotated-north-arrow", "rotated-north-arrow-head"} & all_roles,
        f"{artifact_id}: obsolete rotated-map north arrow was rendered",
        failures,
    )
    _check(
        {"north-arrow", "north-arrow-head", "north-label"} <= all_roles,
        f"{artifact_id}: visible north-up compass mark is missing",
        failures,
    )
    representation = (
        rendering.get("route_representation")
        if isinstance(rendering, dict)
        else None
    )
    _check(
        isinstance(representation, dict)
        and representation.get("sectional_detail_policy")
        == FULL_FIELD_CONTEXT_POLICY_ID,
        f"{artifact_id}: full-field continuous context policy is missing",
        failures,
    )
    _check(
        not (all_roles & FORBIDDEN_PAIRED_ROLES)
        and not any(path.get("data-context-view") for path in all_paths),
        f"{artifact_id}: inset, profile-frame, or fall-line marks violate the "
        "full-field v4 contract",
        failures,
    )
    _check(
        rendering.get("context_detail_insets") in (None, []),
        f"{artifact_id}: renderer metadata still declares context inset panels",
        failures,
    )

    family_evidence = _catalog_family_evidence(manifest)
    if family_evidence is not None:
        _check(
            set(family_evidence) == FAMILY_EVIDENCE_NAMES,
            f"{artifact_id}: context family availability evidence is incomplete",
            failures,
        )
        features = context.get("features") if isinstance(context, dict) else []
        features = features if isinstance(features, list) else []
        selected_features = {
            family: {
                str(feature.get("id")): feature
                for feature in features
                if isinstance(feature, dict)
                and feature.get("id")
                and str(feature.get("kind", "")) in FAMILY_FEATURE_KINDS[family]
            }
            for family in FAMILY_EVIDENCE_NAMES
        }
        family_paths = {
            "roads": [
                path
                for path in all_paths
                if path.get("data-role", "").startswith("source-sampled-road-")
            ],
            "hydrography": [
                path for path in all_paths if path.get("data-role") in WATER_ROLES
            ],
            "landcover": [
                path for path in all_paths if path.get("data-role") in LAND_COVER_ROLES
            ],
        }
        for family in sorted(FAMILY_EVIDENCE_NAMES):
            item = family_evidence.get(family, {})
            candidate_count = item.get("source_candidate_count")
            selected_count = item.get("selected_feature_count")
            valid_counts = (
                isinstance(candidate_count, int)
                and not isinstance(candidate_count, bool)
                and candidate_count >= 0
                and isinstance(selected_count, int)
                and not isinstance(selected_count, bool)
                and 0 <= selected_count <= candidate_count
            )
            _check(
                valid_counts,
                f"{artifact_id}: {family} availability counts are invalid",
                failures,
            )
            if not valid_counts:
                continue
            _check(
                selected_count == len(selected_features[family]),
                f"{artifact_id}: {family} selected count does not match the "
                "frozen catalog features",
                failures,
            )
            assessed_count = item.get(
                "page_legibility_assessed_feature_count", 0
            )
            legible_count = item.get("page_legible_feature_count", 0)
            sub_legible_count = item.get("sub_legible_feature_count", 0)
            valid_page_counts = (
                isinstance(assessed_count, int)
                and not isinstance(assessed_count, bool)
                and isinstance(legible_count, int)
                and not isinstance(legible_count, bool)
                and isinstance(sub_legible_count, int)
                and not isinstance(sub_legible_count, bool)
                and 0 <= assessed_count <= selected_count
                and 0 <= legible_count <= assessed_count
                and sub_legible_count == assessed_count - legible_count
            )
            _check(
                valid_page_counts,
                f"{artifact_id}: {family} page-legibility counts are invalid",
                failures,
            )
            expected_status = (
                "source-features-selected-sub-legible-at-page-scale"
                if (
                    selected_count
                    and valid_page_counts
                    and assessed_count
                    and legible_count == 0
                )
                else "source-features-selected"
                if selected_count
                else "source-candidates-unrenderable"
                if candidate_count
                else "source-query-zero-results"
            )
            _check(
                item.get("status") == expected_status,
                f"{artifact_id}: {family} availability status/count drift",
                failures,
            )

            # Selection evidence describes source objects before exact page
            # clipping and physical legibility filtering.  Reconcile emitted
            # ink as a provenance-preserving subset instead of requiring every
            # selected family to survive.  This keeps the factual check strict:
            # no rendered feature may be invented or borrowed from a different
            # family/source, while a sub-0.75 mm road may truthfully emit zero.
            rendered_ids: set[str] = set()
            for path in family_paths[family]:
                path_ids = _path_feature_ids(path)
                rendered_ids.update(path_ids)
                for feature_id in path_ids:
                    source_feature = selected_features[family].get(feature_id)
                    _check(
                        source_feature is not None,
                        f"{artifact_id}: rendered {family} feature {feature_id!r} "
                        "is absent from frozen selection evidence",
                        failures,
                    )
                    if source_feature is not None:
                        _check(
                            path.get("data-source-ref")
                            == str(source_feature.get("source_ref", "")),
                            f"{artifact_id}: rendered {family} feature "
                            f"{feature_id!r} cites the wrong frozen source",
                            failures,
                        )
            _check(
                rendered_ids <= set(selected_features[family]),
                f"{artifact_id}: rendered {family} post-clipping feature set "
                "does not match selection evidence",
                failures,
            )

            if family == "landcover":
                retention = rendering.get("green_context_retention")
                if isinstance(retention, dict):
                    _check(
                        retention.get("selected_source_feature_count")
                        == selected_count
                        and retention.get("final_rendered_feature_count")
                        == len(rendered_ids),
                        f"{artifact_id}: rendered landcover post-clipping "
                        "metadata does not reconcile with SVG provenance",
                        failures,
                    )
                    if GREEN_POST_MASK_TRIGGER_FIELDS & set(retention):
                        overview_paths = [
                            path
                            for path in family_paths[family]
                            if not path.get("data-context-view")
                        ]
                        final_rendered_ids: set[str] = set()
                        for path in overview_paths:
                            final_rendered_ids.update(_path_feature_ids(path))
                        expected_rendered_ids = sorted(final_rendered_ids)
                        required_fields_present = (
                            GREEN_POST_MASK_REQUIRED_FIELDS <= set(retention)
                        )
                        _check(
                            required_fields_present,
                            f"{artifact_id}: green post-mask metadata is incomplete",
                            failures,
                        )
                        _check(
                            retention.get("final_count_policy")
                            == GREEN_POST_MASK_FINAL_COUNT_POLICY_ID
                            and retention.get("post_copy_mask_rendered_feature_ids")
                            == expected_rendered_ids,
                            f"{artifact_id}: green post-mask rendered ID list or "
                            "count policy does not match final SVG provenance",
                            failures,
                        )

                        pre_mask_count = retention.get(
                            "pre_copy_mask_final_rendered_feature_count"
                        )
                        final_count = retention.get("final_rendered_feature_count")
                        omitted_feature_count = retention.get(
                            "copy_mask_omitted_feature_count"
                        )
                        valid_feature_arithmetic = all(
                            isinstance(value, int)
                            and not isinstance(value, bool)
                            and value >= 0
                            for value in (
                                pre_mask_count,
                                final_count,
                                omitted_feature_count,
                            )
                        )
                        if valid_feature_arithmetic:
                            assert isinstance(pre_mask_count, int)
                            assert isinstance(final_count, int)
                            assert isinstance(omitted_feature_count, int)
                            valid_feature_arithmetic = (
                                pre_mask_count >= final_count
                                and final_count == len(expected_rendered_ids)
                                and omitted_feature_count
                                == pre_mask_count - final_count
                            )
                        _check(
                            valid_feature_arithmetic,
                            f"{artifact_id}: green post-mask pre/final/omitted "
                            "feature arithmetic is invalid",
                            failures,
                        )

                        contour_mask = rendering.get(
                            "contour_altitude_legibility_mask"
                        )
                        role_statistics = (
                            contour_mask.get("role_statistics")
                            if isinstance(contour_mask, dict)
                            else None
                        )
                        counter_fields = (
                            "affected_record_count",
                            "omitted_record_count",
                            "emitted_part_count",
                        )
                        aggregate_counters = dict.fromkeys(counter_fields, 0)
                        valid_role_statistics = (
                            isinstance(contour_mask, dict)
                            and contour_mask.get("policy_id")
                            == CONTOUR_COPY_MASK_POLICY_ID
                            and isinstance(role_statistics, dict)
                        )
                        if isinstance(role_statistics, dict):
                            for role in LAND_COVER_ROLES:
                                summary = role_statistics.get(role)
                                if summary is None:
                                    continue
                                if not isinstance(summary, dict):
                                    valid_role_statistics = False
                                    continue
                                values = [summary.get(field) for field in counter_fields]
                                if not all(
                                    isinstance(value, int)
                                    and not isinstance(value, bool)
                                    and value >= 0
                                    for value in values
                                ):
                                    valid_role_statistics = False
                                    continue
                                affected, omitted, emitted = values
                                assert isinstance(affected, int)
                                assert isinstance(omitted, int)
                                assert isinstance(emitted, int)
                                if (
                                    omitted > affected
                                    or emitted < affected - omitted
                                ):
                                    valid_role_statistics = False
                                for field, value in zip(
                                    counter_fields, values, strict=True
                                ):
                                    assert isinstance(value, int)
                                    aggregate_counters[field] += value

                        declared_counters = {
                            "affected_record_count": retention.get(
                                "copy_mask_affected_landcover_record_count"
                            ),
                            "omitted_record_count": retention.get(
                                "copy_mask_omitted_landcover_record_count"
                            ),
                            "emitted_part_count": retention.get(
                                "copy_mask_emitted_landcover_path_part_count"
                            ),
                        }
                        _check(
                            valid_role_statistics
                            and declared_counters == aggregate_counters,
                            f"{artifact_id}: green post-mask aggregate counters "
                            "do not match contour-mask role statistics",
                            failures,
                        )
                        masked_svg_part_count = sum(
                            path.get("data-copy-legibility-mask-policy")
                            == CONTOUR_COPY_MASK_POLICY_ID
                            for path in overview_paths
                        )
                        _check(
                            declared_counters["emitted_part_count"]
                            == masked_svg_part_count,
                            f"{artifact_id}: green post-mask emitted-part count "
                            "does not match final SVG mask provenance",
                            failures,
                        )

    sources = manifest.get("sources") or []
    sources_by_id = {
        str(source.get("id")): source
        for source in sources
        if isinstance(source, dict) and source.get("id")
    }
    source_ids = set(sources_by_id)
    route = catalog_record.get("route") if isinstance(catalog_record, dict) else None
    _check(
        isinstance(route, dict)
        and route.get("profile_status") == "source-elevation-sampled",
        f"{artifact_id}: sourced route elevation profile is missing",
        failures,
    )
    if isinstance(route, dict):
        elevation_source_ref = str(route.get("elevation_source_ref", ""))
        elevation_method = str(route.get("elevation_method", ""))
        elevation_datum = str(route.get("elevation_datum", ""))
        profile_paths = [
            path
            for path in all_paths
            if path.get("data-role") == "source-elevation-profile"
        ]
        _check(
            elevation_source_ref in source_ids
            and bool(elevation_method)
            and (
                bool(elevation_datum)
                or elevation_method == "route-source-embedded-elevation-v1"
            ),
            f"{artifact_id}: route elevation profile lacks source provenance",
            failures,
        )
        _check(
            bool(profile_paths)
            and all(
                path.get("data-source-ref") == elevation_source_ref
                and path.get("data-elevation-method") == elevation_method
                and str(path.get("data-elevation-datum") or "") == elevation_datum
                for path in profile_paths
            ),
            f"{artifact_id}: rendered elevation profile cites the wrong source",
            failures,
        )
        profile_segments = route.get("profile_segments")
        if isinstance(profile_segments, list):
            expected_source_points = sum(
                len(segment.get("points", ()))
                for segment in profile_segments
                if isinstance(segment, dict)
            )
            actual_rendered_vertices = sum(
                len(points)
                for path in profile_paths
                if (points := _parse_linear_ml_path(path.get("d"))) is not None
            )
            declared_source_counts = {
                _finite_number(path.get("data-profile-source-point-count"))
                for path in profile_paths
            }
            declared_rendered_counts = {
                _finite_number(path.get("data-profile-rendered-vertex-count"))
                for path in profile_paths
            }
            declared_pitch = {
                _finite_number(path.get("data-profile-average-vertex-pitch-mm"))
                for path in profile_paths
            }
            policies = {
                path.get("data-profile-generalization-policy")
                for path in profile_paths
            }
            _check(
                declared_source_counts == {float(expected_source_points)}
                and declared_rendered_counts == {float(actual_rendered_vertices)},
                f"{artifact_id}: physical profile generalization counts do not "
                "reconcile with the complete source inventory and SVG",
                failures,
            )
            if policies == {
                "adaptive-physical-rdp-nib-pitch-preserve-global-extrema-v1"
            }:
                _check(
                    declared_pitch
                    and None not in declared_pitch
                    and min(float(value) for value in declared_pitch if value is not None)
                    >= 0.25
                    and all(
                        path.get(
                            "data-profile-global-extrema-vertices-preserved"
                        )
                        == "true"
                        and (
                            tolerance := _finite_number(
                                path.get(
                                    "data-profile-simplification-tolerance-mm"
                                )
                            )
                        )
                        is not None
                        and 0.1 <= tolerance <= 0.3
                        for path in profile_paths
                    ),
                    f"{artifact_id}: dense profile was not generalized above "
                    "the 0.25 mm pen pitch with its extrema retained",
                    failures,
                )
        _check_chainage_profile_contract(
            artifact_id=artifact_id,
            route=route,
            all_paths=all_paths,
            failures=failures,
        )
        expected_extrema = _expected_profile_extrema_disclosure(route)
        extrema_rendering = (
            rendering.get("profile_extrema_disclosure")
            if isinstance(rendering, dict)
            else None
        )
        profile_minimum = (
            _finite_number(profile_paths[0].get("data-elevation-min-m"))
            if profile_paths
            else None
        )
        profile_maximum = (
            _finite_number(profile_paths[0].get("data-elevation-max-m"))
            if profile_paths
            else None
        )
        _check(
            expected_extrema is not None
            and isinstance(extrema_rendering, dict)
            and (
                extrema_rendering.get("status"),
                extrema_rendering.get("policy_id"),
                extrema_rendering.get("source_ref"),
            )
            == expected_extrema[:3]
            and profile_minimum is not None
            and profile_maximum is not None
            and _close(extrema_rendering.get("minimum_m"), profile_minimum)
            and _close(extrema_rendering.get("maximum_m"), profile_maximum)
            and extrema_rendering.get("distance_label_basis")
            == expected_extrema[3]
            and (
                _published_route_distance_km(route) is None
                or _close(
                    extrema_rendering.get("official_total_distance_km"),
                    _published_route_distance_km(route),
                )
            )
            and extrema_rendering.get("caption")
            == _expected_profile_extrema_caption(
                route, profile_minimum, profile_maximum
            ),
            f"{artifact_id}: profile extrema rendering disclosure drift",
            failures,
        )

    terrain_field, terrain, terrain_policy_valid = _terrain_for_variant(
        context,
        variant_id,
        rendering=rendering if isinstance(rendering, dict) else {},
        catalog_record=(catalog_record if isinstance(catalog_record, dict) else {}),
    )
    _check(
        terrain_policy_valid,
        f"{artifact_id}: detailed terrain source policy is missing, malformed, "
        "or unsupported by frozen sparse-native evidence",
        failures,
    )
    terrain_source = (
        str(terrain.get("source_ref", "")) if isinstance(terrain, dict) else ""
    )
    contours = terrain.get("contours") if isinstance(terrain, dict) else None
    terrain_source_record = sources_by_id.get(terrain_source, {})
    _check(
        isinstance(terrain, dict)
        and terrain.get("status") == "source-derived-dtm-relief"
        and terrain_source in source_ids
        and _source_has_frozen_snapshot(terrain_source_record),
        f"{artifact_id}: {terrain_field} lacks frozen factual source provenance",
        failures,
    )
    _check(
        isinstance(contours, list)
        and bool(contours)
        and all(
            isinstance(contour, dict)
            and _finite_number(contour.get("elevation_m")) is not None
            and bool(contour.get("paths"))
            for contour in contours
        ),
        f"{artifact_id}: {terrain_field} lacks elevation-valued source contours",
        failures,
    )

    terrain_derivation = (
        catalog_record.get("terrain_derivation")
        if isinstance(catalog_record, dict)
        else None
    )
    precedence = (
        terrain_derivation.get("source_precedence")
        if isinstance(terrain_derivation, dict)
        else None
    )
    route_elevation_source = (
        str(route.get("elevation_source_ref", ""))
        if isinstance(route, dict)
        else ""
    )
    detailed_terrain = context.get("terrain") if isinstance(context, dict) else None
    detailed_terrain_source = (
        str(detailed_terrain.get("source_ref", ""))
        if isinstance(detailed_terrain, dict)
        else ""
    )
    relief_terrain = (
        context.get("relief_terrain") if isinstance(context, dict) else None
    )
    relief_terrain_source = (
        str(relief_terrain.get("source_ref", ""))
        if isinstance(relief_terrain, dict)
        else detailed_terrain_source
    )
    native_terrain = not detailed_terrain_source.startswith(
        GLOBAL_TERRAIN_SOURCE_PREFIX
    )
    global_profile = route_elevation_source.startswith(GLOBAL_TERRAIN_SOURCE_PREFIX)
    global_relief = (
        isinstance(relief_terrain, dict)
        and relief_terrain_source.startswith(GLOBAL_TERRAIN_SOURCE_PREFIX)
    )
    relief_precedence_matches = (
        precedence.get("relief_terrain_source_ref") == relief_terrain_source
        and precedence.get("global_relief_terrain_retained") is global_relief
        if isinstance(precedence, dict)
        and (
            "relief_terrain_source_ref" in precedence
            or "global_relief_terrain_retained" in precedence
        )
        else not isinstance(relief_terrain, dict)
    )
    _check(
        isinstance(precedence, dict)
        and precedence.get("policy_id") == SOURCE_PRECEDENCE_POLICY_ID
        and precedence.get("terrain_source_ref") == detailed_terrain_source
        and precedence.get("route_elevation_source_ref")
        == route_elevation_source
        and precedence.get("native_terrain_restored") is native_terrain
        and precedence.get("global_route_profile_retained") is global_profile
        and relief_precedence_matches,
        f"{artifact_id}: native/global terrain-profile source precedence drift",
        failures,
    )

    features = context.get("features") if isinstance(context, dict) else []
    features = features if isinstance(features, list) else []
    peak_features = {
        str(feature.get("id")): feature
        for feature in features
        if isinstance(feature, dict)
        and feature.get("kind") == "peak"
        and feature.get("id")
        and feature.get("display_label", True) is not False
    }
    for peak_id, feature in peak_features.items():
        elevation = _finite_number(feature.get("elevation_m"))
        elevation_source = str(feature.get("elevation_source_ref", ""))
        elevation_method = str(feature.get("elevation_method", ""))
        if elevation is None:
            _check(
                not elevation_method and not elevation_source,
                f"{artifact_id}: source peak {peak_id!r} carries partial or "
                "inferred altitude metadata",
                failures,
            )
            continue
        explicit_source_object = str(
            feature.get("elevation_source_object")
            or (
                f"{feature.get('osm_type')}/{feature.get('osm_id')}"
                if feature.get("osm_type") and feature.get("osm_id")
                else ""
            )
        )
        _check(
            elevation_method in EXPLICIT_ELEVATION_METHODS
            and elevation_source in source_ids
            and (
                elevation_method != "osm-ele-tag"
                or (
                    explicit_source_object.startswith("node/")
                    and feature.get("osm_type") == "node"
                )
            ),
            f"{artifact_id}: source peak {peak_id!r} altitude is not from an "
            "explicit authoritative source",
            failures,
        )

    peak_label_paths = [
        path
        for path in paths_by_layer.get("context_labels", [])
        if path.get("data-role") == "peak-label"
    ]
    rendered_peak_ids = {
        str(path.get("data-label-id"))
        for path in peak_label_paths
        if path.get("data-label-id")
    }
    if peak_features:
        _check(
            bool(rendered_peak_ids),
            f"{artifact_id}: source peaks exist but no peak label was rendered",
            failures,
        )
    _check(
        rendered_peak_ids <= set(peak_features),
        f"{artifact_id}: rendered peak labels do not resolve to catalog peaks",
        failures,
    )
    peak_paths_by_id: dict[str, list[ET.Element]] = {}
    for path in peak_label_paths:
        peak_paths_by_id.setdefault(str(path.get("data-label-id", "")), []).append(
            path
        )
    for peak_id, label_paths in sorted(peak_paths_by_id.items()):
        source_peak = peak_features.get(peak_id)
        if source_peak is None:
            # The set-level failure above already identifies this invented
            # label; do not multiply it by the number of glyph paths.
            continue
        source_elevation = _finite_number(source_peak.get("elevation_m"))
        if source_elevation is None:
            _check(
                all(
                    _finite_number(path.get("data-elevation-m")) is None
                    and path.get("data-elevation-method") is None
                    and path.get("data-elevation-source-ref") is None
                    for path in label_paths
                ),
                f"{artifact_id}: peak label {peak_id!r} invents an altitude",
                failures,
            )
        else:
            _check(
                all(
                    (rendered_elevation := _finite_number(
                        path.get("data-elevation-m")
                    ))
                    is not None
                    # Renderer metadata is serialized to 0.01 m.  Accept only
                    # that explicit rounding envelope, not an inferred height.
                    and math.isclose(
                        rendered_elevation,
                        source_elevation,
                        rel_tol=0.0,
                        abs_tol=0.005001,
                    )
                    and path.get("data-elevation-method")
                    == source_peak.get("elevation_method")
                    and path.get("data-elevation-source-ref")
                    == source_peak.get("elevation_source_ref")
                    for path in label_paths
                )
                and len(
                    {
                        (
                            path.get("data-elevation-m"),
                            path.get("data-elevation-method"),
                            path.get("data-elevation-source-ref"),
                        )
                        for path in label_paths
                    }
                )
                == 1,
                f"{artifact_id}: peak label {peak_id!r} lacks matching explicit "
                "altitude provenance",
                failures,
            )

    relief_paths = [
        *paths_by_layer.get("context_relief", []),
        *paths_by_layer.get("context_relief_index", []),
    ]
    contour_paths = [
        path
        for path in relief_paths
        if path.get("data-role") == "source-derived-dtm-contour"
    ]
    minor_contour_paths = [
        path
        for path in paths_by_layer.get("context_relief", [])
        if path.get("data-role") == "source-derived-dtm-contour"
    ]
    index_contour_paths = [
        path
        for path in paths_by_layer.get("context_relief_index", [])
        if path.get("data-role") == "source-derived-dtm-contour"
    ]
    altitude_paths = [
        path
        for path in paths_by_layer.get("context_relief_labels", [])
        if path.get("data-role") == "source-derived-contour-altitude-label"
    ]
    source_levels = {
        float(contour["elevation_m"])
        for contour in contours or []
        if isinstance(contour, dict)
        and _finite_number(contour.get("elevation_m")) is not None
        and contour.get("paths")
    }
    rendered_levels = {
        float(path.get("data-elevation-m", "nan"))
        for path in contour_paths
        if _finite_number(path.get("data-elevation-m")) is not None
    }
    minimum_levels = min(4, len(source_levels))
    maximum_levels = min(8, len(source_levels))
    _check(
        bool(contour_paths)
        and minimum_levels <= len(rendered_levels)
        and rendered_levels <= source_levels
        and (
            variant_id != "detailed-map" or len(rendered_levels) <= maximum_levels
        ),
        f"{artifact_id}: full-field contour stack is missing, discontinuous, or "
        "outside the 4-8 context-level policy",
        failures,
    )
    _check(
        all(
            (elevation := _finite_number(path.get("data-elevation-m"))) is not None
            and elevation >= 0.0
            for path in contour_paths
        ),
        f"{artifact_id}: negative or invalid contour elevation rendered while "
        "bathymetry is disabled",
        failures,
    )
    for path in contour_paths:
        contour_elevation = _finite_number(path.get("data-elevation-m"))
        _check(
            path.get("data-source-ref") == terrain_source
            and path.get("data-relief-status") == "source-derived-dtm"
            and contour_elevation is not None
            and contour_elevation >= 0.0
            and path.get("data-contour-class")
            in {"minor", "index"}
            and path.get("data-contour-hierarchy-policy")
            == "factual-fifth-index-grey-pen-hierarchy-v1"
            and path.get("data-bathymetry-status")
            == "not-rendered-no-qualified-source",
            f"{artifact_id}: a rendered contour lacks factual altitude metadata",
            failures,
        )

    minor_semantic_keys = {
        (
            path.get("data-source-ref"),
            path.get("data-contour-id"),
            path.get("data-elevation-m"),
        )
        for path in minor_contour_paths
    }
    index_semantic_keys = {
        (
            path.get("data-source-ref"),
            path.get("data-contour-id"),
            path.get("data-elevation-m"),
        )
        for path in index_contour_paths
    }
    minor_geometry_keys = {
        (
            path.get("data-source-ref"),
            path.get("data-contour-id"),
            path.get("data-elevation-m"),
            path.get("d"),
        )
        for path in minor_contour_paths
    }
    index_geometry_keys = {
        (
            path.get("data-source-ref"),
            path.get("data-contour-id"),
            path.get("data-elevation-m"),
            path.get("d"),
        )
        for path in index_contour_paths
    }
    _check(
        minor_semantic_keys.isdisjoint(index_semantic_keys)
        and minor_geometry_keys.isdisjoint(index_geometry_keys),
        f"{artifact_id}: minor/index contour partitions overlap",
        failures,
    )

    hierarchy = rendering.get("terrain_contour_hierarchy")
    index_paths = paths_by_layer.get("context_relief_index", [])
    _check(
        isinstance(hierarchy, dict)
        and hierarchy.get("policy_id")
        == "factual-fifth-index-grey-pen-hierarchy-v1"
        and hierarchy.get("minor_pen_id") == "grey-0-25"
        and hierarchy.get("index_pen_id") == "grey-0-4"
        and _finite_number(hierarchy.get("minor_pen_width_mm")) == 0.25
        and _finite_number(hierarchy.get("index_pen_width_mm")) == 0.4
        and hierarchy.get("index_every_n_minor_levels") == 5
        and hierarchy.get("zero_elevation_index_suppressed") is True
        and hierarchy.get("bathymetry_status")
        == "not-rendered-no-qualified-source",
        f"{artifact_id}: contour hierarchy metadata is missing or invalid",
        failures,
    )
    hierarchy_index_levels = (
        hierarchy.get("index_levels_m") if isinstance(hierarchy, dict) else None
    )
    rendered_index_levels = {
        float(path.get("data-elevation-m", "nan"))
        for path in index_paths
        if _finite_number(path.get("data-elevation-m")) is not None
    }
    _check(
        isinstance(hierarchy_index_levels, list)
        and bool(index_paths) == bool(hierarchy_index_levels)
        and rendered_index_levels
        == {
            float(level)
            for level in hierarchy_index_levels
            if _finite_number(level) is not None
        }
        and all(
            path.get("data-role") == "source-derived-dtm-contour"
            and path.get("data-contour-class") == "index"
            and path.get("data-contour-pen-width-mm") == "0.4"
            and _finite_number(path.get("data-elevation-m")) not in {None, 0.0}
            for path in index_paths
        )
        and all(
            path.get("data-contour-class") != "index"
            and path.get("data-contour-pen-width-mm") == "0.25"
            for path in paths_by_layer.get("context_relief", [])
            if path.get("data-role") == "source-derived-dtm-contour"
        ),
        f"{artifact_id}: minor/index contours are not separated onto Grey 0.25/0.40",
        failures,
    )

    if variant_id == "detailed-map":
        _check(
            not altitude_paths,
            f"{artifact_id}: detailed-map contains relief-only altitude labels",
            failures,
        )
        return

    _check(
        bool(altitude_paths),
        f"{artifact_id}: terrain-relief has no source-valued contour labels",
        failures,
    )
    labelled_levels = {
        float(path.get("data-elevation-m", "nan"))
        for path in altitude_paths
        if _finite_number(path.get("data-elevation-m")) is not None
    }
    _check(
        1 <= len(labelled_levels) <= 4
        and labelled_levels <= rendered_index_levels,
        f"{artifact_id}: relief altitude copy is not limited to 1-4 rendered index levels",
        failures,
    )
    for path in altitude_paths:
        _check(
            path.get("data-source-ref") == terrain_source
            and path.get("data-relief-status") == "source-derived-dtm"
            and bool(path.get("data-contour-id"))
            and path.get("data-contour-class") == "index"
            and _finite_number(path.get("data-elevation-m")) is not None,
            f"{artifact_id}: a contour altitude label lacks factual elevation metadata",
            failures,
        )

    source_fall_lines = (
        terrain.get("relief_strokes", []) if isinstance(terrain, dict) else []
    )
    fall_line_rendering = (
        rendering.get("terrain_fall_lines")
        if isinstance(rendering, dict)
        else None
    )
    _check(
        isinstance(source_fall_lines, list)
        and isinstance(fall_line_rendering, dict)
        and fall_line_rendering.get("source_stroke_count")
        == len(source_fall_lines)
        and fall_line_rendering.get("clearance_eligible_path_count") == 0
        and fall_line_rendering.get("cluster_rejected_path_count") == 0
        and fall_line_rendering.get("retained_path_count") == 0
        and fall_line_rendering.get("omission_reason")
        in {
            FALL_LINE_NO_SOURCE_OMISSION_REASON,
            FALL_LINE_NO_CLUSTER_OMISSION_REASON,
        },
        f"{artifact_id}: zero-hachure terrain policy does not reconcile with "
        "frozen source evidence",
        failures,
    )


def qa_entry(
    entry: dict[str, Any], *, expected_dpi: float, series_dir: Path
) -> dict[str, Any]:
    failures: list[str] = []
    artifact_id = str(entry.get("artifact_id") or entry.get("id") or "").strip()
    subject_id = str(entry.get("subject_id") or "").strip()
    variant_id = str(entry.get("variant_id") or "").strip()
    identity_label = artifact_id or subject_id or "<unknown hiking artifact>"
    _check(bool(subject_id), f"{identity_label}: index subject_id is missing", failures)
    _check(
        variant_id in EXPECTED_VARIANTS,
        f"{identity_label}: index variant_id is invalid",
        failures,
    )
    expected_artifact_id = f"{subject_id}--{variant_id}"
    _check(
        bool(artifact_id)
        and artifact_id == expected_artifact_id
        and entry.get("id") == artifact_id,
        f"{identity_label}: index artifact identity drift",
        failures,
    )
    outputs = entry.get("outputs") or {}
    svg_path = Path((outputs.get("svg") or {}).get("path", ""))
    manifest_path = Path((outputs.get("manifest") or {}).get("path", ""))
    png_path = Path((outputs.get("png") or {}).get("path", ""))
    _check(svg_path.is_file(), f"{subject_id}: SVG is missing", failures)
    _check(manifest_path.is_file(), f"{subject_id}: manifest is missing", failures)
    _check(png_path.is_file(), f"{subject_id}: PNG is missing", failures)
    if failures:
        return {
            "id": identity_label,
            "artifact_id": artifact_id,
            "subject_id": subject_id,
            "variant_id": variant_id,
            "passed": False,
            "failures": failures,
        }

    expected_parent = (series_dir / "hikes").resolve()
    for label, path in (
        ("SVG", svg_path),
        ("manifest", manifest_path),
        ("PNG", png_path),
    ):
        _check(
            path.resolve().is_relative_to(series_dir),
            f"{subject_id}: {label} output escapes the series directory",
            failures,
        )
        _check(
            path.resolve().parent == expected_parent,
            f"{subject_id}: {label} is not directly inside the hiking output directory",
            failures,
        )
    for output_name in ("svg", "manifest", "png"):
        output = outputs[output_name]
        expected_hash = output.get("sha256")
        path = Path(output["path"])
        _check(
            isinstance(expected_hash, str) and expected_hash == _sha256(path),
            f"{subject_id}: {output_name} SHA-256 mismatch",
            failures,
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failures.append(f"{subject_id}: invalid manifest JSON: {exc}")
        return {
            "id": identity_label,
            "artifact_id": artifact_id,
            "subject_id": subject_id,
            "variant_id": variant_id,
            "passed": False,
            "failures": failures,
        }
    try:
        root = ET.parse(svg_path).getroot()
    except (OSError, ET.ParseError) as exc:
        failures.append(f"{subject_id}: invalid SVG XML: {exc}")
        return {
            "id": identity_label,
            "artifact_id": artifact_id,
            "subject_id": subject_id,
            "variant_id": variant_id,
            "passed": False,
            "failures": failures,
        }

    _check(
        manifest.get("subject_id") == subject_id,
        f"{subject_id}: manifest ID drift",
        failures,
    )
    _check(
        manifest.get("artifact_id") == artifact_id
        and manifest.get("variant_id") == variant_id,
        f"{identity_label}: manifest artifact/variant identity drift",
        failures,
    )
    metadata_node = root.find(f"{{{SVG_NS}}}metadata")
    try:
        svg_metadata = (
            json.loads(metadata_node.text or "") if metadata_node is not None else {}
        )
    except json.JSONDecodeError:
        svg_metadata = {}
    _check(
        svg_metadata.get("subject_id") == subject_id
        and svg_metadata.get("artifact_id") == artifact_id
        and svg_metadata.get("variant_id") == variant_id,
        f"{identity_label}: embedded SVG artifact identity drift",
        failures,
    )
    _check(
        entry.get("domain") == "hikes",
        f"{subject_id}: index domain is not hikes",
        failures,
    )
    _check(
        manifest.get("domain") == "hikes",
        f"{subject_id}: manifest domain is not hikes",
        failures,
    )
    _check(
        manifest.get("artifact_kind") == "hiking-pen-map"
        and manifest.get("subject_kind") == "route_plate",
        f"{subject_id}: manifest is not a hiking route plate",
        failures,
    )
    for key in (
        "title",
        "subtitle",
        "scale_status",
        "evidence_status",
        "rights_status",
    ):
        manifest_value = (
            (manifest.get("evidence") or {}).get(key)
            if key in {"scale_status", "evidence_status"}
            else (manifest.get("rights") or {}).get("status")
            if key == "rights_status"
            else manifest.get(key)
        )
        _check(
            entry.get(key) == manifest_value,
            f"{subject_id}: index/manifest {key} drift",
            failures,
        )

    sources = manifest.get("sources") or []
    _check(bool(sources), f"{subject_id}: no source records", failures)
    source_ids: list[str] = []
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            failures.append(f"{subject_id}: source {index} is not an object")
            continue
        source_ids.append(str(source.get("id", "")))
        _check(
            bool(source.get("id")),
            f"{subject_id}: source {index} has no stable ID",
            failures,
        )
        _check(
            str(source.get("url", "")).startswith("https://"),
            f"{subject_id}: source {index} has no HTTPS URL",
            failures,
        )
        for field in ("publisher", "license", "attribution", "use", "retrieved_at"):
            _check(
                bool(str(source.get(field, "")).strip()),
                f"{subject_id}: source {index} has no {field}",
                failures,
            )
    _check(
        len(source_ids) == len(set(source_ids)),
        f"{subject_id}: source IDs are not unique",
        failures,
    )
    evidence = manifest.get("evidence") or {}
    _check(
        bool(evidence.get("scale_status")),
        f"{subject_id}: no scale disclosure",
        failures,
    )
    rights = manifest.get("rights") or {}
    _check(
        rights.get("logos_or_trade_dress_used") is False,
        f"{subject_id}: logo gate failed",
        failures,
    )
    _check(
        rights.get("broadcast_frames_traced") is False,
        f"{subject_id}: trace gate failed",
        failures,
    )
    readiness = manifest.get("production_readiness") or {}
    _check(
        readiness.get("production_ready") is False
        and readiness.get("mode") == "review-only",
        f"{subject_id}: uncalibrated example is not fail-closed review-only",
        failures,
    )

    sequence = manifest.get("pen_sequence") or []
    steps = [record.get("step") for record in sequence]
    pen_ids = [record.get("pen_id") for record in sequence]
    _check(
        steps == list(range(1, len(steps) + 1)),
        f"{subject_id}: non-contiguous steps",
        failures,
    )
    _check(len(pen_ids) == len(set(pen_ids)), f"{subject_id}: pen reloaded", failures)
    expected_pen_ids = ["grey-0-25"]
    if "context_relief_index" in _paths_by_logical_layer(root):
        expected_pen_ids.append("grey-0-4")
    expected_pen_ids.append("blue-0-25")
    if _catalog_declares_landcover(manifest):
        expected_pen_ids.append("green-0-25")
    expected_pen_ids.extend(["black-0-25", "black-0-6", "red-0-4"])
    _check(
        pen_ids == expected_pen_ids,
        f"{subject_id}: hiking pen plan/order drift: {pen_ids}",
        failures,
    )
    physical_layers = _physical_layers(root)
    _check(
        [layer.get("data-pen-step") for layer in physical_layers]
        == [str(step) for step in steps],
        f"{subject_id}: SVG physical layer steps drift from the manifest",
        failures,
    )
    _check(
        [layer.get("id") for layer in physical_layers]
        == [f"layer-pen-{pen_id}" for pen_id in pen_ids],
        f"{subject_id}: SVG physical layer order drifts from the pen sequence",
        failures,
    )
    manifest_logical_layers = {
        str(logical_id)
        for layer in manifest.get("layers") or []
        for logical_id in (layer.get("logical_layers") or [])
    }
    _check(
        manifest_logical_layers == set(_paths_by_logical_layer(root)),
        f"{subject_id}: SVG logical layers drift from the manifest",
        failures,
    )

    pen_files = outputs.get("pen_files") or []
    _check(
        len(pen_files) == len(sequence),
        f"{subject_id}: split-pen file count does not match its pen plan",
        failures,
    )
    for expected_step, pen_file in enumerate(pen_files, start=1):
        path = Path(str(pen_file.get("path", "")))
        _check(
            path.is_file(),
            f"{subject_id}: missing split-pen file {expected_step}",
            failures,
        )
        if not path.is_file():
            continue
        _check(
            path.resolve().is_relative_to(series_dir),
            f"{subject_id}: split-pen file {expected_step} escapes the series directory",
            failures,
        )
        _check(
            path.resolve().parent == expected_parent,
            f"{subject_id}: split-pen file {expected_step} is outside the hiking directory",
            failures,
        )
        _check(
            pen_file.get("step") == expected_step,
            f"{subject_id}: split-pen file order drift at step {expected_step}",
            failures,
        )
        expected_sequence = (
            sequence[expected_step - 1] if expected_step <= len(sequence) else {}
        )
        _check(
            pen_file.get("pen_id") == expected_sequence.get("pen_id"),
            f"{subject_id}: split-pen file {expected_step} pen ID drift",
            failures,
        )
        _check(
            pen_file.get("sha256") == _sha256(path),
            f"{subject_id}: split-pen file {expected_step} SHA-256 mismatch",
            failures,
        )
        pen_root = ET.parse(path).getroot()
        pen_layers = [
            child
            for child in pen_root
            if child.tag == f"{{{SVG_NS}}}g"
            and child.get("{http://www.inkscape.org/namespaces/inkscape}groupmode")
            == "layer"
        ]
        _check(
            len(pen_layers) == 1
            and pen_layers[0].get("data-pen-step") == str(expected_step),
            f"{subject_id}: split-pen file {expected_step} is not a single physical layer",
            failures,
        )
        if len(pen_layers) == 1 and expected_step <= len(physical_layers):
            _check(
                _master_group_signature(pen_layers[0])
                == _master_group_signature(physical_layers[expected_step - 1]),
                f"{subject_id}: split-pen file {expected_step} geometry/metadata drift",
                failures,
            )
    summary = manifest.get("plot_summary") or {}
    coverage = summary.get("field_ink_coverage_upper_bound")
    _check(
        _valid_coverage_measurement(coverage),
        f"{subject_id}: ink coverage measurement is missing/invalid",
        failures,
    )
    travel = summary.get("travel_ratio")
    _check(
        isinstance(travel, (int, float)) and travel <= 2.0,
        f"{subject_id}: travel ratio failed",
        failures,
    )

    for forbidden in (
        "text",
        "image",
        "use",
        "circle",
        "ellipse",
        "rect",
        "line",
        "polyline",
        "polygon",
        "foreignObject",
    ):
        _check(
            not root.findall(f".//{{{SVG_NS}}}{forbidden}"),
            f"{subject_id}: contains forbidden <{forbidden}> elements",
            failures,
        )
    paths = root.findall(f".//{{{SVG_NS}}}path")
    _check(bool(paths), f"{subject_id}: SVG has no plotted paths", failures)
    _check(
        all(path.get("data-logical-layer") for path in paths),
        f"{subject_id}: a plotted path has no logical-layer metadata",
        failures,
    )

    _check_a5_contract(
        subject_id=subject_id,
        entry=entry,
        manifest=manifest,
        root=root,
        failures=failures,
    )
    _check_hiking_semantics(
        subject_id=subject_id,
        manifest=manifest,
        root=root,
        failures=failures,
    )
    _check_paired_hiking_variant_semantics(
        subject_id=subject_id,
        variant_id=variant_id,
        manifest=manifest,
        root=root,
        failures=failures,
    )
    code, validator_output = _run(
        [str(PYTHON), str(VALIDATOR), str(svg_path), "--warnings-as-errors"]
    )
    _check(
        code == 0,
        f"{subject_id}: format validator failed: {validator_output}",
        failures,
    )

    code, plotsim_output = _run([str(PYTHON), str(PLOTSIM), str(svg_path), "--compare"])
    _check(code == 0, f"{subject_id}: plotsim failed: {plotsim_output}", failures)
    ratios = [
        float(value) for value in re.findall(r"ratio\s+([0-9.]+)x", plotsim_output)
    ]
    _check(
        bool(ratios) and max(ratios) <= 2.0,
        f"{subject_id}: plotsim ratio unavailable/high",
        failures,
    )

    page = manifest.get("page") or {}
    try:
        expected_size = (
            round(float(page["width_mm"]) * expected_dpi / 25.4),
            round(float(page["height_mm"]) * expected_dpi / 25.4),
        )
        actual_size = _png_dimensions(png_path)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        expected_size = (0, 0)
        actual_size = (0, 0)
        failures.append(f"{subject_id}: PNG/page metadata is invalid: {exc}")
    _check(
        _close((outputs.get("png") or {}).get("dpi"), expected_dpi),
        f"{subject_id}: PNG DPI metadata does not equal requested {expected_dpi:g}",
        failures,
    )
    _check(
        actual_size == expected_size,
        f"{subject_id}: PNG {actual_size} != {expected_size}",
        failures,
    )
    parity, parity_detail = _raster_parity(svg_path, png_path, dpi=expected_dpi)
    _check(
        parity,
        f"{subject_id}: PNG pixels drift from the master SVG ({parity_detail})",
        failures,
    )
    return {
        "id": artifact_id,
        "artifact_id": artifact_id,
        "subject_id": subject_id,
        "variant_id": variant_id,
        "domain": entry.get("domain"),
        "passed": not failures,
        "failures": failures,
        "format_id": entry.get("format_id"),
        "pen_steps": len(sequence),
        "coverage": coverage,
        "travel_ratio": travel,
        "plotsim_ratios": ratios,
        "png_dimensions": actual_size,
        "png_pixel_parity": parity,
    }


def _check_suite_contract(
    *,
    index: dict[str, Any],
    entries: list[dict[str, Any]],
    series_dir: Path,
    failures: list[str],
    expected_subject_ids: frozenset[str] | set[str] | None = None,
) -> None:
    if expected_subject_ids is None:
        try:
            expected_subject_ids = _release_subject_ids()
        except Exception as exc:  # the release gate must report, not crash
            expected_subject_ids = frozenset()
            failures.append(f"could not resolve hiking release subject IDs: {exc}")
    expected_subject_ids = frozenset(expected_subject_ids)
    _check(
        len(expected_subject_ids) == EXPECTED_SUBJECT_COUNT,
        "release subject inventory is not exactly 40 unique IDs",
        failures,
    )

    counts = Counter(str(entry.get("domain")) for entry in entries)
    artifact_ids = [str(entry.get("artifact_id") or "") for entry in entries]
    compatibility_ids = [str(entry.get("id") or "") for entry in entries]
    subject_ids = [str(entry.get("subject_id") or "") for entry in entries]
    variant_ids = [str(entry.get("variant_id") or "") for entry in entries]
    subject_variant_pairs = list(zip(subject_ids, variant_ids, strict=True))
    _check(
        len(entries) == EXPECTED_ARTIFACT_COUNT,
        f"series has {len(entries)} entries, expected {EXPECTED_ARTIFACT_COUNT}",
        failures,
    )
    _check(
        counts == Counter({"hikes": EXPECTED_ARTIFACT_COUNT}),
        f"unexpected domain counts: {dict(counts)}",
        failures,
    )
    _check(
        len(artifact_ids) == len(set(artifact_ids)) and all(artifact_ids),
        "series artifact IDs are not 80 unique non-empty values",
        failures,
    )
    _check(
        compatibility_ids == artifact_ids,
        "index compatibility IDs do not match artifact IDs",
        failures,
    )
    _check(
        len(set(subject_ids)) == EXPECTED_SUBJECT_COUNT and all(subject_ids),
        "series does not contain exactly 40 unique non-empty subject IDs",
        failures,
    )
    _check(
        set(subject_ids) == expected_subject_ids,
        "series subject set drift: "
        f"missing={sorted(expected_subject_ids - set(subject_ids))}, "
        f"extra={sorted(set(subject_ids) - expected_subject_ids)}",
        failures,
    )
    _check(
        set(variant_ids) == set(EXPECTED_VARIANTS),
        f"series variants are not exactly {list(EXPECTED_VARIANTS)!r}",
        failures,
    )
    _check(
        Counter(variant_ids)
        == Counter(
            {variant_id: EXPECTED_SUBJECT_COUNT for variant_id in EXPECTED_VARIANTS}
        ),
        f"unexpected variant counts: {dict(Counter(variant_ids))}",
        failures,
    )
    expected_pairs = {
        (subject_id, variant_id)
        for subject_id in expected_subject_ids
        for variant_id in EXPECTED_VARIANTS
    }
    _check(
        set(subject_variant_pairs) == expected_pairs
        and len(subject_variant_pairs) == len(set(subject_variant_pairs)),
        "every release subject must have exactly one artifact of each variant",
        failures,
    )
    expected_artifact_ids = {
        f"{subject_id}--{variant_id}" for subject_id, variant_id in expected_pairs
    }
    _check(
        set(artifact_ids) == expected_artifact_ids,
        "artifact IDs do not exactly encode their subject/variant pairs",
        failures,
    )
    _check(
        all(
            artifact_id == f"{subject_id}--{variant_id}"
            for artifact_id, subject_id, variant_id in zip(
                artifact_ids, subject_ids, variant_ids, strict=True
            )
        ),
        "an artifact ID does not match its indexed subject and variant",
        failures,
    )

    _check(index.get("schema_version") == 2, "index schema is not version 2", failures)
    _check(
        index.get("subject_count") == EXPECTED_SUBJECT_COUNT,
        "index subject_count is not exactly 40",
        failures,
    )
    _check(
        index.get("artifact_count") == EXPECTED_ARTIFACT_COUNT,
        "index artifact_count is not exactly 80",
        failures,
    )
    _check(
        index.get("count") == EXPECTED_ARTIFACT_COUNT,
        "index compatibility count is not exactly 80",
        failures,
    )
    _check(
        index.get("variants") == list(EXPECTED_VARIANTS),
        "index variants are missing, reordered, or unsupported",
        failures,
    )
    _check(
        index.get("counts_by_domain") == {"hikes": EXPECTED_ARTIFACT_COUNT},
        "index domain counts are not exactly 80 hiking artifacts",
        failures,
    )
    _check(
        index.get("counts_by_variant")
        == {variant_id: EXPECTED_SUBJECT_COUNT for variant_id in EXPECTED_VARIANTS},
        "index variant counts are not exactly 40 per paired variant",
        failures,
    )

    declared_svgs: set[Path] = set()
    declared_manifests: set[Path] = set()
    declared_pngs: set[Path] = set()
    for entry in entries:
        artifact_id = str(entry.get("artifact_id") or "")
        outputs = entry.get("outputs") or {}
        primary = {
            "svg": (declared_svgs, f"{artifact_id}.svg"),
            "manifest": (declared_manifests, f"{artifact_id}.plot.json"),
            "png": (declared_pngs, f"{artifact_id}.png"),
        }
        for output_name, (collection, expected_name) in primary.items():
            output_path = Path(str((outputs.get(output_name) or {}).get("path", "")))
            _check(
                output_path.name == expected_name,
                f"{artifact_id}: {output_name} filename drift",
                failures,
            )
            collection.add(output_path.resolve())
        for expected_step, pen_file in enumerate(
            outputs.get("pen_files") or [], start=1
        ):
            pen_id = str(pen_file.get("pen_id", ""))
            path = Path(str(pen_file.get("path", ""))).resolve()
            expected_name = f"{artifact_id}.pen-{expected_step:02d}-{pen_id}.svg"
            _check(
                path.name == expected_name,
                f"{artifact_id}: split-pen filename drift at step {expected_step}",
                failures,
            )
            declared_svgs.add(path)

    _check(
        len(declared_manifests) == EXPECTED_ARTIFACT_COUNT
        and len(declared_pngs) == EXPECTED_ARTIFACT_COUNT,
        "primary artifact paths are not unique for all 80 hiking artifacts",
        failures,
    )
    source_register_path = series_dir / "SOURCES.json"
    source_register: dict[str, Any] = {}
    try:
        loaded_register = json.loads(source_register_path.read_text(encoding="utf-8"))
        if isinstance(loaded_register, dict):
            source_register = loaded_register
    except (OSError, json.JSONDecodeError):
        pass
    _check(
        source_register.get("schema_version") == 1
        and source_register.get("release_status") == "review-only"
        and source_register.get("commercial_clearance_status") == "incomplete"
        and source_register.get("subject_count") == EXPECTED_SUBJECT_COUNT
        and source_register.get("artifact_count") == EXPECTED_ARTIFACT_COUNT
        and source_register.get("provider_attribution_review_required") is True,
        "release source register is missing or does not fail closed",
        failures,
    )
    registered_subjects = source_register.get("subjects")
    registered_artifact_ids: set[str] = set()
    registered_subject_ids: set[str] = set()
    if isinstance(registered_subjects, list):
        for subject in registered_subjects:
            if not isinstance(subject, dict):
                continue
            registered_subject_ids.add(str(subject.get("subject_id") or ""))
            artifacts = subject.get("artifacts")
            if isinstance(artifacts, list):
                registered_artifact_ids.update(
                    str(artifact.get("artifact_id") or "")
                    for artifact in artifacts
                    if isinstance(artifact, dict)
                )
    _check(
        registered_subject_ids == expected_subject_ids
        and registered_artifact_ids == expected_artifact_ids,
        "release source register does not cover the exact paired inventory",
        failures,
    )
    try:
        licence_register = (series_dir / "LICENSES.txt").read_text(encoding="utf-8")
    except OSError:
        licence_register = ""
    _check(
        "NOT COMMERCIALLY CLEARED" in licence_register
        and "MAPZEN" in licence_register.upper(),
        "human-readable licence register is missing the review boundary",
        failures,
    )
    contact_sheets = index.get("contact_sheets") or {}
    hike_contacts = (
        contact_sheets.get("hikes") if isinstance(contact_sheets, dict) else None
    )
    _check(
        isinstance(hike_contacts, dict)
        and set(hike_contacts) == set(EXPECTED_VARIANTS),
        "hiking contact sheets are not indexed once per variant",
        failures,
    )
    if isinstance(hike_contacts, dict):
        for variant_id in EXPECTED_VARIANTS:
            contact_raw = str(hike_contacts.get(variant_id, ""))
            contact_path = Path(contact_raw).resolve() if contact_raw else Path()
            _check(
                bool(contact_raw)
                and contact_path.name == f"hikes-{variant_id}-contact-sheet.png"
                and contact_path.is_file()
                and contact_path.parent == (series_dir / "hikes").resolve(),
                f"{variant_id} hiking contact sheet is missing or misplaced",
                failures,
            )
            if contact_raw:
                declared_pngs.add(contact_path)

    hikes_dir = series_dir / "hikes"
    _check(hikes_dir.is_dir(), "hiking artifact directory is missing", failures)
    if hikes_dir.is_dir():
        actual_svgs = {path.resolve() for path in hikes_dir.glob("*.svg")}
        actual_manifests = {path.resolve() for path in hikes_dir.glob("*.plot.json")}
        actual_pngs = {path.resolve() for path in hikes_dir.glob("*.png")}
        _check(
            actual_svgs == declared_svgs,
            "hiking SVG inventory drift: "
            f"undeclared={sorted(str(path.name) for path in actual_svgs - declared_svgs)}, "
            f"missing={sorted(str(path.name) for path in declared_svgs - actual_svgs)}",
            failures,
        )
        _check(
            actual_manifests == declared_manifests,
            "hiking manifest inventory drift: "
            f"undeclared={sorted(str(path.name) for path in actual_manifests - declared_manifests)}, "
            f"missing={sorted(str(path.name) for path in declared_manifests - actual_manifests)}",
            failures,
        )
        _check(
            actual_pngs == declared_pngs,
            "hiking PNG inventory drift: "
            f"undeclared={sorted(str(path.name) for path in actual_pngs - declared_pngs)}, "
            f"missing={sorted(str(path.name) for path in declared_pngs - actual_pngs)}",
            failures,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("series_dir", type=Path)
    parser.add_argument("--dpi", type=float, default=300.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        parser.error("--dpi must be a positive finite number")
    series_dir = args.series_dir.resolve()
    index_path = series_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = index.get("entries") or []
    results = [
        qa_entry(entry, expected_dpi=args.dpi, series_dir=series_dir)
        for entry in entries
    ]
    counts = Counter(str(entry.get("domain")) for entry in entries)
    variant_counts = Counter(str(entry.get("variant_id")) for entry in entries)
    subject_count = len(
        {str(entry.get("subject_id")) for entry in entries if entry.get("subject_id")}
    )
    suite_failures: list[str] = []
    _check_suite_contract(
        index=index,
        entries=entries,
        series_dir=series_dir,
        failures=suite_failures,
    )
    failed = [result for result in results if not result["passed"]]
    report = {
        "schema_version": 2,
        "series_dir": str(series_dir),
        "passed": not failed and not suite_failures,
        "entry_count": len(entries),
        "subject_count": subject_count,
        "artifact_count": len(entries),
        "variants": list(EXPECTED_VARIANTS),
        "counts_by_domain": dict(counts),
        "counts_by_variant": dict(variant_counts),
        "suite_failures": suite_failures,
        "failed_entry_count": len(failed),
        "results": results,
    }
    report_path = series_dir / "qa-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = [
        "# Hiking series QA",
        "",
        f"Overall: **{'PASS' if report['passed'] else 'FAIL'}**",
        "",
        f"Subjects: {subject_count}; artifacts: {len(entries)}; failed: {len(failed)}",
        "",
        "Coverage is reported as an advisory measurement; it is never an acceptance gate.",
        "",
        "| Plate | Domain | Result | Pens | Coverage (advisory) | Travel |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for result in results:
        markdown.append(
            f"| `{result['id']}` | {result.get('domain')} | "
            f"{'PASS' if result['passed'] else 'FAIL'} | {result.get('pen_steps', '-')} | "
            f"{100 * float(result.get('coverage') or 0):.2f}% | "
            f"{float(result.get('travel_ratio') or 0):.2f} |"
        )
        for failure in result["failures"]:
            markdown.append(f"|  |  | ↳ {failure} |  |  |  |")
    for failure in suite_failures:
        markdown.append(f"\n- Suite failure: {failure}")
    (series_dir / "qa-report.md").write_text(
        "\n".join(markdown) + "\n", encoding="utf-8"
    )
    _rewrite_checksums(series_dir)
    print(
        f"{'PASS' if report['passed'] else 'FAIL'}: {len(entries)} entries, {len(failed)} failed"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
