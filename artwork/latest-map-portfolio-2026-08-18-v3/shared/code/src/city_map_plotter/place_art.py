"""Premium, source-pinned place artwork built on the shared plate engine.

The ordinary city renderer is intentionally source-complete.  This module has a
different, explicitly selective job: it composes supplied WGS84 GeoJSON into a
commercial place portrait whose hierarchy is resolved in millimetres on the
chosen plate.  Source geometry is never decoratively perturbed.  Only generated
textures and optional organic crop ornament may use the deterministic seed.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import random
import re
from typing import Any, cast, Iterable, Iterator, Mapping, NoReturn, Sequence

from shapely import affinity, make_valid
from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    Point,
    Polygon,
    box,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, split, transform, unary_union
from shapely.strtree import STRtree

from .models import EARTH_RADIUS_M, MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PENS_BY_ID,
    PlateArtwork,
    PlateContext,
    Rect,
    add_text,
    circle_stroke,
    context_for,
    plotter_copy,
    polyline_length_mm,
    text_strokes_fit,
)
from .stroke_font import text_width_mm


PointTuple = tuple[float, float]
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
FORMAT_IDS = frozenset(
    {
        "a5-portrait",
        "a5-landscape",
        "a4-portrait",
        "a4-landscape",
        "a3-portrait",
        "a3-landscape",
    }
)
DETAIL_LEVELS = frozenset({"sparse", "medium", "dense"})
BUILDING_DENSITIES = frozenset({"off", "sparse", "medium", "dense"})
FRAME_SHAPES = frozenset({"rectangle", "circle", "organic"})
WATER_TEXTURES = frozenset({"negative", "hatch", "waves", "shoreline"})
PARK_TEXTURES = frozenset({"none", "hatch", "organic"})


@dataclass(frozen=True)
class PresetPolicy:
    id: str
    label: str
    concept: str
    hero: str
    default_detail: str
    default_buildings: str
    road_kinds: tuple[str, ...]
    road_coverage_fraction: float
    target_coverage_fraction: float
    water_texture: str
    park_texture: str
    default_frame_shape: str = "rectangle"
    label_kinds: tuple[str, ...] = ()
    label_limit: int = 0
    show_scale_bar: bool = False
    show_north: bool = False
    show_index_ticks: bool = False
    require_any: tuple[str, ...] = ()


PRESETS: dict[str, PresetPolicy] = {
    "monochrome-street-portrait": PresetPolicy(
        id="monochrome-street-portrait",
        label="Monochrome Street Portrait",
        concept="Street network as the hero; water is predominantly negative space.",
        hero="roads",
        default_detail="medium",
        default_buildings="off",
        road_kinds=(
            "road_major",
            "road_secondary",
            "road_local",
            "road_service",
            "road_pedestrian",
            "road_path",
            "road_cycleway",
        ),
        road_coverage_fraction=0.205,
        target_coverage_fraction=0.245,
        water_texture="negative",
        park_texture="none",
        label_kinds=("landmark",),
        label_limit=2,
        require_any=("road",),
    ),
    "river-and-road": PresetPolicy(
        id="river-and-road",
        label="River and Road",
        concept="Waterway or coastline is primary; fine roads make the secondary web.",
        hero="water",
        default_detail="medium",
        default_buildings="off",
        road_kinds=("road_major", "road_secondary", "road_local", "road_service"),
        road_coverage_fraction=0.115,
        target_coverage_fraction=0.235,
        water_texture="hatch",
        park_texture="none",
        label_kinds=("water_area", "water_line", "coastline", "landmark"),
        label_limit=4,
        show_north=True,
        require_any=("water",),
    ),
    "urban-blueprint": PresetPolicy(
        id="urban-blueprint",
        label="Urban Blueprint",
        concept="Technical frame, index ticks, north mark, scale bar, transport hierarchy.",
        hero="transport",
        default_detail="medium",
        default_buildings="sparse",
        road_kinds=(
            "road_major",
            "road_secondary",
            "road_local",
            "road_service",
            "road_pedestrian",
        ),
        road_coverage_fraction=0.145,
        target_coverage_fraction=0.245,
        water_texture="negative",
        park_texture="none",
        label_kinds=("landmark",),
        label_limit=3,
        show_scale_bar=True,
        show_north=True,
        show_index_ticks=True,
        require_any=("road", "rail"),
    ),
    "topographic-place-portrait": PresetPolicy(
        id="topographic-place-portrait",
        label="Topographic Place Portrait",
        concept="Real contour geometry is the hero; settlement is sparse context.",
        hero="contours",
        default_detail="medium",
        default_buildings="sparse",
        road_kinds=("road_major", "road_secondary", "road_local", "road_path"),
        road_coverage_fraction=0.055,
        target_coverage_fraction=0.245,
        water_texture="negative",
        park_texture="none",
        label_kinds=("landmark",),
        label_limit=4,
        show_scale_bar=True,
        show_north=True,
        require_any=("contour",),
    ),
    "campus-graduation-map": PresetPolicy(
        id="campus-graduation-map",
        label="Campus Graduation Map",
        concept="Campus boundary and principal buildings lead; city context recedes.",
        hero="campus",
        default_detail="sparse",
        default_buildings="medium",
        road_kinds=("road_major", "road_secondary", "road_local", "road_pedestrian"),
        road_coverage_fraction=0.065,
        target_coverage_fraction=0.245,
        water_texture="negative",
        park_texture="hatch",
        label_kinds=("campus_building", "landmark"),
        label_limit=6,
        show_north=True,
        require_any=("campus",),
    ),
    "landmark-radius": PresetPolicy(
        id="landmark-radius",
        label="Landmark Radius",
        concept="One supplied landmark anchors a radial local street composition.",
        hero="landmark",
        default_detail="medium",
        default_buildings="sparse",
        road_kinds=(
            "road_major",
            "road_secondary",
            "road_local",
            "road_service",
            "road_pedestrian",
        ),
        road_coverage_fraction=0.145,
        target_coverage_fraction=0.235,
        water_texture="negative",
        park_texture="none",
        default_frame_shape="circle",
        label_kinds=("focal",),
        label_limit=1,
        show_north=True,
        require_any=("focal",),
    ),
    "our-places": PresetPolicy(
        id="our-places",
        label="Our Places",
        concept="Two to five milestones share one quiet regional composition.",
        hero="milestones",
        default_detail="sparse",
        default_buildings="off",
        road_kinds=("road_major", "road_secondary"),
        road_coverage_fraction=0.065,
        target_coverage_fraction=0.205,
        water_texture="negative",
        park_texture="none",
        label_kinds=("milestone",),
        label_limit=5,
        show_north=True,
        require_any=("milestone",),
    ),
    "minimal-coordinates": PresetPolicy(
        id="minimal-coordinates",
        label="Minimal Coordinates",
        concept="A sparse outline, water edge, or major-road trace supports large copy.",
        hero="place",
        default_detail="sparse",
        default_buildings="off",
        road_kinds=("road_major", "road_secondary"),
        road_coverage_fraction=0.045,
        target_coverage_fraction=0.16,
        water_texture="negative",
        park_texture="none",
        label_limit=0,
        require_any=("road", "water", "boundary"),
    ),
    "city-layers": PresetPolicy(
        id="city-layers",
        label="City Layers",
        concept="Independent physical pens separate roads, water, rail, land, and copy.",
        hero="layers",
        default_detail="medium",
        default_buildings="sparse",
        road_kinds=(
            "road_major",
            "road_secondary",
            "road_local",
            "road_service",
            "road_pedestrian",
            "road_path",
            "road_cycleway",
        ),
        road_coverage_fraction=0.135,
        target_coverage_fraction=0.245,
        water_texture="hatch",
        park_texture="hatch",
        label_kinds=("water_area", "water_line", "landmark"),
        label_limit=4,
        show_north=True,
        require_any=("road", "water", "rail", "park"),
    ),
    "organic-map": PresetPolicy(
        id="organic-map",
        label="Organic Map",
        concept="Factual geometry stays exact; only textures and crop ornament are seeded.",
        hero="place",
        default_detail="medium",
        default_buildings="sparse",
        road_kinds=(
            "road_major",
            "road_secondary",
            "road_local",
            "road_service",
            "road_pedestrian",
            "road_path",
        ),
        road_coverage_fraction=0.125,
        target_coverage_fraction=0.235,
        water_texture="waves",
        park_texture="organic",
        default_frame_shape="organic",
        label_kinds=("landmark", "water_line"),
        label_limit=4,
        show_north=True,
        require_any=("road", "water", "park", "boundary"),
    ),
}

PRESET_ALIASES = {
    "a": "monochrome-street-portrait",
    "b": "river-and-road",
    "c": "urban-blueprint",
    "d": "topographic-place-portrait",
    "e": "campus-graduation-map",
    "f": "landmark-radius",
    "g": "our-places",
    "h": "minimal-coordinates",
    "i": "city-layers",
    "j": "organic-map",
    "monochrome-street": "monochrome-street-portrait",
    "topographic-place": "topographic-place-portrait",
    "campus-graduation": "campus-graduation-map",
}


@dataclass(frozen=True)
class PlaceRequest:
    id: str
    preset: str
    format_id: str
    title: str
    subtitle: str | None
    geojson: dict[str, Any]
    sources: tuple[dict[str, Any], ...]
    extent: dict[str, Any]
    personalisation: dict[str, Any]
    options: dict[str, Any]
    rights_status: str
    notes: tuple[str, ...]
    data_snapshot: str
    credit_lines: tuple[str, ...]
    request_path: Path | None = None
    geojson_path: Path | None = None
    geojson_file_sha256: str | None = None


@dataclass
class GeoRecord:
    id: str
    source_id: str
    source_ref: str
    kind: str
    geometry: BaseGeometry
    properties: dict[str, Any]
    source_geometry_sha256: str


@dataclass
class ProjectedRecord:
    record: GeoRecord
    geometry: BaseGeometry


@dataclass
class PlannedRecord:
    layer_id: str
    label: str
    pen_id: str
    points: list[PointTuple]
    source_ref: str | None
    role: str
    importance: float
    required: bool = False
    removable_group: str = "context"
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def length_mm(self) -> float:
        return polyline_length_mm(self.points)

    @property
    def ink_mm2(self) -> float:
        return self.length_mm * PENS_BY_ID[self.pen_id].mark_width_mm


@dataclass(frozen=True)
class LocalAzimuthalEquidistant:
    """North-up local projection with defensive longitude wrapping."""

    latitude_0: float
    longitude_0: float

    def forward(self, longitude: float, latitude: float) -> PointTuple:
        if not all(math.isfinite(value) for value in (longitude, latitude)):
            raise MapPlotterError("Place geometry contains a non-finite coordinate.")
        if not -90.0 <= latitude <= 90.0:
            raise MapPlotterError("Place geometry latitude is outside WGS84 bounds.")
        latitude_rad = math.radians(latitude)
        latitude_0_rad = math.radians(self.latitude_0)
        delta_longitude = math.radians(
            ((longitude - self.longitude_0 + 180.0) % 360.0) - 180.0
        )
        cosine_c = math.sin(latitude_0_rad) * math.sin(latitude_rad) + math.cos(
            latitude_0_rad
        ) * math.cos(latitude_rad) * math.cos(delta_longitude)
        cosine_c = min(1.0, max(-1.0, cosine_c))
        angular_distance = math.acos(cosine_c)
        if angular_distance > math.radians(145.0):
            raise MapPlotterError(
                "Place geometry crosses the local projection boundary; split the "
                "artwork into a smaller regional extent."
            )
        if angular_distance <= 1e-12:
            scale = 1.0
        else:
            sine_c = math.sin(angular_distance)
            if abs(sine_c) <= 1e-12:
                raise MapPlotterError(
                    "Place geometry reaches a projection singularity."
                )
            scale = angular_distance / sine_c
        x_m = (
            EARTH_RADIUS_M * scale * math.cos(latitude_rad) * math.sin(delta_longitude)
        )
        y_m = (
            EARTH_RADIUS_M
            * scale
            * (
                math.cos(latitude_0_rad) * math.sin(latitude_rad)
                - math.sin(latitude_0_rad)
                * math.cos(latitude_rad)
                * math.cos(delta_longitude)
            )
        )
        return (x_m, y_m)

    def project_geometry(self, geometry: BaseGeometry) -> BaseGeometry:
        def project(
            x: float | Sequence[float],
            y: float | Sequence[float],
            z: float | Sequence[float] | None = None,
        ) -> tuple[Any, ...]:
            del z
            if isinstance(x, Sequence) and isinstance(y, Sequence):
                projected = [
                    self.forward(float(longitude), float(latitude))
                    for longitude, latitude in zip(x, y, strict=True)
                ]
                return (
                    tuple(point[0] for point in projected),
                    tuple(point[1] for point in projected),
                )
            return self.forward(float(cast(float, x)), float(cast(float, y)))

        try:
            return transform(project, geometry)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MapPlotterError(
                f"Could not project supplied place geometry: {exc}"
            ) from exc


@dataclass(frozen=True)
class PageTransform:
    world_bounds: tuple[float, float, float, float]
    field: Rect

    @property
    def scale_mm_per_m(self) -> float:
        min_x, min_y, max_x, max_y = self.world_bounds
        return self.field.width / (max_x - min_x)

    @property
    def scale_denominator(self) -> float:
        return 1_000.0 / self.scale_mm_per_m

    def point(self, x_m: float, y_m: float) -> PointTuple:
        min_x, min_y, max_x, max_y = self.world_bounds
        x_mm = self.field.left + (x_m - min_x) * self.field.width / (max_x - min_x)
        y_mm = self.field.top + (max_y - y_m) * self.field.height / (max_y - min_y)
        return (x_mm, y_mm)

    def geometry(self, geometry: BaseGeometry) -> BaseGeometry:
        def to_page(
            x: float | Sequence[float],
            y: float | Sequence[float],
            z: float | Sequence[float] | None = None,
        ) -> tuple[Any, ...]:
            del z
            if isinstance(x, Sequence) and isinstance(y, Sequence):
                projected = [
                    self.point(float(x_value), float(y_value))
                    for x_value, y_value in zip(x, y, strict=True)
                ]
                return (
                    tuple(point[0] for point in projected),
                    tuple(point[1] for point in projected),
                )
            return self.point(float(cast(float, x)), float(cast(float, y)))

        return transform(to_page, geometry)


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid place-art request: {message}")


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


def _check_keys(
    value: Mapping[str, Any],
    label: str,
    *,
    required: Iterable[str] = (),
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


def _canonical_json_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail(f"GeoJSON is not finite canonical JSON: {exc}")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read pinned place source {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def available_place_presets() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "id": policy.id,
            "label": policy.label,
            "concept": policy.concept,
        }
        for policy in PRESETS.values()
    )


def _normalise_preset(value: Any) -> str:
    preset = _text(value, "preset").casefold()
    preset = PRESET_ALIASES.get(preset, preset)
    if preset not in PRESETS:
        _fail(f"preset {preset!r} is unknown; choose {', '.join(sorted(PRESETS))}.")
    return preset


def _validate_source(value: Any, index: int) -> dict[str, Any]:
    label = f"sources[{index}]"
    source = _object(value, label)
    _check_keys(
        source,
        label,
        required={"id", "label", "license", "attribution"},
        optional={
            "kind",
            "url",
            "snapshot",
            "rights_status",
            "geometry_sha256",
            "credit_line",
            "notes",
        },
    )
    source_id = _text(source["id"], f"{label}.id")
    if _ID.fullmatch(source_id) is None:
        _fail(f"{label}.id must be a lower-case stable identifier.")
    for key in ("label", "license", "attribution"):
        _text(source[key], f"{label}.{key}")
    if "url" in source and not _text(source["url"], f"{label}.url").startswith(
        "https://"
    ):
        _fail(f"{label}.url must use HTTPS.")
    if "geometry_sha256" in source:
        digest = _text(source["geometry_sha256"], f"{label}.geometry_sha256")
        if _SHA256.fullmatch(digest) is None:
            _fail(f"{label}.geometry_sha256 must be a lowercase SHA-256.")
    return copy.deepcopy(source)


def _validate_extent(value: Any) -> dict[str, Any]:
    if value is None:
        return {"mode": "data", "fit": "contain", "padding_fraction": 0.025}
    extent = _object(value, "extent")
    _check_keys(
        extent,
        "extent",
        optional={
            "bbox",
            "center",
            "radius_km",
            "polygon",
            "feature_id",
            "fit",
            "padding_fraction",
        },
    )
    modes = [
        "bbox" if "bbox" in extent else None,
        "center" if "center" in extent or "radius_km" in extent else None,
        "polygon" if "polygon" in extent else None,
        "feature" if "feature_id" in extent else None,
    ]
    selected = [mode for mode in modes if mode is not None]
    if len(selected) != 1:
        _fail(
            "extent must define exactly one of bbox, center plus radius_km, "
            "polygon, or feature_id."
        )
    result = copy.deepcopy(extent)
    result["mode"] = selected[0]
    fit = str(result.get("fit", "contain"))
    if fit not in {"contain", "cover"}:
        _fail("extent.fit must be contain or cover.")
    result["fit"] = fit
    padding = _number(result.get("padding_fraction", 0.0), "extent.padding_fraction")
    if not 0.0 <= padding <= 0.25:
        _fail("extent.padding_fraction must be between 0 and 0.25.")
    result["padding_fraction"] = padding
    if selected[0] == "bbox":
        bbox_values = _array(result["bbox"], "extent.bbox")
        if len(bbox_values) != 4:
            _fail("extent.bbox must be [west, south, east, north].")
        west, south, east, north = (
            _number(item, f"extent.bbox[{index}]")
            for index, item in enumerate(bbox_values)
        )
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            _fail("extent bbox longitude must be between -180 and 180.")
        if not (-90 <= south < north <= 90):
            _fail("extent bbox latitude is invalid.")
        if math.isclose(west, east, abs_tol=1e-12):
            _fail("extent bbox has zero longitude span.")
        result["bbox"] = [west, south, east, north]
    elif selected[0] == "center":
        if "center" not in result or "radius_km" not in result:
            _fail("extent.center and extent.radius_km must appear together.")
        centre = _array(result["center"], "extent.center")
        if len(centre) != 2:
            _fail("extent.center must be [latitude, longitude].")
        latitude = _number(centre[0], "extent.center[0]")
        longitude = _number(centre[1], "extent.center[1]")
        radius_km = _number(result["radius_km"], "extent.radius_km")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            _fail("extent.center is outside WGS84 bounds.")
        if radius_km <= 0 or radius_km > 5_000:
            _fail("extent.radius_km must be greater than zero and at most 5000.")
        result["center"] = [latitude, longitude]
        result["radius_km"] = radius_km
    elif selected[0] == "polygon":
        polygon = _object(result["polygon"], "extent.polygon")
        if polygon.get("type") not in {"Polygon", "MultiPolygon"}:
            _fail("extent.polygon must be GeoJSON Polygon or MultiPolygon geometry.")
    else:
        result["feature_id"] = _text(result["feature_id"], "extent.feature_id")
    return result


PERSONALISATION_FIELDS = frozenset(
    {
        "place_name",
        "neighbourhood",
        "coordinates",
        "date",
        "date_range",
        "recipient_names",
        "address",
        "degree",
        "graduate_name",
        "graduation_date",
        "thesis_title",
        "event",
        "dedication",
        "quotation",
        "elevation_range",
        "map_scale",
        "distance",
        "show_distance",
    }
)


OPTION_FIELDS = frozenset(
    {
        "detail",
        "building_density",
        "labels",
        "label_limit",
        "label_feature_ids",
        "simplify_mm",
        "minimum_movement_mm",
        "focal_feature_id",
        "route_feature_id",
        "selected_building_ids",
        "frame_shape",
        "seed",
        "water_texture",
        "park_texture",
        "contour_interval_m",
        "index_contour_every",
        "clip_bleed_mm",
        "pen_roles",
        "show_scale_bar",
        "show_north",
        "show_index_ticks",
        "max_coverage",
    }
)


def _validate_personalisation(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    personalisation = _object(value, "personalisation")
    unexpected = sorted(set(personalisation) - PERSONALISATION_FIELDS)
    if unexpected:
        _fail("personalisation has unsupported fields: " + ", ".join(unexpected) + ".")
    result = copy.deepcopy(personalisation)
    for key, item in result.items():
        if key in {"show_distance"}:
            if not isinstance(item, bool):
                _fail(f"personalisation.{key} must be true or false.")
            continue
        if key == "coordinates" and isinstance(item, bool):
            continue
        if key == "recipient_names" and isinstance(item, list):
            if not item or len(item) > 5:
                _fail("personalisation.recipient_names must contain one to five names.")
            result[key] = [
                _text(name, f"personalisation.recipient_names[{index}]")
                for index, name in enumerate(item)
            ]
            continue
        result[key] = _text(item, f"personalisation.{key}")
        if len(result[key]) > 240:
            _fail(f"personalisation.{key} is too long for a plotted plate.")
    return result


def _validate_options(value: Any, policy: PresetPolicy) -> dict[str, Any]:
    options = {} if value is None else copy.deepcopy(_object(value, "options"))
    unexpected = sorted(set(options) - OPTION_FIELDS)
    if unexpected:
        _fail("options has unsupported fields: " + ", ".join(unexpected) + ".")
    detail = str(options.get("detail", policy.default_detail))
    if detail not in DETAIL_LEVELS:
        _fail(f"options.detail must be one of {', '.join(sorted(DETAIL_LEVELS))}.")
    options["detail"] = detail
    buildings = str(options.get("building_density", policy.default_buildings))
    if buildings not in BUILDING_DENSITIES:
        _fail(
            "options.building_density must be one of "
            f"{', '.join(sorted(BUILDING_DENSITIES))}."
        )
    options["building_density"] = buildings
    for flag in ("labels", "show_scale_bar", "show_north", "show_index_ticks"):
        if flag in options and not isinstance(options[flag], bool):
            _fail(f"options.{flag} must be true or false.")
    options.setdefault("labels", policy.label_limit > 0)
    options.setdefault("show_scale_bar", policy.show_scale_bar)
    options.setdefault("show_north", policy.show_north)
    options.setdefault("show_index_ticks", policy.show_index_ticks)
    label_limit = options.get("label_limit", policy.label_limit)
    if isinstance(label_limit, bool) or not isinstance(label_limit, int):
        _fail("options.label_limit must be an integer.")
    if not 0 <= label_limit <= 20:
        _fail("options.label_limit must be between 0 and 20.")
    options["label_limit"] = label_limit
    frame_shape = str(options.get("frame_shape", policy.default_frame_shape))
    if frame_shape not in FRAME_SHAPES:
        _fail(f"options.frame_shape must be one of {', '.join(sorted(FRAME_SHAPES))}.")
    options["frame_shape"] = frame_shape
    water_texture = str(options.get("water_texture", policy.water_texture))
    if water_texture not in WATER_TEXTURES:
        _fail(
            f"options.water_texture must be one of {', '.join(sorted(WATER_TEXTURES))}."
        )
    options["water_texture"] = water_texture
    park_texture = str(options.get("park_texture", policy.park_texture))
    if park_texture not in PARK_TEXTURES:
        _fail(
            f"options.park_texture must be one of {', '.join(sorted(PARK_TEXTURES))}."
        )
    options["park_texture"] = park_texture
    seed = options.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
        _fail("options.seed must be an integer between 0 and 2147483647.")
    options["seed"] = seed
    for name, default, lower, upper in (
        ("simplify_mm", None, 0.0, 1.0),
        ("minimum_movement_mm", None, 0.0, 10.0),
        ("clip_bleed_mm", 0.0, 0.0, 5.0),
        ("max_coverage", policy.target_coverage_fraction, 0.03, 0.28),
    ):
        if name not in options and default is None:
            continue
        number = _number(options.get(name, default), f"options.{name}")
        if not lower <= number <= upper:
            _fail(f"options.{name} must be between {lower:g} and {upper:g}.")
        options[name] = number
    if "contour_interval_m" in options:
        interval = _number(options["contour_interval_m"], "options.contour_interval_m")
        if interval <= 0:
            _fail("options.contour_interval_m must be greater than zero.")
        options["contour_interval_m"] = interval
    index_every = options.get("index_contour_every", 5)
    if isinstance(index_every, bool) or not isinstance(index_every, int):
        _fail("options.index_contour_every must be an integer.")
    if not 2 <= index_every <= 20:
        _fail("options.index_contour_every must be between 2 and 20.")
    options["index_contour_every"] = index_every
    for name in ("focal_feature_id", "route_feature_id"):
        if name in options:
            options[name] = _text(options[name], f"options.{name}")
    for name in ("label_feature_ids", "selected_building_ids"):
        if name in options:
            values = _array(options[name], f"options.{name}")
            options[name] = [
                _text(item, f"options.{name}[{index}]")
                for index, item in enumerate(values)
            ]
    pen_roles = options.get("pen_roles", {})
    if not isinstance(pen_roles, dict):
        _fail("options.pen_roles must be an object of semantic role to pen ID.")
    for role, pen_id in pen_roles.items():
        _text(role, "options.pen_roles key")
        resolved = _text(pen_id, f"options.pen_roles.{role}")
        if resolved not in PENS_BY_ID:
            _fail(f"options.pen_roles.{role} names unknown pen {resolved!r}.")
        if PENS_BY_ID[resolved].ink.casefold() == "white":
            _fail("White ink is not supported on the light-stock place-art plate.")
    options["pen_roles"] = pen_roles
    return options


def load_place_request(path: Path) -> PlaceRequest:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read place-art request {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Place-art request {path} is not valid JSON: {exc}"
        ) from exc
    return validate_place_request(document, base_dir=path.parent, request_path=path)


def validate_place_request(
    value: Any,
    *,
    base_dir: Path | None = None,
    request_path: Path | None = None,
) -> PlaceRequest:
    request = _object(value, "request")
    _check_keys(
        request,
        "request",
        required={"schema_version", "id", "preset", "format_id", "title", "sources"},
        optional={
            "subtitle",
            "geojson",
            "geojson_file",
            "geojson_file_sha256",
            "extent",
            "personalisation",
            "options",
            "rights_status",
            "notes",
            "data_snapshot",
            "credit_lines",
        },
    )
    if request["schema_version"] != 1:
        _fail("schema_version must be 1.")
    subject_id = _text(request["id"], "id")
    if _ID.fullmatch(subject_id) is None:
        _fail("id must be a lower-case stable identifier.")
    preset = _normalise_preset(request["preset"])
    policy = PRESETS[preset]
    format_id = _text(request["format_id"], "format_id").casefold()
    if format_id not in FORMAT_IDS:
        _fail(f"format_id must be one of {', '.join(sorted(FORMAT_IDS))}.")
    title = _text(request["title"], "title")
    subtitle = _text(request["subtitle"], "subtitle") if "subtitle" in request else None
    inline = request.get("geojson")
    file_value = request.get("geojson_file")
    if (inline is None) == (file_value is None):
        _fail("exactly one of geojson or geojson_file is required.")
    geojson_path: Path | None = None
    geojson_file_sha256: str | None = None
    if file_value is not None:
        if base_dir is None:
            _fail("geojson_file requires a base directory for resolution.")
        geojson_path = Path(_text(file_value, "geojson_file"))
        if not geojson_path.is_absolute():
            geojson_path = base_dir / geojson_path
        try:
            geojson = json.loads(geojson_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise MapPlotterError(
                f"Could not read pinned GeoJSON {geojson_path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise MapPlotterError(
                f"Pinned GeoJSON {geojson_path} is not valid JSON: {exc}"
            ) from exc
        expected_digest = request.get("geojson_file_sha256")
        if expected_digest is None:
            _fail("geojson_file requires geojson_file_sha256 for pinned-source use.")
        digest_text = _text(expected_digest, "geojson_file_sha256")
        if _SHA256.fullmatch(digest_text) is None:
            _fail("geojson_file_sha256 must be a lowercase SHA-256.")
        actual_digest = _file_sha256(geojson_path)
        if actual_digest != digest_text:
            _fail(
                f"geojson_file_sha256 disagrees with {geojson_path}: "
                f"expected {digest_text}, found {actual_digest}."
            )
        geojson_file_sha256 = actual_digest
    else:
        geojson = copy.deepcopy(inline)
        if "geojson_file_sha256" in request:
            _fail("geojson_file_sha256 applies only to geojson_file.")
    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        _fail("GeoJSON must be a FeatureCollection.")
    _array(geojson.get("features"), "geojson.features", nonempty=True)
    sources_raw = _array(request["sources"], "sources", nonempty=True)
    sources = tuple(
        _validate_source(source, index) for index, source in enumerate(sources_raw)
    )
    source_ids = [str(source["id"]) for source in sources]
    duplicates = sorted(item for item in set(source_ids) if source_ids.count(item) > 1)
    if duplicates:
        _fail("sources repeat IDs: " + ", ".join(duplicates) + ".")
    personalisation = _validate_personalisation(request.get("personalisation"))
    options = _validate_options(request.get("options"), policy)
    extent = _validate_extent(request.get("extent"))
    notes_raw = request.get("notes", [])
    notes = tuple(
        _text(note, f"notes[{index}]")
        for index, note in enumerate(_array(notes_raw, "notes"))
    )
    rights_status = str(request.get("rights_status", "review-required"))
    if rights_status not in {
        "commercial-clear",
        "project-authored",
        "odbl-attribution-required",
        "review-required",
    }:
        _fail("rights_status is unsupported.")
    data_snapshot = str(request.get("data_snapshot", "source-records"))
    credit_values = request.get("credit_lines")
    if credit_values is None:
        credit_lines = tuple(
            str(source.get("credit_line") or source["attribution"]).strip()
            for source in sources
        )
    else:
        credit_lines = tuple(
            _text(line, f"credit_lines[{index}]")
            for index, line in enumerate(
                _array(credit_values, "credit_lines", nonempty=True)
            )
        )
    credit_lines = tuple(dict.fromkeys(line for line in credit_lines if line))
    if not 1 <= len(credit_lines) <= 2:
        _fail("credit_lines must resolve to one or two visible attribution lines.")
    return PlaceRequest(
        id=subject_id,
        preset=preset,
        format_id=format_id,
        title=title,
        subtitle=subtitle,
        geojson=geojson,
        sources=sources,
        extent=extent,
        personalisation=personalisation,
        options=options,
        rights_status=rights_status,
        notes=notes,
        data_snapshot=data_snapshot,
        credit_lines=credit_lines,
        request_path=request_path,
        geojson_path=geojson_path,
        geojson_file_sha256=geojson_file_sha256,
    )


EXPLICIT_KIND_ALIASES: dict[str, str] = {
    "motorway": "road_major",
    "trunk": "road_major",
    "primary": "road_major",
    "major-road": "road_major",
    "road-major": "road_major",
    "road_major": "road_major",
    "secondary": "road_secondary",
    "tertiary": "road_secondary",
    "secondary-road": "road_secondary",
    "road-secondary": "road_secondary",
    "road_secondary": "road_secondary",
    "local": "road_local",
    "residential": "road_local",
    "local-road": "road_local",
    "road-local": "road_local",
    "road_local": "road_local",
    "service": "road_service",
    "road-service": "road_service",
    "road_service": "road_service",
    "pedestrian": "road_pedestrian",
    "footway": "road_pedestrian",
    "road-pedestrian": "road_pedestrian",
    "road_pedestrian": "road_pedestrian",
    "path": "road_path",
    "track": "road_path",
    "road-path": "road_path",
    "road_path": "road_path",
    "cycleway": "road_cycleway",
    "road-cycleway": "road_cycleway",
    "road_cycleway": "road_cycleway",
    "water": "water_area",
    "lake": "water_area",
    "reservoir": "water_area",
    "harbour": "water_area",
    "water-area": "water_area",
    "water_area": "water_area",
    "river": "water_line",
    "canal": "water_line",
    "waterway": "water_line",
    "water-line": "water_line",
    "water_line": "water_line",
    "coastline": "coastline",
    "park": "park_area",
    "woodland": "park_area",
    "green-space": "park_area",
    "open-land": "park_area",
    "park-area": "park_area",
    "park_area": "park_area",
    "rail": "rail_main",
    "railway": "rail_main",
    "rail-main": "rail_main",
    "rail_main": "rail_main",
    "tram": "rail_transit",
    "metro": "rail_transit",
    "subway": "rail_transit",
    "light-rail": "rail_transit",
    "rail-transit": "rail_transit",
    "rail_transit": "rail_transit",
    "building": "building",
    "campus-building": "campus_building",
    "campus_building": "campus_building",
    "campus-boundary": "campus_boundary",
    "campus_boundary": "campus_boundary",
    "contour": "contour",
    "boundary": "boundary",
    "district-boundary": "boundary",
    "region-boundary": "boundary",
    "landmark": "landmark",
    "landmark-outline": "landmark_outline",
    "landmark_outline": "landmark_outline",
    "route": "route",
    "highlight-route": "route",
    "milestone": "milestone",
    "place": "place",
}

ROAD_KINDS = frozenset(
    {
        "road_major",
        "road_secondary",
        "road_local",
        "road_service",
        "road_pedestrian",
        "road_path",
        "road_cycleway",
    }
)
WATER_KINDS = frozenset({"water_area", "water_line", "coastline"})
RAIL_KINDS = frozenset({"rail_main", "rail_transit"})
BUILDING_KINDS = frozenset({"building", "campus_building"})
AREA_KINDS = frozenset(
    {"water_area", "park_area", "building", "campus_building", "campus_boundary"}
)
LINE_KINDS = (
    ROAD_KINDS
    | WATER_KINDS
    | RAIL_KINDS
    | frozenset({"contour", "boundary", "route", "landmark_outline"})
)
POINT_KINDS = frozenset({"landmark", "milestone", "place"})


def _tag(properties: Mapping[str, Any], key: str) -> str:
    value = properties.get(key)
    if value is None and isinstance(properties.get("tags"), dict):
        value = properties["tags"].get(key)
    return str(value).strip().casefold() if value is not None else ""


def _classify_feature(
    properties: Mapping[str, Any], geometry: BaseGeometry
) -> str | None:
    explicit = properties.get("mapplot:layer", properties.get("layer"))
    if isinstance(explicit, str) and explicit.strip():
        token = explicit.strip().casefold().replace(" ", "-")
        kind = EXPLICIT_KIND_ALIASES.get(token)
        if kind is None:
            _fail(f"feature layer {explicit!r} is unsupported.")
        return kind

    highway = _tag(properties, "highway")
    if highway:
        if highway in {
            "motorway",
            "motorway_link",
            "trunk",
            "trunk_link",
            "primary",
            "primary_link",
        }:
            return "road_major"
        if highway in {"secondary", "secondary_link", "tertiary", "tertiary_link"}:
            return "road_secondary"
        if highway in {"residential", "unclassified", "living_street", "road"}:
            return "road_local"
        if highway == "service":
            return "road_service"
        if highway in {"pedestrian", "footway", "steps", "corridor"}:
            return "road_pedestrian"
        if highway == "cycleway":
            return "road_cycleway"
        if highway in {"path", "track", "bridleway"}:
            return "road_path"

    natural = _tag(properties, "natural")
    waterway = _tag(properties, "waterway")
    landuse = _tag(properties, "landuse")
    leisure = _tag(properties, "leisure")
    railway = _tag(properties, "railway")
    building = _tag(properties, "building")
    amenity = _tag(properties, "amenity")
    if natural == "coastline":
        return "coastline"
    if waterway in {"river", "canal", "stream", "drain", "ditch", "tidal_channel"}:
        return "water_line" if not isinstance(geometry, Polygon) else "water_area"
    if (
        natural == "water"
        or landuse in {"reservoir", "basin", "salt_pond"}
        or _tag(properties, "water")
    ):
        return "water_area"
    if (
        leisure in {"park", "garden", "nature_reserve", "common"}
        or landuse
        in {"forest", "grass", "meadow", "village_green", "recreation_ground"}
        or natural in {"wood", "scrub", "heath", "grassland"}
    ):
        return "park_area"
    if railway in {"tram", "subway", "light_rail", "monorail"}:
        return "rail_transit"
    if railway in {"rail", "narrow_gauge", "preserved"}:
        return "rail_main"
    if building:
        if properties.get("campus") is True or amenity in {
            "university",
            "college",
            "school",
        }:
            return "campus_building"
        return "building"
    if amenity in {"university", "college"} and geometry.geom_type in {
        "Polygon",
        "MultiPolygon",
    }:
        return "campus_boundary"
    if _tag(properties, "contour") or "elevation" in properties:
        return "contour"
    if _tag(properties, "boundary") or "admin_level" in properties:
        return "boundary"
    if any(
        _tag(properties, key) for key in ("historic", "tourism", "memorial", "man_made")
    ):
        return "landmark"
    if _tag(properties, "place"):
        return "place"
    return None


def _geometry_sha256(geometry_value: Any) -> str:
    return _canonical_json_sha256(geometry_value)


def _compatible_geometry(
    kind: str, geometry: BaseGeometry, feature_id: str
) -> BaseGeometry:
    if kind in AREA_KINDS and geometry.geom_type not in {
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }:
        _fail(f"feature {feature_id!r} layer {kind!r} requires polygon geometry.")
    if kind in LINE_KINDS and geometry.geom_type not in {
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }:
        _fail(f"feature {feature_id!r} layer {kind!r} requires line geometry.")
    if kind in POINT_KINDS and geometry.geom_type not in {
        "Point",
        "MultiPoint",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }:
        _fail(f"feature {feature_id!r} layer {kind!r} requires point-like geometry.")
    return geometry


def _extract_records(request: PlaceRequest) -> tuple[list[GeoRecord], dict[str, Any]]:
    source_ids = {str(source["id"]) for source in request.sources}
    default_source_id = next(iter(source_ids)) if len(source_ids) == 1 else None
    seen_ids: set[str] = set()
    records: list[GeoRecord] = []
    ignored = 0
    repaired = 0
    per_source_payload: dict[str, list[dict[str, Any]]] = {
        source_id: [] for source_id in source_ids
    }
    for index, raw_feature in enumerate(request.geojson["features"]):
        feature = _object(raw_feature, f"geojson.features[{index}]")
        if feature.get("type") != "Feature":
            _fail(f"geojson.features[{index}] must be a GeoJSON Feature.")
        geometry_value = feature.get("geometry")
        if not isinstance(geometry_value, dict):
            _fail(f"geojson.features[{index}].geometry must be an object.")
        properties_raw = feature.get("properties", {})
        properties = _object(properties_raw, f"geojson.features[{index}].properties")
        feature_id_value = feature.get(
            "id", properties.get("id", f"feature-{index + 1}")
        )
        feature_id = str(feature_id_value).strip()
        if not feature_id:
            _fail(f"geojson.features[{index}] has an empty feature ID.")
        if feature_id in seen_ids:
            _fail(f"GeoJSON repeats feature ID {feature_id!r}.")
        seen_ids.add(feature_id)
        source_id = str(properties.get("source_id") or default_source_id or "")
        if source_id not in source_ids:
            _fail(f"feature {feature_id!r} must name one of the request source IDs.")
        try:
            geometry = shape(geometry_value)
        except (TypeError, ValueError) as exc:
            raise MapPlotterError(
                f"Invalid place-art request: feature {feature_id!r} has invalid GeoJSON: {exc}"
            ) from exc
        if geometry.is_empty:
            ignored += 1
            continue
        if not geometry.is_valid:
            geometry = make_valid(geometry)
            repaired += 1
        if geometry.is_empty:
            ignored += 1
            continue
        kind = _classify_feature(properties, geometry)
        if kind is None:
            ignored += 1
            continue
        geometry = _compatible_geometry(kind, geometry, feature_id)
        digest = _geometry_sha256(geometry_value)
        source_ref = str(properties.get("source_ref") or f"{source_id}/{feature_id}")
        records.append(
            GeoRecord(
                id=feature_id,
                source_id=source_id,
                source_ref=source_ref,
                kind=kind,
                geometry=geometry,
                properties=copy.deepcopy(properties),
                source_geometry_sha256=digest,
            )
        )
        per_source_payload[source_id].append(
            {
                "id": feature_id,
                "geometry": geometry_value,
                "properties": properties,
            }
        )

    if not records:
        _fail("GeoJSON contains no supported drawable features.")
    source_geometry_digests = {
        source_id: _canonical_json_sha256(
            sorted(items, key=lambda item: str(item["id"]))
        )
        for source_id, items in per_source_payload.items()
    }
    for source in request.sources:
        expected = source.get("geometry_sha256")
        actual = source_geometry_digests[str(source["id"])]
        if expected is not None and expected != actual:
            _fail(
                f"source {source['id']!r} geometry_sha256 disagrees with its exact "
                f"features: expected {expected}, found {actual}."
            )
    return records, {
        "input_feature_count": len(request.geojson["features"]),
        "supported_feature_count": len(records),
        "ignored_feature_count": ignored,
        "repaired_geometry_count": repaired,
        "canonical_geojson_sha256": _canonical_json_sha256(request.geojson),
        "source_geometry_sha256": source_geometry_digests,
    }


def _coordinate_pairs(value: Any) -> Iterator[PointTuple]:
    if isinstance(value, (list, tuple)):
        if (
            len(value) >= 2
            and isinstance(value[0], (int, float))
            and not isinstance(value[0], bool)
            and isinstance(value[1], (int, float))
            and not isinstance(value[1], bool)
        ):
            longitude = float(value[0])
            latitude = float(value[1])
            if not all(math.isfinite(item) for item in (longitude, latitude)):
                _fail("GeoJSON contains a non-finite coordinate.")
            yield (longitude, latitude)
            return
        for item in value:
            yield from _coordinate_pairs(item)


def _geometry_coordinates(geometry: Mapping[str, Any]) -> list[PointTuple]:
    if geometry.get("type") == "GeometryCollection":
        return [
            point
            for item in geometry.get("geometries", [])
            if isinstance(item, dict)
            for point in _geometry_coordinates(item)
        ]
    return list(_coordinate_pairs(geometry.get("coordinates", [])))


def _circular_longitude(values: Sequence[float]) -> float:
    if not values:
        _fail("Cannot choose a projection centre without coordinates.")
    sine = sum(math.sin(math.radians(value)) for value in values)
    cosine = sum(math.cos(math.radians(value)) for value in values)
    if math.hypot(sine, cosine) <= 1e-12:
        return ((values[0] + 180.0) % 360.0) - 180.0
    return math.degrees(math.atan2(sine, cosine))


def _extent_coordinates(
    request: PlaceRequest, records: Sequence[GeoRecord]
) -> list[PointTuple]:
    mode = request.extent["mode"]
    if mode == "bbox":
        west, south, east, north = request.extent["bbox"]
        span = east - west if east > west else east + 360.0 - west
        return [
            (((west + span * x_fraction + 180.0) % 360.0) - 180.0, latitude)
            for x_fraction in (0.0, 0.5, 1.0)
            for latitude in (south, (south + north) / 2.0, north)
        ]
    if mode == "center":
        latitude, longitude = request.extent["center"]
        return [(longitude, latitude)]
    if mode == "polygon":
        return _geometry_coordinates(request.extent["polygon"])
    if mode == "feature":
        wanted = str(request.extent["feature_id"])
        for record in records:
            if record.id == wanted:
                interface = record.geometry.__geo_interface__
                return _geometry_coordinates(interface)
        _fail(f"extent.feature_id {wanted!r} does not exist in GeoJSON.")
    return [
        point
        for record in records
        for point in _geometry_coordinates(record.geometry.__geo_interface__)
    ]


def _projection_for(
    request: PlaceRequest, records: Sequence[GeoRecord]
) -> LocalAzimuthalEquidistant:
    if request.extent["mode"] == "center":
        latitude, longitude = request.extent["center"]
        return LocalAzimuthalEquidistant(float(latitude), float(longitude))
    if request.extent["mode"] == "bbox":
        west, south, east, north = request.extent["bbox"]
        span = east - west if east > west else east + 360.0 - west
        longitude = ((west + span / 2.0 + 180.0) % 360.0) - 180.0
        return LocalAzimuthalEquidistant((south + north) / 2.0, longitude)
    coordinates = _extent_coordinates(request, records)
    latitudes = [point[1] for point in coordinates]
    longitudes = [point[0] for point in coordinates]
    return LocalAzimuthalEquidistant(
        (min(latitudes) + max(latitudes)) / 2.0,
        _circular_longitude(longitudes),
    )


def _projected_extent_bounds(
    request: PlaceRequest,
    records: Sequence[GeoRecord],
    projection: LocalAzimuthalEquidistant,
) -> tuple[float, float, float, float]:
    mode = request.extent["mode"]
    if mode == "center":
        radius_m = float(request.extent["radius_km"]) * 1_000.0
        bounds = (-radius_m, -radius_m, radius_m, radius_m)
    elif mode == "bbox":
        projected = [
            projection.forward(longitude, latitude)
            for longitude, latitude in _extent_coordinates(request, records)
        ]
        bounds = (
            min(point[0] for point in projected),
            min(point[1] for point in projected),
            max(point[0] for point in projected),
            max(point[1] for point in projected),
        )
    elif mode == "polygon":
        polygon = shape(request.extent["polygon"])
        if not polygon.is_valid:
            polygon = make_valid(polygon)
        bounds = projection.project_geometry(polygon).bounds
    elif mode == "feature":
        wanted = str(request.extent["feature_id"])
        feature = next(record for record in records if record.id == wanted)
        bounds = projection.project_geometry(feature.geometry).bounds
    else:
        projected_geometries = [
            projection.project_geometry(record.geometry) for record in records
        ]
        bounds = unary_union(projected_geometries).bounds
    min_x, min_y, max_x, max_y = (float(item) for item in bounds)
    width = max_x - min_x
    height = max_y - min_y
    if width <= 1e-6 and height <= 1e-6:
        width = height = 2_000.0
        centre_x = (min_x + max_x) / 2.0
        centre_y = (min_y + max_y) / 2.0
        min_x, max_x = centre_x - width / 2.0, centre_x + width / 2.0
        min_y, max_y = centre_y - height / 2.0, centre_y + height / 2.0
    elif width <= 1e-6:
        centre_x = (min_x + max_x) / 2.0
        min_x, max_x = centre_x - height / 2.0, centre_x + height / 2.0
    elif height <= 1e-6:
        centre_y = (min_y + max_y) / 2.0
        min_y, max_y = centre_y - width / 2.0, centre_y + width / 2.0
    padding = float(request.extent.get("padding_fraction", 0.0))
    if padding:
        x_pad = (max_x - min_x) * padding
        y_pad = (max_y - min_y) * padding
        min_x -= x_pad
        max_x += x_pad
        min_y -= y_pad
        max_y += y_pad
    return (min_x, min_y, max_x, max_y)


def _adjust_bounds_to_field(
    bounds: tuple[float, float, float, float], field_rect: Rect, fit: str
) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = bounds
    width = max_x - min_x
    height = max_y - min_y
    target_aspect = field_rect.width / field_rect.height
    aspect = width / height
    centre_x = (min_x + max_x) / 2.0
    centre_y = (min_y + max_y) / 2.0
    if fit == "contain":
        if aspect < target_aspect:
            width = height * target_aspect
        else:
            height = width / target_aspect
    else:
        if aspect < target_aspect:
            height = width / target_aspect
        else:
            width = height * target_aspect
    return (
        centre_x - width / 2.0,
        centre_y - height / 2.0,
        centre_x + width / 2.0,
        centre_y + height / 2.0,
    )


def _organic_clip(field_rect: Rect, seed: int) -> Polygon:
    randomizer = random.Random(seed)
    centre_x, centre_y = field_rect.centre
    radius_x = field_rect.width / 2.0
    radius_y = field_rect.height / 2.0
    phase_1 = randomizer.uniform(0.0, 2.0 * math.pi)
    phase_2 = randomizer.uniform(0.0, 2.0 * math.pi)
    points: list[PointTuple] = []
    for index in range(96):
        angle = 2.0 * math.pi * index / 96.0
        modulation = (
            0.965
            + 0.018 * math.sin(3.0 * angle + phase_1)
            + 0.010 * math.sin(7.0 * angle + phase_2)
        )
        points.append(
            (
                centre_x + radius_x * modulation * math.cos(angle),
                centre_y + radius_y * modulation * math.sin(angle),
            )
        )
    return Polygon(points)


def _clip_geometry(context: PlateContext, frame_shape: str, seed: int) -> BaseGeometry:
    if frame_shape == "rectangle":
        return box(
            context.field.left,
            context.field.top,
            context.field.right,
            context.field.bottom,
        )
    if frame_shape == "circle":
        radius = min(context.field.width, context.field.height) / 2.0
        return Point(context.field.centre).buffer(radius, quad_segs=32)
    return _organic_clip(context.field, seed)


def _project_records(
    records: Sequence[GeoRecord],
    projection: LocalAzimuthalEquidistant,
    page_transform: PageTransform,
    clip_geometry: BaseGeometry,
    bleed_mm: float,
) -> tuple[list[ProjectedRecord], dict[str, int]]:
    processing_clip = clip_geometry.buffer(bleed_mm) if bleed_mm else clip_geometry
    projected: list[ProjectedRecord] = []
    outside = 0
    repaired = 0
    for record in records:
        geometry = page_transform.geometry(projection.project_geometry(record.geometry))
        if not geometry.is_valid:
            geometry = make_valid(geometry)
            repaired += 1
        geometry = geometry.intersection(processing_clip)
        if geometry.is_empty:
            outside += 1
            continue
        projected.append(ProjectedRecord(record=record, geometry=geometry))
    return projected, {
        "projected_feature_count": len(projected),
        "outside_extent_count": outside,
        "post_projection_repair_count": repaired,
    }


def _line_parts(geometry: BaseGeometry) -> Iterator[LineString]:
    if isinstance(geometry, LineString):
        if not geometry.is_empty and len(geometry.coords) >= 2:
            yield geometry
        return
    if isinstance(geometry, Polygon):
        yield LineString(geometry.exterior.coords)
        for interior in geometry.interiors:
            yield LineString(interior.coords)
        return
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _line_parts(part)


def _polygon_parts(geometry: BaseGeometry) -> Iterator[Polygon]:
    if isinstance(geometry, Polygon):
        if not geometry.is_empty and geometry.area > 1e-12:
            yield geometry
        return
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _polygon_parts(part)


def _point_parts(geometry: BaseGeometry) -> Iterator[Point]:
    if isinstance(geometry, Point):
        if not geometry.is_empty:
            yield geometry
        return
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _point_parts(part)


def _stable_geometry_sha256(records: Sequence[ProjectedRecord]) -> str:
    payload = [
        {
            "id": item.record.id,
            "kind": item.record.kind,
            "wkb_hex": item.geometry.wkb_hex,
        }
        for item in sorted(records, key=lambda item: (item.record.id, item.record.kind))
    ]
    return _canonical_json_sha256(payload)


def _unique_points(points: Iterable[Point], precision: int = 8) -> list[Point]:
    unique: dict[tuple[float, float], Point] = {}
    for point in points:
        key = (round(point.x, precision), round(point.y, precision))
        unique.setdefault(key, point)
    return [unique[key] for key in sorted(unique)]


def _simplify_line_at_junctions(
    line: LineString,
    junctions: Sequence[Point],
    tolerance_mm: float,
) -> BaseGeometry:
    if tolerance_mm <= 0:
        return line
    interior = [
        point
        for point in _unique_points(junctions)
        if 1e-7 < line.project(point) < line.length - 1e-7
        and line.distance(point) <= 1e-7
    ]
    pieces: list[LineString] = []
    if interior:
        try:
            split_result = split(line, MultiPoint(interior))
            pieces = list(_line_parts(split_result))
        except GEOSException:
            pieces = [line]
    else:
        pieces = [line]
    simplified_lines = [
        part
        for piece in pieces
        if piece.length > 1e-12
        for part in [piece.simplify(tolerance_mm, preserve_topology=True)]
        if isinstance(part, LineString) and len(part.coords) >= 2
    ]
    if not simplified_lines:
        return GeometryCollection()
    merged = (
        linemerge(MultiLineString(simplified_lines))
        if len(simplified_lines) > 1
        else simplified_lines[0]
    )
    return merged


def _road_junctions(
    records: Sequence[ProjectedRecord],
) -> dict[tuple[int, int], list[Point]]:
    parts: list[tuple[int, int, LineString, tuple[str, str, str]]] = []
    for record_index, projected in enumerate(records):
        if projected.record.kind not in ROAD_KINDS:
            continue
        properties = projected.record.properties
        grade = (
            str(properties.get("layer", properties.get("level", "0"))),
            str(properties.get("bridge", "")),
            str(properties.get("tunnel", "")),
        )
        for part_index, line in enumerate(_line_parts(projected.geometry)):
            parts.append((record_index, part_index, line, grade))
    result: dict[tuple[int, int], list[Point]] = {}
    if not parts:
        return result
    lines = [item[2] for item in parts]
    tree = STRtree(lines)
    for index, (record_index, part_index, line, grade) in enumerate(parts):
        for candidate_value in tree.query(line):
            candidate_index = int(candidate_value)
            if candidate_index <= index:
                continue
            other_record, other_part, other, other_grade = parts[candidate_index]
            if grade != other_grade:
                continue
            intersection = line.intersection(other)
            points = list(_point_parts(intersection))
            if not points:
                continue
            result.setdefault((record_index, part_index), []).extend(points)
            result.setdefault((other_record, other_part), []).extend(points)
    return result


def _simplify_projected_records(
    records: Sequence[ProjectedRecord],
    *,
    tolerance_mm: float,
    clip_geometry: BaseGeometry,
) -> tuple[list[ProjectedRecord], dict[str, int]]:
    junctions = _road_junctions(records)
    result: list[ProjectedRecord] = []
    collapsed = 0
    for record_index, projected in enumerate(records):
        geometry = projected.geometry
        if projected.record.kind in ROAD_KINDS:
            parts = [
                _simplify_line_at_junctions(
                    line,
                    junctions.get((record_index, part_index), ()),
                    tolerance_mm,
                )
                for part_index, line in enumerate(_line_parts(geometry))
            ]
            line_parts = [line for part in parts for line in _line_parts(part)]
            geometry = unary_union(line_parts) if line_parts else GeometryCollection()
        elif projected.record.kind in AREA_KINDS:
            geometry = geometry.simplify(tolerance_mm, preserve_topology=True)
        else:
            line_tolerance = (
                min(tolerance_mm, 0.04)
                if projected.record.kind in {"contour", "coastline", "water_line"}
                else tolerance_mm
            )
            geometry = geometry.simplify(line_tolerance, preserve_topology=True)
        geometry = geometry.intersection(clip_geometry)
        if geometry.is_empty:
            collapsed += 1
            continue
        result.append(ProjectedRecord(record=projected.record, geometry=geometry))
    return result, {"simplification_collapsed_feature_count": collapsed}


def _pen_for(ink: str, target_nib_mm: float) -> str:
    candidates = [
        pen for pen in PENS_BY_ID.values() if pen.ink.casefold() == ink.casefold()
    ]
    exact = [
        pen
        for pen in candidates
        if math.isclose(pen.mark_width_mm, target_nib_mm, abs_tol=1e-9)
    ]
    if exact:
        return exact[0].identity
    not_broader = [pen for pen in candidates if pen.mark_width_mm <= target_nib_mm]
    if not_broader:
        return max(not_broader, key=lambda pen: pen.mark_width_mm).identity
    if candidates:
        return min(candidates, key=lambda pen: pen.mark_width_mm).identity
    raise MapPlotterError(f"The studio inventory has no {ink} pen.")


def _semantic_pens(
    context: PlateContext, policy: PresetPolicy, overrides: Mapping[str, str]
) -> dict[str, str]:
    nibs = context.plate["map_linework_nib_mm"]
    hairline = float(nibs["hairline"])
    text = float(nibs["text"])
    primary = float(nibs["primary"])
    heavy = float(nibs["heavy"])
    monochrome = policy.id == "monochrome-street-portrait"
    blueprint = policy.id == "urban-blueprint"
    pens = {
        "road_major": _pen_for("Black", heavy),
        "road_secondary": _pen_for("Black", primary),
        "road_local": _pen_for("Black", text),
        "road_minor": _pen_for("Grey", hairline),
        "water_outline": _pen_for("Grey" if monochrome else "Blue", primary),
        "water_texture": _pen_for("Grey" if monochrome else "Blue", hairline),
        "park_outline": _pen_for("Grey" if monochrome else "Green", hairline),
        "park_texture": _pen_for("Grey" if monochrome else "Green", hairline),
        "rail": _pen_for(
            "Red" if blueprint or policy.id == "city-layers" else "Grey", hairline
        ),
        "building": _pen_for("Black", text),
        "building_hero": _pen_for("Black", primary),
        "building_texture": _pen_for("Grey", hairline),
        "contour": _pen_for("Grey", hairline),
        "contour_index": _pen_for("Black", text),
        "boundary": _pen_for(
            "Purple" if policy.id == "city-layers" else "Grey", hairline
        ),
        "route": _pen_for("Red", primary),
        "relationship": _pen_for("Red", hairline),
        "landmark": _pen_for(
            "Red" if policy.id in {"landmark-radius", "our-places"} else "Black",
            primary,
        ),
        "labels": _pen_for("Black", text),
        "annotations": _pen_for("Black", hairline),
        "decorative": _pen_for("Grey", hairline),
    }
    for role, pen_id in overrides.items():
        if role not in pens:
            _fail(
                f"options.pen_roles contains unknown semantic role {role!r}; "
                f"choose {', '.join(sorted(pens))}."
            )
        pens[role] = str(pen_id)
    ladder = [float(value) for value in context.plate["nib_ladder_mm"]]
    for role, pen_id in pens.items():
        nib = PENS_BY_ID[pen_id].mark_width_mm
        if not any(math.isclose(nib, allowed, abs_tol=1e-9) for allowed in ladder):
            _fail(
                f"semantic pen role {role!r} selects {pen_id!r}, whose {nib:g} mm "
                f"nib is not on the {context.format_id} ladder."
            )
    return pens


def _line_key(points: Sequence[PointTuple]) -> tuple[PointTuple, ...]:
    rounded = tuple((round(x, 3), round(y, 3)) for x, y in points)
    reversed_points = tuple(reversed(rounded))
    return min(rounded, reversed_points)


def _near_duplicate_lines(
    first: LineString, second: LineString, *, tolerance_mm: float
) -> bool:
    """Recognise a whole-line physical retrace, not merely a nearby road."""

    if tolerance_mm <= 0.0:
        return False
    maximum_length = max(first.length, second.length)
    if abs(first.length - second.length) > max(
        2.0 * tolerance_mm, 0.02 * maximum_length
    ):
        return False
    first_start = Point(first.coords[0])
    first_end = Point(first.coords[-1])
    second_start = Point(second.coords[0])
    second_end = Point(second.coords[-1])
    endpoints_match = (
        first_start.distance(second_start) <= tolerance_mm
        and first_end.distance(second_end) <= tolerance_mm
    ) or (
        first_start.distance(second_end) <= tolerance_mm
        and first_end.distance(second_start) <= tolerance_mm
    )
    return endpoints_match and first.hausdorff_distance(second) <= tolerance_mm


def _hatch_polygon(
    geometry: BaseGeometry,
    *,
    spacing_mm: float,
    angle_deg: float,
    organic: bool,
    seed: int,
) -> list[LineString]:
    if geometry.is_empty or spacing_mm <= 0:
        return []
    rotated = affinity.rotate(geometry, -angle_deg, origin=(0.0, 0.0))
    min_x, min_y, max_x, max_y = rotated.bounds
    randomizer = random.Random(seed)
    start = math.floor(min_y / spacing_mm) * spacing_mm
    strokes: list[LineString] = []
    row = 0
    y_value = start
    while y_value <= max_y + spacing_mm:
        jitter = randomizer.uniform(-0.16, 0.16) * spacing_mm if organic else 0.0
        y = y_value + jitter
        if organic:
            step = max(1.2, spacing_mm * 1.6)
            count = max(2, int(math.ceil((max_x - min_x + 2 * spacing_mm) / step)))
            phase = randomizer.uniform(0.0, 2.0 * math.pi)
            points = [
                (
                    min_x
                    - spacing_mm
                    + index * (max_x - min_x + 2 * spacing_mm) / count,
                    y + 0.12 * spacing_mm * math.sin(index * 0.9 + phase),
                )
                for index in range(count + 1)
            ]
            guide: BaseGeometry = LineString(points)
        else:
            guide = LineString([(min_x - spacing_mm, y), (max_x + spacing_mm, y)])
        clipped = guide.intersection(rotated)
        strokes.extend(_line_parts(clipped))
        row += 1
        y_value = start + row * spacing_mm
    return [affinity.rotate(line, angle_deg, origin=(0.0, 0.0)) for line in strokes]


def _wave_hatch(
    geometry: BaseGeometry, *, spacing_mm: float, seed: int
) -> list[LineString]:
    if geometry.is_empty:
        return []
    min_x, min_y, max_x, max_y = geometry.bounds
    randomizer = random.Random(seed)
    start = math.floor(min_y / spacing_mm) * spacing_mm
    lines: list[LineString] = []
    row = 0
    y = start
    while y <= max_y + spacing_mm:
        phase = randomizer.uniform(0.0, 2.0 * math.pi)
        wavelength = max(7.0, 4.0 * spacing_mm)
        count = max(8, int(math.ceil((max_x - min_x + 4.0) / 1.2)))
        points = [
            (
                min_x - 2.0 + index * (max_x - min_x + 4.0) / count,
                y
                + 0.18
                * math.sin(
                    2.0 * math.pi * (index * (max_x - min_x + 4.0) / count) / wavelength
                    + phase
                ),
            )
            for index in range(count + 1)
        ]
        lines.extend(_line_parts(LineString(points).intersection(geometry)))
        row += 1
        y = start + row * spacing_mm
    return lines


ROAD_PRIORITY = {
    "road_major": 7,
    "road_secondary": 6,
    "road_local": 5,
    "road_service": 3,
    "road_pedestrian": 2,
    "road_cycleway": 2,
    "road_path": 1,
}

TYPICAL_ROAD_SEGMENT_M = {
    "road_major": 450.0,
    "road_secondary": 250.0,
    "road_local": 90.0,
    "road_service": 50.0,
    "road_pedestrian": 35.0,
    "road_cycleway": 45.0,
    "road_path": 30.0,
}


class _Composer:
    def __init__(
        self,
        request: PlaceRequest,
        context: PlateContext,
        policy: PresetPolicy,
        projected: Sequence[ProjectedRecord],
        clip_geometry: BaseGeometry,
        page_transform: PageTransform,
    ) -> None:
        self.request = request
        self.context = context
        self.policy = policy
        self.projected = list(projected)
        self.clip_geometry = clip_geometry
        self.page_transform = page_transform
        self.pens = _semantic_pens(
            context, policy, request.options.get("pen_roles", {})
        )
        self.planned: list[PlannedRecord] = []
        self._geometry_keys: set[tuple[str, tuple[PointTuple, ...]]] = set()
        self.omissions: dict[str, int] = {}
        self.selection: dict[str, Any] = {}
        self.label_boxes: list[BaseGeometry] = []

    def omit(self, reason: str, count: int = 1) -> None:
        self.omissions[reason] = self.omissions.get(reason, 0) + count

    def _minimum_length(self, pen_id: str) -> float:
        physical = 3.0 * PENS_BY_ID[pen_id].mark_width_mm
        requested = self.request.options.get("minimum_movement_mm")
        return max(physical, float(requested)) if requested is not None else physical

    def add_line(
        self,
        *,
        layer_id: str,
        label: str,
        pen_id: str,
        line: LineString,
        source_ref: str | None,
        role: str,
        importance: float,
        required: bool = False,
        removable_group: str = "context",
        attributes: Mapping[str, str] | None = None,
        deduplicate: bool = True,
    ) -> bool:
        if line.is_empty or len(line.coords) < 2:
            self.omit("empty_after_clipping")
            return False
        points = [(float(x), float(y)) for x, y, *_ in line.coords]
        length = polyline_length_mm(points)
        if length + 1e-9 < self._minimum_length(pen_id):
            self.omit("below_three_nib_movement")
            return False
        key = (pen_id, _line_key(points))
        if deduplicate and key in self._geometry_keys:
            self.omit("duplicate_or_retraced_line")
            return False
        self._geometry_keys.add(key)
        self.planned.append(
            PlannedRecord(
                layer_id=layer_id,
                label=label,
                pen_id=pen_id,
                points=points,
                source_ref=source_ref,
                role=role,
                importance=importance,
                required=required,
                removable_group=removable_group,
                attributes=dict(attributes or {}),
            )
        )
        return True

    def add_geometry_lines(self, geometry: BaseGeometry, **values: Any) -> int:
        return sum(self.add_line(line=line, **values) for line in _line_parts(geometry))

    def records_of_kind(self, *kinds: str) -> list[ProjectedRecord]:
        wanted = set(kinds)
        return [item for item in self.projected if item.record.kind in wanted]

    def _road_pen(self, kind: str) -> str:
        if kind == "road_major":
            return self.pens["road_major"]
        if kind == "road_secondary":
            return self.pens["road_secondary"]
        if kind == "road_local":
            return self.pens["road_local"]
        return self.pens["road_minor"]

    def _road_importance(self, item: ProjectedRecord, line: LineString) -> float:
        properties = item.record.properties
        named = bool(properties.get("name") or properties.get("ref"))
        selected = item.record.id in {
            self.request.options.get("route_feature_id"),
            self.request.options.get("focal_feature_id"),
        }
        explicit = properties.get("importance", 0)
        explicit_number = (
            float(explicit)
            if isinstance(explicit, (int, float)) and not isinstance(explicit, bool)
            else 0.0
        )
        return (
            ROAD_PRIORITY[item.record.kind] * 100.0
            + (35.0 if named else 0.0)
            + min(line.length, 500.0) / 10.0
            + explicit_number
            + (10_000.0 if selected else 0.0)
        )

    def _spatial_road_order(
        self, candidates: Sequence[tuple[ProjectedRecord, LineString, float]]
    ) -> list[tuple[ProjectedRecord, LineString, float]]:
        if not candidates:
            return []
        field = self.context.field
        cells: dict[
            tuple[int, int], list[tuple[ProjectedRecord, LineString, float]]
        ] = {}
        for candidate in candidates:
            point = candidate[1].interpolate(0.5, normalized=True)
            column = min(5, max(0, int(6 * (point.x - field.left) / field.width)))
            row = min(7, max(0, int(8 * (point.y - field.top) / field.height)))
            cells.setdefault((row, column), []).append(candidate)
        for values in cells.values():
            values.sort(
                key=lambda value: (-value[2], value[0].record.id, value[1].wkb_hex)
            )
        ordered: list[tuple[ProjectedRecord, LineString, float]] = []
        depth = 0
        while True:
            added = False
            for cell in sorted(cells):
                if depth < len(cells[cell]):
                    ordered.append(cells[cell][depth])
                    added = True
            if not added:
                break
            depth += 1
        return ordered

    def add_roads(self) -> None:
        road_records = [
            item
            for item in self.projected
            if item.record.kind in self.policy.road_kinds
        ]
        detail = str(self.request.options["detail"])
        legibility_floor = {"sparse": 1.2, "medium": 0.9, "dense": 0.7}[detail]
        allowed_kinds = set(self.policy.road_kinds)
        scale = self.page_transform.scale_mm_per_m
        for kind in tuple(allowed_kinds):
            typical_mm = TYPICAL_ROAD_SEGMENT_M[kind] * scale
            if typical_mm + 1e-9 < legibility_floor:
                if not (
                    self.policy.hero == "roads"
                    and kind in {"road_major", "road_secondary", "road_local"}
                ):
                    allowed_kinds.remove(kind)
                    self.omit(f"scale_suppressed_{kind}")
        if detail == "sparse":
            allowed_kinds -= {
                "road_service",
                "road_pedestrian",
                "road_path",
                "road_cycleway",
            }
        elif detail == "medium":
            allowed_kinds -= {"road_path"}
        candidates: list[tuple[ProjectedRecord, LineString, float]] = []
        for item in road_records:
            if item.record.kind not in allowed_kinds:
                self.omit("detail_suppressed_road")
                continue
            for line in _line_parts(item.geometry):
                candidates.append((item, line, self._road_importance(item, line)))
        ordered = [
            candidate
            for rank in sorted(set(ROAD_PRIORITY.values()), reverse=True)
            for candidate in self._spatial_road_order(
                [
                    item
                    for item in candidates
                    if ROAD_PRIORITY[item[0].record.kind] == rank
                ]
            )
        ]
        road_budget = (
            self.context.field.width
            * self.context.field.height
            * self.policy.road_coverage_fraction
        )
        used = 0.0
        selected = 0
        dropped_budget = 0
        dropped_near_duplicate = 0
        factual_road_keys: set[tuple[PointTuple, ...]] = set()
        road_bucket_mm = 2.0 * max(
            PENS_BY_ID[self._road_pen(kind)].mark_width_mm for kind in allowed_kinds
        )
        road_buckets: dict[tuple[int, int], list[tuple[LineString, str]]] = {}
        for item, line, importance in ordered:
            pen_id = self._road_pen(item.record.kind)
            road_key = _line_key([(float(x), float(y)) for x, y, *_ in line.coords])
            if road_key in factual_road_keys:
                self.omit("duplicate_or_retraced_road")
                continue
            endpoints = sorted(
                (
                    (float(line.coords[0][0]), float(line.coords[0][1])),
                    (float(line.coords[-1][0]), float(line.coords[-1][1])),
                )
            )
            bucket = (
                math.floor(endpoints[0][0] / road_bucket_mm),
                math.floor(endpoints[0][1] / road_bucket_mm),
            )
            near_duplicate = False
            for x_offset in (-1, 0, 1):
                for y_offset in (-1, 0, 1):
                    for existing, existing_pen_id in road_buckets.get(
                        (bucket[0] + x_offset, bucket[1] + y_offset), ()
                    ):
                        tolerance = 0.20 * min(
                            PENS_BY_ID[pen_id].mark_width_mm,
                            PENS_BY_ID[existing_pen_id].mark_width_mm,
                        )
                        if _near_duplicate_lines(
                            line, existing, tolerance_mm=tolerance
                        ):
                            near_duplicate = True
                            break
                    if near_duplicate:
                        break
                if near_duplicate:
                    break
            if near_duplicate:
                dropped_near_duplicate += 1
                self.omit("near_duplicate_or_retraced_road")
                continue
            ink = line.length * PENS_BY_ID[pen_id].mark_width_mm
            required = item.record.kind in {"road_major", "road_secondary"}
            if used + ink > road_budget and not required:
                dropped_budget += 1
                continue
            before = len(self.planned)
            self.add_line(
                layer_id=item.record.kind,
                label=item.record.kind.replace("_", " ").title(),
                pen_id=pen_id,
                line=line,
                source_ref=item.record.source_ref,
                role=item.record.kind,
                importance=importance,
                required=required and self.policy.hero in {"roads", "transport"},
                removable_group=(
                    "local_roads"
                    if item.record.kind == "road_local"
                    else "minor_roads"
                    if item.record.kind not in {"road_major", "road_secondary"}
                    else "transport"
                ),
                attributes={
                    "data-source-geometry-sha256": item.record.source_geometry_sha256,
                    "data-road-rank": str(ROAD_PRIORITY[item.record.kind]),
                },
            )
            if len(self.planned) > before:
                factual_road_keys.add(road_key)
                road_buckets.setdefault(bucket, []).append((line, pen_id))
                used += ink
                selected += 1
        if dropped_budget:
            self.omit("road_ink_budget", dropped_budget)
        self.selection["roads"] = {
            "candidate_part_count": len(candidates),
            "selected_part_count": selected,
            "budget_omitted_part_count": dropped_budget,
            "near_duplicate_omitted_part_count": dropped_near_duplicate,
            "allowed_kinds": sorted(allowed_kinds),
            "budget_mm2": round(road_budget, 6),
            "selected_ink_mm2_upper_bound": round(used, 6),
            "legibility_floor_mm": legibility_floor,
        }

    def _building_role(self, item: ProjectedRecord) -> tuple[float, bool]:
        properties = item.record.properties
        selected = item.record.id in set(
            self.request.options.get("selected_building_ids", [])
        )
        focal = item.record.id == self.request.options.get("focal_feature_id")
        campus = (
            item.record.kind == "campus_building" or properties.get("campus") is True
        )
        named = bool(properties.get("name"))
        building_type = str(properties.get("building", "")).casefold()
        landmark = building_type in {
            "cathedral",
            "church",
            "stadium",
            "university",
            "college",
            "civic",
            "public",
            "train_station",
            "castle",
            "palace",
        }
        importance = (
            (20_000.0 if focal else 0.0)
            + (10_000.0 if selected else 0.0)
            + (1_000.0 if campus else 0.0)
            + (500.0 if landmark else 0.0)
            + (100.0 if named else 0.0)
            + min(item.geometry.area, 500.0)
        )
        return importance, focal or selected or (
            campus and self.policy.hero == "campus"
        )

    def add_buildings(self) -> None:
        density = str(self.request.options["building_density"])
        candidates = self.records_of_kind("building", "campus_building")
        if density == "off" and self.policy.hero != "campus":
            self.omit("building_density_off", len(candidates))
            self.selection["buildings"] = {
                "candidate_count": len(candidates),
                "selected_count": 0,
            }
            return
        sheet = str(self.context.plate["sheet"]).casefold()
        limits = {
            "a5": {"sparse": 12, "medium": 36, "dense": 80},
            "a4": {"sparse": 24, "medium": 80, "dense": 180},
            "a3": {"sparse": 36, "medium": 150, "dense": 320},
        }
        effective_density = "medium" if density == "off" else density
        limit = limits[sheet][effective_density]
        ranked = sorted(
            (
                (item, *self._building_role(item))
                for item in candidates
                if any(True for _ in _polygon_parts(item.geometry))
            ),
            key=lambda value: (-value[1], value[0].record.id),
        )
        chosen: list[tuple[ProjectedRecord, float, bool]] = []
        budget_fraction = {
            "sparse": 0.012,
            "medium": 0.028,
            "dense": 0.05,
        }[effective_density]
        budget = self.context.field.width * self.context.field.height * budget_fraction
        used = 0.0
        for item, importance, required in ranked:
            pen_id = self.pens["building_hero"] if required else self.pens["building"]
            perimeter = sum(polygon.length for polygon in _polygon_parts(item.geometry))
            ink = perimeter * PENS_BY_ID[pen_id].mark_width_mm
            if len(chosen) >= limit and not required:
                continue
            if used + ink > budget and not required:
                continue
            chosen.append((item, importance, required))
            used += ink
        chosen_ids = {item.record.id for item, _, _ in chosen}
        for selected_id in self.request.options.get("selected_building_ids", []):
            if selected_id not in {item.record.id for item in candidates}:
                _fail(f"selected building {selected_id!r} does not exist.")
            if selected_id not in chosen_ids:
                _fail(
                    f"selected building {selected_id!r} cannot be drawn at the physical scale."
                )
        for item, importance, required in chosen:
            pen_id = self.pens["building_hero"] if required else self.pens["building"]
            self.add_geometry_lines(
                item.geometry.boundary,
                layer_id=(
                    "campus_buildings"
                    if item.record.kind == "campus_building"
                    else "buildings"
                ),
                label=(
                    "Principal campus buildings"
                    if item.record.kind == "campus_building"
                    else "Selected building footprints"
                ),
                pen_id=pen_id,
                source_ref=item.record.source_ref,
                role=(
                    "campus-building"
                    if item.record.kind == "campus_building"
                    else "building-outline"
                ),
                importance=importance,
                required=required,
                removable_group="buildings",
                attributes={
                    "data-source-geometry-sha256": item.record.source_geometry_sha256
                },
            )
            if effective_density == "dense" and (required or importance >= 500.0):
                for hatch in _hatch_polygon(
                    item.geometry,
                    spacing_mm=1.55,
                    angle_deg=45.0,
                    organic=False,
                    seed=int(self.request.options["seed"]),
                ):
                    self.add_line(
                        layer_id="building_texture",
                        label="Sparse building hatch",
                        pen_id=self.pens["building_texture"],
                        line=hatch,
                        source_ref=item.record.source_ref,
                        role="building-hatch",
                        importance=30.0,
                        removable_group="building_texture",
                        deduplicate=False,
                    )
        omitted = max(0, len(ranked) - len(chosen))
        if omitted:
            self.omit("building_density_or_budget", omitted)
        self.selection["buildings"] = {
            "density": effective_density,
            "candidate_count": len(ranked),
            "selected_count": len(chosen),
            "object_limit": limit,
            "ink_budget_mm2": round(budget, 6),
            "selected_ink_mm2_upper_bound": round(used, 6),
            "selected_ids": sorted(chosen_ids),
        }

    def add_water(self) -> None:
        water_records = self.records_of_kind("water_area", "water_line", "coastline")
        outline_parts = 0
        texture_parts = 0
        texture = str(self.request.options["water_texture"])
        sheet = str(self.context.plate["sheet"]).casefold()
        spacing = {"a5": 3.6, "a4": 3.1, "a3": 2.7}[sheet]
        if self.request.options["detail"] == "sparse":
            spacing *= 1.25
        elif self.request.options["detail"] == "dense":
            spacing *= 0.86
        for item_index, item in enumerate(water_records):
            required = self.policy.hero == "water"
            if item.record.kind == "water_area":
                outline_parts += self.add_geometry_lines(
                    item.geometry.boundary,
                    layer_id="water_outlines",
                    label="Water and harbour outlines",
                    pen_id=self.pens["water_outline"],
                    source_ref=item.record.source_ref,
                    role="water-boundary",
                    importance=900.0 if required else 180.0,
                    required=required,
                    removable_group="water",
                    attributes={
                        "data-source-geometry-sha256": item.record.source_geometry_sha256
                    },
                )
                if texture == "negative":
                    continue
                polygons = list(_polygon_parts(item.geometry))
                for polygon_index, polygon in enumerate(polygons):
                    if texture == "hatch":
                        hatches = _hatch_polygon(
                            polygon,
                            spacing_mm=spacing,
                            angle_deg=0.0,
                            organic=False,
                            seed=int(self.request.options["seed"])
                            + item_index * 97
                            + polygon_index,
                        )
                    elif texture == "waves":
                        hatches = _wave_hatch(
                            polygon,
                            spacing_mm=spacing,
                            seed=int(self.request.options["seed"])
                            + item_index * 97
                            + polygon_index,
                        )
                    else:
                        hatches = []
                        inset = spacing
                        while inset <= spacing * 5.0:
                            inner = polygon.buffer(-inset)
                            if inner.is_empty:
                                break
                            hatches.extend(_line_parts(inner.boundary))
                            inset += spacing
                    for hatch in hatches:
                        texture_parts += int(
                            self.add_line(
                                layer_id="water_texture",
                                label="Plotter-native water texture",
                                pen_id=self.pens["water_texture"],
                                line=hatch,
                                source_ref=item.record.source_ref,
                                role=f"water-{texture}",
                                importance=110.0 if required else 25.0,
                                required=False,
                                removable_group="water_texture",
                                deduplicate=False,
                            )
                        )
            else:
                role = (
                    "coastline"
                    if item.record.kind == "coastline"
                    else "water-centreline"
                )
                outline_parts += self.add_geometry_lines(
                    item.geometry,
                    layer_id=(
                        "coastline" if item.record.kind == "coastline" else "waterways"
                    ),
                    label=(
                        "Coastline"
                        if item.record.kind == "coastline"
                        else "Rivers and canals"
                    ),
                    pen_id=self.pens["water_outline"],
                    source_ref=item.record.source_ref,
                    role=role,
                    importance=1_000.0 if required else 210.0,
                    required=required,
                    removable_group="water",
                    attributes={
                        "data-source-geometry-sha256": item.record.source_geometry_sha256
                    },
                )
        self.selection["water"] = {
            "source_feature_count": len(water_records),
            "outline_part_count": outline_parts,
            "texture_part_count": texture_parts,
            "texture": texture,
            "texture_spacing_mm": round(spacing, 3),
            "solid_fills_used": False,
        }

    def add_parks(self) -> None:
        park_records = self.records_of_kind("park_area")
        outline_count = 0
        texture_count = 0
        texture = str(self.request.options["park_texture"])
        spacing = {"sparse": 5.0, "medium": 4.1, "dense": 3.4}[
            str(self.request.options["detail"])
        ]
        for item_index, item in enumerate(park_records):
            outline_count += self.add_geometry_lines(
                item.geometry.boundary,
                layer_id="park_outlines",
                label="Parks, woodland, and open land",
                pen_id=self.pens["park_outline"],
                source_ref=item.record.source_ref,
                role="land-cover-boundary",
                importance=75.0,
                removable_group="park",
                attributes={
                    "data-source-geometry-sha256": item.record.source_geometry_sha256
                },
            )
            if texture == "none":
                continue
            for polygon_index, polygon in enumerate(_polygon_parts(item.geometry)):
                for hatch in _hatch_polygon(
                    polygon,
                    spacing_mm=spacing,
                    angle_deg=35.0,
                    organic=texture == "organic",
                    seed=int(self.request.options["seed"])
                    + 503 * item_index
                    + polygon_index,
                ):
                    texture_count += int(
                        self.add_line(
                            layer_id="park_texture",
                            label="Sparse land-cover texture",
                            pen_id=self.pens["park_texture"],
                            line=hatch,
                            source_ref=item.record.source_ref,
                            role=f"park-{texture}",
                            importance=12.0,
                            removable_group="park_texture",
                            deduplicate=False,
                        )
                    )
        self.selection["parks"] = {
            "source_feature_count": len(park_records),
            "outline_part_count": outline_count,
            "texture_part_count": texture_count,
            "texture": texture,
            "texture_spacing_mm": spacing,
            "solid_fills_used": False,
        }

    def add_contours(self) -> tuple[float | None, float | None]:
        contour_records = self.records_of_kind("contour")
        if not contour_records:
            self.selection["contours"] = {
                "source_feature_count": 0,
                "selected_feature_count": 0,
            }
            return (None, None)
        elevations: dict[str, float] = {}
        for item in contour_records:
            raw = item.record.properties.get(
                "elevation", item.record.properties.get("ele")
            )
            if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
                _fail(f"contour feature {item.record.id!r} needs a numeric elevation.")
            try:
                elevation = float(raw)
            except (TypeError, ValueError):
                _fail(f"contour feature {item.record.id!r} needs a numeric elevation.")
            if not math.isfinite(elevation):
                _fail(f"contour feature {item.record.id!r} has non-finite elevation.")
            elevations[item.record.id] = elevation
        unique = sorted(set(elevations.values()))
        positive_differences = [
            second - first
            for first, second in zip(unique, unique[1:], strict=False)
            if second - first > 1e-9
        ]
        base_interval = float(
            self.request.options.get(
                "contour_interval_m",
                min(positive_differences) if positive_differences else 1.0,
            )
        )
        target_by_sheet = {"A5": 42, "A4": 72, "A3": 118}
        target = target_by_sheet[str(self.context.plate["sheet"])]
        detail_factor = {"sparse": 0.62, "medium": 1.0, "dense": 1.35}[
            str(self.request.options["detail"])
        ]
        target = max(8, round(target * detail_factor))
        multiplier = max(1, math.ceil(len(unique) / target))
        selected_elevations = set(unique[::multiplier])
        selected_elevations.add(unique[-1])
        effective_interval = base_interval * multiplier
        index_every = int(self.request.options["index_contour_every"])
        selected_count = 0
        for item in contour_records:
            elevation = elevations[item.record.id]
            if elevation not in selected_elevations:
                self.omit("contour_interval_thinning")
                continue
            index_value = round((elevation - unique[0]) / effective_interval)
            indexed = index_value % index_every == 0
            pen_id = self.pens["contour_index"] if indexed else self.pens["contour"]
            selected_count += self.add_geometry_lines(
                item.geometry,
                layer_id=("indexed_contours" if indexed else "contours"),
                label=(
                    "Indexed elevation contours" if indexed else "Elevation contours"
                ),
                pen_id=pen_id,
                source_ref=item.record.source_ref,
                role=("indexed-contour" if indexed else "contour"),
                importance=1_000.0 if indexed else 760.0,
                required=self.policy.hero == "contours",
                removable_group="contours",
                attributes={
                    "data-elevation-m": f"{elevation:g}",
                    "data-source-geometry-sha256": item.record.source_geometry_sha256,
                },
            )
        self.selection["contours"] = {
            "source_feature_count": len(contour_records),
            "source_elevation_count": len(unique),
            "selected_elevation_count": len(selected_elevations),
            "selected_part_count": selected_count,
            "base_interval_m": base_interval,
            "thinning_multiplier": multiplier,
            "effective_interval_m": effective_interval,
            "index_every": index_every,
            "elevation_min_m": unique[0],
            "elevation_max_m": unique[-1],
            "terrain_geometry_invented": False,
        }
        return (unique[0], unique[-1])

    def add_rail_and_boundaries(self) -> None:
        rail_count = 0
        for item in self.records_of_kind("rail_main", "rail_transit"):
            rail_count += self.add_geometry_lines(
                item.geometry,
                layer_id=(
                    "railways" if item.record.kind == "rail_main" else "urban_transit"
                ),
                label=(
                    "Railways"
                    if item.record.kind == "rail_main"
                    else "Tram, metro, and light rail"
                ),
                pen_id=self.pens["rail"],
                source_ref=item.record.source_ref,
                role=item.record.kind.replace("_", "-"),
                importance=620.0 if item.record.kind == "rail_main" else 480.0,
                required=self.policy.hero == "transport",
                removable_group="transport",
                attributes={
                    "data-source-geometry-sha256": item.record.source_geometry_sha256
                },
            )
        boundary_count = 0
        for item in self.records_of_kind("boundary"):
            boundary_count += self.add_geometry_lines(
                item.geometry,
                layer_id="boundaries",
                label="District and regional boundaries",
                pen_id=self.pens["boundary"],
                source_ref=item.record.source_ref,
                role="boundary",
                importance=350.0 if self.policy.id == "minimal-coordinates" else 90.0,
                required=self.policy.id == "minimal-coordinates"
                and not self.records_of_kind("water_area", "water_line", "coastline"),
                removable_group="boundary",
                attributes={
                    "data-source-geometry-sha256": item.record.source_geometry_sha256
                },
            )
        self.selection["transport_and_boundaries"] = {
            "rail_part_count": rail_count,
            "boundary_part_count": boundary_count,
        }

    def add_campus(self) -> None:
        count = 0
        for item in self.records_of_kind("campus_boundary"):
            count += self.add_geometry_lines(
                item.geometry.boundary,
                layer_id="campus_boundary",
                label="Campus boundary",
                pen_id=self.pens["building_hero"],
                source_ref=item.record.source_ref,
                role="campus-boundary",
                importance=5_000.0,
                required=self.policy.hero == "campus",
                removable_group="campus",
                attributes={
                    "data-source-geometry-sha256": item.record.source_geometry_sha256
                },
            )
        self.selection["campus_boundary_part_count"] = count

    def _focal_record(self) -> ProjectedRecord | None:
        focal_id = self.request.options.get("focal_feature_id")
        candidates = [
            item
            for item in self.projected
            if item.record.kind
            in {"landmark", "landmark_outline", "building", "campus_building"}
            and (
                item.record.properties.get("focal") is True
                or item.record.kind in {"landmark", "landmark_outline"}
            )
        ]
        if focal_id is not None:
            match = next(
                (item for item in self.projected if item.record.id == focal_id), None
            )
            if match is None:
                _fail(f"options.focal_feature_id {focal_id!r} does not exist.")
            return match
        if self.policy.id != "landmark-radius":
            return None
        if len(candidates) != 1:
            _fail(
                "landmark-radius needs exactly one focal landmark, or an explicit "
                "options.focal_feature_id."
            )
        return candidates[0]

    def add_focal_and_route(self) -> None:
        focal = self._focal_record()
        focal_parts = 0
        if focal is not None:
            if focal.geometry.geom_type in {"Point", "MultiPoint"}:
                points = list(_point_parts(focal.geometry))
                if not points:
                    point = focal.geometry.representative_point()
                    points = [point]
                for point in points:
                    marker = LineString(
                        circle_stroke((point.x, point.y), 2.8, segments=28)
                    )
                    focal_parts += int(
                        self.add_line(
                            layer_id="focal_landmark",
                            label="Selected focal landmark",
                            pen_id=self.pens["landmark"],
                            line=marker,
                            source_ref=focal.record.source_ref,
                            role="focal-landmark-marker",
                            importance=20_000.0,
                            required=True,
                            removable_group="focal",
                            deduplicate=False,
                        )
                    )
            else:
                geometry = (
                    focal.geometry.boundary
                    if any(True for _ in _polygon_parts(focal.geometry))
                    else focal.geometry
                )
                focal_parts += self.add_geometry_lines(
                    geometry,
                    layer_id="focal_landmark",
                    label="Selected focal landmark outline",
                    pen_id=self.pens["landmark"],
                    source_ref=focal.record.source_ref,
                    role="focal-landmark-outline",
                    importance=20_000.0,
                    required=True,
                    removable_group="focal",
                    attributes={
                        "data-source-geometry-sha256": focal.record.source_geometry_sha256
                    },
                )
        route_id = self.request.options.get("route_feature_id")
        route_records = self.records_of_kind("route")
        if route_id is not None:
            route_records = [
                item for item in self.projected if item.record.id == route_id
            ]
            if not route_records:
                _fail(f"options.route_feature_id {route_id!r} does not exist.")
        route_parts = 0
        for item in route_records:
            route_parts += self.add_geometry_lines(
                item.geometry,
                layer_id="highlighted_route",
                label="Supplied focal route",
                pen_id=self.pens["route"],
                source_ref=item.record.source_ref,
                role="supplied-highlight-route",
                importance=9_000.0,
                required=True,
                removable_group="route",
                attributes={
                    "data-route-claim": "supplied-geometry-not-inferred",
                    "data-source-geometry-sha256": item.record.source_geometry_sha256,
                },
            )
        self.selection["focal_and_route"] = {
            "focal_feature_id": focal.record.id if focal is not None else None,
            "focal_part_count": focal_parts,
            "route_part_count": route_parts,
        }

    def _milestone_point(self, item: ProjectedRecord) -> Point:
        if isinstance(item.geometry, Point):
            return item.geometry
        return item.geometry.representative_point()

    def add_milestones(self) -> list[ProjectedRecord]:
        milestones = self.records_of_kind("milestone")
        if self.policy.id != "our-places" and not milestones:
            return []
        if self.policy.id == "our-places" and not 2 <= len(milestones) <= 5:
            _fail("our-places requires two to five milestone features.")
        milestones.sort(
            key=lambda item: (
                int(item.record.properties.get("sequence", 999))
                if str(item.record.properties.get("sequence", "")).isdigit()
                else 999,
                str(item.record.properties.get("date", "")),
                item.record.id,
            )
        )
        points = [self._milestone_point(item) for item in milestones]
        connection_count = 0
        for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
            dx = end.x - start.x
            dy = end.y - start.y
            length = math.hypot(dx, dy)
            if length <= 1e-9:
                continue
            normal_x, normal_y = -dy / length, dx / length
            bow = min(9.0, 0.08 * length) * (1.0 if index % 2 == 0 else -1.0)
            sampled: list[PointTuple] = []
            for sample in range(25):
                t = sample / 24.0
                base_x = start.x + dx * t
                base_y = start.y + dy * t
                offset = 4.0 * t * (1.0 - t) * bow
                sampled.append((base_x + normal_x * offset, base_y + normal_y * offset))
            for part in _line_parts(
                LineString(sampled).intersection(self.clip_geometry)
            ):
                connection_count += int(
                    self.add_line(
                        layer_id="relationship_connections",
                        label="Relationship connections (not routes)",
                        pen_id=self.pens["relationship"],
                        line=part,
                        source_ref="derived:relationship-connection",
                        role="relationship-connection-not-route",
                        importance=8_000.0,
                        required=True,
                        removable_group="relationship",
                        attributes={"data-factual-route-claim": "none"},
                        deduplicate=False,
                    )
                )
        marker_count = 0
        for index, (item, point) in enumerate(
            zip(milestones, points, strict=True), start=1
        ):
            marker = LineString(circle_stroke((point.x, point.y), 2.15, segments=24))
            marker_count += int(
                self.add_line(
                    layer_id="milestones",
                    label="Meaningful place markers",
                    pen_id=self.pens["landmark"],
                    line=marker,
                    source_ref=item.record.source_ref,
                    role="milestone-marker",
                    importance=10_000.0,
                    required=True,
                    removable_group="milestone",
                    attributes={"data-milestone-sequence": str(index)},
                    deduplicate=False,
                )
            )
        self.selection["milestones"] = {
            "count": len(milestones),
            "marker_part_count": marker_count,
            "connection_part_count": connection_count,
            "connections_are_routes": False,
            "ordered_ids": [item.record.id for item in milestones],
        }
        return milestones

    def add_crop_border(self) -> None:
        frame_shape = str(self.request.options["frame_shape"])
        if frame_shape == "rectangle":
            return
        count = self.add_geometry_lines(
            self.clip_geometry.boundary,
            layer_id="composition_crop",
            label="Composition crop boundary",
            pen_id=self.pens["decorative"],
            source_ref="derived:composition-crop",
            role=f"{frame_shape}-crop-boundary",
            importance=700.0,
            required=True,
            removable_group="decorative",
            attributes={
                "data-factual-geometry": "false",
                "data-organic-seed": str(self.request.options["seed"]),
            },
            deduplicate=False,
        )
        self.selection["composition_crop"] = {
            "shape": frame_shape,
            "border_part_count": count,
            "factual_geometry_perturbed": False,
        }

    def _ink_union(self) -> BaseGeometry:
        buffered = []
        for record in self.planned:
            line = LineString(record.points)
            radius = PENS_BY_ID[record.pen_id].mark_width_mm / 2.0 + 0.22
            buffered.append(line.buffer(radius, cap_style="flat", join_style="mitre"))
        return unary_union(buffered) if buffered else GeometryCollection()

    def _label_candidates(self, item: ProjectedRecord) -> list[PointTuple]:
        anchor = (
            item.geometry
            if isinstance(item.geometry, Point)
            else item.geometry.representative_point()
        )
        return [
            (anchor.x, anchor.y - 3.4),
            (anchor.x + 4.0, anchor.y - 1.2),
            (anchor.x - 4.0, anchor.y - 1.2),
            (anchor.x, anchor.y + 2.8),
            (anchor.x + 5.5, anchor.y + 2.8),
            (anchor.x - 5.5, anchor.y + 2.8),
        ]

    def add_labels(self) -> None:
        if (
            not self.request.options["labels"]
            or int(self.request.options["label_limit"]) == 0
        ):
            self.selection["labels"] = {
                "candidate_count": 0,
                "placed_count": 0,
                "dropped_count": 0,
            }
            return
        explicit_ids = set(self.request.options.get("label_feature_ids", []))
        focal_id = self.request.options.get("focal_feature_id")
        candidates: list[tuple[int, ProjectedRecord, str]] = []
        for item in self.projected:
            properties = item.record.properties
            text_value = properties.get("label") or properties.get("name")
            if not isinstance(text_value, str) or not text_value.strip():
                continue
            kind_allowed = item.record.kind in self.policy.label_kinds
            if "focal" in self.policy.label_kinds and (
                item.record.id == focal_id or properties.get("focal") is True
            ):
                kind_allowed = True
            if item.record.id in explicit_ids:
                kind_allowed = True
            if not kind_allowed:
                continue
            rank_value = properties.get("label_rank", 0)
            rank = (
                int(rank_value)
                if isinstance(rank_value, int) and not isinstance(rank_value, bool)
                else 0
            )
            if item.record.kind == "milestone":
                rank += 10_000
            if item.record.id == focal_id:
                rank += 9_000
            candidates.append((rank, item, plotter_copy(text_value)))
        missing_explicit = explicit_ids - {item.record.id for _, item, _ in candidates}
        if missing_explicit:
            _fail(
                "label_feature_ids are missing or unnamed: "
                + ", ".join(sorted(missing_explicit))
                + "."
            )
        candidates.sort(key=lambda value: (-value[0], value[1].record.id))
        candidates = candidates[: int(self.request.options["label_limit"])]
        ink = self._ink_union()
        pen_id = self.pens["labels"]
        nib = PENS_BY_ID[pen_id].mark_width_mm
        floor = 8.0 * nib
        preferred = max(
            floor, min(float(self.context.plate["type_scale_mm"]["detail"]), 3.4)
        )
        occupied: list[BaseGeometry] = []
        placed = 0
        dropped = 0
        for _rank, item, text_value in candidates:
            maximum_width = self.context.field.width * 0.29
            natural = text_width_mm(text_value, cap_height_mm=preferred)
            cap = min(preferred, preferred * maximum_width / max(natural, 1e-9))
            if cap + 1e-9 < floor:
                dropped += 1
                self.omit("map_label_below_type_floor")
                continue
            width = text_width_mm(text_value, cap_height_mm=cap)
            chosen: tuple[float, float, Polygon] | None = None
            for centre_x, top_y in self._label_candidates(item):
                label_box = box(
                    centre_x - width / 2.0 - 0.45,
                    top_y - 0.45,
                    centre_x + width / 2.0 + 0.45,
                    top_y + cap + 0.45,
                )
                if not self.clip_geometry.covers(label_box):
                    continue
                if any(label_box.intersects(other) for other in occupied):
                    continue
                if not ink.is_empty and label_box.intersects(ink):
                    continue
                chosen = (centre_x, top_y, label_box)
                break
            if chosen is None:
                dropped += 1
                self.omit("map_label_no_clear_paper")
                continue
            centre_x, top_y, label_box = chosen
            strokes = text_strokes_fit(
                text_value,
                x_mm=centre_x,
                y_mm=top_y,
                preferred_cap_mm=cap,
                maximum_width_mm=maximum_width,
                pen_id=pen_id,
                anchor="middle",
                minimum_cap_mm=floor,
            )
            for stroke in strokes:
                self.add_line(
                    layer_id="map_labels",
                    label="Selected place labels",
                    pen_id=pen_id,
                    line=LineString(stroke),
                    source_ref=item.record.source_ref,
                    role="map-label",
                    importance=400.0 + _rank,
                    required=False,
                    removable_group="labels",
                    attributes={"data-copy": text_value},
                    deduplicate=False,
                )
            occupied.append(label_box)
            self.label_boxes.append(label_box)
            placed += 1
        self.selection["labels"] = {
            "candidate_count": len(candidates),
            "placed_count": placed,
            "dropped_count": dropped,
            "collision_policy": "clear-paper-only-no-white-ink",
            "label_box_overlap_count": 0,
        }

    def apply_complexity_budget(self) -> None:
        field_area = self.context.field.width * self.context.field.height
        maximum_fraction = float(self.request.options["max_coverage"])
        maximum = field_area * maximum_fraction
        total = sum(record.ink_mm2 for record in self.planned)
        required = sum(record.ink_mm2 for record in self.planned if record.required)
        if required > maximum + 1e-9:
            raise MapPlotterError(
                "Required hero geometry exceeds the selected plate's place-art "
                f"complexity budget ({required:.1f} mm2 required, {maximum:.1f} mm2 "
                "available). Choose a larger plate, a tighter extent, or a sparser source."
            )
        before_count = len(self.planned)
        before_ink = total
        removal_order = {
            "park_texture": 0,
            "building_texture": 1,
            "decorative": 2,
            "minor_roads": 3,
            "buildings": 4,
            "local_roads": 5,
            "labels": 6,
            "park": 7,
            "boundary": 8,
            "water_texture": 9,
            "context": 10,
            "transport": 11,
            "water": 12,
            "contours": 13,
        }
        removable = sorted(
            (record for record in self.planned if not record.required),
            key=lambda record: (
                removal_order.get(record.removable_group, 20),
                record.importance / max(record.ink_mm2, 1e-9),
                record.source_ref or "",
                record.layer_id,
            ),
        )
        removed_ids: set[int] = set()
        removed_groups: dict[str, int] = {}
        for record in removable:
            if total <= maximum + 1e-9:
                break
            removed_ids.add(id(record))
            total -= record.ink_mm2
            removed_groups[record.removable_group] = (
                removed_groups.get(record.removable_group, 0) + 1
            )
        if removed_ids:
            self.planned = [
                record for record in self.planned if id(record) not in removed_ids
            ]
            self.omit("final_complexity_budget", len(removed_ids))
        self.selection["complexity"] = {
            "measurement": "sum(serialized-planning-length-mm * physical-nib-mm)",
            "field_area_mm2": round(field_area, 6),
            "maximum_coverage": maximum_fraction,
            "maximum_ink_mm2": round(maximum, 6),
            "before_record_count": before_count,
            "before_ink_mm2_upper_bound": round(before_ink, 6),
            "after_record_count": len(self.planned),
            "after_ink_mm2_upper_bound": round(total, 6),
            "after_coverage_upper_bound": round(total / field_area, 9),
            "removed_record_count": len(removed_ids),
            "removed_groups": removed_groups,
        }

    def _optimised_records(self) -> list[PlannedRecord]:
        grouped: dict[tuple[str, str], list[PlannedRecord]] = {}
        for record in self.planned:
            grouped.setdefault((record.pen_id, record.layer_id), []).append(record)
        result: list[PlannedRecord] = []
        current = (self.context.field.left, self.context.field.top)
        for key in sorted(grouped, key=lambda value: (value[0], value[1])):
            remaining = sorted(
                grouped[key],
                key=lambda record: (
                    record.source_ref or "",
                    record.role,
                    _line_key(record.points),
                ),
            )
            while remaining:
                best_index = 0
                best_reverse = False
                best_key: tuple[float, str, str] | None = None
                for index, record in enumerate(remaining):
                    start_distance = math.dist(current, record.points[0])
                    end_distance = math.dist(current, record.points[-1])
                    reverse = (
                        end_distance + 1e-12 < start_distance
                        and record.points[0] != record.points[-1]
                    )
                    distance = end_distance if reverse else start_distance
                    candidate_key = (distance, record.source_ref or "", record.role)
                    if best_key is None or candidate_key < best_key:
                        best_index = index
                        best_reverse = reverse
                        best_key = candidate_key
                selected = remaining.pop(best_index)
                if best_reverse:
                    selected = copy.deepcopy(selected)
                    selected.points.reverse()
                result.append(selected)
                current = selected.points[-1]
        return result

    def artwork_layers(self) -> list[ArtworkLayer]:
        layers: dict[str, ArtworkLayer] = {}
        for sequence, record in enumerate(self._optimised_records(), start=1):
            layer = layers.get(record.layer_id)
            if layer is None:
                layer = ArtworkLayer(record.layer_id, record.label, record.pen_id)
                layers[record.layer_id] = layer
            elif layer.pen_id != record.pen_id:
                raise MapPlotterError(
                    f"Logical place-art layer {record.layer_id!r} resolved to two pens."
                )
            layer.add(
                record.points,
                source_ref=record.source_ref,
                role=record.role,
                sequence=sequence,
                attributes={
                    **record.attributes,
                    "data-importance": f"{record.importance:.3f}",
                    "data-required-hero": str(record.required).lower(),
                },
            )
        return list(layers.values())


def _nice_distance_metres(target_m: float) -> float:
    if not math.isfinite(target_m) or target_m <= 0:
        raise MapPlotterError("Cannot derive a scale bar for a degenerate map scale.")
    exponent = 10.0 ** math.floor(math.log10(target_m))
    normalized = target_m / exponent
    value = (
        1.0
        if normalized < 1.5
        else 2.0
        if normalized < 3.5
        else 5.0
        if normalized < 7.5
        else 10.0
    )
    return value * exponent


def _scale_label(distance_m: float) -> str:
    if distance_m >= 1_000.0:
        kilometres = distance_m / 1_000.0
        return f"{kilometres:g} KM"
    return f"{distance_m:g} M"


def _reference_layers(
    request: PlaceRequest,
    context: PlateContext,
    page_transform: PageTransform,
    pens: Mapping[str, str],
) -> list[ArtworkLayer]:
    show_scale = bool(request.options["show_scale_bar"])
    show_north = bool(request.options["show_north"])
    show_ticks = bool(request.options["show_index_ticks"])
    if not (show_scale or show_north or show_ticks):
        return []
    pen_id = pens["annotations"]
    pen = PENS_BY_ID[pen_id]
    layer = ArtworkLayer("place_reference", "Scale, north, and index furniture", pen_id)
    furniture = context.zones["furniture"]
    minimum = 3.0 * pen.mark_width_mm
    if show_scale:
        target_mm = min(furniture.width * (0.42 if not show_north else 0.31), 42.0)
        target_mm = max(target_mm, 12.0)
        ground_m = _nice_distance_metres(target_mm / page_transform.scale_mm_per_m)
        bar_mm = ground_m * page_transform.scale_mm_per_m
        while bar_mm > furniture.width * (0.55 if not show_north else 0.40):
            ground_m /= 2.0
            bar_mm = ground_m * page_transform.scale_mm_per_m
        x = furniture.left
        y = furniture.centre[1]
        tick = max(minimum, min(2.2, furniture.height * 0.38))
        layer.add([(x, y), (x + bar_mm, y)], role="scale-bar")
        layer.add([(x, y - tick / 2.0), (x, y + tick / 2.0)], role="scale-bar-tick")
        layer.add(
            [(x + bar_mm, y - tick / 2.0), (x + bar_mm, y + tick / 2.0)],
            role="scale-bar-tick",
        )
        cap = max(8.0 * pen.mark_width_mm, min(2.4, furniture.height * 0.46))
        add_text(
            layer,
            _scale_label(ground_m),
            x_mm=x + bar_mm + max(2.0, context.plate["gap_mm"] * 0.45),
            y_mm=furniture.centre[1] - cap / 2.0,
            preferred_cap_mm=cap,
            maximum_width_mm=max(10.0, furniture.width * 0.27),
            role="scale-label",
            attributes={"data-ground-distance-m": f"{ground_m:g}"},
        )
    if show_north:
        arrow_x = furniture.right - max(4.0, furniture.width * 0.075)
        arrow_bottom = furniture.bottom - 0.35
        arrow_top = max(furniture.top + 0.35, arrow_bottom - max(3.4, minimum * 2.3))
        head = max(minimum, min(1.8, (arrow_bottom - arrow_top) * 0.42))
        layer.add([(arrow_x, arrow_bottom), (arrow_x, arrow_top)], role="north-arrow")
        layer.add(
            [
                (arrow_x - head * 0.55, arrow_top + head),
                (arrow_x, arrow_top),
                (arrow_x + head * 0.55, arrow_top + head),
            ],
            role="north-arrow-head",
        )
        cap = max(8.0 * pen.mark_width_mm, min(2.2, furniture.height * 0.42))
        add_text(
            layer,
            "N",
            x_mm=arrow_x - head - 1.2,
            y_mm=furniture.centre[1] - cap / 2.0,
            preferred_cap_mm=cap,
            maximum_width_mm=cap * 1.8,
            anchor="middle",
            role="north-label",
        )
    if show_ticks:
        field_rect = context.field
        tick = max(minimum, min(1.8, float(context.plate["gap_mm"]) * 0.42))
        cap = max(8.0 * pen.mark_width_mm, 2.0)
        for index in range(1, 5):
            fraction = index / 5.0
            x = field_rect.left + field_rect.width * fraction
            y = field_rect.top + field_rect.height * fraction
            letter = chr(ord("A") + index - 1)
            layer.add(
                [(x, field_rect.top), (x, field_rect.top + tick)],
                role="coordinate-tick",
                attributes={"data-index-mark": letter},
            )
            layer.add(
                [(field_rect.left, y), (field_rect.left + tick, y)],
                role="coordinate-tick",
                attributes={"data-index-mark": str(index)},
            )
            add_text(
                layer,
                letter,
                x_mm=x,
                y_mm=field_rect.top + tick + 0.3,
                preferred_cap_mm=cap,
                maximum_width_mm=cap * 1.8,
                anchor="middle",
                role="map-index-label",
            )
            add_text(
                layer,
                str(index),
                x_mm=field_rect.left + tick + cap * 0.75,
                y_mm=y - cap / 2.0,
                preferred_cap_mm=cap,
                maximum_width_mm=cap * 1.8,
                anchor="middle",
                role="map-index-label",
            )
    return [layer]


def _title_fits(title: str, context: PlateContext) -> bool:
    title = plotter_copy(title)
    zone = context.zones["title"]
    preferred = float(context.plate["type_scale_mm"]["title"])
    minimum = float(context.plate["rules"]["min_cap_height_mm"]["title"])
    if text_width_mm(title, cap_height_mm=preferred) <= zone.width + 1e-9:
        return True
    words = title.split()
    return any(
        max(
            text_width_mm(" ".join(words[:split_index]), cap_height_mm=minimum),
            text_width_mm(" ".join(words[split_index:]), cap_height_mm=minimum),
        )
        <= zone.width + 1e-9
        for split_index in range(1, len(words))
    )


def _fits_line(text: str, *, cap_mm: float, width_mm: float) -> bool:
    return text_width_mm(plotter_copy(text), cap_height_mm=cap_mm) <= width_mm + 1e-9


def _wrap_copy(text: str, *, cap_mm: float, width_mm: float) -> list[str]:
    normalized = plotter_copy(text)
    if _fits_line(normalized, cap_mm=cap_mm, width_mm=width_mm):
        return [normalized]
    words = normalized.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and not _fits_line(candidate, cap_mm=cap_mm, width_mm=width_mm):
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
        if current and not _fits_line(
            " ".join(current), cap_mm=cap_mm, width_mm=width_mm
        ):
            raise MapPlotterError(
                f"Personalisation word {word!r} cannot fit the binding detail zone."
            )
    if current:
        lines.append(" ".join(current))
    return lines


def _coordinate_copy(projection: LocalAzimuthalEquidistant) -> str:
    latitude = projection.latitude_0
    longitude = projection.longitude_0
    return (
        f"{abs(latitude):.5f} {'N' if latitude >= 0 else 'S'} / "
        f"{abs(longitude):.5f} {'E' if longitude >= 0 else 'W'}"
    )


def _haversine_m(first: PointTuple, second: PointTuple) -> float:
    first_lon, first_lat = map(math.radians, first)
    second_lon, second_lat = map(math.radians, second)
    delta_lat = second_lat - first_lat
    delta_lon = ((second_lon - first_lon + math.pi) % (2.0 * math.pi)) - math.pi
    value = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(first_lat) * math.cos(second_lat) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def _milestone_distance_copy(records: Sequence[GeoRecord]) -> str | None:
    milestones = [record for record in records if record.kind == "milestone"]
    if len(milestones) < 2:
        return None
    milestones.sort(
        key=lambda record: (
            int(record.properties.get("sequence", 999))
            if str(record.properties.get("sequence", "")).isdigit()
            else 999,
            str(record.properties.get("date", "")),
            record.id,
        )
    )
    points: list[PointTuple] = []
    for record in milestones:
        point = (
            record.geometry
            if isinstance(record.geometry, Point)
            else record.geometry.representative_point()
        )
        points.append((point.x, point.y))
    distance_m = sum(
        _haversine_m(first, second)
        for first, second in zip(points, points[1:], strict=False)
    )
    if distance_m >= 100_000.0:
        return f"{distance_m / 1_000.0:,.0f} KM BETWEEN OUR PLACES"
    if distance_m >= 1_000.0:
        return f"{distance_m / 1_000.0:.1f} KM BETWEEN OUR PLACES"
    return f"{distance_m:.0f} M BETWEEN OUR PLACES"


def _personalised_copy(
    request: PlaceRequest,
    context: PlateContext,
    policy: PresetPolicy,
    projection: LocalAzimuthalEquidistant,
    page_transform: PageTransform,
    elevation_range: tuple[float | None, float | None],
    records: Sequence[GeoRecord],
) -> tuple[str, str, tuple[str, ...], dict[str, Any]]:
    values = request.personalisation
    title = str(values.get("place_name") or request.title)
    fallback_title: str | None = None
    if not _title_fits(title, context):
        fallback_title = title
        title = "PLACE PORTRAIT"
    subtitle_candidates: list[str] = []
    detail_candidates: list[str] = []
    if request.subtitle:
        subtitle_candidates.append(request.subtitle)
    if values.get("neighbourhood"):
        subtitle_candidates.append(str(values["neighbourhood"]))

    if policy.id == "campus-graduation-map":
        subtitle_candidates.extend(
            str(values[key]) for key in ("degree",) if values.get(key)
        )
        if values.get("graduate_name"):
            detail_candidates.append(str(values["graduate_name"]))
        if values.get("graduation_date"):
            detail_candidates.append(str(values["graduation_date"]))
        if values.get("thesis_title"):
            detail_candidates.append(f"THESIS / {values['thesis_title']}")
    elif policy.id == "our-places":
        names = values.get("recipient_names")
        if isinstance(names, list):
            subtitle_candidates.append(" / ".join(str(name) for name in names))
        elif names:
            subtitle_candidates.append(str(names))
        if values.get("date_range"):
            detail_candidates.append(str(values["date_range"]))
    else:
        names = values.get("recipient_names")
        if isinstance(names, list):
            detail_candidates.append(" / ".join(str(name) for name in names))
        elif names:
            detail_candidates.append(str(names))

    coordinate_value = values.get("coordinates")
    coordinates_default = policy.id in {
        "monochrome-street-portrait",
        "landmark-radius",
        "minimal-coordinates",
        "organic-map",
    }
    if isinstance(coordinate_value, str):
        detail_candidates.append(coordinate_value)
    elif coordinate_value is True or (coordinate_value is None and coordinates_default):
        detail_candidates.append(_coordinate_copy(projection))
    for key in ("date", "event", "address"):
        if values.get(key):
            detail_candidates.append(str(values[key]))
    if values.get("date_range") and policy.id != "our-places":
        detail_candidates.append(str(values["date_range"]))
    for key in ("dedication", "quotation"):
        if values.get(key):
            detail_candidates.append(str(values[key]))
    if values.get("distance"):
        detail_candidates.append(str(values["distance"]))
    elif values.get("show_distance"):
        distance_copy = _milestone_distance_copy(records)
        if distance_copy:
            detail_candidates.append(distance_copy)
    low, high = elevation_range
    if isinstance(values.get("elevation_range"), str):
        detail_candidates.append(str(values["elevation_range"]))
    elif policy.hero == "contours" and low is not None and high is not None:
        detail_candidates.append(f"ELEVATION {low:g}-{high:g} M")
    if values.get("map_scale"):
        detail_candidates.append(str(values["map_scale"]))
    elif policy.id in {"urban-blueprint", "topographic-place-portrait"}:
        detail_candidates.append(
            f"APPROX SCALE 1:{round(page_transform.scale_denominator):,}"
        )

    subtitle = (
        subtitle_candidates.pop(0) if subtitle_candidates else policy.label.upper()
    )
    detail_candidates = [*subtitle_candidates, *detail_candidates]
    subtitle_zone = context.zones["subtitle"]
    subtitle_floor = float(context.plate["rules"]["min_cap_height_mm"]["subtitle"])
    if not _fits_line(subtitle, cap_mm=subtitle_floor, width_mm=subtitle_zone.width):
        detail_candidates.insert(0, subtitle)
        subtitle = "CARTOGRAPHIC PLACE PORTRAIT"
    if fallback_title is not None:
        detail_candidates.insert(0, fallback_title)

    detail_zone = context.zones["detail"]
    detail_cap = float(context.plate["type_scale_mm"]["detail"])
    wrapped: list[str] = []
    for candidate in dict.fromkeys(
        plotter_copy(item) for item in detail_candidates if item
    ):
        wrapped.extend(
            _wrap_copy(candidate, cap_mm=detail_cap, width_mm=detail_zone.width)
        )
    while len(wrapped) > 3:
        merged = False
        for index in range(len(wrapped) - 1):
            joined = f"{wrapped[index]} / {wrapped[index + 1]}"
            if _fits_line(joined, cap_mm=detail_cap, width_mm=detail_zone.width):
                wrapped[index : index + 2] = [joined]
                merged = True
                break
        if not merged:
            raise MapPlotterError(
                "Personalisation needs more than the binding three detail lines. "
                "Shorten the dedication, omit a field, or choose a larger format."
            )
    return (
        title,
        subtitle,
        tuple(wrapped),
        {
            "title_strategy": "fallback-to-detail"
            if fallback_title
            else "binding-title-zone",
            "subtitle_strategy": "single-binding-subtitle-zone",
            "detail_line_count": len(wrapped),
            "detail_lines": wrapped,
            "personalisation_fields": sorted(values),
        },
    )


def _preset_source_gate(
    request: PlaceRequest, policy: PresetPolicy, records: Sequence[GeoRecord]
) -> dict[str, Any]:
    present = {record.kind for record in records}
    category_present = {
        "road": bool(present & ROAD_KINDS),
        "water": bool(present & WATER_KINDS),
        "rail": bool(present & RAIL_KINDS),
        "park": "park_area" in present,
        "boundary": "boundary" in present,
        "contour": "contour" in present,
        "campus": bool(present & {"campus_boundary", "campus_building"}),
        "milestone": "milestone" in present,
        "focal": bool(
            present & {"landmark", "landmark_outline", "building", "campus_building"}
        ),
    }
    if policy.require_any and not any(
        category_present.get(name, False) for name in policy.require_any
    ):
        _fail(
            f"preset {policy.id!r} needs at least one of: "
            + ", ".join(policy.require_any)
            + "."
        )
    if policy.id == "topographic-place-portrait" and not category_present["contour"]:
        _fail("topographic-place-portrait requires supplied real contour geometry.")
    if policy.id == "campus-graduation-map" and not category_present["campus"]:
        _fail("campus-graduation-map requires a campus boundary or campus buildings.")
    if policy.id == "river-and-road" and not category_present["water"]:
        _fail(
            "river-and-road requires a supplied waterway, water surface, or coastline."
        )
    if policy.id == "our-places":
        milestone_count = sum(record.kind == "milestone" for record in records)
        if not 2 <= milestone_count <= 5:
            _fail("our-places requires two to five milestone features.")
    return category_present


def _default_simplify_mm(request: PlaceRequest) -> float:
    if "simplify_mm" in request.options:
        return float(request.options["simplify_mm"])
    return {"sparse": 0.11, "medium": 0.065, "dense": 0.04}[
        str(request.options["detail"])
    ]


def _focal_geo_record(
    request: PlaceRequest, records: Sequence[GeoRecord]
) -> GeoRecord | None:
    focal_id = request.options.get("focal_feature_id")
    if focal_id is not None:
        return next((record for record in records if record.id == focal_id), None)
    candidates = [
        record
        for record in records
        if record.kind in {"landmark", "landmark_outline"}
        and (
            record.properties.get("focal") is True
            or request.preset == "landmark-radius"
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def build_place_artwork(
    request_value: PlaceRequest | Mapping[str, Any],
    *,
    base_dir: Path | None = None,
) -> PlateArtwork:
    request = (
        request_value
        if isinstance(request_value, PlaceRequest)
        else validate_place_request(dict(request_value), base_dir=base_dir)
    )
    context = context_for(request.format_id)
    policy = PRESETS[request.preset]
    records, source_diagnostics = _extract_records(request)
    source_gate = _preset_source_gate(request, policy, records)
    projection = _projection_for(request, records)
    extent_bounds = _projected_extent_bounds(request, records, projection)

    focal_record = _focal_geo_record(request, records)
    if request.preset == "landmark-radius":
        if focal_record is None:
            _fail(
                "landmark-radius needs one focal landmark or options.focal_feature_id."
            )
        focal_geometry = projection.project_geometry(focal_record.geometry)
        focal_point = focal_geometry.representative_point()
        width = extent_bounds[2] - extent_bounds[0]
        height = extent_bounds[3] - extent_bounds[1]
        extent_bounds = (
            focal_point.x - width / 2.0,
            focal_point.y - height / 2.0,
            focal_point.x + width / 2.0,
            focal_point.y + height / 2.0,
        )
    adjusted_bounds = _adjust_bounds_to_field(
        extent_bounds, context.field, str(request.extent["fit"])
    )
    page_transform = PageTransform(adjusted_bounds, context.field)
    full_clip = _clip_geometry(
        context, str(request.options["frame_shape"]), int(request.options["seed"])
    )
    data_clip = full_clip
    annotation_inset_mm = 0.0
    if request.options["show_index_ticks"]:
        label_pen = _semantic_pens(
            context, policy, request.options.get("pen_roles", {})
        )["annotations"]
        label_floor = 8.0 * PENS_BY_ID[label_pen].mark_width_mm
        annotation_inset_mm = max(
            float(context.plate["gap_mm"]) * 0.8, label_floor + 0.45
        )
        inset_rect = context.field.inset(annotation_inset_mm)
        data_clip = full_clip.intersection(
            box(inset_rect.left, inset_rect.top, inset_rect.right, inset_rect.bottom)
        )
    projected, projection_diagnostics = _project_records(
        records,
        projection,
        page_transform,
        data_clip,
        float(request.options["clip_bleed_mm"]),
    )
    simplify_mm = _default_simplify_mm(request)
    display_records, simplify_diagnostics = _simplify_projected_records(
        projected,
        tolerance_mm=simplify_mm,
        clip_geometry=data_clip,
    )
    visible_source_gate = _preset_source_gate(
        request, policy, [item.record for item in display_records]
    )
    composer = _Composer(
        request,
        context,
        policy,
        display_records,
        data_clip,
        page_transform,
    )
    composer.add_water()
    composer.add_parks()
    elevation_range = composer.add_contours()
    composer.add_rail_and_boundaries()
    composer.add_roads()
    composer.add_campus()
    composer.add_buildings()
    composer.add_focal_and_route()
    composer.add_milestones()
    # The decorative crop follows the full requested composition boundary, not
    # the smaller blueprint annotation inset.
    composer.clip_geometry = full_clip
    composer.add_crop_border()
    composer.clip_geometry = data_clip
    composer.add_labels()
    composer.apply_complexity_budget()
    layers = composer.artwork_layers()
    layers.extend(_reference_layers(request, context, page_transform, composer.pens))
    title, subtitle, details, personalisation_layout = _personalised_copy(
        request,
        context,
        policy,
        projection,
        page_transform,
        elevation_range,
        records,
    )
    source_records: list[dict[str, Any]] = []
    for source in request.sources:
        item = copy.deepcopy(source)
        item["resolved_geometry_sha256"] = source_diagnostics["source_geometry_sha256"][
            str(source["id"])
        ]
        source_records.append(item)
    if request.geojson_path is not None:
        source_records.append(
            {
                "id": "pinned-geojson-file",
                "kind": "compiled-geometry-container",
                "path": str(request.geojson_path),
                "sha256": request.geojson_file_sha256,
                "license": "inherits per-feature source records",
                "attribution": "see source records",
            }
        )
    rendering_metadata = {
        "place_art_schema_version": 1,
        "place_art_preset_id": policy.id,
        "place_art_preset_label": policy.label,
        "place_art_concept": policy.concept,
        "place_art_projection": {
            "method": "local-azimuthal-equidistant-sphere",
            "earth_radius_m": EARTH_RADIUS_M,
            "latitude_0": projection.latitude_0,
            "longitude_0": projection.longitude_0,
            "antimeridian_wrapping": True,
            "projection_boundary_guard_degrees": 145.0,
        },
        "place_art_extent": {
            "request": copy.deepcopy(request.extent),
            "projected_source_bounds_m": [round(value, 6) for value in extent_bounds],
            "projected_display_bounds_m": [
                round(value, 6) for value in adjusted_bounds
            ],
            "fit": request.extent["fit"],
            "scale_mm_per_m": round(page_transform.scale_mm_per_m, 12),
            "approximate_scale_denominator": round(page_transform.scale_denominator, 3),
            "clip_shape": request.options["frame_shape"],
            "clip_bleed_mm": request.options["clip_bleed_mm"],
            "blueprint_annotation_inset_mm": round(annotation_inset_mm, 6),
            "all_emitted_geometry_clipped_to_field": True,
        },
        "place_art_geometry": {
            **source_diagnostics,
            **projection_diagnostics,
            **simplify_diagnostics,
            "simplification_tolerance_mm": simplify_mm,
            "display_geometry_sha256": _stable_geometry_sha256(display_records),
            "source_and_display_geometry_retained_separately": True,
            "factual_geometry_perturbed": False,
            "decorative_seed": request.options["seed"],
            "solid_fills_used": False,
        },
        "place_art_source_gate": {
            "supplied": source_gate,
            "visible_after_projection_and_clip": visible_source_gate,
        },
        "place_art_selection": copy.deepcopy(composer.selection),
        "place_art_omissions": dict(sorted(composer.omissions.items())),
        "place_art_personalisation": personalisation_layout,
        "place_art_label_boxes_mm": [
            {
                "x": round(label_box.bounds[0], 3),
                "y": round(label_box.bounds[1], 3),
                "width": round(label_box.bounds[2] - label_box.bounds[0], 3),
                "height": round(label_box.bounds[3] - label_box.bounds[1], 3),
            }
            for label_box in composer.label_boxes
        ],
        "place_art_claims": {
            "relationship_connections_are_routes": False,
            "terrain_invented": False,
            "white_ink_label_halos": False,
            "logos_or_crests_used": False,
            "landmark_geometry_origin": (
                "not-applicable"
                if focal_record is None
                else "supplied-landmark-outline"
                if focal_record.kind == "landmark_outline"
                else "supplied-building-footprint"
                if focal_record.kind in {"building", "campus_building"}
                else "supplied-landmark-point"
            ),
        },
    }
    catalog_record = {
        "schema_version": 1,
        "id": request.id,
        "preset": request.preset,
        "format_id": request.format_id,
        "title": request.title,
        "subtitle": request.subtitle,
        "personalisation": copy.deepcopy(request.personalisation),
        "options": copy.deepcopy(request.options),
        "sources": source_records,
        "request_path": str(request.request_path) if request.request_path else None,
    }
    return PlateArtwork(
        subject_id=request.id,
        domain="place-art",
        subject_kind="map",
        title=title,
        subtitle=subtitle,
        details=details,
        credit_line=" | ".join(request.credit_lines),
        scale_status=(
            "LOCAL AZIMUTHAL EQUIDISTANT / "
            f"APPROX 1:{round(page_transform.scale_denominator):,}"
        ),
        evidence_status="source-derived-selective-cartographic-composition",
        rights_status=request.rights_status,
        sources=tuple(source_records),
        context=context,
        layers=layers,
        artifact_kind="place-cartographic-artwork",
        rendering_preset=f"place-art-{policy.id}-v1",
        format_subject_policy="map",
        source_provider=" / ".join(str(source["label"]) for source in request.sources),
        source_license=" / ".join(
            dict.fromkeys(str(source["license"]) for source in request.sources)
        ),
        data_snapshot=request.data_snapshot,
        notes=(
            *request.notes,
            "Cartographic selection is deliberate and paper-scale dependent; the "
            "manifest preserves exact source and display geometry digests.",
            "Decorative perturbation is restricted to generated texture and crop ornament.",
        ),
        catalog_record=catalog_record,
        rendering_metadata=rendering_metadata,
    )


def build_place_artwork_from_file(path: Path) -> PlateArtwork:
    return build_place_artwork(load_place_request(path))
