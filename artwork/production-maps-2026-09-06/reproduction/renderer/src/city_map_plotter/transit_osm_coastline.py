"""Exact, hash-pinned Great Britain coastline context from an OSM PBF.

The national operator plates must not infer a coastline by subtracting
independently generalised sea-fill polygons.  This loader streams only authored
``natural=coastline`` ways and their exact referenced nodes, proves their
directed land-left cycles, and selects the Great Britain cartographic scope
without snapping, reversing, or inventing geometry.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from shapely.geometry import Point, Polygon, box

from .models import MapPlotterError
from .transit_zoomstack import DEFAULT_GB_BOUNDS, GB_CONTEXT_GEOGRAPHIC_SCOPE


OSM_GB_COASTLINE_POLICY_VERSION = "osm-authored-gb-coastline-v1"
OSM_GB_COASTLINE_SOURCE_URL = (
    "https://download.geofabrik.de/europe/great-britain.html"
)
PINNED_GB_PBF_2026_08_07_SHA256 = (
    "2e0b431cfe07311baa5356375e32d3cc6532edb9d1dab458f360104c2c73f9c3"
)
_PINNED_GB_PBF_2026_08_07_INVARIANTS = {
    "source_byte_count": 2_156_721_330,
    "source_way_count": 28_172,
    "required_node_count": 2_156_690,
    "raw_closed_way_count": 19_278,
    "nonclosed_way_count": 8_894,
    "directed_stitched_cycle_count": 363,
    "source_cycle_count": 19_641,
    "selected_cycle_count": 19_640,
    "selected_way_count": 28_171,
    "excluded_way_ids": (1_339_962_684,),
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_HEADER_TIMESTAMP_KEYS = ("osmosis_replication_timestamp", "timestamp")

# These are jurisdiction/coverage assertions only.  They select no coordinate
# and never alter a source way.  Every required point must lie on exactly one
# authored closed land ring; every denied point must lie on none.
REQUIRED_GB_LAND_ANCHORS: dict[str, tuple[float, float]] = {
    "England mainland": (-0.1276, 51.5072),
    "Scotland mainland": (-4.2518, 55.8642),
    "Wales mainland": (-3.1791, 51.4816),
    "Anglesey": (-4.3106, 53.2556),
    "Isle of Wight": (-1.2970, 50.7000),
    "Skye": (-6.1942, 57.4125),
    "Mull": (-6.0723, 56.6210),
    "Arran": (-5.1470, 55.5760),
    "Lewis and Harris": (-6.3865, 58.2093),
    "Orkney Mainland": (-2.9587, 58.9847),
    "Shetland Mainland": (-1.1450, 60.1550),
    "Isles of Scilly": (-6.3150, 49.9140),
}
DENIED_NON_GB_LAND_ANCHORS: dict[str, tuple[float, float]] = {
    "Northern Ireland (Belfast)": (-5.9301, 54.5973),
    "Northern Ireland (Derry)": (-7.3100, 54.9960),
    "Ireland (Dublin)": (-6.2603, 53.3498),
    "Ireland (Cork)": (-8.4756, 51.8985),
    "Isle of Man": (-4.4821, 54.1523),
    "Guernsey": (-2.5370, 49.4550),
    "Jersey": (-2.1049, 49.1868),
    "France (Calais)": (1.8587, 50.9513),
    "France (Cherbourg)": (-1.6220, 49.6330),
    "Netherlands": (4.4777, 51.9244),
}


@dataclass(frozen=True, slots=True)
class OsmCoastlineWay:
    """One selected authored OSM coastline way with exact node lineage."""

    osm_way_id: int
    node_refs: tuple[int, ...]
    geometry: tuple[tuple[float, float], ...]
    bounds_wgs84: tuple[float, float, float, float]
    osm_version: int | None = None
    osm_timestamp: str | None = None

    @property
    def source_object(self) -> str:
        return f"way/{self.osm_way_id}"


@dataclass(frozen=True, slots=True)
class GreatBritainCoastline:
    """Verified authored coastline ready for per-operator clipping."""

    source_path: Path
    source_sha256: str
    source_byte_count: int
    source_timestamp: str
    source_timestamp_kind: str
    header_bounds_wgs84: tuple[float, float, float, float]
    bounds_wgs84: tuple[float, float, float, float]
    ways: tuple[OsmCoastlineWay, ...]
    audit: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _RawCoastlineWay:
    osm_way_id: int
    node_refs: tuple[int, ...]
    osm_version: int | None
    osm_timestamp: str | None


def _stat_signature(value: Any) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Cannot hash authored-coastline PBF {path}: {exc}") from exc
    return digest.hexdigest()


def _import_osmium() -> Any:
    try:
        return importlib.import_module("osmium")
    except ModuleNotFoundError as exc:
        if exc.name != "osmium":
            raise
        raise MapPlotterError(
            "Authored OSM coastline extraction requires PyOsmium. "
            "Install city-map-plotter[pbf]."
        ) from exc


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any, name: str) -> int | None:
    raw = getattr(value, name, None)
    if callable(raw):
        raw = raw()
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _node_ref(value: Any) -> int:
    raw = getattr(value, "ref", value)
    try:
        result = int(raw)
    except (TypeError, ValueError) as exc:
        raise MapPlotterError("An authored coastline way has a malformed node ref.") from exc
    if result <= 0:
        raise MapPlotterError("An authored coastline way has a non-positive node ref.")
    return result


def _tags(value: Any) -> dict[str, str]:
    tags = getattr(value, "tags", ())
    try:
        return {str(tag.k): str(tag.v) for tag in tags}
    except AttributeError:
        return {str(key): str(raw) for key, raw in tags}


def _read_header(
    osmium: Any, path: Path
) -> tuple[tuple[float, float, float, float], str, str, str | None]:
    reader_type = getattr(getattr(osmium, "io", None), "Reader", None)
    if reader_type is None:
        raise MapPlotterError("PyOsmium does not expose its PBF header reader.")
    reader = None
    try:
        reader = reader_type(str(path))
        header = reader.header()
        getter = getattr(header, "get", None)
        if not callable(getter):
            raise MapPlotterError("Authored-coastline PBF header has no metadata.")
        source_timestamp: str | None = None
        timestamp_kind: str | None = None
        for key in _HEADER_TIMESTAMP_KEYS:
            try:
                candidate = _timestamp(getter(key))
            except (KeyError, RuntimeError):
                candidate = None
            if candidate:
                iso = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
                try:
                    parsed = datetime.fromisoformat(iso)
                except ValueError as exc:
                    raise MapPlotterError(
                        f"Authored-coastline PBF {key} is not ISO-8601."
                    ) from exc
                if parsed.tzinfo is None:
                    raise MapPlotterError(
                        f"Authored-coastline PBF {key} lacks a UTC offset."
                    )
                source_timestamp = candidate
                timestamp_kind = key
                break
        if source_timestamp is None or timestamp_kind is None:
            raise MapPlotterError(
                "Authored-coastline PBF has no dated source timestamp."
            )
        try:
            generator = _timestamp(getter("generator"))
        except (KeyError, RuntimeError):
            generator = None
        source_box = header.box()
        if not source_box.valid():
            raise MapPlotterError(
                "Authored-coastline PBF has no valid declared coverage box."
            )
        bounds = (
            float(source_box.bottom_left.lon),
            float(source_box.bottom_left.lat),
            float(source_box.top_right.lon),
            float(source_box.top_right.lat),
        )
    except MapPlotterError:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Cannot read authored-coastline PBF header {path}: {exc}"
        ) from exc
    finally:
        if reader is not None:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
    west, south, east, north = bounds
    if not (
        all(isfinite(part) for part in bounds)
        and -180.0 <= west < east <= 180.0
        and -90.0 <= south < north <= 90.0
    ):
        raise MapPlotterError("Authored-coastline PBF header bounds are malformed.")
    return bounds, source_timestamp, timestamp_kind, generator


def _covers(
    outer: Sequence[float], inner: Sequence[float]
) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _directed_cycles(
    ways: Mapping[int, _RawCoastlineWay],
) -> tuple[tuple[tuple[int, ...], ...], dict[str, Any]]:
    """Consume every way in authored direction; never reverse or proximity-join."""

    closed = tuple(sorted(way_id for way_id, way in ways.items() if way.node_refs[0] == way.node_refs[-1]))
    open_way_ids = tuple(sorted(set(ways).difference(closed)))
    by_start: dict[int, list[int]] = defaultdict(list)
    by_end: dict[int, list[int]] = defaultdict(list)
    for way_id in open_way_ids:
        refs = ways[way_id].node_refs
        by_start[refs[0]].append(way_id)
        by_end[refs[-1]].append(way_id)
    start_histogram = Counter(len(value) for value in by_start.values())
    end_histogram = Counter(len(value) for value in by_end.values())
    bad_nodes = sorted(
        node_id
        for node_id in set(by_start) | set(by_end)
        if len(by_start.get(node_id, ())) != 1 or len(by_end.get(node_id, ())) != 1
    )
    if bad_nodes:
        raise MapPlotterError(
            "Authored coastline is not a directed one-in/one-out graph at "
            f"{len(bad_nodes)} nonclosed endpoints (first: {bad_nodes[:8]})."
        )
    successor = {
        way_id: by_start[ways[way_id].node_refs[-1]][0]
        for way_id in open_way_ids
    }
    remaining = set(open_way_ids)
    stitched: list[tuple[int, ...]] = []
    while remaining:
        first = min(remaining)
        cycle: list[int] = []
        current = first
        while current in remaining:
            cycle.append(current)
            remaining.remove(current)
            current = successor[current]
        if current != first:
            raise MapPlotterError(
                "Authored coastline directed traversal entered another cycle."
            )
        stitched.append(tuple(cycle))
    cycles = tuple(sorted((*( (way_id,) for way_id in closed), *stitched)))
    consumed = {way_id for cycle in cycles for way_id in cycle}
    if consumed != set(ways):
        raise MapPlotterError("Authored coastline cycle assembly lost source ways.")
    return cycles, {
        "raw_closed_way_count": len(closed),
        "nonclosed_way_count": len(open_way_ids),
        "directed_stitched_cycle_count": len(stitched),
        "directed_cycle_count": len(cycles),
        "nonclosed_start_multiplicity_histogram": {
            str(key): value for key, value in sorted(start_histogram.items())
        },
        "nonclosed_end_multiplicity_histogram": {
            str(key): value for key, value in sorted(end_histogram.items())
        },
        "directed_endpoint_failure_count": 0,
        "reversed_way_count": 0,
        "proximity_join_count": 0,
        "consumed_way_count": len(consumed),
        "source_way_parity": True,
    }


def _cycle_refs(
    cycle: Sequence[int], ways: Mapping[int, _RawCoastlineWay]
) -> tuple[int, ...]:
    result: list[int] = []
    for index, way_id in enumerate(cycle):
        refs = ways[way_id].node_refs
        if index and result[-1] != refs[0]:
            raise MapPlotterError(
                "Authored coastline cycle lost an exact source-node boundary."
            )
        result.extend(refs if index == 0 else refs[1:])
    if len(result) < 4 or result[0] != result[-1]:
        raise MapPlotterError("Authored coastline directed cycle is not closed.")
    return tuple(result)


def _ring_covering_indices(
    polygons: Sequence[Polygon], point: tuple[float, float]
) -> tuple[int, ...]:
    marker = Point(point)
    return tuple(
        index for index, polygon in enumerate(polygons) if polygon.covers(marker)
    )


def _geometry_sha256(ways: Iterable[OsmCoastlineWay]) -> str:
    digest = hashlib.sha256()
    for way in ways:
        payload = {
            "geometry": way.geometry,
            "node_refs": way.node_refs,
            "osm_way_id": way.osm_way_id,
        }
        digest.update(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _stream_ways_and_nodes(
    osmium: Any, path: Path, source_stat: Any
) -> tuple[dict[int, _RawCoastlineWay], dict[int, tuple[float, float]], dict[str, Any]]:
    processor_type = getattr(osmium, "FileProcessor", None)
    filters = getattr(osmium, "filter", None)
    key_filter_type = getattr(filters, "KeyFilter", None)
    id_filter_type = getattr(filters, "IdFilter", None)
    osm_types = getattr(osmium, "osm", None)
    if (
        processor_type is None
        or key_filter_type is None
        or id_filter_type is None
        or osm_types is None
    ):
        raise MapPlotterError(
            "PyOsmium lacks FileProcessor with native KeyFilter and IdFilter."
        )

    ways: dict[int, _RawCoastlineWay] = {}
    natural_callback_count = 0
    try:
        processor = processor_type(str(path), entities=osm_types.WAY).with_filter(
            key_filter_type("natural")
        )
        for value in processor:
            natural_callback_count += 1
            tags = _tags(value)
            if tags.get("natural", "").strip().casefold() != "coastline":
                continue
            way_id = int(value.id)
            if way_id in ways:
                raise MapPlotterError(f"PBF repeats authored coastline way {way_id}.")
            refs = tuple(_node_ref(node) for node in value.nodes)
            if len(refs) < 2:
                raise MapPlotterError(
                    f"Authored coastline way {way_id} has fewer than two nodes."
                )
            if any(first == second for first, second in zip(refs, refs[1:], strict=False)):
                raise MapPlotterError(
                    f"Authored coastline way {way_id} repeats consecutive nodes."
                )
            ways[way_id] = _RawCoastlineWay(
                osm_way_id=way_id,
                node_refs=refs,
                osm_version=_optional_int(value, "version"),
                osm_timestamp=_timestamp(getattr(value, "timestamp", None)),
            )
        if _stat_signature(source_stat) != _stat_signature(path.stat()):
            raise MapPlotterError("Authored-coastline PBF changed during way scan.")
    except MapPlotterError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MapPlotterError(f"Cannot stream authored coastline ways: {exc}") from exc
    if not ways:
        raise MapPlotterError("Pinned PBF contains no natural=coastline ways.")

    required_nodes = {node_id for way in ways.values() for node_id in way.node_refs}
    nodes: dict[int, tuple[float, float]] = {}
    node_callback_count = 0
    try:
        processor = processor_type(str(path), entities=osm_types.NODE).with_filter(
            id_filter_type(required_nodes)
        )
        for value in processor:
            node_callback_count += 1
            node_id = int(value.id)
            if node_id in nodes:
                raise MapPlotterError(
                    f"PBF repeats required coastline node {node_id}."
                )
            location = getattr(value, "location", None)
            valid = getattr(location, "valid", None)
            if location is None or (callable(valid) and not valid()):
                continue
            lon = float(location.lon)
            lat = float(location.lat)
            if not (
                isfinite(lon)
                and isfinite(lat)
                and -180.0 <= lon <= 180.0
                and -85.0 <= lat <= 85.0
            ):
                continue
            nodes[node_id] = (lon, lat)
        if _stat_signature(source_stat) != _stat_signature(path.stat()):
            raise MapPlotterError("Authored-coastline PBF changed during node scan.")
    except MapPlotterError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MapPlotterError(f"Cannot stream authored coastline nodes: {exc}") from exc
    missing = sorted(required_nodes.difference(nodes))
    if missing:
        raise MapPlotterError(
            "Authored-coastline PBF is reference-incomplete: "
            f"{len(missing)} missing nodes (first: {missing[:8]})."
        )
    return ways, nodes, {
        "natural_tagged_way_callback_count": natural_callback_count,
        "selected_coastline_way_count": len(ways),
        "required_node_count": len(required_nodes),
        "selected_node_callback_count": node_callback_count,
        "resolved_node_count": len(nodes),
        "missing_node_count": 0,
        "reference_complete": True,
        "entity_filter_backend": "pyosmium FileProcessor KeyFilter + IdFilter",
        "pbf_pass_count": 2,
    }


def load_great_britain_coastline(
    path: Path,
    *,
    expected_sha256: str,
    bounds_wgs84: tuple[float, float, float, float] = DEFAULT_GB_BOUNDS,
) -> GreatBritainCoastline:
    """Load exact authored coastline and fail closed on topology or scope drift."""

    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise MapPlotterError(
            "Authored-coastline expected_sha256 must be lower-case SHA-256."
        )
    try:
        resolved = path.expanduser().resolve(strict=True)
        source_stat = resolved.stat()
    except OSError as exc:
        raise MapPlotterError(f"Cannot inspect authored-coastline PBF {path}: {exc}") from exc
    if not resolved.is_file():
        raise MapPlotterError(
            f"Authored-coastline PBF is not a regular file: {resolved}"
        )
    actual_sha256 = _sha256_path(resolved)
    if actual_sha256 != expected_sha256:
        raise MapPlotterError(
            "Authored-coastline PBF SHA-256 mismatch: expected "
            f"{expected_sha256}, got {actual_sha256}."
        )
    if _stat_signature(source_stat) != _stat_signature(resolved.stat()):
        raise MapPlotterError("Authored-coastline PBF changed while hashing.")

    osmium = _import_osmium()
    header_bounds, source_timestamp, timestamp_kind, generator = _read_header(
        osmium, resolved
    )
    if not _covers(header_bounds, bounds_wgs84):
        raise MapPlotterError(
            "Authored-coastline PBF header does not cover the complete GB "
            f"cartographic bounds {bounds_wgs84}."
        )
    raw_ways, nodes, streaming_audit = _stream_ways_and_nodes(
        osmium, resolved, source_stat
    )
    cycles, topology_audit = _directed_cycles(raw_ways)
    cycle_refs = tuple(_cycle_refs(cycle, raw_ways) for cycle in cycles)
    polygons: list[Polygon] = []
    invalid_cycle_ids: list[int] = []
    nonpositive_orientation_cycle_ids: list[int] = []
    for cycle_index, refs in enumerate(cycle_refs):
        polygon = Polygon([nodes[node_id] for node_id in refs])
        if polygon.is_empty or polygon.area <= 0.0 or not polygon.is_valid:
            invalid_cycle_ids.append(cycle_index)
        # The shoelace sign is positive when land is left of authored travel.
        signed_area = sum(
            nodes[first][0] * nodes[second][1]
            - nodes[second][0] * nodes[first][1]
            for first, second in zip(refs, refs[1:], strict=False)
        )
        if signed_area <= 0.0:
            nonpositive_orientation_cycle_ids.append(cycle_index)
        polygons.append(polygon)
    if invalid_cycle_ids:
        raise MapPlotterError(
            "Authored coastline contains invalid directed land rings: "
            f"{invalid_cycle_ids[:8]}."
        )
    if nonpositive_orientation_cycle_ids:
        raise MapPlotterError(
            "Authored coastline violates the OSM land-left direction rule for "
            f"cycles {nonpositive_orientation_cycle_ids[:8]}."
        )

    required_anchor_cycles: dict[str, int] = {}
    for name, point in REQUIRED_GB_LAND_ANCHORS.items():
        matches = _ring_covering_indices(polygons, point)
        if len(matches) != 1:
            raise MapPlotterError(
                f"Required GB land anchor {name!r} is covered by {len(matches)} "
                "authored coastline rings; expected exactly one."
            )
        required_anchor_cycles[name] = matches[0]
    denied_anchor_matches = {
        name: list(_ring_covering_indices(polygons, point))
        for name, point in DENIED_NON_GB_LAND_ANCHORS.items()
    }
    denied_present = {
        name: matches for name, matches in denied_anchor_matches.items() if matches
    }
    if denied_present:
        raise MapPlotterError(
            "Authored GB coastline source unexpectedly contains denied non-GB "
            f"jurisdictions: {sorted(denied_present)}."
        )

    selection = box(*bounds_wgs84)
    selected_cycle_indices = tuple(
        index
        for index, polygon in enumerate(polygons)
        if polygon.intersects(selection)
        and not polygon.intersection(selection).is_empty
    )
    selected_cycle_set = set(selected_cycle_indices)
    missing_required = sorted(
        name
        for name, cycle_index in required_anchor_cycles.items()
        if cycle_index not in selected_cycle_set
    )
    if missing_required:
        raise MapPlotterError(
            "Complete-GB cartographic bounds exclude required islands: "
            + ", ".join(missing_required)
        )
    selected_way_ids = {
        way_id for index in selected_cycle_indices for way_id in cycles[index]
    }
    excluded_way_ids = tuple(sorted(set(raw_ways).difference(selected_way_ids)))
    pinned_source_invariant_verified = False
    if actual_sha256 == PINNED_GB_PBF_2026_08_07_SHA256:
        observed = {
            "source_byte_count": source_stat.st_size,
            "source_way_count": len(raw_ways),
            "required_node_count": streaming_audit.get("required_node_count"),
            "raw_closed_way_count": topology_audit["raw_closed_way_count"],
            "nonclosed_way_count": topology_audit["nonclosed_way_count"],
            "directed_stitched_cycle_count": topology_audit[
                "directed_stitched_cycle_count"
            ],
            "source_cycle_count": len(cycles),
            "selected_cycle_count": len(selected_cycle_indices),
            "selected_way_count": len(selected_way_ids),
            "excluded_way_ids": excluded_way_ids,
        }
        if observed != _PINNED_GB_PBF_2026_08_07_INVARIANTS:
            raise MapPlotterError(
                "Pinned 2026-08-07 GB coastline invariant changed: "
                f"observed {observed}."
            )
        pinned_source_invariant_verified = True
    output: list[OsmCoastlineWay] = []
    for way_id in sorted(selected_way_ids):
        raw = raw_ways[way_id]
        geometry = tuple(nodes[node_id] for node_id in raw.node_refs)
        longitudes = [point[0] for point in geometry]
        latitudes = [point[1] for point in geometry]
        output.append(
            OsmCoastlineWay(
                osm_way_id=way_id,
                node_refs=raw.node_refs,
                geometry=geometry,
                bounds_wgs84=(
                    min(longitudes),
                    min(latitudes),
                    max(longitudes),
                    max(latitudes),
                ),
                osm_version=raw.osm_version,
                osm_timestamp=raw.osm_timestamp,
            )
        )
    ways = tuple(output)
    selected_source_objects = [way.source_object for way in ways]
    audit: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": OSM_GB_COASTLINE_POLICY_VERSION,
        "source_product": "OpenStreetMap Great Britain extract",
        "source_url": OSM_GB_COASTLINE_SOURCE_URL,
        "source_sha256": actual_sha256,
        "source_byte_count": source_stat.st_size,
        "source_timestamp": source_timestamp,
        "source_timestamp_kind": timestamp_kind,
        "source_generator": generator,
        "header_bounds_wgs84": list(header_bounds),
        "cartographic_selection_bounds_wgs84": list(bounds_wgs84),
        "geographic_scope": GB_CONTEXT_GEOGRAPHIC_SCOPE,
        "authored_coastline_geometry_used": True,
        "natural_coastline_source_tag": "natural=coastline",
        "streaming": streaming_audit,
        "topology": {
            **topology_audit,
            "invalid_polygon_count": 0,
            "nonpositive_land_left_orientation_count": 0,
            "exact_node_join_only": True,
            "individually_closed_ways_remain_independent_at_shared_nodes": True,
        },
        "jurisdiction": {
            "required_gb_anchor_cycle_indices": dict(
                sorted(required_anchor_cycles.items())
            ),
            "required_gb_anchor_count": len(required_anchor_cycles),
            "denied_non_gb_anchor_matches": dict(sorted(denied_anchor_matches.items())),
            "denied_non_gb_anchor_match_count": 0,
            "northern_ireland_included": False,
            "northern_ireland_exclusion_verified": True,
            "isle_of_man_included": False,
            "channel_islands_included": False,
        },
        "source_cycle_count": len(cycles),
        "selected_cycle_count": len(selected_cycle_indices),
        "bounds_excluded_cycle_count": len(cycles) - len(selected_cycle_indices),
        "bounds_excluded_way_ids": list(excluded_way_ids),
        "selected_way_count": len(ways),
        "pinned_2026_08_07_source_invariant_verified": (
            pinned_source_invariant_verified
        ),
        "pinned_2026_08_07_expected_invariant": (
            dict(_PINNED_GB_PBF_2026_08_07_INVARIANTS)
            if actual_sha256 == PINNED_GB_PBF_2026_08_07_SHA256
            else None
        ),
        "selected_source_object_count": len(selected_source_objects),
        "selected_source_object_lineage_sha256": hashlib.sha256(
            "\0".join(selected_source_objects).encode("ascii")
        ).hexdigest(),
        "geometry_sha256": _geometry_sha256(ways),
        "invented_connector_count": 0,
        "reversed_way_count": 0,
        "proximity_join_count": 0,
        "zoomstack_sea_fill_geometry_used": False,
        "operator_service_geometry_claimed": False,
        "permitted_use": "authored Great Britain coastline context only",
    }
    audit["ordered_evidence_sha256"] = hashlib.sha256(
        json.dumps(
            audit,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return GreatBritainCoastline(
        source_path=resolved,
        source_sha256=actual_sha256,
        source_byte_count=source_stat.st_size,
        source_timestamp=source_timestamp,
        source_timestamp_kind=timestamp_kind,
        header_bounds_wgs84=header_bounds,
        bounds_wgs84=bounds_wgs84,
        ways=ways,
        audit=audit,
    )


__all__ = [
    "DENIED_NON_GB_LAND_ANCHORS",
    "GreatBritainCoastline",
    "OSM_GB_COASTLINE_POLICY_VERSION",
    "OSM_GB_COASTLINE_SOURCE_URL",
    "PINNED_GB_PBF_2026_08_07_SHA256",
    "OsmCoastlineWay",
    "REQUIRED_GB_LAND_ANCHORS",
    "load_great_britain_coastline",
]
