#!/usr/bin/env python3
"""Derive selected hiking contours from reviewed, range-readable DEM rasters.

This companion to ``derive_hiking_terrain_context.py`` handles non-GB source
rasters.  It reads only a plate window (and downsamples before transfer when
the source is a COG), records a canonical hash of that extracted window, and
writes sparse elevation-valued contours into ``hike-context-v3.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
import rasterio  # type: ignore[import-untyped]
from pyproj import Transformer  # type: ignore[import-not-found]
from rasterio.enums import Resampling  # type: ignore[import-untyped]
from rasterio.features import shapes  # type: ignore[import-untyped]
from rasterio.windows import Window, from_bounds  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point, Polygon, shape as shapely_shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from city_map_plotter.hike_plates import _layout, _route_rect
from city_map_plotter.niche_common import context_for

from derive_hiking_terrain_context import (
    _canonical_sha256,
    _contour_paths,
    _load_object,
    _overlay,
    _record,
    _route_geometry,
    densified_bbox_polygon,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-v1.json"
DEFAULT_BUNDLE = ROOT / "src" / "city_map_plotter" / "data" / "hike-context-v3.json"
DERIVATION_VERSION = 7

RELIEF_ALGORITHM_ID = "dem-gradient-fall-line-v1"
FALL_LINE_PEN_NIB_MM = 0.25
FALL_LINE_GAUSSIAN_SIGMA_MM = 0.60
FALL_LINE_LATTICE_MM = 4.2
FALL_LINE_LATTICE_OFFSET_MM = 2.1
FALL_LINE_TRACE_STEP_MM = 0.45
FALL_LINE_MINIMUM_MM = 3.0
FALL_LINE_MAXIMUM_MM = 7.0
FALL_LINE_PREFERRED_MAXIMUM_MM = 5.2
FALL_LINE_SPACING_MM = 1.2
FALL_LINE_CLUSTER_DISTANCE_MM = 6.0
FALL_LINE_CLUSTER_MINIMUM_STROKES = 3
FALL_LINE_CLUSTER_MINIMUM_LENGTH_MM = 9.0
FALL_LINE_MAXIMUM_STROKES = 140
FALL_LINE_MAXIMUM_TOTAL_MM = 650.0
CONTOUR_MINIMUM_PAGE_MM = 8.0
CONTOUR_MAXIMUM_PATHS = 8
CONTOUR_MAXIMUM_TOTAL_MM = 400.0


SWISS_ATTRIBUTION = (
    "Bundesamt für Landestopografie swisstopo; Tarquini S., I. Isola, "
    "M. Favalli, A. Battistini, G. Dotta (2023). TINITALY, a digital "
    "elevation model of Italy with a 10 meters cell size (Version 1.1). "
    "Istituto Nazionale di Geofisica e Vulcanologia (INGV), "
    "doi:10.13127/tinitaly/1.1; DGM Österreich, geoland.at; DGM1, "
    "Bayerische Vermessungsverwaltung; DGM1, Baden-Württemberg: LGL; "
    "RGEAlti, Institut National de l’information géographique et forestière"
)


@dataclass(frozen=True)
class ElevationMaskConfig:
    """Source-threshold polygon selection and its disclosed plot treatment."""

    threshold_m: int
    simplify_m: float
    minimum_area_m2: float
    maximum_route_distance_m: float | None
    maximum_components: int
    hachure_spacing_mm: float = 3.5
    hachure_along_pitch_mm: float = 5.6
    hachure_segment_length_mm: float = 3.0
    hachure_angle_deg: float = 14.0
    hachure_inset_mm: float = 0.45
    hachure_max_strokes_per_area: int = 48


@dataclass(frozen=True)
class RasterTerrainConfig:
    source_id: str
    source_url: str
    publisher: str
    license: str
    attribution: str
    product_url: str
    source_release: str
    source_resolution_m: float
    vertical_datum: str
    target_resolution_m: float
    levels_m: tuple[int, ...]
    caps: tuple[int, ...]
    simplify_m: float
    minimum_length_m: float
    credit_line: str
    minimum_separation_m: float = 0.0
    maximum_route_distance_m: float | None = None
    maximum_path_span_m: float | None = None
    maximum_path_length_m: float | None = None
    finish_clearance_m: float = 0.0
    prefer_broad_contours: bool = False
    anchor_feature_ids: tuple[str, ...] = ()
    anchor_cluster_radius_m: float = 0.0
    anchor_cluster_minimum_paths: int = 2
    anchor_cluster_maximum_paths: int = 4
    elevation_mask: ElevationMaskConfig | None = None
    fall_line_seed_minimum_elevation_m: float | None = None
    fall_line_seed_slope_policy: str = "all-at-or-above-4deg-v1"


@dataclass(frozen=True)
class PageRect:
    """Physical route rectangle used by the A-series renderer, in millimetres."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


@dataclass(frozen=True)
class GeographicPageTransform:
    """Exact inverse pair for ``hike_plates._geographic_transform``."""

    rect: PageRect
    format_id: str
    binding_sha256: str
    west: float
    south: float
    east: float
    north: float
    cosine: float
    centre_x: float
    centre_y: float
    rotation_cosine: float
    rotation_sine: float
    minimum_x: float
    maximum_y: float
    scale: float
    offset_x: float
    offset_y: float

    def geographic_to_page(self, point: tuple[float, float]) -> tuple[float, float]:
        longitude, latitude = point
        source_x = longitude * self.cosine
        delta_x = source_x - self.centre_x
        delta_y = latitude - self.centre_y
        projected_x = (
            self.centre_x
            + delta_x * self.rotation_cosine
            - delta_y * self.rotation_sine
        )
        projected_y = (
            self.centre_y
            + delta_x * self.rotation_sine
            + delta_y * self.rotation_cosine
        )
        return (
            self.offset_x + (projected_x - self.minimum_x) * self.scale,
            self.offset_y + (self.maximum_y - projected_y) * self.scale,
        )

    def page_to_geographic(self, point: tuple[float, float]) -> tuple[float, float]:
        page_x, page_y = point
        projected_x = self.minimum_x + (page_x - self.offset_x) / self.scale
        projected_y = self.maximum_y - (page_y - self.offset_y) / self.scale
        rotated_x = projected_x - self.centre_x
        rotated_y = projected_y - self.centre_y
        source_x = (
            self.centre_x
            + rotated_x * self.rotation_cosine
            + rotated_y * self.rotation_sine
        )
        latitude = (
            self.centre_y
            - rotated_x * self.rotation_sine
            + rotated_y * self.rotation_cosine
        )
        return (source_x / self.cosine, latitude)


SWISS_CONFIG = RasterTerrainConfig(
    source_id="swisstopo-swissaltiregio-2026-08-03",
    source_url=(
        "https://data.geo.admin.ch/ch.swisstopo.swissaltiregio/"
        "swissaltiregio/swissaltiregio_2056_5728.tif"
    ),
    publisher="Federal Office of Topography swisstopo and named source partners",
    license="Swiss federal free geodata terms; composite source conditions",
    attribution=SWISS_ATTRIBUTION,
    product_url="https://www.swisstopo.admin.ch/en/height-model-swissaltiregio",
    source_release="2025-or-later live COG retrieved 2026-08-03",
    source_resolution_m=10.0,
    vertical_datum="LN02 in Switzerland; original source datums abroad",
    target_resolution_m=50.0,
    levels_m=(1_000, 1_500, 2_000, 2_500, 3_000, 3_500, 4_000),
    caps=(5, 7, 9, 9, 8, 5, 2),
    simplify_m=190.0,
    minimum_length_m=4_500.0,
    credit_line=(
        "RELIEF © SWISSTOPO | © OSM CONTRIBUTORS / OPENSTREETMAP.ORG/COPYRIGHT"
    ),
)


VIA_ALPINA_CONFIG = replace(
    SWISS_CONFIG,
    levels_m=(2_000, 3_000),
    caps=(4, 4),
    simplify_m=350.0,
    minimum_length_m=6_000.0,
    minimum_separation_m=0.0,
    maximum_route_distance_m=25_000.0,
    maximum_path_span_m=90_000.0,
    maximum_path_length_m=350_000.0,
    elevation_mask=None,
    fall_line_seed_minimum_elevation_m=1_500.0,
)


ALPINE_PASSES_CONFIG = replace(
    SWISS_CONFIG,
    levels_m=(2_000, 3_000),
    caps=(4, 4),
    simplify_m=380.0,
    minimum_length_m=6_000.0,
    minimum_separation_m=0.0,
    maximum_route_distance_m=25_000.0,
    maximum_path_span_m=100_000.0,
    maximum_path_length_m=500_000.0,
    elevation_mask=None,
    fall_line_seed_minimum_elevation_m=1_800.0,
)


CONFIGS = {
    "RTE-CH-VA1-01": VIA_ALPINA_CONFIG,
    "RTE-CH-AP6-01": ALPINE_PASSES_CONFIG,
    "RTE-IS-LAUG-01": RasterTerrainConfig(
        source_id="natt-islandsdem-v1-20m",
        source_url=(
            "https://ftp.natt.is/gisdata/raster/IslandsDEMv1.0_20x20m_isn93_zmasl.tif"
        ),
        publisher="Náttúrufræðistofnun",
        license="CC BY 4.0",
        attribution="ÍslandsDEM v1.0 by Náttúrufræðistofnun; CC BY 4.0",
        product_url=(
            "https://gatt.natt.is/geonetwork/srv/api/records/"
            "e6712430-a63c-4ae5-9158-c89d16da6361"
        ),
        source_release="ÍslandsDEM v1.0",
        source_resolution_m=20.0,
        vertical_datum="orthometric elevation corrected with IceGeoid",
        target_resolution_m=40.0,
        levels_m=(600, 1_000),
        caps=(4, 4),
        simplify_m=170.0,
        minimum_length_m=2_000.0,
        credit_line=(
            "ÍSLANDSDEM V1.0 / CC BY 4.0 | "
            "© OSM CONTRIBUTORS / OPENSTREETMAP.ORG/COPYRIGHT"
        ),
        minimum_separation_m=0.0,
        maximum_route_distance_m=12_000.0,
        maximum_path_span_m=20_000.0,
        maximum_path_length_m=110_000.0,
        finish_clearance_m=1_500.0,
        elevation_mask=None,
        fall_line_seed_minimum_elevation_m=800.0,
    ),
}


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"derive_hiking_raster_terrain_context: {message}")


def _derivation_id(subject_id: str) -> str:
    return f"raster-dem-{subject_id.casefold()}-relief-v{DERIVATION_VERSION}"


def _window_digest(
    values: np.ndarray,
    *,
    crs: str,
    transform_values: tuple[float, ...],
) -> str:
    digest = hashlib.sha256()
    header = json.dumps(
        {
            "crs": crs,
            "shape": list(values.shape),
            "transform": [round(value, 12) for value in transform_values],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest.update(header)
    canonical = np.nan_to_num(
        values.astype(np.float32, copy=False),
        nan=-3.4e38,
        posinf=3.4e38,
        neginf=-3.4e38,
    ).astype("<f4", copy=False)
    digest.update(canonical.tobytes(order="C"))
    return digest.hexdigest()


def _intersect_window(window: Window, width: int, height: int) -> Window:
    rounded = window.round_offsets().round_lengths()
    intersection = rounded.intersection(Window(0, 0, width, height))
    if intersection.width <= 1 or intersection.height <= 1:
        _fail("plate extent does not intersect the source raster")
    return intersection


def _read_window(
    config: RasterTerrainConfig,
    *,
    extent: tuple[float, float, float, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, tuple[float, ...], float]:
    environment = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
        "GDAL_HTTP_MULTIRANGE": "YES",
    }
    with rasterio.Env(**environment), rasterio.open(config.source_url) as dataset:
        if dataset.count != 1 or dataset.crs is None:
            _fail("source DEM must contain one georeferenced band")
        if (
            abs(float(dataset.transform.b)) > 1e-9
            or abs(float(dataset.transform.d)) > 1e-9
        ):
            _fail("rotated source rasters are not supported")
        transformer = Transformer.from_crs(
            "EPSG:4326", dataset.crs.to_string(), always_xy=True
        )
        crop = transform(
            transformer.transform,
            densified_bbox_polygon(*extent),
        )
        window = _intersect_window(
            from_bounds(*crop.bounds, transform=dataset.transform),
            dataset.width,
            dataset.height,
        )
        native_x = abs(float(dataset.transform.a))
        native_y = abs(float(dataset.transform.e))
        output_width = max(
            2,
            int(math.ceil(float(window.width) * native_x / config.target_resolution_m)),
        )
        output_height = max(
            2,
            int(
                math.ceil(float(window.height) * native_y / config.target_resolution_m)
            ),
        )
        masked = dataset.read(
            1,
            window=window,
            out_shape=(output_height, output_width),
            resampling=Resampling.bilinear,
            masked=True,
        )
        values = np.asarray(masked.filled(np.nan), dtype=np.float32)
        values[(values < -500.0) | (values > 9_000.0)] = np.nan
        window_transform = dataset.window_transform(window)
        output_transform = window_transform * rasterio.Affine.scale(
            float(window.width) / output_width,
            float(window.height) / output_height,
        )
        x_coordinates = float(output_transform.c) + (
            np.arange(output_width, dtype=np.float64) + 0.5
        ) * float(output_transform.a)
        y_coordinates = float(output_transform.f) + (
            np.arange(output_height, dtype=np.float64) + 0.5
        ) * float(output_transform.e)
        crs = dataset.crs.to_string()
        valid_fraction = float(np.isfinite(values).sum()) / float(values.size)
        return (
            values,
            x_coordinates,
            y_coordinates,
            crs,
            tuple(float(value) for value in output_transform)[:6],
            valid_fraction,
        )


def _page_transform_for_record(record: dict[str, Any]) -> GeographicPageTransform:
    """Reproduce the route-space transform used by the hiking plate renderer."""

    format_id = str(record["composition"]["format_id"])
    binding_context = context_for(format_id)
    has_profile = all(
        len(point) == 3
        for segment in record["route"]["segments"]
        for point in segment["points"]
    )
    renderer_map_rect, _ = _layout(
        binding_context.field,
        has_profile=has_profile,
    )
    renderer_route_rect = _route_rect(renderer_map_rect)
    route_rect = PageRect(
        renderer_route_rect.x,
        renderer_route_rect.y,
        renderer_route_rect.width,
        renderer_route_rect.height,
    )

    context = record["context"]
    raw_extent = context.get("map_extent", context["extent"])
    west, south, east, north = (float(value) for value in raw_extent)
    mean_latitude = (south + north) / 2.0
    cosine = max(math.cos(math.radians(mean_latitude)), 1e-6)
    centre_x = ((west + east) / 2.0) * cosine
    centre_y = (south + north) / 2.0
    angle = math.radians(float(record["context"].get("rotation_deg", 0.0)))
    rotation_cosine = math.cos(angle)
    rotation_sine = math.sin(angle)

    def projected(longitude: float, latitude: float) -> tuple[float, float]:
        source_x = longitude * cosine
        delta_x = source_x - centre_x
        delta_y = latitude - centre_y
        return (
            centre_x + delta_x * rotation_cosine - delta_y * rotation_sine,
            centre_y + delta_x * rotation_sine + delta_y * rotation_cosine,
        )

    bounds = [
        projected(west, south),
        projected(west, north),
        projected(east, south),
        projected(east, north),
    ]
    minimum_x = min(point[0] for point in bounds)
    maximum_x = max(point[0] for point in bounds)
    minimum_y = min(point[1] for point in bounds)
    maximum_y = max(point[1] for point in bounds)
    span_x = max(maximum_x - minimum_x, 1e-12)
    span_y = max(maximum_y - minimum_y, 1e-12)
    scale = min(route_rect.width / span_x, route_rect.height / span_y)
    used_width = span_x * scale
    used_height = span_y * scale
    offset_x = route_rect.x + (route_rect.width - used_width) / 2.0
    offset_y = route_rect.y + (route_rect.height - used_height) / 2.0
    binding_sha256 = _canonical_sha256(
        {
            "format_id": format_id,
            "has_profile": has_profile,
            "route_rect_mm": {
                "x": route_rect.x,
                "y": route_rect.y,
                "width": route_rect.width,
                "height": route_rect.height,
            },
            "extent": [west, south, east, north],
            "rotation_deg": float(context.get("rotation_deg", 0.0)),
            "map_extent_binding": context.get("map_extent_binding"),
        }
    )
    return GeographicPageTransform(
        rect=route_rect,
        format_id=format_id,
        binding_sha256=binding_sha256,
        west=west,
        south=south,
        east=east,
        north=north,
        cosine=cosine,
        centre_x=centre_x,
        centre_y=centre_y,
        rotation_cosine=rotation_cosine,
        rotation_sine=rotation_sine,
        minimum_x=minimum_x,
        maximum_y=maximum_y,
        scale=scale,
        offset_x=offset_x,
        offset_y=offset_y,
    )


def _polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(second[0] - first[0], second[1] - first[1])
        for first, second in zip(points, points[1:])
    )


def _convolve_same_axis(
    values: np.ndarray,
    kernel: np.ndarray,
    *,
    axis: int,
) -> np.ndarray:
    """Apply a one-dimensional kernel without scipy or edge extrapolation."""

    output = np.empty_like(values, dtype=np.float32)
    source = values if axis == 1 else values.T
    target = output if axis == 1 else output.T
    start = len(kernel) // 2
    for index in range(source.shape[0]):
        full = np.convolve(source[index], kernel, mode="full")
        target[index] = full[start : start + source.shape[1]]
    return output


def _gaussian_kernel(sigma_pixels: float) -> np.ndarray:
    sigma = max(float(sigma_pixels), 0.01)
    radius = max(1, int(math.ceil(3.0 * sigma)))
    offsets = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-0.5 * np.square(offsets / sigma)).astype(np.float32)
    kernel /= float(kernel.sum())
    return kernel


def _nan_gaussian_separable(
    values: np.ndarray,
    *,
    sigma_x_pixels: float,
    sigma_y_pixels: float,
) -> np.ndarray:
    """NaN-aware custom separable Gaussian used by the frozen derivation."""

    finite = np.isfinite(values)
    numerator = np.where(finite, values, 0.0).astype(np.float32, copy=False)
    weights = finite.astype(np.float32)
    for axis, sigma in ((1, sigma_x_pixels), (0, sigma_y_pixels)):
        kernel = _gaussian_kernel(sigma)
        numerator = _convolve_same_axis(numerator, kernel, axis=axis)
        weights = _convolve_same_axis(weights, kernel, axis=axis)
    result = np.full(values.shape, np.nan, dtype=np.float32)
    sufficiently_supported = weights >= 0.35
    result[sufficiently_supported] = (
        numerator[sufficiently_supported] / weights[sufficiently_supported]
    )
    return result


def _central_difference_gradients(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return dz/dx and dz/dy using central differences in the source CRS."""

    gradient_x = np.full(values.shape, np.nan, dtype=np.float32)
    gradient_y = np.full(values.shape, np.nan, dtype=np.float32)
    x_denominator = x_coordinates[2:] - x_coordinates[:-2]
    y_denominator = y_coordinates[2:] - y_coordinates[:-2]
    gradient_x[:, 1:-1] = (values[:, 2:] - values[:, :-2]) / x_denominator[
        np.newaxis, :
    ]
    gradient_y[1:-1, :] = (values[2:, :] - values[:-2, :]) / y_denominator[
        :, np.newaxis
    ]
    valid = np.isfinite(gradient_x) & np.isfinite(gradient_y)
    gradient_x[~valid] = np.nan
    gradient_y[~valid] = np.nan
    return gradient_x, gradient_y


def _bilinear_sample(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    x: float,
    y: float,
) -> float | None:
    if (
        x < float(x_coordinates[0])
        or x > float(x_coordinates[-1])
        or y < float(y_coordinates[0])
        or y > float(y_coordinates[-1])
    ):
        return None
    column = int(np.searchsorted(x_coordinates, x, side="right") - 1)
    row = int(np.searchsorted(y_coordinates, y, side="right") - 1)
    column = min(max(column, 0), len(x_coordinates) - 2)
    row = min(max(row, 0), len(y_coordinates) - 2)
    cell = values[row : row + 2, column : column + 2]
    if not np.isfinite(cell).all():
        return None
    x_fraction = (x - float(x_coordinates[column])) / float(
        x_coordinates[column + 1] - x_coordinates[column]
    )
    y_fraction = (y - float(y_coordinates[row])) / float(
        y_coordinates[row + 1] - y_coordinates[row]
    )
    top = float(cell[0, 0]) * (1.0 - x_fraction) + float(cell[0, 1]) * x_fraction
    bottom = float(cell[1, 0]) * (1.0 - x_fraction) + float(cell[1, 1]) * x_fraction
    return top * (1.0 - y_fraction) + bottom * y_fraction


def _page_gradient_sample(
    page_point: tuple[float, float],
    *,
    page_transform: GeographicPageTransform,
    forward: Transformer,
    inverse: Transformer,
    gradient_x: np.ndarray,
    gradient_y: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    direction_sign: float,
    probe_m: float,
) -> tuple[float, float, float, float, float] | None:
    longitude, latitude = page_transform.page_to_geographic(page_point)
    source_x, source_y = forward.transform(longitude, latitude)
    sampled_x = _bilinear_sample(
        gradient_x, x_coordinates, y_coordinates, source_x, source_y
    )
    sampled_y = _bilinear_sample(
        gradient_y, x_coordinates, y_coordinates, source_x, source_y
    )
    if sampled_x is None or sampled_y is None:
        return None
    magnitude = math.hypot(sampled_x, sampled_y)
    slope_deg = math.degrees(math.atan(magnitude))
    if magnitude <= 1e-12:
        return (slope_deg, 0.0, 0.0, source_x, source_y)
    downhill_x = -sampled_x / magnitude
    downhill_y = -sampled_y / magnitude
    probe_longitude, probe_latitude = inverse.transform(
        source_x + direction_sign * downhill_x * probe_m,
        source_y + direction_sign * downhill_y * probe_m,
    )
    probe_page = page_transform.geographic_to_page((probe_longitude, probe_latitude))
    page_dx = probe_page[0] - page_point[0]
    page_dy = probe_page[1] - page_point[1]
    page_magnitude = math.hypot(page_dx, page_dy)
    if page_magnitude <= 1e-12:
        return None
    return (
        slope_deg,
        page_dx / page_magnitude,
        page_dy / page_magnitude,
        source_x,
        source_y,
    )


def _trace_fall_line_branch(
    seed: tuple[float, float],
    *,
    target_mm: float,
    direction_sign: float,
    page_transform: GeographicPageTransform,
    forward: Transformer,
    inverse: Transformer,
    gradient_x: np.ndarray,
    gradient_y: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    probe_m: float,
    minimum_trace_slope_deg: float = 1.5,
) -> list[tuple[float, float]]:
    points = [seed]
    previous_direction: tuple[float, float] | None = None
    travelled = 0.0
    consecutive_low_slope = 0
    while travelled + 1e-9 < target_mm:
        sample = _page_gradient_sample(
            points[-1],
            page_transform=page_transform,
            forward=forward,
            inverse=inverse,
            gradient_x=gradient_x,
            gradient_y=gradient_y,
            x_coordinates=x_coordinates,
            y_coordinates=y_coordinates,
            direction_sign=direction_sign,
            probe_m=probe_m,
        )
        if sample is None:
            break
        step = min(FALL_LINE_TRACE_STEP_MM, target_mm - travelled)
        if sample[0] < minimum_trace_slope_deg:
            consecutive_low_slope += 1
            if consecutive_low_slope >= 2 or previous_direction is None:
                break
            direction = previous_direction
        else:
            consecutive_low_slope = 0
            direction = (sample[1], sample[2])
        if previous_direction is not None:
            previous_angle = math.atan2(previous_direction[1], previous_direction[0])
            direction_angle = math.atan2(direction[1], direction[0])
            delta = (direction_angle - previous_angle + math.pi) % (
                2.0 * math.pi
            ) - math.pi
            maximum_turn = math.radians(15.0 * step / FALL_LINE_TRACE_STEP_MM)
            if abs(delta) > maximum_turn:
                regularized_angle = previous_angle + math.copysign(maximum_turn, delta)
                direction = (
                    math.cos(regularized_angle),
                    math.sin(regularized_angle),
                )
        next_point = (
            points[-1][0] + direction[0] * step,
            points[-1][1] + direction[1] * step,
        )
        rect = page_transform.rect
        if not (
            rect.x <= next_point[0] <= rect.right
            and rect.y <= next_point[1] <= rect.bottom
        ):
            break
        next_sample = _page_gradient_sample(
            next_point,
            page_transform=page_transform,
            forward=forward,
            inverse=inverse,
            gradient_x=gradient_x,
            gradient_y=gradient_y,
            x_coordinates=x_coordinates,
            y_coordinates=y_coordinates,
            direction_sign=direction_sign,
            probe_m=probe_m,
        )
        if next_sample is None:
            break
        points.append(next_point)
        travelled += step
        previous_direction = direction
    return points


def _fall_line_clusters(
    selected: list[dict[str, Any]],
) -> list[list[int]]:
    remaining = set(range(len(selected)))
    clusters: list[list[int]] = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component = [seed]
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            neighbours = [
                candidate
                for candidate in sorted(remaining)
                if selected[current]["_page_geometry"].distance(
                    selected[candidate]["_page_geometry"]
                )
                <= FALL_LINE_CLUSTER_DISTANCE_MM
            ]
            for candidate in neighbours:
                remaining.remove(candidate)
                component.append(candidate)
                frontier.append(candidate)
        clusters.append(sorted(component))
    return clusters


def _derive_relief_strokes(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    *,
    record: dict[str, Any],
    config: RasterTerrainConfig,
    forward: Transformer,
    inverse: Transformer,
    window_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze factual DEM-gradient fall lines as geographic source geometry."""

    page_transform = _page_transform_for_record(record)
    if float(y_coordinates[0]) > float(y_coordinates[-1]):
        y_coordinates = y_coordinates[::-1].copy()
        values = values[::-1, :].copy()
    if float(x_coordinates[0]) > float(x_coordinates[-1]):
        x_coordinates = x_coordinates[::-1].copy()
        values = values[:, ::-1].copy()

    centre_source_x = float((x_coordinates[0] + x_coordinates[-1]) / 2.0)
    centre_source_y = float((y_coordinates[0] + y_coordinates[-1]) / 2.0)
    centre_geo = inverse.transform(centre_source_x, centre_source_y)
    centre_page = page_transform.geographic_to_page(centre_geo)
    source_dx = float(np.median(np.diff(x_coordinates)))
    source_dy = float(np.median(np.diff(y_coordinates)))
    x_geo = inverse.transform(centre_source_x + source_dx, centre_source_y)
    y_geo = inverse.transform(centre_source_x, centre_source_y + source_dy)
    x_page = page_transform.geographic_to_page(x_geo)
    y_page = page_transform.geographic_to_page(y_geo)
    x_pixel_mm = max(math.dist(centre_page, x_page), 1e-9)
    y_pixel_mm = max(math.dist(centre_page, y_page), 1e-9)
    sigma_x_pixels = FALL_LINE_GAUSSIAN_SIGMA_MM / x_pixel_mm
    sigma_y_pixels = FALL_LINE_GAUSSIAN_SIGMA_MM / y_pixel_mm
    smoothed = _nan_gaussian_separable(
        values,
        sigma_x_pixels=sigma_x_pixels,
        sigma_y_pixels=sigma_y_pixels,
    )
    gradient_x, gradient_y = _central_difference_gradients(
        smoothed, x_coordinates, y_coordinates
    )
    finite_gradient = np.isfinite(gradient_x) & np.isfinite(gradient_y)
    slope_values = np.degrees(
        np.arctan(np.hypot(gradient_x[finite_gradient], gradient_y[finite_gradient]))
    )
    adaptive_slope_metadata: dict[str, Any] | None = None
    minimum_seed_slope_deg = 4.0
    minimum_trace_slope_deg = 1.5
    adaptive_seed_slope = (
        config.fall_line_seed_slope_policy == "global-page-smoothed-adaptive-v1"
    )
    if (
        not adaptive_seed_slope
        and config.fall_line_seed_slope_policy != "all-at-or-above-4deg-v1"
    ):
        _fail("unsupported fall-line seed slope policy")
    probe_m = max(abs(source_dx), abs(source_dy))

    phase_seed = int(window_sha256[:16], 16)
    phase_x = float(phase_seed & 0xFFFFFFFF) / float(2**32) * FALL_LINE_LATTICE_MM
    phase_y = (
        float((phase_seed >> 32) & 0xFFFFFFFF) / float(2**32) * FALL_LINE_LATTICE_MM
    )
    if adaptive_seed_slope:
        lattice_slopes: list[float] = []
        lattice_row = 0
        lattice_y = page_transform.rect.y - FALL_LINE_LATTICE_MM + phase_y
        while lattice_y < page_transform.rect.y:
            lattice_y += FALL_LINE_LATTICE_MM
        while lattice_y <= page_transform.rect.bottom + 1e-9:
            lattice_x = (
                page_transform.rect.x
                - FALL_LINE_LATTICE_MM
                + phase_x
                + (FALL_LINE_LATTICE_OFFSET_MM if lattice_row % 2 else 0.0)
            )
            while lattice_x < page_transform.rect.x:
                lattice_x += FALL_LINE_LATTICE_MM
            while lattice_x <= page_transform.rect.right + 1e-9:
                lattice_sample = _page_gradient_sample(
                    (lattice_x, lattice_y),
                    page_transform=page_transform,
                    forward=forward,
                    inverse=inverse,
                    gradient_x=gradient_x,
                    gradient_y=gradient_y,
                    x_coordinates=x_coordinates,
                    y_coordinates=y_coordinates,
                    direction_sign=1.0,
                    probe_m=probe_m,
                )
                if lattice_sample is not None:
                    lattice_slopes.append(float(lattice_sample[0]))
                lattice_x += FALL_LINE_LATTICE_MM
            lattice_y += FALL_LINE_LATTICE_MM
            lattice_row += 1
        maximum_slope_deg = float(np.max(slope_values)) if slope_values.size else 0.0
        lattice_maximum_slope_deg = max(lattice_slopes, default=0.0)
        percentile_slope_deg = (
            float(np.percentile(lattice_slopes, 75.0)) if lattice_slopes else 0.0
        )
        activation_slope_deg = 0.75
        if maximum_slope_deg >= activation_slope_deg:
            selected = min(4.0, max(0.15, percentile_slope_deg))
            minimum_seed_slope_deg = math.floor(selected * 1_000.0) / 1_000.0
            minimum_trace_slope_deg = (
                math.floor(max(0.05, min(1.5, minimum_seed_slope_deg * 0.4)) * 1_000.0)
                / 1_000.0
            )
        adaptive_slope_metadata = {
            "selection_rule": (
                "fixed-4deg-when-smoothed-grid-maximum-below-0.75deg-otherwise-"
                "clamp-page-lattice-p75-to-0.15..4deg-v2"
            ),
            "page_smoothed_gradient_sample_count": int(slope_values.size),
            "page_smoothed_lattice_sample_count": len(lattice_slopes),
            "page_smoothed_slope_percentile": 75.0,
            "page_smoothed_percentile_slope_deg": round(percentile_slope_deg, 6),
            "page_smoothed_lattice_maximum_slope_deg": round(
                lattice_maximum_slope_deg, 6
            ),
            "page_smoothed_maximum_slope_deg": round(maximum_slope_deg, 6),
            "activation_slope_deg": activation_slope_deg,
            "selected_minimum_seed_slope_deg": minimum_seed_slope_deg,
            "selected_minimum_trace_slope_deg": minimum_trace_slope_deg,
        }
    candidates: list[dict[str, Any]] = []
    row = 0
    page_y = page_transform.rect.y - FALL_LINE_LATTICE_MM + phase_y
    while page_y < page_transform.rect.y:
        page_y += FALL_LINE_LATTICE_MM
    while page_y <= page_transform.rect.bottom + 1e-9:
        page_x = (
            page_transform.rect.x
            - FALL_LINE_LATTICE_MM
            + phase_x
            + (FALL_LINE_LATTICE_OFFSET_MM if row % 2 else 0.0)
        )
        while page_x < page_transform.rect.x:
            page_x += FALL_LINE_LATTICE_MM
        column = 0
        while page_x <= page_transform.rect.right + 1e-9:
            seed = (page_x, page_y)
            sample = _page_gradient_sample(
                seed,
                page_transform=page_transform,
                forward=forward,
                inverse=inverse,
                gradient_x=gradient_x,
                gradient_y=gradient_y,
                x_coordinates=x_coordinates,
                y_coordinates=y_coordinates,
                direction_sign=1.0,
                probe_m=probe_m,
            )
            if sample is not None:
                slope_deg = sample[0]
                seed_elevation_m = _bilinear_sample(
                    smoothed,
                    x_coordinates,
                    y_coordinates,
                    sample[3],
                    sample[4],
                )
                elevation_is_eligible = seed_elevation_m is not None and (
                    config.fall_line_seed_minimum_elevation_m is None
                    or seed_elevation_m >= config.fall_line_seed_minimum_elevation_m
                )
                if slope_deg >= minimum_seed_slope_deg and elevation_is_eligible:
                    assert seed_elevation_m is not None
                    target_mm = FALL_LINE_MINIMUM_MM + (
                        FALL_LINE_PREFERRED_MAXIMUM_MM - FALL_LINE_MINIMUM_MM
                    ) * min(
                        1.0,
                        max(
                            0.0,
                            (slope_deg - minimum_seed_slope_deg)
                            / max(30.0 - minimum_seed_slope_deg, 1.0),
                        ),
                    )
                    target_mm = min(target_mm, FALL_LINE_MAXIMUM_MM)
                    uphill = _trace_fall_line_branch(
                        seed,
                        target_mm=target_mm / 2.0,
                        direction_sign=-1.0,
                        page_transform=page_transform,
                        forward=forward,
                        inverse=inverse,
                        gradient_x=gradient_x,
                        gradient_y=gradient_y,
                        x_coordinates=x_coordinates,
                        y_coordinates=y_coordinates,
                        probe_m=probe_m,
                        minimum_trace_slope_deg=minimum_trace_slope_deg,
                    )
                    uphill_length_mm = _polyline_length(uphill)
                    downhill = _trace_fall_line_branch(
                        seed,
                        target_mm=max(target_mm - uphill_length_mm, 0.0),
                        direction_sign=1.0,
                        page_transform=page_transform,
                        forward=forward,
                        inverse=inverse,
                        gradient_x=gradient_x,
                        gradient_y=gradient_y,
                        x_coordinates=x_coordinates,
                        y_coordinates=y_coordinates,
                        probe_m=probe_m,
                        minimum_trace_slope_deg=minimum_trace_slope_deg,
                    )
                    page_points = [*reversed(uphill), *downhill[1:]]
                    page_length_mm = _polyline_length(page_points)
                    if page_length_mm >= FALL_LINE_MINIMUM_MM:
                        traced_slopes = [
                            traced_sample[0]
                            for traced_sample in (
                                _page_gradient_sample(
                                    point,
                                    page_transform=page_transform,
                                    forward=forward,
                                    inverse=inverse,
                                    gradient_x=gradient_x,
                                    gradient_y=gradient_y,
                                    x_coordinates=x_coordinates,
                                    y_coordinates=y_coordinates,
                                    direction_sign=1.0,
                                    probe_m=probe_m,
                                )
                                for point in page_points
                            )
                            if traced_sample is not None
                        ]
                        mean_slope_deg = sum(traced_slopes) / len(traced_slopes)
                        geographic_points = [
                            [round(longitude, 6), round(latitude, 6)]
                            for longitude, latitude in (
                                page_transform.page_to_geographic(point)
                                for point in page_points
                            )
                        ]
                        seed_longitude, seed_latitude = (
                            page_transform.page_to_geographic(seed)
                        )
                        sampled_x = _bilinear_sample(
                            gradient_x,
                            x_coordinates,
                            y_coordinates,
                            sample[3],
                            sample[4],
                        )
                        sampled_y = _bilinear_sample(
                            gradient_y,
                            x_coordinates,
                            y_coordinates,
                            sample[3],
                            sample[4],
                        )
                        if sampled_x is None or sampled_y is None:
                            page_x += FALL_LINE_LATTICE_MM
                            column += 1
                            continue
                        aspect_deg = (
                            math.degrees(math.atan2(-sampled_x, -sampled_y)) + 360.0
                        ) % 360.0
                        stroke_id = f"fall-line-r{row:03d}-c{column:03d}"
                        candidates.append(
                            {
                                "id": stroke_id,
                                "points": geographic_points,
                                "seed": [
                                    round(seed_longitude, 6),
                                    round(seed_latitude, 6),
                                ],
                                "seed_lattice": {
                                    "row": row,
                                    "column": column,
                                    "page_x_mm": round(seed[0], 3),
                                    "page_y_mm": round(seed[1], 3),
                                },
                                "seed_slope_deg": round(slope_deg, 3),
                                "mean_slope_deg": round(mean_slope_deg, 3),
                                "seed_aspect_deg": round(aspect_deg, 3),
                                "seed_elevation_m": round(float(seed_elevation_m), 3),
                                "page_length_mm": round(page_length_mm, 3),
                                "geometry_sha256": _canonical_sha256(geographic_points),
                                "algorithm_id": RELIEF_ALGORITHM_ID,
                                "provenance": {
                                    "source_ref": config.source_id,
                                    "derived_window_sha256": window_sha256,
                                    "source_crs": str(forward.target_crs),
                                    "gradient": "central-difference-source-crs-v1",
                                    "interpolation": "bilinear-gradient-v1",
                                },
                                "_page_geometry": LineString(page_points),
                            }
                        )
            page_x += FALL_LINE_LATTICE_MM
            column += 1
        page_y += FALL_LINE_LATTICE_MM
        row += 1

    candidates.sort(
        key=lambda stroke: (
            -float(stroke["seed_slope_deg"]),
            str(stroke["geometry_sha256"]),
            str(stroke["id"]),
        )
    )
    selected: list[dict[str, Any]] = []
    selected_length_mm = 0.0
    for candidate in candidates:
        if len(selected) >= FALL_LINE_MAXIMUM_STROKES:
            break
        length_mm = float(candidate["page_length_mm"])
        if selected_length_mm + length_mm > FALL_LINE_MAXIMUM_TOTAL_MM:
            continue
        if any(
            candidate["_page_geometry"].distance(stroke["_page_geometry"])
            < FALL_LINE_SPACING_MM
            for stroke in selected
        ):
            continue
        selected.append(candidate)
        selected_length_mm += length_mm

    retained_indices: set[int] = set()
    for cluster in _fall_line_clusters(selected):
        cluster_length_mm = sum(
            float(selected[index]["page_length_mm"]) for index in cluster
        )
        if (
            len(cluster) >= FALL_LINE_CLUSTER_MINIMUM_STROKES
            and cluster_length_mm >= FALL_LINE_CLUSTER_MINIMUM_LENGTH_MM
        ):
            retained_indices.update(cluster)
    retained = [
        stroke for index, stroke in enumerate(selected) if index in retained_indices
    ]
    retained.sort(
        key=lambda stroke: (
            int(stroke["seed_lattice"]["row"]),
            (
                int(stroke["seed_lattice"]["column"])
                if int(stroke["seed_lattice"]["row"]) % 2 == 0
                else -int(stroke["seed_lattice"]["column"])
            ),
        )
    )
    for stroke in retained:
        stroke.pop("_page_geometry", None)
    manifest = [
        {"id": stroke["id"], "geometry_sha256": stroke["geometry_sha256"]}
        for stroke in retained
    ]
    policy = {
        "algorithm_id": RELIEF_ALGORITHM_ID,
        "binding_format_id": page_transform.format_id,
        "binding_transform_sha256": page_transform.binding_sha256,
        "pen_nib_mm": FALL_LINE_PEN_NIB_MM,
        "gaussian_sigma_page_mm": FALL_LINE_GAUSSIAN_SIGMA_MM,
        "gaussian_sigma_pixels": {
            "x": round(sigma_x_pixels, 6),
            "y": round(sigma_y_pixels, 6),
        },
        "gradient": "central-difference-source-crs-v1",
        "lattice_spacing_mm": FALL_LINE_LATTICE_MM,
        "alternate_row_offset_mm": FALL_LINE_LATTICE_OFFSET_MM,
        "lattice_phase_from_window_sha256": {
            "x_mm": round(phase_x, 6),
            "y_mm": round(phase_y, 6),
        },
        "seed_slope_policy": config.fall_line_seed_slope_policy,
        "minimum_seed_slope_deg": minimum_seed_slope_deg,
        "minimum_seed_elevation_m": (config.fall_line_seed_minimum_elevation_m),
        "seed_elevation_is_trace_constraint": False,
        "minimum_trace_slope_deg": minimum_trace_slope_deg,
        "low_slope_stop_samples": 2,
        "trace_step_mm": FALL_LINE_TRACE_STEP_MM,
        "preferred_target_length_mm": [
            FALL_LINE_MINIMUM_MM,
            FALL_LINE_PREFERRED_MAXIMUM_MM,
        ],
        "hard_maximum_length_mm": FALL_LINE_MAXIMUM_MM,
        "target_length_slope_mapping": ("linear-selected-minimum-to-30deg-clamped-v1"),
        "maximum_turn_deg_per_trace_step": 15.0,
        "exhausted_uphill_budget_redistributed_downhill": True,
        "minimum_stroke_mm": FALL_LINE_MINIMUM_MM,
        "acceptance": "steepest-seed-first-v1",
        "minimum_pairwise_spacing_mm": FALL_LINE_SPACING_MM,
        "cluster_connectivity_distance_mm": FALL_LINE_CLUSTER_DISTANCE_MM,
        "minimum_cluster_strokes": FALL_LINE_CLUSTER_MINIMUM_STROKES,
        "minimum_cluster_total_mm": FALL_LINE_CLUSTER_MINIMUM_LENGTH_MM,
        "maximum_strokes": FALL_LINE_MAXIMUM_STROKES,
        "maximum_total_mm": FALL_LINE_MAXIMUM_TOTAL_MM,
        "candidate_count": len(candidates),
        "spacing_accepted_count": len(selected),
        "retained_count": len(retained),
        "retained_total_mm": round(
            sum(float(stroke["page_length_mm"]) for stroke in retained), 3
        ),
        "emission_order": "spatial-serpentine-lattice-v1",
        "runtime_clearance_mm": {"route": 1.1, "label": 0.5, "water": 0.4},
        "geometry_manifest_sha256": _canonical_sha256(manifest),
    }
    if adaptive_slope_metadata is not None:
        policy["adaptive_seed_slope"] = adaptive_slope_metadata
    return retained, policy


def _filter_contours_page_equivalent(
    contours: list[dict[str, Any]],
    *,
    page_transform: GeographicPageTransform,
    minimum_page_mm: float = CONTOUR_MINIMUM_PAGE_MM,
    maximum_paths: int = CONTOUR_MAXIMUM_PATHS,
    maximum_total_mm: float = CONTOUR_MAXIMUM_TOTAL_MM,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enforce the A5 contour floor and a spatially balanced plot budget.

    Contours arrive in elevation order.  Consuming the global path/length
    budget in that order starves higher levels and turns a relief plate into a
    few low regional outlines.  Selecting only the longest component at every
    level can also starve one end of a long route when a DEM contour has
    disconnected western and eastern components.  The first pass therefore
    rotates through three north-up page-x bands and chooses a substantial
    representative component for that band at each elevation.  Only complete
    source paths are retained; the policy never joins, shortens or invents
    terrain geometry.
    """

    spatial_band_count = 3
    band_width_mm = page_transform.rect.width / spatial_band_count
    eligible: list[list[tuple[int, list[list[float]], float, float]]] = []
    for contour_index, contour in enumerate(contours):
        candidates: list[tuple[int, list[list[float]], float, float]] = []
        for path_index, path in enumerate(contour["paths"]):
            page_points = [
                page_transform.geographic_to_page((float(point[0]), float(point[1])))
                for point in path
            ]
            length_mm = _polyline_length(page_points)
            if length_mm >= minimum_page_mm:
                page_x = (
                    min(point[0] for point in page_points)
                    + max(point[0] for point in page_points)
                ) / 2.0
                candidates.append((path_index, path, length_mm, page_x))
        if candidates:
            maximum_length_mm = max(item[2] for item in candidates)
            target_band = contour_index % spatial_band_count
            target_x = (
                page_transform.rect.x + (target_band + 0.5) * band_width_mm
            )
            representative = min(
                candidates,
                key=lambda item: (
                    abs(item[3] - target_x) / max(band_width_mm, 1e-9)
                    - item[2] / maximum_length_mm,
                    -item[2],
                    item[0],
                ),
            )
            remaining = sorted(
                (item for item in candidates if item is not representative),
                key=lambda item: (-item[2], item[0]),
            )
            candidates = [representative, *remaining]
        eligible.append(candidates)

    selected: list[list[tuple[int, list[list[float]], float, float]]] = [
        [] for _ in contours
    ]
    retained_paths = 0
    retained_total_mm = 0.0
    rank = 0
    while retained_paths < maximum_paths:
        progressed = False
        for contour_index, candidates in enumerate(eligible):
            if retained_paths >= maximum_paths:
                break
            if rank >= len(candidates):
                continue
            candidate = candidates[rank]
            if retained_total_mm + candidate[2] > maximum_total_mm:
                continue
            selected[contour_index].append(candidate)
            retained_paths += 1
            retained_total_mm += candidate[2]
            progressed = True
        if not progressed:
            break
        rank += 1

    retained: list[dict[str, Any]] = []
    for contour, chosen in zip(contours, selected):
        if not chosen:
            continue
        chosen.sort(key=lambda item: item[0])
        retained.append(
            {
                **contour,
                "paths": [item[1] for item in chosen],
                "page_lengths_mm": [round(item[2], 3) for item in chosen],
            }
        )
    retained_band_counts = [0] * spatial_band_count
    for chosen in selected:
        for _path_index, _path, _length_mm, page_x in chosen:
            band_index = min(
                spatial_band_count - 1,
                max(
                    0,
                    int(
                        (page_x - page_transform.rect.x)
                        / max(band_width_mm, 1e-9)
                    ),
                ),
            )
            retained_band_counts[band_index] += 1
    return retained, {
        "minimum_path_page_mm": minimum_page_mm,
        "maximum_paths": maximum_paths,
        "maximum_total_mm": maximum_total_mm,
        "retained_paths": retained_paths,
        "retained_levels": len(retained),
        "retained_total_mm": round(retained_total_mm, 3),
        "geometry_policy": (
            "whole-source-contour-level-spatial-page-budget-v3"
        ),
        "spatial_band_count": spatial_band_count,
        "first_pass_band_order": "contour-index-modulo-page-x-thirds-v1",
        "representative_score": (
            "band-centre-distance-minus-normalized-length-v1"
        ),
        "retained_path_centre_band_counts": retained_band_counts,
    }


def _finish_exclusion(
    record: dict[str, Any],
    *,
    forward: Transformer,
    radius_m: float,
) -> BaseGeometry | None:
    """Build a source-control-centred no-relief zone for a crowded finish."""

    if radius_m <= 0.0:
        return None
    finish_points = [
        control["point"]
        for control in record["route"].get("controls", [])
        if control.get("kind") == "finish"
    ]
    if not finish_points:
        _fail("finish clearance requested but the route has no finish control")
    projected = [
        transform(
            forward.transform,
            Point(float(point[0]), float(point[1])),
        )
        for point in finish_points
    ]
    return unary_union(projected).buffer(radius_m)


def _terrain_anchors(
    record: dict[str, Any],
    *,
    forward: Transformer,
    feature_ids: tuple[str, ...],
) -> tuple[tuple[str, str, Point], ...]:
    """Project the reviewed named features that organise relief clusters."""

    if not feature_ids:
        return ()
    features = {
        str(feature["id"]): feature for feature in record["context"]["features"]
    }
    missing = [feature_id for feature_id in feature_ids if feature_id not in features]
    if missing:
        _fail(f"unknown terrain anchor feature(s): {', '.join(missing)}")
    anchors: list[tuple[str, str, Point]] = []
    for feature_id in feature_ids:
        feature = features[feature_id]
        point = feature["point"]
        projected = transform(
            forward.transform,
            Point(float(point[0]), float(point[1])),
        )
        anchors.append((feature_id, str(feature["kind"]), projected))
    return tuple(anchors)


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    """Return valid polygon parts without bridging disconnected source masks."""

    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        return [
            polygon
            for child in geometry.geoms  # type: ignore[attr-defined]
            for polygon in _polygon_parts(child)
        ]
    return []


def _select_elevation_mask_candidates(
    candidates: list[tuple[float, float, Polygon]],
    *,
    route: BaseGeometry,
    cap: int,
) -> list[tuple[float, float, Polygon]]:
    """Spread coherent threshold components along the route's dominant axis."""

    if cap <= 0 or not candidates:
        return []
    minimum_x, minimum_y, maximum_x, maximum_y = route.bounds
    use_x = maximum_x - minimum_x >= maximum_y - minimum_y
    axis_minimum = minimum_x if use_x else minimum_y
    axis_maximum = maximum_x if use_x else maximum_y
    axis_span = max(axis_maximum - axis_minimum, 1.0)
    bin_count = min(5, cap)
    buckets: list[list[tuple[float, float, Polygon]]] = [[] for _ in range(bin_count)]

    def rank(item: tuple[float, float, Polygon]) -> tuple[int, float, float, str]:
        distance, area, polygon = item
        return (int(distance // 5_000.0), -area, distance, polygon.wkb_hex)

    for item in candidates:
        centre = item[2].representative_point()
        coordinate = float(centre.x if use_x else centre.y)
        ratio = min(1.0, max(0.0, (coordinate - axis_minimum) / axis_span))
        index = min(int(ratio * bin_count), bin_count - 1)
        buckets[index].append(item)
    for bucket in buckets:
        bucket.sort(key=rank)

    selected: list[tuple[float, float, Polygon]] = []
    while len(selected) < cap:
        progressed = False
        for bucket in buckets:
            if bucket and len(selected) < cap:
                selected.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break
    if len(selected) < cap:
        selected_ids = {item[2].wkb_hex for item in selected}
        for candidate in sorted(candidates, key=rank):
            if len(selected) >= cap:
                break
            if candidate[2].wkb_hex not in selected_ids:
                selected.append(candidate)
                selected_ids.add(candidate[2].wkb_hex)
    return selected


def _elevation_masks(
    values: np.ndarray,
    *,
    affine: tuple[float, ...],
    crop: Polygon,
    route: BaseGeometry,
    inverse: Transformer,
    config: ElevationMaskConfig | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Polygonise a factual DEM threshold and select whole route-context masses."""

    if config is None:
        return [], None
    if not 3.0 <= config.hachure_spacing_mm <= 4.0:
        _fail("elevation-mask hachure spacing must be 3..4 mm")
    if not 5.0 <= config.hachure_along_pitch_mm <= 6.0:
        _fail("elevation-mask along-row hachure pitch must be 5..6 mm")
    if not 2.5 <= config.hachure_segment_length_mm <= 3.5:
        _fail("elevation-mask hachure length must be 2.5..3.5 mm")
    if config.minimum_area_m2 <= 0.0 or config.maximum_components <= 0:
        _fail("elevation-mask area floor and component cap must be positive")

    finite = np.isfinite(values)
    thresholded = np.where(finite & (values >= config.threshold_m), 1, 0).astype(
        np.uint8
    )
    raster_transform = rasterio.Affine(*affine)
    candidates: list[tuple[float, float, Polygon]] = []
    source_component_count = 0
    for mapping, value in shapes(
        thresholded,
        mask=finite,
        transform=raster_transform,
        connectivity=8,
    ):
        if int(value) != 1:
            continue
        source_component_count += 1
        clipped = shapely_shape(mapping).intersection(crop)
        for source_part in _polygon_parts(clipped):
            simplified = source_part.simplify(config.simplify_m, preserve_topology=True)
            for polygon in _polygon_parts(simplified):
                polygon = Polygon(
                    polygon.exterior.coords,
                    [list(interior.coords) for interior in polygon.interiors],
                )
                if polygon.area < config.minimum_area_m2:
                    continue
                distance = float(polygon.distance(route))
                if (
                    config.maximum_route_distance_m is not None
                    and distance > config.maximum_route_distance_m
                ):
                    continue
                candidates.append((distance, float(polygon.area), polygon))

    selected = _select_elevation_mask_candidates(
        candidates,
        route=route,
        cap=config.maximum_components,
    )
    masks: list[dict[str, Any]] = []
    for index, (distance, area, polygon) in enumerate(selected, start=1):
        geographic = transform(inverse.transform, polygon)
        geographic_parts = _polygon_parts(geographic)
        if len(geographic_parts) != 1:
            _fail(
                "a selected elevation-mask component changed topology in reprojection"
            )
        geographic_polygon = geographic_parts[0]
        outer = [
            [round(float(x), 6), round(float(y), 6)]
            for x, y in geographic_polygon.exterior.coords
        ]
        holes = sorted(
            (
                [[round(float(x), 6), round(float(y), 6)] for x, y in interior.coords]
                for interior in geographic_polygon.interiors
            ),
            key=_canonical_sha256,
        )
        geometry_sha256 = _canonical_sha256({"outer": outer, "holes": holes})
        masks.append(
            {
                "id": f"elevation-mask-{config.threshold_m}m-{index:02d}",
                "minimum_elevation_m": config.threshold_m,
                "outer": outer,
                "holes": holes,
                "derived_area_m2": round(area, 1),
                "distance_to_route_m": round(distance, 1),
                "geometry_sha256": geometry_sha256,
                "render_as_hachure": True,
                "geometry_policy": (
                    "source-derived-threshold-polygon-no-invented-links-v1"
                ),
                "rendering": {
                    "spacing_mm": config.hachure_spacing_mm,
                    "along_pitch_mm": config.hachure_along_pitch_mm,
                    "nominal_segment_length_mm": (config.hachure_segment_length_mm),
                    "angle_deg": config.hachure_angle_deg,
                    "inset_mm": config.hachure_inset_mm,
                    "maximum_strokes_per_area": (config.hachure_max_strokes_per_area),
                    "minimum_stroke_mm": 2.5,
                    "perimeter_rendered": False,
                    "treatment": ("source-mask-clipped-short-hachure-no-perimeter-v2"),
                },
            }
        )
    policy = {
        "threshold_m": config.threshold_m,
        "polygonization": "rasterio-features-8-connected-threshold-v1",
        "simplification_tolerance_m": config.simplify_m,
        "minimum_component_area_m2": config.minimum_area_m2,
        "maximum_route_distance_m": config.maximum_route_distance_m,
        "maximum_components": config.maximum_components,
        "source_component_count": source_component_count,
        "eligible_component_count": len(candidates),
        "selected_component_count": len(masks),
        "selected_geometry_manifest_sha256": _canonical_sha256(
            [
                {
                    "id": mask["id"],
                    "geometry_sha256": mask["geometry_sha256"],
                    "minimum_elevation_m": mask["minimum_elevation_m"],
                }
                for mask in masks
            ]
        ),
        "geometry_policy": ("whole-source-threshold-components-no-invented-links-v1"),
        "perimeter_rendered": False,
    }
    return masks, policy


def derive_subject(
    *,
    subject_id: str,
    catalog: dict[str, Any],
    bundle: dict[str, Any],
    retrieved_at: str,
) -> dict[str, Any]:
    config = CONFIGS.get(subject_id)
    if config is None:
        _fail(f"no reviewed raster terrain config for {subject_id!r}")
    record = _record(catalog, subject_id)
    raw_extent = record["context"].get(
        "map_extent", record["context"]["extent"]
    )
    if not isinstance(raw_extent, list) or len(raw_extent) != 4:
        _fail(f"{subject_id} has an invalid context extent")
    west, south, east, north = (float(value) for value in raw_extent)
    extent = (west, south, east, north)
    values, x_coordinates, y_coordinates, crs, affine, valid_fraction = _read_window(
        config, extent=extent
    )
    window_sha256 = _window_digest(values, crs=crs, transform_values=affine)
    forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    crop = transform(
        forward.transform,
        densified_bbox_polygon(west, south, east, north),
    )
    route = _route_geometry(record, forward)
    finish_exclusion = _finish_exclusion(
        record,
        forward=forward,
        radius_m=config.finish_clearance_m,
    )
    terrain_anchors = _terrain_anchors(
        record,
        forward=forward,
        feature_ids=config.anchor_feature_ids,
    )
    contours = _contour_paths(
        values,
        x_coordinates,
        y_coordinates,
        levels_m=config.levels_m,
        caps=config.caps,
        crop=crop,
        route=route,
        inverse=inverse,
        simplify_m=config.simplify_m,
        minimum_length_m=config.minimum_length_m,
        maximum_route_distance_m=config.maximum_route_distance_m,
        minimum_separation_m=config.minimum_separation_m,
        maximum_path_span_m=config.maximum_path_span_m,
        maximum_path_length_m=config.maximum_path_length_m,
        exclusion=finish_exclusion,
        prefer_broad=config.prefer_broad_contours,
        anchors=terrain_anchors,
        anchor_cluster_radius_m=config.anchor_cluster_radius_m,
        anchor_cluster_minimum_paths=config.anchor_cluster_minimum_paths,
        anchor_cluster_maximum_paths=config.anchor_cluster_maximum_paths,
    )
    page_transform = _page_transform_for_record(record)
    contours, contour_page_policy = _filter_contours_page_equivalent(
        contours,
        page_transform=page_transform,
    )
    if not contours:
        _fail(f"{subject_id} produced no contours above the 8 mm page floor")
    relief_strokes, relief_stroke_policy = _derive_relief_strokes(
        values,
        x_coordinates,
        y_coordinates,
        record=record,
        config=config,
        forward=forward,
        inverse=inverse,
        window_sha256=window_sha256,
    )
    if not relief_strokes:
        _fail(f"{subject_id} produced no plot-eligible DEM fall lines")
    overlay = _overlay(bundle, subject_id)
    sources = overlay.setdefault("sources", [])
    if not isinstance(sources, list):
        _fail(f"{subject_id} overlay sources must be an array")
    sources[:] = [item for item in sources if item.get("id") != config.source_id]
    sources.append(
        {
            "id": config.source_id,
            "publisher": config.publisher,
            "url": config.product_url,
            "license": config.license,
            "attribution": config.attribution,
            "use": (
                "source DEM window; selected elevation-valued artwork contours "
                "and frozen DEM-gradient fall-line hachures"
            ),
            "release": config.source_release,
            "retrieved_at": retrieved_at,
            "source_raster_url": config.source_url,
            "horizontal_crs": crs,
            "vertical_datum": config.vertical_datum,
            "source_resolution_m": config.source_resolution_m,
            "derived_window_resolution_m": config.target_resolution_m,
            "derived_window_sha256": window_sha256,
            "derived_window_valid_fraction": round(valid_fraction, 6),
        }
    )
    context = overlay.setdefault("context", {})
    context["terrain"] = {
        "status": "source-derived-dtm-relief",
        "source_ref": config.source_id,
        "derivation_id": _derivation_id(subject_id),
        "source_crs": crs,
        "vertical_datum": config.vertical_datum,
        "source_grid_resolution_m": config.source_resolution_m,
        "derived_grid_resolution_m": config.target_resolution_m,
        "derived_window_sha256": window_sha256,
        "derived_window_valid_fraction": round(valid_fraction, 6),
        "contour_levels_m": list(config.levels_m),
        "simplification_tolerance_m": {"contours": config.simplify_m},
        "contour_selection_caps": {
            str(level): cap
            for level, cap in zip(config.levels_m, config.caps, strict=True)
        },
        "contour_selection_policy": {
            "minimum_pairwise_separation_m": config.minimum_separation_m,
            "maximum_route_distance_m": config.maximum_route_distance_m,
            "maximum_path_span_m": config.maximum_path_span_m,
            "maximum_path_length_m": config.maximum_path_length_m,
            "finish_clearance_m": config.finish_clearance_m,
            "prefer_broad_source_contours": config.prefer_broad_contours,
            "anchor_feature_ids": list(config.anchor_feature_ids),
            "anchor_cluster_radius_m": config.anchor_cluster_radius_m,
            "anchor_cluster_minimum_paths": config.anchor_cluster_minimum_paths,
            "anchor_cluster_maximum_paths": config.anchor_cluster_maximum_paths,
            "anchor_selection": "named-source-feature-nested-clusters-v1",
            "geometry_policy": "whole-source-contour-selection-no-invented-links-v1",
            "page_equivalent_filter": contour_page_policy,
        },
        "relief_algorithm_id": RELIEF_ALGORITHM_ID,
        "relief_strokes": relief_strokes,
        "relief_stroke_policy": relief_stroke_policy,
        "relief_geometry_manifest_sha256": relief_stroke_policy[
            "geometry_manifest_sha256"
        ],
        # Both older collections remain inert.  Production v7 geometry is
        # frozen in relief_strokes and never reconstructed from an area mask.
        "areas": [],
        "elevation_masks": [],
        "contours": contours,
    }
    backdrop = overlay.setdefault("backdrop", {})
    backdrop["status"] = "source-derived"
    backdrop["terrain"] = "source-derived-dtm-relief"
    overlay["credit_line"] = config.credit_line
    return {
        "levels": len(contours),
        "paths": sum(len(contour["paths"]) for contour in contours),
        "fall_lines": len(relief_strokes),
        "fall_line_mm": relief_stroke_policy["retained_total_mm"],
        "contour_mm": contour_page_policy["retained_total_mm"],
        "minimum_m": float(np.nanmin(values)),
        "maximum_m": float(np.nanmax(values)),
        "valid_fraction": valid_fraction,
        "window_sha256": window_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", action="append", required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--retrieved-at", default="2026-08-03T00:00:00Z")
    args = parser.parse_args()
    catalog = _load_object(args.catalog)
    bundle = _load_object(args.bundle)
    for subject_id in args.subject:
        result = derive_subject(
            subject_id=subject_id,
            catalog=catalog,
            bundle=bundle,
            retrieved_at=args.retrieved_at,
        )
        print(
            f"{subject_id}: levels={result['levels']}, paths={result['paths']}, "
            f"fall_lines={result['fall_lines']}/{result['fall_line_mm']:.1f}mm, "
            f"contours={result['contour_mm']:.1f}mm, "
            f"elevation={result['minimum_m']:.1f}..{result['maximum_m']:.1f}m, "
            f"valid={result['valid_fraction']:.1%}, "
            f"window_sha256={result['window_sha256']}"
        )
    args.bundle.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
