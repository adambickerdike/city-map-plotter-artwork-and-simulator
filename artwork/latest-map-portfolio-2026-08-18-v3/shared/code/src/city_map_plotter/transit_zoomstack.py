"""Pinned OS Open Zoomstack physical railway context.

Zoomstack is an excellent free national cartographic source, but its ``rail``
layer is not a connected routing graph and its feature IDs are not persistent.
This module therefore exposes physical linework only.  It must never be used to
infer an operator's passenger route between timetable calls.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import gzip
import hashlib
import json
from math import asinh, atan, degrees, floor, isfinite, pi, radians, sinh, tan
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.errors import GEOSException
from shapely.ops import linemerge, unary_union

from .models import MapPlotterError
from .transit_extent import COMPLETE_GB_BOUNDS_WGS84


ZOOMSTACK_PRODUCT_NAME = "OS_Open_Zoomstack"
ZOOMSTACK_RAIL_LAYER = "rail"
ZOOMSTACK_RAIL_TYPES = frozenset(
    {"Multi Track", "Single Track", "Narrow Gauge", "Tunnel"}
)
ZOOMSTACK_NATIONAL_CONTEXT_LAYERS = (
    "sea",
    "surfacewater",
    "roads",
    "boundaries",
    "names",
)
ZOOMSTACK_NATIONAL_ROAD_TYPES = frozenset({"Motorway", "Primary"})
ZOOMSTACK_NATIONAL_PLACE_TYPES = frozenset({"Capital", "City"})
DEFAULT_NATIONAL_CONTEXT_ZOOM = 6
DEFAULT_GB_BOUNDS = COMPLETE_GB_BOUNDS_WGS84
GB_CONTEXT_GEOGRAPHIC_SCOPE = (
    "Great Britain (England, Scotland and Wales, including detached islands); "
    "Northern Ireland excluded"
)
GB_CONTEXT_COUNTRY_NAMES = frozenset({"England", "Scotland", "Wales"})
# These points are selection evidence only. They are never emitted as geometry:
# all coastline vertices still come from the hash-pinned Zoomstack sea layer.
# Source-published Country labels take precedence when present; the fixed points
# make the territorial exclusion deterministic if a label is absent at a chosen
# context zoom.
_NON_GB_JURISDICTION_SELECTION_ANCHORS = {
    "Northern Ireland": (-6.6919, 54.6670),
    "Ireland": (-8.0000, 53.3000),
    "Isle of Man": (-4.5500, 54.2300),
    "Guernsey": (-2.5800, 49.4600),
    "Jersey": (-2.1300, 49.2100),
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class PhysicalRailFeature:
    geometry: tuple[tuple[float, float], ...]
    rail_type: str
    source_objects: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "geometry": [[lon, lat] for lon, lat in self.geometry],
            "rail_type": self.rail_type,
            "source_objects": list(self.source_objects),
        }


@dataclass(frozen=True, slots=True)
class NationalContextLine:
    """One exact, non-routing line in the quiet national backdrop."""

    geometry: tuple[tuple[float, float], ...]
    context_class: str
    source_layer: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "geometry": [[lon, lat] for lon, lat in self.geometry],
            "context_class": self.context_class,
            "source_layer": self.source_layer,
        }


@dataclass(frozen=True, slots=True)
class NationalContextPlace:
    """One OS-published city/capital point available to the label policy."""

    point: tuple[float, float]
    name: str
    place_type: str
    source_layer: str = "names"

    def as_dict(self) -> dict[str, Any]:
        return {
            "point": list(self.point),
            "name": self.name,
            "place_type": self.place_type,
            "source_layer": self.source_layer,
        }


@dataclass(frozen=True, slots=True)
class ZoomstackNationalContext:
    """Hash-pinned national context extracted from the same MBTiles bytes."""

    source_sha256: str
    zoom: int
    bounds_wgs84: tuple[float, float, float, float]
    lines: tuple[NationalContextLine, ...]
    places: tuple[NationalContextPlace, ...]
    audit: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ZoomstackPhysicalRail:
    source_path: Path
    source_sha256: str
    zoom: int
    bounds_wgs84: tuple[float, float, float, float]
    features: tuple[PhysicalRailFeature, ...]
    audit: dict[str, Any]
    national_context: ZoomstackNationalContext | None = None


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Cannot read Zoomstack MBTiles {path}: {exc}") from exc
    return digest.hexdigest()


def _decoder() -> Any:
    try:
        import mapbox_vector_tile
    except ModuleNotFoundError as exc:
        if exc.name != "mapbox_vector_tile":
            raise
        raise MapPlotterError(
            "OS Open Zoomstack vector tiles require the optional MIT-licensed "
            "mapbox-vector-tile dependency. Install city-map-plotter[zoomstack]."
        ) from exc
    return mapbox_vector_tile


def _validate_bounds(
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        raise MapPlotterError("Zoomstack bounds need west, south, east, north.")
    try:
        west, south, east, north = (float(value) for value in bounds)
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(
            "Zoomstack bounds need four finite numeric WGS84 values."
        ) from exc
    if not (-180.0 <= west < east <= 180.0 and -85.0 <= south < north <= 85.0):
        raise MapPlotterError("Zoomstack bounds are outside usable WGS84 limits.")
    return west, south, east, north


def _xyz_x(lon: float, zoom: int) -> int:
    count = 1 << zoom
    return min(count - 1, max(0, floor((lon + 180.0) / 360.0 * count)))


def _xyz_y(lat: float, zoom: int) -> int:
    count = 1 << zoom
    value = (1.0 - asinh(tan(radians(lat))) / pi) / 2.0 * count
    return min(count - 1, max(0, floor(value)))


def _tile_coordinate_to_wgs84(
    x: float,
    y_down: float,
    *,
    tile_x: int,
    xyz_y: int,
    zoom: int,
    extent: int,
) -> tuple[float, float]:
    count = 1 << zoom
    world_x = (tile_x + x / extent) / count
    world_y = (xyz_y + y_down / extent) / count
    lon = world_x * 360.0 - 180.0
    lat = degrees(atan(sinh(pi * (1.0 - 2.0 * world_y))))
    return (round(lon, 10), round(lat, 10))


def _line_parts(value: Any) -> Iterable[LineString]:
    if isinstance(value, LineString):
        yield value
    elif isinstance(value, MultiLineString):
        yield from value.geoms
    elif isinstance(value, GeometryCollection):
        for child in value.geoms:
            yield from _line_parts(child)


def _polygon_parts(value: Any) -> Iterable[Polygon]:
    if isinstance(value, Polygon):
        yield value
    elif isinstance(value, MultiPolygon):
        yield from value.geoms
    elif isinstance(value, GeometryCollection):
        for child in value.geoms:
            yield from _polygon_parts(child)


def _canonical_geometry(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    reverse = tuple(reversed(points))
    return min(points, reverse)


def national_context_geometry_sha256(
    lines: Iterable[NationalContextLine],
    places: Iterable[NationalContextPlace],
) -> str:
    payload = {
        "lines": [line.as_dict() for line in lines],
        "places": [place.as_dict() for place in places],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_context_metadata(metadata: dict[str, str]) -> None:
    try:
        layer_document = json.loads(metadata["json"])
        records = layer_document["vector_layers"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MapPlotterError("Zoomstack vector-layer metadata is malformed.") from exc
    if not isinstance(records, list):
        raise MapPlotterError("Zoomstack vector-layer metadata is malformed.")
    layers = {
        record.get("id"): record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    expected_fields = {
        "sea": {},
        "surfacewater": {},
        "roads": {
            "type": "String",
            "name": "String",
            "number": "String",
            "level": "Number",
        },
        "boundaries": {"type": "String"},
        "names": {
            "type": "String",
            "name1": "String",
            "name1language": "String",
            "name2": "String",
            "name2language": "String",
        },
    }
    for layer_id, fields in expected_fields.items():
        record = layers.get(layer_id)
        if not isinstance(record, dict) or record.get("fields") != fields:
            raise MapPlotterError(
                f"Zoomstack {layer_id} layer schema is missing or unexpected."
            )


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        records = list(connection.execute("SELECT name, value FROM metadata"))
    except sqlite3.Error as exc:
        raise MapPlotterError(f"Cannot read Zoomstack MBTiles metadata: {exc}") from exc
    names = [str(name) for name, _ in records]
    duplicate_names = sorted(
        name for name, count in Counter(names).items() if count > 1
    )
    if duplicate_names:
        raise MapPlotterError(
            "Zoomstack MBTiles repeats metadata key "
            + ", ".join(repr(name) for name in duplicate_names)
            + "."
        )
    values = {str(name): str(value) for name, value in records}
    if values.get("name") != ZOOMSTACK_PRODUCT_NAME:
        raise MapPlotterError("MBTiles is not the OS Open Zoomstack product.")
    if values.get("format") != "pbf":
        raise MapPlotterError("Zoomstack MBTiles format must be pbf.")
    if values.get("scheme", "tms").casefold() != "tms":
        raise MapPlotterError("Zoomstack MBTiles must use the TMS row scheme.")
    try:
        layer_document = json.loads(values["json"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MapPlotterError("Zoomstack vector-layer metadata is malformed.") from exc
    if not isinstance(layer_document, dict) or not isinstance(
        layer_document.get("vector_layers"), list
    ):
        raise MapPlotterError("Zoomstack vector-layer metadata is malformed.")
    layers: dict[str, dict[str, Any]] = {}
    for layer_index, record in enumerate(layer_document["vector_layers"]):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise MapPlotterError(
                f"Zoomstack vector-layer metadata record {layer_index} is malformed."
            )
        layer_id = record["id"]
        if layer_id in layers:
            raise MapPlotterError(
                f"Zoomstack vector-layer metadata repeats layer {layer_id!r}."
            )
        layers[layer_id] = record
    rail = layers.get(ZOOMSTACK_RAIL_LAYER)
    if not isinstance(rail, dict) or rail.get("fields") != {"type": "String"}:
        raise MapPlotterError("Zoomstack rail layer schema is missing or unexpected.")
    return values


def _feature_coordinate_sets(
    raw_feature: Any,
    *,
    feature_index: int,
) -> tuple[str, list[Any]]:
    if not isinstance(raw_feature, dict):
        raise MapPlotterError(
            f"Zoomstack rail feature {feature_index} must be an object."
        )
    properties = raw_feature.get("properties", {})
    if not isinstance(properties, dict):
        raise MapPlotterError(
            f"Zoomstack rail feature {feature_index} properties must be an object."
        )
    rail_type = properties.get("type")
    if rail_type not in ZOOMSTACK_RAIL_TYPES:
        raise MapPlotterError(
            f"Zoomstack rail feature has unexpected type {rail_type!r}."
        )
    raw_geometry = raw_feature.get("geometry")
    if not isinstance(raw_geometry, dict):
        raise MapPlotterError(
            f"Zoomstack rail feature {feature_index} geometry must be an object."
        )
    geometry_type = raw_geometry.get("type")
    coordinates = raw_geometry.get("coordinates")
    if geometry_type == "LineString":
        if not isinstance(coordinates, list):
            raise MapPlotterError(
                f"Zoomstack rail feature {feature_index} LineString coordinates "
                "must be an array."
            )
        coordinate_sets = [coordinates]
    elif geometry_type == "MultiLineString":
        if not isinstance(coordinates, list):
            raise MapPlotterError(
                f"Zoomstack rail feature {feature_index} MultiLineString coordinates "
                "must be an array."
            )
        coordinate_sets = coordinates
    else:
        raise MapPlotterError(
            f"Zoomstack rail feature uses unsupported {geometry_type!r}."
        )
    return str(rail_type), coordinate_sets


def _decoded_points(
    coordinates: Any,
    *,
    feature_index: int,
    part_index: int,
    tile_x: int,
    xyz_y: int,
    zoom: int,
    extent: int,
) -> tuple[tuple[float, float], ...]:
    if not isinstance(coordinates, list):
        raise MapPlotterError(
            f"Zoomstack rail feature {feature_index} part {part_index} must be "
            "an array of points."
        )
    result: list[tuple[float, float]] = []
    for point_index, point in enumerate(coordinates):
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or isinstance(point[0], bool)
            or isinstance(point[1], bool)
        ):
            raise MapPlotterError(
                f"Zoomstack rail feature {feature_index} part {part_index} point "
                f"{point_index} is invalid."
            )
        try:
            x = float(point[0])
            y = float(point[1])
        except (TypeError, ValueError) as exc:
            raise MapPlotterError(
                f"Zoomstack rail feature {feature_index} part {part_index} point "
                f"{point_index} is invalid."
            ) from exc
        if not isfinite(x) or not isfinite(y):
            raise MapPlotterError(
                f"Zoomstack rail feature {feature_index} part {part_index} point "
                f"{point_index} is non-finite."
            )
        result.append(
            _tile_coordinate_to_wgs84(
                x,
                y,
                tile_x=tile_x,
                xyz_y=xyz_y,
                zoom=zoom,
                extent=extent,
            )
        )
    return tuple(result)


def _context_point(
    raw_point: Any,
    *,
    layer_id: str,
    feature_index: int,
    tile_x: int,
    xyz_y: int,
    zoom: int,
    extent: int,
) -> tuple[float, float]:
    if (
        not isinstance(raw_point, (list, tuple))
        or len(raw_point) != 2
        or isinstance(raw_point[0], bool)
        or isinstance(raw_point[1], bool)
    ):
        raise MapPlotterError(
            f"Zoomstack {layer_id} feature {feature_index} has an invalid point."
        )
    try:
        x = float(raw_point[0])
        y = float(raw_point[1])
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Zoomstack {layer_id} feature {feature_index} has an invalid point."
        ) from exc
    if not isfinite(x) or not isfinite(y):
        raise MapPlotterError(
            f"Zoomstack {layer_id} feature {feature_index} has a non-finite point."
        )
    return _tile_coordinate_to_wgs84(
        x,
        y,
        tile_x=tile_x,
        xyz_y=xyz_y,
        zoom=zoom,
        extent=extent,
    )


def _context_points(
    raw_points: Any,
    *,
    layer_id: str,
    feature_index: int,
    tile_x: int,
    xyz_y: int,
    zoom: int,
    extent: int,
) -> tuple[tuple[float, float], ...]:
    if not isinstance(raw_points, list):
        raise MapPlotterError(
            f"Zoomstack {layer_id} feature {feature_index} coordinates must be an array."
        )
    return tuple(
        _context_point(
            point,
            layer_id=layer_id,
            feature_index=feature_index,
            tile_x=tile_x,
            xyz_y=xyz_y,
            zoom=zoom,
            extent=extent,
        )
        for point in raw_points
    )


def _context_line_coordinate_sets(
    raw_geometry: Any,
    *,
    layer_id: str,
    feature_index: int,
) -> list[Any]:
    if not isinstance(raw_geometry, dict):
        raise MapPlotterError(
            f"Zoomstack {layer_id} feature {feature_index} geometry must be an object."
        )
    geometry_type = raw_geometry.get("type")
    coordinates = raw_geometry.get("coordinates")
    if geometry_type == "LineString" and isinstance(coordinates, list):
        return [coordinates]
    if geometry_type == "MultiLineString" and isinstance(coordinates, list):
        return coordinates
    raise MapPlotterError(
        f"Zoomstack {layer_id} feature {feature_index} uses unsupported "
        f"{geometry_type!r}."
    )


def _context_polygon_coordinate_sets(
    raw_geometry: Any,
    *,
    layer_id: str,
    feature_index: int,
) -> list[Any]:
    if not isinstance(raw_geometry, dict):
        raise MapPlotterError(
            f"Zoomstack {layer_id} feature {feature_index} geometry must be an object."
        )
    geometry_type = raw_geometry.get("type")
    coordinates = raw_geometry.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        return [coordinates]
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        return coordinates
    raise MapPlotterError(
        f"Zoomstack {layer_id} feature {feature_index} uses unsupported "
        f"{geometry_type!r}."
    )


def _on_selection_edge(
    first: tuple[float, float],
    second: tuple[float, float],
    bounds: tuple[float, float, float, float],
) -> bool:
    west, south, east, north = bounds
    tolerance = 1e-8
    return any(
        abs(first[axis] - value) <= tolerance and abs(second[axis] - value) <= tolerance
        for axis, value in ((0, west), (0, east), (1, south), (1, north))
    )


def _without_selection_edges(
    points: tuple[tuple[float, float], ...],
    bounds: tuple[float, float, float, float],
) -> list[tuple[tuple[float, float], ...]]:
    """Remove clip-box edges from polygon banks without touching real coast."""

    pieces: list[tuple[tuple[float, float], ...]] = []
    current: list[tuple[float, float]] = []
    for first, second in zip(points, points[1:], strict=False):
        if _on_selection_edge(first, second, bounds):
            if len(current) >= 2:
                pieces.append(tuple(current))
            current = []
            continue
        if not current:
            current.append(first)
        elif current[-1] != first:
            pieces.append(tuple(current))
            current = [first]
        current.append(second)
    if len(current) >= 2:
        pieces.append(tuple(current))
    return pieces


def _merged_context_lines(
    geometries: Iterable[tuple[tuple[float, float], ...]],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    unique = {
        _canonical_geometry(geometry)
        for geometry in geometries
        if len(geometry) >= 2 and any(point != geometry[0] for point in geometry[1:])
    }
    if not unique:
        return ()
    try:
        merged = (
            LineString(next(iter(unique)))
            if len(unique) == 1
            else linemerge(MultiLineString(sorted(unique)))
        )
    except (GEOSException, ValueError) as exc:
        raise MapPlotterError(
            f"Cannot assemble exact-endpoint Zoomstack context linework: {exc}"
        ) from exc
    result = {
        _canonical_geometry(
            tuple((round(float(x), 10), round(float(y), 10)) for x, y in part.coords)
        )
        for part in _line_parts(merged)
        if len(part.coords) >= 2
    }
    return tuple(sorted(result))


def _polygon_bank_lines(
    polygons: Iterable[Polygon],
    *,
    bounds: tuple[float, float, float, float],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    polygon_list = list(polygons)
    if not polygon_list:
        return ()
    try:
        dissolved = unary_union(polygon_list)
        boundary = dissolved.boundary
    except (GEOSException, ValueError) as exc:
        raise MapPlotterError(
            f"Cannot dissolve Zoomstack polygon banks: {exc}"
        ) from exc
    pieces: list[tuple[tuple[float, float], ...]] = []
    for line in _line_parts(boundary):
        points = tuple(
            (round(float(x), 10), round(float(y), 10)) for x, y in line.coords
        )
        pieces.extend(_without_selection_edges(points, bounds))
    return _merged_context_lines(pieces)


def _component_indices_covering_points(
    components: tuple[Polygon, ...],
    points: Iterable[tuple[float, float]],
) -> set[int]:
    result: set[int] = set()
    for point in points:
        marker = Point(point)
        for component_index, component in enumerate(components):
            if component.buffer(1e-9).covers(marker):
                result.add(component_index)
                break
    return result


def _great_britain_coastline_lines(
    sea_polygons: Iterable[Polygon],
    *,
    bounds: tuple[float, float, float, float],
    gb_country_anchor_points: Iterable[tuple[float, float]],
    non_gb_country_anchor_points: Iterable[tuple[float, float]],
) -> tuple[tuple[tuple[tuple[float, float], ...], ...], dict[str, Any]]:
    """Select a truthful Great Britain coastline, including detached islands.

    England, Scotland and Wales labels select the Great Britain seed land. A
    mainland-only filter would discard Anglesey, the Isle of Wight, Scilly,
    Hebridean, Orkney and Shetland coastlines, so remaining complete land
    components are assigned to their nearest GB or non-GB seed. Components cut
    by the rectangular source boundary are non-GB seeds: complete GB coastlines
    fit inside ``DEFAULT_GB_BOUNDS``, while Ireland and continental land are cut
    by it. Fixed non-GB jurisdiction points only classify components; they do
    not author, move or simplify a single coastline vertex.
    """

    polygon_list = list(sea_polygons)
    gb_anchors = tuple(gb_country_anchor_points)
    if not polygon_list or not gb_anchors:
        return _polygon_bank_lines(polygon_list, bounds=bounds), {
            "geographic_scope": "requested bounds; GB jurisdiction unverified",
            "northern_ireland_included": None,
            "northern_ireland_exclusion_verified": False,
            "territory_selection_mode": "unfiltered-sea-bank-fallback",
            "land_component_count": 0,
            "anchored_gb_land_component_count": 0,
            "included_detached_island_component_count": 0,
            "excluded_land_component_count": 0,
            "authored_coastline_geometry_used": False,
        }
    selection = box(*bounds)
    try:
        land = selection.difference(unary_union(polygon_list))
    except (GEOSException, ValueError) as exc:
        raise MapPlotterError(
            f"Cannot derive Zoomstack Great Britain land from sea polygons: {exc}"
        ) from exc
    components = tuple(
        sorted(
            (
                component
                for component in _polygon_parts(land)
                if not component.is_empty and component.area > 0.0
            ),
            key=lambda item: (
                tuple(round(float(value), 10) for value in item.bounds),
                round(float(item.area), 12),
            ),
        )
    )
    gb_seed_indices = _component_indices_covering_points(components, gb_anchors)
    if not gb_seed_indices:
        raise MapPlotterError(
            "Zoomstack sea geometry does not contain the OS England/Scotland/Wales "
            "country-name anchors on Great Britain land."
        )

    source_non_gb_anchors = tuple(non_gb_country_anchor_points)
    selection_non_gb_anchors = tuple(
        point
        for point in _NON_GB_JURISDICTION_SELECTION_ANCHORS.values()
        if selection.covers(Point(point))
    )
    non_gb_seed_indices = _component_indices_covering_points(
        components,
        (*source_non_gb_anchors, *selection_non_gb_anchors),
    )
    conflicting = gb_seed_indices & non_gb_seed_indices
    if conflicting:
        raise MapPlotterError(
            "Zoomstack land cannot separate Great Britain from a non-GB "
            "jurisdiction anchor."
        )

    # A land component clipped by the requested rectangle is not a complete
    # island outline and must never be silently presented as part of GB.
    boundary_seed_indices = {
        index
        for index, component in enumerate(components)
        if component.boundary.distance(selection.boundary) <= 1e-9
    } - gb_seed_indices
    non_gb_seed_indices.update(boundary_seed_indices)

    try:
        gb_seed_land = unary_union(
            [components[index] for index in sorted(gb_seed_indices)]
        )
        non_gb_seed_land = (
            unary_union([components[index] for index in sorted(non_gb_seed_indices)])
            if non_gb_seed_indices
            else GeometryCollection()
        )
    except (GEOSException, ValueError) as exc:
        raise MapPlotterError(
            f"Cannot assemble Zoomstack territory-selection seeds: {exc}"
        ) from exc

    included_island_indices: set[int] = set()
    excluded_island_indices: set[int] = set()
    for component_index, component in enumerate(components):
        if component_index in gb_seed_indices or component_index in non_gb_seed_indices:
            continue
        gb_distance = float(component.distance(gb_seed_land))
        non_gb_distance = (
            float("inf")
            if non_gb_seed_land.is_empty
            else float(component.distance(non_gb_seed_land))
        )
        if gb_distance + 1e-10 < non_gb_distance:
            included_island_indices.add(component_index)
        else:
            # Ties are conservatively excluded rather than passed off as GB.
            excluded_island_indices.add(component_index)

    selected_indices = gb_seed_indices | included_island_indices
    selected = [components[index] for index in sorted(selected_indices)]
    northern_ireland_anchor = Point(
        _NON_GB_JURISDICTION_SELECTION_ANCHORS["Northern Ireland"]
    )
    northern_ireland_component_indices = {
        index
        for index, component in enumerate(components)
        if component.buffer(1e-9).covers(northern_ireland_anchor)
    }
    northern_ireland_exclusion_verified = bool(
        northern_ireland_component_indices
    ) and northern_ireland_component_indices.isdisjoint(selected_indices)
    if selection.covers(northern_ireland_anchor) and not (
        northern_ireland_exclusion_verified
    ):
        raise MapPlotterError(
            "Zoomstack Great Britain selection could not verify the explicit "
            "Northern Ireland exclusion."
        )
    try:
        boundary = unary_union(selected).boundary
    except (GEOSException, ValueError) as exc:
        raise MapPlotterError(
            f"Cannot derive Zoomstack Great Britain coastline: {exc}"
        ) from exc
    pieces: list[tuple[tuple[float, float], ...]] = []
    for line in _line_parts(boundary):
        points = tuple(
            (round(float(x), 10), round(float(y), 10)) for x, y in line.coords
        )
        pieces.extend(_without_selection_edges(points, bounds))
    lines = _merged_context_lines(pieces)
    excluded_indices = (non_gb_seed_indices | excluded_island_indices) - gb_seed_indices
    return lines, {
        "geographic_scope": GB_CONTEXT_GEOGRAPHIC_SCOPE,
        "northern_ireland_included": False,
        "northern_ireland_exclusion_verified": (
            northern_ireland_exclusion_verified
            if selection.covers(northern_ireland_anchor)
            else False
        ),
        "territory_selection_mode": "country-seeds-plus-nearest-detached-island",
        "land_component_count": len(components),
        "anchored_gb_land_component_count": len(gb_seed_indices),
        "included_detached_island_component_count": len(included_island_indices),
        "excluded_land_component_count": len(excluded_indices),
        "excluded_detached_island_component_count": len(excluded_island_indices),
        "selection_boundary_seed_component_count": len(boundary_seed_indices),
        "source_non_gb_country_anchor_count": len(source_non_gb_anchors),
        "house_non_gb_selection_anchor_count": len(selection_non_gb_anchors),
        "island_assignment_policy": (
            "complete unanchored land component assigned to nearest GB or non-GB "
            "seed; distance ties excluded"
        ),
        "authored_coastline_geometry_used": False,
    }


def _load_national_context(
    connection: sqlite3.Connection,
    decoder: Any,
    metadata: dict[str, str],
    *,
    source_sha256: str,
    zoom: int,
    bounds: tuple[float, float, float, float],
) -> ZoomstackNationalContext:
    _validate_context_metadata(metadata)
    west, south, east, north = bounds
    minimum_x = _xyz_x(west, zoom)
    maximum_x = _xyz_x(east, zoom)
    minimum_xyz_y = _xyz_y(north, zoom)
    maximum_xyz_y = _xyz_y(south, zoom)
    tile_count = 1 << zoom
    minimum_tms_y = tile_count - 1 - maximum_xyz_y
    maximum_tms_y = tile_count - 1 - minimum_xyz_y
    try:
        rows = connection.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles "
            "WHERE zoom_level = ? AND tile_column BETWEEN ? AND ? "
            "AND tile_row BETWEEN ? AND ? ORDER BY tile_column, tile_row",
            (zoom, minimum_x, maximum_x, minimum_tms_y, maximum_tms_y),
        )
    except sqlite3.Error as exc:
        raise MapPlotterError(f"Cannot query Zoomstack context tiles: {exc}") from exc

    clip = box(west, south, east, north)
    polygons: dict[str, list[Polygon]] = {"sea": [], "surfacewater": []}
    raw_lines: dict[str, list[tuple[tuple[float, float], ...]]] = {
        "road-motorway": [],
        "road-primary": [],
        "national-boundary": [],
    }
    places: dict[tuple[str, str, tuple[float, float]], NationalContextPlace] = {}
    gb_country_anchor_points: dict[str, tuple[float, float]] = {}
    non_gb_country_anchor_points: dict[str, tuple[float, float]] = {}
    input_counts: Counter[str] = Counter()
    selected_counts: Counter[str] = Counter()
    excluded_road_types: Counter[str] = Counter()
    source_objects: set[str] = set()
    decoded_tile_count = 0
    for tile_x, tms_y, payload in rows:
        decoded_tile_count += 1
        try:
            raw_tile = (
                gzip.decompress(payload)
                if bytes(payload).startswith(b"\x1f\x8b")
                else bytes(payload)
            )
            document = decoder.decode(
                raw_tile,
                default_options={"y_coord_down": True},
            )
        except Exception as exc:
            raise MapPlotterError(
                f"Cannot decode Zoomstack context tile {zoom}/{tile_x}/{tms_y}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise MapPlotterError(
                f"Zoomstack decoded context tile {zoom}/{tile_x}/{tms_y} "
                "must be an object."
            )
        xyz_y = tile_count - 1 - int(tms_y)
        for layer_id in ZOOMSTACK_NATIONAL_CONTEXT_LAYERS:
            layer = document.get(layer_id)
            if layer is None:
                continue
            if not isinstance(layer, dict):
                raise MapPlotterError(
                    f"Zoomstack decoded {layer_id} layer must be an object."
                )
            extent = layer.get("extent")
            if isinstance(extent, bool) or not isinstance(extent, int) or extent <= 0:
                raise MapPlotterError(
                    f"Zoomstack {layer_id} layer has an invalid extent."
                )
            raw_features = layer.get("features")
            if not isinstance(raw_features, list):
                raise MapPlotterError(
                    f"Zoomstack {layer_id} layer features must be an array."
                )
            for feature_index, raw_feature in enumerate(raw_features):
                input_counts[layer_id] += 1
                if not isinstance(raw_feature, dict):
                    raise MapPlotterError(
                        f"Zoomstack {layer_id} feature {feature_index} must be an object."
                    )
                properties = raw_feature.get("properties", {})
                if not isinstance(properties, dict):
                    raise MapPlotterError(
                        f"Zoomstack {layer_id} feature {feature_index} properties "
                        "must be an object."
                    )
                source_object = (
                    f"mbtiles/{zoom}/{int(tile_x)}/{int(tms_y)}/{layer_id}/"
                    f"{raw_feature.get('id', 'unidentified')}"
                )
                raw_geometry = raw_feature.get("geometry")
                if layer_id in {"sea", "surfacewater"}:
                    coordinate_sets = _context_polygon_coordinate_sets(
                        raw_geometry,
                        layer_id=layer_id,
                        feature_index=feature_index,
                    )
                    feature_selected = False
                    for polygon_coordinates in coordinate_sets:
                        if (
                            not isinstance(polygon_coordinates, list)
                            or not polygon_coordinates
                        ):
                            raise MapPlotterError(
                                f"Zoomstack {layer_id} feature {feature_index} polygon "
                                "coordinates must contain at least one ring."
                            )
                        rings = [
                            _context_points(
                                ring,
                                layer_id=layer_id,
                                feature_index=feature_index,
                                tile_x=int(tile_x),
                                xyz_y=xyz_y,
                                zoom=zoom,
                                extent=extent,
                            )
                            for ring in polygon_coordinates
                        ]
                        if len(rings[0]) < 4:
                            raise MapPlotterError(
                                f"Zoomstack {layer_id} feature {feature_index} has "
                                "a polygon shell with fewer than four points."
                            )
                        try:
                            polygon_geometry = Polygon(
                                rings[0], rings[1:]
                            ).intersection(clip)
                        except (GEOSException, ValueError) as exc:
                            raise MapPlotterError(
                                f"Zoomstack {layer_id} feature {feature_index} has "
                                f"invalid polygon geometry: {exc}"
                            ) from exc
                        for polygon_part in _polygon_parts(polygon_geometry):
                            if not polygon_part.is_empty and polygon_part.area > 0.0:
                                polygons[layer_id].append(polygon_part)
                                feature_selected = True
                    if feature_selected:
                        selected_counts[layer_id] += 1
                        source_objects.add(source_object)
                    continue

                if layer_id in {"roads", "boundaries"}:
                    road_type = properties.get("type")
                    if layer_id == "roads":
                        if road_type not in ZOOMSTACK_NATIONAL_ROAD_TYPES:
                            excluded_road_types[str(road_type)] += 1
                            continue
                        context_class = f"road-{str(road_type).casefold()}"
                    else:
                        if road_type != "National":
                            continue
                        context_class = "national-boundary"
                    feature_selected = False
                    for raw_points in _context_line_coordinate_sets(
                        raw_geometry,
                        layer_id=layer_id,
                        feature_index=feature_index,
                    ):
                        points = _context_points(
                            raw_points,
                            layer_id=layer_id,
                            feature_index=feature_index,
                            tile_x=int(tile_x),
                            xyz_y=xyz_y,
                            zoom=zoom,
                            extent=extent,
                        )
                        if len(points) < 2:
                            continue
                        try:
                            clipped = LineString(points).intersection(clip)
                        except (GEOSException, ValueError) as exc:
                            raise MapPlotterError(
                                f"Zoomstack {layer_id} feature {feature_index} has "
                                f"invalid line geometry: {exc}"
                            ) from exc
                        for line_part in _line_parts(clipped):
                            line_geometry = tuple(
                                (round(float(x), 10), round(float(y), 10))
                                for x, y in line_part.coords
                            )
                            if len(line_geometry) >= 2 and any(
                                point != line_geometry[0] for point in line_geometry[1:]
                            ):
                                raw_lines[context_class].append(line_geometry)
                                feature_selected = True
                    if feature_selected:
                        selected_counts[layer_id] += 1
                        source_objects.add(source_object)
                    continue

                if layer_id == "names":
                    place_type = properties.get("type")
                    name = properties.get("name1")
                    is_country_anchor = place_type == "Country" and isinstance(
                        name, str
                    )
                    if (
                        place_type not in ZOOMSTACK_NATIONAL_PLACE_TYPES
                        and not is_country_anchor
                    ):
                        continue
                    if not isinstance(name, str) or not name.strip():
                        raise MapPlotterError(
                            f"Zoomstack names feature {feature_index} has no name1."
                        )
                    if (
                        not isinstance(raw_geometry, dict)
                        or raw_geometry.get("type") != "Point"
                    ):
                        raise MapPlotterError(
                            f"Zoomstack names feature {feature_index} must be a Point."
                        )
                    point = _context_point(
                        raw_geometry.get("coordinates"),
                        layer_id=layer_id,
                        feature_index=feature_index,
                        tile_x=int(tile_x),
                        xyz_y=xyz_y,
                        zoom=zoom,
                        extent=extent,
                    )
                    if not clip.covers(Point(point)):
                        continue
                    if is_country_anchor:
                        anchor_map = (
                            gb_country_anchor_points
                            if name in GB_CONTEXT_COUNTRY_NAMES
                            else non_gb_country_anchor_points
                        )
                        previous_anchor = anchor_map.get(name)
                        if previous_anchor is None or point < previous_anchor:
                            anchor_map[name] = point
                        source_objects.add(source_object)
                        continue
                    key = (name.strip(), str(place_type), point)
                    places[key] = NationalContextPlace(
                        point=point,
                        name=name.strip(),
                        place_type=str(place_type),
                    )
                    selected_counts[layer_id] += 1
                    source_objects.add(source_object)

    coastline_lines, coastline_selection_audit = _great_britain_coastline_lines(
        polygons["sea"],
        bounds=bounds,
        gb_country_anchor_points=gb_country_anchor_points.values(),
        non_gb_country_anchor_points=non_gb_country_anchor_points.values(),
    )
    lines: list[NationalContextLine] = []
    lines.extend(
        NationalContextLine(
            geometry=geometry,
            context_class="coastline",
            source_layer="sea",
        )
        for geometry in coastline_lines
    )
    lines.extend(
        NationalContextLine(
            geometry=geometry,
            context_class="surface-water-bank",
            source_layer="surfacewater",
        )
        for geometry in _polygon_bank_lines(
            polygons["surfacewater"],
            bounds=bounds,
        )
    )
    for context_class in (
        "national-boundary",
        "road-primary",
        "road-motorway",
    ):
        source_layer = "boundaries" if context_class == "national-boundary" else "roads"
        lines.extend(
            NationalContextLine(
                geometry=geometry,
                context_class=context_class,
                source_layer=source_layer,
            )
            for geometry in _merged_context_lines(raw_lines[context_class])
        )
    lines_tuple = tuple(
        sorted(
            lines,
            key=lambda item: (item.context_class, item.geometry, item.source_layer),
        )
    )
    places_tuple = tuple(
        sorted(
            places.values(),
            key=lambda item: (item.place_type, item.name, item.point),
        )
    )
    if not lines_tuple:
        raise MapPlotterError("Zoomstack national context produced no linework.")
    line_counts = Counter(line.context_class for line in lines_tuple)
    place_counts = Counter(place.place_type for place in places_tuple)
    geometry_sha256 = national_context_geometry_sha256(lines_tuple, places_tuple)
    lineage_sha256 = hashlib.sha256(
        "\n".join(sorted(source_objects)).encode("utf-8")
    ).hexdigest()
    audit = {
        "policy_version": "zoomstack-national-house-context-v2",
        "source_product": "OS Open Zoomstack",
        "source_sha256": source_sha256,
        "zoom": zoom,
        "bounds_wgs84": list(bounds),
        "decoded_tile_count": decoded_tile_count,
        "input_layer_feature_counts": dict(sorted(input_counts.items())),
        "selected_raw_feature_counts": dict(sorted(selected_counts.items())),
        "excluded_road_type_counts": dict(sorted(excluded_road_types.items())),
        "included_road_types": sorted(ZOOMSTACK_NATIONAL_ROAD_TYPES),
        "emitted_context_line_counts": dict(sorted(line_counts.items())),
        "candidate_place_counts": dict(sorted(place_counts.items())),
        "emitted_context_line_count": len(lines_tuple),
        "candidate_place_count": len(places_tuple),
        "mainland_country_anchor_count": len(gb_country_anchor_points),
        "mainland_country_anchor_names": sorted(gb_country_anchor_points),
        "non_gb_country_anchor_count": len(non_gb_country_anchor_points),
        "non_gb_country_anchor_names": sorted(non_gb_country_anchor_points),
        "coastline_selection_policy": (
            "same-source OS England/Scotland/Wales land seeds plus nearest-seed "
            "assignment of complete detached island components; Northern Ireland "
            "and other non-GB jurisdiction seeds excluded"
        ),
        **coastline_selection_audit,
        "geometry_sha256": geometry_sha256,
        "selected_source_object_count": len(source_objects),
        "selected_source_object_lineage_sha256": lineage_sha256,
        "polygon_tile_seams_dissolved": True,
        "polygon_tile_seams_dissolved_scope": (
            "exactly coincident polygon edges only; near-coincident fill-partition "
            "cracks are not repaired"
        ),
        "seam_free_coastline_certified": False,
        "production_coastline_permitted": False,
        "production_coastline_replacement": (
            "hash-pinned authored OSM natural=coastline ways"
        ),
        "selection_boundary_segments_suppressed": True,
        "road_merge_policy": "exact endpoints within one source road class only",
        "invented_connector_count": 0,
        "connected_routing_graph_claimed": False,
        "operator_service_geometry_claimed": False,
        "feature_ids_persistent": False,
        "permitted_use": "quiet national cartographic context only",
    }
    return ZoomstackNationalContext(
        source_sha256=source_sha256,
        zoom=zoom,
        bounds_wgs84=bounds,
        lines=lines_tuple,
        places=places_tuple,
        audit=audit,
    )


def load_zoomstack_physical_rail(
    path: Path,
    *,
    expected_sha256: str,
    zoom: int = 10,
    bounds_wgs84: tuple[float, float, float, float] = DEFAULT_GB_BOUNDS,
    national_context_zoom: int | None = None,
) -> ZoomstackPhysicalRail:
    """Load clipped physical rail linework from one exact MBTiles snapshot.

    The result intentionally carries no graph connectivity or operator/service
    semantics.  Joining is permitted later only at identical emitted endpoints
    and only for pen optimisation; it cannot become passenger-route evidence.
    """

    if (
        not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise MapPlotterError("Zoomstack expected_sha256 must be a lowercase digest.")
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not 0 <= zoom <= 22:
        raise MapPlotterError("Zoomstack zoom must be an integer from 0 to 22.")
    if national_context_zoom is not None and (
        isinstance(national_context_zoom, bool)
        or not isinstance(national_context_zoom, int)
        or not 0 <= national_context_zoom <= 22
    ):
        raise MapPlotterError(
            "Zoomstack national_context_zoom must be null or an integer from 0 to 22."
        )
    bounds = _validate_bounds(bounds_wgs84)
    national_context: ZoomstackNationalContext | None = None
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise MapPlotterError(f"Zoomstack MBTiles is absent: {path}: {exc}") from exc
    actual_sha256 = _sha256_path(resolved)
    if actual_sha256 != expected_sha256:
        raise MapPlotterError(
            "Zoomstack MBTiles SHA-256 does not match the pinned source: "
            f"expected {expected_sha256}, got {actual_sha256}."
        )

    try:
        connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise MapPlotterError(f"Cannot open Zoomstack MBTiles: {exc}") from exc
    try:
        metadata = _metadata(connection)
        try:
            minimum_zoom = int(metadata["minzoom"])
            maximum_zoom = int(metadata["maxzoom"])
        except (KeyError, ValueError) as exc:
            raise MapPlotterError(
                "Zoomstack minzoom/maxzoom metadata is invalid."
            ) from exc
        if not minimum_zoom <= zoom <= maximum_zoom:
            raise MapPlotterError(
                f"Zoomstack zoom {zoom} is outside {minimum_zoom}..{maximum_zoom}."
            )
        if national_context_zoom is not None and not (
            minimum_zoom <= national_context_zoom <= maximum_zoom
        ):
            raise MapPlotterError(
                f"Zoomstack context zoom {national_context_zoom} is outside "
                f"{minimum_zoom}..{maximum_zoom}."
            )

        west, south, east, north = bounds
        minimum_x = _xyz_x(west, zoom)
        maximum_x = _xyz_x(east, zoom)
        minimum_xyz_y = _xyz_y(north, zoom)
        maximum_xyz_y = _xyz_y(south, zoom)
        tile_count = 1 << zoom
        minimum_tms_y = tile_count - 1 - maximum_xyz_y
        maximum_tms_y = tile_count - 1 - minimum_xyz_y
        try:
            rows = connection.execute(
                "SELECT tile_column, tile_row, tile_data FROM tiles "
                "WHERE zoom_level = ? AND tile_column BETWEEN ? AND ? "
                "AND tile_row BETWEEN ? AND ? ORDER BY tile_column, tile_row",
                (zoom, minimum_x, maximum_x, minimum_tms_y, maximum_tms_y),
            )
        except sqlite3.Error as exc:
            raise MapPlotterError(f"Cannot query Zoomstack tiles: {exc}") from exc

        decoder = _decoder()
        clip = box(west, south, east, north)
        feature_map: dict[
            tuple[str, tuple[tuple[float, float], ...]],
            set[str],
        ] = {}
        input_feature_count = 0
        clipped_geometry_occurrence_count = 0
        decoded_tile_count = 0
        for tile_x, tms_y, payload in rows:
            decoded_tile_count += 1
            try:
                raw_tile = (
                    gzip.decompress(payload)
                    if bytes(payload).startswith(b"\x1f\x8b")
                    else bytes(payload)
                )
                document = decoder.decode(
                    raw_tile,
                    default_options={"y_coord_down": True},
                )
            except Exception as exc:
                raise MapPlotterError(
                    f"Cannot decode Zoomstack tile {zoom}/{tile_x}/{tms_y}: {exc}"
                ) from exc
            if not isinstance(document, dict):
                raise MapPlotterError(
                    f"Zoomstack decoded tile {zoom}/{tile_x}/{tms_y} must be an object."
                )
            layer = document.get(ZOOMSTACK_RAIL_LAYER)
            if layer is None:
                continue
            if not isinstance(layer, dict):
                raise MapPlotterError("Zoomstack decoded rail layer must be an object.")
            extent = layer.get("extent")
            if isinstance(extent, bool) or not isinstance(extent, int) or extent <= 0:
                raise MapPlotterError("Zoomstack rail layer has an invalid extent.")
            raw_features = layer.get("features")
            if not isinstance(raw_features, list):
                raise MapPlotterError("Zoomstack rail layer features must be an array.")
            xyz_y = tile_count - 1 - int(tms_y)
            for feature_index, raw_feature in enumerate(raw_features):
                input_feature_count += 1
                rail_type, coordinate_sets = _feature_coordinate_sets(
                    raw_feature,
                    feature_index=feature_index,
                )
                source_object = (
                    f"mbtiles/{zoom}/{int(tile_x)}/{int(tms_y)}/"
                    f"{raw_feature.get('id', 'unidentified')}"
                )
                for part_index, coordinates in enumerate(coordinate_sets):
                    points = _decoded_points(
                        coordinates,
                        feature_index=feature_index,
                        part_index=part_index,
                        tile_x=int(tile_x),
                        xyz_y=xyz_y,
                        zoom=zoom,
                        extent=extent,
                    )
                    if len(points) < 2:
                        continue
                    try:
                        pieces = tuple(
                            _line_parts(LineString(points).intersection(clip))
                        )
                    except (GEOSException, ValueError) as exc:
                        raise MapPlotterError(
                            f"Zoomstack rail feature {feature_index} part "
                            f"{part_index} has invalid line geometry: {exc}"
                        ) from exc
                    for piece in pieces:
                        clipped = tuple(
                            (round(float(x), 10), round(float(y), 10))
                            for x, y in piece.coords
                        )
                        if len(clipped) < 2 or all(
                            point == clipped[0] for point in clipped[1:]
                        ):
                            continue
                        clipped_geometry_occurrence_count += 1
                        canonical = _canonical_geometry(clipped)
                        feature_map.setdefault((str(rail_type), canonical), set()).add(
                            source_object
                        )
        if national_context_zoom is not None:
            national_context = _load_national_context(
                connection,
                decoder,
                metadata,
                source_sha256=actual_sha256,
                zoom=national_context_zoom,
                bounds=bounds,
            )
    finally:
        connection.close()

    features = tuple(
        PhysicalRailFeature(
            geometry=geometry,
            rail_type=rail_type,
            source_objects=tuple(sorted(source_objects)),
        )
        for (rail_type, geometry), source_objects in sorted(feature_map.items())
    )
    if not features:
        raise MapPlotterError("Zoomstack selection produced no physical rail linework.")
    type_counts = Counter(feature.rail_type for feature in features)
    geometry_hash = hashlib.sha256(
        json.dumps(
            [feature.as_dict() for feature in features],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    audit = {
        "policy_version": "zoomstack-physical-rail-v1",
        "source_product": "OS Open Zoomstack",
        "source_sha256": actual_sha256,
        "zoom": zoom,
        "bounds_wgs84": list(bounds),
        "decoded_tile_count": decoded_tile_count,
        "input_tile_feature_count": input_feature_count,
        "clipped_geometry_occurrence_count": clipped_geometry_occurrence_count,
        "emitted_physical_feature_count": len(features),
        "deduplicated_occurrence_count": (
            clipped_geometry_occurrence_count - len(features)
        ),
        "rail_type_counts": dict(sorted(type_counts.items())),
        "geometry_sha256": geometry_hash,
        "feature_ids_persistent": False,
        "connected_routing_graph_claimed": False,
        "operator_service_geometry_claimed": False,
        "permitted_use": "physical/cartographic rail context only",
        "invented_connector_count": 0,
    }
    return ZoomstackPhysicalRail(
        source_path=resolved,
        source_sha256=actual_sha256,
        zoom=zoom,
        bounds_wgs84=bounds,
        features=features,
        audit=audit,
        national_context=national_context,
    )


__all__ = [
    "DEFAULT_NATIONAL_CONTEXT_ZOOM",
    "DEFAULT_GB_BOUNDS",
    "GB_CONTEXT_COUNTRY_NAMES",
    "GB_CONTEXT_GEOGRAPHIC_SCOPE",
    "NationalContextLine",
    "NationalContextPlace",
    "PhysicalRailFeature",
    "ZoomstackNationalContext",
    "ZoomstackPhysicalRail",
    "load_zoomstack_physical_rail",
    "national_context_geometry_sha256",
]
