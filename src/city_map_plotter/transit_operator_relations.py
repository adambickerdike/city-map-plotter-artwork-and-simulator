"""Audit explicit OSM ``route=train`` operator tags against an exact rail graph.

This is an honest fallback inventory, not the WTT compiler.  It can support a
plate explicitly labelled as an OSM operator-tag snapshot when coverage is
adequate.  Relation gaps are reported and never repaired; generic physical
track names or proximity are never used to infer operator service.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal

from .models import MapPlotterError
from .transit_operator_candidates import sha256_file
from .transit_operator_registry import (
    DEFAULT_OPERATOR_KEYS,
    OPERATOR_REGISTRY,
    normalize_operator_token,
)
from .transit_rail_graph import OsmRailGraph


RELATION_AUDIT_POLICY_VERSION = "osm-operator-train-relations-v2"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_SPLIT_RE = re.compile(r"\s*(?:;|\||/)\s*")
_OPERATOR_TOKENS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        key: product.current_osm_tokens
        for key, product in OPERATOR_REGISTRY.by_key.items()
    }
)
_OPERATOR_TAG_KEYS = ("operator", "network", "brand", "operator:short")
_REVIEWED_RELATION_ID_TO_KEY = {
    relation_id: key
    for key, product in OPERATOR_REGISTRY.by_key.items()
    for relation_id in product.reviewed_relation_allowlist
}
_ALIGNMENT_WAY_ROLES = frozenset(
    {"", "route", "forward", "backward"}
)
_SUPPORT_WAY_ROLES = frozenset(
    {
        "platform",
        "platform_entry_only",
        "platform_exit_only",
        "station",
        "stop",
    }
)


@dataclass(frozen=True, slots=True)
class OsmRelationMember:
    member_type: Literal["node", "way", "relation", "unknown"]
    ref: int
    role: str


@dataclass(frozen=True, slots=True)
class OsmTrainRelationRecord:
    relation_id: int
    operator_code: str
    matched_tags: tuple[tuple[str, str], ...]
    tags: tuple[tuple[str, str], ...]
    members: tuple[OsmRelationMember, ...]
    osm_version: int | None
    osm_timestamp: str | None
    selection_method: str = "explicit-current-operator-tag"


@dataclass(frozen=True, slots=True)
class OsmRelationScanExclusion:
    relation_id: int
    reason: str
    operator_codes: tuple[str, ...]
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class OsmTrainRelationScan:
    records: tuple[OsmTrainRelationRecord, ...]
    exclusions: tuple[OsmRelationScanExclusion, ...]


def _normalise_operator_token(value: str) -> str:
    return normalize_operator_token(value)


def explicit_operator_codes(tags: Mapping[str, str]) -> tuple[str, ...]:
    """Match only explicit operator/network fields; relation names are ignored."""

    observed: set[str] = set()
    for key in _OPERATOR_TAG_KEYS:
        for part in _TOKEN_SPLIT_RE.split(tags.get(key, "")):
            token = _normalise_operator_token(part)
            if token:
                observed.add(token)
    return tuple(
        code
        for code, accepted in sorted(_OPERATOR_TOKENS.items())
        if observed.intersection(accepted)
    )


def explicit_legacy_operator_codes(tags: Mapping[str, str]) -> tuple[str, ...]:
    """Return registry keys matched only by denied historical source tokens."""

    observed: set[str] = set()
    for key in _OPERATOR_TAG_KEYS:
        for part in _TOKEN_SPLIT_RE.split(tags.get(key, "")):
            token = _normalise_operator_token(part)
            if token:
                observed.add(token)
    return tuple(
        key
        for key in sorted(DEFAULT_OPERATOR_KEYS)
        if any(
            key in OPERATOR_REGISTRY.legacy_token_to_keys.get(token, ())
            for token in observed
        )
    )


def way_member_role_classification(
    role: str,
) -> Literal["alignment", "support", "unsupported"]:
    """Classify a way role without treating platforms as route alignment."""

    normalised = role.strip().casefold()
    if normalised in _ALIGNMENT_WAY_ROLES:
        return "alignment"
    if normalised in _SUPPORT_WAY_ROLES or normalised.startswith("platform_"):
        return "support"
    return "unsupported"


def _member_type(value: Any) -> Literal["node", "way", "relation", "unknown"]:
    raw = str(getattr(value, "type", "")).casefold()
    return {
        "n": "node",
        "node": "node",
        "w": "way",
        "way": "way",
        "r": "relation",
        "relation": "relation",
    }.get(raw, "unknown")  # type: ignore[return-value]


def stream_explicit_operator_train_relations(
    path: Path,
    *,
    expected_sha256: str,
    source_hash_already_verified: bool = False,
) -> tuple[OsmTrainRelationRecord, ...]:
    """Stream relations matched by explicit operator tags from one pinned PBF."""

    return scan_explicit_operator_train_relations(
        path,
        expected_sha256=expected_sha256,
        source_hash_already_verified=source_hash_already_verified,
    ).records


def scan_explicit_operator_train_relations(
    path: Path,
    *,
    expected_sha256: str,
    source_hash_already_verified: bool = False,
) -> OsmTrainRelationScan:
    """Stream selected records and quantify ambiguous/master exclusions."""

    try:
        resolved = path.resolve(strict=True)
        initial_stat = resolved.stat()
    except OSError as exc:
        raise MapPlotterError(f"Cannot inspect operator-relation PBF: {exc}") from exc
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise MapPlotterError("Expected PBF SHA-256 is malformed.")
    if not source_hash_already_verified:
        actual, _ = sha256_file(path)
        if actual != expected_sha256:
            raise MapPlotterError(
                f"Operator relation PBF hash mismatch: expected {expected_sha256}, "
                f"got {actual}."
            )
    osmium = importlib.import_module("osmium")
    base = getattr(osmium, "SimpleHandler", None)
    if base is None:
        raise MapPlotterError("PyOsmium does not expose SimpleHandler.")

    class _RelationHandler(base):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.records: dict[int, OsmTrainRelationRecord] = {}
            self.exclusions: dict[int, OsmRelationScanExclusion] = {}

        def relation(self, value: Any) -> None:
            tags = {str(tag.k): str(tag.v) for tag in value.tags}
            relation_type = tags.get("type", "").casefold()
            route = tags.get("route", "").casefold()
            route_master = tags.get("route_master", "").casefold()
            codes = explicit_operator_codes(tags)
            explicit_codes = codes
            legacy_codes = explicit_legacy_operator_codes(tags)
            relation_id = int(value.id)
            reviewed_code = _REVIEWED_RELATION_ID_TO_KEY.get(relation_id)
            selected_tags = tuple(
                (key, tags[key])
                for key in (
                    "type",
                    "route",
                    "route_master",
                    "name",
                    "ref",
                    "from",
                    "to",
                    "via",
                    "operator",
                    "network",
                    "brand",
                    "operator:short",
                )
                if key in tags
            )
            if relation_type == "route_master" and (
                route_master == "train" or route == "train"
            ):
                if codes or legacy_codes:
                    self.exclusions[relation_id] = OsmRelationScanExclusion(
                        relation_id=relation_id,
                        reason=(
                            "direct-route-master-not-expanded"
                            if codes
                            else "legacy-operator-token"
                        ),
                        operator_codes=codes or legacy_codes,
                        tags=selected_tags,
                    )
                return
            if relation_type != "route" or route != "train":
                return
            if reviewed_code is not None:
                if codes and codes != (reviewed_code,):
                    self.exclusions[relation_id] = OsmRelationScanExclusion(
                        relation_id=relation_id,
                        reason="reviewed-relation-id-conflicts-with-current-tag",
                        operator_codes=tuple(sorted(set((*codes, reviewed_code)))),
                        tags=selected_tags,
                    )
                    return
                if legacy_codes:
                    self.exclusions[relation_id] = OsmRelationScanExclusion(
                        relation_id=relation_id,
                        reason="reviewed-relation-id-has-legacy-operator-token",
                        operator_codes=tuple(
                            sorted(set((*legacy_codes, reviewed_code)))
                        ),
                        tags=selected_tags,
                    )
                    return
                codes = (reviewed_code,)
            if codes and legacy_codes:
                self.exclusions[relation_id] = OsmRelationScanExclusion(
                    relation_id=relation_id,
                    reason="mixed-current-and-legacy-operator-tokens",
                    operator_codes=tuple(sorted(set((*codes, *legacy_codes)))),
                    tags=selected_tags,
                )
                return
            if len(codes) > 1:
                self.exclusions[relation_id] = OsmRelationScanExclusion(
                    relation_id=relation_id,
                    reason="ambiguous-multiple-operator-codes",
                    operator_codes=codes,
                    tags=selected_tags,
                )
                return
            if not codes:
                if legacy_codes:
                    self.exclusions[relation_id] = OsmRelationScanExclusion(
                        relation_id=relation_id,
                        reason="legacy-operator-token",
                        operator_codes=legacy_codes,
                        tags=selected_tags,
                    )
                return
            members = tuple(
                OsmRelationMember(
                    member_type=_member_type(member),
                    ref=int(member.ref),
                    role=str(member.role),
                )
                for member in value.members
            )
            matched_tags = tuple(
                (key, tags[key])
                for key in _OPERATOR_TAG_KEYS
                if tags.get(key, "").strip()
            )
            selection_method = (
                "catalog-reviewed-relation-id"
                if relation_id in _REVIEWED_RELATION_ID_TO_KEY
                and not explicit_codes
                else "explicit-current-operator-tag"
            )
            version_raw = getattr(value, "version", None)
            timestamp_raw = getattr(value, "timestamp", None)
            self.records[relation_id] = OsmTrainRelationRecord(
                relation_id=relation_id,
                operator_code=codes[0],
                matched_tags=matched_tags,
                tags=selected_tags,
                members=members,
                osm_version=int(version_raw) if version_raw else None,
                osm_timestamp=str(timestamp_raw) if timestamp_raw else None,
                selection_method=selection_method,
            )

    handler = _RelationHandler()
    try:
        handler.apply_file(str(resolved), locations=False)
        final_stat = resolved.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise MapPlotterError(f"Cannot stream operator train relations: {exc}") from exc
    signature = lambda value: (  # noqa: E731 - local immutable stat tuple.
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )
    if signature(initial_stat) != signature(final_stat):
        raise MapPlotterError("Operator-relation PBF changed during its streaming pass.")
    return OsmTrainRelationScan(
        records=tuple(
            sorted(handler.records.values(), key=lambda item: item.relation_id)
        ),
        exclusions=tuple(
            sorted(handler.exclusions.values(), key=lambda item: item.relation_id)
        ),
    )


def _component_count(graph: OsmRailGraph, edge_ids: Iterable[str]) -> int:
    adjacency: dict[int, set[int]] = defaultdict(set)
    all_nodes: set[int] = set()
    for edge_id in edge_ids:
        edge = graph.edges[edge_id]
        first, second = edge.node_ids
        all_nodes.update((first, second))
        adjacency[first].add(second)
        adjacency[second].add(first)
    count = 0
    visited: set[int] = set()
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
            queue.extend(sorted(adjacency[node_id].difference(visited)))
    return count


def _relation_evidence(
    relation: OsmTrainRelationRecord, graph: OsmRailGraph
) -> dict[str, Any]:
    resolved_way_ids: list[int] = []
    missing_way_ids: list[int] = []
    support_way_ids: list[int] = []
    missing_support_way_ids: list[int] = []
    unsupported_members: list[dict[str, Any]] = []
    ordered_way_endpoints: list[tuple[int, tuple[int, int]]] = []
    ordered_members: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
    edge_ids_by_way: dict[int, list[str]] = defaultdict(list)
    for edge in graph.edges.values():
        edge_ids_by_way[edge.source_way_id].append(edge.edge_id)

    for index, member in enumerate(relation.members):
        item: dict[str, Any] = {
            "index": index,
            "type": member.member_type,
            "ref": member.ref,
            "role": member.role,
            "graph_resolution": "non-geometry-member",
        }
        if member.member_type == "way":
            role_class = way_member_role_classification(member.role)
            item["route_role_class"] = role_class
            way = graph.ways.get(member.ref)
            if role_class == "support":
                support_way_ids.append(member.ref)
                if way is None:
                    missing_support_way_ids.append(member.ref)
                    item["graph_resolution"] = "missing-support-way-not-route-gap"
                else:
                    item["graph_resolution"] = "exact-support-way-not-alignment"
            elif role_class == "unsupported":
                unsupported_members.append(item.copy())
                item["graph_resolution"] = "unsupported-way-role"
            elif way is None:
                missing_way_ids.append(member.ref)
                item["graph_resolution"] = "missing-or-excluded-alignment-way"
            else:
                resolved_way_ids.append(member.ref)
                item["graph_resolution"] = "exact-selected-alignment-way"
                way_edge_ids = sorted(edge_ids_by_way[member.ref])
                item["graph_edge_ids"] = way_edge_ids
                edge_ids.update(way_edge_ids)
                first, last = way.node_refs[0], way.node_refs[-1]
                if member.role.casefold() in {"backward", "reverse"}:
                    first, last = last, first
                ordered_way_endpoints.append((index, (first, last)))
        elif member.member_type in {"relation", "unknown"}:
            unsupported_members.append(item.copy())
            item["graph_resolution"] = "unsupported-nested-or-unknown-member"
        ordered_members.append(item)

    discontinuities: list[dict[str, Any]] = []
    for (first_index, first_ends), (second_index, second_ends) in zip(
        ordered_way_endpoints, ordered_way_endpoints[1:], strict=False
    ):
        shared = sorted(set(first_ends).intersection(second_ends))
        if not shared:
            discontinuities.append(
                {
                    "from_member_index": first_index,
                    "to_member_index": second_index,
                    "from_endpoint_node_ids": list(first_ends),
                    "to_endpoint_node_ids": list(second_ends),
                }
            )
    ordered_edge_ids = sorted(edge_ids)
    length_m = sum(graph.edges[edge_id].length_m for edge_id in ordered_edge_ids)
    return {
        "relation_id": relation.relation_id,
        "operator_code": relation.operator_code,
        "selection_method": relation.selection_method,
        "matched_tags": [list(item) for item in relation.matched_tags],
        "tags": dict(relation.tags),
        "osm_version": relation.osm_version,
        "osm_timestamp": relation.osm_timestamp,
        "ordered_members": ordered_members,
        "resolved_way_ids": resolved_way_ids,
        "missing_or_excluded_way_ids": missing_way_ids,
        "support_way_ids": support_way_ids,
        "missing_support_way_ids": missing_support_way_ids,
        "unsupported_member_count": len(unsupported_members),
        "exact_graph_edge_ids": ordered_edge_ids,
        "exact_graph_edge_count": len(ordered_edge_ids),
        "exact_graph_length_m": length_m,
        "exact_node_component_count": _component_count(graph, ordered_edge_ids),
        "ordered_way_discontinuities": discontinuities,
        "complete_against_graph_policy": not missing_way_ids
        and not unsupported_members
        and not discontinuities,
    }


def audit_operator_train_relations(
    records: Iterable[OsmTrainRelationRecord], graph: OsmRailGraph
) -> dict[str, Any]:
    """Resolve selected relation member ways exactly and report every gap."""

    evidence = [_relation_evidence(record, graph) for record in records]
    by_operator: dict[str, dict[str, Any]] = {}
    for code in sorted(_OPERATOR_TOKENS):
        selected = [item for item in evidence if item["operator_code"] == code]
        edge_ids = {
            edge_id for item in selected for edge_id in item["exact_graph_edge_ids"]
        }
        by_operator[code] = {
            "relation_count": len(selected),
            "complete_relation_count": sum(
                bool(item["complete_against_graph_policy"]) for item in selected
            ),
            "resolved_member_way_occurrence_count": sum(
                len(item["resolved_way_ids"]) for item in selected
            ),
            "missing_or_excluded_member_way_occurrence_count": sum(
                len(item["missing_or_excluded_way_ids"]) for item in selected
            ),
            "support_member_way_occurrence_count": sum(
                len(item["support_way_ids"]) for item in selected
            ),
            "missing_support_member_way_occurrence_count": sum(
                len(item["missing_support_way_ids"]) for item in selected
            ),
            "unsupported_member_occurrence_count": sum(
                int(item["unsupported_member_count"]) for item in selected
            ),
            "ordered_way_discontinuity_count": sum(
                len(item["ordered_way_discontinuities"]) for item in selected
            ),
            "unique_exact_graph_edge_count": len(edge_ids),
            "unique_exact_graph_length_m": sum(
                graph.edges[edge_id].length_m for edge_id in edge_ids
            ),
            "exact_node_component_count": _component_count(graph, edge_ids),
        }
    document: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": RELATION_AUDIT_POLICY_VERSION,
        "release_state": "candidate-not-reviewed",
        "approved": False,
        "geometry_claim": "explicit-osm-operator-tag-snapshot-only",
        "wtt_compiled": False,
        "operator_alignment_approved": False,
        "invented_connector_count": 0,
        "proximity_join_count": 0,
        "graph_source_sha256": graph.source.sha256,
        "graph_sha256": graph.graph_sha256,
        "operator_summary": by_operator,
        "relations": evidence,
    }
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    document["ordered_evidence_sha256"] = hashlib.sha256(payload).hexdigest()
    return document


__all__ = [
    "OsmRelationMember",
    "OsmRelationScanExclusion",
    "OsmTrainRelationRecord",
    "OsmTrainRelationScan",
    "RELATION_AUDIT_POLICY_VERSION",
    "audit_operator_train_relations",
    "explicit_legacy_operator_codes",
    "explicit_operator_codes",
    "scan_explicit_operator_train_relations",
    "stream_explicit_operator_train_relations",
    "way_member_role_classification",
]
