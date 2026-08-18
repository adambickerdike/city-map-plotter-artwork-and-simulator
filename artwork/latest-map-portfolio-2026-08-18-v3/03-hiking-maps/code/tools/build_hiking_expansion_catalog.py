#!/usr/bin/env python3
"""Build north-up hiking records from frozen Waymarked Trails and OSM evidence.

The recipe file chooses the subjects and official identity references.  This
tool acquires route geometry from one explicit OpenStreetMap relation through
Waymarked Trails, then selects a bounded set of nearby OSM context objects.
It never reads or traces rendered map tiles.  Terrain is added in a separate,
frozen DEM pass by ``derive_hiking_global_terrain.py``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, NoReturn, Sequence

from shapely.affinity import affine_transform
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points, unary_union

from hiking_map_extent import (
    aspect_expanded_map_extent,
    bind_aspect_expanded_map_extent,
    route_rect_aspect,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = (
    ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-expansion-v1.json"
)
CATALOG_ID = "hike-plates-expansion-v1"
PEN_PLAN_ID = "HIKE-A5-V2"
PENS = (
    "grey-0-25",
    "grey-0-4",
    "blue-0-25",
    "green-0-25",
    "black-0-25",
    "black-0-6",
    "red-0-4",
)
WAYMARKED_DETAILS = (
    "https://hiking.waymarkedtrails.org/api/v1/details/relation/{relation_id}?lang=en"
)
OSM_RELATION = "https://api.openstreetmap.org/api/0.6/relation/{relation_id}.json"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
OVERPASS_PROVENANCE_URL = "https://wiki.openstreetmap.org/wiki/Overpass_API"
CONTEXT_QUERY_VERSION = "hiking-overpass-context-v2"
CONTEXT_SELECTION_VERSION = "hiking-context-distributed-geometry-v1"
OVERPASS_GEOMETRY_BATCH_SIZE = 15
USER_AGENT = "city-map-plotter/0.2 hiking-artwork-research"
EARTH_RADIUS_M = 6_378_137.0
CONTEXT_FAMILY_KINDS = {
    "roads": frozenset({"road"}),
    "hydrography": frozenset({"river", "water", "coast", "sea"}),
    "landcover": frozenset({"woodland", "grass"}),
}
CONTEXT_FAMILY_QUERY_GROUPS = {
    "roads": ("linear",),
    "hydrography": ("labels", "linear", "areas"),
    "landcover": ("areas",),
}
CONTEXT_AREA_KINDS = frozenset({"water", "woodland", "grass"})
CONTEXT_LINEAR_KINDS = frozenset({"road", "river", "coast"})
# Index responses contain tags and centres but no usable source geometry.  A
# final A5 cap therefore cannot safely be spent at the index stage: a nearby
# centre can belong to a minute fragment while a coherent polygon or long line
# sits slightly farther away.  The bounded acquisition caps below provide a
# geometry choice without turning the artwork into an unbounded data scrape.
CONTEXT_MAX_ACQUISITION_PER_KIND = 48
CONTEXT_MAX_GEOMETRY_ACQUISITION = 96
CONTEXT_AREA_ACQUISITION_FACTOR = 3
CONTEXT_LINEAR_ACQUISITION_FACTOR = 2
# Fractions are measured after clipping source geometry to the north-up map
# extent and normalising that extent to a unit page.  They are curation gates,
# not synthetic geometry: sub-threshold source objects remain eligible only
# after every page-legible candidate in their route band.
CONTEXT_LEGIBLE_AREA_FRACTION = 0.0007
CONTEXT_LEGIBLE_LINE_FRACTION = 0.012


@dataclass(frozen=True)
class _RouteAxis:
    lines: tuple[LineString, ...]
    offsets_m: tuple[float, ...]
    length_m: float


@dataclass(frozen=True)
class _IndexCandidate:
    kind: str
    priority: int
    distance_m: float
    route_axis: float
    identifier: int
    element: dict[str, Any]


@dataclass(frozen=True)
class _FeatureCandidate:
    kind: str
    priority: int
    distance_m: float
    route_axis: float
    page_metric: float
    page_legible: bool
    feature: dict[str, Any]


@dataclass(frozen=True)
class RouteGeometry:
    segments: tuple[tuple[tuple[float, float], ...], ...]
    source_length_m: float
    bbox: tuple[float, float, float, float]


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"build_hiking_expansion_catalog: {message}")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"could not read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{path} must contain an object")
    return value


def _cache_path(cache_dir: Path, namespace: str, identifier: str) -> Path:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in identifier
    )
    return cache_dir / namespace / f"{safe}.json"


def _query_cache_id(identifier: str, query: str) -> str:
    """Bind a cached response to the exact canonical query text."""

    return f"{identifier}-{CONTEXT_QUERY_VERSION}-q-{_canonical_sha256(query)}"


def _fetch_json(
    url: str,
    *,
    cache_path: Path,
    method: str = "GET",
    body: bytes | None = None,
    timeout_s: float = 180.0,
) -> tuple[dict[str, Any], str]:
    if cache_path.is_file():
        data = cache_path.read_bytes()
        value = json.loads(data)
        if not isinstance(value, dict):
            _fail(f"cached response {cache_path} is not an object")
        return value, _sha256_bytes(data)
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            data = response.read()
    except (OSError, urllib.error.URLError) as exc:
        _fail(f"request failed for {url}: {exc}")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        _fail(f"response from {url} was not JSON: {exc}")
    if not isinstance(value, dict):
        _fail(f"response from {url} is not an object")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return value, _sha256_bytes(data)


def _mercator_to_lonlat(point: Sequence[float]) -> tuple[float, float]:
    longitude = math.degrees(float(point[0]) / EARTH_RADIUS_M)
    latitude = math.degrees(
        2.0 * math.atan(math.exp(float(point[1]) / EARTH_RADIUS_M)) - math.pi / 2.0
    )
    return longitude, latitude


def _lonlat_to_mercator(point: Sequence[float]) -> tuple[float, float]:
    longitude = math.radians(float(point[0])) * EARTH_RADIUS_M
    latitude = max(min(float(point[1]), 85.05112878), -85.05112878)
    northing = EARTH_RADIUS_M * math.log(
        math.tan(math.pi / 4.0 + math.radians(latitude) / 2.0)
    )
    return longitude, northing


def _route_base_geometries(
    node: Any,
    *,
    selected_relation_ids: set[int] | None = None,
    excluded_relation_ids: set[int] | None = None,
    selection_satisfied: bool = False,
) -> Iterable[list[tuple[float, float]]]:
    if not isinstance(node, dict):
        return
    relation_id = node.get("id")
    if isinstance(relation_id, int):
        if excluded_relation_ids and relation_id in excluded_relation_ids:
            return
        if (
            selected_relation_ids is not None
            and not selection_satisfied
            and node.get("route_type") == "route"
            and relation_id not in selected_relation_ids
        ):
            return
        if selected_relation_ids is not None and relation_id in selected_relation_ids:
            selection_satisfied = True
    geometry = node.get("geometry")
    if node.get("route_type") == "base" and isinstance(geometry, dict):
        coordinates = geometry.get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) >= 2:
            points = [
                (float(point[0]), float(point[1]))
                for point in coordinates
                if isinstance(point, list) and len(point) >= 2
            ]
            if len(points) >= 2:
                if int(node.get("direction", 0)) < 0:
                    points.reverse()
                yield points
    # Waymarked ``appendices`` commonly contain access spurs, alternatives and
    # transfers.  They are useful on an interactive route browser but create
    # misleading branches and excess pen travel on a single hero-line plate.
    # The audited expansion recipes all select the continuous main itinerary.
    child_keys = ["main", "ways"]
    if node.get("route_type") == "split":
        # ``forward`` and ``backward`` describe the same directed split in
        # opposite travel directions.  Rendering both doubles the path.
        child_keys.append("forward")
    for key in child_keys:
        children = node.get(key, [])
        if isinstance(children, list):
            for child in children:
                yield from _route_base_geometries(
                    child,
                    selected_relation_ids=selected_relation_ids,
                    excluded_relation_ids=excluded_relation_ids,
                    selection_satisfied=selection_satisfied,
                )


def _stitch_segments(
    parts: Iterable[Sequence[tuple[float, float]]],
    *,
    maximum_gap_m: float = 1.0,
) -> list[list[tuple[float, float]]]:
    """Assemble touching pieces and order gaps without drawing across them.

    Waymarked usually emits ways in travel order, so the first pass handles
    the common case.  Ferry, split, and interleaved relation members can later
    reconnect to an earlier open chain, though.  A deterministic second pass
    merges all numerically identical endpoints.  Remaining chains are ordered
    from the original source start by nearest endpoint but remain independent
    LineStrings, so ordering never invents a connector.
    """

    ranked: list[tuple[int, list[tuple[float, float]]]] = []
    source_start: tuple[float, float] | None = None
    join_tolerance_m = min(maximum_gap_m, 1e-6)
    for source_index, raw in enumerate(parts):
        points = list(raw)
        if len(points) < 2:
            continue
        if source_start is None:
            source_start = points[0]
        if not ranked:
            ranked.append((source_index, points))
            continue
        _, current = ranked[-1]
        distances = (
            math.dist(current[-1], points[0]),
            math.dist(current[-1], points[-1]),
        )
        # Only remove a numerically duplicated endpoint.  Even a short visible
        # gap is source evidence and must remain a separate SVG subpath; adding
        # a straight line would invent route geometry.
        if min(distances) <= join_tolerance_m:
            if distances[1] < distances[0]:
                points.reverse()
            current.extend(points[1:])
        else:
            ranked.append((source_index, points))

    if source_start is None:
        return []

    def join(
        left: list[tuple[float, float]],
        right: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        return [*left, *right[1:]]

    # The first pass only compares the current tail.  Merge a later return to
    # any earlier chain as well; after the first pass this list is small even
    # for continent-scale relations.
    while True:
        best: tuple[float, int, int, int, int, int] | None = None
        for left_index, (left_rank, left) in enumerate(ranked):
            for right_index in range(left_index + 1, len(ranked)):
                right_rank, right = ranked[right_index]
                distances = (
                    math.dist(left[-1], right[0]),
                    math.dist(left[-1], right[-1]),
                    math.dist(left[0], right[-1]),
                    math.dist(left[0], right[0]),
                )
                for orientation, distance in enumerate(distances):
                    if distance > join_tolerance_m:
                        continue
                    candidate = (
                        distance,
                        min(left_rank, right_rank),
                        max(left_rank, right_rank),
                        left_index,
                        right_index,
                        orientation,
                    )
                    if best is None or candidate < best:
                        best = candidate
        if best is None:
            break
        _, _, _, left_index, right_index, orientation = best
        left_rank, left = ranked[left_index]
        right_rank, right = ranked[right_index]
        if orientation == 0:  # left end -> right start
            merged = join(left, right)
        elif orientation == 1:  # left end -> right end
            merged = join(left, list(reversed(right)))
        elif orientation == 2:  # right end -> left start
            merged = join(right, left)
        else:  # left start -> right start
            merged = join(list(reversed(left)), right)
        ranked[left_index] = (min(left_rank, right_rank), merged)
        ranked.pop(right_index)

    # Stable nearest-endpoint ordering fixes interleaved source members (for
    # example the West Coast Trail) while retaining every real gap as a move
    # between separate output segments.
    remaining = list(ranked)
    _, _, first_index, first_reverse = min(
        (
            math.dist(source_start, points[endpoint]),
            source_rank,
            index,
            endpoint == -1,
        )
        for index, (source_rank, points) in enumerate(remaining)
        for endpoint in (0, -1)
    )
    _, first = remaining.pop(first_index)
    if first_reverse:
        first.reverse()
    ordered = [first]
    while remaining:
        current_end = ordered[-1][-1]
        _, _, next_index, next_reverse = min(
            (
                math.dist(current_end, points[endpoint]),
                source_rank,
                index,
                endpoint == -1,
            )
            for index, (source_rank, points) in enumerate(remaining)
            for endpoint in (0, -1)
        )
        _, following = remaining.pop(next_index)
        if next_reverse:
            following.reverse()
        ordered.append(following)
    return ordered


def _nested_relation_ids(node: Any) -> set[int]:
    if not isinstance(node, dict):
        return set()
    identifiers = {int(node["id"])} if isinstance(node.get("id"), int) else set()
    for key in ("main", "appendices", "ways", "forward", "backward"):
        children = node.get(key, [])
        if isinstance(children, list):
            for child in children:
                identifiers.update(_nested_relation_ids(child))
    return identifiers


def _route_geometry(
    details: dict[str, Any],
    *,
    tolerance_m: float,
    recipe: dict[str, Any] | None = None,
) -> RouteGeometry:
    route = details.get("route")
    if not isinstance(route, dict):
        _fail("Waymarked Trails detail has no route object")
    geometry_recipe = (
        recipe.get("geometry_recipe", {}) if isinstance(recipe, dict) else {}
    )
    geometry_recipe = geometry_recipe if isinstance(geometry_recipe, dict) else {}
    selected_raw = geometry_recipe.get(
        "draw_selected_member_relations_recursively_main_only"
    )
    selected = (
        {int(identifier) for identifier in selected_raw}
        if isinstance(selected_raw, list)
        else None
    )
    excluded_raw = geometry_recipe.get("draw_main_stage_relations_except")
    excluded = (
        {int(identifier) for identifier in excluded_raw}
        if isinstance(excluded_raw, list)
        else set()
    )
    available_relation_ids = _nested_relation_ids(route)
    if selected is not None and not selected.issubset(available_relation_ids):
        _fail(
            "geometry recipe selects absent relation(s): "
            + ", ".join(map(str, sorted(selected - available_relation_ids)))
        )
    if not excluded.issubset(available_relation_ids):
        _fail(
            "geometry recipe excludes absent relation(s): "
            + ", ".join(map(str, sorted(excluded - available_relation_ids)))
        )
    segments = _stitch_segments(
        _route_base_geometries(
            route,
            selected_relation_ids=selected,
            excluded_relation_ids=excluded,
        )
    )
    if not segments:
        _fail("Waymarked Trails detail yielded no route geometry")
    reverse_output = geometry_recipe.get("reverse_output", False)
    if not isinstance(reverse_output, bool):
        _fail("geometry_recipe.reverse_output must be a boolean")
    if reverse_output:
        segments = [list(reversed(points)) for points in reversed(segments)]
    simplified: list[tuple[tuple[float, float], ...]] = []
    for points in segments:
        geometry = LineString(points).simplify(tolerance_m, preserve_topology=False)
        if geometry.geom_type != "LineString" or len(geometry.coords) < 2:
            continue
        geographic = tuple(
            (round(longitude, 6), round(latitude, 6))
            for longitude, latitude in (
                _mercator_to_lonlat(point) for point in geometry.coords
            )
        )
        if len(geographic) >= 2:
            simplified.append(geographic)
    if not simplified:
        _fail("route simplification removed every segment")
    raw_bbox = details.get("bbox")
    # A curated member selection deliberately omits route alternatives or
    # appendices.  The upstream relation bbox still encloses those branches,
    # which would leave the selected walk floating in excess whitespace.  In
    # that case the extent must follow the geometry we actually publish.
    use_selected_bbox = selected is not None or bool(excluded)
    if use_selected_bbox or not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        all_points = [point for segment in segments for point in segment]
        minimum_x = min(point[0] for point in all_points)
        minimum_y = min(point[1] for point in all_points)
        maximum_x = max(point[0] for point in all_points)
        maximum_y = max(point[1] for point in all_points)
    else:
        minimum_x, minimum_y, maximum_x, maximum_y = map(float, raw_bbox)
    west, south = _mercator_to_lonlat((minimum_x, minimum_y))
    east, north = _mercator_to_lonlat((maximum_x, maximum_y))
    selected_length = geometry_recipe.get(
        "selected_length_m",
        geometry_recipe.get("selected_walking_length_m"),
    )
    source_length = (
        float(selected_length)
        if isinstance(selected_length, (int, float))
        and not isinstance(selected_length, bool)
        and float(selected_length) > 0.0
        else float(route.get("length", 0.0))
    )
    return RouteGeometry(
        segments=tuple(simplified),
        source_length_m=source_length,
        bbox=(west, south, east, north),
    )


def _padded_extent(
    bbox: Sequence[float], *, route_length_m: float
) -> tuple[float, float, float, float]:
    west, south, east, north = map(float, bbox)
    longitude_span = max(east - west, 0.01)
    latitude_span = max(north - south, 0.01)
    fraction = 0.08 if route_length_m < 400_000 else 0.045
    longitude_padding = max(longitude_span * fraction, 0.015)
    latitude_padding = max(latitude_span * fraction, 0.015)
    return (
        max(-180.0, west - longitude_padding),
        max(-85.0, south - latitude_padding),
        min(180.0, east + longitude_padding),
        min(85.0, north + latitude_padding),
    )


def _choose_format(extent: Sequence[float]) -> str:
    west, south, east, north = map(float, extent)
    mean_latitude = (south + north) / 2.0
    width = (east - west) * max(math.cos(math.radians(mean_latitude)), 0.2)
    height = north - south
    return "a5-landscape" if width / max(height, 1e-9) > 1.12 else "a5-portrait"


def _route_line(route: RouteGeometry) -> Any:
    lines = [
        LineString([_lonlat_to_mercator(point) for point in segment])
        for segment in route.segments
    ]
    return unary_union(lines)


def _route_axis(route: RouteGeometry) -> _RouteAxis:
    lines = tuple(
        LineString([_lonlat_to_mercator(point) for point in segment])
        for segment in route.segments
        if len(segment) >= 2
    )
    offsets: list[float] = []
    length_m = 0.0
    for line in lines:
        offsets.append(length_m)
        length_m += float(line.length)
    return _RouteAxis(lines=lines, offsets_m=tuple(offsets), length_m=length_m)


def _axis_measure(axis: _RouteAxis, geometry: Any) -> tuple[float, float]:
    """Return ordered route progress and distance for factual geometry."""

    if not axis.lines or axis.length_m <= 1e-9 or geometry.is_empty:
        return 0.5, math.inf
    best_distance = math.inf
    best_progress = 0.5
    for line, offset_m in zip(axis.lines, axis.offsets_m, strict=True):
        distance_m = float(line.distance(geometry))
        if distance_m > best_distance + 1e-9:
            continue
        route_point = nearest_points(line, geometry)[0]
        progress = (offset_m + float(line.project(route_point))) / axis.length_m
        if distance_m < best_distance - 1e-9 or progress < best_progress:
            best_distance = distance_m
            best_progress = progress
    return min(1.0, max(0.0, best_progress)), best_distance


def _acquisition_cap(kind: str, final_cap: int) -> int:
    if final_cap <= 0:
        return 0
    if kind in CONTEXT_AREA_KINDS:
        requested = final_cap * CONTEXT_AREA_ACQUISITION_FACTOR
    elif kind in CONTEXT_LINEAR_KINDS:
        requested = final_cap * CONTEXT_LINEAR_ACQUISITION_FACTOR
    else:
        requested = final_cap
    return min(CONTEXT_MAX_ACQUISITION_PER_KIND, requested)


def _route_band(route_axis: float, band_count: int) -> int:
    return min(int(min(1.0, max(0.0, route_axis)) * band_count), band_count - 1)


def _distributed_select(
    candidates: Sequence[Any],
    *,
    maximum: int,
    band_count: int,
    score: Callable[[Any], tuple[Any, ...]],
) -> list[Any]:
    """Select one best source object per route band before quality fill."""

    if maximum <= 0 or band_count <= 0:
        return []
    ordered = sorted(candidates, key=score)
    selected: list[Any] = []
    selected_ids: set[int] = set()
    for band in range(band_count):
        options = [
            (index, candidate)
            for index, candidate in enumerate(ordered)
            if index not in selected_ids
            and _route_band(float(candidate.route_axis), band_count) == band
        ]
        if not options:
            continue
        index, candidate = min(options, key=lambda item: score(item[1]))
        selected.append(candidate)
        selected_ids.add(index)
        if len(selected) >= maximum:
            return selected
    for index, candidate in enumerate(ordered):
        if len(selected) >= maximum:
            break
        if index not in selected_ids:
            selected.append(candidate)
    return selected


def _index_candidate_score(candidate: _IndexCandidate) -> tuple[Any, ...]:
    return (
        candidate.priority,
        candidate.distance_m,
        candidate.identifier,
    )


def _normalised_label(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _select_index_kind_candidates(
    kind: str,
    candidates: Sequence[_IndexCandidate],
    *,
    final_cap: int,
    acquisition_cap: int,
    required_labels: Sequence[str] = (),
) -> list[_IndexCandidate]:
    if acquisition_cap <= 0:
        if required_labels:
            _fail(f"required {kind} labels cannot fit a zero context cap")
        return []
    required: list[_IndexCandidate] = []
    required_keys: set[tuple[str, int]] = set()
    for label in required_labels:
        label_key = _normalised_label(label)
        matches = [
            candidate
            for candidate in candidates
            if _normalised_label(candidate.element.get("tags", {}).get("name"))
            == label_key
        ]
        if not matches:
            _fail(f"required {kind} context label {label!r} is absent from OSM index")
        candidate = min(matches, key=_index_candidate_score)
        key = (str(candidate.element.get("type", "")), candidate.identifier)
        if key not in required_keys:
            required.append(candidate)
            required_keys.add(key)
    if len(required) > acquisition_cap:
        _fail(
            f"{len(required)} required {kind} labels exceed acquisition cap "
            f"{acquisition_cap}"
        )
    remaining_candidates = [
        candidate
        for candidate in candidates
        if (str(candidate.element.get("type", "")), candidate.identifier)
        not in required_keys
    ]
    if kind != "road":
        return [
            *required,
            *_distributed_select(
                remaining_candidates,
                maximum=acquisition_cap - len(required),
                band_count=max(1, min(final_cap, 10)),
                score=_index_candidate_score,
            ),
        ]

    # A nearest/class-first road index can otherwise spend every acquisition
    # slot on tiny motorway/primary fragments.  Reserve a small, bounded share
    # for every factual OSM road tier before the geometry-aware final ranking.
    # This does not promote those tiers to the plate; it merely lets their true
    # clipped length compete once geometry exists.
    class_quota = max(1, min(2, acquisition_cap // 8))
    selected: list[_IndexCandidate] = list(required)
    selected_ids: set[int] = {candidate.identifier for candidate in required}
    for priority in sorted({candidate.priority for candidate in remaining_candidates}):
        class_candidates = [
            candidate
            for candidate in remaining_candidates
            if candidate.priority == priority
        ]
        for candidate in _distributed_select(
            class_candidates,
            maximum=class_quota,
            band_count=max(1, min(class_quota, final_cap)),
            score=_index_candidate_score,
        ):
            if candidate.identifier in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(candidate.identifier)
            if len(selected) >= acquisition_cap:
                return selected
    for candidate in _distributed_select(
        remaining_candidates,
        maximum=acquisition_cap,
        band_count=max(1, min(final_cap, 10)),
        score=_index_candidate_score,
    ):
        if candidate.identifier in selected_ids:
            continue
        selected.append(candidate)
        selected_ids.add(candidate.identifier)
        if len(selected) >= acquisition_cap:
            break
    return selected


def _normalised_visible_geometry(geometry: Any, extent: Sequence[float]) -> Any:
    west, south, east, north = map(float, extent)
    minimum_x, minimum_y = _lonlat_to_mercator((west, south))
    maximum_x, maximum_y = _lonlat_to_mercator((east, north))
    width = maximum_x - minimum_x
    height = maximum_y - minimum_y
    if width <= 1e-9 or height <= 1e-9 or geometry.is_empty:
        return LineString()
    try:
        visible = geometry.intersection(box(minimum_x, minimum_y, maximum_x, maximum_y))
        if visible.is_empty:
            return visible
        return affine_transform(
            visible,
            [
                1.0 / width,
                0.0,
                0.0,
                1.0 / height,
                -minimum_x / width,
                -minimum_y / height,
            ],
        )
    except GEOSException:
        return LineString()


def _feature_candidate_score(candidate: _FeatureCandidate) -> tuple[Any, ...]:
    if candidate.kind in CONTEXT_AREA_KINDS | CONTEXT_LINEAR_KINDS:
        return (
            0 if candidate.page_legible else 1,
            candidate.priority,
            -candidate.page_metric,
            candidate.distance_m,
            str(candidate.feature["id"]),
        )
    return (
        candidate.priority,
        candidate.distance_m,
        str(candidate.feature["id"]),
    )


def _corridor_samples(
    route: RouteGeometry, *, count: int = 5
) -> list[tuple[float, float]]:
    geometry = _route_line(route)
    if geometry.is_empty:
        return []
    if geometry.geom_type == "MultiLineString":
        line = max(geometry.geoms, key=lambda item: item.length)
    else:
        line = geometry
    sample_count = max(2, min(count, int(line.length / 75_000.0) + 2))
    values: list[tuple[float, float]] = []
    for index in range(sample_count):
        point = line.interpolate(index / (sample_count - 1), normalized=True)
        values.append(_mercator_to_lonlat((point.x, point.y)))
    return values


def _overpass_query_groups(route: RouteGeometry) -> dict[str, str]:
    label_radius_m = (
        18_000
        if route.source_length_m >= 1_000_000
        else 14_000
        if route.source_length_m >= 300_000
        else 10_000
        if route.source_length_m >= 100_000
        else 7_000
    )
    road_radius_m = min(label_radius_m, 3_500)
    local_radius_m = min(label_radius_m, 1_500)
    water_radius_m = min(label_radius_m, 3_500)
    green_radius_m = min(label_radius_m, 1_800)
    clauses: dict[str, list[str]] = {"labels": [], "linear": [], "areas": []}
    for longitude, latitude in _corridor_samples(route):
        labels = f"around:{label_radius_m},{latitude:.6f},{longitude:.6f}"
        roads = f"around:{road_radius_m},{latitude:.6f},{longitude:.6f}"
        local = f"around:{local_radius_m},{latitude:.6f},{longitude:.6f}"
        water = f"around:{water_radius_m},{latitude:.6f},{longitude:.6f}"
        green = f"around:{green_radius_m},{latitude:.6f},{longitude:.6f}"
        clauses["labels"].extend(
            [
                f'node({labels})["place"~"city|town|village"]["name"];',
                f'node({labels})["place"~"sea|ocean"]["name"];',
                f'node({labels})["natural"~"peak|volcano|saddle"]["name"];',
                f'node({labels})["natural"~"mountain_range|bay"]["name"];',
                f'node({labels})["mountain_pass"="yes"]["name"];',
            ]
        )
        clauses["linear"].extend(
            [
                f'way({roads})["highway"~"motorway|trunk|primary|secondary"];',
                f'way({local})["highway"~"tertiary|unclassified|track"];',
                f'way({water})["waterway"~"river|stream"];',
                f'way({water})["natural"="coastline"];',
            ]
        )
        clauses["areas"].extend(
            [
                f'way({water})["natural"="water"];',
                f'way({green})["natural"~"wood|grassland|heath|scrub"];',
                f'way({green})["landuse"~"forest|grass|meadow"];',
            ]
        )
    return {
        group: "[out:json][timeout:120];("
        + "".join(group_clauses)
        + ");out tags center;"
        for group, group_clauses in clauses.items()
    }


def _distributed_landcover_query(route: RouteGeometry) -> str:
    """Acquire green-area centres at finer route-axis intervals when needed."""

    label_radius_m = (
        18_000
        if route.source_length_m >= 1_000_000
        else 14_000
        if route.source_length_m >= 300_000
        else 10_000
        if route.source_length_m >= 100_000
        else 7_000
    )
    green_radius_m = min(label_radius_m, 1_800)
    clauses: list[str] = []
    for longitude, latitude in _corridor_samples(route, count=9):
        green = f"around:{green_radius_m},{latitude:.6f},{longitude:.6f}"
        clauses.extend(
            [
                f'way({green})["natural"~"wood|grassland|heath|scrub"];',
                f'way({green})["landuse"~"forest|grass|meadow"];',
            ]
        )
    return "[out:json][timeout:120];(" + "".join(clauses) + ");out tags center;"


def _landcover_route_band_count(
    elements: Sequence[dict[str, Any]],
    *,
    route: RouteGeometry,
    band_count: int = 6,
    kinds: frozenset[str] = CONTEXT_AREA_KINDS - {"water"},
) -> int:
    axis = _route_axis(route)
    occupied: set[int] = set()
    for element in elements:
        classification = _element_kind_priority(element)
        point = _element_point(element)
        if classification is None or classification[0] not in kinds or point is None:
            continue
        route_axis, _ = _axis_measure(axis, Point(_lonlat_to_mercator(point)))
        occupied.add(_route_band(route_axis, band_count))
    return len(occupied)


def _needs_distributed_landcover_query(
    elements: Sequence[dict[str, Any]],
    *,
    route: RouteGeometry,
    caps: dict[str, int],
) -> bool:
    for kind in ("woodland", "grass"):
        cap = int(caps.get(kind, 0))
        if cap <= 0:
            continue
        candidate_count = sum(
            (_element_kind_priority(element) or (None, 0))[0] == kind
            for element in elements
        )
        desired_bands = min(3, cap, max(1, candidate_count))
        if (
            _landcover_route_band_count(
                elements,
                route=route,
                kinds=frozenset({kind}),
            )
            < desired_bands
        ):
            return True
    return False


def _overpass_query(route: RouteGeometry) -> str:
    """Canonical disclosure payload retained for manifest compatibility."""

    return json.dumps(
        _overpass_query_groups(route),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _element_kind_priority(element: dict[str, Any]) -> tuple[str, int] | None:
    tags = element.get("tags", {})
    if not isinstance(tags, dict):
        return None
    if element.get("type") == "node":
        place = str(tags.get("place", ""))
        if place in {"city", "town", "village"} and tags.get("name"):
            return "settlement", {"city": 0, "town": 1, "village": 2}[place]
        if place in {"sea", "ocean"} and tags.get("name"):
            return "sea", 0
        if tags.get("natural") == "mountain_range" and tags.get("name"):
            return "range", 0
        if tags.get("natural") == "bay" and tags.get("name"):
            return "sea", 1
        if tags.get("natural") in {"peak", "volcano"} and tags.get("name"):
            return "peak", 0
        if (
            tags.get("natural") == "saddle" or tags.get("mountain_pass") == "yes"
        ) and tags.get("name"):
            return "pass", 1
        return None
    if element.get("type") != "way":
        return None
    highway = str(tags.get("highway", ""))
    waterway = str(tags.get("waterway", ""))
    natural = str(tags.get("natural", ""))
    landuse = str(tags.get("landuse", ""))
    if highway:
        road_class = (
            "major"
            if highway in {"motorway", "trunk", "primary"}
            else "secondary"
            if highway in {"secondary", "tertiary"}
            else "track"
            if highway == "track"
            else "local"
        )
        return "road", {"major": 0, "secondary": 1, "local": 2, "track": 3}[road_class]
    if waterway in {"river", "stream"}:
        return "river", 0 if waterway == "river" else 2
    if natural == "coastline":
        return "coast", 0
    if natural == "water":
        return "water", 0
    if natural in {"wood", "heath", "scrub"} or landuse == "forest":
        return "woodland", 0 if natural == "wood" or landuse == "forest" else 1
    if natural == "grassland" or landuse in {"grass", "meadow"}:
        return "grass", 0
    return None


def _element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    if element.get("type") == "node":
        raw = element
    else:
        raw = element.get("center")
    if not isinstance(raw, dict):
        return None
    try:
        return float(raw["lon"]), float(raw["lat"])
    except (KeyError, TypeError, ValueError):
        return None


def _select_index_elements(
    payload: dict[str, Any],
    *,
    route: RouteGeometry,
    caps: dict[str, int],
    required_labels: dict[str, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    axis = _route_axis(route)
    candidates: dict[str, list[_IndexCandidate]] = {}
    elements = payload.get("elements", [])
    if not isinstance(elements, list):
        _fail("Overpass index elements must be an array")
    seen: set[tuple[str, int]] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        identifier = element.get("id")
        object_type = str(element.get("type", ""))
        if not isinstance(identifier, int) or object_type not in {"node", "way"}:
            continue
        key = (object_type, identifier)
        if key in seen:
            continue
        seen.add(key)
        classification = _element_kind_priority(element)
        point = _element_point(element)
        if classification is None or point is None:
            continue
        kind, priority = classification
        route_axis, distance = _axis_measure(
            axis,
            Point(_lonlat_to_mercator(point)),
        )
        candidates.setdefault(kind, []).append(
            _IndexCandidate(
                kind=kind,
                priority=priority,
                distance_m=distance,
                route_axis=route_axis,
                identifier=identifier,
                element=element,
            )
        )
    for kind, labels in (required_labels or {}).items():
        if labels and not candidates.get(kind):
            _fail(
                f"required {kind} context label(s) are absent from OSM index: "
                + ", ".join(repr(label) for label in labels)
            )
    selected_by_kind: dict[str, list[_IndexCandidate]] = {}
    for kind in sorted(candidates):
        values = candidates[kind]
        final_cap = int(caps.get(kind, 0))
        acquisition_cap = _acquisition_cap(kind, final_cap)
        selected_by_kind[kind] = _select_index_kind_candidates(
            kind,
            values,
            final_cap=final_cap,
            acquisition_cap=acquisition_cap,
            required_labels=(required_labels or {}).get(kind, ()),
        )

    # Preserve every label candidate selected at its final cap.  Geometry has
    # a separate per-record ceiling so a high-cap coastal route cannot turn
    # one A5 plate into hundreds of public-Overpass geometry requests.  Each
    # way kind first receives its complete final cap; remaining choice slots
    # are then shared round-robin across semantic kinds.
    selected: list[_IndexCandidate] = []
    way_kinds = sorted(CONTEXT_AREA_KINDS | CONTEXT_LINEAR_KINDS)
    extras_by_kind: dict[str, list[_IndexCandidate]] = {}
    for kind in sorted(selected_by_kind):
        values = selected_by_kind[kind]
        if kind not in CONTEXT_AREA_KINDS | CONTEXT_LINEAR_KINDS:
            selected.extend(values)
            continue
        base_count = min(int(caps.get(kind, 0)), len(values))
        selected.extend(values[:base_count])
        extras_by_kind[kind] = values[base_count:]
    geometry_count = sum(
        candidate.kind in CONTEXT_AREA_KINDS | CONTEXT_LINEAR_KINDS
        for candidate in selected
    )
    remaining = max(0, CONTEXT_MAX_GEOMETRY_ACQUISITION - geometry_count)
    extra_index = 0
    while remaining:
        added = False
        for kind in way_kinds:
            extras = extras_by_kind.get(kind, [])
            if extra_index >= len(extras):
                continue
            selected.append(extras[extra_index])
            remaining -= 1
            added = True
            if remaining == 0:
                break
        if not added:
            break
        extra_index += 1
    return [candidate.element for candidate in selected]


def _geometry_query(way_ids: Sequence[int]) -> str:
    if not way_ids:
        return "[out:json];out;"
    identifiers = ",".join(str(identifier) for identifier in sorted(set(way_ids)))
    return f"[out:json][timeout:240];way(id:{identifiers});out tags center geom;"


def _fetch_overpass(
    query: str,
    *,
    cache_path: Path,
) -> tuple[dict[str, Any], str, str]:
    if cache_path.is_file():
        payload = cache_path.read_bytes()
        value = json.loads(payload)
        if not isinstance(value, dict):
            _fail(f"cached Overpass response {cache_path} is not an object")
        return value, _sha256_bytes(payload), OVERPASS_PROVENANCE_URL
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    failures: list[str] = []
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            value, digest = _fetch_json(
                endpoint,
                cache_path=cache_path,
                method="POST",
                body=body,
                # Public instances occasionally accept a connection but never
                # start a response.  Bound each attempt so the next published
                # mirror is tried instead of hanging one route for five
                # minutes; the query itself already declares a 120 s budget.
                timeout_s=120.0,
            )
            return value, digest, OVERPASS_PROVENANCE_URL
        except SystemExit as exc:
            failures.append(str(exc))
            time.sleep(2.0)
    _fail("; ".join(failures))


def _fetch_geometry_elements(
    way_ids: Sequence[int],
    *,
    cache_dir: Path,
    context_cache_id: str,
    fetcher: Callable[..., tuple[dict[str, Any], str, str]] | None = None,
) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """Fetch selected way geometry without first issuing one large query.

    An exact-query combined cache remains authoritative so already completed
    routes reproduce byte-for-byte source ordering.  When that cache is absent,
    sorted unique way IDs are acquired in independently cached small batches.
    """

    active_fetcher = fetcher or _fetch_overpass
    identifiers = sorted(set(way_ids))
    combined_query = _geometry_query(identifiers)
    combined_cache_path = _cache_path(
        cache_dir,
        "overpass-geometry",
        _query_cache_id(context_cache_id, combined_query),
    )
    if combined_cache_path.is_file():
        payload, snapshot_sha256, endpoint = active_fetcher(
            combined_query,
            cache_path=combined_cache_path,
        )
        elements = payload.get("elements", [])
        if not isinstance(elements, list):
            _fail(f"{context_cache_id} cached Overpass geometry response is invalid")
        return (
            elements,
            [_query_evidence("geometry", combined_query, snapshot_sha256, payload)],
            [endpoint],
        )

    # Nothing needs to be sent to Overpass when every context cap selected
    # zero ways.  Keep the same query-evidence shape as the former combined
    # path without manufacturing a remote endpoint or cache snapshot.
    if not identifiers:
        payload: dict[str, Any] = {"elements": []}
        return (
            [],
            [
                _query_evidence(
                    "geometry",
                    combined_query,
                    _canonical_sha256(payload),
                    payload,
                )
            ],
            [],
        )

    batches = [
        identifiers[index : index + OVERPASS_GEOMETRY_BATCH_SIZE]
        for index in range(0, len(identifiers), OVERPASS_GEOMETRY_BATCH_SIZE)
    ]
    fetched_batches: list[tuple[list[int], str, dict[str, Any], str, str]] = []

    def fetch_batch(
        batch: list[int],
    ) -> list[tuple[list[int], str, dict[str, Any], str, str]]:
        query = _geometry_query(batch)
        try:
            payload, snapshot_sha256, endpoint = active_fetcher(
                query,
                cache_path=_cache_path(
                    cache_dir,
                    "overpass-geometry",
                    _query_cache_id(context_cache_id, query),
                ),
            )
        except SystemExit:
            # A single topologically large coast/forest way can make an
            # otherwise small public-Overpass batch time out.  Preserve exact
            # query caching and deterministically bisect only that failed
            # batch; never omit the source object or broaden the query.
            if len(batch) <= 1:
                raise
            midpoint = len(batch) // 2
            return [*fetch_batch(batch[:midpoint]), *fetch_batch(batch[midpoint:])]
        return [(batch, query, payload, snapshot_sha256, endpoint)]

    for batch in batches:
        fetched_batches.extend(fetch_batch(batch))

    elements_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    endpoints: list[str] = []
    for batch_index, (
        batch,
        query,
        payload,
        snapshot_sha256,
        endpoint,
    ) in enumerate(fetched_batches, start=1):
        elements = payload.get("elements", [])
        if not isinstance(elements, list):
            _fail(
                f"{context_cache_id} Overpass geometry batch "
                f"{batch_index}/{len(fetched_batches)} response is invalid"
            )
        batch_identifiers = set(batch)
        for element in elements:
            if not isinstance(element, dict):
                _fail(
                    f"{context_cache_id} Overpass geometry batch "
                    f"{batch_index}/{len(fetched_batches)} contains a non-object element"
                )
            object_type = element.get("type")
            identifier = element.get("id")
            if (
                object_type != "way"
                or isinstance(identifier, bool)
                or not isinstance(identifier, int)
                or identifier not in batch_identifiers
            ):
                _fail(
                    f"{context_cache_id} Overpass geometry batch "
                    f"{batch_index}/{len(fetched_batches)} returned an unrequested element"
                )
            key = (object_type, identifier)
            if key in elements_by_key:
                _fail(
                    f"{context_cache_id} Overpass geometry batches returned "
                    f"duplicate {object_type} {identifier}"
                )
            elements_by_key[key] = element
        batch_evidence = _query_evidence(
            f"geometry-batch-{batch_index:02d}",
            query,
            snapshot_sha256,
            payload,
        )
        batch_evidence.update(
            {
                "endpoint": endpoint,
                "batch_index": batch_index,
                "batch_count": len(fetched_batches),
                "way_id_count": len(batch),
            }
        )
        evidence.append(batch_evidence)
        endpoints.append(endpoint)

    ordered_elements = [
        elements_by_key[key]
        for key in sorted(elements_by_key, key=lambda item: (item[0], item[1]))
    ]
    return ordered_elements, evidence, endpoints


def _feature_path(element: dict[str, Any]) -> list[list[float]]:
    geometry = element.get("geometry")
    if not isinstance(geometry, list):
        return []
    path = [
        [round(float(point["lon"]), 6), round(float(point["lat"]), 6)]
        for point in geometry
        if isinstance(point, dict) and "lon" in point and "lat" in point
    ]
    cleaned: list[list[float]] = []
    for point in path:
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)
    return cleaned


def _path_metric(path: Sequence[Sequence[float]]) -> LineString:
    return LineString([_lonlat_to_mercator(point) for point in path])


def _representative_point(
    path: Sequence[Sequence[float]], *, closed: bool
) -> list[float]:
    metric = _path_metric(path)
    point: Point
    if closed and len(path) >= 4:
        polygon = Polygon(metric.coords)
        point = polygon.representative_point() if polygon.is_valid else metric.centroid
    else:
        point = metric.interpolate(0.5, normalized=True)
    longitude, latitude = _mercator_to_lonlat((point.x, point.y))
    return [round(longitude, 6), round(latitude, 6)]


def _normalise_ele(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", ".")
    for suffix in (" metres", " meters", " metre", " meter", " m"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    try:
        elevation = float(text)
    except ValueError:
        return None
    return elevation if -500.0 <= elevation <= 9_000.0 else None


def _context_features(
    overpass: dict[str, Any],
    *,
    route: RouteGeometry,
    extent: Sequence[float],
    source_ref: str,
    caps: dict[str, int],
    required_labels: dict[str, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    axis = _route_axis(route)
    candidates: dict[str, list[_FeatureCandidate]] = {
        "settlement": [],
        "peak": [],
        "pass": [],
        "range": [],
        "sea": [],
        "road": [],
        "water": [],
        "river": [],
        "coast": [],
        "woodland": [],
        "grass": [],
    }
    seen_objects: set[str] = set()
    elements = overpass.get("elements", [])
    if not isinstance(elements, list):
        _fail("Overpass response elements must be an array")
    for element in elements:
        if not isinstance(element, dict):
            continue
        object_type = str(element.get("type", ""))
        identifier = element.get("id")
        if object_type not in {"node", "way"} or not isinstance(identifier, int):
            continue
        source_object = f"{object_type}/{identifier}"
        if source_object in seen_objects:
            continue
        seen_objects.add(source_object)
        tags = element.get("tags", {})
        if not isinstance(tags, dict):
            tags = {}
        name = str(tags.get("name") or tags.get("ref") or "").strip()
        path = _feature_path(element)
        if object_type == "node":
            try:
                point = [
                    round(float(element["lon"]), 6),
                    round(float(element["lat"]), 6),
                ]
            except (KeyError, TypeError, ValueError):
                continue
            west, south, east, north = map(float, extent)
            if not (west <= point[0] <= east and south <= point[1] <= north):
                continue
            metric_point = Point(_lonlat_to_mercator(point))
            route_axis, distance = _axis_measure(axis, metric_point)
            if tags.get("place") in {"city", "town", "village"} and name:
                kind = "settlement"
                priority = {"city": 0, "town": 1, "village": 2}[str(tags["place"])]
            elif tags.get("place") in {"sea", "ocean"} and name:
                kind = "sea"
                priority = 0
            elif tags.get("natural") == "mountain_range" and name:
                kind = "range"
                priority = 0
            elif tags.get("natural") == "bay" and name:
                kind = "sea"
                priority = 1
            elif tags.get("natural") in {"peak", "volcano"} and name:
                kind = "peak"
                priority = 0
            elif tags.get("natural") == "saddle" or tags.get("mountain_pass") == "yes":
                if not name:
                    continue
                kind = "pass"
                priority = 1
            else:
                continue
            feature: dict[str, Any] = {
                "id": f"osm-{object_type}-{identifier}",
                "kind": kind,
                "label": name.upper(),
                "point": point,
                "source_ref": source_ref,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "osm_type": object_type,
                "osm_id": identifier,
                "priority": priority,
                "paths": [],
                "distance_to_route_m": round(distance, 1),
                "route_axis_fraction": round(route_axis, 6),
            }
            elevation = _normalise_ele(tags.get("ele"))
            if elevation is not None and kind in {"peak", "pass"}:
                feature.update(
                    {
                        "elevation_m": elevation,
                        "elevation_method": "osm-ele-tag",
                        "elevation_source_ref": source_ref,
                    }
                )
            candidates[kind].append(
                _FeatureCandidate(
                    kind=kind,
                    priority=priority,
                    distance_m=distance,
                    route_axis=route_axis,
                    page_metric=0.0,
                    page_legible=True,
                    feature=feature,
                )
            )
            continue

        if len(path) < 2:
            continue
        metric = _path_metric(path)
        closed = len(path) >= 4 and path[0] == path[-1]
        highway = str(tags.get("highway", ""))
        waterway = str(tags.get("waterway", ""))
        natural = str(tags.get("natural", ""))
        landuse = str(tags.get("landuse", ""))
        if highway:
            road_class = (
                "major"
                if highway in {"motorway", "trunk", "primary"}
                else "secondary"
                if highway in {"secondary", "tertiary"}
                else "track"
                if highway == "track"
                else "local"
            )
            kind = "road"
            priority = {"major": 0, "secondary": 1, "local": 2, "track": 3}[road_class]
            label = name.upper() if name else f"{road_class.upper()} ROAD"
            feature = {
                "id": f"osm-way-{identifier}",
                "kind": kind,
                "road_class": road_class,
                "label": label,
                "display_label": False,
                "point": _representative_point(path, closed=False),
                "paths": [path],
                "source_ref": source_ref,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "osm_type": "way",
                "osm_id": identifier,
                "priority": priority,
            }
        elif waterway in {"river", "stream"}:
            kind = "river"
            priority = 0 if waterway == "river" else 2
            feature = {
                "id": f"osm-way-{identifier}",
                "kind": kind,
                "label": name.upper() if name else waterway.upper(),
                "display_label": bool(name),
                "point": _representative_point(path, closed=False),
                "paths": [path],
                "source_ref": source_ref,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "osm_type": "way",
                "osm_id": identifier,
                "priority": priority,
            }
        elif natural == "coastline":
            kind = "coast"
            priority = 0
            feature = {
                "id": f"osm-way-{identifier}",
                "kind": kind,
                "label": name.upper() if name else "COAST",
                "display_label": bool(name),
                "point": _representative_point(path, closed=False),
                "paths": [path],
                "source_ref": source_ref,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "osm_type": "way",
                "osm_id": identifier,
                "priority": priority,
            }
        elif natural == "water" and closed:
            kind = "water"
            priority = 0
            feature = {
                "id": f"osm-way-{identifier}",
                "kind": kind,
                "label": name.upper() if name else "WATER",
                "display_label": bool(name),
                "point": _representative_point(path, closed=True),
                "paths": [path],
                "source_ref": source_ref,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "osm_type": "way",
                "osm_id": identifier,
                "priority": priority,
            }
        elif (natural in {"wood", "heath", "scrub"} or landuse == "forest") and closed:
            kind = "woodland"
            priority = 0 if natural == "wood" or landuse == "forest" else 1
            feature = {
                "id": f"osm-way-{identifier}",
                "kind": kind,
                "label": name.upper() if name else "WOODLAND",
                "display_label": False,
                "point": _representative_point(path, closed=True),
                "paths": [path],
                "source_ref": source_ref,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "osm_type": "way",
                "osm_id": identifier,
                "priority": priority,
            }
        elif (natural == "grassland" or landuse in {"grass", "meadow"}) and closed:
            kind = "grass"
            priority = 0
            feature = {
                "id": f"osm-way-{identifier}",
                "kind": kind,
                "label": name.upper() if name else "GRASSLAND",
                "display_label": False,
                "point": _representative_point(path, closed=True),
                "paths": [path],
                "source_ref": source_ref,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "osm_type": "way",
                "osm_id": identifier,
                "priority": priority,
            }
        else:
            continue
        selection_geometry: Any = metric
        if kind in CONTEXT_AREA_KINDS:
            polygon = Polygon(metric.coords)
            if not polygon.is_valid or polygon.area <= 0.0:
                continue
            selection_geometry = polygon
        route_axis, distance = _axis_measure(axis, selection_geometry)
        normalised = _normalised_visible_geometry(selection_geometry, extent)
        page_metric = (
            float(normalised.area)
            if kind in CONTEXT_AREA_KINDS
            else float(normalised.length)
        )
        page_legible = page_metric >= (
            CONTEXT_LEGIBLE_AREA_FRACTION
            if kind in CONTEXT_AREA_KINDS
            else CONTEXT_LEGIBLE_LINE_FRACTION
        )
        feature["distance_to_route_m"] = round(distance, 1)
        feature["route_axis_fraction"] = round(route_axis, 6)
        feature[
            "visible_page_area_fraction"
            if kind in CONTEXT_AREA_KINDS
            else "visible_page_length_fraction"
        ] = round(page_metric, 8)
        feature["selection_legibility"] = (
            "page-legible" if page_legible else "sub-legible-fallback"
        )
        candidates[kind].append(
            _FeatureCandidate(
                kind=kind,
                priority=priority,
                distance_m=distance,
                route_axis=route_axis,
                page_metric=page_metric,
                page_legible=page_legible,
                feature=feature,
            )
        )

    for kind, labels in (required_labels or {}).items():
        if labels and not candidates.get(kind):
            _fail(
                f"required {kind} context label(s) are absent from acquired OSM "
                "geometry: "
                + ", ".join(repr(label) for label in labels)
            )
    output: list[dict[str, Any]] = []
    for kind in sorted(candidates):
        values = candidates[kind]
        cap = int(caps.get(kind, 0))
        required: list[_FeatureCandidate] = []
        required_ids: set[str] = set()
        for label in (required_labels or {}).get(kind, ()):
            label_key = _normalised_label(label)
            matches = [
                candidate
                for candidate in values
                if _normalised_label(candidate.feature.get("label")) == label_key
            ]
            if not matches:
                _fail(
                    f"required {kind} context label {label!r} is absent from "
                    "acquired OSM geometry"
                )
            candidate = min(matches, key=_feature_candidate_score)
            identifier = str(candidate.feature["id"])
            if identifier in required_ids:
                continue
            candidate.feature["selection_reason"] = (
                "recipe-required-factual-context-label"
            )
            required.append(candidate)
            required_ids.add(identifier)
        if len(required) > cap:
            _fail(f"{len(required)} required {kind} labels exceed final cap {cap}")
        remaining = [
            candidate
            for candidate in values
            if str(candidate.feature["id"]) not in required_ids
        ]
        selected = [
            *required,
            *_distributed_select(
                remaining,
                maximum=cap - len(required),
                band_count=max(1, min(cap, 10)),
                score=_feature_candidate_score,
            ),
        ]
        output.extend(
            candidate.feature for candidate in selected
        )
    output.sort(key=lambda feature: (int(feature.get("priority", 9)), feature["id"]))
    return output


def _query_evidence(
    group: str,
    query: str,
    snapshot_sha256: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    elements = payload.get("elements", [])
    if not isinstance(elements, list):
        _fail(f"Overpass {group} response elements must be an array")
    return {
        "id": group,
        "query_sha256": _canonical_sha256(query),
        "snapshot_sha256": snapshot_sha256,
        "result_count": sum(isinstance(element, dict) for element in elements),
    }


def _context_family_evidence(
    index_payload: dict[str, Any],
    features: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe factual family availability, including truthful zero results."""

    elements = index_payload.get("elements", [])
    if not isinstance(elements, list):
        _fail("Overpass index elements must be an array")
    candidates: dict[str, set[tuple[str, int]]] = {
        family: set() for family in CONTEXT_FAMILY_KINDS
    }
    for element in elements:
        if not isinstance(element, dict):
            continue
        classification = _element_kind_priority(element)
        identifier = element.get("id")
        object_type = str(element.get("type", ""))
        if (
            classification is None
            or isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or object_type not in {"node", "way"}
        ):
            continue
        kind = classification[0]
        for family, family_kinds in CONTEXT_FAMILY_KINDS.items():
            if kind in family_kinds:
                candidates[family].add((object_type, identifier))

    selected_counts = {
        family: sum(
            str(feature.get("kind", "")) in family_kinds for feature in features
        )
        for family, family_kinds in CONTEXT_FAMILY_KINDS.items()
    }
    assessed_counts = {
        family: sum(
            str(feature.get("kind", "")) in family_kinds
            and feature.get("selection_legibility")
            in {"page-legible", "sub-legible-fallback"}
            for feature in features
        )
        for family, family_kinds in CONTEXT_FAMILY_KINDS.items()
    }
    legible_counts = {
        family: sum(
            str(feature.get("kind", "")) in family_kinds
            and feature.get("selection_legibility") == "page-legible"
            for feature in features
        )
        for family, family_kinds in CONTEXT_FAMILY_KINDS.items()
    }
    evidence: list[dict[str, Any]] = []
    for family in CONTEXT_FAMILY_KINDS:
        candidate_count = len(candidates[family])
        selected_count = selected_counts[family]
        assessed_count = assessed_counts[family]
        legible_count = legible_counts[family]
        if selected_count and assessed_count and legible_count == 0:
            status = "source-features-selected-sub-legible-at-page-scale"
        elif selected_count:
            status = "source-features-selected"
        elif candidate_count:
            status = "source-candidates-unrenderable"
        else:
            status = "source-query-zero-results"
        item = {
            "family": family,
            "status": status,
            "source_candidate_count": candidate_count,
            "selected_feature_count": selected_count,
            "query_groups": list(CONTEXT_FAMILY_QUERY_GROUPS[family]),
        }
        if assessed_count:
            item.update(
                {
                    "page_legibility_assessed_feature_count": assessed_count,
                    "page_legible_feature_count": legible_count,
                    "sub_legible_feature_count": assessed_count - legible_count,
                }
            )
        evidence.append(item)
    return evidence


def _relation_metadata(payload: dict[str, Any]) -> tuple[int, str]:
    elements = payload.get("elements", [])
    if not isinstance(elements, list) or len(elements) != 1:
        _fail("OSM relation metadata response must contain one element")
    relation = elements[0]
    if not isinstance(relation, dict):
        _fail("OSM relation metadata element is invalid")
    version = relation.get("version")
    timestamp = relation.get("timestamp")
    if not isinstance(version, int) or version <= 0 or not isinstance(timestamp, str):
        _fail("OSM relation metadata lacks version/timestamp")
    return version, timestamp


def _validate_verified_snapshots(
    recipe: dict[str, Any],
    *,
    details: dict[str, Any],
    relation_payload: dict[str, Any],
) -> None:
    """Fail if acquired route identity/version has drifted from the audit recipe."""

    subject_id = _required_recipe_text(recipe, "id")
    relation_id = recipe.get("relation_id")
    verified_relation = recipe.get("verified_relation")
    if not isinstance(verified_relation, dict):
        _fail(f"{subject_id}.verified_relation must be an object")
    expected_relation_id = verified_relation.get(
        "id", verified_relation.get("relation_id")
    )
    if expected_relation_id != relation_id:
        _fail(f"{subject_id}.verified_relation ID does not match relation_id")
    elements = relation_payload.get("elements", [])
    if not isinstance(elements, list) or len(elements) != 1:
        _fail(f"{subject_id} relation snapshot must contain exactly one element")
    relation = elements[0]
    if not isinstance(relation, dict) or relation.get("id") != relation_id:
        _fail(f"{subject_id} relation snapshot ID does not match the recipe")
    version, timestamp = _relation_metadata(relation_payload)
    if (
        verified_relation.get("version") != version
        or verified_relation.get("timestamp") != timestamp
    ):
        _fail(f"{subject_id} relation version/timestamp drifted from verification")

    verified_waymarked = recipe.get("verified_waymarked")
    if not isinstance(verified_waymarked, dict):
        _fail(f"{subject_id}.verified_waymarked must be an object")
    route = details.get("route")
    route_length = route.get("length") if isinstance(route, dict) else None
    expected_length = verified_waymarked.get("route_length_m")
    if (
        isinstance(route_length, bool)
        or not isinstance(route_length, (int, float))
        or isinstance(expected_length, bool)
        or not isinstance(expected_length, (int, float))
        or not math.isclose(
            float(route_length), float(expected_length), rel_tol=0.0, abs_tol=1.0
        )
    ):
        _fail(f"{subject_id} Waymarked route length drifted from verification")
    raw_bbox = details.get("bbox")
    expected_bbox = verified_waymarked.get("bbox_wgs84")
    if (
        not isinstance(raw_bbox, list)
        or len(raw_bbox) != 4
        or not isinstance(expected_bbox, list)
        or len(expected_bbox) != 4
    ):
        _fail(f"{subject_id} verified Waymarked bbox is invalid")
    minimum_x, minimum_y, maximum_x, maximum_y = map(float, raw_bbox)
    west, south = _mercator_to_lonlat((minimum_x, minimum_y))
    east, north = _mercator_to_lonlat((maximum_x, maximum_y))
    actual_bbox = (west, south, east, north)
    if any(
        not math.isclose(actual, float(expected), rel_tol=0.0, abs_tol=1e-5)
        for actual, expected in zip(actual_bbox, expected_bbox)
    ):
        _fail(f"{subject_id} Waymarked bbox drifted from verification")


def _required_recipe_text(recipe: dict[str, Any], key: str) -> str:
    value = recipe.get(key)
    if not isinstance(value, str) or not value.strip():
        _fail(f"recipe {recipe.get('id', '<unknown>')} requires {key}")
    return value.strip()


def _context_caps(recipe: dict[str, Any]) -> dict[str, int]:
    caps = {
        "settlement": 12,
        "peak": 8,
        "pass": 4,
        "range": 4,
        "sea": 4,
        "road": 28,
        "water": 10,
        "river": 14,
        "coast": 10,
        "woodland": 18,
        "grass": 14,
    }
    overrides = recipe.get("context_caps", {})
    if not isinstance(overrides, dict):
        _fail(f"{recipe.get('id', '<unknown>')}.context_caps must be an object")
    for raw_key, raw_value in overrides.items():
        key = str(raw_key)
        if key not in caps:
            _fail(f"{recipe.get('id', '<unknown>')}.context_caps has unknown {key!r}")
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, int)
            or not 0 <= raw_value <= 40
        ):
            _fail(
                f"{recipe.get('id', '<unknown>')}.context_caps.{key} "
                "must be an integer from 0 to 40"
            )
        caps[key] = raw_value
    return caps


def _required_context_labels(
    recipe: dict[str, Any], caps: dict[str, int]
) -> dict[str, tuple[str, ...]]:
    subject_id = str(recipe.get("id", "<unknown>"))
    raw = recipe.get("required_context_labels", {})
    if not isinstance(raw, dict):
        _fail(f"{subject_id}.required_context_labels must be an object")
    required: dict[str, tuple[str, ...]] = {}
    for raw_kind, raw_labels in raw.items():
        kind = str(raw_kind)
        if kind not in caps:
            _fail(f"{subject_id}.required_context_labels has unknown kind {kind!r}")
        if not isinstance(raw_labels, list):
            _fail(f"{subject_id}.required_context_labels.{kind} must be an array")
        labels: list[str] = []
        keys: set[str] = set()
        for raw_label in raw_labels:
            if not isinstance(raw_label, str) or not raw_label.strip():
                _fail(
                    f"{subject_id}.required_context_labels.{kind} must contain "
                    "non-empty strings"
                )
            label = " ".join(raw_label.split())
            key = _normalised_label(label)
            if key in keys:
                _fail(
                    f"{subject_id}.required_context_labels.{kind} repeats "
                    f"{label!r}"
                )
            labels.append(label)
            keys.add(key)
        if len(labels) > int(caps[kind]):
            _fail(
                f"{subject_id}.required_context_labels.{kind} exceeds context cap"
            )
        required[kind] = tuple(labels)
    return required


def _route_simplification_tolerance(
    recipe: dict[str, Any], *, route_length_m: float
) -> float:
    subject_id = str(recipe.get("id", "<unknown>"))
    default = max(80.0, min(4_000.0, route_length_m / 1_600.0))
    raw = recipe.get("route_simplification_webmercator_m")
    if raw is None:
        return default
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
        or not 5.0 <= float(raw) <= 4_000.0
    ):
        _fail(
            f"{subject_id}.route_simplification_webmercator_m must be a finite "
            "number from 5 through 4000"
        )
    return float(raw)


def _record(
    recipe: dict[str, Any],
    *,
    details: dict[str, Any],
    details_sha256: str,
    relation_payload: dict[str, Any],
    relation_sha256: str,
    context_index_payload: dict[str, Any],
    context_payload: dict[str, Any],
    context_sha256: str,
    context_query_evidence: Sequence[dict[str, Any]],
    context_query_contract_sha256: str,
    overpass_endpoint: str,
    retrieved_at: str,
) -> dict[str, Any]:
    subject_id = _required_recipe_text(recipe, "id")
    title = _required_recipe_text(recipe, "title").upper()
    relation_id = recipe.get("relation_id")
    if (
        isinstance(relation_id, bool)
        or not isinstance(relation_id, int)
        or relation_id <= 0
    ):
        _fail(f"{subject_id}.relation_id must be positive")
    official_url = _required_recipe_text(recipe, "official_url")
    if not official_url.startswith("https://"):
        _fail(f"{subject_id}.official_url must use HTTPS")
    route_length = float(details.get("route", {}).get("length", 0.0))
    if route_length <= 0.0:
        _fail(f"{subject_id} has no positive Waymarked route length")
    tolerance_m = _route_simplification_tolerance(
        recipe,
        route_length_m=route_length,
    )
    route = _route_geometry(details, tolerance_m=tolerance_m, recipe=recipe)
    extent = _padded_extent(route.bbox, route_length_m=route.source_length_m)
    relation_version, relation_timestamp = _relation_metadata(relation_payload)
    caps = _context_caps(recipe)
    required_labels = _required_context_labels(recipe, caps)
    format_id = str(recipe.get("format_id") or _choose_format(extent))
    if format_id not in {"a5-portrait", "a5-landscape"}:
        _fail(f"{subject_id}.format_id must be A5 portrait or landscape")
    map_extent = aspect_expanded_map_extent(
        extent,
        target_aspect=route_rect_aspect(format_id, has_profile=True),
    )
    features = _context_features(
        context_payload,
        route=route,
        extent=map_extent,
        source_ref="osm-context",
        caps=caps,
        required_labels=required_labels,
    )
    family_evidence = _context_family_evidence(context_index_payload, features)
    tags = details.get("tags", {}) if isinstance(details.get("tags"), dict) else {}
    itinerary = details.get("itinerary", [])
    itinerary = itinerary if isinstance(itinerary, list) else []
    start_name = str(
        recipe.get("start")
        or tags.get("from")
        or (itinerary[0] if itinerary else "START")
    )
    finish_name = str(
        recipe.get("finish")
        or tags.get("to")
        or (itinerary[-1] if itinerary else "FINISH")
    )
    # Keep the full audited endpoint names for controls and provenance, while
    # allowing an explicit compact form in the physically constrained A5
    # detail rail.  The compact copy is editorial only and must be supplied by
    # the recipe; the builder never guesses abbreviations from source names.
    detail_start_name = str(recipe.get("detail_start") or start_name).strip()
    detail_finish_name = str(recipe.get("detail_finish") or finish_name).strip()
    if not detail_start_name or not detail_finish_name:
        _fail(f"{subject_id}.detail_start/detail_finish must be non-empty")
    raw_detail_copy = recipe.get("detail_copy")
    if raw_detail_copy is not None and (
        not isinstance(raw_detail_copy, str)
        or not raw_detail_copy.strip()
        or "\n" in raw_detail_copy
        or "\r" in raw_detail_copy
    ):
        _fail(f"{subject_id}.detail_copy must be one non-empty line")
    start_point = list(route.segments[0][0])
    finish_point = list(route.segments[-1][-1])
    if not any(feature["kind"] == "settlement" for feature in features):
        for kind, label, point in (
            ("start", start_name, start_point),
            ("finish", finish_name, finish_point),
        ):
            features.append(
                {
                    "id": f"route-{kind}-context",
                    "kind": "settlement",
                    "label": label.upper(),
                    "point": point,
                    "source_ref": "osm-route",
                    "priority": -2,
                    "paths": [],
                }
            )
    official_distance_km = float(
        recipe.get("official_distance_km")
        or details.get("official_length", 0.0) / 1_000.0
        or route_length / 1_000.0
    )
    detail_copy = (
        " ".join(raw_detail_copy.split()).upper()
        if isinstance(raw_detail_copy, str)
        else (
            f"{official_distance_km:.0f} KM / "
            f"{detail_start_name.upper()} > {detail_finish_name.upper()}"
        )
    )
    country = _required_recipe_text(recipe, "country").upper()
    route_segments = [
        {
            "id": f"walk-{index:02d}",
            "mode": "walk",
            "source_ref": "osm-route",
            "points": [list(point) for point in segment],
        }
        for index, segment in enumerate(route.segments, start=1)
    ]
    credit = "© OPENSTREETMAP CONTRIBUTORS / ODBL | TERRAIN: MAPZEN / AWS OPEN DATA"
    record = {
        "id": subject_id,
        "subject_kind": "route_plate",
        "title": title,
        "subtitle": f"{country} / {official_distance_km:.0f} KM",
        "details": [
            detail_copy,
            "MAP + RELIEF / NORTH UP",
            "ARTWORK / NOT FOR NAVIGATION",
        ],
        "credit_line": credit,
        "sources": [
            {
                "id": "osm-route",
                "publisher": "OpenStreetMap contributors via Waymarked Trails",
                "url": f"https://www.openstreetmap.org/relation/{relation_id}",
                "license": "ODbL-1.0",
                "attribution": "© OpenStreetMap contributors",
                "use": "source-sampled route geometry",
                "retrieved_at": retrieved_at,
                "relation_id": relation_id,
                "relation_version": relation_version,
                "relation_timestamp": relation_timestamp,
                "acquisition_url": WAYMARKED_DETAILS.format(relation_id=relation_id),
                "waymarked_snapshot_sha256": details_sha256,
                "osm_relation_snapshot_sha256": relation_sha256,
            },
            {
                "id": "official-identity",
                "publisher": _required_recipe_text(recipe, "official_publisher"),
                "url": official_url,
                "license": "reference-only",
                "attribution": "Identity reference only",
                "use": "route identity, endpoints and published distance; no rendered map copied",
                "retrieved_at": retrieved_at,
            },
            {
                "id": "osm-context",
                "publisher": "OpenStreetMap contributors via Overpass API",
                "url": "https://www.openstreetmap.org/copyright",
                "license": "ODbL-1.0",
                "attribution": "© OpenStreetMap contributors",
                "use": "selected roads, hydrography, vegetation, settlements and terrain points",
                "retrieved_at": retrieved_at,
                "acquisition_endpoint": overpass_endpoint,
                "query_contract_id": CONTEXT_QUERY_VERSION,
                "selection_contract_id": CONTEXT_SELECTION_VERSION,
                "query_sha256": context_query_contract_sha256,
                "snapshot_sha256": context_sha256,
                "feature_count": len(features),
                "query_groups": copy.deepcopy(list(context_query_evidence)),
            },
        ],
        "route": {
            "geometry_status": "source-sampled-not-navigational",
            "navigation_status": "artwork-not-for-navigation",
            "coordinate_order": "lon-lat-ele-optional",
            "source_ref": "osm-route",
            "relation_id": relation_id,
            "relation_version": relation_version,
            "relation_timestamp": relation_timestamp,
            "official_distance_km": official_distance_km,
            "source_length_m": round(route.source_length_m),
            "simplification_webmercator_m": round(tolerance_m, 1),
            "segment_count": len(route_segments),
            "segment_ordering": (
                "source-start-nearest-endpoint-no-invented-connectors-v1"
            ),
            "geometry_recipe": recipe.get(
                "geometry_recipe",
                {"status": "main-itinerary-only", "appendices": "excluded"},
            ),
            "profile_status": "not-embedded",
            "segments": route_segments,
            "controls": [
                {
                    "kind": "start",
                    "name": start_name.upper(),
                    "point": start_point,
                    "source_ref": "osm-route",
                },
                {
                    "kind": "finish",
                    "name": finish_name.upper(),
                    "point": finish_point,
                    "source_ref": "osm-route",
                },
            ],
        },
        "backdrop": {
            "status": "stylized",
            "terrain": "stylized-contour-lines",
            "vegetation": "source-sampled",
            "seed": int(hashlib.sha256(subject_id.encode()).hexdigest()[:8], 16),
        },
        "context": {
            "status": "curated-source-sampled-art-context",
            "geometry_status": "generalized-not-for-navigation",
            "source_ref": "osm-context",
            "extent": [round(value, 6) for value in extent],
            "rotation_deg": 0.0,
            "orientation_status": "north-up",
            "selection": {
                "policy_id": CONTEXT_SELECTION_VERSION,
                "index_stage": (
                    "route-band-distributed-bounded-over-acquisition-from-OSM-centres"
                ),
                "geometry_stage": (
                    "route-band-distributed-page-clipped-source-area-or-length"
                ),
                "maximum_acquisition_per_kind": CONTEXT_MAX_ACQUISITION_PER_KIND,
                "maximum_geometry_acquisition_per_record": (
                    CONTEXT_MAX_GEOMETRY_ACQUISITION
                ),
                "area_acquisition_factor": CONTEXT_AREA_ACQUISITION_FACTOR,
                "linear_acquisition_factor": CONTEXT_LINEAR_ACQUISITION_FACTOR,
                "legible_area_fraction": CONTEXT_LEGIBLE_AREA_FRACTION,
                "legible_line_fraction": CONTEXT_LEGIBLE_LINE_FRACTION,
                "invented_connectors": False,
            },
            "family_evidence": family_evidence,
            "features": features,
        },
        "composition": {
            "pen_plan": PEN_PLAN_ID,
            "recommended_crs": "local Web Mercator sampling / north-up page transform",
            "format_id": format_id,
        },
        "scale_status": "route-scale north-up artwork",
        "evidence_status": "source-sampled OSM route and context; terrain added in frozen DEM pass",
        "rights_status": "open-data-attribution-required",
        "notes": [
            "Route and contextual vectors are source-sampled and generalized for A5 artwork.",
            "Roads are single centrelines; no casing, invented connector or doubled shoreline is drawn.",
            "The plate is artwork and must not be used for navigation.",
        ],
    }
    bind_aspect_expanded_map_extent(record, has_profile=True)
    return record


def build_catalog(
    recipes: dict[str, Any],
    *,
    cache_dir: Path,
    retrieved_at: str,
    subject_ids: set[str] | None = None,
) -> dict[str, Any]:
    if (
        recipes.get("schema_version") != 1
        or recipes.get("id") != "hike-expansion-recipes-v1"
    ):
        _fail("recipe root must use schema 1 and id hike-expansion-recipes-v1")
    if not isinstance(retrieved_at, str) or not retrieved_at.strip():
        _fail("retrieved_at must be an explicit non-empty timestamp")
    raw_routes = recipes.get("routes")
    if not isinstance(raw_routes, list) or len(raw_routes) != 30:
        _fail("recipe root requires exactly thirty routes")
    route_ids: list[str] = []
    relation_ids: list[int] = []
    for index, raw_recipe in enumerate(raw_routes):
        if not isinstance(raw_recipe, dict):
            _fail(f"routes[{index}] must be an object")
        route_ids.append(_required_recipe_text(raw_recipe, "id"))
        relation_id = raw_recipe.get("relation_id")
        if (
            isinstance(relation_id, bool)
            or not isinstance(relation_id, int)
            or relation_id <= 0
        ):
            _fail(f"{route_ids[-1]}.relation_id must be a positive integer")
        relation_ids.append(relation_id)
        caps = _context_caps(raw_recipe)
        _required_context_labels(raw_recipe, caps)
        verified_waymarked = raw_recipe.get("verified_waymarked")
        route_length = (
            verified_waymarked.get("route_length_m")
            if isinstance(verified_waymarked, dict)
            else None
        )
        if isinstance(route_length, (int, float)) and not isinstance(route_length, bool):
            _route_simplification_tolerance(
                raw_recipe,
                route_length_m=float(route_length),
            )
    if len(set(route_ids)) != 30 or len(set(relation_ids)) != 30:
        _fail("recipe root must contain thirty unique route and relation IDs")
    records: list[dict[str, Any]] = []
    for index, raw_recipe in enumerate(raw_routes):
        if not isinstance(raw_recipe, dict):
            _fail(f"routes[{index}] must be an object")
        subject_id = _required_recipe_text(raw_recipe, "id")
        if subject_ids is not None and subject_id not in subject_ids:
            continue
        relation_id = raw_recipe.get("relation_id")
        if isinstance(relation_id, bool) or not isinstance(relation_id, int):
            _fail(f"{subject_id}.relation_id must be an integer")
        details_url = WAYMARKED_DETAILS.format(relation_id=relation_id)
        details, details_sha256 = _fetch_json(
            details_url,
            cache_path=_cache_path(cache_dir, "waymarked", str(relation_id)),
        )
        if int(details.get("id", 0)) != relation_id:
            _fail(f"{subject_id} Waymarked response relation ID mismatch")
        relation, relation_sha256 = _fetch_json(
            OSM_RELATION.format(relation_id=relation_id),
            cache_path=_cache_path(cache_dir, "osm-relations", str(relation_id)),
        )
        _validate_verified_snapshots(
            raw_recipe,
            details=details,
            relation_payload=relation,
        )
        route_length_m = float(details.get("route", {}).get("length", 0.0))
        # Context snapshots are keyed to the original bounded acquisition
        # geometry.  A publication-only finer simplification may improve the
        # displayed/profile line without silently changing the frozen
        # Overpass query contract or requiring a fresh network snapshot.
        context_tolerance_m = max(
            80.0,
            min(4_000.0, route_length_m / 1_600.0),
        )
        route = _route_geometry(
            details,
            tolerance_m=context_tolerance_m,
            recipe=raw_recipe,
        )
        context_cache_key = str(raw_recipe.get("context_cache_key", "")).strip()
        context_cache_id = (
            f"{subject_id}-{context_cache_key}" if context_cache_key else subject_id
        )
        index_elements: list[dict[str, Any]] = []
        index_endpoints: list[str] = []
        query_evidence: list[dict[str, Any]] = []
        for group, query in _overpass_query_groups(route).items():
            group_payload, group_sha256, group_endpoint = _fetch_overpass(
                query,
                cache_path=_cache_path(
                    cache_dir,
                    f"overpass-index-{group}",
                    _query_cache_id(context_cache_id, query),
                ),
            )
            group_elements = group_payload.get("elements", [])
            if not isinstance(group_elements, list):
                _fail(f"{subject_id} Overpass {group} response is invalid")
            index_elements.extend(
                element for element in group_elements if isinstance(element, dict)
            )
            index_endpoints.append(group_endpoint)
            query_evidence.append(
                _query_evidence(group, query, group_sha256, group_payload)
            )
        default_caps = _context_caps(raw_recipe)
        required_labels = _required_context_labels(raw_recipe, default_caps)
        if _needs_distributed_landcover_query(
            index_elements,
            route=route,
            caps=default_caps,
        ):
            distributed_query = _distributed_landcover_query(route)
            distributed_payload, distributed_sha256, distributed_endpoint = (
                _fetch_overpass(
                    distributed_query,
                    cache_path=_cache_path(
                        cache_dir,
                        "overpass-index-areas-distributed",
                        _query_cache_id(context_cache_id, distributed_query),
                    ),
                )
            )
            distributed_elements = distributed_payload.get("elements", [])
            if not isinstance(distributed_elements, list):
                _fail(
                    f"{subject_id} Overpass distributed landcover response is invalid"
                )
            index_elements.extend(
                element for element in distributed_elements if isinstance(element, dict)
            )
            index_endpoints.append(distributed_endpoint)
            query_evidence.append(
                _query_evidence(
                    "areas-distributed",
                    distributed_query,
                    distributed_sha256,
                    distributed_payload,
                )
            )
        context_index = {"elements": index_elements}
        selected = _select_index_elements(
            context_index,
            route=route,
            caps=default_caps,
            required_labels=required_labels,
        )
        selected_nodes = [
            element for element in selected if element.get("type") == "node"
        ]
        selected_way_ids = [
            int(element["id"]) for element in selected if element.get("type") == "way"
        ]
        geometry_elements, geometry_evidence, geometry_endpoints = (
            _fetch_geometry_elements(
                selected_way_ids,
                cache_dir=cache_dir,
                context_cache_id=context_cache_id,
            )
        )
        context = {"elements": [*selected_nodes, *geometry_elements]}
        context_sha256 = _canonical_sha256(context)
        query_evidence.extend(geometry_evidence)
        endpoint = " + ".join(dict.fromkeys([*index_endpoints, *geometry_endpoints]))
        records.append(
            _record(
                raw_recipe,
                details=details,
                details_sha256=details_sha256,
                relation_payload=relation,
                relation_sha256=relation_sha256,
                context_index_payload=context_index,
                context_payload=context,
                context_sha256=context_sha256,
                context_query_evidence=query_evidence,
                context_query_contract_sha256=_canonical_sha256(
                    _overpass_query(route)
                ),
                overpass_endpoint=endpoint,
                retrieved_at=retrieved_at,
            )
        )
        print(f"{subject_id}: route={route.source_length_m / 1000:.1f} km")
    if subject_ids is not None:
        missing = sorted(subject_ids - {record["id"] for record in records})
        if missing:
            _fail(f"unknown requested subjects: {', '.join(missing)}")
    return {
        "schema_version": 1,
        "id": CATALOG_ID,
        "subject_kind": "route_plate",
        "data_snapshot": retrieved_at,
        "pen_plan": {
            "id": PEN_PLAN_ID,
            "pens": list(PENS),
            "order_note": (
                "Terrain/roads, water, vegetation, annotation, title/border, "
                "hero route last; omit no factual layer from paired release."
            ),
        },
        "plates": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipes", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--subject", action="append")
    parser.add_argument("--retrieved-at", required=True)
    args = parser.parse_args()
    catalog = build_catalog(
        _load_object(args.recipes),
        cache_dir=args.cache_dir,
        retrieved_at=args.retrieved_at,
        subject_ids=set(args.subject) if args.subject else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(catalog['plates'])} routes to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
