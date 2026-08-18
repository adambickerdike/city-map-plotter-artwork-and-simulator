#!/usr/bin/env python3
"""Derive Camino overview contours from a hashed CNIG MDT25 tile corridor."""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, NoReturn, Sequence

import contourpy  # type: ignore[import-not-found]
import numpy as np
import rasterio  # type: ignore[import-untyped]
from pyproj import Transformer  # type: ignore[import-not-found]
from rasterio.enums import Resampling  # type: ignore[import-untyped]
from rasterio.merge import merge  # type: ignore[import-untyped]
from shapely.geometry import LineString, Polygon
from shapely.ops import transform
from shapely.validation import make_valid

from derive_hiking_terrain_context import (
    _geometry_lines,
    _load_object,
    _overlay,
    _record,
    _route_geometry,
    densified_bbox_polygon,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-v1.json"
DEFAULT_BUNDLE = ROOT / "src" / "city_map_plotter" / "data" / "hike-context-v3.json"
SUBJECT_ID = "RTE-ES-CAM-ES01C"
SOURCE_ID = "ign-es-mdt25-cob1-camino-corridor"
PRODUCT_URL = (
    "https://centrodedescargas.cnig.es/CentroDescargas/"
    "modelo-digital-terreno-mdt25-primera-cobertura"
)
LEVELS_M = (400, 600, 800, 1_000, 1_200, 1_400, 1_600)
CAPS = (5, 6, 6, 7, 6, 5, 2)
MAXIMUM_CONTOUR_PATHS = 37
TARGET_RESOLUTION_M = 100.0
SIMPLIFICATION_M = 300.0
MINIMUM_LENGTH_M = 15_000.0

if sum(CAPS) != MAXIMUM_CONTOUR_PATHS:
    raise RuntimeError("Camino contour caps must total the 37-path A5 contract")


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"derive_hiking_cnig_terrain_context: {message}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verified_tiles(manifest_path: Path) -> tuple[dict[str, Any], list[Path]]:
    manifest = _load_object(manifest_path)
    if manifest.get("subject_id") != SUBJECT_ID:
        _fail(f"manifest must target {SUBJECT_ID}")
    raw_tiles = manifest.get("tiles")
    if not isinstance(raw_tiles, list) or not raw_tiles:
        _fail("manifest has no downloaded tiles")
    # CNIG may return the same western MTN50 sheet in both native UTM 29 and
    # extended UTM 30.  The route-wide mosaic deliberately uses the HU30
    # editions so one metric grid spans the full Camino without a lossy
    # per-sheet warp.
    raw_tiles = [
        tile
        for tile in raw_tiles
        if isinstance(tile, dict) and "_HU30_" in str(tile.get("filename", ""))
    ]
    if not raw_tiles:
        _fail("manifest contains no zone-30-extended MDT25 tiles")
    paths: list[Path] = []
    for index, raw_tile in enumerate(raw_tiles):
        if not isinstance(raw_tile, dict):
            _fail(f"manifest tile {index} is invalid")
        filename = raw_tile.get("filename")
        expected_hash = raw_tile.get("sha256")
        if not isinstance(filename, str) or not isinstance(expected_hash, str):
            _fail(f"manifest tile {index} lacks filename/hash")
        path = manifest_path.parent / filename
        if not path.is_file():
            _fail(f"missing MDT25 tile {path}")
        if _sha256(path) != expected_hash:
            _fail(f"MDT25 tile hash mismatch: {path}")
        paths.append(path)
    return manifest, paths


def _mosaic(
    paths: list[Path],
    *,
    geographic_crop: Any,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, float]:
    with ExitStack() as stack:
        datasets = [stack.enter_context(rasterio.open(path)) for path in paths]
        crs_values = {
            dataset.crs.to_string() if dataset.crs is not None else ""
            for dataset in datasets
        }
        if len(crs_values) != 1 or not next(iter(crs_values)):
            _fail(f"MDT25 tiles use inconsistent CRS values: {sorted(crs_values)!r}")
        crs = next(iter(crs_values))
        forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        crop = transform(forward.transform, geographic_crop)
        mosaic, affine = merge(
            datasets,
            bounds=crop.bounds,
            res=TARGET_RESOLUTION_M,
            nodata=np.nan,
            masked=True,
            resampling=Resampling.bilinear,
            method="first",
        )
    values = np.asarray(mosaic[0].filled(np.nan), dtype=np.float32)
    values[(values < -500.0) | (values > 9_000.0)] = np.nan
    x_coordinates = float(affine.c) + (
        np.arange(values.shape[1], dtype=np.float64) + 0.5
    ) * float(affine.a)
    y_coordinates = float(affine.f) + (
        np.arange(values.shape[0], dtype=np.float64) + 0.5
    ) * float(affine.e)
    valid_fraction = float(np.isfinite(values).sum()) / float(values.size)
    return values, x_coordinates, y_coordinates, crs, valid_fraction


def _complete_contour_paths(
    values: np.ndarray,
    x_coordinates: np.ndarray,
    y_coordinates: np.ndarray,
    *,
    levels_m: Sequence[int],
    caps: Sequence[int],
    crop: Polygon,
    route: Any,
    inverse: Transformer,
) -> list[dict[str, Any]]:
    """Select complete relief forms, never lines cut by corridor tile edges.

    The acquired MDT25 evidence is a route corridor rather than a wall-to-wall
    northern-Spain raster.  Open contours at the edge of that evidence made the
    old artwork expose the acquisition footprint as a jagged grey band.  Closed
    source contours remain honest elevation isolines and read as coherent hill
    forms without pretending that the missing surrounding terrain was sampled.
    """

    generator = contourpy.contour_generator(
        x=x_coordinates,
        y=y_coordinates,
        z=np.ma.masked_invalid(values),
        line_type="Separate",
    )
    output: list[dict[str, Any]] = []
    for level, cap in zip(levels_m, caps, strict=True):
        candidates: list[tuple[float, float, LineString]] = []
        for raw_line in generator.lines(float(level)):
            coordinates = np.asarray(raw_line, dtype=np.float64)
            if coordinates.ndim != 2 or coordinates.shape[0] < 2:
                continue
            clipped = make_valid(LineString(coordinates.tolist()).intersection(crop))
            for line in _geometry_lines(clipped):
                if line.length < MINIMUM_LENGTH_M:
                    continue
                simple = line.simplify(SIMPLIFICATION_M, preserve_topology=True)
                for part in _geometry_lines(simple):
                    if part.length < MINIMUM_LENGTH_M or not part.is_ring:
                        continue
                    distance = float(part.distance(route))
                    score = distance - min(float(part.length), 60_000.0) * 0.12
                    candidates.append((score, -float(part.length), part))
        candidates.sort(key=lambda item: (item[0], item[1], item[2].wkb_hex))
        selected = candidates[:cap]
        paths: list[list[list[float]]] = []
        total_length_m = 0.0
        for _, _, line in selected:
            total_length_m += float(line.length)
            geographic = transform(inverse.transform, line)
            path = [
                [round(float(x), 6), round(float(y), 6)] for x, y in geographic.coords
            ]
            deduplicated = [
                point
                for index, point in enumerate(path)
                if index == 0 or point != path[index - 1]
            ]
            if len(deduplicated) >= 4:
                if deduplicated[-1] != deduplicated[0]:
                    deduplicated.append(deduplicated[0])
                paths.append(deduplicated)
        if paths:
            output.append(
                {
                    "elevation_m": level,
                    "paths": paths,
                    "selection_rule": (
                        f"route-ranked-complete-{cap}-minimum-{int(MINIMUM_LENGTH_M)}m"
                    ),
                    "derived_total_length_m": round(total_length_m, 1),
                }
            )
    if not output:
        _fail("no complete plot-legible contours survived selection")
    if sum(len(contour["paths"]) for contour in output) > MAXIMUM_CONTOUR_PATHS:
        _fail("complete contour selection exceeded its 37-path A5 contract")
    return output


def derive(
    *,
    manifest_path: Path,
    catalog_path: Path,
    bundle_path: Path,
) -> dict[str, Any]:
    manifest, paths = _verified_tiles(manifest_path)
    catalog = _load_object(catalog_path)
    bundle = _load_object(bundle_path)
    record = _record(catalog, SUBJECT_ID)
    west, south, east, north = (float(value) for value in record["context"]["extent"])
    geographic_crop = densified_bbox_polygon(west, south, east, north)
    values, x_coordinates, y_coordinates, crs, valid_fraction = _mosaic(
        paths,
        geographic_crop=geographic_crop,
    )
    forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    crop = transform(forward.transform, geographic_crop)
    route = _route_geometry(record, forward)
    contours = _complete_contour_paths(
        values,
        x_coordinates,
        y_coordinates,
        levels_m=LEVELS_M,
        caps=CAPS,
        crop=crop,
        route=route,
        inverse=inverse,
    )
    tile_evidence = [
        {
            "catalogue_id": tile["catalogue_id"],
            "filename": tile["filename"],
            "size_bytes": tile["size_bytes"],
            "sha256": tile["sha256"],
        }
        for tile in manifest["tiles"]
        if "_HU30_" in str(tile.get("filename", ""))
    ]
    evidence_sha256 = _canonical_sha256(tile_evidence)
    overlay = _overlay(bundle, SUBJECT_ID)
    sources = overlay.setdefault("sources", [])
    if not isinstance(sources, list):
        _fail("Camino overlay sources must be an array")
    sources[:] = [item for item in sources if item.get("id") != SOURCE_ID]
    sources.append(
        {
            "id": SOURCE_ID,
            "publisher": "Instituto Geográfico Nacional / CNIG España",
            "url": PRODUCT_URL,
            "license": "CC BY 4.0 compatible IGN Spain open-data policy",
            "attribution": ("Obra derivada de MDT25-cob1 2008-2015 CC-BY 4.0 scne.es"),
            "use": ("MDT25 first-coverage route corridor; selected overview contours"),
            "retrieved_at": str(manifest["retrieved_at"]),
            "source_crs": crs,
            "vertical_datum": "orthometric elevations from MDT25 source sheets",
            "source_resolution_m": 25,
            "derived_resolution_m": TARGET_RESOLUTION_M,
            "tile_count": len(tile_evidence),
            "selected_utm_zone": 30,
            "snapshot_sha256": evidence_sha256,
            "tile_evidence_sha256": evidence_sha256,
            "corridor_km": float(manifest["corridor_km"]),
            "coverage_status": "route-corridor-not-full-context-bbox",
        }
    )
    context = overlay.setdefault("context", {})
    context["terrain"] = {
        "status": "source-derived-dtm-relief",
        "source_ref": SOURCE_ID,
        "derivation_id": "cnig-mdt25-camino-corridor-contours-v4",
        "source_crs": crs,
        "vertical_datum": "orthometric elevations from MDT25 source sheets",
        "source_grid_resolution_m": 25,
        "derived_grid_resolution_m": TARGET_RESOLUTION_M,
        "tile_count": len(tile_evidence),
        "selected_utm_zone": 30,
        "tile_evidence_sha256": evidence_sha256,
        "coverage_status": "route-corridor-not-full-context-bbox",
        "derived_window_valid_fraction": round(valid_fraction, 6),
        "contour_levels_m": list(LEVELS_M),
        "contour_selection_policy": "complete-closed-source-isolines",
        "maximum_contour_paths": MAXIMUM_CONTOUR_PATHS,
        "minimum_contour_length_m": MINIMUM_LENGTH_M,
        "simplification_tolerance_m": {"contours": SIMPLIFICATION_M},
        "contour_selection_caps": {
            str(level): cap for level, cap in zip(LEVELS_M, CAPS, strict=True)
        },
        "areas": [],
        "contours": contours,
    }
    backdrop = overlay.setdefault("backdrop", {})
    backdrop["status"] = "source-derived"
    backdrop["terrain"] = "source-derived-dtm-relief"
    overlay["credit_line"] = (
        "MDT25-COB1 © IGN ESPAÑA / CC BY 4.0 | "
        "© OSM CONTRIBUTORS / OPENSTREETMAP.ORG/COPYRIGHT"
    )
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "tiles": len(tile_evidence),
        "levels": len(contours),
        "paths": sum(len(contour["paths"]) for contour in contours),
        "valid_fraction": valid_fraction,
        "minimum_m": float(np.nanmin(values)),
        "maximum_m": float(np.nanmax(values)),
        "evidence_sha256": evidence_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    args = parser.parse_args()
    result = derive(
        manifest_path=args.manifest,
        catalog_path=args.catalog,
        bundle_path=args.bundle,
    )
    print(
        f"{SUBJECT_ID}: tiles={result['tiles']}, levels={result['levels']}, "
        f"paths={result['paths']}, elevation="
        f"{result['minimum_m']:.1f}..{result['maximum_m']:.1f}m, "
        f"valid={result['valid_fraction']:.1%}, "
        f"evidence_sha256={result['evidence_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
