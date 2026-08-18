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

# Tolerance on measured-versus-official length. OSM traces the road centreline
# while the certified distance follows the shortest running line, and vertices
# are simplified, so a faithful trace still lands a little short or long. Two
# percent is 844 m on a marathon -- wide enough for honest tracing error,
# far too tight to admit a half marathon (-50%) or a truncated course.
DEFAULT_LENGTH_TOLERANCE = 0.02

# Below this, two coordinates are the same point for stitching purposes. One
# metre is far under any real course feature and far over float noise.
JOIN_TOLERANCE_M = 1.0

EARTH_RADIUS_M = 6_371_008.8

_RELATION_REF = re.compile(r"^relation/([1-9][0-9]*)$")


def parse_relation_ref(value: str) -> int:
    """Validate an exact ``relation/<id>`` reference and return its id."""

    match = _RELATION_REF.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise MapPlotterError(
            "A course source must be an exact 'relation/<positive-id>' reference, "
            f"not {value!r}."
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
        latitudes = [lat for part in self.parts for lat, _ in part]
        longitudes = [lon for part in self.parts for _, lon in part]
        if not latitudes:
            raise MapPlotterError(f"Course {self.id!r} has no geometry to bound.")
        return BoundingBox(
            min(longitudes), min(latitudes), max(longitudes), max(latitudes)
        )

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
    """Fetch one route relation with the full geometry of every member way."""

    if relation_id <= 0:
        raise MapPlotterError("A course relation id must be a positive integer.")
    return (
        f"[out:json][timeout:{timeout_s}];\n"
        f"relation({relation_id});\n"
        "out tags;\n"
        f"way(r:{relation_id});\n"
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

    lengths = [polyline_length_m(part) for part in parts]
    measured = sum(lengths)
    coordinate_count = sum(len(part) for part in parts)
    largest_fraction = (max(lengths) / measured) if measured > 0 else 0.0
    reasons: list[str] = []

    if not parts or measured <= 0:
        reasons.append("no geometry")
    else:
        error = abs(measured - official_distance_m) / official_distance_m
        if error > tolerance:
            reasons.append(
                f"measured {measured / 1000:.3f} km differs from the official "
                f"{official_distance_m / 1000:.3f} km by {error * 100:.1f}%, "
                f"above the {tolerance * 100:.1f}% tolerance"
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
