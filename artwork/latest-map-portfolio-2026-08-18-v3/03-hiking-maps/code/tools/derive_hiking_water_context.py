#!/usr/bin/env python3
"""Derive plot-legible WHW hydrography from a frozen OSM PBF extraction.

The input is the canonical JSON emitted from ``city_map_plotter.pbf`` with the
``water_areas`` and ``waterways`` layers enabled.  Geometry is selected in a
metric CRS, clipped before simplification, and written into the checked hiking
context bundle.  The result is artwork context, not navigation data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence

from pyproj import Transformer  # type: ignore[import-not-found]
from shapely import make_valid
from shapely.geometry import LineString, MultiLineString, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, transform, unary_union


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-v1.json"
DEFAULT_BUNDLE = ROOT / "src" / "city_map_plotter" / "data" / "hike-context-v3.json"
DEFAULT_SELECTION_GATE = (
    ROOT / "src" / "city_map_plotter" / "data" / "hike-uk-osm-selection-v1.json"
)
DEFAULT_SUBJECT_ID = "RTE-GB-WHW-01"
SOURCE_ID = "osm-scotland-hydrography-2026-08-02"
LANDCOVER_SOURCE_ID = "osm-scotland-landcover-2026-08-02"
ROUTE_CONTEXT_LIMIT_M = 10_000.0
SOURCE_URL = (
    "https://download.geofabrik.de/europe/united-kingdom/scotland-260802.osm.pbf"
)
INLAND_SOURCE_OBJECTS = (
    "relation/11015",  # Loch Lomond
    "relation/16820132",  # Loch Tulla
    "relation/17747060",  # Loch Arklet
    "way/4917752",  # Loch Eilde Mor
    "relation/896902",  # Loch Bà
    "relation/2822559",  # Blackwater Reservoir
    "way/4795076",  # Loch Sloy
    "relation/1463708",  # Loch Katrine
    "way/223515923",  # Loch Lyon
    "relation/898190",  # Loch Laidon
)
COAST_CLOSED_CHAIN_HASHES = frozenset(
    {
        "16538b54469b",
        "3d132424eabc",
        "3066c13ad4f6",
        "37ab7e3823e9",
        "bd95e8e8f097",
        "c81f8ce9c980",
        "c5392edf8db5",
        "e6ffe3b470eb",
        "412630b77277",
        "336d472dd9c0",
        "897c04bdd5e0",
        "8624dd8c8218",
    }
)
RIVER_ENTITIES: tuple[tuple[str, str, tuple[int, ...], str | None], ...] = (
    (
        "endrick-water",
        "Endrick Water",
        (10207253, 276758298, 276765440, 82257019),
        None,
    ),
    (
        "river-falloch",
        "River Falloch",
        (11430815, 272674117, 461132009, 461132010, 470298336, 470298337),
        None,
    ),
    (
        "river-fillan",
        "River Fillan",
        (272765235, 272765248, 272765274, 272765275),
        None,
    ),
    (
        "river-orchy",
        "River Orchy",
        (
            10362085,
            10362086,
            1125958470,
            252158938,
            272608460,
            272609301,
            353735845,
            40551398,
            40551874,
        ),
        None,
    ),
    ("water-of-tulla", "Water of Tulla", (27826464, 824300648), None),
    ("river-etive", "River Etive", (25225048, 32229491), None),
    ("river-coupall", "River Coupall", (77611370, 99843988), None),
    ("river-coe", "River Coe", (188150067, 60844161, 676274300), None),
    ("river-leven-kinlochleven", "River Leven", (42596520,), "Q24640556"),
    ("water-of-nevis", "Water of Nevis", (239976123, 366082952), None),
    ("river-nevis", "River Nevis", (23557802, 239182258), None),
    (
        "river-lochy-fort-william",
        "River Lochy",
        (4049661, 4049667, 1508161509),
        "Q24640759",
    ),
)


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"derive_hiking_water_context: {message}")


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"could not read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{path} must contain a JSON object")
    return value


def _geometry_polygons(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]  # type: ignore[list-item]
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        return [
            polygon
            for child in geometry.geoms  # type: ignore[attr-defined]
            for polygon in _geometry_polygons(child)
        ]
    return []


def _geometry_lines(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [LineString(geometry.coords)]  # type: ignore[attr-defined]
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        return [
            line
            for child in geometry.geoms  # type: ignore[attr-defined]
            for line in _geometry_lines(child)
        ]
    return []


def _source_points(raw: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    """Convert the PBF export's [latitude, longitude] order to x/y."""

    return [(float(point[1]), float(point[0])) for point in raw]


def _closed_ring(points: Iterable[Sequence[float]]) -> list[list[float]]:
    result = [[round(float(x), 6), round(float(y), 6)] for x, y in points]
    if result and result[0] != result[-1]:
        result.append(list(result[0]))
    return result


def _open_line(points: Iterable[Sequence[float]]) -> list[list[float]]:
    result = [[round(float(x), 6), round(float(y), 6)] for x, y in points]
    deduplicated: list[list[float]] = []
    for point in result:
        if not deduplicated or point != deduplicated[-1]:
            deduplicated.append(point)
    return deduplicated


def _geometry_hash(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _selection_gate(path: Path) -> tuple[dict[str, Any], str]:
    manifest = _load_object(path)
    if (
        manifest.get("id") != "hike-uk-osm-selection-v1"
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "frozen-audited-selection-gate"
    ):
        _fail("UK selection gate has an unsupported schema")
    gate = manifest.get("whw_route_context_gate")
    if not isinstance(gate, dict) or gate.get("subject_id") != DEFAULT_SUBJECT_ID:
        _fail("UK selection gate has no WHW route-context gate")
    if float(gate.get("maximum_distance_to_route_m") or -1) != ROUTE_CONTEXT_LIMIT_M:
        _fail("WHW route-context limit differs from the derivation contract")
    return gate, _geometry_hash(manifest)


def _geometry_manifest_hash(
    features: Sequence[dict[str, Any]], *, hash_key: str
) -> str:
    payload = sorted(
        (
            {
                "source_object": str(feature["source_object"]),
                hash_key: str(feature[hash_key]),
            }
            for feature in features
        ),
        key=lambda item: item["source_object"],
    )
    return _geometry_hash(payload)


def _prune_landcover(
    overlay: dict[str, Any], *, gate: dict[str, Any], manifest_sha256: str
) -> None:
    context = overlay.get("context")
    landcover = context.get("landcover") if isinstance(context, dict) else None
    if not isinstance(landcover, dict):
        _fail("WHW overlay has no reviewed landcover context")
    features = landcover.get("features")
    if not isinstance(features, list) or not all(
        isinstance(feature, dict) for feature in features
    ):
        _fail("WHW landcover features must be objects")

    expected = {str(value) for value in gate.get("landcover_source_objects") or []}
    removed_gate = gate.get("removed") or {}
    removed_items = (
        removed_gate.get("landcover") if isinstance(removed_gate, dict) else None
    )
    if not isinstance(removed_items, list):
        _fail("WHW landcover removal gate is invalid")
    removed = {str(item["source_object"]): item for item in removed_items}
    by_object = {str(feature["source_object"]): feature for feature in features}
    actual = set(by_object)
    if not expected <= actual or not actual <= expected | set(removed):
        _fail("WHW landcover identities differ from the audited before/after gate")
    for source_object in expected:
        feature = by_object[source_object]
        if float(feature["distance_to_route_m"]) > ROUTE_CONTEXT_LIMIT_M:
            _fail(f"WHW retained landcover {source_object} exceeds the route corridor")
    for source_object in actual & set(removed):
        feature = by_object[source_object]
        expected_distance = float(removed[source_object]["distance_to_route_m"])
        if (
            abs(float(feature["distance_to_route_m"]) - expected_distance) > 0.05
            or expected_distance <= ROUTE_CONTEXT_LIMIT_M
        ):
            _fail(f"WHW removed landcover evidence drifted for {source_object}")

    retained = [
        feature for feature in features if str(feature["source_object"]) in expected
    ]
    if len(retained) != int(gate["expected"]["landcover_objects"]):
        _fail("WHW retained landcover count differs from the audited gate")
    if _geometry_manifest_hash(retained, hash_key="source_geometry_sha256") != gate.get(
        "landcover_geometry_manifest_sha256"
    ):
        _fail("WHW retained landcover source-geometry manifest differs")
    landcover["features"] = retained
    landcover["derivation_id"] = "osm-pbf-whw-woodland-legibility-v4"
    landcover["selection_profile_id"] = "hike-uk-osm-selection-v1"
    landcover["selection_rule"] = str(gate["selection_rules"]["landcover"])
    landcover["maximum_route_context_distance_m"] = ROUTE_CONTEXT_LIMIT_M
    landcover["derivation_counts"] = {
        "reviewed_source_objects_before_corridor": len(expected) + len(removed),
        "selected_source_objects": len(retained),
        "removed_off_route_source_objects": len(removed),
    }

    sources = overlay.get("sources")
    if not isinstance(sources, list):
        _fail("WHW overlay sources must be an array")
    source = next(
        (item for item in sources if item.get("id") == LANDCOVER_SOURCE_ID), None
    )
    evidence = gate["source_evidence"]
    if not isinstance(source, dict) or (
        source.get("snapshot_sha256") != evidence["snapshot_sha256"]
        or source.get("canonical_extraction_sha256")
        != evidence["landcover_canonical_extraction_sha256"]
        or int(source.get("canonical_feature_count") or -1)
        != int(evidence["landcover_canonical_feature_count"])
    ):
        _fail("WHW landcover source evidence differs from the audited gate")
    source["selection_gate"] = {
        "id": "hike-uk-osm-selection-v1",
        "manifest_sha256": manifest_sha256,
        "selection_rules": {"landcover": gate["selection_rules"]["landcover"]},
    }


def _record(catalog: dict[str, Any], subject_id: str) -> dict[str, Any]:
    for record in catalog.get("plates", []):
        if isinstance(record, dict) and record.get("id") == subject_id:
            return record
    _fail(f"catalog has no plate {subject_id!r}")


def _overlay(bundle: dict[str, Any], subject_id: str) -> dict[str, Any]:
    for record in bundle.get("records", []):
        if isinstance(record, dict) and record.get("subject_id") == subject_id:
            return record
    _fail(f"context bundle has no overlay for {subject_id!r}")


def _source_object(feature: dict[str, Any]) -> str:
    return f"{feature['osm_type']}/{feature['osm_id']}"


def _polygon_groups(
    features: Sequence[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        if feature.get("geometry_type") == "polygon_ring":
            grouped[(str(feature["osm_type"]), str(feature["osm_id"]))].append(feature)
    result: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for key in sorted(grouped):
        parts = grouped[key]
        inners: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for part in parts:
            if part.get("ring_role") == "inner":
                inners[str(part.get("outer_ring_part"))].append(part)
        for outer in sorted(parts, key=lambda item: str(item.get("part"))):
            if outer.get("ring_role") != "outer":
                continue
            result.append((outer, inners.get(str(outer.get("part")), [])))
    return result


def _projected_polygon(
    outer: dict[str, Any],
    inners: Sequence[dict[str, Any]],
    *,
    forward: Transformer,
) -> BaseGeometry:
    polygon = Polygon(
        _source_points(outer["points"]),
        [_source_points(inner["points"]) for inner in inners],
    )
    return transform(forward.transform, make_valid(polygon))


def _water_class(tags: dict[str, Any]) -> str | None:
    if tags.get("natural") != "water":
        return None
    water = str(tags.get("water") or "lake")
    if water in {"pond", "basin", "wastewater", "lagoon", "lock"}:
        return None
    if water == "reservoir":
        return "reservoir"
    if water in {"river", "stream", "canal"}:
        return "river-water"
    return "lake"


def _derive_areas(
    groups: Sequence[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
) -> list[dict[str, Any]]:
    rank = {
        source_object: index
        for index, source_object in enumerate(INLAND_SOURCE_OBJECTS)
    }
    candidates: list[tuple[float, str, dict[str, Any], Polygon]] = []
    for outer, inners in groups:
        source_object = _source_object(outer)
        if source_object not in rank:
            continue
        semantic_class = _water_class(outer.get("tags") or {})
        if semantic_class is None:
            continue
        geometry = _projected_polygon(outer, inners, forward=forward).intersection(crop)
        if geometry.is_empty:
            continue
        for part_index, polygon in enumerate(_geometry_polygons(make_valid(geometry))):
            area_m2 = float(polygon.area)
            distance_m = float(polygon.distance(route))
            name = str((outer.get("tags") or {}).get("name") or "")
            simplified = make_valid(polygon.simplify(110.0, preserve_topology=True))
            for simplified_index, simple_polygon in enumerate(
                _geometry_polygons(simplified)
            ):
                if simple_polygon.area < 120_000.0:
                    continue
                suffix = f"{part_index:02d}-{simplified_index:02d}"
                geometry_wgs84 = transform(inverse.transform, simple_polygon)
                outer_ring = _closed_ring(geometry_wgs84.exterior.coords)
                holes = [
                    _closed_ring(ring.coords)
                    for ring in geometry_wgs84.interiors
                    if Polygon(ring).area > 0.0
                ]
                feature = {
                    "id": (f"water-{outer['osm_type']}-{outer['osm_id']}-{suffix}"),
                    "class": semantic_class,
                    "name": name or None,
                    "source_object": source_object,
                    "area_m2": round(area_m2, 1),
                    "route_distance_m": round(distance_m, 1),
                    "source_hole_count": len(inners),
                    "outer": outer_ring,
                    "holes": holes,
                }
                if source_object == "relation/2598529":
                    feature["geometry_review_required"] = True
                    feature["geometry_review_reason"] = (
                        "OSM source carries fixme=outline"
                    )
                feature["geometry_sha256"] = _geometry_hash(
                    {"outer": outer_ring, "holes": holes}
                )
                candidates.append((area_m2, source_object, feature, simple_polygon))
    candidates.sort(key=lambda item: (rank[item[1]], -item[0], item[2]["id"]))
    output = [candidate[2] for candidate in candidates]
    if len(output) != len(INLAND_SOURCE_OBJECTS):
        _fail(
            "binding inland-water whitelist did not resolve one polygon per object "
            f"({len(output)} != {len(INLAND_SOURCE_OBJECTS)})"
        )
    if sum(int(item["source_hole_count"]) for item in output) != 260:
        _fail("binding inland-water source-hole count drifted from 260")
    return output


def _derive_coastlines(
    features: Sequence[dict[str, Any]],
    *,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
) -> list[dict[str, Any]]:
    """Stitch exact OSM-way endpoints before clipping or simplification."""

    candidates = [
        feature
        for feature in features
        if feature.get("geometry_type") == "line"
        and (feature.get("tags") or {}).get("natural") == "coastline"
    ]
    if len(candidates) != 1_013:
        _fail(f"binding raw coastline count drifted from 1013 ({len(candidates)})")

    parents = list(range(len(candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    endpoint_members: dict[tuple[float, float], list[int]] = defaultdict(list)
    for index, feature in enumerate(candidates):
        endpoint_members[tuple(feature["points"][0])].append(index)
        endpoint_members[tuple(feature["points"][-1])].append(index)
    for members in endpoint_members.values():
        for member in members[1:]:
            union(members[0], member)

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(candidates)):
        components[find(index)].append(index)
    if len(components) != 582:
        _fail(f"binding coastline component count drifted from 582 ({len(components)})")

    output: list[dict[str, Any]] = []
    selected_components = 0
    route_context_components = 0
    selected_source_ways: set[int] = set()
    route_context_crop = make_valid(
        crop.intersection(route.buffer(ROUTE_CONTEXT_LIMIT_M))
    )
    for component_indices in components.values():
        endpoint_degrees: dict[tuple[float, float], int] = defaultdict(int)
        source_ids: list[int] = []
        source_lines: list[LineString] = []
        for index in component_indices:
            feature = candidates[index]
            endpoint_degrees[tuple(feature["points"][0])] += 1
            endpoint_degrees[tuple(feature["points"][-1])] += 1
            source_ids.append(int(feature["osm_id"]))
            source_lines.append(LineString(_source_points(feature["points"])))
        source_ids.sort()
        chain_hash = hashlib.sha256(
            ",".join(str(identifier) for identifier in source_ids).encode("ascii")
        ).hexdigest()[:12]
        closed_chain = all(degree == 2 for degree in endpoint_degrees.values())
        if closed_chain and chain_hash not in COAST_CLOSED_CHAIN_HASHES:
            continue
        selected_components += 1
        merged = linemerge(MultiLineString(source_lines))
        source_geometry = transform(forward.transform, merged)
        distance_m = float(source_geometry.distance(route))
        geometry = source_geometry.intersection(route_context_crop)
        if geometry.is_empty or distance_m > ROUTE_CONTEXT_LIMIT_M:
            continue
        route_context_components += 1
        paths: list[list[list[float]]] = []
        total_length_m = 0.0
        for line in _geometry_lines(geometry):
            total_length_m += float(line.length)
            simplified = line.simplify(130.0, preserve_topology=True)
            for simple_line in _geometry_lines(simplified):
                path = _open_line(transform(inverse.transform, simple_line).coords)
                if len(path) >= 2:
                    paths.append(path)
        if not paths:
            continue
        selected_source_ways.update(source_ids)
        source_objects = [f"way/{identifier}" for identifier in source_ids]
        item = {
            "id": f"coast-chain-{chain_hash}",
            "chain_hash": chain_hash,
            "closed_chain": closed_chain,
            "source_object": source_objects[0],
            "source_objects": source_objects,
            "source_objects_sha256": _geometry_hash(source_objects),
            "length_m": round(total_length_m, 1),
            "distance_to_route_m": round(distance_m, 1),
            "paths": paths,
        }
        item["geometry_sha256"] = _geometry_hash(paths)
        output.append(item)
    if selected_components != 23:
        _fail(
            f"binding coastline candidate count drifted from 23 ({selected_components})"
        )
    if route_context_components != 2:
        _fail(
            "binding coastline route-context selection drifted from 2 components "
            f"({route_context_components})"
        )
    if len(output) != 2 or len(selected_source_ways) != 279:
        _fail(
            "binding rendered coastline selection drifted from 2 chains/279 ways "
            f"({len(output)} chains/{len(selected_source_ways)} ways)"
        )
    subpath_count = sum(len(item["paths"]) for item in output)
    if subpath_count != 4:
        _fail(f"binding coastline subpath count drifted from 4 ({subpath_count})")
    return sorted(output, key=lambda item: item["id"])


def _derive_rivers(
    features: Sequence[dict[str, Any]],
    *,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
) -> list[dict[str, Any]]:
    del route  # The binding river set is identity-curated, not score-selected.
    ways_by_id = {
        int(feature["osm_id"]): feature
        for feature in features
        if feature.get("osm_type") == "way" and feature.get("geometry_type") == "line"
    }
    output: list[dict[str, Any]] = []
    selected_source_ways: set[int] = set()
    for entity_id, name, source_ids, wikidata in RIVER_ENTITIES:
        missing = [
            identifier for identifier in source_ids if identifier not in ways_by_id
        ]
        if missing:
            _fail(f"binding river {entity_id} is missing ways {missing}")
        source_lines = [
            LineString(_source_points(ways_by_id[identifier]["points"]))
            for identifier in source_ids
        ]
        merged = linemerge(MultiLineString(source_lines))
        geometry = transform(forward.transform, merged).intersection(crop)
        if geometry.is_empty:
            _fail(f"binding river {entity_id} does not intersect the WHW crop")
        paths: list[list[list[float]]] = []
        total_length_m = 0.0
        for line in _geometry_lines(geometry):
            total_length_m += float(line.length)
            simplified = line.simplify(100.0, preserve_topology=True)
            for simple_line in _geometry_lines(simplified):
                path = _open_line(transform(inverse.transform, simple_line).coords)
                if len(path) >= 2:
                    paths.append(path)
        if not paths:
            _fail(f"binding river {entity_id} produced no paths")
        selected_source_ways.update(source_ids)
        source_objects = [f"way/{identifier}" for identifier in sorted(source_ids)]
        item = {
            "id": f"river-entity-{entity_id}",
            "class": "river",
            "name": name,
            "source_object": source_objects[0],
            "source_objects": source_objects,
            "length_m": round(total_length_m, 1),
            "paths": paths,
        }
        if wikidata is not None:
            item["wikidata"] = wikidata
        item["geometry_sha256"] = _geometry_hash(paths)
        output.append(item)
    if len(output) != 12 or len(selected_source_ways) != 40:
        _fail(
            "binding river selection drifted from 12 entities/40 ways "
            f"({len(output)} entities/{len(selected_source_ways)} ways)"
        )
    return output


def _derive_labels(
    groups: Sequence[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
) -> list[dict[str, Any]]:
    desired = {"Firth of Clyde", "Firth of Lorn"}
    geometries: dict[str, list[tuple[dict[str, Any], BaseGeometry]]] = defaultdict(list)
    for outer, inners in groups:
        name = str((outer.get("tags") or {}).get("name") or "")
        if name not in desired:
            continue
        geometry = _projected_polygon(outer, inners, forward=forward).intersection(crop)
        if not geometry.is_empty:
            geometries[name].append((outer, geometry))
    output: list[dict[str, Any]] = []
    for name in sorted(geometries):
        entries = geometries[name]
        geometry = unary_union([entry[1] for entry in entries])
        point = transform(inverse.transform, geometry.representative_point())
        source = entries[0][0]
        source_object = _source_object(source)
        output.append(
            {
                "id": f"hydro-label-{name.casefold().replace(' ', '-')}",
                "kind": "sea",
                "label": name.upper(),
                "point": [round(float(point.x), 6), round(float(point.y), 6)],
                "source_object": source_object,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "priority": 2,
            }
        )
    return output


def derive(
    *,
    raw_path: Path,
    catalog_path: Path,
    bundle_path: Path,
    selection_gate_path: Path,
    subject_id: str,
) -> dict[str, int]:
    raw = _load_object(raw_path)
    catalog = _load_object(catalog_path)
    bundle = _load_object(bundle_path)
    record = _record(catalog, subject_id)
    overlay = _overlay(bundle, subject_id)
    gate, manifest_sha256 = _selection_gate(selection_gate_path)
    if subject_id != gate["subject_id"]:
        _fail("the WHW selection gate cannot be used for another subject")
    _prune_landcover(overlay, gate=gate, manifest_sha256=manifest_sha256)
    features = raw.get("features")
    metadata = raw.get("source_metadata")
    if not isinstance(features, list) or not isinstance(metadata, dict):
        _fail("raw extraction must contain features and source_metadata")
    canonical = metadata.get("canonical_features") or {}
    snapshot_hash = str(metadata.get("content_sha256") or "")
    extraction_hash = str(canonical.get("sha256") or "")
    if not all(
        len(value) == 64 and all(character in "0123456789abcdef" for character in value)
        for value in (snapshot_hash, extraction_hash)
    ):
        _fail("raw extraction is missing frozen SHA-256 evidence")
    if not bool((metadata.get("coverage") or {}).get("coverage_proven")):
        _fail("raw extraction does not prove bbox coverage")
    source_evidence = gate["source_evidence"]
    if (
        snapshot_hash != source_evidence["snapshot_sha256"]
        or extraction_hash != source_evidence["hydrography_canonical_extraction_sha256"]
        or int(canonical.get("count") or -1)
        != int(source_evidence["hydrography_canonical_feature_count"])
    ):
        _fail("WHW hydrography raw evidence differs from the audited gate")

    extent = record["context"]["extent"]
    if not (
        isinstance(extent, list)
        and len(extent) == 4
        and all(isinstance(value, (int, float)) for value in extent)
    ):
        _fail("catalog context extent is invalid")
    west, south, east, north = (float(value) for value in extent)
    forward = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    inverse = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    crop = transform(forward.transform, box(west, south, east, north))
    route_lines = [
        LineString([(float(point[0]), float(point[1])) for point in segment["points"]])
        for segment in record["route"]["segments"]
    ]
    route = transform(forward.transform, unary_union(route_lines))
    groups = _polygon_groups(features)

    areas = _derive_areas(
        groups, forward=forward, inverse=inverse, crop=crop, route=route
    )
    coastlines = _derive_coastlines(
        features, forward=forward, inverse=inverse, crop=crop, route=route
    )
    rivers = _derive_rivers(
        features,
        forward=forward,
        inverse=inverse,
        crop=crop,
        route=route,
    )
    labels = _derive_labels(groups, forward=forward, inverse=inverse, crop=crop)
    if not areas or not coastlines or not rivers:
        _fail("selection unexpectedly produced an empty hydrography class")
    if any(float(area["route_distance_m"]) > ROUTE_CONTEXT_LIMIT_M for area in areas):
        _fail("off-route inland water escaped the 10 km context gate")
    if any(
        float(coastline["distance_to_route_m"]) > ROUTE_CONTEXT_LIMIT_M
        for coastline in coastlines
    ):
        _fail("off-route coastline escaped the 10 km context gate")
    expected_water = {str(value) for value in gate["water_source_objects"]}
    if {str(area["source_object"]) for area in areas} != expected_water:
        _fail("WHW inland-water identities differ from the audited gate")
    if (
        _geometry_manifest_hash(areas, hash_key="geometry_sha256")
        != gate["water_geometry_manifest_sha256"]
    ):
        _fail("WHW retained water geometry manifest differs")
    actual_coast = {str(item["chain_hash"]): item for item in coastlines}
    expected_coast = {str(item["chain_hash"]): item for item in gate["coast_chains"]}
    if set(actual_coast) != set(expected_coast):
        _fail("WHW coastline identities differ from the audited gate")
    for chain_hash, expected in expected_coast.items():
        actual = actual_coast[chain_hash]
        if (
            bool(actual["closed_chain"]) is not bool(expected["closed"])
            or len(actual["source_objects"]) != int(expected["source_way_count"])
            or len(actual["paths"]) != int(expected["subpath_count"])
            or actual["source_objects_sha256"] != expected["source_objects_sha256"]
            or actual["geometry_sha256"] != expected["geometry_sha256"]
            or abs(
                float(actual["distance_to_route_m"])
                - float(expected["distance_to_route_m"])
            )
            > 0.05
        ):
            _fail(f"WHW coastline evidence drifted for {chain_hash}")

    source = {
        "id": SOURCE_ID,
        "publisher": "OpenStreetMap contributors / Geofabrik",
        "url": SOURCE_URL,
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors; openstreetmap.org/copyright",
        "use": (
            "systematic source polygons and lines for coast, major inland water, "
            "and named route-context rivers"
        ),
        "source_timestamp": str(metadata.get("source_timestamp") or ""),
        "retrieved_at": "2026-08-03T00:00:00Z",
        "snapshot_sha256": snapshot_hash,
        "canonical_extraction_sha256": extraction_hash,
        "canonical_feature_count": int(canonical.get("count") or 0),
        "coverage_proven": True,
        "selection_gate": {
            "id": "hike-uk-osm-selection-v1",
            "manifest_sha256": manifest_sha256,
            "selection_rules": {
                key: gate["selection_rules"][key]
                for key in ("water", "coast", "rivers")
            },
        },
    }
    sources = overlay.setdefault("sources", [])
    sources[:] = [item for item in sources if item.get("id") != SOURCE_ID]
    sources.append(source)
    context = overlay.setdefault("context", {})
    context["water"] = {
        "status": "source-sampled-hydrography",
        "source_ref": SOURCE_ID,
        "derivation_id": "osm-pbf-whw-hydrography-stitched-v5",
        "source_crs": "EPSG:4326",
        "working_crs": "EPSG:27700",
        "selection_rule": (
            "10 identity-curated inland objects within 10km of the route; exact-"
            "endpoint coastline components within 10km of the route clipped to "
            "the 10km route corridor; 12 "
            "identity-disambiguated river entities assembled from 40 exact ways"
        ),
        "selection_profile_id": "hike-uk-osm-selection-v1",
        "maximum_route_context_distance_m": ROUTE_CONTEXT_LIMIT_M,
        "simplification_tolerance_m": {
            "water_areas": 110,
            "coastlines": 130,
            "rivers": 100,
        },
        "source_feature_count": int(canonical.get("count") or 0),
        "derivation_counts": {
            "raw_area_rings": 8809,
            "raw_unassembled_areas": 1,
            "raw_waterway_lines": 30891,
            "raw_waterway_rings": 752,
            "selected_inland_objects": 10,
            "selected_inland_source_holes": 260,
            "rendered_inland_hole_boundaries_a5": 17,
            "coastline_source_lines": 1013,
            "coastline_components": 582,
            "coastline_candidates": 23,
            "route_context_limit_m": 10000,
            "rendered_coastline_chains": 2,
            "rendered_coastline_subpaths_before_physical_floor": 4,
            "rendered_coastline_source_ways": 279,
            "river_entities": 12,
            "river_source_ways": 40,
        },
        "areas": areas,
        "coastlines": coastlines,
        "rivers": rivers,
        "labels": labels,
    }
    overlay.setdefault("backdrop", {})["water"] = "source-sampled-hydrography"
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "areas": len(areas),
        "coastlines": len(coastlines),
        "rivers": len(rivers),
        "labels": len(labels),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-json", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--selection-gate", type=Path, default=DEFAULT_SELECTION_GATE)
    parser.add_argument("--subject-id", default=DEFAULT_SUBJECT_ID)
    args = parser.parse_args()
    counts = derive(
        raw_path=args.raw_json,
        catalog_path=args.catalog,
        bundle_path=args.bundle,
        selection_gate_path=args.selection_gate,
        subject_id=args.subject_id,
    )
    print("derived " + ", ".join(f"{name}={count}" for name, count in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
