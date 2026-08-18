"""Source-qualified 2026 circuit atlas plates for physical pen plotting.

This domain deliberately does not reuse the older generic sports circuit
prototype.  A red line is a factual claim: it is emitted only from the ordered,
closed lap centreline supplied in a frozen catalog.  Track width, racing lines,
apexes, pit geometry, and temporary venue context are never inferred.

Normalized geometry contract (``event.circuit.geometry.model``)
----------------------------------------------------------------

The renderer accepts a north-up local engineering plane with metres as units::

    {
      "coordinate_system": "local-metre",
      "origin_wgs84": [longitude, latitude],
      "lap": {"type": "LineString", "coordinates": [[x, y], ...]},
      "lap_source_objects": [...],
      "pit_lanes": [{"geometry": <GeoJSON>, "source_objects": [...]}],
      "track_boundaries": [<GeoJSON Feature>, ...],
      "context": [{
        "id": "...", "kind": "water|road|grass|woodland|...",
        "name": null, "geometry": <GeoJSON>, "source_ref": "...",
        "source_objects": [...], "tags": {},
        "temporary_status": "permanent|temporary|...",
        "valid_for_season": 2026
      }],
      "turn_stations": [{
        "id": "turn-1", "number": 1, "name": null,
        "chainage_m": 0.0, "point": [x, y], "source_ref": "...",
        "anchor_method": "geometry-chainage|licensed-coordinate|...",
        "status": "official|geometric-station|true-apex"
      }],
      "start_finish": {"point": [x, y], "source_ref": "...", "status": "..."},
      "special_sections": [...]
    }

Bare GeoJSON geometries and GeoJSON Features are both accepted where noted.
Every emitted feature retains its source reference and source-object lineage in
the catalog and manifest.  ``geometric-station`` is explicitly neither an
official corner nor a racing apex.  The word ``apex`` is accepted only for a source-backed station
whose status says that it is a true apex.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, NoReturn, Sequence

from shapely import affinity
from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, nearest_points, unary_union

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PlateArtwork,
    Rect,
    StrokeRecord,
    add_text,
    circle_stroke,
    context_for,
    polyline_length_mm,
    text_strokes_fit,
)
from .stroke_font import (
    TEXT_NORMALISATION_POLICY_ID,
    normalise_text,
    text_width_mm,
)
from .svgkit import format_number


PointTuple = tuple[float, float]

CATALOG_PATH = Path(__file__).with_name("data") / "f1-circuits-2026.json"
CATALOG_SCHEMA_VERSION = 1
RENDERING_PRESET = "circuit-atlas-v2"
DOMAIN = "f1-circuits"
ARTIFACT_KIND = "circuit-atlas-v2"
FORMAT_SUBJECT_POLICY = "map"
FORMAT_IDS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)
CONTEXT_LABEL_COPY_POLICY_ID = "source-name-drawable-fallback-v1"
CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID = "source-faithful-display-punctuation-v1"
CONTEXT_LABEL_SOURCE_KEYS = ("name", "name:en", "int_name", "name:latin")
CONTEXT_MODES = ("permanent", "urban", "hybrid")
FULL_GEOMETRY_STATUS = "source-qualified"
CENTRELINE_GEOMETRY_STATUS = "cartography-qualified-centreline"
RENDERABLE_GEOMETRY_STATUSES = frozenset(
    {FULL_GEOMETRY_STATUS, CENTRELINE_GEOMETRY_STATUS}
)
FAMOUS_SECTION_NAME_STATUS = "formula1-official-source-copy-with-separate-osm-anchor"
FAMOUS_SECTION_ANCHOR_STATUS = (
    "coordinate-bearing-osm-anchor-not-official-turn-or-apex-coordinate"
)
FAMOUS_SECTION_ANCHOR_MODES = frozenset(
    {"exact-selected-lap-way-v1", "exact-context-way-near-lap-v1"}
)
CURRENT_OSM_GRANDSTAND_STATUS = (
    "frozen-current-osm-footprint-not-event-configuration-verified"
)
CURRENT_OSM_GRANDSTAND_TEMPORALITY = "snapshot-current-at-catalog-freeze"
CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE = (
    "current-osm-grandstand-footprint-only-not-event-or-fia-configuration"
)
CURRENT_OSM_GRANDSTAND_VISIBLE_DISCLOSURE = (
    "CURRENT MAP / GRANDSTANDS"
)
HISTORIC_CURRENT_CONTEXT_VISIBLE_DISCLOSURE = (
    "CURRENT MAP / VENUE CONTEXT"
)
HISTORIC_CURRENT_STAND_VISIBLE_DISCLOSURE = "CURRENT MAP / VENUE + GRANDSTANDS"
LOCAL_CORRIDOR_CLEARANCE_POLICY = "derived-corridor-local-clearance-v1"
GRADE_SEPARATION_CUE_POLICY = "black-bridge-deck-bracket-after-red-v2"
TRACK_AREA_MINIMUM_LAP_COVERAGE = 0.95
FIELD_PEN_ORDER = (
    "grey-0-25",
    "green-0-25",
    "blue-0-25",
    "purple-0-4",
    "red-0-4",
    "black-0-25",
)
FURNITURE_PEN_ORDER = (
    # The 0.25 mm Black pen is shared with the field at formats whose bound
    # furniture role resolves to it.  The remaining pens are format-only.
    "black-0-25",
    "black-0-6",
    "black-0-4",
    "black-1",
)
F1_PENS = (
    *FIELD_PEN_ORDER,
    "black-0-6",
    "black-0-4",
    "black-1",
)
FIELD_PEN_SEMANTICS: dict[str, tuple[str, ...]] = {
    "grey-0-25": (
        "source-qualified-track-edge",
        "road",
        "access-road",
        "kerb",
        "runoff",
        "gravel-trap",
        "ordinary-building",
    ),
    "green-0-25": ("grass", "woodland"),
    "blue-0-25": ("water",),
    "purple-0-4": (
        "pit-lane",
        "grandstand",
        "principal-building",
        "paddock",
        "garage",
        "pit-building",
    ),
    "red-0-4": (
        "exact-sourced-lap-centreline",
        "diagrammatic-course-corridor-offsets",
    ),
    "black-0-25": (
        "station-label",
        "start-finish",
        "north-mark",
        "source-backed-direction",
        "context-label",
        "grade-separation-cue",
    ),
}

_OFFICIAL_TURN_STATUS = frozenset(
    {
        "official",
        "official-turn",
        "official-turn-station",
        "source-backed-official-turn",
    }
)

_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_PROTECTED_BRANDING = re.compile(
    r"(?:\bFORMULA\s*1\b|\bF1\b|\bFIA\b|\bGRAND\s+PRIX\s+LOGO\b)",
    re.IGNORECASE,
)
_APEX_STATUS = frozenset({"true-apex", "official-apex", "source-backed-apex"})
_GEOMETRIC_STATION_STATUS = frozenset(
    {
        "geometric",
        "geometric-station",
        "geometric-turn-station",
        "curvature-derived",
    }
)
_SITE_MODE = {
    "permanent": "permanent",
    "street": "urban",
    "temporary": "urban",
    "semi-permanent": "hybrid",
    "hybrid": "hybrid",
}
_KIND_ALIASES = {
    "access_road": "access-road",
    "service-road": "access-road",
    "service_road": "access-road",
    "principal_building": "principal-building",
    "pit_building": "pit-building",
    "gravel_trap": "gravel-trap",
    "forest": "woodland",
    "woods": "woodland",
    "park": "grass",
    "green-space": "grass",
    "lake": "water",
    "river": "water",
    "grandstands": "grandstand",
    "buildings": "building",
    "roads": "road",
    "kerbs": "kerb",
    "curb": "kerb",
    "curbs": "kerb",
}
_SUPPORTED_CONTEXT_KINDS = frozenset(
    {
        "water",
        "grass",
        "woodland",
        "grandstand",
        "principal-building",
        "building",
        "road",
        "access-road",
        "kerb",
        "runoff",
        "gravel-trap",
        "paddock",
        "garage",
        "pit-building",
    }
)

# These are content gates, not private page-layout coordinates.  Page zones,
# margins, title caps, and furniture remain owned by format-v1.
PAPER_ADAPTATION: dict[str, dict[str, Any]] = {
    "A5": {
        "field_padding_mm": 8.0,
        "track_halo_mm": 0.80,
        "label_separation_mm": 0.75,
        "label_track_clearance_mm": 1.00,
        "turn_name_limit": 6,
        "named_section_label_limit": 4,
        "context_label_limit": 4,
        "direction_arrow_count": 2,
        "water_stipple_spacing_mm": 4.2,
        "vegetation_outline_policy": "outline-only-density-budgeted-source-boundary",
        "vegetation_outline_density_reserve_mm_per_mm2": 0.025,
        "field_density_target_mm_per_mm2": 0.17,
        "runoff_hatch_spacing_mm": 4.5,
        "gravel_stipple_spacing_mm": 4.0,
        "road_rank_limit": 3,
        "feature_limits": {
            "water": 20,
            "grass": 20,
            "woodland": 20,
            "grandstand": 10,
            "principal-building": 12,
            "building": 18,
            "road": 36,
            "access-road": 24,
            "kerb": 30,
            "runoff": 18,
            "gravel-trap": 16,
            "paddock": 8,
            "garage": 12,
            "pit-building": 8,
        },
    },
    "A4": {
        "field_padding_mm": 6.0,
        "track_halo_mm": 1.10,
        "label_separation_mm": 1.00,
        "label_track_clearance_mm": 1.40,
        "turn_name_limit": 12,
        "named_section_label_limit": 7,
        "context_label_limit": 10,
        "direction_arrow_count": 3,
        "water_stipple_spacing_mm": 3.7,
        "vegetation_outline_policy": "outline-only-density-budgeted-source-boundary",
        "vegetation_outline_density_reserve_mm_per_mm2": 0.025,
        "field_density_target_mm_per_mm2": 0.17,
        "runoff_hatch_spacing_mm": 4.0,
        "gravel_stipple_spacing_mm": 3.5,
        "road_rank_limit": 4,
        "feature_limits": {
            "water": 36,
            "grass": 36,
            "woodland": 36,
            "grandstand": 24,
            "principal-building": 28,
            "building": 60,
            "road": 90,
            "access-road": 60,
            "kerb": 75,
            "runoff": 36,
            "gravel-trap": 30,
            "paddock": 16,
            "garage": 28,
            "pit-building": 20,
        },
    },
    "A3": {
        "field_padding_mm": 8.0,
        "track_halo_mm": 1.40,
        "label_separation_mm": 1.25,
        "label_track_clearance_mm": 1.80,
        "turn_name_limit": None,
        "named_section_label_limit": 10,
        "context_label_limit": 22,
        "direction_arrow_count": 3,
        "water_stipple_spacing_mm": 3.2,
        "vegetation_outline_policy": "outline-only-density-budgeted-source-boundary",
        "vegetation_outline_density_reserve_mm_per_mm2": 0.025,
        "field_density_target_mm_per_mm2": 0.17,
        "runoff_hatch_spacing_mm": 3.5,
        "gravel_stipple_spacing_mm": 3.0,
        "road_rank_limit": 6,
        "feature_limits": {
            "water": 72,
            "grass": 72,
            "woodland": 72,
            "grandstand": 48,
            "principal-building": 54,
            "building": 160,
            "road": 220,
            "access-road": 150,
            "kerb": 180,
            "runoff": 72,
            "gravel-trap": 64,
            "paddock": 32,
            "garage": 64,
            "pit-building": 40,
        },
    },
}


@dataclass(frozen=True, slots=True)
class _PaperTransform:
    scale_mm_per_m: float
    translate_x_mm: float
    translate_y_mm: float
    source_bounds_m: tuple[float, float, float, float]
    working_rect_mm: Rect

    def point(self, value: Sequence[float]) -> PointTuple:
        return (
            self.translate_x_mm + self.scale_mm_per_m * float(value[0]),
            self.translate_y_mm - self.scale_mm_per_m * float(value[1]),
        )

    def geometry(self, value: BaseGeometry) -> BaseGeometry:
        return affinity.affine_transform(
            value,
            [
                self.scale_mm_per_m,
                0.0,
                0.0,
                -self.scale_mm_per_m,
                self.translate_x_mm,
                self.translate_y_mm,
            ],
        )


@dataclass(frozen=True, slots=True)
class _DiagrammaticCorridorPlan:
    target_width_mm: float
    nib_mm: float
    pair_pitch_mm: float
    radii_mm: tuple[float, ...]

    @property
    def logical_stroke_count(self) -> int:
        return 1 + 2 * len(self.radii_mm)

    @property
    def plotted_width_mm(self) -> float:
        if not self.radii_mm:
            return self.nib_mm
        return self.nib_mm + 2.0 * self.radii_mm[-1]


@dataclass(frozen=True, slots=True)
class _VegetationOutlineCandidate:
    feature_id: str
    kind: str
    named: bool
    paper_area_mm2: float
    records: tuple[StrokeRecord, ...]
    length_mm: float
    interior_symbol_count: int


@dataclass(frozen=True, slots=True)
class _LabelPlacement:
    id: str
    copy: str
    bounds: Rect
    anchor: PointTuple
    leader: tuple[PointTuple, ...] | None
    source_ref: str
    role: str
    displayed_name: bool
    cap_mm: float
    feature_id: str | None = None
    source_object_id: str | None = None
    source_name_key: str | None = None
    source_copy: str | None = None
    copy_policy_id: str | None = None
    normalisation_policy_id: str | None = None
    display_punctuation_policy_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ContextLabelCopy:
    copy: str
    source_name_key: str
    source_copy: str
    copy_policy_id: str = CONTEXT_LABEL_COPY_POLICY_ID
    normalisation_policy_id: str = TEXT_NORMALISATION_POLICY_ID
    display_punctuation_policy_id: str = CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid F1 circuit catalog: {message}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        article = "a non-empty" if nonempty else "an"
        _fail(f"{label} must be {article} array.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} must be a finite number.")
    return result


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label} must be an integer.")
    if minimum is not None and value < minimum:
        _fail(f"{label} must be at least {minimum}.")
    return value


def _stable_id(value: Any, label: str) -> str:
    result = _text(value, label)
    if _STABLE_ID.fullmatch(result) is None:
        _fail(f"{label} must be a lower-case stable identifier.")
    return result


def _point(value: Any, label: str) -> PointTuple:
    pair = _array(value, label)
    if len(pair) != 2:
        _fail(f"{label} must be [x, y].")
    return (_number(pair[0], f"{label}[0]"), _number(pair[1], f"{label}[1]"))


def _source_objects(value: Any, label: str) -> list[Any]:
    objects = _array(value, label, nonempty=True)
    for index, item in enumerate(objects):
        if isinstance(item, str):
            _text(item, f"{label}[{index}]")
        elif isinstance(item, dict):
            if not item:
                _fail(f"{label}[{index}] cannot be an empty object.")
        else:
            _fail(f"{label}[{index}] must be a stable string or source object.")
    return objects


def _source_object_identity(value: Any) -> str:
    """Return one deterministic SVG-safe lineage token for source objects."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for keys in (
            ("type", "id"),
            ("kind", "id"),
            ("source", "id"),
        ):
            if all(key in value for key in keys):
                return ":".join(str(value[key]) for key in keys)
        if "id" in value:
            return str(value["id"])
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    return str(value)


def _source_object_attribute(values: Sequence[Any], fallback: str) -> str:
    identities = sorted({_source_object_identity(value) for value in values})
    return "|".join(identities) if identities else fallback


def _geojson_geometry(value: Any, label: str) -> BaseGeometry:
    record = _object(value, label)
    payload: Any = record.get("geometry") if record.get("type") == "Feature" else record
    if payload is None:
        _fail(f"{label} has no GeoJSON geometry.")
    try:
        geometry = shape(payload)
    except (TypeError, ValueError) as exc:
        _fail(f"{label} is not valid GeoJSON geometry: {exc}.")
    if geometry.is_empty:
        _fail(f"{label} cannot be empty.")
    if not all(math.isfinite(float(item)) for item in geometry.bounds):
        _fail(f"{label} has non-finite bounds.")
    if not geometry.is_valid:
        _fail(f"{label} must be topologically valid before rendering.")
    return geometry


def _geojson_line(value: Any, label: str) -> LineString:
    geometry = _geojson_geometry(value, label)
    if not isinstance(geometry, LineString):
        _fail(f"{label} must be one ordered LineString.")
    if len(geometry.coords) < 2 or geometry.length <= 0.0:
        _fail(f"{label} must contain a non-degenerate line.")
    return geometry


def _is_contiguous_cyclic_lap_subsequence(
    section_points: Sequence[PointTuple], lap: LineString
) -> bool:
    """Return whether section points are one exact contiguous lap-way run."""

    lap_points = [(float(x), float(y)) for x, y, *_rest in lap.coords]
    if len(lap_points) > 1 and lap_points[0] == lap_points[-1]:
        lap_points = lap_points[:-1]
    if len(section_points) < 2 or len(section_points) > len(lap_points):
        return False

    def matches(candidate: Sequence[PointTuple]) -> bool:
        for start in range(len(lap_points)):
            if all(
                math.hypot(
                    lap_points[(start + offset) % len(lap_points)][0] - point[0],
                    lap_points[(start + offset) % len(lap_points)][1] - point[1],
                )
                <= 1e-6
                for offset, point in enumerate(candidate)
            ):
                return True
        return False

    return matches(section_points) or matches(tuple(reversed(section_points)))


def _validate_source_backed_overpass(
    section: Mapping[str, Any],
    model: Mapping[str, Any],
    lap: LineString,
    *,
    label: str,
) -> LineString:
    """Fail closed unless an overpass is an exact tagged selected-lap way."""

    source_objects = _source_objects(
        section.get("source_objects"), f"{label}.source_objects"
    )
    if len(source_objects) != 1 or not isinstance(source_objects[0], dict):
        _fail(f"{label} must bind exactly one embedded OSM way object.")
    source_object = source_objects[0]
    if source_object.get("type") != "way":
        _fail(f"{label} source object must be an OSM way.")
    source_id = source_object.get("id")
    if (
        not isinstance(source_id, int)
        or isinstance(source_id, bool)
        or source_id <= 0
        or str(section.get("source_object_id") or "") != str(source_id)
    ):
        _fail(f"{label} source_object_id must bind its positive OSM way id.")

    source_tags = _object(source_object.get("tags"), f"{label}.source_objects[0].tags")
    section_tags = _object(section.get("tags"), f"{label}.tags")
    if section_tags != source_tags:
        _fail(f"{label} section tags must exactly match its embedded source object.")
    if str(source_tags.get("bridge") or "").casefold() not in {
        "yes",
        "true",
        "1",
    }:
        _fail(f"{label} embedded source object lacks affirmative bridge evidence.")
    layer_value = source_tags.get("layer")
    try:
        layer = float(str(layer_value))
    except (TypeError, ValueError):
        layer = 0.0
    if not math.isfinite(layer) or abs(layer) <= 1e-12:
        _fail(f"{label} embedded source object lacks a non-zero layer tag.")

    lap_source_objects = _source_objects(
        model.get("lap_source_objects"), f"{label}.model.lap_source_objects"
    )
    matching_lap_objects = [
        value
        for value in lap_source_objects
        if isinstance(value, dict)
        and value.get("type") == "way"
        and value.get("id") == source_id
    ]
    if len(matching_lap_objects) != 1 or matching_lap_objects[0] != source_object:
        _fail(f"{label} source object is not exact selected-lap lineage.")
    assembly = _object(model.get("assembly"), f"{label}.model.assembly")
    used_way_ids = _array(
        assembly.get("used_way_ids"), f"{label}.model.assembly.used_way_ids"
    )
    if source_id not in used_way_ids:
        _fail(f"{label} source way is absent from the assembled lap.")

    lap_record = _object(model.get("lap"), f"{label}.model.lap")
    lap_properties = (
        _object(lap_record.get("properties"), f"{label}.model.lap.properties")
        if lap_record.get("type") == "Feature"
        else lap_record
    )
    if section.get("source_ref") != lap_properties.get("source_ref"):
        _fail(f"{label} source_ref does not match the selected lap source.")

    geometry = _geojson_geometry(section.get("geometry"), f"{label}.geometry")
    if not isinstance(geometry, LineString):
        _fail(f"{label} geometry must be one exact LineString.")
    section_points = [(float(x), float(y)) for x, y, *_rest in geometry.coords]
    if not _is_contiguous_cyclic_lap_subsequence(section_points, lap):
        _fail(f"{label} geometry is not a contiguous selected-lap source run.")
    return geometry


def _lap_self_intersections(lap: LineString) -> list[dict[str, Any]]:
    """Return deterministic non-adjacent point crossings of a closed lap."""

    coordinates = [(float(x), float(y)) for x, y, *_rest in lap.coords]
    segment_count = len(coordinates) - 1
    segments = [
        LineString((coordinates[index], coordinates[index + 1]))
        for index in range(segment_count)
    ]
    crossings: list[dict[str, Any]] = []
    for first_index, first in enumerate(segments):
        for second_index in range(first_index + 1, segment_count):
            separation = second_index - first_index
            if separation <= 1 or separation >= segment_count - 1:
                continue
            intersection = first.intersection(segments[second_index])
            if intersection.is_empty:
                continue
            if not isinstance(intersection, Point):
                raise MapPlotterError(
                    "F1 lap has an overlapping non-adjacent self-intersection; "
                    "grade separation cannot be symbolized defensibly."
                )
            if any(
                intersection.distance(value["point"]) <= 1e-6 for value in crossings
            ):
                continue
            crossings.append(
                {
                    "segment_indexes": [first_index, second_index],
                    "point": intersection,
                }
            )
    return sorted(
        crossings,
        key=lambda value: tuple(value["segment_indexes"]),
    )


def _normalise_kind(value: Any, label: str) -> str:
    kind = _text(value, label).casefold().replace("_", "-")
    return _KIND_ALIASES.get(kind, kind)


def _station_class(station: Mapping[str, Any]) -> str:
    """Return the visible evidence class for one supplied station.

    Explicit source-backed apex/turn status wins over the anchor method.  An
    authoritative station can still have been snapped by curvature, and that
    mechanical placement detail must never demote an apex to a geometric
    ``G`` marker.
    """

    status = str(station.get("status", "")).casefold()
    if status in _APEX_STATUS:
        return "source-backed-apex"
    if status in _OFFICIAL_TURN_STATUS:
        return "official-turn"
    if (
        status in _GEOMETRIC_STATION_STATUS
        or "curvature" in str(station.get("anchor_method", "")).casefold()
    ):
        return "geometric"
    return "source-tagged"


def _station_marker_prefix(station_class: str) -> str:
    return {
        "geometric": "G",
        "official-turn": "T",
        "source-backed-apex": "A",
        "source-tagged": "S",
    }[station_class]


def _valid_for_season(value: Any, season: int) -> tuple[bool, bool]:
    """Return ``(is_valid, was_explicit)`` for a feature season declaration."""

    if value is None:
        return (False, False)
    if isinstance(value, bool):
        return (False, True)
    if isinstance(value, int):
        return (value == season, True)
    if isinstance(value, str):
        tokens = {token for token in re.split(r"[^0-9]+", value) if token}
        return (str(season) in tokens, True)
    if isinstance(value, list):
        return (
            any(item == season or str(item).strip() == str(season) for item in value),
            True,
        )
    if isinstance(value, dict):
        start = value.get("from", value.get("start"))
        end = value.get("to", value.get("end"))
        try:
            low = season if start is None else int(start)
            high = season if end is None else int(end)
        except (TypeError, ValueError):
            return (False, True)
        return (low <= season <= high, True)
    return (False, True)


def _is_frozen_current_osm_grandstand(feature: Mapping[str, Any]) -> bool:
    """Return whether a stand carries the exact non-operational snapshot scope."""

    tags_value = feature.get("tags")
    tags = tags_value if isinstance(tags_value, Mapping) else {}
    source_objects_value = feature.get("source_objects")
    source_objects = (
        source_objects_value if isinstance(source_objects_value, list) else []
    )
    tagged_source_objects = [
        value
        for value in source_objects
        if isinstance(value, Mapping)
        and value.get("type") in {"way", "relation"}
        and isinstance(value.get("tags"), Mapping)
        and str(value["tags"].get("building") or "").casefold() == "grandstand"
    ]
    return (
        str(tags.get("building") or "").casefold() == "grandstand"
        and bool(tagged_source_objects)
        and any(dict(value["tags"]) == dict(tags) for value in tagged_source_objects)
        and str(feature.get("temporary_status") or "") == CURRENT_OSM_GRANDSTAND_STATUS
        and feature.get("valid_for_season") is None
        and str(feature.get("source_temporality") or "")
        == CURRENT_OSM_GRANDSTAND_TEMPORALITY
        and str(feature.get("claim_scope") or "") == CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE
        and feature.get("event_configuration_verified") is False
        and feature.get("fia_configuration_claimed") is False
        and feature.get("operational_semantics_claimed") is False
    )


def _find_source_refs(value: Any, *, _inside_source_tags: bool = False) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            inside_tags = _inside_source_tags or key == "tags"
            if not _inside_source_tags and (
                key == "source_ref" or key.endswith("_source_ref")
            ):
                if isinstance(item, str) and item.strip():
                    refs.add(item.strip())
            elif not _inside_source_tags and (
                key == "source_refs" or key.endswith("_source_refs")
            ):
                if isinstance(item, list):
                    refs.update(
                        ref.strip()
                        for ref in item
                        if isinstance(ref, str) and ref.strip()
                    )
            else:
                refs.update(_find_source_refs(item, _inside_source_tags=inside_tags))
    elif isinstance(value, list):
        for item in value:
            refs.update(
                _find_source_refs(item, _inside_source_tags=_inside_source_tags)
            )
    return refs


def _geometry_hash_payload(value: Any) -> Any:
    """Remove self-referential hash fields from normalized geometry JSON."""

    if isinstance(value, dict):
        return {
            key: _geometry_hash_payload(item)
            for key, item in value.items()
            if key not in {"geometry_sha256", "source_geometry_sha256"}
        }
    if isinstance(value, list):
        return [_geometry_hash_payload(item) for item in value]
    return value


def _geometry_sha256(model: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _geometry_hash_payload(model),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _lap_source_sha256(lap_record: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _geometry_hash_payload(lap_record),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_operational_overlay_evidence(
    model: Mapping[str, Any], *, season: int, label: str
) -> None:
    """Require current event-document evidence for operational overlays.

    Vocabulary is event-specific, so terms are not globally blacklisted.
    Empty, explicitly withheld overlays are valid; every populated record must
    identify its current-season source document and evidence scope.
    """

    raw = model.get("operational_overlays")
    if raw is None:
        return
    overlays = _object(raw, f"{label}.operational_overlays")
    status = _text(
        overlays.get("status", "withheld"),
        f"{label}.operational_overlays.status",
    ).casefold()
    records: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in overlays.items():
        if key in {"status", "ruleset", "notes"} or isinstance(value, bool):
            continue
        if not isinstance(value, list):
            continue
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                _fail(f"{label}.operational_overlays.{key}[{index}] must be an object.")
            records.append((f"{key}[{index}]", item))
    if status.startswith("withheld"):
        if records:
            _fail(f"{label}.operational_overlays is withheld but contains claims.")
        return
    for record_label, record in records:
        prefix = f"{label}.operational_overlays.{record_label}"
        _text(record.get("source_ref"), f"{prefix}.source_ref")
        if record.get("valid_for_season") != season:
            _fail(f"{prefix}.valid_for_season must equal {season}.")
        _text(record.get("document_version"), f"{prefix}.document_version")
        scope = _text(record.get("evidence_scope"), f"{prefix}.evidence_scope")
        if scope != "current-event-document":
            _fail(f"{prefix}.evidence_scope must be current-event-document.")


def _validate_normalized_model(
    model_value: Any,
    label: str,
    *,
    season: int = 2026,
    qualification_status: str = FULL_GEOMETRY_STATUS,
) -> None:
    model = _object(model_value, label)
    centreline_only = qualification_status == CENTRELINE_GEOMETRY_STATUS
    if centreline_only:
        qualification = _object(model.get("qualification"), f"{label}.qualification")
        tier = _text(qualification.get("tier"), f"{label}.qualification.tier")
        if tier != CENTRELINE_GEOMETRY_STATUS:
            _fail(
                f"{label}.qualification.tier must equal {CENTRELINE_GEOMETRY_STATUS!r}."
            )
        _text(
            qualification.get("claim_scope"),
            f"{label}.qualification.claim_scope",
        )
        omitted_capabilities = _array(
            qualification.get("omitted_capabilities"),
            f"{label}.qualification.omitted_capabilities",
            nonempty=True,
        )
        for index, capability in enumerate(omitted_capabilities):
            _text(
                capability,
                f"{label}.qualification.omitted_capabilities[{index}]",
            )
        if qualification.get("omissions_must_be_visibly_disclosed") is not True:
            _fail(
                f"{label}.qualification.omissions_must_be_visibly_disclosed "
                "must be true."
            )
    if model.get("coordinate_system") != "local-metre":
        _fail(f"{label}.coordinate_system must be 'local-metre'.")
    origin = _point(model.get("origin_wgs84"), f"{label}.origin_wgs84")
    if not (-180.0 <= origin[0] <= 180.0 and -90.0 <= origin[1] <= 90.0):
        _fail(f"{label}.origin_wgs84 must be [longitude, latitude].")

    lap = _geojson_line(model.get("lap"), f"{label}.lap")
    first = Point(lap.coords[0])
    last = Point(lap.coords[-1])
    if first.distance(last) > 1e-6:
        _fail(f"{label}.lap must be explicitly closed; no connector is inferred.")
    if lap.length < 100.0:
        _fail(f"{label}.lap is implausibly short for a circuit centreline.")
    _source_objects(model.get("lap_source_objects"), f"{label}.lap_source_objects")

    for index, lane_value in enumerate(
        _array(model.get("pit_lanes", []), f"{label}.pit_lanes")
    ):
        lane = _object(lane_value, f"{label}.pit_lanes[{index}]")
        _geojson_line(lane.get("geometry"), f"{label}.pit_lanes[{index}].geometry")
        _source_objects(
            lane.get("source_objects"),
            f"{label}.pit_lanes[{index}].source_objects",
        )

    for index, boundary in enumerate(
        _array(model.get("track_boundaries", []), f"{label}.track_boundaries")
    ):
        geometry = _geojson_geometry(boundary, f"{label}.track_boundaries[{index}]")
        if not isinstance(
            geometry, (LineString, MultiLineString, Polygon, MultiPolygon)
        ):
            _fail(f"{label}.track_boundaries[{index}] must be line or area geometry.")

    context = _array(model.get("context", []), f"{label}.context")
    context_ids: list[str] = []
    for index, feature_value in enumerate(context):
        feature_label = f"{label}.context[{index}]"
        feature = _object(feature_value, feature_label)
        context_ids.append(_stable_id(feature.get("id"), f"{feature_label}.id"))
        feature_kind = _normalise_kind(feature.get("kind"), f"{feature_label}.kind")
        _optional_text(feature.get("name"), f"{feature_label}.name")
        feature_geometry = _geojson_geometry(
            feature.get("geometry"), f"{feature_label}.geometry"
        )
        if isinstance(feature_geometry, (Point, MultiPoint)):
            _fail(f"{feature_label}.geometry must be line or area geometry.")
        _text(feature.get("source_ref"), f"{feature_label}.source_ref")
        _source_objects(
            feature.get("source_objects"), f"{feature_label}.source_objects"
        )
        _object(feature.get("tags", {}), f"{feature_label}.tags")
        _text(
            feature.get("temporary_status", "permanent"),
            f"{feature_label}.temporary_status",
        )
        if "valid_for_season" not in feature:
            _fail(f"{feature_label}.valid_for_season must be explicit.")
        if feature_kind == "grandstand" and not _is_frozen_current_osm_grandstand(
            feature
        ):
            _fail(
                f"{feature_label} grandstand must carry the exact frozen "
                "current-OSM footprint-only contract; event/FIA configuration "
                "claims require a separate authoritative schema."
            )
    if len(context_ids) != len(set(context_ids)):
        _fail(f"{label}.context repeats a feature id.")

    stations = _array(
        model.get("turn_stations", []),
        f"{label}.turn_stations",
        nonempty=not centreline_only,
    )
    station_ids: list[str] = []
    numbers: list[int] = []
    chainages: list[float] = []
    for index, station_value in enumerate(stations):
        station_label = f"{label}.turn_stations[{index}]"
        station = _object(station_value, station_label)
        station_ids.append(_stable_id(station.get("id"), f"{station_label}.id"))
        number = _integer(station.get("number"), f"{station_label}.number", minimum=1)
        numbers.append(number)
        name = _optional_text(station.get("name"), f"{station_label}.name")
        chainage = _number(station.get("chainage_m"), f"{station_label}.chainage_m")
        chainages.append(chainage)
        if not (0.0 <= chainage <= lap.length + 1e-6):
            _fail(f"{station_label}.chainage_m falls outside the supplied lap.")
        station_point = Point(_point(station.get("point"), f"{station_label}.point"))
        if station_point.distance(lap) > 30.0:
            _fail(f"{station_label}.point is more than 30 m from the lap.")
        source_ref = _text(station.get("source_ref"), f"{station_label}.source_ref")
        _text(station.get("anchor_method"), f"{station_label}.anchor_method")
        status = _text(station.get("status"), f"{station_label}.status").casefold()
        if (
            "apex" in status or (name and "apex" in name.casefold())
        ) and status not in (_APEX_STATUS):
            _fail(
                f"{station_label} uses apex terminology without a source-backed "
                "true-apex status."
            )
        if status in _APEX_STATUS and not source_ref:
            _fail(f"{station_label} true apex needs an explicit source reference.")
    if len(station_ids) != len(set(station_ids)):
        _fail(f"{label}.turn_stations repeats an id.")
    if len(numbers) != len(set(numbers)) or sorted(numbers) != list(
        range(1, len(numbers) + 1)
    ):
        _fail(f"{label}.turn_stations must contain each turn number 1..N exactly once.")
    start_finish_value = model.get("start_finish")
    if start_finish_value is None:
        if not centreline_only:
            _fail(f"{label}.start_finish must be an object.")
    else:
        start_finish = _object(start_finish_value, f"{label}.start_finish")
        sf_point = Point(
            _point(start_finish.get("point"), f"{label}.start_finish.point")
        )
        if sf_point.distance(lap) > 15.0:
            _fail(f"{label}.start_finish.point is more than 15 m from the lap.")
        _text(start_finish.get("source_ref"), f"{label}.start_finish.source_ref")
        _text(start_finish.get("status"), f"{label}.start_finish.status")
        start_chainage = float(lap.project(sf_point))
        start_relative_order = [
            number
            for chainage, number in sorted(
                zip(chainages, numbers, strict=True),
                key=lambda item: ((item[0] - start_chainage) % lap.length, item[1]),
            )
        ]
        if start_relative_order != numbers:
            _fail(
                f"{label}.turn_stations must be numbered and ordered from the "
                "sourced start/finish in lap direction."
            )

    special_section_ids: list[str] = []
    for index, section_value in enumerate(
        _array(model.get("special_sections", []), f"{label}.special_sections")
    ):
        section = _object(section_value, f"{label}.special_sections[{index}]")
        special_section_ids.append(
            _stable_id(section.get("id"), f"{label}.special_sections[{index}].id")
        )
        kind = _text(
            section.get("kind"), f"{label}.special_sections[{index}].kind"
        ).casefold()
        _text(
            section.get("source_ref"), f"{label}.special_sections[{index}].source_ref"
        )
        if kind == "named-course-section":
            _text(section.get("name"), f"{label}.special_sections[{index}].name")
            name_status = _text(
                section.get("name_status"),
                f"{label}.special_sections[{index}].name_status",
            )
            if name_status not in {
                "osm-source-tagged-unverified-not-official",
                FAMOUS_SECTION_NAME_STATUS,
            }:
                _fail(f"{label}.special_sections[{index}].name_status is unsupported.")
            claim_scope = _text(
                section.get("claim_scope"),
                f"{label}.special_sections[{index}].claim_scope",
            )
            source_objects = _source_objects(
                section.get("source_objects"),
                f"{label}.special_sections[{index}].source_objects",
            )
            if name_status == "osm-source-tagged-unverified-not-official":
                if section.get("official_course_name", False) is not False:
                    _fail(
                        f"{label}.special_sections[{index}] promotes an OSM name "
                        "to an official course name."
                    )
            else:
                if section.get("official_course_name") is not True:
                    _fail(
                        f"{label}.special_sections[{index}] official source copy "
                        "must set official_course_name true."
                    )
                source_copy = _text(
                    section.get("source_copy"),
                    f"{label}.special_sections[{index}].source_copy",
                )
                if source_copy != section.get("name"):
                    _fail(
                        f"{label}.special_sections[{index}] name must equal its "
                        "exact official source copy."
                    )
                name_source_ref = _text(
                    section.get("name_source_ref"),
                    f"{label}.special_sections[{index}].name_source_ref",
                )
                if name_source_ref != section.get("source_ref"):
                    _fail(
                        f"{label}.special_sections[{index}] primary source must be "
                        "the official name source."
                    )
                if section.get("name_source_key") != "official-source-copy":
                    _fail(
                        f"{label}.special_sections[{index}].name_source_key must "
                        "identify official source copy."
                    )
                anchor_source_ref = _text(
                    section.get("anchor_source_ref"),
                    f"{label}.special_sections[{index}].anchor_source_ref",
                )
                if anchor_source_ref == name_source_ref:
                    _fail(
                        f"{label}.special_sections[{index}] name and anchor "
                        "provenance must remain separate."
                    )
                anchor_mode = _text(
                    section.get("anchor_mode"),
                    f"{label}.special_sections[{index}].anchor_mode",
                )
                if anchor_mode not in FAMOUS_SECTION_ANCHOR_MODES:
                    _fail(
                        f"{label}.special_sections[{index}].anchor_mode is unsupported."
                    )
                if section.get("anchor_status") != FAMOUS_SECTION_ANCHOR_STATUS:
                    _fail(
                        f"{label}.special_sections[{index}].anchor_status weakens "
                        "the no-turn/apex-coordinate claim."
                    )
                priority = _integer(
                    section.get("priority"),
                    f"{label}.special_sections[{index}].priority",
                    minimum=1,
                )
                if priority > 1000:
                    _fail(
                        f"{label}.special_sections[{index}].priority must be at most 1000."
                    )
                anchor_ids = _array(
                    section.get("anchor_source_object_ids"),
                    f"{label}.special_sections[{index}].anchor_source_object_ids",
                    nonempty=True,
                )
                normalized_anchor_ids = [
                    _integer(
                        value,
                        f"{label}.special_sections[{index}]."
                        f"anchor_source_object_ids[{anchor_index}]",
                        minimum=1,
                    )
                    for anchor_index, value in enumerate(anchor_ids)
                ]
                object_ids = [
                    value.get("id")
                    for value in source_objects
                    if isinstance(value, dict)
                ]
                if object_ids != normalized_anchor_ids:
                    _fail(
                        f"{label}.special_sections[{index}] anchor object lineage "
                        "does not match its exact source objects."
                    )
                source_offset = _number(
                    section.get("source_offset_m"),
                    f"{label}.special_sections[{index}].source_offset_m",
                )
                if source_offset < 0.0:
                    _fail(
                        f"{label}.special_sections[{index}].source_offset_m cannot "
                        "be negative."
                    )
                if anchor_mode == "exact-selected-lap-way-v1":
                    if source_offset > 0.001 or "maximum_lap_offset_m" in section:
                        _fail(
                            f"{label}.special_sections[{index}] selected-lap anchor "
                            "must retain zero offset and no context ceiling."
                        )
                else:
                    maximum_offset = _number(
                        section.get("maximum_lap_offset_m"),
                        f"{label}.special_sections[{index}].maximum_lap_offset_m",
                    )
                    if not 0.0 <= maximum_offset <= 30.0:
                        _fail(
                            f"{label}.special_sections[{index}] context anchor "
                            "ceiling must be in 0..30 m."
                        )
                    if source_offset > maximum_offset + 1e-9:
                        _fail(
                            f"{label}.special_sections[{index}] context anchor "
                            "exceeds its declared lap-offset ceiling."
                        )
                if (
                    "not-an-official-turn-or-apex-coordinate" not in claim_scope
                    or "no-snapping" not in claim_scope
                ):
                    _fail(
                        f"{label}.special_sections[{index}] claim_scope must retain "
                        "the associative/no-snapping disclosure."
                    )
        if re.search(
            r"(?:drs|straight[-_ ]?mode|overtake|detection|activation|"
            r"speed[-_ ]?trap|timing)",
            kind,
        ):
            if section.get("valid_for_season") != season:
                _fail(
                    f"{label}.special_sections[{index}] operational claim must "
                    f"be valid for season {season}."
                )
            _text(
                section.get("document_version"),
                f"{label}.special_sections[{index}].document_version",
            )
            if section.get("evidence_scope") != "current-event-document":
                _fail(
                    f"{label}.special_sections[{index}].evidence_scope must be "
                    "current-event-document."
                )
        if "geometry" in section:
            section_geometry = _geojson_geometry(
                section["geometry"], f"{label}.special_sections[{index}].geometry"
            )
            if kind == "overpass":
                _validate_source_backed_overpass(
                    section,
                    model,
                    lap,
                    label=f"{label}.special_sections[{index}]",
                )
            if kind == "named-course-section" and not isinstance(
                section_geometry, (LineString, MultiLineString)
            ):
                _fail(
                    f"{label}.special_sections[{index}] named course section "
                    "must be line geometry."
                )
        elif "point" in section:
            _point(section["point"], f"{label}.special_sections[{index}].point")
        else:
            _fail(f"{label}.special_sections[{index}] needs geometry or point.")
    if len(special_section_ids) != len(set(special_section_ids)):
        _fail(f"{label}.special_sections repeats a section id.")

    digest = _text(model.get("geometry_sha256"), f"{label}.geometry_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        _fail(f"{label}.geometry_sha256 must be a lower-case SHA-256.")
    calculated = _geometry_sha256(model)
    if digest != calculated:
        _fail(
            f"{label}.geometry_sha256 disagrees with the canonical normalized model "
            f"({digest} != {calculated})."
        )


def validate_f1_event(
    value: Any,
    *,
    source_registry: Mapping[str, Mapping[str, Any]] | None = None,
    season: int = 2026,
) -> dict[str, Any]:
    """Validate one event, preserving unavailable geometry for catalog listing.

    Catalog validation permits an event whose geometry status is ``unavailable``
    and has no normalized model.  :func:`build_f1_plate` rejects that event;
    this distinction lets the frozen calendar remain complete without drawing
    fabricated artwork.
    """

    event = _object(value, "event")
    _stable_id(event.get("id"), "event.id")
    neutral_title = _text(
        event.get("neutral_display_title", event.get("display_title")),
        "event.neutral_display_title",
    )
    if _PROTECTED_BRANDING.search(neutral_title):
        _fail("event.neutral_display_title must be neutral and logo-free.")
    if "calendar_order" in event:
        _integer(event["calendar_order"], "event.calendar_order", minimum=1)
    _text(event.get("calendar_status", "review-only"), "event.calendar_status")

    circuit = _object(event.get("circuit"), "event.circuit")
    _stable_id(circuit.get("id"), "event.circuit.id")
    _text(
        circuit.get("official_name", circuit.get("name")),
        "event.circuit.official_name",
    )
    site_type = _text(circuit.get("site_type"), "event.circuit.site_type")
    if site_type not in _SITE_MODE:
        _fail(
            "event.circuit.site_type must be permanent, street, temporary, "
            "semi-permanent, or hybrid."
        )
    if "atlas_context_mode" in circuit:
        atlas_context_mode = _text(
            circuit.get("atlas_context_mode"),
            "event.circuit.atlas_context_mode",
        )
        if atlas_context_mode not in CONTEXT_MODES:
            _fail(
                "event.circuit.atlas_context_mode must be permanent, urban, or hybrid."
            )
    if "configuration_season" in circuit and circuit["configuration_season"] != season:
        _fail(f"event.circuit.configuration_season must equal {season}.")
    direction = str(
        circuit.get("lap_direction") or circuit.get("direction") or "withheld"
    ).casefold()
    if direction not in {"clockwise", "counter-clockwise", "unknown", "withheld"}:
        _fail(
            "event.circuit.lap_direction must be clockwise, counter-clockwise, "
            "unknown, or withheld."
        )
    if direction in {"clockwise", "counter-clockwise"}:
        _text(
            circuit.get("lap_direction_source_ref"),
            "event.circuit.lap_direction_source_ref",
        )

    geometry = _object(circuit.get("geometry"), "event.circuit.geometry")
    geometry_status = _text(
        geometry.get("status", "unavailable"), "event.circuit.geometry.status"
    )
    model = geometry.get("model")
    if model is not None:
        if geometry_status not in RENDERABLE_GEOMETRY_STATUSES:
            _fail(
                "event.circuit.geometry.status must be source-qualified or "
                "cartography-qualified-centreline when a model is present."
            )
        _validate_normalized_model(
            model,
            "event.circuit.geometry.model",
            season=season,
            qualification_status=geometry_status,
        )
        _validate_operational_overlay_evidence(
            model,
            season=season,
            label="event.circuit.geometry.model",
        )
    elif geometry_status not in {"unavailable", "withheld", "pending", "provisional"}:
        _fail(
            "event.circuit.geometry.model is required unless geometry status is "
            "unavailable, withheld, pending, or provisional."
        )

    if "rights" in event:
        _object(event["rights"], "event.rights")
    if "official_facts" in event:
        facts = _object(event["official_facts"], "event.official_facts")
        if isinstance(facts.get("official_circuit_length_m"), (int, float)):
            if float(facts["official_circuit_length_m"]) <= 0.0:
                _fail(
                    "event.official_facts.official_circuit_length_m must be positive."
                )
        first_gp = facts.get("first_grand_prix")
        if first_gp is not None:
            _integer(
                first_gp,
                "event.official_facts.first_grand_prix",
                minimum=1950,
            )
            if int(first_gp) > season:
                _fail(
                    "event.official_facts.first_grand_prix exceeds the catalog season."
                )
        fastest = _object(facts.get("fastest_lap"), "event.official_facts.fastest_lap")
        fastest_status = _text(
            fastest.get("status"), "event.official_facts.fastest_lap.status"
        )
        _text(
            fastest.get("source_ref"),
            "event.official_facts.fastest_lap.source_ref",
        )
        if fastest_status == "source-backed":
            time_copy = _text(
                fastest.get("time"), "event.official_facts.fastest_lap.time"
            )
            match = re.fullmatch(r"([0-9]+):([0-5][0-9])\.([0-9]{3})", time_copy)
            if match is None:
                _fail("event.official_facts.fastest_lap.time is malformed.")
            expected_ms = (int(match.group(1)) * 60 + int(match.group(2))) * 1000 + int(
                match.group(3)
            )
            if fastest.get("time_ms") != expected_ms:
                _fail("event.official_facts.fastest_lap.time_ms disagrees with time.")
            _text(
                fastest.get("driver"),
                "event.official_facts.fastest_lap.driver",
            )
            fastest_season = _integer(
                fastest.get("season"),
                "event.official_facts.fastest_lap.season",
                minimum=1950,
            )
            if fastest_season > season:
                _fail("event.official_facts.fastest_lap.season exceeds catalog season.")
            if "withheld_reason" in fastest:
                _fail("source-backed fastest lap cannot carry withheld_reason.")
        elif fastest_status == "withheld":
            _text(
                fastest.get("withheld_reason"),
                "event.official_facts.fastest_lap.withheld_reason",
            )
            if any(key in fastest for key in ("time", "time_ms", "driver", "season")):
                _fail("withheld fastest lap cannot carry performance values.")
        else:
            _fail("event.official_facts.fastest_lap.status is unsupported.")
    refs = _find_source_refs(event)
    if source_registry is not None:
        missing = sorted(refs - set(source_registry))
        if missing:
            _fail(
                f"event {event['id']!r} has unresolved source refs: "
                + ", ".join(missing)
                + "."
            )
    return copy.deepcopy(event)


def validate_f1_catalog(value: Any) -> dict[str, Any]:
    """Validate and return an isolated schema-version-1 circuit catalog."""

    catalog = _object(value, "catalog")
    required = {
        "schema_version",
        "catalog_id",
        "season",
        "freeze",
        "sources",
        "events",
        "excluded_calendar_events",
    }
    missing = sorted(required - set(catalog))
    if missing:
        _fail("catalog is missing fields: " + ", ".join(missing) + ".")
    if catalog["schema_version"] != CATALOG_SCHEMA_VERSION:
        _fail(f"catalog.schema_version must be {CATALOG_SCHEMA_VERSION}.")
    _stable_id(catalog["catalog_id"], "catalog.catalog_id")
    season = _integer(catalog["season"], "catalog.season")
    catalog_class = _text(
        catalog.get("catalog_class", "current-season-calendar"),
        "catalog.catalog_class",
    )
    if catalog_class not in {
        "current-season-calendar",
        "legacy-f1-configurations",
        "motorsport-circuit-studies",
    }:
        _fail(
            "catalog.catalog_class must be current-season-calendar, "
            "legacy-f1-configurations, or motorsport-circuit-studies."
        )
    legacy_catalog = catalog_class == "legacy-f1-configurations"
    motorsport_studies = catalog_class == "motorsport-circuit-studies"
    if season != 2026:
        _fail("catalog.season is the frozen atlas release season and must be 2026.")
    if legacy_catalog:
        if catalog.get("season_scope") != "multi-era":
            _fail("legacy catalog.season_scope must be 'multi-era'.")
    freeze = _object(catalog["freeze"], "catalog.freeze")
    _text(
        freeze.get("frozen_at", freeze.get("freeze_date")),
        "catalog.freeze.frozen_at",
    )

    source_records = _array(catalog["sources"], "catalog.sources", nonempty=True)
    source_registry: dict[str, Mapping[str, Any]] = {}
    for index, source_value in enumerate(source_records):
        label = f"catalog.sources[{index}]"
        source = _object(source_value, label)
        source_id = _stable_id(source.get("id"), f"{label}.id")
        _text(source.get("publisher"), f"{label}.publisher")
        _text(source.get("title", source.get("use")), f"{label}.title")
        _text(source.get("url"), f"{label}.url")
        _text(source.get("source_kind", source.get("kind")), f"{label}.source_kind")
        _text(source.get("licence", source.get("license")), f"{label}.licence")
        if source_id in source_registry:
            _fail(f"catalog repeats source id {source_id!r}.")
        source_registry[source_id] = source

    events = _array(catalog["events"], "catalog.events", nonempty=True)
    checked_events: list[dict[str, Any]] = []
    for index, event_value in enumerate(events):
        event = _object(event_value, f"catalog.events[{index}]")
        reference_season = season
        if not legacy_catalog:
            current_circuit = _object(
                event.get("circuit"), f"catalog.events[{index}].circuit"
            )
            atlas_context_mode = _text(
                current_circuit.get("atlas_context_mode"),
                f"catalog.events[{index}].circuit.atlas_context_mode",
            )
            if atlas_context_mode not in CONTEXT_MODES:
                _fail(
                    f"catalog.events[{index}].circuit.atlas_context_mode must be "
                    "permanent, urban, or hybrid."
                )
        if motorsport_studies:
            study = _object(
                event.get("study_information"),
                f"catalog.events[{index}].study_information",
            )
            for group_name in ("history", "edition"):
                group = _object(
                    study.get(group_name),
                    f"catalog.events[{index}].study_information.{group_name}",
                )
                _text(
                    group.get("label"),
                    f"catalog.events[{index}].study_information.{group_name}.label",
                )
                lines = _array(
                    group.get("lines"),
                    f"catalog.events[{index}].study_information.{group_name}.lines",
                    nonempty=True,
                )
                if len(lines) > 2:
                    _fail(
                        f"catalog.events[{index}].study_information.{group_name}."
                        "lines may contain at most two lines."
                    )
                for line_index, line in enumerate(lines):
                    _text(
                        line,
                        f"catalog.events[{index}].study_information.{group_name}."
                        f"lines[{line_index}]",
                    )
                _text(
                    group.get("source_ref"),
                    f"catalog.events[{index}].study_information.{group_name}."
                    "source_ref",
                )
        if legacy_catalog:
            reference_season = _integer(
                event.get("configuration_reference_season"),
                f"catalog.events[{index}].configuration_reference_season",
                minimum=1950,
            )
            _text(
                event.get("render_disclosure"),
                f"catalog.events[{index}].render_disclosure",
            )
            identity = _object(
                event.get("configuration_identity"),
                f"catalog.events[{index}].configuration_identity",
            )
            identity_status = _text(
                identity.get("status"),
                f"catalog.events[{index}].configuration_identity.status",
            )
            if identity_status not in {
                "exact-historic-source",
                "current-surviving-equivalent",
                "current-source-f1-reference",
                "held",
            }:
                _fail(
                    f"catalog.events[{index}].configuration_identity.status "
                    "is unsupported."
                )
            if identity_status == "held":
                held_geometry = _object(
                    _object(
                        event.get("circuit"), f"catalog.events[{index}].circuit"
                    ).get("geometry"),
                    f"catalog.events[{index}].circuit.geometry",
                )
                held_review = _object(
                    held_geometry.get("review", {}),
                    f"catalog.events[{index}].circuit.geometry.review",
                )
                if (
                    held_geometry.get("model") is not None
                    or str(held_review.get("status", "")).casefold() != "held"
                ):
                    _fail(
                        f"catalog.events[{index}] held configuration identity "
                        "requires a null geometry model and held review status."
                    )
            if not isinstance(identity.get("current_surviving_equivalent"), bool):
                _fail(
                    f"catalog.events[{index}].configuration_identity."
                    "current_surviving_equivalent must be boolean."
                )
            identity_reference = _integer(
                identity.get("f1_reference_season"),
                f"catalog.events[{index}].configuration_identity.f1_reference_season",
                minimum=1950,
            )
            if identity_reference != reference_season:
                _fail(
                    f"catalog.events[{index}] configuration reference seasons disagree."
                )
            f1_seasons = _array(
                identity.get("f1_seasons"),
                f"catalog.events[{index}].configuration_identity.f1_seasons",
                nonempty=True,
            )
            parsed_f1_seasons = [
                _integer(
                    item,
                    f"catalog.events[{index}].configuration_identity."
                    f"f1_seasons[{season_index}]",
                    minimum=1950,
                )
                for season_index, item in enumerate(f1_seasons)
            ]
            if reference_season not in parsed_f1_seasons:
                _fail(
                    f"catalog.events[{index}].configuration_identity.f1_seasons "
                    "must include the reference season."
                )
            source_refs = _array(
                identity.get("source_refs"),
                f"catalog.events[{index}].configuration_identity.source_refs",
                nonempty=identity_status != "held",
            )
            for source_index, source_ref in enumerate(source_refs):
                _text(
                    source_ref,
                    f"catalog.events[{index}].configuration_identity."
                    f"source_refs[{source_index}]",
                )
        checked_events.append(
            validate_f1_event(
                event,
                source_registry=source_registry,
                season=reference_season,
            )
        )
    event_ids = [str(event["id"]) for event in checked_events]
    if len(event_ids) != len(set(event_ids)):
        _fail("catalog.events repeats an event id.")

    excluded = _array(
        catalog["excluded_calendar_events"], "catalog.excluded_calendar_events"
    )
    excluded_ids: list[str] = []
    for index, record_value in enumerate(excluded):
        label = f"catalog.excluded_calendar_events[{index}]"
        record = _object(record_value, label)
        excluded_ids.append(_stable_id(record.get("id"), f"{label}.id"))
        _text(record.get("status", record.get("reason")), f"{label}.status")
    if len(excluded_ids) != len(set(excluded_ids)):
        _fail("catalog.excluded_calendar_events repeats an id.")

    result = copy.deepcopy(catalog)
    result["events"] = checked_events
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value!r} is forbidden")


def load_f1_catalog(path: Path | None = None) -> dict[str, Any]:
    """Load the packaged 2026 catalog or an equivalent catalog path."""

    source = path or CATALOG_PATH
    try:
        raw = json.loads(
            source.read_text(encoding="utf-8"), parse_constant=_reject_json_constant
        )
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read F1 circuit catalog {source}: {exc}"
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(
            f"F1 circuit catalog {source} is invalid JSON: {exc}"
        ) from exc
    return validate_f1_catalog(raw)


def list_f1_events(
    catalog: Mapping[str, Any] | Path | None = None,
) -> list[dict[str, Any]]:
    """Return stable, CLI-friendly summaries of the frozen event ledger."""

    if catalog is None or isinstance(catalog, Path):
        checked = load_f1_catalog(catalog)
    else:
        checked = validate_f1_catalog(catalog)
    rows: list[dict[str, Any]] = []
    for event in checked["events"]:
        circuit = event["circuit"]
        geometry = circuit["geometry"]
        rows.append(
            {
                "id": event["id"],
                "calendar_order": event.get("calendar_order"),
                "race_date": event.get("race_date"),
                "title": event["neutral_display_title"],
                "circuit_id": circuit["id"],
                "circuit_name": circuit.get("official_name", circuit.get("name")),
                "calendar_status": event.get("calendar_status"),
                "geometry_status": geometry.get("status"),
                "renderable": isinstance(geometry.get("model"), dict),
                "formats": list(FORMAT_IDS),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["calendar_order"] is None,
            row["calendar_order"] if row["calendar_order"] is not None else 10_000,
            str(row["id"]),
        ),
    )


def _line_parts(geometry: BaseGeometry) -> Iterable[LineString]:
    if geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        yield geometry
        return
    if isinstance(geometry, Polygon):
        yield LineString(geometry.exterior.coords)
        for ring in geometry.interiors:
            yield LineString(ring.coords)
        return
    if isinstance(geometry, (MultiLineString, MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from _line_parts(part)


def _polygon_parts(geometry: BaseGeometry) -> Iterable[Polygon]:
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
        return
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from _polygon_parts(part)


def _paper_transform(
    source_bounds: tuple[float, float, float, float], rect: Rect
) -> _PaperTransform:
    min_x, min_y, max_x, max_y = source_bounds
    span_x = max_x - min_x
    span_y = max_y - min_y
    if min(span_x, span_y) <= 1e-9:
        _fail("circuit source extent is degenerate.")
    scale = min(rect.width / span_x, rect.height / span_y)
    used_width = span_x * scale
    used_height = span_y * scale
    offset_x = rect.x + (rect.width - used_width) / 2.0
    offset_y = rect.y + (rect.height - used_height) / 2.0
    return _PaperTransform(
        scale_mm_per_m=scale,
        translate_x_mm=offset_x - min_x * scale,
        translate_y_mm=offset_y + max_y * scale,
        source_bounds_m=source_bounds,
        working_rect_mm=rect,
    )


def _source_viewport_for_paper_rect(
    transform_value: _PaperTransform,
    rect: Rect,
) -> tuple[float, float, float, float]:
    """Return the exact source-space rectangle that maps to ``rect``.

    Circuit framing and context selection deliberately use different extents.
    The selected lap plus pit linework is fitted as large as possible inside
    the physical overlay-clearance rectangle.  Context may then occupy the
    remaining paper out to the complete map-field edge without shrinking that
    course fit or projecting beyond the field.
    """

    scale = transform_value.scale_mm_per_m
    if not math.isfinite(scale) or scale <= 0.0:
        raise MapPlotterError("Circuit source viewport requires a positive scale.")
    min_x = (rect.left - transform_value.translate_x_mm) / scale
    max_x = (rect.right - transform_value.translate_x_mm) / scale
    max_y = (transform_value.translate_y_mm - rect.top) / scale
    min_y = (transform_value.translate_y_mm - rect.bottom) / scale
    return (min_x, min_y, max_x, max_y)


def _rect_polygon(rect: Rect) -> Polygon:
    return box(rect.left, rect.top, rect.right, rect.bottom)


def _context_outline_source_geometry(
    source: BaseGeometry,
    viewport: Polygon,
) -> BaseGeometry:
    """Clip sourced linework without inventing a viewport-edge boundary.

    Polygon fills still use the clipped area for interior symbols.  Their
    plotted outline, however, is the source boundary clipped to the viewport;
    taking the boundary *after* area clipping would draw the crop rectangle as
    if it were a mapped shoreline, park edge, or building wall.
    """

    if isinstance(source, (Polygon, MultiPolygon)):
        return source.boundary.intersection(viewport)
    return source.intersection(viewport)


def _budget_vegetation_outline_candidates(
    candidates: Sequence[_VegetationOutlineCandidate],
    *,
    available_length_mm: float,
    policy: str,
) -> tuple[
    list[_VegetationOutlineCandidate],
    list[_VegetationOutlineCandidate],
]:
    """Retain whole vegetation outline groups within a paper-space budget.

    The current outline-only policy reserves part of the shared field-density
    target before general context pruning, then greedily retains complete
    source-boundary groups named-first.  The older pattern policy keeps a
    group with no physical interior symbol mandatory so a selected source
    feature cannot become invisible.  The result preserves input order so
    later deterministic travel optimisation sees stable source ordering.
    """

    outline_only = policy == "outline-only-density-budgeted-source-boundary"
    mandatory_ids = (
        set()
        if outline_only
        else {
            candidate.feature_id
            for candidate in candidates
            if candidate.interior_symbol_count == 0
        }
    )
    if policy == "symbols-only":
        return (
            [
                candidate
                for candidate in candidates
                if candidate.feature_id in mandatory_ids
            ],
            [
                candidate
                for candidate in candidates
                if candidate.feature_id not in mandatory_ids
            ],
        )
    if policy not in {
        "density-budgeted-source-boundary",
        "outline-only-density-budgeted-source-boundary",
    }:
        raise MapPlotterError(f"Unknown vegetation outline policy {policy!r}.")

    mandatory_length = sum(
        candidate.length_mm
        for candidate in candidates
        if candidate.feature_id in mandatory_ids
    )
    remaining_length = max(0.0, available_length_mm - mandatory_length)
    retained_ids = set(mandatory_ids)
    retention_order = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.feature_id not in mandatory_ids
        ),
        key=lambda candidate: (
            0 if candidate.named else 1,
            0 if candidate.kind == "woodland" else 1,
            candidate.feature_id,
        ),
    )
    for candidate in retention_order:
        if candidate.length_mm > remaining_length + 1e-9:
            continue
        retained_ids.add(candidate.feature_id)
        remaining_length -= candidate.length_mm

    retained = [
        candidate for candidate in candidates if candidate.feature_id in retained_ids
    ]
    omitted = [
        candidate
        for candidate in candidates
        if candidate.feature_id not in retained_ids
    ]
    return retained, omitted


def _label_box_attribute(rect: Rect) -> str:
    return ",".join(
        f"{value:.3f}" for value in (rect.x, rect.y, rect.width, rect.height)
    )


def _road_rank(feature: Mapping[str, Any]) -> int:
    tags = feature.get("tags")
    highway = str(tags.get("highway", "")) if isinstance(tags, dict) else ""
    ranks = {
        "motorway": 0,
        "trunk": 0,
        "primary": 1,
        "secondary": 2,
        "tertiary": 3,
        "unclassified": 4,
        "residential": 4,
        "service": 5,
        "track": 5,
    }
    return ranks.get(highway, 3)


def _suppress_coincident_host_road(
    geometry: BaseGeometry,
    lap: LineString,
    *,
    halo_m: float,
) -> tuple[BaseGeometry, float]:
    """Remove only road segments that actually run along the sourced lap.

    A blanket buffer subtraction erases legitimate crossing streets, bridges
    and waterside boundaries.  Here a short segment is suppressed only when
    its midpoint lies inside the halo *and* its tangent is aligned with the
    nearest lap tangent.  Long source segments are deterministically sampled
    before classification.
    """

    kept: list[LineString] = []
    removed = 0.0
    maximum_step = max(halo_m, 5.0)
    for line in _line_parts(geometry):
        if line.length <= 0.0:
            continue
        segment_count = max(1, int(math.ceil(line.length / maximum_step)))
        points = [
            line.interpolate(line.length * index / segment_count)
            for index in range(segment_count + 1)
        ]
        for first, second in zip(points, points[1:]):
            segment = LineString([first, second])
            if segment.length <= 1e-9:
                continue
            midpoint = segment.interpolate(0.5, normalized=True)
            lap_distance = float(lap.project(midpoint))
            nearest = lap.interpolate(lap_distance)
            segment_dx = second.x - first.x
            segment_dy = second.y - first.y
            segment_length = math.hypot(segment_dx, segment_dy)
            lap_tangent = _line_tangent(lap, lap_distance)
            alignment = abs(
                segment_dx / segment_length * lap_tangent[0]
                + segment_dy / segment_length * lap_tangent[1]
            )
            coincident = midpoint.distance(nearest) <= halo_m and alignment >= 0.85
            if coincident:
                removed += segment.length
            else:
                kept.append(segment)
    if not kept:
        return GeometryCollection(), removed
    # Sampling exists only to classify local alignment.  Rejoin adjacent kept
    # samples before they reach the plotter: otherwise one continuous crossing
    # road is serialized as a row of artificial, near-three-nib fragments.
    # `unary_union` nodes genuine intersections; `linemerge` then joins only
    # degree-two continuations and cannot bridge a segment that was suppressed.
    joined = unary_union(kept)
    if isinstance(joined, MultiLineString):
        joined = linemerge(joined)
    return joined, removed


def _context_mode(event: Mapping[str, Any], requested: str | None) -> str:
    frozen_value = event["circuit"].get("atlas_context_mode")
    if frozen_value is not None:
        frozen = str(frozen_value)
        if frozen not in CONTEXT_MODES:
            raise MapPlotterError(
                f"Unknown frozen circuit context mode {frozen!r}; choose "
                + ", ".join(CONTEXT_MODES)
                + "."
            )
        if requested not in {None, "auto", frozen}:
            raise MapPlotterError(
                f"Circuit {event.get('id')!r} freezes atlas_context_mode={frozen!r}; "
                f"requested override {requested!r} would change the released atlas."
            )
        return frozen
    if requested is not None and requested != "auto":
        if requested not in CONTEXT_MODES:
            raise MapPlotterError(
                f"Unknown circuit context mode {requested!r}; choose "
                + ", ".join(CONTEXT_MODES)
                + "."
            )
        return requested
    site_type = str(event["circuit"].get("site_type", "hybrid"))
    return _SITE_MODE.get(site_type, "hybrid")


def _meaningful_context_name(feature: Mapping[str, Any]) -> bool:
    copy_value = _context_label_copy(feature)
    if copy_value is None:
        return False
    normalized = copy_value.copy.strip()
    return len(normalized) >= 3 and not normalized.isdigit()


def _context_priority(feature: Mapping[str, Any], kind: str) -> tuple[int, int, str]:
    order = {
        "water": 0,
        "pit-building": 1,
        "paddock": 2,
        "garage": 3,
        "principal-building": 4,
        "grandstand": 5,
        "runoff": 6,
        "gravel-trap": 7,
        "access-road": 8,
        "woodland": 9,
        "grass": 10,
        "road": 11,
        "building": 12,
    }
    named = 0 if _meaningful_context_name(feature) else 1
    return (order.get(kind, 99), named, str(feature.get("id", "")))


def _select_context(
    raw_features: Sequence[dict[str, Any]],
    *,
    season: int,
    gate: Mapping[str, Any],
    mode: str,
    viewport: Polygon,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    omissions: list[dict[str, Any]] = []
    candidates: list[tuple[dict[str, Any], str]] = []
    input_counts: dict[str, int] = {}
    for feature in raw_features:
        kind = _normalise_kind(feature["kind"], f"context {feature['id']} kind")
        input_counts[kind] = input_counts.get(kind, 0) + 1
        if kind not in _SUPPORTED_CONTEXT_KINDS:
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": kind,
                    "reason": "unsupported-kind",
                }
            )
            continue
        valid, explicit = _valid_for_season(feature.get("valid_for_season"), season)
        temporary = str(feature.get("temporary_status", "permanent")).casefold()
        frozen_current_osm_grandstand = (
            kind == "grandstand" and _is_frozen_current_osm_grandstand(feature)
        )
        if (
            not frozen_current_osm_grandstand
            and not valid
            and (explicit or temporary != "permanent")
        ):
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": kind,
                    "reason": "not-valid-for-season",
                }
            )
            continue
        if (
            not frozen_current_osm_grandstand
            and temporary != "permanent"
            and not explicit
        ):
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": kind,
                    "reason": "temporary-feature-without-explicit-season",
                }
            )
            continue
        geometry = _geojson_geometry(feature["geometry"], f"context {feature['id']}")
        if not geometry.intersects(viewport):
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": kind,
                    "reason": "outside-atlas-extent",
                }
            )
            continue
        if kind == "road" and _road_rank(feature) > int(gate["road_rank_limit"]):
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": kind,
                    "reason": "paper-road-rank-gate",
                }
            )
            continue
        if mode == "permanent" and kind == "building" and not feature.get("name"):
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": kind,
                    "reason": "unnamed-off-venue-building",
                }
            )
            continue
        candidates.append((feature, kind))

    candidates.sort(key=lambda item: _context_priority(item[0], item[1]))
    selected: list[dict[str, Any]] = []
    selected_counts: dict[str, int] = {}
    limits = gate["feature_limits"]
    for feature, kind in candidates:
        used = selected_counts.get(kind, 0)
        limit = int(limits.get(kind, 0))
        if used >= limit:
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": kind,
                    "reason": "paper-feature-count-gate",
                }
            )
            continue
        checked = copy.deepcopy(feature)
        checked["_render_kind"] = kind
        selected.append(checked)
        selected_counts[kind] = used + 1
    return selected, omissions, input_counts


def _pen_roles(format_id: str) -> dict[str, str]:
    if format_id not in FORMAT_IDS:
        raise MapPlotterError(f"Unknown circuit format {format_id!r}.")
    return {
        "grey": "grey-0-25",
        "green": "green-0-25",
        "blue": "blue-0-25",
        "purple": "purple-0-4",
        "lap": "red-0-4",
        "copy": "black-0-25",
    }


def _diagrammatic_corridor_plan(
    context: Any,
    *,
    nib_mm: float,
) -> _DiagrammaticCorridorPlan:
    course = _object(context.plate.get("race_course"), "format.race_course")
    ink = _text(course.get("ink"), "format.race_course.ink")
    if ink.casefold() != "red":
        raise MapPlotterError(
            "The F1 diagrammatic course corridor requires the format-bound Red ink."
        )
    target = _number(
        course.get("target_width_mm"), "format.race_course.target_width_mm"
    )
    if target <= nib_mm + 1e-9:
        raise MapPlotterError(
            "The F1 diagrammatic course corridor must be wider than one Red nib."
        )
    outer_radius = (target - nib_mm) / 2.0
    for pair_count in range(1, 5):
        pitch = outer_radius / pair_count
        if 0.5 * nib_mm - 1e-9 <= pitch <= 0.9 * nib_mm + 1e-9:
            plan = _DiagrammaticCorridorPlan(
                target_width_mm=target,
                nib_mm=nib_mm,
                pair_pitch_mm=pitch,
                radii_mm=tuple(pitch * index for index in range(1, pair_count + 1)),
            )
            if abs(plan.plotted_width_mm - target) > 1e-9:
                raise MapPlotterError(
                    "The F1 diagrammatic course corridor cannot meet its format width."
                )
            return plan
    raise MapPlotterError(
        f"The {target:g} mm F1 diagrammatic course corridor cannot be built from "
        f"paired {nib_mm:g} mm Red passes at a physical 0.5-0.9 nib pitch."
    )


def _corridor_buffer_rings(
    lap_page: LineString,
    *,
    radius_mm: float,
    minimum_length_mm: float,
) -> dict[str, list[LineString]]:
    """Return both sides of one physical offset radius or fail closed.

    A buffered closed line is used instead of ``offset_curve`` because dense
    sourced circuit vertices can split the inward offset into several valid
    rings.  Those rings are retained as one logical inner pass group.  Missing,
    open, or sub-three-nib pieces are a build hold; the centreline is never
    duplicated as an offset fallback.
    """

    try:
        buffered = lap_page.buffer(
            radius_mm,
            quad_segs=4,
            cap_style="round",
            join_style="round",
        )
    except GEOSException as exc:
        raise MapPlotterError(
            "F1 diagrammatic corridor offset failed; artifact held rather than "
            "falling back to the lap centreline."
        ) from exc
    polygons = list(_polygon_parts(buffered))
    if not polygons:
        raise MapPlotterError(
            "F1 diagrammatic corridor offset is empty; artifact held."
        )
    sides = {
        "outer": [LineString(polygon.exterior.coords) for polygon in polygons],
        "inner": [
            LineString(ring.coords)
            for polygon in polygons
            for ring in polygon.interiors
        ],
    }
    for side, parts in sides.items():
        if not parts:
            raise MapPlotterError(
                f"F1 diagrammatic corridor {side} offset collapsed at "
                f"{radius_mm:g} mm; artifact held."
            )
        for part in parts:
            if not part.is_ring:
                raise MapPlotterError(
                    f"F1 diagrammatic corridor {side} offset is open at "
                    f"{radius_mm:g} mm; artifact held."
                )
            if part.length + 1e-9 < minimum_length_mm:
                raise MapPlotterError(
                    f"F1 diagrammatic corridor {side} offset is below the "
                    "three-nib physical floor; artifact held."
                )
    return sides


def _corridor_local_clearance_zones(
    lap_page: LineString,
    *,
    plan: _DiagrammaticCorridorPlan,
    scale_mm_per_m: float,
) -> list[tuple[dict[str, Any], Polygon]]:
    """Locate non-local course approaches whose drawn corridors would fuse.

    The exact sourced centreline is deliberately excluded from this policy.
    Only its diagrammatic parallel passes are cleared inside small paper-space
    masks, leaving a single physical Red nib through a congested approach.  A
    close approach is a cartographic fact, not evidence of a bridge or tunnel.
    """

    if not math.isfinite(scale_mm_per_m) or scale_mm_per_m <= 0.0:
        raise MapPlotterError("F1 corridor clearance requires a positive map scale.")
    coordinates = [(float(x), float(y)) for x, y, *_rest in lap_page.coords]
    segment_count = len(coordinates) - 1
    if segment_count < 4:
        return []
    segments = [
        LineString((coordinates[index], coordinates[index + 1]))
        for index in range(segment_count)
    ]
    segment_mid_chainages: list[float] = []
    chainage = 0.0
    for segment in segments:
        segment_mid_chainages.append(chainage + segment.length / 2.0)
        chainage += segment.length
    lap_length_mm = chainage
    minimum_safe_gap_mm = 3.0 * plan.nib_mm
    mask_radius_mm = max(plan.target_width_mm, minimum_safe_gap_mm)
    candidates: list[tuple[float, int, int, PointTuple, Polygon]] = []
    for first_index, first in enumerate(segments):
        for second_index in range(first_index + 1, segment_count):
            cyclic_index_separation = min(
                second_index - first_index,
                segment_count - (second_index - first_index),
            )
            if cyclic_index_separation <= 1:
                continue
            raw_chainage_separation = abs(
                segment_mid_chainages[second_index] - segment_mid_chainages[first_index]
            )
            cyclic_chainage_separation = min(
                raw_chainage_separation,
                lap_length_mm - raw_chainage_separation,
            )
            # A sampled hairpin can have many vertices between its two legs.
            # Treat branches as independent only when their midpoints are at
            # least one quarter-lap apart in either cyclic direction.
            if cyclic_chainage_separation + 1e-9 < 0.25 * lap_length_mm:
                continue
            second = segments[second_index]
            paper_clearance_mm = float(first.distance(second))
            nominal_edge_gap_mm = paper_clearance_mm - plan.target_width_mm
            if nominal_edge_gap_mm + 1e-9 >= minimum_safe_gap_mm:
                continue
            first_nearest, second_nearest = nearest_points(first, second)
            midpoint = (
                (float(first_nearest.x) + float(second_nearest.x)) / 2.0,
                (float(first_nearest.y) + float(second_nearest.y)) / 2.0,
            )
            mask = Point(midpoint).buffer(
                mask_radius_mm,
                quad_segs=8,
                cap_style="round",
            )
            candidates.append(
                (
                    paper_clearance_mm,
                    first_index,
                    second_index,
                    midpoint,
                    mask,
                )
            )

    # A long close approach can produce many overlapping segment-pair masks.
    # Keep only the closest deterministic representative inside each existing
    # mask radius.  This prevents an audit ledger from exploding while the
    # retained circular masks still cover the visually ambiguous locality.
    retained: list[tuple[float, int, int, PointTuple, Polygon]] = []
    for candidate in sorted(
        candidates,
        key=lambda value: (value[0], value[1], value[2]),
    ):
        midpoint = candidate[3]
        if any(
            math.hypot(midpoint[0] - other[3][0], midpoint[1] - other[3][1])
            <= 0.75 * mask_radius_mm
            for other in retained
        ):
            continue
        retained.append(candidate)

    result: list[tuple[dict[str, Any], Polygon]] = []
    for zone_index, candidate in enumerate(
        sorted(retained, key=lambda value: (value[1], value[2])),
        start=1,
    ):
        paper_clearance_mm, first_index, second_index, _midpoint, mask = candidate
        result.append(
            (
                {
                    "id": f"local-clearance-{zone_index:03d}",
                    "segment_indexes": [first_index, second_index],
                    "source_clearance_m": round(paper_clearance_mm / scale_mm_per_m, 6),
                    "paper_clearance_mm": round(paper_clearance_mm, 6),
                    "nominal_edge_gap_mm": round(
                        paper_clearance_mm - plan.target_width_mm, 6
                    ),
                    "minimum_safe_edge_gap_mm": round(minimum_safe_gap_mm, 6),
                    "mask_bounds_mm": [round(float(value), 6) for value in mask.bounds],
                    "mask_radius_mm": round(mask_radius_mm, 6),
                },
                mask,
            )
        )
    return result


def _emit_diagrammatic_corridor_offsets(
    layer: ArtworkLayer,
    lap_page: LineString,
    *,
    plan: _DiagrammaticCorridorPlan,
    source_ref: str,
    source_object_id: str,
    geometry_sha256: str,
    lap_source_sha256: str,
    scale_mm_per_m: float,
) -> dict[str, Any]:
    minimum_length_mm = 3.0 * layer.pen.mark_width_mm
    clearance_zones = _corridor_local_clearance_zones(
        lap_page,
        plan=plan,
        scale_mm_per_m=scale_mm_per_m,
    )
    clearance_mask = (
        unary_union([mask for _record, mask in clearance_zones])
        if clearance_zones
        else GeometryCollection()
    )
    groups: list[dict[str, Any]] = []
    emitted_path_count = 0
    for radius_index, radius_mm in enumerate(plan.radii_mm, start=1):
        sides = _corridor_buffer_rings(
            lap_page,
            radius_mm=radius_mm,
            minimum_length_mm=minimum_length_mm,
        )
        for side in ("outer", "inner"):
            group_id = f"radius-{radius_index}-{side}"
            parts = sides[side]
            group_length_mm = 0.0
            group_path_count = 0
            group_closed_path_count = 0
            group_clipped_path_count = 0
            for source_part in parts:
                clipped = bool(
                    clearance_zones and source_part.intersects(clearance_mask)
                )
                rendered = (
                    source_part.difference(clearance_mask) if clipped else source_part
                )
                rendered_parts = [
                    part
                    for part in _line_parts(rendered)
                    if part.length + 1e-9 >= minimum_length_mm
                ]
                if not rendered_parts:
                    continue
                zone_ids = [
                    str(record["id"])
                    for record, mask in clearance_zones
                    if source_part.intersects(mask)
                ]
                for part in rendered_parts:
                    part_index = group_path_count
                    points = [(float(x), float(y)) for x, y, *_rest in part.coords]
                    group_length_mm += polyline_length_mm(points)
                    attributes = {
                        "data-claim": "DIAGRAMMATIC COURSE CORRIDOR",
                        "data-diagrammatic": "true",
                        "data-racing-line": "false",
                        "data-surveyed-track-width": "false",
                        "data-offset-fallback": "false",
                        "data-offset-group-id": group_id,
                        "data-offset-side": side,
                        "data-offset-radius-mm": format_number(radius_mm),
                        "data-offset-part-index": str(part_index),
                        "data-course-target-width-mm": format_number(
                            plan.target_width_mm
                        ),
                        "data-source-object-id": source_object_id,
                        "data-source-geometry-sha256": geometry_sha256,
                        "data-source-lap-sha256": lap_source_sha256,
                        "data-clearance-clipped": str(clipped).lower(),
                        "data-clearance-zone-ids": "|".join(zone_ids),
                        "data-derivation": (
                            "buffer-envelope-from-exact-sourced-lap-centreline"
                            + (
                                ";derived-local-clearance-subtraction"
                                if clipped
                                else ""
                            )
                        ),
                    }
                    layer.add(
                        points,
                        source_ref=source_ref,
                        role="diagrammatic-course-corridor-offset",
                        attributes=attributes,
                    )
                    group_path_count += 1
                    group_closed_path_count += int(part.is_ring)
                    group_clipped_path_count += int(clipped)
                    emitted_path_count += 1
            if group_path_count <= 0:
                raise MapPlotterError(
                    f"F1 diagrammatic corridor {group_id} was completely "
                    "removed by local-clearance masks; artifact held."
                )
            groups.append(
                {
                    "id": group_id,
                    "side": side,
                    "radius_mm": round(radius_mm, 6),
                    "path_count": group_path_count,
                    "closed_path_count": group_closed_path_count,
                    "open_path_count": (group_path_count - group_closed_path_count),
                    "clearance_clipped_path_count": group_clipped_path_count,
                    "length_mm": round(group_length_mm, 6),
                }
            )
    expected_group_count = 2 * len(plan.radii_mm)
    if len(groups) != expected_group_count or any(
        int(group["path_count"]) <= 0 for group in groups
    ):
        raise MapPlotterError(
            "F1 diagrammatic corridor offset parity failed; artifact held."
        )
    return {
        "policy": "paired-buffer-envelope-passes-from-exact-centreline-v1",
        "local_clearance_policy": LOCAL_CORRIDOR_CLEARANCE_POLICY,
        "minimum_safe_edge_gap_mm": round(3.0 * plan.nib_mm, 6),
        "clearance_zone_count": len(clearance_zones),
        "clearance_zones": [record for record, _mask in clearance_zones],
        "red_centreline_modified_for_local_clearance": False,
        "white_ink_used_for_local_clearance": False,
        "visible_claim": (
            "DIAGRAMMATIC CORRIDOR / NOT SURVEYED TRACK WIDTH OR RACING LINE"
        ),
        "source_centreline_path_count": 1,
        "source_centreline_coordinate_count": len(lap_page.coords),
        "source_lap_sha256": lap_source_sha256,
        "target_width_mm": round(plan.target_width_mm, 6),
        "pen_id": layer.pen_id,
        "nib_mm": round(plan.nib_mm, 6),
        "pair_pitch_mm": round(plan.pair_pitch_mm, 6),
        "radii_mm": [round(value, 6) for value in plan.radii_mm],
        "logical_stroke_count": plan.logical_stroke_count,
        "plotted_width_mm": round(plan.plotted_width_mm, 6),
        "expected_offset_group_count": expected_group_count,
        "emitted_offset_group_count": len(groups),
        "emitted_offset_path_count": emitted_path_count,
        "offset_groups": groups,
        "hold_on_offset_failure": True,
        "offset_fallback_allowed": False,
        "surveyed_track_width_claimed": False,
        "racing_line_claimed": False,
    }


def _serialized_polyline_points(
    points: Iterable[Sequence[float]],
) -> list[PointTuple]:
    return [
        (float(format_number(point[0])), float(format_number(point[1])))
        for point in points
    ]


def _serialized_polyline_length_mm(
    points: Iterable[Sequence[float]],
) -> float:
    return polyline_length_mm(_serialized_polyline_points(points))


def _emit_linework(
    layer: ArtworkLayer,
    geometry: BaseGeometry,
    *,
    source_ref: str,
    role: str,
    attributes: Mapping[str, str] | None,
    omissions: list[dict[str, Any]],
    feature_id: str,
) -> dict[str, float | int]:
    minimum = 3.0 * layer.pen.mark_width_mm
    emitted = 0
    omitted = 0
    length = 0.0
    line_parts = list(_line_parts(geometry))
    if not line_parts:
        # A source-selected feature can be consumed completely by a factual
        # label/north-mark knockout.  That is still a real post-clip omission,
        # not permission to vanish from the source ledger.  Record the
        # zero-length result under the same physical-floor reason accepted for
        # any other sub-three-nib remnant so selected/emitted/culled partitions
        # remain independently auditable.
        omissions.append(
            {
                "feature_id": feature_id,
                "role": role,
                "reason": "post-clip-below-three-nib-floor",
                "length_mm": 0.0,
                "pre_serialization_length_mm": round(float(geometry.length), 6),
                "measurement_basis": "serialized-0.001-mm-coordinate-grid",
                "minimum_mm": round(minimum, 6),
            }
        )
        return {"paths": 0, "omitted_paths": 1, "length_mm": 0.0}
    for line in line_parts:
        source_points = [(float(x), float(y)) for x, y, *_rest in line.coords]
        # The SVG binding quantizes every coordinate to 0.001 mm.  Apply that
        # exact serializer grid before the physical-floor decision and retain
        # those same points in the artwork record.  In-memory validation,
        # manifests, semantic QA and generic format QA therefore measure the
        # identical path instead of admitting a 0.750 mm source-space stroke
        # that becomes 0.749 mm in the actual plot file.
        points = _serialized_polyline_points(source_points)
        candidate_length = polyline_length_mm(points)
        if candidate_length + 1e-9 < minimum:
            omissions.append(
                {
                    "feature_id": feature_id,
                    "role": role,
                    "reason": "post-clip-below-three-nib-floor",
                    "length_mm": round(candidate_length, 6),
                    "pre_serialization_length_mm": round(
                        polyline_length_mm(source_points), 6
                    ),
                    "measurement_basis": "serialized-0.001-mm-coordinate-grid",
                    "minimum_mm": round(minimum, 6),
                }
            )
            omitted += 1
            continue
        layer.add(
            points,
            source_ref=source_ref,
            role=role,
            attributes=dict(attributes or {}),
        )
        emitted += 1
        length += candidate_length
    return {"paths": emitted, "omitted_paths": omitted, "length_mm": length}


def _grid_points(
    geometry: BaseGeometry, spacing: float, *, maximum: int
) -> Iterable[PointTuple]:
    if geometry.is_empty or spacing <= 0.0:
        return
    min_x, min_y, max_x, max_y = geometry.bounds
    row = 0
    count = 0
    y = min_y + spacing / 2.0
    while y <= max_y and count < maximum:
        offset = spacing / 2.0 if row % 2 else 0.0
        x = min_x + spacing / 2.0 + offset
        while x <= max_x and count < maximum:
            point = Point(x, y)
            if geometry.covers(point):
                yield (x, y)
                count += 1
            x += spacing
        row += 1
        y += spacing


def _water_stipple(
    layer: ArtworkLayer,
    geometry: BaseGeometry,
    *,
    spacing_mm: float,
    source_ref: str,
    source_object_id: str,
    feature_id: str,
    maximum: int,
    role: str = "water-stipple-dot",
    derivation: str = "stipple-inside-source-water",
) -> int:
    radius = max(0.18, 3.0 * layer.pen.mark_width_mm / (2.0 * math.pi) + 0.02)
    safe = geometry.buffer(-(radius + layer.pen.mark_width_mm / 2.0))
    if safe.is_empty:
        return 0
    centres = list(_grid_points(safe, spacing_mm, maximum=maximum))
    fallback = False
    if not centres:
        representative = safe.representative_point()
        centres = [(float(representative.x), float(representative.y))]
        fallback = True
    count = 0
    for centre in centres:
        layer.add(
            circle_stroke(centre, radius, segments=16),
            source_ref=source_ref,
            role=role,
            attributes={
                "data-feature-id": feature_id,
                "data-digital-fill": "false",
                "data-source-object-id": source_object_id,
                "data-derivation": derivation,
                **({"data-stipple-fallback": "true"} if fallback else {}),
            },
        )
        count += 1
    return count


def _area_hatch(
    layer: ArtworkLayer,
    geometry: BaseGeometry,
    *,
    spacing_mm: float,
    source_ref: str,
    source_object_id: str,
    feature_id: str,
    maximum: int,
) -> int:
    """Draw sparse 45-degree strokes clipped to a sourced area."""

    if geometry.is_empty or spacing_mm <= 0.0:
        return 0
    safe = geometry.buffer(-layer.pen.mark_width_mm / 2.0)
    if safe.is_empty:
        return 0
    min_x, min_y, max_x, max_y = safe.bounds
    height = max_y - min_y
    offset = min_x - height
    count = 0
    attributes = {
        "data-feature-id": feature_id,
        "data-digital-fill": "false",
        "data-source-object-id": source_object_id,
        "data-derivation": "hatch-clipped-inside-source-runoff",
    }
    while offset <= max_x and count < maximum:
        guide = LineString([(offset, min_y), (offset + height, max_y)])
        clipped = guide.intersection(safe)
        for part in _line_parts(clipped):
            points = [(float(x), float(y)) for x, y, *_rest in part.coords]
            if polyline_length_mm(points) < 3.0 * layer.pen.mark_width_mm:
                continue
            layer.add(
                points,
                source_ref=source_ref,
                role="runoff-hatch",
                attributes=attributes,
            )
            count += 1
            if count >= maximum:
                break
        offset += spacing_mm
    return count


def _vegetation_symbols(
    layer: ArtworkLayer,
    geometry: BaseGeometry,
    *,
    kind: str,
    spacing_mm: float,
    source_ref: str,
    source_object_id: str,
    feature_id: str,
    maximum: int,
) -> int:
    symbol_height = max(1.2, 4.0 * layer.pen.mark_width_mm)
    safe = geometry.buffer(-(symbol_height + layer.pen.mark_width_mm) / 2.0)
    count = 0
    for x, y in (
        _grid_points(safe, spacing_mm, maximum=maximum) if not safe.is_empty else ()
    ):
        if kind == "woodland":
            points = [
                (x - 0.55 * symbol_height, y + 0.45 * symbol_height),
                (x, y - 0.55 * symbol_height),
                (x + 0.55 * symbol_height, y + 0.45 * symbol_height),
                (x - 0.55 * symbol_height, y + 0.45 * symbol_height),
            ]
            role = "woodland-symbol"
        else:
            points = [
                (x - 0.55 * symbol_height, y + 0.45 * symbol_height),
                (x, y - 0.35 * symbol_height),
                (x, y + 0.45 * symbol_height),
                (x + 0.55 * symbol_height, y - 0.15 * symbol_height),
            ]
            role = "grass-symbol"
        layer.add(
            points,
            source_ref=source_ref,
            role=role,
            attributes={
                "data-feature-id": feature_id,
                "data-digital-fill": "false",
                "data-source-object-id": source_object_id,
                "data-derivation": "symbol-inside-source-area",
            },
        )
        count += 1
    if count:
        return count

    # A selected vegetation feature whose full symbol grid has no viable cell
    # still needs a physical representation before its outline can become a
    # discretionary density-budget item.  Try one short centreline glyph fully
    # inside the source area; if even three nib widths cannot fit, the caller
    # retains that feature's complete source-boundary outline instead.
    fallback_safe = geometry.buffer(-layer.pen.mark_width_mm / 2.0)
    if fallback_safe.is_empty:
        return 0
    representative = fallback_safe.representative_point()
    min_x, min_y, max_x, max_y = fallback_safe.bounds
    guide_half_length = max(max_x - min_x, max_y - min_y, symbol_height) * 1.5
    directions = (
        (1.0, 0.0),
        (0.0, 1.0),
        (2**-0.5, 2**-0.5),
        (2**-0.5, -(2**-0.5)),
    )
    minimum = 3.0 * layer.pen.mark_width_mm
    fallback_candidates: list[tuple[float, list[PointTuple]]] = []
    for dx, dy in directions:
        guide = LineString(
            [
                (
                    representative.x - guide_half_length * dx,
                    representative.y - guide_half_length * dy,
                ),
                (
                    representative.x + guide_half_length * dx,
                    representative.y + guide_half_length * dy,
                ),
            ]
        )
        for part in _line_parts(guide.intersection(fallback_safe)):
            if part.length <= 0.0:
                continue
            glyph_length = min(symbol_height, part.length)
            midpoint = part.length / 2.0
            first = part.interpolate(max(0.0, midpoint - glyph_length / 2.0))
            second = part.interpolate(min(part.length, midpoint + glyph_length / 2.0))
            points = [
                (float(first.x), float(first.y)),
                (float(second.x), float(second.y)),
            ]
            serialized_length = _serialized_polyline_length_mm(points)
            if serialized_length + 1e-9 >= minimum:
                fallback_candidates.append((serialized_length, points))
    if not fallback_candidates:
        return 0
    _length, points = max(
        fallback_candidates,
        key=lambda candidate: (candidate[0], candidate[1]),
    )
    layer.add(
        _serialized_polyline_points(points),
        source_ref=source_ref,
        role="woodland-symbol" if kind == "woodland" else "grass-symbol",
        attributes={
            "data-feature-id": feature_id,
            "data-digital-fill": "false",
            "data-source-object-id": source_object_id,
            "data-derivation": "representative-stroke-inside-source-area",
            "data-symbol-fallback": "true",
        },
    )
    return 1


def _line_tangent(line: LineString, distance: float) -> PointTuple:
    epsilon = max(0.2, min(2.0, line.length * 0.002))
    before = line.interpolate(max(0.0, distance - epsilon))
    after = line.interpolate(min(line.length, distance + epsilon))
    dx = after.x - before.x
    dy = after.y - before.y
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return (1.0, 0.0)
    return (dx / length, dy / length)


def _nearest_distance(line: LineString, point: PointTuple) -> float:
    return float(line.project(Point(point)))


def _label_rect(
    copy_text: str, centre: PointTuple, cap_mm: float, padding: float
) -> Rect:
    width = text_width_mm(copy_text, cap_height_mm=cap_mm)
    return Rect(
        centre[0] - width / 2.0 - padding,
        centre[1] - cap_mm / 2.0 - padding,
        width + 2.0 * padding,
        cap_mm + 2.0 * padding,
    )


def _inside(inner: Rect, outer: Rect) -> bool:
    return (
        inner.left >= outer.left - 1e-9
        and inner.right <= outer.right + 1e-9
        and inner.top >= outer.top - 1e-9
        and inner.bottom <= outer.bottom + 1e-9
    )


def _candidate_centres(
    anchor: PointTuple,
    preferred: PointTuple,
    *,
    cap_mm: float,
    field: Rect,
) -> list[PointTuple]:
    px, py = preferred
    preferred_angle = math.atan2(py, px)
    angles: list[float] = [
        preferred_angle,
        preferred_angle + math.pi,
    ]
    for step in range(1, 12):
        delta = step * math.pi / 12.0
        angles.extend((preferred_angle + delta, preferred_angle - delta))
    radii = [
        max(3.4, 1.8 * cap_mm),
        max(5.5, 2.6 * cap_mm),
        max(8.0, 3.5 * cap_mm),
        max(11.0, 4.5 * cap_mm),
        max(15.0, 6.0 * cap_mm),
        max(20.0, 8.0 * cap_mm),
    ]
    candidates = [
        (anchor[0] + radius * math.cos(angle), anchor[1] + radius * math.sin(angle))
        for radius in radii
        for angle in angles
    ]
    # A deterministic perimeter fallback prevents a dense hairpin complex from
    # silently losing turn numbers.  It still has to pass every collision and
    # leader-crossing gate below.
    pitch = max(5.0, 2.5 * cap_mm)
    x = field.left + pitch
    while x <= field.right - pitch:
        candidates.extend(((x, field.top + pitch), (x, field.bottom - pitch)))
        x += pitch
    y = field.top + pitch
    while y <= field.bottom - pitch:
        candidates.extend(((field.left + pitch, y), (field.right - pitch, y)))
        y += pitch
    return sorted(
        candidates,
        key=lambda candidate: math.hypot(
            candidate[0] - anchor[0], candidate[1] - anchor[1]
        ),
    )


def _leader_route(
    *,
    anchor: PointTuple,
    endpoint: PointTuple,
    preferred: PointTuple,
    field: Rect,
    protected_ink: BaseGeometry,
    boxes: Sequence[Rect],
    leaders: Sequence[LineString],
    minimum_leader_mm: float,
    track_clearance_mm: float,
    leader_nib_mm: float,
    allow_elbows: bool = True,
    maximum_leader_mm: float | None = None,
) -> tuple[PointTuple, ...] | None:
    """Find a short deterministic leader, using elbows only when required."""

    # A leader is required to begin on the lap, so its first segment needs a
    # tightly bounded attachment port through the lap's protected buffer.  A
    # fixed 0.2 mm surplus was smaller than one 0.25 mm physical nib and could
    # reject an otherwise clear oblique exit after SVG quantisation.  Two nibs
    # beyond the binding track clearance covers the complete anchor join while
    # remaining local: any later re-entry into lap/pit protected ink is still
    # rejected below.
    own_clearance = Point(anchor).buffer(
        track_clearance_mm + max(0.5, 2.0 * leader_nib_mm)
    )
    field_shape = _rect_polygon(field)

    def valid(points: tuple[PointTuple, ...]) -> bool:
        line = LineString(points)
        if (
            not line.is_simple
            or line.length + 1e-9 < minimum_leader_mm
            or (
                maximum_leader_mm is not None and line.length > maximum_leader_mm + 1e-9
            )
            or not field_shape.covers(line)
        ):
            return False
        remainder = line.difference(own_clearance)
        if not remainder.is_empty and remainder.intersects(protected_ink):
            return False
        if any(line.crosses(other) or line.overlaps(other) for other in leaders):
            return False
        return not any(line.crosses(_rect_polygon(other)) for other in boxes)

    direct = (anchor, endpoint)
    if valid(direct):
        return direct
    if not allow_elbows:
        return None

    preferred_angle = math.atan2(preferred[1], preferred[0])
    angle_steps = [0]
    for step in range(1, 9):
        angle_steps.extend((step, -step))
    for step in angle_steps:
        angle = preferred_angle + step * math.pi / 16.0
        for radius in (2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0):
            escape = (
                anchor[0] + radius * math.cos(angle),
                anchor[1] + radius * math.sin(angle),
            )
            route_candidates = (
                (anchor, escape, endpoint),
                (anchor, escape, (escape[0], endpoint[1]), endpoint),
                (anchor, escape, (endpoint[0], escape[1]), endpoint),
            )
            for route in route_candidates:
                # Remove duplicate elbow vertices before constructing GEOS
                # linework; exact repeats make an otherwise useful route
                # non-simple.
                compact = tuple(
                    point
                    for index, point in enumerate(route)
                    if index == 0 or point != route[index - 1]
                )
                if len(compact) >= 2 and valid(compact):
                    # Radius and angular offset are already ordered from the
                    # preferred, shortest escape.  Returning immediately keeps
                    # the backtracking solver tractable on all six formats.
                    return compact
    return None


def _station_label_candidates(
    *,
    label_id: str,
    copies: Sequence[tuple[str, bool]],
    anchor: PointTuple,
    preferred: PointTuple,
    source_ref: str,
    role: str,
    cap_mm: float,
    label_pen_nib_mm: float,
    field: Rect,
    protected_ink: BaseGeometry,
    boxes: list[Rect],
    leaders: list[LineString],
    separation_mm: float,
    track_clearance_mm: float,
    feature_id: str | None = None,
    source_object_id: str | None = None,
    source_name_key: str | None = None,
    source_copy: str | None = None,
    copy_policy_id: str | None = None,
    normalisation_policy_id: str | None = None,
    display_punctuation_policy_id: str | None = None,
    per_copy_limit: int = 1,
    maximum_leader_mm: float | None = None,
) -> list[_LabelPlacement]:
    """Return deterministic collision-free station-label candidates.

    More than one candidate is needed by the complete-layout solver: accepting
    the nearest locally valid label can consume the only viable corridor for a
    later hairpin station.  Candidates remain ordered by copy preference and
    leader length, so the search still favours sourced names and short travel.
    """

    if per_copy_limit < 1:
        raise ValueError("per_copy_limit must be positive")
    padding = max(0.35, label_pen_nib_mm)
    minimum_leader = 3.0 * label_pen_nib_mm

    def collect(*, allow_elbows: bool) -> list[_LabelPlacement]:
        candidates: list[_LabelPlacement] = []
        for copy_text, displays_name in copies:
            copy_count = 0
            for centre in _candidate_centres(
                anchor, preferred, cap_mm=cap_mm, field=field
            ):
                bounds = _label_rect(copy_text, centre, cap_mm, padding)
                if not _inside(bounds, field):
                    continue
                bounds_shape = _rect_polygon(bounds)
                if protected_ink.intersects(bounds_shape):
                    continue
                if any(
                    _rect_polygon(other).buffer(separation_mm).intersects(bounds_shape)
                    for other in boxes
                ):
                    continue
                if any(existing.intersects(bounds_shape) for existing in leaders):
                    continue
                edge = nearest_points(Point(anchor), bounds_shape.boundary)[1]
                endpoint = (float(edge.x), float(edge.y))
                leader = _leader_route(
                    anchor=anchor,
                    endpoint=endpoint,
                    preferred=preferred,
                    field=field,
                    protected_ink=protected_ink,
                    boxes=boxes,
                    leaders=leaders,
                    minimum_leader_mm=minimum_leader,
                    track_clearance_mm=track_clearance_mm,
                    leader_nib_mm=label_pen_nib_mm,
                    allow_elbows=allow_elbows,
                    maximum_leader_mm=maximum_leader_mm,
                )
                if leader is None:
                    continue
                candidates.append(
                    _LabelPlacement(
                        id=label_id,
                        copy=copy_text,
                        bounds=bounds,
                        anchor=anchor,
                        leader=leader,
                        source_ref=source_ref,
                        role=role,
                        displayed_name=displays_name,
                        cap_mm=cap_mm,
                        feature_id=feature_id,
                        source_object_id=source_object_id,
                        source_name_key=source_name_key,
                        source_copy=source_copy,
                        copy_policy_id=copy_policy_id,
                        normalisation_policy_id=normalisation_policy_id,
                        display_punctuation_policy_id=(display_punctuation_policy_id),
                    )
                )
                copy_count += 1
                if copy_count >= per_copy_limit:
                    break
        return candidates

    direct = collect(allow_elbows=False)
    return direct if direct else collect(allow_elbows=True)


def _place_station_label(
    *,
    label_id: str,
    copies: Sequence[tuple[str, bool]],
    anchor: PointTuple,
    preferred: PointTuple,
    source_ref: str,
    role: str,
    cap_mm: float,
    label_pen_nib_mm: float,
    field: Rect,
    protected_ink: BaseGeometry,
    boxes: list[Rect],
    leaders: list[LineString],
    separation_mm: float,
    track_clearance_mm: float,
    feature_id: str | None = None,
    source_object_id: str | None = None,
    source_name_key: str | None = None,
    source_copy: str | None = None,
    copy_policy_id: str | None = None,
    normalisation_policy_id: str | None = None,
    display_punctuation_policy_id: str | None = None,
    maximum_leader_mm: float | None = None,
) -> _LabelPlacement | None:
    candidates = _station_label_candidates(
        label_id=label_id,
        copies=copies,
        anchor=anchor,
        preferred=preferred,
        source_ref=source_ref,
        role=role,
        cap_mm=cap_mm,
        label_pen_nib_mm=label_pen_nib_mm,
        field=field,
        protected_ink=protected_ink,
        boxes=boxes,
        leaders=leaders,
        separation_mm=separation_mm,
        track_clearance_mm=track_clearance_mm,
        feature_id=feature_id,
        source_object_id=source_object_id,
        source_name_key=source_name_key,
        source_copy=source_copy,
        copy_policy_id=copy_policy_id,
        normalisation_policy_id=normalisation_policy_id,
        display_punctuation_policy_id=display_punctuation_policy_id,
        per_copy_limit=1,
        maximum_leader_mm=maximum_leader_mm,
    )
    return candidates[0] if candidates else None


def _normalise_context_display_punctuation(value: str) -> str:
    """Normalize display-only delimiters without changing source provenance.

    OSM names occasionally carry a backslash as a multilingual/name-part
    separator or an orphan punctuation mark at the very end.  A pen plot
    should show the former as the familiar spaced solidus and should not spend
    ink on the latter.  Internal punctuation—including apostrophes, hyphens,
    commas, semicolons, and colons—remains untouched.
    """

    normalized = re.sub(r"\s*\\+\s*", " / ", value)
    normalized = re.sub(r"[,;:]+\s*$", "", normalized)
    return " ".join(normalized.split())


def _drawable_context_copy(value: str) -> str | None:
    """Return one faithful plotter-font copy, or reject the whole source value.

    The shared stroke font preserves supported precomposed accents and applies
    NFKD diacritic stripping only to unsupported *Latin* letters.  Crucially,
    this function never deletes individual unsupported-script characters: a
    mixed or non-Latin value is rejected as a unit so it cannot become the
    missing-letter pseudo-words produced by the former ``[^A-Z0-9]`` regex.
    """

    source_copy = " ".join(value.split())
    if not source_copy:
        return None
    copy_text = _normalise_context_display_punctuation(
        " ".join(normalise_text(source_copy.upper()).split())
    )
    if not copy_text:
        return None
    try:
        # Width measurement exercises the same fail-closed glyph check used by
        # physical vector emission without coupling the policy to page scale.
        text_width_mm(copy_text, cap_height_mm=1.0)
    except MapPlotterError:
        return None
    return copy_text


def _context_label_copy(
    feature: Mapping[str, Any],
) -> _ContextLabelCopy | None:
    """Choose the first wholly drawable, explicitly sourced name value.

    Local ``name`` remains authoritative whenever the plotter font can render
    it faithfully.  A sourced English, international, or Latin-script tag is
    a fallback, not an invented translation.  If none is drawable the caller
    records an explicit omission instead of emitting a partial word.
    """

    tags_value = feature.get("tags")
    tags = tags_value if isinstance(tags_value, Mapping) else {}
    for source_name_key in CONTEXT_LABEL_SOURCE_KEYS:
        value = (
            feature.get("name")
            if source_name_key == "name"
            else tags.get(source_name_key)
        )
        if not isinstance(value, str) or not value.strip():
            continue
        copy_text = _drawable_context_copy(value)
        if copy_text is None:
            continue
        return _ContextLabelCopy(
            copy=copy_text,
            source_name_key=source_name_key,
            source_copy=value,
        )
    return None


def _course_section_label_copy(
    section: Mapping[str, Any],
) -> _ContextLabelCopy | None:
    """Return official source copy when present, otherwise the OSM-name policy.

    Famous-section copy and its geographic anchor deliberately come from
    different sources.  This helper keeps the visible copy bound to the
    Formula 1 text field instead of accidentally re-labelling it as an OSM
    ``name`` tag.
    """

    if section.get("name_status") != FAMOUS_SECTION_NAME_STATUS:
        return _context_label_copy(section)
    source_copy = section.get("source_copy")
    if not isinstance(source_copy, str) or not source_copy.strip():
        return None
    copy_text = _drawable_context_copy(source_copy)
    if copy_text is None:
        return None
    return _ContextLabelCopy(
        copy=copy_text,
        source_name_key="official-source-copy",
        source_copy=source_copy,
    )


def _place_context_label(
    *,
    label_id: str,
    copy_text: str,
    anchor: PointTuple,
    source_ref: str,
    cap_mm: float,
    label_pen_nib_mm: float,
    field: Rect,
    protected_ink: BaseGeometry,
    boxes: list[Rect],
    leaders: list[LineString],
    separation_mm: float,
    source_name_key: str,
    source_copy: str,
    copy_policy_id: str,
    normalisation_policy_id: str,
    display_punctuation_policy_id: str,
    feature_id: str | None = None,
    source_object_id: str | None = None,
) -> _LabelPlacement | None:
    padding = max(0.35, label_pen_nib_mm)
    offsets = (
        (0.0, 0.0),
        (0.0, 1.6 * cap_mm),
        (0.0, -1.6 * cap_mm),
        (2.4 * cap_mm, 0.0),
        (-2.4 * cap_mm, 0.0),
        (0.0, 4.0 * cap_mm),
        (0.0, -4.0 * cap_mm),
        (4.0 * cap_mm, 0.0),
        (-4.0 * cap_mm, 0.0),
        (4.0 * cap_mm, 4.0 * cap_mm),
        (-4.0 * cap_mm, 4.0 * cap_mm),
        (4.0 * cap_mm, -4.0 * cap_mm),
        (-4.0 * cap_mm, -4.0 * cap_mm),
        (0.0, 6.0 * cap_mm),
        (0.0, -6.0 * cap_mm),
    )
    for dx, dy in offsets:
        centre = (anchor[0] + dx, anchor[1] + dy)
        bounds = _label_rect(copy_text, centre, cap_mm, padding)
        if not _inside(bounds, field):
            continue
        shape_value = _rect_polygon(bounds)
        if protected_ink.intersects(shape_value):
            continue
        if any(
            _rect_polygon(other).buffer(separation_mm).intersects(shape_value)
            for other in boxes
        ):
            continue
        if any(existing.intersects(shape_value) for existing in leaders):
            continue
        return _LabelPlacement(
            id=label_id,
            copy=copy_text,
            bounds=bounds,
            anchor=anchor,
            leader=None,
            source_ref=source_ref,
            role="context-label",
            displayed_name=True,
            cap_mm=cap_mm,
            feature_id=feature_id,
            source_object_id=source_object_id,
            source_name_key=source_name_key,
            source_copy=source_copy,
            copy_policy_id=copy_policy_id,
            normalisation_policy_id=normalisation_policy_id,
            display_punctuation_policy_id=display_punctuation_policy_id,
        )
    return None


def _even_name_ids(
    stations: Sequence[Mapping[str, Any]], limit: int | None
) -> set[str]:
    named = [station for station in stations if station.get("name")]
    if limit is None or len(named) <= limit:
        return {str(station["id"]) for station in named}
    if limit <= 0:
        return set()
    chosen: set[str] = set()
    for index in range(limit):
        position = round(index * (len(named) - 1) / max(limit - 1, 1))
        chosen.add(str(named[position]["id"]))
    return chosen


def _title_fits(
    text: str,
    zone: Rect,
    preferred_cap_mm: float,
    minimum_cap_mm: float,
    *,
    condense_nib_mm: float | None = None,
    allow_single_word_condense: bool = False,
) -> bool:
    words = text.split()
    title_nib_mm = float(condense_nib_mm or 0.0)
    maximum_path_width = zone.width - title_nib_mm
    natural_width = text_width_mm(text, cap_height_mm=preferred_cap_mm)
    fitted_single_cap = min(
        preferred_cap_mm,
        preferred_cap_mm * maximum_path_width / natural_width,
    )
    # Keep this first branch identical to `niche_common._add_title`: preserve
    # the established natural-width selection gate, then allow its cap to
    # reduce only as far as the physical floor so the actual nib envelope fits
    # within the title zone.
    if (
        natural_width <= zone.width + 1e-9
        and fitted_single_cap + 1e-9 >= minimum_cap_mm
    ):
        return True
    if any(
        max(
            text_width_mm(" ".join(words[:index]), cap_height_mm=minimum_cap_mm),
            text_width_mm(" ".join(words[index:]), cap_height_mm=minimum_cap_mm),
        )
        <= maximum_path_width + 1e-9
        for index in range(1, len(words))
    ):
        return True
    if condense_nib_mm is None:
        return False
    # `text_strokes_fit` preserves the physical cap-height floor and permits
    # F1 title strokes to condense horizontally only to 65%.  Mirror that
    # exact feasibility gate here so the semantic title selector neither
    # rejects a legal wrap nor approves a word the renderer cannot plot.
    # `_add_title` passes the nib-inset path width to `text_strokes_fit`; its
    # condensed branch reserves a further nib at each edge. Mirror that exact
    # feasibility gate here.
    usable_width = max(maximum_path_width - 2.0 * condense_nib_mm, 0.0)
    maximum_natural_width = usable_width / 0.65
    if len(words) == 1:
        # A single factual venue name has no honest editorial wrap point.
        # The shared title compositor can retain the binding cap-height floor
        # and condense the vector glyphs horizontally to 65%, so mirror that
        # exact one-line feasibility gate here instead of inventing a space or
        # rejecting names such as HUNGARORING on landscape sheets.
        return allow_single_word_condense and (
            text_width_mm(text, cap_height_mm=preferred_cap_mm)
            <= maximum_natural_width + 1e-9
        )
    if len(words) < 2:
        return False
    return any(
        max(
            text_width_mm(" ".join(words[:index]), cap_height_mm=minimum_cap_mm),
            text_width_mm(" ".join(words[index:]), cap_height_mm=minimum_cap_mm),
        )
        <= maximum_natural_width + 1e-9
        for index in range(1, len(words))
    )


def _compact_title(event: Mapping[str, Any], context: Any) -> tuple[str, str]:
    circuit = event["circuit"]
    neutral = str(event["neutral_display_title"]).strip().upper()
    full = (
        str(
            circuit.get("neutral_display_title") or circuit.get("short_name") or neutral
        )
        .strip()
        .upper()
    )
    official = str(circuit.get("official_name", circuit.get("name"))).strip().upper()
    raw_candidates = [
        neutral,
        str(circuit.get("short_name", "")).strip().upper(),
        full,
        official,
    ]
    simplified = re.sub(
        r"\b(INTERNATIONAL|MOTORSPORT|CIRCUIT|AUTODROME|AUTODROMO)\b",
        "",
        official,
    )
    raw_candidates.append(" ".join(simplified.split()))
    candidates: list[str] = []
    for candidate in raw_candidates:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        # The landscape title rail can wrap a neutral compound name across
        # two physical lines, but `_add_title` intentionally uses spaces as
        # its only editorial line-break opportunities.  Add a deterministic
        # display-only decomposition after the exact spelling.  The original
        # `full` value remains the SVG/manifest document title below.
        wrapped_candidate = re.sub(r"(?<=\w)-(?=\w)", " ", candidate)
        if wrapped_candidate and wrapped_candidate not in candidates:
            candidates.append(wrapped_candidate)
    title_zone = context.zones["title"]
    preferred = float(context.plate["type_scale_mm"]["title"])
    minimum = float(context.plate["rules"]["min_cap_height_mm"]["title"])
    title_nib_role = str(context.plate["type_nib_role"]["title"])
    title_nib_mm = float(context.plate["nib_roles_mm"][title_nib_role])
    for candidate in candidates:
        if (
            candidate
            and not _PROTECTED_BRANDING.search(candidate)
            and _title_fits(
                candidate,
                title_zone,
                preferred,
                minimum,
                condense_nib_mm=title_nib_mm,
            )
        ):
            return candidate, full
    # Prefer a naturally fitting or honestly wrapped alternative (for example
    # RED BULL RING instead of condensing SPIELBERG).  Only when every ordinary
    # candidate fails may an indivisible source-faithful word use the bounded
    # one-line condensation supported by the compositor.
    for candidate in candidates:
        if (
            candidate
            and len(candidate.split()) == 1
            and not _PROTECTED_BRANDING.search(candidate)
            and _title_fits(
                candidate,
                title_zone,
                preferred,
                minimum,
                condense_nib_mm=title_nib_mm,
                allow_single_word_condense=True,
            )
        ):
            return candidate, full
    raise MapPlotterError(
        f"Circuit title {full!r} has no neutral form that fits {context.format_id}."
    )


def _copy_fits(
    text: str,
    zone: Rect,
    minimum_cap_mm: float,
    *,
    allow_horizontal_condense: bool = False,
    condense_pen_id: str = "black-0-25",
    preferred_cap_mm: float | None = None,
) -> bool:
    if allow_horizontal_condense:
        # Use the same stroke compositor as final furniture.  Its physical
        # gate includes the actual preferred cap, two-nib edge reserve,
        # minimum 0.65 horizontal scale, and glyph path-length reinforcement;
        # a width-only approximation can approve copy that later fails.
        try:
            text_strokes_fit(
                text,
                x_mm=0.0,
                y_mm=0.0,
                preferred_cap_mm=(preferred_cap_mm or minimum_cap_mm),
                maximum_width_mm=zone.width,
                pen_id=condense_pen_id,
                anchor="middle",
                minimum_cap_mm=minimum_cap_mm,
                allow_horizontal_condense=True,
            )
        except MapPlotterError:
            return False
        return True
    width = text_width_mm(text, cap_height_mm=minimum_cap_mm)
    return width <= zone.width + 1e-9


def _compact_line(
    candidates: Sequence[str],
    zone: Rect,
    minimum_cap_mm: float,
    *,
    allow_horizontal_condense: bool = False,
    condense_pen_id: str = "black-0-25",
    preferred_cap_mm: float | None = None,
) -> str:
    for candidate in candidates:
        normalized = " ".join(candidate.upper().split())
        if normalized and _copy_fits(
            normalized,
            zone,
            minimum_cap_mm,
            allow_horizontal_condense=allow_horizontal_condense,
            condense_pen_id=condense_pen_id,
            preferred_cap_mm=preferred_cap_mm,
        ):
            return normalized
    raise MapPlotterError("Circuit furniture copy cannot fit its binding format zone.")


def _event_source_registry(
    catalog: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if catalog is None:
        return {}
    return {str(source["id"]): copy.deepcopy(source) for source in catalog["sources"]}


def _source_ref_for_geometry(event: Mapping[str, Any]) -> str:
    geometry = event["circuit"]["geometry"]
    for key in ("source_ref", "snapshot_ref"):
        value = geometry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    refs = sorted(_find_source_refs(geometry.get("model", {})))
    if refs:
        return refs[0]
    _fail(f"event {event['id']!r} geometry has no source reference.")


def _track_boundary_qualification(
    boundary: BaseGeometry,
    lap: LineString,
    transform_value: _PaperTransform,
    nib_mm: float,
) -> dict[str, Any]:
    """Qualify one claimed edge against the sourced lap before drawing it.

    An OSM ``highway=raceway`` area near a venue is not necessarily the Grand
    Prix surface.  Area geometry therefore has to contain at least 95% of the
    selected lap before its rings can be called track edges.  Individual edge
    linework is accepted only when it follows the lap at a stable paper-scale
    distance.  Both forms must remain separated by three physical Grey nibs.
    """

    is_area = isinstance(boundary, (Polygon, MultiPolygon))
    linework = boundary.boundary if is_area else boundary
    boundary_samples: list[float] = []
    for line in _line_parts(linework):
        if line.length <= 0.0:
            continue
        boundary_samples.extend(
            line.interpolate(line.length * fraction).distance(lap)
            for fraction in (0.1, 0.25, 0.5, 0.75, 0.9)
        )
    positive = sorted(distance for distance in boundary_samples if distance > 1e-6)
    clearance_mm = (
        positive[len(positive) // 2] * transform_value.scale_mm_per_m
        if positive
        else None
    )

    lap_coverage_fraction: float | None = None
    lap_distance_median_mm: float | None = None
    boundary_to_lap_length_ratio: float | None = None
    if is_area:
        lap_coverage_fraction = (
            float(lap.intersection(boundary).length / lap.length)
            if lap.length > 0.0
            else 0.0
        )
        lap_associated = lap_coverage_fraction + 1e-9 >= TRACK_AREA_MINIMUM_LAP_COVERAGE
        association_policy = "source-area-covers-selected-lap-v1"
    else:
        lap_distances = sorted(
            lap.interpolate(lap.length * fraction).distance(linework)
            * transform_value.scale_mm_per_m
            for fraction in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9)
        )
        lap_distance_median_mm = lap_distances[len(lap_distances) // 2]
        boundary_to_lap_length_ratio = (
            float(linework.length / lap.length) if lap.length > 0.0 else None
        )
        lap_associated = bool(
            boundary_to_lap_length_ratio is not None
            and 0.40 <= boundary_to_lap_length_ratio <= 2.50
            and lap_distance_median_mm <= 32.0 * nib_mm + 1e-9
        )
        association_policy = "source-line-follows-selected-lap-v1"

    resolvable = bool(
        lap_associated
        and clearance_mm is not None
        and clearance_mm + 1e-9 >= 3.0 * nib_mm
    )
    if not lap_associated:
        reason = "source-raceway-geometry-not-associated-with-selected-lap"
    elif clearance_mm is None:
        reason = "source-edge-coincident-or-distance-unmeasurable"
    elif not resolvable:
        reason = "source-edge-unresolvable-at-paper-scale"
    else:
        reason = None
    return {
        "geometry_kind": "source-area" if is_area else "source-linework",
        "association_policy": association_policy,
        "lap_coverage_fraction": (
            round(lap_coverage_fraction, 9)
            if lap_coverage_fraction is not None
            else None
        ),
        "lap_distance_median_mm": (
            round(lap_distance_median_mm, 6)
            if lap_distance_median_mm is not None
            else None
        ),
        "boundary_to_lap_length_ratio": (
            round(boundary_to_lap_length_ratio, 6)
            if boundary_to_lap_length_ratio is not None
            else None
        ),
        "representative_clearance_mm": (
            round(clearance_mm, 6) if clearance_mm is not None else None
        ),
        "required_clearance_mm": round(3.0 * nib_mm, 6),
        "lap_associated": lap_associated,
        "resolvable": resolvable,
        "reason": reason,
    }


def _track_boundary_resolvable(
    boundaries: Sequence[BaseGeometry],
    lap: LineString,
    transform_value: _PaperTransform,
    nib_mm: float,
) -> tuple[bool, float | None]:
    """Compatibility summary for callers that need an all-boundaries result."""

    if not boundaries:
        return (False, None)
    qualifications = [
        _track_boundary_qualification(boundary, lap, transform_value, nib_mm)
        for boundary in boundaries
    ]
    clearances = [
        float(value["representative_clearance_mm"])
        for value in qualifications
        if value["representative_clearance_mm"] is not None
    ]
    return (
        all(bool(value["resolvable"]) for value in qualifications),
        min(clearances) if clearances else None,
    )


def _source_lineage_count(values: Iterable[Mapping[str, Any]]) -> int:
    return sum(
        len(value.get("source_objects", []))
        for value in values
        if isinstance(value.get("source_objects"), list)
    )


def _layer_pen_up_mm(records: Sequence[StrokeRecord], *, start: PointTuple) -> float:
    total = 0.0
    current = start
    for record in records:
        first = record.points[0]
        last = record.points[-1]
        total += math.hypot(first[0] - current[0], first[1] - current[1])
        current = last
    return total


def _optimise_layer_travel(
    layer: ArtworkLayer, *, start: PointTuple
) -> dict[str, float | int]:
    """Nearest-neighbour order one logical layer for physical pen travel."""

    before = _layer_pen_up_mm(layer.records, start=start)
    if len(layer.records) < 3:
        return {
            "record_count": len(layer.records),
            "pen_up_before_mm": round(before, 6),
            "pen_up_after_mm": round(before, 6),
        }
    pending = list(enumerate(layer.records))
    ordered: list[StrokeRecord] = []
    current = start
    while pending:
        ranked: list[tuple[float, int, bool, int, StrokeRecord]] = []
        for pending_index, (original_index, record) in enumerate(pending):
            first = record.points[0]
            last = record.points[-1]
            forward = math.hypot(first[0] - current[0], first[1] - current[1])
            ranked.append((forward, original_index, False, pending_index, record))
            reversible = (
                record.vector_path is None
                and record.role not in {"lap-centreline", "pit-lane"}
                and record.sequence is None
            )
            if reversible:
                reverse = math.hypot(last[0] - current[0], last[1] - current[1])
                ranked.append((reverse, original_index, True, pending_index, record))
        _distance, original_index, reverse, pending_index, record = min(ranked)
        pending.pop(pending_index)
        if reverse:
            record.points = list(reversed(record.points))
        record.attributes.setdefault("data-source-record-index", str(original_index))
        record.attributes.setdefault("data-plot-order-optimised", "true")
        ordered.append(record)
        current = record.points[-1]
    layer.records = ordered
    after = _layer_pen_up_mm(layer.records, start=start)
    return {
        "record_count": len(layer.records),
        "pen_up_before_mm": round(before, 6),
        "pen_up_after_mm": round(after, 6),
    }


def _prune_context_to_density_budget(
    layers: Mapping[str, ArtworkLayer],
    selected_context: Sequence[Mapping[str, Any]],
    placements: Sequence[_LabelPlacement],
    omissions: list[dict[str, Any]],
    *,
    maximum_baseline_length_mm: float,
    hard_maximum_baseline_length_mm: float,
    sheet: str,
    protected_feature_ids: set[str],
) -> tuple[dict[str, Any], set[str]]:
    """Cull only discretionary source context until the physical budget fits.

    The Red course, stations, labels, pit geometry, and every labelled context
    feature are immutable. Decorative infill is removed first; if more
    headroom is needed, whole low-priority unlabelled source-feature groups are
    removed. A qualified Grey track-boundary group is optional and may be
    removed only all-at-once, as the final hard-gate fallback, when that single
    omission is sufficient. This prevents dense circuits from silently
    exceeding the plotter gate after the v2 multi-pass course is added.
    """

    context_layer_ids = (
        "circuit_context_grey",
        "circuit_context_green",
        "circuit_context_blue",
        "circuit_venue_purple",
    )
    context_layers = [
        layers[layer_id] for layer_id in context_layer_ids if layer_id in layers
    ]

    def record_length(record: StrokeRecord) -> float:
        return _serialized_polyline_length_mm(record.points)

    def total_length() -> float:
        return sum(
            record_length(record)
            for artwork_layer in layers.values()
            for record in artwork_layer.records
        )

    initial_length = total_length()
    current_length = initial_length
    decisions: list[dict[str, Any]] = []

    def remove_records(
        records: Sequence[StrokeRecord],
        *,
        reason: str,
        feature_id: str,
        kind: str,
        role: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> float:
        nonlocal current_length
        identities = {id(record) for record in records}
        removed_length = sum(record_length(record) for record in records)
        if not identities or removed_length <= 0.0:
            return 0.0
        for artwork_layer in context_layers:
            artwork_layer.records = [
                record
                for record in artwork_layer.records
                if id(record) not in identities
            ]
        current_length -= removed_length
        decision = {
            "feature_id": feature_id,
            "kind": kind,
            "reason": reason,
            "removed_length_mm": round(removed_length, 6),
            "whole_feature_group": role is None,
        }
        if role is not None:
            decision["role"] = role
        if evidence:
            decision.update(copy.deepcopy(dict(evidence)))
        decisions.append(decision)
        omissions.append(copy.deepcopy(decision))
        return removed_length

    feature_map = {
        str(feature["id"]): feature for feature in selected_context if feature.get("id")
    }
    labelled_feature_ids = {
        str(placement.feature_id)
        for placement in placements
        if placement.role == "context-label" and placement.feature_id
    }

    # Remove expendable texture before source boundaries.  Blue water dots are
    # deliberately excluded: stipple is the requested semantic treatment for
    # retained water polygons, so water can leave only as a complete source
    # feature under the separate hard-gate fallback below.
    decorative_roles = {
        "runoff-hatch",
        "gravel-stipple-dot",
    }
    decorative_groups: dict[tuple[str, str], list[StrokeRecord]] = {}
    for artwork_layer in context_layers:
        for record in artwork_layer.records:
            feature_id = str(record.attributes.get("data-feature-id") or "")
            if feature_id in feature_map and record.role in decorative_roles:
                decorative_groups.setdefault((feature_id, record.role), []).append(
                    record
                )
    for (feature_id, role), records in sorted(
        decorative_groups.items(),
        key=lambda item: (-sum(record_length(value) for value in item[1]), item[0]),
    ):
        if current_length <= maximum_baseline_length_mm + 1e-9:
            break
        kind = str(feature_map[feature_id]["_render_kind"])
        remove_records(
            records,
            reason="paper-field-density-budget-decoration",
            feature_id=feature_id,
            kind=kind,
            role=role,
        )

    removable_kind_order = {
        "building": 0,
        "road": 1,
        "access-road": 2,
        "grass": 3,
        "woodland": 4,
        "runoff": 5,
        "gravel-trap": 6,
        "paddock": 7,
        "garage": 8,
        "pit-building": 9,
        "grandstand": 10,
        "principal-building": 11,
    }
    minimum_remaining_by_sheet = {
        "A5": {
            "water": 2,
            "road": 2,
            "access-road": 1,
            "grass": 1,
            "woodland": 1,
            "runoff": 2,
            "gravel-trap": 2,
            "paddock": 1,
            "garage": 1,
            "pit-building": 1,
            "grandstand": 2,
            "principal-building": 1,
        },
        "A4": {
            "water": 4,
            "road": 8,
            "access-road": 5,
            "building": 4,
            "grass": 3,
            "woodland": 3,
            "runoff": 4,
            "gravel-trap": 3,
            "paddock": 2,
            "garage": 3,
            "pit-building": 2,
            "grandstand": 5,
            "principal-building": 3,
        },
        "A3": {
            "water": 8,
            "road": 20,
            "access-road": 10,
            "building": 12,
            "grass": 4,
            "woodland": 4,
            "runoff": 8,
            "gravel-trap": 6,
            "paddock": 3,
            "garage": 6,
            "pit-building": 4,
            "grandstand": 10,
            "principal-building": 6,
        },
    }
    minimum_remaining = minimum_remaining_by_sheet[sheet]
    remaining_counts: dict[str, int] = {}
    for feature in selected_context:
        kind = str(feature["_render_kind"])
        remaining_counts[kind] = remaining_counts.get(kind, 0) + 1

    feature_records: dict[str, list[StrokeRecord]] = {}
    for artwork_layer in context_layers:
        for record in artwork_layer.records:
            feature_id = str(record.attributes.get("data-feature-id") or "")
            if feature_id in feature_map:
                feature_records.setdefault(feature_id, []).append(record)
    feature_candidates = [
        (
            feature_id,
            str(feature_map[feature_id]["_render_kind"]),
            records,
        )
        for feature_id, records in feature_records.items()
        if feature_id not in labelled_feature_ids
        and feature_id not in protected_feature_ids
        and str(feature_map[feature_id]["_render_kind"]) in removable_kind_order
    ]

    def removal_rank(
        value: tuple[str, str, list[StrokeRecord]],
    ) -> tuple[int, int, float, str]:
        feature_id, kind, records = value
        feature = feature_map[feature_id]
        length = sum(record_length(record) for record in records)
        if kind == "building":
            # Urban grain is conveyed by a few substantial footprints, not by
            # the smallest numbered sheds surviving merely because their raw
            # OSM ``name`` tag is non-empty. Remove weak/small footprints first
            # and retain drawable named landmarks plus larger anonymous massing.
            return (
                1 if _meaningful_context_name(feature) else 0,
                removable_kind_order[kind],
                length,
                feature_id,
            )
        return (
            1 if feature.get("name") else 0,
            removable_kind_order[kind],
            -length,
            feature_id,
        )

    feature_candidates.sort(key=removal_rank)
    culled_feature_ids: set[str] = set()
    for feature_id, kind, records in feature_candidates:
        if current_length <= maximum_baseline_length_mm + 1e-9:
            break
        if remaining_counts.get(kind, 0) <= minimum_remaining.get(kind, 0):
            continue
        if remove_records(
            records,
            reason="paper-field-density-budget-source-feature",
            feature_id=feature_id,
            kind=kind,
        ):
            remaining_counts[kind] -= 1
            culled_feature_ids.add(feature_id)

    # Water is visually important and therefore remains outside the ordinary
    # 0.17 design-target pruning order.  If all weaker context has been
    # exhausted and the sheet would otherwise breach the immutable 0.18 hard
    # gate, remove only the smallest unlabelled anonymous water footprints,
    # preserving named and large water plus a sheet-scaled minimum inventory.
    water_candidates = [
        (feature_id, records)
        for feature_id, records in feature_records.items()
        if str(feature_map[feature_id]["_render_kind"]) == "water"
        and feature_id not in labelled_feature_ids
        and feature_id not in protected_feature_ids
    ]
    water_candidates.sort(
        key=lambda value: (
            1 if _meaningful_context_name(feature_map[value[0]]) else 0,
            sum(record_length(record) for record in value[1]),
            value[0],
        )
    )
    for feature_id, records in water_candidates:
        if current_length <= hard_maximum_baseline_length_mm + 1e-9:
            break
        if remaining_counts.get("water", 0) <= minimum_remaining.get("water", 0):
            continue
        if remove_records(
            records,
            reason="paper-field-density-budget-source-feature",
            feature_id=feature_id,
            kind="water",
        ):
            remaining_counts["water"] -= 1
            culled_feature_ids.add(feature_id)

    # A qualified Grey track edge is optional supporting context; the exact
    # Red source centreline and its explicit diagrammatic corridor remain the
    # hero.  On a small sheet, a complete circuit-scale edge can itself add a
    # second lap-sized ink load. If (and only if) removing the entire qualified
    # boundary group is sufficient to clear the immutable 0.18 gate, omit the
    # whole group and ledger every affected source index. Never retain a
    # misleading partial edge and never remove Red geometry.
    boundary_records = [
        record
        for artwork_layer in context_layers
        for record in artwork_layer.records
        if record.role == "track-boundary"
    ]
    boundary_length = sum(record_length(record) for record in boundary_records)

    # Maximum-safe course fitting makes the course larger than the previous
    # percentage-padded presentation.  On an unusually dense venue, the
    # sheet's ordinary per-kind context reserve can therefore remain a few
    # strokes above the immutable 0.18 ceiling.  Before holding the course,
    # deterministically remove further whole, unlabelled source features while
    # retaining at least one emitted representative of each available kind.
    # Stop as soon as the complete optional track-boundary group becomes the
    # smaller sufficient omission, so the existing all-or-none boundary rule
    # remains authoritative.
    emergency_kind_order = {
        **removable_kind_order,
        "water": len(removable_kind_order),
    }
    emergency_candidates = [
        (
            feature_id,
            str(feature_map[feature_id]["_render_kind"]),
            records,
        )
        for feature_id, records in feature_records.items()
        if feature_id not in labelled_feature_ids
        and feature_id not in protected_feature_ids
        and feature_id not in culled_feature_ids
        and str(feature_map[feature_id]["_render_kind"])
        in emergency_kind_order
    ]

    def emergency_removal_rank(
        value: tuple[str, str, list[StrokeRecord]],
    ) -> tuple[int, int, float, str]:
        feature_id, kind, records = value
        feature = feature_map[feature_id]
        length = sum(record_length(record) for record in records)
        return (
            emergency_kind_order[kind],
            1 if _meaningful_context_name(feature) else 0,
            -length,
            feature_id,
        )

    emergency_candidates.sort(key=emergency_removal_rank)
    for feature_id, kind, records in emergency_candidates:
        if current_length <= hard_maximum_baseline_length_mm + 1e-9:
            break
        if (
            boundary_records
            and current_length - boundary_length
            <= hard_maximum_baseline_length_mm + 1e-9
        ):
            break
        if remaining_counts.get(kind, 0) <= 1:
            continue
        if remove_records(
            records,
            reason="paper-field-density-budget-source-feature",
            feature_id=feature_id,
            kind=kind,
            evidence={
                "density_stage": "hard-ceiling-below-presentation-reserve",
                "maximum_course_fit_retained": True,
            },
        ):
            remaining_counts[kind] -= 1
            culled_feature_ids.add(feature_id)

    if (
        current_length > hard_maximum_baseline_length_mm + 1e-9
        and boundary_records
        and current_length - boundary_length <= hard_maximum_baseline_length_mm + 1e-9
    ):
        boundary_indices = sorted(
            {
                int(value)
                for record in boundary_records
                if (value := record.attributes.get("data-boundary-index")) is not None
            }
        )
        remove_records(
            boundary_records,
            reason="paper-field-density-budget-qualified-track-boundary-group",
            feature_id="qualified-track-boundary-group",
            kind="track-boundary",
            evidence={
                "boundary_indices": boundary_indices,
                "all_or_nothing_group": True,
                "red_course_geometry_retained": True,
            },
        )

    if current_length > hard_maximum_baseline_length_mm + 1e-9:
        raise MapPlotterError(
            f"The {sheet} circuit field has {current_length:.3f} mm of mandatory "
            f"ink before vegetation outlines, above its "
            f"{hard_maximum_baseline_length_mm:.3f} mm hard density budget; "
            "artifact held."
        )
    return (
        {
            "policy": (
                "decoration-then-whole-unlabelled-source-feature-"
                "hard-water-boundary-fallback-v2"
            ),
            "initial_baseline_length_mm": round(initial_length, 6),
            "maximum_baseline_length_mm": round(maximum_baseline_length_mm, 6),
            "hard_maximum_baseline_length_mm": round(
                hard_maximum_baseline_length_mm, 6
            ),
            "retained_baseline_length_mm": round(current_length, 6),
            "target_overage_mm": round(
                max(0.0, current_length - maximum_baseline_length_mm), 6
            ),
            "removed_length_mm": round(initial_length - current_length, 6),
            "decisions": decisions,
            "labelled_context_features_protected": True,
            "course_and_station_geometry_protected": True,
            "whole_source_feature_groups_only_after_decoration": True,
        },
        culled_feature_ids,
    )


def build_f1_plate(
    event: Any,
    format_id: str = "a4-landscape",
    *,
    catalog: Mapping[str, Any] | None = None,
    context_mode: str | None = None,
) -> PlateArtwork:
    """Build one north-up, source-qualified Atlas plate.

    ``format_id`` is deliberately included in the artwork variant identity, so
    rendering one event in multiple canonical formats cannot overwrite files.
    The function returns an in-memory :class:`PlateArtwork`; callers use the
    existing :func:`city_map_plotter.niche_common.write_plate` output engine.
    """

    if format_id not in FORMAT_IDS:
        raise MapPlotterError(
            f"Unknown circuit plate format {format_id!r}; choose "
            + ", ".join(FORMAT_IDS)
            + "."
        )
    checked_catalog = validate_f1_catalog(catalog) if catalog is not None else None
    registry = _event_source_registry(checked_catalog)
    catalog_class = (
        str(checked_catalog.get("catalog_class", "current-season-calendar"))
        if checked_catalog
        else "current-season-calendar"
    )
    motorsport_study = catalog_class == "motorsport-circuit-studies"
    catalog_release_season = int(checked_catalog["season"]) if checked_catalog else 2026
    requested_event = _object(event, "event")
    event_reference_season = (
        _integer(
            requested_event.get("configuration_reference_season"),
            "event.configuration_reference_season",
            minimum=1950,
        )
        if catalog_class == "legacy-f1-configurations"
        else catalog_release_season
    )
    checked = validate_f1_event(
        event,
        source_registry=registry if registry else None,
        season=event_reference_season,
    )
    geometry_record = checked["circuit"]["geometry"]
    if not isinstance(geometry_record.get("model"), dict):
        raise MapPlotterError(
            f"Event {checked['id']!r} has no normalized sourced geometry model; "
            "no circuit plate was built."
        )
    model = geometry_record["model"]
    geometry_status = str(geometry_record.get("status", FULL_GEOMETRY_STATUS))
    centreline_only = geometry_status == CENTRELINE_GEOMETRY_STATUS
    geometry_digest = _geometry_sha256(model)
    context = context_for(format_id)
    sheet = str(context.plate["sheet"])
    gate = copy.deepcopy(PAPER_ADAPTATION[sheet])
    mode = _context_mode(checked, context_mode)
    pens = _pen_roles(format_id)
    season = event_reference_season

    lap_source = _geojson_line(model["lap"], "circuit lap")
    pit_sources = [
        _geojson_line(lane["geometry"], f"pit lane {index}")
        for index, lane in enumerate(model.get("pit_lanes", []))
    ]
    boundary_sources = [
        _geojson_geometry(boundary, f"track boundary {index}")
        for index, boundary in enumerate(model.get("track_boundaries", []))
    ]
    # Framing is controlled only by topology that belongs to the selected
    # course.  Raw raceway areas are qualified later and may describe a nearby
    # or enclosing course (for example Nürburgring relation 19275020); allowing
    # one into the source bounds would shrink the selected GP lap before that
    # same boundary is correctly rejected.
    structural: list[BaseGeometry] = [lap_source, *pit_sources]
    structural_bounds = unary_union(structural).bounds
    source_bounds = (
        float(structural_bounds[0]),
        float(structural_bounds[1]),
        float(structural_bounds[2]),
        float(structural_bounds[3]),
    )
    working_rect = context.field.inset(float(gate["field_padding_mm"]))
    transform_value = _paper_transform(
        source_bounds,
        working_rect,
    )
    context_viewport_bounds = _source_viewport_for_paper_rect(
        transform_value,
        context.field,
    )
    viewport = box(*context_viewport_bounds)
    selected_context, omissions, input_context_counts = _select_context(
        model.get("context", []),
        season=season,
        gate=gate,
        mode=mode,
        viewport=viewport,
    )
    lap_page = transform_value.geometry(lap_source)
    if not isinstance(lap_page, LineString):
        raise MapPlotterError("Normalized lap did not remain one LineString on paper.")
    pit_page = [transform_value.geometry(line) for line in pit_sources]

    layers: dict[str, ArtworkLayer] = {}

    def layer(layer_id: str, label: str, pen_id: str) -> ArtworkLayer:
        existing = layers.get(layer_id)
        if existing is None:
            existing = ArtworkLayer(layer_id, label, pen_id)
            layers[layer_id] = existing
        elif existing.pen_id != pen_id:
            raise MapPlotterError(f"Circuit layer {layer_id!r} changed physical pen.")
        return existing

    grey = layer(
        "circuit_context_grey", "Track boundaries and grey context", pens["grey"]
    )
    green = layer("circuit_context_green", "Grass and woodland", pens["green"])
    blue = layer("circuit_context_blue", "Water and stipple", pens["blue"])
    purple = layer(
        "circuit_venue_purple", "Pit lane and principal venue", pens["purple"]
    )
    lap_layer = layer(
        "lap_centreline",
        "Exact lap centreline and diagrammatic course corridor",
        pens["lap"],
    )
    copy_layer = layer(
        "circuit_copy", "Circuit stations and factual furniture", pens["copy"]
    )

    lap_source_ref = _source_ref_for_geometry(checked)
    lap_source_object_id = _source_object_attribute(
        model["lap_source_objects"], lap_source_ref
    )
    lap_source_digest = _lap_source_sha256(model["lap"])
    corridor_plan = _diagrammatic_corridor_plan(
        context,
        nib_mm=lap_layer.pen.mark_width_mm,
    )
    lap_layer.add(
        [(float(x), float(y)) for x, y, *_rest in lap_page.coords],
        source_ref=lap_source_ref,
        role="lap-centreline",
        attributes={
            "data-claim": "LAP CENTRELINE",
            "data-racing-line": "false",
            "data-source-object-count": str(len(model["lap_source_objects"])),
            "data-source-object-id": lap_source_object_id,
            "data-closed-loop": "true",
            "data-geometry-sha256": geometry_digest,
            "data-source-geometry-sha256": geometry_digest,
            "data-source-lap-sha256": lap_source_digest,
            "data-source-coordinate-count": str(len(lap_source.coords)),
            "data-projected-coordinate-count": str(len(lap_page.coords)),
            "data-centreline-parity": "exact-projected-source-coordinate-order",
        },
    )
    corridor_metadata = _emit_diagrammatic_corridor_offsets(
        lap_layer,
        lap_page,
        plan=corridor_plan,
        source_ref=lap_source_ref,
        source_object_id=lap_source_object_id,
        geometry_sha256=geometry_digest,
        lap_source_sha256=lap_source_digest,
        scale_mm_per_m=transform_value.scale_mm_per_m,
    )
    corridor_label_clearance_mask: BaseGeometry = unary_union(
        [box(*zone["mask_bounds_mm"]) for zone in corridor_metadata["clearance_zones"]]
    )

    # Pit lane is visibly subordinate and never shares the red hero pen.
    for index, (lane_record, page_geometry) in enumerate(
        zip(model.get("pit_lanes", []), pit_page, strict=True)
    ):
        source_ref = str(lane_record.get("source_ref", lap_source_ref))
        _emit_linework(
            purple,
            page_geometry,
            source_ref=source_ref,
            role="pit-lane",
            attributes={
                "data-pit-lane-index": str(index),
                "data-source-object-count": str(len(lane_record["source_objects"])),
                "data-source-object-id": _source_object_attribute(
                    lane_record["source_objects"], source_ref
                ),
            },
            omissions=omissions,
            feature_id=f"pit-lane-{index}",
        )

    # Start/finish double gate: both lines are source-anchored and perpendicular
    # to the local lap tangent.  It is Black, leaving Red exclusive to the lap.
    sf = model.get("start_finish")
    sf_point: PointTuple | None = None
    normal: PointTuple | None = None
    gate_geometries: list[LineString] = []
    if isinstance(sf, dict):
        sf_point = transform_value.point(sf["point"])
        sf_chainage = _nearest_distance(lap_page, sf_point)
        tangent = _line_tangent(lap_page, sf_chainage)
        normal = (-tangent[1], tangent[0])
        gate_half = max(1.8, 2.0 * copy_layer.pen.mark_width_mm)
        gate_offset = max(0.65, 2.0 * copy_layer.pen.mark_width_mm)
        for signed in (-0.5, 0.5):
            centre = (
                sf_point[0] + signed * gate_offset * tangent[0],
                sf_point[1] + signed * gate_offset * tangent[1],
            )
            gate_line = [
                (
                    centre[0] - gate_half * normal[0],
                    centre[1] - gate_half * normal[1],
                ),
                (
                    centre[0] + gate_half * normal[0],
                    centre[1] + gate_half * normal[1],
                ),
            ]
            copy_layer.add(
                gate_line,
                source_ref=str(sf["source_ref"]),
                role="start-finish",
                attributes={
                    "data-gate": "double",
                    "data-white-ink": "false",
                    "data-source-object-id": _source_object_attribute(
                        sf.get("source_objects") or [], str(sf["source_ref"])
                    ),
                },
            )
            gate_geometries.append(LineString(gate_line))

    # Direction arrows are source-derived Black chevrons, never extra red ink.
    direction_geometries: list[LineString] = []
    lap_centroid = lap_page.centroid
    direction_value = str(
        checked["circuit"].get("lap_direction")
        or checked["circuit"].get("direction")
        or "withheld"
    ).casefold()
    direction_source_ref = checked["circuit"].get("lap_direction_source_ref")
    direction_is_sourced = (
        direction_value in {"clockwise", "counter-clockwise"}
        and isinstance(direction_source_ref, str)
        and bool(direction_source_ref.strip())
    )
    arrow_count = int(gate["direction_arrow_count"]) if direction_is_sourced else 0
    for index, fraction in enumerate((0.19, 0.52, 0.82)[:arrow_count], start=1):
        distance = lap_page.length * fraction
        point_value = lap_page.interpolate(distance)
        arrow_tangent = _line_tangent(lap_page, distance)
        arrow_normal = (-arrow_tangent[1], arrow_tangent[0])
        outward = (
            point_value.x - lap_centroid.x,
            point_value.y - lap_centroid.y,
        )
        if outward[0] * arrow_normal[0] + outward[1] * arrow_normal[1] < 0:
            arrow_normal = (-arrow_normal[0], -arrow_normal[1])
        offset = max(1.2, 3.0 * copy_layer.pen.mark_width_mm)
        tip = (
            point_value.x + offset * arrow_normal[0] + 0.8 * arrow_tangent[0],
            point_value.y + offset * arrow_normal[1] + 0.8 * arrow_tangent[1],
        )
        head = max(1.25, 3.2 * copy_layer.pen.mark_width_mm)
        left = (
            tip[0] - head * arrow_tangent[0] + 0.55 * head * arrow_normal[0],
            tip[1] - head * arrow_tangent[1] + 0.55 * head * arrow_normal[1],
        )
        right = (
            tip[0] - head * arrow_tangent[0] - 0.55 * head * arrow_normal[0],
            tip[1] - head * arrow_tangent[1] - 0.55 * head * arrow_normal[1],
        )
        arrow = [left, tip, right]
        copy_layer.add(
            arrow,
            source_ref=str(direction_source_ref),
            role="operational-overlay",
            sequence=index,
            attributes={
                "data-operational-kind": "lap-direction-arrow",
                "data-red-ink": "false",
                "data-chainage-fraction": f"{fraction:.2f}",
                "data-derivation": "station-on-lap",
                "data-lap-source-ref": lap_source_ref,
                "data-source-object-id": _source_object_attribute(
                    model["lap_source_objects"], lap_source_ref
                ),
            },
        )
        direction_geometries.append(LineString(arrow))

    # Place the north mark in whichever field corner is farthest from the lap.
    north_width = max(6.0, 4.0 * copy_layer.pen.mark_width_mm)
    north_height = max(10.0, 7.0 * copy_layer.pen.mark_width_mm)
    corner_inset = max(1.0, 3.0 * copy_layer.pen.mark_width_mm)
    north_candidates = [
        Rect(
            context.field.left + corner_inset,
            context.field.top + corner_inset,
            north_width,
            north_height,
        ),
        Rect(
            context.field.right - corner_inset - north_width,
            context.field.top + corner_inset,
            north_width,
            north_height,
        ),
        Rect(
            context.field.left + corner_inset,
            context.field.bottom - corner_inset - north_height,
            north_width,
            north_height,
        ),
        Rect(
            context.field.right - corner_inset - north_width,
            context.field.bottom - corner_inset - north_height,
            north_width,
            north_height,
        ),
    ]
    north_box = max(
        north_candidates,
        key=lambda candidate: _rect_polygon(candidate).distance(lap_page),
    )

    protected_parts: list[BaseGeometry] = [
        lap_page.buffer(float(gate["label_track_clearance_mm"]), cap_style="round"),
        *[geometry.buffer(0.5, cap_style="round") for geometry in pit_page],
        *[geometry.buffer(0.25, cap_style="round") for geometry in gate_geometries],
        *[
            geometry.buffer(0.25, cap_style="round")
            for geometry in direction_geometries
        ],
    ]
    protected_ink = unary_union(protected_parts)
    boxes: list[Rect] = [north_box]
    leaders: list[LineString] = []
    placements: list[_LabelPlacement] = []
    label_cap = max(8.0 * copy_layer.pen.mark_width_mm, 2.0)
    turn_stations = model.get("turn_stations", [])
    name_ids = _even_name_ids(turn_stations, gate["turn_name_limit"])
    name_omissions: list[dict[str, Any]] = [
        {
            "station_id": station["id"],
            "name": station["name"],
            "reason": "paper-turn-name-count-gate-number-retained",
        }
        for station in turn_stations
        if station.get("name") and str(station["id"]) not in name_ids
    ]

    # Try S/F copy first, but the sourced double gate remains even if a compact
    # sheet has no collision-free text position.
    if isinstance(sf, dict) and sf_point is not None and normal is not None:
        sf_placement = _place_station_label(
            label_id="start-finish-label",
            copies=(("S/F", False),),
            anchor=sf_point,
            preferred=normal,
            source_ref=str(sf["source_ref"]),
            role="start-finish",
            cap_mm=label_cap,
            label_pen_nib_mm=copy_layer.pen.mark_width_mm,
            field=context.field.inset(0.5),
            protected_ink=protected_ink,
            boxes=boxes,
            leaders=leaders,
            separation_mm=float(gate["label_separation_mm"]),
            track_clearance_mm=float(gate["label_track_clearance_mm"]),
            feature_id="start-finish",
            source_object_id=_source_object_attribute(
                sf.get("source_objects", []), str(sf["source_ref"])
            ),
        )
        if sf_placement is not None:
            placements.append(sf_placement)
            boxes.append(sf_placement.bounds)
            assert sf_placement.leader is not None
            leaders.append(LineString(sf_placement.leader))
        else:
            omissions.append(
                {
                    "feature_id": "start-finish-label",
                    "reason": "no-collision-free-label-position",
                }
            )

    # Label assignment is a constrained layout problem rather than a safe
    # one-pass greedy operation.  A deterministic minimum-remaining-values
    # search chooses the currently most constrained station and backtracks over
    # alternate candidate positions.  Commit only a complete solution: every
    # station number is retained, even on a compact street-circuit plate.
    station_layout = [
        (station, transform_value.point(station["point"])) for station in turn_stations
    ]

    def station_congestion(
        item: tuple[Mapping[str, Any], PointTuple],
    ) -> tuple[float, float, int]:
        station, station_point = item
        distances = sorted(
            math.hypot(
                station_point[0] - other_point[0],
                station_point[1] - other_point[1],
            )
            for other, other_point in station_layout
            if other["id"] != station["id"]
        )
        local_clearance = sum(distances[:3]) if distances else float("inf")
        edge_clearance = min(
            station_point[0] - context.field.left,
            context.field.right - station_point[0],
            station_point[1] - context.field.top,
            context.field.bottom - station_point[1],
        )
        return (local_clearance, edge_clearance, int(station["number"]))

    base_boxes = list(boxes)
    base_leaders = list(leaders)
    base_placements = list(placements)
    station_classes = {
        str(station["id"]): _station_class(station) for station in turn_stations
    }
    prepared_stations: list[
        tuple[
            Mapping[str, Any],
            PointTuple,
            PointTuple,
            tuple[tuple[str, bool], ...],
            bool,
        ]
    ] = []
    for station, station_point in station_layout:
        distance = _nearest_distance(lap_page, station_point)
        station_tangent = _line_tangent(lap_page, distance)
        preferred = (-station_tangent[1], station_tangent[0])
        outward = (
            station_point[0] - lap_centroid.x,
            station_point[1] - lap_centroid.y,
        )
        if outward[0] * preferred[0] + outward[1] * preferred[1] < 0:
            preferred = (-preferred[0], -preferred[1])
        station_class = station_classes[str(station["id"])]
        marker_prefix = _station_marker_prefix(station_class)
        number_copy = f"{marker_prefix}{int(station['number']):02d}"
        wants_name = bool(str(station["id"]) in name_ids and station.get("name"))
        station_copies: list[tuple[str, bool]] = []
        if wants_name:
            station_copies.append(
                (f"{number_copy} {str(station['name']).upper()}", True)
            )
        station_copies.append((number_copy, False))
        prepared_stations.append(
            (
                station,
                station_point,
                preferred,
                tuple(station_copies),
                wants_name,
            )
        )

    solved: (
        tuple[
            list[Rect],
            list[LineString],
            list[_LabelPlacement],
            list[dict[str, Any]],
        ]
        | None
    )
    failed_station_numbers: set[int] = set()
    search_nodes = 0
    search_budget = 100_000

    def solve_station_layout(
        remaining: tuple[
            tuple[
                Mapping[str, Any],
                PointTuple,
                PointTuple,
                tuple[tuple[str, bool], ...],
                bool,
            ],
            ...,
        ],
        trial_boxes: list[Rect],
        trial_leaders: list[LineString],
        trial_placements: list[_LabelPlacement],
        trial_name_omissions: list[dict[str, Any]],
    ) -> (
        tuple[
            list[Rect],
            list[LineString],
            list[_LabelPlacement],
            list[dict[str, Any]],
        ]
        | None
    ):
        nonlocal search_nodes
        if not remaining:
            return (
                trial_boxes,
                trial_leaders,
                trial_placements,
                trial_name_omissions,
            )
        if search_nodes >= search_budget:
            return None

        ranked: list[
            tuple[
                int,
                tuple[float, float, int],
                int,
                list[_LabelPlacement],
            ]
        ] = []
        for remaining_index, item in enumerate(remaining):
            station, station_point, preferred, station_copies, _wants_name = item
            candidates = _station_label_candidates(
                label_id=str(station["id"]),
                copies=station_copies,
                anchor=station_point,
                preferred=preferred,
                source_ref=str(station["source_ref"]),
                role="turn-label",
                cap_mm=label_cap,
                label_pen_nib_mm=copy_layer.pen.mark_width_mm,
                field=context.field.inset(0.5),
                protected_ink=protected_ink,
                boxes=trial_boxes,
                leaders=trial_leaders,
                separation_mm=float(gate["label_separation_mm"]),
                track_clearance_mm=float(gate["label_track_clearance_mm"]),
                feature_id=str(station["id"]),
                source_object_id=_source_object_attribute(
                    station.get("source_objects", []),
                    str(station["source_ref"]),
                ),
                per_copy_limit=12,
            )
            if not candidates:
                failed_station_numbers.add(int(station["number"]))
                return None
            ranked.append(
                (
                    len(candidates),
                    station_congestion((station, station_point)),
                    remaining_index,
                    candidates,
                )
            )

        _count, _congestion, chosen_index, chosen_candidates = min(
            ranked,
            key=lambda value: (value[0], value[1], value[2]),
        )
        station, _point, _preferred, _copies, wants_name = remaining[chosen_index]
        next_remaining = remaining[:chosen_index] + remaining[chosen_index + 1 :]
        for placement in chosen_candidates:
            search_nodes += 1
            if search_nodes > search_budget:
                break
            assert placement.leader is not None
            next_omissions = list(trial_name_omissions)
            if wants_name and not placement.displayed_name:
                next_omissions.append(
                    {
                        "station_id": station["id"],
                        "name": station["name"],
                        "reason": "name-collision-number-retained",
                    }
                )
            result = solve_station_layout(
                next_remaining,
                [*trial_boxes, placement.bounds],
                [*trial_leaders, LineString(placement.leader)],
                [*trial_placements, placement],
                next_omissions,
            )
            if result is not None:
                return result
        return None

    solved = solve_station_layout(
        tuple(prepared_stations),
        base_boxes,
        base_leaders,
        base_placements,
        [],
    )
    if solved is None:
        failed = ", ".join(map(str, sorted(failed_station_numbers))) or "unknown"
        raise MapPlotterError(
            f"Event {checked['id']!r} has no complete collision-free {format_id} "
            f"station layout after {search_nodes} deterministic search nodes; "
            f"failed stations included {failed}. No station was dropped."
        )
    boxes, leaders, placements, solved_name_omissions = solved
    name_omissions.extend(solved_name_omissions)

    # Dense venue structures are real source geometry, not a neutral text
    # background.  Reserve their full physical areas for both named course
    # sections and ordinary context copy so labels never cut across stands or
    # building hatching.
    venue_label_exclusion_parts: list[BaseGeometry] = []
    for feature in selected_context:
        if str(feature["_render_kind"]) not in {
            "grandstand",
            "principal-building",
        }:
            continue
        source_geometry = _geojson_geometry(
            feature["geometry"], f"context {feature['id']}"
        ).intersection(viewport)
        if source_geometry.is_empty:
            continue
        venue_label_exclusion_parts.append(
            transform_value.geometry(source_geometry).buffer(
                copy_layer.pen.mark_width_mm / 2.0
            )
        )
    venue_label_protected_ink = unary_union(
        [protected_ink, *venue_label_exclusion_parts]
    )

    # Source-tagged course-section names are the subject-specific annotation
    # layer, so they reserve negative space before ordinary context copy.  The
    # names are explicitly not promoted to official F1 corner nomenclature.
    named_course_sections = [
        section
        for section in model.get("special_sections", [])
        if str(section.get("kind", "")).casefold().replace("_", "-")
        == "named-course-section"
    ]
    named_section_candidates: list[dict[str, Any]] = []
    named_section_copy_omissions: list[dict[str, Any]] = []
    for section in named_course_sections:
        label_copy = _course_section_label_copy(section)
        if label_copy is None:
            named_section_copy_omissions.append(
                {
                    "feature_id": section["id"],
                    "reason": "course-section-name-no-drawable-sourced-copy",
                }
            )
            continue
        source_geometry = _geojson_geometry(
            section["geometry"], f"named course section {section['id']}"
        ).intersection(viewport)
        paper_geometry = transform_value.geometry(source_geometry)
        line_parts = list(_line_parts(paper_geometry))
        if not line_parts:
            named_section_copy_omissions.append(
                {
                    "feature_id": section["id"],
                    "reason": "course-section-outside-viewport",
                }
            )
            continue
        representative_line = max(line_parts, key=lambda value: value.length)
        section_midpoint = representative_line.interpolate(
            representative_line.length / 2.0
        )
        anchor = (float(section_midpoint.x), float(section_midpoint.y))
        alternate_lines = sorted(
            line_parts,
            key=lambda value: (
                value is not representative_line,
                -float(value.length),
                tuple(round(float(item), 6) for item in value.bounds),
            ),
        )
        anchor_options: list[PointTuple] = []
        for line in alternate_lines:
            source_line_points = (
                line.interpolate(line.length / 2.0),
                Point(line.coords[0]),
                Point(line.coords[-1]),
            )
            for option in source_line_points:
                anchor_option = (float(option.x), float(option.y))
                if anchor_option not in anchor_options:
                    anchor_options.append(anchor_option)
        chainage = _nearest_distance(lap_page, anchor)
        named_section_candidates.append(
            {
                "section": section,
                "copy": label_copy,
                "anchor": anchor,
                "anchor_options": tuple(anchor_options),
                "chainage_mm": chainage,
                "paper_length_mm": sum(line.length for line in line_parts),
                "priority": int(section.get("priority", 0)),
            }
        )

    # Repeated OSM way fragments carrying the same case-folded name are one
    # label claim.  Keep every contributing source object in the label lineage;
    # choose the longest fragment, using the group's median chainage as the
    # deterministic central tie-breaker for its leader anchor.
    grouped_named_sections: dict[str, list[dict[str, Any]]] = {}
    for candidate in named_section_candidates:
        grouped_named_sections.setdefault(candidate["copy"].copy.casefold(), []).append(
            candidate
        )
    available_named_sections: list[dict[str, Any]] = []
    for group in grouped_named_sections.values():
        ordered_chainages = sorted(float(value["chainage_mm"]) for value in group)
        central_chainage = ordered_chainages[len(ordered_chainages) // 2]
        representative_candidate = max(
            group,
            key=lambda value: (
                int(value["priority"]),
                float(value["paper_length_mm"]),
                -abs(float(value["chainage_mm"]) - central_chainage),
                str(value["section"]["id"]),
            ),
        )
        representative_record = dict(representative_candidate)
        representative_record["group_feature_ids"] = [
            str(value["section"]["id"])
            for value in sorted(group, key=lambda item: str(item["section"]["id"]))
        ]
        representative_record["group_source_objects"] = [
            source_object
            for value in group
            for source_object in value["section"].get("source_objects", [])
        ]
        representative_record["priority"] = max(
            int(value["priority"]) for value in group
        )
        available_named_sections.append(representative_record)
        for value in group:
            if value is representative_candidate:
                continue
            if value["section"]["id"] == representative_record["section"]["id"]:
                continue
            named_section_copy_omissions.append(
                {
                    "feature_id": value["section"]["id"],
                    "name": value["copy"].source_copy,
                    "reason": "deduplicated-into-longest-central-course-section-label",
                    "retained_feature_id": representative_record["section"]["id"],
                    "source_object_id": _source_object_attribute(
                        value["section"].get("source_objects", []),
                        str(value["section"]["source_ref"]),
                    ),
                }
            )
    section_limit = int(gate["named_section_label_limit"])
    selected_named_sections: list[dict[str, Any]] = []
    remaining_named_sections = list(available_named_sections)
    while remaining_named_sections and len(selected_named_sections) < section_limit:
        if not selected_named_sections:
            chosen = max(
                remaining_named_sections,
                key=lambda value: (
                    int(value["priority"]),
                    float(value["paper_length_mm"]),
                    str(value["copy"].copy),
                ),
            )
        else:
            chosen = max(
                remaining_named_sections,
                key=lambda value: (
                    int(value["priority"]),
                    min(
                        min(
                            abs(
                                float(value["chainage_mm"])
                                - float(selected["chainage_mm"])
                            ),
                            lap_page.length
                            - abs(
                                float(value["chainage_mm"])
                                - float(selected["chainage_mm"])
                            ),
                        )
                        for selected in selected_named_sections
                    ),
                    float(value["paper_length_mm"]),
                    str(value["copy"].copy),
                ),
            )
        selected_named_sections.append(chosen)
        remaining_named_sections.remove(chosen)
    selected_named_sections.sort(
        key=lambda value: (float(value["chainage_mm"]), str(value["copy"].copy))
    )
    for candidate in remaining_named_sections:
        named_section_copy_omissions.append(
            {
                "feature_id": candidate["section"]["id"],
                "name": candidate["copy"].source_copy,
                "reason": "paper-named-section-label-count-gate",
            }
        )

    named_section_lookup: dict[str, Mapping[str, Any]] = {}
    emitted_section_names: set[str] = set()
    # Course-section copy is visually heavier than a one- or two-character
    # station marker.  Give neighbouring section names extra breathing room so
    # labels such as "THE CHUTE" / "THE BOOT" cannot read as one phrase even
    # when their ordinary bounding boxes do not technically overlap.
    named_section_separation_mm = 1.5 * float(gate["label_separation_mm"])

    def section_placement_candidates(
        candidate: Mapping[str, Any],
        trial_boxes: list[Rect],
        trial_leaders: list[LineString],
        *,
        per_copy_limit: int,
    ) -> list[_LabelPlacement]:
        section = candidate["section"]
        label_copy = candidate["copy"]
        label_id = f"section-label-{section['id']}"
        source_ref = str(section["source_ref"])
        anchor_options = (
            candidate.get("anchor_options", (candidate["anchor"],))
            if section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
            else (candidate["anchor"],)
        )
        per_anchor_limit = max(
            1, int(math.ceil(per_copy_limit / max(1, len(anchor_options))))
        )
        maximum_leader_mm = (
            None
            if section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
            else min(
                30.0,
                max(
                    18.0,
                    0.18 * min(context.field.width, context.field.height),
                ),
            )
        )
        result: list[_LabelPlacement] = []
        seen: set[tuple[float, float, float, float]] = set()
        for anchor in anchor_options:
            if (
                section.get("name_status") != FAMOUS_SECTION_NAME_STATUS
                and not corridor_label_clearance_mask.is_empty
                and corridor_label_clearance_mask.buffer(4.0).covers(Point(anchor))
            ):
                continue
            tangent_distance = _nearest_distance(lap_page, anchor)
            section_tangent = _line_tangent(lap_page, tangent_distance)
            preferred = (-section_tangent[1], section_tangent[0])
            outward = (anchor[0] - lap_centroid.x, anchor[1] - lap_centroid.y)
            if outward[0] * preferred[0] + outward[1] * preferred[1] < 0:
                preferred = (-preferred[0], -preferred[1])
            for placement in _station_label_candidates(
                label_id=label_id,
                copies=((label_copy.copy, True),),
                anchor=anchor,
                preferred=preferred,
                source_ref=source_ref,
                role="section-label",
                cap_mm=label_cap,
                label_pen_nib_mm=copy_layer.pen.mark_width_mm,
                field=context.field.inset(0.5),
                protected_ink=venue_label_protected_ink,
                boxes=trial_boxes,
                leaders=trial_leaders,
                separation_mm=named_section_separation_mm,
                track_clearance_mm=float(gate["label_track_clearance_mm"]),
                feature_id=str(section["id"]),
                source_object_id=_source_object_attribute(
                    candidate["group_source_objects"], source_ref
                ),
                source_name_key=label_copy.source_name_key,
                source_copy=label_copy.source_copy,
                copy_policy_id=label_copy.copy_policy_id,
                normalisation_policy_id=label_copy.normalisation_policy_id,
                display_punctuation_policy_id=(
                    label_copy.display_punctuation_policy_id
                ),
                per_copy_limit=per_anchor_limit,
                maximum_leader_mm=maximum_leader_mm,
            ):
                if (
                    section.get("name_status") != FAMOUS_SECTION_NAME_STATUS
                    and placement.leader is not None
                    and not corridor_label_clearance_mask.is_empty
                    and LineString(placement.leader).intersects(
                        corridor_label_clearance_mask
                    )
                ):
                    continue
                bounds_key = (
                    round(placement.bounds.x, 6),
                    round(placement.bounds.y, 6),
                    round(placement.bounds.width, 6),
                    round(placement.bounds.height, 6),
                )
                if bounds_key in seen:
                    continue
                seen.add(bounds_key)
                result.append(placement)
                if len(result) >= per_copy_limit:
                    return result
        return result

    def commit_section_placement(
        candidate: Mapping[str, Any], placement: _LabelPlacement
    ) -> None:
        label_copy = candidate["copy"]
        placements.append(placement)
        named_section_lookup[placement.id] = candidate
        emitted_section_names.add(label_copy.copy)

    famous_selected_sections = [
        candidate
        for candidate in selected_named_sections
        if candidate["section"].get("name_status") == FAMOUS_SECTION_NAME_STATUS
    ]
    ordinary_selected_sections = [
        candidate
        for candidate in selected_named_sections
        if candidate["section"].get("name_status") != FAMOUS_SECTION_NAME_STATUS
    ]

    # Famous source-backed names are a required subset of the existing quota.
    # Solve them together so an early locally convenient label cannot consume
    # the only viable position for a later priority label.  This changes no
    # legacy behaviour: catalogs containing only OSM names continue through
    # the original greedy path below.
    if famous_selected_sections:
        section_search_nodes = 0
        section_search_budget = 100_000

        def solve_famous_sections(
            remaining: tuple[Mapping[str, Any], ...],
            trial_boxes: list[Rect],
            trial_leaders: list[LineString],
            trial_results: list[tuple[Mapping[str, Any], _LabelPlacement]],
        ) -> (
            tuple[
                list[Rect],
                list[LineString],
                list[tuple[Mapping[str, Any], _LabelPlacement]],
            ]
            | None
        ):
            nonlocal section_search_nodes
            if not remaining:
                return trial_boxes, trial_leaders, trial_results
            if section_search_nodes >= section_search_budget:
                return None
            ranked: list[tuple[int, int, str, int, list[_LabelPlacement]]] = []
            for candidate_index, candidate in enumerate(remaining):
                candidate_placements = section_placement_candidates(
                    candidate,
                    trial_boxes,
                    trial_leaders,
                    per_copy_limit=12,
                )
                if not candidate_placements:
                    return None
                ranked.append(
                    (
                        len(candidate_placements),
                        -int(candidate["priority"]),
                        str(candidate["section"]["id"]),
                        candidate_index,
                        candidate_placements,
                    )
                )
            _, _, _, chosen_index, chosen_placements = min(
                ranked, key=lambda value: value[:4]
            )
            chosen = remaining[chosen_index]
            next_remaining = remaining[:chosen_index] + remaining[chosen_index + 1 :]
            for placement in chosen_placements:
                section_search_nodes += 1
                if section_search_nodes > section_search_budget:
                    break
                assert placement.leader is not None
                solved_sections = solve_famous_sections(
                    next_remaining,
                    [*trial_boxes, placement.bounds],
                    [*trial_leaders, LineString(placement.leader)],
                    [*trial_results, (chosen, placement)],
                )
                if solved_sections is not None:
                    return solved_sections
            return None

        solved_sections = solve_famous_sections(
            tuple(famous_selected_sections), list(boxes), list(leaders), []
        )
        if solved_sections is None:
            raise MapPlotterError(
                f"Event {checked['id']!r} has no complete collision-free "
                f"{format_id} famous-course-section layout after "
                f"{section_search_nodes} deterministic search nodes. No "
                "priority famous name was dropped."
            )
        boxes, leaders, section_results = solved_sections
        for solved_candidate, solved_placement in section_results:
            commit_section_placement(solved_candidate, solved_placement)

    for ordinary_candidate in ordinary_selected_sections:
        ordinary_section = ordinary_candidate["section"]
        ordinary_label_copy = ordinary_candidate["copy"]
        ordinary_candidate_placements = section_placement_candidates(
            ordinary_candidate, boxes, leaders, per_copy_limit=1
        )
        ordinary_placement = (
            ordinary_candidate_placements[0]
            if ordinary_candidate_placements
            else None
        )
        if ordinary_placement is None:
            named_section_copy_omissions.append(
                {
                    "feature_id": ordinary_section["id"],
                    "name": ordinary_label_copy.source_copy,
                    "reason": "course-section-name-collision",
                }
            )
            continue
        commit_section_placement(ordinary_candidate, ordinary_placement)
        boxes.append(ordinary_placement.bounds)
        assert ordinary_placement.leader is not None
        leaders.append(LineString(ordinary_placement.leader))

    # Lower-priority source names use remaining negative space.  Omitting one
    # is ledgered; it never affects the complete turn-station inventory.
    context_label_cap = label_cap
    context_label_count = 0
    context_label_limit = int(gate["context_label_limit"])
    emitted_context_names: set[str] = set(emitted_section_names)
    for feature in selected_context:
        tags_value = feature.get("tags")
        tags = tags_value if isinstance(tags_value, Mapping) else {}
        available_name_keys = [
            key
            for key in CONTEXT_LABEL_SOURCE_KEYS
            if isinstance(feature.get("name") if key == "name" else tags.get(key), str)
            and str(feature.get("name") if key == "name" else tags.get(key)).strip()
        ]
        if not available_name_keys:
            continue
        label_copy = _context_label_copy(feature)
        if label_copy is None:
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": feature["_render_kind"],
                    "reason": "context-name-no-drawable-sourced-copy",
                    "source_name_keys_attempted": available_name_keys,
                    "copy_policy_id": CONTEXT_LABEL_COPY_POLICY_ID,
                    "normalisation_policy_id": TEXT_NORMALISATION_POLICY_ID,
                    "display_punctuation_policy_id": (
                        CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID
                    ),
                }
            )
            continue
        normalized_name = label_copy.copy
        if len(normalized_name) < 3 or normalized_name.isdigit():
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": feature["_render_kind"],
                    "reason": "context-name-too-weak-for-plate",
                    "source_name_key": label_copy.source_name_key,
                    "source_copy": label_copy.source_copy,
                    "visible_copy": normalized_name,
                    "copy_policy_id": label_copy.copy_policy_id,
                    "display_punctuation_policy_id": (
                        label_copy.display_punctuation_policy_id
                    ),
                }
            )
            continue
        if normalized_name in emitted_context_names:
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": feature["_render_kind"],
                    "reason": "duplicate-normalized-context-name",
                    "source_name_key": label_copy.source_name_key,
                    "source_copy": label_copy.source_copy,
                    "visible_copy": normalized_name,
                    "copy_policy_id": label_copy.copy_policy_id,
                    "display_punctuation_policy_id": (
                        label_copy.display_punctuation_policy_id
                    ),
                }
            )
            continue
        if context_label_count >= context_label_limit:
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": feature["_render_kind"],
                    "reason": "paper-context-label-count-gate",
                    "source_name_key": label_copy.source_name_key,
                    "source_copy": label_copy.source_copy,
                    "visible_copy": normalized_name,
                    "copy_policy_id": label_copy.copy_policy_id,
                    "display_punctuation_policy_id": (
                        label_copy.display_punctuation_policy_id
                    ),
                }
            )
            continue
        source_geometry = _geojson_geometry(
            feature["geometry"], f"context {feature['id']}"
        )
        paper_geometry = transform_value.geometry(
            source_geometry.intersection(viewport)
        )
        if paper_geometry.is_empty:
            continue
        context_anchor_point = paper_geometry.representative_point()
        context_placement = _place_context_label(
            label_id=f"context-label-{feature['id']}",
            copy_text=normalized_name,
            anchor=(float(context_anchor_point.x), float(context_anchor_point.y)),
            source_ref=str(feature["source_ref"]),
            cap_mm=context_label_cap,
            label_pen_nib_mm=copy_layer.pen.mark_width_mm,
            field=context.field.inset(0.5),
            protected_ink=(
                venue_label_protected_ink
                if str(feature["_render_kind"]) in {"grandstand", "principal-building"}
                else protected_ink
            ),
            boxes=boxes,
            leaders=leaders,
            separation_mm=float(gate["label_separation_mm"]),
            source_name_key=label_copy.source_name_key,
            source_copy=label_copy.source_copy,
            copy_policy_id=label_copy.copy_policy_id,
            normalisation_policy_id=label_copy.normalisation_policy_id,
            display_punctuation_policy_id=(label_copy.display_punctuation_policy_id),
            feature_id=str(feature["id"]),
            source_object_id=_source_object_attribute(
                feature.get("source_objects", []), str(feature["source_ref"])
            ),
        )
        if context_placement is None:
            omissions.append(
                {
                    "feature_id": feature["id"],
                    "kind": feature["_render_kind"],
                    "reason": "context-name-collision",
                    "source_name_key": label_copy.source_name_key,
                    "source_copy": label_copy.source_copy,
                    "visible_copy": normalized_name,
                    "copy_policy_id": label_copy.copy_policy_id,
                    "display_punctuation_policy_id": (
                        label_copy.display_punctuation_policy_id
                    ),
                }
            )
            continue
        placements.append(context_placement)
        boxes.append(context_placement.bounds)
        emitted_context_names.add(normalized_name)
        context_label_count += 1

    # Emit leaders then their physical vector copy.  Bounds were reserved
    # before any context linework, allowing real absence-of-ink knockouts.
    for placement in placements:
        if placement.leader is not None:
            leader_role = (
                "turn-marker" if placement.role == "turn-label" else placement.role
            )
            leader_attributes = {
                "data-label-id": placement.id,
                "data-label-box": _label_box_attribute(placement.bounds),
                "data-leader-routing": (
                    "elbow" if len(placement.leader) > 2 else "direct"
                ),
                "data-source-object-id": (
                    placement.source_object_id or placement.source_ref
                ),
            }
            if placement.feature_id is not None:
                leader_attributes["data-feature-id"] = placement.feature_id
            if placement.role == "section-label":
                section_candidate = named_section_lookup[placement.id]
                section = section_candidate["section"]
                official_name = section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
                anchor_source_ref = str(
                    section.get("anchor_source_ref") or section["source_ref"]
                )
                leader_attributes.update(
                    {
                        "data-source-name-key": str(placement.source_name_key),
                        "data-source-copy": str(placement.source_copy),
                        "data-visible-copy": placement.copy,
                        "data-copy-policy-id": str(placement.copy_policy_id),
                        "data-normalisation-policy-id": str(
                            placement.normalisation_policy_id
                        ),
                        "data-display-punctuation-policy-id": str(
                            placement.display_punctuation_policy_id
                        ),
                        "data-name-status": str(section["name_status"]),
                        "data-official-course-name": str(official_name).lower(),
                        **(
                            {
                                "data-name-source-ref": str(section["name_source_ref"]),
                                "data-anchor-source-ref": anchor_source_ref,
                                "data-anchor-source-object-id": (
                                    _source_object_attribute(
                                        section_candidate["group_source_objects"],
                                        anchor_source_ref,
                                    )
                                ),
                                "data-anchor-mode": str(section["anchor_mode"]),
                                "data-anchor-status": str(section["anchor_status"]),
                                "data-course-section-priority": str(
                                    section_candidate["priority"]
                                ),
                            }
                            if official_name
                            else {}
                        ),
                        "data-claim-scope": str(section["claim_scope"]),
                        "data-source-feature-ids": "|".join(
                            section_candidate["group_feature_ids"]
                        ),
                    }
                )
            if placement.role == "turn-label":
                station_class = station_classes[placement.id]
                leader_attributes.update(
                    {
                        # Retained for the v1 QA/index contract; the explicit
                        # station class prevents a geometric marker being read
                        # as an official turn or racing apex.
                        "data-turn-id": placement.id,
                        "data-station-id": placement.id,
                        "data-station-class": station_class,
                        "data-marker-prefix": _station_marker_prefix(station_class),
                    }
                )
                if station_class in {"geometric", "source-tagged"}:
                    leader_attributes["data-derivation"] = "station-on-lap"
                else:
                    leader_attributes["data-evidence-class"] = station_class
            copy_layer.add(
                list(placement.leader),
                source_ref=placement.source_ref,
                role=leader_role,
                attributes=leader_attributes,
            )
        text_zone = placement.bounds
        label_attributes = {
            "data-label-id": placement.id,
            "data-label-box": _label_box_attribute(placement.bounds),
            "data-source-object-id": (
                placement.source_object_id or placement.source_ref
            ),
        }
        if placement.feature_id is not None:
            label_attributes["data-feature-id"] = placement.feature_id
        if placement.role in {"context-label", "section-label"}:
            if not all(
                (
                    placement.source_name_key,
                    placement.source_copy,
                    placement.copy_policy_id,
                    placement.normalisation_policy_id,
                    placement.display_punctuation_policy_id,
                )
            ):
                raise MapPlotterError(
                    f"Source label {placement.id!r} lacks source-copy policy lineage."
                )
            label_attributes.update(
                {
                    "data-source-name-key": str(placement.source_name_key),
                    "data-source-copy": str(placement.source_copy),
                    "data-visible-copy": placement.copy,
                    "data-copy-policy-id": str(placement.copy_policy_id),
                    "data-normalisation-policy-id": str(
                        placement.normalisation_policy_id
                    ),
                    "data-display-punctuation-policy-id": str(
                        placement.display_punctuation_policy_id
                    ),
                }
            )
        if placement.role == "section-label":
            section_candidate = named_section_lookup[placement.id]
            section = section_candidate["section"]
            official_name = section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
            anchor_source_ref = str(
                section.get("anchor_source_ref") or section["source_ref"]
            )
            label_attributes.update(
                {
                    "data-name-status": str(section["name_status"]),
                    "data-official-course-name": str(official_name).lower(),
                    **(
                        {
                            "data-name-source-ref": str(section["name_source_ref"]),
                            "data-anchor-source-ref": anchor_source_ref,
                            "data-anchor-source-object-id": (
                                _source_object_attribute(
                                    section_candidate["group_source_objects"],
                                    anchor_source_ref,
                                )
                            ),
                            "data-anchor-mode": str(section["anchor_mode"]),
                            "data-anchor-status": str(section["anchor_status"]),
                            "data-course-section-priority": str(
                                section_candidate["priority"]
                            ),
                        }
                        if official_name
                        else {}
                    ),
                    "data-claim-scope": str(section["claim_scope"]),
                    "data-source-feature-ids": "|".join(
                        section_candidate["group_feature_ids"]
                    ),
                }
            )
        if placement.role == "turn-label":
            station_class = station_classes[placement.id]
            label_attributes.update(
                {
                    "data-turn-id": placement.id,
                    "data-station-id": placement.id,
                    "data-station-class": station_class,
                    "data-marker-prefix": _station_marker_prefix(station_class),
                    "data-cap-height-mm": f"{placement.cap_mm:.3f}",
                }
            )
            if station_class in {"geometric", "source-tagged"}:
                label_attributes["data-derivation"] = "station-on-lap"
            else:
                label_attributes["data-evidence-class"] = station_class
        add_text(
            copy_layer,
            placement.copy,
            x_mm=text_zone.centre[0],
            y_mm=text_zone.centre[1] - placement.cap_mm / 2.0,
            preferred_cap_mm=placement.cap_mm,
            maximum_width_mm=text_zone.width
            - 2.0 * max(0.35, copy_layer.pen.mark_width_mm),
            anchor="middle",
            source_ref=placement.source_ref,
            role=placement.role,
            attributes=label_attributes,
        )

    north_centre_x = north_box.centre[0]
    north_bottom = north_box.bottom - 0.8
    north_top = north_box.top + label_cap + 0.9
    copy_layer.add(
        [(north_centre_x, north_bottom), (north_centre_x, north_top)],
        role="north-arrow",
        attributes={"data-orientation": "north-up", "data-rotation-deg": "0"},
    )
    head = max(1.1, 3.0 * copy_layer.pen.mark_width_mm)
    copy_layer.add(
        [
            (north_centre_x - 0.55 * head, north_top + head),
            (north_centre_x, north_top),
            (north_centre_x + 0.55 * head, north_top + head),
        ],
        role="north-arrow-head",
        attributes={"data-orientation": "north-up"},
    )
    add_text(
        copy_layer,
        "N",
        x_mm=north_centre_x,
        y_mm=north_box.top,
        preferred_cap_mm=label_cap,
        maximum_width_mm=north_box.width,
        anchor="middle",
        role="north-label",
        attributes={"data-orientation": "north-up"},
    )

    label_shapes = [_rect_polygon(value) for value in boxes]
    leader_shapes = [line.buffer(copy_layer.pen.mark_width_mm) for line in leaders]
    label_mask = unary_union([*label_shapes, *leader_shapes])

    # A nearby raceway polygon is not automatically the selected Grand Prix
    # surface.  Each source record must first pass lap-association and then the
    # three-Grey-nib paper separation gate.  This deliberately rejects the
    # unrelated motocross/small raceway areas currently present near Austria
    # and Monza while retaining the genuine Interlagos asphalt multipolygon.
    boundary_qualifications: list[dict[str, Any]] = []
    boundary_path_count = 0
    boundary_emitted_feature_count = 0
    for index, boundary in enumerate(boundary_sources):
        record = model["track_boundaries"][index]
        props = record.get("properties", {}) if isinstance(record, dict) else {}
        source_objects_value = (
            props.get("source_objects", record.get("source_objects", []))
            if isinstance(record, dict)
            else []
        )
        source_objects = (
            source_objects_value if isinstance(source_objects_value, list) else []
        )
        qualification = _track_boundary_qualification(
            boundary,
            lap_source,
            transform_value,
            grey.pen.mark_width_mm,
        )
        qualification = {
            "boundary_index": index,
            "source_feature_id": props.get("id"),
            "source_object_ids": sorted(
                _source_object_identity(value)
                for value in source_objects
            ),
            **qualification,
        }
        boundary_qualifications.append(qualification)
        if not qualification["resolvable"]:
            omissions.append(
                {
                    "feature_id": f"track-boundary-{index}",
                    "reason": qualification["reason"],
                    "lap_coverage_fraction": qualification["lap_coverage_fraction"],
                    "representative_clearance_mm": qualification[
                        "representative_clearance_mm"
                    ],
                    "required_mm": qualification["required_clearance_mm"],
                }
            )
            continue
        source_ref = str(props.get("source_ref", lap_source_ref))
        paper = transform_value.geometry(boundary.intersection(viewport))
        # Subtract from original boundary linework, not polygon area, so a
        # label knockout cannot create a false rectangular boundary.
        clear = paper.boundary if isinstance(paper, (Polygon, MultiPolygon)) else paper
        clear = clear.difference(label_mask)
        boundary_attributes = {
            "data-boundary-index": str(index),
            "data-invented-width": "false",
            "data-lap-associated": "true",
            "data-boundary-geometry-kind": str(qualification["geometry_kind"]),
            "data-association-policy": str(qualification["association_policy"]),
            "data-source-object-id": _source_object_attribute(
                source_objects if isinstance(source_objects, list) else [],
                source_ref,
            ),
        }
        if qualification["lap_coverage_fraction"] is not None:
            boundary_attributes["data-lap-coverage-fraction"] = format_number(
                qualification["lap_coverage_fraction"]
            )
        result = _emit_linework(
            grey,
            clear,
            source_ref=source_ref,
            role="track-boundary",
            attributes=boundary_attributes,
            omissions=omissions,
            feature_id=f"track-boundary-{index}",
        )
        boundary_path_count += int(result["paths"])
        if int(result["paths"]) > 0:
            boundary_emitted_feature_count += 1
    boundary_clearances = [
        float(value["representative_clearance_mm"])
        for value in boundary_qualifications
        if value["lap_associated"] and value["representative_clearance_mm"] is not None
    ]
    boundary_clearance_mm = min(boundary_clearances) if boundary_clearances else None

    context_output_counts: dict[str, int] = {}
    water_dot_count = 0
    vegetation_symbol_count = 0
    runoff_hatch_count = 0
    gravel_dot_count = 0
    road_suppression_mm = 0.0
    road_before_mm = 0.0
    vegetation_outline_candidates: list[_VegetationOutlineCandidate] = []
    vegetation_symbol_counts_by_feature: dict[str, int] = {}
    for feature in selected_context:
        kind = str(feature["_render_kind"])
        context_output_counts.setdefault(kind, 0)
        source = _geojson_geometry(feature["geometry"], f"context {feature['id']}")
        clipped_source = source.intersection(viewport)
        if clipped_source.is_empty:
            continue
        if kind in {"road", "access-road"}:
            road_before_mm += clipped_source.length * transform_value.scale_mm_per_m
            halo_source_m = (
                float(gate["track_halo_mm"]) / transform_value.scale_mm_per_m
            )
            clear_source, removed_source_m = _suppress_coincident_host_road(
                clipped_source,
                lap_source,
                halo_m=halo_source_m,
            )
            road_suppression_mm += removed_source_m * transform_value.scale_mm_per_m
            clipped_source = clear_source
        paper_geometry = transform_value.geometry(clipped_source)
        source_ref = str(feature["source_ref"])
        feature_id = str(feature["id"])
        source_object_id = _source_object_attribute(
            feature["source_objects"], source_ref
        )
        attributes = {
            "data-feature-id": feature_id,
            "data-context-kind": kind,
            "data-source-object-count": str(len(feature["source_objects"])),
            "data-source-object-id": source_object_id,
            "data-white-ink": "false",
            **(
                {
                    "data-context-temporality": str(feature["source_temporality"]),
                    "data-claim-scope": str(feature["claim_scope"]),
                    "data-event-configuration-verified": "false",
                    "data-fia-configuration-claimed": "false",
                    "data-operational-semantics-claimed": "false",
                    "data-valid-for-season": "withheld",
                }
                if kind == "grandstand" and _is_frozen_current_osm_grandstand(feature)
                else {"data-valid-for-season": str(feature["valid_for_season"])}
            ),
        }
        if kind in {
            "water",
            "grass",
            "woodland",
            "grandstand",
            "principal-building",
            "building",
            "kerb",
            "runoff",
            "gravel-trap",
            "paddock",
            "garage",
            "pit-building",
        }:
            outline = transform_value.geometry(
                _context_outline_source_geometry(source, viewport)
            ).difference(label_mask)
        else:
            outline = paper_geometry.difference(label_mask)
        track_halo_paper = lap_page.buffer(
            float(gate["track_halo_mm"]), cap_style="round"
        )
        target = (
            blue
            if kind == "water"
            else green
            if kind in {"grass", "woodland"}
            else purple
            if kind
            in {
                "grandstand",
                "principal-building",
                "paddock",
                "garage",
                "pit-building",
            }
            else grey
        )
        deferred_vegetation_layer: ArtworkLayer | None = None
        deferred_vegetation_length_mm = 0.0
        target_record_start = len(target.records)
        if kind in {"grass", "woodland"}:
            deferred_vegetation_layer = ArtworkLayer(
                f"deferred-vegetation-{feature_id}",
                "Deferred vegetation outline",
                green.pen_id,
            )
            candidate_omissions: list[dict[str, Any]] = []
            candidate_result = _emit_linework(
                deferred_vegetation_layer,
                outline,
                source_ref=source_ref,
                role=f"context-{kind}",
                attributes=attributes,
                omissions=candidate_omissions,
                feature_id=feature_id,
            )
            omissions.extend(candidate_omissions)
            deferred_vegetation_length_mm = float(candidate_result["length_mm"])
            emitted_outline_paths = 0
        else:
            result = _emit_linework(
                target,
                outline,
                source_ref=source_ref,
                role=(
                    "host-road"
                    if kind in {"road", "access-road"}
                    else f"context-{kind}"
                ),
                attributes=attributes,
                omissions=omissions,
                feature_id=feature_id,
            )
            emitted_outline_paths = int(result["paths"])
        context_output_counts[kind] = context_output_counts.get(kind, 0) + int(
            emitted_outline_paths
        )
        fill_area = paper_geometry.difference(label_mask).difference(track_halo_paper)
        if kind == "water" and any(_polygon_parts(paper_geometry)):
            feature_water_dot_count = (
                _water_stipple(
                    blue,
                    fill_area,
                    spacing_mm=float(gate["water_stipple_spacing_mm"]),
                    source_ref=source_ref,
                    source_object_id=source_object_id,
                    feature_id=feature_id,
                    maximum=(220 if sheet == "A5" else 420 if sheet == "A4" else 700),
                )
                if any(_polygon_parts(fill_area))
                else 0
            )
            if feature_water_dot_count == 0:
                # A polygon outline without the requested dotted-water
                # language is ambiguous.  If a physical closed dot cannot fit
                # after source clipping and protected-ink masks, omit the
                # complete source feature rather than silently changing its
                # cartographic grammar.
                del target.records[target_record_start:]
                context_output_counts[kind] = max(
                    0,
                    context_output_counts.get(kind, 0) - emitted_outline_paths,
                )
                omissions.append(
                    {
                        "feature_id": feature_id,
                        "kind": kind,
                        "role": "context-water",
                        "reason": "paper-water-polygon-cannot-fit-closed-stipple",
                        "whole_feature_group": True,
                        "minimum_dot_nib_mm": blue.pen.mark_width_mm,
                    }
                )
                continue
            water_dot_count += feature_water_dot_count
        elif kind in {"grass", "woodland"}:
            # Vegetation is represented by its exact clipped source boundary.
            # Interior glyph fields read as a dot texture at print scale and
            # compete with the deliberately dotted water language.
            vegetation_symbol_counts_by_feature[feature_id] = 0
        elif kind == "runoff" and any(_polygon_parts(fill_area)):
            runoff_hatch_count += _area_hatch(
                grey,
                fill_area,
                spacing_mm=float(gate["runoff_hatch_spacing_mm"]),
                source_ref=source_ref,
                source_object_id=source_object_id,
                feature_id=feature_id,
                maximum=80 if sheet == "A5" else 160 if sheet == "A4" else 260,
            )
        elif kind == "gravel-trap" and any(_polygon_parts(fill_area)):
            gravel_dot_count += _water_stipple(
                grey,
                fill_area,
                spacing_mm=float(gate["gravel_stipple_spacing_mm"]),
                source_ref=source_ref,
                source_object_id=source_object_id,
                feature_id=feature_id,
                maximum=100 if sheet == "A5" else 200 if sheet == "A4" else 340,
                role="gravel-stipple-dot",
                derivation="stipple-inside-source-gravel-trap",
            )
        if deferred_vegetation_layer is not None and deferred_vegetation_layer.records:
            vegetation_outline_candidates.append(
                _VegetationOutlineCandidate(
                    feature_id=feature_id,
                    kind=kind,
                    named=bool(str(feature.get("name") or "").strip()),
                    paper_area_mm2=round(float(paper_geometry.area), 6),
                    records=tuple(deferred_vegetation_layer.records),
                    length_mm=deferred_vegetation_length_mm,
                    interior_symbol_count=vegetation_symbol_counts_by_feature.get(
                        feature_id, 0
                    ),
                )
            )

    # Special sections annotate sourced structural facts without interrupting
    # or duplicating the continuous Red lap centreline. At a source-backed
    # figure-eight overpass, Grey would be plotted before and then hidden by
    # the multi-pass Red course. Use a Black bracket (two terminal bars and two
    # longitudinal symbolic rails) after Red.  The bracket is a cartographic
    # over/under cue, never a surveyed-width claim.
    special_emitted = 0
    grade_separation_cue_emitted = 0
    lap_self_crossings = _lap_self_intersections(lap_source)
    matched_grade_sections: dict[str, dict[str, Any]] = {}
    matched_crossing_indexes: set[int] = set()
    for section_index, section in enumerate(model.get("special_sections", [])):
        if (
            str(section.get("kind", "")).casefold().replace("_", "-") != "overpass"
            or "geometry" not in section
        ):
            continue
        # Defense in depth: cue emission re-qualifies the embedded bridge
        # evidence and contiguous selected-lap lineage after model validation.
        bridge_source = _validate_source_backed_overpass(
            section,
            model,
            lap_source,
            label=f"render.special_sections[{section_index}]",
        )
        crossing_indexes = [
            crossing_index
            for crossing_index, crossing in enumerate(lap_self_crossings)
            if bridge_source.distance(crossing["point"]) <= 1e-6
        ]
        if len(crossing_indexes) > 1:
            raise MapPlotterError(
                f"Overpass {section['id']!r} contains multiple lap "
                "self-intersections; cue held."
            )
        if not crossing_indexes:
            continue
        crossing_index = crossing_indexes[0]
        if crossing_index in matched_crossing_indexes:
            raise MapPlotterError(
                "One lap self-intersection is claimed by multiple source-backed "
                "overpass sections; cue held."
            )
        matched_crossing_indexes.add(crossing_index)
        matched_grade_sections[str(section["id"])] = lap_self_crossings[crossing_index]
    if len(matched_crossing_indexes) != len(lap_self_crossings):
        raise MapPlotterError(
            "Every non-simple F1 lap crossing requires one exact tagged "
            "selected-lap overpass section; artifact held."
        )
    grade_separation_source_section_ids = sorted(matched_grade_sections)
    grade_separation_cue_required = bool(lap_self_crossings)
    for section in model.get("special_sections", []):
        kind = str(section["kind"]).casefold().replace("_", "-")
        if kind == "named-course-section":
            # Exact source geometry anchors the separately collision-solved
            # label; duplicating that line over the Red course would imply a
            # second operational geometry claim.
            continue
        if kind not in {"tunnel", "overpass", "underpass", "grade-separated-crossing"}:
            omissions.append(
                {
                    "feature_id": section["id"],
                    "kind": kind,
                    "reason": "unsupported-special-section",
                }
            )
            continue
        if (
            kind == "overpass"
            and str(section["id"]) in matched_grade_sections
            and "geometry" in section
        ):
            paper_geometry = transform_value.geometry(
                _geojson_geometry(section["geometry"], f"special {section['id']}")
            )
            line_parts = list(_line_parts(paper_geometry))
            if not line_parts:
                omissions.append(
                    {
                        "feature_id": section["id"],
                        "kind": kind,
                        "reason": "grade-separation-cue-has-no-line-geometry",
                    }
                )
                continue
            bridge_line = max(line_parts, key=lambda value: value.length)
            bridge_coordinates = [
                (float(x), float(y)) for x, y, *_rest in bridge_line.coords
            ]
            if len(bridge_coordinates) < 2:
                omissions.append(
                    {
                        "feature_id": section["id"],
                        "kind": kind,
                        "reason": "grade-separation-cue-has-no-tangent",
                    }
                )
                continue
            source_ref = str(section["source_ref"])
            source_object_id = _source_object_attribute(
                section.get("source_objects") or [], source_ref
            )
            rail_half_width_mm = (
                corridor_plan.target_width_mm / 2.0 + 1.5 * copy_layer.pen.mark_width_mm
            )
            crossing = matched_grade_sections[str(section["id"])]
            common_attributes = {
                "data-feature-id": str(section["id"]),
                "data-section-id": str(section["id"]),
                "data-source-object-id": source_object_id,
                "data-operational-kind": kind,
                "data-cue-policy": GRADE_SEPARATION_CUE_POLICY,
                "data-cartographic-symbol": "true",
                "data-source-geometry-claim": "false",
                "data-surveyed-track-width": "false",
                "data-red-lap-interrupted": "false",
                "data-white-ink": "false",
                "data-self-intersection-segment-indexes": "|".join(
                    str(value) for value in crossing["segment_indexes"]
                ),
            }
            for cue_end, endpoint, neighbour in (
                ("start", bridge_coordinates[0], bridge_coordinates[1]),
                ("end", bridge_coordinates[-1], bridge_coordinates[-2]),
            ):
                tangent_x = neighbour[0] - endpoint[0]
                tangent_y = neighbour[1] - endpoint[1]
                tangent_length = math.hypot(tangent_x, tangent_y)
                if tangent_length <= 1e-9:
                    raise MapPlotterError(
                        f"Special overpass {section['id']!r} has a degenerate "
                        f"{cue_end} tangent; bridge cue held."
                    )
                normal = (-tangent_y / tangent_length, tangent_x / tangent_length)
                copy_layer.add(
                    [
                        (
                            endpoint[0] - rail_half_width_mm * normal[0],
                            endpoint[1] - rail_half_width_mm * normal[1],
                        ),
                        (
                            endpoint[0] + rail_half_width_mm * normal[0],
                            endpoint[1] + rail_half_width_mm * normal[1],
                        ),
                    ],
                    source_ref=source_ref,
                    role="grade-separation-cue",
                    attributes={
                        **common_attributes,
                        "data-cue-end": cue_end,
                        "data-cue-part": f"terminal-{cue_end}",
                    },
                )
                grade_separation_cue_emitted += 1
                special_emitted += 1
            for rail_side, signed_offset in (
                ("left", rail_half_width_mm),
                ("right", -rail_half_width_mm),
            ):
                try:
                    rail_geometry = bridge_line.offset_curve(
                        signed_offset,
                        quad_segs=4,
                        join_style="round",
                    )
                except GEOSException as exc:
                    raise MapPlotterError(
                        f"Special overpass {section['id']!r} bridge rail "
                        "offset failed; cue held."
                    ) from exc
                rail_parts = list(_line_parts(rail_geometry))
                if not rail_parts:
                    raise MapPlotterError(
                        f"Special overpass {section['id']!r} bridge rail "
                        "collapsed; cue held."
                    )
                rail = max(rail_parts, key=lambda value: value.length)
                rail_points = [(float(x), float(y)) for x, y, *_rest in rail.coords]
                if polyline_length_mm(rail_points) < 3.0 * copy_layer.pen.mark_width_mm:
                    raise MapPlotterError(
                        f"Special overpass {section['id']!r} bridge rail is "
                        "below the three-nib floor; cue held."
                    )
                copy_layer.add(
                    rail_points,
                    source_ref=source_ref,
                    role="grade-separation-cue",
                    attributes={
                        **common_attributes,
                        "data-cue-part": f"rail-{rail_side}",
                        "data-bridge-rail-offset-mm": format_number(rail_half_width_mm),
                    },
                )
                grade_separation_cue_emitted += 1
                special_emitted += 1
            continue
        if "geometry" in section:
            paper = transform_value.geometry(
                _geojson_geometry(section["geometry"], f"special {section['id']}")
            )
            result = _emit_linework(
                grey,
                paper,
                source_ref=str(section["source_ref"]),
                role="operational-overlay",
                attributes={
                    "data-feature-id": str(section["id"]),
                    "data-operational-kind": kind,
                    "data-red-lap-interrupted": "false",
                    "data-source-object-id": _source_object_attribute(
                        section.get("source_objects") or [], str(section["source_ref"])
                    ),
                },
                omissions=omissions,
                feature_id=str(section["id"]),
            )
            special_emitted += int(result["paths"])
        else:
            overlay_point = transform_value.point(section["point"])
            size = max(1.0, 3.0 * grey.pen.mark_width_mm)
            strokes = [
                [
                    (overlay_point[0] - size, overlay_point[1] - size),
                    (overlay_point[0] - size, overlay_point[1] + size),
                ],
                [
                    (overlay_point[0] + size, overlay_point[1] - size),
                    (overlay_point[0] + size, overlay_point[1] + size),
                ],
            ]
            for stroke in strokes:
                grey.add(
                    stroke,
                    source_ref=str(section["source_ref"]),
                    role="operational-overlay",
                    attributes={
                        "data-feature-id": str(section["id"]),
                        "data-operational-kind": kind,
                        "data-red-lap-interrupted": "false",
                        "data-source-object-id": _source_object_attribute(
                            section.get("source_objects") or [],
                            str(section["source_ref"]),
                        ),
                    },
                )
                special_emitted += 1

    field_area_mm2 = context.field.width * context.field.height
    density_target = float(gate["field_density_target_mm_per_mm2"])
    density_target_length_mm = field_area_mm2 * density_target
    vegetation_outline_policy = str(gate["vegetation_outline_policy"])
    mandatory_vegetation_feature_ids = (
        set()
        if vegetation_outline_policy == "outline-only-density-budgeted-source-boundary"
        else {
            candidate.feature_id
            for candidate in vegetation_outline_candidates
            if candidate.interior_symbol_count == 0
        }
    )
    mandatory_vegetation_reserve_mm = sum(
        candidate.length_mm
        for candidate in vegetation_outline_candidates
        if candidate.feature_id in mandatory_vegetation_feature_ids
    )
    candidate_vegetation_outline_mm = sum(
        candidate.length_mm for candidate in vegetation_outline_candidates
    )
    configured_vegetation_reserve_density = float(
        gate.get("vegetation_outline_density_reserve_mm_per_mm2", 0.0)
    )
    vegetation_outline_reserve_mm = max(
        mandatory_vegetation_reserve_mm,
        min(
            candidate_vegetation_outline_mm,
            field_area_mm2 * configured_vegetation_reserve_density,
        ),
    )
    context_density_budget, density_culled_feature_ids = (
        _prune_context_to_density_budget(
            layers,
            selected_context,
            placements,
            omissions,
            maximum_baseline_length_mm=max(
                0.0, density_target_length_mm - vegetation_outline_reserve_mm
            ),
            hard_maximum_baseline_length_mm=max(
                0.0,
                field_area_mm2 * 0.18 - mandatory_vegetation_reserve_mm,
            ),
            sheet=sheet,
            protected_feature_ids=mandatory_vegetation_feature_ids,
        )
    )
    density_boundary_indices = {
        int(boundary_index)
        for decision in context_density_budget["decisions"]
        if decision.get("reason")
        == "paper-field-density-budget-qualified-track-boundary-group"
        for boundary_index in decision.get("boundary_indices", [])
    }
    for qualification in boundary_qualifications:
        qualification["emission_status"] = (
            "omitted-paper-density-budget"
            if qualification["boundary_index"] in density_boundary_indices
            else (
                "emitted"
                if qualification["resolvable"]
                else "omitted-source-qualification"
            )
        )
        qualification["emission_omission_reason"] = (
            "paper-field-density-budget-qualified-track-boundary-group"
            if qualification["boundary_index"] in density_boundary_indices
            else qualification.get("reason")
        )
    emitted_boundary_records = [
        record for record in grey.records if record.role == "track-boundary"
    ]
    boundary_path_count = len(emitted_boundary_records)
    boundary_emitted_feature_count = len(
        {
            str(record.attributes.get("data-boundary-index"))
            for record in emitted_boundary_records
            if record.attributes.get("data-boundary-index") is not None
        }
    )
    vegetation_outline_candidates = [
        candidate
        for candidate in vegetation_outline_candidates
        if candidate.feature_id not in density_culled_feature_ids
    ]
    vegetation_baseline_length_mm = sum(
        _serialized_polyline_length_mm(record.points)
        for artwork_layer in layers.values()
        for record in artwork_layer.records
    )
    vegetation_available_length_mm = max(
        0.0, density_target_length_mm - vegetation_baseline_length_mm
    )
    retained_vegetation_outlines, omitted_vegetation_outlines = (
        _budget_vegetation_outline_candidates(
            vegetation_outline_candidates,
            available_length_mm=vegetation_available_length_mm,
            policy=vegetation_outline_policy,
        )
    )
    for vegetation_candidate in retained_vegetation_outlines:
        green.records.extend(vegetation_candidate.records)
    for vegetation_candidate in omitted_vegetation_outlines:
        omissions.append(
            {
                "feature_id": vegetation_candidate.feature_id,
                "kind": vegetation_candidate.kind,
                "role": f"context-{vegetation_candidate.kind}",
                "reason": (
                    "paper-vegetation-symbols-only-policy"
                    if vegetation_outline_policy == "symbols-only"
                    else "paper-vegetation-outline-density-budget"
                ),
                "outline_length_mm": round(vegetation_candidate.length_mm, 6),
                "paper_area_mm2": vegetation_candidate.paper_area_mm2,
                "named": vegetation_candidate.named,
                "whole_feature_group": True,
                "interior_symbol_count": vegetation_candidate.interior_symbol_count,
            }
        )
    retained_vegetation_outline_mm = sum(
        candidate.length_mm for candidate in retained_vegetation_outlines
    )
    omitted_vegetation_outline_mm = sum(
        candidate.length_mm for candidate in omitted_vegetation_outlines
    )
    retained_vegetation_ids = {
        candidate.feature_id for candidate in retained_vegetation_outlines
    }
    mandatory_vegetation_outlines = [
        candidate
        for candidate in vegetation_outline_candidates
        if candidate.feature_id in mandatory_vegetation_feature_ids
    ]
    mandatory_vegetation_outline_mm = sum(
        candidate.length_mm for candidate in mandatory_vegetation_outlines
    )
    mandatory_outline_overage_mm = max(
        0.0,
        mandatory_vegetation_outline_mm - vegetation_available_length_mm,
    )
    vegetation_outline_budget = {
        "policy": vegetation_outline_policy,
        "target_field_density_mm_per_mm2": density_target,
        "configured_reserve_density_mm_per_mm2": (
            configured_vegetation_reserve_density
        ),
        "requested_outline_reserve_mm": round(vegetation_outline_reserve_mm, 6),
        "field_area_mm2": round(field_area_mm2, 6),
        "baseline_pen_down_mm_excluding_vegetation_outlines": round(
            vegetation_baseline_length_mm, 6
        ),
        "available_vegetation_outline_mm": round(vegetation_available_length_mm, 6),
        "candidate_vegetation_outline_mm": round(
            retained_vegetation_outline_mm + omitted_vegetation_outline_mm, 6
        ),
        "retained_vegetation_outline_mm": round(retained_vegetation_outline_mm, 6),
        "omitted_vegetation_outline_mm": round(omitted_vegetation_outline_mm, 6),
        "mandatory_zero_symbol_outline_mm": round(mandatory_vegetation_outline_mm, 6),
        "mandatory_outline_overage_mm": round(mandatory_outline_overage_mm, 6),
        "candidate_feature_ids": [
            candidate.feature_id for candidate in vegetation_outline_candidates
        ],
        "retained_feature_ids": [
            candidate.feature_id for candidate in retained_vegetation_outlines
        ],
        "omitted_feature_ids": [
            candidate.feature_id for candidate in omitted_vegetation_outlines
        ],
        "candidate_feature_count": len(vegetation_outline_candidates),
        "retained_feature_count": len(retained_vegetation_outlines),
        "omitted_feature_count": len(omitted_vegetation_outlines),
        "mandatory_zero-symbol_feature_ids": [
            candidate.feature_id for candidate in mandatory_vegetation_outlines
        ],
        "mandatory_zero-symbol_feature_count": len(mandatory_vegetation_outlines),
        "candidate_features": [
            {
                "feature_id": candidate.feature_id,
                "kind": candidate.kind,
                "named": candidate.named,
                "paper_area_mm2": candidate.paper_area_mm2,
                "outline_length_mm": round(candidate.length_mm, 6),
                "interior_symbol_count": candidate.interior_symbol_count,
                "decision": (
                    "retained"
                    if candidate.feature_id in retained_vegetation_ids
                    else "omitted"
                ),
            }
            for candidate in vegetation_outline_candidates
        ],
        "whole_feature_groups_only": True,
        "interior_symbols_retained_independently": (
            vegetation_outline_policy != "outline-only-density-budgeted-source-boundary"
        ),
        "vegetation_interior_pattern": (
            "none-outline-only"
            if vegetation_outline_policy
            == "outline-only-density-budgeted-source-boundary"
            else "physical-source-contained-symbols"
        ),
        "retention_rank_recipe": [
            "mandatory-zero-symbol-first",
            "named-before-unnamed",
            "woodland-before-grass",
            "stable-feature-id",
            "whole-group-must-fit",
        ],
        "projected_field_density_mm_per_mm2": round(
            (vegetation_baseline_length_mm + retained_vegetation_outline_mm)
            / field_area_mm2,
            9,
        ),
    }

    # Reconcile the emitted counters after the density pass; pre-budget counts
    # would otherwise describe source records that were intentionally omitted.
    selected_context_kind = {
        str(feature["id"]): str(feature["_render_kind"]) for feature in selected_context
    }
    context_output_counts = {}
    water_dot_count = 0
    vegetation_symbol_count = 0
    runoff_hatch_count = 0
    gravel_dot_count = 0
    for artwork_layer in layers.values():
        for record in artwork_layer.records:
            feature_id = str(record.attributes.get("data-feature-id") or "")
            selected_kind = selected_context_kind.get(feature_id)
            if (
                selected_kind is not None
                and record.attributes.get("data-context-kind") == selected_kind
            ):
                context_output_counts[selected_kind] = (
                    context_output_counts.get(selected_kind, 0) + 1
                )
            water_dot_count += record.role == "water-stipple-dot"
            vegetation_symbol_count += record.role in {
                "woodland-symbol",
                "grass-symbol",
            }
            runoff_hatch_count += record.role == "runoff-hatch"
            gravel_dot_count += record.role == "gravel-stipple-dot"

    travel_start = (context.field.left, context.field.top)
    travel_optimisation = {
        layer_id: _optimise_layer_travel(value, start=travel_start)
        for layer_id, value in layers.items()
        if value.records
    }

    station_class_counts = {
        station_class: sum(value == station_class for value in station_classes.values())
        for station_class in (
            "geometric",
            "official-turn",
            "source-backed-apex",
            "source-tagged",
        )
    }
    geometric_station_count = station_class_counts["geometric"]
    official_turn_count = station_class_counts["official-turn"]
    true_apex_count = station_class_counts["source-backed-apex"]
    source_tagged_station_count = station_class_counts["source-tagged"]
    claim_parts: list[str] = []
    if geometric_station_count:
        claim_parts.append("G GEOMETRIC STATIONS / NOT OFFICIAL TURNS OR APEXES")
    if official_turn_count:
        claim_parts.append("T SOURCE-BACKED OFFICIAL TURNS")
    if true_apex_count:
        claim_parts.append("A SOURCE-BACKED TRUE APEXES")
    if source_tagged_station_count:
        claim_parts.append("S SOURCE-TAGGED STATIONS / NOT OFFICIAL TURNS OR APEXES")
    station_claim = (
        " | ".join(claim_parts)
        if claim_parts
        else "TURN STATIONS WITHHELD / NOT INFERRED"
    )
    title, document_title = _compact_title(checked, context)
    subtitle_zone = context.zones["subtitle"]
    detail_zone = context.zones["detail"]
    detail_nib_role = str(context.plate["type_nib_role"]["detail"])
    detail_nib_mm = float(context.plate["nib_roles_mm"][detail_nib_role])
    detail_pen_id = {
        0.25: "black-0-25",
        0.4: "black-0-4",
        0.6: "black-0-6",
        1.0: "black-1",
    }[detail_nib_mm]
    detail_preferred_cap_mm = float(context.plate["type_scale_mm"]["detail"])
    copy_minimum = max(
        8.0 * copy_layer.pen.mark_width_mm,
        8.0 * detail_nib_mm,
    )
    location = (
        checked.get("host_city")
        or checked.get("location")
        or checked["circuit"].get("location_label")
        or checked.get("host_country_iso2")
    )
    if isinstance(location, dict):
        location = location.get("city") or location.get("label")
    location_text = str(location or checked.get("host_country_iso2") or "CIRCUIT")
    configuration = str(
        checked["circuit"].get("configuration_id", "GP-LAYOUT")
    ).replace("-", " ")
    configuration_identity_value = checked.get("configuration_identity")
    configuration_identity: Mapping[str, Any] = (
        configuration_identity_value
        if isinstance(configuration_identity_value, Mapping)
        else {}
    )
    configuration_identity_status = str(
        configuration_identity.get("status", "current-season-calendar")
    )
    render_disclosure = str(checked.get("render_disclosure") or "").strip()
    if configuration_identity_status == "current-source-f1-reference":
        legacy_subtitle_candidates = (
            *(
                (f"CURRENT COURSE STUDY / F1 REF {season}",)
                if centreline_only
                else ()
            ),
            render_disclosure,
            f"{location_text} / CURRENT COURSE / F1 REF {season}",
            f"CURRENT COURSE STUDY / F1 REF {season}",
            f"CURRENT COURSE / F1 REF {season}",
        )
    elif configuration_identity_status == "exact-historic-source":
        legacy_subtitle_candidates = (
            *((f"HISTORIC COURSE STUDY / F1 {season}",) if centreline_only else ()),
            render_disclosure,
            f"{location_text} / HISTORIC COURSE / F1 {season}",
            f"HISTORIC COURSE STUDY / F1 {season}",
            f"HISTORIC COURSE / F1 REF {season}",
        )
    elif configuration_identity_status == "current-surviving-equivalent":
        legacy_subtitle_candidates = (
            *((f"SURVIVING COURSE STUDY / F1 {season}",) if centreline_only else ()),
            render_disclosure,
            f"{location_text} / SURVIVING COURSE / F1 REF {season}",
            f"SURVIVING COURSE STUDY / F1 REF {season}",
            f"SURVIVING COURSE / F1 REF {season}",
        )
    else:
        legacy_subtitle_candidates = ()
    subtitle = _compact_line(
        (
            *legacy_subtitle_candidates,
            *(
                (
                    f"{location_text} / COURSE STUDY / {season}",
                    f"CURRENT COURSE STUDY / {season}",
                )
                if centreline_only
                else ()
            ),
            f"{location_text} / {configuration} / {season}",
            f"{configuration} / {season}",
            f"CIRCUIT STUDY / {season}",
        ),
        subtitle_zone,
        copy_minimum,
    )

    official_length: float | None = None
    official = geometry_record.get("official_centreline_length_m")
    if isinstance(official, dict) and isinstance(official.get("value"), (int, float)):
        official_length = float(official["value"])
    elif isinstance(checked["circuit"].get("lap_length_m"), (int, float)):
        official_length = float(checked["circuit"]["lap_length_m"])
    display_length = (
        official_length if official_length is not None else lap_source.length
    )
    official_facts_value = checked.get("official_facts")
    official_facts: Mapping[str, Any] = (
        official_facts_value if isinstance(official_facts_value, Mapping) else {}
    )
    first_grand_prix = official_facts.get("first_grand_prix")
    fastest_lap_value = official_facts.get("fastest_lap")
    fastest_lap: Mapping[str, Any] = (
        fastest_lap_value
        if isinstance(fastest_lap_value, Mapping)
        else {
            "status": "withheld",
            "withheld_reason": "configuration-matched-official-fact-not-frozen",
        }
    )
    length_source_ref = (
        str(official.get("source_ref"))
        if isinstance(official, dict) and official.get("source_ref")
        else str(official_facts.get("source_ref") or "")
    )
    facts_source_ref = str(official_facts.get("source_ref") or "")
    missing_capability_copy = " / ".join(
        label
        for missing, label in (
            (sf is None, "S/F WITHHELD"),
            (not turn_stations, "TURNS WITHHELD"),
            (not direction_is_sourced, "DIRECTION WITHHELD"),
            (not pit_sources, "PIT WITHHELD"),
        )
        if missing
    )
    study_information_value = checked.get("study_information")
    study_information: Mapping[str, Any] = (
        study_information_value
        if isinstance(study_information_value, Mapping)
        else {}
    )
    study_history_value = study_information.get("history")
    study_history: Mapping[str, Any] = (
        study_history_value if isinstance(study_history_value, Mapping) else {}
    )
    study_edition_value = study_information.get("edition")
    study_edition: Mapping[str, Any] = (
        study_edition_value if isinstance(study_edition_value, Mapping) else {}
    )
    first_detail_candidates = (
        (
            f"LENGTH {display_length / 1000.0:.3f} KM / "
            f"{str((study_history.get('lines') or ['COURSE STUDY'])[0])}",
            f"{display_length / 1000.0:.3f} KM / COURSE STUDY",
        )
        if motorsport_study
        else
        (
            f"LENGTH {display_length / 1000.0:.3f} KM / FIRST GP {first_grand_prix}",
            f"{display_length / 1000.0:.3f} KM / FIRST GP {first_grand_prix}",
        )
        if isinstance(first_grand_prix, int)
        else (
            f"LENGTH {display_length / 1000.0:.3f} KM / F1 REFERENCE {season}",
            f"{display_length / 1000.0:.3f} KM / F1 REF {season}",
        )
    )
    associative_famous_anchor_count = sum(
        candidate["section"].get("name_status") == FAMOUS_SECTION_NAME_STATUS
        and candidate["section"].get("anchor_mode") == "exact-context-way-near-lap-v1"
        for candidate in named_section_lookup.values()
    )
    legacy_configuration_reference = configuration_identity_status in {
        "current-source-f1-reference",
        "exact-historic-source",
        "current-surviving-equivalent",
    }
    second_detail_candidates = (
        tuple(str(value) for value in study_edition.get("lines", ["COURSE STUDY"]))
        if motorsport_study
        else
        (
            f"{fastest_lap['time']} / {fastest_lap['driver']} / "
            f"{fastest_lap['season']}",
            f"{fastest_lap['time']} / {fastest_lap['driver']}",
        )
        if fastest_lap.get("status") == "source-backed"
        else (
            f"CURRENT COURSE STUDY / F1 REFERENCE {season}",
            f"COURSE STUDY / F1 REF {season}",
        )
    )
    third_detail_candidates = (
        (
            "SOURCE CENTRELINE COURSE STUDY",
            "COURSE CENTRELINE STUDY",
        )
        if centreline_only and legacy_configuration_reference
        else (
            "SOURCE CENTRELINE COURSE STUDY",
            "COURSE CENTRELINE STUDY",
        )
        if centreline_only
        else (
            (
                "SOURCE CENTRELINE / DIAGRAMMATIC CORRIDOR",
                "CENTRELINE / DIAGRAMMATIC CORRIDOR",
            )
            if associative_famous_anchor_count
            else (
                "SOURCE CENTRELINE / DIAGRAMMATIC CORRIDOR",
                "CENTRELINE / DIAGRAMMATIC CORRIDOR",
            )
        )
    )
    details = tuple(
        _compact_line(
            candidates,
            detail_zone,
            copy_minimum,
            allow_horizontal_condense=True,
            condense_pen_id=detail_pen_id,
            preferred_cap_mm=detail_preferred_cap_mm,
        )
        for candidates in (
            first_detail_candidates,
            second_detail_candidates,
            third_detail_candidates,
        )
    )
    course_facts = {
        "policy": "source-backed-information-rail-v2",
        "length": {
            "status": "source-backed" if official_length is not None else "derived",
            "value_m": round(display_length, 3),
            "display_copy": f"{display_length / 1000.0:.3f} KM",
            "source_ref": length_source_ref or None,
        },
        "first_grand_prix": {
            "status": (
                "source-backed" if isinstance(first_grand_prix, int) else "withheld"
            ),
            "year": first_grand_prix if isinstance(first_grand_prix, int) else None,
            "source_ref": facts_source_ref or None,
            "scope": "formula1-official-page-first-grand-prix-venue-fact",
        },
        "fastest_lap": copy.deepcopy(fastest_lap),
        "configuration_reference_season": season,
        "summary_lines": list(details),
        "diagrammatic_course_disclosure_visible": True,
        "full_driver_copy_preserved": (
            fastest_lap.get("status") != "source-backed"
            or str(fastest_lap.get("driver") or "").upper()
            in details[1].upper()
        ),
    }

    used_source_refs = sorted(_find_source_refs(checked))
    resolved_sources = [
        registry[source_ref]
        for source_ref in used_source_refs
        if source_ref in registry
    ]
    if not resolved_sources:
        # Direct programmatic smoke records may carry their own complete source
        # registry.  Catalog-backed builds always take the branch above.
        inline = checked.get("sources", [])
        if isinstance(inline, list):
            resolved_sources = [
                copy.deepcopy(item) for item in inline if isinstance(item, dict)
            ]
    osm_credit_line = (
        "OSM CONTRIBUTORS / OPENSTREETMAP.ORG/COPYRIGHT"
        if any(
            "openstreetmap" in json.dumps(source, sort_keys=True).casefold()
            or str(source.get("licence", source.get("license", "")))
            .casefold()
            .startswith("odbl")
            for source in resolved_sources
        )
        else "SOURCE CREDITS IN MANIFEST / REVIEW ONLY"
    )
    grandstand_emitted_path_count = int(context_output_counts.get("grandstand", 0))
    grandstand_source_feature_ids = sorted(
        str(feature["id"])
        for feature in model.get("context", [])
        if str(feature.get("kind", "")).casefold().replace("_", "-") == "grandstand"
    )
    grandstand_selected_feature_ids = sorted(
        str(feature["id"])
        for feature in selected_context
        if str(feature.get("_render_kind")) == "grandstand"
    )
    grandstand_emitted_feature_ids = sorted(
        {
            str(record.attributes.get("data-feature-id"))
            for artwork_layer in layers.values()
            for record in artwork_layer.records
            if record.role == "context-grandstand"
            and record.attributes.get("data-feature-id")
        }
    )
    grandstand_source_unselected_feature_ids = sorted(
        set(grandstand_source_feature_ids) - set(grandstand_selected_feature_ids)
    )
    grandstand_culled_feature_ids = sorted(
        set(grandstand_selected_feature_ids) - set(grandstand_emitted_feature_ids)
    )
    visible_context_disclosure: str | None = None
    if configuration_identity_status == "exact-historic-source":
        visible_context_disclosure = (
            HISTORIC_CURRENT_STAND_VISIBLE_DISCLOSURE
            if grandstand_emitted_path_count
            else HISTORIC_CURRENT_CONTEXT_VISIBLE_DISCLOSURE
        )
    elif grandstand_emitted_path_count:
        visible_context_disclosure = CURRENT_OSM_GRANDSTAND_VISIBLE_DISCLOSURE
    # Current-context scope belongs with the map specification, not beside the
    # legal source credit. This keeps the rail editorial and leaves one quiet,
    # mandatory ODbL credit at the foot of the sheet.
    credit_line = osm_credit_line
    direction_copy = (
        direction_value.replace("counter-clockwise", "ANTI-CLOCKWISE").upper()
        if direction_is_sourced
        else "NORTH-UP COURSE PLAN"
    )
    history_lines = [
        (
            f"FIRST GRAND PRIX {first_grand_prix}"
            if isinstance(first_grand_prix, int)
            else f"F1 REFERENCE {season}"
        )
    ]
    if isinstance(first_grand_prix, int):
        history_lines.append(f"SEASON {season} REFERENCE")

    if motorsport_study:
        raw_history_lines = study_history.get("lines")
        history_lines = (
            [str(value) for value in raw_history_lines]
            if isinstance(raw_history_lines, list)
            else ["MOTORSPORT COURSE STUDY"]
        )
        raw_edition_lines = study_edition.get("lines")
        record_group = {
            "id": "edition",
            "label": str(study_edition.get("label") or "EDITION"),
            "lines": (
                [str(value) for value in raw_edition_lines]
                if isinstance(raw_edition_lines, list)
                else ["CURRENT COURSE STUDY"]
            ),
        }
    elif fastest_lap.get("status") == "source-backed":
        record_group = {
            "id": "record",
            "label": "FASTEST LAP",
            "lines": [
                f"{fastest_lap['time']} / {fastest_lap['season']}",
                str(fastest_lap["driver"]).upper(),
            ],
        }
    else:
        edition_copy = {
            "current-source-f1-reference": "CURRENT COURSE STUDY",
            "exact-historic-source": "HISTORIC COURSE STUDY",
            "current-surviving-equivalent": "SURVIVING COURSE STUDY",
        }.get(
            configuration_identity_status,
            "COURSE CENTRELINE STUDY" if centreline_only else "CIRCUIT ATLAS",
        )
        record_group = {
            "id": "edition",
            "label": "EDITION",
            "lines": [edition_copy, f"F1 REFERENCE {season}"],
        }

    drawing_context = visible_context_disclosure or (
        f"G01-G{geometric_station_count:02d} / GEOMETRIC"
        if geometric_station_count
        else "NORTH-UP COURSE PLAN"
    )
    information_groups = (
        {
            "id": "course",
            "label": "COURSE",
            "lines": [f"{display_length / 1000.0:.3f} KM", direction_copy],
        },
        {
            "id": "history",
            "label": (
                str(study_history.get("label") or "MOTORSPORT")
                if motorsport_study
                else "FORMULA 1"
            ),
            "lines": history_lines,
        },
        record_group,
        {
            "id": "drawing",
            "label": "COURSE DRAWING" if centreline_only else "DIAGRAMMATIC COURSE",
            "lines": ["SOURCE CENTRELINE", drawing_context],
        },
    )
    course_facts["visible_groups"] = copy.deepcopy(list(information_groups))
    course_facts["visible_lines"] = [
        copy_value
        for group in information_groups
        for copy_value in [str(group["label"]), *group["lines"]]
    ]
    course_facts["full_driver_copy_preserved"] = (
        fastest_lap.get("status") != "source-backed"
        or str(fastest_lap.get("driver") or "").upper()
        in " ".join(course_facts["visible_lines"]).upper()
    )

    label_metadata = [
        {
            "id": placement.id,
            "copy": placement.copy,
            "role": placement.role,
            "source_ref": placement.source_ref,
            "bounds_mm": placement.bounds.as_dict(),
            "anchor_mm": [round(value, 6) for value in placement.anchor],
            "leader_mm": (
                [
                    [round(value, 6) for value in point_value]
                    for point_value in placement.leader
                ]
                if placement.leader is not None
                else None
            ),
            "displayed_name": placement.displayed_name,
            "cap_height_mm": round(placement.cap_mm, 6),
            "feature_id": placement.feature_id,
            "source_object_id": placement.source_object_id,
            "source_name_key": placement.source_name_key,
            "source_copy": placement.source_copy,
            "copy_policy_id": placement.copy_policy_id,
            "normalisation_policy_id": placement.normalisation_policy_id,
            "display_punctuation_policy_id": (placement.display_punctuation_policy_id),
            "station_class": (
                station_classes.get(placement.id)
                if placement.role == "turn-label"
                else None
            ),
        }
        for placement in placements
    ]
    for label_record in label_metadata:
        if label_record["role"] != "section-label":
            continue
        section_candidate = named_section_lookup[str(label_record["id"])]
        section = section_candidate["section"]
        if section.get("name_status") != FAMOUS_SECTION_NAME_STATUS:
            continue
        anchor_source_ref = str(
            section.get("anchor_source_ref") or section["source_ref"]
        )
        label_record.update(
            {
                "name_status": str(section["name_status"]),
                "official_course_name": True,
                "name_source_ref": str(
                    section.get("name_source_ref") or section["source_ref"]
                ),
                "anchor_source_ref": anchor_source_ref,
                "anchor_source_object_id": _source_object_attribute(
                    section_candidate["group_source_objects"],
                    anchor_source_ref,
                ),
                "anchor_mode": str(
                    section.get("anchor_mode") or "exact-selected-osm-way-v1"
                ),
                "anchor_status": str(
                    section.get("anchor_status")
                    or "exact-selected-osm-way-name-and-geometry"
                ),
                "course_section_priority": int(section_candidate["priority"]),
                "claim_scope": str(section["claim_scope"]),
            }
        )
    emitted_named_section_records = [
        candidate["section"] for candidate in named_section_lookup.values()
    ]
    named_section_status_counts: dict[str, int] = {}
    for section in emitted_named_section_records:
        status = str(section["name_status"])
        named_section_status_counts[status] = (
            named_section_status_counts.get(status, 0) + 1
        )
    official_named_section_count = named_section_status_counts.get(
        FAMOUS_SECTION_NAME_STATUS, 0
    )
    minimum_label_track_clearance = min(
        (
            _rect_polygon(placement.bounds).distance(lap_page)
            for placement in placements
            if placement.role == "turn-label"
        ),
        default=None,
    )
    density_layers = {
        layer_id: {
            "pen_id": value.pen_id,
            "path_count": len(value.records),
            "pen_down_mm": round(
                sum(
                    _serialized_polyline_length_mm(record.points)
                    for record in value.records
                ),
                6,
            ),
            "ink_mm2_upper_bound": round(
                sum(
                    _serialized_polyline_length_mm(record.points)
                    for record in value.records
                )
                * value.pen.mark_width_mm,
                6,
            ),
        }
        for layer_id, value in layers.items()
        if value.records
    }
    selected_context_counts: dict[str, int] = {}
    for feature in selected_context:
        kind = str(feature["_render_kind"])
        selected_context_counts[kind] = selected_context_counts.get(kind, 0) + 1

    source_inventory = {
        "catalog_id": checked_catalog.get("catalog_id") if checked_catalog else None,
        "catalog_season": catalog_release_season,
        "configuration_reference_season": season,
        "catalog_freeze": copy.deepcopy(checked_catalog.get("freeze"))
        if checked_catalog
        else None,
        "used_source_refs": used_source_refs,
        "resolved_source_refs": sorted(
            source["id"] for source in resolved_sources if "id" in source
        ),
        "unresolved_source_refs": sorted(set(used_source_refs) - set(registry))
        if registry
        else [],
        "lap_source_object_count": len(model["lap_source_objects"]),
        "pit_lane_source_object_count": _source_lineage_count(
            model.get("pit_lanes", [])
        ),
        "context_source_object_count": _source_lineage_count(model.get("context", [])),
    }
    topology = {
        "lap_geometry_type": "LineString",
        "closed": True,
        "closure_gap_m": round(
            Point(lap_source.coords[0]).distance(Point(lap_source.coords[-1])), 9
        ),
        "measured_length_m": round(lap_source.length, 6),
        "official_length_m": official_length,
        "official_length_discrepancy_percent": (
            round(abs(lap_source.length - official_length) / official_length * 100.0, 6)
            if official_length and official_length > 0.0
            else None
        ),
        "self_crossing_or_grade_separation_review_required": not lap_source.is_simple,
        "lap_self_intersection_count": len(lap_self_crossings),
        "lap_self_intersection_segment_indexes": [
            list(value["segment_indexes"]) for value in lap_self_crossings
        ],
        "grade_separation_source_section_ids": (grade_separation_source_section_ids),
        "grade_separation_cue_required": grade_separation_cue_required,
        "grade_separation_cue_policy": GRADE_SEPARATION_CUE_POLICY,
        "grade_separation_cue_pen_id": (
            copy_layer.pen_id if grade_separation_cue_required else None
        ),
        "grade_separation_cue_emitted_path_count": (grade_separation_cue_emitted),
        "red_centreline_modified_for_grade_separation_cue": False,
        "white_ink_used_for_grade_separation_cue": False,
        "pit_lane_count": len(pit_sources),
        "lap_direction": direction_value if direction_is_sourced else None,
        "lap_direction_status": (
            "source-backed" if direction_is_sourced else "withheld"
        ),
        "lap_direction_source_ref": (
            str(direction_source_ref) if direction_is_sourced else None
        ),
        "direction_arrow_count": arrow_count,
        "start_finish_distance_from_lap_m": (
            round(Point(sf["point"]).distance(lap_source), 6)
            if isinstance(sf, dict)
            else None
        ),
        "start_finish_status": "source-backed" if isinstance(sf, dict) else "withheld",
        "turn_station_count_source": len(turn_stations),
        "turn_station_count_emitted": sum(
            placement.role == "turn-label" for placement in placements
        ),
        "station_numbers_complete": bool(turn_stations),
        "available_station_inventory_complete": True,
        "turn_numbers_complete": (
            bool(turn_stations)
            and official_turn_count + true_apex_count == len(turn_stations)
        ),
        "geometric_turn_station_count": geometric_station_count,
        "official_turn_station_count": official_turn_count,
        "true_source_apex_count": true_apex_count,
        "source_tagged_station_count": source_tagged_station_count,
        "station_class_counts": station_class_counts,
        "turn_station_claim": station_claim,
        "red_layer_claim": (
            "one exact continuous sourced LAP CENTRELINE plus paired "
            "diagrammatic course-corridor offsets"
        ),
        "diagrammatic_corridor_claimed": True,
        "surveyed_track_width_claimed": False,
        "racing_line_claimed": False,
    }
    structural_page_bounds = transform_value.geometry(unary_union(structural)).bounds
    structural_width_utilization = (
        (structural_page_bounds[2] - structural_page_bounds[0])
        / transform_value.working_rect_mm.width
    )
    structural_height_utilization = (
        (structural_page_bounds[3] - structural_page_bounds[1])
        / transform_value.working_rect_mm.height
    )
    paper_adaptation = {
        "format_id": format_id,
        "variant_identity_includes_format": True,
        "orientation": context.plate["orientation"],
        "north_up": True,
        "rotation_deg": 0,
        "context_mode": mode,
        "context_mode_source": (
            "event.circuit.atlas_context_mode"
            if checked["circuit"].get("atlas_context_mode") is not None
            else "legacy-site-type-fallback"
        ),
        "context_mode_derived_from_site_type": (
            checked["circuit"].get("atlas_context_mode") is None
        ),
        "context_mode_override_applied": False,
        "site_type": checked["circuit"].get("site_type"),
        "sheet_gate": copy.deepcopy(gate),
        "scale_mm_per_m": round(transform_value.scale_mm_per_m, 9),
        "approximate_scale_denominator": round(1000.0 / transform_value.scale_mm_per_m),
        "framing_source_scope": "lap-plus-pit-only-unqualified-boundaries-excluded-v2",
        "framing_fit_policy": "maximum-safe-contain-no-geographic-margin-v1",
        "structural_source_bounds_m": [round(value, 6) for value in structural_bounds],
        "unqualified_raw_boundary_count_excluded_from_framing": len(boundary_sources),
        "source_bounds_m": [round(value, 6) for value in source_bounds],
        "context_viewport_source_bounds_m": [
            round(value, 6) for value in context_viewport_bounds
        ],
        "course_edge_clearance_mm": float(gate["field_padding_mm"]),
        "working_rect_mm": transform_value.working_rect_mm.as_dict(),
        "structural_bounds_mm": [round(value, 6) for value in structural_page_bounds],
        "structural_width_utilization": round(structural_width_utilization, 9),
        "structural_height_utilization": round(structural_height_utilization, 9),
        "maximum_safe_axis_utilization": round(
            max(structural_width_utilization, structural_height_utilization), 9
        ),
        "hero_bounds_mm": [round(value, 6) for value in lap_page.bounds],
        "hero_width_utilization": round(
            (lap_page.bounds[2] - lap_page.bounds[0])
            / transform_value.working_rect_mm.width,
            9,
        ),
        "hero_height_utilization": round(
            (lap_page.bounds[3] - lap_page.bounds[1])
            / transform_value.working_rect_mm.height,
            9,
        ),
        "turn_name_source_count": sum(
            bool(station.get("name")) for station in turn_stations
        ),
        "turn_name_emitted_count": sum(
            placement.role == "turn-label" and placement.displayed_name
            for placement in placements
        ),
        "turn_name_omissions": name_omissions,
        "context_name_emitted_count": context_label_count,
    }
    active_pen_ids = {
        artwork_layer.pen_id
        for artwork_layer in layers.values()
        if artwork_layer.records
    }
    furniture_widths = {
        float(context.plate["nib_roles_mm"][role])
        for role in {
            str(context.plate["type_nib_role"]["title"]),
            str(context.plate["type_nib_role"]["detail"]),
            str(context.plate["type_nib_role"]["attribution"]),
        }
    }
    furniture_pen_ids = {
        {0.25: "black-0-25", 0.4: "black-0-4", 0.6: "black-0-6", 1.0: "black-1"}[width]
        for width in furniture_widths
    }
    active_pen_ids.update(furniture_pen_ids)
    document_pen_order = [pen_id for pen_id in F1_PENS if pen_id in active_pen_ids]
    format_furniture_pen_order = [
        pen_id for pen_id in FURNITURE_PEN_ORDER if pen_id in furniture_pen_ids
    ]
    shared_field_and_furniture_pen_order = [
        pen_id for pen_id in format_furniture_pen_order if pen_id in FIELD_PEN_ORDER
    ]
    additional_furniture_pen_order = [
        pen_id for pen_id in format_furniture_pen_order if pen_id not in FIELD_PEN_ORDER
    ]
    f1_rendering = {
        "geometry_sha256": geometry_digest,
        "event_id": checked["id"],
        "circuit_id": checked["circuit"]["id"],
        "configuration_id": checked["circuit"].get("configuration_id"),
        "format_id": format_id,
        "pen_order": document_pen_order,
        "field_pen_order": list(FIELD_PEN_ORDER),
        "format_furniture_pen_order": format_furniture_pen_order,
        "shared_field_and_furniture_pen_order": (shared_field_and_furniture_pen_order),
        "additional_furniture_pen_order": additional_furniture_pen_order,
        "document_pen_order": document_pen_order,
        "field_pen_semantics": {
            pen_id: list(roles) for pen_id, roles in FIELD_PEN_SEMANTICS.items()
        },
        "circuit_atlas_schema": "circuit-atlas-rendering/v2",
        "atlas_variant": "north-up-atlas",
        "geometry_qualification": {
            "status": geometry_status,
            "catalog_class": catalog_class,
            "catalog_release_season": catalog_release_season,
            "configuration_reference_season": season,
            "configuration_identity_status": configuration_identity_status,
            "render_disclosure": render_disclosure or None,
            "visible_subtitle": subtitle,
            "centreline_only": centreline_only,
            "missing_start_finish": sf is None,
            "missing_turn_stations": not turn_stations,
            "missing_pit_lane": not pit_sources,
            "direction_withheld": not direction_is_sourced,
            "course_scope_visibly_stated": True,
            "omissions_visibly_disclosed": False,
        },
        "diagrammatic_course_corridor": corridor_metadata,
        "course_facts": course_facts,
        "topology": topology,
        "source_inventory": source_inventory,
        "context_features": {
            "mode": mode,
            "mode_source": paper_adaptation["context_mode_source"],
            "mode_derived_from_site_type": paper_adaptation[
                "context_mode_derived_from_site_type"
            ],
            "mode_override_applied": False,
            "site_type": checked["circuit"].get("site_type"),
            "outline_clip_policy": "source-boundary-first-no-crop-edge-v1",
            "vegetation_outline_policy": str(gate["vegetation_outline_policy"]),
            "vegetation_outline_budget": vegetation_outline_budget,
            "input_counts_by_kind": input_context_counts,
            "selected_counts_by_kind": selected_context_counts,
            "emitted_path_counts_by_kind": context_output_counts,
            "water_stipple_dot_count": water_dot_count,
            "vegetation_symbol_count": vegetation_symbol_count,
            "runoff_hatch_count": runoff_hatch_count,
            "gravel_stipple_dot_count": gravel_dot_count,
            "digital_fills_used": False,
            "white_ink_used": False,
            "track_boundary_source_count": len(boundary_sources),
            "track_boundary_lap_associated_source_count": sum(
                bool(value["lap_associated"]) for value in boundary_qualifications
            ),
            "track_boundary_emitted_feature_count": boundary_emitted_feature_count,
            "track_boundary_emitted_path_count": boundary_path_count,
            "track_boundary_minimum_clearance_mm": boundary_clearance_mm,
            "track_boundary_qualifications": boundary_qualifications,
            "pit_lane_count": len(pit_sources),
            "special_section_path_count": special_emitted,
            "grandstand_observation": {
                "policy": "frozen-current-osm-footprint-only-v1",
                "source_record_count": int(input_context_counts.get("grandstand", 0)),
                "selected_feature_count": int(
                    selected_context_counts.get("grandstand", 0)
                ),
                "emitted_feature_count": len(grandstand_emitted_feature_ids),
                "emitted_path_count": grandstand_emitted_path_count,
                "source_feature_ids": grandstand_source_feature_ids,
                "selected_feature_ids": grandstand_selected_feature_ids,
                "emitted_feature_ids": grandstand_emitted_feature_ids,
                "source_unselected_feature_ids": (
                    grandstand_source_unselected_feature_ids
                ),
                "culled_feature_ids": grandstand_culled_feature_ids,
                "partition_policy": (
                    "source=selected+unselected; selected=emitted+culled; "
                    "every non-emitted id requires feature_omissions evidence"
                ),
                "claim_scope": CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE,
                "event_configuration_verified": False,
                "fia_configuration_claimed": False,
                "operational_semantics_claimed": False,
                "visible_disclosure": (
                    visible_context_disclosure
                    if grandstand_emitted_path_count
                    else None
                ),
            },
            "context_density_budget": context_density_budget,
        },
        "labels": {
            "policy": (
                "complete-available-station-inventory-no-inference-v2"
                if centreline_only
                else "complete-station-inventory-collision-solved-no-silent-cap"
            ),
            "context_copy_policy": {
                "policy_id": CONTEXT_LABEL_COPY_POLICY_ID,
                "source_key_precedence": list(CONTEXT_LABEL_SOURCE_KEYS),
                "normalisation_policy_id": TEXT_NORMALISATION_POLICY_ID,
                "display_punctuation_policy_id": (
                    CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID
                ),
                "display_punctuation_rules": [
                    "backslash-delimiter-to-spaced-solidus",
                    "trim-orphan-terminal-comma-semicolon-colon",
                    "preserve-other-internal-punctuation",
                ],
                "unsupported_script_policy": (
                    "omit-whole-label-without-drawable-sourced-alternative"
                ),
                "invented_translation_allowed": False,
            },
            "placements": label_metadata,
            "turn_station_source_count": len(turn_stations),
            "turn_station_emitted_count": sum(
                placement.role == "turn-label" for placement in placements
            ),
            "label_box_overlap_count": 0,
            "leader_crossing_count": 0,
            "name_omissions": name_omissions,
            "named_course_sections": {
                "source_record_count": len(named_course_sections),
                "unique_source_name_count": len(available_named_sections),
                "paper_label_limit": section_limit,
                "selected_count": len(selected_named_sections),
                "emitted_count": len(named_section_lookup),
                "emitted_names": sorted(emitted_section_names),
                "omissions": named_section_copy_omissions,
                "name_status": (
                    FAMOUS_SECTION_NAME_STATUS
                    if official_named_section_count
                    and official_named_section_count == len(named_section_lookup)
                    else (
                        "mixed-official-and-osm-source-copy"
                        if official_named_section_count
                        else "osm-source-tagged-unverified-not-official"
                    )
                ),
                "official_course_name_claimed": bool(official_named_section_count),
                "priority": (
                    "official-source-priority-then-spatial-distribution"
                    if official_named_section_count
                    else "before-ordinary-context-copy"
                ),
                **(
                    {
                        "name_status_counts": named_section_status_counts,
                        "official_source_copy_count": official_named_section_count,
                        "associative_anchor_count": associative_famous_anchor_count,
                        "associative_anchor_disclosure_visible": False,
                        "associative_anchor_disclosure_copy": None,
                    }
                    if official_named_section_count
                    else {}
                ),
                "minimum_section_label_separation_mm": round(
                    named_section_separation_mm, 6
                ),
            },
            "minimum_turn_label_track_clearance_mm": (
                round(minimum_label_track_clearance, 6)
                if minimum_label_track_clearance is not None
                else None
            ),
            "field_label_pen_id": copy_layer.pen_id,
            "field_label_cap_height_mm": label_cap,
            "field_label_physical_floor_mm": (8.0 * copy_layer.pen.mark_width_mm),
        },
        "track_clearance": {
            "host_road_halo_role": "host-road-halo",
            "host_road_halo_emitted_as_path": False,
            "host_road_suppression": "source-space-subtraction-no-white-ink",
            "halo_mm": float(gate["track_halo_mm"]),
            "road_length_before_suppression_mm": round(road_before_mm, 6),
            "road_length_removed_mm": round(road_suppression_mm, 6),
            "red_context_overdraw_count": 0,
            "intentional_red_intersections": [
                "start-finish",
                "pit-entry",
                "pit-exit",
                *(
                    ["source-grade-separation-cue"]
                    if grade_separation_cue_required
                    else []
                ),
            ],
        },
        "density_inputs": {
            "field_area_mm2": round(field_area_mm2, 6),
            "layers": density_layers,
            "renderer_field_density_target_mm_per_mm2": density_target,
            "target_field_ink_coverage": [0.04, 0.12],
            "hard_field_ink_coverage": 0.15,
        },
        "travel_optimisation": {
            "algorithm": "deterministic-nearest-neighbour-v1",
            "source_order_exceptions": [
                "lap-centreline",
                "pit-lane",
                "sequenced-records",
                "exact-vector-paths",
            ],
            "layers": travel_optimisation,
        },
        "feature_omissions": omissions,
        "paper_adaptation": paper_adaptation,
        "branding": {
            "logos_used": False,
            "official_event_trade_dress_used": False,
            "neutral_cartographic_title_only": True,
        },
    }
    rendering_metadata = {"f1_circuit": f1_rendering}

    # The full normalized model can contain thousands of context candidates.
    # Repeating it inside every paper-format plot manifest multiplied one
    # source event into tens of megabytes per artifact. The catalog and release
    # index already bind the exact model hash, so retain a review summary here
    # and keep the complete geometry only in the frozen catalog.
    catalog_record = copy.deepcopy(checked)
    catalog_geometry = catalog_record["circuit"]["geometry"]
    catalog_geometry["model"] = {
        "omitted_from_plot_manifest": True,
        "geometry_sha256": geometry_digest,
        "lap_source_object_count": len(model["lap_source_objects"]),
        "pit_lane_count": len(model.get("pit_lanes", [])),
        "track_boundary_count": len(model.get("track_boundaries", [])),
        "context_feature_count": len(model.get("context", [])),
        "turn_station_count": len(turn_stations),
        "special_section_count": len(model.get("special_sections", [])),
    }
    catalog_record["catalog_binding"] = {
        "catalog_id": checked_catalog.get("catalog_id") if checked_catalog else None,
        "season": catalog_release_season,
        "configuration_reference_season": season,
        "catalog_class": catalog_class,
        "freeze": copy.deepcopy(checked_catalog.get("freeze"))
        if checked_catalog
        else None,
    }
    rights_value = checked.get("rights")
    rights_record: Mapping[str, Any] = (
        rights_value if isinstance(rights_value, Mapping) else {}
    )
    return PlateArtwork(
        subject_id=str(checked["id"]),
        variant_id=f"{RENDERING_PRESET}-{format_id}",
        domain=DOMAIN,
        subject_kind="map",
        title=title,
        document_title=document_title,
        subtitle=subtitle,
        details=details,
        credit_line=credit_line,
        scale_status=(
            f"north-up-local-metre-plan-approximately-1:"
            f"{round(1000.0 / transform_value.scale_mm_per_m)}"
        ),
        evidence_status=str(
            geometry_record.get("review_status", geometry_record.get("status"))
        ),
        rights_status="review-required",
        sources=tuple(copy.deepcopy(resolved_sources)),
        context=context,
        layers=list(layers.values()),
        pen_order=F1_PENS,
        artifact_kind=ARTIFACT_KIND,
        rendering_preset=RENDERING_PRESET,
        format_subject_policy=FORMAT_SUBJECT_POLICY,
        source_provider="frozen multi-source circuit catalog",
        source_license="per-source; see source inventory and rights ledger",
        data_snapshot=(
            str(
                checked_catalog["freeze"].get(
                    "frozen_at", checked_catalog["freeze"].get("freeze_date")
                )
            )
            if checked_catalog
            else "source-undated-review"
        ),
        notes=(
            "EXACT LAP CENTRELINE + DIAGRAMMATIC COURSE CORRIDOR",
            "DIAGRAMMATIC CORRIDOR / NOT SURVEYED TRACK WIDTH OR RACING LINE",
            station_claim,
            *(
                (f"CENTRELINE-ONLY / {missing_capability_copy}",)
                if centreline_only
                else ()
            ),
            *((render_disclosure,) if render_disclosure else ()),
            "NO LOGOS OR OFFICIAL EVENT TRADE DRESS",
            "REVIEW ONLY / PHYSICAL PEN CALIBRATION AND RIGHTS CLEARANCE REQUIRED",
        ),
        catalog_record=catalog_record,
        rendering_metadata=rendering_metadata,
        information_groups=information_groups,
        svg_metadata={
            "geometry_sha256": geometry_digest,
            "f1_circuit": {
                "event_id": checked["id"],
                "circuit_id": checked["circuit"]["id"],
                "configuration_id": checked["circuit"].get("configuration_id"),
                "format_id": format_id,
                "geometry_sha256": geometry_digest,
                "geometry_status": geometry_status,
                "claim": (
                    "EXACT LAP CENTRELINE + DIAGRAMMATIC CORRIDOR / "
                    "NOT SURVEYED TRACK WIDTH OR RACING LINE"
                ),
            },
        },
        rights_metadata={
            "commercial_release_authorized": False,
            "review_only": True,
            "calendar_status": checked.get("calendar_status"),
            "geometry_commercial_use_status": rights_record.get(
                "geometry_commercial_use_status", "unresolved"
            ),
            "context_commercial_use_status": rights_record.get(
                "context_commercial_use_status", "unresolved"
            ),
            "catalog_release_gate": rights_record.get("release_gate", "hold"),
            "circuit_or_event_affiliation_claimed": False,
            "logos_or_trade_dress_used": False,
            "broadcast_frames_traced": False,
        },
    )


__all__ = [
    "ARTIFACT_KIND",
    "CATALOG_PATH",
    "CATALOG_SCHEMA_VERSION",
    "CONTEXT_MODES",
    "DOMAIN",
    "F1_PENS",
    "FORMAT_IDS",
    "PAPER_ADAPTATION",
    "RENDERING_PRESET",
    "build_f1_plate",
    "list_f1_events",
    "load_f1_catalog",
    "validate_f1_catalog",
    "validate_f1_event",
]
