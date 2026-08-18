#!/usr/bin/env python3
"""Enrich one hiking release plate from frozen Natural Earth hydrography.

The tool is deliberately offline and fail-closed.  It accepts only local
GeoJSON snapshots whose SHA-256 digests are supplied by the caller, clips
river centrelines to the already-frozen map extent, and retains only exact
lake shorelines wholly contained by that extent.  It never buffers, enlarges,
or synthesizes geographic geometry.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    box,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from city_map_plotter.hike_plates import _validate_release_catalog


RIVER_SOURCE_ID = "natural-earth-rivers-10m"
LAKE_SOURCE_ID = "natural-earth-lakes-10m"
ENRICHMENT_ID = "natural-earth-10m-hydrography-page-legible-v1"
RIVER_URL = (
    "https://www.naturalearthdata.com/downloads/10m-physical-vectors/"
    "10m-rivers-lake-centerlines/"
)
LAKE_URL = (
    "https://www.naturalearthdata.com/downloads/10m-physical-vectors/10m-lakes/"
)
SOURCE_IDS = frozenset({RIVER_SOURCE_ID, LAKE_SOURCE_ID})
MIN_RIVER_PAGE_LENGTH_MM = 2.5
MIN_LAKE_PAGE_AREA_MM2 = 0.32
MIN_LAKE_PAGE_PERIMETER_MM = 2.5
MAX_ROUTE_DISTANCE_MM = 4.0
MAX_RIVERS = 7
MAX_LAKES = 3
ROUTE_BANDS = 5


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_geojson(path: Path, expected_sha256: str) -> dict[str, Any]:
    actual = _file_sha256(path)
    if actual != expected_sha256:
        raise ValueError(
            f"Natural Earth snapshot hash mismatch for {path}: "
            f"expected {expected_sha256}, got {actual}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection" or not isinstance(
        payload.get("features"), list
    ):
        raise ValueError(f"{path} is not a GeoJSON FeatureCollection")
    crs_name = ((payload.get("crs") or {}).get("properties") or {}).get("name")
    if crs_name not in {None, "urn:ogc:def:crs:OGC:1.3:CRS84", "EPSG:4326"}:
        raise ValueError(f"{path} must use geographic longitude/latitude CRS")
    return payload


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        return sorted(
            [part for item in geometry.geoms for part in _line_parts(item)],
            key=lambda item: (item.bounds, item.wkb_hex),
        )
    return []


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        return sorted(
            [part for item in geometry.geoms for part in _polygon_parts(item)],
            key=lambda item: (item.bounds, item.wkb_hex),
        )
    return []


def _rounded_path(coordinates: Iterable[Sequence[float]]) -> list[list[float]]:
    return [
        [round(float(coordinate[0]), 9), round(float(coordinate[1]), 9)]
        for coordinate in coordinates
    ]


def _page_transform(
    extent: Sequence[float],
) -> Callable[[float, float, float | None], tuple[float, float]]:
    """Match the renderer's A5-profile route rectangle exactly."""

    west, south, east, north = (float(value) for value in extent)
    rect_x, rect_y, rect_width, rect_height = 17.5, 43.622, 113.0, 102.324
    cosine = max(math.cos(math.radians((south + north) / 2.0)), 1e-6)
    minimum_x = west * cosine
    maximum_x = east * cosine
    scale = min(
        rect_width / (maximum_x - minimum_x),
        rect_height / (north - south),
    )
    horizontal_margin = rect_width - (maximum_x - minimum_x) * scale
    vertical_margin = rect_height - (north - south) * scale

    def physical(
        longitude: float,
        latitude: float,
        _elevation: float | None = None,
    ) -> tuple[float, float]:
        return (
            rect_x
            + horizontal_margin / 2.0
            + (float(longitude) * cosine - minimum_x) * scale,
            rect_y
            + vertical_margin / 2.0
            + (north - float(latitude)) * scale,
        )

    return physical


def _page_route(record: dict[str, Any], physical: Callable[..., tuple[float, float]]) -> LineString:
    coordinates = [
        physical(float(point[0]), float(point[1]))
        for segment in record["route"]["segments"]
        for point in segment["points"]
    ]
    return LineString(coordinates)


def _representative_line_point(geometry: BaseGeometry) -> tuple[float, float]:
    part = max(_line_parts(geometry), key=lambda item: (item.length, item.wkb_hex))
    point = part.interpolate(0.5, normalized=True)
    return (float(point.x), float(point.y))


def _route_metrics(
    page_geometry: BaseGeometry,
    page_route: LineString,
) -> tuple[float, float]:
    parts = _line_parts(page_geometry)
    if not parts:
        point = page_geometry.representative_point()
        route_distance = float(page_route.distance(point))
        route_axis = float(page_route.project(point) / page_route.length)
        return route_distance, route_axis
    representative = max(parts, key=lambda item: (item.length, item.wkb_hex)).interpolate(
        0.5,
        normalized=True,
    )
    return (
        min(float(page_route.distance(part)) for part in parts),
        float(page_route.project(representative) / page_route.length),
    )


@dataclass(frozen=True)
class Candidate:
    feature_index: int
    name: str
    scalerank: int
    source_feature_sha256: str
    geometry: BaseGeometry
    page_length_mm: float
    page_area_mm2: float
    route_distance_mm: float
    route_axis: float


def _river_candidates(
    payload: dict[str, Any],
    *,
    extent: Sequence[float],
    physical: Callable[..., tuple[float, float]],
    page_route: LineString,
) -> list[Candidate]:
    crop = box(*[float(value) for value in extent])
    candidates: list[Candidate] = []
    for index, feature in enumerate(payload["features"]):
        properties = feature.get("properties") or {}
        if properties.get("featurecla") != "River":
            continue
        name = properties.get("name_en") or properties.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        geometry = shape(feature["geometry"]).intersection(crop)
        if not _line_parts(geometry):
            continue
        page_geometry = transform(physical, geometry)
        page_length = sum(part.length for part in _line_parts(page_geometry))
        route_distance, route_axis = _route_metrics(page_geometry, page_route)
        if (
            page_length + 1e-9 < MIN_RIVER_PAGE_LENGTH_MM
            or route_distance > MAX_ROUTE_DISTANCE_MM
        ):
            continue
        candidates.append(
            Candidate(
                feature_index=index,
                name=name.strip(),
                scalerank=int(round(float(properties.get("scalerank", 99)))),
                source_feature_sha256=_canonical_sha256(feature),
                geometry=geometry,
                page_length_mm=float(page_length),
                page_area_mm2=0.0,
                route_distance_mm=route_distance,
                route_axis=route_axis,
            )
        )
    return sorted(candidates, key=_candidate_score)


def _lake_candidates(
    payload: dict[str, Any],
    *,
    extent: Sequence[float],
    physical: Callable[..., tuple[float, float]],
    page_route: LineString,
) -> list[Candidate]:
    crop = box(*[float(value) for value in extent])
    candidates: list[Candidate] = []
    for index, feature in enumerate(payload["features"]):
        properties = feature.get("properties") or {}
        name = properties.get("name_en") or properties.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        geometry = shape(feature["geometry"])
        # A clipped polygon boundary includes crop edges.  Retain only lakes
        # wholly inside the frozen map extent so every plotted shoreline is
        # present in the source snapshot.
        if geometry.is_empty or not crop.covers(geometry):
            continue
        polygons = _polygon_parts(geometry)
        if not polygons:
            continue
        page_geometry = transform(physical, geometry)
        page_area = float(page_geometry.area)
        page_length = float(page_geometry.boundary.length)
        route_distance, route_axis = _route_metrics(
            page_geometry.boundary,
            page_route,
        )
        if (
            page_area + 1e-9 < MIN_LAKE_PAGE_AREA_MM2
            or page_length + 1e-9 < MIN_LAKE_PAGE_PERIMETER_MM
            or route_distance > MAX_ROUTE_DISTANCE_MM
        ):
            continue
        candidates.append(
            Candidate(
                feature_index=index,
                name=name.strip(),
                scalerank=int(round(float(properties.get("scalerank", 99)))),
                source_feature_sha256=_canonical_sha256(feature),
                geometry=geometry,
                page_length_mm=page_length,
                page_area_mm2=page_area,
                route_distance_mm=route_distance,
                route_axis=route_axis,
            )
        )
    return sorted(candidates, key=_candidate_score)


def _candidate_score(candidate: Candidate) -> tuple[float, int, float, float, str, int]:
    return (
        0.0 if candidate.route_distance_mm <= 0.9 else 1.0,
        candidate.scalerank,
        -candidate.page_length_mm,
        candidate.route_distance_mm,
        candidate.name.casefold(),
        candidate.feature_index,
    )


def _select_distributed(
    candidates: Sequence[Candidate],
    *,
    maximum: int,
) -> list[Candidate]:
    ordered = sorted(candidates, key=_candidate_score)
    selected: list[Candidate] = []
    selected_indices: set[int] = set()
    for band in range(ROUTE_BANDS):
        in_band = [
            candidate
            for candidate in ordered
            if min(int(candidate.route_axis * ROUTE_BANDS), ROUTE_BANDS - 1) == band
        ]
        if not in_band:
            continue
        candidate = min(in_band, key=_candidate_score)
        selected.append(candidate)
        selected_indices.add(candidate.feature_index)
        if len(selected) >= maximum:
            break
    for candidate in ordered:
        if len(selected) >= maximum:
            break
        if candidate.feature_index in selected_indices:
            continue
        selected.append(candidate)
        selected_indices.add(candidate.feature_index)
    return sorted(
        selected,
        key=lambda item: (item.route_axis, item.scalerank, item.name, item.feature_index),
    )


def _river_feature(candidate: Candidate) -> dict[str, Any]:
    paths = [
        _rounded_path(part.coords)
        for part in _line_parts(candidate.geometry)
        if len(part.coords) >= 2
    ]
    point = _representative_line_point(candidate.geometry)
    return {
        "id": f"natural-earth-river-10m-{candidate.feature_index}",
        "kind": "river",
        "label": candidate.name.upper(),
        "point": [round(point[0], 9), round(point[1], 9)],
        "paths": paths,
        "source_ref": RIVER_SOURCE_ID,
        "source_url": RIVER_URL,
        "priority": candidate.scalerank,
        "display_label": True,
        "source_feature_index": candidate.feature_index,
        "source_feature_sha256": candidate.source_feature_sha256,
        "geometry_sha256": _canonical_sha256(paths),
        "source_page_length_mm": round(candidate.page_length_mm, 6),
        "source_route_distance_mm": round(candidate.route_distance_mm, 6),
        "selection_rule": (
            "exact-natural-earth-10m-river-clipped-to-frozen-map-extent; "
            "page-length>=2.5mm; route-distance<=4mm; route-axis-stratified-cap7"
        ),
    }


def _lake_feature(candidate: Candidate) -> dict[str, Any]:
    paths: list[list[list[float]]] = []
    for polygon in _polygon_parts(candidate.geometry):
        paths.append(_rounded_path(polygon.exterior.coords))
        paths.extend(_rounded_path(ring.coords) for ring in polygon.interiors)
    anchor = candidate.geometry.representative_point()
    return {
        "id": f"natural-earth-lake-10m-{candidate.feature_index}",
        "kind": "water",
        "label": candidate.name.upper(),
        "point": [round(float(anchor.x), 9), round(float(anchor.y), 9)],
        "paths": paths,
        "source_ref": LAKE_SOURCE_ID,
        "source_url": LAKE_URL,
        "priority": candidate.scalerank,
        "display_label": True,
        "source_feature_index": candidate.feature_index,
        "source_feature_sha256": candidate.source_feature_sha256,
        "geometry_sha256": _canonical_sha256(paths),
        "source_page_perimeter_mm": round(candidate.page_length_mm, 6),
        "source_page_area_mm2": round(candidate.page_area_mm2, 6),
        "source_route_distance_mm": round(candidate.route_distance_mm, 6),
        "selection_rule": (
            "whole-natural-earth-10m-lake-within-frozen-map-extent; "
            "page-area>=0.32mm2; perimeter>=2.5mm; route-distance<=4mm; cap3"
        ),
    }


def _family_item(context: dict[str, Any], family: str) -> dict[str, Any]:
    return next(item for item in context["family_evidence"] if item["family"] == family)


def _page_legible_hydro_count(
    features: Sequence[dict[str, Any]],
    physical: Callable[..., tuple[float, float]],
) -> int:
    count = 0
    for feature in features:
        if feature.get("kind") not in {"river", "water", "coast", "sea"}:
            continue
        page_lines = [
            LineString([physical(float(point[0]), float(point[1])) for point in path])
            for path in feature.get("paths", [])
            if isinstance(path, list) and len(path) >= 2
        ]
        if feature.get("kind") == "water":
            areas = [
                Polygon(line.coords).area
                for line in page_lines
                if len(line.coords) >= 4 and line.is_ring
            ]
            legible = max(areas, default=0.0) + 1e-9 >= MIN_LAKE_PAGE_AREA_MM2
        else:
            legible = sum(line.length for line in page_lines) + 1e-9 >= 0.75
        count += int(legible)
    return count


def enrich_catalog(
    catalog: dict[str, Any],
    *,
    route_id: str,
    rivers_payload: dict[str, Any],
    lakes_payload: dict[str, Any],
    rivers_sha256: str,
    lakes_sha256: str,
    retrieved_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output = copy.deepcopy(catalog)
    record = next((item for item in output["plates"] if item["id"] == route_id), None)
    if record is None:
        raise ValueError(f"route {route_id!r} is absent from the release catalog")
    context = record["context"]
    previous = context.get("natural_earth_hydrography_enrichment")
    base_evidence = (
        copy.deepcopy(previous["base_hydrography_evidence"])
        if isinstance(previous, dict)
        else copy.deepcopy(_family_item(context, "hydrography"))
    )
    context["features"] = [
        feature
        for feature in context["features"]
        if feature.get("source_ref") not in SOURCE_IDS
    ]
    record["sources"] = [
        source for source in record["sources"] if source.get("id") not in SOURCE_IDS
    ]

    physical = _page_transform(context["extent"])
    page_route = _page_route(record, physical)
    river_candidates = _river_candidates(
        rivers_payload,
        extent=context["extent"],
        physical=physical,
        page_route=page_route,
    )
    lake_candidates = _lake_candidates(
        lakes_payload,
        extent=context["extent"],
        physical=physical,
        page_route=page_route,
    )
    selected_rivers = _select_distributed(river_candidates, maximum=MAX_RIVERS)
    selected_lakes = _select_distributed(lake_candidates, maximum=MAX_LAKES)
    additions = [
        *[_river_feature(candidate) for candidate in selected_rivers],
        *[_lake_feature(candidate) for candidate in selected_lakes],
    ]
    context["features"].extend(additions)

    river_selection_geometry_sha256 = _canonical_sha256(
        [
            {
                "id": feature["id"],
                "geometry_sha256": feature["geometry_sha256"],
            }
            for feature in additions
            if feature["source_ref"] == RIVER_SOURCE_ID
        ]
    )
    lake_selection_geometry_sha256 = _canonical_sha256(
        [
            {
                "id": feature["id"],
                "geometry_sha256": feature["geometry_sha256"],
            }
            for feature in additions
            if feature["source_ref"] == LAKE_SOURCE_ID
        ]
    )
    selected_geometry_sha256 = _canonical_sha256(
        [
            {
                "id": feature["id"],
                "geometry_sha256": feature["geometry_sha256"],
            }
            for feature in additions
        ]
    )
    record["sources"].extend(
        [
            {
                "id": RIVER_SOURCE_ID,
                "publisher": "Natural Earth",
                "url": RIVER_URL,
                "license": "public-domain",
                "attribution": "Made with Natural Earth",
                "use": "selected generalized river centrelines at 1:10m scale",
                "retrieved_at": retrieved_at,
                "dataset_id": "ne_10m_rivers_lake_centerlines",
                "source_format": "GeoJSON / CRS84",
                "snapshot_sha256": rivers_sha256,
                "source_feature_count": len(rivers_payload["features"]),
                "eligible_feature_count": len(river_candidates),
                "selected_feature_indices": [
                    candidate.feature_index for candidate in selected_rivers
                ],
                "selection_geometry_sha256": river_selection_geometry_sha256,
            },
            {
                "id": LAKE_SOURCE_ID,
                "publisher": "Natural Earth",
                "url": LAKE_URL,
                "license": "public-domain",
                "attribution": "Made with Natural Earth",
                "use": "selected generalized inland shorelines at 1:10m scale",
                "retrieved_at": retrieved_at,
                "dataset_id": "ne_10m_lakes",
                "source_format": "GeoJSON / CRS84",
                "snapshot_sha256": lakes_sha256,
                "source_feature_count": len(lakes_payload["features"]),
                "eligible_feature_count": len(lake_candidates),
                "selected_feature_indices": [
                    candidate.feature_index for candidate in selected_lakes
                ],
                "selection_geometry_sha256": lake_selection_geometry_sha256,
            },
        ]
    )

    hydro_features = [
        feature
        for feature in context["features"]
        if feature.get("kind") in {"river", "water", "coast", "sea"}
    ]
    assessed_count = len(hydro_features)
    legible_count = _page_legible_hydro_count(hydro_features, physical)
    hydro_evidence = copy.deepcopy(base_evidence)
    hydro_evidence.update(
        {
            "status": "source-features-selected",
            "source_candidate_count": int(base_evidence["source_candidate_count"])
            + len(river_candidates)
            + len(lake_candidates),
            "selected_feature_count": len(hydro_features),
            "page_legibility_assessed_feature_count": assessed_count,
            "page_legible_feature_count": legible_count,
            "sub_legible_feature_count": assessed_count - legible_count,
            "query_groups": [
                *base_evidence["query_groups"],
                "natural-earth-10m-rivers",
                "natural-earth-10m-lakes",
            ],
        }
    )
    evidence_index = next(
        index
        for index, item in enumerate(context["family_evidence"])
        if item["family"] == "hydrography"
    )
    context["family_evidence"][evidence_index] = hydro_evidence
    context["natural_earth_hydrography_enrichment"] = {
        "id": ENRICHMENT_ID,
        "status": "frozen-local-snapshot-derived",
        "geometry_policy": (
            "source-lines-clipped-only; whole-source-lake-shorelines; "
            "no-buffer-no-enlargement-no-invented-connectors"
        ),
        "north_up": True,
        "base_hydrography_evidence": base_evidence,
        "river_snapshot_sha256": rivers_sha256,
        "lake_snapshot_sha256": lakes_sha256,
        "selected_geometry_sha256": selected_geometry_sha256,
        "river_candidate_count": len(river_candidates),
        "lake_candidate_count": len(lake_candidates),
        "selected_river_count": len(selected_rivers),
        "selected_lake_count": len(selected_lakes),
        "selected_source_page_length_mm": round(
            sum(candidate.page_length_mm for candidate in selected_rivers)
            + sum(candidate.page_length_mm for candidate in selected_lakes),
            6,
        ),
        "selected_source_page_area_mm2": round(
            sum(candidate.page_area_mm2 for candidate in selected_lakes),
            6,
        ),
    }
    metrics = {
        "route_id": route_id,
        "river_snapshot_sha256": rivers_sha256,
        "lake_snapshot_sha256": lakes_sha256,
        "river_candidate_count": len(river_candidates),
        "lake_candidate_count": len(lake_candidates),
        "selected_rivers": [
            {
                "feature_index": candidate.feature_index,
                "name": candidate.name,
                "page_length_mm": round(candidate.page_length_mm, 6),
                "route_distance_mm": round(candidate.route_distance_mm, 6),
                "route_axis": round(candidate.route_axis, 6),
            }
            for candidate in selected_rivers
        ],
        "selected_lakes": [
            {
                "feature_index": candidate.feature_index,
                "name": candidate.name,
                "page_perimeter_mm": round(candidate.page_length_mm, 6),
                "page_area_mm2": round(candidate.page_area_mm2, 6),
                "route_distance_mm": round(candidate.route_distance_mm, 6),
                "route_axis": round(candidate.route_axis, 6),
            }
            for candidate in selected_lakes
        ],
        "selected_source_page_length_mm": context[
            "natural_earth_hydrography_enrichment"
        ]["selected_source_page_length_mm"],
        "page_legible_hydro_feature_count": legible_count,
        "river_selection_geometry_sha256": river_selection_geometry_sha256,
        "lake_selection_geometry_sha256": lake_selection_geometry_sha256,
        "selected_geometry_sha256": selected_geometry_sha256,
    }
    _validate_release_catalog(output)
    return output, metrics


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--route-id", default="RTE-US-CDT-01")
    parser.add_argument("--rivers", type=Path, required=True)
    parser.add_argument("--lakes", type=Path, required=True)
    parser.add_argument("--expected-rivers-sha256", required=True)
    parser.add_argument("--expected-lakes-sha256", required=True)
    parser.add_argument("--retrieved-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    rivers_payload = _verified_geojson(
        args.rivers,
        args.expected_rivers_sha256,
    )
    lakes_payload = _verified_geojson(
        args.lakes,
        args.expected_lakes_sha256,
    )
    enriched, metrics = enrich_catalog(
        catalog,
        route_id=args.route_id,
        rivers_payload=rivers_payload,
        lakes_payload=lakes_payload,
        rivers_sha256=args.expected_rivers_sha256,
        lakes_sha256=args.expected_lakes_sha256,
        retrieved_at=args.retrieved_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if args.metrics is not None:
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
