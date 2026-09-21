from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Any, Iterable

from .models import MapFeature, MapPlotterError


# These are deliberately explicit allow-lists. OpenStreetMap's ``highway`` key
# is also used for platforms, construction works, proposed infrastructure, and
# point/area features which are not live transport lines.  The sets below cover
# every documented linear road and path type from OSM's Map Features highway
# table. Keeping one vocabulary here lets Overpass JSON and local PBF extraction
# apply exactly the same semantic rules.
MAJOR_HIGHWAYS = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
    }
)
SECONDARY_HIGHWAYS = frozenset(
    {"secondary", "secondary_link", "tertiary", "tertiary_link"}
)
LOCAL_HIGHWAYS = frozenset(
    {"residential", "living_street", "unclassified", "service", "road"}
)
OTHER_HIGHWAYS = frozenset(
    {
        "busway",
        "bus_guideway",
        "escape",
        "raceway",
    }
)
PATH_HIGHWAYS = frozenset(
    {
        "path",
        "footway",
        "cycleway",
        "bridleway",
        "steps",
        "pedestrian",
        "track",
        "corridor",
        "elevator",
        "ladder",
        "via_ferrata",
    }
)
SUPPORTED_HIGHWAYS = frozenset(
    MAJOR_HIGHWAYS
    | SECONDARY_HIGHWAYS
    | LOCAL_HIGHWAYS
    | OTHER_HIGHWAYS
    | PATH_HIGHWAYS
    | {"construction"}
)

# These values are intentionally not line-network geometry.  Keeping them
# explicit lets manifests distinguish a known non-route/lifecycle exclusion
# from a genuinely unfamiliar value which needs review. Correctly tagged cycle
# streets use ``bicycle_road=yes`` or ``cyclestreet=yes`` alongside an ordinary
# supported ``highway=*`` value. Correctly tagged escalators use
# ``highway=steps`` (or ``footway`` for a moving walkway) plus ``conveying=*``.
INACTIVE_HIGHWAYS = frozenset(
    {
        "abandoned",
        "demolished",
        "destroyed",
        "dismantled",
        "disused",
        "planned",
        "proposed",
        "razed",
        "removed",
    }
)
NON_ROUTE_HIGHWAYS = frozenset(
    {
        "bus_stop",
        "crossing",
        "cyclist_waiting_aid",
        "emergency_access_point",
        "emergency_bay",
        "give_way",
        "hitchhiking",
        "milestone",
        "mini_roundabout",
        "motorway_junction",
        "passing_place",
        "platform",
        "rest_area",
        "services",
        "speed_camera",
        "speed_display",
        "stop",
        "street_lamp",
        "toll_gantry",
        "traffic_calming",
        "traffic_island",
        "traffic_mirror",
        "traffic_signals",
        "trailhead",
        "turning_circle",
        "turning_loop",
    }
)
PLACEHOLDER_HIGHWAYS = frozenset({"no", "yes"})
KNOWN_EXCLUDED_HIGHWAYS = frozenset(
    INACTIVE_HIGHWAYS | NON_ROUTE_HIGHWAYS | PLACEHOLDER_HIGHWAYS
)

SUPPORTED_RAILWAYS = frozenset(
    {
        "rail",
        "light_rail",
        "tram",
        "subway",
        "narrow_gauge",
        "monorail",
        "funicular",
        "preserved",
        "miniature",
        # Disused rail still has physical tracks in place. It belongs on a
        # detail-faithful plate; abandoned/dismantled/razed rail does not make
        # that same physical claim and remains excluded below.
        "disused",
    }
)

# ``building=*`` normally describes a physical structure, but these values are
# explicit negative/placeholder values rather than building types.  Keep this
# list deliberately narrow: disused, abandoned, and construction buildings can
# still have a real outline that is useful to plot.
NON_BUILDING_VALUES = frozenset({"", "0", "false", "no", "none", "nonexistent"})
HERITAGE_SITE_HISTORIC_VALUES = frozenset({"castle", "palace"})

# A supported railway value can coexist with lifecycle tagging.  Bare markers
# describe the current object, while a prefixed key can describe an older use:
# ``railway=miniature`` plus ``abandoned:railway=rail`` is still a live
# miniature railway.  Suppress a prefixed lifecycle only when it describes the
# same railway value; otherwise faithful extraction would erase valid current
# linework.  Suffixed ``railway:abandoned=yes`` markers describe the current
# railway and remain authoritative.
INACTIVE_RAILWAY_LIFECYCLES = frozenset(
    {
        "abandoned",
        "construction",
        "demolished",
        "destroyed",
        "dismantled",
        "planned",
        "proposed",
        "razed",
        "removed",
    }
)
FALSE_LIFECYCLE_VALUES = frozenset({"", "0", "false", "no", "none"})

SUPPORTED_LINEAR_WATERWAYS = frozenset(
    {"river", "stream", "canal", "drain", "ditch", "tidal_channel"}
)
AREA_LAYERS = frozenset({"water_areas", "green_space", "buildings", "road_areas"})
FALSE_AREA_HIGHWAY_VALUES = frozenset({"", "0", "false", "no", "none"})


def has_landmark_identity(tags: dict[str, str]) -> bool:
    """Return whether tags identify a landmark independently of geometry."""

    return bool(
        any(
            tags.get(key, "").strip()
            for key in ("name", "official_name", "wikidata", "wikipedia")
        )
        or tags.get("heritage", "").strip().casefold()
        not in {"", "0", "false", "no", "none"}
        or any(
            key.casefold().startswith("ref:")
            and value.strip().casefold() not in {"", "0", "false", "no", "none"}
            for key, value in tags.items()
            if "heritage" in key.casefold() or "nhle" in key.casefold()
        )
    )


def is_heritage_site_candidate(tags: dict[str, str]) -> bool:
    """Recognise the narrow non-building castle/palace site vocabulary.

    The special path applies only when ``building=*`` is absent. Explicit
    negative or lifecycle-like building values keep their existing semantics;
    this prevents a broad ``historic=*`` catch-all from entering the building
    footprint layer.
    """

    return (
        not tags.get("building", "").strip()
        and tags.get("historic", "").strip().casefold()
        in HERITAGE_SITE_HISTORIC_VALUES
    )


def is_identified_heritage_site(tags: dict[str, str]) -> bool:
    return is_heritage_site_candidate(tags) and has_landmark_identity(tags)


def _has_inactive_railway_lifecycle(tags: dict[str, str]) -> bool:
    """Return whether tags explicitly mark an otherwise supported rail as inactive."""

    current_value = tags.get("railway", "").strip().casefold()
    for lifecycle in INACTIVE_RAILWAY_LIFECYCLES:
        bare_value = tags.get(lifecycle)
        if (
            bare_value is not None
            and bare_value.strip().casefold() not in FALSE_LIFECYCLE_VALUES
        ):
            return True

        suffix_value = tags.get(f"railway:{lifecycle}")
        if (
            suffix_value is not None
            and suffix_value.strip().casefold() not in FALSE_LIFECYCLE_VALUES
        ):
            return True

        historical_value = tags.get(f"{lifecycle}:railway")
        if historical_value is None:
            continue
        normalized_historical = historical_value.strip().casefold()
        if normalized_historical in FALSE_LIFECYCLE_VALUES:
            continue
        if normalized_historical in {"yes", current_value}:
            return True
    return False


def _normalised_tag(tags: dict[str, str], key: str) -> str:
    return tags.get(key, "").strip().casefold()


def _layer_for_highway_value(highway: str) -> str | None:
    if highway in MAJOR_HIGHWAYS:
        return "roads_major"
    if highway in SECONDARY_HIGHWAYS:
        return "roads_secondary"
    if highway in LOCAL_HIGHWAYS:
        return "roads_local"
    if highway in OTHER_HIGHWAYS:
        return "roads_other"
    if highway in PATH_HIGHWAYS:
        return "paths"
    return None


def effective_highway_value(tags: dict[str, str]) -> str:
    """Return the physical road/path type, resolving construction lifecycle tags."""

    highway = _normalised_tag(tags, "highway")
    if highway != "construction":
        return highway
    target = _normalised_tag(tags, "construction")
    return target if _layer_for_highway_value(target) is not None else "road"


def _classify_highway(tags: dict[str, str]) -> str | None:
    highway = _normalised_tag(tags, "highway")
    if highway == "construction":
        # Active construction has verifiable geometry on the ground. Retain it
        # even when ``construction=*`` is absent or unfamiliar, placing that
        # conservative fallback in roads_other rather than silently erasing it.
        return _layer_for_highway_value(_normalised_tag(tags, "construction")) or (
            "roads_other"
        )
    documented_layer = _layer_for_highway_value(highway)
    if documented_layer is not None:
        return documented_layer
    # The broad Overpass/PBF acquisition deliberately sees local and newly
    # introduced values. Once explicit lifecycle, placeholder, and non-route
    # values are excluded, a still-unknown highway way is safer to retain as a
    # low-priority road than to erase. Coverage diagnostics keep the value
    # visible for later semantic review.
    if highway and highway not in KNOWN_EXCLUDED_HIGHWAYS:
        return "roads_other"
    return None


def highway_coverage_from_tag_counts(
    tag_counts: dict[tuple[str, str], int] | Counter[tuple[str, str]],
    area_highway_counts: dict[str, int] | Counter[str] | None = None,
) -> dict[str, Any]:
    """Summarize classified, lifecycle-excluded, and unknown highway ways."""

    by_value: Counter[str] = Counter()
    by_layer: Counter[str] = Counter()
    construction_by_target: Counter[str] = Counter()
    known_excluded: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    classified_count = 0
    construction_count = 0
    for (highway, construction), count in tag_counts.items():
        if count <= 0:
            continue
        normalized_tags = {"highway": highway}
        if construction:
            normalized_tags["construction"] = construction
        by_value[highway] += count
        layer = _classify_highway(normalized_tags)
        is_unknown = (
            highway not in SUPPORTED_HIGHWAYS and highway not in KNOWN_EXCLUDED_HIGHWAYS
        )
        if layer is not None:
            classified_count += count
            by_layer[layer] += count
            if is_unknown:
                unknown[highway] += count
            if highway == "construction":
                construction_count += count
                construction_by_target[construction or "<missing>"] += count
        elif highway in KNOWN_EXCLUDED_HIGHWAYS:
            known_excluded[highway] += count
        elif is_unknown:
            unknown[highway] += count

    source_count = sum(by_value.values())
    area_counts = Counter(area_highway_counts or {})
    area_counts = Counter(
        {
            value: count
            for value, count in area_counts.items()
            if value not in FALSE_AREA_HIGHWAY_VALUES and count > 0
        }
    )
    return {
        "source_highway_way_count": source_count,
        "classified_highway_way_count": classified_count,
        "excluded_highway_way_count": source_count - classified_count,
        "under_construction_way_count": construction_count,
        "by_value": dict(sorted(by_value.items())),
        "classified_by_layer": dict(sorted(by_layer.items())),
        "construction_by_target": dict(sorted(construction_by_target.items())),
        "known_excluded_by_value": dict(sorted(known_excluded.items())),
        "unknown_highway_way_count": sum(unknown.values()),
        "unknown_by_value": dict(sorted(unknown.items())),
        "unknown_values_require_review": sorted(unknown),
        "area_highway_object_count": sum(area_counts.values()),
        "area_highway_by_value": dict(sorted(area_counts.items())),
        "policy": {
            "area_highways": "perimeter retained",
            "area:highway": (
                "non-routable micromapped perimeter retained separately from "
                "highway centrelines"
            ),
            "construction": "retained and classified by construction=* target",
            "proposed_or_inactive": "excluded",
            "known_non_route_objects": "excluded",
            "unknown_values": (
                "retained in roads_other and reported for semantic review"
            ),
        },
    }


def highway_coverage(data: dict[str, Any]) -> dict[str, Any]:
    """Audit unique Overpass highway ways using the canonical classifier."""

    counts: Counter[tuple[str, str]] = Counter()
    area_counts: Counter[str] = Counter()
    seen_highways: set[tuple[str, str]] = set()
    seen_area_highways: set[tuple[str, str]] = set()
    elements = data.get("elements")
    if not isinstance(elements, list):
        return highway_coverage_from_tag_counts(counts)
    for element in elements:
        if not isinstance(element, dict) or element.get("type") not in {
            "way",
            "relation",
        }:
            continue
        element_id = str(element.get("id", "unknown"))
        osm_type = str(element.get("type"))
        identity = (osm_type, element_id)
        tags_value = element.get("tags")
        if not isinstance(tags_value, dict):
            continue
        tags = {str(key): str(value) for key, value in tags_value.items()}
        highway = _normalised_tag(tags, "highway")
        if osm_type == "way" and highway and identity not in seen_highways:
            seen_highways.add(identity)
            counts[(highway, _normalised_tag(tags, "construction"))] += 1
        area_highway = _normalised_tag(tags, "area:highway")
        if (
            area_highway not in FALSE_AREA_HIGHWAY_VALUES
            and identity not in seen_area_highways
        ):
            seen_area_highways.add(identity)
            area_counts[area_highway] += 1
    return highway_coverage_from_tag_counts(counts, area_counts)


def _classify_supported(tags: dict[str, str]) -> str | None:
    """Classify drawable linework without catch-all infrastructure values.

    This returns the primary semantic layer. A supported ``highway=*`` wins
    before railway and water tags so a road over a dam remains in the road
    hierarchy. Extraction separately preserves a co-tagged physical railway as
    a second layer via :func:`_classify_supported_layers`.
    Unsupported highway/railway/waterway values may still fall through to a
    separate valid area classification, such as a building or park.
    """

    highway_layer = _classify_highway(tags)
    if highway_layer is not None:
        return highway_layer
    if _normalised_tag(tags, "area:highway") not in FALSE_AREA_HIGHWAY_VALUES:
        return "road_areas"

    if (
        tags.get("natural") in {"water", "bay", "wetland"}
        or tags.get("waterway") == "riverbank"
        or tags.get("landuse") in {"reservoir", "basin"}
    ):
        return "water_areas"
    if (
        tags.get("waterway") in SUPPORTED_LINEAR_WATERWAYS
        or tags.get("natural") == "coastline"
        or tags.get("man_made") in {"pier", "breakwater"}
    ):
        return "waterways"
    if tags.get("leisure") in {"park", "nature_reserve", "golf_course"}:
        return "green_space"
    if tags.get("landuse") in {
        "forest",
        "grass",
        "recreation_ground",
        "cemetery",
        "meadow",
        "village_green",
        "allotments",
        "orchard",
    }:
        return "green_space"
    if tags.get("natural") in {"wood", "scrub", "heath"}:
        return "green_space"
    railway_value = tags.get("railway") or tags.get("disused:railway")
    if railway_value in SUPPORTED_RAILWAYS and not _has_inactive_railway_lifecycle(
        tags
    ):
        return "railways"
    if tags.get("boundary") == "administrative":
        return "boundaries"
    if tags.get("leisure") == "stadium":
        return "buildings"
    building = tags.get("building")
    if building is not None and building.strip().casefold() not in NON_BUILDING_VALUES:
        return "buildings"
    if is_identified_heritage_site(tags):
        return "buildings"
    return None


def _classify_supported_layers(tags: dict[str, str]) -> tuple[str, ...]:
    """Return every independently drawable semantic layer for one object.

    Most OSM objects have one drawable meaning, but street-running tram and
    rail alignments are legitimately co-tagged with ``highway=*``. Treating
    classification as a single-choice precedence chain silently erased their
    railway colour and source lineage. Keep the established primary layer and
    add the physical railway overlay when its current/lifecycle tags are
    supported. The tuple is stable and duplicate-free.
    """

    primary = _classify_supported(tags)
    layers = [primary] if primary is not None else []
    railway_value = tags.get("railway") or tags.get("disused:railway")
    if (
        railway_value in SUPPORTED_RAILWAYS
        and not _has_inactive_railway_lifecycle(tags)
        and "railways" not in layers
    ):
        layers.append("railways")
    return tuple(layers)


def classify_supported_layer(tags: dict[str, str]) -> str | None:
    """Return the canonical drawable layer selected by extraction.

    Geometry-integrity auditing deliberately shares this *selection* decision
    with extraction.  The audit remains an independent check of the raw
    coordinate representation and of raw-to-canonical geometry preservation;
    duplicating the tag vocabulary there would instead risk auditing a
    different set of objects from the set the renderer selected.
    """

    return _classify_supported(tags)


def _geometry_parts(geometry: Any) -> Iterable[list[tuple[float, float]]]:
    if not isinstance(geometry, list):
        return
    current: list[tuple[float, float]] = []
    for point in geometry:
        if isinstance(point, dict) and "lat" in point and "lon" in point:
            try:
                coordinate = (float(point["lat"]), float(point["lon"]))
                if all(isfinite(value) for value in coordinate):
                    current.append(coordinate)
                    continue
            except (TypeError, ValueError):
                pass
        if len(current) >= 2:
            yield current
        current = []
    if len(current) >= 2:
        yield current


def _endpoint(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], 7), round(point[1], 7))


def _assemble_member_parts(
    parts: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Chain relation-member fragments by matching endpoints in any direction."""

    unused = [list(part) for part in parts if len(part) >= 2]
    assembled: list[list[tuple[float, float]]] = []
    while unused:
        current = unused.pop(0)
        changed = True
        while changed and _endpoint(current[0]) != _endpoint(current[-1]):
            changed = False
            for index, candidate in enumerate(unused):
                current_start = _endpoint(current[0])
                current_end = _endpoint(current[-1])
                candidate_start = _endpoint(candidate[0])
                candidate_end = _endpoint(candidate[-1])
                if current_end == candidate_start:
                    current.extend(candidate[1:])
                elif current_end == candidate_end:
                    current.extend(reversed(candidate[:-1]))
                elif current_start == candidate_end:
                    current = candidate[:-1] + current
                elif current_start == candidate_start:
                    current = list(reversed(candidate[1:])) + current
                else:
                    continue
                unused.pop(index)
                changed = True
                break
        assembled.append(current)
    return assembled


def _compose_area_role(parent_role: str, child_role: str) -> str:
    """Compose nested multipolygon roles without flattening away holes.

    A child outer inside a parent inner is a hole, while a child inner inside
    that parent inner becomes an island again.  Treating nested members as
    unconditionally outer would therefore change the represented area.
    """

    return "inner" if (parent_role == "inner") != (child_role == "inner") else "outer"


def _collect_relation_member_geometry(
    relation: dict[str, Any],
    *,
    layer: str,
    relation_index: dict[str, dict[str, Any]],
    ancestry: frozenset[str],
    inherited_area_role: str,
    traversal: tuple[str, ...],
    area_parts: dict[str, list[list[tuple[float, float]]]],
    linear_records: list[tuple[str, list[tuple[float, float]], str | None, str | None]],
) -> None:
    """Resolve inline way geometry, including referenced nested relations.

    Overpass ``out geom`` puts geometry directly on way members but represents
    a relation member only by its reference.  The referenced relation is also
    present in the response's elements list.  Resolution is deliberately
    bounded by ``ancestry``: malformed missing references and cycles produce no
    invented geometry here and are rejected by the independent integrity audit.
    """

    members = relation.get("members")
    if not isinstance(members, list):
        return
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            continue
        member_type = member.get("type")
        raw_role = str(member.get("role", ""))
        role = raw_role or ("outer" if layer in AREA_LAYERS else "member")
        member_ref = str(member.get("ref", "unknown"))
        member_traversal = (*traversal, str(index))

        if member_type == "way":
            parts = list(_geometry_parts(member.get("geometry")))
            if layer in AREA_LAYERS and raw_role in {"", "outer", "inner"}:
                effective_role = _compose_area_role(
                    inherited_area_role,
                    "inner" if role == "inner" else "outer",
                )
                area_parts[effective_role].extend(parts)
            else:
                path = "/".join(member_traversal)
                linear_records.extend(
                    (
                        f"{role}:{path}:way-{member_ref}:{part_index}",
                        points,
                        role,
                        None,
                    )
                    for part_index, points in enumerate(parts)
                )
            continue

        if member_type != "relation" or raw_role not in {"", "outer", "inner"}:
            continue
        nested = relation_index.get(member_ref)
        if nested is None or member_ref in ancestry:
            continue
        nested_role = inherited_area_role
        if layer in AREA_LAYERS:
            nested_role = _compose_area_role(
                inherited_area_role,
                "inner" if role == "inner" else "outer",
            )
        _collect_relation_member_geometry(
            nested,
            layer=layer,
            relation_index=relation_index,
            ancestry=ancestry | {member_ref},
            inherited_area_role=nested_role,
            traversal=(*traversal, f"{index}:relation-{member_ref}"),
            area_parts=area_parts,
            linear_records=linear_records,
        )


def _ring_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 4 or _endpoint(points[0]) != _endpoint(points[-1]):
        return 0.0
    # Longitude is x and latitude is y. Only relative area is needed to choose
    # the smallest containing outer ring, so a local planar shoelace is enough.
    return abs(
        sum(
            left[1] * right[0] - right[1] * left[0]
            for left, right in zip(points, points[1:])
        )
        / 2
    )


def _ring_contains(ring: list[tuple[float, float]], point: tuple[float, float]) -> bool:
    """Return whether a latitude/longitude point lies inside a closed ring."""

    if len(ring) < 4 or _endpoint(ring[0]) != _endpoint(ring[-1]):
        return False
    latitude, longitude = point
    inside = False
    for left, right in zip(ring, ring[1:]):
        left_lat, left_lon = left
        right_lat, right_lon = right
        if (left_lat > latitude) == (right_lat > latitude):
            continue
        crossing_lon = left_lon + (latitude - left_lat) * (right_lon - left_lon) / (
            right_lat - left_lat
        )
        if longitude < crossing_lon:
            inside = not inside
    return inside


def _optional_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def extract_features(
    data: dict[str, Any], enabled_layers: set[str]
) -> list[MapFeature]:
    elements = data.get("elements")
    if not isinstance(elements, list):
        raise MapPlotterError("Overpass data has no elements list.")

    # ``out geom`` does not inline a relation member's geometry on its parent;
    # it returns the referenced relation as another top-level element.  Index
    # those objects before extraction so forward references resolve as well as
    # backward references.  The integrity audit rejects missing, ambiguous, or
    # cyclic references; extraction never fabricates geometry for them.
    relation_index: dict[str, dict[str, Any]] = {}
    for value in elements:
        if not isinstance(value, dict) or value.get("type") != "relation":
            continue
        relation_id = value.get("id")
        if relation_id is None:
            continue
        relation_index.setdefault(str(relation_id), value)

    features: list[MapFeature] = []
    seen: set[tuple[str, str, str, str, tuple[tuple[float, float], ...]]] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags_value = element.get("tags", {})
        tags = (
            {str(key): str(value) for key, value in tags_value.items()}
            if isinstance(tags_value, dict)
            else {}
        )
        selected_layers = tuple(
            layer
            for layer in _classify_supported_layers(tags)
            if layer in enabled_layers
        )
        if not selected_layers:
            continue
        layer = selected_layers[0]

        osm_type = str(element.get("type", "unknown"))
        element_id = str(element.get("id", "unknown"))
        heritage_site = is_identified_heritage_site(tags)
        if (
            heritage_site
            and osm_type == "relation"
            and tags.get("type", "").strip().casefold() != "multipolygon"
        ):
            continue
        geometry_records: list[
            tuple[
                str,
                list[tuple[float, float]],
                str | None,
                str | None,
            ]
        ] = []
        relation_members: tuple[tuple[str, str, str], ...] = ()
        if osm_type == "way":
            for part_index, points in enumerate(
                _geometry_parts(element.get("geometry"))
            ):
                geometry_records.append((f"way:{part_index}", points, None, None))
        elif osm_type == "relation":
            members = element.get("members", [])
            if isinstance(members, list):
                relation_members = tuple(
                    (
                        str(member.get("type", "unknown")),
                        str(member.get("ref", "unknown")),
                        str(member.get("role", "")),
                    )
                    for member in members
                    if isinstance(member, dict)
                )
                area_parts: dict[str, list[list[tuple[float, float]]]] = {
                    "outer": [],
                    "inner": [],
                }
                _collect_relation_member_geometry(
                    element,
                    layer=layer,
                    relation_index=relation_index,
                    ancestry=frozenset({element_id}),
                    inherited_area_role="outer",
                    traversal=(),
                    area_parts=area_parts,
                    linear_records=geometry_records,
                )
                if layer in AREA_LAYERS:
                    outer_rings = _assemble_member_parts(area_parts["outer"])
                    outer_records = [
                        (f"outer:ring-{index}", points)
                        for index, points in enumerate(outer_rings)
                    ]
                    geometry_records.extend(
                        (part, points, "outer", None) for part, points in outer_records
                    )
                    for inner_index, points in enumerate(
                        _assemble_member_parts(area_parts["inner"])
                    ):
                        containers = [
                            (_ring_area(outer), part)
                            for part, outer in outer_records
                            if _ring_contains(outer, points[0])
                        ]
                        outer_ring_part = min(containers)[1] if containers else None
                        geometry_records.append(
                            (
                                f"inner:ring-{inner_index}",
                                points,
                                "inner",
                                outer_ring_part,
                            )
                        )

        if heritage_site and osm_type == "relation" and not any(
            area_role == "outer" and _ring_area(points) > 0
            for _part, points, area_role, _outer_ring_part in geometry_records
        ):
            continue

        for source_id, points, area_role, outer_ring_part in geometry_records:
            rounded = tuple((round(lat, 7), round(lon, 7)) for lat, lon in points)
            reverse = tuple(reversed(rounded))
            for feature_layer in selected_layers:
                signature = (
                    feature_layer,
                    osm_type,
                    element_id,
                    area_role or "",
                    min(rounded, reverse),
                )
                if signature in seen:
                    continue
                seen.add(signature)
                feature_tags = dict(tags)
                if area_role in {"outer", "inner"}:
                    feature_tags["mapplot:area-role"] = area_role
                inferred_ring_role = area_role
                if (
                    inferred_ring_role is None
                    and feature_layer in AREA_LAYERS
                    and len(points) >= 4
                    and _endpoint(points[0]) == _endpoint(points[-1])
                ):
                    inferred_ring_role = "outer"
                    feature_tags["mapplot:area-role"] = "outer"
                if (
                    heritage_site
                    and (
                        inferred_ring_role not in {"outer", "inner"}
                        or _ring_area(points) <= 0
                    )
                ):
                    continue
                features.append(
                    MapFeature(
                        layer=feature_layer,
                        points=points,
                        osm_type=osm_type,
                        osm_id=element_id,
                        part=source_id,
                        tags=feature_tags,
                        geometry_type=(
                            "polygon_ring"
                            if inferred_ring_role in {"outer", "inner"}
                            and len(points) >= 4
                            and _endpoint(points[0]) == _endpoint(points[-1])
                            else "area_boundary_fragment"
                            if inferred_ring_role in {"outer", "inner"}
                            else "line"
                        ),
                        ring_role=inferred_ring_role,
                        outer_ring_part=outer_ring_part,
                        node_refs=(
                            tuple(str(ref) for ref in element.get("nodes", []))
                            if osm_type == "way"
                            and isinstance(element.get("nodes"), list)
                            and len(element["nodes"]) == len(points)
                            else ()
                        ),
                        relation_members=relation_members,
                        osm_version=_optional_nonnegative_int(element.get("version")),
                        osm_timestamp=(
                            str(element["timestamp"])
                            if element.get("timestamp") is not None
                            else None
                        ),
                        osm_changeset=_optional_nonnegative_int(
                            element.get("changeset")
                        ),
                        osm_uid=_optional_nonnegative_int(element.get("uid")),
                        osm_user=(
                            str(element["user"])
                            if element.get("user") is not None
                            else None
                        ),
                    )
                )
    return features
