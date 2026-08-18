"""Topology-aware line generalisation for faithful plot geometry.

The rendering pipeline works in page millimetres, so tolerances in this module
are physical distances rather than map-coordinate guesses.  The important
distinction is between source topology and geometric intersection: two roads
that cross on the page are *not* connected unless they share an input vertex
or, preferably, an OpenStreetMap node reference.

This module is deliberately independent from the poster-selection policy.  It
can therefore be used by a lossless/fidelity profile without inheriting length
budgets or minimum-fragment deletion.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import hypot, isfinite
from typing import TypeAlias, cast

from shapely import frechet_distance, hausdorff_distance
from shapely.geometry import GeometryCollection, LineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import Layout, simplify_polyline
from .models import MapFeature, MapPlotterError, PlotStroke


Point2D: TypeAlias = tuple[float, float]
SourceKey: TypeAlias = tuple[str, str, str]
NodeKey: TypeAlias = tuple[object, ...]
TagItems: TypeAlias = tuple[tuple[str, str], ...]
LayerTolerance: TypeAlias = float | Mapping[str, float]


# A change in one of these values marks a drawing/style transition.  Features
# are expected to be split where their tags change (as OSM ways normally are),
# so the transition occurs at the shared endpoint between two source lines.
DEFAULT_SEMANTIC_KEYS = (
    "highway",
    "construction",
    "name",
    "ref",
    "access",
    "service",
    "junction",
    "oneway",
    "lanes",
    "width",
    "surface",
    "route",
)
DEFAULT_GRADE_KEYS = ("layer", "level", "bridge", "tunnel", "covered", "ford")


def _tag_items(tags: Mapping[str, str]) -> TagItems:
    return tuple(sorted((str(key), str(value)) for key, value in tags.items()))


def _tag_dict(items: TagItems) -> dict[str, str]:
    return dict(items)


def _clean_node_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.casefold() == "unknown" else text


def _boolish_tag(tags: Mapping[str, str], key: str) -> str:
    value = tags.get(key, "").strip().casefold()
    return "no" if value in {"", "0", "false", "no"} else value


def normalize_osm_level(value: str | None) -> str:
    """Return a stable scalar OSM level without guessing compound syntax.

    An absent level denotes the ordinary zero-level domain, so numeric spellings
    of zero must compare equal to it. Scalar decimal values are canonicalised;
    semicolon lists, ranges, and named levels remain opaque after outer
    whitespace removal so distinct indoor domains are never merged by guesswork.
    """

    text = "" if value is None else str(value).strip()
    if not text:
        return "0"
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if not number.is_finite():
        return text
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def semantic_signature(
    layer: str,
    tags: Mapping[str, str],
    *,
    keys: Sequence[str] = DEFAULT_SEMANTIC_KEYS,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Return the attributes across which road chains must not be merged."""

    return (
        layer,
        tuple((key, tags.get(key, "").strip()) for key in keys),
    )


def grade_signature(
    tags: Mapping[str, str],
    *,
    keys: Sequence[str] = DEFAULT_GRADE_KEYS,
) -> tuple[tuple[str, str], ...]:
    """Return a normalised structure/layer/indoor-level signature.

    Missing ``layer`` and ``level`` are equivalent to their respective zero
    domains, while false-like structure tags are normalised to ``no``. This
    prevents coordinate fallback from connecting a bridge, tunnel, or indoor
    floor to visually coincident linework in another physical domain.
    """

    values: list[tuple[str, str]] = []
    for key in keys:
        if key == "layer":
            values.append((key, tags.get(key, "0").strip() or "0"))
        elif key == "level":
            values.append((key, normalize_osm_level(tags.get(key))))
        else:
            values.append((key, _boolish_tag(tags, key)))
    return tuple(values)


@dataclass(frozen=True, order=True)
class SourceReference:
    """Stable source lineage retained through splitting and chain assembly."""

    osm_type: str
    osm_id: str
    part: str = "0"

    @property
    def key(self) -> SourceKey:
        return (self.osm_type, self.osm_id, self.part)

    @property
    def label(self) -> str:
        return f"{self.osm_type}/{self.osm_id}/{self.part}"


@dataclass(frozen=True)
class TopologyLine:
    """One source polyline in page space, optionally with OSM node IDs."""

    line_id: str
    layer: str
    points: tuple[Point2D, ...]
    sources: tuple[SourceReference, ...]
    tags: TagItems = ()
    node_ids: tuple[str | None, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "points",
            tuple((float(point[0]), float(point[1])) for point in self.points),
        )
        object.__setattr__(self, "sources", tuple(dict.fromkeys(self.sources)))
        object.__setattr__(self, "tags", _tag_items(dict(self.tags)))
        object.__setattr__(
            self,
            "node_ids",
            tuple(_clean_node_id(node_id) for node_id in self.node_ids),
        )
        if not self.line_id:
            raise MapPlotterError("A topology line must have a stable line_id.")
        if len(self.points) < 2:
            raise MapPlotterError("A topology line requires at least two points.")
        if not all(isfinite(value) for point in self.points for value in point):
            raise MapPlotterError("Topology coordinates must be finite numbers.")
        if self.node_ids and len(self.node_ids) != len(self.points):
            raise MapPlotterError(
                "A topology line's node_ids must align one-for-one with its points."
            )
        if not self.sources:
            raise MapPlotterError("A topology line must retain at least one source.")

    @property
    def tags_dict(self) -> dict[str, str]:
        return _tag_dict(self.tags)

    @property
    def semantic_key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        return semantic_signature(self.layer, self.tags_dict)

    @property
    def grade_key(self) -> tuple[tuple[str, str], ...]:
        return grade_signature(self.tags_dict)

    @classmethod
    def from_map_feature(
        cls,
        feature: MapFeature,
        *,
        layout: Layout,
        node_ids: Sequence[str | int | None] | None = None,
        line_id: str | None = None,
    ) -> "TopologyLine":
        """Project a ``MapFeature`` into page space without losing lineage."""

        resolved_node_ids = node_ids
        if resolved_node_ids is None:
            resolved_node_ids = feature.node_refs
        return cls(
            line_id=line_id
            or f"map:{feature.osm_type}:{feature.osm_id}:{feature.part}",
            layer=feature.layer,
            points=tuple(
                layout.project_to_page(latitude, longitude)
                for latitude, longitude in feature.points
            ),
            sources=(SourceReference(feature.osm_type, feature.osm_id, feature.part),),
            tags=_tag_items(feature.tags),
            node_ids=tuple(_clean_node_id(node_id) for node_id in resolved_node_ids),
        )

    @classmethod
    def from_plot_stroke(
        cls,
        stroke: PlotStroke,
        *,
        node_ids: Sequence[str | int | None] | None = None,
        line_id: str | None = None,
    ) -> "TopologyLine":
        """Adapt a page-space ``PlotStroke`` for validation/generalisation."""

        return cls(
            line_id=line_id
            or f"stroke:{stroke.osm_type}:{stroke.osm_id}:{stroke.part}",
            layer=stroke.layer,
            points=tuple(stroke.points),
            sources=(SourceReference(stroke.osm_type, stroke.osm_id, stroke.part),),
            tags=_tag_items(stroke.tags),
            node_ids=tuple(_clean_node_id(node_id) for node_id in (node_ids or ())),
        )


def topology_lines_from_map_features(
    features: Iterable[MapFeature],
    layout: Layout,
    *,
    node_ids_by_source: Mapping[SourceKey, Sequence[str | int | None]] | None = None,
) -> tuple[TopologyLine, ...]:
    """Project map features and attach node IDs supplied by an ingest layer."""

    node_ids_by_source = node_ids_by_source or {}
    lines: list[TopologyLine] = []
    used_ids: Counter[str] = Counter()
    for feature in features:
        key = (feature.osm_type, feature.osm_id, feature.part)
        base_id = f"map:{feature.osm_type}:{feature.osm_id}:{feature.part}"
        suffix = used_ids[base_id]
        used_ids[base_id] += 1
        lines.append(
            TopologyLine.from_map_feature(
                feature,
                layout=layout,
                node_ids=node_ids_by_source.get(key),
                line_id=base_id if suffix == 0 else f"{base_id}:{suffix}",
            )
        )
    return tuple(lines)


def topology_lines_from_plot_strokes(
    strokes: Iterable[PlotStroke],
) -> tuple[TopologyLine, ...]:
    """Adapt plot strokes while making duplicate line IDs deterministic."""

    lines: list[TopologyLine] = []
    used_ids: Counter[str] = Counter()
    for stroke in strokes:
        base_id = f"stroke:{stroke.osm_type}:{stroke.osm_id}:{stroke.part}"
        suffix = used_ids[base_id]
        used_ids[base_id] += 1
        lines.append(
            TopologyLine.from_plot_stroke(
                stroke,
                line_id=base_id if suffix == 0 else f"{base_id}:{suffix}",
            )
        )
    return tuple(lines)


@dataclass(frozen=True)
class TopologyAnchor:
    line_id: str
    vertex_index: int
    point: Point2D
    node_key: NodeKey
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TopologySpan:
    """The source geometry between two consecutive protected anchors."""

    span_id: str
    line_id: str
    layer: str
    points: tuple[Point2D, ...]
    start_node: NodeKey
    end_node: NodeKey
    sources: tuple[SourceReference, ...]
    tags: TagItems
    semantic_key: tuple[str, tuple[tuple[str, str], ...]]
    grade_key: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TopologyNetwork:
    """Non-planar source topology plus anchor-to-anchor simplification spans."""

    lines: tuple[TopologyLine, ...]
    spans: tuple[TopologySpan, ...]
    anchors: tuple[TopologyAnchor, ...]
    node_degrees: Mapping[NodeKey, int]
    node_occurrences: Mapping[NodeKey, tuple[tuple[str, int], ...]]

    def anchors_for_line(self, line_id: str) -> tuple[TopologyAnchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.line_id == line_id)


def _coordinate_key(
    point: Point2D,
    grade: tuple[tuple[str, str], ...],
    precision: int,
) -> NodeKey:
    return ("coordinate", round(point[0], precision), round(point[1], precision), grade)


def build_road_topology(
    lines: Iterable[TopologyLine],
    *,
    protected_node_ids: Set[str] = frozenset(),
    protected_points: Iterable[Point2D] = (),
    coordinate_precision: int = 9,
    node_coordinate_epsilon_mm: float = 1e-6,
) -> TopologyNetwork:
    """Build a road graph from source vertices without planar noding.

    Explicit OSM node IDs take precedence over coordinates.  Without those
    IDs, equal page coordinates are considered connected only within the same
    grade signature.  Intersections that fall between supplied vertices are
    intentionally ignored.
    """

    source_lines = tuple(lines)
    if not source_lines:
        return TopologyNetwork((), (), (), {}, {})
    if len({line.line_id for line in source_lines}) != len(source_lines):
        raise MapPlotterError("Topology line_id values must be unique.")
    if not isinstance(coordinate_precision, int) or coordinate_precision < 0:
        raise MapPlotterError("coordinate_precision must be a non-negative integer.")
    if not isfinite(node_coordinate_epsilon_mm) or node_coordinate_epsilon_mm < 0:
        raise MapPlotterError("Node coordinate epsilon cannot be negative.")

    explicit_points: dict[NodeKey, Point2D] = {}
    keys_by_line: list[tuple[NodeKey, ...]] = []
    physical_keys_by_line: list[tuple[tuple[float, float], ...]] = []
    occurrences: defaultdict[NodeKey, list[tuple[str, int]]] = defaultdict(list)
    physical_occurrences: defaultdict[tuple[float, float], list[tuple[int, int]]] = (
        defaultdict(list)
    )

    for line_index, line in enumerate(source_lines):
        keys: list[NodeKey] = []
        physical_keys: list[tuple[float, float]] = []
        for index, point in enumerate(line.points):
            node_id = line.node_ids[index] if line.node_ids else None
            if node_id is not None:
                key: NodeKey = ("osm", node_id)
                previous = explicit_points.setdefault(key, point)
                if hypot(previous[0] - point[0], previous[1] - point[1]) > (
                    node_coordinate_epsilon_mm
                ):
                    raise MapPlotterError(
                        f"OSM node {node_id!r} has inconsistent page coordinates."
                    )
            else:
                key = _coordinate_key(point, line.grade_key, coordinate_precision)
            physical_key = (
                round(point[0], coordinate_precision),
                round(point[1], coordinate_precision),
            )
            keys.append(key)
            physical_keys.append(physical_key)
            occurrences[key].append((line.line_id, index))
            physical_occurrences[physical_key].append((line_index, index))
        keys_by_line.append(tuple(keys))
        physical_keys_by_line.append(tuple(physical_keys))

    incidence: defaultdict[NodeKey, set[tuple[int, int, int]]] = defaultdict(set)
    for line_index, line_keys in enumerate(keys_by_line):
        for segment_index, (start, end) in enumerate(zip(line_keys, line_keys[1:])):
            if start == end:
                continue
            incidence[start].add((line_index, segment_index, 0))
            incidence[end].add((line_index, segment_index, 1))
    node_degrees = {key: len(ends) for key, ends in incidence.items()}

    protected_nodes = {_clean_node_id(node_id) for node_id in protected_node_ids}
    protected_nodes.discard(None)
    protected_physical = {
        (round(point[0], coordinate_precision), round(point[1], coordinate_precision))
        for point in protected_points
    }

    anchors: list[TopologyAnchor] = []
    spans: list[TopologySpan] = []
    for line_index, line in enumerate(source_lines):
        line_keys = keys_by_line[line_index]
        anchor_indices: list[int] = []
        for index, (point, key) in enumerate(zip(line.points, line_keys, strict=True)):
            reasons: list[str] = []
            if index in {0, len(line.points) - 1}:
                reasons.append("endpoint")
            if node_degrees.get(key, 0) != 2:
                reasons.append("degree-not-two")

            distinct_lines = {item[0] for item in occurrences[key]}
            if len(distinct_lines) > 1:
                reasons.append("shared-node")

            physical_key = physical_keys_by_line[line_index][index]
            physical_lines = {
                source_lines[item[0]].line_id
                for item in physical_occurrences[physical_key]
            }
            if len(physical_lines) > 1:
                reasons.append("shared-coordinate")

            physical_semantics = {
                source_lines[item[0]].semantic_key
                for item in physical_occurrences[physical_key]
            }
            if len(physical_semantics) > 1:
                reasons.append("semantic-transition")
            physical_grades = {
                source_lines[item[0]].grade_key
                for item in physical_occurrences[physical_key]
            }
            if len(physical_grades) > 1:
                reasons.append("grade-transition")

            node_id = line.node_ids[index] if line.node_ids else None
            if node_id in protected_nodes or physical_key in protected_physical:
                reasons.append("explicitly-protected")

            if reasons:
                anchor_indices.append(index)
                anchors.append(
                    TopologyAnchor(
                        line.line_id,
                        index,
                        point,
                        key,
                        tuple(dict.fromkeys(reasons)),
                    )
                )

        # Endpoints guarantee a non-empty list.  Deduplication also protects
        # unusual one-segment lines where multiple reasons identify a vertex.
        anchor_indices = sorted(set(anchor_indices))
        for start_index, end_index in zip(anchor_indices, anchor_indices[1:]):
            span_points = line.points[start_index : end_index + 1]
            if len(span_points) < 2 or len(set(span_points)) < 2:
                continue
            spans.append(
                TopologySpan(
                    span_id=f"{line.line_id}@{start_index}:{end_index}",
                    line_id=line.line_id,
                    layer=line.layer,
                    points=span_points,
                    start_node=line_keys[start_index],
                    end_node=line_keys[end_index],
                    sources=line.sources,
                    tags=line.tags,
                    semantic_key=line.semantic_key,
                    grade_key=line.grade_key,
                )
            )

    frozen_occurrences = {key: tuple(items) for key, items in occurrences.items()}
    return TopologyNetwork(
        source_lines,
        tuple(spans),
        tuple(anchors),
        node_degrees,
        frozen_occurrences,
    )


@dataclass(frozen=True)
class GeometryErrorMetrics:
    source_vertices: int
    output_vertices: int
    source_length_mm: float
    output_length_mm: float
    length_ratio: float
    hausdorff_mm: float
    frechet_mm: float


@dataclass(frozen=True)
class BoundedPolylineRounding:
    """A sampled corner rounding whose emitted centreline has been validated.

    The returned points are the actual line segments written to the SVG, not
    merely control points for a later unmeasured curve.  Consequently the
    reported error metrics describe the geometry that a plotter receives.
    Endpoints are never moved, which lets topology chains meet exactly at road
    junctions and at grade transitions.
    """

    points: tuple[Point2D, ...]
    metrics: GeometryErrorMetrics
    tolerance_mm: float
    rounded_corner_count: int
    fallback_to_source: bool

    @property
    def applied(self) -> bool:
        return self.rounded_corner_count > 0 and not self.fallback_to_source


def geometry_error_metrics(
    source_points: Sequence[Point2D],
    output_points: Sequence[Point2D],
    *,
    densify: float = 0.1,
) -> GeometryErrorMetrics:
    """Measure page-space displacement and length change between two lines."""

    if len(source_points) < 2 or len(output_points) < 2:
        raise MapPlotterError("Geometry error metrics require two valid polylines.")
    if not 0 < densify <= 1:
        raise MapPlotterError(
            "Hausdorff densify must be greater than zero and at most one."
        )
    source = LineString(source_points)
    output = LineString(output_points)
    ratio = output.length / source.length if source.length > 0 else 1.0
    identical = tuple(source_points) == tuple(output_points)
    return GeometryErrorMetrics(
        source_vertices=len(source_points),
        output_vertices=len(output_points),
        source_length_mm=source.length,
        output_length_mm=output.length,
        length_ratio=ratio,
        hausdorff_mm=(
            0.0
            if identical
            else float(hausdorff_distance(source, output, densify=densify))
        ),
        frechet_mm=(
            0.0
            if identical
            else float(frechet_distance(source, output, densify=densify))
        ),
    )


def within_page_error_bound(
    source_points: Sequence[Point2D],
    output_points: Sequence[Point2D],
    tolerance_mm: float,
) -> bool:
    """Conservatively test a symmetric physical-distance error envelope.

    Both polylines must be covered by the other's page-space buffer.  GEOS
    constructs circular buffers from inscribed chords, so passing this test is
    at least as strict as the requested Euclidean tolerance (apart from normal
    floating-point epsilon).  This is stronger than relying solely on a
    discrete Hausdorff sample.
    """

    if not isfinite(tolerance_mm) or tolerance_mm < 0:
        raise MapPlotterError("Geometry error tolerance cannot be negative.")
    if len(source_points) < 2 or len(output_points) < 2:
        raise MapPlotterError("Geometry error bounds require two valid polylines.")
    if tuple(source_points) == tuple(output_points):
        return True
    if tolerance_mm == 0:
        return False
    source = LineString(source_points)
    output = LineString(output_points)
    # The tiny additive epsilon prevents a point lying exactly on a theoretical
    # boundary from failing because of one floating-point ulp.
    buffered_tolerance = tolerance_mm + 1e-12
    return source.buffer(buffered_tolerance, quad_segs=16).covers(
        output
    ) and output.buffer(buffered_tolerance, quad_segs=16).covers(source)


def _sample_rounded_polyline(
    points: tuple[Point2D, ...],
    *,
    maximum_trim_mm: float,
    samples_per_corner: int,
    protected_points: Set[Point2D],
) -> tuple[tuple[Point2D, ...], int]:
    """Replace eligible open-polyline corners with sampled quadratic fillets."""

    if len(points) < 3 or points[0] == points[-1] or maximum_trim_mm <= 0:
        return points, 0

    output: list[Point2D] = [points[0]]
    rounded_corners = 0
    for before, vertex, after in zip(points, points[1:], points[2:], strict=False):
        if vertex in protected_points:
            if output[-1] != vertex:
                output.append(vertex)
            continue
        incoming_x = vertex[0] - before[0]
        incoming_y = vertex[1] - before[1]
        outgoing_x = after[0] - vertex[0]
        outgoing_y = after[1] - vertex[1]
        incoming_length = hypot(incoming_x, incoming_y)
        outgoing_length = hypot(outgoing_x, outgoing_y)
        if incoming_length <= 1e-12 or outgoing_length <= 1e-12:
            if output[-1] != vertex:
                output.append(vertex)
            continue

        incoming_unit = (
            incoming_x / incoming_length,
            incoming_y / incoming_length,
        )
        outgoing_unit = (
            outgoing_x / outgoing_length,
            outgoing_y / outgoing_length,
        )
        dot = incoming_unit[0] * outgoing_unit[0] + incoming_unit[1] * outgoing_unit[1]
        cross = (
            incoming_unit[0] * outgoing_unit[1] - incoming_unit[1] * outgoing_unit[0]
        )
        # Do not add points to an already straight run, and do not invent a
        # loop at a source hairpin.  Round joins still soften either case at
        # the physical nib boundary.
        if (abs(cross) <= 1e-7 and dot >= 0) or dot <= -0.98:
            if output[-1] != vertex:
                output.append(vertex)
            continue

        trim = min(
            maximum_trim_mm,
            incoming_length * 0.25,
            outgoing_length * 0.25,
        )
        if trim <= 1e-9:
            if output[-1] != vertex:
                output.append(vertex)
            continue
        entry = (
            vertex[0] - incoming_unit[0] * trim,
            vertex[1] - incoming_unit[1] * trim,
        )
        exit_point = (
            vertex[0] + outgoing_unit[0] * trim,
            vertex[1] + outgoing_unit[1] * trim,
        )
        if output[-1] != entry:
            output.append(entry)
        for sample_index in range(1, samples_per_corner + 1):
            parameter = sample_index / samples_per_corner
            inverse = 1.0 - parameter
            sample = (
                inverse * inverse * entry[0]
                + 2.0 * inverse * parameter * vertex[0]
                + parameter * parameter * exit_point[0],
                inverse * inverse * entry[1]
                + 2.0 * inverse * parameter * vertex[1]
                + parameter * parameter * exit_point[1],
            )
            if output[-1] != sample:
                output.append(sample)
        rounded_corners += 1

    if output[-1] != points[-1]:
        output.append(points[-1])
    return tuple(output), rounded_corners


def round_polyline_within_page_error(
    points: Sequence[Point2D],
    tolerance_mm: float,
    *,
    samples_per_corner: int = 4,
    protected_points: Set[Point2D] = frozenset(),
) -> BoundedPolylineRounding:
    """Round eligible corners without exceeding a symmetric page-mm bound.

    Quadratic fillets are flattened immediately, so downstream length,
    clipping, optimisation, and physical-resolution checks all operate on the
    same centreline that is eventually emitted.  The initial tangent trim is
    deliberately generous for visible softness; it is halved until the exact
    emitted polyline passes :func:`within_page_error_bound`.
    """

    resolved = tuple((float(point[0]), float(point[1])) for point in points)
    if len(resolved) < 2:
        raise MapPlotterError("Polyline rounding requires at least two points.")
    if not isfinite(tolerance_mm) or tolerance_mm < 0:
        raise MapPlotterError("Polyline rounding tolerance cannot be negative.")
    if isinstance(samples_per_corner, bool) or not isinstance(samples_per_corner, int):
        raise MapPlotterError("Corner sample count must be an integer.")
    if samples_per_corner < 2 or samples_per_corner > 32:
        raise MapPlotterError("Corner sample count must be between 2 and 32.")
    resolved_protected = {
        (float(point[0]), float(point[1])) for point in protected_points
    }

    exact_metrics = geometry_error_metrics(resolved, resolved)
    if tolerance_mm == 0 or len(resolved) < 3 or resolved[0] == resolved[-1]:
        return BoundedPolylineRounding(
            resolved,
            exact_metrics,
            float(tolerance_mm),
            0,
            False,
        )

    # A right-angle quadratic fillet's greatest displacement is roughly one
    # quarter of its tangent trim.  Direct validation below remains the source
    # of truth for acute, obtuse, and irregular corners.
    maximum_trim = tolerance_mm * 4.0
    for _ in range(12):
        candidate, corner_count = _sample_rounded_polyline(
            resolved,
            maximum_trim_mm=maximum_trim,
            samples_per_corner=samples_per_corner,
            protected_points=resolved_protected,
        )
        if corner_count == 0:
            return BoundedPolylineRounding(
                resolved,
                exact_metrics,
                float(tolerance_mm),
                0,
                False,
            )
        if within_page_error_bound(resolved, candidate, tolerance_mm):
            return BoundedPolylineRounding(
                candidate,
                geometry_error_metrics(resolved, candidate),
                float(tolerance_mm),
                corner_count,
                False,
            )
        maximum_trim *= 0.5

    # Fidelity is more important than softness.  This path should be extremely
    # rare, but makes the bound an invariant instead of a best effort.
    return BoundedPolylineRounding(
        resolved,
        exact_metrics,
        float(tolerance_mm),
        0,
        True,
    )


@dataclass(frozen=True)
class GeneralizedSpan:
    span: TopologySpan
    points: tuple[Point2D, ...]
    metrics: GeometryErrorMetrics


@dataclass(frozen=True)
class GeneralizedChain:
    """Compatible degree-two spans assembled without losing source lineage."""

    chain_id: str
    layer: str
    points: tuple[Point2D, ...]
    start_node: NodeKey
    end_node: NodeKey
    span_ids: tuple[str, ...]
    sources: tuple[SourceReference, ...]
    source_tags: tuple[tuple[SourceReference, TagItems], ...]
    semantic_key: tuple[str, tuple[tuple[str, str], ...]]
    grade_key: tuple[tuple[str, str], ...]

    def to_plot_stroke(self, *, part: str | None = None) -> PlotStroke:
        """Create a non-smoothed plot stroke with serialised source references."""

        common_tags: dict[str, str] = {}
        tag_dicts = [dict(items) for _, items in self.source_tags]
        if tag_dicts:
            common_tags = {
                key: value
                for key, value in tag_dicts[0].items()
                if all(tags.get(key) == value for tags in tag_dicts[1:])
            }
        common_tags.update(
            {
                "compiled": "topology",
                "source-count": str(len(self.sources)),
                "source-refs": ";".join(source.label for source in self.sources),
            }
        )
        names = {
            tags.get("name") or tags.get("ref")
            for tags in tag_dicts
            if tags.get("name") or tags.get("ref")
        }
        return PlotStroke(
            layer=self.layer,
            points=list(self.points),
            osm_type=(
                self.sources[0].osm_type if len(self.sources) == 1 else "compiled"
            ),
            osm_id=(self.sources[0].osm_id if len(self.sources) == 1 else "multiple"),
            part=part or self.chain_id,
            tags=common_tags,
            name=next(iter(names)) if len(names) == 1 else None,
            smooth=False,
        )


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    span_id: str | None = None


@dataclass(frozen=True)
class TopologyValidation:
    valid: bool
    issues: tuple[ValidationIssue, ...]
    max_anchor_displacement_mm: float
    max_hausdorff_mm: float
    connectivity_preserved: bool


@dataclass(frozen=True)
class TopologyGeneralization:
    network: TopologyNetwork
    spans: tuple[GeneralizedSpan, ...]
    chains: tuple[GeneralizedChain, ...]
    validation: TopologyValidation
    tolerance_mm: LayerTolerance

    def to_plot_strokes(self) -> list[PlotStroke]:
        return [
            chain.to_plot_stroke(part=f"topology:{index}")
            for index, chain in enumerate(self.chains)
        ]


def _layer_tolerance(tolerance_mm: LayerTolerance, layer: str) -> float:
    """Resolve one page-space tolerance, optionally by output road layer."""

    if isinstance(tolerance_mm, Mapping):
        try:
            value = tolerance_mm[layer]
        except KeyError as exc:
            raise MapPlotterError(
                f"No road simplification tolerance was supplied for layer {layer!r}."
            ) from exc
    else:
        value = tolerance_mm
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MapPlotterError("Road simplification tolerances must be finite numbers.")
    resolved = float(value)
    if not isfinite(resolved) or resolved < 0:
        raise MapPlotterError("Road simplification tolerance cannot be negative.")
    return resolved


def _validate_layer_tolerances(
    tolerance_mm: LayerTolerance, network: TopologyNetwork
) -> None:
    layers = {span.layer for span in network.spans} or {
        line.layer for line in network.lines
    }
    if layers:
        for layer in layers:
            _layer_tolerance(tolerance_mm, layer)
    elif not isinstance(tolerance_mm, Mapping):
        _layer_tolerance(tolerance_mm, "")


def _bounded_simplify(
    points: tuple[Point2D, ...],
    tolerance_mm: float,
    *,
    densify: float,
) -> tuple[tuple[Point2D, ...], GeometryErrorMetrics]:
    if tolerance_mm <= 0 or len(points) <= 2:
        output = points
    else:
        output = tuple(simplify_polyline(list(points), tolerance_mm))
    metrics = geometry_error_metrics(points, output, densify=densify)
    # DP bounds source vertices from their replacement segments.  The symmetric
    # Hausdorff check additionally catches unusual hairpins where the new chord
    # strays farther from the source polyline.  Fidelity wins over reduction.
    if not within_page_error_bound(points, output, tolerance_mm):
        output = points
        metrics = geometry_error_metrics(points, output, densify=densify)
    return output, metrics


def _chain_generalized_spans(
    network: TopologyNetwork,
    generalized: tuple[GeneralizedSpan, ...],
) -> tuple[GeneralizedChain, ...]:
    endpoint_incidence: defaultdict[NodeKey, list[tuple[int, int]]] = defaultdict(list)
    for index, item in enumerate(generalized):
        endpoint_incidence[item.span.start_node].append((index, 0))
        endpoint_incidence[item.span.end_node].append((index, 1))

    def outward_vector(endpoint: tuple[int, int]) -> Point2D:
        item = generalized[endpoint[0]]
        points = item.points
        if endpoint[1] == 0:
            start, adjacent = points[0], points[1]
        else:
            start, adjacent = points[-1], points[-2]
        length = hypot(adjacent[0] - start[0], adjacent[1] - start[1])
        if length <= 1e-12:
            return (0.0, 0.0)
        return (
            (adjacent[0] - start[0]) / length,
            (adjacent[1] - start[1]) / length,
        )

    continuation: dict[tuple[int, int], tuple[int, int]] = {}
    for endpoints in endpoint_incidence.values():
        candidates: list[tuple[int, float, tuple[int, int], tuple[int, int]]] = []
        for left_index, first in enumerate(endpoints):
            for second in endpoints[left_index + 1 :]:
                if first[0] == second[0]:
                    continue
                first_span = generalized[first[0]].span
                second_span = generalized[second[0]].span
                compatible = (
                    first_span.semantic_key == second_span.semantic_key
                    and first_span.grade_key == second_span.grade_key
                )
                if not compatible:
                    continue
                first_vector = outward_vector(first)
                second_vector = outward_vector(second)
                alignment = (
                    first_vector[0] * second_vector[0]
                    + first_vector[1] * second_vector[1]
                )
                # Preserve an uninterrupted source way before considering a
                # compatible continuation from another way.  Within each class,
                # pair the straightest radiating edges first.  This changes only
                # pen-lift partitioning: the source node/edge graph is untouched.
                source_priority = int(first_span.line_id != second_span.line_id)
                candidates.append((source_priority, alignment, first, second))

        used: set[tuple[int, int]] = set()
        for _, _, first, second in sorted(candidates):
            if first in used or second in used:
                continue
            continuation[first] = second
            continuation[second] = first
            used.update((first, second))

    line_tags = {source: line.tags for line in network.lines for source in line.sources}
    chains: list[GeneralizedChain] = []
    unvisited = set(range(len(generalized)))
    while unvisited:
        seed = min(unvisited)
        component: set[int] = set()
        pending = [seed]
        while pending:
            span_index = pending.pop()
            if span_index in component:
                continue
            component.add(span_index)
            for side in (0, 1):
                next_item = continuation.get((span_index, side))
                if next_item is not None and next_item[0] not in component:
                    pending.append(next_item[0])

        boundaries = sorted(
            (span_index, side)
            for span_index in component
            for side in (0, 1)
            if (span_index, side) not in continuation
        )
        # A component without a boundary is a cycle.  Starting from either side
        # will return to the same node after visiting every span once.
        current_index, entering_side = boundaries[0] if boundaries else (seed, 0)

        ordered: list[tuple[int, int]] = []
        while current_index in unvisited:
            ordered.append((current_index, entering_side))
            unvisited.remove(current_index)
            exit_side = 1 - entering_side
            next_item = continuation.get((current_index, exit_side))
            if next_item is None or next_item[0] not in unvisited:
                break
            current_index, entering_side = next_item

        points: list[Point2D] = []
        span_ids: list[str] = []
        sources: list[SourceReference] = []
        for span_index, entry_side in ordered:
            item = generalized[span_index]
            oriented = item.points if entry_side == 0 else tuple(reversed(item.points))
            points.extend(oriented if not points else oriented[1:])
            span_ids.append(item.span.span_id)
            sources.extend(item.span.sources)
        unique_sources = tuple(dict.fromkeys(sources))
        first_span_index, first_entry_side = ordered[0]
        last_span_index, last_entry_side = ordered[-1]
        first_span = generalized[first_span_index].span
        last_span = generalized[last_span_index].span
        chain_start_node = (
            first_span.start_node
            if first_entry_side == 0
            else first_span.end_node
        )
        chain_end_node = (
            last_span.end_node
            if last_entry_side == 0
            else last_span.start_node
        )
        seed_span = generalized[ordered[0][0]].span
        chains.append(
            GeneralizedChain(
                chain_id=f"chain:{len(chains)}",
                layer=seed_span.layer,
                points=tuple(points),
                start_node=chain_start_node,
                end_node=chain_end_node,
                span_ids=tuple(span_ids),
                sources=unique_sources,
                source_tags=tuple(
                    (source, line_tags.get(source, ())) for source in unique_sources
                ),
                semantic_key=seed_span.semantic_key,
                grade_key=seed_span.grade_key,
            )
        )
    return tuple(chains)


def validate_generalization(
    network: TopologyNetwork,
    spans: Sequence[GeneralizedSpan],
    *,
    tolerance_mm: LayerTolerance,
    coordinate_epsilon_mm: float = 1e-9,
) -> TopologyValidation:
    """Check anchor coordinates, error bounds, and combinatorial connectivity."""

    issues: list[ValidationIssue] = []
    source_by_id = {span.span_id: span for span in network.spans}
    output_by_id = {item.span.span_id: item for item in spans}
    if set(source_by_id) != set(output_by_id):
        issues.append(
            ValidationIssue(
                "span-set-changed",
                "The generalized output does not contain exactly the source spans.",
            )
        )

    max_anchor_displacement = 0.0
    max_hausdorff = 0.0
    for span_id, source in source_by_id.items():
        output = output_by_id.get(span_id)
        if output is None:
            continue
        start_distance = hypot(
            source.points[0][0] - output.points[0][0],
            source.points[0][1] - output.points[0][1],
        )
        end_distance = hypot(
            source.points[-1][0] - output.points[-1][0],
            source.points[-1][1] - output.points[-1][1],
        )
        max_anchor_displacement = max(
            max_anchor_displacement, start_distance, end_distance
        )
        max_hausdorff = max(max_hausdorff, output.metrics.hausdorff_mm)
        if max(start_distance, end_distance) > coordinate_epsilon_mm:
            issues.append(
                ValidationIssue(
                    "anchor-moved",
                    "A protected span endpoint moved during generalization.",
                    span_id,
                )
            )
        span_tolerance = _layer_tolerance(tolerance_mm, source.layer)
        if not within_page_error_bound(source.points, output.points, span_tolerance):
            issues.append(
                ValidationIssue(
                    "error-bound-exceeded",
                    "A generalized span exceeds the declared page-space tolerance.",
                    span_id,
                )
            )

    source_connectivity = Counter(
        (node, span.span_id)
        for span in network.spans
        for node in (span.start_node, span.end_node)
    )
    output_connectivity = Counter(
        (node, item.span.span_id)
        for item in spans
        for node in (item.span.start_node, item.span.end_node)
    )
    connectivity_preserved = source_connectivity == output_connectivity
    if not connectivity_preserved:
        issues.append(
            ValidationIssue(
                "connectivity-changed",
                "Generalization changed the source span/node incidence graph.",
            )
        )
    return TopologyValidation(
        valid=not issues,
        issues=tuple(issues),
        max_anchor_displacement_mm=max_anchor_displacement,
        max_hausdorff_mm=max_hausdorff,
        connectivity_preserved=connectivity_preserved,
    )


def simplify_road_topology(
    network: TopologyNetwork,
    tolerance_mm: LayerTolerance,
    *,
    hausdorff_densify: float = 0.1,
) -> TopologyGeneralization:
    """Simplify each anchor span once, then join compatible degree-two spans."""

    _validate_layer_tolerances(tolerance_mm, network)
    if not 0 < hausdorff_densify <= 1:
        raise MapPlotterError(
            "Hausdorff densify must be greater than zero and at most one."
        )
    generalized = tuple(
        GeneralizedSpan(
            span,
            *_bounded_simplify(
                span.points,
                _layer_tolerance(tolerance_mm, span.layer),
                densify=hausdorff_densify,
            ),
        )
        for span in network.spans
    )
    validation = validate_generalization(
        network,
        generalized,
        tolerance_mm=tolerance_mm,
    )
    return TopologyGeneralization(
        network=network,
        spans=generalized,
        chains=_chain_generalized_spans(network, generalized),
        validation=validation,
        tolerance_mm=tolerance_mm,
    )


def _line_parts(geometry: BaseGeometry) -> Iterable[LineString]:
    if geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        if len(geometry.coords) >= 2 and geometry.length > 0:
            yield geometry
        return
    if hasattr(geometry, "geoms"):
        for child in geometry.geoms:
            yield from _line_parts(child)


@dataclass(frozen=True)
class SegmentwiseSuppression:
    """Visible centreline pieces after subtracting mapped broad-water areas."""

    visible_parts: tuple[tuple[Point2D, ...], ...]
    source_length_mm: float
    visible_length_mm: float
    suppressed_length_mm: float

    @property
    def fully_suppressed(self) -> bool:
        return not self.visible_parts


def suppress_river_centerline_segments(
    points: Sequence[Point2D],
    mapped_water: BaseGeometry | Iterable[BaseGeometry],
    *,
    mask_buffer_mm: float = 0.0,
) -> SegmentwiseSuppression:
    """Subtract broad-water coverage while retaining every uncovered segment.

    Unlike a whole-line overlap ratio, this preserves the upstream/downstream
    portions of a river centreline when only its middle runs through a mapped
    river polygon.  No minimum-length gate is applied.
    """

    if len(points) < 2:
        raise MapPlotterError(
            "River suppression requires a centreline with two points."
        )
    if not isfinite(mask_buffer_mm) or mask_buffer_mm < 0:
        raise MapPlotterError("River mask buffer cannot be negative.")
    source = LineString(points)
    mask: BaseGeometry
    if isinstance(mapped_water, BaseGeometry):
        mask = mapped_water
    else:
        geometries = [
            geometry
            for geometry in cast(Iterable[BaseGeometry], mapped_water)
            if not geometry.is_empty
        ]
        mask = unary_union(geometries) if geometries else GeometryCollection()
    if not mask.is_valid:
        mask = mask.buffer(0)
    if mask_buffer_mm > 0 and not mask.is_empty:
        mask = mask.buffer(mask_buffer_mm)
    visible = source if mask.is_empty else source.difference(mask)

    ordered_parts: list[tuple[float, tuple[Point2D, ...]]] = []
    for part in _line_parts(visible):
        part_points = tuple((float(x), float(y)) for x, y in part.coords)
        start_position = (
            source.project(part.boundary.geoms[0]) if not part.is_ring else 0.0
        )
        end_position = (
            source.project(part.boundary.geoms[-1])
            if not part.is_ring
            else start_position
        )
        if end_position < start_position:
            part_points = tuple(reversed(part_points))
            start_position, end_position = end_position, start_position
        ordered_parts.append((start_position, part_points))
    ordered_parts.sort(key=lambda item: item[0])
    visible_parts = tuple(part for _, part in ordered_parts)
    visible_length = sum(LineString(part).length for part in visible_parts)
    return SegmentwiseSuppression(
        visible_parts=visible_parts,
        source_length_mm=source.length,
        visible_length_mm=visible_length,
        suppressed_length_mm=max(0.0, source.length - visible_length),
    )


def suppress_topology_river_line(
    line: TopologyLine,
    mapped_water: BaseGeometry | Iterable[BaseGeometry],
    *,
    mask_buffer_mm: float = 0.0,
) -> tuple[TopologyLine, ...]:
    """Lineage-preserving adapter for segmentwise river suppression."""

    result = suppress_river_centerline_segments(
        line.points,
        mapped_water,
        mask_buffer_mm=mask_buffer_mm,
    )
    return tuple(
        TopologyLine(
            line_id=f"{line.line_id}:visible:{index}",
            layer=line.layer,
            points=part,
            sources=line.sources,
            tags=line.tags,
            # Difference-created boundary vertices have no OSM node identity.
            node_ids=(),
        )
        for index, part in enumerate(result.visible_parts)
    )
