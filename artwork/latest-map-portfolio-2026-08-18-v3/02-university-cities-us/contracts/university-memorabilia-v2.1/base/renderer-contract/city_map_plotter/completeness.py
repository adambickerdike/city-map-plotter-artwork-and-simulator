from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
from math import floor, hypot, isfinite
from pathlib import Path
import re
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from shapely.geometry import LineString, Polygon, box

from .features import (
    AREA_LAYERS,
    classify_supported_layer,
    is_identified_heritage_site,
)
from .geometry import Layout
from .models import MapFeature, PlotStroke


# This intentionally duplicates the extraction contract instead of importing
# its allow-lists.  It is a regression oracle: if acquisition/extraction changes
# without a deliberate update here, the audit should expose the disagreement.
# New non-empty highway values are conservatively required in ``roads_other``;
# that fallback prevents a newly introduced OSM road type from silently
# disappearing while still making the unfamiliar vocabulary visible for review.
AUDITED_HIGHWAY_LAYERS: dict[str, str] = {
    "motorway": "roads_major",
    "motorway_link": "roads_major",
    "trunk": "roads_major",
    "trunk_link": "roads_major",
    "primary": "roads_major",
    "primary_link": "roads_major",
    "secondary": "roads_secondary",
    "secondary_link": "roads_secondary",
    "tertiary": "roads_secondary",
    "tertiary_link": "roads_secondary",
    "residential": "roads_local",
    "living_street": "roads_local",
    "unclassified": "roads_local",
    "service": "roads_local",
    "road": "roads_local",
    "busway": "roads_other",
    "bus_guideway": "roads_other",
    "escape": "roads_other",
    "raceway": "roads_other",
    "path": "paths",
    "footway": "paths",
    "cycleway": "paths",
    "bridleway": "paths",
    "steps": "paths",
    "pedestrian": "paths",
    "track": "paths",
    "corridor": "paths",
    "elevator": "paths",
    "ladder": "paths",
    "via_ferrata": "paths",
}

ROAD_LAYERS = frozenset(AUDITED_HIGHWAY_LAYERS.values())
AUDITED_GEOMETRY_LAYERS = ROAD_LAYERS | {"road_areas"}
FUTURE_HIGHWAY_VALUES = frozenset({"planned", "proposed"})
INACTIVE_HIGHWAY_VALUES = frozenset(
    {
        "abandoned",
        "demolished",
        "destroyed",
        "disused",
        "dismantled",
        "razed",
        "removed",
    }
)
NON_ROUTE_HIGHWAY_VALUES = frozenset(
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
PLACEHOLDER_HIGHWAY_VALUES = frozenset({"", "no", "yes"})
LIFECYCLE_KEYS = frozenset(
    {"construction"} | FUTURE_HIGHWAY_VALUES | INACTIVE_HIGHWAY_VALUES
)
FALSE_VALUES = frozenset({"", "0", "false", "no", "none"})
SOURCE_GEOMETRY_FAILURES = frozenset(
    {"missing_or_invalid_geometry", "zero_length_geometry"}
)
_SVG_NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_SVG_NUMBER_RE = re.compile(_SVG_NUMBER_PATTERN)
_SVG_POINT_PATTERN = rf"{_SVG_NUMBER_PATTERN}(?:\s*,\s*|\s+){_SVG_NUMBER_PATTERN}"
_SVG_RENDER_PATH_RE = re.compile(
    rf"\A\s*M\s*{_SVG_POINT_PATTERN}"
    rf"(?:\s+(?:L\s*{_SVG_POINT_PATTERN}|"
    rf"C\s*{_SVG_POINT_PATTERN}\s+{_SVG_POINT_PATTERN}\s+{_SVG_POINT_PATTERN}))+"
    rf"\s*(?:Z\s*)?\Z"
)
_OVERPASS_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_NON_RENDERING_SVG_CONTAINERS = frozenset(
    {
        "clipPath",
        "defs",
        "desc",
        "marker",
        "mask",
        "metadata",
        "pattern",
        "symbol",
        "title",
    }
)


@dataclass(frozen=True)
class HighwayCompletenessRecord:
    osm_type: str
    osm_id: str
    source_ref: str
    semantic_kind: str
    has_highway_tag: bool
    highway: str
    area_highway: str | None
    name: str | None
    raw_occurrences: int
    in_frame: bool
    geometry_status: str
    expected_layer: str | None
    expected: bool
    extracted: bool
    cartographic: bool
    physical: bool
    svg: bool | None
    status: str
    reason: str | None
    notes: tuple[str, ...] = ()
    physical_minimum_evidence_ids: tuple[str, ...] = ()
    ink_budget_evidence_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["notes"] = list(self.notes)
        value["physical_minimum_evidence_ids"] = list(
            self.physical_minimum_evidence_ids
        )
        value["ink_budget_evidence_ids"] = list(self.ink_budget_evidence_ids)
        return value


@dataclass(frozen=True)
class HighwayCompletenessAudit:
    records: tuple[HighwayCompletenessRecord, ...]
    source_available: bool
    final_stage: str
    source_scope_warning: str
    acquisition_scope_complete: bool | None = None
    highway_way_scope_complete: bool | None = None
    road_area_scope_complete: bool | None = None
    physical_minimum_evidence_supplied_count: int = 0
    physical_minimum_evidence_valid_count: int = 0
    physical_minimum_evidenced_source_refs: tuple[str, ...] = ()
    invalid_physical_minimum_evidence: tuple[tuple[str, str], ...] = ()
    detail_profile: str = "faithful"
    ink_budget_evidence_supplied: bool = False
    ink_budget_evidence_ledger_valid: bool = False
    ink_budget_evidence_supplied_count: int = 0
    ink_budget_evidence_valid_count: int = 0
    ink_budget_evidenced_source_refs: tuple[str, ...] = ()
    invalid_ink_budget_evidence: tuple[tuple[str, str], ...] = ()
    ink_budget_input_count: int | None = None
    ink_budget_retained_input_count: int = 0
    ink_budget_omitted_input_count: int = 0
    ink_budget_omitted_group_count: int = 0
    ink_budget_omitted_layer_counts: tuple[tuple[str, int], ...] = ()
    ink_budget_uncullable_source_group_count: int | None = None
    ink_budget_diagnostics_supplied: bool = False
    ink_budget_diagnostics_valid: bool = False
    invalid_ink_budget_diagnostics: tuple[tuple[str, str], ...] = ()

    @property
    def expected_records(self) -> tuple[HighwayCompletenessRecord, ...]:
        return tuple(record for record in self.records if record.expected)

    @property
    def missing_expected(self) -> tuple[HighwayCompletenessRecord, ...]:
        return tuple(
            record for record in self.expected_records if record.status != "emitted"
        )

    def as_dict(self, *, include_records: bool = True) -> dict[str, Any]:
        expected = self.expected_records
        missing = self.missing_expected
        in_frame = tuple(record for record in self.records if record.in_frame)
        unresolved = tuple(record for record in in_frame if not record.expected)
        retained_unknown = tuple(
            record
            for record in expected
            if record.semantic_kind == "centreline"
            and _is_retained_unknown_highway(record.highway)
        )
        highway_records = tuple(
            record for record in self.records if record.has_highway_tag
        )
        in_frame_highways = tuple(
            record for record in highway_records if record.in_frame
        )
        expected_highways = tuple(
            record for record in expected if record.has_highway_tag
        )
        road_area_records = tuple(
            record for record in self.records if record.semantic_kind == "road_area"
        )
        source_geometry_failures = tuple(
            record for record in missing if record.reason in SOURCE_GEOMETRY_FAILURES
        )
        excluded = tuple(
            record for record in self.records if record.status == "excluded"
        )
        stage_counts = {
            "expected": len(expected),
            "extracted": sum(record.extracted for record in expected),
            "cartographic": sum(record.cartographic for record in expected),
            "physical": sum(record.physical for record in expected),
        }
        if self.final_stage == "svg":
            stage_counts["svg"] = sum(bool(record.svg) for record in expected)
        literal_budget_omissions = self.ink_budget_omitted_input_count > 0
        pipeline_complete = (
            self.source_available and not missing and not literal_budget_omissions
        )
        end_to_end_complete = (
            pipeline_complete and self.acquisition_scope_complete is True
        )
        ink_budget_omissions = tuple(
            record for record in missing if record.reason == "ink_budget_gate"
        )
        unexpected_cartographic_drops = tuple(
            record
            for record in missing
            if record.reason == "unexpected_cartographic_drop"
        )
        # The budget ledger authenticates the canonical pre/post-selection
        # stroke partition and diagnostics directly.  A PBF deliberately has
        # no Overpass-shaped raw element list, so raw acquisition availability
        # must not invalidate an otherwise exact ledger.  Acquisition scope
        # remains independently unknown through ``source_available``,
        # ``acquisition_scope_complete``, and ``complete`` below.
        ink_budget_evidence_complete = bool(
            self.ink_budget_evidence_supplied
            and self.ink_budget_evidence_ledger_valid
            and self.ink_budget_diagnostics_supplied
            and self.ink_budget_diagnostics_valid
            and self.ink_budget_uncullable_source_group_count == 0
            and not unexpected_cartographic_drops
            and all(record.ink_budget_evidence_ids for record in ink_budget_omissions)
        )
        ink_budget_policy_conformant = bool(
            self.detail_profile == "ink-balanced" and ink_budget_evidence_complete
        )
        verified_budget_group_count = (
            self.ink_budget_omitted_group_count if ink_budget_policy_conformant else 0
        )
        verified_budget_input_count = (
            self.ink_budget_omitted_input_count if ink_budget_policy_conformant else 0
        )
        unverified_budget_omission_count = (
            0
            if ink_budget_policy_conformant
            else self.ink_budget_evidence_supplied_count
        )
        result: dict[str, Any] = {
            "schema_version": 2,
            "source_available": self.source_available,
            "source_scope_warning": self.source_scope_warning,
            "acquisition_scope_complete": self.acquisition_scope_complete,
            "highway_way_scope_complete": self.highway_way_scope_complete,
            "road_area_scope_complete": self.road_area_scope_complete,
            "final_stage": self.final_stage,
            "raw_highway_element_count": sum(
                record.raw_occurrences for record in highway_records
            ),
            "raw_unique_highway_count": len(highway_records),
            "raw_duplicate_occurrence_count": sum(
                max(0, record.raw_occurrences - 1) for record in highway_records
            ),
            "raw_audited_road_object_count": sum(
                record.raw_occurrences for record in self.records
            ),
            "raw_unique_audited_road_object_count": len(self.records),
            "raw_unique_road_area_count": len(road_area_records),
            "in_frame_unique_highway_count": len(in_frame_highways),
            "in_frame_unique_audited_road_object_count": len(in_frame),
            "expected_in_frame_highway_count": len(expected_highways),
            "expected_in_frame_geometry_count": len(expected),
            "expected_in_frame_road_area_count": sum(
                record.expected for record in road_area_records
            ),
            "stage_counts": stage_counts,
            "missing_expected_count": len(missing),
            "missing_expected_source_refs": [record.source_ref for record in missing],
            "missing_by_reason": dict(
                sorted(
                    Counter(record.reason or "unknown" for record in missing).items()
                )
            ),
            "source_geometry_failure_count": len(source_geometry_failures),
            "source_geometry_failure_source_refs": [
                record.source_ref for record in source_geometry_failures
            ],
            "source_geometry_failures_by_reason": dict(
                sorted(
                    Counter(
                        record.reason or "unknown"
                        for record in source_geometry_failures
                    ).items()
                )
            ),
            "excluded_by_reason": dict(
                sorted(
                    Counter(record.reason or "unknown" for record in excluded).items()
                )
            ),
            "unresolved_in_frame_count": len(unresolved),
            "unresolved_in_frame_by_reason": dict(
                sorted(
                    Counter(record.reason or "unknown" for record in unresolved).items()
                )
            ),
            "retained_unknown_in_frame_count": len(retained_unknown),
            "retained_unknown_by_value": dict(
                sorted(Counter(record.highway for record in retained_unknown).items())
            ),
            "pipeline_complete_for_supplied_source": pipeline_complete,
            "complete_scope": "acquisition_query_and_pipeline",
            "complete": end_to_end_complete,
            "physical_minimum_omission_evidence": {
                "schema_version": 1,
                "supplied_entry_count": (self.physical_minimum_evidence_supplied_count),
                "valid_entry_count": self.physical_minimum_evidence_valid_count,
                "invalid_entry_count": len(self.invalid_physical_minimum_evidence),
                "evidenced_source_ref_count": len(
                    self.physical_minimum_evidenced_source_refs
                ),
                "evidenced_source_refs": list(
                    self.physical_minimum_evidenced_source_refs
                ),
                "invalid_entries": [
                    {"omission_id": omission_id, "reason": reason}
                    for omission_id, reason in self.invalid_physical_minimum_evidence
                ],
            },
            "ink_budget_omission_evidence": {
                "schema_version": 2,
                "policy": "ink-balanced-v2",
                "ledger_supplied": self.ink_budget_evidence_supplied,
                "ledger_valid": self.ink_budget_evidence_ledger_valid,
                "diagnostics_supplied": self.ink_budget_diagnostics_supplied,
                "diagnostics_valid": self.ink_budget_diagnostics_valid,
                "supplied_entry_count": self.ink_budget_evidence_supplied_count,
                "valid_entry_count": self.ink_budget_evidence_valid_count,
                "invalid_entry_count": len(self.invalid_ink_budget_evidence),
                "evidenced_source_ref_count": len(
                    self.ink_budget_evidenced_source_refs
                ),
                "evidenced_source_refs": list(self.ink_budget_evidenced_source_refs),
                "pre_budget_input_count": self.ink_budget_input_count,
                "retained_input_count": self.ink_budget_retained_input_count,
                "omitted_input_count": self.ink_budget_omitted_input_count,
                "omitted_group_count": self.ink_budget_omitted_group_count,
                "omitted_layer_counts": dict(self.ink_budget_omitted_layer_counts),
                "uncullable_source_group_count": (
                    self.ink_budget_uncullable_source_group_count
                ),
                "invalid_entries": [
                    {"omission_id": omission_id, "reason": reason}
                    for omission_id, reason in self.invalid_ink_budget_evidence
                ],
                "invalid_diagnostics": [
                    {"component": component, "reason": reason}
                    for component, reason in self.invalid_ink_budget_diagnostics
                ],
            },
            "ink_budget_policy": {
                "schema_version": 2,
                "profile": self.detail_profile,
                "policy": "ink-balanced-v2",
                "scope": ("cartographic_ink_budget_omissions_for_supplied_source"),
                "verified_omission_count": verified_budget_group_count,
                "verified_omitted_input_count": (verified_budget_input_count),
                "verified_omitted_layer_counts": (
                    dict(self.ink_budget_omitted_layer_counts)
                    if ink_budget_policy_conformant
                    else {}
                ),
                "verified_omission_source_refs": [
                    *(
                        self.ink_budget_evidenced_source_refs
                        if ink_budget_policy_conformant
                        else ()
                    )
                ],
                "unexpected_cartographic_drop_count": len(
                    unexpected_cartographic_drops
                ),
                "unexpected_cartographic_drop_source_refs": [
                    record.source_ref for record in unexpected_cartographic_drops
                ],
                "evidence_complete": ink_budget_evidence_complete,
                "policy_conformant": ink_budget_policy_conformant,
                "semantic_policy_conformant": ink_budget_policy_conformant,
                "literal_geometry_complete": pipeline_complete,
                "uncullable_source_group_count": (
                    self.ink_budget_uncullable_source_group_count
                ),
                "unverified_budget_omission_count": (unverified_budget_omission_count),
                "unverified_omission_count": (
                    unverified_budget_omission_count
                    + len(unexpected_cartographic_drops)
                ),
                "pipeline_complete_for_supplied_source": pipeline_complete,
                "end_to_end_complete": end_to_end_complete,
            },
        }
        if include_records:
            result["records"] = [record.as_dict() for record in self.records]
        return result


@dataclass(frozen=True)
class RawGeometryIntegrityFinding:
    """One machine-readable failure in selected raw source geometry."""

    reason: str
    layer: str
    source_ref: str
    element_index: int
    osm_type: str
    osm_id: str
    component: str
    member_index: int | None = None
    member_type: str | None = None
    member_ref: str | None = None
    member_role: str | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawGeometryIntegrityAudit:
    """Integrity evidence for drawable objects in a supplied raw JSON source.

    This result intentionally does not claim acquisition completeness.  Even a
    clean response proves only that selected objects *present in that response*
    had intact raw geometry and that visible geometry reached canonical
    features.  PBF ingestion has canonical-feature evidence instead and is
    represented as not audited here, never as a vacuous success.
    """

    findings: tuple[RawGeometryIntegrityFinding, ...]
    source_available: bool
    evidence_kind: str
    evidence_scope: str
    source_query_available: bool
    checked_object_count: int = 0
    checked_geometry_part_count: int = 0
    raw_to_canonical_checked_part_count: int = 0
    ignored_non_geometry_member_count: int = 0
    selected_by_layer: tuple[tuple[str, int], ...] = ()

    @property
    def failures(self) -> tuple[RawGeometryIntegrityFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "error")

    @property
    def supplied_source_geometry_complete(self) -> bool | None:
        if not self.source_available:
            return None
        return not self.failures

    def as_dict(self, *, include_findings: bool = True) -> dict[str, Any]:
        failures = self.failures
        warnings = tuple(item for item in self.findings if item.severity == "warning")
        status = (
            "not_audited"
            if not self.source_available and self.evidence_kind.startswith("pbf")
            else "unavailable"
            if not self.source_available
            else "incomplete"
            if failures
            else "verified"
        )
        result: dict[str, Any] = {
            "schema_version": 1,
            "status": status,
            "source_available": self.source_available,
            "evidence_kind": self.evidence_kind,
            "evidence_scope": self.evidence_scope,
            "source_query_available": self.source_query_available,
            "acquisition_scope_complete": None,
            "complete_scope": "selected_geometry_present_in_supplied_source_only",
            "supplied_source_geometry_complete": (
                self.supplied_source_geometry_complete
            ),
            "end_to_end_complete": None,
            "checked_object_count": self.checked_object_count,
            "checked_geometry_part_count": self.checked_geometry_part_count,
            "raw_to_canonical_checked_part_count": (
                self.raw_to_canonical_checked_part_count
            ),
            "ignored_non_geometry_member_count": (
                self.ignored_non_geometry_member_count
            ),
            "selected_by_layer": dict(self.selected_by_layer),
            "failure_count": len(failures),
            "warning_count": len(warnings),
            "failures_by_reason": dict(
                sorted(Counter(item.reason for item in failures).items())
            ),
            "failures_by_layer": dict(
                sorted(Counter(item.layer for item in failures).items())
            ),
        }
        if include_findings:
            result["findings"] = [item.as_dict() for item in self.findings]
        return result


def _tags(element: dict[str, Any]) -> dict[str, str]:
    value = element.get("tags")
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _geometry_parts(element: dict[str, Any]) -> Iterable[LineString]:
    geometry = element.get("geometry")
    if not isinstance(geometry, list):
        return
    current: list[tuple[float, float]] = []
    for item in geometry:
        if isinstance(item, dict) and "lat" in item and "lon" in item:
            try:
                point = (float(item["lon"]), float(item["lat"]))
            except (TypeError, ValueError):
                point = None
            if point is not None and all(isfinite(value) for value in point):
                current.append(point)
                continue
        if len(current) >= 2:
            yield LineString(current)
        current = []
    if len(current) >= 2:
        yield LineString(current)


def _geometry_value_failure(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return "missing_or_invalid_geometry"
    part_lengths: list[float] = []
    current: list[tuple[float, float]] = []
    invalid_fragment = False
    for item in value:
        coordinate: tuple[float, float] | None = None
        if isinstance(item, dict):
            if "lat" not in item or "lon" not in item:
                invalid_fragment = True
            else:
                try:
                    candidate = (float(item["lon"]), float(item["lat"]))
                except (TypeError, ValueError):
                    candidate = None
                if (
                    candidate is None
                    or not all(isfinite(number) for number in candidate)
                    or not (-180.0 <= candidate[0] <= 180.0)
                    or not (-90.0 <= candidate[1] <= 90.0)
                ):
                    invalid_fragment = True
                else:
                    coordinate = candidate
        else:
            # Overpass may insert bare nulls when inline geometry is cropped.
            # A null is corruption *within this one way coordinate list*, not a
            # legitimate separator between relation members.  Preserving the
            # visible runs cannot make the source geometry complete.
            invalid_fragment = True
        if coordinate is not None:
            current.append(coordinate)
            continue
        if len(current) == 1:
            invalid_fragment = True
        elif len(current) >= 2:
            part_lengths.append(LineString(current).length)
        current = []
    if len(current) == 1:
        invalid_fragment = True
    elif len(current) >= 2:
        part_lengths.append(LineString(current).length)
    if invalid_fragment or not part_lengths:
        return "missing_or_invalid_geometry"
    if any(length <= 1e-12 for length in part_lengths):
        return "zero_length_geometry"
    return None


def _inline_geometry_failures(element: dict[str, Any]) -> list[str]:
    osm_type = str(element.get("type", "unknown"))
    if osm_type == "way":
        failure = _geometry_value_failure(element.get("geometry"))
        return [failure] if failure is not None else []
    if osm_type != "relation":
        return []
    members = element.get("members")
    if not isinstance(members, list):
        return ["missing_or_invalid_geometry"]
    way_members = [
        member
        for member in members
        if isinstance(member, dict) and member.get("type") == "way"
    ]
    if not way_members:
        return ["missing_or_invalid_geometry"]
    return [
        failure
        for member in way_members
        if (failure := _geometry_value_failure(member.get("geometry"))) is not None
    ]


def _element_geometry_parts(element: dict[str, Any]) -> Iterable[LineString]:
    """Yield way geometry and inline relation-member geometry."""

    yield from _geometry_parts(element)
    members = element.get("members")
    if not isinstance(members, list):
        return
    for member in members:
        if isinstance(member, dict) and member.get("type") == "way":
            yield from _geometry_parts(member)


def _raw_geometry_status(
    elements: list[dict[str, Any]], layout: Layout
) -> tuple[str, bool]:
    geometry_failures = [
        failure
        for element in elements
        for failure in _inline_geometry_failures(element)
    ]
    parts = [part for element in elements for part in _element_geometry_parts(element)]
    if not parts:
        return "missing_or_invalid_geometry", False
    nonzero = [part for part in parts if part.length > 1e-12]
    if not nonzero:
        return "zero_length_geometry", False
    frame = box(
        layout.bbox.west,
        layout.bbox.south,
        layout.bbox.east,
        layout.bbox.north,
    )
    visible_length = sum(part.intersection(frame).length for part in nonzero)
    in_frame = visible_length > 1e-12
    if "missing_or_invalid_geometry" in geometry_failures:
        return "missing_or_invalid_geometry", in_frame
    if "zero_length_geometry" in geometry_failures:
        return "zero_length_geometry", in_frame
    if visible_length <= 1e-12:
        return "outside_frame", False
    return "visible", True


SourcePartIndex = dict[tuple[str, str, str], set[str]]
GeometrySignature = tuple[tuple[float, float], ...]
FeatureGeometryIndex = dict[tuple[str, str, str], dict[str, set[GeometrySignature]]]


@dataclass(frozen=True)
class _PhysicalMinimumEvidenceAudit:
    by_source_ref: dict[tuple[str, str], tuple[str, ...]]
    by_source_input: dict[tuple[str, str], tuple[tuple[int, str, str], ...]]
    supplied_count: int
    valid_count: int
    evidenced_source_refs: tuple[str, ...]
    invalid_entries: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _InkBudgetEvidenceAudit:
    by_source_input: dict[tuple[str, str], tuple[tuple[int, str], ...]]
    supplied: bool
    ledger_valid: bool
    supplied_count: int
    valid_count: int
    evidenced_source_refs: tuple[str, ...]
    invalid_entries: tuple[tuple[str, str], ...]
    input_count: int | None
    retained_input_count: int
    omitted_input_count: int
    omitted_group_count: int
    omitted_layer_counts: tuple[tuple[str, int], ...]
    uncullable_source_group_count: int | None
    diagnostics_supplied: bool
    diagnostics_valid: bool
    invalid_diagnostics: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _InkBudgetDiagnosticsAudit:
    supplied: bool
    valid: bool
    uncullable_source_group_count: int | None
    invalid: tuple[tuple[str, str], ...]


def _serialized_stroke_points(
    stroke: PlotStroke,
) -> tuple[tuple[float, float], ...]:
    return tuple((float(f"{x:.3f}"), float(f"{y:.3f}")) for x, y in stroke.points)


def _serialized_stroke_length(stroke: PlotStroke) -> float:
    points = _serialized_stroke_points(stroke)
    return sum(
        ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
        for start, end in zip(points, points[1:])
    )


def _serialized_stroke_sha256(stroke: PlotStroke) -> str:
    payload = ";".join(f"{x:.3f},{y:.3f}" for x, y in stroke.points).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    resolved = float(value)
    return resolved if isfinite(resolved) else None


def _physical_minimum_entry_error(
    entry: dict[str, Any], cartographic_strokes: tuple[PlotStroke, ...]
) -> str | None:
    layer = entry.get("layer")
    if not isinstance(layer, str) or not layer:
        return "missing_layer"
    source_refs_value = entry.get("source_refs")
    if not isinstance(source_refs_value, list) or any(
        not isinstance(item, str) or not item for item in source_refs_value
    ):
        return "invalid_source_refs"
    if source_refs_value != sorted(set(source_refs_value)):
        return "noncanonical_source_refs"

    branch = entry.get("branch")
    reason = entry.get("reason")
    allowed_branches = {
        "initial_source_minimum_gate",
        "weighted_part_minimum_gate",
        "residual_network_trail_minimum_gate",
    }
    if branch not in allowed_branches:
        return "unknown_omission_branch"
    if reason not in {
        "below_minimum_serialized_length",
        "below_minimum_area",
    }:
        return "unknown_minimum_gate_reason"

    nib_mm = _finite_number(entry.get("effective_nib_mm"))
    three_nib_floor_mm = _finite_number(entry.get("required_three_nib_floor_mm"))
    effective_floor_mm = _finite_number(entry.get("required_effective_length_floor_mm"))
    measured_length_mm = _finite_number(entry.get("measured_serialized_length_mm"))
    if (
        nib_mm is None
        or nib_mm <= 0
        or three_nib_floor_mm is None
        or effective_floor_mm is None
        or measured_length_mm is None
        or measured_length_mm < 0
    ):
        return "invalid_length_or_nib_measurement"
    if abs(three_nib_floor_mm - 3 * nib_mm) > 1e-8:
        return "three_nib_floor_mismatch"
    if abs(effective_floor_mm - max(0.5, three_nib_floor_mm)) > 1e-8:
        return "effective_length_floor_mismatch"
    if entry.get("serialized_coordinate_precision_mm") != 0.001:
        return "coordinate_precision_mismatch"
    omitted_hash = entry.get("serialized_geometry_sha256")
    if (
        not isinstance(omitted_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", omitted_hash) is None
    ):
        return "invalid_omitted_geometry_fingerprint"

    inputs_value = entry.get("input_strokes")
    if not isinstance(inputs_value, list) or not inputs_value:
        return "missing_input_stroke_evidence"
    indexes: set[int] = set()
    actual_inputs: list[PlotStroke] = []
    actual_source_refs: set[str] = set()
    input_lengths: list[float] = []
    for value in inputs_value:
        if not isinstance(value, dict):
            return "invalid_input_stroke_evidence"
        index = value.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(cartographic_strokes)
            or index in indexes
        ):
            return "invalid_input_stroke_index"
        indexes.add(index)
        actual = cartographic_strokes[index]
        actual_inputs.append(actual)
        if value.get("layer") != actual.layer or actual.layer != layer:
            return "input_stroke_layer_mismatch"
        if value.get("part") != str(actual.part):
            return "input_stroke_part_mismatch"
        expected_refs = sorted(
            {item for item in actual.tags.get("source-refs", "").split(";") if item}
        )
        if value.get("source_refs") != expected_refs:
            return "input_stroke_source_ref_mismatch"
        actual_source_refs.update(expected_refs)
        actual_length = round(_serialized_stroke_length(actual), 9)
        recorded_length = _finite_number(value.get("serialized_length_mm"))
        if recorded_length is None or abs(recorded_length - actual_length) > 1e-9:
            return "input_stroke_length_mismatch"
        input_lengths.append(actual_length)
        if value.get("serialized_geometry_sha256") != _serialized_stroke_sha256(actual):
            return "input_stroke_geometry_fingerprint_mismatch"
    if source_refs_value != sorted(actual_source_refs):
        return "omission_source_ref_union_mismatch"
    if (
        branch
        in {
            "initial_source_minimum_gate",
            "weighted_part_minimum_gate",
        }
        and len(actual_inputs) != 1
    ):
        return "single_input_branch_has_multiple_inputs"

    if reason == "below_minimum_serialized_length":
        if entry.get("measurement") != "serialized_polyline_length_mm":
            return "length_measurement_kind_mismatch"
        if measured_length_mm + 1e-9 >= effective_floor_mm:
            return "length_does_not_fail_effective_floor"
        if (
            entry.get("measured_area_mm2") is not None
            or entry.get("required_minimum_area_mm2") is not None
        ):
            return "length_evidence_contains_area_measurement"
        if branch == "initial_source_minimum_gate":
            if abs(measured_length_mm - input_lengths[0]) > 1e-9:
                return "initial_gate_length_mismatch"
            if omitted_hash != _serialized_stroke_sha256(actual_inputs[0]):
                return "initial_gate_geometry_fingerprint_mismatch"
        elif branch == "residual_network_trail_minimum_gate":
            tolerance = max(1e-8, len(input_lengths) * 1e-8)
            if abs(measured_length_mm - sum(input_lengths)) > tolerance:
                return "network_trail_length_conservation_mismatch"
    else:
        if branch != "initial_source_minimum_gate":
            return "area_gate_used_outside_initial_branch"
        if entry.get("measurement") != "serialized_polygon_area_mm2":
            return "area_measurement_kind_mismatch"
        measured_area_mm2 = _finite_number(entry.get("measured_area_mm2"))
        minimum_area_mm2 = _finite_number(entry.get("required_minimum_area_mm2"))
        if measured_area_mm2 is None or minimum_area_mm2 is None:
            return "invalid_area_measurement"
        if abs(minimum_area_mm2 - (2 * nib_mm) ** 2) > 1e-8:
            return "minimum_area_mismatch"
        if measured_area_mm2 + 1e-12 >= minimum_area_mm2:
            return "area_does_not_fail_minimum"
        points = _serialized_stroke_points(actual_inputs[0])
        polygon = LineString(points)
        if not polygon.is_ring:
            return "area_evidence_input_is_not_closed"
        actual_area_mm2 = Polygon(points).area
        if abs(measured_area_mm2 - actual_area_mm2) > 1e-9:
            return "initial_gate_area_mismatch"
        if omitted_hash != _serialized_stroke_sha256(actual_inputs[0]):
            return "initial_gate_geometry_fingerprint_mismatch"
    return None


def _audit_physical_minimum_evidence(
    evidence: Iterable[Any],
    cartographic_strokes: tuple[PlotStroke, ...],
) -> _PhysicalMinimumEvidenceAudit:
    resolved = tuple(evidence)
    indexed: dict[tuple[str, str], list[str]] = defaultdict(list)
    indexed_inputs: dict[tuple[str, str], list[tuple[int, str, str]]] = defaultdict(
        list
    )
    invalid: list[tuple[str, str]] = []
    valid_count = 0
    seen_ids: set[str] = set()
    for position, value in enumerate(resolved, start=1):
        entry = value.as_dict() if hasattr(value, "as_dict") else value
        fallback_id = f"supplied-entry-{position}"
        if not isinstance(entry, dict):
            invalid.append((fallback_id, "entry_is_not_an_object"))
            continue
        omission_id = entry.get("omission_id")
        if not isinstance(omission_id, str) or not omission_id:
            invalid.append((fallback_id, "missing_omission_id"))
            continue
        if omission_id in seen_ids:
            invalid.append((omission_id, "duplicate_omission_id"))
            continue
        seen_ids.add(omission_id)
        if entry.get("schema_version") != 1:
            invalid.append((omission_id, "unsupported_schema_version"))
            continue
        error = _physical_minimum_entry_error(entry, cartographic_strokes)
        if error is not None:
            invalid.append((omission_id, error))
            continue
        valid_count += 1
        layer = str(entry["layer"])
        for source_ref in entry["source_refs"]:
            indexed[(layer, source_ref)].append(omission_id)
            for input_stroke in entry["input_strokes"]:
                if source_ref in input_stroke["source_refs"]:
                    indexed_inputs[(layer, source_ref)].append(
                        (
                            int(input_stroke["index"]),
                            omission_id,
                            str(entry["branch"]),
                        )
                    )
    evidenced_refs = tuple(sorted({source_ref for _layer, source_ref in indexed}))
    return _PhysicalMinimumEvidenceAudit(
        by_source_ref={key: tuple(value) for key, value in sorted(indexed.items())},
        by_source_input={
            key: tuple(value) for key, value in sorted(indexed_inputs.items())
        },
        supplied_count=len(resolved),
        valid_count=valid_count,
        evidenced_source_refs=evidenced_refs,
        invalid_entries=tuple(invalid),
    )


_INK_BUDGET_SCHEMA_VERSION = 2
_INK_BALANCED_POLICY = "ink-balanced-v2"
_INK_BUDGET_TIERS = {
    "water_areas": "water_areas",
    "rivers": "rivers",
    "waterways": "waterways",
    "roads_major": "major_roads",
    "roads_secondary": "secondary_roads",
    "roads_local": "local_roads",
    "roads_other": "other_roads",
    "road_areas": "road_areas",
    "railways": "railways",
    "paths": "path_context",
    "green_space": "park_context",
}
_INK_SEMANTIC_ROLES = (
    "arterial_through",
    "arterial_link",
    "secondary_through",
    "secondary_link",
    "local_street",
    "other_road",
    "principal_water_area",
    "principal_river",
    "principal_waterway",
    "minor_water_area",
    "river",
    "minor_waterway",
    "active_principal_rail",
    "subway_rail",
    "disused_rail",
    "service_rail",
    "path_context",
    "park_context",
    "road_area",
    "supplemental",
)
_INK_SEMANTIC_RANK = {role: rank for rank, role in enumerate(_INK_SEMANTIC_ROLES)}
_INK_ROAD_LAYERS = frozenset(
    {"roads_major", "roads_secondary", "roads_local", "roads_other"}
)
_INK_FAIR_CONTEXT_LAYERS = frozenset(
    {
        "water_areas",
        "rivers",
        "waterways",
        "railways",
        "paths",
        "green_space",
    }
)
_INK_RESERVE_LAYERS = (
    "roads_major",
    "roads_secondary",
    "roads_local",
    "water_areas",
    "rivers",
    "waterways",
    "railways",
    "paths",
    "green_space",
)
_INK_SELECTION_STAGES = (
    "reserve_prefill",
    "road_fill",
    "fair_context_fill",
    "late_fill",
)
_INK_PRIORITY_INTEGER_FIELDS = (
    "tile_x",
    "tile_y",
    "tile_order",
    "tile_round",
    "max_road_rank",
    "semantic_rank",
    "shared_topology_node_count",
    "consideration_order",
)
_INK_PRIORITY_BOOLEAN_FIELDS = (
    "named",
    "bridge_or_tunnel",
    "connected_to_selected_topology",
    "link",
    "active_rail",
    "rail_service",
    "principal_water",
)
_INK_CUTOFF_NUMBER_FIELDS = (
    "budget_ink_mm2",
    "fixed_ink_mm2",
    "selected_map_ink_before_mm2",
    "remaining_ink_before_mm2",
    "group_planned_effective_ink_mm2",
    "would_exceed_by_mm2",
)
_INK_LEDGER_FIELDS = frozenset(
    {
        "schema_version",
        "policy",
        "reason",
        "entries",
        "entry_count",
        "omitted_group_count",
        "input_count",
        "retained_input_indexes",
        "omitted_input_indexes",
        "omitted_input_stroke_count",
        "omitted_source_refs",
        "omitted_canonical_source_refs",
        "uncullable_source_group_count",
        "selection_partition_sha256",
    }
)
_INK_ENTRY_FIELDS = frozenset(
    {
        "schema_version",
        "policy",
        "omission_id",
        "reason",
        "group_id",
        "layer",
        "tier",
        "stage",
        "semantic_role",
        "cullable",
        "canonical_source_refs",
        "input_indexes",
        "input_strokes",
        "serialized_length_mm",
        "field_serialized_length_mm",
        "serialized_geometry_sha256",
        "planned_effective_ink_mm2",
        "effective_nibs_mm",
        "priority",
        "cutoff",
    }
)
_INK_BY_LAYER_FIELDS = frozenset(
    {
        "tier",
        "source_ref_count",
        "canonical_source_ref_count",
        "candidate_group_count",
        "selected_group_count",
        "omitted_group_count",
        "candidate_stroke_count",
        "selected_stroke_count",
        "omitted_stroke_count",
        "selected_source_ref_count",
        "omitted_source_ref_count",
        "candidate_serialized_length_mm",
        "selected_serialized_length_mm",
        "omitted_serialized_length_mm",
        "candidate_planned_effective_ink_mm2",
        "selected_planned_effective_ink_mm2",
        "omitted_planned_effective_ink_mm2",
        "group_retention_ratio",
        "stroke_retention_ratio",
        "source_ref_retention_ratio",
        "length_retention_ratio",
        "ink_retention_ratio",
    }
)
_INK_RESERVE_FIELDS = frozenset(
    {
        "present",
        "candidate_group_count",
        "candidate_ink_mm2",
        "fittable_group_count",
        "minimum_group_ink_mm2",
        "requested_coverage",
        "requested_ink_mm2",
        "selected_reserve_group_count",
        "selected_reserve_ink_mm2",
        "achieved",
        "nonzero_selected",
        "final_selected_group_count",
        "final_selected_ink_mm2",
        "final_ink_retention_ratio",
    }
)
_INK_SELECTION_STAGE_FIELDS = frozenset(
    {
        "selected_group_count",
        "selected_ink_mm2",
        "omitted_group_count",
        "omitted_ink_mm2",
    }
)
_INK_SEMANTIC_STATS_FIELDS = frozenset(
    {
        "candidate_group_count",
        "candidate_ink_mm2",
        "selected_group_count",
        "selected_ink_mm2",
        "omitted_group_count",
        "omitted_ink_mm2",
        "retention_ratio",
    }
)
_INK_INPUT_STROKE_FIELDS = frozenset(
    {
        "index",
        "layer",
        "part",
        "source_refs",
        "canonical_source_refs",
        "serialized_length_mm",
        "field_serialized_length_mm",
        "serialized_geometry_sha256",
        "effective_nib_mm",
        "emitted_path_multiplier",
        "planned_effective_ink_mm2",
    }
)


def _canonical_source_root(reference: str) -> str | None:
    parsed = _parse_source_ref(reference)
    if parsed is None:
        return None
    return f"{parsed[0]}/{parsed[1]}"


def _stroke_source_references_for_evidence(stroke: PlotStroke) -> list[str]:
    return sorted(
        {
            item.strip()
            for item in stroke.tags.get("source-refs", "").split(";")
            if item.strip()
        }
    )


def _stroke_canonical_source_roots(stroke: PlotStroke) -> list[str]:
    roots = {
        root
        for reference in _stroke_source_references_for_evidence(stroke)
        if (root := _canonical_source_root(reference)) is not None
    }
    return sorted(roots)


def _serialized_field_stroke_length(stroke: PlotStroke, layout: Layout) -> float:
    points = _serialized_stroke_points(stroke)
    left, top, right, bottom = layout.clip_rect
    length = 0.0
    for start, end in zip(points, points[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        segment_length = hypot(dx, dy)
        if segment_length <= 0:
            continue
        enter = 0.0
        leave = 1.0
        visible = True
        for direction, distance_to_edge in (
            (-dx, start[0] - left),
            (dx, right - start[0]),
            (-dy, start[1] - top),
            (dy, bottom - start[1]),
        ):
            if direction == 0:
                if distance_to_edge < 0:
                    visible = False
                    break
                continue
            parameter = distance_to_edge / direction
            if direction < 0:
                enter = max(enter, parameter)
            else:
                leave = min(leave, parameter)
            if enter > leave:
                visible = False
                break
        if visible:
            length += segment_length * max(0.0, leave - enter)
    return round(length, 9)


def _canonical_json_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _ink_budget_group_geometry_sha256(
    input_strokes: list[dict[str, Any]],
) -> str:
    payload = sorted(
        (
            {
                "layer": value["layer"],
                "part": value["part"],
                "source_refs": value["source_refs"],
                "serialized_geometry_sha256": value["serialized_geometry_sha256"],
            }
            for value in input_strokes
        ),
        key=lambda item: (
            str(item["layer"]),
            str(item["part"]),
            json.dumps(item["source_refs"], separators=(",", ":")),
            str(item["serialized_geometry_sha256"]),
        ),
    )
    return _canonical_json_sha256(payload)


def _ink_budget_partition_sha256(
    *,
    input_count: int,
    retained_input_indexes: list[int],
    entries: list[dict[str, Any]],
) -> str:
    omitted_groups = [
        {
            "group_id": entry["group_id"],
            "input_indexes": sorted(entry["input_indexes"]),
        }
        for entry in sorted(entries, key=lambda item: str(item["group_id"]))
    ]
    payload = {
        "input_count": input_count,
        "retained_input_indexes": sorted(retained_input_indexes),
        "omitted_groups": omitted_groups,
    }
    return _canonical_json_sha256(payload)


def _ink_budget_tier_for_layer(layer: str) -> str:
    return _INK_BUDGET_TIERS.get(layer, "supplemental")


def _ink_budget_stage_for_layer(layer: str) -> str:
    if layer in _INK_ROAD_LAYERS:
        return "road_fill"
    if layer in _INK_FAIR_CONTEXT_LAYERS:
        return "fair_context_fill"
    return "late_fill"


def _ink_budget_tag_values(strokes: Iterable[PlotStroke], key: str) -> tuple[str, ...]:
    return tuple(
        value
        for stroke in strokes
        if (value := stroke.tags.get(key, "").strip().casefold())
    )


def _ink_budget_group_named(strokes: Iterable[PlotStroke]) -> bool:
    return any(
        bool(stroke.name or stroke.tags.get("name") or stroke.tags.get("ref"))
        for stroke in strokes
    )


def _ink_budget_semantic_role(layer: str, strokes: Iterable[PlotStroke]) -> str:
    group = tuple(strokes)
    highways = _ink_budget_tag_values(group, "highway")
    link = any(value.endswith("_link") for value in highways)
    named = _ink_budget_group_named(group)
    if layer == "roads_major":
        return "arterial_link" if link else "arterial_through"
    if layer == "roads_secondary":
        return "secondary_link" if link else "secondary_through"
    if layer == "roads_local":
        return "local_street"
    if layer == "roads_other":
        return "other_road"
    if layer == "road_areas":
        return "road_area"
    if layer == "water_areas":
        water_values = set(_ink_budget_tag_values(group, "water"))
        natural_values = set(_ink_budget_tag_values(group, "natural"))
        waterway_values = set(_ink_budget_tag_values(group, "waterway"))
        explicit_minor = bool(
            water_values & {"pond", "basin"}
            or waterway_values & {"drain", "ditch", "stream"}
        )
        principal = named or bool(
            waterway_values & {"river", "riverbank"}
            or water_values & {"river", "reservoir", "lake"}
            or "bay" in natural_values
        )
        return (
            "principal_water_area"
            if principal and not explicit_minor
            else "minor_water_area"
        )
    if layer == "rivers":
        waterways = set(_ink_budget_tag_values(group, "waterway"))
        return "principal_river" if named or "river" in waterways else "river"
    if layer == "waterways":
        waterways = set(_ink_budget_tag_values(group, "waterway"))
        explicit_minor = bool(waterways & {"drain", "ditch", "stream"})
        principal = named or bool(waterways & {"river", "canal"})
        return (
            "principal_waterway"
            if principal and not explicit_minor
            else "minor_waterway"
        )
    if layer == "railways":
        services = set(_ink_budget_tag_values(group, "service"))
        railways = set(_ink_budget_tag_values(group, "railway"))
        if services & {"yard", "siding", "spur", "crossover"}:
            return "service_rail"
        if railways & {
            "disused",
            "abandoned",
            "razed",
            "construction",
            "proposed",
        }:
            return "disused_rail"
        if "subway" in railways:
            return "subway_rail"
        return "active_principal_rail"
    if layer == "paths":
        return "path_context"
    if layer == "green_space":
        return "park_context"
    return "supplemental"


def _ink_budget_group_id(layer: str, canonical_source_refs: list[str]) -> str:
    digest = _canonical_json_sha256(
        {"layer": layer, "canonical_source_refs": canonical_source_refs}
    )
    return f"ink-group-{digest[:20]}"


@dataclass(frozen=True)
class _InkBudgetGroupFacts:
    group_id: str
    layer: str
    input_indexes: tuple[int, ...]
    source_refs: tuple[str, ...]
    canonical_source_refs: tuple[str, ...]
    serialized_length_mm: float
    semantic_role: str
    stage: str


def _ink_budget_atomic_group_facts(
    strokes: tuple[PlotStroke, ...],
) -> tuple[tuple[_InkBudgetGroupFacts, ...], str | None]:
    parent = list(range(len(strokes)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        if first_root < second_root:
            parent[second_root] = first_root
        else:
            parent[first_root] = second_root

    first_by_source: dict[tuple[str, str], int] = {}
    source_refs_by_index: list[tuple[str, ...]] = []
    canonical_refs_by_index: list[tuple[str, ...]] = []
    for index, stroke in enumerate(strokes):
        source_refs = tuple(_stroke_source_references_for_evidence(stroke))
        if not source_refs:
            return (), "input_stroke_missing_source_refs"
        canonical_ref_values: list[str] = []
        for source_ref in source_refs:
            canonical_ref = _canonical_source_root(source_ref)
            if canonical_ref is None:
                return (), "input_stroke_invalid_source_ref"
            canonical_ref_values.append(canonical_ref)
        canonical = tuple(sorted(set(canonical_ref_values)))
        source_refs_by_index.append(source_refs)
        canonical_refs_by_index.append(canonical)
        for canonical_ref in canonical:
            key = (stroke.layer, canonical_ref)
            previous = first_by_source.setdefault(key, index)
            union(previous, index)

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(strokes)):
        grouped[find(index)].append(index)
    facts: list[_InkBudgetGroupFacts] = []
    for indexes in grouped.values():
        ordered_indexes = tuple(sorted(indexes))
        layers = {strokes[index].layer for index in ordered_indexes}
        if len(layers) != 1:
            return (), "atomic_group_crosses_layers"
        layer = next(iter(layers))
        source_refs = tuple(
            sorted(
                {
                    source_ref
                    for index in ordered_indexes
                    for source_ref in source_refs_by_index[index]
                }
            )
        )
        canonical_refs = tuple(
            sorted(
                {
                    source_ref
                    for index in ordered_indexes
                    for source_ref in canonical_refs_by_index[index]
                }
            )
        )
        group_strokes = tuple(strokes[index] for index in ordered_indexes)
        facts.append(
            _InkBudgetGroupFacts(
                group_id=_ink_budget_group_id(layer, list(canonical_refs)),
                layer=layer,
                input_indexes=ordered_indexes,
                source_refs=source_refs,
                canonical_source_refs=canonical_refs,
                serialized_length_mm=round(
                    sum(
                        round(_serialized_stroke_length(stroke), 9)
                        for stroke in group_strokes
                    ),
                    9,
                ),
                semantic_role=_ink_budget_semantic_role(layer, group_strokes),
                stage=_ink_budget_stage_for_layer(layer),
            )
        )
    return tuple(sorted(facts, key=lambda item: item.group_id)), None


def _ink_budget_tile_order_lookup() -> dict[tuple[int, int], int]:
    centre_x = 1.5
    centre_y = 1.5
    tiles = sorted(
        ((x, y) for y in range(4) for x in range(4)),
        key=lambda tile: (
            (tile[0] - centre_x) ** 2 + (tile[1] - centre_y) ** 2,
            tile[1],
            tile[0],
        ),
    )
    return {tile: index for index, tile in enumerate(tiles)}


_INK_BUDGET_TILE_ORDER = _ink_budget_tile_order_lookup()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _canonical_integer_list(value: Any) -> list[int] | None:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in value
    ):
        return None
    return value if value == sorted(set(value)) else None


def _ink_budget_entry_error(
    entry: dict[str, Any],
    pre_budget_strokes: tuple[PlotStroke, ...],
    layout: Layout,
    atomic_groups_by_id: dict[str, _InkBudgetGroupFacts],
) -> str | None:
    if set(entry) != _INK_ENTRY_FIELDS:
        return "invalid_entry_structure"
    if entry.get("schema_version") != _INK_BUDGET_SCHEMA_VERSION:
        return "unsupported_schema_version"
    if entry.get("policy") != _INK_BALANCED_POLICY:
        return "unsupported_policy"
    if entry.get("reason") != "ink_budget_capacity":
        return "unsupported_omission_reason"
    if not isinstance(entry.get("group_id"), str) or not entry["group_id"]:
        return "missing_group_id"
    layer = entry.get("layer")
    if not isinstance(layer, str) or not layer:
        return "missing_layer"
    if entry.get("tier") != _ink_budget_tier_for_layer(layer):
        return "omission_tier_layer_mismatch"
    if entry.get("cullable") is not True:
        return "omission_group_is_not_explicitly_cullable"
    if entry.get("stage") != _ink_budget_stage_for_layer(layer):
        return "omission_stage_layer_mismatch"

    input_indexes = _canonical_integer_list(entry.get("input_indexes"))
    if input_indexes is None or not input_indexes:
        return "invalid_input_indexes"
    input_strokes_value = entry.get("input_strokes")
    if not isinstance(input_strokes_value, list) or len(input_strokes_value) != len(
        input_indexes
    ):
        return "input_stroke_count_mismatch"
    if any(not isinstance(value, dict) for value in input_strokes_value):
        return "invalid_input_stroke_evidence"
    input_strokes = [value for value in input_strokes_value if isinstance(value, dict)]
    if [value.get("index") for value in input_strokes] != input_indexes:
        return "input_stroke_index_order_mismatch"
    if any(set(value) != _INK_INPUT_STROKE_FIELDS for value in input_strokes):
        return "invalid_input_stroke_structure"

    actual_canonical_refs: set[str] = set()
    actual_lengths: list[float] = []
    actual_field_lengths: list[float] = []
    actual_nibs: list[float] = []
    actual_ink: list[float] = []
    actual_inputs: list[PlotStroke] = []
    for value, index in zip(input_strokes, input_indexes):
        if index >= len(pre_budget_strokes):
            return "invalid_input_stroke_index"
        actual = pre_budget_strokes[index]
        actual_inputs.append(actual)
        if value.get("layer") != actual.layer or actual.layer != layer:
            return "input_stroke_layer_mismatch"
        if value.get("part") != str(actual.part):
            return "input_stroke_part_mismatch"
        source_refs = _stroke_source_references_for_evidence(actual)
        if not source_refs:
            return "input_stroke_missing_source_refs"
        if value.get("source_refs") != source_refs:
            return "input_stroke_source_ref_mismatch"
        if any(_parse_source_ref(reference) is None for reference in source_refs):
            return "input_stroke_invalid_source_ref"
        canonical_refs = _stroke_canonical_source_roots(actual)
        if not canonical_refs:
            return "input_stroke_invalid_canonical_source_refs"
        if value.get("canonical_source_refs") != canonical_refs:
            return "input_stroke_canonical_source_ref_mismatch"
        actual_canonical_refs.update(canonical_refs)

        actual_length = round(_serialized_stroke_length(actual), 9)
        recorded_length = _finite_number(value.get("serialized_length_mm"))
        if recorded_length is None or abs(recorded_length - actual_length) > 1e-9:
            return "input_stroke_length_mismatch"
        actual_lengths.append(actual_length)
        actual_field_length = _serialized_field_stroke_length(actual, layout)
        recorded_field_length = _finite_number(value.get("field_serialized_length_mm"))
        if (
            recorded_field_length is None
            or abs(recorded_field_length - actual_field_length) > 1e-9
        ):
            return "input_stroke_field_length_mismatch"
        actual_field_lengths.append(actual_field_length)
        if value.get("serialized_geometry_sha256") != _serialized_stroke_sha256(actual):
            return "input_stroke_geometry_fingerprint_mismatch"

        nib_mm = _finite_number(value.get("effective_nib_mm"))
        emitted_path_multiplier = value.get("emitted_path_multiplier")
        planned_ink_mm2 = _finite_number(value.get("planned_effective_ink_mm2"))
        if (
            nib_mm is None
            or nib_mm <= 0
            or isinstance(emitted_path_multiplier, bool)
            or not isinstance(emitted_path_multiplier, int)
            or emitted_path_multiplier <= 0
            or emitted_path_multiplier > 24
            or planned_ink_mm2 is None
        ):
            return "invalid_input_stroke_ink_measurement"
        expected_ink = round(actual_field_length * nib_mm * emitted_path_multiplier, 9)
        if abs(planned_ink_mm2 - expected_ink) > 1e-9:
            return "input_stroke_ink_measurement_mismatch"
        actual_nibs.append(nib_mm)
        actual_ink.append(expected_ink)

    canonical_source_refs = entry.get("canonical_source_refs")
    if canonical_source_refs != sorted(actual_canonical_refs):
        return "omission_canonical_source_ref_union_mismatch"
    if entry.get("group_id") != _ink_budget_group_id(
        layer, sorted(actual_canonical_refs)
    ):
        return "omission_group_id_mismatch"
    atomic_group = atomic_groups_by_id.get(str(entry["group_id"]))
    if atomic_group is None or list(atomic_group.input_indexes) != input_indexes:
        return "omission_atomic_source_group_mismatch"
    aggregate_length = _finite_number(entry.get("serialized_length_mm"))
    if (
        aggregate_length is None
        or abs(aggregate_length - round(sum(actual_lengths), 9)) > 1e-9
    ):
        return "omission_length_mismatch"
    aggregate_field_length = _finite_number(entry.get("field_serialized_length_mm"))
    if (
        aggregate_field_length is None
        or abs(aggregate_field_length - round(sum(actual_field_lengths), 9)) > 1e-9
    ):
        return "omission_field_length_mismatch"
    effective_nibs = entry.get("effective_nibs_mm")
    expected_nibs = sorted(set(actual_nibs))
    if effective_nibs != expected_nibs:
        return "omission_effective_nib_union_mismatch"
    planned_ink = _finite_number(entry.get("planned_effective_ink_mm2"))
    if planned_ink is None or abs(planned_ink - round(sum(actual_ink), 9)) > 1e-9:
        return "omission_ink_measurement_mismatch"
    if entry.get("serialized_geometry_sha256") != _ink_budget_group_geometry_sha256(
        input_strokes
    ):
        return "omission_geometry_fingerprint_mismatch"

    expected_semantic_role = _ink_budget_semantic_role(layer, actual_inputs)
    if entry.get("semantic_role") != expected_semantic_role:
        return "omission_semantic_role_mismatch"

    priority = entry.get("priority")
    if not isinstance(priority, dict) or set(priority) != {
        *_INK_PRIORITY_INTEGER_FIELDS,
        *_INK_PRIORITY_BOOLEAN_FIELDS,
        "stable_group_sha256",
    }:
        return "invalid_priority_structure"
    for field in _INK_PRIORITY_INTEGER_FIELDS:
        priority_value = priority.get(field)
        if (
            isinstance(priority_value, bool)
            or not isinstance(priority_value, int)
            or priority_value < 0
        ):
            return "invalid_priority_integer"
    if any(
        not isinstance(priority.get(field), bool)
        for field in _INK_PRIORITY_BOOLEAN_FIELDS
    ):
        return "invalid_priority_boolean"
    if not _valid_sha256(priority.get("stable_group_sha256")):
        return "invalid_priority_group_fingerprint"
    if priority.get("stable_group_sha256") != entry.get("serialized_geometry_sha256"):
        return "priority_group_fingerprint_mismatch"
    expected_max_road_rank = 0
    for stroke in actual_inputs:
        try:
            road_rank = max(0, int(float(stroke.tags.get("road-rank", "0"))))
        except (TypeError, ValueError, OverflowError):
            road_rank = 0
        expected_max_road_rank = max(expected_max_road_rank, road_rank)
    if priority.get("max_road_rank") != expected_max_road_rank:
        return "priority_road_rank_mismatch"
    if priority.get("semantic_rank") != _INK_SEMANTIC_RANK[expected_semantic_role]:
        return "priority_semantic_rank_mismatch"
    expected_named = _ink_budget_group_named(actual_inputs)
    if priority.get("named") is not expected_named:
        return "priority_named_mismatch"
    expected_bridge_or_tunnel = any(
        bool(value and value.casefold() not in {"no", "false", "0"})
        for stroke in actual_inputs
        for value in (stroke.tags.get("bridge"), stroke.tags.get("tunnel"))
    )
    if priority.get("bridge_or_tunnel") is not expected_bridge_or_tunnel:
        return "priority_bridge_or_tunnel_mismatch"
    expected_link = any(
        value.endswith("_link")
        for value in _ink_budget_tag_values(actual_inputs, "highway")
    )
    if priority.get("link") is not expected_link:
        return "priority_link_mismatch"
    expected_rail_service = expected_semantic_role == "service_rail"
    if priority.get("rail_service") is not expected_rail_service:
        return "priority_rail_service_mismatch"
    expected_active_rail = expected_semantic_role == "active_principal_rail"
    if priority.get("active_rail") is not expected_active_rail:
        return "priority_active_rail_mismatch"
    expected_principal_water = expected_semantic_role in {
        "principal_water_area",
        "principal_river",
        "principal_waterway",
    }
    if priority.get("principal_water") is not expected_principal_water:
        return "priority_principal_water_mismatch"
    points = [
        point for stroke in actual_inputs for point in _serialized_stroke_points(stroke)
    ]
    centre_x = (
        min(point[0] for point in points) + max(point[0] for point in points)
    ) / 2
    centre_y = (
        min(point[1] for point in points) + max(point[1] for point in points)
    ) / 2
    relative_x = (centre_x - layout.map_x_mm) / layout.map_width_mm
    relative_y = (centre_y - layout.map_y_mm) / layout.map_height_mm
    tile_x = min(3, max(0, floor(relative_x * 4)))
    tile_y = min(3, max(0, floor(relative_y * 4)))
    if priority.get("tile_x") != tile_x or priority.get("tile_y") != tile_y:
        return "priority_tile_mismatch"
    if priority.get("tile_order") != _INK_BUDGET_TILE_ORDER[(tile_x, tile_y)]:
        return "priority_tile_order_mismatch"
    if priority.get("connected_to_selected_topology") is not (
        priority.get("shared_topology_node_count", 0) > 0
    ):
        return "priority_topology_connection_mismatch"

    cutoff = entry.get("cutoff")
    if not isinstance(cutoff, dict) or set(cutoff) != set(_INK_CUTOFF_NUMBER_FIELDS):
        return "invalid_cutoff_structure"
    cutoff_values: dict[str, float] = {}
    for field in _INK_CUTOFF_NUMBER_FIELDS:
        cutoff_value = _finite_number(cutoff.get(field))
        if cutoff_value is None or cutoff_value < 0:
            return "invalid_cutoff_measurement"
        cutoff_values[field] = cutoff_value
    if abs(cutoff_values["group_planned_effective_ink_mm2"] - planned_ink) > 1e-9:
        return "cutoff_group_ink_mismatch"
    expected_remaining = round(
        max(
            0.0,
            cutoff_values["budget_ink_mm2"]
            - cutoff_values["fixed_ink_mm2"]
            - cutoff_values["selected_map_ink_before_mm2"],
        ),
        9,
    )
    if abs(cutoff_values["remaining_ink_before_mm2"] - expected_remaining) > 1e-9:
        return "cutoff_remaining_ink_mismatch"
    expected_excess = round(
        max(
            0.0,
            cutoff_values["fixed_ink_mm2"]
            + cutoff_values["selected_map_ink_before_mm2"]
            + planned_ink
            - cutoff_values["budget_ink_mm2"],
        ),
        9,
    )
    if (
        expected_excess <= 0
        or abs(cutoff_values["would_exceed_by_mm2"] - expected_excess) > 1e-9
    ):
        return "cutoff_excess_mismatch"
    return None


def _ink_budget_diagnostics_audit(
    evidence: Any,
    *,
    pre_budget_strokes: tuple[PlotStroke, ...],
    retained_indexes: list[int],
    omitted_indexes: list[int],
    valid_entries: list[dict[str, Any]],
    selection_partition_sha256: str | None,
    ledger_valid: bool,
    groups: tuple[_InkBudgetGroupFacts, ...],
    group_error: str | None,
) -> _InkBudgetDiagnosticsAudit:
    if evidence is None:
        return _InkBudgetDiagnosticsAudit(
            supplied=False,
            valid=False,
            uncullable_source_group_count=None,
            invalid=(("diagnostics", "diagnostics_not_supplied"),),
        )
    diagnostics = evidence.diagnostics if hasattr(evidence, "diagnostics") else evidence
    if not isinstance(diagnostics, dict):
        return _InkBudgetDiagnosticsAudit(
            supplied=True,
            valid=False,
            uncullable_source_group_count=None,
            invalid=(("diagnostics", "diagnostics_is_not_an_object"),),
        )

    invalid: list[tuple[str, str]] = []

    def reject(component: str, reason: str) -> None:
        invalid.append((component, reason))

    def integer(
        value: Any,
        *,
        component: str,
        field: str,
    ) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reject(component, f"invalid_{field}")
            return None
        return value

    def number(
        value: Any,
        *,
        component: str,
        field: str,
    ) -> float | None:
        resolved = _finite_number(value)
        if resolved is None or resolved < 0:
            reject(component, f"invalid_{field}")
            return None
        return resolved

    def same_number(
        actual: float | None,
        expected: float,
        *,
        component: str,
        field: str,
    ) -> None:
        if actual is not None and abs(actual - expected) > 1e-9:
            reject(component, f"{field}_mismatch")

    if diagnostics.get("schema_version") != _INK_BUDGET_SCHEMA_VERSION:
        reject("diagnostics", "unsupported_schema_version")
    if diagnostics.get("policy") != _INK_BALANCED_POLICY:
        reject("diagnostics", "unsupported_policy")
    uncullable = integer(
        diagnostics.get("uncullable_source_group_count"),
        component="diagnostics",
        field="uncullable_source_group_count",
    )
    if uncullable is not None and uncullable != 0:
        reject("diagnostics", "uncullable_source_groups_present")
    if not ledger_valid:
        reject("diagnostics", "ledger_not_valid_for_reconciliation")

    if group_error is not None:
        reject("diagnostics", group_error)
    retained_set = set(retained_indexes)
    omitted_set = set(omitted_indexes)
    retained_groups = tuple(
        group for group in groups if set(group.input_indexes).issubset(retained_set)
    )
    omitted_groups = tuple(
        group for group in groups if set(group.input_indexes).issubset(omitted_set)
    )
    if len(retained_groups) + len(omitted_groups) != len(groups):
        reject("diagnostics", "atomic_group_split_across_partition")

    entry_by_group = {
        str(entry["group_id"]): entry
        for entry in valid_entries
        if isinstance(entry.get("group_id"), str)
    }
    if set(entry_by_group) != {group.group_id for group in omitted_groups}:
        reject("diagnostics", "omitted_group_evidence_set_mismatch")
    omitted_ink_by_group = {
        group_id: float(entry["planned_effective_ink_mm2"])
        for group_id, entry in entry_by_group.items()
        if _finite_number(entry.get("planned_effective_ink_mm2")) is not None
    }

    count_expectations = {
        "input_stroke_count": len(pre_budget_strokes),
        "retained_stroke_count": len(retained_indexes),
        "omitted_stroke_count": len(omitted_indexes),
        "input_group_count": len(groups),
        "retained_group_count": len(retained_groups),
        "omitted_group_count": len(omitted_groups),
    }
    for field, expected in count_expectations.items():
        value = integer(diagnostics.get(field), component="diagnostics", field=field)
        if value is not None and value != expected:
            reject("diagnostics", f"{field}_mismatch")
    if diagnostics.get("retained_input_indexes") != retained_indexes:
        reject("diagnostics", "retained_input_indexes_mismatch")
    if diagnostics.get("omitted_input_indexes") != omitted_indexes:
        reject("diagnostics", "omitted_input_indexes_mismatch")
    if diagnostics.get("selection_partition_sha256") != selection_partition_sha256:
        reject("diagnostics", "selection_partition_fingerprint_mismatch")

    target_coverage = number(
        diagnostics.get("target_coverage"),
        component="diagnostics",
        field="target_coverage",
    )
    field_area = number(
        diagnostics.get("field_area_mm2"),
        component="diagnostics",
        field="field_area_mm2",
    )
    budget_ink = number(
        diagnostics.get("budget_ink_mm2"),
        component="diagnostics",
        field="budget_ink_mm2",
    )
    fixed_ink = number(
        diagnostics.get("fixed_ink_mm2"),
        component="diagnostics",
        field="fixed_ink_mm2",
    )
    if target_coverage is not None and not (0 < target_coverage <= 0.28):
        reject("diagnostics", "target_coverage_out_of_policy")
    if field_area is not None and field_area <= 0:
        reject("diagnostics", "field_area_is_not_positive")
    if field_area is not None and target_coverage is not None:
        same_number(
            budget_ink,
            round(field_area * target_coverage, 9),
            component="diagnostics",
            field="budget_ink_mm2",
        )
    for entry in valid_entries:
        cutoff = entry.get("cutoff")
        if not isinstance(cutoff, dict):
            continue
        omission_id = str(entry.get("omission_id", "unknown"))
        if (
            budget_ink is not None
            and abs(float(cutoff["budget_ink_mm2"]) - budget_ink) > 1e-9
        ):
            reject(omission_id, "diagnostic_budget_cutoff_mismatch")
        if (
            fixed_ink is not None
            and abs(float(cutoff["fixed_ink_mm2"]) - fixed_ink) > 1e-9
        ):
            reject(omission_id, "diagnostic_fixed_ink_cutoff_mismatch")

    by_layer_value = diagnostics.get("by_layer")
    if not isinstance(by_layer_value, dict):
        reject("by_layer", "by_layer_is_not_an_object")
        by_layer_value = {}
    expected_layers = {group.layer for group in groups}
    if set(by_layer_value) != expected_layers:
        reject("by_layer", "layer_set_mismatch")
    layer_claims: dict[str, dict[str, float | int]] = {}
    for layer in sorted(expected_layers):
        component = f"by_layer.{layer}"
        record = by_layer_value.get(layer)
        if not isinstance(record, dict):
            reject(component, "layer_summary_is_not_an_object")
            continue
        if set(record) != _INK_BY_LAYER_FIELDS:
            reject(component, "invalid_layer_summary_structure")
        layer_groups = tuple(group for group in groups if group.layer == layer)
        selected_layer_groups = tuple(
            group for group in retained_groups if group.layer == layer
        )
        omitted_layer_groups = tuple(
            group for group in omitted_groups if group.layer == layer
        )
        candidate_indexes = {
            index for group in layer_groups for index in group.input_indexes
        }
        selected_indexes = {
            index for group in selected_layer_groups for index in group.input_indexes
        }
        omitted_layer_indexes = {
            index for group in omitted_layer_groups for index in group.input_indexes
        }
        candidate_refs = {
            source_ref for group in layer_groups for source_ref in group.source_refs
        }
        selected_refs = {
            source_ref
            for group in selected_layer_groups
            for source_ref in group.source_refs
        }
        omitted_refs = {
            source_ref
            for group in omitted_layer_groups
            for source_ref in group.source_refs
        }
        candidate_canonical_refs = {
            source_ref
            for group in layer_groups
            for source_ref in group.canonical_source_refs
        }
        expected_integers = {
            "source_ref_count": len(candidate_refs),
            "canonical_source_ref_count": len(candidate_canonical_refs),
            "candidate_group_count": len(layer_groups),
            "selected_group_count": len(selected_layer_groups),
            "omitted_group_count": len(omitted_layer_groups),
            "candidate_stroke_count": len(candidate_indexes),
            "selected_stroke_count": len(selected_indexes),
            "omitted_stroke_count": len(omitted_layer_indexes),
            "selected_source_ref_count": len(selected_refs),
            "omitted_source_ref_count": len(omitted_refs),
        }
        parsed_integers: dict[str, int] = {}
        for field, expected in expected_integers.items():
            value = integer(record.get(field), component=component, field=field)
            if value is not None:
                parsed_integers[field] = value
                if value != expected:
                    reject(component, f"{field}_mismatch")
        if record.get("tier") != _ink_budget_tier_for_layer(layer):
            reject(component, "tier_mismatch")
        expected_candidate_length = round(
            sum(group.serialized_length_mm for group in layer_groups), 9
        )
        expected_selected_length = round(
            sum(group.serialized_length_mm for group in selected_layer_groups), 9
        )
        expected_omitted_length = round(
            sum(group.serialized_length_mm for group in omitted_layer_groups), 9
        )
        for field, expected_length in {
            "candidate_serialized_length_mm": expected_candidate_length,
            "selected_serialized_length_mm": expected_selected_length,
            "omitted_serialized_length_mm": expected_omitted_length,
        }.items():
            length_value = number(record.get(field), component=component, field=field)
            same_number(
                length_value,
                expected_length,
                component=component,
                field=field,
            )
        candidate_ink = number(
            record.get("candidate_planned_effective_ink_mm2"),
            component=component,
            field="candidate_planned_effective_ink_mm2",
        )
        selected_ink = number(
            record.get("selected_planned_effective_ink_mm2"),
            component=component,
            field="selected_planned_effective_ink_mm2",
        )
        omitted_ink = number(
            record.get("omitted_planned_effective_ink_mm2"),
            component=component,
            field="omitted_planned_effective_ink_mm2",
        )
        expected_omitted_ink = round(
            sum(
                omitted_ink_by_group.get(group.group_id, 0.0)
                for group in omitted_layer_groups
            ),
            9,
        )
        same_number(
            omitted_ink,
            expected_omitted_ink,
            component=component,
            field="omitted_planned_effective_ink_mm2",
        )
        if None not in (candidate_ink, selected_ink, omitted_ink):
            assert candidate_ink is not None
            assert selected_ink is not None
            assert omitted_ink is not None
            if abs(candidate_ink - round(selected_ink + omitted_ink, 9)) > 1e-9:
                reject(component, "planned_ink_partition_mismatch")
            layer_claims[layer] = {
                **parsed_integers,
                "candidate_ink_mm2": candidate_ink,
                "selected_ink_mm2": selected_ink,
                "omitted_ink_mm2": omitted_ink,
            }

        ratio_inputs: dict[str, tuple[float, float]] = {
            "group_retention_ratio": (
                len(selected_layer_groups),
                len(layer_groups),
            ),
            "stroke_retention_ratio": (len(selected_indexes), len(candidate_indexes)),
            "source_ref_retention_ratio": (len(selected_refs), len(candidate_refs)),
            "length_retention_ratio": (
                expected_selected_length,
                expected_candidate_length,
            ),
        }
        if candidate_ink is not None and selected_ink is not None:
            ratio_inputs["ink_retention_ratio"] = (selected_ink, candidate_ink)
        for field, (numerator, denominator) in ratio_inputs.items():
            ratio_value = number(record.get(field), component=component, field=field)
            expected_ratio = (
                1.0 if denominator <= 0 else round(numerator / denominator, 9)
            )
            same_number(
                ratio_value,
                expected_ratio,
                component=component,
                field=field,
            )

    total_selected_ink = round(
        sum(
            float(claims.get("selected_ink_mm2", 0.0))
            for claims in layer_claims.values()
        ),
        9,
    )
    selected_map_ink = number(
        diagnostics.get("selected_map_ink_mm2"),
        component="diagnostics",
        field="selected_map_ink_mm2",
    )
    same_number(
        selected_map_ink,
        total_selected_ink,
        component="diagnostics",
        field="selected_map_ink_mm2",
    )
    planned_total_ink = number(
        diagnostics.get("planned_total_ink_mm2"),
        component="diagnostics",
        field="planned_total_ink_mm2",
    )
    if fixed_ink is not None:
        same_number(
            planned_total_ink,
            round(fixed_ink + total_selected_ink, 9),
            component="diagnostics",
            field="planned_total_ink_mm2",
        )
    if planned_total_ink is not None and budget_ink is not None:
        if planned_total_ink > budget_ink + 1e-9:
            reject("diagnostics", "planned_total_exceeds_budget")

    reserves_value = diagnostics.get("reserves")
    if not isinstance(reserves_value, dict):
        reject("reserves", "reserves_is_not_an_object")
        reserves_value = {}
    if set(reserves_value) != set(_INK_RESERVE_LAYERS):
        reject("reserves", "reserve_layer_set_mismatch")
    reserve_claims: dict[str, dict[str, float | int]] = {}
    for layer in _INK_RESERVE_LAYERS:
        component = f"reserves.{layer}"
        record = reserves_value.get(layer)
        if not isinstance(record, dict):
            reject(component, "reserve_is_not_an_object")
            continue
        if set(record) != _INK_RESERVE_FIELDS:
            reject(component, "invalid_reserve_structure")
        layer_values = layer_claims.get(layer, {})
        candidate_group_count = int(layer_values.get("candidate_group_count", 0))
        selected_group_count = int(layer_values.get("selected_group_count", 0))
        candidate_ink = float(layer_values.get("candidate_ink_mm2", 0.0))
        final_selected_ink = float(layer_values.get("selected_ink_mm2", 0.0))
        if record.get("present") is not (candidate_group_count > 0):
            reject(component, "present_mismatch")
        parsed_candidate_count = integer(
            record.get("candidate_group_count"),
            component=component,
            field="candidate_group_count",
        )
        if (
            parsed_candidate_count is not None
            and parsed_candidate_count != candidate_group_count
        ):
            reject(component, "candidate_group_count_mismatch")
        parsed_candidate_ink = number(
            record.get("candidate_ink_mm2"),
            component=component,
            field="candidate_ink_mm2",
        )
        same_number(
            parsed_candidate_ink,
            candidate_ink,
            component=component,
            field="candidate_ink_mm2",
        )
        fittable_count = integer(
            record.get("fittable_group_count"),
            component=component,
            field="fittable_group_count",
        )
        if fittable_count is not None and fittable_count > candidate_group_count:
            reject(component, "fittable_group_count_exceeds_candidates")
        minimum_group_ink = number(
            record.get("minimum_group_ink_mm2"),
            component=component,
            field="minimum_group_ink_mm2",
        )
        if candidate_group_count == 0:
            same_number(
                minimum_group_ink,
                0.0,
                component=component,
                field="minimum_group_ink_mm2",
            )
        elif minimum_group_ink is not None and minimum_group_ink > candidate_ink + 1e-9:
            reject(component, "minimum_group_ink_exceeds_candidate_ink")
        expected_coverage = 0.02 if layer in _INK_ROAD_LAYERS else 0.005
        requested_coverage = number(
            record.get("requested_coverage"),
            component=component,
            field="requested_coverage",
        )
        same_number(
            requested_coverage,
            expected_coverage,
            component=component,
            field="requested_coverage",
        )
        requested_ink = number(
            record.get("requested_ink_mm2"),
            component=component,
            field="requested_ink_mm2",
        )
        if field_area is not None:
            same_number(
                requested_ink,
                round(min(candidate_ink, field_area * expected_coverage), 9),
                component=component,
                field="requested_ink_mm2",
            )
        reserve_group_count = integer(
            record.get("selected_reserve_group_count"),
            component=component,
            field="selected_reserve_group_count",
        )
        reserve_ink = number(
            record.get("selected_reserve_ink_mm2"),
            component=component,
            field="selected_reserve_ink_mm2",
        )
        if reserve_group_count is not None:
            if reserve_group_count > selected_group_count:
                reject(component, "reserve_group_count_exceeds_final_selection")
            if fittable_count is not None and reserve_group_count > fittable_count:
                reject(component, "reserve_group_count_exceeds_fittable_groups")
        if reserve_ink is not None and reserve_ink > final_selected_ink + 1e-9:
            reject(component, "reserve_ink_exceeds_final_selection")
        if record.get("achieved") is not (
            requested_ink is not None
            and reserve_ink is not None
            and reserve_ink + 1e-9 >= requested_ink
        ):
            reject(component, "achieved_mismatch")
        if record.get("nonzero_selected") is not (
            reserve_group_count is not None and reserve_group_count > 0
        ):
            reject(component, "nonzero_selected_mismatch")
        if (
            requested_ink is not None
            and requested_ink > 1e-9
            and fittable_count is not None
            and fittable_count > 0
            and reserve_group_count == 0
        ):
            reject(component, "fittable_reserve_has_no_selection")
        final_group_count = integer(
            record.get("final_selected_group_count"),
            component=component,
            field="final_selected_group_count",
        )
        if final_group_count is not None and final_group_count != selected_group_count:
            reject(component, "final_selected_group_count_mismatch")
        final_ink = number(
            record.get("final_selected_ink_mm2"),
            component=component,
            field="final_selected_ink_mm2",
        )
        same_number(
            final_ink,
            final_selected_ink,
            component=component,
            field="final_selected_ink_mm2",
        )
        retention_ratio = number(
            record.get("final_ink_retention_ratio"),
            component=component,
            field="final_ink_retention_ratio",
        )
        expected_retention = (
            1.0 if candidate_ink <= 0 else round(final_selected_ink / candidate_ink, 9)
        )
        same_number(
            retention_ratio,
            expected_retention,
            component=component,
            field="final_ink_retention_ratio",
        )
        if reserve_group_count is not None and reserve_ink is not None:
            reserve_claims[layer] = {
                "group_count": reserve_group_count,
                "ink_mm2": reserve_ink,
            }

    semantic_value = diagnostics.get("semantic_priorities")
    if not isinstance(semantic_value, dict) or set(semantic_value) != {
        "ordering",
        "by_role",
    }:
        reject("semantic_priorities", "invalid_semantic_priority_structure")
        semantic_value = {}
    if semantic_value.get("ordering") != list(_INK_SEMANTIC_ROLES):
        reject("semantic_priorities", "semantic_role_order_mismatch")
    by_role_value = semantic_value.get("by_role")
    if not isinstance(by_role_value, dict):
        reject("semantic_priorities.by_role", "by_role_is_not_an_object")
        by_role_value = {}
    if set(by_role_value) != set(_INK_SEMANTIC_ROLES):
        reject("semantic_priorities.by_role", "semantic_role_set_mismatch")
    semantic_claims: dict[str, dict[str, float]] = {}
    for role in _INK_SEMANTIC_ROLES:
        component = f"semantic_priorities.by_role.{role}"
        record = by_role_value.get(role)
        if not isinstance(record, dict):
            reject(component, "semantic_summary_is_not_an_object")
            continue
        if set(record) != _INK_SEMANTIC_STATS_FIELDS:
            reject(component, "invalid_semantic_summary_structure")
        role_groups = tuple(group for group in groups if group.semantic_role == role)
        omitted_role_groups = tuple(
            group for group in omitted_groups if group.semantic_role == role
        )
        expected_counts = {
            "candidate_group_count": len(role_groups),
            "selected_group_count": len(role_groups) - len(omitted_role_groups),
            "omitted_group_count": len(omitted_role_groups),
        }
        for field, expected in expected_counts.items():
            value = integer(record.get(field), component=component, field=field)
            if value is not None and value != expected:
                reject(component, f"{field}_mismatch")
        candidate_role_ink = number(
            record.get("candidate_ink_mm2"),
            component=component,
            field="candidate_ink_mm2",
        )
        selected_role_ink = number(
            record.get("selected_ink_mm2"),
            component=component,
            field="selected_ink_mm2",
        )
        omitted_role_ink = number(
            record.get("omitted_ink_mm2"),
            component=component,
            field="omitted_ink_mm2",
        )
        expected_omitted_role_ink = round(
            sum(
                omitted_ink_by_group.get(group.group_id, 0.0)
                for group in omitted_role_groups
            ),
            9,
        )
        same_number(
            omitted_role_ink,
            expected_omitted_role_ink,
            component=component,
            field="omitted_ink_mm2",
        )
        if None not in (candidate_role_ink, selected_role_ink, omitted_role_ink):
            assert candidate_role_ink is not None
            assert selected_role_ink is not None
            assert omitted_role_ink is not None
            if (
                abs(candidate_role_ink - round(selected_role_ink + omitted_role_ink, 9))
                > 1e-9
            ):
                reject(component, "semantic_ink_partition_mismatch")
            semantic_claims[role] = {
                "candidate": candidate_role_ink,
                "selected": selected_role_ink,
                "omitted": omitted_role_ink,
            }
        retention = number(
            record.get("retention_ratio"),
            component=component,
            field="retention_ratio",
        )
        if candidate_role_ink is not None and selected_role_ink is not None:
            expected_retention = (
                1.0
                if candidate_role_ink <= 0
                else round(selected_role_ink / candidate_role_ink, 9)
            )
            same_number(
                retention,
                expected_retention,
                component=component,
                field="retention_ratio",
            )
    for layer, claims in layer_claims.items():
        roles = {group.semantic_role for group in groups if group.layer == layer}
        for semantic_field, layer_field in (
            ("candidate", "candidate_ink_mm2"),
            ("selected", "selected_ink_mm2"),
            ("omitted", "omitted_ink_mm2"),
        ):
            role_total = round(
                sum(
                    semantic_claims.get(role, {}).get(semantic_field, 0.0)
                    for role in roles
                ),
                9,
            )
            if abs(role_total - float(claims.get(layer_field, 0.0))) > 1e-9:
                reject(
                    f"semantic_priorities.{layer}",
                    f"{semantic_field}_ink_layer_total_mismatch",
                )

    stages_value = diagnostics.get("selection_stages")
    if not isinstance(stages_value, dict):
        reject("selection_stages", "selection_stages_is_not_an_object")
        stages_value = {}
    if set(stages_value) != set(_INK_SELECTION_STAGES):
        reject("selection_stages", "selection_stage_set_mismatch")
    expected_stage_counts = {
        stage: {
            "selected_group_count": 0,
            "selected_ink_mm2": 0.0,
            "omitted_group_count": 0,
            "omitted_ink_mm2": 0.0,
        }
        for stage in _INK_SELECTION_STAGES
    }
    for layer, reserve in reserve_claims.items():
        expected_stage_counts["reserve_prefill"]["selected_group_count"] += int(
            reserve["group_count"]
        )
        expected_stage_counts["reserve_prefill"]["selected_ink_mm2"] += float(
            reserve["ink_mm2"]
        )
    for layer, claims in layer_claims.items():
        reserve = reserve_claims.get(layer, {"group_count": 0, "ink_mm2": 0.0})
        stage = _ink_budget_stage_for_layer(layer)
        expected_stage_counts[stage]["selected_group_count"] += int(
            claims.get("selected_group_count", 0)
        ) - int(reserve["group_count"])
        expected_stage_counts[stage]["selected_ink_mm2"] += float(
            claims.get("selected_ink_mm2", 0.0)
        ) - float(reserve["ink_mm2"])
    for group in omitted_groups:
        expected_stage_counts[group.stage]["omitted_group_count"] += 1
        expected_stage_counts[group.stage]["omitted_ink_mm2"] += (
            omitted_ink_by_group.get(group.group_id, 0.0)
        )
    for stage in _INK_SELECTION_STAGES:
        component = f"selection_stages.{stage}"
        record = stages_value.get(stage)
        if not isinstance(record, dict):
            reject(component, "selection_stage_is_not_an_object")
            continue
        if set(record) != _INK_SELECTION_STAGE_FIELDS:
            reject(component, "invalid_selection_stage_structure")
        stage_expected = expected_stage_counts[stage]
        for field in ("selected_group_count", "omitted_group_count"):
            expected_group_count = int(stage_expected[field])
            stage_count = integer(record.get(field), component=component, field=field)
            if stage_count is not None and stage_count != expected_group_count:
                reject(component, f"{field}_mismatch")
        for field in ("selected_ink_mm2", "omitted_ink_mm2"):
            expected_stage_ink = round(float(stage_expected[field]), 9)
            stage_ink = number(record.get(field), component=component, field=field)
            same_number(
                stage_ink,
                expected_stage_ink,
                component=component,
                field=field,
            )

    return _InkBudgetDiagnosticsAudit(
        supplied=True,
        valid=not invalid,
        uncullable_source_group_count=uncullable,
        invalid=tuple(invalid),
    )


def _stroke_partition_identity(stroke: PlotStroke) -> tuple[Any, ...]:
    return (
        stroke.layer,
        str(stroke.part),
        tuple(_stroke_source_references_for_evidence(stroke)),
        round(_serialized_stroke_length(stroke), 9),
        _serialized_stroke_sha256(stroke),
    )


def _audit_ink_budget_evidence(
    evidence: Any,
    diagnostics_evidence: Any,
    pre_budget_strokes: tuple[PlotStroke, ...],
    selected_strokes: tuple[PlotStroke, ...],
    layout: Layout,
    *,
    pre_budget_input_supplied: bool,
) -> _InkBudgetEvidenceAudit:
    if evidence is None:
        return _InkBudgetEvidenceAudit(
            by_source_input={},
            supplied=False,
            ledger_valid=False,
            supplied_count=0,
            valid_count=0,
            evidenced_source_refs=(),
            invalid_entries=(),
            input_count=None,
            retained_input_count=0,
            omitted_input_count=0,
            omitted_group_count=0,
            omitted_layer_counts=(),
            uncullable_source_group_count=None,
            diagnostics_supplied=diagnostics_evidence is not None,
            diagnostics_valid=False,
            invalid_diagnostics=(("diagnostics", "ledger_not_supplied"),),
        )
    ledger = evidence.as_dict() if hasattr(evidence, "as_dict") else evidence
    if not isinstance(ledger, dict):
        return _InkBudgetEvidenceAudit(
            by_source_input={},
            supplied=True,
            ledger_valid=False,
            supplied_count=0,
            valid_count=0,
            evidenced_source_refs=(),
            invalid_entries=(("ledger", "ledger_is_not_an_object"),),
            input_count=None,
            retained_input_count=0,
            omitted_input_count=0,
            omitted_group_count=0,
            omitted_layer_counts=(),
            uncullable_source_group_count=None,
            diagnostics_supplied=diagnostics_evidence is not None,
            diagnostics_valid=False,
            invalid_diagnostics=(("diagnostics", "ledger_is_not_an_object"),),
        )

    invalid: list[tuple[str, str]] = []
    if set(ledger) != _INK_LEDGER_FIELDS:
        invalid.append(("ledger", "invalid_ledger_structure"))
    if ledger.get("schema_version") != _INK_BUDGET_SCHEMA_VERSION:
        invalid.append(("ledger", "unsupported_schema_version"))
    if ledger.get("policy") != _INK_BALANCED_POLICY:
        invalid.append(("ledger", "unsupported_policy"))
    if ledger.get("reason") != "ink_budget_gate":
        invalid.append(("ledger", "unsupported_gate_reason"))
    entries_value = ledger.get("entries")
    if not isinstance(entries_value, list):
        invalid.append(("ledger", "entries_are_not_a_list"))
        entries_value = []
    entries = [value for value in entries_value if isinstance(value, dict)]
    if len(entries) != len(entries_value):
        invalid.append(("ledger", "entry_is_not_an_object"))
    atomic_groups, atomic_group_error = _ink_budget_atomic_group_facts(
        pre_budget_strokes
    )
    atomic_groups_by_id = {group.group_id: group for group in atomic_groups}
    if atomic_group_error is not None:
        invalid.append(("ledger", atomic_group_error))

    seen_ids: set[str] = set()
    seen_group_ids: set[str] = set()
    valid_entries: list[dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        fallback_id = f"supplied-entry-{position}"
        omission_id = entry.get("omission_id")
        if not isinstance(omission_id, str) or not omission_id:
            invalid.append((fallback_id, "missing_omission_id"))
            continue
        if omission_id in seen_ids:
            invalid.append((omission_id, "duplicate_omission_id"))
            continue
        seen_ids.add(omission_id)
        group_id = entry.get("group_id")
        if isinstance(group_id, str) and group_id in seen_group_ids:
            invalid.append((omission_id, "duplicate_group_id"))
            continue
        if isinstance(group_id, str):
            seen_group_ids.add(group_id)
        error = _ink_budget_entry_error(
            entry,
            pre_budget_strokes,
            layout,
            atomic_groups_by_id,
        )
        if error is not None:
            invalid.append((omission_id, error))
            continue
        valid_entries.append(entry)

    input_count_value = ledger.get("input_count")
    input_count = (
        input_count_value
        if isinstance(input_count_value, int)
        and not isinstance(input_count_value, bool)
        else None
    )
    if input_count is None or input_count < 0:
        invalid.append(("ledger", "invalid_input_count"))
    elif input_count != len(pre_budget_strokes):
        invalid.append(("ledger", "input_count_mismatch"))
    if entries and not pre_budget_input_supplied:
        invalid.append(("ledger", "pre_budget_input_not_supplied"))

    retained_indexes = _canonical_integer_list(ledger.get("retained_input_indexes"))
    omitted_indexes = _canonical_integer_list(ledger.get("omitted_input_indexes"))
    if retained_indexes is None:
        invalid.append(("ledger", "invalid_retained_input_indexes"))
        retained_indexes = []
    if omitted_indexes is None:
        invalid.append(("ledger", "invalid_omitted_input_indexes"))
        omitted_indexes = []
    entry_omitted_indexes = sorted(
        {
            index
            for entry in valid_entries
            for index in entry.get("input_indexes", [])
            if isinstance(index, int) and not isinstance(index, bool)
        }
    )
    if omitted_indexes != entry_omitted_indexes:
        invalid.append(("ledger", "omitted_input_index_union_mismatch"))
    if set(retained_indexes) & set(omitted_indexes):
        invalid.append(("ledger", "retained_and_omitted_indexes_overlap"))
    if input_count is not None and sorted(retained_indexes + omitted_indexes) != list(
        range(input_count)
    ):
        invalid.append(("ledger", "selection_partition_is_incomplete"))
    if any(index >= len(pre_budget_strokes) for index in retained_indexes):
        invalid.append(("ledger", "retained_input_index_out_of_range"))
    else:
        expected_selected = Counter(
            _stroke_partition_identity(pre_budget_strokes[index])
            for index in retained_indexes
        )
        actual_selected = Counter(
            _stroke_partition_identity(stroke) for stroke in selected_strokes
        )
        if expected_selected != actual_selected:
            invalid.append(("ledger", "retained_stroke_partition_mismatch"))
        elif [
            _stroke_partition_identity(pre_budget_strokes[index])
            for index in retained_indexes
        ] != [_stroke_partition_identity(stroke) for stroke in selected_strokes]:
            invalid.append(("ledger", "retained_stroke_order_mismatch"))
        retained_topology_nodes = {
            node
            for index in retained_indexes
            for node in (
                pre_budget_strokes[index].tags.get("topology:start-node", ""),
                pre_budget_strokes[index].tags.get("topology:end-node", ""),
            )
            if node
        }
        for entry in valid_entries:
            group_topology_nodes = {
                node
                for index in entry["input_indexes"]
                for node in (
                    pre_budget_strokes[index].tags.get("topology:start-node", ""),
                    pre_budget_strokes[index].tags.get("topology:end-node", ""),
                )
                if node
            }
            final_shared_count = len(group_topology_nodes & retained_topology_nodes)
            claimed_shared_count = entry["priority"]["shared_topology_node_count"]
            if claimed_shared_count > final_shared_count:
                invalid.append(
                    (
                        str(entry["omission_id"]),
                        "priority_topology_connection_not_in_retained_partition",
                    )
                )

    count_fields = {
        "entry_count": len(entries),
        "omitted_group_count": len(entries),
        "omitted_input_stroke_count": len(omitted_indexes),
    }
    for field, expected_value in count_fields.items():
        value = ledger.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value != expected_value
        ):
            invalid.append(("ledger", f"{field}_mismatch"))

    uncullable_value = ledger.get("uncullable_source_group_count")
    uncullable_source_group_count = (
        uncullable_value
        if isinstance(uncullable_value, int)
        and not isinstance(uncullable_value, bool)
        and uncullable_value >= 0
        else None
    )
    if uncullable_source_group_count is None:
        invalid.append(("ledger", "invalid_uncullable_source_group_count"))
    elif uncullable_source_group_count != 0:
        invalid.append(("ledger", "uncullable_source_groups_present"))

    expected_omitted_source_refs = sorted(
        {
            source_ref
            for entry in valid_entries
            for input_stroke in entry.get("input_strokes", [])
            if isinstance(input_stroke, dict)
            for source_ref in input_stroke.get("source_refs", [])
            if isinstance(source_ref, str)
        }
    )
    if ledger.get("omitted_source_refs") != expected_omitted_source_refs:
        invalid.append(("ledger", "omitted_source_ref_union_mismatch"))
    expected_omitted_canonical_refs = sorted(
        {
            source_ref
            for entry in valid_entries
            for source_ref in entry.get("canonical_source_refs", [])
            if isinstance(source_ref, str)
        }
    )
    if ledger.get("omitted_canonical_source_refs") != expected_omitted_canonical_refs:
        invalid.append(("ledger", "omitted_canonical_source_ref_union_mismatch"))
    if valid_entries:
        consideration_orders = [
            entry["priority"]["consideration_order"] for entry in valid_entries
        ]
        if consideration_orders != sorted(set(consideration_orders)):
            invalid.append(("ledger", "omission_consideration_order_mismatch"))
        expected_ids = [
            f"ink-budget-{position}" for position in range(1, len(valid_entries) + 1)
        ]
        if [entry["omission_id"] for entry in valid_entries] != expected_ids:
            invalid.append(("ledger", "noncanonical_omission_id_order"))
        budgets = {entry["cutoff"]["budget_ink_mm2"] for entry in valid_entries}
        fixed_amounts = {entry["cutoff"]["fixed_ink_mm2"] for entry in valid_entries}
        if len(budgets) != 1:
            invalid.append(("ledger", "inconsistent_budget_cutoffs"))
        if len(fixed_amounts) != 1:
            invalid.append(("ledger", "inconsistent_fixed_ink_cutoffs"))
    expected_partition_sha = (
        _ink_budget_partition_sha256(
            input_count=input_count,
            retained_input_indexes=retained_indexes,
            entries=valid_entries,
        )
        if input_count is not None
        else None
    )
    if ledger.get("selection_partition_sha256") != expected_partition_sha:
        invalid.append(("ledger", "selection_partition_fingerprint_mismatch"))

    ledger_valid = not invalid
    diagnostics_audit = _ink_budget_diagnostics_audit(
        diagnostics_evidence,
        pre_budget_strokes=pre_budget_strokes,
        retained_indexes=retained_indexes,
        omitted_indexes=omitted_indexes,
        valid_entries=valid_entries,
        selection_partition_sha256=expected_partition_sha,
        ledger_valid=ledger_valid,
        groups=atomic_groups,
        group_error=atomic_group_error,
    )
    indexed: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    evidenced_refs: set[str] = set()
    if ledger_valid:
        for entry in valid_entries:
            omission_id = str(entry["omission_id"])
            layer = str(entry["layer"])
            for input_stroke in entry["input_strokes"]:
                index = int(input_stroke["index"])
                for source_ref in input_stroke["source_refs"]:
                    indexed[(layer, source_ref)].append((index, omission_id))
                    evidenced_refs.add(source_ref)
    return _InkBudgetEvidenceAudit(
        by_source_input={key: tuple(value) for key, value in sorted(indexed.items())},
        supplied=True,
        ledger_valid=ledger_valid,
        supplied_count=len(entries_value),
        valid_count=len(valid_entries),
        evidenced_source_refs=tuple(sorted(evidenced_refs)),
        invalid_entries=tuple(invalid),
        input_count=input_count,
        retained_input_count=len(retained_indexes),
        omitted_input_count=len(omitted_indexes),
        omitted_group_count=len(valid_entries),
        omitted_layer_counts=tuple(
            sorted(Counter(str(entry["layer"]) for entry in valid_entries).items())
        ),
        uncullable_source_group_count=uncullable_source_group_count,
        diagnostics_supplied=diagnostics_audit.supplied,
        diagnostics_valid=diagnostics_audit.valid,
        invalid_diagnostics=diagnostics_audit.invalid,
    )


def _canonical_source_ref(
    osm_type: str, osm_id: str, part: str
) -> tuple[str, str, str, str] | None:
    normalized = (osm_type.strip(), osm_id.strip(), part.strip())
    if (
        not all(normalized)
        or normalized[0] == "compiled"
        or normalized[1] == "multiple"
    ):
        return None
    return (*normalized, "/".join(normalized))


def _parse_source_ref(reference: str) -> tuple[str, str, str, str] | None:
    parts = reference.strip().split("/", maxsplit=2)
    if len(parts) != 3:
        return None
    return _canonical_source_ref(parts[0], parts[1], parts[2])


def _geometry_signature(
    points: Iterable[tuple[float, float]],
) -> GeometrySignature:
    forward = tuple((round(first, 7), round(second, 7)) for first, second in points)
    reverse = tuple(reversed(forward))
    return min(forward, reverse)


def _raw_latlon_parts(geometry: Any) -> Iterable[list[tuple[float, float]]]:
    if not isinstance(geometry, list):
        return
    current: list[tuple[float, float]] = []
    for value in geometry:
        coordinate: tuple[float, float] | None = None
        if isinstance(value, dict) and "lat" in value and "lon" in value:
            try:
                candidate = (float(value["lat"]), float(value["lon"]))
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None and all(isfinite(item) for item in candidate):
                coordinate = candidate
        if coordinate is not None:
            current.append(coordinate)
            continue
        if len(current) >= 2:
            yield current
        current = []
    if len(current) >= 2:
        yield current


def _raw_endpoint(point: tuple[float, float]) -> tuple[float, float]:
    return round(point[0], 7), round(point[1], 7)


def _assemble_raw_member_parts(
    parts: Iterable[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    unused = [list(part) for part in parts if len(part) >= 2]
    assembled: list[list[tuple[float, float]]] = []
    while unused:
        current = unused.pop(0)
        changed = True
        while changed and _raw_endpoint(current[0]) != _raw_endpoint(current[-1]):
            changed = False
            for index, candidate in enumerate(unused):
                current_start = _raw_endpoint(current[0])
                current_end = _raw_endpoint(current[-1])
                candidate_start = _raw_endpoint(candidate[0])
                candidate_end = _raw_endpoint(candidate[-1])
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


def _compose_raw_area_role(parent_role: str, child_role: str) -> str:
    return "inner" if (parent_role == "inner") != (child_role == "inner") else "outer"


def _collect_raw_relation_geometry(
    relation: dict[str, Any],
    *,
    layer: str,
    relation_index: dict[str, dict[str, Any]],
    ancestry: frozenset[str],
    inherited_area_role: str,
    traversal: tuple[str, ...],
    area_parts: dict[str, list[list[tuple[float, float]]]],
    geometry_records: list[tuple[str, list[tuple[float, float]]]],
) -> None:
    """Independently reconstruct raw relation geometry for lineage auditing."""

    members = relation.get("members")
    if not isinstance(members, list):
        return
    for member_index, member in enumerate(members):
        if not isinstance(member, dict):
            continue
        member_type = member.get("type")
        raw_role = str(member.get("role", ""))
        role = raw_role or ("outer" if layer in AREA_LAYERS else "member")
        member_ref = str(member.get("ref", "unknown"))
        member_traversal = (*traversal, str(member_index))
        if member_type == "way":
            parts = list(_raw_latlon_parts(member.get("geometry")))
            if layer in AREA_LAYERS and raw_role in {"", "outer", "inner"}:
                effective_role = _compose_raw_area_role(
                    inherited_area_role,
                    "inner" if role == "inner" else "outer",
                )
                area_parts[effective_role].extend(parts)
            else:
                path = "/".join(member_traversal)
                geometry_records.extend(
                    (
                        f"{role}:{path}:way-{member_ref}:{part_index}",
                        points,
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
            nested_role = _compose_raw_area_role(
                inherited_area_role,
                "inner" if role == "inner" else "outer",
            )
        _collect_raw_relation_geometry(
            nested,
            layer=layer,
            relation_index=relation_index,
            ancestry=ancestry | {member_ref},
            inherited_area_role=nested_role,
            traversal=(*traversal, f"{member_index}:relation-{member_ref}"),
            area_parts=area_parts,
            geometry_records=geometry_records,
        )


def _raw_visible_geometry(
    element: dict[str, Any],
    *,
    osm_type: str,
    osm_id: str,
    layer: str | None,
    layout: Layout,
    relation_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, GeometrySignature]:
    if layer is None:
        return {}
    frame = box(
        layout.bbox.west,
        layout.bbox.south,
        layout.bbox.east,
        layout.bbox.north,
    )
    geometry_records: list[tuple[str, list[tuple[float, float]]]] = []
    if osm_type == "way":
        geometry_records.extend(
            (f"way:{part_index}", points)
            for part_index, points in enumerate(
                _raw_latlon_parts(element.get("geometry"))
            )
        )
    elif osm_type == "relation":
        members = element.get("members")
        if not isinstance(members, list):
            return {}
        area_parts: dict[str, list[list[tuple[float, float]]]] = defaultdict(list)
        resolved_relations = relation_index or {}
        _collect_raw_relation_geometry(
            element,
            layer=layer,
            relation_index=resolved_relations,
            ancestry=frozenset({osm_id}),
            inherited_area_role="outer",
            traversal=(),
            area_parts=area_parts,
            geometry_records=geometry_records,
        )
        if layer in AREA_LAYERS:
            geometry_records.extend(
                (f"outer:ring-{index}", points)
                for index, points in enumerate(
                    _assemble_raw_member_parts(area_parts["outer"])
                )
            )
            geometry_records.extend(
                (f"inner:ring-{index}", points)
                for index, points in enumerate(
                    _assemble_raw_member_parts(area_parts["inner"])
                )
            )
    else:
        return {}

    result: dict[str, GeometrySignature] = {}
    for part, points in geometry_records:
        line = LineString([(longitude, latitude) for latitude, longitude in points])
        if line.length <= 1e-12 or line.intersection(frame).length <= 1e-12:
            continue
        parsed = _canonical_source_ref(osm_type, osm_id, part)
        if parsed is not None:
            result[parsed[3]] = _geometry_signature(points)
    return result


def _feature_geometry_index(
    features: Iterable[MapFeature],
    audited_layers: set[str] | frozenset[str] = AUDITED_GEOMETRY_LAYERS,
) -> FeatureGeometryIndex:
    result: FeatureGeometryIndex = {}
    for feature in features:
        if feature.layer not in audited_layers:
            continue
        parsed = _canonical_source_ref(feature.osm_type, feature.osm_id, feature.part)
        if parsed is None:
            continue
        osm_type, osm_id, _part, serialized = parsed
        by_reference = result.setdefault((osm_type, osm_id, feature.layer), {})
        by_reference.setdefault(serialized, set()).add(
            _geometry_signature(feature.points)
        )
    return result


def _valid_osm_identifier(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        return value.isdigit() and int(value) > 0
    return False


def _raw_identified_heritage_site_is_polygon(element: dict[str, Any]) -> bool:
    """Mirror the geometry gate for bare castle/palace site footprints."""

    osm_type = element.get("type")
    if osm_type == "relation":
        return (
            _tags(element).get("type", "").strip().casefold() == "multipolygon"
        )
    if osm_type != "way":
        return False
    parts = list(_raw_latlon_parts(element.get("geometry")))
    if len(parts) != 1:
        return False
    points = parts[0]
    if (
        len(points) < 4
        or tuple(round(value, 7) for value in points[0])
        != tuple(round(value, 7) for value in points[-1])
    ):
        return False
    return abs(
        sum(
            left[1] * right[0] - right[1] * left[0]
            for left, right in zip(points, points[1:])
        )
        / 2
    ) > 1e-12


def audit_raw_geometry_integrity(
    data: dict[str, Any],
    *,
    layout: Layout,
    enabled_layers: set[str],
    features: Iterable[MapFeature],
    source_query: str | None = None,
    evidence_kind: str | None = None,
) -> RawGeometryIntegrityAudit:
    """Validate raw inline geometry for every selected drawable feature family.

    Coordinate-list corruption is checked at the way/member boundary: separate
    relation members are legitimate fragments, while a null or malformed item
    *inside* one member's geometry is not.  For intact source objects, every
    visible assembled raw part must also have an exact, orientation-insensitive
    counterpart in the canonical feature stage.
    """

    elements_value = data.get("elements")
    resolved_kind = evidence_kind or (
        "overpass_json_with_query"
        if source_query is not None
        else "saved_overpass_json_without_query"
    )
    if not isinstance(elements_value, list):
        scope = (
            "PBF is audited through canonical-feature hashes and downstream "
            "lineage; raw Overpass inline geometry is unavailable."
            if resolved_kind.startswith("pbf")
            else "No Overpass-shaped elements list was available for raw geometry audit."
        )
        return RawGeometryIntegrityAudit(
            findings=(),
            source_available=False,
            evidence_kind=resolved_kind,
            evidence_scope=scope,
            source_query_available=source_query is not None,
        )

    findings: list[RawGeometryIntegrityFinding] = []
    selected_counts: Counter[str] = Counter()
    checked_objects = 0
    checked_parts = 0
    checked_canonical_parts = 0
    ignored_members = 0
    extracted_geometry = _feature_geometry_index(features, enabled_layers)
    duplicate_groups: dict[
        tuple[str, str], list[tuple[int, dict[str, Any], str, str]]
    ] = defaultdict(list)
    relation_occurrences: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(
        list
    )
    for relation_element_index, relation_value in enumerate(elements_value):
        if (
            isinstance(relation_value, dict)
            and relation_value.get("type") == "relation"
            and _valid_osm_identifier(relation_value.get("id"))
        ):
            relation_occurrences[str(relation_value["id"])].append(
                (relation_element_index, relation_value)
            )
    relation_index = {
        relation_id: occurrences[0][1]
        for relation_id, occurrences in relation_occurrences.items()
    }

    def add_finding(
        *,
        reason: str,
        layer: str,
        source_ref: str,
        element_index: int,
        osm_type: str,
        osm_id: str,
        component: str,
        member_index: int | None = None,
        member_type: str | None = None,
        member_ref: str | None = None,
        member_role: str | None = None,
    ) -> None:
        findings.append(
            RawGeometryIntegrityFinding(
                reason=reason,
                layer=layer,
                source_ref=source_ref,
                element_index=element_index,
                osm_type=osm_type,
                osm_id=osm_id,
                component=component,
                member_index=member_index,
                member_type=member_type,
                member_ref=member_ref,
                member_role=member_role,
            )
        )

    for element_index, value in enumerate(elements_value):
        if not isinstance(value, dict):
            continue
        tags = _tags(value)
        if is_identified_heritage_site(
            tags
        ) and not _raw_identified_heritage_site_is_polygon(value):
            # These source objects are deliberately outside extraction rather
            # than canonical geometry losses: the heritage-site exception is
            # area-only and must never admit an open historic line or point.
            continue
        layer = classify_supported_layer(tags)
        if layer is None or layer not in enabled_layers:
            continue

        checked_objects += 1
        selected_counts[layer] += 1
        type_value = value.get("type")
        id_value = value.get("id")
        osm_type = str(type_value) if type_value is not None else "missing"
        osm_id = str(id_value) if id_value is not None else "missing"
        identity_valid = osm_type in {"way", "relation"} and _valid_osm_identifier(
            id_value
        )
        source_ref = (
            f"{osm_type}/{osm_id}" if identity_valid else f"elements[{element_index}]"
        )
        failure_count_before = len(findings)

        if osm_type not in {"way", "relation"}:
            add_finding(
                reason="unsupported_or_missing_osm_type",
                layer=layer,
                source_ref=source_ref,
                element_index=element_index,
                osm_type=osm_type,
                osm_id=osm_id,
                component="type",
            )
            continue
        if not _valid_osm_identifier(id_value):
            add_finding(
                reason="missing_or_invalid_osm_id",
                layer=layer,
                source_ref=source_ref,
                element_index=element_index,
                osm_type=osm_type,
                osm_id=osm_id,
                component="id",
            )
        else:
            duplicate_groups[(osm_type, osm_id)].append(
                (element_index, value, layer, source_ref)
            )

        if osm_type == "way":
            checked_parts += 1
            failure = _geometry_value_failure(value.get("geometry"))
            if failure is not None:
                add_finding(
                    reason=failure,
                    layer=layer,
                    source_ref=source_ref,
                    element_index=element_index,
                    osm_type=osm_type,
                    osm_id=osm_id,
                    component="geometry",
                )
        else:

            def validate_relation_members(
                relation: dict[str, Any],
                *,
                ancestry: frozenset[str],
                component_prefix: str,
            ) -> int:
                """Return the number of recursively drawable leaf way members."""

                nonlocal checked_parts, ignored_members
                assert layer is not None
                members = relation.get("members")
                if not isinstance(members, list) or not members:
                    add_finding(
                        reason="missing_or_invalid_relation_members",
                        layer=layer,
                        source_ref=source_ref,
                        element_index=element_index,
                        osm_type=osm_type,
                        osm_id=osm_id,
                        component=f"{component_prefix}members",
                    )
                    return 0

                leaf_way_count = 0
                for member_index, member in enumerate(members):
                    member_component = f"{component_prefix}members[{member_index}]"
                    if not isinstance(member, dict):
                        add_finding(
                            reason="malformed_relation_member",
                            layer=layer,
                            source_ref=source_ref,
                            element_index=element_index,
                            osm_type=osm_type,
                            osm_id=osm_id,
                            component=member_component,
                            member_index=member_index,
                        )
                        continue
                    member_type_value = member.get("type")
                    member_type = (
                        str(member_type_value)
                        if member_type_value is not None
                        else "missing"
                    )
                    member_ref_value = member.get("ref")
                    member_ref = (
                        str(member_ref_value)
                        if member_ref_value is not None
                        else "missing"
                    )
                    raw_role = str(member.get("role", ""))
                    member_role = raw_role or (
                        "outer" if layer in AREA_LAYERS else "member"
                    )
                    geometry_component = f"{member_component}.geometry"

                    if member_type == "way":
                        leaf_way_count += 1
                        checked_parts += 1
                        if not _valid_osm_identifier(member_ref_value):
                            add_finding(
                                reason="missing_or_invalid_relation_member_ref",
                                layer=layer,
                                source_ref=source_ref,
                                element_index=element_index,
                                osm_type=osm_type,
                                osm_id=osm_id,
                                component=f"{member_component}.ref",
                                member_index=member_index,
                                member_type=member_type,
                                member_ref=member_ref,
                                member_role=member_role,
                            )
                        failure = _geometry_value_failure(member.get("geometry"))
                        if failure is not None:
                            add_finding(
                                reason=failure,
                                layer=layer,
                                source_ref=source_ref,
                                element_index=element_index,
                                osm_type=osm_type,
                                osm_id=osm_id,
                                component=geometry_component,
                                member_index=member_index,
                                member_type=member_type,
                                member_ref=member_ref,
                                member_role=member_role,
                            )
                        continue

                    if member_type == "node":
                        if raw_role in {"", "outer", "inner"}:
                            add_finding(
                                reason="non_way_member_in_geometry_role",
                                layer=layer,
                                source_ref=source_ref,
                                element_index=element_index,
                                osm_type=osm_type,
                                osm_id=osm_id,
                                component=geometry_component,
                                member_index=member_index,
                                member_type=member_type,
                                member_ref=member_ref,
                                member_role=member_role,
                            )
                        else:
                            ignored_members += 1
                        continue

                    if member_type == "relation":
                        if raw_role not in {"", "outer", "inner"}:
                            # Roles such as subarea are semantic links, not ring parts.
                            ignored_members += 1
                            continue
                        if not _valid_osm_identifier(member_ref_value):
                            add_finding(
                                reason="missing_or_invalid_relation_member_ref",
                                layer=layer,
                                source_ref=source_ref,
                                element_index=element_index,
                                osm_type=osm_type,
                                osm_id=osm_id,
                                component=f"{member_component}.ref",
                                member_index=member_index,
                                member_type=member_type,
                                member_ref=member_ref,
                                member_role=member_role,
                            )
                            continue
                        if member_ref in ancestry:
                            add_finding(
                                reason="cyclic_nested_relation_geometry",
                                layer=layer,
                                source_ref=source_ref,
                                element_index=element_index,
                                osm_type=osm_type,
                                osm_id=osm_id,
                                component=geometry_component,
                                member_index=member_index,
                                member_type=member_type,
                                member_ref=member_ref,
                                member_role=member_role,
                            )
                            continue
                        occurrences = relation_occurrences.get(member_ref, [])
                        if not occurrences:
                            add_finding(
                                reason="missing_nested_relation_reference",
                                layer=layer,
                                source_ref=source_ref,
                                element_index=element_index,
                                osm_type=osm_type,
                                osm_id=osm_id,
                                component=geometry_component,
                                member_index=member_index,
                                member_type=member_type,
                                member_ref=member_ref,
                                member_role=member_role,
                            )
                            continue
                        nested_variants = {
                            json.dumps(
                                nested.get("members"),
                                sort_keys=True,
                                separators=(",", ":"),
                                default=str,
                            )
                            for _nested_index, nested in occurrences
                        }
                        if len(nested_variants) > 1:
                            add_finding(
                                reason="ambiguous_nested_relation_reference",
                                layer=layer,
                                source_ref=source_ref,
                                element_index=element_index,
                                osm_type=osm_type,
                                osm_id=osm_id,
                                component=geometry_component,
                                member_index=member_index,
                                member_type=member_type,
                                member_ref=member_ref,
                                member_role=member_role,
                            )
                            continue
                        nested = occurrences[0][1]
                        leaf_way_count += validate_relation_members(
                            nested,
                            ancestry=ancestry | {member_ref},
                            component_prefix=(
                                f"{member_component}->relation/{member_ref}."
                            ),
                        )
                        continue

                    add_finding(
                        reason="malformed_relation_member",
                        layer=layer,
                        source_ref=source_ref,
                        element_index=element_index,
                        osm_type=osm_type,
                        osm_id=osm_id,
                        component=f"{member_component}.type",
                        member_index=member_index,
                        member_type=member_type,
                        member_ref=member_ref,
                        member_role=member_role,
                    )
                return leaf_way_count

            way_member_count = validate_relation_members(
                value,
                ancestry=frozenset({osm_id}),
                component_prefix="",
            )
            if not way_member_count:
                add_finding(
                    reason="relation_has_no_drawable_way_members",
                    layer=layer,
                    source_ref=source_ref,
                    element_index=element_index,
                    osm_type=osm_type,
                    osm_id=osm_id,
                    component="members",
                )

        if len(findings) != failure_count_before or not identity_valid:
            continue
        requirements = _raw_visible_geometry(
            value,
            osm_type=osm_type,
            osm_id=osm_id,
            layer=layer,
            layout=layout,
            relation_index=relation_index,
        )
        canonical = extracted_geometry.get((osm_type, osm_id, layer), {})
        for reference, signature in requirements.items():
            checked_canonical_parts += 1
            if signature in canonical.get(reference, set()):
                continue
            add_finding(
                reason="raw_to_canonical_geometry_loss",
                layer=layer,
                source_ref=source_ref,
                element_index=element_index,
                osm_type=osm_type,
                osm_id=osm_id,
                component=reference,
            )

    for (osm_type, osm_id), occurrences in duplicate_groups.items():
        if len(occurrences) < 2:
            continue
        tag_variants = {
            json.dumps(_tags(element), sort_keys=True, separators=(",", ":"))
            for _index, element, _layer, _source_ref in occurrences
        }
        source_variants = {
            json.dumps(
                {
                    "geometry": element.get("geometry"),
                    "members": element.get("members"),
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            for _index, element, _layer, _source_ref in occurrences
        }
        reason = (
            "conflicting_duplicate_tags"
            if len(tag_variants) > 1
            else "conflicting_duplicate_source_geometry"
            if len(source_variants) > 1
            else None
        )
        if reason is None:
            continue
        element_index, _element, layer, source_ref = min(
            occurrences, key=lambda occurrence: occurrence[0]
        )
        add_finding(
            reason=reason,
            layer=layer,
            source_ref=source_ref,
            element_index=element_index,
            osm_type=osm_type,
            osm_id=osm_id,
            component="duplicate_occurrences",
        )

    scope = (
        "Selected drawable objects present in the supplied Overpass JSON were "
        "checked for intact inline geometry and visible raw-to-canonical "
        "preservation. This does not prove that the acquisition query returned "
        "every relevant OpenStreetMap object."
    )
    return RawGeometryIntegrityAudit(
        findings=tuple(findings),
        source_available=True,
        evidence_kind=resolved_kind,
        evidence_scope=scope,
        source_query_available=source_query is not None,
        checked_object_count=checked_objects,
        checked_geometry_part_count=checked_parts,
        raw_to_canonical_checked_part_count=checked_canonical_parts,
        ignored_non_geometry_member_count=ignored_members,
        selected_by_layer=tuple(sorted(selected_counts.items())),
    )


def _add_source_part(
    result: SourcePartIndex,
    osm_type: str,
    osm_id: str,
    layer: str,
    part: str,
) -> None:
    parsed = _canonical_source_ref(osm_type, osm_id, part)
    if parsed is None or layer not in AUDITED_GEOMETRY_LAYERS:
        return
    parsed_type, parsed_id, _parsed_part, serialized = parsed
    result.setdefault((parsed_type, parsed_id, layer), set()).add(serialized)


def _source_parts_from_features(
    features: Iterable[MapFeature],
    layout: Layout,
    *,
    visible_only: bool = False,
) -> SourcePartIndex:
    result: SourcePartIndex = {}
    for feature in features:
        if feature.layer not in AUDITED_GEOMETRY_LAYERS:
            continue
        if visible_only and not _feature_has_page_geometry(feature, layout):
            continue
        _add_source_part(
            result,
            feature.osm_type,
            feature.osm_id,
            feature.layer,
            feature.part,
        )
    return result


def _points_form_nonzero_line(points: Iterable[tuple[float, float]]) -> bool:
    resolved = list(points)
    if len(resolved) < 2:
        return False
    try:
        return bool(LineString(resolved).length > 1e-9)
    except (TypeError, ValueError):
        return False


def _source_parts_from_strokes(strokes: Iterable[PlotStroke]) -> SourcePartIndex:
    result: SourcePartIndex = {}
    for stroke in strokes:
        if stroke.layer not in AUDITED_GEOMETRY_LAYERS or not _points_form_nonzero_line(
            stroke.points
        ):
            continue
        for reference in stroke.tags.get("source-refs", "").split(";"):
            parsed = _parse_source_ref(reference)
            if parsed is None:
                continue
            osm_type, osm_id, part, _serialized = parsed
            _add_source_part(result, osm_type, osm_id, stroke.layer, part)
    return result


def _svg_local_name(element: ET.Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", maxsplit=1)[-1]


def _element_is_hidden(element: ET.Element) -> bool:
    if element.get("display", "").strip().casefold() == "none":
        return True
    if element.get("visibility", "").strip().casefold() in {"hidden", "collapse"}:
        return True
    style = element.get("style", "")
    return bool(
        re.search(
            r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|opacity\s*:\s*0(?:\.0*)?)\s*(?:;|$)",
            style,
            flags=re.IGNORECASE,
        )
    ) or element.get("opacity", "").strip() in {"0", "0.0", "0.00"}


def _svg_path_is_drawable(element: ET.Element) -> bool:
    if _svg_local_name(element) != "path":
        return False
    data = element.get("d", "").strip()
    if not data or _SVG_RENDER_PATH_RE.fullmatch(data) is None:
        return False
    numbers = [float(value) for value in _SVG_NUMBER_RE.findall(data)]
    if not numbers or not all(isfinite(value) for value in numbers):
        return False
    points = list(zip(numbers[0::2], numbers[1::2], strict=True))
    return len(set(points)) >= 2


def _svg_source_parts(
    svg: Path | ET.Element | ET.ElementTree | None,
) -> SourcePartIndex | None:
    if svg is None:
        return None
    if isinstance(svg, Path):
        root = ET.parse(svg).getroot()
    elif isinstance(svg, ET.ElementTree):
        resolved_root = svg.getroot()
        if resolved_root is None:
            return {}
        root = resolved_root
    else:
        root = svg
    result: SourcePartIndex = {}

    def visit(
        element: ET.Element,
        inherited_layer: str | None = None,
        inherited_hidden: bool = False,
        inherited_non_rendering: bool = False,
    ) -> None:
        local_name = _svg_local_name(element)
        current_layer = inherited_layer
        if local_name == "g":
            element_id = element.get("id", "")
            if element_id.startswith("layer-"):
                current_layer = element_id.removeprefix("layer-")
        hidden = inherited_hidden or _element_is_hidden(element)
        non_rendering = (
            inherited_non_rendering or local_name in _NON_RENDERING_SVG_CONTAINERS
        )
        if (
            not hidden
            and not non_rendering
            and current_layer in AUDITED_GEOMETRY_LAYERS
            and _svg_path_is_drawable(element)
        ):
            for reference in element.get("data-osm-source-refs", "").split(";"):
                parsed = _parse_source_ref(reference)
                if parsed is None:
                    continue
                osm_type, osm_id, part, _serialized = parsed
                _add_source_part(result, osm_type, osm_id, current_layer, part)
        for child in element:
            visit(child, current_layer, hidden, non_rendering)

    visit(root)
    return result


def _has_source_parts(
    index: SourcePartIndex | None,
    key: tuple[str, str],
    expected_layer: str | None,
    required_refs: set[str] | None = None,
) -> bool | None:
    if index is None:
        return None
    if expected_layer is None:
        return False
    observed = index.get((key[0], key[1], expected_layer), set())
    if required_refs:
        return required_refs <= observed
    return bool(observed)


def _has_lifecycle_marker(tags: dict[str, str]) -> bool:
    for key in LIFECYCLE_KEYS:
        for candidate in (key, f"{key}:highway", f"highway:{key}"):
            value = tags.get(candidate)
            if value is not None and value.strip().casefold() not in FALSE_VALUES:
                return True
    return False


def _feature_has_page_geometry(feature: MapFeature, layout: Layout) -> bool:
    clip = box(*layout.clip_rect)
    points: list[tuple[float, float]] = []
    for latitude, longitude in feature.points:
        x, y = layout.project_to_page(latitude, longitude)
        point = (round(x, 4), round(y, 4))
        if not points or point != points[-1]:
            points.append(point)
    if len(points) < 2:
        return False
    line = LineString(points)
    return bool(line.length > 1e-6 and line.intersection(clip).length > 1e-6)


def _has_page_geometry(
    features: Iterable[MapFeature],
    osm_type: str,
    osm_id: str,
    expected_layer: str | None,
    layout: Layout,
) -> bool:
    for feature in features:
        if (
            feature.osm_type != osm_type
            or feature.osm_id != osm_id
            or feature.layer != expected_layer
        ):
            continue
        if _feature_has_page_geometry(feature, layout):
            return True
    return False


def _base_exclusion_reason(
    *,
    osm_type: str,
    highway: str,
    tags: dict[str, str],
    geometry_status: str,
    in_frame: bool,
) -> str | None:
    if osm_type != "way":
        return "unsupported_osm_type"
    normalized = highway.strip().casefold()
    if normalized in FUTURE_HIGHWAY_VALUES:
        return "construction_or_proposed"
    if normalized in INACTIVE_HIGHWAY_VALUES:
        return "inactive_lifecycle"
    if normalized in NON_ROUTE_HIGHWAY_VALUES:
        return "known_non_route_highway"
    if normalized in PLACEHOLDER_HIGHWAY_VALUES:
        return "placeholder_highway_value"
    if geometry_status in SOURCE_GEOMETRY_FAILURES:
        return geometry_status
    if not in_frame:
        return "outside_frame"
    return None


def _road_area_exclusion_reason(
    *, osm_type: str, geometry_status: str, in_frame: bool
) -> str | None:
    if osm_type not in {"way", "relation"}:
        return "unsupported_osm_type"
    if geometry_status in SOURCE_GEOMETRY_FAILURES:
        return geometry_status
    if not in_frame:
        return "outside_frame"
    return None


def _is_retained_unknown_highway(highway: str) -> bool:
    normalized = highway.strip().casefold()
    return bool(
        normalized
        and normalized != "construction"
        and normalized not in AUDITED_HIGHWAY_LAYERS
        and normalized not in FUTURE_HIGHWAY_VALUES
        and normalized not in INACTIVE_HIGHWAY_VALUES
        and normalized not in NON_ROUTE_HIGHWAY_VALUES
        and normalized not in PLACEHOLDER_HIGHWAY_VALUES
    )


def _audited_highway_layer(tags: dict[str, str]) -> str | None:
    highway = tags.get("highway", "").strip().casefold()
    if highway == "construction":
        target = tags.get("construction", "").strip().casefold()
        # Construction geometry is physically present.  An absent or newly added
        # target stays visible in the low-priority road layer instead of vanishing.
        return AUDITED_HIGHWAY_LAYERS.get(target, "roads_other")
    documented_layer = AUDITED_HIGHWAY_LAYERS.get(highway)
    if documented_layer is not None:
        return documented_layer
    if _is_retained_unknown_highway(highway):
        return "roads_other"
    return None


def _strip_overpass_comments(query: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    return re.sub(r"//[^\r\n]*", "", without_blocks)


def _query_has_geometry_output(query: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[;\n)])\s*out\s+geom(?:\s+qt)?\s*;",
            query,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    )


def _query_selector_covers_layout(
    query: str,
    *,
    object_type: str,
    tag_key: str,
    layout: Layout,
) -> bool:
    pattern = re.compile(
        rf'\b{re.escape(object_type)}\s*\[\s*"{re.escape(tag_key)}"\s*\]'
        rf"\s*\(\s*({_OVERPASS_NUMBER})\s*,\s*({_OVERPASS_NUMBER})\s*,"
        rf"\s*({_OVERPASS_NUMBER})\s*,\s*({_OVERPASS_NUMBER})\s*\)\s*;",
        flags=re.IGNORECASE,
    )
    tolerance = 1e-7
    for match in pattern.finditer(query):
        south, west, north, east = (float(value) for value in match.groups())
        if (
            south <= layout.bbox.south + tolerance
            and west <= layout.bbox.west + tolerance
            and north >= layout.bbox.north - tolerance
            and east >= layout.bbox.east - tolerance
        ):
            return True
    return False


def _query_scope_flags(
    source_query: str | None, layout: Layout
) -> tuple[bool | None, bool | None, bool | None]:
    if source_query is None:
        return None, None, None
    query = _strip_overpass_comments(source_query)
    geometry_output = _query_has_geometry_output(query)
    highway_way = geometry_output and _query_selector_covers_layout(
        query, object_type="way", tag_key="highway", layout=layout
    )
    area_way = geometry_output and _query_selector_covers_layout(
        query, object_type="way", tag_key="area:highway", layout=layout
    )
    area_relation = geometry_output and _query_selector_covers_layout(
        query, object_type="relation", tag_key="area:highway", layout=layout
    )
    road_area = area_way and area_relation
    return highway_way, road_area, highway_way and road_area


def audit_highway_completeness(
    data: dict[str, Any],
    *,
    layout: Layout,
    enabled_layers: set[str],
    features: Iterable[MapFeature],
    cartographic_strokes: Iterable[PlotStroke],
    physical_strokes: Iterable[PlotStroke],
    physical_omission_evidence: Iterable[Any] = (),
    pre_budget_cartographic_strokes: Iterable[PlotStroke] | None = None,
    ink_budget_omission_evidence: Any = None,
    ink_budget_diagnostics_evidence: Any = None,
    svg: Path | ET.Element | ET.ElementTree | None = None,
    detail_profile: str = "faithful",
    source_query: str | None = None,
) -> HighwayCompletenessAudit:
    """Trace supplied highway centrelines and road areas through every stage.

    This audits only objects present in ``data``.  A narrow Overpass allow-list
    cannot prove that other highway values were absent from OpenStreetMap; use
    a broad local PBF or an extract containing ``way[highway]`` plus
    way/relation ``area:highway`` for an acquisition-completeness audit.
    """

    resolved_cartographic_strokes = tuple(cartographic_strokes)
    resolved_physical_strokes = tuple(physical_strokes)
    pre_budget_input_supplied = pre_budget_cartographic_strokes is not None
    resolved_pre_budget_strokes = (
        tuple(pre_budget_cartographic_strokes)
        if pre_budget_cartographic_strokes is not None
        else resolved_cartographic_strokes
    )
    physical_evidence_audit = _audit_physical_minimum_evidence(
        physical_omission_evidence,
        resolved_cartographic_strokes,
    )
    ink_budget_evidence_audit = _audit_ink_budget_evidence(
        ink_budget_omission_evidence,
        ink_budget_diagnostics_evidence,
        resolved_pre_budget_strokes,
        resolved_cartographic_strokes,
        layout,
        pre_budget_input_supplied=pre_budget_input_supplied,
    )
    pre_budget_input_indexes: dict[tuple[str, str], set[int]] = defaultdict(set)
    for stroke_index, stroke in enumerate(resolved_pre_budget_strokes):
        for source_ref in stroke.tags.get("source-refs", "").split(";"):
            if source_ref:
                pre_budget_input_indexes[(stroke.layer, source_ref)].add(stroke_index)
    cartographic_input_indexes: dict[tuple[str, str], set[int]] = defaultdict(set)
    for stroke_index, stroke in enumerate(resolved_cartographic_strokes):
        for source_ref in stroke.tags.get("source-refs", "").split(";"):
            if source_ref:
                cartographic_input_indexes[(stroke.layer, source_ref)].add(stroke_index)
    elements_value = data.get("elements")
    if not isinstance(elements_value, list):
        return HighwayCompletenessAudit(
            records=(),
            source_available=False,
            final_stage="svg" if svg is not None else "physical",
            source_scope_warning=(
                "No Overpass-shaped elements list was supplied; raw highway "
                "completeness was not audited."
            ),
            acquisition_scope_complete=None,
            highway_way_scope_complete=None,
            road_area_scope_complete=None,
            physical_minimum_evidence_supplied_count=(
                physical_evidence_audit.supplied_count
            ),
            physical_minimum_evidence_valid_count=(physical_evidence_audit.valid_count),
            physical_minimum_evidenced_source_refs=(
                physical_evidence_audit.evidenced_source_refs
            ),
            invalid_physical_minimum_evidence=(physical_evidence_audit.invalid_entries),
            detail_profile=detail_profile,
            ink_budget_evidence_supplied=ink_budget_evidence_audit.supplied,
            ink_budget_evidence_ledger_valid=(ink_budget_evidence_audit.ledger_valid),
            ink_budget_evidence_supplied_count=(
                ink_budget_evidence_audit.supplied_count
            ),
            ink_budget_evidence_valid_count=ink_budget_evidence_audit.valid_count,
            ink_budget_evidenced_source_refs=(
                ink_budget_evidence_audit.evidenced_source_refs
            ),
            invalid_ink_budget_evidence=(ink_budget_evidence_audit.invalid_entries),
            ink_budget_input_count=ink_budget_evidence_audit.input_count,
            ink_budget_retained_input_count=(
                ink_budget_evidence_audit.retained_input_count
            ),
            ink_budget_omitted_input_count=(
                ink_budget_evidence_audit.omitted_input_count
            ),
            ink_budget_omitted_group_count=(
                ink_budget_evidence_audit.omitted_group_count
            ),
            ink_budget_omitted_layer_counts=(
                ink_budget_evidence_audit.omitted_layer_counts
            ),
            ink_budget_uncullable_source_group_count=(
                ink_budget_evidence_audit.uncullable_source_group_count
            ),
            ink_budget_diagnostics_supplied=(
                ink_budget_evidence_audit.diagnostics_supplied
            ),
            ink_budget_diagnostics_valid=(ink_budget_evidence_audit.diagnostics_valid),
            invalid_ink_budget_diagnostics=(
                ink_budget_evidence_audit.invalid_diagnostics
            ),
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    occurrence_indexes: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, value in enumerate(elements_value):
        if not isinstance(value, dict):
            continue
        tags = _tags(value)
        area_highway = tags.get("area:highway", "").strip().casefold()
        if "highway" not in tags and area_highway in FALSE_VALUES:
            continue
        key = (str(value.get("type", "unknown")), str(value.get("id", "unknown")))
        grouped[key].append(value)
        occurrence_indexes[key].append(index)

    resolved_features = tuple(features)
    extracted_parts = _source_parts_from_features(resolved_features, layout)
    visible_feature_parts = _source_parts_from_features(
        resolved_features, layout, visible_only=True
    )
    extracted_geometry = _feature_geometry_index(resolved_features)
    pre_budget_parts = _source_parts_from_strokes(resolved_pre_budget_strokes)
    cartographic_parts = _source_parts_from_strokes(resolved_cartographic_strokes)
    physical_parts = _source_parts_from_strokes(resolved_physical_strokes)
    svg_parts = _svg_source_parts(svg)
    records: list[HighwayCompletenessRecord] = []
    for (osm_type, osm_id), raw_elements in sorted(grouped.items()):
        serialized_elements = [
            json.dumps(element, sort_keys=True, separators=(",", ":"), default=str)
            for element in raw_elements
        ]
        representative_index = min(
            range(len(raw_elements)), key=lambda index: serialized_elements[index]
        )
        tags = _tags(raw_elements[representative_index])
        tag_variants = {
            json.dumps(_tags(element), sort_keys=True, separators=(",", ":"))
            for element in raw_elements
        }
        conflicting_duplicate_tags = len(tag_variants) > 1
        conflicting_duplicate_source = (
            not conflicting_duplicate_tags and len(set(serialized_elements)) > 1
        )
        has_highway_tag = "highway" in tags
        highway = tags.get("highway", "").strip().casefold()
        area_highway_value = tags.get("area:highway", "").strip().casefold()
        geometry_status, in_frame = _raw_geometry_status(raw_elements, layout)
        centreline_layer = _audited_highway_layer(tags) if has_highway_tag else None
        if centreline_layer is not None:
            semantic_kind = "centreline"
            expected_layer = centreline_layer
            reason = _base_exclusion_reason(
                osm_type=osm_type,
                highway=highway,
                tags=tags,
                geometry_status=geometry_status,
                in_frame=in_frame,
            )
        elif area_highway_value not in FALSE_VALUES:
            # A standalone area:highway object is pavement-surface
            # micromapping, not a routable centreline. If both tags classify as
            # a live highway, the centreline above deliberately wins, matching
            # extraction and avoiding two records for the same raw object.
            semantic_kind = "road_area"
            expected_layer = "road_areas"
            reason = _road_area_exclusion_reason(
                osm_type=osm_type,
                geometry_status=geometry_status,
                in_frame=in_frame,
            )
        else:
            semantic_kind = "highway_exclusion"
            expected_layer = None
            reason = _base_exclusion_reason(
                osm_type=osm_type,
                highway=highway,
                tags=tags,
                geometry_status=geometry_status,
                in_frame=in_frame,
            )
        if reason in SOURCE_GEOMETRY_FAILURES and expected_layer not in enabled_layers:
            reason = "style_layer_disabled"
        elif reason is None and expected_layer not in enabled_layers:
            reason = "style_layer_disabled"
        if conflicting_duplicate_tags:
            reason = "conflicting_duplicate_tags"
        elif conflicting_duplicate_source:
            reason = "conflicting_duplicate_source"
        expected = (
            reason is None
            or reason in SOURCE_GEOMETRY_FAILURES
            or bool(conflicting_duplicate_tags or conflicting_duplicate_source)
        )
        key = (osm_type, osm_id)
        source_index_key = (
            (osm_type, osm_id, expected_layer) if expected_layer is not None else None
        )
        extracted_refs = (
            extracted_parts.get(source_index_key, set())
            if source_index_key is not None
            else set()
        )
        visible_feature_refs = (
            visible_feature_parts.get(source_index_key, set())
            if source_index_key is not None
            else set()
        )
        raw_geometry_requirements = _raw_visible_geometry(
            raw_elements[representative_index],
            osm_type=osm_type,
            osm_id=osm_id,
            layer=expected_layer,
            layout=layout,
        )
        extracted_geometry_for_object = (
            extracted_geometry.get(source_index_key, {})
            if source_index_key is not None
            else {}
        )
        extracted = bool(extracted_refs) and all(
            signature in extracted_geometry_for_object.get(reference, set())
            for reference, signature in raw_geometry_requirements.items()
        )
        visible_required_refs = set(raw_geometry_requirements) or visible_feature_refs
        cartographic = bool(
            _has_source_parts(
                cartographic_parts,
                key,
                expected_layer,
                visible_required_refs or None,
            )
        )
        pre_budget_cartographic = bool(
            _has_source_parts(
                pre_budget_parts,
                key,
                expected_layer,
                visible_required_refs or None,
            )
        )
        physical = bool(
            _has_source_parts(
                physical_parts,
                key,
                expected_layer,
                visible_required_refs or None,
            )
        )
        cartographic_required_refs = visible_required_refs or (
            cartographic_parts.get(source_index_key, set())
            if source_index_key is not None
            else set()
        )
        observed_physical_refs = (
            physical_parts.get(source_index_key, set())
            if source_index_key is not None
            else set()
        )
        missing_physical_refs = cartographic_required_refs - observed_physical_refs
        pre_budget_required_refs = visible_required_refs or (
            pre_budget_parts.get(source_index_key, set())
            if source_index_key is not None
            else set()
        )
        observed_cartographic_refs = (
            cartographic_parts.get(source_index_key, set())
            if source_index_key is not None
            else set()
        )
        missing_cartographic_refs = (
            pre_budget_required_refs - observed_cartographic_refs
        )
        ink_budget_evidence_groups: list[tuple[str, ...]] = []
        if expected_layer is not None:
            for reference in sorted(missing_cartographic_refs):
                required_indexes = pre_budget_input_indexes.get(
                    (expected_layer, reference), set()
                )
                claims = ink_budget_evidence_audit.by_source_input.get(
                    (expected_layer, reference), ()
                )
                ids_by_input = {
                    input_index: {
                        omission_id
                        for claimed_index, omission_id in claims
                        if claimed_index == input_index
                    }
                    for input_index in required_indexes
                }
                if required_indexes and all(ids_by_input.values()):
                    ink_budget_evidence_groups.append(
                        tuple(
                            sorted(
                                {
                                    omission_id
                                    for ids in ids_by_input.values()
                                    for omission_id in ids
                                }
                            )
                        )
                    )
                else:
                    ink_budget_evidence_groups.append(())
        ink_budget_evidenced = bool(
            pre_budget_cartographic
            and missing_cartographic_refs
            and len(ink_budget_evidence_groups) == len(missing_cartographic_refs)
            and all(ink_budget_evidence_groups)
            and ink_budget_evidence_audit.ledger_valid
        )
        ink_budget_evidence_ids = tuple(
            sorted({item for group in ink_budget_evidence_groups for item in group})
        )
        evidence_groups: list[tuple[str, ...]] = []
        if expected_layer is not None:
            for reference in sorted(missing_physical_refs):
                required_indexes = cartographic_input_indexes.get(
                    (expected_layer, reference), set()
                )
                physical_claims = physical_evidence_audit.by_source_input.get(
                    (expected_layer, reference), ()
                )
                if detail_profile in {"plotter-faithful", "ink-balanced"} and (
                    expected_layer in ROAD_LAYERS
                ):
                    physical_claims = tuple(
                        claim
                        for claim in physical_claims
                        if claim[2] == "residual_network_trail_minimum_gate"
                    )
                ids_by_input = {
                    input_index: {
                        omission_id
                        for claimed_index, omission_id, _branch in physical_claims
                        if claimed_index == input_index
                    }
                    for input_index in required_indexes
                }
                if required_indexes and all(ids_by_input.values()):
                    evidence_groups.append(
                        tuple(
                            sorted(
                                {
                                    omission_id
                                    for ids in ids_by_input.values()
                                    for omission_id in ids
                                }
                            )
                        )
                    )
                else:
                    evidence_groups.append(())
        physical_minimum_evidenced = (
            bool(missing_physical_refs)
            and len(evidence_groups) == len(missing_physical_refs)
            and all(evidence_groups)
        )
        physical_minimum_evidence_ids = tuple(
            sorted({item for group in evidence_groups for item in group})
        )
        emitted_svg = _has_source_parts(
            svg_parts,
            key,
            expected_layer,
            visible_required_refs or None,
        )

        immutable_source_failure = reason in SOURCE_GEOMETRY_FAILURES or reason in {
            "conflicting_duplicate_tags",
            "conflicting_duplicate_source",
        }
        if expected and not immutable_source_failure and not extracted:
            reason = "unexpected_extraction_drop"
        elif expected and not immutable_source_failure and not cartographic:
            if not _has_page_geometry(
                resolved_features, osm_type, osm_id, expected_layer, layout
            ):
                reason = "projection_or_clipping_collapse"
            elif detail_profile == "plot" and semantic_kind == "road_area":
                reason = "road_area_plot_policy"
            elif detail_profile == "plot" and tags.get("access") in {"private", "no"}:
                reason = "access_restricted_by_plot_profile"
            elif detail_profile == "plot" and highway == "service":
                reason = "service_plot_policy"
            elif detail_profile == "plot" and expected_layer == "paths":
                reason = "path_plot_policy"
            elif detail_profile == "plot":
                reason = "cartographic_plot_policy"
            elif detail_profile == "ink-balanced" and ink_budget_evidenced:
                reason = "ink_budget_gate"
            else:
                reason = "unexpected_cartographic_drop"
        elif expected and not immutable_source_failure and not physical:
            reason = (
                "physical_minimum_gate"
                if detail_profile in {"plot", "plotter-faithful", "ink-balanced"}
                and physical_minimum_evidenced
                else "unexpected_physical_drop"
            )
        elif (
            expected
            and not immutable_source_failure
            and svg_parts is not None
            and not emitted_svg
        ):
            reason = "svg_lineage_or_serialization_drop"

        stage_chain_complete = bool(
            extracted
            and cartographic
            and physical
            and (svg_parts is None or emitted_svg)
        )
        status = (
            "emitted"
            if expected and reason is None and stage_chain_complete
            else "missing"
            if expected
            else "excluded"
        )
        notes: list[str] = []
        if len(raw_elements) > 1:
            notes.append("duplicate_raw_element")
        if conflicting_duplicate_tags:
            notes.append("conflicting_duplicate_tags")
        elif conflicting_duplicate_source:
            notes.append("conflicting_duplicate_source")
        if _has_lifecycle_marker(tags):
            notes.append("lifecycle_tag_present")
        if tags.get("access") in {"private", "no"}:
            notes.append("restricted_access")
        if tags.get("area", "").strip().casefold() == "yes":
            notes.append("area_perimeter")
        if semantic_kind == "road_area":
            notes.append("non_routable_area_highway_perimeter")
        elif area_highway_value not in FALSE_VALUES:
            notes.append("area_highway_cotag_uses_centreline_semantics")
        if _is_retained_unknown_highway(highway):
            notes.append("unknown_highway_value_retained_for_review")
        if physical_minimum_evidence_ids:
            notes.append(
                "physical_minimum_evidence_ids="
                + ",".join(physical_minimum_evidence_ids)
            )
        if reason == "ink_budget_gate" and ink_budget_evidence_ids:
            notes.append("ink_budget_evidence_ids=" + ",".join(ink_budget_evidence_ids))
        notes.append(
            "raw_occurrence_indexes="
            + ",".join(str(index) for index in occurrence_indexes[(osm_type, osm_id)])
        )
        records.append(
            HighwayCompletenessRecord(
                osm_type=osm_type,
                osm_id=osm_id,
                source_ref=f"{osm_type}/{osm_id}",
                semantic_kind=semantic_kind,
                has_highway_tag=has_highway_tag,
                highway=highway,
                area_highway=(area_highway_value or None),
                name=tags.get("name") or tags.get("ref"),
                raw_occurrences=len(raw_elements),
                in_frame=in_frame,
                geometry_status=geometry_status,
                expected_layer=expected_layer,
                expected=expected,
                extracted=extracted,
                cartographic=cartographic,
                physical=physical,
                svg=emitted_svg,
                status=status,
                reason=None if status == "emitted" else reason,
                notes=tuple(notes),
                physical_minimum_evidence_ids=(
                    physical_minimum_evidence_ids
                    if reason == "physical_minimum_gate"
                    else ()
                ),
                ink_budget_evidence_ids=(
                    ink_budget_evidence_ids if reason == "ink_budget_gate" else ()
                ),
            )
        )

    (
        broad_highway_query,
        broad_road_area_query,
        acquisition_scope_complete,
    ) = _query_scope_flags(source_query, layout)
    if acquisition_scope_complete:
        source_scope_warning = (
            "Within the acquisition extent, the source query requested every "
            "highway-tagged way and every area:highway-tagged way/relation; "
            "unknown and excluded values are summarized below."
        )
    elif acquisition_scope_complete is False:
        source_scope_warning = (
            "The supplied query did not request every highway=* way and every "
            "area:highway=* way/relation. Pipeline lineage is audited, but "
            "acquisition omissions remain possible."
        )
    else:
        source_scope_warning = (
            "The audit covers highway-tagged objects present in the supplied JSON "
            "only. Its original query is unavailable, so acquisition completeness "
            "cannot be certified."
        )
    return HighwayCompletenessAudit(
        records=tuple(records),
        source_available=True,
        final_stage="svg" if svg_parts is not None else "physical",
        source_scope_warning=source_scope_warning,
        acquisition_scope_complete=acquisition_scope_complete,
        highway_way_scope_complete=broad_highway_query,
        road_area_scope_complete=broad_road_area_query,
        physical_minimum_evidence_supplied_count=(
            physical_evidence_audit.supplied_count
        ),
        physical_minimum_evidence_valid_count=physical_evidence_audit.valid_count,
        physical_minimum_evidenced_source_refs=(
            physical_evidence_audit.evidenced_source_refs
        ),
        invalid_physical_minimum_evidence=(physical_evidence_audit.invalid_entries),
        detail_profile=detail_profile,
        ink_budget_evidence_supplied=ink_budget_evidence_audit.supplied,
        ink_budget_evidence_ledger_valid=(ink_budget_evidence_audit.ledger_valid),
        ink_budget_evidence_supplied_count=(ink_budget_evidence_audit.supplied_count),
        ink_budget_evidence_valid_count=ink_budget_evidence_audit.valid_count,
        ink_budget_evidenced_source_refs=(
            ink_budget_evidence_audit.evidenced_source_refs
        ),
        invalid_ink_budget_evidence=ink_budget_evidence_audit.invalid_entries,
        ink_budget_input_count=ink_budget_evidence_audit.input_count,
        ink_budget_retained_input_count=(
            ink_budget_evidence_audit.retained_input_count
        ),
        ink_budget_omitted_input_count=(ink_budget_evidence_audit.omitted_input_count),
        ink_budget_omitted_group_count=(ink_budget_evidence_audit.omitted_group_count),
        ink_budget_omitted_layer_counts=(
            ink_budget_evidence_audit.omitted_layer_counts
        ),
        ink_budget_uncullable_source_group_count=(
            ink_budget_evidence_audit.uncullable_source_group_count
        ),
        ink_budget_diagnostics_supplied=(
            ink_budget_evidence_audit.diagnostics_supplied
        ),
        ink_budget_diagnostics_valid=(ink_budget_evidence_audit.diagnostics_valid),
        invalid_ink_budget_diagnostics=(ink_budget_evidence_audit.invalid_diagnostics),
    )
