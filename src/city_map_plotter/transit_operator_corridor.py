"""Truthful advertised-customer corridors for otherwise blocked operators.

This module deliberately does *not* compile an operational Hull Trains track
map.  It combines a dated official customer timetable station scope with a
mechanically selected shortest path over a hash-pinned physical OSM rail
graph.  Station-to-track anchors and paths remain explicitly unreviewed as
operator/platform choices; every emitted segment is nevertheless an exact
consecutive source-node segment and no connector is invented.
"""

from __future__ import annotations

from collections import Counter
import hashlib
from dataclasses import replace
from importlib import resources
import json
from math import asin, cos, radians, sin, sqrt
from typing import Any, Mapping, Sequence

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
from .transit_rail_graph import OsmRailGraph, RailGraphRoutingError, RailRoute


HULL_ADVERTISED_CORRIDOR_POLICY_VERSION = (
    "hull-trains-advertised-customer-corridor-v1"
)
HULL_OPERATOR_CODE = "HT"
HULL_PRODUCT_ID = "hull-trains-2026"
DEFAULT_ANCHOR_RADIUS_M = 500.0
DEFAULT_ANCHOR_CANDIDATE_LIMIT = 8
MAX_MECHANICAL_ANCHOR_DISTANCE_M = 100.0
MAX_LEG_DETOUR_RATIO = 1.8
ELIGIBLE_CORRIDOR_RAILWAY_VALUE = "rail"
EXCLUDED_CORRIDOR_SERVICE_VALUES = frozenset({"siding", "spur", "yard"})
_EARTH_RADIUS_M = 6_371_008.8


def _fail(message: str) -> None:
    raise MapPlotterError(message)


def _sha256_document(document: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _registry_sha256() -> str:
    resource = resources.files("city_map_plotter").joinpath(REGISTRY_RESOURCE)
    try:
        return hashlib.sha256(resource.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - packaged resource invariant.
        raise MapPlotterError(f"Cannot hash operator registry: {exc}") from exc


def _verified_candidate_digest(candidate: Mapping[str, Any]) -> str:
    payload = dict(candidate)
    recorded = payload.pop("candidate_document_sha256", None)
    if not isinstance(recorded, str) or len(recorded) != 64:
        _fail("Hull candidate lacks its canonical evidence digest.")
    if _sha256_document(payload) != recorded:
        _fail("Hull candidate canonical evidence digest changed.")
    return recorded


def _haversine_m(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    lon1, lat1 = first
    lon2, lat2 = second
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    value = (
        sin(d_lat / 2.0) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_M * asin(min(1.0, sqrt(value)))


def _grade(tags: Mapping[str, str]) -> str:
    if tags.get("bridge") not in {None, "", "no"}:
        return "bridge"
    if tags.get("tunnel") not in {None, "", "no"}:
        return "tunnel"
    return "unknown"


def _candidate_node_dict(value: Any) -> dict[str, Any]:
    return {
        "distance_m": value.distance_m,
        "incident_edge_ids": list(value.incident_edge_ids),
        "lat": value.lat,
        "lon": value.lon,
        "osm_node_id": value.osm_node_id,
    }


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object.")
    return value


def _require_sequence(value: Any, *, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _fail(f"{field} must be a list.")
    return value


def _physical_pen() -> TransitPen:
    product = OPERATOR_REGISTRY.by_key[HULL_OPERATOR_CODE]
    try:
        physical = next(
            pen
            for pen in ACTUAL_PEN_INVENTORY.pens
            if pen.identity == product.presentation.pen_id
        )
    except StopIteration as exc:  # pragma: no cover - checked-in invariant.
        raise MapPlotterError(
            "Hull corridor registry pen is absent from the owned inventory."
        ) from exc
    if abs(physical.nominal_nib_mm - product.presentation.nib_mm) > 1e-9:
        _fail("Hull corridor registry pen width changed.")
    return TransitPen(
        ink=physical.ink,
        nominal_nib_mm=physical.nominal_nib_mm,
        match_status="nominal-unmeasured",
        pen_id=physical.identity,
        calibration_state=physical.calibration_state,
        preview_hex=physical.preview_color,
    )


def _validate_scope(
    scope: Mapping[str, Any],
    *,
    official_pdf_sha256: str,
    registry_sha256: str,
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    if (
        scope.get("schema_version") != 1
        or scope.get("policy_version")
        != "hull-trains-advertised-customer-scope-v1"
        or scope.get("product_id") != HULL_PRODUCT_ID
        or scope.get("operator_code") != HULL_OPERATOR_CODE
        or scope.get("service_date") != "2026-08-08"
        or scope.get("day_table") != "SATURDAYS"
    ):
        _fail("Hull advertised customer scope identity changed.")
    official = _require_mapping(
        scope.get("official_customer_timetable"),
        field="official_customer_timetable",
    )
    if official.get("sha256") != official_pdf_sha256:
        _fail("Hull official customer timetable hash changed.")
    counts = _require_mapping(
        scope.get("advertised_service_counts"),
        field="advertised_service_counts",
    )
    if counts != {
        "towards_london": 6,
        "from_london": 6,
        "total": 12,
        "beverley_extension_towards_london": 2,
        "beverley_extension_from_london": 2,
        "beverley_extension_total": 4,
    }:
        _fail("Hull Saturday advertised service counts changed.")
    boundary = _require_mapping(
        scope.get("claim_boundary"), field="claim_boundary"
    )
    if not (
        boundary.get("advertised_customer_station_scope") is True
        and boundary.get("operator_track_or_platform_binding_reviewed") is False
        and boundary.get("exact_operational_track_claimed") is False
        and boundary.get("wtt_timing_points_used_as_customer_stations") is False
    ):
        _fail("Hull customer-scope claim boundary was weakened.")
    stations = list(_require_sequence(scope.get("stations"), field="stations"))
    if len(stations) != 10:
        _fail("Hull advertised customer scope must contain exactly ten stations.")
    for order, raw in enumerate(stations):
        station = _require_mapping(raw, field=f"stations[{order}]")
        if station.get("order") != order:
            _fail("Hull advertised stations are not in exact customer order.")
        if station.get("scope") not in {"core", "selected-services-extension"}:
            _fail(f"Hull advertised station {order} has an unsupported scope.")
    if [station["scope"] for station in stations] != [
        *("core" for _ in range(8)),
        "selected-services-extension",
        "selected-services-extension",
    ]:
        _fail("Hull core/extension station boundary changed.")
    exclusions = _require_sequence(
        scope.get("explicit_exclusions"), field="explicit_exclusions"
    )
    if not any(
        isinstance(item, Mapping) and item.get("location") == "STEVENAGE"
        for item in exclusions
    ):
        _fail("Hull Saturday scope no longer explicitly excludes Stevenage.")
    upstream = _require_mapping(
        scope.get("upstream_candidate_scope_assertion"),
        field="upstream_candidate_scope_assertion",
    )
    if upstream != {
        "sha256": registry_sha256,
        "resolved_path": (
            "src/city_map_plotter/data/gb-passenger-operators-2026-08-08.json"
        ),
        "resolved_source_kind": "dated-passenger-operator-registry",
        "same_bytes_as_official_customer_timetable": False,
        "note": (
            "The digest exactly matches the checked-in dated passenger-operator "
            "registry, not the Hull Trains customer-timetable PDF. The immutable "
            "upstream candidate is preserved unchanged."
        ),
    }:
        _fail("Hull scope misidentifies the upstream operator-scope assertion.")
    return stations, official


def compile_hull_advertised_corridor(
    graph: OsmRailGraph,
    candidate: Mapping[str, Any],
    scope: Mapping[str, Any],
    *,
    candidate_file_sha256: str,
    scope_file_sha256: str,
    official_pdf_sha256: str,
    naptan_sha256: str,
    retrieved_at: str = "2026-08-08",
    max_anchor_distance_m: float = MAX_MECHANICAL_ANCHOR_DISTANCE_M,
    max_leg_detour_ratio: float = MAX_LEG_DETOUR_RATIO,
) -> tuple[TransitNetwork, dict[str, Any]]:
    """Compile a disclosed non-operational corridor or fail closed."""

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
        _fail(
            "Hull candidate operator-scope assertion is not the immutable dated "
            "passenger-operator registry."
        )

    stations, official = _validate_scope(
        scope,
        official_pdf_sha256=official_pdf_sha256,
        registry_sha256=registry_sha256,
    )
    timing_locations_raw = _require_sequence(
        candidate.get("timing_locations"), field="candidate.timing_locations"
    )
    timing_by_name: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(timing_locations_raw):
        record = _require_mapping(raw, field=f"candidate.timing_locations[{index}]")
        location = record.get("location")
        if isinstance(location, str):
            timing_by_name[location] = record

    station_audit: list[dict[str, Any]] = []
    anchor_ids: list[int] = []
    for station in stations:
        location = str(station["location"])
        try:
            timing = timing_by_name[location]
        except KeyError as exc:
            raise MapPlotterError(
                f"Hull candidate lacks advertised station {location}."
            ) from exc
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
                f"Hull station {location} needs exactly one canonical NaPTAN RLY record."
            )
        coordinate = matches[0]
        if (
            coordinate.get("stop_type") != "RLY"
            or abs(float(coordinate.get("lon")) - float(station["lon"])) > 1e-11
            or abs(float(coordinate.get("lat")) - float(station["lat"])) > 1e-11
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

    leg_routes: list[RailRoute] = []
    leg_audit: list[dict[str, Any]] = []
    allowed_edge_ids = frozenset(
        edge_id
        for edge_id, edge in graph.edges.items()
        if dict(edge.tags).get("railway") == ELIGIBLE_CORRIDOR_RAILWAY_VALUE
        and dict(edge.tags).get("service")
        not in EXCLUDED_CORRIDOR_SERVICE_VALUES
    )
    if not allowed_edge_ids:
        _fail("Hull physical graph has no eligible standard-rail corridor edges.")
    used_railway_values: Counter[str] = Counter()
    used_service_values: Counter[str] = Counter()
    seen_edge_ids_by_pattern: dict[str, set[str]] = {
        "core": set(),
        "extension": set(),
    }
    for index, (first, second) in enumerate(zip(stations, stations[1:])):
        try:
            route = graph.shortest_path(
                anchor_ids[index],
                anchor_ids[index + 1],
                allowed_edge_ids=allowed_edge_ids,
            )
        except RailGraphRoutingError as exc:
            raise type(exc)(
                f"Hull mechanical corridor leg {first['location']}–"
                f"{second['location']} failed closed: {exc}",
                exc.evidence,
            ) from exc
        direct_m = _haversine_m(
            (float(first["lon"]), float(first["lat"])),
            (float(second["lon"]), float(second["lat"])),
        )
        detour_ratio = route.total_length_m / direct_m
        if detour_ratio > max_leg_detour_ratio:
            _fail(
                f"Hull mechanical corridor leg {first['location']}–"
                f"{second['location']} has detour ratio {detour_ratio:.3f}; "
                "the physical graph is not defensible for this product."
            )
        pattern_scope = "core" if index < 7 else "extension"
        seen_edge_ids = seen_edge_ids_by_pattern[pattern_scope]
        repeated = seen_edge_ids.intersection(step.edge_id for step in route.steps)
        if repeated:
            _fail(
                "Hull mechanical corridor repeats physical edges across advertised "
                "legs; a loop cannot be silently published."
            )
        seen_edge_ids.update(step.edge_id for step in route.steps)
        for step in route.steps:
            step_tags = dict(step.tags)
            used_railway_values[step_tags.get("railway", "missing")] += 1
            used_service_values[step_tags.get("service", "none")] += 1
        leg_routes.append(route)
        candidate_path = route.evidence.candidates[0]
        leg_audit.append(
            {
                "order": index,
                "from_location": first["location"],
                "to_location": second["location"],
                "start_osm_node_id": anchor_ids[index],
                "end_osm_node_id": anchor_ids[index + 1],
                "path_status": route.evidence.status,
                "path_sha256": candidate_path.path_sha256,
                "edge_count": len(route.steps),
                "length_m": route.total_length_m,
                "station_great_circle_distance_m": direct_m,
                "detour_ratio": detour_ratio,
                "selection_method": (
                    "unique-shortest-undirected-standard-rail-physical-osm-path-"
                    "excluding-yard-siding-spur-not-operational-track-claim"
                ),
                "allowed_edge_count": len(allowed_edge_ids),
                "invented_connector_count": 0,
            }
        )

    pbf_source_id = "osm-gb-physical-rail-corridor-2026-08-06"
    pdf_source_id = "hull-trains-customer-timetable-may-december-2026"
    naptan_source_id = "naptan-stations-2026-08-07"
    scope_source_id = "hull-trains-advertised-customer-scope-2026-08-08"
    candidate_source_id = "hull-trains-unreviewed-wtt-candidate-2026-08-08"
    registry_source_id = "gb-passenger-operator-registry-2026-08-08"
    product = OPERATOR_REGISTRY.by_key[HULL_OPERATOR_CODE]
    transit_sources = (
        TransitSource(
            id=pdf_source_id,
            publisher="Hull Trains",
            url=str(official["url"]),
            licence="Official customer timetable; factual calls transcribed",
            attribution="Hull Trains",
            retrieved_at=retrieved_at,
            sha256=official_pdf_sha256,
            use=(
                "Official Saturday advertised station order and service counts; "
                "not track or platform geometry."
            ),
            commercial_reuse_status="review-required",
            valid_from=str(official["valid_from"]),
            valid_to=str(official["valid_to"]),
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
                "Exact consecutive railway=rail node segments selected by a "
                "mechanical shortest-corridor algorithm after excluding yard, "
                "siding and spur service tracks; no Hull Trains usage claim."
            ),
            commercial_reuse_status="commercial-allowed",
        ),
        TransitSource(
            id=scope_source_id,
            publisher="City Map Plotter",
            url=(
                "https://github.com/adambickerdike/city-map-plotter/tree/main/"
                "review-output/transit-gb-passenger-operators-route-evidence-"
                "2026-08-08/sources"
            ),
            licence="Derived factual evidence ledger; review proof only",
            attribution="Derived from the pinned official customer timetable",
            retrieved_at=retrieved_at,
            sha256=scope_file_sha256,
            use="Reviewed transcription and explicit non-operational claim boundary.",
            commercial_reuse_status="review-required",
        ),
        TransitSource(
            id=candidate_source_id,
            publisher="City Map Plotter",
            url=(
                "https://github.com/adambickerdike/city-map-plotter/tree/main/"
                "review-output/transit-gb-passenger-operators-route-evidence-"
                "2026-08-08/hull-trains-wtt-candidates"
            ),
            licence="Derived source audit; review proof only",
            attribution="Derived from pinned WTT, NaPTAN and OSM inputs",
            retrieved_at=retrieved_at,
            sha256=candidate_file_sha256,
            use=(
                "Unreviewed WTT candidate and station/graph candidate evidence. "
                "Its WTT identities, track bindings and transition paths do not "
                "approve this corridor."
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
            attribution="City Map Plotter passenger-operator registry",
            retrieved_at=retrieved_at,
            sha256=registry_sha256,
            use="Customer product identity and house presentation reference.",
            commercial_reuse_status="review-required",
            valid_from=OPERATOR_REGISTRY.snapshot,
        ),
    )

    line_id = "hull-trains-advertised-customer-corridor"
    line = TransitLine(
        id=line_id,
        name="Hull Trains advertised customer corridor",
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
        service_class="advertised-customer-corridor-not-operational-track",
        source_ref=pdf_source_id,
    )

    route_node_ids = sorted(
        {node_id for route in leg_routes for node_id in route.node_ids}
    )
    nodes: list[TransitNode] = [
        TransitNode(
            id=f"corridor-osm-node-{node_id}",
            kind="junction",
            lon=graph.nodes[node_id].lon,
            lat=graph.nodes[node_id].lat,
            source_ref=pbf_source_id,
            source_object=f"node/{node_id}",
        )
        for node_id in route_node_ids
    ]
    station_ids: list[str] = []
    for station in stations:
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

    edge_ids: dict[str, str] = {}
    edges: list[TransitEdge] = []
    for route in leg_routes:
        for step in route.steps:
            if step.edge_id in edge_ids:
                continue
            graph_edge = graph.edges[step.edge_id]
            edge_id = (
                f"corridor-osm-way-{graph_edge.source_way_id}-segment-"
                f"{graph_edge.source_segment_index}"
            )
            edge_ids[step.edge_id] = edge_id
            first = graph.nodes[graph_edge.source_from_node_id]
            second = graph.nodes[graph_edge.source_to_node_id]
            edges.append(
                TransitEdge(
                    id=edge_id,
                    from_node=(
                        f"corridor-osm-node-{graph_edge.source_from_node_id}"
                    ),
                    to_node=f"corridor-osm-node-{graph_edge.source_to_node_id}",
                    geometry=((first.lon, first.lat), (second.lon, second.lat)),
                    line_ids=(line_id,),
                    source_ref=pbf_source_id,
                    source_object=f"way/{graph_edge.source_way_id}",
                    status=(
                        "mechanical-advertised-corridor-physical-segment-"
                        "not-operational-track-claim"
                    ),
                    grade=_grade(dict(graph_edge.tags)),
                )
            )

    def traversals(routes: Sequence[RailRoute]) -> tuple[EdgeTraversal, ...]:
        return tuple(
            EdgeTraversal(
                edge_id=edge_ids[step.edge_id],
                direction="forward" if step.follows_source_direction else "reverse",
            )
            for route in routes
            for step in route.steps
        )

    patterns = (
        ServicePattern(
            id="hull-trains-advertised-core-corridor",
            line_id=line_id,
            name="London King's Cross – Hull advertised corridor",
            traversals=traversals(leg_routes[:7]),
            station_ids=tuple(station_ids[:8]),
            source_ref=pdf_source_id,
            valid_from=str(official["valid_from"]),
            valid_to=str(official["valid_to"]),
            derivation_status=(
                "official-customer-station-scope-plus-mechanical-unique-shortest-"
                "physical-osm-path-not-operational-track"
            ),
            continuity_breaks=(),
        ),
        ServicePattern(
            id="hull-trains-advertised-beverley-extension-corridor",
            line_id=line_id,
            name="Hull – Beverley selected-services extension corridor",
            traversals=traversals(leg_routes[7:]),
            station_ids=tuple(station_ids[7:]),
            source_ref=pdf_source_id,
            valid_from=str(official["valid_from"]),
            valid_to=str(official["valid_to"]),
            derivation_status=(
                "official-customer-station-scope-plus-mechanical-unique-shortest-"
                "physical-osm-path-not-operational-track"
            ),
            continuity_breaks=(),
        ),
    )

    audit: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": HULL_ADVERTISED_CORRIDOR_POLICY_VERSION,
        "release_state": "review-proof-not-operational-track-map",
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
            "candidate_evidence_sha256": candidate_evidence_sha256,
            "candidate_operator_scope_registry_sha256": registry_sha256,
            "registry_sha256": registry_sha256,
        },
        "claim_boundary": {
            "advertised_customer_station_scope": True,
            "mechanical_physical_corridor": True,
            "operator_track_or_platform_binding_reviewed": False,
            "exact_operational_track_claimed": False,
            "wtt_transition_paths_approved": False,
            "generic_rail_inferred_as_operator_service": False,
        },
        "selection_policy": {
            "station_coordinate": "exact pinned NaPTAN RLY record",
            "track_anchor": (
                "rank-1 nearest exact OSM graph node within 100 m; mechanical, "
                "not a reviewed platform/track binding"
            ),
            "between_station_path": (
                "unique shortest exact-node railway=rail physical path after "
                "excluding service=yard/siding/spur; fail closed on equal shortest "
                "ambiguity, disconnection, repeat, or detour ratio above 1.8"
            ),
        },
        "stations": station_audit,
        "legs": leg_audit,
        "advertised_station_count": len(stations),
        "physical_edge_count": len(edges),
        "total_corridor_length_m": sum(route.total_length_m for route in leg_routes),
        "maximum_anchor_distance_m": max(
            float(item["distance_m"]) for item in station_audit
        ),
        "maximum_leg_detour_ratio": max(
            float(item["detour_ratio"]) for item in leg_audit
        ),
        "eligible_physical_edge_count": len(allowed_edge_ids),
        "eligible_railway_value": ELIGIBLE_CORRIDOR_RAILWAY_VALUE,
        "excluded_service_values": sorted(EXCLUDED_CORRIDOR_SERVICE_VALUES),
        "used_railway_value_edge_counts": dict(sorted(used_railway_values.items())),
        "used_service_value_edge_counts": dict(sorted(used_service_values.items())),
        "invented_connector_count": 0,
        "proximity_join_count": 0,
    }
    audit["ordered_evidence_sha256"] = _sha256_document(audit)

    network = TransitNetwork(
        id="hull-trains-advertised-customer-corridor-2026-08-08",
        name="HULL TRAINS",
        kind="national-operator",
        scope=(
            "SATURDAY ADVERTISED CUSTOMER CORRIDOR / MECHANICAL PHYSICAL "
            "ALIGNMENT / NOT AN OPERATIONAL TRACK MAP"
        ),
        format_id=product.format_id,
        snapshot="2026-08-08",
        validity_status="candidate-not-reviewed",
        geometry_mode=(
            "exact-osm-standard-rail-physical-segments-mechanical-shortest-"
            "customer-corridor-no-operational-track-claim"
        ),
        sources=transit_sources,
        lines=(line,),
        nodes=tuple(nodes),
        edges=tuple(edges),
        service_patterns=patterns,
        context=(),
        omissions=(
            {
                "kind": "operational-track-alignment",
                "status": "not-reviewed-not-claimed",
                "reason": (
                    "WTT timing-point-to-track and transition-path choices remain "
                    "unreviewed; this product cannot be called an exact Hull Trains "
                    "operational route."
                ),
            },
            {
                "kind": "station-track-binding",
                "status": "mechanical-nearest-candidate-not-human-reviewed",
                "reason": (
                    "Advertised NaPTAN station points are associated with the nearest "
                    "physical graph node only to form a disclosed corridor; no "
                    "platform or operator-track choice is asserted."
                ),
            },
            {
                "kind": "wtt-operational-points",
                "status": "not-rendered-not-used-as-customer-stations",
                "reason": (
                    "Operational pass points and the non-advertised W00041/1H03 "
                    "identity are not converted into visible station markers."
                ),
            },
            {
                "kind": "saturday-customer-scope",
                "status": "dated-2026-08-08",
                "reason": (
                    "The plate represents the official Saturday table only: 12 "
                    "advertised trains, four extending between Hull and Beverley, "
                    "and no Stevenage call."
                ),
            },
            {
                "kind": "geographic-context",
                "status": "required-separate-pinned-attachment",
                "reason": (
                    "Scale-aware house context must be attached separately; generic "
                    "physical rail context is never operator-service evidence."
                ),
            },
        ),
        notes=(
            "REVIEW PROOF — advertised customer corridor, not an operational track map.",
            "Every route edge is one exact consecutive node segment from the pinned OSM physical rail graph.",
            "Corridor routing admits railway=rail only and excludes service=yard/siding/spur shortcuts; any crossover use remains disclosed in the audit.",
            "Station anchors and between-station paths are deterministic mechanical selections, not human-reviewed Hull Trains platform or track bindings.",
            "No connector, proximity join, WTT timing-point marker, or operator use of generic rail has been invented.",
            f"Corridor evidence SHA-256: {audit['ordered_evidence_sha256']}.",
            f"Compiler policy: {HULL_ADVERTISED_CORRIDOR_POLICY_VERSION}.",
        ),
        contract_sha256="",
    )
    validate_transit_network(network)
    return network, audit


def compile_mixed_operator_overview_with_hull_corridor(
    base_overview: TransitNetwork,
    corridor: TransitNetwork,
    corridor_audit: Mapping[str, Any],
) -> tuple[TransitNetwork, dict[str, Any]]:
    """Add Hull as a visibly mixed-evidence 25th overview line.

    The existing 24 OSM operator-tag products keep their original edge and
    source records.  Hull edges remain separate even where coordinates overlap,
    because folding them into an OSM operator-relation edge would erase the
    corridor's distinct non-operational physical-graph provenance.
    """

    if (
        base_overview.kind != "national-operator-overview"
        or len(base_overview.lines) != 24
        or base_overview.context
    ):
        _fail("Mixed overview requires the route-only reviewed 24-line base.")
    if any(line.short_name == "HT" for line in base_overview.lines):
        _fail("Mixed overview base already contains a Hull Trains line.")
    if not any(
        item.get("product_id") == HULL_PRODUCT_ID
        and item.get("status") == "blocked-no-usable-osm-relation"
        for item in base_overview.omissions
    ):
        _fail("Mixed overview base no longer discloses the OSM Hull coverage gap.")
    if (
        corridor.id != "hull-trains-advertised-customer-corridor-2026-08-08"
        or corridor.kind != "national-operator"
        or len(corridor.lines) != 1
        or corridor.context
    ):
        _fail("Mixed overview received the wrong route-only Hull corridor.")
    audit_payload = dict(corridor_audit)
    audit_digest = audit_payload.pop("ordered_evidence_sha256", None)
    if (
        not isinstance(audit_digest, str)
        or _sha256_document(audit_payload) != audit_digest
        or corridor_audit.get("policy_version")
        != HULL_ADVERTISED_CORRIDOR_POLICY_VERSION
    ):
        _fail("Mixed overview Hull corridor audit does not verify.")
    boundary = _require_mapping(
        corridor_audit.get("claim_boundary"), field="corridor claim_boundary"
    )
    if not (
        boundary.get("exact_operational_track_claimed") is False
        and boundary.get("operator_track_or_platform_binding_reviewed") is False
        and boundary.get("mechanical_physical_corridor") is True
    ):
        _fail("Mixed overview Hull claim boundary was weakened.")

    base_source_by_id = {source.id: source for source in base_overview.sources}
    extra_sources: list[TransitSource] = []
    for source in corridor.sources:
        existing = base_source_by_id.get(source.id)
        if existing is None:
            extra_sources.append(source)
        elif existing.sha256 != source.sha256:
            _fail(
                f"Mixed overview source {source.id!r} has conflicting hashes."
            )

    registry_order = next(
        index
        for index, product in enumerate(OPERATOR_REGISTRY.products)
        if product.operator_key == HULL_OPERATOR_CODE
    )
    source_line = corridor.lines[0]
    hull_line = replace(
        source_line,
        name="Hull Trains — advertised corridor (not operational track)",
        short_name="HT*",
        order=registry_order,
    )
    if hull_line.id in {line.id for line in base_overview.lines}:
        _fail("Mixed overview Hull line ID collides with the base.")
    lines = tuple(
        sorted(
            (*base_overview.lines, hull_line),
            key=lambda line: (line.order, line.id),
        )
    )

    base_node_ids = {node.id for node in base_overview.nodes}
    corridor_junctions = tuple(node for node in corridor.nodes if not node.is_station)
    collisions = sorted(base_node_ids.intersection(node.id for node in corridor_junctions))
    if collisions:
        _fail("Mixed overview node IDs collide: " + ", ".join(collisions[:8]))
    base_edge_ids = {edge.id for edge in base_overview.edges}
    edge_collisions = sorted(base_edge_ids.intersection(edge.id for edge in corridor.edges))
    if edge_collisions:
        _fail("Mixed overview edge IDs collide: " + ", ".join(edge_collisions[:8]))
    base_pattern_ids = {pattern.id for pattern in base_overview.service_patterns}
    pattern_collisions = sorted(
        base_pattern_ids.intersection(
            pattern.id for pattern in corridor.service_patterns
        )
    )
    if pattern_collisions:
        _fail(
            "Mixed overview pattern IDs collide: "
            + ", ".join(pattern_collisions[:8])
        )
    hull_patterns = tuple(
        replace(
            pattern,
            line_id=hull_line.id,
            station_ids=(),
            name=pattern.name + " / OVERVIEW — NON-OPERATIONAL CORRIDOR",
        )
        for pattern in corridor.service_patterns
    )

    mixed_audit: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": "gb-passenger-operator-mixed-overview-v1",
        "release_state": "review-proof-mixed-evidence-not-official-service-map",
        "base_overview_contract_sha256": base_overview.contract_sha256,
        "hull_corridor_contract_sha256": corridor.contract_sha256,
        "hull_corridor_evidence_sha256": audit_digest,
        "registry_product_count": len(OPERATOR_REGISTRY.products),
        "osm_operator_relation_product_count": 24,
        "advertised_customer_corridor_product_count": 1,
        "output_line_count": len(lines),
        "claim_boundary": {
            "all_25_products_use_one_evidence_method": False,
            "hull_is_osm_operator_tag_relation": False,
            "hull_is_exact_operational_track_map": False,
            "hull_is_advertised_customer_corridor": True,
            "mixed_evidence_legend_required": True,
        },
        "hull_station_markers_at_national_overview_scale": "omitted",
        "hull_edge_source_records_kept_separate": True,
        "invented_connector_count": 0,
    }
    mixed_audit["ordered_evidence_sha256"] = _sha256_document(mixed_audit)

    network = TransitNetwork(
        id="great-britain-passenger-operator-mixed-overview-2026-08-08",
        name="GREAT BRITAIN PASSENGER OPERATORS",
        kind="national-operator-overview",
        scope=(
            "GREAT BRITAIN / 25 REGISTRY PRODUCTS / 24 OSM OPERATOR-RELATION "
            "PROOFS + 1 ADVERTISED CUSTOMER CORRIDOR / NOT OFFICIAL COMPLETE "
            "OPERATIONAL COVERAGE"
        ),
        format_id=base_overview.format_id,
        snapshot="2026-08-08",
        validity_status="candidate-not-reviewed",
        geometry_mode=(
            "mixed-evidence-24-osm-operator-relation-proofs-plus-one-hull-"
            "mechanical-advertised-corridor-separate-edge-provenance"
        ),
        sources=(*base_overview.sources, *extra_sources),
        lines=lines,
        nodes=(*base_overview.nodes, *corridor_junctions),
        edges=(*base_overview.edges, *corridor.edges),
        service_patterns=(*base_overview.service_patterns, *hull_patterns),
        context=(),
        omissions=(
            *base_overview.omissions,
            *corridor.omissions,
            {
                "kind": "mixed-evidence-product",
                "product_id": HULL_PRODUCT_ID,
                "status": "advertised-customer-corridor-not-operational-track",
                "reason": (
                    "Hull Trains remains absent from the OSM operator-tag relation "
                    "snapshot. Its separately sourced line uses official advertised "
                    "station scope and a mechanical physical-graph corridor only."
                ),
            },
            {
                "kind": "overview-station-markers",
                "product_id": HULL_PRODUCT_ID,
                "status": "omitted-at-national-scale",
                "reason": (
                    "The ten advertised stations remain in the standalone corridor "
                    "contract and audit; the combined national overview omits their "
                    "symbols to avoid implying a uniform station-evidence method."
                ),
            },
        ),
        notes=(
            *base_overview.notes,
            "MIXED EVIDENCE: Hull Trains is an advertised customer corridor, not an OSM operator-tag relation or operational track map.",
            "Hull physical edges retain their separate PBF corridor source; coincident geometry is not silently reassigned to the base overview source.",
            f"Hull corridor evidence SHA-256: {audit_digest}.",
            f"Mixed overview evidence SHA-256: {mixed_audit['ordered_evidence_sha256']}.",
        ),
        contract_sha256="",
    )
    validate_transit_network(network)
    return network, mixed_audit


__all__ = [
    "DEFAULT_ANCHOR_CANDIDATE_LIMIT",
    "DEFAULT_ANCHOR_RADIUS_M",
    "HULL_ADVERTISED_CORRIDOR_POLICY_VERSION",
    "MAX_LEG_DETOUR_RATIO",
    "MAX_MECHANICAL_ANCHOR_DISTANCE_M",
    "compile_hull_advertised_corridor",
    "compile_mixed_operator_overview_with_hull_corridor",
]
