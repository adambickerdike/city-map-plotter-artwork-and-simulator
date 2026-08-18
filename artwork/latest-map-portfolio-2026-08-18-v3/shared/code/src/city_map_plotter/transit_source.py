"""Explicit, cacheable source compilation for transit network contracts.

Live acquisition is a maintenance action, never part of rendering.  It follows
OSM network/route-master/route relations, snapshots every response, and turns
only route-member railway ways into atomic graph edges.  Official operator
sources verify line identity and service scope; their diagram geometry is not
traced or copied.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from math import cos, hypot, radians
from pathlib import Path
import re
import time
from typing import Any, Sequence
import urllib.error
import urllib.request

from .models import MapPlotterError
from .transit import catalog_network, canonical_contract_bytes, load_transit_network


OSM_API = "https://api.openstreetmap.org/api/0.6"
_RAILWAY_VALUES = frozenset({"rail", "light_rail", "subway", "tram", "narrow_gauge"})
_STOP_ROLES = frozenset(
    {
        "stop",
        "stop_entry_only",
        "stop_exit_only",
        "platform",
        "platform_entry_only",
        "platform_exit_only",
    }
)
STATION_NAME_CLUSTER_LIMIT_M = 450.0
DISTINCT_SAME_NAME_CLUSTER_LIMIT_M = 120.0
# Endpoint snapping is an exceptional repair for a catalog-reviewed defect in
# consecutive members of one OSM route relation. The hard bound holds it to an
# approximately one-metre source mismatch rather than an ordinary mapping gap.
# A catalog rule may impose a smaller limit but can never raise this hard cap.
MAX_CONSECUTIVE_ROUTE_ENDPOINT_SNAP_M = 1.25


@dataclass(frozen=True, slots=True)
class EndpointSnapRule:
    first_way_id: int
    second_way_id: int
    maximum_distance_m: float
    expected_occurrence_count: int
    reason: str


@dataclass(frozen=True, slots=True)
class EndpointSnapEvidence:
    relation_id: int
    line_id: str
    after_source_way_index: int
    first_way_id: int
    second_way_id: int
    from_node_id: int
    to_node_id: int
    from_position: tuple[float, float]
    to_position: tuple[float, float]
    distance_m: float
    maximum_distance_m: float
    connector_edge_id: str
    connector_direction: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "line_id": self.line_id,
            "after_source_way_index": self.after_source_way_index,
            "first_way_id": self.first_way_id,
            "second_way_id": self.second_way_id,
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "from_position": list(self.from_position),
            "to_position": list(self.to_position),
            "distance_m": round(self.distance_m, 6),
            "maximum_distance_m": self.maximum_distance_m,
            "connector_edge_id": self.connector_edge_id,
            "connector_direction": self.connector_direction,
            "reason": self.reason,
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return result or "unnamed"


def _station_name_key(value: str) -> str:
    normalized = value.casefold().strip()
    normalized = re.sub(r"\s+(?:underground|subway|metro)\s+station$", "", normalized)
    normalized = re.sub(r"\s+(?:station|tram stop)$", "", normalized)
    return _slug(normalized)


def _looks_like_platform_label(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.casefold().strip()
    if re.fullmatch(r"[0-9\s,;/&-]+", normalized):
        return True
    if normalized.endswith(" line"):
        return True
    return bool(
        re.search(
            r"\b(platform|northbound|southbound|eastbound|westbound|inner|outer)\b",
            normalized,
        )
    )


def _station_distance_m(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    latitude = (first[1] + second[1]) / 2.0
    return 111_320.0 * hypot(
        (first[0] - second[0]) * cos(radians(latitude)),
        first[1] - second[1],
    )


def _route_way_graph_is_closed(
    way_ids: list[int], way_endpoints: dict[int, tuple[int, int]]
) -> bool:
    """Return whether a route relation has no topological service endpoint.

    OSM circular services choose an arbitrary first member and first stop.  It
    is therefore wrong to promote those list boundaries to passenger-service
    terminals.  A closed route has even endpoint degree throughout its member
    graph; malformed/missing memberships fail conservatively as non-closed.
    """

    degrees: dict[int, int] = defaultdict(int)
    for way_id in way_ids:
        endpoints = way_endpoints.get(way_id)
        if endpoints is None:
            return False
        degrees[endpoints[0]] += 1
        degrees[endpoints[1]] += 1
    return bool(degrees) and all(degree % 2 == 0 for degree in degrees.values())


def _endpoint_snap_rules(
    acquisition: dict[str, Any],
) -> tuple[dict[tuple[int, int], EndpointSnapRule], int, int, str]:
    """Parse a fail-closed catalog allowlist for exceptional endpoint snaps."""

    raw_policy = acquisition.get("endpoint_snap_policy")
    if raw_policy is None:
        return {}, 0, 0, hashlib.sha256(_canonical_json([])).hexdigest()
    if not isinstance(raw_policy, dict):
        raise MapPlotterError("Transit endpoint_snap_policy must be an object.")
    expected_policy_fields = {
        "policy_version",
        "allowed_consecutive_way_pairs",
        "expected_occurrence_count",
        "expected_connector_count",
        "expected_evidence_sha256",
    }
    if set(raw_policy) != expected_policy_fields:
        raise MapPlotterError(
            "Transit endpoint_snap_policy fields do not match the strict contract."
        )
    if raw_policy.get("policy_version") != "consecutive-route-members-v1":
        raise MapPlotterError(
            "Transit endpoint_snap_policy has an unsupported policy_version."
        )
    raw_rules = raw_policy.get("allowed_consecutive_way_pairs")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise MapPlotterError(
            "Transit endpoint_snap_policy needs an explicit non-empty way-pair allowlist."
        )
    rules: dict[tuple[int, int], EndpointSnapRule] = {}
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise MapPlotterError(
                f"Transit endpoint snap rule {index} must be an object."
            )
        expected_rule_fields = {
            "first_way_id",
            "second_way_id",
            "maximum_distance_m",
            "expected_occurrence_count",
            "reason",
        }
        if set(raw_rule) != expected_rule_fields:
            raise MapPlotterError(
                f"Transit endpoint snap rule {index} fields do not match the "
                "strict contract."
            )
        first_way_id = raw_rule.get("first_way_id")
        second_way_id = raw_rule.get("second_way_id")
        maximum_distance_m = raw_rule.get("maximum_distance_m")
        expected_occurrence_count = raw_rule.get("expected_occurrence_count")
        reason = raw_rule.get("reason")
        if (
            isinstance(first_way_id, bool)
            or not isinstance(first_way_id, int)
            or first_way_id < 1
            or isinstance(second_way_id, bool)
            or not isinstance(second_way_id, int)
            or second_way_id < 1
            or first_way_id == second_way_id
        ):
            raise MapPlotterError(
                f"Transit endpoint snap rule {index} has invalid way IDs."
            )
        if (
            isinstance(maximum_distance_m, bool)
            or not isinstance(maximum_distance_m, (int, float))
            or not 0.0
            < float(maximum_distance_m)
            <= MAX_CONSECUTIVE_ROUTE_ENDPOINT_SNAP_M
        ):
            raise MapPlotterError(
                f"Transit endpoint snap rule {index} must use a positive limit no "
                f"greater than {MAX_CONSECUTIVE_ROUTE_ENDPOINT_SNAP_M:g} m."
            )
        if (
            isinstance(expected_occurrence_count, bool)
            or not isinstance(expected_occurrence_count, int)
            or expected_occurrence_count < 1
        ):
            raise MapPlotterError(
                f"Transit endpoint snap rule {index} has an invalid occurrence pin."
            )
        if not isinstance(reason, str) or not reason.strip():
            raise MapPlotterError(
                f"Transit endpoint snap rule {index} needs a review reason."
            )
        key = (first_way_id, second_way_id)
        if key in rules:
            raise MapPlotterError(
                f"Transit endpoint snap pair {first_way_id}->{second_way_id} is duplicated."
            )
        rules[key] = EndpointSnapRule(
            first_way_id=first_way_id,
            second_way_id=second_way_id,
            maximum_distance_m=float(maximum_distance_m),
            expected_occurrence_count=expected_occurrence_count,
            reason=reason.strip(),
        )
    expected_occurrence_count = raw_policy.get("expected_occurrence_count")
    expected_connector_count = raw_policy.get("expected_connector_count")
    expected_evidence_sha256 = raw_policy.get("expected_evidence_sha256")
    if (
        isinstance(expected_occurrence_count, bool)
        or not isinstance(expected_occurrence_count, int)
        or expected_occurrence_count
        != sum(rule.expected_occurrence_count for rule in rules.values())
    ):
        raise MapPlotterError(
            "Transit endpoint snap policy occurrence pin does not equal its rule pins."
        )
    if (
        isinstance(expected_connector_count, bool)
        or not isinstance(expected_connector_count, int)
        or not 1 <= expected_connector_count <= expected_occurrence_count
    ):
        raise MapPlotterError(
            "Transit endpoint snap policy has an invalid connector-count pin."
        )
    if not isinstance(expected_evidence_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_evidence_sha256
    ):
        raise MapPlotterError(
            "Transit endpoint snap policy needs a lower-case SHA-256 evidence pin."
        )
    return (
        rules,
        expected_occurrence_count,
        expected_connector_count,
        expected_evidence_sha256,
    )


class SnapshotClient:
    def __init__(
        self, *, user_agent: str, cache_dir: Path, minimum_interval_s: float = 0.15
    ):
        if len(user_agent.strip()) < 8:
            raise MapPlotterError(
                "Transit acquisition requires an identifying User-Agent."
            )
        self.user_agent = user_agent.strip()
        self.cache_dir = cache_dir
        self.minimum_interval_s = max(0.0, float(minimum_interval_s))
        self.last_request = 0.0
        self._retrieved_at: dict[str, date] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.bin", self.cache_dir / f"{key}.json"

    def _record_snapshot(
        self,
        url: str,
        payload: bytes,
        metadata_path: Path,
        *,
        fetched_at: date | None = None,
    ) -> date:
        digest = hashlib.sha256(payload).hexdigest()
        if metadata_path.exists():
            try:
                value = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MapPlotterError(
                    f"Transit snapshot metadata {metadata_path} is invalid: {exc}"
                ) from exc
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != 1
                or value.get("url") != url
                or value.get("sha256") != digest
                or value.get("byte_count") != len(payload)
            ):
                raise MapPlotterError(
                    f"Transit snapshot metadata does not match cached bytes for {url}."
                )
            try:
                retrieved = date.fromisoformat(str(value.get("retrieved_at", "")))
            except ValueError as exc:
                raise MapPlotterError(
                    f"Transit snapshot metadata has an invalid retrieval date for {url}."
                ) from exc
        else:
            retrieved = fetched_at or date.today()
            metadata = {
                "schema_version": 1,
                "url": url,
                "retrieved_at": retrieved.isoformat(),
                "sha256": digest,
                "byte_count": len(payload),
            }
            temporary = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
            temporary.write_bytes(_canonical_json(metadata) + b"\n")
            temporary.replace(metadata_path)
        self._retrieved_at[url] = retrieved
        return retrieved

    def retrieved_at(self, url: str) -> date:
        if url not in self._retrieved_at:
            raise MapPlotterError(f"Transit source {url} has not been snapshotted.")
        return self._retrieved_at[url]

    @property
    def latest_retrieved_at(self) -> date:
        if not self._retrieved_at:
            raise MapPlotterError("No transit source has been snapshotted.")
        return max(self._retrieved_at.values())

    def get(self, url: str, *, expect_json: bool = False) -> tuple[bytes, bool]:
        path, metadata_path = self._cache_paths(url)
        if path.exists():
            if not metadata_path.exists():
                raise MapPlotterError(
                    f"Transit snapshot {url} has cached bytes but no retrieval "
                    "metadata; refetch it into a new cache before compiling."
                )
            payload = path.read_bytes()
            if expect_json:
                _validate_json_payload(payload, url)
            self._record_snapshot(url, payload, metadata_path)
            return payload, True
        elapsed = time.monotonic() - self.last_request
        if elapsed < self.minimum_interval_s:
            time.sleep(self.minimum_interval_s - elapsed)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MapPlotterError(
                f"Could not fetch transit source {url}: {exc}"
            ) from exc
        self.last_request = time.monotonic()
        if expect_json:
            _validate_json_payload(payload, url)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
        self._record_snapshot(url, payload, metadata_path, fetched_at=date.today())
        return payload, False

    def json(self, url: str) -> tuple[dict[str, Any], bytes, bool]:
        payload, cached = self.get(url, expect_json=True)
        value = json.loads(payload)
        assert isinstance(value, dict)
        return value, payload, cached


def _validate_json_payload(payload: bytes, source: str) -> None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Transit source {source} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise MapPlotterError(f"Transit source {source} must contain a JSON object.")


def _elements(document: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    values = document.get("elements")
    if not isinstance(values, list):
        raise MapPlotterError(f"OSM response {source} has no elements list.")
    return [value for value in values if isinstance(value, dict)]


def _relation(document: dict[str, Any], relation_id: int) -> dict[str, Any]:
    matches = [
        value
        for value in _elements(document, source=f"relation/{relation_id}")
        if value.get("type") == "relation" and value.get("id") == relation_id
    ]
    if len(matches) != 1:
        raise MapPlotterError(
            f"OSM response does not contain relation/{relation_id} exactly once."
        )
    return matches[0]


def _member_relations(relation: dict[str, Any]) -> list[int]:
    members = relation.get("members", [])
    return [
        int(member["ref"])
        for member in members
        if isinstance(member, dict)
        and member.get("type") == "relation"
        and isinstance(member.get("ref"), int)
    ]


def _tags(value: dict[str, Any]) -> dict[str, str]:
    raw = value.get("tags", {})
    return (
        {str(key): str(item) for key, item in raw.items()}
        if isinstance(raw, dict)
        else {}
    )


def _relation_kind(relation: dict[str, Any]) -> str:
    tags = _tags(relation)
    return tags.get("type", "")


def _discover_route_relations(
    client: SnapshotClient,
    root_ids: Sequence[int],
) -> tuple[dict[int, dict[str, Any]], dict[int, bytes], dict[int, tuple[int, ...]]]:
    metadata: dict[int, dict[str, Any]] = {}
    payloads: dict[int, bytes] = {}
    route_master_routes: dict[int, tuple[int, ...]] = {}
    pending = list(dict.fromkeys(int(value) for value in root_ids))
    visited: set[int] = set()
    while pending:
        relation_id = pending.pop(0)
        if relation_id in visited:
            continue
        document, payload, _ = client.json(f"{OSM_API}/relation/{relation_id}.json")
        relation = _relation(document, relation_id)
        metadata[relation_id] = relation
        payloads[relation_id] = payload
        visited.add(relation_id)
        kind = _relation_kind(relation)
        children = _member_relations(relation)
        if kind == "route_master":
            route_master_routes[relation_id] = tuple(children)
        elif kind == "network":
            pending.extend(children)
        elif kind == "route":
            continue
        else:
            raise MapPlotterError(
                f"OSM relation/{relation_id} is type={kind!r}, not network, route_master, or route."
            )
    return metadata, payloads, route_master_routes


def _route_full(
    client: SnapshotClient, relation_id: int
) -> tuple[dict[str, Any], bytes]:
    document, payload, _ = client.json(f"{OSM_API}/relation/{relation_id}/full.json")
    relation = _relation(document, relation_id)
    if _relation_kind(relation) != "route":
        raise MapPlotterError(f"OSM relation/{relation_id} is not a route relation.")
    return document, payload


def _centroid_for_element(
    element: dict[str, Any],
    element_index: dict[tuple[str, int], dict[str, Any]],
    ancestry: frozenset[tuple[str, int]] = frozenset(),
) -> tuple[float, float] | None:
    object_type = str(element.get("type", ""))
    object_id = element.get("id")
    identity = (object_type, int(object_id)) if isinstance(object_id, int) else None
    if identity is not None and identity in ancestry:
        return None
    child_ancestry = ancestry | ({identity} if identity is not None else set())
    if element.get("type") == "node":
        lat = element.get("lat")
        lon = element.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return (float(lon), float(lat))
    members: list[dict[str, Any]] = []
    if element.get("type") == "way":
        members = [
            element_index[("node", int(node_id))]
            for node_id in element.get("nodes", [])
            if ("node", int(node_id)) in element_index
        ]
    elif element.get("type") == "relation":
        members = []
        for relation_member in element.get("members", []):
            if not isinstance(relation_member, dict):
                continue
            reference = relation_member.get("ref")
            if not isinstance(reference, int):
                continue
            key = (str(relation_member.get("type")), reference)
            if key in element_index:
                members.append(element_index[key])
    points = [
        point
        for member in members
        if (
            point := _centroid_for_element(
                member,
                element_index,
                frozenset(child_ancestry),
            )
        )
        is not None
    ]
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _nearest_start_direction(
    previous_end: int,
    candidate_endpoints: tuple[int, int],
    endpoint_positions: dict[int, tuple[float, float]],
) -> tuple[str, int, float]:
    start, end = candidate_endpoints
    if previous_end not in endpoint_positions:
        raise MapPlotterError(
            f"OSM route traversal lacks endpoint geometry for node/{previous_end}."
        )
    for node_id in (start, end):
        if node_id not in endpoint_positions:
            raise MapPlotterError(
                f"OSM route traversal lacks endpoint geometry for node/{node_id}."
            )
    start_distance = _station_distance_m(
        endpoint_positions[previous_end], endpoint_positions[start]
    )
    end_distance = _station_distance_m(
        endpoint_positions[previous_end], endpoint_positions[end]
    )
    if start_distance <= end_distance:
        return "forward", start, start_distance
    return "reverse", end, end_distance


def _connector_identity(
    first_node_id: int, second_node_id: int
) -> tuple[str, str, int, int]:
    from_node_id, to_node_id = sorted((first_node_id, second_node_id))
    return (
        f"osm-endpoint-snap-{from_node_id}-{to_node_id}",
        "forward" if first_node_id == from_node_id else "reverse",
        from_node_id,
        to_node_id,
    )


def _way_direction_sequence_with_snaps(
    way_ids: Sequence[int],
    endpoints: dict[int, tuple[int, int]],
    endpoint_positions: dict[int, tuple[float, float]],
    *,
    route_id: int,
    line_id: str,
    snap_rules: dict[tuple[int, int], EndpointSnapRule],
) -> tuple[list[dict[str, str]], list[int], list[EndpointSnapEvidence]]:
    """Orient one ordered OSM route and encode only allowlisted tiny gaps.

    Exact shared node IDs always win.  At a disconnected boundary the next
    way is oriented toward the geographically nearest endpoint.  A connector
    is emitted only when that *ordered* way pair is catalog allowlisted and its
    measured gap is inside the pair-specific limit; every other gap remains a
    declared break.
    """

    if not way_ids:
        return [], [], []
    directions: list[str] = []
    first_from, first_to = endpoints[way_ids[0]]
    if len(way_ids) == 1:
        directions.append("forward")
    else:
        second_endpoints = set(endpoints[way_ids[1]])
        if first_to in second_endpoints:
            directions.append("forward")
        elif first_from in second_endpoints:
            directions.append("reverse")
        else:
            # The first member has no predecessor, so choose the orientation
            # whose end is closest to either endpoint of the second member.
            forward_distance = min(
                _station_distance_m(
                    endpoint_positions[first_to], endpoint_positions[node_id]
                )
                for node_id in second_endpoints
            )
            reverse_distance = min(
                _station_distance_m(
                    endpoint_positions[first_from], endpoint_positions[node_id]
                )
                for node_id in second_endpoints
            )
            directions.append(
                "forward" if forward_distance <= reverse_distance else "reverse"
            )
    previous_end = first_to if directions[0] == "forward" else first_from
    boundary_snaps: dict[int, EndpointSnapEvidence] = {}
    raw_break_boundaries: set[int] = set()
    for index, way_id in enumerate(way_ids[1:], start=1):
        start, end = endpoints[way_id]
        if start == previous_end:
            direction = "forward"
            selected_start = start
            distance_m = 0.0
        elif end == previous_end:
            direction = "reverse"
            selected_start = end
            distance_m = 0.0
        else:
            direction, selected_start, distance_m = _nearest_start_direction(
                previous_end, (start, end), endpoint_positions
            )
            pair = (way_ids[index - 1], way_id)
            rule = snap_rules.get(pair)
            if rule is None or distance_m > rule.maximum_distance_m + 1e-9:
                raw_break_boundaries.add(index - 1)
            else:
                connector_id, connector_direction, _, _ = _connector_identity(
                    previous_end, selected_start
                )
                boundary_snaps[index - 1] = EndpointSnapEvidence(
                    relation_id=route_id,
                    line_id=line_id,
                    after_source_way_index=index - 1,
                    first_way_id=pair[0],
                    second_way_id=pair[1],
                    from_node_id=previous_end,
                    to_node_id=selected_start,
                    from_position=endpoint_positions[previous_end],
                    to_position=endpoint_positions[selected_start],
                    distance_m=distance_m,
                    maximum_distance_m=rule.maximum_distance_m,
                    connector_edge_id=connector_id,
                    connector_direction=connector_direction,
                    reason=rule.reason,
                )
        directions.append(direction)
        previous_end = end if direction == "forward" else start

    traversals: list[dict[str, str]] = []
    breaks: list[int] = []
    snaps: list[EndpointSnapEvidence] = []
    for index, (way_id, direction) in enumerate(zip(way_ids, directions)):
        traversals.append({"edge_id": f"osm-way-{way_id}", "direction": direction})
        if index in raw_break_boundaries:
            breaks.append(len(traversals) - 1)
        snap = boundary_snaps.get(index)
        if snap is not None:
            traversals.append(
                {
                    "edge_id": snap.connector_edge_id,
                    "direction": snap.connector_direction,
                }
            )
            snaps.append(snap)
    return traversals, breaks, snaps


def _way_direction_sequence(
    way_ids: Sequence[int],
    endpoints: dict[int, tuple[int, int]],
) -> tuple[list[dict[str, str]], list[int]]:
    """Compatibility wrapper for exact-node-only traversal unit tests."""

    endpoint_positions = {
        node_id: (float(node_id), 0.0)
        for pair in endpoints.values()
        for node_id in pair
    }
    traversals, breaks, _ = _way_direction_sequence_with_snaps(
        way_ids,
        endpoints,
        endpoint_positions,
        route_id=0,
        line_id="test-line",
        snap_rules={},
    )
    return traversals, breaks


def acquire_osm_transit_contract(
    network_id: str,
    *,
    user_agent: str,
    cache_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    catalog = catalog_network(network_id)
    acquisition = catalog.get("acquisition")
    if (
        not isinstance(acquisition, dict)
        or acquisition.get("mode") != "osm-route-relations"
    ):
        raise MapPlotterError(
            f"{network_id} is not approved for OSM route-relation acquisition; "
            "use the licensed structured source named by the catalog."
        )
    if acquisition.get("release_gate") != "enabled":
        raise MapPlotterError(
            f"{network_id} acquisition is gated: {acquisition.get('release_gate_reason', 'catalog review required')}."
        )
    qa = catalog.get("qa", {})
    if not isinstance(qa, dict):
        raise MapPlotterError(f"Transit catalog {network_id!r} qa must be an object.")
    (
        endpoint_snap_rules,
        expected_endpoint_snap_occurrences,
        expected_endpoint_snap_connectors,
        expected_endpoint_snap_evidence_sha256,
    ) = _endpoint_snap_rules(acquisition)
    distinct_same_name_stations = {
        _station_name_key(str(value))
        for value in qa.get("distinct_same_name_station_names", [])
    }
    client = SnapshotClient(user_agent=user_agent, cache_dir=cache_dir)
    line_records = catalog.get("lines")
    if not isinstance(line_records, list) or not line_records:
        raise MapPlotterError(f"Transit catalog {network_id!r} has no line records.")
    root_ids = [
        int(relation_id)
        for record in line_records
        if isinstance(record, dict)
        for relation_id in record.get("osm_route_master_ids", [])
    ]
    if not root_ids:
        root_ids = [int(value) for value in acquisition.get("root_relation_ids", [])]
    metadata, metadata_payloads, master_routes = _discover_route_relations(
        client, root_ids
    )
    master_to_line: dict[int, str] = {}
    for record in line_records:
        assert isinstance(record, dict)
        line_id = str(record["id"])
        for relation_id in record.get("osm_route_master_ids", []):
            master_to_line[int(relation_id)] = line_id
    if set(master_to_line) - set(master_routes):
        missing = sorted(set(master_to_line) - set(master_routes))
        raise MapPlotterError(
            "Catalog route masters are missing or not route_master relations: "
            + ", ".join(str(value) for value in missing)
        )
    route_to_line: dict[int, str] = {}
    for master_id, routes in master_routes.items():
        if master_id not in master_to_line:
            continue
        for route_id in routes:
            existing = route_to_line.setdefault(route_id, master_to_line[master_id])
            if existing != master_to_line[master_id]:
                raise MapPlotterError(
                    f"OSM route/{route_id} belongs to two catalog lines."
                )
    if not route_to_line:
        raise MapPlotterError(
            f"OSM acquisition found no route relations for {network_id}."
        )

    route_documents: dict[int, dict[str, Any]] = {}
    raw_payloads = dict(metadata_payloads)
    for route_id in sorted(route_to_line):
        document, payload = _route_full(client, route_id)
        route_documents[route_id] = document
        raw_payloads[route_id] = payload

    way_geometries: dict[int, list[tuple[float, float]]] = {}
    way_endpoints: dict[int, tuple[int, int]] = {}
    way_lines: dict[int, set[str]] = defaultdict(set)
    route_way_ids: dict[int, list[int]] = {}
    station_records: dict[str, dict[str, Any]] = {}
    route_station_ids: dict[int, list[str]] = {}
    route_tags: dict[int, dict[str, str]] = {}

    for route_id, document in route_documents.items():
        elements = _elements(document, source=f"relation/{route_id}/full")
        index = {
            (str(element.get("type")), int(element["id"])): element
            for element in elements
            if isinstance(element.get("id"), int)
        }
        relation = _relation(document, route_id)
        route_tags[route_id] = _tags(relation)
        line_id = route_to_line[route_id]
        member_ways: list[int] = []
        member_stations: list[str] = []
        station_candidates: list[dict[str, Any]] = []
        for member in relation.get("members", []):
            if not isinstance(member, dict) or not isinstance(member.get("ref"), int):
                continue
            object_type = str(member.get("type"))
            object_id = int(member["ref"])
            role = str(member.get("role", ""))
            element = index.get((object_type, object_id))
            if element is None:
                continue
            tags = _tags(element)
            if object_type == "way" and tags.get("railway", "") in _RAILWAY_VALUES:
                node_ids = [
                    int(value)
                    for value in element.get("nodes", [])
                    if isinstance(value, int)
                ]
                if len(node_ids) < 2:
                    raise MapPlotterError(
                        f"OSM way/{object_id} has fewer than two nodes."
                    )
                points: list[tuple[float, float]] = []
                for node_id in node_ids:
                    node = index.get(("node", node_id))
                    if (
                        node is None
                        or not isinstance(node.get("lon"), (int, float))
                        or not isinstance(node.get("lat"), (int, float))
                    ):
                        raise MapPlotterError(
                            f"OSM route/{route_id} way/{object_id} is missing node/{node_id} geometry."
                        )
                    points.append((float(node["lon"]), float(node["lat"])))
                prior = way_geometries.setdefault(object_id, points)
                if prior != points:
                    raise MapPlotterError(
                        f"OSM way/{object_id} changed inside one acquisition snapshot."
                    )
                way_endpoints[object_id] = (node_ids[0], node_ids[-1])
                way_lines[object_id].add(line_id)
                member_ways.append(object_id)
                continue
            if role not in _STOP_ROLES:
                continue
            point = _centroid_for_element(element, index)
            if point is None:
                continue
            station_candidates.append(
                {
                    "role": role,
                    "object_type": object_type,
                    "object_id": object_id,
                    "tags": tags,
                    "point": point,
                }
            )
        # Public-transport route relations often list both one stop_position
        # and one or more platform objects for the same visit, but occasional
        # stations are platform-only.  Keep every stop.  Keep a platform only
        # when it is not a named/generic duplicate of a nearby stop; this
        # preserves platform-only stations without double-counting ordinary
        # stop/platform pairs.
        stop_candidates = [
            candidate
            for candidate in station_candidates
            if str(candidate["role"]).startswith("stop")
        ]
        selected_candidates: list[dict[str, Any]] = []
        for candidate in station_candidates:
            if str(candidate["role"]).startswith("stop"):
                selected_candidates.append(candidate)
                continue
            candidate_tags = candidate["tags"]
            candidate_name = candidate_tags.get("name") or candidate_tags.get(
                "official_name"
            )
            if _looks_like_platform_label(candidate_name):
                continue
            nearest_stop = min(
                stop_candidates,
                key=lambda stop: _station_distance_m(
                    tuple(candidate["point"]), tuple(stop["point"])
                ),
                default=None,
            )
            if nearest_stop is not None:
                distance = _station_distance_m(
                    tuple(candidate["point"]), tuple(nearest_stop["point"])
                )
                stop_tags = nearest_stop["tags"]
                stop_name = stop_tags.get("name") or stop_tags.get("official_name")
                duplicate_name = stop_name is not None and _station_name_key(
                    candidate_name
                ) == _station_name_key(stop_name)
                if distance <= STATION_NAME_CLUSTER_LIMIT_M and duplicate_name:
                    continue
            selected_candidates.append(candidate)
        for candidate in selected_candidates:
            object_type = str(candidate["object_type"])
            object_id = int(candidate["object_id"])
            tags = candidate["tags"]
            point = candidate["point"]
            source_object = f"{object_type}/{object_id}"
            name = tags.get("name") or tags.get("official_name") or source_object
            normalized_name = _station_name_key(name)
            station_id: str | None = None
            if name != source_object:
                for candidate_id, candidate in station_records.items():
                    if candidate["_normalized_name"] != normalized_name:
                        continue
                    if _station_distance_m(tuple(candidate["position"]), point) <= (
                        DISTINCT_SAME_NAME_CLUSTER_LIMIT_M
                        if normalized_name in distinct_same_name_stations
                        else STATION_NAME_CLUSTER_LIMIT_M
                    ):
                        station_id = candidate_id
                        break
            if station_id is None:
                station_id = f"station-osm-{object_type}-{object_id}"
                station_records[station_id] = {
                    "id": station_id,
                    "kind": "station",
                    "position": [point[0], point[1]],
                    "name": name,
                    "station_tier": "local",
                    "source_ref": "osm-route-geometry",
                    "source_object": source_object,
                    "line_ids": set(),
                    "_normalized_name": normalized_name,
                    "_positions": [],
                    "_source_objects": set(),
                }
            record = station_records[station_id]
            record["line_ids"].add(line_id)
            record["_positions"].append(point)
            record["_source_objects"].add(source_object)
            if not member_stations or member_stations[-1] != station_id:
                member_stations.append(station_id)
        if not member_ways:
            raise MapPlotterError(
                f"OSM route/{route_id} contains no operational railway ways."
            )
        route_way_ids[route_id] = member_ways
        route_station_ids[route_id] = member_stations

    endpoint_ids = sorted(
        {node_id for pair in way_endpoints.values() for node_id in pair}
    )
    endpoint_positions: dict[int, tuple[float, float]] = {}
    for way_id, points in way_geometries.items():
        start_id, end_id = way_endpoints[way_id]
        endpoint_positions.setdefault(start_id, points[0])
        endpoint_positions.setdefault(end_id, points[-1])
    nodes = [
        {
            "id": f"osm-node-{node_id}",
            "kind": "junction",
            "position": list(endpoint_positions[node_id]),
            "source_ref": "osm-route-geometry",
            "source_object": f"node/{node_id}",
        }
        for node_id in endpoint_ids
    ]

    # A named stop used by multiple lines is an interchange; the first/last
    # stop of any service pattern is a terminal.  Terminal wins because it is
    # the stronger label tier in the overview renderer.
    closed_route_ids = {
        route_id
        for route_id, way_ids in route_way_ids.items()
        if _route_way_graph_is_closed(way_ids, way_endpoints)
    }
    terminal_ids = {
        station_id
        for route_id, station_ids in route_station_ids.items()
        if route_id not in closed_route_ids
        if station_ids
        for station_id in (station_ids[0], station_ids[-1])
    }
    for station_id, record in sorted(station_records.items()):
        line_ids = record.pop("line_ids")
        positions = record.pop("_positions")
        record.pop("_normalized_name")
        source_objects = record.pop("_source_objects")
        record["position"] = [
            sum(point[0] for point in positions) / len(positions),
            sum(point[1] for point in positions) / len(positions),
        ]
        record["source_object"] = " ".join(sorted(source_objects))
        if station_id in terminal_ids:
            record["kind"] = "terminal"
            record["station_tier"] = "terminal"
        elif len(line_ids) > 1:
            record["kind"] = "interchange"
            record["station_tier"] = "interchange"
        nodes.append(record)

    edges = [
        {
            "id": f"osm-way-{way_id}",
            "from_node": f"osm-node-{way_endpoints[way_id][0]}",
            "to_node": f"osm-node-{way_endpoints[way_id][1]}",
            "geometry": [[lon, lat] for lon, lat in way_geometries[way_id]],
            "line_ids": sorted(way_lines[way_id]),
            "source_ref": "osm-route-geometry",
            "source_object": f"way/{way_id}",
            "status": "operational",
            "grade": "source-tagged-or-unknown",
        }
        for way_id in sorted(way_geometries)
    ]
    patterns: list[dict[str, Any]] = []
    endpoint_snap_evidence: list[EndpointSnapEvidence] = []
    connector_records: dict[str, dict[str, Any]] = {}
    for route_id in sorted(route_to_line):
        line_id = route_to_line[route_id]
        traversals, breaks, route_snaps = _way_direction_sequence_with_snaps(
            route_way_ids[route_id],
            way_endpoints,
            endpoint_positions,
            route_id=route_id,
            line_id=line_id,
            snap_rules=endpoint_snap_rules,
        )
        endpoint_snap_evidence.extend(route_snaps)
        for snap in route_snaps:
            _, _, connector_from, connector_to = _connector_identity(
                snap.from_node_id, snap.to_node_id
            )
            candidate = connector_records.setdefault(
                snap.connector_edge_id,
                {
                    "id": snap.connector_edge_id,
                    "from_node": f"osm-node-{connector_from}",
                    "to_node": f"osm-node-{connector_to}",
                    "geometry": [
                        list(endpoint_positions[connector_from]),
                        list(endpoint_positions[connector_to]),
                    ],
                    "line_ids": set(),
                    "source_ref": "osm-route-geometry",
                    "source_objects": set(),
                    "status": "inferred-consecutive-route-continuity",
                    "grade": "catalog-allowlisted-consecutive-route-members",
                },
            )
            if (
                candidate["from_node"] != f"osm-node-{connector_from}"
                or candidate["to_node"] != f"osm-node-{connector_to}"
                or candidate["geometry"]
                != [
                    list(endpoint_positions[connector_from]),
                    list(endpoint_positions[connector_to]),
                ]
            ):
                raise MapPlotterError(
                    f"Endpoint snap connector {snap.connector_edge_id} is not stable."
                )
            candidate["line_ids"].add(line_id)
            candidate["source_objects"].add(
                f"relation/{route_id}:way/{snap.first_way_id}->way/{snap.second_way_id}"
            )
        tags = route_tags[route_id]
        patterns.append(
            {
                "id": f"osm-route-{route_id}",
                "line_id": line_id,
                "name": tags.get("name") or f"OSM route {route_id}",
                "traversals": traversals,
                "station_ids": route_station_ids[route_id],
                "source_ref": "osm-route-geometry",
                "derivation_status": "ordered-osm-route-members",
                "continuity_breaks": breaks,
            }
        )
    for connector_id in sorted(connector_records):
        record = connector_records[connector_id]
        edges.append(
            {
                key: value
                for key, value in record.items()
                if key not in {"source_objects"}
            }
            | {
                "line_ids": sorted(record["line_ids"]),
                "source_object": " ".join(sorted(record["source_objects"])),
            }
        )

    endpoint_snap_dicts = [
        evidence.as_dict()
        for evidence in sorted(
            endpoint_snap_evidence,
            key=lambda item: (
                item.relation_id,
                item.after_source_way_index,
                item.first_way_id,
                item.second_way_id,
            ),
        )
    ]
    endpoint_snap_evidence_sha256 = hashlib.sha256(
        _canonical_json(endpoint_snap_dicts)
    ).hexdigest()
    if endpoint_snap_rules:
        observed_by_pair: dict[tuple[int, int], int] = defaultdict(int)
        for evidence in endpoint_snap_evidence:
            observed_by_pair[(evidence.first_way_id, evidence.second_way_id)] += 1
        for pair, rule in endpoint_snap_rules.items():
            if observed_by_pair[pair] != rule.expected_occurrence_count:
                raise MapPlotterError(
                    f"Endpoint snap pair {pair[0]}->{pair[1]} occurred "
                    f"{observed_by_pair[pair]} times; catalog expects "
                    f"{rule.expected_occurrence_count}."
                )
        if len(endpoint_snap_evidence) != expected_endpoint_snap_occurrences:
            raise MapPlotterError(
                f"Transit endpoint snap ledger has {len(endpoint_snap_evidence)} "
                f"occurrences; catalog expects {expected_endpoint_snap_occurrences}."
            )
        if len(connector_records) != expected_endpoint_snap_connectors:
            raise MapPlotterError(
                f"Transit endpoint snap ledger has {len(connector_records)} unique "
                f"connectors; catalog expects {expected_endpoint_snap_connectors}."
            )
        if endpoint_snap_evidence_sha256 != expected_endpoint_snap_evidence_sha256:
            raise MapPlotterError(
                "Transit endpoint snap evidence differs from the catalog SHA-256 pin."
            )
    elif endpoint_snap_evidence:
        raise MapPlotterError(
            "Transit acquisition emitted endpoint snaps without a catalog policy."
        )

    station_nodes = [
        node
        for node in nodes
        if node.get("kind") in {"station", "terminal", "interchange"}
    ]
    minimum_stations = int(qa.get("minimum_station_count", 1))
    maximum_stations = int(qa.get("maximum_station_count", 10000))
    if not minimum_stations <= len(station_nodes) <= maximum_stations:
        raise MapPlotterError(
            f"{network_id} acquisition found {len(station_nodes)} stations; "
            f"catalog QA requires {minimum_stations}..{maximum_stations}."
        )
    minimum_patterns = int(qa.get("minimum_service_pattern_count", len(line_records)))
    if len(patterns) < minimum_patterns:
        raise MapPlotterError(
            f"{network_id} acquisition found {len(patterns)} service patterns; "
            f"catalog QA requires at least {minimum_patterns}."
        )
    continuity_break_count = sum(
        len(pattern["continuity_breaks"]) for pattern in patterns
    )
    maximum_continuity_breaks = int(
        qa.get("maximum_continuity_break_count", continuity_break_count)
    )
    if continuity_break_count > maximum_continuity_breaks:
        raise MapPlotterError(
            f"{network_id} acquisition found {continuity_break_count} route-relation "
            f"continuity breaks; catalog QA allows at most "
            f"{maximum_continuity_breaks}."
        )
    found_terminal_names = {
        _station_name_key(str(node.get("name", "")))
        for node in station_nodes
        if node.get("kind") == "terminal"
    }
    required_terminals = {
        _station_name_key(str(value)) for value in qa.get("required_terminal_names", [])
    }
    missing_terminals = sorted(required_terminals - found_terminal_names)
    if missing_terminals:
        raise MapPlotterError(
            f"{network_id} acquisition is missing required terminal names: "
            + ", ".join(missing_terminals)
        )

    reference_sources: list[dict[str, Any]] = []
    reference_payloads: dict[str, bytes] = {}
    for source_index, source in enumerate(catalog.get("sources", [])):
        if not isinstance(source, dict):
            raise MapPlotterError(f"Catalog source {source_index} is not an object.")
        source_url = str(source["url"])
        payload, _ = client.get(source_url)
        reference_payloads[str(source["id"])] = payload
        reference_sources.append(
            {
                "id": source["id"],
                "publisher": source["publisher"],
                "url": source_url,
                "licence": source["licence"],
                "attribution": source["attribution"],
                "retrieved_at": client.retrieved_at(source_url).isoformat(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "use": source["use"],
                "commercial_reuse_status": source["commercial_reuse_status"],
                **(
                    {"valid_from": source["valid_from"]}
                    if source.get("valid_from")
                    else {}
                ),
                **({"valid_to": source["valid_to"]} if source.get("valid_to") else {}),
            }
        )
    authoritative_station_note: str | None = None
    authoritative_station_json = qa.get("authoritative_station_json")
    if authoritative_station_json is not None:
        if not isinstance(authoritative_station_json, dict):
            raise MapPlotterError(
                f"Transit catalog {network_id!r} authoritative_station_json must be an object."
            )
        source_ref = str(authoritative_station_json.get("source_ref", ""))
        if source_ref not in reference_payloads:
            raise MapPlotterError(
                f"{network_id} authoritative station QA names missing source {source_ref!r}."
            )
        try:
            authoritative_document = json.loads(reference_payloads[source_ref])
        except json.JSONDecodeError as exc:
            raise MapPlotterError(
                f"{network_id} authoritative station source is not JSON: {exc}"
            ) from exc
        if not isinstance(authoritative_document, dict):
            raise MapPlotterError(
                f"{network_id} authoritative station source must be a JSON object."
            )
        list_field = str(authoritative_station_json.get("list_field", ""))
        records = authoritative_document.get(list_field)
        if not isinstance(records, list):
            raise MapPlotterError(
                f"{network_id} authoritative station source lacks list {list_field!r}."
            )
        filter_field = str(authoritative_station_json.get("filter_field", ""))
        filter_value = authoritative_station_json.get("filter_value")
        authoritative_count = sum(
            isinstance(record, dict) and record.get(filter_field) == filter_value
            for record in records
        )
        expected_count = int(authoritative_station_json.get("expected_count", -1))
        if authoritative_count != expected_count:
            raise MapPlotterError(
                f"{network_id} authoritative source contains {authoritative_count} filtered "
                f"stations; catalog expects {expected_count}."
            )
        if len(station_nodes) != authoritative_count:
            raise MapPlotterError(
                f"{network_id} normalized graph contains {len(station_nodes)} stations but "
                f"the authoritative source contains {authoritative_count}."
            )
        authoritative_station_note = (
            f"Normalized station count {len(station_nodes)} matches {source_ref} "
            f"filter {filter_field}={filter_value!r}."
        )
    combined_osm = hashlib.sha256()
    for relation_id in sorted(raw_payloads):
        combined_osm.update(str(relation_id).encode("ascii"))
        combined_osm.update(b"\0")
        combined_osm.update(raw_payloads[relation_id])
        combined_osm.update(b"\0")
    sources = [
        *reference_sources,
        {
            "id": "osm-route-geometry",
            "publisher": "OpenStreetMap contributors",
            "url": "https://www.openstreetmap.org/copyright",
            "licence": "ODbL 1.0",
            "attribution": "© OpenStreetMap contributors",
            "retrieved_at": client.latest_retrieved_at.isoformat(),
            "sha256": combined_osm.hexdigest(),
            "use": (
                "Geographic track geometry and community route-relation ordering; "
                "official operator sources separately verify current line identity."
            ),
            "commercial_reuse_status": "commercial-allowed",
        },
    ]
    lines = []
    for record in line_records:
        assert isinstance(record, dict)
        lines.append(
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
        )
    snapshot_date = client.latest_retrieved_at.isoformat()
    document = {
        "schema_version": 1,
        "network": {
            "id": catalog["id"],
            "name": catalog["name"],
            "kind": catalog["kind"],
            "scope": catalog["scope"],
            "format_id": catalog["format_id"],
            "snapshot": snapshot_date,
            "validity_status": "official-current-scope-with-community-geometry-review",
            "geometry_mode": "geographic-osm-route-relations",
        },
        "sources": sources,
        "lines": lines,
        "nodes": nodes,
        "edges": edges,
        "service_patterns": patterns,
        "context": [],
        "omissions": [
            {
                "kind": "context",
                "status": "not-supplied",
                "reason": "Add a separately pinned Overpass/PBF context extract before final rendering.",
            },
            *(
                [
                    {
                        "kind": "source-topology-discontinuity",
                        "status": "retained-with-explicit-endpoint-snap-connector",
                        "reason": (
                            "Catalog-reviewed consecutive OSM route members have "
                            "distinct endpoint node IDs within the strict pair-specific "
                            "distance cap; the exact endpoint-to-endpoint connector is "
                            "retained and fully enumerated rather than silently merging "
                            "source nodes."
                        ),
                        "policy": "consecutive-route-members-v1",
                        "hard_maximum_distance_m": (
                            MAX_CONSECUTIVE_ROUTE_ENDPOINT_SNAP_M
                        ),
                        "occurrence_count": len(endpoint_snap_dicts),
                        "connector_count": len(connector_records),
                        "evidence_sha256": endpoint_snap_evidence_sha256,
                        "evidence": endpoint_snap_dicts,
                    }
                ]
                if endpoint_snap_dicts
                else []
            ),
        ],
        "notes": [
            "Operator diagram geometry, logos, roundels, and proprietary typography were not copied.",
            "OSM route relations are a community service-order source and require comparison with the pinned official normal-service reference.",
            (
                "Directional stop members with the same normalized name are clustered within "
                f"{STATION_NAME_CLUSTER_LIMIT_M:g} m; declared same-name station complexes use "
                f"{DISTINCT_SAME_NAME_CLUSTER_LIMIT_M:g} m."
            ),
            *(
                [
                    (
                        f"{len(endpoint_snap_dicts)} catalog-allowlisted consecutive "
                        "route-member endpoint occurrences use "
                        f"{len(connector_records)} explicit connector; evidence SHA-256 "
                        f"{endpoint_snap_evidence_sha256}. No non-allowlisted or "
                        "over-limit proximity was joined."
                    )
                ]
                if endpoint_snap_dicts
                else []
            ),
            *([authoritative_station_note] if authoritative_station_note else []),
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_bytes(_canonical_json(document) + b"\n")
    temporary.replace(output_path)
    # Re-open through the strict consumer before claiming the contract exists.
    network = load_transit_network(output_path)
    normalized = canonical_contract_bytes(network)
    if json.loads(normalized) != json.loads(output_path.read_bytes()):
        raise MapPlotterError(
            "Transit acquisition output is not canonically reproducible."
        )
    return {
        "path": str(output_path.resolve()),
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "network_id": network.id,
        "line_count": len(network.lines),
        "station_count": sum(node.is_station for node in network.nodes),
        "edge_count": len(network.edges),
        "service_pattern_count": len(network.service_patterns),
    }
