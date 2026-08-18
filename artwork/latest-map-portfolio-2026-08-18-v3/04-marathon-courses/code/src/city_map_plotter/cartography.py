from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
from math import ceil, cos, floor, hypot, isfinite, pi, sin
import re
from typing import Any, Iterable, cast

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    Point,
    Polygon,
    box,
)
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize_full, substring, unary_union
from shapely.strtree import STRtree

from .features import (
    effective_highway_value,
    has_landmark_identity,
    is_heritage_site_candidate,
    is_identified_heritage_site,
)
from .course import COURSE_LAYER
from .geometry import Layout, load_plate_format, poster_preset_composition
from .models import MapFeature, MapPlotterError, PlotStroke
from .topology import (
    build_road_topology,
    grade_signature,
    round_polyline_within_page_error,
    simplify_road_topology,
    suppress_river_centerline_segments,
    topology_lines_from_map_features,
)


ROAD_LAYERS = {
    "roads_major",
    "roads_secondary",
    "roads_local",
    "roads_other",
    "paths",
}

AREA_OUTLINE_LAYERS = {"water_areas", "green_space", "buildings", "road_areas"}
SOURCE_COMPLETE_DETAIL_PROFILES = frozenset({"faithful", "plotter-faithful"})
INK_BUDGETED_DETAIL_PROFILES = frozenset({"ink-balanced"})
FULL_CARTOGRAPHY_DETAIL_PROFILES = (
    SOURCE_COMPLETE_DETAIL_PROFILES | INK_BUDGETED_DETAIL_PROFILES
)
PHYSICAL_MINIMUM_GATED_DETAIL_PROFILES = frozenset({"plotter-faithful", "ink-balanced"})
DETAIL_PROFILE_CHOICES = FULL_CARTOGRAPHY_DETAIL_PROFILES | {"plot"}
WATER_FILL_CHOICES = frozenset({"none", "dots"})
WATER_DOT_SPACING_MM = 2.2
WATER_DOT_DIAMETER_MM = 0.32
WATER_DOT_VERTICES = 8
WATER_DOT_INK_BUDGET_FIELD_FRACTION = 0.02
SURFACE_CENTRELINE_WATERWAYS = frozenset(
    {"river", "canal", "tidal_channel", "stream", "drain", "ditch"}
)
LANDMARK_INK_BUDGET_FIELD_FRACTION = 0.004
LANDMARK_MIN_ORIENTED_SPAN_MM = 0.50
LANDMARK_MIN_VISIBLE_PERIMETER_MM = 0.75
LANDMARK_REQUIRED_MIN_AREA_MM2 = 0.75
LANDMARK_ROLE_BUCKETS = (
    ("cathedral", frozenset({"cathedral"}), 4),
    ("heritage", frozenset({"heritage"}), 3),
    ("stadium", frozenset({"stadium"}), 3),
    ("university", frozenset({"university"}), 8),
    ("worship", frozenset({"worship"}), 5),
    ("civic_culture", frozenset({"civic", "culture"}), 6),
    ("health_transport", frozenset({"health", "transport"}), 3),
)
LANDMARK_MAX_SOURCE_COUNT = sum(limit for _name, _roles, limit in LANDMARK_ROLE_BUCKETS)


@dataclass(frozen=True)
class LandmarkBuildingPolicy:
    """How heavy landmark buildings are, and how many of them a sheet carries.

    Resolved from the active plate's ``landmark_buildings`` block. A5's field
    is roughly half A4's, so the object count and outline budget that read as a
    legible set of landmarks on A4 silt up the smaller sheet; the same values
    cannot serve both.
    """

    nib_role: str
    nib_mm: float
    ink_budget_field_fraction: float
    max_objects: int
    minimum_area_scale: float
    minimum_oriented_span_mm: float
    minimum_visible_perimeter_mm: float
    role_buckets: tuple[tuple[str, frozenset[str], int], ...]

    @property
    def max_source_count(self) -> int:
        return sum(limit for _name, _roles, limit in self.role_buckets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "nib_role": self.nib_role,
            "nib_mm": self.nib_mm,
            "ink_budget_field_fraction": self.ink_budget_field_fraction,
            "max_objects": self.max_objects,
            "minimum_area_scale": self.minimum_area_scale,
            "minimum_oriented_span_mm": self.minimum_oriented_span_mm,
            "minimum_visible_perimeter_mm": self.minimum_visible_perimeter_mm,
        }


DEFAULT_LANDMARK_POLICY = LandmarkBuildingPolicy(
    nib_role="hairline",
    nib_mm=0.25,
    ink_budget_field_fraction=LANDMARK_INK_BUDGET_FIELD_FRACTION,
    max_objects=LANDMARK_MAX_SOURCE_COUNT,
    minimum_area_scale=1.0,
    minimum_oriented_span_mm=LANDMARK_MIN_ORIENTED_SPAN_MM,
    minimum_visible_perimeter_mm=LANDMARK_MIN_VISIBLE_PERIMETER_MM,
    role_buckets=LANDMARK_ROLE_BUCKETS,
)


def _scaled_role_buckets(
    max_objects: int,
) -> tuple[tuple[str, frozenset[str], int], ...]:
    """Rescale the per-role caps to a sheet's total, keeping their proportions.

    Every role keeps at least one slot: a sheet that admits landmarks at all
    should still be able to show its cathedral.
    """

    total = LANDMARK_MAX_SOURCE_COUNT
    scaled: list[tuple[str, frozenset[str], int]] = []
    for name, roles, limit in LANDMARK_ROLE_BUCKETS:
        share = max(1, int(round(limit * max_objects / total)))
        scaled.append((name, roles, share))
    return tuple(scaled)


def landmark_building_policy(layout: Layout) -> LandmarkBuildingPolicy:
    """Resolve the landmark policy the active plate binds."""

    if layout.format_id is None:
        return DEFAULT_LANDMARK_POLICY
    block = load_plate_format(layout.format_id).get("landmark_buildings")
    if not isinstance(block, dict):
        return DEFAULT_LANDMARK_POLICY
    try:
        max_objects = int(block["max_objects"])
        policy = LandmarkBuildingPolicy(
            nib_role=str(block["nib_role"]),
            nib_mm=float(block["nib_mm"]),
            ink_budget_field_fraction=float(block["ink_budget_field_fraction"]),
            max_objects=max_objects,
            minimum_area_scale=float(block["minimum_area_scale"]),
            minimum_oriented_span_mm=float(block["minimum_oriented_span_mm"]),
            minimum_visible_perimeter_mm=float(block["minimum_visible_perimeter_mm"]),
            role_buckets=_scaled_role_buckets(max_objects),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Plate format {layout.format_id!r} has an invalid landmark-building "
            "policy."
        ) from exc
    if (
        max_objects < 1
        or policy.minimum_area_scale <= 0
        or policy.ink_budget_field_fraction <= 0
        or policy.nib_mm <= 0
    ):
        raise MapPlotterError(
            f"Plate format {layout.format_id!r} has an out-of-range "
            "landmark-building policy."
        )
    return policy


RoadGradeKey = tuple[tuple[str, str], ...]
RoadGroupKey = tuple[str, int, str, str, RoadGradeKey, str]
# Topology roads are rounded to 4 decimal places during cartographic clipping,
# then SVG coordinates are formatted to 3 decimal places.  Reserving the
# worst-case two-axis displacement from both stages makes the final combined
# error statement literal rather than ignoring serialization.
ROAD_COORDINATE_QUANTIZATION_ALLOWANCE_MM = hypot(0.00005 + 0.0005, 0.00005 + 0.0005)


@dataclass
class CartographyResult:
    strokes: list[PlotStroke]
    diagnostics: dict[str, Any]
    warnings: list[str]


_LANDMARK_WORSHIP_BUILDINGS = frozenset(
    {
        "church",
        "chapel",
        "religious",
        "mosque",
        "synagogue",
        "temple",
        "shrine",
    }
)
_LANDMARK_HERITAGE_BUILDINGS = frozenset({"castle", "palace"})
_LANDMARK_HISTORIC_BUILDINGS = frozenset(
    {"castle", "memorial", "monument", "palace", "tower"}
)
_LANDMARK_FALSE_BUILDING_VALUES = frozenset(
    {"", "0", "false", "no", "none", "nonexistent"}
)
_LANDMARK_INACTIVE_VALUES = frozenset(
    {
        "abandoned",
        "construction",
        "demolished",
        "destroyed",
        "disused",
        "proposed",
        "razed",
        "removed",
    }
)
_LANDMARK_LIFECYCLE_KEYS = (
    "abandoned:building",
    "construction:building",
    "demolished:building",
    "destroyed:building",
    "disused:building",
    "proposed:building",
    "razed:building",
    "removed:building",
)
_LANDMARK_FALSE_LIFECYCLE_VALUES = frozenset({"", "no", "0", "false"})


def _landmark_has_identity(tags: dict[str, str]) -> bool:
    return has_landmark_identity(tags)


def _landmark_lifecycle_rejection(tags: dict[str, str]) -> str | None:
    """Return the exact active-building lifecycle gate that rejected tags."""

    building = tags.get("building", "").strip().casefold()
    if building in _LANDMARK_INACTIVE_VALUES:
        return f"inactive building value building={building}"
    for key in _LANDMARK_LIFECYCLE_KEYS:
        value = tags.get(key, "").strip().casefold()
        if value not in _LANDMARK_FALSE_LIFECYCLE_VALUES:
            return f"active lifecycle marker {key}={value}"
    status = tags.get("status", "").strip().casefold()
    if status in _LANDMARK_INACTIVE_VALUES:
        return f"inactive status={status}"
    lifecycle = tags.get("lifecycle", "").strip().casefold()
    if lifecycle in _LANDMARK_INACTIVE_VALUES:
        return f"inactive lifecycle={lifecycle}"
    construction = tags.get("construction", "").strip().casefold()
    if construction not in _LANDMARK_FALSE_LIFECYCLE_VALUES:
        return f"active construction={construction}"
    return None


def _required_landmark_is_building(tags: dict[str, str]) -> bool:
    building = tags.get("building", "").strip().casefold()
    return (
        building not in _LANDMARK_FALSE_BUILDING_VALUES
        or tags.get("leisure", "").strip().casefold() == "stadium"
        or is_identified_heritage_site(tags)
    )


def _source_object_reference(source_ref: str) -> str:
    object_type, separator, remainder = source_ref.partition("/")
    object_id, second_separator, _part = remainder.partition("/")
    if separator and second_separator and object_type in {"way", "relation"}:
        return f"{object_type}/{object_id}"
    if separator and object_type in {"way", "relation"}:
        return f"{object_type}/{remainder}"
    return source_ref


def _landmark_building_role(
    tags: dict[str, str], area_mm2: float, area_scale: float = 1.0
) -> tuple[str, int] | None:
    """Return a physically eligible semantic role and deterministic rank.

    ``area_scale`` raises or lowers every footprint threshold together, so a
    small sheet admits only relatively larger landmarks without needing a
    second copy of the role table.
    """

    building = tags.get("building", "").strip().casefold()
    amenity = tags.get("amenity", "").strip().casefold()
    leisure = tags.get("leisure", "").strip().casefold()
    tourism = tags.get("tourism", "").strip().casefold()
    healthcare = tags.get("healthcare", "").strip().casefold()
    historic = tags.get("historic", "").strip().casefold()
    if _landmark_lifecycle_rejection(tags) is not None:
        return None
    positive_building = building not in _LANDMARK_FALSE_BUILDING_VALUES
    identified = _landmark_has_identity(tags)

    role: str | None = None
    minimum_area_mm2 = float("inf")
    identity_required = False
    if building == "cathedral":
        role, minimum_area_mm2 = "cathedral", 0.75
    elif (
        building in _LANDMARK_HERITAGE_BUILDINGS
        or (positive_building and historic in _LANDMARK_HISTORIC_BUILDINGS)
        or is_identified_heritage_site(tags)
    ):
        # A generic ``building=tower`` covers modern water, cooling, control,
        # and communications towers and is far too noisy for a landmark
        # plate.  Towers, monuments, and memorials therefore need an explicit
        # historic semantic; castles and palaces may express that semantic
        # directly as the building value.  Every heritage footprint must also
        # carry a stable identity/listing so an anonymous micromapped object
        # cannot consume the deliberately small heritage quota.
        role, minimum_area_mm2, identity_required = "heritage", 0.75, True
    elif building in _LANDMARK_WORSHIP_BUILDINGS or (
        positive_building and amenity == "place_of_worship"
    ):
        role, minimum_area_mm2 = "worship", 0.75
        if not identified:
            minimum_area_mm2 = 2.0
    elif building in {"university", "college"} or (
        positive_building and amenity in {"university", "college"}
    ):
        role, minimum_area_mm2 = "university", 1.5 if identified else 3.0
    elif building in {"stadium", "grandstand", "sports_hall"}:
        role, minimum_area_mm2 = "stadium", 1.5
    elif leisure == "stadium":
        role, minimum_area_mm2 = "stadium", 1.5 if positive_building else 6.0
    elif building in {"civic", "public", "government"} or (
        positive_building and amenity in {"townhall", "courthouse", "library"}
    ):
        role, minimum_area_mm2, identity_required = "civic", 1.5, True
    elif positive_building and (
        amenity in {"theatre", "arts_centre"} or tourism in {"museum", "gallery"}
    ):
        role, minimum_area_mm2, identity_required = "culture", 1.5, True
    elif building == "hospital" or (
        positive_building and (amenity == "hospital" or healthcare == "hospital")
    ):
        role, minimum_area_mm2, identity_required = "health", 3.0, True
    elif building == "train_station":
        role, minimum_area_mm2, identity_required = "transport", 3.0, True

    if role is None or area_mm2 + 1e-9 < minimum_area_mm2 * area_scale:
        return None
    if identity_required and not identified:
        return None
    role_priority = {
        "cathedral": 8,
        "heritage": 7,
        "stadium": 7,
        "university": 6,
        "worship": 5,
        "civic": 4,
        "culture": 4,
        "health": 3,
        "transport": 3,
    }[role]
    return role, role_priority + int(identified)


def is_landmark_building_candidate(tags: dict[str, str]) -> bool:
    """Return whether tags can represent a house-style landmark building.

    This is the tag, identity, and lifecycle gate only. Renderers that know the
    final paper projection still apply their physical-area and ink-budget
    gates. Keeping this predicate beside the authoritative role table lets a
    local PBF build mirror the landmark-only live query instead of importing
    every house and shed into a transit contract.
    """

    return _landmark_building_role(tags, area_mm2=float("inf")) is not None


def _minimum_oriented_span_mm(geometry: BaseGeometry) -> float:
    if geometry.is_empty:
        return 0.0
    rectangle = geometry.minimum_rotated_rectangle
    if not isinstance(rectangle, Polygon):
        return 0.0
    coordinates = list(rectangle.exterior.coords)
    edge_lengths = [
        hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(coordinates, coordinates[1:])
        if hypot(x2 - x1, y2 - y1) > 1e-9
    ]
    return min(edge_lengths, default=0.0)


def _water_dot_ring(
    x_mm: float,
    y_mm: float,
    *,
    diameter_mm: float,
) -> list[tuple[float, float]]:
    radius = diameter_mm / 2
    return [
        (
            x_mm + radius * cos(2 * pi * index / WATER_DOT_VERTICES),
            y_mm + radius * sin(2 * pi * index / WATER_DOT_VERTICES),
        )
        for index in range(WATER_DOT_VERTICES + 1)
    ]


def _water_stipple_strokes(
    records: list[tuple[MapFeature, BaseGeometry]],
    *,
    preset: str,
    clip_rect: tuple[float, float, float, float],
    spacing_mm: float = WATER_DOT_SPACING_MM,
    diameter_mm: float = WATER_DOT_DIAMETER_MM,
    nib_mm: float = 0.25,
) -> tuple[list[PlotStroke], dict[str, Any]]:
    """Create deterministic, plotter-safe micro-circles inside mapped water.

    A pen tap or SVG fill has no portable physical meaning. Each visible dot is
    therefore an eight-segment closed path whose circumference clears the
    three-nib floor of the physical pen selected for the waterways layer.
    """

    left, top, right, bottom = clip_rect
    if not isfinite(nib_mm) or nib_mm <= 0:
        raise MapPlotterError("Water-dot nib must be a positive finite width.")
    if not isfinite(spacing_mm) or spacing_mm <= 0:
        raise MapPlotterError("Water-dot spacing must be positive and finite.")
    if not isfinite(diameter_mm) or diameter_mm <= 0:
        raise MapPlotterError("Water-dot diameter must be positive and finite.")
    requested_diameter_mm = diameter_mm
    # A regular octagon with centreline diameter equal to the nib has a
    # perimeter just over three nib widths.  Preserve the designed 0.32 mm
    # dots for fine pens, but grow them when a wider physical pen is resolved.
    diameter_mm = max(diameter_mm, nib_mm)
    radius = diameter_mm / 2
    # Inset the centreline by its radius plus the physical pen radius.  The
    # final 0.005 mm absorbs coordinate serialization so ink cannot bleed over
    # the mapped water boundary.
    inset_mm = radius + nib_mm / 2 + 0.005
    row_step = spacing_mm * 0.8660254037844386
    stroke_index_by_centre: dict[tuple[int, int], int] = {}
    strokes: list[PlotStroke] = []
    represented_sources: set[str] = set()
    eligible_area_mm2 = 0.0
    for source, raw_area in sorted(
        records,
        key=lambda item: (
            item[0].osm_type,
            item[0].osm_id,
            item[0].part,
        ),
    ):
        area = raw_area.buffer(-inset_mm)
        if area.is_empty:
            continue
        eligible_area_mm2 += area.area
        min_x, min_y, max_x, max_y = area.bounds
        first_row = ceil((min_y - top) / row_step)
        last_row = int((max_y - top) // row_step)
        source_refs = _surface_area_source_refs(source)
        serialized_source_refs = ";".join(source_refs)
        for row in range(first_row, last_row + 1):
            y_mm = top + row * row_step
            offset = spacing_mm / 2 if row % 2 else 0.0
            first_column = ceil((min_x - left - offset) / spacing_mm)
            last_column = int((max_x - left - offset) // spacing_mm)
            for column in range(first_column, last_column + 1):
                x_mm = left + offset + column * spacing_mm
                centre_key = (round(x_mm * 10_000), round(y_mm * 10_000))
                if not area.covers(Point(x_mm, y_mm)):
                    continue
                existing_index = stroke_index_by_centre.get(centre_key)
                if existing_index is not None:
                    # Overlapping OSM surfaces share one physical dot, but it
                    # must carry every source it represents.  Otherwise the
                    # later surface in deterministic source order disappears
                    # from lineage merely because an earlier polygon claimed
                    # the same lattice point first.
                    existing = strokes[existing_index]
                    combined_refs = sorted(
                        {
                            *existing.tags.get("source-refs", "").split(";"),
                            *source_refs,
                        }
                        - {""}
                    )
                    existing.tags["source-count"] = str(len(combined_refs))
                    existing.tags["source-refs"] = ";".join(combined_refs)
                    represented_sources.update(source_refs)
                    continue
                stroke_index_by_centre[centre_key] = len(strokes)
                represented_sources.update(source_refs)
                strokes.append(
                    PlotStroke(
                        layer="waterways",
                        points=_water_dot_ring(
                            x_mm,
                            y_mm,
                            diameter_mm=diameter_mm,
                        ),
                        osm_type=source.osm_type,
                        osm_id=source.osm_id,
                        part=f"water-dot:{len(strokes)}",
                        tags={
                            "compiled": preset,
                            "source-count": str(len(source_refs)),
                            "source-refs": serialized_source_refs,
                            "water-pattern": "stipple-circle",
                            "water-dot-spacing-mm": f"{spacing_mm:g}",
                            "water-dot-diameter-mm": f"{diameter_mm:g}",
                        },
                        name=source.name,
                        smooth=False,
                    )
                )
    candidate_dot_count = len(strokes)
    dot_length_mm = sum(
        hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(
            _water_dot_ring(0.0, 0.0, diameter_mm=diameter_mm),
            _water_dot_ring(0.0, 0.0, diameter_mm=diameter_mm)[1:],
        )
    )
    field_area_mm2 = (right - left) * (bottom - top)
    ink_budget_mm2 = WATER_DOT_INK_BUDGET_FIELD_FRACTION * field_area_mm2
    maximum_dot_count = max(1, floor(ink_budget_mm2 / (dot_length_mm * nib_mm)))
    if candidate_dot_count > maximum_dot_count:
        first_index_by_source: dict[str, int] = {}
        for index, stroke in enumerate(strokes):
            for source_ref in stroke.tags.get("source-refs", "").split(";"):
                if source_ref:
                    first_index_by_source.setdefault(source_ref, index)
        reserved_indices = sorted(set(first_index_by_source.values()))
        if len(reserved_indices) > maximum_dot_count:
            selected_indices = {
                reserved_indices[
                    floor(index * len(reserved_indices) / maximum_dot_count)
                ]
                for index in range(maximum_dot_count)
            }
        else:
            selected_indices = set(reserved_indices)
            remaining_indices = [
                index
                for index in range(candidate_dot_count)
                if index not in selected_indices
            ]
            remaining_slots = maximum_dot_count - len(selected_indices)
            selected_indices.update(
                remaining_indices[
                    floor(index * len(remaining_indices) / remaining_slots)
                ]
                for index in range(remaining_slots)
            )
        strokes = [strokes[index] for index in sorted(selected_indices)]
    selected_ink_area_mm2 = len(strokes) * dot_length_mm * nib_mm
    selected_sources = {
        source_ref
        for stroke in strokes
        for source_ref in stroke.tags.get("source-refs", "").split(";")
        if source_ref
    }
    return strokes, {
        "enabled": True,
        "method": "staggered plotter-safe micro-circles clipped inside water polygons",
        "physical_layer": "waterways",
        "spacing_mm": spacing_mm,
        "requested_diameter_mm": requested_diameter_mm,
        "diameter_mm": diameter_mm,
        "physical_nib_mm": nib_mm,
        "vertices_per_dot": WATER_DOT_VERTICES,
        "dot_path_count": len(strokes),
        "candidate_dot_path_count": candidate_dot_count,
        "omitted_budget_dot_path_count": candidate_dot_count - len(strokes),
        "ink_budget_field_fraction": WATER_DOT_INK_BUDGET_FIELD_FRACTION,
        "ink_budget_mm2": round(ink_budget_mm2, 3),
        "dot_path_length_mm": round(dot_length_mm, 6),
        "selected_ink_area_mm2": round(selected_ink_area_mm2, 3),
        "eligible_inset_area_mm2": round(eligible_area_mm2, 3),
        "candidate_represented_source_count": len(represented_sources),
        "represented_source_count": len(selected_sources),
    }


def _is_closed_semantic_bay_surface(
    feature: MapFeature,
    projected_area: BaseGeometry,
) -> bool:
    """Identify a polygonal bay extent that is not itself a physical bank."""

    if (
        feature.layer != "water_areas"
        or feature.tags.get("natural", "").strip().casefold() != "bay"
        or projected_area.is_empty
        or len(feature.points) < 4
    ):
        return False
    return feature.points[0] == feature.points[-1]


def _semantic_bay_boundary_suppression(
    records: list[tuple[MapFeature, LineString]],
    *,
    surface_areas_by_ref: dict[str, list[BaseGeometry]],
    stipple_strokes: list[PlotStroke],
    enabled: bool,
) -> tuple[list[tuple[MapFeature, LineString]], dict[str, Any]]:
    """Suppress only semantic bay extents with a verified stipple carrier.

    A closed ``natural=bay`` polygon describes the extent of a named water
    surface.  Its ring often closes conceptually across the bay mouth and must
    not be plotted as a bank.  Real ``natural=coastline`` ways remain separate
    source records and are untouched.  If no selected stipple dot carries the
    bay source reference, retain its ring fail-closed rather than silently
    deleting the only physical representation of that source.
    """

    empty_sha = hashlib.sha256(b"[]").hexdigest()
    if not enabled and records:
        raise MapPlotterError(
            "Semantic bay-boundary candidates require dotted water."
        )

    dots_by_ref: dict[str, list[PlotStroke]] = defaultdict(list)
    for stroke in stipple_strokes:
        if stroke.tags.get("water-pattern") != "stipple-circle":
            continue
        for source_ref in sorted(_stroke_source_references(stroke)):
            dots_by_ref[source_ref].append(stroke)

    records_by_ref: dict[str, list[tuple[MapFeature, LineString]]] = defaultdict(list)
    for source, line in records:
        source_ref = _source_reference(source)
        surfaces = surface_areas_by_ref.get(source_ref, [])
        if (
            source.tags.get("natural", "").strip().casefold() != "bay"
            or len(source.points) < 4
            or source.points[0] != source.points[-1]
            or not surfaces
            or all(surface.is_empty for surface in surfaces)
        ):
            raise MapPlotterError(
                "Semantic bay-boundary suppression received an unverified "
                f"source: {source_ref}."
            )
        records_by_ref[source_ref].append((source, line))

    retained_records: list[tuple[MapFeature, LineString]] = []
    entries: list[dict[str, Any]] = []
    suppressed_refs: list[str] = []
    retained_refs: list[str] = []
    for source_ref, source_records in sorted(records_by_ref.items()):
        source = source_records[0][0]
        lines = [line for _source, line in source_records]
        surfaces = surface_areas_by_ref[source_ref]
        carrier_strokes = dots_by_ref.get(source_ref, [])
        carrier_lines = [LineString(stroke.points) for stroke in carrier_strokes]
        represented = bool(carrier_strokes)
        if represented:
            suppressed_refs.append(source_ref)
        else:
            retained_refs.append(source_ref)
            retained_records.extend(source_records)
        entries.append(
            {
                "source_ref": source_ref,
                "natural": "bay",
                "geometry_type": source.geometry_type,
                "ring_role": source.ring_role,
                "source_ring_closed": True,
                "disposition": (
                    "suppressed_as_stippled_semantic_surface"
                    if represented
                    else "retained_fail_closed_without_stipple_carrier"
                ),
                "candidate_path_count": len(lines),
                "candidate_length_mm": round(sum(line.length for line in lines), 9),
                "suppressed_path_count": len(lines) if represented else 0,
                "suppressed_length_mm": round(
                    sum(line.length for line in lines) if represented else 0.0,
                    9,
                ),
                "retained_path_count": 0 if represented else len(lines),
                "retained_length_mm": round(
                    0.0 if represented else sum(line.length for line in lines),
                    9,
                ),
                "projected_surface_area_mm2": round(
                    sum(surface.area for surface in surfaces), 9
                ),
                "boundary_geometry_sha256": _line_geometry_sha256(lines),
                "surface_geometry_sha256": _polygon_geometry_sha256(
                    polygon
                    for surface in surfaces
                    for polygon in _polygon_parts(surface)
                ),
                "stipple_carrier_path_count": len(carrier_strokes),
                "stipple_carrier_geometry_sha256": (
                    _line_geometry_sha256(carrier_lines)
                    if carrier_lines
                    else empty_sha
                ),
                "source_lineage_carrier_verified": represented,
            }
        )

    suppressed_records = [
        record
        for source_ref, source_records in sorted(records_by_ref.items())
        if source_ref in set(suppressed_refs)
        for record in source_records
    ]
    diagnostics = {
        "schema_version": 1,
        "policy": "dotted-closed-natural-bay-semantic-surface-v1",
        "enabled": enabled,
        "fail_closed_without_stipple_carrier": True,
        "candidate_source_ref_count": len(records_by_ref),
        "candidate_source_refs": sorted(records_by_ref),
        "candidate_path_count": len(records),
        "candidate_length_mm": round(sum(line.length for _source, line in records), 9),
        "suppressed_source_ref_count": len(suppressed_refs),
        "suppressed_source_refs": suppressed_refs,
        "suppressed_path_count": len(suppressed_records),
        "suppressed_length_mm": round(
            sum(line.length for _source, line in suppressed_records), 9
        ),
        "retained_without_stipple_source_ref_count": len(retained_refs),
        "retained_without_stipple_source_refs": retained_refs,
        "retained_path_count": len(retained_records),
        "retained_length_mm": round(
            sum(line.length for _source, line in retained_records), 9
        ),
        "all_suppressed_sources_have_stipple_carriers": all(
            entry["source_lineage_carrier_verified"]
            for entry in entries
            if entry["suppressed_path_count"]
        ),
        "entries": entries,
        "ledger_sha256": hashlib.sha256(
            json.dumps(
                entries,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest(),
    }
    return retained_records, diagnostics


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


def _point_parts(geometry: BaseGeometry) -> Iterable[Point]:
    if geometry.is_empty:
        return
    if isinstance(geometry, Point):
        yield geometry
        return
    if isinstance(geometry, MultiPoint):
        yield from geometry.geoms
        return
    if isinstance(geometry, LineString):
        if not geometry.is_ring:
            yield from _point_parts(geometry.boundary)
        return
    if isinstance(geometry, GeometryCollection):
        for child in geometry.geoms:
            yield from _point_parts(child)


def _polygon_parts(geometry: BaseGeometry) -> Iterable[Polygon]:
    """Yield polygonal members without treating holes as independent water."""

    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        if geometry.area > 1e-6:
            yield geometry
        return
    if hasattr(geometry, "geoms"):
        for child in geometry.geoms:
            yield from _polygon_parts(child)


def _quantized_line(points: list[tuple[float, float]]) -> LineString | None:
    quantized: list[tuple[float, float]] = []
    for x, y in points:
        point = (round(x, 4), round(y, 4))
        if not quantized or point != quantized[-1]:
            quantized.append(point)
    if len(quantized) < 2:
        return None
    line = LineString(quantized)
    return line if line.length > 1e-6 else None


def _canonical_polygon_payload(polygon: Polygon) -> dict[str, Any]:
    """Return a direction/start-vertex independent polygon ledger entry."""

    return {
        "exterior": _canonical_line_coordinates(LineString(polygon.exterior.coords)),
        "interiors": sorted(
            _canonical_line_coordinates(LineString(ring.coords))
            for ring in polygon.interiors
        ),
    }


def _polygon_geometry_sha256(polygons: Iterable[Polygon]) -> str:
    payload = sorted(
        (_canonical_polygon_payload(polygon) for polygon in polygons),
        key=lambda item: json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ),
    )
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()


def _directed_projected_coastline_segments(
    records: list[tuple[MapFeature, LineString]],
    *,
    layout: Layout,
    clip_polygon: BaseGeometry,
) -> list[tuple[MapFeature, LineString]]:
    """Rebuild clipped coastline segments in their original OSM direction.

    GEOS is free to reverse a clipped or dissolved line. Water-side semantics
    cannot rely on that incidental ordering, so each consecutive source
    segment is projected and clipped independently, then explicitly aligned to
    its un-clipped source vector.
    """

    source_by_ref = {
        _source_reference(source): source for source, _line in records
    }
    directed: list[tuple[MapFeature, LineString]] = []
    for source_ref in sorted(source_by_ref):
        source = source_by_ref[source_ref]
        projected = _quantized_line(
            [
                layout.project_to_page(latitude, longitude)
                for latitude, longitude in source.points
            ]
        )
        if projected is None:
            continue
        coordinates = list(projected.coords)
        for first, second in zip(coordinates, coordinates[1:]):
            source_dx = second[0] - first[0]
            source_dy = second[1] - first[1]
            source_segment = LineString([first, second])
            for clipped in _line_parts(source_segment.intersection(clip_polygon)):
                clipped_coordinates = list(clipped.coords)
                clipped_dx = clipped_coordinates[-1][0] - clipped_coordinates[0][0]
                clipped_dy = clipped_coordinates[-1][1] - clipped_coordinates[0][1]
                if clipped_dx * source_dx + clipped_dy * source_dy < 0:
                    clipped = LineString(list(reversed(clipped_coordinates)))
                directed.append((source, clipped))
    return sorted(
        directed,
        key=lambda item: (
            _source_reference(item[0]),
            tuple((round(x, 6), round(y, 6)) for x, y in item[1].coords),
        ),
    )


def _coastline_water_surface_records(
    records: list[tuple[MapFeature, LineString]],
    *,
    layout: Layout,
    clip_rect: tuple[float, float, float, float],
    enabled: bool,
) -> tuple[list[tuple[MapFeature, BaseGeometry]], dict[str, Any]]:
    """Derive conservative water cells from directed OSM coastline ways.

    OpenStreetMap coastline direction places water on the geographic right.
    Page projection reverses the Y axis, so the same water lies on the
    page-space *left* of each directed coastline. The crop boundary closes the
    linework for polygonization. Any incomplete or contradictory topology
    disables the entire derived mask; explicit ``water_areas`` remain usable.
    """

    empty_sha = hashlib.sha256(b"[]").hexdigest()
    sorted_records = sorted(
        records,
        key=lambda item: (
            _source_reference(item[0]),
            _canonical_line_coordinates(item[1]),
        ),
    )
    source_by_ref = {
        _source_reference(source): source for source, _line in sorted_records
    }
    input_refs = sorted(source_by_ref)
    diagnostics: dict[str, Any] = {
        "schema_version": 1,
        "policy": "directed-osm-coastline-crop-cells-v1",
        "enabled": enabled,
        "attempted": False,
        "fail_closed": True,
        "topology_valid": False,
        "osm_water_side": "right",
        "page_water_side": "left",
        "projection_y_inverted": True,
        "crop_boundary_used": True,
        "input_path_count": len(sorted_records),
        "input_source_ref_count": len(input_refs),
        "input_source_refs": input_refs,
        "input_length_mm": round(
            sum(line.length for _source, line in sorted_records), 9
        ),
        "directed_segment_count": 0,
        "noded_edge_count": 0,
        "graph_node_count": 0,
        "nonmanifold_node_count": 0,
        "nonmanifold_nodes": [],
        "cut_path_count": 0,
        "dangle_path_count": 0,
        "invalid_ring_path_count": 0,
        "coastline_crop_overlap_length_mm": 0.0,
        "unresolved_directed_segment_count": 0,
        "unresolved_source_refs": [],
        "face_count": 0,
        "water_face_count": 0,
        "land_face_count": 0,
        "conflicting_face_count": 0,
        "unclassified_face_count": 0,
        "derived_surface_count": 0,
        "crop_area_mm2": 0.0,
        "partition_area_mm2": 0.0,
        "partition_error_mm2": 0.0,
        "classified_water_area_mm2": 0.0,
        "classified_land_area_mm2": 0.0,
        "derived_water_area_mm2": 0.0,
        "represented_source_refs": [],
        "unrepresented_source_refs": input_refs,
        "failure_reasons": [],
        "faces": [],
        "directed_geometry_sha256": empty_sha,
        "mask_geometry_sha256": empty_sha,
        "ledger_sha256": empty_sha,
    }
    if not enabled or not sorted_records:
        return [], diagnostics

    diagnostics["attempted"] = True
    clip_polygon = box(*clip_rect)
    crop_area = clip_polygon.area
    diagnostics["crop_area_mm2"] = round(crop_area, 9)
    directed_segments = _directed_projected_coastline_segments(
        sorted_records,
        layout=layout,
        clip_polygon=clip_polygon,
    )
    diagnostics["directed_segment_count"] = len(directed_segments)
    diagnostics["directed_geometry_sha256"] = hashlib.sha256(
        json.dumps(
            [
                {
                    "source_ref": _source_reference(source),
                    "coordinates": [
                        [round(float(x), 6), round(float(y), 6)]
                        for x, y in line.coords
                    ],
                }
                for source, line in directed_segments
            ],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    if not directed_segments:
        diagnostics["failure_reasons"] = ["no-directed-segments-inside-crop"]
        return [], diagnostics

    unique_coastline = unary_union([line for _source, line in directed_segments])
    noded_coastline_edges = [
        LineString([first, second])
        for line in _line_parts(unique_coastline)
        for first, second in zip(line.coords, list(line.coords)[1:])
        if LineString([first, second]).length > 1e-6
    ]
    diagnostics["noded_edge_count"] = len(noded_coastline_edges)

    node_degrees: dict[tuple[float, float], int] = defaultdict(int)
    for edge in noded_coastline_edges:
        first, second = edge.coords[0], edge.coords[-1]
        node_degrees[(round(first[0], 6), round(first[1], 6))] += 1
        node_degrees[(round(second[0], 6), round(second[1], 6))] += 1
    nonmanifold_nodes: list[dict[str, Any]] = []
    for (x_mm, y_mm), degree in sorted(node_degrees.items()):
        on_crop = clip_polygon.boundary.distance(Point(x_mm, y_mm)) <= 1e-5
        valid_degree = degree in ({1, 2} if on_crop else {2})
        if not valid_degree:
            nonmanifold_nodes.append(
                {
                    "x_mm": x_mm,
                    "y_mm": y_mm,
                    "degree": degree,
                    "on_crop_boundary": on_crop,
                }
            )
    diagnostics["graph_node_count"] = len(node_degrees)
    diagnostics["nonmanifold_node_count"] = len(nonmanifold_nodes)
    diagnostics["nonmanifold_nodes"] = nonmanifold_nodes

    noded_linework = unary_union([clip_polygon.boundary, unique_coastline])
    polygons, cuts, dangles, invalid_rings = polygonize_full(noded_linework)
    cut_parts = list(_line_parts(cuts))
    dangle_parts = list(_line_parts(dangles))
    invalid_parts = list(_line_parts(invalid_rings))
    diagnostics["cut_path_count"] = len(cut_parts)
    diagnostics["dangle_path_count"] = len(dangle_parts)
    diagnostics["invalid_ring_path_count"] = len(invalid_parts)

    cells = sorted(
        (
            orient(cell, sign=1.0)
            for cell in _polygon_parts(polygons)
            if clip_polygon.covers(cell.representative_point())
        ),
        key=lambda cell: json.dumps(
            _canonical_polygon_payload(cell),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ),
    )
    partition_area = sum(cell.area for cell in cells)
    partition_error = abs(crop_area - partition_area)
    partition_tolerance = max(1e-5, crop_area * 1e-9)
    diagnostics["face_count"] = len(cells)
    diagnostics["partition_area_mm2"] = round(partition_area, 9)
    diagnostics["partition_error_mm2"] = round(partition_error, 9)

    water_votes: list[set[str]] = [set() for _cell in cells]
    land_votes: list[set[str]] = [set() for _cell in cells]
    face_edge_ids: list[set[str]] = [set() for _cell in cells]
    directed_segment_tree = STRtree(
        [line for _source, line in directed_segments]
    )
    matched_directed_segment_indexes: set[int] = set()
    unresolved_segment_indexes: set[int] = set()

    # ``orient(sign=1)`` makes exterior rings counter-clockwise and holes
    # clockwise, which places polygon material on the left of every ring edge.
    # Page projection reverses geographic Y, therefore OSM water-right also
    # occupies page-space left: a SAME-direction coastline/ring edge is a
    # water vote and the reverse direction is a land vote. This exact edge
    # comparison avoids tolerance probes that could cross a narrow estuary.
    for cell_index, cell in enumerate(cells):
        rings = [cell.exterior, *cell.interiors]
        for ring in rings:
            coordinates = list(ring.coords)
            for first, second in zip(coordinates, coordinates[1:]):
                face_edge = LineString([first, second])
                if face_edge.length <= 1e-6:
                    continue
                contributor_indexes: list[int] = []
                for candidate_value in directed_segment_tree.query(
                    face_edge.buffer(1e-7, cap_style="flat", join_style="mitre")
                ):
                    candidate_index = int(candidate_value)
                    _candidate_source, candidate = directed_segments[candidate_index]
                    if face_edge.intersection(candidate).length > 1e-6:
                        contributor_indexes.append(candidate_index)
                if not contributor_indexes:
                    continue

                edge_source_refs: set[str] = set()
                face_dx = second[0] - first[0]
                face_dy = second[1] - first[1]
                for candidate_index in sorted(set(contributor_indexes)):
                    source, candidate = directed_segments[candidate_index]
                    source_ref = _source_reference(source)
                    candidate_dx = candidate.coords[-1][0] - candidate.coords[0][0]
                    candidate_dy = candidate.coords[-1][1] - candidate.coords[0][1]
                    alignment = face_dx * candidate_dx + face_dy * candidate_dy
                    if abs(alignment) <= 1e-12:
                        unresolved_segment_indexes.add(candidate_index)
                        continue
                    matched_directed_segment_indexes.add(candidate_index)
                    edge_source_refs.add(source_ref)
                    if alignment > 0:
                        water_votes[cell_index].add(source_ref)
                    else:
                        land_votes[cell_index].add(source_ref)
                if edge_source_refs:
                    edge_payload = {
                        "coordinates": _canonical_line_coordinates(face_edge),
                        "source_refs": sorted(edge_source_refs),
                    }
                    edge_sha = hashlib.sha256(
                        json.dumps(
                            edge_payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ).encode("ascii")
                    ).hexdigest()
                    face_edge_ids[cell_index].add(f"coast-edge-{edge_sha[:16]}")

    unresolved_segment_indexes.update(
        set(range(len(directed_segments))) - matched_directed_segment_indexes
    )
    unresolved_segment_refs = {
        _source_reference(directed_segments[index][0])
        for index in unresolved_segment_indexes
    }
    diagnostics["unresolved_directed_segment_count"] = len(
        unresolved_segment_indexes
    )
    diagnostics["unresolved_source_refs"] = sorted(unresolved_segment_refs)
    coastline_crop_overlap_length = unique_coastline.intersection(
        clip_polygon.boundary
    ).length
    diagnostics["coastline_crop_overlap_length_mm"] = round(
        coastline_crop_overlap_length, 9
    )

    face_ledger: list[dict[str, Any]] = []
    face_classes: list[str] = []
    water_face_indexes: list[int] = []
    represented_refs: set[str] = set()
    for index, cell in enumerate(cells):
        cell_water_refs = sorted(water_votes[index])
        cell_land_refs = sorted(land_votes[index])
        if cell_water_refs and not cell_land_refs:
            classification = "water"
            water_face_indexes.append(index)
            represented_refs.update(cell_water_refs)
        elif cell_land_refs and not cell_water_refs:
            classification = "land"
        elif cell_water_refs and cell_land_refs:
            classification = "conflict"
        else:
            classification = "unclassified"
        face_classes.append(classification)
        geometry_sha = _polygon_geometry_sha256([cell])
        face_ledger.append(
            {
                "face_id": f"coast-face-{geometry_sha[:16]}",
                "classification": classification,
                "area_mm2": round(cell.area, 9),
                "water_source_refs": cell_water_refs,
                "land_source_refs": cell_land_refs,
                "coastline_edge_ids": sorted(face_edge_ids[index]),
                "geometry_sha256": geometry_sha,
            }
        )

    water_face_count = face_classes.count("water")
    land_face_count = face_classes.count("land")
    conflicting_face_count = face_classes.count("conflict")
    unclassified_face_count = face_classes.count("unclassified")
    diagnostics.update(
        {
            "water_face_count": water_face_count,
            "land_face_count": land_face_count,
            "conflicting_face_count": conflicting_face_count,
            "unclassified_face_count": unclassified_face_count,
            "classified_water_area_mm2": round(
                sum(cells[index].area for index in water_face_indexes), 9
            ),
            "classified_land_area_mm2": round(
                sum(
                    cell.area
                    for cell, classification in zip(cells, face_classes)
                    if classification == "land"
                ),
                9,
            ),
            "faces": face_ledger,
            "ledger_sha256": hashlib.sha256(
                json.dumps(
                    face_ledger,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
        }
    )

    failure_reasons: list[str] = []
    if cut_parts:
        failure_reasons.append("polygonize-cut-edges")
    if dangle_parts:
        failure_reasons.append("polygonize-dangles")
    if invalid_parts:
        failure_reasons.append("polygonize-invalid-rings")
    if nonmanifold_nodes:
        failure_reasons.append("nonmanifold-coastline-graph")
    if unresolved_segment_refs:
        failure_reasons.append("unresolved-directed-side")
    if coastline_crop_overlap_length > 1e-6:
        failure_reasons.append("coastline-overlaps-crop-boundary")
    if conflicting_face_count:
        failure_reasons.append("conflicting-water-land-votes")
    if unclassified_face_count:
        failure_reasons.append("unclassified-crop-faces")
    if not cells or partition_error > partition_tolerance:
        failure_reasons.append("incomplete-crop-partition")
    if not water_face_indexes:
        failure_reasons.append("no-proved-water-face")
    diagnostics["failure_reasons"] = failure_reasons
    diagnostics["topology_valid"] = not failure_reasons
    if failure_reasons:
        return [], diagnostics

    output: list[tuple[MapFeature, BaseGeometry]] = []
    for index in water_face_indexes:
        source_refs = sorted(water_votes[index])
        representative = source_by_ref[source_refs[0]]
        face = face_ledger[index]
        synthetic_source = replace(
            representative,
            tags={
                **representative.tags,
                "mapplot:surface-source-refs": ";".join(source_refs),
                "mapplot:synthetic-water-surface": (
                    "directed-osm-coastline-crop-cells-v1"
                ),
                "mapplot:coastline-face-id": str(face["face_id"]),
                "mapplot:coastline-face-geometry-sha256": str(
                    face["geometry_sha256"]
                ),
            },
        )
        output.append((synthetic_source, cells[index]))

    water_polygons = [cells[index] for index in water_face_indexes]
    diagnostics["derived_surface_count"] = len(output)
    diagnostics["derived_water_area_mm2"] = round(
        sum(polygon.area for polygon in water_polygons), 9
    )
    diagnostics["represented_source_refs"] = sorted(represented_refs)
    diagnostics["unrepresented_source_refs"] = sorted(
        set(input_refs) - represented_refs
    )
    diagnostics["mask_geometry_sha256"] = _polygon_geometry_sha256(water_polygons)
    return output, diagnostics


def _project_feature(
    feature: MapFeature,
    layout: Layout,
    clip_box: BaseGeometry,
    *,
    preserve_exact_boundary_slivers: bool = False,
) -> list[LineString]:
    line = _quantized_line(
        [
            layout.project_to_page(latitude, longitude)
            for latitude, longitude in feature.points
        ]
    )
    parts = [] if line is None else list(_line_parts(line.intersection(clip_box)))
    if parts or not preserve_exact_boundary_slivers:
        return parts
    return _project_exact_boundary_sliver(feature, layout, clip_box)


def _project_exact_boundary_sliver(
    feature: MapFeature,
    layout: Layout,
    clip_box: BaseGeometry,
) -> list[LineString]:
    """Recover only sub-floor crop slivers erased by page quantization.

    Road topology and normal clipping deliberately use four-decimal page
    coordinates.  A source segment can cross the crop by less than half that
    grid and therefore have a real lineal intersection before quantization but
    none afterwards.  Preserve only intersections that touch the crop boundary
    and are below the compiler's absolute 0.5 mm floor.  The physical compiler
    must then measure and ledger the omission; this is not a general waiver for
    projection or clipping collapse.
    """

    points: list[tuple[float, float]] = []
    for latitude, longitude in feature.points:
        point = layout.project_to_page(latitude, longitude)
        if not points or point != points[-1]:
            points.append(point)
    if len(points) < 2:
        return []
    line = LineString(points)
    if line.length <= 1e-6 or not line.intersects(clip_box.boundary):
        return []
    parts = list(_line_parts(line.intersection(clip_box)))
    if not parts or sum(part.length for part in parts) >= 0.5:
        return []
    return parts


def _project_feature_area(
    feature: MapFeature,
    layout: Layout,
    clip_box: BaseGeometry,
) -> BaseGeometry:
    """Return the clipped page-space area for genuinely closed OSM outlines."""

    if feature.layer not in AREA_OUTLINE_LAYERS:
        return GeometryCollection()
    line = _quantized_line(
        [
            layout.project_to_page(latitude, longitude)
            for latitude, longitude in feature.points
        ]
    )
    if line is None or not line.is_ring or len(line.coords) < 4:
        return GeometryCollection()
    polygon: BaseGeometry = Polygon(line)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon.intersection(clip_box)


def _project_feature_areas(
    features: list[MapFeature],
    layout: Layout,
    clip_box: BaseGeometry,
) -> dict[int, BaseGeometry]:
    """Project area rings as real polygons with associated interior holes.

    PBF/libosmium and Overpass ingestion expose the same ``ring_role`` and
    ``outer_ring_part`` fields.  Constructing the polygon before clipping keeps
    holes and crop-edge topology intact; treating every inner ring as an
    independent polygon would incorrectly turn islands into water/park area.
    """

    empty = GeometryCollection()
    result: dict[int, BaseGeometry] = {id(feature): empty for feature in features}
    ring_groups: dict[tuple[str, str, str], list[MapFeature]] = defaultdict(list)
    for feature in features:
        if (
            feature.layer in AREA_OUTLINE_LAYERS
            and feature.geometry_type == "polygon_ring"
            and feature.ring_role in {"outer", "inner"}
        ):
            ring_groups[(feature.layer, feature.osm_type, feature.osm_id)].append(
                feature
            )
        else:
            result[id(feature)] = _project_feature_area(feature, layout, clip_box)

    for group in ring_groups.values():
        projected_rings: dict[int, LineString] = {}
        for feature in group:
            line = _quantized_line(
                [
                    layout.project_to_page(latitude, longitude)
                    for latitude, longitude in feature.points
                ]
            )
            if line is not None and line.is_ring and len(line.coords) >= 4:
                projected_rings[id(feature)] = line

        outers = [
            feature
            for feature in group
            if feature.ring_role == "outer" and id(feature) in projected_rings
        ]
        inners = [
            feature
            for feature in group
            if feature.ring_role == "inner" and id(feature) in projected_rings
        ]
        outer_by_part = {feature.part: feature for feature in outers}
        assigned: dict[int, list[LineString]] = defaultdict(list)
        for inner in inners:
            outer = (
                outer_by_part.get(inner.outer_ring_part)
                if inner.outer_ring_part is not None
                else None
            )
            if outer is None:
                inner_point = Point(projected_rings[id(inner)].coords[0])
                containers = [
                    (Polygon(projected_rings[id(candidate)]).area, candidate)
                    for candidate in outers
                    if Polygon(projected_rings[id(candidate)]).covers(inner_point)
                ]
                outer = (
                    min(containers, key=lambda item: item[0])[1] if containers else None
                )
            if outer is not None:
                assigned[id(outer)].append(projected_rings[id(inner)])

        for outer in outers:
            shell = projected_rings[id(outer)]
            try:
                polygon: BaseGeometry = Polygon(
                    shell,
                    holes=[list(hole.coords) for hole in assigned[id(outer)]],
                )
            except (TypeError, ValueError):
                polygon = Polygon(shell)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            result[id(outer)] = polygon.intersection(clip_box)
            # Inner rings remain drawable outlines, but deliberately have no
            # standalone filled area in selection or river-mask calculations.
            for inner in inners:
                if inner.outer_ring_part == outer.part:
                    result[id(inner)] = empty
    return result


def _merge_lines(lines: list[LineString]) -> list[LineString]:
    """Join exact endpoints without planar-noding every crossing.

    ``unary_union`` splits innocent roads wherever geometries cross.  That is
    useful for topology analysis but creates needless pen lifts and jagged
    fragments in a drawing compiler.  Exact duplicate removal plus linemerge
    retains the supplied vertices and joins only compatible endpoints.
    """

    if not lines:
        return []
    unique: list[LineString] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for line in lines:
        coordinates = tuple((float(x), float(y)) for x, y in line.coords)
        canonical = min(coordinates, tuple(reversed(coordinates)))
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append(line)
    if len(unique) == 1:
        return unique
    try:
        merged = linemerge(MultiLineString(unique))
    except (TypeError, ValueError):
        merged = MultiLineString(unique)
    return list(_line_parts(merged))


def _canonical_line_coordinates(
    line: LineString,
) -> tuple[tuple[float, float], ...]:
    """Canonicalize an outline across direction and closed-ring start vertex."""

    coordinates = tuple(
        (round(float(x), 6), round(float(y), 6)) for x, y in line.coords
    )
    if len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
        ring = coordinates[:-1]
        variants: list[tuple[tuple[float, float], ...]] = []
        for candidate in (ring, tuple(reversed(ring))):
            for index in range(len(candidate)):
                rotated = candidate[index:] + candidate[:index]
                variants.append((*rotated, rotated[0]))
        return min(variants)
    return min(coordinates, tuple(reversed(coordinates)))


def _landmark_geometry_signature(
    records: list[tuple[MapFeature, LineString]],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    # Relation/member duplicates commonly share an exterior while only the
    # relation carries courtyard or seating-bowl holes.  Including inner rings
    # in the identity would therefore fail to recognise the duplicate outer
    # and plot it twice.  Keep inner geometry on the preferred representative,
    # but identify coincident objects by their complete exterior ring set.
    exterior_signatures = {
        _canonical_line_coordinates(line)
        for source, line in records
        if source.ring_role != "inner"
    }
    if not exterior_signatures:
        exterior_signatures = {
            _canonical_line_coordinates(line) for _source, line in records
        }
    return tuple(sorted(exterior_signatures))


def _landmark_source_refs(feature: MapFeature) -> tuple[str, ...]:
    serialized = feature.tags.get("mapplot:landmark-source-refs", "")
    return (
        tuple(item for item in serialized.split(";") if item)
        if serialized
        else (_source_reference(feature),)
    )


def _simplify(
    geometry: BaseGeometry,
    tolerance_mm: float,
    *,
    preserve_topology: bool = True,
) -> BaseGeometry:
    """Simplify only when the caller explicitly requested a positive tolerance.

    GEOS is allowed to remove redundant vertices even for a zero tolerance.  A
    faithful ``--simplify-mm 0`` therefore has to bypass the operation rather
    than call ``simplify(0)``.
    """

    if tolerance_mm <= 0:
        return geometry
    return geometry.simplify(tolerance_mm, preserve_topology=preserve_topology)


def road_rank(tags: dict[str, str]) -> int:
    """Return a stable physical hierarchy rank for a highway feature."""

    highway = effective_highway_value(tags)
    base = highway.removesuffix("_link")
    ranks = {
        "motorway": 6,
        "trunk": 6,
        "primary": 5,
        "secondary": 4,
        "tertiary": 4,
        "residential": 3,
        "living_street": 2,
        "unclassified": 3,
        "road": 2,
        "service": 2,
        "pedestrian": 2,
        "track": 1,
        "cycleway": 1,
        "bridleway": 1,
        "path": 1,
        "footway": 1,
        "steps": 1,
    }.get(base, 1)
    if highway.endswith("_link"):
        ranks = max(1, ranks - 1)

    lanes = [
        int(value)
        for value in re.findall(r"\d+", tags.get("lanes", ""))
        if int(value) > 0
    ]
    width_text = tags.get("width", "")
    width_match = re.search(r"\d+(?:[.,]\d+)?", width_text)
    width_m = (
        float(width_match.group().replace(",", "."))
        if width_match is not None
        else None
    )
    if width_m is not None and "'" in width_text:
        width_m *= 0.3048
    lane_count = max(lanes, default=0)
    if (width_m is not None and width_m >= 20) or lane_count >= 6:
        ranks = max(ranks, 6)
    elif (width_m is not None and width_m >= 13) or lane_count >= 4:
        ranks = max(ranks, 5)
    return ranks


def _poster_road_target(
    feature: MapFeature, length_mm: float, *, balanced: bool
) -> str | None:
    highway = feature.tags.get("highway", "")
    access = feature.tags.get("access", "")
    if access in {"private", "no"}:
        return None
    if not balanced:
        if feature.layer == "paths":
            return (
                "paths"
                if highway == "pedestrian"
                and bool(feature.name)
                and feature.tags.get("bridge") in {None, "no"}
                and feature.tags.get("tunnel") in {None, "no"}
                and length_mm >= 1.2
                else None
            )
        if highway == "service":
            return (
                "roads_local"
                if bool(feature.name)
                and feature.tags.get("service")
                not in {"alley", "driveway", "parking_aisle", "drive-through"}
                and length_mm >= 2.0
                else None
            )
        if feature.layer == "roads_other" and length_mm < 1.2:
            return None
        return feature.layer

    if feature.layer == "paths":
        subtype = feature.tags.get("footway", "")
        if highway == "pedestrian":
            return "roads_local" if (bool(feature.name) or length_mm >= 3.0) else None
        if highway == "footway":
            if subtype in {"sidewalk", "crossing"}:
                return None
            if feature.tags.get("bridge") not in {None, "no"}:
                return "paths" if length_mm >= 1.0 else None
            if feature.name and length_mm >= 1.2:
                return "paths"
            return "paths" if length_mm >= 6.0 else None
        if highway == "cycleway":
            if feature.name and length_mm >= 1.0:
                return "paths"
            return "paths" if length_mm >= 4.0 else None
        if highway in {"path", "track", "bridleway"}:
            if feature.name and length_mm >= 1.5:
                return "paths"
            return "paths" if length_mm >= 6.0 else None
        return None

    if highway == "service":
        service = feature.tags.get("service", "")
        if service in {"driveway", "parking_aisle", "drive-through"}:
            return None
        if feature.name and length_mm >= 1.0:
            return "roads_other"
        if not service and length_mm >= 4.0:
            return "roads_other"
        return None
    if feature.layer == "roads_other" and length_mm < 1.0:
        return None
    return feature.layer


def _parallel_path_is_redundant(
    lines: list[LineString],
    road_proximity: BaseGeometry,
    bridge_proximity: BaseGeometry,
    *,
    is_bridge: bool,
) -> bool:
    if not lines or road_proximity.is_empty:
        return False
    candidate = unary_union(lines)
    length = candidate.length
    if length <= 1e-6:
        return True
    parallel_ratio = candidate.intersection(road_proximity).length / length
    if parallel_ratio >= 0.80:
        return True
    if is_bridge and not bridge_proximity.is_empty:
        bridge_ratio = candidate.intersection(bridge_proximity).length / length
        return bridge_ratio >= 0.65
    return False


def _retain_poster_context(
    feature: MapFeature,
    parts: list[LineString],
    *,
    balanced: bool,
    full: bool,
    clipped_area_mm2: float = 0.0,
) -> bool:
    if full:
        return True
    length_mm = sum(part.length for part in parts)
    if feature.layer == "railways":
        railway = feature.tags.get("railway", "")
        if railway not in {"rail", "light_rail", "tram", "subway"}:
            return False
        if railway == "rail":
            usage = feature.tags.get("usage", "")
            service = feature.tags.get("service", "")
            return usage in {"", "main", "branch"} and service not in {
                "yard",
                "siding",
                "spur",
            }
        service = feature.tags.get("service", "")
        return service not in {"yard", "siding", "spur"} or (
            balanced and length_mm >= 8.0
        )
    if feature.layer == "green_space":
        area_mm2 = clipped_area_mm2
        for part in parts:
            if part.is_ring:
                area_mm2 = max(area_mm2, abs(Polygon(part).area))
        if area_mm2 <= 0 and parts:
            # Stop-gap for clipped relation fragments whose outer ring was not
            # assembled upstream.  It is deliberately conservative.
            area_mm2 = unary_union(parts).convex_hull.area * 0.5
        if feature.tags.get("leisure") in {"park", "nature_reserve"}:
            threshold = 6.0 if balanced else 12.0
            return bool(feature.name) or area_mm2 >= threshold or length_mm >= 10.0
        threshold = 20.0 if balanced else 35.0
        return area_mm2 >= threshold or (bool(feature.name) and length_mm >= 8.0)
    return True


def _detail_group_key(feature: MapFeature) -> tuple[str, str, str]:
    identity = (
        feature.name.casefold()
        if feature.name
        else f"{feature.osm_type}:{feature.osm_id}"
    )
    return (feature.layer, feature.tags.get("highway", ""), identity)


def _detail_category(feature: MapFeature) -> str | None:
    if feature.layer == "paths":
        return "path"
    if feature.tags.get("highway") == "service":
        return "service"
    if feature.layer == "roads_local":
        return "local"
    return None


def _balanced_detail_keys(
    projected: list[tuple[MapFeature, list[LineString]]], layout: Layout
) -> dict[str, set[tuple[str, str, str]]]:
    categories: dict[str, dict[tuple[str, str, str], dict[str, float | bool]]] = {
        "local": {},
        "service": {},
        "path": {},
    }
    centre = Point(
        layout.map_x_mm + layout.map_width_mm / 2,
        layout.map_y_mm + layout.map_height_mm / 2,
    )
    half_diagonal = hypot(layout.map_width_mm, layout.map_height_mm) / 2
    for feature, parts in projected:
        if feature.layer not in ROAD_LAYERS:
            continue
        length_mm = sum(part.length for part in parts)
        target = _poster_road_target(feature, length_mm, balanced=True)
        if target is None:
            continue
        if feature.layer == "paths":
            category = "path"
        elif feature.tags.get("highway") == "service":
            category = "service"
        elif feature.layer == "roads_local":
            category = "local"
        else:
            continue
        key = _detail_group_key(feature)
        record = categories[category].setdefault(
            key,
            {"length": 0.0, "distance": half_diagonal, "bridge": False},
        )
        record["length"] = float(record["length"]) + length_mm
        record["distance"] = min(
            float(record["distance"]),
            min(part.distance(centre) for part in parts),
        )
        record["bridge"] = bool(record["bridge"]) or feature.tags.get("bridge") not in {
            None,
            "no",
        }

    map_area_mm2 = layout.map_width_mm * layout.map_height_mm
    budget_density = {"local": 0.0715, "service": 0.0163, "path": 0.0358}
    budgets = {
        category: density * map_area_mm2 for category, density in budget_density.items()
    }
    minimums = {"local": 0.8, "service": 1.0, "path": 1.0}
    result: dict[str, set[tuple[str, str, str]]] = {}
    for category, records in categories.items():
        ranked: list[tuple[float, tuple[str, str, str], float]] = []
        for key, record in records.items():
            length = float(record["length"])
            if length < minimums[category]:
                continue
            centrality = 1.25 - 0.25 * min(
                float(record["distance"]) / max(half_diagonal, 1e-6), 1.0
            )
            named_bonus = 1.15 if not key[2].startswith(("way:", "relation:")) else 1.0
            type_bonus = 1.0
            if category == "path" and key[1] in {"cycleway", "pedestrian"}:
                type_bonus = 1.20
            bridge_bonus = 10.0 if bool(record["bridge"]) else 1.0
            ranked.append(
                (
                    length * centrality * named_bonus * type_bonus * bridge_bonus,
                    key,
                    length,
                )
            )
        retained: set[tuple[str, str, str]] = set()
        used = 0.0
        for _, key, length in sorted(ranked, reverse=True):
            if used + length > budgets[category] and retained:
                continue
            retained.add(key)
            used += length
        result[category] = retained
    return result


def _suppress_parallel_lines(
    lines: list[LineString], *, distance_mm: float, ratio: float
) -> list[LineString]:
    retained: list[LineString] = []
    proximity: BaseGeometry = GeometryCollection()
    for line in sorted(lines, key=lambda item: item.length, reverse=True):
        if (
            not proximity.is_empty
            and line.intersection(proximity).length / max(line.length, 1e-6) >= ratio
        ):
            continue
        retained.append(line)
        proximity = unary_union(retained).buffer(distance_mm)
    return retained


def _within_length_budget(
    lines: list[LineString], *, budget_mm: float, minimum_mm: float
) -> list[LineString]:
    retained: list[LineString] = []
    used = 0.0
    for line in sorted(lines, key=lambda item: item.length, reverse=True):
        if line.length < minimum_mm:
            continue
        if used + line.length > budget_mm and retained:
            continue
        retained.append(line)
        used += line.length
    return retained


def _cross_section_is_flanked(
    river: LineString,
    banks: BaseGeometry,
    distance: float,
) -> bool:
    delta = min(max(river.length * 0.015, 0.15), 0.7)
    before = river.interpolate(max(0.0, distance - delta))
    after = river.interpolate(min(river.length, distance + delta))
    dx = after.x - before.x
    dy = after.y - before.y
    magnitude = hypot(dx, dy)
    if magnitude <= 1e-9:
        return False
    normal_x = -dy / magnitude
    normal_y = dx / magnitude
    centre = river.interpolate(distance)
    # Size the probe from the data instead of assuming every river is 20 mm
    # wide on paper.  A little headroom is needed to reach the opposite bank.
    nearest_bank_mm = banks.distance(centre)
    half_width_mm = max(0.75, nearest_bank_mm * 2.25 + 0.25)
    cross_section = LineString(
        [
            (
                centre.x - normal_x * half_width_mm,
                centre.y - normal_y * half_width_mm,
            ),
            (
                centre.x + normal_x * half_width_mm,
                centre.y + normal_y * half_width_mm,
            ),
        ]
    )
    signed: list[float] = []
    for point in _point_parts(banks.intersection(cross_section)):
        signed.append((point.x - centre.x) * normal_x + (point.y - centre.y) * normal_y)
    return any(value < -0.2 for value in signed) and any(
        value > 0.2 for value in signed
    )


def _river_is_flanked(river: LineString, banks: BaseGeometry) -> bool:
    if river.length <= 1e-6 or banks.is_empty:
        return False
    sample_count = max(7, min(19, int(river.length / 7.0) + 1))
    covered = 0
    for index in range(1, sample_count + 1):
        distance = river.length * index / (sample_count + 1)
        if _cross_section_is_flanked(river, banks, distance):
            covered += 1
    return covered / sample_count >= 0.6


def _flanked_centerline_suppression(
    centreline: LineString,
    boundaries: BaseGeometry,
) -> tuple[list[LineString], float]:
    """Clip sustained, two-sided bank coverage without masking unbanked ends."""

    if centreline.length <= 1e-6 or boundaries.is_empty:
        return [centreline], 0.0
    interval_count = max(1, min(1024, ceil(centreline.length / 0.75)))
    interval_mm = centreline.length / interval_count
    flanked = [
        _cross_section_is_flanked(
            centreline,
            boundaries,
            interval_mm * (index + 0.5),
        )
        for index in range(interval_count)
    ]
    if not any(flanked):
        return [centreline], 0.0

    suppressed_ranges: list[tuple[float, float]] = []
    start: int | None = None
    for index, covered in enumerate((*flanked, False)):
        if covered and start is None:
            start = index
            continue
        if covered or start is None:
            continue
        run_count = index - start
        run_length = run_count * interval_mm
        # Reject isolated cross-section coincidences. A wholly flanked short
        # channel is still valid evidence, while partial runs need spatial depth.
        if (
            run_count >= 2
            and run_length >= 0.75
            or start == 0
            and index == interval_count
        ):
            suppressed_ranges.append(
                (start * interval_mm, min(centreline.length, index * interval_mm))
            )
        start = None
    if not suppressed_ranges:
        return [centreline], 0.0

    suppressed_lines = [
        cast(LineString, substring(centreline, start_mm, end_mm))
        for start_mm, end_mm in suppressed_ranges
        if end_mm - start_mm > 1e-6
    ]
    if not suppressed_lines:
        return [centreline], 0.0
    suppressed_mask = unary_union(suppressed_lines)
    visible = centreline.difference(suppressed_mask.buffer(1e-6, cap_style="flat"))
    visible_parts = list(_line_parts(visible))
    visible_length = sum(part.length for part in visible_parts)
    return visible_parts, max(0.0, centreline.length - visible_length)


def _truthy_osm_value(value: str | None) -> bool:
    return bool(value and value.strip().casefold() not in {"no", "false", "0"})


def _subterranean_waterway_reasons(tags: dict[str, str]) -> tuple[str, ...]:
    reasons: list[str] = []
    if _truthy_osm_value(tags.get("tunnel")):
        reasons.append("tunnel")
    if tags.get("covered", "").strip().casefold() == "yes":
        reasons.append("covered")
    if tags.get("location", "").strip().casefold() == "underground":
        reasons.append("underground_location")
    return tuple(reasons)


def _line_geometry_sha256(lines: Iterable[LineString]) -> str:
    payload: list[tuple[tuple[float, float], ...]] = []
    for line in lines:
        coordinates = tuple(
            (float(f"{x:.4f}"), float(f"{y:.4f}")) for x, y in line.coords
        )
        payload.append(min(coordinates, tuple(reversed(coordinates))))
    return hashlib.sha256(
        json.dumps(sorted(payload), separators=(",", ":"), ensure_ascii=True).encode(
            "ascii"
        )
    ).hexdigest()


def _river_is_inside_mapped_area(river: LineString, water_area: BaseGeometry) -> bool:
    if river.length <= 1e-6 or water_area.is_empty:
        return False
    # A hairline tolerance absorbs page-projection rounding without turning
    # nearby canals or roads into members of the polygon.
    covered = river.intersection(water_area.buffer(0.03)).length
    return covered / river.length >= 0.6


def _stroke_from_line(
    layer: str,
    line: LineString,
    *,
    part: str,
    smooth: bool,
    tags: dict[str, str] | None = None,
    osm_type: str = "compiled",
    osm_id: str = "multiple",
    name: str | None = None,
) -> PlotStroke:
    return PlotStroke(
        layer=layer,
        points=[(float(x), float(y)) for x, y in line.coords],
        part=part,
        tags=tags or {"compiled": "a5-poster"},
        osm_type=osm_type,
        osm_id=osm_id,
        name=name,
        smooth=smooth,
    )


def _source_reference(feature: MapFeature) -> str:
    return f"{feature.osm_type}/{feature.osm_id}/{feature.part}"


def _surface_area_source_refs(feature: MapFeature) -> tuple[str, ...]:
    """Return all real OSM sources represented by a mapped/derived surface."""

    serialized = feature.tags.get("mapplot:surface-source-refs", "")
    if not serialized:
        return (_source_reference(feature),)
    return tuple(sorted({item for item in serialized.split(";") if item}))


def _source_references(
    records: Iterable[MapFeature | tuple[MapFeature, object]],
) -> str:
    features = (
        record[0] if isinstance(record, tuple) else record for record in records
    )
    return ";".join(sorted({_source_reference(feature) for feature in features}))


def _serialized_topology_node(node_key: tuple[object, ...]) -> str:
    """Return a deterministic opaque identity for a protected road node.

    Trail assembly downstream must distinguish two OSM nodes that happen to
    project to the same page coordinate.  A string representation is sufficient
    because the value is compared only for exact identity; it is never parsed or
    exposed as source geometry.
    """

    return repr(node_key)


def _clipped_topology_endpoint(
    point: tuple[float, float],
    *,
    chain_points: tuple[tuple[float, float], ...],
    chain_start_node: str,
    chain_end_node: str,
    endpoint_role: str,
    clip_identity: str,
) -> str:
    """Keep real graph endpoints joinable and crop-created endpoints isolated."""

    if endpoint_role not in {"start", "end"}:
        raise MapPlotterError("A clipped topology endpoint role must be start or end.")
    quantized = (round(point[0], 4), round(point[1], 4))
    matches_start = quantized == (
        round(chain_points[0][0], 4),
        round(chain_points[0][1], 4),
    )
    matches_end = quantized == (
        round(chain_points[-1][0], 4),
        round(chain_points[-1][1], 4),
    )
    # An open OSM chain may be visually closed: distinct source nodes can
    # legitimately project to identical start/end coordinates. Preserve their
    # oriented identities instead of collapsing both ends to the first match.
    if matches_start and matches_end:
        return chain_start_node if endpoint_role == "start" else chain_end_node
    if matches_start:
        return chain_start_node
    if matches_end:
        return chain_end_node
    return f"clip:{clip_identity}"


@dataclass(frozen=True)
class _SourceLineIndex:
    """Spatial index that keeps merged-path attribution sub-quadratic."""

    records: tuple[tuple[MapFeature, LineString], ...]
    tree: STRtree

    @classmethod
    def build(
        cls, records: Iterable[tuple[MapFeature, LineString]]
    ) -> _SourceLineIndex:
        resolved = tuple(records)
        return cls(resolved, STRtree([line for _, line in resolved]))

    def contributors(self, line: LineString) -> list[MapFeature]:
        contributors: dict[str, MapFeature] = {}
        # The small envelope absorbs overlay ulps without associating a nearby
        # independent street at any physically meaningful plot scale.
        candidate_indexes = [
            int(index_value) for index_value in self.tree.query(line.buffer(1e-4))
        ]
        for index in candidate_indexes:
            source, candidate = self.records[index]
            if line.intersection(candidate).length > 1e-6:
                contributors[_source_reference(source)] = source

        # GEOS noding can place a split point a few ulps off its source segment.
        # Exact line intersection then reports only endpoint Points and zero
        # length even though the entire dissolved segment is source-backed.
        # Recover only candidates that cover the *whole* output line inside a
        # 0.1-nanometre paper-space envelope. A crossing or shared endpoint
        # cannot pass this full-coverage proof, so no provenance is inferred
        # from mere proximity. If nothing proves coverage the caller still
        # receives an empty result and fails closed.
        overlay_tolerance_mm = 1e-7
        maximum_uncovered_mm = 1e-6
        for index in candidate_indexes:
            source, candidate = self.records[index]
            source_ref = _source_reference(source)
            if (
                source_ref in contributors
                or candidate.distance(line) > overlay_tolerance_mm
            ):
                continue
            carrier = candidate.buffer(
                overlay_tolerance_mm,
                cap_style="flat",
                join_style="mitre",
            )
            if line.difference(carrier).length <= maximum_uncovered_mm:
                contributors[source_ref] = source
        return [contributors[key] for key in sorted(contributors)]


def _normalized_ink_balanced_water_boundaries(
    records: list[tuple[MapFeature, LineString]],
    *,
    preset: str,
    simplify_mm: float,
    policy: str = "ink-balanced-water-boundary-v2",
) -> tuple[list[PlotStroke], dict[str, Any], int, int]:
    """Dissolve only exact water-boundary overlap and preserve every source ref."""

    raw_length = sum(line.length for _source, line in records)
    input_refs = {_source_reference(source) for source, _line in records}
    if not records:
        return (
            [],
            {
                "schema_version": 2,
                "policy": policy,
                "enabled": True,
                "input_path_count": 0,
                "output_path_count": 0,
                "input_source_ref_count": 0,
                "represented_source_ref_count": 0,
                "unrepresented_source_refs": [],
                "raw_length_mm": 0.0,
                "unique_length_mm": 0.0,
                "overlap_removed_mm": 0.0,
                "estimated_ink_saved_mm2_at_0_4mm": 0.0,
                "geometry_sha256": hashlib.sha256(b"[]").hexdigest(),
            },
            0,
            0,
        )

    source_index = _SourceLineIndex.build(records)
    dissolved = unary_union([line for _source, line in records])
    unique_length = dissolved.length
    output: list[PlotStroke] = []
    represented_refs: set[str] = set()
    geometry_payload: list[tuple[tuple[float, float], ...]] = []
    river_bank_refs = {
        _source_reference(source)
        for source, _line in records
        if source.tags.get("water") == "river"
        or source.tags.get("waterway") == "riverbank"
    }
    other_water_refs = {
        _source_reference(source)
        for source, _line in records
        if _source_reference(source) not in river_bank_refs
        and source.tags.get("natural") != "coastline"
    }
    bank_output_count = 0
    other_water_output_count = 0

    for merged_index, merged in enumerate(_merge_lines(list(_line_parts(dissolved)))):
        contributors = source_index.contributors(merged)
        if not contributors:
            raise MapPlotterError(
                "Surface-water boundary normalization lost source attribution."
            )
        contributor_refs = {_source_reference(source) for source in contributors}
        represented_refs.update(contributor_refs)
        if contributor_refs & river_bank_refs:
            bank_output_count += 1
        if contributor_refs & other_water_refs:
            other_water_output_count += 1

        names = {source.name for source in contributors if source.name}
        osm_types = {source.osm_type for source in contributors}
        osm_ids = {source.osm_id for source in contributors}
        natural_values = {
            value for source in contributors if (value := source.tags.get("natural"))
        }
        water_values = {
            value for source in contributors if (value := source.tags.get("water"))
        }
        waterway_values = {
            value for source in contributors if (value := source.tags.get("waterway"))
        }
        tags = {
            "compiled": preset,
            "source-count": str(len(contributors)),
            "source-refs": _source_references(contributors),
            "water-boundary-normalized": "yes",
        }
        for preferred in ("river", "reservoir", "lake", "pond", "basin"):
            if preferred in water_values:
                tags["water"] = preferred
                break
        for preferred in ("riverbank", "river", "canal", "stream", "drain", "ditch"):
            if preferred in waterway_values:
                tags["waterway"] = preferred
                break
        for preferred in ("bay", "strait", "coastline", "water"):
            if preferred in natural_values:
                tags["natural"] = preferred
                break

        simplified = _simplify(merged, simplify_mm)
        for part_index, part in enumerate(_line_parts(simplified)):
            coordinates = tuple(
                (float(f"{x:.4f}"), float(f"{y:.4f}")) for x, y in part.coords
            )
            geometry_payload.append(min(coordinates, tuple(reversed(coordinates))))
            output.append(
                _stroke_from_line(
                    "water_areas",
                    part,
                    part=f"water-boundary:{merged_index}:{part_index}",
                    smooth=False,
                    tags=tags,
                    osm_type=(
                        next(iter(osm_types)) if len(osm_types) == 1 else "compiled"
                    ),
                    osm_id=(next(iter(osm_ids)) if len(osm_ids) == 1 else "multiple"),
                    name=next(iter(names)) if len(names) == 1 else None,
                )
            )

    unrepresented = sorted(input_refs - represented_refs)
    if unrepresented:
        raise MapPlotterError(
            "Surface-water boundary normalization failed to represent every source: "
            + ", ".join(unrepresented)
        )
    overlap_removed = max(0.0, raw_length - unique_length)
    geometry_sha = hashlib.sha256(
        json.dumps(
            sorted(geometry_payload),
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    diagnostics = {
        "schema_version": 2,
        "policy": policy,
        "enabled": True,
        "input_path_count": len(records),
        "output_path_count": len(output),
        "input_source_ref_count": len(input_refs),
        "represented_source_ref_count": len(represented_refs & input_refs),
        "unrepresented_source_refs": unrepresented,
        "raw_length_mm": round(raw_length, 9),
        "unique_length_mm": round(unique_length, 9),
        "overlap_removed_mm": round(overlap_removed, 9),
        "estimated_ink_saved_mm2_at_0_4mm": round(overlap_removed * 0.4, 9),
        "geometry_sha256": geometry_sha,
    }
    return output, diagnostics, bank_output_count, other_water_output_count


def _stroke_source_references(stroke: PlotStroke) -> set[str]:
    serialized = stroke.tags.get("source-refs", "")
    if serialized:
        return {item for item in serialized.split(";") if item}
    return set()


def _source_lineage_summary(
    projected: list[tuple[MapFeature, list[LineString]]],
    strokes: list[PlotStroke],
    *,
    explicitly_excluded_source_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """Return an exact visible-source ledger for cartographic compilation."""

    visible_by_layer: dict[str, set[str]] = defaultdict(set)
    for feature, parts in projected:
        if parts:
            visible_by_layer[feature.layer].add(_source_reference(feature))
    emitted_by_layer: dict[str, set[str]] = defaultdict(set)
    for stroke in strokes:
        emitted_by_layer[stroke.layer].update(_stroke_source_references(stroke))

    visible = set().union(*visible_by_layer.values()) if visible_by_layer else set()
    emitted = set().union(*emitted_by_layer.values()) if emitted_by_layer else set()
    omitted = sorted(visible - emitted)
    explicitly_excluded = sorted(set(explicitly_excluded_source_refs) & set(omitted))
    unexplained_omissions = sorted(set(omitted) - set(explicitly_excluded))
    return {
        "visible_source_ref_count": len(visible),
        "emitted_source_ref_count": len(visible & emitted),
        "omitted_source_ref_count": len(omitted),
        "omitted_source_refs": omitted,
        "explicitly_excluded_source_ref_count": len(explicitly_excluded),
        "explicitly_excluded_source_refs": explicitly_excluded,
        "unexplained_omitted_source_ref_count": len(unexplained_omissions),
        "unexplained_omitted_source_refs": unexplained_omissions,
        "by_source_layer": {
            layer: {
                "visible": len(refs),
                "emitted": len(refs & emitted),
                "omitted": len(refs - emitted),
            }
            for layer, refs in sorted(visible_by_layer.items())
        },
    }


def _compile_topology_roads(
    features: list[MapFeature],
    layout: Layout,
    map_clip: BaseGeometry,
    *,
    tolerances_mm: dict[str, float],
    preset: str,
    faithful: bool,
    restore_sub_nib_slivers: bool = False,
) -> tuple[list[PlotStroke], dict[str, Any], int]:
    """Generalise selected roads without moving or deleting graph anchors.

    Source lines are projected before simplification and clipped only after the
    validated topology pass.  This avoids the common failure mode where crop
    edges become accidental simplification anchors or junctions disappear from
    independently simplified ways.
    """

    lines = topology_lines_from_map_features(features, layout)
    network = build_road_topology(lines)
    # Share one declared error budget between centreline simplification and
    # sampled corner rounding.  This keeps the final plotted path within the
    # user's --simplify-mm envelope instead of adding an unmeasured SVG curve
    # after an already fully-budgeted simplification pass.
    active_layers = {line.layer for line in network.lines}
    too_small = {
        layer: tolerance
        for layer, tolerance in tolerances_mm.items()
        if layer in active_layers
        and 0 < tolerance < ROAD_COORDINATE_QUANTIZATION_ALLOWANCE_MM
    }
    if too_small:
        detail = ", ".join(
            f"{layer}={tolerance:.9f} mm"
            for layer, tolerance in sorted(too_small.items())
        )
        raise MapPlotterError(
            "A positive road error budget must cover the SVG coordinate "
            "quantization allowance "
            f"({ROAD_COORDINATE_QUANTIZATION_ALLOWANCE_MM:.9f} mm); got {detail}. "
            "Use --simplify-mm 0 for vertex-exact geometry or a larger value."
        )
    available_tolerances = {
        layer: (
            0.0
            if tolerance <= 0
            else max(0.0, tolerance - ROAD_COORDINATE_QUANTIZATION_ALLOWANCE_MM)
        )
        for layer, tolerance in tolerances_mm.items()
    }
    rounding_tolerances = {
        layer: min(0.03, available_tolerances[layer] * 0.35) for layer in tolerances_mm
    }
    simplification_tolerances = {
        layer: max(0.0, available_tolerances[layer] - rounding_tolerances[layer])
        for layer in tolerances_mm
    }
    generalized = simplify_road_topology(network, simplification_tolerances)
    validation = generalized.validation
    if not validation.valid:
        issue_codes = ", ".join(issue.code for issue in validation.issues)
        raise MapPlotterError(
            "Topology-aware road generalization failed validation"
            f" ({issue_codes or 'unknown validation error'})."
        )

    strokes: list[PlotStroke] = []
    omitted_short = 0
    rounded_chain_count = 0
    rounded_corner_count = 0
    rounding_fallback_count = 0
    maximum_rounding_hausdorff_mm = 0.0
    maximum_combined_error_upper_bound_mm = 0.0
    maximum_output_vertex_ratio = 1.0
    span_by_id = {item.span.span_id: item for item in generalized.spans}
    protected_junction_points = {
        anchor.point
        for anchor in network.anchors
        if network.node_degrees.get(anchor.node_key, 0) != 2
    }
    for chain_index, chain in enumerate(generalized.chains):
        source_stroke = chain.to_plot_stroke(part=f"topology:{chain_index}")
        chain_start_node = _serialized_topology_node(chain.start_node)
        chain_end_node = _serialized_topology_node(chain.end_node)
        chain_protected_junction_points = {
            point for point in chain.points if point in protected_junction_points
        }
        rounding = round_polyline_within_page_error(
            chain.points,
            rounding_tolerances[chain.layer],
            protected_points=chain_protected_junction_points,
        )
        if (
            rounding.points[0] != chain.points[0]
            or rounding.points[-1] != chain.points[-1]
        ):
            raise MapPlotterError(
                "Bounded road rounding moved a protected topology endpoint."
            )
        if not chain_protected_junction_points.issubset(set(rounding.points)):
            raise MapPlotterError(
                "Bounded road rounding moved a protected internal junction."
            )
        chain_simplification_error = max(
            (span_by_id[span_id].metrics.hausdorff_mm for span_id in chain.span_ids),
            default=0.0,
        )
        combined_error_upper_bound = (
            chain_simplification_error
            + rounding.metrics.hausdorff_mm
            + (
                ROAD_COORDINATE_QUANTIZATION_ALLOWANCE_MM
                if tolerances_mm[chain.layer] > 0
                else 0.0
            )
        )
        if combined_error_upper_bound > tolerances_mm[chain.layer] + 1e-9:
            raise MapPlotterError(
                "Rounded road centreline exceeded its declared page-space error "
                f"budget for {chain.layer!r}."
            )
        rounded_chain_count += int(rounding.applied)
        rounded_corner_count += rounding.rounded_corner_count
        rounding_fallback_count += int(rounding.fallback_to_source)
        maximum_rounding_hausdorff_mm = max(
            maximum_rounding_hausdorff_mm,
            rounding.metrics.hausdorff_mm,
        )
        maximum_combined_error_upper_bound_mm = max(
            maximum_combined_error_upper_bound_mm,
            combined_error_upper_bound,
        )
        maximum_output_vertex_ratio = max(
            maximum_output_vertex_ratio,
            len(rounding.points) / max(1, len(chain.points)),
        )
        line = _quantized_line(list(rounding.points))
        if line is None:
            continue
        tags = dict(source_stroke.tags)
        tags.update(
            {
                "compiled": preset,
                "generalization": "topology-aware",
                "rounding": (
                    "bounded-sampled-polyline" if rounding.applied else "none"
                ),
                "rounding-tolerance-mm": f"{rounding.tolerance_mm:.6f}",
                "rounding-error-mm": f"{rounding.metrics.hausdorff_mm:.6f}",
                "road-rank": str(road_rank(tags)),
                "bridge": (
                    "yes" if tags.get("bridge") not in {None, "", "no"} else "no"
                ),
                "tunnel": (
                    "yes" if tags.get("tunnel") not in {None, "", "no"} else "no"
                ),
            }
        )
        for part_index, part in enumerate(_line_parts(line.intersection(map_clip))):
            if not faithful and part.length < 0.35:
                omitted_short += 1
                continue
            part_points = [(float(x), float(y)) for x, y in part.coords]
            part_tags = dict(tags)
            part_tags.update(
                {
                    "topology:start-node": _clipped_topology_endpoint(
                        part_points[0],
                        chain_points=chain.points,
                        chain_start_node=chain_start_node,
                        chain_end_node=chain_end_node,
                        endpoint_role="start",
                        clip_identity=f"{chain_index}:{part_index}:start",
                    ),
                    "topology:end-node": _clipped_topology_endpoint(
                        part_points[-1],
                        chain_points=chain.points,
                        chain_start_node=chain_start_node,
                        chain_end_node=chain_end_node,
                        endpoint_role="end",
                        clip_identity=f"{chain_index}:{part_index}:end",
                    ),
                }
            )
            strokes.append(
                _stroke_from_line(
                    source_stroke.layer,
                    part,
                    part=f"topology:{chain_index}:{part_index}",
                    smooth=False,
                    tags=part_tags,
                    osm_type=source_stroke.osm_type,
                    osm_id=source_stroke.osm_id,
                    name=source_stroke.name,
                )
            )

    restored_sub_nib_sources = 0
    restored_sub_nib_strokes = 0
    restored_prequantization_boundary_sources = 0
    restored_prequantization_boundary_strokes = 0
    if restore_sub_nib_slivers:
        emitted_refs = (
            set().union(*(_stroke_source_references(stroke) for stroke in strokes))
            if strokes
            else set()
        )
        for feature in features:
            source_ref = _source_reference(feature)
            if source_ref in emitted_refs:
                continue
            visible_parts = _project_feature(feature, layout, map_clip)
            restored_prequantization_boundary = False
            if not visible_parts:
                visible_parts = _project_exact_boundary_sliver(
                    feature, layout, map_clip
                )
                restored_prequantization_boundary = bool(visible_parts)
            # These slivers can legitimately disappear within the declared
            # topology/serialization error envelope, but are smaller than the
            # compiler's absolute 0.5 mm physical floor in every case. Restore
            # them to the cartographic ledger so physical compilation—not a
            # topology side effect—owns and reports their deliberate omission.
            if not visible_parts or sum(part.length for part in visible_parts) >= 0.5:
                continue
            restored_sub_nib_sources += 1
            restored_prequantization_boundary_sources += int(
                restored_prequantization_boundary
            )
            emitted_refs.add(source_ref)
            for part_index, part in enumerate(visible_parts):
                tags = dict(feature.tags)
                tags.update(
                    {
                        "compiled": preset,
                        "generalization": "exact-sub-nib-fallback",
                        "road-rank": str(road_rank(tags)),
                        "source-count": "1",
                        "source-refs": source_ref,
                        "topology:start-node": (
                            f"sub-nib:{source_ref}:{part_index}:start"
                        ),
                        "topology:end-node": f"sub-nib:{source_ref}:{part_index}:end",
                    }
                )
                if restored_prequantization_boundary:
                    tags["projection-recovery"] = (
                        "exact-prequantization-boundary-sliver"
                    )
                strokes.append(
                    _stroke_from_line(
                        feature.layer,
                        part,
                        part=f"sub-nib-fallback:{part_index}",
                        smooth=False,
                        tags=tags,
                        osm_type=feature.osm_type,
                        osm_id=feature.osm_id,
                        name=feature.name,
                    )
                )
                restored_sub_nib_strokes += 1
                restored_prequantization_boundary_strokes += int(
                    restored_prequantization_boundary
                )

    diagnostics: dict[str, Any] = {
        "enabled": True,
        "restored_sub_nib_boundary_source_count": restored_sub_nib_sources,
        "restored_sub_nib_boundary_stroke_count": restored_sub_nib_strokes,
        "restored_prequantization_boundary_source_count": (
            restored_prequantization_boundary_sources
        ),
        "restored_prequantization_boundary_stroke_count": (
            restored_prequantization_boundary_strokes
        ),
        "source_line_count": len(network.lines),
        "source_vertex_count": sum(len(line.points) for line in network.lines),
        "anchor_count": len(network.anchors),
        "span_count": len(network.spans),
        "output_chain_count": len(generalized.chains),
        "output_clipped_stroke_count": len(strokes),
        "tolerance_mm_by_layer": {
            layer: round(tolerance, 4)
            for layer, tolerance in sorted(tolerances_mm.items())
        },
        "simplification_tolerance_mm_by_layer": {
            layer: round(tolerance, 6)
            for layer, tolerance in sorted(simplification_tolerances.items())
        },
        "coordinate_quantization_allowance_mm": round(
            ROAD_COORDINATE_QUANTIZATION_ALLOWANCE_MM, 9
        ),
        "rounding": {
            "enabled": any(rounding_tolerances.values()),
            "method": "quadratic fillets flattened to validated line segments",
            "tolerance_mm_by_layer": {
                layer: round(tolerance, 6)
                for layer, tolerance in sorted(rounding_tolerances.items())
            },
            "rounded_chain_count": rounded_chain_count,
            "rounded_corner_count": rounded_corner_count,
            "fallback_to_exact_chain_count": rounding_fallback_count,
            "max_rounding_hausdorff_mm": round(maximum_rounding_hausdorff_mm, 6),
            "max_output_vertex_ratio": round(maximum_output_vertex_ratio, 3),
            "compiled_sampled_polyline_validated": True,
        },
        "validation": {
            "valid": validation.valid,
            "connectivity_preserved": validation.connectivity_preserved,
            "protected_chain_endpoints_preserved": True,
            "protected_internal_junctions_preserved": True,
            "max_anchor_displacement_mm": round(
                validation.max_anchor_displacement_mm, 6
            ),
            "max_hausdorff_mm": round(validation.max_hausdorff_mm, 6),
            "max_combined_error_upper_bound_mm": round(
                maximum_combined_error_upper_bound_mm, 6
            ),
            "combined_error_within_declared_tolerance": True,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "span_id": issue.span_id,
                }
                for issue in validation.issues
            ],
        },
    }
    return strokes, diagnostics, omitted_short


def prepare_clean_poster_strokes(
    features: list[MapFeature],
    layout: Layout,
    *,
    simplify_mm: float,
    detail_profile: str = "plot",
    water_fill: str = "none",
    landmark_buildings: bool = False,
    landmark_refs: tuple[str, ...] = (),
    landmark_source_tags: dict[str, dict[str, str] | None] | None = None,
    water_dot_nib_mm: float = 0.25,
    landmark_nib_mm: float = 0.25,
    course_clearance_mm: float = 0.0,
) -> CartographyResult:
    """Compile OSM features using either plot or source-faithful semantics.

    ``plot`` preserves the restrained poster behaviour.  All profiles in
    :data:`FULL_CARTOGRAPHY_DETAIL_PROFILES` bypass semantic selection, fixed
    detail budgets, parallel suppression, hidden simplification floors, and
    decorative curve smoothing.  ``plotter-faithful`` and ``ink-balanced``
    also restore crop-edge slivers so a later physical compiler owns every
    deliberate omission.  Unlike the two source-complete profiles,
    ``ink-balanced`` is explicitly permitted to apply a verified ink-budget
    gate *after* this full cartographic preparation stage.
    """

    if detail_profile not in DETAIL_PROFILE_CHOICES:
        raise MapPlotterError(
            "Detail profile must be one of: "
            f"{', '.join(sorted(DETAIL_PROFILE_CHOICES))}."
        )
    if water_fill not in WATER_FILL_CHOICES:
        raise MapPlotterError(
            f"Water fill must be one of: {', '.join(sorted(WATER_FILL_CHOICES))}."
        )
    if landmark_refs and not landmark_buildings:
        raise MapPlotterError(
            "Required landmark refs require landmark-building selection."
        )
    for label, nib_mm in (
        ("water-dot", water_dot_nib_mm),
        ("landmark", landmark_nib_mm),
    ):
        if not isfinite(nib_mm) or nib_mm <= 0:
            raise MapPlotterError(
                f"{label.title()} nib must be a positive finite width."
            )

    # Dotted water is a physical area treatment: its polygon bank and stipple
    # already communicate the feature, so a centreline inside that same area
    # is redundant ink.  Reuse the audited ink-balanced normalization without
    # changing ordinary source-faithful exports that did not request dots.
    surface_water_normalization_enabled = (
        detail_profile == "ink-balanced" or water_fill == "dots"
    )
    full_cartography = detail_profile in FULL_CARTOGRAPHY_DETAIL_PROFILES
    # The richer selection belongs to the composition, not to one sheet, so an
    # A4 balanced poster gets the same cartography as the A5 one.
    balanced = (
        poster_preset_composition(layout.preset) == "balanced" and not full_cartography
    )
    landmark_policy = landmark_building_policy(layout)
    full = full_cartography or layout.preset == "standard"
    preset = layout.preset
    xmin, ymin, xmax, ymax = layout.clip_rect
    map_clip = box(xmin, ymin, xmax, ymax)
    projected: list[tuple[MapFeature, list[LineString]]] = []
    projected_areas = _project_feature_areas(features, layout, map_clip)
    for feature in features:
        parts = _project_feature(
            feature,
            layout,
            map_clip,
            preserve_exact_boundary_slivers=(
                detail_profile in PHYSICAL_MINIMUM_GATED_DETAIL_PROFILES
                and feature.layer in ROAD_LAYERS
            ),
        )
        # An area can cover the entire crop while its boundary lies outside it.
        # Keep that polygon for masking/selection even though it has no visible
        # outline segment inside the page rectangle.
        if parts or not projected_areas[id(feature)].is_empty:
            projected.append((feature, parts))

    requested_landmark_refs = frozenset(landmark_refs)
    feature_tags_by_object: dict[str, dict[str, str]] = {}
    building_feature_objects: set[str] = set()
    for feature in features:
        object_key = f"{feature.osm_type}/{feature.osm_id}"
        combined = feature_tags_by_object.setdefault(object_key, {})
        for key, value in feature.tags.items():
            if value and not combined.get(key):
                combined[key] = value
        if feature.name and not combined.get("name"):
            combined["name"] = feature.name
        if feature.layer == "buildings":
            building_feature_objects.add(object_key)
    projected_building_objects = {
        f"{feature.osm_type}/{feature.osm_id}"
        for feature, _parts in projected
        if feature.layer == "buildings"
    }
    required_landmark_dispositions: dict[str, dict[str, Any]] = {}
    for required_ref in landmark_refs:
        source_tags = (
            landmark_source_tags.get(required_ref)
            if landmark_source_tags is not None
            else feature_tags_by_object.get(required_ref)
        )
        disposition: dict[str, Any] = {
            "requested_ref": required_ref,
            "status": "pending",
            "reason": None,
            "representative_object": None,
            "represented_source_refs": [],
            "area_mm2": None,
            "minimum_area_mm2": round(
                LANDMARK_REQUIRED_MIN_AREA_MM2 * landmark_policy.minimum_area_scale,
                6,
            ),
            "minimum_oriented_span_mm": None,
            "visible_perimeter_mm": None,
        }
        if source_tags is None:
            disposition.update(
                status="absent_from_source",
                reason="requested OSM object is absent from the acquired source",
            )
        elif is_heritage_site_candidate(source_tags) and not _landmark_has_identity(
            source_tags
        ):
            disposition.update(
                status="ineligible",
                reason=(
                    "historic castle/palace sites require a name, knowledge-base "
                    "identity, or heritage reference"
                ),
            )
        elif not _required_landmark_is_building(source_tags):
            disposition.update(
                status="non_building",
                reason=(
                    "requested OSM object is neither a positive building nor "
                    "leisure=stadium"
                ),
            )
        elif (rejection := _landmark_lifecycle_rejection(source_tags)) is not None:
            disposition.update(status="ineligible", reason=rejection)
        elif required_ref not in building_feature_objects:
            disposition.update(
                status="ineligible",
                reason="requested building has no extractable polygon footprint",
            )
        elif required_ref not in projected_building_objects:
            disposition.update(
                status="outside_crop",
                reason="requested building footprint does not intersect the map crop",
            )
        required_landmark_dispositions[required_ref] = disposition
    balanced_detail_keys = _balanced_detail_keys(projected, layout) if balanced else {}

    reference_road_lines: list[LineString] = []
    reference_bridge_lines: list[LineString] = []
    for feature, parts in projected:
        if feature.layer not in ROAD_LAYERS or feature.layer == "paths":
            continue
        target = (
            feature.layer
            if full
            else _poster_road_target(
                feature, sum(part.length for part in parts), balanced=balanced
            )
        )
        if target is None:
            continue
        category = _detail_category(feature)
        if (
            balanced
            and category is not None
            and _detail_group_key(feature) not in balanced_detail_keys[category]
        ):
            continue
        reference_road_lines.extend(parts)
        if feature.tags.get("bridge") not in {None, "no"}:
            reference_bridge_lines.extend(parts)
    road_reference = (
        unary_union(reference_road_lines)
        if reference_road_lines
        else GeometryCollection()
    )
    bridge_reference = (
        unary_union(reference_bridge_lines)
        if reference_bridge_lines
        else GeometryCollection()
    )
    road_proximity = (
        road_reference.buffer(0.60)
        if not road_reference.is_empty
        else GeometryCollection()
    )
    bridge_proximity = (
        bridge_reference.buffer(0.95)
        if not bridge_reference.is_empty
        else GeometryCollection()
    )

    road_groups: dict[RoadGroupKey, list[LineString]] = defaultdict(list)
    road_metadata: dict[RoadGroupKey, dict[str, Any]] = {}
    road_source_records: dict[RoadGroupKey, list[tuple[MapFeature, LineString]]] = (
        defaultdict(list)
    )
    topology_road_features: list[MapFeature] = []
    bridge_lines: list[tuple[MapFeature, LineString, float]] = []
    river_bank_records: list[tuple[MapFeature, LineString]] = []
    river_area_geometries: list[BaseGeometry] = []
    surface_water_area_records: list[tuple[MapFeature, BaseGeometry]] = []
    other_water_area_records: list[tuple[MapFeature, LineString]] = []
    semantic_bay_boundary_candidates: list[tuple[MapFeature, LineString]] = []
    semantic_bay_surface_areas: dict[str, list[BaseGeometry]] = defaultdict(list)
    coastline_records: list[tuple[MapFeature, LineString]] = []
    river_centre_records: list[tuple[MapFeature, LineString]] = []
    narrow_water_records: list[tuple[MapFeature, LineString]] = []
    subterranean_water_records: list[tuple[MapFeature, LineString]] = []
    passthrough: dict[str, list[tuple[MapFeature, LineString]]] = defaultdict(list)
    omitted_paths = 0
    omitted_service = 0
    omitted_parallel_paths = 0
    retained_road_features = 0
    retained_path_features = 0
    retained_service_features = 0
    omitted_context_features = 0
    omitted_detail_budget_features = 0
    omitted_area_budget_strokes = 0
    course_cleared_strokes = 0
    course_cleared_mm = 0.0
    excluded_non_landmark_refs: list[str] = []
    excluded_landmark_budget_refs: list[str] = []

    landmark_evaluations: dict[str, dict[str, Any] | None] = {}
    if landmark_buildings:
        landmark_groups: dict[str, list[tuple[MapFeature, list[LineString]]]] = (
            defaultdict(list)
        )
        for candidate, candidate_parts in projected:
            if candidate.layer == "buildings":
                landmark_groups[f"{candidate.osm_type}/{candidate.osm_id}"].append(
                    (candidate, candidate_parts)
                )
        for object_key, landmark_object_records in landmark_groups.items():
            combined_tags: dict[str, str] = {}
            areas: list[BaseGeometry] = []
            unique_outlines: dict[tuple[tuple[float, float], ...], LineString] = {}
            for candidate, candidate_parts in landmark_object_records:
                for key, value in candidate.tags.items():
                    if value and not combined_tags.get(key):
                        combined_tags[key] = value
                if candidate.name and not combined_tags.get("name"):
                    combined_tags["name"] = candidate.name
                area = projected_areas[id(candidate)]
                if not area.is_empty:
                    areas.append(area)
                for line in candidate_parts:
                    coordinates = tuple(
                        (round(float(x), 6), round(float(y), 6)) for x, y in line.coords
                    )
                    canonical = min(coordinates, tuple(reversed(coordinates)))
                    unique_outlines[canonical] = line
            footprint = unary_union(areas) if areas else GeometryCollection()
            area_mm2 = float(footprint.area)
            perimeter_mm = sum(line.length for line in unique_outlines.values())
            span_mm = _minimum_oriented_span_mm(footprint)
            role_and_rank = _landmark_building_role(
                combined_tags, area_mm2, landmark_policy.minimum_area_scale
            )
            required = object_key in requested_landmark_refs
            required_disposition = required_landmark_dispositions.get(object_key)
            if required and required_disposition is not None:
                required_disposition.update(
                    area_mm2=round(area_mm2, 6),
                    minimum_oriented_span_mm=round(span_mm, 6),
                    visible_perimeter_mm=round(perimeter_mm, 6),
                )
            if (
                required
                and required_disposition is not None
                and required_disposition["status"] == "pending"
            ):
                minimum_required_area_mm2 = (
                    LANDMARK_REQUIRED_MIN_AREA_MM2 * landmark_policy.minimum_area_scale
                )
                physical_reasons: list[str] = []
                if area_mm2 + 1e-9 < minimum_required_area_mm2:
                    physical_reasons.append(
                        f"projected area {area_mm2:.6f} mm² is below "
                        f"{minimum_required_area_mm2:.6f} mm²"
                    )
                if span_mm + 1e-9 < landmark_policy.minimum_oriented_span_mm:
                    physical_reasons.append(
                        f"oriented span {span_mm:.6f} mm is below "
                        f"{landmark_policy.minimum_oriented_span_mm:.6f} mm"
                    )
                if perimeter_mm + 1e-9 < landmark_policy.minimum_visible_perimeter_mm:
                    physical_reasons.append(
                        f"visible perimeter {perimeter_mm:.6f} mm is below "
                        f"{landmark_policy.minimum_visible_perimeter_mm:.6f} mm"
                    )
                if physical_reasons:
                    required_disposition.update(
                        status="physically_unprintable",
                        reason="; ".join(physical_reasons),
                    )
                    landmark_evaluations[object_key] = None
                    continue
                required_disposition.update(
                    status="eligible",
                    reason="passed source, lifecycle, crop, and physical-size gates",
                )
                role, rank = role_and_rank or ("required", 10)
                landmark_evaluations[object_key] = {
                    "role": role,
                    "rank": rank,
                    "area_mm2": area_mm2,
                    "span_mm": span_mm,
                    "perimeter_mm": perimeter_mm,
                    "identified": _landmark_has_identity(combined_tags),
                    "required": True,
                }
                continue
            if (
                role_and_rank is None
                or span_mm + 1e-9 < landmark_policy.minimum_oriented_span_mm
                or perimeter_mm + 1e-9 < landmark_policy.minimum_visible_perimeter_mm
            ):
                landmark_evaluations[object_key] = None
                continue
            role, rank = role_and_rank
            landmark_evaluations[object_key] = {
                "role": role,
                "rank": rank,
                "area_mm2": area_mm2,
                "span_mm": span_mm,
                "perimeter_mm": perimeter_mm,
                "identified": _landmark_has_identity(combined_tags),
                "required": False,
            }

    for feature, parts in projected:
        projected_area_mm2 = projected_areas[id(feature)].area
        if feature.layer == "road_areas" and not full_cartography:
            # These non-routable pavement perimeters are valuable in a literal
            # high-detail export but visually overwhelm the restrained poster
            # profile in extensively micromapped cities.
            omitted_context_features += 1
            continue
        if feature.layer in ROAD_LAYERS:
            length_mm = sum(part.length for part in parts)
            target_layer = (
                feature.layer
                if full
                else _poster_road_target(feature, length_mm, balanced=balanced)
            )
            if target_layer is None:
                if feature.layer == "paths":
                    omitted_paths += 1
                if feature.tags.get("highway") == "service":
                    omitted_service += 1
                continue
            category = _detail_category(feature)
            if (
                balanced
                and category is not None
                and _detail_group_key(feature) not in balanced_detail_keys[category]
            ):
                omitted_detail_budget_features += 1
                if feature.layer == "paths":
                    omitted_paths += 1
                if feature.tags.get("highway") == "service":
                    omitted_service += 1
                continue
            is_bridge = feature.tags.get("bridge") not in {None, "no"}
            if (
                balanced
                and feature.layer == "paths"
                and target_layer == "paths"
                and _parallel_path_is_redundant(
                    parts,
                    road_proximity,
                    bridge_proximity,
                    is_bridge=is_bridge,
                )
            ):
                omitted_paths += 1
                omitted_parallel_paths += 1
                continue
            retained_road_features += 1
            if feature.layer == "paths":
                retained_path_features += 1
            if feature.tags.get("highway") == "service":
                retained_service_features += 1
            topology_road_features.append(replace(feature, layer=target_layer))
            highway = feature.tags.get("highway", "")
            group = (
                target_layer,
                road_rank(feature.tags),
                highway,
                (feature.name or "").casefold(),
                grade_signature(feature.tags),
                (
                    f"{feature.osm_type}:{feature.osm_id}:{feature.part}"
                    if full_cartography
                    else ""
                ),
            )
            road_groups[group].extend(parts)
            road_source_records[group].extend((feature, part) for part in parts)
            metadata = road_metadata.setdefault(
                group,
                {
                    "bridge": False,
                    "tunnel": False,
                    "tags": dict(feature.tags),
                },
            )
            metadata["bridge"] = bool(metadata["bridge"]) or is_bridge
            metadata["tunnel"] = bool(metadata["tunnel"]) or feature.tags.get(
                "tunnel"
            ) not in {None, "no"}
            if is_bridge:
                radius = 0.30 if feature.layer == "paths" else 0.65
                bridge_lines.extend((feature, part, radius) for part in parts)
            continue

        if feature.layer == "water_areas":
            area_geometry = projected_areas[id(feature)]
            if not area_geometry.is_empty:
                surface_water_area_records.append((feature, area_geometry))
            if (
                feature.tags.get("water") == "river"
                or feature.tags.get("waterway") == "riverbank"
            ):
                river_bank_records.extend((feature, part) for part in parts)
                if not area_geometry.is_empty:
                    river_area_geometries.append(area_geometry)
            elif water_fill == "dots" and _is_closed_semantic_bay_surface(
                feature, area_geometry
            ):
                source_ref = _source_reference(feature)
                semantic_bay_boundary_candidates.extend(
                    (feature, part) for part in parts
                )
                semantic_bay_surface_areas[source_ref].append(area_geometry)
            else:
                other_water_area_records.extend((feature, part) for part in parts)
            continue

        if feature.layer == "waterways":
            subterranean_reasons = _subterranean_waterway_reasons(feature.tags)
            if (
                detail_profile == "ink-balanced"
                and feature.tags.get("waterway")
                and subterranean_reasons
            ):
                subterranean_water_records.extend((feature, part) for part in parts)
                continue
            if feature.tags.get("natural") == "coastline":
                coastline_records.extend((feature, part) for part in parts)
            elif feature.tags.get("waterway") == "river" or (
                detail_profile == "ink-balanced"
                and feature.tags.get("waterway") in {"canal", "tidal_channel"}
            ):
                river_centre_records.extend((feature, part) for part in parts)
            else:
                narrow_water_records.extend((feature, part) for part in parts)
            continue

        if feature.layer == "buildings" and landmark_buildings:
            object_key = f"{feature.osm_type}/{feature.osm_id}"
            landmark = landmark_evaluations.get(object_key)
            if landmark is None:
                excluded_non_landmark_refs.append(_source_reference(feature))
                continue
            original_feature = feature
            feature = replace(
                feature,
                tags={
                    **feature.tags,
                    "mapplot:landmark-object": object_key,
                    "mapplot:landmark-role": str(landmark["role"]),
                    "mapplot:landmark-rank": str(landmark["rank"]),
                    "mapplot:projected-area-mm2": (
                        f"{float(landmark['area_mm2']):.6f}"
                    ),
                    "mapplot:minimum-oriented-span-mm": (
                        f"{float(landmark['span_mm']):.6f}"
                    ),
                    "mapplot:visible-perimeter-mm": (
                        f"{float(landmark['perimeter_mm']):.6f}"
                    ),
                    "mapplot:identified": ("yes" if landmark["identified"] else "no"),
                    "mapplot:required-landmark": (
                        "yes" if landmark.get("required") else "no"
                    ),
                },
            )
            # `projected_areas` is keyed by object identity, and `replace`
            # produced a new object. Without re-registering it, any later
            # lookup -- the buildings area budget is one -- raises KeyError on
            # a landmark that was selected perfectly well.
            projected_areas[id(feature)] = projected_areas[id(original_feature)]

        if _retain_poster_context(
            feature,
            parts,
            balanced=balanced,
            full=full,
            clipped_area_mm2=projected_area_mm2,
        ):
            passthrough[feature.layer].extend((feature, part) for part in parts)
        else:
            omitted_context_features += 1

    coastline_surface_records, coastline_water_mask_diagnostics = (
        _coastline_water_surface_records(
            coastline_records,
            layout=layout,
            clip_rect=layout.clip_rect,
            enabled=water_fill == "dots",
        )
    )
    surface_water_area_records.extend(coastline_surface_records)

    strokes: list[PlotStroke] = []
    stipple_strokes: list[PlotStroke] = []
    water_stipple_diagnostics: dict[str, Any] = {"enabled": False}
    if water_fill == "dots":
        stipple_strokes, water_stipple_diagnostics = _water_stipple_strokes(
            surface_water_area_records,
            preset=preset,
            clip_rect=layout.clip_rect,
            nib_mm=water_dot_nib_mm,
        )
        strokes.extend(stipple_strokes)

    (
        retained_semantic_bay_boundaries,
        semantic_bay_boundary_diagnostics,
    ) = _semantic_bay_boundary_suppression(
        semantic_bay_boundary_candidates,
        surface_areas_by_ref=semantic_bay_surface_areas,
        stipple_strokes=stipple_strokes,
        enabled=water_fill == "dots",
    )
    other_water_area_records.extend(retained_semantic_bay_boundaries)

    road_tolerances = (
        {
            "roads_major": simplify_mm * 0.65,
            "roads_secondary": simplify_mm * 0.8,
            "roads_local": simplify_mm,
            "roads_other": simplify_mm,
            "paths": simplify_mm,
        }
        if full_cartography
        else {
            "roads_major": max(0.03, simplify_mm * 0.65),
            "roads_secondary": max(0.04, simplify_mm * 0.8),
            "roads_local": max(0.05, simplify_mm),
            "roads_other": max(0.06, simplify_mm),
            "paths": max(0.07, simplify_mm),
        }
    )
    merged_road_count = 0
    omitted_short_road_strokes = 0
    topology_diagnostics: dict[str, Any] = {"enabled": False}
    if full_cartography and topology_road_features:
        topology_strokes, topology_diagnostics, omitted_short_road_strokes = (
            _compile_topology_roads(
                topology_road_features,
                layout,
                map_clip,
                tolerances_mm=road_tolerances,
                preset=preset,
                faithful=full_cartography,
                restore_sub_nib_slivers=(
                    detail_profile in PHYSICAL_MINIMUM_GATED_DETAIL_PROFILES
                ),
            )
        )
        strokes.extend(topology_strokes)
        merged_road_count = len(topology_strokes)
    else:
        # Plot mode uses its established semantic merge policy. Full-cartography
        # profiles always go through the source-topology graph, including at
        # zero tolerance, so compatible OSM ways join without moving a source
        # vertex.
        for group, lines in road_groups.items():
            layer, rank, highway, _, _, _ = group
            metadata = road_metadata[group]
            source_index = _SourceLineIndex.build(road_source_records[group])
            for index, merged in enumerate(_merge_lines(lines)):
                contributors = source_index.contributors(merged)
                source_ids = sorted({source.osm_id for source in contributors})
                source_types = sorted({source.osm_type for source in contributors})
                names = {source.name for source in contributors if source.name}
                retained_tags = dict(metadata["tags"])
                retained_tags.update(
                    {
                        "compiled": preset,
                        "road-rank": str(rank),
                        "source-count": str(len(contributors)),
                        "source-refs": _source_references(contributors),
                        "bridge": "yes" if metadata["bridge"] else "no",
                        "tunnel": "yes" if metadata["tunnel"] else "no",
                    }
                )
                if highway:
                    retained_tags["highway"] = highway
                simplified = _simplify(merged, road_tolerances[layer])
                for part_index, part in enumerate(_line_parts(simplified)):
                    if not full_cartography and part.length < 0.35:
                        omitted_short_road_strokes += 1
                        continue
                    strokes.append(
                        _stroke_from_line(
                            layer,
                            part,
                            part=f"merged:{index}:{part_index}",
                            smooth=not full_cartography and len(part.coords) >= 3,
                            tags=retained_tags,
                            osm_type=(
                                source_types[0]
                                if len(source_types) == 1
                                else "compiled"
                            ),
                            osm_id=(
                                source_ids[0] if len(source_ids) == 1 else "multiple"
                            ),
                            name=next(iter(names)) if len(names) == 1 else None,
                        )
                    )
                    merged_road_count += 1

    river_bank_crop_warning = bool(
        river_bank_records and any(not line.is_ring for _, line in river_bank_records)
    )
    river_bank_lines = [line for _, line in river_bank_records]
    fallback_river_bank_lines = [
        line
        for source, line in river_bank_records
        if source.geometry_type != "polygon_ring"
        and projected_areas[id(source)].is_empty
    ]
    other_water_area_lines = [line for _, line in other_water_area_records]
    coastline_lines = [line for _, line in coastline_records]
    surface_boundary_records = [
        *river_bank_records,
        *other_water_area_records,
        *coastline_records,
    ]
    surface_boundary_lines = [line for _source, line in surface_boundary_records]
    surface_boundary_union = (
        unary_union(surface_boundary_lines)
        if surface_boundary_lines
        else GeometryCollection()
    )
    bank_union = (
        unary_union(river_bank_lines) if river_bank_lines else GeometryCollection()
    )
    fallback_bank_union = (
        unary_union(fallback_river_bank_lines)
        if fallback_river_bank_lines
        else GeometryCollection()
    )
    relevant_bridge_buffers: list[BaseGeometry] = []
    relevant_bridge_source_refs: set[str] = set()
    bridge_intersection_target = (
        surface_boundary_union if detail_profile == "ink-balanced" else bank_union
    )
    for bridge_source, bridge, radius in bridge_lines:
        footprint = bridge.buffer(radius, cap_style="square", join_style="mitre")
        if not bridge_intersection_target.is_empty and footprint.intersects(
            bridge_intersection_target
        ):
            relevant_bridge_buffers.append(footprint)
            relevant_bridge_source_refs.add(_source_reference(bridge_source))
    if not full_cartography and relevant_bridge_buffers and not bank_union.is_empty:
        bank_union = bank_union.difference(unary_union(relevant_bridge_buffers))

    # At A5, sub-millimetre quay and survey detail reads as wobble rather than
    # useful geography. Generalise broad banks, but leave curve generation to
    # the bounded SVG renderer rather than inflating every source segment here.
    water_tolerance = (
        simplify_mm
        if full_cartography
        else max(0.08, simplify_mm)
        if full
        else max(0.70, min(0.85, simplify_mm * 9.0))
    )
    water_boundary_normalization: dict[str, Any] = {
        "schema_version": 2,
        "policy": (
            "ink-balanced-water-boundary-v2"
            if detail_profile == "ink-balanced"
            else "dotted-surface-water-boundary-v1"
        ),
        "enabled": False,
        "input_path_count": 0,
        "output_path_count": 0,
        "input_source_ref_count": 0,
        "represented_source_ref_count": 0,
        "unrepresented_source_refs": [],
        "raw_length_mm": 0.0,
        "unique_length_mm": 0.0,
        "overlap_removed_mm": 0.0,
        "estimated_ink_saved_mm2_at_0_4mm": 0.0,
        "geometry_sha256": hashlib.sha256(b"[]").hexdigest(),
    }
    normalized_bank_count = 0
    normalized_other_water_count = 0
    normalized_strokes: list[PlotStroke] = []
    bridge_boundary_knockouts: dict[str, Any] = {
        "enabled": detail_profile == "ink-balanced",
        "bridge_buffer_count": 0,
        "bridge_source_refs": [],
        "affected_path_count": 0,
        "affected_source_ref_count": 0,
        "affected_source_refs": [],
        "removed_length_mm": 0.0,
        "retained_length_mm": 0.0,
        "fully_removed_source_refs": [],
        "geometry_sha256": hashlib.sha256(b"[]").hexdigest(),
    }
    if surface_water_normalization_enabled:
        normalized_boundary_records = list(surface_boundary_records)
        if (
            detail_profile == "ink-balanced"
            and relevant_bridge_buffers
            and normalized_boundary_records
        ):
            knockout_union = unary_union(relevant_bridge_buffers)
            knocked_out_records: list[tuple[MapFeature, LineString]] = []
            affected_refs: set[str] = set()
            removed_length = 0.0
            affected_path_count = 0
            for source, line in normalized_boundary_records:
                remaining_parts = list(_line_parts(line.difference(knockout_union)))
                retained_length = sum(part.length for part in remaining_parts)
                removed = max(0.0, line.length - retained_length)
                # A complete deletion has no drawable carrier for its provenance;
                # retain that tiny source path instead of silently losing it.
                if removed > 1e-6 and remaining_parts:
                    knocked_out_records.extend(
                        (source, part) for part in remaining_parts
                    )
                    affected_refs.add(_source_reference(source))
                    affected_path_count += 1
                    removed_length += removed
                else:
                    knocked_out_records.append((source, line))
            normalized_boundary_records = knocked_out_records
            bridge_boundary_knockouts = {
                "enabled": True,
                "bridge_buffer_count": len(relevant_bridge_buffers),
                "bridge_source_refs": sorted(relevant_bridge_source_refs),
                "affected_path_count": affected_path_count,
                "affected_source_ref_count": len(affected_refs),
                "affected_source_refs": sorted(affected_refs),
                "removed_length_mm": round(removed_length, 9),
                "retained_length_mm": round(
                    sum(line.length for _source, line in normalized_boundary_records),
                    9,
                ),
                "fully_removed_source_refs": [],
                "geometry_sha256": _line_geometry_sha256(
                    line for _source, line in normalized_boundary_records
                ),
            }
        (
            normalized_strokes,
            water_boundary_normalization,
            normalized_bank_count,
            normalized_other_water_count,
        ) = _normalized_ink_balanced_water_boundaries(
            normalized_boundary_records,
            preset=preset,
            simplify_mm=water_tolerance,
            policy=(
                "ink-balanced-water-boundary-v2"
                if detail_profile == "ink-balanced"
                else "dotted-surface-water-boundary-v1"
            ),
        )
        strokes.extend(normalized_strokes)
        # The common representation above owns emission for all three inputs.
        # Keep polygon masks/fallback probes already derived from the originals,
        # while preventing their legacy per-kind emitters from drawing them again.
        river_bank_records = []
        other_water_area_records = []
        coastline_records = []
        river_bank_lines = []
        other_water_area_lines = []
        coastline_lines = []
        bank_union = GeometryCollection()

    bank_stroke_count = normalized_bank_count
    river_bank_index = _SourceLineIndex.build(river_bank_records)
    for index, bank in enumerate(_merge_lines(list(_line_parts(bank_union)))):
        contributors = river_bank_index.contributors(bank)
        river_bank_ids = {source.osm_id for source in contributors}
        river_bank_types = {source.osm_type for source in contributors}
        river_bank_names = {source.name for source in contributors if source.name}
        simplified = _simplify(bank, water_tolerance)
        for part_index, part in enumerate(_line_parts(simplified)):
            if not full_cartography and part.length < 0.4:
                continue
            strokes.append(
                _stroke_from_line(
                    "water_areas",
                    part,
                    part=f"river-bank:{index}:{part_index}",
                    smooth=(
                        not full_cartography
                        and len(part.coords) >= 3
                        and not part.is_ring
                    ),
                    tags={
                        "water": "river",
                        "compiled": preset,
                        "source-count": str(len(contributors)),
                        "source-refs": _source_references(contributors),
                    },
                    osm_type=(
                        next(iter(river_bank_types))
                        if len(river_bank_types) == 1
                        else "compiled"
                    ),
                    osm_id=(
                        next(iter(river_bank_ids))
                        if len(river_bank_ids) == 1
                        else "multiple"
                    ),
                    name=(
                        next(iter(river_bank_names))
                        if len(river_bank_names) == 1
                        else None
                    ),
                )
            )
            bank_stroke_count += 1

    retained_water_area_count = normalized_other_water_count
    omitted_tiny_water_areas = 0
    for index, (source, water_line) in enumerate(other_water_area_records):
        simplified = cast(
            LineString,
            _simplify(water_line, simplify_mm if full_cartography else 0.08),
        )
        if not full_cartography and (
            simplified.length < 0.9
            or (simplified.is_ring and Polygon(simplified).area < 0.08)
        ):
            omitted_tiny_water_areas += 1
            continue
        strokes.append(
            _stroke_from_line(
                "water_areas",
                simplified,
                part=f"water-area:{index}",
                smooth=(
                    not full_cartography
                    and len(simplified.coords) >= 4
                    and not simplified.is_ring
                ),
                tags={
                    **source.tags,
                    "compiled": preset,
                    "source-count": "1",
                    "source-refs": _source_reference(source),
                },
                osm_type=source.osm_type,
                osm_id=source.osm_id,
                name=source.name,
            )
        )
        retained_water_area_count += 1

    coastline_union: BaseGeometry = (
        unary_union(coastline_lines) if coastline_lines else GeometryCollection()
    )
    duplicate_water_boundaries = river_bank_lines + other_water_area_lines
    if (
        not full_cartography
        and not coastline_union.is_empty
        and duplicate_water_boundaries
    ):
        coastline_union = coastline_union.difference(
            unary_union(duplicate_water_boundaries).buffer(0.12)
        )
    coastline_index = _SourceLineIndex.build(coastline_records)
    for index, coastline in enumerate(_merge_lines(list(_line_parts(coastline_union)))):
        contributors = coastline_index.contributors(coastline)
        coastline_ids = {source.osm_id for source in contributors}
        coastline_types = {source.osm_type for source in contributors}
        coastline_names = {source.name for source in contributors if source.name}
        simplified = cast(
            LineString,
            _simplify(coastline, simplify_mm if full_cartography else 0.20),
        )
        strokes.append(
            _stroke_from_line(
                "water_areas",
                simplified,
                part=f"coastline:{index}",
                smooth=not full_cartography and len(simplified.coords) >= 3,
                tags={
                    "natural": "coastline",
                    "compiled": preset,
                    "source-count": str(len(contributors)),
                    "source-refs": _source_references(contributors),
                },
                osm_type=(
                    next(iter(coastline_types))
                    if len(coastline_types) == 1
                    else "compiled"
                ),
                osm_id=(
                    next(iter(coastline_ids)) if len(coastline_ids) == 1 else "multiple"
                ),
                name=(
                    next(iter(coastline_names)) if len(coastline_names) == 1 else None
                ),
            )
        )

    subterranean_by_ref: dict[str, list[tuple[MapFeature, LineString]]] = defaultdict(
        list
    )
    for source, line in subterranean_water_records:
        subterranean_by_ref[_source_reference(source)].append((source, line))
    subterranean_entries: list[dict[str, Any]] = []
    for source_ref, records in sorted(subterranean_by_ref.items()):
        source = records[0][0]
        lines = [line for _source, line in records]
        subterranean_entries.append(
            {
                "source_ref": source_ref,
                "waterway": source.tags.get("waterway", ""),
                "tags": dict(sorted(source.tags.items())),
                "reasons": list(_subterranean_waterway_reasons(source.tags)),
                "path_count": len(lines),
                "projected_length_mm": round(sum(line.length for line in lines), 9),
                "geometry_sha256": _line_geometry_sha256(lines),
            }
        )
    subterranean_exclusions = {
        "enabled": detail_profile == "ink-balanced",
        "entry_count": len(subterranean_entries),
        "excluded_path_count": len(subterranean_water_records),
        "source_ref_count": len(subterranean_entries),
        "source_refs": [entry["source_ref"] for entry in subterranean_entries],
        "total_length_mm": round(
            sum(float(entry["projected_length_mm"]) for entry in subterranean_entries),
            9,
        ),
        "entries": subterranean_entries,
        "ledger_sha256": hashlib.sha256(
            json.dumps(
                subterranean_entries,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest(),
    }

    surface_centreline_records: list[tuple[str, MapFeature, LineString]] = []
    untouched_narrow_water_records = list(narrow_water_records)
    if surface_water_normalization_enabled:
        suppressible_narrow_records = [
            (source, line)
            for source, line in narrow_water_records
            if source.tags.get("waterway") in SURFACE_CENTRELINE_WATERWAYS
        ]
        untouched_narrow_water_records = [
            (source, line)
            for source, line in narrow_water_records
            if source.tags.get("waterway") not in SURFACE_CENTRELINE_WATERWAYS
        ]
        surface_centreline_records = sorted(
            [("rivers", source, line) for source, line in river_centre_records]
            + [
                ("waterways", source, line)
                for source, line in suppressible_narrow_records
            ],
            key=lambda item: (
                item[0],
                item[1].osm_type,
                item[1].osm_id,
                item[1].part,
                _canonical_line_coordinates(item[2]),
            ),
        )

    centreline_suppression: dict[str, Any] = {
        "enabled": surface_water_normalization_enabled,
        "input_path_count": 0,
        "input_source_ref_count": 0,
        "input_source_refs": [],
        "input_length_mm": 0.0,
        "visible_path_count": 0,
        "visible_source_ref_count": 0,
        "visible_source_refs": [],
        "visible_length_mm": 0.0,
        "partially_suppressed_source_ref_count": 0,
        "partially_suppressed_source_refs": [],
        "fully_suppressed_source_ref_count": 0,
        "fully_suppressed_source_refs": [],
        **(
            {
                "standalone_retained_source_ref_count": 0,
                "standalone_retained_source_refs": [],
            }
            if water_fill == "dots"
            else {}
        ),
        "polygon_suppressed_length_mm": 0.0,
        "flank_suppressed_length_mm": 0.0,
        "suppressed_length_mm": 0.0,
        "source_records": [],
        "representation_link_count": 0,
        "representation_links": [],
        "representation_sha256": hashlib.sha256(b"[]").hexdigest(),
        "geometry_sha256": hashlib.sha256(b"[]").hexdigest(),
    }
    if surface_water_normalization_enabled and surface_centreline_records:
        mapped_surface_area = (
            unary_union([area for _source, area in surface_water_area_records])
            if surface_water_area_records
            else GeometryCollection()
        )
        boundary_refs_by_part = {
            stroke.part: _stroke_source_references(stroke)
            for stroke in normalized_strokes
        }
        emitted_boundary_refs = (
            set().union(*boundary_refs_by_part.values())
            if boundary_refs_by_part
            else set()
        )
        audit_by_ref: dict[str, dict[str, Any]] = {}
        visible_centre_records: list[tuple[str, MapFeature, LineString]] = []

        for target_layer, source, line in surface_centreline_records:
            source_ref = _source_reference(source)
            audit = audit_by_ref.setdefault(
                source_ref,
                {
                    "source": source,
                    "target_layer": target_layer,
                    "input_lines": [],
                    "visible_lines": [],
                    "suppressed_lines": [],
                    "input_length_mm": 0.0,
                    "visible_length_mm": 0.0,
                    "polygon_suppressed_length_mm": 0.0,
                    "flank_suppressed_length_mm": 0.0,
                    "representation_target_refs": set(),
                },
            )
            if audit["target_layer"] != target_layer:
                raise MapPlotterError(
                    "One waterway source resolved to conflicting physical layers: "
                    f"{source_ref}."
                )
            audit["input_lines"].append(line)
            audit["input_length_mm"] += line.length

            polygon_visible = [line]
            polygon_suppressed = 0.0
            if not mapped_surface_area.is_empty:
                polygon_result = suppress_river_centerline_segments(
                    tuple((float(x), float(y)) for x, y in line.coords),
                    mapped_surface_area,
                    mask_buffer_mm=0.03,
                )
                polygon_visible = [
                    LineString(points) for points in polygon_result.visible_parts
                ]
                polygon_suppressed = polygon_result.suppressed_length_mm

            final_visible: list[LineString] = []
            flank_suppressed = 0.0
            for polygon_part in polygon_visible:
                if detail_profile == "ink-balanced":
                    visible_parts, removed = _flanked_centerline_suppression(
                        polygon_part,
                        surface_boundary_union,
                    )
                else:
                    # Dotted faithful output suppresses only geometry proved to
                    # lie inside a mapped surface. Nearby standalone streams
                    # and canals must never disappear merely because unrelated
                    # banks happen to flank them.
                    visible_parts, removed = [polygon_part], 0.0
                final_visible.extend(visible_parts)
                flank_suppressed += removed

            visible_union: BaseGeometry = (
                unary_union(final_visible) if final_visible else GeometryCollection()
            )
            suppressed_geometry = line.difference(visible_union)
            suppressed_parts = list(_line_parts(suppressed_geometry))
            suppressed_length = sum(part.length for part in suppressed_parts)
            target_refs: set[str] = set()
            if suppressed_length > 1e-6:
                for area_source, area in surface_water_area_records:
                    if (
                        suppressed_geometry.intersection(area.buffer(0.03)).length
                        > 1e-6
                    ):
                        target_refs.update(_surface_area_source_refs(area_source))
                for suppressed_part in suppressed_parts:
                    midpoint = suppressed_part.interpolate(suppressed_part.length / 2)
                    nearest_boundary = surface_boundary_union.distance(midpoint)
                    search_radius = max(0.75, nearest_boundary * 2.5 + 0.25)
                    search_area = suppressed_part.buffer(search_radius)
                    for boundary_source, boundary in surface_boundary_records:
                        if boundary.intersects(search_area):
                            target_refs.add(_source_reference(boundary_source))
                target_refs &= emitted_boundary_refs

            # Suppression without a drawable representation carrier would be a
            # silent source deletion. Keep the centreline in that exceptional case.
            if suppressed_length > 1e-6 and not target_refs:
                final_visible = [line]
                suppressed_parts = []
                polygon_suppressed = 0.0
                flank_suppressed = 0.0
                suppressed_length = 0.0

            visible_length = sum(part.length for part in final_visible)
            audit["visible_lines"].extend(final_visible)
            audit["suppressed_lines"].extend(suppressed_parts)
            audit["visible_length_mm"] += visible_length
            audit["polygon_suppressed_length_mm"] += polygon_suppressed
            audit["flank_suppressed_length_mm"] += flank_suppressed
            audit["representation_target_refs"].update(target_refs)
            visible_centre_records.extend(
                (target_layer, source, part) for part in final_visible
            )

        source_records: list[dict[str, Any]] = []
        representation_links: list[dict[str, Any]] = []
        fully_suppressed_refs: list[str] = []
        partially_suppressed_refs: list[str] = []
        standalone_retained_refs: list[str] = []
        visible_refs: list[str] = []
        for source_ref, audit in sorted(audit_by_ref.items()):
            input_length = float(audit["input_length_mm"])
            visible_length = float(audit["visible_length_mm"])
            suppressed_length = max(0.0, input_length - visible_length)
            if visible_length > 1e-6:
                visible_refs.append(source_ref)
            status = "visible"
            if suppressed_length > 1e-6 and visible_length <= 1e-6:
                status = "fully_suppressed"
                fully_suppressed_refs.append(source_ref)
            elif suppressed_length > 1e-6:
                status = "partially_suppressed"
                partially_suppressed_refs.append(source_ref)
            elif visible_length > 1e-6:
                standalone_retained_refs.append(source_ref)
            resolved_target_refs = sorted(audit["representation_target_refs"])
            boundary_parts = sorted(
                part
                for part, refs in boundary_refs_by_part.items()
                if refs & set(resolved_target_refs)
            )
            source_record = {
                "source_ref": source_ref,
                "waterway": audit["source"].tags.get("waterway", ""),
                "target_layer": audit["target_layer"],
                "status": status,
                "input_length_mm": round(input_length, 9),
                "visible_length_mm": round(visible_length, 9),
                "suppressed_length_mm": round(suppressed_length, 9),
                "polygon_suppressed_length_mm": round(
                    float(audit["polygon_suppressed_length_mm"]), 9
                ),
                "flank_suppressed_length_mm": round(
                    float(audit["flank_suppressed_length_mm"]), 9
                ),
                "input_geometry_sha256": _line_geometry_sha256(audit["input_lines"]),
                "visible_geometry_sha256": _line_geometry_sha256(
                    audit["visible_lines"]
                ),
                "representation_target_refs": resolved_target_refs,
            }
            source_records.append(source_record)
            if suppressed_length > 1e-6:
                representation_links.append(
                    {
                        "source_ref": source_ref,
                        "target_layer": audit["target_layer"],
                        "status": status,
                        "suppressed_length_mm": round(suppressed_length, 9),
                        "representation_target_refs": resolved_target_refs,
                        "representation_boundary_parts": boundary_parts,
                    }
                )

        links_by_source = {link["source_ref"]: link for link in representation_links}
        for source_ref in fully_suppressed_refs:
            link = links_by_source[source_ref]
            targets = set(link["representation_target_refs"])
            attached = False
            for stroke in normalized_strokes:
                base_refs = boundary_refs_by_part.get(stroke.part, set())
                if not base_refs & targets:
                    continue
                refs = _stroke_source_references(stroke) | {source_ref}
                equivalents = {
                    item
                    for item in stroke.tags.get(
                        "represented-centreline-source-refs", ""
                    ).split(";")
                    if item
                }
                equivalents.add(source_ref)
                stroke.tags["source-refs"] = ";".join(sorted(refs))
                stroke.tags["source-count"] = str(len(refs))
                stroke.tags["represented-centreline-source-refs"] = ";".join(
                    sorted(equivalents)
                )
                attached = True
            if not attached:
                raise MapPlotterError(
                    "A fully suppressed surface-water centreline has no boundary "
                    f"representation carrier: {source_ref}."
                )

        geometry_records = [
            {
                "source_ref": record["source_ref"],
                "target_layer": record["target_layer"],
                "input_geometry_sha256": record["input_geometry_sha256"],
                "visible_geometry_sha256": record["visible_geometry_sha256"],
            }
            for record in source_records
        ]
        centreline_suppression = {
            "enabled": True,
            "input_path_count": len(surface_centreline_records),
            "input_source_ref_count": len(source_records),
            "input_source_refs": [record["source_ref"] for record in source_records],
            "input_length_mm": round(
                sum(float(record["input_length_mm"]) for record in source_records), 9
            ),
            "visible_path_count": len(visible_centre_records),
            "visible_source_ref_count": len(visible_refs),
            "visible_source_refs": visible_refs,
            "visible_length_mm": round(
                sum(float(record["visible_length_mm"]) for record in source_records),
                9,
            ),
            "partially_suppressed_source_ref_count": len(partially_suppressed_refs),
            "partially_suppressed_source_refs": partially_suppressed_refs,
            "fully_suppressed_source_ref_count": len(fully_suppressed_refs),
            "fully_suppressed_source_refs": fully_suppressed_refs,
            "standalone_retained_source_ref_count": len(standalone_retained_refs),
            "standalone_retained_source_refs": standalone_retained_refs,
            "polygon_suppressed_length_mm": round(
                sum(
                    float(record["polygon_suppressed_length_mm"])
                    for record in source_records
                ),
                9,
            ),
            "flank_suppressed_length_mm": round(
                sum(
                    float(record["flank_suppressed_length_mm"])
                    for record in source_records
                ),
                9,
            ),
            "suppressed_length_mm": round(
                sum(float(record["suppressed_length_mm"]) for record in source_records),
                9,
            ),
            "source_records": source_records,
            "representation_link_count": len(representation_links),
            "representation_links": representation_links,
            "representation_sha256": hashlib.sha256(
                json.dumps(
                    representation_links,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
            "geometry_sha256": hashlib.sha256(
                json.dumps(
                    geometry_records,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
        }
        river_centre_records = [
            (source, line)
            for target_layer, source, line in visible_centre_records
            if target_layer == "rivers"
        ]
        narrow_water_records = [
            *untouched_narrow_water_records,
            *(
                (source, line)
                for target_layer, source, line in visible_centre_records
                if target_layer == "waterways"
            ),
        ]

    mapped_river_area = (
        unary_union(river_area_geometries)
        if river_area_geometries
        else GeometryCollection()
    )
    suppressed_rivers = 0
    polygon_suppressed_rivers = 0
    partially_polygon_suppressed_rivers = 0
    polygon_suppressed_length_mm = 0.0
    fallback_suppressed_rivers = 0
    river_groups: dict[tuple[str, str], list[tuple[MapFeature, LineString]]] = (
        defaultdict(list)
    )
    for source, line in river_centre_records:
        river_groups[
            (source.tags.get("waterway", ""), (source.name or "").casefold())
        ].append((source, line))
    river_index = 0
    for records in river_groups.values():
        river_source_index = _SourceLineIndex.build(records)
        for river in _merge_lines([line for _, line in records]):
            visible_rivers = [river]
            if not full_cartography and not mapped_river_area.is_empty:
                suppression = suppress_river_centerline_segments(
                    tuple((float(x), float(y)) for x, y in river.coords),
                    mapped_river_area,
                    mask_buffer_mm=0.03,
                )
                polygon_suppressed_length_mm += suppression.suppressed_length_mm
                if suppression.fully_suppressed:
                    suppressed_rivers += 1
                    polygon_suppressed_rivers += 1
                    continue
                if suppression.suppressed_length_mm > 1e-6:
                    partially_polygon_suppressed_rivers += 1
                visible_rivers = [
                    LineString(points) for points in suppression.visible_parts
                ]

            for visible_river in visible_rivers:
                # Boundary fragments from incomplete legacy relation geometry
                # cannot form an area mask. Keep the conservative bank-probe
                # fallback for those records, after polygon subtraction.
                if not full_cartography and _river_is_flanked(
                    visible_river, fallback_bank_union
                ):
                    suppressed_rivers += 1
                    fallback_suppressed_rivers += 1
                    continue
                contributors = river_source_index.contributors(visible_river)
                if not contributors:
                    contributors = river_source_index.contributors(river)
                source = contributors[0] if contributors else records[0][0]
                ids = {item.osm_id for item in contributors}
                osm_types = {item.osm_type for item in contributors}
                names = {item.name for item in contributors if item.name}
                simplified = cast(
                    LineString,
                    _simplify(
                        visible_river,
                        (simplify_mm if full_cartography else max(0.10, simplify_mm)),
                    ),
                )
                strokes.append(
                    _stroke_from_line(
                        "rivers",
                        simplified,
                        part=f"unbanked-river:{river_index}",
                        smooth=(not full_cartography and len(simplified.coords) >= 3),
                        tags={
                            **source.tags,
                            "waterway": source.tags.get("waterway", "river"),
                            "compiled": preset,
                            "source-count": str(len(contributors)),
                            "source-refs": _source_references(contributors),
                        },
                        osm_type=(
                            next(iter(osm_types)) if len(osm_types) == 1 else "compiled"
                        ),
                        osm_id=source.osm_id if len(ids) == 1 else "multiple",
                        name=next(iter(names)) if len(names) == 1 else None,
                    )
                )
                river_index += 1

    narrow_groups: dict[tuple[str, str], list[tuple[MapFeature, LineString]]] = (
        defaultdict(list)
    )
    for source, line in narrow_water_records:
        narrow_groups[
            (source.tags.get("waterway", ""), (source.name or "").casefold())
        ].append((source, line))
    waterway_index = 0
    for records in narrow_groups.values():
        waterway_source_index = _SourceLineIndex.build(records)
        for waterway in _merge_lines([line for _, line in records]):
            contributors = waterway_source_index.contributors(waterway)
            source = contributors[0] if contributors else records[0][0]
            ids = {item.osm_id for item in contributors}
            osm_types = {item.osm_type for item in contributors}
            names = {item.name for item in contributors if item.name}
            simplified = cast(
                LineString,
                _simplify(
                    waterway,
                    (
                        simplify_mm
                        if full_cartography
                        else max(0.06, simplify_mm * 0.75)
                    ),
                    preserve_topology=full_cartography,
                ),
            )
            strokes.append(
                _stroke_from_line(
                    "waterways",
                    simplified,
                    part=f"narrow-waterway:{waterway_index}",
                    smooth=(not full_cartography and len(simplified.coords) >= 3),
                    tags={
                        **source.tags,
                        "compiled": preset,
                        "source-count": str(len(contributors)),
                        "source-refs": _source_references(contributors),
                    },
                    osm_type=(
                        next(iter(osm_types)) if len(osm_types) == 1 else "compiled"
                    ),
                    osm_id=source.osm_id if len(ids) == 1 else "multiple",
                    name=next(iter(names)) if len(names) == 1 else None,
                )
            )
            waterway_index += 1

    landmark_selection_diagnostics: dict[str, Any] = {
        "candidate_object_count": 0,
        "selected_object_count": 0,
        "selected_outline_length_mm": 0.0,
        "selected_ink_area_mm2": 0.0,
        "selected_by_role": {},
    }
    # A race course drawn on top of the streets it follows reads as a traced
    # highlight, not as a route: the road showing through its middle makes a
    # 1.2 mm mark look like two thin ones. Clearing a corridor underneath means
    # the course sits ON the sheet rather than over other ink, and it also
    # removes the pen-on-pen overlap that would bleed on absorbent stock.
    course_corridor: BaseGeometry | None = None
    course_records = passthrough.get(COURSE_LAYER) or []
    if course_records and course_clearance_mm > 0:
        course_lines = [line for _source, line in course_records if not line.is_empty]
        if course_lines:
            course_corridor = unary_union(
                [line.buffer(course_clearance_mm, cap_style="round") for line in course_lines]
            )

    for layer, records in passthrough.items():
        compiled_records: list[tuple[MapFeature | None, LineString]]
        if layer in AREA_OUTLINE_LAYERS:
            compiled_records = list(records)
            if layer == "buildings" and landmark_buildings:
                landmark_grouped: dict[str, list[tuple[MapFeature, LineString]]] = (
                    defaultdict(list)
                )
                for landmark_source, landmark_line in compiled_records:
                    if landmark_source is None:
                        continue
                    key = landmark_source.tags.get(
                        "mapplot:landmark-object",
                        f"{landmark_source.osm_type}/{landmark_source.osm_id}",
                    )
                    landmark_grouped[key].append((landmark_source, landmark_line))
                source_object_count = len(landmark_grouped)
                geometry_buckets: dict[
                    tuple[tuple[tuple[float, float], ...], ...],
                    list[tuple[str, list[tuple[MapFeature, LineString]]]],
                ] = defaultdict(list)
                for object_key, landmark_group_records in landmark_grouped.items():
                    geometry_buckets[
                        _landmark_geometry_signature(landmark_group_records)
                    ].append((object_key, landmark_group_records))
                deduplicated_grouped: dict[
                    str, list[tuple[MapFeature, LineString]]
                ] = {}
                duplicate_object_count = 0
                duplicate_groups: list[dict[str, Any]] = []
                for coincident_objects in geometry_buckets.values():
                    representative_key, representative_records = min(
                        coincident_objects,
                        key=lambda item: (
                            0 if item[0].startswith("relation/") else 1,
                            -int(item[1][0][0].tags.get("mapplot:landmark-rank", "0")),
                            -len(item[1]),
                            item[0],
                        ),
                    )
                    represented_refs = sorted(
                        {
                            source_ref
                            for _object_key, object_records in coincident_objects
                            for source, _line in object_records
                            for source_ref in _landmark_source_refs(source)
                        }
                    )
                    object_keys = sorted(
                        object_key for object_key, _records in coincident_objects
                    )
                    duplicate_object_count += len(coincident_objects) - 1
                    if len(coincident_objects) > 1:
                        duplicate_groups.append(
                            {
                                "representative_object": representative_key,
                                "coincident_objects": object_keys,
                                "represented_source_refs": represented_refs,
                            }
                        )
                    combined_refs = ";".join(represented_refs)
                    # Every object in this bucket has the same complete
                    # exterior signature.  Emit that exterior exactly once,
                    # but retain the union of distinct inner rings: a higher-
                    # ranked canonical relation can legitimately omit a
                    # courtyard or stadium bowl carried by a coincident
                    # relation.  Prefer the representative copy of an inner
                    # ring when more than one object supplies it.
                    canonical_records = [
                        record
                        for record in representative_records
                        if record[0].ring_role != "inner"
                    ]
                    if not canonical_records:
                        canonical_records = list(representative_records)
                    inner_by_signature: dict[
                        tuple[tuple[float, float], ...],
                        tuple[MapFeature, LineString],
                    ] = {}
                    ordered_objects = [
                        (representative_key, representative_records),
                        *sorted(
                            (
                                item
                                for item in coincident_objects
                                if item[0] != representative_key
                            ),
                            key=lambda item: item[0],
                        ),
                    ]
                    for _object_key, object_records in ordered_objects:
                        for source, line in object_records:
                            if source.ring_role != "inner":
                                continue
                            inner_by_signature.setdefault(
                                _canonical_line_coordinates(line),
                                (source, line),
                            )
                    canonical_records.extend(
                        inner_by_signature[signature]
                        for signature in sorted(inner_by_signature)
                    )
                    representative_landmark_tags = {
                        key: value
                        for key, value in representative_records[0][0].tags.items()
                        if key.startswith("mapplot:landmark-")
                    }
                    deduplicated: list[tuple[MapFeature, LineString]] = []
                    for source, line in canonical_records:
                        merged_feature = replace(
                            source,
                            tags={
                                **source.tags,
                                **representative_landmark_tags,
                                "mapplot:landmark-object": representative_key,
                                "mapplot:landmark-source-count": str(
                                    len(represented_refs)
                                ),
                                "mapplot:landmark-source-refs": combined_refs,
                                "mapplot:coincident-object-count": str(
                                    len(coincident_objects)
                                ),
                            },
                        )
                        # Same identity trap as the landmark re-tag above:
                        # `replace` mints a new object, so its projected area
                        # has to be carried across or the buildings area budget
                        # cannot look it up.
                        if id(source) in projected_areas:
                            projected_areas[id(merged_feature)] = projected_areas[
                                id(source)
                            ]
                        deduplicated.append((merged_feature, line))
                    deduplicated_grouped[representative_key] = deduplicated
                landmark_grouped = deduplicated_grouped
                outline_budget_mm = (
                    landmark_policy.ink_budget_field_fraction
                    * layout.map_width_mm
                    * layout.map_height_mm
                    / landmark_nib_mm
                )
                candidates: list[tuple[str, list[tuple[MapFeature, LineString]]]] = (
                    list(landmark_grouped.items())
                )
                pinned_group_keys: set[str] = set()
                required_groups: list[
                    tuple[str, list[tuple[MapFeature, LineString]]]
                ] = []
                required_group_refs: dict[str, tuple[str, ...]] = {}
                for object_key, group_records in candidates:
                    represented_source_refs = tuple(
                        sorted(
                            {
                                source_ref
                                for source, _line in group_records
                                for source_ref in _landmark_source_refs(source)
                            }
                        )
                    )
                    represented_object_refs = {
                        _source_object_reference(source_ref)
                        for source_ref in represented_source_refs
                    }
                    represented_required_refs = sorted(
                        represented_object_refs & requested_landmark_refs
                    )
                    if not represented_required_refs:
                        continue
                    pinned_group_keys.add(object_key)
                    eligible_required_refs = [
                        required_ref
                        for required_ref in represented_required_refs
                        if required_landmark_dispositions[required_ref]["status"]
                        == "eligible"
                    ]
                    if not eligible_required_refs:
                        continue
                    required_groups.append((object_key, group_records))
                    required_group_refs[object_key] = represented_source_refs

                represented_eligible_refs = {
                    required_ref
                    for object_key, _group_records in required_groups
                    for required_ref in requested_landmark_refs
                    if any(
                        _source_object_reference(source_ref) == required_ref
                        for source_ref in required_group_refs[object_key]
                    )
                }
                for required_ref in landmark_refs:
                    disposition = required_landmark_dispositions[required_ref]
                    if (
                        disposition["status"] == "eligible"
                        and required_ref not in represented_eligible_refs
                    ):
                        disposition.update(
                            status="ineligible",
                            reason=(
                                "eligible footprint did not survive canonical "
                                "landmark grouping"
                            ),
                        )

                required_groups = sorted(required_groups, key=lambda item: item[0])
                required_length_mm = sum(
                    line.length
                    for _object_key, group_records in required_groups
                    for _source, line in group_records
                )
                required_capacity_ok = (
                    len(required_groups) <= landmark_policy.max_objects
                )
                required_budget_ok = required_length_mm <= outline_budget_mm + 1e-9
                selected_groups: list[
                    tuple[str, list[tuple[MapFeature, LineString]]]
                ] = []
                selected_length_mm = 0.0
                if required_capacity_ok and required_budget_ok:
                    selected_groups.extend(required_groups)
                    selected_length_mm = required_length_mm
                    for object_key, _group_records in required_groups:
                        represented_source_refs = required_group_refs[object_key]
                        represented_object_refs = {
                            _source_object_reference(source_ref)
                            for source_ref in represented_source_refs
                        }
                        for required_ref in sorted(
                            represented_object_refs & requested_landmark_refs
                        ):
                            disposition = required_landmark_dispositions[required_ref]
                            if disposition["status"] != "eligible":
                                continue
                            disposition.update(
                                status="selected",
                                reason=(
                                    "reserved before generic role quotas and "
                                    "within total landmark gates"
                                ),
                                representative_object=object_key,
                                represented_source_refs=list(represented_source_refs),
                            )
                elif required_groups:
                    budget_reason = (
                        f"required landmark set needs {len(required_groups)} objects; "
                        f"total maximum is {landmark_policy.max_objects}"
                        if not required_capacity_ok
                        else (
                            "required landmark set needs "
                            f"{required_length_mm:.6f} mm of outline; landmark "
                            f"budget allows {outline_budget_mm:.6f} mm"
                        )
                    )
                    for object_key, _group_records in required_groups:
                        represented_object_refs = {
                            _source_object_reference(source_ref)
                            for source_ref in required_group_refs[object_key]
                        }
                        for required_ref in sorted(
                            represented_object_refs & requested_landmark_refs
                        ):
                            disposition = required_landmark_dispositions[required_ref]
                            if disposition["status"] == "eligible":
                                disposition.update(
                                    status="budget_omitted",
                                    reason=budget_reason,
                                    representative_object=object_key,
                                    represented_source_refs=list(
                                        required_group_refs[object_key]
                                    ),
                                )

                generic_candidates = [
                    item for item in candidates if item[0] not in pinned_group_keys
                ]
                role_queues: dict[
                    str,
                    list[tuple[str, list[tuple[MapFeature, LineString]]]],
                ] = {}
                for bucket, roles, limit in landmark_policy.role_buckets:
                    bucket_candidates = [
                        item
                        for item in generic_candidates
                        if item[1][0][0].tags.get("mapplot:landmark-role") in roles
                    ]
                    role_queues[bucket] = sorted(
                        bucket_candidates,
                        key=lambda item: (
                            -int(item[1][0][0].tags.get("mapplot:landmark-rank", "0")),
                            -float(
                                item[1][0][0].tags.get(
                                    "mapplot:projected-area-mm2", "0"
                                )
                            ),
                            item[0],
                        ),
                    )[:limit]
                while (
                    any(role_queues.values())
                    and len(selected_groups) < landmark_policy.max_objects
                ):
                    for bucket, _roles, _limit in landmark_policy.role_buckets:
                        if len(selected_groups) >= landmark_policy.max_objects:
                            break
                        queue = role_queues[bucket]
                        if not queue:
                            continue
                        object_key, landmark_group_records = queue.pop(0)
                        group_length = sum(
                            line.length for _source, line in landmark_group_records
                        )
                        if selected_length_mm + group_length > outline_budget_mm + 1e-9:
                            continue
                        selected_groups.append((object_key, landmark_group_records))
                        selected_length_mm += group_length
                compiled_records = [
                    record
                    for _object_key, landmark_group_records in selected_groups
                    for record in landmark_group_records
                ]
                selected_object_keys = {
                    object_key for object_key, _records in selected_groups
                }
                for object_key, landmark_group_records in landmark_grouped.items():
                    if object_key in selected_object_keys:
                        continue
                    excluded_landmark_budget_refs.extend(
                        source_ref
                        for landmark_source, _line in landmark_group_records
                        for source_ref in _landmark_source_refs(landmark_source)
                    )
                selected_by_role: dict[str, int] = defaultdict(int)
                for _object_key, landmark_group_records in selected_groups:
                    landmark_source = landmark_group_records[0][0]
                    selected_by_role[
                        landmark_source.tags.get("mapplot:landmark-role", "unknown")
                    ] += 1
                selected_ink_area_mm2 = selected_length_mm * landmark_nib_mm
                landmark_selection_diagnostics = {
                    "source_object_count": source_object_count,
                    "candidate_object_count": len(landmark_grouped),
                    "coincident_duplicate_object_count": duplicate_object_count,
                    "coincident_duplicate_groups": duplicate_groups,
                    "selected_object_count": len(selected_groups),
                    "maximum_object_count": landmark_policy.max_objects,
                    "required_group_count": len(required_groups),
                    "required_outline_length_mm": round(required_length_mm, 3),
                    "required_set_capacity_ok": required_capacity_ok,
                    "required_set_ink_budget_ok": required_budget_ok,
                    "selected_outline_length_mm": round(selected_length_mm, 3),
                    "outline_budget_mm": round(outline_budget_mm, 3),
                    "selected_ink_area_mm2": round(selected_ink_area_mm2, 3),
                    "ink_budget_mm2": round(
                        landmark_policy.ink_budget_field_fraction
                        * layout.map_width_mm
                        * layout.map_height_mm,
                        3,
                    ),
                    "ink_coverage": round(
                        selected_ink_area_mm2
                        / (layout.map_width_mm * layout.map_height_mm),
                        9,
                    ),
                    "selected_by_role": dict(sorted(selected_by_role.items())),
                    "role_buckets": [
                        {
                            "id": bucket,
                            "roles": sorted(roles),
                            "maximum_objects": limit,
                        }
                        for bucket, roles, limit in landmark_policy.role_buckets
                    ],
                }
            budget_mm: float | None = None
            minimum_mm = 0.0
            map_area_mm2 = layout.map_width_mm * layout.map_height_mm
            if not full and layer == "green_space":
                budget_mm = (0.0358 if balanced else 0.0228) * map_area_mm2
                minimum_mm = 2.0
            elif not full and layer == "buildings":
                budget_mm = (0.0975 if balanced else 0.0455) * map_area_mm2
                minimum_mm = 1.2
            if budget_mm is not None:
                candidate_count = len(compiled_records)
                retained_records: list[tuple[MapFeature | None, LineString]] = []
                used_mm = 0.0
                for record in sorted(
                    compiled_records,
                    key=lambda item: (
                        projected_areas[id(item[0])].area if item[0] is not None else 0,
                        item[1].length,
                    ),
                    reverse=True,
                ):
                    if record[1].length < minimum_mm:
                        continue
                    if used_mm + record[1].length > budget_mm and retained_records:
                        continue
                    retained_records.append(record)
                    used_mm += record[1].length
                compiled_records = retained_records
                omitted_area_budget_strokes += candidate_count - len(compiled_records)
        else:
            compiled_records = list(records)
            if balanced and layer == "railways":
                source_by_line = {id(line): source for source, line in records}
                compiled_lines = [line for _, line in records]
                compiled_lines = _suppress_parallel_lines(
                    compiled_lines, distance_mm=0.35, ratio=0.78
                )
                compiled_lines = _within_length_budget(
                    compiled_lines,
                    budget_mm=0.0325 * layout.map_width_mm * layout.map_height_mm,
                    minimum_mm=1.0,
                )
                compiled_records = [
                    (source_by_line[id(line)], line) for line in compiled_lines
                ]
        if course_corridor is not None and layer != COURSE_LAYER:
            cleared: list[tuple[MapFeature | None, LineString]] = []
            for compiled_source, line in compiled_records:
                remainder = line.difference(course_corridor)
                if remainder.is_empty:
                    course_cleared_strokes += 1
                    continue
                parts = list(_line_parts(remainder))
                if not parts:
                    course_cleared_strokes += 1
                    continue
                # A segment split by the corridor becomes two, each keeping its
                # own source lineage: nothing is invented, only interrupted.
                if len(parts) > 1 or parts[0].length + 1e-9 < line.length:
                    course_cleared_mm += line.length - sum(p.length for p in parts)
                cleared.extend((compiled_source, part) for part in parts)
            compiled_records = cleared

        for index, (passthrough_source, line) in enumerate(compiled_records):
            simplified = cast(LineString, _simplify(line, simplify_mm))
            tags = {"compiled": preset}
            if passthrough_source is not None:
                tags.update(passthrough_source.tags)
                source_refs = _landmark_source_refs(passthrough_source)
                tags["source-count"] = str(len(source_refs))
                tags["source-refs"] = ";".join(source_refs)
            strokes.append(
                _stroke_from_line(
                    layer,
                    simplified,
                    part=f"compiled:{index}",
                    smooth=False,
                    tags=tags,
                    osm_type=(
                        passthrough_source.osm_type
                        if passthrough_source is not None
                        else "compiled"
                    ),
                    osm_id=(
                        passthrough_source.osm_id
                        if passthrough_source is not None
                        else "multiple"
                    ),
                    name=(
                        passthrough_source.name
                        if passthrough_source is not None
                        else None
                    ),
                )
            )

    warnings: list[str] = []
    if river_bank_crop_warning:
        warnings.append(
            "Some water polygons reach the map crop edge; their visible banks were merged as linework."
        )

    surface_water_normalization = {
        "schema_version": 2,
        "policy": (
            "ink-balanced-surface-water-v2"
            if detail_profile == "ink-balanced"
            else "dotted-surface-water-v1"
        ),
        "enabled": surface_water_normalization_enabled,
        "subterranean_exclusions": subterranean_exclusions,
        "semantic_bay_boundary_suppression": (
            semantic_bay_boundary_diagnostics
        ),
        "centreline_suppression": centreline_suppression,
        "bridge_boundary_knockouts": bridge_boundary_knockouts,
    }
    excluded_water_refs = [str(entry["source_ref"]) for entry in subterranean_entries]
    diagnostics = {
        "preset": preset,
        "detail_profile": detail_profile,
        "profile_semantics": {
            "full_cartographic_selection": full_cartography,
            "post_cartography_ink_budget_required": (
                detail_profile in INK_BUDGETED_DETAIL_PROFILES
            ),
            "source_complete_output_required": (
                detail_profile in SOURCE_COMPLETE_DETAIL_PROFILES
            ),
        },
        "semantic_selection_enabled": not full,
        "curve_smoothing_enabled": (
            not full_cartography
            or bool(topology_diagnostics.get("rounding", {}).get("enabled", False))
        ),
        "source_features_projected": len(projected),
        "retained_road_features": retained_road_features,
        "omitted_path_features": omitted_paths,
        "omitted_service_features": omitted_service,
        "omitted_parallel_path_features": omitted_parallel_paths,
        "retained_path_features": retained_path_features,
        "retained_service_features": retained_service_features,
        "omitted_context_features": omitted_context_features,
        "omitted_detail_budget_features": omitted_detail_budget_features,
        "omitted_area_budget_strokes": omitted_area_budget_strokes,
        "omitted_short_road_strokes": omitted_short_road_strokes,
        "merged_road_strokes": merged_road_count,
        "road_topology_generalization": topology_diagnostics,
        "river_bank_strokes": bank_stroke_count,
        "retained_other_water_area_strokes": retained_water_area_count,
        "omitted_tiny_water_area_strokes": omitted_tiny_water_areas,
        "suppressed_banked_river_centrelines": suppressed_rivers,
        "polygon_suppressed_river_centrelines": polygon_suppressed_rivers,
        "partially_polygon_suppressed_river_centrelines": (
            partially_polygon_suppressed_rivers
        ),
        "polygon_suppressed_river_length_mm": round(polygon_suppressed_length_mm, 3),
        "fallback_suppressed_river_centrelines": fallback_suppressed_rivers,
        "assembled_inner_ring_count": sum(
            feature.geometry_type == "polygon_ring" and feature.ring_role == "inner"
            for feature in features
        ),
        "bridge_water_knockouts": (
            len(relevant_bridge_buffers)
            if detail_profile == "ink-balanced"
            else 0
            if full_cartography
            else len(relevant_bridge_buffers)
        ),
        "water_simplify_tolerance_mm": round(water_tolerance, 3),
        "water_boundary_normalization": water_boundary_normalization,
        "surface_water_normalization": surface_water_normalization,
        "coastline_water_mask": coastline_water_mask_diagnostics,
        "water_stipple": water_stipple_diagnostics,
        "landmark_buildings": {
            "enabled": landmark_buildings,
            "policy": (
                "semantic prominent-footprint selector with physical thresholds, "
                "role quotas, and the active plate's landmark ink budget"
                if landmark_buildings
                else "all selected buildings"
            ),
            "plate_policy": landmark_policy.as_dict(),
            "format_id": layout.format_id,
            "minimum_oriented_span_mm": landmark_policy.minimum_oriented_span_mm,
            "minimum_visible_perimeter_mm": (
                landmark_policy.minimum_visible_perimeter_mm
            ),
            "physical_nib_mm": landmark_nib_mm,
            "ink_budget_field_fraction": landmark_policy.ink_budget_field_fraction,
            "omitted_non_landmark_source_count": len(set(excluded_non_landmark_refs)),
            "omitted_landmark_budget_source_count": len(
                set(excluded_landmark_budget_refs)
            ),
            "selection": landmark_selection_diagnostics,
            "must_have": {
                "schema_version": 1,
                "policy_id": "required-landmark-footprint-v1",
                "requested_refs": list(landmark_refs),
                "dispositions": [
                    required_landmark_dispositions[required_ref]
                    for required_ref in landmark_refs
                ],
                "all_selected": all(
                    required_landmark_dispositions[required_ref]["status"] == "selected"
                    for required_ref in landmark_refs
                ),
            },
        },
        "bezier_smoothing": not full_cartography,
        "source_lineage": _source_lineage_summary(
            projected,
            strokes,
            explicitly_excluded_source_refs=(
                *excluded_water_refs,
                *excluded_non_landmark_refs,
                *excluded_landmark_budget_refs,
            ),
        ),
        "loss_report": {
            "profile": detail_profile,
            # These categories deliberately remain separate: a service path
            # can belong to more than one category, so summing them would
            # overstate the number of unique omitted source features.
            "omission_counts": {
                "path_features": omitted_paths,
                "service_features": omitted_service,
                "detail_budget_features": omitted_detail_budget_features,
                "parallel_path_features": omitted_parallel_paths,
                "cartographic_short_road_strokes": omitted_short_road_strokes,
                "context_features": omitted_context_features,
                "area_budget_strokes": omitted_area_budget_strokes,
                "tiny_water_area_strokes": omitted_tiny_water_areas,
                "non_landmark_building_sources": len(set(excluded_non_landmark_refs)),
                "landmark_budget_sources": len(set(excluded_landmark_budget_refs)),
            },
        },
    }
    return CartographyResult(strokes, diagnostics, warnings)


def prepare_map_strokes(
    features: list[MapFeature],
    layout: Layout,
    *,
    simplify_mm: float,
    detail_profile: str = "plot",
    water_fill: str = "none",
    landmark_buildings: bool = False,
    landmark_refs: tuple[str, ...] = (),
    landmark_source_tags: dict[str, dict[str, str] | None] | None = None,
    water_dot_nib_mm: float = 0.25,
    landmark_nib_mm: float = 0.25,
    course_clearance_mm: float = 0.0,
) -> CartographyResult:
    """Compile every rendering profile through the shared geometry pipeline."""

    return prepare_clean_poster_strokes(
        features,
        layout,
        simplify_mm=simplify_mm,
        detail_profile=detail_profile,
        water_fill=water_fill,
        landmark_buildings=landmark_buildings,
        landmark_refs=landmark_refs,
        landmark_source_tags=landmark_source_tags,
        water_dot_nib_mm=water_dot_nib_mm,
        landmark_nib_mm=landmark_nib_mm,
        course_clearance_mm=course_clearance_mm,
    )
