"""Truth-preserving route ingestion and processing for sports artwork.

This module deliberately stops before page composition.  It accepts supplied
route vertices, removes only inspectable duplicate/spike noise, measures the
retained source geometry, and provides physical simplification and quantitative
downsampling helpers.  It never performs map matching and never substitutes a
measured track length for an event's authoritative distance.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree as ET

from .course import parse_kml
from .models import MapPlotterError
from .route_chainage import geodesic_distance_m


MAX_ROUTE_FILE_BYTES = 40 * 1024 * 1024
MAX_ROUTE_POINTS = 500_000
MAX_ROUTE_SEGMENTS = 10_000

SUPPORTED_POINT_CHANNELS = frozenset(
    {
        "pace",
        "speed",
        "heart_rate",
        "cadence",
        "power",
        "stroke_rate",
        "throttle",
        "brake",
        "gear",
        "position",
        "temperature",
        "wind_speed",
        "heading",
    }
)

DEFAULT_MAX_SPEED_M_S = {
    "run": 18.0,
    "running": 18.0,
    "walk": 8.0,
    "hike": 12.0,
    "cycling": 55.0,
    "bike": 55.0,
    "swim": 5.0,
    "rowing": 15.0,
    "sailing": 45.0,
    "motorsport": 140.0,
    "drive": 140.0,
}


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise MapPlotterError(f"{label} must be a finite number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(f"{label} must be a finite number.") from exc
    if not math.isfinite(result):
        raise MapPlotterError(f"{label} must be a finite number.")
    return result


def _optional_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MapPlotterError(f"{label} must be a number or null.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(f"{label} must be a number or null.") from exc
    # Missing sensor/elevation samples commonly arrive as NaN from FIT/TCX
    # conversion.  Treat them as absent evidence; never draw them as zero.
    return result if math.isfinite(result) else None


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MapPlotterError(f"{label} must be non-empty text.")
    return value.strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, name: str) -> str | None:
    for child in node:
        if _local_name(child.tag) == name:
            return child.text
    return None


def _descendant_text(node: ET.Element, names: set[str]) -> str | None:
    for child in node.iter():
        if _local_name(child.tag) in names and child.text:
            return child.text
    return None


def _timestamp_seconds(value: str | None, label: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MapPlotterError(f"{label} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class SportsPoint:
    """One supplied route vertex and its optional observed channels."""

    x: float
    y: float
    elevation_m: float | None = None
    distance_m: float | None = None
    timestamp: str | None = None
    channels: Mapping[str, float | None] = field(default_factory=dict)
    source_index: int = 0
    preserve: bool = False

    @property
    def coordinates(self) -> tuple[float, float]:
        return (self.x, self.y)

    def channel(self, name: str) -> float | None:
        if name == "elevation":
            return self.elevation_m
        return self.channels.get(name)


@dataclass(frozen=True, slots=True)
class SportsSegment:
    """One source segment; source breaks are never bridged automatically."""

    id: str
    discipline: str
    source_ref: str
    points: tuple[SportsPoint, ...]
    lap: int | None = None
    role: str = "course"
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RouteFilteringConfig:
    """Configurable geometric noise policy recorded in every manifest."""

    enabled: bool = True
    duplicate_tolerance_m: float = 0.05
    spike_detour_ratio: float = 8.0
    spike_minimum_leg_m: float = 30.0
    spike_median_leg_multiplier: float = 8.0
    maximum_speed_m_s: float = 55.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "duplicate_tolerance_m": self.duplicate_tolerance_m,
            "spike_detour_ratio": self.spike_detour_ratio,
            "spike_minimum_leg_m": self.spike_minimum_leg_m,
            "spike_median_leg_multiplier": self.spike_median_leg_multiplier,
            "maximum_speed_m_s": self.maximum_speed_m_s,
        }


@dataclass(frozen=True, slots=True)
class RemovedRoutePoint:
    segment_id: str
    source_index: int
    reason: str
    evidence: Mapping[str, float | int | str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "source_index": self.source_index,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class SegmentProcessingReport:
    segment_id: str
    input_point_count: int
    retained_point_count: int
    duplicate_count: int
    spike_count: int
    measured_length_m: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "input_point_count": self.input_point_count,
            "retained_point_count": self.retained_point_count,
            "duplicate_count": self.duplicate_count,
            "spike_count": self.spike_count,
            "measured_length_m": round(self.measured_length_m, 3),
        }


@dataclass(frozen=True, slots=True)
class RouteProcessingResult:
    coordinate_space: str
    scale_m_per_unit: float
    segments: tuple[SportsSegment, ...]
    filtering: RouteFilteringConfig
    removed_points: tuple[RemovedRoutePoint, ...]
    reports: tuple[SegmentProcessingReport, ...]
    measured_length_m: float
    map_matching: Mapping[str, Any]

    @property
    def point_count(self) -> int:
        return sum(len(segment.points) for segment in self.segments)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": "inspectable-route-filter-v1",
            "coordinate_space": self.coordinate_space,
            "scale_m_per_unit": self.scale_m_per_unit,
            "input_point_count": sum(
                report.input_point_count for report in self.reports
            ),
            "retained_point_count": self.point_count,
            "measured_length_m": round(self.measured_length_m, 3),
            "filtering": self.filtering.as_dict(),
            "removed_points": [point.as_dict() for point in self.removed_points],
            "segments": [report.as_dict() for report in self.reports],
            "map_matching": dict(self.map_matching),
        }


@dataclass(frozen=True, slots=True)
class QuantitativeSample:
    distance_m: float
    value: float
    source_index: int


@dataclass(frozen=True, slots=True)
class PhysicalSimplification:
    points: tuple[tuple[float, float], ...]
    retained_indices: tuple[int, ...]
    protected_indices: tuple[int, ...]
    tolerance_mm: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": "physical-rdp-protected-route-identity-v1",
            "input_point_count": max(self.retained_indices, default=-1) + 1,
            "rendered_point_count": len(self.points),
            "protected_vertex_count": len(self.protected_indices),
            "tolerance_mm": self.tolerance_mm,
        }


def filtering_config(
    route: Mapping[str, Any], disciplines: Iterable[str]
) -> RouteFilteringConfig:
    raw = route.get("filtering") or {}
    if not isinstance(raw, dict):
        raise MapPlotterError("route.filtering must be an object.")
    discipline_list = [str(value).casefold() for value in disciplines]
    default_speed = max(
        (DEFAULT_MAX_SPEED_M_S.get(value, 55.0) for value in discipline_list),
        default=55.0,
    )

    def positive(name: str, default: float) -> float:
        value = _finite_number(raw.get(name, default), f"route.filtering.{name}")
        if value <= 0.0:
            raise MapPlotterError(f"route.filtering.{name} must be positive.")
        return value

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MapPlotterError("route.filtering.enabled must be true or false.")
    return RouteFilteringConfig(
        enabled=enabled,
        duplicate_tolerance_m=positive("duplicate_tolerance_m", 0.05),
        spike_detour_ratio=positive("spike_detour_ratio", 8.0),
        spike_minimum_leg_m=positive("spike_minimum_leg_m", 30.0),
        spike_median_leg_multiplier=positive(
            "spike_median_leg_multiplier", 8.0
        ),
        maximum_speed_m_s=positive("maximum_speed_m_s", default_speed),
    )


def _normalise_point(
    value: Any,
    *,
    coordinate_space: str,
    segment_id: str,
    index: int,
) -> SportsPoint:
    label = f"route segment {segment_id!r} point {index}"
    channels: dict[str, float | None] = {}
    elevation: float | None = None
    distance: float | None = None
    timestamp: str | None = None
    preserve = False
    if isinstance(value, dict):
        coordinates = value.get("coordinates", value.get("point"))
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
            raise MapPlotterError(f"{label} needs a two-value coordinates array.")
        x = _finite_number(coordinates[0], f"{label} x")
        y = _finite_number(coordinates[1], f"{label} y")
        coordinate_elevation = coordinates[2] if len(coordinates) >= 3 else None
        elevation = _optional_number(
            value.get("elevation_m", coordinate_elevation), f"{label} elevation_m"
        )
        distance = _optional_number(value.get("distance_m"), f"{label} distance_m")
        raw_timestamp = value.get("timestamp")
        if raw_timestamp is not None:
            timestamp = _text(raw_timestamp, f"{label} timestamp")
            _timestamp_seconds(timestamp, f"{label} timestamp")
        preserve_value = value.get("preserve", False)
        if not isinstance(preserve_value, bool):
            raise MapPlotterError(f"{label}.preserve must be true or false.")
        preserve = preserve_value
        raw_channels = value.get("channels") or {}
        if not isinstance(raw_channels, dict):
            raise MapPlotterError(f"{label}.channels must be an object.")
        for name, raw_channel in raw_channels.items():
            channel_name = _text(name, f"{label} channel name").casefold()
            channels[channel_name] = _optional_number(
                raw_channel, f"{label} channel {channel_name!r}"
            )
        for channel_name in SUPPORTED_POINT_CHANNELS:
            if channel_name in value:
                channels[channel_name] = _optional_number(
                    value[channel_name], f"{label}.{channel_name}"
                )
    elif isinstance(value, (list, tuple)) and len(value) in {2, 3}:
        x = _finite_number(value[0], f"{label} x")
        y = _finite_number(value[1], f"{label} y")
        elevation = _optional_number(
            value[2] if len(value) == 3 else None, f"{label} elevation_m"
        )
    else:
        raise MapPlotterError(
            f"{label} must be [x, y, optional elevation] or a point object."
        )
    if coordinate_space == "wgs84":
        if not -180.0 <= x <= 180.0 or not -90.0 <= y <= 90.0:
            raise MapPlotterError(f"{label} is outside WGS84 coordinate bounds.")
    if distance is not None and distance < 0.0:
        raise MapPlotterError(f"{label} distance_m cannot be negative.")
    return SportsPoint(
        x=x,
        y=y,
        elevation_m=elevation,
        distance_m=distance,
        timestamp=timestamp,
        channels=channels,
        source_index=index,
        preserve=preserve,
    )


def _distance_between(
    first: SportsPoint,
    second: SportsPoint,
    *,
    coordinate_space: str,
    scale_m_per_unit: float,
) -> float:
    if coordinate_space == "wgs84":
        return geodesic_distance_m(first.coordinates, second.coordinates)
    return math.hypot(second.x - first.x, second.y - first.y) * scale_m_per_unit


def _merge_duplicate(first: SportsPoint, second: SportsPoint) -> SportsPoint:
    channels = dict(first.channels)
    channels.update(
        {name: value for name, value in second.channels.items() if value is not None}
    )
    return replace(
        first,
        elevation_m=(
            second.elevation_m
            if second.elevation_m is not None
            else first.elevation_m
        ),
        distance_m=(
            second.distance_m if second.distance_m is not None else first.distance_m
        ),
        timestamp=second.timestamp or first.timestamp,
        channels=channels,
        preserve=first.preserve or second.preserve,
    )


def _deduplicate(
    points: Sequence[SportsPoint],
    *,
    segment_id: str,
    coordinate_space: str,
    scale_m_per_unit: float,
    tolerance_m: float,
) -> tuple[list[SportsPoint], list[RemovedRoutePoint]]:
    retained: list[SportsPoint] = []
    removed: list[RemovedRoutePoint] = []
    for point in points:
        if retained:
            separation = _distance_between(
                retained[-1],
                point,
                coordinate_space=coordinate_space,
                scale_m_per_unit=scale_m_per_unit,
            )
            if separation <= tolerance_m + 1e-9:
                retained[-1] = _merge_duplicate(retained[-1], point)
                removed.append(
                    RemovedRoutePoint(
                        segment_id=segment_id,
                        source_index=point.source_index,
                        reason="consecutive-duplicate",
                        evidence={"separation_m": round(separation, 6)},
                    )
                )
                continue
        retained.append(point)
    return retained, removed


def _edge_speed(
    first: SportsPoint,
    second: SportsPoint,
    distance_m: float,
    *,
    label: str,
) -> float | None:
    first_time = _timestamp_seconds(first.timestamp, f"{label} first timestamp")
    second_time = _timestamp_seconds(second.timestamp, f"{label} second timestamp")
    if first_time is None or second_time is None:
        return None
    elapsed = second_time - first_time
    if elapsed <= 0.0:
        return None
    return distance_m / elapsed


def _remove_spikes(
    points: Sequence[SportsPoint],
    *,
    segment_id: str,
    coordinate_space: str,
    scale_m_per_unit: float,
    config: RouteFilteringConfig,
) -> tuple[list[SportsPoint], list[RemovedRoutePoint]]:
    # Three-point out-and-back tracks are meaningful geometry, not enough
    # evidence for an isolated spike decision.
    if len(points) < 4 or not config.enabled:
        return list(points), []
    edge_lengths = [
        _distance_between(
            first,
            second,
            coordinate_space=coordinate_space,
            scale_m_per_unit=scale_m_per_unit,
        )
        for first, second in zip(points, points[1:])
    ]
    nonzero = [length for length in edge_lengths if length > 1e-6]
    typical = median(nonzero) if nonzero else 0.0
    minimum_leg = max(
        config.spike_minimum_leg_m,
        typical * config.spike_median_leg_multiplier,
    )
    retained = [points[0]]
    removed: list[RemovedRoutePoint] = []
    for index in range(1, len(points) - 1):
        before = retained[-1]
        candidate = points[index]
        after = points[index + 1]
        if candidate.preserve:
            retained.append(candidate)
            continue
        first_leg = _distance_between(
            before,
            candidate,
            coordinate_space=coordinate_space,
            scale_m_per_unit=scale_m_per_unit,
        )
        second_leg = _distance_between(
            candidate,
            after,
            coordinate_space=coordinate_space,
            scale_m_per_unit=scale_m_per_unit,
        )
        shortcut = _distance_between(
            before,
            after,
            coordinate_space=coordinate_space,
            scale_m_per_unit=scale_m_per_unit,
        )
        detour_ratio = (first_leg + second_leg) / max(shortcut, 0.01)
        first_speed = _edge_speed(
            before, candidate, first_leg, label=f"{segment_id} point {index}"
        )
        second_speed = _edge_speed(
            candidate, after, second_leg, label=f"{segment_id} point {index}"
        )
        speed_evidence = max(
            (speed for speed in (first_speed, second_speed) if speed is not None),
            default=None,
        )
        impossible_speed = (
            speed_evidence is not None
            and speed_evidence > config.maximum_speed_m_s
        )
        geometric_evidence = (
            first_leg >= minimum_leg
            and second_leg >= minimum_leg
            and detour_ratio >= config.spike_detour_ratio
        )
        # With timestamps, demand both geometric and impossible-speed evidence.
        # Without timestamps, the much stronger median-leg gate protects real
        # switchbacks and sparse out-and-back recordings.
        is_spike = geometric_evidence and (
            impossible_speed or speed_evidence is None
        )
        if is_spike:
            evidence: dict[str, float | int | str] = {
                "first_leg_m": round(first_leg, 3),
                "second_leg_m": round(second_leg, 3),
                "shortcut_m": round(shortcut, 3),
                "detour_ratio": round(detour_ratio, 3),
                "median_leg_m": round(typical, 3),
                "minimum_leg_gate_m": round(minimum_leg, 3),
            }
            if speed_evidence is not None:
                evidence["maximum_observed_edge_speed_m_s"] = round(
                    speed_evidence, 3
                )
                evidence["maximum_allowed_speed_m_s"] = config.maximum_speed_m_s
            removed.append(
                RemovedRoutePoint(
                    segment_id=segment_id,
                    source_index=candidate.source_index,
                    reason="isolated-gps-spike",
                    evidence=evidence,
                )
            )
            continue
        retained.append(candidate)
    retained.append(points[-1])
    return retained, removed


def process_route(
    route: Mapping[str, Any],
    *,
    source_ids: Iterable[str],
) -> RouteProcessingResult:
    """Validate and clean supplied route segments without joining source gaps."""

    if not isinstance(route, Mapping):
        raise MapPlotterError("route must be an object.")
    coordinate_space = str(route.get("coordinate_space", "wgs84")).casefold()
    if coordinate_space not in {"wgs84", "local"}:
        raise MapPlotterError("route.coordinate_space must be 'wgs84' or 'local'.")
    scale_m_per_unit = _finite_number(
        route.get("scale_m_per_unit", 1.0), "route.scale_m_per_unit"
    )
    if scale_m_per_unit <= 0.0:
        raise MapPlotterError("route.scale_m_per_unit must be positive.")
    raw_segments = route.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise MapPlotterError("route.segments must be a non-empty array.")
    if len(raw_segments) > MAX_ROUTE_SEGMENTS:
        raise MapPlotterError(
            f"route exceeds the {MAX_ROUTE_SEGMENTS} source-segment limit."
        )
    known_sources = set(source_ids)
    disciplines = [
        str(segment.get("discipline", route.get("discipline", "route")))
        for segment in raw_segments
        if isinstance(segment, dict)
    ]
    config = filtering_config(route, disciplines)
    processed: list[SportsSegment] = []
    removed: list[RemovedRoutePoint] = []
    reports: list[SegmentProcessingReport] = []
    total_points = 0
    total_length = 0.0
    segment_ids: set[str] = set()
    for segment_index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise MapPlotterError(f"route.segments[{segment_index}] must be an object.")
        segment_id = _text(
            raw_segment.get("id", f"segment-{segment_index + 1}"),
            f"route.segments[{segment_index}].id",
        )
        if segment_id in segment_ids:
            raise MapPlotterError(f"route repeats segment id {segment_id!r}.")
        segment_ids.add(segment_id)
        source_ref = _text(
            raw_segment.get("source_ref"), f"route segment {segment_id!r}.source_ref"
        )
        if source_ref not in known_sources:
            raise MapPlotterError(
                f"route segment {segment_id!r} references unknown source {source_ref!r}."
            )
        raw_points = raw_segment.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            raise MapPlotterError(
                f"route segment {segment_id!r} needs at least two points."
            )
        total_points += len(raw_points)
        if total_points > MAX_ROUTE_POINTS:
            raise MapPlotterError(
                f"route exceeds the {MAX_ROUTE_POINTS} source-point limit."
            )
        points = [
            _normalise_point(
                point,
                coordinate_space=coordinate_space,
                segment_id=segment_id,
                index=index,
            )
            for index, point in enumerate(raw_points)
        ]
        deduplicated, duplicates = _deduplicate(
            points,
            segment_id=segment_id,
            coordinate_space=coordinate_space,
            scale_m_per_unit=scale_m_per_unit,
            tolerance_m=config.duplicate_tolerance_m,
        )
        filtered, spikes = _remove_spikes(
            deduplicated,
            segment_id=segment_id,
            coordinate_space=coordinate_space,
            scale_m_per_unit=scale_m_per_unit,
            config=config,
        )
        if len(filtered) < 2:
            raise MapPlotterError(
                f"route segment {segment_id!r} is degenerate after filtering."
            )
        measured = sum(
            _distance_between(
                first,
                second,
                coordinate_space=coordinate_space,
                scale_m_per_unit=scale_m_per_unit,
            )
            for first, second in zip(filtered, filtered[1:])
        )
        if measured <= 0.0:
            raise MapPlotterError(
                f"route segment {segment_id!r} has no measurable retained length."
            )
        lap_raw = raw_segment.get("lap")
        if lap_raw is not None and (
            isinstance(lap_raw, bool) or not isinstance(lap_raw, int) or lap_raw <= 0
        ):
            raise MapPlotterError(
                f"route segment {segment_id!r}.lap must be a positive integer."
            )
        segment = SportsSegment(
            id=segment_id,
            discipline=_text(
                raw_segment.get("discipline", route.get("discipline", "route")),
                f"route segment {segment_id!r}.discipline",
            ).casefold(),
            source_ref=source_ref,
            points=tuple(filtered),
            lap=lap_raw,
            role=str(raw_segment.get("role", "course")),
            attributes={
                key: copy.deepcopy(value)
                for key, value in raw_segment.items()
                if key not in {"id", "discipline", "source_ref", "points", "lap"}
            },
        )
        processed.append(segment)
        removed.extend(duplicates)
        removed.extend(spikes)
        reports.append(
            SegmentProcessingReport(
                segment_id=segment_id,
                input_point_count=len(points),
                retained_point_count=len(filtered),
                duplicate_count=len(duplicates),
                spike_count=len(spikes),
                measured_length_m=measured,
            )
        )
        total_length += measured
    map_matching_raw = route.get("map_matching") or {}
    if not isinstance(map_matching_raw, dict):
        raise MapPlotterError("route.map_matching must be an object.")
    supplied_status = map_matching_raw.get("status")
    if supplied_status is None:
        map_matching = {
            "performed_by_sports_compiler": False,
            "status": "not-requested",
        }
    else:
        map_matching = {
            "performed_by_sports_compiler": False,
            "status": _text(supplied_status, "route.map_matching.status"),
            **{
                str(key): copy.deepcopy(value)
                for key, value in map_matching_raw.items()
                if key != "status"
            },
        }
    return RouteProcessingResult(
        coordinate_space=coordinate_space,
        scale_m_per_unit=scale_m_per_unit,
        segments=tuple(processed),
        filtering=config,
        removed_points=tuple(removed),
        reports=tuple(reports),
        measured_length_m=total_length,
        map_matching=map_matching,
    )


def _perpendicular_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    finish: tuple[float, float],
) -> float:
    if start == finish:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    dx = finish[0] - start[0]
    dy = finish[1] - start[1]
    return abs(
        dy * point[0]
        - dx * point[1]
        + finish[0] * start[1]
        - finish[1] * start[0]
    ) / math.hypot(dx, dy)


def _rdp_indices(
    points: Sequence[tuple[float, float]], start: int, finish: int, tolerance: float
) -> list[int]:
    if finish <= start + 1:
        return [start, finish] if finish > start else [start]
    maximum = -1.0
    selected = start
    for index in range(start + 1, finish):
        distance = _perpendicular_distance(points[index], points[start], points[finish])
        if distance > maximum:
            maximum = distance
            selected = index
    if maximum <= tolerance:
        return [start, finish]
    left = _rdp_indices(points, start, selected, tolerance)
    right = _rdp_indices(points, selected, finish, tolerance)
    return [*left[:-1], *right]


def _turn_protected_indices(
    points: Sequence[tuple[float, float]], threshold_degrees: float
) -> set[int]:
    protected: set[int] = set()
    for index in range(1, len(points) - 1):
        before = points[index - 1]
        current = points[index]
        after = points[index + 1]
        first = (current[0] - before[0], current[1] - before[1])
        second = (after[0] - current[0], after[1] - current[1])
        first_length = math.hypot(*first)
        second_length = math.hypot(*second)
        if min(first_length, second_length) <= 1e-12:
            continue
        cosine = max(
            -1.0,
            min(
                1.0,
                (first[0] * second[0] + first[1] * second[1])
                / (first_length * second_length),
            ),
        )
        change = math.degrees(math.acos(cosine))
        if change + 1e-9 >= threshold_degrees:
            protected.add(index)
    return protected


def _coincident_protected_indices(
    points: Sequence[tuple[float, float]], tolerance_mm: float
) -> set[int]:
    if tolerance_mm <= 0.0:
        return set()
    cell_size = tolerance_mm
    cells: dict[tuple[int, int], list[int]] = {}
    protected: set[int] = set()
    for index, point in enumerate(points):
        cell = (math.floor(point[0] / cell_size), math.floor(point[1] / cell_size))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for previous in cells.get((cell[0] + dx, cell[1] + dy), ()):
                    if index - previous <= 2:
                        continue
                    other = points[previous]
                    if math.hypot(point[0] - other[0], point[1] - other[1]) <= tolerance_mm:
                        protected.update((previous, index))
        cells.setdefault(cell, []).append(index)
    return protected


def simplify_physical_route(
    points: Sequence[tuple[float, float]],
    *,
    tolerance_mm: float,
    explicit_protected_indices: Iterable[int] = (),
    turn_threshold_degrees: float = 28.0,
    coincidence_tolerance_mm: float = 0.4,
) -> PhysicalSimplification:
    """Simplify in paper units while retaining turns, crossings, and controls."""

    if len(points) < 2:
        raise MapPlotterError("A physical route needs at least two projected points.")
    if not math.isfinite(tolerance_mm) or tolerance_mm < 0.0:
        raise MapPlotterError("Physical simplification tolerance must be non-negative.")
    protected = {0, len(points) - 1}
    protected.update(_turn_protected_indices(points, turn_threshold_degrees))
    protected.update(
        _coincident_protected_indices(points, coincidence_tolerance_mm)
    )
    for index in explicit_protected_indices:
        if not 0 <= int(index) < len(points):
            raise MapPlotterError("A protected route vertex index is out of range.")
        protected.add(int(index))
    boundaries = sorted(protected)
    retained: list[int] = []
    for start, finish in zip(boundaries, boundaries[1:]):
        section = _rdp_indices(points, start, finish, tolerance_mm)
        if retained and section and retained[-1] == section[0]:
            section = section[1:]
        retained.extend(section)
    if len(boundaries) == 1:
        retained = boundaries
    return PhysicalSimplification(
        points=tuple(points[index] for index in retained),
        retained_indices=tuple(retained),
        protected_indices=tuple(boundaries),
        tolerance_mm=tolerance_mm,
    )


def extrema_preserving_downsample(
    samples: Sequence[QuantitativeSample], *, max_points: int
) -> tuple[QuantitativeSample, ...]:
    """Downsample without interpolation, retaining bucket and global extrema."""

    if max_points < 4:
        raise MapPlotterError("A quantitative trace needs a four-point minimum budget.")
    if len(samples) <= max_points:
        return tuple(samples)
    if any(
        not math.isfinite(sample.distance_m) or not math.isfinite(sample.value)
        for sample in samples
    ):
        raise MapPlotterError("Quantitative samples must be finite before downsampling.")
    if any(
        second.distance_m < first.distance_m
        for first, second in zip(samples, samples[1:])
    ):
        raise MapPlotterError("Quantitative sample distances must be monotonic.")
    interior = samples[1:-1]
    bucket_count = max(1, (max_points - 2) // 2)
    chosen = {0, len(samples) - 1}
    for bucket in range(bucket_count):
        start = 1 + len(interior) * bucket // bucket_count
        finish = 1 + len(interior) * (bucket + 1) // bucket_count
        indices = range(start, max(start + 1, finish))
        minimum = min(indices, key=lambda index: (samples[index].value, index))
        maximum = max(indices, key=lambda index: (samples[index].value, -index))
        chosen.update((minimum, maximum))
    global_minimum = min(range(len(samples)), key=lambda index: samples[index].value)
    global_maximum = max(range(len(samples)), key=lambda index: samples[index].value)
    chosen.update((global_minimum, global_maximum))
    ordered = sorted(chosen)
    # Bucket extrema can exceed an odd budget by one.  Remove only unprotected
    # interior candidates with the smallest local deviation.
    protected = {0, len(samples) - 1, global_minimum, global_maximum}
    while len(ordered) > max_points:
        candidates: list[tuple[float, int]] = []
        for position in range(1, len(ordered) - 1):
            index = ordered[position]
            if index in protected:
                continue
            before = samples[ordered[position - 1]]
            current = samples[index]
            after = samples[ordered[position + 1]]
            span = max(after.distance_m - before.distance_m, 1e-12)
            expected = before.value + (
                (current.distance_m - before.distance_m) / span
            ) * (after.value - before.value)
            candidates.append((abs(current.value - expected), index))
        if not candidates:
            break
        _deviation, remove_index = min(candidates)
        ordered.remove(remove_index)
    return tuple(samples[index] for index in ordered)


def split_quantitative_runs(
    samples: Sequence[tuple[float, float | None]],
) -> tuple[tuple[QuantitativeSample, ...], ...]:
    """Split a trace at missing evidence; no gaps are interpolated."""

    runs: list[tuple[QuantitativeSample, ...]] = []
    current: list[QuantitativeSample] = []
    previous_distance = -math.inf
    for index, (raw_distance, raw_value) in enumerate(samples):
        distance = _finite_number(raw_distance, f"sample {index} distance")
        if distance < previous_distance:
            raise MapPlotterError("Quantitative sample distances must be monotonic.")
        previous_distance = distance
        value = _optional_number(raw_value, f"sample {index} value")
        if value is None:
            if current:
                runs.append(tuple(current))
                current = []
            continue
        current.append(QuantitativeSample(distance, value, index))
    if current:
        runs.append(tuple(current))
    return tuple(runs)


def _read_route_bytes(path: Path) -> bytes:
    try:
        stat = path.stat()
    except OSError as exc:
        raise MapPlotterError(f"Could not read route file {path}: {exc}") from exc
    if not path.is_file() or stat.st_size <= 0 or stat.st_size > MAX_ROUTE_FILE_BYTES:
        raise MapPlotterError(
            f"Route file must be a regular 1..{MAX_ROUTE_FILE_BYTES} byte file."
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MapPlotterError(f"Could not read route file {path}: {exc}") from exc


def _parse_xml(data: bytes, label: str) -> ET.Element:
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise MapPlotterError(f"{label} document types and entities are forbidden.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise MapPlotterError(f"Malformed {label}: {exc}") from exc


def _gpx_route(data: bytes, source_ref: str, discipline: str) -> dict[str, Any]:
    root = _parse_xml(data, "GPX")
    if _local_name(root.tag) != "gpx":
        raise MapPlotterError("GPX root element must be gpx.")
    segments: list[dict[str, Any]] = []
    for parent in root:
        parent_kind = _local_name(parent.tag)
        containers = (
            [child for child in parent if _local_name(child.tag) == "trkseg"]
            if parent_kind == "trk"
            else [parent] if parent_kind == "rte" else []
        )
        for container in containers:
            point_kind = "trkpt" if parent_kind == "trk" else "rtept"
            points: list[dict[str, Any]] = []
            for node in container:
                if _local_name(node.tag) != point_kind:
                    continue
                try:
                    lon = float(node.attrib["lon"])
                    lat = float(node.attrib["lat"])
                except (KeyError, ValueError) as exc:
                    raise MapPlotterError("GPX point has invalid lon/lat.") from exc
                point: dict[str, Any] = {"coordinates": [lon, lat]}
                elevation = _child_text(node, "ele")
                if elevation is not None:
                    point["elevation_m"] = _optional_number(
                        elevation, "GPX point elevation"
                    )
                timestamp = _child_text(node, "time")
                if timestamp:
                    point["timestamp"] = timestamp.strip()
                channels: dict[str, float | None] = {}
                for descendant in node.iter():
                    name = _local_name(descendant.tag).casefold()
                    channel = {
                        "hr": "heart_rate",
                        "cad": "cadence",
                        "power": "power",
                        "watts": "power",
                        "speed": "speed",
                    }.get(name)
                    if channel and descendant.text:
                        channels[channel] = _optional_number(
                            descendant.text, f"GPX {channel}"
                        )
                if channels:
                    point["channels"] = channels
                points.append(point)
            if len(points) >= 2:
                segments.append(
                    {
                        "id": f"gpx-segment-{len(segments) + 1}",
                        "discipline": discipline,
                        "source_ref": source_ref,
                        "points": points,
                    }
                )
    if not segments:
        raise MapPlotterError("GPX contains no two-point track or route segment.")
    return {
        "coordinate_space": "wgs84",
        "coordinate_order": "lon-lat",
        "geometry_status": "supplied-track",
        "segments": segments,
    }


def _tcx_route(data: bytes, source_ref: str, discipline: str) -> dict[str, Any]:
    root = _parse_xml(data, "TCX")
    if _local_name(root.tag) != "TrainingCenterDatabase":
        raise MapPlotterError("TCX root element must be TrainingCenterDatabase.")
    segments: list[dict[str, Any]] = []
    for track in (node for node in root.iter() if _local_name(node.tag) == "Track"):
        points: list[dict[str, Any]] = []
        for node in track:
            if _local_name(node.tag) != "Trackpoint":
                continue
            latitude = _descendant_text(node, {"LatitudeDegrees"})
            longitude = _descendant_text(node, {"LongitudeDegrees"})
            if latitude is None or longitude is None:
                continue
            point: dict[str, Any] = {
                "coordinates": [
                    _finite_number(longitude, "TCX longitude"),
                    _finite_number(latitude, "TCX latitude"),
                ]
            }
            elevation = _descendant_text(node, {"AltitudeMeters"})
            if elevation is not None:
                point["elevation_m"] = _optional_number(
                    elevation, "TCX elevation"
                )
            distance = _descendant_text(node, {"DistanceMeters"})
            if distance is not None:
                point["distance_m"] = _optional_number(distance, "TCX distance")
            timestamp = _descendant_text(node, {"Time"})
            if timestamp is not None:
                point["timestamp"] = timestamp.strip()
            channels: dict[str, float | None] = {}
            channel_tags = {
                "HeartRateBpm": "heart_rate",
                "Cadence": "cadence",
                "RunCadence": "cadence",
                "Speed": "speed",
                "Watts": "power",
            }
            for descendant in node.iter():
                channel = channel_tags.get(_local_name(descendant.tag))
                if channel is None:
                    continue
                # HeartRateBpm wraps a Value child in standard TCX, so the
                # wrapper itself commonly has no text node.
                raw_value = (
                    _descendant_text(descendant, {"Value"})
                    if channel == "heart_rate"
                    else descendant.text
                )
                if raw_value is not None:
                    channels[channel] = _optional_number(
                        raw_value, f"TCX {channel}"
                    )
            if channels:
                point["channels"] = channels
            points.append(point)
        if len(points) >= 2:
            segments.append(
                {
                    "id": f"tcx-track-{len(segments) + 1}",
                    "discipline": discipline,
                    "source_ref": source_ref,
                    "points": points,
                }
            )
    if not segments:
        raise MapPlotterError("TCX contains no track with two positioned points.")
    return {
        "coordinate_space": "wgs84",
        "coordinate_order": "lon-lat",
        "geometry_status": "supplied-track",
        "segments": segments,
    }


def _geojson_route(
    document: Any, source_ref: str, discipline: str
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []

    def add_line(coordinates: Any, properties: Mapping[str, Any]) -> None:
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return
        raw_channels = properties.get("channels") or {}
        if not isinstance(raw_channels, Mapping):
            raise MapPlotterError("GeoJSON feature channels must be an object.")
        timestamps = properties.get("timestamps") or []
        distances = properties.get("distances_m") or []
        points: list[dict[str, Any]] = []
        for index, coordinate in enumerate(coordinates):
            if not isinstance(coordinate, list) or len(coordinate) < 2:
                raise MapPlotterError("GeoJSON line position is malformed.")
            point: dict[str, Any] = {"coordinates": coordinate[:3]}
            channels = {
                str(name): values[index]
                for name, values in raw_channels.items()
                if isinstance(values, list) and index < len(values)
            }
            if channels:
                point["channels"] = channels
            if isinstance(timestamps, list) and index < len(timestamps):
                point["timestamp"] = timestamps[index]
            if isinstance(distances, list) and index < len(distances):
                point["distance_m"] = distances[index]
            points.append(point)
        segment_id = str(properties.get("id") or f"geojson-line-{len(segments) + 1}")
        segments.append(
            {
                "id": segment_id,
                "discipline": str(properties.get("discipline") or discipline),
                "source_ref": source_ref,
                "points": points,
                **({"lap": properties["lap"]} if "lap" in properties else {}),
            }
        )

    def walk(geometry: Any, properties: Mapping[str, Any]) -> None:
        if not isinstance(geometry, Mapping):
            return
        kind = geometry.get("type")
        if kind == "LineString":
            add_line(geometry.get("coordinates"), properties)
        elif kind == "MultiLineString":
            for coordinates in geometry.get("coordinates") or []:
                add_line(coordinates, properties)
        elif kind == "GeometryCollection":
            for child in geometry.get("geometries") or []:
                walk(child, properties)

    if not isinstance(document, Mapping):
        raise MapPlotterError("GeoJSON route must be an object.")
    kind = document.get("type")
    if kind == "FeatureCollection":
        for feature in document.get("features") or []:
            if isinstance(feature, Mapping):
                properties = feature.get("properties") or {}
                if not isinstance(properties, Mapping):
                    raise MapPlotterError("GeoJSON properties must be an object.")
                walk(feature.get("geometry"), properties)
    elif kind == "Feature":
        properties = document.get("properties") or {}
        if not isinstance(properties, Mapping):
            raise MapPlotterError("GeoJSON properties must be an object.")
        walk(document.get("geometry"), properties)
    else:
        walk(document, {})
    if not segments:
        raise MapPlotterError("GeoJSON contains no two-point line geometry.")
    return {
        "coordinate_space": "wgs84",
        "coordinate_order": "lon-lat",
        "geometry_status": "supplied-track",
        "segments": segments,
    }


def load_route_file(
    path: Path,
    *,
    source_ref: str,
    discipline: str = "run",
) -> dict[str, Any]:
    """Load GPX, TCX, KML, GeoJSON, or a normalized FIT-derived JSON route."""

    file_path = Path(path)
    data = _read_route_bytes(file_path)
    suffixes = [suffix.casefold() for suffix in file_path.suffixes]
    suffix = suffixes[-1] if suffixes else ""
    if suffix == ".gpx":
        return _gpx_route(data, source_ref, discipline)
    if suffix == ".tcx":
        return _tcx_route(data, source_ref, discipline)
    if suffix == ".kml":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MapPlotterError("KML must be UTF-8 text.") from exc
        parts = parse_kml(text)
        return {
            "coordinate_space": "wgs84",
            "coordinate_order": "lon-lat",
            "geometry_status": "supplied-track",
            "segments": [
                {
                    "id": f"kml-line-{index + 1}",
                    "discipline": discipline,
                    "source_ref": source_ref,
                    "points": [[lon, lat] for lat, lon in part],
                }
                for index, part in enumerate(parts)
            ],
        }
    if suffix in {".json", ".geojson"}:
        try:
            document = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MapPlotterError(f"Route JSON is malformed: {exc}") from exc
        if isinstance(document, Mapping) and isinstance(document.get("route"), Mapping):
            return copy.deepcopy(dict(document["route"]))
        if isinstance(document, Mapping) and isinstance(document.get("segments"), list):
            return copy.deepcopy(dict(document))
        return _geojson_route(document, source_ref, discipline)
    if suffix == ".fit":
        raise MapPlotterError(
            "Binary FIT decoding is not bundled. Supply the FIT-derived polyline "
            "through the existing JSON/GeoJSON loader so channel values and units "
            "remain explicit."
        )
    raise MapPlotterError(
        "Route file must be GPX, TCX, KML, GeoJSON, normalized JSON, or "
        "FIT-derived JSON."
    )
