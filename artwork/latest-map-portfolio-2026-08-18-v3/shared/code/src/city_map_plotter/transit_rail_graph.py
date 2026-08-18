"""Hash-pinned OpenStreetMap rail alignment graph.

The graph in this module is deliberately stricter than a generic spatial
network.  Every edge is one consecutive pair of node references from one OSM
way.  Coordinates that merely touch or pass close to one another never create
connectivity.  That rule is essential for bridges, tunnels, parallel tracks,
and other grade-separated railway geometry.

This is an alignment source, not evidence that a passenger operator uses a
particular track.  Timetable calls and reviewed station-to-track choices remain
separate inputs to an operator route compiler.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import heapq
import importlib
import json
from math import asin, cos, floor, isfinite, radians, sin, sqrt
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal

from .models import MapPlotterError


RAIL_GRAPH_POLICY_VERSION = "osm-operational-rail-graph-v1"
ACCEPTED_RAILWAY_VALUES = frozenset({"rail", "light_rail", "narrow_gauge"})
PRESERVED_RAIL_TAG_KEYS = (
    "railway",
    "bridge",
    "tunnel",
    "usage",
    "service",
    "electrified",
    "gauge",
    "name",
    "ref",
    "operator",
)
_LIFECYCLE_RAILWAY_VALUES = frozenset(
    {
        "abandoned",
        "construction",
        "demolished",
        "disused",
        "proposed",
        "razed",
    }
)
_LIFECYCLE_TAG_KEYS = (
    "abandoned",
    "disused",
    "construction",
    "proposed",
    "razed",
    "demolished",
    "removed",
)
_FALSE_TAG_VALUES = frozenset({"", "0", "false", "no", "off"})
_HEADER_TIMESTAMP_KEYS = ("osmosis_replication_timestamp", "timestamp")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EARTH_RADIUS_M = 6_371_008.8
_SPATIAL_CELL_DEGREES = 0.025
_DISTANCE_EPSILON_M = 1e-7


def _tag_pairs(tags: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, tags[key]) for key in PRESERVED_RAIL_TAG_KEYS if key in tags
    )


@dataclass(frozen=True, slots=True)
class RailGraphSource:
    """Immutable provenance for one verified PBF snapshot."""

    path: Path
    sha256: str
    byte_count: int
    header_bounds_wgs84: tuple[float, float, float, float]
    required_bounds_wgs84: tuple[float, float, float, float]
    source_timestamp: str
    source_timestamp_kind: str
    generator: str | None


@dataclass(frozen=True, slots=True)
class RailNode:
    """One exact OSM source node retained by a selected rail way."""

    osm_node_id: int
    lon: float
    lat: float
    osm_version: int | None = None
    osm_timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class RailWay:
    """One selected source way before it is split into graph edges."""

    osm_way_id: int
    node_refs: tuple[int, ...]
    tags: tuple[tuple[str, str], ...]
    osm_version: int | None = None
    osm_timestamp: str | None = None

    def tag(self, key: str) -> str | None:
        return dict(self.tags).get(key)


@dataclass(frozen=True, slots=True)
class RailEdge:
    """One undirected edge derived from an exact consecutive node pair."""

    edge_id: str
    source_way_id: int
    source_segment_index: int
    source_from_node_id: int
    source_to_node_id: int
    length_m: float
    tags: tuple[tuple[str, str], ...]

    @property
    def node_ids(self) -> tuple[int, int]:
        return self.source_from_node_id, self.source_to_node_id


@dataclass(frozen=True, slots=True)
class RailNodeCandidate:
    osm_node_id: int
    lon: float
    lat: float
    distance_m: float
    incident_edge_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RailEdgeCandidate:
    edge_id: str
    source_way_id: int
    source_segment_index: int
    source_from_node_id: int
    source_to_node_id: int
    distance_m: float
    projected_lon: float
    projected_lat: float
    source_fraction: float
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class RailPathCandidateEvidence:
    edge_ids: tuple[str, ...]
    node_ids: tuple[int, ...]
    total_length_m: float
    path_sha256: str


@dataclass(frozen=True, slots=True)
class ShortestPathEvidence:
    """Machine-readable proof of a unique route or a fail-closed result."""

    status: Literal["unique", "ambiguous", "disconnected"]
    graph_sha256: str
    start_node_id: int
    end_node_id: int
    ambiguity_tolerance_m: float
    allowed_edge_count: int
    settled_node_count: int
    start_component_id: int
    end_component_id: int
    candidates: tuple[RailPathCandidateEvidence, ...]
    candidate_count_lower_bound: int
    candidate_count_exact: int | None
    candidates_exhaustive: bool
    candidate_enumeration_limit: int


@dataclass(frozen=True, slots=True)
class RailRouteStep:
    edge_id: str
    from_node_id: int
    to_node_id: int
    source_way_id: int
    source_segment_index: int
    follows_source_direction: bool
    length_m: float
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class RailRoute:
    graph_sha256: str
    node_ids: tuple[int, ...]
    steps: tuple[RailRouteStep, ...]
    total_length_m: float
    evidence: ShortestPathEvidence


class RailGraphRoutingError(MapPlotterError):
    """Base error carrying structured routing evidence."""

    def __init__(self, message: str, evidence: ShortestPathEvidence) -> None:
        super().__init__(message)
        self.evidence = evidence


class RailGraphAmbiguityError(RailGraphRoutingError):
    """Raised instead of choosing arbitrarily between equal shortest paths."""


class RailGraphDisconnectedError(RailGraphRoutingError):
    """Raised when no exact-node path exists between the requested nodes."""


@dataclass(frozen=True, slots=True)
class _RawWay:
    osm_way_id: int
    node_refs: tuple[int, ...]
    tags: tuple[tuple[str, str], ...]
    osm_version: int | None
    osm_timestamp: str | None


def _normalised_tag(value: str | None) -> str:
    return "" if value is None else value.strip().casefold()


def _tag_is_positive(value: str | None) -> bool:
    return _normalised_tag(value) not in _FALSE_TAG_VALUES


def _selection_reason(tags: Mapping[str, str]) -> tuple[bool, str]:
    railway = _normalised_tag(tags.get("railway"))
    public_transport = _normalised_tag(tags.get("public_transport"))
    if railway == "platform" or public_transport == "platform":
        return False, "platform"
    if railway in _LIFECYCLE_RAILWAY_VALUES:
        return False, f"railway={railway}"
    if railway not in ACCEPTED_RAILWAY_VALUES:
        return False, f"unsupported-railway={railway or '<missing>'}"
    for key in _LIFECYCLE_TAG_KEYS:
        if _tag_is_positive(tags.get(key)):
            return False, f"lifecycle-tag={key}"
        if _tag_is_positive(tags.get(f"{key}:railway")):
            return False, f"lifecycle-namespace={key}:railway"
    return True, f"railway={railway}"


def _tags_dict(value: Any) -> dict[str, str]:
    tags = getattr(value, "tags", ())
    try:
        return {str(tag.k): str(tag.v) for tag in tags}
    except AttributeError:
        return {str(key): str(raw) for key, raw in tags}


def _optional_int(value: Any, name: str) -> int | None:
    raw = getattr(value, name, None)
    if callable(raw):
        raw = raw()
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError, RuntimeError):
        return None
    return parsed if parsed > 0 else None


def _optional_timestamp(value: Any) -> str | None:
    raw = getattr(value, "timestamp", None)
    try:
        if raw is None:
            return None
        valid = getattr(raw, "valid", None)
        if callable(valid) and not valid():
            return None
        text = str(raw).strip()
    except (RuntimeError, ValueError):
        return None
    return text or None


def _object_id(value: Any) -> int:
    raw = getattr(value, "id", None)
    if callable(raw):
        raw = raw()
    if raw is None:
        raise MapPlotterError("OSM object has no source ID.")
    try:
        result = int(raw)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise MapPlotterError(f"OSM object has invalid source ID {raw!r}.") from exc
    if result <= 0:
        raise MapPlotterError(f"OSM object has non-positive source ID {result}.")
    return result


def _node_ref(value: Any) -> int:
    raw = getattr(value, "ref", None)
    if callable(raw):
        raw = raw()
    if raw is None:
        raw = getattr(value, "id", None)
        if callable(raw):
            raw = raw()
    if raw is None:
        raise MapPlotterError("Rail way has a node reference without an ID.")
    try:
        result = int(raw)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise MapPlotterError(f"Rail way has invalid node reference {raw!r}.") from exc
    if result <= 0:
        raise MapPlotterError(f"Rail way has non-positive node reference {result}.")
    return result


def _validate_bounds(
    value: Sequence[float], *, name: str
) -> tuple[float, float, float, float]:
    if len(value) != 4:
        raise MapPlotterError(f"{name} must contain west, south, east, north.")
    try:
        west, south, east, north = (float(part) for part in value)
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(f"{name} must contain finite numeric values.") from exc
    if not all(isfinite(part) for part in (west, south, east, north)):
        raise MapPlotterError(f"{name} must contain finite numeric values.")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise MapPlotterError(f"{name} is outside valid WGS84 bounds.")
    return west, south, east, north


def _covers(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _validate_timestamp(value: str, *, kind: str) -> str:
    text = value.strip()
    if not text:
        raise MapPlotterError(f"PBF header {kind} is empty.")
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise MapPlotterError(
            f"PBF header {kind} is not an ISO-8601 timestamp: {text!r}."
        ) from exc
    if parsed.tzinfo is None:
        raise MapPlotterError(f"PBF header {kind} must include a UTC offset.")
    return text


def _import_osmium() -> Any:
    try:
        return importlib.import_module("osmium")
    except ModuleNotFoundError as exc:
        if exc.name != "osmium":
            raise
        raise MapPlotterError(
            "OSM rail graph extraction requires the optional PyOsmium dependency. "
            "Install city-map-plotter[pbf]."
        ) from exc


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Cannot hash rail alignment PBF {path}: {exc}") from exc
    return digest.hexdigest()


def _stat_signature(value: Any) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _read_header(
    osmium: Any, path: Path
) -> tuple[tuple[float, float, float, float], str, str, str | None]:
    reader_type = getattr(getattr(osmium, "io", None), "Reader", None)
    if reader_type is None:
        raise MapPlotterError(
            "The installed PyOsmium does not expose osmium.io.Reader for PBF "
            "provenance validation."
        )
    reader = None
    try:
        reader = reader_type(str(path))
        header = reader.header()
        getter = getattr(header, "get", None)
        if not callable(getter):
            raise MapPlotterError("PBF header does not expose metadata fields.")
        timestamp: str | None = None
        timestamp_kind: str | None = None
        for key in _HEADER_TIMESTAMP_KEYS:
            try:
                raw = getter(key)
            except (KeyError, RuntimeError):
                continue
            if raw is not None and str(raw).strip():
                timestamp = _validate_timestamp(str(raw), kind=key)
                timestamp_kind = key
                break
        if timestamp is None or timestamp_kind is None:
            raise MapPlotterError(
                "PBF header has no source timestamp; a hash alone does not prove "
                "the map edition date."
            )
        try:
            generator_raw = getter("generator")
        except (KeyError, RuntimeError):
            generator_raw = None
        generator = (
            str(generator_raw).strip()
            if generator_raw is not None and str(generator_raw).strip()
            else None
        )
        box_reader = getattr(header, "box", None)
        if not callable(box_reader):
            raise MapPlotterError("PBF header has no declared coverage box.")
        box = box_reader()
        valid = getattr(box, "valid", None)
        if not callable(valid) or not valid():
            raise MapPlotterError("PBF header has no valid declared coverage box.")
        try:
            bounds = _validate_bounds(
                (
                    float(box.bottom_left.lon),
                    float(box.bottom_left.lat),
                    float(box.top_right.lon),
                    float(box.top_right.lat),
                ),
                name="PBF header coverage",
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise MapPlotterError("PBF header coverage box is malformed.") from exc
        return bounds, timestamp, timestamp_kind, generator
    except MapPlotterError:
        raise
    except (OSError, RuntimeError) as exc:
        raise MapPlotterError(f"Cannot read rail alignment PBF header: {exc}") from exc
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


def _haversine_m(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    lon1, lat1 = first
    lon2, lat2 = second
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    lat1_r = radians(lat1)
    lat2_r = radians(lat2)
    value = sin(d_lat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(min(1.0, sqrt(value)))


def _point_segment_projection(
    lon: float,
    lat: float,
    first: RailNode,
    second: RailNode,
) -> tuple[float, float, float, float]:
    # A local tangent plane is accurate at OSM node-to-node segment scale and
    # keeps the projected source fraction deterministic.
    cos_lat = max(1e-9, cos(radians(lat)))
    ax = radians(first.lon - lon) * _EARTH_RADIUS_M * cos_lat
    ay = radians(first.lat - lat) * _EARTH_RADIUS_M
    bx = radians(second.lon - lon) * _EARTH_RADIUS_M * cos_lat
    by = radians(second.lat - lat) * _EARTH_RADIUS_M
    dx = bx - ax
    dy = by - ay
    denominator = dx * dx + dy * dy
    fraction = 0.0 if denominator == 0 else -(ax * dx + ay * dy) / denominator
    fraction = min(1.0, max(0.0, fraction))
    px = ax + fraction * dx
    py = ay + fraction * dy
    projected_lon = lon + px / (_EARTH_RADIUS_M * cos_lat) * 180.0 / 3.141592653589793
    projected_lat = lat + py / _EARTH_RADIUS_M * 180.0 / 3.141592653589793
    return sqrt(px * px + py * py), projected_lon, projected_lat, fraction


def _spatial_key(lon: float, lat: float) -> tuple[int, int]:
    return floor(lon / _SPATIAL_CELL_DEGREES), floor(lat / _SPATIAL_CELL_DEGREES)


def _query_cells(
    lon: float, lat: float, max_distance_m: float
) -> Iterable[tuple[int, int]]:
    lat_delta = max_distance_m / 110_574.0
    lon_scale = max(1e-6, 111_320.0 * cos(radians(lat)))
    lon_delta = max_distance_m / lon_scale
    west, south = _spatial_key(lon - lon_delta, lat - lat_delta)
    east, north = _spatial_key(lon + lon_delta, lat + lat_delta)
    for x in range(west, east + 1):
        for y in range(south, north + 1):
            yield x, y


def _path_sha256(edge_ids: Sequence[str], node_ids: Sequence[int]) -> str:
    raw = json.dumps(
        {"edge_ids": list(edge_ids), "node_ids": list(node_ids)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


class OsmRailGraph:
    """Deterministic undirected graph built only from exact OSM topology."""

    def __init__(
        self,
        *,
        source: RailGraphSource,
        nodes: Iterable[RailNode],
        ways: Iterable[RailWay],
        selection_audit: Mapping[str, Any],
    ) -> None:
        node_records = sorted(nodes, key=lambda item: item.osm_node_id)
        way_records = sorted(ways, key=lambda item: item.osm_way_id)
        node_map = {item.osm_node_id: item for item in node_records}
        if len(node_map) != len(node_records):
            raise MapPlotterError("Rail graph repeats an OSM node ID.")
        way_map = {item.osm_way_id: item for item in way_records}
        if len(way_map) != len(way_records):
            raise MapPlotterError("Rail graph repeats an OSM way ID.")

        edge_records: list[RailEdge] = []
        for way in way_records:
            if len(way.node_refs) < 2:
                raise MapPlotterError(
                    f"Selected rail way {way.osm_way_id} has fewer than two nodes."
                )
            for segment_index, (first_id, second_id) in enumerate(
                zip(way.node_refs, way.node_refs[1:], strict=False)
            ):
                if first_id == second_id:
                    raise MapPlotterError(
                        f"Selected rail way {way.osm_way_id} repeats node {first_id} "
                        f"at consecutive positions {segment_index}/{segment_index + 1}."
                    )
                try:
                    first = node_map[first_id]
                    second = node_map[second_id]
                except KeyError as exc:
                    raise MapPlotterError(
                        f"Selected rail way {way.osm_way_id} references missing OSM "
                        f"node {exc.args[0]}."
                    ) from exc
                length_m = _haversine_m(
                    (first.lon, first.lat), (second.lon, second.lat)
                )
                if not isfinite(length_m) or length_m <= _DISTANCE_EPSILON_M:
                    raise MapPlotterError(
                        f"Rail way {way.osm_way_id} segment {segment_index} has no "
                        "measurable source-node separation."
                    )
                edge_records.append(
                    RailEdge(
                        edge_id=f"osm-way/{way.osm_way_id}/segment/{segment_index}",
                        source_way_id=way.osm_way_id,
                        source_segment_index=segment_index,
                        source_from_node_id=first_id,
                        source_to_node_id=second_id,
                        length_m=length_m,
                        tags=way.tags,
                    )
                )
        edge_records.sort(key=lambda item: item.edge_id)
        edge_map = {item.edge_id: item for item in edge_records}
        if len(edge_map) != len(edge_records):
            raise MapPlotterError("Rail graph repeats a derived edge ID.")

        adjacency: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for edge in edge_records:
            adjacency[edge.source_from_node_id].append(
                (edge.source_to_node_id, edge.edge_id)
            )
            adjacency[edge.source_to_node_id].append(
                (edge.source_from_node_id, edge.edge_id)
            )
        self._adjacency = {
            node_id: tuple(sorted(values, key=lambda item: (item[1], item[0])))
            for node_id, values in adjacency.items()
        }
        self.source = source
        self.nodes: Mapping[int, RailNode] = MappingProxyType(node_map)
        self.ways: Mapping[int, RailWay] = MappingProxyType(way_map)
        self.edges: Mapping[str, RailEdge] = MappingProxyType(edge_map)
        # Keep the diagnostic record independent of mutable caller-owned
        # dictionaries; ``audit()`` returns another deep copy below.
        self._selection_audit = json.loads(
            json.dumps(selection_audit, sort_keys=True, separators=(",", ":"))
        )
        self.graph_sha256 = self._canonical_graph_sha256()
        self._component_by_node, self._component_sizes = self._components()
        self._node_grid, self._edge_grid = self._spatial_indexes()

    def _canonical_graph_sha256(self) -> str:
        payload = {
            "policy_version": RAIL_GRAPH_POLICY_VERSION,
            "nodes": [
                {
                    "osm_node_id": node.osm_node_id,
                    "lon": format(node.lon, ".7f"),
                    "lat": format(node.lat, ".7f"),
                    "osm_version": node.osm_version,
                    "osm_timestamp": node.osm_timestamp,
                }
                for node in self.nodes.values()
            ],
            "ways": [
                {
                    "osm_way_id": way.osm_way_id,
                    "node_refs": way.node_refs,
                    "tags": way.tags,
                    "osm_version": way.osm_version,
                    "osm_timestamp": way.osm_timestamp,
                }
                for way in self.ways.values()
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _components(self) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
        assignments: dict[int, int] = {}
        sizes: dict[int, tuple[int, int]] = {}
        for start in sorted(self.nodes):
            if start in assignments:
                continue
            queue = deque([start])
            members: set[int] = set()
            edge_ids: set[str] = set()
            while queue:
                node_id = queue.popleft()
                if node_id in members:
                    continue
                members.add(node_id)
                for neighbour, edge_id in self._adjacency.get(node_id, ()):
                    edge_ids.add(edge_id)
                    if neighbour not in members:
                        queue.append(neighbour)
            component_id = min(members)
            for node_id in members:
                assignments[node_id] = component_id
            sizes[component_id] = (len(members), len(edge_ids))
        return assignments, sizes

    def _spatial_indexes(
        self,
    ) -> tuple[
        dict[tuple[int, int], tuple[int, ...]],
        dict[tuple[int, int], tuple[str, ...]],
    ]:
        node_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for node in self.nodes.values():
            node_grid[_spatial_key(node.lon, node.lat)].append(node.osm_node_id)
        edge_grid: dict[tuple[int, int], list[str]] = defaultdict(list)
        for edge in self.edges.values():
            first = self.nodes[edge.source_from_node_id]
            second = self.nodes[edge.source_to_node_id]
            west, south = _spatial_key(min(first.lon, second.lon), min(first.lat, second.lat))
            east, north = _spatial_key(max(first.lon, second.lon), max(first.lat, second.lat))
            for x in range(west, east + 1):
                for y in range(south, north + 1):
                    edge_grid[(x, y)].append(edge.edge_id)
        return (
            {key: tuple(sorted(values)) for key, values in node_grid.items()},
            {key: tuple(sorted(values)) for key, values in edge_grid.items()},
        )

    def audit(self) -> dict[str, Any]:
        selection_audit = json.loads(
            json.dumps(self._selection_audit, sort_keys=True, separators=(",", ":"))
        )
        return {
            "policy_version": RAIL_GRAPH_POLICY_VERSION,
            "accepted_railway_values": sorted(ACCEPTED_RAILWAY_VALUES),
            "preserved_tag_keys": list(PRESERVED_RAIL_TAG_KEYS),
            "excluded_lifecycle_railway_values": sorted(
                _LIFECYCLE_RAILWAY_VALUES
            ),
            "excluded_lifecycle_tag_keys": list(_LIFECYCLE_TAG_KEYS),
            "excluded_platform_markers": [
                "railway=platform",
                "public_transport=platform",
            ],
            "source_path": str(self.source.path),
            "source_sha256": self.source.sha256,
            "source_byte_count": self.source.byte_count,
            "source_timestamp": self.source.source_timestamp,
            "source_timestamp_kind": self.source.source_timestamp_kind,
            "header_bounds_wgs84": list(self.source.header_bounds_wgs84),
            "required_bounds_wgs84": list(self.source.required_bounds_wgs84),
            "header_covers_required_bounds": True,
            **selection_audit,
            "node_count": len(self.nodes),
            "way_count": len(self.ways),
            "edge_count": len(self.edges),
            "connected_component_count": len(self._component_sizes),
            "graph_sha256": self.graph_sha256,
            "edge_construction": "consecutive-exact-osm-node-references-only",
            "graph_directionality": "undirected-physical-alignment",
            "routing_weight": "great-circle-source-node-segment-length-metres",
            "station_attachment": "ranked-candidates-only-no-automatic-snap",
            "operational_status_claim": "selected-by-explicit-osm-tag-policy-only",
            "invented_connector_count": 0,
            "proximity_join_count": 0,
            "operator_service_geometry_claimed": False,
        }

    @staticmethod
    def _validate_query(
        lon: float, lat: float, *, max_distance_m: float, limit: int
    ) -> tuple[float, float, float, int]:
        try:
            longitude = float(lon)
            latitude = float(lat)
            distance = float(max_distance_m)
        except (TypeError, ValueError) as exc:
            raise MapPlotterError("Rail candidate query values must be numeric.") from exc
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise MapPlotterError("Rail candidate query coordinate is outside WGS84.")
        if not isfinite(distance) or distance <= 0:
            raise MapPlotterError("Rail candidate max_distance_m must be positive.")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise MapPlotterError("Rail candidate limit must be a positive integer.")
        return longitude, latitude, distance, limit

    def nearest_node_candidates(
        self,
        lon: float,
        lat: float,
        *,
        max_distance_m: float,
        limit: int = 8,
    ) -> tuple[RailNodeCandidate, ...]:
        longitude, latitude, distance_limit, result_limit = self._validate_query(
            lon, lat, max_distance_m=max_distance_m, limit=limit
        )
        ids = {
            node_id
            for key in _query_cells(longitude, latitude, distance_limit)
            for node_id in self._node_grid.get(key, ())
        }
        records: list[RailNodeCandidate] = []
        for node_id in ids:
            node = self.nodes[node_id]
            distance_m = _haversine_m(
                (longitude, latitude), (node.lon, node.lat)
            )
            if distance_m <= distance_limit:
                records.append(
                    RailNodeCandidate(
                        osm_node_id=node_id,
                        lon=node.lon,
                        lat=node.lat,
                        distance_m=distance_m,
                        incident_edge_ids=tuple(
                            edge_id
                            for _, edge_id in self._adjacency.get(node_id, ())
                        ),
                    )
                )
        records.sort(key=lambda item: (item.distance_m, item.osm_node_id))
        return tuple(records[:result_limit])

    def nearest_edge_candidates(
        self,
        lon: float,
        lat: float,
        *,
        max_distance_m: float,
        limit: int = 8,
    ) -> tuple[RailEdgeCandidate, ...]:
        longitude, latitude, distance_limit, result_limit = self._validate_query(
            lon, lat, max_distance_m=max_distance_m, limit=limit
        )
        edge_ids = {
            edge_id
            for key in _query_cells(longitude, latitude, distance_limit)
            for edge_id in self._edge_grid.get(key, ())
        }
        records: list[RailEdgeCandidate] = []
        for edge_id in edge_ids:
            edge = self.edges[edge_id]
            first = self.nodes[edge.source_from_node_id]
            second = self.nodes[edge.source_to_node_id]
            distance_m, projected_lon, projected_lat, fraction = (
                _point_segment_projection(longitude, latitude, first, second)
            )
            if distance_m <= distance_limit:
                records.append(
                    RailEdgeCandidate(
                        edge_id=edge_id,
                        source_way_id=edge.source_way_id,
                        source_segment_index=edge.source_segment_index,
                        source_from_node_id=edge.source_from_node_id,
                        source_to_node_id=edge.source_to_node_id,
                        distance_m=distance_m,
                        projected_lon=projected_lon,
                        projected_lat=projected_lat,
                        source_fraction=fraction,
                        tags=edge.tags,
                    )
                )
        records.sort(key=lambda item: (item.distance_m, item.edge_id))
        return tuple(records[:result_limit])

    def shortest_path(
        self,
        start_node_id: int,
        end_node_id: int,
        *,
        allowed_edge_ids: Collection[str] | None = None,
        ambiguity_tolerance_m: float = 0.05,
        candidate_enumeration_limit: int = 3,
    ) -> RailRoute:
        if start_node_id not in self.nodes:
            raise MapPlotterError(f"Unknown rail start node {start_node_id}.")
        if end_node_id not in self.nodes:
            raise MapPlotterError(f"Unknown rail end node {end_node_id}.")
        try:
            tolerance = float(ambiguity_tolerance_m)
        except (TypeError, ValueError) as exc:
            raise MapPlotterError("ambiguity_tolerance_m must be numeric.") from exc
        if not isfinite(tolerance) or tolerance < 0:
            raise MapPlotterError("ambiguity_tolerance_m must be finite and non-negative.")
        if (
            isinstance(candidate_enumeration_limit, bool)
            or not isinstance(candidate_enumeration_limit, int)
            or not 2 <= candidate_enumeration_limit <= 1024
        ):
            raise MapPlotterError(
                "candidate_enumeration_limit must be an integer from 2 to 1024."
            )
        if allowed_edge_ids is None:
            allowed = frozenset(self.edges)
        else:
            allowed = frozenset(str(edge_id) for edge_id in allowed_edge_ids)
            unknown = sorted(allowed.difference(self.edges))
            if unknown:
                raise MapPlotterError(
                    "Shortest-path edge filter contains unknown edge IDs: "
                    + ", ".join(unknown[:8])
                )

        start_component = self._component_by_node[start_node_id]
        end_component = self._component_by_node[end_node_id]
        distances: dict[int, float] = {start_node_id: 0.0}
        predecessors: dict[int, list[tuple[int, str]]] = defaultdict(list)
        heap: list[tuple[float, int]] = [(0.0, start_node_id)]
        settled: set[int] = set()
        while heap:
            distance, node_id = heapq.heappop(heap)
            if distance > distances.get(node_id, float("inf")) + tolerance:
                continue
            target_distance = distances.get(end_node_id)
            if target_distance is not None and distance > target_distance + tolerance:
                break
            settled.add(node_id)
            for neighbour, edge_id in self._adjacency.get(node_id, ()):
                if edge_id not in allowed:
                    continue
                edge = self.edges[edge_id]
                candidate_distance = distance + edge.length_m
                current = distances.get(neighbour)
                if current is None or candidate_distance < current - tolerance:
                    distances[neighbour] = candidate_distance
                    predecessors[neighbour] = [(node_id, edge_id)]
                    heapq.heappush(heap, (candidate_distance, neighbour))
                elif (
                    abs(candidate_distance - current) <= tolerance
                    and distances[node_id] < current - _DISTANCE_EPSILON_M
                ):
                    predecessor = (node_id, edge_id)
                    if predecessor not in predecessors[neighbour]:
                        predecessors[neighbour].append(predecessor)
                        predecessors[neighbour].sort(key=lambda item: (item[1], item[0]))

        if end_node_id not in distances:
            evidence = ShortestPathEvidence(
                status="disconnected",
                graph_sha256=self.graph_sha256,
                start_node_id=start_node_id,
                end_node_id=end_node_id,
                ambiguity_tolerance_m=tolerance,
                allowed_edge_count=len(allowed),
                settled_node_count=len(settled),
                start_component_id=start_component,
                end_component_id=end_component,
                candidates=(),
                candidate_count_lower_bound=0,
                candidate_count_exact=0,
                candidates_exhaustive=True,
                candidate_enumeration_limit=candidate_enumeration_limit,
            )
            raise RailGraphDisconnectedError(
                "No route exists using consecutive exact OSM node references; "
                "no proximity connector was invented.",
                evidence,
            )

        raw_paths, candidates_exhaustive = self._reconstruct_paths(
            start_node_id,
            end_node_id,
            predecessors,
            limit=candidate_enumeration_limit,
        )
        candidates = tuple(
            self._path_evidence(node_ids, edge_ids)
            for node_ids, edge_ids in raw_paths
        )
        if not candidates:
            # The start=end case has a valid empty path and is handled by the
            # reconstruction helper. Any other empty result is an internal
            # provenance failure and must not be guessed around.
            raise MapPlotterError("Shortest-path predecessor evidence is incomplete.")
        if len(candidates) > 1:
            evidence = ShortestPathEvidence(
                status="ambiguous",
                graph_sha256=self.graph_sha256,
                start_node_id=start_node_id,
                end_node_id=end_node_id,
                ambiguity_tolerance_m=tolerance,
                allowed_edge_count=len(allowed),
                settled_node_count=len(settled),
                start_component_id=start_component,
                end_component_id=end_component,
                candidates=candidates,
                candidate_count_lower_bound=len(candidates),
                candidate_count_exact=(
                    len(candidates) if candidates_exhaustive else None
                ),
                candidates_exhaustive=candidates_exhaustive,
                candidate_enumeration_limit=candidate_enumeration_limit,
            )
            raise RailGraphAmbiguityError(
                "More than one shortest exact-node rail alignment is within the "
                "declared ambiguity tolerance; a reviewed choice is required.",
                evidence,
            )

        path_candidate = candidates[0]
        evidence = ShortestPathEvidence(
            status="unique",
            graph_sha256=self.graph_sha256,
            start_node_id=start_node_id,
            end_node_id=end_node_id,
            ambiguity_tolerance_m=tolerance,
            allowed_edge_count=len(allowed),
            settled_node_count=len(settled),
            start_component_id=start_component,
            end_component_id=end_component,
            candidates=candidates,
            candidate_count_lower_bound=1,
            candidate_count_exact=1,
            candidates_exhaustive=True,
            candidate_enumeration_limit=candidate_enumeration_limit,
        )
        steps: list[RailRouteStep] = []
        node_pairs = zip(
            path_candidate.node_ids, path_candidate.node_ids[1:], strict=False
        )
        for (first_id, second_id), edge_id in zip(
            node_pairs, path_candidate.edge_ids, strict=True
        ):
            edge = self.edges[edge_id]
            steps.append(
                RailRouteStep(
                    edge_id=edge_id,
                    from_node_id=first_id,
                    to_node_id=second_id,
                    source_way_id=edge.source_way_id,
                    source_segment_index=edge.source_segment_index,
                    follows_source_direction=(
                        first_id == edge.source_from_node_id
                        and second_id == edge.source_to_node_id
                    ),
                    length_m=edge.length_m,
                    tags=edge.tags,
                )
            )
        return RailRoute(
            graph_sha256=self.graph_sha256,
            node_ids=path_candidate.node_ids,
            steps=tuple(steps),
            total_length_m=path_candidate.total_length_m,
            evidence=evidence,
        )

    def _reconstruct_paths(
        self,
        start_node_id: int,
        end_node_id: int,
        predecessors: Mapping[int, list[tuple[int, str]]],
        *,
        limit: int,
    ) -> tuple[list[tuple[tuple[int, ...], tuple[str, ...]]], bool]:
        if limit <= 0:
            return [], False
        if end_node_id == start_node_id:
            return [((start_node_id,), ())], True

        def ordered(node_id: int) -> tuple[tuple[int, str], ...]:
            return tuple(
                sorted(
                    predecessors.get(node_id, ()),
                    key=lambda item: (item[1], item[0]),
                )
            )

        # National routes can exceed Python's recursion limit by several times.
        # Enumerate the same edge-ID/previous-node ordered depth-first paths with
        # explicit frames. At most ``limit`` complete paths are retained.  The
        # returned exhaustive flag is true only when the predecessor DAG was
        # fully drained before that bound; this distinguishes an exact
        # equivalence-class count from a lower bound without materialising an
        # unbounded number of equal paths in a national graph.
        records: list[tuple[tuple[int, ...], tuple[str, ...]]] = []
        reverse_node_ids = [end_node_id]
        reverse_edge_ids: list[str] = []
        frames: list[tuple[int, int, tuple[tuple[int, str], ...]]] = [
            (end_node_id, 0, ordered(end_node_id))
        ]
        while frames and len(records) < limit:
            node_id, index, choices = frames[-1]
            if index >= len(choices):
                frames.pop()
                if frames:
                    reverse_node_ids.pop()
                    reverse_edge_ids.pop()
                continue
            previous_id, edge_id = choices[index]
            frames[-1] = (node_id, index + 1, choices)
            if previous_id in reverse_node_ids:
                raise MapPlotterError(
                    "Shortest-path predecessor evidence contains a cycle."
                )
            reverse_node_ids.append(previous_id)
            reverse_edge_ids.append(edge_id)
            if previous_id == start_node_id:
                records.append(
                    (
                        tuple(reversed(reverse_node_ids)),
                        tuple(reversed(reverse_edge_ids)),
                    )
                )
                reverse_node_ids.pop()
                reverse_edge_ids.pop()
                continue
            frames.append((previous_id, 0, ordered(previous_id)))
        return records, not frames

    def _path_evidence(
        self, node_ids: tuple[int, ...], edge_ids: tuple[str, ...]
    ) -> RailPathCandidateEvidence:
        total = sum(self.edges[edge_id].length_m for edge_id in edge_ids)
        return RailPathCandidateEvidence(
            edge_ids=edge_ids,
            node_ids=node_ids,
            total_length_m=total,
            path_sha256=_path_sha256(edge_ids, node_ids),
        )


def _handlers_for(osmium: Any) -> tuple[type[Any], type[Any]]:
    base = getattr(osmium, "SimpleHandler", None)
    if base is None:
        raise MapPlotterError(
            "The installed PyOsmium does not expose SimpleHandler for streaming PBFs."
        )

    class _WayHandler(base):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.ways: dict[int, _RawWay] = {}
            self.required_node_ids: set[int] = set()
            self.scanned_way_count = 0
            self.selected_railway_value_counts: Counter[str] = Counter()
            self.excluded_policy_counts: Counter[str] = Counter()
            self.ignored_railway_value_counts: Counter[str] = Counter()

        def way(self, value: Any) -> None:
            self.scanned_way_count += 1
            tags = _tags_dict(value)
            railway = _normalised_tag(tags.get("railway"))
            selected, reason = _selection_reason(tags)
            if not selected:
                if reason.startswith("unsupported-railway="):
                    if railway:
                        self.ignored_railway_value_counts[railway] += 1
                else:
                    self.excluded_policy_counts[reason] += 1
                return
            way_id = _object_id(value)
            if way_id in self.ways:
                raise MapPlotterError(f"PBF repeats selected OSM way {way_id}.")
            node_refs = tuple(_node_ref(node) for node in getattr(value, "nodes", ()))
            if len(node_refs) < 2:
                raise MapPlotterError(
                    f"Selected rail way {way_id} has fewer than two node references."
                )
            self.ways[way_id] = _RawWay(
                osm_way_id=way_id,
                node_refs=node_refs,
                tags=_tag_pairs(tags),
                osm_version=_optional_int(value, "version"),
                osm_timestamp=_optional_timestamp(value),
            )
            self.required_node_ids.update(node_refs)
            self.selected_railway_value_counts[railway] += 1

    class _NodeHandler(base):  # type: ignore[misc, valid-type]
        def __init__(self, required_node_ids: set[int]) -> None:
            super().__init__()
            self.required_node_ids = required_node_ids
            self.nodes: dict[int, RailNode] = {}

        def node(self, value: Any) -> None:
            node_id = _object_id(value)
            if node_id not in self.required_node_ids:
                return
            if node_id in self.nodes:
                raise MapPlotterError(f"PBF repeats required OSM node {node_id}.")
            location = getattr(value, "location", None)
            valid = getattr(location, "valid", None)
            if location is None or (callable(valid) and not valid()):
                raise MapPlotterError(
                    f"Required OSM rail node {node_id} has no valid location."
                )
            try:
                lon = float(location.lon)
                lat = float(location.lat)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                raise MapPlotterError(
                    f"Required OSM rail node {node_id} has a malformed location."
                ) from exc
            if not (isfinite(lon) and isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise MapPlotterError(
                    f"Required OSM rail node {node_id} is outside WGS84."
                )
            self.nodes[node_id] = RailNode(
                osm_node_id=node_id,
                lon=lon,
                lat=lat,
                osm_version=_optional_int(value, "version"),
                osm_timestamp=_optional_timestamp(value),
            )

    return _WayHandler, _NodeHandler


def load_osm_rail_graph(
    path: Path,
    *,
    expected_sha256: str,
    required_bounds_wgs84: Sequence[float],
) -> OsmRailGraph:
    """Load a pinned PBF into an exact-node operational rail graph.

    The file is streamed twice without a global location index: the first pass
    retains selected way node references and the second retains only the source
    nodes those ways use.  This keeps the Python heap proportional to rail
    topology rather than every road/building node in a Great Britain extract.
    """

    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(
        expected_sha256
    ):
        raise MapPlotterError("expected_sha256 must be one lowercase SHA-256 digest.")
    required_bounds = _validate_bounds(
        required_bounds_wgs84, name="required_bounds_wgs84"
    )
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise MapPlotterError(f"Cannot open rail alignment PBF {path}: {exc}") from exc
    if not resolved.is_file():
        raise MapPlotterError(f"Rail alignment PBF is not a regular file: {resolved}")
    try:
        initial_stat = resolved.stat()
    except OSError as exc:
        raise MapPlotterError(f"Cannot inspect rail alignment PBF: {exc}") from exc
    actual_sha256 = _sha256_path(resolved)
    try:
        hashed_stat = resolved.stat()
    except OSError as exc:
        raise MapPlotterError(f"Rail alignment PBF vanished while hashing: {exc}") from exc
    if _stat_signature(initial_stat) != _stat_signature(hashed_stat):
        raise MapPlotterError("Rail alignment PBF changed while its hash was computed.")
    if actual_sha256 != expected_sha256:
        raise MapPlotterError(
            "Rail alignment PBF SHA-256 mismatch: expected "
            f"{expected_sha256}, got {actual_sha256}."
        )

    osmium = _import_osmium()
    header_bounds, source_timestamp, timestamp_kind, generator = _read_header(
        osmium, resolved
    )
    if not _covers(header_bounds, required_bounds):
        raise MapPlotterError(
            "Rail alignment PBF header does not cover required bounds: "
            f"header={header_bounds}, required={required_bounds}."
        )
    way_handler_type, node_handler_type = _handlers_for(osmium)
    way_handler = way_handler_type()
    try:
        way_handler.apply_file(str(resolved), locations=False)
        midway_stat = resolved.stat()
        if _stat_signature(initial_stat) != _stat_signature(midway_stat):
            raise MapPlotterError("Rail alignment PBF changed during the way pass.")
        node_handler = node_handler_type(way_handler.required_node_ids)
        node_handler.apply_file(str(resolved), locations=False)
        final_stat = resolved.stat()
    except MapPlotterError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise MapPlotterError(
            f"Cannot stream rail alignment PBF {resolved}: {exc}. Ensure it is a "
            "complete ordinary snapshot, not an incomplete change file."
        ) from exc
    if _stat_signature(initial_stat) != _stat_signature(final_stat):
        raise MapPlotterError("Rail alignment PBF changed during the node pass.")
    missing_nodes = sorted(
        way_handler.required_node_ids.difference(node_handler.nodes)
    )
    if missing_nodes:
        raise MapPlotterError(
            "Rail alignment PBF is reference-incomplete; selected ways require "
            f"{len(missing_nodes)} missing nodes (first: {missing_nodes[:8]})."
        )

    source = RailGraphSource(
        path=resolved,
        sha256=actual_sha256,
        byte_count=initial_stat.st_size,
        header_bounds_wgs84=header_bounds,
        required_bounds_wgs84=required_bounds,
        source_timestamp=source_timestamp,
        source_timestamp_kind=timestamp_kind,
        generator=generator,
    )
    ways = (
        RailWay(
            osm_way_id=raw.osm_way_id,
            node_refs=raw.node_refs,
            tags=raw.tags,
            osm_version=raw.osm_version,
            osm_timestamp=raw.osm_timestamp,
        )
        for raw in way_handler.ways.values()
    )
    selection_audit = {
        "streaming_pass_count": 2,
        "global_node_location_index_used": False,
        "scanned_way_count": way_handler.scanned_way_count,
        "selected_railway_value_counts": dict(
            sorted(way_handler.selected_railway_value_counts.items())
        ),
        "excluded_policy_counts": dict(
            sorted(way_handler.excluded_policy_counts.items())
        ),
        "ignored_railway_value_counts": dict(
            sorted(way_handler.ignored_railway_value_counts.items())
        ),
    }
    return OsmRailGraph(
        source=source,
        nodes=node_handler.nodes.values(),
        ways=ways,
        selection_audit=selection_audit,
    )


__all__ = [
    "ACCEPTED_RAILWAY_VALUES",
    "OsmRailGraph",
    "PRESERVED_RAIL_TAG_KEYS",
    "RAIL_GRAPH_POLICY_VERSION",
    "RailEdge",
    "RailEdgeCandidate",
    "RailGraphAmbiguityError",
    "RailGraphDisconnectedError",
    "RailGraphRoutingError",
    "RailGraphSource",
    "RailNode",
    "RailNodeCandidate",
    "RailPathCandidateEvidence",
    "RailRoute",
    "RailRouteStep",
    "RailWay",
    "ShortestPathEvidence",
    "load_osm_rail_graph",
]
