"""Compile one truthful Great Britain passenger-operator overview contract.

The overview is a dated OSM review proof, not an official service map.  It
keeps exactly one display line per represented registry product, shares each
physical OSM atomic segment across every product that references it, and
never repairs missing relation geometry.  The national plate is deliberately
limited to one owned 0.4 mm pen pass per product line.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import hashlib
from importlib import resources
import json
from typing import Any

from .models import MapPlotterError
from .pens import ACTUAL_PEN_INVENTORY
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
from .transit_operator_registry import OPERATOR_REGISTRY, REGISTRY_RESOURCE
from .transit_operator_relations import (
    OsmTrainRelationRecord,
    way_member_role_classification,
)
from .transit_operator_snapshot import (
    TargetedOperatorGeometry,
    _drawable_way_ids,
    _geometry_sha256,
    _plan_relation_runs,
    _segment_edge_ids,
)


OPERATOR_OVERVIEW_POLICY_VERSION = "gb-passenger-operator-overview-v1"
DEFAULT_OVERVIEW_SNAPSHOT_DATE = "2026-08-06"
DEFAULT_OVERVIEW_RETRIEVED_AT = "2026-08-07"
EXPECTED_BLOCKED_PRODUCT_IDS = frozenset({"hull-trains-2026"})
EUROSTAR_PRODUCT_ID = "eurostar-2026"
HEATHROW_EXPRESS_PRODUCT_ID = "heathrow-express-2026"

_OVERVIEW_PEN_NIB_MM = 0.4
_OVERVIEW_PEN_FALLBACK_ID = "grey-0-4"
_OVERVIEW_PEN_BY_ID = {
    pen.identity: pen
    for pen in ACTUAL_PEN_INVENTORY.pens
    if abs(pen.mark_width_mm - _OVERVIEW_PEN_NIB_MM) <= 1e-9
}


def _registry_sha256() -> str:
    resource = resources.files("city_map_plotter").joinpath(REGISTRY_RESOURCE)
    try:
        return hashlib.sha256(resource.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - packaged resource invariant.
        raise MapPlotterError(
            f"Cannot hash passenger-operator registry {REGISTRY_RESOURCE}: {exc}"
        ) from exc


def _audit_evidence_sha256(document: Mapping[str, Any]) -> str:
    """Verify the canonical evidence digest without one huge JSON allocation."""

    payload = dict(document)
    payload.pop("ordered_evidence_sha256", None)
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256()
    for chunk in encoder.iterencode(payload):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _verify_audit_geometry_binding(
    geometry: TargetedOperatorGeometry,
    audit: Mapping[str, Any],
) -> None:
    """Bind the sealed audit to source, selected relations, and exact way geometry.

    Rebuilding the complete evidence document can require hundreds of megabytes
    for a national snapshot.  This verifies every compiler-relevant source
    identity, relation member, scan exclusion, and retained alignment-way
    geometry without materialising a second full audit document.
    """

    source = audit.get("source")
    if not isinstance(source, Mapping) or (
        source.get("artifact_name") != geometry.source_path.name
        or source.get("sha256") != geometry.source_sha256
        or source.get("byte_count") != geometry.source_byte_count
    ):
        raise MapPlotterError("Operator snapshot audit is not bound to this source.")

    raw_relations = audit.get("relations")
    if not isinstance(raw_relations, list):
        raise MapPlotterError("Operator snapshot audit relation ledger is malformed.")
    audit_relation_by_id: dict[int, Mapping[str, Any]] = {}
    for raw in raw_relations:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("relation_id"), int):
            raise MapPlotterError(
                "Operator snapshot audit relation ledger is malformed."
            )
        relation_id = int(raw["relation_id"])
        if relation_id in audit_relation_by_id:
            raise MapPlotterError(
                f"Operator snapshot audit repeats relation {relation_id}."
            )
        audit_relation_by_id[relation_id] = raw
    if set(audit_relation_by_id) != {
        relation.relation_id for relation in geometry.relations
    }:
        raise MapPlotterError(
            "Operator snapshot audit selected-relation IDs do not match geometry."
        )
    for relation in geometry.relations:
        raw = audit_relation_by_id[relation.relation_id]
        if (
            raw.get("operator_code") != relation.operator_code
            or raw.get("selection_method") != relation.selection_method
            or raw.get("matched_tags") != [list(item) for item in relation.matched_tags]
            or raw.get("tags") != dict(relation.tags)
            or raw.get("osm_version") != relation.osm_version
            or raw.get("osm_timestamp") != relation.osm_timestamp
        ):
            raise MapPlotterError(
                f"Operator snapshot audit relation {relation.relation_id} "
                "identity does not match geometry."
            )
        ordered_members = raw.get("ordered_members")
        if not isinstance(ordered_members, list) or len(ordered_members) != len(
            relation.members
        ):
            raise MapPlotterError(
                f"Operator snapshot audit relation {relation.relation_id} "
                "member ledger does not match geometry."
            )
        for index, (member, raw_member) in enumerate(
            zip(relation.members, ordered_members)
        ):
            if not isinstance(raw_member, Mapping) or any(
                raw_member.get(field) != value
                for field, value in (
                    ("index", index),
                    ("type", member.member_type),
                    ("ref", member.ref),
                    ("role", member.role),
                )
            ):
                raise MapPlotterError(
                    f"Operator snapshot audit relation {relation.relation_id} "
                    f"member {index} does not match geometry."
                )

    expected_exclusions = [
        {
            "relation_id": item.relation_id,
            "reason": item.reason,
            "operator_codes": list(item.operator_codes),
            "tags": dict(item.tags),
        }
        for item in geometry.relation_scan_exclusions
    ]
    if audit.get("relation_selection_exclusions") != expected_exclusions:
        raise MapPlotterError(
            "Operator snapshot audit relation exclusions do not match geometry."
        )

    relation_ids_by_way: dict[int, set[int]] = defaultdict(set)
    for relation in geometry.relations:
        for member in relation.members:
            if (
                member.member_type == "way"
                and way_member_role_classification(member.role) == "alignment"
            ):
                relation_ids_by_way[member.ref].add(relation.relation_id)
    source_nodes = geometry.node_by_id
    ready_way_ids = {
        way.osm_way_id
        for way in geometry.ways
        if way.osm_way_id in relation_ids_by_way
        and len(way.node_refs) >= 2
        and all(node_id in source_nodes for node_id in way.node_refs)
    }
    raw_lineage = audit.get("way_relation_lineage")
    if not isinstance(raw_lineage, list):
        raise MapPlotterError("Operator snapshot audit way lineage is malformed.")
    lineage_by_way: dict[int, Mapping[str, Any]] = {}
    for raw in raw_lineage:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("way_id"), int):
            raise MapPlotterError("Operator snapshot audit way lineage is malformed.")
        way_id = int(raw["way_id"])
        if way_id in lineage_by_way:
            raise MapPlotterError(f"Operator snapshot audit repeats way {way_id}.")
        lineage_by_way[way_id] = raw
    if set(lineage_by_way) != ready_way_ids:
        raise MapPlotterError(
            "Operator snapshot audit retained-way lineage does not match geometry."
        )
    ways = geometry.way_by_id
    for way_id in sorted(ready_way_ids):
        way = ways[way_id]
        raw = lineage_by_way[way_id]
        if (
            raw.get("relation_ids") != sorted(relation_ids_by_way[way_id])
            or raw.get("node_refs") != list(way.node_refs)
            or raw.get("geometry_sha256") != _geometry_sha256(way, source_nodes)
            or raw.get("tags") != dict(way.tags)
            or raw.get("osm_version") != way.osm_version
            or raw.get("osm_timestamp") != way.osm_timestamp
        ):
            raise MapPlotterError(
                f"Operator snapshot audit way {way_id} lineage does not match geometry."
            )


def _overview_pen(pen_id: str) -> tuple[TransitPen, bool]:
    """Resolve one and only one owned 0.4 mm pass for an overview line."""

    selected_id = pen_id if pen_id in _OVERVIEW_PEN_BY_ID else _OVERVIEW_PEN_FALLBACK_ID
    try:
        physical = _OVERVIEW_PEN_BY_ID[selected_id]
    except KeyError as exc:  # pragma: no cover - the checked-in inventory owns grey.
        raise MapPlotterError(
            "The operator overview requires an owned grey-0-4 fallback pen."
        ) from exc
    substituted = selected_id != pen_id
    return (
        TransitPen(
            ink=physical.ink,
            nominal_nib_mm=physical.nominal_nib_mm,
            match_status="nominal-unmeasured",
            pen_id=physical.identity,
            calibration_state=physical.calibration_state,
            preview_hex=physical.preview_color,
        ),
        substituted,
    )


def _relations_by_operator(
    geometry: TargetedOperatorGeometry,
) -> dict[str, tuple[OsmTrainRelationRecord, ...]]:
    grouped: dict[str, list[OsmTrainRelationRecord]] = defaultdict(list)
    for relation in geometry.relations:
        grouped[relation.operator_code].append(relation)
    return {
        key: tuple(sorted(values, key=lambda item: item.relation_id))
        for key, values in grouped.items()
    }


def _grade_for_way(tags: Mapping[str, str]) -> str:
    if tags.get("bridge") not in {None, "", "no"}:
        return "bridge"
    if tags.get("tunnel") not in {None, "", "no"}:
        return "tunnel"
    return "unknown"


def _summary_count(summary: Mapping[str, Any], field: str) -> int:
    value = summary.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MapPlotterError(f"Operator audit field {field!r} is malformed.")
    return value


def compile_operator_overview_network(
    geometry: TargetedOperatorGeometry,
    audit: Mapping[str, Any],
    *,
    snapshot_date: str = DEFAULT_OVERVIEW_SNAPSHOT_DATE,
    retrieved_at: str = DEFAULT_OVERVIEW_RETRIEVED_AT,
    require_expected_coverage: bool = True,
) -> TransitNetwork:
    """Compile the registry-wide Great Britain OSM operator review proof.

    With ``require_expected_coverage`` enabled (the CLI default), the pinned
    2026 source must represent every registry product except the known blocked
    Hull Trains product.  This prevents a newly incomplete extraction from
    silently becoming the purported full overview.
    """

    audit_sha = str(audit.get("ordered_evidence_sha256", ""))
    if len(audit_sha) != 64 or any(
        character not in "0123456789abcdef" for character in audit_sha
    ):
        raise MapPlotterError("Operator snapshot audit has no valid evidence digest.")
    if _audit_evidence_sha256(audit) != audit_sha:
        raise MapPlotterError(
            "Operator snapshot audit evidence digest does not verify."
        )
    _verify_audit_geometry_binding(geometry, audit)

    raw_summaries = audit.get("operator_summary")
    if not isinstance(raw_summaries, Mapping):
        raise MapPlotterError("Operator snapshot audit has no operator summary.")
    expected_keys = {product.operator_key for product in OPERATOR_REGISTRY.products}
    if set(raw_summaries) != expected_keys:
        raise MapPlotterError(
            "Operator snapshot audit does not cover the exact versioned registry."
        )

    drawable = _drawable_way_ids(geometry)
    relations_by_key = _relations_by_operator(geometry)
    available_products = []
    unavailable_products = []
    selected_way_ids_by_key: dict[str, frozenset[int]] = {}
    for product in OPERATOR_REGISTRY.products:
        summary = raw_summaries[product.operator_key]
        if not isinstance(summary, Mapping):
            raise MapPlotterError(
                f"Operator summary for {product.operator_key} is malformed."
            )
        relation_ways = frozenset(
            member.ref
            for relation in relations_by_key.get(product.operator_key, ())
            for member in relation.members
            if member.member_type == "way"
            and way_member_role_classification(member.role) == "alignment"
            and member.ref in drawable
        )
        declared_way_count = _summary_count(
            summary, "unique_drawable_alignment_way_count"
        )
        if declared_way_count != len(relation_ways):
            raise MapPlotterError(
                f"Operator {product.operator_key} audit/geometry way counts disagree."
            )
        selected_way_ids_by_key[product.operator_key] = relation_ways
        if relation_ways:
            available_products.append(product)
        else:
            unavailable_products.append(product)

    unexpected_missing = sorted(
        product.id
        for product in unavailable_products
        if product.id not in EXPECTED_BLOCKED_PRODUCT_IDS
    )
    if require_expected_coverage and unexpected_missing:
        raise MapPlotterError(
            "Great Britain operator overview lost expected drawable products: "
            + ", ".join(unexpected_missing)
            + ". Re-audit the pinned source; the compiler will not silently shrink."
        )
    if not available_products:
        raise MapPlotterError("Operator overview has no drawable registry products.")

    osm_source_id = "osm-gb-passenger-operator-overview-2026-08-06"
    audit_source_id = "osm-gb-passenger-operator-overview-evidence-v1"
    registry_source_id = "gb-passenger-operator-registry-2026-08-08"
    sources = (
        TransitSource(
            id=osm_source_id,
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
            url="https://github.com/adambickerdike/city-map-plotter",
            licence="Derived ODbL evidence ledger; review proof only",
            attribution="Derived from the pinned OSM snapshot",
            retrieved_at=retrieved_at,
            sha256=audit_sha,
            use=(
                "Ordered relation/member lineage, unresolved geometry, selection "
                "exceptions, and per-product coverage audit for the overview."
            ),
            commercial_reuse_status="review-required",
        ),
        TransitSource(
            id=registry_source_id,
            publisher="City Map Plotter",
            url=(
                "https://github.com/adambickerdike/city-map-plotter/blob/main/"
                "src/city_map_plotter/data/gb-passenger-operators-2026-08-08.json"
            ),
            licence="Factual compilation with source-specific rights retained",
            attribution="City Map Plotter passenger-operator product registry",
            retrieved_at=retrieved_at,
            sha256=_registry_sha256(),
            use=(
                "Dated product roster, customer-facing names, house presentation "
                "references, current OSM tokens, and reviewed relation exceptions."
            ),
            commercial_reuse_status="review-required",
            valid_from=OPERATOR_REGISTRY.snapshot,
        ),
    )

    lines: list[TransitLine] = []
    line_id_by_key: dict[str, str] = {}
    substituted_pen_products: list[dict[str, Any]] = []
    for registry_order, product in enumerate(OPERATOR_REGISTRY.products):
        if product not in available_products:
            continue
        line_id = f"operator-{product.presentation.slug}"
        line_id_by_key[product.operator_key] = line_id
        pen, substituted = _overview_pen(product.presentation.pen_id)
        if substituted:
            substituted_pen_products.append(
                {
                    "product_id": product.id,
                    "requested_pen_id": product.presentation.pen_id,
                    "overview_pen_id": pen.pen_id,
                    "reason": (
                        "The registry presentation pen is not an owned 0.4 mm pen; "
                        "the overview forbids broad or multiple route passes."
                    ),
                }
            )
        lines.append(
            TransitLine(
                id=line_id,
                name=product.name,
                short_name="/".join(product.atoc_codes),
                order=registry_order,
                colour=ColourSpec(
                    name=f"{product.name} registry presentation reference",
                    display_hex=product.presentation.display_hex,
                    role="operator-network",
                    provenance="house-palette",
                    numeric_value_status="house-value",
                    source_ref=registry_source_id,
                ),
                pen=pen,
                service_class="osm-catalog-qualified-review-proof",
                source_ref=osm_source_id,
            )
        )

    if len(lines) != len(available_products) or len(line_id_by_key) != len(lines):
        raise MapPlotterError(
            "Operator overview failed its one-parent-line-per-product invariant."
        )

    ways = geometry.way_by_id
    source_nodes = geometry.node_by_id
    way_line_ids: dict[int, set[str]] = defaultdict(set)
    for product in available_products:
        line_id = line_id_by_key[product.operator_key]
        for way_id in selected_way_ids_by_key[product.operator_key]:
            way_line_ids[way_id].add(line_id)

    line_order = {line.id: line.order for line in lines}
    selected_node_ids = {
        node_id for way_id in way_line_ids for node_id in ways[way_id].node_refs
    }
    nodes = tuple(
        TransitNode(
            id=f"osm-node-{node_id}",
            kind="junction",
            lon=source_nodes[node_id].lon,
            lat=source_nodes[node_id].lat,
            source_ref=osm_source_id,
            source_object=f"node/{node_id}",
        )
        for node_id in sorted(selected_node_ids)
    )
    edges: list[TransitEdge] = []
    for way_id in sorted(way_line_ids):
        way = ways[way_id]
        edge_line_ids = tuple(
            sorted(way_line_ids[way_id], key=lambda value: (line_order[value], value))
        )
        grade = _grade_for_way(dict(way.tags))
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
                    line_ids=edge_line_ids,
                    source_ref=osm_source_id,
                    source_object=f"way/{way_id}",
                    status="osm-catalog-qualified-route-train-member-overview-proof",
                    grade=grade,
                )
            )

    patterns: list[ServicePattern] = []
    pattern_count_by_key: dict[str, int] = defaultdict(int)
    for product in available_products:
        key = product.operator_key
        line_id = line_id_by_key[key]
        for relation in relations_by_key.get(key, ()):
            plan = _plan_relation_runs(relation, geometry, drawable)
            relation_name = dict(relation.tags).get(
                "name", f"OSM RELATION {relation.relation_id}"
            )
            for part_index, run in enumerate(plan.runs, start=1):
                suffix = f" PART {part_index}" if len(plan.runs) > 1 else ""
                patterns.append(
                    ServicePattern(
                        id=(
                            f"{product.presentation.slug}-relation-"
                            f"{relation.relation_id}-part-{part_index}"
                        ),
                        line_id=line_id,
                        name=f"{relation_name}{suffix}",
                        traversals=tuple(
                            EdgeTraversal(
                                edge_id=edge_id,
                                direction=occurrence.direction,
                            )
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
                pattern_count_by_key[key] += 1

    missing_patterns = [
        product.id
        for product in available_products
        if pattern_count_by_key[product.operator_key] == 0
    ]
    if missing_patterns:
        raise MapPlotterError(
            "Drawable products produced no continuous relation run: "
            + ", ".join(missing_patterns)
            + "."
        )

    omissions: list[dict[str, Any]] = [
        {
            "kind": "official-service-completeness",
            "status": "not-claimed",
            "reason": (
                "The plate is a catalog-qualified OSM route=train relation snapshot, "
                "not an official timetable, operator route map, or complete service claim."
            ),
        },
        {
            "kind": "registry-product-coverage",
            "status": "quantified",
            "reason": (
                "Every product in the dated registry is classified as represented "
                "or unrepresented from drawable selected relation geometry."
            ),
            "registry_snapshot": OPERATOR_REGISTRY.snapshot,
            "registry_product_count": len(OPERATOR_REGISTRY.products),
            "represented_product_count": len(available_products),
            "unrepresented_product_count": len(unavailable_products),
            "represented_product_ids": [product.id for product in available_products],
            "unrepresented_product_ids": [
                product.id for product in unavailable_products
            ],
        },
        {
            "kind": "great-britain-geographic-scope",
            "status": "northern-ireland-out-of-scope",
            "reason": (
                "The pinned source and registry cover Great Britain. This is not a "
                "United Kingdom-wide/Northern Ireland Railways service map."
            ),
        },
        {
            "kind": "station-labels",
            "status": "omitted",
            "reason": (
                "OSM relation stop members are not converted into station-to-track "
                "bindings without a separate reviewed station source."
            ),
        },
        {
            "kind": "geographic-context",
            "status": "required-separate-pinned-attachment",
            "reason": (
                "Coastline, water, boundaries, and strategic-road orientation are "
                "attached by the scale-aware transit context compiler; generic "
                "physical railway context is not inferred as operator service."
            ),
        },
    ]
    for product in unavailable_products:
        status = (
            "blocked-no-usable-osm-relation"
            if product.id in EXPECTED_BLOCKED_PRODUCT_IDS
            else "not-represented-no-drawable-geometry"
        )
        omissions.append(
            {
                "kind": "operator-product",
                "product_id": product.id,
                "operator_key": product.operator_key,
                "name": product.name,
                "status": status,
                "reason": (
                    "No catalog-qualified selected route=train relation supplied "
                    "drawable operational member-way geometry in the pinned source."
                ),
            }
        )

    eurostar = OPERATOR_REGISTRY.by_id[EUROSTAR_PRODUCT_ID]
    if eurostar in available_products:
        summary = raw_summaries[eurostar.operator_key]
        assert isinstance(summary, Mapping)
        omissions.append(
            {
                "kind": "operator-product",
                "product_id": eurostar.id,
                "operator_key": eurostar.operator_key,
                "name": eurostar.name,
                "status": "partial-great-britain-section-only",
                "missing_alignment_way_occurrence_count": _summary_count(
                    summary, "missing_alignment_way_occurrence_count"
                ),
                "reason": (
                    "A Great Britain PBF cannot contain Eurostar's continental "
                    "member-way geometry; only resolved British/HS1 segments are drawn."
                ),
            }
        )

    heathrow = OPERATOR_REGISTRY.by_id[HEATHROW_EXPRESS_PRODUCT_ID]
    if heathrow in available_products:
        reviewed_ids = sorted(
            relation.relation_id
            for relation in relations_by_key.get(heathrow.operator_key, ())
            if relation.selection_method == "catalog-reviewed-relation-id"
        )
        if reviewed_ids:
            omissions.append(
                {
                    "kind": "relation-selection-exception",
                    "product_id": heathrow.id,
                    "status": "catalog-reviewed-relation-id",
                    "relation_ids": reviewed_ids,
                    "reason": (
                        "These route=train relations are selected only by the "
                        "versioned reviewed relation-ID allowlist, not a current "
                        "explicit operator tag."
                    ),
                }
            )

    omissions.extend(
        {
            "kind": "physical-pen-substitution",
            "status": "explicit-owned-0-4-mm-substitution",
            **item,
        }
        for item in substituted_pen_products
    )
    physical_groups: dict[str, list[str]] = defaultdict(list)
    for line in lines:
        assert line.pen.pen_id is not None
        physical_groups[line.pen.pen_id].append(line.id)
    collisions = {
        pen_id: line_ids
        for pen_id, line_ids in sorted(physical_groups.items())
        if len(line_ids) > 1
    }
    if collisions:
        omissions.append(
            {
                "kind": "physical-ink-distinction",
                "status": "legend-required-shared-owned-pens",
                "pen_line_groups": collisions,
                "reason": (
                    "The owned six-colour 0.4 mm inventory cannot physically encode "
                    "a unique ink for every represented operator; reference screen "
                    "colours therefore collapse to disclosed physical pen groups."
                ),
            }
        )

    network = TransitNetwork(
        id="great-britain-passenger-operator-overview-2026-08-06",
        name="GREAT BRITAIN PASSENGER OPERATORS",
        kind="national-operator-overview",
        scope=(
            f"GREAT BRITAIN / {len(available_products)} OF "
            f"{len(OPERATOR_REGISTRY.products)} REGISTRY PRODUCTS REPRESENTED / "
            "NOT OFFICIAL COMPLETE COVERAGE"
        ),
        format_id="a3-landscape",
        snapshot=snapshot_date,
        validity_status="candidate-not-reviewed",
        geometry_mode=(
            "shared-atomic-exact-osm-route-train-member-consecutive-node-"
            "segments-no-joins"
        ),
        sources=sources,
        lines=tuple(lines),
        nodes=nodes,
        edges=tuple(edges),
        service_patterns=tuple(patterns),
        context=(),
        omissions=tuple(omissions),
        notes=(
            "REVIEW PROOF ONLY — not an official operator or timetable map.",
            "One parent line is emitted for each represented registry product; child brands and individual services are consolidated.",
            "Every physical OSM way segment is stored once and carries the union of registry-product line IDs that reference it.",
            "No route masters are expanded, no nearby track is searched, and no proximity joins or invented connectors are used.",
            "The overview render policy requests exactly one owned 0.4 mm pass per represented product line.",
            "A scale-aware pinned context must be attached before a customer-map render; generic physical rail is never treated as operator evidence.",
            f"Operator evidence ledger SHA-256: {audit_sha}.",
            f"Compiler policy: {OPERATOR_OVERVIEW_POLICY_VERSION}.",
        ),
        contract_sha256="",
    )
    validate_transit_network(network)
    return network


__all__ = [
    "DEFAULT_OVERVIEW_RETRIEVED_AT",
    "DEFAULT_OVERVIEW_SNAPSHOT_DATE",
    "EXPECTED_BLOCKED_PRODUCT_IDS",
    "OPERATOR_OVERVIEW_POLICY_VERSION",
    "compile_operator_overview_network",
]
