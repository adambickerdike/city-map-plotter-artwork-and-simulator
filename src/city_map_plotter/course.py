"""Race-course geometry: acquire it, assemble it, and prove it is a marathon.

A course is the one line on a marathon plate that a buyer reads as fact, so
nothing here guesses.  A course is only usable when its geometry assembles into
a connected run and its **measured** ground length lands within tolerance of the
official distance.  Everything else is rejected with the measurement that
rejected it, so the failure is auditable rather than silent.

This module deliberately knows nothing about pens, plates or paper.  It turns
an OSM route relation into verified WGS84 geometry; `cartography` decides how
heavily to draw it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from math import asin, cos, isfinite, radians, sin, sqrt
from typing import Any, Iterable, Sequence

from .models import BoundingBox, MapPlotterError

# The IAAF/World Athletics marathon distance. Courses are certified to this
# exact length; a traced course that disagrees by more than a couple of percent
# is either incomplete or is not the marathon it claims to be.
MARATHON_DISTANCE_M = 42_195.0

# Tolerance on measured-versus-official length, kept symmetric for callers that
# want one number. Two percent is 844 m on a marathon -- wide enough for honest
# tracing error, far too tight to admit a half marathon (-50%).
DEFAULT_LENGTH_TOLERANCE = 0.02

# Measured error is not symmetric, and the asymmetry is empirical rather than
# assumed. Across 20 published course files that verified, EVERY measurement
# came out long: +0.07% to +1.97%, none negative. That is the signature of a
# recorded trace -- GPS jitter and dense vertices add length and cannot remove
# it, and a course drawn along the road centreline is longer than the shortest
# running line the certified distance follows.
#
# A course measuring SHORT means something is missing. London's OSM relation
# measured -2.5% because a kilometre of it genuinely was not there.
#
# So the two bounds do different jobs: the lower one guards against missing
# geometry and stays tight, while the upper one absorbs trace noise.
TRACE_LENGTH_TOLERANCE_UNDER = 0.01
TRACE_LENGTH_TOLERANCE_OVER = 0.03

# Below this, two coordinates are the same point for stitching purposes. One
# metre is far under any real course feature and far over float noise.
JOIN_TOLERANCE_M = 1.0

EARTH_RADIUS_M = 6_371_008.8

_RELATION_REF = re.compile(r"^relation/([1-9][0-9]*)$")


def parse_relation_ref(value: str) -> int:
    """Validate an exact ``relation/<id>`` reference and return its id.

    Whitespace is rejected rather than trimmed, matching the convention
    ``osm.canonical_landmark_refs`` already sets for exact object references:
    these are identifiers in a committed catalog, not user input, and a
    reference that needed cleaning up is a reference worth looking at.
    """

    match = _RELATION_REF.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise MapPlotterError(
            "A course source must be an exact 'relation/<positive-id>' reference "
            f"without whitespace or leading zeroes, not {value!r}."
        )
    return int(match.group(1))


def haversine_m(
    start: tuple[float, float], end: tuple[float, float]
) -> float:
    """Great-circle distance in metres between two (latitude, longitude) pairs.

    A marathon spans tens of kilometres across a whole city, so the local
    equirectangular projection the plate uses is not accurate enough to
    *certify* a length with -- that is what this is for.
    """

    lat1, lon1 = radians(start[0]), radians(start[1])
    lat2, lon2 = radians(end[0]), radians(end[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    haversine = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(min(1.0, sqrt(haversine)))


def polyline_length_m(points: Sequence[tuple[float, float]]) -> float:
    return sum(
        haversine_m(start, end) for start, end in zip(points, points[1:])
    )


@dataclass(frozen=True)
class CourseVerification:
    """Why a course was accepted or refused, in numbers."""

    official_distance_m: float
    measured_length_m: float
    tolerance: float
    component_count: int
    largest_component_fraction: float
    coordinate_count: int
    accepted: bool
    reasons: tuple[str, ...] = ()

    @property
    def relative_error(self) -> float:
        return (
            self.measured_length_m - self.official_distance_m
        ) / self.official_distance_m

    def as_dict(self) -> dict[str, Any]:
        return {
            "official_distance_m": round(self.official_distance_m, 3),
            "measured_length_m": round(self.measured_length_m, 3),
            "relative_error": round(self.relative_error, 6),
            "tolerance": self.tolerance,
            "component_count": self.component_count,
            "largest_component_fraction": round(self.largest_component_fraction, 6),
            "coordinate_count": self.coordinate_count,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RaceCourse:
    """One verified race course in WGS84, with the evidence that verified it."""

    id: str
    name: str
    source_ref: str
    parts: tuple[tuple[tuple[float, float], ...], ...]
    verification: CourseVerification
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def points(self) -> tuple[tuple[float, float], ...]:
        """The single longest run of the course, its principal line."""

        return max(self.parts, key=polyline_length_m) if self.parts else ()

    @property
    def measured_length_m(self) -> float:
        return self.verification.measured_length_m

    def bbox(self) -> BoundingBox:
        """The full extent of the course, never degenerate.

        A course that runs dead straight along one axis has zero span on the
        other, which is not a rectangle a map can be framed in.  Real courses
        are never that, but the framing code must not depend on that being
        true, so a flat axis is opened to a metre rather than raising.
        """

        latitudes = [lat for part in self.parts for lat, _ in part]
        longitudes = [lon for part in self.parts for _, lon in part]
        if not latitudes:
            raise MapPlotterError(f"Course {self.id!r} has no geometry to bound.")
        south, north = min(latitudes), max(latitudes)
        west, east = min(longitudes), max(longitudes)
        minimum_span = 1.0 / 111_320.0  # one metre in degrees of latitude
        if north - south < minimum_span:
            middle = (north + south) / 2
            south, north = middle - minimum_span / 2, middle + minimum_span / 2
        if east - west < minimum_span:
            middle = (east + west) / 2
            west, east = middle - minimum_span / 2, middle + minimum_span / 2
        return BoundingBox(west, south, east, north)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source_ref": self.source_ref,
            "part_count": len(self.parts),
            "coordinate_count": sum(len(part) for part in self.parts),
            "measured_length_m": round(self.measured_length_m, 3),
            "verification": self.verification.as_dict(),
            "tags": dict(sorted(self.tags.items())),
        }


def build_course_relation_query(relation_id: int, timeout_s: int = 180) -> str:
    """Fetch one route relation with the full geometry of every member way.

    ``way(r)`` recurses from the relation in the named set down to its member
    ways.  Note it is *not* ``way(r:<id>)`` -- that form filters members by
    *role*, so it silently matches nothing and the course arrives empty.
    """

    if relation_id <= 0:
        raise MapPlotterError("A course relation id must be a positive integer.")
    return (
        f"[out:json][timeout:{timeout_s}];\n"
        f"relation({relation_id})->.course;\n"
        ".course out tags;\n"
        "way(r.course);\n"
        "out geom;\n"
    )


def _way_geometries(document: dict[str, Any]) -> list[list[tuple[float, float]]]:
    ways: list[list[tuple[float, float]]] = []
    for element in document.get("elements", []):
        if element.get("type") != "way":
            continue
        geometry = element.get("geometry")
        if not isinstance(geometry, list):
            # A way returned without geometry means the query asked for tags
            # only; stitching a course from partial members would invent a
            # shortcut between real ones.
            raise MapPlotterError(
                f"Course way {element.get('id')} arrived without geometry; "
                "request it with 'out geom'."
            )
        points = [
            (float(node["lat"]), float(node["lon"]))
            for node in geometry
            if isinstance(node, dict) and "lat" in node and "lon" in node
        ]
        if len(points) >= 2:
            ways.append(points)
    return ways


def _relation_tags(document: dict[str, Any], relation_id: int) -> dict[str, str]:
    for element in document.get("elements", []):
        if element.get("type") == "relation" and int(element.get("id", 0)) == relation_id:
            tags = element.get("tags") or {}
            return {str(k): str(v) for k, v in tags.items()}
    raise MapPlotterError(
        f"Relation {relation_id} is absent from the course response; it may have "
        "been deleted or the id may be wrong."
    )


def stitch(
    ways: Iterable[Sequence[tuple[float, float]]],
    *,
    join_tolerance_m: float = JOIN_TOLERANCE_M,
) -> list[list[tuple[float, float]]]:
    """Join member ways end-to-end into the fewest connected runs.

    Route relations store members in running order but individual ways point
    whichever way the mapper drew them, and a relation may legitimately hold
    more than one run (London has separate Red and Blue starts that merge).
    Ways are only ever joined where their endpoints actually touch: a gap is
    kept as a gap rather than closed with a straight line the runners never ran.
    """

    remaining = [list(way) for way in ways if len(way) >= 2]
    runs: list[list[tuple[float, float]]] = []
    while remaining:
        current = remaining.pop(0)
        extended = True
        while extended:
            extended = False
            for index, candidate in enumerate(remaining):
                for candidate_points in (candidate, candidate[::-1]):
                    if haversine_m(current[-1], candidate_points[0]) <= join_tolerance_m:
                        current.extend(candidate_points[1:])
                        remaining.pop(index)
                        extended = True
                        break
                    if haversine_m(current[0], candidate_points[-1]) <= join_tolerance_m:
                        current[:0] = candidate_points[:-1]
                        remaining.pop(index)
                        extended = True
                        break
                if extended:
                    break
        runs.append(current)
    runs.sort(key=polyline_length_m, reverse=True)
    return runs


def verify(
    parts: Sequence[Sequence[tuple[float, float]]],
    *,
    official_distance_m: float = MARATHON_DISTANCE_M,
    tolerance: float = DEFAULT_LENGTH_TOLERANCE,
    tolerance_over: float | None = None,
) -> CourseVerification:
    """Measure assembled geometry against the official distance.

    Two independent things have to hold. The total length must match the
    certified distance, and the course must be essentially one connected run --
    a set of disjoint fragments can total 42 km without ever being a course.
    """

    if not isfinite(official_distance_m) or official_distance_m <= 0:
        raise MapPlotterError("Official course distance must be a positive length.")
    if not isfinite(tolerance) or tolerance <= 0:
        raise MapPlotterError("Course length tolerance must be positive.")
    over = tolerance if tolerance_over is None else tolerance_over
    if not isfinite(over) or over <= 0:
        raise MapPlotterError("Course over-length tolerance must be positive.")

    lengths = [polyline_length_m(part) for part in parts]
    measured = sum(lengths)
    coordinate_count = sum(len(part) for part in parts)
    largest_fraction = (max(lengths) / measured) if measured > 0 else 0.0
    reasons: list[str] = []

    if not parts or measured <= 0:
        reasons.append("no geometry")
    else:
        signed = (measured - official_distance_m) / official_distance_m
        limit = over if signed >= 0 else tolerance
        if abs(signed) > limit:
            direction = "long" if signed >= 0 else "short"
            reasons.append(
                f"measured {measured / 1000:.3f} km differs from the official "
                f"{official_distance_m / 1000:.3f} km by {abs(signed) * 100:.1f}% "
                f"({direction}), above the {limit * 100:.1f}% tolerance"
            )
        # A real course is one run. Allow a modest tail for the separate start
        # spurs some events genuinely have, but refuse a bag of fragments.
        if largest_fraction < 0.80:
            reasons.append(
                f"largest connected run is only {largest_fraction * 100:.0f}% of the "
                f"geometry across {len(parts)} disconnected parts"
            )

    return CourseVerification(
        official_distance_m=official_distance_m,
        measured_length_m=measured,
        tolerance=tolerance,
        component_count=len(parts),
        largest_component_fraction=largest_fraction,
        coordinate_count=coordinate_count,
        accepted=not reasons,
        reasons=tuple(reasons),
    )


def course_from_overpass(
    document: dict[str, Any],
    *,
    course_id: str,
    source_ref: str,
    official_distance_m: float = MARATHON_DISTANCE_M,
    tolerance: float = DEFAULT_LENGTH_TOLERANCE,
    require_accepted: bool = True,
) -> RaceCourse:
    """Turn one Overpass route-relation response into a verified course."""

    relation_id = parse_relation_ref(source_ref)
    tags = _relation_tags(document, relation_id)
    parts = stitch(_way_geometries(document))
    verification = verify(
        parts,
        official_distance_m=official_distance_m,
        tolerance=tolerance,
    )
    if require_accepted and not verification.accepted:
        raise MapPlotterError(
            f"Course {course_id!r} from {source_ref} is not a verified "
            f"{official_distance_m / 1000:.3f} km course: "
            + "; ".join(verification.reasons)
            + ". It will not be drawn."
        )
    return RaceCourse(
        id=course_id,
        name=tags.get("name", course_id),
        source_ref=source_ref,
        parts=tuple(tuple(part) for part in parts),
        verification=verification,
        tags=tags,
    )


COURSE_LAYER = "race_course"


def course_features(course: RaceCourse) -> list[Any]:
    """Present a verified course as ordinary source features.

    The course then travels the same road as every other line on the plate --
    projection, clipping, the 3 x nib gate, the physical pen fit, the manifest
    -- instead of being drawn by a private path that none of those rules see.
    """

    from .models import MapFeature

    # A course may come from an OSM relation or from an official track file.
    # Provenance records which, so the manifest never implies an OSM object
    # that does not exist.
    if course.source_ref.startswith("relation/"):
        osm_type, osm_id = "relation", str(parse_relation_ref(course.source_ref))
    else:
        osm_type, osm_id = "course-file", course.source_ref.split("/", 1)[-1]
    features: list[Any] = []
    for index, part in enumerate(course.parts):
        if len(part) < 2:
            continue
        features.append(
            MapFeature(
                layer=COURSE_LAYER,
                points=[(float(lat), float(lon)) for lat, lon in part],
                osm_type=osm_type,
                osm_id=osm_id,
                part=str(index),
                tags={
                    **course.tags,
                    "mapplot:course-id": course.id,
                    "mapplot:course-source": course.source_ref,
                    "mapplot:course-measured-m": f"{course.measured_length_m:.3f}",
                    "mapplot:course-part": str(index),
                },
                geometry_type="line",
            )
        )
    if not features:
        raise MapPlotterError(
            f"Course {course.id!r} verified but produced no drawable geometry."
        )
    return features


GPX_NS = "http://www.topografix.com/GPX/1/1"
GPX_NS_10 = "http://www.topografix.com/GPX/1/0"
KML_NS = "http://www.opengis.net/kml/2.2"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_gpx(text: str) -> list[list[tuple[float, float]]]:
    """Read track segments, or a route, from GPX 1.0/1.1.

    Race organisers publish GPX far more often than they contribute to OSM, so
    this is the path that reaches the events OSM has never mapped.  Track
    segments are kept separate rather than concatenated: a GPX with two
    segments is two runs until the stitcher proves their ends actually meet.
    """

    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MapPlotterError(f"Course GPX is not well-formed XML: {exc}") from exc

    parts: list[list[tuple[float, float]]] = []

    def points_of(container: Any, point_tag: str) -> list[tuple[float, float]]:
        found: list[tuple[float, float]] = []
        for node in container.iter():
            if _local_name(node.tag) != point_tag:
                continue
            try:
                found.append((float(node.attrib["lat"]), float(node.attrib["lon"])))
            except (KeyError, ValueError) as exc:
                raise MapPlotterError(
                    f"Course GPX has a {point_tag} without usable lat/lon: {exc}"
                ) from exc
        return found

    for node in root.iter():
        if _local_name(node.tag) == "trkseg":
            segment = points_of(node, "trkpt")
            if len(segment) >= 2:
                parts.append(segment)
    if not parts:
        for node in root.iter():
            if _local_name(node.tag) == "rte":
                route = points_of(node, "rtept")
                if len(route) >= 2:
                    parts.append(route)
    if not parts:
        raise MapPlotterError(
            "Course GPX contains no track segment or route with two or more points."
        )
    return parts


def parse_kml(text: str) -> list[list[tuple[float, float]]]:
    """Read every LineString from KML. Coordinates are lon,lat[,elevation]."""

    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MapPlotterError(f"Course KML is not well-formed XML: {exc}") from exc

    parts: list[list[tuple[float, float]]] = []
    for node in root.iter():
        if _local_name(node.tag) != "coordinates":
            continue
        points: list[tuple[float, float]] = []
        for token in (node.text or "").replace("\n", " ").split():
            pieces = token.split(",")
            if len(pieces) < 2:
                continue
            try:
                # KML is lon,lat -- the opposite order to GPX. Getting this
                # backwards silently puts the course in the wrong hemisphere.
                points.append((float(pieces[1]), float(pieces[0])))
            except ValueError as exc:
                raise MapPlotterError(
                    f"Course KML has an unreadable coordinate {token!r}: {exc}"
                ) from exc
        if len(points) >= 2:
            parts.append(points)
    if not parts:
        raise MapPlotterError("Course KML contains no LineString with two or more points.")
    return parts


def parse_geojson(text: str) -> tuple[list[list[tuple[float, float]]], str | None]:
    """Read every LineString from GeoJSON, returning parts and a name.

    GeoJSON positions are ``[longitude, latitude]`` -- and often carry a third
    elevation value, which is ignored here.  Published course files routinely
    split the route into many features, so each is kept separate for the
    stitcher to join only where the ends actually meet.
    """

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Course GeoJSON is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise MapPlotterError("Course GeoJSON must be an object.")

    parts: list[list[tuple[float, float]]] = []
    name: str | None = None

    def take(coordinates: Any) -> None:
        points: list[tuple[float, float]] = []
        for position in coordinates or []:
            if not isinstance(position, (list, tuple)) or len(position) < 2:
                continue
            try:
                points.append((float(position[1]), float(position[0])))
            except (TypeError, ValueError) as exc:
                raise MapPlotterError(
                    f"Course GeoJSON has an unreadable position {position!r}: {exc}"
                ) from exc
        if len(points) >= 2:
            parts.append(points)

    def walk(node: Any) -> None:
        nonlocal name
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "FeatureCollection":
            for feature in node.get("features") or []:
                walk(feature)
            return
        if node_type == "Feature":
            properties = node.get("properties") or {}
            if name is None and isinstance(properties, dict):
                candidate = properties.get("name")
                if isinstance(candidate, str) and candidate.strip():
                    name = candidate.strip()
            walk(node.get("geometry"))
            return
        if node_type == "LineString":
            take(node.get("coordinates"))
        elif node_type == "MultiLineString":
            for line in node.get("coordinates") or []:
                take(line)
        elif node_type == "GeometryCollection":
            for geometry in node.get("geometries") or []:
                walk(geometry)

    walk(document)
    if not parts:
        raise MapPlotterError(
            "Course GeoJSON contains no LineString with two or more positions."
        )
    return parts, name


def course_from_track_file(
    path: Any,
    *,
    course_id: str,
    source_ref: str,
    name: str | None = None,
    official_distance_m: float = MARATHON_DISTANCE_M,
    tolerance: float = TRACE_LENGTH_TOLERANCE_UNDER,
    require_accepted: bool = True,
) -> RaceCourse:
    """Import an official GPX or KML course and hold it to the same proof.

    A file from the race organiser is better provenance than a volunteer trace,
    but it is not exempt: it is measured against the certified distance exactly
    as an OSM relation is, and rejected the same way when it does not agree.
    """

    from pathlib import Path as _Path

    file_path = _Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MapPlotterError(f"Could not read course file {file_path}: {exc}") from exc

    suffix = file_path.suffix.casefold()
    discovered_name: str | None = None
    if suffix == ".gpx":
        raw_parts = parse_gpx(text)
    elif suffix == ".kml":
        raw_parts = parse_kml(text)
    elif suffix in {".geojson", ".json"}:
        raw_parts, discovered_name = parse_geojson(text)
    else:
        raise MapPlotterError(
            f"Course file {file_path.name} must be .gpx, .kml or .geojson, "
            f"not {suffix or 'unknown'}."
        )

    parts = stitch(raw_parts)
    verification = verify(
        parts,
        official_distance_m=official_distance_m,
        tolerance=tolerance,
        tolerance_over=TRACE_LENGTH_TOLERANCE_OVER,
    )
    if require_accepted and not verification.accepted:
        raise MapPlotterError(
            f"Course {course_id!r} from {file_path.name} is not a verified "
            f"{official_distance_m / 1000:.3f} km course: "
            + "; ".join(verification.reasons)
            + ". It will not be drawn."
        )
    return RaceCourse(
        id=course_id,
        name=name or discovered_name or course_id,
        source_ref=source_ref,
        parts=tuple(tuple(part) for part in parts),
        verification=verification,
        tags={"source_file": file_path.name, "source_format": suffix.lstrip(".")},
    )


def load_course_file(path: Any, *, course_id: str, source_ref: str) -> RaceCourse:
    """Load a course from a saved Overpass response, for offline rebuilds."""

    try:
        document = json.loads(open(path, encoding="utf-8").read())
    except OSError as exc:
        raise MapPlotterError(f"Could not read course response {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Course response {path} is not valid JSON: {exc}"
        ) from exc
    return course_from_overpass(
        document, course_id=course_id, source_ref=source_ref
    )
