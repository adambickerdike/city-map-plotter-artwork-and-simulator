#!/usr/bin/env python3
"""Fail-closed semantic QA for Formula One circuit atlases.

The release index is only an inventory.  This checker independently binds each
published artifact to the frozen catalog, measures the final SVG bytes, and
checks circuit topology and pen-plot constraints.  A renderer-provided summary
is useful evidence, but it is never treated as proof of a pass.

The production contracts are deliberately narrow:

* the current-calendar catalog is frozen on 2026-08-10 and contains 23
  releasable events (22 confirmed plus conditional Sepang);
* the legacy-configuration catalog is frozen on 2026-08-11 and binds each
  plate to one reference season plus an explicit current/historic disclosure;
* every requested event/format pair has a unique, format-qualified artifact;
* ``circuit-atlas-v2`` masters preserve one exact source lap plus explicitly
  diagrammatic, physical paired-offset course-corridor passes; and
* masters and one-pen SVG jobs are byte-semantically identical.

The module is importable for focused tests and also provides a small CLI.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence
from unicodedata import combining, normalize
from xml.etree import ElementTree as ET

from city_map_plotter.stroke_font import stroke_text


SCHEMA_VERSION = 1
ARTIFACT_KIND = "circuit-atlas-v2"
SEASON = 2026
FREEZE_DATE = "2026-08-10"
EXPECTED_EVENT_COUNT = 23
LEGACY_CATALOG_CLASS = "legacy-f1-configurations"
LEGACY_SEASON_SCOPE = "multi-era"
LEGACY_FREEZE_DATE = "2026-08-11"
LEGACY_RENDERABLE_IDENTITY_STATUSES = frozenset(
    {
        "exact-historic-source",
        "current-surviving-equivalent",
        "current-source-f1-reference",
    }
)
FORMATS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)
F1_FIELD_PEN_ORDER = (
    "grey-0-25",
    "green-0-25",
    "blue-0-25",
    "purple-0-4",
    "red-0-4",
    "black-0-25",
)
F1_PEN_ORDER = (
    *F1_FIELD_PEN_ORDER,
    "black-0-6",
    "black-0-4",
    "black-1",
)
MAX_PEN_COUNT = 8
MAX_COVERAGE = 0.15
MAX_DENSITY_MM_PER_MM2 = 0.18
F1_DESIGN_DENSITY_MM_PER_MM2 = 0.17
F1_VEGETATION_RESERVE_MM_PER_MM2 = 0.025
MAX_TRAVEL_RATIO = 2.0
MAX_LAP_CLOSURE_MM = 0.10
MIN_CONNECTED_HERO_FRACTION = 0.95
MAX_LENGTH_DISCREPANCY = 0.01
HOST_ROAD_ALIGNMENT_THRESHOLD = 0.85
HOST_ROAD_CLEARANCE_TOLERANCE_MM = 0.02
MAX_PLOT_SECONDS_BY_SHEET = {"A5": 4500.0, "A4": 9000.0, "A3": 14400.0}
COURSE_TARGET_WIDTH_BY_SHEET = {"A5": 0.8, "A4": 1.2, "A3": 1.6}
COURSE_LOGICAL_STROKES_BY_SHEET = {"A5": 3, "A4": 5, "A3": 5}
NAMED_SECTION_LABEL_LIMIT_BY_SHEET = {"A5": 4, "A4": 7, "A3": 10}
GRANDSTAND_FEATURE_LIMIT_BY_SHEET = {"A5": 10, "A4": 24, "A3": 48}
FULL_GEOMETRY_STATUS = "source-qualified"
CENTRELINE_GEOMETRY_STATUS = "cartography-qualified-centreline"

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
MAP_NS = "urn:city-map-plotter:metadata"
SVG = f"{{{SVG_NS}}}"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_TOKEN = re.compile(
    r"(?P<command>[A-Za-z])|"
    r"(?P<number>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)|"
    r"(?P<separator>[\s,]+)|(?P<invalid>.)"
)
_WHITE = frozenset({"white", "#fff", "#ffffff", "rgb(255,255,255)"})
_GRAPHIC_TAGS = frozenset(
    {
        "path",
        "text",
        "image",
        "use",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
    }
)
_SOURCE_GEOMETRY_ROLES = frozenset(
    {
        "lap-centreline",
        "lap-segment",
        "diagrammatic-course-corridor-offset",
        "pit-lane",
        "track-boundary",
        "host-road",
        "water",
        "vegetation",
        "stand",
        "venue-building",
        "special-tunnel",
        "operational-overlay",
    }
)
_HERO_ROLES = frozenset({"lap-centreline", "lap-segment", "hero-segment"})
_LABEL_ROLES = frozenset(
    {
        "turn-label",
        "circuit-label",
        "section-label",
        "context-label",
    }
)
CONTEXT_LABEL_COPY_POLICY_ID = "source-name-drawable-fallback-v1"
CONTEXT_LABEL_NORMALISATION_POLICY_ID = "nfc-supported-diacritics-v2"
CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID = "source-faithful-display-punctuation-v1"
CONTEXT_LABEL_SOURCE_KEYS = ("name", "name:en", "int_name", "name:latin")
FAMOUS_SECTION_NAME_STATUS = "formula1-official-source-copy-with-separate-osm-anchor"
FAMOUS_SECTION_ANCHOR_STATUS = (
    "coordinate-bearing-osm-anchor-not-official-turn-or-apex-coordinate"
)
CONTEXT_MODES = frozenset({"permanent", "urban", "hybrid"})
CURRENT_OSM_GRANDSTAND_STATUS = (
    "frozen-current-osm-footprint-not-event-configuration-verified"
)
CURRENT_OSM_GRANDSTAND_TEMPORALITY = "snapshot-current-at-catalog-freeze"
CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE = (
    "current-osm-grandstand-footprint-only-not-event-or-fia-configuration"
)
CURRENT_OSM_GRANDSTAND_VISIBLE_DISCLOSURE = (
    "CURRENT MAP / GRANDSTANDS"
)
HISTORIC_CURRENT_CONTEXT_VISIBLE_DISCLOSURE = (
    "CURRENT MAP / VENUE CONTEXT"
)
HISTORIC_CURRENT_STAND_VISIBLE_DISCLOSURE = "CURRENT MAP / VENUE + GRANDSTANDS"
LOCAL_CORRIDOR_CLEARANCE_POLICY = "derived-corridor-local-clearance-v1"
LOCAL_CORRIDOR_MINIMUM_SAFE_EDGE_GAP_MM = 1.2
LOCAL_CORRIDOR_MINIMUM_CYCLIC_SEPARATION_FRACTION = 0.25
GRADE_SEPARATION_CUE_POLICY = "black-bridge-deck-bracket-after-red-v2"
_CONTEXT_SUPPORTED_NON_ASCII_GLYPHS = frozenset("©°ÁÈÉÍÑÓÔÖÚÜÝÞ")
_CONTEXT_TEXT_REPLACEMENTS = str.maketrans(
    {
        "\u00a0": " ",
        "\u00ad": "-",
        "\u00ab": '"',
        "\u00bb": '"',
        "\u00c6": "AE",
        "\u00d0": "D",
        "\u00d8": "O",
        "\u00df": "ss",
        "\u00e6": "ae",
        "\u00f0": "d",
        "\u00f8": "o",
        "\u0110": "D",
        "\u0111": "d",
        "\u0126": "H",
        "\u0127": "h",
        "\u0131": "i",
        "\u0138": "k",
        "\u0141": "L",
        "\u0142": "l",
        "\u014a": "N",
        "\u014b": "n",
        "\u0152": "OE",
        "\u0153": "oe",
        "\u0166": "T",
        "\u0167": "t",
        "\u02bc": "'",
        "\u1680": " ",
        "\u2000": " ",
        "\u2001": " ",
        "\u2002": " ",
        "\u2003": " ",
        "\u2004": " ",
        "\u2005": " ",
        "\u2006": " ",
        "\u2007": " ",
        "\u2008": " ",
        "\u2009": " ",
        "\u200a": " ",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2026": "...",
        "\u202f": " ",
        "\u2032": "'",
        "\u2033": '"',
        "\u205f": " ",
        "\u2212": "-",
        "\u3000": " ",
        "\u1e9e": "SS",
    }
)
_MAP_ROLES = (
    _SOURCE_GEOMETRY_ROLES
    | _HERO_ROLES
    | frozenset(
        {
            "turn-marker",
            "turn-label",
            "circuit-label",
            "section-label",
            "context-label",
            "start-finish",
            "pit-entry",
            "pit-exit",
            "grade-separation-cue",
        }
    )
)
_FRAME_ROLES = frozenset({"outer-border", "inner-border"})
_CIRCUIT_INFORMATION_ROLES = frozenset(
    {
        "circuit-information-rule",
        "circuit-information-label",
        "circuit-information-value",
    }
)
_ZONE_BY_ROLE = {
    "title": "title",
    "subtitle": "subtitle",
    "detail": "detail",
    "attribution": "attribution",
    "attribution-disclosure": "furniture",
    "field-frame": "map_field",
}


def _is_source_geometry_role(role: str) -> bool:
    return (
        role in _SOURCE_GEOMETRY_ROLES
        or role.startswith("context-")
        or role.startswith("venue-")
        or role.startswith("special-")
    )


def _is_context_derived_role(role: str) -> bool:
    return role in {"water-stipple-dot", "woodland-symbol", "grass-symbol"}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _array(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _is_frozen_current_osm_grandstand(feature: Mapping[str, Any]) -> bool:
    tags = _mapping(feature.get("tags"))
    tagged_source_objects = [
        value
        for raw in _array(feature.get("source_objects"))
        if (value := _mapping(raw)).get("type") in {"way", "relation"}
        and str(_mapping(value.get("tags")).get("building") or "").casefold()
        == "grandstand"
    ]
    return (
        str(tags.get("building") or "").casefold() == "grandstand"
        and bool(tagged_source_objects)
        and any(_mapping(value.get("tags")) == tags for value in tagged_source_objects)
        and str(feature.get("temporary_status") or "") == CURRENT_OSM_GRANDSTAND_STATUS
        and feature.get("valid_for_season") is None
        and str(feature.get("source_temporality") or "")
        == CURRENT_OSM_GRANDSTAND_TEMPORALITY
        and str(feature.get("claim_scope") or "") == CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE
        and feature.get("event_configuration_verified") is False
        and feature.get("fia_configuration_claimed") is False
        and feature.get("operational_semantics_claimed") is False
    )


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _without_geometry_hashes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_geometry_hashes(item)
            for key, item in value.items()
            if key not in {"geometry_sha256", "source_geometry_sha256"}
        }
    if isinstance(value, list):
        return [_without_geometry_hashes(item) for item in value]
    return value


def canonical_geometry_sha256(model: Mapping[str, Any]) -> str:
    """Hash the complete factual geometry model, excluding digest fields.

    This exact operation is shared with the renderer contract.  It prevents a
    catalog edit from being hidden by retaining an old declared digest.
    """

    return _stable_sha256(_without_geometry_hashes(copy.deepcopy(dict(model))))


def canonical_lap_sha256(lap: Mapping[str, Any]) -> str:
    """Hash only the exact sourced lap record, independently of renderer metadata."""

    return _stable_sha256(_without_geometry_hashes(copy.deepcopy(dict(lap))))


def _load_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r} is forbidden")

    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _text_blob(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).casefold()


def _source_object_identity(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for keys in (("type", "id"), ("kind", "id"), ("source", "id")):
            if all(key in value for key in keys):
                return ":".join(str(value[key]) for key in keys)
        if "id" in value:
            return str(value["id"])
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    return str(value)


def _declared_source_object_ids(model: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for key, item in _walk(model):
        if key == "source_object_id" and isinstance(item, str) and item:
            result.update(part for part in item.split("|") if part)
        elif key == "source_objects" and isinstance(item, list):
            result.update(
                identity
                for value in item
                if (identity := _source_object_identity(value))
            )
    result.update(
        identity
        for value in _array(model.get("lap_source_objects"))
        if (identity := _source_object_identity(value))
    )
    return result


def _source_ids(catalog: Mapping[str, Any]) -> set[str]:
    return {
        str(source.get("id"))
        for source in _array(catalog.get("sources"))
        if isinstance(source, dict) and source.get("id")
    }


def _event_id(event: Mapping[str, Any]) -> str:
    return str(event.get("id") or event.get("event_id") or "")


def _event_status(event: Mapping[str, Any]) -> str:
    return str(
        event.get("calendar_status")
        or event.get("release_status")
        or event.get("status")
        or ""
    ).casefold()


def _model(event: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(
        _mapping(_mapping(event.get("circuit")).get("geometry")).get("model")
    )


def _independent_drawable_context_copy(value: str) -> str | None:
    """Recompute the renderer's source-faithful copy without trusting metadata."""

    source_copy = " ".join(value.split()).upper()
    if not source_copy:
        return None
    composed = normalize("NFC", source_copy.translate(_CONTEXT_TEXT_REPLACEMENTS))
    result: list[str] = []
    for character in composed:
        if character == " " or "!" <= character <= "~":
            result.append(character)
            continue
        if character in _CONTEXT_SUPPORTED_NON_ASCII_GLYPHS:
            result.append(character)
            continue
        decomposed = normalize("NFKD", character)
        fallback = "".join(item for item in decomposed if not combining(item))
        if not fallback or any(
            item != " "
            and not ("!" <= item <= "~")
            and item not in _CONTEXT_SUPPORTED_NON_ASCII_GLYPHS
            for item in fallback
        ):
            # Reject the whole source value.  Partial deletion would recreate
            # the destructive missing-letter labels this audit guards against.
            return None
        result.append(fallback)
    copy_text = " ".join("".join(result).split())
    copy_text = re.sub(r"\s*\\+\s*", " / ", copy_text)
    copy_text = re.sub(r"[,;:]+\s*$", "", copy_text)
    copy_text = " ".join(copy_text.split())
    return copy_text or None


def _independent_context_label_copy(
    feature: Mapping[str, Any],
) -> tuple[str, str, str] | None:
    tags = _mapping(feature.get("tags"))
    for source_name_key in CONTEXT_LABEL_SOURCE_KEYS:
        value = (
            feature.get("name")
            if source_name_key == "name"
            else tags.get(source_name_key)
        )
        if not isinstance(value, str) or not value.strip():
            continue
        copy_text = _independent_drawable_context_copy(value)
        if copy_text is not None:
            return source_name_key, value, copy_text
    return None


def _independent_course_section_label_copy(
    section: Mapping[str, Any],
) -> tuple[str, str, str] | None:
    if section.get("name_status") != FAMOUS_SECTION_NAME_STATUS:
        return _independent_context_label_copy(section)
    source_copy = section.get("source_copy")
    if not isinstance(source_copy, str) or not source_copy.strip():
        return None
    visible_copy = _independent_drawable_context_copy(source_copy)
    if visible_copy is None:
        return None
    return "official-source-copy", source_copy, visible_copy


def _declared_geometry_hash(event: Mapping[str, Any]) -> str:
    geometry = _mapping(_mapping(event.get("circuit")).get("geometry"))
    model = _mapping(geometry.get("model"))
    return str(
        model.get("geometry_sha256")
        or model.get("source_geometry_sha256")
        or geometry.get("source_geometry_sha256")
        or geometry.get("geometry_sha256")
        or ""
    )


def _geojson_coordinates(value: Any) -> list[tuple[float, float]]:
    geometry = _mapping(value)
    if geometry.get("type") == "Feature":
        geometry = _mapping(geometry.get("geometry"))
    if geometry.get("type") != "LineString":
        return []
    result: list[tuple[float, float]] = []
    for point in _array(geometry.get("coordinates")):
        if not isinstance(point, list) or len(point) < 2:
            return []
        x = _finite(point[0])
        y = _finite(point[1])
        if x is None or y is None:
            return []
        result.append((x, y))
    return result


def _geojson_coordinate_bounds(value: Any) -> tuple[float, float, float, float] | None:
    """Measure any GeoJSON geometry without trusting renderer-authored bounds."""

    geometry = _mapping(value)
    if geometry.get("type") == "Feature":
        geometry = _mapping(geometry.get("geometry"))
    points: list[tuple[float, float]] = []

    def collect(raw: Any) -> None:
        if not isinstance(raw, list):
            return
        if len(raw) >= 2:
            x = _finite(raw[0])
            y = _finite(raw[1])
            if x is not None and y is not None:
                points.append((x, y))
                return
        for item in raw:
            collect(item)

    collect(geometry.get("coordinates"))
    for child in _array(geometry.get("geometries")):
        child_bounds = _geojson_coordinate_bounds(child)
        if child_bounds is not None:
            points.extend(
                [
                    (child_bounds[0], child_bounds[1]),
                    (child_bounds[2], child_bounds[3]),
                ]
            )
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _geometry_value(record: Mapping[str, Any]) -> Any:
    return record.get("geometry") or record.get("line") or record.get("path") or record


def _line_length(points: Sequence[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _point_segment_distance(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    square = dx * dx + dy * dy
    if square <= 1e-18:
        return math.hypot(point[0] - first[0], point[1] - first[1])
    position = ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / square
    position = min(1.0, max(0.0, position))
    projected = (first[0] + position * dx, first[1] + position * dy)
    return math.hypot(point[0] - projected[0], point[1] - projected[1])


def _point_line_distance(
    point: tuple[float, float], line: Sequence[tuple[float, float]]
) -> float:
    if len(line) < 2:
        return math.inf
    return min(_point_segment_distance(point, a, b) for a, b in zip(line, line[1:]))


def _point_value(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict) and value.get("type") == "Point":
        value = value.get("coordinates")
    if isinstance(value, dict):
        value = value.get("point") or value.get("coordinates")
    if not isinstance(value, list) or len(value) < 2:
        return None
    x = _finite(value[0])
    y = _finite(value[1])
    return None if x is None or y is None else (x, y)


def _published_length_km(event: Mapping[str, Any]) -> float | None:
    circuit = _mapping(event.get("circuit"))
    geometry = _mapping(circuit.get("geometry"))
    candidates = (
        circuit.get("published_length_km"),
        circuit.get("lap_length_km"),
        _mapping(circuit.get("length")).get("published_km"),
        _mapping(circuit.get("length")).get("value"),
    )
    for value in candidates:
        number = _finite(value)
        if number is not None:
            return number
    metre_candidates = (
        circuit.get("lap_length_m"),
        _mapping(geometry.get("official_centreline_length_m")).get("value"),
    )
    for value in metre_candidates:
        number = _finite(value)
        if number is not None:
            return number / 1000.0
    return None


def _official_fact_catalog_failures(
    event: Mapping[str, Any], *, source_ids: set[str], prefix: str
) -> list[str]:
    """Validate the optional frozen official-fact record independently.

    Legacy and deliberately generic records may omit ``official_facts``.  Once
    the record is present, however, every visible claim it enables must be
    source-backed and internally exact; malformed facts are never silently
    downgraded to generic copy.
    """

    if "official_facts" not in event:
        return []
    raw_facts = event.get("official_facts")
    if not isinstance(raw_facts, dict):
        return [f"{prefix}: official_facts must be an object"]

    failures = _source_ref_failures(raw_facts, source_ids, f"{prefix}: official_facts")
    source_ref = raw_facts.get("source_ref")
    if not isinstance(source_ref, str) or source_ref not in source_ids:
        failures.append(f"{prefix}: official_facts.source_ref is absent or unresolved")

    length_m = _finite(raw_facts.get("official_circuit_length_m"))
    if length_m is None or length_m <= 0.0:
        failures.append(
            f"{prefix}: official_facts.official_circuit_length_m is absent or invalid"
        )

    first_gp = raw_facts.get("first_grand_prix")
    if (
        not isinstance(first_gp, int)
        or isinstance(first_gp, bool)
        or not 1950 <= first_gp <= SEASON
    ):
        failures.append(f"{prefix}: official_facts.first_grand_prix is invalid")

    raw_fastest = raw_facts.get("fastest_lap")
    if not isinstance(raw_fastest, dict):
        failures.append(f"{prefix}: official_facts.fastest_lap must be an object")
        return failures
    fastest = raw_fastest
    status = str(fastest.get("status") or "")
    fastest_source_ref = fastest.get("source_ref")
    if not isinstance(fastest_source_ref, str) or fastest_source_ref not in source_ids:
        failures.append(
            f"{prefix}: official_facts.fastest_lap.source_ref is absent or unresolved"
        )
    if status == "source-backed":
        time_copy = fastest.get("time")
        match = (
            re.fullmatch(r"([0-9]+):([0-5][0-9])\.([0-9]{3})", time_copy)
            if isinstance(time_copy, str)
            else None
        )
        if match is None:
            failures.append(f"{prefix}: official_facts.fastest_lap.time is malformed")
        else:
            expected_ms = (int(match.group(1)) * 60 + int(match.group(2))) * 1000 + int(
                match.group(3)
            )
            if fastest.get("time_ms") != expected_ms:
                failures.append(
                    f"{prefix}: official_facts.fastest_lap.time_ms disagrees with time"
                )
        if (
            not isinstance(fastest.get("driver"), str)
            or not str(fastest.get("driver")).strip()
        ):
            failures.append(f"{prefix}: official_facts.fastest_lap.driver is absent")
        fastest_season = fastest.get("season")
        if (
            not isinstance(fastest_season, int)
            or isinstance(fastest_season, bool)
            or not 1950 <= fastest_season <= SEASON
        ):
            failures.append(f"{prefix}: official_facts.fastest_lap.season is invalid")
        if "withheld_reason" in fastest:
            failures.append(
                f"{prefix}: source-backed fastest lap carries withheld_reason"
            )
    elif status == "withheld":
        if (
            not isinstance(fastest.get("withheld_reason"), str)
            or not str(fastest.get("withheld_reason")).strip()
        ):
            failures.append(f"{prefix}: withheld fastest lap has no withheld_reason")
        if any(key in fastest for key in ("time", "time_ms", "driver", "season")):
            failures.append(
                f"{prefix}: withheld fastest lap carries performance values"
            )
    else:
        failures.append(f"{prefix}: official_facts.fastest_lap.status is unsupported")
    return failures


def _source_ref_failures(value: Any, known: set[str], prefix: str) -> list[str]:
    failures: list[str] = []
    for key, item in _walk_evidence_source_refs(value):
        if key == "source_ref" or key.endswith("_source_ref"):
            if not isinstance(item, str) or item not in known:
                failures.append(f"{prefix}: unresolved source_ref {item!r}")
        elif key == "source_refs" or key.endswith("_source_refs"):
            if not isinstance(item, list) or not item:
                failures.append(f"{prefix}: source_refs must be a non-empty list")
            else:
                for source_ref in item:
                    if not isinstance(source_ref, str) or source_ref not in known:
                        failures.append(
                            f"{prefix}: unresolved source_refs value {source_ref!r}"
                        )
    return failures


def _walk_evidence_source_refs(
    value: Any, *, _inside_source_tags: bool = False
) -> Iterable[tuple[str, Any]]:
    """Yield evidence bindings while treating raw source ``tags`` as opaque.

    OpenStreetMap permits a literal ``source_ref`` tag whose value is raw tag
    data (often a URL or local survey notation), not an ID in this catalog's
    evidence registry. This mirrors the renderer's ``_find_source_refs``
    boundary while retaining fail-closed validation everywhere else.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            inside_tags = _inside_source_tags or key == "tags"
            if not _inside_source_tags and (
                key == "source_ref"
                or key.endswith("_source_ref")
                or key == "source_refs"
                or key.endswith("_source_refs")
            ):
                yield key, item
            else:
                yield from _walk_evidence_source_refs(
                    item,
                    _inside_source_tags=inside_tags,
                )
    elif isinstance(value, list):
        for item in value:
            yield from _walk_evidence_source_refs(
                item,
                _inside_source_tags=_inside_source_tags,
            )


def _operational_overlay_records(
    model: Mapping[str, Any],
) -> tuple[str, list[tuple[str, Mapping[str, Any]]], list[str]]:
    """Return normalized operational claims without trusting renderer metadata."""

    raw = model.get("operational_overlays")
    if raw is None:
        return "withheld", [], []
    if not isinstance(raw, dict):
        return "", [], ["model.operational_overlays must be an object"]
    status = str(raw.get("status") or "withheld").strip().casefold()
    records: list[tuple[str, Mapping[str, Any]]] = []
    failures: list[str] = []
    for key, value in raw.items():
        if key in {"status", "ruleset", "notes"} or isinstance(value, bool):
            continue
        if not isinstance(value, list):
            failures.append(f"model.operational_overlays.{key} must be a list")
            continue
        for index, item in enumerate(value):
            label = f"model.operational_overlays.{key}[{index}]"
            if not isinstance(item, dict):
                failures.append(f"{label} must be an object")
                continue
            records.append((label, item))
    return status, records, failures


def _current_event_claim_failures(
    record: Mapping[str, Any],
    *,
    source_ids: set[str],
    prefix: str,
) -> list[str]:
    """Require event-document evidence for a current-season operational claim."""

    failures: list[str] = []
    source_ref = record.get("source_ref")
    if not isinstance(source_ref, str) or source_ref not in source_ids:
        failures.append(f"{prefix}.source_ref is absent or unresolved")
    if record.get("valid_for_season") != SEASON:
        failures.append(f"{prefix}.valid_for_season must equal {SEASON}")
    if (
        not isinstance(record.get("document_version"), str)
        or not str(record.get("document_version")).strip()
    ):
        failures.append(f"{prefix}.document_version is absent")
    if record.get("evidence_scope") != "current-event-document":
        failures.append(f"{prefix}.evidence_scope must be current-event-document")
    return failures


def _operational_overlay_failures(
    model: Mapping[str, Any], *, source_ids: set[str], prefix: str
) -> list[str]:
    status, records, failures = _operational_overlay_records(model)
    failures = [f"{prefix}: {failure}" for failure in failures]
    if not status:
        failures.append(f"{prefix}: model.operational_overlays.status is absent")
    if status.startswith("withheld"):
        if records:
            failures.append(
                f"{prefix}: model.operational_overlays is withheld but contains claims"
            )
        return failures
    for label, record in records:
        failures.extend(
            _current_event_claim_failures(
                record,
                source_ids=source_ids,
                prefix=f"{prefix}: {label}",
            )
        )
    return failures


def _contains_drs_term(*values: Any) -> bool:
    return bool(re.search(r"\bdrs(?:[-_ ]?zone)?\b", " ".join(map(str, values)), re.I))


def _referenced_source_ids(value: Any) -> set[str]:
    result: set[str] = set()
    for key, item in _walk_evidence_source_refs(value):
        if (key == "source_ref" or key.endswith("_source_ref")) and isinstance(
            item, str
        ):
            if item:
                result.add(item)
        elif (key == "source_refs" or key.endswith("_source_refs")) and isinstance(
            item, list
        ):
            result.update(value for value in item if isinstance(value, str) and value)
    return result


def _legacy_lap_lineage_failures(model: Mapping[str, Any], *, prefix: str) -> list[str]:
    """Bind an assembled legacy lap to every exact frozen OSM object in order."""

    failures: list[str] = []
    declared_objects = _array(model.get("lap_source_objects"))
    lap_record = _mapping(model.get("lap"))
    if lap_record.get("type") == "Feature":
        lap_properties = _mapping(lap_record.get("properties"))
    else:
        lap_properties = lap_record
    bound_objects = _array(lap_properties.get("source_objects"))

    if not declared_objects:
        return [f"{prefix}: lap_source_objects is empty"]
    if not bound_objects:
        failures.append(f"{prefix}: legacy lap has no ordered source_objects binding")

    declared_identities: list[str] = []
    for index, value in enumerate(declared_objects):
        source_object = _mapping(value)
        label = f"{prefix}: lap_source_objects[{index}]"
        object_type = str(source_object.get("type") or "")
        object_id = source_object.get("id")
        version = source_object.get("version")
        timestamp = str(source_object.get("timestamp") or "")
        if object_type not in {"node", "way", "relation"}:
            failures.append(f"{label} has no supported OSM object type")
        if (
            not isinstance(object_id, int)
            or isinstance(object_id, bool)
            or object_id <= 0
        ):
            failures.append(f"{label} has no positive OSM object id")
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            failures.append(f"{label} has no positive OSM object version")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp):
            failures.append(f"{label} has no frozen UTC timestamp")
        declared_identities.append(_source_object_identity(source_object))

    bound_identities = [_source_object_identity(value) for value in bound_objects]
    if bound_identities != declared_identities:
        failures.append(
            f"{prefix}: legacy lap ordered source_objects do not exactly bind "
            "lap_source_objects"
        )
    if len(declared_identities) != len(set(declared_identities)):
        failures.append(f"{prefix}: legacy lap_source_objects contains duplicates")
    if not str(lap_properties.get("source_ref") or "").strip():
        failures.append(f"{prefix}: legacy lap source_ref is absent")
    assembly_method = str(lap_properties.get("assembly_method") or "")
    if "exact" not in assembly_method or not assembly_method.endswith("-v1"):
        failures.append(
            f"{prefix}: legacy lap assembly_method is not exact and versioned"
        )
    if lap_properties.get("closed_lap") is not True:
        failures.append(f"{prefix}: legacy lap closed_lap evidence is not true")
    return failures


def _legacy_event_failures(
    event: Mapping[str, Any],
    *,
    model: Mapping[str, Any],
    source_ids: set[str],
    prefix: str,
) -> list[str]:
    """Validate event-local season identity and non-backdated legacy claims."""

    failures: list[str] = []
    reference_season = event.get("configuration_reference_season")
    if (
        not isinstance(reference_season, int)
        or isinstance(reference_season, bool)
        or not 1950 <= reference_season <= SEASON
    ):
        failures.append(f"{prefix}: configuration_reference_season is invalid")
        return failures

    circuit = _mapping(event.get("circuit"))
    if circuit.get("configuration_season") != reference_season:
        failures.append(
            f"{prefix}: circuit.configuration_season does not bind the reference season"
        )
    if _event_status(event) != "historic-reference":
        failures.append(
            f"{prefix}: legacy renderable status must be historic-reference"
        )

    identity = _mapping(event.get("configuration_identity"))
    identity_status = str(identity.get("status") or "")
    if identity_status not in LEGACY_RENDERABLE_IDENTITY_STATUSES:
        failures.append(f"{prefix}: legacy configuration identity is not renderable")
    if identity.get("f1_reference_season") != reference_season:
        failures.append(f"{prefix}: identity F1 reference season drifted")
    if _array(identity.get("f1_seasons")) != [reference_season]:
        failures.append(
            f"{prefix}: renderable legacy identity must bind exactly one F1 season"
        )

    disclosure = str(event.get("render_disclosure") or "")
    if identity_status == "exact-historic-source":
        expected_disclosure = (
            f"HISTORIC SOURCE COURSE / F1 REFERENCE {reference_season}"
        )
        expected_equivalence = False
    else:
        expected_disclosure = f"CURRENT-SOURCE COURSE / F1 REFERENCE {reference_season}"
        expected_equivalence = identity_status == "current-surviving-equivalent"
    if disclosure != expected_disclosure:
        failures.append(f"{prefix}: legacy render disclosure drifted from identity")
    if identity.get("current_surviving_equivalent") is not expected_equivalence:
        failures.append(
            f"{prefix}: current-surviving equivalence claim is inconsistent"
        )

    identity_sources = _array(identity.get("source_refs"))
    results_source_id = f"f1-results-{reference_season}"
    if results_source_id not in identity_sources:
        failures.append(f"{prefix}: identity lacks {results_source_id!r} evidence")
    failures.extend(_source_ref_failures(identity, source_ids, prefix))

    review = _mapping(event.get("review"))
    if review.get("catalog_build_status") != "geometry-verified-centreline":
        failures.append(f"{prefix}: legacy geometry review status is not verified")
    if review.get("production_ready") is not False:
        failures.append(f"{prefix}: legacy review must retain the production hold")
    if review.get("current_context_historic_claim") is not False:
        failures.append(f"{prefix}: current context is being promoted as historic")
    if review.get("operational_overlay_status") != "withheld":
        failures.append(f"{prefix}: legacy operational overlays must remain withheld")

    for index, value in enumerate(_array(model.get("context"))):
        context = _mapping(value)
        if context.get("kind") == "grandstand":
            if not _is_frozen_current_osm_grandstand(context):
                failures.append(
                    f"{prefix}: context[{index}] grandstand weakens the frozen "
                    "current-OSM footprint-only scope"
                )
            continue
        if context.get("valid_for_season") != reference_season:
            failures.append(
                f"{prefix}: context[{index}].valid_for_season drifted from reference"
            )
        if context.get("source_temporality") != "snapshot-current-not-backdated":
            failures.append(
                f"{prefix}: context[{index}] is not disclosed as current/non-backdated"
            )
    failures.extend(_legacy_lap_lineage_failures(model, prefix=prefix))
    return failures


def validate_f1_event(
    event: Mapping[str, Any],
    *,
    source_ids: set[str],
    legacy_catalog: bool = False,
) -> list[str]:
    """Return fail-closed factual and topological failures for one event."""

    failures: list[str] = []
    event_id = _event_id(event) or "<missing-event-id>"
    prefix = event_id
    if not _event_id(event):
        failures.append("event has no stable id")
    if not legacy_catalog and _event_status(event) not in {"confirmed", "conditional"}:
        failures.append(f"{prefix}: included event status is not confirmed/conditional")
    circuit = _mapping(event.get("circuit"))
    if not legacy_catalog and circuit.get("atlas_context_mode") not in CONTEXT_MODES:
        failures.append(
            f"{prefix}: circuit.atlas_context_mode is absent or unsupported"
        )
    geometry_record = _mapping(circuit.get("geometry"))
    geometry_status = str(geometry_record.get("status") or "")
    centreline_only = geometry_status == CENTRELINE_GEOMETRY_STATUS
    if geometry_status not in {FULL_GEOMETRY_STATUS, CENTRELINE_GEOMETRY_STATUS}:
        failures.append(
            f"{prefix}: renderable geometry status {geometry_status!r} is unsupported"
        )
    model = _model(event)
    if not model:
        failures.append(f"{prefix}: circuit.geometry.model is absent")
        return failures
    failures.extend(
        _official_fact_catalog_failures(
            event,
            source_ids=source_ids,
            prefix=prefix,
        )
    )
    if legacy_catalog:
        failures.extend(
            _legacy_event_failures(
                event,
                model=model,
                source_ids=source_ids,
                prefix=prefix,
            )
        )
    else:
        for index, value in enumerate(_array(model.get("context"))):
            context = _mapping(value)
            if context.get("kind") != "grandstand":
                continue
            if not _is_frozen_current_osm_grandstand(context):
                failures.append(
                    f"{prefix}: context[{index}] grandstand is not a frozen "
                    "current-OSM footprint-only observation"
                )
    if model.get("coordinate_system") != "local-metre":
        failures.append(f"{prefix}: model coordinate_system must be local-metre")
    origin = model.get("origin_wgs84")
    if _point_value(origin) is None:
        failures.append(f"{prefix}: model origin_wgs84 is absent or invalid")
    qualification = _mapping(model.get("qualification"))
    if centreline_only:
        if qualification.get("tier") != CENTRELINE_GEOMETRY_STATUS:
            failures.append(f"{prefix}: centreline qualification tier is absent")
        if not str(qualification.get("claim_scope") or "").strip():
            failures.append(f"{prefix}: centreline qualification claim_scope is absent")
        omitted_capabilities = _array(qualification.get("omitted_capabilities"))
        if not omitted_capabilities or any(
            not isinstance(value, str) or not value.strip()
            for value in omitted_capabilities
        ):
            failures.append(
                f"{prefix}: centreline qualification omitted_capabilities is absent"
            )
        if qualification.get("omissions_must_be_visibly_disclosed") is not True:
            failures.append(
                f"{prefix}: centreline qualification does not require visible omissions"
            )

    required_model_fields = (
        "lap",
        "lap_source_objects",
        "pit_lanes",
        "track_boundaries",
        "context",
        "turn_stations",
        "start_finish",
        "special_sections",
    )
    for field in required_model_fields:
        if field not in model:
            failures.append(f"{prefix}: model.{field} is absent")

    declared_hash = _declared_geometry_hash(event)
    if not _SHA256.fullmatch(declared_hash):
        failures.append(f"{prefix}: source geometry digest is absent or invalid")
    else:
        actual_hash = canonical_geometry_sha256(model)
        if declared_hash != actual_hash:
            failures.append(f"{prefix}: source geometry digest does not bind the model")

    failures.extend(_source_ref_failures(model, source_ids, prefix))
    lap = _geojson_coordinates(model.get("lap"))
    if len(lap) < 4:
        failures.append(
            f"{prefix}: lap must be a source LineString with at least four points"
        )
        return failures
    closure_m = math.hypot(lap[-1][0] - lap[0][0], lap[-1][1] - lap[0][1])
    if closure_m > 0.1:
        failures.append(f"{prefix}: source lap is open by {closure_m:.3f} m")
    measured_km = _line_length(lap) / 1000.0
    published_km = _published_length_km(event)
    if published_km is None or published_km <= 0.0:
        failures.append(f"{prefix}: published lap length is absent or invalid")
    elif measured_km <= 0.0:
        failures.append(f"{prefix}: measured lap length is zero")
    else:
        discrepancy = abs(measured_km - published_km) / published_km
        if discrepancy > MAX_LENGTH_DISCREPANCY + 1e-12:
            failures.append(
                f"{prefix}: published/measured lap discrepancy {100 * discrepancy:.3f}% "
                f"exceeds {100 * MAX_LENGTH_DISCREPANCY:.0f}% (review hold)"
            )

    if not legacy_catalog:
        source_objects = {
            str(item.get("id"))
            for item in _array(model.get("lap_source_objects"))
            if isinstance(item, dict) and item.get("id")
        }
        if not source_objects:
            failures.append(f"{prefix}: lap_source_objects is empty")
        lap_record = _mapping(model.get("lap"))
        if lap_record.get("type") == "Feature":
            lap_record = {**_mapping(lap_record.get("properties")), **lap_record}
        lap_object_id = str(lap_record.get("source_object_id") or "")
        if not lap_object_id or lap_object_id not in source_objects:
            failures.append(f"{prefix}: lap is not bound to one lap_source_object")

    start_finish = _mapping(model.get("start_finish"))
    if start_finish:
        start_point = _point_value(start_finish)
        if start_point is None or _point_line_distance(start_point, lap) > 1.0:
            failures.append(f"{prefix}: start/finish is not on the source lap")
        station = _finite(start_finish.get("station_fraction"))
        if station is not None and not 0.0 <= station < 1.0:
            failures.append(
                f"{prefix}: start/finish station_fraction must be within [0,1)"
            )
        if not start_finish.get("source_ref") or not start_finish.get("status"):
            failures.append(f"{prefix}: start/finish lacks source/status evidence")
    elif not centreline_only:
        failures.append(f"{prefix}: start/finish is not on the source lap")

    pits = _array(model.get("pit_lanes"))
    if not pits and not centreline_only:
        failures.append(f"{prefix}: no source-backed pit lane is present")
    for index, value in enumerate(pits):
        pit = _mapping(value)
        points = _geojson_coordinates(_geometry_value(pit))
        label = f"{prefix}: pit_lanes[{index}]"
        if len(points) < 2:
            failures.append(f"{label} is not a LineString")
            continue
        endpoint_tolerance = max(1.0, _line_length(lap) * 0.005)
        if _point_line_distance(points[0], lap) > endpoint_tolerance:
            failures.append(f"{label} entry does not join the lap")
        if _point_line_distance(points[-1], lap) > endpoint_tolerance:
            failures.append(f"{label} exit does not join the lap")
        entry_station = _finite(pit.get("entry_station_fraction"))
        exit_station = _finite(pit.get("exit_station_fraction"))
        if entry_station is not None and not 0.0 <= entry_station < 1.0:
            failures.append(f"{label} has an invalid entry_station_fraction")
        if exit_station is not None and not 0.0 <= exit_station < 1.0:
            failures.append(f"{label} has an invalid exit_station_fraction")
        if not pit.get("source_ref") or not (
            pit.get("source_object_id") or _array(pit.get("source_objects"))
        ):
            failures.append(f"{label} is not source-object bound")

    turns = _array(model.get("turn_stations"))
    if not turns and not centreline_only:
        failures.append(f"{prefix}: turn_stations is empty")
    turn_ids: list[str] = []
    turn_numbers: list[int] = []
    for index, value in enumerate(turns):
        turn = _mapping(value)
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            failures.append(f"{prefix}: turn_stations[{index}] has no id")
        turn_ids.append(turn_id)
        number = turn.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            failures.append(f"{prefix}: turn {turn_id!r} has no integer number")
        else:
            turn_numbers.append(number)
        turn_station = _finite(turn.get("station_fraction"))
        chainage = _finite(turn.get("chainage_m"))
        if turn_station is None and chainage is not None and measured_km > 0.0:
            turn_station = chainage / (measured_km * 1000.0)
        if turn_station is None or not 0.0 <= turn_station <= 1.0:
            failures.append(f"{prefix}: turn {turn_id!r} has no valid station/chainage")
        if not turn.get("source_ref") or not (
            turn.get("anchor_method") or turn.get("derivation") or turn.get("status")
        ):
            failures.append(f"{prefix}: turn {turn_id!r} lacks source/anchor evidence")
        derivation = str(
            turn.get("derivation")
            or turn.get("anchor_method")
            or turn.get("status")
            or ""
        ).casefold()
        if not derivation:
            failures.append(f"{prefix}: turn {turn_id!r} has no derivation/status")
        status = str(turn.get("status") or "").casefold()
        source_apex_statuses = {
            "true-apex",
            "official-apex",
            "source-backed-apex",
        }
        if "apex" in _text_blob(turn) and status not in source_apex_statuses:
            failures.append(
                f"{prefix}: derived turn {turn_id!r} is falsely called a racing apex"
            )
    if len(turn_ids) != len(set(turn_ids)):
        failures.append(f"{prefix}: turn station ids are duplicated")
    if sorted(turn_numbers) != list(range(1, len(turns) + 1)):
        failures.append(f"{prefix}: turn numbers must contain 1..N exactly once")

    for index, value in enumerate(_array(model.get("special_sections"))):
        section = _mapping(value)
        section_prefix = f"{prefix}: model.special_sections[{index}]"
        if section.get("name_status") == FAMOUS_SECTION_NAME_STATUS:
            if (
                section.get("official_course_name") is not True
                or section.get("source_copy") != section.get("name")
                or section.get("source_ref") != section.get("name_source_ref")
                or section.get("name_source_key") != "official-source-copy"
            ):
                failures.append(
                    f"{section_prefix}: official famous-section copy lineage drifted"
                )
            if (
                section.get("anchor_source_ref") == section.get("name_source_ref")
                or section.get("anchor_source_ref") not in source_ids
                or section.get("name_source_ref") not in source_ids
            ):
                failures.append(
                    f"{section_prefix}: name and anchor sources are not separate/resolved"
                )
            if (
                section.get("anchor_mode")
                not in {
                    "exact-selected-lap-way-v1",
                    "exact-context-way-near-lap-v1",
                }
                or section.get("anchor_status") != FAMOUS_SECTION_ANCHOR_STATUS
            ):
                failures.append(
                    f"{section_prefix}: famous-section anchor policy drifted"
                )
            priority = section.get("priority")
            if (
                not isinstance(priority, int)
                or isinstance(priority, bool)
                or not 1 <= priority <= 1000
            ):
                failures.append(f"{section_prefix}: famous-section priority is invalid")
            source_objects = _array(section.get("source_objects"))
            object_ids = [
                item.get("id")
                for item in source_objects
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            ]
            if not source_objects or object_ids != _array(
                section.get("anchor_source_object_ids")
            ):
                failures.append(
                    f"{section_prefix}: exact anchor object lineage drifted"
                )
            claim_scope = str(section.get("claim_scope") or "")
            if (
                "not-an-official-turn-or-apex-coordinate" not in claim_scope
                or "no-snapping" not in claim_scope
            ):
                failures.append(
                    f"{section_prefix}: associative/no-snapping claim is absent"
                )
        if _contains_drs_term(section.get("kind"), section.get("name")):
            failures.extend(
                _current_event_claim_failures(
                    section,
                    source_ids=source_ids,
                    prefix=section_prefix,
                )
            )
    failures.extend(
        _operational_overlay_failures(
            model,
            source_ids=source_ids,
            prefix=prefix,
        )
    )
    for key, item in _walk(model):
        lowered = key.casefold().replace("_", "-")
        if "connector" in lowered and item not in (None, False, "", [], {}):
            failures.append(f"{prefix}: invented connector field {key!r} is populated")
        if "track-width" in lowered and item not in (None, False, "", [], {}):
            failures.append(
                f"{prefix}: unsupported track-width claim {key!r} is populated"
            )
    return failures


def validate_f1_catalog(
    catalog: Mapping[str, Any],
    *,
    expected_event_count: int = EXPECTED_EVENT_COUNT,
    event_ids: Iterable[str] | None = None,
) -> list[str]:
    """Return ledger failures and validate all or a selected event scope."""

    failures: list[str] = []
    catalog_class = str(catalog.get("catalog_class") or "")
    legacy_catalog = catalog_class == LEGACY_CATALOG_CLASS
    if catalog.get("schema_version") != SCHEMA_VERSION:
        failures.append("catalog schema_version must be 1")
    if not isinstance(catalog.get("catalog_id"), str) or not catalog.get("catalog_id"):
        failures.append("catalog_id is absent")
    if catalog.get("season") != SEASON:
        failures.append(f"catalog season must be {SEASON}")
    if legacy_catalog:
        if catalog.get("season_scope") != LEGACY_SEASON_SCOPE:
            failures.append(
                f"legacy catalog season_scope must be {LEGACY_SEASON_SCOPE!r}"
            )
    elif catalog_class:
        failures.append(f"unsupported catalog_class {catalog_class!r}")
    freeze = catalog.get("freeze")
    if isinstance(freeze, str):
        freeze_date = freeze
    else:
        freeze_map = _mapping(freeze)
        freeze_date = str(
            freeze_map.get("frozen_at")
            or freeze_map.get("freeze_date")
            or freeze_map.get("date")
            or freeze_map.get("as_of")
            or ""
        )[:10]
        if not freeze_map:
            failures.append("catalog freeze ledger is absent")
    expected_freeze_date = LEGACY_FREEZE_DATE if legacy_catalog else FREEZE_DATE
    if freeze_date != expected_freeze_date:
        failures.append(f"catalog freeze must be {expected_freeze_date}")

    sources = _array(catalog.get("sources"))
    source_ids: set[str] = set()
    for index, value in enumerate(sources):
        source = _mapping(value)
        source_id = str(source.get("id") or "")
        if not source_id:
            failures.append(f"sources[{index}] has no id")
        elif source_id in source_ids:
            failures.append(f"source id {source_id!r} is duplicated")
        source_ids.add(source_id)
        if not _SHA256.fullmatch(str(source.get("sha256") or "")):
            failures.append(f"source {source_id!r} has no binding sha256")
        if not source.get("publisher") or not source.get("url"):
            failures.append(f"source {source_id!r} lacks publisher/url evidence")
    if not sources:
        failures.append("catalog sources is empty")

    requested_event_ids = None if event_ids is None else set(event_ids)
    events = _array(catalog.get("events"))
    if len(events) != expected_event_count:
        failures.append(
            f"catalog has {len(events)} release events, expected {expected_event_count}"
        )
    catalog_event_ids = [_event_id(_mapping(event)) for event in events]
    if any(not value for value in catalog_event_ids):
        failures.append("one or more release events has no id")
    if len(catalog_event_ids) != len(set(catalog_event_ids)):
        failures.append("release event ids are duplicated")
    if requested_event_ids is not None:
        unknown_event_ids = requested_event_ids - set(catalog_event_ids)
        if unknown_event_ids:
            failures.append(
                "selected QA event ids are absent from the catalog: "
                + ", ".join(sorted(unknown_event_ids))
            )
    statuses = [_event_status(_mapping(event)) for event in events]
    if not legacy_catalog and expected_event_count == EXPECTED_EVENT_COUNT:
        if statuses.count("conditional") != 1 or statuses.count("confirmed") != 22:
            failures.append(
                "release ledger must contain 22 confirmed and one conditional event"
            )
        conditional = [
            _mapping(event)
            for event in events
            if _event_status(_mapping(event)) == "conditional"
        ]
        if conditional and "sepang" not in _text_blob(conditional[0]):
            failures.append("the conditional release event must be the Sepang event")
    for event in events:
        event_mapping = _mapping(event)
        if (
            requested_event_ids is None
            or _event_id(event_mapping) in requested_event_ids
        ):
            failures.extend(
                validate_f1_event(
                    event_mapping,
                    source_ids=source_ids,
                    legacy_catalog=legacy_catalog,
                )
            )

    excluded = _array(catalog.get("excluded_calendar_events"))
    if not legacy_catalog and expected_event_count == EXPECTED_EVENT_COUNT:
        if len(excluded) != 2:
            failures.append("excluded_calendar_events must ledger Sakhir and Jeddah")
        excluded_blob = _text_blob(excluded)
        if not ("sakhir" in excluded_blob or "bahrain" in excluded_blob):
            failures.append("excluded calendar ledger is missing Sakhir/Bahrain")
        if not ("jeddah" in excluded_blob or "saudi" in excluded_blob):
            failures.append("excluded calendar ledger is missing Jeddah/Saudi Arabia")
    for index, value in enumerate(excluded):
        record = _mapping(value)
        status = str(
            record.get("status") or record.get("calendar_status") or ""
        ).casefold()
        if status not in {"called-off", "cancelled", "canceled"}:
            failures.append(f"excluded_calendar_events[{index}] is not called off")
        failures.extend(_source_ref_failures(record, source_ids, f"excluded[{index}]"))
    return failures


@dataclass(frozen=True, slots=True)
class ParsedSubpath:
    points: tuple[tuple[float, float], ...]
    length: float
    closed: bool

    @property
    def start(self) -> tuple[float, float]:
        return self.points[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.points[-1]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            min(point[0] for point in self.points),
            min(point[1] for point in self.points),
            max(point[0] for point in self.points),
            max(point[1] for point in self.points),
        )


def _path_tokens(data: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(data):
        match = _TOKEN.match(data, position)
        if match is None or match.lastgroup == "invalid":
            raise ValueError("path contains an invalid token")
        if match.lastgroup in {"command", "number"}:
            tokens.append(match.group(0))
        position = match.end()
    return tokens


def _cubic(
    start: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
    end: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    inverse = 1.0 - t
    return (
        inverse**3 * start[0]
        + 3.0 * inverse * inverse * t * first[0]
        + 3.0 * inverse * t * t * second[0]
        + t**3 * end[0],
        inverse**3 * start[1]
        + 3.0 * inverse * inverse * t * first[1]
        + 3.0 * inverse * t * t * second[1]
        + t**3 * end[1],
    )


def _parse_path(data: str) -> list[ParsedSubpath]:
    tokens = _path_tokens(data)
    index = 0
    command = ""
    current = (0.0, 0.0)
    start = (0.0, 0.0)
    points: list[tuple[float, float]] = []
    length = 0.0
    result: list[ParsedSubpath] = []

    def number() -> float:
        nonlocal index
        if index >= len(tokens) or not _NUMBER.fullmatch(tokens[index]):
            raise ValueError("path command is missing a coordinate")
        value = float(tokens[index])
        index += 1
        if not math.isfinite(value):
            raise ValueError("path coordinate is non-finite")
        return value

    def finish(closed: bool) -> None:
        nonlocal points, length
        if points:
            result.append(ParsedSubpath(tuple(points), length, closed))
        points = []
        length = 0.0

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command not in {"M", "L", "H", "V", "C", "Z"}:
                raise ValueError(
                    f"unsupported or relative SVG path command {command!r}"
                )
        if command == "M":
            if points:
                finish(False)
            current = (number(), number())
            start = current
            points = [current]
            command = "L"
        elif command == "L":
            following = (number(), number())
            length += math.hypot(following[0] - current[0], following[1] - current[1])
            points.append(following)
            current = following
        elif command == "H":
            following = (number(), current[1])
            length += abs(following[0] - current[0])
            points.append(following)
            current = following
        elif command == "V":
            following = (current[0], number())
            length += abs(following[1] - current[1])
            points.append(following)
            current = following
        elif command == "C":
            control_1 = (number(), number())
            control_2 = (number(), number())
            following = (number(), number())
            previous = current
            for sample in range(1, 17):
                sampled = _cubic(
                    current, control_1, control_2, following, sample / 16.0
                )
                length += math.hypot(sampled[0] - previous[0], sampled[1] - previous[1])
                points.append(sampled)
                previous = sampled
            current = following
        elif command == "Z":
            if not points:
                raise ValueError("Z appears before M")
            if current != start:
                length += math.hypot(start[0] - current[0], start[1] - current[1])
                points.append(start)
            current = start
            finish(True)
            command = ""
        elif not command:
            raise ValueError("path data must begin with M")
    if points:
        finish(False)
    if not result or any(len(item.points) < 2 for item in result):
        raise ValueError("path has no drawable subpath")
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(slots=True)
class PathEvidence:
    element: ET.Element
    group: ET.Element
    role: str
    pen_id: str
    ink: str
    nib_mm: float
    subpaths: list[ParsedSubpath]

    @property
    def length(self) -> float:
        return sum(item.length for item in self.subpaths)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        boxes = [item.bounds for item in self.subpaths]
        return (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )


def _physical_groups(root: ET.Element) -> list[ET.Element]:
    return [
        element
        for element in root.iter(f"{SVG}g")
        if element.get("data-pen-step") is not None
    ]


def _paths_with_evidence(root: ET.Element, failures: list[str]) -> list[PathEvidence]:
    result: list[PathEvidence] = []
    owned: set[int] = set()
    for group in _physical_groups(root):
        pen_id = str(group.get("data-plot-pen-id") or "")
        ink = str(group.get("data-plot-ink") or "")
        nib = None
        try:
            nib = float(str(group.get("data-plot-nib-mm") or ""))
        except ValueError:
            pass
        if not pen_id or not ink or nib is None or not math.isfinite(nib) or nib <= 0.0:
            failures.append("physical pen group has incomplete pen/ink/nib metadata")
            continue
        for path in group.iter(f"{SVG}path"):
            owned.add(id(path))
            if path.get("transform"):
                failures.append(
                    "visible SVG path uses a transform instead of physical millimetres"
                )
            try:
                parsed = _parse_path(str(path.get("d") or ""))
            except ValueError as exc:
                failures.append(f"SVG path cannot be measured: {exc}")
                continue
            result.append(
                PathEvidence(
                    path,
                    group,
                    str(path.get("data-role") or ""),
                    pen_id,
                    ink,
                    nib,
                    parsed,
                )
            )
    orphan_count = sum(1 for path in root.iter(f"{SVG}path") if id(path) not in owned)
    if orphan_count:
        failures.append(
            f"SVG contains {orphan_count} paths outside physical pen groups"
        )
    return result


def _serialized_title_line_envelope_failures(
    paths: Sequence[PathEvidence],
    page: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Measure title-line ink envelopes from the published SVG paths.

    Paths are grouped by the compositor's explicit line identity before any
    bounds are compared. This intentionally never compares two glyph strokes
    from the same line, which would report the normal joins and intersections
    inside a stroke font as false collisions.
    """

    failures: list[str] = []
    metrics: dict[str, Any] = {}
    title_paths = [path for path in paths if path.role == "title"]
    if not title_paths:
        return ["SVG contains no serialized title paths"], metrics

    contract = _mapping(page.get("title_line_layout"))
    contract_nib = _finite(contract.get("nib_mm"))
    horizontal_ink_inset = _finite(contract.get("horizontal_ink_inset_mm"))
    contract_clearance = _finite(contract.get("min_ink_clearance_mm"))
    contract_bounds_gap = _finite(contract.get("min_path_bounds_gap_mm"))
    maximum_lines_value = _finite(contract.get("maximum_lines"))
    if None in {
        contract_nib,
        horizontal_ink_inset,
        contract_clearance,
        contract_bounds_gap,
        maximum_lines_value,
    }:
        return ["manifest page has no complete title_line_layout contract"], metrics
    assert contract_nib is not None
    assert horizontal_ink_inset is not None
    assert contract_clearance is not None
    assert contract_bounds_gap is not None
    assert maximum_lines_value is not None
    maximum_lines = int(maximum_lines_value)
    if maximum_lines_value != maximum_lines or maximum_lines < 1:
        failures.append("title_line_layout maximum_lines is not a positive integer")
    if contract_nib <= 0.0:
        failures.append("title_line_layout nib_mm is not positive")
    if horizontal_ink_inset + 1e-9 < contract_nib / 2.0:
        failures.append(
            "title_line_layout horizontal inset does not contain the title nib"
        )
    # This is deliberately independent of the compositor summary: the QA
    # policy itself requires one actual title nib of white paper.
    if contract_clearance + 1e-9 < contract_nib:
        failures.append(
            "title_line_layout permits less than one title nib of white clearance"
        )
    if contract_bounds_gap + 1e-9 < contract_nib + contract_clearance:
        failures.append(
            "title_line_layout path-bounds gap does not include both ink envelopes"
        )

    title_zone = _rect(_mapping(page.get("zones_mm")).get("title"))
    if title_zone is None:
        failures.append("manifest page has no valid title zone")

    grouped: dict[str, dict[int, list[PathEvidence]]] = {}
    declared_counts: dict[str, set[int]] = {}
    for path in title_paths:
        block_id = str(path.element.get("data-title-block-id") or "")
        index_raw = str(path.element.get("data-title-line-index") or "")
        count_raw = str(path.element.get("data-title-line-count") or "")
        if not block_id or not index_raw or not count_raw:
            failures.append(
                "serialized title path has incomplete block/line identity metadata"
            )
            continue
        try:
            line_index = int(index_raw)
            line_count = int(count_raw)
        except ValueError:
            failures.append(
                "serialized title path has non-integer line identity metadata"
            )
            continue
        if line_index < 0 or line_count < 1:
            failures.append("serialized title path has invalid line identity metadata")
            continue
        grouped.setdefault(block_id, {}).setdefault(line_index, []).append(path)
        declared_counts.setdefault(block_id, set()).add(line_count)

    if set(grouped) != {"plate-title"}:
        failures.append("serialized title must contain exactly one plate-title block")

    minimum_measured_clearance = math.inf
    total_line_count = 0
    for block_id, line_groups in grouped.items():
        counts = declared_counts.get(block_id, set())
        if len(counts) != 1:
            failures.append(
                f"serialized title block {block_id!r} has inconsistent line counts"
            )
            continue
        declared_count = next(iter(counts))
        indices = sorted(line_groups)
        if indices != list(range(declared_count)):
            failures.append(
                f"serialized title block {block_id!r} has non-contiguous line indices"
            )
        if declared_count > maximum_lines:
            failures.append(
                f"serialized title block {block_id!r} exceeds maximum_lines"
            )
        total_line_count += len(line_groups)

        line_records: list[tuple[int, tuple[float, float, float, float], float]] = []
        for line_index in indices:
            line_paths = line_groups[line_index]
            line_nibs = {round(path.nib_mm, 9) for path in line_paths}
            if len(line_nibs) != 1:
                failures.append(
                    f"title line {line_index} uses more than one physical nib"
                )
            line_nib = max(path.nib_mm for path in line_paths)
            if not math.isclose(
                line_nib,
                contract_nib,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                failures.append(
                    f"title line {line_index} nib {line_nib:g} mm differs from "
                    f"the {contract_nib:g} mm format contract"
                )
            boxes = [path.bounds for path in line_paths]
            centreline_bounds = (
                min(box[0] for box in boxes),
                min(box[1] for box in boxes),
                max(box[2] for box in boxes),
                max(box[3] for box in boxes),
            )
            ink_bounds = (
                centreline_bounds[0] - line_nib / 2.0,
                centreline_bounds[1] - line_nib / 2.0,
                centreline_bounds[2] + line_nib / 2.0,
                centreline_bounds[3] + line_nib / 2.0,
            )
            if title_zone is not None and not _inside(ink_bounds, title_zone, 0.002):
                failures.append(
                    f"title line {line_index} physical ink envelope leaves the title zone"
                )
            line_records.append((line_index, centreline_bounds, line_nib))

        for upper, lower in zip(line_records, line_records[1:]):
            upper_index, upper_bounds, upper_nib = upper
            lower_index, lower_bounds, lower_nib = lower
            if lower_bounds[1] < upper_bounds[1] - 0.002:
                failures.append(
                    "serialized title line indices are not ordered from top to bottom"
                )
            raw_gap = lower_bounds[1] - upper_bounds[3]
            ink_clearance = raw_gap - (upper_nib + lower_nib) / 2.0
            minimum_measured_clearance = min(minimum_measured_clearance, ink_clearance)
            required_clearance = max(
                contract_clearance,
                upper_nib,
                lower_nib,
            )
            if ink_clearance + 0.002 < required_clearance:
                failures.append(
                    f"title lines {upper_index} and {lower_index} leave only "
                    f"{ink_clearance:.3f} mm of white paper; "
                    f"{required_clearance:.3f} mm is required"
                )

    metrics["serialized_title_line_count"] = total_line_count
    metrics["minimum_title_ink_clearance_mm"] = (
        None
        if math.isinf(minimum_measured_clearance)
        else round(minimum_measured_clearance, 3)
    )
    return failures, metrics


def _rect(value: Any) -> tuple[float, float, float, float] | None:
    record = _mapping(value)
    x = _finite(record.get("x"))
    y = _finite(record.get("y"))
    width = _finite(record.get("width"))
    height = _finite(record.get("height"))
    if (
        None in {x, y, width, height}
        or width is None
        or height is None
        or width <= 0
        or height <= 0
    ):
        return None
    assert x is not None and y is not None
    return x, y, x + width, y + height


def _inside(
    bounds: tuple[float, float, float, float],
    zone: tuple[float, float, float, float],
    tolerance: float = 0.05,
) -> bool:
    return (
        zone[0] - tolerance <= bounds[0]
        and zone[1] - tolerance <= bounds[1]
        and bounds[2] <= zone[2] + tolerance
        and bounds[3] <= zone[3] + tolerance
    )


def _parse_label_box(value: str | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    chunks = [part for part in re.split(r"[\s,]+", value.strip()) if part]
    if len(chunks) != 4:
        return None
    try:
        x, y, width, height = (float(part) for part in chunks)
    except ValueError:
        return None
    if (
        not all(math.isfinite(item) for item in (x, y, width, height))
        or width <= 0
        or height <= 0
    ):
        return None
    return x, y, x + width, y + height


def _context_label_lineage_failures(
    event: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    """Independently reconcile visible context copy with frozen source tags."""

    failures: list[str] = []
    label_ledger = _mapping(f1_rendering.get("labels"))
    context_copy_policy = _mapping(label_ledger.get("context_copy_policy"))
    expected_context_copy_policy = {
        "policy_id": CONTEXT_LABEL_COPY_POLICY_ID,
        "source_key_precedence": list(CONTEXT_LABEL_SOURCE_KEYS),
        "normalisation_policy_id": CONTEXT_LABEL_NORMALISATION_POLICY_ID,
        "display_punctuation_policy_id": (CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID),
        "display_punctuation_rules": [
            "backslash-delimiter-to-spaced-solidus",
            "trim-orphan-terminal-comma-semicolon-colon",
            "preserve-other-internal-punctuation",
        ],
        "unsupported_script_policy": (
            "omit-whole-label-without-drawable-sourced-alternative"
        ),
        "invented_translation_allowed": False,
    }
    source_context = {
        str(feature.get("id")): feature
        for feature in _array(_model(event).get("context"))
        if isinstance(feature, dict) and feature.get("id")
    }
    context_placements: dict[str, dict[str, Any]] = {}
    for raw_placement in _array(label_ledger.get("placements")):
        placement = _mapping(raw_placement)
        if placement.get("role") != "context-label":
            continue
        label_id = str(placement.get("id") or "")
        feature_id = str(placement.get("feature_id") or "")
        if not label_id or label_id in context_placements:
            failures.append("context-label placement IDs are absent or duplicated")
            continue
        context_placements[label_id] = placement
        feature = source_context.get(feature_id)
        if feature is None:
            failures.append(
                f"context-label placement {label_id!r} invents feature {feature_id!r}"
            )
            continue
        expected_copy = _independent_context_label_copy(feature)
        if expected_copy is None:
            failures.append(
                f"context-label placement {label_id!r} has no drawable sourced name"
            )
            continue
        expected_key, expected_source, expected_visible = expected_copy
        if (
            placement.get("source_name_key") != expected_key
            or placement.get("source_copy") != expected_source
            or placement.get("copy") != expected_visible
            or placement.get("copy_policy_id") != CONTEXT_LABEL_COPY_POLICY_ID
            or placement.get("normalisation_policy_id")
            != CONTEXT_LABEL_NORMALISATION_POLICY_ID
            or placement.get("display_punctuation_policy_id")
            != CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID
        ):
            failures.append(
                f"context-label placement {label_id!r} copy/source policy drifted"
            )

    serialized_context_labels: dict[str, list[PathEvidence]] = {}
    for path in paths:
        if path.role != "context-label":
            continue
        label_id = str(path.element.get("data-label-id") or "")
        serialized_context_labels.setdefault(label_id, []).append(path)
    if (
        context_placements or serialized_context_labels
    ) and context_copy_policy != expected_context_copy_policy:
        failures.append("manifest has no binding source-faithful context-copy policy")
    if set(serialized_context_labels) != set(context_placements):
        failures.append(
            "serialized context-label IDs differ from manifest context placements"
        )
    for label_id, label_paths in serialized_context_labels.items():
        placement = context_placements.get(label_id)
        if placement is None:
            continue
        expected_attributes = {
            "data-feature-id": str(placement.get("feature_id") or ""),
            "data-source-name-key": str(placement.get("source_name_key") or ""),
            "data-source-copy": str(placement.get("source_copy") or ""),
            "data-visible-copy": str(placement.get("copy") or ""),
            "data-copy-policy-id": CONTEXT_LABEL_COPY_POLICY_ID,
            "data-normalisation-policy-id": (CONTEXT_LABEL_NORMALISATION_POLICY_ID),
            "data-display-punctuation-policy-id": (
                CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID
            ),
        }
        if any(
            path.element.get(key) != expected
            for path in label_paths
            for key, expected in expected_attributes.items()
        ):
            failures.append(
                f"serialized context-label {label_id!r} source/copy lineage drifted"
            )

    for omission in _array(f1_rendering.get("feature_omissions")):
        record = _mapping(omission)
        if record.get("reason") != "context-name-no-drawable-sourced-copy":
            continue
        feature_id = str(record.get("feature_id") or "")
        feature = source_context.get(feature_id)
        if (
            feature is None
            or _independent_context_label_copy(feature) is not None
            or record.get("copy_policy_id") != CONTEXT_LABEL_COPY_POLICY_ID
            or record.get("normalisation_policy_id")
            != CONTEXT_LABEL_NORMALISATION_POLICY_ID
            or record.get("display_punctuation_policy_id")
            != CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID
        ):
            failures.append(
                f"unsupported context-name omission {feature_id!r} is not source-faithful"
            )
    return failures


def _rect_distance(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    dx = max(0.0, first[0] - second[2], second[0] - first[2])
    dy = max(0.0, first[1] - second[3], second[1] - first[3])
    return math.hypot(dx, dy)


def _named_section_lineage_failures(
    event: Mapping[str, Any],
    page: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    """Bind section copy to exact source tags and keep adjacent names legible."""

    failures: list[str] = []
    sections = {
        str(section.get("id")): section
        for section in _array(_model(event).get("special_sections"))
        if isinstance(section, dict)
        and section.get("id")
        and str(section.get("kind") or "").casefold().replace("_", "-")
        == "named-course-section"
    }
    label_ledger = _mapping(f1_rendering.get("labels"))
    section_ledger = _mapping(label_ledger.get("named_course_sections"))
    placement_records = {
        str(record.get("id")): record
        for raw in _array(label_ledger.get("placements"))
        if (record := _mapping(raw)).get("role") == "section-label" and record.get("id")
    }
    serialized: dict[str, list[PathEvidence]] = {}
    for path in paths:
        if path.role == "section-label":
            serialized.setdefault(
                str(path.element.get("data-label-id") or ""), []
            ).append(path)
    if set(serialized) != set(placement_records):
        failures.append(
            "serialized section-label IDs differ from manifest section placements"
        )
    paper = str(page.get("paper") or "").upper()
    limit = NAMED_SECTION_LABEL_LIMIT_BY_SHEET.get(paper)
    if limit is None:
        failures.append("section-label QA cannot resolve the paper size")
    elif len(serialized) > limit:
        failures.append(
            f"section-label count {len(serialized)} exceeds the {paper} limit {limit}"
        )
    if limit is not None:
        famous_sections = sorted(
            (
                section
                for section in sections.values()
                if section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
            ),
            key=lambda section: (
                -int(section.get("priority", 0)),
                str(section.get("id") or ""),
            ),
        )
        expected_famous_ids = {
            str(section["id"]) for section in famous_sections[:limit]
        }
        emitted_feature_ids = {
            str(placement.get("feature_id") or "")
            for placement in placement_records.values()
        }
        missing_famous_ids = sorted(expected_famous_ids - emitted_feature_ids)
        if missing_famous_ids:
            failures.append(
                f"{paper} famous-section priority gate omitted: "
                + ", ".join(missing_famous_ids)
            )
    emitted_sections = [
        sections[feature_id]
        for placement in placement_records.values()
        if (feature_id := str(placement.get("feature_id") or "")) in sections
    ]
    official_section_count = sum(
        section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
        for section in emitted_sections
    )
    associative_section_count = sum(
        section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
        and section.get("anchor_mode") == "exact-context-way-near-lap-v1"
        for section in emitted_sections
    )
    expected_status_counts: dict[str, int] = {}
    for section in emitted_sections:
        status = str(section.get("name_status") or "")
        expected_status_counts[status] = expected_status_counts.get(status, 0) + 1
    expected_ledger_status = (
        FAMOUS_SECTION_NAME_STATUS
        if official_section_count and official_section_count == len(emitted_sections)
        else (
            "mixed-official-and-osm-source-copy"
            if official_section_count
            else "osm-source-tagged-unverified-not-official"
        )
    )
    if section_ledger:
        if int(section_ledger.get("emitted_count", -1)) != len(serialized):
            failures.append("named-course-section emitted-count ledger drifted")
        if section_ledger.get("official_course_name_claimed") is not bool(
            official_section_count
        ):
            failures.append("named-course-section official-name ledger drifted")
        if section_ledger.get("name_status") != expected_ledger_status:
            failures.append("named-course-section ledger name status drifted")
        expected_priority_policy = (
            "official-source-priority-then-spatial-distribution"
            if official_section_count
            else "before-ordinary-context-copy"
        )
        if section_ledger.get("priority") != expected_priority_policy:
            failures.append("named-course-section priority ledger drifted")
        if official_section_count:
            if _mapping(section_ledger.get("name_status_counts")) != (
                expected_status_counts
            ):
                failures.append("named-course-section status-count ledger drifted")
            if section_ledger.get("official_source_copy_count") != (
                official_section_count
            ):
                failures.append("named-course-section official-copy count drifted")
            if section_ledger.get("associative_anchor_count") != (
                associative_section_count
            ):
                failures.append("named-course-section associative count drifted")
            if section_ledger.get("associative_anchor_disclosure_visible") is not False:
                failures.append(
                    "named-course-section audit language is exposed as display copy"
                )
            disclosure_copy = section_ledger.get("associative_anchor_disclosure_copy")
            if disclosure_copy is not None:
                failures.append(
                    "named-course-section audit disclosure copy must stay internal"
                )
            visible_lines = _array(
                _mapping(f1_rendering.get("course_facts")).get("visible_lines")
            )
            if associative_section_count and "SOURCE CENTRELINE" not in visible_lines:
                failures.append(
                    "named-course-section source scope is absent from the information rail"
                )

    visible_names: dict[str, str] = {}
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for label_id, label_paths in serialized.items():
        placement = placement_records.get(label_id)
        if placement is None or not label_paths:
            continue
        feature_id = str(placement.get("feature_id") or "")
        section = sections.get(feature_id)
        if section is None:
            failures.append(
                f"section-label {label_id!r} invents source feature {feature_id!r}"
            )
            continue
        expected_copy = _independent_course_section_label_copy(section)
        if expected_copy is None:
            failures.append(f"section-label {label_id!r} has no drawable sourced name")
            continue
        source_key, source_copy, visible_copy = expected_copy
        if (
            placement.get("source_name_key") != source_key
            or placement.get("source_copy") != source_copy
            or placement.get("copy") != visible_copy
        ):
            failures.append(f"section-label {label_id!r} source copy drifted")
        folded = visible_copy.casefold()
        if folded in visible_names:
            failures.append(
                f"section-label names {visible_names[folded]!r} and {label_id!r} duplicate"
            )
        visible_names[folded] = label_id
        official_name = section.get("name_status") == FAMOUS_SECTION_NAME_STATUS
        expected_attributes = {
            "data-feature-id": feature_id,
            "data-source-name-key": source_key,
            "data-source-copy": source_copy,
            "data-visible-copy": visible_copy,
            "data-name-status": str(section.get("name_status") or ""),
            "data-official-course-name": str(official_name).lower(),
            "data-claim-scope": str(section.get("claim_scope") or ""),
        }
        if official_name:
            anchor_identities = "|".join(
                sorted(
                    {
                        _source_object_identity(value)
                        for value in _array(section.get("source_objects"))
                    }
                )
            )
            expected_attributes.update(
                {
                    "data-source-ref": str(section.get("name_source_ref") or ""),
                    "data-name-source-ref": str(section.get("name_source_ref") or ""),
                    "data-anchor-source-ref": str(
                        section.get("anchor_source_ref") or ""
                    ),
                    "data-anchor-source-object-id": anchor_identities,
                    "data-anchor-mode": str(section.get("anchor_mode") or ""),
                    "data-anchor-status": FAMOUS_SECTION_ANCHOR_STATUS,
                    "data-course-section-priority": str(section.get("priority")),
                }
            )
            expected_placement_lineage = {
                "name_status": FAMOUS_SECTION_NAME_STATUS,
                "official_course_name": True,
                "name_source_ref": str(section.get("name_source_ref") or ""),
                "anchor_source_ref": str(section.get("anchor_source_ref") or ""),
                "anchor_source_object_id": anchor_identities,
                "anchor_mode": str(section.get("anchor_mode") or ""),
                "anchor_status": FAMOUS_SECTION_ANCHOR_STATUS,
                "course_section_priority": section.get("priority"),
                "claim_scope": str(section.get("claim_scope") or ""),
            }
            if any(
                placement.get(key) != value
                for key, value in expected_placement_lineage.items()
            ):
                failures.append(
                    f"section-label {label_id!r} manifest dual lineage drifted"
                )
        elif any(
            key in path.element.attrib
            for path in label_paths
            for key in (
                "data-name-source-ref",
                "data-anchor-source-ref",
                "data-anchor-source-object-id",
                "data-anchor-mode",
                "data-anchor-status",
                "data-course-section-priority",
            )
        ):
            failures.append(
                f"section-label {label_id!r} adds official dual lineage to an OSM name"
            )
        if any(
            path.element.get(key) != value
            for path in label_paths
            for key, value in expected_attributes.items()
        ):
            failures.append(f"section-label {label_id!r} source/name lineage drifted")
        feature_ids = {
            value
            for value in str(
                label_paths[0].element.get("data-source-feature-ids") or ""
            ).split("|")
            if value
        }
        if feature_id not in feature_ids or not feature_ids <= set(sections):
            failures.append(f"section-label {label_id!r} fragment lineage drifted")
        box = _parse_label_box(label_paths[0].element.get("data-label-box"))
        if box is not None:
            boxes[label_id] = box

    separation = _finite(section_ledger.get("minimum_section_label_separation_mm"))
    if len(boxes) > 1:
        if separation is None or separation <= 0.0:
            failures.append("named-course-section separation ledger is absent")
        else:
            box_items = sorted(boxes.items())
            for index, (left_id, left_box) in enumerate(box_items):
                for right_id, right_box in box_items[index + 1 :]:
                    if _rect_distance(left_box, right_box) + 1e-9 < separation:
                        failures.append(
                            f"section labels {left_id!r} and {right_id!r} are below "
                            f"the {separation:g} mm separation floor"
                        )
    return failures


def _line_has_nonadjacent_self_intersection(
    points: Sequence[tuple[float, float]],
) -> bool:
    return bool(_nonadjacent_self_intersections(points))


def _nonadjacent_self_intersections(
    points: Sequence[tuple[float, float]],
) -> list[tuple[int, int, tuple[float, float]]]:
    """Return independently measured, deduplicated non-adjacent crossings."""

    segments = list(zip(points, points[1:]))
    result: list[tuple[int, int, tuple[float, float]]] = []
    for left_index, (a, b) in enumerate(segments):
        for right_index, (c, d) in enumerate(
            segments[left_index + 1 :], left_index + 1
        ):
            if right_index == left_index + 1:
                continue
            if left_index == 0 and right_index == len(segments) - 1:
                continue
            point = _proper_segment_intersection_point(a, b, c, d)
            if point is None:
                continue
            if any(
                math.hypot(point[0] - existing[2][0], point[1] - existing[2][1]) <= 1e-6
                for existing in result
            ):
                continue
            result.append((left_index, right_index, point))
    return result


def _is_contiguous_cyclic_lap_subsequence(
    section_points: Sequence[tuple[float, float]],
    lap_points: Sequence[tuple[float, float]],
) -> bool:
    base = list(lap_points)
    if len(base) > 1 and base[0] == base[-1]:
        base = base[:-1]
    if len(section_points) < 2 or len(section_points) > len(base):
        return False

    def matches(candidate: Sequence[tuple[float, float]]) -> bool:
        for start in range(len(base)):
            if all(
                math.hypot(
                    base[(start + offset) % len(base)][0] - point[0],
                    base[(start + offset) % len(base)][1] - point[1],
                )
                <= 1e-6
                for offset, point in enumerate(candidate)
            ):
                return True
        return False

    return matches(section_points) or matches(tuple(reversed(section_points)))


def _overpass_source_evidence_failures(
    section: Mapping[str, Any],
    model: Mapping[str, Any],
    lap: Sequence[tuple[float, float]],
) -> list[str]:
    failures: list[str] = []
    source_objects = _array(section.get("source_objects"))
    source_object = _mapping(source_objects[0]) if len(source_objects) == 1 else {}
    source_id = source_object.get("id")
    if (
        len(source_objects) != 1
        or source_object.get("type") != "way"
        or not isinstance(source_id, int)
        or isinstance(source_id, bool)
        or str(section.get("source_object_id") or "") != str(source_id)
    ):
        failures.append("does not bind exactly one declared OSM way")

    source_tags = _mapping(source_object.get("tags"))
    section_tags = _mapping(section.get("tags"))
    if section_tags != source_tags:
        failures.append("section tags do not exactly match the embedded source way")
    if str(source_tags.get("bridge") or "").casefold() not in {
        "yes",
        "true",
        "1",
    }:
        failures.append("embedded source way has no affirmative bridge tag")
    try:
        source_layer = float(str(source_tags.get("layer")))
    except (TypeError, ValueError):
        source_layer = 0.0
    if not math.isfinite(source_layer) or abs(source_layer) <= 1e-12:
        failures.append("embedded source way has no non-zero layer tag")

    matching_lap_objects = [
        value
        for raw in _array(model.get("lap_source_objects"))
        if (value := _mapping(raw)).get("type") == "way"
        and value.get("id") == source_id
    ]
    if len(matching_lap_objects) != 1 or matching_lap_objects[0] != source_object:
        failures.append("embedded source way is not exact selected-lap lineage")
    if source_id not in _array(_mapping(model.get("assembly")).get("used_way_ids")):
        failures.append("embedded source way is absent from assembled lap membership")

    lap_record = _mapping(model.get("lap"))
    lap_properties = (
        _mapping(lap_record.get("properties"))
        if lap_record.get("type") == "Feature"
        else lap_record
    )
    if section.get("source_ref") != lap_properties.get("source_ref"):
        failures.append("source_ref differs from the selected lap source")
    section_points = _geojson_coordinates(section.get("geometry"))
    if not _is_contiguous_cyclic_lap_subsequence(section_points, lap):
        failures.append("geometry is not a contiguous selected-lap source run")
    return failures


def _source_to_paper_point(
    point: tuple[float, float], paper: Mapping[str, Any]
) -> tuple[float, float] | None:
    bounds = _array(paper.get("source_bounds_m"))
    rect = _mapping(paper.get("working_rect_mm"))
    scale = _finite(paper.get("scale_mm_per_m"))
    if len(bounds) != 4 or scale is None or scale <= 0.0:
        return None
    source = [_finite(value) for value in bounds]
    x = _finite(rect.get("x"))
    y = _finite(rect.get("y"))
    width = _finite(rect.get("width"))
    height = _finite(rect.get("height"))
    if any(value is None for value in [*source, x, y, width, height]):
        return None
    min_x, min_y, max_x, max_y = (float(value) for value in source)
    assert x is not None and y is not None and width is not None and height is not None
    used_width = (max_x - min_x) * scale
    used_height = (max_y - min_y) * scale
    offset_x = x + (width - used_width) / 2.0
    offset_y = y + (height - used_height) / 2.0
    return (
        offset_x + (point[0] - min_x) * scale,
        offset_y + (max_y - point[1]) * scale,
    )


@dataclass(frozen=True, slots=True)
class _LapProximityConflict:
    """One independently measured non-local course-corridor conflict."""

    left_segment_index: int
    right_segment_index: int
    cyclic_separation_fraction: float
    source_clearance_m: float
    paper_clearance_mm: float
    nominal_edge_gap_mm: float
    connector_start_mm: tuple[float, float]
    connector_end_mm: tuple[float, float]
    midpoint_mm: tuple[float, float]


def _project_point_to_segment(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> tuple[float, float]:
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    square = dx * dx + dy * dy
    if square <= 1e-18:
        return first
    position = ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / square
    position = min(1.0, max(0.0, position))
    return (first[0] + position * dx, first[1] + position * dy)


def _proper_segment_intersection_point(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> tuple[float, float] | None:
    """Return a unique segment intersection, including shared endpoints.

    Collinear overlap has no unique crossing point and therefore returns the
    lexicographically first shared endpoint.  The frozen F1 laps currently use
    only proper crossings; the deterministic fallback keeps adversarial QA
    well-defined without importing the renderer's geometry implementation.
    """

    if not _segments_intersect(a, b, c, d):
        return None
    first_vector = (b[0] - a[0], b[1] - a[1])
    second_vector = (d[0] - c[0], d[1] - c[1])
    denominator = (
        first_vector[0] * second_vector[1] - first_vector[1] * second_vector[0]
    )
    if abs(denominator) > 1e-12:
        offset = (c[0] - a[0], c[1] - a[1])
        position = (
            offset[0] * second_vector[1] - offset[1] * second_vector[0]
        ) / denominator
        return (
            a[0] + position * first_vector[0],
            a[1] + position * first_vector[1],
        )
    shared = sorted(
        point
        for point in {a, b, c, d}
        if _point_segment_distance(point, a, b) <= 1e-9
        and _point_segment_distance(point, c, d) <= 1e-9
    )
    return shared[0] if shared else None


def _segment_closest_points(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float], float]:
    intersection = _proper_segment_intersection_point(a, b, c, d)
    if intersection is not None:
        return intersection, intersection, 0.0
    candidates = (
        (a, _project_point_to_segment(a, c, d)),
        (b, _project_point_to_segment(b, c, d)),
        (_project_point_to_segment(c, a, b), c),
        (_project_point_to_segment(d, a, b), d),
    )
    left, right = min(
        candidates,
        key=lambda pair: (
            math.hypot(pair[0][0] - pair[1][0], pair[0][1] - pair[1][1]),
            pair,
        ),
    )
    return left, right, math.hypot(left[0] - right[0], left[1] - right[1])


def _lap_segment_midpoint_chainages(
    points: Sequence[tuple[float, float]],
) -> tuple[list[float], float]:
    chainages: list[float] = []
    total = 0.0
    for first, second in zip(points, points[1:]):
        length = math.hypot(second[0] - first[0], second[1] - first[1])
        chainages.append(total + length / 2.0)
        total += length
    return chainages, total


def _independent_local_corridor_conflicts(
    event: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
) -> list[_LapProximityConflict]:
    """Recompute every distant lap-leg conflict from source and page geometry.

    A close approach is considered non-local only when the segment midpoints
    are at least one quarter of a lap apart in either cyclic direction.  This
    excludes densely sampled bends and hairpins while retaining the actual
    Suzuka figure-eight and Singapore's visually crossing distant legs.
    """

    lap = _geojson_coordinates(_model(event).get("lap"))
    paper = _mapping(f1_rendering.get("paper_adaptation"))
    ledger = _mapping(f1_rendering.get("diagrammatic_course_corridor"))
    scale = _finite(paper.get("scale_mm_per_m"))
    target = _finite(ledger.get("target_width_mm"))
    if len(lap) < 4 or scale is None or scale <= 0.0 or target is None:
        return []
    chainages, total_length = _lap_segment_midpoint_chainages(lap)
    if total_length <= 0.0:
        return []
    segments = list(zip(lap, lap[1:]))
    result: list[_LapProximityConflict] = []
    for left_index, (a, b) in enumerate(segments):
        for right_index in range(left_index + 1, len(segments)):
            if right_index == left_index + 1:
                continue
            if left_index == 0 and right_index == len(segments) - 1:
                continue
            chainage_delta = abs(chainages[right_index] - chainages[left_index])
            cyclic_delta = min(chainage_delta, total_length - chainage_delta)
            cyclic_fraction = cyclic_delta / total_length
            if (
                cyclic_fraction + 1e-12
                < LOCAL_CORRIDOR_MINIMUM_CYCLIC_SEPARATION_FRACTION
            ):
                continue
            c, d = segments[right_index]
            left_source, right_source, source_clearance = _segment_closest_points(
                a, b, c, d
            )
            paper_clearance = source_clearance * scale
            nominal_edge_gap = paper_clearance - target
            if nominal_edge_gap + 1e-9 >= LOCAL_CORRIDOR_MINIMUM_SAFE_EDGE_GAP_MM:
                continue
            left_paper = _source_to_paper_point(left_source, paper)
            right_paper = _source_to_paper_point(right_source, paper)
            if left_paper is None or right_paper is None:
                continue
            result.append(
                _LapProximityConflict(
                    left_segment_index=left_index,
                    right_segment_index=right_index,
                    cyclic_separation_fraction=cyclic_fraction,
                    source_clearance_m=source_clearance,
                    paper_clearance_mm=paper_clearance,
                    nominal_edge_gap_mm=nominal_edge_gap,
                    connector_start_mm=left_paper,
                    connector_end_mm=right_paper,
                    midpoint_mm=(
                        (left_paper[0] + right_paper[0]) / 2.0,
                        (left_paper[1] + right_paper[1]) / 2.0,
                    ),
                )
            )
    return sorted(
        result,
        key=lambda item: (
            item.left_segment_index,
            item.right_segment_index,
            item.source_clearance_m,
        ),
    )


def _framing_failures(
    event: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    failures: list[str] = []
    model = _model(event)
    lap = _geojson_coordinates(model.get("lap"))
    structural_points = list(lap)
    for value in _array(model.get("pit_lanes")):
        structural_points.extend(_geojson_coordinates(_geometry_value(_mapping(value))))
    paper = _mapping(f1_rendering.get("paper_adaptation"))
    if not structural_points:
        return ["framing has no independently measurable lap/pit geometry"]
    expected_structural = [
        min(point[0] for point in structural_points),
        min(point[1] for point in structural_points),
        max(point[0] for point in structural_points),
        max(point[1] for point in structural_points),
    ]
    declared_structural = _array(paper.get("structural_source_bounds_m"))
    if len(declared_structural) != 4 or any(
        _finite(value) is None or abs(float(value) - expected) > 1e-6
        for value, expected in zip(
            declared_structural, expected_structural, strict=False
        )
    ):
        failures.append("framing structural bounds are not exact lap-plus-pit bounds")
    if paper.get("framing_source_scope") != (
        "lap-plus-pit-only-unqualified-boundaries-excluded-v2"
    ):
        failures.append("framing does not exclude unqualified raw boundaries")
    if paper.get("framing_fit_policy") != (
        "maximum-safe-contain-no-geographic-margin-v1"
    ):
        failures.append("framing does not declare maximum safe course fitting")
    boundary_count = len(_array(model.get("track_boundaries")))
    if (
        paper.get("unqualified_raw_boundary_count_excluded_from_framing")
        != boundary_count
    ):
        failures.append("framing raw-boundary exclusion count drifted")
    gate = _mapping(paper.get("sheet_gate"))
    declared_bounds = _array(paper.get("source_bounds_m"))
    if len(declared_bounds) != 4 or any(
        _finite(value) is None or abs(float(value) - expected) > 1e-5
        for value, expected in zip(
            declared_bounds, expected_structural, strict=False
        )
    ):
        failures.append("framing source bounds are not exact lap-plus-pit bounds")
    clearance = _finite(gate.get("field_padding_mm"))
    if clearance is None or clearance <= 0.0:
        failures.append("framing has no positive physical course-edge clearance")
    elif abs(float(paper.get("course_edge_clearance_mm", -1.0)) - clearance) > 1e-9:
        failures.append("framing course-edge clearance ledger drifted")
    lap_paths = [path for path in paths if path.role == "lap-centreline"]
    rect = _mapping(paper.get("working_rect_mm"))
    if len(lap_paths) == 1:
        rect_x = _finite(rect.get("x"))
        rect_y = _finite(rect.get("y"))
        width = _finite(rect.get("width"))
        height = _finite(rect.get("height"))
        if (
            rect_x is None
            or rect_y is None
            or width is None
            or height is None
            or min(width, height) <= 0.0
        ):
            failures.append("framing working rectangle is invalid")
        else:
            bounds = lap_paths[0].bounds
            expected_width = (bounds[2] - bounds[0]) / width
            expected_height = (bounds[3] - bounds[1]) / height
            if (
                abs(float(paper.get("hero_width_utilization", -1.0)) - expected_width)
                > 2e-5
            ):
                failures.append("framing hero-width utilization ledger drifted")
            if (
                abs(float(paper.get("hero_height_utilization", -1.0)) - expected_height)
                > 2e-5
            ):
                failures.append("framing hero-height utilization ledger drifted")
            if len(declared_bounds) == 4 and all(
                _finite(value) is not None for value in declared_bounds
            ):
                min_x, min_y, max_x, max_y = (float(value) for value in declared_bounds)
                span_x = max_x - min_x
                span_y = max_y - min_y
                if min(span_x, span_y) <= 0.0:
                    failures.append("framing padded source extent is degenerate")
                else:
                    expected_scale = min(width / span_x, height / span_y)
                    declared_scale = _finite(paper.get("scale_mm_per_m"))
                    if (
                        declared_scale is None
                        or abs(declared_scale - expected_scale) > 2e-8
                    ):
                        failures.append(
                            "framing scale is not the exact fit of structural source bounds"
                        )
                    expected_denominator = round(1000.0 / expected_scale)
                    if (
                        paper.get("approximate_scale_denominator")
                        != expected_denominator
                    ):
                        failures.append("framing approximate scale denominator drifted")

                    used_width = span_x * expected_scale
                    used_height = span_y * expected_scale
                    expected_width_utilization = used_width / width
                    expected_height_utilization = used_height / height
                    if (
                        abs(
                            float(paper.get("structural_width_utilization", -1.0))
                            - expected_width_utilization
                        )
                        > 2e-8
                    ):
                        failures.append(
                            "framing structural-width utilization ledger drifted"
                        )
                    if (
                        abs(
                            float(paper.get("structural_height_utilization", -1.0))
                            - expected_height_utilization
                        )
                        > 2e-8
                    ):
                        failures.append(
                            "framing structural-height utilization ledger drifted"
                        )
                    expected_maximum_utilization = max(
                        expected_width_utilization, expected_height_utilization
                    )
                    if (
                        abs(
                            float(paper.get("maximum_safe_axis_utilization", -1.0))
                            - expected_maximum_utilization
                        )
                        > 2e-8
                        or abs(expected_maximum_utilization - 1.0) > 2e-8
                    ):
                        failures.append(
                            "framing does not fill one axis of the maximum safe rectangle"
                        )
                    translate_x = (
                        rect_x + (width - used_width) / 2.0 - min_x * expected_scale
                    )
                    translate_y = (
                        rect_y + (height - used_height) / 2.0 + max_y * expected_scale
                    )
                    if clearance is not None and clearance > 0.0:
                        expected_viewport = (
                            (rect_x - clearance - translate_x) / expected_scale,
                            (
                                translate_y
                                - (rect_y + height + clearance)
                            )
                            / expected_scale,
                            (rect_x + width + clearance - translate_x)
                            / expected_scale,
                            (translate_y - (rect_y - clearance)) / expected_scale,
                        )
                        declared_viewport = _array(
                            paper.get("context_viewport_source_bounds_m")
                        )
                        if len(declared_viewport) != 4 or any(
                            _finite(value) is None
                            or abs(float(value) - expected) > 1e-5
                            for value, expected in zip(
                                declared_viewport,
                                expected_viewport,
                                strict=False,
                            )
                        ):
                            failures.append(
                                "framing context viewport does not map to the full field"
                            )
                    source_lap = _geojson_coordinates(model.get("lap"))
                    rendered_lap = (
                        lap_paths[0].subpaths[0].points
                        if len(lap_paths[0].subpaths) == 1
                        else ()
                    )
                    if len(rendered_lap) != len(source_lap):
                        failures.append(
                            "framing rendered lap coordinate count differs from source"
                        )
                    else:
                        expected_lap = [
                            (
                                translate_x + expected_scale * point[0],
                                translate_y - expected_scale * point[1],
                            )
                            for point in source_lap
                        ]
                        if any(
                            math.hypot(
                                actual[0] - expected[0],
                                actual[1] - expected[1],
                            )
                            > 0.003
                            for actual, expected in zip(
                                rendered_lap, expected_lap, strict=True
                            )
                        ):
                            failures.append(
                                "framing rendered lap does not match independent "
                                "source-to-paper transform"
                            )

                    declared_hero_bounds = _array(paper.get("hero_bounds_mm"))
                    if len(declared_hero_bounds) != 4 or any(
                        _finite(value) is None or abs(float(value) - expected) > 0.003
                        for value, expected in zip(
                            declared_hero_bounds,
                            lap_paths[0].bounds,
                            strict=False,
                        )
                    ):
                        failures.append("framing hero bounds ledger drifted")
    return failures


def _visible_attribution_copy_integrity_failures(
    expected_copy: str,
    paths: Sequence[PathEvidence],
) -> list[str]:
    """Bind visible disclosure copy to its complete plotted stroke inventory.

    ``data-copy`` alone is not evidence that the words remain visible: one
    surviving glyph could retain that attribute after the rest of the line was
    deleted.  The QA therefore derives the exact component count directly from
    the frozen stroke font and measures the union of the serialized SVG paths.
    """

    matching = [
        path
        for path in paths
        if path.role in {"attribution", "attribution-disclosure"}
        and path.element.get("data-copy") == expected_copy
    ]
    if not matching:
        return ["serialized SVG attribution ink is missing the exact copy"]
    expected_path_count = len(
        stroke_text(
            expected_copy,
            x_mm=0.0,
            y_mm=0.0,
            height_mm=1.0,
            anchor="start",
        )
    )
    failures: list[str] = []
    if len(matching) != expected_path_count:
        failures.append(
            "serialized SVG attribution ink has an incomplete stroke inventory "
            f"({len(matching)} != {expected_path_count})"
        )
    indices: list[int] = []
    for path in matching:
        raw_index = path.element.get("data-copy-stroke-index")
        try:
            index = int(raw_index) if raw_index is not None else -1
        except ValueError:
            index = -1
        indices.append(index)
        if path.element.get("data-copy-stroke-count") != str(expected_path_count):
            failures.append(
                "serialized SVG attribution ink stroke-count binding drifted"
            )
            break
    if sorted(indices) != list(range(expected_path_count)):
        failures.append("serialized SVG attribution ink stroke-index inventory drifted")
    ordered = [
        path
        for _index, path in sorted(
            zip(indices, matching, strict=True), key=lambda pair: pair[0]
        )
    ]
    serialized = "\n".join(str(path.element.get("d") or "") for path in ordered)
    actual_geometry_sha256 = hashlib.sha256(serialized.encode("ascii")).hexdigest()
    stored_geometry_digests = {
        str(path.element.get("data-copy-geometry-sha256") or "") for path in matching
    }
    if stored_geometry_digests != {actual_geometry_sha256}:
        failures.append(
            "serialized SVG attribution ink disagrees with its ordered geometry digest"
        )
    bounds = (
        min(path.bounds[0] for path in matching),
        min(path.bounds[1] for path in matching),
        max(path.bounds[2] for path in matching),
        max(path.bounds[3] for path in matching),
    )
    nib = max(path.nib_mm for path in matching)
    if (
        bounds[2] - bounds[0] + 1e-9 < 3.0 * nib
        or bounds[3] - bounds[1] + 1e-9 < 3.0 * nib
    ):
        failures.append("serialized SVG attribution ink has degenerate physical extent")
    return failures


def _grandstand_independent_selection(
    source_stands: Mapping[str, Mapping[str, Any]],
    paper: Mapping[str, Any],
) -> tuple[list[str], dict[str, str], list[str]]:
    """Recompute the deterministic stand gate from source geometry and format."""

    failures: list[str] = []
    raw_bounds = _array(paper.get("context_viewport_source_bounds_m"))
    if len(raw_bounds) != 4 or any(_finite(value) is None for value in raw_bounds):
        return [], {}, ["grandstand selection has no valid source viewport"]
    viewport = tuple(float(value) for value in raw_bounds)
    if viewport[0] > viewport[2] or viewport[1] > viewport[3]:
        return [], {}, ["grandstand selection source viewport is inverted"]
    format_id = str(paper.get("format_id") or "")
    sheet = format_id.split("-", 1)[0].upper()
    limit = GRANDSTAND_FEATURE_LIMIT_BY_SHEET.get(sheet)
    if limit is None:
        return [], {}, ["grandstand selection format has no fixed sheet limit"]

    eligible: list[tuple[int, str]] = []
    expected_omission_reason: dict[str, str] = {}
    epsilon = 1e-6
    for feature_id, feature in source_stands.items():
        if not _is_frozen_current_osm_grandstand(feature):
            continue
        bounds = _geojson_coordinate_bounds(feature.get("geometry"))
        if bounds is None:
            failures.append(
                f"source grandstand {feature_id!r} has no measurable geometry"
            )
            continue
        intersects = not (
            bounds[2] < viewport[0] - epsilon
            or bounds[0] > viewport[2] + epsilon
            or bounds[3] < viewport[1] - epsilon
            or bounds[1] > viewport[3] + epsilon
        )
        if not intersects:
            expected_omission_reason[feature_id] = "outside-atlas-extent"
            continue
        label = _independent_context_label_copy(feature)
        meaningful = (
            label is not None
            and len(label[2].strip()) >= 3
            and not label[2].strip().isdigit()
        )
        eligible.append((0 if meaningful else 1, feature_id))

    eligible.sort()
    expected_selected = sorted(feature_id for _named, feature_id in eligible[:limit])
    for _named, feature_id in eligible[limit:]:
        expected_omission_reason[feature_id] = "paper-feature-count-gate"
    return expected_selected, expected_omission_reason, failures


def _track_boundary_density_omission_failures(
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    """Verify the exceptional all-or-none Grey edge hard-gate fallback."""

    failures: list[str] = []
    context = _mapping(f1_rendering.get("context_features"))
    density = _mapping(context.get("context_density_budget"))
    decisions = [
        record
        for raw in _array(density.get("decisions"))
        if (record := _mapping(raw)).get("reason")
        == "paper-field-density-budget-qualified-track-boundary-group"
    ]
    omissions = [
        record
        for raw in _array(f1_rendering.get("feature_omissions"))
        if (record := _mapping(raw)).get("reason")
        == "paper-field-density-budget-qualified-track-boundary-group"
    ]
    qualifications = [
        record
        for raw in _array(context.get("track_boundary_qualifications"))
        if (record := _mapping(raw)).get("boundary_index") is not None
    ]
    ledger_indices = {
        int(record["boundary_index"])
        for record in qualifications
        if record.get("resolvable") is True
        and record.get("emission_status") == "omitted-paper-density-budget"
    }
    if not decisions and not omissions and not ledger_indices:
        return []
    if len(decisions) != 1 or len(omissions) != 1 or decisions[0] != omissions[0]:
        return ["track-boundary density fallback is not one exact decision/omission"]
    decision = decisions[0]
    raw_indices = _array(decision.get("boundary_indices"))
    if (
        decision.get("feature_id") != "qualified-track-boundary-group"
        or decision.get("kind") != "track-boundary"
        or decision.get("whole_feature_group") is not True
        or decision.get("all_or_nothing_group") is not True
        or decision.get("red_course_geometry_retained") is not True
        or not raw_indices
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in raw_indices
        )
    ):
        failures.append("track-boundary density fallback contract is incomplete")
    decision_indices = {int(value) for value in raw_indices if isinstance(value, int)}
    resolvable_indices = {
        int(record["boundary_index"])
        for record in qualifications
        if record.get("resolvable") is True
    }
    if decision_indices != ledger_indices or decision_indices != resolvable_indices:
        failures.append(
            "track-boundary density fallback is not the full qualified group"
        )
    if any(path.role == "track-boundary" for path in paths):
        failures.append("density-omitted track-boundary group still emits Grey ink")
    if len([path for path in paths if path.role == "lap-centreline"]) != 1:
        failures.append("density-omitted track boundary does not retain one Red lap")

    initial = _finite(density.get("initial_baseline_length_mm"))
    retained = _finite(density.get("retained_baseline_length_mm"))
    hard_maximum = _finite(density.get("hard_maximum_baseline_length_mm"))
    boundary_removed = _finite(decision.get("removed_length_mm"))
    removed_values = [
        _finite(_mapping(raw).get("removed_length_mm"))
        for raw in _array(density.get("decisions"))
    ]
    if (
        initial is None
        or retained is None
        or hard_maximum is None
        or boundary_removed is None
        or boundary_removed <= 0.0
        or any(value is None for value in removed_values)
        or retained > hard_maximum + 1e-6
        or retained + boundary_removed <= hard_maximum + 1e-6
        or abs(
            initial
            - sum(float(value) for value in removed_values if value is not None)
            - retained
        )
        > 0.01
    ):
        failures.append("track-boundary density fallback hard-gate arithmetic drifted")
    return failures


def _grandstand_observation_failures(
    event: Mapping[str, Any],
    manifest: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    failures: list[str] = []
    source_stands = {
        str(value.get("id")): value
        for raw in _array(_model(event).get("context"))
        if (value := _mapping(raw)).get("kind") == "grandstand" and value.get("id")
    }
    context = _mapping(f1_rendering.get("context_features"))
    ledger = _mapping(context.get("grandstand_observation"))
    stand_paths = [path for path in paths if path.role == "context-grandstand"]
    source_ids = sorted(source_stands)
    emitted_ids = sorted(
        {
            str(path.element.get("data-feature-id") or "")
            for path in stand_paths
            if path.element.get("data-feature-id")
        }
    )
    expected_ledger = {
        "policy": "frozen-current-osm-footprint-only-v1",
        "source_record_count": len(source_stands),
        "emitted_feature_count": len(emitted_ids),
        "emitted_path_count": len(stand_paths),
        "source_feature_ids": source_ids,
        "emitted_feature_ids": emitted_ids,
        "partition_policy": (
            "source=selected+unselected; selected=emitted+culled; "
            "every non-emitted id requires feature_omissions evidence"
        ),
        "claim_scope": CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE,
        "event_configuration_verified": False,
        "fia_configuration_claimed": False,
        "operational_semantics_claimed": False,
    }
    for key, expected in expected_ledger.items():
        if ledger.get(key) != expected:
            failures.append(f"grandstand observation ledger {key!r} drifted")
    for source_id, feature in source_stands.items():
        if not _is_frozen_current_osm_grandstand(feature):
            failures.append(
                f"source grandstand {source_id!r} weakens its exact OSM tag/claim scope"
            )

    paper = _mapping(f1_rendering.get("paper_adaptation"))
    expected_selected_ids, expected_selection_omissions, selection_failures = (
        _grandstand_independent_selection(source_stands, paper)
    )
    failures.extend(selection_failures)

    def ledger_ids(key: str) -> list[str] | None:
        values = _array(ledger.get(key))
        if any(
            not isinstance(value, str) or not value for value in values
        ) or values != sorted(set(values)):
            failures.append(f"grandstand observation ledger {key!r} is invalid")
            return None
        return values

    selected_ids = ledger_ids("selected_feature_ids")
    unselected_ids = ledger_ids("source_unselected_feature_ids")
    culled_ids = ledger_ids("culled_feature_ids")
    selected = ledger.get("selected_feature_count")
    if (
        not isinstance(selected, int)
        or isinstance(selected, bool)
        or not 0 <= selected <= len(source_stands)
    ):
        failures.append("grandstand selected-feature count is invalid")
    elif selected_ids is not None and selected != len(selected_ids):
        failures.append("grandstand selected-feature count/id ledger drifted")
    if selected_ids is not None and selected_ids != expected_selected_ids:
        failures.append(
            "grandstand selected IDs disagree with independent viewport, priority, "
            "and fixed sheet-limit selection"
        )
    if selected and not stand_paths and not culled_ids:
        failures.append("selected frozen grandstands emitted no visible footprint")
    if selected_ids is not None and unselected_ids is not None:
        if (
            set(selected_ids).intersection(unselected_ids)
            or sorted({*selected_ids, *unselected_ids}) != source_ids
        ):
            failures.append("grandstand source/selected/unselected partition drifted")
    if selected_ids is not None and culled_ids is not None:
        if (
            set(emitted_ids).intersection(culled_ids)
            or sorted({*emitted_ids, *culled_ids}) != selected_ids
        ):
            failures.append("grandstand selected/emitted/culled partition drifted")
    input_counts = _mapping(context.get("input_counts_by_kind"))
    selected_counts = _mapping(context.get("selected_counts_by_kind"))
    emitted_counts = _mapping(context.get("emitted_path_counts_by_kind"))
    if input_counts.get("grandstand", 0) != len(source_ids):
        failures.append("grandstand input count is not source-exact")
    if selected_ids is not None and selected_counts.get("grandstand", 0) != len(
        selected_ids
    ):
        failures.append("grandstand selected context count/id ledger drifted")
    if emitted_counts.get("grandstand", 0) != len(stand_paths):
        failures.append("grandstand emitted context path count drifted")
    omissions_by_id: dict[str, set[str]] = {}
    for raw in _array(f1_rendering.get("feature_omissions")):
        value = _mapping(raw)
        if value.get("feature_id") and value.get("reason"):
            omissions_by_id.setdefault(str(value["feature_id"]), set()).add(
                str(value["reason"])
            )
    omitted_ids = {
        str(value.get("feature_id"))
        for raw in _array(f1_rendering.get("feature_omissions"))
        if (value := _mapping(raw)).get("feature_id") and value.get("reason")
    }
    for feature_id in sorted({*(unselected_ids or []), *(culled_ids or [])}):
        if feature_id not in omitted_ids:
            failures.append(
                f"non-emitted grandstand {feature_id!r} has no explicit omission"
            )
    emitted_id_set = set(emitted_ids)
    expected_selected_set = set(expected_selected_ids)
    if not emitted_id_set.issubset(expected_selected_set):
        failures.append("SVG emitted a grandstand outside independent selection")
    allowed_selected_omissions = {
        "paper-field-density-budget-source-feature",
        "post-clip-below-three-nib-floor",
    }
    selection_stage_reasons = {
        "outside-atlas-extent",
        "paper-feature-count-gate",
    }
    for feature_id in expected_selected_ids:
        reasons = omissions_by_id.get(feature_id, set())
        if reasons.intersection(selection_stage_reasons):
            failures.append(
                f"independently selected grandstand {feature_id!r} has a false "
                "selection-stage omission"
            )
        if feature_id not in emitted_id_set and not reasons.intersection(
            allowed_selected_omissions
        ):
            failures.append(
                f"independently selected grandstand {feature_id!r} is neither "
                "emitted nor explicitly density/geometry-omitted"
            )
    for feature_id, expected_reason in expected_selection_omissions.items():
        if expected_reason not in omissions_by_id.get(feature_id, set()):
            failures.append(
                f"independently unselected grandstand {feature_id!r} lacks exact "
                f"{expected_reason!r} evidence"
            )
    for path in stand_paths:
        feature_id = str(path.element.get("data-feature-id") or "")
        feature = source_stands.get(feature_id)
        if feature is None or not _is_frozen_current_osm_grandstand(feature):
            failures.append("SVG grandstand is not a frozen current-OSM observation")
            continue
        if path.pen_id != "purple-0-4" or path.ink.casefold() != "purple":
            failures.append("SVG grandstand does not use the Purple 0.4 venue pen")
        expected_attributes = {
            "data-context-temporality": CURRENT_OSM_GRANDSTAND_TEMPORALITY,
            "data-claim-scope": CURRENT_OSM_GRANDSTAND_CLAIM_SCOPE,
            "data-event-configuration-verified": "false",
            "data-fia-configuration-claimed": "false",
            "data-operational-semantics-claimed": "false",
            "data-valid-for-season": "withheld",
            "data-white-ink": "false",
        }
        if any(
            path.element.get(key) != value for key, value in expected_attributes.items()
        ):
            failures.append(f"SVG grandstand {feature_id!r} claim scope drifted")
    stand_ids = set(source_stands)
    if any(
        path.role == "operational-overlay"
        and str(path.element.get("data-feature-id") or "") in stand_ids
        for path in paths
    ):
        failures.append("frozen grandstand footprint is falsely operational")
    if stand_paths:
        identity = _mapping(event.get("configuration_identity"))
        expected_visible = (
            HISTORIC_CURRENT_STAND_VISIBLE_DISCLOSURE
            if identity.get("status") == "exact-historic-source"
            else CURRENT_OSM_GRANDSTAND_VISIBLE_DISCLOSURE
        )
        if ledger.get("visible_disclosure") != expected_visible:
            failures.append("grandstand visible-disclosure ledger drifted")
        visible_lines = _array(
            _mapping(f1_rendering.get("course_facts")).get("visible_lines")
        )
        if expected_visible not in visible_lines:
            failures.append("grandstand scope is absent from the information rail")
        if not any(
            path.role == "circuit-information-value"
            and path.element.get("data-copy") == expected_visible
            for path in paths
        ):
            failures.append("serialized SVG information rail omits grandstand scope")
    return failures


def _historic_current_context_disclosure_failures(
    event: Mapping[str, Any],
    manifest: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    """Require the exact-historic/current-context caveat in visible copy.

    Legacy geometry may be an exact historic source while the surrounding OSM
    context is the frozen current snapshot.  That temporal mismatch must remain
    visible even when no grandstand footprint survives selection.
    """

    identity = _mapping(event.get("configuration_identity"))
    if identity.get("status") != "exact-historic-source":
        return []
    has_stands = any(path.role == "context-grandstand" for path in paths)
    expected = (
        HISTORIC_CURRENT_STAND_VISIBLE_DISCLOSURE
        if has_stands
        else HISTORIC_CURRENT_CONTEXT_VISIBLE_DISCLOSURE
    )
    f1_rendering = _mapping(_mapping(manifest.get("rendering")).get("f1_circuit"))
    visible_lines = _array(
        _mapping(f1_rendering.get("course_facts")).get("visible_lines")
    )
    if expected not in visible_lines:
        return [
            "exact-historic plate omits the visible current-context temporal caveat"
        ]
    if not any(
        path.role == "circuit-information-value"
        and path.element.get("data-copy") == expected
        for path in paths
    ):
        return ["exact-historic SVG omits the plotted current-context temporal caveat"]
    return []


def _atlas_context_mode_failures(
    event: Mapping[str, Any], f1_rendering: Mapping[str, Any]
) -> list[str]:
    failures: list[str] = []
    frozen = _mapping(event.get("circuit")).get("atlas_context_mode")
    if frozen is None:
        return failures
    paper = _mapping(f1_rendering.get("paper_adaptation"))
    context = _mapping(f1_rendering.get("context_features"))
    if frozen not in CONTEXT_MODES:
        failures.append("catalog atlas_context_mode is unsupported")
    if paper.get("context_mode") != frozen or context.get("mode") != frozen:
        failures.append("rendered context mode drifted from the frozen catalog")
    if (
        paper.get("context_mode_source") != "event.circuit.atlas_context_mode"
        or context.get("mode_source") != "event.circuit.atlas_context_mode"
    ):
        failures.append("rendered context mode is not catalog-sourced")
    if (
        paper.get("context_mode_derived_from_site_type") is not False
        or context.get("mode_derived_from_site_type") is not False
    ):
        failures.append("rendered context mode is falsely derived from site type")
    if (
        paper.get("context_mode_override_applied") is not False
        or context.get("mode_override_applied") is not False
    ):
        failures.append("released artifact applies a context-mode override")
    return failures


def _grade_separation_cue_failures(
    event: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    failures: list[str] = []
    model = _model(event)
    lap = _geojson_coordinates(model.get("lap"))
    intersections = _nonadjacent_self_intersections(lap)
    self_crossing = bool(intersections)
    intersection_sections: dict[str, tuple[Mapping[str, Any], int]] = {}
    claimed_intersections: dict[int, str] = {}
    for raw in _array(model.get("special_sections")):
        section = _mapping(raw)
        if str(section.get("kind") or "").casefold().replace("_", "-") != "overpass":
            continue
        section_id = str(section.get("id") or "")
        evidence_failures = _overpass_source_evidence_failures(section, model, lap)
        if evidence_failures:
            failures.extend(
                f"overpass {section_id!r} {failure}" for failure in evidence_failures
            )
            continue
        section_points = _geojson_coordinates(section.get("geometry"))
        matches = [
            crossing_index
            for crossing_index, (_left, _right, point) in enumerate(intersections)
            if _point_line_distance(point, section_points) <= 1e-6
        ]
        if len(matches) > 1:
            failures.append(
                f"overpass {section_id!r} contains multiple lap self-intersections"
            )
        elif matches:
            crossing_index = matches[0]
            if crossing_index in claimed_intersections:
                failures.append(
                    "one lap self-intersection is claimed by multiple exact "
                    "source-backed overpasses"
                )
            else:
                claimed_intersections[crossing_index] = section_id
                intersection_sections[section_id] = (section, crossing_index)
    unmatched_intersections = set(range(len(intersections))) - set(
        claimed_intersections
    )
    if unmatched_intersections:
        failures.append(
            "one or more lap self-intersections lacks one exact source-backed "
            "selected-lap overpass"
        )
    required = self_crossing and not unmatched_intersections
    topology = _mapping(f1_rendering.get("topology"))
    if (
        topology.get("self_crossing_or_grade_separation_review_required")
        is not self_crossing
    ):
        failures.append("topology self-crossing ledger drifted")
    if topology.get("lap_self_intersection_count") != len(intersections):
        failures.append("topology lap self-intersection count drifted")
    expected_crossing_indexes = [[left, right] for left, right, _point in intersections]
    if (
        _array(topology.get("lap_self_intersection_segment_indexes"))
        != expected_crossing_indexes
    ):
        failures.append("topology lap self-intersection segment ledger drifted")
    if topology.get("grade_separation_cue_required") is not required:
        failures.append("topology grade-separation cue requirement drifted")
    expected_ids = sorted(intersection_sections) if required else []
    if sorted(_array(topology.get("grade_separation_source_section_ids"))) != sorted(
        expected_ids
    ):
        failures.append("topology overpass source-section ledger drifted")
    cues = [path for path in paths if path.role == "grade-separation-cue"]
    if topology.get("grade_separation_cue_emitted_path_count") != len(cues):
        failures.append("topology bridge-cue emitted count drifted")
    if topology.get("grade_separation_cue_policy") != GRADE_SEPARATION_CUE_POLICY:
        failures.append("topology bridge-cue policy drifted")
    if (
        topology.get("red_centreline_modified_for_grade_separation_cue") is not False
        or topology.get("white_ink_used_for_grade_separation_cue") is not False
    ):
        failures.append("topology bridge cue weakens Red/no-white guarantees")
    if not required:
        if cues:
            failures.append("SVG invents a grade-separation cue")
        return failures
    red_paths = [path for path in paths if path.role == "lap-centreline"]
    red_step = (
        int(red_paths[0].group.get("data-pen-step", "0")) if len(red_paths) == 1 else 0
    )
    paper = _mapping(f1_rendering.get("paper_adaptation"))
    cues_by_section: dict[str, list[PathEvidence]] = {}
    for cue in cues:
        cues_by_section.setdefault(
            str(cue.element.get("data-section-id") or ""), []
        ).append(cue)
    if sorted(cues_by_section) != expected_ids:
        failures.append("SVG bridge-cue section parity drifted")
    for section_id in expected_ids:
        section, crossing_index = intersection_sections[section_id]
        section_cues = cues_by_section.get(section_id, [])
        expected_parts = {
            "terminal-start",
            "terminal-end",
            "rail-left",
            "rail-right",
        }
        parts = {
            str(cue.element.get("data-cue-part") or ""): cue for cue in section_cues
        }
        if len(section_cues) != 4 or set(parts) != expected_parts:
            failures.append(f"bridge cue {section_id!r} is not one four-part bracket")
            continue
        coordinates = _geojson_coordinates(section.get("geometry"))
        source_object_ids = {
            _source_object_identity(value)
            for raw in _array(section.get("source_objects"))
            if (value := _mapping(raw)).get("id") is not None
        }
        for cue in section_cues:
            if cue.pen_id != "black-0-25" or cue.ink.casefold() != "black":
                failures.append(f"bridge cue {section_id!r} is not Black 0.25")
            try:
                cue_step = int(str(cue.group.get("data-pen-step") or "0"))
            except ValueError:
                cue_step = 0
            if cue_step <= red_step:
                failures.append(f"bridge cue {section_id!r} is not plotted after Red")
            object_parts = {
                value
                for value in str(cue.element.get("data-source-object-id") or "").split(
                    "|"
                )
                if value
            }
            expected_attributes = {
                "data-feature-id": section_id,
                "data-operational-kind": "overpass",
                "data-cue-policy": GRADE_SEPARATION_CUE_POLICY,
                "data-cartographic-symbol": "true",
                "data-source-geometry-claim": "false",
                "data-surveyed-track-width": "false",
                "data-red-lap-interrupted": "false",
                "data-white-ink": "false",
                "data-self-intersection-segment-indexes": "|".join(
                    str(value) for value in intersections[crossing_index][:2]
                ),
            }
            if (
                object_parts != source_object_ids
                or cue.element.get("data-source-ref") != section.get("source_ref")
                or any(
                    cue.element.get(key) != value
                    for key, value in expected_attributes.items()
                )
            ):
                failures.append(
                    f"bridge cue {section_id!r} source/claim lineage drifted"
                )
            if len(cue.subpaths) != 1 or len(cue.subpaths[0].points) < 2:
                failures.append(f"bridge cue {section_id!r} has invalid path geometry")

        target = _finite(
            _mapping(f1_rendering.get("diagrammatic_course_corridor")).get(
                "target_width_mm"
            )
        )
        expected_half_width = target / 2.0 + 1.5 * 0.25 if target is not None else None
        if expected_half_width is None:
            failures.append(
                f"bridge cue {section_id!r} cannot resolve the corridor width"
            )
            continue
        for cue_end in ("start", "end"):
            cue = parts[f"terminal-{cue_end}"]
            if len(cue.subpaths) != 1 or len(cue.subpaths[0].points) < 2:
                continue
            if abs(cue.length - 2.0 * expected_half_width) > 0.003:
                failures.append(
                    f"bridge cue {section_id!r} terminal width drifted from corridor"
                )
            endpoint_index = 0 if cue_end == "start" else -1
            neighbour_index = 1 if endpoint_index == 0 else -2
            expected_point = _source_to_paper_point(coordinates[endpoint_index], paper)
            expected_neighbour = _source_to_paper_point(
                coordinates[neighbour_index], paper
            )
            points = cue.subpaths[0].points
            midpoint = (
                (points[0][0] + points[-1][0]) / 2.0,
                (points[0][1] + points[-1][1]) / 2.0,
            )
            if (
                expected_point is None
                or math.hypot(
                    midpoint[0] - expected_point[0], midpoint[1] - expected_point[1]
                )
                > 0.003
            ):
                failures.append(
                    f"bridge cue {section_id!r} moved off its exact source endpoint"
                )
            if expected_point is None or expected_neighbour is None:
                failures.append(f"bridge cue {section_id!r} has no measurable tangent")
                continue
            cue_vector = (
                points[-1][0] - points[0][0],
                points[-1][1] - points[0][1],
            )
            tangent = (
                expected_neighbour[0] - expected_point[0],
                expected_neighbour[1] - expected_point[1],
            )
            denominator = math.hypot(*cue_vector) * math.hypot(*tangent)
            perpendicular_error = (
                abs(cue_vector[0] * tangent[0] + cue_vector[1] * tangent[1])
                / denominator
                if denominator > 1e-12
                else 1.0
            )
            if perpendicular_error > 0.002:
                failures.append(
                    f"bridge cue {section_id!r} is not perpendicular to source tangent"
                )

        # The current source-backed crossing is a straight two-point OSM way.
        # Verify the complete bracket independently in paper coordinates; a
        # future curved bridge remains fail-closed until this verifier grows an
        # equally independent offset implementation.
        source_paper = [
            value
            for point in coordinates
            if (value := _source_to_paper_point(point, paper)) is not None
        ]
        if len(source_paper) != len(coordinates) or len(source_paper) != 2:
            failures.append(
                f"bridge cue {section_id!r} has unsupported non-straight source geometry"
            )
            continue
        start, end = source_paper
        tangent = (end[0] - start[0], end[1] - start[1])
        tangent_length = math.hypot(*tangent)
        if tangent_length <= 1e-12:
            failures.append(f"bridge cue {section_id!r} source geometry is degenerate")
            continue
        normal = (-tangent[1] / tangent_length, tangent[0] / tangent_length)
        expected_rails = {
            "rail-left": (
                (
                    start[0] + expected_half_width * normal[0],
                    start[1] + expected_half_width * normal[1],
                ),
                (
                    end[0] + expected_half_width * normal[0],
                    end[1] + expected_half_width * normal[1],
                ),
            ),
            "rail-right": (
                (
                    start[0] - expected_half_width * normal[0],
                    start[1] - expected_half_width * normal[1],
                ),
                (
                    end[0] - expected_half_width * normal[0],
                    end[1] - expected_half_width * normal[1],
                ),
            ),
        }
        for part_name, expected_endpoints in expected_rails.items():
            cue = parts[part_name]
            try:
                offset = float(str(cue.element.get("data-bridge-rail-offset-mm") or ""))
            except ValueError:
                offset = None
            if offset is None or abs(offset - expected_half_width) > 0.001:
                failures.append(f"bridge cue {section_id!r} rail offset claim drifted")
            if len(cue.subpaths) != 1 or len(cue.subpaths[0].points) < 2:
                continue
            actual_endpoints = (cue.subpaths[0].points[0], cue.subpaths[0].points[-1])
            forward_error = max(
                math.hypot(
                    actual_endpoints[index][0] - expected_endpoints[index][0],
                    actual_endpoints[index][1] - expected_endpoints[index][1],
                )
                for index in (0, 1)
            )
            reverse_error = max(
                math.hypot(
                    actual_endpoints[index][0] - expected_endpoints[1 - index][0],
                    actual_endpoints[index][1] - expected_endpoints[1 - index][1],
                )
                for index in (0, 1)
            )
            if min(forward_error, reverse_error) > 0.004:
                failures.append(
                    f"bridge cue {section_id!r} {part_name} moved off its symbolic offset"
                )
    return failures


def _local_corridor_clearance_failures(
    event: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    offsets: Sequence[PathEvidence],
) -> list[str]:
    """Verify local derived-offset clearances without trusting their ledger.

    Only diagrammatic offset passes may be cut.  The exact Red lap is checked
    separately by ``_course_corridor_failures`` and must remain one continuous,
    source-coordinate-identical path.
    """

    failures: list[str] = []
    corridor = _mapping(f1_rendering.get("diagrammatic_course_corridor"))
    policy = corridor.get("local_clearance_policy")
    if policy != LOCAL_CORRIDOR_CLEARANCE_POLICY:
        failures.append("course-corridor local-clearance policy drifted")
    minimum_gap = _finite(corridor.get("minimum_safe_edge_gap_mm"))
    if (
        minimum_gap is None
        or abs(minimum_gap - LOCAL_CORRIDOR_MINIMUM_SAFE_EDGE_GAP_MM) > 1e-6
    ):
        failures.append("course-corridor local-clearance safe-gap floor drifted")
    if (
        corridor.get("red_centreline_modified_for_local_clearance") is not False
        or corridor.get("white_ink_used_for_local_clearance") is not False
    ):
        failures.append(
            "course-corridor local clearance weakens Red/no-white guarantees"
        )

    raw_zones = _array(corridor.get("clearance_zones"))
    if corridor.get("clearance_zone_count") != len(raw_zones):
        failures.append("course-corridor local-clearance zone-count ledger drifted")
    target = _finite(corridor.get("target_width_mm"))
    expected_radius = (
        max(target, LOCAL_CORRIDOR_MINIMUM_SAFE_EDGE_GAP_MM)
        if target is not None
        else None
    )
    independent = _independent_local_corridor_conflicts(event, f1_rendering)
    independent_by_pair = {
        (item.left_segment_index, item.right_segment_index): item
        for item in independent
    }
    zones: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_zones):
        zone = _mapping(raw)
        zone_id = str(zone.get("id") or "")
        prefix = f"course-corridor clearance zone[{index}]"
        if not zone_id or zone_id in zones:
            failures.append(f"{prefix} has an absent or duplicate id")
            continue
        raw_indexes = _array(zone.get("segment_indexes"))
        if len(raw_indexes) != 2 or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in raw_indexes
        ):
            failures.append(f"{prefix} has invalid source segment indexes")
            continue
        segment_indexes = (int(raw_indexes[0]), int(raw_indexes[1]))
        conflict = independent_by_pair.get(segment_indexes)
        if conflict is None:
            failures.append(f"{prefix} does not bind an independent non-local conflict")

        radius = _finite(zone.get("mask_radius_mm"))
        raw_bounds = _array(zone.get("mask_bounds_mm"))
        bounds_values = [_finite(value) for value in raw_bounds]
        if (
            radius is None
            or radius <= 0.0
            or len(bounds_values) != 4
            or any(value is None for value in bounds_values)
        ):
            failures.append(f"{prefix} has invalid physical mask geometry")
            continue
        assert all(value is not None for value in bounds_values)
        left, top, right, bottom = (float(value) for value in bounds_values)
        if left >= right or top >= bottom:
            failures.append(f"{prefix} has inverted physical mask bounds")
            continue
        center = ((left + right) / 2.0, (top + bottom) / 2.0)
        if (
            abs((right - left) - 2.0 * radius) > 0.003
            or abs((bottom - top) - 2.0 * radius) > 0.003
            or expected_radius is None
            or abs(radius - expected_radius) > 1e-6
        ):
            failures.append(
                f"{prefix} mask radius/bounds drifted from the paper policy"
            )

        expected_numbers = (
            (
                ("source_clearance_m", conflict.source_clearance_m),
                ("paper_clearance_mm", conflict.paper_clearance_mm),
                ("nominal_edge_gap_mm", conflict.nominal_edge_gap_mm),
                (
                    "minimum_safe_edge_gap_mm",
                    LOCAL_CORRIDOR_MINIMUM_SAFE_EDGE_GAP_MM,
                ),
            )
            if conflict is not None
            else ()
        )
        for key, expected in expected_numbers:
            actual = _finite(zone.get(key))
            if actual is None or abs(actual - expected) > 0.000002:
                failures.append(f"{prefix} {key} disagrees with source/page geometry")

        if (
            conflict is not None
            and math.hypot(
                conflict.midpoint_mm[0] - center[0],
                conflict.midpoint_mm[1] - center[1],
            )
            > 0.003
        ):
            failures.append(f"{prefix} mask center moved off its source connector")
        zones[zone_id] = {
            "center": center,
            "radius": radius,
            "segment_indexes": segment_indexes,
        }

    # A renderer may coalesce adjacent risky segment pairs into one physical
    # mask.  Every independently risky midpoint must nevertheless fall inside
    # at least one declared zone, and every zone must bind one exact pair.
    for conflict in independent:
        if not any(
            math.hypot(
                conflict.midpoint_mm[0] - zone["center"][0],
                conflict.midpoint_mm[1] - zone["center"][1],
            )
            <= float(zone["radius"]) + 0.003
            for zone in zones.values()
        ):
            failures.append(
                "course-corridor omits an independently measured non-local "
                f"conflict at segments {conflict.left_segment_index}/"
                f"{conflict.right_segment_index}"
            )

    used_zone_ids: set[str] = set()
    for offset in offsets:
        clipped = offset.element.get("data-clearance-clipped")
        raw_ids = str(offset.element.get("data-clearance-zone-ids") or "")
        zone_ids = {value for value in raw_ids.split("|") if value}
        any_open = any(not subpath.closed for subpath in offset.subpaths)
        if clipped not in {"true", "false"}:
            failures.append(
                "course-corridor offset lacks a Boolean clearance-cut claim"
            )
            continue
        if clipped == "true":
            if not zone_ids or not any_open:
                failures.append(
                    "course-corridor clearance-clipped offset has no zone or open path"
                )
            unknown = zone_ids - set(zones)
            if unknown:
                failures.append(
                    "course-corridor offset references unknown clearance zones "
                    + ", ".join(sorted(unknown))
                )
            used_zone_ids.update(zone_ids & set(zones))
        elif zone_ids or any_open:
            failures.append(
                "course-corridor uncut offset carries a clearance zone or open path"
            )

        # Check every offset against every mask, not merely its self-declared
        # zone list.  A forged empty list therefore cannot hide penetration.
        for zone_id, zone in zones.items():
            center = zone["center"]
            radius = float(zone["radius"])
            for subpath in offset.subpaths:
                distance = _point_line_distance(center, subpath.points)
                if distance + 0.02 < radius:
                    failures.append(
                        f"course-corridor offset penetrates clearance zone {zone_id!r}"
                    )
                    break

    unused = set(zones) - used_zone_ids
    if unused:
        failures.append(
            "course-corridor clearance zones cut no derived offset paths: "
            + ", ".join(sorted(unused))
        )
    return failures


def _course_corridor_failures(
    event: Mapping[str, Any],
    page: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    """Independently verify the v2 physical Red course-corridor contract."""

    failures: list[str] = []
    model = _model(event)
    lap_record = _mapping(model.get("lap"))
    expected_lap_hash = canonical_lap_sha256(lap_record)
    source_coordinate_count = len(_geojson_coordinates(lap_record))
    lap_paths = [path for path in paths if path.role == "lap-centreline"]
    if len(lap_paths) != 1:
        # The primary topology check reports the same count; return here to
        # avoid turning one missing centreline into misleading parity errors.
        return failures
    lap_path = lap_paths[0]
    expected_lap_attributes = {
        "data-source-lap-sha256": expected_lap_hash,
        "data-source-geometry-sha256": _declared_geometry_hash(event),
        "data-source-coordinate-count": str(source_coordinate_count),
        "data-projected-coordinate-count": str(source_coordinate_count),
        "data-centreline-parity": "exact-projected-source-coordinate-order",
        "data-racing-line": "false",
    }
    if any(
        lap_path.element.get(key) != value
        for key, value in expected_lap_attributes.items()
    ):
        failures.append("lap-centreline source-coordinate/hash parity drifted")
    if (
        len(lap_path.subpaths) == 1
        and len(lap_path.subpaths[0].points) != source_coordinate_count
    ):
        failures.append("serialized lap coordinate count differs from its source")

    paper = str(page.get("paper") or "").upper()
    target = COURSE_TARGET_WIDTH_BY_SHEET.get(paper)
    logical_strokes = COURSE_LOGICAL_STROKES_BY_SHEET.get(paper)
    if target is None or logical_strokes is None:
        failures.append("course-corridor QA cannot resolve the paper size")
        return failures
    expected_groups = logical_strokes - 1
    ledger = _mapping(f1_rendering.get("diagrammatic_course_corridor"))
    exact_numbers = {
        "target_width_mm": target,
        "plotted_width_mm": target,
        "nib_mm": 0.4,
        "logical_stroke_count": float(logical_strokes),
        "expected_offset_group_count": float(expected_groups),
        "emitted_offset_group_count": float(expected_groups),
        "source_centreline_path_count": 1.0,
        "source_centreline_coordinate_count": float(source_coordinate_count),
    }
    for key, expected in exact_numbers.items():
        actual = _finite(ledger.get(key))
        if actual is None or abs(actual - expected) > 1e-6:
            failures.append(f"course-corridor ledger {key} drifted")
    if ledger.get("pen_id") != "red-0-4":
        failures.append("course-corridor ledger does not bind red-0-4")
    if ledger.get("source_lap_sha256") != expected_lap_hash:
        failures.append("course-corridor ledger source lap hash drifted")
    boolean_contract = {
        "hold_on_offset_failure": True,
        "offset_fallback_allowed": False,
        "surveyed_track_width_claimed": False,
        "racing_line_claimed": False,
    }
    for key, expected in boolean_contract.items():
        if ledger.get(key) is not expected:
            failures.append(f"course-corridor ledger {key} drifted")
    radii = [_finite(value) for value in _array(ledger.get("radii_mm"))]
    if len(radii) != expected_groups // 2 or any(value is None for value in radii):
        failures.append("course-corridor radius-pair inventory drifted")

    offsets = [
        path for path in paths if path.role == "diagrammatic-course-corridor-offset"
    ]
    if len(offsets) != int(_finite(ledger.get("emitted_offset_path_count")) or -1):
        failures.append("course-corridor path-count ledger drifted")
    emitted_groups: dict[str, list[PathEvidence]] = {}
    for path in offsets:
        group_id = str(path.element.get("data-offset-group-id") or "")
        emitted_groups.setdefault(group_id, []).append(path)
        expected_attributes = {
            "data-claim": "DIAGRAMMATIC COURSE CORRIDOR",
            "data-diagrammatic": "true",
            "data-racing-line": "false",
            "data-surveyed-track-width": "false",
            "data-offset-fallback": "false",
            "data-course-target-width-mm": f"{target:g}",
            "data-source-lap-sha256": expected_lap_hash,
            "data-source-geometry-sha256": _declared_geometry_hash(event),
        }
        if path.pen_id != "red-0-4" or path.ink.casefold() != "red":
            failures.append("course-corridor offset is not a red-0-4 pass")
        if any(
            path.element.get(key) != value for key, value in expected_attributes.items()
        ):
            failures.append("course-corridor offset claim/source attributes drifted")
        expected_derivation = "buffer-envelope-from-exact-sourced-lap-centreline"
        if path.element.get("data-clearance-clipped") == "true":
            expected_derivation += ";derived-local-clearance-subtraction"
        if path.element.get("data-derivation") != expected_derivation:
            failures.append("course-corridor offset derivation claim drifted")
        if not group_id:
            failures.append("course-corridor offset has no group id")

    ledger_groups = {
        str(group.get("id")): group
        for raw in _array(ledger.get("offset_groups"))
        if (group := _mapping(raw)).get("id")
    }
    if (
        set(emitted_groups) != set(ledger_groups)
        or len(emitted_groups) != expected_groups
    ):
        failures.append("course-corridor paired group parity drifted")
    for group_id, group_paths in emitted_groups.items():
        ledger_group = ledger_groups.get(group_id)
        if ledger_group is None:
            continue
        if int(ledger_group.get("path_count", -1)) != len(group_paths):
            failures.append(f"course-corridor group {group_id!r} path count drifted")
        closed_path_count = sum(
            all(subpath.closed for subpath in path.subpaths) for path in group_paths
        )
        open_path_count = len(group_paths) - closed_path_count
        if int(ledger_group.get("closed_path_count", -1)) != closed_path_count:
            failures.append(
                f"course-corridor group {group_id!r} closure ledger drifted"
            )
        if int(ledger_group.get("open_path_count", -1)) != open_path_count:
            failures.append(
                f"course-corridor group {group_id!r} open-path ledger drifted"
            )
        clipped_path_count = sum(
            path.element.get("data-clearance-clipped") == "true" for path in group_paths
        )
        if (
            int(ledger_group.get("clearance_clipped_path_count", -1))
            != clipped_path_count
        ):
            failures.append(
                f"course-corridor group {group_id!r} clearance-cut ledger drifted"
            )
    failures.extend(_local_corridor_clearance_failures(event, f1_rendering, offsets))
    invalid_red_roles = sorted(
        {path.role for path in paths if path.ink.casefold() == "red"}
        - {"lap-centreline", "diagrammatic-course-corridor-offset"}
    )
    if invalid_red_roles:
        failures.append(f"Red carries non-course roles {invalid_red_roles}")
    return failures


def _boxes_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        min(first[2], second[2]) - max(first[0], second[0]) > 1e-9
        and min(first[3], second[3]) - max(first[1], second[1]) > 1e-9
    )


def _point_in_box(
    point: tuple[float, float], box: tuple[float, float, float, float]
) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _orientation(
    first: tuple[float, float], second: tuple[float, float], third: tuple[float, float]
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
        third[0] - first[0]
    )


def _segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    first = _orientation(a, b, c)
    second = _orientation(a, b, d)
    third = _orientation(c, d, a)
    fourth = _orientation(c, d, b)

    def on_segment(
        start: tuple[float, float],
        end: tuple[float, float],
        point: tuple[float, float],
    ) -> bool:
        return (
            min(start[0], end[0]) - 1e-9 <= point[0] <= max(start[0], end[0]) + 1e-9
            and min(start[1], end[1]) - 1e-9 <= point[1] <= max(start[1], end[1]) + 1e-9
        )

    if first * second < -1e-12 and third * fourth < -1e-12:
        return True
    return (
        (abs(first) <= 1e-12 and on_segment(a, b, c))
        or (abs(second) <= 1e-12 and on_segment(a, b, d))
        or (abs(third) <= 1e-12 and on_segment(c, d, a))
        or (abs(fourth) <= 1e-12 and on_segment(c, d, b))
    )


def _line_intersects_box(
    points: Sequence[tuple[float, float]], box: tuple[float, float, float, float]
) -> bool:
    corners = (
        (box[0], box[1]),
        (box[2], box[1]),
        (box[2], box[3]),
        (box[0], box[3]),
        (box[0], box[1]),
    )
    for first, second in zip(points, points[1:]):
        if _point_in_box(first, box) or _point_in_box(second, box):
            return True
        if any(
            _segments_intersect(first, second, a, b)
            for a, b in zip(corners, corners[1:])
        ):
            return True
    return False


def _line_distance(
    first: Sequence[tuple[float, float]], second: Sequence[tuple[float, float]]
) -> float:
    if len(first) < 2 or len(second) < 2:
        return math.inf
    minimum = math.inf
    for a, b in zip(first, first[1:]):
        for c, d in zip(second, second[1:]):
            if _segments_intersect(a, b, c, d):
                return 0.0
            minimum = min(
                minimum,
                _point_segment_distance(a, c, d),
                _point_segment_distance(b, c, d),
                _point_segment_distance(c, a, b),
                _point_segment_distance(d, a, b),
            )
    return minimum


def _project_onto_polyline(
    point: tuple[float, float], points: Sequence[tuple[float, float]]
) -> tuple[float, float]:
    """Return nearest distance and chainage on one measured polyline."""

    best_distance = math.inf
    best_chainage = 0.0
    chainage = 0.0
    for first, second in zip(points, points[1:]):
        dx = second[0] - first[0]
        dy = second[1] - first[1]
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            continue
        fraction = min(
            1.0,
            max(
                0.0,
                ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy)
                / (length * length),
            ),
        )
        projected = (first[0] + fraction * dx, first[1] + fraction * dy)
        distance = math.hypot(point[0] - projected[0], point[1] - projected[1])
        if distance < best_distance:
            best_distance = distance
            best_chainage = chainage + fraction * length
        chainage += length
    return best_distance, best_chainage


def _interpolate_polyline(
    points: Sequence[tuple[float, float]], chainage: float
) -> tuple[float, float]:
    remaining = max(0.0, chainage)
    for first, second in zip(points, points[1:]):
        length = math.hypot(second[0] - first[0], second[1] - first[1])
        if length <= 1e-12:
            continue
        if remaining <= length:
            fraction = remaining / length
            return (
                first[0] + fraction * (second[0] - first[0]),
                first[1] + fraction * (second[1] - first[1]),
            )
        remaining -= length
    return points[-1]


def _polyline_tangent(
    points: Sequence[tuple[float, float]],
    chainage: float,
    *,
    epsilon_mm: float,
) -> tuple[float, float]:
    length = _line_length(points)
    before = _interpolate_polyline(points, max(0.0, chainage - epsilon_mm))
    after = _interpolate_polyline(points, min(length, chainage + epsilon_mm))
    dx = after[0] - before[0]
    dy = after[1] - before[1]
    magnitude = math.hypot(dx, dy)
    if magnitude <= 1e-12:
        return (1.0, 0.0)
    return (dx / magnitude, dy / magnitude)


def _host_road_alignment_metrics(
    host_roads: Sequence[PathEvidence],
    lap_path: PathEvidence,
    *,
    clearance_mm: float,
    lap_tangent_epsilon_mm: float,
) -> dict[str, float | int | None]:
    """Measure duplicate road ink without rejecting transverse crossings.

    Every emitted road segment is subdivided to no more than the declared halo.
    A sample inside the halo is coincident only when its tangent has an absolute
    dot product of at least 0.85 with the independently measured local lap
    tangent. Lower alignment is retained as a genuine transverse crossing.
    """

    minimum_clearance = min(
        _line_distance(road_subpath.points, lap_subpath.points)
        for road in host_roads
        for road_subpath in road.subpaths
        for lap_subpath in lap_path.subpaths
    )
    minimum_aligned_clearance = math.inf
    coincident_length = 0.0
    transverse_length = 0.0
    coincident_samples = 0
    transverse_samples = 0
    maximum_step = max(clearance_mm, 1e-6)
    for road in host_roads:
        for road_subpath in road.subpaths:
            for first, second in zip(road_subpath.points, road_subpath.points[1:]):
                dx = second[0] - first[0]
                dy = second[1] - first[1]
                length = math.hypot(dx, dy)
                if length <= 1e-12:
                    continue
                segment_count = max(1, int(math.ceil(length / maximum_step)))
                tangent = (dx / length, dy / length)
                sample_length = length / segment_count
                for index in range(segment_count):
                    fraction = (index + 0.5) / segment_count
                    midpoint = (first[0] + fraction * dx, first[1] + fraction * dy)
                    candidates: list[tuple[float, tuple[float, float]]] = []
                    for lap_subpath in lap_path.subpaths:
                        distance, chainage = _project_onto_polyline(
                            midpoint, lap_subpath.points
                        )
                        candidates.append(
                            (
                                distance,
                                _polyline_tangent(
                                    lap_subpath.points,
                                    chainage,
                                    epsilon_mm=lap_tangent_epsilon_mm,
                                ),
                            )
                        )
                    distance, lap_tangent = min(
                        candidates, key=lambda candidate: candidate[0]
                    )
                    if distance + HOST_ROAD_CLEARANCE_TOLERANCE_MM >= clearance_mm:
                        continue
                    alignment = abs(
                        tangent[0] * lap_tangent[0] + tangent[1] * lap_tangent[1]
                    )
                    if alignment >= HOST_ROAD_ALIGNMENT_THRESHOLD:
                        coincident_samples += 1
                        coincident_length += sample_length
                        minimum_aligned_clearance = min(
                            minimum_aligned_clearance, distance
                        )
                    else:
                        transverse_samples += 1
                        transverse_length += sample_length
    return {
        "minimum_clearance_mm": (
            None if math.isinf(minimum_clearance) else minimum_clearance
        ),
        "minimum_aligned_clearance_mm": (
            None if math.isinf(minimum_aligned_clearance) else minimum_aligned_clearance
        ),
        "coincident_sample_count": coincident_samples,
        "coincident_length_mm": coincident_length,
        "transverse_sample_count": transverse_samples,
        "transverse_length_mm": transverse_length,
    }


def _resolve_output(series: Path, value: Any) -> Path | None:
    if isinstance(value, dict):
        value = value.get("path")
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = series / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(series.resolve())
    except ValueError:
        return None
    return candidate


def _output_digest(record: Any) -> str:
    return str(_mapping(record).get("sha256") or "")


def _metadata_payload(root: ET.Element) -> dict[str, Any]:
    metadata = root.find(f"{SVG}metadata")
    if metadata is None or not (metadata.text or "").strip():
        return {}
    try:
        value = json.loads(str(metadata.text))
    except json.JSONDecodeError:
        return {}
    return _mapping(value)


def _group_signature(group: ET.Element) -> tuple[Any, ...]:
    group_attributes = tuple(
        sorted((key, value) for key, value in group.attrib.items())
    )
    paths = tuple(
        tuple(sorted((key, value) for key, value in path.attrib.items()))
        for path in group.iter(f"{SVG}path")
    )
    return group_attributes, paths


def _split_parity_failures(
    series: Path,
    root: ET.Element,
    manifest: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    groups = _physical_groups(root)
    master_by_pen = {
        str(group.get("data-plot-pen-id") or ""): group for group in groups
    }
    pen_files = _array(_mapping(manifest.get("outputs")).get("pen_files"))
    if len(pen_files) != len(groups):
        failures.append(
            f"split/master parity has {len(pen_files)} split files for {len(groups)} pen groups"
        )
        return failures
    seen: set[str] = set()
    for record_value in pen_files:
        record = _mapping(record_value)
        pen_id = str(record.get("pen_id") or "")
        path = _resolve_output(series, record)
        if pen_id in seen:
            failures.append(f"split pen {pen_id!r} is duplicated")
            continue
        seen.add(pen_id)
        if pen_id not in master_by_pen:
            failures.append(f"split pen {pen_id!r} is absent from master")
            continue
        if path is None or not path.is_file():
            failures.append(
                f"split pen {pen_id!r} path is absent or leaves release root"
            )
            continue
        digest = str(record.get("sha256") or "")
        if not _SHA256.fullmatch(digest) or digest != _sha256_file(path):
            failures.append(
                f"split pen {pen_id!r} sha256 does not bind published bytes"
            )
        try:
            split_root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            failures.append(f"split pen {pen_id!r} is invalid XML: {exc}")
            continue
        split_groups = _physical_groups(split_root)
        if len(split_groups) != 1:
            failures.append(
                f"split pen {pen_id!r} contains {len(split_groups)} physical groups"
            )
            continue
        split_group = split_groups[0]
        if str(split_group.get("data-plot-pen-id") or "") != pen_id:
            failures.append(f"split pen {pen_id!r} contains the wrong physical group")
        elif _group_signature(split_group) != _group_signature(master_by_pen[pen_id]):
            failures.append(f"split pen {pen_id!r} differs from its master group")
    return failures


def _context_ink_contract_failures(paths: Sequence[PathEvidence]) -> list[str]:
    """Check v2.3 context semantics against serialized physical pen groups."""

    failures: list[str] = []
    forbidden_counts: dict[str, int] = {}
    vegetation_ink_counts: dict[tuple[str, str, str], int] = {}
    water_ink_counts: dict[tuple[str, str, str], int] = {}
    for path in paths:
        role = path.role.casefold()
        context_kind = str(path.element.get("data-context-kind") or "").casefold()
        if role in {"grass-symbol", "woodland-symbol"}:
            forbidden_counts[role] = forbidden_counts.get(role, 0) + 1

        is_vegetation = role in {
            "vegetation",
            "grass",
            "woodland",
            "context-grass",
            "context-woodland",
        } or context_kind in {"grass", "woodland"}
        if is_vegetation and (
            path.pen_id != "green-0-25" or path.ink.casefold() != "green"
        ):
            key = (role or "untyped", path.pen_id, path.ink)
            vegetation_ink_counts[key] = vegetation_ink_counts.get(key, 0) + 1

        is_water = role in {"water", "context-water", "water-stipple-dot"} or (
            context_kind == "water"
        )
        if is_water and (path.pen_id != "blue-0-25" or path.ink.casefold() != "blue"):
            key = (role or "untyped", path.pen_id, path.ink)
            water_ink_counts[key] = water_ink_counts.get(key, 0) + 1

    if forbidden_counts:
        failures.append(
            "vegetation interior symbols are forbidden; source grass/woodland must "
            "be outline-only ("
            + ", ".join(
                f"{role}={count}" for role, count in sorted(forbidden_counts.items())
            )
            + ")"
        )
    for (role, pen_id, ink), count in sorted(vegetation_ink_counts.items()):
        failures.append(
            f"{count} retained grass/woodland outline path(s) with role {role!r} "
            f"use {ink or '<missing>'}/{pen_id or '<missing>'}; Green green-0-25 is required"
        )
    for (role, pen_id, ink), count in sorted(water_ink_counts.items()):
        failures.append(
            f"{count} water polygon outline/stipple path(s) with role {role!r} "
            f"use {ink or '<missing>'}/{pen_id or '<missing>'}; Blue blue-0-25 is required"
        )
    return failures


def _context_reserve_and_water_failures(
    event: Mapping[str, Any],
    page: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
    paths: Sequence[PathEvidence],
) -> list[str]:
    """Verify the reserved vegetation budget and all-or-nothing polygon water."""

    failures: list[str] = []
    context = _mapping(f1_rendering.get("context_features"))
    vegetation = _mapping(context.get("vegetation_outline_budget"))
    map_zone = _rect(_mapping(page.get("zones_mm")).get("map_field"))
    if map_zone is None:
        return ["vegetation reserve cannot be verified without the map field"]
    field_area = (map_zone[2] - map_zone[0]) * (map_zone[3] - map_zone[1])

    if not vegetation:
        failures.append("vegetation_outline_budget ledger is absent")
    else:
        configured = _finite(vegetation.get("configured_reserve_density_mm_per_mm2"))
        target = _finite(vegetation.get("target_field_density_mm_per_mm2"))
        ledger_area = _finite(vegetation.get("field_area_mm2"))
        if (
            vegetation.get("policy") != "outline-only-density-budgeted-source-boundary"
            or vegetation.get("vegetation_interior_pattern") != "none-outline-only"
            or vegetation.get("interior_symbols_retained_independently") is not False
            or vegetation.get("whole_feature_groups_only") is not True
        ):
            failures.append("vegetation outline-only budget contract drifted")
        if (
            configured is None
            or abs(configured - F1_VEGETATION_RESERVE_MM_PER_MM2) > 1e-12
            or target is None
            or abs(target - F1_DESIGN_DENSITY_MM_PER_MM2) > 1e-12
            or ledger_area is None
            or abs(ledger_area - field_area) > 0.001
        ):
            failures.append("vegetation 0.025-within-0.17 density reserve drifted")

        candidate_records = [
            record
            for raw in _array(vegetation.get("candidate_features"))
            if (record := _mapping(raw))
        ]
        candidate_ids = [
            str(record.get("feature_id") or "") for record in candidate_records
        ]
        retained_ids = [
            str(value) for value in _array(vegetation.get("retained_feature_ids"))
        ]
        omitted_ids = [
            str(value) for value in _array(vegetation.get("omitted_feature_ids"))
        ]
        declared_candidate_ids = [
            str(value) for value in _array(vegetation.get("candidate_feature_ids"))
        ]
        if (
            any(not value for value in candidate_ids)
            or len(candidate_ids) != len(set(candidate_ids))
            or declared_candidate_ids != candidate_ids
            or sorted({*retained_ids, *omitted_ids}) != sorted(candidate_ids)
            or set(retained_ids).intersection(omitted_ids)
            or vegetation.get("candidate_feature_count") != len(candidate_ids)
            or vegetation.get("retained_feature_count") != len(retained_ids)
            or vegetation.get("omitted_feature_count") != len(omitted_ids)
        ):
            failures.append("vegetation candidate/retained/omitted partition drifted")
        if any(
            record.get("kind") not in {"grass", "woodland"}
            or record.get("interior_symbol_count") != 0
            or record.get("decision")
            != ("retained" if record.get("feature_id") in retained_ids else "omitted")
            for record in candidate_records
        ):
            failures.append(
                "vegetation candidate evidence is not outline-only and exact"
            )

        actual_retained_paths = [
            path for path in paths if path.role in {"context-grass", "context-woodland"}
        ]
        actual_retained_ids = sorted(
            {
                str(path.element.get("data-feature-id") or "")
                for path in actual_retained_paths
                if path.element.get("data-feature-id")
            }
        )
        if sorted(retained_ids) != actual_retained_ids:
            failures.append(
                "vegetation retained-feature ledger differs from SVG outlines"
            )
        actual_retained_length = sum(path.length for path in actual_retained_paths)
        retained_length = _finite(vegetation.get("retained_vegetation_outline_mm"))
        omitted_length = _finite(vegetation.get("omitted_vegetation_outline_mm"))
        candidate_length = _finite(vegetation.get("candidate_vegetation_outline_mm"))
        record_candidate_length = sum(
            float(value)
            for record in candidate_records
            if (value := _finite(record.get("outline_length_mm"))) is not None
        )
        if (
            retained_length is None
            or omitted_length is None
            or candidate_length is None
            or abs(retained_length - actual_retained_length) > 0.01
            or abs(candidate_length - retained_length - omitted_length) > 0.01
            or abs(candidate_length - record_candidate_length) > 0.01
        ):
            failures.append(
                "vegetation outline-length ledger drifted from serialized SVG"
            )

        requested = _finite(vegetation.get("requested_outline_reserve_mm"))
        expected_requested = min(
            candidate_length or 0.0,
            field_area * F1_VEGETATION_RESERVE_MM_PER_MM2,
        )
        baseline = _finite(
            vegetation.get("baseline_pen_down_mm_excluding_vegetation_outlines")
        )
        available = _finite(vegetation.get("available_vegetation_outline_mm"))
        projected = _finite(vegetation.get("projected_field_density_mm_per_mm2"))
        if (
            requested is None
            or abs(requested - expected_requested) > 0.001
            or baseline is None
            or available is None
            or abs(
                available
                - max(0.0, field_area * F1_DESIGN_DENSITY_MM_PER_MM2 - baseline)
            )
            > 0.01
            or projected is None
            or retained_length is None
            or abs(projected - (baseline + retained_length) / field_area) > 1e-6
        ):
            failures.append("vegetation reserve arithmetic drifted")

        context_density = _mapping(context.get("context_density_budget"))
        maximum_baseline = _finite(context_density.get("maximum_baseline_length_mm"))
        hard_baseline = _finite(context_density.get("hard_maximum_baseline_length_mm"))
        mandatory_reserve = _finite(vegetation.get("mandatory_zero_symbol_outline_mm"))
        if (
            not context_density
            or requested is None
            or maximum_baseline is None
            or hard_baseline is None
            or mandatory_reserve is None
            or abs(
                maximum_baseline
                - max(
                    0.0,
                    field_area * F1_DESIGN_DENSITY_MM_PER_MM2 - requested,
                )
            )
            > 0.01
            or abs(
                hard_baseline
                - max(0.0, field_area * MAX_DENSITY_MM_PER_MM2 - mandatory_reserve)
            )
            > 0.01
        ):
            failures.append(
                "context pruning did not reserve vegetation inside the design "
                "gate while preserving the mandatory-only hard gate"
            )

    source_polygon_water_ids: set[str] = set()
    for raw in _array(_model(event).get("context")):
        feature = _mapping(raw)
        if str(feature.get("kind") or "").casefold().replace("_", "-") != "water":
            continue
        geometry = _mapping(_geometry_value(feature))
        if geometry.get("type") == "Feature":
            geometry = _mapping(geometry.get("geometry"))
        if geometry.get("type") in {"Polygon", "MultiPolygon"} and feature.get("id"):
            source_polygon_water_ids.add(str(feature["id"]))
    water_outlines_by_id: dict[str, int] = {}
    water_dots_by_id: dict[str, int] = {}
    for path in paths:
        feature_id = str(path.element.get("data-feature-id") or "")
        if path.role == "context-water" and feature_id:
            water_outlines_by_id[feature_id] = (
                water_outlines_by_id.get(feature_id, 0) + 1
            )
        elif path.role == "water-stipple-dot" and feature_id:
            water_dots_by_id[feature_id] = water_dots_by_id.get(feature_id, 0) + 1
    for feature_id in sorted(source_polygon_water_ids & set(water_outlines_by_id)):
        if water_dots_by_id.get(feature_id, 0) == 0:
            failures.append(
                f"retained polygon water {feature_id!r} has an outline but no stipple"
            )
    actual_water_dots = sum(water_dots_by_id.values())
    if context.get("water_stipple_dot_count", 0) != actual_water_dots:
        failures.append("water stipple-dot count ledger drifted from SVG")
    if _mapping(context.get("emitted_path_counts_by_kind")).get("water", 0) != sum(
        water_outlines_by_id.values()
    ):
        failures.append("water outline path-count ledger drifted from SVG")
    for raw in _array(_mapping(context.get("context_density_budget")).get("decisions")):
        decision = _mapping(raw)
        if decision.get("kind") == "water" and (
            decision.get("whole_feature_group") is not True
            or decision.get("role") is not None
        ):
            failures.append("density pruning partially removes retained polygon water")
    return failures


def _visible_copy(value: Any) -> str:
    return " ".join(str(value or "").upper().split())


def _expected_length_fact(
    event: Mapping[str, Any],
) -> tuple[float | None, str, str | None]:
    """Resolve length from catalog evidence, preferring the new official fact."""

    facts = _mapping(event.get("official_facts"))
    official_fact_length = _finite(facts.get("official_circuit_length_m"))
    if official_fact_length is not None and official_fact_length > 0.0:
        return (
            official_fact_length,
            "source-backed",
            str(facts.get("source_ref") or "") or None,
        )

    circuit = _mapping(event.get("circuit"))
    geometry = _mapping(circuit.get("geometry"))
    geometry_length = _mapping(geometry.get("official_centreline_length_m"))
    geometry_value = _finite(geometry_length.get("value"))
    if geometry_value is not None and geometry_value > 0.0:
        return (
            geometry_value,
            "source-backed",
            str(geometry_length.get("source_ref") or "") or None,
        )
    lap_length = _finite(circuit.get("lap_length_m"))
    if lap_length is not None and lap_length > 0.0:
        return lap_length, "source-backed", None
    published_length = _published_length_km(event)
    if published_length is not None and published_length > 0.0:
        return published_length * 1000.0, "source-backed", None
    lap = _geojson_coordinates(_model(event).get("lap"))
    if len(lap) >= 2:
        derived = _line_length(lap)
        if derived > 0.0:
            return derived, "derived", None
    return None, "withheld", None


def _course_fact_failures(
    event: Mapping[str, Any],
    manifest: Mapping[str, Any],
    f1_rendering: Mapping[str, Any],
) -> list[str]:
    """Bind the structured four-card rail to the frozen fact ledger."""

    failures: list[str] = []
    raw_details = manifest.get("details")
    details = (
        list(raw_details)
        if isinstance(raw_details, list)
        and all(isinstance(value, str) and value.strip() for value in raw_details)
        else []
    )
    if len(details) != 3:
        failures.append("manifest details must contain three non-empty summaries")

    raw_course_facts = f1_rendering.get("course_facts")
    if not isinstance(raw_course_facts, dict):
        failures.append("manifest F1 course_facts ledger is absent")
        course_facts: dict[str, Any] = {}
    else:
        course_facts = raw_course_facts
    if course_facts.get("summary_lines") != details:
        failures.append("course_facts.summary_lines differs from manifest details")
    raw_groups = course_facts.get("visible_groups")
    groups = (
        list(raw_groups)
        if isinstance(raw_groups, list)
        and len(raw_groups) == 4
        and all(isinstance(value, dict) for value in raw_groups)
        else []
    )
    if not groups:
        failures.append("course_facts must contain four visible information groups")
    group_by_id = {
        str(group.get("id") or ""): group for group in groups if group.get("id")
    }
    visible_lines = [
        copy_value
        for group in groups
        for copy_value in [str(group.get("label") or ""), *group.get("lines", [])]
    ]
    if course_facts.get("visible_lines") != visible_lines:
        failures.append("course_facts.visible_lines differs from its visible groups")
    visible_copy = _visible_copy(" ".join(map(str, visible_lines)))
    if any(term in visible_copy for term in ("WITHHELD", "UNVERIFIED", "NOT INFERRED")):
        failures.append("information rail exposes internal withholding/review language")

    length_ledger = _mapping(course_facts.get("length"))
    expected_length_m, expected_length_status, expected_length_source = (
        _expected_length_fact(event)
    )
    if expected_length_m is None:
        if length_ledger.get("status") != "withheld":
            failures.append("course_facts.length must be explicitly withheld")
    else:
        expected_display = f"{expected_length_m / 1000.0:.3f} KM"
        ledger_value = _finite(length_ledger.get("value_m"))
        if (
            length_ledger.get("status") != expected_length_status
            or ledger_value is None
            or abs(ledger_value - expected_length_m) > 0.001
            or length_ledger.get("display_copy") != expected_display
        ):
            failures.append(
                "course_facts.length does not exactly match catalog length "
                f"{expected_length_m:.3f} m / {expected_display}"
            )
        if expected_length_source is not None and (
            length_ledger.get("source_ref") != expected_length_source
        ):
            failures.append("course_facts.length.source_ref drifted from catalog facts")
        course_group = _mapping(group_by_id.get("course"))
        course_copy = _visible_copy(" ".join(map(str, course_group.get("lines", []))))
        if expected_display not in course_copy:
            failures.append("visible course card length does not match catalog facts")

    facts_present = isinstance(event.get("official_facts"), dict)
    facts = _mapping(event.get("official_facts"))
    facts_source_ref = str(facts.get("source_ref") or "") or None
    first_gp = facts.get("first_grand_prix")
    first_gp_ledger = _mapping(course_facts.get("first_grand_prix"))
    if isinstance(first_gp, int) and not isinstance(first_gp, bool):
        if (
            first_gp_ledger.get("status") != "source-backed"
            or first_gp_ledger.get("year") != first_gp
            or first_gp_ledger.get("source_ref") != facts_source_ref
        ):
            failures.append(
                "course_facts.first_grand_prix does not exactly match catalog facts"
            )
        history_group = _mapping(group_by_id.get("history"))
        history_copy = _visible_copy(" ".join(map(str, history_group.get("lines", []))))
        if f"FIRST GRAND PRIX {first_gp}" not in history_copy:
            failures.append("visible history card does not match First GP facts")
    else:
        if (
            first_gp_ledger.get("status") != "withheld"
            or first_gp_ledger.get("year") is not None
        ):
            failures.append("course_facts.first_grand_prix must be withheld")

    fastest_ledger = _mapping(course_facts.get("fastest_lap"))
    catalog_fastest = _mapping(facts.get("fastest_lap"))
    fastest_status = str(catalog_fastest.get("status") or "")
    if not facts_present:
        fastest_status = "withheld"
    if fastest_status == "source-backed":
        expected_time = catalog_fastest.get("time")
        expected_driver = catalog_fastest.get("driver")
        expected_season = catalog_fastest.get("season")
        expected_time_ms = catalog_fastest.get("time_ms")
        expected_fastest_source = catalog_fastest.get("source_ref")
        if any(
            (
                fastest_ledger.get("status") != "source-backed",
                fastest_ledger.get("time") != expected_time,
                fastest_ledger.get("time_ms") != expected_time_ms,
                fastest_ledger.get("driver") != expected_driver,
                fastest_ledger.get("season") != expected_season,
                fastest_ledger.get("source_ref") != expected_fastest_source,
                "withheld_reason" in fastest_ledger,
            )
        ):
            failures.append(
                "course_facts.fastest_lap does not exactly match catalog time/full driver/year"
            )
        record_group = _mapping(group_by_id.get("record"))
        record_copy = _visible_copy(
            " ".join(
                [
                    str(record_group.get("label") or ""),
                    *map(str, record_group.get("lines", [])),
                ]
            )
        )
        exact_visible_values = (
            isinstance(expected_time, str)
            and expected_time in record_copy
            and isinstance(expected_driver, str)
            and _visible_copy(expected_driver) in record_copy
            and isinstance(expected_season, int)
            and str(expected_season) in record_copy
            and "FASTEST LAP" in record_copy
        )
        if not exact_visible_values:
            failures.append(
                "visible fastest lap does not preserve exact catalog time/full driver/year"
            )
        if course_facts.get("full_driver_copy_preserved") is not True:
            failures.append(
                "course_facts does not affirm full driver copy preservation"
            )
    elif fastest_status == "withheld":
        if fastest_ledger.get("status") != "withheld":
            failures.append("course_facts.fastest_lap must be explicitly withheld")
        if any(
            key in fastest_ledger for key in ("time", "time_ms", "driver", "season")
        ):
            failures.append(
                "withheld course_facts.fastest_lap leaks performance values"
            )
        expected_reason = catalog_fastest.get("withheld_reason")
        expected_source = catalog_fastest.get("source_ref")
        if facts_present and (
            fastest_ledger.get("withheld_reason") != expected_reason
            or fastest_ledger.get("source_ref") != expected_source
        ):
            failures.append("withheld fastest-lap ledger drifted from catalog facts")
        if (
            not facts_present
            and not str(fastest_ledger.get("withheld_reason") or "").strip()
        ):
            failures.append("generic fastest-lap withholding has no reason")
        if "record" in group_by_id:
            failures.append("withheld fastest lap is falsely presented as a record")
        if not any(group.get("id") == "edition" for group in groups):
            failures.append("missing fastest lap must resolve to a neutral edition card")
    else:
        failures.append("catalog fastest-lap status is unsupported for visible facts")

    circuit = _mapping(event.get("circuit"))
    expected_reference_season = event.get(
        "configuration_reference_season", circuit.get("configuration_season")
    )
    if (
        isinstance(expected_reference_season, int)
        and course_facts.get("configuration_reference_season")
        != expected_reference_season
    ):
        failures.append("course_facts configuration reference season drifted")
    return failures


def _artifact_failures(
    *,
    series: Path,
    entry: Mapping[str, Any],
    event: Mapping[str, Any],
    catalog_hash: str,
    catalog_sources: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    metrics: dict[str, Any] = {}
    event_id = _event_id(event)
    artifact_id = str(entry.get("id") or entry.get("artifact_id") or "")
    format_id = str(entry.get("format_id") or entry.get("format") or "")
    expected_geometry_hash = _declared_geometry_hash(event)
    if str(entry.get("event_id") or entry.get("subject_id") or "") != event_id:
        failures.append("release entry event_id does not bind its catalog event")
    if not format_id or format_id not in FORMATS:
        failures.append("release entry has an unsupported format_id")
    if format_id and format_id not in artifact_id:
        failures.append("artifact id does not embed its format id")
    if str(entry.get("artifact_kind") or ARTIFACT_KIND) != ARTIFACT_KIND:
        failures.append(f"release entry artifact_kind must be {ARTIFACT_KIND}")
    if str(entry.get("catalog_sha256") or "") != catalog_hash:
        failures.append("release entry catalog_sha256 does not bind the frozen catalog")
    if str(entry.get("source_geometry_sha256") or "") != expected_geometry_hash:
        failures.append("release entry source geometry digest drifted from the catalog")
    if str(
        entry.get("calendar_status") or entry.get("release_status") or ""
    ).casefold() != _event_status(event):
        failures.append("release entry event status drifted from the current ledger")

    outputs = _mapping(entry.get("outputs"))
    svg_path = _resolve_output(series, outputs.get("svg"))
    manifest_path = _resolve_output(series, outputs.get("manifest"))
    if svg_path is None or not svg_path.is_file():
        failures.append("master SVG is absent or leaves the release root")
        return failures, metrics
    svg_digest = _output_digest(outputs.get("svg"))
    if not _SHA256.fullmatch(svg_digest) or svg_digest != _sha256_file(svg_path):
        failures.append("release entry SVG sha256 does not bind published bytes")
    if manifest_path is None or not manifest_path.is_file():
        failures.append("plot manifest is absent or leaves the release root")
        return failures, metrics
    manifest_digest = _output_digest(outputs.get("manifest"))
    if not _SHA256.fullmatch(manifest_digest) or manifest_digest != _sha256_file(
        manifest_path
    ):
        failures.append("release entry manifest sha256 does not bind published bytes")
    try:
        manifest = _load_json(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        failures.append(f"plot manifest is invalid: {exc}")
        return failures, metrics
    if manifest.get("artifact_kind") != ARTIFACT_KIND:
        failures.append(f"manifest artifact_kind must be {ARTIFACT_KIND}")
    if manifest.get("artifact_id") != artifact_id:
        failures.append("manifest artifact_id drifted from the release entry")
    if str(manifest.get("subject_id") or manifest.get("event_id") or "") != event_id:
        failures.append("manifest subject_id drifted from the release entry")
    page = _mapping(manifest.get("page"))
    if str(page.get("format_id") or manifest.get("format_id") or "") != format_id:
        failures.append("manifest format_id drifted from the release entry")
    f1_rendering = _mapping(_mapping(manifest.get("rendering")).get("f1_circuit"))
    if (
        str(
            f1_rendering.get("geometry_sha256")
            or f1_rendering.get("source_geometry_sha256")
            or ""
        )
        != expected_geometry_hash
    ):
        failures.append("manifest F1 geometry digest drifted from the catalog")
    if str(f1_rendering.get("catalog_sha256") or catalog_hash) != catalog_hash:
        failures.append("manifest catalog digest drifted from the frozen catalog")
    expected_source_refs = _referenced_source_ids(event)
    manifest_sources = {
        str(source.get("id")): source
        for source in _array(manifest.get("sources"))
        if isinstance(source, dict) and source.get("id")
    }
    if set(manifest_sources) != expected_source_refs:
        failures.append(
            "manifest source inventory differs from event source references"
        )
    for source_id, source in manifest_sources.items():
        catalog_source = catalog_sources.get(source_id)
        if catalog_source is None or source.get("sha256") != catalog_source.get(
            "sha256"
        ):
            failures.append(
                f"manifest source {source_id!r} digest drifted from the frozen catalog"
            )

    manifest_outputs = _mapping(manifest.get("outputs"))
    manifest_svg_path = _resolve_output(series, manifest_outputs.get("svg"))
    if manifest_svg_path != svg_path or _output_digest(
        manifest_outputs.get("svg")
    ) != _sha256_file(svg_path):
        failures.append(
            "manifest SVG output binding differs from published master bytes"
        )
    manifest_manifest_path = _resolve_output(series, manifest_outputs.get("manifest"))
    if manifest_manifest_path != manifest_path:
        failures.append("manifest self-path differs from the release entry")

    try:
        root = ET.parse(svg_path).getroot()
    except ET.ParseError as exc:
        failures.append(f"master SVG is invalid XML: {exc}")
        return failures, metrics
    if _local_name(root.tag) != "svg":
        failures.append("master output is not an SVG root")
        return failures, metrics
    metadata = _metadata_payload(root)
    metadata_hash = str(
        metadata.get("geometry_sha256")
        or metadata.get("source_geometry_sha256")
        or _mapping(metadata.get("f1_circuit")).get("geometry_sha256")
        or root.get("data-source-geometry-sha256")
        or ""
    )
    if metadata_hash != expected_geometry_hash:
        failures.append("SVG metadata geometry digest drifted from the catalog")
    if metadata.get("artifact_id") != artifact_id:
        failures.append("SVG metadata artifact_id drifted from the release entry")
    if str(metadata.get("format_id") or "") != format_id:
        failures.append("SVG metadata format_id drifted from the release entry")
    svg_sources = {
        str(source.get("id")): source
        for source in _array(metadata.get("sources"))
        if isinstance(source, dict) and source.get("id")
    }
    if set(svg_sources) != expected_source_refs:
        failures.append(
            "SVG metadata source inventory differs from event source references"
        )
    for source_id, source in svg_sources.items():
        catalog_source = catalog_sources.get(source_id)
        if catalog_source is None or source.get("sha256") != catalog_source.get(
            "sha256"
        ):
            failures.append(
                f"SVG metadata source {source_id!r} digest drifted from the frozen catalog"
            )

    for element in root.iter():
        local = _local_name(element.tag)
        if local in {"text", "image", "use"}:
            failures.append(f"SVG contains forbidden visible <{local}> content")
        elif local in _GRAPHIC_TAGS and local != "path":
            failures.append(f"SVG uses <{local}> instead of editable physical paths")
        fill = str(element.get("fill") or "").replace(" ", "").casefold()
        style = str(element.get("style") or "").replace(" ", "").casefold()
        if fill in _WHITE or any(f"fill:{white}" in style for white in _WHITE):
            failures.append("SVG contains forbidden white fill")
        if element.get("transform") and local in _GRAPHIC_TAGS:
            failures.append("SVG visible geometry uses a transform")

    path_failures: list[str] = []
    paths = _paths_with_evidence(root, path_failures)
    failures.extend(path_failures)
    title_failures, title_metrics = _serialized_title_line_envelope_failures(
        paths,
        page,
    )
    failures.extend(title_failures)
    metrics.update(title_metrics)
    groups = _physical_groups(root)
    pen_steps: list[int] = []
    pen_ids: list[str] = []
    for group in groups:
        try:
            pen_steps.append(int(str(group.get("data-pen-step") or "")))
        except ValueError:
            failures.append("physical pen group has a non-integer step")
        pen_ids.append(str(group.get("data-plot-pen-id") or ""))
    if pen_steps != list(range(1, len(groups) + 1)):
        failures.append("physical pen steps are not contiguous document order")
    if len(pen_ids) != len(set(pen_ids)):
        failures.append("a physical pen is loaded in more than one group")
    if len(pen_ids) > MAX_PEN_COUNT:
        failures.append(
            f"artifact uses {len(pen_ids)} pens, above the {MAX_PEN_COUNT}-pen ceiling"
        )
    expected_subsequence = [pen for pen in F1_PEN_ORDER if pen in pen_ids]
    if pen_ids != expected_subsequence:
        failures.append("physical pens are not in the binding F1 order")
    manifest_pen_ids = [
        str(item.get("pen_id") or "")
        for item in _array(manifest.get("pen_sequence"))
        if isinstance(item, dict)
    ]
    if manifest_pen_ids != pen_ids:
        failures.append("manifest pen sequence differs from SVG document order")
    declared_field_order = _array(f1_rendering.get("field_pen_order"))
    if declared_field_order != list(F1_FIELD_PEN_ORDER):
        failures.append("manifest does not declare the binding F1 field_pen_order")
    failures.extend(_context_ink_contract_failures(paths))
    failures.extend(
        _context_reserve_and_water_failures(event, page, f1_rendering, paths)
    )
    failures.extend(_course_fact_failures(event, manifest, f1_rendering))

    for path in paths:
        if path.length + 1e-9 < 3.0 * path.nib_mm:
            failures.append(
                f"{path.role or 'untyped path'} is {path.length:.3f} mm, below its three-nib floor"
            )
        lowered_role = path.role.casefold()
        if lowered_role == "host-road-halo":
            failures.append(
                "SVG emits a forbidden host-road halo path instead of negative space"
            )
        if "connector" in lowered_role:
            failures.append("SVG contains an invented connector")
        if path.element.get("data-track-width-mm") is not None:
            failures.append("SVG contains an unsupported track-width claim")
        if _is_source_geometry_role(path.role):
            if not path.element.get("data-source-ref") or not path.element.get(
                "data-source-object-id"
            ):
                failures.append(
                    f"source geometry role {path.role!r} is not source-object bound"
                )
        derivation = str(path.element.get("data-derivation") or "").casefold()
        if derivation and "apex" in (
            lowered_role + " " + str(path.element.attrib).casefold()
        ):
            failures.append("derived SVG station is falsely called a racing apex")

    declared_source_objects = _declared_source_object_ids(_model(event))
    context_ids = {
        str(item.get("id"))
        for item in _array(_model(event).get("context"))
        if isinstance(item, dict) and item.get("id")
    }
    for path in paths:
        if not _is_source_geometry_role(path.role):
            if _is_context_derived_role(path.role):
                feature_id = str(path.element.get("data-feature-id") or "")
                if feature_id not in context_ids or not path.element.get(
                    "data-source-ref"
                ):
                    failures.append(
                        f"derived context role {path.role!r} lacks source feature lineage"
                    )
            continue
        object_id = str(path.element.get("data-source-object-id") or "")
        object_parts = {part for part in object_id.split("|") if part}
        if object_parts and not object_parts <= declared_source_objects:
            failures.append(
                f"source geometry role {path.role!r} invents source object {object_id!r}"
            )
        feature_id = str(path.element.get("data-feature-id") or "")
        if path.role.startswith("context-") and feature_id not in context_ids:
            failures.append(
                f"source context role {path.role!r} invents feature {feature_id!r}"
            )

    boundary_count = len(_array(_model(event).get("track_boundaries")))
    emitted_boundary_indices: list[int] = []
    context_ledger = _mapping(f1_rendering.get("context_features"))
    boundary_ledger = {
        int(record.get("boundary_index")): record
        for raw in _array(context_ledger.get("track_boundary_qualifications"))
        if (record := _mapping(raw)).get("boundary_index") is not None
        and isinstance(record.get("boundary_index"), int)
    }
    density_boundary_omissions = [
        record
        for raw in _array(f1_rendering.get("feature_omissions"))
        if (record := _mapping(raw)).get("reason")
        == "paper-field-density-budget-qualified-track-boundary-group"
    ]
    density_omitted_indices: set[int] = set()
    for omission in density_boundary_omissions:
        raw_indices = _array(omission.get("boundary_indices"))
        if (
            omission.get("feature_id") != "qualified-track-boundary-group"
            or omission.get("kind") != "track-boundary"
            or omission.get("all_or_nothing_group") is not True
            or omission.get("red_course_geometry_retained") is not True
            or not raw_indices
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in raw_indices
            )
        ):
            failures.append(
                "paper-density track-boundary omission evidence is incomplete"
            )
            continue
        density_omitted_indices.update(int(value) for value in raw_indices)
    if density_boundary_omissions:
        density_budget = _mapping(context_ledger.get("context_density_budget"))
        density_decisions = [
            record
            for raw in _array(density_budget.get("decisions"))
            if (record := _mapping(raw)).get("reason")
            == "paper-field-density-budget-qualified-track-boundary-group"
        ]
        if len(density_boundary_omissions) != 1 or len(density_decisions) != 1:
            failures.append(
                "track-boundary density omission is not one all-or-nothing decision"
            )
        else:
            omission = density_boundary_omissions[0]
            decision = density_decisions[0]
            removed = _finite(decision.get("removed_length_mm"))
            initial = _finite(density_budget.get("initial_baseline_length_mm"))
            retained = _finite(density_budget.get("retained_baseline_length_mm"))
            hard_maximum = _finite(
                density_budget.get("hard_maximum_baseline_length_mm")
            )
            if omission != decision:
                failures.append(
                    "track-boundary density decision differs from omission evidence"
                )
            if (
                removed is None
                or removed <= 0.0
                or retained is None
                or hard_maximum is None
                or retained > hard_maximum + 1e-6
                or retained + removed <= hard_maximum + 1e-6
            ):
                failures.append(
                    "track-boundary omission lacks exact necessary hard-gate evidence"
                )
            all_removed = [
                _finite(_mapping(raw).get("removed_length_mm"))
                for raw in _array(density_budget.get("decisions"))
            ]
            if (
                initial is None
                or retained is None
                or any(value is None for value in all_removed)
                or abs(
                    initial
                    - sum(float(value) for value in all_removed if value is not None)
                    - retained
                )
                > 0.01
            ):
                failures.append(
                    "track-boundary density decision totals do not reconcile"
                )
            if len([path for path in paths if path.role == "lap-centreline"]) != 1:
                failures.append(
                    "track-boundary density omission does not retain one exact Red lap"
                )
    for path in paths:
        if path.role != "track-boundary":
            continue
        raw_index = path.element.get("data-boundary-index")
        if raw_index is None and path.element.get("data-boundary-id"):
            continue
        try:
            boundary_index = int(str(raw_index))
        except (TypeError, ValueError):
            failures.append("SVG track-boundary lacks a source boundary index")
            continue
        emitted_boundary_indices.append(boundary_index)
        if not 0 <= boundary_index < boundary_count:
            failures.append("SVG track-boundary index invents a source boundary")
            continue
        qualification = boundary_ledger.get(boundary_index)
        if qualification is None or qualification.get("resolvable") is not True:
            failures.append("SVG track-boundary lacks a resolvable qualification")
        if path.element.get("data-lap-associated") != "true":
            failures.append("SVG track-boundary is not explicitly lap-associated")
        if path.element.get("data-boundary-geometry-kind") == "source-area":
            try:
                coverage = float(
                    str(path.element.get("data-lap-coverage-fraction") or "")
                )
            except ValueError:
                coverage = None
            if coverage is None or coverage + 1e-9 < 0.95:
                failures.append(
                    "SVG source-area track edge covers under 95% of the lap"
                )
    if boundary_ledger:
        ledger_density_indices: set[int] = set()
        for index, record in boundary_ledger.items():
            status = record.get("emission_status")
            if status is None:
                # Backward-compatible synthetic evidence: before responsive
                # omission existed, every resolvable boundary had to emit.
                status = (
                    "emitted"
                    if record.get("resolvable") is True
                    else "omitted-source-qualification"
                )
            if status == "omitted-paper-density-budget":
                ledger_density_indices.add(index)
                if (
                    record.get("resolvable") is not True
                    or record.get("emission_omission_reason")
                    != "paper-field-density-budget-qualified-track-boundary-group"
                ):
                    failures.append(
                        "density-omitted track boundary weakens source qualification"
                    )
            elif status == "emitted":
                if record.get("resolvable") is not True:
                    failures.append("unresolvable track boundary is marked emitted")
            elif status == "omitted-source-qualification":
                if record.get("resolvable") is True:
                    failures.append(
                        "resolvable track boundary is falsely source-qualification omitted"
                    )
            else:
                failures.append("track-boundary emission status is unsupported")
        if ledger_density_indices != density_omitted_indices:
            failures.append(
                "track-boundary density omission ledger/source evidence drifted"
            )
        resolvable_indices = {
            index
            for index, record in boundary_ledger.items()
            if record.get("resolvable") is True
        }
        if ledger_density_indices and ledger_density_indices != resolvable_indices:
            failures.append(
                "track-boundary density omission is not the complete qualified group"
            )
        expected_emitted = {
            index
            for index, record in boundary_ledger.items()
            if record.get("resolvable") is True
            and record.get("emission_status") != "omitted-paper-density-budget"
        }
        if set(emitted_boundary_indices) != expected_emitted:
            failures.append("track-boundary emission differs from qualification ledger")
    if context_ledger.get("track_boundary_emitted_path_count") != len(
        emitted_boundary_indices
    ):
        failures.append("track-boundary emitted path count ledger drifted")
    if context_ledger.get("track_boundary_emitted_feature_count") != len(
        set(emitted_boundary_indices)
    ):
        failures.append("track-boundary emitted feature count ledger drifted")

    special_sections = {
        str(item.get("id")): item
        for item in _array(_model(event).get("special_sections"))
        if isinstance(item, dict) and item.get("id")
    }
    _overlay_status, operational_records, _overlay_failures = (
        _operational_overlay_records(_model(event))
    )
    sourced_operational_claims = {
        str(item.get("id")): item
        for _label, item in operational_records
        if item.get("id")
    }
    expected_sections = {*special_sections, *sourced_operational_claims}
    for path in paths:
        kind = str(path.element.get("data-operational-kind") or "")
        feature_id = str(
            path.element.get("data-section-id")
            or path.element.get("data-feature-id")
            or ""
        )
        is_drs_claim = _contains_drs_term(path.role, kind, feature_id)
        if path.role != "operational-overlay":
            if is_drs_claim:
                failures.append(
                    "SVG current-event DRS claim must use the operational-overlay role"
                )
            continue
        if kind == "lap-direction-arrow":
            if path.element.get("data-derivation") != "station-on-lap":
                failures.append(
                    "lap direction overlay lacks its station-on-lap derivation"
                )
        elif feature_id not in expected_sections:
            failures.append(
                f"operational overlay invents special section {feature_id!r}"
            )
        if not is_drs_claim:
            continue
        claim = sourced_operational_claims.get(feature_id) or special_sections.get(
            feature_id
        )
        if claim is None or not _contains_drs_term(
            claim.get("kind"), claim.get("name")
        ):
            failures.append(
                "SVG DRS claim is not bound to a matching catalog operational claim"
            )
            continue
        if path.element.get("data-source-ref") != claim.get("source_ref"):
            failures.append("SVG DRS claim source_ref drifted from the catalog")
        if path.element.get("data-valid-for-season") != str(SEASON):
            failures.append(f"SVG DRS claim data-valid-for-season must equal {SEASON}")
        if path.element.get("data-document-version") != claim.get("document_version"):
            failures.append("SVG DRS claim document version drifted from the catalog")
        if path.element.get("data-evidence-scope") != "current-event-document":
            failures.append(
                "SVG DRS claim data-evidence-scope must be current-event-document"
            )

    lap_paths = [path for path in paths if path.role == "lap-centreline"]
    if len(lap_paths) != 1:
        failures.append(
            f"SVG has {len(lap_paths)} lap-centreline paths, expected exactly one"
        )
    lap_path = lap_paths[0] if len(lap_paths) == 1 else None
    if lap_path is not None:
        if lap_path.ink.casefold() != "red" or lap_path.pen_id != "red-0-4":
            failures.append("the one closed lap is not the binding red-0-4 hero")
        if len(lap_path.subpaths) != 1:
            failures.append(
                "lap-centreline contains more than one disconnected subpath"
            )
        else:
            subpath = lap_path.subpaths[0]
            closure = math.hypot(
                subpath.end[0] - subpath.start[0], subpath.end[1] - subpath.start[1]
            )
            metrics["lap_closure_mm"] = closure
            if not subpath.closed and closure > MAX_LAP_CLOSURE_MM + 1e-12:
                failures.append(
                    f"lap-centreline is open by {closure:.3f} mm, above {MAX_LAP_CLOSURE_MM:.2f} mm"
                )
    red_hero = [
        path
        for path in paths
        if path.ink.casefold() == "red" and path.role in _HERO_ROLES
    ]
    total_red_hero = sum(path.length for path in red_hero)
    connected_fraction = (
        lap_path.length / total_red_hero
        if lap_path is not None and total_red_hero > 0.0
        else 0.0
    )
    metrics["connected_hero_fraction"] = connected_fraction
    if connected_fraction + 1e-12 < MIN_CONNECTED_HERO_FRACTION:
        failures.append(
            f"connected red hero fraction {connected_fraction:.3f} is below {MIN_CONNECTED_HERO_FRACTION:.2f}"
        )

    failures.extend(_course_corridor_failures(event, page, f1_rendering, paths))
    failures.extend(_framing_failures(event, f1_rendering, paths))
    failures.extend(_track_boundary_density_omission_failures(f1_rendering, paths))
    failures.extend(_atlas_context_mode_failures(event, f1_rendering))
    failures.extend(
        _grandstand_observation_failures(event, manifest, f1_rendering, paths)
    )
    failures.extend(
        _historic_current_context_disclosure_failures(event, manifest, paths)
    )
    failures.extend(_grade_separation_cue_failures(event, f1_rendering, paths))

    expected_turns = {
        str(turn.get("id"))
        for turn in _array(_model(event).get("turn_stations"))
        if isinstance(turn, dict) and turn.get("id")
    }
    emitted_turns = [
        str(path.element.get("data-turn-id") or "")
        for path in paths
        if path.role == "turn-marker"
    ]
    if set(emitted_turns) != expected_turns or len(emitted_turns) != len(
        expected_turns
    ):
        missing = sorted(expected_turns - set(emitted_turns))
        extra = sorted(set(emitted_turns) - expected_turns)
        duplicates = sorted(
            {value for value in emitted_turns if emitted_turns.count(value) > 1}
        )
        failures.append(
            "turn station parity failed "
            f"(missing={missing}, extra={extra}, duplicates={duplicates})"
        )

    geometry_status = str(
        _mapping(_mapping(event.get("circuit")).get("geometry")).get("status") or ""
    )
    centreline_only = geometry_status == CENTRELINE_GEOMETRY_STATUS
    source_start_finish = _mapping(_model(event).get("start_finish"))
    start_finish_paths = [path for path in paths if path.role == "start-finish"]
    if source_start_finish and not start_finish_paths:
        failures.append("SVG has no start-finish mark for its sourced anchor")
    elif not source_start_finish and start_finish_paths:
        failures.append("SVG invents a start-finish mark absent from the source model")
    elif start_finish_paths and (
        lap_path is not None
        and min(
            _line_distance(mark_subpath.points, lap_subpath.points)
            for mark in start_finish_paths
            for mark_subpath in mark.subpaths
            for lap_subpath in lap_path.subpaths
        )
        > MAX_LAP_CLOSURE_MM
    ):
        failures.append("SVG start-finish mark is not on the rendered lap")
    pit_paths = [path for path in paths if path.role == "pit-lane"]
    if len(pit_paths) != len(_array(_model(event).get("pit_lanes"))):
        failures.append("SVG pit-lane count differs from the catalog topology")
    for pit in pit_paths:
        if lap_path is not None:
            pit_endpoints = [
                endpoint
                for subpath in pit.subpaths
                for endpoint in (subpath.start, subpath.end)
            ]
            for endpoint_name, endpoint in zip(("entry", "exit"), pit_endpoints[:2]):
                distance = min(
                    _point_line_distance(endpoint, lap_subpath.points)
                    for lap_subpath in lap_path.subpaths
                )
                if distance > MAX_LAP_CLOSURE_MM:
                    failures.append(
                        f"SVG pit {endpoint_name} is {distance:.3f} mm from the rendered lap"
                    )

    geometry_qualification = _mapping(f1_rendering.get("geometry_qualification"))
    if centreline_only:
        if (
            geometry_qualification.get("status") != CENTRELINE_GEOMETRY_STATUS
            or geometry_qualification.get("centreline_only") is not True
            or geometry_qualification.get("course_scope_visibly_stated") is not True
            or geometry_qualification.get("omissions_visibly_disclosed") is not False
        ):
            failures.append("manifest centreline-only qualification ledger drifted")
        visible_copy = " ".join(
            map(
                str,
                _array(
                    _mapping(f1_rendering.get("course_facts")).get("visible_lines")
                ),
            )
        ).upper()
        if "SOURCE CENTRELINE" not in visible_copy:
            failures.append(
                "centreline-only qualification is absent from visible furniture"
            )
        if "COURSE DRAWING" not in visible_copy:
            failures.append(
                "centreline-only drawing scope is absent from visible information rail"
            )
    elif (
        geometry_qualification and geometry_qualification.get("centreline_only") is True
    ):
        failures.append("full geometry is falsely marked centreline-only")

    failures.extend(_context_label_lineage_failures(event, f1_rendering, paths))
    failures.extend(_named_section_lineage_failures(event, page, f1_rendering, paths))

    labels: dict[str, tuple[float, float, float, float]] = {}
    for path in paths:
        if path.role not in _LABEL_ROLES and path.element.get("data-label-id") is None:
            continue
        label_id = str(path.element.get("data-label-id") or "")
        box = _parse_label_box(path.element.get("data-label-box"))
        if not label_id or box is None:
            failures.append(
                f"{path.role} lacks deterministic data-label-id/data-label-box"
            )
            continue
        previous = labels.setdefault(label_id, box)
        if previous != box:
            failures.append(f"label {label_id!r} repeats with inconsistent boxes")
    label_items = sorted(labels.items())
    label_overlap_count = 0
    for left_index, (left_id, left_box) in enumerate(label_items):
        for right_id, right_box in label_items[left_index + 1 :]:
            if _boxes_overlap(left_box, right_box):
                label_overlap_count += 1
                failures.append(f"label boxes {left_id!r} and {right_id!r} overlap")
    route_overlap_count = 0
    route_paths = [
        path
        for path in paths
        if path.role
        in {
            "lap-centreline",
            "diagrammatic-course-corridor-offset",
            "pit-lane",
        }
    ]
    for label_id, box in label_items:
        if any(
            _line_intersects_box(subpath.points, box)
            for route in route_paths
            for subpath in route.subpaths
        ):
            route_overlap_count += 1
            failures.append(
                f"label box {label_id!r} overlaps the course corridor or pit route"
            )
    metrics["label_bbox_overlap_count"] = label_overlap_count
    metrics["label_route_overlap_count"] = route_overlap_count

    host_roads = [path for path in paths if path.role == "host-road"]
    clearance_ledger = _mapping(f1_rendering.get("track_clearance"))
    clearance_policy = str(
        clearance_ledger.get("policy")
        or clearance_ledger.get("host_road_suppression")
        or ""
    )
    clearance = _finite(
        clearance_ledger.get("minimum_clearance_mm")
        or clearance_ledger.get("clearance_mm")
        or clearance_ledger.get("halo_mm")
    )
    if clearance_policy not in {
        "source-space-subtraction",
        "negative-space-clip",
        "source-space-subtraction-no-white-ink",
    }:
        failures.append("manifest has no source-space track-clearance ledger")
    if clearance_ledger.get("host_road_halo_emitted_as_path") is not False:
        failures.append("track-clearance ledger does not forbid a visible halo path")
    if host_roads:
        if clearance is None or clearance + 1e-9 < 3.0 * max(
            road.nib_mm for road in host_roads
        ):
            failures.append(
                "track-clearance ledger is below three host-road nib widths"
            )
        elif lap_path is not None:
            source_lap_length = _line_length(
                _geojson_coordinates(_model(event).get("lap"))
            )
            if source_lap_length > 1e-9:
                paper_scale = lap_path.length / source_lap_length
                source_epsilon = max(
                    0.2,
                    min(2.0, source_lap_length * 0.002),
                )
                tangent_epsilon = max(1e-6, source_epsilon * paper_scale)
            else:
                tangent_epsilon = max(1e-6, lap_path.length * 0.002)
            alignment_metrics = _host_road_alignment_metrics(
                host_roads,
                lap_path,
                clearance_mm=clearance,
                lap_tangent_epsilon_mm=tangent_epsilon,
            )
            measured_clearance = alignment_metrics["minimum_clearance_mm"]
            metrics["minimum_host_road_lap_clearance_mm"] = measured_clearance
            metrics["minimum_aligned_host_road_lap_clearance_mm"] = alignment_metrics[
                "minimum_aligned_clearance_mm"
            ]
            metrics["host_road_coincident_halo_sample_count"] = alignment_metrics[
                "coincident_sample_count"
            ]
            metrics["host_road_coincident_halo_length_mm"] = alignment_metrics[
                "coincident_length_mm"
            ]
            metrics["host_road_transverse_halo_sample_count"] = alignment_metrics[
                "transverse_sample_count"
            ]
            metrics["host_road_transverse_halo_length_mm"] = alignment_metrics[
                "transverse_length_mm"
            ]
            metrics["host_road_alignment_threshold"] = HOST_ROAD_ALIGNMENT_THRESHOLD
            if int(alignment_metrics["coincident_sample_count"] or 0) > 0:
                failures.append(
                    "published host-road geometry overdraws the ledgered "
                    "negative-space clearance with "
                    f"{float(alignment_metrics['coincident_length_mm']):.3f} mm "
                    f"of aligned ink (|dot| >= {HOST_ROAD_ALIGNMENT_THRESHOLD:.2f})"
                )

    zones_raw = _mapping(page.get("zones_mm"))
    zones = {name: _rect(value) for name, value in zones_raw.items()}
    if not zones or any(zone is None for zone in zones.values()):
        failures.append("manifest page has no complete canonical zones_mm")
    else:
        page_width = _finite(page.get("width_mm"))
        page_height = _finite(page.get("height_mm"))
        margin = _finite(page.get("margin_mm"))
        safe = (
            (
                margin,
                margin,
                page_width - margin,
                page_height - margin,
            )
            if None not in {page_width, page_height, margin}
            else None
        )
        for path in paths:
            if path.role in _FRAME_ROLES:
                if safe is None or not _inside(path.bounds, safe):
                    failures.append(f"{path.role} leaves the plotter-safe page")
                continue
            zone_name = (
                path.element.get("data-information-zone")
                if path.role in _CIRCUIT_INFORMATION_ROLES
                else _ZONE_BY_ROLE.get(path.role, "map_field")
            )
            zone = zones.get(zone_name) if zone_name else None
            if zone is None:
                failures.append(
                    f"path role {path.role!r} has no canonical zone assignment"
                )
            elif not _inside(path.bounds, zone):
                failures.append(
                    f"path role {path.role!r} leaves canonical zone {zone_name!r}"
                )

    map_zone = zones.get("map_field") if zones else None
    if map_zone is not None:
        field_area = (map_zone[2] - map_zone[0]) * (map_zone[3] - map_zone[1])
        field_paths = [path for path in paths if _inside(path.bounds, map_zone)]
        field_length = sum(path.length for path in field_paths)
        field_ink = sum(path.length * path.nib_mm for path in field_paths)
        density = field_length / field_area
        coverage = field_ink / field_area
        metrics.update(
            {
                "field_area_mm2": field_area,
                "field_path_length_mm": field_length,
                "field_ink_mm2_upper_bound": field_ink,
                "density_mm_per_mm2": density,
                "coverage": coverage,
            }
        )
        if density > MAX_DENSITY_MM_PER_MM2 + 1e-12:
            failures.append(
                f"map density {density:.4f} mm/mm2 exceeds {MAX_DENSITY_MM_PER_MM2:.2f}"
            )
        if coverage > MAX_COVERAGE + 1e-12:
            failures.append(
                f"map coverage {100 * coverage:.2f}% exceeds {100 * MAX_COVERAGE:.0f}%"
            )
        context_density = _mapping(
            _mapping(f1_rendering.get("context_features")).get("context_density_budget")
        )
        if context_density:
            if (
                context_density.get("policy")
                != (
                    "decoration-then-whole-unlabelled-source-feature-"
                    "hard-water-boundary-fallback-v2"
                )
                or context_density.get("labelled_context_features_protected")
                is not True
                or context_density.get("course_and_station_geometry_protected")
                is not True
                or context_density.get(
                    "whole_source_feature_groups_only_after_decoration"
                )
                is not True
            ):
                failures.append("context density-budget protection contract drifted")
            initial = _finite(context_density.get("initial_baseline_length_mm"))
            retained = _finite(context_density.get("retained_baseline_length_mm"))
            removed = _finite(context_density.get("removed_length_mm"))
            hard = _finite(context_density.get("hard_maximum_baseline_length_mm"))
            if (
                None in {initial, retained, removed, hard}
                or initial is None
                or retained is None
                or removed is None
                or hard is None
                or abs((initial - retained) - removed) > 1e-5
                or retained > hard + 1e-6
                or hard > field_area * MAX_DENSITY_MM_PER_MM2 + 1e-6
            ):
                failures.append("context density-budget arithmetic drifted")
            labelled_feature_ids = {
                str(path.element.get("data-feature-id") or "")
                for path in paths
                if path.role == "context-label"
            }
            source_context_ids = {
                str(feature.get("id"))
                for feature in _array(_model(event).get("context"))
                if isinstance(feature, dict) and feature.get("id")
            }
            for raw_decision in _array(context_density.get("decisions")):
                decision = _mapping(raw_decision)
                feature_id = str(decision.get("feature_id") or "")
                reason = str(decision.get("reason") or "")
                if reason == (
                    "paper-field-density-budget-qualified-track-boundary-group"
                ):
                    # Exact group, hard-gate necessity, Red retention, source
                    # qualification and arithmetic are checked independently
                    # in the boundary-emission audit above.
                    continue
                if feature_id not in source_context_ids:
                    failures.append("context density budget invents a source feature")
                    continue
                if reason == "paper-field-density-budget-source-feature":
                    if decision.get("whole_feature_group") is not True:
                        failures.append(
                            "density-cull source feature is not a whole group"
                        )
                    if feature_id in labelled_feature_ids:
                        failures.append(
                            "context density budget culled a labelled feature"
                        )
                    if any(
                        path.element.get("data-feature-id") == feature_id
                        and (
                            path.role.startswith("context-")
                            or path.role
                            in {
                                "host-road",
                                "water-stipple-dot",
                                "grass-symbol",
                                "woodland-symbol",
                                "runoff-hatch",
                                "gravel-stipple-dot",
                            }
                        )
                        for path in paths
                    ):
                        failures.append(
                            "density-cull whole source feature still emits geometry"
                        )
                elif reason == "paper-field-density-budget-decoration":
                    role = str(decision.get("role") or "")
                    if decision.get("whole_feature_group") is not False or role not in {
                        "runoff-hatch",
                        "gravel-stipple-dot",
                    }:
                        failures.append("density decoration decision is unsupported")
                    if any(
                        path.role == role
                        and path.element.get("data-feature-id") == feature_id
                        for path in paths
                    ):
                        failures.append("density-culled decoration is still emitted")
                else:
                    failures.append("context density budget has an unknown decision")

    down = sum(path.length for path in paths)
    up = 0.0
    margin_for_motion = _finite(page.get("margin_mm")) or 0.0
    current: tuple[float, float] | None = (margin_for_motion, margin_for_motion)
    for group in groups:
        for path in [item for item in paths if item.group is group]:
            for subpath in path.subpaths:
                if current is not None:
                    up += math.hypot(
                        subpath.start[0] - current[0], subpath.start[1] - current[1]
                    )
                current = subpath.end
    travel_ratio = up / down if down > 0.0 else math.inf
    metrics.update({"pen_down_mm": down, "pen_up_mm": up, "travel_ratio": travel_ratio})
    if travel_ratio > MAX_TRAVEL_RATIO + 1e-12:
        failures.append(
            f"document-order travel ratio {travel_ratio:.3f} exceeds {MAX_TRAVEL_RATIO:g}"
        )

    sheet = str(page.get("paper") or "").upper()
    maximum_seconds = MAX_PLOT_SECONDS_BY_SHEET.get(sheet)
    declared_seconds = _finite(
        _mapping(manifest.get("plot_summary")).get(
            "estimated_plot_seconds_including_pen_up"
        )
    )
    measured_seconds = down / 35.0 + up / 80.0
    metrics.update(
        {
            "sheet": sheet,
            "measured_plot_seconds": measured_seconds,
            "declared_plot_seconds": declared_seconds,
            "maximum_plot_seconds": maximum_seconds,
        }
    )
    if maximum_seconds is None:
        failures.append("manifest paper size has no F1 plot-time ceiling")
    elif measured_seconds > maximum_seconds + 1e-9:
        failures.append(
            f"measured {sheet} plot time {measured_seconds:.1f}s exceeds {maximum_seconds:.0f}s"
        )
    if declared_seconds is None or abs(declared_seconds - measured_seconds) > max(
        1.0, measured_seconds * 0.02
    ):
        failures.append("manifest plot-time estimate does not bind measured SVG motion")

    failures.extend(_split_parity_failures(series, root, manifest))
    return failures, metrics


def audit_f1_circuit_series(
    series_dir: Path,
    *,
    catalog_file: Path,
    expected_event_count: int = EXPECTED_EVENT_COUNT,
    event_ids: Sequence[str] | None = None,
    complete_release: bool = False,
) -> dict[str, Any]:
    """Audit an index-scoped pilot or an exact complete release matrix."""

    series = series_dir.resolve()
    catalog_path = catalog_file.resolve()
    catalog = _load_json(catalog_path)
    catalog_hash = _sha256_file(catalog_path)
    catalog_events = [_mapping(value) for value in _array(catalog.get("events"))]
    events = {_event_id(value): value for value in catalog_events}
    requested_event_ids = None if event_ids is None else list(event_ids)
    scope_failures: list[str] = []
    if complete_release and requested_event_ids is not None:
        scope_failures.append("--complete-release cannot be combined with --event")
    if requested_event_ids is not None:
        if not requested_event_ids:
            scope_failures.append("explicit pilot scope contains no event ids")
        if len(requested_event_ids) != len(set(requested_event_ids)):
            scope_failures.append("explicit pilot scope contains duplicate event ids")

    index_path = series / "index.json"
    if not index_path.is_file():
        if complete_release:
            scoped_event_ids = list(events)
            scope_mode = "complete-release"
        elif requested_event_ids is not None:
            scoped_event_ids = list(dict.fromkeys(requested_event_ids))
            scope_mode = "explicit-subset"
        else:
            scoped_event_ids = []
            scope_mode = "index-subset"
        global_failures = validate_f1_catalog(
            catalog,
            expected_event_count=expected_event_count,
            event_ids=scoped_event_ids,
        )
        return {
            "schema_version": 1,
            "artifact_kind": ARTIFACT_KIND,
            "passed": False,
            "technical_pass": False,
            "scope_mode": scope_mode,
            "scoped_event_ids": scoped_event_ids,
            "rights_hold": True,
            "physical_proof_hold": True,
            "commercial_release_authorized": False,
            "review_hold": True,
            "failures": [
                *global_failures,
                *scope_failures,
                "release index.json is absent",
            ],
            "results": [],
        }
    index = _load_json(index_path)
    entries = [_mapping(value) for value in _array(index.get("entries"))]
    indexed_event_ids = list(
        dict.fromkeys(
            str(entry.get("event_id") or entry.get("subject_id") or "")
            for entry in entries
            if str(entry.get("event_id") or entry.get("subject_id") or "")
        )
    )
    if complete_release:
        scoped_event_ids = list(events)
        scope_mode = "complete-release"
    elif requested_event_ids is not None:
        scoped_event_ids = list(dict.fromkeys(requested_event_ids))
        scope_mode = "explicit-subset"
    else:
        scoped_event_ids = indexed_event_ids
        scope_mode = "index-subset"
        if not scoped_event_ids:
            scope_failures.append("release index contains no event scope")

    global_failures = validate_f1_catalog(
        catalog,
        expected_event_count=expected_event_count,
        event_ids=scoped_event_ids,
    )
    global_failures.extend(scope_failures)
    if index.get("schema_version") != 1:
        global_failures.append("release index schema_version must be 1")
    if index.get("artifact_kind") != ARTIFACT_KIND:
        global_failures.append(f"release index artifact_kind must be {ARTIFACT_KIND}")
    if index.get("catalog_id") != catalog.get("catalog_id"):
        global_failures.append("release index catalog_id drifted from the catalog")
    if index.get("catalog_sha256") != catalog_hash:
        global_failures.append(
            "release index catalog_sha256 does not bind the catalog bytes"
        )
    requested_formats = _array(index.get("formats"))
    if not requested_formats or any(
        value not in FORMATS for value in requested_formats
    ):
        global_failures.append("release index formats is empty or unsupported")
    if len(requested_formats) != len(set(requested_formats)):
        global_failures.append("release index formats contains duplicates")
    matrix_formats = list(requested_formats)
    if complete_release:
        if tuple(requested_formats) != FORMATS:
            global_failures.append(
                "complete-release mode requires all six binding formats in order"
            )
        matrix_formats = list(FORMATS)
    catalog_sources = {
        str(source.get("id")): source
        for source in _array(catalog.get("sources"))
        if isinstance(source, dict) and source.get("id")
    }
    expected_pairs = {
        (event_id, str(format_id))
        for event_id in scoped_event_ids
        for format_id in matrix_formats
    }
    actual_pairs = {
        (
            str(entry.get("event_id") or entry.get("subject_id") or ""),
            str(entry.get("format_id") or entry.get("format") or ""),
        )
        for entry in entries
    }
    if actual_pairs != expected_pairs or len(entries) != len(expected_pairs):
        global_failures.append(
            f"{scope_mode} matrix is incomplete, out of scope, or duplicated "
            f"(expected {len(expected_pairs)}, found {len(entries)})"
        )
    artifact_ids = [
        str(entry.get("id") or entry.get("artifact_id") or "") for entry in entries
    ]
    if any(not value for value in artifact_ids) or len(artifact_ids) != len(
        set(artifact_ids)
    ):
        global_failures.append("artifact ids are absent or duplicated")
    output_paths: list[str] = []
    for entry in entries:
        for key in ("svg", "manifest", "png"):
            path = _resolve_output(series, _mapping(entry.get("outputs")).get(key))
            if path is not None:
                output_paths.append(str(path))
    if len(output_paths) != len(set(output_paths)):
        global_failures.append("two release artifacts overwrite/share an output path")

    results: list[dict[str, Any]] = []
    for entry in entries:
        event_id = str(entry.get("event_id") or entry.get("subject_id") or "")
        event = events.get(event_id)
        if event is None:
            failures = ["release entry has no matching catalog event"]
            metrics: dict[str, Any] = {}
        else:
            failures, metrics = _artifact_failures(
                series=series,
                entry=entry,
                event=event,
                catalog_hash=catalog_hash,
                catalog_sources=catalog_sources,
            )
        results.append(
            {
                "artifact_id": str(entry.get("id") or entry.get("artifact_id") or ""),
                "event_id": event_id,
                "format_id": str(entry.get("format_id") or entry.get("format") or ""),
                "passed": not failures,
                "failures": failures,
                "metrics": metrics,
            }
        )
    technical_pass = not global_failures and all(result["passed"] for result in results)
    return {
        "schema_version": 1,
        "artifact_kind": ARTIFACT_KIND,
        "catalog_id": catalog.get("catalog_id"),
        "catalog_sha256": catalog_hash,
        "expected_event_count": expected_event_count,
        "scope_mode": scope_mode,
        "scoped_event_ids": scoped_event_ids,
        "complete_release": complete_release,
        "requested_formats": requested_formats,
        "expected_artifact_count": len(expected_pairs),
        "artifact_count": len(entries),
        "passed": technical_pass,
        "technical_pass": technical_pass,
        "rights_hold": True,
        "physical_proof_hold": True,
        "commercial_release_authorized": False,
        "review_hold": True,
        "failures": global_failures,
        "results": results,
    }


def write_qa_artifacts(
    series_dir: Path, report: Mapping[str, Any]
) -> tuple[Path, Path]:
    json_path = series_dir / "qa-f1-circuit-series.json"
    markdown_path = series_dir / "qa-f1-circuit-series.md"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# F1 circuit atlas QA",
        "",
        (
            "Technical result: **"
            + ("PASS" if report.get("technical_pass") else "FAIL")
            + "**"
        ),
        "",
        f"Scope: `{report.get('scope_mode', 'unknown')}`",
        "",
        (
            "Release holds: "
            f"rights={'YES' if report.get('rights_hold') else 'NO'}, "
            f"physical proof={'YES' if report.get('physical_proof_hold') else 'NO'}, "
            "commercial authorization="
            f"{'YES' if report.get('commercial_release_authorized') else 'NO'}"
        ),
        "",
        f"Artifacts: {report.get('artifact_count', 0)} / {report.get('expected_artifact_count', 0)}",
        "",
    ]
    failures = _array(report.get("failures"))
    if failures:
        lines.extend(["## Release failures", ""])
        lines.extend(f"- {failure}" for failure in failures)
        lines.append("")
    lines.extend(
        [
            "## Artifacts",
            "",
            "| Artifact | Format | Result | Coverage | Density | Travel | Plot time |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for result in _array(report.get("results")):
        item = _mapping(result)
        metrics = _mapping(item.get("metrics"))
        coverage = _finite(metrics.get("coverage"))
        density = _finite(metrics.get("density_mm_per_mm2"))
        travel = _finite(metrics.get("travel_ratio"))
        seconds = _finite(metrics.get("measured_plot_seconds"))
        lines.append(
            "| "
            + " | ".join(
                (
                    str(item.get("artifact_id") or ""),
                    str(item.get("format_id") or ""),
                    "PASS" if item.get("passed") else "HOLD",
                    "—" if coverage is None else f"{100 * coverage:.2f}%",
                    "—" if density is None else f"{density:.4f}",
                    "—" if travel is None else f"{travel:.3f}",
                    "—" if seconds is None else f"{seconds:.1f}s",
                )
            )
            + " |"
        )
        for failure in _array(item.get("failures")):
            lines.append(f"\n  - {failure}")
    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return json_path, markdown_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed semantic QA for an index-scoped circuit pilot or an "
            "exact complete release."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("series_dir", type=Path)
    parser.add_argument("--catalog-file", required=True, type=Path)
    parser.add_argument(
        "--expected-event-count", type=int, default=EXPECTED_EVENT_COUNT
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--event",
        action="append",
        help=(
            "Audit this event ID; repeatable. Without this option, pilot scope "
            "is inferred from index entries."
        ),
    )
    scope.add_argument(
        "--complete-release",
        action="store_true",
        help="Require every catalog event in all six binding formats.",
    )
    parser.add_argument("--write-reports", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = audit_f1_circuit_series(
            args.series_dir,
            catalog_file=args.catalog_file,
            expected_event_count=args.expected_event_count,
            event_ids=args.event,
            complete_release=args.complete_release,
        )
        if args.write_reports:
            write_qa_artifacts(args.series_dir, report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["technical_pass"] else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"qa-f1-circuit-series: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
