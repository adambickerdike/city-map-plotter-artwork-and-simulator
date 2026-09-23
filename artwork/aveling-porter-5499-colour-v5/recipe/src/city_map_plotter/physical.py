from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
from math import hypot, isfinite
import re
from typing import Any, Iterable

from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    Polygon,
    box,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .models import LayerStyle, MapPlotterError, PlotStroke
from .pens import (
    PenInventory,
    PenWidthFit,
    fit_locked_pen_width,
    fit_pen_width,
    style_pen_width,
)
from .topology import normalize_osm_level


def _physical_measurement(value: float) -> str:
    """Serialize calibrated widths without conflating them with path precision."""

    formatted = f"{value:.6f}".rstrip("0").rstrip(".")
    return formatted if formatted not in {"", "-0"} else "0"


ROAD_LAYERS = {
    "roads_major",
    "roads_secondary",
    "roads_local",
    "roads_other",
    "paths",
}
NETWORK_TRAIL_LAYERS = ROAD_LAYERS | {"railways"}
AREA_LAYERS = {"water_areas", "green_space", "buildings"}
ROAD_STYLE_CHOICES = frozenset({"multi", "single-nib", "centreline"})
_PHYSICAL_INPUT_INDEXES_TAG = "plot:compiler-input-indexes"


@dataclass(frozen=True)
class PhysicalCompileResult:
    strokes: list[PlotStroke]
    diagnostics: dict[str, Any]
    warnings: list[str]
    omissions: tuple[PhysicalMinimumOmission, ...] = ()


@dataclass(frozen=True)
class PhysicalInputStrokeEvidence:
    """Immutable identity for one cartographic stroke entering the compiler."""

    index: int
    layer: str
    part: str
    source_refs: tuple[str, ...]
    serialized_length_mm: float
    serialized_geometry_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "layer": self.layer,
            "part": self.part,
            "source_refs": list(self.source_refs),
            "serialized_length_mm": self.serialized_length_mm,
            "serialized_geometry_sha256": self.serialized_geometry_sha256,
        }


@dataclass(frozen=True)
class PhysicalMinimumOmission:
    """Measured proof that one physical path failed a nib-relative floor."""

    omission_id: str
    layer: str
    stroke_part: str
    source_refs: tuple[str, ...]
    input_strokes: tuple[PhysicalInputStrokeEvidence, ...]
    branch: str
    reason: str
    measurement: str
    measured_serialized_length_mm: float
    measured_area_mm2: float | None
    effective_nib_mm: float
    required_three_nib_floor_mm: float
    required_effective_length_floor_mm: float
    required_minimum_area_mm2: float | None
    serialized_geometry_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "omission_id": self.omission_id,
            "layer": self.layer,
            "stroke_part": self.stroke_part,
            "source_refs": list(self.source_refs),
            "input_strokes": [item.as_dict() for item in self.input_strokes],
            "branch": self.branch,
            "reason": self.reason,
            "measurement": self.measurement,
            "measured_serialized_length_mm": (self.measured_serialized_length_mm),
            "measured_area_mm2": self.measured_area_mm2,
            "effective_nib_mm": self.effective_nib_mm,
            "required_three_nib_floor_mm": self.required_three_nib_floor_mm,
            "required_effective_length_floor_mm": (
                self.required_effective_length_floor_mm
            ),
            "required_minimum_area_mm2": self.required_minimum_area_mm2,
            "serialized_coordinate_precision_mm": 0.001,
            "serialized_geometry_sha256": self.serialized_geometry_sha256,
        }


GradeSignature = tuple[float, str, bool, bool]


@dataclass(frozen=True)
class _RoadContext:
    rank: int
    plotted_width_mm: float
    line: LineString
    grade: GradeSignature


@dataclass(frozen=True)
class _WeightedPart:
    centre_index: int
    weight_index: int
    part_index: int
    distance_mm: float
    line: LineString


@dataclass(frozen=True)
class _OmittedWeightedPart:
    centre_index: int
    weight_index: int
    part_index: int
    line: LineString


@dataclass(frozen=True)
class _TrailEdge:
    index: int
    stroke: PlotStroke
    length_mm: float
    start_node: tuple[object, ...]
    end_node: tuple[object, ...]


@dataclass(frozen=True)
class _NetworkTrailAssembly:
    strokes: list[PlotStroke]
    diagnostics: dict[str, Any]


def _physical_plan(
    *,
    style: LayerStyle,
    stroke_count: int,
    inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
    lock_inventory_nib: bool = False,
) -> PenWidthFit:
    assert style.ink is not None and style.nib_mm is not None
    legacy = style_pen_width(
        ink=style.ink,
        nib_mm=style.nib_mm,
        stroke_count=stroke_count,
    )
    if inventory is None:
        return legacy
    if lock_inventory_nib:
        return fit_locked_pen_width(
            inventory,
            ink=style.ink,
            nominal_nib_mm=style.nib_mm,
            stroke_count=stroke_count,
            allowed_nibs_mm=allowed_nibs_mm,
        )
    return fit_pen_width(
        inventory,
        ink=style.ink,
        requested_width_mm=legacy.plotted_width_mm,
        allowed_nibs_mm=allowed_nibs_mm,
    )


def _line_parts(geometry: BaseGeometry) -> Iterable[LineString]:
    if geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        if len(geometry.coords) >= 2 and geometry.length > 1e-6:
            yield geometry
        return
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        for child in geometry.geoms:
            yield from _line_parts(child)


def _number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", "."))
    if match is None:
        return None
    try:
        result = float(match.group())
    except ValueError:
        return None
    return result if isfinite(result) else None


def _road_stroke_count(stroke: PlotStroke, style: LayerStyle, road_style: str) -> int:
    count = style.strokes
    rank = int(_number(stroke.tags.get("road-rank")) or 0)
    if road_style == "single-nib":
        count = max(count, 3 if rank >= 6 else 2 if rank >= 4 else 1)
    elif rank >= 6:
        count = max(count, 2)
    return max(1, min(count, 5))


def _is_closed(stroke: PlotStroke) -> bool:
    return len(stroke.points) >= 4 and stroke.points[0] == stroke.points[-1]


def _passes_minimum_gate(stroke: PlotStroke, line: LineString, nib_mm: float) -> bool:
    if line.length < max(0.5, 3 * nib_mm):
        return False
    if stroke.layer in AREA_LAYERS and _is_closed(stroke):
        polygon = Polygon(line)
        if polygon.is_valid and polygon.area < (2 * nib_mm) ** 2:
            return False
    return True


def _offset_positions(count: int, pitch_mm: float) -> list[float]:
    centre = (count - 1) / 2
    return [(index - centre) * pitch_mm for index in range(count)]


def _tag_is_enabled(value: str | None) -> bool:
    return value is not None and value.strip().casefold() not in {
        "",
        "0",
        "false",
        "no",
    }


def _road_grade_signature(stroke: PlotStroke) -> GradeSignature:
    """Return a conservative grade and indoor-level domain for roads.

    An explicit OSM ``layer`` (or compiled ``z-layer``) takes precedence.  When
    it is absent, bridge and tunnel flags imply the conventional +1/-1 domain.
    Keeping the flags and normalized OSM ``level`` in the signature avoids
    treating a nominal layer-zero bridge, tunnel, or another indoor floor as a
    surface intersection when the source tagging is incomplete or unusual.
    """

    bridge = _tag_is_enabled(stroke.tags.get("bridge"))
    tunnel = _tag_is_enabled(stroke.tags.get("tunnel"))
    layer = _number(stroke.tags.get("z-layer"))
    if layer is None:
        layer = _number(stroke.tags.get("layer"))
    if layer is None:
        layer = 1.0 if bridge and not tunnel else -1.0 if tunnel and not bridge else 0.0
    return (
        round(layer, 6),
        normalize_osm_level(stroke.tags.get("level")),
        bridge,
        tunnel,
    )


_TRAIL_DOMAIN_TAGS = (
    "plot:ink",
    "plot:pen-id",
    "plot:nib-mm",
    "plot:nominal-nib-mm",
    "plot:calibration-state",
    "plot:calibration-substrate",
    "plot:pen-profile",
    "plot:plotted-width-mm",
    "plot:requested-width-mm",
    "plot:width-fit-error-mm",
    "plot:width-fit-mode",
    "plot:offset-pitch-mm",
    "plot:stroke-index",
    "plot:stroke-count",
    "plot:pass-index",
    "plot:pass-count",
    "plot:offset-fallback",
)


def _polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(
        hypot(end[0] - start[0], end[1] - start[1])
        for start, end in zip(points, points[1:])
    )


def _serialized_polyline_length(points: list[tuple[float, float]]) -> float:
    """Measure the coordinates that the SVG's 0.001 mm formatter will emit."""

    serialized = [(float(f"{x:.3f}"), float(f"{y:.3f}")) for x, y in points]
    return _polyline_length(serialized)


def _serialized_geometry_sha256(points: list[tuple[float, float]]) -> str:
    """Fingerprint exactly the coordinate tokens that can reach the SVG."""

    payload = ";".join(f"{x:.3f},{y:.3f}" for x, y in points).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _source_refs(stroke: PlotStroke) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                reference
                for reference in stroke.tags.get("source-refs", "").split(";")
                if reference
            }
        )
    )


def _input_stroke_evidence(
    strokes: list[PlotStroke], indexes: Iterable[int]
) -> tuple[PhysicalInputStrokeEvidence, ...]:
    evidence: list[PhysicalInputStrokeEvidence] = []
    for index in sorted(set(indexes)):
        if index < 0 or index >= len(strokes):
            raise MapPlotterError(
                "Physical omission provenance referenced an unknown input stroke."
            )
        stroke = strokes[index]
        evidence.append(
            PhysicalInputStrokeEvidence(
                index=index,
                layer=stroke.layer,
                part=str(stroke.part),
                source_refs=_source_refs(stroke),
                serialized_length_mm=round(
                    _serialized_polyline_length(stroke.points), 9
                ),
                serialized_geometry_sha256=_serialized_geometry_sha256(stroke.points),
            )
        )
    return tuple(evidence)


@dataclass(frozen=True)
class _MinimumGateFailure:
    reason: str
    measurement: str
    serialized_length_mm: float
    area_mm2: float | None
    required_minimum_area_mm2: float | None


def _minimum_gate_failure(
    stroke: PlotStroke, line: LineString, nib_mm: float
) -> _MinimumGateFailure | None:
    points = [(float(x), float(y)) for x, y in line.coords]
    serialized_length_mm = _serialized_polyline_length(points)
    minimum_length_mm = max(0.5, 3 * nib_mm)
    if serialized_length_mm + 1e-9 < minimum_length_mm:
        return _MinimumGateFailure(
            reason="below_minimum_serialized_length",
            measurement="serialized_polyline_length_mm",
            serialized_length_mm=serialized_length_mm,
            area_mm2=None,
            required_minimum_area_mm2=None,
        )
    if stroke.layer in AREA_LAYERS and _is_closed(stroke):
        serialized_points = [(float(f"{x:.3f}"), float(f"{y:.3f}")) for x, y in points]
        polygon = Polygon(serialized_points)
        minimum_area_mm2 = (2 * nib_mm) ** 2
        if polygon.is_valid and polygon.area + 1e-12 < minimum_area_mm2:
            return _MinimumGateFailure(
                reason="below_minimum_area",
                measurement="serialized_polygon_area_mm2",
                serialized_length_mm=serialized_length_mm,
                area_mm2=polygon.area,
                required_minimum_area_mm2=minimum_area_mm2,
            )
    return None


def _minimum_stroke_mm(stroke: PlotStroke) -> float:
    nib_mm = _number(stroke.tags.get("plot:nib-mm"))
    if nib_mm is None:
        raise MapPlotterError(
            "A physical network stroke is missing its effective nib width."
        )
    return max(0.5, 3 * nib_mm)


def _is_network_trail_candidate(stroke: PlotStroke) -> bool:
    """Return whether a stroke may enter exact edge-disjoint trail assembly."""

    return (
        stroke.layer in NETWORK_TRAIL_LAYERS
        and not stroke.smooth
        and not (stroke.layer in AREA_LAYERS and _is_closed(stroke))
    )


def _trail_domain(stroke: PlotStroke) -> tuple[object, ...]:
    """Return the exact physical domain across which a pen may stay down."""

    return (
        stroke.layer,
        _road_grade_signature(stroke),
        stroke.smooth,
        tuple((key, stroke.tags.get(key)) for key in _TRAIL_DOMAIN_TAGS),
    )


def _trail_endpoint_node(
    stroke: PlotStroke, edge_index: int, side: int
) -> tuple[object, ...]:
    point = stroke.points[0] if side == 0 else stroke.points[-1]
    tag_key = "topology:start-node" if side == 0 else "topology:end-node"
    topology_node = stroke.tags.get(tag_key)
    if topology_node:
        # Including the coordinate makes an exact geometric join an invariant,
        # even if malformed input reuses one topology identity at two positions.
        return ("topology", topology_node, point[0], point[1])
    # PlotStroke callers outside the topology compiler may not carry protected
    # node IDs. Exact page coordinates are the conservative fallback used by
    # the source-topology builder for unreferenced nodes.
    return (
        "coordinate",
        point[0],
        point[1],
        _road_grade_signature(stroke),
    )


def _incidence_direction(edge: _TrailEdge, side: int) -> tuple[float, float]:
    points = edge.stroke.points if side == 0 else list(reversed(edge.stroke.points))
    origin = points[0]
    for point in points[1:]:
        dx = point[0] - origin[0]
        dy = point[1] - origin[1]
        magnitude = hypot(dx, dy)
        if magnitude > 1e-12:
            return (dx / magnitude, dy / magnitude)
    return (0.0, 0.0)


def _merge_trail_strokes(
    oriented_edges: list[tuple[_TrailEdge, int]],
    *,
    domain_index: int,
    trail_index: int,
) -> PlotStroke:
    """Concatenate one continuous edge-disjoint trail and union its lineage."""

    first_edge, first_side = oriented_edges[0]
    first_points = (
        list(first_edge.stroke.points)
        if first_side == 0
        else list(reversed(first_edge.stroke.points))
    )
    points = first_points
    trail_strokes = [first_edge.stroke]
    for edge, side in oriented_edges[1:]:
        oriented = (
            list(edge.stroke.points)
            if side == 0
            else list(reversed(edge.stroke.points))
        )
        if points[-1] != oriented[0]:
            raise MapPlotterError(
                "Network trail assembly encountered a non-contiguous topology edge."
            )
        points.extend(oriented[1:])
        trail_strokes.append(edge.stroke)

    if len(trail_strokes) == 1:
        return replace(
            first_edge.stroke,
            points=points,
            tags=dict(first_edge.stroke.tags),
        )

    common_tags = dict(trail_strokes[0].tags)
    for key in list(common_tags):
        if any(
            stroke.tags.get(key) != common_tags[key] for stroke in trail_strokes[1:]
        ):
            del common_tags[key]
    source_refs = sorted(
        {
            item
            for stroke in trail_strokes
            for item in stroke.tags.get("source-refs", "").split(";")
            if item
        }
    )
    if source_refs:
        common_tags["source-refs"] = ";".join(source_refs)
        common_tags["source-count"] = str(len(source_refs))
    input_indexes = sorted(
        {
            int(item)
            for stroke in trail_strokes
            for item in stroke.tags.get(_PHYSICAL_INPUT_INDEXES_TAG, "").split(";")
            if item
        }
    )
    if input_indexes:
        common_tags[_PHYSICAL_INPUT_INDEXES_TAG] = ";".join(
            str(item) for item in input_indexes
        )
    common_tags.pop("topology:start-node", None)
    common_tags.pop("topology:end-node", None)
    common_tags.update(
        {
            "plot:network-trail": "edge-disjoint",
            "plot:network-trail-edge-count": str(len(trail_strokes)),
            "plot:network-trail-length-mm": f"{_polyline_length(points):.6f}",
        }
    )
    if _polyline_length(points) + 1e-9 >= _minimum_stroke_mm(first_edge.stroke):
        common_tags.pop("plot:physical-conflict", None)
    else:
        common_tags["plot:physical-conflict"] = "below-reliable-nib-size"

    names = {stroke.name for stroke in trail_strokes}
    return PlotStroke(
        layer=first_edge.stroke.layer,
        points=points,
        osm_type="compiled",
        osm_id="multiple",
        part=f"network-trail:{domain_index}:{trail_index}",
        tags=common_tags,
        name=next(iter(names)) if len(names) == 1 else None,
        # A round SVG linejoin softens the physical corner without moving a
        # protected junction. Bezier smoothing here would alter road topology.
        smooth=False,
    )


def _assemble_domain_trails(
    strokes: list[PlotStroke], *, domain_index: int
) -> tuple[list[PlotStroke], dict[str, int]]:
    """Pair compatible edge incidences into deterministic continuous trails."""

    edges = [
        _TrailEdge(
            index=index,
            stroke=stroke,
            length_mm=_polyline_length(stroke.points),
            start_node=_trail_endpoint_node(stroke, index, 0),
            end_node=_trail_endpoint_node(stroke, index, 1),
        )
        for index, stroke in enumerate(strokes)
    ]
    incidence: dict[tuple[object, ...], list[tuple[int, int]]] = defaultdict(list)
    for edge in edges:
        incidence[edge.start_node].append((edge.index, 0))
        incidence[edge.end_node].append((edge.index, 1))

    pairs: dict[tuple[int, int], tuple[int, int]] = {}
    for node in sorted(incidence, key=repr):
        remaining = list(incidence[node])
        if len(remaining) % 2:
            # Leaving the longest incidence unmatched makes it least likely that
            # the resulting endpoint-to-endpoint trail falls below 3 x nib.
            unmatched = max(
                remaining,
                key=lambda item: (edges[item[0]].length_mm, -item[0], -item[1]),
            )
            remaining.remove(unmatched)
        while remaining:
            left = min(
                remaining,
                key=lambda item: (edges[item[0]].length_mm, item[0], item[1]),
            )
            remaining.remove(left)
            left_direction = _incidence_direction(edges[left[0]], left[1])
            alternatives = [item for item in remaining if item[0] != left[0]]
            candidates = alternatives or remaining

            def partner_key(item: tuple[int, int]) -> tuple[float, float, int, int]:
                right_direction = _incidence_direction(edges[item[0]], item[1])
                direction_dot = (
                    left_direction[0] * right_direction[0]
                    + left_direction[1] * right_direction[1]
                )
                # Opposing outward tangents (dot=-1) form the straightest
                # continuation through the protected junction. For equal turns,
                # pairing a short edge with a longer one best clears the floor.
                return (
                    direction_dot,
                    -edges[item[0]].length_mm,
                    item[0],
                    item[1],
                )

            right = min(candidates, key=partner_key)
            remaining.remove(right)
            pairs[left] = right
            pairs[right] = left

    visited: set[int] = set()
    oriented_trails: list[list[tuple[_TrailEdge, int]]] = []

    def walk(start_edge: int, start_side: int) -> list[tuple[_TrailEdge, int]]:
        trail: list[tuple[_TrailEdge, int]] = []
        edge_index = start_edge
        side = start_side
        while edge_index not in visited:
            edge = edges[edge_index]
            visited.add(edge_index)
            trail.append((edge, side))
            far_side = 1 - side
            continuation = pairs.get((edge_index, far_side))
            if continuation is None:
                break
            edge_index, side = continuation
        return trail

    starts = sorted(
        incidence_item
        for items in incidence.values()
        for incidence_item in items
        if incidence_item not in pairs
    )
    for edge_index, side in starts:
        if edge_index not in visited:
            oriented_trails.append(walk(edge_index, side))
    # Even-degree components are closed trails and therefore have no unmatched
    # incidence from which to start.
    for edge in edges:
        if edge.index not in visited:
            oriented_trails.append(walk(edge.index, 0))
    if len(visited) != len(edges):
        raise MapPlotterError(
            "Network trail assembly did not consume every input edge."
        )

    output = [
        _merge_trail_strokes(
            trail,
            domain_index=domain_index,
            trail_index=trail_index,
        )
        for trail_index, trail in enumerate(oriented_trails)
    ]

    # Count components whose entire available ink-domain geometry is below the
    # floor. No edge-disjoint joining algorithm can make these physically viable.
    unseen: set[int] = set(range(len(edges)))
    below_minimum_components = 0
    minimum_mm = _minimum_stroke_mm(strokes[0])
    while unseen:
        stack = [min(unseen)]
        unseen.remove(stack[0])
        component_length = 0.0
        while stack:
            edge_index = stack.pop()
            edge = edges[edge_index]
            component_length += edge.length_mm
            for node in (edge.start_node, edge.end_node):
                for neighbour, _ in incidence[node]:
                    if neighbour in unseen:
                        unseen.remove(neighbour)
                        stack.append(neighbour)
        below_minimum_components += int(component_length + 1e-9 < minimum_mm)

    return output, {
        "input_edges": len(edges),
        "output_trails": len(output),
        "joined_edges": len(edges) - len(output),
        "below_minimum_components": below_minimum_components,
    }


def _source_ref_set(strokes: Iterable[PlotStroke]) -> set[str]:
    return {
        item
        for stroke in strokes
        for item in stroke.tags.get("source-refs", "").split(";")
        if item
    }


def _assemble_road_network_trails(
    strokes: list[PlotStroke],
) -> _NetworkTrailAssembly:
    """Join exact road/rail edges while preserving pen, pass, grade, and lineage."""

    grouped: dict[tuple[object, ...], list[PlotStroke]] = {}
    group_order: list[tuple[object, ...]] = []
    for stroke in strokes:
        if not _is_network_trail_candidate(stroke):
            continue
        domain = _trail_domain(stroke)
        if domain not in grouped:
            grouped[domain] = []
            group_order.append(domain)
        grouped[domain].append(stroke)

    assembled_by_domain: dict[tuple[object, ...], list[PlotStroke]] = {}
    domain_summaries: dict[tuple[object, ...], dict[str, int]] = {}
    for domain_index, domain in enumerate(group_order):
        assembled, summary = _assemble_domain_trails(
            grouped[domain], domain_index=domain_index
        )
        assembled_by_domain[domain] = assembled
        domain_summaries[domain] = summary

    output: list[PlotStroke] = []
    emitted_domains: set[tuple[object, ...]] = set()
    for stroke in strokes:
        if not _is_network_trail_candidate(stroke):
            output.append(stroke)
            continue
        domain = _trail_domain(stroke)
        if domain in emitted_domains:
            continue
        emitted_domains.add(domain)
        output.extend(assembled_by_domain[domain])

    input_roads = [stroke for stroke in strokes if stroke.layer in ROAD_LAYERS]
    output_roads = [stroke for stroke in output if stroke.layer in ROAD_LAYERS]
    input_rails = [stroke for stroke in strokes if stroke.layer == "railways"]
    output_rails = [stroke for stroke in output if stroke.layer == "railways"]
    input_length_mm = sum(_polyline_length(stroke.points) for stroke in input_roads)
    output_length_mm = sum(_polyline_length(stroke.points) for stroke in output_roads)
    if abs(input_length_mm - output_length_mm) > max(1e-8, input_length_mm * 1e-10):
        raise MapPlotterError("Road trail assembly changed pen-down road geometry.")
    input_refs = _source_ref_set(input_roads)
    output_refs = _source_ref_set(output_roads)
    if input_refs != output_refs:
        raise MapPlotterError("Road trail assembly changed source lineage coverage.")

    input_rail_length_mm = sum(
        _polyline_length(stroke.points) for stroke in input_rails
    )
    output_rail_length_mm = sum(
        _polyline_length(stroke.points) for stroke in output_rails
    )
    if abs(input_rail_length_mm - output_rail_length_mm) > max(
        1e-8, input_rail_length_mm * 1e-10
    ):
        raise MapPlotterError("Rail trail assembly changed pen-down rail geometry.")
    input_rail_refs = _source_ref_set(input_rails)
    output_rail_refs = _source_ref_set(output_rails)
    if input_rail_refs != output_rail_refs:
        raise MapPlotterError("Rail trail assembly changed source lineage coverage.")

    initial_below_by_layer: dict[str, int] = defaultdict(int)
    residual_below_by_layer: dict[str, int] = defaultdict(int)
    shortest_residual_by_layer: dict[str, float] = {}
    minimum_by_layer: dict[str, float] = {}
    for stroke in input_roads:
        minimum_mm = _minimum_stroke_mm(stroke)
        minimum_by_layer[stroke.layer] = minimum_mm
        if _polyline_length(stroke.points) + 1e-9 < minimum_mm:
            initial_below_by_layer[stroke.layer] += 1
    for stroke in output_roads:
        minimum_mm = _minimum_stroke_mm(stroke)
        length_mm = _polyline_length(stroke.points)
        if length_mm + 1e-9 < minimum_mm:
            residual_below_by_layer[stroke.layer] += 1
            shortest_residual_by_layer[stroke.layer] = min(
                shortest_residual_by_layer.get(stroke.layer, length_mm),
                length_mm,
            )

    initial_rail_below = 0
    residual_rail_below = 0
    shortest_residual_rail_mm: float | None = None
    rail_minimums_mm: set[float] = set()
    for stroke in input_rails:
        minimum_mm = _minimum_stroke_mm(stroke)
        rail_minimums_mm.add(minimum_mm)
        if _polyline_length(stroke.points) + 1e-9 < minimum_mm:
            initial_rail_below += 1
    for stroke in output_rails:
        minimum_mm = _minimum_stroke_mm(stroke)
        rail_minimums_mm.add(minimum_mm)
        length_mm = _polyline_length(stroke.points)
        if length_mm + 1e-9 < minimum_mm:
            residual_rail_below += 1
            shortest_residual_rail_mm = (
                length_mm
                if shortest_residual_rail_mm is None
                else min(shortest_residual_rail_mm, length_mm)
            )

    below_components_by_layer: dict[str, int] = defaultdict(int)
    rail_below_minimum_components = 0
    for domain, summary in domain_summaries.items():
        layer = str(domain[0])
        if layer in ROAD_LAYERS:
            below_components_by_layer[layer] += summary["below_minimum_components"]
        elif layer == "railways":
            rail_below_minimum_components += summary["below_minimum_components"]

    return _NetworkTrailAssembly(
        strokes=output,
        diagnostics={
            "enabled": True,
            "method": "exact-endpoint edge-disjoint trail decomposition",
            "join_scope": "same layer, grade, physical pen, offset, and pass",
            "input_road_edges": len(input_roads),
            "output_road_trails": len(output_roads),
            "joined_edge_count": len(input_roads) - len(output_roads),
            "edge_count_preserved": len(input_roads),
            "source_ref_count_preserved": len(input_refs),
            "input_pen_down_mm": round(input_length_mm, 6),
            "output_pen_down_mm": round(output_length_mm, 6),
            "pen_down_geometry_preserved": True,
            "initial_below_minimum_strokes": sum(initial_below_by_layer.values()),
            "initial_below_minimum_by_layer": dict(
                sorted(initial_below_by_layer.items())
            ),
            "residual_below_minimum_trails": sum(residual_below_by_layer.values()),
            "residual_below_minimum_by_layer": dict(
                sorted(residual_below_by_layer.items())
            ),
            "below_minimum_connected_components_by_layer": dict(
                sorted(below_components_by_layer.items())
            ),
            "shortest_residual_mm_by_layer": {
                layer: round(length_mm, 6)
                for layer, length_mm in sorted(shortest_residual_by_layer.items())
            },
            "minimum_stroke_mm_by_layer": {
                layer: round(length_mm, 6)
                for layer, length_mm in sorted(minimum_by_layer.items())
            },
            "input_rail_edges": len(input_rails),
            "output_rail_trails": len(output_rails),
            "joined_rail_edge_count": len(input_rails) - len(output_rails),
            "rail_edge_count_preserved": len(input_rails),
            "rail_source_ref_count_preserved": len(input_rail_refs),
            "rail_input_pen_down_mm": round(input_rail_length_mm, 6),
            "rail_output_pen_down_mm": round(output_rail_length_mm, 6),
            "rail_pen_down_geometry_preserved": True,
            "initial_rail_below_minimum_strokes": initial_rail_below,
            "residual_rail_below_minimum_trails": residual_rail_below,
            "rail_below_minimum_connected_components": (rail_below_minimum_components),
            "shortest_residual_rail_mm": (
                None
                if shortest_residual_rail_mm is None
                else round(shortest_residual_rail_mm, 6)
            ),
            "rail_minimum_stroke_mm_options": sorted(
                round(length_mm, 6) for length_mm in rail_minimums_mm
            ),
            "remediation": (
                "Residual trails are connected ink-domain components below the "
                "physical floor, or branches that cannot be combined without "
                "repeating geometry. Use a larger sheet, a tighter map extent, "
                "or a finer supplied pen; no road or rail edge was dropped or "
                "overtraced."
            ),
        },
    )


def _compile_weighted_parts(
    centres: list[LineString],
    *,
    positions: list[float],
    clip: BaseGeometry,
    minimum_length_mm: float,
    preserve_part: bool,
) -> tuple[list[_WeightedPart], list[_OmittedWeightedPart], int, bool]:
    """Compile every requested offset and report companions that disappeared."""

    compiled: list[_WeightedPart] = []
    omitted_short: list[_OmittedWeightedPart] = []
    missing_companions = 0
    retained_below_minimum = False
    for centre_index, centre in enumerate(centres):
        for weight_index, distance in enumerate(positions, start=1):
            weighted: BaseGeometry
            try:
                if abs(distance) <= 1e-9:
                    weighted = centre
                else:
                    weighted = centre.offset_curve(
                        distance,
                        quad_segs=4,
                        join_style="mitre",
                        mitre_limit=2.5,
                    )
                    if not weighted.is_simple:
                        weighted = unary_union(weighted)
                weighted = weighted.intersection(clip)
            except GEOSException:
                weighted = GeometryCollection()

            accepted_for_position = 0
            for part_index, part in enumerate(_line_parts(weighted)):
                part_points = [(float(x), float(y)) for x, y in part.coords]
                below_minimum = (
                    _serialized_polyline_length(part_points) + 1e-9 < minimum_length_mm
                )
                if below_minimum and not preserve_part:
                    omitted_short.append(
                        _OmittedWeightedPart(
                            centre_index=centre_index,
                            weight_index=weight_index,
                            part_index=part_index,
                            line=part,
                        )
                    )
                    continue
                retained_below_minimum = retained_below_minimum or below_minimum
                compiled.append(
                    _WeightedPart(
                        centre_index=centre_index,
                        weight_index=weight_index,
                        part_index=part_index,
                        distance_mm=distance,
                        line=part,
                    )
                )
                accepted_for_position += 1
            if accepted_for_position == 0:
                missing_companions += 1
    return compiled, omitted_short, missing_companions, retained_below_minimum


def compile_physical_strokes(
    strokes: list[PlotStroke],
    styles: list[LayerStyle],
    *,
    clip_rect: tuple[float, float, float, float],
    road_style: str = "multi",
    preserve_network: bool = False,
    preserve_all: bool = False,
    drop_residual_conflicts: bool = False,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
    allow_repeat_passes: bool = False,
) -> PhysicalCompileResult:
    """Apply nib-relative gates, physical offsets, and explicit repeat passes.

    Style-driven output spaces offsets at 85% of the nib diameter.  With a pen
    inventory, one compatible physical nib is preferred; symmetric offsets
    are introduced only when the target is wider than every compatible nib.
    Repeat passes remain explicit ink-density requests and never stand in for
    width.
    """

    if road_style not in ROAD_STYLE_CHOICES:
        raise MapPlotterError(
            f"Road style must be one of: {', '.join(sorted(ROAD_STYLE_CHOICES))}."
        )
    repeated_style_layers = [style.id for style in styles if style.passes > 1]
    if pen_inventory is not None and repeated_style_layers and not allow_repeat_passes:
        raise MapPlotterError(
            "Pen-inventory output contains repeat-over-the-same-line passes in "
            f"layers {', '.join(repeated_style_layers)}. Repeats affect ink density, "
            "not width; approve them explicitly with --allow-repeat-passes."
        )
    excessive_repeat_layers = [style.id for style in styles if style.passes > 2]
    if pen_inventory is not None and excessive_repeat_layers:
        raise MapPlotterError(
            "Inventory-aware output permits at most two density passes; layers "
            f"{', '.join(excessive_repeat_layers)} request more. Use one pass where "
            "possible and calibrate any second pass on the intended stock."
        )
    style_by_layer = {style.id: style for style in styles}
    clip = box(*clip_rect)
    output: list[PlotStroke] = []
    omission_evidence: list[PhysicalMinimumOmission] = []
    omitted_short = 0
    source_strokes = 0
    offset_paths = 0
    repeated_paths = 0
    cross_class_trimmed_parts = 0
    cross_class_trimmed_mm = 0.0
    offset_fallback_source_strokes = 0
    missing_offset_companion_paths = 0
    retained_conflict_strokes: set[int] = set()
    network_conflict_strokes: set[int] = set()
    rail_conflict_strokes: set[int] = set()
    preserve_road_network = preserve_network or preserve_all
    fit_counts: dict[str, int] = {}
    selected_pen_counts: dict[str, dict[str, Any]] = {}
    maximum_absolute_fit_error_mm = 0.0
    width_fit_tolerance_violation_source_strokes = 0
    observed_offset_pitch_ratios: set[float] = set()

    def record_minimum_omission(
        omitted_stroke: PlotStroke,
        *,
        input_indexes: Iterable[int],
        branch: str,
        physical_nib_mm: float,
        failure: _MinimumGateFailure,
    ) -> None:
        inputs = _input_stroke_evidence(strokes, input_indexes)
        source_refs = tuple(
            sorted({reference for item in inputs for reference in item.source_refs})
        )
        omission_evidence.append(
            PhysicalMinimumOmission(
                omission_id=f"physical-minimum-{len(omission_evidence) + 1}",
                layer=omitted_stroke.layer,
                stroke_part=str(omitted_stroke.part),
                source_refs=source_refs,
                input_strokes=inputs,
                branch=branch,
                reason=failure.reason,
                measurement=failure.measurement,
                measured_serialized_length_mm=round(failure.serialized_length_mm, 9),
                measured_area_mm2=(
                    None if failure.area_mm2 is None else round(failure.area_mm2, 9)
                ),
                effective_nib_mm=round(physical_nib_mm, 9),
                required_three_nib_floor_mm=round(3 * physical_nib_mm, 9),
                required_effective_length_floor_mm=round(
                    max(0.5, 3 * physical_nib_mm), 9
                ),
                required_minimum_area_mm2=(
                    None
                    if failure.required_minimum_area_mm2 is None
                    else round(failure.required_minimum_area_mm2, 9)
                ),
                serialized_geometry_sha256=_serialized_geometry_sha256(
                    omitted_stroke.points
                ),
            )
        )

    road_context: dict[int, _RoadContext] = {}
    for stroke in strokes:
        style = style_by_layer.get(stroke.layer)
        if style is None or stroke.layer not in ROAD_LAYERS or len(stroke.points) < 2:
            continue
        requested_count = (
            1
            if road_style == "centreline"
            else _road_stroke_count(stroke, style, road_style)
        )
        plan = _physical_plan(
            style=style,
            stroke_count=requested_count,
            inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
            lock_inventory_nib=road_style == "single-nib",
        )
        rank = int(_number(stroke.tags.get("road-rank")) or 1)
        road_context[id(stroke)] = _RoadContext(
            rank=rank,
            plotted_width_mm=plan.plotted_width_mm,
            line=LineString(stroke.points),
            grade=_road_grade_signature(stroke),
        )

    knockout_by_class: dict[tuple[int, float, GradeSignature], BaseGeometry] = {}
    if not preserve_road_network and road_style in {"multi", "single-nib"}:
        for light in road_context.values():
            key = (light.rank, round(light.plotted_width_mm, 6), light.grade)
            if key in knockout_by_class:
                continue
            heavier_bands = [
                heavy.line.buffer(
                    heavy.plotted_width_mm / 2 + light.plotted_width_mm / 2,
                    cap_style="flat",
                    join_style="round",
                )
                for heavy in road_context.values()
                if heavy.rank > light.rank and heavy.grade == light.grade
            ]
            knockout_by_class[key] = (
                unary_union(heavier_bands) if heavier_bands else GeometryCollection()
            )

    for input_index, stroke in enumerate(strokes):
        style = style_by_layer.get(stroke.layer)
        if style is None or len(stroke.points) < 2:
            continue
        source_strokes += 1
        line = LineString(stroke.points)
        assert style.nib_mm is not None and style.ink is not None
        requested_count = (
            1 if preserve_all and stroke.layer not in ROAD_LAYERS else style.strokes
        )
        if stroke.layer in ROAD_LAYERS:
            requested_count = (
                1
                if road_style == "centreline"
                else _road_stroke_count(stroke, style, road_style)
            )
        plan = _physical_plan(
            style=style,
            stroke_count=requested_count,
            inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
            lock_inventory_nib=(
                stroke.layer in ROAD_LAYERS and road_style == "single-nib"
            ),
        )
        physical_nib_mm = plan.pen.mark_width_mm
        minimum_failure = _minimum_gate_failure(stroke, line, physical_nib_mm)
        retain_conflicting_centreline = minimum_failure is not None and (
            preserve_all or preserve_network and stroke.layer in NETWORK_TRAIL_LAYERS
        )
        if minimum_failure is not None and not retain_conflicting_centreline:
            omitted_short += 1
            record_minimum_omission(
                stroke,
                input_indexes=(input_index,),
                branch="initial_source_minimum_gate",
                physical_nib_mm=physical_nib_mm,
                failure=minimum_failure,
            )
            continue
        if retain_conflicting_centreline:
            retained_conflict_strokes.add(id(stroke))
            if stroke.layer in ROAD_LAYERS:
                network_conflict_strokes.add(id(stroke))
            elif stroke.layer == "railways":
                rail_conflict_strokes.add(id(stroke))

        # Faithful output keeps non-road source geometry as one centreline.
        # Roads may still use explicit multi/single-nib styling when requested.
        requested_count = (
            1 if preserve_all and stroke.layer not in ROAD_LAYERS else style.strokes
        )
        if stroke.layer in ROAD_LAYERS:
            requested_count = (
                1
                if road_style == "centreline" or retain_conflicting_centreline
                else _road_stroke_count(stroke, style, road_style)
            )
        plan = _physical_plan(
            style=style,
            stroke_count=requested_count,
            inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
            lock_inventory_nib=(
                stroke.layer in ROAD_LAYERS and road_style == "single-nib"
            ),
        )
        physical_nib_mm = plan.pen.mark_width_mm
        count = plan.stroke_count
        pitch_mm = plan.offset_pitch_mm
        plotted_width_mm = plan.plotted_width_mm
        fit_mode = plan.mode
        fit_error_mm = plan.width_error_mm
        centre_geometry: BaseGeometry = line
        context = road_context.get(id(stroke))
        if (
            not preserve_road_network
            and context is not None
            and road_style in {"multi", "single-nib"}
        ):
            knockout = knockout_by_class[
                (context.rank, round(context.plotted_width_mm, 6), context.grade)
            ]
            if not knockout.is_empty and line.intersects(knockout):
                centre_geometry = line.difference(knockout)
                cross_class_trimmed_parts += 1
                cross_class_trimmed_mm += max(0.0, line.length - centre_geometry.length)

        centres = list(_line_parts(centre_geometry))
        preserve_part = preserve_all or (
            preserve_network and stroke.layer in NETWORK_TRAIL_LAYERS
        )
        weighted_parts, candidate_omitted, missing_companions, retained_below = (
            _compile_weighted_parts(
                centres,
                positions=plan.offset_positions(),
                clip=clip,
                minimum_length_mm=max(0.5, 3 * physical_nib_mm),
                preserve_part=preserve_part,
            )
        )
        offset_fallback = count > 1 and bool(centres) and missing_companions > 0
        if offset_fallback:
            offset_fallback_source_strokes += 1
            missing_offset_companion_paths += missing_companions
            count = 1
            pitch_mm = 0.0
            plotted_width_mm = physical_nib_mm
            fit_mode = "offset-fallback-centreline"
            fit_error_mm = plotted_width_mm - plan.requested_width_mm
            weighted_parts, fallback_omitted, _, retained_below = (
                _compile_weighted_parts(
                    centres,
                    positions=[0.0],
                    clip=clip,
                    minimum_length_mm=max(0.5, 3 * physical_nib_mm),
                    preserve_part=preserve_part,
                )
            )
            selected_omissions = fallback_omitted
        else:
            selected_omissions = candidate_omitted

        omitted_short += len(selected_omissions)
        for omitted_part in selected_omissions:
            omitted_points = [(float(x), float(y)) for x, y in omitted_part.line.coords]
            omitted_stroke = replace(
                stroke,
                points=omitted_points,
                part=(
                    f"{stroke.part}:centre-{omitted_part.centre_index}"
                    f":weight-{omitted_part.weight_index}-"
                    f"{omitted_part.part_index}"
                ),
            )
            failure = _minimum_gate_failure(
                omitted_stroke, omitted_part.line, physical_nib_mm
            )
            if failure is None or failure.reason != "below_minimum_serialized_length":
                raise MapPlotterError(
                    "A weighted physical omission did not reproduce its measured "
                    "minimum-length failure."
                )
            record_minimum_omission(
                omitted_stroke,
                input_indexes=(input_index,),
                branch="weighted_part_minimum_gate",
                physical_nib_mm=physical_nib_mm,
                failure=failure,
            )

        if retained_below:
            retained_conflict_strokes.add(id(stroke))
            if stroke.layer in ROAD_LAYERS:
                network_conflict_strokes.add(id(stroke))
            elif stroke.layer == "railways":
                rail_conflict_strokes.add(id(stroke))

        fit_counts[fit_mode] = fit_counts.get(fit_mode, 0) + 1
        assert plan.pen.id is not None
        pen_record = selected_pen_counts.setdefault(
            plan.pen.id,
            {**plan.pen.as_dict(), "source_stroke_count": 0},
        )
        pen_record["source_stroke_count"] += 1
        maximum_absolute_fit_error_mm = max(
            maximum_absolute_fit_error_mm,
            abs(fit_error_mm),
        )
        width_fit_tolerance_mm = max(0.05, plan.requested_width_mm * 0.15)
        if abs(fit_error_mm) > width_fit_tolerance_mm + 1e-9:
            width_fit_tolerance_violation_source_strokes += 1
        if count > 1 and pitch_mm > 0:
            observed_offset_pitch_ratios.add(round(pitch_mm / physical_nib_mm, 6))

        for weighted_part in weighted_parts:
            for pass_index in range(1, style.passes + 1):
                tags = dict(stroke.tags)
                tags.update(
                    {
                        _PHYSICAL_INPUT_INDEXES_TAG: str(input_index),
                        "plot:ink": style.ink,
                        "plot:pen-id": str(plan.pen.id),
                        "plot:nib-mm": _physical_measurement(physical_nib_mm),
                        "plot:nominal-nib-mm": _physical_measurement(
                            plan.pen.nominal_nib_mm
                        ),
                        "plot:calibration-state": plan.pen.calibration_state,
                        "plot:plotted-width-mm": _physical_measurement(
                            plotted_width_mm
                        ),
                        "plot:requested-width-mm": _physical_measurement(
                            plan.requested_width_mm
                        ),
                        "plot:width-fit-error-mm": _physical_measurement(fit_error_mm),
                        "plot:width-fit-mode": fit_mode,
                        "plot:offset-pitch-mm": _physical_measurement(pitch_mm),
                        "plot:stroke-index": str(weighted_part.weight_index),
                        "plot:stroke-count": str(count),
                        "plot:pass-index": str(pass_index),
                        "plot:pass-count": str(style.passes),
                    }
                )
                if pen_inventory is not None:
                    tags["plot:pen-profile"] = pen_inventory.id
                if plan.pen.substrate is not None:
                    tags["plot:calibration-substrate"] = plan.pen.substrate
                if offset_fallback:
                    tags["plot:offset-fallback"] = "centreline"
                if id(stroke) in retained_conflict_strokes:
                    tags["plot:physical-conflict"] = "below-reliable-nib-size"
                output.append(
                    replace(
                        stroke,
                        points=[
                            (float(x), float(y)) for x, y in weighted_part.line.coords
                        ],
                        part=(
                            f"{stroke.part}:centre-{weighted_part.centre_index}"
                            f":weight-{weighted_part.weight_index}-"
                            f"{weighted_part.part_index}:pass-{pass_index}"
                        ),
                        tags=tags,
                        smooth=stroke.smooth and not weighted_part.line.is_ring,
                    )
                )
                offset_paths += int(abs(weighted_part.distance_mm) > 1e-9)
                repeated_paths += int(pass_index > 1)

    initial_network_conflicts = len(network_conflict_strokes)
    initial_rail_conflicts = len(rail_conflict_strokes)
    initial_physical_conflicts = len(retained_conflict_strokes)
    if preserve_road_network:
        trail_assembly = _assemble_road_network_trails(output)
        output = trail_assembly.strokes
    else:
        trail_assembly = _NetworkTrailAssembly(
            strokes=output,
            diagnostics={
                "enabled": False,
                "method": "exact-endpoint edge-disjoint trail decomposition",
                "reason": "road-network preservation was not requested",
            },
        )
    if drop_residual_conflicts:
        filtered_output: list[PlotStroke] = []
        for stroke in output:
            length_mm = _serialized_polyline_length(stroke.points)
            if length_mm + 1e-9 < _minimum_stroke_mm(stroke):
                omitted_short += 1
                input_indexes = tuple(
                    int(item)
                    for item in stroke.tags.get(_PHYSICAL_INPUT_INDEXES_TAG, "").split(
                        ";"
                    )
                    if item
                )
                omitted_nib_mm = _number(stroke.tags.get("plot:nib-mm"))
                if not input_indexes or omitted_nib_mm is None:
                    raise MapPlotterError(
                        "A residual physical omission is missing compiler lineage "
                        "or its effective nib width."
                    )
                line = LineString(stroke.points)
                failure = _minimum_gate_failure(stroke, line, omitted_nib_mm)
                if (
                    failure is None
                    or failure.reason != "below_minimum_serialized_length"
                ):
                    raise MapPlotterError(
                        "A residual physical omission did not reproduce its measured "
                        "minimum-length failure."
                    )
                record_minimum_omission(
                    stroke,
                    input_indexes=input_indexes,
                    branch="residual_network_trail_minimum_gate",
                    physical_nib_mm=omitted_nib_mm,
                    failure=failure,
                )
                continue
            if stroke.tags.get("plot:physical-conflict") == "below-reliable-nib-size":
                tags = dict(stroke.tags)
                tags.pop("plot:physical-conflict", None)
                stroke = replace(stroke, tags=tags)
            filtered_output.append(stroke)
        output = filtered_output
    residual_network_conflicts = sum(
        stroke.layer in ROAD_LAYERS
        and stroke.tags.get("plot:physical-conflict") == "below-reliable-nib-size"
        for stroke in output
    )
    residual_rail_conflicts = sum(
        stroke.layer == "railways"
        and stroke.tags.get("plot:physical-conflict") == "below-reliable-nib-size"
        for stroke in output
    )
    residual_physical_conflicts = sum(
        stroke.tags.get("plot:physical-conflict") == "below-reliable-nib-size"
        for stroke in output
    )
    cleaned_output: list[PlotStroke] = []
    for stroke in output:
        if _PHYSICAL_INPUT_INDEXES_TAG not in stroke.tags:
            cleaned_output.append(stroke)
            continue
        tags = dict(stroke.tags)
        tags.pop(_PHYSICAL_INPUT_INDEXES_TAG, None)
        cleaned_output.append(replace(stroke, tags=tags))
    output = cleaned_output

    if len(omission_evidence) != omitted_short:
        raise MapPlotterError(
            "Physical minimum omission accounting is incomplete: the aggregate "
            "count does not match the evidence ledger."
        )
    evidenced_source_refs = sorted(
        {
            source_ref
            for omission in omission_evidence
            for source_ref in omission.source_refs
        }
    )
    input_requirements: dict[str, set[tuple[int, str]]] = defaultdict(set)
    for input_index, stroke in enumerate(strokes):
        for source_ref in _source_refs(stroke):
            input_requirements[source_ref].add((input_index, stroke.layer))
    output_source_refs = _source_ref_set(output)
    fully_omitted_source_refs = sorted(set(input_requirements) - output_source_refs)
    evidence_claims: dict[str, set[tuple[int, str]]] = defaultdict(set)
    for omission in omission_evidence:
        for input_stroke in omission.input_strokes:
            for source_ref in input_stroke.source_refs:
                evidence_claims[source_ref].add(
                    (input_stroke.index, input_stroke.layer)
                )
    fully_evidenced_source_refs = sorted(
        source_ref
        for source_ref in fully_omitted_source_refs
        if input_requirements[source_ref] <= evidence_claims.get(source_ref, set())
    )
    unevidenced_fully_omitted_source_refs = sorted(
        set(fully_omitted_source_refs) - set(fully_evidenced_source_refs)
    )
    omission_ledger = {
        "schema_version": 1,
        "measurement_scope": (
            "Every path deliberately removed by a physical minimum gate; lengths "
            "and geometry fingerprints use coordinates rounded to 0.001 mm."
        ),
        "entry_count": len(omission_evidence),
        "source_ref_count": len(evidenced_source_refs),
        "evidenced_source_refs": evidenced_source_refs,
        "fully_omitted_source_ref_count": len(fully_omitted_source_refs),
        "fully_omitted_source_refs": fully_omitted_source_refs,
        "fully_omitted_source_refs_with_complete_input_evidence": (
            fully_evidenced_source_refs
        ),
        "fully_omitted_source_refs_without_complete_input_evidence": (
            unevidenced_fully_omitted_source_refs
        ),
        "fully_omitted_source_evidence_complete": not (
            unevidenced_fully_omitted_source_refs
        ),
        "entries": [item.as_dict() for item in omission_evidence],
    }

    warnings: list[str] = []
    if omitted_short:
        warnings.append(
            f"Omitted {omitted_short} map fragments shorter or smaller than "
            "the physical nib can render reliably."
        )
    joined_conflicts = initial_network_conflicts - residual_network_conflicts
    if joined_conflicts > 0:
        warnings.append(
            f"Joined {joined_conflicts} short road edges into continuous, "
            "edge-disjoint pen trails without changing their geometry or source "
            "lineage."
        )
    joined_rail_conflicts = initial_rail_conflicts - residual_rail_conflicts
    if joined_rail_conflicts > 0:
        warnings.append(
            f"Joined {joined_rail_conflicts} short railway edges into continuous, "
            "edge-disjoint pen trails without changing their geometry or source "
            "lineage."
        )
    if residual_physical_conflicts:
        warnings.append(
            f"Retained {residual_physical_conflicts} map trails below the reliable "
            "nib size for fidelity. Use a larger sheet, tighter map extent, or a "
            "finer supplied pen; the compiler did not drop or overtrace them."
        )
    if offset_fallback_source_strokes:
        warnings.append(
            f"Fell back to single centrelines for {offset_fallback_source_strokes} "
            f"source strokes because {missing_offset_companion_paths} expected offset "
            "companions could not be emitted; reported plotted widths were reduced "
            "to the physical nib width."
        )
    unmeasured_pen_ids = sorted(
        pen_id
        for pen_id, record in selected_pen_counts.items()
        if record["calibration_state"] == "nominal-unmeasured"
    )
    if pen_inventory is not None and unmeasured_pen_ids:
        warnings.append(
            "Physical geometry uses nominal, unmeasured widths for pens "
            f"{', '.join(unmeasured_pen_ids)}. Plot and measure the calibration "
            "card before production, then supply effective_width_mm values."
        )
    return PhysicalCompileResult(
        strokes=output,
        diagnostics={
            "road_style": road_style,
            "source_strokes": source_strokes,
            "physical_strokes": len(output),
            "offset_paths": offset_paths,
            "repeat_pass_paths": repeated_paths,
            "cross_class_trimmed_source_strokes": cross_class_trimmed_parts,
            "cross_class_trimmed_mm": round(cross_class_trimmed_mm, 3),
            "omitted_sub_nib_fragments": omitted_short,
            "physical_minimum_omission_ledger": omission_ledger,
            "offset_fallback_source_strokes": offset_fallback_source_strokes,
            "missing_offset_companion_paths": missing_offset_companion_paths,
            "preserve_network": int(preserve_road_network),
            "preserve_network_requested": int(preserve_network),
            "preserve_all_features": int(preserve_all),
            "residual_conflicts_dropped": int(drop_residual_conflicts),
            "initial_network_conflicts": initial_network_conflicts,
            "initial_rail_conflicts": initial_rail_conflicts,
            "initial_physical_conflicts": initial_physical_conflicts,
            "reported_network_conflicts": residual_network_conflicts,
            "reported_rail_conflicts": residual_rail_conflicts,
            "reported_physical_conflicts": residual_physical_conflicts,
            "network_trail_assembly": trail_assembly.diagnostics,
            "offset_pitch_policy": (
                "exact-target-with-0.5-to-0.9-effective-nib-pitch"
                if pen_inventory is not None
                else "fixed-0.85-nib-pitch"
            ),
            "offset_pitch_nib_ratio": (None if pen_inventory is not None else 0.85),
            "observed_offset_pitch_nib_ratios": sorted(observed_offset_pitch_ratios),
            "pen_profile": (pen_inventory.id if pen_inventory is not None else "style"),
            "pen_inventory": (
                pen_inventory.as_dict() if pen_inventory is not None else None
            ),
            "allowed_nibs_mm": (
                list(allowed_nibs_mm) if allowed_nibs_mm is not None else None
            ),
            "width_fit_source_strokes_by_mode": dict(sorted(fit_counts.items())),
            "maximum_absolute_width_fit_error_mm": round(
                maximum_absolute_fit_error_mm, 6
            ),
            "width_fit_tolerance_violation_source_strokes": (
                width_fit_tolerance_violation_source_strokes
            ),
            "selected_physical_pens": [
                selected_pen_counts[pen_id] for pen_id in sorted(selected_pen_counts)
            ],
            "uncalibrated_pen_ids": unmeasured_pen_ids,
            "same_line_passes_added_for_width": 0,
            "repeat_passes_explicitly_approved": int(allow_repeat_passes),
            "repeat_pass_layers": repeated_style_layers,
        },
        warnings=warnings,
        omissions=tuple(omission_evidence),
    )
