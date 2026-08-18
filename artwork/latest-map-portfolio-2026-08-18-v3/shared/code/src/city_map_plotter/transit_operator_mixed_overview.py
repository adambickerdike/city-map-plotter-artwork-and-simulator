"""Compile the disclosed 25-product Great Britain mixed-evidence overview.

The first 24 lines are retained byte-for-byte at the model-record level from
the reviewed OSM operator-relation overview.  Hull Trains is admitted only from
the separately sealed scale-aware *cartographic* corridor.  The latter is not
an operational track, platform, or WTT-transition claim.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import hashlib
from importlib import resources
import json
from math import isclose, isfinite
import re
from typing import Any, NoReturn

from .models import MapPlotterError
from .pens import ACTUAL_PEN_INVENTORY
from .transit import (
    EdgeTraversal,
    ServicePattern,
    TransitEdge,
    TransitNetwork,
    TransitSource,
    validate_transit_network,
)
from .transit_operator_cartographic_corridor import (
    CARTOGRAPHIC_SELECTION_COMPONENTS,
    CARTOGRAPHIC_SELECTION_RULE,
    EQUIVALENCE_COMPARISON_POLICY_VERSION,
    HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION,
    MAXIMUM_NIB_FRACTION,
    OWNED_ROUTE_NIB_MM,
    RENDERER_GEOMETRY_TOLERANCE_MM,
)
from .transit_operator_overview import OPERATOR_OVERVIEW_POLICY_VERSION
from .transit_operator_registry import OPERATOR_REGISTRY, REGISTRY_RESOURCE


MIXED_OVERVIEW_POLICY_VERSION = "gb-passenger-operator-mixed-evidence-overview-v3"
MIXED_OVERVIEW_ATTRIBUTION_POLICY_VERSION = (
    "gb-passenger-operator-mixed-overview-visible-attribution-v1"
)
MIXED_OVERVIEW_INTERNAL_ATTRIBUTION = "City Map Plotter evidence"
MIXED_OVERVIEW_OSM_DATABASE_ATTRIBUTION = (
    "© OpenStreetMap contributors / https://www.openstreetmap.org/copyright / "
    "DATABASE SOURCE: https://download.geofabrik.de/europe/great-britain.html / "
    "ODbL 1.0: https://opendatacommons.org/licenses/odbl/1-0/"
)
BASE_OVERVIEW_ID = "great-britain-passenger-operator-overview-2026-08-06"
HULL_CORRIDOR_ID = "hull-trains-scale-aware-cartographic-corridor-2026-08-08"
HULL_LINE_ID = "hull-trains-scale-aware-cartographic-corridor"
HULL_PRODUCT_ID = "hull-trains-2026"
HULL_OPERATOR_CODE = "HT"
BASE_OSM_SOURCE_ID = "osm-gb-passenger-operator-overview-2026-08-06"
BASE_EVIDENCE_SOURCE_ID = "osm-gb-passenger-operator-overview-evidence-v1"
REGISTRY_SOURCE_ID = "gb-passenger-operator-registry-2026-08-08"
HULL_PBF_SOURCE_ID = "osm-gb-physical-rail-cartographic-corridor-2026-08-06"
HULL_NAPTAN_SOURCE_ID = "naptan-stations-2026-08-07"
HULL_COMPARISON_SOURCE_ID = "hull-trains-exhaustive-equivalence-comparison-2026-08-08"
HULL_LEGACY_COMPARISON_SOURCE_ID = (
    "hull-trains-legacy-pair-ambiguity-comparison-2026-08-08"
)
MAXIMUM_EXHAUSTIVE_EQUIVALENCE_CLASS_CANDIDATES = 256
EXHAUSTIVE_EQUIVALENCE_CLASS_ENUMERATION_LIMIT = 257
# GEOS/Shapely can return a tiny non-zero Hausdorff residual when a projected
# line is compared with itself.  One micrometre on the ground is many orders of
# magnitude below the sealed paper-space gate, while still rejecting a changed
# reference geometry.
SELF_SEPARATION_NUMERIC_TOLERANCE_M = 1e-6

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_HULL_STATIONS = (
    ("LONDON KINGS CROSS", "London King's Cross", "9100KNGX"),
    ("GRANTHAM", "Grantham", "9100GTHM"),
    ("RETFORD", "Retford", "9100RTFD"),
    ("DONCASTER", "Doncaster", "9100DONC"),
    ("SELBY", "Selby", "9100SELBY"),
    ("HOWDEN", "Howden", "9100HOWDEN"),
    ("BROUGH", "Brough", "9100BROUGH"),
    ("HULL", "Hull", "9100HULL"),
    ("COTTINGHAM", "Cottingham", "9100CTTGHM"),
    ("BEVERLEY", "Beverley", "9100BEVERLY"),
)
_AMBIGUOUS_LEGS = {
    0: "LONDON KINGS CROSS–GRANTHAM",
    3: "DONCASTER–SELBY",
}
_HULL_SOURCE_AUDIT_BINDINGS = {
    "hull-trains-customer-timetable-may-december-2026": (
        "official_customer_timetable_sha256"
    ),
    HULL_NAPTAN_SOURCE_ID: "naptan_sha256",
    HULL_PBF_SOURCE_ID: "osm_pbf_sha256",
    "hull-trains-advertised-customer-scope-2026-08-08": "scope_file_sha256",
    "hull-trains-unreviewed-wtt-candidate-2026-08-08": "candidate_file_sha256",
    REGISTRY_SOURCE_ID: "registry_sha256",
    "hull-trains-all-nine-corridor-diagnostic-2026-08-08": (
        "all_nine_diagnostic_file_sha256"
    ),
    HULL_LEGACY_COMPARISON_SOURCE_ID: "legacy_pair_comparison_file_sha256",
    HULL_COMPARISON_SOURCE_ID: "equivalence_comparison_file_sha256",
    "hull-trains-exact-corridor-blocker-2026-08-08": (
        "exact_corridor_blocker_file_sha256"
    ),
}
_EXCLUDED_CORRIDOR_SERVICE_VALUES = ("siding", "spur", "yard")
_COMMON_INTERSECTION_INTERPRETATION = (
    "Exact edge-ID intersection shared by every fully enumerated equal-"
    "shortest candidate; service/crossover tags here are unavoidable "
    "within this cartographic equivalence class, not operator-use proof."
)
_FULL_PATH_TAG_RULE = (
    "full-path tag inventory; zero ineligible and zero yard/siding/spur are "
    "absolute, while nonempty service/crossover tags are classified against "
    "the exact class-common intersection before selection"
)


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(message)


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object.")
    return value


def _sequence(value: Any, *, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _fail(f"{field} must be a list.")
    return value


def _digest(value: str, *, field: str) -> str:
    if _HEX_SHA256.fullmatch(value) is None:
        _fail(f"{field} must be an exact lower-case SHA-256 digest.")
    return value


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{field} must be a finite number.")
    result = float(value)
    if not isfinite(result):
        _fail(f"{field} must be a finite number.")
    return result


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _document_sha256(document: Mapping[str, Any]) -> str:
    payload = dict(document)
    payload.pop("ordered_evidence_sha256", None)
    return _canonical_sha256(payload)


def _sorted_unique_strings(value: Any, *, field: str) -> tuple[str, ...]:
    raw = _sequence(value, field=field)
    if any(not isinstance(item, str) or not item for item in raw):
        _fail(f"{field} must contain non-empty strings.")
    result = tuple(raw)
    if len(set(result)) != len(result) or result != tuple(sorted(result)):
        _fail(f"{field} must be sorted and contain no duplicate IDs.")
    return result


def _service_value_counts(value: Any, *, field: str) -> dict[str, int]:
    raw = _mapping(value, field=field)
    result: dict[str, int] = {}
    for key, count in raw.items():
        if not isinstance(key, str) or not key or key == "none":
            _fail(f"{field} contains an invalid service value.")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            _fail(f"{field} contains an invalid edge count.")
        result[key] = count
    if list(raw) != sorted(raw):
        _fail(f"{field} keys must be sorted.")
    return result


def _aggregate_service_value_counts(
    value: Any,
    *,
    field: str,
    expected_edge_count: int,
) -> dict[str, int]:
    raw = _mapping(value, field=field)
    result: dict[str, int] = {}
    for key, count in raw.items():
        if not isinstance(key, str) or not key:
            _fail(f"{field} contains an invalid service value.")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            _fail(f"{field} contains an invalid edge count.")
        result[key] = count
    if not (
        list(raw) == sorted(raw)
        and sum(result.values()) == expected_edge_count
        and not set(result).intersection(_EXCLUDED_CORRIDOR_SERVICE_VALUES)
    ):
        _fail(f"{field} is incomplete, unsorted, or contains an excluded edge.")
    return result


def _validate_tag_audit(
    value: Any,
    *,
    field: str,
    edge_scope: tuple[str, ...],
    full_path: bool,
) -> dict[str, Any]:
    audit = _mapping(value, field=field)
    common_keys = {
        "ineligible_edge_ids",
        "ineligible_edge_count",
        "excluded_service_values",
        "excluded_service_value_edge_ids",
        "excluded_service_value_edge_count",
        "service_tagged_edge_ids",
        "service_tagged_edge_count",
        "service_value_edge_counts",
        "crossover_edge_ids",
        "crossover_edge_count",
    }
    full_path_keys = {"rule", "eligible_and_excluded_edge_gate_passed"}
    if set(audit) != common_keys | (full_path_keys if full_path else set()):
        _fail(f"{field} field scope changed.")

    ineligible = _sorted_unique_strings(
        audit.get("ineligible_edge_ids"),
        field=f"{field}.ineligible_edge_ids",
    )
    excluded = _sorted_unique_strings(
        audit.get("excluded_service_value_edge_ids"),
        field=f"{field}.excluded_service_value_edge_ids",
    )
    service = _sorted_unique_strings(
        audit.get("service_tagged_edge_ids"),
        field=f"{field}.service_tagged_edge_ids",
    )
    crossover = _sorted_unique_strings(
        audit.get("crossover_edge_ids"),
        field=f"{field}.crossover_edge_ids",
    )
    excluded_values = _sequence(
        audit.get("excluded_service_values"),
        field=f"{field}.excluded_service_values",
    )
    counts = _service_value_counts(
        audit.get("service_value_edge_counts"),
        field=f"{field}.service_value_edge_counts",
    )
    scope = set(edge_scope)
    if not (
        excluded_values == list(_EXCLUDED_CORRIDOR_SERVICE_VALUES)
        and set(ineligible).issubset(scope)
        and set(excluded).issubset(scope)
        and set(service).issubset(scope)
        and set(crossover).issubset(service)
        and set(excluded).issubset(service)
        and audit.get("ineligible_edge_count") == len(ineligible)
        and audit.get("excluded_service_value_edge_count") == len(excluded)
        and audit.get("service_tagged_edge_count") == len(service)
        and audit.get("crossover_edge_count") == len(crossover)
        and sum(counts.values()) == len(service)
        and counts.get("crossover", 0) == len(crossover)
        and sum(
            counts.get(service_value, 0)
            for service_value in _EXCLUDED_CORRIDOR_SERVICE_VALUES
        )
        == len(excluded)
    ):
        _fail(f"{field} edge IDs, service values, or counts are inconsistent.")

    if full_path:
        if not (
            audit.get("rule") == _FULL_PATH_TAG_RULE
            and audit.get("eligible_and_excluded_edge_gate_passed")
            is (not ineligible and not excluded)
        ):
            _fail(f"{field} full-path diagnostic is inconsistent.")
    return {
        "ineligible": ineligible,
        "excluded": excluded,
        "service": service,
        "crossover": crossover,
        "service_counts": counts,
    }


def _validate_separation_metrics(
    value: Any,
    *,
    field: str,
    renderer_metres_per_mm: float,
) -> tuple[float, float, float]:
    metrics = _mapping(value, field=field)
    if set(metrics) != {
        "densified_hausdorff_max_m",
        "renderer_paper_max_mm",
        "owned_0_4_mm_nib_fraction",
        "below_renderer_geometry_tolerance",
        "below_maximum_nib_fraction",
    }:
        _fail(f"{field} field scope changed.")
    metres = _number(
        metrics.get("densified_hausdorff_max_m"),
        field=f"{field}.densified_hausdorff_max_m",
    )
    paper_mm = _number(
        metrics.get("renderer_paper_max_mm"),
        field=f"{field}.renderer_paper_max_mm",
    )
    nib_fraction = _number(
        metrics.get("owned_0_4_mm_nib_fraction"),
        field=f"{field}.owned_0_4_mm_nib_fraction",
    )
    if not (
        metres >= 0.0
        and paper_mm >= 0.0
        and nib_fraction >= 0.0
        and isclose(
            paper_mm,
            metres / renderer_metres_per_mm,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        and isclose(
            nib_fraction,
            paper_mm / OWNED_ROUTE_NIB_MM,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        and metrics.get("below_renderer_geometry_tolerance")
        is (paper_mm <= RENDERER_GEOMETRY_TOLERANCE_MM)
        and metrics.get("below_maximum_nib_fraction")
        is (nib_fraction <= MAXIMUM_NIB_FRACTION)
    ):
        _fail(f"{field} is inconsistent with the sealed A3 paper-space gate.")
    return metres, paper_mm, nib_fraction


def _registry_sha256() -> str:
    resource = resources.files("city_map_plotter").joinpath(REGISTRY_RESOURCE)
    try:
        return hashlib.sha256(resource.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - packaged-resource invariant.
        raise MapPlotterError(
            f"Cannot hash passenger-operator registry {REGISTRY_RESOURCE}: {exc}"
        ) from exc


@dataclass(frozen=True, slots=True)
class MixedOverviewInputHashes:
    """All immutable inputs required by the mixed-evidence merge."""

    base_contract_sha256: str
    base_osm_audit_file_sha256: str
    base_osm_evidence_sha256: str
    hull_contract_sha256: str
    hull_audit_file_sha256: str
    hull_audit_evidence_sha256: str

    def validated(self) -> "MixedOverviewInputHashes":
        for field in (
            "base_contract_sha256",
            "base_osm_audit_file_sha256",
            "base_osm_evidence_sha256",
            "hull_contract_sha256",
            "hull_audit_file_sha256",
            "hull_audit_evidence_sha256",
        ):
            _digest(str(getattr(self, field)), field=field)
        return self


@dataclass(frozen=True, slots=True)
class _HullAmbiguityReference:
    path_sha256: str
    graph_edge_ids: tuple[str, ...]
    service_value_edge_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class _HullPatternEdgeLedger:
    core_traversal_count: int
    core_ordered_traversal_sha256: str
    extension_traversal_count: int
    extension_ordered_traversal_sha256: str
    traversal_occurrence_count: int
    unique_physical_edge_count: int
    physical_edge_union_sha256: str
    cross_pattern_shared_physical_edge_count: int
    cross_pattern_shared_physical_edge_ids_sha256: str


def _ordered_traversal_sha256(traversals: Sequence[EdgeTraversal]) -> str:
    return _canonical_sha256(
        [
            {"edge_id": traversal.edge_id, "direction": traversal.direction}
            for traversal in traversals
        ]
    )


def _graph_path_sha256_from_contract_traversals(
    traversals: Sequence[EdgeTraversal],
    *,
    edge_by_id: Mapping[str, TransitEdge],
    field: str,
) -> tuple[str, tuple[str, ...]]:
    graph_edge_ids: list[str] = []
    graph_node_ids: list[int] = []
    for index, traversal in enumerate(traversals):
        edge = edge_by_id.get(traversal.edge_id)
        if edge is None:
            _fail(f"{field} references an absent physical edge.")
        edge_id_match = re.fullmatch(
            r"cartographic-osm-way-(\d+)-segment-(\d+)", edge.id
        )
        if edge_id_match is None:
            _fail(f"{field} contains a non-OSM cartographic edge ID.")
        first_node_id, second_node_id = (
            (edge.from_node, edge.to_node)
            if traversal.direction == "forward"
            else (edge.to_node, edge.from_node)
        )
        prefix = "cartographic-osm-node-"
        if not (first_node_id.startswith(prefix) and second_node_id.startswith(prefix)):
            _fail(f"{field} contains a connector or non-OSM route node.")
        try:
            first_graph_node_id = int(first_node_id.removeprefix(prefix))
            second_graph_node_id = int(second_node_id.removeprefix(prefix))
        except ValueError:
            _fail(f"{field} contains a malformed OSM route-node ID.")
        if index == 0:
            graph_node_ids.append(first_graph_node_id)
        elif graph_node_ids[-1] != first_graph_node_id:
            _fail(f"{field} is not one exact connected graph path.")
        graph_node_ids.append(second_graph_node_id)
        graph_edge_ids.append(
            f"osm-way/{edge_id_match.group(1)}/segment/{edge_id_match.group(2)}"
        )
    if not graph_edge_ids:
        _fail(f"{field} has no physical edge traversal.")
    digest = _canonical_sha256({"edge_ids": graph_edge_ids, "node_ids": graph_node_ids})
    return digest, tuple(graph_edge_ids)


def _owned_overview_pen(line_id: str, pen_id: str | None, nib_mm: float) -> None:
    if pen_id is None or abs(nib_mm - 0.4) > 1e-9:
        _fail(f"Overview line {line_id!r} is not mapped to one 0.4 mm pen.")
    physical = next(
        (pen for pen in ACTUAL_PEN_INVENTORY.pens if pen.identity == pen_id),
        None,
    )
    if physical is None or abs(physical.nominal_nib_mm - 0.4) > 1e-9:
        _fail(f"Overview line {line_id!r} names an unowned 0.4 mm pen.")


def _validate_base_overview(
    base: TransitNetwork,
    hashes: MixedOverviewInputHashes,
) -> None:
    if base.contract_sha256 != hashes.base_contract_sha256:
        _fail("The 24-line base contract hash does not match the sealed input.")
    if (
        base.id != BASE_OVERVIEW_ID
        or base.kind != "national-operator-overview"
        or base.format_id != "a3-landscape"
        or base.snapshot != "2026-08-06"
        or base.validity_status != "candidate-not-reviewed"
        or base.geometry_mode
        != "shared-atomic-exact-osm-route-train-member-consecutive-node-segments-no-joins"
        or base.context
    ):
        _fail("Mixed overview requires the exact route-only reviewed 24-line base.")

    expected_products = tuple(
        product
        for product in OPERATOR_REGISTRY.products
        if product.id != HULL_PRODUCT_ID
    )
    expected_by_line_id = {
        f"operator-{product.presentation.slug}": (index, product)
        for index, product in enumerate(OPERATOR_REGISTRY.products)
        if product.id != HULL_PRODUCT_ID
    }
    if len(base.lines) != 24 or set(base.line_by_id) != set(expected_by_line_id):
        _fail("The base no longer represents the exact 24 non-Hull products.")
    if len(expected_products) != 24:
        _fail("The dated registry no longer contains exactly 25 products.")
    for line in base.lines:
        order, product = expected_by_line_id[line.id]
        if (
            line.name != product.name
            or line.short_name != "/".join(product.atoc_codes)
            or line.order != order
            or line.source_ref != BASE_OSM_SOURCE_ID
            or line.colour.source_ref != REGISTRY_SOURCE_ID
            or line.service_class != "osm-catalog-qualified-review-proof"
        ):
            _fail(f"Base product line {line.id!r} changed identity or evidence.")
        _owned_overview_pen(line.id, line.pen.pen_id, line.pen.nominal_nib_mm)

    sources = base.source_by_id
    if set(sources) != {
        BASE_OSM_SOURCE_ID,
        BASE_EVIDENCE_SOURCE_ID,
        REGISTRY_SOURCE_ID,
    }:
        _fail("The route-only base source ledger changed.")
    if sources[BASE_EVIDENCE_SOURCE_ID].sha256 != hashes.base_osm_evidence_sha256:
        _fail("The base OSM evidence digest does not match the sealed input.")
    if sources[REGISTRY_SOURCE_ID].sha256 != _registry_sha256():
        _fail("The base is not bound to the current dated product registry.")
    if not any(
        note == f"Compiler policy: {OPERATOR_OVERVIEW_POLICY_VERSION}."
        for note in base.notes
    ):
        _fail("The base overview compiler policy ID is absent or changed.")

    if any(node.is_station for node in base.nodes):
        _fail("The 24-line relation overview unexpectedly contains station claims.")
    if any(
        edge.source_ref != BASE_OSM_SOURCE_ID
        or edge.status != "osm-catalog-qualified-route-train-member-overview-proof"
        for edge in base.edges
    ):
        _fail("The base contains non-OSM or changed route-edge evidence.")
    if any(pattern.station_ids for pattern in base.service_patterns):
        _fail("The base relation proof unexpectedly binds station markers.")

    blocked = [
        item
        for item in base.omissions
        if item.get("product_id") == HULL_PRODUCT_ID
        and item.get("status") == "blocked-no-usable-osm-relation"
    ]
    if len(blocked) != 1 or not (
        blocked[0].get("kind") == "operator-product"
        and blocked[0].get("operator_key") == HULL_OPERATOR_CODE
        and blocked[0].get("name") == "Hull Trains"
    ):
        _fail("The original Hull OSM relation omission is absent or changed.")
    coverage = [
        item
        for item in base.omissions
        if item.get("kind") == "registry-product-coverage"
    ]
    expected_ids = [product.id for product in expected_products]
    if len(coverage) != 1 or not (
        coverage[0].get("registry_snapshot") == OPERATOR_REGISTRY.snapshot
        and coverage[0].get("registry_product_count") == 25
        and coverage[0].get("represented_product_count") == 24
        and coverage[0].get("unrepresented_product_count") == 1
        and coverage[0].get("represented_product_ids") == expected_ids
        and coverage[0].get("unrepresented_product_ids") == [HULL_PRODUCT_ID]
    ):
        _fail("The base 24-of-25 registry coverage ledger changed.")


def _validate_hull_sources(
    corridor: TransitNetwork,
    audit_sources: Mapping[str, Any],
) -> None:
    expected_audit_keys = {
        *_HULL_SOURCE_AUDIT_BINDINGS.values(),
        "rail_graph_sha256",
        "candidate_evidence_sha256",
    }
    if set(audit_sources) != expected_audit_keys:
        _fail("Hull standalone audit source-hash scope changed.")
    for key in ("rail_graph_sha256", "candidate_evidence_sha256"):
        value = audit_sources.get(key)
        if not isinstance(value, str):
            _fail(f"Hull audit {key} is malformed.")
        _digest(value, field=f"Hull audit {key}")
    source_by_id = corridor.source_by_id
    if set(source_by_id) != set(_HULL_SOURCE_AUDIT_BINDINGS):
        _fail("Hull standalone contract source scope changed.")
    for source_id, audit_key in _HULL_SOURCE_AUDIT_BINDINGS.items():
        digest = audit_sources.get(audit_key)
        if not isinstance(digest, str) or source_by_id[source_id].sha256 != digest:
            _fail(f"Hull source {source_id!r} is not bound to its audit hash.")
    if source_by_id[REGISTRY_SOURCE_ID].sha256 != _registry_sha256():
        _fail("Hull standalone contract uses a different product registry.")


def _validate_hull_station_scope(
    corridor: TransitNetwork,
    audit: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    raw_stations = _sequence(audit.get("stations"), field="Hull audit stations")
    if len(raw_stations) != len(_HULL_STATIONS):
        _fail("Hull audit must bind exactly ten advertised customer stations.")
    contract_stations = [node for node in corridor.nodes if node.is_station]
    if len(contract_stations) != len(_HULL_STATIONS):
        _fail("Hull contract station scope changed from the ten advertised points.")
    station_by_code = {node.source_object: node for node in contract_stations}
    ordered_station_ids: list[str] = []
    ordered_anchor_ids: list[int] = []
    for order, ((location, display_name, atco), raw) in enumerate(
        zip(_HULL_STATIONS, raw_stations, strict=True)
    ):
        station = _mapping(raw, field=f"Hull audit stations[{order}]")
        if set(station) != {
            "order",
            "location",
            "display_name",
            "naptan_atco_code",
            "station_lon",
            "station_lat",
            "selected_osm_node_id",
            "selected_node_lon",
            "selected_node_lat",
            "distance_m",
            "candidate_rank",
            "selection_method",
        }:
            _fail(f"Hull advertised station field scope changed at order {order}.")
        if not (
            station.get("order") == order
            and station.get("location") == location
            and station.get("display_name") == display_name
            and station.get("naptan_atco_code") == atco
            and station.get("candidate_rank") == 1
            and station.get("selection_method")
            == "mechanical-nearest-exact-graph-node-not-human-reviewed-operator-platform-binding"
        ):
            _fail(f"Hull advertised station scope changed at order {order}.")
        distance_m = _number(
            station.get("distance_m"), field=f"Hull {location} distance_m"
        )
        lon = _number(station.get("station_lon"), field=f"Hull {location} station_lon")
        lat = _number(station.get("station_lat"), field=f"Hull {location} station_lat")
        if distance_m < 0.0 or distance_m > 100.0:
            _fail(f"Hull advertised station {location} exceeds the anchor gate.")
        anchor_id = station.get("selected_osm_node_id")
        if not isinstance(anchor_id, int) or isinstance(anchor_id, bool):
            _fail(f"Hull advertised station {location} anchor ID is malformed.")
        anchor_lon = _number(
            station.get("selected_node_lon"),
            field=f"Hull {location} selected_node_lon",
        )
        anchor_lat = _number(
            station.get("selected_node_lat"),
            field=f"Hull {location} selected_node_lat",
        )
        anchor_node = corridor.node_by_id.get(f"cartographic-osm-node-{anchor_id}")
        if anchor_node is None or not (
            anchor_node.source_ref == HULL_PBF_SOURCE_ID
            and anchor_node.source_object == f"node/{anchor_id}"
            and abs(anchor_node.lon - anchor_lon) <= 1e-11
            and abs(anchor_node.lat - anchor_lat) <= 1e-11
        ):
            _fail(f"Hull advertised station {location} graph anchor changed.")
        node = station_by_code.get(atco)
        expected_kind = "terminal" if order in {0, 9} else "station"
        expected_tier = "terminal" if order in {0, 9} else "major"
        if node is None or not (
            node.name == display_name
            and node.source_ref == HULL_NAPTAN_SOURCE_ID
            and node.kind == expected_kind
            and node.station_tier == expected_tier
            and abs(node.lon - lon) <= 1e-11
            and abs(node.lat - lat) <= 1e-11
        ):
            _fail(f"Hull contract/audit station binding changed for {location}.")
        ordered_station_ids.append(node.id)
        ordered_anchor_ids.append(anchor_id)
    if set(station_by_code) != {item[2] for item in _HULL_STATIONS}:
        _fail("Hull contract contains an unexpected advertised station marker.")
    return tuple(ordered_station_ids), tuple(ordered_anchor_ids)


def _validate_ambiguity_gate(
    audit: Mapping[str, Any],
    *,
    station_anchor_ids: tuple[int, ...],
) -> dict[int, _HullAmbiguityReference]:
    gate = _mapping(audit.get("scale_gate"), field="Hull audit scale_gate")
    gate_renderer_metres_per_mm = _number(
        gate.get("renderer_metres_per_mm"),
        field="Hull audit scale_gate.renderer_metres_per_mm",
    )
    if not (
        gate.get("format_id") == "a3-landscape"
        and gate_renderer_metres_per_mm > 0.0
        and gate.get("renderer_geometry_tolerance_mm") == RENDERER_GEOMETRY_TOLERANCE_MM
        and gate.get("owned_route_pen_nib_mm") == OWNED_ROUTE_NIB_MM
        and gate.get("maximum_nib_fraction") == MAXIMUM_NIB_FRACTION
        and gate.get("maximum_allowed_paper_separation_mm")
        == min(
            RENDERER_GEOMETRY_TOLERANCE_MM,
            OWNED_ROUTE_NIB_MM * MAXIMUM_NIB_FRACTION,
        )
        and gate.get("policy_max_equivalence_candidate_count")
        == MAXIMUM_EXHAUSTIVE_EQUIVALENCE_CLASS_CANDIDATES
    ):
        _fail("Hull overview received a changed A3 physical scale gate.")
    collapses = _sequence(
        audit.get("ambiguity_collapses"), field="Hull ambiguity_collapses"
    )
    if len(collapses) != 2:
        _fail("Hull scale-aware proof must contain exactly two ambiguity collapses.")
    seen_orders: set[int] = set()
    reference_by_order: dict[int, _HullAmbiguityReference] = {}
    for index, raw in enumerate(collapses):
        item = _mapping(raw, field=f"Hull ambiguity_collapses[{index}]")
        order = item.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            _fail("Hull ambiguity order is malformed.")
        if set(item) != {
            "order",
            "leg",
            "start_osm_node_id",
            "end_osm_node_id",
            "station_great_circle_distance_m",
            "legacy_witness_path_sha256",
            "equivalence_class",
            "class_common_edge_intersection",
            "candidate_path_sha256",
            "clean_zero_addition_candidate_path_sha256",
            "no_zero_addition_candidate_available",
            "cartographic_reference",
            "candidates",
            "renderer_metres_per_mm",
            "renderer_geometry_tolerance_mm",
            "owned_route_pen_nib_mm",
            "nib_fraction_tolerance",
            "pairwise_maximum_separation",
            "all_candidates_below_reference_geometry_tolerance",
            "all_candidates_below_reference_nib_fraction",
            "all_candidate_pairs_below_geometry_tolerance",
            "all_candidate_pairs_below_nib_fraction",
            "all_full_paths_zero_ineligible_or_excluded_edges",
            "all_candidate_specific_tag_counts_disclosed",
            "minimum_tuple_cartographic_reference_selected",
            "zero_addition_candidate_required",
            "selected_nonzero_candidate_specific_count_disclosed",
        }:
            _fail("Hull ambiguity field scope changed.")
        candidates = _sequence(
            item.get("candidates"), field=f"Hull ambiguity {order} candidates"
        )
        equivalence = _mapping(
            item.get("equivalence_class"),
            field=f"Hull ambiguity {order} equivalence_class",
        )
        if set(equivalence) != {
            "candidate_count_exact",
            "candidate_count_lower_bound",
            "candidates_exhaustive",
            "candidate_enumeration_limit",
            "policy_max_candidate_count",
            "overflow",
        }:
            _fail("Hull ambiguity equivalence-class field scope changed.")
        exact_count = equivalence.get("candidate_count_exact")
        if not isinstance(exact_count, int) or isinstance(exact_count, bool):
            _fail("Hull ambiguity exact candidate count is malformed.")
        if not (
            order in _AMBIGUOUS_LEGS
            and order == (0, 3)[index]
            and item.get("leg") == _AMBIGUOUS_LEGS[order]
            and item.get("start_osm_node_id") == station_anchor_ids[order]
            and item.get("end_osm_node_id") == station_anchor_ids[order + 1]
            and 2 <= exact_count <= MAXIMUM_EXHAUSTIVE_EQUIVALENCE_CLASS_CANDIDATES
            and equivalence.get("candidate_count_lower_bound") == exact_count
            and equivalence.get("candidates_exhaustive") is True
            and len(candidates) == exact_count
            and equivalence.get("candidate_enumeration_limit")
            == EXHAUSTIVE_EQUIVALENCE_CLASS_ENUMERATION_LIMIT
            and equivalence.get("policy_max_candidate_count")
            == MAXIMUM_EXHAUSTIVE_EQUIVALENCE_CLASS_CANDIDATES
            and equivalence.get("overflow") is False
            and item.get("renderer_geometry_tolerance_mm")
            == RENDERER_GEOMETRY_TOLERANCE_MM
            and item.get("owned_route_pen_nib_mm") == OWNED_ROUTE_NIB_MM
            and item.get("nib_fraction_tolerance") == MAXIMUM_NIB_FRACTION
            and item.get("all_candidates_below_reference_geometry_tolerance") is True
            and item.get("all_candidates_below_reference_nib_fraction") is True
            and item.get("all_candidate_pairs_below_geometry_tolerance") is True
            and item.get("all_candidate_pairs_below_nib_fraction") is True
        ):
            _fail(
                "Hull ambiguity is not a complete, exhaustive cartographic "
                "equivalence class within the 256-candidate safety cap."
            )
        renderer_metres_per_mm = _number(
            item.get("renderer_metres_per_mm"),
            field=f"Hull ambiguity {order} renderer_metres_per_mm",
        )
        if renderer_metres_per_mm <= 0.0:
            _fail("Hull ambiguity renderer scale is not positive.")
        if not isclose(
            renderer_metres_per_mm,
            gate_renderer_metres_per_mm,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            _fail("Hull ambiguity renderer scale changed from the sealed scale gate.")
        station_distance_m = _number(
            item.get("station_great_circle_distance_m"),
            field="Hull ambiguity station_great_circle_distance_m",
        )
        if station_distance_m <= 0.0:
            _fail("Hull ambiguity station distance is not positive.")
        reference = _mapping(
            item.get("cartographic_reference"),
            field=f"Hull ambiguity {order} cartographic_reference",
        )
        if not (
            set(reference)
            == {
                "path_sha256",
                "selection_rule",
                "minimum_tuple",
                "selected_candidate_specific_service_or_crossover_union_edge_count",
                "selected_candidate_specific_crossover_edge_count",
                "selected_candidate_specific_nonempty_service_edge_count",
                "selected_has_nonzero_candidate_specific_service_or_crossover_count",
                "operator_track_selection_claimed",
            }
            and reference.get("selection_rule") == CARTOGRAPHIC_SELECTION_RULE
            and reference.get("operator_track_selection_claimed") is False
        ):
            _fail("Hull cartographic reference rule or claim boundary changed.")

        common = _mapping(
            item.get("class_common_edge_intersection"),
            field=f"Hull ambiguity {order} class_common_edge_intersection",
        )
        common_edge_ids = _sorted_unique_strings(
            common.get("edge_ids"),
            field=f"Hull ambiguity {order} common edge_ids",
        )
        common_tag_keys = {
            "ineligible_edge_ids",
            "ineligible_edge_count",
            "excluded_service_values",
            "excluded_service_value_edge_ids",
            "excluded_service_value_edge_count",
            "service_tagged_edge_ids",
            "service_tagged_edge_count",
            "service_value_edge_counts",
            "crossover_edge_ids",
            "crossover_edge_count",
        }
        if not (
            set(common)
            == common_tag_keys
            | {"edge_ids", "edge_ids_sha256", "edge_count", "interpretation"}
            and common.get("edge_ids_sha256")
            == _canonical_sha256(list(common_edge_ids))
            and common.get("edge_count") == len(common_edge_ids)
            and common.get("interpretation") == _COMMON_INTERSECTION_INTERPRETATION
        ):
            _fail("Hull exact class-common edge intersection changed.")
        common_tags = _validate_tag_audit(
            {key: common.get(key) for key in common_tag_keys},
            field=f"Hull ambiguity {order} common tag audit",
            edge_scope=common_edge_ids,
            full_path=False,
        )
        if common_tags["ineligible"] or common_tags["excluded"]:
            _fail("Hull class-common path contains an ineligible or excluded edge.")

        pairwise = _mapping(
            item.get("pairwise_maximum_separation"),
            field=f"Hull ambiguity {order} pairwise_maximum_separation",
        )
        pairwise_metric_keys = {
            "densified_hausdorff_max_m",
            "renderer_paper_max_mm",
            "owned_0_4_mm_nib_fraction",
            "below_renderer_geometry_tolerance",
            "below_maximum_nib_fraction",
        }
        if set(pairwise) != pairwise_metric_keys | {
            "method",
            "candidate_pair_count",
            "worst_pair_path_sha256",
        }:
            _fail("Hull ambiguity pairwise field scope changed.")
        _, pairwise_mm, pairwise_nib_fraction = _validate_separation_metrics(
            {key: pairwise.get(key) for key in pairwise_metric_keys},
            field=f"Hull ambiguity {order} pairwise metrics",
            renderer_metres_per_mm=renderer_metres_per_mm,
        )
        if not (
            pairwise.get("method")
            == "OSGB EPSG:27700; Shapely densified discrete Hausdorff 0.1"
            and pairwise.get("candidate_pair_count")
            == exact_count * (exact_count - 1) // 2
            and 0.0 <= pairwise_mm <= RENDERER_GEOMETRY_TOLERANCE_MM
            and 0.0 <= pairwise_nib_fraction <= MAXIMUM_NIB_FRACTION
            and pairwise.get("below_renderer_geometry_tolerance") is True
            and pairwise.get("below_maximum_nib_fraction") is True
        ):
            _fail("Hull ambiguity pairwise class exceeds the sealed sub-nib gate.")

        candidate_hashes: set[str] = set()
        clean_hashes: list[str] = []
        rank_by_hash: dict[str, tuple[int, int, int, float, str]] = {}
        service_counts_by_hash: dict[str, dict[str, int]] = {}
        reference_metrics_by_hash: dict[str, tuple[float, float, float]] = {}
        edge_scope_by_hash: dict[str, tuple[str, ...]] = {}
        candidate_full_edge_sets: list[set[str]] = []
        candidate_lengths_m: list[float] = []
        for candidate_index, raw_candidate in enumerate(candidates):
            candidate = _mapping(
                raw_candidate,
                field=f"Hull ambiguity {order} candidate {candidate_index}",
            )
            if set(candidate) != {
                "path_sha256",
                "length_m",
                "edge_count",
                "node_count",
                "same_anchor_pair",
                "eligible_physical_edge_count",
                "ineligible_physical_edge_count",
                "detour_ratio",
                "detour_gate_passed",
                "class_relative_tag_selection",
                "reference_separation",
            }:
                _fail("Hull ambiguity candidate field scope changed.")
            path_sha = candidate.get("path_sha256")
            if not isinstance(path_sha, str):
                _fail("Hull ambiguity candidate lacks its exact path hash.")
            candidate_sha = _digest(path_sha, field="Hull candidate path_sha256")
            if candidate_sha in candidate_hashes:
                _fail("Hull ambiguity repeats an exact candidate path hash.")
            candidate_hashes.add(candidate_sha)
            selection = _mapping(
                candidate.get("class_relative_tag_selection"),
                field="Hull candidate class_relative_tag_selection",
            )
            if set(selection) != {
                "rule",
                "full_path_tag_audit",
                "candidate_specific_edge_ids",
                "candidate_specific_edge_ids_sha256",
                "candidate_specific_edge_count",
                "candidate_specific_tag_audit",
                "candidate_specific_service_or_crossover_union_edge_ids",
                "candidate_specific_service_or_crossover_union_edge_count",
                "candidate_specific_crossover_edge_count",
                "candidate_specific_nonempty_service_edge_count",
                "adds_zero_service_or_crossover_edges_outside_common",
                "clean_under_prior_zero_addition_rule",
                "cartographic_selection_rank",
            }:
                _fail("Hull candidate class-relative selection scope changed.")
            candidate_specific_ids = _sorted_unique_strings(
                selection.get("candidate_specific_edge_ids"),
                field="Hull candidate candidate_specific_edge_ids",
            )
            if set(candidate_specific_ids).intersection(common_edge_ids):
                _fail("Hull candidate-specific edges overlap the class intersection.")
            full_edge_scope = tuple(
                sorted(set(common_edge_ids) | set(candidate_specific_ids))
            )
            candidate_full_edge_sets.append(set(full_edge_scope))
            edge_scope_by_hash[candidate_sha] = full_edge_scope
            edge_count = candidate.get("edge_count")
            node_count = candidate.get("node_count")
            eligible_edge_count = candidate.get("eligible_physical_edge_count")
            ineligible_edge_count = candidate.get("ineligible_physical_edge_count")
            if not (
                isinstance(edge_count, int)
                and not isinstance(edge_count, bool)
                and isinstance(node_count, int)
                and not isinstance(node_count, bool)
                and isinstance(eligible_edge_count, int)
                and not isinstance(eligible_edge_count, bool)
                and isinstance(ineligible_edge_count, int)
                and not isinstance(ineligible_edge_count, bool)
                and edge_count > 0
                and node_count == edge_count + 1
                and eligible_edge_count == edge_count
                and ineligible_edge_count == 0
                and edge_count == len(full_edge_scope)
                and selection.get("candidate_specific_edge_ids_sha256")
                == _canonical_sha256(list(candidate_specific_ids))
                and selection.get("candidate_specific_edge_count")
                == len(candidate_specific_ids)
            ):
                _fail("Hull ambiguity candidate edge evidence is malformed.")

            full_tags = _validate_tag_audit(
                selection.get("full_path_tag_audit"),
                field=f"Hull candidate {candidate_sha} full-path tag audit",
                edge_scope=full_edge_scope,
                full_path=True,
            )
            specific_tags = _validate_tag_audit(
                selection.get("candidate_specific_tag_audit"),
                field=f"Hull candidate {candidate_sha} candidate-specific tag audit",
                edge_scope=candidate_specific_ids,
                full_path=False,
            )
            combined_counts = dict(common_tags["service_counts"])
            for service_value, count in specific_tags["service_counts"].items():
                combined_counts[service_value] = (
                    combined_counts.get(service_value, 0) + count
                )
            if not (
                set(full_tags["ineligible"])
                == set(common_tags["ineligible"]) | set(specific_tags["ineligible"])
                and set(full_tags["excluded"])
                == set(common_tags["excluded"]) | set(specific_tags["excluded"])
                and set(full_tags["service"])
                == set(common_tags["service"]) | set(specific_tags["service"])
                and set(full_tags["crossover"])
                == set(common_tags["crossover"]) | set(specific_tags["crossover"])
                and full_tags["service_counts"] == dict(sorted(combined_counts.items()))
                and not full_tags["ineligible"]
                and not full_tags["excluded"]
                and not specific_tags["ineligible"]
                and not specific_tags["excluded"]
            ):
                _fail(
                    "Hull candidate full-path and class-relative tag proofs disagree."
                )
            service_counts_by_hash[candidate_sha] = dict(full_tags["service_counts"])

            length_m = _number(
                candidate.get("length_m"),
                field="Hull ambiguity candidate length_m",
            )
            candidate_lengths_m.append(length_m)
            union_ids = _sorted_unique_strings(
                selection.get("candidate_specific_service_or_crossover_union_edge_ids"),
                field="Hull candidate service/crossover union edge IDs",
            )
            expected_union_ids = tuple(
                sorted(set(specific_tags["service"]) | set(specific_tags["crossover"]))
            )
            union_count = selection.get(
                "candidate_specific_service_or_crossover_union_edge_count"
            )
            crossover_count = selection.get("candidate_specific_crossover_edge_count")
            service_count = selection.get(
                "candidate_specific_nonempty_service_edge_count"
            )
            if not (
                isinstance(union_count, int)
                and not isinstance(union_count, bool)
                and isinstance(crossover_count, int)
                and not isinstance(crossover_count, bool)
                and isinstance(service_count, int)
                and not isinstance(service_count, bool)
            ):
                _fail("Hull candidate cartographic rank counts are malformed.")
            clean = union_count == 0
            rank = _mapping(
                selection.get("cartographic_selection_rank"),
                field="Hull candidate cartographic_selection_rank",
            )
            if set(rank) != {
                "ordered_component_names",
                "ordered_values",
                "candidate_specific_service_or_crossover_union_edge_count",
                "candidate_specific_crossover_edge_count",
                "candidate_specific_nonempty_service_edge_count",
                "path_length_m",
                "path_sha256",
            }:
                _fail("Hull candidate cartographic rank field scope changed.")
            ordered_values = _sequence(
                rank.get("ordered_values"),
                field="Hull candidate cartographic rank ordered_values",
            )
            expected_rank_values: list[int | float | str] = [
                union_count,
                crossover_count,
                service_count,
                length_m,
                candidate_sha,
            ]
            if not (
                selection.get("rule") == CARTOGRAPHIC_SELECTION_RULE
                and union_ids == expected_union_ids
                and union_count == len(expected_union_ids)
                and crossover_count == len(specific_tags["crossover"])
                and service_count == len(specific_tags["service"])
                and selection.get("adds_zero_service_or_crossover_edges_outside_common")
                is clean
                and selection.get("clean_under_prior_zero_addition_rule") is clean
                and rank.get("ordered_component_names")
                == list(CARTOGRAPHIC_SELECTION_COMPONENTS)
                and list(ordered_values) == expected_rank_values
                and rank.get("candidate_specific_service_or_crossover_union_edge_count")
                == union_count
                and rank.get("candidate_specific_crossover_edge_count")
                == crossover_count
                and rank.get("candidate_specific_nonempty_service_edge_count")
                == service_count
                and rank.get("path_length_m") == length_m
                and rank.get("path_sha256") == candidate_sha
            ):
                _fail("Hull candidate cartographic minimization rank changed.")
            if clean:
                clean_hashes.append(candidate_sha)
            rank_by_hash[candidate_sha] = (
                union_count,
                crossover_count,
                service_count,
                length_m,
                candidate_sha,
            )
            detour_ratio = _number(
                candidate.get("detour_ratio"),
                field="Hull ambiguity candidate detour_ratio",
            )
            if not (
                candidate.get("same_anchor_pair") is True
                and length_m > 0.0
                and 0.0 < detour_ratio <= 1.8
                and isclose(
                    detour_ratio,
                    length_m / station_distance_m,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                and candidate.get("detour_gate_passed") is True
            ):
                _fail("Hull ambiguity contains an ineligible or unbounded candidate.")
            reference_metrics = _validate_separation_metrics(
                candidate.get("reference_separation"),
                field=f"Hull candidate {candidate_sha} reference separation",
                renderer_metres_per_mm=renderer_metres_per_mm,
            )
            reference_metrics_by_hash[candidate_sha] = reference_metrics
            _, reference_mm, reference_nib_fraction = reference_metrics
            if not (
                reference_mm <= RENDERER_GEOMETRY_TOLERANCE_MM
                and reference_nib_fraction <= MAXIMUM_NIB_FRACTION
            ):
                _fail("Hull ambiguity contains an ineligible or unbounded candidate.")
        if len(candidate_hashes) != exact_count:
            _fail("Hull ambiguity repeats or omits an exact candidate path hash.")
        if max(candidate_lengths_m) - min(candidate_lengths_m) > 0.05 + 1e-9:
            _fail("Hull ambiguity candidates are not one equal-shortest class.")
        if pairwise_mm + 1e-9 < max(
            metrics[1] for metrics in reference_metrics_by_hash.values()
        ):
            _fail("Hull pairwise maximum is smaller than a reference separation.")
        recomputed_common = set(candidate_full_edge_sets[0])
        for full_edge_set in candidate_full_edge_sets[1:]:
            recomputed_common.intersection_update(full_edge_set)
        if recomputed_common != set(common_edge_ids):
            _fail("Hull declared common edge set is not the exact class intersection.")
        raw_candidate_hashes = _sorted_unique_strings(
            item.get("candidate_path_sha256"),
            field=f"Hull ambiguity {order} candidate hash disclosure",
        )
        raw_clean_hashes = _sorted_unique_strings(
            item.get("clean_zero_addition_candidate_path_sha256"),
            field=f"Hull ambiguity {order} prior-clean candidate disclosure",
        )
        for path_sha in (*raw_candidate_hashes, *raw_clean_hashes):
            _digest(path_sha, field="Hull disclosed candidate path_sha256")
        if not (
            raw_candidate_hashes == tuple(sorted(candidate_hashes))
            and raw_clean_hashes == tuple(sorted(clean_hashes))
            and item.get("no_zero_addition_candidate_available") is (not clean_hashes)
        ):
            _fail("Hull all-candidate or prior-zero-addition disclosure changed.")
        reference_sha = reference.get("path_sha256")
        if not isinstance(reference_sha, str) or reference_sha not in candidate_hashes:
            _fail("Hull cartographic representative is outside the sealed class.")
        minimum_tuple = min(rank_by_hash.values())
        raw_minimum_tuple = _sequence(
            reference.get("minimum_tuple"),
            field=f"Hull ambiguity {order} minimum_tuple",
        )
        if not (
            list(raw_minimum_tuple) == list(minimum_tuple)
            and reference_sha == minimum_tuple[4]
            and rank_by_hash[reference_sha] == minimum_tuple
            and reference.get(
                "selected_candidate_specific_service_or_crossover_union_edge_count"
            )
            == minimum_tuple[0]
            and reference.get("selected_candidate_specific_crossover_edge_count")
            == minimum_tuple[1]
            and reference.get("selected_candidate_specific_nonempty_service_edge_count")
            == minimum_tuple[2]
            and reference.get(
                "selected_has_nonzero_candidate_specific_service_or_crossover_count"
            )
            is (minimum_tuple[0] > 0)
        ):
            _fail("Hull cartographic reference is not the deterministic minimum tuple.")
        if (
            reference_metrics_by_hash[reference_sha][0]
            > SELF_SEPARATION_NUMERIC_TOLERANCE_M
        ):
            _fail("Hull selected reference does not have zero self-separation.")
        reference_by_order[order] = _HullAmbiguityReference(
            path_sha256=reference_sha,
            graph_edge_ids=edge_scope_by_hash[reference_sha],
            service_value_edge_counts=tuple(
                sorted(service_counts_by_hash[reference_sha].items())
            ),
        )

        raw_worst_pair = _sorted_unique_strings(
            pairwise.get("worst_pair_path_sha256"),
            field="Hull ambiguity worst_pair_path_sha256",
        )
        if not (
            len(raw_worst_pair) == 2 and set(raw_worst_pair).issubset(candidate_hashes)
        ):
            _fail("Hull pairwise maximum is not bound to two class members.")
        for path_sha in raw_worst_pair:
            _digest(path_sha, field="Hull pairwise path_sha256")
        legacy_witnesses = _sorted_unique_strings(
            item.get("legacy_witness_path_sha256"),
            field="Hull ambiguity legacy_witness_path_sha256",
        )
        if not (
            len(legacy_witnesses) == 2
            and set(legacy_witnesses).issubset(candidate_hashes)
        ):
            _fail("Hull legacy ambiguity witnesses left the exhaustive class.")
        for path_sha in legacy_witnesses:
            _digest(path_sha, field="Hull legacy witness path_sha256")
        if (
            not all(
                item.get(key) is True
                for key in (
                    "all_candidates_below_reference_geometry_tolerance",
                    "all_candidates_below_reference_nib_fraction",
                    "all_candidate_pairs_below_geometry_tolerance",
                    "all_candidate_pairs_below_nib_fraction",
                    "all_full_paths_zero_ineligible_or_excluded_edges",
                    "all_candidate_specific_tag_counts_disclosed",
                    "minimum_tuple_cartographic_reference_selected",
                    "selected_nonzero_candidate_specific_count_disclosed",
                )
            )
            or item.get("zero_addition_candidate_required") is not False
        ):
            _fail("Hull ambiguity class acceptance proof was weakened.")
        seen_orders.add(order)
    if seen_orders != set(_AMBIGUOUS_LEGS):
        _fail("Hull ambiguity orders changed from the two sealed customer legs.")
    return reference_by_order


def _validate_hull_contract_and_audit(
    corridor: TransitNetwork,
    audit: Mapping[str, Any],
    hashes: MixedOverviewInputHashes,
) -> _HullPatternEdgeLedger:
    if corridor.contract_sha256 != hashes.hull_contract_sha256:
        _fail("The Hull cartographic corridor contract hash does not match.")
    audit_digest = audit.get("ordered_evidence_sha256")
    if not isinstance(audit_digest, str) or (
        _digest(audit_digest, field="Hull audit ordered_evidence_sha256")
        != hashes.hull_audit_evidence_sha256
        or _document_sha256(audit) != audit_digest
    ):
        _fail("The Hull cartographic corridor audit digest does not verify.")
    if set(audit) != {
        "schema_version",
        "policy_version",
        "release_state",
        "product_id",
        "operator_code",
        "service_date",
        "sources",
        "claim_boundary",
        "scale_gate",
        "stations",
        "ambiguity_collapses",
        "representative_selection_rule",
        "legs",
        "selected_representative_service_value_edge_counts",
        "advertised_station_count",
        "physical_edge_count",
        "invented_connector_count",
        "proximity_join_count",
        "ordered_evidence_sha256",
    }:
        _fail("Hull standalone audit field scope changed.")
    if not (
        audit.get("schema_version") == 3
        and audit.get("policy_version") == HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION
        and audit.get("release_state")
        == "review-proof-scale-aware-cartographic-corridor-not-operational-track"
        and audit.get("product_id") == HULL_PRODUCT_ID
        and audit.get("operator_code") == HULL_OPERATOR_CODE
        and audit.get("service_date") == "2026-08-08"
    ):
        _fail("Hull audit policy, product, or dated identity changed.")
    claim = _mapping(audit.get("claim_boundary"), field="Hull claim_boundary")
    if dict(claim) != {
        "advertised_customer_station_scope": True,
        "scale_aware_cartographic_corridor": True,
        "sub_nib_parallel_track_ambiguity_collapsed": True,
        "operator_track_or_platform_binding_reviewed": False,
        "exact_operational_track_claimed": False,
        "exact_edge_corridor_policy_still_blocked": True,
        "generic_rail_inferred_as_operator_service": False,
    }:
        _fail("Hull claim boundary or exact-policy blocker status changed.")
    if not (
        audit.get("advertised_station_count") == 10
        and audit.get("invented_connector_count") == 0
        and audit.get("proximity_join_count") == 0
    ):
        _fail("Hull audit has changed station or connector counts.")
    audit_sources = _mapping(audit.get("sources"), field="Hull audit sources")
    _validate_hull_sources(corridor, audit_sources)

    if not (
        corridor.id == HULL_CORRIDOR_ID
        and corridor.kind == "national-operator"
        and corridor.format_id == "a3-landscape"
        and corridor.snapshot == "2026-08-08"
        and corridor.validity_status
        == "review-proof-cartographic-not-operational-track"
        and corridor.geometry_mode
        == "exact-osm-physical-segments-selected-as-scale-aware-cartographic-representatives-no-operational-track-claim"
        and not corridor.context
        and len(corridor.lines) == 1
    ):
        _fail("Mixed overview received the wrong Hull standalone contract.")
    line = corridor.lines[0]
    product = OPERATOR_REGISTRY.by_id[HULL_PRODUCT_ID]
    if not (
        line.id == HULL_LINE_ID
        and line.name == "Hull Trains scale-aware advertised corridor"
        and line.short_name == HULL_OPERATOR_CODE
        and line.order == 0
        and line.service_class
        == "scale-aware-cartographic-corridor-not-operational-track"
        and line.source_ref == HULL_COMPARISON_SOURCE_ID
        and line.colour.source_ref == REGISTRY_SOURCE_ID
        and line.colour.display_hex == product.presentation.display_hex
        and line.pen.pen_id == product.presentation.pen_id
    ):
        _fail("Hull cartographic line identity, claim, or presentation changed.")
    _owned_overview_pen(line.id, line.pen.pen_id, line.pen.nominal_nib_mm)

    ordered_station_ids, station_anchor_ids = _validate_hull_station_scope(
        corridor, audit
    )
    patterns = {pattern.id: pattern for pattern in corridor.service_patterns}
    if set(patterns) != {
        "hull-trains-scale-aware-advertised-core-corridor",
        "hull-trains-scale-aware-beverley-extension-corridor",
    }:
        _fail("Hull customer line/pattern scope changed.")
    core = patterns["hull-trains-scale-aware-advertised-core-corridor"]
    extension = patterns["hull-trains-scale-aware-beverley-extension-corridor"]
    expected_derivation = (
        "official-customer-station-scope-plus-scale-aware-sub-nib-"
        "cartographic-physical-corridor-not-operational-track"
    )
    if not (
        core.line_id == HULL_LINE_ID
        and extension.line_id == HULL_LINE_ID
        and core.name == "London King's Cross – Hull advertised cartographic corridor"
        and extension.name == "Hull – Beverley selected-services cartographic extension"
        and core.station_ids == ordered_station_ids[:8]
        and extension.station_ids == ordered_station_ids[7:]
        and core.source_ref == "hull-trains-customer-timetable-may-december-2026"
        and extension.source_ref == "hull-trains-customer-timetable-may-december-2026"
        and core.valid_from == "2026-05-17"
        and core.valid_to == "2026-12-12"
        and extension.valid_from == "2026-05-17"
        and extension.valid_to == "2026-12-12"
        and core.derivation_status == expected_derivation
        and extension.derivation_status == expected_derivation
        and not core.continuity_breaks
        and not extension.continuity_breaks
    ):
        _fail("Hull core/Beverley advertised station scope changed.")

    route_nodes = [node for node in corridor.nodes if not node.is_station]
    route_node_ids = {node.id for node in route_nodes}
    if not route_nodes or any(
        node.source_ref != HULL_PBF_SOURCE_ID
        or not node.id.startswith("cartographic-osm-node-")
        or node.source_object
        != node.id.removeprefix("cartographic-osm-").replace("node-", "node/", 1)
        for node in route_nodes
    ):
        _fail("Hull route-node provenance changed or contains a connector node.")
    if audit.get("physical_edge_count") != len(corridor.edges):
        _fail("Hull contract/audit physical edge counts disagree.")
    if any(
        edge.line_ids != (HULL_LINE_ID,)
        or edge.source_ref != HULL_PBF_SOURCE_ID
        or not edge.source_object.startswith("way/")
        or edge.status
        != "scale-aware-cartographic-representative-physical-segment-not-operational-track-claim"
        or edge.from_node not in route_node_ids
        or edge.to_node not in route_node_ids
        for edge in corridor.edges
    ):
        _fail("Hull route geometry contains changed evidence or an invented connector.")
    ambiguity_reference_by_order = _validate_ambiguity_gate(
        audit,
        station_anchor_ids=station_anchor_ids,
    )
    legs = _sequence(audit.get("legs"), field="Hull audit legs")
    if len(legs) != 9:
        _fail("Hull audit must bind all nine consecutive advertised legs.")
    leg_edge_counts: list[int] = []
    for order, raw in enumerate(legs):
        leg = _mapping(raw, field=f"Hull audit legs[{order}]")
        if set(leg) != {
            "order",
            "from_location",
            "to_location",
            "start_osm_node_id",
            "end_osm_node_id",
            "source_path_status",
            "selected_representative_path_sha256",
            "edge_count",
            "length_m",
            "station_great_circle_distance_m",
            "detour_ratio",
            "detour_gate_passed",
            "repeat_gate_passed",
            "invented_connector_count",
        }:
            _fail(f"Hull advertised leg field scope changed at order {order}.")
        first = _HULL_STATIONS[order][0]
        second = _HULL_STATIONS[order + 1][0]
        edge_count = leg.get("edge_count")
        if not isinstance(edge_count, int) or isinstance(edge_count, bool):
            _fail(f"Hull advertised leg edge count is malformed at order {order}.")
        length_m = _number(leg.get("length_m"), field=f"Hull leg {order} length_m")
        station_distance_m = _number(
            leg.get("station_great_circle_distance_m"),
            field=f"Hull leg {order} station_great_circle_distance_m",
        )
        detour_ratio = _number(
            leg.get("detour_ratio"), field=f"Hull leg {order} detour_ratio"
        )
        selected_sha = leg.get("selected_representative_path_sha256")
        if not isinstance(selected_sha, str):
            _fail(f"Hull advertised leg path hash is malformed at order {order}.")
        _digest(selected_sha, field=f"Hull leg {order} selected path SHA-256")
        if not (
            leg.get("order") == order
            and leg.get("from_location") == first
            and leg.get("to_location") == second
            and leg.get("start_osm_node_id") == station_anchor_ids[order]
            and leg.get("end_osm_node_id") == station_anchor_ids[order + 1]
            and edge_count > 0
            and length_m > 0.0
            and station_distance_m > 0.0
            and 0.0 < detour_ratio <= 1.8
            and isclose(
                detour_ratio,
                length_m / station_distance_m,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            and leg.get("invented_connector_count") == 0
            and leg.get("detour_gate_passed") is True
            and leg.get("repeat_gate_passed") is True
            and leg.get("source_path_status")
            == ("ambiguous" if order in _AMBIGUOUS_LEGS else "unique")
            and (
                order not in _AMBIGUOUS_LEGS
                or selected_sha == ambiguity_reference_by_order[order].path_sha256
            )
        ):
            _fail(f"Hull advertised leg evidence changed at order {order}.")
        leg_edge_counts.append(edge_count)

    core_traversal_ids = tuple(item.edge_id for item in core.traversals)
    extension_traversal_ids = tuple(item.edge_id for item in extension.traversals)
    all_traversal_ids = (*core_traversal_ids, *extension_traversal_ids)
    core_edge_ids = set(core_traversal_ids)
    extension_edge_ids = set(extension_traversal_ids)
    contract_edge_ids = {edge.id for edge in corridor.edges}
    physical_edge_union = core_edge_ids | extension_edge_ids
    cross_pattern_shared_edge_ids = core_edge_ids & extension_edge_ids
    if not (
        len(core_traversal_ids) == sum(leg_edge_counts[:7])
        and len(extension_traversal_ids) == sum(leg_edge_counts[7:])
        and len(all_traversal_ids) == sum(leg_edge_counts)
    ):
        _fail("Hull leg edge counts do not bind exactly to contract traversals.")
    if not (
        len(core_edge_ids) == len(core_traversal_ids)
        and len(extension_edge_ids) == len(extension_traversal_ids)
    ):
        _fail("Hull repeats a physical edge within one advertised pattern.")
    if physical_edge_union != contract_edge_ids:
        _fail("Hull pattern membership contains an orphan or extra physical edge.")
    if not (
        len(contract_edge_ids) == audit.get("physical_edge_count")
        and len(all_traversal_ids)
        == len(contract_edge_ids) + len(cross_pattern_shared_edge_ids)
    ):
        _fail("Hull cross-pattern physical-edge occurrence accounting changed.")
    for order, raw_leg in enumerate(legs):
        leg = _mapping(raw_leg, field=f"Hull audit legs[{order}]")
        if order < 7:
            start = sum(leg_edge_counts[:order])
            selected_traversals = core.traversals[
                start : start + leg_edge_counts[order]
            ]
        else:
            start = sum(leg_edge_counts[7:order])
            selected_traversals = extension.traversals[
                start : start + leg_edge_counts[order]
            ]
        selected_path_sha256, selected_graph_ids = (
            _graph_path_sha256_from_contract_traversals(
                selected_traversals,
                edge_by_id=corridor.edge_by_id,
                field=f"Hull advertised leg {order}",
            )
        )
        if selected_path_sha256 != leg.get("selected_representative_path_sha256"):
            _fail(
                f"Hull advertised leg {order} traversal path changed from its "
                "sealed selected representative."
            )
        ambiguity_reference = ambiguity_reference_by_order.get(order)
        if (
            ambiguity_reference is not None
            and tuple(sorted(selected_graph_ids)) != ambiguity_reference.graph_edge_ids
        ):
            _fail(
                f"Hull ambiguity leg {order} contract traversal changed from "
                "the selected exhaustive-class member."
            )
    selected_service_counts = _aggregate_service_value_counts(
        audit.get("selected_representative_service_value_edge_counts"),
        field="Hull selected service-tag counts",
        expected_edge_count=sum(leg_edge_counts),
    )
    ambiguity_service_minimums: dict[str, int] = defaultdict(int)
    for ambiguity_reference in ambiguity_reference_by_order.values():
        for service_value, count in ambiguity_reference.service_value_edge_counts:
            ambiguity_service_minimums[service_value] += count
    if any(
        selected_service_counts.get(service_value, 0) < minimum_count
        for service_value, minimum_count in ambiguity_service_minimums.items()
    ):
        _fail(
            "Hull selected service aggregate hides a disclosed class-common "
            "service or crossover edge."
        )
    rule = _mapping(
        audit.get("representative_selection_rule"),
        field="Hull representative selection rule",
    )
    if dict(rule) != {
        "required_ineligible_edge_count": 0,
        "required_excluded_yard_siding_spur_edge_count": 0,
        "zero_candidate_specific_service_or_crossover_count_required": False,
        "ordered_minimum_tuple_components": list(CARTOGRAPHIC_SELECTION_COMPONENTS),
        "candidate_specific_service_or_crossover_union_definition": (
            "union of nonempty-service and service=crossover edge IDs outside "
            "the exact class-common edge intersection"
        ),
        "class_common_service_or_crossover_edges": (
            "allowed only as disclosed unavoidable members of the exact edge-"
            "ID intersection shared by the full exhausted class; no operator-"
            "use inference"
        ),
        "crossover_classification": (
            "service=crossover; disclosed subset of nonempty service tags"
        ),
        "selection_rule": CARTOGRAPHIC_SELECTION_RULE,
        "nonzero_minimum_interpretation": (
            "bounded cartographic choice only; never operator-use evidence"
        ),
        "operator_track_inference": False,
    }:
        _fail("Hull cartographic representative selection policy changed.")
    if not any(
        item.get("kind") == "exact-edge-advertised-corridor-policy"
        and item.get("status") == "blocked-two-equal-shortest-ambiguities"
        for item in corridor.omissions
    ) or not any(
        item.get("kind") == "exact-operational-track-alignment"
        and item.get("status") == "not-reviewed-not-claimed"
        for item in corridor.omissions
    ):
        _fail("Hull exact-policy blocker disclosure is absent or changed.")
    if not any(
        note == f"Scale-aware evidence SHA-256: {audit_digest}."
        for note in corridor.notes
    ) or not any(
        note == f"Compiler policy: {HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION}."
        for note in corridor.notes
    ):
        _fail("Hull contract no longer binds its exact audit and policy IDs.")
    return _HullPatternEdgeLedger(
        core_traversal_count=len(core_traversal_ids),
        core_ordered_traversal_sha256=_ordered_traversal_sha256(core.traversals),
        extension_traversal_count=len(extension_traversal_ids),
        extension_ordered_traversal_sha256=_ordered_traversal_sha256(
            extension.traversals
        ),
        traversal_occurrence_count=len(all_traversal_ids),
        unique_physical_edge_count=len(contract_edge_ids),
        physical_edge_union_sha256=_canonical_sha256(sorted(contract_edge_ids)),
        cross_pattern_shared_physical_edge_count=len(cross_pattern_shared_edge_ids),
        cross_pattern_shared_physical_edge_ids_sha256=_canonical_sha256(
            sorted(cross_pattern_shared_edge_ids)
        ),
    )


def _merge_sources(
    base_sources: tuple[TransitSource, ...],
    hull_sources: tuple[TransitSource, ...],
) -> tuple[TransitSource, ...]:
    by_id = {source.id: source for source in base_sources}
    extra: list[TransitSource] = []
    for source in hull_sources:
        existing = by_id.get(source.id)
        if existing is None:
            extra.append(source)
            continue
        if source.id != REGISTRY_SOURCE_ID or existing.sha256 != source.sha256:
            _fail(f"Mixed overview source {source.id!r} conflicts with the base.")
    return (*base_sources, *extra)


def compile_mixed_operator_overview_with_scale_aware_hull(
    base_overview: TransitNetwork,
    hull_corridor: TransitNetwork,
    hull_audit: Mapping[str, Any],
    *,
    input_hashes: MixedOverviewInputHashes,
) -> tuple[TransitNetwork, dict[str, Any]]:
    """Merge one sealed Hull cartographic line into the unchanged 24-line base."""

    hashes = input_hashes.validated()
    _validate_base_overview(base_overview, hashes)
    hull_pattern_ledger = _validate_hull_contract_and_audit(
        hull_corridor, hull_audit, hashes
    )

    registry_order = next(
        index
        for index, product in enumerate(OPERATOR_REGISTRY.products)
        if product.id == HULL_PRODUCT_ID
    )
    hull_source_line = hull_corridor.lines[0]
    hull_line = replace(
        hull_source_line,
        name="Hull Trains — advertised cartographic corridor (not operational track)",
        short_name="HT*",
        order=registry_order,
    )
    if hull_line.id in base_overview.line_by_id:
        _fail("The base already contains the Hull cartographic line ID.")
    lines = tuple(
        sorted(
            (*base_overview.lines, hull_line),
            key=lambda line: (line.order, line.id),
        )
    )
    if len(lines) != 25:
        _fail("Mixed overview did not produce exactly 25 product lines.")

    hull_route_nodes = tuple(
        node for node in hull_corridor.nodes if not node.is_station
    )
    base_node_ids = set(base_overview.node_by_id)
    node_collisions = sorted(
        base_node_ids.intersection(node.id for node in hull_route_nodes)
    )
    if node_collisions:
        _fail("Mixed overview node IDs collide: " + ", ".join(node_collisions[:8]))
    base_edge_ids = set(base_overview.edge_by_id)
    edge_collisions = sorted(
        base_edge_ids.intersection(edge.id for edge in hull_corridor.edges)
    )
    if edge_collisions:
        _fail("Mixed overview edge IDs collide: " + ", ".join(edge_collisions[:8]))
    base_pattern_ids = {pattern.id for pattern in base_overview.service_patterns}
    pattern_collisions = sorted(
        base_pattern_ids.intersection(
            pattern.id for pattern in hull_corridor.service_patterns
        )
    )
    if pattern_collisions:
        _fail(
            "Mixed overview pattern IDs collide: " + ", ".join(pattern_collisions[:8])
        )
    hull_patterns: tuple[ServicePattern, ...] = tuple(
        replace(
            pattern,
            line_id=hull_line.id,
            station_ids=(),
            name=pattern.name + " / OVERVIEW — CARTOGRAPHIC, NOT OPERATIONAL",
        )
        for pattern in hull_corridor.service_patterns
    )

    pen_groups: defaultdict[str, list[str]] = defaultdict(list)
    pen_mapping: list[dict[str, Any]] = []
    for line in lines:
        assert line.pen.pen_id is not None
        _owned_overview_pen(line.id, line.pen.pen_id, line.pen.nominal_nib_mm)
        pen_groups[line.pen.pen_id].append(line.id)
        pen_mapping.append(
            {
                "line_id": line.id,
                "display_name": line.name,
                "pen_id": line.pen.pen_id,
                "nominal_nib_mm": 0.4,
                "pass_count": 1,
                "requested_plotted_width_mm": 0.4,
            }
        )
    collisions = {
        pen_id: line_ids
        for pen_id, line_ids in sorted(pen_groups.items())
        if len(line_ids) > 1
    }

    hull_audit_digest = str(hull_audit["ordered_evidence_sha256"])
    mixed_audit: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": MIXED_OVERVIEW_POLICY_VERSION,
        "release_state": ("review-proof-mixed-evidence-not-official-operational-map"),
        "input_bindings": {
            "base_overview": {
                "contract_sha256": hashes.base_contract_sha256,
                "compiler_policy_version": OPERATOR_OVERVIEW_POLICY_VERSION,
                "audit_file_sha256": hashes.base_osm_audit_file_sha256,
                "osm_evidence_sha256": hashes.base_osm_evidence_sha256,
                "line_count": 24,
                "evidence_method": "osm-catalog-qualified-route-train-relations",
            },
            "hull_cartographic_corridor": {
                "contract_sha256": hashes.hull_contract_sha256,
                "audit_file_sha256": hashes.hull_audit_file_sha256,
                "audit_evidence_sha256": hashes.hull_audit_evidence_sha256,
                "compiler_policy_version": (HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION),
                "equivalence_comparison_policy_version": (
                    EQUIVALENCE_COMPARISON_POLICY_VERSION
                ),
                "exact_edge_blocker_policy_version": (
                    "hull-trains-advertised-customer-corridor-v1"
                ),
                "product_id": HULL_PRODUCT_ID,
                "line_count": 1,
                "advertised_station_count_in_standalone": 10,
                "evidence_method": (
                    "advertised-scale-aware-cartographic-corridor-not-operational-track"
                ),
            },
        },
        "output_counts": {
            "registry_product_count": 25,
            "osm_relation_product_count": 24,
            "advertised_cartographic_corridor_product_count": 1,
            "line_count": 25,
            "station_marker_count": 0,
        },
        "claim_boundary": {
            "all_25_products_use_one_evidence_method": False,
            "hull_is_osm_operator_relation": False,
            "hull_is_exact_operational_track_or_platform_map": False,
            "hull_is_advertised_scale_aware_cartographic_corridor": True,
            "hull_exact_edge_policy_still_blocked": True,
            "mixed_evidence_legend_required": True,
        },
        "hull_scale_gate": {
            "ambiguity_count": 2,
            "equivalence_classes_exhaustive": True,
            "all_full_paths_zero_ineligible_or_excluded_edges": True,
            "all_candidate_specific_tag_counts_disclosed": True,
            "minimum_tuple_cartographic_references_selected": True,
            "zero_addition_candidate_required": False,
            "selected_nonzero_candidate_specific_counts_disclosed": True,
            "maximum_exhaustive_class_candidate_count": (
                MAXIMUM_EXHAUSTIVE_EQUIVALENCE_CLASS_CANDIDATES
            ),
            "exact_candidate_counts_by_leg": [
                {
                    "order": item["order"],
                    "leg": item["leg"],
                    "candidate_count_exact": item["equivalence_class"][
                        "candidate_count_exact"
                    ],
                }
                for item in hull_audit["ambiguity_collapses"]
            ],
            "class_common_intersections": [
                {
                    "order": item["order"],
                    "leg": item["leg"],
                    "edge_ids_sha256": item["class_common_edge_intersection"][
                        "edge_ids_sha256"
                    ],
                    "edge_count": item["class_common_edge_intersection"]["edge_count"],
                    "service_value_edge_counts": item["class_common_edge_intersection"][
                        "service_value_edge_counts"
                    ],
                    "crossover_edge_count": item["class_common_edge_intersection"][
                        "crossover_edge_count"
                    ],
                }
                for item in hull_audit["ambiguity_collapses"]
            ],
            "reference_selection_audits": [
                {
                    "order": item["order"],
                    "selected_path_sha256": item["cartographic_reference"][
                        "path_sha256"
                    ],
                    "selection_rule": item["cartographic_reference"]["selection_rule"],
                    "minimum_tuple": item["cartographic_reference"]["minimum_tuple"],
                    "selected_candidate_specific_service_or_crossover_union_edge_count": item[
                        "cartographic_reference"
                    ][
                        "selected_candidate_specific_service_or_crossover_union_edge_count"
                    ],
                    "selected_candidate_specific_crossover_edge_count": item[
                        "cartographic_reference"
                    ]["selected_candidate_specific_crossover_edge_count"],
                    "selected_candidate_specific_nonempty_service_edge_count": item[
                        "cartographic_reference"
                    ]["selected_candidate_specific_nonempty_service_edge_count"],
                    "selected_has_nonzero_candidate_specific_service_or_crossover_count": item[
                        "cartographic_reference"
                    ][
                        "selected_has_nonzero_candidate_specific_service_or_crossover_count"
                    ],
                    "candidate_path_sha256": item["candidate_path_sha256"],
                    "clean_zero_addition_candidate_path_sha256": item[
                        "clean_zero_addition_candidate_path_sha256"
                    ],
                    "no_zero_addition_candidate_available": item[
                        "no_zero_addition_candidate_available"
                    ],
                }
                for item in hull_audit["ambiguity_collapses"]
            ],
            "renderer_geometry_tolerance_mm": RENDERER_GEOMETRY_TOLERANCE_MM,
            "owned_route_pen_nib_mm": OWNED_ROUTE_NIB_MM,
            "maximum_nib_fraction": MAXIMUM_NIB_FRACTION,
        },
        "geometry_preservation": {
            "base_sources_lines_nodes_edges_patterns_unchanged": True,
            "base_omissions_and_notes_preserved_as_prefix": True,
            "hull_edges_keep_separate_physical_pbf_provenance": True,
            "hull_station_markers_omitted_only_from_national_overview": True,
            "invented_connector_count": 0,
            "proximity_join_count": 0,
        },
        "hull_pattern_edge_membership": {
            "core": {
                "pattern_id": ("hull-trains-scale-aware-advertised-core-corridor"),
                "traversal_count": hull_pattern_ledger.core_traversal_count,
                "ordered_traversal_sha256": (
                    hull_pattern_ledger.core_ordered_traversal_sha256
                ),
                "duplicate_traversal_count": 0,
            },
            "beverley_extension": {
                "pattern_id": ("hull-trains-scale-aware-beverley-extension-corridor"),
                "traversal_count": (hull_pattern_ledger.extension_traversal_count),
                "ordered_traversal_sha256": (
                    hull_pattern_ledger.extension_ordered_traversal_sha256
                ),
                "duplicate_traversal_count": 0,
            },
            "traversal_occurrence_count": (
                hull_pattern_ledger.traversal_occurrence_count
            ),
            "unique_physical_edge_count": (
                hull_pattern_ledger.unique_physical_edge_count
            ),
            "physical_edge_union_sha256": (
                hull_pattern_ledger.physical_edge_union_sha256
            ),
            "orphan_physical_edge_count": 0,
            "extra_traversal_edge_count": 0,
            "cross_pattern_shared_physical_edge_count": (
                hull_pattern_ledger.cross_pattern_shared_physical_edge_count
            ),
            "cross_pattern_shared_physical_edge_ids_sha256": (
                hull_pattern_ledger.cross_pattern_shared_physical_edge_ids_sha256
            ),
            "occurrence_accounting": (
                "core + extension = unique physical union + cross-pattern intersection"
            ),
            "cross_pattern_reuse_emits_duplicate_physical_edge_records": False,
        },
        "overview_pen_policy": {
            "policy": "exactly-one-owned-0-4-mm-pass-per-product-line",
            "line_mapping": pen_mapping,
            "physical_pen_colour_collisions": collisions,
            "screen_colours_are_not_distinct_physical_ink_claims": True,
        },
        "hull_source_audit_evidence_sha256": hull_audit_digest,
    }
    mixed_audit["ordered_evidence_sha256"] = _document_sha256(mixed_audit)

    network = TransitNetwork(
        id="great-britain-passenger-operator-mixed-evidence-overview-2026-08-08",
        name="GREAT BRITAIN PASSENGER OPERATORS",
        kind="national-operator-overview",
        scope=(
            "GREAT BRITAIN / 25 REGISTRY PRODUCTS / 24 OSM OPERATOR-RELATION "
            "REVIEW PROOFS + 1 HULL ADVERTISED SCALE-AWARE CARTOGRAPHIC "
            "CORRIDOR / NOT AN OFFICIAL OPERATIONAL TRACK MAP"
        ),
        format_id=base_overview.format_id,
        snapshot="2026-08-08",
        validity_status="review-proof-mixed-evidence-not-operational-track",
        geometry_mode=(
            "mixed-evidence-24-unchanged-osm-relation-proofs-plus-one-separate-"
            "hull-scale-aware-cartographic-corridor-no-joins"
        ),
        sources=_merge_sources(base_overview.sources, hull_corridor.sources),
        lines=lines,
        nodes=(*base_overview.nodes, *hull_route_nodes),
        edges=(*base_overview.edges, *hull_corridor.edges),
        service_patterns=(*base_overview.service_patterns, *hull_patterns),
        context=(),
        omissions=(
            *base_overview.omissions,
            *hull_corridor.omissions,
            {
                "kind": "mixed-evidence-registry-product-coverage",
                "status": "25-products-represented-by-two-disclosed-methods",
                "registry_snapshot": OPERATOR_REGISTRY.snapshot,
                "registry_product_count": 25,
                "osm_relation_product_count": 24,
                "advertised_cartographic_corridor_product_count": 1,
                "reason": (
                    "This additive record does not rewrite the original 24-of-25 "
                    "OSM coverage ledger. It records that the separate HT* "
                    "cartographic evidence supplies a 25th visual product."
                ),
            },
            {
                "kind": "mixed-evidence-product",
                "product_id": HULL_PRODUCT_ID,
                "status": (
                    "advertised-scale-aware-cartographic-corridor-not-operational-track"
                ),
                "reason": (
                    "The original OSM relation omission remains. HT* is a "
                    "separately hashed advertised cartographic corridor whose two "
                    "exhaustive parallel-track equivalence classes are sub-nib at "
                    "A3. Class-common service/crossover edges are disclosed as "
                    "unavoidable graph structure; every physically valid candidate "
                    "is disclosed and the bounded minimum tuple is cartographic "
                    "only, even when its selected count is nonzero. It is not "
                    "an operator track, platform, or WTT-transition claim."
                ),
            },
            {
                "kind": "overview-station-markers",
                "product_id": HULL_PRODUCT_ID,
                "status": "omitted-at-national-scale-retained-in-standalone",
                "reason": (
                    "The ten advertised station points and their exact scope remain "
                    "in the sealed standalone contract/audit. The national overview "
                    "does not mix those markers into the relation-only products."
                ),
            },
            {
                "kind": "mixed-overview-physical-pen-binding",
                "status": "exactly-one-owned-0-4-mm-pass-per-product-line",
                "hull_line_id": HULL_LINE_ID,
                "hull_pen_id": hull_line.pen.pen_id,
                "hull_pass_count": 1,
                "hull_plotted_width_mm": 0.4,
                "pen_line_groups": dict(sorted(pen_groups.items())),
                "reason": (
                    "The overview uses one physical 0.4 mm pass per product. Screen "
                    "colours are reference colours; shared owned inks are disclosed "
                    "rather than presented as 25 physically distinct pens."
                ),
            },
        ),
        notes=(
            *base_overview.notes,
            "MIXED EVIDENCE: HT* is the advertised scale-aware Hull Trains cartographic corridor, not an OSM relation or operational track/platform map.",
            "The original 24 OSM lines, their source records, geometry, patterns, omissions, and notes are retained unchanged; Hull keeps separate PBF edge provenance.",
            "Hull ambiguity classes are exhaustive: every path has zero ineligible or yard/siding/spur edges, all candidate-specific tag counts are disclosed, and the cartographic representative minimizes union count, crossover count, service count, path length, then path hash; a nonzero minimum proves no operator use.",
            f"Hull pattern membership has {hull_pattern_ledger.traversal_occurrence_count} traversal occurrences over {hull_pattern_ledger.unique_physical_edge_count} unique physical edge records; {hull_pattern_ledger.cross_pattern_shared_physical_edge_count} physical edges belong to both the core and Beverley-extension patterns and are emitted once.",
            "The ten Hull advertised station points stay in the standalone proof and are deliberately omitted from this national overview.",
            "Every overview product requests exactly one owned 0.4 mm pen pass; display colours do not claim 25 physically distinct inks.",
            f"Base 24-line contract SHA-256: {hashes.base_contract_sha256}.",
            f"Base OSM audit file SHA-256: {hashes.base_osm_audit_file_sha256}.",
            f"Hull contract SHA-256: {hashes.hull_contract_sha256}.",
            f"Hull audit file SHA-256: {hashes.hull_audit_file_sha256}.",
            f"Hull evidence SHA-256: {hull_audit_digest}.",
            f"Mixed overview evidence SHA-256: {mixed_audit['ordered_evidence_sha256']}.",
            f"Compiler policy: {MIXED_OVERVIEW_POLICY_VERSION}.",
        ),
        contract_sha256="",
    )
    validate_transit_network(network)
    return network, mixed_audit


def compact_internal_attributions_for_house_overview(
    network: TransitNetwork,
) -> tuple[TransitNetwork, dict[str, Any]]:
    """Prepare complete but deduplicable visible credit for the dense A3 overview.

    The source IDs, hashes, publishers, URLs, licences, uses, validity dates,
    and reuse statuses remain exact. Repeated first-party copy is compacted.
    The three records for the same pinned OSM GB database receive one identical
    expanded attribution that contains the original publisher copy plus the
    database and licence URLs; the renderer can then deduplicate it without
    weakening the visible ODbL notice. Every binding remains in the manifest.
    """

    if network.kind != "national-operator-overview":
        _fail("Internal attribution compaction is restricted to operator overviews.")
    internal_indexes = tuple(
        index
        for index, source in enumerate(network.sources)
        if source.publisher == "City Map Plotter"
    )
    if len(internal_indexes) < 2:
        _fail("Operator overview has fewer than two first-party source credits.")

    original_document = network.as_dict()
    osm_duplicate_indexes = tuple(
        index
        for index, source in enumerate(network.sources)
        if source.publisher == "OpenStreetMap contributors; extract by Geofabrik"
        and source.sha256
        == "2e0b431cfe07311baa5356375e32d3cc6532edb9d1dab458f360104c2c73f9c3"
        and source.url == "https://download.geofabrik.de/europe/great-britain.html"
        and source.attribution == "© OpenStreetMap contributors"
        and "open database licence 1.0" in source.licence.casefold()
    )
    if len(osm_duplicate_indexes) != 3:
        _fail(
            "Mixed overview must contain exactly three identical pinned OSM GB "
            "database bindings before visible-credit normalization."
        )
    compacted_sources = tuple(
        replace(source, attribution=MIXED_OVERVIEW_INTERNAL_ATTRIBUTION)
        if index in internal_indexes
        else replace(source, attribution=MIXED_OVERVIEW_OSM_DATABASE_ATTRIBUTION)
        if index in osm_duplicate_indexes
        else source
        for index, source in enumerate(network.sources)
    )
    note = (
        "PRESENTATION CREDIT: repeated first-party City Map Plotter evidence "
        "bindings use one compact visible attribution, while the three bindings "
        "to the same pinned OSM GB database use one complete shared OSM/DB/ODbL "
        "notice. Every source ID, hash, URL, licence, use, and full record remains "
        "in this contract and the plot manifest."
    )
    compacted = replace(
        network,
        sources=compacted_sources,
        notes=(*network.notes, note),
        contract_sha256="",
    )
    validate_transit_network(compacted)

    compacted_document = compacted.as_dict()
    restored_document = json.loads(json.dumps(compacted_document))
    for index in internal_indexes:
        restored_document["sources"][index]["attribution"] = original_document[
            "sources"
        ][index]["attribution"]
    for index in osm_duplicate_indexes:
        restored_document["sources"][index]["attribution"] = original_document[
            "sources"
        ][index]["attribution"]
    if restored_document["notes"][-1] != note:
        _fail("Internal attribution audit could not identify its appended note.")
    restored_document["notes"] = restored_document["notes"][:-1]
    if restored_document != original_document:
        _fail("Internal attribution compaction changed undeclared contract fields.")

    unchanged_external_indexes = tuple(
        index
        for index in range(len(network.sources))
        if index not in internal_indexes and index not in osm_duplicate_indexes
    )
    audit: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": MIXED_OVERVIEW_ATTRIBUTION_POLICY_VERSION,
        "release_state": "review-presentation-binding-not-route-evidence",
        "network_id": network.id,
        "compact_visible_attribution": MIXED_OVERVIEW_INTERNAL_ATTRIBUTION,
        "compacted_source_ids": sorted(
            network.sources[index].id for index in internal_indexes
        ),
        "compacted_source_count": len(internal_indexes),
        "normalized_duplicate_osm_source_ids": sorted(
            network.sources[index].id for index in osm_duplicate_indexes
        ),
        "normalized_duplicate_osm_source_count": len(osm_duplicate_indexes),
        "normalized_duplicate_osm_visible_attribution": (
            MIXED_OVERVIEW_OSM_DATABASE_ATTRIBUTION
        ),
        "osm_publisher_attribution_preserved_verbatim": all(
            network.sources[index].attribution in compacted.sources[index].attribution
            for index in osm_duplicate_indexes
        ),
        "unchanged_external_source_ids": sorted(
            network.sources[index].id for index in unchanged_external_indexes
        ),
        "unchanged_external_source_records_exact": all(
            compacted.sources[index] == network.sources[index]
            for index in unchanged_external_indexes
        ),
        "source_ids_unchanged": [source.id for source in compacted.sources]
        == [source.id for source in network.sources],
        "source_hashes_unchanged": [source.sha256 for source in compacted.sources]
        == [source.sha256 for source in network.sources],
        "all_source_non_attribution_fields_unchanged": all(
            replace(
                compacted.sources[index], attribution=network.sources[index].attribution
            )
            == network.sources[index]
            for index in (*internal_indexes, *osm_duplicate_indexes)
        ),
        "only_declared_attribution_and_note_fields_changed": True,
        "route_geometry_sha256": _canonical_sha256(
            {
                key: original_document[key]
                for key in ("lines", "nodes", "edges", "service_patterns")
            }
        ),
        "context_geometry_sha256": _canonical_sha256(original_document["context"]),
        "original_attributions": {
            network.sources[index].id: network.sources[index].attribution
            for index in internal_indexes
        },
        "appended_note": note,
    }
    audit["ordered_evidence_sha256"] = _document_sha256(audit)
    return compacted, audit


__all__ = [
    "BASE_OVERVIEW_ID",
    "HULL_CORRIDOR_ID",
    "MAXIMUM_EXHAUSTIVE_EQUIVALENCE_CLASS_CANDIDATES",
    "MIXED_OVERVIEW_ATTRIBUTION_POLICY_VERSION",
    "MIXED_OVERVIEW_INTERNAL_ATTRIBUTION",
    "MIXED_OVERVIEW_OSM_DATABASE_ATTRIBUTION",
    "MIXED_OVERVIEW_POLICY_VERSION",
    "SELF_SEPARATION_NUMERIC_TOLERANCE_M",
    "MixedOverviewInputHashes",
    "compact_internal_attributions_for_house_overview",
    "compile_mixed_operator_overview_with_scale_aware_hull",
]
