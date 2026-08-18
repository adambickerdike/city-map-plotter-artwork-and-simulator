#!/usr/bin/env python3
"""Derive sparse, source-valued hiking contours from an OS Terrain 50 archive.

The input is the unmodified national Grid ASCII download.  Only tiles which
intersect a catalog plate are read.  Contours are clipped in British National
Grid, simplified in metres, ranked for route context, and written as WGS84
paths into ``hike-context-v3.json``.  The result is artwork context and is not
navigation data.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Sequence

import contourpy  # type: ignore[import-not-found]
import numpy as np
from pyproj import Transformer  # type: ignore[import-not-found]
from shapely import make_valid
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-v1.json"
DEFAULT_BUNDLE = ROOT / "src" / "city_map_plotter" / "data" / "hike-context-v3.json"
DEFAULT_SELECTION_GATE = (
    ROOT / "src" / "city_map_plotter" / "data" / "hike-uk-osm-selection-v1.json"
)
SOURCE_ID = "os-terrain-50-2026"
SOURCE_URL = "https://osdatahub.os.uk/downloads/open/Terrain50"
TILE_MEMBER = re.compile(
    r"(?:^|/)data/(?P<square>[a-z]{2})/(?P<tile>[a-z]{2}[0-9]{2})_"
    r"OST50GRID_[0-9]{8}\.zip$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TerrainStyle:
    levels_m: tuple[int, ...]
    caps: tuple[int, ...]
    simplify_m: float
    minimum_length_m: float
    maximum_route_distance_m: float | None = None


SUBJECT_STYLES = {
    "RTE-GB-HEB-WALK-01": TerrainStyle(
        levels_m=(150, 300, 450, 600, 750),
        caps=(7, 8, 7, 6, 4),
        simplify_m=140.0,
        minimum_length_m=2_800.0,
        maximum_route_distance_m=10_000.0,
    ),
    "RTE-GB-GGW-01": TerrainStyle(
        levels_m=(300, 600, 900, 1_200),
        caps=(7, 9, 8, 4),
        simplify_m=150.0,
        minimum_length_m=2_500.0,
    ),
    "RTE-GB-JMW-WALK-01": TerrainStyle(
        levels_m=(100, 200, 300, 400, 500, 600),
        caps=(6, 7, 7, 6, 5, 3),
        simplify_m=140.0,
        minimum_length_m=3_000.0,
    ),
    "RTE-GB-WHW-01": TerrainStyle(
        levels_m=(300, 600, 900, 1_200),
        caps=(6, 10, 8, 2),
        simplify_m=180.0,
        minimum_length_m=2_500.0,
    ),
}


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"derive_hiking_terrain_context: {message}")


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"could not read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _terrain_selection_gate(
    subject_id: str, path: Path
) -> tuple[dict[str, Any], str] | None:
    if subject_id != "RTE-GB-HEB-WALK-01":
        return None
    manifest = _load_object(path)
    if (
        manifest.get("id") != "hike-uk-osm-selection-v1"
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "frozen-audited-selection-gate"
    ):
        _fail("UK selection gate has an unsupported schema")
    subjects = manifest.get("subjects")
    subject = subjects.get(subject_id) if isinstance(subjects, dict) else None
    gate = subject.get("terrain_gate") if isinstance(subject, dict) else None
    if not isinstance(gate, dict):
        _fail(f"UK selection gate has no terrain gate for {subject_id}")
    return gate, _canonical_sha256(manifest)


def _record(catalog: dict[str, Any], subject_id: str) -> dict[str, Any]:
    for record in catalog.get("plates", []):
        if isinstance(record, dict) and record.get("id") == subject_id:
            return record
    _fail(f"catalog has no plate {subject_id!r}")


def _overlay(bundle: dict[str, Any], subject_id: str) -> dict[str, Any]:
    records = bundle.setdefault("records", [])
    if not isinstance(records, list):
        _fail("context bundle records must be an array")
    for record in records:
        if isinstance(record, dict) and record.get("subject_id") == subject_id:
            return record
    overlay: dict[str, Any] = {
        "subject_id": subject_id,
        "sources": [],
        "context": {},
        "backdrop": {},
    }
    records.append(overlay)
    return overlay


def _geometry_lines(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [LineString(geometry.coords)]  # type: ignore[attr-defined]
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        return [
            line
            for child in geometry.geoms  # type: ignore[attr-defined]
            for line in _geometry_lines(child)
        ]
    return []


def densified_bbox_polygon(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    segments_per_edge: int = 100,
) -> Polygon:
    """Represent a geographic bbox without replacing curved projected edges by chords."""

    if segments_per_edge < 1:
        _fail("segments_per_edge must be positive")
    points: list[tuple[float, float]] = []
    for index in range(segments_per_edge + 1):
        ratio = index / segments_per_edge
        points.append((west + (east - west) * ratio, south))
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        points.append((east, south + (north - south) * ratio))
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        points.append((east - (east - west) * ratio, north))
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        points.append((west, north - (north - south) * ratio))
    if points[-1] != points[0]:
        points.append(points[0])
    return Polygon(points)


def _grid_square_origin(square: str) -> tuple[int, int]:
    """Return the BNG origin of a two-letter 100 km square."""

    if len(square) != 2 or not square.isalpha():
        _fail(f"invalid OS grid square {square!r}")
    indices: list[int] = []
    for character in square.upper():
        index = ord(character) - ord("A")
        if index > 7:  # The National Grid alphabet omits I.
            index -= 1
        if not 0 <= index <= 24:
            _fail(f"invalid OS grid square {square!r}")
        indices.append(index)
    first, second = indices
    easting = (((first - 2) % 5) * 5 + second % 5) * 100_000
    northing = (19 - (first // 5) * 5 - second // 5) * 100_000
    return easting, northing


def _tile_origin(tile: str) -> tuple[int, int]:
    match = re.fullmatch(r"([a-zA-Z]{2})([0-9])([0-9])", tile)
    if match is None:
        _fail(f"invalid OS Terrain 50 tile {tile!r}")
    easting, northing = _grid_square_origin(match.group(1))
    return (
        easting + int(match.group(2)) * 10_000,
        northing + int(match.group(3)) * 10_000,
    )


def _selected_members(
    archive: zipfile.ZipFile,
    *,
    crop: BaseGeometry,
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for member in archive.namelist():
        match = TILE_MEMBER.search(member)
        if match is None:
            continue
        tile = match.group("tile").lower()
        x, y = _tile_origin(tile)
        if box(x, y, x + 10_000, y + 10_000).intersects(crop):
            selected.append((tile, member))
    return sorted(selected)


def _read_ascii_tile(
    archive: zipfile.ZipFile, member: str
) -> tuple[float, float, float, np.ndarray]:
    with zipfile.ZipFile(io.BytesIO(archive.read(member))) as nested:
        ascii_members = [
            name for name in nested.namelist() if name.lower().endswith(".asc")
        ]
        if len(ascii_members) != 1:
            _fail(f"{member} must contain exactly one Grid ASCII file")
        with nested.open(ascii_members[0]) as handle:
            header: dict[str, float] = {}
            for _ in range(5):
                line = handle.readline().decode("ascii").strip().split()
                if len(line) != 2:
                    _fail(f"invalid Grid ASCII header in {member}")
                header[line[0].casefold()] = float(line[1])
            required = {"ncols", "nrows", "xllcorner", "yllcorner", "cellsize"}
            if not required.issubset(header):
                _fail(f"incomplete Grid ASCII header in {member}")
            values = np.loadtxt(handle, dtype=np.float32)
    ncols = int(header["ncols"])
    nrows = int(header["nrows"])
    if values.shape != (nrows, ncols):
        _fail(f"unexpected raster shape {values.shape!r} in {member}")
    if ncols != 200 or nrows != 200 or header["cellsize"] != 50.0:
        _fail(f"unexpected OS Terrain 50 grid geometry in {member}")
    # Grid ASCII rows run north to south.  The mosaic is south to north.
    return (
        header["xllcorner"],
        header["yllcorner"],
        header["cellsize"],
        np.flipud(values),
    )


def _mosaic(
    archive: zipfile.ZipFile,
    members: Sequence[tuple[str, str]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    if not members:
        _fail("no OS Terrain 50 tiles intersect the requested extent")
    tiles = [(*_read_ascii_tile(archive, member), tile) for tile, member in members]
    cell_sizes = {tile[2] for tile in tiles}
    if cell_sizes != {50.0}:
        _fail(f"mixed Terrain 50 cell sizes: {sorted(cell_sizes)!r}")
    min_x = min(tile[0] for tile in tiles)
    min_y = min(tile[1] for tile in tiles)
    max_x = max(tile[0] + tile[3].shape[1] * 50.0 for tile in tiles)
    max_y = max(tile[1] + tile[3].shape[0] * 50.0 for tile in tiles)
    columns = int(round((max_x - min_x) / 50.0))
    rows = int(round((max_y - min_y) / 50.0))
    values: np.ndarray = np.full((rows, columns), np.nan, dtype=np.float32)
    for x, y, _, tile_values, _ in tiles:
        column = int(round((x - min_x) / 50.0))
        row = int(round((y - min_y) / 50.0))
        height, width = tile_values.shape
        values[row : row + height, column : column + width] = tile_values
    x_coordinates = min_x + 25.0 + np.arange(columns, dtype=np.float64) * 50.0
    y_coordinates = min_y + 25.0 + np.arange(rows, dtype=np.float64) * 50.0
    return values, x_coordinates, y_coordinates, [tile[4] for tile in tiles]


def _route_geometry(record: dict[str, Any], forward: Transformer) -> BaseGeometry:
    lines = [
        LineString([(float(point[0]), float(point[1])) for point in segment["points"]])
        for segment in record["route"]["segments"]
    ]
    return transform(forward.transform, unary_union(lines))


def _select_contour_candidates(
    candidates: Sequence[tuple[float, float, bool, LineString]],
    *,
    cap: int,
    route: BaseGeometry,
    minimum_separation_m: float = 0.0,
    already_selected: Sequence[LineString] = (),
    prefer_broad: bool = False,
) -> list[tuple[float, float, bool, LineString]]:
    """Select coherent relief near, and along, the whole route.

    A global ``distance - length`` score allowed one very long but remote
    mountain chain to displace several route-relevant contours.  That was
    particularly misleading on island routes: Skye could receive more relief
    than the Outer Hebrides.  Use the route's dominant axis as a small set of
    coverage bins, rank proximity in 5 km bands, then prefer closed complete
    isolines and length.  Round-robin selection prevents all relief collecting
    at only one end of a long route while keeping remote context as a fallback.
    """

    if cap <= 0 or not candidates:
        return []
    if minimum_separation_m < 0.0:
        _fail("minimum contour separation cannot be negative")
    minimum_x, minimum_y, maximum_x, maximum_y = route.bounds
    use_x = maximum_x - minimum_x >= maximum_y - minimum_y
    axis_minimum = minimum_x if use_x else minimum_y
    axis_maximum = maximum_x if use_x else maximum_y
    axis_span = max(axis_maximum - axis_minimum, 1.0)
    bin_count = min(5, cap)
    route_span = max(maximum_x - minimum_x, maximum_y - minimum_y)
    context_limit_m = min(40_000.0, max(15_000.0, route_span * 0.18))

    def rank(
        item: tuple[float, float, bool, LineString],
    ) -> tuple[int, float, float, float, str]:
        distance, length, closed, line = item
        if prefer_broad:
            return (
                int(distance // 5_000.0),
                -length,
                distance,
                0.0 if closed else 1.0,
                line.wkb_hex,
            )
        return (
            int(distance // 5_000.0),
            0.0 if closed else 1.0,
            distance,
            -length,
            line.wkb_hex,
        )

    primary = [item for item in candidates if item[0] <= context_limit_m]
    pool = primary if len(primary) >= min(cap, len(candidates)) else list(candidates)
    buckets: list[list[tuple[float, float, bool, LineString]]] = [
        [] for _ in range(bin_count)
    ]
    for item in pool:
        line = item[3]
        centre = line.interpolate(0.5, normalized=True)
        coordinate = float(centre.x if use_x else centre.y)
        ratio = min(1.0, max(0.0, (coordinate - axis_minimum) / axis_span))
        index = min(int(ratio * bin_count), bin_count - 1)
        buckets[index].append(item)
    for bucket in buckets:
        bucket.sort(key=rank)

    selected: list[tuple[float, float, bool, LineString]] = []

    def separated(line: LineString) -> bool:
        if minimum_separation_m <= 0.0:
            return True
        comparison = (*already_selected, *(item[3] for item in selected))
        return all(
            line.distance(existing) + 1e-6 >= minimum_separation_m
            for existing in comparison
        )

    while len(selected) < cap:
        progressed = False
        for bucket in buckets:
            while bucket and len(selected) < cap:
                candidate = bucket.pop(0)
                if not separated(candidate[3]):
                    continue
                selected.append(candidate)
                progressed = True
                break
        if not progressed:
            break
    if len(selected) < cap:
        selected_ids = {item[3].wkb_hex for item in selected}
        remainder = sorted(
            (item for item in candidates if item[3].wkb_hex not in selected_ids),
            key=rank,
        )
        for candidate in remainder:
            if len(selected) >= cap:
                break
            if separated(candidate[3]):
                selected.append(candidate)
    return selected


def _closed_contour_contains(line: LineString, anchor: Point) -> bool:
    if not line.is_closed or len(line.coords) < 4:
        return False
    try:
        polygon = make_valid(Polygon(line.coords))
    except (TypeError, ValueError):
        return False
    return bool(not polygon.is_empty and polygon.covers(anchor))


def _select_anchored_contour_clusters(
    candidate_levels: Sequence[
        tuple[int, int, Sequence[tuple[float, float, bool, LineString]]]
    ],
    *,
    anchors: Sequence[tuple[str, str, Point]],
    radius_m: float,
    minimum_paths: int,
    maximum_paths: int,
    minimum_separation_m: float,
) -> tuple[
    dict[int, list[tuple[float, float, bool, LineString]]],
    dict[str, str],
    list[str],
]:
    """Select nested, source-complete contour clusters around named anchors."""

    if radius_m <= 0.0:
        _fail("anchor cluster radius must be positive")
    if not 2 <= minimum_paths <= maximum_paths:
        _fail("anchor clusters require 2..maximum_paths source contours")
    selected_by_level: dict[
        int, list[tuple[float, float, bool, LineString]]
    ] = {level: [] for level, _, _ in candidate_levels}
    selected_lines: list[LineString] = []
    selected_ids: set[str] = set()
    anchor_by_line: dict[str, str] = {}
    completed_anchors: list[str] = []

    for anchor_id, anchor_kind, anchor in anchors:
        options: list[
            tuple[int, list[tuple[float, float, bool, LineString]]]
        ] = []
        for level, cap, candidates in candidate_levels:
            available = cap - len(selected_by_level[level])
            if available <= 0:
                continue
            ranked = sorted(
                (
                    item
                    for item in candidates
                    if item[3].wkb_hex not in selected_ids
                    and item[3].distance(anchor) <= radius_m
                ),
                key=lambda item: (
                    0 if _closed_contour_contains(item[3], anchor) else 1,
                    item[3].distance(anchor),
                    -item[1],
                    item[3].wkb_hex,
                ),
            )
            if ranked:
                options.append((level, ranked[:3]))
        if len(options) < minimum_paths:
            continue

        best: tuple[
            tuple[float, ...],
            tuple[tuple[int, tuple[float, float, bool, LineString]], ...],
        ] | None = None
        choices = [[None, *items] for _, items in options]
        for raw_choice in itertools.product(*choices):
            chosen = tuple(
                (options[index][0], item)
                for index, item in enumerate(raw_choice)
                if item is not None
            )
            if not minimum_paths <= len(chosen) <= maximum_paths:
                continue
            lines = [item[3] for _, item in chosen]
            if any(
                first.distance(second) + 1e-6 < minimum_separation_m
                for first, second in itertools.combinations(lines, 2)
            ):
                continue
            if any(
                line.distance(existing) + 1e-6 < minimum_separation_m
                for line in lines
                for existing in selected_lines
            ):
                continue
            containing = sum(
                _closed_contour_contains(item[3], anchor) for _, item in chosen
            )
            anchor_distance = sum(item[3].distance(anchor) for _, item in chosen)
            total_length = sum(item[1] for _, item in chosen)
            # More elevations and actual nested containment dominate.  Distance
            # and length then make the deterministic choice local and coherent.
            score = (
                -float(len(chosen)),
                -float(containing),
                anchor_distance,
                -total_length,
            )
            if anchor_kind in {"peak", "range"} and containing == 0:
                score = (*score[:1], score[1] + 0.5, *score[2:])
            if best is None or score < best[0]:
                best = (score, chosen)
        if best is None:
            continue
        for level, item in best[1]:
            selected_by_level[level].append(item)
            selected_lines.append(item[3])
            selected_ids.add(item[3].wkb_hex)
            anchor_by_line[item[3].wkb_hex] = anchor_id
        completed_anchors.append(anchor_id)

    return selected_by_level, anchor_by_line, completed_anchors


def _contour_paths(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    *,
    levels_m: Sequence[int],
    caps: Sequence[int],
    crop: Polygon,
    route: BaseGeometry,
    inverse: Transformer,
    simplify_m: float,
    minimum_length_m: float,
    maximum_route_distance_m: float | None,
    minimum_separation_m: float = 0.0,
    maximum_path_span_m: float | None = None,
    maximum_path_length_m: float | None = None,
    exclusion: BaseGeometry | None = None,
    prefer_broad: bool = False,
    anchors: Sequence[tuple[str, str, Point]] = (),
    anchor_cluster_radius_m: float | None = None,
    anchor_cluster_minimum_paths: int = 2,
    anchor_cluster_maximum_paths: int = 4,
) -> list[dict[str, Any]]:
    if anchors:
        if anchor_cluster_radius_m is None:
            _fail("named contour anchors require a cluster radius")
        return _anchored_contour_paths(
            values,
            x_coordinates,
            y_coordinates,
            levels_m=levels_m,
            caps=caps,
            crop=crop,
            route=route,
            inverse=inverse,
            simplify_m=simplify_m,
            minimum_length_m=minimum_length_m,
            maximum_route_distance_m=maximum_route_distance_m,
            minimum_separation_m=minimum_separation_m,
            maximum_path_span_m=maximum_path_span_m,
            maximum_path_length_m=maximum_path_length_m,
            exclusion=exclusion,
            anchors=anchors,
            anchor_cluster_radius_m=anchor_cluster_radius_m,
            anchor_cluster_minimum_paths=anchor_cluster_minimum_paths,
            anchor_cluster_maximum_paths=anchor_cluster_maximum_paths,
        )
    generator = contourpy.contour_generator(
        x=x_coordinates,
        y=y_coordinates,
        z=np.ma.masked_invalid(values),
        line_type="Separate",
    )
    output: list[dict[str, Any]] = []
    selected_lines: list[LineString] = []
    for level, cap in zip(levels_m, caps, strict=True):
        candidates: list[tuple[float, float, bool, LineString]] = []
        for raw_line in generator.lines(float(level)):
            coordinates = np.asarray(raw_line, dtype=np.float64)
            if coordinates.ndim != 2 or coordinates.shape[0] < 2:
                continue
            clipped = make_valid(LineString(coordinates.tolist()).intersection(crop))
            for line in _geometry_lines(clipped):
                if line.length < minimum_length_m:
                    continue
                simple = line.simplify(simplify_m, preserve_topology=True)
                for part in _geometry_lines(simple):
                    if part.length < minimum_length_m:
                        continue
                    distance = float(part.distance(route))
                    if (
                        maximum_route_distance_m is not None
                        and distance > maximum_route_distance_m
                    ):
                        continue
                    minimum_x, minimum_y, maximum_x, maximum_y = part.bounds
                    span = max(maximum_x - minimum_x, maximum_y - minimum_y)
                    if maximum_path_span_m is not None and span > maximum_path_span_m:
                        continue
                    if (
                        maximum_path_length_m is not None
                        and part.length > maximum_path_length_m
                    ):
                        continue
                    if exclusion is not None and part.intersects(exclusion):
                        continue
                    candidates.append(
                        (
                            distance,
                            float(part.length),
                            bool(part.is_closed),
                            part,
                        )
                    )
        selected = _select_contour_candidates(
            candidates,
            cap=cap,
            route=route,
            minimum_separation_m=minimum_separation_m,
            already_selected=selected_lines,
            prefer_broad=prefer_broad,
        )
        selected_lines.extend(item[3] for item in selected)
        paths: list[list[list[float]]] = []
        total_length_m = 0.0
        selected_closed = 0
        selected_maximum_route_distance_m = 0.0
        for distance, _, closed, line in selected:
            total_length_m += float(line.length)
            selected_closed += int(closed)
            selected_maximum_route_distance_m = max(
                selected_maximum_route_distance_m, distance
            )
            geographic = transform(inverse.transform, line)
            path = [
                [round(float(x), 6), round(float(y), 6)] for x, y in geographic.coords
            ]
            deduplicated = [
                point
                for index, point in enumerate(path)
                if index == 0 or point != path[index - 1]
            ]
            if len(deduplicated) >= 2:
                paths.append(deduplicated)
        if paths:
            output.append(
                {
                    "elevation_m": level,
                    "paths": paths,
                    "selection_rule": (
                        f"route-axis-stratified-coherent-{cap}-minimum-"
                        f"{int(minimum_length_m)}m"
                        f"-spacing-{int(minimum_separation_m)}m"
                        + (
                            f"-route-within-{int(maximum_route_distance_m)}m"
                            if maximum_route_distance_m is not None
                            else ""
                        )
                    ),
                    "derived_total_length_m": round(total_length_m, 1),
                    "selected_closed_path_count": selected_closed,
                    "selected_maximum_route_distance_m": round(
                        selected_maximum_route_distance_m, 1
                    ),
                    "selection_constraints": {
                        "minimum_pairwise_separation_m": minimum_separation_m,
                        "maximum_route_distance_m": maximum_route_distance_m,
                        "maximum_path_span_m": maximum_path_span_m,
                        "maximum_path_length_m": maximum_path_length_m,
                        "exclusion_applied": exclusion is not None,
                        "prefer_broad_source_contours": prefer_broad,
                    },
                }
            )
    if not output:
        _fail("no plot-legible contours survived selection")
    return output


def _anchored_contour_paths(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    *,
    levels_m: Sequence[int],
    caps: Sequence[int],
    crop: Polygon,
    route: BaseGeometry,
    inverse: Transformer,
    simplify_m: float,
    minimum_length_m: float,
    maximum_route_distance_m: float | None,
    minimum_separation_m: float,
    maximum_path_span_m: float | None,
    maximum_path_length_m: float | None,
    exclusion: BaseGeometry | None,
    anchors: Sequence[tuple[str, str, Point]],
    anchor_cluster_radius_m: float,
    anchor_cluster_minimum_paths: int,
    anchor_cluster_maximum_paths: int,
) -> list[dict[str, Any]]:
    """Derive whole-contour mountain clusters around named source points."""

    generator = contourpy.contour_generator(
        x=x_coordinates,
        y=y_coordinates,
        z=np.ma.masked_invalid(values),
        line_type="Separate",
    )
    candidate_levels: list[
        tuple[int, int, list[tuple[float, float, bool, LineString]]]
    ] = []
    for level, cap in zip(levels_m, caps, strict=True):
        candidates: list[tuple[float, float, bool, LineString]] = []
        for raw_line in generator.lines(float(level)):
            coordinates = np.asarray(raw_line, dtype=np.float64)
            if coordinates.ndim != 2 or coordinates.shape[0] < 2:
                continue
            clipped = make_valid(LineString(coordinates.tolist()).intersection(crop))
            for line in _geometry_lines(clipped):
                if line.length < minimum_length_m:
                    continue
                simple = line.simplify(simplify_m, preserve_topology=True)
                for part in _geometry_lines(simple):
                    if part.length < minimum_length_m:
                        continue
                    distance = float(part.distance(route))
                    if (
                        maximum_route_distance_m is not None
                        and distance > maximum_route_distance_m
                    ):
                        continue
                    minimum_x, minimum_y, maximum_x, maximum_y = part.bounds
                    span = max(maximum_x - minimum_x, maximum_y - minimum_y)
                    if maximum_path_span_m is not None and span > maximum_path_span_m:
                        continue
                    if (
                        maximum_path_length_m is not None
                        and part.length > maximum_path_length_m
                    ):
                        continue
                    if exclusion is not None and part.intersects(exclusion):
                        continue
                    candidates.append(
                        (
                            distance,
                            float(part.length),
                            bool(part.is_closed),
                            part,
                        )
                    )
        candidate_levels.append((int(level), int(cap), candidates))

    selected_by_level, anchor_by_line, completed_anchors = (
        _select_anchored_contour_clusters(
            candidate_levels,
            anchors=anchors,
            radius_m=anchor_cluster_radius_m,
            minimum_paths=anchor_cluster_minimum_paths,
            maximum_paths=anchor_cluster_maximum_paths,
            minimum_separation_m=minimum_separation_m,
        )
    )
    output: list[dict[str, Any]] = []
    for level, cap, _ in candidate_levels:
        selected = selected_by_level[level]
        if not selected:
            continue
        paths: list[list[list[float]]] = []
        path_anchor_ids: list[str] = []
        total_length_m = 0.0
        selected_closed = 0
        selected_maximum_route_distance_m = 0.0
        for distance, _, closed, line in selected:
            total_length_m += float(line.length)
            selected_closed += int(closed)
            selected_maximum_route_distance_m = max(
                selected_maximum_route_distance_m, distance
            )
            geographic = transform(inverse.transform, line)
            path = [
                [round(float(x), 6), round(float(y), 6)]
                for x, y in geographic.coords
            ]
            deduplicated = [
                point
                for index, point in enumerate(path)
                if index == 0 or point != path[index - 1]
            ]
            if len(deduplicated) >= 2:
                paths.append(deduplicated)
                path_anchor_ids.append(anchor_by_line[line.wkb_hex])
        if paths:
            output.append(
                {
                    "elevation_m": level,
                    "paths": paths,
                    "path_anchor_ids": path_anchor_ids,
                    "cluster_anchor_ids": sorted(set(path_anchor_ids)),
                    "completed_cluster_anchor_ids": completed_anchors,
                    "selection_rule": (
                        "named-anchor-nested-whole-contours-v1-"
                        f"{anchor_cluster_minimum_paths}to"
                        f"{anchor_cluster_maximum_paths}-cap-{cap}"
                    ),
                    "derived_total_length_m": round(total_length_m, 1),
                    "selected_closed_path_count": selected_closed,
                    "selected_maximum_route_distance_m": round(
                        selected_maximum_route_distance_m, 1
                    ),
                    "selection_constraints": {
                        "minimum_pairwise_separation_m": minimum_separation_m,
                        "maximum_route_distance_m": maximum_route_distance_m,
                        "maximum_path_span_m": maximum_path_span_m,
                        "maximum_path_length_m": maximum_path_length_m,
                        "anchor_cluster_radius_m": anchor_cluster_radius_m,
                        "anchor_cluster_minimum_paths": (
                            anchor_cluster_minimum_paths
                        ),
                        "anchor_cluster_maximum_paths": (
                            anchor_cluster_maximum_paths
                        ),
                        "exclusion_applied": exclusion is not None,
                    },
                }
            )
    if not output:
        _fail("no named-anchor contour cluster survived selection")
    return output


def derive_subject(
    *,
    archive: zipfile.ZipFile,
    archive_sha256: str,
    catalog: dict[str, Any],
    bundle: dict[str, Any],
    subject_id: str,
    retrieved_at: str,
    selection_gate_path: Path = DEFAULT_SELECTION_GATE,
) -> dict[str, Any]:
    style = SUBJECT_STYLES.get(subject_id)
    if style is None:
        _fail(f"no reviewed Terrain 50 style for {subject_id!r}")
    record = _record(catalog, subject_id)
    extent = record["context"]["extent"]
    if not isinstance(extent, list) or len(extent) != 4:
        _fail(f"{subject_id} has an invalid extent")
    west, south, east, north = (float(value) for value in extent)
    forward = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    inverse = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    crop = transform(
        forward.transform,
        densified_bbox_polygon(west, south, east, north),
    )
    route = _route_geometry(record, forward)
    members = _selected_members(archive, crop=crop)
    values, x_coordinates, y_coordinates, tile_ids = _mosaic(archive, members)
    contours = _contour_paths(
        values,
        x_coordinates,
        y_coordinates,
        levels_m=style.levels_m,
        caps=style.caps,
        crop=crop,
        route=route,
        inverse=inverse,
        simplify_m=style.simplify_m,
        minimum_length_m=style.minimum_length_m,
        maximum_route_distance_m=style.maximum_route_distance_m,
    )
    gate_info = _terrain_selection_gate(subject_id, selection_gate_path)
    if gate_info is not None:
        gate, _ = gate_info
        if archive_sha256 != gate["source_snapshot_sha256"]:
            _fail(f"{subject_id} Terrain 50 snapshot differs from its audited gate")
        path_counts = {
            str(contour["elevation_m"]): len(contour["paths"]) for contour in contours
        }
        maximum_distances = {
            str(contour["elevation_m"]): contour["selected_maximum_route_distance_m"]
            for contour in contours
        }
        geometry_manifest = [
            {"elevation_m": contour["elevation_m"], "paths": contour["paths"]}
            for contour in contours
        ]
        if (
            sum(path_counts.values()) != int(gate["selected_path_count"])
            or path_counts != gate["selected_path_counts_by_level"]
            or maximum_distances != gate["selected_maximum_route_distance_m_by_level"]
            or _canonical_sha256(geometry_manifest) != gate["geometry_manifest_sha256"]
        ):
            _fail(f"{subject_id} terrain paths differ from their audited gate")
    overlay = _overlay(bundle, subject_id)
    sources = overlay.setdefault("sources", [])
    if not isinstance(sources, list):
        _fail(f"{subject_id} overlay sources must be an array")
    sources[:] = [item for item in sources if item.get("id") != SOURCE_ID]
    source: dict[str, Any] = {
        "id": SOURCE_ID,
        "publisher": "Ordnance Survey",
        "url": SOURCE_URL,
        "license": "Open Government Licence v3.0",
        "attribution": ("Contains OS data © Crown copyright and database right 2026"),
        "use": ("OS Terrain 50 50 m DTM; selected, elevation-valued artwork contours"),
        "release": "July 2026",
        "retrieved_at": retrieved_at,
        "horizontal_crs": "EPSG:27700",
        "vertical_datum": "ODN / EPSG:5701",
        "snapshot_sha256": archive_sha256,
    }
    if gate_info is not None:
        _, manifest_sha256 = gate_info
        source["selection_gate"] = {
            "id": "hike-uk-osm-selection-v1",
            "manifest_sha256": manifest_sha256,
            "selection_rule": _load_object(selection_gate_path)["subjects"][subject_id][
                "selection_rules"
            ]["terrain"],
        }
    sources.append(source)
    context = overlay.setdefault("context", {})
    context["terrain"] = {
        "status": "source-derived-dtm-relief",
        "source_ref": SOURCE_ID,
        "derivation_id": f"os-terrain50-{subject_id.casefold()}-contours-v4",
        "source_crs": "EPSG:27700",
        "vertical_datum": "ODN / EPSG:5701",
        "source_grid_resolution_m": 50,
        "contour_levels_m": list(style.levels_m),
        "simplification_tolerance_m": {"contours": style.simplify_m},
        "contour_selection_caps": {
            str(level): cap
            for level, cap in zip(style.levels_m, style.caps, strict=True)
        },
        "maximum_route_context_distance_m": style.maximum_route_distance_m,
        "source_tiles": tile_ids,
        "areas": [],
        "contours": contours,
    }
    if gate_info is not None:
        context["terrain"]["selection_profile_id"] = "hike-uk-osm-selection-v1"
    backdrop = overlay.setdefault("backdrop", {})
    backdrop["status"] = "source-derived"
    backdrop["terrain"] = "source-derived-dtm-relief"
    existing_credit = str(overlay.get("credit_line") or record.get("credit_line") or "")
    if "OPENSTREETMAP" in existing_credit.upper():
        overlay["credit_line"] = (
            "OS DATA © CROWN COPYRIGHT 2026 | "
            "© OSM CONTRIBUTORS / OPENSTREETMAP.ORG/COPYRIGHT"
        )
    else:
        overlay["credit_line"] = "CONTAINS OS DATA © CROWN COPYRIGHT 2026"
    return {
        "tiles": len(tile_ids),
        "levels": len(contours),
        "paths": sum(len(contour["paths"]) for contour in contours),
        "minimum_m": float(np.nanmin(values)),
        "maximum_m": float(np.nanmax(values)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--subject", action="append", required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--selection-gate", type=Path, default=DEFAULT_SELECTION_GATE)
    parser.add_argument("--retrieved-at", default="2026-08-03T00:00:00Z")
    parser.add_argument(
        "--snapshot-sha256",
        help="Verified archive digest; computed when omitted.",
    )
    args = parser.parse_args()
    if not args.archive.is_file():
        parser.error(f"archive does not exist: {args.archive}")
    catalog = _load_object(args.catalog)
    bundle = _load_object(args.bundle)
    archive_sha256 = args.snapshot_sha256 or _sha256(args.archive)
    if not re.fullmatch(r"[0-9a-f]{64}", archive_sha256):
        parser.error("--snapshot-sha256 must be 64 lower-case hexadecimal characters")
    with zipfile.ZipFile(args.archive) as archive:
        for subject_id in args.subject:
            result = derive_subject(
                archive=archive,
                archive_sha256=archive_sha256,
                catalog=catalog,
                bundle=bundle,
                subject_id=subject_id,
                retrieved_at=args.retrieved_at,
                selection_gate_path=args.selection_gate,
            )
            print(
                f"{subject_id}: tiles={result['tiles']}, levels={result['levels']}, "
                f"paths={result['paths']}, elevation="
                f"{result['minimum_m']:.1f}..{result['maximum_m']:.1f}m"
            )
    args.bundle.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
