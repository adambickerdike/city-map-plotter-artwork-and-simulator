"""Paper-space planning for geographic passenger-rail networks.

The planner never creates service connectivity.  It projects the normalized
contract, gives coincident display groups deterministic lanes, carries those
lanes through compatible degree-two boundaries, tapers them back to the exact
graph node at real topology changes, and simplifies only inside a declared
physical tolerance while protecting stations and endpoints.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
import heapq
import hashlib
from math import ceil, cos, hypot, radians, tan
from typing import Iterable, Sequence

from shapely.geometry import LineString
from shapely.ops import substring, unary_union
from shapely.strtree import STRtree

from .models import MapPlotterError
from .niche_common import Rect
from .pens import (
    ACTUAL_PEN_INVENTORY,
    PenWidthFit,
    PhysicalPen,
    fit_pen_width,
)
from .stroke_font import text_width_mm
from .transit import TransitEdge, TransitLine, TransitNetwork
from .transit_extent import (
    NAMED_OPERATOR_KINDS,
    TransitProjectionExtent,
    named_operator_projection_extent,
)


Point = tuple[float, float]

EARTH_RADIUS_M = 6_371_008.8
DEFAULT_ROUTE_NIB_MM = 0.4
DEFAULT_ROUTE_TARGET_MM = 1.0
SECONDARY_ROUTE_TARGET_MM = 0.6
COMPACT_COLOURED_ROUTE_STROKE_COUNT = 4
COMPACT_COLOURED_ROUTE_PITCH_MM = 0.2
SCALE_ROUTE_TARGETS_MM: dict[str, float] = {
    "compact-network": 1.0,
    "urban-network": 0.8,
    "regional-network": 0.8,
    "national-network": 0.8,
}
NATIVE_OWNED_NIB_PROMOTION_POLICY_VERSION = (
    "transit-registry-bound-native-owned-nib-promotion-v1"
)
MERSEYRAIL_NATIVE_WIDTH_PRODUCT_ID = "merseyrail-2026"
MERSEYRAIL_NATIVE_WIDTH_OPERATOR_KEY = "ME"
MERSEYRAIL_NATIVE_WIDTH_REGISTRY_SNAPSHOT = "2026-08-08"
MERSEYRAIL_NATIVE_WIDTH_NETWORK_ID = (
    "merseyrail-osm-explicit-operator-tag-2026-08-06"
)
MERSEYRAIL_NATIVE_WIDTH_LINE_ID = (
    "merseyrail-osm-explicit-operator-tag-snapshot"
)
MERSEYRAIL_NATIVE_WIDTH_SCALE_TIER = "urban-network"
MERSEYRAIL_NATIVE_WIDTH_BASELINE_MM = 0.8
MERSEYRAIL_NATIVE_WIDTH_RESOLVED_MM = 1.0
DEFAULT_PROJECTOR_MARGIN_FRACTION = 0.045
COMPACT_MAX_SCALE_DENOMINATOR = 75_000.0
URBAN_MAX_SCALE_DENOMINATOR = 250_000.0
REGIONAL_MAX_SCALE_DENOMINATOR = 750_000.0
DEFAULT_LANE_GAP_MM = 0.20
DEFAULT_SIMPLIFICATION_MM = 0.025
DISTANT_SIMPLIFICATION_MM = 0.04
DEFAULT_TAPER_MM = 2.4
STATION_ASSOCIATION_LIMIT_MM = 2.0
LABEL_CLEARANCE_MM = 0.45
LABEL_TEXT_PADDING_MM = 0.35
LABEL_TO_LABEL_GAP_MM = 0.45
LABEL_FRAME_CLEARANCE_MM = 0.65
STATION_SYMBOL_NIB_MM = 0.4
SAME_LINE_CORRIDOR_POLICY_VERSION = "paper-corridor-v1"
SAME_LINE_CORRIDOR_TOLERANCE_NIBS = 1.0
SAME_LINE_CLOSED_LOOP_TOLERANCE_NIBS = 1.25
SAME_LINE_CORRIDOR_MINIMUM_RUN_NIBS = 3.0
SAME_LINE_CORRIDOR_SAMPLE_STEP_NIBS = 1.0 / 3.0
SAME_LINE_CORRIDOR_MAXIMUM_ANGLE_DEGREES = 20.0
SAME_LINE_CORRIDOR_MAX_FIXED_POINT_ITERATIONS = 4


@dataclass(frozen=True, slots=True)
class Projector:
    """Local metric WGS84 projection fitted into one physical map field."""

    lon0: float
    lat0: float
    min_x_m: float
    min_y_m: float
    scale_mm_per_m: float
    offset_x_mm: float
    offset_y_mm: float
    rect: Rect
    source_width_m: float
    source_height_m: float
    extent_policy: TransitProjectionExtent | None

    def metric(self, lon: float, lat: float) -> Point:
        return (
            EARTH_RADIUS_M * radians(lon - self.lon0) * cos(radians(self.lat0)),
            EARTH_RADIUS_M * radians(lat - self.lat0),
        )

    def point(self, lon: float, lat: float) -> Point:
        x_m, y_m = self.metric(lon, lat)
        return (
            self.offset_x_mm + (x_m - self.min_x_m) * self.scale_mm_per_m,
            self.offset_y_mm
            + self.source_height_m * self.scale_mm_per_m
            - (y_m - self.min_y_m) * self.scale_mm_per_m,
        )

    @property
    def scale_denominator(self) -> float:
        return 1000.0 / self.scale_mm_per_m


@dataclass(frozen=True, slots=True)
class PlannedRouteStroke:
    line_id: str
    edge_ids: tuple[str, ...]
    start_node_id: str
    end_node_id: str
    points: tuple[Point, ...]
    source_refs: tuple[str, ...]
    maximum_lane_offset_mm: float
    simplification_tolerance_mm: float
    source_vertex_count: int
    represented_edge_ids: tuple[str, ...] = ()

    @property
    def source_membership_edge_ids(self) -> tuple[str, ...]:
        """Source memberships represented by this physical display path."""

        return self.represented_edge_ids or self.edge_ids


@dataclass(frozen=True, slots=True)
class PlannedContextStroke:
    feature_id: str
    kind: str
    points: tuple[Point, ...]
    source_ref: str
    source_object: str
    represented_feature_ids: tuple[str, ...] = ()
    represented_source_refs: tuple[str, ...] = ()
    represented_source_objects: tuple[str, ...] = ()
    source_layer: str | None = None
    source_tags: tuple[tuple[str, str], ...] = ()
    node_refs: tuple[str, ...] = ()
    geometry_type: str = "line"
    ring_role: str | None = None

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return self.represented_feature_ids or (self.feature_id,)

    @property
    def source_refs(self) -> tuple[str, ...]:
        return self.represented_source_refs or (self.source_ref,)

    @property
    def source_objects(self) -> tuple[str, ...]:
        return self.represented_source_objects or (self.source_object,)


@dataclass(frozen=True, slots=True)
class RouteWidthPlan:
    line_id: str
    fit: PenWidthFit
    source_pen_match_status: str
    native_owned_nib_promotion: dict[str, object] | None = None

    @property
    def plotted_width_mm(self) -> float:
        return self.fit.plotted_width_mm


@dataclass(frozen=True, slots=True)
class StationMark:
    node_id: str
    name: str
    tier: str
    point: Point
    source_point: Point
    displacement_mm: float
    association_status: str
    line_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StationLabel:
    node_id: str
    text: str
    x_mm: float
    y_mm: float
    anchor: str
    cap_height_mm: float
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class TransitPlan:
    projector: Projector
    scale_tier: str
    route_strokes: tuple[PlannedRouteStroke, ...]
    context_strokes: tuple[PlannedContextStroke, ...]
    station_marks: tuple[StationMark, ...]
    station_labels: tuple[StationLabel, ...]
    omitted_station_labels: tuple[str, ...]
    station_association_issues: tuple[dict[str, object], ...]
    lane_pitch_mm: float
    simplification_tolerance_mm: float
    source_edge_count: int
    emitted_edge_memberships: int
    short_route_strokes: tuple[dict[str, object], ...]
    same_line_corridor_audit: dict[str, object]
    station_label_policy_audit: dict[str, object]
    route_width_plans: tuple[RouteWidthPlan, ...]
    route_target_width_mm: float
    route_target_maximum_width_mm: float
    lane_clearance_mm: float

    @property
    def route_width_by_line(self) -> dict[str, RouteWidthPlan]:
        return {item.line_id: item for item in self.route_width_plans}


def projector_for(
    network: TransitNetwork,
    rect: Rect,
    *,
    margin_fraction: float = DEFAULT_PROJECTOR_MARGIN_FRACTION,
) -> Projector:
    if not 0.0 <= margin_fraction < 0.25:
        raise MapPlotterError("Transit field margin fraction must be in [0, 0.25).")
    route_points = [point for edge in network.edges for point in edge.geometry]
    if len(route_points) < 2:
        raise MapPlotterError("A transit network needs geographic edge geometry.")
    usable = rect.inset(min(rect.width, rect.height) * margin_fraction)
    extent_policy: TransitProjectionExtent | None = None
    if network.kind in NAMED_OPERATOR_KINDS:
        extent_policy = named_operator_projection_extent(
            network_kind=network.kind,
            route_bounds=network.bbox(),
            target_metric_aspect=usable.width / usable.height,
            padding_fraction=0.0,
        )
        bounds = extent_policy.expanded_bounds
        lon0 = (bounds.west + bounds.east) / 2.0
        lat0 = (bounds.south + bounds.north) / 2.0
        points = [
            (bounds.west, bounds.south),
            (bounds.west, bounds.north),
            (bounds.east, bounds.south),
            (bounds.east, bounds.north),
        ]
    else:
        points = route_points
        lon0 = sum(point[0] for point in points) / len(points)
        lat0 = sum(point[1] for point in points) / len(points)
    metric = [
        (
            EARTH_RADIUS_M * radians(lon - lon0) * cos(radians(lat0)),
            EARTH_RADIUS_M * radians(lat - lat0),
        )
        for lon, lat in points
    ]
    xs = [point[0] for point in metric]
    ys = [point[1] for point in metric]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width_m = max_x - min_x
    height_m = max_y - min_y
    if width_m <= 0.0 or height_m <= 0.0:
        raise MapPlotterError("Transit extent is degenerate.")
    scale = min(usable.width / width_m, usable.height / height_m)
    used_width = width_m * scale
    used_height = height_m * scale
    return Projector(
        lon0=lon0,
        lat0=lat0,
        min_x_m=min_x,
        min_y_m=min_y,
        scale_mm_per_m=scale,
        offset_x_mm=usable.x + (usable.width - used_width) / 2.0,
        offset_y_mm=usable.y + (usable.height - used_height) / 2.0,
        rect=usable,
        source_width_m=width_m,
        source_height_m=height_m,
        extent_policy=extent_policy,
    )


def scale_tier(projector: Projector) -> str:
    """Classify detail by the geography's actual scale on this sheet."""

    denominator = projector.scale_denominator
    if denominator <= COMPACT_MAX_SCALE_DENOMINATOR:
        return "compact-network"
    if denominator <= URBAN_MAX_SCALE_DENOMINATOR:
        return "urban-network"
    if denominator <= REGIONAL_MAX_SCALE_DENOMINATOR:
        return "regional-network"
    return "national-network"


def route_target_width_for_scale(tier: str) -> float:
    """Return the default normal-service route width for one map scale.

    Compact systems carry the full four-pass 1 mm band. Urban, regional and
    national overviews retain a confident 0.8 mm hierarchy: the route must
    remain visibly dominant over 0.25/0.4 mm house context even when the full
    network is fitted to A3. Limited and seasonal service is separately capped
    at 0.6 mm. Every target is constructible from the owned coloured 0.4 mm nib
    with overlapping, never gapped, passes.
    """

    try:
        return SCALE_ROUTE_TARGETS_MM[tier]
    except KeyError as exc:  # pragma: no cover - internal tier invariant
        raise MapPlotterError(f"Unknown transit scale tier {tier!r}.") from exc


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        return hypot(point[0] - start[0], point[1] - start[1])
    t = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq,
        ),
    )
    projection = (start[0] + t * dx, start[1] + t * dy)
    return hypot(point[0] - projection[0], point[1] - projection[1])


def _point_segment_projection(point: Point, start: Point, end: Point) -> Point:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        return start
    t = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq,
        ),
    )
    return (start[0] + t * dx, start[1] + t * dy)


def _rdp_indices(points: Sequence[Point], tolerance: float) -> set[int]:
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start_index, end_index = stack.pop()
        start = points[start_index]
        end = points[end_index]
        maximum = -1.0
        candidate = -1
        for index in range(start_index + 1, end_index):
            distance = _point_segment_distance(points[index], start, end)
            if distance > maximum:
                maximum = distance
                candidate = index
        if candidate >= 0 and maximum > tolerance:
            keep.add(candidate)
            stack.extend(((start_index, candidate), (candidate, end_index)))
    return keep


def simplify_protected(
    points: Sequence[Point],
    *,
    tolerance_mm: float,
    protected_indices: Iterable[int] = (),
) -> list[Point]:
    if len(points) <= 2:
        return list(points)
    protected = {0, len(points) - 1, *protected_indices}
    if any(not 0 <= index < len(points) for index in protected):
        raise MapPlotterError("A protected transit vertex index is out of range.")
    ordered = sorted(protected)
    keep: set[int] = set()
    for start, end in zip(ordered, ordered[1:]):
        segment = points[start : end + 1]
        keep.update(start + index for index in _rdp_indices(segment, tolerance_mm))
    return [points[index] for index in sorted(keep)]


def _vertex_normals(points: Sequence[Point]) -> list[Point]:
    segment_normals: list[Point] = []
    for start, end in zip(points, points[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = hypot(dx, dy)
        segment_normals.append(
            (0.0, 0.0) if length <= 1e-12 else (-dy / length, dx / length)
        )
    result: list[Point] = []
    closed = (
        len(points) > 2
        and hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1]) <= 1e-9
    )
    for index in range(len(points)):
        if index == 0 and not closed:
            result.append(segment_normals[0])
            continue
        if index == len(points) - 1 and not closed:
            result.append(segment_normals[-1])
            continue
        before = segment_normals[-1] if index == 0 else segment_normals[index - 1]
        after = (
            segment_normals[0] if index == len(points) - 1 else segment_normals[index]
        )
        x = before[0] + after[0]
        y = before[1] + after[1]
        length = hypot(x, y)
        normal = after if length <= 1e-12 else (x / length, y / length)
        result.append(normal)
    if closed:
        result[-1] = result[0]
    return result


def offset_with_taper(
    points: Sequence[Point],
    *,
    offset_mm: float,
    taper_mm: float,
    taper_start: bool = True,
    taper_end: bool = True,
) -> list[Point]:
    if len(points) < 2 or abs(offset_mm) <= 1e-12:
        return list(points)
    cumulative = [0.0]
    for first, second in zip(points, points[1:]):
        cumulative.append(
            cumulative[-1] + hypot(second[0] - first[0], second[1] - first[1])
        )
    total = cumulative[-1]
    if total <= 1e-12:
        return list(points)
    angle_limited_taper = abs(offset_mm) / max(tan(radians(12.0)), 1e-9)
    taper = max(min(max(taper_mm, angle_limited_taper), total * 0.45), 1e-9)
    normals = _vertex_normals(points)
    result: list[Point] = []
    for index, point in enumerate(points):
        factors = [1.0]
        if taper_start:
            factors.append(cumulative[index] / taper)
        if taper_end:
            factors.append((total - cumulative[index]) / taper)
        factor = min(1.0, max(0.0, min(factors)))
        # Zero first derivative at both ends avoids a visible kink where a
        # separated shared-corridor lane reaches its full offset.
        factor = factor * factor * (3.0 - 2.0 * factor)
        normal = normals[index]
        result.append(
            (
                point[0] + normal[0] * offset_mm * factor,
                point[1] + normal[1] * offset_mm * factor,
            )
        )
    # Exact graph nodes are binding wherever a lane intentionally converges.
    # Compatible ordinary boundaries are internal chain vertices and therefore
    # retain their full offset instead of repeatedly pinching to the centreline.
    if taper_start:
        result[0] = points[0]
    if taper_end:
        result[-1] = points[-1]
    return result


def _station_lines(network: TransitNetwork) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {
        node.id: set() for node in network.nodes if node.is_station
    }
    for pattern in network.service_patterns:
        for station_id in pattern.station_ids:
            result.setdefault(station_id, set()).add(pattern.line_id)
    return {key: tuple(sorted(value)) for key, value in result.items()}


def _station_protected_vertices(
    network: TransitNetwork,
    projector: Projector,
) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {edge.id: set() for edge in network.edges}
    edge_page = {
        edge.id: [projector.point(lon, lat) for lon, lat in edge.geometry]
        for edge in network.edges
    }
    station_lines = _station_lines(network)
    for node in network.nodes:
        if not node.is_station:
            continue
        station = projector.point(node.lon, node.lat)
        relevant_lines = set(station_lines.get(node.id, ()))
        candidates: list[tuple[float, str, int]] = []
        for edge in network.edges:
            if relevant_lines and not relevant_lines.intersection(edge.line_ids):
                continue
            for index, point in enumerate(edge_page[edge.id]):
                candidates.append(
                    (
                        hypot(point[0] - station[0], point[1] - station[1]),
                        edge.id,
                        index,
                    )
                )
        if candidates:
            distance, edge_id, vertex_index = min(candidates)
            if distance <= STATION_ASSOCIATION_LIMIT_MM:
                result[edge_id].add(vertex_index)
    return result


def _fallback_width_fit(line: TransitLine, requested_width_mm: float) -> PenWidthFit:
    """Build a review-only width plan for an ink absent from the inventory.

    The physical path remains honest: it uses the line contract's nominal nib
    and adjacent offsets.  Its unresolved/approximate colour status continues
    to block production in the manifest.
    """

    nib_mm = line.pen.nominal_nib_mm
    physical_pen = PhysicalPen(
        ink=line.pen.ink,
        nominal_nib_mm=nib_mm,
        preview_color=line.colour.display_hex,
        id=line.pen.pen_id or line.pen.plot_key,
    )
    tolerance = max(0.05, requested_width_mm * 0.15)
    if abs(nib_mm - requested_width_mm) <= tolerance:
        return PenWidthFit(
            pen=physical_pen,
            requested_width_mm=requested_width_mm,
            stroke_count=1,
            offset_pitch_mm=0.0,
            plotted_width_mm=nib_mm,
            mode="single-nib",
        )
    for stroke_count in range(2, 7):
        pitch = (requested_width_mm - nib_mm) / (stroke_count - 1)
        if nib_mm * 0.5 - 1e-9 <= pitch <= nib_mm * 0.9 + 1e-9:
            return PenWidthFit(
                pen=physical_pen,
                requested_width_mm=requested_width_mm,
                stroke_count=stroke_count,
                offset_pitch_mm=pitch,
                plotted_width_mm=requested_width_mm,
                mode="parallel-offsets",
            )
    raise MapPlotterError(
        f"Transit line {line.id!r} cannot construct a {requested_width_mm:g} mm "
        f"route from its declared {nib_mm:g} mm {line.pen.ink} pen."
    )


def route_width_plan(
    line: TransitLine, *, normal_target_mm: float = DEFAULT_ROUTE_TARGET_MM
) -> RouteWidthPlan:
    target_mm = (
        min(normal_target_mm, SECONDARY_ROUTE_TARGET_MM)
        if line.service_class in {"seasonal-service", "limited-service"}
        else normal_target_mm
    )
    try:
        fit = fit_pen_width(
            ACTUAL_PEN_INVENTORY,
            ink=line.pen.ink,
            requested_width_mm=target_mm,
        )
    except MapPlotterError:
        fit = _fallback_width_fit(line, target_mm)
    if (
        abs(target_mm - DEFAULT_ROUTE_TARGET_MM) <= 1e-9
        and abs(fit.pen.mark_width_mm - DEFAULT_ROUTE_NIB_MM) <= 1e-9
        and fit.pen.ink.casefold() not in {"black", "white"}
    ):
        # Three 0.4 mm passes at 0.3 mm pitch are mathematically contiguous,
        # but their 0.1 mm overlaps fall below one pixel in the standard
        # 150 dpi proof. Four honest adjacent passes at 0.2 mm pitch keep the
        # same exact 1.0 mm union and give every seam 0.2 mm physical overlap.
        fit = PenWidthFit(
            pen=fit.pen,
            requested_width_mm=target_mm,
            stroke_count=COMPACT_COLOURED_ROUTE_STROKE_COUNT,
            offset_pitch_mm=COMPACT_COLOURED_ROUTE_PITCH_MM,
            plotted_width_mm=target_mm,
            mode="parallel-offsets",
        )
    return RouteWidthPlan(
        line_id=line.id,
        fit=fit,
        source_pen_match_status=line.pen.match_status,
    )


def _registry_bound_native_owned_nib_width_plan(
    network: TransitNetwork,
    line: TransitLine,
    *,
    scale_tier_name: str,
    scale_target_width_mm: float,
    route_target_maximum_width_mm: float,
    automatic_scale_target: bool,
) -> RouteWidthPlan | None:
    """Resolve the sole reviewed oversize native-nib product exception.

    Gold is owned only at 1.0 mm.  The dated Merseyrail product is an urban
    0.8 mm plate, so narrowing it would require an unowned pen.  Preserve the
    registry-bound Gold treatment with one native 1.0 mm pass, but only for
    the exact frozen product/network/line tuple.  No name, ink, or broad-pen
    heuristic is permitted to widen another map.
    """

    binding = (network.id, line.id)
    expected_binding = (
        MERSEYRAIL_NATIVE_WIDTH_NETWORK_ID,
        MERSEYRAIL_NATIVE_WIDTH_LINE_ID,
    )
    if binding != expected_binding:
        return None

    # Import locally so the general topology module is not initialized through
    # the dated product registry unless this exact frozen binding is present.
    from .transit_operator_registry import OPERATOR_REGISTRY

    product = OPERATOR_REGISTRY.by_key.get(MERSEYRAIL_NATIVE_WIDTH_OPERATOR_KEY)
    if (
        product is None
        or OPERATOR_REGISTRY.snapshot
        != MERSEYRAIL_NATIVE_WIDTH_REGISTRY_SNAPSHOT
        or product.id != MERSEYRAIL_NATIVE_WIDTH_PRODUCT_ID
        or product.operator_key != MERSEYRAIL_NATIVE_WIDTH_OPERATOR_KEY
        or product.presentation.pen_id != "gold-1"
        or abs(product.presentation.nib_mm - MERSEYRAIL_NATIVE_WIDTH_RESOLVED_MM)
        > 1e-9
        or product.presentation.ink.casefold() != "gold"
    ):
        raise MapPlotterError(
            "The Merseyrail native-width exception no longer matches the dated "
            "operator registry."
        )
    if (
        network.kind != "national-operator"
        or network.snapshot != "2026-08-06"
        or network.format_id != product.format_id
        or len(network.lines) != 1
        or not automatic_scale_target
        or scale_tier_name != MERSEYRAIL_NATIVE_WIDTH_SCALE_TIER
        or abs(scale_target_width_mm - MERSEYRAIL_NATIVE_WIDTH_BASELINE_MM) > 1e-9
    ):
        raise MapPlotterError(
            "The Merseyrail native-width exception is valid only for the exact "
            "dated urban individual-product contract and its automatic scale target."
        )
    if (
        line.pen.pen_id != product.presentation.pen_id
        or line.pen.ink.casefold() != product.presentation.ink.casefold()
        or abs(line.pen.nominal_nib_mm - product.presentation.nib_mm) > 1e-9
    ):
        raise MapPlotterError(
            "The Merseyrail native-width exception does not match its declared "
            "registry pen."
        )
    matches = tuple(
        pen
        for pen in ACTUAL_PEN_INVENTORY.pens
        if pen.identity == product.presentation.pen_id
    )
    if len(matches) != 1:
        raise MapPlotterError(
            "The Merseyrail native-width exception requires exactly one owned "
            "gold-1 pen."
        )
    physical = matches[0]
    if (
        physical.ink.casefold() != product.presentation.ink.casefold()
        or abs(physical.nominal_nib_mm - product.presentation.nib_mm) > 1e-9
        or abs(physical.mark_width_mm - MERSEYRAIL_NATIVE_WIDTH_RESOLVED_MM) > 1e-9
        or physical.mark_width_mm
        > route_target_maximum_width_mm + 1e-9
    ):
        raise MapPlotterError(
            "The owned Merseyrail Gold nib is absent, changed, or wider than the "
            "format maximum."
        )
    fit = PenWidthFit(
        pen=physical,
        requested_width_mm=physical.mark_width_mm,
        stroke_count=1,
        offset_pitch_mm=0.0,
        plotted_width_mm=physical.mark_width_mm,
        mode="single-nib",
    )
    promotion: dict[str, object] = {
        "policy_version": NATIVE_OWNED_NIB_PROMOTION_POLICY_VERSION,
        "product_id": product.id,
        "operator_key": product.operator_key,
        "registry_snapshot": OPERATOR_REGISTRY.snapshot,
        "registry_colour_status": product.presentation.colour_status,
        "network_id": network.id,
        "line_id": line.id,
        "scale_tier": scale_tier_name,
        "baseline_scale_target_width_mm": round(scale_target_width_mm, 6),
        "resolved_native_width_mm": round(physical.mark_width_mm, 6),
        "width_delta_mm": round(
            physical.mark_width_mm - scale_target_width_mm, 6
        ),
        "format_maximum_width_mm": round(route_target_maximum_width_mm, 6),
        "physical_pen_id": physical.identity,
        "ink": physical.ink,
        "nominal_nib_mm": round(physical.nominal_nib_mm, 6),
        "effective_width_mm": round(physical.mark_width_mm, 6),
        "stroke_count": 1,
        "one_pass": True,
        "colour_substitution": False,
        "source_pen_match_status": line.pen.match_status,
        "reason": (
            "Gold is owned only at 1.0 mm; use one native owned pass instead "
            "of inventing a 0.8 mm Gold nib or silently changing ink."
        ),
    }
    return RouteWidthPlan(
        line_id=line.id,
        fit=fit,
        source_pen_match_status=line.pen.match_status,
        native_owned_nib_promotion=promotion,
    )


def _line_offsets(
    edge: TransitEdge,
    lines: dict[str, TransitLine],
    plotted_widths: dict[str, float],
    clearance_mm: float,
) -> dict[str, float]:
    """Centre the full neighbouring ink envelopes, including mixed widths."""

    ordered = sorted(edge.line_ids, key=lambda value: (lines[value].order, value))
    if len(ordered) == 1:
        return {ordered[0]: 0.0}
    centres = [0.0]
    for previous, current in zip(ordered, ordered[1:]):
        centres.append(
            centres[-1]
            + plotted_widths[previous] / 2.0
            + clearance_mm
            + plotted_widths[current] / 2.0
        )
    left = min(
        centre - plotted_widths[line_id] / 2.0
        for centre, line_id in zip(centres, ordered)
    )
    right = max(
        centre + plotted_widths[line_id] / 2.0
        for centre, line_id in zip(centres, ordered)
    )
    envelope_centre = (left + right) / 2.0
    return {
        line_id: centre - envelope_centre for line_id, centre in zip(ordered, centres)
    }


def _ordered_line_ids(
    edge: TransitEdge, lines: dict[str, TransitLine]
) -> tuple[str, ...]:
    return tuple(sorted(edge.line_ids, key=lambda value: (lines[value].order, value)))


def _compatible_edge_chains(
    edges: Sequence[TransitEdge], lines: dict[str, TransitLine]
) -> list[tuple[tuple[tuple[TransitEdge, bool], ...], bool]]:
    """Return deterministic physical chains through ordinary graph boundaries.

    A boundary is ordinary only when exactly two distinct physical edges meet
    there and both have the same ordered display-line membership.  Edge source
    orientation is deliberately ignored: each returned bool orients that edge
    along the chain, so lane order cannot flip when adjacent source ways happen
    to have opposing directions.
    """

    edge_by_id = {edge.id: edge for edge in edges}
    incidences: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        incidences.setdefault(edge.from_node, []).append((edge.id, "from"))
        incidences.setdefault(edge.to_node, []).append((edge.id, "to"))
    links: dict[tuple[str, str], tuple[str, str]] = {}
    for values in incidences.values():
        if len(values) != 2 or values[0][0] == values[1][0]:
            continue
        first, second = values
        if _ordered_line_ids(edge_by_id[first[0]], lines) != _ordered_line_ids(
            edge_by_id[second[0]], lines
        ):
            continue
        links[first] = second
        links[second] = first

    unvisited = set(edge_by_id)
    open_start_heap = [
        (node_id, edge_id, side)
        for node_id, values in incidences.items()
        for edge_id, side in values
        if (edge_id, side) not in links
    ]
    heapq.heapify(open_start_heap)
    unvisited_edge_heap = list(edge_by_id)
    heapq.heapify(unvisited_edge_heap)
    result: list[tuple[tuple[tuple[TransitEdge, bool], ...], bool]] = []
    while unvisited:
        while open_start_heap and open_start_heap[0][1] not in unvisited:
            heapq.heappop(open_start_heap)
        if open_start_heap:
            _, edge_id, entry_side = heapq.heappop(open_start_heap)
        else:
            while (
                unvisited_edge_heap
                and unvisited_edge_heap[0] not in unvisited
            ):
                heapq.heappop(unvisited_edge_heap)
            if not unvisited_edge_heap:  # pragma: no cover - heap/set invariant
                raise MapPlotterError(
                    "Transit compatible-chain edge index became inconsistent."
                )
            edge_id = heapq.heappop(unvisited_edge_heap)
            edge = edge_by_id[edge_id]
            entry_side = min(((edge.from_node, "from"), (edge.to_node, "to")))[1]
        first_entry = (edge_id, entry_side)
        current = first_entry
        chain: list[tuple[TransitEdge, bool]] = []
        closed = False
        while current[0] in unvisited:
            current_edge = edge_by_id[current[0]]
            forward = current[1] == "from"
            chain.append((current_edge, forward))
            unvisited.remove(current_edge.id)
            exit_side = "to" if forward else "from"
            neighbour = links.get((current_edge.id, exit_side))
            if neighbour is None:
                break
            if neighbour == first_entry:
                closed = True
                break
            current = neighbour
        result.append((tuple(chain), closed))
    return result


def _join_strokes(
    records: Sequence[PlannedRouteStroke], *, tolerance_mm: float = 0.002
) -> list[PlannedRouteStroke]:
    """Join touching same-line strokes without inventing a connecting segment.

    Short source branches are seeded first. At odd-degree junctions this keeps
    a small but valid terminal/platform edge attached to a longer pen-down
    trail instead of letting a mainline-first pass strand that edge as a
    sub-floor standalone stroke. Connectivity still depends only on exact
    graph node identity; ordering never creates a geometric bridge.
    """

    decorated = [
        (_stroke_length(record), input_index, record)
        for input_index, record in enumerate(records)
    ]
    decorated.sort(
        key=lambda item: (item[0], item[2].line_id, item[2].edge_ids, item[1])
    )
    ordered = [item[2] for item in decorated]
    lengths = [item[0] for item in decorated]
    incidence_count: dict[tuple[str, str], int] = {}
    for record in records:
        for node_id in (record.start_node_id, record.end_node_id):
            key = (record.line_id, node_id)
            incidence_count[key] = incidence_count.get(key, 0) + 1

    # ``pending`` used to be rescanned from the beginning for both every trail
    # seed and every continuation.  Preserve that exact stable rank, but index
    # it by endpoint and use lazy-deletion heaps.  A record is never inserted
    # again, so no geometry or graph connectivity can be introduced here.
    active = [True] * len(ordered)
    endpoint_ranks: dict[tuple[str, str], list[int]] = defaultdict(list)
    seed_heap: list[tuple[int, float, str, tuple[str, ...], int]] = []
    for rank, (record, length) in enumerate(zip(ordered, lengths)):
        for node_id in {record.start_node_id, record.end_node_id}:
            heapq.heappush(endpoint_ranks[(record.line_id, node_id)], rank)
        terminal_priority = (
            0
            if min(
                incidence_count[(record.line_id, record.start_node_id)],
                incidence_count[(record.line_id, record.end_node_id)],
            )
            == 1
            else 1
        )
        heapq.heappush(
            seed_heap,
            (
                terminal_priority,
                length,
                record.line_id,
                record.edge_ids,
                rank,
            ),
        )

    def lowest_active_rank(line_id: str, node_id: str) -> int | None:
        ranks = endpoint_ranks.get((line_id, node_id))
        if not ranks:
            return None
        while ranks and not active[ranks[0]]:
            heapq.heappop(ranks)
        return ranks[0] if ranks else None

    output: list[PlannedRouteStroke] = []
    remaining = len(ordered)
    while remaining:
        while seed_heap and not active[seed_heap[0][-1]]:
            heapq.heappop(seed_heap)
        if not seed_heap:  # pragma: no cover - heap/activity invariant
            raise MapPlotterError("Transit stroke join seed index became inconsistent.")
        current_rank = heapq.heappop(seed_heap)[-1]
        current = ordered[current_rank]
        active[current_rank] = False
        remaining -= 1
        point_chunks = deque((list(current.points),))
        edge_id_chunks = deque((list(current.edge_ids),))
        start_node_id = current.start_node_id
        end_node_id = current.end_node_id
        start_point = current.points[0]
        end_point = current.points[-1]
        source_chunks = deque((list(current.source_refs),))
        represented_edge_chunks = deque(
            (list(current.source_membership_edge_ids),)
        )
        source_vertices = current.source_vertex_count
        max_offset = current.maximum_lane_offset_mm
        while True:
            candidate_ranks = [
                rank
                for rank in (
                    lowest_active_rank(current.line_id, start_node_id),
                    lowest_active_rank(current.line_id, end_node_id),
                )
                if rank is not None
            ]
            if not candidate_ranks:
                break
            candidate_rank = min(candidate_ranks)
            candidate = ordered[candidate_rank]
            candidate_points = list(candidate.points)
            candidate_edges = list(candidate.edge_ids)
            candidate_start = candidate.start_node_id
            candidate_end = candidate.end_node_id
            orientation: str | None = None
            if end_node_id == candidate_start:
                orientation = "append"
            elif end_node_id == candidate_end:
                candidate_points.reverse()
                candidate_edges.reverse()
                candidate_start, candidate_end = candidate_end, candidate_start
                orientation = "append"
            elif start_node_id == candidate_end:
                orientation = "prepend"
            elif start_node_id == candidate_start:
                candidate_points.reverse()
                candidate_edges.reverse()
                candidate_start, candidate_end = candidate_end, candidate_start
                orientation = "prepend"
            if orientation is None:  # pragma: no cover - endpoint-index invariant
                raise MapPlotterError(
                    "Transit stroke join endpoint index became inconsistent."
                )
            # Node identity is the connectivity proof.  The small numeric
            # check catches malformed projections without ever allowing
            # coincident but grade-separated nodes to join.
            join_first = (
                end_point if orientation == "append" else candidate_points[-1]
            )
            join_second = (
                candidate_points[0] if orientation == "append" else start_point
            )
            if (
                hypot(
                    join_first[0] - join_second[0],
                    join_first[1] - join_second[1],
                )
                > tolerance_mm
            ):
                raise MapPlotterError(
                    "Transit graph nodes share an ID but project to different positions."
                )
            active[candidate_rank] = False
            remaining -= 1
            if orientation == "append":
                point_chunks.append(candidate_points[1:])
                edge_id_chunks.append(candidate_edges)
                source_chunks.append(list(candidate.source_refs))
                represented_edge_chunks.append(
                    list(candidate.source_membership_edge_ids)
                )
                end_node_id = candidate_end
                end_point = candidate_points[-1]
            else:
                point_chunks.appendleft(candidate_points[:-1])
                edge_id_chunks.appendleft(candidate_edges)
                source_chunks.appendleft(list(candidate.source_refs))
                represented_edge_chunks.appendleft(
                    list(candidate.source_membership_edge_ids)
                )
                start_node_id = candidate_start
                start_point = candidate_points[0]
            source_vertices += candidate.source_vertex_count - 1
            max_offset = max(max_offset, candidate.maximum_lane_offset_mm)
        output.append(
            PlannedRouteStroke(
                line_id=current.line_id,
                edge_ids=tuple(
                    edge_id for chunk in edge_id_chunks for edge_id in chunk
                ),
                start_node_id=start_node_id,
                end_node_id=end_node_id,
                points=tuple(point for chunk in point_chunks for point in chunk),
                source_refs=tuple(
                    dict.fromkeys(
                        source for chunk in source_chunks for source in chunk
                    )
                ),
                maximum_lane_offset_mm=max_offset,
                simplification_tolerance_mm=current.simplification_tolerance_mm,
                source_vertex_count=source_vertices,
                represented_edge_ids=tuple(
                    dict.fromkeys(
                        edge_id
                        for chunk in represented_edge_chunks
                        for edge_id in chunk
                    )
                ),
            )
        )
    return output


def _stroke_length(record: PlannedRouteStroke) -> float:
    return sum(
        hypot(second[0] - first[0], second[1] - first[1])
        for first, second in zip(record.points, record.points[1:])
    )


def _line_tangent(line: LineString, distance: float, epsilon: float) -> Point:
    before = line.interpolate(max(0.0, distance - epsilon))
    after = line.interpolate(min(line.length, distance + epsilon))
    dx = float(after.x - before.x)
    dy = float(after.y - before.y)
    length = hypot(dx, dy)
    return (0.0, 0.0) if length <= 1e-12 else (dx / length, dy / length)


class _LineSpatialIndex:
    """Incremental deterministic spatial index over stable line positions.

    STRtree is immutable, so accepted paths are retained in power-of-two
    batches.  Each path participates in at most logarithmically many rebuilds,
    while every query searches only logarithmically many trees.  Returned
    indexes are sorted back into the original accepted-path order before the
    exact distance and tangent rules run.
    """

    def __init__(self, lines: Sequence[LineString] = ()) -> None:
        self._lines: list[LineString] = []
        self._batches: list[tuple[tuple[int, ...], STRtree]] = []
        for line in lines:
            self.add(line)

    @property
    def count(self) -> int:
        return len(self._lines)

    def add(self, line: LineString) -> None:
        index = len(self._lines)
        self._lines.append(line)
        indexes = (index,)
        while self._batches and len(self._batches[-1][0]) == len(indexes):
            previous_indexes, _ = self._batches.pop()
            indexes = (*previous_indexes, *indexes)
        tree = STRtree([self._lines[value] for value in indexes])
        self._batches.append((indexes, tree))

    def query_indices(
        self, geometry: object, *, distance: float
    ) -> tuple[int, ...]:
        return self.query_many_indices((geometry,), distance=distance)[0]

    def query_many_indices(
        self, geometries: Sequence[object], *, distance: float
    ) -> list[tuple[int, ...]]:
        if not geometries:
            return []
        indexes_by_geometry: list[list[int]] = [
            [] for _ in range(len(geometries))
        ]
        for global_indexes, tree in self._batches:
            pairs = tree.query(
                geometries,
                predicate="dwithin",
                distance=distance,
            )
            for geometry_index, local_index in zip(pairs[0], pairs[1]):
                indexes_by_geometry[int(geometry_index)].append(
                    global_indexes[int(local_index)]
                )
        return [tuple(sorted(indexes)) for indexes in indexes_by_geometry]


def _parallel_sample_intervals(
    candidate: LineString,
    references: Sequence[LineString],
    *,
    tolerance_mm: float,
    sample_step_mm: float,
    spatial_index: _LineSpatialIndex | None = None,
) -> list[tuple[float, float, int | None, float]]:
    """Classify source-path intervals against already retained same-line paths.

    A close crossing is not a corridor: tangent agreement is mandatory.  The
    returned intervals remain distances along the candidate source path, so a
    later cut can only retain or omit source geometry; it cannot bridge paths.
    """

    segment_count = max(1, ceil(candidate.length / sample_step_mm))
    distances = [
        candidate.length * index / segment_count for index in range(segment_count + 1)
    ]
    angular_cosine = cos(radians(SAME_LINE_CORRIDOR_MAXIMUM_ANGLE_DEGREES))
    epsilon = max(sample_step_mm, 0.02)
    reference_index = spatial_index or _LineSpatialIndex(references)
    if reference_index.count != len(references):
        raise MapPlotterError(
            "Same-line corridor spatial index does not match its references."
        )
    intervals = list(zip(distances, distances[1:]))
    centres = [(start + end) / 2.0 for start, end in intervals]
    sample_points = [candidate.interpolate(centre) for centre in centres]
    nearby_by_sample = reference_index.query_many_indices(
        sample_points,
        distance=tolerance_mm + 1e-9,
    )
    result: list[tuple[float, float, int | None, float]] = []
    for (start, end), centre, point, nearby_indexes in zip(
        intervals,
        centres,
        sample_points,
        nearby_by_sample,
    ):
        candidate_tangent = _line_tangent(candidate, centre, epsilon)
        matches: list[tuple[float, int]] = []
        for index in nearby_indexes:
            reference = references[index]
            separation = float(reference.distance(point))
            if separation > tolerance_mm + 1e-9:
                continue
            reference_distance = float(reference.project(point))
            reference_tangent = _line_tangent(reference, reference_distance, epsilon)
            alignment = abs(
                candidate_tangent[0] * reference_tangent[0]
                + candidate_tangent[1] * reference_tangent[1]
            )
            if alignment + 1e-12 >= angular_cosine:
                matches.append((separation, index))
        if matches:
            separation, index = min(matches)
            result.append((start, end, index, separation))
        else:
            result.append((start, end, None, 0.0))
    return result


def _collapsible_parallel_runs(
    intervals: Sequence[tuple[float, float, int | None, float]],
    *,
    minimum_run_mm: float,
) -> list[tuple[float, float, tuple[int, ...], float]]:
    result: list[tuple[float, float, tuple[int, ...], float]] = []
    index = 0
    while index < len(intervals):
        if intervals[index][2] is None:
            index += 1
            continue
        end_index = index + 1
        targets = {intervals[index][2]}
        maximum_separation = intervals[index][3]
        while end_index < len(intervals) and intervals[end_index][2] is not None:
            targets.add(intervals[end_index][2])
            maximum_separation = max(maximum_separation, intervals[end_index][3])
            end_index += 1
        start_distance = intervals[index][0]
        end_distance = intervals[end_index - 1][1]
        if end_distance - start_distance + 1e-9 >= minimum_run_mm:
            result.append(
                (
                    start_distance,
                    end_distance,
                    tuple(sorted(int(value) for value in targets if value is not None)),
                    maximum_separation,
                )
            )
        index = end_index
    return result


def _retained_ranges(
    length: float,
    collapsed: Sequence[tuple[float, float, tuple[int, ...], float]],
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end, _, _ in collapsed:
        if start > cursor + 1e-9:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if length > cursor + 1e-9:
        result.append((cursor, length))
    return result


def _paper_cut_node_id(
    record: PlannedRouteStroke, distance_mm: float, *, boundary: str
) -> str:
    payload = "\0".join(
        (
            record.line_id,
            *record.edge_ids,
            f"{distance_mm:.9f}",
            boundary,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"paper-cut-{digest}"


def _edge_id_sha256(values: Iterable[str]) -> str:
    payload = "\n".join(sorted(set(values))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_fragment(
    record: PlannedRouteStroke,
    line: LineString,
    *,
    start_mm: float,
    end_mm: float,
) -> PlannedRouteStroke:
    if start_mm <= 1e-9 and end_mm >= line.length - 1e-9:
        return record
    geometry = substring(line, start_mm, end_mm)
    if not isinstance(geometry, LineString) or geometry.is_empty:
        raise MapPlotterError("Same-line corridor generalization made an empty cut.")
    points = tuple((float(x), float(y)) for x, y in geometry.coords)
    if len(points) < 2:
        raise MapPlotterError("Same-line corridor generalization made a point cut.")
    return PlannedRouteStroke(
        line_id=record.line_id,
        edge_ids=record.edge_ids,
        start_node_id=(
            record.start_node_id
            if start_mm <= 1e-9
            else _paper_cut_node_id(record, start_mm, boundary="start")
        ),
        end_node_id=(
            record.end_node_id
            if end_mm >= line.length - 1e-9
            else _paper_cut_node_id(record, end_mm, boundary="end")
        ),
        points=points,
        source_refs=record.source_refs,
        maximum_lane_offset_mm=record.maximum_lane_offset_mm,
        simplification_tolerance_mm=record.simplification_tolerance_mm,
        source_vertex_count=len(points),
        represented_edge_ids=record.source_membership_edge_ids,
    )


def _sampled_directed_distance(
    source: LineString, target: object, *, sample_step_mm: float
) -> float:
    segment_count = max(1, ceil(source.length / sample_step_mm))
    distances = [
        source.length * index / segment_count for index in range(segment_count + 1)
    ]
    return max(
        float(target.distance(source.interpolate(distance)))  # type: ignore[attr-defined]
        for distance in distances
    )


def _generalize_same_line_corridors_one_pass(
    records: Sequence[PlannedRouteStroke],
    lines: dict[str, TransitLine],
) -> tuple[list[PlannedRouteStroke], dict[str, object]]:
    """Collapse physically indistinguishable same-colour directional tracks.

    The representative ink is always an unaltered substring of one planned
    source path.  Only sustained, near-parallel runs of the *same display line*
    are removed; different line IDs retain their independently offset lanes.
    Every source edge membership is carried on a representative path and the
    final source-to-display deviation is sampled and fail-closed.
    """

    output: list[PlannedRouteStroke] = []
    line_results: list[dict[str, object]] = []
    all_collapse_records: list[dict[str, object]] = []
    all_absorbed_fragment_records: list[dict[str, object]] = []
    overall_safe = True
    total_source_memberships = 0
    total_represented_memberships = 0
    for line_id in sorted(lines, key=lambda value: (lines[value].order, value)):
        source_records = [record for record in records if record.line_id == line_id]
        nib_mm = lines[line_id].pen.nominal_nib_mm
        tolerance_mm = nib_mm * SAME_LINE_CORRIDOR_TOLERANCE_NIBS
        closed_loop_tolerance_mm = nib_mm * SAME_LINE_CLOSED_LOOP_TOLERANCE_NIBS
        sample_step_mm = nib_mm * SAME_LINE_CORRIDOR_SAMPLE_STEP_NIBS
        minimum_run_mm = nib_mm * SAME_LINE_CORRIDOR_MINIMUM_RUN_NIBS
        accepted: list[PlannedRouteStroke] = []
        accepted_lines: list[LineString] = []
        accepted_spatial_index = _LineSpatialIndex()
        collapse_records: list[dict[str, object]] = []
        absorbed_fragment_records: list[dict[str, object]] = []
        absorbed_fragment_length_mm = 0.0
        ordered = sorted(
            source_records,
            key=lambda record: (
                -_stroke_length(record),
                record.edge_ids,
                record.start_node_id,
                record.end_node_id,
            ),
        )
        for source_record in ordered:
            source_line = LineString(source_record.points)
            collapsed: list[tuple[float, float, tuple[int, ...], float]]
            closed_loop_matches: list[tuple[float, int]] = []
            if source_line.is_ring:
                for index in accepted_spatial_index.query_indices(
                    source_line,
                    distance=closed_loop_tolerance_mm + 1e-9,
                ):
                    reference = accepted_lines[index]
                    if (
                        not reference.is_ring
                        or max(source_line.length, reference.length)
                        / min(source_line.length, reference.length)
                        > 1.02
                    ):
                        continue
                    separation = float(
                        source_line.hausdorff_distance(reference)
                    )
                    if separation <= closed_loop_tolerance_mm + 1e-9:
                        closed_loop_matches.append((separation, index))
            if closed_loop_matches:
                closed_separation, closed_target_index = min(closed_loop_matches)
                collapsed = [
                    (
                        0.0,
                        float(source_line.length),
                        (closed_target_index,),
                        closed_separation,
                    )
                ]
                collapse_rule = "closed-loop-directional-pair"
                applied_tolerance_mm = closed_loop_tolerance_mm
            else:
                intervals = _parallel_sample_intervals(
                    source_line,
                    accepted_lines,
                    tolerance_mm=tolerance_mm,
                    sample_step_mm=sample_step_mm,
                    spatial_index=accepted_spatial_index,
                )
                collapsed = _collapsible_parallel_runs(
                    intervals, minimum_run_mm=minimum_run_mm
                )
                collapse_rule = "sustained-sub-nib-parallel-run"
                applied_tolerance_mm = tolerance_mm
            for start, end, target_indexes, maximum_separation in collapsed:
                target_edge_ids = sorted(
                    {
                        edge_id
                        for index in target_indexes
                        for edge_id in accepted[index].edge_ids
                    }
                )
                collapse_records.append(
                    {
                        "source_representative_edge_count": len(
                            set(source_record.edge_ids)
                        ),
                        "source_representative_edge_sha256": _edge_id_sha256(
                            source_record.edge_ids
                        ),
                        "source_membership_edge_count": len(
                            set(source_record.source_membership_edge_ids)
                        ),
                        "source_membership_edge_sha256": _edge_id_sha256(
                            source_record.source_membership_edge_ids
                        ),
                        "target_representative_edge_count": len(target_edge_ids),
                        "target_representative_edge_sha256": _edge_id_sha256(
                            target_edge_ids
                        ),
                        "rule": collapse_rule,
                        "applied_tolerance_mm": round(applied_tolerance_mm, 6),
                        "start_mm": round(start, 6),
                        "end_mm": round(end, 6),
                        "length_mm": round(end - start, 6),
                        "maximum_sampled_separation_mm": round(maximum_separation, 6),
                    }
                )
                # Bind omitted source memberships to every physical path that
                # represents the collapsed run. This is provenance only; no
                # point or graph-node geometry is changed or connected.
                for target_index in target_indexes:
                    target = accepted[target_index]
                    accepted[target_index] = replace(
                        target,
                        source_refs=tuple(
                            dict.fromkeys(
                                (*target.source_refs, *source_record.source_refs)
                            )
                        ),
                        represented_edge_ids=tuple(
                            dict.fromkeys(
                                (
                                    *target.source_membership_edge_ids,
                                    *source_record.source_membership_edge_ids,
                                )
                            )
                        ),
                    )
            retained_ranges = _retained_ranges(source_line.length, collapsed)
            for start, end in retained_ranges:
                fragment_geometry = substring(source_line, start, end)
                if (
                    collapsed
                    and end - start + 1e-9 < minimum_run_mm
                    and isinstance(fragment_geometry, LineString)
                    and accepted_lines
                ):
                    maximum_fragment_deviation = _sampled_directed_distance(
                        fragment_geometry,
                        unary_union(accepted_lines),
                        sample_step_mm=sample_step_mm,
                    )
                    if (
                        maximum_fragment_deviation
                        <= tolerance_mm + sample_step_mm + 1e-9
                    ):
                        absorbed_fragment_records.append(
                            {
                                "source_membership_edge_count": len(
                                    set(source_record.source_membership_edge_ids)
                                ),
                                "source_membership_edge_sha256": _edge_id_sha256(
                                    source_record.source_membership_edge_ids
                                ),
                                "start_mm": round(start, 6),
                                "end_mm": round(end, 6),
                                "length_mm": round(end - start, 6),
                                "maximum_sampled_source_to_display_deviation_mm": round(
                                    maximum_fragment_deviation, 6
                                ),
                                "reason": (
                                    "sub-three-nib transition fragment already "
                                    "covered by retained same-line ink"
                                ),
                            }
                        )
                        absorbed_fragment_length_mm += end - start
                        continue
                fragment = _source_fragment(
                    source_record,
                    source_line,
                    start_mm=start,
                    end_mm=end,
                )
                accepted.append(fragment)
                accepted_line = LineString(fragment.points)
                accepted_lines.append(accepted_line)
                accepted_spatial_index.add(accepted_line)

        # The accepted geometry must cover every input source path inside the
        # declared paper tolerance, and it must contain no further sustained
        # same-line near-parallel double pass.
        display_geometry = unary_union(accepted_lines)
        maximum_source_deviation = max(
            (
                _sampled_directed_distance(
                    LineString(record.points),
                    display_geometry,
                    sample_step_mm=sample_step_mm,
                )
                for record in source_records
            ),
            default=0.0,
        )
        unresolved: list[dict[str, object]] = []
        unresolved_spatial_index = _LineSpatialIndex()
        for index, record in enumerate(accepted):
            candidate = LineString(record.points)
            intervals = _parallel_sample_intervals(
                candidate,
                accepted_lines[:index],
                tolerance_mm=tolerance_mm,
                sample_step_mm=sample_step_mm,
                spatial_index=unresolved_spatial_index,
            )
            for (
                start,
                end,
                target_indexes,
                maximum_separation,
            ) in _collapsible_parallel_runs(intervals, minimum_run_mm=minimum_run_mm):
                unresolved.append(
                    {
                        "representative_edge_ids": list(record.edge_ids),
                        "target_indexes": list(target_indexes),
                        "length_mm": round(end - start, 6),
                        "maximum_sampled_separation_mm": round(maximum_separation, 6),
                    }
                )
            unresolved_spatial_index.add(accepted_lines[index])
        source_edge_ids = {
            edge_id
            for record in source_records
            for edge_id in record.source_membership_edge_ids
        }
        represented_edge_ids = {
            edge_id
            for record in accepted
            for edge_id in record.source_membership_edge_ids
        }
        total_source_memberships += len(source_edge_ids)
        total_represented_memberships += len(represented_edge_ids)
        parity = represented_edge_ids == source_edge_ids
        line_safe = (
            parity
            and not unresolved
            and maximum_source_deviation <= tolerance_mm + sample_step_mm + 1e-9
        )
        overall_safe = overall_safe and line_safe
        source_distance = sum(_stroke_length(record) for record in source_records)
        output_distance = sum(_stroke_length(record) for record in accepted)
        line_result: dict[str, object] = {
            "line_id": line_id,
            "nominal_nib_mm": nib_mm,
            "collapse_tolerance_mm": round(tolerance_mm, 6),
            "closed_loop_directional_tolerance_mm": round(closed_loop_tolerance_mm, 6),
            "minimum_parallel_run_mm": round(minimum_run_mm, 6),
            "input_path_count": len(source_records),
            "output_path_count": len(accepted),
            "input_pen_down_distance_mm": round(source_distance, 6),
            "output_pen_down_distance_mm": round(output_distance, 6),
            "avoided_duplicate_pen_down_distance_mm": round(
                source_distance - output_distance, 6
            ),
            "source_edge_membership_count": len(source_edge_ids),
            "represented_edge_membership_count": len(represented_edge_ids),
            "source_edge_membership_sha256": _edge_id_sha256(source_edge_ids),
            "represented_edge_membership_sha256": _edge_id_sha256(represented_edge_ids),
            "edge_membership_parity": parity,
            "maximum_sampled_source_to_display_deviation_mm": round(
                maximum_source_deviation, 6
            ),
            "collapsed_parallel_run_count": len(collapse_records),
            "absorbed_short_transition_fragment_count": len(absorbed_fragment_records),
            "absorbed_short_transition_length_mm": round(
                absorbed_fragment_length_mm, 6
            ),
            "unresolved_parallel_run_count": len(unresolved),
            "unresolved_parallel_runs": unresolved,
            "safe": line_safe,
        }
        line_results.append(line_result)
        all_collapse_records.extend(
            {"line_id": line_id, **record} for record in collapse_records
        )
        all_absorbed_fragment_records.extend(
            {"line_id": line_id, **record} for record in absorbed_fragment_records
        )
        output.extend(accepted)

    audit: dict[str, object] = {
        "policy_version": SAME_LINE_CORRIDOR_POLICY_VERSION,
        "geometry_policy": (
            "retain unaltered planned source-path substrings; collapse only "
            "sustained sub-nib near-parallel runs of the same display line; "
            "closed reciprocal loops may use the separately declared one-sample "
            "quantization allowance"
        ),
        "different_line_lanes_preserved": True,
        "created_connector_count": 0,
        "tolerance_nib_widths": SAME_LINE_CORRIDOR_TOLERANCE_NIBS,
        "closed_loop_directional_tolerance_nib_widths": (
            SAME_LINE_CLOSED_LOOP_TOLERANCE_NIBS
        ),
        "minimum_parallel_run_nib_widths": SAME_LINE_CORRIDOR_MINIMUM_RUN_NIBS,
        "sample_step_nib_widths": SAME_LINE_CORRIDOR_SAMPLE_STEP_NIBS,
        "maximum_parallel_angle_degrees": SAME_LINE_CORRIDOR_MAXIMUM_ANGLE_DEGREES,
        "input_path_count": len(records),
        "output_path_count": len(output),
        "input_pen_down_distance_mm": round(
            sum(_stroke_length(record) for record in records), 6
        ),
        "output_pen_down_distance_mm": round(
            sum(_stroke_length(record) for record in output), 6
        ),
        "source_edge_memberships": total_source_memberships,
        "represented_edge_memberships": total_represented_memberships,
        "edge_membership_parity": all(
            record["edge_membership_parity"] is True for record in line_results
        ),
        "collapse_records": all_collapse_records,
        "absorbed_short_transition_fragments": all_absorbed_fragment_records,
        "line_results": line_results,
        "safe": overall_safe,
    }
    return output, audit


def generalize_same_line_corridors(
    records: Sequence[PlannedRouteStroke],
    lines: dict[str, TransitLine],
) -> tuple[list[PlannedRouteStroke], dict[str, object]]:
    """Reach a fail-closed fixed point of the one-pass corridor reducer."""

    original = list(records)
    current = list(records)
    iteration_results: list[dict[str, object]] = []
    aggregate_collapse_records: list[dict[str, object]] = []
    aggregate_absorbed_records: list[dict[str, object]] = []
    previous_unresolved_count: int | None = None
    final_pass_audit: dict[str, object] | None = None

    for iteration in range(1, SAME_LINE_CORRIDOR_MAX_FIXED_POINT_ITERATIONS + 1):
        output, pass_audit = _generalize_same_line_corridors_one_pass(
            current, lines
        )
        raw_line_results = pass_audit["line_results"]
        assert isinstance(raw_line_results, list)
        unresolved_count = sum(
            int(record["unresolved_parallel_run_count"])
            for record in raw_line_results
            if isinstance(record, dict)
        )
        input_distance = sum(_stroke_length(record) for record in current)
        output_distance = sum(_stroke_length(record) for record in output)
        path_count_decreased = len(output) < len(current)
        distance_decreased = output_distance < input_distance - 1e-9
        membership_parity = pass_audit["edge_membership_parity"] is True
        local_deviation_safe = all(
            float(record["maximum_sampled_source_to_display_deviation_mm"])
            <= float(record["collapse_tolerance_mm"])
            + float(record["nominal_nib_mm"])
            * SAME_LINE_CORRIDOR_SAMPLE_STEP_NIBS
            + 1e-9
            for record in raw_line_results
            if isinstance(record, dict)
        )
        if not membership_parity or not local_deviation_safe:
            raise MapPlotterError(
                "Same-line paper corridor fixed point violated membership or "
                "source-deviation safety."
            )
        if previous_unresolved_count is not None and (
            unresolved_count >= previous_unresolved_count
            or not (path_count_decreased or distance_decreased)
        ):
            raise MapPlotterError(
                "Same-line paper corridor fixed point made no strict progress: "
                f"{previous_unresolved_count} to {unresolved_count} unresolved runs, "
                f"{len(current)} to {len(output)} paths, and "
                f"{input_distance:.6f} to {output_distance:.6f} mm."
            )

        raw_collapses = pass_audit["collapse_records"]
        raw_absorbed = pass_audit["absorbed_short_transition_fragments"]
        assert isinstance(raw_collapses, list)
        assert isinstance(raw_absorbed, list)
        iteration_collapses = [
            {"fixed_point_iteration": iteration, **record}
            for record in raw_collapses
            if isinstance(record, dict)
        ]
        iteration_absorbed = [
            {"fixed_point_iteration": iteration, **record}
            for record in raw_absorbed
            if isinstance(record, dict)
        ]
        aggregate_collapse_records.extend(iteration_collapses)
        aggregate_absorbed_records.extend(iteration_absorbed)
        iteration_results.append(
            {
                "iteration": iteration,
                "input_path_count": len(current),
                "output_path_count": len(output),
                "input_pen_down_distance_mm": round(input_distance, 6),
                "output_pen_down_distance_mm": round(output_distance, 6),
                "unresolved_parallel_run_count": unresolved_count,
                "collapse_record_count": len(iteration_collapses),
                "absorbed_short_transition_fragment_count": len(
                    iteration_absorbed
                ),
                "edge_membership_parity": membership_parity,
                "local_source_deviation_safe": local_deviation_safe,
                "strict_progress_required": iteration > 1,
                "path_count_decreased": path_count_decreased,
                "pen_down_distance_decreased": distance_decreased,
            }
        )
        current = output
        final_pass_audit = pass_audit
        if unresolved_count == 0:
            break
        previous_unresolved_count = unresolved_count
    else:  # pragma: no cover - cap exercised through a forced unit test
        raise MapPlotterError(
            "Same-line paper corridor fixed point exceeded "
            f"{SAME_LINE_CORRIDOR_MAX_FIXED_POINT_ITERATIONS} iterations."
        )

    assert final_pass_audit is not None
    final_line_results_raw = final_pass_audit["line_results"]
    assert isinstance(final_line_results_raw, list)
    final_pass_by_line = {
        str(record["line_id"]): record
        for record in final_line_results_raw
        if isinstance(record, dict)
    }
    final_line_results: list[dict[str, object]] = []
    total_source_memberships = 0
    total_represented_memberships = 0
    overall_safe = True
    for line_id in sorted(lines, key=lambda value: (lines[value].order, value)):
        source_records = [record for record in original if record.line_id == line_id]
        output_records = [record for record in current if record.line_id == line_id]
        source_edge_ids = {
            edge_id
            for record in source_records
            for edge_id in record.source_membership_edge_ids
        }
        represented_edge_ids = {
            edge_id
            for record in output_records
            for edge_id in record.source_membership_edge_ids
        }
        total_source_memberships += len(source_edge_ids)
        total_represented_memberships += len(represented_edge_ids)
        parity = represented_edge_ids == source_edge_ids
        display_geometry = unary_union(
            [LineString(record.points) for record in output_records]
        )
        nib_mm = lines[line_id].pen.nominal_nib_mm
        tolerance_mm = nib_mm * SAME_LINE_CORRIDOR_TOLERANCE_NIBS
        sample_step_mm = nib_mm * SAME_LINE_CORRIDOR_SAMPLE_STEP_NIBS
        maximum_source_deviation = max(
            (
                _sampled_directed_distance(
                    LineString(record.points),
                    display_geometry,
                    sample_step_mm=sample_step_mm,
                )
                for record in source_records
            ),
            default=0.0,
        )
        final_pass_line = final_pass_by_line[line_id]
        unresolved = final_pass_line["unresolved_parallel_runs"]
        assert isinstance(unresolved, list)
        line_safe = (
            parity
            and not unresolved
            and maximum_source_deviation
            <= tolerance_mm + sample_step_mm + 1e-9
        )
        overall_safe = overall_safe and line_safe
        line_collapses = [
            record
            for record in aggregate_collapse_records
            if record.get("line_id") == line_id
        ]
        line_absorbed = [
            record
            for record in aggregate_absorbed_records
            if record.get("line_id") == line_id
        ]
        absorbed_length_mm = 0.0
        for record in line_absorbed:
            length_value = record.get("length_mm")
            if isinstance(length_value, (int, float)) and not isinstance(
                length_value, bool
            ):
                absorbed_length_mm += float(length_value)
        source_distance = sum(_stroke_length(record) for record in source_records)
        output_distance = sum(_stroke_length(record) for record in output_records)
        final_line_results.append(
            {
                "line_id": line_id,
                "nominal_nib_mm": nib_mm,
                "collapse_tolerance_mm": round(tolerance_mm, 6),
                "closed_loop_directional_tolerance_mm": round(
                    nib_mm * SAME_LINE_CLOSED_LOOP_TOLERANCE_NIBS, 6
                ),
                "minimum_parallel_run_mm": round(
                    nib_mm * SAME_LINE_CORRIDOR_MINIMUM_RUN_NIBS, 6
                ),
                "input_path_count": len(source_records),
                "output_path_count": len(output_records),
                "input_pen_down_distance_mm": round(source_distance, 6),
                "output_pen_down_distance_mm": round(output_distance, 6),
                "avoided_duplicate_pen_down_distance_mm": round(
                    source_distance - output_distance, 6
                ),
                "source_edge_membership_count": len(source_edge_ids),
                "represented_edge_membership_count": len(represented_edge_ids),
                "source_edge_membership_sha256": _edge_id_sha256(source_edge_ids),
                "represented_edge_membership_sha256": _edge_id_sha256(
                    represented_edge_ids
                ),
                "edge_membership_parity": parity,
                "maximum_sampled_source_to_display_deviation_mm": round(
                    maximum_source_deviation, 6
                ),
                "collapsed_parallel_run_count": len(line_collapses),
                "absorbed_short_transition_fragment_count": len(line_absorbed),
                "absorbed_short_transition_length_mm": round(
                    absorbed_length_mm, 6
                ),
                "unresolved_parallel_run_count": len(unresolved),
                "unresolved_parallel_runs": unresolved,
                "fixed_point_iteration_count": len(iteration_results),
                "safe": line_safe,
            }
        )

    if not overall_safe:
        unsafe_lines = [
            str(record["line_id"])
            for record in final_line_results
            if record["safe"] is not True
        ]
        raise MapPlotterError(
            "Unsafe same-line paper corridor generalization for: "
            + ", ".join(unsafe_lines)
        )

    audit: dict[str, object] = {
        "policy_version": SAME_LINE_CORRIDOR_POLICY_VERSION,
        "geometry_policy": (
            "retain unaltered planned source-path substrings; collapse only "
            "sustained sub-nib near-parallel runs of the same display line; "
            "closed reciprocal loops may use the separately declared one-sample "
            "quantization allowance; repeat the identical reducer to a bounded "
            "strictly-progressing fixed point"
        ),
        "different_line_lanes_preserved": True,
        "created_connector_count": 0,
        "tolerance_nib_widths": SAME_LINE_CORRIDOR_TOLERANCE_NIBS,
        "closed_loop_directional_tolerance_nib_widths": (
            SAME_LINE_CLOSED_LOOP_TOLERANCE_NIBS
        ),
        "minimum_parallel_run_nib_widths": SAME_LINE_CORRIDOR_MINIMUM_RUN_NIBS,
        "sample_step_nib_widths": SAME_LINE_CORRIDOR_SAMPLE_STEP_NIBS,
        "maximum_parallel_angle_degrees": SAME_LINE_CORRIDOR_MAXIMUM_ANGLE_DEGREES,
        "fixed_point_policy": {
            "maximum_iteration_count": (
                SAME_LINE_CORRIDOR_MAX_FIXED_POINT_ITERATIONS
            ),
            "actual_iteration_count": len(iteration_results),
            "converged": True,
            "final_unresolved_parallel_run_count": 0,
            "retry_requires_fewer_unresolved_runs": True,
            "retry_requires_path_or_pen_distance_reduction": True,
            "original_source_membership_and_deviation_rechecked": True,
            "iterations": iteration_results,
        },
        "input_path_count": len(original),
        "output_path_count": len(current),
        "input_pen_down_distance_mm": round(
            sum(_stroke_length(record) for record in original), 6
        ),
        "output_pen_down_distance_mm": round(
            sum(_stroke_length(record) for record in current), 6
        ),
        "source_edge_memberships": total_source_memberships,
        "represented_edge_memberships": total_represented_memberships,
        "edge_membership_parity": total_source_memberships
        == total_represented_memberships
        and all(record["edge_membership_parity"] is True for record in final_line_results),
        "collapse_records": aggregate_collapse_records,
        "absorbed_short_transition_fragments": aggregate_absorbed_records,
        "line_results": final_line_results,
        "safe": overall_safe,
    }
    return current, audit


def _bounds_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    gap: float = 0.45,
) -> bool:
    return not (
        first[2] + gap < second[0]
        or second[2] + gap < first[0]
        or first[3] + gap < second[1]
        or second[3] + gap < first[1]
    )


def _station_symbol_radius(mark: StationMark) -> float:
    if mark.tier == "interchange":
        return 1.62
    if mark.tier == "terminal":
        return 1.0
    return 0.68


def _rectangles_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] < second[0]
        or second[2] < first[0]
        or first[3] < second[1]
        or second[3] < first[1]
    )


def _segment_intersects_bounds(
    start: Point,
    end: Point,
    bounds: tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky segment/closed-rectangle intersection."""

    left, top, right, bottom = bounds
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    lower = 0.0
    upper = 1.0
    for direction, distance in (
        (-dx, start[0] - left),
        (dx, right - start[0]),
        (-dy, start[1] - top),
        (dy, bottom - start[1]),
    ):
        if abs(direction) <= 1e-12:
            if distance < 0.0:
                return False
            continue
        ratio = distance / direction
        if direction < 0.0:
            if ratio > upper:
                return False
            lower = max(lower, ratio)
        else:
            if ratio < lower:
                return False
            upper = min(upper, ratio)
    return lower <= upper


def transit_label_furniture_bounds(
    field: Rect,
) -> tuple[tuple[float, float, float, float], ...]:
    """Conservative reserved envelopes for renderer-owned map furniture."""

    # The renderer's north mark is centred 8 mm from the right and runs from
    # 7 mm to roughly 19.6 mm below the top once its N is included.  The scale
    # bar starts 7 mm from the left, is at most 34 mm long, and occupies the
    # bottom 8.2 mm including its label.  These bounds add the same physical
    # clearance used around route and station ink.
    return (
        (
            field.right - 11.15,
            field.top + 6.35,
            field.right - 4.85,
            field.top + 20.25,
        ),
        (
            field.left + 6.35,
            field.bottom - 8.85,
            field.left + 41.65,
            field.bottom - 2.25,
        ),
    )


def _label_candidates(
    mark: StationMark,
    *,
    width: float,
    cap_height_mm: float,
) -> Iterable[tuple[float, float, str, tuple[float, float, float, float]]]:
    symbol_envelope = (
        _station_symbol_radius(mark) + STATION_SYMBOL_NIB_MM / 2.0 + LABEL_CLEARANCE_MM
    )
    for extra in (0.0, 2.4, 4.8, 7.2):
        distance = symbol_envelope + LABEL_TEXT_PADDING_MM + extra
        placements = (
            (distance, 0.0, "start", "middle"),
            (-distance, 0.0, "end", "middle"),
            (0.0, -distance, "middle", "above"),
            (0.0, distance, "middle", "below"),
            (distance, -distance, "start", "above"),
            (-distance, -distance, "end", "above"),
            (distance, distance, "start", "below"),
            (-distance, distance, "end", "below"),
        )
        for dx, dy, anchor, vertical in placements:
            x = mark.point[0] + dx
            if vertical == "above":
                y = mark.point[1] + dy - cap_height_mm
            elif vertical == "below":
                y = mark.point[1] + dy
            else:
                y = mark.point[1] - cap_height_mm / 2.0
            left = (
                x
                if anchor == "start"
                else x - width
                if anchor == "end"
                else x - width / 2.0
            )
            bounds = (
                left - LABEL_TEXT_PADDING_MM,
                y - LABEL_TEXT_PADDING_MM,
                left + width + LABEL_TEXT_PADDING_MM,
                y + cap_height_mm + LABEL_TEXT_PADDING_MM,
            )
            yield x, y, anchor, bounds


def plan_station_labels(
    marks: Sequence[StationMark],
    field: Rect,
    *,
    policy: str,
    cap_height_mm: float = 2.1,
    route_strokes: Sequence[PlannedRouteStroke] = (),
    line_nib_mm: dict[str, float] | None = None,
) -> tuple[list[StationLabel], list[str]]:
    if policy not in {"none", "key", "all"}:
        raise MapPlotterError("Station-label policy must be none, key, or all.")
    if policy == "none":
        return [], [mark.node_id for mark in marks]
    tier_order = {"terminal": 0, "interchange": 1, "major": 2, "local": 3}
    candidates = sorted(
        marks, key=lambda mark: (tier_order.get(mark.tier, 3), mark.name, mark.node_id)
    )
    if policy == "key":
        candidates = [
            mark
            for mark in candidates
            if mark.tier in {"terminal", "interchange", "major"}
        ]
    placed: list[StationLabel] = []
    occupied: list[tuple[float, float, float, float]] = []
    route_nibs = line_nib_mm or {}
    safe_field = field.inset(LABEL_FRAME_CLEARANCE_MM)
    furniture = transit_label_furniture_bounds(field)
    station_obstacles = [
        (
            mark.point[0] - _station_symbol_radius(mark) - STATION_SYMBOL_NIB_MM / 2.0,
            mark.point[1] - _station_symbol_radius(mark) - STATION_SYMBOL_NIB_MM / 2.0,
            mark.point[0] + _station_symbol_radius(mark) + STATION_SYMBOL_NIB_MM / 2.0,
            mark.point[1] + _station_symbol_radius(mark) + STATION_SYMBOL_NIB_MM / 2.0,
        )
        for mark in marks
    ]
    candidate_ids = {mark.node_id for mark in candidates}
    omitted = [mark.node_id for mark in marks if mark.node_id not in candidate_ids]
    for mark in candidates:
        width = text_width_mm(mark.name.upper(), cap_height_mm=cap_height_mm)
        label: StationLabel | None = None
        for x, y, anchor, bounds in _label_candidates(
            mark, width=width, cap_height_mm=cap_height_mm
        ):
            if (
                bounds[0] < safe_field.left
                or bounds[1] < safe_field.top
                or bounds[2] > safe_field.right
                or bounds[3] > safe_field.bottom
                or any(_rectangles_intersect(bounds, item) for item in furniture)
                or any(
                    _rectangles_intersect(bounds, item) for item in station_obstacles
                )
                or any(
                    _segment_intersects_bounds(
                        start,
                        end,
                        (
                            bounds[0]
                            - route_nibs.get(stroke.line_id, DEFAULT_ROUTE_NIB_MM) / 2.0
                            - LABEL_CLEARANCE_MM,
                            bounds[1]
                            - route_nibs.get(stroke.line_id, DEFAULT_ROUTE_NIB_MM) / 2.0
                            - LABEL_CLEARANCE_MM,
                            bounds[2]
                            + route_nibs.get(stroke.line_id, DEFAULT_ROUTE_NIB_MM) / 2.0
                            + LABEL_CLEARANCE_MM,
                            bounds[3]
                            + route_nibs.get(stroke.line_id, DEFAULT_ROUTE_NIB_MM) / 2.0
                            + LABEL_CLEARANCE_MM,
                        ),
                    )
                    for stroke in route_strokes
                    for start, end in zip(stroke.points, stroke.points[1:])
                )
                or any(
                    _bounds_overlap(bounds, existing, gap=LABEL_TO_LABEL_GAP_MM)
                    for existing in occupied
                )
            ):
                continue
            label = StationLabel(
                mark.node_id, mark.name.upper(), x, y, anchor, cap_height_mm, bounds
            )
            break
        if label is None:
            omitted.append(mark.node_id)
            continue
        placed.append(label)
        occupied.append(label.bounds)
    return placed, sorted(set(omitted))


_ASSEMBLED_CONTEXT_KINDS = frozenset(
    {
        "coastline",
        "roads-major",
        "roads-secondary",
        "roads-local",
        "roads-other",
        "road-areas",
        "paths",
        "railways",
        "water-lines",
    }
)


def _signed_ring_area(points: Sequence[Point]) -> float:
    """Return twice the signed area of one explicitly closed paper-space ring."""

    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:], strict=False)
    )


def _simplify_closed_context_ring(
    points: Sequence[Point], *, tolerance_mm: float
) -> tuple[list[Point], bool]:
    """Simplify a sourced closed ring without opening or reversing it.

    Ordinary protected-endpoint RDP sees a closed line's identical endpoints
    as a zero-length baseline.  GEOS' topology-preserving line simplifier is
    ring-aware: the closure remains explicit and self-intersection cannot be
    introduced.  Any numerical or topology anomaly falls back to the exact
    projected source ring instead of guessing a repair.
    """

    original_points = list(points)
    if (
        len(original_points) < 4
        or original_points[0] != original_points[-1]
        or tolerance_mm <= 0.0
    ):
        return original_points, False
    original = LineString(original_points)
    try:
        simplified = original.simplify(tolerance_mm, preserve_topology=True)
    except (TypeError, ValueError):
        return original_points, True
    if not isinstance(simplified, LineString):
        return original_points, True
    simplified_points = [
        (float(x), float(y)) for x, y in simplified.coords
    ]
    original_orientation = _signed_ring_area(original_points)
    simplified_orientation = _signed_ring_area(simplified_points)
    invalid = (
        len(simplified_points) < 4
        or simplified_points[0] != simplified_points[-1]
        or len(set(simplified_points[:-1])) < 3
        or not simplified.is_ring
        or simplified.length <= 1e-9
        or original.hausdorff_distance(simplified) > tolerance_mm + 1e-9
        or (
            abs(original_orientation) > 1e-12
            and original_orientation * simplified_orientation <= 0.0
        )
    )
    return (original_points, True) if invalid else (simplified_points, False)


def _context_domain(record: PlannedContextStroke) -> tuple[object, ...]:
    tags = dict(record.source_tags)
    return (
        record.kind,
        tags.get("highway", ""),
        tags.get("railway", ""),
        tags.get("service", ""),
        tags.get("usage", ""),
        tags.get("bridge", ""),
        tags.get("tunnel", ""),
        tags.get("layer", ""),
        tags.get("level", ""),
    )


def _context_endpoint_key(
    record: PlannedContextStroke, index: int
) -> tuple[object, ...]:
    domain = _context_domain(record)
    if len(record.node_refs) == len(record.points) and record.node_refs[index]:
        return ("node", record.node_refs[index], *domain[5:])
    point = record.points[index]
    # This is equality after the shared WGS84->paper projection, not a
    # proximity snap. Rounding only removes floating serialization noise.
    return ("coordinate", round(point[0], 9), round(point[1], 9), *domain[5:])


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def assemble_context_trails(
    records: Sequence[PlannedContextStroke],
    *,
    simplify_mm: float = 0.04,
) -> tuple[list[PlannedContextStroke], dict[str, object]]:
    """Join source-connected context before applying the physical path floor.

    Only degree-two endpoints inside an identical semantic/grade domain may be
    crossed. No coordinate proximity, interior crossing, or invented connector
    is permitted. Area/boundary rings retain their original source paths.
    """

    if simplify_mm < 0.0 or simplify_mm > 0.1:
        raise MapPlotterError("Transit context simplification must be in [0, 0.1] mm.")
    passthrough: list[PlannedContextStroke] = []
    grouped: dict[tuple[object, ...], list[PlannedContextStroke]] = {}
    for record in records:
        if (
            record.kind not in _ASSEMBLED_CONTEXT_KINDS
            or record.geometry_type != "line"
            or len(record.points) < 2
        ):
            passthrough.append(record)
            continue
        grouped.setdefault(_context_domain(record), []).append(record)

    output = list(passthrough)
    represented_input_ids: set[str] = {
        feature_id for record in passthrough for feature_id in record.feature_ids
    }
    assembled_trail_count = 0
    joined_boundary_count = 0
    closed_ring_count = 0
    simplified_closed_ring_count = 0
    closed_ring_fallback_count = 0
    pre_simplified_trail_count = 0
    maximum_hausdorff_mm = 0.0

    for domain, domain_records in sorted(
        grouped.items(), key=lambda item: repr(item[0])
    ):
        ordered = sorted(
            domain_records,
            key=lambda item: (item.feature_id, item.source_ref, item.source_object),
        )
        endpoints = {
            index: (
                _context_endpoint_key(record, 0),
                _context_endpoint_key(record, -1),
            )
            for index, record in enumerate(ordered)
        }
        adjacency: dict[tuple[object, ...], list[tuple[int, int]]] = {}
        for index, (start, end) in endpoints.items():
            adjacency.setdefault(start, []).append((index, 0))
            adjacency.setdefault(end, []).append((index, 1))
        for values in adjacency.values():
            values.sort()

        unvisited = set(range(len(ordered)))
        # Degree != 2 endpoints are the only valid starts while any remain.
        # Computing the full candidate list inside every trail iteration made
        # disconnected street networks quadratic: a city with thousands of
        # cul-de-sacs repeatedly scanned almost the entire unvisited set.  The
        # set only shrinks, so the same deterministic ``min(..., key=repr)``
        # choice can be made from one pre-sorted list with a monotonic cursor.
        anchor_candidates = sorted(
            (
                (endpoint, index, side)
                for index, endpoint_pair in endpoints.items()
                for side, endpoint in enumerate(endpoint_pair)
                if len(adjacency[endpoint]) != 2
            ),
            key=repr,
        )
        anchor_cursor = 0
        cycle_cursor = 0
        trails: list[list[tuple[int, bool]]] = []
        while unvisited:
            while (
                anchor_cursor < len(anchor_candidates)
                and anchor_candidates[anchor_cursor][1] not in unvisited
            ):
                anchor_cursor += 1
            if anchor_cursor < len(anchor_candidates):
                _, first_index, first_side = anchor_candidates[anchor_cursor]
            else:
                while cycle_cursor not in unvisited:
                    cycle_cursor += 1
                first_index = cycle_cursor
                first_side = (
                    0
                    if repr(endpoints[first_index][0])
                    <= repr(endpoints[first_index][1])
                    else 1
                )
            current_index = first_index
            entry_side = first_side
            trail: list[tuple[int, bool]] = []
            while current_index in unvisited:
                forward = entry_side == 0
                trail.append((current_index, forward))
                unvisited.remove(current_index)
                exit_side = 1 if forward else 0
                exit_key = endpoints[current_index][exit_side]
                if len(adjacency[exit_key]) != 2:
                    break
                candidates = [
                    (next_index, next_side)
                    for next_index, next_side in adjacency[exit_key]
                    if next_index in unvisited
                ]
                if not candidates:
                    break
                current_index, entry_side = min(candidates)
                joined_boundary_count += 1
            trails.append(trail)

        for trail in trails:
            points: list[Point] = []
            feature_ids: list[str] = []
            source_refs: list[str] = []
            source_objects: list[str] = []
            for record_index, forward in trail:
                record = ordered[record_index]
                segment = list(record.points if forward else reversed(record.points))
                if points and points[-1] == segment[0]:
                    points.extend(segment[1:])
                else:
                    points.extend(segment)
                feature_ids.extend(record.feature_ids)
                source_refs.extend(record.source_refs)
                source_objects.extend(record.source_objects)
            if len(points) < 2:
                continue
            original = LineString(points)
            declared_pre_tolerances: list[float] = []
            for record_index, _forward in trail:
                raw_tolerance = dict(ordered[record_index].source_tags).get(
                    "paper_space_simplification_tolerance_mm"
                )
                try:
                    declared = float(raw_tolerance) if raw_tolerance is not None else 0.0
                except (TypeError, ValueError):
                    declared = 0.0
                declared_pre_tolerances.append(declared)
            trail_simplify_mm = (
                0.0
                if declared_pre_tolerances
                and all(value + 1e-12 >= simplify_mm for value in declared_pre_tolerances)
                else simplify_mm
            )
            pre_simplified_trail_count += int(trail_simplify_mm == 0.0)
            is_closed_ring = (
                len(points) >= 4
                and points[0] == points[-1]
                and len(set(points[:-1])) >= 3
            )
            if is_closed_ring:
                closed_ring_count += 1
                simplified_points, used_fallback = _simplify_closed_context_ring(
                    points, tolerance_mm=trail_simplify_mm
                )
                closed_ring_fallback_count += int(used_fallback)
                simplified_closed_ring_count += int(
                    not used_fallback and len(simplified_points) < len(points)
                )
            else:
                simplified_points = simplify_protected(
                    points,
                    tolerance_mm=trail_simplify_mm,
                    protected_indices=(0, len(points) - 1),
                )
            simplified = LineString(simplified_points)
            # RDP sees the shared start/end of a small closed trail as a
            # zero-length baseline and can legitimately select only those two
            # identical vertices.  That would erase the trail before the
            # renderer can apply (and ledger) its physical paper-space floor.
            # Retain the sourced ring in that case; selection belongs to the
            # later nib-aware stage, not topology assembly.
            if (
                len(set(simplified_points)) < 2
                or simplified.length <= 1e-9
            ) and original.length > 1e-9:
                simplified_points = list(points)
                simplified = original
            maximum_hausdorff_mm = max(
                maximum_hausdorff_mm, original.hausdorff_distance(simplified)
            )
            stable_ids = _ordered_unique(feature_ids)
            digest = hashlib.sha256("\0".join(stable_ids).encode("utf-8")).hexdigest()
            exemplar = ordered[trail[0][0]]
            output.append(
                PlannedContextStroke(
                    feature_id=f"context-trail-{exemplar.kind}-{digest[:16]}",
                    kind=exemplar.kind,
                    points=tuple(simplified_points),
                    source_ref=source_refs[0],
                    source_object=source_objects[0],
                    represented_feature_ids=stable_ids,
                    represented_source_refs=_ordered_unique(source_refs),
                    represented_source_objects=_ordered_unique(source_objects),
                    source_layer=exemplar.source_layer,
                    source_tags=exemplar.source_tags,
                    geometry_type="line",
                )
            )
            represented_input_ids.update(stable_ids)
            assembled_trail_count += 1

    input_ids = {feature_id for record in records for feature_id in record.feature_ids}
    output.sort(key=lambda item: (item.kind, item.feature_id))
    diagnostics: dict[str, object] = {
        "policy_version": "transit-context-topology-first-v1",
        "input_path_count": len(records),
        "assembled_trail_count": assembled_trail_count,
        "passthrough_path_count": len(passthrough),
        "output_path_count": len(output),
        "joined_degree_two_boundary_count": joined_boundary_count,
        "closed_ring_count": closed_ring_count,
        "simplified_closed_ring_count": simplified_closed_ring_count,
        "closed_ring_fallback_count": closed_ring_fallback_count,
        "closed_ring_simplification_preserves_closure_and_orientation": True,
        "pre_simplified_trail_count": pre_simplified_trail_count,
        "pre_simplified_trails_receive_no_second_simplification": True,
        "input_source_feature_count": len(input_ids),
        "represented_source_feature_count": len(represented_input_ids),
        "source_feature_parity": input_ids == represented_input_ids,
        "invented_connector_count": 0,
        "moved_anchor_count": 0,
        "simplification_tolerance_mm": simplify_mm,
        "maximum_hausdorff_mm": round(maximum_hausdorff_mm, 6),
    }
    if input_ids != represented_input_ids:
        raise MapPlotterError("Transit context trail assembly lost source lineage.")
    return output, diagnostics


def build_transit_plan(
    network: TransitNetwork,
    field: Rect,
    *,
    station_label_policy: str = "key",
    simplification_tolerance_mm: float | None = None,
    lane_gap_mm: float = DEFAULT_LANE_GAP_MM,
    taper_mm: float = DEFAULT_TAPER_MM,
    route_target_width_mm: float | None = None,
    route_target_maximum_width_mm: float = DEFAULT_ROUTE_TARGET_MM,
    projector_margin_fraction: float = DEFAULT_PROJECTOR_MARGIN_FRACTION,
) -> TransitPlan:
    if lane_gap_mm < 0.08:
        raise MapPlotterError("Transit lane gap must be at least 0.08 mm.")
    if route_target_width_mm is not None and route_target_width_mm <= 0.0:
        raise MapPlotterError("Transit route target width must be positive.")
    if route_target_maximum_width_mm <= 0.0:
        raise MapPlotterError("Transit route target maximum width must be positive.")
    if (
        route_target_width_mm is not None
        and route_target_width_mm > route_target_maximum_width_mm + 1e-9
    ):
        raise MapPlotterError(
            "Transit route target width cannot exceed its format maximum."
        )
    projector = projector_for(
        network,
        field,
        margin_fraction=projector_margin_fraction,
    )
    tier = scale_tier(projector)
    simplification_tolerance_mm = (
        DISTANT_SIMPLIFICATION_MM
        if simplification_tolerance_mm is None
        and tier in {"regional-network", "national-network"}
        else DEFAULT_SIMPLIFICATION_MM
        if simplification_tolerance_mm is None
        else simplification_tolerance_mm
    )
    if simplification_tolerance_mm < 0.0 or simplification_tolerance_mm > 0.25:
        raise MapPlotterError(
            "Transit simplification tolerance must be in [0, 0.25] mm."
        )
    automatic_scale_target = route_target_width_mm is None
    resolved_route_target_width_mm = (
        min(
            route_target_width_for_scale(tier),
            route_target_maximum_width_mm,
        )
        if route_target_width_mm is None
        else route_target_width_mm
    )
    line_map = network.line_by_id
    width_plan_records: list[RouteWidthPlan] = []
    for line in sorted(network.lines, key=lambda item: (item.order, item.id)):
        promoted = _registry_bound_native_owned_nib_width_plan(
            network,
            line,
            scale_tier_name=tier,
            scale_target_width_mm=resolved_route_target_width_mm,
            route_target_maximum_width_mm=route_target_maximum_width_mm,
            automatic_scale_target=automatic_scale_target,
        )
        width_plan_records.append(
            promoted
            if promoted is not None
            else route_width_plan(
                line,
                normal_target_mm=resolved_route_target_width_mm,
            )
        )
    width_plans = tuple(width_plan_records)
    width_plan_map = {item.line_id: item for item in width_plans}
    plotted_widths = {
        line_id: item.plotted_width_mm for line_id, item in width_plan_map.items()
    }
    maximum_centre_separation = 0.0
    protected = _station_protected_vertices(network, projector)
    route_parts: list[PlannedRouteStroke] = []
    emitted_memberships = 0
    base_by_edge: dict[str, list[Point]] = {}
    simplified_by_edge: dict[str, list[Point]] = {}
    for edge in network.edges:
        base = [projector.point(lon, lat) for lon, lat in edge.geometry]
        base_by_edge[edge.id] = base
        simplified_by_edge[edge.id] = simplify_protected(
            base,
            tolerance_mm=simplification_tolerance_mm,
            protected_indices=protected.get(edge.id, ()),
        )
    for chain, closed in _compatible_edge_chains(network.edges, line_map):
        chain_points: list[Point] = []
        chain_edge_ids: list[str] = []
        chain_sources: list[str] = []
        source_vertex_count = 0
        for edge, forward in chain:
            points = list(simplified_by_edge[edge.id])
            if not forward:
                points.reverse()
            if chain_points:
                chain_points.extend(points[1:])
                source_vertex_count += len(base_by_edge[edge.id]) - 1
            else:
                chain_points.extend(points)
                source_vertex_count += len(base_by_edge[edge.id])
            chain_edge_ids.append(edge.id)
            chain_sources.append(edge.source_ref)
        if closed:
            chain_points[-1] = chain_points[0]
        first_edge, first_forward = chain[0]
        last_edge, last_forward = chain[-1]
        start_node_id = first_edge.from_node if first_forward else first_edge.to_node
        end_node_id = last_edge.to_node if last_forward else last_edge.from_node
        offsets = _line_offsets(
            first_edge,
            line_map,
            plotted_widths,
            lane_gap_mm,
        )
        ordered_offsets = [
            offsets[line_id] for line_id in _ordered_line_ids(first_edge, line_map)
        ]
        maximum_centre_separation = max(
            maximum_centre_separation,
            max(
                (
                    abs(second - first)
                    for first, second in zip(ordered_offsets, ordered_offsets[1:])
                ),
                default=0.0,
            ),
        )
        for line_id, offset in offsets.items():
            route_parts.append(
                PlannedRouteStroke(
                    line_id=line_id,
                    edge_ids=tuple(chain_edge_ids),
                    start_node_id=start_node_id,
                    end_node_id=end_node_id,
                    points=tuple(
                        offset_with_taper(
                            chain_points,
                            offset_mm=offset,
                            taper_mm=taper_mm,
                            taper_start=not closed,
                            taper_end=not closed,
                        )
                    ),
                    source_refs=tuple(dict.fromkeys(chain_sources)),
                    maximum_lane_offset_mm=abs(offset),
                    simplification_tolerance_mm=simplification_tolerance_mm,
                    source_vertex_count=source_vertex_count,
                    represented_edge_ids=tuple(chain_edge_ids),
                )
            )
            emitted_memberships += len(chain)
    joined_route_strokes = _join_strokes(
        sorted(
            route_parts,
            key=lambda item: (
                line_map[item.line_id].order,
                item.line_id,
                item.edge_ids,
            ),
        )
    )
    route_strokes, same_line_corridor_audit = generalize_same_line_corridors(
        joined_route_strokes, line_map
    )
    station_lines = _station_lines(network)
    edge_page = {
        edge.id: [projector.point(lon, lat) for lon, lat in edge.geometry]
        for edge in network.edges
    }
    marks: list[StationMark] = []
    association_issues: list[dict[str, object]] = []
    for node in network.nodes:
        if not node.is_station:
            continue
        source_point = projector.point(node.lon, node.lat)
        relevant_lines = set(station_lines.get(node.id, ()))
        candidates: list[tuple[float, Point, str]] = []
        for edge in network.edges:
            if relevant_lines and not relevant_lines.intersection(edge.line_ids):
                continue
            points = edge_page[edge.id]
            for start, end in zip(points, points[1:]):
                projected = _point_segment_projection(source_point, start, end)
                candidates.append(
                    (
                        hypot(
                            source_point[0] - projected[0],
                            source_point[1] - projected[1],
                        ),
                        projected,
                        edge.id,
                    )
                )
        nearest = min(candidates, default=None, key=lambda item: (item[0], item[2]))
        if nearest is not None and nearest[0] <= STATION_ASSOCIATION_LIMIT_MM:
            mark_point = nearest[1]
            displacement = nearest[0]
            status = "snapped-to-source-route"
        else:
            mark_point = source_point
            displacement = nearest[0] if nearest is not None else float("inf")
            status = "unmatched-source-position"
            association_issues.append(
                {
                    "station_id": node.id,
                    "name": node.name or node.id,
                    "nearest_route_distance_mm": (
                        round(displacement, 6) if displacement != float("inf") else None
                    ),
                    "maximum_mm": STATION_ASSOCIATION_LIMIT_MM,
                }
            )
        marks.append(
            StationMark(
                node_id=node.id,
                name=node.name or node.id,
                tier=node.station_tier
                or (
                    "terminal"
                    if node.kind == "terminal"
                    else "interchange"
                    if node.kind == "interchange"
                    else "local"
                ),
                point=mark_point,
                source_point=source_point,
                displacement_mm=displacement,
                association_status=status,
                line_ids=station_lines.get(node.id, ()),
            )
        )
    applied_station_label_policy = station_label_policy
    station_label_fallback_reason: str | None = None
    has_key_station = any(
        mark.tier in {"terminal", "interchange", "major"} for mark in marks
    )
    closed_single_line_style = (
        tier == "compact-network"
        and len(network.lines) == 1
        and bool(network.service_patterns)
        and all(
            len(pattern.station_ids) >= 3
            and pattern.station_ids[0] == pattern.station_ids[-1]
            for pattern in network.service_patterns
        )
    )
    if (
        station_label_policy == "key"
        and not has_key_station
        and closed_single_line_style
    ):
        applied_station_label_policy = "all"
        station_label_fallback_reason = (
            "compact closed single-line network has no key-tier stations; "
            "all local stations become label candidates and normal collision "
            "omission remains binding"
        )
    labels, omitted = plan_station_labels(
        marks,
        field,
        policy=applied_station_label_policy,
        route_strokes=route_strokes,
        line_nib_mm=plotted_widths,
    )
    station_label_policy_audit: dict[str, object] = {
        "requested": station_label_policy,
        "applied": applied_station_label_policy,
        "fallback_applied": station_label_fallback_reason is not None,
        "fallback_reason": station_label_fallback_reason,
        "closed_single_line_style": closed_single_line_style,
        "key_tier_station_count": sum(
            mark.tier in {"terminal", "interchange", "major"} for mark in marks
        ),
        "eligible_station_count": (
            len(marks)
            if applied_station_label_policy == "all"
            else sum(
                mark.tier in {"terminal", "interchange", "major"} for mark in marks
            )
            if applied_station_label_policy == "key"
            else 0
        ),
        "emitted_station_label_count": len(labels),
        "collision_omission_count": len(omitted),
    }
    context = tuple(
        PlannedContextStroke(
            feature_id=feature.id,
            kind=feature.kind,
            points=tuple(projector.point(lon, lat) for lon, lat in feature.geometry),
            source_ref=feature.source_ref,
            source_object=feature.source_object,
            represented_feature_ids=(feature.id,),
            represented_source_refs=(feature.source_ref,),
            represented_source_objects=(feature.source_object,),
            source_layer=feature.source_layer,
            source_tags=feature.source_tags,
            node_refs=feature.node_refs,
            geometry_type=feature.geometry_type,
            ring_role=feature.ring_role,
        )
        for feature in network.context
    )
    short: list[dict[str, object]] = []
    for stroke in route_strokes:
        length = sum(
            hypot(second[0] - first[0], second[1] - first[1])
            for first, second in zip(stroke.points, stroke.points[1:])
        )
        floor = 3.0 * width_plan_map[stroke.line_id].fit.pen.mark_width_mm
        if length + 1e-9 < floor:
            short.append(
                {
                    "line_id": stroke.line_id,
                    "edge_ids": list(stroke.edge_ids),
                    "length_mm": round(length, 6),
                    "minimum_mm": round(floor, 6),
                }
            )
    return TransitPlan(
        projector=projector,
        scale_tier=tier,
        route_strokes=tuple(route_strokes),
        context_strokes=context,
        station_marks=tuple(marks),
        station_labels=tuple(labels),
        omitted_station_labels=tuple(omitted),
        station_association_issues=tuple(association_issues),
        lane_pitch_mm=maximum_centre_separation,
        simplification_tolerance_mm=simplification_tolerance_mm,
        source_edge_count=len(network.edges),
        emitted_edge_memberships=emitted_memberships,
        short_route_strokes=tuple(short),
        same_line_corridor_audit=same_line_corridor_audit,
        station_label_policy_audit=station_label_policy_audit,
        route_width_plans=width_plans,
        route_target_width_mm=resolved_route_target_width_mm,
        route_target_maximum_width_mm=route_target_maximum_width_mm,
        lane_clearance_mm=lane_gap_mm,
    )
