"""Local OpenStreetMap PBF ingestion with canonical source provenance.

The optional :mod:`osmium` package (PyOsmium) supplies reference-complete way
locations and libosmium's multipolygon assembler.  This module intentionally
does not convert PBF data into an Overpass-shaped intermediate document: doing
so would discard node references, relation membership, object versions, and
area ring semantics before the cartographic pipeline can use them.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Iterable, Iterator

from .features import (
    FALSE_AREA_HIGHWAY_VALUES,
    _classify_supported,
    _classify_supported_layers,
    _ring_area,
    highway_coverage_from_tag_counts,
    is_identified_heritage_site,
)
from .models import AcquisitionResult, BoundingBox, MapFeature, MapPlotterError


_AREA_LAYERS = frozenset({"water_areas", "green_space", "buildings", "road_areas"})
_PBF_CHUNK_BYTES = 4 * 1024 * 1024
_HEADER_FIELDS = (
    "generator",
    "timestamp",
    "osmosis_replication_timestamp",
    "osmosis_replication_sequence_number",
    "osmosis_replication_base_url",
)


@dataclass(frozen=True)
class _ObjectProvenance:
    osm_version: int | None
    osm_timestamp: str | None
    osm_changeset: int | None
    osm_uid: int | None
    osm_user: str | None
    node_refs: tuple[str, ...] = ()
    relation_members: tuple[tuple[str, str, str], ...] = ()


class _RelationProvenanceStore:
    """Bound relation metadata on disk while libosmium performs two passes.

    PyOsmium delivers every relation callback before assembled area callbacks,
    so an ordinary dictionary grows with the entire regional extract. SQLite
    keeps that unavoidable staging state out of Python heap memory while still
    preserving exact relation members and object metadata for emitted rings.
    """

    backend = "temporary-sqlite"

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(
            prefix="city-map-plotter-pbf-relations-"
        )
        self.path = Path(self._directory.name) / "relations.sqlite3"
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.execute("PRAGMA journal_mode=OFF")
            self._connection.execute("PRAGMA synchronous=OFF")
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute(
                "CREATE TABLE relation_provenance "
                "(osm_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise MapPlotterError(
                f"Could not create bounded PBF relation staging storage: {exc}"
            ) from exc

    def put(self, osm_id: str, provenance: _ObjectProvenance) -> None:
        payload = json.dumps(
            {
                "osm_version": provenance.osm_version,
                "osm_timestamp": provenance.osm_timestamp,
                "osm_changeset": provenance.osm_changeset,
                "osm_uid": provenance.osm_uid,
                "osm_user": provenance.osm_user,
                "node_refs": provenance.node_refs,
                "relation_members": provenance.relation_members,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self._require_connection().execute(
                "INSERT OR REPLACE INTO relation_provenance (osm_id, payload) "
                "VALUES (?, ?)",
                (osm_id, payload),
            )
        except sqlite3.Error as exc:
            raise MapPlotterError(
                f"Could not stage PBF relation {osm_id}: {exc}"
            ) from exc

    def pop(self, osm_id: str, default: _ObjectProvenance) -> _ObjectProvenance:
        try:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT payload FROM relation_provenance WHERE osm_id = ?",
                (osm_id,),
            ).fetchone()
            if row is None:
                return default
            connection.execute(
                "DELETE FROM relation_provenance WHERE osm_id = ?", (osm_id,)
            )
            record = json.loads(str(row[0]))
            return _ObjectProvenance(
                osm_version=record["osm_version"],
                osm_timestamp=record["osm_timestamp"],
                osm_changeset=record["osm_changeset"],
                osm_uid=record["osm_uid"],
                osm_user=record["osm_user"],
                node_refs=tuple(str(item) for item in record["node_refs"]),
                relation_members=tuple(
                    (str(member_type), str(ref), str(role))
                    for member_type, ref, role in record["relation_members"]
                ),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.Error,
        ) as exc:
            raise MapPlotterError(
                f"Could not restore staged PBF relation {osm_id}: {exc}"
            ) from exc

    def __len__(self) -> int:
        try:
            row = (
                self._require_connection()
                .execute("SELECT COUNT(*) FROM relation_provenance")
                .fetchone()
            )
        except sqlite3.Error as exc:
            raise MapPlotterError(
                f"Could not inspect PBF relation staging storage: {exc}"
            ) from exc
        return int(row[0]) if row is not None else 0

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise MapPlotterError("PBF relation staging storage is closed.")
        return self._connection

    def close(self) -> None:
        connection, self._connection = self._connection, None
        try:
            if connection is not None:
                connection.close()
        finally:
            self._directory.cleanup()


def _import_osmium() -> Any:
    try:
        return importlib.import_module("osmium")
    except ModuleNotFoundError as exc:
        if exc.name != "osmium":
            raise
        raise MapPlotterError(
            "Reading --input-pbf requires the optional free/open-source "
            "PyOsmium dependency. Install it with "
            "'python -m pip install \"city-map-plotter[pbf]\"' or "
            "'python -m pip install osmium'."
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(_PBF_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Could not read local PBF file {path}: {exc}") from exc
    return digest.hexdigest()


def _stat_signature(value: Any) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _normalise_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"0", "1970-01-01T00:00:00Z"}:
        return None
    return text


def _integer_attribute(value: Any, name: str) -> int | None:
    raw = getattr(value, name, None)
    if callable(raw):
        raw = raw()
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _string_attribute(value: Any, name: str) -> str | None:
    raw = getattr(value, name, None)
    if callable(raw):
        raw = raw()
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _tags_dict(value: Any) -> dict[str, str]:
    tags = getattr(value, "tags", value)
    if tags is None:
        return {}
    items = getattr(tags, "items", None)
    if callable(items):
        try:
            return {str(key): str(item) for key, item in items()}
        except (TypeError, ValueError):
            pass

    result: dict[str, str] = {}
    try:
        iterator = iter(tags)
    except TypeError:
        return result
    for tag in iterator:
        key = getattr(tag, "k", None)
        item = getattr(tag, "v", None)
        if key is not None and item is not None:
            result[str(key)] = str(item)
            continue
        if isinstance(tag, tuple) and len(tag) == 2:
            result[str(tag[0])] = str(tag[1])
    return result


def _member_type(value: Any) -> str:
    text = str(value).strip().casefold()
    aliases = {
        "n": "node",
        "node": "node",
        "w": "way",
        "way": "way",
        "r": "relation",
        "relation": "relation",
    }
    if text in aliases:
        return aliases[text]
    name = getattr(value, "name", None)
    return aliases.get(str(name).casefold(), text or "unknown")


def _relation_members(value: Any) -> tuple[tuple[str, str, str], ...]:
    result: list[tuple[str, str, str]] = []
    for member in getattr(value, "members", ()):
        member_ref = _integer_attribute(member, "ref")
        if member_ref is None:
            continue
        result.append(
            (
                _member_type(getattr(member, "type", "unknown")),
                str(member_ref),
                _string_attribute(member, "role") or "",
            )
        )
    return tuple(result)


def _location(node: Any) -> tuple[float, float] | None:
    location = getattr(node, "location", node)
    valid = getattr(location, "valid", None)
    try:
        if callable(valid) and not valid():
            return None
        latitude = getattr(node, "lat", getattr(location, "lat", None))
        longitude = getattr(node, "lon", getattr(location, "lon", None))
        if callable(latitude):
            latitude = latitude()
        if callable(longitude):
            longitude = longitude()
        if latitude is None or longitude is None:
            return None
        coordinate = (float(latitude), float(longitude))
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
    if not all(isfinite(item) for item in coordinate):
        return None
    if not (-85 <= coordinate[0] <= 85 and -180 <= coordinate[1] <= 180):
        return None
    return coordinate


def _node_sequence(
    nodes: Iterable[Any], *, close_ring: bool = False
) -> tuple[list[tuple[float, float]], tuple[str, ...]]:
    points: list[tuple[float, float]] = []
    node_refs: list[str] = []
    for node in nodes:
        coordinate = _location(node)
        if coordinate is None:
            # A missing reference must split a line. PyOsmium's locations=True
            # normally prevents this; rejecting the whole entity is safer than
            # silently bridging an unresolved gap.
            return [], ()
        node_ref = _integer_attribute(node, "ref")
        if node_ref is None:
            node_ref = _integer_attribute(node, "id")
        points.append(coordinate)
        node_refs.append(str(node_ref) if node_ref is not None else "unknown")

    if close_ring and points and points[0] != points[-1]:
        points.append(points[0])
        node_refs.append(node_refs[0])
    return points, tuple(node_refs)


def _overlaps_bbox(points: list[tuple[float, float]], bbox: BoundingBox) -> bool:
    if not points:
        return False
    south = min(point[0] for point in points)
    north = max(point[0] for point in points)
    west = min(point[1] for point in points)
    east = max(point[1] for point in points)
    return not (
        east < bbox.west or west > bbox.east or north < bbox.south or south > bbox.north
    )


def _object_provenance(
    value: Any,
    *,
    node_refs: tuple[str, ...] = (),
    relation_members: tuple[tuple[str, str, str], ...] = (),
) -> _ObjectProvenance:
    return _ObjectProvenance(
        osm_version=_integer_attribute(value, "version"),
        osm_timestamp=_normalise_timestamp(getattr(value, "timestamp", None)),
        osm_changeset=_integer_attribute(value, "changeset"),
        osm_uid=_integer_attribute(value, "uid"),
        osm_user=_string_attribute(value, "user"),
        node_refs=node_refs,
        relation_members=relation_members,
    )


def _feature(
    *,
    layer: str,
    points: list[tuple[float, float]],
    osm_type: str,
    osm_id: str,
    part: str,
    tags: dict[str, str],
    provenance: _ObjectProvenance,
    geometry_type: str = "line",
    ring_role: str | None = None,
    outer_ring_part: str | None = None,
    node_refs: tuple[str, ...] | None = None,
) -> MapFeature:
    canonical_tags = dict(tags)
    if ring_role in {"outer", "inner"}:
        canonical_tags["mapplot:area-role"] = ring_role
    return MapFeature(
        layer=layer,
        points=points,
        osm_type=osm_type,
        osm_id=osm_id,
        part=part,
        tags=canonical_tags,
        geometry_type=geometry_type,
        ring_role=ring_role,
        outer_ring_part=outer_ring_part,
        node_refs=provenance.node_refs if node_refs is None else node_refs,
        relation_members=provenance.relation_members,
        osm_version=provenance.osm_version,
        osm_timestamp=provenance.osm_timestamp,
        osm_changeset=provenance.osm_changeset,
        osm_uid=provenance.osm_uid,
        osm_user=provenance.osm_user,
    )


def _call_bool(value: Any, name: str) -> bool:
    raw = getattr(value, name, False)
    return bool(raw() if callable(raw) else raw)


def _call_id(value: Any, name: str) -> str:
    raw = getattr(value, name, None)
    if callable(raw):
        raw = raw()
    if raw is None:
        raw = getattr(value, "id", "unknown")
    return str(raw)


def _iter_inner_rings(area: Any, outer: Any) -> Iterator[Any]:
    inner_rings = getattr(area, "inner_rings", None)
    if not callable(inner_rings):
        return iter(())
    return iter(inner_rings(outer))


def _canonical_feature_hash(features: list[MapFeature]) -> str:
    digest = hashlib.sha256()
    ordered = sorted(
        features,
        key=lambda item: (item.osm_type, item.osm_id, item.part, item.layer),
    )
    for item in ordered:
        record = {
            "layer": item.layer,
            "geometry_type": item.geometry_type,
            "ring_role": item.ring_role,
            "outer_ring_part": item.outer_ring_part,
            "osm_type": item.osm_type,
            "osm_id": item.osm_id,
            "part": item.part,
            "points": item.points,
            "node_refs": item.node_refs,
            "relation_members": item.relation_members,
            "tags": item.tags,
            "version": item.osm_version,
            "timestamp": item.osm_timestamp,
            "changeset": item.osm_changeset,
            "uid": item.osm_uid,
            "user": item.osm_user,
        }
        digest.update(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _read_header(osmium: Any, path: Path) -> dict[str, Any]:
    io_module = getattr(osmium, "io", None)
    reader_type = getattr(io_module, "Reader", None)
    if reader_type is None:
        return {}
    reader = None
    result: dict[str, Any] = {}
    try:
        reader = reader_type(str(path))
        header = reader.header()
        getter = getattr(header, "get", None)
        if not callable(getter):
            return result
        for name in _HEADER_FIELDS:
            try:
                value = getter(name)
            except (KeyError, RuntimeError):
                continue
            if value is not None and str(value).strip():
                result[name] = str(value).strip()
        box_reader = getattr(header, "box", None)
        if callable(box_reader):
            try:
                box = box_reader()
                valid = getattr(box, "valid", None)
                if callable(valid) and valid():
                    bottom_left = box.bottom_left
                    top_right = box.top_right
                    result["bounding_box_wgs84"] = {
                        "west": float(bottom_left.lon),
                        "south": float(bottom_left.lat),
                        "east": float(top_right.lon),
                        "north": float(top_right.lat),
                    }
            except (AttributeError, RuntimeError, TypeError, ValueError):
                # A missing or malformed header box remains explicit later as
                # unknown acquisition coverage; entity parsing is still valid.
                pass
    except (OSError, RuntimeError):
        # Header metadata is useful provenance, but the entity pass below is
        # authoritative and will report an actionable read error if necessary.
        return {}
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()
    return result


def _handler_for(
    osmium: Any,
    *,
    bbox: BoundingBox,
    enabled_layers: set[str],
    relation_store: _RelationProvenanceStore,
) -> Any:
    base = getattr(osmium, "SimpleHandler", None)
    if base is None:
        raise MapPlotterError(
            "The installed osmium package does not provide SimpleHandler; "
            "install a current PyOsmium release."
        )

    class _PbfHandler(base):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.features: list[MapFeature] = []
            # Canonical counters describe only geometry which can affect the
            # requested extraction. File-wide diagnostics are kept separately
            # so a large regional source cannot make the bbox result look as if
            # it contained failures or assembled areas from elsewhere.
            self.extraction_invalid_geometry_count = 0
            self.file_scan_invalid_geometry_count = 0
            self.file_scan_relevant_way_count = 0
            self.file_scan_relevant_relation_count = 0
            self.file_scan_area_callback_count = 0
            self.file_scan_successful_way_area_count = 0
            self.file_scan_successful_relation_area_count = 0
            self.file_scan_failed_way_area_count = 0
            self.file_scan_failed_relation_area_count = 0
            self.extraction_highway_tag_counts: Counter[tuple[str, str]] = Counter()
            self.extraction_area_highway_tag_counts: Counter[str] = Counter()
            self.latest_timestamp: str | None = None
            self.relation_store = relation_store
            self.area_way_fallbacks: dict[str, MapFeature] = {}
            # These sets are deliberately extraction-scoped. In particular,
            # they must not grow once per building in a country-sized PBF.
            self.assembled_area_ways: set[str] = set()
            self.assembled_area_relations: set[str] = set()
            self.assembled_relation_way_members: set[tuple[str, str]] = set()
            self.failed_extraction_area_ways: set[str] = set()
            self.failed_extraction_area_relations: set[str] = set()

        def _record_timestamp(self, value: str | None) -> None:
            if value is not None and (
                self.latest_timestamp is None or value > self.latest_timestamp
            ):
                self.latest_timestamp = value

        def relation(self, relation: Any) -> None:
            tags = _tags_dict(relation)
            layer = _classify_supported(tags)
            if layer is None or layer not in enabled_layers:
                return
            if (
                is_identified_heritage_site(tags)
                and tags.get("type", "").strip().casefold() != "multipolygon"
            ):
                return
            if (
                layer not in _AREA_LAYERS
                and tags.get("type") not in {"multipolygon", "boundary"}
                and tags.get("area") != "yes"
            ):
                return
            members = _relation_members(relation)
            provenance = _object_provenance(relation, relation_members=members)
            relation_id = _call_id(relation, "id")
            self.file_scan_relevant_relation_count += 1
            self.relation_store.put(relation_id, provenance)
            self._record_timestamp(provenance.osm_timestamp)

        def way(self, way: Any) -> None:
            tags = _tags_dict(way)
            selected_layers = tuple(
                layer
                for layer in _classify_supported_layers(tags)
                if layer in enabled_layers
            )
            highway = tags.get("highway", "").strip().casefold()
            audits_highways = bool(
                highway
                and enabled_layers
                & {
                    "roads_major",
                    "roads_secondary",
                    "roads_local",
                    "roads_other",
                    "paths",
                    "road_areas",
                }
            )
            if not selected_layers and not audits_highways:
                return
            points, node_refs = _node_sequence(getattr(way, "nodes", ()))
            if len(points) < 2:
                # Without usable coordinates there is no truthful way to say
                # that this file-wide failure intersects the requested bbox.
                self.file_scan_invalid_geometry_count += 1
                return
            if is_identified_heritage_site(tags) and _ring_area(points) <= 0:
                # Bare historic castle/palace tags denote a site footprint,
                # never arbitrary linear heritage geometry. Only libosmium-
                # assemblable closed ways enter the buildings area path.
                return
            overlaps_bbox = _overlaps_bbox(points, bbox)
            if audits_highways and overlaps_bbox:
                self.extraction_highway_tag_counts[
                    (highway, tags.get("construction", "").strip().casefold())
                ] += 1
            area_highway = tags.get("area:highway", "").strip().casefold()
            if overlaps_bbox and area_highway not in FALSE_AREA_HIGHWAY_VALUES:
                self.extraction_area_highway_tag_counts[area_highway] += 1
            if not selected_layers:
                return
            self.file_scan_relevant_way_count += 1
            way_id = _call_id(way, "id")
            provenance = _object_provenance(way, node_refs=node_refs)
            self._record_timestamp(provenance.osm_timestamp)
            for layer in selected_layers:
                feature = _feature(
                    layer=layer,
                    points=points,
                    osm_type="way",
                    osm_id=way_id,
                    part="way:0",
                    tags=tags,
                    provenance=provenance,
                )
                if layer in _AREA_LAYERS or tags.get("area") == "yes":
                    # The area callback will supply assembled closed rings. Keep a
                    # fallback so malformed/open source areas remain auditable,
                    # but retain geometry only when its envelope can affect this
                    # extraction. This is the critical memory bound for regional
                    # PBFs containing millions of building ways. An OSM area has
                    # one primary boundary semantics, so do not create a second
                    # fallback for an incidental co-tagged linear overlay.
                    feature.geometry_type = "unassembled_area_boundary"
                    if overlaps_bbox and way_id not in self.area_way_fallbacks:
                        self.area_way_fallbacks[way_id] = feature
                    continue
                if overlaps_bbox:
                    self.features.append(feature)

        def area(self, area: Any) -> None:
            tags = _tags_dict(area)
            layer = _classify_supported(tags)
            if layer is None or layer not in enabled_layers:
                return

            from_way = _call_bool(area, "from_way")
            if (
                is_identified_heritage_site(tags)
                and not from_way
                and tags.get("type", "").strip().casefold() != "multipolygon"
            ):
                return
            osm_type = "way" if from_way else "relation"
            osm_id = _call_id(area, "orig_id")
            self.file_scan_area_callback_count += 1
            if from_way:
                fallback = self.area_way_fallbacks.get(osm_id)
                provenance = (
                    _ObjectProvenance(
                        osm_version=fallback.osm_version,
                        osm_timestamp=fallback.osm_timestamp,
                        osm_changeset=fallback.osm_changeset,
                        osm_uid=fallback.osm_uid,
                        osm_user=fallback.osm_user,
                        node_refs=fallback.node_refs,
                    )
                    if fallback is not None
                    else _object_provenance(area)
                )
            else:
                fallback = None
                provenance = self.relation_store.pop(osm_id, _object_provenance(area))
            self._record_timestamp(provenance.osm_timestamp)

            relation_way_members = {
                (ref, layer)
                for member_type, ref, _role in provenance.relation_members
                if member_type == "way"
            }
            fallback_overlaps_bbox = fallback is not None or any(
                ref in self.area_way_fallbacks
                and self.area_way_fallbacks[ref].layer == member_layer
                for ref, member_layer in relation_way_members
            )

            def record_failed_assembly(*, affects_extraction: bool) -> None:
                if from_way:
                    self.file_scan_failed_way_area_count += 1
                    if affects_extraction:
                        self.failed_extraction_area_ways.add(osm_id)
                else:
                    self.file_scan_failed_relation_area_count += 1
                    if affects_extraction:
                        self.failed_extraction_area_relations.add(osm_id)

            outer_rings = getattr(area, "outer_rings", None)
            if not callable(outer_rings):
                self.file_scan_invalid_geometry_count += 1
                if fallback_overlaps_bbox:
                    self.extraction_invalid_geometry_count += 1
                record_failed_assembly(affects_extraction=fallback_overlaps_bbox)
                return

            staged_features: list[MapFeature] = []
            valid_outer_ring_count = 0
            invalid_geometry_count = 0
            candidate_overlaps_bbox = fallback_overlaps_bbox
            assembled_geometry_overlaps_bbox = False
            for outer_index, outer in enumerate(outer_rings()):
                outer_points, outer_refs = _node_sequence(outer, close_ring=True)
                outer_part = f"outer:{outer_index}"
                if len(outer_points) < 4:
                    invalid_geometry_count += 1
                    candidate_overlaps_bbox = candidate_overlaps_bbox or _overlaps_bbox(
                        outer_points, bbox
                    )
                    continue
                valid_outer_ring_count += 1
                outer_overlaps_bbox = _overlaps_bbox(outer_points, bbox)
                candidate_overlaps_bbox = candidate_overlaps_bbox or outer_overlaps_bbox
                assembled_geometry_overlaps_bbox = (
                    assembled_geometry_overlaps_bbox or outer_overlaps_bbox
                )
                if outer_overlaps_bbox:
                    staged_features.append(
                        _feature(
                            layer=layer,
                            points=outer_points,
                            osm_type=osm_type,
                            osm_id=osm_id,
                            part=outer_part,
                            tags=tags,
                            provenance=provenance,
                            geometry_type="polygon_ring",
                            ring_role="outer",
                            node_refs=outer_refs,
                        )
                    )
                for inner_index, inner in enumerate(_iter_inner_rings(area, outer)):
                    inner_points, inner_refs = _node_sequence(inner, close_ring=True)
                    if len(inner_points) < 4:
                        invalid_geometry_count += 1
                        candidate_overlaps_bbox = (
                            candidate_overlaps_bbox
                            or outer_overlaps_bbox
                            or _overlaps_bbox(inner_points, bbox)
                        )
                        continue
                    inner_overlaps_bbox = _overlaps_bbox(inner_points, bbox)
                    candidate_overlaps_bbox = (
                        candidate_overlaps_bbox or inner_overlaps_bbox
                    )
                    assembled_geometry_overlaps_bbox = (
                        assembled_geometry_overlaps_bbox or inner_overlaps_bbox
                    )
                    if not inner_overlaps_bbox:
                        continue
                    staged_features.append(
                        _feature(
                            layer=layer,
                            points=inner_points,
                            osm_type=osm_type,
                            osm_id=osm_id,
                            part=f"inner:{outer_index}:{inner_index}",
                            tags=tags,
                            provenance=provenance,
                            geometry_type="polygon_ring",
                            ring_role="inner",
                            outer_ring_part=outer_part,
                            node_refs=inner_refs,
                        )
                    )
            if valid_outer_ring_count == 0 and invalid_geometry_count == 0:
                # An empty outer-ring iterator is one invalid assembly event;
                # invalid rings themselves were already counted above.
                invalid_geometry_count = 1
            self.file_scan_invalid_geometry_count += invalid_geometry_count
            if candidate_overlaps_bbox:
                self.extraction_invalid_geometry_count += invalid_geometry_count

            # An inner ring can never make an area assembly successful by
            # itself. Do not suppress source ways until at least one valid
            # outer ring exists; otherwise final_features() must retain the
            # auditable source-way fallback.
            if valid_outer_ring_count == 0:
                record_failed_assembly(affects_extraction=candidate_overlaps_bbox)
                return

            if from_way:
                self.file_scan_successful_way_area_count += 1
            else:
                self.file_scan_successful_relation_area_count += 1

            if not assembled_geometry_overlaps_bbox:
                return

            area_highway = tags.get("area:highway", "").strip().casefold()
            if not from_way and area_highway not in FALSE_AREA_HIGHWAY_VALUES:
                self.extraction_area_highway_tag_counts[area_highway] += 1

            self.features.extend(staged_features)
            if from_way:
                self.assembled_area_ways.add(osm_id)
            else:
                self.assembled_area_relations.add(osm_id)
                self.assembled_relation_way_members.update(relation_way_members)

        def final_features(self) -> list[MapFeature]:
            # Relation member ways are replaced only when libosmium produced a
            # complete assembled area for their parent relation. This prevents
            # duplicate outlines while preserving unrelated coincident ways.
            result = [
                feature
                for feature in self.features
                if not (
                    feature.osm_type == "way"
                    and (feature.osm_id, feature.layer)
                    in self.assembled_relation_way_members
                )
            ]
            for way_id, fallback in self.area_way_fallbacks.items():
                if (
                    way_id not in self.assembled_area_ways
                    and (way_id, fallback.layer)
                    not in self.assembled_relation_way_members
                    and _overlaps_bbox(fallback.points, bbox)
                ):
                    result.append(fallback)
            return result

    return _PbfHandler()


def load_pbf(
    path: Path,
    bbox: BoundingBox,
    enabled_layers: set[str],
) -> AcquisitionResult:
    """Stream a local ``.osm.pbf`` into provenance-rich canonical features.

    ``bbox`` selects candidate geometries by envelope. Exact clipping remains a
    later page-space operation so source coordinates and node references are
    not mutated during acquisition.
    """

    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise MapPlotterError(f"Could not open local PBF file {path}: {exc}") from exc
    if not resolved.is_file():
        raise MapPlotterError(f"Local PBF input is not a regular file: {resolved}")
    if not enabled_layers:
        raise MapPlotterError("At least one enabled layer is required for PBF input.")

    osmium = _import_osmium()
    try:
        source_stat = resolved.stat()
    except OSError as exc:
        raise MapPlotterError(
            f"Could not inspect local PBF file {resolved}: {exc}"
        ) from exc
    content_sha256 = _sha256_file(resolved)
    header = _read_header(osmium, resolved)
    relation_store = _RelationProvenanceStore()
    try:
        handler = _handler_for(
            osmium,
            bbox=bbox,
            enabled_layers=enabled_layers,
            relation_store=relation_store,
        )
        try:
            # locations=True resolves every WayNodeList from its referenced
            # nodes. Defining area() asks PyOsmium/libosmium to perform its
            # two-pass multipolygon assembly, including outer/inner rings.
            handler.apply_file(str(resolved), locations=True, idx="flex_mem")
        except (OSError, RuntimeError, ValueError) as exc:
            raise MapPlotterError(
                f"Could not parse local PBF file {resolved}: {exc}. Ensure this is a "
                "complete, valid .osm.pbf extract with referenced nodes and "
                "relation members."
            ) from exc
        try:
            final_stat = resolved.stat()
        except OSError as exc:
            raise MapPlotterError(
                "Local PBF file disappeared or changed while it was being read: "
                f"{resolved}"
            ) from exc
        if _stat_signature(source_stat) != _stat_signature(final_stat):
            raise MapPlotterError(
                f"Local PBF file changed while it was being read: {resolved}. Use a "
                "pinned snapshot and retry so its SHA-256 matches the extracted "
                "geometry."
            )

        features = handler.final_features()
        unassembled_relevant_relation_count = len(relation_store)
    finally:
        relation_store.close()
    by_type = Counter(feature.osm_type for feature in features)
    by_geometry = Counter(feature.geometry_type for feature in features)
    referenced_nodes = {
        node_ref
        for feature in features
        for node_ref in feature.node_refs
        if node_ref != "unknown"
    }
    source_timestamp: str | None
    source_timestamp_kind: str | None
    if header.get("osmosis_replication_timestamp"):
        source_timestamp = header["osmosis_replication_timestamp"]
        source_timestamp_kind = "osmosis-replication-cutoff"
    elif header.get("timestamp"):
        source_timestamp = header["timestamp"]
        source_timestamp_kind = "pbf-header-snapshot"
    else:
        source_timestamp = handler.latest_timestamp
        source_timestamp_kind = (
            "latest-selected-object" if source_timestamp is not None else None
        )
    header_bbox = header.get("bounding_box_wgs84")
    covers_requested_bbox = None
    if isinstance(header_bbox, dict):
        try:
            covers_requested_bbox = bool(
                float(header_bbox["west"]) <= bbox.west
                and float(header_bbox["south"]) <= bbox.south
                and float(header_bbox["east"]) >= bbox.east
                and float(header_bbox["north"]) >= bbox.north
            )
        except (KeyError, TypeError, ValueError):
            covers_requested_bbox = None
    source_metadata: dict[str, Any] = {
        "backend": "pyosmium/libosmium",
        "format": "osm.pbf",
        "source_path": str(resolved),
        "content_sha256": content_sha256,
        "size_bytes": source_stat.st_size,
        "source_timestamp": source_timestamp,
        "source_timestamp_kind": source_timestamp_kind,
        "highway_coverage": highway_coverage_from_tag_counts(
            handler.extraction_highway_tag_counts,
            handler.extraction_area_highway_tag_counts,
        ),
        "pbf_header": header,
        "coverage": {
            "policy_id": "pbf-header-bbox-covers-acquisition-v1",
            "requested_bbox_wgs84": bbox.as_dict(),
            "header_bbox_wgs84": header_bbox,
            "covers_requested_bbox": covers_requested_bbox,
            "coverage_proven": covers_requested_bbox is True,
        },
        "extraction": {
            "bbox_wgs84": bbox.as_dict(),
            "way_node_locations_resolved": True,
            # This is a capability statement, not a claim that every malformed
            # source relation assembled successfully. Outcomes are counted
            # explicitly below.
            "multipolygon_assembler_enabled": True,
            "area_assembly_backend": "libosmium",
            "relation_provenance_staging": _RelationProvenanceStore.backend,
            "exact_geometry_clipped_later": True,
            "enabled_layers": sorted(enabled_layers),
        },
        "canonical_features": {
            "count": len(features),
            "sha256": _canonical_feature_hash(features),
            "by_osm_type": dict(sorted(by_type.items())),
            "by_geometry_type": dict(sorted(by_geometry.items())),
            "referenced_node_count": len(referenced_nodes),
            "assembled_way_area_count": len(handler.assembled_area_ways),
            "assembled_relation_area_count": len(handler.assembled_area_relations),
            "failed_way_area_assembly_count": len(handler.failed_extraction_area_ways),
            "failed_relation_area_assembly_count": len(
                handler.failed_extraction_area_relations
            ),
            "unassembled_area_boundary_count": by_geometry.get(
                "unassembled_area_boundary", 0
            ),
            "invalid_geometry_count": handler.extraction_invalid_geometry_count,
        },
        "file_scan": {
            "scope": "enabled-layer candidates across the full PBF before bbox selection",
            "relevant_way_count": handler.file_scan_relevant_way_count,
            "relevant_relation_count": handler.file_scan_relevant_relation_count,
            "area_callback_count": handler.file_scan_area_callback_count,
            "successful_way_area_assembly_count": (
                handler.file_scan_successful_way_area_count
            ),
            "successful_relation_area_assembly_count": (
                handler.file_scan_successful_relation_area_count
            ),
            "failed_way_area_assembly_count": (handler.file_scan_failed_way_area_count),
            "failed_relation_area_assembly_count": (
                handler.file_scan_failed_relation_area_count
            ),
            "unassembled_relevant_relation_count": (
                handler.file_scan_failed_relation_area_count
                + unassembled_relevant_relation_count
            ),
            "invalid_geometry_count": handler.file_scan_invalid_geometry_count,
        },
    }
    data: dict[str, Any] = {"elements": []}
    if source_timestamp is not None:
        data["osm3s"] = {"timestamp_osm_base": source_timestamp}
    return AcquisitionResult(
        data=data,
        endpoint=f"file:{resolved}",
        query=None,
        cache_path=str(resolved),
        from_cache=False,
        features=features,
        source_metadata=source_metadata,
    )
