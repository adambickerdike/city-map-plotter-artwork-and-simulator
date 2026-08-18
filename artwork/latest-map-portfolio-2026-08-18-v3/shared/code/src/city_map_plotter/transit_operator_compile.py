"""Fail-closed compiler for reviewed National Rail operator alignments.

Working Timetable observations, station coordinates, physical rail geometry,
and a human route decision are four different claims.  This module keeps those
claims separate until their hashes, dates, identifiers, and topology agree.  It
never snaps a timing point or chooses between rail paths on a caller's behalf.

The public compiler accepts already parsed, hash-audited WTT records and an
exact-node :class:`~city_map_plotter.transit_rail_graph.OsmRailGraph`.  The two
review manifests below are immutable, canonically hashable records.  Modifying
one after review invalidates its pin and compilation stops.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
from math import isfinite
import re
from typing import Any, Literal, NoReturn, TypeVar

from .models import MapPlotterError
from .transit import (
    ColourSpec,
    EdgeTraversal,
    ServicePattern,
    TransitEdge,
    TransitLine,
    TransitNetwork,
    TransitNode,
    TransitPen,
    TransitSource,
    canonical_contract_bytes,
    validate_transit_network,
)
from .transit_rail_graph import OsmRailGraph
from .transit_operator_registry import OPERATOR_REGISTRY
from .transit_wtt import (
    SUPPORTED_OPERATOR_NAMES,
    WttArchive,
    WttScheduleRecord,
)


OPERATOR_PRESENTATION: dict[str, tuple[str, str, str]] = {
    # These are explicitly house preview values, not claims of published
    # numeric corporate standards.  A release pipeline may replace them later.
    "GR": ("lner", "LNER", "#C94C53"),
    "GW": ("gwr", "Great Western Railway", "#396B5A"),
    "NT": ("northern", "Northern", "#444B7A"),
    "SN": ("southern", "Southern", "#78A85A"),
}
for _operator_code, _operator_name in SUPPORTED_OPERATOR_NAMES.items():
    if _operator_code in OPERATOR_PRESENTATION:
        continue
    _selector = "LE" if _operator_code == "SX" else _operator_code
    _product = OPERATOR_REGISTRY.resolve(_selector)
    _slug = _product.presentation.slug
    if len(_product.atoc_codes) > 1 or _operator_code in {
        *_product.legacy_ingestion_codes,
        "SX",
    }:
        _slug = f"{_slug}-{_operator_code.casefold()}"
    OPERATOR_PRESENTATION[_operator_code] = (
        _slug,
        _operator_name,
        _product.presentation.display_hex,
    )
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_STABLE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_STATION_KINDS = frozenset({"station", "terminal", "interchange"})
_NODE_KINDS = _STATION_KINDS | {"junction"}
_STATION_TIERS = frozenset({"local", "major", "interchange", "terminal"})
_SERVICE_CLASSES = frozenset({"regular", "seasonal", "limited"})
HT_IDENTITY_ASSEMBLY_POLICY = "hull-trains-uid-tid-800-802-v1"
_DAY_INDEX = {"M": 0, "T": 1, "W": 2, "Th": 3, "F": 4, "S": 5, "Su": 6}


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    reviewer: str
    reviewed_at: str
    rationale: str
    status: Literal["approved"] = "approved"


@dataclass(frozen=True, slots=True)
class AnchorSelectionReview:
    review: ReviewRecord
    graph_sha256: str
    selected_node_id: int
    candidate_node_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TimingPointBinding:
    """One exact WTT location-to-OSM-node decision.

    ``candidate_node_ids`` is the complete ordered result of the declared
    nearest-node query.  If it has more than one member, ``anchor_review`` is
    mandatory and must repeat that exact candidate set and selected node.
    """

    location: str
    lon: float
    lat: float
    coordinate_source_object: str
    graph_node_id: int
    candidate_node_ids: tuple[int, ...]
    search_radius_m: float
    candidate_limit: int
    node_kind: Literal["junction", "station", "terminal", "interchange"]
    name: str | None = None
    station_tier: str | None = None
    anchor_review: AnchorSelectionReview | None = None


@dataclass(frozen=True, slots=True)
class CoordinateBindingManifest:
    operator_code: str
    wtt_archive_sha256: str
    graph_source_sha256: str
    graph_sha256: str
    graph_source_timestamp: str
    coordinate_source_sha256: str
    bindings: tuple[TimingPointBinding, ...]
    review: ReviewRecord
    manifest_sha256: str = ""


@dataclass(frozen=True, slots=True)
class PathSelectionReview:
    review: ReviewRecord
    graph_sha256: str
    path_sha256: str
    edge_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransitionSelection:
    """Reviewed alignment for one consecutive pair in one WTT route slice."""

    schedule_ref: str
    slice_index: int
    from_point_index: int
    to_point_index: int
    from_location: str
    to_location: str
    start_node_id: int
    end_node_id: int
    method: Literal["explicit-edge-list", "reviewed-unique-shortest"]
    edge_ids: tuple[str, ...]
    path_sha256: str
    path_review: PathSelectionReview | None = None


@dataclass(frozen=True, slots=True)
class RouteSelectionManifest:
    operator_code: str
    service_date: date
    wtt_archive_sha256: str
    graph_source_sha256: str
    graph_sha256: str
    graph_source_timestamp: str
    bindings_manifest_sha256: str
    transitions: tuple[TransitionSelection, ...]
    review: ReviewRecord
    manifest_sha256: str = ""


@dataclass(frozen=True, slots=True)
class ScopedRouteSelectionManifest:
    """HT route review pinned to one dated customer-service scope decision."""

    operator_code: str
    service_date: date
    wtt_archive_sha256: str
    graph_source_sha256: str
    graph_sha256: str
    graph_source_timestamp: str
    bindings_manifest_sha256: str
    service_identity_selection_manifest_sha256: str
    transitions: tuple[TransitionSelection, ...]
    review: ReviewRecord
    manifest_sha256: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleClassification:
    schedule_ref: str
    service_class: Literal["regular", "seasonal", "limited"]
    rationale: str
    seasonal: bool | None = None
    limited: bool | None = None


@dataclass(frozen=True, slots=True)
class ServiceClassificationManifest:
    operator_code: str
    service_date: date
    wtt_archive_sha256: str
    entries: tuple[ScheduleClassification, ...]
    review: ReviewRecord
    manifest_sha256: str = ""


@dataclass(frozen=True, slots=True)
class WttServiceIdentity:
    """One customer train identity assembled from exact WTT observations.

    Hull Trains publishes the same UID/TID in complementary ``800`` and
    ``802`` route books.  The component schedules and their slices remain
    separate evidence; assembly groups them under one customer identity and
    never joins route geometry.
    """

    identity_ref: str
    uid: str
    tid: str
    component_schedule_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceIdentityDecision:
    identity_ref: str
    uid: str
    tid: str
    component_schedule_refs: tuple[str, ...]
    decision: Literal["included", "excluded"]
    evidence_locator: str
    rationale: str


@dataclass(frozen=True, slots=True)
class ServiceIdentitySelectionManifest:
    """Sealed dated decision for advertised versus excluded train identities."""

    operator_code: str
    service_date: date
    wtt_archive_sha256: str
    scope_source_sha256: str
    assembly_policy: str
    included_identity_count: int
    excluded_identity_count: int
    entries: tuple[ServiceIdentityDecision, ...]
    review: ReviewRecord
    manifest_sha256: str = ""


@dataclass(frozen=True, slots=True)
class OperatorSourceSet:
    """Contract source records in explicit semantic roles."""

    wtt: TransitSource
    rail_graph: TransitSource
    station_coordinates: TransitSource
    coordinate_bindings: TransitSource
    route_selection: TransitSource
    classification: TransitSource | None = None
    service_scope: TransitSource | None = None
    service_identity_selection: TransitSource | None = None


@dataclass(frozen=True, slots=True)
class OperatorCompilePolicy:
    max_graph_age_days: int = 183
    max_future_graph_days: int = 31
    format_id: str = "a3-portrait"


_ManifestT = TypeVar(
    "_ManifestT",
    CoordinateBindingManifest,
    RouteSelectionManifest,
    ScopedRouteSelectionManifest,
    ServiceClassificationManifest,
    ServiceIdentitySelectionManifest,
)


def _manifest_document(value: Any) -> dict[str, Any]:
    document = asdict(value)
    document.pop("manifest_sha256", None)

    def normalise(item: Any) -> Any:
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, tuple):
            return [normalise(child) for child in item]
        if isinstance(item, list):
            return [normalise(child) for child in item]
        if isinstance(item, dict):
            return {str(key): normalise(child) for key, child in item.items()}
        return item

    result = normalise(document)
    if not isinstance(result, dict):  # pragma: no cover - dataclass invariant.
        raise AssertionError("Manifest did not normalize to an object.")
    return result


def manifest_payload_sha256(value: _ManifestT) -> str:
    """Hash all manifest fields except its own pin in canonical JSON form."""

    payload = json.dumps(
        _manifest_document(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def seal_coordinate_bindings(
    value: CoordinateBindingManifest,
) -> CoordinateBindingManifest:
    return replace(value, manifest_sha256=manifest_payload_sha256(value))


def seal_route_selections(value: RouteSelectionManifest) -> RouteSelectionManifest:
    return replace(value, manifest_sha256=manifest_payload_sha256(value))


def seal_scoped_route_selections(
    value: ScopedRouteSelectionManifest,
) -> ScopedRouteSelectionManifest:
    return replace(value, manifest_sha256=manifest_payload_sha256(value))


def seal_service_classifications(
    value: ServiceClassificationManifest,
) -> ServiceClassificationManifest:
    return replace(value, manifest_sha256=manifest_payload_sha256(value))


def seal_service_identity_selection(
    value: ServiceIdentitySelectionManifest,
) -> ServiceIdentitySelectionManifest:
    return replace(value, manifest_sha256=manifest_payload_sha256(value))


def wtt_schedule_ref(schedule: WttScheduleRecord) -> str:
    """Stable contract-safe identity for a precise WTT schedule observation."""

    document = {
        "uid": schedule.uid,
        "tid": schedule.tid,
        "operator_code": schedule.operator_code,
        "origin": schedule.origin.raw,
        "destination": schedule.destination.raw,
        "start_date": schedule.start_date.isoformat(),
        "end_date": schedule.end_date.isoformat(),
        "running_days": schedule.running_days,
        "timing_load": schedule.timing_load,
        "service_code": schedule.service_code,
    }
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    suffix = hashlib.sha256(payload).hexdigest()[:16]
    return f"{schedule.uid.casefold()}-{schedule.tid.casefold()}-{suffix}"


def _wtt_service_identity_document(schedule: WttScheduleRecord) -> dict[str, str]:
    """Return the exact header fields shared by complementary route books."""

    return {
        "uid": schedule.uid,
        "tid": schedule.tid,
        "operator_code": schedule.operator_code,
        "operator_name": schedule.operator_name,
        "origin": schedule.origin.raw,
        "destination": schedule.destination.raw,
        "start_date": schedule.start_date.isoformat(),
        "end_date": schedule.end_date.isoformat(),
        "running_days": schedule.running_days,
        "service_code": schedule.service_code or "",
    }


def wtt_service_identity_ref(schedule: WttScheduleRecord) -> str:
    """Return a stable UID/TID identity that deliberately excludes timing load."""

    payload = json.dumps(
        _wtt_service_identity_document(schedule),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    suffix = hashlib.sha256(payload).hexdigest()[:16]
    return f"{schedule.uid.casefold()}-{schedule.tid.casefold()}-{suffix}"


def assemble_hull_trains_service_identities(
    schedules: dict[str, WttScheduleRecord],
) -> tuple[WttServiceIdentity, ...]:
    """Group exact HT ``800``/``802`` observations without joining their paths.

    The current public WTT stores complementary route-book slices under two
    timing loads.  This function accepts only one internally consistent pair
    for every UID/TID.  A different load inventory, disagreeing header, or
    stale schedule key fails instead of being guessed into one train.
    """

    grouped: dict[tuple[str, str], list[tuple[str, WttScheduleRecord]]] = {}
    for schedule_ref, schedule in schedules.items():
        if schedule_ref != wtt_schedule_ref(schedule):
            _fail(f"HT schedule key {schedule_ref!r} is stale/tampered.")
        if schedule.operator_code != "HT":
            _fail("Hull Trains identity assembly received another operator.")
        grouped.setdefault((schedule.uid, schedule.tid), []).append(
            (schedule_ref, schedule)
        )

    identities: list[WttServiceIdentity] = []
    for (uid, tid), components in sorted(grouped.items()):
        documents = {
            json.dumps(
                _wtt_service_identity_document(schedule),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for _, schedule in components
        }
        if len(documents) != 1:
            _fail(
                f"HT UID/TID {uid}/{tid} has disagreeing headers across route "
                "book observations."
            )
        by_load: dict[str, tuple[str, WttScheduleRecord]] = {}
        for schedule_ref, schedule in components:
            timing_load = (schedule.timing_load or "").strip()
            if timing_load in by_load:
                _fail(
                    f"HT UID/TID {uid}/{tid} repeats timing load "
                    f"{timing_load!r}."
                )
            by_load[timing_load] = (schedule_ref, schedule)
        if set(by_load) != {"800", "802"}:
            _fail(
                f"HT UID/TID {uid}/{tid} must have exactly complementary 800 "
                "and 802 observations."
            )
        ordered_refs = tuple(by_load[load][0] for load in ("800", "802"))
        identities.append(
            WttServiceIdentity(
                identity_ref=wtt_service_identity_ref(by_load["800"][1]),
                uid=uid,
                tid=tid,
                component_schedule_refs=ordered_refs,
            )
        )
    if not identities:
        _fail("Hull Trains identity assembly received no schedules.")
    return tuple(identities)


def rail_path_sha256(edge_ids: tuple[str, ...], node_ids: tuple[int, ...]) -> str:
    """Match the exact path-evidence digest used by the rail graph."""

    payload = json.dumps(
        {"edge_ids": list(edge_ids), "node_ids": list(node_ids)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"National operator compilation failed: {message}")


def _require_sha256(value: str, *, field: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        _fail(f"{field} must be one lowercase SHA-256 digest.")


def _parse_timestamp(value: str, *, field: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _fail(f"{field} must be an ISO-8601 timestamp.")
    if parsed.tzinfo is None:
        _fail(f"{field} must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def _validate_review(value: ReviewRecord, *, field: str) -> datetime:
    if value.status != "approved":
        _fail(f"{field} is not approved.")
    if not value.reviewer.strip() or not value.rationale.strip():
        _fail(f"{field} needs a reviewer and rationale.")
    return _parse_timestamp(value.reviewed_at, field=f"{field}.reviewed_at")


def _verify_manifest_pin(value: _ManifestT, *, field: str) -> None:
    _require_sha256(value.manifest_sha256, field=f"{field}.manifest_sha256")
    actual = manifest_payload_sha256(value)
    if actual != value.manifest_sha256:
        _fail(
            f"{field} was modified after review: expected payload "
            f"{value.manifest_sha256}, got {actual}."
        )


def _validate_source(value: TransitSource, *, field: str, service_date: date) -> None:
    # Reparse through the contract constructor so direct dataclass construction
    # cannot bypass stable IDs, dates, licence fields, or reuse status.
    parsed = TransitSource.from_dict(value.as_dict(), index=0)
    if parsed != value:
        _fail(f"{field} is not in canonical TransitSource form.")
    if value.valid_from is not None and service_date < date.fromisoformat(
        value.valid_from
    ):
        _fail(f"{field} is not yet valid on {service_date.isoformat()}.")
    if value.valid_to is not None and service_date > date.fromisoformat(value.valid_to):
        _fail(f"{field} expired before {service_date.isoformat()}.")


def _running_weekdays(value: str) -> frozenset[int]:
    """Decode the day tokens used by the published WTT route books.

    Network Rail defines EWD as Monday--Saturday and the ``O``/``X`` suffixes
    as "only"/"excepted".  Sunday is the explicit ``SU`` token.  An unknown
    form fails instead of being treated as an every-day service.
    """

    raw = value.strip()
    if raw == "EWD":
        return frozenset(range(6))
    if len(raw) < 2 or raw[-1] not in {"O", "X"}:
        _fail(f"WTT running-days value {value!r} is unsupported.")
    suffix = raw[-1]
    prefix = raw[:-1].replace("TH", "Th").replace("SU", "Su")
    tokens: list[str] = []
    index = 0
    while index < len(prefix):
        token = next(
            (
                candidate
                for candidate in ("Th", "Su", "M", "T", "W", "F", "S")
                if prefix.startswith(candidate, index)
            ),
            None,
        )
        if token is None:
            _fail(f"WTT running-days value {value!r} is unsupported.")
        tokens.append(token)
        index += len(token)
    if len(set(tokens)) != len(tokens):
        _fail(f"WTT running-days value {value!r} repeats a day token.")
    selected = {_DAY_INDEX[token] for token in tokens}
    if suffix == "O":
        return frozenset(selected)
    if 6 in selected:
        _fail(f"WTT running-days exception value {value!r} is unsupported.")
    return frozenset(set(range(6)) - selected)


def wtt_schedule_runs_on(schedule: WttScheduleRecord, service_date: date) -> bool:
    """Return whether a WTT schedule is applicable on one calendar date.

    A parsed WTT archive legitimately contains schedules for several date and
    running-day bands.  Selection belongs here, after the complete archive has
    been hash/provenance checked; callers must not rewrite the source audit to
    make a date-filtered tuple look like the original archive.
    """

    return (
        schedule.start_date <= service_date <= schedule.end_date
        and service_date.weekday() in _running_weekdays(schedule.running_days)
    )


def _validate_wtt_archive(
    archive: WttArchive, *, operator_code: str, service_date: date
) -> dict[str, WttScheduleRecord]:
    audit = archive.audit
    _require_sha256(audit.archive_sha256, field="WTT archive audit SHA-256")
    if audit.archive_byte_count <= 0:
        _fail("WTT archive audit records no source bytes.")
    if audit.schedule_count != len(archive.schedules):
        _fail("WTT schedule count disagrees with its archive audit.")
    route_slice_count = sum(
        len(schedule.route_slices) for schedule in archive.schedules
    )
    if audit.route_slice_count != route_slice_count:
        _fail("WTT route-slice count disagrees with its archive audit.")
    if audit.workbook_count != len(audit.workbooks):
        _fail("WTT workbook count disagrees with its archive audit.")
    if audit.worksheet_count != sum(len(item.sheets) for item in audit.workbooks):
        _fail("WTT worksheet count disagrees with its archive audit.")

    entry_by_path = {item.path: item for item in audit.entries if not item.is_directory}
    if len(entry_by_path) != sum(not item.is_directory for item in audit.entries):
        _fail("WTT archive audit repeats a member path.")
    workbook_by_path = {item.archive_path: item for item in audit.workbooks}
    if len(workbook_by_path) != len(audit.workbooks):
        _fail("WTT archive audit repeats a workbook path.")
    for path, workbook in workbook_by_path.items():
        entry = entry_by_path.get(path)
        if entry is None or entry.sha256 != workbook.sha256:
            _fail(f"WTT workbook {path!r} has no matching outer-archive pin.")
        _require_sha256(workbook.sha256, field=f"WTT workbook {path!r} SHA-256")

    schedules: dict[str, WttScheduleRecord] = {}
    observed_schedule_refs: set[str] = set()
    for schedule in archive.schedules:
        if schedule.operator_code != operator_code:
            _fail(
                f"WTT input contains operator {schedule.operator_code!r}; exactly "
                f"one requested operator ({operator_code}) is allowed."
            )
        if schedule.operator_name != SUPPORTED_OPERATOR_NAMES[operator_code]:
            _fail(f"WTT schedule {schedule.uid} has an inconsistent operator name.")
        schedule_ref = wtt_schedule_ref(schedule)
        if schedule_ref in observed_schedule_refs:
            _fail(f"WTT input repeats schedule identity {schedule_ref!r}.")
        observed_schedule_refs.add(schedule_ref)
        if not schedule.route_slices:
            _fail(f"WTT schedule {schedule_ref!r} has no route-book slice.")
        for slice_index, route_slice in enumerate(schedule.route_slices):
            if len(route_slice.timing_points) < 2:
                _fail(
                    f"WTT schedule {schedule_ref!r} slice {slice_index} has fewer "
                    "than two timing points."
                )
            if not route_slice.provenance:
                _fail(
                    f"WTT schedule {schedule_ref!r} slice {slice_index} has no "
                    "column provenance."
                )
            for provenance in route_slice.provenance:
                matched_workbook = workbook_by_path.get(provenance.workbook_path)
                if (
                    matched_workbook is None
                    or matched_workbook.sha256 != provenance.workbook_sha256
                ):
                    _fail(
                        f"WTT slice provenance names stale workbook "
                        f"{provenance.workbook_path!r}."
                    )
                matching_sheets = [
                    sheet
                    for sheet in matched_workbook.sheets
                    if sheet.name == provenance.sheet_name
                    and sheet.part_path == provenance.sheet_part
                ]
                if len(matching_sheets) != 1:
                    _fail("WTT slice provenance does not identify exactly one sheet.")
                sheet = matching_sheets[0]
                if (
                    not sheet.fully_read
                    or sheet.sha256 is None
                    or sheet.sha256 != provenance.sheet_sha256
                ):
                    _fail("WTT slice provenance does not match a fully hashed sheet.")
        if wtt_schedule_runs_on(schedule, service_date):
            schedules[schedule_ref] = schedule
    if not schedules:
        _fail(
            f"WTT input has no schedules for operator {operator_code} running on "
            f"{service_date.isoformat()}."
        )
    return schedules


def _select_hull_trains_schedules(
    manifest: ServiceIdentitySelectionManifest,
    *,
    schedules: dict[str, WttScheduleRecord],
) -> tuple[dict[str, WttScheduleRecord], dict[str, str]]:
    """Apply one complete reviewed HT identity decision to exact WTT records."""

    if manifest.assembly_policy != HT_IDENTITY_ASSEMBLY_POLICY:
        _fail("HT service identity selection uses an unsupported assembly policy.")
    _require_sha256(
        manifest.scope_source_sha256,
        field="HT service identity selection scope source SHA-256",
    )
    for field, count in (
        ("included_identity_count", manifest.included_identity_count),
        ("excluded_identity_count", manifest.excluded_identity_count),
    ):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _fail(f"HT service identity selection {field} is invalid.")

    identities = assemble_hull_trains_service_identities(schedules)
    expected_refs = tuple(identity.identity_ref for identity in identities)
    observed_refs = tuple(entry.identity_ref for entry in manifest.entries)
    if len(observed_refs) != len(set(observed_refs)):
        _fail("HT service identity selection repeats an identity_ref.")
    if observed_refs != expected_refs:
        missing = sorted(set(expected_refs) - set(observed_refs))
        extra = sorted(set(observed_refs) - set(expected_refs))
        if missing:
            _fail(
                "HT service identity selection omits identities: "
                + ", ".join(missing[:8])
            )
        if extra:
            _fail(
                "HT service identity selection contains stale identities: "
                + ", ".join(extra[:8])
            )
        _fail("HT service identity selection is not in deterministic UID/TID order.")

    included: dict[str, WttScheduleRecord] = {}
    identity_by_schedule: dict[str, str] = {}
    included_count = 0
    excluded_count = 0
    for identity, entry in zip(identities, manifest.entries, strict=True):
        if (entry.uid, entry.tid) != (identity.uid, identity.tid):
            _fail(
                f"HT service identity {identity.identity_ref!r} has stale UID/TID."
            )
        if entry.component_schedule_refs != identity.component_schedule_refs:
            _fail(
                f"HT service identity {identity.identity_ref!r} has stale or "
                "reordered 800/802 components."
            )
        if entry.decision not in {"included", "excluded"}:
            _fail(
                f"HT service identity {identity.identity_ref!r} has an invalid "
                "inclusion decision."
            )
        if not entry.evidence_locator.strip() or not entry.rationale.strip():
            _fail(
                f"HT service identity {identity.identity_ref!r} needs an evidence "
                "locator and rationale."
            )
        if entry.decision == "included":
            included_count += 1
            for schedule_ref in identity.component_schedule_refs:
                included[schedule_ref] = schedules[schedule_ref]
                identity_by_schedule[schedule_ref] = identity.identity_ref
        else:
            excluded_count += 1

    if included_count != manifest.included_identity_count:
        _fail("HT included identity count disagrees with its reviewed entries.")
    if excluded_count != manifest.excluded_identity_count:
        _fail("HT excluded identity count disagrees with its reviewed entries.")
    if included_count + excluded_count != len(identities):
        _fail("HT identity decision counts do not cover the WTT inventory.")
    if not included:
        _fail("HT service identity selection excludes every dated train.")
    return included, identity_by_schedule


def _validate_sources(
    sources: OperatorSourceSet,
    *,
    service_date: date,
    archive: WttArchive,
    graph: OsmRailGraph,
    bindings: CoordinateBindingManifest,
    routes: RouteSelectionManifest | ScopedRouteSelectionManifest,
    classifications: ServiceClassificationManifest | None,
    service_identity_selection: ServiceIdentitySelectionManifest | None,
) -> tuple[TransitSource, ...]:
    values = [
        sources.wtt,
        sources.rail_graph,
        sources.station_coordinates,
        sources.coordinate_bindings,
        sources.route_selection,
    ]
    if sources.classification is not None:
        values.append(sources.classification)
    if sources.service_scope is not None:
        values.append(sources.service_scope)
    if sources.service_identity_selection is not None:
        values.append(sources.service_identity_selection)
    if len({item.id for item in values}) != len(values):
        _fail("semantic source roles must use distinct source IDs.")
    for index, source in enumerate(values):
        _validate_source(source, field=f"sources[{index}]", service_date=service_date)

    expected = (
        ("WTT", sources.wtt.sha256, archive.audit.archive_sha256),
        ("rail graph", sources.rail_graph.sha256, graph.source.sha256),
        (
            "station coordinates",
            sources.station_coordinates.sha256,
            bindings.coordinate_source_sha256,
        ),
        (
            "coordinate bindings",
            sources.coordinate_bindings.sha256,
            bindings.manifest_sha256,
        ),
        (
            "route selection",
            sources.route_selection.sha256,
            routes.manifest_sha256,
        ),
    )
    for label, actual, required in expected:
        if actual != required:
            _fail(f"{label} source SHA-256 does not match the compiled evidence.")
    if classifications is None:
        if sources.classification is not None:
            _fail("a classification source was supplied without a manifest.")
    elif (
        sources.classification is None
        or sources.classification.sha256 != classifications.manifest_sha256
    ):
        _fail("classification source SHA-256 does not match its manifest.")
    if service_identity_selection is None:
        if (
            sources.service_scope is not None
            or sources.service_identity_selection is not None
        ):
            _fail(
                "HT service-scope sources were supplied without an identity "
                "selection manifest."
            )
    elif (
        sources.service_scope is None
        or sources.service_scope.sha256
        != service_identity_selection.scope_source_sha256
    ):
        _fail("HT service-scope source SHA-256 does not match its manifest.")
    elif (
        sources.service_identity_selection is None
        or sources.service_identity_selection.sha256
        != service_identity_selection.manifest_sha256
    ):
        _fail("HT identity-selection source SHA-256 does not match its manifest.")
    return tuple(values)


def _validate_graph_freshness(
    graph: OsmRailGraph, *, service_date: date, policy: OperatorCompilePolicy
) -> datetime:
    if isinstance(policy.max_graph_age_days, bool) or policy.max_graph_age_days < 0:
        _fail("max_graph_age_days must be a non-negative integer.")
    if (
        isinstance(policy.max_future_graph_days, bool)
        or policy.max_future_graph_days < 0
    ):
        _fail("max_future_graph_days must be a non-negative integer.")
    timestamp = _parse_timestamp(
        graph.source.source_timestamp, field="rail graph source timestamp"
    )
    delta_days = (service_date - timestamp.date()).days
    if delta_days > policy.max_graph_age_days:
        _fail(
            f"rail graph source is stale by {delta_days} days; policy permits "
            f"{policy.max_graph_age_days}."
        )
    if -delta_days > policy.max_future_graph_days:
        _fail(
            f"rail graph source postdates the service date by {-delta_days} days; "
            f"policy permits {policy.max_future_graph_days}."
        )
    audit = graph.audit()
    if audit.get("invented_connector_count") != 0:
        _fail("rail graph audit does not prove zero invented connectors.")
    if audit.get("proximity_join_count") != 0:
        _fail("rail graph audit reports a proximity join.")
    if audit.get("edge_construction") != "consecutive-exact-osm-node-references-only":
        _fail("rail graph is not built from exact consecutive OSM node references.")
    return timestamp


def _validate_manifest_headers(
    *,
    operator_code: str,
    service_date: date,
    archive: WttArchive,
    graph: OsmRailGraph,
    graph_timestamp: datetime,
    bindings: CoordinateBindingManifest,
    routes: RouteSelectionManifest | ScopedRouteSelectionManifest,
    classifications: ServiceClassificationManifest | None,
    service_identity_selection: ServiceIdentitySelectionManifest | None,
) -> None:
    _verify_manifest_pin(bindings, field="coordinate binding manifest")
    if isinstance(routes, ScopedRouteSelectionManifest):
        _verify_manifest_pin(routes, field="route selection manifest")
    else:
        _verify_manifest_pin(routes, field="route selection manifest")
    binding_review_time = _validate_review(bindings.review, field="binding review")
    route_review_time = _validate_review(routes.review, field="route review")
    for label, manifest in (("bindings", bindings), ("routes", routes)):
        if manifest.operator_code != operator_code:
            _fail(f"{label} manifest has the wrong operator code.")
        if manifest.wtt_archive_sha256 != archive.audit.archive_sha256:
            _fail(f"{label} manifest names a stale WTT archive.")
        if manifest.graph_source_sha256 != graph.source.sha256:
            _fail(f"{label} manifest names a stale rail source.")
        if manifest.graph_sha256 != graph.graph_sha256:
            _fail(f"{label} manifest names a stale/tampered rail graph.")
        if manifest.graph_source_timestamp != graph.source.source_timestamp:
            _fail(f"{label} manifest names a different rail source timestamp.")
    if binding_review_time < graph_timestamp or route_review_time < graph_timestamp:
        _fail("binding and route reviews must postdate the reviewed graph snapshot.")
    if route_review_time < binding_review_time:
        _fail("route review predates the coordinate bindings it claims to use.")
    if routes.service_date != service_date:
        _fail("route selection manifest has the wrong service date.")
    if routes.bindings_manifest_sha256 != bindings.manifest_sha256:
        _fail("route selection manifest names stale coordinate bindings.")

    if operator_code == "HT":
        if not isinstance(routes, ScopedRouteSelectionManifest):
            _fail("HT requires a scope-pinned route selection manifest.")
        if service_identity_selection is None:
            _fail(
                "an explicit dated Hull Trains service identity selection "
                "manifest is required."
            )
        _verify_manifest_pin(
            service_identity_selection,
            field="HT service identity selection manifest",
        )
        identity_review_time = _validate_review(
            service_identity_selection.review,
            field="HT service identity selection review",
        )
        if service_identity_selection.operator_code != "HT":
            _fail("HT service identity selection has the wrong operator code.")
        if service_identity_selection.service_date != service_date:
            _fail("HT service identity selection has the wrong service date.")
        if (
            service_identity_selection.wtt_archive_sha256
            != archive.audit.archive_sha256
        ):
            _fail("HT service identity selection names a stale WTT archive.")
        if route_review_time < identity_review_time:
            _fail("route review predates the HT service identity selection.")
        if (
            routes.service_identity_selection_manifest_sha256
            != service_identity_selection.manifest_sha256
        ):
            _fail("route selection manifest names a stale HT identity selection.")
        if classifications is not None:
            _fail("HT uses a service identity selection, not a class manifest.")
    elif service_identity_selection is not None:
        _fail("service identity selection manifests currently apply only to HT.")
    elif isinstance(routes, ScopedRouteSelectionManifest):
        _fail("scope-pinned route selections currently apply only to HT.")

    if operator_code in {"GW", "SN"} and classifications is None:
        required = "GWR seasonal" if operator_code == "GW" else "Southern limited"
        _fail(f"an explicit {required} classification manifest is required.")
    if classifications is not None:
        _verify_manifest_pin(classifications, field="classification manifest")
        review_time = _validate_review(
            classifications.review, field="classification review"
        )
        if review_time < graph_timestamp:
            _fail("classification review predates the graph snapshot.")
        if classifications.operator_code != operator_code:
            _fail("classification manifest has the wrong operator code.")
        if classifications.service_date != service_date:
            _fail("classification manifest has the wrong service date.")
        if classifications.wtt_archive_sha256 != archive.audit.archive_sha256:
            _fail("classification manifest names a stale WTT archive.")


def _validate_bindings(
    manifest: CoordinateBindingManifest,
    *,
    graph: OsmRailGraph,
    schedules: dict[str, WttScheduleRecord],
) -> dict[str, TimingPointBinding]:
    required_locations = {
        point.location
        for schedule in schedules.values()
        for route_slice in schedule.route_slices
        for point in route_slice.timing_points
    }
    records: dict[str, TimingPointBinding] = {}
    selected_node_bindings: dict[int, TimingPointBinding] = {}
    for binding in manifest.bindings:
        location = binding.location.strip()
        if not location or location != binding.location:
            _fail("timing-point binding locations must be non-empty canonical text.")
        if location in records:
            _fail(f"coordinate bindings repeat WTT location {location!r}.")
        if binding.node_kind not in _NODE_KINDS:
            _fail(f"binding {location!r} has an unsupported node kind.")
        if binding.node_kind in _STATION_KINDS and not (binding.name or "").strip():
            _fail(f"station binding {location!r} needs a display name.")
        if binding.node_kind == "junction" and binding.station_tier is not None:
            _fail(f"junction binding {location!r} cannot have a station tier.")
        if (
            binding.station_tier is not None
            and binding.station_tier not in _STATION_TIERS
        ):
            _fail(f"binding {location!r} has an unsupported station tier.")
        if (
            not isfinite(binding.lon)
            or not isfinite(binding.lat)
            or not -180 <= binding.lon <= 180
            or not -90 <= binding.lat <= 90
        ):
            _fail(f"binding {location!r} has invalid WGS84 coordinates.")
        if not binding.coordinate_source_object.strip():
            _fail(f"binding {location!r} has no coordinate source object.")
        if (
            isinstance(binding.candidate_limit, bool)
            or binding.candidate_limit < 2
            or binding.candidate_limit > 256
        ):
            _fail(f"binding {location!r} has an invalid candidate limit.")
        candidates = graph.nearest_node_candidates(
            binding.lon,
            binding.lat,
            max_distance_m=binding.search_radius_m,
            limit=binding.candidate_limit,
        )
        if not candidates:
            _fail(f"binding {location!r} has no exact rail-node candidate.")
        if len(candidates) == binding.candidate_limit:
            _fail(
                f"binding {location!r} exhausted its candidate limit; completeness "
                "is not proved."
            )
        candidate_ids = tuple(item.osm_node_id for item in candidates)
        if candidate_ids != binding.candidate_node_ids:
            _fail(f"binding {location!r} candidate evidence is stale/tampered.")
        if binding.graph_node_id not in candidate_ids:
            _fail(f"binding {location!r} selects a node outside its candidate set.")
        if len(candidate_ids) > 1 and binding.anchor_review is None:
            _fail(f"binding {location!r} has ambiguous anchors without a review.")
        if binding.anchor_review is not None:
            anchor = binding.anchor_review
            _validate_review(anchor.review, field=f"binding {location!r} anchor review")
            if (
                anchor.graph_sha256 != graph.graph_sha256
                or anchor.selected_node_id != binding.graph_node_id
                or anchor.candidate_node_ids != candidate_ids
            ):
                _fail(f"binding {location!r} anchor review is stale/tampered.")
        previous = selected_node_bindings.get(binding.graph_node_id)
        if previous is not None and (
            previous.node_kind,
            previous.name,
            previous.station_tier,
        ) != (binding.node_kind, binding.name, binding.station_tier):
            _fail(
                f"WTT locations {previous.location!r} and {location!r} attach to "
                "one rail node with conflicting identities."
            )
        selected_node_bindings[binding.graph_node_id] = binding
        records[location] = binding

    missing = sorted(required_locations - set(records))
    extra = sorted(set(records) - required_locations)
    if missing:
        _fail("coordinate bindings omit timing points: " + ", ".join(missing[:8]))
    if extra:
        _fail(
            "coordinate bindings contain unused timing points: " + ", ".join(extra[:8])
        )
    return records


def _walk_explicit_edges(
    graph: OsmRailGraph,
    *,
    start_node_id: int,
    end_node_id: int,
    edge_ids: tuple[str, ...],
) -> tuple[int, ...]:
    if start_node_id == end_node_id:
        _fail("a timing-point transition collapses to one graph node.")
    if not edge_ids:
        _fail("a timing-point transition has no selected graph edges.")
    nodes = [start_node_id]
    current = start_node_id
    for edge_id in edge_ids:
        edge = graph.edges.get(edge_id)
        if edge is None:
            _fail(f"route selection names unknown graph edge {edge_id!r}.")
        if current == edge.source_from_node_id:
            current = edge.source_to_node_id
        elif current == edge.source_to_node_id:
            current = edge.source_from_node_id
        else:
            _fail(
                f"route edge {edge_id!r} is not consecutive after graph node {current}."
            )
        nodes.append(current)
    if current != end_node_id:
        _fail(
            f"selected graph edges finish at node {current}, not reviewed endpoint "
            f"{end_node_id}."
        )
    return tuple(nodes)


def _expected_transition_keys(
    schedules: dict[str, WttScheduleRecord],
) -> dict[tuple[str, int, int], tuple[str, str]]:
    expected: dict[tuple[str, int, int], tuple[str, str]] = {}
    for schedule_ref, schedule in schedules.items():
        for slice_index, route_slice in enumerate(schedule.route_slices):
            for point_index, (first, second) in enumerate(
                zip(route_slice.timing_points, route_slice.timing_points[1:])
            ):
                expected[(schedule_ref, slice_index, point_index)] = (
                    first.location,
                    second.location,
                )
    return expected


def _validate_routes(
    manifest: RouteSelectionManifest | ScopedRouteSelectionManifest,
    *,
    graph: OsmRailGraph,
    schedules: dict[str, WttScheduleRecord],
    bindings: dict[str, TimingPointBinding],
) -> dict[tuple[str, int, int], tuple[TransitionSelection, tuple[int, ...]]]:
    expected = _expected_transition_keys(schedules)
    selected: dict[
        tuple[str, int, int], tuple[TransitionSelection, tuple[int, ...]]
    ] = {}
    for transition in manifest.transitions:
        key = (
            transition.schedule_ref,
            transition.slice_index,
            transition.from_point_index,
        )
        if key in selected:
            _fail(f"route selections repeat transition {key!r}.")
        locations = expected.get(key)
        if locations is None:
            _fail(f"route selection contains unknown transition {key!r}.")
        if transition.to_point_index != transition.from_point_index + 1:
            _fail(f"route selection {key!r} does not cover consecutive timing points.")
        if locations != (transition.from_location, transition.to_location):
            _fail(f"route selection {key!r} location identity is stale/tampered.")
        start_node = bindings[transition.from_location].graph_node_id
        end_node = bindings[transition.to_location].graph_node_id
        if (start_node, end_node) != (
            transition.start_node_id,
            transition.end_node_id,
        ):
            _fail(f"route selection {key!r} names stale/tampered station anchors.")
        node_ids = _walk_explicit_edges(
            graph,
            start_node_id=start_node,
            end_node_id=end_node,
            edge_ids=transition.edge_ids,
        )
        path_digest = rail_path_sha256(transition.edge_ids, node_ids)
        if transition.path_sha256 != path_digest:
            _fail(f"route selection {key!r} path evidence is stale/tampered.")
        if transition.method == "reviewed-unique-shortest":
            route = graph.shortest_path(start_node, end_node)
            computed_edges = tuple(step.edge_id for step in route.steps)
            if computed_edges != transition.edge_ids or route.node_ids != node_ids:
                _fail(f"route selection {key!r} is not the unique shortest path.")
            path_review = transition.path_review
            if path_review is None:
                _fail(f"unique-shortest route {key!r} has no explicit path review.")
            _validate_review(path_review.review, field=f"route {key!r} path review")
            if (
                path_review.graph_sha256 != graph.graph_sha256
                or path_review.path_sha256 != path_digest
                or path_review.edge_ids != transition.edge_ids
            ):
                _fail(f"unique-shortest route {key!r} review is stale/tampered.")
        elif transition.method == "explicit-edge-list":
            if transition.path_review is not None:
                _fail(f"explicit route {key!r} carries an inapplicable path review.")
        else:
            _fail(f"route selection {key!r} uses an unsupported method.")
        selected[key] = (transition, node_ids)

    missing = sorted(set(expected) - set(selected))
    extra = sorted(set(selected) - set(expected))
    if missing:
        _fail(
            "route selections omit WTT timing-point transitions/branches: "
            + ", ".join(str(item) for item in missing[:8])
        )
    if extra:  # pragma: no cover - caught while iterating.
        _fail("route selections contain unknown transitions.")
    return selected


def _validate_classifications(
    manifest: ServiceClassificationManifest | None,
    *,
    operator_code: str,
    schedules: dict[str, WttScheduleRecord],
) -> dict[str, str]:
    if manifest is None:
        return {schedule_ref: "regular" for schedule_ref in schedules}
    records: dict[str, str] = {}
    for entry in manifest.entries:
        if entry.schedule_ref in records:
            _fail(f"classification repeats schedule {entry.schedule_ref!r}.")
        if entry.service_class not in _SERVICE_CLASSES or not entry.rationale.strip():
            _fail(f"classification {entry.schedule_ref!r} is incomplete.")
        if operator_code == "GW":
            if entry.seasonal is None or entry.limited is not None:
                _fail("every GWR classification must explicitly decide seasonal.")
            expected = "seasonal" if entry.seasonal else "regular"
            if entry.service_class != expected:
                _fail(f"GWR classification {entry.schedule_ref!r} is inconsistent.")
        elif operator_code == "SN":
            if entry.limited is None or entry.seasonal is not None:
                _fail("every Southern classification must explicitly decide limited.")
            expected = "limited" if entry.limited else "regular"
            if entry.service_class != expected:
                _fail(
                    f"Southern classification {entry.schedule_ref!r} is inconsistent."
                )
        elif entry.seasonal is not None or entry.limited is not None:
            _fail("seasonal/limited flags apply only to GWR/Southern manifests.")
        elif entry.service_class != "regular":
            _fail("LNER/Northern classifications may only use regular service class.")
        records[entry.schedule_ref] = entry.service_class
    missing = sorted(set(schedules) - set(records))
    extra = sorted(set(records) - set(schedules))
    if missing:
        _fail("classifications omit schedules: " + ", ".join(missing[:8]))
    if extra:
        _fail("classifications contain stale schedules: " + ", ".join(extra[:8]))
    return records


def _line_for_class(
    operator_code: str,
    service_class: str,
    *,
    order: int,
    source_ref: str,
    colour_source_ref: str,
) -> TransitLine:
    slug, name, colour = OPERATOR_PRESENTATION[operator_code]
    registry_selector = "LE" if operator_code == "SX" else operator_code
    physical = OPERATOR_REGISTRY.resolve(registry_selector).presentation
    suffix = "" if service_class == "regular" else f"-{service_class}"
    class_label = "" if service_class == "regular" else f" — {service_class.title()}"
    return TransitLine(
        id=f"{slug}{suffix}",
        name=f"{name}{class_label}",
        short_name=name,
        order=order,
        colour=ColourSpec(
            name=f"{name} house preview",
            display_hex=colour,
            role="operator-network",
            provenance="house-palette",
            numeric_value_status="house-value",
            source_ref=colour_source_ref,
        ),
        pen=TransitPen(
            ink=physical.ink,
            nominal_nib_mm=physical.nib_mm,
            match_status="nominal-unmeasured",
            pen_id=physical.pen_id,
            calibration_state="nominal-unmeasured",
            preview_hex=colour,
        ),
        service_class=service_class,
        source_ref=source_ref,
    )


def compile_national_operator_network(
    *,
    operator_code: str,
    service_date: date,
    wtt: WttArchive,
    graph: OsmRailGraph,
    bindings: CoordinateBindingManifest,
    route_selections: RouteSelectionManifest | ScopedRouteSelectionManifest,
    sources: OperatorSourceSet,
    classifications: ServiceClassificationManifest | None = None,
    service_identity_selection: ServiceIdentitySelectionManifest | None = None,
    policy: OperatorCompilePolicy = OperatorCompilePolicy(),
) -> TransitNetwork:
    """Compile a normalized operator contract only after every gate passes."""

    code = operator_code.strip().upper()
    if code not in OPERATOR_PRESENTATION or code not in SUPPORTED_OPERATOR_NAMES:
        _fail(
            "operator_code must be one of: "
            + ", ".join(sorted(OPERATOR_PRESENTATION))
            + "."
        )
    if not isinstance(service_date, date) or isinstance(service_date, datetime):
        _fail("service_date must be one calendar date.")
    graph_timestamp = _validate_graph_freshness(
        graph, service_date=service_date, policy=policy
    )
    dated_schedules = _validate_wtt_archive(
        wtt, operator_code=code, service_date=service_date
    )
    _validate_manifest_headers(
        operator_code=code,
        service_date=service_date,
        archive=wtt,
        graph=graph,
        graph_timestamp=graph_timestamp,
        bindings=bindings,
        routes=route_selections,
        classifications=classifications,
        service_identity_selection=service_identity_selection,
    )
    identity_by_schedule = {
        schedule_ref: schedule_ref for schedule_ref in dated_schedules
    }
    schedules = dated_schedules
    if code == "HT":
        if service_identity_selection is None:  # pragma: no cover - header gate.
            raise AssertionError("HT selection manifest gate was bypassed.")
        schedules, identity_by_schedule = _select_hull_trains_schedules(
            service_identity_selection,
            schedules=dated_schedules,
        )
    binding_by_location = _validate_bindings(bindings, graph=graph, schedules=schedules)
    selected_routes = _validate_routes(
        route_selections,
        graph=graph,
        schedules=schedules,
        bindings=binding_by_location,
    )
    class_by_schedule = _validate_classifications(
        classifications, operator_code=code, schedules=schedules
    )
    contract_sources = _validate_sources(
        sources,
        service_date=service_date,
        archive=wtt,
        graph=graph,
        bindings=bindings,
        routes=route_selections,
        classifications=classifications,
        service_identity_selection=service_identity_selection,
    )
    if not _STABLE_ID_RE.fullmatch(policy.format_id):
        _fail("policy.format_id is not a stable contract identifier.")

    service_classes = sorted(
        set(class_by_schedule.values()),
        key=lambda item: ("regular", "seasonal", "limited").index(item),
    )
    line_source_ref = (
        sources.service_identity_selection.id
        if (
            service_identity_selection is not None
            and sources.service_identity_selection is not None
        )
        else (
            sources.classification.id
            if classifications is not None and sources.classification is not None
            else sources.route_selection.id
        )
    )
    lines = tuple(
        _line_for_class(
            code,
            service_class,
            order=index,
            source_ref=line_source_ref,
            colour_source_ref=sources.route_selection.id,
        )
        for index, service_class in enumerate(service_classes)
    )
    line_id_by_class = {line.service_class: line.id for line in lines}

    used_graph_nodes: set[int] = set()
    edge_line_ids: dict[str, set[str]] = {}
    pattern_records: list[
        tuple[
            str,
            WttScheduleRecord,
            int,
            tuple[TransitionSelection, ...],
            tuple[int, ...],
        ]
    ] = []
    for schedule_ref, schedule in schedules.items():
        line_id = line_id_by_class[class_by_schedule[schedule_ref]]
        for slice_index, route_slice in enumerate(schedule.route_slices):
            transitions: list[TransitionSelection] = []
            route_nodes: list[int] = []
            for point_index in range(len(route_slice.timing_points) - 1):
                transition, node_ids = selected_routes[
                    (schedule_ref, slice_index, point_index)
                ]
                transitions.append(transition)
                if not route_nodes:
                    route_nodes.extend(node_ids)
                else:
                    if (
                        route_nodes[-1] != node_ids[0]
                    ):  # pragma: no cover - binding gate.
                        _fail("reviewed transitions do not form a consecutive slice.")
                    route_nodes.extend(node_ids[1:])
                for edge_id in transition.edge_ids:
                    edge_line_ids.setdefault(edge_id, set()).add(line_id)
            used_graph_nodes.update(route_nodes)
            pattern_records.append(
                (
                    schedule_ref,
                    schedule,
                    slice_index,
                    tuple(transitions),
                    tuple(route_nodes),
                )
            )

    bindings_by_node: dict[int, TimingPointBinding] = {}
    for binding in binding_by_location.values():
        bindings_by_node.setdefault(binding.graph_node_id, binding)
    nodes: list[TransitNode] = []
    for graph_node_id in sorted(used_graph_nodes):
        graph_node = graph.nodes[graph_node_id]
        selected_binding = bindings_by_node.get(graph_node_id)
        nodes.append(
            TransitNode(
                id=f"osm-node-{graph_node_id}",
                kind=(
                    selected_binding.node_kind
                    if selected_binding is not None
                    else "junction"
                ),
                lon=graph_node.lon,
                lat=graph_node.lat,
                source_ref=(
                    sources.coordinate_bindings.id
                    if selected_binding is not None
                    else sources.rail_graph.id
                ),
                name=selected_binding.name if selected_binding is not None else None,
                station_tier=(
                    selected_binding.station_tier
                    if selected_binding is not None
                    else None
                ),
                source_object=(
                    f"{selected_binding.coordinate_source_object};"
                    f"osm-node/{graph_node_id}"
                    if selected_binding is not None
                    else f"osm-node/{graph_node_id}"
                ),
            )
        )

    transit_edge_id: dict[str, str] = {}
    edges: list[TransitEdge] = []
    for edge_id in sorted(edge_line_ids):
        graph_edge = graph.edges[edge_id]
        first = graph.nodes[graph_edge.source_from_node_id]
        second = graph.nodes[graph_edge.source_to_node_id]
        normalized_id = (
            f"rail-{graph_edge.source_way_id}-{graph_edge.source_segment_index}"
        )
        transit_edge_id[edge_id] = normalized_id
        edges.append(
            TransitEdge(
                id=normalized_id,
                from_node=f"osm-node-{graph_edge.source_from_node_id}",
                to_node=f"osm-node-{graph_edge.source_to_node_id}",
                geometry=((first.lon, first.lat), (second.lon, second.lat)),
                line_ids=tuple(sorted(edge_line_ids[edge_id])),
                source_ref=sources.rail_graph.id,
                source_object=edge_id,
                status="operational-source-policy",
                grade=dict(graph_edge.tags).get("bridge")
                or dict(graph_edge.tags).get("tunnel")
                or "unknown",
            )
        )

    patterns: list[ServicePattern] = []
    for schedule_ref, schedule, slice_index, pattern_transitions, _ in pattern_records:
        traversals: list[EdgeTraversal] = []
        for transition in pattern_transitions:
            current = transition.start_node_id
            for edge_id in transition.edge_ids:
                graph_edge = graph.edges[edge_id]
                forward = current == graph_edge.source_from_node_id
                traversals.append(
                    EdgeTraversal(
                        edge_id=transit_edge_id[edge_id],
                        direction="forward" if forward else "reverse",
                    )
                )
                current = (
                    graph_edge.source_to_node_id
                    if forward
                    else graph_edge.source_from_node_id
                )
        route_slice = schedule.route_slices[slice_index]
        station_ids = tuple(
            f"osm-node-{binding_by_location[point.location].graph_node_id}"
            for point in route_slice.timing_points
            if binding_by_location[point.location].node_kind in _STATION_KINDS
        )
        patterns.append(
            ServicePattern(
                id=(
                    f"pattern-{identity_by_schedule[schedule_ref]}-"
                    f"{schedule.timing_load or 'no-load'}-slice-{slice_index}"
                    if code == "HT"
                    else f"pattern-{schedule_ref}-slice-{slice_index}"
                ),
                line_id=line_id_by_class[class_by_schedule[schedule_ref]],
                name=(
                    f"{schedule.origin.name} to {schedule.destination.name} "
                    f"({schedule.uid}/{schedule.tid}, route-book slice "
                    f"{slice_index + 1})"
                ),
                traversals=tuple(traversals),
                station_ids=station_ids,
                source_ref=sources.route_selection.id,
                valid_from=schedule.start_date.isoformat(),
                valid_to=schedule.end_date.isoformat(),
                derivation_status="wtt-order-reviewed-exact-osm-edges",
                continuity_breaks=(),
            )
        )

    slug, display_name, _ = OPERATOR_PRESENTATION[code]
    excluded_identity_entries = (
        [
            {
                "identity_ref": entry.identity_ref,
                "uid": entry.uid,
                "tid": entry.tid,
                "component_schedule_refs": list(entry.component_schedule_refs),
                "evidence_locator": entry.evidence_locator,
                "rationale": entry.rationale,
            }
            for entry in service_identity_selection.entries
            if entry.decision == "excluded"
        ]
        if service_identity_selection is not None
        else []
    )
    identity_omission = (
        {
            "kind": "hull-trains-service-identity-assembly",
            "status": "reviewed-date-specific-selection",
            "reason": (
                "Complementary 800/802 route-book observations are grouped by "
                "sealed UID/TID identity. Their geographic slices remain "
                "separate and no cross-route-book connector is invented."
            ),
            "included_identity_count": (
                service_identity_selection.included_identity_count
                if service_identity_selection is not None
                else 0
            ),
            "excluded_identity_count": (
                service_identity_selection.excluded_identity_count
                if service_identity_selection is not None
                else 0
            ),
            "assembly_policy": (
                service_identity_selection.assembly_policy
                if service_identity_selection is not None
                else ""
            ),
            "scope_source_ref": (
                sources.service_scope.id if sources.service_scope is not None else ""
            ),
            "service_identity_selection_manifest_sha256": (
                service_identity_selection.manifest_sha256
                if service_identity_selection is not None
                else ""
            ),
            "excluded_identities": excluded_identity_entries,
        }
        if code == "HT"
        else None
    )
    omissions: tuple[dict[str, Any], ...] = (
        {
            "kind": "cross-route-book-joining",
            "status": "intentionally-omitted",
            "reason": (
                "Disjoint WTT route-book observations are separate service "
                "patterns; the compiler never invents joins between them."
            ),
        },
    )
    if identity_omission is not None:
        omissions += (identity_omission,)
    identity_notes = (
        (
            "Hull Trains service-identity selection manifest SHA-256: "
            f"{service_identity_selection.manifest_sha256}."
        ),
    ) if service_identity_selection is not None else ()
    network = TransitNetwork(
        id=f"{slug}-national-{service_date.isoformat()}",
        name=f"{display_name} — {service_date.isoformat()}",
        kind="national-operator",
        scope="Reviewed WTT route-book slices on exact OSM rail topology",
        format_id=policy.format_id,
        snapshot=service_date.isoformat(),
        validity_status="date-specific-reviewed-source-pinned",
        geometry_mode="exact-osm-node-edge-reviewed-operator-alignment",
        sources=contract_sources,
        lines=lines,
        nodes=tuple(nodes),
        edges=tuple(edges),
        service_patterns=tuple(patterns),
        context=(),
        omissions=omissions,
        notes=(
            "Every WTT timing-point transition is backed by reviewed consecutive "
            "graph edge IDs.",
            "Invented connector count: 0; proximity join count: 0.",
            f"Rail graph SHA-256: {graph.graph_sha256}.",
            f"Route-selection manifest SHA-256: {route_selections.manifest_sha256}.",
        )
        + identity_notes,
        contract_sha256="",
    )
    validate_transit_network(network)
    digest = hashlib.sha256(canonical_contract_bytes(network)).hexdigest()
    return replace(network, contract_sha256=digest)
