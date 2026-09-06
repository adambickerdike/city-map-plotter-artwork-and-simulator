"""Source-faithful, physical-pen maps of landmark golf courses.

The ordinary city-map classifier treats an entire golf course as generic green
space.  This focused renderer instead consumes a frozen catalog of exact OSM
objects for holes, tees, greens, fairways, bunkers, water, paths, vegetation,
and buildings.  It never completes an unmapped feature by inference.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, NoReturn, Sequence

from shapely import affinity, concave_hull, set_precision
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, transform as transform_geometry, unary_union
from shapely.prepared import prep

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
)


CATALOG_PATH = Path(__file__).with_name("data") / "golf-courses-v2.json"
CATALOG_ID = "golf-courses-v2"
FORMAT_ID = "a3-portrait"
FORMAT_SUBJECT_POLICY = "map"
GOLF_PENS = (
    "grey-0-25",
    "grey-0-4",
    "green-0-25",
    "green-0-4",
    "blue-0-25",
    "blue-0-4",
    "red-0-25",
    "black-0-4",
    "black-1",
    "gold-1",
)
SCALE_DENOMINATOR_INCREMENT = 100
HATCH_SIMPLIFY_MM = 0.04
GREEN_HATCH_SPACING_MM = 1.45
# Keep the 0.25 mm fill pen inside the sourced green while allowing the fine
# hatches to meet the heavier 0.40 mm outline.  A larger outline-clearance
# inset left small, correctly mapped greens visibly empty at course scale.
GREEN_FILL_INSET_MM = 0.16
GREEN_ROUTE_TEXTURE_CLEARANCE_MM = 0.68
GREEN_FALLBACK_PROBE_SPACING_MM = 0.18
WATER_STIPPLE_SPACING_MM = 3.0
WATER_STIPPLE_RADIUS_MM = 0.18
WATER_STIPPLE_MINIMUM_DISTANCE_MM = 2.45
WATER_STIPPLE_PHYSICAL_MINIMUM_DISTANCE_MM = 0.63
LINEAR_WATER_STIPPLE_SPACING_MM = 3.0
SEA_STIPPLE_SPACING_MM = 6.2
SEA_STIPPLE_MINIMUM_DISTANCE_MM = 5.0
SEA_STIPPLE_BAND_FIELD_FRACTION = 0.12
PLAYING_ENVELOPE_HOLE_BUFFER_M = 18.0
PLAYING_ENVELOPE_CONCAVE_HULL_RATIO = 0.25
PLAYING_ENVELOPE_OUTSET_M = 10.0
PLAYING_ENVELOPE_SIMPLIFY_MM = 0.35
ROUTE_TEXTURE_CLEARANCE_MM = 0.9
COURSE_WATER_CONTEXT_DISTANCE_M = 60.0
LABEL_RADIUS_MM = 2.8
LABEL_CLEARANCE_MM = 0.9
LABEL_MASK_OVERCUT_MM = 0.02


PointTuple = tuple[float, float]
WATER_DOT_ROLES = frozenset(
    {
        "water-area-stipple-dot",
        "water-linear-stipple-dot",
        "water-narrow-boundary-stipple-dot",
        "water-narrow-source-stipple-dot",
    }
)


def _is_water_dot_role(role: str | None) -> bool:
    return role in WATER_DOT_ROLES


def _water_dot_represents_geometry(
    centre: PointTuple,
    geometry: BaseGeometry,
    role: str,
    *,
    tolerance_mm: float = 0.002,
) -> bool:
    """Return whether a dot centre truthfully belongs to its claimed source."""

    point = Point(centre)
    if role == "water-area-stipple-dot":
        physical_radius = WATER_STIPPLE_RADIUS_MM + 0.125
        safe = geometry.buffer(-physical_radius)
        return not safe.is_empty and safe.covers(point)
    if role in {
        "water-linear-stipple-dot",
        "water-narrow-boundary-stipple-dot",
    }:
        return geometry.distance(point) <= tolerance_mm
    if role == "water-narrow-source-stipple-dot":
        return geometry.buffer(tolerance_mm).covers(point)
    return False


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid golf course data: {message}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(f"{label} must be {'a non-empty' if nonempty else 'an'} array.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} must be a finite number.")
    return result


def _keys(
    value: dict[str, Any],
    label: str,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unexpected = sorted(set(value) - allowed)
    if missing:
        _fail(f"{label} is missing fields: {', '.join(missing)}.")
    if unexpected:
        _fail(f"{label} has unsupported fields: {', '.join(unexpected)}.")


def _leading_hole_number(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.match(r"\s*(\d{1,2})(?:\D|$)", value)
    if match is None:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 18 else None


def _geometry_payload(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "coordinate_system": model["coordinate_system"],
        "origin_wgs84": model["origin_wgs84"],
        "boundary": model["boundary"],
        "features": model["features"],
    }


def _geometry_sha256(model: dict[str, Any]) -> str:
    payload = json.dumps(
        _geometry_payload(model),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_path(path: Any, label: str) -> dict[str, Any]:
    value = _object(path, label)
    _keys(value, label, required={"role", "points"}, optional={"source_ref"})
    _text(value["role"], f"{label}.role")
    points = _array(value["points"], f"{label}.points", nonempty=True)
    if len(points) < 1:
        _fail(f"{label}.points cannot be empty.")
    for index, point in enumerate(points):
        pair = _array(point, f"{label}.points[{index}]")
        if len(pair) != 2:
            _fail(f"{label}.points[{index}] must be [x, y].")
        _number(pair[0], f"{label}.points[{index}][0]")
        _number(pair[1], f"{label}.points[{index}][1]")
    if "source_ref" in value:
        _text(value["source_ref"], f"{label}.source_ref")
    return value


def validate_golf_record(record: Any) -> dict[str, Any]:
    """Validate and return an isolated golf-course catalog record."""

    value = _object(record, "record")
    _keys(
        value,
        "record",
        required={
            "id",
            "title",
            "subtitle",
            "subject_kind",
            "format_id",
            "location",
            "championship_context",
            "selection_note",
            "sources",
            "evidence",
            "rights_status",
            "notes",
            "model",
        },
        optional={"data_snapshot", "source_contract", "series"},
    )
    subject_id = _text(value["id"], "record.id")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", subject_id) is None:
        _fail("record.id must be a lower-case stable identifier.")
    _text(value["title"], "record.title")
    _text(value["subtitle"], "record.subtitle")
    if value["subject_kind"] != "map":
        _fail("record.subject_kind must be 'map'.")
    if value["format_id"] != FORMAT_ID:
        _fail(f"record.format_id must be {FORMAT_ID!r} for this dense edition.")

    location = _object(value["location"], "record.location")
    _keys(location, "record.location", required={"label", "country_code"})
    _text(location["label"], "record.location.label")
    if (
        re.fullmatch(
            r"[A-Z]{2}", _text(location["country_code"], "record.location.country_code")
        )
        is None
    ):
        _fail("record.location.country_code must be uppercase ISO alpha-2.")
    _text(value["championship_context"], "record.championship_context")
    _text(value["selection_note"], "record.selection_note")

    sources = _array(value["sources"], "record.sources", nonempty=True)
    if not any(
        isinstance(source, dict)
        and source.get("kind") == "openstreetmap"
        and source.get("license") == "ODbL-1.0"
        for source in sources
    ):
        _fail("record.sources needs a pinned ODbL OpenStreetMap source.")
    for index, source in enumerate(sources):
        checked_source = _object(source, f"record.sources[{index}]")
        for key in ("id", "kind", "publisher", "license", "url", "use"):
            _text(checked_source.get(key), f"record.sources[{index}].{key}")

    evidence = _object(value["evidence"], "record.evidence")
    _keys(
        evidence,
        "record.evidence",
        required={
            "status",
            "hole_inventory",
            "selection_method",
            "feature_counts",
            "statement",
        },
    )
    if evidence["hole_inventory"] != "exactly-18-numbered-source-centrelines":
        _fail("record.evidence must bind exactly 18 numbered source holes.")
    _text(evidence["statement"], "record.evidence.statement")
    counts = _object(evidence["feature_counts"], "record.evidence.feature_counts")
    if counts.get("golf:hole") != 18:
        _fail("record.evidence.feature_counts must report exactly 18 golf holes.")
    if value["rights_status"] != "odbl-attribution-required":
        _fail("record.rights_status must preserve the ODbL attribution requirement.")
    notes = _array(value["notes"], "record.notes")
    for index, note in enumerate(notes):
        _text(note, f"record.notes[{index}]")

    model = _object(value["model"], "record.model")
    _keys(
        model,
        "record.model",
        required={
            "coordinate_system",
            "origin_wgs84",
            "projection",
            "boundary",
            "features",
            "geometry_sha256",
        },
    )
    if model["coordinate_system"] != "local-equirectangular-metre":
        _fail("record.model must use one shared local-equirectangular-metre grid.")
    origin = _array(model["origin_wgs84"], "record.model.origin_wgs84")
    if len(origin) != 2:
        _fail("record.model.origin_wgs84 must be [latitude, longitude].")
    _number(origin[0], "record.model.origin_wgs84[0]")
    _number(origin[1], "record.model.origin_wgs84[1]")
    boundary = _array(model["boundary"], "record.model.boundary", nonempty=True)
    for index, path in enumerate(boundary):
        checked_path = _validate_path(path, f"record.model.boundary[{index}]")
        if len(checked_path["points"]) < 4:
            _fail(f"record.model.boundary[{index}] is not a closed polygonal path.")
    features = _array(model["features"], "record.model.features", nonempty=True)
    hole_numbers: list[int] = []
    for feature_index, feature in enumerate(features):
        checked = _object(feature, f"record.model.features[{feature_index}]")
        _keys(
            checked,
            f"record.model.features[{feature_index}]",
            required={
                "source_ref",
                "source_version",
                "source_timestamp",
                "tags",
                "paths",
            },
        )
        source_ref = _text(
            checked["source_ref"], f"record.model.features[{feature_index}].source_ref"
        )
        if re.fullmatch(r"(?:node|way|relation)/\d+", source_ref) is None:
            _fail(
                f"record.model.features[{feature_index}].source_ref is not an OSM reference."
            )
        if (
            not isinstance(checked["source_version"], int)
            or checked["source_version"] <= 0
        ):
            _fail(
                f"record.model.features[{feature_index}].source_version must be positive."
            )
        _text(
            checked["source_timestamp"],
            f"record.model.features[{feature_index}].source_timestamp",
        )
        tags = _object(checked["tags"], f"record.model.features[{feature_index}].tags")
        paths = _array(
            checked["paths"],
            f"record.model.features[{feature_index}].paths",
            nonempty=True,
        )
        for path_index, path in enumerate(paths):
            _validate_path(
                path, f"record.model.features[{feature_index}].paths[{path_index}]"
            )
        if tags.get("golf") == "hole":
            number = _leading_hole_number(tags.get("ref"))
            if number is None:
                _fail(f"source hole {source_ref} has no valid 1..18 ref.")
            hole_numbers.append(number)
    if sorted(hole_numbers) != list(range(1, 19)):
        _fail("record.model must contain each source hole number exactly once.")
    digest = _text(model["geometry_sha256"], "record.model.geometry_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        _fail("record.model.geometry_sha256 must be a lower-case SHA-256.")
    if _geometry_sha256(model) != digest:
        _fail("record.model.geometry_sha256 disagrees with the complete model.")
    return copy.deepcopy(value)


def load_golf_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the packaged 25-course catalog or an equivalent strict wrapper."""

    source = path or CATALOG_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(f"Could not read golf catalog {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Golf catalog {source} is invalid JSON: {exc}") from exc
    wrapper = _object(raw, "catalog")
    _keys(
        wrapper,
        "catalog",
        required={
            "schema_version",
            "catalog_id",
            "data_snapshot",
            "source_contract",
            "series",
            "subjects",
        },
    )
    if wrapper["schema_version"] != 1 or wrapper["catalog_id"] != CATALOG_ID:
        _fail("catalog wrapper has the wrong schema_version or catalog_id.")
    records = [
        validate_golf_record(record)
        for record in _array(wrapper["subjects"], "catalog.subjects", nonempty=True)
    ]
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        _fail("catalog repeats a course id.")
    if path is None and len(records) != 25:
        _fail("packaged catalog must contain exactly 25 courses.")
    for record in records:
        record["data_snapshot"] = wrapper["data_snapshot"]
        record["source_contract"] = copy.deepcopy(wrapper["source_contract"])
        record["series"] = copy.deepcopy(wrapper["series"])
    return records


def _paths_geometry(paths: Sequence[dict[str, Any]]) -> BaseGeometry:
    points: list[Point] = []
    lines: list[LineString] = []
    outer_lines: list[LineString] = []
    inner_lines: list[LineString] = []
    for path in paths:
        coordinates = [(float(point[0]), float(point[1])) for point in path["points"]]
        if len(coordinates) == 1:
            points.append(Point(coordinates[0]))
        elif len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
            target = inner_lines if path.get("role") == "inner" else outer_lines
            target.append(LineString(coordinates))
        elif len(coordinates) >= 2:
            lines.append(LineString(coordinates))
    polygons: BaseGeometry | None = None
    if outer_lines:
        polygon_parts = list(polygonize(unary_union(outer_lines)))
        if not polygon_parts:
            polygon_parts = [
                Polygon(line.coords).buffer(0)
                for line in outer_lines
                if len(line.coords) >= 4
            ]
        polygons = unary_union(polygon_parts)
        if inner_lines:
            inner_parts = list(polygonize(unary_union(inner_lines)))
            if inner_parts:
                polygons = polygons.difference(unary_union(inner_parts))
    geometries: list[BaseGeometry] = []
    if polygons is not None and not polygons.is_empty:
        geometries.append(polygons.buffer(0))
    geometries.extend(lines)
    geometries.extend(points)
    return unary_union(geometries) if geometries else GeometryCollection()


def _feature_kind(tags: dict[str, str]) -> str | None:
    golf = tags.get("golf")
    if golf:
        return f"golf:{golf}"
    natural = tags.get("natural")
    if natural in {"water", "coastline"} or tags.get("water"):
        return "water"
    if natural in {"wood", "scrub", "heath", "wetland"}:
        return f"vegetation:{natural}"
    landuse = tags.get("landuse")
    if landuse == "forest":
        return f"vegetation:{landuse}"
    if tags.get("waterway"):
        return "waterway"
    if tags.get("highway"):
        return "path"
    if tags.get("building"):
        return "building"
    return None


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        result: list[Polygon] = []
        for part in geometry.geoms:
            result.extend(_polygon_parts(part))
        return result
    return []


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, Polygon):
        return [
            LineString(geometry.exterior.coords),
            *[LineString(ring.coords) for ring in geometry.interiors],
        ]
    if isinstance(geometry, MultiPolygon):
        result: list[LineString] = []
        for polygon in geometry.geoms:
            result.extend(_line_parts(polygon))
        return result
    if isinstance(geometry, GeometryCollection):
        result = []
        for part in geometry.geoms:
            result.extend(_line_parts(part))
        return result
    return []


def _paper_geometry(
    geometry: BaseGeometry,
    *,
    rotated_centre: PointTuple,
    rotation_deg: float,
    paper_rect: Rect,
    scale_mm_per_m: float,
) -> BaseGeometry:
    angle = math.radians(rotation_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)

    def transform(x: Any, y: Any, z: Any = None):
        try:
            rotated_x = [
                cosine * float(x_value) - sine * float(y_value)
                for x_value, y_value in zip(x, y, strict=True)
            ]
            rotated_y = [
                sine * float(x_value) + cosine * float(y_value)
                for x_value, y_value in zip(x, y, strict=True)
            ]
            return (
                [
                    paper_rect.centre[0] + (value - rotated_centre[0]) * scale_mm_per_m
                    for value in rotated_x
                ],
                [
                    paper_rect.centre[1] - (value - rotated_centre[1]) * scale_mm_per_m
                    for value in rotated_y
                ],
            )
        except TypeError:
            rotated_scalar_x = cosine * float(x) - sine * float(y)
            rotated_scalar_y = sine * float(x) + cosine * float(y)
            return (
                paper_rect.centre[0]
                + (rotated_scalar_x - rotated_centre[0]) * scale_mm_per_m,
                paper_rect.centre[1]
                - (rotated_scalar_y - rotated_centre[1]) * scale_mm_per_m,
            )

    return transform_geometry(transform, geometry)


def _page_fit(
    geometry: BaseGeometry, rect: Rect
) -> tuple[int, PointTuple, int, tuple[float, float]]:
    """Choose the clearest page rotation and an accurate 1:100 plan scale.

    Golf routing is read more clearly when the long axis of the source course
    uses the long axis available on paper.  The geometry remains metric and the
    furniture shows true north; only its presentation bearing changes.
    """

    if geometry.is_empty:
        _fail("course geometry cannot be empty during page fitting.")
    min_x, min_y, max_x, max_y = geometry.bounds
    if max_x - min_x <= 0 or max_y - min_y <= 0:
        _fail("course geometry must span both local-metre axes.")

    candidates: list[tuple[float, int, PointTuple, float, float]] = []
    for rotation_deg in range(-90, 90):
        rotated = affinity.rotate(geometry, rotation_deg, origin=(0.0, 0.0))
        rotated_min_x, rotated_min_y, rotated_max_x, rotated_max_y = rotated.bounds
        span_x = rotated_max_x - rotated_min_x
        span_y = rotated_max_y - rotated_min_y
        required = max(
            1_000.0 * span_x / rect.width,
            1_000.0 * span_y / rect.height,
        )
        candidates.append(
            (
                required,
                rotation_deg,
                (
                    (rotated_min_x + rotated_max_x) / 2.0,
                    (rotated_min_y + rotated_max_y) / 2.0,
                ),
                span_x,
                span_y,
            )
        )
    required, rotation_deg, rotated_centre, span_x, span_y = min(
        candidates,
        key=lambda value: (value[0], abs(value[1]), value[1]),
    )
    denominator = (
        math.ceil((required - 1e-9) / SCALE_DENOMINATOR_INCREMENT)
        * SCALE_DENOMINATOR_INCREMENT
    )
    utilisation = (
        1_000.0 * span_x / denominator / rect.width,
        1_000.0 * span_y / denominator / rect.height,
    )
    return denominator, rotated_centre, rotation_deg, utilisation


def _hatches(
    geometry: BaseGeometry, *, spacing_mm: float, angle_deg: float
) -> list[list[PointTuple]]:
    result: list[list[PointTuple]] = []
    for polygon in _polygon_parts(geometry):
        if polygon.area <= 0.2:
            continue
        rotated = affinity.rotate(polygon, -angle_deg, origin=polygon.centroid)
        min_x, min_y, max_x, max_y = rotated.bounds
        y = math.floor(min_y / spacing_mm) * spacing_mm
        while y <= max_y + 1e-9:
            probe = LineString([(min_x - spacing_mm, y), (max_x + spacing_mm, y)])
            clipped = probe.intersection(rotated)
            restored = affinity.rotate(clipped, angle_deg, origin=polygon.centroid)
            for line in _line_parts(restored):
                simplified = line.simplify(HATCH_SIMPLIFY_MM, preserve_topology=True)
                if len(simplified.coords) >= 2:
                    result.append(
                        [(float(x), float(y_value)) for x, y_value in simplified.coords]
                    )
            y += spacing_mm
    return result


def _principal_axis_angle(geometry: BaseGeometry) -> float:
    """Return the bearing of the longest side of a polygon's fitted rectangle."""

    polygons = _polygon_parts(geometry)
    if not polygons:
        return 0.0
    polygon = max(polygons, key=lambda value: value.area)
    rectangle = polygon.minimum_rotated_rectangle
    if not isinstance(rectangle, Polygon):
        return 0.0
    coordinates = list(rectangle.exterior.coords)
    edges = [
        (
            math.dist(coordinates[index], coordinates[index + 1]),
            coordinates[index],
            coordinates[index + 1],
        )
        for index in range(len(coordinates) - 1)
    ]
    _length, start, end = max(edges, key=lambda value: value[0])
    return math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))


def _surface_hatches(
    geometry: BaseGeometry,
    *,
    spacing_mm: float,
    angle_offset_deg: float,
    inset_mm: float,
) -> list[list[PointTuple]]:
    """Create restrained, polygon-clipped fine-line surface texture."""

    result: list[list[PointTuple]] = []
    for polygon in _polygon_parts(geometry):
        inset = polygon.buffer(-inset_mm)
        if inset.is_empty:
            continue
        angle = _principal_axis_angle(polygon) + angle_offset_deg
        result.extend(_hatches(inset, spacing_mm=spacing_mm, angle_deg=angle))
    return result


def _fallback_green_fill(geometry: BaseGeometry) -> list[list[PointTuple]]:
    """Choose a longest legal interior fill stroke for each tiny green part."""

    result: list[list[PointTuple]] = []
    for polygon in _polygon_parts(geometry):
        safe = polygon.buffer(-GREEN_FILL_INSET_MM)
        if safe.is_empty:
            continue
        base_angle = _principal_axis_angle(polygon)
        candidates: list[list[PointTuple]] = []
        for angle_offset in (48.0, -48.0, 0.0, 90.0):
            candidates.extend(
                _hatches(
                    safe,
                    spacing_mm=GREEN_FALLBACK_PROBE_SPACING_MM,
                    angle_deg=base_angle + angle_offset,
                )
            )
        legal = [
            candidate
            for candidate in candidates
            if polyline_length_mm(candidate) + 1e-9 >= 0.75
        ]
        if legal:
            result.append(max(legal, key=polyline_length_mm))
    return result


def _water_stipple(
    geometry: BaseGeometry,
    *,
    spacing_mm: float = WATER_STIPPLE_SPACING_MM,
    minimum_distance_mm: float = 2.45,
) -> list[list[PointTuple]]:
    """Return a deterministic field of physical, closed blue stipple marks.

    Deterministic dart throwing with a spatial hash creates an irregular,
    blue-noise-like distribution with no underlying row lattice.  It is clipped
    to a conservative negative buffer of each source polygon, so marks never
    cross a shoreline or create misleading internal bands.  Tiny/narrow
    polygons retain their outline but receive no physically unplottable dot.
    """

    result: list[list[PointTuple]] = []
    radius = WATER_STIPPLE_RADIUS_MM
    spacing = spacing_mm
    for polygon in _polygon_parts(geometry):
        safe = polygon.buffer(-0.35)
        if safe.is_empty:
            continue
        prepared = prep(safe)
        prepared_source = prep(polygon)
        min_x, min_y, max_x, max_y = safe.bounds
        before = len(result)
        target = max(1, int(math.ceil(safe.area / (spacing * spacing))))
        minimum_distance = minimum_distance_mm
        cell_size = minimum_distance
        accepted: list[PointTuple] = []
        cells: dict[tuple[int, int], list[PointTuple]] = {}
        bounds_seed = (
            int(round(abs(min_x) * 1_000.0))
            ^ (int(round(abs(min_y) * 1_000.0)) << 7)
            ^ (int(round(abs(max_x) * 1_000.0)) << 13)
            ^ (int(round(abs(max_y) * 1_000.0)) << 19)
        ) & 0xFFFFFFFF
        state = bounds_seed or 0x9E3779B9

        def random_fraction() -> float:
            nonlocal state
            state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
            return state / 4_294_967_296.0

        bbox_area = max((max_x - min_x) * (max_y - min_y), 1e-9)
        fill_fraction = max(min(safe.area / bbox_area, 1.0), 0.01)
        maximum_attempts = min(
            800_000,
            max(160, int(math.ceil(target * 45.0 / fill_fraction))),
        )
        for _attempt in range(maximum_attempts):
            if len(accepted) >= target:
                break
            x = min_x + random_fraction() * (max_x - min_x)
            y = min_y + random_fraction() * (max_y - min_y)
            point = Point(x, y)
            if not prepared.covers(point):
                continue
            cell = (math.floor(x / cell_size), math.floor(y / cell_size))
            if any(
                math.dist((x, y), neighbour) < minimum_distance
                for x_offset in (-1, 0, 1)
                for y_offset in (-1, 0, 1)
                for neighbour in cells.get((cell[0] + x_offset, cell[1] + y_offset), [])
            ):
                continue
            physical_mark = point.buffer(radius + 0.125, quad_segs=12)
            if not prepared_source.covers(physical_mark):
                continue
            accepted.append((x, y))
            cells.setdefault(cell, []).append((x, y))
        result.extend(circle_stroke(point, radius, segments=12) for point in accepted)
        if len(result) == before:
            centre = safe.representative_point()
            physical_mark = centre.buffer(radius + 0.125, quad_segs=12)
            if prepared_source.covers(physical_mark):
                result.append(
                    circle_stroke(
                        (float(centre.x), float(centre.y)),
                        radius,
                        segments=12,
                    )
                )
    return result


def _linear_water_stipple(
    geometry: BaseGeometry,
    *,
    spacing_mm: float = LINEAR_WATER_STIPPLE_SPACING_MM,
    source_key: str | None = None,
) -> list[list[PointTuple]]:
    """Place legal closed dot symbols directly on sourced linear water.

    A line-only OSM waterway has no sourced width, so buffering and filling it
    would invent an extent.  These symbols retain the exact source centreline:
    each centre is interpolated on that line, with a midpoint mark retained for
    a physically short visible fragment.
    """

    result: list[list[PointTuple]] = []
    if source_key is None:
        first_distance = spacing_mm / 2.0
    else:
        seed = int.from_bytes(
            hashlib.sha256(source_key.encode("utf-8")).digest()[:4], "big"
        )
        edge_clearance = min(0.35, spacing_mm / 4.0)
        first_distance = edge_clearance + (seed / 0xFFFFFFFF) * (
            spacing_mm - 2.0 * edge_clearance
        )
    for line in _line_parts(geometry):
        if line.length <= 1e-9:
            continue
        distances: list[float] = []
        distance = first_distance
        while distance < line.length - 1e-9:
            distances.append(distance)
            distance += spacing_mm
        if not distances:
            distances.append(line.length / 2.0)
        for distance in distances:
            point = line.interpolate(distance)
            result.append(
                circle_stroke(
                    (float(point.x), float(point.y)),
                    WATER_STIPPLE_RADIUS_MM,
                    segments=12,
                )
            )
    return result


def _source_anchored_water_stipple(
    geometry: BaseGeometry,
) -> list[list[PointTuple]]:
    """Return one legal symbolic dot centred inside each tiny source polygon."""

    result: list[list[PointTuple]] = []
    for polygon in _polygon_parts(geometry):
        centre = polygon.representative_point()
        result.append(
            circle_stroke(
                (float(centre.x), float(centre.y)),
                WATER_STIPPLE_RADIUS_MM,
                segments=12,
            )
        )
    return result


def _coastline_water_side(
    geometry: BaseGeometry, *, band_width_m: float
) -> BaseGeometry:
    """Derive the sea-side stipple mask from oriented OSM coastline ways.

    OpenStreetMap coastline direction is a semantic part of the source: land
    lies to the left and water to the right.  A negative single-sided buffer
    therefore makes a bounded water-side symbol mask without closing or
    plotting an invented shoreline.  Only the sourced coastline itself is
    emitted as an outline.
    """

    parts = [
        line.buffer(
            -band_width_m,
            single_sided=True,
            cap_style="flat",
            join_style="mitre",
        )
        for line in _line_parts(geometry)
        if len(line.coords) >= 2 and line.length > 0
    ]
    return unary_union(parts) if parts else GeometryCollection()


def _morton_key(point: PointTuple, rect: Rect) -> int:
    x = max(0, min(1023, int(1023 * (point[0] - rect.left) / rect.width)))
    y = max(0, min(1023, int(1023 * (point[1] - rect.top) / rect.height)))

    def split(value: int) -> int:
        value &= 0x3FF
        value = (value | value << 16) & 0x030000FF
        value = (value | value << 8) & 0x0300F00F
        value = (value | value << 4) & 0x030C30C3
        value = (value | value << 2) & 0x09249249
        return value

    return split(x) | (split(y) << 1)


def _optimise_layer(layer: ArtworkLayer, rect: Rect) -> None:
    def centre(record: StrokeRecord) -> PointTuple:
        xs = [point[0] for point in record.points]
        ys = [point[1] for point in record.points]
        return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)

    layer.records.sort(
        key=lambda record: (
            _morton_key(centre(record), rect),
            record.source_ref or "",
            record.role or "",
        )
    )
    pending = list(layer.records)
    ordered: list[StrokeRecord] = []
    current = (rect.left, rect.top)
    while pending:
        best_index = 0
        best_reverse = False
        best_distance = math.inf
        for index, record in enumerate(pending):
            start = record.points[0]
            dx = current[0] - start[0]
            dy = current[1] - start[1]
            start_distance = dx * dx + dy * dy
            if start_distance < best_distance:
                best_index = index
                best_reverse = False
                best_distance = start_distance
            if record.points[0] != record.points[-1]:
                end = record.points[-1]
                dx = current[0] - end[0]
                dy = current[1] - end[1]
                end_distance = dx * dx + dy * dy
                if end_distance < best_distance:
                    best_index = index
                    best_reverse = True
                    best_distance = end_distance
        record = pending.pop(best_index)
        if best_reverse:
            record.points.reverse()
        ordered.append(record)
        current = record.points[-1]
    layer.records = ordered


def _dedupe_physical_records(layers: Sequence[ArtworkLayer]) -> None:
    """Remove coincident segments across logical layers sharing one real pen.

    OSM surface polygons often share part of a boundary (green/fairway, tee/
    fairway, overlapping multipolygon members). Plotting both source objects is
    truthful, but traversing their coincident segment twice needlessly deposits
    ink. Keep the first path's provenance and retain only the non-coincident
    remainder of later paths. Crossings remain untouched.
    """

    used_by_pen: dict[str, BaseGeometry] = {}
    for layer in layers:
        minimum = 3.0 * layer.pen.mark_width_mm
        retained: list[StrokeRecord] = []
        used = used_by_pen.get(layer.pen_id, GeometryCollection())
        for record in layer.records:
            if record.vector_path is not None:
                retained.append(record)
                continue
            line = set_precision(LineString(record.points), 0.001)
            if _is_water_dot_role(record.role):
                # Dots are already globally separated and are atomic symbols.
                # Same-pen crossings have zero shared length, so difference()
                # can only damage the loop rather than remove a retrace.
                retained.append(record)
                used = unary_union([used, line])
                continue
            # Unary union also removes a retraced portion within one polyline.
            unique_line = unary_union(line)
            remainder = unique_line if used.is_empty else unique_line.difference(used)
            remainder = set_precision(remainder, 0.001)
            used = unary_union([used, unique_line])
            for part in _line_parts(remainder):
                points = [(float(x), float(y)) for x, y in part.coords]
                if len(points) < 2 or part.length + 1e-9 < minimum:
                    continue
                retained.append(
                    StrokeRecord(
                        points=points,
                        source_ref=record.source_ref,
                        role=record.role,
                        sequence=record.sequence,
                        attributes=dict(record.attributes),
                    )
                )
        layer.records = retained
        used_by_pen[layer.pen_id] = used


class _StrokeSink:
    def __init__(self) -> None:
        self._seen: dict[str, set[tuple[PointTuple, ...]]] = {}

    @staticmethod
    def _key(points: Sequence[PointTuple]) -> tuple[PointTuple, ...]:
        rounded = tuple((round(x, 3), round(y, 3)) for x, y in points)
        reversed_key = tuple(reversed(rounded))
        return min(rounded, reversed_key)

    def add(
        self,
        layer: ArtworkLayer,
        points: Sequence[PointTuple],
        *,
        source_ref: str | None,
        role: str,
        attributes: dict[str, str],
        sequence: int | None = None,
    ) -> bool:
        if (
            len(points) < 2
            or polyline_length_mm(points) + 1e-9 < 3.0 * layer.pen.mark_width_mm
        ):
            return False
        key = self._key(points)
        seen = self._seen.setdefault(layer.pen_id, set())
        if key in seen:
            return False
        seen.add(key)
        layer.add(
            points,
            source_ref=source_ref,
            role=role,
            sequence=sequence,
            attributes=attributes,
        )
        return True


def _source_attributes(feature: dict[str, Any], kind: str) -> dict[str, str]:
    return {
        "data-feature-kind": kind,
        "data-source-version": str(feature["source_version"]),
        "data-source-timestamp": str(feature["source_timestamp"]),
        "data-evidence-tier": "source-object",
        "data-claim-status": "source-derived-not-survey",
    }


def _pattern_attributes(feature: dict[str, Any], kind: str) -> dict[str, str]:
    return {
        "data-feature-kind": kind,
        "data-source-version": str(feature["source_version"]),
        "data-source-timestamp": str(feature["source_timestamp"]),
        "data-evidence-tier": "symbol-derived-from-source-polygon",
        "data-claim-status": "symbolic-fill-within-source-polygon",
    }


def _scale_bar_length(denominator: int) -> tuple[float, int]:
    for metres in (500, 250, 200, 100, 50):
        length = metres * 1_000.0 / denominator
        if 16.0 <= length <= 45.0:
            return length, metres
    metres = 100
    return metres * 1_000.0 / denominator, metres


def _add_reference_furniture(
    artwork: PlateArtwork,
    furniture_rect: Rect,
    denominator: int,
    rotation_deg: float,
    sink: _StrokeSink,
) -> list[Rect]:
    layer = artwork.layer("map_reference", "North and scale", "black-0-4")
    attrs = {
        "data-evidence-tier": "derived-from-shared-projection",
        "data-claim-status": "metric-map-reference",
        "data-course-page-rotation-deg": f"{rotation_deg:g}",
    }
    angle = math.radians(rotation_deg)
    north_vector = (-math.sin(angle), -math.cos(angle))
    perpendicular = (-north_vector[1], north_vector[0])
    north_centre = (furniture_rect.right - 7.0, furniture_rect.centre[1])
    north_tip = (
        north_centre[0] + 3.8 * north_vector[0],
        north_centre[1] + 3.8 * north_vector[1],
    )
    north_tail = (
        north_centre[0] - 2.8 * north_vector[0],
        north_centre[1] - 2.8 * north_vector[1],
    )
    sink.add(
        layer,
        [north_tail, north_tip],
        source_ref="projection:true-north",
        role="north-arrow",
        attributes={
            **attrs,
            "data-north-is-page-up": str(rotation_deg == 0).lower(),
            "data-north-page-vector": (f"{north_vector[0]:.6f},{north_vector[1]:.6f}"),
        },
    )
    head_base = (
        north_tip[0] - 1.55 * north_vector[0],
        north_tip[1] - 1.55 * north_vector[1],
    )
    sink.add(
        layer,
        [
            (
                head_base[0] + 0.85 * perpendicular[0],
                head_base[1] + 0.85 * perpendicular[1],
            ),
            north_tip,
            (
                head_base[0] - 0.85 * perpendicular[0],
                head_base[1] - 0.85 * perpendicular[1],
            ),
        ],
        source_ref="projection:true-north",
        role="north-arrow-head",
        attributes={
            **attrs,
            "data-north-is-page-up": str(rotation_deg == 0).lower(),
        },
    )
    add_text(
        layer,
        "N",
        x_mm=furniture_rect.right - 18.0,
        y_mm=furniture_rect.centre[1] - 1.6,
        preferred_cap_mm=3.2,
        minimum_cap_mm=3.2,
        maximum_width_mm=4.0,
        anchor="middle",
        source_ref="projection:true-north",
        role="north-label",
        attributes={
            **attrs,
            "data-north-is-page-up": str(rotation_deg == 0).lower(),
        },
    )

    bar_length, metres = _scale_bar_length(denominator)
    bar_x = furniture_rect.left + 2.0
    bar_y = furniture_rect.bottom - 2.2
    sink.add(
        layer,
        [(bar_x, bar_y), (bar_x + bar_length, bar_y)],
        source_ref="projection:scale",
        role="scale-bar",
        attributes={
            **attrs,
            "data-scale-denominator": str(denominator),
            "data-scale-distance-m": str(metres),
        },
    )
    for x in (bar_x, bar_x + bar_length / 2.0, bar_x + bar_length):
        sink.add(
            layer,
            [(x, bar_y - 2.0), (x, bar_y + 2.0)],
            source_ref="projection:scale",
            role="scale-bar-tick",
            attributes={**attrs, "data-scale-denominator": str(denominator)},
        )
    add_text(
        layer,
        f"{metres} M",
        x_mm=bar_x + bar_length / 2.0,
        y_mm=furniture_rect.top + 0.4,
        preferred_cap_mm=3.2,
        minimum_cap_mm=3.2,
        maximum_width_mm=max(bar_length, 14.0),
        anchor="middle",
        source_ref="projection:scale",
        role="scale-label",
        attributes={**attrs, "data-scale-denominator": str(denominator)},
    )
    return []


def _rects_overlap(first: Rect, second: Rect, gap: float = 0.8) -> bool:
    return not (
        first.right + gap <= second.left
        or second.right + gap <= first.left
        or first.bottom + gap <= second.top
        or second.bottom + gap <= first.top
    )


def _marker_placements(
    holes: list[tuple[int, dict[str, Any], LineString]],
    map_rect: Rect,
    reserved: list[Rect],
    obstacles: BaseGeometry,
) -> list[tuple[int, dict[str, Any], PointTuple, PointTuple]]:
    placed_geometry: list[BaseGeometry] = []
    placed_leaders: list[BaseGeometry] = []
    result: list[tuple[int, dict[str, Any], PointTuple, PointTuple]] = []
    course_centre = map_rect.centre
    prepared_obstacles = None if obstacles.is_empty else prep(obstacles)

    def candidates_for(
        line: LineString,
    ) -> list[tuple[float, float, int, PointTuple, BaseGeometry]]:
        endpoint = (float(line.coords[-1][0]), float(line.coords[-1][1]))
        radial = math.atan2(
            endpoint[1] - course_centre[1], endpoint[0] - course_centre[0]
        )
        preferred_angles = [
            radial,
            radial + math.pi / 4,
            radial - math.pi / 4,
            radial + math.pi / 2,
            radial - math.pi / 2,
            radial + math.pi,
        ]
        angles = [
            *preferred_angles,
            *[index * math.pi / 16.0 for index in range(32)],
        ]
        seen: set[tuple[float, float]] = set()
        candidates: list[tuple[float, float, int, PointTuple, BaseGeometry]] = []
        candidate_index = 0
        for radius in (
            7.0,
            10.0,
            13.5,
            17.0,
            21.0,
            26.0,
            32.0,
            39.0,
            47.0,
            56.0,
            66.0,
            78.0,
        ):
            for angle_value in angles:
                centre = (
                    endpoint[0] + radius * math.cos(angle_value),
                    endpoint[1] + radius * math.sin(angle_value),
                )
                key = (round(centre[0], 3), round(centre[1], 3))
                if key in seen:
                    continue
                seen.add(key)
                margin = LABEL_RADIUS_MM + LABEL_CLEARANCE_MM
                if not (
                    map_rect.left + margin <= centre[0] <= map_rect.right - margin
                    and map_rect.top + margin <= centre[1] <= map_rect.bottom - margin
                ):
                    continue
                footprint_geometry = Point(centre).buffer(margin, quad_segs=12)
                footprint_rect = Rect(
                    centre[0] - margin,
                    centre[1] - margin,
                    2.0 * margin,
                    2.0 * margin,
                )
                if any(
                    _rects_overlap(footprint_rect, occupied) for occupied in reserved
                ):
                    continue
                overlap = float(
                    prepared_obstacles is not None
                    and prepared_obstacles.intersects(footprint_geometry)
                )
                candidates.append(
                    (
                        overlap,
                        radius,
                        candidate_index,
                        centre,
                        footprint_geometry,
                    )
                )
                candidate_index += 1
        return candidates

    prepared: list[
        tuple[
            int,
            int,
            dict[str, Any],
            LineString,
            list[tuple[float, float, int, PointTuple, BaseGeometry]],
        ]
    ] = []
    for number, feature, line in holes:
        candidates = candidates_for(line)
        clean_count = sum(candidate[0] <= 1e-9 for candidate in candidates)
        prepared.append((clean_count, number, feature, line, candidates))

    prepared.sort(
        key=lambda item: (
            item[0],
            item[1],
        )
    )
    for _clean_count, number, feature, line, candidates in prepared:
        endpoint = (float(line.coords[-1][0]), float(line.coords[-1][1]))
        available = [
            candidate
            for candidate in candidates
            if not any(
                candidate[4].intersects(occupied) for occupied in placed_geometry
            )
            and not any(candidate[4].intersects(leader) for leader in placed_leaders)
            and not any(
                LineString([endpoint, candidate[3]]).intersects(occupied)
                for occupied in placed_geometry
            )
        ]
        if available:
            _overlap, _radius, _index, selected, selected_geometry = min(
                available,
                key=lambda candidate: (candidate[0], candidate[1], candidate[2]),
            )
        else:
            _fail(
                f"hole {number} has no non-overlapping marker position inside "
                "the binding map field."
            )
        placed_geometry.append(selected_geometry)
        placed_leaders.append(LineString([endpoint, selected]))
        result.append((number, feature, endpoint, selected))
    return sorted(result, key=lambda item: item[0])


def _mask_records_around_labels(
    layers: Sequence[ArtworkLayer],
    centres: Sequence[PointTuple],
) -> dict[str, int]:
    """Create a paper-white clearance halo around every plotted hole number."""

    if not centres:
        return {
            "records_trimmed": 0,
            "records_preserved_whole": 0,
            "subnib_fragments_dropped": 0,
        }
    mask = unary_union(
        [
            Point(centre).buffer(
                LABEL_RADIUS_MM + LABEL_CLEARANCE_MM + LABEL_MASK_OVERCUT_MM,
                quad_segs=16,
            )
            for centre in centres
        ]
    )
    records_trimmed = 0
    records_preserved_whole = 0
    subnib_fragments_dropped = 0
    for layer in layers:
        if layer.id in {"hole_markers", "hole_numbers"}:
            continue
        minimum = 3.0 * layer.pen.mark_width_mm
        retained: list[StrokeRecord] = []
        for record in layer.records:
            if record.vector_path is not None:
                raise MapPlotterError(
                    "Golf label masking cannot safely rewrite an exact vector path."
                )
            source_line = LineString(record.points)
            if not source_line.intersects(mask):
                retained.append(record)
                continue
            if _is_water_dot_role(record.role):
                # A stipple mark is an atomic symbol. Cutting the closed loop
                # would leave a confusing crescent beside a label.
                records_trimmed += 1
                continue
            parts = _line_parts(source_line.difference(mask))
            qualifying = [part for part in parts if part.length + 1e-9 >= minimum]
            subnib_fragments_dropped += len(parts) - len(qualifying)
            if not qualifying:
                if record.attributes.get("data-claim-status") == (
                    "symbolic-fill-within-source-polygon"
                ):
                    # Pattern marks are a derived cartographic texture, not a
                    # source boundary.  Omitting a mark beneath a number halo
                    # preserves both meaning and physical label clearance.
                    records_trimmed += 1
                    continue
                # Never silently erase a complete sourced record.  The hard
                # overlap check below will reject the plate and force a better
                # marker placement if this fallback is ever needed.
                retained.append(record)
                records_preserved_whole += 1
                continue
            records_trimmed += 1
            for part in qualifying:
                attributes = dict(record.attributes)
                attributes["data-label-clearance-mask"] = "true"
                retained.append(
                    StrokeRecord(
                        points=[(float(x), float(y)) for x, y in part.coords],
                        source_ref=record.source_ref,
                        role=record.role,
                        sequence=record.sequence,
                        attributes=attributes,
                    )
                )
        layer.records = retained
    return {
        "records_trimmed": records_trimmed,
        "records_preserved_whole": records_preserved_whole,
        "subnib_fragments_dropped": subnib_fragments_dropped,
    }


def _label_feature_overlap_mm(
    layers: Sequence[ArtworkLayer],
    centres: Sequence[PointTuple],
) -> float:
    if not centres:
        return 0.0
    label_ink = unary_union(
        [Point(centre).buffer(LABEL_RADIUS_MM, quad_segs=16) for centre in centres]
    )
    overlap = 0.0
    for layer in layers:
        if layer.id in {"hole_markers", "hole_numbers", "map_reference"}:
            continue
        for record in layer.records:
            overlap += LineString(record.points).intersection(label_ink).length
    return overlap


def _layout_record(record: dict[str, Any]) -> tuple[list[ArtworkLayer], dict[str, Any]]:
    context = context_for(FORMAT_ID)
    map_rect = context.field.inset(float(context.plate["gap_mm"]))
    model = record["model"]
    features: list[tuple[dict[str, Any], str, BaseGeometry]] = []
    for feature in model["features"]:
        kind = _feature_kind(feature["tags"])
        if kind is None:
            continue
        geometry = _paths_geometry(feature["paths"])
        if not geometry.is_empty:
            features.append((feature, kind, geometry))

    play_geometries = [
        geometry
        for _feature, kind, geometry in features
        if kind
        in {"golf:hole", "golf:fairway", "golf:green", "golf:tee", "golf:bunker"}
    ]
    if not play_geometries:
        _fail(f"{record['id']} has no playing geometry.")
    model_boundary_geometry = _paths_geometry(model["boundary"])
    selection_method = str(record["evidence"]["selection_method"])
    local_holes = unary_union(
        [geometry for _feature, kind, geometry in features if kind == "golf:hole"]
    )
    local_playing_context = local_holes.buffer(COURSE_WATER_CONTEXT_DISTANCE_M)
    # Preserve every complete sourced hole even when an OSM multipolygon member
    # is open at the acquisition bbox or the published course boundary is a
    # little tighter than its mapped centreline.  The small buffer is only a
    # context-selection clip and is never emitted as claimed geometry.
    local_course_clip = model_boundary_geometry.union(local_holes.buffer(30.0))
    if "intersected-with" in selection_method:
        local_course_clip = (
            local_holes.buffer(130.0)
            .intersection(model_boundary_geometry)
            .union(local_holes.buffer(30.0))
        )
    # Build a restrained cartographic envelope from the exact 18 source routes
    # and nearby mapped playing surfaces.  The catalog root boundary remains a
    # context-selection mask: across the series it variously describes a
    # property parcel, several courses, or open extraction linework, so it is
    # neither a reliable nor a visually useful course outline.
    envelope_surface_parts = [
        (feature, geometry.intersection(local_course_clip))
        for feature, kind, geometry in features
        if kind in {"golf:fairway", "golf:green", "golf:tee", "golf:bunker"}
        and geometry.intersects(local_playing_context)
    ]
    playing_envelope_source_refs = sorted(
        {
            str(feature["source_ref"])
            for feature, kind, _geometry in features
            if kind == "golf:hole"
        }
        | {
            str(feature["source_ref"])
            for feature, geometry in envelope_surface_parts
            if not geometry.is_empty
        }
    )
    playing_envelope_core = unary_union(
        [
            local_holes.buffer(PLAYING_ENVELOPE_HOLE_BUFFER_M),
            *[
                geometry
                for _feature, geometry in envelope_surface_parts
                if not geometry.is_empty
            ],
        ]
    )
    playing_envelope = concave_hull(
        playing_envelope_core,
        ratio=PLAYING_ENVELOPE_CONCAVE_HULL_RATIO,
        allow_holes=False,
    ).buffer(PLAYING_ENVELOPE_OUTSET_M, quad_segs=8, join_style="round")
    if playing_envelope.is_empty or not _polygon_parts(playing_envelope):
        _fail(f"{record['id']} could not derive a playing-area envelope.")

    # Fit the complete playing course, hazards, water, and the modest symbolic
    # envelope—not the administrative-looking source perimeter.  Including the
    # envelope keeps its complete closed outline inside the map field.
    fit_kinds = {
        "golf:hole",
        "golf:fairway",
        "golf:green",
        "golf:tee",
        "golf:bunker",
        "golf:water_hazard",
        "golf:lateral_water_hazard",
        "water",
        "waterway",
    }
    fit_parts: list[BaseGeometry] = []
    for _feature, kind, geometry in features:
        if kind not in fit_kinds:
            continue
        if kind in {"golf:fairway", "golf:green", "golf:tee", "golf:bunker"} and (
            not geometry.intersects(local_playing_context)
        ):
            continue
        selected_geometry = geometry
        if kind in {"water", "waterway"}:
            selected_geometry = selected_geometry.intersection(
                local_holes.buffer(COURSE_WATER_CONTEXT_DISTANCE_M)
            )
        selected_geometry = selected_geometry.intersection(local_course_clip)
        if not selected_geometry.is_empty:
            fit_parts.append(selected_geometry)
    fit_geometry = unary_union(
        [
            *[geometry for geometry in fit_parts if not geometry.is_empty],
            playing_envelope,
        ]
    )
    denominator, rotated_centre, rotation_deg, utilisation = _page_fit(
        fit_geometry, map_rect
    )
    scale = 1_000.0 / denominator
    clip_geometry = box(map_rect.left, map_rect.top, map_rect.right, map_rect.bottom)
    course_clip = _paper_geometry(
        local_course_clip,
        rotated_centre=rotated_centre,
        rotation_deg=rotation_deg,
        paper_rect=map_rect,
        scale_mm_per_m=scale,
    ).intersection(clip_geometry)
    paper_hole_lines = _paper_geometry(
        local_holes,
        rotated_centre=rotated_centre,
        rotation_deg=rotation_deg,
        paper_rect=map_rect,
        scale_mm_per_m=scale,
    ).intersection(clip_geometry)
    paper_hole_corridor = paper_hole_lines.buffer(
        ROUTE_TEXTURE_CLEARANCE_MM, cap_style="round"
    )
    paper_green_hole_corridor = paper_hole_lines.buffer(
        GREEN_ROUTE_TEXTURE_CLEARANCE_MM,
        cap_style="round",
    )
    layers: dict[str, ArtworkLayer] = {}

    def layer(layer_id: str, label: str, pen_id: str) -> ArtworkLayer:
        if layer_id not in layers:
            layers[layer_id] = ArtworkLayer(layer_id, label, pen_id)
        return layers[layer_id]

    path_layer = layer("course_paths", "Cart paths and footways", "grey-0-25")
    bunker_layer = layer("bunkers", "Bunker outlines and sand hachures", "grey-0-25")
    envelope_layer = layer(
        "playing_envelope",
        "Illustrative playing-area envelope",
        "grey-0-4",
    )
    fairway_layer = layer(
        "fairways",
        "Fairway source outlines only",
        "green-0-25",
    )
    target_layer = layer(
        "greens_and_tees",
        "Green and tee outlines",
        "green-0-4",
    )
    water_stipple_layer = layer(
        "water_stipple", "Dotted water fills and linear-water symbols", "blue-0-25"
    )
    water_layer = layer("water", "Water and hazards", "blue-0-4")
    marker_layer = layer("hole_markers", "Hole number markers", "red-0-25")
    copy_layer = layer("hole_numbers", "Hole numbers", "black-0-4")
    building_layer = layer("course_buildings", "Course buildings", "black-0-4")
    hole_layer = layer("holes", "Numbered hole centrelines", "gold-1")
    sink = _StrokeSink()

    transformed_holes: list[tuple[int, dict[str, Any], LineString]] = []
    label_obstacles: list[BaseGeometry] = []
    emitted_counts: dict[str, int] = {}
    water_dot_cells: dict[tuple[int, int], list[tuple[PointTuple, StrokeRecord]]] = {}
    visible_water_source_modes: dict[str, str] = {}
    water_symbol_sources: dict[
        str, tuple[BaseGeometry, BaseGeometry, dict[str, str], str]
    ] = {}
    water_dot_role_counts: dict[str, int] = {}
    visible_green_source_refs: set[str] = set()
    green_fill_sources: dict[str, tuple[BaseGeometry, dict[str, str]]] = {}

    paper_playing_envelope = _paper_geometry(
        playing_envelope,
        rotated_centre=rotated_centre,
        rotation_deg=rotation_deg,
        paper_rect=map_rect,
        scale_mm_per_m=scale,
    ).intersection(clip_geometry)
    envelope_attrs = {
        "data-feature-kind": "golf:playing-envelope",
        "data-evidence-tier": "symbol-derived-from-source-playing-geometry",
        "data-claim-status": (
            "illustrative-envelope-not-property-or-official-course-boundary"
        ),
        "data-derived-from": (
            "18-source-hole-centrelines-and-nearby-mapped-playing-surfaces"
        ),
        "data-source-refs": ",".join(playing_envelope_source_refs),
    }
    for polygon in _polygon_parts(paper_playing_envelope):
        outline = LineString(polygon.exterior.coords).simplify(
            PLAYING_ENVELOPE_SIMPLIFY_MM,
            preserve_topology=True,
        )
        sink.add(
            envelope_layer,
            [(float(x), float(y)) for x, y in outline.coords],
            source_ref=None,
            role="playing-area-envelope",
            attributes=envelope_attrs,
        )

    # The A3 landmark-building contract caps contextual footprints at 32.
    # Restrict those slots to clubhouses and named buildings, prioritising the
    # explicitly golf-tagged clubhouse and then larger source footprints.
    building_limit = int(context.plate["landmark_buildings"]["max_objects"])
    building_candidates = [
        (kind != "golf:clubhouse", -geometry.area, str(feature["source_ref"]))
        for feature, kind, geometry in features
        if kind in {"building", "golf:clubhouse"}
        and (kind == "golf:clubhouse" or bool(feature["tags"].get("name")))
    ]
    selected_buildings = {
        source_ref
        for _generic_first, _negative_area, source_ref in sorted(building_candidates)[
            :building_limit
        ]
    }

    def count(kind: str) -> None:
        emitted_counts[kind] = emitted_counts.get(kind, 0) + 1

    def add_water_dot(
        dot: list[PointTuple],
        *,
        source_ref: str,
        role: str,
        attributes: dict[str, str],
        representation_geometry: BaseGeometry,
    ) -> bool:
        """Add one water dot, merging lineage only on shared source geometry."""

        dot_centre = (
            (min(x for x, _y in dot) + max(x for x, _y in dot)) / 2.0,
            (min(y for _x, y in dot) + max(y for _x, y in dot)) / 2.0,
        )
        physical_radius = WATER_STIPPLE_RADIUS_MM + 0.125
        if not clip_geometry.buffer(-physical_radius).covers(Point(dot_centre)):
            return False
        cell = (
            math.floor(dot_centre[0] / WATER_STIPPLE_MINIMUM_DISTANCE_MM),
            math.floor(dot_centre[1] / WATER_STIPPLE_MINIMUM_DISTANCE_MM),
        )
        collisions: list[tuple[PointTuple, StrokeRecord]] = []
        for x_offset in (-1, 0, 1):
            for y_offset in (-1, 0, 1):
                for neighbour, record_value in water_dot_cells.get(
                    (cell[0] + x_offset, cell[1] + y_offset), []
                ):
                    if (
                        math.dist(dot_centre, neighbour)
                        < WATER_STIPPLE_MINIMUM_DISTANCE_MM
                    ):
                        collisions.append((neighbour, record_value))
        if collisions:
            colliding_record: StrokeRecord | None = None
            for neighbour, record_value in collisions:
                if _water_dot_represents_geometry(
                    neighbour,
                    representation_geometry,
                    role,
                ):
                    colliding_record = record_value
                    break
            if colliding_record is None:
                minimum_distance = (
                    WATER_STIPPLE_MINIMUM_DISTANCE_MM
                    if role == "water-area-stipple-dot"
                    else WATER_STIPPLE_PHYSICAL_MINIMUM_DISTANCE_MM
                )
                if any(
                    math.dist(dot_centre, neighbour) < minimum_distance
                    for neighbour, _record_value in collisions
                ):
                    return False
            else:
                represented = set(
                    filter(
                        None,
                        colliding_record.attributes.get(
                            "data-represented-source-refs",
                            colliding_record.source_ref or "",
                        ).split(","),
                    )
                )
                represented.add(source_ref)
                colliding_record.attributes["data-represented-source-refs"] = ",".join(
                    sorted(represented)
                )
                return True

        dot_attrs = dict(attributes)
        dot_attrs["data-represented-source-refs"] = source_ref
        if not sink.add(
            water_stipple_layer,
            dot,
            source_ref=source_ref,
            role=role,
            attributes=dot_attrs,
        ):
            return False
        record_value = water_stipple_layer.records[-1]
        water_dot_cells.setdefault(cell, []).append((dot_centre, record_value))
        water_dot_role_counts[role] = water_dot_role_counts.get(role, 0) + 1
        return True

    for feature, kind, source_geometry in features:
        if kind in {"golf:fairway", "golf:green", "golf:tee", "golf:bunker"} and (
            not source_geometry.intersects(local_playing_context)
        ):
            continue
        source_is_polygonal = bool(_polygon_parts(source_geometry))
        coastline_geometry = (
            source_geometry if feature["tags"].get("natural") == "coastline" else None
        )
        if kind in {"water", "waterway"}:
            source_geometry = source_geometry.intersection(
                local_holes.buffer(COURSE_WATER_CONTEXT_DISTANCE_M)
            )
            if source_geometry.is_empty:
                continue
        paper_full = _paper_geometry(
            source_geometry,
            rotated_centre=rotated_centre,
            rotation_deg=rotation_deg,
            paper_rect=map_rect,
            scale_mm_per_m=scale,
        )
        paper = paper_full.intersection(clip_geometry).intersection(course_clip)
        if paper.is_empty:
            continue
        paper = paper.simplify(0.035, preserve_topology=True)
        outline_source = (
            source_geometry.boundary
            if isinstance(source_geometry, (Polygon, MultiPolygon))
            else source_geometry
        )
        paper_outline = (
            _paper_geometry(
                outline_source,
                rotated_centre=rotated_centre,
                rotation_deg=rotation_deg,
                paper_rect=map_rect,
                scale_mm_per_m=scale,
            )
            .intersection(clip_geometry)
            .intersection(course_clip)
        )
        paper_outline = paper_outline.simplify(0.035, preserve_topology=True)
        attrs = _source_attributes(feature, kind)
        source_ref = str(feature["source_ref"])
        is_area_water = kind in {
            "golf:water_hazard",
            "golf:lateral_water_hazard",
            "water",
        } or (kind == "waterway" and source_is_polygonal)

        if (
            kind
            in {
                "golf:fairway",
                "golf:green",
                "golf:tee",
                "golf:bunker",
                "golf:water_hazard",
                "golf:lateral_water_hazard",
                "water",
                "building",
                "golf:clubhouse",
            }
            or is_area_water
        ):
            label_obstacles.append(paper)
        elif kind in {
            "golf:hole",
            "golf:cartpath",
            "golf:path",
            "path",
            "waterway",
        }:
            label_obstacles.append(paper_outline.buffer(0.8, cap_style="round"))
        elif kind.startswith("vegetation:"):
            label_obstacles.append(paper_outline.buffer(0.4, cap_style="round"))

        if kind == "golf:hole":
            number = _leading_hole_number(feature["tags"].get("ref"))
            lines = _line_parts(paper_outline)
            if number is None or not lines:
                continue
            line_value = max(lines, key=lambda line_item: line_item.length)
            points = [(float(x), float(y)) for x, y in line_value.coords]
            if sink.add(
                hole_layer,
                points,
                source_ref=source_ref,
                role="hole-centreline",
                sequence=number,
                attributes={**attrs, "data-hole-number": str(number)},
            ):
                transformed_holes.append((number, feature, line_value))
                count(kind)
            continue

        if kind in {"golf:fairway", "golf:green", "golf:tee"}:
            surface_role = kind.split(":", 1)[1]
            surface_layer = fairway_layer if kind == "golf:fairway" else target_layer
            outline_emitted = False
            for outline in _line_parts(paper_outline):
                if sink.add(
                    surface_layer,
                    [(float(x), float(y)) for x, y in outline.coords],
                    source_ref=source_ref,
                    role=f"{surface_role}-outline",
                    attributes=attrs,
                ):
                    outline_emitted = True
                    count(kind)
            if kind != "golf:green":
                # Fairways and tees are deliberately outline-only.  The green
                # is the sole filled playing surface, matching conventional
                # course-diagram hierarchy without turning the page into wash.
                continue
            pattern_attrs = _pattern_attributes(feature, kind)
            if not outline_emitted:
                continue
            visible_green_source_refs.add(source_ref)
            green_fill_sources[source_ref] = (paper, pattern_attrs)
            green_fill_geometry = paper.difference(paper_green_hole_corridor)
            fill_emitted = False
            for hatch in _surface_hatches(
                green_fill_geometry,
                spacing_mm=GREEN_HATCH_SPACING_MM,
                angle_offset_deg=48.0,
                inset_mm=GREEN_FILL_INSET_MM,
            ):
                fill_emitted = (
                    sink.add(
                        fairway_layer,
                        hatch,
                        source_ref=source_ref,
                        role="green-fine-line-fill",
                        attributes=pattern_attrs,
                    )
                    or fill_emitted
                )
            if not fill_emitted:
                for hatch in _fallback_green_fill(green_fill_geometry):
                    fill_emitted = (
                        sink.add(
                            fairway_layer,
                            hatch,
                            source_ref=source_ref,
                            role="green-fine-line-fill",
                            attributes=pattern_attrs,
                        )
                        or fill_emitted
                    )
            continue

        if kind == "golf:bunker":
            for outline in _line_parts(paper_outline):
                if sink.add(
                    bunker_layer,
                    [(float(x), float(y)) for x, y in outline.coords],
                    source_ref=source_ref,
                    role="bunker-outline",
                    attributes=attrs,
                ):
                    count(kind)
            pattern_attrs = _pattern_attributes(feature, kind)
            for hatch in _hatches(
                paper.buffer(-0.35).difference(paper_hole_corridor),
                spacing_mm=2.6,
                angle_deg=-48.0,
            ):
                sink.add(
                    bunker_layer,
                    hatch,
                    source_ref=source_ref,
                    role="bunker-hachure",
                    attributes=pattern_attrs,
                )
            continue

        if is_area_water:
            if not _polygon_parts(paper) and not _line_parts(paper_outline):
                continue
            is_coastline = coastline_geometry is not None
            visible_water_source_modes[source_ref] = (
                "oriented-coastline-area" if is_coastline else "polygonal-water"
            )
            feature_emitted = False
            for outline in _line_parts(paper_outline):
                if sink.add(
                    water_layer,
                    [(float(x), float(y)) for x, y in outline.coords],
                    source_ref=source_ref,
                    role="water-outline",
                    attributes=attrs,
                ):
                    feature_emitted = True
            stipple_geometry = paper
            stipple_spacing = WATER_STIPPLE_SPACING_MM
            stipple_minimum_distance = WATER_STIPPLE_MINIMUM_DISTANCE_MM
            pattern_attrs = _pattern_attributes(feature, kind)
            if coastline_geometry is not None:
                sea_mask = _coastline_water_side(
                    coastline_geometry,
                    band_width_m=(
                        map_rect.width * SEA_STIPPLE_BAND_FIELD_FRACTION / scale
                    ),
                )
                stipple_geometry = _paper_geometry(
                    sea_mask,
                    rotated_centre=rotated_centre,
                    rotation_deg=rotation_deg,
                    paper_rect=map_rect,
                    scale_mm_per_m=scale,
                ).intersection(clip_geometry)
                stipple_spacing = SEA_STIPPLE_SPACING_MM
                stipple_minimum_distance = SEA_STIPPLE_MINIMUM_DISTANCE_MM
                pattern_attrs["data-water-symbol"] = (
                    "osm-oriented-coastline-water-right"
                )
                pattern_attrs["data-evidence-tier"] = (
                    "symbol-derived-from-oriented-source-coastline"
                )
                pattern_attrs["data-claim-status"] = (
                    "symbolic-water-side-fill-not-a-sourced-area"
                )
            water_symbol_sources[source_ref] = (
                stipple_geometry,
                paper_outline,
                dict(pattern_attrs),
                visible_water_source_modes[source_ref],
            )
            represented = False
            area_dot_geometry = stipple_geometry.difference(paper_hole_corridor)
            for dot in _water_stipple(
                area_dot_geometry,
                spacing_mm=stipple_spacing,
                minimum_distance_mm=stipple_minimum_distance,
            ):
                represented = (
                    add_water_dot(
                        dot,
                        source_ref=source_ref,
                        role="water-area-stipple-dot",
                        attributes=pattern_attrs,
                        representation_geometry=area_dot_geometry,
                    )
                    or represented
                )

            # Some sourced hazards are narrower than the physical diameter of
            # a legal 0.25 mm-pen dot.  Mark their exact source boundary rather
            # than inventing a filled width or silently leaving water undotted.
            if not represented and not is_coastline:
                narrow_attrs = {
                    **attrs,
                    "data-evidence-tier": "symbol-derived-from-source-water-boundary",
                    "data-claim-status": (
                        "symbolic-dots-on-source-boundary-for-physically-narrow-water"
                    ),
                    "data-water-symbol": "narrow-source-boundary-stipple",
                }
                for dot in _linear_water_stipple(paper_outline, source_key=source_ref):
                    represented = (
                        add_water_dot(
                            dot,
                            source_ref=source_ref,
                            role="water-narrow-boundary-stipple-dot",
                            attributes=narrow_attrs,
                            representation_geometry=paper_outline,
                        )
                        or represented
                    )
            if not represented and _polygon_parts(paper):
                anchored_attrs = {
                    **attrs,
                    "data-evidence-tier": "symbol-anchored-inside-source-water-polygon",
                    "data-claim-status": (
                        "source-anchored-dot-for-water-too-narrow-for-contained-mark"
                    ),
                    "data-water-symbol": "physically-narrow-source-anchored-stipple",
                }
                for dot in _source_anchored_water_stipple(paper):
                    represented = (
                        add_water_dot(
                            dot,
                            source_ref=source_ref,
                            role="water-narrow-source-stipple-dot",
                            attributes=anchored_attrs,
                            representation_geometry=paper,
                        )
                        or represented
                    )
            if feature_emitted or represented:
                count(kind)
            continue

        if kind == "waterway":
            if not _line_parts(paper_outline):
                # A source line can touch the context clip at one mathematical
                # point.  That is not visible linework and cannot carry a
                # source-centred physical symbol.
                continue
            visible_water_source_modes[source_ref] = "linear-waterway"
            linear_attrs = {
                **attrs,
                "data-evidence-tier": "symbol-derived-from-source-waterway-line",
                "data-claim-status": (
                    "closed-dot-symbols-centred-on-source-line-no-inferred-width"
                ),
                "data-water-symbol": "source-line-centred-stipple",
            }
            water_symbol_sources[source_ref] = (
                paper_outline,
                paper_outline,
                dict(linear_attrs),
                visible_water_source_modes[source_ref],
            )
            represented = False
            for dot in _linear_water_stipple(paper_outline, source_key=source_ref):
                represented = (
                    add_water_dot(
                        dot,
                        source_ref=source_ref,
                        role="water-linear-stipple-dot",
                        attributes=linear_attrs,
                        representation_geometry=paper_outline,
                    )
                    or represented
                )
            if represented:
                count(kind)
            continue

        if kind in {"golf:cartpath", "golf:path", "path"}:
            if kind == "path" and not feature["tags"].get("name"):
                continue
            for line_value in _line_parts(paper_outline):
                if sink.add(
                    path_layer,
                    [(float(x), float(y)) for x, y in line_value.coords],
                    source_ref=source_ref,
                    role="course-path",
                    attributes=attrs,
                ):
                    count(kind)
            continue

        if kind == "building" or kind == "golf:clubhouse":
            if source_ref not in selected_buildings:
                continue
            for outline in _line_parts(paper_outline):
                if outline.length < 3.0:
                    continue
                if sink.add(
                    building_layer,
                    [(float(x), float(y)) for x, y in outline.coords],
                    source_ref=source_ref,
                    role="building-outline",
                    attributes=attrs,
                ):
                    count(kind)
            continue

        if kind == "golf:rough":
            continue

        if kind.startswith("vegetation:"):
            # The previous edition's many interlocking woodland polygons read
            # as a second course boundary.  They remain in the frozen source
            # contract but are intentionally not emitted in this course-first
            # edition.
            continue

    if sorted(number for number, _feature, _line in transformed_holes) != list(
        range(1, 19)
    ):
        _fail(f"{record['id']} did not emit all 18 complete source hole lines.")

    artwork = PlateArtwork(
        subject_id=str(record["id"]),
        domain="golf",
        subject_kind="map",
        title=str(record["title"]),
        subtitle=str(record["subtitle"]),
        details=(),
        credit_line="© OpenStreetMap contributors / ODbL-1.0",
        scale_status=f"metric-plan-scale-1:{denominator}-rotated-to-fit",
        evidence_status=str(record["evidence"]["status"]),
        rights_status=str(record["rights_status"]),
        sources=tuple(copy.deepcopy(record["sources"])),
        context=context,
        layers=list(layers.values()),
    )
    reserved = _add_reference_furniture(
        artwork,
        context.zones["furniture"],
        denominator,
        rotation_deg,
        sink,
    )
    plotted_obstacles = [
        LineString(stroke.points).buffer(
            layer_value.pen.mark_width_mm / 2.0,
            cap_style="round",
        )
        for layer_value in artwork.layers
        if layer_value.id not in {"hole_markers", "hole_numbers", "map_reference"}
        for stroke in layer_value.records
    ]
    obstacle_parts = [*label_obstacles, *plotted_obstacles]
    obstacles = unary_union(obstacle_parts) if obstacle_parts else GeometryCollection()
    placements = _marker_placements(
        transformed_holes,
        map_rect,
        reserved,
        obstacles,
    )
    for index, first in enumerate(placements):
        for second in placements[index + 1 :]:
            if math.dist(first[3], second[3]) <= 2.0 * (
                LABEL_RADIUS_MM + LABEL_CLEARANCE_MM
            ):
                _fail(
                    f"{record['id']} placed hole markers {first[0]} and "
                    f"{second[0]} without physical clearance."
                )
    label_centres = [centre for _number, _feature, _endpoint, centre in placements]
    masking = _mask_records_around_labels(
        artwork.layers,
        label_centres,
    )
    filled_green_source_refs = {
        stroke.source_ref
        for stroke in fairway_layer.records
        if stroke.role == "green-fine-line-fill" and stroke.source_ref is not None
    }
    physically_unfillable_green_source_refs: set[str] = set()
    green_label_mask = unary_union(
        [
            Point(centre).buffer(
                LABEL_RADIUS_MM + LABEL_CLEARANCE_MM + LABEL_MASK_OVERCUT_MM + 0.125,
                quad_segs=16,
            )
            for centre in label_centres
        ]
    )
    for missing_green_ref in sorted(
        visible_green_source_refs - filled_green_source_refs
    ):
        source_green, pattern_attrs = green_fill_sources[missing_green_ref]
        clear_green = (
            source_green.difference(paper_green_hole_corridor)
            .difference(green_label_mask)
            .intersection(clip_geometry)
        )
        fallback = _fallback_green_fill(clear_green)
        if not fallback:
            physically_unfillable_green_source_refs.add(missing_green_ref)
            continue
        for hatch in fallback:
            sink.add(
                fairway_layer,
                hatch,
                source_ref=missing_green_ref,
                role="green-fine-line-fill",
                attributes=pattern_attrs,
            )
    label_overlap_mm = _label_feature_overlap_mm(artwork.layers, label_centres)
    if label_overlap_mm > 1e-6 or masking["records_preserved_whole"]:
        _fail(
            f"{record['id']} could not keep every hole number clear of mapped ink "
            f"(overlap={label_overlap_mm:.6f} mm, masking={masking})."
        )
    for number, feature, endpoint, centre in placements:
        attrs = {
            **_source_attributes(feature, "golf:hole"),
            "data-hole-number": str(number),
        }
        vector_x = centre[0] - endpoint[0]
        vector_y = centre[1] - endpoint[1]
        distance = math.hypot(vector_x, vector_y)
        if distance > 3.1:
            scale_to_edge = LABEL_RADIUS_MM / distance
            leader_end = (
                centre[0] - vector_x * scale_to_edge,
                centre[1] - vector_y * scale_to_edge,
            )
            sink.add(
                marker_layer,
                [endpoint, leader_end],
                source_ref=str(feature["source_ref"]),
                role="hole-marker-leader",
                attributes=attrs,
            )
        sink.add(
            marker_layer,
            circle_stroke(centre, LABEL_RADIUS_MM, segments=24),
            source_ref=str(feature["source_ref"]),
            role="hole-marker",
            attributes=attrs,
            sequence=number,
        )
        add_text(
            copy_layer,
            str(number),
            x_mm=centre[0],
            y_mm=centre[1] - 1.6,
            preferred_cap_mm=3.2,
            minimum_cap_mm=3.2,
            maximum_width_mm=4.4,
            anchor="middle",
            source_ref=str(feature["source_ref"]),
            role="hole-number",
            attributes=attrs,
        )

    _dedupe_physical_records(
        [
            layer_value
            for layer_value in artwork.layers
            if layer_value is not marker_layer
        ]
    )
    final_filled_green_source_refs = {
        stroke.source_ref
        for stroke in fairway_layer.records
        if stroke.role == "green-fine-line-fill" and stroke.source_ref is not None
    }
    uncovered_fillable_green_source_refs = sorted(
        visible_green_source_refs
        - physically_unfillable_green_source_refs
        - final_filled_green_source_refs
    )
    if physically_unfillable_green_source_refs:
        _fail(
            f"{record['id']} has visible greens without a legal fine-line fill: "
            + ", ".join(sorted(physically_unfillable_green_source_refs))
            + "."
        )
    if uncovered_fillable_green_source_refs:
        _fail(
            f"{record['id']} left physically fillable greens without fill strokes: "
            + ", ".join(uncovered_fillable_green_source_refs)
            + "."
        )

    def final_water_coverage() -> tuple[set[str], dict[str, int]]:
        represented: set[str] = set()
        role_counts: dict[str, int] = {}
        for stroke in water_stipple_layer.records:
            if not _is_water_dot_role(stroke.role):
                continue
            role_counts[stroke.role or ""] = role_counts.get(stroke.role or "", 0) + 1
            represented.update(
                filter(
                    None,
                    stroke.attributes.get(
                        "data-represented-source-refs", stroke.source_ref or ""
                    ).split(","),
                )
            )
        return represented, role_counts

    represented_water_source_refs, final_water_dot_role_counts = final_water_coverage()
    uncovered_water_source_refs = sorted(
        set(visible_water_source_modes) - represented_water_source_refs
    )
    label_occluded_water_source_refs: set[str] = set()
    if uncovered_water_source_refs:
        # A label halo may remove the sole legal symbol from a short stream or
        # tiny hazard.  Re-index the surviving dots, then make one final source-
        # faithful placement pass outside every protected label disk.
        water_dot_cells.clear()
        for stroke in water_stipple_layer.records:
            if not _is_water_dot_role(stroke.role):
                continue
            centre = (
                (min(x for x, _y in stroke.points) + max(x for x, _y in stroke.points))
                / 2.0,
                (min(y for _x, y in stroke.points) + max(y for _x, y in stroke.points))
                / 2.0,
            )
            cell = (
                math.floor(centre[0] / WATER_STIPPLE_MINIMUM_DISTANCE_MM),
                math.floor(centre[1] / WATER_STIPPLE_MINIMUM_DISTANCE_MM),
            )
            water_dot_cells.setdefault(cell, []).append((centre, stroke))
        dot_label_mask = unary_union(
            [
                Point(centre).buffer(
                    LABEL_RADIUS_MM
                    + LABEL_CLEARANCE_MM
                    + LABEL_MASK_OVERCUT_MM
                    + WATER_STIPPLE_RADIUS_MM
                    + 0.125,
                    quad_segs=16,
                )
                for centre in label_centres
            ]
        )
        for missing_ref in uncovered_water_source_refs:
            fill_geometry, boundary_geometry, source_attrs, mode = water_symbol_sources[
                missing_ref
            ]
            clear_boundary_geometry = boundary_geometry.difference(dot_label_mask)
            if clear_boundary_geometry.is_empty:
                # The complete visible fragment falls beneath a binding label
                # clearance disk, so it is no longer a visible water source in
                # the final plate and cannot truthfully carry a source-centred
                # symbol outside that disk.
                label_occluded_water_source_refs.add(missing_ref)
                continue
            represented = False
            if mode == "linear-waterway":
                representation_geometry = clear_boundary_geometry
                candidates = _linear_water_stipple(
                    representation_geometry, source_key=missing_ref
                )
                role = "water-linear-stipple-dot"
            else:
                representation_geometry = fill_geometry.difference(dot_label_mask)
                candidates = _water_stipple(
                    representation_geometry,
                    spacing_mm=(
                        SEA_STIPPLE_SPACING_MM
                        if mode == "oriented-coastline-area"
                        else WATER_STIPPLE_SPACING_MM
                    ),
                    minimum_distance_mm=(
                        SEA_STIPPLE_MINIMUM_DISTANCE_MM
                        if mode == "oriented-coastline-area"
                        else WATER_STIPPLE_MINIMUM_DISTANCE_MM
                    ),
                )
                role = "water-area-stipple-dot"
            for dot in candidates:
                represented = (
                    add_water_dot(
                        dot,
                        source_ref=missing_ref,
                        role=role,
                        attributes=source_attrs,
                        representation_geometry=representation_geometry,
                    )
                    or represented
                )
            if not represented:
                narrow_attrs = {
                    **source_attrs,
                    "data-evidence-tier": "symbol-derived-from-source-water-boundary",
                    "data-claim-status": (
                        "symbolic-dots-on-source-boundary-for-physically-narrow-water"
                    ),
                    "data-water-symbol": "narrow-source-boundary-stipple",
                }
                for dot in _linear_water_stipple(
                    clear_boundary_geometry, source_key=missing_ref
                ):
                    represented = (
                        add_water_dot(
                            dot,
                            source_ref=missing_ref,
                            role="water-narrow-boundary-stipple-dot",
                            attributes=narrow_attrs,
                            representation_geometry=clear_boundary_geometry,
                        )
                        or represented
                    )
            if not represented and _polygon_parts(fill_geometry):
                clear_source_geometry = fill_geometry.difference(dot_label_mask)
                anchored_attrs = {
                    **source_attrs,
                    "data-evidence-tier": "symbol-anchored-inside-source-water-polygon",
                    "data-claim-status": (
                        "source-anchored-dot-for-water-too-narrow-for-contained-mark"
                    ),
                    "data-water-symbol": "physically-narrow-source-anchored-stipple",
                }
                for dot in _source_anchored_water_stipple(clear_source_geometry):
                    represented = (
                        add_water_dot(
                            dot,
                            source_ref=missing_ref,
                            role="water-narrow-source-stipple-dot",
                            attributes=anchored_attrs,
                            representation_geometry=clear_source_geometry,
                        )
                        or represented
                    )
            if not represented:
                safe_source_line = clear_boundary_geometry.intersection(
                    clip_geometry.buffer(-(WATER_STIPPLE_RADIUS_MM + 0.125 + 0.001))
                )
                rescue_role = (
                    "water-linear-stipple-dot"
                    if mode == "linear-waterway"
                    else "water-narrow-boundary-stipple-dot"
                )
                for dot in _linear_water_stipple(
                    safe_source_line, source_key=missing_ref
                ):
                    represented = (
                        add_water_dot(
                            dot,
                            source_ref=missing_ref,
                            role=rescue_role,
                            attributes=source_attrs,
                            representation_geometry=safe_source_line,
                        )
                        or represented
                    )
        represented_water_source_refs, final_water_dot_role_counts = (
            final_water_coverage()
        )
        uncovered_water_source_refs = sorted(
            set(visible_water_source_modes)
            - label_occluded_water_source_refs
            - represented_water_source_refs
        )
    if uncovered_water_source_refs:
        _fail(
            f"{record['id']} left visible water sources without dot symbols: "
            + ", ".join(uncovered_water_source_refs)
            + "."
        )
    marker_records = [
        stroke for stroke in marker_layer.records if stroke.role == "hole-marker"
    ]
    marker_numbers = [
        stroke.sequence for stroke in marker_records if stroke.sequence is not None
    ]
    if (
        len(marker_records) != 18
        or len(marker_numbers) != len(marker_records)
        or sorted(marker_numbers) != list(range(1, 19))
    ):
        _fail(
            f"{record['id']} did not retain exactly one physical marker for "
            "each hole after pen-level deduplication."
        )
    for layer_value in artwork.layers:
        if layer_value.records:
            _optimise_layer(layer_value, map_rect)
    artwork.layers = [
        layer_value for layer_value in artwork.layers if layer_value.records
    ]
    return artwork.layers, {
        "course_geometry_policy": (
            "pinned-source-objects-with-explicitly-symbolic-playing-envelope"
        ),
        "course_hole_count": 18,
        "course_holes_numbered": list(range(1, 19)),
        "course_orientation": "rotated-to-page-fit-with-true-north-arrow",
        "course_page_rotation_deg": rotation_deg,
        "north_page_vector": [
            round(-math.sin(math.radians(rotation_deg)), 6),
            round(-math.cos(math.radians(rotation_deg)), 6),
        ],
        "plan_scale_denominator": denominator,
        "fitted_geometry_working_rect_utilisation": {
            "width": round(utilisation[0], 6),
            "height": round(utilisation[1], 6),
            "maximum": round(max(utilisation), 6),
        },
        "fit_extent_policy": (
            "complete-playing-course-hazards-water-and-playing-envelope"
        ),
        "playing_surface_selection_policy": (
            "whole-source-fairway-green-tee-and-bunker-objects-intersecting"
            "-the-exact-18-hole-routes-within-60m"
        ),
        "map_field_inner_clearance_mm": float(context.plate["gap_mm"]),
        "scale_bar_status": "visible-derived-from-metric-plan-scale",
        "catalog_geometry_sha256": model["geometry_sha256"],
        "source_feature_counts": copy.deepcopy(record["evidence"]["feature_counts"]),
        "emitted_feature_counts": emitted_counts,
        "unmapped_features_invented": False,
        "course_boundary_emitted": False,
        "course_boundary_rendering": ("raw-root-boundary-omitted-selection-mask-only"),
        "playing_envelope_emitted": True,
        "playing_envelope_rendering": (
            "grey-0.40-derived-from-source-hole-routes-and-nearby-playing-surfaces"
            "-illustrative-not-property-or-official-boundary"
        ),
        "playing_envelope_source_refs": playing_envelope_source_refs,
        "fairway_rendering": "green-0.25-source-outline-only",
        "green_and_tee_rendering": (
            "green-0.40-source-outlines-with-green-only-green-0.25-fine-line-fill"
            "-tees-outline-only"
        ),
        "green_fill_coverage": {
            "fill_inset_mm": GREEN_FILL_INSET_MM,
            "fill_pen_nib_mm": 0.25,
            "gold_route_clearance_mm": GREEN_ROUTE_TEXTURE_CLEARANCE_MM,
            "visible_source_count": len(visible_green_source_refs),
            "filled_source_count": len(
                final_filled_green_source_refs & visible_green_source_refs
            ),
            "physically_unfillable_source_refs": sorted(
                physically_unfillable_green_source_refs
            ),
            "uncovered_fillable_source_refs": uncovered_fillable_green_source_refs,
        },
        "water_rendering": (
            "blue-0.40-area-outlines-with-blue-0.25-closed-dot-symbols-for-every"
            "-visible-area-linear-and-physically-narrow-water-source"
        ),
        "water_source_dot_coverage": {
            "visible_source_count": len(
                set(visible_water_source_modes) - label_occluded_water_source_refs
            ),
            "represented_source_count": len(
                represented_water_source_refs
                & (set(visible_water_source_modes) - label_occluded_water_source_refs)
            ),
            "uncovered_source_refs": uncovered_water_source_refs,
            "fully_occluded_by_label_clearance_source_refs": sorted(
                label_occluded_water_source_refs
            ),
            "visible_source_modes": {
                mode: sum(
                    1
                    for source_ref_value, value in visible_water_source_modes.items()
                    if value == mode
                    and source_ref_value not in label_occluded_water_source_refs
                )
                for mode in sorted(set(visible_water_source_modes.values()))
            },
            "dot_role_counts_generated": water_dot_role_counts,
            "dot_role_counts_final": final_water_dot_role_counts,
        },
        "coastline_rendering": (
            "source-outline-with-sparse-stipple-on-osm-oriented-water-right-side"
        ),
        "vegetation_rendering": "omitted-for-playing-course-legibility",
        "label_policy": "collision-scored-markers-with-paper-white-clearance-halos",
        "label_clearance_mm": LABEL_CLEARANCE_MM,
        "label_feature_overlap_mm": round(label_overlap_mm, 6),
        "label_masking": masking,
    }


def build_golf_plate(record: Any, format_id: str | None = None) -> PlateArtwork:
    """Build one complete, clarity-first A3 source-study golf map."""

    checked = validate_golf_record(record)
    selected = format_id or str(checked["format_id"])
    if selected != FORMAT_ID:
        raise MapPlotterError(
            f"The full-feature golf edition requires {FORMAT_ID}; got {selected!r}."
        )
    layers, rendering_metadata = _layout_record(checked)
    denominator = int(rendering_metadata["plan_scale_denominator"])
    rotation_deg = int(rendering_metadata["course_page_rotation_deg"])
    details = (
        "18 SOURCE HOLES / GREY OUTLINE ILLUSTRATIVE / NOT A BOUNDARY",
        f"PLAN 1:{denominator} / ROTATED {rotation_deg:+d} DEG / NORTH ARROW",
        f"{checked['championship_context']} / NOT A SURVEY",
    )
    sources = tuple(copy.deepcopy(checked["sources"]))
    osm_source = next(source for source in sources if source["kind"] == "openstreetmap")
    return PlateArtwork(
        subject_id=str(checked["id"]),
        domain="golf",
        subject_kind="map",
        title=str(checked["title"]),
        subtitle=str(checked["subtitle"]),
        details=details,
        credit_line="© OpenStreetMap contributors / ODbL-1.0",
        scale_status=f"metric-plan-scale-1:{denominator}-rotated-to-fit",
        evidence_status=str(checked["evidence"]["status"]),
        rights_status=str(checked["rights_status"]),
        sources=sources,
        context=context_for(selected),
        layers=layers,
        pen_order=GOLF_PENS,
        artifact_kind="golf-course-source-map",
        rendering_preset="golf-clarity-course-a3-v4",
        format_subject_policy=FORMAT_SUBJECT_POLICY,
        source_provider="OpenStreetMap contributors / official course profile",
        source_license="ODbL-1.0 / reference-only factual context",
        data_snapshot=str(
            checked.get(
                "data_snapshot", osm_source.get("snapshot_date", "source-undated")
            )
        ),
        notes=tuple(str(note) for note in checked["notes"])
        + (
            str(checked["evidence"]["statement"]),
            str(checked["selection_note"]),
        ),
        catalog_record=checked,
        rendering_metadata=rendering_metadata,
    )


__all__ = [
    "CATALOG_ID",
    "CATALOG_PATH",
    "FORMAT_ID",
    "GOLF_PENS",
    "build_golf_plate",
    "load_golf_catalog",
    "validate_golf_record",
]
