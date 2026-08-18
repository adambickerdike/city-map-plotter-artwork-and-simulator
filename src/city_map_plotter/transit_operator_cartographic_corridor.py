"""Scale-aware Hull Trains cartographic corridor compilation.

This is intentionally separate from the exact-edge advertised-corridor policy.
The exact policy remains blocked when equal shortest physical paths exist.  This
module may collapse only a sealed, scale-proved, sub-nib parallel-track
ambiguity into one deterministic *cartographic* representative.  It never
claims that Hull Trains uses the selected track, crossover, or platform.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import isclose
from typing import Any, Mapping, NoReturn, Sequence

from pyproj import Transformer
from shapely import hausdorff_distance
from shapely.geometry import LineString

from .models import BoundingBox, MapPlotterError
from .niche_common import PlateContext
from .transit import (
    ColourSpec,
    EdgeTraversal,
    ServicePattern,
    TransitEdge,
    TransitLine,
    TransitNetwork,
    TransitNode,
    TransitSource,
    validate_transit_network,
)
from .transit_composition import (
    DEFAULT_FURNITURE_MARGIN_FRACTION,
    MINIMUM_FURNITURE_MARGIN_MM,
)
from .transit_extent import named_operator_projection_extent
from .transit_operator_corridor import (
    DEFAULT_ANCHOR_CANDIDATE_LIMIT,
    DEFAULT_ANCHOR_RADIUS_M,
    ELIGIBLE_CORRIDOR_RAILWAY_VALUE,
    EXCLUDED_CORRIDOR_SERVICE_VALUES,
    HULL_OPERATOR_CODE,
    HULL_PRODUCT_ID,
    MAX_LEG_DETOUR_RATIO,
    MAX_MECHANICAL_ANCHOR_DISTANCE_M,
    _candidate_node_dict,
    _grade,
    _haversine_m,
    _physical_pen,
    _registry_sha256,
    _require_mapping,
    _require_sequence,
    _sha256_document,
    _validate_scope,
    _verified_candidate_digest,
)
from .transit_operator_registry import OPERATOR_REGISTRY
from .transit_rail_graph import (
    OsmRailGraph,
    RailGraphAmbiguityError,
    RailGraphRoutingError,
    RailPathCandidateEvidence,
    RailRoute,
)


HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION = (
    "hull-trains-scale-aware-cartographic-corridor-v3"
)
LEGACY_COMPARISON_POLICY_VERSION = (
    "hull-trains-ambiguity-geographic-comparison-v1"
)
COMPARISON_POLICY_VERSION = LEGACY_COMPARISON_POLICY_VERSION
EQUIVALENCE_COMPARISON_POLICY_VERSION = (
    "hull-trains-ambiguity-equivalence-class-comparison-v3"
)
POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT = 256
EQUIVALENCE_CANDIDATE_ENUMERATION_LIMIT = (
    POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT + 1
)
ALL_NINE_DIAGNOSTIC_POLICY_VERSION = (
    "hull-trains-advertised-customer-corridor-v1"
)
RENDERER_GEOMETRY_TOLERANCE_MM = 0.04
MAXIMUM_NIB_FRACTION = 0.10
OWNED_ROUTE_NIB_MM = 0.4
AMBIGUOUS_LEG_ORDERS = frozenset({0, 3})
CARTOGRAPHIC_SELECTION_COMPONENTS = (
    "candidate_specific_service_or_crossover_union_edge_count",
    "candidate_specific_crossover_edge_count",
    "candidate_specific_nonempty_service_edge_count",
    "path_length_m",
    "path_sha256",
)
CARTOGRAPHIC_SELECTION_RULE = (
    "require-exhaustive-class-with-zero-full-path-ineligible-and-zero-yard-"
    "siding-spur;rank-all-physically-valid-candidates-by-(candidate-specific-"
    "service-or-crossover-union-edge-count-outside-exact-common-intersection,"
    "candidate-specific-crossover-edge-count,candidate-specific-nonempty-"
    "service-edge-count,path-length-m,path-sha256);cartographic-only-no-"
    "operational-track-inference"
)
_WGS84_TO_OSGB = Transformer.from_crs(
    "EPSG:4326", "EPSG:27700", always_xy=True
)


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(message)


@dataclass(frozen=True, slots=True)
class _ValidatedInputs:
    candidate_evidence_sha256: str
    registry_sha256: str
    stations: tuple[Mapping[str, Any], ...]
    official: Mapping[str, Any]
    anchor_ids: tuple[int, ...]
    station_audit: tuple[dict[str, Any], ...]


def _validate_inputs(
    graph: OsmRailGraph,
    candidate: Mapping[str, Any],
    scope: Mapping[str, Any],
    *,
    official_pdf_sha256: str,
    naptan_sha256: str,
    max_anchor_distance_m: float,
) -> _ValidatedInputs:
    candidate_evidence_sha256 = _verified_candidate_digest(candidate)
    if (
        candidate.get("operator_code") != HULL_OPERATOR_CODE
        or candidate.get("service_date") != "2026-08-08"
        or candidate.get("approved") is not False
        or candidate.get("release_state") != "candidate-not-reviewed"
    ):
        _fail("Hull upstream candidate identity or review state changed.")
    claims = _require_mapping(candidate.get("claims"), field="candidate.claims")
    if not (
        claims.get("coordinate_binding_approved") is False
        and claims.get("operator_alignment_approved") is False
        and claims.get("service_identity_selection_approved") is False
        and claims.get("invented_connector_count") == 0
        and claims.get("proximity_join_count") == 0
    ):
        _fail("Hull candidate must retain unresolved operational bindings.")
    sources = _require_mapping(candidate.get("sources"), field="candidate.sources")
    graph_source = _require_mapping(
        sources.get("rail_graph"), field="candidate.sources.rail_graph"
    )
    pbf_source = _require_mapping(
        sources.get("osm_pbf"), field="candidate.sources.osm_pbf"
    )
    naptan_source = _require_mapping(
        sources.get("naptan"), field="candidate.sources.naptan"
    )
    operator_scope_source = _require_mapping(
        sources.get("operator_scope_evidence"),
        field="candidate.sources.operator_scope_evidence",
    )
    registry_sha256 = _registry_sha256()
    if (
        graph_source.get("graph_sha256") != graph.graph_sha256
        or graph_source.get("source_sha256") != graph.source.sha256
        or pbf_source.get("sha256") != graph.source.sha256
    ):
        _fail("Hull candidate is not bound to the loaded physical rail graph.")
    if naptan_source.get("sha256") != naptan_sha256:
        _fail("Hull candidate NaPTAN source hash changed.")
    if operator_scope_source.get("sha256") != registry_sha256:
        _fail("Hull candidate operator-scope assertion changed.")

    stations_raw, official = _validate_scope(
        scope,
        official_pdf_sha256=official_pdf_sha256,
        registry_sha256=registry_sha256,
    )
    stations = tuple(stations_raw)
    timing_locations = _require_sequence(
        candidate.get("timing_locations"), field="candidate.timing_locations"
    )
    timing_by_name: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(timing_locations):
        record = _require_mapping(raw, field=f"candidate.timing_locations[{index}]")
        location = record.get("location")
        if isinstance(location, str):
            timing_by_name[location] = record

    anchor_ids: list[int] = []
    station_audit: list[dict[str, Any]] = []
    for station in stations:
        location = str(station["location"])
        timing = timing_by_name.get(location)
        if timing is None:
            _fail(f"Hull candidate lacks advertised station {location}.")
        raw_coordinates = _require_sequence(
            timing.get("coordinate_candidates"),
            field=f"candidate timing location {location} coordinates",
        )
        matches = [
            _require_mapping(item, field=f"candidate {location} coordinate")
            for item in raw_coordinates
            if isinstance(item, Mapping)
            and item.get("source_kind") == "naptan"
            and item.get("source_object") == station["naptan_atco_code"]
            and item.get("match_strength") == "canonical-exact"
        ]
        if len(matches) != 1:
            _fail(
                f"Hull station {location} needs exactly one canonical NaPTAN RLY "
                "record."
            )
        coordinate = matches[0]
        if (
            coordinate.get("stop_type") != "RLY"
            or abs(
                float(coordinate.get("lon"))  # type: ignore[arg-type]
                - float(station["lon"])
            )
            > 1e-11
            or abs(
                float(coordinate.get("lat"))  # type: ignore[arg-type]
                - float(station["lat"])
            )
            > 1e-11
        ):
            _fail(f"Hull station {location} NaPTAN coordinate changed.")
        live_candidates = graph.nearest_node_candidates(
            float(station["lon"]),
            float(station["lat"]),
            max_distance_m=DEFAULT_ANCHOR_RADIUS_M,
            limit=DEFAULT_ANCHOR_CANDIDATE_LIMIT,
        )
        stored_candidates = _require_sequence(
            coordinate.get("graph_node_candidates"),
            field=f"candidate {location} graph nodes",
        )
        if [_candidate_node_dict(item) for item in live_candidates] != list(
            stored_candidates
        ):
            _fail(f"Hull station {location} graph-node candidate evidence changed.")
        if not live_candidates:
            _fail(f"Hull station {location} has no physical rail anchor candidate.")
        selected = live_candidates[0]
        if selected.distance_m > max_anchor_distance_m:
            _fail(
                f"Hull station {location} nearest physical node is "
                f"{selected.distance_m:.1f} m away; no connector was invented."
            )
        anchor_ids.append(selected.osm_node_id)
        station_audit.append(
            {
                "order": station["order"],
                "location": location,
                "display_name": station["display_name"],
                "naptan_atco_code": station["naptan_atco_code"],
                "station_lon": station["lon"],
                "station_lat": station["lat"],
                "selected_osm_node_id": selected.osm_node_id,
                "selected_node_lon": selected.lon,
                "selected_node_lat": selected.lat,
                "distance_m": selected.distance_m,
                "candidate_rank": 1,
                "selection_method": (
                    "mechanical-nearest-exact-graph-node-not-human-reviewed-"
                    "operator-platform-binding"
                ),
            }
        )
    return _ValidatedInputs(
        candidate_evidence_sha256=candidate_evidence_sha256,
        registry_sha256=registry_sha256,
        stations=stations,
        official=official,
        anchor_ids=tuple(anchor_ids),
        station_audit=tuple(station_audit),
    )


def _diagnostic_leg_by_order(
    diagnostic: Mapping[str, Any],
) -> dict[int, Mapping[str, Any]]:
    if (
        diagnostic.get("schema_version") != 1
        or diagnostic.get("policy_version")
        != ALL_NINE_DIAGNOSTIC_POLICY_VERSION
        or diagnostic.get("release_state")
        != "diagnostic-only-no-path-selected"
    ):
        _fail("Hull all-nine diagnostic identity changed.")
    summary = _require_mapping(
        diagnostic.get("summary"), field="all-nine diagnostic summary"
    )
    if not (
        summary.get("leg_count") == 9
        and summary.get("unique_count") == 7
        and summary.get("ambiguous_count") == 2
        and summary.get("disconnected_count") == 0
        and summary.get("all_nine_gates_passed") is False
        and summary.get("invented_connector_count") == 0
        and summary.get("proximity_join_count") == 0
    ):
        _fail("Hull all-nine diagnostic summary changed.")
    claim = _require_mapping(
        diagnostic.get("claim_boundary"), field="all-nine claim_boundary"
    )
    if not (
        claim.get("diagnostic_candidates_only") is True
        and claim.get("selected_path_emitted") is False
        and claim.get("operator_track_or_platform_binding_reviewed") is False
        and claim.get("exact_operational_track_claimed") is False
    ):
        _fail("Hull all-nine diagnostic claim boundary was weakened.")
    result: dict[int, Mapping[str, Any]] = {}
    for raw in _require_sequence(diagnostic.get("legs"), field="diagnostic legs"):
        record = _require_mapping(raw, field="diagnostic leg")
        order = record.get("order")
        if isinstance(order, int) and not isinstance(order, bool):
            result[order] = record
    if set(result) != set(range(9)):
        _fail("Hull all-nine diagnostic must contain exactly legs 0 through 8.")
    return result


def _comparison_by_leg(
    comparison: Mapping[str, Any],
    *,
    graph: OsmRailGraph,
    all_nine_file_sha256: str,
) -> dict[str, Mapping[str, Any]]:
    if (
        comparison.get("schema_version") != 1
        or comparison.get("policy_version") != COMPARISON_POLICY_VERSION
        or comparison.get("release_state")
        != "read-only-comparison-no-path-selected"
    ):
        _fail("Hull ambiguity comparison identity changed.")
    binding = _require_mapping(
        comparison.get("source_bindings"), field="comparison source_bindings"
    )
    if not (
        binding.get("osm_pbf_sha256") == graph.source.sha256
        and binding.get("rail_graph_sha256") == graph.graph_sha256
        and binding.get("all_nine_diagnostic_sha256")
        == all_nine_file_sha256
    ):
        _fail("Hull ambiguity comparison source binding changed.")
    gate = _require_mapping(
        comparison.get("acceptance_gate"), field="comparison acceptance_gate"
    )
    required_gate_keys = {
        "renderer_geometry_tolerance_mm",
        "owned_route_pen_nib_mm",
        "maximum_nib_fraction",
        "maximum_allowed_paper_separation_mm",
        "all_enumerated_candidates_below_geometry_tolerance",
        "all_enumerated_candidates_below_nib_fraction",
        "candidate_selection_performed",
    }
    numeric_gate_values = {
        "renderer_geometry_tolerance_mm": RENDERER_GEOMETRY_TOLERANCE_MM,
        "owned_route_pen_nib_mm": OWNED_ROUTE_NIB_MM,
        "maximum_nib_fraction": MAXIMUM_NIB_FRACTION,
        "maximum_allowed_paper_separation_mm": min(
            RENDERER_GEOMETRY_TOLERANCE_MM,
            OWNED_ROUTE_NIB_MM * MAXIMUM_NIB_FRACTION,
        ),
    }
    numeric_gate_matches = True
    try:
        numeric_gate_matches = all(
            isclose(
                float(gate[key]),
                expected,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            for key, expected in numeric_gate_values.items()
        )
    except (KeyError, TypeError, ValueError):
        numeric_gate_matches = False
    if not (
        set(gate) == required_gate_keys
        and numeric_gate_matches
        and gate.get("all_enumerated_candidates_below_geometry_tolerance")
        is True
        and gate.get("all_enumerated_candidates_below_nib_fraction") is True
        and gate.get("candidate_selection_performed") is False
    ):
        _fail("Hull ambiguity comparison acceptance gate changed.")
    claim = _require_mapping(
        comparison.get("claim_boundary"), field="comparison claim_boundary"
    )
    if not (
        claim.get("read_only_comparison") is True
        and claim.get("candidate_selected") is False
        and claim.get("geometry_emitted") is False
        and claim.get("operational_track_claimed") is False
    ):
        _fail("Hull ambiguity comparison claim boundary was weakened.")
    result: dict[str, Mapping[str, Any]] = {}
    for raw in _require_sequence(
        comparison.get("comparisons"), field="comparison records"
    ):
        record = _require_mapping(raw, field="comparison record")
        leg = record.get("leg")
        if isinstance(leg, str):
            result[leg] = record
    if set(result) != {
        "LONDON KINGS CROSS–GRANTHAM",
        "DONCASTER–SELBY",
    }:
        _fail("Hull ambiguity comparison must contain exactly two blocked legs.")
    return result


def _equivalence_comparison_by_leg(
    comparison: Mapping[str, Any],
    *,
    graph: OsmRailGraph,
    all_nine_file_sha256: str,
    legacy_comparison_file_sha256: str,
) -> dict[str, Mapping[str, Any]]:
    if (
        comparison.get("schema_version") != 3
        or comparison.get("policy_version")
        != EQUIVALENCE_COMPARISON_POLICY_VERSION
        or comparison.get("release_state")
        != (
            "read-only-exhaustive-equivalence-class-comparison-no-operational-"
            "track-selection"
        )
    ):
        _fail("Hull equivalence comparison identity changed.")
    recorded_digest = comparison.get("ordered_evidence_sha256")
    payload = dict(comparison)
    payload.pop("ordered_evidence_sha256", None)
    if (
        not isinstance(recorded_digest, str)
        or _sha256_document(payload) != recorded_digest
    ):
        _fail("Hull equivalence comparison canonical evidence digest changed.")
    binding = _require_mapping(
        comparison.get("source_bindings"),
        field="equivalence comparison source_bindings",
    )
    if not (
        binding.get("osm_pbf_sha256") == graph.source.sha256
        and binding.get("rail_graph_sha256") == graph.graph_sha256
        and binding.get("all_nine_diagnostic_sha256")
        == all_nine_file_sha256
        and binding.get("legacy_pair_comparison_sha256")
        == legacy_comparison_file_sha256
    ):
        _fail("Hull equivalence comparison source binding changed.")
    gate = _require_mapping(
        comparison.get("acceptance_gate"),
        field="equivalence comparison acceptance_gate",
    )
    required_gate_keys = {
        "renderer_geometry_tolerance_mm",
        "owned_route_pen_nib_mm",
        "maximum_nib_fraction",
        "maximum_allowed_paper_separation_mm",
        "policy_max_candidate_count_per_leg",
        "all_equivalence_classes_exhaustive",
        "all_candidates_below_reference_geometry_tolerance",
        "all_candidates_below_reference_nib_fraction",
        "all_candidate_pairs_below_geometry_tolerance",
        "all_candidate_pairs_below_nib_fraction",
        "all_full_paths_zero_ineligible_or_excluded_edges",
        "all_candidate_specific_tag_counts_disclosed",
        "minimum_tuple_cartographic_reference_selected",
        "zero_addition_candidate_required",
        "selected_nonzero_candidate_specific_count_disclosed",
        "operational_track_selection_performed",
    }
    numeric_values = {
        "renderer_geometry_tolerance_mm": RENDERER_GEOMETRY_TOLERANCE_MM,
        "owned_route_pen_nib_mm": OWNED_ROUTE_NIB_MM,
        "maximum_nib_fraction": MAXIMUM_NIB_FRACTION,
        "maximum_allowed_paper_separation_mm": min(
            RENDERER_GEOMETRY_TOLERANCE_MM,
            OWNED_ROUTE_NIB_MM * MAXIMUM_NIB_FRACTION,
        ),
    }
    try:
        numeric_matches = all(
            isclose(
                float(gate[key]),
                expected,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            for key, expected in numeric_values.items()
        )
    except (KeyError, TypeError, ValueError):
        numeric_matches = False
    if not (
        set(gate) == required_gate_keys
        and numeric_matches
        and gate.get("policy_max_candidate_count_per_leg")
        == POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT
        and gate.get("all_equivalence_classes_exhaustive") is True
        and gate.get("all_candidates_below_reference_geometry_tolerance")
        is True
        and gate.get("all_candidates_below_reference_nib_fraction") is True
        and gate.get("all_candidate_pairs_below_geometry_tolerance") is True
        and gate.get("all_candidate_pairs_below_nib_fraction") is True
        and gate.get("all_full_paths_zero_ineligible_or_excluded_edges") is True
        and gate.get("all_candidate_specific_tag_counts_disclosed") is True
        and gate.get("minimum_tuple_cartographic_reference_selected") is True
        and gate.get("zero_addition_candidate_required") is False
        and gate.get("selected_nonzero_candidate_specific_count_disclosed")
        is True
        and gate.get("operational_track_selection_performed") is False
    ):
        _fail("Hull equivalence comparison acceptance gate changed.")
    claim = _require_mapping(
        comparison.get("claim_boundary"),
        field="equivalence comparison claim_boundary",
    )
    if not (
        claim.get("read_only_comparison") is True
        and claim.get("cartographic_reference_determined") is True
        and claim.get("cartographic_reference_is_operational_track_selection")
        is False
        and claim.get(
            "candidate_specific_service_or_crossover_counts_used_only_for_cartographic_ranking"
        )
        is True
        and claim.get("nonzero_selected_count_is_operational_track_evidence")
        is False
        and claim.get("geometry_emitted") is False
        and claim.get("operator_track_or_platform_binding_reviewed") is False
        and claim.get("exact_operational_track_claimed") is False
        and claim.get("exact_edge_corridor_policy_still_blocked") is True
    ):
        _fail("Hull equivalence comparison claim boundary was weakened.")
    result: dict[str, Mapping[str, Any]] = {}
    for raw in _require_sequence(
        comparison.get("comparisons"), field="equivalence comparison records"
    ):
        record = _require_mapping(raw, field="equivalence comparison record")
        leg = record.get("leg")
        if not isinstance(leg, str) or leg in result:
            _fail("Hull equivalence comparison repeats or omits a leg name.")
        result[leg] = record
    if set(result) != {
        "LONDON KINGS CROSS–GRANTHAM",
        "DONCASTER–SELBY",
    }:
        _fail("Hull equivalence comparison must contain exactly two blocked legs.")
    return result


def _validate_exact_blocker(
    blocker: Mapping[str, Any],
    *,
    all_nine_file_sha256: str,
) -> None:
    if (
        blocker.get("release_state") != "blocked-no-corridor-contract-emitted"
        or blocker.get("policy_version")
        != ALL_NINE_DIAGNOSTIC_POLICY_VERSION
    ):
        _fail("Hull exact corridor blocker identity changed.")
    output = _require_mapping(blocker.get("output_state"), field="blocker output")
    if not (
        output.get("standalone_corridor_contract_written") is False
        and output.get("mixed_25_line_overview_written") is False
        and output.get("existing_osm_operator_relation_overview_line_count") == 24
    ):
        _fail("Hull exact corridor blocker no longer preserves the 24-line state.")
    diagnostic = _require_mapping(
        blocker.get("all_nine_diagnostic"), field="blocker all_nine_diagnostic"
    )
    if not (
        diagnostic.get("sha256") == all_nine_file_sha256
        and diagnostic.get("ambiguous_count") == 2
        and diagnostic.get("selected_path_emitted") is False
    ):
        _fail("Hull exact corridor blocker diagnostic binding changed.")


def _candidate_summary(candidate: RailPathCandidateEvidence) -> dict[str, Any]:
    return {
        "path_sha256": candidate.path_sha256,
        "length_m": candidate.total_length_m,
        "edge_count": len(candidate.edge_ids),
        "node_count": len(candidate.node_ids),
    }


def _eligible_edge_ids(graph: OsmRailGraph) -> frozenset[str]:
    result: set[str] = set()
    for edge_id, edge in graph.edges.items():
        tags = dict(edge.tags)
        railway = str(tags.get("railway", "")).strip().casefold()
        service = str(tags.get("service", "")).strip().casefold()
        if (
            railway == ELIGIBLE_CORRIDOR_RAILWAY_VALUE
            and service not in EXCLUDED_CORRIDOR_SERVICE_VALUES
        ):
            result.add(edge_id)
    return frozenset(result)


def _full_path_tag_audit(
    graph: OsmRailGraph,
    candidate: RailPathCandidateEvidence,
    *,
    allowed_edge_ids: frozenset[str],
) -> dict[str, Any]:
    ineligible_edge_ids: list[str] = []
    excluded_service_value_edge_ids: list[str] = []
    service_tagged_edge_ids: list[str] = []
    crossover_edge_ids: list[str] = []
    service_value_counts: Counter[str] = Counter()
    for edge_id in candidate.edge_ids:
        tags = dict(graph.edges[edge_id].tags)
        service = str(tags.get("service", "")).strip().casefold()
        if edge_id not in allowed_edge_ids:
            ineligible_edge_ids.append(edge_id)
        if service:
            service_tagged_edge_ids.append(edge_id)
            service_value_counts[service] += 1
        if service in EXCLUDED_CORRIDOR_SERVICE_VALUES:
            excluded_service_value_edge_ids.append(edge_id)
        if service == "crossover":
            crossover_edge_ids.append(edge_id)
    return {
        "rule": (
            "full-path tag inventory; zero ineligible and zero yard/siding/spur "
            "are absolute, while nonempty service/crossover tags are classified "
            "against the exact class-common intersection before selection"
        ),
        "ineligible_edge_ids": sorted(ineligible_edge_ids),
        "ineligible_edge_count": len(ineligible_edge_ids),
        "excluded_service_values": sorted(EXCLUDED_CORRIDOR_SERVICE_VALUES),
        "excluded_service_value_edge_ids": sorted(
            excluded_service_value_edge_ids
        ),
        "excluded_service_value_edge_count": len(
            excluded_service_value_edge_ids
        ),
        "service_tagged_edge_ids": sorted(service_tagged_edge_ids),
        "service_tagged_edge_count": len(service_tagged_edge_ids),
        "service_value_edge_counts": dict(sorted(service_value_counts.items())),
        "crossover_edge_ids": sorted(crossover_edge_ids),
        "crossover_edge_count": len(crossover_edge_ids),
        "eligible_and_excluded_edge_gate_passed": (
            not ineligible_edge_ids and not excluded_service_value_edge_ids
        ),
    }


def _class_relative_tag_selection_audit(
    graph: OsmRailGraph,
    candidates: Sequence[RailPathCandidateEvidence],
    *,
    allowed_edge_ids: frozenset[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if len(candidates) < 2:
        _fail("Hull class-relative tag audit needs at least two candidates.")
    common_edge_ids = set(candidates[0].edge_ids)
    for candidate in candidates[1:]:
        common_edge_ids.intersection_update(candidate.edge_ids)
    common_sorted = sorted(common_edge_ids)

    def tag_lists(edge_ids: Sequence[str]) -> dict[str, Any]:
        ineligible: list[str] = []
        excluded: list[str] = []
        service_tagged: list[str] = []
        crossover: list[str] = []
        service_values: Counter[str] = Counter()
        for edge_id in edge_ids:
            tags = dict(graph.edges[edge_id].tags)
            service = str(tags.get("service", "")).strip().casefold()
            if edge_id not in allowed_edge_ids:
                ineligible.append(edge_id)
            if service:
                service_tagged.append(edge_id)
                service_values[service] += 1
            if service in EXCLUDED_CORRIDOR_SERVICE_VALUES:
                excluded.append(edge_id)
            if service == "crossover":
                crossover.append(edge_id)
        return {
            "ineligible_edge_ids": sorted(ineligible),
            "ineligible_edge_count": len(ineligible),
            "excluded_service_values": sorted(
                EXCLUDED_CORRIDOR_SERVICE_VALUES
            ),
            "excluded_service_value_edge_ids": sorted(excluded),
            "excluded_service_value_edge_count": len(excluded),
            "service_tagged_edge_ids": sorted(service_tagged),
            "service_tagged_edge_count": len(service_tagged),
            "service_value_edge_counts": dict(sorted(service_values.items())),
            "crossover_edge_ids": sorted(crossover),
            "crossover_edge_count": len(crossover),
        }

    common_tags = tag_lists(common_sorted)
    common_audit = {
        "edge_ids": common_sorted,
        "edge_ids_sha256": _sha256_document(common_sorted),
        "edge_count": len(common_sorted),
        "interpretation": (
            "Exact edge-ID intersection shared by every fully enumerated equal-"
            "shortest candidate; service/crossover tags here are unavoidable "
            "within this cartographic equivalence class, not operator-use proof."
        ),
        **common_tags,
    }
    by_path: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_specific = sorted(set(candidate.edge_ids).difference(common_edge_ids))
        candidate_specific_tags = tag_lists(candidate_specific)
        full_path_tags = _full_path_tag_audit(
            graph,
            candidate,
            allowed_edge_ids=allowed_edge_ids,
        )
        service_or_crossover_ids = sorted(
            set(candidate_specific_tags["service_tagged_edge_ids"]).union(
                candidate_specific_tags["crossover_edge_ids"]
            )
        )
        service_or_crossover_count = len(service_or_crossover_ids)
        crossover_count = int(candidate_specific_tags["crossover_edge_count"])
        service_count = int(candidate_specific_tags["service_tagged_edge_count"])
        ordered_values: list[int | float | str] = [
            service_or_crossover_count,
            crossover_count,
            service_count,
            candidate.total_length_m,
            candidate.path_sha256,
        ]
        clean = service_or_crossover_count == 0
        by_path[candidate.path_sha256] = {
            "rule": CARTOGRAPHIC_SELECTION_RULE,
            "full_path_tag_audit": full_path_tags,
            "candidate_specific_edge_ids": candidate_specific,
            "candidate_specific_edge_ids_sha256": _sha256_document(
                candidate_specific
            ),
            "candidate_specific_edge_count": len(candidate_specific),
            "candidate_specific_tag_audit": candidate_specific_tags,
            "candidate_specific_service_or_crossover_union_edge_ids": (
                service_or_crossover_ids
            ),
            "candidate_specific_service_or_crossover_union_edge_count": (
                service_or_crossover_count
            ),
            "candidate_specific_crossover_edge_count": crossover_count,
            "candidate_specific_nonempty_service_edge_count": service_count,
            "adds_zero_service_or_crossover_edges_outside_common": clean,
            "clean_under_prior_zero_addition_rule": clean,
            "cartographic_selection_rank": {
                "ordered_component_names": list(
                    CARTOGRAPHIC_SELECTION_COMPONENTS
                ),
                "ordered_values": ordered_values,
                "candidate_specific_service_or_crossover_union_edge_count": (
                    service_or_crossover_count
                ),
                "candidate_specific_crossover_edge_count": crossover_count,
                "candidate_specific_nonempty_service_edge_count": service_count,
                "path_length_m": candidate.total_length_m,
                "path_sha256": candidate.path_sha256,
            },
        }
    return common_audit, by_path


def _cartographic_selection_key(
    candidate: RailPathCandidateEvidence,
    class_relative_selection: Mapping[str, Any],
) -> tuple[int, int, int, float, str]:
    """Return the sealed cartographic-only rank; never an operator-use rank."""

    rank = _require_mapping(
        class_relative_selection.get("cartographic_selection_rank"),
        field=f"cartographic selection rank for {candidate.path_sha256}",
    )
    if rank.get("ordered_component_names") != list(
        CARTOGRAPHIC_SELECTION_COMPONENTS
    ):
        _fail("Hull cartographic selection rank component order changed.")
    key = (
        int(rank["candidate_specific_service_or_crossover_union_edge_count"]),
        int(rank["candidate_specific_crossover_edge_count"]),
        int(rank["candidate_specific_nonempty_service_edge_count"]),
        float(rank["path_length_m"]),
        str(rank["path_sha256"]),
    )
    if (
        rank.get("ordered_values") != list(key)
        or key[3] != candidate.total_length_m
        or key[4] != candidate.path_sha256
    ):
        _fail("Hull cartographic selection rank payload changed.")
    return key


def _line(candidate: RailPathCandidateEvidence, graph: OsmRailGraph) -> LineString:
    return LineString(
        [
            _WGS84_TO_OSGB.transform(
                graph.nodes[node_id].lon,
                graph.nodes[node_id].lat,
            )
            for node_id in candidate.node_ids
        ]
    )


def _hausdorff_metrics(
    first: RailPathCandidateEvidence,
    second: RailPathCandidateEvidence,
    graph: OsmRailGraph,
    *,
    renderer_metres_per_mm: float,
) -> dict[str, Any]:
    maximum_m = float(
        hausdorff_distance(
            _line(first, graph),
            _line(second, graph),
            densify=0.1,
        )
    )
    maximum_mm = maximum_m / renderer_metres_per_mm
    maximum_nib_fraction = maximum_mm / OWNED_ROUTE_NIB_MM
    return {
        "densified_hausdorff_max_m": maximum_m,
        "renderer_paper_max_mm": maximum_mm,
        "owned_0_4_mm_nib_fraction": maximum_nib_fraction,
        "below_renderer_geometry_tolerance": (
            maximum_mm <= RENDERER_GEOMETRY_TOLERANCE_MM
        ),
        "below_maximum_nib_fraction": (
            maximum_nib_fraction <= MAXIMUM_NIB_FRACTION
        ),
    }


def _pairwise_maximum_separation(
    graph: OsmRailGraph,
    candidates: Sequence[RailPathCandidateEvidence],
    *,
    renderer_metres_per_mm: float,
) -> dict[str, Any]:
    maximum_m = -1.0
    worst_pair: tuple[str, str] | None = None
    lines = [(item, _line(item, graph)) for item in candidates]
    for first_index, (first, first_line) in enumerate(lines):
        for second, second_line in lines[first_index + 1 :]:
            separation_m = float(
                hausdorff_distance(first_line, second_line, densify=0.1)
            )
            pair = (
                min(first.path_sha256, second.path_sha256),
                max(first.path_sha256, second.path_sha256),
            )
            if separation_m > maximum_m or (
                isclose(separation_m, maximum_m, rel_tol=0.0, abs_tol=1e-9)
                and (worst_pair is None or pair < worst_pair)
            ):
                maximum_m = separation_m
                worst_pair = pair
    if worst_pair is None:
        _fail("Hull equivalence class needs at least two paths for pairwise proof.")
    maximum_mm = maximum_m / renderer_metres_per_mm
    maximum_nib_fraction = maximum_mm / OWNED_ROUTE_NIB_MM
    return {
        "method": "OSGB EPSG:27700; Shapely densified discrete Hausdorff 0.1",
        "candidate_pair_count": len(candidates) * (len(candidates) - 1) // 2,
        "worst_pair_path_sha256": list(worst_pair),
        "densified_hausdorff_max_m": maximum_m,
        "renderer_paper_max_mm": maximum_mm,
        "owned_0_4_mm_nib_fraction": maximum_nib_fraction,
        "below_renderer_geometry_tolerance": (
            maximum_mm <= RENDERER_GEOMETRY_TOLERANCE_MM
        ),
        "below_maximum_nib_fraction": (
            maximum_nib_fraction <= MAXIMUM_NIB_FRACTION
        ),
    }


def _renderer_metres_per_mm(
    graph: OsmRailGraph,
    all_candidates: Sequence[RailPathCandidateEvidence],
    *,
    format_id: str,
) -> tuple[float, dict[str, Any]]:
    points = [
        (graph.nodes[node_id].lon, graph.nodes[node_id].lat)
        for candidate in all_candidates
        for node_id in candidate.node_ids
    ]
    if not points:
        _fail("Hull scale-aware corridor has no candidate geometry.")
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    bounds = BoundingBox(min(lons), min(lats), max(lons), max(lats))
    plate = PlateContext.load(format_id)
    margin_mm = max(
        MINIMUM_FURNITURE_MARGIN_MM,
        min(plate.field.width, plate.field.height)
        * DEFAULT_FURNITURE_MARGIN_FRACTION,
    )
    viewport = plate.field.inset(margin_mm)
    extent = named_operator_projection_extent(
        network_kind="national-operator",
        route_bounds=bounds,
        target_metric_aspect=viewport.width / viewport.height,
        padding_fraction=0.0,
    )
    metres_per_mm = (
        extent.expanded_bounds.approximate_height_m / viewport.height
    )
    return metres_per_mm, {
        "format_id": format_id,
        "map_field_mm": plate.field.as_dict(),
        "furniture_margin_mm": margin_mm,
        "geographic_viewport_mm": viewport.as_dict(),
        "route_bbox_wgs84": {
            "west": bounds.west,
            "south": bounds.south,
            "east": bounds.east,
            "north": bounds.north,
        },
        "renderer_extent": extent.as_dict(),
        "renderer_metres_per_mm": metres_per_mm,
        "renderer_representative_fraction": metres_per_mm * 1000.0,
    }


def compile_hull_scale_aware_cartographic_corridor(
    graph: OsmRailGraph,
    candidate: Mapping[str, Any],
    scope: Mapping[str, Any],
    all_nine_diagnostic: Mapping[str, Any],
    legacy_comparison: Mapping[str, Any],
    equivalence_comparison: Mapping[str, Any],
    exact_blocker: Mapping[str, Any],
    *,
    candidate_file_sha256: str,
    scope_file_sha256: str,
    official_pdf_sha256: str,
    naptan_sha256: str,
    all_nine_file_sha256: str,
    legacy_comparison_file_sha256: str,
    equivalence_comparison_file_sha256: str,
    exact_blocker_file_sha256: str,
    retrieved_at: str = "2026-08-08",
    max_anchor_distance_m: float = MAX_MECHANICAL_ANCHOR_DISTANCE_M,
    max_leg_detour_ratio: float = MAX_LEG_DETOUR_RATIO,
) -> tuple[TransitNetwork, dict[str, Any]]:
    """Compile one scale-aware visual corridor or fail closed."""

    inputs = _validate_inputs(
        graph,
        candidate,
        scope,
        official_pdf_sha256=official_pdf_sha256,
        naptan_sha256=naptan_sha256,
        max_anchor_distance_m=max_anchor_distance_m,
    )
    diagnostic_by_order = _diagnostic_leg_by_order(all_nine_diagnostic)
    legacy_comparison_by_leg = _comparison_by_leg(
        legacy_comparison,
        graph=graph,
        all_nine_file_sha256=all_nine_file_sha256,
    )
    equivalence_comparison_by_leg = _equivalence_comparison_by_leg(
        equivalence_comparison,
        graph=graph,
        all_nine_file_sha256=all_nine_file_sha256,
        legacy_comparison_file_sha256=legacy_comparison_file_sha256,
    )
    _validate_exact_blocker(
        exact_blocker,
        all_nine_file_sha256=all_nine_file_sha256,
    )
    product = OPERATOR_REGISTRY.by_key[HULL_OPERATOR_CODE]
    if product.format_id != "a3-landscape":
        _fail("Hull scale-aware comparison is sealed to A3 landscape.")

    allowed_edge_ids = _eligible_edge_ids(graph)
    if not allowed_edge_ids:
        _fail("Hull physical graph has no eligible standard-rail corridor edges.")
    selected_routes: list[RailRoute | RailPathCandidateEvidence] = []
    all_candidates: list[RailPathCandidateEvidence] = []
    pending_ambiguities: list[
        tuple[
            int,
            Mapping[str, Any],
            Mapping[str, Any],
            tuple[RailPathCandidateEvidence, ...],
            int,
        ]
    ] = []
    leg_audit: list[dict[str, Any]] = []
    for index, (first, second) in enumerate(
        zip(inputs.stations, inputs.stations[1:])
    ):
        try:
            route = graph.shortest_path(
                inputs.anchor_ids[index],
                inputs.anchor_ids[index + 1],
                allowed_edge_ids=allowed_edge_ids,
                candidate_enumeration_limit=(
                    EQUIVALENCE_CANDIDATE_ENUMERATION_LIMIT
                ),
            )
        except RailGraphAmbiguityError as exc:
            if index not in AMBIGUOUS_LEG_ORDERS:
                raise RailGraphAmbiguityError(
                    f"Unexpected Hull scale-aware ambiguity at "
                    f"{first['location']}–{second['location']}.",
                    exc.evidence,
                ) from exc
            candidates = exc.evidence.candidates
            if not (
                exc.evidence.candidates_exhaustive
                and exc.evidence.candidate_count_exact is not None
                and 2
                <= exc.evidence.candidate_count_exact
                <= POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT
                and exc.evidence.candidate_count_lower_bound
                == exc.evidence.candidate_count_exact
                and len(candidates) == exc.evidence.candidate_count_exact
            ):
                _fail(
                    "Hull scale-aware policy requires an exhaustively proven "
                    "equal-shortest equivalence class of no more than 256 paths "
                    f"at {first['location']}–{second['location']}: lower_bound="
                    f"{exc.evidence.candidate_count_lower_bound}, exact="
                    f"{exc.evidence.candidate_count_exact}, exhaustive="
                    f"{exc.evidence.candidates_exhaustive}, enumeration_limit="
                    f"{exc.evidence.candidate_enumeration_limit}."
                )
            all_candidates.extend(candidates)
            pending_ambiguities.append(
                (
                    index,
                    first,
                    second,
                    candidates,
                    exc.evidence.candidate_enumeration_limit,
                )
            )
            selected_routes.append(candidates[0])
            continue
        except RailGraphRoutingError as exc:
            raise type(exc)(
                f"Hull scale-aware corridor leg {first['location']}–"
                f"{second['location']} failed closed: {exc}",
                exc.evidence,
            ) from exc
        if index in AMBIGUOUS_LEG_ORDERS:
            _fail("A sealed Hull ambiguous leg unexpectedly became unique.")
        all_candidates.extend(route.evidence.candidates)
        selected_routes.append(route)

    metres_per_mm, scale_audit = _renderer_metres_per_mm(
        graph,
        all_candidates,
        format_id=product.format_id,
    )
    sealed_scale = _require_mapping(
        equivalence_comparison.get("expected_house_scale"),
        field="equivalence comparison house scale",
    )
    if not isclose(
        float(sealed_scale.get("renderer_metres_per_mm", 0.0)),
        metres_per_mm,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        _fail("Hull ambiguity comparison renderer scale changed.")

    ambiguity_audit: list[dict[str, Any]] = []
    for (
        index,
        first,
        second,
        candidates,
        candidate_enumeration_limit,
    ) in pending_ambiguities:
        leg_name = f"{first['location']}–{second['location']}"
        legacy_sealed = legacy_comparison_by_leg[leg_name]
        sealed = equivalence_comparison_by_leg[leg_name]
        actual_hashes = {item.path_sha256 for item in candidates}
        diagnostic = diagnostic_by_order[index]
        diagnostic_hashes = {
            str(_require_mapping(item, field="diagnostic candidate").get("path_sha256"))
            for item in _require_sequence(
                diagnostic.get("candidates"), field="diagnostic candidates"
            )
        }
        if (
            diagnostic.get("path_status") != "ambiguous"
            or diagnostic.get("start_osm_node_id") != inputs.anchor_ids[index]
            or diagnostic.get("end_osm_node_id") != inputs.anchor_ids[index + 1]
            or not diagnostic_hashes.issubset(actual_hashes)
        ):
            _fail(f"Hull all-nine evidence changed for ambiguous leg {leg_name}.")
        legacy_hashes = {
            str(
                _require_mapping(
                    legacy_sealed.get(key), field=f"legacy {leg_name} {key}"
                ).get("path_sha256")
            )
            for key in ("candidate_1", "candidate_2")
        }
        if not legacy_hashes.issubset(diagnostic_hashes):
            _fail(f"Hull legacy witness hashes changed for {leg_name}.")
        raw_legacy_witnesses = _require_sequence(
            sealed.get("legacy_witness_path_sha256"),
            field=f"{leg_name} legacy witnesses",
        )
        if set(str(value) for value in raw_legacy_witnesses) != legacy_hashes:
            _fail(f"Hull equivalence legacy witness binding changed for {leg_name}.")
        sealed_equivalence = _require_mapping(
            sealed.get("equivalence_class"),
            field=f"{leg_name} equivalence_class",
        )
        if not (
            sealed.get("order") == index
            and sealed.get("start_osm_node_id") == inputs.anchor_ids[index]
            and sealed.get("end_osm_node_id") == inputs.anchor_ids[index + 1]
            and sealed_equivalence.get("candidate_count_exact") == len(candidates)
            and sealed_equivalence.get("candidate_count_lower_bound")
            == len(candidates)
            and sealed_equivalence.get("candidates_exhaustive") is True
            and sealed_equivalence.get("candidate_enumeration_limit")
            == candidate_enumeration_limit
            and sealed_equivalence.get("policy_max_candidate_count")
            == POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT
            and sealed_equivalence.get("overflow") is False
        ):
            _fail(f"Hull equivalence class proof changed for {leg_name}.")
        sealed_candidate_records: dict[str, Mapping[str, Any]] = {}
        for raw in _require_sequence(
            sealed.get("candidates"), field=f"{leg_name} candidates"
        ):
            record = _require_mapping(raw, field=f"{leg_name} candidate")
            path_hash = record.get("path_sha256")
            if not isinstance(path_hash, str) or path_hash in sealed_candidate_records:
                _fail(f"Hull equivalence candidates repeat a hash for {leg_name}.")
            sealed_candidate_records[path_hash] = record
        if set(sealed_candidate_records) != actual_hashes:
            _fail(f"Hull equivalence candidate hashes changed for {leg_name}.")
        direct_m = _haversine_m(
            (float(first["lon"]), float(first["lat"])),
            (float(second["lon"]), float(second["lat"])),
        )
        if not isclose(
            float(sealed.get("station_great_circle_distance_m", -1.0)),
            direct_m,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            _fail(f"Hull equivalence station distance changed for {leg_name}.")
        for item in candidates:
            if item.node_ids[0] != inputs.anchor_ids[index] or item.node_ids[-1] != (
                inputs.anchor_ids[index + 1]
            ):
                _fail(f"Hull candidate endpoints changed for {leg_name}.")
            unknown = sorted(set(item.edge_ids).difference(allowed_edge_ids))
            if unknown:
                _fail(f"Hull candidate uses ineligible rail edges at {leg_name}.")
            detour = item.total_length_m / direct_m
            if detour > max_leg_detour_ratio:
                _fail(f"Hull candidate detour gate failed at {leg_name}.")
        common_edge_audit, selection_by_path = (
            _class_relative_tag_selection_audit(
                graph,
                candidates,
                allowed_edge_ids=allowed_edge_ids,
            )
        )
        for path_hash, selection in selection_by_path.items():
            full_path = _require_mapping(
                selection.get("full_path_tag_audit"),
                field=f"{leg_name} full path tag audit",
            )
            if (
                full_path.get("ineligible_edge_count") != 0
                or full_path.get("excluded_service_value_edge_count") != 0
            ):
                _fail(
                    f"Hull candidate {path_hash} contains an ineligible or "
                    f"excluded yard/siding/spur edge at {leg_name}."
                )
        candidate_hashes = sorted(item.path_sha256 for item in candidates)
        clean_hashes = sorted(
            item.path_sha256
            for item in candidates
            if selection_by_path[item.path_sha256][
                "clean_under_prior_zero_addition_rule"
            ]
        )
        selected = min(
            candidates,
            key=lambda item: _cartographic_selection_key(
                item,
                selection_by_path[item.path_sha256],
            ),
        )
        selected_relative = selection_by_path[selected.path_sha256]
        selected_key = _cartographic_selection_key(
            selected,
            selected_relative,
        )
        selected_nonzero = selected_key[0] > 0
        sealed_common = _require_mapping(
            sealed.get("class_common_edge_intersection"),
            field=f"{leg_name} class common edge intersection",
        )
        if dict(sealed_common) != common_edge_audit:
            _fail(f"Hull class-common edge proof changed for {leg_name}.")
        if sealed.get("candidate_path_sha256") != candidate_hashes:
            _fail(f"Hull candidate disclosure changed for {leg_name}.")
        if (
            sealed.get("clean_zero_addition_candidate_path_sha256")
            != clean_hashes
            or sealed.get("no_zero_addition_candidate_available")
            is not (not clean_hashes)
        ):
            _fail(f"Hull zero-addition disclosure changed for {leg_name}.")
        sealed_reference = _require_mapping(
            sealed.get("cartographic_reference"),
            field=f"{leg_name} cartographic_reference",
        )
        if not (
            sealed_reference.get("path_sha256") == selected.path_sha256
            and sealed_reference.get("selection_rule")
            == CARTOGRAPHIC_SELECTION_RULE
            and sealed_reference.get("minimum_tuple") == list(selected_key)
            and sealed_reference.get(
                "selected_candidate_specific_service_or_crossover_union_edge_count"
            )
            == selected_key[0]
            and sealed_reference.get(
                "selected_candidate_specific_crossover_edge_count"
            )
            == selected_key[1]
            and sealed_reference.get(
                "selected_candidate_specific_nonempty_service_edge_count"
            )
            == selected_key[2]
            and sealed_reference.get(
                "selected_has_nonzero_candidate_specific_service_or_crossover_count"
            )
            is selected_nonzero
            and sealed_reference.get("operator_track_selection_claimed") is False
        ):
            _fail(f"Hull cartographic reference proof changed for {leg_name}.")
        candidate_records: list[dict[str, Any]] = []
        for item in candidates:
            class_relative_selection = selection_by_path[item.path_sha256]
            detour = item.total_length_m / direct_m
            metrics = _hausdorff_metrics(
                item,
                selected,
                graph,
                renderer_metres_per_mm=metres_per_mm,
            )
            if not (
                metrics["below_renderer_geometry_tolerance"]
                and metrics["below_maximum_nib_fraction"]
            ):
                _fail(
                    f"Hull candidate exceeds the reference paper-space gate at "
                    f"{leg_name}."
                )
            actual_record = {
                **_candidate_summary(item),
                "same_anchor_pair": True,
                "eligible_physical_edge_count": len(item.edge_ids),
                "ineligible_physical_edge_count": 0,
                "detour_ratio": detour,
                "detour_gate_passed": True,
                "class_relative_tag_selection": class_relative_selection,
                "reference_separation": metrics,
            }
            sealed_record = sealed_candidate_records[item.path_sha256]
            sealed_selection = _require_mapping(
                sealed_record.get("class_relative_tag_selection"),
                field=f"{leg_name} sealed class-relative tag selection",
            )
            sealed_metrics = _require_mapping(
                sealed_record.get("reference_separation"),
                field=f"{leg_name} sealed reference separation",
            )
            if not (
                sealed_record.get("length_m") is not None
                and isclose(
                    float(sealed_record["length_m"]),
                    item.total_length_m,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
                and sealed_record.get("edge_count") == len(item.edge_ids)
                and sealed_record.get("node_count") == len(item.node_ids)
                and sealed_record.get("same_anchor_pair") is True
                and sealed_record.get("eligible_physical_edge_count")
                == len(item.edge_ids)
                and sealed_record.get("ineligible_physical_edge_count") == 0
                and isclose(
                    float(sealed_record.get("detour_ratio", -1.0)),
                    detour,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                and sealed_record.get("detour_gate_passed") is True
                and dict(sealed_selection) == dict(class_relative_selection)
                and isclose(
                    float(
                        sealed_metrics.get("densified_hausdorff_max_m", -1.0)
                    ),
                    float(metrics["densified_hausdorff_max_m"]),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
                and isclose(
                    float(sealed_metrics.get("renderer_paper_max_mm", -1.0)),
                    float(metrics["renderer_paper_max_mm"]),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and isclose(
                    float(
                        sealed_metrics.get("owned_0_4_mm_nib_fraction", -1.0)
                    ),
                    float(metrics["owned_0_4_mm_nib_fraction"]),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and sealed_metrics.get("below_renderer_geometry_tolerance")
                is True
                and sealed_metrics.get("below_maximum_nib_fraction") is True
            ):
                _fail(f"Hull sealed candidate proof changed for {leg_name}.")
            candidate_records.append(actual_record)
        pairwise = _pairwise_maximum_separation(
            graph,
            candidates,
            renderer_metres_per_mm=metres_per_mm,
        )
        if not (
            pairwise["below_renderer_geometry_tolerance"]
            and pairwise["below_maximum_nib_fraction"]
        ):
            _fail(f"Hull equivalence pair exceeds paper-space gate at {leg_name}.")
        sealed_pairwise = _require_mapping(
            sealed.get("pairwise_maximum_separation"),
            field=f"{leg_name} pairwise maximum separation",
        )
        if not (
            sealed_pairwise.get("method") == pairwise["method"]
            and sealed_pairwise.get("candidate_pair_count")
            == pairwise["candidate_pair_count"]
            and sealed_pairwise.get("worst_pair_path_sha256")
            == pairwise["worst_pair_path_sha256"]
            and isclose(
                float(
                    sealed_pairwise.get("densified_hausdorff_max_m", -1.0)
                ),
                float(pairwise["densified_hausdorff_max_m"]),
                rel_tol=0.0,
                abs_tol=1e-6,
            )
            and isclose(
                float(sealed_pairwise.get("renderer_paper_max_mm", -1.0)),
                float(pairwise["renderer_paper_max_mm"]),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and isclose(
                float(
                    sealed_pairwise.get("owned_0_4_mm_nib_fraction", -1.0)
                ),
                float(pairwise["owned_0_4_mm_nib_fraction"]),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and sealed_pairwise.get("below_renderer_geometry_tolerance") is True
            and sealed_pairwise.get("below_maximum_nib_fraction") is True
            and all(
                sealed.get(key) is True
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
            and sealed.get("zero_addition_candidate_required") is False
        ):
            _fail(f"Hull sealed pairwise proof changed for {leg_name}.")
        selected_routes[index] = selected
        ambiguity_audit.append(
            {
                "order": index,
                "leg": leg_name,
                "start_osm_node_id": inputs.anchor_ids[index],
                "end_osm_node_id": inputs.anchor_ids[index + 1],
                "station_great_circle_distance_m": direct_m,
                "legacy_witness_path_sha256": sorted(legacy_hashes),
                "equivalence_class": {
                    "candidate_count_exact": len(candidates),
                    "candidate_count_lower_bound": len(candidates),
                    "candidates_exhaustive": True,
                    "candidate_enumeration_limit": candidate_enumeration_limit,
                    "policy_max_candidate_count": (
                        POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT
                    ),
                    "overflow": False,
                },
                "class_common_edge_intersection": common_edge_audit,
                "candidate_path_sha256": candidate_hashes,
                "clean_zero_addition_candidate_path_sha256": clean_hashes,
                "no_zero_addition_candidate_available": not clean_hashes,
                "cartographic_reference": {
                    "path_sha256": selected.path_sha256,
                    "selection_rule": CARTOGRAPHIC_SELECTION_RULE,
                    "minimum_tuple": list(selected_key),
                    "selected_candidate_specific_service_or_crossover_union_edge_count": selected_key[0],
                    "selected_candidate_specific_crossover_edge_count": selected_key[1],
                    "selected_candidate_specific_nonempty_service_edge_count": selected_key[2],
                    "selected_has_nonzero_candidate_specific_service_or_crossover_count": selected_nonzero,
                    "operator_track_selection_claimed": False,
                },
                "candidates": candidate_records,
                "renderer_metres_per_mm": metres_per_mm,
                "renderer_geometry_tolerance_mm": (
                    RENDERER_GEOMETRY_TOLERANCE_MM
                ),
                "owned_route_pen_nib_mm": OWNED_ROUTE_NIB_MM,
                "nib_fraction_tolerance": MAXIMUM_NIB_FRACTION,
                "pairwise_maximum_separation": pairwise,
                "all_candidates_below_reference_geometry_tolerance": True,
                "all_candidates_below_reference_nib_fraction": True,
                "all_candidate_pairs_below_geometry_tolerance": True,
                "all_candidate_pairs_below_nib_fraction": True,
                "all_full_paths_zero_ineligible_or_excluded_edges": True,
                "all_candidate_specific_tag_counts_disclosed": True,
                "minimum_tuple_cartographic_reference_selected": True,
                "zero_addition_candidate_required": False,
                "selected_nonzero_candidate_specific_count_disclosed": True,
            }
        )

    selected_candidates: list[RailPathCandidateEvidence] = []
    for selected_route in selected_routes:
        if isinstance(selected_route, RailRoute):
            selected_candidates.append(selected_route.evidence.candidates[0])
        else:
            selected_candidates.append(selected_route)
    seen_by_pattern: dict[str, set[str]] = {"core": set(), "extension": set()}
    for index, (first, second, selected) in enumerate(
        zip(inputs.stations, inputs.stations[1:], selected_candidates)
    ):
        pattern_scope = "core" if index < 7 else "extension"
        repeated = seen_by_pattern[pattern_scope].intersection(selected.edge_ids)
        if repeated:
            _fail(
                "Hull scale-aware representative repeats physical edges across "
                "advertised legs."
            )
        seen_by_pattern[pattern_scope].update(selected.edge_ids)
        direct_m = _haversine_m(
            (float(first["lon"]), float(first["lat"])),
            (float(second["lon"]), float(second["lat"])),
        )
        detour = selected.total_length_m / direct_m
        if detour > max_leg_detour_ratio:
            _fail(f"Hull selected detour gate failed at leg {index}.")
        diagnostic = diagnostic_by_order[index]
        if diagnostic.get("start_osm_node_id") != inputs.anchor_ids[index] or (
            diagnostic.get("end_osm_node_id") != inputs.anchor_ids[index + 1]
        ):
            _fail(f"Hull diagnostic anchor pair changed at leg {index}.")
        if index not in AMBIGUOUS_LEG_ORDERS and (
            diagnostic.get("path_status") != "unique"
            or diagnostic.get("path_sha256") != selected.path_sha256
        ):
            _fail(f"Hull unique diagnostic path changed at leg {index}.")
        leg_audit.append(
            {
                "order": index,
                "from_location": first["location"],
                "to_location": second["location"],
                "start_osm_node_id": inputs.anchor_ids[index],
                "end_osm_node_id": inputs.anchor_ids[index + 1],
                "source_path_status": diagnostic.get("path_status"),
                "selected_representative_path_sha256": selected.path_sha256,
                "edge_count": len(selected.edge_ids),
                "length_m": selected.total_length_m,
                "station_great_circle_distance_m": direct_m,
                "detour_ratio": detour,
                "detour_gate_passed": True,
                "repeat_gate_passed": True,
                "invented_connector_count": 0,
            }
        )

    pdf_source_id = "hull-trains-customer-timetable-may-december-2026"
    naptan_source_id = "naptan-stations-2026-08-07"
    pbf_source_id = "osm-gb-physical-rail-cartographic-corridor-2026-08-06"
    scope_source_id = "hull-trains-advertised-customer-scope-2026-08-08"
    candidate_source_id = "hull-trains-unreviewed-wtt-candidate-2026-08-08"
    registry_source_id = "gb-passenger-operator-registry-2026-08-08"
    diagnostic_source_id = "hull-trains-all-nine-corridor-diagnostic-2026-08-08"
    legacy_comparison_source_id = (
        "hull-trains-legacy-pair-ambiguity-comparison-2026-08-08"
    )
    equivalence_comparison_source_id = (
        "hull-trains-exhaustive-equivalence-comparison-2026-08-08"
    )
    blocker_source_id = "hull-trains-exact-corridor-blocker-2026-08-08"
    sources = (
        TransitSource(
            id=pdf_source_id,
            publisher="Hull Trains",
            url=str(inputs.official["url"]),
            licence="Official customer timetable; factual calls transcribed",
            attribution="Hull Trains",
            retrieved_at=retrieved_at,
            sha256=official_pdf_sha256,
            use="Official Saturday advertised station order; not track geometry.",
            commercial_reuse_status="review-required",
            valid_from=str(inputs.official["valid_from"]),
            valid_to=str(inputs.official["valid_to"]),
        ),
        TransitSource(
            id=naptan_source_id,
            publisher="UK Department for Transport",
            url="https://beta-naptan.dft.gov.uk/download",
            licence="Open Government Licence v3.0",
            attribution="Contains NaPTAN data",
            retrieved_at="2026-08-07",
            sha256=naptan_sha256,
            use="Canonical RLY station coordinates only.",
            commercial_reuse_status="commercial-allowed",
        ),
        TransitSource(
            id=pbf_source_id,
            publisher="OpenStreetMap contributors; extract by Geofabrik",
            url="https://download.geofabrik.de/europe/great-britain.html",
            licence="Open Data Commons Open Database Licence 1.0",
            attribution="© OpenStreetMap contributors",
            retrieved_at="2026-08-07",
            sha256=graph.source.sha256,
            use=(
                "Exact physical railway=rail segments used only as a scale-aware "
                "cartographic corridor representation; no operator-track claim."
            ),
            commercial_reuse_status="commercial-allowed",
        ),
        TransitSource(
            id=scope_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike/city-map-plotter",
            licence="Derived factual evidence ledger; review proof only",
            attribution="Derived from pinned Hull Trains customer timetable",
            retrieved_at=retrieved_at,
            sha256=scope_file_sha256,
            use="Advertised station scope and non-operational claim boundary.",
            commercial_reuse_status="review-required",
        ),
        TransitSource(
            id=candidate_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike/city-map-plotter",
            licence="Derived source audit; review proof only",
            attribution="Derived from pinned WTT, NaPTAN and OSM inputs",
            retrieved_at=retrieved_at,
            sha256=candidate_file_sha256,
            use="Unreviewed station/graph candidates; no WTT path approval.",
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
            attribution="City Map Plotter passenger-operator registry",
            retrieved_at=retrieved_at,
            sha256=inputs.registry_sha256,
            use="Customer product identity and house presentation reference.",
            commercial_reuse_status="review-required",
            valid_from=OPERATOR_REGISTRY.snapshot,
        ),
        TransitSource(
            id=diagnostic_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike/city-map-plotter",
            licence="Derived diagnostic evidence; review proof only",
            attribution="City Map Plotter all-nine Hull corridor diagnostic",
            retrieved_at=retrieved_at,
            sha256=all_nine_file_sha256,
            use="All nine leg statuses and unselected candidate path hashes.",
            commercial_reuse_status="review-required",
        ),
        TransitSource(
            id=legacy_comparison_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike/city-map-plotter",
            licence="Derived scale-comparison evidence; review proof only",
            attribution="City Map Plotter Hull legacy pair ambiguity comparison",
            retrieved_at=retrieved_at,
            sha256=legacy_comparison_file_sha256,
            use=(
                "Original two ambiguity witnesses; retained as superseded, "
                "non-exhaustive provenance only."
            ),
            commercial_reuse_status="review-required",
        ),
        TransitSource(
            id=equivalence_comparison_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike/city-map-plotter",
            licence="Derived scale-comparison evidence; review proof only",
            attribution=(
                "City Map Plotter Hull exhaustive ambiguity equivalence comparison"
            ),
            retrieved_at=retrieved_at,
            sha256=equivalence_comparison_file_sha256,
            use=(
                "Sealed exhaustive equal-shortest classes plus candidate-to-"
                "reference and pairwise A3 paper-space bounds."
            ),
            commercial_reuse_status="review-required",
        ),
        TransitSource(
            id=blocker_source_id,
            publisher="City Map Plotter",
            url="https://github.com/adambickerdike/city-map-plotter",
            licence="Derived blocker evidence; review proof only",
            attribution="City Map Plotter exact-edge Hull corridor blocker",
            retrieved_at=retrieved_at,
            sha256=exact_blocker_file_sha256,
            use="Proof that the distinct exact-edge/operational policy stays blocked.",
            commercial_reuse_status="review-required",
        ),
    )

    line_id = "hull-trains-scale-aware-cartographic-corridor"
    line = TransitLine(
        id=line_id,
        name="Hull Trains scale-aware advertised corridor",
        short_name="HT",
        order=0,
        colour=ColourSpec(
            name="Hull Trains house reference",
            display_hex=product.presentation.display_hex,
            role="operator-network",
            provenance="house-palette",
            numeric_value_status="house-value",
            source_ref=registry_source_id,
        ),
        pen=_physical_pen(),
        service_class=(
            "scale-aware-cartographic-corridor-not-operational-track"
        ),
        source_ref=equivalence_comparison_source_id,
    )
    route_node_ids = sorted(
        {node_id for selected in selected_candidates for node_id in selected.node_ids}
    )
    nodes: list[TransitNode] = [
        TransitNode(
            id=f"cartographic-osm-node-{node_id}",
            kind="junction",
            lon=graph.nodes[node_id].lon,
            lat=graph.nodes[node_id].lat,
            source_ref=pbf_source_id,
            source_object=f"node/{node_id}",
        )
        for node_id in route_node_ids
    ]
    station_ids: list[str] = []
    for station in inputs.stations:
        station_id = (
            "advertised-station-"
            + str(station["display_name"])
            .casefold()
            .replace("'", "")
            .replace(" ", "-")
        )
        station_ids.append(station_id)
        order = int(station["order"])
        nodes.append(
            TransitNode(
                id=station_id,
                kind="terminal" if order in {0, 9} else "station",
                lon=float(station["lon"]),
                lat=float(station["lat"]),
                source_ref=naptan_source_id,
                name=str(station["display_name"]),
                station_tier="terminal" if order in {0, 9} else "major",
                source_object=str(station["naptan_atco_code"]),
            )
        )
    output_edge_ids: dict[str, str] = {}
    edges: list[TransitEdge] = []
    for selected in selected_candidates:
        for edge_id in selected.edge_ids:
            if edge_id in output_edge_ids:
                continue
            graph_edge = graph.edges[edge_id]
            output_id = (
                f"cartographic-osm-way-{graph_edge.source_way_id}-segment-"
                f"{graph_edge.source_segment_index}"
            )
            output_edge_ids[edge_id] = output_id
            first_node = graph.nodes[graph_edge.source_from_node_id]
            second_node = graph.nodes[graph_edge.source_to_node_id]
            edges.append(
                TransitEdge(
                    id=output_id,
                    from_node=(
                        f"cartographic-osm-node-"
                        f"{graph_edge.source_from_node_id}"
                    ),
                    to_node=(
                        f"cartographic-osm-node-{graph_edge.source_to_node_id}"
                    ),
                    geometry=(
                        (first_node.lon, first_node.lat),
                        (second_node.lon, second_node.lat),
                    ),
                    line_ids=(line_id,),
                    source_ref=pbf_source_id,
                    source_object=f"way/{graph_edge.source_way_id}",
                    status=(
                        "scale-aware-cartographic-representative-physical-segment-"
                        "not-operational-track-claim"
                    ),
                    grade=_grade(dict(graph_edge.tags)),
                )
            )

    def traversals(
        selected: Sequence[RailPathCandidateEvidence],
    ) -> tuple[EdgeTraversal, ...]:
        result: list[EdgeTraversal] = []
        for candidate_path in selected:
            for first_id, second_id, edge_id in zip(
                candidate_path.node_ids[:-1],
                candidate_path.node_ids[1:],
                candidate_path.edge_ids,
                strict=True,
            ):
                edge = graph.edges[edge_id]
                result.append(
                    EdgeTraversal(
                        edge_id=output_edge_ids[edge_id],
                        direction=(
                            "forward"
                            if (
                                first_id == edge.source_from_node_id
                                and second_id == edge.source_to_node_id
                            )
                            else "reverse"
                        ),
                    )
                )
        return tuple(result)

    patterns = (
        ServicePattern(
            id="hull-trains-scale-aware-advertised-core-corridor",
            line_id=line_id,
            name="London King's Cross – Hull advertised cartographic corridor",
            traversals=traversals(selected_candidates[:7]),
            station_ids=tuple(station_ids[:8]),
            source_ref=pdf_source_id,
            valid_from=str(inputs.official["valid_from"]),
            valid_to=str(inputs.official["valid_to"]),
            derivation_status=(
                "official-customer-station-scope-plus-scale-aware-sub-nib-"
                "cartographic-physical-corridor-not-operational-track"
            ),
            continuity_breaks=(),
        ),
        ServicePattern(
            id="hull-trains-scale-aware-beverley-extension-corridor",
            line_id=line_id,
            name="Hull – Beverley selected-services cartographic extension",
            traversals=traversals(selected_candidates[7:]),
            station_ids=tuple(station_ids[7:]),
            source_ref=pdf_source_id,
            valid_from=str(inputs.official["valid_from"]),
            valid_to=str(inputs.official["valid_to"]),
            derivation_status=(
                "official-customer-station-scope-plus-scale-aware-sub-nib-"
                "cartographic-physical-corridor-not-operational-track"
            ),
            continuity_breaks=(),
        ),
    )

    service_counts = Counter(
        str(dict(graph.edges[edge_id].tags).get("service", "none"))
        for selected in selected_candidates
        for edge_id in selected.edge_ids
    )
    audit: dict[str, Any] = {
        "schema_version": 3,
        "policy_version": HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION,
        "release_state": (
            "review-proof-scale-aware-cartographic-corridor-not-operational-track"
        ),
        "product_id": HULL_PRODUCT_ID,
        "operator_code": HULL_OPERATOR_CODE,
        "service_date": "2026-08-08",
        "sources": {
            "official_customer_timetable_sha256": official_pdf_sha256,
            "naptan_sha256": naptan_sha256,
            "osm_pbf_sha256": graph.source.sha256,
            "rail_graph_sha256": graph.graph_sha256,
            "scope_file_sha256": scope_file_sha256,
            "candidate_file_sha256": candidate_file_sha256,
            "candidate_evidence_sha256": inputs.candidate_evidence_sha256,
            "all_nine_diagnostic_file_sha256": all_nine_file_sha256,
            "legacy_pair_comparison_file_sha256": (
                legacy_comparison_file_sha256
            ),
            "equivalence_comparison_file_sha256": (
                equivalence_comparison_file_sha256
            ),
            "exact_corridor_blocker_file_sha256": exact_blocker_file_sha256,
            "registry_sha256": inputs.registry_sha256,
        },
        "claim_boundary": {
            "advertised_customer_station_scope": True,
            "scale_aware_cartographic_corridor": True,
            "sub_nib_parallel_track_ambiguity_collapsed": True,
            "operator_track_or_platform_binding_reviewed": False,
            "exact_operational_track_claimed": False,
            "exact_edge_corridor_policy_still_blocked": True,
            "generic_rail_inferred_as_operator_service": False,
        },
        "scale_gate": {
            **scale_audit,
            "renderer_geometry_tolerance_mm": RENDERER_GEOMETRY_TOLERANCE_MM,
            "owned_route_pen_nib_mm": OWNED_ROUTE_NIB_MM,
            "maximum_nib_fraction": MAXIMUM_NIB_FRACTION,
            "maximum_allowed_paper_separation_mm": (
                min(
                    RENDERER_GEOMETRY_TOLERANCE_MM,
                    OWNED_ROUTE_NIB_MM * MAXIMUM_NIB_FRACTION,
                )
            ),
            "policy_max_equivalence_candidate_count": (
                POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT
            ),
        },
        "stations": list(inputs.station_audit),
        "ambiguity_collapses": ambiguity_audit,
        "representative_selection_rule": {
            "required_ineligible_edge_count": 0,
            "required_excluded_yard_siding_spur_edge_count": 0,
            "zero_candidate_specific_service_or_crossover_count_required": False,
            "ordered_minimum_tuple_components": list(
                CARTOGRAPHIC_SELECTION_COMPONENTS
            ),
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
        },
        "legs": leg_audit,
        "selected_representative_service_value_edge_counts": dict(
            sorted(service_counts.items())
        ),
        "advertised_station_count": len(inputs.stations),
        "physical_edge_count": len(edges),
        "invented_connector_count": 0,
        "proximity_join_count": 0,
    }
    audit["ordered_evidence_sha256"] = _sha256_document(audit)

    network = TransitNetwork(
        id="hull-trains-scale-aware-cartographic-corridor-2026-08-08",
        name="HULL TRAINS",
        kind="national-operator",
        scope=(
            "SATURDAY ADVERTISED CUSTOMER CORRIDOR / SCALE-AWARE SUB-NIB "
            "PARALLEL-TRACK COLLAPSE / NOT AN OPERATIONAL TRACK MAP"
        ),
        format_id=product.format_id,
        snapshot="2026-08-08",
        validity_status="review-proof-cartographic-not-operational-track",
        geometry_mode=(
            "exact-osm-physical-segments-selected-as-scale-aware-cartographic-"
            "representatives-no-operational-track-claim"
        ),
        sources=sources,
        lines=(line,),
        nodes=tuple(nodes),
        edges=tuple(edges),
        service_patterns=patterns,
        context=(),
        omissions=(
            {
                "kind": "exact-operational-track-alignment",
                "status": "not-reviewed-not-claimed",
                "reason": (
                    "No Hull Trains platform or exact operational track choice was "
                    "reviewed; this is a regional cartographic corridor only."
                ),
            },
            {
                "kind": "exact-edge-advertised-corridor-policy",
                "status": "blocked-two-equal-shortest-ambiguities",
                "reason": (
                    "The distinct exact policy remains blocked at London King's "
                    "Cross–Grantham and Doncaster–Selby; this product does not "
                    "upgrade or replace that evidence claim."
                ),
            },
            {
                "kind": "scale-aware-cartographic-collapse",
                "status": (
                    "two-exhaustive-sub-nib-parallel-track-equivalence-classes-"
                    "collapsed"
                ),
                "reason": (
                    "Every member and every pair in each exhaustively bounded "
                    "equal-shortest class is below 0.04 mm and below 0.10 of "
                    "the owned 0.4 mm nib at the sealed A3 scale."
                ),
            },
            {
                "kind": "station-track-binding",
                "status": "mechanical-nearest-candidate-not-human-reviewed",
                "reason": (
                    "Station points use rank-1 nearest graph nodes without a "
                    "platform or operator-track claim."
                ),
            },
            {
                "kind": "geographic-context",
                "status": "required-separate-pinned-attachment",
                "reason": "No customer artwork is rendered from this route-only proof.",
            },
        ),
        notes=(
            "REVIEW PROOF — scale-aware advertised cartographic corridor, not an operational track map.",
            "Two parallel-track ambiguity classes were exhaustively enumerated at no more than 256 paths per leg and collapsed only after every member and pair passed sealed A3 bounds below 0.04 mm and 0.10 of the 0.4 mm nib.",
            "Every candidate requires zero ineligible and zero yard/siding/spur edges; all physically valid candidates remain in a disclosed cartographic ranking by candidate-specific service/crossover union count, crossover count, nonempty-service count, length, then path hash. A nonzero minimum proves no operator use.",
            "The separate exact-edge corridor policy remains blocked and no platform or WTT transition is approved.",
            f"Scale-aware evidence SHA-256: {audit['ordered_evidence_sha256']}.",
            f"Compiler policy: {HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION}.",
        ),
        contract_sha256="",
    )
    validate_transit_network(network)
    return network, audit


__all__ = [
    "CARTOGRAPHIC_SELECTION_COMPONENTS",
    "CARTOGRAPHIC_SELECTION_RULE",
    "COMPARISON_POLICY_VERSION",
    "EQUIVALENCE_CANDIDATE_ENUMERATION_LIMIT",
    "EQUIVALENCE_COMPARISON_POLICY_VERSION",
    "HULL_SCALE_AWARE_CORRIDOR_POLICY_VERSION",
    "LEGACY_COMPARISON_POLICY_VERSION",
    "MAXIMUM_NIB_FRACTION",
    "OWNED_ROUTE_NIB_MM",
    "POLICY_MAX_EQUIVALENCE_CANDIDATE_COUNT",
    "RENDERER_GEOMETRY_TOLERANCE_MM",
    "compile_hull_scale_aware_cartographic_corridor",
]
