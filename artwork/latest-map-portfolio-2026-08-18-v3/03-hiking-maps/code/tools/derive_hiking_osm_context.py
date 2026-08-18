#!/usr/bin/env python3
"""Derive factual, plot-legible hiking context from canonical PBF JSON.

The input is the JSON written by ``extract_hiking_context_pbf.py``.  This tool
does not read or download a PBF itself.  It verifies the frozen extraction
metadata, selects geometry in a local metric projection, and upserts one
non-WHW record in ``hike-context-v3.json``.  The West Highland Way record is a
separate reviewed v4 derivation and is deliberately immutable here.

Only ``natural=wood`` and ``landuse=forest`` become land cover.  Coastline and
same-name river ways are stitched at exact source endpoints before clipping or
simplification.  Every selected feature carries its canonical OSM object(s), a
hash of the exact source geometry, and a hash of the derived geometry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence

from pyproj import CRS, Transformer  # type: ignore[import-not-found]
from shapely import make_valid
from shapely.geometry import LineString, MultiLineString, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, transform, unary_union


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "hike-plates-v1.json"
DEFAULT_BUNDLE = ROOT / "src" / "city_map_plotter" / "data" / "hike-context-v3.json"
UK_SELECTION_PATH = (
    ROOT / "src" / "city_map_plotter" / "data" / "hike-uk-osm-selection-v1.json"
)
NONUK_SELECTION_PATH = (
    ROOT / "src" / "city_map_plotter" / "data" / "hike-nonuk-osm-selection-v1.json"
)
WHW_SUBJECT_ID = "RTE-GB-WHW-01"
REQUIRED_LAYERS = frozenset({"green_space", "water_areas", "waterways"})
GENERIC_LANDCOVER_DERIVATION = "osm-pbf-generic-forest-woodland-v1"
GENERIC_WATER_DERIVATION = "osm-pbf-generic-hydrography-stitched-v1"
UK_AUDITED_SUBJECTS = frozenset(
    {"RTE-GB-HEB-WALK-01", "RTE-GB-GGW-01", "RTE-GB-JMW-WALK-01"}
)
HEB_SUBJECT_ID = "RTE-GB-HEB-WALK-01"
HEB_ROUTE_CONTEXT_LIMIT_M = 5_000.0
HEB_MIN_LANDCOVER_PART_MM2 = 0.32
HEB_MAX_COASTLINE_CHAINS = 16
NONUK_AUDITED_SUBJECTS = frozenset(
    {
        "RTE-CH-VA1-01",
        "RTE-CH-AP6-01",
        "RTE-IS-LAUG-01",
        "RTE-ES-CAM-ES01C",
    }
)
PARTIAL_CROP_AUDITED_SUBJECTS = frozenset({"RTE-CH-AP6-01", "RTE-ES-CAM-ES01C"})
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")


class ContextDerivationError(ValueError):
    """Raised when frozen evidence or derived context is not trustworthy."""


@dataclass(frozen=True)
class SelectionOptions:
    """Optional absolute overrides for the scale-aware selection defaults."""

    simplification_m: float | None = None
    minimum_landcover_area_m2: float | None = None
    minimum_water_area_m2: float | None = None
    river_context_distance_m: float | None = None
    minimum_river_length_m: float | None = None
    minimum_coastline_length_m: float | None = None
    minimum_closed_coastline_area_m2: float | None = None
    max_landcover_features: int = 72
    max_water_areas: int = 14
    max_coastline_chains: int = 24
    max_river_entities: int = 12


@dataclass(frozen=True)
class ResolvedSelection:
    simplification_m: float
    minimum_landcover_area_m2: float
    minimum_water_area_m2: float
    route_context_distance_m: float
    minimum_river_length_m: float
    minimum_coastline_length_m: float
    minimum_closed_coastline_area_m2: float
    max_landcover_features: int
    max_water_areas: int
    max_coastline_chains: int
    max_river_entities: int


@dataclass(frozen=True)
class DerivationResult:
    subject_id: str
    landcover_features: int
    water_areas: int
    coastline_chains: int
    river_entities: int
    labels: int


@dataclass(frozen=True)
class RawEvidence:
    path: Path
    source_url: str
    metadata: dict[str, Any]
    features: list[dict[str, Any]]
    requested_bbox: tuple[float, float, float, float]
    header_bbox: tuple[float, float, float, float]
    raw_coverage_proven: bool
    snapshot_sha256: str
    canonical_extraction_sha256: str
    raw_context_payload_sha256: str


@dataclass(frozen=True)
class CoverageEvidence:
    """Coverage proof for the actual plate crop and route."""

    crop_coverage_fraction: float
    crop_uncovered_area_degrees2: float
    route_coverage_fraction: float
    route_fully_covered: bool
    full_crop_coverage_proven: bool
    absence_claims_safe: bool


def _fail(message: str) -> NoReturn:
    raise ContextDerivationError(message)


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"could not read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{path} must contain a JSON object")
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_sha256(value: Any) -> str:
    text = str(value or "")
    if SHA256.fullmatch(text) is None:
        _fail("raw extraction is missing lower-case SHA-256 evidence")
    return text


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


def _whw_digest(bundle: dict[str, Any]) -> str:
    for record in bundle.get("records", []):
        if isinstance(record, dict) and record.get("subject_id") == WHW_SUBJECT_ID:
            return _canonical_sha256(record)
    _fail("context bundle is missing the reviewed WHW v4 record")


def _catalog_extent(record: dict[str, Any]) -> tuple[float, float, float, float]:
    context = record.get("context")
    extent = context.get("extent") if isinstance(context, dict) else None
    if not (
        isinstance(extent, list)
        and len(extent) == 4
        and all(isinstance(value, (int, float)) for value in extent)
    ):
        _fail(f"{record.get('id')} has an invalid context extent")
    west, south, east, north = (float(value) for value in extent)
    if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
        _fail(f"{record.get('id')} has an invalid context extent")
    return west, south, east, north


def _coverage_bbox(
    metadata: dict[str, Any], *, key: str
) -> tuple[float, float, float, float]:
    coverage = metadata.get("coverage")
    if not isinstance(coverage, dict):
        _fail("raw extraction has no coverage evidence")
    bbox = coverage.get(key)
    if not isinstance(bbox, dict):
        _fail(f"raw extraction has no {key} evidence")
    try:
        west, south, east, north = (
            float(bbox[field]) for field in ("west", "south", "east", "north")
        )
    except (KeyError, TypeError, ValueError):
        _fail(f"raw extraction {key} is invalid")
    if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
        _fail(f"raw extraction {key} is invalid")
    return west, south, east, north


def _verify_raw(
    raw: dict[str, Any],
    *,
    path: Path,
    source_url: str,
    audited_header_coverage: bool,
) -> RawEvidence:
    features = raw.get("features")
    metadata = raw.get("source_metadata")
    if not isinstance(features, list) or not all(
        isinstance(feature, dict) for feature in features
    ):
        _fail("raw extraction features must be an array of objects")
    if not isinstance(metadata, dict):
        _fail("raw extraction must contain source_metadata")
    snapshot_hash = _valid_sha256(metadata.get("content_sha256"))
    canonical = metadata.get("canonical_features")
    if not isinstance(canonical, dict):
        _fail("raw extraction is missing canonical_features evidence")
    extraction_hash = _valid_sha256(canonical.get("sha256"))
    if canonical.get("count") != len(features):
        _fail("canonical feature count does not match the serialized extraction")
    extraction = metadata.get("extraction")
    enabled = extraction.get("enabled_layers") if isinstance(extraction, dict) else None
    if not isinstance(enabled, list) or not REQUIRED_LAYERS.issubset(
        {str(layer) for layer in enabled}
    ):
        _fail(
            "raw extraction must enable green_space, water_areas, and waterways; "
            "otherwise absence cannot be interpreted truthfully"
        )
    coverage = metadata.get("coverage")
    if not isinstance(coverage, dict):
        _fail("raw extraction has no coverage evidence")
    raw_coverage_proven = coverage.get("coverage_proven") is True
    if not raw_coverage_proven and not audited_header_coverage:
        _fail("raw extraction does not prove that the PBF covers the requested bbox")
    requested_bbox = _coverage_bbox(metadata, key="requested_bbox_wgs84")
    header_bbox = (
        _coverage_bbox(metadata, key="header_bbox_wgs84")
        if audited_header_coverage
        else requested_bbox
    )
    raw_payload_hash = _canonical_sha256(
        {"source_metadata": metadata, "features": features}
    )
    return RawEvidence(
        path=path,
        source_url=source_url,
        metadata=metadata,
        features=features,
        requested_bbox=requested_bbox,
        header_bbox=header_bbox,
        raw_coverage_proven=raw_coverage_proven,
        snapshot_sha256=snapshot_hash,
        canonical_extraction_sha256=extraction_hash,
        raw_context_payload_sha256=raw_payload_hash,
    )


def _combined_raw_inputs(
    *,
    raw_paths: Sequence[Path],
    source_urls: Sequence[str],
    extent: tuple[float, float, float, float],
    audited_header_coverage: bool = False,
) -> tuple[list[dict[str, Any]], list[RawEvidence], BaseGeometry]:
    if not raw_paths:
        _fail("at least one raw extraction is required")
    if len(raw_paths) != len(source_urls):
        _fail("each raw extraction must have one corresponding source URL")
    evidence = [
        _verify_raw(
            _load_object(path),
            path=path,
            source_url=source_url,
            audited_header_coverage=audited_header_coverage,
        )
        for path, source_url in zip(raw_paths, source_urls, strict=True)
    ]
    coverage_union = unary_union(
        [
            box(*(item.header_bbox if audited_header_coverage else item.requested_bbox))
            for item in evidence
        ]
    )
    subject_box = box(*extent)
    uncovered = subject_box.difference(coverage_union)
    if (
        not audited_header_coverage
        and not uncovered.is_empty
        and uncovered.area > subject_box.area * 1e-12
    ):
        _fail(
            "the union of proven raw-extraction bboxes does not cover the catalog "
            "context extent"
        )

    by_key: dict[tuple[str, ...], tuple[str, dict[str, Any], set[str]]] = {}
    for item in evidence:
        for raw_feature in item.features:
            feature = dict(raw_feature)
            key = tuple(
                str(feature.get(field) or "")
                for field in (
                    "layer",
                    "osm_type",
                    "osm_id",
                    "part",
                    "geometry_type",
                    "ring_role",
                    "outer_ring_part",
                )
            )
            payload_hash = _canonical_sha256(_source_feature_payload(feature))
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = (
                    payload_hash,
                    feature,
                    {item.snapshot_sha256},
                )
                continue
            if existing[0] != payload_hash:
                _fail(
                    "overlapping raw extracts contain conflicting geometry/tags for "
                    f"{feature.get('osm_type')}/{feature.get('osm_id')} part "
                    f"{feature.get('part')}"
                )
            existing[2].add(item.snapshot_sha256)
    combined: list[dict[str, Any]] = []
    for _, feature, snapshot_hashes in sorted(
        by_key.values(), key=lambda item: _feature_sort_key(item[1])
    ):
        feature["_input_snapshot_sha256s"] = sorted(snapshot_hashes)
        combined.append(feature)
    return combined, evidence, coverage_union


def _uk_selection_gate(
    *,
    subject_id: str,
    extent: tuple[float, float, float, float],
    evidence: Sequence[RawEvidence],
) -> tuple[dict[str, Any], str] | None:
    if subject_id not in UK_AUDITED_SUBJECTS:
        return None
    manifest = _load_object(UK_SELECTION_PATH)
    if (
        manifest.get("id") != "hike-uk-osm-selection-v1"
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "frozen-audited-selection-gate"
    ):
        _fail("UK audited selection manifest has an unsupported schema")
    subjects = manifest.get("subjects")
    gate = subjects.get(subject_id) if isinstance(subjects, dict) else None
    if not isinstance(gate, dict):
        _fail(f"UK audited selection manifest has no {subject_id}")
    if list(extent) != gate.get("extent"):
        _fail(f"{subject_id} extent does not match its frozen UK selection gate")
    if len(evidence) != 1:
        _fail(f"{subject_id} requires its one audited Scotland PBF extraction")
    item = evidence[0]
    if (
        item.snapshot_sha256 != manifest.get("source_snapshot_sha256")
        or item.canonical_extraction_sha256 != gate.get("canonical_features_sha256")
        or len(item.features) != gate.get("canonical_feature_count")
    ):
        _fail(
            f"{subject_id} raw extraction does not match the frozen UK audited "
            "snapshot/count/hash"
        )
    return gate, _canonical_sha256(manifest)


def _nonuk_manifest_objects(gate: dict[str, Any]) -> list[str]:
    """Return source IDs in the frozen audit compiler's canonical layer order."""

    result = [str(value) for value in gate.get("water_source_objects") or []]
    result.extend(str(value) for value in gate.get("landcover_source_objects") or [])
    for component in gate.get("coast_components") or []:
        if isinstance(component, dict):
            result.extend(
                str(value) for value in component.get("source_object_ids") or []
            )
    for entity in gate.get("river_entities") or []:
        if isinstance(entity, dict):
            result.extend(str(value) for value in entity.get("source_object_ids") or [])
    return result


def _nonuk_selection_gate(
    *,
    subject_id: str,
    extent: tuple[float, float, float, float],
) -> tuple[dict[str, Any], str] | None:
    if subject_id not in NONUK_AUDITED_SUBJECTS:
        return None
    manifest = _load_object(NONUK_SELECTION_PATH)
    if (
        manifest.get("id") != "hike-nonuk-osm-selection-v1"
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "frozen-audited-selection-gate"
    ):
        _fail("non-UK audited selection manifest has an unsupported schema")
    subjects = manifest.get("subjects")
    gate = subjects.get(subject_id) if isinstance(subjects, dict) else None
    if not isinstance(gate, dict):
        _fail(f"non-UK audited selection manifest has no {subject_id}")
    if list(extent) != gate.get("extent"):
        _fail(f"{subject_id} extent does not match its frozen non-UK selection gate")

    ordered_objects = _nonuk_manifest_objects(gate)
    if len(ordered_objects) != int(gate.get("selected_source_object_count") or -1):
        _fail(f"{subject_id} non-UK selection manifest object count is invalid")
    ordered_hash = hashlib.sha256(
        "\n".join(ordered_objects).encode("utf-8")
    ).hexdigest()
    if ordered_hash != gate.get("selected_source_object_manifest_sha256"):
        _fail(f"{subject_id} non-UK selection manifest object hash is invalid")
    quarantined = {str(value) for value in gate.get("quarantined_source_objects") or []}
    if quarantined.intersection(ordered_objects):
        _fail(f"{subject_id} selects a quarantined OSM source object")
    return gate, _canonical_sha256(manifest)


def _assert_nonuk_input_gate(
    *,
    subject_id: str,
    gate: dict[str, Any],
    evidence: Sequence[RawEvidence],
) -> None:
    expected_sources = gate.get("source_snapshots")
    if not isinstance(expected_sources, list):
        _fail(f"{subject_id} non-UK selection gate has invalid source evidence")

    def expected_key(item: dict[str, Any]) -> tuple[Any, ...]:
        bbox = item.get("header_bbox_wgs84")
        if not isinstance(bbox, dict):
            _fail(f"{subject_id} non-UK selection gate has invalid header bbox")
        return (
            str(item.get("snapshot_content_sha256") or ""),
            str(item.get("canonical_feature_sha256") or ""),
            int(item.get("canonical_feature_count") or -1),
            str(item.get("source_timestamp") or ""),
            tuple(float(bbox[field]) for field in ("west", "south", "east", "north")),
        )

    def actual_key(item: RawEvidence) -> tuple[Any, ...]:
        return (
            item.snapshot_sha256,
            item.canonical_extraction_sha256,
            len(item.features),
            str(item.metadata.get("source_timestamp") or ""),
            item.header_bbox,
        )

    expected = Counter(expected_key(item) for item in expected_sources)
    actual = Counter(actual_key(item) for item in evidence)
    if actual != expected:
        _fail(
            f"{subject_id} raw extraction(s) do not match the frozen non-UK "
            "snapshot/count/hash/header evidence"
        )


def _river_gate_key(item: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return (
        " ".join(str(item.get("name") or "").casefold().split()),
        tuple(sorted(str(value) for value in item.get("source_objects") or [])),
    )


def _assert_uk_selection_gate(
    *,
    subject_id: str,
    gate: dict[str, Any],
    landcover: Sequence[dict[str, Any]],
    areas: Sequence[dict[str, Any]],
    coastlines: Sequence[dict[str, Any]],
    rivers: Sequence[dict[str, Any]],
) -> None:
    expected_water = {str(value) for value in gate.get("water_source_objects") or []}
    actual_water = {str(item["source_object"]) for item in areas}
    expected_land = {str(value) for value in gate.get("landcover_source_objects") or []}
    actual_land = {str(item["source_object"]) for item in landcover}
    expected_rivers = Counter(
        _river_gate_key(item) for item in gate.get("river_entities") or []
    )
    actual_rivers = Counter(_river_gate_key(item) for item in rivers)
    expected_coast = {
        str(item["audit_chain_hash"]): (
            bool(item["closed"]),
            int(item["source_way_count"]),
            int(item["subpath_count"]),
        )
        for item in gate.get("coast_chains") or []
    }
    actual_coast = {
        str(item["audit_chain_hash"]): (
            bool(item["closed_chain"]),
            len(item["source_objects"]),
            len(item["paths"]),
        )
        for item in coastlines
    }
    actual_coast_by_hash = {str(item["audit_chain_hash"]): item for item in coastlines}
    failures: list[str] = []
    if actual_water != expected_water:
        failures.append(
            f"water missing={sorted(expected_water - actual_water)} "
            f"unexpected={sorted(actual_water - expected_water)}"
        )
    if actual_land != expected_land:
        failures.append(
            f"landcover missing={sorted(expected_land - actual_land)} "
            f"unexpected={sorted(actual_land - expected_land)}"
        )
    if actual_rivers != expected_rivers:
        failures.append("named river entities/source ways differ")
    if actual_coast != expected_coast:
        failures.append("coast chain hashes/closure/source counts differ")
    for expected in gate.get("coast_chains") or []:
        audit_hash = str(expected.get("audit_chain_hash") or "")
        actual = actual_coast_by_hash.get(audit_hash)
        if actual is None:
            continue
        expected_objects_hash = expected.get("source_objects_sha256")
        if (
            expected_objects_hash is not None
            and actual.get("source_objects_sha256") != expected_objects_hash
        ):
            failures.append(
                f"coast chain {audit_hash} source-object identity hash differs"
            )
        expected_geometry_hash = expected.get("source_geometry_sha256")
        if (
            expected_geometry_hash is not None
            and actual.get("source_geometry_sha256") != expected_geometry_hash
        ):
            failures.append(f"coast chain {audit_hash} source-geometry hash differs")
    if failures:
        _fail(f"{subject_id} failed frozen UK selection gate: {'; '.join(failures)}")


def _assert_nonuk_selection_gate(
    *,
    subject_id: str,
    gate: dict[str, Any],
    landcover: Sequence[dict[str, Any]],
    areas: Sequence[dict[str, Any]],
    coastlines: Sequence[dict[str, Any]],
    rivers: Sequence[dict[str, Any]],
) -> None:
    expected_water = {str(value) for value in gate.get("water_source_objects") or []}
    expected_land = {str(value) for value in gate.get("landcover_source_objects") or []}
    actual_water = {str(item["source_object"]) for item in areas}
    actual_land = {str(item["source_object"]) for item in landcover}
    expected_rivers = Counter(
        (
            str(item.get("normalized_name") or ""),
            tuple(sorted(str(value) for value in item.get("source_object_ids") or [])),
        )
        for item in gate.get("river_entities") or []
    )
    actual_rivers = Counter(
        (
            " ".join(str(item.get("name") or "").casefold().split()),
            tuple(sorted(str(value) for value in item.get("source_objects") or [])),
        )
        for item in rivers
    )
    expected_coast = {
        (
            str(item.get("component_hash") or ""),
            tuple(sorted(str(value) for value in item.get("source_object_ids") or [])),
        ): bool(item.get("closed"))
        for item in gate.get("coast_components") or []
    }
    actual_coast = {
        (
            str(item.get("audit_chain_hash") or ""),
            tuple(sorted(str(value) for value in item.get("source_objects") or [])),
        ): bool(item.get("closed_chain"))
        for item in coastlines
    }
    failures: list[str] = []
    if actual_water != expected_water:
        failures.append(
            f"water missing={sorted(expected_water - actual_water)} "
            f"unexpected={sorted(actual_water - expected_water)}"
        )
    if actual_land != expected_land:
        failures.append(
            f"landcover missing={sorted(expected_land - actual_land)} "
            f"unexpected={sorted(actual_land - expected_land)}"
        )
    if actual_rivers != expected_rivers:
        failures.append("named river entities/source ways differ")
    if actual_coast != expected_coast:
        failures.append("coast component identity/source ways differ")
    expected_counts = gate.get("expected")
    actual_counts = {
        "water": len(actual_water),
        "woodland": len(actual_land),
        "coast": len(coastlines),
        "rivers": len(rivers),
    }
    if actual_counts != expected_counts:
        failures.append(f"counts {actual_counts!r} != {expected_counts!r}")
    selected_objects = _selected_source_objects(landcover, areas, coastlines, rivers)
    quarantined = {str(value) for value in gate.get("quarantined_source_objects") or []}
    if quarantined.intersection(selected_objects):
        failures.append("quarantined source object leaked into selection")
    if subject_id == "RTE-ES-CAM-ES01C":
        if "relation/2588155" not in actual_water:
            failures.append("Fuente del Azufre relation was not retained")
        if "way/23609119" in actual_water:
            failures.append("Fuente del Azufre duplicate way was not suppressed")
    if failures:
        _fail(
            f"{subject_id} failed frozen non-UK selection gate: {'; '.join(failures)}"
        )


def _working_transformers(
    extent: tuple[float, float, float, float],
) -> tuple[Transformer, Transformer, str]:
    west, south, east, north = extent
    centre_lon = (west + east) / 2.0
    centre_lat = (south + north) / 2.0
    working = CRS.from_proj4(
        "+proj=aeqd "
        f"+lat_0={centre_lat:.8f} +lon_0={centre_lon:.8f} "
        "+datum=WGS84 +units=m +no_defs"
    )
    forward = Transformer.from_crs("EPSG:4326", working, always_xy=True)
    inverse = Transformer.from_crs(working, "EPSG:4326", always_xy=True)
    return forward, inverse, working.to_string()


def _densified_bbox_polygon(
    extent: tuple[float, float, float, float], *, segments_per_edge: int = 400
) -> Polygon:
    """Represent a geographic crop without projected four-corner chords."""

    if segments_per_edge < 1:
        _fail("segments_per_edge must be positive")
    west, south, east, north = extent
    points: list[tuple[float, float]] = []
    for index in range(segments_per_edge + 1):
        ratio = index / segments_per_edge
        points.append((west + (east - west) * ratio, south))
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        points.append((east, south + (north - south) * ratio))
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        points.append((east - (east - west) * ratio, north))
    for index in range(1, segments_per_edge):
        ratio = index / segments_per_edge
        points.append((west, north - (north - south) * ratio))
    return Polygon(points)


def _page_scale_mm_per_km(record: dict[str, Any]) -> float:
    """Match the renderer's no-profile A5 map scale for audit floors."""

    composition = record.get("composition")
    format_id = composition.get("format_id") if isinstance(composition, dict) else None
    if format_id == "a5-portrait":
        width, height = 118.0, 122.924
    elif format_id == "a5-landscape":
        width, height = 127.71, 118.0
    else:
        return 1.0
    west, south, east, north = _catalog_extent(record)
    cosine = math.cos(math.radians((south + north) / 2.0))
    centre_x = (west + east) * cosine / 2.0
    centre_y = (south + north) / 2.0
    context = record.get("context")
    rotation = (
        float(context.get("rotation_deg", 0.0)) if isinstance(context, dict) else 0.0
    )
    angle = math.radians(rotation)
    cosine_angle, sine_angle = math.cos(angle), math.sin(angle)
    corners: list[tuple[float, float]] = []
    for longitude in (west, east):
        for latitude in (south, north):
            x, y = longitude * cosine, latitude
            delta_x, delta_y = x - centre_x, y - centre_y
            corners.append(
                (
                    centre_x + delta_x * cosine_angle - delta_y * sine_angle,
                    centre_y + delta_x * sine_angle + delta_y * cosine_angle,
                )
            )
    span_x = max(point[0] for point in corners) - min(point[0] for point in corners)
    span_y = max(point[1] for point in corners) - min(point[1] for point in corners)
    scale_mm_per_degree = min(width / span_x, height / span_y)
    return scale_mm_per_degree / 111.32


def _route_geometry(record: dict[str, Any], forward: Transformer) -> BaseGeometry:
    route = record.get("route")
    segments = route.get("segments") if isinstance(route, dict) else None
    if not isinstance(segments, list):
        _fail(f"{record.get('id')} has no route segments")
    lines: list[LineString] = []
    for segment in segments:
        points = segment.get("points") if isinstance(segment, dict) else None
        if not isinstance(points, list) or len(points) < 2:
            continue
        try:
            line = LineString([(float(point[0]), float(point[1])) for point in points])
        except (IndexError, TypeError, ValueError):
            continue
        if not line.is_empty and line.length > 0.0:
            lines.append(line)
    if not lines:
        _fail(f"{record.get('id')} has no usable route geometry")
    return transform(forward.transform, unary_union(lines))


def _coverage_evidence(
    *,
    subject_id: str,
    record: dict[str, Any],
    extent: tuple[float, float, float, float],
    coverage_union: BaseGeometry,
    evidence: Sequence[RawEvidence],
    forward: Transformer,
    audited_gate: dict[str, Any] | None,
) -> CoverageEvidence:
    crop = box(*extent)
    uncovered = crop.difference(coverage_union)
    uncovered_area = 0.0 if uncovered.is_empty else float(uncovered.area)
    crop_area = max(float(crop.area), 1e-15)
    crop_fraction = max(0.0, min(1.0, 1.0 - uncovered_area / crop_area))

    route = _route_geometry(record, forward)
    coverage_polygons = [
        transform(
            forward.transform,
            _densified_bbox_polygon(
                item.header_bbox if audited_gate is not None else item.requested_bbox
            ),
        )
        for item in evidence
    ]
    projected_coverage = unary_union(coverage_polygons)
    route_length = float(route.length)
    if route_length <= 0.0:
        _fail(f"{subject_id} has no measurable route for coverage proof")
    covered_route_length = float(route.intersection(projected_coverage).length)
    route_fraction = max(0.0, min(1.0, covered_route_length / route_length))
    route_fully_covered = route_fraction >= 1.0 - 1e-9
    full_crop = crop_fraction >= 1.0 - 1e-12

    if audited_gate is None:
        if not full_crop:
            _fail(
                "the union of proven raw-extraction bboxes does not cover the "
                "catalog context extent"
            )
        return CoverageEvidence(
            crop_coverage_fraction=1.0,
            crop_uncovered_area_degrees2=0.0,
            route_coverage_fraction=1.0,
            route_fully_covered=True,
            full_crop_coverage_proven=True,
            absence_claims_safe=True,
        )

    expected = audited_gate.get("coverage")
    if not isinstance(expected, dict):
        _fail(f"{subject_id} non-UK selection gate has invalid coverage evidence")
    expected_crop = float(expected.get("crop_coverage_fraction") or 0.0)
    expected_uncovered = float(expected.get("crop_uncovered_area_degrees2") or 0.0)
    if not math.isclose(crop_fraction, expected_crop, rel_tol=0.0, abs_tol=1e-9):
        _fail(f"{subject_id} crop coverage fraction differs from its frozen audit")
    if not math.isclose(uncovered_area, expected_uncovered, rel_tol=0.0, abs_tol=1e-9):
        _fail(f"{subject_id} uncovered crop area differs from its frozen audit")
    if not route_fully_covered or expected.get("route_fully_covered") is not True:
        _fail(f"{subject_id} route is not fully covered by the frozen PBF source(s)")

    expected_absence_safe = expected.get("absence_claims_safe") is True
    if subject_id in PARTIAL_CROP_AUDITED_SUBJECTS:
        if full_crop or expected_absence_safe:
            _fail(f"{subject_id} partial-crop audit must not certify absence claims")
    elif not full_crop or not expected_absence_safe:
        _fail(f"{subject_id} audited preset requires proven full-crop coverage")

    return CoverageEvidence(
        crop_coverage_fraction=crop_fraction,
        crop_uncovered_area_degrees2=uncovered_area,
        route_coverage_fraction=route_fraction,
        route_fully_covered=route_fully_covered,
        full_crop_coverage_proven=full_crop,
        absence_claims_safe=expected_absence_safe,
    )


def _resolve_selection(
    crop: BaseGeometry, options: SelectionOptions
) -> ResolvedSelection:
    min_x, min_y, max_x, max_y = crop.bounds
    diagonal = math.hypot(max_x - min_x, max_y - min_y)
    crop_area = max(float(crop.area), 1.0)

    def positive(value: float | None, default: float, label: str) -> float:
        result = default if value is None else float(value)
        if not math.isfinite(result) or result <= 0.0:
            _fail(f"{label} must be a positive finite number")
        return result

    for label, cap in (
        ("max_landcover_features", options.max_landcover_features),
        ("max_water_areas", options.max_water_areas),
        ("max_coastline_chains", options.max_coastline_chains),
        ("max_river_entities", options.max_river_entities),
    ):
        if cap <= 0:
            _fail(f"{label} must be positive")
    return ResolvedSelection(
        simplification_m=positive(
            options.simplification_m,
            max(25.0, min(450.0, diagonal / 1_200.0)),
            "simplification_m",
        ),
        minimum_landcover_area_m2=positive(
            options.minimum_landcover_area_m2,
            max(50_000.0, min(12_000_000.0, crop_area / 20_000.0)),
            "minimum_landcover_area_m2",
        ),
        minimum_water_area_m2=positive(
            options.minimum_water_area_m2,
            max(50_000.0, min(12_000_000.0, crop_area / 30_000.0)),
            "minimum_water_area_m2",
        ),
        route_context_distance_m=positive(
            options.river_context_distance_m,
            max(3_000.0, min(30_000.0, diagonal * 0.06)),
            "river_context_distance_m",
        ),
        minimum_river_length_m=positive(
            options.minimum_river_length_m,
            max(800.0, min(15_000.0, diagonal * 0.008)),
            "minimum_river_length_m",
        ),
        minimum_coastline_length_m=positive(
            options.minimum_coastline_length_m,
            max(500.0, min(20_000.0, diagonal * 0.003)),
            "minimum_coastline_length_m",
        ),
        minimum_closed_coastline_area_m2=positive(
            options.minimum_closed_coastline_area_m2,
            max(2_500.0, crop_area * 0.000025),
            "minimum_closed_coastline_area_m2",
        ),
        max_landcover_features=options.max_landcover_features,
        max_water_areas=options.max_water_areas,
        max_coastline_chains=options.max_coastline_chains,
        max_river_entities=options.max_river_entities,
    )


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
    """Convert canonical PBF [latitude, longitude] to geometry x/y."""

    try:
        return [(float(point[1]), float(point[0])) for point in raw]
    except (IndexError, TypeError, ValueError):
        return []


def _closed_ring(points: Iterable[Sequence[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for x, y in points:
        point = [round(float(x), 6), round(float(y), 6)]
        if not result or point != result[-1]:
            result.append(point)
    if result and result[0] != result[-1]:
        result.append(list(result[0]))
    return result


def _open_line(points: Iterable[Sequence[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for x, y in points:
        point = [round(float(x), 6), round(float(y), 6)]
        if not result or point != result[-1]:
            result.append(point)
    return result


def _source_object(feature: dict[str, Any]) -> str:
    object_type = str(feature.get("osm_type") or "")
    identifier = str(feature.get("osm_id") or "")
    if object_type not in {"node", "way", "relation"} or not identifier.isdigit():
        _fail("raw feature has an invalid canonical OSM object identifier")
    if int(identifier) <= 0:
        _fail("raw feature has an invalid canonical OSM object identifier")
    return f"{object_type}/{identifier}"


def _feature_sort_key(feature: dict[str, Any]) -> tuple[str, int, str]:
    identifier = str(feature.get("osm_id") or "0")
    return (
        str(feature.get("osm_type") or ""),
        int(identifier) if identifier.isdigit() else 0,
        str(feature.get("part") or ""),
    )


def _source_feature_payload(feature: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": feature.get("layer"),
        "points": feature.get("points"),
        "osm_type": feature.get("osm_type"),
        "osm_id": feature.get("osm_id"),
        "part": feature.get("part"),
        "tags": feature.get("tags"),
        "geometry_type": feature.get("geometry_type"),
        "ring_role": feature.get("ring_role"),
        "outer_ring_part": feature.get("outer_ring_part"),
    }


def _feature_snapshot_hashes(feature: dict[str, Any]) -> set[str]:
    values = feature.get("_input_snapshot_sha256s")
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if SHA256.fullmatch(str(value))}


def _polygon_groups(
    features: Sequence[dict[str, Any]], layer: str
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        if (
            feature.get("layer") == layer
            and feature.get("geometry_type") == "polygon_ring"
        ):
            grouped[(str(feature.get("osm_type")), str(feature.get("osm_id")))].append(
                feature
            )
    result: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for key in sorted(grouped):
        parts = sorted(grouped[key], key=_feature_sort_key)
        inners: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for part in parts:
            if part.get("ring_role") == "inner":
                inners[str(part.get("outer_ring_part"))].append(part)
        for outer in parts:
            if outer.get("ring_role") == "outer":
                result.append((outer, inners.get(str(outer.get("part")), [])))
    return result


def _polygon_object_groups(
    features: Sequence[dict[str, Any]], layer: str
) -> list[list[tuple[dict[str, Any], list[dict[str, Any]]]]]:
    """Group every outer ring of one canonical OSM object for selection."""

    grouped: dict[
        tuple[str, str], list[tuple[dict[str, Any], list[dict[str, Any]]]]
    ] = defaultdict(list)
    for outer, inners in _polygon_groups(features, layer):
        grouped[(str(outer.get("osm_type")), str(outer.get("osm_id")))].append(
            (outer, inners)
        )
    return [
        sorted(grouped[key], key=lambda item: _feature_sort_key(item[0]))
        for key in sorted(grouped)
    ]


def _polygon_object_source_hash(
    parts: Sequence[tuple[dict[str, Any], Sequence[dict[str, Any]]]],
) -> str:
    return _canonical_sha256(
        [
            {
                "outer": _source_feature_payload(outer),
                "inners": [
                    _source_feature_payload(inner)
                    for inner in sorted(inners, key=_feature_sort_key)
                ],
            }
            for outer, inners in parts
        ]
    )


def _polygon_object_snapshot_hashes(
    parts: Sequence[tuple[dict[str, Any], Sequence[dict[str, Any]]]],
) -> list[str]:
    hashes: set[str] = set()
    for outer, inners in parts:
        hashes.update(_feature_snapshot_hashes(outer))
        for inner in inners:
            hashes.update(_feature_snapshot_hashes(inner))
    return sorted(hashes)


def _projected_polygon(
    outer: dict[str, Any],
    inners: Sequence[dict[str, Any]],
    *,
    forward: Transformer,
) -> BaseGeometry:
    outer_points = _source_points(outer.get("points") or [])
    inner_points = [_source_points(inner.get("points") or []) for inner in inners]
    if len(outer_points) < 4:
        return Polygon()
    try:
        geometry = make_valid(Polygon(outer_points, inner_points))
    except (TypeError, ValueError):
        return Polygon()
    return transform(forward.transform, geometry)


def _polygon_source_hash(
    outer: dict[str, Any], inners: Sequence[dict[str, Any]]
) -> str:
    return _canonical_sha256(
        {
            "outer": _source_feature_payload(outer),
            "inners": [
                _source_feature_payload(inner)
                for inner in sorted(inners, key=_feature_sort_key)
            ],
        }
    )


def _polygon_output(
    *,
    prefix: str,
    semantic_class: str,
    name: str | None,
    outer: dict[str, Any],
    inners: Sequence[dict[str, Any]],
    polygon: Polygon,
    unsimplified_area_m2: float,
    route_distance_m: float,
    inverse: Transformer,
    part_index: int,
) -> dict[str, Any] | None:
    geographic = transform(inverse.transform, polygon)
    if geographic.geom_type != "Polygon":
        return None
    outer_ring = _closed_ring(geographic.exterior.coords)  # type: ignore[attr-defined]
    if len(outer_ring) < 4:
        return None
    holes = [
        ring
        for interior in geographic.interiors  # type: ignore[attr-defined]
        if len(ring := _closed_ring(interior.coords)) >= 4
    ]
    source_object = _source_object(outer)
    identifier = source_object.replace("/", "-")
    derived_payload = {"outer": outer_ring, "holes": holes}
    source_snapshots = _feature_snapshot_hashes(outer)
    for inner in inners:
        source_snapshots.update(_feature_snapshot_hashes(inner))
    return {
        "id": f"{prefix}-{identifier}-{str(outer.get('part') or 'outer').replace(':', '-')}-{part_index:02d}",
        "class": semantic_class,
        "source_object": source_object,
        "source_part": str(outer.get("part") or ""),
        "source_snapshot_sha256s": sorted(source_snapshots),
        "name": name,
        "area_m2": round(unsimplified_area_m2, 1),
        "distance_to_route_m": round(route_distance_m, 1),
        "source_hole_count": len(inners),
        "outer": outer_ring,
        "holes": holes,
        "source_geometry_sha256": _polygon_source_hash(outer, inners),
        "derived_geometry_sha256": _canonical_sha256(derived_payload),
    }


def _landcover_class(tags: dict[str, Any]) -> str | None:
    if str(tags.get("landuse") or "") == "forest":
        return "forest"
    if str(tags.get("natural") or "") == "wood":
        return "woodland"
    return None


def _derive_landcover(
    features: Sequence[dict[str, Any]],
    *,
    subject_id: str,
    mm_per_km: float,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
    selection: ResolvedSelection,
    audited_source_objects: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates: list[tuple[tuple[float, float, str], list[dict[str, Any]]]] = []
    factual_source_objects: set[str] = set()
    audited_order = (
        {str(value): index for index, value in enumerate(audited_source_objects)}
        if audited_source_objects is not None
        else None
    )
    for object_parts in _polygon_object_groups(features, "green_space"):
        outer, _ = object_parts[0]
        tags = outer.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        semantic_class = _landcover_class(tags)
        if semantic_class is None:
            continue
        source_object = _source_object(outer)
        factual_source_objects.add(source_object)
        if audited_order is not None and source_object not in audited_order:
            continue
        projected_parts = [
            (
                part_outer,
                part_inners,
                make_valid(
                    _projected_polygon(
                        part_outer,
                        part_inners,
                        forward=forward,
                    ).intersection(crop)
                ),
            )
            for part_outer, part_inners in object_parts
        ]
        aggregate = make_valid(
            unary_union(
                [
                    geometry
                    for _, _, geometry in projected_parts
                    if not geometry.is_empty
                ]
            )
        )
        polygons = _geometry_polygons(aggregate)
        if not polygons:
            continue
        aggregate = unary_union(polygons)
        area_m2 = float(aggregate.area)
        distance_m = float(aggregate.distance(route))
        name = str(tags.get("name") or "").strip() or None
        minimum = selection.minimum_landcover_area_m2
        if audited_order is not None:
            keep = True
        elif subject_id == HEB_SUBJECT_ID:
            # Hebridean context must describe the route islands, not large
            # forests on Skye or the mainland that happen to intersect the
            # rectangular extraction.  Named woodland gets a slightly wider
            # context radius so Aline Community Woodland remains factual.
            keep = (distance_m <= 2_500.0 and area_m2 >= 500_000.0) or (
                name is not None and distance_m <= 6_000.0 and area_m2 >= 300_000.0
            )
        elif subject_id == "RTE-GB-JMW-WALK-01":
            keep = (
                (area_m2 >= 8_000_000.0 and distance_m <= 10_000.0)
                or (distance_m <= 2_500.0 and area_m2 >= 500_000.0)
                or (name is not None and distance_m <= 6_000.0 and area_m2 >= 300_000.0)
            )
        elif subject_id in UK_AUDITED_SUBJECTS:
            keep = (
                area_m2 >= 8_000_000.0
                or (distance_m <= 2_500.0 and area_m2 >= 500_000.0)
                or (name is not None and distance_m <= 6_000.0 and area_m2 >= 300_000.0)
            )
        else:
            keep = (
                area_m2 >= minimum * 6.0
                or (
                    area_m2 >= minimum
                    and distance_m <= selection.route_context_distance_m
                )
                or (
                    name is not None
                    and area_m2 >= minimum * 0.6
                    and distance_m <= selection.route_context_distance_m * 2.0
                )
            )
        if not keep:
            continue
        source_hash = _polygon_object_source_hash(object_parts)
        snapshot_hashes = _polygon_object_snapshot_hashes(object_parts)
        source_hole_count = sum(len(inners) for _, inners in object_parts)
        outputs: list[dict[str, Any]] = []
        for outer_index, (part_outer, part_inners, geometry) in enumerate(
            projected_parts
        ):
            for part_index, polygon in enumerate(_geometry_polygons(geometry)):
                part_area_m2 = float(polygon.area)
                part_physical_area_mm2 = part_area_m2 / 1_000_000.0 * mm_per_km**2
                minimum_part_area_mm2 = (
                    HEB_MIN_LANDCOVER_PART_MM2 if subject_id == HEB_SUBJECT_ID else 0.01
                )
                if (
                    audited_order is None
                    and part_physical_area_mm2 < minimum_part_area_mm2
                ):
                    continue
                simplified = make_valid(
                    polygon.simplify(
                        selection.simplification_m,
                        preserve_topology=True,
                    )
                )
                for simplified_index, simple_polygon in enumerate(
                    _geometry_polygons(simplified)
                ):
                    output = _polygon_output(
                        prefix="landcover",
                        semantic_class=semantic_class,
                        name=name,
                        outer=part_outer,
                        inners=part_inners,
                        polygon=simple_polygon,
                        unsimplified_area_m2=area_m2,
                        route_distance_m=distance_m,
                        inverse=inverse,
                        part_index=(
                            outer_index * 10_000 + part_index * 100 + simplified_index
                        ),
                    )
                    if output is None:
                        continue
                    output["part_area_m2"] = round(part_area_m2, 1)
                    output["source_object_physical_area_mm2"] = round(
                        area_m2 / 1_000_000.0 * mm_per_km**2,
                        3,
                    )
                    output["part_physical_area_mm2"] = round(part_physical_area_mm2, 3)
                    output["source_hole_count"] = source_hole_count
                    output["source_object_geometry_sha256"] = source_hash
                    output["source_snapshot_sha256s"] = snapshot_hashes
                    outputs.append(output)
        if not outputs:
            continue
        outputs.sort(key=lambda item: str(item["id"]))
        if audited_order is not None:
            score = (float(audited_order[source_object]), -area_m2, source_object)
        elif subject_id == HEB_SUBJECT_ID:
            score = (distance_m, -area_m2, source_object)
        elif subject_id in UK_AUDITED_SUBJECTS:
            score = (-area_m2, distance_m, source_object)
        else:
            score = (
                distance_m / max(selection.route_context_distance_m, 1.0)
                - math.log1p(area_m2 / max(minimum, 1.0)) * 0.45,
                -area_m2,
                source_object,
            )
        candidates.append((score, outputs))
    candidates.sort(key=lambda item: item[0])
    if audited_order is not None:
        selected_objects = candidates
    else:
        cap = (
            64
            if subject_id in UK_AUDITED_SUBJECTS
            else selection.max_landcover_features
        )
        selected_objects = candidates[:cap]
    selected = [output for _, outputs in selected_objects for output in outputs]
    if audited_order is not None:
        actual = {str(item["source_object"]) for item in selected}
        expected = set(audited_order)
        if actual != expected:
            _fail(
                f"{subject_id} audited landcover missing={sorted(expected - actual)} "
                f"unexpected={sorted(actual - expected)}"
            )
    return selected, {
        "factual_source_objects": len(factual_source_objects),
        "eligible_source_objects": len(candidates),
        "selected_source_objects": len(selected_objects),
        "eligible_derived_parts": sum(len(outputs) for _, outputs in candidates),
        "selected_derived_parts": len(selected),
    }


def _water_class(tags: dict[str, Any]) -> str | None:
    natural = str(tags.get("natural") or "")
    water = str(tags.get("water") or "")
    landuse = str(tags.get("landuse") or "")
    waterway = str(tags.get("waterway") or "")
    # Linear-water area polygons are deliberately omitted. Drawing their banks
    # as well as the selected named centreline creates a false double stroke.
    if waterway == "riverbank" or water in {"river", "stream", "canal"}:
        return None
    if landuse == "reservoir" or water == "reservoir":
        return "reservoir"
    if natural != "water":
        return None
    if water in {"pond", "basin", "wastewater", "lagoon", "lock"}:
        return None
    return "lake"


def _derive_water_areas(
    features: Sequence[dict[str, Any]],
    *,
    subject_id: str,
    mm_per_km: float,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
    selection: ResolvedSelection,
    audited_source_objects: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates: list[tuple[tuple[float, float, str], list[dict[str, Any]]]] = []
    factual_source_objects: set[str] = set()
    audited_order = (
        {str(value): index for index, value in enumerate(audited_source_objects)}
        if audited_source_objects is not None
        else None
    )
    for object_parts in _polygon_object_groups(features, "water_areas"):
        outer, _ = object_parts[0]
        tags = outer.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        semantic_class = _water_class(tags)
        if semantic_class is None:
            continue
        source_object = _source_object(outer)
        factual_source_objects.add(source_object)
        if audited_order is not None and source_object not in audited_order:
            continue
        projected_parts = [
            (
                part_outer,
                part_inners,
                make_valid(
                    _projected_polygon(
                        part_outer,
                        part_inners,
                        forward=forward,
                    ).intersection(crop)
                ),
            )
            for part_outer, part_inners in object_parts
        ]
        aggregate = make_valid(
            unary_union(
                [
                    geometry
                    for _, _, geometry in projected_parts
                    if not geometry.is_empty
                ]
            )
        )
        polygons = _geometry_polygons(aggregate)
        if not polygons:
            continue
        aggregate = unary_union(polygons)
        area_m2 = float(aggregate.area)
        distance_m = float(aggregate.distance(route))
        name = str(tags.get("name") or "").strip() or None
        minimum = selection.minimum_water_area_m2
        if audited_order is not None:
            keep = True
        elif subject_id == "RTE-GB-HEB-WALK-01":
            keep = area_m2 >= 2_500_000.0 or (
                distance_m <= 3_000.0 and area_m2 >= 500_000.0
            )
        elif subject_id == "RTE-GB-GGW-01":
            keep = (
                (area_m2 >= 15_000_000.0 and distance_m <= 10_000.0)
                or (name is not None and distance_m <= 8_000.0 and area_m2 >= 750_000.0)
                or (distance_m <= 3_000.0 and area_m2 >= 500_000.0)
            )
        elif subject_id == "RTE-GB-JMW-WALK-01":
            keep = (
                area_m2 >= 5_000_000.0
                or (name is not None and distance_m <= 3_000.0 and area_m2 >= 250_000.0)
                or (name is not None and distance_m <= 8_000.0 and area_m2 >= 750_000.0)
            )
        else:
            keep = area_m2 >= minimum and (
                distance_m <= selection.route_context_distance_m
                or (
                    area_m2 >= minimum * 12.0
                    and distance_m <= selection.route_context_distance_m * 2.0
                )
            )
        if not keep:
            continue
        source_hash = _polygon_object_source_hash(object_parts)
        snapshot_hashes = _polygon_object_snapshot_hashes(object_parts)
        source_hole_count = sum(len(inners) for _, inners in object_parts)
        outputs: list[dict[str, Any]] = []
        for outer_index, (part_outer, part_inners, geometry) in enumerate(
            projected_parts
        ):
            for part_index, polygon in enumerate(_geometry_polygons(geometry)):
                part_area_m2 = float(polygon.area)
                if (
                    audited_order is None
                    and part_area_m2 / 1_000_000.0 * mm_per_km**2 < 0.01
                ):
                    continue
                simplified = make_valid(
                    polygon.simplify(
                        selection.simplification_m,
                        preserve_topology=True,
                    )
                )
                for simplified_index, simple_polygon in enumerate(
                    _geometry_polygons(simplified)
                ):
                    output = _polygon_output(
                        prefix="water",
                        semantic_class=semantic_class,
                        name=name,
                        outer=part_outer,
                        inners=part_inners,
                        polygon=simple_polygon,
                        unsimplified_area_m2=area_m2,
                        route_distance_m=distance_m,
                        inverse=inverse,
                        part_index=(
                            outer_index * 10_000 + part_index * 100 + simplified_index
                        ),
                    )
                    if output is None:
                        continue
                    output["part_area_m2"] = round(part_area_m2, 1)
                    output["source_object_physical_area_mm2"] = round(
                        area_m2 / 1_000_000.0 * mm_per_km**2,
                        3,
                    )
                    output["part_physical_area_mm2"] = round(
                        part_area_m2 / 1_000_000.0 * mm_per_km**2,
                        3,
                    )
                    output["source_hole_count"] = source_hole_count
                    output["source_object_geometry_sha256"] = source_hash
                    output["source_snapshot_sha256s"] = snapshot_hashes
                    outputs.append(output)
        if not outputs:
            continue
        outputs.sort(key=lambda item: str(item["id"]))
        if audited_order is not None:
            score = (float(audited_order[source_object]), -area_m2, source_object)
        elif subject_id in UK_AUDITED_SUBJECTS:
            score = (-area_m2, distance_m, source_object)
        else:
            score = (
                distance_m / max(selection.route_context_distance_m, 1.0)
                - math.log1p(area_m2 / max(minimum, 1.0)) * 0.3
                - (0.15 if name is not None else 0.0),
                -area_m2,
                source_object,
            )
        candidates.append((score, outputs))
    candidates.sort(key=lambda item: item[0])
    selected_objects = (
        candidates
        if audited_order is not None
        else candidates[: selection.max_water_areas]
    )
    selected = [output for _, outputs in selected_objects for output in outputs]
    if audited_order is not None:
        actual = {str(item["source_object"]) for item in selected}
        expected = set(audited_order)
        if actual != expected:
            _fail(
                f"{subject_id} audited water missing={sorted(expected - actual)} "
                f"unexpected={sorted(actual - expected)}"
            )
    return selected, {
        "factual_source_objects": len(factual_source_objects),
        "eligible_source_objects": len(candidates),
        "selected_source_objects": len(selected_objects),
        "eligible_derived_parts": sum(len(outputs) for _, outputs in candidates),
        "selected_derived_parts": len(selected),
    }


def _line_components(
    features: Sequence[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    if not features:
        return []
    ordered = sorted(features, key=_feature_sort_key)
    parents = list(range(len(ordered)))

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

    endpoints: dict[tuple[float, float], list[int]] = defaultdict(list)
    for index, feature in enumerate(ordered):
        raw_points = feature.get("points") or []
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            continue
        try:
            endpoints[(float(raw_points[0][0]), float(raw_points[0][1]))].append(index)
            endpoints[(float(raw_points[-1][0]), float(raw_points[-1][1]))].append(
                index
            )
        except (IndexError, TypeError, ValueError):
            continue
    for members in endpoints.values():
        for member in members[1:]:
            union(members[0], member)
    components: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, feature in enumerate(ordered):
        components[find(index)].append(feature)
    return sorted(
        (sorted(component, key=_feature_sort_key) for component in components.values()),
        key=lambda component: _feature_sort_key(component[0]),
    )


def _component_geometry(
    component: Sequence[dict[str, Any]],
) -> BaseGeometry:
    lines = [
        LineString(points)
        for feature in component
        if len(points := _source_points(feature.get("points") or [])) >= 2
    ]
    if not lines:
        return MultiLineString([])
    if len(lines) == 1:
        return lines[0]
    try:
        return linemerge(MultiLineString(lines))
    except (TypeError, ValueError):
        return unary_union(lines)


def _component_is_closed(component: Sequence[dict[str, Any]]) -> bool:
    endpoint_degrees: dict[tuple[float, float], int] = defaultdict(int)
    for feature in component:
        raw_points = feature.get("points") or []
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            return False
        try:
            first = (float(raw_points[0][0]), float(raw_points[0][1]))
            last = (float(raw_points[-1][0]), float(raw_points[-1][1]))
        except (IndexError, TypeError, ValueError):
            return False
        endpoint_degrees[first] += 1
        endpoint_degrees[last] += 1
    return bool(endpoint_degrees) and all(
        degree == 2 for degree in endpoint_degrees.values()
    )


def _closed_component_area_m2(geometry: BaseGeometry) -> float:
    area_m2 = 0.0
    for line in _geometry_lines(geometry):
        if not line.is_ring or len(line.coords) < 4:
            continue
        polygon = make_valid(Polygon(line.coords))
        area_m2 += sum(float(part.area) for part in _geometry_polygons(polygon))
    return area_m2


def _component_source_hash(component: Sequence[dict[str, Any]]) -> str:
    return _canonical_sha256(
        [_source_feature_payload(feature) for feature in component]
    )


def _component_snapshot_hashes(component: Sequence[dict[str, Any]]) -> list[str]:
    hashes: set[str] = set()
    for feature in component:
        hashes.update(_feature_snapshot_hashes(feature))
    return sorted(hashes)


def _derived_line_paths(
    geometry: BaseGeometry,
    *,
    inverse: Transformer,
    simplification_m: float,
) -> tuple[list[list[list[float]]], float]:
    paths: list[list[list[float]]] = []
    total_length_m = 0.0
    for line in _geometry_lines(geometry):
        total_length_m += float(line.length)
        simplified = line.simplify(simplification_m, preserve_topology=True)
        for part in _geometry_lines(simplified):
            path = _open_line(transform(inverse.transform, part).coords)
            if len(path) >= 2:
                paths.append(path)
    paths.sort(key=_canonical_sha256)
    return paths, total_length_m


def _derive_coastlines(
    features: Sequence[dict[str, Any]],
    *,
    subject_id: str,
    mm_per_km: float,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
    selection: ResolvedSelection,
    audited_components: Sequence[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates = [
        feature
        for feature in features
        if feature.get("layer") == "waterways"
        and feature.get("geometry_type") == "line"
        and isinstance(feature.get("tags"), dict)
        and feature["tags"].get("natural") == "coastline"
    ]
    outputs: list[tuple[tuple[float, float, str], dict[str, Any]]] = []
    components = _line_components(candidates)
    for component in components:
        source_objects = sorted({_source_object(feature) for feature in component})
        closed_chain = _component_is_closed(component)
        geometry = transform(forward.transform, _component_geometry(component))
        enclosed_area_m2 = _closed_component_area_m2(geometry) if closed_chain else 0.0
        if (
            audited_components is None
            and subject_id not in UK_AUDITED_SUBJECTS
            and closed_chain
            and enclosed_area_m2 < selection.minimum_closed_coastline_area_m2
        ):
            continue
        clipped = make_valid(geometry.intersection(crop))
        if clipped.is_empty:
            continue
        paths, length_m = _derived_line_paths(
            clipped,
            inverse=inverse,
            simplification_m=selection.simplification_m,
        )
        if not paths or (
            audited_components is None
            and length_m < selection.minimum_coastline_length_m
        ):
            continue
        distance_m = float(geometry.distance(route))
        if audited_components is None and subject_id in UK_AUDITED_SUBJECTS:
            if not closed_chain and length_m / 1_000.0 * mm_per_km < 0.75:
                continue
            if closed_chain and (enclosed_area_m2 / 1_000_000.0 * mm_per_km**2 < 0.25):
                continue
            if (
                subject_id == "RTE-GB-HEB-WALK-01"
                and closed_chain
                and enclosed_area_m2 < 10_000_000.0
                and distance_m > 2_000.0
            ):
                continue
            if subject_id == HEB_SUBJECT_ID and distance_m > HEB_ROUTE_CONTEXT_LIMIT_M:
                # The expanded rectangular crop necessarily intersects Skye
                # and a mainland/southern edge chain.  Neither is Hebridean
                # Way context.  Keep the evidence in the raw extraction, but
                # only plot coast components genuinely near the route.
                continue
        chain_hash = hashlib.sha256(
            ",".join(source_objects).encode("ascii")
        ).hexdigest()[:16]
        audit_chain_hash = hashlib.sha256(
            ",".join(
                str(identifier)
                for identifier in sorted(
                    int(source_object.partition("/")[2])
                    for source_object in source_objects
                )
            ).encode("ascii")
        ).hexdigest()[:12]
        item: dict[str, Any] = {
            "id": f"coast-chain-{chain_hash}",
            "chain_hash": chain_hash,
            "audit_chain_hash": audit_chain_hash,
            "closed_chain": closed_chain,
            "source_object": source_objects[0],
            "source_objects": source_objects,
            "source_objects_sha256": _canonical_sha256(source_objects),
            "source_snapshot_sha256s": _component_snapshot_hashes(component),
            "length_m": round(length_m, 1),
            "distance_to_route_m": round(distance_m, 1),
            "paths": paths,
            "source_geometry_sha256": _component_source_hash(component),
            "derived_geometry_sha256": _canonical_sha256(paths),
        }
        if closed_chain:
            item["enclosed_area_m2"] = round(enclosed_area_m2, 1)
            item["estimated_page_area_mm2"] = round(
                enclosed_area_m2 / max(float(crop.area), 1.0) * 10_000.0,
                3,
            )
        outputs.append(((distance_m, -length_m, str(item["id"])), item))
    outputs.sort(key=lambda item: item[0])
    # A main-land/open coast carries more route-location information than an
    # offshore island loop.  Preserve every eligible open chain up to the cap,
    # then use the remaining plot budget for closed islands in ranked order.
    # This prevents a dense island group from displacing the coast itself.
    open_outputs = [item for item in outputs if item[1]["closed_chain"] is False]
    closed_outputs = [item for item in outputs if item[1]["closed_chain"] is True]
    if audited_components is not None:
        by_identity = {
            (
                str(item["audit_chain_hash"]),
                tuple(sorted(str(value) for value in item["source_objects"])),
            ): item
            for _, item in outputs
        }
        selected = []
        for expected in audited_components:
            expected_hash = str(expected.get("component_hash") or "")
            expected_objects = tuple(
                sorted(str(value) for value in expected.get("source_object_ids") or [])
            )
            expected_item = by_identity.get((expected_hash, expected_objects))
            if expected_item is None:
                _fail(
                    f"{subject_id} is missing audited coast component {expected_hash}"
                )
            if bool(expected_item["closed_chain"]) is not bool(expected.get("closed")):
                _fail(f"{subject_id} audited coast closure state differs")
            selected.append(expected_item)
    else:
        if subject_id == HEB_SUBJECT_ID:
            selected_outputs = sorted(
                outputs,
                key=lambda item: (
                    float(item[1]["distance_to_route_m"]),
                    -float(item[1].get("enclosed_area_m2") or 0.0),
                    -float(item[1]["length_m"]),
                    str(item[1]["id"]),
                ),
            )[:HEB_MAX_COASTLINE_CHAINS]
        elif subject_id in UK_AUDITED_SUBJECTS:
            cap = 8
            ranked_open = sorted(
                open_outputs,
                key=lambda item: (
                    -float(item[1]["length_m"]),
                    str(item[1]["id"]),
                ),
            )
            ranked_closed = sorted(
                closed_outputs,
                key=lambda item: (
                    -float(item[1].get("enclosed_area_m2") or 0.0),
                    -float(item[1]["length_m"]),
                    str(item[1]["id"]),
                ),
            )
            selected_outputs = ranked_open[:cap]
            selected_outputs.extend(
                ranked_closed[: max(cap - len(selected_outputs), 0)]
            )
        else:
            selected_outputs = open_outputs[: selection.max_coastline_chains]
            remaining = selection.max_coastline_chains - len(selected_outputs)
            if remaining > 0:
                selected_outputs.extend(closed_outputs[:remaining])
            selected_outputs.sort(key=lambda item: item[0])
        selected = [item[1] for item in selected_outputs]
    return selected, {
        "raw_source_ways": len(candidates),
        "stitched_components": len(components),
        "eligible_chains": len(outputs),
        "eligible_open_chains": len(open_outputs),
        "eligible_closed_chains": len(closed_outputs),
        "selected_chains": len(selected),
        "selected_open_chains": sum(item["closed_chain"] is False for item in selected),
        "selected_closed_chains": sum(
            item["closed_chain"] is True for item in selected
        ),
    }


def _river_identity(feature: dict[str, Any]) -> tuple[str, str] | None:
    tags = feature.get("tags")
    if not isinstance(tags, dict) or tags.get("waterway") != "river":
        return None
    name = str(tags.get("name") or "").strip()
    if not name:
        return None
    normalized_name = " ".join(name.casefold().split())
    return normalized_name, name


def _river_components(
    features: Sequence[dict[str, Any]], *, name: str
) -> list[list[dict[str, Any]]]:
    """Stitch endpoints first, then reunite source gaps with one identity.

    Wikidata is deliberately not the initial grouping key: ways missing that
    optional tag must still join their touching neighbours.  Once exact source
    endpoints have formed components, matching non-empty Wikidata identifiers
    can safely reunite gaps in the same normalized-name group.
    """

    components = _line_components(features)
    if not components:
        return []
    parents = list(range(len(components)))

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

    first_by_wikidata: dict[str, int] = {}
    for index, component in enumerate(components):
        identities = {
            str((feature.get("tags") or {}).get("wikidata") or "").strip()
            for feature in component
            if str((feature.get("tags") or {}).get("wikidata") or "").strip()
        }
        if len(identities) > 1:
            _fail(
                f"connected river {name!r} has conflicting Wikidata identities: "
                f"{sorted(identities)}"
            )
        if not identities:
            continue
        identity = next(iter(identities))
        previous = first_by_wikidata.setdefault(identity, index)
        union(previous, index)

    reunited: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, component in enumerate(components):
        reunited[find(index)].extend(component)
    return sorted(
        (sorted(component, key=_feature_sort_key) for component in reunited.values()),
        key=lambda component: _feature_sort_key(component[0]),
    )


def _derive_audited_rivers(
    features: Sequence[dict[str, Any]],
    *,
    subject_id: str,
    audited_entities: Sequence[dict[str, Any]],
    quarantined_source_objects: Sequence[str],
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
    selection: ResolvedSelection,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    named_source_ways: set[str] = set()
    for feature in features:
        if (
            feature.get("layer") != "waterways"
            or feature.get("geometry_type") != "line"
            or _river_identity(feature) is None
        ):
            continue
        source_object = _source_object(feature)
        by_object[source_object].append(feature)
        named_source_ways.add(source_object)

    quarantined = {str(value) for value in quarantined_source_objects}
    outputs: list[dict[str, Any]] = []
    used_objects: set[str] = set()
    for entity_index, expected in enumerate(audited_entities):
        source_objects = [
            str(value) for value in expected.get("source_object_ids") or []
        ]
        if not source_objects or len(source_objects) != len(set(source_objects)):
            _fail(f"{subject_id} audited river entity has invalid source IDs")
        if used_objects.intersection(source_objects):
            _fail(f"{subject_id} audited river source object is selected twice")
        if quarantined.intersection(source_objects):
            _fail(f"{subject_id} audited river selects a quarantined source object")
        missing = [value for value in source_objects if value not in by_object]
        if missing:
            _fail(f"{subject_id} audited river source objects are missing: {missing}")
        component = sorted(
            [feature for value in source_objects for feature in by_object[value]],
            key=_feature_sort_key,
        )
        expected_normalized = str(expected.get("normalized_name") or "")
        actual_names = {
            identity[0]
            for feature in component
            if (identity := _river_identity(feature)) is not None
        }
        if actual_names != {expected_normalized}:
            _fail(
                f"{subject_id} audited river name identity differs for "
                f"{expected.get('name')!r}"
            )
        wikidata_values = {
            str((feature.get("tags") or {}).get("wikidata") or "").strip()
            for feature in component
            if str((feature.get("tags") or {}).get("wikidata") or "").strip()
        }
        expected_wikidata = str(expected.get("wikidata") or "").strip() or None
        if len(wikidata_values) > 1:
            _fail(
                f"{subject_id} audited river {expected.get('name')!r} has "
                "conflicting Wikidata identities"
            )
        if expected_wikidata is not None and wikidata_values != {expected_wikidata}:
            _fail(
                f"{subject_id} audited river {expected.get('name')!r} has "
                "different Wikidata evidence"
            )

        geometry = transform(forward.transform, _component_geometry(component))
        clipped = make_valid(geometry.intersection(crop))
        paths, length_m = _derived_line_paths(
            clipped,
            inverse=inverse,
            simplification_m=selection.simplification_m,
        )
        if not paths:
            _fail(f"{subject_id} audited river has no geometry inside the crop")
        distance_m = float(geometry.distance(route))
        entity_hash = hashlib.sha256(
            f"{expected_normalized}|{','.join(sorted(source_objects))}".encode("utf-8")
        ).hexdigest()[:16]
        item: dict[str, Any] = {
            "id": f"river-entity-{entity_hash}-{entity_index:02d}",
            "class": "river",
            "name": str(expected.get("name") or ""),
            "source_object": sorted(source_objects)[0],
            "source_objects": sorted(source_objects),
            "source_snapshot_sha256s": _component_snapshot_hashes(component),
            "length_m": round(length_m, 1),
            "distance_to_route_m": round(distance_m, 1),
            "paths": paths,
            "source_geometry_sha256": _component_source_hash(component),
            "derived_geometry_sha256": _canonical_sha256(paths),
        }
        if expected_wikidata is not None:
            item["wikidata"] = expected_wikidata
        outputs.append(item)
        used_objects.update(source_objects)

    return outputs, {
        "named_source_ways": len(named_source_ways),
        "stitched_components": len(outputs),
        "eligible_entities": len(outputs),
        "selected_entities": len(outputs),
        "quarantined_source_objects": len(quarantined.intersection(by_object)),
    }


def _derive_rivers(
    features: Sequence[dict[str, Any]],
    *,
    subject_id: str,
    forward: Transformer,
    inverse: Transformer,
    crop: BaseGeometry,
    route: BaseGeometry,
    selection: ResolvedSelection,
    audited_entities: Sequence[dict[str, Any]] | None = None,
    quarantined_source_objects: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if audited_entities is not None:
        return _derive_audited_rivers(
            features,
            subject_id=subject_id,
            audited_entities=audited_entities,
            quarantined_source_objects=quarantined_source_objects,
            forward=forward,
            inverse=inverse,
            crop=crop,
            route=route,
            selection=selection,
        )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    raw_source_ways: set[str] = set()
    for feature in features:
        if (
            feature.get("layer") != "waterways"
            or feature.get("geometry_type") != "line"
        ):
            continue
        identity = _river_identity(feature)
        if identity is None:
            continue
        identity_key, _ = identity
        grouped[identity_key].append(feature)
        raw_source_ways.add(_source_object(feature))
    outputs: list[tuple[tuple[float, float, str], dict[str, Any]]] = []
    stitched_components = 0
    for identity_key in sorted(grouped):
        display_name = _river_identity(grouped[identity_key][0])
        if display_name is None:
            continue
        for component_index, component in enumerate(
            _river_components(grouped[identity_key], name=display_name[1])
        ):
            stitched_components += 1
            names = sorted(
                {
                    str((feature.get("tags") or {}).get("name") or "").strip()
                    for feature in component
                },
                key=lambda value: (value.casefold(), value),
            )
            name = names[0]
            wikidata_values = sorted(
                {
                    str((feature.get("tags") or {}).get("wikidata") or "").strip()
                    for feature in component
                    if str((feature.get("tags") or {}).get("wikidata") or "").strip()
                }
            )
            if len(wikidata_values) > 1:
                _fail(
                    f"connected river {name!r} has conflicting Wikidata identities: "
                    f"{wikidata_values}"
                )
            wikidata = wikidata_values[0] if wikidata_values else None
            geometry = transform(forward.transform, _component_geometry(component))
            clipped = make_valid(geometry.intersection(crop))
            if clipped.is_empty:
                continue
            paths, length_m = _derived_line_paths(
                clipped,
                inverse=inverse,
                simplification_m=selection.simplification_m,
            )
            if not paths:
                continue
            distance_m = float(geometry.distance(route))
            if subject_id == "RTE-GB-HEB-WALK-01":
                keep = (distance_m <= 3_000.0 and length_m >= 2_000.0) or (
                    distance_m <= 10_000.0 and length_m >= 12_000.0
                )
            elif subject_id == "RTE-GB-GGW-01":
                keep = distance_m <= 3_000.0 and length_m >= 9_000.0
            elif subject_id == "RTE-GB-JMW-WALK-01":
                keep = distance_m <= 250.0 and length_m >= 8_000.0
            else:
                keep = (
                    length_m >= selection.minimum_river_length_m
                    and distance_m <= selection.route_context_distance_m
                )
            if not keep:
                continue
            source_objects = sorted({_source_object(feature) for feature in component})
            entity_hash = hashlib.sha256(
                f"{identity_key}|{','.join(source_objects)}".encode("utf-8")
            ).hexdigest()[:16]
            item: dict[str, Any] = {
                "id": f"river-entity-{entity_hash}-{component_index:02d}",
                "class": "river",
                "name": name,
                "source_object": source_objects[0],
                "source_objects": source_objects,
                "source_snapshot_sha256s": _component_snapshot_hashes(component),
                "length_m": round(length_m, 1),
                "distance_to_route_m": round(distance_m, 1),
                "paths": paths,
                "source_geometry_sha256": _component_source_hash(component),
                "derived_geometry_sha256": _canonical_sha256(paths),
            }
            if wikidata is not None:
                item["wikidata"] = wikidata
            score = (
                distance_m / max(selection.route_context_distance_m, 1.0)
                - math.log1p(length_m / selection.minimum_river_length_m) * 0.35
            )
            outputs.append(((score, -length_m, item["id"]), item))
    outputs.sort(key=lambda item: item[0])
    selected = [item[1] for item in outputs[: selection.max_river_entities]]
    return selected, {
        "named_source_ways": len(raw_source_ways),
        "stitched_components": stitched_components,
        "eligible_entities": len(outputs),
        "selected_entities": len(selected),
    }


def _polygon_label_point(feature: dict[str, Any]) -> list[float] | None:
    try:
        polygon = Polygon(feature["outer"], feature.get("holes") or [])
    except (KeyError, TypeError, ValueError):
        return None
    if polygon.is_empty:
        return None
    point = polygon.representative_point()
    return [round(float(point.x), 6), round(float(point.y), 6)]


def _river_label_point(feature: dict[str, Any]) -> list[float] | None:
    lines: list[LineString] = []
    for path in feature.get("paths") or []:
        try:
            line = LineString(path)
        except (TypeError, ValueError):
            continue
        if not line.is_empty and line.length > 0.0:
            lines.append(line)
    if not lines:
        return None
    line = max(lines, key=lambda item: item.length)
    point = line.interpolate(0.5, normalized=True)
    return [round(float(point.x), 6), round(float(point.y), 6)]


def _derive_labels(
    areas: Sequence[dict[str, Any]], rivers: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates: list[tuple[int, str, dict[str, Any], list[float]]] = []
    for area in areas:
        name = str(area.get("name") or "").strip()
        point = _polygon_label_point(area)
        if name and point is not None:
            candidates.append((3, "water", area, point))
    for river in rivers:
        name = str(river.get("name") or "").strip()
        point = _river_label_point(river)
        if name and point is not None:
            candidates.append((4, "river", river, point))
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for priority, kind, feature, point in sorted(
        candidates,
        key=lambda item: (item[0], str(item[2].get("name") or "").casefold()),
    ):
        name = str(feature["name"])
        identity = name.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        source_object = str(feature["source_object"])
        output.append(
            {
                "id": f"hydro-label-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}",
                "kind": kind,
                "label": name.upper(),
                "point": point,
                "source_object": source_object,
                "source_url": f"https://www.openstreetmap.org/{source_object}",
                "priority": priority,
            }
        )
    return output


def _selected_source_objects(
    landcover: Sequence[dict[str, Any]],
    areas: Sequence[dict[str, Any]],
    coastlines: Sequence[dict[str, Any]],
    rivers: Sequence[dict[str, Any]],
) -> list[str]:
    objects = {str(feature["source_object"]) for feature in [*landcover, *areas]}
    for feature in [*coastlines, *rivers]:
        objects.update(str(value) for value in feature["source_objects"])
    return sorted(objects)


def _visible_osm_credit_line(existing: str) -> str:
    retained: list[str] = []
    for line in existing.split("|"):
        for clause in line.split(" / "):
            text = clause.strip()
            upper = text.upper()
            if not text:
                continue
            if (
                "OPENSTREETMAP" in upper
                or "OSM CONTRIBUTORS" in upper
                or upper in {"OSM", "© OSM"}
                or "SOURCE PARTNERS IN SVG METADATA" in upper
            ):
                continue
            retained.append(text)
    retained_credit = " / ".join(retained)
    # The complete publisher/partner attribution remains in the embedded source
    # records.  The face has room for two physical text lines, one of which must
    # carry OSM's visible credit, so keep the non-OSM raster credit concise.
    if retained_credit.startswith("ÍSLANDSDEM V1.0 © NÁTTÚRUFRÆÐISTOFNUN"):
        retained_credit = "ÍSLANDSDEM V1.0 / CC BY 4.0"
    osm_credit = "© OSM CONTRIBUTORS / OPENSTREETMAP.ORG/COPYRIGHT"
    return f"{retained_credit} | {osm_credit}" if retained_credit else osm_credit


def _upsert_context(
    *,
    bundle: dict[str, Any],
    subject_id: str,
    source: dict[str, Any],
    base_credit_line: str,
    working_crs: str,
    source_feature_count: int,
    selection: ResolvedSelection,
    audited_rules: dict[str, Any] | None,
    audited_profile_id: str | None,
    landcover: list[dict[str, Any]],
    landcover_counts: dict[str, int],
    areas: list[dict[str, Any]],
    water_area_counts: dict[str, int],
    coastlines: list[dict[str, Any]],
    coastline_counts: dict[str, int],
    rivers: list[dict[str, Any]],
    river_counts: dict[str, int],
    labels: list[dict[str, Any]],
) -> None:
    overlay = _overlay(bundle, subject_id)
    sources = overlay.setdefault("sources", [])
    if not isinstance(sources, list):
        _fail(f"{subject_id} overlay sources must be an array")
    sources[:] = [item for item in sources if item.get("id") != source["id"]]
    sources.append(source)
    context = overlay.setdefault("context", {})
    backdrop = overlay.setdefault("backdrop", {})
    if not isinstance(context, dict) or not isinstance(backdrop, dict):
        _fail(f"{subject_id} overlay context/backdrop must be objects")
    existing_credit = str(overlay.get("credit_line") or base_credit_line)
    overlay["credit_line"] = _visible_osm_credit_line(existing_credit)
    if landcover:
        context["landcover"] = {
            "status": "source-sampled-landcover-polygons",
            "source_ref": source["id"],
            "derivation_id": GENERIC_LANDCOVER_DERIVATION,
            "source_crs": "EPSG:4326",
            "working_crs": working_crs,
            "selection_profile_id": (audited_profile_id or "scale-aware-generic-v1"),
            "selection_rule": (
                str(audited_rules.get("landcover", audited_rules.get("woodland", "")))
                if audited_rules is not None
                else (
                    "natural=wood or landuse=forest only; scale-aware minimum area; "
                    "route-proximity/major-area ranking; capped after metric clipping"
                )
            ),
            "simplification_tolerance_m": selection.simplification_m,
            "minimum_area_m2": selection.minimum_landcover_area_m2,
            "source_feature_count": source_feature_count,
            "derivation_counts": landcover_counts,
            "features": landcover,
        }
        backdrop["vegetation"] = "source-sampled-landcover-polygons"
    else:
        previous = context.get("landcover")
        if (
            isinstance(previous, dict)
            and previous.get("derivation_id") == GENERIC_LANDCOVER_DERIVATION
        ):
            context.pop("landcover")
        if backdrop.get("vegetation") == "source-sampled-landcover-polygons":
            backdrop.pop("vegetation")
    if areas or coastlines or rivers:
        context["water"] = {
            "status": "source-sampled-hydrography",
            "source_ref": source["id"],
            "derivation_id": GENERIC_WATER_DERIVATION,
            "source_crs": "EPSG:4326",
            "working_crs": working_crs,
            "selection_profile_id": (audited_profile_id or "scale-aware-generic-v1"),
            "selection_rule": (
                {
                    "water": str(audited_rules["water"]),
                    "coastlines": str(audited_rules["coast"]),
                    "rivers": str(audited_rules["rivers"]),
                }
                if audited_rules is not None
                else (
                    "major metric-clipped inland-water polygons; exact-endpoint "
                    "stitched coastline components; connected named waterway=river "
                    "entities ranked by route context and coherent length"
                )
            ),
            "simplification_tolerance_m": {
                "water_areas": selection.simplification_m,
                "coastlines": selection.simplification_m,
                "rivers": selection.simplification_m,
            },
            "minimum_area_m2": selection.minimum_water_area_m2,
            "minimum_river_length_m": selection.minimum_river_length_m,
            "river_context_distance_m": selection.route_context_distance_m,
            "source_feature_count": source_feature_count,
            "derivation_counts": {
                "water_areas": water_area_counts,
                "coastlines": coastline_counts,
                "rivers": river_counts,
            },
            "areas": areas,
            "coastlines": coastlines,
            "rivers": rivers,
            "labels": labels,
        }
        backdrop["water"] = "source-sampled-hydrography"
    else:
        previous = context.get("water")
        if (
            isinstance(previous, dict)
            and previous.get("derivation_id") == GENERIC_WATER_DERIVATION
        ):
            context.pop("water")
        if backdrop.get("water") == "source-sampled-hydrography":
            backdrop.pop("water")


def derive(
    *,
    catalog_path: Path,
    bundle_path: Path,
    subject_id: str,
    source_id: str,
    retrieved_at: str,
    raw_paths: Sequence[Path] | None = None,
    source_urls: Sequence[str] | None = None,
    raw_path: Path | None = None,
    source_url: str | None = None,
    options: SelectionOptions = SelectionOptions(),
    publisher: str = "OpenStreetMap contributors / Geofabrik",
    license_name: str = "ODbL-1.0",
    attribution: str = "© OpenStreetMap contributors; openstreetmap.org/copyright",
) -> DerivationResult:
    """Derive and persist one non-WHW overlay after all evidence checks pass."""

    if subject_id == WHW_SUBJECT_ID:
        _fail("the reviewed WHW v4 context is immutable in this generic tool")
    if SOURCE_ID.fullmatch(source_id) is None:
        _fail(
            "source_id must contain only lower-case letters, digits, dot, dash, underscore"
        )
    if raw_paths is not None and raw_path is not None:
        _fail("use raw_paths or raw_path, not both")
    if source_urls is not None and source_url is not None:
        _fail("use source_urls or source_url, not both")
    input_paths = list(raw_paths) if raw_paths is not None else [raw_path]
    input_urls = list(source_urls) if source_urls is not None else [source_url]
    if any(path is None for path in input_paths) or any(
        url is None for url in input_urls
    ):
        _fail("raw extraction paths and source URLs are required")
    checked_paths = [path for path in input_paths if isinstance(path, Path)]
    checked_urls = [str(url) for url in input_urls if url is not None]
    if len(checked_paths) != len(input_paths):
        _fail("raw extraction paths must be Path values")
    if not all(url.startswith("https://") for url in checked_urls):
        _fail("every source_url must use HTTPS")
    for label, value in (
        ("retrieved_at", retrieved_at),
        ("publisher", publisher),
        ("license", license_name),
        ("attribution", attribution),
    ):
        if not value.strip():
            _fail(f"{label} must not be empty")

    catalog = _load_object(catalog_path)
    bundle = _load_object(bundle_path)
    if bundle.get("id") != "hike-context-v3" or bundle.get("schema_version") != 3:
        _fail("bundle must be the hike-context-v3 schema")
    whw_before = _whw_digest(bundle)
    record = _record(catalog, subject_id)
    extent = _catalog_extent(record)
    nonuk_gate_info = _nonuk_selection_gate(
        subject_id=subject_id,
        extent=extent,
    )
    features, evidence, coverage_union = _combined_raw_inputs(
        raw_paths=checked_paths,
        source_urls=checked_urls,
        extent=extent,
        audited_header_coverage=nonuk_gate_info is not None,
    )
    uk_gate_info = _uk_selection_gate(
        subject_id=subject_id,
        extent=extent,
        evidence=evidence,
    )
    if nonuk_gate_info is not None:
        _assert_nonuk_input_gate(
            subject_id=subject_id,
            gate=nonuk_gate_info[0],
            evidence=evidence,
        )
    forward, inverse, working_crs = _working_transformers(extent)
    coverage = _coverage_evidence(
        subject_id=subject_id,
        record=record,
        extent=extent,
        coverage_union=coverage_union,
        evidence=evidence,
        forward=forward,
        audited_gate=(nonuk_gate_info[0] if nonuk_gate_info is not None else None),
    )
    crop = transform(forward.transform, _densified_bbox_polygon(extent))
    route = _route_geometry(record, forward)
    selection = _resolve_selection(crop, options)
    mm_per_km = _page_scale_mm_per_km(record)

    landcover, landcover_counts = _derive_landcover(
        features,
        subject_id=subject_id,
        mm_per_km=mm_per_km,
        forward=forward,
        inverse=inverse,
        crop=crop,
        route=route,
        selection=selection,
        audited_source_objects=(
            nonuk_gate_info[0]["landcover_source_objects"]
            if nonuk_gate_info is not None
            else None
        ),
    )
    areas, water_area_counts = _derive_water_areas(
        features,
        subject_id=subject_id,
        mm_per_km=mm_per_km,
        forward=forward,
        inverse=inverse,
        crop=crop,
        route=route,
        selection=selection,
        audited_source_objects=(
            nonuk_gate_info[0]["water_source_objects"]
            if nonuk_gate_info is not None
            else None
        ),
    )
    coastlines, coastline_counts = _derive_coastlines(
        features,
        subject_id=subject_id,
        mm_per_km=mm_per_km,
        forward=forward,
        inverse=inverse,
        crop=crop,
        route=route,
        selection=selection,
        audited_components=(
            nonuk_gate_info[0]["coast_components"]
            if nonuk_gate_info is not None
            else None
        ),
    )
    rivers, river_counts = _derive_rivers(
        features,
        subject_id=subject_id,
        forward=forward,
        inverse=inverse,
        crop=crop,
        route=route,
        selection=selection,
        audited_entities=(
            nonuk_gate_info[0]["river_entities"]
            if nonuk_gate_info is not None
            else None
        ),
        quarantined_source_objects=(
            nonuk_gate_info[0]["quarantined_source_objects"]
            if nonuk_gate_info is not None
            else ()
        ),
    )
    if uk_gate_info is not None:
        _assert_uk_selection_gate(
            subject_id=subject_id,
            gate=uk_gate_info[0],
            landcover=landcover,
            areas=areas,
            coastlines=coastlines,
            rivers=rivers,
        )
    if nonuk_gate_info is not None:
        _assert_nonuk_selection_gate(
            subject_id=subject_id,
            gate=nonuk_gate_info[0],
            landcover=landcover,
            areas=areas,
            coastlines=coastlines,
            rivers=rivers,
        )
    if not (landcover or areas or coastlines or rivers):
        _fail("no factual, plot-legible OSM context survived selection")
    labels = _derive_labels(areas, rivers)
    selected_objects = _selected_source_objects(landcover, areas, coastlines, rivers)
    input_snapshots = [
        {
            "url": item.source_url,
            "source_timestamp": str(item.metadata.get("source_timestamp") or "unknown"),
            "snapshot_sha256": item.snapshot_sha256,
            "canonical_extraction_sha256": item.canonical_extraction_sha256,
            "canonical_feature_count": int(
                item.metadata["canonical_features"]["count"]
            ),
            "raw_context_payload_sha256": item.raw_context_payload_sha256,
            "requested_bbox_wgs84": dict(
                zip(
                    ("west", "south", "east", "north"),
                    item.requested_bbox,
                    strict=True,
                )
            ),
            "header_bbox_wgs84": dict(
                zip(
                    ("west", "south", "east", "north"),
                    item.header_bbox,
                    strict=True,
                )
            ),
            "raw_coverage_proven": item.raw_coverage_proven,
        }
        for item in evidence
    ]
    snapshot_hash = (
        evidence[0].snapshot_sha256
        if len(evidence) == 1
        else _canonical_sha256([item.snapshot_sha256 for item in evidence])
    )
    extraction_hash = (
        evidence[0].canonical_extraction_sha256
        if len(evidence) == 1
        else _canonical_sha256([item.canonical_extraction_sha256 for item in evidence])
    )
    raw_payload_hash = (
        evidence[0].raw_context_payload_sha256
        if len(evidence) == 1
        else _canonical_sha256([item.raw_context_payload_sha256 for item in evidence])
    )
    source_timestamps = sorted(
        {str(item.metadata.get("source_timestamp") or "unknown") for item in evidence}
    )
    source = {
        "id": source_id,
        "publisher": publisher,
        "url": checked_urls[0],
        "license": license_name,
        "attribution": attribution,
        "use": (
            "systematic source geometry for factual forest/woodland, coast, major "
            "inland water, and named route-context rivers"
        ),
        "source_timestamp": ", ".join(source_timestamps),
        "retrieved_at": retrieved_at,
        "snapshot_sha256": snapshot_hash,
        "canonical_extraction_sha256": extraction_hash,
        "canonical_feature_count": len(features),
        "raw_context_payload_sha256": raw_payload_hash,
        "input_snapshots": input_snapshots,
        "selected_source_object_count": len(selected_objects),
        "selected_source_objects_sha256": _canonical_sha256(selected_objects),
        "coverage_proven": coverage.full_crop_coverage_proven,
        "full_crop_coverage_proven": coverage.full_crop_coverage_proven,
        "route_coverage_proven": coverage.route_fully_covered,
        "crop_coverage_fraction": round(coverage.crop_coverage_fraction, 15),
        "crop_uncovered_area_degrees2": round(
            coverage.crop_uncovered_area_degrees2, 15
        ),
        "route_coverage_fraction": round(coverage.route_coverage_fraction, 15),
        "absence_claims_safe": coverage.absence_claims_safe,
    }
    if uk_gate_info is not None:
        source["selection_gate"] = {
            "id": "hike-uk-osm-selection-v1",
            "manifest_sha256": uk_gate_info[1],
            "selection_rules": uk_gate_info[0]["selection_rules"],
        }
    elif nonuk_gate_info is not None:
        source["selection_gate"] = {
            "id": "hike-nonuk-osm-selection-v1",
            "manifest_sha256": nonuk_gate_info[1],
            "audit_payload_sha256": _load_object(NONUK_SELECTION_PATH)[
                "audit_payload_sha256"
            ],
            "selected_source_object_manifest_sha256": nonuk_gate_info[0][
                "selected_source_object_manifest_sha256"
            ],
            "selection_rules": nonuk_gate_info[0]["selection_rules"],
            "coverage": {
                "crop_coverage_fraction": round(coverage.crop_coverage_fraction, 15),
                "crop_uncovered_area_degrees2": round(
                    coverage.crop_uncovered_area_degrees2, 15
                ),
                "route_coverage_fraction": round(coverage.route_coverage_fraction, 15),
                "route_fully_covered": coverage.route_fully_covered,
                "full_crop_coverage_proven": coverage.full_crop_coverage_proven,
                "absence_claims_safe": coverage.absence_claims_safe,
            },
        }
    audited_gate_info = uk_gate_info or nonuk_gate_info
    audited_profile_id = (
        "hike-uk-osm-selection-v1"
        if uk_gate_info is not None
        else ("hike-nonuk-osm-selection-v1" if nonuk_gate_info is not None else None)
    )
    _upsert_context(
        bundle=bundle,
        subject_id=subject_id,
        source=source,
        base_credit_line=str(record.get("credit_line") or ""),
        working_crs=working_crs,
        source_feature_count=len(features),
        selection=selection,
        audited_rules=(
            audited_gate_info[0]["selection_rules"]
            if audited_gate_info is not None
            else None
        ),
        audited_profile_id=audited_profile_id,
        landcover=landcover,
        landcover_counts=landcover_counts,
        areas=areas,
        water_area_counts=water_area_counts,
        coastlines=coastlines,
        coastline_counts=coastline_counts,
        rivers=rivers,
        river_counts=river_counts,
        labels=labels,
    )
    if _whw_digest(bundle) != whw_before:
        _fail("internal error: generic derivation changed the WHW v4 record")
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return DerivationResult(
        subject_id=subject_id,
        landcover_features=len(
            {str(feature["source_object"]) for feature in landcover}
        ),
        water_areas=len({str(feature["source_object"]) for feature in areas}),
        coastline_chains=len(coastlines),
        river_entities=len(rivers),
        labels=len(labels),
    )


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-json", type=Path, action="append", required=True)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-url", action="append", required=True)
    parser.add_argument("--retrieved-at", required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--publisher", default="OpenStreetMap contributors / Geofabrik")
    parser.add_argument("--license", dest="license_name", default="ODbL-1.0")
    parser.add_argument(
        "--attribution",
        default="© OpenStreetMap contributors; openstreetmap.org/copyright",
    )
    parser.add_argument("--simplification-m", type=_positive_float)
    parser.add_argument("--minimum-landcover-area-m2", type=_positive_float)
    parser.add_argument("--minimum-water-area-m2", type=_positive_float)
    parser.add_argument("--river-context-distance-m", type=_positive_float)
    parser.add_argument("--minimum-river-length-m", type=_positive_float)
    parser.add_argument("--minimum-coastline-length-m", type=_positive_float)
    parser.add_argument("--minimum-closed-coastline-area-m2", type=_positive_float)
    parser.add_argument("--max-landcover-features", type=_positive_int, default=72)
    parser.add_argument("--max-water-areas", type=_positive_int, default=14)
    parser.add_argument("--max-coastline-chains", type=_positive_int, default=24)
    parser.add_argument("--max-river-entities", type=_positive_int, default=12)
    args = parser.parse_args()
    try:
        result = derive(
            raw_paths=args.raw_json,
            catalog_path=args.catalog,
            bundle_path=args.bundle,
            subject_id=args.subject_id,
            source_id=args.source_id,
            source_urls=args.source_url,
            retrieved_at=args.retrieved_at,
            publisher=args.publisher,
            license_name=args.license_name,
            attribution=args.attribution,
            options=SelectionOptions(
                simplification_m=args.simplification_m,
                minimum_landcover_area_m2=args.minimum_landcover_area_m2,
                minimum_water_area_m2=args.minimum_water_area_m2,
                river_context_distance_m=args.river_context_distance_m,
                minimum_river_length_m=args.minimum_river_length_m,
                minimum_coastline_length_m=args.minimum_coastline_length_m,
                minimum_closed_coastline_area_m2=(
                    args.minimum_closed_coastline_area_m2
                ),
                max_landcover_features=args.max_landcover_features,
                max_water_areas=args.max_water_areas,
                max_coastline_chains=args.max_coastline_chains,
                max_river_entities=args.max_river_entities,
            ),
        )
    except ContextDerivationError as exc:
        parser.exit(2, f"derive_hiking_osm_context: {exc}\n")
    print(
        f"{result.subject_id}: landcover={result.landcover_features}, "
        f"water_areas={result.water_areas}, "
        f"coastline_chains={result.coastline_chains}, "
        f"rivers={result.river_entities}, labels={result.labels}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
