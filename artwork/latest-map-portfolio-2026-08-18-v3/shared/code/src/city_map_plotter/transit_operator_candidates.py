"""Reproducible, non-approving evidence packs for national rail operators.

This module bridges the intentionally large gap between Network Rail Working
Timetable names and reviewed exact OSM track nodes.  It creates ranked review
candidates; it never emits an approved binding, chooses a nearest track, joins
nearby geometry, or claims that a physical railway is used by an operator.

The generated JSON is deliberately *not* accepted by
``transit_operator_manifest``.  A reviewer must turn its evidence into the
three sealed manifests required by ``compile_national_operator_network``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
import csv
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import importlib
import json
from math import isfinite
from pathlib import Path
import re
import unicodedata
from typing import Any, Literal

from .models import MapPlotterError
from .transit_operator_compile import (
    HT_IDENTITY_ASSEMBLY_POLICY,
    assemble_hull_trains_service_identities,
    wtt_schedule_ref,
    wtt_schedule_runs_on,
)
from .transit_rail_graph import OsmRailGraph
from .transit_wtt import SUPPORTED_OPERATOR_NAMES, WttArchive, WttScheduleRecord


CANDIDATE_POLICY_VERSION = "national-operator-review-candidates-v1"
SUPPORTED_STATION_STOP_TYPES = frozenset({"RLY", "RSE", "RPL"})
_STOP_TYPE_RANK = {"RLY": 0, "RSE": 1, "RPL": 2}
_NAMED_RAILWAY_VALUES = frozenset(
    {
        "buffer_stop",
        "halt",
        "junction",
        "railway_crossing",
        "station",
        "stop",
        "switch",
    }
)
_NAMED_PUBLIC_TRANSPORT_VALUES = frozenset({"station", "stop_position"})
_LIFECYCLE_KEYS = frozenset(
    {
        "abandoned",
        "construction",
        "demolished",
        "disused",
        "proposed",
        "razed",
        "removed",
    }
)
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WORD_RE = re.compile(r"[a-z0-9]+")
_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*")
_GENERIC_STATION_SUFFIX_RE = re.compile(
    r"\b(?:railway\s+station|rail\s+station|train\s+station|station|stn)\Z"
)
_QUALIFIER_RE = re.compile(r"\b(?:for|via)\b.*\Z")


@dataclass(frozen=True, slots=True)
class StationCoordinateRecord:
    atco_code: str
    stop_type: Literal["RLY", "RSE", "RPL"]
    common_name: str
    short_common_name: str | None
    locality_name: str | None
    town: str | None
    lon: float
    lat: float
    modification_datetime: str | None


@dataclass(frozen=True, slots=True)
class NamedRailNodeRecord:
    osm_node_id: int
    name: str
    lon: float
    lat: float
    railway: str | None
    public_transport: str | None
    ref: str | None
    operator: str | None
    osm_version: int | None
    osm_timestamp: str | None


@dataclass(frozen=True, slots=True)
class CandidateIndexes:
    naptan_exact: Mapping[str, tuple[StationCoordinateRecord, ...]]
    naptan_relaxed: Mapping[str, tuple[StationCoordinateRecord, ...]]
    osm_exact: Mapping[str, tuple[NamedRailNodeRecord, ...]]
    osm_relaxed: Mapping[str, tuple[NamedRailNodeRecord, ...]]


def sha256_file(path: Path) -> tuple[str, int]:
    """Hash a source file without loading it into memory."""

    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Cannot hash candidate source {path}: {exc}") from exc
    return digest.hexdigest(), byte_count


def _require_source_hash(path: Path, expected_sha256: str) -> int:
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise MapPlotterError("Expected source SHA-256 must be lowercase hexadecimal.")
    actual, byte_count = sha256_file(path)
    if actual != expected_sha256:
        raise MapPlotterError(
            f"Candidate source SHA-256 mismatch for {path}: expected "
            f"{expected_sha256}, got {actual}."
        )
    return byte_count


def normalise_timing_location(value: str) -> str:
    """Canonical exact-match key, with only mechanical rail abbreviations."""

    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = text.casefold().replace("&", " and ")
    words = _WORD_RE.findall(text)
    expansions = {
        "jcn": "junction",
        "jn": "junction",
        "nth": "north",
        "sth": "south",
        "stn": "station",
    }
    expanded = [expansions.get(word, word) for word in words]
    result = " ".join(expanded)
    # NaPTAN public names commonly add a generic object suffix that is not part
    # of the WTT operational location label.  Nothing geographic is removed.
    result = _GENERIC_STATION_SUFFIX_RE.sub("", result).strip()
    return result


def relaxed_location_key(value: str) -> str:
    """A weaker discovery key; results using it always require review."""

    without_parenthetical = _PARENTHETICAL_RE.sub(" ", value)
    primary = normalise_timing_location(without_parenthetical)
    return _QUALIFIER_RE.sub("", primary).strip()


def _optional_text(row: Mapping[str, str], key: str) -> str | None:
    value = row.get(key, "").strip()
    return value or None


def parse_naptan_station_coordinates(
    path: Path, *, expected_sha256: str
) -> tuple[StationCoordinateRecord, ...]:
    """Read active rail station/entrance/platform points from pinned NaPTAN CSV."""

    _require_source_hash(path, expected_sha256)
    records: dict[str, StationCoordinateRecord] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {
                "ATCOCode",
                "CommonName",
                "ShortCommonName",
                "LocalityName",
                "Town",
                "Longitude",
                "Latitude",
                "StopType",
                "ModificationDateTime",
                "Status",
            }
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                missing = sorted(required.difference(reader.fieldnames or ()))
                raise MapPlotterError(
                    "NaPTAN CSV is missing required columns: " + ", ".join(missing)
                )
            for row_number, row in enumerate(reader, start=2):
                stop_type = row["StopType"].strip().upper()
                if (
                    stop_type not in SUPPORTED_STATION_STOP_TYPES
                    or row["Status"].strip().casefold() != "active"
                ):
                    continue
                atco_code = row["ATCOCode"].strip()
                common_name = row["CommonName"].strip()
                if not atco_code or not common_name:
                    continue
                try:
                    lon = float(row["Longitude"])
                    lat = float(row["Latitude"])
                except ValueError:
                    continue
                if not (
                    isfinite(lon)
                    and isfinite(lat)
                    and -180 <= lon <= 180
                    and -90 <= lat <= 90
                ):
                    raise MapPlotterError(
                        f"NaPTAN row {row_number} has coordinates outside WGS84."
                    )
                if atco_code in records:
                    raise MapPlotterError(f"NaPTAN repeats active ATCOCode {atco_code}.")
                records[atco_code] = StationCoordinateRecord(
                    atco_code=atco_code,
                    stop_type=stop_type,  # type: ignore[arg-type]
                    common_name=common_name,
                    short_common_name=_optional_text(row, "ShortCommonName"),
                    locality_name=_optional_text(row, "LocalityName"),
                    town=_optional_text(row, "Town"),
                    lon=lon,
                    lat=lat,
                    modification_datetime=_optional_text(
                        row, "ModificationDateTime"
                    ),
                )
    except OSError as exc:
        raise MapPlotterError(f"Cannot read NaPTAN candidate source {path}: {exc}") from exc
    return tuple(
        sorted(
            records.values(),
            key=lambda item: (
                _STOP_TYPE_RANK[item.stop_type],
                item.common_name.casefold(),
                item.atco_code,
            ),
        )
    )


def _record_names(record: StationCoordinateRecord) -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            record.common_name,
            record.short_common_name,
        )
        if value
    )


def index_naptan_records(
    records: Iterable[StationCoordinateRecord],
) -> tuple[
    dict[str, tuple[StationCoordinateRecord, ...]],
    dict[str, tuple[StationCoordinateRecord, ...]],
]:
    exact: dict[str, list[StationCoordinateRecord]] = defaultdict(list)
    relaxed: dict[str, list[StationCoordinateRecord]] = defaultdict(list)
    for record in records:
        for name in _record_names(record):
            primary = normalise_timing_location(name)
            weak = relaxed_location_key(name)
            if primary and record not in exact[primary]:
                exact[primary].append(record)
            if weak and weak != primary and record not in relaxed[weak]:
                relaxed[weak].append(record)
    sort_key = lambda item: (  # noqa: E731 - shared deterministic ordering.
        _STOP_TYPE_RANK[item.stop_type],
        item.common_name.casefold(),
        item.atco_code,
    )
    return (
        {key: tuple(sorted(values, key=sort_key)) for key, values in exact.items()},
        {
            key: tuple(sorted(values, key=sort_key))
            for key, values in relaxed.items()
        },
    )


def _tags(value: Any) -> dict[str, str]:
    try:
        return {str(tag.k): str(tag.v) for tag in value.tags}
    except AttributeError:
        return {}


def _positive(value: str | None) -> bool:
    return value is not None and value.strip().casefold() not in _FALSE_VALUES


def _active_named_rail_node(tags: Mapping[str, str]) -> bool:
    if not tags.get("name", "").strip():
        return False
    railway = tags.get("railway", "").strip().casefold()
    public_transport = tags.get("public_transport", "").strip().casefold()
    if (
        railway not in _NAMED_RAILWAY_VALUES
        and public_transport not in _NAMED_PUBLIC_TRANSPORT_VALUES
    ):
        return False
    for key in _LIFECYCLE_KEYS:
        if _positive(tags.get(key)) or _positive(tags.get(f"{key}:railway")):
            return False
    return True


def stream_named_rail_node_candidates(
    path: Path,
    *,
    expected_sha256: str,
    target_names: Iterable[str],
    source_hash_already_verified: bool = False,
) -> tuple[NamedRailNodeRecord, ...]:
    """Stream named rail nodes matching WTT keys from the same pinned PBF."""

    try:
        resolved = path.resolve(strict=True)
        initial_stat = resolved.stat()
    except OSError as exc:
        raise MapPlotterError(f"Cannot inspect named-node PBF {path}: {exc}") from exc
    if source_hash_already_verified:
        if _SHA256_RE.fullmatch(expected_sha256) is None:
            raise MapPlotterError("Expected PBF SHA-256 is malformed.")
    else:
        _require_source_hash(path, expected_sha256)
    exact_targets = {normalise_timing_location(value) for value in target_names}
    relaxed_targets = {relaxed_location_key(value) for value in target_names}
    osmium = importlib.import_module("osmium")
    processor_type = getattr(osmium, "FileProcessor", None)
    key_filter_type = getattr(getattr(osmium, "filter", None), "KeyFilter", None)
    osm_types = getattr(osmium, "osm", None)
    if processor_type is None or key_filter_type is None or osm_types is None:
        raise MapPlotterError(
            "PyOsmium does not expose FileProcessor with the native key filter."
        )

    records: dict[int, NamedRailNodeRecord] = {}
    try:
        processor = processor_type(
            str(resolved), entities=osm_types.NODE
        ).with_filter(key_filter_type("name"))
        for value in processor:
            tags = _tags(value)
            if not _active_named_rail_node(tags):
                continue
            name = tags["name"].strip()
            if (
                normalise_timing_location(name) not in exact_targets
                and relaxed_location_key(name) not in relaxed_targets
            ):
                continue
            location = getattr(value, "location", None)
            try:
                if location is None or not location.valid():
                    continue
                lon = float(location.lon)
                lat = float(location.lat)
                node_id = int(value.id)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            version_raw = getattr(value, "version", None)
            timestamp_raw = getattr(value, "timestamp", None)
            version = int(version_raw) if version_raw else None
            timestamp = str(timestamp_raw) if timestamp_raw else None
            records[node_id] = NamedRailNodeRecord(
                osm_node_id=node_id,
                name=name,
                lon=lon,
                lat=lat,
                railway=tags.get("railway"),
                public_transport=tags.get("public_transport"),
                ref=tags.get("ref"),
                operator=tags.get("operator"),
                osm_version=version,
                osm_timestamp=timestamp,
            )
        final_stat = resolved.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise MapPlotterError(f"Cannot stream named rail nodes from {path}: {exc}") from exc
    signature = lambda value: (  # noqa: E731 - local immutable stat tuple.
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )
    if signature(initial_stat) != signature(final_stat):
        raise MapPlotterError("Named-node PBF changed during its streaming pass.")
    return tuple(sorted(records.values(), key=lambda item: item.osm_node_id))


def index_named_osm_records(
    records: Iterable[NamedRailNodeRecord],
) -> tuple[
    dict[str, tuple[NamedRailNodeRecord, ...]],
    dict[str, tuple[NamedRailNodeRecord, ...]],
]:
    exact: dict[str, list[NamedRailNodeRecord]] = defaultdict(list)
    relaxed: dict[str, list[NamedRailNodeRecord]] = defaultdict(list)
    for record in records:
        primary = normalise_timing_location(record.name)
        weak = relaxed_location_key(record.name)
        exact[primary].append(record)
        if weak and weak != primary:
            relaxed[weak].append(record)
    return (
        {
            key: tuple(sorted(values, key=lambda item: item.osm_node_id))
            for key, values in exact.items()
        },
        {
            key: tuple(sorted(values, key=lambda item: item.osm_node_id))
            for key, values in relaxed.items()
        },
    )


def schedules_for_service_date(
    archive: WttArchive, *, operator_code: str, service_date: date
) -> tuple[WttScheduleRecord, ...]:
    """Select applicable schedules while preserving the unmodified source audit."""

    if operator_code not in SUPPORTED_OPERATOR_NAMES:
        raise MapPlotterError(f"Unsupported national operator code {operator_code!r}.")
    selected = tuple(
        schedule
        for schedule in archive.schedules
        if schedule.operator_code == operator_code
        and wtt_schedule_runs_on(schedule, service_date)
    )
    if not selected:
        raise MapPlotterError(
            f"No {operator_code} WTT schedules run on {service_date.isoformat()}."
        )
    return selected


def _timing_inventory(
    schedules: Sequence[WttScheduleRecord],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    locations: dict[str, dict[str, Any]] = {}
    transitions: dict[tuple[str, str], dict[str, Any]] = {}
    totals: Counter[str] = Counter()
    for schedule in schedules:
        schedule_ref = wtt_schedule_ref(schedule)
        totals["schedule_count"] += 1
        for slice_index, route_slice in enumerate(schedule.route_slices):
            totals["route_slice_count"] += 1
            for point_index, point in enumerate(route_slice.timing_points):
                totals["timing_point_occurrence_count"] += 1
                record = locations.setdefault(
                    point.location,
                    {
                        "location": point.location,
                        "occurrence_count": 0,
                        "call_count": 0,
                        "pass_count": 0,
                        "slice_endpoint_count": 0,
                        "schedule_refs": set(),
                    },
                )
                record["occurrence_count"] += 1
                record["call_count"] += int(
                    point.arrival is not None or point.departure is not None
                )
                record["pass_count"] += int(point.pass_time is not None)
                record["slice_endpoint_count"] += int(
                    point_index in {0, len(route_slice.timing_points) - 1}
                )
                record["schedule_refs"].add(schedule_ref)
            for from_index, (first, second) in enumerate(
                zip(
                    route_slice.timing_points,
                    route_slice.timing_points[1:],
                    strict=False,
                )
            ):
                totals["transition_occurrence_count"] += 1
                key = (first.location, second.location)
                transition = transitions.setdefault(
                    key,
                    {
                        "from_location": first.location,
                        "to_location": second.location,
                        "occurrence_count": 0,
                        "example": {
                            "schedule_ref": schedule_ref,
                            "slice_index": slice_index,
                            "from_point_index": from_index,
                            "to_point_index": from_index + 1,
                        },
                    },
                )
                transition["occurrence_count"] += 1
    for record in locations.values():
        record["schedule_count"] = len(record.pop("schedule_refs"))
    totals["unique_timing_location_count"] = len(locations)
    totals["unique_directed_transition_count"] = len(transitions)
    return (
        locations,
        sorted(
            transitions.values(),
            key=lambda item: (item["from_location"], item["to_location"]),
        ),
        totals,
    )


def _candidate_source_record(
    record: StationCoordinateRecord,
    *,
    match_strength: Literal["canonical-exact", "relaxed-discovery"],
    graph: OsmRailGraph | None,
    radius_m: float,
    limit: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_kind": "naptan",
        "source_object": record.atco_code,
        "match_strength": match_strength,
        "stop_type": record.stop_type,
        "name": record.common_name,
        "lon": record.lon,
        "lat": record.lat,
        "modification_datetime": record.modification_datetime,
        "graph_node_candidates": [],
        "selected_graph_node_id": None,
        "review_state": "not-reviewed",
    }
    if graph is not None:
        result["graph_node_candidates"] = [
            asdict(candidate)
            for candidate in graph.nearest_node_candidates(
                record.lon,
                record.lat,
                max_distance_m=radius_m,
                limit=limit,
            )
        ]
    return result


def _candidate_osm_record(
    record: NamedRailNodeRecord,
    *,
    match_strength: Literal["canonical-exact", "relaxed-discovery"],
    graph: OsmRailGraph | None,
    radius_m: float,
    limit: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_kind": "osm-named-node",
        "source_object": f"osm-node/{record.osm_node_id}",
        "match_strength": match_strength,
        "name": record.name,
        "lon": record.lon,
        "lat": record.lat,
        "railway": record.railway,
        "public_transport": record.public_transport,
        "ref": record.ref,
        "operator": record.operator,
        "source_node_is_graph_node": False,
        "graph_node_candidates": [],
        "selected_graph_node_id": None,
        "review_state": "not-reviewed",
    }
    if graph is not None:
        if record.osm_node_id in graph.nodes:
            node = graph.nodes[record.osm_node_id]
            result["source_node_is_graph_node"] = True
            result["graph_node_candidates"] = [
                {
                    "osm_node_id": node.osm_node_id,
                    "lon": node.lon,
                    "lat": node.lat,
                    "distance_m": 0.0,
                    "incident_edge_ids": sorted(
                        edge.edge_id
                        for edge in graph.edges.values()
                        if record.osm_node_id in edge.node_ids
                    ),
                }
            ]
        else:
            result["graph_node_candidates"] = [
                asdict(candidate)
                for candidate in graph.nearest_node_candidates(
                    record.lon,
                    record.lat,
                    max_distance_m=radius_m,
                    limit=limit,
                )
            ]
    return result


def build_operator_candidate_document(
    archive: WttArchive,
    *,
    operator_code: str,
    service_date: date,
    indexes: CandidateIndexes,
    naptan_sha256: str,
    pbf_sha256: str,
    operator_scope_sha256: str,
    graph: OsmRailGraph | None = None,
    graph_search_radius_m: float = 500.0,
    graph_candidate_limit: int = 8,
    named_osm_scan_performed: bool = True,
) -> dict[str, Any]:
    """Build one deterministic review pack without making a selection claim."""

    if graph is not None and graph.source.sha256 != pbf_sha256:
        raise MapPlotterError("Candidate PBF SHA-256 does not match loaded graph source.")
    if graph_search_radius_m <= 0 or graph_candidate_limit <= 0:
        raise MapPlotterError("Graph candidate query parameters must be positive.")
    schedules = schedules_for_service_date(
        archive, operator_code=operator_code, service_date=service_date
    )
    location_inventory, transitions, totals = _timing_inventory(schedules)
    location_records: list[dict[str, Any]] = []
    coverage: Counter[str] = Counter()
    locations_with_any_graph_ids: set[str] = set()
    locations_with_single_graph_id: set[str] = set()
    locations_with_exact_coordinate: set[str] = set()

    for location, inventory in sorted(location_inventory.items()):
        primary = normalise_timing_location(location)
        weak = relaxed_location_key(location)
        exact_naptan = indexes.naptan_exact.get(primary, ())
        weak_naptan = (
            indexes.naptan_exact.get(weak, ()) + indexes.naptan_relaxed.get(weak, ())
            if weak != primary
            else indexes.naptan_relaxed.get(weak, ())
        )
        exact_osm = indexes.osm_exact.get(primary, ())
        weak_osm = (
            indexes.osm_exact.get(weak, ()) + indexes.osm_relaxed.get(weak, ())
            if weak != primary
            else indexes.osm_relaxed.get(weak, ())
        )

        candidates: list[dict[str, Any]] = []
        seen_sources: set[tuple[str, str]] = set()
        for strength, records in (
            ("canonical-exact", exact_naptan[:8]),
            ("canonical-exact", exact_osm[:8]),
            ("relaxed-discovery", weak_naptan[:8]),
            ("relaxed-discovery", weak_osm[:8]),
        ):
            for record in records:
                if isinstance(record, StationCoordinateRecord):
                    source_key = ("naptan", record.atco_code)
                    if source_key in seen_sources:
                        continue
                    candidate = _candidate_source_record(
                        record,
                        match_strength=strength,  # type: ignore[arg-type]
                        graph=graph,
                        radius_m=graph_search_radius_m,
                        limit=graph_candidate_limit,
                    )
                else:
                    source_key = ("osm", str(record.osm_node_id))
                    if source_key in seen_sources:
                        continue
                    candidate = _candidate_osm_record(
                        record,
                        match_strength=strength,  # type: ignore[arg-type]
                        graph=graph,
                        radius_m=graph_search_radius_m,
                        limit=graph_candidate_limit,
                    )
                seen_sources.add(source_key)
                candidates.append(candidate)

        exact_candidates = [
            item for item in candidates if item["match_strength"] == "canonical-exact"
        ]
        if exact_candidates:
            coverage["locations_with_canonical_exact_coordinate_candidate"] += 1
            locations_with_exact_coordinate.add(location)
        if candidates:
            coverage["locations_with_any_coordinate_candidate"] += 1
        else:
            coverage["locations_without_coordinate_candidate"] += 1
        graph_ids = {
            graph_candidate["osm_node_id"]
            for candidate in candidates
            for graph_candidate in candidate["graph_node_candidates"]
        }
        if graph_ids:
            locations_with_any_graph_ids.add(location)
            coverage["locations_with_graph_node_candidates"] += 1
            if len(graph_ids) == 1:
                locations_with_single_graph_id.add(location)
                coverage["locations_with_one_unique_graph_node_candidate"] += 1
            else:
                coverage["locations_with_ambiguous_graph_node_candidates"] += 1
        elif graph is not None:
            coverage["locations_without_graph_node_candidate"] += 1
        location_records.append(
            {
                **inventory,
                "canonical_key": primary,
                "relaxed_key": weak,
                "coordinate_candidates": candidates,
                "selected_coordinate_source_object": None,
                "selected_graph_node_id": None,
                "review_state": "not-reviewed",
            }
        )

    transition_occurrences_with_exact_coordinates = 0
    transition_occurrences_with_graph_candidates = 0
    transition_occurrences_with_single_graph_candidates = 0
    for transition in transitions:
        endpoints = {transition["from_location"], transition["to_location"]}
        count = int(transition["occurrence_count"])
        if endpoints.issubset(locations_with_exact_coordinate):
            transition_occurrences_with_exact_coordinates += count
        if endpoints.issubset(locations_with_any_graph_ids):
            transition_occurrences_with_graph_candidates += count
        if endpoints.issubset(locations_with_single_graph_id):
            transition_occurrences_with_single_graph_candidates += count

    classification_required = operator_code in {"GW", "SN"}
    service_identity_candidates: list[dict[str, Any]] = []
    if operator_code == "HT":
        schedules_by_ref = {
            wtt_schedule_ref(schedule): schedule for schedule in schedules
        }
        service_identity_candidates = [
            {
                "identity_ref": identity.identity_ref,
                "uid": identity.uid,
                "tid": identity.tid,
                "component_schedule_refs": list(identity.component_schedule_refs),
                "decision": None,
                "evidence_locator": None,
                "rationale": None,
                "review_state": "not-reviewed",
            }
            for identity in assemble_hull_trains_service_identities(
                schedules_by_ref
            )
        ]
    if graph is None:
        coverage["locations_without_graph_node_candidate"] = totals[
            "unique_timing_location_count"
        ]
    document: dict[str, Any] = {
        "schema_version": 1,
        "policy_version": CANDIDATE_POLICY_VERSION,
        "release_state": "candidate-not-reviewed",
        "approved": False,
        "operator_code": operator_code,
        "operator_name": SUPPORTED_OPERATOR_NAMES[operator_code],
        "service_date": service_date.isoformat(),
        "claims": {
            "wtt_presence": True,
            "coordinate_binding_approved": False,
            "operator_alignment_approved": False,
            "service_classification_approved": not classification_required,
            **(
                {"service_identity_selection_approved": False}
                if operator_code == "HT"
                else {}
            ),
            "invented_connector_count": 0,
            "proximity_join_count": 0,
        },
        "sources": {
            "wtt_archive": {
                "sha256": archive.audit.archive_sha256,
                "byte_count": archive.audit.archive_byte_count,
            },
            "naptan": {"sha256": naptan_sha256},
            "osm_pbf": {"sha256": pbf_sha256},
            "operator_scope_evidence": {"sha256": operator_scope_sha256},
            "rail_graph": (
                {
                    "source_sha256": graph.source.sha256,
                    "graph_sha256": graph.graph_sha256,
                    "source_timestamp": graph.source.source_timestamp,
                    "node_count": len(graph.nodes),
                    "edge_count": len(graph.edges),
                }
                if graph is not None
                else None
            ),
        },
        "candidate_policy": {
            "name_matching": [
                "canonical exact mechanical normalization",
                "relaxed parenthetical/for/via removal for discovery only",
            ],
            "graph_search_radius_m": graph_search_radius_m,
            "graph_candidate_limit": graph_candidate_limit,
            "nearest_candidate_is_not_a_selection": True,
            "operator_scope_pdf_is_not_geometry": True,
            "named_osm_scan_performed": named_osm_scan_performed,
        },
        "wtt_audit": dict(sorted(totals.items())),
        "coverage": {
            **dict(sorted(coverage.items())),
            "transition_occurrences_with_exact_endpoint_coordinates": (
                transition_occurrences_with_exact_coordinates
            ),
            "transition_occurrences_with_graph_candidates_at_both_endpoints": (
                transition_occurrences_with_graph_candidates
            ),
            "transition_occurrences_with_single_graph_candidate_at_both_endpoints": (
                transition_occurrences_with_single_graph_candidates
            ),
            "coordinate_binding_locations_reviewed": 0,
            "coordinate_binding_locations_remaining": totals[
                "unique_timing_location_count"
            ],
            "route_selection_occurrences_reviewed": 0,
            "route_selection_occurrences_remaining": totals[
                "transition_occurrence_count"
            ],
            "schedule_classifications_reviewed": 0,
            "schedule_classifications_remaining": (
                totals["schedule_count"] if classification_required else 0
            ),
            **(
                {
                    "service_identity_decisions_reviewed": 0,
                    "service_identity_decisions_remaining": len(
                        service_identity_candidates
                    ),
                }
                if operator_code == "HT"
                else {}
            ),
        },
        "timing_locations": location_records,
        "unique_directed_transitions": transitions,
        "classification_candidates": (
            [
                {
                    "schedule_ref": wtt_schedule_ref(schedule),
                    "service_class": None,
                    "review_state": "not-reviewed",
                    "reason": (
                        "Official operator map distinguishes service classes but "
                        "the PDF has not been converted into schedule-level evidence."
                    ),
                }
                for schedule in schedules
            ]
            if classification_required
            else []
        ),
        **(
            {
                "service_identity_candidate_policy": HT_IDENTITY_ASSEMBLY_POLICY,
                "service_identity_candidates": service_identity_candidates,
            }
            if operator_code == "HT"
            else {}
        ),
        "next_gate": (
            (
                "Review every dated Hull Trains UID/TID inclusion or exclusion, "
                "then "
                if operator_code == "HT"
                else ""
            )
            + "review every timing-location coordinate/track binding, then review "
            "one exact consecutive graph-edge path for every WTT transition "
            "occurrence. No national operator plate may be released before the "
            "sealed manifest compiler accepts all evidence."
        ),
    }
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    document["candidate_document_sha256"] = hashlib.sha256(payload).hexdigest()
    return document


def write_candidate_document(
    path: Path, document: Mapping[str, Any], *, overwrite: bool = False
) -> None:
    if path.exists() and not overwrite:
        raise MapPlotterError(f"Refusing to overwrite candidate document {path}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    try:
        path.write_text(payload + "\n", encoding="utf-8")
    except OSError as exc:
        raise MapPlotterError(f"Cannot write candidate document {path}: {exc}") from exc


__all__ = [
    "CANDIDATE_POLICY_VERSION",
    "CandidateIndexes",
    "NamedRailNodeRecord",
    "StationCoordinateRecord",
    "build_operator_candidate_document",
    "index_named_osm_records",
    "index_naptan_records",
    "normalise_timing_location",
    "parse_naptan_station_coordinates",
    "relaxed_location_key",
    "schedules_for_service_date",
    "sha256_file",
    "stream_named_rail_node_candidates",
    "write_candidate_document",
]
