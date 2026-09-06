"""Pinned, scale-aware basemap context for transit-network contracts.

Transit routes remain the factual subject.  This module imports a separately
hashed OSM snapshot as subdued road, water, railway, park, boundary, and
landmark-building context.  Rendering never performs a live request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from shapely.geometry import GeometryCollection, LineString, MultiLineString, box
from shapely.ops import unary_union

from .cartography import is_landmark_building_candidate
from .features import (
    LOCAL_HIGHWAYS,
    MAJOR_HIGHWAYS,
    SECONDARY_HIGHWAYS,
    extract_features,
)
from .models import AcquisitionResult, BoundingBox, MapFeature, MapPlotterError
from .osm import fetch_overpass, load_overpass_file
from .transit import canonical_contract_bytes, load_transit_network


ALL_CONTEXT_LAYERS = frozenset(
    {
        "roads_major",
        "roads_secondary",
        "roads_local",
        "roads_other",
        "paths",
        "road_areas",
        "water_areas",
        "rivers",
        "waterways",
        "railways",
        "green_space",
        "buildings",
        "boundaries",
    }
)

TRANSIT_KIND_BY_SOURCE_LAYER = {
    "roads_major": "roads-major",
    "roads_secondary": "roads-secondary",
    "roads_local": "roads-local",
    "roads_other": "roads-other",
    "road_areas": "roads-other",
    "paths": "paths",
    "water_areas": "water-areas",
    "rivers": "water-lines",
    "waterways": "water-lines",
    "railways": "railways",
    "green_space": "green-space",
    "buildings": "buildings",
    "boundaries": "boundaries",
}

_STABLE_SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
MAX_DETAILED_METROPOLITAN_CONTEXT_AREA_KM2 = 1_500.0
CONTEXT_PROFILES = frozenset({"auto", "detail", "house", "plot"})
DEFAULT_CONTEXT_PROFILE = "house"

_HOUSE_WATER_LAYERS = frozenset({"water_areas", "rivers", "waterways"})
HOUSE_LARGE_METROPOLITAN_CONTEXT_LAYERS = frozenset(
    {
        "roads_major",
        "roads_secondary",
        "roads_local",
        "railways",
        *_HOUSE_WATER_LAYERS,
        "green_space",
        "buildings",
        "boundaries",
    }
)
HOUSE_REGIONAL_CONTEXT_LAYERS = frozenset(
    {
        "roads_major",
        "roads_secondary",
        "railways",
        *_HOUSE_WATER_LAYERS,
        "boundaries",
    }
)
HOUSE_NATIONAL_CONTEXT_LAYERS = frozenset(
    {
        "roads_major",
        "railways",
        *_HOUSE_WATER_LAYERS,
        "boundaries",
    }
)

_CONTEXT_SOURCE_TAG_KEYS = (
    "highway",
    "railway",
    "building",
    "amenity",
    "leisure",
    "tourism",
    "healthcare",
    "historic",
    "heritage",
    "service",
    "usage",
    "bridge",
    "tunnel",
    "layer",
    "level",
    "name",
)


@dataclass(frozen=True, slots=True)
class ContextSnapshotProvenance:
    """Caller-supplied identity for one immutable OSM context snapshot.

    A local filename and the current clock are not source provenance.  The
    caller must therefore record a stable contract source ID, the public or
    archival URI represented by the bytes, and the date those bytes were
    retrieved.  Reusing these values with identical bytes produces identical
    contract bytes regardless of the local snapshot filename.
    """

    source_id: str
    source_url: str
    retrieved_at: str

    def validated(self) -> "ContextSnapshotProvenance":
        source_id = self.source_id.strip()
        source_url = self.source_url.strip()
        retrieved_at = self.retrieved_at.strip()
        if _STABLE_SOURCE_ID.fullmatch(source_id) is None:
            raise MapPlotterError(
                "Transit context source_id must use lower-case letters, digits, "
                "and hyphens."
            )
        if ":" not in source_url or source_url.startswith("file:"):
            raise MapPlotterError(
                "Transit context source_url must be a stable non-file URI for "
                "the represented snapshot source."
            )
        try:
            parsed_date = date.fromisoformat(retrieved_at)
        except ValueError as exc:
            raise MapPlotterError(
                "Transit context retrieved_at must be an explicit ISO-8601 date."
            ) from exc
        return ContextSnapshotProvenance(
            source_id=source_id,
            source_url=source_url,
            retrieved_at=parsed_date.isoformat(),
        )


def _require_provenance(
    provenance: ContextSnapshotProvenance | None,
) -> ContextSnapshotProvenance:
    if provenance is None:
        raise MapPlotterError(
            "Transit context attachment requires explicit snapshot provenance: "
            "source_id, source_url, and retrieved_at."
        )
    return provenance.validated()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(
            f"Could not hash transit context snapshot {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "part"


def context_bbox(network_path: Path, *, padding_fraction: float = 0.065) -> BoundingBox:
    if not 0.0 <= padding_fraction <= 0.25:
        raise MapPlotterError("Transit context padding must be in [0, 0.25].")
    network = load_transit_network(network_path)
    bounds = network.bbox()
    lon_pad = max((bounds.east - bounds.west) * padding_fraction, 0.002)
    lat_pad = max((bounds.north - bounds.south) * padding_fraction, 0.002)
    return BoundingBox(
        max(-180.0, bounds.west - lon_pad),
        max(-85.0, bounds.south - lat_pad),
        min(180.0, bounds.east + lon_pad),
        min(85.0, bounds.north + lat_pad),
    )


def context_layers_for_bbox(
    bbox: BoundingBox, *, profile: str = DEFAULT_CONTEXT_PROFILE
) -> frozenset[str]:
    """Keep only detail that remains physically legible at the route extent.

    ``house`` is the current product rule: complete city context for compact and
    ordinary metropolitan networks, a bounded but still locally legible London
    treatment, and progressively quieter regional/national context. ``auto``
    preserves the established detail-first series rule. ``plot`` remains the
    legacy lower-ink production proof. ``detail`` requests the complete
    supported vocabulary and remains subject to the response-size gate.
    """

    if profile not in CONTEXT_PROFILES:
        raise MapPlotterError(
            "Transit context profile must be auto, detail, house, or plot."
        )
    if profile == "detail":
        return ALL_CONTEXT_LAYERS

    span_km = max(bbox.approximate_width_m, bbox.approximate_height_m) / 1000.0
    if profile == "house":
        if span_km > 250.0:
            return HOUSE_NATIONAL_CONTEXT_LAYERS
        if span_km > 80.0:
            return HOUSE_REGIONAL_CONTEXT_LAYERS
        if bbox.approximate_area_km2 > MAX_DETAILED_METROPOLITAN_CONTEXT_AREA_KM2:
            return HOUSE_LARGE_METROPOLITAN_CONTEXT_LAYERS
        return ALL_CONTEXT_LAYERS
    if profile == "plot":
        if (
            span_km > 80.0
            or bbox.approximate_area_km2 > MAX_DETAILED_METROPOLITAN_CONTEXT_AREA_KM2
        ):
            return frozenset({"roads_major", "water_areas", "waterways"})
        if span_km > 12.0:
            return frozenset(
                {
                    "roads_major",
                    "roads_secondary",
                    "water_areas",
                    "waterways",
                    "railways",
                    "green_space",
                    "buildings",
                }
            )
        return ALL_CONTEXT_LAYERS
    if span_km > 250.0:
        return frozenset({"water_areas", "rivers", "waterways", "boundaries"})
    if (
        span_km > 80.0
        or bbox.approximate_area_km2 > MAX_DETAILED_METROPOLITAN_CONTEXT_AREA_KM2
    ):
        return frozenset({"roads_major", "water_areas", "waterways"})
    if span_km > 20.0:
        return frozenset(
            {
                "roads_major",
                "roads_secondary",
                "water_areas",
                "rivers",
                "waterways",
                "railways",
                "green_space",
                "buildings",
                "boundaries",
            }
        )
    return ALL_CONTEXT_LAYERS


def _families_for_layers(layers: Iterable[str]) -> tuple[str, ...]:
    values = set(layers)
    result: list[str] = []
    if values.intersection(
        {
            "roads_major",
            "roads_secondary",
            "roads_local",
            "roads_other",
            "paths",
            "road_areas",
        }
    ):
        result.append("roads")
    if values.intersection({"water_areas", "rivers", "waterways"}):
        result.append("water")
    if "railways" in values:
        result.append("railways")
    if "green_space" in values:
        result.append("parks")
    if "buildings" in values:
        result.append("buildings")
    if "boundaries" in values:
        result.append("boundaries")
    return tuple(result)


def _road_values_for_layers(layers: frozenset[str]) -> frozenset[str] | None:
    """Apply the declared scale policy at acquisition as well as extraction."""

    road_layers = layers.intersection(
        {
            "roads_major",
            "roads_secondary",
            "roads_local",
            "roads_other",
            "paths",
            "road_areas",
        }
    )
    if not road_layers:
        return frozenset()
    if road_layers == {"roads_major"}:
        return MAJOR_HIGHWAYS
    if road_layers == {"roads_major", "roads_secondary"}:
        return MAJOR_HIGHWAYS | SECONDARY_HIGHWAYS
    if road_layers == {"roads_major", "roads_secondary", "roads_local"}:
        return MAJOR_HIGHWAYS | SECONDARY_HIGHWAYS | LOCAL_HIGHWAYS
    return None


def fetch_transit_context(
    network_path: Path,
    *,
    user_agent: str,
    cache_dir: Path,
    endpoint: str,
    timeout_s: int = 240,
    refresh: bool = False,
    max_response_mb: float = 256.0,
    profile: str = DEFAULT_CONTEXT_PROFILE,
) -> tuple[AcquisitionResult, BoundingBox, frozenset[str]]:
    """Explicit maintenance-time Overpass fetch for an urban/regional plate."""

    bbox = context_bbox(network_path)
    if bbox.approximate_area_km2 > 20_000.0:
        raise MapPlotterError(
            "This transit extent is too large for a responsible public Overpass "
            "context request. Use a pinned regional .osm.pbf extract instead."
        )
    layers = context_layers_for_bbox(bbox, profile=profile)
    span_km = max(bbox.approximate_width_m, bbox.approximate_height_m) / 1000.0
    large_metropolitan_house_context = (
        profile == "house"
        and span_km <= 80.0
        and bbox.approximate_area_km2 > MAX_DETAILED_METROPOLITAN_CONTEXT_AREA_KM2
    )
    major_river_context_only = (
        profile == "plot" and span_km > 12.0
    ) or layers == frozenset({"roads_major", "water_areas", "waterways"})
    acquisition = fetch_overpass(
        bbox,
        _families_for_layers(layers),
        endpoint=endpoint,
        user_agent=user_agent,
        cache_dir=cache_dir,
        timeout_s=timeout_s,
        refresh=refresh,
        landmark_buildings_only=True,
        landmark_green_space_only=(
            (profile == "plot" and span_km > 12.0) or large_metropolitan_house_context
        ),
        road_highway_values=_road_values_for_layers(layers),
        linear_water_only="water_areas" not in layers,
        major_river_context_only=major_river_context_only,
        max_response_mb=max_response_mb,
    )
    return acquisition, bbox, layers


def _line_parts(geometry: Any) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [line for child in geometry.geoms for line in _line_parts(child)]
    return []


def _aligned_node_refs(
    feature: MapFeature, coordinates: list[tuple[float, float]]
) -> list[str] | None:
    """Return source node IDs only when they align with the clipped geometry.

    Shapely may insert a synthetic coordinate where a source way crosses the
    context boundary.  Retaining the original way's node list in that case
    would make topology assembly mistake the clipped endpoint for a real OSM
    node.  Exact contiguous source subsequences (in either orientation) remain
    useful and safe join metadata.
    """

    if not feature.node_refs or len(feature.node_refs) != len(feature.points):
        return None
    source_coordinates = [(float(lon), float(lat)) for lat, lon in feature.points]
    coordinate_count = len(coordinates)
    if coordinate_count > len(source_coordinates):
        return None
    for oriented_coordinates, oriented_refs in (
        (source_coordinates, list(feature.node_refs)),
        (list(reversed(source_coordinates)), list(reversed(feature.node_refs))),
    ):
        for start in range(len(oriented_coordinates) - coordinate_count + 1):
            if oriented_coordinates[start : start + coordinate_count] == coordinates:
                return oriented_refs[start : start + coordinate_count]
    return None


def _context_records(
    features: Iterable[MapFeature],
    *,
    bbox: BoundingBox,
    layers: frozenset[str],
    source_ref: str,
) -> list[dict[str, Any]]:
    clip = box(bbox.west, bbox.south, bbox.east, bbox.north)
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for feature in sorted(
        features,
        key=lambda item: (item.layer, item.osm_type, item.osm_id, item.part),
    ):
        if feature.layer not in layers or len(feature.points) < 2:
            continue
        if feature.layer == "buildings" and not is_landmark_building_candidate(
            feature.tags
        ):
            # Match the university/marathon landmark policy and the live
            # landmark-only query. Ordinary houses and sheds would swamp both
            # the transit route and the finite Purple 0.25 pen budget.
            continue
        # Canonical MapFeature coordinates are (latitude, longitude); transit
        # contracts consistently store WGS84 as [longitude, latitude].
        line = LineString([(lon, lat) for lat, lon in feature.points])
        if line.is_empty or line.length <= 0.0:
            continue
        for piece_index, piece in enumerate(_line_parts(line.intersection(clip))):
            coordinates = [(float(x), float(y)) for x, y in piece.coords]
            if len(coordinates) < 2 or all(
                point == coordinates[0] for point in coordinates[1:]
            ):
                continue
            base_id = (
                f"context-{_slug(feature.layer)}-{_slug(feature.osm_type)}-"
                f"{_slug(feature.osm_id)}-{_slug(feature.part)}-{piece_index}"
            )
            feature_id = base_id
            duplicate = 1
            while feature_id in seen_ids:
                duplicate += 1
                feature_id = f"{base_id}-{duplicate}"
            seen_ids.add(feature_id)
            source_tags = {
                key: str(feature.tags[key]).strip()
                for key in _CONTEXT_SOURCE_TAG_KEYS
                if key in feature.tags and str(feature.tags[key]).strip()
            }
            record: dict[str, Any] = {
                "id": feature_id,
                "kind": TRANSIT_KIND_BY_SOURCE_LAYER[feature.layer],
                "geometry": [[lon, lat] for lon, lat in coordinates],
                "source_ref": source_ref,
                "source_object": f"{feature.osm_type}/{feature.osm_id}#{feature.part}",
                "source_layer": feature.layer,
            }
            if source_tags:
                record["source_tags"] = source_tags
            aligned_node_refs = _aligned_node_refs(feature, coordinates)
            if aligned_node_refs:
                record["node_refs"] = aligned_node_refs
            if feature.ring_role is not None:
                # Canonical source features call this ``polygon_ring``; the
                # transit contract deliberately exposes the smaller line /
                # area-ring vocabulary.  Open or clipped source rings retain
                # their source role so later joins can reconstruct lineage.
                record["geometry_type"] = "area-ring"
                record["ring_role"] = feature.ring_role
            records.append(record)
    return records


def attach_context(
    network_path: Path,
    acquisition: AcquisitionResult,
    *,
    bbox: BoundingBox,
    enabled_layers: frozenset[str],
    source_path: Path,
    output_path: Path,
    provenance: ContextSnapshotProvenance | None = None,
    context_profile: str = DEFAULT_CONTEXT_PROFILE,
) -> dict[str, Any]:
    """Attach one hashed context snapshot and revalidate the whole contract."""

    checked_provenance = _require_provenance(provenance)
    if context_profile not in CONTEXT_PROFILES:
        raise MapPlotterError(
            "Transit context profile must be auto, detail, house, or plot."
        )
    network = load_transit_network(network_path)
    features = (
        list(acquisition.features)
        if acquisition.features is not None
        else extract_features(acquisition.data, set(enabled_layers))
    )
    records = _context_records(
        features,
        bbox=bbox,
        layers=enabled_layers,
        source_ref=checked_provenance.source_id,
    )
    if not records:
        raise MapPlotterError(
            "The supplied transit context snapshot produced no usable features."
        )
    source_digest = _sha256(source_path)
    query_digest = (
        hashlib.sha256(acquisition.query.encode("utf-8")).hexdigest()
        if acquisition.query
        else None
    )
    document = network.as_dict()
    document["sources"] = [
        source
        for source in document["sources"]
        if source["id"] != checked_provenance.source_id
    ]
    document["sources"].append(
        {
            "id": checked_provenance.source_id,
            "publisher": "OpenStreetMap contributors",
            "url": checked_provenance.source_url,
            "licence": "ODbL 1.0",
            "attribution": "© OpenStreetMap contributors",
            "retrieved_at": checked_provenance.retrieved_at,
            "sha256": source_digest,
            "use": (
                f"Pinned basemap context snapshot; profile {context_profile}"
                + (f"; extraction query SHA-256 {query_digest}" if query_digest else "")
            ),
            "commercial_reuse_status": "commercial-allowed",
        }
    )
    document["context"] = records
    document["omissions"] = [
        item
        for item in document.get("omissions", [])
        if not (item.get("kind") == "context" and item.get("status") == "not-supplied")
    ]
    document["notes"] = [
        *document.get("notes", []),
        (
            "Scale-aware context imported from stable snapshot "
            f"{checked_provenance.source_id} ({source_digest}): "
            f"{len(records)} clipped line/ring features from {len(features)} canonical "
            f"features using the {context_profile} profile."
        ),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checked = load_transit_network(temporary)
    canonical = canonical_contract_bytes(checked)
    matches_canonical = temporary.stat().st_size == len(canonical)
    if matches_canonical:
        with temporary.open("rb") as stream:
            for offset in range(0, len(canonical), 1024 * 1024):
                if stream.read(1024 * 1024) != canonical[offset : offset + 1024 * 1024]:
                    matches_canonical = False
                    break
    if not matches_canonical:
        raise MapPlotterError("Transit context output is not canonically reproducible.")
    temporary.replace(output_path)
    return {
        "path": str(output_path.resolve()),
        "sha256": _sha256(output_path),
        "network_id": checked.id,
        "context_feature_count": len(checked.context),
        "source_feature_count": len(features),
        "enabled_layers": sorted(enabled_layers),
        "source_sha256": source_digest,
        "source_id": checked_provenance.source_id,
        "source_url": checked_provenance.source_url,
        "retrieved_at": checked_provenance.retrieved_at,
        "context_profile": context_profile,
    }


def attach_overpass_file(
    network_path: Path,
    overpass_path: Path,
    *,
    output_path: Path,
    provenance: ContextSnapshotProvenance | None = None,
    profile: str = DEFAULT_CONTEXT_PROFILE,
) -> dict[str, Any]:
    acquisition = load_overpass_file(overpass_path)
    bbox = context_bbox(network_path)
    layers = context_layers_for_bbox(bbox, profile=profile)
    return attach_context(
        network_path,
        acquisition,
        bbox=bbox,
        enabled_layers=layers,
        source_path=overpass_path,
        output_path=output_path,
        provenance=provenance,
        context_profile=profile,
    )


def attach_pbf_file(
    network_path: Path,
    pbf_path: Path,
    *,
    output_path: Path,
    provenance: ContextSnapshotProvenance | None = None,
    profile: str = DEFAULT_CONTEXT_PROFILE,
) -> dict[str, Any]:
    from .pbf import load_pbf

    bbox = context_bbox(network_path)
    layers = context_layers_for_bbox(bbox, profile=profile)
    acquisition = load_pbf(pbf_path, bbox, set(layers))
    return attach_context(
        network_path,
        acquisition,
        bbox=bbox,
        enabled_layers=layers,
        source_path=pbf_path,
        output_path=output_path,
        provenance=provenance,
        context_profile=profile,
    )


def attach_pbf_files(
    network_path: Path,
    pbf_paths: Iterable[Path],
    *,
    output_path: Path,
    provenance: ContextSnapshotProvenance | None = None,
    profile: str = DEFAULT_CONTEXT_PROFILE,
) -> dict[str, Any]:
    """Attach one deterministic context assembled from adjacent PBF extracts.

    National and edge-of-region networks commonly cross an extract boundary.
    Each member is independently hash-pinned, overlapping canonical objects
    must be byte-equivalent, and the union of the PBF header extents must cover
    the requested network box.  No proximity stitching is performed here.
    """

    from .pbf import load_pbf

    paths = tuple(Path(path) for path in pbf_paths)
    if not paths:
        raise MapPlotterError("At least one PBF source-pack member is required.")
    resolved = tuple(path.expanduser().resolve() for path in paths)
    if len(resolved) != len(set(resolved)):
        raise MapPlotterError("A transit PBF source pack cannot repeat a member.")

    bbox = context_bbox(network_path)
    layers = context_layers_for_bbox(bbox, profile=profile)
    members: list[dict[str, Any]] = []
    features_by_identity: dict[tuple[str, ...], MapFeature] = {}
    header_extents = []
    for path in sorted(resolved, key=lambda value: str(value)):
        acquisition = load_pbf(path, bbox, set(layers))
        source_digest = _sha256(path)
        metadata = acquisition.source_metadata
        header_bbox = metadata.get("coverage", {}).get("header_bbox_wgs84")
        if isinstance(header_bbox, dict):
            try:
                header_extents.append(
                    box(
                        float(header_bbox["west"]),
                        float(header_bbox["south"]),
                        float(header_bbox["east"]),
                        float(header_bbox["north"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                pass
        members.append(
            {
                "sha256": source_digest,
                "size_bytes": path.stat().st_size,
                "source_timestamp": metadata.get("source_timestamp"),
                "pbf_header": metadata.get("pbf_header", {}),
            }
        )
        for feature in acquisition.features or []:
            identity = (
                feature.layer,
                feature.osm_type,
                feature.osm_id,
                feature.part,
                feature.geometry_type,
                feature.ring_role or "",
            )
            existing = features_by_identity.get(identity)
            if existing is None:
                features_by_identity[identity] = feature
                continue
            if existing != feature:
                raise MapPlotterError(
                    "Overlapping PBF members disagree for canonical object "
                    f"{'/'.join(identity)}; use one consistent snapshot family."
                )

    requested_extent = box(bbox.west, bbox.south, bbox.east, bbox.north)
    coverage_proven = bool(header_extents) and unary_union(header_extents).covers(
        requested_extent
    )
    if not coverage_proven:
        raise MapPlotterError(
            "The union of PBF header extents does not cover the complete transit "
            "context box; add the missing adjacent extract rather than emitting "
            "a blank edge."
        )

    source_pack_members = sorted(members, key=lambda item: item["sha256"])
    source_pack = {
        "schema_version": 1,
        "kind": "transit-context-pbf-source-pack",
        "members": source_pack_members,
        "requested_bbox_wgs84": bbox.as_dict(),
        "enabled_layers": sorted(layers),
        "coverage_proven": True,
    }
    source_pack_path = output_path.with_suffix(".context-source-pack.json")
    source_pack_path.parent.mkdir(parents=True, exist_ok=True)
    source_pack_path.write_text(
        json.dumps(source_pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    combined = AcquisitionResult(
        data={},
        endpoint="local-pbf-source-pack",
        query=None,
        cache_path=None,
        from_cache=False,
        features=[features_by_identity[key] for key in sorted(features_by_identity)],
        source_metadata={
            "format": "osm.pbf-source-pack-v1",
            "members": source_pack["members"],
            "coverage_proven": True,
        },
    )
    result = attach_context(
        network_path,
        combined,
        bbox=bbox,
        enabled_layers=layers,
        source_path=source_pack_path,
        output_path=output_path,
        provenance=provenance,
        context_profile=profile,
    )
    return {
        **result,
        "source_pack_path": str(source_pack_path.resolve()),
        "source_pack_member_count": len(members),
        "source_pack_member_sha256s": [
            item["sha256"] for item in source_pack_members
        ],
        "coverage_proven": True,
    }
