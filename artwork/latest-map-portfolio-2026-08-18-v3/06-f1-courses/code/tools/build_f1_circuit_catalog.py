#!/usr/bin/env python3
"""Build the frozen 2026 F1 circuit catalog without network access.

The builder verifies every snapshot hash, transcribes a small factual field set
from the official race-page HTML, and assembles each OSM lap only through exact
shared endpoints.  It never traces official circuit-map artwork and never adds
a connector to close a source gap.  Unclosed, ambiguous, length-mismatched, or
policy-pending records remain explicit holds in the packaged catalog.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence

from shapely.geometry import LineString, Point, Polygon, shape as shapely_shape
from shapely.geometry.polygon import orient
from shapely.ops import polygonize_full

from city_map_plotter.f1_circuits import validate_f1_catalog


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_ROOT = ROOT / "contracts" / "f1-circuits-2026"
REGISTRY_PATH = CONTRACT_ROOT / "event-registry.json"
MANIFEST_PATH = CONTRACT_ROOT / "source-manifest.json"
OUTPUT_PATH = ROOT / "src" / "city_map_plotter" / "data" / "f1-circuits-2026.json"

CATALOG_ID = "f1-circuits-2026"
SCHEMA_VERSION = 1
EARTH_RADIUS_M = 6_371_008.8
LENGTH_REVIEW_THRESHOLD_PERCENT = 1.0
ENDPOINT_EPSILON_DEGREES = 1e-9
LOCAL_COORDINATE_DECIMALS = 3
CURRENT_OSM_GRANDSTAND_STATUS = (
    "frozen-current-osm-footprint-not-event-configuration-verified"
)
CURRENT_OSM_GRANDSTAND_TEMPORALITY = "snapshot-current-at-catalog-freeze"
CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE = (
    "current-osm-grandstand-footprint-only-not-event-or-fia-configuration"
)

CONTEXT_CATALOG_CAPS = {
    "water": 144,
    "grass": 126,
    "woodland": 126,
    "grandstand": 108,
    "principal-building": 108,
    "building": 300,
    "road": 420,
    "access-road": 270,
    "runoff": 180,
    "gravel-trap": 126,
    "paddock": 72,
    "garage": 180,
    "pit-building": 72,
}
CONTEXT_ROAD_RANK = {
    "motorway": 0,
    "trunk": 0,
    "primary": 1,
    "secondary": 2,
    "tertiary": 3,
    "unclassified": 4,
    "residential": 4,
    "service": 5,
    "track": 5,
    "path": 6,
    "footway": 6,
}

HASH_KEYS = frozenset({"geometry_sha256", "source_geometry_sha256"})
PIT_WORDS = frozenset(
    {
        "pit",
        "pits",
        "pitlane",
        "pit_lane",
        "pit-lane",
        "boxenstrasse",
        "pitstraat",
    }
)
EXCLUDED_LAP_ROLES = frozenset(
    {
        "alternate",
        "alternative",
        "escape",
        "service",
        "shortcut",
        "access",
    }
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False
        self._title_complete = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "title" and not self._title_complete:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            if self._in_title:
                self._title_complete = True
            self._in_title = False

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)
            if self._in_title:
                self.title_parts.append(value)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _portable_source_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_uri()


def _canonical_hash_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical_hash_payload(child)
            for key, child in value.items()
            if key not in HASH_KEYS
        }
    if isinstance(value, list):
        return [_canonical_hash_payload(child) for child in value]
    return value


def _geometry_sha256(model: dict[str, Any]) -> str:
    payload = json.dumps(
        _canonical_hash_payload(model),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(payload)


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _verify_manifest_source(source: dict[str, Any]) -> bytes:
    path_value = source.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"Source {source.get('id')} has no snapshot path")
    path = ROOT / path_value
    compressed = path.read_bytes()
    if _sha256(compressed) != source.get("compressed_sha256"):
        raise ValueError(f"Compressed hash mismatch for {source.get('id')}")
    payload = gzip.decompress(compressed)
    if _sha256(payload) != source.get("payload_sha256"):
        raise ValueError(f"Payload hash mismatch for {source.get('id')}")
    if len(payload) != source.get("payload_bytes"):
        raise ValueError(f"Payload byte-count mismatch for {source.get('id')}")
    for component in source.get("component_snapshots", []):
        if not isinstance(component, dict) or not component.get("path"):
            raise ValueError(f"Invalid component snapshot for {source.get('id')}")
        component_path = ROOT / str(component["path"])
        component_compressed = component_path.read_bytes()
        if _sha256(component_compressed) != component.get("compressed_sha256"):
            raise ValueError(
                f"Component compressed hash mismatch for {source.get('id')}"
            )
        component_payload = gzip.decompress(component_compressed)
        if _sha256(component_payload) != component.get("payload_sha256"):
            raise ValueError(f"Component payload hash mismatch for {source.get('id')}")
    return payload


def _parse_number(text: str, label: str) -> float | None:
    pattern = rf"{re.escape(label)}\s*([0-9]+(?:\.[0-9]+)?)\s*km"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _parse_track_stats(payload: bytes) -> dict[str, Any] | None:
    """Return one internally consistent F1 ``trackStats`` record.

    Formula 1 serialises this record inside escaped Next.js payloads.  It is a
    factual text record, not the accompanying protected circuit illustration.
    Duplicate page fragments are accepted only when their selected fields are
    identical; conflicting records fail closed.
    """

    text = payload.decode("utf-8", errors="replace").replace(r"\"", '"')
    records: list[dict[str, Any]] = []
    for match in re.finditer(r'"trackStats"\s*:\s*(\{[^{}]*\})', text):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    if not records:
        return None

    field_map = {
        "circuitShortName": "circuit_short_name",
        "circuitOfficialName": "circuit_official_name",
        "circuitLocation": "circuit_location",
        "circuitType": "circuit_type",
        "trackLength": "track_length_raw",
        "startLineOffset": "start_line_offset_raw",
        "controlLineLocation": "control_line_location_raw",
        "direction": "direction_raw",
        "startSeason": "configuration_start_season_raw",
        "endSeason": "configuration_end_season_raw",
        "venueFirstSeason": "venue_first_season_raw",
        "fastestLapTime": "fastest_lap_time_raw",
        "fastestLapSeason": "fastest_lap_season_raw",
        "fastestLapDriver": "fastest_lap_driver_raw",
        "fastestLapTeam": "fastest_lap_team_raw",
        "fastestLapDriverKey": "fastest_lap_driver_key",
        "circuitConfigurationKey": "circuit_configuration_key",
    }
    selected = [
        {
            output_key: record.get(source_key)
            for source_key, output_key in field_map.items()
        }
        for record in records
    ]
    canonical = {
        json.dumps(value, sort_keys=True, separators=(",", ":")) for value in selected
    }
    if len(canonical) != 1:
        return None
    result = selected[0]
    result["source_field_scope"] = "formula1-race-page-trackStats"
    result["start_finish_anchor_status"] = (
        "relative-metadata-only-not-coordinate-bearing"
    )
    return result


def _lap_time_ms(value: str) -> int | None:
    match = re.fullmatch(r"([0-9]+):([0-5][0-9])\.([0-9]{3})", value.strip())
    if match is None:
        return None
    minutes, seconds, milliseconds = map(int, match.groups())
    result = (minutes * 60 + seconds) * 1000 + milliseconds
    return result if result > 0 else None


def _parse_visible_fastest_lap(text: str) -> dict[str, Any] | None:
    matches = {
        (
            match.group("time").strip(),
            " ".join(match.group("driver").split()),
            int(match.group("season")),
        )
        for match in re.finditer(
            r"Fastest lap time\s*"
            r"(?P<time>[0-9]+:[0-5][0-9]\.[0-9]{3})\s*"
            r"(?P<driver>[^0-9()]{2,100}?)\s*"
            r"\((?P<season>[0-9]{4})\)",
            text,
            flags=re.IGNORECASE,
        )
    }
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Official race page contains conflicting fastest-lap copy")
    time_copy, driver, season = next(iter(matches))
    return {"time": time_copy, "driver": driver, "season": season}


def _fastest_lap_fact(
    text: str,
    track_stats: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind visible and structured official F1 fastest-lap facts fail-closed."""

    visible = _parse_visible_fastest_lap(text)
    raw_time = str((track_stats or {}).get("fastest_lap_time_raw") or "").strip()
    raw_driver = str((track_stats or {}).get("fastest_lap_driver_raw") or "").strip()
    raw_season = str((track_stats or {}).get("fastest_lap_season_raw") or "").strip()
    structured_available = bool(
        _lap_time_ms(raw_time)
        and raw_driver
        and raw_season.isdigit()
        and 1950 <= int(raw_season) <= 2026
    )
    if visible is None and not structured_available:
        return {
            "status": "withheld",
            "source_label": "Fastest lap time",
            "withheld_reason": "official-source-placeholder-no-fastest-lap-yet",
            "claim_scope": "formula1-official-page-fastest-lap-time",
        }
    if visible is None or not structured_available:
        raise ValueError(
            "Official race page fastest-lap visible and structured fields are partial"
        )
    structured = {
        "time": raw_time,
        "driver": raw_driver,
        "season": int(raw_season),
    }
    if visible != structured:
        raise ValueError(
            "Official race page fastest-lap visible and structured fields disagree"
        )
    return {
        "status": "source-backed",
        **structured,
        "time_ms": _lap_time_ms(raw_time),
        "source_label": "Fastest lap time",
        "claim_scope": "formula1-official-page-fastest-lap-time",
        **(
            {"team_source_copy": str(track_stats["fastest_lap_team_raw"])}
            if track_stats and track_stats.get("fastest_lap_team_raw")
            else {}
        ),
        **(
            {"driver_key": str(track_stats["fastest_lap_driver_key"])}
            if track_stats and track_stats.get("fastest_lap_driver_key")
            else {}
        ),
    }


def _normalise_track_direction(track_stats: dict[str, Any] | None) -> str | None:
    if track_stats is None:
        return None
    raw = str(track_stats.get("direction_raw") or "").strip().casefold()
    if raw == "clockwise":
        return "clockwise"
    if raw in {"anti-clockwise", "counter-clockwise"}:
        return "counter-clockwise"
    return None


def _parse_race_page(payload: bytes) -> dict[str, Any]:
    parser = _TextExtractor()
    parser.feed(payload.decode("utf-8", errors="replace"))
    text = html.unescape(" ".join(parser.parts))
    first_gp_match = re.search(
        r"First Grand Prix\s*([0-9]{4}|N/?A)", text, flags=re.IGNORECASE
    )
    laps_match = re.search(r"Number of Laps\s*([0-9]+)", text, flags=re.IGNORECASE)
    circuit_length_km = _parse_number(text, "Circuit Length")
    race_distance_km = _parse_number(text, "Race Distance")
    track_stats = _parse_track_stats(payload)
    first_grand_prix = (
        int(first_gp_match.group(1))
        if first_gp_match and first_gp_match.group(1).isdigit()
        else None
    )
    structured_first_season = str(
        (track_stats or {}).get("venue_first_season_raw") or ""
    ).strip()
    if (
        first_grand_prix is not None
        and structured_first_season.isdigit()
        and first_grand_prix != int(structured_first_season)
    ):
        raise ValueError(
            "Official race page First Grand Prix and venueFirstSeason disagree"
        )
    return {
        "page_title": " ".join(parser.title_parts) or None,
        "official_circuit_length_m": (
            round(circuit_length_km * 1000.0, 3)
            if circuit_length_km is not None
            else None
        ),
        "number_of_laps": int(laps_match.group(1)) if laps_match else None,
        "race_distance_km": race_distance_km,
        "first_grand_prix": first_grand_prix,
        "fastest_lap": _fastest_lap_fact(text, track_stats),
        "official_track_stats": track_stats,
    }


def _coordinate_key(coordinate: Sequence[float]) -> tuple[int, int]:
    return (
        round(float(coordinate[0]) / ENDPOINT_EPSILON_DEGREES),
        round(float(coordinate[1]) / ENDPOINT_EPSILON_DEGREES),
    )


def _way_coordinates(element: dict[str, Any]) -> list[list[float]]:
    result: list[list[float]] = []
    for value in element.get("geometry", []):
        if not isinstance(value, dict):
            continue
        lat = value.get("lat")
        lon = value.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            result.append([float(lon), float(lat)])
    return result


def _element_index(
    snapshot: dict[str, Any],
) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for element in snapshot.get("elements", []):
        if not isinstance(element, dict):
            continue
        element_type = element.get("type")
        element_id = element.get("id")
        if isinstance(element_type, str) and isinstance(element_id, int):
            result[(element_type, element_id)] = element
    return result


def _source_object(element: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": str(element.get("type")),
        "id": int(element.get("id")),
    }
    for key in ("version", "timestamp"):
        if element.get(key) is not None:
            result[key] = element[key]
    tags = element.get("tags")
    if isinstance(tags, dict):
        result["tags"] = {str(key): str(value) for key, value in sorted(tags.items())}
    return result


def _member_source_objects(
    element: dict[str, Any],
    index: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    objects = [_source_object(element)]
    if element.get("type") != "relation":
        return objects
    seen = {(objects[0]["type"], objects[0]["id"])}
    for member in element.get("members", []):
        if not isinstance(member, dict):
            continue
        member_type = member.get("type")
        member_id = member.get("ref")
        if not isinstance(member_type, str) or not isinstance(member_id, int):
            continue
        key = (member_type, member_id)
        if key in seen:
            continue
        seen.add(key)
        source = index.get(key)
        if source is not None:
            objects.append(_source_object(source))
        else:
            objects.append({"type": member_type, "id": member_id})
    return objects


def _normalised_words(value: object) -> set[str]:
    return {part for part in re.split(r"[^a-z0-9]+", str(value).casefold()) if part}


def _normalised_phrase(value: object) -> str:
    return " ".join(
        part for part in re.split(r"[^a-z0-9]+", str(value).casefold()) if part
    )


def _is_explicit_pit_role(role: str) -> bool:
    role_words = _normalised_words(role)
    return bool(role_words & PIT_WORDS or "pit" in role.casefold())


def _is_pit_way(role: str, element: dict[str, Any]) -> bool:
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    if _is_explicit_pit_role(role):
        return True
    explicit_words: set[str] = set()
    for key in ("raceway", "motor_racing", "service", "ref"):
        explicit_words.update(_normalised_words(tags.get(key, "")))
    if explicit_words & PIT_WORDS:
        return True

    # Names are supporting evidence only on a mapped drivable way.  Requiring
    # a pit-lane phrase (or a language-specific equivalent) avoids treating
    # Silverstone's source lap segment "National Pit Straight" or a building
    # such as "Pit Stop Cafe" as pit-lane geometry.
    highway = str(tags.get("highway", "")).casefold()
    if not highway:
        return False
    for name_key in ("name", "name:en", "name:fr"):
        name = tags.get(name_key, "")
        name_words = _normalised_words(name)
        if "straight" in name_words:
            continue
        if (
            {"pit", "lane"} <= name_words
            or name_words & {"pitlane", "boxenstrasse", "pitstraat"}
            or _normalised_phrase(name) in {"voie des stands", "sortie des stands"}
            or ("pit" in name_words and highway == "raceway")
        ):
            return True
    return False


def _excluded_lap_member(role: str, element: dict[str, Any]) -> bool:
    if _is_pit_way(role, element):
        return True
    role_words = _normalised_words(role)
    if role_words & EXCLUDED_LAP_ROLES:
        return True
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    if str(tags.get("area", "")).casefold() == "yes":
        return True
    if tags.get("area:highway"):
        return True
    return False


def _selection_ways(
    event: dict[str, Any],
    index: dict[tuple[str, int], dict[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], str]],
    list[dict[str, Any]],
    dict[str, Any] | None,
    list[str],
]:
    selection = event["osm_selection"]
    lap: list[tuple[dict[str, Any], str]] = []
    pits: list[dict[str, Any]] = []
    relation_role_pits: list[dict[str, Any]] = []
    relation_other_pits: list[dict[str, Any]] = []
    findings: list[str] = []
    relation: dict[str, Any] | None = None
    if selection["mode"] == "relation":
        relation_id = int(selection["relation_id"])
        relation = index.get(("relation", relation_id))
        if relation is None:
            return [], [], None, [f"selected relation/{relation_id} is absent"]
        for member in relation.get("members", []):
            if not isinstance(member, dict) or member.get("type") != "way":
                continue
            member_id = member.get("ref")
            if not isinstance(member_id, int):
                continue
            way = index.get(("way", member_id))
            if way is None or len(_way_coordinates(way)) < 2:
                findings.append(f"relation member way/{member_id} has no geometry")
                continue
            role = str(member.get("role") or "")
            if _is_pit_way(role, way):
                if _is_explicit_pit_role(role):
                    relation_role_pits.append(way)
                else:
                    relation_other_pits.append(way)
            elif _excluded_lap_member(role, way):
                findings.append(
                    f"excluded non-lap relation member way/{member_id} role={role!r}"
                )
            else:
                lap.append((way, role))
    else:
        for way_id in selection["way_ids"]:
            way = index.get(("way", int(way_id)))
            if way is None or len(_way_coordinates(way)) < 2:
                findings.append(f"selected way/{way_id} is absent or geometry-free")
            else:
                lap.append((way, "forward"))

    if relation_role_pits:
        pits.extend(relation_role_pits)
        for way in relation_other_pits:
            findings.append(
                "excluded non-primary pit relation member way/"
                f"{int(way['id'])}; explicit pit role takes precedence"
            )
    else:
        pits.extend(relation_other_pits)

    selected_ids = {int(way["id"]) for way, _ in lap}
    pit_ids = {int(way["id"]) for way in pits}
    # An explicit selected-relation pit role is configuration evidence and
    # takes precedence over other pit roads mapped elsewhere in the venue.
    # Only configurations without such a role use the nearby tagged fallback.
    if not pits:
        for (element_type, element_id), element in index.items():
            if (
                element_type != "way"
                or element_id in selected_ids
                or element_id in pit_ids
            ):
                continue
            if _is_pit_way("", element):
                pits.append(element)
                pit_ids.add(element_id)
    return lap, pits, relation, findings


def _allowed_orientations(role: str, *, honour_role: bool = True) -> tuple[bool, ...]:
    if not honour_role:
        return (False, True)
    role_value = role.casefold()
    if role_value == "backward":
        return (True,)
    if role_value == "forward":
        return (False,)
    return (False, True)


def _ordered_chain(
    ways: list[tuple[dict[str, Any], str]],
    *,
    honour_roles: bool = True,
) -> tuple[list[list[float]], list[int], bool] | None:
    if not ways:
        return None
    for first_reverse in _allowed_orientations(ways[0][1], honour_role=honour_roles):
        first = _way_coordinates(ways[0][0])
        if first_reverse:
            first = list(reversed(first))
        result = list(first)
        used = [int(ways[0][0]["id"])]
        valid = True
        for way, role in ways[1:]:
            coordinates = _way_coordinates(way)
            orientation: list[list[float]] | None = None
            for reverse in _allowed_orientations(role, honour_role=honour_roles):
                candidate = list(reversed(coordinates)) if reverse else coordinates
                if _coordinate_key(result[-1]) == _coordinate_key(candidate[0]):
                    orientation = candidate
                    break
            if orientation is None:
                valid = False
                break
            result.extend(orientation[1:])
            used.append(int(way["id"]))
        if valid:
            closed = _coordinate_key(result[0]) == _coordinate_key(result[-1])
            return result, used, closed
    return None


def _exact_graph_cycle(
    ways: list[tuple[dict[str, Any], str]],
    *,
    honour_roles: bool = True,
) -> tuple[list[list[float]], list[int], bool] | None:
    if not ways:
        return None
    by_endpoint: dict[tuple[int, int], list[int]] = {}
    coordinates: list[list[list[float]]] = []
    for index_value, (way, _) in enumerate(ways):
        path = _way_coordinates(way)
        coordinates.append(path)
        by_endpoint.setdefault(_coordinate_key(path[0]), []).append(index_value)
        by_endpoint.setdefault(_coordinate_key(path[-1]), []).append(index_value)
    if any(len(values) != 2 for values in by_endpoint.values()):
        return None

    for first_reverse in _allowed_orientations(ways[0][1], honour_role=honour_roles):
        first = list(reversed(coordinates[0])) if first_reverse else coordinates[0]
        result = list(first)
        used_indexes = {0}
        used_ids = [int(ways[0][0]["id"])]
        while len(used_indexes) < len(ways):
            endpoint = _coordinate_key(result[-1])
            candidates = [
                value
                for value in by_endpoint.get(endpoint, [])
                if value not in used_indexes
            ]
            if len(candidates) != 1:
                break
            candidate_index = candidates[0]
            way, role = ways[candidate_index]
            path = coordinates[candidate_index]
            oriented: list[list[float]] | None = None
            for reverse in _allowed_orientations(role, honour_role=honour_roles):
                candidate = list(reversed(path)) if reverse else path
                if _coordinate_key(candidate[0]) == endpoint:
                    oriented = candidate
                    break
            if oriented is None:
                break
            result.extend(oriented[1:])
            used_indexes.add(candidate_index)
            used_ids.append(int(way["id"]))
        if len(used_indexes) == len(ways):
            closed = _coordinate_key(result[0]) == _coordinate_key(result[-1])
            return result, used_ids, closed
    return None


def _leaf_pruned_cycle_ways(
    ways: list[tuple[dict[str, Any], str]],
) -> tuple[list[tuple[dict[str, Any], str]], list[int]] | None:
    """Remove only edges that cannot belong to any exact-endpoint cycle.

    Iterative degree-one pruning is the graph two-core operation.  It safely
    removes open configuration branches while retaining every edge in a
    closed cycle.  Ambiguous theta graphs and multiple cycles are deliberately
    left for the exact-cycle assembler to reject.
    """

    active = set(range(len(ways)))
    removed: set[int] = set()
    while active:
        degree: dict[tuple[int, int], int] = {}
        endpoints: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {}
        for index_value in active:
            coordinates = _way_coordinates(ways[index_value][0])
            if len(coordinates) < 2:
                removed.add(index_value)
                continue
            start = _coordinate_key(coordinates[0])
            end = _coordinate_key(coordinates[-1])
            endpoints[index_value] = (start, end)
            degree[start] = degree.get(start, 0) + 1
            degree[end] = degree.get(end, 0) + 1
        peel = {
            index_value
            for index_value in active
            if index_value not in endpoints
            or degree[endpoints[index_value][0]] < 2
            or degree[endpoints[index_value][1]] < 2
        }
        if not peel:
            break
        active.difference_update(peel)
        removed.update(peel)
    if not active or not removed:
        return None
    retained = [ways[index_value] for index_value in sorted(active)]
    removed_ids = sorted(int(ways[index_value][0]["id"]) for index_value in removed)
    return retained, removed_ids


def _assemble_lap(
    ways: list[tuple[dict[str, Any], str]],
) -> tuple[list[list[float]], list[int], bool, str, list[str]]:
    ordered = _ordered_chain(ways)
    if ordered is not None:
        coordinates, used, closed = ordered
        return coordinates, used, closed, "selected-order-exact-endpoint-v1", []
    graph = _exact_graph_cycle(ways)
    if graph is not None:
        coordinates, used, closed = graph
        return coordinates, used, closed, "degree-two-exact-endpoint-cycle-v1", []
    # Some source relations contain stale or internally inconsistent
    # ``forward``/``backward`` member roles even though the exact member
    # endpoints form one unambiguous closed cycle.  Relax only orientation,
    # never membership or endpoint equality; record the relaxation for review.
    ordered = _ordered_chain(ways, honour_roles=False)
    if ordered is not None:
        coordinates, used, closed = ordered
        return (
            coordinates,
            used,
            closed,
            "selected-order-exact-endpoint-role-relaxed-v1",
            ["relation member orientation roles conflict with exact endpoints"],
        )
    graph = _exact_graph_cycle(ways, honour_roles=False)
    if graph is not None:
        coordinates, used, closed = graph
        return (
            coordinates,
            used,
            closed,
            "degree-two-exact-endpoint-cycle-role-relaxed-v1",
            ["relation member orientation roles conflict with exact endpoints"],
        )
    pruned = _leaf_pruned_cycle_ways(ways)
    if pruned is not None:
        retained, removed_ids = pruned
        pruning_finding = (
            "leaf-pruned source ways that cannot belong to an exact-endpoint "
            f"cycle: {removed_ids}"
        )
        for method, assembler, honour_roles in (
            (
                "leaf-pruned-selected-order-exact-endpoint-cycle-v1",
                _ordered_chain,
                True,
            ),
            (
                "leaf-pruned-degree-two-exact-endpoint-cycle-v1",
                _exact_graph_cycle,
                True,
            ),
            (
                "leaf-pruned-selected-order-exact-endpoint-cycle-role-relaxed-v1",
                _ordered_chain,
                False,
            ),
            (
                "leaf-pruned-degree-two-exact-endpoint-cycle-role-relaxed-v1",
                _exact_graph_cycle,
                False,
            ),
        ):
            assembled = assembler(retained, honour_roles=honour_roles)
            if assembled is None or not assembled[2]:
                continue
            coordinates, used, closed = assembled
            findings = [pruning_finding]
            if not honour_roles:
                findings.append(
                    "relation member orientation roles conflict with exact endpoints"
                )
            return coordinates, used, closed, method, findings
    if not ways:
        return [], [], False, "unresolved-no-source-way", ["no lap ways selected"]
    # Preserve a real source path for review, but do not concatenate the gaps.
    longest = max(ways, key=lambda value: len(_way_coordinates(value[0])))
    way = longest[0]
    return (
        _way_coordinates(way),
        [int(way["id"])],
        False,
        "unresolved-longest-source-way-no-connector",
        ["selected ways do not form one unambiguous exact-endpoint cycle"],
    )


def _origin(coordinates: Sequence[Sequence[float]]) -> tuple[float, float]:
    if not coordinates:
        return 0.0, 0.0
    west = min(float(value[0]) for value in coordinates)
    east = max(float(value[0]) for value in coordinates)
    south = min(float(value[1]) for value in coordinates)
    north = max(float(value[1]) for value in coordinates)
    return (round((west + east) / 2.0, 8), round((south + north) / 2.0, 8))


def _projector(origin_lon: float, origin_lat: float):
    cosine = math.cos(math.radians(origin_lat))

    def project(coordinate: Sequence[float]) -> list[float]:
        x = math.radians(float(coordinate[0]) - origin_lon) * EARTH_RADIUS_M * cosine
        y = math.radians(float(coordinate[1]) - origin_lat) * EARTH_RADIUS_M
        return [
            round(x, LOCAL_COORDINATE_DECIMALS),
            round(y, LOCAL_COORDINATE_DECIMALS),
        ]

    return project


def _line_feature(
    *,
    feature_id: str,
    coordinates: list[list[float]],
    source_ref: str,
    source_objects: list[dict[str, Any]],
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feature_properties: dict[str, Any] = {
        "id": feature_id,
        "source_ref": source_ref,
        "source_objects": source_objects,
    }
    if properties:
        feature_properties.update(properties)
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "properties": feature_properties,
    }


def _point_feature(
    *,
    feature_id: str,
    coordinate: list[float],
    source_ref: str,
    source_objects: list[dict[str, Any]],
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feature_properties: dict[str, Any] = {
        "id": feature_id,
        "source_ref": source_ref,
        "source_objects": source_objects,
    }
    if properties:
        feature_properties.update(properties)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coordinate},
        "properties": feature_properties,
    }


def _relation_member_way_coordinates(
    member: dict[str, Any],
    index: dict[tuple[str, int], dict[str, Any]],
) -> list[list[float]]:
    coordinates = _way_coordinates(member)
    if coordinates:
        return coordinates
    member_id = member.get("ref")
    if not isinstance(member_id, int):
        return []
    way = index.get(("way", member_id))
    return _way_coordinates(way) if way is not None else []


def _polygonized_rings(
    paths: list[list[list[float]]], *, project
) -> list[Polygon] | None:
    if not paths:
        return []
    linework: list[LineString] = []
    for path in paths:
        projected = [project(value) for value in path]
        if len(projected) < 2:
            return None
        linework.append(LineString(projected))
    polygons, cuts, dangles, invalid = polygonize_full(linework)
    if not cuts.is_empty or not dangles.is_empty or not invalid.is_empty:
        return None
    rings = [Polygon(polygon.exterior.coords) for polygon in polygons.geoms]
    if not rings or any(ring.is_empty or not ring.is_valid for ring in rings):
        return None
    return sorted(
        rings,
        key=lambda ring: (
            tuple(round(value, 6) for value in ring.bounds),
            round(float(ring.area), 6),
            ring.wkt,
        ),
    )


def _multipolygon_geometry(
    element: dict[str, Any],
    *,
    project,
    index: dict[tuple[str, int], dict[str, Any]],
) -> tuple[dict[str, Any], int, int] | None:
    outer_paths: list[list[list[float]]] = []
    inner_paths: list[list[list[float]]] = []
    for member in element.get("members", []):
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        role = str(member.get("role") or "").casefold()
        if role not in {"", "outer", "inner"}:
            continue
        coordinates = _relation_member_way_coordinates(member, index)
        if len(coordinates) < 2:
            return None
        (inner_paths if role == "inner" else outer_paths).append(coordinates)

    outers = _polygonized_rings(outer_paths, project=project)
    inners = _polygonized_rings(inner_paths, project=project)
    if not outers or inners is None:
        return None

    holes_by_outer: list[list[list[tuple[float, float]]]] = [[] for _ in outers]
    for inner in inners:
        point = inner.representative_point()
        candidates = [
            (float(outer.area), index_value)
            for index_value, outer in enumerate(outers)
            if outer.covers(point)
        ]
        if not candidates:
            return None
        _, owner = min(candidates)
        holes_by_outer[owner].append(list(inner.exterior.coords))

    polygons: list[Polygon] = []
    for outer, holes in zip(outers, holes_by_outer, strict=True):
        holes.sort(
            key=lambda ring: (
                min(point[0] for point in ring),
                min(point[1] for point in ring),
                len(ring),
            )
        )
        polygon = orient(Polygon(outer.exterior.coords, holes), sign=1.0)
        if polygon.is_empty or not polygon.is_valid:
            return None
        polygons.append(polygon)

    def ring_coordinates(ring) -> list[list[float]]:
        return [
            [
                round(float(coordinate[0]), LOCAL_COORDINATE_DECIMALS),
                round(float(coordinate[1]), LOCAL_COORDINATE_DECIMALS),
            ]
            for coordinate in ring.coords
        ]

    polygon_coordinates = [
        [
            ring_coordinates(polygon.exterior),
            *(ring_coordinates(ring) for ring in polygon.interiors),
        ]
        for polygon in polygons
    ]
    geometry = (
        {"type": "Polygon", "coordinates": polygon_coordinates[0]}
        if len(polygon_coordinates) == 1
        else {"type": "MultiPolygon", "coordinates": polygon_coordinates}
    )
    return geometry, len(outers), len(inners)


def _element_geojson(
    element: dict[str, Any],
    *,
    project,
    source_ref: str,
    index: dict[tuple[str, int], dict[str, Any]],
    category: str,
) -> dict[str, Any] | None:
    element_type = str(element.get("type"))
    element_id = int(element.get("id"))
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    source_objects = _member_source_objects(element, index)
    properties = {
        "id": f"osm-{element_type}-{element_id}",
        "category": category,
        "source_ref": source_ref,
        "source_objects": source_objects,
        "tags": {str(key): str(value) for key, value in sorted(tags.items())},
    }
    if element_type == "node":
        lat = element.get("lat")
        lon = element.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None
        geometry: dict[str, Any] = {
            "type": "Point",
            "coordinates": project([float(lon), float(lat)]),
        }
    elif element_type == "way":
        source_coordinates = _way_coordinates(element)
        if len(source_coordinates) < 2:
            return None
        coordinates = [project(value) for value in source_coordinates]
        closed = coordinates[0] == coordinates[-1]
        area_category = category in {
            "track_boundaries",
            "buildings",
            "principal_buildings",
            "grandstands",
            "grass",
            "woodland",
            "water",
            "runoffs",
            "gravel_traps",
            "paddocks",
            "garages",
            "pit_buildings",
        }
        if closed and area_category and len(coordinates) >= 4:
            geometry = {"type": "Polygon", "coordinates": [coordinates]}
        else:
            geometry = {"type": "LineString", "coordinates": coordinates}
    elif element_type == "relation" and str(tags.get("type", "")).casefold() == (
        "multipolygon"
    ):
        multipolygon = _multipolygon_geometry(
            element,
            project=project,
            index=index,
        )
        if multipolygon is None:
            return None
        geometry, outer_count, inner_count = multipolygon
        properties["geometry_scope"] = "source-multipolygon-outer-inner-rings-v1"
        properties["outer_ring_count"] = outer_count
        properties["inner_ring_count"] = inner_count
    elif element_type == "relation":
        paths: list[list[list[float]]] = []
        for member in element.get("members", []):
            if not isinstance(member, dict):
                continue
            member_geometry = member.get("geometry")
            if not isinstance(member_geometry, list):
                continue
            path = []
            for value in member_geometry:
                if not isinstance(value, dict):
                    continue
                if isinstance(value.get("lat"), (int, float)) and isinstance(
                    value.get("lon"), (int, float)
                ):
                    path.append(project([float(value["lon"]), float(value["lat"])]))
            if len(path) >= 2:
                paths.append(path)
        if not paths:
            return None
        geometry = {"type": "MultiLineString", "coordinates": paths}
        properties["geometry_scope"] = (
            "source-relation-member-lines-not-inferred-filled-polygon"
        )
    else:
        return None
    serialized_shape = shapely_shape(geometry)
    if serialized_shape.is_empty or not serialized_shape.is_valid:
        # Projection/rounding can expose a self-touch even when the source
        # ring assembly was valid at full precision.  Context is optional, so
        # omit the invalid feature rather than repairing or fabricating it.
        return None
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _classify_context(tags: dict[str, Any]) -> str | None:
    building = str(tags.get("building", "")).casefold()
    landuse = str(tags.get("landuse", "")).casefold()
    natural = str(tags.get("natural", "")).casefold()
    leisure = str(tags.get("leisure", "")).casefold()
    highway = str(tags.get("highway", "")).casefold()
    raceway = str(tags.get("raceway", "")).casefold()
    motor_racing = str(tags.get("motor_racing", "")).casefold()
    amenity = str(tags.get("amenity", "")).casefold()
    surface = str(tags.get("surface", "")).casefold()
    racing_values = {raceway, motor_racing}
    runoff_values = {"runoff", "run-off", "run_off"}
    gravel_trap_values = {"gravel_trap", "gravel-trap", "gravel trap"}
    pit_building_values = {
        "pit_building",
        "pit-building",
        "pit_garage",
        "pit-garage",
    }
    if racing_values & gravel_trap_values or (
        racing_values & runoff_values
        and surface in {"gravel", "fine_gravel", "pebblestone"}
    ):
        return "gravel_traps"
    if racing_values & runoff_values:
        return "runoffs"
    if building in pit_building_values or racing_values & pit_building_values:
        return "pit_buildings"
    if building in {"garage", "garages"}:
        return "garages"
    if "paddock" in {raceway, motor_racing, landuse, amenity}:
        return "paddocks"
    if tags.get("area:highway") == "raceway" or (
        highway == "raceway" and str(tags.get("area", "")).casefold() == "yes"
    ):
        return "track_boundaries"
    if building == "grandstand":
        return "grandstands"
    if building in {"stadium", "sports_hall"} or (
        building in {"commercial", "office", "public"} and tags.get("name")
    ):
        return "principal_buildings"
    if building and building not in {"no", "0", "false"}:
        return "buildings"
    if landuse in {"grass", "meadow", "recreation_ground"} or leisure in {
        "park",
        "garden",
    }:
        return "grass"
    if landuse == "forest" or natural in {"wood", "scrub"}:
        return "woodland"
    if (
        natural in {"water", "wetland", "coastline", "bay"}
        or landuse in {"reservoir", "basin"}
        or tags.get("waterway")
    ):
        return "water"
    if highway and highway != "raceway":
        return "roads"
    return None


def _context_features(
    *,
    index: dict[tuple[str, int], dict[str, Any]],
    selected_way_ids: set[int],
    pit_way_ids: set[int],
    selected_relation_id: int | None,
    project,
    source_ref: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    relation_features: dict[tuple[str, int], tuple[str, dict[str, Any]]] = {}
    suppressed_member_categories: set[tuple[int, str]] = set()

    # Build relation geometry first.  A member way is suppressed only after
    # its parent relation has emitted usable geometry, and only for the same
    # semantic category; a member carrying a distinct road/building meaning
    # remains an independently sourced context feature.
    for (element_type, element_id), element in sorted(index.items()):
        if element_type != "relation" or element_id == selected_relation_id:
            continue
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        category = _classify_context(tags)
        if category is None:
            continue
        feature = _element_geojson(
            element,
            project=project,
            source_ref=source_ref,
            index=index,
            category=category,
        )
        if feature is None:
            continue
        relation_features[(element_type, element_id)] = (category, feature)
        multipolygon = str(tags.get("type", "")).casefold() == "multipolygon"
        for member in element.get("members", []):
            if not isinstance(member, dict) or member.get("type") != "way":
                continue
            role = str(member.get("role") or "").casefold()
            if multipolygon and role not in {"", "outer", "inner"}:
                continue
            member_id = member.get("ref")
            if not isinstance(member_id, int):
                continue
            member_element = index.get(("way", member_id))
            if member_element is None:
                continue
            member_tags = (
                member_element.get("tags")
                if isinstance(member_element.get("tags"), dict)
                else {}
            )
            if _classify_context(member_tags) == category:
                suppressed_member_categories.add((member_id, category))

    for (element_type, element_id), element in sorted(index.items()):
        if element_type not in {"way", "relation"}:
            continue
        if element_type == "way" and (
            element_id in selected_way_ids or element_id in pit_way_ids
        ):
            continue
        if element_type == "relation" and element_id == selected_relation_id:
            continue
        prepared_relation = relation_features.get((element_type, element_id))
        if prepared_relation is not None:
            category, feature = prepared_relation
        else:
            if element_type == "relation":
                continue
            tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
            category = _classify_context(tags)
            if category is None or (element_id, category) in (
                suppressed_member_categories
            ):
                continue
            feature = _element_geojson(
                element,
                project=project,
                source_ref=source_ref,
                index=index,
                category=category,
            )
            if feature is None:
                continue
        dedupe_key = (category, element_type, element_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if category == "track_boundaries":
            boundaries.append(feature)
        else:
            properties = feature["properties"]
            tags = properties["tags"]
            kind = {
                "grass": "grass",
                "woodland": "woodland",
                "water": "water",
                "roads": (
                    "access-road"
                    if str(tags.get("highway", "")).casefold()
                    in {"service", "track", "path", "footway"}
                    else "road"
                ),
                "buildings": "building",
                "principal_buildings": "principal-building",
                "grandstands": "grandstand",
                "runoffs": "runoff",
                "gravel_traps": "gravel-trap",
                "paddocks": "paddock",
                "garages": "garage",
                "pit_buildings": "pit-building",
            }[category]
            current_osm_grandstand = kind == "grandstand"
            temporary_status = (
                CURRENT_OSM_GRANDSTAND_STATUS
                if current_osm_grandstand
                else "permanent-or-current-osm"
            )
            valid_for_season = None if current_osm_grandstand else 2026
            result.append(
                {
                    "id": properties["id"],
                    "kind": kind,
                    "name": tags.get("name"),
                    "geometry": feature["geometry"],
                    "source_ref": source_ref,
                    "source_objects": properties["source_objects"],
                    "tags": tags,
                    "temporary_status": temporary_status,
                    "valid_for_season": valid_for_season,
                    **(
                        {
                            "source_temporality": (CURRENT_OSM_GRANDSTAND_TEMPORALITY),
                            "claim_scope": CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE,
                            "event_configuration_verified": False,
                            "fia_configuration_claimed": False,
                            "operational_semantics_claimed": False,
                        }
                        if current_osm_grandstand
                        else {}
                    ),
                }
            )
    return result, boundaries


def _prune_context_features(
    features: list[dict[str, Any]], *, lap: LineString
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retain a generous deterministic candidate pool for paper adaptation.

    Raw OSM evidence stays intact in the source snapshot.  This only limits
    the serialised model so dense urban venues do not turn the packaged
    catalog into hundreds of megabytes before the renderer's paper-size gate.
    """

    before_counts = {kind: 0 for kind in CONTEXT_CATALOG_CAPS}
    ranked: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = {
        kind: [] for kind in CONTEXT_CATALOG_CAPS
    }
    for feature in features:
        kind = str(feature["kind"])
        if kind not in ranked:
            continue
        before_counts[kind] += 1
        tags = feature.get("tags") if isinstance(feature.get("tags"), dict) else {}
        geometry = shapely_shape(feature["geometry"])
        distance = float(geometry.distance(lap)) if not geometry.is_empty else math.inf
        size = float(geometry.area) if geometry.area > 0.0 else float(geometry.length)
        road_rank = (
            CONTEXT_ROAD_RANK.get(str(tags.get("highway", "")).casefold(), 7)
            if kind in {"road", "access-road"}
            else 0
        )
        rank = (
            0 if feature.get("name") else 1,
            road_rank,
            round(distance, 6),
            -round(size, 6),
            str(feature["id"]),
        )
        ranked[kind].append((rank, feature))

    selected: list[dict[str, Any]] = []
    after_counts = {kind: 0 for kind in CONTEXT_CATALOG_CAPS}
    for kind in CONTEXT_CATALOG_CAPS:
        candidates = sorted(ranked[kind], key=lambda value: value[0])
        retained = candidates[: CONTEXT_CATALOG_CAPS[kind]]
        selected.extend(feature for _, feature in retained)
        after_counts[kind] = len(retained)

    selected.sort(key=lambda feature: str(feature["id"]))
    omitted_count = sum(before_counts.values()) - len(selected)
    return selected, {
        "status": "deterministic-source-backed-candidate-pool-v1",
        "scope": "packaged-model-only-full-source-snapshot-retained",
        "caps_by_kind": CONTEXT_CATALOG_CAPS,
        "before_counts_by_kind": before_counts,
        "after_counts_by_kind": after_counts,
        "before_count": sum(before_counts.values()),
        "after_count": len(selected),
        "omitted_count": omitted_count,
        "rank_recipe": [
            "named-first-within-kind",
            "semantic-highway-rank-first-for-road-kinds",
            "minimum-distance-to-selected-lap",
            "larger-area-or-length",
            "stable-source-feature-id",
        ],
        "omitted_feature_claim": "not-copied-to-model-remains-in-hash-bound-source",
    }


def _pit_way_components(
    pits: Iterable[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Return deterministic components joined only by exact source endpoints."""

    ways_by_id: dict[int, dict[str, Any]] = {}
    endpoint_way_ids: dict[tuple[int, int], set[int]] = {}
    endpoints_by_id: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {}
    for pit in pits:
        pit_id = int(pit["id"])
        coordinates = _way_coordinates(pit)
        if pit_id in ways_by_id or len(coordinates) < 2:
            continue
        ways_by_id[pit_id] = pit
        endpoints = (
            _coordinate_key(coordinates[0]),
            _coordinate_key(coordinates[-1]),
        )
        endpoints_by_id[pit_id] = endpoints
        for endpoint in endpoints:
            endpoint_way_ids.setdefault(endpoint, set()).add(pit_id)

    remaining = set(ways_by_id)
    components: list[list[dict[str, Any]]] = []
    while remaining:
        pending = [min(remaining)]
        remaining.remove(pending[0])
        component_ids: list[int] = []
        while pending:
            pit_id = pending.pop()
            component_ids.append(pit_id)
            neighbours: set[int] = set()
            for endpoint in endpoints_by_id[pit_id]:
                neighbours.update(endpoint_way_ids[endpoint])
            for neighbour in sorted(neighbours & remaining, reverse=True):
                remaining.remove(neighbour)
                pending.append(neighbour)
        components.append([ways_by_id[pit_id] for pit_id in sorted(component_ids)])
    return components


def _exact_open_way_chain(
    ways: Sequence[dict[str, Any]],
) -> tuple[list[list[float]], list[int], str] | None:
    """Assemble one unbranched open chain without snapping or connectors."""

    if not ways:
        return None
    paths: dict[int, list[list[float]]] = {}
    endpoint_way_ids: dict[tuple[int, int], list[int]] = {}
    endpoints_by_id: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {}
    for way in ways:
        way_id = int(way["id"])
        coordinates = _way_coordinates(way)
        if way_id in paths or len(coordinates) < 2:
            return None
        start = _coordinate_key(coordinates[0])
        end = _coordinate_key(coordinates[-1])
        if start == end:
            return None
        paths[way_id] = coordinates
        endpoints_by_id[way_id] = (start, end)
        endpoint_way_ids.setdefault(start, []).append(way_id)
        endpoint_way_ids.setdefault(end, []).append(way_id)

    if any(len(values) > 2 for values in endpoint_way_ids.values()):
        return None
    outer_endpoints = sorted(
        endpoint for endpoint, values in endpoint_way_ids.items() if len(values) == 1
    )
    if len(outer_endpoints) != 2 or any(
        len(values) not in {1, 2} for values in endpoint_way_ids.values()
    ):
        return None

    # Preserve a consistently digitised source direction when the constituent
    # ways provide one.  Otherwise use the stable coordinate-key ordering; the
    # geometry remains source-exact either way and is not race-direction proof.
    directed_starts = {
        endpoint
        for endpoint in outer_endpoints
        if sum(start == endpoint for start, _ in endpoints_by_id.values()) == 1
        and sum(end == endpoint for _, end in endpoints_by_id.values()) == 0
    }
    directed_ends = {
        endpoint
        for endpoint in outer_endpoints
        if sum(start == endpoint for start, _ in endpoints_by_id.values()) == 0
        and sum(end == endpoint for _, end in endpoints_by_id.values()) == 1
    }
    directed_internal = all(
        sum(start == endpoint for start, _ in endpoints_by_id.values()) == 1
        and sum(end == endpoint for _, end in endpoints_by_id.values()) == 1
        for endpoint, values in endpoint_way_ids.items()
        if len(values) == 2
    )
    directed_chain = (
        len(directed_starts) == 1 and len(directed_ends) == 1 and directed_internal
    )
    start_endpoint = (
        next(iter(directed_starts)) if directed_chain else outer_endpoints[0]
    )

    result: list[list[float]] = []
    used_ids: list[int] = []
    unused = set(paths)
    endpoint = start_endpoint
    while unused:
        candidates = sorted(set(endpoint_way_ids.get(endpoint, [])) & unused)
        if len(candidates) != 1:
            return None
        way_id = candidates[0]
        coordinates = paths[way_id]
        start, end = endpoints_by_id[way_id]
        if start == endpoint:
            oriented = coordinates
            endpoint = end
        elif end == endpoint and not directed_chain:
            oriented = list(reversed(coordinates))
            endpoint = start
        else:
            return None
        if result:
            result.extend(oriented[1:])
        else:
            result.extend(oriented)
        used_ids.append(way_id)
        unused.remove(way_id)

    if endpoint == start_endpoint or endpoint not in outer_endpoints:
        return None
    orientation_status = (
        "source-way-direction-not-independent-race-direction-evidence"
        if directed_chain
        else "deterministic-chain-orientation-not-race-direction-evidence"
    )
    return result, used_ids, orientation_status


def _shared_tags(ways: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not ways:
        return {}
    first_tags = ways[0].get("tags")
    if not isinstance(first_tags, dict):
        return {}
    result = dict(first_tags)
    for way in ways[1:]:
        tags = way.get("tags") if isinstance(way.get("tags"), dict) else {}
        result = {
            key: value
            for key, value in result.items()
            if key in tags and tags[key] == value
        }
    return result


def _pit_features(
    pits: Iterable[dict[str, Any]],
    *,
    project,
    source_ref: str,
    lap: LineString,
    lap_coordinates: Sequence[Sequence[float]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build topology-qualified pit chains from source-mapped way pieces.

    Every connected source component must be one unbranched open chain whose
    two outer endpoints, and no interior chain endpoint, exactly join the
    selected lap.  A bad component is reported instead of being silently
    discarded so geometry qualification fails closed.
    """

    result: list[dict[str, Any]] = []
    findings: list[str] = []
    pit_values: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for pit in pits:
        pit_id = int(pit["id"])
        if pit_id in seen_ids:
            continue
        seen_ids.add(pit_id)
        if len(_way_coordinates(pit)) < 2:
            findings.append(f"pit candidate way/{pit_id} has no usable source geometry")
            continue
        pit_values.append(pit)
    lap_coordinate_keys = {_coordinate_key(value) for value in lap_coordinates}
    for component in _pit_way_components(pit_values):
        component_ids = sorted(int(way["id"]) for way in component)
        assembled = _exact_open_way_chain(component)
        if assembled is None:
            findings.append(
                "pit candidate ways do not form one unambiguous exact-endpoint "
                f"open chain: {component_ids}"
            )
            continue
        raw_coordinates, used_ids, orientation_status = assembled
        outer_keys = {
            _coordinate_key(raw_coordinates[0]),
            _coordinate_key(raw_coordinates[-1]),
        }
        outer_lap_joins = outer_keys & lap_coordinate_keys
        interior_lap_joins = {
            _coordinate_key(value) for value in raw_coordinates[1:-1]
        } & lap_coordinate_keys
        if len(outer_lap_joins) != 2:
            findings.append(
                "pit chain outer endpoints do not both join the selected lap "
                f"by exact source coordinates: {component_ids}"
            )
            continue
        if interior_lap_joins:
            findings.append(
                "pit chain has additional interior exact joins to the selected "
                f"lap: {component_ids}"
            )
            continue

        ways_by_id = {int(way["id"]): way for way in component}
        ordered_ways = [ways_by_id[way_id] for way_id in used_ids]
        coordinates = [project(value) for value in raw_coordinates]
        entry_fraction = (
            round(float(lap.project(Point(coordinates[0]))) / lap.length, 9)
            if lap.length > 0.0
            else None
        )
        exit_fraction = (
            round(float(lap.project(Point(coordinates[-1]))) / lap.length, 9)
            if lap.length > 0.0
            else None
        )
        feature_id = (
            f"osm-way-{used_ids[0]}"
            if len(used_ids) == 1
            else "osm-pit-chain-" + "-".join(str(value) for value in sorted(used_ids))
        )
        result.append(
            {
                "id": feature_id,
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "source_ref": source_ref,
                "source_object_id": str(used_ids[0]),
                "source_objects": [_source_object(way) for way in ordered_ways],
                "source_way_ids": used_ids,
                "entry_station_fraction": entry_fraction,
                "exit_station_fraction": exit_fraction,
                "claim_scope": (
                    "source-mapped-pit-lane-centrelines-exact-endpoint-open-chain"
                ),
                "assembly_method": "exact-source-endpoint-open-chain-v1",
                "endpoint_topology": (
                    "two-exact-lap-joins-no-interior-exact-lap-joins"
                ),
                "endpoint_role_status": orientation_status,
                "tags": _shared_tags(ordered_ways),
            }
        )
    result.sort(key=lambda feature: str(feature["id"]))
    return result, findings


def _source_turn_stations(
    *,
    index: dict[tuple[str, int], dict[str, Any]],
    lap: LineString,
    project,
    source_ref: str,
    start_chainage_m: float,
) -> list[dict[str, Any]]:
    stations: list[dict[str, Any]] = []
    for (element_type, element_id), element in sorted(index.items()):
        if element_type != "node":
            continue
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        raceway = str(tags.get("raceway", "")).casefold()
        motor_racing = str(tags.get("motor_racing", "")).casefold()
        if raceway not in {"corner", "turn"} and motor_racing not in {
            "corner",
            "turn",
        }:
            continue
        if not isinstance(element.get("lon"), (int, float)) or not isinstance(
            element.get("lat"), (int, float)
        ):
            continue
        raw_point = project([float(element["lon"]), float(element["lat"])])
        point = Point(raw_point)
        distance_to_lap = float(lap.distance(point))
        if distance_to_lap > 40.0:
            continue
        chainage = float(lap.project(point))
        anchored = lap.interpolate(chainage)
        stations.append(
            {
                "id": f"osm-turn-station-node-{element_id}",
                "label": tags.get("ref") or tags.get("name"),
                "name": tags.get("name"),
                "number": (
                    int(tags["ref"]) if str(tags.get("ref", "")).isdigit() else None
                ),
                "chainage_m": round(chainage, 3),
                "lap_relative_chainage_m": round(
                    (chainage - start_chainage_m) % lap.length, 3
                ),
                "station_fraction": round(chainage / lap.length, 9),
                "point": [round(anchored.x, 3), round(anchored.y, 3)],
                "anchor_method": "osm-tagged-turn-node-projected-to-lap-v1",
                "claim_scope": "source-tagged-turn-station-only",
                "source_ref": source_ref,
                "source_objects": [_source_object(element)],
                "source_offset_m": round(distance_to_lap, 3),
                "status": "source-turn-station",
                "review_status": "source-backed-review-required",
            }
        )
    return sorted(
        stations,
        key=lambda value: (
            value["lap_relative_chainage_m"],
            value["id"],
        ),
    )


def _complete_source_turn_set(stations: list[dict[str, Any]]) -> bool:
    numbers = [station.get("number") for station in stations]
    return bool(stations) and numbers == list(range(1, len(stations) + 1))


def _cyclic_distance(first: float, second: float, length: float) -> float:
    direct = abs(first - second)
    return min(direct, max(0.0, length - direct))


def _derived_turn_stations(
    lap: LineString,
    *,
    source_ref: str,
    source_objects: list[dict[str, Any]],
    start_chainage_m: float,
) -> list[dict[str, Any]]:
    length = float(lap.length)
    if length < 300.0:
        return []
    spacing = 20.0
    count = max(16, int(math.ceil(length / spacing)))
    samples = [
        lap.interpolate(min(length, index * length / count)) for index in range(count)
    ]
    candidates: list[tuple[float, float, Point]] = []
    offset = max(2, int(round(60.0 / (length / count))))
    for index_value, point in enumerate(samples):
        previous = samples[(index_value - offset) % count]
        following = samples[(index_value + offset) % count]
        first_angle = math.atan2(point.y - previous.y, point.x - previous.x)
        second_angle = math.atan2(following.y - point.y, following.x - point.x)
        delta = math.degrees(
            abs(
                math.atan2(
                    math.sin(second_angle - first_angle),
                    math.cos(second_angle - first_angle),
                )
            )
        )
        if delta >= 14.0:
            chainage = index_value * length / count
            candidates.append((delta, chainage, point))
    selected: list[tuple[float, float, Point]] = []
    for candidate in sorted(candidates, key=lambda value: (-value[0], value[1])):
        if all(
            _cyclic_distance(candidate[1], existing[1], length) >= 95.0
            for existing in selected
        ):
            selected.append(candidate)
        if len(selected) >= 30:
            break
    selected.sort(key=lambda value: ((value[1] - start_chainage_m) % length, value[1]))
    return [
        {
            "id": f"geometric-station-{index_value + 1:02d}",
            "label": None,
            "name": None,
            "number": index_value + 1,
            "chainage_m": round(chainage, 3),
            "lap_relative_chainage_m": round((chainage - start_chainage_m) % length, 3),
            "station_fraction": round(chainage / length, 9),
            "point": [round(point.x, 3), round(point.y, 3)],
            "anchor_method": "local-centreline-curvature-station-v1",
            "claim_scope": (
                "geometric-station-only-no-official-corner-or-racing-line-claim"
            ),
            "source_ref": source_ref,
            "source_objects": source_objects,
            "curvature_window_degrees": round(delta, 3),
            "status": "geometric-station",
            "review_status": "derived-review-required",
        }
        for index_value, (delta, chainage, point) in enumerate(selected)
    ]


def _start_finish(
    *,
    relation: dict[str, Any] | None,
    index: dict[tuple[str, int], dict[str, Any]],
    project,
    source_ref: str,
    lap: LineString,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, str, dict[str, Any]]] = []

    def priority(value: str) -> int | None:
        words = _normalised_words(value)
        has_start = "start" in words or "starting" in words
        has_finish = "finish" in words or "finishing" in words
        if has_start and has_finish:
            return 0
        if has_start:
            return 1
        if has_finish:
            return 2
        return None

    if relation is not None:
        for member in relation.get("members", []):
            if not isinstance(member, dict) or member.get("type") != "node":
                continue
            role = str(member.get("role") or "").casefold()
            role_priority = priority(role)
            if role_priority is None:
                continue
            node = index.get(("node", int(member["ref"])))
            if node is not None:
                candidates.append((role_priority, f"relation-member-role:{role}", node))
    for (element_type, _), element in sorted(index.items()):
        if element_type not in {"node", "way"}:
            continue
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        for tag_key in ("raceway", "motor_racing"):
            tag_value = str(tags.get(tag_key, "")).casefold()
            tag_priority = priority(tag_value)
            if tag_priority is not None:
                candidates.append(
                    (
                        tag_priority,
                        f"tag:{tag_key}={tag_value}",
                        element,
                    )
                )

    ranked: list[tuple[int, float, int, int, str, str, Point, dict[str, Any]]] = []
    seen: set[tuple[str, int, str]] = set()
    for candidate_priority, evidence, candidate in candidates:
        key = (str(candidate["type"]), int(candidate["id"]))
        evidence_key = (*key, evidence)
        if evidence_key in seen:
            continue
        seen.add(evidence_key)
        if (
            candidate["type"] == "node"
            and isinstance(candidate.get("lon"), (int, float))
            and isinstance(candidate.get("lat"), (int, float))
        ):
            candidate_point = Point(project([candidate["lon"], candidate["lat"]]))
            status = "source-node-projected-to-lap"
        elif candidate["type"] == "way":
            coordinates = [project(value) for value in _way_coordinates(candidate)]
            if len(coordinates) >= 2:
                candidate_line = LineString(coordinates)
                candidate_point = candidate_line.interpolate(0.5, normalized=True)
                status = "source-line-midpoint-projected-to-lap"
            else:
                continue
        else:
            continue
        offset = float(lap.distance(candidate_point))
        if offset > 30.0 or lap.length <= 0.0:
            continue
        ranked.append(
            (
                candidate_priority,
                offset,
                0 if candidate["type"] == "node" else 1,
                int(candidate["id"]),
                evidence,
                status,
                candidate_point,
                candidate,
            )
        )
    if not ranked:
        return None
    (
        _,
        offset,
        _,
        _,
        evidence,
        status,
        candidate_point,
        candidate,
    ) = min(ranked, key=lambda value: value[:4])
    chainage = float(lap.project(candidate_point))
    anchored = lap.interpolate(chainage)
    return {
        "point": [round(anchored.x, 3), round(anchored.y, 3)],
        "station_fraction": round(chainage / lap.length, 9),
        "source_ref": source_ref,
        "source_object_id": str(candidate["id"]),
        "source_objects": [_source_object(candidate)],
        "status": status,
        "source_offset_m": round(offset, 3),
        "evidence": evidence,
        "claim_scope": "source-mapped-start-finish-anchor-not-derived-from-lap-order",
    }


def _special_sections(
    ways: Iterable[tuple[dict[str, Any], str]],
    *,
    project,
    source_ref: str,
    generic_course_names: Iterable[str] = (),
    excluded_named_way_ids: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """Preserve exact source-way sections without promoting OSM names.

    Bridge and tunnel semantics are source tags.  A selected lap way's name is
    also useful cartographic evidence, but it is explicitly *not* an official
    corner-name claim.  Repeated whole-course names are omitted so that a
    renderer can density-limit the remaining section labels without receiving
    dozens of copies of the venue name.
    """

    def normalise_name(value: Any) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    excluded_names = {
        normalise_name(value) for value in generic_course_names if str(value).strip()
    }
    excluded_named_ids = {int(value) for value in excluded_named_way_ids}
    result: list[dict[str, Any]] = []
    for way, _ in ways:
        tags = way.get("tags") if isinstance(way.get("tags"), dict) else {}
        kinds: list[tuple[str, str | None, str | None]] = []
        if str(tags.get("bridge", "")).casefold() not in {"", "no", "false", "0"}:
            kinds.append(("overpass", None, None))
        if str(tags.get("tunnel", "")).casefold() not in {"", "no", "false", "0"}:
            kinds.append(("tunnel", None, None))
        for name_key in ("name:en", "int_name", "name"):
            name = str(tags.get(name_key) or "").strip()
            if not name:
                continue
            if (
                int(way["id"]) not in excluded_named_ids
                and normalise_name(name) not in excluded_names
            ):
                kinds.append(("named-course-section", name, name_key))
            break
        if not kinds:
            continue
        coordinates = [project(value) for value in _way_coordinates(way)]
        for kind, name, name_key in kinds:
            section = {
                "id": f"osm-way-{way['id']}-{kind}",
                "kind": kind,
                "geometry": {
                    "type": "LineString",
                    "coordinates": coordinates,
                },
                "source_ref": source_ref,
                "source_object_id": str(way["id"]),
                "source_objects": [_source_object(way)],
                "tags": tags,
            }
            if kind == "named-course-section":
                section.update(
                    {
                        "name": name,
                        "name_source_key": name_key,
                        "name_status": "osm-source-tagged-unverified-not-official",
                        "claim_scope": (
                            "exact-selected-osm-way-name-and-geometry; "
                            "not-an-official-corner-or-section-name-claim"
                        ),
                    }
                )
            result.append(section)
    return result


FAMOUS_SECTION_ANCHOR_STATUS = (
    "coordinate-bearing-osm-anchor-not-official-turn-or-apex-coordinate"
)
FAMOUS_SECTION_NAME_STATUS = "formula1-official-source-copy-with-separate-osm-anchor"
FAMOUS_SECTION_ANCHOR_MODES = frozenset(
    {"exact-selected-lap-way-v1", "exact-context-way-near-lap-v1"}
)


def _source_copy_is_present(payload: bytes, source_copy: str) -> bool:
    """Check exact factual copy in one frozen textual payload.

    Only HTML entity decoding and whitespace folding are allowed.  The
    registry therefore cannot silently paraphrase an official source, while
    harmless line wrapping in HTML remains deterministic.
    """

    source_text = html.unescape(payload.decode("utf-8", errors="replace"))
    haystack = " ".join(source_text.split())
    needle = " ".join(html.unescape(source_copy).split())
    return bool(needle) and needle in haystack


def _famous_course_sections(
    records: Any,
    *,
    event_id: str,
    index: dict[tuple[str, int], dict[str, Any]],
    used_way_ids: Iterable[int],
    lap: LineString,
    project,
    osm_source_ref: str,
    sources: dict[str, dict[str, Any]],
    payloads: dict[str, bytes],
) -> tuple[list[dict[str, Any]], set[int]]:
    """Compile official name copy onto exact, separately sourced OSM lines.

    Registry records contain object identities and tag assertions, never raw
    coordinates.  Selected-lap anchors must be members of the exact assembled
    lap.  Context anchors retain their source geometry in place and merely
    prove proximity to the lap; no snapping or inferred turn/apex point is
    produced.
    """

    if records is None:
        return [], set()
    if not isinstance(records, list):
        raise ValueError(f"{event_id}: famous_course_sections must be a list")

    used_ids = {int(value) for value in used_way_ids}
    result: list[dict[str, Any]] = []
    claimed_way_ids: set[int] = set()
    section_ids: set[str] = set()
    priorities: set[int] = set()
    for record_index, raw_record in enumerate(records):
        label = f"{event_id}: famous_course_sections[{record_index}]"
        if not isinstance(raw_record, dict):
            raise ValueError(f"{label} must be an object")
        section_id = str(raw_record.get("id") or "")
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", section_id) is None:
            raise ValueError(f"{label}.id is not a stable lower-case id")
        if section_id in section_ids:
            raise ValueError(
                f"{event_id}: repeated famous course section id {section_id!r}"
            )
        section_ids.add(section_id)

        source_copy = raw_record.get("source_copy")
        if not isinstance(source_copy, str) or not source_copy.strip():
            raise ValueError(f"{label}.source_copy must be non-empty text")
        if source_copy != " ".join(source_copy.split()):
            raise ValueError(f"{label}.source_copy must use canonical whitespace")
        official_source_ref = str(raw_record.get("official_source_ref") or "")
        official_source = sources.get(official_source_ref)
        official_payload = payloads.get(official_source_ref)
        if official_source is None or official_payload is None:
            raise ValueError(
                f"{label} official source {official_source_ref!r} is not frozen"
            )
        if official_source.get("event_id") != event_id:
            raise ValueError(f"{label} official source is not bound to this event")
        if "factual-transcription" not in official_source.get("allowed_uses", []):
            raise ValueError(f"{label} official source does not permit transcription")
        if not _source_copy_is_present(official_payload, source_copy):
            raise ValueError(
                f"{label}.source_copy is absent from frozen {official_source_ref!r}"
            )

        priority = raw_record.get("priority")
        if (
            not isinstance(priority, int)
            or isinstance(priority, bool)
            or not 1 <= priority <= 1000
        ):
            raise ValueError(f"{label}.priority must be an integer in 1..1000")
        if priority in priorities:
            raise ValueError(f"{event_id}: repeated famous section priority {priority}")
        priorities.add(priority)

        anchor = raw_record.get("anchor")
        if not isinstance(anchor, dict):
            raise ValueError(f"{label}.anchor must be an object")
        anchor_source_ref = str(anchor.get("source_ref") or "")
        if anchor_source_ref != osm_source_ref:
            raise ValueError(
                f"{label}.anchor.source_ref must be this event's frozen OSM source"
            )
        anchor_mode = str(anchor.get("mode") or "")
        if anchor_mode not in FAMOUS_SECTION_ANCHOR_MODES:
            raise ValueError(f"{label}.anchor.mode is unsupported")
        if anchor.get("status") != FAMOUS_SECTION_ANCHOR_STATUS:
            raise ValueError(f"{label}.anchor.status weakens the no-coordinate claim")
        maximum_offset: float | None
        if anchor_mode == "exact-context-way-near-lap-v1":
            maximum_offset_value = anchor.get("maximum_lap_offset_m")
            if (
                not isinstance(maximum_offset_value, (int, float))
                or isinstance(maximum_offset_value, bool)
                or not math.isfinite(float(maximum_offset_value))
                or not 0.0 <= float(maximum_offset_value) <= 30.0
            ):
                raise ValueError(
                    f"{label}.anchor.maximum_lap_offset_m must be in 0..30"
                )
            maximum_offset = float(maximum_offset_value)
        else:
            if "maximum_lap_offset_m" in anchor:
                raise ValueError(
                    f"{label} selected-lap anchor must not declare an offset ceiling"
                )
            maximum_offset = None

        object_records = anchor.get("objects")
        if not isinstance(object_records, list) or not object_records:
            raise ValueError(f"{label}.anchor.objects must be a non-empty list")
        source_objects: list[dict[str, Any]] = []
        line_coordinates: list[list[list[float]]] = []
        object_ids: list[int] = []
        source_offsets: list[float] = []
        for object_index, raw_object in enumerate(object_records):
            object_label = f"{label}.anchor.objects[{object_index}]"
            if not isinstance(raw_object, dict) or raw_object.get("type") != "way":
                raise ValueError(f"{object_label} must identify an OSM way")
            object_id = raw_object.get("id")
            if (
                not isinstance(object_id, int)
                or isinstance(object_id, bool)
                or object_id <= 0
                or object_id in object_ids
            ):
                raise ValueError(f"{object_label}.id must be a unique positive integer")
            if object_id in claimed_way_ids:
                raise ValueError(
                    f"{object_label} is already claimed by another famous section"
                )
            source_way = index.get(("way", object_id))
            if source_way is None:
                raise ValueError(f"{object_label} is absent from the frozen OSM source")
            required_tags = raw_object.get("required_tags")
            if not isinstance(required_tags, dict) or not required_tags:
                raise ValueError(f"{object_label}.required_tags must be non-empty")
            tags = (
                source_way.get("tags")
                if isinstance(source_way.get("tags"), dict)
                else {}
            )
            for tag_key, expected_value in sorted(required_tags.items()):
                if not isinstance(tag_key, str) or not isinstance(expected_value, str):
                    raise ValueError(f"{object_label}.required_tags must be text")
                if str(tags.get(tag_key) or "") != expected_value:
                    raise ValueError(
                        f"{object_label} required {tag_key}={expected_value!r}"
                    )
            if anchor_mode == "exact-selected-lap-way-v1":
                if object_id not in used_ids:
                    raise ValueError(f"{object_label} is not in the assembled lap")
            elif object_id in used_ids:
                raise ValueError(f"{object_label} is not a context-only way")

            coordinates = [project(value) for value in _way_coordinates(source_way)]
            if len(coordinates) < 2:
                raise ValueError(f"{object_label} has no usable line geometry")
            source_line = LineString(coordinates)
            offset = float(source_line.distance(lap))
            if maximum_offset is not None and offset > maximum_offset + 1e-9:
                raise ValueError(
                    f"{object_label} is {offset:.3f} m from the lap, above "
                    f"{maximum_offset:.3f} m"
                )
            object_ids.append(object_id)
            source_offsets.append(offset)
            source_objects.append(_source_object(source_way))
            line_coordinates.append(coordinates)

        claimed_way_ids.update(object_ids)
        geometry: dict[str, Any]
        if len(line_coordinates) == 1:
            geometry = {"type": "LineString", "coordinates": line_coordinates[0]}
        else:
            geometry = {"type": "MultiLineString", "coordinates": line_coordinates}
        maximum_measured_offset = max(source_offsets, default=0.0)
        result.append(
            {
                "id": f"famous-course-section-{section_id}",
                "kind": "named-course-section",
                "name": source_copy,
                "source_copy": source_copy,
                "source_ref": official_source_ref,
                "name_source_ref": official_source_ref,
                "name_source_key": "official-source-copy",
                "name_status": FAMOUS_SECTION_NAME_STATUS,
                "official_course_name": True,
                "priority": priority,
                "geometry": geometry,
                "anchor_source_ref": anchor_source_ref,
                "anchor_mode": anchor_mode,
                "anchor_status": FAMOUS_SECTION_ANCHOR_STATUS,
                "anchor_source_object_ids": object_ids,
                "source_object_id": str(object_ids[0]),
                "source_objects": source_objects,
                "source_offset_m": round(maximum_measured_offset, 3),
                **(
                    {"maximum_lap_offset_m": maximum_offset}
                    if maximum_offset is not None
                    else {}
                ),
                "claim_scope": (
                    "official-formula1-name-copy-with-exact-separate-osm-line-anchor; "
                    "anchor-is-not-an-official-turn-or-apex-coordinate; no-snapping-"
                    "tracing-or-inferred-point"
                ),
            }
        )
    return result, claimed_way_ids


def _site_type(raw: str) -> str:
    if raw == "permanent":
        return "permanent"
    if raw.startswith("temporary-street"):
        return "street"
    if raw.startswith("temporary-"):
        return "temporary"
    if raw.startswith("semi-permanent"):
        return "semi-permanent"
    if raw.startswith("hybrid"):
        return "hybrid"
    raise ValueError(f"Unknown site type {raw!r}")


def _public_source_record(source: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "source_kind",
        "publisher",
        "title",
        "url",
        "path",
        "media_type",
        "retrieved_at",
        "payload_bytes",
        "payload_sha256",
        "compressed_bytes",
        "compressed_sha256",
        "licence",
        "terms_url",
        "commercial_use_status",
        "allowed_uses",
        "attribution",
        "event_id",
        "query_sha256",
        "osm_base_timestamp",
        "element_count",
        "selection",
        "context_bbox",
        "component_snapshots",
        "document_version",
        "issued_on",
        "geometry_derivation_status",
        "evidence_scope",
    }
    result = {key: source[key] for key in source if key in allowed}
    result["sha256"] = source["payload_sha256"]
    return result


def _build_event(
    event: dict[str, Any],
    *,
    sources: dict[str, dict[str, Any]],
    payloads: dict[str, bytes],
) -> dict[str, Any]:
    race_source_id = f"f1-race-page-{event['id']}"
    osm_source_id = f"osm-circuit-context-{event['id']}"
    race_facts = (
        _parse_race_page(payloads[race_source_id])
        if race_source_id in payloads
        else {
            "page_title": None,
            "official_circuit_length_m": None,
            "number_of_laps": None,
            "race_distance_km": None,
            "first_grand_prix": None,
            "fastest_lap": {
                "status": "withheld",
                "source_label": "Fastest lap time",
                "withheld_reason": "official-race-page-unavailable",
                "claim_scope": "formula1-official-page-fastest-lap-time",
            },
            "official_track_stats": None,
        }
    )
    track_stats = race_facts.get("official_track_stats")
    source_direction = _normalise_track_direction(
        track_stats if isinstance(track_stats, dict) else None
    )
    direction = source_direction or "withheld"
    layout_evidence = event.get("layout_evidence")
    if not isinstance(layout_evidence, dict):
        layout_evidence = {}
    official_turn_count = layout_evidence.get("official_turn_count")
    if not isinstance(official_turn_count, int) or official_turn_count < 1:
        official_turn_count = None
    review_findings: list[str] = []
    geometry_source = sources.get(osm_source_id)
    geometry_review: dict[str, Any]
    if geometry_source is None or osm_source_id not in payloads:
        review_findings.append("OSM circuit/context snapshot is unavailable")
        model = None
        geometry_status = "unavailable"
        measured_length_m = None
        discrepancy_m = None
        discrepancy_percent = None
        geometry_review = {
            "status": "held",
            "method": "no-snapshot",
            "closed_lap": False,
            "findings": review_findings,
        }
    else:
        snapshot = json.loads(payloads[osm_source_id])
        index = _element_index(snapshot)
        selected, pits, relation, selection_findings = _selection_ways(event, index)
        review_findings.extend(selection_findings)
        raw_lap, used_ids, closed, assembly_method, assembly_findings = _assemble_lap(
            selected
        )
        review_findings.extend(assembly_findings)
        all_selected_coordinates = [
            coordinate for way, _ in selected for coordinate in _way_coordinates(way)
        ]
        origin_lon, origin_lat = _origin(all_selected_coordinates or raw_lap)
        project = _projector(origin_lon, origin_lat)
        projected_lap = [project(value) for value in raw_lap]
        lap_line = (
            LineString(projected_lap) if len(projected_lap) >= 2 else LineString()
        )
        measured_length_m = (
            round(float(lap_line.length), 3) if not lap_line.is_empty else None
        )
        official_length_m = race_facts["official_circuit_length_m"]
        if measured_length_m is not None and official_length_m:
            discrepancy_m = round(measured_length_m - official_length_m, 3)
            discrepancy_percent = round(
                abs(discrepancy_m) / official_length_m * 100.0, 6
            )
        else:
            discrepancy_m = None
            discrepancy_percent = None
        if not closed:
            review_findings.append("lap is not one exact-endpoint closed cycle")
        if official_length_m is None:
            review_findings.append("official circuit length was not parsed")
        elif discrepancy_percent is not None and (
            discrepancy_percent > LENGTH_REVIEW_THRESHOLD_PERCENT
        ):
            review_findings.append(
                "measured OSM lap differs from official length by "
                f"{discrepancy_percent:.6f}%"
            )
        base_unresolved = (
            not closed
            or official_length_m is None
            or discrepancy_percent is None
            or discrepancy_percent > LENGTH_REVIEW_THRESHOLD_PERCENT
        )
        official_configuration_key = (
            str(track_stats.get("circuit_configuration_key") or "").strip()
            if isinstance(track_stats, dict)
            else ""
        )
        configuration_identity_qualified = bool(official_configuration_key)
        if not configuration_identity_qualified:
            review_findings.append(
                "official race-page configuration identity is unavailable"
            )
        selected_by_id = {int(way["id"]): way for way, _ in selected}
        selected_relation_id = int(relation["id"]) if relation is not None else None
        lap_objects: list[dict[str, Any]] = []
        if relation is not None:
            lap_objects.append(_source_object(relation))
        lap_objects.extend(
            _source_object(selected_by_id[way_id])
            for way_id in used_ids
            if way_id in selected_by_id
        )
        lap_feature = (
            _line_feature(
                feature_id=f"{event['id']}-lap",
                coordinates=projected_lap,
                source_ref=osm_source_id,
                source_objects=lap_objects,
                properties={
                    "claim_scope": "osm-centreline-exact-selected-configuration",
                    "width_status": "centreline-only",
                    "closed_lap": closed,
                    "assembly_method": assembly_method,
                    "source_object_id": (
                        str(lap_objects[0]["id"]) if lap_objects else None
                    ),
                },
            )
            if len(projected_lap) >= 2
            else None
        )
        pit_features, pit_topology_findings = _pit_features(
            pits,
            project=project,
            source_ref=osm_source_id,
            lap=lap_line,
            lap_coordinates=raw_lap,
        )
        review_findings.extend(pit_topology_findings)
        pit_topology_valid = bool(pit_features) and not pit_topology_findings
        context, boundaries = _context_features(
            index=index,
            selected_way_ids={int(way["id"]) for way, _ in selected},
            pit_way_ids={int(way["id"]) for way in pits},
            selected_relation_id=selected_relation_id,
            project=project,
            source_ref=osm_source_id,
        )
        context, context_selection = _prune_context_features(context, lap=lap_line)
        start_finish = (
            _start_finish(
                relation=relation,
                index=index,
                project=project,
                source_ref=osm_source_id,
                lap=lap_line,
            )
            if not lap_line.is_empty
            else None
        )
        start_chainage_m = (
            float(lap_line.length) * float(start_finish["station_fraction"])
            if start_finish is not None
            else 0.0
        )
        source_stations = (
            _source_turn_stations(
                index=index,
                lap=lap_line,
                project=project,
                source_ref=osm_source_id,
                start_chainage_m=start_chainage_m,
            )
            if not lap_line.is_empty and start_finish is not None
            else []
        )
        turn_stations = (
            source_stations
            if _complete_source_turn_set(source_stations)
            else (
                _derived_turn_stations(
                    lap_line,
                    source_ref=osm_source_id,
                    source_objects=lap_objects,
                    start_chainage_m=start_chainage_m,
                )
                if closed and not lap_line.is_empty and start_finish is not None
                else []
            )
        )
        turn_inventory_status = (
            "source-tagged-unverified-not-official-inventory"
            if source_stations and turn_stations is source_stations
            else "derived-geometric-only-not-official-inventory"
        )
        if official_turn_count is None:
            review_findings.append(
                "official turn numbering and apex inventory are not source-qualified; "
                "rendered stations are cartographic only"
            )
        else:
            review_findings.append(
                f"official {official_turn_count}-turn count is source-qualified but "
                "turn stations and apex anchors are not; rendered stations remain "
                "cartographic only"
            )
        if not pits:
            review_findings.append("no source-backed pit lane was found")
        elif not pit_topology_valid and not pit_topology_findings:
            review_findings.append("no topology-qualified pit lane was produced")
        if not turn_stations:
            review_findings.append("no defensible turn-station set was produced")
        if start_finish is None:
            review_findings.append(
                "no source-backed start/finish anchor was found; lap order was not substituted"
            )
        operational_geometry_unresolved = (
            base_unresolved
            or not pit_topology_valid
            or not turn_stations
            or start_finish is None
            or lap_feature is None
            or not lap_objects
            or not configuration_identity_qualified
        )
        centreline_qualified = (
            not base_unresolved
            and lap_feature is not None
            and bool(lap_objects)
            and configuration_identity_qualified
        )
        # Polygon winding is an assembler implementation detail.  Direction
        # comes only from the current official race-page trackStats record.
        if source_direction is None:
            raw_direction = (
                str(track_stats.get("direction_raw") or "")
                if isinstance(track_stats, dict)
                else ""
            )
            if raw_direction == "8":
                review_findings.append(
                    "official race-page trackStats direction value '8' describes "
                    "Suzuka's figure-eight topology, not a supported lap orientation; "
                    "lap direction withheld"
                )
            else:
                review_findings.append(
                    "lap direction withheld; source cycle winding is not factual evidence"
                )
        famous_sections, famous_anchor_way_ids = _famous_course_sections(
            layout_evidence.get("famous_course_sections"),
            event_id=str(event["id"]),
            index=index,
            used_way_ids=used_ids,
            lap=lap_line,
            project=project,
            osm_source_ref=osm_source_id,
            sources=sources,
            payloads=payloads,
        )
        source_special_sections = _special_sections(
            selected,
            project=project,
            source_ref=osm_source_id,
            generic_course_names=(
                event["circuit_name"],
                (
                    track_stats.get("circuit_short_name")
                    if isinstance(track_stats, dict)
                    else ""
                ),
                (
                    track_stats.get("circuit_official_name")
                    if isinstance(track_stats, dict)
                    else ""
                ),
            ),
            excluded_named_way_ids=famous_anchor_way_ids,
        )
        if not operational_geometry_unresolved:
            geometry_status = "source-qualified"
        elif centreline_qualified:
            geometry_status = "cartography-qualified-centreline"
        else:
            geometry_status = "provisional"
        omitted_capabilities: list[str] = []
        if start_finish is None:
            omitted_capabilities.append("start-finish-anchor")
        if not turn_stations:
            omitted_capabilities.append("turn-stations")
        if not pit_topology_valid:
            omitted_capabilities.append("pit-lane-topology")
        if source_direction is None:
            omitted_capabilities.append("lap-direction")
        omitted_capabilities.append("current-event-operational-overlays")
        geometry_review = {
            "status": "held" if operational_geometry_unresolved else "passed",
            "qualification_tier": geometry_status,
            "configuration_identity_status": (
                "source-backed-official-race-page-track-stats"
                if configuration_identity_qualified
                else "withheld"
            ),
            "official_configuration_key": official_configuration_key or None,
            "centreline_gate": {
                "status": "passed" if centreline_qualified else "held",
                "requirements": [
                    "exact-closed-selected-osm-centreline",
                    "official-race-page-length",
                    "length-discrepancy-at-or-below-one-percent",
                    "official-race-page-configuration-identity",
                    "source-object-lineage",
                ],
                "claim_scope": (
                    "base-map-cartography-only; no implied start-finish, turn-apex, "
                    "pit-topology, direction, or operational-overlay qualification"
                ),
            },
            "method": assembly_method,
            "selected_way_ids": [int(way["id"]) for way, _ in selected],
            "used_way_ids": used_ids,
            "omitted_selected_way_ids": sorted(
                {int(way["id"]) for way, _ in selected} - set(used_ids)
            ),
            "closed_lap": closed,
            "official_length_m": official_length_m,
            "measured_length_m": measured_length_m,
            "length_discrepancy_m": discrepancy_m,
            "length_discrepancy_percent": discrepancy_percent,
            "review_threshold_percent": LENGTH_REVIEW_THRESHOLD_PERCENT,
            "context_selection": context_selection,
            "turn_inventory_status": turn_inventory_status,
            "pit_lane_topology_status": (
                "passed-exact-endpoint-open-chain-v1" if pit_topology_valid else "held"
            ),
            "official_turn_count": official_turn_count,
            "omitted_capabilities": omitted_capabilities,
            "findings": review_findings,
        }
        candidate_model = {
            "model_version": 1,
            "coordinate_system": "local-metre",
            "origin_wgs84": [origin_lon, origin_lat],
            "coordinate_space": "local-metres",
            "projection_metadata": {
                "longitude": origin_lon,
                "latitude": origin_lat,
                "source_crs": "EPSG:4326",
                "projection": "local-equirectangular-v1",
                "earth_radius_m": EARTH_RADIUS_M,
                "x_axis": "east",
                "y_axis": "north",
                "units": "m",
            },
            "lap": lap_feature,
            "lap_source_objects": lap_objects,
            "pit_lanes": pit_features if pit_topology_valid else [],
            "track_boundaries": boundaries,
            "context": context,
            "turn_stations": turn_stations,
            "turn_inventory": {
                "status": turn_inventory_status,
                "official_count": official_turn_count,
                "official_numbering_verified": False,
                "apex_inventory_verified": False,
                "claim_scope": (
                    "cartographic-stations-only-not-official-corners-or-apexes"
                ),
            },
            "start_finish": start_finish,
            "special_sections": [*source_special_sections, *famous_sections],
            "operational_overlays": {
                "status": "withheld-awaiting-current-fia-event-document",
                "ruleset": "2026-active-aero",
                "straight_mode_zones": [],
                "overtake_detection_points": [],
                "overtake_activation_points": [],
                "speed_traps": [],
                "intermediate_timing_lines": [],
                "legacy_overlay_omitted": True,
            },
            "qualification": {
                "tier": geometry_status,
                "claim_scope": (
                    "source-qualified-centreline-cartography-only-not-an-"
                    "operational-track-map"
                    if geometry_status == "cartography-qualified-centreline"
                    else "source-qualified-normalized-circuit-model"
                ),
                "omitted_capabilities": omitted_capabilities,
                "omissions_must_be_visibly_disclosed": bool(omitted_capabilities),
            },
            "source_ref": osm_source_id,
            "assembly": geometry_review,
        }
        if not centreline_qualified:
            model = None
        else:
            candidate_model["geometry_sha256"] = _geometry_sha256(candidate_model)
            model = candidate_model

    approval_hold_reasons: list[str] = []
    if event["wmsc_status"] != "approved":
        approval_hold_reasons.append("WMSC approval is pending")
    if event["homologation_status"] != "confirmed":
        approval_hold_reasons.append(
            f"homologation status is {event['homologation_status']}"
        )
    if geometry_status == "cartography-qualified-centreline":
        approval_hold_reasons.append(
            "operational geometry is incomplete; centreline-only cartography tier"
        )
    elif geometry_status != "source-qualified":
        approval_hold_reasons.append("circuit geometry is unresolved")
    approval_hold_reasons.extend(
        [
            "circuit-owner outline rights require legal clearance",
            "physical pen calibration and plotted proof remain outstanding",
        ]
    )

    geometry_record: dict[str, Any] = {
        "status": geometry_status,
        "model": model,
        "official_centreline_length_m": {
            "value": race_facts["official_circuit_length_m"],
            "source_ref": race_source_id,
        },
        "review": geometry_review,
    }
    if geometry_source is not None and osm_source_id in payloads:
        geometry_record["source_ref"] = osm_source_id

    event_sources: dict[str, str] = {
        "official_race_page_ref": race_source_id,
    }
    if geometry_source is not None and osm_source_id in payloads:
        event_sources["geometry_and_context_ref"] = osm_source_id

    output = {
        "id": event["id"],
        "calendar_order": event["calendar_order"],
        "calendar_status": (
            "conditional" if event["wmsc_status"] != "approved" else "confirmed"
        ),
        "calendar_status_detail": event["calendar_status"],
        "weekend_start": event["weekend_start"],
        "weekend_end": event["weekend_end"],
        "race_date": event["race_date"],
        "race_day": event["race_day"],
        "event_identity": event["event_identity"],
        "official_event_title": race_facts["page_title"],
        "neutral_display_title": event["neutral_display_title"],
        "event_country_iso2": event["event_country_iso2"],
        "host_country_iso2": event["host_country_iso2"],
        "host_city": str(event["location_label"]).split(",", 1)[0].strip(),
        "location": event["location_label"],
        "calendar_source_refs": event["calendar_source_refs"],
        "approval": {
            "wmsc_status": event["wmsc_status"],
            "homologation_status": event["homologation_status"],
            "checked_at": "2026-08-10",
            "source_refs": event["calendar_source_refs"],
        },
        "official_facts": {
            **race_facts,
            "fastest_lap": {
                **race_facts["fastest_lap"],
                "source_ref": race_source_id,
            },
            "source_ref": race_source_id,
            "claim_scope": "factual-transcription-only-no-map-image-use",
        },
        "circuit": {
            "id": event["id"].removesuffix("-2026"),
            "official_name": event["circuit_name"],
            "name": event["circuit_name"],
            "location_label": event["location_label"],
            "site_type": _site_type(event["site_type"]),
            "site_type_detail": event["site_type"],
            "atlas_context_mode": event["atlas_context_mode"],
            "configuration_season": 2026,
            "direction": direction,
            "lap_direction": direction,
            "lap_direction_status": (
                "source-backed-official-race-page-track-stats"
                if source_direction is not None
                else (
                    "withheld-official-track-stats-value-is-figure-eight-not-lap-orientation"
                    if isinstance(track_stats, dict)
                    and str(track_stats.get("direction_raw") or "") == "8"
                    else "withheld-awaiting-current-authoritative-direction-record"
                )
            ),
            **(
                {"lap_direction_source_ref": race_source_id}
                if source_direction is not None
                else {}
            ),
            "start_finish_reference": {
                "status": "relative-metadata-only-not-coordinate-bearing",
                "start_line_offset_raw": (
                    track_stats.get("start_line_offset_raw")
                    if isinstance(track_stats, dict)
                    else None
                ),
                "control_line_location_raw": (
                    track_stats.get("control_line_location_raw")
                    if isinstance(track_stats, dict)
                    else None
                ),
                "source_ref": race_source_id,
                "geometry_anchor_used": False,
            },
            **({"layout_evidence": layout_evidence} if layout_evidence else {}),
            "lap_length_m": race_facts["official_circuit_length_m"],
            "published_length_km": (
                race_facts["official_circuit_length_m"] / 1000.0
                if race_facts["official_circuit_length_m"] is not None
                else None
            ),
            "geometry": geometry_record,
        },
        "sources": event_sources,
        "rights": {
            "official_page_use": "reference-only-factual-transcription",
            "osm_geometry_use": "conditional-ODbL-produced-work",
            "circuit_outline_ip_clearance": "required",
            "logos_and-protected-graphics": "not-acquired-not-permitted",
            "required_attribution": [
                "© OpenStreetMap contributors",
                "https://www.openstreetmap.org/copyright",
            ],
            "production_release_status": "hold",
        },
        "review": {
            "catalog_build_status": (
                "geometry-verified" if geometry_status == "source-qualified" else "held"
            ),
            "production_ready": False,
            "hold_reasons": approval_hold_reasons,
            "geometry_findings": review_findings,
            "operational_overlay_status": (
                "withheld-awaiting-current-fia-event-document"
            ),
        },
    }
    return output


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that rebuilding matches the existing output byte for byte",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    registry = _load_json(args.registry)
    manifest = _load_json(args.manifest)
    expected_registry_hash = manifest.get("freeze", {}).get("event_registry_sha256")
    actual_registry_hash = _sha256(args.registry.read_bytes())
    if expected_registry_hash != actual_registry_hash:
        raise SystemExit(
            "Event registry hash does not match the frozen source manifest"
        )

    source_records = {
        str(source["id"]): source for source in manifest.get("sources", [])
    }
    payloads: dict[str, bytes] = {}
    source_errors: list[str] = []
    for source_id, source in source_records.items():
        try:
            payloads[source_id] = _verify_manifest_source(source)
        except (OSError, ValueError, gzip.BadGzipFile) as exc:
            source_errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

    events = [
        _build_event(event, sources=source_records, payloads=payloads)
        for event in registry["events"]
    ]
    operationally_incomplete = [
        event["id"]
        for event in events
        if event["circuit"]["geometry"]["status"] != "source-qualified"
    ]
    cartography_qualified = [
        event["id"]
        for event in events
        if event["circuit"]["geometry"]["status"] == "cartography-qualified-centreline"
    ]
    unresolved = [
        event["id"]
        for event in events
        if event["circuit"]["geometry"]["status"]
        not in {"source-qualified", "cartography-qualified-centreline"}
    ]
    pending_policy = [
        event["id"]
        for event in events
        if event["approval"]["wmsc_status"] != "approved"
        or event["approval"]["homologation_status"] != "confirmed"
    ]
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "catalog_id": CATALOG_ID,
        "season": int(registry["season"]),
        "freeze": {
            "frozen_at": registry["frozen_at"],
            "event_registry_path": _portable_source_path(args.registry),
            "event_registry_sha256": actual_registry_hash,
            "source_manifest_path": _portable_source_path(args.manifest),
            "source_manifest_sha256": _sha256(args.manifest.read_bytes()),
            "calendar_model": (
                "22-wmsc-listed-plus-conditional-sepang-event-16; "
                "sakhir-and-jeddah-excluded-called-off"
            ),
            "operational_overlay_policy": (
                "withheld until latest 2026 FIA event circuit document is pinned"
            ),
            "grandstand_context_policy": (
                "frozen current-OSM footprint only; event/FIA configuration and "
                "operational semantics are not claimed"
            ),
            "atlas_context_mode_policy": (
                "event-registry-frozen-independent-of-site-type"
            ),
            "official_map_image_policy": (
                "standalone-images-not-acquired; reference-only-event-documents-"
                "never-traced-or-used-as-geometry"
            ),
            "geometry_review_summary": {
                "event_count": len(events),
                "verified_closed_lap_count": len(events) - len(unresolved),
                "source_qualified_count": (len(events) - len(operationally_incomplete)),
                "cartography_qualified_centreline_count": len(cartography_qualified),
                "cartography_qualified_centreline_ids": cartography_qualified,
                "unresolved_event_count": len(unresolved),
                "unresolved_event_ids": unresolved,
                "operationally_incomplete_event_count": len(operationally_incomplete),
                "operationally_incomplete_event_ids": operationally_incomplete,
                "calendar_or_homologation_hold_ids": pending_policy,
                "source_verification_errors": source_errors,
                "acquisition_errors": manifest.get("acquisition_errors", []),
            },
        },
        "sources": [
            _public_source_record(source)
            for source in sorted(source_records.values(), key=lambda value: value["id"])
        ],
        "events": events,
        "excluded_calendar_events": registry["excluded_calendar_events"],
    }
    # The offline source compiler and renderer share this final semantic gate;
    # catalog drift therefore fails before any packaged JSON is replaced.
    validate_f1_catalog(catalog)
    output_bytes = _pretty_json(catalog)
    if args.check:
        if not args.output.is_file():
            raise SystemExit(f"Cannot check missing output: {args.output}")
        existing = args.output.read_bytes()
        if existing != output_bytes:
            raise SystemExit(
                "Offline rebuild differs from packaged catalog: "
                f"expected {_sha256(existing)}, rebuilt {_sha256(output_bytes)}"
            )
        print(f"deterministic catalog match: {_sha256(existing)}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output_bytes)
    print(
        f"wrote {args.output}: {len(events)} events, "
        f"{len(cartography_qualified)} centreline-only, {len(unresolved)} unresolved, "
        f"sha256={_sha256(output_bytes)}"
    )
    if cartography_qualified:
        print("cartography-qualified centreline: " + ", ".join(cartography_qualified))
    if unresolved:
        print("unresolved geometry: " + ", ".join(unresolved))
    if pending_policy:
        print("calendar/homologation holds: " + ", ".join(pending_policy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
