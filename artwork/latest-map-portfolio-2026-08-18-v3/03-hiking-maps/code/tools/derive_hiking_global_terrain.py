#!/usr/bin/env python3
"""Freeze global Mapzen Terrarium terrain into hiking catalog records.

The renderer never calls this tool and never needs network access.  This
derivation stage downloads (or reuses cached) AWS Open Data Terrarium PNG
tiles, decodes their elevations, samples route/peak elevations, and stores
selected contours and DEM-gradient fall lines as ordinary geographic points.

Only north-up A5 bindings are accepted.  The emitted ``context.terrain``
object deliberately follows the legacy hiking terrain contract so catalogs
can be rendered by existing releases of :mod:`city_map_plotter.hike_plates`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import tempfile
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence

import numpy as np
from pyproj import Transformer  # type: ignore[import-not-found]
from rasterio.errors import NotGeoreferencedWarning  # type: ignore[import-untyped]
from rasterio.io import MemoryFile  # type: ignore[import-untyped]
from shapely.geometry import LineString, box
from shapely.ops import transform, unary_union

from derive_hiking_raster_terrain_context import (
    RELIEF_ALGORITHM_ID,
    RasterTerrainConfig,
    _bilinear_sample,
    _derive_relief_strokes,
    _filter_contours_page_equivalent,
    _page_transform_for_record,
)
from derive_hiking_terrain_context import _contour_paths
from hiking_map_extent import bind_aspect_expanded_map_extent


TERRARIUM_URL_TEMPLATE = (
    "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png"
)
TERRARIUM_PRODUCT_URL = "https://registry.opendata.aws/terrain-tiles/"
TERRARIUM_PUBLISHER = "Mapzen terrain tiles via AWS Open Data"
TERRARIUM_LICENSE = "mixed source terms; location-specific review required"
TERRARIUM_ATTRIBUTION = (
    "Mapzen terrain tiles via AWS Open Data; underlying provider attribution required"
)
TERRARIUM_ATTRIBUTION_URL = (
    "https://github.com/tilezen/joerd/blob/master/docs/attribution.md"
)
VISIBLE_OSM_TERRAIN_CREDIT = "© OpenStreetMap CONTRIBUTORS / MAPZEN AWS TERRAIN"
WEB_MERCATOR_LIMIT_LATITUDE = 85.0511287798066
WEB_MERCATOR_HALF_WORLD_M = 20_037_508.342789244
TILE_PIXELS = 256
DEFAULT_ZOOM = 9
MAX_TILES_PER_SUBJECT = 256
AUTO_ZOOM_TILE_TARGET = 48
MINIMUM_AUTO_ZOOM = 4
DERIVATION_VERSION = 6
SOURCE_ID_PREFIX = "aws-mapzen-terrarium"
TERRESTRIAL_MINIMUM_ELEVATION_M = 0.0
TERRESTRIAL_FALLBACK_RADIUS_PIXELS = 3
TERRESTRIAL_ROUTE_ELEVATION_METHOD = (
    "mapzen-terrarium-terrestrial-bilinear-or-nearest-nonnegative-v1"
)
TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID = (
    "terrestrial-walk-nonnegative-source-sample-v1"
)
# The relief edition deliberately follows the continuous contour language of
# the accepted Tour des Refuges proof.  The renderer already drops fragments
# below 2.5 mm, so imposing a larger derivation floor only made mountain ranges
# dissolve into a handful of regional outlines.  The total budget remains
# finite and comparable with the 3.9 m grey-pen control artwork.
GLOBAL_CONTOUR_MINIMUM_PAGE_MM = 2.5
GLOBAL_CONTOUR_MAXIMUM_PATHS = 144
GLOBAL_CONTOUR_MAXIMUM_TOTAL_MM = 3_800.0
GLOBAL_CONTOUR_PATHS_PER_LEVEL = 16
GLOBAL_CONTOUR_TARGET_LEVELS = 16.0
GLOBAL_CONTOUR_MAXIMUM_LEVELS = 20
MIXED_ROUTE_SAMPLING_POLICY_ID = (
    "mode-aware-terrestrial-and-ferry-sea-surface-reference-v1"
)
FERRY_SEA_SURFACE_POLICY_ID = "explicit-ferry-sea-surface-reference-0m-v1"
FERRY_SEA_SURFACE_REFERENCE_M = 0.0
FERRY_SEA_SURFACE_DATUM = "nominal sea-surface reference"

TileFetcher = Callable[[str], bytes]


@dataclass(frozen=True)
class RouteSamplingSummary:
    """Counts that distinguish source samples from explicit ferry references."""

    total_point_count: int
    terrestrial_source_sample_count: int
    terrestrial_fallback_count: int
    ferry_sea_surface_reference_count: int
    ferry_segment_count: int
    profile_point_count: int
    profile_source_sample_count: int
    profile_fallback_count: int


class GlobalTerrainError(ValueError):
    """Raised when terrain evidence cannot be derived truthfully."""


def _fail(message: str) -> NoReturn:
    raise GlobalTerrainError(message)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_extent(raw: Any, *, subject_id: str) -> tuple[float, float, float, float]:
    if not isinstance(raw, list) or len(raw) != 4:
        _fail(f"{subject_id}: context.extent must be [west, south, east, north]")
    try:
        west, south, east, north = (float(value) for value in raw)
    except (TypeError, ValueError):
        _fail(f"{subject_id}: context.extent must contain finite numbers")
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        _fail(f"{subject_id}: context.extent must contain finite numbers")
    if not (
        -180.0 <= west < east <= 180.0
        and -WEB_MERCATOR_LIMIT_LATITUDE <= south < north <= WEB_MERCATOR_LIMIT_LATITUDE
    ):
        _fail(f"{subject_id}: context.extent is outside the Web Mercator domain")
    return west, south, east, north


def _world_pixel(longitude: float, latitude: float, zoom: int) -> tuple[float, float]:
    world_pixels = float(TILE_PIXELS * (1 << zoom))
    latitude = min(
        max(latitude, -WEB_MERCATOR_LIMIT_LATITUDE),
        WEB_MERCATOR_LIMIT_LATITUDE,
    )
    x = (longitude + 180.0) / 360.0 * world_pixels
    radians = math.radians(latitude)
    y = (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * world_pixels
    return x, y


def _tile_range(
    extent: tuple[float, float, float, float], zoom: int
) -> tuple[range, range]:
    west, south, east, north = extent
    west_px, north_px = _world_pixel(west, north, zoom)
    east_px, south_px = _world_pixel(east, south, zoom)
    tile_count = 1 << zoom
    minimum_x = min(max(int(math.floor(west_px / TILE_PIXELS)), 0), tile_count - 1)
    maximum_x = min(
        max(int(math.floor(math.nextafter(east_px, -math.inf) / TILE_PIXELS)), 0),
        tile_count - 1,
    )
    minimum_y = min(max(int(math.floor(north_px / TILE_PIXELS)), 0), tile_count - 1)
    maximum_y = min(
        max(int(math.floor(math.nextafter(south_px, -math.inf) / TILE_PIXELS)), 0),
        tile_count - 1,
    )
    return range(minimum_x, maximum_x + 1), range(minimum_y, maximum_y + 1)


def _automatic_zoom(
    record: dict[str, Any],
    *,
    maximum_zoom: int,
) -> int:
    """Choose the finest useful zoom without making continental mosaics.

    A5 cannot reproduce z9 detail across a multi-state or cross-country route.
    Capping the mosaic also keeps the frozen derivation practical while short
    mountain walks retain the finer terrain grid.
    """

    subject_id = str(record.get("id") or "<unknown>")
    working = copy.deepcopy(record)
    if working.get("subject_kind") == "route_plate":
        bind_aspect_expanded_map_extent(working, has_profile=True)
    context = working.get("context")
    if not isinstance(context, dict):
        _fail(f"{subject_id}: context must be an object")
    extent = _validate_extent(context.get("extent"), subject_id=subject_id)
    for candidate in range(maximum_zoom, MINIMUM_AUTO_ZOOM - 1, -1):
        x_range, y_range = _tile_range(extent, candidate)
        if len(x_range) * len(y_range) <= AUTO_ZOOM_TILE_TARGET:
            return candidate
    _fail(
        f"{subject_id}: extent remains too large for automatic terrain at "
        f"zoom {MINIMUM_AUTO_ZOOM}"
    )


def _decode_terrarium_png(payload: bytes) -> np.ndarray:
    """Decode one RGB Terrarium tile to elevation metres."""

    try:
        with warnings.catch_warnings():
            # Slippy-map tiles are located by z/x/y and intentionally carry no
            # embedded geotransform; their absence is not a data defect.
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with MemoryFile(payload) as memory_file, memory_file.open() as dataset:
                if dataset.width != TILE_PIXELS or dataset.height != TILE_PIXELS:
                    _fail(
                        "Terrarium tile dimensions must be "
                        f"{TILE_PIXELS}x{TILE_PIXELS}, got "
                        f"{dataset.width}x{dataset.height}"
                    )
                if dataset.count < 3:
                    _fail("Terrarium PNG must contain at least three RGB bands")
                rgb = dataset.read((1, 2, 3)).astype(np.float32)
    except GlobalTerrainError:
        raise
    except Exception as exc:  # rasterio/GDAL exposes several backend exceptions.
        _fail(f"could not decode Terrarium PNG: {exc}")
    red, green, blue = rgb
    values = red * 256.0 + green + blue / 256.0 - 32_768.0
    values[(values < -500.0) | (values > 9_000.0)] = np.nan
    return values.astype(np.float32, copy=False)


def _download_tile(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "city-map-plotter-global-terrain/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError) as exc:
        _fail(f"could not download {url}: {exc}")
    if not payload:
        _fail(f"downloaded empty Terrarium tile {url}")
    return payload


def _cached_tile(
    *,
    cache_dir: Path,
    zoom: int,
    x: int,
    y: int,
    fetcher: TileFetcher,
) -> tuple[np.ndarray, dict[str, Any]]:
    url = TERRARIUM_URL_TEMPLATE.format(z=zoom, x=x, y=y)
    path = cache_dir / str(zoom) / str(x) / f"{y}.png"
    if path.is_file():
        try:
            payload = path.read_bytes()
        except OSError as exc:
            _fail(f"could not read cached tile {path}: {exc}")
        cache_status = "hit"
    else:
        payload = fetcher(url)
        # Validate before making a failed or non-PNG response persistent.
        values = _decode_terrarium_png(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            _fail(f"could not cache tile {path}: {exc}")
        cache_status = "downloaded"
    if cache_status == "hit":
        values = _decode_terrarium_png(payload)
    return values, {
        "z": zoom,
        "x": x,
        "y": y,
        "url": url,
        "sha256": _file_sha256(payload),
        "cache_status": cache_status,
    }


def _window_digest(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    *,
    extent: tuple[float, float, float, float],
    zoom: int,
    tile_records: Sequence[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "crs": "EPSG:3857",
                "extent": list(extent),
                "shape": list(values.shape),
                "zoom": zoom,
                "tiles": [
                    {
                        "z": record["z"],
                        "x": record["x"],
                        "y": record["y"],
                        "sha256": record["sha256"],
                    }
                    for record in tile_records
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(np.asarray(x_coordinates, dtype="<f8").tobytes(order="C"))
    digest.update(np.asarray(y_coordinates, dtype="<f8").tobytes(order="C"))
    canonical = np.nan_to_num(
        values.astype(np.float32, copy=False),
        nan=-3.4e38,
        posinf=3.4e38,
        neginf=-3.4e38,
    ).astype("<f4", copy=False)
    digest.update(canonical.tobytes(order="C"))
    return digest.hexdigest()


def _load_extent_grid(
    extent: tuple[float, float, float, float],
    *,
    zoom: int,
    cache_dir: Path,
    fetcher: TileFetcher = _download_tile,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict[str, Any]], str]:
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not 0 <= zoom <= 15:
        _fail("zoom must be an integer from 0 through 15")
    x_range, y_range = _tile_range(extent, zoom)
    tile_total = len(x_range) * len(y_range)
    if tile_total <= 0 or tile_total > MAX_TILES_PER_SUBJECT:
        _fail(
            f"extent needs {tile_total} tiles at zoom {zoom}; "
            f"the safety maximum is {MAX_TILES_PER_SUBJECT}"
        )
    mosaic: np.ndarray = np.full(
        (len(y_range) * TILE_PIXELS, len(x_range) * TILE_PIXELS),
        np.nan,
        dtype=np.float32,
    )
    tile_records: list[dict[str, Any]] = []
    for tile_row, y in enumerate(y_range):
        for tile_column, x in enumerate(x_range):
            tile, record = _cached_tile(
                cache_dir=cache_dir,
                zoom=zoom,
                x=x,
                y=y,
                fetcher=fetcher,
            )
            row = tile_row * TILE_PIXELS
            column = tile_column * TILE_PIXELS
            mosaic[row : row + TILE_PIXELS, column : column + TILE_PIXELS] = tile
            tile_records.append(record)

    west, south, east, north = extent
    west_px, north_px = _world_pixel(west, north, zoom)
    east_px, south_px = _world_pixel(east, south, zoom)
    origin_x = x_range.start * TILE_PIXELS
    origin_y = y_range.start * TILE_PIXELS
    column_start = max(0, int(math.floor(west_px)) - origin_x)
    column_stop = min(mosaic.shape[1], int(math.ceil(east_px)) - origin_x)
    row_start = max(0, int(math.floor(north_px)) - origin_y)
    row_stop = min(mosaic.shape[0], int(math.ceil(south_px)) - origin_y)
    if column_stop - column_start < 3 or row_stop - row_start < 3:
        _fail(
            f"extent resolves to only {column_stop - column_start}x"
            f"{row_stop - row_start} pixels at zoom {zoom}; increase --zoom"
        )
    values = mosaic[row_start:row_stop, column_start:column_stop].copy()
    global_columns = (
        origin_x + column_start + np.arange(values.shape[1], dtype=np.float64) + 0.5
    )
    global_rows = (
        origin_y + row_start + np.arange(values.shape[0], dtype=np.float64) + 0.5
    )
    world_pixels = float(TILE_PIXELS * (1 << zoom))
    metres_per_pixel = 2.0 * WEB_MERCATOR_HALF_WORLD_M / world_pixels
    x_coordinates = -WEB_MERCATOR_HALF_WORLD_M + global_columns * metres_per_pixel
    y_coordinates = WEB_MERCATOR_HALF_WORLD_M - global_rows * metres_per_pixel
    # Derivation helpers require monotonically increasing axes.
    if y_coordinates[0] > y_coordinates[-1]:
        y_coordinates = y_coordinates[::-1].copy()
        values = values[::-1, :].copy()
    valid_cells = int(np.isfinite(values).sum())
    if valid_cells < 9:
        _fail(
            "Terrarium crop has fewer than nine land/near-shore cells in the "
            "legacy -500..9000 metre relief range"
        )
    window_sha256 = _window_digest(
        values,
        x_coordinates,
        y_coordinates,
        extent=extent,
        zoom=zoom,
        tile_records=tile_records,
    )
    return values, x_coordinates, y_coordinates, tile_records, window_sha256


def _format_id(record: dict[str, Any]) -> str:
    composition = record.get("composition")
    candidate = composition.get("format_id") if isinstance(composition, dict) else None
    candidate = candidate or record.get("format_id") or "a5-portrait"
    value = str(candidate)
    if value not in {"a5-portrait", "a5-landscape"}:
        _fail(f"{record.get('id')}: global terrain derivation requires an A5 format")
    return value


def _north_up_working_record(record: dict[str, Any]) -> dict[str, Any]:
    subject_id = str(record.get("id") or "<unknown>")
    context = record.get("context")
    if not isinstance(context, dict):
        _fail(f"{subject_id}: context must be an object")
    try:
        rotation = float(context.get("rotation_deg", 0.0))
    except (TypeError, ValueError):
        _fail(f"{subject_id}: rotation_deg must be finite and numeric")
    if not math.isfinite(rotation) or abs(rotation) > 1e-9:
        _fail(
            f"{subject_id}: terrain derivation is north-up only; rotation_deg must be 0"
        )
    orientation = context.get("orientation_status")
    if orientation not in {None, "north-up"}:
        _fail(f"{subject_id}: orientation_status must be 'north-up'")
    route = record.get("route")
    if not isinstance(route, dict) or not isinstance(route.get("segments"), list):
        _fail(f"{subject_id}: route.segments must be an array")
    working = copy.deepcopy(record)
    composition = working.get("composition")
    if composition is None:
        composition = {}
        working["composition"] = composition
    if not isinstance(composition, dict):
        _fail(f"{subject_id}: composition must be an object")
    composition["format_id"] = _format_id(record)
    working["context"]["rotation_deg"] = 0.0
    working["context"]["orientation_status"] = "north-up"
    if working.get("subject_kind") == "route_plate":
        bind_aspect_expanded_map_extent(working, has_profile=True)
    return working


def _sample_grid(
    longitude: float,
    latitude: float,
    *,
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    forward: Transformer,
    label: str,
    minimum_valid_elevation_m: float | None = None,
) -> tuple[float, str]:
    x, y = forward.transform(longitude, latitude)
    x = min(max(float(x), float(x_coordinates[0])), float(x_coordinates[-1]))
    y = min(max(float(y), float(y_coordinates[0])), float(y_coordinates[-1]))
    sampled = _bilinear_sample(values, x_coordinates, y_coordinates, x, y)
    sampled_is_eligible = (
        sampled is not None
        and math.isfinite(sampled)
        and (minimum_valid_elevation_m is None or sampled >= minimum_valid_elevation_m)
    )
    if not sampled_is_eligible:
        column = int(np.argmin(np.abs(x_coordinates - x)))
        row = int(np.argmin(np.abs(y_coordinates - y)))
        radius = TERRESTRIAL_FALLBACK_RADIUS_PIXELS
        row_start = max(0, row - radius)
        row_stop = min(values.shape[0], row + radius + 1)
        column_start = max(0, column - radius)
        column_stop = min(values.shape[1], column + radius + 1)
        neighbourhood = values[row_start:row_stop, column_start:column_stop]
        eligible = np.isfinite(neighbourhood)
        if minimum_valid_elevation_m is not None:
            eligible &= neighbourhood >= minimum_valid_elevation_m
        valid_rows, valid_columns = np.nonzero(eligible)
        if valid_rows.size:
            candidates = [
                (
                    math.hypot(
                        float(x_coordinates[column_start + local_column]) - x,
                        float(y_coordinates[row_start + local_row]) - y,
                    ),
                    float(neighbourhood[local_row, local_column]),
                )
                for local_row, local_column in zip(
                    valid_rows.tolist(), valid_columns.tolist(), strict=True
                )
            ]
            sampled = min(candidates, key=lambda item: item[0])[1]
            return float(sampled), "nearest-eligible-source-cell"
    if (
        sampled is None
        or not math.isfinite(sampled)
        or (
            minimum_valid_elevation_m is not None
            and sampled < minimum_valid_elevation_m
        )
    ):
        domain = (
            f" at or above {minimum_valid_elevation_m:g} m"
            if minimum_valid_elevation_m is not None
            else ""
        )
        _fail(f"could not sample a valid Terrarium elevation{domain} for {label}")
    return float(sampled), "bilinear-source-grid"


def _sample_route_and_peaks(
    record: dict[str, Any],
    *,
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    source_id: str,
    forward: Transformer,
    extent: tuple[float, float, float, float],
) -> tuple[RouteSamplingSummary, int]:
    subject_id = str(record["id"])
    west, south, east, north = extent

    def checked_point(raw: Any, label: str) -> tuple[float, float]:
        if not isinstance(raw, list) or len(raw) not in {2, 3}:
            _fail(f"{label} must be [lon, lat] or [lon, lat, elevation]")
        try:
            longitude, latitude = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            _fail(f"{label} longitude/latitude must be numeric")
        if not all(math.isfinite(value) for value in (longitude, latitude)):
            _fail(f"{label} longitude/latitude must be finite")
        if not (west <= longitude <= east and south <= latitude <= north):
            _fail(f"{label} lies outside context.extent")
        return longitude, latitude

    route_points = 0
    terrestrial_source_samples = 0
    terrestrial_fallbacks = 0
    ferry_reference_points = 0
    ferry_segments = 0
    for segment_index, segment in enumerate(record["route"]["segments"]):
        points = segment.get("points") if isinstance(segment, dict) else None
        if not isinstance(points, list) or len(points) < 2:
            _fail(f"{subject_id}: route segment {segment_index} has invalid points")
        mode = segment.get("mode")
        if mode not in {"walk", "alternate", "ferry"}:
            _fail(
                f"{subject_id}: route segment {segment_index} has invalid mode {mode!r}"
            )
        sampled_points: list[list[float]] = []
        segment_fallbacks = 0
        for point_index, point in enumerate(points):
            longitude, latitude = checked_point(
                point,
                f"{subject_id}: route point {segment_index}/{point_index}",
            )
            if mode == "ferry":
                # A ferry is a sea-surface connector, not terrestrial route
                # geometry.  Do not sample bathymetry and do not search for a
                # potentially kilometres-distant land cell.  The zero is an
                # explicit nominal reference, never a clamped DEM observation.
                elevation = FERRY_SEA_SURFACE_REFERENCE_M
                ferry_reference_points += 1
            else:
                elevation, sampling_method = _sample_grid(
                    longitude,
                    latitude,
                    values=values,
                    x_coordinates=x_coordinates,
                    y_coordinates=y_coordinates,
                    forward=forward,
                    label=f"{subject_id} route point {segment_index}/{point_index}",
                    minimum_valid_elevation_m=TERRESTRIAL_MINIMUM_ELEVATION_M,
                )
                used_fallback = sampling_method == "nearest-eligible-source-cell"
                terrestrial_fallbacks += int(used_fallback)
                segment_fallbacks += int(used_fallback)
                terrestrial_source_samples += 1
            sampled_points.append([longitude, latitude, round(elevation, 1)])
            route_points += 1
        segment["points"] = sampled_points
        if mode == "ferry":
            ferry_segments += 1
            segment["elevation_sampling_policy"] = {
                "id": FERRY_SEA_SURFACE_POLICY_ID,
                "datum": FERRY_SEA_SURFACE_DATUM,
                "reference_elevation_m": FERRY_SEA_SURFACE_REFERENCE_M,
                "point_count": len(sampled_points),
                "terrarium_source_sample_count": 0,
                "nearest_land_fallback_count": 0,
                "bathymetric_value_clamp_count": 0,
            }
        else:
            segment["elevation_sampling_policy"] = {
                "id": TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID,
                "source_ref": source_id,
                "minimum_valid_elevation_m": TERRESTRIAL_MINIMUM_ELEVATION_M,
                "fallback_radius_pixels": TERRESTRIAL_FALLBACK_RADIUS_PIXELS,
                "point_count": len(sampled_points),
                "bilinear_sample_count": len(sampled_points) - segment_fallbacks,
                "nearest_eligible_fallback_count": segment_fallbacks,
                "clamping": False,
            }
    profile_points = 0
    profile_source_samples = 0
    profile_fallbacks = 0
    profile_segments = record["route"].get("profile_segments")
    if profile_segments is not None:
        if not isinstance(profile_segments, list) or not profile_segments:
            _fail(f"{subject_id}: route.profile_segments must be a non-empty array")
        for segment_index, segment in enumerate(profile_segments):
            points = segment.get("points") if isinstance(segment, dict) else None
            if (
                not isinstance(segment, dict)
                or segment.get("mode") != "walk"
                or not isinstance(points, list)
                or len(points) < 2
            ):
                _fail(
                    f"{subject_id}: profile segment {segment_index} must be walking "
                    "source geometry with at least two points"
                )
            sampled_points: list[list[float]] = []
            segment_fallbacks = 0
            for point_index, point in enumerate(points):
                longitude, latitude = checked_point(
                    point,
                    f"{subject_id}: profile point {segment_index}/{point_index}",
                )
                elevation, sampling_method = _sample_grid(
                    longitude,
                    latitude,
                    values=values,
                    x_coordinates=x_coordinates,
                    y_coordinates=y_coordinates,
                    forward=forward,
                    label=(
                        f"{subject_id} profile point {segment_index}/{point_index}"
                    ),
                    minimum_valid_elevation_m=TERRESTRIAL_MINIMUM_ELEVATION_M,
                )
                used_fallback = sampling_method == "nearest-eligible-source-cell"
                profile_fallbacks += int(used_fallback)
                segment_fallbacks += int(used_fallback)
                profile_source_samples += 1
                profile_points += 1
                sampled_points.append([longitude, latitude, round(elevation, 1)])
            segment["points"] = sampled_points
            segment["elevation_sampling_policy"] = {
                "id": TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID,
                "source_ref": source_id,
                "minimum_valid_elevation_m": TERRESTRIAL_MINIMUM_ELEVATION_M,
                "fallback_radius_pixels": TERRESTRIAL_FALLBACK_RADIUS_PIXELS,
                "point_count": len(sampled_points),
                "bilinear_sample_count": len(sampled_points) - segment_fallbacks,
                "nearest_eligible_fallback_count": segment_fallbacks,
                "clamping": False,
            }
        record["route"]["profile_elevation_sampling_policy"] = {
            "id": TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID,
            "source_ref": source_id,
            "source_sampled_point_count": profile_source_samples,
            "bilinear_sample_count": profile_source_samples - profile_fallbacks,
            "nearest_eligible_fallback_count": profile_fallbacks,
            "complete_ordered_profile_geometry": True,
            "clamping": False,
        }

    record["route"]["profile_status"] = "source-elevation-sampled"
    record["route"]["elevation_source_ref"] = source_id
    record["route"]["elevation_method"] = TERRESTRIAL_ROUTE_ELEVATION_METHOD
    record["route"]["elevation_datum"] = "Mapzen composite source vertical datums"
    record["route"]["elevation_sampling_policy"] = {
        "id": (
            MIXED_ROUTE_SAMPLING_POLICY_ID
            if ferry_reference_points
            else TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID
        ),
        "minimum_valid_elevation_m": TERRESTRIAL_MINIMUM_ELEVATION_M,
        "fallback_radius_pixels": TERRESTRIAL_FALLBACK_RADIUS_PIXELS,
        "source_sampled_point_count": terrestrial_source_samples,
        "bilinear_sample_count": terrestrial_source_samples - terrestrial_fallbacks,
        "nearest_eligible_fallback_count": terrestrial_fallbacks,
        "ferry_segment_count": ferry_segments,
        "ferry_sea_surface_reference_point_count": ferry_reference_points,
        "clamping": False,
    }
    if ferry_reference_points:
        record["route"]["elevation_sampling_policy"]["ferry_reference"] = {
            "id": FERRY_SEA_SURFACE_POLICY_ID,
            "datum": FERRY_SEA_SURFACE_DATUM,
            "reference_elevation_m": FERRY_SEA_SURFACE_REFERENCE_M,
            "terrarium_sampling": False,
            "nearest_land_fallback": False,
            "bathymetry_clamping": False,
        }

    explicit_peak_elevation_count = 0
    features = record["context"].get("features", [])
    if not isinstance(features, list):
        _fail(f"{subject_id}: context.features must be an array")
    for feature in features:
        if not isinstance(feature, dict) or feature.get("kind") != "peak":
            continue
        # A named summit height is a property of the summit, not the elevation
        # of whichever low-zoom raster cell happens to contain its coordinate.
        # Preserve an explicit source height (for example an OSM ``ele`` tag),
        # but never manufacture display copy from the global fallback DEM.
        if feature.get("elevation_m") is not None:
            explicit_peak_elevation_count += 1
    return (
        RouteSamplingSummary(
            total_point_count=route_points,
            terrestrial_source_sample_count=terrestrial_source_samples,
            terrestrial_fallback_count=terrestrial_fallbacks,
            ferry_sea_surface_reference_count=ferry_reference_points,
            ferry_segment_count=ferry_segments,
            profile_point_count=profile_points,
            profile_source_sample_count=profile_source_samples,
            profile_fallback_count=profile_fallbacks,
        ),
        explicit_peak_elevation_count,
    )


def _nice_interval(elevation_range_m: float) -> int:
    target = max(elevation_range_m / GLOBAL_CONTOUR_TARGET_LEVELS, 5.0)
    for interval in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1_000, 2_000):
        if interval >= target:
            return interval
    return 2_000


def _contour_levels(values: np.ndarray) -> tuple[int, ...]:
    finite = values[np.isfinite(values)]
    if finite.size < 9:
        _fail("terrain crop has too few valid samples for contours")
    minimum = float(np.percentile(finite, 2.0))
    maximum = float(np.percentile(finite, 98.0))
    interval = _nice_interval(maximum - minimum)
    first = int(math.ceil(minimum / interval) * interval)
    last = int(math.floor(maximum / interval) * interval)
    levels = tuple(range(first, last + 1, interval))
    if len(levels) > GLOBAL_CONTOUR_MAXIMUM_LEVELS:
        stride = int(math.ceil(len(levels) / GLOBAL_CONTOUR_MAXIMUM_LEVELS))
        levels = levels[::stride]
    if not levels:
        midpoint = int(round((minimum + maximum) / 2.0))
        levels = (midpoint,)
    return levels


def _derive_contours(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    *,
    extent: tuple[float, float, float, float],
    record: dict[str, Any],
    forward: Transformer,
    inverse: Transformer,
) -> tuple[list[dict[str, Any]], dict[str, Any], tuple[int, ...]]:
    levels = _contour_levels(values)
    west, south, east, north = extent
    southwest = forward.transform(west, south)
    northeast = forward.transform(east, north)
    crop = box(
        min(southwest[0], northeast[0]),
        min(southwest[1], northeast[1]),
        max(southwest[0], northeast[0]),
        max(southwest[1], northeast[1]),
    )
    route_lines = [
        LineString([(float(point[0]), float(point[1])) for point in segment["points"]])
        for segment in record["route"]["segments"]
    ]
    route = unary_union(route_lines)
    route = transform(forward.transform, route)
    pixel_m = max(
        abs(float(np.median(np.diff(x_coordinates)))),
        abs(float(np.median(np.diff(y_coordinates)))),
    )
    contours = _contour_paths(
        values,
        x_coordinates,
        y_coordinates,
        levels_m=levels,
        caps=tuple(GLOBAL_CONTOUR_PATHS_PER_LEVEL for _ in levels),
        crop=crop,
        route=route,
        inverse=inverse,
        simplify_m=max(pixel_m * 0.65, 1.0),
        minimum_length_m=max(
            pixel_m * 2.5,
            math.hypot(crop.bounds[2] - crop.bounds[0], crop.bounds[3] - crop.bounds[1])
            * 0.015,
        ),
        maximum_route_distance_m=None,
        minimum_separation_m=0.0,
        maximum_path_span_m=None,
        maximum_path_length_m=None,
        exclusion=None,
        prefer_broad=False,
        anchors=(),
        anchor_cluster_radius_m=0.0,
        anchor_cluster_minimum_paths=2,
        anchor_cluster_maximum_paths=4,
    )
    page_transform = _page_transform_for_record(record)
    contours, page_policy = _filter_contours_page_equivalent(
        contours,
        page_transform=page_transform,
        minimum_page_mm=GLOBAL_CONTOUR_MINIMUM_PAGE_MM,
        maximum_paths=GLOBAL_CONTOUR_MAXIMUM_PATHS,
        maximum_total_mm=GLOBAL_CONTOUR_MAXIMUM_TOTAL_MM,
    )
    if not contours:
        _fail("no contours survived the physical 8 mm A5 floor")
    for contour in contours:
        contour["geometry_sha256"] = _canonical_sha256(contour["paths"])
    return contours, page_policy, levels


def _source_id(zoom: int) -> str:
    return f"{SOURCE_ID_PREFIX}-z{zoom}"


def _upsert_source(
    record: dict[str, Any],
    *,
    source_id: str,
    retrieved_at: str,
    zoom: int,
    tile_records: Sequence[dict[str, Any]],
    source_window_sha256: str,
    window_sha256: str,
    source_valid_fraction: float,
    valid_fraction: float,
    pixel_size_m: float,
    route_sampling: RouteSamplingSummary,
    route_extent: Sequence[float],
    map_extent: Sequence[float],
    map_extent_binding_sha256: str,
) -> None:
    sources = record.setdefault("sources", [])
    if not isinstance(sources, list):
        _fail(f"{record.get('id')}: sources must be an array")
    sources[:] = [
        item
        for item in sources
        if not isinstance(item, dict) or item.get("id") != source_id
    ]
    tile_manifest = [
        {
            "z": item["z"],
            "x": item["x"],
            "y": item["y"],
            "url": item["url"],
            "sha256": item["sha256"],
        }
        for item in tile_records
    ]
    uses_ferry_reference = route_sampling.ferry_sea_surface_reference_count > 0
    sources.append(
        {
            "id": source_id,
            "publisher": TERRARIUM_PUBLISHER,
            "url": TERRARIUM_PRODUCT_URL,
            "license": TERRARIUM_LICENSE,
            "attribution": TERRARIUM_ATTRIBUTION,
            "attribution_requirements_url": TERRARIUM_ATTRIBUTION_URL,
            "provider_attribution_review_required": True,
            "use": (
                "cached Terrarium DEM tiles; sampled route/peak elevations, "
                "selected elevation contours, and frozen DEM-gradient fall lines; "
                "ferry route points use an explicit non-DEM 0 m sea-surface "
                "reference"
                if uses_ferry_reference
                else "cached Terrarium DEM tiles; sampled route/peak elevations, "
                "selected elevation contours, and frozen DEM-gradient fall lines"
            ),
            "retrieved_at": retrieved_at,
            "source_raster_url": TERRARIUM_URL_TEMPLATE,
            "encoding": "terrarium-rgb-r256-plus-g-plus-b-div-256-minus-32768",
            "point_sampling": (
                MIXED_ROUTE_SAMPLING_POLICY_ID
                if uses_ferry_reference
                else "terrestrial-bilinear-or-nearest-nonnegative-within-3-pixels-v1"
            ),
            "point_sampling_minimum_elevation_m": TERRESTRIAL_MINIMUM_ELEVATION_M,
            "point_sampling_clamps_values": False,
            "point_sampling_terrarium_source_sample_count": (
                route_sampling.terrestrial_source_sample_count
            ),
            "profile_point_sampling_terrarium_source_sample_count": (
                route_sampling.profile_source_sample_count
            ),
            "profile_point_sampling_nearest_nonnegative_fallback_count": (
                route_sampling.profile_fallback_count
            ),
            "point_sampling_ferry_sea_surface_reference_count": (
                route_sampling.ferry_sea_surface_reference_count
            ),
            "zoom": zoom,
            "horizontal_crs": "EPSG:3857",
            "vertical_datum": "Mapzen composite source vertical datums",
            "derived_grid_resolution_m": round(pixel_size_m, 6),
            "route_extent": list(route_extent),
            "source_window_extent": list(map_extent),
            "map_extent_binding_sha256": map_extent_binding_sha256,
            "source_window_sha256": source_window_sha256,
            "source_window_valid_fraction": round(source_valid_fraction, 6),
            "derived_window_sha256": window_sha256,
            "derived_window_valid_fraction": round(valid_fraction, 6),
            "derived_surface_domain": "terrestrial-nonnegative-source-cells-v1",
            "tile_count": len(tile_manifest),
            "tile_manifest_sha256": _canonical_sha256(tile_manifest),
            "tiles": tile_manifest,
        }
    )


def _referenced_source_ids(value: Any) -> set[str]:
    """Collect explicit source-reference fields, excluding the source register."""

    references: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "sources":
                continue
            if (key == "source_ref" or key.endswith("_source_ref")) and isinstance(
                child, str
            ):
                references.add(child)
            else:
                references.update(_referenced_source_ids(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_referenced_source_ids(child))
    return references


def _replaced_terrain_source(source: dict[str, Any]) -> bool:
    use = str(source.get("use") or "").casefold()
    return any(token in use for token in ("terrain", "contour", "dem", "dtm"))


def _prune_replaced_terrain_sources(
    record: dict[str, Any],
    *,
    current_source_id: str,
) -> list[str]:
    """Drop superseded DEM/contour records once no final geometry cites them."""

    sources = record.get("sources")
    if not isinstance(sources, list):
        _fail(f"{record.get('id')}: sources must be an array")
    referenced = _referenced_source_ids(record)
    retained: list[Any] = []
    removed: list[str] = []
    for source in sources:
        source_id = str(source.get("id") or "") if isinstance(source, dict) else ""
        if (
            isinstance(source, dict)
            and source_id
            and source_id != current_source_id
            and source_id not in referenced
            and _replaced_terrain_source(source)
        ):
            removed.append(source_id)
        else:
            retained.append(source)
    sources[:] = retained
    return sorted(set(removed))


def _visible_release_credit(record: dict[str, Any]) -> str:
    """Compose at most two plotted lines from the sources used by final geometry."""

    sources = record.get("sources")
    route = record.get("route")
    if not isinstance(sources, list) or not isinstance(route, dict):
        _fail(f"{record.get('id')}: cannot compose visible source credit")
    by_id = {
        str(source.get("id")): source
        for source in sources
        if isinstance(source, dict) and source.get("id")
    }
    route_source_ref = str(route.get("source_ref") or "")
    route_source = by_id.get(route_source_ref)
    if route_source is None:
        _fail(f"{record.get('id')}: route source is absent from source register")
    route_license = str(route_source.get("license") or "")
    if route_license.upper().startswith("ODBL"):
        return VISIBLE_OSM_TERRAIN_CREDIT
    route_attribution = str(route_source.get("attribution") or "").strip()
    if not route_attribution:
        _fail(f"{record.get('id')}: non-ODbL route source lacks attribution copy")
    words = route_attribution.split()
    if len(route_attribution) <= 52 or len(words) < 2:
        route_lines = [route_attribution]
    else:
        split_index = min(
            range(1, len(words)),
            key=lambda index: abs(
                len(" ".join(words[:index])) - len(" ".join(words[index:]))
            ),
        )
        route_lines = [
            " ".join(words[:split_index]),
            " ".join(words[split_index:]),
        ]
    return " | ".join([*route_lines, VISIBLE_OSM_TERRAIN_CREDIT])


def derive_record(
    record: dict[str, Any],
    *,
    zoom: int,
    cache_dir: Path,
    retrieved_at: str,
    fetcher: TileFetcher = _download_tile,
) -> dict[str, Any]:
    """Return a copy of one record with frozen global terrain evidence."""

    working = _north_up_working_record(record)
    subject_id = str(working.get("id") or "<unknown>")
    extent = _validate_extent(working["context"].get("extent"), subject_id=subject_id)
    route_extent = _validate_extent(
        working["context"].get("route_extent", working["context"].get("extent")),
        subject_id=subject_id,
    )
    map_extent_binding = working["context"].get("map_extent_binding")
    if not isinstance(map_extent_binding, dict):
        map_extent_binding = {
            "policy_id": "legacy-context-extent-v1",
            "north_up": True,
            "route_extent": list(route_extent),
            "map_extent": list(extent),
        }
    map_extent_binding_sha256 = _canonical_sha256(map_extent_binding)
    values, x_coordinates, y_coordinates, tiles, window_sha256 = _load_extent_grid(
        extent,
        zoom=zoom,
        cache_dir=cache_dir,
        fetcher=fetcher,
    )
    source_window_sha256 = window_sha256
    surface_values = np.where(
        np.isfinite(values) & (values >= TERRESTRIAL_MINIMUM_ELEVATION_M),
        values,
        np.nan,
    )
    source_valid_fraction = float(np.isfinite(values).sum()) / float(values.size)
    surface_valid_fraction = float(np.isfinite(surface_values).sum()) / float(
        surface_values.size
    )
    if int(np.isfinite(surface_values).sum()) < 9:
        _fail(f"{subject_id}: fewer than nine non-bathymetric terrain samples")
    surface_window_sha256 = _window_digest(
        surface_values,
        x_coordinates,
        y_coordinates,
        extent=extent,
        zoom=zoom,
        tile_records=tiles,
    )
    source_id = _source_id(zoom)
    forward = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    inverse = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    route_sampling, explicit_peak_elevation_count = _sample_route_and_peaks(
        working,
        values=values,
        x_coordinates=x_coordinates,
        y_coordinates=y_coordinates,
        source_id=source_id,
        forward=forward,
        extent=extent,
    )
    try:
        contours, contour_page_policy, levels = _derive_contours(
            surface_values,
            x_coordinates,
            y_coordinates,
            extent=extent,
            record=working,
            forward=forward,
            inverse=inverse,
        )
    except SystemExit as exc:
        _fail(f"{subject_id}: contour derivation failed: {exc.code}")
    pixel_size_m = max(
        abs(float(np.median(np.diff(x_coordinates)))),
        abs(float(np.median(np.diff(y_coordinates)))),
    )
    relief_config = RasterTerrainConfig(
        source_id=source_id,
        source_url=TERRARIUM_URL_TEMPLATE,
        publisher=TERRARIUM_PUBLISHER,
        license=TERRARIUM_LICENSE,
        attribution=TERRARIUM_ATTRIBUTION,
        product_url=TERRARIUM_PRODUCT_URL,
        source_release=f"AWS Open Data cache retrieved {retrieved_at}",
        source_resolution_m=pixel_size_m,
        vertical_datum="Mapzen composite source vertical datums",
        target_resolution_m=pixel_size_m,
        levels_m=levels,
        caps=tuple(GLOBAL_CONTOUR_PATHS_PER_LEVEL for _ in levels),
        simplify_m=max(pixel_size_m * 0.65, 1.0),
        minimum_length_m=max(pixel_size_m * 2.5, 1.0),
        credit_line=TERRARIUM_ATTRIBUTION,
        fall_line_seed_minimum_elevation_m=TERRESTRIAL_MINIMUM_ELEVATION_M,
        fall_line_seed_slope_policy="global-page-smoothed-adaptive-v1",
    )
    try:
        relief_strokes, relief_policy = _derive_relief_strokes(
            surface_values,
            x_coordinates,
            y_coordinates,
            record=working,
            config=relief_config,
            forward=forward,
            inverse=inverse,
            window_sha256=surface_window_sha256,
        )
    except SystemExit as exc:
        _fail(f"{subject_id}: fall-line derivation failed: {exc.code}")
    # The adaptive threshold operates only on the page-smoothed, terrestrial
    # DEM.  A genuinely flat walk can still truthfully emit no fall lines;
    # contours and the elevation profile remain the factual relief evidence.
    valid_fraction = surface_valid_fraction
    working["context"]["terrain"] = {
        "status": "source-derived-dtm-relief",
        "source_ref": source_id,
        "derivation_id": f"global-terrarium-{subject_id.casefold()}-relief-v{DERIVATION_VERSION}",
        "source_crs": "EPSG:3857",
        "vertical_datum": "Mapzen composite source vertical datums",
        "source_grid_resolution_m": round(pixel_size_m, 6),
        "derived_grid_resolution_m": round(pixel_size_m, 6),
        "route_extent": list(route_extent),
        "source_window_extent": list(extent),
        "map_extent_binding_sha256": map_extent_binding_sha256,
        "source_window_sha256": source_window_sha256,
        "derived_window_sha256": surface_window_sha256,
        "derived_window_valid_fraction": round(valid_fraction, 6),
        "surface_domain_policy": {
            "id": "terrestrial-nonnegative-source-cells-v1",
            "minimum_elevation_m": TERRESTRIAL_MINIMUM_ELEVATION_M,
            "bathymetric_cell_count": int(
                (np.isfinite(values) & (values < TERRESTRIAL_MINIMUM_ELEVATION_M)).sum()
            ),
            "elevation_values_clamped": False,
            "applies_to": [
                "contours",
                "fall-lines",
                "terrestrial-route-point-sampling",
            ],
        },
        "contour_levels_m": list(levels),
        "simplification_tolerance_m": {
            "contours": round(max(pixel_size_m * 0.65, 1.0), 6)
        },
        "contour_selection_policy": {
            "geometry_policy": "whole-source-contour-selection-no-invented-links-v1",
            "page_equivalent_filter": contour_page_policy,
            "north_up": True,
        },
        "relief_algorithm_id": RELIEF_ALGORITHM_ID,
        "relief_strokes": relief_strokes,
        "relief_stroke_policy": relief_policy,
        "relief_geometry_manifest_sha256": relief_policy["geometry_manifest_sha256"],
        "areas": [],
        "elevation_masks": [],
        "contours": contours,
        "source_tile_manifest_sha256": _canonical_sha256(
            [
                {
                    "z": tile["z"],
                    "x": tile["x"],
                    "y": tile["y"],
                    "sha256": tile["sha256"],
                }
                for tile in tiles
            ]
        ),
    }
    backdrop = working.setdefault("backdrop", {})
    if isinstance(backdrop, dict):
        backdrop["status"] = "source-derived"
        backdrop["terrain"] = "source-derived-dtm-relief"
    _upsert_source(
        working,
        source_id=source_id,
        retrieved_at=retrieved_at,
        zoom=zoom,
        tile_records=tiles,
        source_window_sha256=source_window_sha256,
        window_sha256=surface_window_sha256,
        source_valid_fraction=source_valid_fraction,
        valid_fraction=valid_fraction,
        pixel_size_m=pixel_size_m,
        route_sampling=route_sampling,
        route_extent=route_extent,
        map_extent=extent,
        map_extent_binding_sha256=map_extent_binding_sha256,
    )
    prior_derivation = working.get("terrain_derivation")
    prior_global = (
        prior_derivation.get("global_terrarium")
        if isinstance(prior_derivation, dict)
        else None
    )
    prior_replaced_sources = (
        prior_global.get("replaced_terrain_source_ids")
        if isinstance(prior_global, dict)
        else []
    )
    removed_sources = _prune_replaced_terrain_sources(
        working,
        current_source_id=source_id,
    )
    replaced_source_ids = sorted(
        {
            *(str(item) for item in prior_replaced_sources or []),
            *removed_sources,
        }
    )
    working["credit_line"] = _visible_release_credit(working)
    working["rights_status"] = "review-required"
    notes = working.setdefault("notes", [])
    if isinstance(notes, list):
        review_note = (
            "Commercial use remains blocked pending location-specific Mapzen "
            "terrain-provider attribution and rights review."
        )
        if review_note not in notes:
            notes.append(review_note)
    working.setdefault("terrain_derivation", {})["global_terrarium"] = {
        "route_points_total": route_sampling.total_point_count,
        "route_points_sampled": route_sampling.terrestrial_source_sample_count,
        "profile_points_total": route_sampling.profile_point_count,
        "profile_points_sampled": route_sampling.profile_source_sample_count,
        "profile_points_nearest_nonnegative_fallback": (
            route_sampling.profile_fallback_count
        ),
        "peaks_sampled": 0,
        "explicit_peak_elevations_preserved": explicit_peak_elevation_count,
        "named_peak_elevation_policy": "explicit-source-only-v1",
        "route_points_nearest_nonnegative_fallback": (
            route_sampling.terrestrial_fallback_count
        ),
        "route_points_sea_surface_referenced": (
            route_sampling.ferry_sea_surface_reference_count
        ),
        "ferry_segments_sea_surface_referenced": route_sampling.ferry_segment_count,
        "contour_paths": sum(len(item["paths"]) for item in contours),
        "fall_lines": len(relief_strokes),
        "zoom": zoom,
        "north_up": True,
        "route_extent": list(route_extent),
        "map_extent": list(extent),
        "map_extent_binding_sha256": map_extent_binding_sha256,
        "source_window_sha256": source_window_sha256,
        "derived_window_sha256": surface_window_sha256,
        "replaced_terrain_source_ids": replaced_source_ids,
    }
    return working


def derive_catalog(
    catalog: dict[str, Any],
    *,
    subjects: Sequence[str] | None,
    zoom: int,
    auto_zoom: bool = False,
    cache_dir: Path,
    retrieved_at: str,
    fetcher: TileFetcher = _download_tile,
) -> dict[str, Any]:
    """Return a catalog copy with selected records replaced by derived copies."""

    result = copy.deepcopy(catalog)
    records = result.get("plates")
    if not isinstance(records, list):
        _fail("catalog must contain a plates array")
    identifiers = [
        str(record.get("id"))
        for record in records
        if isinstance(record, dict) and record.get("id") is not None
    ]
    if len(identifiers) != len(set(identifiers)):
        _fail("catalog repeats a plate ID")
    by_id = {
        str(record.get("id")): record
        for record in records
        if isinstance(record, dict) and record.get("id") is not None
    }
    wanted = list(subjects or by_id)
    if len(wanted) != len(set(wanted)):
        _fail("--subject values must be unique")
    missing = sorted(set(wanted) - set(by_id))
    if missing:
        _fail(f"catalog has no subject(s): {', '.join(missing)}")
    if not isinstance(retrieved_at, str) or not retrieved_at.strip():
        _fail("retrieved_at must be a non-empty source timestamp")
    replacements: dict[str, dict[str, Any]] = {}
    for subject_id in wanted:
        selected_zoom = (
            _automatic_zoom(by_id[subject_id], maximum_zoom=zoom) if auto_zoom else zoom
        )
        replacements[subject_id] = derive_record(
            by_id[subject_id],
            zoom=selected_zoom,
            cache_dir=cache_dir,
            retrieved_at=retrieved_at,
            fetcher=fetcher,
        )
    result["plates"] = [
        replacements.get(str(record.get("id")), record)
        if isinstance(record, dict)
        else record
        for record in records
    ]
    return result


def _load_catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"could not read catalog {path}: {exc}")
    if not isinstance(value, dict):
        _fail("catalog root must be an object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _fail(f"could not write {path}: {exc}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", action="append", help="Route ID; repeat as needed")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--zoom", type=int, default=DEFAULT_ZOOM)
    parser.add_argument(
        "--auto-zoom",
        action="store_true",
        help=(
            "Use --zoom as a maximum and lower it for very large route extents "
            "so no subject needs more than the A5 terrain tile target."
        ),
    )
    parser.add_argument("--retrieved-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog = _load_catalog(args.catalog)
        result = derive_catalog(
            catalog,
            subjects=args.subject,
            zoom=args.zoom,
            auto_zoom=args.auto_zoom,
            cache_dir=args.cache_dir,
            retrieved_at=args.retrieved_at,
        )
        _write_json_atomic(args.output, result)
    except (GlobalTerrainError, OSError) as exc:
        print(f"derive_hiking_global_terrain: {exc}", file=sys.stderr)
        return 2
    count = len(args.subject or result.get("plates", []))
    print(f"Derived global north-up terrain for {count} route(s) -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
