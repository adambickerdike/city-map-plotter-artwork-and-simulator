#!/usr/bin/env python3
"""Freeze plot-legible French hiking contours from an IGN WFS snapshot.

The input is the unmodified GeoJSON response from IGN's public
``ELEVATION.CONTOUR.LINE:courbe`` WFS, requested with ``importance='1'`` for
the exact plate extent.  Geometry is clipped and simplified in Lambert-93,
then reduced to a reviewed, subject-specific artwork interval.  The compact WGS84
derivative and the raw-response digest are written to ``hike-context-v3``.

This produces contextual artwork, not navigation data.  Keep the raw WFS
response outside the repository under a release cache and preserve the hash
recorded by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence
from urllib.parse import urlencode

from pyproj import Transformer  # type: ignore[import-not-found]
from shapely import make_valid
from shapely.geometry import LineString, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-v1.json"
DEFAULT_BUNDLE = ROOT / "src" / "city_map_plotter" / "data" / "hike-context-v3.json"
WFS_ENDPOINT = "https://data.geopf.fr/wfs/ows"
FEATURE_TYPE = "ELEVATION.CONTOUR.LINE:courbe"
SOURCE_PRODUCT_URL = (
    "https://data.geopf.fr/annexes/ressources/documentation/"
    "DC_Courbes_de_niveau_1-0.pdf"
)
SOURCE_ID = "ign-france-contours-wfs-2026-08-03"
SUPPORTED_SUBJECTS = frozenset(
    {"RTE-FR-ECR-976000", "RTE-FR-ECR-995181"}
)
RENDER_INTERVAL_M = {
    "RTE-FR-ECR-976000": 100,
    "RTE-FR-ECR-995181": 300,
}
SOURCE_IMPORTANCE_INTERVAL_M = 25
SIMPLIFY_M = {
    "RTE-FR-ECR-976000": 18.0,
    "RTE-FR-ECR-995181": 24.0,
}
MINIMUM_LENGTH_M = {
    "RTE-FR-ECR-976000": 120.0,
    "RTE-FR-ECR-995181": 220.0,
}


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"derive_hiking_ign_contours: {message}")


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"could not read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail(f"could not hash {path}: {exc}")
    return digest.hexdigest()


def _record(catalog: dict[str, Any], subject_id: str) -> dict[str, Any]:
    for record in catalog.get("plates", []):
        if isinstance(record, dict) and record.get("id") == subject_id:
            return record
    _fail(f"catalog has no plate {subject_id!r}")


def _overlay(bundle: dict[str, Any], subject_id: str) -> dict[str, Any]:
    records = bundle.setdefault("records", [])
    if not isinstance(records, list):
        _fail("context bundle records must be an array")
    for record in records:
        if isinstance(record, dict) and record.get("subject_id") == subject_id:
            return record
    overlay: dict[str, Any] = {
        "subject_id": subject_id,
        "sources": [],
        "context": {},
        "backdrop": {},
    }
    records.append(overlay)
    return overlay


def _lines(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [LineString(geometry.coords)]  # type: ignore[attr-defined]
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        return [
            line
            for child in geometry.geoms  # type: ignore[attr-defined]
            for line in _lines(child)
        ]
    return []


def _extent(record: dict[str, Any]) -> tuple[float, float, float, float]:
    raw = (record.get("context") or {}).get("extent")
    if not isinstance(raw, list) or len(raw) != 4:
        _fail(f"{record.get('id')} has an invalid context extent")
    extent = tuple(float(value) for value in raw)
    if not all(math.isfinite(value) for value in extent):
        _fail(f"{record.get('id')} has a non-finite context extent")
    west, south, east, north = extent
    if not (west < east and south < north):
        _fail(f"{record.get('id')} has an inverted context extent")
    return west, south, east, north


def _request_url(extent: Sequence[float]) -> str:
    west, south, east, north = extent
    parameters = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": FEATURE_TYPE,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "CQL_FILTER": (
            f"BBOX(geom,{west:.6f},{south:.6f},{east:.6f},{north:.6f},"
            "'EPSG:4326') AND importance='1'"
        ),
    }
    return f"{WFS_ENDPOINT}?{urlencode(parameters)}"


def _validated_features(snapshot: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    if snapshot.get("type") != "FeatureCollection":
        _fail(f"{path} is not a GeoJSON FeatureCollection")
    features = snapshot.get("features")
    if not isinstance(features, list) or not features:
        _fail(f"{path} contains no contour features")
    returned = snapshot.get("numberReturned")
    matched = snapshot.get("numberMatched")
    if returned != len(features) or matched != len(features):
        _fail(
            f"{path} is incomplete: matched={matched!r}, returned={returned!r}, "
            f"features={len(features)}"
        )
    crs_name = str(((snapshot.get("crs") or {}).get("properties") or {}).get("name"))
    if crs_name != "urn:ogc:def:crs:EPSG::4326":
        _fail(f"{path} has unexpected CRS {crs_name!r}")
    checked: list[dict[str, Any]] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            _fail(f"{path} feature {index} is not an object")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            _fail(f"{path} feature {index} has no properties")
        if str(properties.get("importance")) != "1":
            _fail(f"{path} feature {index} is not importance=1")
        altitude = properties.get("altitude")
        if (
            isinstance(altitude, bool)
            or not isinstance(altitude, (int, float))
            or not math.isfinite(float(altitude))
        ):
            _fail(f"{path} feature {index} has invalid altitude")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {
            "LineString",
            "MultiLineString",
        }:
            _fail(f"{path} feature {index} has unsupported contour geometry")
        checked.append(feature)
    return checked


def _deduplicated_path(points: Iterable[Sequence[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for x, y in points:
        point = [round(float(x), 6), round(float(y), 6)]
        if not result or point != result[-1]:
            result.append(point)
    return result


def _contours(
    features: Sequence[dict[str, Any]],
    *,
    extent: Sequence[float],
    render_interval_m: int,
    simplify_m: float,
    minimum_length_m: float,
) -> tuple[list[dict[str, Any]], int]:
    forward = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    inverse = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    west, south, east, north = (float(value) for value in extent)
    crop = transform(forward.transform, box(west, south, east, north))
    levels: dict[int, list[tuple[str, LineString]]] = defaultdict(list)
    selected_feature_ids: set[str] = set()
    for feature in features:
        altitude = int(round(float(feature["properties"]["altitude"])))
        if altitude % render_interval_m:
            continue
        source_id = str(feature.get("id") or feature["properties"].get("id") or "")
        projected = transform(forward.transform, shape(feature["geometry"]))
        clipped = make_valid(projected.intersection(crop))
        for line in _lines(clipped):
            simple = line.simplify(simplify_m, preserve_topology=True)
            for part in _lines(simple):
                if part.length < minimum_length_m:
                    continue
                levels[altitude].append((source_id, part))
                selected_feature_ids.add(source_id)

    output: list[dict[str, Any]] = []
    for altitude in sorted(levels):
        paths: list[list[list[float]]] = []
        source_ids: list[str] = []
        total_length_m = 0.0
        for source_id, line in sorted(
            levels[altitude],
            key=lambda item: (-item[1].length, item[0], item[1].wkb_hex),
        ):
            geographic = transform(inverse.transform, line)
            path = _deduplicated_path(geographic.coords)
            if len(path) < 2:
                continue
            paths.append(path)
            source_ids.append(source_id)
            total_length_m += float(line.length)
        if paths:
            output.append(
                {
                    "elevation_m": altitude,
                    "paths": paths,
                    "source_feature_ids": sorted(set(source_ids)),
                    "selection_rule": (
                        f"importance-1-altitude-mod-{render_interval_m}-"
                        f"minimum-{int(minimum_length_m)}m"
                    ),
                    "derived_total_length_m": round(total_length_m, 1),
                }
            )
    if not output:
        _fail("no plot-legible contour geometry survived derivation")
    return output, len(selected_feature_ids)


def derive_subject(
    *,
    subject_id: str,
    snapshot_path: Path,
    catalog: dict[str, Any],
    bundle: dict[str, Any],
    retrieved_at: str,
) -> dict[str, int]:
    if subject_id not in SUPPORTED_SUBJECTS:
        _fail(f"unsupported subject {subject_id!r}")
    record = _record(catalog, subject_id)
    extent = _extent(record)
    snapshot = _load_object(snapshot_path)
    features = _validated_features(snapshot, snapshot_path)
    render_interval_m = RENDER_INTERVAL_M[subject_id]
    contours, selected_feature_count = _contours(
        features,
        extent=extent,
        render_interval_m=render_interval_m,
        simplify_m=SIMPLIFY_M[subject_id],
        minimum_length_m=MINIMUM_LENGTH_M[subject_id],
    )
    snapshot_sha256 = _sha256(snapshot_path)
    overlay = _overlay(bundle, subject_id)
    sources = overlay.setdefault("sources", [])
    if not isinstance(sources, list):
        _fail(f"{subject_id} overlay sources must be an array")
    sources[:] = [source for source in sources if source.get("id") != SOURCE_ID]
    sources.append(
        {
            "id": SOURCE_ID,
            "publisher": "Institut national de l'information géographique et forestière (IGN)",
            "url": SOURCE_PRODUCT_URL,
            "license": "Licence Ouverte 2.0",
            "attribution": (
                "Source: IGN — Courbes de niveau; Licence Ouverte 2.0"
            ),
            "use": (
                "official elevation contours; clipped, projected, simplified, "
                f"and selected at a {render_interval_m} m artwork interval"
            ),
            "retrieved_at": retrieved_at,
            "source_timestamp": str(snapshot.get("timeStamp") or "unknown"),
            "horizontal_crs": "EPSG:4326",
            "vertical_datum": "NGF-IGN69 normal heights",
            "wfs_feature_type": FEATURE_TYPE,
            "wfs_request_url": _request_url(extent),
            "snapshot_sha256": snapshot_sha256,
            "source_feature_count": len(features),
            "selected_feature_count": selected_feature_count,
        }
    )
    context = overlay.setdefault("context", {})
    if not isinstance(context, dict):
        _fail(f"{subject_id} overlay context must be an object")
    context["terrain"] = {
        "status": "source-derived-dtm-relief",
        "source_ref": SOURCE_ID,
        "derivation_id": (
            f"ign-wfs-{subject_id.casefold()}-important-"
            f"{render_interval_m}m-contours-v2"
        ),
        "source_crs": "EPSG:4326",
        "processing_crs": "EPSG:2154",
        "vertical_datum": "NGF-IGN69 normal heights",
        "source_importance_interval_m": SOURCE_IMPORTANCE_INTERVAL_M,
        "rendered_contour_interval_m": render_interval_m,
        "simplification_tolerance_m": {"contours": SIMPLIFY_M[subject_id]},
        "minimum_retained_length_m": MINIMUM_LENGTH_M[subject_id],
        "source_snapshot_sha256": snapshot_sha256,
        "areas": [],
        "contours": contours,
    }
    backdrop = overlay.setdefault("backdrop", {})
    if not isinstance(backdrop, dict):
        _fail(f"{subject_id} overlay backdrop must be an object")
    backdrop["status"] = "source-derived"
    backdrop["terrain"] = "source-derived-dtm-relief"
    overlay["credit_line"] = (
        "IGN LO2 / © OSM CONTRIBUTORS | OPENSTREETMAP.ORG/COPYRIGHT"
    )
    return {
        "source_features": len(features),
        "selected_features": selected_feature_count,
        "levels": len(contours),
        "paths": sum(len(contour["paths"]) for contour in contours),
    }


def _subject_inputs(values: Sequence[str], parser: argparse.ArgumentParser) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        subject_id, separator, raw_path = value.partition("=")
        if separator != "=" or not subject_id or not raw_path:
            parser.error("--input must use SUBJECT_ID=/path/to/snapshot.geojson")
        if subject_id in result:
            parser.error(f"repeated --input subject {subject_id!r}")
        path = Path(raw_path)
        if not path.is_file():
            parser.error(f"snapshot does not exist: {path}")
        result[subject_id] = path
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        metavar="SUBJECT_ID=GEOJSON",
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--retrieved-at", default="2026-08-03T00:00:00Z")
    args = parser.parse_args()
    subject_inputs = _subject_inputs(args.input, parser)
    unknown = set(subject_inputs) - SUPPORTED_SUBJECTS
    if unknown:
        parser.error(f"unsupported subject(s): {', '.join(sorted(unknown))}")
    catalog = _load_object(args.catalog)
    bundle = _load_object(args.bundle)
    for subject_id in sorted(subject_inputs):
        result = derive_subject(
            subject_id=subject_id,
            snapshot_path=subject_inputs[subject_id],
            catalog=catalog,
            bundle=bundle,
            retrieved_at=args.retrieved_at,
        )
        print(
            f"{subject_id}: source_features={result['source_features']}, "
            f"selected_features={result['selected_features']}, "
            f"levels={result['levels']}, paths={result['paths']}"
        )
    args.bundle.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
