"""Authoritative TfGM geometry plus dated GTFS compiler for Metrolink.

The TfGM map-data archive describes the open physical alignment and 99 stop
points.  It does *not* describe the coloured passenger services.  The GTFS
snapshot supplies ordered stop visits.  This compiler combines those two
sources without tracing the operator diagram and without creating graph nodes
at arbitrary geometric crossings.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import heapq
import io
import json
from math import cos, degrees, hypot, radians
from pathlib import Path
import re
from typing import Any, Iterable, Sequence
import zipfile

from shapely.geometry import LineString, Point
from shapely.ops import substring

from .models import MapPlotterError
from .transit import (
    canonical_contract_bytes,
    catalog_network,
    load_transit_network,
)
from .transit_source import SnapshotClient


LINES_MEMBER = "JSON-format/Metrolink_Lines_Functional.json"
STOPS_MEMBER = "JSON-format/Metrolink_Stops_Functional.json"
DEFAULT_MAX_STATION_SNAP_M = 1.0
DEFAULT_MAX_JOIN_SNAP_M = 1.0
DEFAULT_MAX_SHAPE_HAUSDORFF_M = 120.0
DEFAULT_MAX_SEGMENT_SHAPE_HAUSDORFF_M = 40.0
_EARTH_RADIUS_M = 6_371_008.8


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unnamed"


def _name_key(value: str) -> str:
    normalized = re.sub(
        r"\s*\(Manchester Metrolink\)\s*$", "", value, flags=re.IGNORECASE
    )
    normalized = re.sub(r"\s+Metrolink Stop\s*$", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def _clean_points(points: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for index, raw in enumerate(points):
        if len(raw) < 2:
            raise MapPlotterError(
                f"TfGM geometry coordinate {index} has fewer than two ordinates."
            )
        point = (float(raw[0]), float(raw[1]))
        if not (-180.0 <= point[0] <= 180.0 and -85.0 <= point[1] <= 85.0):
            raise MapPlotterError("TfGM geometry contains an invalid WGS84 coordinate.")
        if not result or point != result[-1]:
            result.append(point)
    if len(result) < 2:
        raise MapPlotterError(
            "TfGM geometry becomes degenerate after duplicate removal."
        )
    return result


def _zip_json(archive: zipfile.ZipFile, member: str) -> dict[str, Any]:
    try:
        info = archive.getinfo(member)
    except KeyError as exc:
        raise MapPlotterError(f"TfGM archive is missing {member}.") from exc
    if info.file_size > 20 * 1024 * 1024:
        raise MapPlotterError(f"TfGM archive member {member} is unexpectedly large.")
    try:
        raw = json.loads(archive.read(info))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"TfGM archive member {member} is invalid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise MapPlotterError(f"TfGM archive member {member} must contain an object.")
    return raw


def _feature_list(
    document: dict[str, Any], *, expected_name: str, source: str
) -> list[dict[str, Any]]:
    if (
        document.get("type") != "FeatureCollection"
        or document.get("name") != expected_name
    ):
        raise MapPlotterError(
            f"{source} must be the TfGM {expected_name} FeatureCollection."
        )
    raw = document.get("features")
    if not isinstance(raw, list) or not raw:
        raise MapPlotterError(f"{source} has no features.")
    if not all(isinstance(item, dict) for item in raw):
        raise MapPlotterError(f"{source} contains a non-object feature.")
    return raw


def _properties(feature: dict[str, Any], *, source: str) -> dict[str, Any]:
    value = feature.get("properties")
    if not isinstance(value, dict):
        raise MapPlotterError(f"{source} feature has no properties object.")
    return value


def _parse_yyyymmdd(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise MapPlotterError(f"{field} must be YYYYMMDD text.")
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise MapPlotterError(f"{field} is not a valid YYYYMMDD date.") from exc


@dataclass(frozen=True, slots=True)
class _SourcePath:
    index: int
    source_object: str
    line: LineString


@dataclass(frozen=True, slots=True)
class _Station:
    code: str
    name: str
    position: tuple[float, float]
    valid_from: str
    ticket_zone: str
    raw_properties: dict[str, Any]

    @property
    def node_id(self) -> str:
        return f"tfgm-stop-{_slug(self.code)}"


@dataclass(frozen=True, slots=True)
class _Cut:
    path_index: int
    distance: float
    position: tuple[float, float]
    kind: str
    station_code: str | None = None


@dataclass(frozen=True, slots=True)
class _GraphEdge:
    id: str
    from_root: int
    to_root: int
    geometry: tuple[tuple[float, float], ...]
    source_object: str
    length_m: float


@dataclass(frozen=True, slots=True)
class _PatternEvidence:
    line_id: str
    route_short_name: str
    origin_code: str
    destination_code: str
    station_codes: tuple[str, ...]
    matching_trip_count: int
    matching_trip_ids: tuple[str, ...]
    representative_trip_id: str
    shape_id: str


@dataclass(frozen=True, slots=True)
class _LocalMetric:
    """Deterministic local equirectangular projection for topology operations."""

    lon0: float
    lat0: float

    def position(self, value: tuple[float, float]) -> tuple[float, float]:
        lon, lat = value
        return (
            _EARTH_RADIUS_M * radians(lon - self.lon0) * cos(radians(self.lat0)),
            _EARTH_RADIUS_M * radians(lat - self.lat0),
        )

    def geographic(self, value: tuple[float, float]) -> tuple[float, float]:
        x, y = value
        longitude_scale = _EARTH_RADIUS_M * cos(radians(self.lat0))
        if longitude_scale <= 0.0:
            raise MapPlotterError("TfGM local metric projection is degenerate.")
        return (
            self.lon0 + degrees(x / longitude_scale),
            self.lat0 + degrees(y / _EARTH_RADIUS_M),
        )

    def line(self, points: Sequence[tuple[float, float]]) -> LineString:
        return LineString([self.position(point) for point in points])


class _UnionFind:
    def __init__(self) -> None:
        self.parents: list[int] = []

    def add(self) -> int:
        value = len(self.parents)
        self.parents.append(value)
        return value

    def find(self, value: int) -> int:
        parent = self.parents[value]
        if parent != value:
            self.parents[value] = self.find(parent)
        return self.parents[value]

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        self.parents[max(first_root, second_root)] = min(first_root, second_root)


def _parse_mapdata(
    payload: bytes,
) -> tuple[list[_SourcePath], list[_Station], dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise MapPlotterError(
            "TfGM map-data source is not a valid ZIP archive."
        ) from exc
    with archive:
        line_document = _zip_json(archive, LINES_MEMBER)
        stop_document = _zip_json(archive, STOPS_MEMBER)
    line_features = _feature_list(
        line_document, expected_name="RailwayLink", source=LINES_MEMBER
    )
    stop_features = _feature_list(
        stop_document, expected_name="RailwayStationNode", source=STOPS_MEMBER
    )

    paths: list[_SourcePath] = []
    raw_vertex_count = 0
    duplicate_vertex_count = 0
    geometry_types: Counter[str] = Counter()
    lineage: set[tuple[str, str]] = set()
    line_feature_audit: list[dict[str, Any]] = []
    for feature_index, feature in enumerate(line_features):
        props = _properties(feature, source=LINES_MEMBER)
        if props.get("type") != "tramway" or props.get("currentStatus") != "functional":
            raise MapPlotterError(
                "TfGM map data contains a non-functional tramway line."
            )
        name = str(props.get("name", "")).strip()
        if not name:
            raise MapPlotterError("TfGM line feature has no name.")
        valid_from_raw = props.get("validFrom")
        valid_from = _parse_yyyymmdd(
            valid_from_raw, field=f"TfGM line {name}.validFrom"
        )
        lineage_key = (name, str(valid_from_raw))
        if lineage_key in lineage:
            raise MapPlotterError(f"TfGM repeats line lineage {lineage_key!r}.")
        lineage.add(lineage_key)
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise MapPlotterError(f"TfGM line {name!r} has no geometry.")
        geometry_type = str(geometry.get("type"))
        geometry_types[geometry_type] += 1
        coordinates_value = geometry.get("coordinates")
        if geometry_type == "LineString":
            raw_parts: list[Any] = [coordinates_value]
        elif geometry_type == "MultiLineString":
            if not isinstance(coordinates_value, list):
                raise MapPlotterError(f"TfGM line {name!r} has empty geometry.")
            raw_parts = list(coordinates_value)
        else:
            raise MapPlotterError(
                f"TfGM line {name!r} uses unsupported {geometry_type!r} geometry."
            )
        if not isinstance(raw_parts, list) or not raw_parts:
            raise MapPlotterError(f"TfGM line {name!r} has empty geometry.")
        source_objects: list[str] = []
        for part_index, raw_part in enumerate(raw_parts):
            if not isinstance(raw_part, list):
                raise MapPlotterError(f"TfGM line {name!r} contains an invalid part.")
            raw_vertex_count += len(raw_part)
            points = _clean_points(raw_part)
            duplicate_vertex_count += len(raw_part) - len(points)
            source_object = f"RailwayLink:{_slug(name)}:{valid_from}:part:{part_index}"
            source_objects.append(source_object)
            paths.append(
                _SourcePath(
                    index=len(paths),
                    source_object=source_object,
                    line=LineString(points),
                )
            )
        line_feature_audit.append(
            {
                "feature_index": feature_index,
                "name": name,
                "valid_from": valid_from,
                "geometry_type": geometry_type,
                "raw_properties": dict(props),
                "source_objects": source_objects,
            }
        )

    stations: list[_Station] = []
    station_codes: set[str] = set()
    station_names: set[str] = set()
    station_positions: set[tuple[float, float]] = set()
    for feature in stop_features:
        props = _properties(feature, source=STOPS_MEMBER)
        if (
            props.get("type") != "railwayStop"
            or props.get("currentStatus") != "functional"
        ):
            raise MapPlotterError("TfGM map data contains a non-functional tram stop.")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            raise MapPlotterError("TfGM stop geometry must be a Point.")
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise MapPlotterError("TfGM stop has invalid coordinates.")
        position = (float(coordinates[0]), float(coordinates[1]))
        code = str(props.get("stationCode", "")).strip()
        name = str(props.get("name", "")).strip()
        zone = str(props.get("ticketZone", "")).strip()
        if not re.fullmatch(r"[A-Z0-9]{3}", code) or not name:
            raise MapPlotterError("TfGM stop has an invalid stationCode or name.")
        if (
            code in station_codes
            or name in station_names
            or position in station_positions
        ):
            raise MapPlotterError(
                "TfGM stop codes, names, and coordinates must be unique."
            )
        station_codes.add(code)
        station_names.add(name)
        station_positions.add(position)
        stations.append(
            _Station(
                code=code,
                name=name,
                position=position,
                valid_from=_parse_yyyymmdd(
                    props.get("validFrom"), field=f"TfGM stop {code}.validFrom"
                ),
                ticket_zone=zone,
                raw_properties=dict(props),
            )
        )

    audit = {
        "line_feature_count": len(line_features),
        "stop_feature_count": len(stop_features),
        "exploded_path_count": len(paths),
        "geometry_type_counts": dict(sorted(geometry_types.items())),
        "raw_vertex_count": raw_vertex_count,
        "removed_consecutive_duplicate_vertex_count": duplicate_vertex_count,
        "line_features": line_feature_audit,
    }
    return paths, stations, audit


def _cut_graph(
    paths: Sequence[_SourcePath],
    stations: Sequence[_Station],
    *,
    maximum_station_snap_m: float,
    maximum_join_snap_m: float,
) -> tuple[
    list[_GraphEdge],
    dict[int, tuple[float, float]],
    dict[int, str],
    dict[str, int],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[int, tuple[str, ...]],
]:
    basis = [
        (float(lon), float(lat)) for path in paths for lon, lat in path.line.coords
    ] + [station.position for station in stations]
    if not basis:
        raise MapPlotterError("TfGM map data has no projection basis.")
    projection = _LocalMetric(
        lon0=sum(point[0] for point in basis) / len(basis),
        lat0=sum(point[1] for point in basis) / len(basis),
    )
    metric_paths = {
        path.index: projection.line(
            [(float(lon), float(lat)) for lon, lat in path.line.coords]
        )
        for path in paths
    }
    cuts: list[_Cut] = []
    cut_ids_by_path: dict[int, list[int]] = defaultdict(list)
    union = _UnionFind()

    def add_cut(cut: _Cut) -> int:
        cut_id = union.add()
        cuts.append(cut)
        cut_ids_by_path[cut.path_index].append(cut_id)
        return cut_id

    endpoint_ids: list[int] = []
    for path in paths:
        metric_line = metric_paths[path.index]
        for distance in (0.0, metric_line.length):
            point = metric_line.interpolate(distance)
            endpoint_ids.append(
                add_cut(
                    _Cut(
                        path_index=path.index,
                        distance=distance,
                        position=(float(point.x), float(point.y)),
                        kind="source-endpoint",
                    )
                )
            )

    station_cut_ids: dict[str, int] = {}
    station_snaps: list[dict[str, Any]] = []
    for station in stations:
        source_metric = projection.position(station.position)
        source_point = Point(source_metric)
        path = min(
            paths,
            key=lambda candidate: source_point.distance(metric_paths[candidate.index]),
        )
        metric_line = metric_paths[path.index]
        distance = metric_line.project(source_point)
        projected = metric_line.interpolate(distance)
        projected_position = (float(projected.x), float(projected.y))
        displacement = source_point.distance(projected)
        if displacement > maximum_station_snap_m:
            raise MapPlotterError(
                f"TfGM stop {station.name!r} is {displacement:.3f} m from the "
                f"functional alignment; maximum is {maximum_station_snap_m:.3f} m."
            )
        station_cut_ids[station.code] = add_cut(
            _Cut(
                path_index=path.index,
                distance=distance,
                position=projected_position,
                kind="station",
                station_code=station.code,
            )
        )
        station_snaps.append(
            {
                "station_code": station.code,
                "station_name": station.name,
                "source_position": list(station.position),
                "graph_position": list(projection.geographic(projected_position)),
                "displacement_m": displacement,
                "source_path": path.source_object,
            }
        )

    # Only source endpoints are allowed to establish connectivity between
    # different paths.  This deliberately avoids treating a bridge-like
    # geometric crossing as a junction when the source has no grade tags.
    junction_contacts: list[dict[str, Any]] = []
    for endpoint_id in endpoint_ids:
        endpoint = cuts[endpoint_id]
        endpoint_point = Point(endpoint.position)
        endpoint_path = paths[endpoint.path_index]
        endpoint_side = "start" if endpoint.distance <= 1e-9 else "end"
        for path in paths:
            if path.index == endpoint.path_index:
                continue
            metric_line = metric_paths[path.index]
            distance = metric_line.project(endpoint_point)
            projected = metric_line.interpolate(distance)
            projected_position = (float(projected.x), float(projected.y))
            displacement = endpoint_point.distance(projected)
            if displacement > maximum_join_snap_m:
                continue
            if distance <= 1e-9 or metric_line.length - distance <= 1e-9:
                target_role = "endpoint"
            else:
                target_role = "interior"
            junction_contacts.append(
                {
                    "endpoint_path": endpoint_path.source_object,
                    "endpoint_side": endpoint_side,
                    "endpoint_position": list(projection.geographic(endpoint.position)),
                    "target_path": path.source_object,
                    "target_role": target_role,
                    "target_position": list(projection.geographic(projected_position)),
                    "distance_m": displacement,
                }
            )
            projected_id = add_cut(
                _Cut(
                    path_index=path.index,
                    distance=distance,
                    position=projected_position,
                    kind="endpoint-to-path",
                )
            )
            union.union(endpoint_id, projected_id)

    # Coincident cuts on the same source path and station/end-point contacts
    # refer to the same graph node.  No other cross-path proximity is noded.
    for path_index, ids in cut_ids_by_path.items():
        ordered = sorted(ids, key=lambda value: cuts[value].distance)
        path = paths[path_index]
        for first_id, second_id in zip(ordered, ordered[1:]):
            along_m = abs(cuts[second_id].distance - cuts[first_id].distance)
            if along_m <= maximum_join_snap_m:
                union.union(first_id, second_id)
        if not ordered or cuts[ordered[0]].distance > 1e-12:
            raise MapPlotterError(f"TfGM path {path.source_object} lost its start cut.")

    root_members: dict[int, list[int]] = defaultdict(list)
    for cut_id in range(len(cuts)):
        root_members[union.find(cut_id)].append(cut_id)
    station_root_by_code = {
        code: union.find(cut_id) for code, cut_id in station_cut_ids.items()
    }
    root_station_code: dict[int, str] = {}
    for code, root in station_root_by_code.items():
        if root in root_station_code and root_station_code[root] != code:
            raise MapPlotterError(
                "Two distinct TfGM stations collapse to one graph node."
            )
        root_station_code[root] = code

    root_positions: dict[int, tuple[float, float]] = {}
    root_metric_positions: dict[int, tuple[float, float]] = {}
    root_source_paths: dict[int, tuple[str, ...]] = {}
    join_snaps: list[dict[str, Any]] = []
    for root, members in sorted(root_members.items()):
        station_members = [
            cut_id for cut_id in members if cuts[cut_id].station_code is not None
        ]
        chosen_id = min(station_members or members)
        canonical = cuts[chosen_id].position
        root_metric_positions[root] = canonical
        root_positions[root] = projection.geographic(canonical)
        root_source_paths[root] = tuple(
            sorted({paths[cuts[cut_id].path_index].source_object for cut_id in members})
        )
        for cut_id in members:
            displacement = hypot(
                cuts[cut_id].position[0] - canonical[0],
                cuts[cut_id].position[1] - canonical[1],
            )
            if displacement > maximum_join_snap_m + 1e-6:
                raise MapPlotterError(
                    "TfGM topology clustering exceeded the join tolerance."
                )
            if displacement > 1e-6:
                join_snaps.append(
                    {
                        "source_position": list(
                            projection.geographic(cuts[cut_id].position)
                        ),
                        "graph_position": list(projection.geographic(canonical)),
                        "displacement_m": displacement,
                        "kind": cuts[cut_id].kind,
                        "source_path": paths[cuts[cut_id].path_index].source_object,
                    }
                )

    graph_edges: list[_GraphEdge] = []
    edge_ids: set[str] = set()
    for path in paths:
        metric_line = metric_paths[path.index]
        root_distances: list[tuple[float, int]] = []
        for cut_id in sorted(
            cut_ids_by_path[path.index], key=lambda value: cuts[value].distance
        ):
            root = union.find(cut_id)
            distance = cuts[cut_id].distance
            if root_distances and root_distances[-1][1] == root:
                continue
            root_distances.append((distance, root))
        for part_index, ((start, from_root), (end, to_root)) in enumerate(
            zip(root_distances, root_distances[1:])
        ):
            if from_root == to_root or end - start <= 1e-12:
                continue
            segment = substring(metric_line, start, end)
            if not isinstance(segment, LineString):
                raise MapPlotterError("TfGM source split did not produce a LineString.")
            metric_points = [(float(x), float(y)) for x, y in segment.coords]
            metric_points[0] = root_metric_positions[from_root]
            metric_points[-1] = root_metric_positions[to_root]
            deduplicated_metric: list[tuple[float, float]] = []
            for coordinate in metric_points:
                if not deduplicated_metric or coordinate != deduplicated_metric[-1]:
                    deduplicated_metric.append(coordinate)
            if len(deduplicated_metric) < 2:
                continue
            length_m = LineString(deduplicated_metric).length
            if length_m <= 0.01:
                continue
            geometry = tuple(
                projection.geographic(point) for point in deduplicated_metric
            )
            digest = hashlib.sha256(
                _canonical_json(
                    {
                        "source": path.source_object,
                        "part": part_index,
                        "geometry": geometry,
                    }
                )
            ).hexdigest()[:16]
            edge_id = f"tfgm-edge-{digest}"
            if edge_id in edge_ids:
                raise MapPlotterError("TfGM graph produced a duplicate edge identity.")
            edge_ids.add(edge_id)
            graph_edges.append(
                _GraphEdge(
                    id=edge_id,
                    from_root=from_root,
                    to_root=to_root,
                    geometry=geometry,
                    source_object=path.source_object,
                    length_m=length_m,
                )
            )
    return (
        graph_edges,
        root_positions,
        root_station_code,
        station_root_by_code,
        station_snaps,
        join_snaps,
        sorted(
            junction_contacts,
            key=lambda value: (
                value["endpoint_path"],
                value["endpoint_side"],
                value["target_path"],
            ),
        ),
        root_source_paths,
    )


def _csv_rows(archive: zipfile.ZipFile, member: str) -> Iterable[dict[str, str]]:
    try:
        stream = archive.open(member)
    except KeyError as exc:
        raise MapPlotterError(f"TfGM GTFS archive is missing {member}.") from exc
    with stream, io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
        yield from csv.DictReader(text)


def _active_services(archive: zipfile.ZipFile, service_date: date) -> set[str]:
    compact = service_date.strftime("%Y%m%d")
    weekday = service_date.strftime("%A").casefold()
    active: set[str] = set()
    if "calendar.txt" in archive.namelist():
        for row in _csv_rows(archive, "calendar.txt"):
            if (
                row.get(weekday) == "1"
                and str(row.get("start_date", "")) <= compact
                and compact <= str(row.get("end_date", ""))
            ):
                active.add(str(row.get("service_id", "")))
    if "calendar_dates.txt" in archive.namelist():
        for row in _csv_rows(archive, "calendar_dates.txt"):
            if row.get("date") != compact:
                continue
            service_id = str(row.get("service_id", ""))
            if row.get("exception_type") == "1":
                active.add(service_id)
            elif row.get("exception_type") == "2":
                active.discard(service_id)
    if not active:
        raise MapPlotterError(f"TfGM GTFS has no active service on {service_date}.")
    return active


def _parse_gtfs_patterns(
    payload: bytes,
    *,
    service_date: date,
    agency_id: str,
    line_records: Sequence[dict[str, Any]],
    stations: Sequence[_Station],
) -> tuple[
    list[_PatternEvidence], dict[str, list[tuple[float, float]]], dict[str, Any]
]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise MapPlotterError("TfGM GTFS source is not a valid ZIP archive.") from exc
    with archive:
        active = _active_services(archive, service_date)
        required_short_names = {
            str(record.get("gtfs_route_short_name", "")) for record in line_records
        }
        if "" in required_short_names:
            raise MapPlotterError(
                "Manchester catalog lines need gtfs_route_short_name."
            )
        routes: dict[str, dict[str, str]] = {}
        for row in _csv_rows(archive, "routes.txt"):
            if (
                row.get("agency_id") == agency_id
                and row.get("route_short_name") in required_short_names
                and row.get("route_type") == "0"
            ):
                routes[str(row["route_id"])] = row
        missing_route_names = required_short_names - {
            row["route_short_name"] for row in routes.values()
        }
        if missing_route_names:
            raise MapPlotterError(
                "TfGM GTFS is missing Metrolink routes: "
                + ", ".join(sorted(missing_route_names))
            )
        trips: dict[str, dict[str, str]] = {}
        for row in _csv_rows(archive, "trips.txt"):
            if row.get("route_id") in routes and row.get("service_id") in active:
                trips[str(row["trip_id"])] = row
        if not trips:
            raise MapPlotterError("TfGM GTFS has no active Metrolink trips.")

        stop_name_by_id = {
            str(row["stop_id"]): str(row.get("stop_name", ""))
            for row in _csv_rows(archive, "stops.txt")
        }
        official_by_key = {_name_key(station.name): station for station in stations}
        if len(official_by_key) != len(stations):
            raise MapPlotterError(
                "TfGM official station names do not normalize uniquely."
            )
        stop_times: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in _csv_rows(archive, "stop_times.txt"):
            trip_id = str(row.get("trip_id", ""))
            if trip_id not in trips:
                continue
            stop_id = str(row.get("stop_id", ""))
            if stop_id not in stop_name_by_id:
                raise MapPlotterError(
                    f"TfGM GTFS trip {trip_id} names missing stop {stop_id}."
                )
            key = _name_key(stop_name_by_id[stop_id])
            if key not in official_by_key:
                raise MapPlotterError(
                    f"TfGM GTFS stop {stop_name_by_id[stop_id]!r} has no official map-data match."
                )
            try:
                sequence = int(str(row.get("stop_sequence", "")))
            except ValueError as exc:
                raise MapPlotterError(
                    "TfGM GTFS has an invalid stop_sequence."
                ) from exc
            stop_times[trip_id].append((sequence, official_by_key[key].code))

        trip_sequences: dict[str, tuple[str, ...]] = {}
        for trip_id, visits in stop_times.items():
            result: list[str] = []
            for _, code in sorted(visits):
                if not result or result[-1] != code:
                    result.append(code)
            if len(result) >= 2:
                trip_sequences[trip_id] = tuple(result)

        station_by_key = {_name_key(station.name): station for station in stations}
        evidence: list[_PatternEvidence] = []
        for line in line_records:
            line_id = str(line.get("id", ""))
            route_short_name = str(line.get("gtfs_route_short_name", ""))
            terminal_names = line.get("gtfs_terminal_names")
            if (
                not isinstance(terminal_names, list)
                or len(terminal_names) != 2
                or not all(isinstance(value, str) for value in terminal_names)
            ):
                raise MapPlotterError(
                    f"Manchester catalog line {line_id!r} needs two gtfs_terminal_names."
                )
            try:
                terminal_codes = tuple(
                    station_by_key[_name_key(str(value))].code
                    for value in terminal_names
                )
            except KeyError as exc:
                raise MapPlotterError(
                    f"Manchester catalog line {line_id!r} names a missing terminal."
                ) from exc
            for origin, destination in (
                terminal_codes,
                tuple(reversed(terminal_codes)),
            ):
                matching: dict[tuple[str, ...], list[str]] = defaultdict(list)
                for trip_id, trip_sequence in trip_sequences.items():
                    route = routes[trips[trip_id]["route_id"]]
                    if (
                        route["route_short_name"] == route_short_name
                        and trip_sequence[0] == origin
                        and trip_sequence[-1] == destination
                    ):
                        matching[trip_sequence].append(trip_id)
                if not matching:
                    raise MapPlotterError(
                        f"TfGM GTFS has no {route_short_name} trip from {origin} to {destination}."
                    )
                if len(matching) != 1:
                    variants = ", ".join(
                        f"{len(sequence)} stops/{len(ids)} trips"
                        for sequence, ids in sorted(matching.items())
                    )
                    raise MapPlotterError(
                        f"TfGM GTFS has ambiguous full-length {line_id} sequences: {variants}."
                    )
                station_codes, trip_ids = next(iter(matching.items()))
                matching_trip_ids = tuple(sorted(trip_ids))
                representative = matching_trip_ids[0]
                shape_ids = {
                    str(trips[trip_id].get("shape_id", ""))
                    for trip_id in matching_trip_ids
                }
                if "" in shape_ids:
                    raise MapPlotterError(
                        f"TfGM GTFS trip group for {line_id} has a missing shape_id."
                    )
                if len(shape_ids) != 1:
                    raise MapPlotterError(
                        f"TfGM GTFS trips sharing the selected {line_id} stop sequence "
                        "use multiple shapes: " + ", ".join(sorted(shape_ids))
                    )
                shape_id = next(iter(shape_ids))
                evidence.append(
                    _PatternEvidence(
                        line_id=line_id,
                        route_short_name=route_short_name,
                        origin_code=origin,
                        destination_code=destination,
                        station_codes=station_codes,
                        matching_trip_count=len(trip_ids),
                        matching_trip_ids=matching_trip_ids,
                        representative_trip_id=representative,
                        shape_id=shape_id,
                    )
                )

        required_shapes = {item.shape_id for item in evidence}
        shape_rows: dict[str, list[tuple[int, tuple[float, float]]]] = defaultdict(list)
        for row in _csv_rows(archive, "shapes.txt"):
            shape_id = str(row.get("shape_id", ""))
            if shape_id not in required_shapes:
                continue
            try:
                point = (
                    float(str(row.get("shape_pt_lon", ""))),
                    float(str(row.get("shape_pt_lat", ""))),
                )
                sequence = int(str(row.get("shape_pt_sequence", "")))
            except ValueError as exc:
                raise MapPlotterError(
                    "TfGM GTFS has invalid shape coordinates."
                ) from exc
            shape_rows[shape_id].append((sequence, point))
        shapes = {
            shape_id: [point for _, point in sorted(values)]
            for shape_id, values in shape_rows.items()
        }
        missing_shapes = required_shapes - set(shapes)
        if missing_shapes:
            raise MapPlotterError(
                "TfGM GTFS is missing shapes: " + ", ".join(sorted(missing_shapes))
            )
    return (
        evidence,
        shapes,
        {
            "service_date": service_date.isoformat(),
            "active_service_id_count": len(active),
            "active_metrolink_trip_count": len(trips),
            "directional_pattern_count": len(evidence),
        },
    )


def _shortest_path(
    origin: int,
    destination: int,
    edges: Sequence[_GraphEdge],
    *,
    allowed_edges: set[int] | None = None,
    edge_weights: dict[int, float] | None = None,
) -> tuple[list[tuple[int, str]], list[int]]:
    adjacency: dict[int, list[tuple[int, float, int, str]]] = defaultdict(list)
    for edge_index, edge in enumerate(edges):
        if allowed_edges is not None and edge_index not in allowed_edges:
            continue
        weight = (
            edge_weights[edge_index]
            if edge_weights is not None and edge_index in edge_weights
            else edge.length_m
        )
        if weight <= 0.0:
            raise MapPlotterError("TfGM graph routing weight must be positive.")
        adjacency[edge.from_root].append((edge.to_root, weight, edge_index, "forward"))
        adjacency[edge.to_root].append((edge.from_root, weight, edge_index, "reverse"))
    for values in adjacency.values():
        values.sort(key=lambda value: (value[0], edges[value[2]].id, value[3]))
    distances = {origin: 0.0}
    previous: dict[int, tuple[int, int, str]] = {}
    queue: list[tuple[float, int]] = [(0.0, origin)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        if node == destination:
            break
        for neighbour, weight, edge_index, direction in adjacency.get(node, []):
            candidate = distance + weight
            if candidate + 1e-9 < distances.get(neighbour, float("inf")):
                distances[neighbour] = candidate
                previous[neighbour] = (node, edge_index, direction)
                heapq.heappush(queue, (candidate, neighbour))
    if destination not in distances:
        raise MapPlotterError(
            "TfGM service stop pair is disconnected in map-data geometry."
        )
    traversals: list[tuple[int, str]] = []
    nodes = [destination]
    current = destination
    while current != origin:
        prior, edge_index, direction = previous[current]
        traversals.append((edge_index, direction))
        nodes.append(prior)
        current = prior
    traversals.reverse()
    nodes.reverse()
    return traversals, nodes


def _oriented_geometry(
    traversals: Sequence[tuple[int, str]], edges: Sequence[_GraphEdge]
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for edge_index, direction in traversals:
        points = list(edges[edge_index].geometry)
        if direction == "reverse":
            points.reverse()
        if result and result[-1] == points[0]:
            result.extend(points[1:])
        else:
            result.extend(points)
    return result


def _sampled_reference_distances(
    line: LineString,
    reference: LineString,
) -> tuple[float, float]:
    """Return maximum and mean directed line-to-reference distances."""

    distances = [
        line.interpolate(line.length * fraction).distance(reference)
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    return max(distances), sum(distances) / len(distances)


def _shape_segment(
    shape_line: LineString,
    start_distance: float,
    end_distance: float,
    *,
    origin_code: str,
    destination_code: str,
) -> LineString:
    if end_distance <= start_distance + 0.01:
        raise MapPlotterError(
            f"TfGM GTFS shape is not monotonic from {origin_code} to "
            f"{destination_code}."
        )
    result = substring(shape_line, start_distance, end_distance)
    if not isinstance(result, LineString) or result.length <= 0.01:
        raise MapPlotterError(
            f"TfGM GTFS shape segment {origin_code} to {destination_code} "
            "is degenerate."
        )
    return result


def compile_tfgm_contract(
    catalog: dict[str, Any],
    *,
    source_payloads: dict[str, bytes],
    service_date: date,
    retrieved_at: date,
    output_path: Path,
    audit_path: Path | None = None,
    source_retrieved_at: dict[str, date] | None = None,
) -> dict[str, Any]:
    """Compile frozen TfGM inputs and write a normalized contract plus audit."""

    acquisition = catalog.get("acquisition")
    if not isinstance(acquisition, dict):
        raise MapPlotterError("Manchester catalog acquisition must be an object.")
    map_source_ref = str(acquisition.get("mapdata_source_ref", ""))
    gtfs_source_ref = str(acquisition.get("gtfs_source_ref", ""))
    if map_source_ref not in source_payloads or gtfs_source_ref not in source_payloads:
        raise MapPlotterError("Manchester compiler is missing map-data or GTFS bytes.")
    source_catalog = catalog.get("sources")
    if not isinstance(source_catalog, list):
        raise MapPlotterError("Manchester catalog sources must be a list.")
    for raw_source in source_catalog:
        if not isinstance(raw_source, dict):
            raise MapPlotterError("Manchester catalog source must be an object.")
        source_id = str(raw_source.get("id", ""))
        if source_id not in source_payloads:
            raise MapPlotterError(
                f"Manchester source {source_id!r} has no frozen bytes."
            )
        actual_sha256 = hashlib.sha256(source_payloads[source_id]).hexdigest()
        expected_sha256 = raw_source.get("expected_sha256")
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise MapPlotterError(
                f"Manchester source {source_id!r} SHA-256 changed: expected "
                f"{expected_sha256}, got {actual_sha256}. Promote reviewed bytes "
                "explicitly before compiling."
            )
    line_records_raw = catalog.get("lines")
    if not isinstance(line_records_raw, list) or not all(
        isinstance(value, dict) for value in line_records_raw
    ):
        raise MapPlotterError("Manchester catalog lines must be objects.")
    line_records = list(line_records_raw)
    maximum_station_snap_m = float(
        acquisition.get("maximum_station_snap_m", DEFAULT_MAX_STATION_SNAP_M)
    )
    maximum_join_snap_m = float(
        acquisition.get("maximum_join_snap_m", DEFAULT_MAX_JOIN_SNAP_M)
    )
    maximum_shape_hausdorff_m = float(
        acquisition.get("maximum_shape_hausdorff_m", DEFAULT_MAX_SHAPE_HAUSDORFF_M)
    )
    maximum_segment_shape_hausdorff_m = float(
        acquisition.get(
            "maximum_segment_shape_hausdorff_m",
            DEFAULT_MAX_SEGMENT_SHAPE_HAUSDORFF_M,
        )
    )
    if maximum_segment_shape_hausdorff_m <= 0.0:
        raise MapPlotterError(
            "Manchester maximum_segment_shape_hausdorff_m must be positive."
        )
    raw_segment_overrides = acquisition.get("segment_shape_tolerance_overrides", [])
    if not isinstance(raw_segment_overrides, list):
        raise MapPlotterError(
            "Manchester segment_shape_tolerance_overrides must be a list."
        )
    segment_overrides: dict[tuple[str, str], tuple[float, str]] = {}
    for raw_override in raw_segment_overrides:
        if not isinstance(raw_override, dict):
            raise MapPlotterError(
                "Manchester segment tolerance override must be an object."
            )
        key = (
            str(raw_override.get("origin_code", "")),
            str(raw_override.get("destination_code", "")),
        )
        limit = float(raw_override.get("maximum_hausdorff_m", 0.0))
        reason = str(raw_override.get("reason", "")).strip()
        if (
            not all(re.fullmatch(r"[A-Z0-9]{3}", code) for code in key)
            or key in segment_overrides
            or limit <= maximum_segment_shape_hausdorff_m
            or not reason
        ):
            raise MapPlotterError(
                "Manchester segment tolerance override is invalid or duplicated."
            )
        segment_overrides[key] = (limit, reason)
    used_segment_overrides: set[tuple[str, str]] = set()
    paths, stations, map_audit = _parse_mapdata(source_payloads[map_source_ref])
    qa = catalog.get("qa")
    if not isinstance(qa, dict):
        raise MapPlotterError("Manchester catalog qa must be an object.")
    minimum_stations = int(qa.get("minimum_station_count", 1))
    maximum_stations = int(qa.get("maximum_station_count", 10_000))
    if not minimum_stations <= len(stations) <= maximum_stations:
        raise MapPlotterError(
            f"Manchester map data has {len(stations)} stops; QA requires "
            f"{minimum_stations}..{maximum_stations}."
        )
    (
        graph_edges,
        root_positions,
        root_station_code,
        station_root_by_code,
        station_snaps,
        join_snaps,
        junction_contacts,
        root_source_paths,
    ) = _cut_graph(
        paths,
        stations,
        maximum_station_snap_m=maximum_station_snap_m,
        maximum_join_snap_m=maximum_join_snap_m,
    )
    contact_sha256 = hashlib.sha256(_canonical_json(junction_contacts)).hexdigest()
    expected_contact_count = acquisition.get("expected_endpoint_contact_count")
    if expected_contact_count is not None and len(junction_contacts) != int(
        expected_contact_count
    ):
        raise MapPlotterError(
            "TfGM source endpoint-contact topology changed: expected "
            f"{expected_contact_count}, got {len(junction_contacts)}."
        )
    expected_contact_sha256 = acquisition.get("expected_endpoint_contact_sha256")
    if (
        expected_contact_sha256 is not None
        and contact_sha256 != expected_contact_sha256
    ):
        raise MapPlotterError(
            "TfGM source endpoint-contact ledger changed; explicit review is required."
        )
    evidence, shapes, gtfs_audit = _parse_gtfs_patterns(
        source_payloads[gtfs_source_ref],
        service_date=service_date,
        agency_id=str(acquisition.get("gtfs_agency_id", "")),
        line_records=line_records,
        stations=stations,
    )
    minimum_patterns = int(qa.get("minimum_service_pattern_count", len(line_records)))
    maximum_patterns = int(
        qa.get("maximum_service_pattern_count", len(line_records) * 2)
    )
    if not minimum_patterns <= len(evidence) <= maximum_patterns:
        raise MapPlotterError(
            f"Manchester GTFS produced {len(evidence)} directional patterns; QA "
            f"requires {minimum_patterns}..{maximum_patterns}."
        )

    station_by_code = {station.code: station for station in stations}
    memberships: dict[int, set[str]] = defaultdict(set)
    line_station_codes: dict[str, set[str]] = defaultdict(set)
    pattern_records: list[dict[str, Any]] = []
    pattern_audit: list[dict[str, Any]] = []
    all_traversed_edges: set[int] = set()
    for item in evidence:
        shape_geometry = shapes[item.shape_id]
        shape_projection = _LocalMetric(
            lon0=sum(point[0] for point in shape_geometry) / len(shape_geometry),
            lat0=sum(point[1] for point in shape_geometry) / len(shape_geometry),
        )
        shape_line = shape_projection.line(shape_geometry)
        metric_graph_edges = [
            shape_projection.line(edge.geometry) for edge in graph_edges
        ]
        shape_distances = [
            shape_line.project(
                Point(shape_projection.position(station_by_code[code].position))
            )
            for code in item.station_codes
        ]
        for origin_code, destination_code, start_distance, end_distance in zip(
            item.station_codes,
            item.station_codes[1:],
            shape_distances,
            shape_distances[1:],
        ):
            if end_distance <= start_distance + 0.01:
                raise MapPlotterError(
                    f"TfGM GTFS shape {item.shape_id} is not monotonic between "
                    f"{origin_code} and {destination_code}."
                )

        traversals: list[tuple[int, str]] = []
        segment_audit: list[dict[str, Any]] = []
        for origin_code, destination_code, start_distance, end_distance in zip(
            item.station_codes,
            item.station_codes[1:],
            shape_distances,
            shape_distances[1:],
        ):
            reference_segment = _shape_segment(
                shape_line,
                start_distance,
                end_distance,
                origin_code=origin_code,
                destination_code=destination_code,
            )
            override = segment_overrides.get((origin_code, destination_code))
            segment_limit = (
                override[0]
                if override is not None
                else maximum_segment_shape_hausdorff_m
            )
            if override is not None:
                used_segment_overrides.add((origin_code, destination_code))
            allowed_edges: set[int] = set()
            edge_weights: dict[int, float] = {}
            min_x, min_y, max_x, max_y = reference_segment.bounds
            limit = segment_limit
            for edge_index, (edge, metric_edge) in enumerate(
                zip(graph_edges, metric_graph_edges)
            ):
                edge_min_x, edge_min_y, edge_max_x, edge_max_y = metric_edge.bounds
                if (
                    edge_max_x < min_x - limit
                    or edge_min_x > max_x + limit
                    or edge_max_y < min_y - limit
                    or edge_min_y > max_y + limit
                ):
                    continue
                maximum_distance, mean_distance = _sampled_reference_distances(
                    metric_edge,
                    reference_segment,
                )
                if maximum_distance > segment_limit:
                    continue
                allowed_edges.add(edge_index)
                # Proximity dominates small length differences at parallel
                # alignments; the final symmetric Hausdorff gate below remains
                # authoritative.
                edge_weights[edge_index] = edge.length_m * (1.0 + mean_distance)
            try:
                piece, node_roots = _shortest_path(
                    station_root_by_code[origin_code],
                    station_root_by_code[destination_code],
                    graph_edges,
                    allowed_edges=allowed_edges,
                    edge_weights=edge_weights,
                )
            except MapPlotterError as exc:
                raise MapPlotterError(
                    f"TfGM service stop pair {origin_code} to {destination_code} "
                    f"is disconnected inside the {segment_limit:.1f} m GTFS-shape "
                    "corridor."
                ) from exc
            unexpected_stations = [
                root_station_code[root]
                for root in node_roots[1:-1]
                if root in root_station_code
            ]
            if unexpected_stations:
                raise MapPlotterError(
                    f"TfGM map match from {origin_code} to {destination_code} "
                    "passes undeclared stops: " + ", ".join(unexpected_stations)
                )
            piece_geometry = _oriented_geometry(piece, graph_edges)
            metric_piece = shape_projection.line(piece_geometry)
            segment_hausdorff_m = metric_piece.hausdorff_distance(reference_segment)
            if segment_hausdorff_m > segment_limit:
                raise MapPlotterError(
                    f"TfGM {item.line_id} map match from {origin_code} to "
                    f"{destination_code} differs from its GTFS shape segment by "
                    f"{segment_hausdorff_m:.1f} m; maximum is "
                    f"{segment_limit:.1f} m."
                )
            segment_audit.append(
                {
                    "origin_code": origin_code,
                    "destination_code": destination_code,
                    "shape_start_distance_m": start_distance,
                    "shape_end_distance_m": end_distance,
                    "graph_length_m": sum(
                        graph_edges[edge_index].length_m for edge_index, _ in piece
                    ),
                    "gtfs_shape_length_m": reference_segment.length,
                    "map_match_hausdorff_m": segment_hausdorff_m,
                    "maximum_allowed_hausdorff_m": segment_limit,
                    "tolerance_override_reason": (
                        override[1] if override is not None else None
                    ),
                    "edge_ids": [graph_edges[edge_index].id for edge_index, _ in piece],
                }
            )
            traversals.extend(piece)
        if not traversals:
            raise MapPlotterError(f"TfGM service {item.line_id} has no traversals.")
        for edge_index, _ in traversals:
            memberships[edge_index].add(item.line_id)
            all_traversed_edges.add(edge_index)
        line_station_codes[item.line_id].update(item.station_codes)
        route_geometry = _oriented_geometry(traversals, graph_edges)
        compared_shape = _shape_segment(
            shape_line,
            shape_distances[0],
            shape_distances[-1],
            origin_code=item.origin_code,
            destination_code=item.destination_code,
        )
        hausdorff_m = shape_projection.line(route_geometry).hausdorff_distance(
            compared_shape
        )
        if hausdorff_m > maximum_shape_hausdorff_m:
            raise MapPlotterError(
                f"TfGM {item.line_id} map match differs from its GTFS shape by "
                f"{hausdorff_m:.1f} m; maximum is {maximum_shape_hausdorff_m:.1f} m."
            )
        origin_name = station_by_code[item.origin_code].name
        destination_name = station_by_code[item.destination_code].name
        pattern_id = (
            f"tfgm-{item.line_id}-{_slug(item.origin_code)}-to-"
            f"{_slug(item.destination_code)}"
        )
        pattern_records.append(
            {
                "id": pattern_id,
                "line_id": item.line_id,
                "name": f"{origin_name} → {destination_name}",
                "traversals": [
                    {
                        "edge_id": graph_edges[edge_index].id,
                        "direction": direction,
                    }
                    for edge_index, direction in traversals
                ],
                "station_ids": [
                    station_by_code[code].node_id for code in item.station_codes
                ],
                "source_ref": gtfs_source_ref,
                "valid_from": service_date.isoformat(),
                "valid_to": service_date.isoformat(),
                "derivation_status": (
                    "official-gtfs-stop-order-map-matched-to-official-tfgm-geometry"
                ),
                "continuity_breaks": [],
            }
        )
        pattern_audit.append(
            {
                "pattern_id": pattern_id,
                "line_id": item.line_id,
                "route_short_name": item.route_short_name,
                "origin": origin_name,
                "destination": destination_name,
                "station_count": len(item.station_codes),
                "matching_active_trip_count": item.matching_trip_count,
                "matching_trip_ids": list(item.matching_trip_ids),
                "representative_trip_id": item.representative_trip_id,
                "shape_id": item.shape_id,
                "map_match_hausdorff_m": hausdorff_m,
                "segment_shape_validation": segment_audit,
            }
        )

    unused_segment_overrides = sorted(set(segment_overrides) - used_segment_overrides)
    if unused_segment_overrides:
        raise MapPlotterError(
            "Manchester segment tolerance overrides did not match a dated GTFS "
            "stop pair: "
            + ", ".join(
                f"{origin}->{destination}"
                for origin, destination in unused_segment_overrides
            )
        )

    visited_stations = {code for codes in line_station_codes.values() for code in codes}
    missing_stations = sorted(set(station_by_code) - visited_stations)
    if missing_stations:
        raise MapPlotterError(
            "TfGM dated passenger-service patterns do not visit all official stops: "
            + ", ".join(missing_stations)
        )
    required_terminals = {
        _name_key(str(value)) for value in qa.get("required_terminal_names", [])
    }
    found_terminals = {
        _name_key(station_by_code[item.origin_code].name) for item in evidence
    } | {_name_key(station_by_code[item.destination_code].name) for item in evidence}
    missing_terminals = sorted(required_terminals - found_terminals)
    if missing_terminals:
        raise MapPlotterError(
            "TfGM patterns are missing required terminals: "
            + ", ".join(missing_terminals)
        )

    line_order = {str(record["id"]): int(record["order"]) for record in line_records}
    used_roots = {
        root
        for edge_index in all_traversed_edges
        for root in (graph_edges[edge_index].from_root, graph_edges[edge_index].to_root)
    }
    incident_used_edges: dict[int, list[int]] = defaultdict(list)
    for edge_index in all_traversed_edges:
        edge = graph_edges[edge_index]
        incident_used_edges[edge.from_root].append(edge_index)
        incident_used_edges[edge.to_root].append(edge_index)
    terminal_codes = {item.origin_code for item in evidence} | {
        item.destination_code for item in evidence
    }
    node_records: list[dict[str, Any]] = []
    root_node_ids: dict[int, str] = {}
    for root in sorted(used_roots, key=lambda value: root_positions[value]):
        position = root_positions[root]
        if root in root_station_code:
            code = root_station_code[root]
            station = station_by_code[code]
            service_lines = sorted(
                (
                    line_id
                    for line_id, codes in line_station_codes.items()
                    if code in codes
                ),
                key=lambda value: (line_order[value], value),
            )
            if code in terminal_codes:
                kind = "terminal"
                tier = "terminal"
            elif len(service_lines) > 1 and (
                len(incident_used_edges[root]) != 2
                or len(
                    {
                        frozenset(memberships[edge_index])
                        for edge_index in incident_used_edges[root]
                    }
                )
                > 1
            ):
                kind = "interchange"
                tier = "interchange"
            else:
                kind = "station"
                tier = "local"
            node_id = station.node_id
            node_records.append(
                {
                    "id": node_id,
                    "kind": kind,
                    "position": list(position),
                    "name": station.name,
                    "station_tier": tier,
                    "source_ref": map_source_ref,
                    "source_object": f"RailwayStationNode:{station.code}",
                }
            )
        else:
            digest = hashlib.sha256(
                f"{position[0]:.9f},{position[1]:.9f}".encode("ascii")
            ).hexdigest()[:16]
            node_id = f"tfgm-junction-{digest}"
            node_records.append(
                {
                    "id": node_id,
                    "kind": "junction",
                    "position": list(position),
                    "source_ref": map_source_ref,
                    "source_object": (
                        "derived-source-endpoint-contact:"
                        + "|".join(root_source_paths[root])
                    ),
                }
            )
        root_node_ids[root] = node_id

    edge_records: list[dict[str, Any]] = []
    for edge_index in sorted(
        all_traversed_edges, key=lambda value: graph_edges[value].id
    ):
        edge = graph_edges[edge_index]
        edge_records.append(
            {
                "id": edge.id,
                "from_node": root_node_ids[edge.from_root],
                "to_node": root_node_ids[edge.to_root],
                "geometry": [list(point) for point in edge.geometry],
                "line_ids": sorted(
                    memberships[edge_index],
                    key=lambda value: (line_order[value], value),
                ),
                "source_ref": map_source_ref,
                "source_object": edge.source_object,
                "status": "operational",
                "grade": "unknown-not-supplied-by-tfgm-mapdata",
            }
        )

    sources: list[dict[str, Any]] = []
    for raw_source in source_catalog:
        assert isinstance(raw_source, dict)
        source_id = str(raw_source.get("id", ""))
        source_date = (source_retrieved_at or {}).get(source_id, retrieved_at)
        sources.append(
            {
                "id": source_id,
                "publisher": raw_source["publisher"],
                "url": raw_source["url"],
                "licence": raw_source["licence"],
                "attribution": raw_source["attribution"],
                "retrieved_at": source_date.isoformat(),
                "sha256": hashlib.sha256(source_payloads[source_id]).hexdigest(),
                "use": raw_source["use"],
                "commercial_reuse_status": raw_source["commercial_reuse_status"],
            }
        )
    lines = [
        {
            "id": record["id"],
            "name": record["name"],
            "short_name": record["short_name"],
            "order": record["order"],
            "colour": record["colour"],
            "pen": record["pen"],
            "service_class": record.get("service_class", "normal-service"),
            "source_ref": record["source_ref"],
        }
        for record in line_records
    ]
    unused_edges = [
        edge
        for index, edge in enumerate(graph_edges)
        if index not in all_traversed_edges
    ]
    document = {
        "schema_version": 1,
        "network": {
            "id": catalog["id"],
            "name": catalog["name"],
            "kind": catalog["kind"],
            "scope": catalog["scope"],
            "format_id": catalog["format_id"],
            "snapshot": service_date.isoformat(),
            "validity_status": "official-dated-passenger-service-review",
            "geometry_mode": "geographic-tfgm-mapdata-plus-gtfs-stop-order",
        },
        "sources": sources,
        "lines": lines,
        "nodes": node_records,
        "edges": edge_records,
        "service_patterns": sorted(pattern_records, key=lambda value: value["id"]),
        "context": [],
        "omissions": [
            {
                "kind": "context",
                "status": "not-supplied",
                "reason": "Add a separately pinned Overpass/PBF context extract before final rendering.",
            },
            {
                "kind": "unserved-functional-alignment",
                "status": "excluded-from-passenger-service",
                "reason": (
                    f"{len(unused_edges)} source graph edges ({sum(edge.length_m for edge in unused_edges):.1f} m) "
                    "are not traversed by the eight dated passenger-service groups."
                ),
            },
            {
                "kind": "physical-grade",
                "status": "unknown-source",
                "reason": (
                    "TfGM map data supplies no bridge/tunnel/layer grade field; only source "
                    "endpoint contacts, never arbitrary geometric crossings, establish junctions."
                ),
            },
        ],
        "notes": [
            "TfGM operator diagrams were used for service identity and colour sampling only; their geometry, typography, logos, and layout were not copied.",
            "The generic #f0b400 stroke embedded in the GIS archive is not a service colour and was ignored.",
            f"GTFS ordered stops are frozen to dated passenger service on {service_date.isoformat()} and map-matched one adjacent stop pair at a time.",
            f"Maximum stop-to-alignment snap: {max(item['displacement_m'] for item in station_snaps):.3f} m.",
            f"Maximum emitted-path to GTFS-shape Hausdorff distance: {max(item['map_match_hausdorff_m'] for item in pattern_audit):.1f} m.",
            f"Maximum adjacent-stop map-match Hausdorff distance: {max(segment['map_match_hausdorff_m'] for item in pattern_audit for segment in item['segment_shape_validation']):.1f} m.",
            "Raw line and station properties, all accepted endpoint contacts, non-zero snaps, matching trip IDs, per-segment shape checks, and complete discarded-edge geometry are retained in the adjacent audit sidecar.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_bytes(_canonical_json(document) + b"\n")
    temporary.replace(output_path)
    network = load_transit_network(output_path)
    if json.loads(canonical_contract_bytes(network)) != json.loads(
        output_path.read_bytes()
    ):
        raise MapPlotterError(
            "Manchester transit contract is not canonically reproducible."
        )

    resolved_audit_path = audit_path or output_path.with_suffix(".audit.json")
    audit = {
        "schema_version": 1,
        "network_id": network.id,
        "service_date": service_date.isoformat(),
        "source_sha256": {
            source_id: hashlib.sha256(payload).hexdigest()
            for source_id, payload in sorted(source_payloads.items())
        },
        "map_data": map_audit,
        "gtfs": gtfs_audit,
        "station_snaps": station_snaps,
        "junction_snaps": join_snaps,
        "junction_contacts": junction_contacts,
        "junction_contact_sha256": contact_sha256,
        "stations": [
            {
                "station_code": station.code,
                "name": station.name,
                "source_position": list(station.position),
                "valid_from": station.valid_from,
                "ticket_zone": station.ticket_zone,
                "raw_properties": station.raw_properties,
            }
            for station in stations
        ],
        "patterns": sorted(pattern_audit, key=lambda value: value["pattern_id"]),
        "unused_physical_edges": [
            {
                "edge_id": edge.id,
                "source_object": edge.source_object,
                "from_position": list(root_positions[edge.from_root]),
                "to_position": list(root_positions[edge.to_root]),
                "geometry": [list(point) for point in edge.geometry],
                "length_m": edge.length_m,
            }
            for edge in unused_edges
        ],
    }
    resolved_audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_temporary = resolved_audit_path.with_suffix(
        resolved_audit_path.suffix + ".tmp"
    )
    audit_temporary.write_bytes(_canonical_json(audit) + b"\n")
    audit_temporary.replace(resolved_audit_path)
    return {
        "path": str(output_path.resolve()),
        "audit_path": str(resolved_audit_path.resolve()),
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "audit_sha256": hashlib.sha256(resolved_audit_path.read_bytes()).hexdigest(),
        "network_id": network.id,
        "line_count": len(network.lines),
        "station_count": sum(node.is_station for node in network.nodes),
        "edge_count": len(network.edges),
        "service_pattern_count": len(network.service_patterns),
    }


def acquire_tfgm_transit_contract(
    network_id: str,
    *,
    user_agent: str,
    cache_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Fetch, hash, compile, and re-open an enabled TfGM catalog record."""

    catalog = catalog_network(network_id)
    acquisition = catalog.get("acquisition")
    if (
        not isinstance(acquisition, dict)
        or acquisition.get("mode") != "official-geometry-plus-gtfs"
    ):
        raise MapPlotterError(f"{network_id} is not a TfGM geometry/GTFS network.")
    if acquisition.get("release_gate") != "enabled":
        raise MapPlotterError(
            f"{network_id} acquisition is gated: "
            f"{acquisition.get('release_gate_reason', 'catalog review required')}."
        )
    raw_date = str(acquisition.get("service_date", ""))
    try:
        service_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise MapPlotterError(
            "Manchester catalog service_date must be ISO YYYY-MM-DD."
        ) from exc
    client = SnapshotClient(user_agent=user_agent, cache_dir=cache_dir)
    source_records = catalog.get("sources")
    if not isinstance(source_records, list):
        raise MapPlotterError("Manchester catalog sources must be a list.")
    payloads: dict[str, bytes] = {}
    retrieval_dates: dict[str, date] = {}
    for source in source_records:
        if not isinstance(source, dict):
            raise MapPlotterError("Manchester catalog source must be an object.")
        source_id = str(source.get("id", ""))
        url = str(source.get("url", ""))
        payloads[source_id], _ = client.get(url)
        retrieval_dates[source_id] = client.retrieved_at(url)
    return compile_tfgm_contract(
        catalog,
        source_payloads=payloads,
        service_date=service_date,
        retrieved_at=client.latest_retrieved_at,
        output_path=output_path,
        source_retrieved_at=retrieval_dates,
    )
