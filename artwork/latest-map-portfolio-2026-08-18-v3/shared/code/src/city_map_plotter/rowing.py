"""Rowing head-course plates: the racing line, its extent, and its copy.

A head course is the one line on the sheet a buyer reads as a claim about the
world, so this module will not draw a line it cannot source. Both halves of the
geometry in ``data/rowing-courses-v1.json`` come from OpenStreetMap and are
generated offline by ``tools/build_course_geometry.py``:

* the start and finish are named OSM features matched to the organiser's own
  published course description, and
* the line between them is the OSM river centre-line, cut at those two points.

The generator measures what it cut and refuses anything more than 12% from the
published distance, so a course that reaches a plate has already been checked
against the number printed under it. Both figures travel into the manifest.

What this module adds on top is the plate side: framing the extent so the whole
course fits the map field, and turning the waypoints into a pen plan wide enough
that the course reads as the subject rather than as one more road.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any, Sequence

from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString, box
from shapely.geometry.base import BaseGeometry

from .geometry import Layout
from .models import BoundingBox, MapPlotterError
from .pens import PenInventory, PenWidthFit, fit_pen_width, style_pen_width
from .styles import race_course_ink, race_course_target_mm


ROWING_COURSE_RESOURCE = "data/rowing-courses-v1.json"
ROWING_COURSE_CATALOG_ID = "city-map-plotter-rowing-courses-v1"
ROWING_COURSE_SCHEMA_VERSION = 1

#: The course layer id, shared with ``styles.DEFAULT_STYLES`` so the course is
#: recognised as map linework by the pen plan and the manifest.
COURSE_LAYER = "race_course"

#: Fraction of the shorter map-field edge left as margin around the course.
#: A course drawn hard against the frame reads as cropped even when it is not.
DEFAULT_COURSE_MARGIN = 0.09

#: Length of the perpendicular bar drawn across the water at each end, as a
#: fraction of the map field's shorter edge.
END_BAR_FRACTION = 0.035


@dataclass(frozen=True)
class CourseEnd:
    label: str
    osm: str
    lat: float
    lon: float


@dataclass(frozen=True)
class CourseMarker:
    """One thing worth naming on the course."""

    label: str
    lat: float
    lon: float
    kind: str
    #: ``+1`` left of the direction of travel, ``-1`` right, ``0`` on the line.
    side: int = 0
    along_m: float = 0.0


@dataclass(frozen=True)
class RowingCourse:
    """One verified head course, with the numbers that verified it."""

    id: str
    name: str
    event: str
    river: str
    reach: str
    city: str
    country: str
    start: CourseEnd
    finish: CourseEnd
    official_distance_m: float
    official_distance_label: str
    direction: str
    first_held: int | None
    boats: str
    held: str
    notes: tuple[str, ...]
    poster: dict[str, Any]
    source_urls: tuple[str, ...]
    waypoints: tuple[tuple[float, float], ...]
    measured_length_m: float
    derivation: str
    catalog_sha256: str
    markers: tuple[CourseMarker, ...] = ()

    @property
    def title(self) -> str:
        return str(self.poster["title"])

    @property
    def relative_error(self) -> float:
        return (
            self.measured_length_m - self.official_distance_m
        ) / self.official_distance_m

    def bbox(self) -> BoundingBox:
        latitudes = [lat for lat, _ in self.waypoints]
        longitudes = [lon for _, lon in self.waypoints]
        return BoundingBox(
            min(longitudes), min(latitudes), max(longitudes), max(latitudes)
        )

    def as_dict(self) -> dict[str, Any]:
        """The provenance block that travels into the plate manifest."""

        return {
            "course_id": self.id,
            "name": self.name,
            "event": self.event,
            "river": self.river,
            "reach": self.reach,
            "start": {"label": self.start.label, "osm_feature": self.start.osm},
            "finish": {"label": self.finish.label, "osm_feature": self.finish.osm},
            "official_distance_m": self.official_distance_m,
            "official_distance_label": self.official_distance_label,
            "measured_centreline_m": round(self.measured_length_m, 1),
            "measured_vs_official": round(self.relative_error, 4),
            "direction": self.direction,
            "waypoint_count": len(self.waypoints),
            "markers": [
                {
                    "label": marker.label,
                    "kind": marker.kind,
                    "along_m": round(marker.along_m, 1),
                }
                for marker in self.markers
            ],
            "geometry_source": "openstreetmap",
            "geometry_licence": "ODbL 1.0",
            "geometry_derivation": self.derivation,
            "catalog_id": ROWING_COURSE_CATALOG_ID,
            "catalog_sha256": self.catalog_sha256,
            "source_urls": list(self.source_urls),
            "claim_scope": (
                "The drawn line is the OSM river centre-line between the two "
                "named endpoints, not a survey of the raced line. The printed "
                "distance is the organiser's published figure; the measured "
                "centre-line length is recorded alongside it."
            ),
        }


def _end(raw: Any, *, course_id: str, which: str) -> CourseEnd:
    if not isinstance(raw, dict):
        raise MapPlotterError(f"Course {course_id!r} {which} must be an object.")
    try:
        return CourseEnd(
            label=str(raw["label"]),
            osm=str(raw["osm"]),
            lat=float(raw["lat"]),
            lon=float(raw["lon"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Course {course_id!r} {which} is missing a label or position."
        ) from exc


def _markers(raw: dict[str, Any]) -> tuple[CourseMarker, ...]:
    """Landmarks, distance marks and bank names, in course order."""

    markers: list[CourseMarker] = []
    for kind in ("landmarks", "distance_marks", "banks"):
        for record in raw.get(kind, ()) or ():
            markers.append(
                CourseMarker(
                    label=str(record["label"]).upper(),
                    lat=float(record["lat"]),
                    lon=float(record["lon"]),
                    kind=str(record.get("kind", kind.rstrip("s"))),
                    side=int(record.get("side", 0)),
                    along_m=float(record.get("along_m", 0.0)),
                )
            )
    return tuple(markers)


def _validate(raw: dict[str, Any], *, catalog_sha256: str) -> RowingCourse:
    course_id = str(raw.get("id", "")).strip()
    if not course_id:
        raise MapPlotterError("Every rowing course must carry an id.")
    geometry = raw.get("geometry")
    if not isinstance(geometry, dict):
        raise MapPlotterError(f"Course {course_id!r} has no geometry block.")
    waypoints = geometry.get("waypoints")
    if not isinstance(waypoints, list) or len(waypoints) < 2:
        raise MapPlotterError(
            f"Course {course_id!r} needs at least two waypoints to be a line."
        )
    points: list[tuple[float, float]] = []
    for index, pair in enumerate(waypoints):
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, (int, float)) for value in pair)
        ):
            raise MapPlotterError(
                f"Course {course_id!r} waypoint {index} is not a [lat, lon] pair."
            )
        points.append((float(pair[0]), float(pair[1])))
    poster = raw.get("poster")
    if not isinstance(poster, dict) or "title" not in poster:
        raise MapPlotterError(f"Course {course_id!r} has no poster title.")
    fields = poster.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 3:
        raise MapPlotterError(
            f"Course {course_id!r} poster must carry one to three fact fields; "
            "the plate footer has three cells."
        )
    for field in fields:
        if (
            not isinstance(field, list)
            or len(field) != 2
            or not all(isinstance(value, str) and value.strip() for value in field)
        ):
            raise MapPlotterError(
                f"Course {course_id!r} poster fields must be [label, value] pairs."
            )
    return RowingCourse(
        id=course_id,
        name=str(raw["name"]),
        event=str(raw["event"]),
        river=str(raw["river"]),
        reach=str(raw["reach"]),
        city=str(raw["city"]),
        country=str(raw["country"]),
        start=_end(raw.get("start"), course_id=course_id, which="start"),
        finish=_end(raw.get("finish"), course_id=course_id, which="finish"),
        official_distance_m=float(raw["official_distance_m"]),
        official_distance_label=str(raw["official_distance_label"]),
        direction=str(raw["direction"]),
        first_held=(
            int(raw["first_held"]) if raw.get("first_held") is not None else None
        ),
        boats=str(raw["boats"]),
        held=str(raw["held"]),
        notes=tuple(str(note) for note in raw.get("notes", ())),
        poster=dict(poster),
        source_urls=tuple(str(url) for url in raw.get("source_urls", ())),
        markers=_markers(raw),
        waypoints=tuple(points),
        measured_length_m=float(geometry.get("measured_length_m", 0.0)),
        derivation=str(geometry.get("derivation", "")),
        catalog_sha256=catalog_sha256,
    )


@lru_cache(maxsize=1)
def load_rowing_courses() -> dict[str, RowingCourse]:
    resource = files("city_map_plotter").joinpath("data", "rowing-courses-v1.json")
    try:
        payload = resource.read_bytes()
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read {ROWING_COURSE_RESOURCE}: {exc}. Run "
            "tools/build_course_geometry.py to generate it."
        ) from exc
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"{ROWING_COURSE_RESOURCE} is not valid JSON: {exc}"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("id") != ROWING_COURSE_CATALOG_ID
        or document.get("schema_version") != ROWING_COURSE_SCHEMA_VERSION
    ):
        raise MapPlotterError(
            f"{ROWING_COURSE_RESOURCE} has an unsupported identity; this build "
            f"reads schema {ROWING_COURSE_SCHEMA_VERSION} of "
            f"{ROWING_COURSE_CATALOG_ID!r}."
        )
    digest = hashlib.sha256(payload).hexdigest()
    courses = [
        _validate(record, catalog_sha256=digest)
        for record in document.get("courses", [])
    ]
    if not courses:
        raise MapPlotterError(f"{ROWING_COURSE_RESOURCE} defines no courses.")
    result = {course.id: course for course in courses}
    if len(result) != len(courses):
        raise MapPlotterError(f"{ROWING_COURSE_RESOURCE} repeats a course id.")
    return result


def load_rowing_course(course_id: str) -> RowingCourse:
    catalog = load_rowing_courses()
    try:
        return catalog[course_id]
    except KeyError as exc:
        raise MapPlotterError(
            f"Unknown rowing course {course_id!r}. Choose from: "
            + ", ".join(sorted(catalog))
        ) from exc


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def course_extent(
    course: RowingCourse, *, margin: float = DEFAULT_COURSE_MARGIN
) -> BoundingBox:
    """Pad the course's own bounds so the whole race fits with air around it.

    The padding is a fraction of the *longer* side, applied to both axes, so a
    course that runs almost straight along one axis does not end up with a
    ludicrous margin on the other after the frame is squared to the plate.
    """

    bounds = course.bbox()
    latitude_span = bounds.north - bounds.south
    longitude_span = bounds.east - bounds.west
    pad = margin * max(latitude_span, longitude_span)
    return BoundingBox(
        bounds.west - pad,
        bounds.south - pad,
        bounds.east + pad,
        bounds.north + pad,
    )


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------


def course_pen_plan(
    *,
    format_id: str,
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> PenWidthFit:
    """Resolve the plate's requested course width against real pens.

    The plate asks for a mark wider than any general-colour nib on purpose, so
    this normally comes back as parallel offsets of the 0.40 -- the same
    construction the road compiler uses for a wide road.
    """

    ink = race_course_ink(format_id)
    target_mm = race_course_target_mm(format_id)
    if pen_inventory is None:
        return style_pen_width(ink=ink, nib_mm=target_mm, stroke_count=1)
    return fit_pen_width(
        pen_inventory,
        ink=ink,
        requested_width_mm=target_mm,
        allowed_nibs_mm=allowed_nibs_mm,
    )


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [part for part in geometry.geoms if not part.is_empty]
    parts: list[LineString] = []
    for part in getattr(geometry, "geoms", ()):
        parts.extend(_line_parts(part))
    return parts


def project_course(
    course: RowingCourse, layout: Layout
) -> list[list[tuple[float, float]]]:
    """Project the course to page millimetres and clip it to the map field."""

    page_points = [
        layout.project_to_page(lat, lon) for lat, lon in course.waypoints
    ]
    if len(page_points) < 2:
        return []
    field = box(*layout.clip_rect)
    try:
        clipped = LineString(page_points).intersection(field)
    except GEOSException as exc:  # pragma: no cover - GEOS edge case
        raise MapPlotterError(f"Course {course.id!r} could not be clipped: {exc}")
    return [
        [(float(x), float(y)) for x, y in part.coords]
        for part in _line_parts(clipped)
        if len(part.coords) >= 2
    ]


def offset_course_strokes(
    parts: Sequence[Sequence[tuple[float, float]]],
    *,
    plan: PenWidthFit,
    minimum_length_mm: float,
) -> list[list[tuple[float, float]]]:
    """Widen the racing line to the plate's course width with parallel passes.

    Round joins, not mitred: a river bend has hundreds of vertices and a mitre
    on the outside of a tight one throws a spike several nibs long.
    """

    positions = plan.offset_positions()
    strokes: list[list[tuple[float, float]]] = []
    for part in parts:
        centre = LineString(part)
        if centre.length + 1e-9 < minimum_length_mm:
            continue
        for distance in positions:
            if abs(distance) <= 1e-9:
                strokes.append([(float(x), float(y)) for x, y in centre.coords])
                continue
            try:
                shifted = centre.offset_curve(distance, quad_segs=4, join_style="round")
            except GEOSException:  # pragma: no cover - GEOS edge case
                shifted = centre
            for piece in _line_parts(shifted):
                if piece.length + 1e-9 >= minimum_length_mm:
                    strokes.append([(float(x), float(y)) for x, y in piece.coords])
    return strokes


def end_bar(
    parts: Sequence[Sequence[tuple[float, float]]],
    layout: Layout,
    *,
    at_start: bool,
) -> list[tuple[float, float]] | None:
    """A bar square across the water, marking the start or finish line."""

    usable = [part for part in parts if len(part) >= 2]
    if not usable:
        return None
    part = usable[0] if at_start else usable[-1]
    anchor = part[0] if at_start else part[-1]
    neighbour = part[1] if at_start else part[-2]
    dx = neighbour[0] - anchor[0]
    dy = neighbour[1] - anchor[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 1e-9:
        return None
    half = END_BAR_FRACTION * min(layout.map_width_mm, layout.map_height_mm) / 2
    normal = (-dy / length * half, dx / length * half)
    bar = [
        (anchor[0] - normal[0], anchor[1] - normal[1]),
        (anchor[0] + normal[0], anchor[1] + normal[1]),
    ]
    left, top, right, bottom = layout.clip_rect
    if any(
        not (left - 1e-6 <= x <= right + 1e-6 and top - 1e-6 <= y <= bottom + 1e-6)
        for x, y in bar
    ):
        return None
    return bar
