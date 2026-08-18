"""Targeted review proofs from catalog-qualified OSM train relations.

This module is deliberately narrower than the National Rail WTT compiler.  It
streams a pinned PBF three times, retaining only explicitly matched
``type=route``/``route=train`` relations selected either by current explicit
operator tags or a product-registry relation-ID exception, their referenced
ways, and those ways' node coordinates. It never searches nearby track,
expands nested relations, or claims an official or complete operator map.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import importlib
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import re
from typing import Any

from .models import MapPlotterError
from .transit import (
    ColourSpec,
    EdgeTraversal,
    ServicePattern,
    TransitEdge,
    TransitLine,
    TransitNetwork,
    TransitNode,
    TransitPen,
    TransitSource,
    validate_transit_network,
)
from .transit_operator_candidates import sha256_file
from .transit_operator_registry import (
    DEFAULT_OPERATOR_KEYS,
    OPERATOR_PENS,
    OPERATOR_PRESENTATION,
    OPERATOR_REGISTRY,
)
from .transit_operator_relations import (
    OsmRelationScanExclusion,
    OsmTrainRelationRecord,
    scan_explicit_operator_train_relations,
    way_member_role_classification,
)
from .transit_rail_graph import ACCEPTED_RAILWAY_VALUES


TARGETED_SNAPSHOT_POLICY_VERSION = "osm-explicit-operator-targeted-geometry-v2"
DEFAULT_OPERATOR_CODES = DEFAULT_OPERATOR_KEYS
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EARTH_RADIUS_M = 6_371_008.8
_WAY_TAG_KEYS = (
    "railway",
    "usage",
    "service",
    "bridge",
    "tunnel",
    "layer",
    "name",
    "ref",
    "operator",
    "network",
    "public_transport",
    "abandoned",
    "disused",
    "construction",
    "proposed",
    "razed",
    "demolished",
    "removed",
    "abandoned:railway",
    "disused:railway",
    "construction:railway",
    "proposed:railway",
    "razed:railway",
    "demolished:railway",
    "removed:railway",
)
_LIFECYCLE_RAILWAY_VALUES = frozenset(
    {"abandoned", "construction", "demolished", "disused", "proposed", "razed"}
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
_SUPPORT_RELATION_ROLES = frozenset({"platform", "station", "stop"})


def _evidence_sha256(document: Mapping[str, Any]) -> str:
    payload_document = dict(document)
    payload_document.pop("ordered_evidence_sha256", None)
    payload = json.dumps(
        payload_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def operator_evidence_sha256(audit: Mapping[str, Any], operator_code: str) -> str:
    """Seal only one operator's evidence, independent of requested siblings."""

    code = operator_code.upper()
    if code not in DEFAULT_OPERATOR_CODES:
        raise MapPlotterError(f"Unsupported operator code {operator_code!r}.")
    summaries = audit.get("operator_summary")
    relations = audit.get("relations")
    lineage = audit.get("way_relation_lineage")
    source = audit.get("source")
    if (
        not isinstance(summaries, Mapping)
        or code not in summaries
        or not isinstance(relations, list)
        or not isinstance(lineage, list)
        or not isinstance(source, Mapping)
    ):
        raise MapPlotterError("Operator snapshot audit is malformed.")
    selected_relations = [
        item
        for item in relations
        if isinstance(item, Mapping) and item.get("operator_code") == code
    ]
    relation_ids = {
        int(item["relation_id"])
        for item in selected_relations
        if isinstance(item.get("relation_id"), int)
    }
    selected_lineage = [
        item
        for item in lineage
        if isinstance(item, Mapping)
        and any(
            relation_id in relation_ids
            for relation_id in item.get("relation_ids", [])
            if isinstance(relation_id, int)
        )
    ]
    payload = {
        "schema_version": audit.get("schema_version"),
        "policy_version": audit.get("policy_version"),
        "release_state": audit.get("release_state"),
        "claim": audit.get("claim"),
        "wtt_compiled": audit.get("wtt_compiled"),
        "source": dict(source),
        "operator_code": code,
        "operator_summary": summaries[code],
        "relation_selection_exclusions": [
            item
            for item in audit.get("relation_selection_exclusions", [])
            if isinstance(item, Mapping)
            and (
                not item.get("operator_codes") or code in item.get("operator_codes", [])
            )
        ],
        "relations": selected_relations,
        "way_relation_lineage": selected_lineage,
        "invented_connector_count": audit.get("invented_connector_count"),
        "proximity_join_count": audit.get("proximity_join_count"),
        "nested_relations_expanded": audit.get("nested_relations_expanded"),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TargetedOsmWay:
    osm_way_id: int
    node_refs: tuple[int, ...]
    tags: tuple[tuple[str, str], ...]
    osm_version: int | None
    osm_timestamp: str | None

    def tag(self, key: str) -> str | None:
        return dict(self.tags).get(key)


@dataclass(frozen=True, slots=True)
class TargetedOsmNode:
    osm_node_id: int
    lon: float
    lat: float
    osm_version: int | None
    osm_timestamp: str | None


@dataclass(frozen=True, slots=True)
class TargetedOperatorGeometry:
    source_path: Path
    source_sha256: str
    source_byte_count: int
    relations: tuple[OsmTrainRelationRecord, ...]
    relation_scan_exclusions: tuple[OsmRelationScanExclusion, ...]
    ways: tuple[TargetedOsmWay, ...]
    nodes: tuple[TargetedOsmNode, ...]
    requested_way_ids: tuple[int, ...]
    missing_way_ids: tuple[int, ...]
    required_node_ids: tuple[int, ...]
    missing_node_ids: tuple[int, ...]
    scanned_way_count: int
    scanned_node_count: int

    @property
    def way_by_id(self) -> dict[int, TargetedOsmWay]:
        return {way.osm_way_id: way for way in self.ways}

    @property
    def node_by_id(self) -> dict[int, TargetedOsmNode]:
        return {node.osm_node_id: node for node in self.nodes}


def _stat_signature(value: Any) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _object_id(value: Any) -> int:
    raw = getattr(value, "id", None)
    raw = raw() if callable(raw) else raw
    if raw is None:
        raise MapPlotterError("OSM object has no ID.")
    try:
        result = int(raw)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise MapPlotterError(f"OSM object has invalid ID {raw!r}.") from exc
    if result <= 0:
        raise MapPlotterError(f"OSM object has non-positive ID {result}.")
    return result


def _optional_int(value: Any, name: str) -> int | None:
    raw = getattr(value, name, None)
    raw = raw() if callable(raw) else raw
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
        text = str(raw).strip() if raw is not None else ""
    except (RuntimeError, ValueError):
        return None
    return text or None


def _node_ref(value: Any) -> int:
    raw = getattr(value, "ref", None)
    raw = raw() if callable(raw) else raw
    if raw is None:
        raise MapPlotterError("OSM way has a node reference without an ID.")
    try:
        result = int(raw)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise MapPlotterError(f"OSM way has invalid node reference {raw!r}.") from exc
    if result <= 0:
        raise MapPlotterError(f"OSM way has non-positive node reference {result}.")
    return result


def extract_targeted_operator_geometry(
    path: Path,
    *,
    expected_sha256: str,
    operator_codes: Iterable[str] = DEFAULT_OPERATOR_CODES,
) -> TargetedOperatorGeometry:
    """Extract only explicit operator relation ways and their source nodes."""

    codes = frozenset(str(code).upper() for code in operator_codes)
    unknown = sorted(codes - DEFAULT_OPERATOR_CODES)
    if unknown or not codes:
        raise MapPlotterError(
            "Targeted operator keys must be a non-empty registry subset."
        )
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise MapPlotterError("Expected PBF SHA-256 is malformed.")
    try:
        resolved = Path(path).resolve(strict=True)
        initial_stat = resolved.stat()
    except OSError as exc:
        raise MapPlotterError(f"Cannot inspect targeted operator PBF: {exc}") from exc
    actual_sha256, byte_count = sha256_file(resolved)
    if actual_sha256 != expected_sha256:
        raise MapPlotterError(
            f"Targeted operator PBF hash mismatch: expected {expected_sha256}, "
            f"got {actual_sha256}."
        )
    if _stat_signature(initial_stat) != _stat_signature(resolved.stat()):
        raise MapPlotterError("Targeted operator PBF changed while hashing.")

    relation_scan = scan_explicit_operator_train_relations(
        resolved,
        expected_sha256=expected_sha256,
        source_hash_already_verified=True,
    )
    relations = tuple(
        relation
        for relation in relation_scan.records
        if relation.operator_code in codes
    )
    requested_way_ids = {
        member.ref
        for relation in relations
        for member in relation.members
        if member.member_type == "way"
        and way_member_role_classification(member.role) in {"alignment", "support"}
    }
    osmium = importlib.import_module("osmium")
    processor_type = getattr(osmium, "FileProcessor", None)
    entity_filter_type = getattr(getattr(osmium, "filter", None), "IdFilter", None)
    osm_types = getattr(osmium, "osm", None)
    if processor_type is None or entity_filter_type is None or osm_types is None:
        raise MapPlotterError(
            "PyOsmium does not expose FileProcessor with the C++ ID filter."
        )
    ways: dict[int, TargetedOsmWay] = {}
    way_callback_count = 0
    try:
        processor = processor_type(str(resolved), entities=osm_types.WAY).with_filter(
            entity_filter_type(requested_way_ids)
        )
        for value in processor:
            way_callback_count += 1
            way_id = _object_id(value)
            if way_id in ways:
                raise MapPlotterError(f"PBF repeats requested OSM way {way_id}.")
            tags = {str(tag.k): str(tag.v) for tag in value.tags}
            ways[way_id] = TargetedOsmWay(
                osm_way_id=way_id,
                node_refs=tuple(_node_ref(node) for node in value.nodes),
                tags=tuple((key, tags[key]) for key in _WAY_TAG_KEYS if key in tags),
                osm_version=_optional_int(value, "version"),
                osm_timestamp=_optional_timestamp(value),
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise MapPlotterError(f"Cannot stream targeted operator ways: {exc}") from exc
    if _stat_signature(initial_stat) != _stat_signature(resolved.stat()):
        raise MapPlotterError("Targeted operator PBF changed during the way pass.")

    required_node_ids = {node_id for way in ways.values() for node_id in way.node_refs}
    nodes: dict[int, TargetedOsmNode] = {}
    node_callback_count = 0
    try:
        processor = processor_type(str(resolved), entities=osm_types.NODE).with_filter(
            entity_filter_type(required_node_ids)
        )
        for value in processor:
            node_callback_count += 1
            node_id = _object_id(value)
            location = getattr(value, "location", None)
            valid = getattr(location, "valid", None)
            if location is None or (callable(valid) and not valid()):
                continue
            try:
                lon, lat = float(location.lon), float(location.lat)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            if not (-180 <= lon <= 180 and -85 <= lat <= 85):
                continue
            nodes[node_id] = TargetedOsmNode(
                osm_node_id=node_id,
                lon=lon,
                lat=lat,
                osm_version=_optional_int(value, "version"),
                osm_timestamp=_optional_timestamp(value),
            )

    except (OSError, RuntimeError, ValueError) as exc:
        raise MapPlotterError(f"Cannot stream targeted operator nodes: {exc}") from exc
    if _stat_signature(initial_stat) != _stat_signature(resolved.stat()):
        raise MapPlotterError("Targeted operator PBF changed during the node pass.")

    return TargetedOperatorGeometry(
        source_path=resolved,
        source_sha256=actual_sha256,
        source_byte_count=byte_count,
        relations=relations,
        relation_scan_exclusions=tuple(
            exclusion
            for exclusion in relation_scan.exclusions
            if not exclusion.operator_codes
            or set(exclusion.operator_codes).intersection(codes)
        ),
        ways=tuple(sorted(ways.values(), key=lambda item: item.osm_way_id)),
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.osm_node_id)),
        requested_way_ids=tuple(sorted(requested_way_ids)),
        missing_way_ids=tuple(sorted(requested_way_ids - set(ways))),
        required_node_ids=tuple(sorted(required_node_ids)),
        missing_node_ids=tuple(sorted(required_node_ids - set(nodes))),
        scanned_way_count=way_callback_count,
        scanned_node_count=node_callback_count,
    )


def _haversine_m(first: TargetedOsmNode, second: TargetedOsmNode) -> float:
    lon1, lat1, lon2, lat2 = map(
        radians, (first.lon, first.lat, second.lon, second.lat)
    )
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = sin(dlat / 2.0) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_M * asin(min(1.0, sqrt(value)))


def _ready_way_ids(geometry: TargetedOperatorGeometry) -> set[int]:
    nodes = geometry.node_by_id
    return {
        way.osm_way_id
        for way in geometry.ways
        if len(way.node_refs) >= 2
        and all(node_id in nodes for node_id in way.node_refs)
    }


def _normalised_tag(value: str | None) -> str:
    return "" if value is None else value.strip().casefold()


def _tag_is_positive(value: str | None) -> bool:
    return _normalised_tag(value) not in _FALSE_TAG_VALUES


def _alignment_way_disposition(
    way: TargetedOsmWay,
    nodes: Mapping[int, TargetedOsmNode],
) -> tuple[bool, str]:
    """Apply the exact operational-rail policy to one relation member way."""

    tags = dict(way.tags)
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
    if len(way.node_refs) < 2:
        return False, "fewer-than-two-node-references"
    if any(node_id not in nodes for node_id in way.node_refs):
        return False, "incomplete-node-geometry"
    for first_id, second_id in zip(way.node_refs, way.node_refs[1:]):
        if first_id == second_id:
            return False, "repeated-consecutive-node-reference"
        first, second = nodes[first_id], nodes[second_id]
        if first.lon == second.lon and first.lat == second.lat:
            return False, "coincident-consecutive-node-coordinates"
    return True, f"railway={railway}"


def _drawable_way_ids(geometry: TargetedOperatorGeometry) -> frozenset[int]:
    nodes = geometry.node_by_id
    return frozenset(
        way.osm_way_id
        for way in geometry.ways
        if _alignment_way_disposition(way, nodes)[0]
    )


def _is_support_relation_role(role: str) -> bool:
    normalised = role.strip().casefold()
    return normalised in _SUPPORT_RELATION_ROLES or normalised.startswith("platform_")


@dataclass(frozen=True, slots=True)
class _OrientedWayOccurrence:
    member_index: int
    way_id: int
    direction: str
    direction_basis: str


@dataclass(frozen=True, slots=True)
class _RelationRunPlan:
    runs: tuple[tuple[_OrientedWayOccurrence, ...], ...]
    discontinuities: tuple[dict[str, Any], ...]


def _allowed_directions(role: str) -> tuple[str, ...]:
    normalised = role.strip().casefold()
    if normalised in {"", "route"}:
        return ("forward", "reverse")
    if normalised == "forward":
        return ("forward",)
    if normalised == "backward":
        return ("reverse",)
    return ()


def _oriented_endpoints(way: TargetedOsmWay, direction: str) -> tuple[int, int]:
    if direction == "reverse":
        return way.node_refs[-1], way.node_refs[0]
    return way.node_refs[0], way.node_refs[-1]


def _path_sort_key(
    path: tuple[_OrientedWayOccurrence, ...],
) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (
            0 if item.direction == "forward" else 1,
            item.member_index,
            item.way_id,
        )
        for item in path
    )


def _orient_candidate_group(
    candidates: list[tuple[int, TargetedOsmWay, str]],
) -> _RelationRunPlan:
    if not candidates:
        return _RelationRunPlan((), ())
    runs: list[tuple[_OrientedWayOccurrence, ...]] = []
    discontinuities: list[dict[str, Any]] = []
    states: list[tuple[int, tuple[_OrientedWayOccurrence, ...]]] = []

    def start_states(
        item: tuple[int, TargetedOsmWay, str],
    ) -> list[tuple[int, tuple[_OrientedWayOccurrence, ...]]]:
        member_index, way, role = item
        result: list[tuple[int, tuple[_OrientedWayOccurrence, ...]]] = []
        for direction in _allowed_directions(role):
            _start, end = _oriented_endpoints(way, direction)
            result.append(
                (
                    end,
                    (
                        _OrientedWayOccurrence(
                            member_index=member_index,
                            way_id=way.osm_way_id,
                            direction=direction,
                            direction_basis=(
                                "explicit-member-role"
                                if role.strip() not in {"", "route"}
                                else "exact-shared-node-or-deterministic-endpoint"
                            ),
                        ),
                    ),
                )
            )
        return result

    states = start_states(candidates[0])
    for candidate in candidates[1:]:
        member_index, way, role = candidate
        extensions: list[tuple[int, tuple[_OrientedWayOccurrence, ...]]] = []
        for previous_end, path in states:
            for direction in _allowed_directions(role):
                start, end = _oriented_endpoints(way, direction)
                if previous_end != start:
                    continue
                extensions.append(
                    (
                        end,
                        (
                            *path,
                            _OrientedWayOccurrence(
                                member_index=member_index,
                                way_id=way.osm_way_id,
                                direction=direction,
                                direction_basis=(
                                    "explicit-member-role-exact-shared-node"
                                    if role.strip() not in {"", "route"}
                                    else "exact-shared-node"
                                ),
                            ),
                        ),
                    )
                )
        if extensions:
            best_by_end: dict[int, tuple[_OrientedWayOccurrence, ...]] = {}
            for end, path in extensions:
                previous = best_by_end.get(end)
                if previous is None or _path_sort_key(path) < _path_sort_key(previous):
                    best_by_end[end] = path
            states = [(end, best_by_end[end]) for end in sorted(best_by_end)]
            continue

        previous_end, selected_path = min(
            states, key=lambda item: _path_sort_key(item[1])
        )
        runs.append(selected_path)
        discontinuities.append(
            {
                "from_member_index": selected_path[-1].member_index,
                "to_member_index": member_index,
                "from_way_id": selected_path[-1].way_id,
                "to_way_id": way.osm_way_id,
                "from_node_id": previous_end,
                "candidate_to_way_endpoint_node_ids": sorted(
                    {way.node_refs[0], way.node_refs[-1]}
                ),
                "reason": "no-exact-shared-endpoint",
            }
        )
        states = start_states(candidate)
    if states:
        runs.append(min(states, key=lambda item: _path_sort_key(item[1]))[1])
    return _RelationRunPlan(tuple(runs), tuple(discontinuities))


def _plan_relation_runs(
    relation: OsmTrainRelationRecord,
    geometry: TargetedOperatorGeometry,
    drawable_way_ids: frozenset[int],
) -> _RelationRunPlan:
    ways = geometry.way_by_id
    all_runs: list[tuple[_OrientedWayOccurrence, ...]] = []
    all_discontinuities: list[dict[str, Any]] = []
    group: list[tuple[int, TargetedOsmWay, str]] = []

    def flush() -> None:
        nonlocal group
        planned = _orient_candidate_group(group)
        all_runs.extend(planned.runs)
        all_discontinuities.extend(planned.discontinuities)
        group = []

    for member_index, member in enumerate(relation.members):
        if member.member_type == "node":
            continue
        if member.member_type == "relation" and _is_support_relation_role(member.role):
            continue
        if member.member_type != "way":
            flush()
            continue
        role_class = way_member_role_classification(member.role)
        if role_class == "support":
            continue
        if (
            role_class != "alignment"
            or member.ref not in drawable_way_ids
            or member.ref not in ways
        ):
            flush()
            continue
        group.append((member_index, ways[member.ref], member.role))
    flush()
    return _RelationRunPlan(tuple(all_runs), tuple(all_discontinuities))


def _component_count(way_ids: Iterable[int], geometry: TargetedOperatorGeometry) -> int:
    ways = geometry.way_by_id
    adjacency: dict[int, set[int]] = defaultdict(set)
    all_nodes: set[int] = set()
    for way_id in way_ids:
        refs = ways[way_id].node_refs
        all_nodes.update(refs)
        for first, second in zip(refs, refs[1:]):
            adjacency[first].add(second)
            adjacency[second].add(first)
    visited: set[int] = set()
    count = 0
    for start in sorted(all_nodes):
        if start in visited:
            continue
        count += 1
        queue = deque([start])
        while queue:
            node_id = queue.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)
            queue.extend(sorted(adjacency[node_id] - visited))
    return count


def _geometry_sha256(way: TargetedOsmWay, nodes: Mapping[int, TargetedOsmNode]) -> str:
    payload = [[nodes[node_id].lon, nodes[node_id].lat] for node_id in way.node_refs]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("ascii")
    ).hexdigest()


def _segment_edge_ids(way: TargetedOsmWay, direction: str) -> tuple[str, ...]:
    indices: Iterable[int] = range(len(way.node_refs) - 1)
    if direction == "reverse":
        indices = reversed(tuple(indices))
    return tuple(f"osm-way-{way.osm_way_id}-segment-{index}" for index in indices)


def audit_targeted_operator_geometry(
    geometry: TargetedOperatorGeometry,
) -> dict[str, Any]:
    """Return complete relation/way lineage and every unresolved occurrence."""

    ways = geometry.way_by_id
    nodes = geometry.node_by_id
    ready = _ready_way_ids(geometry)
    drawable = _drawable_way_ids(geometry)
    relation_records: list[dict[str, Any]] = []
    way_relations: dict[int, set[int]] = defaultdict(set)
    for relation in geometry.relations:
        resolved_alignment: list[int] = []
        missing_alignment: list[int] = []
        incomplete_node_ways: list[int] = []
        policy_excluded_alignment: list[dict[str, Any]] = []
        support_ways: list[int] = []
        missing_support: list[int] = []
        support_relation_members: list[int] = []
        nested_members: list[int] = []
        unsupported: list[dict[str, Any]] = []
        alignment_occurrences = 0
        ordered_members: list[dict[str, Any]] = []
        for index, member in enumerate(relation.members):
            item: dict[str, Any] = {
                "index": index,
                "type": member.member_type,
                "ref": member.ref,
                "role": member.role,
            }
            if member.member_type == "way":
                role_class = way_member_role_classification(member.role)
                item["role_class"] = role_class
                if role_class == "support":
                    support_ways.append(member.ref)
                    item["resolution"] = (
                        "resolved-support-not-alignment"
                        if member.ref in ways
                        else "missing-support-not-route-gap"
                    )
                    if member.ref not in ways:
                        missing_support.append(member.ref)
                elif role_class == "unsupported":
                    item["resolution"] = "unsupported-way-role"
                    unsupported.append(dict(item))
                else:
                    alignment_occurrences += 1
                    way_relations[member.ref].add(relation.relation_id)
                    if member.ref not in ways:
                        missing_alignment.append(member.ref)
                        item["resolution"] = "missing-alignment-way"
                    elif member.ref not in ready:
                        incomplete_node_ways.append(member.ref)
                        item["resolution"] = "incomplete-node-geometry"
                    else:
                        way = ways[member.ref]
                        accepted, reason = _alignment_way_disposition(way, nodes)
                        item["operational_selection_reason"] = reason
                        if not accepted:
                            item["resolution"] = "policy-excluded-alignment-way"
                            policy_excluded_alignment.append(
                                {
                                    "member_index": index,
                                    "way_id": member.ref,
                                    "reason": reason,
                                }
                            )
                        else:
                            resolved_alignment.append(member.ref)
                            item["resolution"] = "exact-operational-member-way-geometry"
            elif member.member_type == "relation":
                if _is_support_relation_role(member.role):
                    support_relation_members.append(member.ref)
                    item["resolution"] = "support-relation-not-route-alignment"
                else:
                    nested_members.append(member.ref)
                    item["resolution"] = "nested-route-relation-not-expanded"
            elif member.member_type == "node":
                item["resolution"] = "non-geometry-stop-support-member"
            else:
                item["resolution"] = "unknown-member-type"
                unsupported.append(dict(item))
            ordered_members.append(item)
        plan = _plan_relation_runs(relation, geometry, drawable)
        orientation_by_index = {
            occurrence.member_index: occurrence
            for run in plan.runs
            for occurrence in run
        }
        for item in ordered_members:
            occurrence = orientation_by_index.get(int(item["index"]))
            if occurrence is not None:
                item["exact_orientation"] = occurrence.direction
                item["direction_basis"] = occurrence.direction_basis
        oriented_runs = [
            {
                "part_index": part_index,
                "members": [
                    {
                        "member_index": occurrence.member_index,
                        "way_id": occurrence.way_id,
                        "direction": occurrence.direction,
                        "direction_basis": occurrence.direction_basis,
                        "segment_edge_ids": list(
                            _segment_edge_ids(
                                ways[occurrence.way_id], occurrence.direction
                            )
                        ),
                    }
                    for occurrence in run
                ],
            }
            for part_index, run in enumerate(plan.runs, start=1)
        ]
        relation_records.append(
            {
                "relation_id": relation.relation_id,
                "operator_code": relation.operator_code,
                "selection_method": relation.selection_method,
                "matched_tags": [list(item) for item in relation.matched_tags],
                "tags": dict(relation.tags),
                "osm_version": relation.osm_version,
                "osm_timestamp": relation.osm_timestamp,
                "alignment_way_occurrence_count": alignment_occurrences,
                "resolved_alignment_way_ids": resolved_alignment,
                "missing_alignment_way_ids": missing_alignment,
                "incomplete_node_geometry_way_ids": incomplete_node_ways,
                "policy_excluded_alignment_ways": policy_excluded_alignment,
                "support_way_ids": support_ways,
                "missing_support_way_ids": missing_support,
                "support_relation_member_ids": support_relation_members,
                "nested_relation_member_ids": nested_members,
                "unsupported_members": unsupported,
                "ordered_alignment_discontinuities": list(plan.discontinuities),
                "oriented_runs": oriented_runs,
                "ordered_members": ordered_members,
                "complete_explicit_relation_geometry": not (
                    alignment_occurrences == 0
                    or missing_alignment
                    or incomplete_node_ways
                    or policy_excluded_alignment
                    or nested_members
                    or unsupported
                    or plan.discontinuities
                ),
            }
        )

    way_lineage = []
    for way_id in sorted(way_relations):
        if way_id not in ready:
            continue
        way = ways[way_id]
        selected, selection_reason = _alignment_way_disposition(way, nodes)
        way_lineage.append(
            {
                "way_id": way_id,
                "relation_ids": sorted(way_relations[way_id]),
                "node_ref_count": len(way.node_refs),
                "node_refs": list(way.node_refs),
                "geometry_sha256": _geometry_sha256(way, nodes),
                "operational_selection_accepted": selected,
                "operational_selection_reason": selection_reason,
                "atomic_segment_edge_ids": (
                    list(_segment_edge_ids(way, "forward")) if selected else []
                ),
                "tags": dict(way.tags),
                "osm_version": way.osm_version,
                "osm_timestamp": way.osm_timestamp,
            }
        )

    operator_summary: dict[str, dict[str, Any]] = {}
    for code in sorted(DEFAULT_OPERATOR_CODES):
        selected_relations = [
            item for item in relation_records if item["operator_code"] == code
        ]
        selected_way_ids = {
            way_id
            for relation in selected_relations
            for way_id in relation["resolved_alignment_way_ids"]
        }
        length_m = 0.0
        for way_id in selected_way_ids:
            refs = ways[way_id].node_refs
            length_m += sum(
                _haversine_m(nodes[first], nodes[second])
                for first, second in zip(refs, refs[1:])
            )
        occurrence_count = sum(
            int(item["alignment_way_occurrence_count"]) for item in selected_relations
        )
        resolved_occurrence_count = sum(
            len(item["resolved_alignment_way_ids"]) for item in selected_relations
        )
        policy_exclusions = [
            exclusion
            for item in selected_relations
            for exclusion in item["policy_excluded_alignment_ways"]
        ]
        exclusion_reasons = Counter(str(item["reason"]) for item in policy_exclusions)
        scan_exclusions = [
            item
            for item in geometry.relation_scan_exclusions
            if not item.operator_codes or code in item.operator_codes
        ]
        resolved_segment_occurrence_count = sum(
            sum(
                len(ways[way_id].node_refs) - 1
                for way_id in item["resolved_alignment_way_ids"]
            )
            for item in selected_relations
        )
        operator_summary[code] = {
            "relation_count": len(selected_relations),
            "ambiguous_multi_code_relation_exclusion_count": sum(
                item.reason == "ambiguous-multiple-operator-codes"
                for item in scan_exclusions
            ),
            "legacy_operator_token_exclusion_count": sum(
                item.reason
                in {
                    "legacy-operator-token",
                    "mixed-current-and-legacy-operator-tokens",
                    "reviewed-relation-id-has-legacy-operator-token",
                }
                for item in scan_exclusions
            ),
            "reviewed_relation_id_count": sum(
                item["selection_method"] == "catalog-reviewed-relation-id"
                for item in selected_relations
            ),
            "direct_route_master_exclusion_count": sum(
                item.reason == "direct-route-master-not-expanded"
                for item in scan_exclusions
            ),
            "complete_relation_count": sum(
                bool(item["complete_explicit_relation_geometry"])
                for item in selected_relations
            ),
            "alignment_way_occurrence_count": occurrence_count,
            "resolved_alignment_way_occurrence_count": resolved_occurrence_count,
            "unique_drawable_alignment_way_count": len(selected_way_ids),
            "unique_drawable_atomic_segment_count": sum(
                len(ways[way_id].node_refs) - 1 for way_id in selected_way_ids
            ),
            "resolved_atomic_segment_occurrence_count": (
                resolved_segment_occurrence_count
            ),
            "deduplicated_alignment_way_occurrence_count": max(
                0, resolved_occurrence_count - len(selected_way_ids)
            ),
            "missing_alignment_way_occurrence_count": sum(
                len(item["missing_alignment_way_ids"]) for item in selected_relations
            ),
            "incomplete_node_geometry_way_occurrence_count": sum(
                len(item["incomplete_node_geometry_way_ids"])
                for item in selected_relations
            ),
            "policy_excluded_alignment_way_occurrence_count": len(policy_exclusions),
            "policy_excluded_alignment_way_occurrence_counts_by_reason": dict(
                sorted(exclusion_reasons.items())
            ),
            "support_way_occurrence_count": sum(
                len(item["support_way_ids"]) for item in selected_relations
            ),
            "missing_support_way_occurrence_count": sum(
                len(item["missing_support_way_ids"]) for item in selected_relations
            ),
            "nested_relation_member_occurrence_count": sum(
                len(item["nested_relation_member_ids"]) for item in selected_relations
            ),
            "support_relation_member_occurrence_count": sum(
                len(item["support_relation_member_ids"]) for item in selected_relations
            ),
            "unsupported_member_occurrence_count": sum(
                len(item["unsupported_members"]) for item in selected_relations
            ),
            "ordered_alignment_discontinuity_count": sum(
                len(item["ordered_alignment_discontinuities"])
                for item in selected_relations
            ),
            "zero_alignment_relation_count": sum(
                int(item["alignment_way_occurrence_count"]) == 0
                for item in selected_relations
            ),
            "exact_node_component_count": _component_count(selected_way_ids, geometry),
            "unique_exact_geometry_length_m": round(length_m, 3),
        }

    document: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": TARGETED_SNAPSHOT_POLICY_VERSION,
        "release_state": "review-proof-not-official-complete-coverage",
        "approved": False,
        "claim": (
            "OSM current operator-tag or catalog-reviewed relation-ID snapshot only"
        ),
        "wtt_compiled": False,
        "source": {
            "artifact_name": geometry.source_path.name,
            "source_lock_ref": "docs/transit/national-source-lock-2026-08-07.json",
            "source_url": "https://download.geofabrik.de/europe/great-britain.html",
            "sha256": geometry.source_sha256,
            "byte_count": geometry.source_byte_count,
            "local_path_excluded_from_evidence_digest": True,
        },
        "streaming": {
            "pbf_pass_count": 3,
            "whole_rail_graph_built": False,
            "entity_filter_backend": "pyosmium FileProcessor C++ IdFilter",
            "source_total_way_count": None,
            "selected_way_callback_count": geometry.scanned_way_count,
            "retained_way_count": len(geometry.ways),
            "requested_way_count": len(geometry.requested_way_ids),
            "missing_way_count": len(geometry.missing_way_ids),
            "source_total_node_count": None,
            "selected_node_callback_count": geometry.scanned_node_count,
            "retained_node_count": len(geometry.nodes),
            "required_node_count": len(geometry.required_node_ids),
            "missing_node_count": len(geometry.missing_node_ids),
        },
        "relation_selection_exclusions": [
            {
                "relation_id": item.relation_id,
                "reason": item.reason,
                "operator_codes": list(item.operator_codes),
                "tags": dict(item.tags),
            }
            for item in geometry.relation_scan_exclusions
        ],
        "operator_summary": operator_summary,
        "relations": relation_records,
        "way_relation_lineage": way_lineage,
        "invented_connector_count": 0,
        "proximity_join_count": 0,
        "nested_relations_expanded": False,
    }
    document["ordered_evidence_sha256"] = _evidence_sha256(document)
    return document


def compile_operator_snapshot_network(
    geometry: TargetedOperatorGeometry,
    audit: Mapping[str, Any],
    *,
    operator_code: str,
    snapshot_date: str = "2026-08-06",
    retrieved_at: str = "2026-08-07",
) -> TransitNetwork:
    """Build a review-only TransitNetwork from exact resolved member ways."""

    code = operator_code.upper()
    if code not in DEFAULT_OPERATOR_CODES:
        raise MapPlotterError(f"Unsupported operator code {operator_code!r}.")
    summary_raw = audit.get("operator_summary", {})
    if not isinstance(summary_raw, Mapping) or code not in summary_raw:
        raise MapPlotterError("Operator snapshot audit lacks the requested operator.")
    audit_sha = str(audit.get("ordered_evidence_sha256", ""))
    if _SHA256_RE.fullmatch(audit_sha) is None:
        raise MapPlotterError("Operator snapshot audit has no valid evidence digest.")
    if _evidence_sha256(audit) != audit_sha:
        raise MapPlotterError(
            "Operator snapshot audit evidence digest does not verify."
        )
    recomputed_audit = audit_targeted_operator_geometry(geometry)
    if recomputed_audit["ordered_evidence_sha256"] != audit_sha:
        raise MapPlotterError("Operator snapshot audit is not bound to this geometry.")
    operator_audit_sha = operator_evidence_sha256(audit, code)

    product = OPERATOR_REGISTRY.by_key[code]
    slug, operator_name, colour_hex = OPERATOR_PRESENTATION[code]
    uses_reviewed_id = any(
        relation.operator_code == code
        and relation.selection_method == "catalog-reviewed-relation-id"
        for relation in geometry.relations
    )
    selection_slug = (
        "osm-reviewed-relation-id" if uses_reviewed_id else "osm-explicit-operator-tag"
    )
    line_id = f"{slug}-{selection_slug}-snapshot"
    source_id = "osm-gb-operator-tag-snapshot-2026-08-06"
    audit_source_id = f"osm-operator-tag-evidence-{code.casefold()}-v2"
    palette_source_id = "national-operator-house-palette-v1"
    palette_payload = json.dumps(
        dict(OPERATOR_PRESENTATION), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    sources = (
        TransitSource(
            id=source_id,
            publisher="OpenStreetMap contributors; extract by Geofabrik",
            url="https://download.geofabrik.de/europe/great-britain.html",
            licence="Open Data Commons Open Database Licence 1.0",
            attribution="© OpenStreetMap contributors",
            retrieved_at=retrieved_at,
            sha256=geometry.source_sha256,
            use=(
                "Catalog-qualified route=train relation membership and exact "
                "referenced member-way geometry; not official or complete coverage."
            ),
            commercial_reuse_status="commercial-allowed",
        ),
        TransitSource(
            id=audit_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike",
            licence="Derived ODbL evidence ledger; review proof only",
            attribution="Derived from the pinned OSM snapshot",
            retrieved_at=retrieved_at,
            sha256=operator_audit_sha,
            use=(
                f"Ordered {code} relation/member lineage, omissions, and "
                "continuity audit."
            ),
            commercial_reuse_status="review-required",
        ),
        TransitSource(
            id=palette_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike",
            licence="House rendering values",
            attribution="City Map Plotter house palette",
            retrieved_at=retrieved_at,
            sha256=hashlib.sha256(palette_payload).hexdigest(),
            use="Review-preview colour only; not an official numeric operator colour.",
            commercial_reuse_status="commercial-allowed",
        ),
    )
    ink, pen_id, nominal_nib_mm = OPERATOR_PENS[code]
    line = TransitLine(
        id=line_id,
        name=f"{operator_name} OSM tag snapshot",
        short_name=slug.upper(),
        order=0,
        colour=ColourSpec(
            name=f"{operator_name} house proof colour",
            display_hex=colour_hex,
            role="operator-network",
            provenance="house-palette",
            numeric_value_status="house-value",
            source_ref=palette_source_id,
        ),
        pen=TransitPen(
            ink=ink,
            nominal_nib_mm=nominal_nib_mm,
            match_status="nominal-unmeasured",
            pen_id=pen_id,
            calibration_state="nominal-unmeasured",
            preview_hex=colour_hex,
        ),
        service_class="unclassified-review-proof",
        source_ref=source_id,
    )

    drawable = _drawable_way_ids(geometry)
    relations = tuple(
        relation for relation in geometry.relations if relation.operator_code == code
    )
    selected_way_ids = {
        member.ref
        for relation in relations
        for member in relation.members
        if member.member_type == "way"
        and way_member_role_classification(member.role) == "alignment"
        and member.ref in drawable
    }
    if not selected_way_ids:
        raise MapPlotterError(f"Operator {code} has no resolved alignment ways.")
    ways = geometry.way_by_id
    source_nodes = geometry.node_by_id
    selected_node_ids = {
        node_id for way_id in selected_way_ids for node_id in ways[way_id].node_refs
    }
    nodes = tuple(
        TransitNode(
            id=f"osm-node-{node_id}",
            kind="junction",
            lon=source_nodes[node_id].lon,
            lat=source_nodes[node_id].lat,
            source_ref=source_id,
            source_object=f"node/{node_id}",
        )
        for node_id in sorted(selected_node_ids)
    )
    edges: list[TransitEdge] = []
    for way_id in sorted(selected_way_ids):
        way = ways[way_id]
        grade = (
            "bridge"
            if way.tag("bridge") not in {None, "", "no"}
            else "tunnel"
            if way.tag("tunnel") not in {None, "", "no"}
            else "unknown"
        )
        for segment_index, (first, second) in enumerate(
            zip(way.node_refs, way.node_refs[1:])
        ):
            edges.append(
                TransitEdge(
                    id=f"osm-way-{way_id}-segment-{segment_index}",
                    from_node=f"osm-node-{first}",
                    to_node=f"osm-node-{second}",
                    geometry=(
                        (source_nodes[first].lon, source_nodes[first].lat),
                        (source_nodes[second].lon, source_nodes[second].lat),
                    ),
                    line_ids=(line_id,),
                    source_ref=source_id,
                    source_object=f"way/{way_id}",
                    status="osm-explicit-route-train-member-review-proof",
                    grade=grade,
                )
            )
    patterns: list[ServicePattern] = []
    for relation in relations:
        plan = _plan_relation_runs(relation, geometry, drawable)
        relation_name = dict(relation.tags).get(
            "name", f"OSM RELATION {relation.relation_id}"
        )
        for part_index, run in enumerate(plan.runs, start=1):
            suffix = f" PART {part_index}" if len(plan.runs) > 1 else ""
            patterns.append(
                ServicePattern(
                    id=f"osm-relation-{relation.relation_id}-part-{part_index}",
                    line_id=line_id,
                    name=f"{relation_name}{suffix}",
                    traversals=tuple(
                        EdgeTraversal(edge_id=edge_id, direction=occurrence.direction)
                        for occurrence in run
                        for edge_id in _segment_edge_ids(
                            ways[occurrence.way_id], occurrence.direction
                        )
                    ),
                    station_ids=(),
                    source_ref=audit_source_id,
                    derivation_status=(
                        "exact-shared-node-oriented-osm-member-segments-"
                        "review-proof-no-gap-repair"
                    ),
                    continuity_breaks=(),
                )
            )
    if not patterns:
        raise MapPlotterError(
            f"Operator {code} has no continuous resolved relation run."
        )

    summary = summary_raw[code]
    assert isinstance(summary, Mapping)
    network = TransitNetwork(
        id=f"{slug}-{selection_slug}-2026-08-06",
        name=f"{operator_name.upper()} — REVIEW PROOF",
        kind="national-operator",
        scope=(
            "OSM CATALOG-QUALIFIED RELATION SNAPSHOT / REVIEW PROOF / "
            "NOT OFFICIAL COMPLETE COVERAGE"
        ),
        format_id=product.format_id,
        snapshot=snapshot_date,
        validity_status="candidate-not-reviewed",
        geometry_mode=(
            "exact-osm-route-train-member-consecutive-node-segments-no-joins"
        ),
        sources=sources,
        lines=(line,),
        nodes=nodes,
        edges=tuple(edges),
        service_patterns=tuple(patterns),
        context=(),
        omissions=(
            {
                "kind": "official-completeness",
                "status": "not-claimed",
                "reason": (
                    "Only explicit OSM operator/network-tagged route=train "
                    "relations are represented; no official coverage claim."
                ),
            },
            {
                "kind": "unresolved-relation-members",
                "status": "reported-not-repaired",
                "reason": (
                    f"{summary['missing_alignment_way_occurrence_count']} missing "
                    "alignment-way occurrences, "
                    f"{summary['incomplete_node_geometry_way_occurrence_count']} "
                    "incomplete-geometry occurrences, "
                    f"{summary['policy_excluded_alignment_way_occurrence_count']} "
                    "operational-policy exclusions, "
                    f"{summary['nested_relation_member_occurrence_count']} nested "
                    "relation occurrences, and "
                    f"{summary['ordered_alignment_discontinuity_count']} ordered "
                    "endpoint discontinuities; see the evidence audit."
                ),
            },
            {
                "kind": "relation-selection-scope",
                "status": "quantified-not-expanded",
                "reason": (
                    f"{summary['ambiguous_multi_code_relation_exclusion_count']} "
                    "ambiguous multi-code route relations, "
                    f"{summary['direct_route_master_exclusion_count']} direct route "
                    "masters, and "
                    f"{summary['zero_alignment_relation_count']} selected relations "
                    "with zero alignment ways; see the evidence audit."
                ),
            },
            {
                "kind": "station-labels",
                "status": "omitted",
                "reason": (
                    "Relation stop members were not converted into station-to-track "
                    "bindings; doing so requires a separate reviewed source decision."
                ),
            },
        ),
        notes=(
            "REVIEW PROOF ONLY — OSM catalog-qualified relation snapshot.",
            "Physical ways are deduplicated by OSM way ID and compiled into exact consecutive-node segments.",
            "Service class is deliberately unclassified because OSM membership is not timetable evidence.",
            "Platform/support ways are audited but are not drawn as route alignment.",
            "Nested relations are reported and never expanded; no proximity joins or invented connectors are used.",
            f"Combined evidence ledger SHA-256: {audit_sha}.",
            f"Operator evidence SHA-256: {operator_audit_sha}.",
        ),
        contract_sha256="",
    )
    validate_transit_network(network)
    return network


__all__ = [
    "DEFAULT_OPERATOR_CODES",
    "TARGETED_SNAPSHOT_POLICY_VERSION",
    "TargetedOperatorGeometry",
    "TargetedOsmNode",
    "TargetedOsmWay",
    "audit_targeted_operator_geometry",
    "compile_operator_snapshot_network",
    "extract_targeted_operator_geometry",
    "operator_evidence_sha256",
]
