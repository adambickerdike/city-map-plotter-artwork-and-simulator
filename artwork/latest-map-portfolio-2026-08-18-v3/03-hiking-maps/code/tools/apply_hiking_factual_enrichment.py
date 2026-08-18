#!/usr/bin/env python3
"""Apply frozen AP6 and Mont Blanc factual corrections to a release copy.

This is deliberately a final, narrow enrichment stage.  It can be run after
global terrain and source-precedence work without rebuilding or overwriting
unrelated hiking records.  The Alpine Passes Trail map line is refreshed from
one frozen ODbL Waymarked snapshot; its much finer complete chain is sampled
from the already-frozen Terrarium cache for the footer profile only.  The Tour
du Mont Blanc receives one exact, versioned OSM summit-node admission through
the existing explicit-elevation overlay contract.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence

from pyproj import Transformer  # type: ignore[import-not-found]

from apply_hiking_source_precedence import apply_peak_overlay_only
from build_hiking_expansion_catalog import (
    _mercator_to_lonlat,
    _route_base_geometries,
    _route_geometry,
    _stitch_segments,
)
from city_map_plotter.hike_plates import _validate_release_catalog
from city_map_plotter.route_chainage import geodesic_distance_m
from derive_hiking_global_terrain import (
    TERRESTRIAL_MINIMUM_ELEVATION_M,
    TERRESTRIAL_ROUTE_ELEVATION_METHOD,
    TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID,
    _load_extent_grid,
    _sample_grid,
)


AP6_ID = "RTE-CH-AP6-01"
TMB_ID = "RTE-EU-TMB-LOOP-01"
RELEASE_ID = "hike-plates-release-v1"
AP6_RELATION_ID = 18_021_781
AP6_HERO_TOLERANCE_M = 376.9
AP6_WAYMARKED_RAW_SHA256 = (
    "bd7bf780ca99d9d0177be0b60f787c0e488277b80095d2f065ef9f2c797cf813"
)
AP6_WAYMARKED_COMPRESSED_NAME = (
    "waymarked-relation-18021781-2026-08-04.json.gz"
)
AP6_OSM_RELATION_COMPRESSED_NAME = "osm-relation-18021781-v30.json.gz"
AP6_OSM_RELATION_RAW_SHA256 = (
    "1d795de5f044bd3fb4196ba9be0be222aba845b4167cf718e92abe7ee729bcc8"
)
AP6_OSM_RELATION_CANONICAL_SHA256 = (
    "fa0672ef36aec146017b89a8c4600110df2f40101e59542b798e32f8c7ad7379"
)
AP6_FACTS_NAME = "schweizmobil-route-6-facts-v1.json"
AP6_OFFICIAL_RAW_NAME = "schweizmobil-route-6-api-2026-08-04.json.gz"
MONT_BLANC_SNAPSHOT_NAME = "osm-node-281399025-v63.json"
MONT_BLANC_RAW_SHA256 = (
    "fe84dd5835f7773a18b70d6aeb39ce6b515ef8787969684438789f6228d6e82b"
)
MONT_BLANC_CANONICAL_SHA256 = (
    "f0a3711da9d26631e6d8451ca6bc4bd7cf0e3e6c60b670d350772daf0cde0e9c"
)
DEFAULT_DATA_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "city_map_plotter" / "data"
)
DEFAULT_SNAPSHOT_DIR = DEFAULT_DATA_DIR / "hike-source-snapshots"
DEFAULT_PEAK_OVERLAY = DEFAULT_DATA_DIR / "hike-explicit-elevations-v1.json"

ElevationSampler = Callable[[Sequence[Sequence[float]], dict[str, Any]], tuple[list[list[float]], dict[str, Any]]]


class FactualEnrichmentError(ValueError):
    """Raised when frozen evidence or the target release fails closed."""


def _fail(message: str) -> NoReturn:
    raise FactualEnrichmentError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _load_json_bytes(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        _fail(f"{label} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label} must contain an object")
    return value


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail(f"could not read {label} {path}: {exc}")
    return _load_json_bytes(payload, label=label)


def load_ap6_source_geometry(
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
) -> tuple[dict[str, Any], list[list[float]], list[list[float]], float]:
    """Return verified details, A5 hero, complete profile chain and its length."""

    path = snapshot_dir / AP6_WAYMARKED_COMPRESSED_NAME
    try:
        raw = gzip.decompress(path.read_bytes())
    except (OSError, gzip.BadGzipFile) as exc:
        _fail(f"could not read frozen AP6 Waymarked snapshot {path}: {exc}")
    if _sha256(raw) != AP6_WAYMARKED_RAW_SHA256:
        _fail("frozen AP6 Waymarked raw snapshot SHA-256 drifted")
    details = _load_json_bytes(raw, label="AP6 Waymarked snapshot")
    if details.get("id") != AP6_RELATION_ID:
        _fail("AP6 Waymarked snapshot relation id drifted")
    route = details.get("route")
    if not isinstance(route, dict) or route.get("length") != 675_645:
        _fail("AP6 Waymarked route length is not the audited 675645 m snapshot")
    chains = _stitch_segments(_route_base_geometries(route))
    if len(chains) != 1 or len(chains[0]) != 57_479:
        _fail(
            "AP6 Waymarked snapshot must stitch to exactly one 57,479-point chain"
        )
    profile = [
        [round(longitude, 7), round(latitude, 7)]
        for longitude, latitude in (
            _mercator_to_lonlat(point) for point in chains[0]
        )
    ]
    hero_geometry = _route_geometry(
        details,
        tolerance_m=AP6_HERO_TOLERANCE_M,
    )
    if len(hero_geometry.segments) != 1 or len(hero_geometry.segments[0]) != 392:
        _fail("AP6 A5 simplification is not the audited one-chain/392-point result")
    hero = [list(point) for point in hero_geometry.segments[0]]
    measured_m = sum(
        geodesic_distance_m(first, second)
        for first, second in zip(profile, profile[1:])
    )
    if not math.isclose(measured_m, 675_643.328, rel_tol=0.0, abs_tol=2.0):
        _fail(f"AP6 complete source-chain length drifted to {measured_m:.3f} m")
    expected_start = [9.818911, 46.508163]
    expected_finish = [6.804684, 46.394219]
    if hero[0] != expected_start or hero[-1] != expected_finish:
        _fail("AP6 hero endpoints drifted from the audited source controls")
    if profile[0] != [9.8189106, 46.5081631] or profile[-1] != [
        6.8046843,
        46.3942194,
    ]:
        _fail("AP6 complete-profile endpoints drifted from the audited controls")
    return details, hero, profile, measured_m


def load_ap6_official_facts(
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
) -> dict[str, Any]:
    value = _load_object(snapshot_dir / AP6_FACTS_NAME, label="AP6 official facts")
    facts = value.get("facts")
    raw_path = snapshot_dir / AP6_OFFICIAL_RAW_NAME
    try:
        raw = gzip.decompress(raw_path.read_bytes())
    except (OSError, gzip.BadGzipFile) as exc:
        _fail(f"could not read frozen AP6 SwitzerlandMobility snapshot {raw_path}: {exc}")
    if _sha256(raw) != value.get("raw_snapshot_sha256"):
        _fail("frozen AP6 SwitzerlandMobility raw snapshot SHA-256 drifted")
    raw_value = _load_json_bytes(raw, label="AP6 SwitzerlandMobility API snapshot")
    raw_segments = raw_value.get("segments")
    raw_stage_numbers = (
        [item.get("segmentNumber") for item in raw_segments]
        if isinstance(raw_segments, list)
        and all(isinstance(item, dict) for item in raw_segments)
        else None
    )
    if (
        value.get("schema_version") != 1
        or value.get("id") != "schweizmobil-hike-route-6-facts-v1"
        or value.get("raw_snapshot_sha256")
        != "f73c58071e91b1c60b45dbb1423a79e2eee3feb4c76792428c29fbed98140607"
        or not isinstance(facts, dict)
        or facts.get("route_number") != 6
        or facts.get("title") != "Alpine Passes Trail"
        or facts.get("start") != "St. Moritz, Corviglia"
        or facts.get("end") != "St-Gingolph"
        or facts.get("published_distance_km") != 695.0
        or facts.get("stage_count") != 43
        or facts.get("stage_numbers") != list(range(1, 44))
        or raw_value.get("routeNumber") != facts.get("route_number")
        or raw_value.get("segmentNumber") != facts.get("segment_number")
        or raw_value.get("title") != facts.get("title")
        or raw_value.get("start") != facts.get("start")
        or raw_value.get("end") != facts.get("end")
        or raw_value.get("length") != facts.get("published_distance_km")
        or raw_value.get("ascent") != facts.get("published_ascent_m")
        or raw_value.get("descent") != facts.get("published_descent_m")
        or not isinstance(raw_segments, list)
        or len(raw_segments) != facts.get("stage_count")
        or raw_stage_numbers != facts.get("stage_numbers")
    ):
        _fail("AP6 official facts drifted from the frozen SwitzerlandMobility record")
    return value


def load_ap6_osm_relation_snapshot(
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
) -> dict[str, Any]:
    """Return the independently frozen OSM relation identity record."""

    path = snapshot_dir / AP6_OSM_RELATION_COMPRESSED_NAME
    try:
        raw = gzip.decompress(path.read_bytes())
    except (OSError, gzip.BadGzipFile) as exc:
        _fail(f"could not read frozen AP6 OSM relation snapshot {path}: {exc}")
    if _sha256(raw) != AP6_OSM_RELATION_RAW_SHA256:
        _fail("frozen AP6 OSM relation raw snapshot SHA-256 drifted")
    value = _load_json_bytes(raw, label="AP6 OSM relation snapshot")
    elements = value.get("elements")
    if not isinstance(elements, list) or len(elements) != 1:
        _fail("AP6 OSM relation snapshot must contain exactly one element")
    relation = elements[0]
    if not isinstance(relation, dict):
        _fail("AP6 OSM relation snapshot element must be an object")
    canonical = {
        key: copy.deepcopy(relation[key])
        for key in (
            "type",
            "id",
            "version",
            "timestamp",
            "changeset",
            "members",
            "tags",
        )
    }
    canonical_snapshot_sha256 = _sha256(
        (
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    tags = relation.get("tags")
    members = relation.get("members")
    if (
        canonical_snapshot_sha256 != AP6_OSM_RELATION_CANONICAL_SHA256
        or relation.get("type") != "relation"
        or relation.get("id") != AP6_RELATION_ID
        or relation.get("version") != 30
        or relation.get("timestamp") != "2025-10-27T06:27:06Z"
        or relation.get("changeset") != 173_822_230
        or not isinstance(members, list)
        or len(members) != 44
        or not isinstance(tags, dict)
        or tags.get("type") != "superroute"
        or tags.get("route") != "hiking"
        or tags.get("network") != "nwn"
        or tags.get("ref") != "6"
        or tags.get("name:en") != "Alpine Passes Trail"
        or tags.get("from") != "Corviglia (St. Moritz)"
        or tags.get("to") != "Saint-Gingolph VS"
    ):
        _fail("AP6 OSM relation facts drifted from the audited snapshot")
    return relation


def load_mont_blanc_snapshot(
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
) -> dict[str, Any]:
    path = snapshot_dir / MONT_BLANC_SNAPSHOT_NAME
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _fail(f"could not read frozen Mont Blanc snapshot {path}: {exc}")
    if _sha256(raw) != MONT_BLANC_RAW_SHA256:
        _fail("Mont Blanc raw OSM node snapshot SHA-256 drifted")
    value = _load_json_bytes(raw, label="Mont Blanc OSM node snapshot")
    elements = value.get("elements")
    if not isinstance(elements, list) or len(elements) != 1:
        _fail("Mont Blanc snapshot must contain exactly one OSM element")
    node = elements[0]
    if not isinstance(node, dict):
        _fail("Mont Blanc snapshot element must be an object")
    canonical = {
        key: copy.deepcopy(node[key])
        for key in (
            "type",
            "id",
            "lat",
            "lon",
            "timestamp",
            "version",
            "changeset",
            "tags",
        )
    }
    # The frozen node contract is the exact newline-terminated compact form
    # emitted by ``jq -S -c`` during acquisition.
    canonical_snapshot_sha256 = _sha256(
        (
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    tags = node.get("tags")
    if (
        canonical_snapshot_sha256 != MONT_BLANC_CANONICAL_SHA256
        or node.get("type") != "node"
        or node.get("id") != 281_399_025
        or node.get("version") != 63
        or node.get("timestamp") != "2025-12-25T16:42:19Z"
        or node.get("lon") != 6.8651706
        or node.get("lat") != 45.8327056
        or not isinstance(tags, dict)
        or tags.get("natural") != "peak"
        or tags.get("name") != "Mont Blanc / Monte Bianco"
        or tags.get("ele") != "4807.3"
    ):
        _fail("Mont Blanc OSM node facts drifted from the audited snapshot")
    return node


def validate_mont_blanc_overlay(
    peak_overlay: dict[str, Any],
    *,
    node: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed unless the overlay repeats the verified frozen node facts.

    The canonical snapshot digest proves the source object, but must never be
    accepted as a free-standing token beside independently mutable overlay
    fields.  Derive every admitted factual value from the already verified raw
    node and compare it before the generic overlay machinery is invoked.
    """

    overlay_objects = peak_overlay.get("objects")
    mont_blanc = next(
        (
            item
            for item in overlay_objects
            if isinstance(item, dict)
            and item.get("source_object") == "node/281399025"
        ),
        None,
    ) if isinstance(overlay_objects, list) else None
    tags = node.get("tags")
    if not isinstance(tags, dict):
        _fail("verified Mont Blanc node is missing tags")
    expected = {
        "snapshot_sha256": MONT_BLANC_CANONICAL_SHA256,
        "version": node.get("version"),
        "timestamp": node.get("timestamp"),
        "name": tags.get("name"),
        "label": tags.get("name:fr"),
        "elevation_m": float(str(tags.get("ele"))),
        "point": [node.get("lon"), node.get("lat")],
        "priority": -10,
        "label_required": True,
    }
    if not isinstance(mont_blanc, dict):
        _fail("explicit elevation overlay lacks the audited Mont Blanc node")
    for field, expected_value in expected.items():
        if mont_blanc.get(field) != expected_value:
            _fail(
                "explicit Mont Blanc overlay field "
                f"{field!r} drifted from the verified frozen OSM node"
            )
    if mont_blanc.get("display_label") is False:
        _fail("explicit Mont Blanc overlay cannot hide its required label")
    return mont_blanc


def _offline_profile_sampler(
    points: Sequence[Sequence[float]],
    record: dict[str, Any],
    *,
    terrain_cache_dir: Path,
) -> tuple[list[list[float]], dict[str, Any]]:
    context = record.get("context")
    if not isinstance(context, dict):
        _fail(f"{AP6_ID}: context must be an object")
    raw_extent = context.get("extent")
    if not isinstance(raw_extent, list) or len(raw_extent) != 4:
        _fail(f"{AP6_ID}: context.extent must contain four numbers")
    extent = tuple(float(value) for value in raw_extent)
    source = next(
        (
            item
            for item in record.get("sources", [])
            if isinstance(item, dict)
            and str(item.get("id") or "").startswith("aws-mapzen-terrarium-z")
        ),
        None,
    )
    if source is None:
        _fail(f"{AP6_ID}: no frozen Terrarium source is present")
    zoom = int(source.get("zoom", 9))

    def no_network(url: str) -> bytes:
        raise FactualEnrichmentError(
            f"missing frozen Terrarium tile; network access is forbidden: {url}"
        )

    values, x_coordinates, y_coordinates, _tiles, window_sha256 = _load_extent_grid(
        extent,  # type: ignore[arg-type]
        zoom=zoom,
        cache_dir=terrain_cache_dir,
        fetcher=no_network,
    )
    expected_window_sha256 = source.get("source_window_sha256")
    if (
        isinstance(expected_window_sha256, str)
        and expected_window_sha256
        and expected_window_sha256 != window_sha256
    ):
        _fail(
            f"{AP6_ID}: Terrarium source-window SHA drifted from "
            f"{expected_window_sha256} to {window_sha256}"
        )
    forward = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    sampled: list[list[float]] = []
    fallback_count = 0
    for index, point in enumerate(points):
        longitude, latitude = float(point[0]), float(point[1])
        elevation, method = _sample_grid(
            longitude,
            latitude,
            values=values,
            x_coordinates=x_coordinates,
            y_coordinates=y_coordinates,
            forward=forward,
            label=f"{AP6_ID} complete profile point {index}",
            minimum_valid_elevation_m=TERRESTRIAL_MINIMUM_ELEVATION_M,
        )
        fallback_count += int(method == "nearest-eligible-source-cell")
        sampled.append([longitude, latitude, round(elevation, 1)])
    policy = {
        "id": TERRESTRIAL_ROUTE_SAMPLING_POLICY_ID,
        "source_ref": str(source["id"]),
        "minimum_valid_elevation_m": TERRESTRIAL_MINIMUM_ELEVATION_M,
        "fallback_radius_pixels": 3,
        "point_count": len(sampled),
        "bilinear_sample_count": len(sampled) - fallback_count,
        "nearest_eligible_fallback_count": fallback_count,
        "clamping": False,
    }
    return sampled, policy


def _source_by_id(record: dict[str, Any], source_id: str) -> dict[str, Any]:
    matches = [
        source
        for source in record.get("sources", [])
        if isinstance(source, dict) and source.get("id") == source_id
    ]
    if len(matches) != 1:
        _fail(f"{record.get('id')}: expected exactly one source {source_id!r}")
    return matches[0]


def apply_ap6_correction(
    record: dict[str, Any],
    *,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    sampler: ElevationSampler,
) -> dict[str, Any]:
    if record.get("id") != AP6_ID:
        _fail(f"AP6 correction received {record.get('id')!r}")
    details, hero, profile, measured_m = load_ap6_source_geometry(snapshot_dir)
    official = load_ap6_official_facts(snapshot_dir)
    osm_relation = load_ap6_osm_relation_snapshot(snapshot_dir)
    facts = official["facts"]
    corrected = copy.deepcopy(record)
    sampled_hero, hero_sampling_policy = sampler(hero, corrected)
    sampled_profile, sampling_policy = sampler(profile, corrected)
    if len(sampled_hero) != len(hero) or any(len(point) != 3 for point in sampled_hero):
        _fail("AP6 elevation sampler did not return one elevation per hero point")
    if len(sampled_profile) != len(profile) or any(
        len(point) != 3 for point in sampled_profile
    ):
        _fail("AP6 elevation sampler did not return one elevation per profile point")
    if hero_sampling_policy.get("source_ref") != sampling_policy.get("source_ref"):
        _fail("AP6 hero and profile elevation sources do not match")

    corrected["subtitle"] = "CORVIGLIA > ST-GINGOLPH / 695 KM"
    corrected["details"][0] = "695 KM / 43 STAGES / ROUTE 6"
    route_source = _source_by_id(corrected, "osm-route")
    route_source.update(
        {
            "publisher": "OpenStreetMap contributors via Waymarked Trails",
            "url": f"https://www.openstreetmap.org/relation/{AP6_RELATION_ID}",
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "use": (
                "one source-ordered route chain; A5 hero simplification and "
                "complete profile-only geometry"
            ),
            "retrieved_at": "2026-08-04T21:21:00Z",
            "relation_id": AP6_RELATION_ID,
            "relation_version": osm_relation["version"],
            "relation_timestamp": osm_relation["timestamp"],
            "acquisition_url": (
                "https://hiking.waymarkedtrails.org/api/v1/details/relation/"
                "18021781?lang=en"
            ),
            "waymarked_snapshot_sha256": AP6_WAYMARKED_RAW_SHA256,
            "osm_relation_snapshot_sha256": AP6_OSM_RELATION_RAW_SHA256,
            "osm_relation_canonical_snapshot_sha256": (
                AP6_OSM_RELATION_CANONICAL_SHA256
            ),
            "osm_relation_acquisition_url": (
                "https://api.openstreetmap.org/api/0.6/relation/18021781.json"
            ),
            "snapshot_sha256": AP6_WAYMARKED_RAW_SHA256,
            "source_route_length_m": int(details["route"]["length"]),
            "complete_profile_point_count": len(profile),
        }
    )
    official_source = _source_by_id(corrected, "official-identity")
    official_source.update(
        {
            "publisher": "SwitzerlandMobility",
            "url": official["url"],
            "license": "reference-only",
            "attribution": "Identity reference only",
            "use": (
                "frozen route identity, endpoints, published distance and stage "
                "count; no rendered map copied"
            ),
            "retrieved_at": official["retrieved_at"],
            "acquisition_url": official["acquisition_url"],
            "raw_snapshot_sha256": official["raw_snapshot_sha256"],
            "published_distance_km": facts["published_distance_km"],
            "stage_count": facts["stage_count"],
            "published_ascent_m": facts["published_ascent_m"],
            "published_descent_m": facts["published_descent_m"],
        }
    )

    route = corrected["route"]
    elevation_source_ref = str(sampling_policy["source_ref"])
    route.update(
        {
            "relation_id": AP6_RELATION_ID,
            "relation_version": osm_relation["version"],
            "relation_timestamp": osm_relation["timestamp"],
            "official_distance_km": 695.0,
            "official_stage_count": 43,
            "source_length_m": int(details["route"]["length"]),
            "simplification_webmercator_m": AP6_HERO_TOLERANCE_M,
            "segment_count": 1,
            "segment_ordering": "single-source-ordered-continuous-chain-v1",
            "geometry_recipe": {
                "status": "one-current-main-itinerary-chain",
                "source_snapshot_sha256": AP6_WAYMARKED_RAW_SHA256,
                "hero_use": "a5-physical-simplification-only",
                "profile_use": "complete-source-chain",
                "appendices": "excluded-by-waymarked-main-traversal",
            },
            "profile_status": "source-elevation-sampled",
            "profile_geometry_status": (
                "complete-source-ordered-profile-only-not-map-rendered-v1"
            ),
            "profile_source_point_count": len(sampled_profile),
            "profile_source_measured_length_m": round(measured_m, 3),
            "profile_source_snapshot_sha256": AP6_WAYMARKED_RAW_SHA256,
            "profile_geometry_sha256": _canonical_sha256(profile),
            "segments": [
                {
                    "id": "walk-01",
                    "mode": "walk",
                    "source_ref": "osm-route",
                    "points": sampled_hero,
                    "elevation_sampling_policy": hero_sampling_policy,
                }
            ],
            "profile_segments": [
                {
                    "id": "profile-walk-01",
                    "mode": "walk",
                    "source_ref": "osm-route",
                    "points": sampled_profile,
                    "elevation_sampling_policy": sampling_policy,
                }
            ],
            "controls": [
                {
                    "kind": "start",
                    "name": "ST. MORITZ, CORVIGLIA",
                    "point": hero[0],
                    "source_ref": "official-identity",
                },
                {
                    "kind": "finish",
                    "name": "ST-GINGOLPH",
                    "point": hero[-1],
                    "source_ref": "official-identity",
                },
            ],
            "elevation_source_ref": elevation_source_ref,
            "elevation_method": TERRESTRIAL_ROUTE_ELEVATION_METHOD,
            "elevation_datum": "Mapzen composite source vertical datums",
            "elevation_sampling_policy": {
                **hero_sampling_policy,
                "source_sampled_point_count": hero_sampling_policy["point_count"],
                "ferry_segment_count": 0,
                "ferry_sea_surface_reference_point_count": 0,
            },
            "profile_elevation_sampling_policy": {
                **sampling_policy,
                "source_sampled_point_count": sampling_policy["point_count"],
                "complete_ordered_profile_geometry": True,
            },
        }
    )
    relief_terrain = corrected.get("context", {}).get("relief_terrain")
    if isinstance(relief_terrain, dict):
        # This 695 km continental field has exceptionally long whole contour
        # paths.  At the four-level semantic minimum, rebalance interior
        # elevation bands toward shorter complete source levels when the
        # detailed/map-led edition would breach the A5 ink ceiling.  The
        # terrain-relief edition remains unaffected.
        relief_terrain["detailed_density_rebalance_policy"] = (
            "level-banded-minimum-ink-v1"
        )
    route.pop("elevation_extrema_evidence", None)
    terrain_source = _source_by_id(corrected, elevation_source_ref)
    terrain_source["point_sampling_terrarium_source_sample_count"] = len(sampled_hero)
    terrain_source["profile_point_sampling_terrarium_source_sample_count"] = len(
        sampled_profile
    )
    terrain_source[
        "profile_point_sampling_nearest_nonnegative_fallback_count"
    ] = sampling_policy["nearest_eligible_fallback_count"]
    terrain_source["use"] = (
        "cached Terrarium DEM tiles; complete ordered profile-only route "
        "elevations and frozen terrain relief"
    )
    derivation = corrected.setdefault("terrain_derivation", {}).get(
        "global_terrarium"
    )
    if isinstance(derivation, dict):
        derivation.update(
            {
                "route_points_total": len(hero),
                "route_points_sampled": len(sampled_hero),
                "route_points_nearest_nonnegative_fallback": hero_sampling_policy[
                    "nearest_eligible_fallback_count"
                ],
                "profile_points_total": len(sampled_profile),
                "profile_points_sampled": len(sampled_profile),
                "profile_points_nearest_nonnegative_fallback": sampling_policy[
                    "nearest_eligible_fallback_count"
                ],
            }
        )
    notes = corrected.setdefault("notes", [])
    note = (
        "AP6 identity/distance frozen from SwitzerlandMobility (695 km, 43 "
        "stages); one current ODbL Waymarked chain supplies a simplified A5 hero "
        "and a separate complete sampled profile axis."
    )
    if isinstance(notes, list) and note not in notes:
        notes.append(note)
    return corrected


def apply_factual_enrichment(
    catalog: dict[str, Any],
    *,
    terrain_cache_dir: Path,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    peak_overlay: dict[str, Any],
) -> dict[str, Any]:
    if catalog.get("schema_version") != 1 or catalog.get("id") != RELEASE_ID:
        _fail(f"catalog must use schema 1 / id {RELEASE_ID!r}")
    records = catalog.get("plates")
    if not isinstance(records, list) or len(records) != 40:
        _fail("catalog must contain exactly forty release plates")
    by_id = {
        str(record.get("id")): record
        for record in records
        if isinstance(record, dict)
    }
    if AP6_ID not in by_id or TMB_ID not in by_id or len(by_id) != 40:
        _fail("release identity inventory is incomplete or duplicated")
    mont_blanc_node = load_mont_blanc_snapshot(snapshot_dir)
    validate_mont_blanc_overlay(peak_overlay, node=mont_blanc_node)

    result = copy.deepcopy(catalog)
    for index, record in enumerate(result["plates"]):
        if record.get("id") == AP6_ID:
            result["plates"][index] = apply_ap6_correction(
                record,
                snapshot_dir=snapshot_dir,
                sampler=lambda points, target: _offline_profile_sampler(
                    points,
                    target,
                    terrain_cache_dir=terrain_cache_dir,
                ),
            )
            break
    result = apply_peak_overlay_only(
        result,
        peak_overlay,
        subject_id=TMB_ID,
    )
    result["factual_enrichment"] = {
        "policy_id": "hiking-factual-enrichment-ap6-tmb-v1",
        "applied_after_source_precedence": True,
        "subjects": [AP6_ID, TMB_ID],
        "ap6_waymarked_snapshot_sha256": AP6_WAYMARKED_RAW_SHA256,
        "ap6_osm_relation_snapshot_sha256": AP6_OSM_RELATION_RAW_SHA256,
        "ap6_osm_relation_canonical_snapshot_sha256": (
            AP6_OSM_RELATION_CANONICAL_SHA256
        ),
        "ap6_official_distance_km": 695.0,
        "ap6_official_stage_count": 43,
        "mont_blanc_source_object": "node/281399025",
        "mont_blanc_canonical_snapshot_sha256": MONT_BLANC_CANONICAL_SHA256,
    }
    return result


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _fail(f"could not write {path}: {exc}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--terrain-cache-dir", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--peak-overlay", type=Path, default=DEFAULT_PEAK_OVERLAY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog = _load_object(args.catalog, label="release catalog")
        overlay = _load_object(args.peak_overlay, label="peak elevation overlay")
        result = apply_factual_enrichment(
            catalog,
            terrain_cache_dir=args.terrain_cache_dir,
            snapshot_dir=args.snapshot_dir,
            peak_overlay=overlay,
        )
        _validate_release_catalog(result)
        _write_atomic(args.output, result)
    except (FactualEnrichmentError, ValueError, OSError) as exc:
        print(f"apply_hiking_factual_enrichment: {exc}", file=sys.stderr)
        return 2
    print(f"Applied frozen AP6/TMB factual enrichment -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
