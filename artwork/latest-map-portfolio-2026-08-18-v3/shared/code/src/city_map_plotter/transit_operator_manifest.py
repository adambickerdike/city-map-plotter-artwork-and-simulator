"""Strict JSON envelopes for reviewed national-operator manifests.

The compiler's dataclasses are convenient in memory; these helpers provide the
only supported reviewed-file representation.  They reject duplicate/unknown
keys, template markers, non-approved reviews, malformed scalar types, and
payloads changed after their canonical review hash was calculated.

Templates intentionally use a different ``release_state`` and invalid pins.
They cannot be loaded by this module or validated by the release schemas.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any, Callable, Literal, NoReturn, TypeVar

from .models import MapPlotterError
from .transit_operator_compile import (
    AnchorSelectionReview,
    CoordinateBindingManifest,
    PathSelectionReview,
    ReviewRecord,
    RouteSelectionManifest,
    ScheduleClassification,
    ScopedRouteSelectionManifest,
    ServiceIdentityDecision,
    ServiceIdentitySelectionManifest,
    ServiceClassificationManifest,
    TimingPointBinding,
    TransitionSelection,
    manifest_payload_sha256,
)


MANIFEST_JSON_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_OPERATORS = frozenset({"GR", "GW", "SN", "NT", "HT"})
_CLASSIFICATION_OPERATORS = frozenset({"GR", "GW", "SN", "NT"})
_NODE_KINDS = frozenset({"junction", "station", "terminal", "interchange"})
_STATION_TIERS = frozenset({"local", "major", "interchange", "terminal"})
_Manifest = TypeVar(
    "_Manifest",
    CoordinateBindingManifest,
    RouteSelectionManifest,
    ScopedRouteSelectionManifest,
    ServiceClassificationManifest,
    ServiceIdentitySelectionManifest,
)
_ManifestKind = Literal[
    "coordinate-bindings",
    "route-selections",
    "scoped-route-selections",
    "service-classifications",
    "service-identity-selections",
]


class OperatorManifestJsonError(MapPlotterError):
    """A reviewed-manifest JSON envelope is unsafe or malformed."""


def _fail(message: str) -> NoReturn:
    raise OperatorManifestJsonError(f"National operator manifest JSON: {message}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate key {key!r} is forbidden.")
        result[key] = value
    return result


def _decode(payload: bytes, *, source: str) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OperatorManifestJsonError(
            f"National operator manifest JSON: {source} is not UTF-8."
        ) from exc
    if text.startswith("\ufeff"):
        _fail(f"{source} must not contain a UTF-8 byte-order mark.")
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=lambda value: _fail(
                f"non-finite JSON number {value!r} is forbidden."
            ),
        )
    except OperatorManifestJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise OperatorManifestJsonError(
            f"National operator manifest JSON: {source} is invalid JSON: {exc}."
        ) from exc


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object.")
    return value


def _exact_keys(
    value: dict[str, Any],
    *,
    field: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        _fail(f"{field} is missing keys: {', '.join(missing)}.")
    if unknown:
        _fail(f"{field} contains unknown keys: {', '.join(unknown)}.")


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail(f"{field} must be non-empty canonical text.")
    return value


def _sha256(value: Any, *, field: str) -> str:
    text = _text(value, field=field)
    if _SHA256_RE.fullmatch(text) is None:
        _fail(f"{field} must be one lowercase SHA-256 digest.")
    return text


def _operator(value: Any, *, field: str) -> str:
    text = _text(value, field=field)
    if text not in _OPERATORS:
        _fail(f"{field} must be GR, GW, SN, NT, or HT.")
    return text


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{field} must be an integer greater than or equal to {minimum}.")
    return value


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{field} must be a finite number.")
    result = float(value)
    if not isfinite(result):
        _fail(f"{field} must be a finite number.")
    return result


def _list(value: Any, *, field: str, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        _fail(f"{field} must be a {qualifier}list.")
    return value


def _date(value: Any, *, field: str) -> date:
    text = _text(value, field=field)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise OperatorManifestJsonError(
            f"National operator manifest JSON: {field} must be an ISO date."
        ) from exc


def _timestamp(value: Any, *, field: str) -> str:
    text = _text(value, field=field)
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise OperatorManifestJsonError(
            f"National operator manifest JSON: {field} must be an ISO timestamp."
        ) from exc
    if parsed.tzinfo is None:
        _fail(f"{field} must include a UTC offset.")
    return text


def _review(value: Any, *, field: str) -> ReviewRecord:
    raw = _object(value, field=field)
    _exact_keys(
        raw,
        field=field,
        required={"reviewer", "reviewed_at", "rationale", "status"},
    )
    status = _text(raw["status"], field=f"{field}.status")
    if status != "approved":
        _fail(
            f"{field}.status must be 'approved'; non-reviewed templates cannot "
            "be loaded as release evidence."
        )
    return ReviewRecord(
        reviewer=_text(raw["reviewer"], field=f"{field}.reviewer"),
        reviewed_at=_timestamp(raw["reviewed_at"], field=f"{field}.reviewed_at"),
        rationale=_text(raw["rationale"], field=f"{field}.rationale"),
        status="approved",
    )


def _int_tuple(value: Any, *, field: str, nonempty: bool = False) -> tuple[int, ...]:
    result = tuple(
        _integer(item, field=f"{field}[{index}]", minimum=1)
        for index, item in enumerate(_list(value, field=field, nonempty=nonempty))
    )
    if len(result) != len(set(result)):
        _fail(f"{field} repeats an identifier.")
    return result


def _edge_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    return tuple(
        _text(item, field=f"{field}[{index}]")
        for index, item in enumerate(_list(value, field=field, nonempty=True))
    )


def _anchor_review(value: Any, *, field: str) -> AnchorSelectionReview:
    raw = _object(value, field=field)
    _exact_keys(
        raw,
        field=field,
        required={
            "review",
            "graph_sha256",
            "selected_node_id",
            "candidate_node_ids",
        },
    )
    return AnchorSelectionReview(
        review=_review(raw["review"], field=f"{field}.review"),
        graph_sha256=_sha256(raw["graph_sha256"], field=f"{field}.graph_sha256"),
        selected_node_id=_integer(
            raw["selected_node_id"], field=f"{field}.selected_node_id", minimum=1
        ),
        candidate_node_ids=_int_tuple(
            raw["candidate_node_ids"],
            field=f"{field}.candidate_node_ids",
            nonempty=True,
        ),
    )


def _binding(value: Any, *, field: str) -> TimingPointBinding:
    raw = _object(value, field=field)
    _exact_keys(
        raw,
        field=field,
        required={
            "location",
            "lon",
            "lat",
            "coordinate_source_object",
            "graph_node_id",
            "candidate_node_ids",
            "search_radius_m",
            "candidate_limit",
            "node_kind",
        },
        optional={"name", "station_tier", "anchor_review"},
    )
    lon = _number(raw["lon"], field=f"{field}.lon")
    lat = _number(raw["lat"], field=f"{field}.lat")
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        _fail(f"{field} coordinates lie outside WGS84.")
    graph_node_id = _integer(
        raw["graph_node_id"], field=f"{field}.graph_node_id", minimum=1
    )
    candidates = _int_tuple(
        raw["candidate_node_ids"],
        field=f"{field}.candidate_node_ids",
        nonempty=True,
    )
    if graph_node_id not in candidates:
        _fail(f"{field}.graph_node_id is absent from candidate_node_ids.")
    node_kind = _text(raw["node_kind"], field=f"{field}.node_kind")
    if node_kind not in _NODE_KINDS:
        _fail(f"{field}.node_kind is unsupported.")
    name = _text(raw["name"], field=f"{field}.name") if "name" in raw else None
    tier = (
        _text(raw["station_tier"], field=f"{field}.station_tier")
        if "station_tier" in raw
        else None
    )
    if tier is not None and tier not in _STATION_TIERS:
        _fail(f"{field}.station_tier is unsupported.")
    if node_kind == "junction" and (name is not None or tier is not None):
        _fail(f"{field} junction cannot carry station display fields.")
    if node_kind != "junction" and name is None:
        _fail(f"{field} station needs a name.")
    anchor = (
        _anchor_review(raw["anchor_review"], field=f"{field}.anchor_review")
        if "anchor_review" in raw
        else None
    )
    if len(candidates) > 1 and anchor is None:
        _fail(f"{field} has ambiguous candidates without anchor_review.")
    if anchor is not None and (
        anchor.selected_node_id != graph_node_id
        or anchor.candidate_node_ids != candidates
    ):
        _fail(f"{field}.anchor_review disagrees with its selection.")
    search_radius = _number(raw["search_radius_m"], field=f"{field}.search_radius_m")
    if search_radius <= 0:
        _fail(f"{field}.search_radius_m must be positive.")
    candidate_limit = _integer(
        raw["candidate_limit"], field=f"{field}.candidate_limit", minimum=2
    )
    if candidate_limit > 256:
        _fail(f"{field}.candidate_limit cannot exceed 256.")
    if len(candidates) >= candidate_limit:
        _fail(f"{field} candidate list does not prove an untruncated query.")
    return TimingPointBinding(
        location=_text(raw["location"], field=f"{field}.location"),
        lon=lon,
        lat=lat,
        coordinate_source_object=_text(
            raw["coordinate_source_object"],
            field=f"{field}.coordinate_source_object",
        ),
        graph_node_id=graph_node_id,
        candidate_node_ids=candidates,
        search_radius_m=search_radius,
        candidate_limit=candidate_limit,
        node_kind=node_kind,  # type: ignore[arg-type]
        name=name,
        station_tier=tier,
        anchor_review=anchor,
    )


def _coordinate_manifest(value: Any) -> CoordinateBindingManifest:
    raw = _object(value, field="manifest")
    _exact_keys(
        raw,
        field="manifest",
        required={
            "operator_code",
            "wtt_archive_sha256",
            "graph_source_sha256",
            "graph_sha256",
            "graph_source_timestamp",
            "coordinate_source_sha256",
            "bindings",
            "review",
            "manifest_sha256",
        },
    )
    bindings = tuple(
        _binding(item, field=f"manifest.bindings[{index}]")
        for index, item in enumerate(
            _list(raw["bindings"], field="manifest.bindings", nonempty=True)
        )
    )
    if len({item.location for item in bindings}) != len(bindings):
        _fail("manifest.bindings repeats a WTT location.")
    result = CoordinateBindingManifest(
        operator_code=_operator(raw["operator_code"], field="manifest.operator_code"),
        wtt_archive_sha256=_sha256(
            raw["wtt_archive_sha256"], field="manifest.wtt_archive_sha256"
        ),
        graph_source_sha256=_sha256(
            raw["graph_source_sha256"], field="manifest.graph_source_sha256"
        ),
        graph_sha256=_sha256(raw["graph_sha256"], field="manifest.graph_sha256"),
        graph_source_timestamp=_timestamp(
            raw["graph_source_timestamp"], field="manifest.graph_source_timestamp"
        ),
        coordinate_source_sha256=_sha256(
            raw["coordinate_source_sha256"],
            field="manifest.coordinate_source_sha256",
        ),
        bindings=bindings,
        review=_review(raw["review"], field="manifest.review"),
        manifest_sha256=_sha256(
            raw["manifest_sha256"], field="manifest.manifest_sha256"
        ),
    )
    for binding in result.bindings:
        if (
            binding.anchor_review is not None
            and binding.anchor_review.graph_sha256 != result.graph_sha256
        ):
            _fail(
                f"binding {binding.location!r} anchor review names a different graph."
            )
    return result


def _path_review(value: Any, *, field: str) -> PathSelectionReview:
    raw = _object(value, field=field)
    _exact_keys(
        raw,
        field=field,
        required={"review", "graph_sha256", "path_sha256", "edge_ids"},
    )
    return PathSelectionReview(
        review=_review(raw["review"], field=f"{field}.review"),
        graph_sha256=_sha256(raw["graph_sha256"], field=f"{field}.graph_sha256"),
        path_sha256=_sha256(raw["path_sha256"], field=f"{field}.path_sha256"),
        edge_ids=_edge_tuple(raw["edge_ids"], field=f"{field}.edge_ids"),
    )


def _transition(value: Any, *, field: str) -> TransitionSelection:
    raw = _object(value, field=field)
    _exact_keys(
        raw,
        field=field,
        required={
            "schedule_ref",
            "slice_index",
            "from_point_index",
            "to_point_index",
            "from_location",
            "to_location",
            "start_node_id",
            "end_node_id",
            "method",
            "edge_ids",
            "path_sha256",
        },
        optional={"path_review"},
    )
    from_index = _integer(raw["from_point_index"], field=f"{field}.from_point_index")
    to_index = _integer(raw["to_point_index"], field=f"{field}.to_point_index")
    if to_index != from_index + 1:
        _fail(f"{field} must cover consecutive timing-point indexes.")
    method = _text(raw["method"], field=f"{field}.method")
    if method not in {"explicit-edge-list", "reviewed-unique-shortest"}:
        _fail(f"{field}.method is unsupported.")
    edge_ids = _edge_tuple(raw["edge_ids"], field=f"{field}.edge_ids")
    path_review = (
        _path_review(raw["path_review"], field=f"{field}.path_review")
        if "path_review" in raw
        else None
    )
    if method == "reviewed-unique-shortest" and path_review is None:
        _fail(f"{field} unique shortest path needs path_review.")
    if method == "explicit-edge-list" and path_review is not None:
        _fail(f"{field} explicit edge list must not carry path_review.")
    path_sha = _sha256(raw["path_sha256"], field=f"{field}.path_sha256")
    if path_review is not None and (
        path_review.edge_ids != edge_ids or path_review.path_sha256 != path_sha
    ):
        _fail(f"{field}.path_review disagrees with the selected path.")
    start_node_id = _integer(
        raw["start_node_id"], field=f"{field}.start_node_id", minimum=1
    )
    end_node_id = _integer(raw["end_node_id"], field=f"{field}.end_node_id", minimum=1)
    if start_node_id == end_node_id:
        _fail(f"{field} collapses two timing points onto one graph node.")
    return TransitionSelection(
        schedule_ref=_text(raw["schedule_ref"], field=f"{field}.schedule_ref"),
        slice_index=_integer(raw["slice_index"], field=f"{field}.slice_index"),
        from_point_index=from_index,
        to_point_index=to_index,
        from_location=_text(raw["from_location"], field=f"{field}.from_location"),
        to_location=_text(raw["to_location"], field=f"{field}.to_location"),
        start_node_id=start_node_id,
        end_node_id=end_node_id,
        method=method,  # type: ignore[arg-type]
        edge_ids=edge_ids,
        path_sha256=path_sha,
        path_review=path_review,
    )


def _parse_route_manifest(
    value: Any, *, scoped: bool = False
) -> RouteSelectionManifest | ScopedRouteSelectionManifest:
    raw = _object(value, field="manifest")
    _exact_keys(
        raw,
        field="manifest",
        required={
            "operator_code",
            "service_date",
            "wtt_archive_sha256",
            "graph_source_sha256",
            "graph_sha256",
            "graph_source_timestamp",
            "bindings_manifest_sha256",
            "transitions",
            "review",
            "manifest_sha256",
        },
        optional=(
            {"service_identity_selection_manifest_sha256"} if scoped else None
        ),
    )
    transitions = tuple(
        _transition(item, field=f"manifest.transitions[{index}]")
        for index, item in enumerate(
            _list(raw["transitions"], field="manifest.transitions", nonempty=True)
        )
    )
    keys = [
        (item.schedule_ref, item.slice_index, item.from_point_index)
        for item in transitions
    ]
    if len(keys) != len(set(keys)):
        _fail("manifest.transitions repeats a timing-point transition.")
    operator_code = _operator(
        raw["operator_code"], field="manifest.operator_code"
    )
    identity_selection_pin = (
        _sha256(
            raw["service_identity_selection_manifest_sha256"],
            field="manifest.service_identity_selection_manifest_sha256",
        )
        if "service_identity_selection_manifest_sha256" in raw
        else None
    )
    if scoped:
        if operator_code != "HT" or identity_selection_pin is None:
            _fail("scoped route selections require HT and an identity pin.")
    elif operator_code == "HT":
        _fail("HT route selections must use the scope-pinned manifest kind.")
    common = {
        "operator_code": operator_code,
        "service_date": _date(raw["service_date"], field="manifest.service_date"),
        "wtt_archive_sha256": _sha256(
            raw["wtt_archive_sha256"], field="manifest.wtt_archive_sha256"
        ),
        "graph_source_sha256": _sha256(
            raw["graph_source_sha256"], field="manifest.graph_source_sha256"
        ),
        "graph_sha256": _sha256(
            raw["graph_sha256"], field="manifest.graph_sha256"
        ),
        "graph_source_timestamp": _timestamp(
            raw["graph_source_timestamp"], field="manifest.graph_source_timestamp"
        ),
        "bindings_manifest_sha256": _sha256(
            raw["bindings_manifest_sha256"],
            field="manifest.bindings_manifest_sha256",
        ),
        "transitions": transitions,
        "review": _review(raw["review"], field="manifest.review"),
        "manifest_sha256": _sha256(
            raw["manifest_sha256"], field="manifest.manifest_sha256"
        ),
    }
    result: RouteSelectionManifest | ScopedRouteSelectionManifest
    if scoped:
        if identity_selection_pin is None:  # pragma: no cover - gate above.
            raise AssertionError("Scoped route selection lost its identity pin.")
        result = ScopedRouteSelectionManifest(
            **common,  # type: ignore[arg-type]
            service_identity_selection_manifest_sha256=identity_selection_pin,
        )
    else:
        result = RouteSelectionManifest(**common)  # type: ignore[arg-type]
    for transition in result.transitions:
        if (
            transition.path_review is not None
            and transition.path_review.graph_sha256 != result.graph_sha256
        ):
            _fail(
                "transition path review names a different graph: "
                f"{transition.schedule_ref}/{transition.slice_index}/"
                f"{transition.from_point_index}."
            )
    return result


def _route_manifest(value: Any) -> RouteSelectionManifest:
    result = _parse_route_manifest(value)
    if not isinstance(result, RouteSelectionManifest):  # pragma: no cover
        raise AssertionError("Base route parser returned a scoped manifest.")
    return result


def _scoped_route_manifest(value: Any) -> ScopedRouteSelectionManifest:
    result = _parse_route_manifest(value, scoped=True)
    if not isinstance(result, ScopedRouteSelectionManifest):  # pragma: no cover
        raise AssertionError("Scoped route parser returned a base manifest.")
    return result


def _classification(value: Any, *, field: str) -> ScheduleClassification:
    raw = _object(value, field=field)
    _exact_keys(
        raw,
        field=field,
        required={"schedule_ref", "service_class", "rationale"},
        optional={"seasonal", "limited"},
    )
    service_class = _text(raw["service_class"], field=f"{field}.service_class")
    if service_class not in {"regular", "seasonal", "limited"}:
        _fail(f"{field}.service_class is unsupported.")
    seasonal = raw["seasonal"] if "seasonal" in raw else None
    limited = raw["limited"] if "limited" in raw else None
    if "seasonal" in raw and not isinstance(seasonal, bool):
        _fail(f"{field}.seasonal must be boolean when present.")
    if "limited" in raw and not isinstance(limited, bool):
        _fail(f"{field}.limited must be boolean when present.")
    return ScheduleClassification(
        schedule_ref=_text(raw["schedule_ref"], field=f"{field}.schedule_ref"),
        service_class=service_class,  # type: ignore[arg-type]
        rationale=_text(raw["rationale"], field=f"{field}.rationale"),
        seasonal=seasonal,
        limited=limited,
    )


def _classification_manifest(value: Any) -> ServiceClassificationManifest:
    raw = _object(value, field="manifest")
    _exact_keys(
        raw,
        field="manifest",
        required={
            "operator_code",
            "service_date",
            "wtt_archive_sha256",
            "entries",
            "review",
            "manifest_sha256",
        },
    )
    operator_code = _operator(raw["operator_code"], field="manifest.operator_code")
    if operator_code not in _CLASSIFICATION_OPERATORS:
        _fail("service classifications apply only to GR, GW, SN, or NT.")
    entries = tuple(
        _classification(item, field=f"manifest.entries[{index}]")
        for index, item in enumerate(
            _list(raw["entries"], field="manifest.entries", nonempty=True)
        )
    )
    if len({item.schedule_ref for item in entries}) != len(entries):
        _fail("manifest.entries repeats a schedule_ref.")
    for entry in entries:
        if operator_code == "GW":
            expected = "seasonal" if entry.seasonal else "regular"
            if (
                entry.seasonal is None
                or entry.limited is not None
                or entry.service_class != expected
            ):
                _fail("every GW entry must consistently decide seasonal.")
        elif operator_code == "SN":
            expected = "limited" if entry.limited else "regular"
            if (
                entry.limited is None
                or entry.seasonal is not None
                or entry.service_class != expected
            ):
                _fail("every SN entry must consistently decide limited.")
        elif (
            entry.service_class != "regular"
            or entry.seasonal is not None
            or entry.limited is not None
        ):
            _fail("GR/NT entries may only use regular without special flags.")
    return ServiceClassificationManifest(
        operator_code=operator_code,
        service_date=_date(raw["service_date"], field="manifest.service_date"),
        wtt_archive_sha256=_sha256(
            raw["wtt_archive_sha256"], field="manifest.wtt_archive_sha256"
        ),
        entries=entries,
        review=_review(raw["review"], field="manifest.review"),
        manifest_sha256=_sha256(
            raw["manifest_sha256"], field="manifest.manifest_sha256"
        ),
    )


def _service_identity_decision(
    value: Any, *, field: str
) -> ServiceIdentityDecision:
    raw = _object(value, field=field)
    _exact_keys(
        raw,
        field=field,
        required={
            "identity_ref",
            "uid",
            "tid",
            "component_schedule_refs",
            "decision",
            "evidence_locator",
            "rationale",
        },
    )
    component_refs = tuple(
        _text(item, field=f"{field}.component_schedule_refs[{index}]")
        for index, item in enumerate(
            _list(
                raw["component_schedule_refs"],
                field=f"{field}.component_schedule_refs",
                nonempty=True,
            )
        )
    )
    if len(component_refs) != len(set(component_refs)):
        _fail(f"{field}.component_schedule_refs repeats a schedule reference.")
    decision = _text(raw["decision"], field=f"{field}.decision")
    if decision not in {"included", "excluded"}:
        _fail(f"{field}.decision must be 'included' or 'excluded'.")
    return ServiceIdentityDecision(
        identity_ref=_text(raw["identity_ref"], field=f"{field}.identity_ref"),
        uid=_text(raw["uid"], field=f"{field}.uid"),
        tid=_text(raw["tid"], field=f"{field}.tid"),
        component_schedule_refs=component_refs,
        decision=decision,  # type: ignore[arg-type]
        evidence_locator=_text(
            raw["evidence_locator"], field=f"{field}.evidence_locator"
        ),
        rationale=_text(raw["rationale"], field=f"{field}.rationale"),
    )


def _service_identity_selection_manifest(
    value: Any,
) -> ServiceIdentitySelectionManifest:
    raw = _object(value, field="manifest")
    _exact_keys(
        raw,
        field="manifest",
        required={
            "operator_code",
            "service_date",
            "wtt_archive_sha256",
            "scope_source_sha256",
            "assembly_policy",
            "included_identity_count",
            "excluded_identity_count",
            "entries",
            "review",
            "manifest_sha256",
        },
    )
    operator_code = _operator(raw["operator_code"], field="manifest.operator_code")
    if operator_code != "HT":
        _fail("service identity selections currently apply only to HT.")
    entries = tuple(
        _service_identity_decision(
            item, field=f"manifest.entries[{index}]"
        )
        for index, item in enumerate(
            _list(raw["entries"], field="manifest.entries", nonempty=True)
        )
    )
    if len({entry.identity_ref for entry in entries}) != len(entries):
        _fail("manifest.entries repeats an identity_ref.")
    included_count = _integer(
        raw["included_identity_count"],
        field="manifest.included_identity_count",
    )
    excluded_count = _integer(
        raw["excluded_identity_count"],
        field="manifest.excluded_identity_count",
    )
    if included_count != sum(entry.decision == "included" for entry in entries):
        _fail("manifest.included_identity_count disagrees with entries.")
    if excluded_count != sum(entry.decision == "excluded" for entry in entries):
        _fail("manifest.excluded_identity_count disagrees with entries.")
    return ServiceIdentitySelectionManifest(
        operator_code=operator_code,
        service_date=_date(raw["service_date"], field="manifest.service_date"),
        wtt_archive_sha256=_sha256(
            raw["wtt_archive_sha256"], field="manifest.wtt_archive_sha256"
        ),
        scope_source_sha256=_sha256(
            raw["scope_source_sha256"], field="manifest.scope_source_sha256"
        ),
        assembly_policy=_text(
            raw["assembly_policy"], field="manifest.assembly_policy"
        ),
        included_identity_count=included_count,
        excluded_identity_count=excluded_count,
        entries=entries,
        review=_review(raw["review"], field="manifest.review"),
        manifest_sha256=_sha256(
            raw["manifest_sha256"], field="manifest.manifest_sha256"
        ),
    )


def _manifest_dict(value: _Manifest) -> dict[str, Any]:
    payload = _value_as_payload(value)
    payload["manifest_sha256"] = value.manifest_sha256
    return payload


def _value_as_payload(value: _Manifest) -> dict[str, Any]:
    """Return a load-equivalent, pin-free JSON payload.

    Optional ``None`` dataclass fields are omitted from the file. The loader
    reconstructs them before verifying the compiler's canonical payload hash.
    """

    # Keep one public representation rather than duplicating dataclass traversal.
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
            return {
                str(key): normalise(child)
                for key, child in item.items()
                if child is not None
            }
        return item

    result = normalise(document)
    if not isinstance(result, dict):  # pragma: no cover - dataclass invariant.
        raise AssertionError("Manifest did not normalize to an object.")
    return result


def _parse_envelope(
    payload: bytes,
    *,
    source: str,
    kind: _ManifestKind,
    parser: Callable[[Any], _Manifest],
) -> _Manifest:
    raw = _object(_decode(payload, source=source), field="root")
    if raw.get("template_only") is True or raw.get("release_state") != "reviewed":
        _fail(
            "template/non-reviewed JSON is not release evidence; complete the "
            "review, remove template markers, and use a fresh canonical hash."
        )
    _exact_keys(
        raw,
        field="root",
        required={"schema_version", "manifest_kind", "release_state", "manifest"},
    )
    if raw["schema_version"] != MANIFEST_JSON_SCHEMA_VERSION or isinstance(
        raw["schema_version"], bool
    ):
        _fail(f"root.schema_version must be {MANIFEST_JSON_SCHEMA_VERSION}.")
    if raw["manifest_kind"] != kind:
        _fail(f"root.manifest_kind must be {kind!r}.")
    result = parser(raw["manifest"])
    actual = manifest_payload_sha256(result)
    if result.manifest_sha256 != actual:
        _fail(
            "manifest payload changed after review: expected "
            f"{result.manifest_sha256}, got {actual}."
        )
    return result


def _read(path: Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise OperatorManifestJsonError(
            f"National operator manifest JSON: cannot read {path}: {exc}."
        ) from exc


def load_coordinate_binding_manifest(path: Path) -> CoordinateBindingManifest:
    return _parse_envelope(
        _read(path),
        source=str(path),
        kind="coordinate-bindings",
        parser=_coordinate_manifest,
    )


def load_route_selection_manifest(path: Path) -> RouteSelectionManifest:
    return _parse_envelope(
        _read(path),
        source=str(path),
        kind="route-selections",
        parser=_route_manifest,
    )


def load_scoped_route_selection_manifest(
    path: Path,
) -> ScopedRouteSelectionManifest:
    return _parse_envelope(
        _read(path),
        source=str(path),
        kind="scoped-route-selections",
        parser=_scoped_route_manifest,
    )


def load_service_classification_manifest(
    path: Path,
) -> ServiceClassificationManifest:
    return _parse_envelope(
        _read(path),
        source=str(path),
        kind="service-classifications",
        parser=_classification_manifest,
    )


def load_service_identity_selection_manifest(
    path: Path,
) -> ServiceIdentitySelectionManifest:
    return _parse_envelope(
        _read(path),
        source=str(path),
        kind="service-identity-selections",
        parser=_service_identity_selection_manifest,
    )


def _dump(
    value: _Manifest,
    path: Path,
    *,
    kind: _ManifestKind,
    parser: Callable[[Any], _Manifest],
    overwrite: bool,
) -> Path:
    envelope = {
        "schema_version": MANIFEST_JSON_SCHEMA_VERSION,
        "manifest_kind": kind,
        "release_state": "reviewed",
        "manifest": _manifest_dict(value),
    }
    payload = (
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    # Round-trip before touching disk. This validates review state, structure,
    # and canonical pin using exactly the public loader rules.
    _parse_envelope(payload, source="in-memory dump", kind=kind, parser=parser)
    destination = Path(path)
    if destination.exists() and not overwrite:
        _fail(f"refusing to overwrite existing manifest {destination}.")
    try:
        destination.write_bytes(payload)
    except OSError as exc:
        raise OperatorManifestJsonError(
            f"National operator manifest JSON: cannot write {destination}: {exc}."
        ) from exc
    return destination


def dump_coordinate_binding_manifest(
    value: CoordinateBindingManifest, path: Path, *, overwrite: bool = False
) -> Path:
    return _dump(
        value,
        path,
        kind="coordinate-bindings",
        parser=_coordinate_manifest,
        overwrite=overwrite,
    )


def dump_route_selection_manifest(
    value: RouteSelectionManifest, path: Path, *, overwrite: bool = False
) -> Path:
    return _dump(
        value,
        path,
        kind="route-selections",
        parser=_route_manifest,
        overwrite=overwrite,
    )


def dump_scoped_route_selection_manifest(
    value: ScopedRouteSelectionManifest,
    path: Path,
    *,
    overwrite: bool = False,
) -> Path:
    return _dump(
        value,
        path,
        kind="scoped-route-selections",
        parser=_scoped_route_manifest,
        overwrite=overwrite,
    )


def dump_service_classification_manifest(
    value: ServiceClassificationManifest, path: Path, *, overwrite: bool = False
) -> Path:
    return _dump(
        value,
        path,
        kind="service-classifications",
        parser=_classification_manifest,
        overwrite=overwrite,
    )


def dump_service_identity_selection_manifest(
    value: ServiceIdentitySelectionManifest,
    path: Path,
    *,
    overwrite: bool = False,
) -> Path:
    return _dump(
        value,
        path,
        kind="service-identity-selections",
        parser=_service_identity_selection_manifest,
        overwrite=overwrite,
    )
