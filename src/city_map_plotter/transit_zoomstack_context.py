"""Attach pinned OS Open Zoomstack context to national transit contracts.

The adapter is intentionally one-way: Zoomstack supplies subdued physical
infrastructure and cartographic context only.  It is never consulted when
selecting, connecting, or naming an operator route.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import replace
import hashlib
import json
from math import isfinite
import re
from typing import Any

from shapely.geometry import GeometryCollection, LineString, MultiLineString, box
from shapely.geometry.base import BaseGeometry

from .models import MapPlotterError
from .niche_common import PlateContext
from .transit import (
    ContextFeature,
    TransitNetwork,
    TransitSource,
    validate_transit_network,
)
from .transit_composition import aspect_aware_map_field
from .transit_extent import (
    DEFAULT_OPERATOR_CONTEXT_PADDING_FRACTION,
    NAMED_OPERATOR_KINDS,
    named_operator_projection_extent,
)
from .transit_osm_coastline import (
    GreatBritainCoastline,
    OSM_GB_COASTLINE_POLICY_VERSION,
    OSM_GB_COASTLINE_SOURCE_URL,
)
from .transit_topology import Projector, projector_for
from .transit_zoomstack import (
    GB_CONTEXT_GEOGRAPHIC_SCOPE,
    NationalContextLine,
    PhysicalRailFeature,
    ZoomstackPhysicalRail,
)


ZOOMSTACK_TRANSIT_CONTEXT_POLICY_VERSION = (
    "zoomstack-plus-osm-coastline-house-national-context-v5"
)
ZOOMSTACK_SOURCE_URL = "https://osdatahub.os.uk/downloads/open/OpenZoomstack"
ZOOMSTACK_SOURCE_LICENCE = "Open Government Licence v3.0"
ZOOMSTACK_SOURCE_ATTRIBUTION = (
    "Contains OS data © Crown copyright and database right 2026"
)
OSM_COASTLINE_SOURCE_LICENCE = "Open Data Commons Open Database Licence 1.0"
OSM_COASTLINE_SOURCE_ATTRIBUTION = "© OpenStreetMap contributors"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COASTLINE_SIMPLIFICATION_MM = 0.04
_CONTEXT_KIND = {
    "coastline": "coastline",
    "surface-water-bank": "water-areas",
    "road-motorway": "roads-strategic",
    "road-primary": "roads-major",
    "national-boundary": "boundaries",
}


def _line_parts(value: BaseGeometry) -> list[LineString]:
    if isinstance(value, LineString):
        return [value] if not value.is_empty else []
    if isinstance(value, MultiLineString):
        return [item for item in value.geoms if not item.is_empty]
    if isinstance(value, GeometryCollection):
        return [line for item in value.geoms for line in _line_parts(item)]
    return []


def _canonical_geometry(
    geometry: Iterable[Sequence[float]],
) -> tuple[tuple[float, float], ...]:
    points = tuple(
        (round(float(lon), 10), round(float(lat), 10)) for lon, lat in geometry
    )
    if len(points) < 2 or all(point == points[0] for point in points[1:]):
        return ()
    reverse = tuple(reversed(points))
    return min(points, reverse)


def _clipped_parts(
    geometry: tuple[tuple[float, float], ...],
    clip: BaseGeometry,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    try:
        clipped = LineString(geometry).intersection(clip)
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(f"Cannot clip Zoomstack context linework: {exc}") from exc
    return tuple(
        sorted(
            points
            for part in _line_parts(clipped)
            if (points := _canonical_geometry(part.coords))
        )
    )


def _bbox_intersects(
    first: Sequence[float], second: Sequence[float]
) -> bool:
    return not (
        first[2] < second[0]
        or first[0] > second[2]
        or first[3] < second[1]
        or first[1] > second[3]
    )


def _paper_simplified_source_subset(
    geometry: tuple[tuple[float, float], ...],
    *,
    projector: Projector,
    tolerance_mm: float,
) -> tuple[tuple[tuple[float, float], ...], float, bool]:
    """Generalise in paper space while retaining only source/clipping vertices."""

    paper_points = tuple(projector.point(lon, lat) for lon, lat in geometry)
    original = LineString(paper_points)
    try:
        simplified = original.simplify(tolerance_mm, preserve_topology=True)
    except (TypeError, ValueError):
        return geometry, 0.0, True
    if not isinstance(simplified, LineString):
        return geometry, 0.0, True
    simplified_paper = tuple(
        (float(x), float(y)) for x, y in simplified.coords
    )
    closed = geometry[0] == geometry[-1]
    original_orientation = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(paper_points, paper_points[1:], strict=False)
    )
    simplified_orientation = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(
            simplified_paper, simplified_paper[1:], strict=False
        )
    )
    invalid = (
        len(simplified_paper) < (4 if closed else 2)
        or (simplified_paper[0] == simplified_paper[-1]) != closed
        or (closed and (not simplified.is_ring or len(set(simplified_paper[:-1])) < 3))
        or (
            closed
            and abs(original_orientation) > 1e-12
            and original_orientation * simplified_orientation <= 0.0
        )
        or simplified.length <= 1e-9
    )
    deviation = float(original.hausdorff_distance(simplified))
    if invalid or not isfinite(deviation) or deviation > tolerance_mm + 1e-9:
        return geometry, 0.0, True

    # GEOS' Douglas-Peucker result is a subset of the input vertices.  Bind
    # those paper points back to the exact WGS84 source/clipping coordinates;
    # fail closed if a future GEOS implementation ever synthesises a point.
    source_by_paper: dict[tuple[float, float], list[tuple[float, float]]] = defaultdict(list)
    for paper, source in zip(paper_points, geometry, strict=True):
        source_by_paper[paper].append(source)
    output: list[tuple[float, float]] = []
    for paper in simplified_paper:
        candidates = source_by_paper.get(paper)
        if not candidates:
            return geometry, 0.0, True
        output.append(candidates[0])
    if closed:
        output[-1] = output[0]
    return tuple(output), deviation, False


def _authored_coastline_context(
    coastline: GreatBritainCoastline,
    *,
    clip: BaseGeometry,
    clip_bounds: tuple[float, float, float, float],
    projector: Projector,
    source_ref: str,
) -> tuple[list[ContextFeature], dict[str, Any]]:
    """Clip and physically generalise exact OSM coastline source ways."""

    output: list[ContextFeature] = []
    intersecting_way_ids: set[int] = set()
    represented_way_ids: set[int] = set()
    source_vertex_count = 0
    emitted_vertex_count = 0
    clipped_occurrence_count = 0
    fallback_count = 0
    maximum_hausdorff_mm = 0.0
    for way in coastline.ways:
        if not _bbox_intersects(way.bounds_wgs84, clip_bounds):
            continue
        parts = _line_parts(LineString(way.geometry).intersection(clip))
        parts = sorted(parts, key=lambda part: tuple(part.coords))
        if not parts:
            continue
        intersecting_way_ids.add(way.osm_way_id)
        source_coordinate_to_refs: dict[tuple[float, float], list[str]] = defaultdict(list)
        for coordinate, node_ref in zip(way.geometry, way.node_refs, strict=True):
            source_coordinate_to_refs[coordinate].append(str(node_ref))
        for piece_index, part in enumerate(parts):
            geometry = tuple((float(x), float(y)) for x, y in part.coords)
            if len(geometry) < 2 or all(point == geometry[0] for point in geometry[1:]):
                continue
            clipped_occurrence_count += 1
            source_vertex_count += len(geometry)
            simplified, deviation, used_fallback = _paper_simplified_source_subset(
                geometry,
                projector=projector,
                tolerance_mm=_COASTLINE_SIMPLIFICATION_MM,
            )
            fallback_count += int(used_fallback)
            maximum_hausdorff_mm = max(maximum_hausdorff_mm, deviation)
            emitted_vertex_count += len(simplified)
            node_refs: list[str] = []
            for coordinate in simplified:
                candidates = source_coordinate_to_refs.get(coordinate)
                if not candidates:
                    node_refs = []
                    break
                node_refs.append(candidates[0])
            payload = {
                "geometry": simplified,
                "osm_way_id": way.osm_way_id,
                "piece_index": piece_index,
                "source_sha256": coastline.source_sha256,
            }
            output.append(
                ContextFeature(
                    id=_feature_id("osm-authored-coastline", payload),
                    kind="coastline",
                    geometry=simplified,
                    source_ref=source_ref,
                    source_object=way.source_object,
                    source_layer="natural=coastline",
                    source_tags=(
                        ("authored_geometry", "true"),
                        ("geographic_scope", GB_CONTEXT_GEOGRAPHIC_SCOPE),
                        ("natural", "coastline"),
                        ("northern_ireland_included", "false"),
                        (
                            "paper_space_simplification_tolerance_mm",
                            f"{_COASTLINE_SIMPLIFICATION_MM:g}",
                        ),
                        ("source_direction", "land-left"),
                    ),
                    node_refs=tuple(node_refs),
                )
            )
            represented_way_ids.add(way.osm_way_id)
    if represented_way_ids != intersecting_way_ids:
        missing = sorted(intersecting_way_ids.difference(represented_way_ids))
        raise MapPlotterError(
            "Authored coastline clipping lost intersecting source ways: "
            f"{missing[:8]}."
        )
    in_bounds_source_objects = [
        f"way/{way_id}" for way_id in sorted(represented_way_ids)
    ]
    return output, {
        "policy_version": OSM_GB_COASTLINE_POLICY_VERSION,
        "loaded_selected_source_way_count": len(coastline.ways),
        "in_bounds_source_way_count": len(intersecting_way_ids),
        "represented_in_bounds_source_way_count": len(represented_way_ids),
        "in_bounds_source_way_parity": True,
        "clipped_geometry_occurrence_count": clipped_occurrence_count,
        "emitted_context_feature_count": len(output),
        "source_vertex_count_after_clipping": source_vertex_count,
        "emitted_vertex_count_after_paper_simplification": emitted_vertex_count,
        "removed_sub_nib_vertex_count": source_vertex_count - emitted_vertex_count,
        "paper_space_simplification_tolerance_mm": _COASTLINE_SIMPLIFICATION_MM,
        "maximum_paper_space_hausdorff_deviation_mm": round(
            maximum_hausdorff_mm, 9
        ),
        "simplification_fallback_count": fallback_count,
        "topology_preserving_simplification": True,
        "source_or_clip_vertex_subset_only": True,
        "authored_coastline_geometry_used": True,
        "zoomstack_sea_fill_geometry_used": False,
        "source_geometry_sha256": coastline.audit.get("geometry_sha256"),
        "source_object_lineage_sha256": coastline.audit.get(
            "selected_source_object_lineage_sha256"
        ),
        "in_bounds_source_object_lineage_sha256": hashlib.sha256(
            "\0".join(in_bounds_source_objects).encode("ascii")
        ).hexdigest(),
        "geographic_scope": GB_CONTEXT_GEOGRAPHIC_SCOPE,
        "northern_ireland_included": False,
        "northern_ireland_exclusion_verified": True,
        "invented_connector_count": 0,
        "reversed_way_count": 0,
        "proximity_join_count": 0,
    }


def _feature_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _physical_rail_context(
    features: tuple[PhysicalRailFeature, ...],
    *,
    clip: BaseGeometry,
    source_ref: str,
) -> tuple[list[ContextFeature], dict[str, Any]]:
    grouped: dict[
        tuple[tuple[float, float], ...],
        dict[str, set[Any]],
    ] = {}
    intersecting_indices: set[int] = set()
    represented_indices: set[int] = set()
    clipped_occurrences = 0
    for feature_index, feature in enumerate(features):
        parts = _clipped_parts(feature.geometry, clip)
        if not parts:
            continue
        intersecting_indices.add(feature_index)
        for geometry in parts:
            clipped_occurrences += 1
            record = grouped.setdefault(
                geometry,
                {
                    "feature_indices": set(),
                    "rail_types": set(),
                    "source_objects": set(),
                },
            )
            record["feature_indices"].add(feature_index)
            record["rail_types"].add(feature.rail_type)
            record["source_objects"].update(feature.source_objects)

    output: list[ContextFeature] = []
    for geometry, evidence in sorted(grouped.items()):
        feature_indices = {int(value) for value in evidence["feature_indices"]}
        represented_indices.update(feature_indices)
        rail_types = tuple(sorted(str(value) for value in evidence["rail_types"]))
        source_objects = tuple(
            sorted(str(value) for value in evidence["source_objects"])
        )
        payload = {
            "geometry": geometry,
            "rail_types": rail_types,
            "source_objects": source_objects,
        }
        output.append(
            ContextFeature(
                id=_feature_id("zoomstack-physical-rail", payload),
                kind="railways",
                geometry=geometry,
                source_ref=source_ref,
                source_object=";".join(source_objects),
                source_layer="rail",
                source_tags=(
                    ("permitted_use", "physical-infrastructure-context-only"),
                    ("zoomstack_rail_types", ";".join(rail_types)),
                ),
            )
        )
    if represented_indices != intersecting_indices:
        raise MapPlotterError(
            "Zoomstack in-bounds physical-rail parity failed during context conversion."
        )
    return output, {
        "loaded_source_feature_count": len(features),
        "in_bounds_source_feature_count": len(intersecting_indices),
        "represented_in_bounds_source_feature_count": len(represented_indices),
        "in_bounds_source_feature_parity": True,
        "clipped_geometry_occurrence_count": clipped_occurrences,
        "emitted_deduplicated_context_feature_count": len(output),
        "deduplicated_clipped_occurrence_count": clipped_occurrences - len(output),
        "source_feature_index_lineage_sha256": hashlib.sha256(
            json.dumps(sorted(represented_indices), separators=(",", ":")).encode(
                "ascii"
            )
        ).hexdigest(),
        "operator_service_geometry_claimed": False,
        "connected_routing_graph_claimed": False,
        "render_kind": "railways",
        "physical_pen_policy": "quiet Grey 0.25 mm beneath operator route",
    }


def _national_line_context(
    lines: tuple[NationalContextLine, ...],
    *,
    clip: BaseGeometry,
    source_ref: str,
    zoom: int,
    geographic_scope: str,
    northern_ireland_included: bool,
) -> tuple[list[ContextFeature], dict[str, Any]]:
    grouped: dict[
        tuple[str, tuple[tuple[float, float], ...]],
        set[str],
    ] = defaultdict(set)
    intersecting_indices: set[int] = set()
    represented_indices: set[int] = set()
    clipped_occurrences = 0
    for line_index, line in enumerate(lines):
        kind = _CONTEXT_KIND.get(line.context_class)
        if kind is None:
            raise MapPlotterError(
                f"Unsupported Zoomstack national context class {line.context_class!r}."
            )
        parts = _clipped_parts(line.geometry, clip)
        if not parts:
            continue
        intersecting_indices.add(line_index)
        for geometry in parts:
            clipped_occurrences += 1
            grouped[(kind, geometry)].add(line.context_class)

    output: list[ContextFeature] = []
    class_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    for (kind, geometry), context_classes in sorted(grouped.items()):
        source_classes = tuple(sorted(context_classes))
        class_counts.update(source_classes)
        kind_counts[kind] += 1
        payload = {
            "geometry": geometry,
            "kind": kind,
            "source_classes": source_classes,
            "zoom": zoom,
        }
        feature_id = _feature_id("zoomstack-national-context", payload)
        source_tags = [
            ("permitted_use", "quiet-national-cartographic-context-only"),
            ("zoomstack_context_classes", ";".join(source_classes)),
        ]
        if kind == "coastline":
            source_tags.extend(
                (
                    ("geographic_scope", geographic_scope),
                    (
                        "northern_ireland_included",
                        str(northern_ireland_included).lower(),
                    ),
                )
            )
        output.append(
            ContextFeature(
                id=feature_id,
                kind=kind,
                geometry=geometry,
                source_ref=source_ref,
                source_object=(
                    f"derived/zoomstack/z{zoom}/"
                    f"{'+'.join(source_classes)}/{feature_id.rsplit('-', 1)[-1]}"
                ),
                source_layer=";".join(source_classes),
                source_tags=tuple(source_tags),
                geometry_type=("area-ring" if kind == "water-areas" else "line"),
                ring_role=("outer" if kind == "water-areas" else None),
            )
        )
    # Every intersecting input line contributes at least one emitted group.  A
    # group can merge coincident input lines, so parity is tracked before
    # geometric deduplication rather than inferred from output count.
    for line_index, line in enumerate(lines):
        if line_index not in intersecting_indices:
            continue
        kind = _CONTEXT_KIND[line.context_class]
        if any(
            (kind, geometry) in grouped
            for geometry in _clipped_parts(line.geometry, clip)
        ):
            represented_indices.add(line_index)
    if represented_indices != intersecting_indices:
        raise MapPlotterError(
            "Zoomstack in-bounds national-context parity failed during conversion."
        )
    return output, {
        "loaded_source_line_count": len(lines),
        "in_bounds_source_line_count": len(intersecting_indices),
        "represented_in_bounds_source_line_count": len(represented_indices),
        "in_bounds_source_line_parity": True,
        "clipped_geometry_occurrence_count": clipped_occurrences,
        "emitted_deduplicated_context_feature_count": len(output),
        "deduplicated_clipped_occurrence_count": clipped_occurrences - len(output),
        "emitted_source_class_counts": dict(sorted(class_counts.items())),
        "emitted_transit_kind_counts": dict(sorted(kind_counts.items())),
        "geographic_scope": geographic_scope,
        "northern_ireland_included": northern_ireland_included,
        "operator_service_geometry_claimed": False,
    }


def attach_zoomstack_house_context(
    network: TransitNetwork,
    zoomstack: ZoomstackPhysicalRail,
    *,
    coastline: GreatBritainCoastline | None = None,
    retrieved_at: str = "2026-08-07",
    padding_fraction: float = DEFAULT_OPERATOR_CONTEXT_PADDING_FRACTION,
) -> tuple[TransitNetwork, dict[str, Any]]:
    """Return the mixed-source house context and its conversion ledger.

    Zoomstack's independently generalised sea-fill mosaics are explicitly
    barred from production coastline use.  Callers must supply the exact,
    hash-pinned OSM authored coastline pack.
    """

    if _SHA256_RE.fullmatch(zoomstack.source_sha256) is None:
        raise MapPlotterError("Zoomstack context source digest is malformed.")
    if zoomstack.audit.get("source_product") != "OS Open Zoomstack":
        raise MapPlotterError("Zoomstack physical-rail source audit is missing.")
    if zoomstack.audit.get("operator_service_geometry_claimed") is not False:
        raise MapPlotterError("Zoomstack must not claim operator service geometry.")
    national = zoomstack.national_context
    if national is None:
        raise MapPlotterError("Zoomstack national house context was not loaded.")
    if national.source_sha256 != zoomstack.source_sha256:
        raise MapPlotterError(
            "Zoomstack rail and national context source hashes differ."
        )
    if national.audit.get("source_product") != "OS Open Zoomstack":
        raise MapPlotterError("Zoomstack national-context source audit is missing.")
    if national.audit.get("operator_service_geometry_claimed") is not False:
        raise MapPlotterError(
            "Zoomstack national context must not claim operator service."
        )
    geographic_scope = national.audit.get("geographic_scope")
    if geographic_scope != GB_CONTEXT_GEOGRAPHIC_SCOPE:
        raise MapPlotterError(
            "Zoomstack national context does not carry the reviewed Great Britain "
            "territorial scope."
        )
    if national.audit.get("northern_ireland_included") is not False:
        raise MapPlotterError(
            "Zoomstack Great Britain context must explicitly exclude Northern Ireland."
        )
    if national.audit.get("northern_ireland_exclusion_verified") is not True:
        raise MapPlotterError(
            "Zoomstack Great Britain context did not verify the Northern Ireland "
            "exclusion against its selected land components."
        )
    if coastline is None:
        raise MapPlotterError(
            "Named operator maps require a verified authored OSM coastline; "
            "Zoomstack sea-fill coastline inference is forbidden."
        )
    if _SHA256_RE.fullmatch(coastline.source_sha256) is None:
        raise MapPlotterError("Authored OSM coastline source digest is malformed.")
    coastline_audit = coastline.audit
    if coastline_audit.get("policy_version") != OSM_GB_COASTLINE_POLICY_VERSION:
        raise MapPlotterError("Authored OSM coastline policy is missing or changed.")
    if coastline_audit.get("source_sha256") != coastline.source_sha256:
        raise MapPlotterError("Authored OSM coastline audit/source hash differs.")
    coastline_evidence_sha256 = coastline_audit.get("ordered_evidence_sha256")
    coastline_evidence_payload = dict(coastline_audit)
    coastline_evidence_payload.pop("ordered_evidence_sha256", None)
    expected_coastline_evidence_sha256 = hashlib.sha256(
        json.dumps(
            coastline_evidence_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if coastline_evidence_sha256 != expected_coastline_evidence_sha256:
        raise MapPlotterError("Authored OSM coastline audit evidence digest changed.")
    if coastline_audit.get("authored_coastline_geometry_used") is not True:
        raise MapPlotterError("OSM coastline audit does not certify authored geometry.")
    if coastline_audit.get("zoomstack_sea_fill_geometry_used") is not False:
        raise MapPlotterError("OSM coastline audit permits inferred sea-fill geometry.")
    if coastline_audit.get("streaming", {}).get("missing_node_count") != 0:
        raise MapPlotterError("Authored OSM coastline has unresolved source nodes.")
    if coastline_audit.get("topology", {}).get("source_way_parity") is not True:
        raise MapPlotterError("Authored OSM coastline topology lacks source-way parity.")
    if coastline_audit.get("topology", {}).get("reversed_way_count") != 0:
        raise MapPlotterError("Authored OSM coastline reverses source ways.")
    jurisdiction_audit = coastline_audit.get("jurisdiction", {})
    if jurisdiction_audit.get("northern_ireland_included") is not False:
        raise MapPlotterError("Authored coastline must exclude Northern Ireland.")
    if jurisdiction_audit.get("northern_ireland_exclusion_verified") is not True:
        raise MapPlotterError("Authored coastline did not verify Northern Ireland exclusion.")

    if network.kind not in NAMED_OPERATOR_KINDS:
        raise MapPlotterError(
            "Zoomstack national house context requires a named operator network."
        )
    plate = PlateContext.load(network.format_id)
    composition = aspect_aware_map_field(network, plate.field)
    projection_extent = named_operator_projection_extent(
        network_kind=network.kind,
        route_bounds=network.bbox(),
        target_metric_aspect=(
            composition.geographic_viewport.width
            / composition.geographic_viewport.height
        ),
        padding_fraction=padding_fraction,
    )
    bbox = projection_extent.expanded_bounds
    source_west, source_south, source_east, source_north = zoomstack.bounds_wgs84
    if not (
        source_west <= bbox.west
        and source_south <= bbox.south
        and source_east >= bbox.east
        and source_north >= bbox.north
    ):
        raise MapPlotterError(
            "Zoomstack source bounds do not cover the padded operator context box."
        )
    clip = box(bbox.west, bbox.south, bbox.east, bbox.north)
    projector = projector_for(
        network,
        composition.geographic_viewport,
        margin_fraction=composition.projector_margin_fraction,
    )
    source_ref = f"os-open-zoomstack-{zoomstack.source_sha256[:12]}"
    coastline_source_ref = f"osm-gb-authored-coastline-{coastline.source_sha256[:12]}"
    rail_context, rail_audit = _physical_rail_context(
        zoomstack.features,
        clip=clip,
        source_ref=source_ref,
    )
    national_context, national_audit = _national_line_context(
        tuple(line for line in national.lines if line.context_class != "coastline"),
        clip=clip,
        source_ref=source_ref,
        zoom=national.zoom,
        geographic_scope=geographic_scope,
        northern_ireland_included=False,
    )
    authored_coastline_context, authored_coastline_audit = (
        _authored_coastline_context(
            coastline,
            clip=clip,
            clip_bounds=(bbox.west, bbox.south, bbox.east, bbox.north),
            projector=projector,
            source_ref=coastline_source_ref,
        )
    )
    context = tuple(
        sorted(
            (*national_context, *authored_coastline_context, *rail_context),
            key=lambda item: item.id,
        )
    )
    if not context or not rail_context or not national_context:
        raise MapPlotterError(
            "Zoomstack conversion needs both physical rail and national backdrop context."
        )
    geometry_payload = [feature.as_dict() for feature in context]
    context_geometry_sha256 = hashlib.sha256(
        json.dumps(
            geometry_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    audit: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": ZOOMSTACK_TRANSIT_CONTEXT_POLICY_VERSION,
        "release_state": "review-proof-not-operator-evidence",
        "source": {
            "product": "OS Open Zoomstack",
            "sha256": zoomstack.source_sha256,
            "physical_rail_zoom": zoomstack.zoom,
            "national_context_zoom": national.zoom,
            "loaded_bounds_wgs84": list(zoomstack.bounds_wgs84),
        },
        "authored_coastline_source": {
            "product": "OpenStreetMap Great Britain extract",
            "source_ref": coastline_source_ref,
            "sha256": coastline.source_sha256,
            "byte_count": coastline.source_byte_count,
            "source_timestamp": coastline.source_timestamp,
            "source_timestamp_kind": coastline.source_timestamp_kind,
            "loaded_bounds_wgs84": list(coastline.bounds_wgs84),
            "source_geometry_sha256": coastline_audit.get("geometry_sha256"),
            "source_audit_ordered_evidence_sha256": coastline_audit.get(
                "ordered_evidence_sha256"
            ),
        },
        "operator_network_id": network.id,
        "operator_route_geometry_unchanged": True,
        "clip_bounds_wgs84": bbox.as_dict(),
        "padding_fraction": padding_fraction,
        "projection_extent": projection_extent.as_dict(),
        "physical_rail": rail_audit,
        "national_context": {
            **national_audit,
            "candidate_place_count": len(national.places),
            "place_labels_emitted": 0,
            "place_label_policy": "omitted-from-operator-proof",
            "northern_ireland_exclusion_verified": True,
            "zoomstack_inferred_coastline_candidate_count": sum(
                line.context_class == "coastline" for line in national.lines
            ),
            "zoomstack_inferred_coastline_emitted_count": 0,
            "zoomstack_inferred_coastline_production_use_allowed": False,
            "authored_coastline_geometry_used": True,
            "authored_coastline": authored_coastline_audit,
        },
        "emitted_context_feature_count": len(context),
        "context_geometry_sha256": context_geometry_sha256,
        "physical_rail_source_audit_geometry_sha256": zoomstack.audit.get(
            "geometry_sha256"
        ),
        "national_source_audit_geometry_sha256": national.audit.get("geometry_sha256"),
        "operator_inference_from_zoomstack": False,
        "invented_connector_count": 0,
        "context_above_operator_route": False,
        "render_order": "OS context beneath coloured operator route",
    }
    evidence_payload = json.dumps(
        audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    audit["ordered_evidence_sha256"] = hashlib.sha256(evidence_payload).hexdigest()

    zoomstack_source = TransitSource(
        id=source_ref,
        publisher="Ordnance Survey",
        url=ZOOMSTACK_SOURCE_URL,
        licence=ZOOMSTACK_SOURCE_LICENCE,
        attribution=ZOOMSTACK_SOURCE_ATTRIBUTION,
        retrieved_at=retrieved_at,
        sha256=zoomstack.source_sha256,
        use=(
            "Quiet surface-water banks, major roads, national boundaries, and "
            "physical rail infrastructure context only; never operator or "
            "passenger-service evidence; sea-fill polygons are never used as "
            "coastline."
        ),
        commercial_reuse_status="commercial-allowed",
    )
    coastline_source = TransitSource(
        id=coastline_source_ref,
        publisher="OpenStreetMap contributors; extract by Geofabrik",
        url=OSM_GB_COASTLINE_SOURCE_URL,
        licence=OSM_COASTLINE_SOURCE_LICENCE,
        attribution=OSM_COASTLINE_SOURCE_ATTRIBUTION,
        retrieved_at=retrieved_at,
        sha256=coastline.source_sha256,
        use=(
            "Exact authored natural=coastline ways for Great Britain, including "
            "detached islands inside the complete-GB bounds; Northern Ireland, "
            "Ireland, Isle of Man, Channel Islands, and continental land excluded."
        ),
        commercial_reuse_status="commercial-allowed",
    )
    sources = tuple(
        source
        for source in network.sources
        if source.id not in {source_ref, coastline_source_ref}
    ) + (
        zoomstack_source,
        coastline_source,
    )
    attached = replace(
        network,
        sources=sources,
        context=context,
        notes=(
            *network.notes,
            (
                "Pinned OS Open Zoomstack house context is rendered beneath the "
                "operator route; physical rail is infrastructure-only Grey 0.25 mm. "
                "Zoomstack sea-fill polygons are not coastline evidence."
            ),
            (
                "The coastline uses exact authored natural=coastline ways from the "
                "pinned OSM GB PBF, generalised only within 0.04 mm in paper space; "
                "Northern Ireland is explicitly excluded."
            ),
            (
                "Mixed-source house context conversion evidence SHA-256: "
                f"{audit['ordered_evidence_sha256']}."
            ),
        ),
        contract_sha256="",
    )
    validate_transit_network(attached)
    return attached, audit


__all__ = [
    "ZOOMSTACK_TRANSIT_CONTEXT_POLICY_VERSION",
    "attach_zoomstack_house_context",
]
