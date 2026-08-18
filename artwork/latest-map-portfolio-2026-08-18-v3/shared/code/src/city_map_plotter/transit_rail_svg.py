"""Plotter-faithful national physical-railway plates.

This renderer deliberately consumes :class:`ZoomstackPhysicalRail`, not a
passenger-service contract.  The four visual classes are the physical classes
published by OS Open Zoomstack.  They must never be interpreted as operator,
calling-pattern, frequency, or routability evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import copy
from dataclasses import dataclass
import hashlib
import heapq
import json
from math import cos, floor, hypot, isfinite, log10, radians
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET

from shapely.errors import GEOSException
from shapely.geometry import LineString, box
from shapely.strtree import STRtree

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PlateArtwork,
    PlateContext,
    Rect,
    add_text,
    render_plate,
)
from .pens import ACTUAL_PEN_INVENTORY
from .stroke_font import text_width_mm
from .svgkit import INKSCAPE_NS, SODIPODI_NS, svg_tag
from .transit_svg import CONTEXT_STYLE
from .transit_zoomstack import (
    DEFAULT_GB_BOUNDS,
    ZOOMSTACK_RAIL_TYPES,
    NationalContextPlace,
    PhysicalRailFeature,
    ZoomstackNationalContext,
    ZoomstackPhysicalRail,
    national_context_geometry_sha256,
)


Point = tuple[float, float]
Stroke = tuple[Point, ...]

FORMAT_ID = "a3-landscape"
ARTIFACT_ID = "great-britain-physical-railways"
HOUSE_STOCK_HEX = "#FCFBF7"
SOURCE_URL = "https://osdatahub.os.uk/downloads/open/OpenZoomstack"
SOURCE_LICENCE = "Open Government Licence v3.0"
SOURCE_ATTRIBUTION = (
    "Contains OS data © Crown copyright and database right 2026"
)
PROJECTION_EARTH_RADIUS_M = 6_371_008.8
DEFAULT_SIMPLIFICATION_TOLERANCE_MM = 0.04
FIELD_PADDING_MM = 6.0
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class PhysicalRailStyle:
    """One source class resolved to one owned, single-pass studio pen."""

    rail_type: str
    layer_id: str
    label: str
    pen_id: str
    preview_hex: str
    role: str

    @property
    def pen(self) -> Any:
        matches = [
            pen for pen in ACTUAL_PEN_INVENTORY.pens if pen.identity == self.pen_id
        ]
        if len(matches) != 1:
            raise MapPlotterError(
                f"Physical rail style asks for unknown pen {self.pen_id!r}."
            )
        return matches[0]

    def as_dict(self) -> dict[str, Any]:
        pen = self.pen
        return {
            "source_class": self.rail_type,
            "logical_layer": self.layer_id,
            "label": self.label,
            "pen_id": pen.identity,
            "ink": pen.ink,
            "nominal_nib_mm": pen.nominal_nib_mm,
            "physical_nib_mm": pen.mark_width_mm,
            "strokes": 1,
            "passes": 1,
            "plotted_width_mm": pen.mark_width_mm,
            "preview_hex": self.preview_hex,
            "preview_is_physical_ink_claim": False,
            "operator_or_service_semantics": False,
        }


@dataclass(frozen=True, slots=True)
class NationalContextStyle:
    context_class: str
    layer_id: str
    label: str
    pen_id: str
    preview_hex: str
    role: str

    @property
    def pen(self) -> Any:
        matches = [
            pen for pen in ACTUAL_PEN_INVENTORY.pens if pen.identity == self.pen_id
        ]
        if len(matches) != 1:
            raise MapPlotterError(
                f"National context style asks for unknown pen {self.pen_id!r}."
            )
        return matches[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_class": self.context_class,
            "logical_layer": self.layer_id,
            "label": self.label,
            "pen_id": self.pen.identity,
            "ink": self.pen.ink,
            "physical_nib_mm": self.pen.mark_width_mm,
            "strokes": 1,
            "passes": 1,
            "preview_hex": self.preview_hex,
            "preview_is_physical_ink_claim": False,
            "operator_or_service_semantics": False,
        }


# The national proof must read from normal viewing distance. Multi-track uses
# the owned 1.0 mm black pen and single-track the owned 0.6 mm black pen; both
# are materially heavier than every coloured context layer below them.
PHYSICAL_RAIL_STYLES: dict[str, PhysicalRailStyle] = {
    "Multi Track": PhysicalRailStyle(
        rail_type="Multi Track",
        layer_id="rail-multi-track",
        label="MULTI TRACK",
        pen_id="black-1",
        preview_hex="#24282B",
        role="physical-rail-multi-track",
    ),
    "Single Track": PhysicalRailStyle(
        rail_type="Single Track",
        layer_id="rail-single-track",
        label="SINGLE TRACK",
        pen_id="black-0-6",
        preview_hex="#24282B",
        role="physical-rail-single-track",
    ),
    "Narrow Gauge": PhysicalRailStyle(
        rail_type="Narrow Gauge",
        layer_id="rail-narrow-gauge",
        label="NARROW GAUGE",
        pen_id="green-0-4",
        preview_hex=CONTEXT_STYLE["green-space"][1],
        role="physical-rail-narrow-gauge",
    ),
    "Tunnel": PhysicalRailStyle(
        rail_type="Tunnel",
        layer_id="rail-tunnel",
        label="TUNNEL",
        pen_id="red-0-4",
        preview_hex=CONTEXT_STYLE["roads-major"][1],
        role="physical-rail-tunnel",
    ),
}


NATIONAL_CONTEXT_STYLES: dict[str, NationalContextStyle] = {
    "national-boundary": NationalContextStyle(
        context_class="national-boundary",
        layer_id="context-national-boundaries",
        label="Quiet national boundaries",
        pen_id="grey-0-25",
        preview_hex=CONTEXT_STYLE["boundaries"][1],
        role="national-context-boundary",
    ),
    "road-primary": NationalContextStyle(
        context_class="road-primary",
        layer_id="context-primary-roads",
        label="Quiet primary-road context",
        pen_id="red-0-25",
        preview_hex=CONTEXT_STYLE["roads-secondary"][1],
        role="national-context-primary-road",
    ),
    "road-motorway": NationalContextStyle(
        context_class="road-motorway",
        layer_id="context-motorways",
        label="Quiet motorway context",
        pen_id="red-0-4",
        preview_hex=CONTEXT_STYLE["roads-major"][1],
        role="national-context-motorway",
    ),
    "surface-water-bank": NationalContextStyle(
        context_class="surface-water-bank",
        layer_id="context-surface-water-banks",
        label="Surface-water banks",
        pen_id="blue-0-25",
        preview_hex="#8BC2DC",
        role="national-context-surface-water-bank",
    ),
    "coastline": NationalContextStyle(
        context_class="coastline",
        layer_id="context-coastline",
        label="Coastline",
        pen_id="blue-0-4",
        preview_hex=CONTEXT_STYLE["coastline"][1],
        role="national-context-coastline",
    ),
}

NATIONAL_CITY_LABEL_PRIORITY = (
    "London",
    "Edinburgh",
    "Cardiff",
    "Glasgow",
    "Birmingham",
    "Manchester",
    "Liverpool",
    "Leeds",
    "Sheffield",
    "Newcastle upon Tyne",
    "Bristol",
    "Southampton",
    "Plymouth",
    "Exeter",
    "Oxford",
    "Cambridge",
    "Norwich",
    "Nottingham",
    "Leicester",
    "York",
    "Kingston upon Hull",
    "Aberdeen",
    "Inverness",
    "Dundee",
    "Swansea",
    "Carlisle",
)
MAX_NATIONAL_CITY_LABELS = 10


@dataclass(frozen=True, slots=True)
class _RailProjector:
    lon0: float
    lat0: float
    minimum_x_m: float
    minimum_y_m: float
    width_m: float
    height_m: float
    scale_mm_per_m: float
    offset_x_mm: float
    offset_y_mm: float
    rect: Rect

    def metric(self, lon: float, lat: float) -> Point:
        return (
            PROJECTION_EARTH_RADIUS_M
            * radians(lon - self.lon0)
            * cos(radians(self.lat0)),
            PROJECTION_EARTH_RADIUS_M * radians(lat - self.lat0),
        )

    def point(self, lon: float, lat: float) -> Point:
        x_m, y_m = self.metric(lon, lat)
        return (
            self.offset_x_mm + (x_m - self.minimum_x_m) * self.scale_mm_per_m,
            self.offset_y_mm
            + self.height_m * self.scale_mm_per_m
            - (y_m - self.minimum_y_m) * self.scale_mm_per_m,
        )

    @property
    def scale_denominator(self) -> float:
        return 1000.0 / self.scale_mm_per_m


@dataclass(frozen=True, slots=True)
class _ProjectedFeature:
    feature_id: str
    rail_type: str
    points: Stroke
    start_key: Point
    end_key: Point
    source_objects: tuple[str, ...]
    source_vertex_count: int


@dataclass(frozen=True, slots=True)
class _RailTrail:
    rail_type: str
    points: Stroke
    feature_ids: tuple[str, ...]
    source_objects: tuple[str, ...]
    source_vertex_count: int


def _feature_identity(index: int) -> str:
    return f"zoomstack-rail-{index:07d}"


def _source_id(rail: ZoomstackPhysicalRail) -> str:
    return f"os-open-zoomstack-{rail.source_sha256[:12]}"


def _canonical_geometry_sha256(features: Sequence[PhysicalRailFeature]) -> str:
    # Match the loader's canonical compact JSON without materialising a second
    # national feature document alongside the already resident source model.
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, feature in enumerate(features):
        if index:
            digest.update(b",")
        digest.update(
            json.dumps(
                feature.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    digest.update(b"]")
    return digest.hexdigest()


def _validate_source(rail: ZoomstackPhysicalRail) -> None:
    if _SHA256.fullmatch(rail.source_sha256) is None:
        raise MapPlotterError("Physical railway source SHA-256 is malformed.")
    if not rail.features:
        raise MapPlotterError("A physical railway plate needs source rail features.")
    if any(
        abs(actual - expected) > 1e-9
        for actual, expected in zip(
            rail.bounds_wgs84,
            DEFAULT_GB_BOUNDS,
            strict=True,
        )
    ):
        raise MapPlotterError(
            "The national Great Britain railway plate requires the pinned "
            "default GB source bounds; render a clipped selection under a "
            "different, non-national artifact identity."
        )
    if rail.audit.get("source_product") != "OS Open Zoomstack":
        raise MapPlotterError(
            "Physical railway plates require an OS Open Zoomstack source audit."
        )
    if rail.audit.get("source_sha256") != rail.source_sha256:
        raise MapPlotterError("Physical railway source hash and audit disagree.")
    if rail.audit.get("zoom") != rail.zoom:
        raise MapPlotterError("Physical railway zoom and source audit disagree.")
    if rail.audit.get("bounds_wgs84") != list(rail.bounds_wgs84):
        raise MapPlotterError("Physical railway bounds and source audit disagree.")
    if rail.audit.get("feature_ids_persistent") is not False:
        raise MapPlotterError(
            "Zoomstack feature identities must remain explicitly non-persistent."
        )
    if rail.audit.get("connected_routing_graph_claimed") is not False:
        raise MapPlotterError("Zoomstack physical rail cannot claim a routing graph.")
    if rail.audit.get("operator_service_geometry_claimed") is not False:
        raise MapPlotterError(
            "Zoomstack physical rail cannot claim operator/service geometry."
        )
    if rail.audit.get("invented_connector_count") != 0:
        raise MapPlotterError(
            "A physical railway source audit cannot contain invented connectors."
        )
    if rail.audit.get("permitted_use") != "physical/cartographic rail context only":
        raise MapPlotterError(
            "Zoomstack source use must remain physical/cartographic rail context only."
        )
    audited_count = rail.audit.get("emitted_physical_feature_count")
    if audited_count != len(rail.features):
        raise MapPlotterError(
            "Physical railway feature count and Zoomstack audit disagree."
        )
    audited_geometry = rail.audit.get("geometry_sha256")
    actual_geometry = _canonical_geometry_sha256(rail.features)
    if audited_geometry != actual_geometry:
        raise MapPlotterError(
            "Physical railway geometry and Zoomstack audit digest disagree."
        )

    type_counts: Counter[str] = Counter()
    for index, feature in enumerate(rail.features, start=1):
        if feature.rail_type not in ZOOMSTACK_RAIL_TYPES:
            raise MapPlotterError(
                f"Physical railway feature {index} has unknown class "
                f"{feature.rail_type!r}."
            )
        if len(feature.geometry) < 2:
            raise MapPlotterError(
                f"Physical railway feature {index} has fewer than two vertices."
            )
        if not feature.source_objects:
            raise MapPlotterError(
                f"Physical railway feature {index} has no source-object lineage."
            )
        for lon, lat in feature.geometry:
            if not (
                isfinite(lon)
                and isfinite(lat)
                and -180.0 <= lon <= 180.0
                and -85.0 <= lat <= 85.0
            ):
                raise MapPlotterError(
                    f"Physical railway feature {index} has invalid WGS84 geometry."
                )
        type_counts[feature.rail_type] += 1
    if rail.audit.get("rail_type_counts") != dict(sorted(type_counts.items())):
        raise MapPlotterError(
            "Physical railway class counts and Zoomstack audit disagree."
        )
    if rail.national_context is not None:
        _validate_national_context(rail, rail.national_context)


def _validate_national_context(
    rail: ZoomstackPhysicalRail,
    context: ZoomstackNationalContext,
) -> None:
    if context.source_sha256 != rail.source_sha256:
        raise MapPlotterError(
            "National context and physical rail must come from identical MBTiles bytes."
        )
    if context.bounds_wgs84 != rail.bounds_wgs84:
        raise MapPlotterError(
            "National context and physical rail must use identical WGS84 bounds."
        )
    audit = context.audit
    expected = {
        "source_product": "OS Open Zoomstack",
        "source_sha256": rail.source_sha256,
        "zoom": context.zoom,
        "bounds_wgs84": list(context.bounds_wgs84),
        "emitted_context_line_count": len(context.lines),
        "candidate_place_count": len(context.places),
        "invented_connector_count": 0,
        "connected_routing_graph_claimed": False,
        "operator_service_geometry_claimed": False,
        "feature_ids_persistent": False,
        "permitted_use": "quiet national cartographic context only",
    }
    for key, value in expected.items():
        if audit.get(key) != value:
            raise MapPlotterError(
                f"National context audit field {key!r} is missing or inconsistent."
            )
    actual_digest = national_context_geometry_sha256(
        context.lines,
        context.places,
    )
    if audit.get("geometry_sha256") != actual_digest:
        raise MapPlotterError(
            "National context geometry and Zoomstack audit digest disagree."
        )
    line_counts: Counter[str] = Counter()
    for index, line in enumerate(context.lines, start=1):
        if line.context_class not in NATIONAL_CONTEXT_STYLES:
            raise MapPlotterError(
                f"National context line {index} has unknown class "
                f"{line.context_class!r}."
            )
        if line.source_layer not in {
            "sea",
            "surfacewater",
            "roads",
            "boundaries",
        }:
            raise MapPlotterError(
                f"National context line {index} has unknown source layer."
            )
        if len(line.geometry) < 2:
            raise MapPlotterError(
                f"National context line {index} has fewer than two vertices."
            )
        if any(
            not (
                isfinite(lon)
                and isfinite(lat)
                and -180.0 <= lon <= 180.0
                and -85.0 <= lat <= 85.0
            )
            for lon, lat in line.geometry
        ):
            raise MapPlotterError(
                f"National context line {index} has invalid WGS84 geometry."
            )
        line_counts[line.context_class] += 1
    if audit.get("emitted_context_line_counts") != dict(sorted(line_counts.items())):
        raise MapPlotterError(
            "National context class counts and Zoomstack audit disagree."
        )
    place_counts: Counter[str] = Counter()
    for index, place in enumerate(context.places, start=1):
        if place.place_type not in {"Capital", "City"} or not place.name.strip():
            raise MapPlotterError(
                f"National context place {index} is not a named city/capital."
            )
        lon, lat = place.point
        if not (
            isfinite(lon)
            and isfinite(lat)
            and -180.0 <= lon <= 180.0
            and -85.0 <= lat <= 85.0
        ):
            raise MapPlotterError(
                f"National context place {index} has invalid WGS84 geometry."
            )
        place_counts[place.place_type] += 1
    if audit.get("candidate_place_counts") != dict(sorted(place_counts.items())):
        raise MapPlotterError(
            "National context place counts and Zoomstack audit disagree."
        )


def _projector_for(rail: ZoomstackPhysicalRail, rect: Rect) -> _RailProjector:
    vertex_count = sum(len(feature.geometry) for feature in rail.features)
    lon0 = sum(
        lon for feature in rail.features for lon, _lat in feature.geometry
    ) / vertex_count
    lat0 = sum(
        lat for feature in rail.features for _lon, lat in feature.geometry
    ) / vertex_count
    longitude_scale = PROJECTION_EARTH_RADIUS_M * cos(radians(lat0))
    latitude_scale = PROJECTION_EARTH_RADIUS_M
    minimum_x = minimum_y = float("inf")
    maximum_x = maximum_y = float("-inf")
    for feature in rail.features:
        for lon, lat in feature.geometry:
            x_m = longitude_scale * radians(lon - lon0)
            y_m = latitude_scale * radians(lat - lat0)
            minimum_x = min(minimum_x, x_m)
            maximum_x = max(maximum_x, x_m)
            minimum_y = min(minimum_y, y_m)
            maximum_y = max(maximum_y, y_m)
    width_m = maximum_x - minimum_x
    height_m = maximum_y - minimum_y
    if width_m <= 0.0 or height_m <= 0.0:
        raise MapPlotterError("Physical railway extent is degenerate.")
    usable = rect.inset(FIELD_PADDING_MM)
    scale = min(usable.width / width_m, usable.height / height_m)
    used_width = width_m * scale
    used_height = height_m * scale
    return _RailProjector(
        lon0=lon0,
        lat0=lat0,
        minimum_x_m=minimum_x,
        minimum_y_m=minimum_y,
        width_m=width_m,
        height_m=height_m,
        scale_mm_per_m=scale,
        offset_x_mm=usable.left + (usable.width - used_width) / 2.0,
        offset_y_mm=usable.top + (usable.height - used_height) / 2.0,
        rect=usable,
    )


def _deduplicate_consecutive(points: Iterable[Point]) -> Stroke:
    result: list[Point] = []
    for point in points:
        if not result or hypot(point[0] - result[-1][0], point[1] - result[-1][1]) > 1e-12:
            result.append(point)
    return tuple(result)


def _geometry_line_parts(value: Any) -> Iterable[LineString]:
    if isinstance(value, LineString):
        yield value
    elif hasattr(value, "geoms"):
        for child in value.geoms:
            yield from _geometry_line_parts(child)


def _simplify_feature(points: Stroke, tolerance_mm: float) -> Stroke:
    if tolerance_mm <= 0.0:
        return points
    simplified = LineString(points).simplify(
        tolerance_mm,
        preserve_topology=False,
    )
    result = _deduplicate_consecutive(
        (float(x), float(y)) for x, y in simplified.coords
    )
    if len(result) < 2:
        return points
    # GEOS preserves open-line endpoints.  Keep this a fail-closed invariant so
    # topology-safe source joins cannot silently become near-coordinate joins.
    if (
        hypot(result[0][0] - points[0][0], result[0][1] - points[0][1]) > 1e-8
        or hypot(result[-1][0] - points[-1][0], result[-1][1] - points[-1][1])
        > 1e-8
    ):
        raise MapPlotterError("Rail simplification changed a source endpoint.")
    return result


def _project_features(
    rail: ZoomstackPhysicalRail,
    projector: _RailProjector,
    *,
    simplify_mm: float,
) -> tuple[list[_ProjectedFeature], tuple[str, ...]]:
    projected: list[_ProjectedFeature] = []
    degenerate: list[str] = []
    for index, feature in enumerate(rail.features, start=1):
        feature_id = _feature_identity(index)
        points = _deduplicate_consecutive(
            projector.point(lon, lat) for lon, lat in feature.geometry
        )
        if len(points) < 2 or LineString(points).length <= 1e-12:
            degenerate.append(feature_id)
            continue
        points = _simplify_feature(points, simplify_mm)
        projected.append(
            _ProjectedFeature(
                feature_id=feature_id,
                rail_type=feature.rail_type,
                points=points,
                start_key=feature.geometry[0],
                end_key=feature.geometry[-1],
                source_objects=feature.source_objects,
                source_vertex_count=len(feature.geometry),
            )
        )
    return projected, tuple(degenerate)


def _assemble_type_trails(edges: Sequence[_ProjectedFeature]) -> list[_RailTrail]:
    """Join only exact, same-class source endpoints for pen travel.

    This operation changes neither visible centreline coverage nor network
    semantics.  In particular, it is not graph construction: coincident
    interior vertices and endpoints from different physical classes are never
    joined.
    """

    if not edges:
        return []
    by_id = {edge.feature_id: edge for edge in edges}
    adjacency: dict[Point, list[str]] = defaultdict(list)
    degree: Counter[Point] = Counter()
    for edge in edges:
        heapq.heappush(adjacency[edge.start_key], edge.feature_id)
        if edge.end_key != edge.start_key:
            heapq.heappush(adjacency[edge.end_key], edge.feature_id)
        degree[edge.start_key] += 1
        degree[edge.end_key] += 1
    unused = set(by_id)
    unused_heap = list(unused)
    heapq.heapify(unused_heap)
    odd_heap = [point for point, count in degree.items() if count % 2]
    heapq.heapify(odd_heap)
    trails: list[_RailTrail] = []

    def next_incident(point: Point) -> str | None:
        candidates = adjacency.get(point)
        if candidates is None:
            return None
        while candidates and candidates[0] not in unused:
            heapq.heappop(candidates)
        return candidates[0] if candidates else None

    def next_start() -> Point:
        while odd_heap:
            candidate = heapq.heappop(odd_heap)
            if degree[candidate] > 0 and degree[candidate] % 2:
                return candidate
        while unused_heap and unused_heap[0] not in unused:
            heapq.heappop(unused_heap)
        if not unused_heap:  # pragma: no cover - guarded by the outer loop.
            raise MapPlotterError("Physical rail trail assembly lost an edge.")
        edge = by_id[unused_heap[0]]
        return min(edge.start_key, edge.end_key)

    def consume_endpoint(point: Point) -> None:
        degree[point] -= 1
        if degree[point] < 0:  # pragma: no cover - defensive invariant.
            raise MapPlotterError("Physical rail endpoint degree became negative.")
        if degree[point] % 2:
            heapq.heappush(odd_heap, point)

    while unused:
        current = next_start()

        points: list[Point] = []
        feature_ids: list[str] = []
        source_objects: set[str] = set()
        source_vertex_count = 0
        rail_type = by_id[min(unused)].rail_type
        while True:
            feature_id = next_incident(current)
            if feature_id is None:
                break
            edge = by_id[feature_id]
            if edge.rail_type != rail_type:
                raise MapPlotterError("Physical rail trail crossed a source class.")
            if edge.start_key == current:
                edge_points = edge.points
                current = edge.end_key
            elif edge.end_key == current:
                edge_points = tuple(reversed(edge.points))
                current = edge.start_key
            else:  # pragma: no cover - adjacency construction makes this unreachable.
                raise MapPlotterError("Physical rail adjacency is inconsistent.")
            if points:
                distance = hypot(
                    points[-1][0] - edge_points[0][0],
                    points[-1][1] - edge_points[0][1],
                )
                if distance > 1e-8:
                    raise MapPlotterError(
                        "Physical rail assembly would require an invented connector."
                    )
                points.extend(edge_points[1:])
            else:
                points.extend(edge_points)
            unused.remove(feature_id)
            consume_endpoint(edge.start_key)
            consume_endpoint(edge.end_key)
            feature_ids.append(feature_id)
            source_objects.update(edge.source_objects)
            source_vertex_count += edge.source_vertex_count
        if not feature_ids:
            raise MapPlotterError("Physical rail trail assembly made no progress.")
        trails.append(
            _RailTrail(
                rail_type=rail_type,
                points=tuple(points),
                feature_ids=tuple(feature_ids),
                source_objects=tuple(sorted(source_objects)),
                source_vertex_count=source_vertex_count,
            )
        )
    return trails


def _assemble_trails(edges: Sequence[_ProjectedFeature]) -> list[_RailTrail]:
    trails: list[_RailTrail] = []
    for rail_type in sorted(ZOOMSTACK_RAIL_TYPES):
        trails.extend(
            _assemble_type_trails(
                sorted(
                    (edge for edge in edges if edge.rail_type == rail_type),
                    key=lambda item: item.feature_id,
                )
            )
        )
    return trails


def _nice_scale_distance(projector: _RailProjector, maximum_mm: float) -> float:
    maximum_m = maximum_mm / projector.scale_mm_per_m
    exponent = floor(log10(maximum_m))
    base = 10.0**exponent
    candidates = [value * base for value in (1.0, 2.0, 5.0, 10.0)]
    return max(value for value in candidates if value <= maximum_m + 1e-9)


def _rectangles_overlap(first: Rect, second: Rect, *, gap_mm: float) -> bool:
    return not (
        first.right + gap_mm <= second.left
        or second.right + gap_mm <= first.left
        or first.bottom + gap_mm <= second.top
        or second.bottom + gap_mm <= first.top
    )


def _label_positions(point: Point, width_mm: float, cap_mm: float) -> tuple[Rect, ...]:
    x, y = point
    return tuple(
        candidate
        for offset in (1.8, 3.6)
        for candidate in (
            Rect(x + offset, y - cap_mm / 2.0, width_mm, cap_mm),
            Rect(x - offset - width_mm, y - cap_mm / 2.0, width_mm, cap_mm),
            Rect(x - width_mm / 2.0, y - offset - cap_mm, width_mm, cap_mm),
            Rect(x - width_mm / 2.0, y + offset, width_mm, cap_mm),
        )
    )


def _inside_rect(inner: Rect, outer: Rect) -> bool:
    return (
        outer.left <= inner.left
        and inner.right <= outer.right
        and outer.top <= inner.top
        and inner.bottom <= outer.bottom
    )


def _leader_label_positions(
    point: Point,
    width_mm: float,
    cap_mm: float,
    field: Rect,
    rail_bounds: Rect,
) -> tuple[tuple[Rect, str], ...]:
    distance_left = abs(point[0] - rail_bounds.left)
    distance_right = abs(rail_bounds.right - point[0])
    side_order = (
        ("left", "right")
        if distance_left <= distance_right
        else ("right", "left")
    )
    offsets = (0.0, -4.0, 4.0, -8.0, 8.0, -12.0, 12.0, -16.0, 16.0)
    result: list[tuple[Rect, str]] = []
    for offset in offsets:
        top = point[1] - cap_mm / 2.0 + offset
        for side in side_order:
            left = (
                max(field.left + 4.0, rail_bounds.left - 5.0 - width_mm)
                if side == "left"
                else min(field.right - 4.0 - width_mm, rail_bounds.right + 5.0)
            )
            result.append((Rect(left, top, width_mm, cap_mm), side))
    return tuple(result)


def _add_national_context(
    national: ZoomstackNationalContext | None,
    *,
    layers: dict[str, ArtworkLayer],
    projector: _RailProjector,
    source_id: str,
    rail_trails: Sequence[_RailTrail],
) -> dict[str, Any]:
    if national is None:
        return {
            "included": False,
            "reason": "source object contains no national context extraction",
            "input_line_count": 0,
            "represented_line_count": 0,
            "clipped_away_line_count": 0,
            "map_path_count": 0,
            "map_pen_down_distance_mm": 0.0,
            "physical_floor_filtered_path_count": 0,
            "physical_floor_filtered_length_mm": 0.0,
            "candidate_place_count": 0,
            "selected_place_count": 0,
            "selected_place_names": [],
        }

    clip = box(
        projector.rect.left,
        projector.rect.top,
        projector.rect.right,
        projector.rect.bottom,
    )
    represented_lines = 0
    path_count = 0
    pen_down_mm = 0.0
    floor_filtered_path_count = 0
    floor_filtered_length_mm = 0.0
    class_results: dict[str, dict[str, Any]] = {
        context_class: {
            "input_line_count": 0,
            "represented_line_count": 0,
            "map_path_count": 0,
            "map_pen_down_distance_mm": 0.0,
            "physical_floor_filtered_path_count": 0,
            "physical_floor_filtered_length_mm": 0.0,
        }
        for context_class in NATIONAL_CONTEXT_STYLES
    }
    for line in national.lines:
        style = NATIONAL_CONTEXT_STYLES[line.context_class]
        result = class_results[line.context_class]
        result["input_line_count"] += 1
        projected = _deduplicate_consecutive(
            projector.point(lon, lat) for lon, lat in line.geometry
        )
        if len(projected) < 2:
            continue
        try:
            pieces = tuple(
                _geometry_line_parts(LineString(projected).intersection(clip))
            )
        except (GEOSException, ValueError) as exc:
            raise MapPlotterError(
                f"Cannot clip national context {line.context_class}: {exc}"
            ) from exc
        emitted_for_line = False
        for piece in pieces:
            points = _deduplicate_consecutive(
                (float(x), float(y)) for x, y in piece.coords
            )
            if len(points) < 2:
                continue
            length = LineString(points).length
            if length <= 1e-12:
                continue
            minimum_length = 3.0 * style.pen.mark_width_mm
            if length + 1e-9 < minimum_length:
                # Unlike the detail-first physical rail layer, this is quiet
                # reference context. A sub-three-nib mark cannot reproduce as
                # a stable bank/road/boundary and is explicitly ledgered rather
                # than emitted as preview-only noise.
                floor_filtered_path_count += 1
                floor_filtered_length_mm += length
                result["physical_floor_filtered_path_count"] += 1
                result["physical_floor_filtered_length_mm"] += length
                continue
            layers[style.layer_id].add(
                points,
                source_ref=source_id,
                role=style.role,
                attributes={
                    "data-context-class": line.context_class,
                    "data-source-layer": line.source_layer,
                    "data-context-is-background": "true",
                    "data-operator-service-claim": "false",
                    "data-routing-graph-claim": "false",
                    "data-invented-connector": "false",
                },
            )
            emitted_for_line = True
            path_count += 1
            pen_down_mm += length
            result["map_path_count"] += 1
            result["map_pen_down_distance_mm"] += length
        if emitted_for_line:
            represented_lines += 1
            result["represented_line_count"] += 1

    label_layer = layers["context-city-labels"]
    leader_layer = layers["context-city-leaders"]
    places_by_name: dict[str, NationalContextPlace] = {}
    for place in national.places:
        previous = places_by_name.get(place.name)
        if previous is None or place.point < previous.point:
            places_by_name[place.name] = place
    accepted_boxes: list[Rect] = []
    selected_names: list[str] = []
    leader_names: list[str] = []
    omitted_no_clear_placement: list[str] = []
    omitted_by_label_cap: list[str] = []
    outside_projected_field: list[str] = []
    rail_conflicted_candidate_count = 0
    rail_lines = [LineString(trail.points) for trail in rail_trails]
    rail_index = STRtree(rail_lines)
    minimum_rail_x = min(line.bounds[0] for line in rail_lines)
    minimum_rail_y = min(line.bounds[1] for line in rail_lines)
    maximum_rail_x = max(line.bounds[2] for line in rail_lines)
    maximum_rail_y = max(line.bounds[3] for line in rail_lines)
    rail_bounds = Rect(
        minimum_rail_x,
        minimum_rail_y,
        maximum_rail_x - minimum_rail_x,
        maximum_rail_y - minimum_rail_y,
    )
    rail_label_clearance_mm = (
        PHYSICAL_RAIL_STYLES["Multi Track"].pen.mark_width_mm / 2.0
        + layers["context-city-labels"].pen.mark_width_mm / 2.0
        + 0.6
    )

    def candidate_is_clear(candidate: Rect) -> bool:
        nonlocal rail_conflicted_candidate_count
        if not _inside_rect(candidate, projector.rect):
            return False
        if any(
            _rectangles_overlap(candidate, accepted, gap_mm=1.0)
            for accepted in accepted_boxes
        ):
            return False
        candidate_geometry = box(
            candidate.left,
            candidate.top,
            candidate.right,
            candidate.bottom,
        )
        nearby_indices = rail_index.query(
            candidate_geometry.buffer(rail_label_clearance_mm)
        )
        if any(
            candidate_geometry.distance(rail_lines[int(index)]) + 1e-9
            < rail_label_clearance_mm
            for index in nearby_indices
        ):
            rail_conflicted_candidate_count += 1
            return False
        return True

    cap_mm = 2.0
    for name in NATIONAL_CITY_LABEL_PRIORITY:
        place = places_by_name.get(name)
        if place is None:
            continue
        point = projector.point(*place.point)
        if not (
            projector.rect.left <= point[0] <= projector.rect.right
            and projector.rect.top <= point[1] <= projector.rect.bottom
        ):
            outside_projected_field.append(name)
            continue
        if len(selected_names) >= MAX_NATIONAL_CITY_LABELS:
            omitted_by_label_cap.append(name)
            continue
        display_name = name.upper()
        width_mm = text_width_mm(display_name, cap_height_mm=cap_mm)
        chosen: Rect | None = None
        chosen_leader_side: str | None = None
        for candidate in _label_positions(point, width_mm, cap_mm):
            if candidate_is_clear(candidate):
                chosen = candidate
                break
        if chosen is None:
            for candidate, side in _leader_label_positions(
                point,
                width_mm,
                cap_mm,
                projector.rect,
                rail_bounds,
            ):
                if candidate_is_clear(candidate):
                    chosen = candidate
                    chosen_leader_side = side
                    break
        if chosen is None:
            omitted_no_clear_placement.append(name)
            continue
        marker_half = 0.55
        marker_attributes = {
            "data-os-name": name,
            "data-place-type": place.place_type,
            "data-source-layer": place.source_layer,
            "data-context-is-background": "true",
            "data-operator-service-claim": "false",
        }
        label_layer.add(
            [(point[0] - marker_half, point[1]), (point[0] + marker_half, point[1])],
            source_ref=source_id,
            role="national-context-city-marker",
            attributes=marker_attributes,
        )
        if chosen_leader_side is not None:
            endpoint_x = (
                chosen.right + 0.6
                if chosen_leader_side == "left"
                else chosen.left - 0.6
            )
            leader_layer.add(
                [point, (endpoint_x, chosen.centre[1])],
                source_ref=source_id,
                role="national-context-city-label-leader",
                attributes={
                    **marker_attributes,
                    "data-label-placement": f"{chosen_leader_side}-leader-lane",
                    "data-crossing-rail-permitted-annotation": "true",
                    "data-invented-rail-connector": "false",
                },
            )
            leader_names.append(name)
        label_layer.add(
            [(point[0], point[1] - marker_half), (point[0], point[1] + marker_half)],
            source_ref=source_id,
            role="national-context-city-marker",
            attributes=marker_attributes,
        )
        add_text(
            label_layer,
            display_name,
            x_mm=chosen.left,
            y_mm=chosen.top,
            preferred_cap_mm=cap_mm,
            maximum_width_mm=chosen.width,
            minimum_cap_mm=cap_mm,
            source_ref=source_id,
            role="national-context-city-label",
            attributes=marker_attributes,
        )
        accepted_boxes.append(chosen)
        selected_names.append(name)

    return {
        "included": True,
        "policy_version": "zoomstack-national-house-context-render-v1",
        "source_sha256": national.source_sha256,
        "source_zoom": national.zoom,
        "source_geometry_sha256": national.audit["geometry_sha256"],
        "input_line_count": len(national.lines),
        "represented_line_count": represented_lines,
        "clipped_away_line_count": len(national.lines) - represented_lines,
        "map_path_count": path_count,
        "map_pen_down_distance_mm": round(pen_down_mm, 3),
        "physical_floor_filtered_path_count": floor_filtered_path_count,
        "physical_floor_filtered_length_mm": round(floor_filtered_length_mm, 3),
        "physical_floor_policy": (
            "quiet context paths below three physical nib widths are ledgered, "
            "not plotted; physical rail detail is unaffected"
        ),
        "candidate_place_count": len(national.places),
        "selected_place_count": len(selected_names),
        "selected_place_names": selected_names,
        "leader_lane_place_count": len(leader_names),
        "leader_lane_place_names": leader_names,
        "maximum_selected_place_count": MAX_NATIONAL_CITY_LABELS,
        "priority_place_count": len(NATIONAL_CITY_LABEL_PRIORITY),
        "outside_projected_field_place_names": outside_projected_field,
        "omitted_no_clear_placement_count": len(omitted_no_clear_placement),
        "omitted_no_clear_placement_names": omitted_no_clear_placement,
        "omitted_by_label_cap_count": len(omitted_by_label_cap),
        "omitted_by_label_cap_names": omitted_by_label_cap,
        "rail_conflicted_candidate_placement_count": rail_conflicted_candidate_count,
        "rail_label_clearance_mm": round(rail_label_clearance_mm, 3),
        "rail_label_clearance_uses_maximum_rail_nib": True,
        "rail_geometry_changed_for_labels": False,
        "white_knockout_or_ink_used": False,
        "frame_clearance_mm": FIELD_PADDING_MM,
        "place_selection_policy": (
            "fixed national editorial priority; OS city/capital candidates only; "
            "eight deterministic local placements followed by deterministic "
            "side leader lanes; 1.0 mm label-box separation; buffered maximum-"
            "rail-nib avoidance; maximum 14 labels; omit if none is clear"
        ),
        "context_is_below_rail_hierarchy": True,
        "invented_connector_count": 0,
        "connected_routing_graph_claimed": False,
        "operator_service_geometry_claimed": False,
        "class_results": [
            {
                "context_class": context_class,
                **{
                    key: (
                        round(value, 3)
                        if key == "map_pen_down_distance_mm"
                        else value
                    )
                    for key, value in result.items()
                },
                "style": NATIONAL_CONTEXT_STYLES[context_class].as_dict(),
            }
            for context_class, result in sorted(class_results.items())
        ],
    }


def _add_map_furniture(
    context: PlateContext,
    layers: dict[str, ArtworkLayer],
    projector: _RailProjector,
) -> None:
    copy_layer = ArtworkLayer(
        "rail-legend-copy",
        "Physical railway legend and map furniture",
        "black-0-4",
    )
    layers[copy_layer.id] = copy_layer

    rail_left = context.zones["title"].left
    rail_right = context.zones["title"].right
    legend_left = rail_left + 5.0
    legend_sample_right = legend_left + 16.0
    legend_label_x = legend_sample_right + 5.0
    legend_width = rail_right - legend_label_x - 2.0
    add_text(
        copy_layer,
        "PHYSICAL KEY",
        x_mm=legend_left,
        y_mm=129.0,
        preferred_cap_mm=4.0,
        maximum_width_mm=rail_right - legend_left,
        role="legend-heading",
    )
    legend_order = ("Multi Track", "Single Track", "Narrow Gauge", "Tunnel")
    for index, rail_type in enumerate(legend_order):
        style = PHYSICAL_RAIL_STYLES[rail_type]
        y = 142.0 + 12.0 * index
        layers[style.layer_id].add(
            [(legend_left, y + 1.6), (legend_sample_right, y + 1.6)],
            role="legend-swatch",
            attributes={
                "data-rail-type": rail_type,
                "data-legend-label": style.label,
                "data-operator-service-claim": "false",
            },
        )
        add_text(
            copy_layer,
            style.label,
            x_mm=legend_label_x,
            y_mm=y,
            preferred_cap_mm=3.2,
            maximum_width_mm=legend_width,
            minimum_cap_mm=3.2,
            allow_horizontal_condense=True,
            role="legend-label",
            attributes={"data-rail-type": rail_type},
        )

    north_x = rail_right - 12.0
    north_tip_y = 116.0
    north_base_y = 126.0
    copy_layer.add(
        [(north_x, north_base_y), (north_x, north_tip_y)],
        role="north-arrow",
    )
    copy_layer.add(
        [
            (north_x - 2.2, north_tip_y + 3.0),
            (north_x, north_tip_y),
            (north_x + 2.2, north_tip_y + 3.0),
        ],
        role="north-arrow-head",
    )
    add_text(
        copy_layer,
        "N",
        x_mm=north_x,
        y_mm=128.5,
        preferred_cap_mm=3.2,
        maximum_width_mm=8.0,
        minimum_cap_mm=3.2,
        anchor="middle",
        role="north-label",
    )

    scale_distance_m = _nice_scale_distance(projector, 52.0)
    scale_length_mm = scale_distance_m * projector.scale_mm_per_m
    scale_x = legend_left
    scale_y = 229.0
    copy_layer.add(
        [(scale_x, scale_y), (scale_x + scale_length_mm, scale_y)],
        role="scale-bar",
        attributes={"data-scale-distance-m": f"{scale_distance_m:g}"},
    )
    for x in (scale_x, scale_x + scale_length_mm):
        copy_layer.add(
            [(x, scale_y - 2.0), (x, scale_y + 2.0)],
            role="scale-bar-tick",
            attributes={"data-scale-distance-m": f"{scale_distance_m:g}"},
        )
    scale_label = (
        f"APPROX {scale_distance_m / 1000:g} KM"
        if scale_distance_m >= 1000.0
        else f"APPROX {scale_distance_m:g} M"
    )
    add_text(
        copy_layer,
        scale_label,
        x_mm=scale_x,
        y_mm=234.0,
        preferred_cap_mm=3.2,
        maximum_width_mm=rail_right - scale_x,
        minimum_cap_mm=3.2,
        role="scale-label",
        attributes={"data-scale-distance-m": f"{scale_distance_m:g}"},
    )


def _data_snapshot(rail: ZoomstackPhysicalRail) -> str:
    for key in ("snapshot", "retrieved_at", "product_version"):
        value = rail.audit.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"sha256:{rail.source_sha256}"


def _build_artwork(
    rail: ZoomstackPhysicalRail,
    *,
    simplify_mm: float,
) -> tuple[
    PlateArtwork,
    _RailProjector,
    list[_RailTrail],
    tuple[str, ...],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    if not isfinite(simplify_mm) or not 0.0 <= simplify_mm <= 0.20:
        raise MapPlotterError(
            "Physical railway simplification must be between 0 and 0.20 mm."
        )
    _validate_source(rail)
    context = PlateContext.load(FORMAT_ID)
    projector = _projector_for(rail, context.field)
    projected, degenerate = _project_features(
        rail,
        projector,
        simplify_mm=simplify_mm,
    )
    trails = _assemble_trails(projected)
    source_id = _source_id(rail)
    layers: dict[str, ArtworkLayer] = {
        style.layer_id: ArtworkLayer(style.layer_id, style.label, style.pen_id)
        for style in NATIONAL_CONTEXT_STYLES.values()
    }
    layers["context-city-labels"] = ArtworkLayer(
        "context-city-labels",
        "Collision-safe key city labels",
        "black-0-25",
    )
    layers["context-city-leaders"] = ArtworkLayer(
        "context-city-leaders",
        "Quiet key-city label leaders",
        "grey-0-25",
    )
    layers.update({
        style.layer_id: ArtworkLayer(style.layer_id, style.label, style.pen_id)
        for style in PHYSICAL_RAIL_STYLES.values()
    })
    context_results = _add_national_context(
        rail.national_context,
        layers=layers,
        projector=projector,
        source_id=source_id,
        rail_trails=trails,
    )
    type_results: dict[str, dict[str, Any]] = {
        rail_type: {
            "input_feature_ids": [],
            "represented_feature_ids": [],
            "omitted_feature_ids": [],
            "below_three_nib_floor_feature_ids": [],
            "map_path_count": 0,
            "map_pen_down_distance_mm": 0.0,
        }
        for rail_type in sorted(ZOOMSTACK_RAIL_TYPES)
    }
    for index, feature in enumerate(rail.features, start=1):
        type_results[feature.rail_type]["input_feature_ids"].append(
            _feature_identity(index)
        )
    degenerate_set = set(degenerate)
    for rail_type, result in type_results.items():
        result["omitted_feature_ids"].extend(
            sorted(degenerate_set.intersection(result["input_feature_ids"]))
        )

    for trail in trails:
        style = PHYSICAL_RAIL_STYLES[trail.rail_type]
        minimum_length = 3.0 * style.pen.mark_width_mm
        length = LineString(trail.points).length
        result = type_results[trail.rail_type]
        if length + 1e-9 < minimum_length:
            # National Zoomstack is intentionally tiled and heavily
            # segmented. Dropping each sub-floor fragment made the railway
            # network look like disconnected Morse code (over 90% vanished in
            # the first real proof). Preserve every non-degenerate source
            # fragment and flag the pen-risk instead; the user's detail-first
            # contract is stricter than the ordinary isolated-mark floor.
            result["below_three_nib_floor_feature_ids"].extend(trail.feature_ids)
        layers[style.layer_id].add(
            trail.points,
            source_ref=source_id,
            role=style.role,
            attributes={
                "data-rail-type": trail.rail_type,
                "data-represented-feature-ids": ";".join(trail.feature_ids),
                "data-source-objects": ";".join(trail.source_objects),
                "data-source-vertex-count": str(trail.source_vertex_count),
                "data-source-classification": "OS Open Zoomstack rail.type",
                "data-operator-service-claim": "false",
                "data-routing-graph-claim": "false",
                "data-three-nib-floor-status": (
                    "below-review-required"
                    if length + 1e-9 < minimum_length
                    else "passed"
                ),
                "data-detail-preservation-exception": (
                    "true" if length + 1e-9 < minimum_length else "false"
                ),
            },
        )
        result["represented_feature_ids"].extend(trail.feature_ids)
        result["map_path_count"] += 1
        result["map_pen_down_distance_mm"] += length

    _add_map_furniture(context, layers, projector)
    source_record = {
        "id": source_id,
        "publisher": "Ordnance Survey",
        "title": "OS Open Zoomstack",
        "url": SOURCE_URL,
        "licence": SOURCE_LICENCE,
        "attribution": SOURCE_ATTRIBUTION,
        "sha256": rail.source_sha256,
        "source_path": str(rail.source_path),
        "zoom": rail.zoom,
        "bounds_wgs84": list(rail.bounds_wgs84),
        "use": (
            "physical/cartographic rail linework and quiet national basemap context"
            if rail.national_context is not None
            else "physical/cartographic rail linework only"
        ),
    }
    artwork = PlateArtwork(
        subject_id=ARTIFACT_ID,
        domain="transit-rail",
        subject_kind="national-physical-railway",
        title="GREAT BRITAIN RAILWAYS",
        subtitle="NATIONAL INFRASTRUCTURE PLATE",
        details=(
            f"OS OPEN ZOOMSTACK / Z{rail.zoom}",
            "PHYSICAL LINEWORK ONLY",
            "NO SERVICE ROUTE CLAIM",
        ),
        credit_line=(
            "CONTAINS OS DATA (C) CROWN COPYRIGHT | "
            "AND DATABASE RIGHT 2026 / OGL V3.0"
        ),
        scale_status=(
            "approximate local-metric equirectangular projection at the "
            "source-vertex mean latitude"
        ),
        evidence_status="hash-pinned OS Open Zoomstack physical rail linework",
        rights_status="commercial-clear",
        sources=(source_record,),
        context=context,
        layers=list(layers.values()),
        artifact_kind="national-physical-railway-pen-map",
        rendering_preset="university-marathon-house-national-rail-v1",
        format_subject_policy="zoomstack-physical-rail-only-v1",
        source_provider="Ordnance Survey / OS Open Zoomstack",
        source_license=SOURCE_LICENCE,
        data_snapshot=_data_snapshot(rail),
        notes=(
            "Zoomstack rail linework is deliberately broken under some mapped obstructions and is not a connected routing graph.",
            "Exact same-class endpoints may be assembled into longer pen trails only; no connector geometry is created.",
            "Preview colours are house-palette display aids, not measured ink-colour claims or operator trade dress.",
            "The optional national backdrop uses only the same hash-pinned Zoomstack bytes and remains visually subordinate to physical rail.",
        ),
        catalog_record={
            "source_audit": copy.deepcopy(rail.audit),
            "scope": "Great Britain physical railway depiction",
            "national_context_source_audit": (
                copy.deepcopy(rail.national_context.audit)
                if rail.national_context is not None
                else None
            ),
            "operator_service_geometry_claimed": False,
            "connected_routing_graph_claimed": False,
        },
        rendering_metadata={
            "stock_preview_hex": HOUSE_STOCK_HEX,
            "physical_rail_styles": [
                PHYSICAL_RAIL_STYLES[rail_type].as_dict()
                for rail_type in ("Multi Track", "Single Track", "Narrow Gauge", "Tunnel")
            ],
            "national_context_styles": [
                NATIONAL_CONTEXT_STYLES[context_class].as_dict()
                for context_class in (
                    "national-boundary",
                    "road-primary",
                    "road-motorway",
                    "surface-water-bank",
                    "coastline",
                )
            ],
            "national_context_zoom": (
                rail.national_context.zoom
                if rail.national_context is not None
                else None
            ),
        },
        rights_metadata={
            "operator_service_claimed": False,
            "operator_logo_used": False,
            "operator_trade_dress_used": False,
        },
    )
    return artwork, projector, trails, degenerate, type_results, context_results


def _postprocess_svg(root: ET.Element) -> None:
    root.set("style", f"background-color:{HOUSE_STOCK_HEX}")
    root.set("data-preview-stock-only", "true")
    title = root.find(svg_tag("title"))
    if title is not None:
        title.text = "Great Britain physical railways"
    description = root.find(svg_tag("desc"))
    if description is not None:
        description.text = (
            "A3 landscape pen plate of OS Open Zoomstack physical railway "
            "classes over a quiet house-style national context. It makes no "
            "operator, passenger-service, frequency, or routing claim."
        )
    namedview = root.find(f"{{{SODIPODI_NS}}}namedview")
    if namedview is not None:
        namedview.set("id", "namedview-mapplot-physical-rail")
        namedview.set("pagecolor", HOUSE_STOCK_HEX)

    for style in NATIONAL_CONTEXT_STYLES.values():
        logical = root.find(
            f".//{svg_tag('g')}[@id='logical-{style.layer_id}']"
        )
        if logical is None:
            continue
        logical.set("stroke", style.preview_hex)
        logical.set("stroke-width", f"{style.pen.mark_width_mm:g}")
        logical.set("data-context-is-background", "true")
        logical.set("data-preview-is-physical-ink-claim", "false")
        logical.set("data-operator-service-claim", "false")
    city_labels = root.find(
        f".//{svg_tag('g')}[@id='logical-context-city-labels']"
    )
    if city_labels is not None:
        city_labels.set("stroke", "#4A4F53")
        city_labels.set("stroke-width", "0.25")
        city_labels.set("data-context-is-background", "true")
        city_labels.set("data-preview-is-physical-ink-claim", "false")
        city_labels.set("data-operator-service-claim", "false")
    city_leaders = root.find(
        f".//{svg_tag('g')}[@id='logical-context-city-leaders']"
    )
    if city_leaders is not None:
        city_leaders.set("stroke", CONTEXT_STYLE["boundaries"][1])
        city_leaders.set("stroke-width", "0.25")
        city_leaders.set("data-context-is-background", "true")
        city_leaders.set("data-preview-is-physical-ink-claim", "false")
        city_leaders.set("data-operator-service-claim", "false")

    for style in PHYSICAL_RAIL_STYLES.values():
        logical = root.find(
            f".//{svg_tag('g')}[@id='logical-{style.layer_id}']"
        )
        if logical is None:
            raise MapPlotterError(
                f"Rendered physical rail plate lost layer {style.layer_id!r}."
            )
        logical.set("stroke", style.preview_hex)
        logical.set("stroke-width", f"{style.pen.mark_width_mm:g}")
        logical.set("data-preview-is-physical-ink-claim", "false")
        logical.set("data-operator-service-claim", "false")


def render_zoomstack_rail_plate(
    rail: ZoomstackPhysicalRail,
    *,
    simplify_mm: float = DEFAULT_SIMPLIFICATION_TOLERANCE_MM,
    generated_at: str | None = None,
) -> tuple[ET.Element, dict[str, Any]]:
    """Render one A3 landscape physical-railway plate and plot manifest."""

    (
        artwork,
        projector,
        trails,
        degenerate,
        type_results,
        context_results,
    ) = _build_artwork(
        rail,
        simplify_mm=simplify_mm,
    )
    root, manifest = render_plate(artwork, generated_at=generated_at)
    _postprocess_svg(root)

    represented: set[str] = set()
    omitted: set[str] = set()
    rail_type_results: list[dict[str, Any]] = []
    for rail_type in ("Multi Track", "Single Track", "Narrow Gauge", "Tunnel"):
        result = type_results[rail_type]
        input_ids = sorted(set(result["input_feature_ids"]))
        represented_ids = sorted(set(result["represented_feature_ids"]))
        omitted_ids = sorted(set(result["omitted_feature_ids"]))
        below_floor_ids = sorted(
            set(result["below_three_nib_floor_feature_ids"])
        )
        represented.update(represented_ids)
        omitted.update(omitted_ids)
        rail_type_results.append(
            {
                "rail_type": rail_type,
                "input_feature_count": len(input_ids),
                "represented_feature_count": len(represented_ids),
                "omitted_feature_count": len(omitted_ids),
                "below_three_nib_floor_feature_count": len(below_floor_ids),
                "below_three_nib_floor_feature_ids": below_floor_ids,
                "map_path_count": result["map_path_count"],
                "map_pen_down_distance_mm": round(
                    result["map_pen_down_distance_mm"], 3
                ),
                "omitted_feature_ids": omitted_ids,
                "style": PHYSICAL_RAIL_STYLES[rail_type].as_dict(),
            }
        )
    expected = {
        _feature_identity(index) for index in range(1, len(rail.features) + 1)
    }
    if represented.intersection(omitted) or represented.union(omitted) != expected:
        raise MapPlotterError(
            "Physical rail manifest cannot account for every source feature."
        )

    full_default_bounds = all(
        abs(actual - expected_value) <= 1e-9
        for actual, expected_value in zip(
            rail.bounds_wgs84,
            DEFAULT_GB_BOUNDS,
            strict=True,
        )
    )
    join_count = sum(max(0, len(trail.feature_ids) - 1) for trail in trails)
    below_floor_count = sum(
        int(result["below_three_nib_floor_feature_count"])
        for result in rail_type_results
    )
    physical_map_ink_mm2 = sum(
        result["map_pen_down_distance_mm"]
        * PHYSICAL_RAIL_STYLES[result["rail_type"]].pen.mark_width_mm
        for result in rail_type_results
    )
    context_map_ink_mm2 = sum(
        LineString(record.points).length * layer.pen.mark_width_mm
        for layer in artwork.layers
        if layer.id.startswith("context-")
        for record in layer.records
    )
    field_frame_nib = float(
        artwork.context.plate["map_linework_nib_mm"]["hairline"]
    )
    physical_map_ink_mm2 += context_map_ink_mm2 + (
        2.0 * (artwork.context.field.width + artwork.context.field.height)
        * field_frame_nib
    )
    field_area = artwork.context.field.width * artwork.context.field.height

    manifest["physical_rail_qa"] = {
        "policy_version": "zoomstack-national-physical-rail-plate-v1",
        "source_product": "OS Open Zoomstack",
        "source_sha256": rail.source_sha256,
        "source_geometry_sha256": rail.audit["geometry_sha256"],
        "source_bounds_wgs84": list(rail.bounds_wgs84),
        "default_gb_extent_selected": full_default_bounds,
        "input_feature_count": len(rail.features),
        "represented_feature_count": len(represented),
        "omitted_feature_count": len(omitted),
        "omitted_feature_ids": sorted(omitted),
        "degenerate_projected_feature_ids": list(degenerate),
        "below_three_nib_floor_feature_count": below_floor_count,
        "below_three_nib_floor_paths_emitted": True,
        "detail_first_no_non_degenerate_source_fragment_dropped": not omitted,
        "source_feature_parity": represented.union(omitted) == expected,
        "emitted_or_ledgered_once": not represented.intersection(omitted),
        "assembled_trail_count": len(trails),
        "exact_same_class_endpoint_join_count": join_count,
        "invented_connector_count": 0,
        "interior_coordinate_join_count": 0,
        "cross_class_join_count": 0,
        "connected_routing_graph_claimed": False,
        "operator_service_geometry_claimed": False,
        "operator_or_service_classification_used": False,
        "main_line_classification_claimed": False,
        "multi_track_is_not_asserted_to_mean_main_line": True,
        "rail_type_results": rail_type_results,
    }
    manifest["national_context_qa"] = context_results
    manifest["rendering"]["view_kind"] = "national-physical-railway"
    manifest["rendering"]["projection"] = {
        "method": "local-metric equirectangular",
        "central_longitude": round(projector.lon0, 9),
        "central_latitude": round(projector.lat0, 9),
        "scale_denominator_at_central_latitude": round(
            projector.scale_denominator
        ),
        "scale_mm_per_m": round(projector.scale_mm_per_m, 12),
        "source_width_m": round(projector.width_m, 3),
        "source_height_m": round(projector.height_m, 3),
        "simplification_tolerance_mm": simplify_mm,
        "simplification_applied_per_source_feature_before_trail_assembly": True,
        "source_endpoints_preserved": True,
    }
    manifest["rendering"]["house_style"] = {
        "authority": "university/marathon city-map house palette and furniture",
        "format_id": FORMAT_ID,
        "stock_preview_hex": HOUSE_STOCK_HEX,
        "stock_preview_is_plotted_fill": False,
        "field_frame": True,
        "double_safe_border": True,
        "north_mark": True,
        "approximate_scale_bar": True,
        "physical_key": True,
        "national_context_included": bool(context_results["included"]),
        "national_context_quieter_than_single_track": (
            max(
                style.pen.mark_width_mm
                for style in NATIONAL_CONTEXT_STYLES.values()
            )
            < PHYSICAL_RAIL_STYLES["Single Track"].pen.mark_width_mm
        ),
        "rail_hierarchy_mm": {
            "multi_track": PHYSICAL_RAIL_STYLES["Multi Track"].pen.mark_width_mm,
            "single_track": PHYSICAL_RAIL_STYLES["Single Track"].pen.mark_width_mm,
            "maximum_coloured_context": max(
                style.pen.mark_width_mm
                for style in NATIONAL_CONTEXT_STYLES.values()
            ),
        },
    }
    manifest["plot_summary"]["field_ink_mm2_upper_bound"] = round(
        physical_map_ink_mm2, 1
    )
    manifest["plot_summary"]["field_ink_coverage_upper_bound"] = round(
        physical_map_ink_mm2 / field_area,
        6,
    )
    manifest["plot_summary"]["field_ink_measurement_scope"] = (
        "physical rail, national context, city-label map paths, and map-field "
        "frame; side-rail legend and page furniture excluded"
    )
    if omitted:
        manifest["production_readiness"]["blocking_reasons"].append(
            f"{len(omitted)} source rail features are degenerate after projection"
        )
    if below_floor_count:
        manifest["production_readiness"]["blocking_reasons"].append(
            f"{below_floor_count} represented source rail fragments remain below "
            "their physical three-nib plotting floor and require a real pen proof"
        )
        manifest["warnings"].append(
            "Every non-degenerate rail fragment is emitted for detail parity; "
            "sub-three-nib paths may plot as short marks and require stock testing."
        )
    if not context_results["included"]:
        manifest["warnings"].append(
            "No national context was attached; use the CLI default context zoom "
            "for the university/marathon house-language plate."
        )
    manifest["warnings"].append(
        "This is a source-classified physical railway depiction, not an operator "
        "map, timetable, passenger route, or connected routing graph."
    )
    return root, manifest


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pen_only_tree(root: ET.Element, pen_id: str) -> ET.Element:
    result = copy.deepcopy(root)
    for child in list(result):
        if (
            child.tag == svg_tag("g")
            and child.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer"
            and child.get("data-plot-pen-id") != pen_id
        ):
            result.remove(child)
    return result


def _rasterize(svg_path: Path, png_path: Path, *, dpi: float) -> None:
    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise MapPlotterError("Physical railway PNG export requires Inkscape on PATH.")
    result = subprocess.run(
        [
            inkscape,
            str(svg_path),
            "--export-type=png",
            "--export-area-page",
            f"--export-dpi={dpi:g}",
            f"--export-background={HOUSE_STOCK_HEX}",
            "--export-background-opacity=255",
            f"--export-filename={png_path}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise MapPlotterError(f"Physical railway PNG export failed: {detail}.")


def write_zoomstack_rail_plate(
    rail: ZoomstackPhysicalRail,
    output_dir: Path,
    *,
    simplify_mm: float = DEFAULT_SIMPLIFICATION_TOLERANCE_MM,
    png: bool = False,
    png_dpi: float = 180.0,
    split_pens: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Write the master SVG, plot manifest, optional PNG, and pen jobs."""

    if not isfinite(png_dpi) or png_dpi <= 0.0:
        raise MapPlotterError("Physical railway PNG DPI must be positive.")
    output_dir.mkdir(parents=True, exist_ok=True)
    root, manifest = render_zoomstack_rail_plate(
        rail,
        simplify_mm=simplify_mm,
        generated_at=generated_at,
    )
    ET.indent(root, space="  ")
    svg_path = output_dir / f"{ARTIFACT_ID}.svg"
    manifest_path = output_dir / f"{ARTIFACT_ID}.plot.json"
    ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)

    pen_files: list[dict[str, Any]] = []
    if split_pens:
        for record in manifest["pen_sequence"]:
            step = int(record["step"])
            pen_id = str(record["pen_id"])
            pen_path = output_dir / f"{ARTIFACT_ID}.pen-{step:02d}-{pen_id}.svg"
            pen_root = _pen_only_tree(root, pen_id)
            ET.indent(pen_root, space="  ")
            ET.ElementTree(pen_root).write(
                pen_path,
                encoding="utf-8",
                xml_declaration=True,
            )
            pen_files.append(
                {
                    "step": step,
                    "pen_id": pen_id,
                    "path": str(pen_path.resolve()),
                    "sha256": _sha256_path(pen_path),
                }
            )

    outputs: dict[str, Any] = {
        "svg": {"path": str(svg_path.resolve()), "sha256": _sha256_path(svg_path)},
        "manifest": {"path": str(manifest_path.resolve())},
        "pen_files": pen_files,
    }
    if png:
        png_path = output_dir / f"{ARTIFACT_ID}.png"
        _rasterize(svg_path, png_path, dpi=png_dpi)
        outputs["png"] = {
            "path": str(png_path.resolve()),
            "dpi": png_dpi,
            "sha256": _sha256_path(png_path),
        }
    manifest["outputs"] = outputs
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    outputs["manifest"]["sha256"] = _sha256_path(manifest_path)
    return outputs


__all__ = [
    "ARTIFACT_ID",
    "DEFAULT_SIMPLIFICATION_TOLERANCE_MM",
    "FORMAT_ID",
    "HOUSE_STOCK_HEX",
    "PHYSICAL_RAIL_STYLES",
    "render_zoomstack_rail_plate",
    "write_zoomstack_rail_plate",
]
